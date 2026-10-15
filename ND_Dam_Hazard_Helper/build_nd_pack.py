# -*- coding: utf-8 -*-
"""Build the offline data pack for North Dakota (v2.6.1).

Run on any computer with internet access - QGIS is NOT needed, any Python
3.7+ works (QGIS's own Python too):

    python build_nd_pack.py                      -> ND_Dam_Suite_Pack/ here (2.24.0)
    python build_nd_pack.py --out D:/ND_Dam_Suite_Pack
    python build_nd_pack.py --out D:/nd_pack.zip (the one-file pack of older releases)
    python build_nd_pack.py --only nsi,usa --refresh    (update two data sets)
    python build_nd_pack.py --list               (what it downloads)

1.19.0 (builder 1.3): the whole pack from one run. Flowlines and waterbodies come from the USGS
3D Hydrography Program (3DHP_flowlines, 3DHP_waterbodies; they replace NHD_*), the aquatic
barrier layers from the SARP services behind tool.aquaticbarriers.org (SARP_dams,
SARP_surveyed_crossings), the DWR and HIFLD layers from the files in the suite data folder's
dwr/ and hifld/ (or --files; DWR_dams from the DWR service when there is no file) and the NLCD
2021 land cover from the MRLC web coverage service (needs GDAL: inside QGIS).
3DHP: the web service is read in 0.25 degree tiles (a state-wide request times out); when the National
Map's 3DHP national file (zip / gdb / gpkg) is in the suite data folder's 3dhp/ (or --files) the
flowlines and waterbodies are cut out of it instead.

2.24.0: the pack is a folder with one GeoPackage per layer (damsuite.gpkgpack: manifest.json,
LAYERS.txt with each layer and its source, <theme>/<name>.gpkg). The downloads still go into
the working file first (<folder>_work/, resume as below); then every data set fetched in this
run (or missing from the folder) is written to its GeoPackage. Layers the folder holds that
this builder does not fetch (the DWR files, HIFLD layers, land cover added to the pack) are
kept. An --out ending in .zip writes the older one-file pack instead.

It fetches every data set the plugin downloads - for all of North Dakota
(its bounding box plus a 0.1 degree margin, so the plugin's 2-3 mile
searches still work at the state line) - from the same services, with the
same addresses (your data-source file is used when --sources names it),
and writes one SQLite file plus manifest.json into a zip. Copy the zip to
the offline computer and choose it on the plugin's Data & Output tab
(7. Offline data pack).

An interrupted build is resumed: the data sets already finished are kept
in <name>_work/ next to the zip (delete that folder to start over). Since
2.8.0 a data set that was in progress is continued too: every finished chunk
(a batch of object ids, an NSI / Overpass tile, a page) is stored at once, and
a rerun after Cancel, a crash or a failed data set fetches only what is
missing (--refresh fetches everything again).

Slow or overloaded services: the requests of a data set share one limiter.
When the service answers HTTP 429 / 503 / 504 (or times out) the number of
requests at once is halved (down to 1), every worker waits together (a
Retry-After header is honoured), and the number slowly recovers after
successes. Each wait is written to the log, and the progress message keeps
changing while waiting ("waiting on the service (HTTP 429), 3 of 6 workers
paused").

The plugin can also run this for you: Data & Output tab > Build offline
pack... (on a computer that is online).
"""

import argparse
import contextvars
import email.utils
import json
import math
import os
import random
import socket
import sys
import threading
import time
import zipfile
import zlib
from concurrent.futures import ThreadPoolExecutor, wait as _cf_wait, FIRST_COMPLETED
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request

HERE = os.path.dirname(os.path.abspath(__file__))
if __package__:                      # imported by the plugin (QGIS)
    from . import constants as C
    from . import osm_parse as O
    from . import packfiles as PF
    from .damsuite import hydro3dhp as H3
    from .damsuite.netproxy import urlopen      # QGIS's proxy setting applies (1.16.2)
    from .offline_pack import (PackWriter, DB_NAME, MANIFEST_NAME, ND_BBOX,
                               DEFAULT_PAD_DEG, PackError, STATUS_PARTIAL,
                               write_clean_copy)
else:                                # run as a script: the plugin folder is beside it
    if HERE not in sys.path:
        sys.path.insert(0, HERE)
    import constants as C            # noqa: E402  (no QGIS imports in these three)
    import osm_parse as O            # noqa: E402
    import packfiles as PF           # noqa: E402
    from damsuite import hydro3dhp as H3   # noqa: E402
    from damsuite.netproxy import urlopen  # noqa: E402
    from damsuite.offline_pack import (PackWriter, DB_NAME, MANIFEST_NAME,  # noqa: E402
                                       ND_BBOX, DEFAULT_PAD_DEG, PackError,
                                       STATUS_PARTIAL, write_clean_copy)

BUILDER_VERSION = "1.3"
NLCD_NAME = "NLCD_2021_landcover"
TIMEOUT_S = 180
RETRIES = 5
NSI_TILE_DEG = C.DOWNLOAD_TILE_DEG           # the plugin's tile size (the API is happy)
OVERPASS_TILE_DEG = 2.0
OVERPASS_TIMEOUT_S = 300
IDS_CHUNK = 1000                             # features per ArcGIS request (or less)
# failed requests in a row (no success between, all workers): the service is
# down. A single bad feature fails about one request per halving level with a
# success between, so concurrent descents never come near it.
OUTAGE_FAILS = 24
MAX_MISSING_SHARE = 0.01                     # more ArcGIS features missing: data set fails
# v2.8.0: throttling / visible retries
THROTTLE_CODES = (429, 503, 504)             # "slow down" answers of the services
RETRY_AFTER_MAX_S = 120                      # longest Retry-After that is obeyed
RECOVER_AFTER = 8                            # successes in a row that give back one request
HEARTBEAT_S = 5.0                            # progress is reported at least this often
_SLICE_S = 0.25                              # how often a waiting thread looks at Cancel

# v2.6.2: set when the build is cancelled - every worker stops at its next
# request or back-off instead of running on (inside QGIS: until QGIS exits)
_CANCEL = threading.Event()


class BuildError(Exception):
    pass


class BuildCancelled(BuildError):
    pass


class _Health(object):
    """Failed requests in a row for one data set; too many = the service is
    down, and halving chunks would only multiply the failing requests."""

    def __init__(self, limit=OUTAGE_FAILS):
        self.lock = threading.Lock()
        self.fails = 0
        self.limit = limit

    def ok(self):
        with self.lock:
            self.fails = 0

    def failed(self):
        with self.lock:
            self.fails += 1
            return self.fails >= self.limit

    def down(self):
        with self.lock:
            return self.fails >= self.limit


def _check_cancel():
    if _CANCEL.is_set():
        raise BuildCancelled("cancelled")


_NET_ERRORS = (HTTPError, URLError, TimeoutError, OSError, ValueError, BuildError)


# ------------------------------------------------------ throttle / reporting --
class ServiceBusy(BuildError):
    """An HTTP 200 answer whose body is an ArcGIS error saying "slow down"."""

    def __init__(self, code, msg):
        BuildError.__init__(self, msg)
        self.code = code


def _backoff_s(kind, attempt):
    """Seconds to wait before try attempt + 2. kind: "busy" (429 / 503 / 504),
    "timeout", "error" (anything else)."""
    if kind == "busy":
        return 30 * (attempt + 1)
    if kind == "timeout":
        return 10 * (attempt + 1)
    return 4 * (attempt + 1)


def _retry_after_s(err):
    """Seconds asked for by a Retry-After header (number or HTTP date), capped
    at RETRY_AFTER_MAX_S; None when there is none / it cannot be read."""
    hdrs = getattr(err, "headers", None)
    if hdrs is None:
        hdrs = getattr(err, "hdrs", None)
    try:
        val = hdrs.get("Retry-After") if hdrs is not None else None
    except Exception:
        val = None
    if val is None:
        return None
    val = str(val).strip()
    try:
        sec = float(val)
    except ValueError:
        try:
            dt = email.utils.parsedate_to_datetime(val)
            if dt.tzinfo is None:
                import datetime
                dt = dt.replace(tzinfo=datetime.timezone.utc)
            sec = dt.timestamp() - time.time()
        except (TypeError, ValueError, IndexError, OverflowError):
            return None
    if sec != sec:                                   # NaN
        return None
    return min(max(sec, 1.0), float(RETRY_AFTER_MAX_S))


def _is_timeout(e):
    if isinstance(e, (TimeoutError, socket.timeout)):
        return True
    reason = getattr(e, "reason", None)
    if isinstance(reason, (TimeoutError, socket.timeout)):
        return True
    return "timed out" in str(reason if reason is not None else e).lower()


def _short(e):
    txt = str(getattr(e, "reason", None) or e)
    txt = " ".join(txt.split())
    return txt if len(txt) <= 80 else txt[:77] + "..."


class _Slot(object):
    __slots__ = ("epoch",)

    def __init__(self, epoch):
        self.epoch = epoch


