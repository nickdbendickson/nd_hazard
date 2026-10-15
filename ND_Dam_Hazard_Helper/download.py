# -*- coding: utf-8 -*-
"""Downloads for the hazard-classification step: USACE National Structure
Inventory (NSI) structures, ND GIS Hub site / structure address points,
FRA/BTS rail lines and grade crossings, National Inventory of Dams points
and lifeline facilities (ND GIS Hub, EIA,
OpenStreetMap Overpass).

Everything is limited to the study area (depth raster extent + 25 ft; the
grade crossings a few miles more for the trains/day lookup), goes through QGIS's own
network stack (so QGIS proxy and SSL settings apply), and is cached in
outputs/_downloads with the retrieval date so a report can be reproduced.
"""

import os
import re
import json
import math
import time
import queue
import hashlib
import gzip
import itertools
import threading
from urllib.parse import urlencode

from qgis.PyQt.QtCore import QUrl
from qgis.PyQt.QtNetwork import QNetworkRequest

from qgis.core import QgsBlockingNetworkRequest, QgsCoordinateReferenceSystem, QgsVectorLayer

try:
    from qgis.core import QgsSettings
except ImportError:                          # pragma: no cover
    QgsSettings = None

from .compat import NET_NO_ERROR, MSG_WARNING, MSG_INFO, enum_value
from .constants import (
    NSI_API_URL, DOWNLOAD_TILE_DEG, ARCGIS_PAGE_SIZE, ARCGIS_MAX_PAGES,
    DOWNLOAD_FOLDER_NAME, ND_ADDRESS_POINTS_URL, USA_STRUCTURES_URL,
    NID_URL, NID_MIRROR_URL, OVERPASS_URL, HTTP_USER_AGENT,
    NBI_URL, DOWNLOAD_WORKERS, OVERPASS_WORKERS,
)
from .helpers import log_message, CancelledByUser
# v2.6.1: the Overpass queries / parsing live in osm_parse.py (shared with the
# offline data pack builder); the names stay importable from here
from .osm_parse import (  # noqa: F401
    overpass_query, overpass_to_features, crossing_category, camp_features, camps_query,
    place_features, crossings_query, crossing_features, places_query, _circle_ll,
    _DEAD_RAIL, trailer_parks_query, trailer_park_features,
)

NSI_MAX_TILES = 400          # 0.2 deg tiles: ~4 x 4 degrees, far beyond any dam-break model

# v2.6: requests run side by side (services, pages, tiles). One cap for the
# whole plugin, whatever the nesting; Overpass has its own smaller one.
_HTTP_SLOTS = threading.BoundedSemaphore(max(1, DOWNLOAD_WORKERS))
_OVERPASS_SLOTS = threading.BoundedSemaphore(max(1, OVERPASS_WORKERS))
PARALLEL = True              # False: everything one after another (debugging)
_ZOMBIE_POOLS = []           # pools left after a cancel (deleting one would wait)
_POOLS_LOCK = threading.Lock()   # nested parallel_map calls prune from several threads


class DownloadError(Exception):
    pass


class NotInPack(DownloadError):
    """v2.6.1 offline mode: the data set is not in the offline data pack (no
    internet request is made)."""


def http_get_bytes(url, user_agent=None):
    if offline_config()[1]:
        raise NotInPack("offline mode - no internet requests ({})".format(url.split("?")[0]))
    req = QgsBlockingNetworkRequest()
    qreq = QNetworkRequest(QUrl(url))
    if user_agent:
        try:
            qreq.setRawHeader(b"User-Agent", user_agent.encode("ascii"))
        except Exception:
            pass
    with _HTTP_SLOTS:
        err = req.get(qreq, True)
    if enum_value(err) != enum_value(NET_NO_ERROR):
        raise DownloadError(req.errorMessage() or "network error")
    content = req.reply().content()
    if isinstance(content, (bytes, bytearray)):
        return bytes(content)
    return bytes(content.data())


# ---------------------------------------------------------------- parallel --
class _Progress(object):
    """Thread-safe record of each job's latest progress; the coordinator
    thread turns it into one callback (Qt widgets are never touched from a
    worker)."""

    def __init__(self, cancel=None):
        self.lock = threading.Lock()
        self.state = {}
        self.cancel = cancel

    def for_job(self, key):
        def cb(pct, msg=""):
            # v2.6.1: a cancelled run stops the job's own tiles / pages too
            # (nested parallel_map, paging loops) instead of letting them run on
            if self.cancel is not None and self.cancel.is_set():
                raise CancelledByUser()
            with self.lock:
                self.state[key] = (float(pct), msg)
        return cb

    def summary(self):
        with self.lock:
            return dict(self.state)


class _PoolHandle(object):
    """A running pool and the Python jobs it runs.

    v2.6.1 (fixes QGIS freezing during downloads): a Qt pool must never be
    destroyed, and Qt must never delete one of its Python jobs, while a job
    may still be running. QThreadPool's destructor waits for its threads
    while holding Python's GIL (PyQt does not release it there), and a
    Python job needs the GIL to finish - and so did Qt's delete of a
    finished Python job (autoDelete). The thread that dropped the pool then
    waited for a job that waited for the GIL: the download never finished
    and, as every Python call in QGIS needs the GIL, QGIS froze. Now the
    jobs are kept here (autoDelete off), the pool is waited for with the
    GIL released before it is let go, and a pool whose jobs may still run
    (cancel, error) is parked in _ZOMBIE_POOLS until its threads are idle."""

    def __init__(self, pool=None, jobs=(), executor=None, futures=()):
        self.pool = pool
        self.jobs = list(jobs)
        self.executor = executor
        self.futures = list(futures)

    def idle(self):
        try:
            if self.pool is not None:
                return self.pool.activeThreadCount() == 0
            return all(f.done() for f in self.futures)
        except Exception:
            return False                        # unsure: keep it

    def wait(self):
        """Wait until every job has returned (PyQt releases the GIL in
        waitForDone, so a job that still needs it can finish)."""
        if self.pool is not None:
            self.pool.waitForDone()
        elif self.futures:
            from concurrent.futures import wait as _wait
            _wait(self.futures)


def _run_pool(runnables, workers):
    """Start callables on a pool; returns a function that waits for the
    next finished (key, ok, value) with a timeout (None = nothing yet). The
    function's .handle (_PoolHandle) must be waited for or parked, never
    just dropped."""
    q = queue.Queue()

    def wrap(key, fn):
        def go():
            try:
                q.put((key, True, fn()))
            except BaseException as e:            # noqa - handed to the coordinator
                q.put((key, False, e))
        return go
    handle = None
    try:
        from qgis.PyQt.QtCore import QThreadPool, QRunnable

        class _Job(QRunnable):
            def __init__(self, go):
                super().__init__()
                self.go = go
                # Qt must not delete a Python job in its worker thread (the
                # delete needs the GIL) - _PoolHandle keeps it instead
                self.setAutoDelete(False)

            def run(self):
                self.go()
        pool = QThreadPool()
        pool.setMaxThreadCount(workers)
        handle = _PoolHandle(pool, [_Job(wrap(key, fn)) for key, fn in runnables])
    except Exception:
        handle = None
    if handle is not None:
        try:
            for job in handle.jobs:
                handle.pool.start(job)
        except BaseException:
            _park(handle)                        # some jobs may be running
            raise
    else:
        from concurrent.futures import ThreadPoolExecutor
        ex = ThreadPoolExecutor(max_workers=workers)
        futures = [ex.submit(wrap(key, fn)) for key, fn in runnables]
        ex.shutdown(wait=False)
        handle = _PoolHandle(executor=ex, futures=futures)

    def next_done(timeout):
        try:
            return q.get(timeout=timeout)
        except queue.Empty:
            return None
    next_done.handle = handle
    return next_done


