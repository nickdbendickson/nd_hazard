"""The offline pack as a source of the ND Data Updater (1.4.0).

One source, "pack": it builds the statewide offline data pack of the ND dam suite (a folder
`ND_Dam_Suite_Pack` of GeoPackages, one per layer, with manifest.json and LAYERS.txt) in the data
folder, and keeps it up to date. The layers are the ones the three dam plugins read: structures
(NSI, addresses, USA Structures), roads, rail, lifelines, dams (NID), the USGS 3DHP flowlines and
waterbodies, the SARP aquatic barrier dams and surveyed road-stream crossings, the DWR and HIFLD
layers (from files in <data folder>/dwr and /hifld), the NLCD land cover and the counties. The
building is done by ndpack/build_nd_pack.py, a copy of the ND Dam Hazard Helper's pack builder.

Each run reads the pack's manifest: a layer that is missing or incomplete is built, a layer older
than the refresh interval (default 30 days) is fetched again, and what is current is left alone.
A layer that cannot be fetched keeps its old copy.

3DHP: the USGS 3DHP web service often answers HTTP 504 / 502 to a state-sized request. In the
default mode ("auto") the updater asks it a small question first; when it does not answer, the
National Map's 3DHP national file (FileGDB zip, about 12 GB) is downloaded once into <data
folder>/3dhp/ and the flowlines and waterbodies are cut out of it. Mode "file" always does that,
mode "service" never does.

1.4.0, deciding about 3DHP in bounded time: the small probe was not enough. The service answers
it, the state-sized query then returns HTTP 504 for twenty-five minutes, and only after that
does anything fall back - so "auto" cost half an hour before it did the sensible thing. Now
`route_3dhp` asks the small question *and* the state-sized one (count only - the same request
the build makes, without the data), the whole decision is capped (`minutes_3dhp`, three minutes
by default), and when the cap is spent the National Map file route is taken with the reason in
the log. The cap is checked between attempts, so the overshoot is at most one request's timeout.

1.4.0, a layer the user replaced with their own file: its manifest entry carries `replaced`
(with when and by whom), and `plan` leaves it out of every refresh however old it is - of the
ordinary one, of "fetch every layer again" and of "rebuild only missing layers". A user who puts
their own county roads file in has better data than the service, and fetching over it on day 31
would throw that away without saying so. The one thing that does overwrite it is "Fetch again"
on that layer (`only_layers`), which is the user asking for the downloaded version back; that
run clears the flag, so the layer returns to the normal cycle.

1.4.0 also: `_share` calls damsuite.suitedata.adopt_folder, so one folder choice sets the suite
data folder, the pack location, the data source and the Dams folder together (the updater's data
folder and the plugins' ndsuite/data_folder drifting apart was the commonest first-run problem);
`only_layers` / `missing_only` let the window fetch one named layer again or only build what is
missing; the manifest is copied to manifest.previous.json before a refresh so the pack change
report (runreport) can diff it afterwards; and the run says in plain words what it did not get.

Pure Python (the builder needs GDAL only for the layer files, the land cover and the 3DHP file:
QGIS has it). No QGIS imports here.
"""

import datetime
import json
import os
import time
import urllib.parse
from urllib.request import Request

from . import core, runreport
from .ndpack.damsuite.netproxy import urlopen      # QGIS's proxy setting applies (1.4.2)

PACK_FOLDER = "ND_Dam_Suite_Pack"
THREE_DHP_LAYERS = ("3DHP_flowlines", "3DHP_waterbodies")
# the five OpenStreetMap layers: built by the "OpenStreetMap pack layers" source (osm_layers.py,
# from Geofabrik's full extract) into <data folder>/osm_layers; the pack takes them from there, and
# only without them does the pack builder ask Overpass (which often times out)
OSM_FOLDER = "osm_layers"
OSM_LAYERS = ("OSM_lifelines", "OSM_crossings", "OSM_campgrounds", "OSM_trailer_parks", "OSM_places")
MODES_3DHP = (
    ("auto", "Web service; the National Map file when the service does not answer"),
    ("file", "Always the National Map file (about 12 GB, downloaded once)"),
    ("service", "Web service only"),
)
_3DHP_COUNT = ("https://hydro.nationalmap.gov/arcgis/rest/services/3DHP_all/MapServer/50/query"
               "?where=1%3D1&returnCountOnly=true&f=json&inSR=4326&spatialRel="
               "esriSpatialRelIntersects&geometryType=esriGeometryEnvelope&geometry=")
