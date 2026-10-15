"""Update engine for ND Data Updater.

Pure Python with no QGIS imports, so it can be tested outside QGIS. The plugin
supplies an HTTP client (network access through QGIS) and a Feedback object
(progress, cancel, log) and calls Updater.check_and_update() in a background task.

How an update works, for each source:
  1. Ask the source for its remote version without downloading the data
     (checksum sidecar file, HTTP ETag/Last-Modified, or latest git commit).
  2. If it matches the version recorded in manifest.json, nothing is downloaded.
  3. Otherwise download into <data folder>/.staging, verify it, then swap it into
     place with directory renames. The working copy is replaced only after a
     complete, verified download, so an interrupted update never leaves
     half-written data behind.
  4. If the network is unavailable, the source is reported "offline" and the
     existing local copy stays in use.

1.4.0: the lock file records the pid of the process that holds it, and a lock whose process is
gone is cleared instead of blocking every later run (killing QGIS in the middle of a 90-minute
pack build used to leave the data folder locked for six hours). Feedback also carries a one-line
`status` message ("layer 12 of 29: ...") beside the percentage, so a long run can say what it is
doing without filling the log.
"""

import datetime
import hashlib
import json
import os
import shutil
import time
import uuid
import zipfile

MANIFEST_NAME = "manifest.json"
STAGING_NAME = ".staging"
LOCK_NAME = ".update.lock"
LOCK_STALE_SECONDS = 6 * 3600


class NetworkError(Exception):
    """The source could not be reached (offline, DNS failure, timeout, 5xx)."""


class UpdateError(Exception):
    """The source was reached but the data is missing or invalid."""


class Cancelled(Exception):
    """The user cancelled the update."""


class Feedback:
    """Progress, cancellation and logging hooks supplied by the caller."""

    def __init__(self, progress=None, is_cancelled=None, log=None, status=None):
        self._progress = progress
        self._is_cancelled = is_cancelled
        self._log = log
        self._status = status
        self._base = 0.0
        self._span = 100.0

    def set_range(self, start_pct, end_pct):
        self._base = start_pct
        self._span = end_pct - start_pct

    def fraction(self, value):
        """Report progress within the current source, 0.0-1.0."""
        if self._progress and value is not None:
            self._progress(self._base + self._span * max(0.0, min(1.0, value)))

    def check_cancelled(self):
        if self._is_cancelled and self._is_cancelled():
            raise Cancelled()

    def log(self, message):
        if self._log:
            self._log(message)

    def status(self, text):
        """One line on what is happening now (replaces the last one; not the log)."""
        if self._status and text:
            self._status(text)


def _now_iso(now):
    return datetime.datetime.fromtimestamp(now).astimezone().isoformat(timespec="seconds")


def _iso_to_ts(value):
    try:
        return datetime.datetime.fromisoformat(value).timestamp()
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------------------ the lock file


def _win_pid_alive(pid):
    """1.5.0: is `pid` running, on Windows, without touching it.

    os.kill(pid, 0) must NOT be used on Windows: Python maps every signal other than
    CTRL_C_EVENT / CTRL_BREAK_EVENT to TerminateProcess, so "signal 0" would END the process in
    the lock file (the other QGIS mid-build). Ask the kernel through OpenProcess instead:
    ERROR_INVALID_PARAMETER (87) means no such pid (dead); ERROR_ACCESS_DENIED (5) means it
    exists but we may not query it (alive); otherwise read the exit code, where 259
    (STILL_ACTIVE) means running."""
    import ctypes
    from ctypes import wintypes
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    ERROR_INVALID_PARAMETER = 87
    k32 = ctypes.windll.kernel32
    handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return k32.GetLastError() != ERROR_INVALID_PARAMETER   # access-denied etc. -> exists
    try:
        code = wintypes.DWORD()
        if k32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return code.value == STILL_ACTIVE
        return True                        # could not read it, but the handle opened: it exists
    finally:
        k32.CloseHandle(handle)