class NetSession(object):
    """The requests of ONE data set: a shared limiter and the reporter of
    their waits (v2.8.0).

    * acquire() / release(): at most `limit` requests run at once. When the
      service pushes back (HTTP 429 / 503 / 504, a timeout) the limit is
      halved (not below 1) - once per "event", however many requests of the
      same round fail - and every worker waits for the SAME pause (the back-off,
      or the Retry-After the service asked for) instead of each backing off on
      its own. RECOVER_AFTER successes in a row give one request back.
    * Every wait is written to `log` once per event; while waiting, beat() (the
      thread that drives the progress bar calls it) reports what is going on
      through `on_beat(text)` at least every HEARTBEAT_S.
    Everything is thread safe. http_json finds the session of the running data
    set in _NET (a context variable set by the builder; a no-op when unset)."""

    def __init__(self, label, workers, log=None, on_beat=None, clock=None):
        self.label = label
        self.max = max(1, int(workers))
        self.limit = self.max
        self.active = 0
        self.blocked = 0                     # threads waiting to run a request
        self.epoch = 0
        self.streak = 0
        self.paused_until = 0.0
        self.reason = ""
        self.pushbacks = 0                   # events so far (for the summary)
        self.cond = threading.Condition()
        self._log = log
        self._log_lock = threading.Lock()
        self._on_beat = on_beat
        self._clock = clock or time.monotonic
        self._owner = threading.get_ident()
        self._last_beat = self._clock()

    # -- reporting ---------------------------------------------------------
    def emit(self, text):
        if self._log is None:
            return
        with self._log_lock:
            try:
                self._log("  {}: {}".format(self.label, text))
            except Exception:
                pass

    def status_text(self):
        now = self._clock()
        with self.cond:
            paused = now < self.paused_until
            if paused or self.blocked:
                return "waiting on the service ({}), {} of {} workers paused".format(
                    self.reason or "slow answers", max(1, self.blocked), self.max)
            if self.limit < self.max:
                return "service is busy - {} of {} requests at a time".format(
                    self.limit, self.max)
            return "waiting for the service to answer ({} request(s) running)".format(
                self.active)

    def touch(self):
        self._last_beat = self._clock()

    def beat(self, main=False):
        """Report the state through on_beat when nothing was reported for
        HEARTBEAT_S. Only the thread that owns the session (the one that
        drives the progress bar) reports, unless main=True. An exception from
        the callback (a cancel) propagates."""
        if self._on_beat is None:
            return
        if not main and threading.get_ident() != self._owner:
            return
        now = self._clock()
        if now - self._last_beat < HEARTBEAT_S:
            return
        self._last_beat = now
        self._on_beat(self.status_text())

    def set_workers(self, n):
        with self.cond:
            n = max(1, int(n))
            self.limit = min(self.limit, n) if self.limit < self.max else n
            self.max = n
            self.cond.notify_all()

    # -- the limiter -------------------------------------------------------
    def acquire(self):
        """Wait until the pause is over and fewer than `limit` requests run;
        cancel-aware. Returns the slot to hand to release()."""
        counted = False
        try:
            while True:
                with self.cond:
                    if self._clock() >= self.paused_until and self.active < self.limit:
                        self.active += 1
                        return _Slot(self.epoch)
                    if not counted:
                        counted = True
                        self.blocked += 1
                    delay = self.paused_until - self._clock()
                    self.cond.wait(_SLICE_S if delay <= 0 else min(_SLICE_S, delay))
                _check_cancel()
                self.beat()
        finally:
            if counted:
                with self.cond:
                    self.blocked -= 1

    def release(self, slot, ok=False, push=None):
        """A request ended. ok: it succeeded. push: the service pushed back -
        {"reason", "wait" (s), "retry_after" (s or None), "try" (the next try
        number, None when there is no retry), "of"}."""
        lines = []
        with self.cond:
            self.active = max(0, self.active - 1)
            if push is not None:
                self.pushbacks += 1
                first = slot is None or slot.epoch == self.epoch
                if first:
                    self.epoch += 1
                    self.streak = 0
                    new = max(1, self.limit // 2)
                    if new < self.limit:
                        self.limit = new
                        lines.append("service is busy - now {} request(s) at a time".format(new))
                ra = push.get("retry_after")
                wait = float(ra) if ra else float(push["wait"])
                until = self._clock() + wait + random.random()
                if until > self.paused_until:
                    self.paused_until = until
                    self.reason = push["reason"]
                if first and push.get("try"):
                    lines.insert(0, "{}, waiting {:.0f} s (try {} of {}){}".format(
                        push["reason"], wait, push["try"], push["of"],
                        " - as the service asked" if ra else ""))
            elif ok:
                self.streak += 1
                if self.limit < self.max and self.streak >= RECOVER_AFTER:
                    self.limit += 1
                    self.streak = 0
                    lines.append("service answers again - {} request(s) at a time".format(
                        self.limit))
            self.cond.notify_all()
        for ln in lines:
            self.emit(ln)

    def backoff(self, reason, wait, attempt, retries):
        """An error that is not a push-back: this worker alone waits (logged),
        cancel-aware."""
        self.emit("{}, waiting {:.0f} s (try {} of {})".format(reason, wait, attempt + 2,
                                                                 retries))
        with self.cond:
            self.blocked += 1
        try:
            self.sleep(wait)
        finally:
            with self.cond:
                self.blocked -= 1

    def sleep(self, seconds):
        end = self._clock() + seconds
        while True:
            _check_cancel()
            rem = end - self._clock()
            if rem <= 0:
                return
            if _CANCEL.wait(min(rem, _SLICE_S)):
                raise BuildCancelled("cancelled")
            self.beat()


_NET = contextvars.ContextVar("nd_pack_net", default=None)


# ------------------------------------------------------------------- HTTP --
def _ua():
    return C.HTTP_USER_AGENT + " offline-pack-builder/" + BUILDER_VERSION


def _sleep_plain(seconds):
    if _CANCEL.wait(seconds):
        raise BuildCancelled("cancelled")


def http_json(url, params=None, post=False, retries=RETRIES, what=""):
    """GET / POST and decode JSON, retried with back-off. An ArcGIS
    {"error": ...} answer is an error too. v2.8.0: inside a data set (a
    NetSession is set) the requests share a limiter, a push-back (429 / 503 /
    504 / timeout) lowers the number of requests at once and pauses all of
    them together (Retry-After honoured), and every wait is logged."""
    data = None
    full = url
    if params:
        enc = urlencode(params)
        if post:
            data = enc.encode("utf-8")
        else:
            full = url + ("&" if "?" in url else "?") + enc
    net = _NET.get()
    last = None
    for attempt in range(retries):
        _check_cancel()
        slot = net.acquire() if net is not None else None
        ok = False
        kind, reason, wait, retry_after, stop = None, "", 0.0, None, False
        more = attempt + 1 < retries
        try:
            try:
                req = Request(full, data=data, headers={
                    "User-Agent": _ua(), "Accept": "application/json, */*",
                    **({"Content-Type": "application/x-www-form-urlencoded"} if data else {})})
                with urlopen(req, timeout=TIMEOUT_S) as r:
                    raw = r.read()
                obj = json.loads(raw.decode("utf-8"))
                if isinstance(obj, dict) and "error" in obj:
                    err = obj["error"]
                    code = err.get("code") if isinstance(err, dict) else None
                    msg = "service error: {}".format(
                        err.get("message") if isinstance(err, dict) else err)
                    try:
                        code = int(code)
                    except (TypeError, ValueError):
                        code = None
                    if code in THROTTLE_CODES:
                        raise ServiceBusy(code, msg)
                    raise BuildError(msg)
                ok = True
                return obj
            except HTTPError as e:
                last = e
                reason = "HTTP {}".format(e.code)
                if e.code in THROTTLE_CODES:
                    kind = "busy"
                    retry_after = _retry_after_s(e)
                else:
                    kind = "error"
                if e.code in (400, 401, 403, 404) and attempt >= 1:
                    stop = True                         # will not get better
            except BuildCancelled:
                raise
            except ServiceBusy as e:
                last = e
                kind, reason = "busy", "HTTP {}".format(e.code)
            except _NET_ERRORS as e:
                last = e
                if _is_timeout(e):
                    kind, reason = "timeout", "timed out"
                else:
                    kind, reason = "error", _short(e)
            # the wait after a failed try (the last try does not wait, but a
            # push-back still slows the others down)
            wait = _backoff_s(kind, attempt if more else 0)
        finally:
            if net is not None:
                push = None
                if kind in ("busy", "timeout"):
                    push = {"reason": reason, "wait": wait, "retry_after": retry_after,
                            "try": attempt + 2 if (more and not stop) else None,
                            "of": retries}
                net.release(slot, ok=ok, push=push)
        if stop:
            break
        if more:                                    # v2.6.2: no sleep after the last try
            if net is None:
                _sleep_plain(wait + random.random())
            elif kind == "error":
                net.backoff(reason, wait, attempt, retries)
            # busy / timeout: the shared pause holds every worker (acquire)
    raise BuildError("{}{} ({})".format(what + ": " if what else "", url.split("?")[0], last))


def _pool(ctx, workers, calls, on_result):
    """Run calls [(fn, args)] on a thread pool; on_result(args, result or
    exception) runs on this thread as each finishes. A cancel (an exception
    from the progress callback) drops the calls not started yet at once.
    v2.8.0: this thread also keeps the progress bar alive while every worker
    waits on the service (NetSession.beat), and the workers see the data
    set's NetSession."""
    net = _NET.get()
    if net is not None:
        net.set_workers(workers)
    ex = ThreadPoolExecutor(max_workers=max(1, workers))
    futs, order = {}, {}
    for fn, args in calls:
        fu = ex.submit(contextvars.copy_context().run, fn, *args)
        futs[fu] = args
        order[fu] = len(order)
    pending = set(futs)
    tick = max(0.01, min(1.0, HEARTBEAT_S))
    try:
        while pending:
            done, pending = _cf_wait(pending, timeout=tick, return_when=FIRST_COMPLETED)
            if not done:
                if net is not None:
                    net.beat(main=True)
                continue
            for fu in sorted(done, key=order.get):     # first come, first stored
                try:
                    res = fu.result()
                except BuildCancelled:
                    raise
                except _NET_ERRORS as e:
                    res = e
                on_result(futs[fu], res)
            if net is not None:
                net.touch()
    except BaseException:
        _CANCEL.set()                      # running workers stop at their next request
        try:
            ex.shutdown(wait=False, cancel_futures=True)
        except TypeError:                               # Python < 3.9
            for fu in futs:
                fu.cancel()
            ex.shutdown(wait=False)
        raise
    ex.shutdown(wait=True)


# ---------------------------------------------------------------- ArcGIS --
def _envelope(b):
    return {"geometry": "{},{},{},{}".format(*b), "geometryType": "esriGeometryEnvelope",
            "inSR": "4326", "spatialRel": "esriSpatialRelIntersects"}


def _split4(b):
    x0, y0, x1, y1 = b
    xm, ym = (x0 + x1) / 2.0, (y0 + y1) / 2.0
    return [(x0, y0, xm, ym), (xm, y0, x1, ym), (x0, ym, xm, y1), (xm, ym, x1, y1)]


def arcgis_ids(url, bbox, where="1=1", depth=0):
    """Every object id in the box (the box is split when the service will
    not list them all at once)."""
    try:
        d = http_json(url.rstrip("/") + "/query", dict(_envelope(bbox), where=where,
                                                         returnIdsOnly="true", f="json"),
                      post=True, retries=2 if depth else 3, what="object ids")
        ids = d.get("objectIds") or []
        return set(int(i) for i in ids)
    except BuildCancelled:
        raise
    except BuildError:
        # 1.19.0: a slow service (3DHP: HTTP 504 for a whole-state id list) answers a smaller box -
        # split sooner and deeper (4^5 boxes at most)
        if depth >= 5:
            raise
        out = set()
        for q in _split4(bbox):
            out |= arcgis_ids(url, q, where, depth + 1)
        return out


def _arcgis_chunk(url, ids, out_fields, precision, retries=RETRIES):
    params = {"objectIds": ",".join(str(i) for i in ids), "outFields": out_fields,
              "outSR": "4326", "returnGeometry": "true", "f": "geojson"}
    if precision is not None:
        params["geometryPrecision"] = str(precision)
    d = http_json(url.rstrip("/") + "/query", params, post=True, retries=retries,
                  what="features")
    return d.get("features", []) if isinstance(d, dict) else []


def _arcgis_chunk_split(url, ids, out_fields, precision, missing, health=None, depth=0):
    """A chunk; a failing chunk is halved until the single failing features
    are known. v2.6.2: the halves are tried once each (not with the full
    retries), and when the service keeps failing (health) the rest of the
    chunk is given up at once instead of halving into thousands of requests."""
    health = health or _Health()
    if health.down():
        missing.extend(ids)
        return []
    try:
        feats = _arcgis_chunk(url, ids, out_fields, precision,
                              retries=RETRIES if depth == 0 else 1)
        health.ok()
        return feats
    except BuildCancelled:
        raise
    except BuildError:
        if health.failed() or len(ids) == 1:
            missing.extend(ids)
            return []
        h = len(ids) // 2
        return (_arcgis_chunk_split(url, ids[:h], out_fields, precision, missing, health,
                                    depth + 1) +
                _arcgis_chunk_split(url, ids[h:], out_fields, precision, missing, health,
                                    depth + 1))


# ------------------------------------------------------- chunk-level resume --
# A data set is fetched in chunks; every finished chunk is stored at once with
# a key (PackWriter.add(chunk=key)), and a rerun after Cancel / crash / failure
# fetches only the chunks that are not stored. A chunk is keyed by WHAT it
# holds - the object ids of an ArcGIS batch, the box of a tile, the offset of
# a page - so an id list that changed between runs never skips a chunk wrongly.
def _ids_key(ids):
    crc = zlib.crc32(",".join(str(i) for i in ids).encode("ascii")) & 0xffffffff
    return "ids:{}:{}:{}:{:08x}".format(ids[0], ids[-1], len(ids), crc)


def _pack_json(obj):
    return zlib.compress(json.dumps(obj, separators=(",", ":")).encode("utf-8"), 6)


def _unpack_json(blob):
    try:
        return json.loads(zlib.decompress(blob).decode("utf-8"))
    except (TypeError, ValueError, zlib.error):
        return None


def _tile_key(t):
    return "tile:{:.6f},{:.6f},{:.6f},{:.6f}".format(*t)


def _resume_ids(ctx, ids):
    """The object ids already stored (chunks kept from an earlier run that are
    still valid for `ids`). A stored chunk holding an id the service no longer
    lists (features deleted meanwhile) is dropped whole - its remaining ids
    are fetched again, so the result is what the service lists now."""
    w = ctx.writer
    kept = w.chunks()
    if not kept:
        return set()
    listed = set(ids)
    done, stale = set(), []
    for key, blob in kept.items():
        cids = _unpack_json(blob) if key.startswith("ids:") and blob else None
        if not isinstance(cids, list) or not all(i in listed for i in cids):
            stale.append(key)
        else:
            done.update(cids)
    if stale:
        w.drop_chunks(stale)
        ctx.log("  the service's feature list changed since the earlier run - {} stored "
                "batch(es) are read again".format(len(stale)))
    if done:
        ctx.log("  resuming: {:,} of {:,} features already stored".format(len(done), len(ids)))
    return done


def _resume_tiles(ctx, tiles):
    """(keys of the tiles already stored, key of every tile). Tiles share
    features along their edges, so when the earlier run used other tiles the
    data set starts over."""
    keys = [_tile_key(t) for t in tiles]
    kept = ctx.writer.chunks()
    if not kept:
        return set(), keys
    if any(k not in set(keys) for k in kept):
        ctx.log("  the earlier partial download used other tiles - starting over")
        ctx.writer.restart_stage()
        return set(), keys
    ctx.log("  resuming: {} of {} tiles already stored".format(len(kept), len(tiles)))
    return set(kept), keys


def _arcgis_chunk_job(url, ids, out_fields, precision, health):
    """One batch of ids on a worker: (features, ids that could not be read)."""
    miss = []
    feats = _arcgis_chunk_split(url, ids, out_fields, precision, miss, health)
    return feats, miss


def _uid_arcgis(oid_field):
    low = (oid_field or "").lower()

    def uid(f):
        if f.get("id") is not None:
            return f["id"]
        props = f.get("properties") or {}
        for k, v in props.items():
            if k.lower() in (low, "objectid", "fid", "oid"):
                return v
        return None
    return uid


def _known_fields(info, out_fields, log):
    """1.19.0: the wanted fields the layer really has (a field list written for one service
    version must not fail the whole layer); "*" stays."""
    if not out_fields or out_fields == "*":
        return "*"
    have = {str(f.get("name")).lower(): f.get("name") for f in info.get("fields") or []}
    if not have:
        return out_fields
    want = [x.strip() for x in out_fields.split(",") if x.strip()]
    keep = [have[x.lower()] for x in want if x.lower() in have]
    lost = [x for x in want if x.lower() not in have]
    if lost:
        log("  fields the layer does not have (left out): {}".format(", ".join(lost)))
    return ",".join(keep) if keep else "*"


def _grid(b, n):
    x0, y0, x1, y1 = b
    dx, dy = (x1 - x0) / n, (y1 - y0) / n
    return [(x0 + i * dx, y0 + j * dy, x0 + (i + 1) * dx, y0 + (j + 1) * dy)
            for j in range(n) for i in range(n)]


def _arcgis_tile_job(url, tile, oid, size, out_fields, precision):
    """One tile on a worker: every feature touching the box, paged by offset."""
    feats, offset = [], 0
    while True:
        params = dict(_envelope(tile), where="1=1", outFields=out_fields, outSR="4326",
                      f="geojson", resultOffset=offset, resultRecordCount=size)
        if oid:
            params["orderByFields"] = "{} ASC".format(oid)
        if precision is not None:
            params["geometryPrecision"] = str(precision)
        d = http_json(url.rstrip("/") + "/query", params, post=True, retries=RETRIES + 3,
                      what="tile")
        page = d.get("features", []) if isinstance(d, dict) else []
        feats.extend(page)
        exceeded = bool(d.get("exceededTransferLimit") or
                        (d.get("properties") or {}).get("exceededTransferLimit"))
        if not page or (len(page) < size and not exceeded):
            return feats
        offset += len(page)


def fetch_arcgis_tiles(ctx, url, bbox, tile_deg, out_fields="*", precision=None,
                       transform=None):
    """1.19.0: a heavy layer (3DHP: HTTP 504 for a state-wide id list, but small boxes answer) is read
    in tiles of tile_deg degrees, each tile paged by offset; a tile that touches two boxes is kept
    once (the feature id). A finished tile is stored at once, so a rerun reads only what is missing."""
    info = http_json(url.rstrip("/"), {"f": "json"}, what="layer description")
    out_fields = _known_fields(info, out_fields, ctx.log)
    transform = transform or (lambda feats: feats)
    oid = info.get("objectIdField") or next(
        (f.get("name") for f in info.get("fields") or []
         if f.get("type") == "esriFieldTypeOID"), None)
    size = max(1, min(1000, int(info.get("maxRecordCount") or 1000)))
    uid = _uid_arcgis(oid)
    tiles = _tiles(bbox, tile_deg)
    ctx.log("  {} tiles of {} degree".format(len(tiles), tile_deg))
    have, keys = _resume_tiles(ctx, tiles)
    failed = []
    done = [len(have)]

    def got(args, res):
        if isinstance(res, Exception):
            failed.append((args[1], str(res)))
        else:
            ctx.writer.add(transform(res), uid, chunk=_tile_key(args[1]))
        done[0] += 1
        ctx.sub(100.0 * done[0] / len(tiles), "{} of {} tiles, {:,} features".format(
            done[0], len(tiles), ctx.writer._cur["n"]))
    _pool(ctx, ctx.workers, [(_arcgis_tile_job, (url, t, oid, size, out_fields, precision))
                             for t, k in zip(tiles, keys) if k not in have], got)
    if failed:
        if len(failed) > max(2, len(tiles) // 20):
            raise BuildError("{} of {} tiles failed, e.g. {}".format(
                len(failed), len(tiles), failed[0][1]))
        note = "{} tile(s) could not be read: {}".format(len(failed), "; ".join(
            "{:.2f},{:.2f}".format(t[0], t[1]) for t, _e in failed))
        ctx.log("  WARNING: " + note)
        ctx.gaps = [tuple(t) for t, _e in failed]
        ctx.status = STATUS_PARTIAL
        return note
    return ""


def fetch_arcgis(ctx, url, bbox, out_fields="*", precision=None, transform=None, id_grid=1,
                 tile_deg=None):
    """Whole-area ArcGIS layer: the object ids first, then the features by
    id in parallel chunks. Services that cannot list ids are paged by
    offset. Returns a note. transform(features) -> features is applied to
    every batch before it is stored (3DHP: the NHD-style properties).
    tile_deg: read in tiles of that size instead (see fetch_arcgis_tiles)."""
    if tile_deg:
        return fetch_arcgis_tiles(ctx, url, bbox, tile_deg, out_fields, precision, transform)
    info = http_json(url.rstrip("/"), {"f": "json"}, what="layer description")
    out_fields = _known_fields(info, out_fields, ctx.log)
    transform = transform or (lambda feats: feats)
    oid = info.get("objectIdField") or next(
        (f.get("name") for f in info.get("fields") or []
         if f.get("type") == "esriFieldTypeOID"), None)
    mrc = int(info.get("maxRecordCount") or IDS_CHUNK)
    size = max(1, min(IDS_CHUNK, mrc))
    uid = _uid_arcgis(oid)
    try:
        if id_grid > 1:             # 1.19.0: a heavy layer (3DHP) times out on the whole state
            found = set()
            boxes = _grid(bbox, id_grid)
            for k, q in enumerate(boxes):
                ctx.sub(0.0, "listing features, area {} of {}".format(k + 1, len(boxes)))
                found |= arcgis_ids(url, q)
            ids = sorted(found)
        else:
            ids = sorted(arcgis_ids(url, bbox))
    except BuildCancelled:
        raise
    except BuildError as e:
        ctx.log("  object ids not listed ({}) - reading pages instead".format(e))
        return _fetch_arcgis_pages(ctx, url, bbox, oid, mrc, out_fields, precision, uid,
                                   transform)
    done_ids = _resume_ids(ctx, ids)
    ctx.log("  {:,} features".format(len(ids)))
    if not ids:
        return "none in the area"
    todo = [i for i in ids if i not in done_ids]
    chunks = [todo[i:i + size] for i in range(0, len(todo), size)]
    missing = []
    finished = [len(done_ids)]
    health = _Health()
    if done_ids:
        ctx.sub(100.0 * len(done_ids) / len(ids), "{:,} of {:,} features".format(
            len(done_ids), len(ids)))

    def got(args, res):
        ch = args[1]
        if isinstance(res, Exception):
            missing.extend(ch)
        else:
            feats, miss = res
            feats = transform(feats)
            if miss:                # not wholly read: no chunk record - read again next run
                missing.extend(miss)
                ctx.writer.add(feats, uid)
            else:
                ctx.writer.add(feats, uid, chunk=_ids_key(ch), payload=_pack_json(ch))
        finished[0] += len(ch)
        ctx.sub(100.0 * finished[0] / len(ids), "{:,} of {:,} features".format(
            min(len(ids), finished[0]), len(ids)))
    _pool(ctx, ctx.workers, [(_arcgis_chunk_job, (url, ch, out_fields, precision, health))
                             for ch in chunks], got)
    n = ctx.writer._cur["n"]
    note = ""
    if health.down():
        raise BuildError("the service stopped answering ({} requests failed in a row) - "
                         "{:,} of {:,} features read".format(health.limit, n, len(ids)))
    if missing or n < len(ids):
        lost = len(missing) or len(ids) - n
        note = "{:,} of {:,} features ({} could not be read)".format(n, len(ids), lost)
        ctx.log("  WARNING: " + note)
        # v2.6.2: never "complete" with features missing - where they are is
        # unknown, so the plugin uses this data set only when the service
        # cannot be reached, with a warning; many missing: it fails (retried
        # on the next build, the NID mirror is tried)
        if lost > max(1, MAX_MISSING_SHARE * len(ids)):
            raise BuildError(note)
        ctx.status = STATUS_PARTIAL
    return note


def _fetch_arcgis_pages(ctx, url, bbox, oid, mrc, out_fields, precision, uid, transform=None):
    transform = transform or (lambda feats: feats)
    offset, page = 0, 0
    size = max(1, min(2000, mrc))
    # v2.8.0: a page is a chunk too (key = offset and size; payload = how many
    # features it returned and whether more follow)
    kept = ctx.writer.chunks()
    stale = [k for k in kept if not k.startswith("page:{}:".format(size))]
    if stale:
        ctx.writer.drop_chunks(stale)
        kept = ctx.writer.chunks()
    if kept:
        ctx.log("  resuming: {} page(s) already stored".format(len(kept)))
    while True:
        key = "page:{}:{}".format(size, offset)
        meta = _unpack_json(kept[key]) if kept.get(key) else None
        if isinstance(meta, dict) and isinstance(meta.get("n"), int):
            nfe, exceeded = meta["n"], bool(meta.get("exceeded"))
            page += 1
            ctx.sub(min(95.0, page), "{:,} features".format(ctx.writer._cur["n"]))
            if not nfe or (nfe < size and not exceeded):
                return ""
            offset += nfe
            continue
        params = dict(_envelope(bbox), where="1=1", outFields=out_fields, outSR="4326",
                      f="geojson", resultOffset=offset, resultRecordCount=size)
        if oid:
            params["orderByFields"] = "{} ASC".format(oid)
        if precision is not None:
            params["geometryPrecision"] = str(precision)
        d = http_json(url.rstrip("/") + "/query", params, post=True, what="page")
        feats = transform(d.get("features", []) if isinstance(d, dict) else [])
        exceeded = bool(d.get("exceededTransferLimit") or
                        (d.get("properties") or {}).get("exceededTransferLimit"))
        added = ctx.writer.add(feats, uid, chunk=key,
                               payload=_pack_json({"n": len(feats), "exceeded": exceeded}))
        page += 1
        ctx.sub(min(95.0, page), "{:,} features".format(ctx.writer._cur["n"]))
        if not feats or (len(feats) < size and not exceeded):
            return ""
        if added == 0:
            return "the service repeats the same page (no paging) - {:,} kept".format(
                ctx.writer._cur["n"])
        offset += len(feats)


# -------------------------------------------------------------------- NSI --
def _tiles(b, deg):
    x0, y0, x1, y1 = b
    nx = max(1, int(math.ceil((x1 - x0) / deg - 1e-9)))
    ny = max(1, int(math.ceil((y1 - y0) / deg - 1e-9)))
    dx, dy = (x1 - x0) / nx, (y1 - y0) / ny
    return [(x0 + i * dx, y0 + j * dy, x0 + (i + 1) * dx, y0 + (j + 1) * dy)
            for j in range(ny) for i in range(nx)]


def _nsi_tile(tile, depth=0):
    x0, y0, x1, y1 = tile
    ring = [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]
    bbox = ",".join("{:.6f},{:.6f}".format(x, y) for x, y in ring)
    try:
        d = http_json(C.NSI_API_URL, {"bbox": bbox, "fmt": "fc"}, what="NSI tile")
        return d.get("features", []) if isinstance(d, dict) else []
    except BuildCancelled:
        raise
    except BuildError:
        if depth >= 2:
            raise
        out = []
        for q in _split4(tile):
            out += _nsi_tile(q, depth + 1)
        return out


def fetch_nsi(ctx, bbox):
    tiles = _tiles(bbox, NSI_TILE_DEG)
    ctx.log("  {} tiles of {} degree".format(len(tiles), NSI_TILE_DEG))
    have, keys = _resume_tiles(ctx, tiles)
    failed = []
    done = [len(have)]

    def uid(f):
        return (f.get("properties") or {}).get("fd_id")

    def got(args, res):
        if isinstance(res, Exception):
            failed.append((args[0], str(res)))
        else:
            ctx.writer.add(res, uid, chunk=_tile_key(args[0]))
        done[0] += 1
        ctx.sub(100.0 * done[0] / len(tiles), "{} of {} tiles, {:,} structures".format(
            done[0], len(tiles), ctx.writer._cur["n"]))
    _pool(ctx, ctx.workers, [(_nsi_tile, (t,)) for t, k in zip(tiles, keys)
                             if k not in have], got)
    if failed:
        if len(failed) > max(2, len(tiles) // 20):
            raise BuildError("{} of {} NSI tiles failed, e.g. {}".format(
                len(failed), len(tiles), failed[0][1]))
        note = "{} tile(s) could not be read: {}".format(len(failed), "; ".join(
            "{:.2f},{:.2f}".format(t[0], t[1]) for t, _e in failed))
        ctx.log("  WARNING: " + note)
        # v2.6.2: the plugin treats a study area touching these as not covered
        ctx.gaps = [tuple(t) for t, _e in failed]
        return note
    return ""


# -------------------------------------------------------------- Overpass --
def _overpass(query):
    d = http_json(C.OVERPASS_URL, {"data": query}, post=True, what="Overpass")
    remark = d.get("remark") if isinstance(d, dict) else None
    if remark and any(s in str(remark).lower() for s in
                      ("error", "timed out", "timeout", "out of memory", "aborted")):
        raise BuildError("Overpass: {}".format(remark))
    return d


def _osm_tile(make_query, parse, tile, depth=0):
    try:
        return parse(_overpass(O.with_timeout(make_query(tile), OVERPASS_TIMEOUT_S)))
    except BuildCancelled:
        raise
    except BuildError:
        if depth >= 2:
            raise
        out = []
        for q in _split4(tile):
            out += _osm_tile(make_query, parse, q, depth + 1)
        return out


def fetch_osm(ctx, bbox, make_query, parse):
    tiles = _tiles(bbox, OVERPASS_TILE_DEG)
    have, keys = _resume_tiles(ctx, tiles)
    done = [len(have)]
    errors = []

    def uid(f):
        return (f.get("properties") or {}).get("osm_id")

    def got(args, res):
        if isinstance(res, Exception):
            errors.append(res)
        else:
            ctx.writer.add(res, uid, chunk=_tile_key(args[2]))
        done[0] += 1
        ctx.sub(100.0 * done[0] / len(tiles), "{} of {} areas, {:,} features".format(
            done[0], len(tiles), ctx.writer._cur["n"]))
    _pool(ctx, min(ctx.workers, C.OVERPASS_WORKERS),
          [(_osm_tile, (make_query, parse, t)) for t, k in zip(tiles, keys)
           if k not in have], got)
    if errors:
        # an area missing would silently drop features: the data set fails
        raise BuildError("{} of {} areas failed: {}".format(len(errors), len(tiles), errors[0]))
    return ""


# ------------------------------------------------------------- data sets --
def datasets():
    """[(name, label, group, kind, spec)] - name = the plugin's download
    name, so the plugin finds each data set under the name it asks for."""
    d = [
        ("NSI_structures", "USACE National Structure Inventory", "nsi", "nsi", {}),
        ("NDGISHUB_address_points", "ND GIS Hub site / structure address points", "address",
         "arcgis", {"url": C.ND_ADDRESS_POINTS_URL}),
        ("USA_Structures", "FEMA USA Structures outlines", "usa", "arcgis",
         {"url": C.USA_STRUCTURES_URL}),
        ("NID_dams", "National Inventory of Dams", "nid", "arcgis",
         {"urls": [C.NID_URL, C.NID_MIRROR_URL]}),
        ("NARN_rail_lines", "FRA/BTS rail network", "rail", "arcgis", {"url": C.NARN_URL}),
        ("FRA_grade_crossings", "FRA grade crossings", "rail", "arcgis",
         {"url": C.CROSSINGS_URL}),
        ("ND_roads_NG911", "ND GIS Hub NG9-1-1 road centerlines", "roads", "arcgis",
         {"url": C.ND_ROADS_URL}),
    ]
    hpms = []
    for url in C.HPMS_ND_URLS:
        year = "".join(ch for ch in url.split("HPMS_FULL_ND_")[-1][:4] if ch.isdigit())
        hpms.append(("HPMS_ND_{}".format(year), url))
    d.append(("HPMS_ND", "FHWA HPMS North Dakota (newest year that answers)", "traffic",
              "first_of", {"choices": hpms}))
    if C.NDDOT_AADT_URL:
        d.append(("NDDOT_traffic_counts", "NDDOT traffic counts", "traffic", "arcgis",
                  {"url": "{}/{}".format(C.NDDOT_AADT_URL.rstrip("/"),
                                         C.NDDOT_AADT_LAYERS.get("aadt", 0))}))
    d.append(("ND_county_roads", "ND GIS Hub county roads (AADT)", "traffic", "arcgis",
              {"url": C.ND_COUNTY_ROADS_URL}))
    for key, label, url, _cat in C.lifeline_downloads():
        d.append(("lifeline_" + key, label, "lifelines", "arcgis", {"url": url}))
    d += [
        ("NBI_bridges", "National Bridge Inventory", "bridges", "arcgis", {"url": C.NBI_URL}),
        ("3DHP_flowlines", "USGS 3DHP flowlines (3D Hydrography Program)", "hydro", "arcgis",
         _hydro_spec(C.NHD_FLOWLINE_URL, True)),
        ("3DHP_waterbodies", "USGS 3DHP waterbodies (3D Hydrography Program)", "hydro", "arcgis",
         _hydro_spec(C.NHD_WATERBODY_URL, False)),
        ("SARP_dams", "Aquatic barrier dams - SARP (tool.aquaticbarriers.org)", "barriers",
         "arcgis", {"url": C.SARP_DAMS_URL, "fields": SARP_DAM_FIELDS, "precision": 6,
                    "source": "Southeast Aquatic Resources Partnership (SARP) aquatic barrier "
                              "prioritization, dams (the data behind tool.aquaticbarriers.org, "
                              "Great Plains / Intermountain West region)",
                    "used_by": "reference (fish passage / dam inventory cross-check)"}),
        ("SARP_surveyed_crossings", "Aquatic barrier road-stream crossings, surveyed - SARP",
         "barriers", "arcgis",
         {"url": C.SARP_CROSSINGS_URL, "fields": SARP_CROSSING_FIELDS, "precision": 6,
          "source": "Southeast Aquatic Resources Partnership (SARP) aquatic barrier "
                    "prioritization, assessed (field-surveyed) road-stream crossings (the data "
                    "behind tool.aquaticbarriers.org, Great Plains / Intermountain West region)",
          "used_by": "reference (culverts and bridges on streams)"}),
        ("OSM_lifelines", "OpenStreetMap lifeline facilities", "osm", "osm",
         {"query": O.overpass_query, "parse": O.overpass_to_features}),
        ("OSM_crossings", "OpenStreetMap culverts / fords / rail bridges", "osm", "osm",
         {"query": O.crossings_query, "parse": O.crossing_features}),
        ("OSM_campgrounds", "OpenStreetMap campgrounds / RV parks", "osm", "osm",
         {"query": O.camps_query, "parse": O.camp_features}),
        ("OSM_trailer_parks", "OpenStreetMap mobile-home parks", "osm", "osm",
         {"query": O.trailer_parks_query, "parse": O.trailer_park_features}),
        ("OSM_places", "OpenStreetMap cities / towns / villages", "osm", "osm",
         {"query": O.places_query, "parse": O.place_features}),
    ]
    d += file_datasets()
    d += suite_datasets()
    return d


# 1.19.0: pack fields kept from the SARP services (they have 257 / 236 fields; a field the layer
# lacks is left out with a note - _known_fields)
SARP_DAM_FIELDS = ("lat,lon,Name,SARPID,Source,SourceID,NIDID,River,StreamSizeClass,"
                   "YearCompleted,Height,Length,Width,Hazard,Construction,Purpose,Passability,"
                   "Condition,Feasibility,StorageVolume,WaterbodyAcres,Fatality,LowheadDam,"
                   "Diversion,Canal,Intermittent,StreamOrder,TotDASqKm,StateRegulated,"
                   "FERCRegulated,Removed,YearRemoved,YearSurveyed,OwnerType,BarrierOwnerType,"
                   "SourceLink,NHDPlusID,Snapped,HUC8,HUC12,State,County")
SARP_CROSSING_FIELDS = ("lat,lon,Name,SARPID,Source,SourceID,CrossingCode,SurveyID,River,"
                        "StreamSizeClass,Road,RoadType,CrossingType,Constriction,"
                        "PotentialProject,BarrierSeverity,SARP_Score,ProtocolUsed,Resurveyed,"
                        "Removed,YearRemoved,YearSurveyed,Condition,Canal,Intermittent,"
                        "StreamOrder,TotDASqKm,OwnerType,BarrierOwnerType,SourceLink,NHDPlusID,"
                        "Snapped,HUC8,HUC12,State,County")


def _hydro_spec(url, flow):
    """The 3DHP layer's spec; a data-source file that points the address at the older NHD service
    gets that service's own fields (and no transform)."""
    if H3.is_3dhp_url(url):
        return {"url": url, "fields": H3.FLOW_FIELDS if flow else H3.WB_FIELDS, "precision": 6,
                "tile_deg": 0.25,
                "transform": H3.normalise_flowlines if flow else H3.normalise_waterbodies,
                "source": "USGS 3D Hydrography Program (3DHP_all MapServer, layer {}); "
                          "NHD-style gnis_name / gnis_id / ftype / fcode added to the 3DHP "
                          "properties".format(H3.layer_of_url(url)),
                "used_by": "Hazard (travel time), Breach Runner (culverts), Screening (pool "
                           "area)"}
    return {"url": url, "precision": 6, "fields": "gnis_name,gnis_id,ftype,fcode,reachcode,"
            "lengthkm" if flow else "gnis_name,gnis_id,ftype,fcode,areasqkm"}


def file_datasets():
    """1.19.0: the layers that come from files (DWR exports, HIFLD downloads) and the NLCD raster.
    kind "file": packfiles scans the suite data folder's dwr/ and hifld/ (and --files); the DWR
    dams fall back to the DWR service when no file is found. kind "raster": the MRLC web
    coverage service, written as a GeoPackage raster (needs GDAL)."""
    out = []
    for name, label, _src, _g, _b in PF.KNOWN:
        if name in PF.OSM_NAMES:         # listed in datasets() as Overpass downloads; a file wins
            continue
        spec = {}
        if name == "DWR_dams":
            spec = {"fallback": {"url": C.DWR_DAMS_URL, "precision": 6,
                                 "source": "ND DWR dams inventory, public service (95 of the "
                                           "export's 109 fields); a DWR export file in dwr/ "
                                           "replaces it",
                                 "used_by": "Screening, Breach Runner"}}
        out.append((name, label, "dwr" if name.startswith("DWR") else "hifld", "file", spec))
    out.append((NLCD_NAME, "NLCD 2021 land cover, North Dakota, 30 m (EPSG:5070)", "landcover",
                "raster", {"service": "mrlc_nlcd2021_wcs",
                           "source": "USGS / MRLC National Land Cover Database 2021 (MRLC WCS)",
                           "used_by": "Screening, Breach Runner (Manning's n)"}))
    return out


# 2.17.0: data sets the suite's dam data reads (damsuite.damdata.PACK_SETS) from the servers
# chosen in Suite data and servers: counties always (the built-in server), the DWR dams and dam
# lines and FEMA flood zones only when a server was chosen there (the built-in DWR choice is the
# HSIP copy, not DWR's inventory; statewide flood zones are large)
SUITE_SETS = (("counties", "ND_counties", "County boundaries", "reference", True),
              ("dwr_dams", "DWR_dams", "ND DWR dams inventory", "dwr", False),
              ("dwr_dam_lines", "DWR_dam_lines", "ND DWR dam lines", "dwr", False),
              ("flood_zones", "FEMA_flood_zones", "FEMA flood hazard zones", "reference", False))


def suite_datasets(catalog=None):
    try:
        try:
            from .damsuite import services as SV
        except ImportError:
            from damsuite import services as SV      # noqa
        cat = catalog if catalog is not None else SV.load()
    except Exception:                                # noqa - no suite list: nothing extra
        return []
    out = []
    for role, name, label, group, always in SUITE_SETS:
        if not always and not cat.choice.get(role):
            continue
        s = cat.chosen(role)
        if s is None or s.get("kind") not in ("arcgis_feature", "arcgis_map") or \
                not SV.data_capable(s, role):
            continue
        try:
            url = SV.layer_url(s)
        except SV.ServiceError:
            continue
        out.append((name, "{} ({})".format(label, s.get("name") or url), group, "arcgis",
                    {"url": url}))
    return out


class _Ctx(object):
    def __init__(self, writer, workers, log, sub, beat=None, refresh=False):
        self.writer, self.workers, self.log, self.sub = writer, workers, log, sub
        self.beat = beat                     # v2.8.0: beat(text) - progress while waiting
        self.refresh = refresh               # v2.8.0: no chunk resume
        self.status = "complete"             # v2.6.2: or partial
        self.gaps = []                       # boxes that could not be read


_GROUP_ALIASES = {"nhd": "hydro", "3dhp": "hydro", "nlcd": "landcover", "sarp": "barriers",
                  "NHD_flowlines": "3DHP_flowlines", "NHD_waterbodies": "3DHP_waterbodies"}


def _pick(names_or_groups, table):
    want = set(_GROUP_ALIASES.get(x.strip(), x.strip())
               for x in (names_or_groups or "").split(",") if x.strip())
    if not want:
        return None
    known = set(n for n, *_r in table) | set(g for _n, _l, g, _k, _s in table)
    bad = want - known
    if bad:
        raise BuildError("unknown data set(s): {} (see --list)".format(", ".join(sorted(bad))))
    return want


def build(out_zip, pad=DEFAULT_PAD_DEG, only=None, skip=None, workers=None, refresh=False,
          sources_file=None, progress=None, log=None, files=None, box=None):
    """Build (or resume / update) the pack and write out_zip. progress(pct,
    msg) and log(msg) are optional callbacks; an exception raised by
    progress (a cancel) stops the build - the finished data sets are kept
    for the next run. Returns the manifest. box: (xmin, ymin, xmax, ymax) lon / lat of another
    area than North Dakota. files: extra folders searched for the DWR / HIFLD
    layer files (the suite data folder's dwr/ and hifld/ are always searched; folder packs
    only)."""
    log = log or print
    progress = progress or (lambda p, m: None)
    _CANCEL.clear()
    # v2.6.2: inside QGIS the plugin has applied the data-source file already;
    # applying it again from here cleared the plugin's overrides
    if sources_file and not __package__:
        C.apply_source_overrides(sources_file)
        if C.SOURCE_OVERRIDES:
            log("Data-source file {}: {} address(es) changed".format(
                sources_file, len(C.SOURCE_OVERRIDES)))
    table = datasets()
    only_set, skip_set = _pick(only, table), _pick(skip, table) or set()
    chosen = [t for t in table if (only_set is None or t[0] in only_set or t[2] in only_set)
              and t[0] not in skip_set and t[2] not in skip_set]
    todo = [t for t in chosen if t[3] not in ("file", "raster")]
    file_todo = [t for t in chosen if t[3] == "file"]
    raster_todo = [t for t in chosen if t[3] == "raster"]
    bbox = tuple(float(v) for v in box) if box else \
        (ND_BBOX[0] - pad, ND_BBOX[1] - pad, ND_BBOX[2] + pad, ND_BBOX[3] + pad)
    out_zip = os.path.abspath(out_zip)
    # 2.24.0: a folder pack (one GeoPackage per layer) unless the name ends in .zip
    folder_pack = not out_zip.lower().endswith(".zip")
    if (file_todo or raster_todo) and not folder_pack:
        log("The layer files and the land cover are added to a pack FOLDER only (not to a "
            ".zip): {}".format(", ".join(t[0] for t in file_todo + raster_todo)))
        file_todo, raster_todo = [], []
    left_out = {}                            # 1.19.0: what could not be put in, and why
    if folder_pack and any(t[0] in ("3DHP_flowlines", "3DHP_waterbodies") for t in todo):
        todo = _add_3dhp_bulk(out_zip, todo, bbox, files, refresh, progress, log)
    if file_todo:
        todo += _add_layer_files(out_zip, file_todo, bbox, files, refresh, left_out, progress,
                                 log)
    if folder_pack and any(t[0] in PF.OSM_NAMES for t in todo):
        todo = _add_osm_files(out_zip, todo, bbox, files, refresh, progress, log)
    work = (out_zip.rstrip("\\/") if folder_pack else os.path.splitext(out_zip)[0]) + "_work"
    os.makedirs(work, exist_ok=True)
    db = os.path.join(work, DB_NAME)
    log("Offline data pack for North Dakota - area {:.3f}, {:.3f} to {:.3f}, {:.3f} "
        "(lon / lat)".format(*bbox))
    log("Working file: {}".format(db))
    writer = PackWriter(db, bbox, created_by="build_nd_pack.py {} / plugin {}".format(
        BUILDER_VERSION, C.PLUGIN_VERSION))
    workers = max(1, int(workers or C.DOWNLOAD_WORKERS))
    failures = []
    try:
        n = len(todo)
        for i, (name, label, _group, kind, spec) in enumerate(todo):
            lo, hi = 100.0 * i / n, 100.0 * (i + 1) / n

            shown = {"p": 0.0, "m": ""}

            def sub(p, m="", lo=lo, hi=hi, label=label, shown=shown):
                shown["p"], shown["m"] = p, m
                progress(lo + (hi - lo) * max(0.0, min(100.0, p)) / 100.0,
                         "{}: {}".format(label, m) if m else label)

            def beat(text, lo=lo, hi=hi, label=label, shown=shown):
                # v2.8.0: the bar does not move, but the message shows life
                progress(lo + (hi - lo) * max(0.0, min(100.0, shown["p"])) / 100.0,
                         "{}: {}{}".format(label, text, " - " + shown["m"] if shown["m"] else ""))
            ctx = _Ctx(writer, workers, log, sub, beat, refresh)
            names = [c[0] for c in spec["choices"]] if kind == "first_of" else [name]
            # (partial, or complete with gaps: fetched again - it may be complete now;
            # a failed re-fetch keeps what there is)
            if not refresh and any(writer.done(x) for x in names):
                log("[{}/{}] {}: already in the pack (kept - --refresh fetches it "
                    "again)".format(i + 1, n, label))
                continue
            log("[{}/{}] {}".format(i + 1, n, label))
            sub(0, "starting")
            t0 = time.time()
            try:
                got = _run_one(ctx, name, label, kind, spec, bbox)
                log("  done: {:,} features in {:.0f} s".format(got, time.time() - t0))
            except (PackError, BuildCancelled):
                raise
            except _NET_ERRORS as e:
                failures.append((label, str(e)))
                log("  FAILED: {}".format(e))
    finally:
        manifest = writer.manifest()
        writer.close(finalize=False)
    if folder_pack:
        progress(95.0, "Writing the GeoPackages...")
        manifest = _write_folder(db, out_zip, manifest, log, progress,
                                 extra_info={t[0]: {k: t[4][k] for k in ("source", "used_by")
                                                    if t[4].get(k)} for t in todo})
        if raster_todo:
            manifest = _add_rasters(out_zip, raster_todo, bbox, refresh, left_out, failures,
                                    progress, log)
        if left_out:
            manifest.setdefault("left_out", {}).update(left_out)
            _gp().write_manifest(out_zip, manifest)
        manifest["_failures"] = failures
        _report(manifest, failures, log, out_zip)
        progress(100.0, "Offline data pack written")
        return manifest
    progress(99.0, "Writing the zip...")
    manifest = _write_zip(db, out_zip, manifest, log)
    ok = [k for k, v in manifest["datasets"].items() if v.get("status") == "complete"]
    total = sum(int(v.get("count") or 0) for v in manifest["datasets"].values()
                if v.get("status") == "complete")
    log("")
    log("Pack written: {} ({:.1f} MB) - {} data sets, {:,} features".format(
        out_zip, os.path.getsize(out_zip) / 1048576.0, len(ok), total))
    bad = {k: v for k, v in manifest["datasets"].items()
           if v.get("status") not in ("complete", STATUS_PARTIAL, "not available")}
    for k, v in sorted(bad.items()):
        log("  NOT in the pack: {} - {}".format(k, v.get("note") or v.get("status")))
    for k, v in sorted(manifest["datasets"].items()):
        if v.get("status") == STATUS_PARTIAL or v.get("gaps"):
            log("  INCOMPLETE (used with a warning; run again to retry): {} - {}".format(
                k, v.get("note") or "features missing"))
        elif v.get("status") == "complete" and v.get("note"):
            log("  note: {} - {}".format(k, v["note"]))
    if failures or bad:
        log("Run the same command again later to retry what failed (finished data sets "
            "are kept).")
    progress(100.0, "Offline data pack written")
    manifest["_failures"] = failures
    return manifest


def _run_one(ctx, name, label, kind, spec, bbox):
    w = ctx.writer
    if kind == "first_of":
        errors = []
        for nm, url in spec["choices"]:
            try:
                n = _run_one(ctx, nm, label, "arcgis", {"url": url}, bbox)
            except BuildCancelled:
                raise
            except _NET_ERRORS as e:
                errors.append((nm, str(e)))
                ctx.log("  {} not available ({}) - trying the next".format(nm, e))
                continue
            for old, _e in errors:          # a newer year not published: not a failure
                w.con.execute("UPDATE datasets SET status='not available', note=? "
                              "WHERE name=?", ("not published - {} used".format(nm), old))
            w.con.commit()
            return n
        raise BuildError("; ".join("{}: {}".format(a, b) for a, b in errors))
    if kind == "arcgis" and spec.get("urls"):
        errors = []
        urls = spec["urls"]
        kept = None                          # count of an incomplete result already stored
        for k, url in enumerate(urls):
            try:
                n = _run_one(ctx, name, label, "arcgis", dict(spec, url=url, urls=None), bbox)
            except BuildCancelled:
                raise
            except _NET_ERRORS as e:
                errors.append(str(e))
                ctx.log("  {} failed ({}) - trying the mirror".format(url.split("/rest/")[0],
                                                                      e))
                continue
            # v2.6.2: features missing - the mirror may have them all (if it
            # fails, the incomplete result stays in the pack)
            if ctx.status != "complete" and k + 1 < len(urls):
                kept = n
                errors.append("incomplete")
                ctx.log("  {} incomplete - trying the mirror".format(url.split("/rest/")[0]))
                ctx.status, ctx.gaps = "complete", []
                continue
            return n
        if kept is not None:
            ctx.log("  the mirror failed too - the incomplete download is kept")
            return kept
        raise BuildError("; ".join(errors))
    url = spec.get("url") or (C.NSI_API_URL if kind == "nsi" else C.OVERPASS_URL)
    # v2.8.0: continue the chunks an interrupted run stored (not with --refresh)
    kept = w.begin(name, label, url, resume=not getattr(ctx, "refresh", False),
                   signature=_signature(kind, spec, bbox))
    if kept:
        ctx.log("  continuing an interrupted download: {} finished chunk(s) kept, {:,} "
                "features".format(kept, w._cur["n"]))
    ctx.status, ctx.gaps = "complete", []
    net = NetSession(label, ctx.workers, log=ctx.log, on_beat=getattr(ctx, "beat", None))
    token = _NET.set(net)
    try:
        if kind == "nsi":
            note = fetch_nsi(ctx, bbox)
        elif kind == "arcgis":
            note = fetch_arcgis(ctx, url, bbox, spec.get("fields", "*"),
                                spec.get("precision"), spec.get("transform"),
                                spec.get("id_grid", 1), spec.get("tile_deg"))
        elif kind == "osm":
            note = fetch_osm(ctx, bbox, spec["query"], spec["parse"])
        else:
            raise BuildError("unknown kind " + kind)
    except BuildCancelled:
        w.fail(name, "interrupted")
        raise
    except _NET_ERRORS as e:
        w.fail(name, str(e))
        raise
    except BaseException:
        w.fail(name, "interrupted")
        raise
    finally:
        _NET.reset(token)
    if net.pushbacks:
        ctx.log("  the service pushed back {} time(s) (HTTP 429 / 503 / 504 or timeouts)".format(
            net.pushbacks))
    return w.finish(note or "", status=ctx.status, gaps=ctx.gaps)


def _signature(kind, spec, bbox):
    """What must be equal for stored chunks of an interrupted run to be
    reused: the settings that change what a chunk holds."""
    if kind == "arcgis":
        tr = spec.get("transform")
        return "arcgis|{}|{}{}".format(spec.get("fields", "*"), spec.get("precision"),
                                       "|" + tr.__name__ if tr is not None else "")
    if kind == "nsi":
        return "nsi|{}".format(NSI_TILE_DEG)
    if kind == "osm":
        try:
            q = spec["query"](tuple(bbox))
            return "osm|{}|{:08x}".format(OVERPASS_TILE_DEG,
                                          zlib.crc32(str(q).encode("utf-8")) & 0xffffffff)
        except Exception:
            return "osm|{}".format(OVERPASS_TILE_DEG)
    return kind


def _gp():
    try:
        from .damsuite import gpkgpack as GP
    except ImportError:                                          # outside QGIS
        from damsuite import gpkgpack as GP                      # noqa
    return GP


def _add_layer_files(pack, file_todo, bbox, extra_folders, refresh, left_out, progress, log):
    """1.19.0: the DWR / HIFLD layer files into the pack folder (before the downloads). Returns the
    entries to download instead (the DWR dams from the DWR service when there is no file)."""
    GP = _gp()
    try:
        from .damsuite import suitedata as SD
    except ImportError:
        from damsuite import suitedata as SD                     # noqa
    try:
        data = SD.data_folder()
    except Exception:                                            # noqa
        data = ""
    extra = [extra_folders] if isinstance(extra_folders, str) else list(extra_folders or [])
    try:
        saved = SD.layer_files_folder()
    except Exception:                                            # noqa
        saved = ""
    folders = PF.folders_for(data, extra + ([saved] if saved else []))
    progress(0.0, "Looking for the DWR / HIFLD layer files...")
    log("Layer files (DWR exports, HIFLD downloads): searching {}".format(
        ", ".join(folders) or "no folder (set the suite data folder, or use --files)"))
    found = PF.scan(folders)
    names = {t[0] for t in file_todo}
    for n, p in sorted(found.items()):
        if n in names:
            log("  found {}: {}".format(n, p))
    added, problems = PF.add_files(pack, found, bbox=bbox, only=names, replace=refresh, log=log)
    have = (GP.read_manifest(pack) or {}).get("datasets") or {}
    fallback = []
    for name, label, group, _k, spec in file_todo:
        if name in added or (name in have and name not in problems):
            continue
        if name in problems:
            log("  {}: NOT added - {}".format(name, problems[name]))
        fb = spec.get("fallback")
        if fb and name not in have:
            log("  {}: no usable file - the public service is used".format(name))
            fallback.append((name, label + " (public service)", group, "arcgis", fb))
        elif name not in have:
            left_out[name] = problems.get(name) or PF.missing_text(name)
            log("  {}: not in the pack - {}".format(name, left_out[name]))
    return fallback


def _add_osm_files(pack, todo, bbox, extra_folders, refresh, progress, log):
    """The OpenStreetMap layers (OSM_*) that exist as files - made from a Geofabrik extract by the
    ND Data Updater (its osm_layers folder) or nd_osm_download.py - go into the pack from the file;
    the Overpass API (which answers HTTP 429 / 504 for state-sized requests) is tried only for
    the others. The suite data folder's osm_layers/, --files and the Layer files folder are
    searched. Returns `todo` without the data sets the files supplied."""
    try:
        from .damsuite import suitedata as SD
    except ImportError:
        from damsuite import suitedata as SD                     # noqa
    try:
        data = SD.data_folder()
    except Exception:                                            # noqa
        data = ""
    extra = [extra_folders] if isinstance(extra_folders, str) else list(extra_folders or [])
    try:
        saved = SD.layer_files_folder()
    except Exception:                                            # noqa
        saved = ""
    folders = PF.folders_for(data, extra + ([saved] if saved else []))
    names = {t[0] for t in todo} & set(PF.OSM_NAMES)
    found = {k: v for k, v in PF.scan(folders).items() if k in names}
    if not found:
        return todo
    progress(0.0, "Reading the OpenStreetMap layer files...")
    for n, p in sorted(found.items()):
        log("  found {}: {}".format(n, p))
    added, problems = PF.add_files(pack, found, bbox=bbox, only=names, replace=refresh, log=log)
    have = (_gp().read_manifest(pack) or {}).get("datasets") or {}
    for n, msg in sorted(problems.items()):
        log("  {}: NOT added from the file - {}; the Overpass API is tried".format(n, msg))
    return [t for t in todo if not (t[0] in found and (t[0] in added or (
        t[0] in have and t[0] not in problems)))]


def _add_3dhp_bulk(pack, todo, bbox, extra_folders, refresh, progress, log):
    """1.19.0: when the 3DHP national file (National Map download; the suite data folder's 3dhp/
    folder, or --files) is at hand, the flowlines and waterbodies are cut out of it - the web
    service answers HTTP 504 / 502 to state-sized requests. Returns `todo` without the data sets
    the file supplied; a missing file or a failure leaves them to the web service."""
    try:
        from .damsuite import suitedata as SD
    except ImportError:
        from damsuite import suitedata as SD                     # noqa
    try:
        data = SD.data_folder()
    except Exception:                                            # noqa
        data = ""
    extra = [extra_folders] if isinstance(extra_folders, str) else list(extra_folders or [])
    try:
        saved = SD.layer_files_folder()
    except Exception:                                            # noqa
        saved = ""
    path = PF.scan_3dhp(PF.folders_3dhp(data, extra + ([saved] if saved else [])))
    if not path:
        return todo
    names = {t[0] for t in todo} & {"3DHP_flowlines", "3DHP_waterbodies"}
    log("3DHP national file found: {} - the flowlines and waterbodies are cut out of it".format(
        path))
    progress(0.0, "Cutting the 3DHP flowlines and waterbodies out of {}...".format(
        os.path.basename(path)))
    added, problems = PF.add_3dhp(pack, path, bbox=bbox, only=names, replace=refresh, log=log)
    have = (_gp().read_manifest(pack) or {}).get("datasets") or {}
    for n, msg in sorted(problems.items()):
        log("  {}: NOT added from the file - {}; the web service is tried".format(n, msg))
    return [t for t in todo if not (t[0] in names and (t[0] in added or (
        t[0] in have and t[0] not in problems)))]


def _nlcd_tiles(box, step):
    x0, y0, x1, y1 = box
    nx, ny = int(math.ceil((x1 - x0) / step)), int(math.ceil((y1 - y0) / step))
    for j in range(ny):
        for i in range(nx):
            yield (x0 + i * step, y0 + j * step, min(x1, x0 + (i + 1) * step),
                   min(y1, y0 + (j + 1) * step))


def _add_rasters(pack, raster_todo, bbox, refresh, left_out, failures, progress, log):
    """1.19.0: the NLCD 2021 land cover (MRLC web coverage service, 30 m, EPSG:5070) as a
    GeoPackage raster of the pack: the state's box fetched in 90 km tiles and joined. Needs GDAL
    (QGIS has it)."""
    GP = _gp()
    try:
        from .damsuite import services as SV
        from .damsuite import terrain_cache as TC
    except ImportError:
        from damsuite import services as SV                      # noqa
        from damsuite import terrain_cache as TC                 # noqa
    man = GP.load_or_new(pack, bbox)
    for name, label, _g, _k, spec in raster_todo:
        if name in (man.get("rasters") or {}) and not refresh:
            log("[raster] {}: already in the pack (kept - --refresh fetches it again)".format(name))
            continue
        log("[raster] {}".format(label))
        try:
            from osgeo import gdal, osr                          # noqa
        except ImportError:
            left_out[name] = "needs GDAL to write a raster: run the build inside QGIS"
            log("  NOT built - " + left_out[name])
            continue
        service = next((s for s in SV.BUILTIN if s["id"] == spec.get("service")), None)
        if service is None:
            left_out[name] = "no land-cover service in the services list"
            continue
        t0 = time.time()
        try:
            _fetch_nlcd(pack, man, name, label, service, bbox, spec, gdal, osr, SV, TC,
                        progress, log)
            log("  done in {:.0f} s".format(time.time() - t0))
        except BuildCancelled:
            raise
        except Exception as e:                                   # noqa - report, go on
            failures.append((label, str(e)))
            log("  FAILED: {}".format(e))
    GP.write_manifest(pack, man)
    return man


def _fetch_nlcd(pack, man, name, label, service, bbox, spec, gdal, osr, SV, TC, progress, log):
    import tempfile
    src = osr.SpatialReference()
    src.ImportFromEPSG(4326)
    dst = osr.SpatialReference()
    dst.ImportFromEPSG(5070)
    for s in (src, dst):
        try:
            s.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        except AttributeError:
            pass
    tr = osr.CoordinateTransformation(src, dst)
    xs, ys = [], []
    x0, y0, x1, y1 = bbox
    for k in range(21):                      # the box's edge, so the curved edges are covered
        f = k / 20.0
        for lon, lat in ((x0 + f * (x1 - x0), y0), (x0 + f * (x1 - x0), y1),
                         (x0, y0 + f * (y1 - y0)), (x1, y0 + f * (y1 - y0))):
            px, py, _z = tr.TransformPoint(lon, lat)
            xs.append(px)
            ys.append(py)
    cell = 30.0
    box = TC.snap((min(xs), min(ys), max(xs), max(ys)), cell)
    tiles = list(_nlcd_tiles(box, 3000 * cell))
    log("  {:.0f} x {:.0f} km in {} tiles".format((box[2] - box[0]) / 1000, (box[3] - box[1]) / 1000,
                                                 len(tiles)))
    work = tempfile.mkdtemp(prefix="nd_nlcd_")
    parts = []
    try:
        for i, t in enumerate(tiles):
            _check_cancel()
            size = (int(round((t[2] - t[0]) / cell)), int(round((t[3] - t[1]) / cell)))
            url = SV.wcs_coverage_url(service, t, size)
            progress(96.0 + 3.0 * i / len(tiles), "{}: tile {} of {}".format(label, i + 1,
                                                                             len(tiles)))
            p = os.path.join(work, "t{:03d}.tif".format(i))
            data = None
            for attempt in range(RETRIES):
                try:
                    req = Request(url, headers={"User-Agent": _ua()})
                    with urlopen(req, timeout=TIMEOUT_S) as r:
                        data = r.read()
                    if data[:4] in (b"II*\x00", b"MM\x00*"):
                        break
                    raise BuildError("the land-cover service sent no GeoTIFF: {!r}".format(
                        bytes(data[:80])))
                except _NET_ERRORS as e:
                    data = None
                    if attempt + 1 >= RETRIES:
                        raise BuildError("land-cover tile {}: {}".format(i + 1, _short(e)))
                    time.sleep(_backoff_s("error", attempt))
            with open(p, "wb") as fh:
                fh.write(data)
            parts.append(p)
        vrt = os.path.join(work, "nlcd.vrt")
        gdal.BuildVRT(vrt, parts)
        tif = os.path.join(work, "nlcd.tif")
        gdal.Translate(tif, vrt, format="GTiff", creationOptions=["COMPRESS=LZW", "TILED=YES"])
        info = {"label": label, "source": spec.get("source") or "", "url": service["url"],
                "fetched": time.strftime("%Y-%m-%d"), "used_by": spec.get("used_by") or ""}
        _gp().add_raster_file(pack, man, name, tif, info, log)
    finally:
        import shutil
        shutil.rmtree(work, ignore_errors=True)


def _write_folder(db, folder, manifest, log, progress=None, extra_info=None):
    """2.24.0: the data sets of the working file as the pack folder's GeoPackages - those the
    folder lacks or holds from an older fetch; the rest stay as they are.

    1.5.0: the folder's own copy is kept, never overwritten or dropped, when it is the better
    one. That is a layer the user replaced with their own file (``replaced``), a layer the folder
    holds with a fetch date at least as new as the working file's (the 3DHP flowlines /
    waterbodies cut straight to the folder from the National Map file, or an OSM file layer), and
    any usable folder copy when this run's fetch of that layer failed (status not usable). Before,
    every such layer was rewritten from the working file, so the user's own file and the 3DHP cut
    were silently put back and a failed re-fetch dropped a good layer out of the pack."""
    GP = _gp()
    have = (GP.read_manifest(folder) or {}).get("datasets") or {}

    def _folder_has(h):
        return bool(h) and os.path.isfile(os.path.join(folder, *(h.get("file") or "?").split("/")))

    todo = set()
    kept = []
    for name, v in (manifest.get("datasets") or {}).items():
        h = have.get(name) or {}
        work_usable = v.get("status") in ("complete", STATUS_PARTIAL)
        if not _folder_has(h):
            # the folder lacks it: write it from the work file, or (when this run could not fetch
            # it either) let convert_v1 record it under left_out so LAYERS.txt says it is missing
            todo.add(name)
            continue
        # the folder already holds a usable copy of this layer
        if h.get("replaced"):
            kept.append("{} (your own file)".format(name)); continue
        if not work_usable:
            kept.append("{} (this run's fetch failed; the pack keeps the copy it had)".format(name))
            continue
        if h.get("fetched") == v.get("fetched") and h.get("count") == v.get("count"):
            continue                           # identical: nothing to write
        if (h.get("fetched") or "") >= (v.get("fetched") or ""):
            kept.append("{} (the pack's copy is newer)".format(name)); continue
        todo.add(name)                         # the work file's copy is genuinely newer
    for k in kept:
        log("  kept the pack's own copy of " + k)
    log("Writing {} data set(s) to {} (one GeoPackage each)...".format(len(todo), folder))
    sub = (lambda p, m="": progress(95.0 + 4.0 * p / 100.0, "GeoPackages: " + m)) \
        if progress else None
    man = GP.convert_v1(db, folder, only=todo, log=log, progress=sub, extra_info=extra_info)
    # 1.19.0: the 3DHP layers replace the NHD ones of an earlier pack
    for old, new in (("NHD_flowlines", "3DHP_flowlines"), ("NHD_waterbodies", "3DHP_waterbodies")):
        if new in (man.get("datasets") or {}) and old in (man.get("datasets") or {}):
            GP.remove_dataset(folder, man, old, "replaced by {} (USGS 3DHP) on {}".format(
                new, time.strftime("%Y-%m-%d")))
            log("  {} removed: {} replaces it".format(old, new))
            GP.write_manifest(folder, man)
    return man


def _report(manifest, failures, log, out):
    ds = manifest.get("datasets") or {}
    total = sum(int(v.get("count") or 0) for v in ds.values())
    log("")
    log("Pack written: {} - {} layers, {:,} features (LAYERS.txt lists each layer and its "
        "source)".format(out, len(ds), total))
    for k, v in sorted((manifest.get("left_out") or {}).items()):
        log("  NOT in the pack: {} - {}".format(k, v))
    for k, v in sorted(ds.items()):
        if v.get("status") == STATUS_PARTIAL or v.get("gaps"):
            log("  INCOMPLETE (used with a warning; run again to retry): {} - {}".format(
                k, v.get("note") or "features missing"))
    if failures:
        log("Run the same command again later to retry what failed (finished data sets "
            "are kept).")


def _write_zip(db, out_zip, manifest, log):
    # a clean single file (no -wal beside it) goes in the zip
    w = PackWriter(db, tuple(manifest["bbox"]))
    manifest = w.manifest()
    leftovers = w.stage_leftovers() or w.has_chunks()
    w.close(finalize=True)
    manifest = dict(manifest)
    src = db
    if leftovers:
        # v2.8.0: the chunks of interrupted / failed data sets stay in the work file
        # (the next run continues with them) but do not go into the zip
        src = write_clean_copy(db, db + ".zipcopy")
    tmp = out_zip + ".part"
    readme = ("ND Dam Hazard Classification Helper - offline data pack\n\n"
              "Built {} for {} (lon / lat {}).\n\n"
              "On the offline computer: plugin > Data & Output tab > 7. Offline data pack > "
              "Browse..., pick this zip (it is unpacked once, next to the zip or in your "
              "home folder), and tick 'Offline only'.\n\nData sets (name: features, fetched "
              "on):\n{}\n").format(
        manifest.get("created"), manifest.get("area"), manifest.get("bbox"),
        "\n".join("  {}: {} {}{}".format(
            k, v.get("count"), v.get("fetched") or "",
            " - INCOMPLETE ({})".format(v.get("note")) if (
                v.get("status") == STATUS_PARTIAL or v.get("gaps")) else
            "" if v.get("status") == "complete" else
            " - not available ({})".format(v.get("note")) if v.get("status") == "not available"
            else " - NOT INCLUDED ({})".format(v.get("note")))
            for k, v in sorted(manifest["datasets"].items())))
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as z:
        z.writestr(MANIFEST_NAME, json.dumps(manifest, indent=2))
        z.writestr("README.txt", readme)
        z.write(src, DB_NAME)
    if src != db:
        try:
            os.remove(src)
        except OSError:
            pass
    os.replace(tmp, out_zip)
    with open(os.path.join(os.path.dirname(db), MANIFEST_NAME), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)
    return manifest


# -------------------------------------------------------------------- CLI --
def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Build the ND Dam Hazard Helper offline data pack (North Dakota).")
    ap.add_argument("--out", default="ND_Dam_Suite_Pack",
                    help="pack folder to write (one GeoPackage per layer), or a .zip for the "
                         "older one-file pack")
    ap.add_argument("--pad", type=float, default=DEFAULT_PAD_DEG,
                    help="margin around North Dakota's bounding box, degrees (default "
                         "%(default)s; 0 = the box itself)")
    ap.add_argument("--only", help="data sets or groups to fetch, comma separated")
    ap.add_argument("--skip", help="data sets or groups to leave out, comma separated")
    ap.add_argument("--refresh", action="store_true",
                    help="fetch again data sets that are already in the pack (and do not "
                         "continue interrupted downloads: everything is read again)")
    ap.add_argument("--workers", type=int, default=None,
                    help="requests at once (default {})".format(C.DOWNLOAD_WORKERS))
    ap.add_argument("--sources", help="the plugin's data-source file (JSON) to use its "
                                      "addresses")
    ap.add_argument("--files", action="append", default=None,
                    help="a folder with the DWR / HIFLD layer files (can be repeated; the suite "
                         "data folder's dwr/ and hifld/ are searched too)")
    ap.add_argument("--box", help="build a pack for another area: xmin,ymin,xmax,ymax in lon / lat "
                                  "(default: North Dakota plus --pad)")
    ap.add_argument("--list", action="store_true", help="list the data sets and stop")
    a = ap.parse_args(argv)
    if a.list:
        for name, label, group, kind, spec in datasets():
            src = spec.get("url") or ", ".join(spec.get("urls") or []) or \
                ", ".join(u for _n, u in spec.get("choices") or []) or \
                ("a file in dwr/ or hifld/" + (" (else " + spec["fallback"]["url"] + ")"
                                                if spec.get("fallback") else "")
                 if kind == "file" else "MRLC web coverage service" if kind == "raster" else
                 C.NSI_API_URL if kind == "nsi" else C.OVERPASS_URL)
            print("{:<32} group {:<10} {}\n{:<32} {}".format(name, group, label, "", src))
        return 0

    last = [0.0]
    tty = sys.stdout.isatty()

    def progress(p, m):
        if tty and time.time() - last[0] > 2.0:
            last[0] = time.time()
            sys.stdout.write("\r  {:5.1f}%  {:<90.90}".format(p, m))
            sys.stdout.flush()

    def log(m):
        if tty:
            sys.stdout.write("\r" + " " * 100 + "\r")
        print(m)
        sys.stdout.flush()
    try:
        man = build(a.out, a.pad, a.only, a.skip, a.workers, a.refresh, a.sources,
                    progress, log, files=a.files,
                    box=[float(v) for v in a.box.split(",")] if a.box else None)
    except (KeyboardInterrupt, BuildCancelled):
        print("\nStopped - the finished data sets are kept; run the same command to go on.")
        return 2
    except (BuildError, PackError) as e:
        print("\nERROR: {}".format(e))
        return 1
    bad = [k for k, v in man["datasets"].items()
           if v.get("status") not in ("complete", "not available") or v.get("gaps")]
    return 1 if (man.get("_failures") or bad) else 0


if __name__ == "__main__":
    sys.exit(main())