# a five-minute-of-arc box: "is the service awake at all"
SERVICE_PROBE = _3DHP_COUNT + "-97.45,46.85,-97.40,46.90"
# the whole state: the question the build itself asks, with returnCountOnly so it costs nothing
# to answer - this is the one that comes back 504 while the small one succeeds
SERVICE_PROBE_STATE = _3DHP_COUNT + "-104.1,45.9,-96.5,49.1"
# how long "auto" may spend finding out whether the service is usable, before the file route
DEFAULT_3DHP_MINUTES = 3
TNM_PRODUCTS = ("https://tnmaccess.nationalmap.gov/api/v1/products?datasets="
                + urllib.parse.quote("3D Hydrography Program (3DHP)") + "&max=50"
                "&bbox=-104.1,45.9,-96.5,49.1")
UPDATER_VERSION = "1.6.0"
USER_AGENT = "ND-Data-Updater-QGIS/" + UPDATER_VERSION


def load_builder():
    """The vendored pack builder module (imports the shared damsuite beside it)."""
    from .ndpack import build_nd_pack
    build_nd_pack.C.PLUGIN_VERSION = "ND Data Updater " + UPDATER_VERSION    # the pack's "created by"
    return build_nd_pack


# ---------------------------------------------------------------- downloads --
def _open(url, headers=None, timeout=60):
    req = Request(url, headers=dict({"User-Agent": USER_AGENT}, **(headers or {})))
    return urlopen(req, timeout=timeout)