def parallel_map(jobs, progress_cb=None, label="Downloading", workers=None):
    """Run jobs = [(key, fn, args, kwargs)] side by side; fn may take a
    progress_cb keyword (it gets a thread-safe one). Returns {key: (ok,
    value or exception)}. progress_cb(pct, msg) is called from the calling
    thread only; a CancelledByUser it raises stops the wait at once (jobs
    not yet started are skipped, running ones finish on their own)."""
    out = {}
    if not jobs:
        return out
    from .helpers import prime_ellipsoids
    prime_ellipsoids()          # v2.20.1: in the calling thread, before the workers
    workers = max(1, int(workers or DOWNLOAD_WORKERS))
    cancel = threading.Event()
    prog = _Progress(cancel)
    n = len(jobs)
    names = {k: k for k, _f, _a, _kw in jobs}

    def make(key, fn, args, kwargs):
        def go():
            if cancel.is_set():
                raise CancelledByUser()
            kw = dict(kwargs)
            kw["progress_cb"] = prog.for_job(key)
            try:
                return fn(*args, **kw)
            except TypeError as e:
                if "progress_cb" not in str(e):
                    raise
                kw.pop("progress_cb", None)
                return fn(*args, **kw)
        return go
    if workers == 1 or not PARALLEL or n == 1:
        for i, (key, fn, args, kwargs) in enumerate(jobs):
            if progress_cb:
                progress_cb(100.0 * i / n, "{}: {}".format(label, names[key]))
            try:
                out[key] = (True, make(key, fn, args, kwargs)())
            except CancelledByUser:
                raise
            except Exception as e:
                out[key] = (False, e)
        if progress_cb:
            progress_cb(100.0, "{}: done".format(label))
        return out
    _prune_pools()
    _warm_transforms()
    next_done = _run_pool([(k, make(k, f, a, kw)) for k, f, a, kw in jobs], workers)
    done = 0
    last = 0.0
    pump = _event_pump()
    try:
        while done < n:
            item = next_done(0.25)
            if item is None and pump is not None:
                pump()                     # main thread (Processing): stay responsive
            if item is not None:
                key, ok, val = item
                if not ok and isinstance(val, CancelledByUser):
                    raise val
                out[key] = (ok, val)
                done += 1
            if progress_cb and (item is not None or time.monotonic() - last > 0.5):
                last = time.monotonic()
                busy = [str(names[k]) for k in names if k not in out]
                sub = prog.summary()
                inner = sum(min(100.0, max(0.0, sub.get(k, (0.0, ""))[0])) for k in busy)
                pct = 100.0 * (done + inner / 100.0) / n
                progress_cb(pct, "{}: {} of {} done{}".format(
                    label, done, n, " - waiting for " + ", ".join(busy[:4]) +
                    (" ..." if len(busy) > 4 else "") if busy else ""))
    except BaseException:
        # cancel (or any error): jobs not yet started are skipped, running
        # requests finish on their own; the pool is parked until they have
        # (letting it go now would wait for them holding the GIL - freeze)
        cancel.set()
        _park(next_done.handle)
        raise
    # every job has reported; wait for the last ones to return before the
    # pool is let go (v2.6.1, see _PoolHandle)
    next_done.handle.wait()
    _adopt_results(out)
    _repair_transforms()
    return out


# QGIS 3.34 thread bug (found on live data, 2.18.x): a QgsVectorLayer made in a download worker thread
# keeps a coordinate reference system whose PROJ state belongs to that thread. Once the thread has ended
# (the pool is let go) the layer reads fine, but every NEW transformation made from its CRS is INVALID -
# and an invalid transformation reports isShortCircuited() == True, which reads as "same CRS". Filter
# rectangles then stay in the wrong CRS and every structure, road and rail segment falls "outside the
# study area" (the dam is then classified Low from nothing). The jobs hand layers back, so each layer is
# re-created here, in the calling thread, from its file (memory layers are copied) before anyone uses it.
_ORPHANS = []      # worker-thread layers: kept alive so Python never deletes them from the wrong thread


def _adopt_layer(lyr):
    try:
        from qgis.core import QgsVectorLayer
        if not isinstance(lyr, QgsVectorLayer) or not lyr.isValid():
            return lyr
        authid = lyr.crs().authid() or "EPSG:4326"
        if lyr.providerType() == "memory":
            from qgis.core import QgsWkbTypes
            new = QgsVectorLayer("{}?crs={}".format(QgsWkbTypes.displayString(lyr.wkbType()),
                                                    authid), lyr.name(), "memory")
            prov = new.dataProvider()
            prov.addAttributes(list(lyr.fields()))
            new.updateFields()
            prov.addFeatures(list(lyr.getFeatures()))
            new.updateExtents()
        else:
            new = QgsVectorLayer(lyr.source(), lyr.name(), lyr.providerType())
            if not new.isValid():
                return lyr
            if not new.crs().isValid():
                new.setCrs(QgsCoordinateReferenceSystem(authid))
        _ORPHANS.append(lyr)
        return new
    except Exception:
        return lyr


def _adopt_value(v):
    if isinstance(v, tuple):
        return tuple(_adopt_value(x) for x in v)
    if isinstance(v, list):
        return [_adopt_value(x) for x in v]
    return _adopt_layer(v)


def _adopt_results(out):
    """parallel_map's results {key: (ok, value)}: layers made by the workers are replaced by
    equivalents made in this (the calling) thread."""
    if threading.current_thread() is not threading.main_thread():
        return
    for k, (ok, val) in list(out.items()):
        if ok:
            out[k] = (ok, _adopt_value(val))


# QGIS 3.34 race (found on live data, 2.18.x): when several threads create the same
# coordinate transformation at once (every download job reprojects), QGIS can cache the
# first attempt as INVALID and hand that to later callers, so EPSG:4326 -> raster-CRS
# transforms silently return the input unchanged and every structure falls "outside the
# study area". The main thread creates the common pairs first (workers then only read the
# cache) and, once the jobs are done, drops any cache entry the race may have poisoned.
_WARM_PAIRS = (("EPSG:4326", "EPSG:3857"), ("EPSG:3857", "EPSG:4326"), ("EPSG:4326", "EPSG:5070"),
               ("EPSG:5070", "EPSG:4326"), ("EPSG:4326", "EPSG:26914"), ("EPSG:26914", "EPSG:4326"))


def _warm_transforms():
    try:
        from qgis.core import QgsCoordinateTransform, QgsCoordinateTransformContext
        if threading.current_thread() is not threading.main_thread():
            return
        ctx = QgsCoordinateTransformContext()
        for a, b in _WARM_PAIRS:
            QgsCoordinateTransform(QgsCoordinateReferenceSystem(a), QgsCoordinateReferenceSystem(b), ctx)
    except Exception:
        pass


def _repair_transforms():
    try:
        from qgis.core import QgsCoordinateTransform, QgsCoordinateTransformContext
        if threading.current_thread() is not threading.main_thread():
            return
        ctx = QgsCoordinateTransformContext()
        bad = [(a, b) for a, b in _WARM_PAIRS
               if not QgsCoordinateTransform(QgsCoordinateReferenceSystem(a),
                                             QgsCoordinateReferenceSystem(b), ctx).isValid()]
        # an invalid entry for a pair we do not test would still hurt: always clear the cache
        QgsCoordinateTransform.invalidateCache()
        if bad:
            log_message("Coordinate transformations {} were invalid after the parallel downloads "
                        "(QGIS thread race); the transform cache was cleared".format(
                            ", ".join("{}->{}".format(a, b) for a, b in bad)), MSG_WARNING)
    except Exception:
        pass


def _park(handle):
    """Keep a pool whose jobs may still run; jobs not yet started are
    dropped from its queue (they would only find the run cancelled)."""
    try:
        if handle.pool is not None:
            handle.pool.clear()
    except Exception:
        pass
    with _POOLS_LOCK:
        _ZOMBIE_POOLS.append(handle)


def _prune_pools():
    """Let go of parked pools whose jobs have all returned."""
    with _POOLS_LOCK:
        idle = [h for h in _ZOMBIE_POOLS if h.idle()]
        for h in idle:
            _ZOMBIE_POOLS.remove(h)
    del idle[:]                               # released here, outside the lock


_LEAKED = []                 # pools deliberately never destroyed (see below)


def wait_for_parked_pools(timeout_s=15.0):
    """Plugin unload / QGIS exit: wait (GIL released) until the requests of
    cancelled runs have returned, so no pool is destroyed with a job still
    running - that would freeze QGIS on exit. v2.6.2: at most timeout_s (a
    request waiting for a password / certificate prompt on the main thread
    would never return while the main thread waits here); a pool still busy
    then is kept alive for good (never destroyed, so it cannot block).
    Returns how many pools were parked."""
    with _POOLS_LOCK:
        handles = list(_ZOMBIE_POOLS)
        del _ZOMBIE_POOLS[:]
    end = time.monotonic() + max(0.0, timeout_s)
    for h in handles:
        try:
            while not h.idle() and time.monotonic() < end:
                if h.pool is not None:
                    h.pool.waitForDone(200)      # PyQt releases the GIL
                else:
                    time.sleep(0.2)
            if not h.idle():
                _keep_forever(h)
        except Exception:
            _keep_forever(h)
    return len(handles)


def _keep_forever(handle):
    """A pool that may still run jobs must never be destroyed - not even
    when Python shuts down with QGIS (module cleanup would destroy it)."""
    _LEAKED.append(handle)
    try:
        import ctypes
        ctypes.pythonapi.Py_IncRef(ctypes.py_object(handle))
    except Exception:
        pass
    log_message("A download of a cancelled run was still running when the plugin was "
                "unloaded; it is left to finish on its own.", MSG_WARNING)


def _event_pump():
    """processEvents when the coordinator is the GUI thread (a network
    prompt in a worker must not deadlock behind it); None off the main
    thread."""
    try:
        from .helpers import on_main_thread
        if not on_main_thread():
            return None
        from qgis.PyQt.QtCore import QCoreApplication
        return QCoreApplication.processEvents
    except Exception:
        return None


def http_get_json(url, user_agent=None):
    raw = http_get_bytes(url, user_agent)
    try:
        data = json.loads(raw.decode("utf-8"))
    except ValueError as e:
        raise DownloadError("invalid JSON from {} ({})".format(url.split("?")[0], e)) from e
    if isinstance(data, dict) and "error" in data:
        err = data["error"]
        msg = err.get("message") if isinstance(err, dict) else str(err)
        raise DownloadError("service error: {}".format(msg))
    return data


def download_folder(output_folder):
    folder = os.path.join(output_folder, DOWNLOAD_FOLDER_NAME)
    os.makedirs(folder, exist_ok=True)
    return folder


