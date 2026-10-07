# -*- coding: utf-8 -*-
"""v2.9: raster overviews (pyramids), so QGIS draws large rasters quickly
when zoomed out.

build_overviews() builds power-of-2 overview levels with GDAL - the same as
QGIS's Raster > Miscellaneous > Build Overviews (gdaladdo). With
external=True, or when the file cannot be opened for writing (read-only,
locked), they go to a "<raster>.ovr" sidecar and the raster itself is not
touched; external=False writes them inside the file.

Overviews are only used for drawing. The plugin reads every value at full
resolution (RasterSampler, the zone-raster strips), so they never change a
result.

 - ACER zone rasters (Hazard_*.tif): internal overviews right after they are
   written (processing.generate_hazard_rasters(overviews=True)).
 - ensure_input_overviews(paths): .ovr sidecars for the user's LARGE depth /
   velocity / arrival rasters that have none. Opt-in, because it writes next
   to the user's files. A sidecar does not change the raster file (size /
   time), so products cached on the raster's signature stay valid; QGIS sees
   new overviews of a layer it already has open after the layer is reloaded
   (or QGIS restarted).

Nothing here raises: a problem is logged and returned as (False, text).
"""

import os

from osgeo import gdal

from .compat import MSG_INFO, MSG_WARNING
from .constants import HAZARD_OVERVIEW_MIN_SIZE
from .helpers import log_message

DEFAULT_MIN_SIZE = 256        # overviews stop once one is smaller than this
_MAX_FACTOR = 1 << 16
_FLOAT_TYPES = tuple(getattr(gdal, n) for n in ("GDT_Float32", "GDT_Float64")
                     if hasattr(gdal, n))


def overview_levels(width, height, min_size=DEFAULT_MIN_SIZE):
    """Power-of-2 factors [2, 4, 8, ...]: a level is added while the previous
    one (the full raster first) still has its smaller side >= min_size, so
    the last overview is the first one whose smaller side is below min_size.
    [] when the raster's smaller side is already below min_size.
    Example: 5000 x 3000 px, 256 -> [2, 4, 8, 16] (the 1/16 overview is
    313 x 188 px)."""
    try:
        side = min(int(width), int(height))
        min_size = max(1, int(min_size))
    except (TypeError, ValueError):
        return []
    levels, f = [], 1
    while side / float(f) >= min_size and f < _MAX_FACTOR:
        f *= 2
        levels.append(f)
    return levels


def _open(path, mode):
    """gdal.Open that returns None instead of raising (whatever GDAL's
    exception mode is)."""
    try:
        return gdal.Open(path, mode)
    except Exception:
        return None


class _Config(object):
    """GDAL config options for the enclosed block only (thread-local where
    the bindings have it, so QGIS and other threads are not affected), the
    previous values put back afterwards."""

    def __init__(self, options):
        self.options = {k: v for k, v in (options or {}).items() if v}
        self.prev = {}

    @staticmethod
    def _fns():
        setter = getattr(gdal, "SetThreadLocalConfigOption", None) or \
            getattr(gdal, "SetConfigOption", None)
        getter = getattr(gdal, "GetThreadLocalConfigOption", None) or \
            getattr(gdal, "GetConfigOption", None)
        return setter, getter

    def __enter__(self):
        setter, getter = self._fns()
        for k, v in self.options.items():
            try:
                self.prev[k] = getter(k, None) if getter else None
                if setter:
                    setter(k, str(v))
            except Exception:
                pass
        return self

    def __exit__(self, *exc):
        setter, _getter = self._fns()
        for k, v in self.prev.items():
            try:
                if setter:
                    setter(k, v)
            except Exception:
                pass
        return False


def _callback(progress, label):
    """GDAL progress callback that forwards to progress(pct, text); an
    exception from progress (e.g. the user cancelled) stops GDAL."""
    if progress is None:
        return None
    state = {"cancelled": False}

    def cb(complete, message=None, data=None):
        try:
            progress(100.0 * float(complete), label)
        except Exception:
            state["cancelled"] = True
            return 0
        return 1
    cb.state = state
    return cb


def _fail(text, level=MSG_WARNING):
    log_message("Overviews: " + text, level)
    return False, text


def _gdal_failed(err):
    """BuildOverviews result: 0 / None = success (CE_None)."""
    if err is None:
        return False
    try:
        return int(err) != 0
    except (TypeError, ValueError):
        return True