def get_json(url, timeout=60):
    try:
        with _open(url, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:  # noqa: BLE001 - any network / JSON failure is "not available"
        raise core.NetworkError(f"{url}: {e}") from e


def download_resumable(url, dest, feedback, chunk=1 << 20, retries=8, opener=None, pause=None):
    """Stream `url` to `dest` (a .part file next to it until complete), continuing a partial
    file with an HTTP Range request after a stop. The file is the size the server announces or
    the download fails (and can be run again to continue). `opener(url, headers)` replaces the
    network (tests); `pause(seconds)` the wait between tries."""
    opener = opener or (lambda u, h: _open(u, h, timeout=60))
    pause = pause or time.sleep
    part = dest + ".part"
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    last = None
    for attempt in range(retries):
        have = os.path.getsize(part) if os.path.exists(part) else 0
        try:
            resp = opener(url, {"Range": f"bytes={have}-"} if have else {})
            status = getattr(resp, "status", 200)
            if status == 416:                      # the partial file is already everything
                os.replace(part, dest)
                return dest
            length = int(resp.headers.get("Content-Length") or 0)
            if status == 206:
                total, mode = have + length, "ab"
            else:                                  # a server that ignores Range sends it all
                total, mode, have = length, "wb", 0
            with open(part, mode) as out:
                got = have
                while True:
                    feedback.check_cancelled()
                    block = resp.read(chunk)
                    if not block:
                        break
                    out.write(block)
                    got += len(block)
                    if total:
                        feedback.fraction(got / total)
            size = os.path.getsize(part)
            if total and size < total:
                raise core.NetworkError(f"stopped at {size} of {total} bytes")
            os.replace(part, dest)
            return dest
        except core.Cancelled:
            raise
        except Exception as e:  # noqa: BLE001 - any network failure: try again from where it is
            last = e
            feedback.log(f"download interrupted ({e}); try {attempt + 2} of {retries}")
            if attempt + 1 < retries:
                pause(min(30, 2 * (attempt + 1)))
    raise core.NetworkError(f"download failed: {last}")


def latest_3dhp_file(fetch=get_json):
    """(url, file name, date YYYYMMDD) of the newest CONUS 3DHP FileGDB on the National Map, or
    None. The National Map's own product list is asked, so a new annual file is found by itself."""
    data = fetch(TNM_PRODUCTS)
    best = None
    for item in (data or {}).get("items") or []:
        url = item.get("downloadURL") or ""
        title = (item.get("title") or "") + " " + (item.get("format") or "")
        if not url.lower().endswith(".zip") or "conus" not in (title + url).lower():
            continue
        if "gdb" not in (url + title).lower() or "filegdb" not in title.lower().replace(" ", "") \
                and "_gdb" not in url.lower():
            continue
        name = url.rsplit("/", 1)[-1]
        digits = [p for p in name.replace(".", "_").split("_") if p.isdigit() and len(p) == 8]
        date = digits[0] if digits else ""
        if best is None or date > best[2]:
            best = (url, name, date)
    return best


def file_date(path):
    """The YYYYMMDD in a 3DHP national file's name (""), for comparing with the newest."""
    name = os.path.basename(path or "")
    digits = [p for p in name.replace(".", "_").split("_") if p.isdigit() and len(p) == 8]
    return digits[0] if digits else ""


def ask_service(probe, budget_s=None, tries=3, gap=3.0, clock=time.monotonic,
                sleep=time.sleep):
    """(answered, seconds, detail): does `probe` give a count within `budget_s` seconds?

    The budget is checked before each attempt, so the time spent is at most the budget plus one
    request's own timeout - there is no way to interrupt a urllib read, and a hard deadline would
    mean a second thread for no gain. `budget_s` None = no cap (the old behaviour)."""
    t0 = clock()
    detail = "no answer"
    for k in range(max(1, int(tries))):
        if budget_s is not None and k and clock() - t0 >= budget_s:
            return False, clock() - t0, f"gave up after {clock() - t0:.0f} s ({detail})"
        try:
            data = probe()
            if isinstance(data, dict) and "count" in data:
                return True, clock() - t0, f"answered with {data['count']:,} feature(s)"
            detail = "the answer held no count"
        except Exception as e:  # noqa: BLE001 - a 504 / timeout / DNS failure all mean "no"
            detail = str(e) or e.__class__.__name__
        if k + 1 < tries:
            sleep(gap)
    return False, clock() - t0, detail


def service_answers(probe=None, tries=3, budget_s=None):
    """True when the 3DHP web service answers a small question (an HTTP 504 / 502 is the usual
    answer when it is struggling)."""
    probe = probe or (lambda: get_json(SERVICE_PROBE, timeout=45))
    return ask_service(probe, budget_s, tries)[0]


def route_3dhp(budget_s=None, probe=None, state_probe=None, tries=2, clock=time.monotonic,
               sleep=time.sleep):
    """("service" | "file", why) for mode "auto": where the 3DHP layers should come from.

    Two questions, both cheap, both inside one time budget: does the service answer at all, and
    does it answer the state-sized question the build actually makes? A service that passes the
    first and fails the second is exactly the case that used to cost 25 minutes of HTTP 504, so
    it is treated as down. The reason is returned for the log, because "it went and downloaded
    12 GB" needs an explanation."""
    probe = probe or (lambda: get_json(SERVICE_PROBE, timeout=45))
    state_probe = state_probe or (lambda: get_json(SERVICE_PROBE_STATE, timeout=90))
    cap = None if budget_s is None else max(1.0, float(budget_s))
    t0 = clock()

    def left():
        return None if cap is None else max(0.0, cap - (clock() - t0))

    ok, _secs, detail = ask_service(probe, left(), tries, clock=clock, sleep=sleep)
    if not ok:
        return "file", ("the web service does not answer a small question (" + detail
                        + ") - the National Map file is used")
    if cap is not None and left() <= 0:
        return "file", (f"the web service answered a small question but the {cap / 60.0:.0f}"
                        "-minute allowance for deciding is spent - the National Map file is used")
    ok, _secs, detail = ask_service(state_probe, left(), tries, clock=clock, sleep=sleep)
    if ok:
        return "service", "the web service answers the statewide question - it is used"
    spent = clock() - t0
    return "file", (f"the web service answers a small question but not the statewide one in "
                    f"{spent:.0f} s ({detail}) - the National Map file is used instead of "
                    "waiting for it")


# --------------------------------------------------------------------- source --
class PackSource(core.Source):
    """See the module text. `in_place`: built straight into the pack folder (it resumes after a
    stop, so it is not staged and swapped like the other sources)."""

    in_place = True
    min_check_interval_hours = 24

    def __init__(self, id, name, folder, data_dir, refresh_days=30, mode_3dhp="auto",
                 share=True, full=False, description="", box=None, builder=None,
                 probe=None, latest=None, downloader=None, now=None, minutes_3dhp=None,
                 only_layers=None, missing_only=False, state_probe=None, dams=None):
        self.id = id
        self.name = name
        self.folder = folder
        self.data_dir = data_dir
        self.refresh_days = max(1, int(refresh_days))
        self.mode_3dhp = mode_3dhp if mode_3dhp in dict(MODES_3DHP) else "auto"
        self.share = bool(share)
        self.full = bool(full)
        self.description = description
        self.box = box
        self._builder = builder
        self._probe = probe
        self._state_probe = state_probe
        self._latest = latest
        self._downloader = downloader
        self._now = now or datetime.datetime.now
        self.minutes_3dhp = max(1, int(minutes_3dhp or DEFAULT_3DHP_MINUTES))
        # one-shot run choices (not part of config_key: they do not change what the pack holds,
        # only what this run does): the window's "Fetch again" and "Rebuild only missing layers"
        self.only_layers = tuple(only_layers or ())
        self.missing_only = bool(missing_only)
        self._dams = dams
        self.report = {}                       # the last pack change report (runreport)
        self.summary = []                      # the last run's plain-words lines
        self.kept_replaced = []                # layers holding the user's own file, left alone
        # pack layers added to the QGIS project by "Add North Dakota Layers to Project"
        self.layer_globs = ("hydro/3DHP_*.gpkg", "barriers/SARP_*.gpkg")
        self.config_key = f"{self.refresh_days}d|{self.mode_3dhp}|{'full' if self.full else ''}"

    # nothing to ask without building: the manifest decides
    def remote_version(self, http, feedback):
        return None

    def fetch(self, http, staging_dir, feedback, remote_version):  # pragma: no cover
        raise NotImplementedError("the pack source builds in place (update())")

    @property
    def builder(self):
        if self._builder is None:
            self._builder = load_builder()
        return self._builder

    # ------------------------------------------------------------- what to do
    def _folders(self):
        d = self.data_dir
        return [os.path.join(d, "dwr"), os.path.join(d, "hifld")]

    def plan(self, pack, force=False):
        """(missing, stale, text): names to build, names to fetch again, and a one-line why.

        `only_layers` narrows the run to the layers the window named ("Fetch again" on one row);
        `missing_only` drops the stale list, which is the explicit "Rebuild only missing layers"
        button - the code already behaved that way for layers it had, and a user could not see
        that it did."""
        B = self.builder
        table = B.datasets()
        web = [t[0] for t in table if t[3] not in ("file", "raster")]
        files = [t[0] for t in table if t[3] == "file"]
        rasters = [t[0] for t in table if t[3] == "raster"]
        man = self._manifest(pack)
        sets = man.get("datasets") or {}
        osm = self.osm_files()
        web = [n for n in web if n not in osm]            # these come from the files, not Overpass
        have_r = set(man.get("rasters") or {})
        now = self._now()
        missing, stale = [], []
        for name in web:
            if name not in sets:
                missing.append(name)
        for name in rasters:
            if name not in have_r and name not in sets:
                missing.append(name)
        missing += self._file_todo(files, sets, man)
        for name in web:
            info = sets.get(name) or {}
            if name in missing or not info:
                continue
            if info.get("replaced"):
                continue                 # the user's own file: never fetched over by a refresh
            status = info.get("status") or ""
            age = self._age_days(info.get("fetched"), now)
            if self.full or (status and status not in ("complete", "not available")) \
                    or age is None or age > self.refresh_days:
                stale.append(name)
        if self.only_layers:
            known = {t[0] for t in table}
            chosen = [n for n in self.only_layers if n in known]
            missing = [n for n in chosen if n not in sets and n not in have_r]
            stale = [n for n in chosen if n not in missing]
            return missing, stale, f"{len(chosen)} layer(s) you asked for again"
        if self.missing_only and stale:
            stale = []
        self.kept_replaced = sorted(                 # every kind of layer, not only the web ones
            n for n, e in list(sets.items()) + list((man.get("rasters") or {}).items())
            if isinstance(e, dict) and e.get("replaced"))
        why = []
        if self.kept_replaced:
            why.append(f"{len(self.kept_replaced)} layer(s) left alone (your own file: "
                       + ", ".join(self.kept_replaced) + ")")
        if missing:
            why.append(f"{len(missing)} layer(s) not in the pack yet")
        if stale:
            why.append(f"{len(stale)} layer(s) older than {self.refresh_days} days or incomplete")
        elif self.missing_only:
            why.append("only the missing layers were asked for")
        return missing, stale, "; ".join(why)

    def _file_todo(self, files, sets, man):
        """DWR / HIFLD layers to add: those with a file in <data folder>/dwr or /hifld that the pack
        does not hold or holds from an older file; the DWR dams from DWR's public service when
        there is no file and the pack has none. Layers with no file are not asked for again."""
        try:
            from .ndpack import packfiles as PF
            found = PF.scan(self._folders())
        except Exception:  # noqa: BLE001
            found = {}
        out = []
        for name in files:
            info = sets.get(name)
            if info and info.get("replaced"):
                continue                 # the user's own file wins over a newer drop-in file
            if name in found:
                if not info:
                    out.append(name)
                    continue
                try:
                    newer = datetime.date.fromtimestamp(os.path.getmtime(found[name])).isoformat() \
                        > str(info.get("fetched") or "")[:10]
                except OSError:
                    newer = False
                if newer:
                    out.append(name)
            elif name == "DWR_dams" and not info:
                out.append(name)
        return out

    @staticmethod
    def _age_days(when, now):
        if not when:
            return None
        try:
            t = datetime.datetime.fromisoformat(str(when)[:19])
        except ValueError:
            return None
        return max(0.0, (now - t).total_seconds() / 86400.0)

    def _manifest(self, pack):
        try:
            from .ndpack.damsuite import gpkgpack as GP
            return GP.read_manifest(pack) or {}
        except Exception:  # noqa: BLE001 - no pack yet / unreadable: build it
            return {}

    # -------------------------------------------------------------- OpenStreetMap
    def osm_files(self):
        """{layer: GeoPackage} of the OSM layers the "OpenStreetMap pack layers" source has built."""
        out = {}
        for name in OSM_LAYERS:
            path = os.path.join(self.data_dir, OSM_FOLDER, name + ".gpkg")
            if os.path.isfile(path):
                out[name] = path
        return out

    def osm_todo(self, pack):
        """OSM layers to put in the pack: those it does not hold, or holds from an older file."""
        sets = (self._manifest(pack).get("datasets")) or {}
        out = {}
        for name, path in self.osm_files().items():
            info = sets.get(name)
            if info and info.get("replaced"):
                continue                 # the user's own file is not overwritten from osm_layers
            if info and info.get("status") == "complete":
                try:
                    if datetime.date.fromtimestamp(os.path.getmtime(path)).isoformat() \
                            <= str(info.get("fetched") or "")[:10]:
                        continue
                except OSError:
                    continue
            out[name] = path
        return out

    def _add_osm(self, pack, files, feedback):
        """Write the OSM layer files into the pack (clipped to its box). Returns the names added."""
        from .ndpack.damsuite import gpkgpack as GP
        table = {t[0]: t for t in self.builder.datasets()}
        man = GP.load_or_new(pack, GP._op().ND_BBOX)
        added = []
        for name, path in sorted(files.items()):
            label = table[name][1] if name in table else name
            info = {"label": label, "url": os.path.basename(path),
                    "source": "OpenStreetMap, Geofabrik North Dakota extract (.osm.pbf, every "
                              "tag), built by the ND Data Updater's OpenStreetMap pack layers "
                              "step"}
            try:
                e = GP.add_vector_file(pack, man, name, path, info, clip_bbox=man.get("bbox"),
                                       log=feedback.log)
                added.append(name)
                feedback.log(f"{self.name}: {name}: {e['count']:,} features from {path}")
            except Exception as ex:  # noqa: BLE001 - leave it to the builder (Overpass) next time
                feedback.log(f"{self.name}: {name} not added from {path}: {ex}")
        if added:
            GP.write_manifest(pack, man)
        return added

    # --------------------------------------------------------------------- 3DHP
    def _use_national_file(self, feedback):
        """Decide where 3DHP comes from. Returns the folder holding the national file to build
        from, or None for the web service. Downloads the file when it is needed and missing or a
        newer one is published."""
        if self.mode_3dhp == "service":
            return None
        folder = os.path.join(self.data_dir, "3dhp")
        if self.mode_3dhp == "auto":
            feedback.log(f"3DHP: asking the web service whether it answers (at most "
                         f"{self.minutes_3dhp} minute(s), then the National Map file)...")
            feedback.status("3DHP: asking the web service whether it answers")
            route, why = route_3dhp(self.minutes_3dhp * 60.0, self._probe, self._state_probe)
            feedback.log("3DHP: " + why)
            if route == "service":
                return None
        from .ndpack import packfiles as PF
        have = PF.scan_3dhp([folder])
        try:
            newest = (self._latest or latest_3dhp_file)()
        except core.NetworkError as e:
            newest = None
            feedback.log(f"3DHP: could not ask the National Map for the newest file ({e})")
        if newest and (not have or file_date(have) < newest[2]):
            url, name, _date = newest
            feedback.log(f"3DHP: downloading {name} (about 12 GB; it continues if stopped) to {folder}")
            feedback.fraction(0.0)
            (self._downloader or download_resumable)(url, os.path.join(folder, name), feedback)
            have = PF.scan_3dhp([folder])
        if not have:
            raise core.UpdateError(
                "3DHP: the web service does not answer and the National Map file could not be "
                "downloaded; put the 3DHP national file (FileGDB zip) in " + folder)
        return folder

    # ------------------------------------------------------------------- update
    def update(self, http, target, feedback, force, entry):
        """Build / refresh the pack. Returns (status, message, version)."""
        B = self.builder
        before = self._manifest(target)
        missing, stale, why = self.plan(target, force)
        osm = self.osm_todo(target)
        todo = missing + stale
        if not todo and not osm:
            return "current", "", entry.get("version") or "built"
        if osm:
            why = (why + "; " if why else "") + f"{len(osm)} OpenStreetMap layer file(s) to add"
        feedback.log(f"{self.name}: {why}")
        runreport.save_previous(target, before)    # for the change report after this run
        files = self._folders()
        if set(todo) & set(THREE_DHP_LAYERS):
            folder = self._use_national_file(feedback)
            if folder:
                files.append(folder)

        # the builder numbers its layers in the log ("[12/29] <label>"); StepTracker reads
        # them back out so the window can say which layer a 90-minute run is on
        steps = runreport.StepTracker()

        def say(text):
            steps.note(text)
            feedback.log(text)

        def progress(pct, msg=""):
            try:
                feedback.check_cancelled()
            except core.Cancelled as e:
                raise B.BuildCancelled("cancelled") from e
            feedback.fraction(pct / 100.0)
            feedback.status(steps.line(msg))

        kw = dict(progress=progress, log=say, files=files)
        have_osm = ",".join(sorted(self.osm_files()))
        if have_osm:
            kw["skip"] = have_osm                     # never ask Overpass for what the files give
        if self.box:
            kw["box"] = tuple(self.box)
        try:
            exists = bool(self._manifest(target))
            man = None
            if missing or not exists:
                # new pack: everything (the builder skips what it cannot get and says why)
                man = B.build(target, only=None if not exists else ",".join(missing),
                              refresh=False, **kw)
            if stale:
                man = B.build(target, only=",".join(stale), refresh=True, **kw)
            if osm:
                self._add_osm(target, osm, feedback)
                man = self._manifest(target)
        except B.BuildCancelled as e:
            raise core.Cancelled() from e
        except B.BuildError as e:
            raise core.UpdateError(str(e)) from e
        man = man or {}
        sets = man.get("datasets") or {}
        bad = sorted(k for k, v in sets.items()
                     if v.get("status") not in ("complete", "not available"))
        n = len(sets) + len(man.get("rasters") or {})
        msg = f"{n} layers" + (f"; not complete: {', '.join(bad)}" if bad else "")
        left = man.get("left_out") or {}
        if left:
            msg += f"; not in the pack: {', '.join(sorted(left))}"
        if self.only_layers:
            # an explicit "Fetch again" on a layer the user had replaced: they asked for the
            # downloaded version back, so the layer goes into the normal refresh cycle again
            back = runreport.clear_replaced(target, self.only_layers)
            if back:
                feedback.log(f"{self.name}: " + ", ".join(back) + " now follows the refresh "
                             "interval again (your own file was fetched over because you asked "
                             "for this layer)")
                man = self._manifest(target)
        if self.share:
            self._share(target, feedback)
        self.summary = runreport.pack_summary_lines(target, man)
        for line in self.summary:
            feedback.log(f"{self.name}: {line}")
        self.report = self._change_report(target, before, man, feedback)
        version = "built:" + self._now().strftime("%Y-%m-%d") + f":{n}"
        return "updated", msg, version

    def _change_report(self, pack, before, man, feedback):
        """Diff the manifest kept before the refresh against the new one (feature 5 / 1.4.0).

        Never raises: a report is a nicety at the end of a 90-minute build, and the build has
        already succeeded by the time this runs."""
        try:
            report = runreport.change_report(pack, old=before, new=man or None,
                                             dams=self.dams_folder())
        except Exception as e:  # noqa: BLE001 - the report must not fail the build
            feedback.log(f"{self.name}: the change report could not be made ({e})")
            return {}
        if report.get("summary"):
            feedback.log(f"{self.name}: {report['summary']}")
        if report.get("stale_text"):
            feedback.log(f"{self.name}: {report['stale_text']}")
        return report

    def dams_folder(self):
        """The suite's Dams folder, for the "which classified dams are now out of date" half of
        the change report ("" when the plugins have not been given one)."""
        if self._dams is not None:
            return self._dams
        try:
            from .ndpack.damsuite import suitedata as SD
            return SD.dams_folder() or ""
        except Exception:  # noqa: BLE001
            return ""

    def _share(self, pack, feedback):
        """Make the three dam plugins read this pack (their shared settings).

        1.4.0: through suitedata.adopt_folder on the *data folder*, not set_pack_location on the
        pack alone. Setting only the pack location left the plugins' own suite data folder
        pointing somewhere else, so their terrain cache, DWR drop-in folder and Dams folder were
        in a different place than the one the user chose here - the commonest first-run problem.
        adopt_folder sets the folder, creates its sub-folders, adopts the pack in it, switches
        the data source to the pack and takes <folder>/Dams as the Dams folder when none is set."""
        try:
            from .ndpack.damsuite import suitedata as SD
        except Exception as e:  # noqa: BLE001 - a settings problem must not fail the build
            feedback.log(f"{self.name}: could not reach the plugins' settings ({e})")
            return {}
        try:
            out = SD.adopt_folder(self.data_dir or os.path.dirname(pack.rstrip("\\/")))
            if not out.get("pack") or os.path.abspath(out["pack"]) != os.path.abspath(pack):
                SD.set_pack_location(pack)         # a pack built somewhere else than the folder
                SD.set_data_source(SD.SOURCE_PACK)
                out["pack"] = pack
            feedback.log(f"{self.name}: the ND dam plugins now read {out['pack']}"
                         + (f"; Dams folder {out['dams']}" if out.get("dams") else ""))
            if out.get("created"):
                feedback.log(f"{self.name}: created " + ", ".join(
                    f"{self.data_dir}/{s}" for s in out["created"]))
            return out
        except Exception as e:  # noqa: BLE001 - a settings problem must not fail the build
            feedback.log(f"{self.name}: could not set the plugins' data folder ({e})")
            return {}


# ------------------------------------------------------------------ the pack's own layers --
def layer_rows(pack):
    """[row] for every layer in the pack, for the window's per-layer table (runreport)."""
    return runreport.layer_rows(pack)


def vintage_rows(pack):
    """[(layer, source, downloaded date, feature count)] for a report's data-vintage stamp.

    Here as well as in runreport because this is where a reader looks for "what is in the pack";
    runreport holds the one implementation."""
    return runreport.vintage_rows(pack)


def replace_info(name, path, label="", table=None, who=None, now=None):
    """The manifest entry for a layer a user replaced with their own file.

    Kept separate from the writing so the bookkeeping can be tested without GDAL. The source
    text names the file, because six months later "where did this come from" is the question,
    and the label falls back to the builder's own label for the layer. `replaced` is what makes
    the refresh leave this layer alone; `replaced_at` and `replaced_by` are so the window and
    the end-of-run block can say when it happened and on whose account."""
    if not label and table:
        row = {t[0]: t for t in table}.get(name)
        label = row[1] if row else ""
    if who is None:
        try:
            import getpass
            who = getpass.getuser()
        except Exception:  # noqa: BLE001 - a nameless account is not a reason to fail
            who = ""
    stamp = (now or datetime.datetime.now()).isoformat(timespec="seconds")
    return {"label": label or name, "url": os.path.basename(path),
            "source": "your own file, put in by hand in the ND Data Updater: " + str(path),
            "fetched": datetime.date.fromtimestamp(
                os.path.getmtime(path)).isoformat() if os.path.exists(path)
            else datetime.date.today().isoformat(),
            "status": "complete", "replaced": True, "replaced_at": stamp,
            "replaced_by": who or ""}


def replace_layer(pack, name, path, log=None, builder=None):
    """Put a user's own file in the pack as layer `name` (feature 23).

    A user with a better county roads file must be able to drop it in; the layer is clipped to
    the pack's area and recorded with the file as its source, so the vintage table and the
    change report show where it came from. Needs GDAL (QGIS has it). Returns the manifest entry."""
    log = log or (lambda m: None)
    from .ndpack.damsuite import gpkgpack as GP
    if not os.path.exists(path):
        raise core.UpdateError(f"{path} does not exist")
    table = None
    try:
        table = (builder or load_builder()).datasets()
    except Exception:  # noqa: BLE001 - only used for the label
        pass
    man = GP.load_or_new(pack, GP._op().ND_BBOX)
    info = replace_info(name, path, table=table)
    entry = GP.add_vector_file(pack, man, name, path, info, clip_bbox=man.get("bbox"), log=log)
    # add_vector_file fills in its own fields; the three that say "this is the user's file" are
    # put back after it, so a later refresh can see them
    entry.update({k: info[k] for k in ("replaced", "replaced_at", "replaced_by")})
    man.setdefault("datasets", {})[name] = entry
    GP.write_manifest(pack, man)
    log(f"{name}: {entry.get('count', 0):,} features from your file {path}")
    return entry