def _cache_path(folder, name, extent):
    key = "{:.4f},{:.4f},{:.4f},{:.4f}".format(*extent)
    digest = hashlib.sha1(key.encode("ascii")).hexdigest()[:8]
    return os.path.join(folder, "{}_{}_{}.geojson".format(
        name, time.strftime("%Y%m%d"), digest))


_CACHE_RE = re.compile(r"^(.*)_(\d{8})_([0-9a-f]{8})\.geojson$")
# v2.4.1: downloads replaced by an older cached copy because the service
# failed this run: [(name, "YYYY-MM-DD", file name)]. Cleared by the pipeline
# at the start of every run and reported in the summary / run log.
STALE_USED = []


def _stale_cache(path):
    """Newest earlier copy of the same download (same name and study area,
    another day) next to `path`, or None."""
    folder, base = os.path.split(path)
    m = _CACHE_RE.match(base)
    if not m or not os.path.isdir(folder):
        return None
    name, _day, digest = m.groups()
    best = None
    for f in os.listdir(folder):
        mm = _CACHE_RE.match(f)
        if mm and f != base and mm.group(1) == name and mm.group(3) == digest:
            if best is None or mm.group(2) > best[0]:
                best = (mm.group(2), f)
    return os.path.join(folder, best[1]) if best else None


def _with_stale_fallback(path, name, live):
    """live() -> (layer, count, path). When the service fails, the newest
    earlier download of the same area is used instead and recorded in
    STALE_USED; without one the error is raised as before."""
    try:
        return live()
    except CancelledByUser:
        raise
    except Exception as e:
        old = _stale_cache(path)
        if old is None:
            raise
        try:
            lyr = _load(old, name)
        except Exception:
            raise e
        day = _CACHE_RE.match(os.path.basename(old)).group(2)
        day = "{}-{}-{}".format(day[:4], day[4:6], day[6:])
        log_message("{}: service failed ({}) - using the download of {} ({})".format(
            name, e, day, os.path.basename(old)), MSG_WARNING)
        STALE_USED.append((name, day, os.path.basename(old)))
        return lyr, lyr.featureCount(), old


# -------------------------------------------------- v2.6.1: offline data pack --
# Settings (QGIS user settings, not the project - they belong to the computer):
# the pack (folder, .sqlite or .zip) and "offline only" (never go online).
# Environment variables win (scripts, tests).
SETTING_PACK = "ND_Dam_Hazard_Helper/offline_pack"
SETTING_ONLY = "ND_Dam_Hazard_Helper/offline_only"
ENV_PACK = "ND_DAM_HAZARD_OFFLINE_PACK"
ENV_ONLY = "ND_DAM_HAZARD_OFFLINE_ONLY"
PACK_FOLDER_NAME = "_pack"
# data sets read from the pack this run: [(label, name, fetched, count,
# partial)]. Cleared with STALE_USED at the start of every run.
PACK_USED = []
_PACK_ERRORS = set()             # pack problems already logged (once each)


def _truthy(v):
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def suite_pack():
    """The suite's offline pack: 2.24.0 the pack location chosen in Suite data and servers (or on
    the Data & Output tab), else the pack in the suite data folder (2.17.0); "" with Data source
    = Download."""
    try:
        from .damsuite import suitedata as _SD
        return _SD.active_pack() or ""
    except Exception:
        return ""


def data_source():
    """2.24.0: the suite's data source, "pack" or "download" (shared by the three plugins)."""
    try:
        from .damsuite import suitedata as _SD
        return _SD.data_source()
    except Exception:
        return "pack"


def set_data_source(value):
    from .damsuite import suitedata as _SD
    return _SD.set_data_source(value)


def offline_config(fallback=True):
    """(pack path or "", offline_only). 2.24.0: Data source = Download (the suite's setting) gives
    no pack and never offline only; otherwise the suite's pack location, else this plugin's own
    pack setting of older releases, else (fallback) the pack in the suite data folder.
    The environment variables win (scripts, tests)."""
    path = os.environ.get(ENV_PACK)
    only = os.environ.get(ENV_ONLY)
    from_env = path is not None
    if not from_env:
        try:
            from .damsuite import suitedata as _SD
            path = _SD.pack_location() or None
        except Exception:
            path = None
    if (path is None or only is None) and QgsSettings is not None:
        try:
            st = QgsSettings()
            if path is None:
                path = st.value(SETTING_PACK, "") or ""
            if only is None:
                only = st.value(SETTING_ONLY, False)
        except Exception:
            pass
    path = str(path or "").strip()
    only = _truthy(only) if only is not None else False
    if not from_env and data_source() == "download":
        return "", False
    if not path and fallback:
        path = suite_pack()
    return path, only


def set_offline_config(path, offline_only):
    """Save the offline settings (main thread). 2.24.0: the pack location is the suite's (the
    three plugins share it); this plugin's own older setting is cleared."""
    st = QgsSettings()
    try:
        from .damsuite import suitedata as _SD
        _SD.set_pack_location(path or "")
        st.setValue(SETTING_PACK, "")
    except Exception:
        st.setValue(SETTING_PACK, path or "")
    st.setValue(SETTING_ONLY, bool(offline_only))


def pack_reader(log=None):
    """The chosen offline data pack (offline_pack.PackReader), or None when
    none is chosen or it cannot be read (the problem is logged once)."""
    from .offline_pack import open_pack
    path, _only = offline_config()
    if not path:
        return None
    try:
        return open_pack(path, log=log or log_message)
    except CancelledByUser:
        raise
    except Exception as e:
        msg = "Offline data pack not used: {}".format(e)
        if msg not in _PACK_ERRORS:
            _PACK_ERRORS.add(msg)
            log_message(msg, MSG_WARNING)
        return None


def _cache_key(path):
    m = _CACHE_RE.match(os.path.basename(path))
    return m.group(1) if m else None


def _from_pack(reader, key, extent_4326, path, label, partial):
    """The pack's features of data set `key` in the extent, written as
    GeoJSON beside the downloads (_downloads/_pack) and loaded."""
    feats = reader.query(key, extent_4326)
    info = reader.info(key) or {}
    fetched = info.get("fetched") or reader.created
    folder = os.path.join(os.path.dirname(path), PACK_FOLDER_NAME)
    os.makedirs(folder, exist_ok=True)
    m = _CACHE_RE.match(os.path.basename(path))
    digest = m.group(3) if m else "area"
    # not a dated download name: the stale-cache fallback never mistakes it
    out = os.path.join(folder, "{}_pack-{}_{}.geojson".format(
        key, (fetched or "").replace("-", ""), digest))
    # v2.6.2: written atomically (a concurrent or interrupted write never
    # leaves half a file that a later run would load)
    tmp = "{}.{}.part".format(out, threading.get_ident())
    _write_fc(tmp, feats)
    try:
        os.replace(tmp, out)
    except OSError:                  # Windows: the file is open elsewhere - own name
        alt = "{}_{}.geojson".format(os.path.splitext(out)[0], threading.get_ident())
        os.replace(tmp, alt)
        out = alt
    PACK_USED.append((label, key, fetched, len(feats), partial))
    why = ""
    if partial:
        why = (" - INCOMPLETE for this area (it reaches beyond the pack's area, or into a part "
               "that could not be downloaded when the pack was built): features may be "
               "missing")
    note = info.get("note") or ""
    log_message("{}: {} feature(s) from the offline data pack (fetched {}){}{}".format(
        label, len(feats), fetched, why, " - pack note: " + note if note else ""),
        MSG_WARNING if (partial or note) else MSG_INFO)
    if not feats:
        gt = info.get("geometry") or ""
        kind = "Polygon" if "Polygon" in gt else "LineString" if "Line" in gt else "Point"
        return QgsVectorLayer("{}?crs=EPSG:4326".format(kind), label, "memory"), 0, out
    return _load(out, label), len(feats), out


def _offline_first(path, label, extent_4326, live, pack_fallback=True):
    """live() -> (layer, count, path), unless the offline data pack holds the
    data set: then the pack is used (online allowed and the pack not complete
    for the study area - beyond its area, a gap, or a partial data set: the
    service first, the pack if it fails). Offline only and not in the pack:
    NotInPack (an earlier download of the same area may still be used by the
    stale-cache fallback). pack_fallback=False: a failing service raises
    (another service is tried first - NID mirror); the caller falls back to
    the pack itself (pack_partial)."""
    key = _cache_key(path)
    path_set, only = offline_config()
    reader = pack_reader() if path_set else None
    if reader is not None and key and reader.has(key):
        if reader.covers(extent_4326, key):
            return _from_pack(reader, key, extent_4326, path, label, False)
        if only:
            return _from_pack(reader, key, extent_4326, path, label, True)
        try:
            return live()
        except CancelledByUser:
            raise
        except Exception as e:
            if not pack_fallback:
                raise
            log_message("{}: service failed ({}) - using the offline data pack".format(
                label, e), MSG_WARNING)
            return _from_pack(reader, key, extent_4326, path, label, True)
    if only:
        raise NotInPack("not in the offline data pack (offline mode)")
    return live()