def pid_alive(pid):
    """Is the process that wrote the lock still running?

    On Linux and macOS signal 0 asks the kernel without touching the process. A PermissionError
    means the pid belongs to somebody else's process and therefore exists, so it counts as
    alive: clearing a lock on a guess is worse than asking the user to wait. On Windows
    os.kill(pid, 0) would kill that process, so a kernel query is used instead (1.5.0)."""
    try:
        pid = int(pid)
    except (ValueError, TypeError):
        return False
    if os.name == "nt":
        try:
            return _win_pid_alive(pid)
        except Exception:
            return True                    # cannot tell on Windows: treat as alive, never kill
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, ValueError, TypeError):
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


def read_lock(path):
    """{"pid", "age", "text"} of a lock file, or {} when there is none.

    `pid` is None for a lock written by an older version (it held no pid) or one that cannot be
    read; `age` is its age in seconds."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read(200).strip()
        age = max(0.0, time.time() - os.path.getmtime(path))
    except OSError:
        return {}
    first = text.split()[0] if text else ""
    try:
        pid = int(first)
    except ValueError:
        pid = None
    return {"pid": pid, "age": age, "text": text}


def lock_decision(info, stale_seconds=LOCK_STALE_SECONDS, alive=pid_alive, own_pid=None):
    """("free" | "clear" | "refuse", why) for a lock file read by read_lock().

    "clear" means the lock is left over from a run that no longer exists (QGIS was killed, the
    machine was restarted) and can be removed: that is the fix for "every later run refuses to
    start". "refuse" is kept for a lock whose process really is still working, where removing it
    would let two builds write the same pack."""
    if not info:
        return "free", ""
    pid = info.get("pid")
    age = float(info.get("age") or 0.0)
    if pid is not None and pid == (os.getpid() if own_pid is None else own_pid):
        return "clear", f"it was left behind by this process (pid {pid})"
    if pid is None:
        if age > stale_seconds:
            return "clear", f"it records no process and is {age / 3600.0:.1f} hours old"
        return "refuse", "another update is already running"
    if not alive(pid):
        return "clear", f"the process that made it (pid {pid}) is no longer running"
    if age > stale_seconds:
        return "clear", (f"pid {pid} is still running but the lock is {age / 3600.0:.1f} hours "
                         "old, longer than an update can take")
    return "refuse", f"another update is already running (process {pid})"


def file_hash(path, algorithm="md5"):
    h = hashlib.new(algorithm)
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def extract_zip(zip_path, dest_dir, feedback):
    """Verify and extract a zip archive, rejecting entries that escape dest_dir."""
    try:
        with zipfile.ZipFile(zip_path) as zf:
            bad = zf.testzip()
            if bad is not None:
                raise UpdateError(f"Downloaded archive is corrupt (bad entry: {bad})")
            root = os.path.realpath(dest_dir)
            for member in zf.infolist():
                target = os.path.realpath(os.path.join(dest_dir, member.filename))
                if target != root and not target.startswith(root + os.sep):
                    raise UpdateError(f"Archive entry outside target folder: {member.filename}")
            members = zf.infolist()
            for i, member in enumerate(members):
                feedback.check_cancelled()
                zf.extract(member, dest_dir)
    except zipfile.BadZipFile as e:
        raise UpdateError(f"Downloaded file is not a valid zip archive: {e}") from e


# --------------------------------------------------------------------------- sources


class Source:
    """One dataset kept in its own sub-folder of the data folder."""

    id = ""
    name = ""
    description = ""
    folder = ""
    # Minimum hours between checks. 0 = check every time the updater runs.
    min_check_interval_hours = 0
    # Glob patterns (relative to the source folder) for layers to load in QGIS.
    layer_globs = ()
    # Substrings of layer names to add to the project from multi-layer files
    # (empty = add every layer). Used to add only buildings from the OSM GeoPackage.
    layer_filter = ()

    def remote_version(self, http, feedback):
        """Return the remote version string cheaply, or None if the data must be
        downloaded to tell whether it changed."""
        raise NotImplementedError

    def fetch(self, http, staging_dir, feedback, remote_version):
        """Download and prepare the data in staging_dir. Return the version string."""
        raise NotImplementedError


class HttpFileSource(Source):
    """A single file (usually a zip) at a fixed URL.

    version_mode:
      "md5"     - the server publishes <url>.md5; the download is verified against it.
      "headers" - use ETag, or Last-Modified plus Content-Length.
    """

    def __init__(self, id, name, url, filename, folder, version_mode="headers",
                 extract=True, layer_globs=(), description="", layer_filter=()):
        self.id = id
        self.name = name
        self.url = url
        self.filename = filename
        self.folder = folder
        self.version_mode = version_mode
        self.extract = extract
        self.layer_globs = tuple(layer_globs)
        self.layer_filter = tuple(layer_filter)
        self.description = description

    def remote_version(self, http, feedback):
        if self.version_mode == "md5":
            md5 = None
            try:
                text = http.get_bytes(self.url + ".md5").decode("utf-8", "replace").strip()
            except UpdateError:
                # the publisher serves no .md5 for this file (e.g. Geofabrik returns 404 for the
                # .gpkg.zip); fall back to ETag / Last-Modified rather than failing every check.
                # A NetworkError (a real outage) is not caught here, so it still surfaces.
                feedback.log(f"{self.name}: no .md5 published; using the server's "
                             f"ETag / Last-Modified instead")
            else:
                digest = text.split()[0].lower() if text else ""
                if len(digest) != 32 or any(c not in "0123456789abcdef" for c in digest):
                    raise UpdateError(f"Unexpected checksum file contents from {self.url}.md5")
                md5 = "md5:" + digest
            if md5:
                return md5
        headers = http.head(self.url)
        etag = headers.get("etag")
        if etag:
            return "etag:" + etag.strip()
        modified = headers.get("last-modified")
        if modified:
            return f"modified:{modified.strip()}|{headers.get('content-length', '')}"
        return None

    def fetch(self, http, staging_dir, feedback, remote_version):
        os.makedirs(staging_dir, exist_ok=True)
        path = os.path.join(staging_dir, self.filename)
        feedback.log(f"{self.name}: downloading {self.url}")
        http.download(self.url, path, feedback)
        feedback.check_cancelled()
        version = remote_version
        if self.version_mode == "md5" and str(remote_version).startswith("md5:"):
            actual = "md5:" + file_hash(path, "md5")
            if actual != remote_version:
                raise UpdateError(f"{self.name}: checksum mismatch after download; not installed")
            version = actual
        elif version is None:
            # no .md5 and no ETag/Last-Modified: record the download's own hash so the next
            # check can tell whether the file changed.
            version = "sha256:" + file_hash(path, "sha256")
        if self.extract and self.filename.lower().endswith(".zip"):
            extract_zip(path, staging_dir, feedback)
            os.remove(path)
        return version


class GitHubRepoSource(Source):
    """Source code of a GitHub repository branch, tracked by latest commit."""

    min_check_interval_hours = 0

    def __init__(self, id, name, owner, repo, branch, folder, description="",
                 commit=None, zip_sha256=None):
        # commit: pin to this reviewed commit instead of following the branch (supply-chain
        #   hygiene: the branch can be force-pushed under us). zip_sha256: the expected SHA-256
        #   of GitHub's archive of that commit; refused on mismatch when set. The download's
        #   SHA-256 is always logged so a change is visible even when no hash is pinned.
        self.commit = commit
        self.zip_sha256 = zip_sha256
        self.id = id
        self.name = name
        self.owner = owner
        self.repo = repo
        self.branch = branch
        self.folder = folder
        self.description = description

    def remote_version(self, http, feedback):
        if self.commit:
            return "git:" + self.commit          # pinned: the branch is not followed
        url = f"https://api.github.com/repos/{self.owner}/{self.repo}/commits/{self.branch}"
        try:
            data = json.loads(http.get_bytes(url, {"Accept": "application/vnd.github+json"}))
            return "git:" + data["sha"]
        except (ValueError, KeyError, TypeError) as e:
            raise UpdateError(f"{self.name}: unexpected response from GitHub") from e

    def fetch(self, http, staging_dir, feedback, remote_version):
        os.makedirs(staging_dir, exist_ok=True)
        ref = remote_version[4:] if remote_version else self.branch
        url = f"https://codeload.github.com/{self.owner}/{self.repo}/zip/{ref}"
        zip_path = os.path.join(staging_dir, "source.zip")
        http.download(url, zip_path, feedback)
        sha256 = file_hash(zip_path, "sha256")
        feedback.log(f"{self.name}: SHA-256 of {url} is {sha256}")
        if self.zip_sha256 and self.commit and ref == self.commit and sha256 != self.zip_sha256:
            raise UpdateError(f"{self.name}: SHA-256 mismatch for {self.owner}/{self.repo}@{ref} "
                              f"(got {sha256}, expected {self.zip_sha256}); not installed")
        unpack = os.path.join(staging_dir, "_unpack")
        extract_zip(zip_path, unpack, feedback)
        os.remove(zip_path)
        # GitHub archives wrap everything in one top-level folder; flatten it.
        entries = os.listdir(unpack)
        top = os.path.join(unpack, entries[0]) if len(entries) == 1 else unpack
        for name in os.listdir(top):
            shutil.move(os.path.join(top, name), os.path.join(staging_dir, name))
        shutil.rmtree(unpack, ignore_errors=True)
        with open(os.path.join(staging_dir, "COMMIT.txt"), "w") as f:
            f.write(f"{self.owner}/{self.repo}@{ref}\n")
        return remote_version or ("git:" + ref)


class OsmLayersSource(Source):
    """The five OSM_* pack layers, built from Geofabrik's North Dakota .osm.pbf.

    The pbf is the same OpenStreetMap data as the 'OpenStreetMap (Geofabrik)' source's
    GeoPackage, but with every tag, which the GeoPackage drops (culverts, fords, trailer
    parks, substations). Change detection is Geofabrik's published MD5 of the pbf; the pbf is
    deleted after the layers are written.
    """

    URL = "https://download.geofabrik.de/north-america/us/north-dakota-latest.osm.pbf"

    def __init__(self, id, name, folder, description=""):
        self.id = id
        self.name = name
        self.folder = folder
        self.description = description
        self.layer_globs = ("*.gpkg",)
        self.config_key = "osm-layers-1"

    def remote_version(self, http, feedback):
        text = http.get_bytes(self.URL + ".md5").decode("utf-8", "replace").strip()
        digest = text.split()[0].lower() if text else ""
        if len(digest) != 32 or any(c not in "0123456789abcdef" for c in digest):
            raise UpdateError(f"Unexpected checksum file contents from {self.URL}.md5")
        return "md5:" + digest

    def fetch(self, http, staging_dir, feedback, remote_version):
        from . import osm_layers
        os.makedirs(staging_dir, exist_ok=True)
        pbf = os.path.join(staging_dir, "north-dakota-latest.osm.pbf")
        feedback.log(f"{self.name}: downloading {self.URL}")
        feedback.set_range(0, 30)
        http.download(self.URL, pbf, feedback)
        feedback.check_cancelled()
        actual = "md5:" + file_hash(pbf, "md5")
        if remote_version and actual != remote_version:
            raise UpdateError(f"{self.name}: checksum mismatch after download; not installed")
        feedback.set_range(30, 95)
        counts = osm_layers.build_layers(
            pbf, staging_dir, tick=lambda n: feedback.check_cancelled(), say=feedback.log)
        os.remove(pbf)
        osm_layers.write_summary(staging_dir, counts, actual, self.URL)
        feedback.log(f"{self.name}: " + ", ".join(f"{k} {v:,}" for k, v in counts.items()))
        return actual


# North Dakota's 53 county FIPS codes: 38001, 38003, ... 38105.
ND_COUNTY_FIPS = tuple(f"38{n:03d}" for n in range(1, 106, 2))


# NSI versions served by the API: /nsiapi/<version>/structures. The unversioned
# /nsiapi/structures follows whatever USACE makes the default (nsi2026 since 2026),
# so the plugin always names the version it wants.
NSI_VERSIONS = {"nsi2026": "2026", "nsi2022": "2022"}
# What the NSI dataset holds: both versions merged (2026 wins where they overlap),
# or one version on its own.
NSI_MODES = {
    "merged": ("nsi2026", "nsi2022"),
    "nsi2026": ("nsi2026",),
    "nsi2022": ("nsi2022",),
}


class NsiApiSource(Source):
    """The published National Structure Inventory for North Dakota, from the NSI API.

    The API has no version header, so the counties are downloaded (at most every
    min_check_interval_hours) and a hash of the normalized features decides whether
    anything changed. mode "merged" downloads NSI 2026 and NSI 2022 and merges them
    county by county (nsi_merge.py: the 2026 point wins where both have a structure,
    2022-only structures are kept and flagged). A merger callable, when given,
    combines the county files into one GeoPackage (the plugin passes a GDAL-based
    merger).
    """

    API = "https://nsi.sec.usace.army.mil/nsiapi"

    def __init__(self, id, name, folder, county_fips=ND_COUNTY_FIPS, merger=None,
                 check_interval_hours=24 * 30, description="", mode="merged",
                 match_m=None):
        from . import nsi_merge
        if mode not in NSI_MODES:
            raise ValueError(f"unknown NSI mode {mode!r}")
        self.id = id
        self.name = name
        self.folder = folder
        self.county_fips = tuple(county_fips)
        self.merger = merger
        self.min_check_interval_hours = check_interval_hours
        self.description = description
        self.mode = mode
        self.match_m = float(match_m if match_m is not None else nsi_merge.DEFAULT_MATCH_M)
        self.layer_globs = ("nsi_nd.gpkg",) if merger else ("counties/*.geojson",)
        # A different mode or match distance needs a rebuild even inside the
        # check interval.
        self.config_key = mode + (f"|{self.match_m:g}m" if mode == "merged" else "")

    def remote_version(self, http, feedback):
        return None

    def _get_county(self, http, version, fips):
        raw = http.get_bytes(f"{self.API}/{version}/structures?fips={fips}&fmt=fc")
        try:
            data = json.loads(raw)
        except ValueError as e:
            raise UpdateError(f"{self.name}: county {fips} ({version}) returned invalid JSON") from e
        if not isinstance(data, dict) or data.get("type") != "FeatureCollection":
            raise UpdateError(f"{self.name}: county {fips} ({version}) returned an "
                              "unexpected response")
        features = data.get("features") or []
        # Hash features in a stable order so an identical inventory hashes identically.
        features.sort(key=lambda f: str((f.get("properties") or {}).get("fd_id", "")))
        return features

    def fetch(self, http, staging_dir, feedback, remote_version):
        from . import nsi_merge
        counties_dir = os.path.join(staging_dir, "counties")
        os.makedirs(counties_dir, exist_ok=True)
        versions = NSI_MODES[self.mode]
        digest = hashlib.sha256(self.config_key.encode())
        written = []
        totals = {}
        per_county = {}
        total = len(self.county_fips)
        for i, fips in enumerate(self.county_fips):
            by_version = {}
            for version in versions:
                feedback.check_cancelled()
                features = self._get_county(http, version, fips)
                digest.update(f"{fips}:{version}".encode())
                for feat in features:
                    digest.update(json.dumps(feat, sort_keys=True,
                                             separators=(",", ":")).encode())
                by_version[version] = features
            if self.mode == "merged":
                features, stats = nsi_merge.merge_versions(
                    by_version["nsi2022"], by_version["nsi2026"], match_m=self.match_m)
            else:
                features = by_version[versions[0]]
                for feat in features:
                    (feat.setdefault("properties", {}))["nsi_version"] = NSI_VERSIONS[versions[0]]
                stats = {"points_" + NSI_VERSIONS[versions[0]]: len(features)}
            by_version = None
            per_county[fips] = stats
            for k, v in stats.items():
                totals[k] = totals.get(k, 0) + v
            if features:
                path = os.path.join(counties_dir, f"{fips}.geojson")
                with open(path, "w", encoding="utf-8") as f:
                    json.dump({"type": "FeatureCollection", "features": features}, f,
                              separators=(",", ":"))
                written.append(path)
            features = None
            feedback.fraction((i + 1) / total * (0.8 if self.merger else 1.0))
        if not written:
            raise UpdateError(f"{self.name}: the API returned no structures")
        with open(os.path.join(staging_dir, "nsi_summary.json"), "w", encoding="utf-8") as f:
            json.dump({"mode": self.mode, "versions": list(versions),
                       "match_m": self.match_m if self.mode == "merged" else None,
                       "totals": totals, "counties": per_county}, f, indent=1, sort_keys=True)
        feedback.log(f"{self.name}: " + ", ".join(f"{k} {v:,}" for k, v in sorted(totals.items())))
        if self.merger:
            feedback.log(f"{self.name}: merging {len(written)} counties into nsi_nd.gpkg")
            self.merger(written, os.path.join(staging_dir, "nsi_nd.gpkg"))
            shutil.rmtree(counties_dir, ignore_errors=True)
        return "sha256:" + digest.hexdigest()


# --------------------------------------------------------------------------- updater


class Result:
    def __init__(self, source, status, message="", version=None):
        self.source_id = source.id
        self.name = source.name
        self.folder = source.folder
        self.status = status  # updated | current | skipped | offline | error | cancelled
        self.message = message
        self.version = version

    def __repr__(self):
        return f"Result({self.source_id!r}, {self.status!r}, {self.message!r})"


class Updater:
    def __init__(self, data_dir, sources, http, now=time.time):
        self.data_dir = data_dir
        self.sources = list(sources)
        self.http = http
        self.now = now

    # manifest -------------------------------------------------------------

    @property
    def manifest_path(self):
        return os.path.join(self.data_dir, MANIFEST_NAME)

    def load_manifest(self):
        try:
            with open(self.manifest_path, encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_manifest(self, manifest):
        tmp = self.manifest_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2, sort_keys=True)
        os.replace(tmp, self.manifest_path)

    def source_path(self, source):
        return os.path.join(self.data_dir, source.folder)

    # locking --------------------------------------------------------------

    def _acquire_lock(self, feedback=None):
        """(path, "") when the lock is ours, or (None, why) when another run holds it.

        A lock whose process is gone is cleared and said so once in the log, instead of refusing
        every run for six hours."""
        path = os.path.join(self.data_dir, LOCK_NAME)
        what, why = lock_decision(read_lock(path))
        if what == "clear":
            try:
                os.remove(path)
            except OSError:
                pass
            if feedback:
                feedback.log(f"Cleared the left-over lock file {path}: {why}")
        elif what == "refuse":
            return None, why
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return None, "another update started at the same moment"
        os.write(fd, f"{os.getpid()} {_now_iso(time.time())}".encode())
        os.close(fd)
        return path, ""

    # main entry -----------------------------------------------------------

    def check_and_update(self, feedback=None, force=False, only=None):
        """Check every source (or the ids in `only`) and install any changes.

        force=True ignores per-source minimum check intervals.
        """
        feedback = feedback or Feedback()
        os.makedirs(self.data_dir, exist_ok=True)
        lock, why = self._acquire_lock(feedback)
        if lock is None:
            raise UpdateError(f"Not started: {why} for this data folder. Wait for it to finish, "
                              f"or delete {os.path.join(self.data_dir, LOCK_NAME)} if you are "
                              "sure nothing is running.")
        try:
            shutil.rmtree(os.path.join(self.data_dir, STAGING_NAME), ignore_errors=True)
            sources = [s for s in self.sources if only is None or s.id in only]
            results = []
            for i, source in enumerate(sources):
                feedback.set_range(100.0 * i / len(sources), 100.0 * (i + 1) / len(sources))
                feedback.fraction(0.0)
                feedback.status(f"{source.name} ({i + 1} of {len(sources)})")
                result = self._update_one(source, feedback, force)
                results.append(result)
                feedback.log(f"{source.name}: {result.status}"
                             + (f" - {result.message}" if result.message else ""))
                if result.status == "cancelled":
                    break
            shutil.rmtree(os.path.join(self.data_dir, STAGING_NAME), ignore_errors=True)
            return results
        finally:
            try:
                os.remove(lock)
            except OSError:
                pass

    def _update_one(self, source, feedback, force):
        manifest = self.load_manifest()
        entry = manifest.get(source.id, {})
        target = self.source_path(source)
        have_local = os.path.isdir(target) and bool(os.listdir(target))
        now = self.now()

        config_key = getattr(source, "config_key", None)
        same_config = entry.get("config") == config_key
        if not force and have_local and same_config and source.min_check_interval_hours:
            last = _iso_to_ts(entry.get("checked_at"))
            if last is not None and now - last < source.min_check_interval_hours * 3600:
                days = (source.min_check_interval_hours * 3600 - (now - last)) / 86400
                return Result(source, "skipped", f"next check in {days:.0f} day(s)",
                              entry.get("version"))

        if getattr(source, "in_place", False):
            return self._update_in_place(source, feedback, force, entry, config_key, now)

        staging = os.path.join(self.data_dir, STAGING_NAME, f"{source.id}-{uuid.uuid4().hex[:8]}")
        try:
            feedback.check_cancelled()
            remote = source.remote_version(self.http, feedback)
            if (remote is not None and remote == entry.get("version") and have_local
                    and same_config):
                status = Result(source, "current", "", remote)
            else:
                version = source.fetch(self.http, staging, feedback, remote)
                feedback.check_cancelled()
                if version == entry.get("version") and have_local:
                    status = Result(source, "current", "", version)
                else:
                    self._swap(staging, target)
                    entry["version"] = version
                    entry["updated_at"] = _now_iso(now)
                    status = Result(source, "updated",
                                    "installed" if not have_local else "new data installed",
                                    version)
            entry["checked_at"] = _now_iso(now)
            entry.pop("last_error", None)
            if config_key is None:
                entry.pop("config", None)
            else:
                entry["config"] = config_key
        except Cancelled:
            return Result(source, "cancelled", "cancelled by user", entry.get("version"))
        except NetworkError as e:
            entry["last_error"] = f"offline: {e}"
            status = Result(source, "offline",
                            "using local copy" if have_local else f"not downloaded yet ({e})",
                            entry.get("version"))
        except Exception as e:  # noqa: BLE001 - one bad source must not stop the rest
            entry["last_error"] = str(e)
            status = Result(source, "error", str(e) or e.__class__.__name__, entry.get("version"))
        finally:
            shutil.rmtree(staging, ignore_errors=True)

        entry["name"] = source.name
        entry["folder"] = source.folder
        manifest = self.load_manifest()
        manifest[source.id] = entry
        self._save_manifest(manifest)
        return status

    def _update_in_place(self, source, feedback, force, entry, config_key, now):
        """A source that builds into its own folder and resumes there (the offline pack): it is
        not staged and swapped, because a stopped build must keep the layers it finished."""
        target = self.source_path(source)
        try:
            feedback.check_cancelled()
            status, message, version = source.update(self.http, target, feedback, force, entry)
            result = Result(source, status, message, version or entry.get("version"))
            if status == "updated":
                entry["version"] = version
                entry["updated_at"] = _now_iso(now)
            entry["checked_at"] = _now_iso(now)
            entry.pop("last_error", None)
            entry["config"] = config_key
        except Cancelled:
            return Result(source, "cancelled", "cancelled by user - the finished layers are kept",
                          entry.get("version"))
        except NetworkError as e:
            entry["last_error"] = f"offline: {e}"
            result = Result(source, "offline", f"using the local copy ({e})", entry.get("version"))
        except Exception as e:  # noqa: BLE001 - one bad source must not stop the rest
            entry["last_error"] = str(e)
            result = Result(source, "error", str(e) or e.__class__.__name__, entry.get("version"))
        entry["name"] = source.name
        entry["folder"] = source.folder
        manifest = self.load_manifest()
        manifest[source.id] = entry
        self._save_manifest(manifest)
        return result

    @staticmethod
    def _swap(staging, target):
        """Replace target with staging using renames (same file system)."""
        backup = target + ".previous"
        shutil.rmtree(backup, ignore_errors=True)
        if os.path.exists(target):
            os.rename(target, backup)
        try:
            os.rename(staging, target)
        except OSError:
            if os.path.exists(backup) and not os.path.exists(target):
                os.rename(backup, target)
            raise
        shutil.rmtree(backup, ignore_errors=True)
