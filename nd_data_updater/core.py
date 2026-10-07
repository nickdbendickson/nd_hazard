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

    def __init__(self, progress=None, is_cancelled=None, log=None):
        self._progress = progress
        self._is_cancelled = is_cancelled
        self._log = log
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


def _now_iso(now):
    return datetime.datetime.fromtimestamp(now).astimezone().isoformat(timespec="seconds")


def _iso_to_ts(value):
    try:
        return datetime.datetime.fromisoformat(value).timestamp()
    except (TypeError, ValueError):
        return None


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
            text = http.get_bytes(self.url + ".md5").decode("utf-8", "replace").strip()
            digest = text.split()[0].lower() if text else ""
            if len(digest) != 32 or any(c not in "0123456789abcdef" for c in digest):
                raise UpdateError(f"Unexpected checksum file contents from {self.url}.md5")
            return "md5:" + digest
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
        if self.version_mode == "md5":
            actual = "md5:" + file_hash(path, "md5")
            if remote_version and actual != remote_version:
                raise UpdateError(f"{self.name}: checksum mismatch after download; not installed")
            version = actual
        elif version is None:
            version = "sha256:" + file_hash(path, "sha256")
        if self.extract and self.filename.lower().endswith(".zip"):
            extract_zip(path, staging_dir, feedback)
            os.remove(path)
        return version


class GitHubRepoSource(Source):
    """Source code of a GitHub repository branch, tracked by latest commit."""

    min_check_interval_hours = 0

    def __init__(self, id, name, owner, repo, branch, folder, description=""):
        self.id = id
        self.name = name
        self.owner = owner
        self.repo = repo
        self.branch = branch
        self.folder = folder
        self.description = description

    def remote_version(self, http, feedback):
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

    def _acquire_lock(self):
        path = os.path.join(self.data_dir, LOCK_NAME)
        try:
            if time.time() - os.path.getmtime(path) > LOCK_STALE_SECONDS:
                os.remove(path)
        except OSError:
            pass
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return None
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return path

    # main entry -----------------------------------------------------------

    def check_and_update(self, feedback=None, force=False, only=None):
        """Check every source (or the ids in `only`) and install any changes.

        force=True ignores per-source minimum check intervals.
        """
        feedback = feedback or Feedback()
        os.makedirs(self.data_dir, exist_ok=True)
        lock = self._acquire_lock()
        if lock is None:
            raise UpdateError("Another update is already running for this data folder")
        try:
            shutil.rmtree(os.path.join(self.data_dir, STAGING_NAME), ignore_errors=True)
            sources = [s for s in self.sources if only is None or s.id in only]
            results = []
            for i, source in enumerate(sources):
                feedback.set_range(100.0 * i / len(sources), 100.0 * (i + 1) / len(sources))
                feedback.fraction(0.0)
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