def _write_fc(path, features):
    # Coordinates are longitude / latitude. Without a crs member QGIS loads the
    # file with no CRS, the study-area clip rejects every point, and the
    # hazard class comes back Low from an empty inventory.
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({
            "type": "FeatureCollection",
            "crs": {"type": "name", "properties": {"name": "urn:ogc:def:crs:EPSG::4326"}},
            "features": features,
        }, fh)


def _load(path, name):
    lyr = QgsVectorLayer(path, name, "ogr")
    if not lyr.isValid():
        raise DownloadError("downloaded file could not be opened: {}".format(path))
    if not lyr.crs().isValid():
        lyr.setCrs(QgsCoordinateReferenceSystem("EPSG:4326"))
    return lyr


def tile_extent(extent, size_deg=DOWNLOAD_TILE_DEG):
    """Split (xmin, ymin, xmax, ymax) in degrees into tiles."""
    xmin, ymin, xmax, ymax = extent
    # small tolerance: 0.2 / 0.2 must be 1 tile, not 2 (float rounding)
    nx = max(1, int(math.ceil((xmax - xmin) / size_deg - 1e-9)))
    ny = max(1, int(math.ceil((ymax - ymin) / size_deg - 1e-9)))
    dx, dy = (xmax - xmin) / nx, (ymax - ymin) / ny
    return [(xmin + i * dx, ymin + j * dy, xmin + (i + 1) * dx, ymin + (j + 1) * dy)
            for j in range(ny) for i in range(nx)]


def _check_extent(extent_4326):
    if extent_4326 is None:
        raise DownloadError("no valid longitude / latitude extent (does the depth raster "
                            "have a CRS?)")
    xmin, ymin, xmax, ymax = extent_4326
    if not (-180 <= xmin < xmax <= 180 and -90 <= ymin < ymax <= 90):
        raise DownloadError("extent is not in longitude / latitude: {}".format(extent_4326))


# ------------------------------------------------- v2.8.0: per-tile download cache --
# A persistent cache (one small gzip-JSON file per source, tile and service
# address) in a folder the caller names (cfg "data_cache_folder"). The tiles
# lie on a fixed grid (TILE_CACHE_DEG), so a later run - or a creep-watch pass,
# or another dam next door - finds the tiles of an overlapping study area.
# Only the internet fetch is replaced: the offline data pack keeps its
# priority (_offline_first) and the day files in outputs/_downloads work as
# before. Without a configured folder nothing here is active (today's
# behaviour).
TILE_CACHE_DEG = 0.1                 # cache grid (degrees); NSI / ArcGIS tiles
TILE_CACHE_VERSION = 1
TILE_CACHE_SUFFIX = ".tile.json.gz"
TILE_CACHE_MAX_AGE_DAYS = 30.0       # default freshness of a tile
TILE_CACHE_MAX_MB = 2000.0           # default size limit (purge, oldest first)
_PURGE_EVERY_BYTES = 25 * 1024 * 1024    # written since the last purge
_TMP_MAX_AGE_S = 3600.0              # leftover .tmp files older than this are removed

# tiles used this run (cleared by configure_tile_cache*): dicts with "source"
# (label), "key", "bbox", "status" ("cache" | "fetched" | "stale"), "fetched"
# (epoch), "fetched_date" (YYYY-MM-DD), "age_days", "reason"
TILE_USED = []
_USED_LOCK = threading.Lock()
_ACTIVE_CACHE = [None]
_CACHE_WARNED = set()
_TMP_COUNTER = itertools.count(1)


def default_cache_folder():
    """<QGIS profile folder, else ~/.nd_dam_hazard_helper>/tile_cache."""
    base = ""
    try:
        from qgis.core import QgsApplication
        base = QgsApplication.qgisSettingsDirPath()
    except Exception:
        base = ""
    if not base:
        base = os.path.join(os.path.expanduser("~"), ".nd_dam_hazard_helper")
    return os.path.join(base, "tile_cache")


def cache_tiles(extent, size_deg=TILE_CACHE_DEG):
    """The cache-grid tiles (xmin, ymin, xmax, ymax) that cover the extent
    (degrees): the same tiles for every extent that overlaps them."""
    xmin, ymin, xmax, ymax = extent
    i0, i1 = int(math.floor(xmin / size_deg + 1e-9)), int(math.ceil(xmax / size_deg - 1e-9))
    j0, j1 = int(math.floor(ymin / size_deg + 1e-9)), int(math.ceil(ymax / size_deg - 1e-9))
    i1, j1 = max(i1, i0 + 1), max(j1, j0 + 1)
    return [(round(i * size_deg, 6), round(j * size_deg, 6),
             round((i + 1) * size_deg, 6), round((j + 1) * size_deg, 6))
            for j in range(j0, j1) for i in range(i0, i1)]


def _date_of(epoch):
    try:
        return time.strftime("%Y-%m-%d", time.localtime(epoch))
    except (OverflowError, OSError, ValueError):
        return "?"


def _safe_name(name):
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", str(name))[:60] or "source"


def _geom_bbox(geom):
    """(xmin, ymin, xmax, ymax) of a GeoJSON geometry, None without coordinates."""
    if not isinstance(geom, dict):
        return None
    xs0 = ys0 = float("inf")
    xs1 = ys1 = float("-inf")
    stack = [geom.get("coordinates")]
    for g in geom.get("geometries") or []:
        bb = _geom_bbox(g)
        if bb:
            xs0, ys0, xs1, ys1 = min(xs0, bb[0]), min(ys0, bb[1]), max(xs1, bb[2]), max(ys1, bb[3])
    while stack:
        c = stack.pop()
        if not isinstance(c, (list, tuple)) or not c:
            continue
        if isinstance(c[0], (int, float)):
            if len(c) >= 2:
                x, y = c[0], c[1]
                xs0, ys0, xs1, ys1 = min(xs0, x), min(ys0, y), max(xs1, x), max(ys1, y)
        else:
            stack.extend(c)
    return None if xs0 == float("inf") else (xs0, ys0, xs1, ys1)


def _in_extent(feature, extent):
    """False only when the feature's geometry box is clear of the extent."""
    bb = _geom_bbox(feature.get("geometry")) if isinstance(feature, dict) else None
    if bb is None:
        return True
    return not (bb[2] < extent[0] or bb[0] > extent[2] or bb[3] < extent[1] or bb[1] > extent[3])


def _merge_tile_features(tile_lists, extent):
    """Features of several tiles, the ones in more than one tile once (by the
    service's feature id, else by content), limited to the extent."""
    out, seen = [], set()
    for feats in tile_lists:
        mine = set()
        for f in feats:
            if not isinstance(f, dict) or not _in_extent(f, extent):
                continue
            fid = f.get("id")
            k = ("id", fid) if fid is not None else ("j", json.dumps(f, sort_keys=True))
            if k in seen:
                continue
            mine.add(k)
            out.append(f)
        seen |= mine
    return out


def _warn_once(msg):
    if msg not in _CACHE_WARNED:
        _CACHE_WARNED.add(msg)
        log_message(msg, MSG_WARNING)