def build_overviews(path, resampling="NEAREST", min_size=DEFAULT_MIN_SIZE, levels=None,
                    external=True, compress="DEFLATE", progress=None):
    """Build overviews for the raster file `path`. Returns (ok, text).

    resampling: GDAL method - NEAREST or MODE for class rasters (zones,
    bands), AVERAGE for continuous values. levels: factors, else
    overview_levels(width, height, min_size). external: a .ovr sidecar (also
    used when the file cannot be opened for writing). compress: overview
    compression (COMPRESS_OVERVIEW). progress(pct, text): optional; raising
    from it cancels. A raster too small for any level is not an error
    (True, "... no overviews needed"). Never raises."""
    name = os.path.basename(str(path or "")) or str(path)
    ds = None
    try:
        path = str(path or "").split("|")[0]
        if not path or not os.path.isfile(path):
            return _fail("{}: not a raster file".format(name))
        ext = bool(external) or not os.access(path, os.W_OK)
        if not ext:
            ds = _open(path, gdal.GA_Update)
            if ds is None:
                ext = True                     # read-only / locked: sidecar instead
                log_message("Overviews: {} cannot be written - using {}.ovr".format(
                    name, name), MSG_INFO)
        if ds is None:
            ds = _open(path, gdal.GA_ReadOnly)
        if ds is None:
            return _fail("{}: GDAL could not open it".format(name))
        w, h = int(ds.RasterXSize), int(ds.RasterYSize)
        if levels:
            lv = sorted({int(f) for f in levels if int(f) > 1})
        else:
            lv = overview_levels(w, h, min_size)
        if not lv:
            ds = None
            return True, "{}: {} x {} px - no overviews needed".format(name, w, h)
        method = str(resampling or "NEAREST").upper()
        ovr = path + ".ovr"
        had_ovr = os.path.exists(ovr)
        opts = {"COMPRESS_OVERVIEW": compress}
        if ext:
            opts["BIGTIFF_OVERVIEW"] = "IF_SAFER"     # a large .ovr may pass 4 GB
        cb = _callback(progress, "Building overviews: {}".format(name))
        err = None
        with _Config(opts):
            try:
                err = ds.BuildOverviews(method, lv, cb) if cb else ds.BuildOverviews(method, lv)
            except Exception as e:                   # GDAL exception mode
                err = e
        cancelled = bool(cb is not None and cb.state["cancelled"])
        if _gdal_failed(err) or cancelled:
            if not ext:
                try:                                  # no half-built internal levels
                    ds.BuildOverviews("NONE", [])
                except Exception:
                    pass
            ds = None                                 # closes the file (and the .ovr)
            if ext and not had_ovr and os.path.exists(ovr):
                try:
                    os.remove(ovr)                    # no half-written sidecar
                except OSError:
                    pass
            if cancelled:
                return _fail("{}: cancelled".format(name), MSG_INFO)
            return _fail("{}: GDAL could not build them ({})".format(
                name, err if isinstance(err, Exception) else "error code {}".format(err)))
        ds = None                                     # flush and close
        text = "{}: overviews 1/{} built ({}, {})".format(
            name, ", 1/".join(str(f) for f in lv), method,
            "{}.ovr".format(name) if ext else "inside the file")
        log_message("Overviews: " + text, MSG_INFO)
        return True, text
    except Exception as e:
        ds = None
        return _fail("{}: {}".format(name, e))


def has_overviews(path):
    """True when the raster already has overviews (inside the file or a .ovr
    sidecar)."""
    path = str(path or "").split("|")[0]
    if not path:
        return False
    if os.path.exists(path + ".ovr"):
        return True
    ds = _open(path, gdal.GA_ReadOnly)
    if ds is None:
        return False
    try:
        band = ds.GetRasterBand(1)
        return band is not None and int(band.GetOverviewCount()) > 0
    except Exception:
        return False
    finally:
        band = ds = None


def _auto_resampling(ds):
    """AVERAGE for floating-point values with a NoData value (depth, velocity,
    arrival); NEAREST otherwise (classes, or a raster whose NoData cells are
    not tagged - averaging would mix -9999 into the overviews)."""
    try:
        band = ds.GetRasterBand(1)
        if band.DataType in _FLOAT_TYPES and band.GetNoDataValue() is not None:
            return "AVERAGE"
    except Exception:
        pass
    return "NEAREST"


def ensure_input_overviews(paths, resampling="AUTO", small=HAZARD_OVERVIEW_MIN_SIZE,
                           min_size=DEFAULT_MIN_SIZE, progress=None):
    """Opt-in: a .ovr sidecar for each LARGE input raster that has no
    overviews yet (the raster file itself is not changed). Skipped: rasters
    that already have overviews, rasters with both sides <= `small` px,
    missing files. resampling "AUTO": see _auto_resampling. progress(pct,
    text): optional; raising from it stops the remaining rasters.
    Returns [(path, status, text)] - status "built", "present", "small",
    "skipped", "failed" or "cancelled". Never raises."""
    out, seen = [], set()
    todo = []
    for p in paths or []:
        p = str(p or "").split("|")[0]
        if not p:
            continue
        key = os.path.normcase(os.path.abspath(p))
        if key in seen:
            continue
        seen.add(key)
        todo.append(p)
    for i, p in enumerate(todo):
        name = os.path.basename(p)
        try:
            if not os.path.isfile(p):
                out.append((p, "skipped", "{}: not a file".format(name)))
                continue
            if has_overviews(p):
                out.append((p, "present", "{}: already has overviews".format(name)))
                continue
            ds = _open(p, gdal.GA_ReadOnly)
            if ds is None:
                out.append((p, "skipped", "{}: GDAL could not open it".format(name)))
                continue
            w, h = int(ds.RasterXSize), int(ds.RasterYSize)
            method = _auto_resampling(ds) if str(resampling).upper() == "AUTO" else resampling
            ds = None
            if max(w, h) <= int(small):
                out.append((p, "small", "{}: {} x {} px - drawn quickly without "
                                        "overviews".format(name, w, h)))
                continue
            sub = None
            if progress is not None:
                def sub(pct, text, _i=i):
                    progress((100.0 * _i + pct) / len(todo), text)
            ok, text = build_overviews(p, method, min_size=min_size, external=True,
                                       progress=sub)
            if not ok and text.endswith("cancelled"):
                out.append((p, "cancelled", text))
                break
            out.append((p, "built" if ok and "no overviews" not in text else
                        ("small" if ok else "failed"), text))
        except Exception as e:                        # never raises
            out.append((p, "failed", "{}: {}".format(name, e)))
            log_message("Overviews: {}: {}".format(name, e), MSG_WARNING)
    return out


def summary_lines(results):
    """Run-log lines for ensure_input_overviews results."""
    lines = []
    for _p, status, text in results or []:
        if status in ("built", "failed", "cancelled"):
            lines.append(("Overviews (input rasters): " + text) if status == "built" else
                         "Overviews not built - " + text)
    return lines