class TileCache(object):
    """Persistent per-tile cache (see above). Thread safe; never raises into
    the caller except what fetch() raises when there is nothing to fall back on."""

    def __init__(self, folder, max_age_days=TILE_CACHE_MAX_AGE_DAYS, refresh=False,
                 max_mb=TILE_CACHE_MAX_MB, clock=None, on_tile=None):
        self.folder = os.path.abspath(os.path.expanduser(str(folder)))
        self.max_age_days = _num(max_age_days, TILE_CACHE_MAX_AGE_DAYS)
        self.refresh = bool(refresh)
        self.max_mb = _num(max_mb, TILE_CACHE_MAX_MB)
        self._clock = clock
        self.on_tile = on_tile
        self._locks = {}
        self._locks_guard = threading.Lock()
        self._refreshed = set()          # paths refetched this run (refresh_data)
        self._written = 0
        self._purge_lock = threading.Lock()

    def now(self):
        return float((self._clock or time.time)())

    # ---- names ------------------------------------------------------------
    @staticmethod
    def url_hash(url_key):
        return hashlib.sha1(str(url_key).encode("utf-8")).hexdigest()[:8]

    def path(self, source, url_key, bbox):
        return os.path.join(self.folder, "{}__{:.4f}_{:.4f}_{:.4f}_{:.4f}__{}{}".format(
            _safe_name(source), bbox[0], bbox[1], bbox[2], bbox[3], self.url_hash(url_key),
            TILE_CACHE_SUFFIX))

    def _lock_for(self, path):
        with self._locks_guard:
            lk = self._locks.get(path)
            if lk is None:
                lk = self._locks[path] = threading.Lock()
            return lk

    # ---- disk ---------------------------------------------------------------
    def read(self, source, url_key, bbox):
        """The stored tile record (dict with "features", "fetched", "marker"...)
        or None: missing, unreadable, corrupt or not this tile - never raises."""
        p = self.path(source, url_key, bbox)
        try:
            if not os.path.isfile(p):
                return None
            with gzip.open(p, "rb") as fh:
                rec = json.loads(fh.read().decode("utf-8"))
            ok = (isinstance(rec, dict) and rec.get("v") == TILE_CACHE_VERSION
                  and rec.get("url") == self.url_hash(url_key)
                  and isinstance(rec.get("features"), list)
                  and isinstance(rec.get("fetched"), (int, float))
                  and rec.get("count") == len(rec["features"])
                  and isinstance(rec.get("bbox"), list) and len(rec["bbox"]) == 4
                  and all(abs(a - b) < 1e-6 for a, b in zip(rec["bbox"], bbox)))
            if not ok:
                raise ValueError("unexpected content")
            return rec
        except Exception as e:                          # noqa - corrupt / unreadable
            _warn_once("Tile cache: {} ignored ({}) - fetched again".format(
                os.path.basename(p), e))
            try:
                os.remove(p)
            except OSError:
                pass
            return None

    def write(self, source, url_key, bbox, features, marker="", fetched=None):
        """Store a tile atomically (tmp file + replace). False when it could not
        be written - never raises."""
        p = self.path(source, url_key, bbox)
        tmp = "{}.{}.{}.{}.tmp".format(p, os.getpid(), threading.get_ident(),
                                       next(_TMP_COUNTER))
        try:
            os.makedirs(self.folder, exist_ok=True)
            fetched = self.now() if fetched is None else fetched
            rec = {"v": TILE_CACHE_VERSION, "source": str(source), "url": self.url_hash(url_key),
                   "bbox": [round(float(b), 6) for b in bbox], "fetched": fetched,
                   "fetched_date": _date_of(fetched), "marker": marker or "",
                   "count": len(features), "features": features}
            data = json.dumps(rec, separators=(",", ":")).encode("utf-8")
            with gzip.open(tmp, "wb", compresslevel=5) as fh:
                fh.write(data)
            for attempt in range(4):
                try:
                    os.replace(tmp, p)
                    break
                except PermissionError:                  # Windows: the file is open elsewhere
                    if attempt == 3:
                        raise
                    time.sleep(0.05 * (attempt + 1))
            size = os.path.getsize(p)
        except Exception as e:                          # noqa
            _warn_once("Tile cache: could not write {} ({}) - the download is used "
                       "but not kept".format(os.path.basename(p), e))
            try:
                os.remove(tmp)
            except OSError:
                pass
            return False
        self._written += size
        if self._written >= _PURGE_EVERY_BYTES and self._purge_lock.acquire(False):
            try:
                self._written = 0
                purge_tile_cache(self.folder, self.max_mb)
            finally:
                self._purge_lock.release()
        return True

    # ---- the tile -----------------------------------------------------------
    def _record(self, source, key, bbox, status, fetched, reason=""):
        age = max(0.0, (self.now() - fetched) / 86400.0)
        rec = {"source": source, "key": key, "bbox": tuple(bbox), "status": status,
               "fetched": fetched, "fetched_date": _date_of(fetched),
               "age_days": round(age, 3), "reason": reason}
        with _USED_LOCK:
            TILE_USED.append(rec)
        if self.on_tile is not None:
            try:
                self.on_tile(dict(rec))
            except Exception:
                pass
        return rec

    def get(self, source, url_key, bbox, fetch, marker="", label=None):
        """The features of one tile. fetch() -> features, or (features,
        complete): complete=False (cut short) is used but not cached. Returns
        (features, info) with info["status"] "cache" (read from disk), "fetched"
        (downloaded now) or "stale" (expired copy: the fetch failed);
        info also has "fetched" (epoch), "fetched_date", "age_days", "reason".
        An error from fetch() is never cached; with no earlier copy it is
        raised, CancelledByUser always."""
        label = label or source
        path = self.path(source, url_key, bbox)
        with self._lock_for(path):
            rec = self.read(source, url_key, bbox)
            reason = ""
            if rec is not None:
                fetched = float(rec["fetched"])
                age = (self.now() - fetched) / 86400.0
                old_marker = rec.get("marker") or ""
                if path in self._refreshed:
                    pass                                # already refetched this run
                elif self.refresh:
                    reason = "refresh requested"
                elif age < -1.0 or self.max_age_days <= 0 or age >= self.max_age_days:
                    reason = "older than {:g} day(s)".format(max(0.0, self.max_age_days))
                elif marker and old_marker and marker != old_marker:
                    reason = "the service was edited since"
                if not reason:
                    info = self._record(label, source, bbox, "cache", fetched)
                    return rec["features"], info
            try:
                res = fetch()
            except CancelledByUser:
                raise
            except Exception as e:
                if rec is None:
                    raise
                fetched = float(rec["fetched"])
                log_message("{}: tile {} could not be refreshed ({}) - the STALE copy of "
                            "{} is used".format(label, _bbox_text(bbox), e, _date_of(fetched)),
                            MSG_WARNING)
                info = self._record(label, source, bbox, "stale", fetched, str(e))
                return rec["features"], info
            if isinstance(res, tuple) and len(res) == 2 and isinstance(res[1], bool):
                feats, complete = res
            else:
                feats, complete = res, True
            feats = list(feats or [])
            now = self.now()
            if complete:
                self.write(source, url_key, bbox, feats, marker=marker, fetched=now)
                if self.refresh:
                    self._refreshed.add(path)
            return feats, self._record(label, source, bbox, "fetched", now, reason)

    # ---- housekeeping -------------------------------------------------------
    def purge(self, max_mb=None):
        return purge_tile_cache(self.folder, self.max_mb if max_mb is None else max_mb)

    def clear(self):
        return clear_tile_cache(self.folder)

    def stats(self):
        return tile_cache_stats(self.folder)


def _num(v, default):
    try:
        f = float(v)
        return f if f == f else default
    except (TypeError, ValueError):
        return default


def _bbox_text(bbox):
    return "({:.2f}, {:.2f}, {:.2f}, {:.2f})".format(*bbox)


def _tile_files(folder):
    """[(path, size, mtime)] of the cache's tile files (never raises)."""
    out = []
    try:
        with os.scandir(folder) as it:
            for e in it:
                try:
                    if e.is_file() and e.name.endswith(TILE_CACHE_SUFFIX):
                        st = e.stat()
                        out.append((e.path, st.st_size, st.st_mtime))
                except OSError:
                    continue
    except OSError:
        pass
    return out


def tile_cache_stats(folder):
    """{"files", "bytes", "oldest", "newest"} (epochs or None) of a cache folder."""
    files = _tile_files(folder)
    return {"files": len(files), "bytes": sum(f[1] for f in files),
            "oldest": min((f[2] for f in files), default=None),
            "newest": max((f[2] for f in files), default=None)}


def purge_tile_cache(folder, max_mb=TILE_CACHE_MAX_MB):
    """Keep the cache within max_mb (MB, 1 MB = 1048576 bytes): the oldest tiles
    go first. Old leftover .tmp files are removed too. Returns (files removed,
    bytes freed); never raises."""
    removed = freed = 0
    try:
        limit = max(0.0, _num(max_mb, TILE_CACHE_MAX_MB)) * 1024 * 1024
        now = time.time()
        try:
            with os.scandir(folder) as it:
                for e in it:
                    if e.is_file() and e.name.endswith(".tmp"):
                        try:
                            if now - e.stat().st_mtime > _TMP_MAX_AGE_S:
                                os.remove(e.path)
                        except OSError:
                            pass
        except OSError:
            pass
        files = sorted(_tile_files(folder), key=lambda f: (f[2], f[0]))
        total = sum(f[1] for f in files)
        for path, size, _m in files:
            if total <= limit:
                break
            try:
                os.remove(path)
            except OSError:
                continue
            total -= size
            removed += 1
            freed += size
    except Exception:                                    # noqa
        pass
    return removed, freed


def clear_tile_cache(folder):
    """Delete every tile of the cache (only the cache's own files). Returns
    (files removed, bytes freed); never raises."""
    removed = freed = 0
    for path, size, _m in _tile_files(folder):
        try:
            os.remove(path)
            removed += 1
            freed += size
        except OSError:
            pass
    try:
        with os.scandir(folder) as it:
            for e in it:
                if e.is_file() and e.name.endswith(".tmp"):
                    try:
                        os.remove(e.path)
                    except OSError:
                        pass
    except OSError:
        pass
    return removed, freed


def active_tile_cache():
    """The configured TileCache, or None (cache off)."""
    return _ACTIVE_CACHE[0]


def reset_tile_usage():
    with _USED_LOCK:
        del TILE_USED[:]


def configure_tile_cache(folder=None, max_age_days=TILE_CACHE_MAX_AGE_DAYS, refresh=False,
                         max_mb=TILE_CACHE_MAX_MB, clock=None, on_tile=None, purge=True):
    """Switch the tile cache on (folder) or off (folder falsy) for the coming
    downloads and clear this run's tile-usage list. "auto" / True: the default
    folder. Returns the TileCache or None. Call it at the start of every run."""
    reset_tile_usage()
    _CACHE_WARNED.clear()
    if folder is True or (isinstance(folder, str) and folder.strip().lower() == "auto"):
        folder = default_cache_folder()
    if not folder:
        _ACTIVE_CACHE[0] = None
        return None
    cache = TileCache(folder, max_age_days=max_age_days, refresh=refresh, max_mb=max_mb,
                      clock=clock, on_tile=on_tile)
    _ACTIVE_CACHE[0] = cache
    if purge:
        purge_tile_cache(cache.folder, cache.max_mb)
    return cache


def configure_tile_cache_from_cfg(cfg, **kw):
    """configure_tile_cache from pipeline cfg keys: data_cache_folder (absent /
    empty: cache off; "auto": default_cache_folder()), data_max_age_days
    (default 30), refresh_data (default False), data_cache_max_mb (default
    2000)."""
    cfg = cfg or {}
    return configure_tile_cache(
        cfg.get("data_cache_folder"),
        max_age_days=_num(cfg.get("data_max_age_days"), TILE_CACHE_MAX_AGE_DAYS),
        refresh=_truthy(cfg.get("refresh_data")),
        max_mb=_num(cfg.get("data_cache_max_mb"), TILE_CACHE_MAX_MB), **kw)


# ---- provenance -----------------------------------------------------------------
def tile_vintage(records=None):
    """What the run took from the tile cache, one dict per data source in the
    order first used: {"source", "tiles", "from_cache", "downloaded", "stale",
    "oldest_fetched", "newest_fetched" (YYYY-MM-DD), "oldest_age_days"}. A tile
    used twice counts once (its latest record). records: TILE_USED (default)."""
    with _USED_LOCK:
        recs = list(TILE_USED if records is None else records)
    tiles = {}
    for r in recs:
        tiles[(r["source"], tuple(r["bbox"]))] = r
    rows = {}
    for (src, _bb), r in tiles.items():
        row = rows.setdefault(src, {"source": src, "tiles": 0, "from_cache": 0,
                                    "downloaded": 0, "stale": 0, "_min": None, "_max": None,
                                    "_age": 0.0})
        row["tiles"] += 1
        row[{"cache": "from_cache", "fetched": "downloaded", "stale": "stale"}[r["status"]]] += 1
        f = r["fetched"]
        row["_min"] = f if row["_min"] is None else min(row["_min"], f)
        row["_max"] = f if row["_max"] is None else max(row["_max"], f)
        row["_age"] = max(row["_age"], r.get("age_days") or 0.0)
    out = []
    for row in rows.values():
        lo, hi = row.pop("_min"), row.pop("_max")
        row["oldest_fetched"], row["newest_fetched"] = _date_of(lo), _date_of(hi)
        row["oldest_age_days"] = round(row.pop("_age"), 1)
        out.append(row)
    return out


def vintage_lines(records=None):
    """One text line per source that used the tile cache, e.g. "NSI: 41 tiles
    (30 from the tile cache, 9 downloaded now, 2 STALE - the service did not
    answer), oldest fetched 2026-09-20". Empty when the cache was not used."""
    lines = []
    for r in tile_vintage(records):
        parts = []
        if r["from_cache"]:
            parts.append("{} from the tile cache".format(r["from_cache"]))
        if r["downloaded"]:
            parts.append("{} downloaded now".format(r["downloaded"]))
        if r["stale"]:
            parts.append("{} STALE - the service did not answer".format(r["stale"]))
        lines.append("{}: {} tile{} ({}), oldest fetched {}".format(
            r["source"], r["tiles"], "" if r["tiles"] == 1 else "s", ", ".join(parts),
            r["oldest_fetched"]))
    return lines


def stale_lines(records=None):
    """One warning line per source with stale tiles (expired copies used
    because the service failed), for the run log / data-source list."""
    with _USED_LOCK:
        recs = list(TILE_USED if records is None else records)
    by = {}
    for r in recs:
        if r["status"] == "stale":
            by.setdefault(r["source"], {})[tuple(r["bbox"])] = r["fetched"]
    return ["{}: the service did not answer - {} tile(s) from the tile cache are STALE "
            "(fetched {} to {})".format(src, len(t), _date_of(min(t.values())),
                                        _date_of(max(t.values())))
            for src, t in by.items()]


def tile_cache_report_lines():
    """Run-log lines (">>> ..."): the vintage line per source, then the stale
    warnings."""
    return [">>> {}".format(l) for l in vintage_lines()] + \
           [">>> WARNING: {}".format(l) for l in stale_lines()]


def tile_cache_source_lines():
    """Lines for the run's data-source list: "Tile cache - <vintage line>" per
    source, stale ones flagged "STALE data"."""
    stale = set(r["source"] for r in tile_vintage() if r["stale"])
    return ["Tile cache - {}{}".format(l, " - STALE data" if l.split(":")[0] in stale else "")
            for l in vintage_lines()]


def _refresh_requested():
    """v2.8: 'refresh every tile' is on - today's whole-download file must not answer
    instead of the (refreshing) tile cache."""
    c = _ACTIVE_CACHE[0]
    return c is not None and bool(getattr(c, "refresh", False))


def fetch_nsi(extent_4326, output_folder, progress_cb=None):
    """NSI structures (points, EPSG:4326) intersecting the extent.
    Returns (layer, count, cache_path)."""
    _check_extent(extent_4326)
    folder = download_folder(output_folder)
    path = _cache_path(folder, "NSI_structures", extent_4326)
    if os.path.exists(path) and not _refresh_requested():
        lyr = _load(path, "NSI (download)")
        log_message("NSI: using today's cached download {}".format(os.path.basename(path)))
        return lyr, lyr.featureCount(), path
    return _with_stale_fallback(path, "NSI (download)", lambda: _offline_first(
        path, "NSI (download)", extent_4326,
        lambda: _fetch_nsi_live(extent_4326, path, progress_cb)))


def _nsi_url(tile):
    x0, y0, x1, y1 = tile
    ring = [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]
    bbox = ",".join("{:.6f},{:.6f}".format(x, y) for x, y in ring)
    return NSI_API_URL + "?" + urlencode({"bbox": bbox, "fmt": "fc"})


def _nsi_tile(tile):
    data = http_get_json(_nsi_url(tile))
    return data.get("features", []) if isinstance(data, dict) else []


def _nsi_tile_cached(cache, tile):
    """One NSI tile through the tile cache. Stricter than _nsi_tile: an answer
    that is not a feature collection is an error (and so never cached); a
    collection with no features is a real, cached, empty tile."""
    def fetch():
        data = http_get_json(_nsi_url(tile))
        feats = data.get("features") if isinstance(data, dict) else None
        if not isinstance(feats, list):
            raise DownloadError("NSI: unexpected answer for tile {}".format(_bbox_text(tile)))
        return feats, True
    return cache.get("NSI_structures", NSI_API_URL, tile, fetch, label="NSI")[0]


def _fetch_nsi_live(extent_4326, path, progress_cb):
    tiles = tile_extent(extent_4326)
    if len(tiles) > NSI_MAX_TILES:
        raise DownloadError("study area too large for the NSI API ({} tiles of {} deg; "
                            "limit {})".format(len(tiles), DOWNLOAD_TILE_DEG, NSI_MAX_TILES))
    cache = active_tile_cache()
    if cache is not None:
        # v2.8.0: fixed-grid tiles, each read from / written to the tile cache
        tiles = cache_tiles(extent_4326)
        got = parallel_map([(i, _nsi_tile_cached, (cache, t), {}) for i, t in enumerate(tiles)],
                           progress_cb=progress_cb, label="NSI structures, {} tile(s)".format(
                               len(tiles)))
    else:
        # v2.6: the tiles side by side
        got = parallel_map([(i, _nsi_tile, (t,), {}) for i, t in enumerate(tiles)],
                           progress_cb=progress_cb, label="NSI structures, {} tile(s)".format(
                               len(tiles)))
    features = {}
    for i in range(len(tiles)):
        ok, val = got[i]
        if not ok:
            raise val
        for f in val:
            if cache is not None and not _in_extent(f, extent_4326):
                continue                       # the grid tile reaches beyond the study area
            props = f.get("properties") or {}
            key = props.get("fd_id")
            if key is None:
                key = json.dumps(f.get("geometry"), sort_keys=True)
            features[key] = f
    _write_fc(path, list(features.values()))
    log_message("NSI: {} structures downloaded for the inundated extent".format(len(features)))
    if not features:
        return QgsVectorLayer("Point?crs=EPSG:4326", "NSI (download)", "memory"), 0, path
    return _load(path, "NSI (download)"), len(features), path


def _layer_meta(url):
    """(object-id field, supportsPagination, page size, version marker) of an
    ArcGIS layer from its ?f=json description. The page size is the service's
    maxRecordCount, at most ARCGIS_PAGE_SIZE; the marker is the layer's last
    edit date when it says one ("" otherwise; used by the tile cache)."""
    try:
        data = http_get_json(url.rstrip("/") + "?f=json")
    except CancelledByUser:
        raise
    except Exception:
        return None, False, ARCGIS_PAGE_SIZE, ""
    if not isinstance(data, dict):
        return None, False, ARCGIS_PAGE_SIZE, ""
    adv = data.get("advancedQueryCapabilities") or {}
    paged = bool(adv.get("supportsPagination")) if isinstance(adv, dict) else False
    size = ARCGIS_PAGE_SIZE
    try:
        mrc = int(data.get("maxRecordCount") or 0)
        if 0 < mrc < size:
            size = mrc
    except (TypeError, ValueError):
        pass
    marker = ""
    ei = data.get("editingInfo")
    if isinstance(ei, dict) and ei.get("lastEditDate") not in (None, ""):
        marker = "edit:{}".format(ei.get("lastEditDate"))
    return data.get("objectIdField") or None, paged, size, marker


def _layer_paging(url):
    """(object-id field, supportsPagination, page size) for deterministic
    paging; the page size is the service's maxRecordCount, at most
    ARCGIS_PAGE_SIZE."""
    return _layer_meta(url)[:3]


def fetch_arcgis_layer(url, extent_4326, output_folder, name, where="1=1",
                       progress_cb=None, allow_stale=True, pack_fallback=True):
    """Features of an ArcGIS FeatureServer layer intersecting the extent,
    paged, as GeoJSON in EPSG:4326. Returns (layer, count, cache_path).
    allow_stale: when the service fails, use an earlier day's download.
    pack_fallback: when the service fails, use an incomplete offline pack."""
    _check_extent(extent_4326)
    folder = download_folder(output_folder)
    path = _cache_path(folder, name, extent_4326)
    if os.path.exists(path) and not _refresh_requested():
        lyr = _load(path, name)
        log_message("{}: using today's cached download {}".format(name, os.path.basename(path)))
        return lyr, lyr.featureCount(), path
    live = lambda: _offline_first(path, name, extent_4326, lambda: _fetch_arcgis_live(  # noqa
        url, extent_4326, path, name, where, progress_cb), pack_fallback=pack_fallback)
    return _with_stale_fallback(path, name, live) if allow_stale else live()


def _arcgis_count(url, extent_4326, where):
    """Number of features the query would return, or None when the service
    does not say."""
    xmin, ymin, xmax, ymax = extent_4326
    params = {"where": where, "f": "json", "returnCountOnly": "true",
              "geometry": "{},{},{},{}".format(xmin, ymin, xmax, ymax),
              "geometryType": "esriGeometryEnvelope", "inSR": "4326",
              "spatialRel": "esriSpatialRelIntersects"}
    try:
        data = http_get_json(url.rstrip("/") + "/query?" + urlencode(params))
    except Exception:
        return None
    n = data.get("count") if isinstance(data, dict) else None
    return int(n) if isinstance(n, (int, float)) and n >= 0 else None


def _arcgis_page(url, extent_4326, where, oid_field, offset, size=ARCGIS_PAGE_SIZE):
    xmin, ymin, xmax, ymax = extent_4326
    params = {
        "where": where, "outFields": "*", "f": "geojson",
        "geometry": "{},{},{},{}".format(xmin, ymin, xmax, ymax),
        "geometryType": "esriGeometryEnvelope", "inSR": "4326", "outSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "resultOffset": offset, "resultRecordCount": size,
    }
    if oid_field:
        params["orderByFields"] = "{} ASC".format(oid_field)
    data = http_get_json(url.rstrip("/") + "/query?" + urlencode(params))
    page_feats = data.get("features", []) if isinstance(data, dict) else []
    exceeded = bool(data.get("exceededTransferLimit")
                    or (data.get("properties") or {}).get("exceededTransferLimit")) \
        if isinstance(data, dict) else False
    return page_feats, exceeded


def _arcgis_collect(url, extent_4326, name, where, progress_cb, paging=None):
    """All features of the layer in the extent, paged. Returns (features,
    complete): complete is False when the answer is cut short (page limit,
    a service that does not page) - such a result is used but never cached
    by the tile cache. paging: (object-id field, paged, size) when already
    known."""
    # v2.3.1: stable sort order makes resultOffset paging deterministic
    oid_field, paged, size = paging if paging is not None else _layer_paging(url)
    complete = True
    features = []
    seen_ids = set()

    def take(page_feats):
        for f in page_feats:
            # paging without orderBy may repeat a feature across pages
            fid = f.get("id") if isinstance(f, dict) else None
            if fid is not None:
                if fid in seen_ids:
                    continue
                seen_ids.add(fid)
            features.append(f)
    # v2.6: a service that pages says how many features there are - the pages
    # are then fetched side by side (in offset order when merged)
    total = _arcgis_count(url, extent_4326, where) if paged and oid_field else None
    if total is not None and total > size and PARALLEL:
        n_pages = min(ARCGIS_MAX_PAGES, int(math.ceil(total / float(size))))
        jobs = [(k, _arcgis_page, (url, extent_4326, where, oid_field, k * size, size), {})
                for k in range(n_pages)]
        got = parallel_map(jobs, progress_cb=progress_cb,
                           label="{}: {} features in {} pages".format(name, total, n_pages))
        pages = []
        for k in range(n_pages):
            ok, val = got[k]
            if not ok:
                raise val
            pages.append(val[0])
        # every page but the last must be full, or the service pages in
        # another way: then the pages are read one after another as before
        if all(len(pg) == size for pg in pages[:-1]) and pages and pages[-1]:
            for pg in pages:
                take(pg)
            if n_pages == ARCGIS_MAX_PAGES and total > n_pages * size:
                log_message("{}: stopped after {} pages".format(name, ARCGIS_MAX_PAGES),
                            MSG_WARNING)
                complete = False
            return features, complete
        log_message("{}: the service returned short pages - read one after another"
                    .format(name))
        features, seen_ids = [], set()
    offset = 0
    for page in range(ARCGIS_MAX_PAGES):
        if progress_cb:
            progress_cb(min(95.0, page * 5.0), "Downloading {}: {} features".format(
                name, len(features)))
        page_feats, exceeded = _arcgis_page(url, extent_4326, where,
                                            oid_field if paged else None, offset, size)
        n_before = len(features)
        take(page_feats)
        if not page_feats or (len(page_feats) < size and not exceeded):
            break
        if page > 0 and len(features) == n_before:
            # a layer without paging support returns the same page again
            log_message("{}: the service repeats the same features (no paging) - {} kept; "
                        "a smaller study area gets all of them".format(name, len(features)),
                        MSG_WARNING)
            complete = False
            break
        offset += len(page_feats)
    else:
        log_message("{}: stopped after {} pages".format(name, ARCGIS_MAX_PAGES), MSG_WARNING)
        complete = False
    return features, complete


def _arcgis_features_tiled(cache, url, extent_4326, name, where, progress_cb):
    """v2.8.0: the layer's features in the extent, fetched per cache-grid tile
    (each read from / written to the tile cache), merged and limited to the
    extent. One failed tile without an earlier copy fails the whole fetch as
    before (the tiles that did arrive stay cached)."""
    oid_field, paged, size, marker = _layer_meta(url)
    tiles = cache_tiles(extent_4326)
    key = "{}|{}".format(url.rstrip("/"), where)

    def one(tile, progress_cb=None):
        return cache.get(name, key, tile, lambda: _arcgis_collect(
            url, tile, name, where, progress_cb, (oid_field, paged, size)),
            marker=marker, label=name)[0]
    got = parallel_map([(i, one, (t,), {}) for i, t in enumerate(tiles)],
                       progress_cb=progress_cb,
                       label="{}: {} tile(s)".format(name, len(tiles)))
    lists = []
    for i in range(len(tiles)):
        ok, val = got[i]
        if not ok:
            raise val
        lists.append(val)
    return _merge_tile_features(lists, extent_4326)


def _hydro3dhp():
    from .damsuite import hydro3dhp
    return hydro3dhp


def _fetch_arcgis_live(url, extent_4326, path, name, where, progress_cb):
    cache = active_tile_cache()
    if cache is not None:
        # v2.8.0: fixed-grid tiles, each read from / written to the tile cache
        features = _arcgis_features_tiled(cache, url, extent_4326, name, where, progress_cb)
    else:
        features = _arcgis_collect(url, extent_4326, name, where, progress_cb)[0]
    # 1.19.0: 3DHP flowlines / waterbodies also get the NHD properties (gnis_name, ftype ...)
    norm = _hydro3dhp().normaliser_for_url(url)
    if norm is not None:
        features = norm(features)
    _write_fc(path, features)
    log_message("{}: {} features downloaded".format(name, len(features)))
    if not features:
        # nothing in the area is a valid answer ("no railroads here")
        return QgsVectorLayer("Point?crs=EPSG:4326", name, "memory"), 0, path
    return _load(path, name), len(features), path


def fetch_nd_address_points(extent_4326, output_folder, progress_cb=None):
    """ND GIS Hub "NDGISHUB SiteStructure Address Points" (NENA site /
    structure address points) intersecting the extent, as GeoJSON in
    EPSG:4326. Returns (layer, count, cache_path)."""
    return fetch_arcgis_layer(ND_ADDRESS_POINTS_URL, extent_4326, output_folder,
                              "NDGISHUB_address_points", progress_cb=progress_cb)


def fetch_usa_structures(extent_4326, output_folder, progress_cb=None):
    """FEMA USA Structures building outlines (polygons; OCC_CLS, PRIM_OCC,
    PROP_ADDR, HEIGHT, SQFEET ...) intersecting the extent, EPSG:4326."""
    return fetch_arcgis_layer(USA_STRUCTURES_URL, extent_4326, output_folder,
                              "USA_Structures", progress_cb=progress_cb)


def fetch_dams(extent_4326, output_folder, progress_cb=None):
    """USACE National Inventory of Dams points intersecting the extent
    (EPSG:4326). The USACE service first, the Esri mirror (same schema) if
    it fails. Returns (layer, count, cache_path, url)."""
    errors = []
    missing = []
    for url in (NID_URL, NID_MIRROR_URL):
        try:
            # v2.6.2: an incomplete pack is used only after BOTH services failed
            lyr, n, path = fetch_arcgis_layer(url, extent_4326, output_folder, "NID_dams",
                                              progress_cb=progress_cb, allow_stale=False,
                                              pack_fallback=False)
            if path and os.path.basename(os.path.dirname(path)) == PACK_FOLDER_NAME:
                return lyr, n, path, "offline data pack"
            return lyr, n, path, url
        except CancelledByUser:
            raise
        except NotInPack as e:
            missing.append(e)
            break                                 # the mirror would not be in it either
        except Exception as e:
            errors.append("{}: {}".format(url.split("/rest/")[0], e))
    # both services failed: the offline pack (even incomplete for this area),
    # else an earlier day's download of the same area
    path = _cache_path(download_folder(output_folder), "NID_dams", extent_4326)
    reader = pack_reader() if offline_config()[0] else None
    if reader is not None and reader.has("NID_dams"):
        log_message("NID_dams: both services failed ({}) - using the offline data pack"
                    .format("; ".join(errors)), MSG_WARNING)
        lyr, n, ppath = _from_pack(reader, "NID_dams", extent_4326, path, "NID_dams",
                                   not reader.covers(extent_4326, "NID_dams"))
        return lyr, n, ppath, "offline data pack"

    def _fail():
        if missing and not errors:
            raise missing[0]
        raise DownloadError("; ".join(errors + [str(e) for e in missing]))
    lyr, n, path = _with_stale_fallback(path, "NID_dams", _fail)
    return lyr, n, path, "earlier download"




def fetch_osm_lifelines(extent_4326, output_folder, progress_cb=None):
    """OpenStreetMap lifeline facilities (OSM_LIFELINE_TAGS) from the Overpass
    API, one request for the extent. Returns (layer, count, cache_path)."""
    _check_extent(extent_4326)
    folder = download_folder(output_folder)
    path = _cache_path(folder, "OSM_lifelines", extent_4326)
    if os.path.exists(path):
        lyr = _load(path, "OSM lifelines")
        log_message("OSM lifelines: using today's cached download {}".format(
            os.path.basename(path)))
        return lyr, lyr.featureCount(), path
    return _with_stale_fallback(path, "OSM lifelines", lambda: _offline_first(
        path, "OSM lifelines", extent_4326, lambda: _fetch_osm_lifelines_live(extent_4326,
                                                                               path)))


def _fetch_osm_lifelines_live(extent_4326, path):
    # Overpass answers 200 with partial results on timeout / out of memory:
    # _overpass_get raises then, so nothing incomplete is cached
    data = _overpass_get(overpass_query(extent_4326))
    feats = overpass_to_features(data)
    _write_fc(path, feats)
    log_message("OSM lifelines: {} facilities downloaded".format(len(feats)))
    if not feats:
        return QgsVectorLayer("Point?crs=EPSG:4326", "OSM lifelines", "memory"), 0, path
    return _load(path, "OSM lifelines"), len(feats), path


def _overpass_get(query):
    url = OVERPASS_URL + "?" + urlencode({"data": query})
    with _OVERPASS_SLOTS:                    # the public Overpass API allows few at once
        data = http_get_json(url, user_agent=HTTP_USER_AGENT)
    remark = data.get("remark") if isinstance(data, dict) else None
    # v2.3.1: Overpass returns 200 with partial results on timeout/out of memory
    if remark and any(s in str(remark).lower() for s in
                      ("error", "timed out", "timeout", "out of memory", "aborted")):
        raise DownloadError("Overpass: {}".format(remark))
    return data




def fetch_osm_crossings(extent_4326, output_folder, progress_cb=None):
    """OpenStreetMap culverts, fords / low-water crossings and railroad
    bridges (OSM_CROSSING_QUERIES) as points. Returns (layer, count, path)."""
    _check_extent(extent_4326)
    folder = download_folder(output_folder)
    path = _cache_path(folder, "OSM_crossings", extent_4326)
    if os.path.exists(path):
        lyr = _load(path, "OSM crossings")
        return lyr, lyr.featureCount(), path
    return _with_stale_fallback(path, "OSM crossings", lambda: _offline_first(
        path, "OSM crossings", extent_4326, lambda: _fetch_osm_crossings_live(extent_4326,
                                                                               path)))


def _fetch_osm_crossings_live(extent_4326, path):
    feats = crossing_features(_overpass_get(crossings_query(extent_4326)))
    _write_fc(path, feats)
    log_message("OSM crossings: {} downloaded".format(len(feats)))
    if not feats:
        return QgsVectorLayer("Point?crs=EPSG:4326", "OSM crossings", "memory"), 0, path
    return _load(path, "OSM crossings"), len(feats), path


# ------------------------------------------------------ v2.5: OSM camps, places --


def fetch_osm_camps(extent_4326, output_folder, progress_cb=None):
    """OpenStreetMap campgrounds and RV parks (overnight recreation, ND
    5.2.1.1) as polygons. Returns (layer, count, cache_path)."""
    _check_extent(extent_4326)
    folder = download_folder(output_folder)
    path = _cache_path(folder, "OSM_campgrounds", extent_4326)
    if os.path.exists(path):
        lyr = _load(path, "OSM campgrounds")
        return lyr, lyr.featureCount(), path

    def live():
        feats = camp_features(_overpass_get(camps_query(extent_4326)))
        _write_fc(path, feats)
        log_message("OSM campgrounds / RV parks: {} downloaded".format(len(feats)))
        if not feats:
            return QgsVectorLayer("Polygon?crs=EPSG:4326", "OSM campgrounds", "memory"), 0, path
        return _load(path, "OSM campgrounds"), len(feats), path
    return _with_stale_fallback(path, "OSM campgrounds", lambda: _offline_first(
        path, "OSM campgrounds", extent_4326, live))




def fetch_osm_trailer_parks(extent_4326, output_folder, progress_cb=None):
    """v2.9: OpenStreetMap mobile-home / trailer parks (areas) - evidence for the
    manufactured-home typing of the structures inside them. Returns
    (layer, count, cache_path)."""
    _check_extent(extent_4326)
    folder = download_folder(output_folder)
    path = _cache_path(folder, "OSM_trailer_parks", extent_4326)
    if os.path.exists(path):
        lyr = _load(path, "OSM mobile-home parks")
        return lyr, lyr.featureCount(), path

    def live():
        feats = trailer_park_features(_overpass_get(trailer_parks_query(extent_4326)))
        _write_fc(path, feats)
        log_message("OSM mobile-home parks: {} downloaded".format(len(feats)))
        if not feats:
            return QgsVectorLayer("Polygon?crs=EPSG:4326", "OSM mobile-home parks",
                                  "memory"), 0, path
        return _load(path, "OSM mobile-home parks"), len(feats), path
    return _with_stale_fallback(path, "OSM mobile-home parks", lambda: _offline_first(
        path, "OSM mobile-home parks", extent_4326, live))


def fetch_osm_places(extent_4326, output_folder, progress_cb=None):
    """OpenStreetMap cities, towns, villages and hamlets (points) for the
    EAP travel-time table. Returns (layer, count, cache_path)."""
    _check_extent(extent_4326)
    folder = download_folder(output_folder)
    path = _cache_path(folder, "OSM_places", extent_4326)
    if os.path.exists(path):
        lyr = _load(path, "OSM places")
        return lyr, lyr.featureCount(), path
    q = places_query(extent_4326)

    def live():
        feats = place_features(_overpass_get(q))
        _write_fc(path, feats)
        log_message("OSM places: {} downloaded".format(len(feats)))
        if not feats:
            return QgsVectorLayer("Point?crs=EPSG:4326", "OSM places", "memory"), 0, path
        return _load(path, "OSM places"), len(feats), path
    return _with_stale_fallback(path, "OSM places", lambda: _offline_first(
        path, "OSM places", extent_4326, live))


def fetch_nbi(extent_4326, output_folder, progress_cb=None):
    """National Bridge Inventory bridges and culverts in the extent."""
    return fetch_arcgis_layer(NBI_URL, extent_4326, output_folder, "NBI_bridges",
                              progress_cb=progress_cb)


def _accepts(fn, name):
    """True when fn takes a `name` keyword (or **kwargs)."""
    import inspect
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return True
    return name in params or any(p.kind == p.VAR_KEYWORD for p in params.values())


def try_download(label, fn, *args, **kwargs):
    """Run a download; log and return (None, 0, None) on failure so the
    classification can continue with local data. A progress_cb the function
    does not take is dropped, not an error."""
    if "progress_cb" in kwargs and not _accepts(fn, "progress_cb"):
        kwargs = {k: v for k, v in kwargs.items() if k != "progress_cb"}
    try:
        return fn(*args, **kwargs)
    except CancelledByUser:
        raise
    except NotInPack as e:
        log_message("{}: skipped - {}".format(label, e))
        return None, 0, None
    except Exception as e:
        log_message("{} download failed: {}".format(label, e), MSG_WARNING)
        return None, 0, None
