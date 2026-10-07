# -*- coding: utf-8 -*-
"""Dams in series: which dam's breach flood reaches which other dam (v2.8).

ND REG_05.2026 5.3(c) and USBR ACER TM 11 ask the engineer to think about
other dams in the flood path. facilities.py already lists the other dams of
the National Inventory of Dams that are wet in the classified dam's own
flood. This module answers the question for a whole folder of dams that have
been studied one by one (each dam folder with its own dam-break rasters and
outputs/QA/Last_Run_Snapshot.json - the layout batch_classify and the
hazard-creep watch use):

  for every ORDERED pair (upstream A, downstream B) where B lies inside A's
  breach flood, the depth (and first arrival time, velocity when the raster
  exists) of A's flood at B's location is read - at the nearest wet cell
  within a tolerance of B's point (an NID point is often on the embankment,
  not in the channel) - and reported.

  CASCADE CANDIDATE   the depth at B is above a screening threshold: B may
                      fail or be overtopped by A's release, so A's
                      classification should consider B's downstream
                      consequences;
  CLASS ORDER         B's own class (ND or TM 11, from its last-run snapshot)
                      is higher than A's: check whether A's incremental
                      consequences must include B's failure.

THIS IS A SCREENING TABLE. It reads each upstream dam's OWN breach flood at
the downstream dam's point. It does not compute a combined or cascading
breach, does not route A's hydrograph through B's reservoir, and does not
decide whether B fails - that stays an engineering judgment. The thresholds
(depth threshold, search tolerance, minimum separation) are parameters; their
defaults are SCREENING VALUES CHOSEN BY THE ENGINEER, not regulatory numbers.

Dam locations come, in this order, from
  * an explicit point given by the caller (a "location" of an entry);
  * dam_location.json in the dam folder: {"lon": .., "lat": ..} or
    {"x": .., "y": .., "crs": "EPSG:26914"} (optionally "nid": "ND00123");
  * dam_location.csv (columns lon, lat or x, y, crs; optional nid, name);
  * an NID layer (the layer the plugin downloads / the engineer supplies) by
    the dam's NID id or, failing that, its name, read with the same
    facilities.read_dams the classification uses.
A dam whose location cannot be found is listed as "location unknown"; it is
not tested as a downstream dam and nothing fails.

Depths are in feet and arrival times in hours as in the plugin's own
rasters. Rasters may be in any CRS (points are converted to each raster's CRS).
"""

import csv
import html
import json
import math
import os
import time

import numpy as np

from . import history as HIS
from .constants import PLUGIN_NAME, PLUGIN_VERSION, OUTPUT_FOLDER_NAME, SUBDIR_QA, DAM_SEARCH_FT
from .helpers import log_message, dam_display_name, prepare_output_path
from .compat import MSG_WARNING

CSV_FILE = "Dams_In_Series_Screen.csv"
DAMS_CSV_FILE = "Dams_In_Series_Screen_Dams.csv"
HTML_FILE = "Dams_In_Series_Screen.html"
UPSTREAM_CSV_FILE = "Dams_Upstream_Of_This_Dam.csv"
LOCATION_JSON = "dam_location.json"
LOCATION_CSV = "dam_location.csv"

# ---- screening parameters (SCREENING VALUES CHOSEN BY THE ENGINEER) --------
DEFAULT_DEPTH_THRESHOLD_FT = 3.0     # depth at B above which B is a cascade candidate
DEFAULT_TOLERANCE_FT = DAM_SEARCH_FT  # nearest wet cell searched within this of B's point
DEFAULT_WET_FT = 0.0                 # a cell is wet when depth is above this
DEFAULT_MIN_SEPARATION_FT = 50.0     # two dam points closer than this are one dam
MAX_CHUNK_CELLS = 2000000            # raster window read per step

DISCLAIMER = ("SCREENING TABLE ONLY: each row reads the UPSTREAM dam's own breach flood at the "
              "DOWNSTREAM dam's point. No combined or cascading breach was computed, no "
              "hydrograph was routed through the downstream dam, and nothing here says the "
              "downstream dam fails - that is an engineering judgment.")

SERIES_FIELDS = ["Upstream", "Downstream", "Depth_ft", "First_Arrival_hr", "Velocity_fps",
                 "Max_Depth_Within_Tol_ft", "Sample_Distance_ft", "Upstream_ND",
                 "Upstream_TM11", "Downstream_ND", "Downstream_TM11", "Cascade_Candidate",
                 "Class_Order_Flag", "Flags", "Message", "Upstream_Folder", "Downstream_Folder"]
DAM_FIELDS = ["Dam", "Folder", "Location", "Location_Source", "Depth_Raster", "Arrival_Raster",
              "Velocity_Raster", "ND", "TM11", "Status", "Floods", "Flooded_By"]

_RANK = {"low": 1, "medium": 2, "significant": 2, "high": 3}


# ------------------------------------------------------------- locations ---
def _valid_lonlat(lon, lat):
    return (isinstance(lon, float) and isinstance(lat, float) and math.isfinite(lon)
            and math.isfinite(lat) and -180.0 <= lon <= 180.0 and -90.0 <= lat <= 90.0)


def _to_float(v):
    try:
        f = float(str(v).strip().replace(",", ".")) if not isinstance(v, (int, float)) \
            else float(v)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def _transform_ctx(ctx=None):
    if ctx is not None:
        return ctx
    from qgis.core import QgsProject
    return QgsProject.instance().transformContext()


def _xy_to_lonlat(x, y, crs_id, ctx=None):
    try:
        from qgis.core import (QgsCoordinateReferenceSystem, QgsCoordinateTransform,
                               QgsPointXY)
        src = QgsCoordinateReferenceSystem(str(crs_id))
        if not src.isValid():
            return None
        xf = QgsCoordinateTransform(src, QgsCoordinateReferenceSystem("EPSG:4326"),
                                    _transform_ctx(ctx))
        p = QgsPointXY(x, y)
        p = p if xf.isShortCircuited() else xf.transform(p)
        return float(p.x()), float(p.y())
    except Exception:
        return None


def parse_point(spec, ctx=None):
    """(lon, lat) in WGS 84 from a point given as {"lon", "lat"},
    {"x", "y", "crs"}, a (lon, lat) tuple (WGS 84), an (x, y, "EPSG:nnnn")
    tuple or an object with x() / y() (a QgsPointXY; WGS 84). None when it
    is missing or cannot be read."""
    if spec is None:
        return None
    lon = lat = None
    try:
        if isinstance(spec, dict):
            low = {str(k).strip().lower(): v for k, v in spec.items()}
            lon = _to_float(low.get("lon", low.get("longitude")))
            lat = _to_float(low.get("lat", low.get("latitude")))
            if lon is None or lat is None:
                x, y, crs = _to_float(low.get("x")), _to_float(low.get("y")), low.get("crs")
                if x is not None and y is not None and crs:
                    return _xy_to_lonlat(x, y, crs, ctx)
                return None
        elif hasattr(spec, "x") and callable(spec.x):
            lon, lat = _to_float(spec.x()), _to_float(spec.y())
        elif isinstance(spec, (tuple, list)):
            if len(spec) >= 3 and isinstance(spec[2], str) and spec[2].strip():
                x, y = _to_float(spec[0]), _to_float(spec[1])
                return None if x is None or y is None else _xy_to_lonlat(x, y, spec[2], ctx)
            if len(spec) >= 2:
                lon, lat = _to_float(spec[0]), _to_float(spec[1])
    except Exception:
        return None
    if lon is None or lat is None or not _valid_lonlat(float(lon), float(lat)):
        return None
    return float(lon), float(lat)


def read_location(folder, ctx=None):
    """The dam location kept in a dam folder: dam_location.json, else
    dam_location.csv. Returns {"point": (lon, lat) or None, "nid": text,
    "source": text, "problem": text}; None when the folder has neither file."""
    jpath = os.path.join(folder, LOCATION_JSON)
    if os.path.isfile(jpath):
        try:
            with open(jpath, encoding="utf-8-sig") as fh:
                data = json.load(fh)
        except (OSError, ValueError) as e:
            return {"point": None, "nid": "", "source": LOCATION_JSON,
                    "problem": "{} could not be read ({})".format(LOCATION_JSON, e)}
        if not isinstance(data, dict):
            return {"point": None, "nid": "", "source": LOCATION_JSON,
                    "problem": "{} is not a JSON object".format(LOCATION_JSON)}
        return _location_from(data, LOCATION_JSON, ctx)
    cpath = os.path.join(folder, LOCATION_CSV)
    if os.path.isfile(cpath):
        try:
            with open(cpath, newline="", encoding="utf-8-sig") as fh:
                text = fh.read()
            first = text.splitlines()[0] if text.strip() else ""
            delim = ";" if first.count(";") > first.count(",") else ","
            rows = list(csv.DictReader(text.splitlines(), delimiter=delim))
        except (OSError, csv.Error, UnicodeDecodeError) as e:
            return {"point": None, "nid": "", "source": LOCATION_CSV,
                    "problem": "{} could not be read ({})".format(LOCATION_CSV, e)}
        rows = [{(k or "").strip().lower(): (v or "").strip() for k, v in r.items()}
                for r in rows if any((v or "").strip() for v in r.values())]
        if not rows:
            return {"point": None, "nid": "", "source": LOCATION_CSV,
                    "problem": "{} has no rows".format(LOCATION_CSV)}
        me = dam_display_name(folder).strip().lower()
        pick = next((r for r in rows if (r.get("name") or r.get("dam") or "").lower() == me),
                    rows[0])
        return _location_from(pick, LOCATION_CSV, ctx)
    return None


def _location_from(data, source, ctx):
    low = {str(k).strip().lower(): v for k, v in data.items()}
    nid = str(low.get("nid") or low.get("nidid") or "").strip()
    pt = parse_point(low, ctx)
    problem = ""
    if pt is None and any(low.get(k) not in (None, "") for k in ("lon", "lat", "x", "y")):
        problem = "the point in {} could not be used (need lon + lat, or x + y + crs)".format(
            source)
    return {"point": pt, "nid": nid, "source": source, "problem": problem}


def write_location_file(folder, lon=None, lat=None, x=None, y=None, crs=None, nid="",
                        name="", source=""):
    """Write dam_location.json into a dam folder (for the classification run
    to leave behind the dam's point, e.g. the NID subject dam). Returns the
    path."""
    data = {}
    if lon is not None and lat is not None:
        data.update({"lon": round(float(lon), 7), "lat": round(float(lat), 7)})
    elif x is not None and y is not None and crs:
        data.update({"x": float(x), "y": float(y), "crs": str(crs)})
    else:
        raise ValueError("give lon + lat, or x + y + crs")
    if nid:
        data["nid"] = str(nid)
    if name:
        data["name"] = str(name)
    if source:
        data["source"] = str(source)
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, LOCATION_JSON)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1)
    os.replace(tmp, path)
    return path


def _norm_name(name):
    from .facilities import _norm_name as n
    return n(name)


def dams_from_layer(layer, ctx=None):
    """NID-like layer -> dam records in WGS 84 (facilities.read_dams, the
    function the classification uses). Each record has nid, name, geom."""
    from qgis.core import QgsCoordinateReferenceSystem
    from . import facilities as FAC
    return FAC.read_dams(layer, "NID layer", QgsCoordinateReferenceSystem("EPSG:4326"),
                         _transform_ctx(ctx))


def locate_from_nid(entries, dam_records):
    """Give entries without a point the location of their dam in the NID
    records (dams_from_layer): by NID id, else by the dam's name when exactly
    one inventory dam has it. Returns the number of dams located."""
    n = 0
    for e in entries:
        if e.get("point") is not None or not dam_records:
            continue
        nid = (e.get("nid") or "").strip().upper()
        hit = None
        how = ""
        if nid:
            hit = next((d for d in dam_records if (d.get("nid") or "").upper() == nid), None)
            how = "NID id {}".format(nid)
            if hit is None:
                e["location_note"] = "NID id {} is not in the NID layer".format(nid)
        if hit is None and not nid:
            target = _norm_name(e.get("name"))
            named = [d for d in dam_records if target and _norm_name(d.get("name")) == target]
            if len(named) == 1:
                hit, how = named[0], "name '{}'".format(named[0].get("name"))
            elif len(named) > 1:
                e["location_note"] = ("{} inventory dams are called '{}': give the NID id in "
                                      "{}".format(len(named), e.get("name"), LOCATION_JSON))
        if hit is not None:
            try:
                p = hit["geom"].asPoint()
                lon, lat = float(p.x()), float(p.y())
            except Exception:
                continue
            if _valid_lonlat(lon, lat):
                e["point"] = (lon, lat)
                e["nid"] = e.get("nid") or hit.get("nid") or ""
                e["location_source"] = "NID layer ({})".format(how)
                n += 1
    return n


# --------------------------------------------------------------- entries ---
def _class_of(snapshot):
    snap = snapshot
    if isinstance(snap, str):
        try:
            with open(snap, encoding="utf-8") as fh:
                snap = json.load(fh)
        except (OSError, ValueError):
            snap = None
    if not isinstance(snap, dict):
        return {"nd": "", "tm11": "", "lij": None, "time": ""}
    return {"nd": snap.get("nd") or "", "tm11": snap.get("tm11") or "",
            "lij": snap.get("lij"), "time": snap.get("time") or ""}


def make_entry(name, point=None, depth=None, arrival=None, velocity=None, snapshot=None,
               folder="", nid="", ctx=None):
    """One dam for screen(). `point`: see parse_point; `depth` / `arrival` /
    `velocity`: raster path or QgsRasterLayer (the dam's own breach maxima:
    depth in ft, first arrival in h); `snapshot`: the dam's Last_Run_Snapshot
    (dict or path) for its classes."""
    e = {"name": str(name), "folder": folder or "", "depth": depth, "arrival": arrival,
         "velocity": velocity, "nid": str(nid or "").strip(), "location_note": ""}
    e.update({"class": _class_of(snapshot)})
    pt = parse_point(point, ctx)
    e["point"] = pt
    e["location_source"] = "given" if pt is not None else ""
    if point is not None and pt is None:
        e["location_note"] = "the point given could not be used"
    return e


def entries_from_tuples(items, ctx=None):
    """Entries from [(name, point, depth raster, arrival raster, snapshot)] -
    a sixth item is the velocity raster; a dict with the keys of make_entry
    is accepted too."""
    out = []
    for it in items:
        if isinstance(it, dict):
            out.append(make_entry(it.get("name") or "Dam", it.get("point"), it.get("depth"),
                                  it.get("arrival"), it.get("velocity"), it.get("snapshot"),
                                  it.get("folder", ""), it.get("nid", ""), ctx))
            continue
        it = list(it) + [None] * (6 - len(it))
        out.append(make_entry(it[0], it[1], it[2], it[3], it[5], it[4], "", "", ctx))
    return _unique_names(out)


def _unique_names(entries):
    seen = {}
    for e in entries:
        n = e["name"]
        seen[n] = seen.get(n, 0) + 1
        if seen[n] > 1:
            e["name"] = "{} ({})".format(n, seen[n])
    return entries


def dam_folders(parent):
    from .watch import dam_folders as f
    return f(parent)


def discover(parent, ctx=None):
    """Entries for the dam folders directly inside `parent` (as the batch
    classification and the watch list them): rasters found by
    autodetect.find_rasters, classes from outputs/QA/Last_Run_Snapshot.json,
    location from dam_location.json / .csv. A dam folder without a depth
    raster is still listed (status "no depth raster")."""
    from .autodetect import find_rasters
    from .watch import qa_folder
    out = []
    for folder in dam_folders(parent):
        found = find_rasters(folder)
        loc = read_location(folder, ctx)
        e = make_entry(dam_display_name(folder), None, found.get("depth"), found.get("arr"),
                       found.get("vel"), HIS.read_snapshot(qa_folder(folder)), folder,
                       (loc or {}).get("nid", ""), ctx)
        if loc is not None:
            if loc["point"] is not None:
                e["point"], e["location_source"] = loc["point"], loc["source"]
            elif loc["problem"]:
                e["location_note"] = loc["problem"]
        out.append(e)
    return _unique_names(out)


# ------------------------------------------------------------- sampling ----
def _crs_id(crs):
    try:
        return crs.authid() or crs.toWkt()
    except Exception:
        return ""


class _Rasters(object):
    """The rasters of one upstream dam, opened once for all downstream dams."""

    def __init__(self, entry, tol_ft, ctx):
        from qgis.core import QgsRasterLayer
        from .helpers import RasterSampler
        self.ctx = ctx
        self.error = ""
        self.layers = {}
        self.samplers = {}
        for key in ("depth", "arrival", "velocity"):
            spec = entry.get(key)
            if not spec:
                continue
            try:
                lyr = spec if hasattr(spec, "isValid") else QgsRasterLayer(
                    str(spec), os.path.basename(str(spec)))
                smp = RasterSampler(lyr, buffer_ft=tol_ft) if lyr.isValid() else None
            except Exception as e:
                lyr, smp = None, None
                if key == "depth":
                    self.error = "depth raster could not be opened ({})".format(e)
            if smp is None or not smp.ok:
                if key == "depth" and not self.error:
                    self.error = "depth raster {} could not be read".format(
                        os.path.basename(str(spec)) if isinstance(spec, str) else "")
                continue
            self.layers[key], self.samplers[key] = lyr, smp
        if "depth" not in self.samplers and not self.error:
            self.error = "no depth raster"
        if "depth" in self.layers and not self.layers["depth"].crs().isValid():
            self.error = "the depth raster has no CRS"
        self._xf = {}

    def close(self):
        for s in self.samplers.values():
            try:
                s.close()
            except Exception:
                pass
        self.samplers = {}

    def _transform(self, x, y, src, dst):
        from qgis.core import QgsCoordinateTransform, QgsPointXY
        if _crs_id(src) == _crs_id(dst):
            return x, y
        key = (_crs_id(src), _crs_id(dst))
        if key not in self._xf:
            self._xf[key] = QgsCoordinateTransform(src, dst, self.ctx)
        p = self._xf[key].transform(QgsPointXY(x, y))
        return float(p.x()), float(p.y())

    def _wgs(self):
        from qgis.core import QgsCoordinateReferenceSystem
        if not hasattr(self, "_w"):
            self._w = QgsCoordinateReferenceSystem("EPSG:4326")
        return self._w

    def at(self, lon, lat, tol_ft, wet_ft):
        """Depth / arrival / velocity of this flood at a WGS 84 point, read
        at the nearest wet cell within tol_ft; None when no cell within the
        tolerance is wet (the point is not in this flood)."""
        from .helpers import ground_distance_xy
        smp, lyr = self.samplers.get("depth"), self.layers.get("depth")
        if smp is None:
            return None
        crs = lyr.crs()
        x, y = self._transform(lon, lat, self._wgs(), crs)
        # 2.18.0: latitude-aware (the point's own latitude) - Web Mercator
        # map units are stretched by 1 / cos(lat), geographic longitude
        # degrees shrink with cos(lat)
        tx, ty = ground_distance_xy(tol_ft, crs, lat=lat)
        tol = max(tx, ty)
        if not (math.isfinite(x) and math.isfinite(y)):
            return None
        # 2.18.0: geographic CRS - feet per degree differ east-west and
        # north-south, so distances are measured on scaled axes (tol units)
        scale = (tol / tx, tol / ty) if tx != ty and tx > 0 and ty > 0 else None
        best = _nearest_wet(smp, x, y, tol, wet_ft, scale=scale)
        if best is None:
            return None
        ratio = tol / tol_ft if tol_ft else 1.0
        out = {"depth": best["depth"], "dist_ft": best["dist"] / ratio if ratio else 0.0,
               "max_depth": best["max_depth"], "wet_cells": best["n"],
               "arrival": None, "velocity": None}
        for key, field in (("arrival", "arrival"), ("velocity", "velocity")):
            s, l2 = self.samplers.get(key), self.layers.get(key)
            if s is None:
                continue
            try:
                px, py = self._transform(best["x"], best["y"], crs, l2.crs())
                from qgis.core import QgsPointXY
                r = s.sample_located(QgsPointXY(px, py))
            except Exception:
                r = None
            if r is not None and math.isfinite(r[0]):
                out[field] = float(r[0])
        return out


def _nearest_wet(smp, x, y, tol, wet, scale=None):
    """{"depth", "x", "y", "dist", "max_depth", "n"} of the wet cell (depth
    above `wet`) nearest to (x, y) within `tol` (raster units): the cell
    holding the point when it is wet (distance 0), else the nearest cell
    centre. Also the largest depth among the wet cells within tol. None when
    there is none. 2.18.0: `scale` (kx, ky) multiplies the x / y offsets
    before measuring (a geographic CRS, see _Rasters.at); the search
    window still spans +-tol on both axes, so each k must be <= 1."""
    inv = smp.inv_gt
    pxs, pys = [], []
    for cx_, cy_ in ((x - tol, y - tol), (x - tol, y + tol), (x + tol, y - tol),
                     (x + tol, y + tol)):
        pxs.append(inv[0] + cx_ * inv[1] + cy_ * inv[2])
        pys.append(inv[3] + cx_ * inv[4] + cy_ * inv[5])
    c0 = max(0, int(math.floor(min(pxs))))
    c1 = min(smp.xsize - 1, int(math.floor(max(pxs))))
    r0 = max(0, int(math.floor(min(pys))))
    r1 = min(smp.ysize - 1, int(math.floor(max(pys))))
    if c0 > c1 or r0 > r1:
        return None
    w = c1 - c0 + 1
    step = max(1, MAX_CHUNK_CELLS // w)
    pcol = int(math.floor(inv[0] + x * inv[1] + y * inv[2]))
    prow = int(math.floor(inv[3] + x * inv[4] + y * inv[5]))
    best = None
    max_depth, n = None, 0
    for ry in range(r0, r1 + 1, step):
        h = min(step, r1 - ry + 1)
        arr = smp.window_values(c0, ry, w, h)
        if arr is None:
            continue
        with np.errstate(invalid="ignore"):
            wetm = np.isfinite(arr) & (arr > wet)
        if not wetm.any():
            continue
        cols, rows = np.meshgrid(np.arange(c0, c0 + w), np.arange(ry, ry + h))
        cxs, cys = smp.cell_centres(cols, rows)
        if scale is None:
            d = np.hypot(cxs - x, cys - y)
        else:
            d = np.hypot((cxs - x) * scale[0], (cys - y) * scale[1])
        ok = wetm & (d <= tol)
        # the cell that holds the point counts as being at the point
        inside = wetm & (cols == pcol) & (rows == prow)
        if inside.any():
            d = np.where(inside, 0.0, d)
            ok = ok | inside
        if not ok.any():
            continue
        vals = np.where(ok, arr, -np.inf)
        m = float(vals.max())
        max_depth = m if max_depth is None else max(max_depth, m)
        n += int(ok.sum())
        dd = np.where(ok, d, np.inf)
        i = int(np.argmin(dd))
        cand = (float(dd.flat[i]), float(arr.flat[i]), float(cxs.flat[i]), float(cys.flat[i]))
        if best is None or cand[0] < best[0]:
            best = cand
    if best is None:
        return None
    return {"dist": best[0], "depth": best[1], "x": best[2], "y": best[3],
            "max_depth": max_depth, "n": n}


# -------------------------------------------------------------- the screen --
def class_rank(cls):
    """1 Low, 2 Medium / Significant, 3 High; 0 when unknown."""
    return _RANK.get(str(cls or "").strip().lower(), 0)


def class_text(entry):
    c = entry.get("class") or {}
    bits = []
    if c.get("nd"):
        bits.append("ND {}".format(c["nd"]))
    if c.get("tm11"):
        bits.append("TM 11 {}".format(c["tm11"]))
    return " / ".join(bits) or "class unknown"


def _higher_class(down, up):
    """True when the downstream dam's class is above the upstream dam's in
    ND or TM 11 (both known)."""
    d, u = down.get("class") or {}, up.get("class") or {}
    for key in ("nd", "tm11"):
        rd, ru = class_rank(d.get(key)), class_rank(u.get(key))
        if rd and ru and rd > ru:
            return True
    return False


def _classes_known(down, up):
    d, u = down.get("class") or {}, up.get("class") or {}
    return any(class_rank(d.get(k)) and class_rank(u.get(k)) for k in ("nd", "tm11"))


def _sep_ft(a, b):
    lat0 = (a[1] + b[1]) / 2.0
    return math.hypot((a[0] - b[0]) * math.cos(math.radians(lat0)), a[1] - b[1]) * 364567.2


class SeriesResult(object):
    def __init__(self):
        self.rows = []
        self.dams = []
        self.chains = []
        self.params = {}
        self.notes = []
        self.folder = ""

    def cascade_candidates(self):
        return [r for r in self.rows if r["Cascade_Candidate"] == "YES"]

    def class_order_flags(self):
        return [r for r in self.rows if r["Class_Order_Flag"] == "YES"]

    def rows_for_upstream(self, name):
        return [r for r in self.rows if r["Upstream"] == name]

    def rows_for_downstream(self, name):
        return [r for r in self.rows if r["Downstream"] == name]

    def summary_lines(self, max_rows=12):
        return summary_lines(self, max_rows)


def screen(entries, depth_threshold_ft=DEFAULT_DEPTH_THRESHOLD_FT,
           tolerance_ft=DEFAULT_TOLERANCE_FT, wet_ft=DEFAULT_WET_FT,
           flag_on="nearest", min_separation_ft=DEFAULT_MIN_SEPARATION_FT, nid_layer=None,
           ctx=None, progress=None):
    """Dams-in-series screening of a list of entries (make_entry /
    entries_from_tuples / discover).

    depth_threshold_ft  depth at B ABOVE which B is a cascade candidate
    tolerance_ft        B's point is searched for the nearest wet cell within
                        this distance (parameter; default = the plugin's own
                        DAM_SEARCH_FT for dams in the flood)
    wet_ft              a cell is wet when its depth is above this
    flag_on             "nearest": the depth at the nearest wet cell decides
                        the flag; "max": the largest depth within the tolerance
    min_separation_ft   two dams closer than this are treated as the same dam
    nid_layer           optional NID layer: dams without a point are located
                        by NID id / name (dams_from_layer)
    Returns a SeriesResult (rows, dams, chains, summary_lines())."""
    if flag_on not in ("nearest", "max"):
        raise ValueError("flag_on must be 'nearest' or 'max'")
    ctx = _transform_ctx(ctx)
    res = SeriesResult()
    res.params = {"depth_threshold_ft": float(depth_threshold_ft),
                  "tolerance_ft": float(tolerance_ft), "wet_ft": float(wet_ft),
                  "flag_on": flag_on, "min_separation_ft": float(min_separation_ft)}
    entries = list(entries)
    if nid_layer is not None:
        try:
            recs = dams_from_layer(nid_layer, ctx)
            got = locate_from_nid(entries, recs)
            res.notes.append("{} dam(s) located in the NID layer ({} inventory dam(s))".format(
                got, len(recs)))
        except Exception as e:
            res.notes.append("the NID layer could not be used ({})".format(e))
            log_message("Dams in series: NID layer not used ({})".format(e), MSG_WARNING)
    located = [e for e in entries if e.get("point") is not None]
    n = max(1, len(entries))
    for i, up in enumerate(entries):
        if progress:
            progress(100.0 * i / n, "{}: reading the flood at the other dams".format(up["name"]))
        up["status"] = ""
        if not up.get("depth"):
            up["status"] = "no depth raster"
            continue
        rs = _Rasters(up, tolerance_ft, ctx)
        try:
            if rs.error:
                up["status"] = rs.error
                log_message("Dams in series: {}: {}".format(up["name"], rs.error), MSG_WARNING)
                continue
            up["status"] = "screened"
            for down in located:
                if down is up:
                    continue
                if up.get("point") is not None and _sep_ft(up["point"], down["point"]) < \
                        min_separation_ft:
                    res.notes.append("{} and {} are at the same place ({:.0f} ft apart) - "
                                     "treated as one dam".format(up["name"], down["name"],
                                                                 _sep_ft(up["point"],
                                                                         down["point"])))
                    continue
                got = rs.at(down["point"][0], down["point"][1], tolerance_ft, wet_ft)
                if got is None:
                    continue
                res.rows.append(_row(up, down, got, depth_threshold_ft, flag_on))
        finally:
            rs.close()
    _mark_mutual(res.rows)
    res.rows.sort(key=lambda r: (r["Upstream"].lower(), _arr_key(r), r["Downstream"].lower()))
    res.dams = _dam_rows(entries, res.rows)
    res.chains = build_chains(res.rows, {e["name"]: e for e in entries})
    for e in entries:
        if e.get("point") is None:
            res.notes.append("{}: location unknown{}".format(
                e["name"], " ({})".format(e["location_note"]) if e.get("location_note") else ""))
    if progress:
        progress(100.0, "dams in series: done")
    return res


def screen_folder(parent, nid_layer=None, ctx=None, **kw):
    """discover() the dam folders of `parent` and screen() them. Extra
    keywords go to screen(). The result's `folder` is `parent`."""
    entries = discover(parent, ctx)
    res = screen(entries, nid_layer=nid_layer, ctx=ctx, **kw)
    res.folder = parent
    res.entries = entries
    return res


def _fmt(v, nd=2):
    return "" if v is None else "{:.{}f}".format(v, nd)


def _row(up, down, got, threshold, flag_on):
    depth = got["depth"]
    basis = got["max_depth"] if flag_on == "max" and got["max_depth"] is not None else depth
    cascade = basis is not None and basis > threshold
    order = _higher_class(down, up)
    known = _classes_known(down, up)
    flags, msgs = [], []
    if cascade:
        flags.append("CASCADE CANDIDATE")
        msgs.append("depth at {b} is {d:.1f} ft (above {t:g} ft) in {a}'s breach flood: {b} may "
                    "fail or be overtopped by {a}'s release, so {a}'s classification should "
                    "consider {b}'s downstream consequences".format(
                        b=down["name"], a=up["name"], d=basis, t=threshold))
    if order:
        flags.append("CLASS ORDER")
        msgs.append("the downstream dam's hazard exceeds the upstream dam's ({b}: {bc}; {a}: "
                    "{ac}): check whether {a}'s incremental consequences must include {b}'s "
                    "failure".format(b=down["name"], a=up["name"], bc=class_text(down),
                                     ac=class_text(up)))
    if got["arrival"] is None and up.get("arrival"):
        msgs.append("no arrival time at this point")
    elif not up.get("arrival"):
        msgs.append("no arrival raster for {}".format(up["name"]))
    if not msgs:
        msgs.append("{} is inside {}'s breach flood; depth below the screening threshold".format(
            down["name"], up["name"]))
    return {"Upstream": up["name"], "Downstream": down["name"], "Depth_ft": _fmt(depth),
            "First_Arrival_hr": _fmt(got["arrival"]), "Velocity_fps": _fmt(got["velocity"]),
            "Max_Depth_Within_Tol_ft": _fmt(got["max_depth"]),
            "Sample_Distance_ft": _fmt(got["dist_ft"], 0),
            "Upstream_ND": (up.get("class") or {}).get("nd", ""),
            "Upstream_TM11": (up.get("class") or {}).get("tm11", ""),
            "Downstream_ND": (down.get("class") or {}).get("nd", ""),
            "Downstream_TM11": (down.get("class") or {}).get("tm11", ""),
            "Cascade_Candidate": "YES" if cascade else "no",
            "Class_Order_Flag": "YES" if order else ("no" if known else "unknown"),
            "Flags": "; ".join(flags), "Message": ". ".join(msgs),
            "Upstream_Folder": up.get("folder", ""), "Downstream_Folder": down.get("folder", "")}


def _mark_mutual(rows):
    pairs = {(r["Upstream"], r["Downstream"]): r for r in rows}
    for (a, b), r in pairs.items():
        if (b, a) in pairs:
            r["Flags"] = "; ".join(x for x in (r["Flags"], "MUTUAL") if x)
            r["Message"] += (". {} also lies in {}'s flood (backwater or overlapping floods): "
                             "the direction of flow between them is not settled by this "
                             "table".format(a, b))


def _arr_key(r):
    try:
        return (0, float(r["First_Arrival_hr"]))
    except (TypeError, ValueError):
        return (1, 0.0)


def _dam_rows(entries, rows):
    out = []
    for e in entries:
        floods = [r["Downstream"] for r in rows if r["Upstream"] == e["name"]]
        by = [r["Upstream"] for r in rows if r["Downstream"] == e["name"]]
        pt = e.get("point")
        status = e.get("status") or ""
        if pt is None:
            status = (status + "; " if status else "") + "location unknown"
        out.append({"Dam": e["name"], "Folder": e.get("folder", ""),
                    "Location": "{:.6f}, {:.6f}".format(pt[1], pt[0]) if pt else "location unknown",
                    "Location_Source": e.get("location_source", ""),
                    "Depth_Raster": os.path.basename(str(e["depth"])) if isinstance(
                        e.get("depth"), str) else ("(layer)" if e.get("depth") else ""),
                    "Arrival_Raster": os.path.basename(str(e["arrival"])) if isinstance(
                        e.get("arrival"), str) else ("(layer)" if e.get("arrival") else ""),
                    "Velocity_Raster": os.path.basename(str(e["velocity"])) if isinstance(
                        e.get("velocity"), str) else ("(layer)" if e.get("velocity") else ""),
                    "ND": (e.get("class") or {}).get("nd", ""),
                    "TM11": (e.get("class") or {}).get("tm11", ""), "Status": status,
                    "Floods": "; ".join(floods), "Flooded_By": "; ".join(by)})
    return out


# ---------------------------------------------------------------- chains ---
def build_chains(rows, entries=None):
    """Chains A -> B -> C in order of arrival time. For every upstream-most
    dam (one that no other dam's flood reaches) the dams its release reaches,
    directly or through other dams, listed by arrival time after ITS breach
    (a dam reached only through another dam: the arrival times add up, marked
    approximate). Pairs that lie in each other's flood (MUTUAL) are left out.
    Returns [{"root", "steps": [{"dam", "from", "depth_ft", "arrival_hr",
    "approx", "cascade"}], "text"}]."""
    entries = entries or {}
    edges = {}
    for r in rows:
        if "MUTUAL" in r["Flags"]:
            continue
        edges.setdefault(r["Upstream"], []).append(r)
    downstream = {r["Downstream"] for rs in edges.values() for r in rs}
    roots = sorted((u for u in edges if u not in downstream), key=str.lower)
    chains = []
    for root in roots:
        direct = {}
        for r in edges[root]:
            direct[r["Downstream"]] = r
        # arrival after the root's breach: direct reading, else summed through parents
        cum = {root: 0.0}
        steps = {}
        for name, r in direct.items():
            a = _num(r["First_Arrival_hr"])
            steps[name] = {"dam": name, "from": root, "depth_ft": _num(r["Depth_ft"]),
                           "arrival_hr": a, "approx": False,
                           "cascade": r["Cascade_Candidate"] == "YES"}
        frontier = list(direct)
        seen = {root} | set(direct)
        while frontier:
            nxt = []
            for u in frontier:
                base = steps[u]["arrival_hr"]
                for r in edges.get(u, ()):
                    v = r["Downstream"]
                    if v in seen:
                        continue
                    a = _num(r["First_Arrival_hr"])
                    tot = base + a if base is not None and a is not None else None
                    steps[v] = {"dam": v, "from": u, "depth_ft": _num(r["Depth_ft"]),
                                "arrival_hr": tot, "approx": True,
                                "cascade": r["Cascade_Candidate"] == "YES"}
                    seen.add(v)
                    nxt.append(v)
            frontier = nxt
        order = sorted(steps.values(), key=lambda s: (s["arrival_hr"] is None,
                                                      s["arrival_hr"] or 0.0, s["dam"].lower()))
        parts = ["{} (breach)".format(root)]
        for s in order:
            bits = []
            if s["depth_ft"] is not None:
                bits.append("{:.1f} ft".format(s["depth_ft"]))
            if s["arrival_hr"] is not None:
                bits.append("{}{:.1f} h".format("~" if s["approx"] else "", s["arrival_hr"]))
            if s["cascade"]:
                bits.append("CASCADE CANDIDATE")
            if s["from"] != root:
                bits.append("via {}".format(s["from"]))
            parts.append("{}{}".format(s["dam"], " [{}]".format(", ".join(bits)) if bits else ""))
        chains.append({"root": root, "steps": order, "text": " -> ".join(parts)})
    return chains


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------- text ---
def summary_lines(res, max_rows=12):
    """Text lines for the dialog / log / report summary."""
    p = res.params
    lines = ["Dams in series - SCREENING TABLE (no combined or cascading breach is computed): "
             "each upstream dam's own breach flood is read at every other dam's point "
             "(nearest wet cell within {:g} ft; cascade candidate: depth above {:g} ft, an "
             "engineer-chosen screening value)".format(p.get("tolerance_ft", 0),
                                                       p.get("depth_threshold_ft", 0))]
    read = [d for d in res.dams if d["Status"].startswith("screened")]
    unk = [d["Dam"] for d in res.dams if d["Location"] == "location unknown"]
    nor = [d["Dam"] for d in res.dams if d["Status"].split(";")[0] not in ("screened", "")]
    lines.append("{} dam(s): {} with a readable depth raster, {} with a known location".format(
        len(res.dams), len(read), len(res.dams) - len(unk)))
    if not res.dams:
        lines.append("no dam folders found")
        return lines
    rows = res.rows
    casc, order = res.cascade_candidates(), res.class_order_flags()
    lines.append("{} pair(s) with the downstream dam inside the upstream dam's flood; {} "
                 "cascade candidate(s), {} class-order flag(s)".format(
                     len(rows), len(casc), len(order)))
    if not rows and read:
        lines.append("no dam lies in another dam's breach flood (within the tolerance)")
    for r in casc[:max_rows]:
        lines.append("CASCADE CANDIDATE: {} -> {}: {} ft at {}{} ({} ND / {} TM 11)".format(
            r["Upstream"], r["Downstream"], r["Depth_ft"], r["Downstream"],
            ", arrival {} h".format(r["First_Arrival_hr"]) if r["First_Arrival_hr"] else "",
            r["Downstream_ND"] or "?", r["Downstream_TM11"] or "?"))
    for r in order[:max_rows]:
        lines.append("CLASS ORDER: {} -> {}: the downstream dam's hazard exceeds the upstream "
                     "dam's - check whether {}'s incremental consequences must include {}'s "
                     "failure".format(r["Upstream"], r["Downstream"], r["Upstream"],
                                      r["Downstream"]))
    if len(casc) > max_rows or len(order) > max_rows:
        lines.append("... more in {}".format(CSV_FILE))
    for c in res.chains:
        lines.append("Chain: " + c["text"])
    if unk:
        lines.append("Location unknown (not tested as a downstream dam): {}".format(
            ", ".join(unk)))
    if nor:
        lines.append("No usable depth raster (not screened as an upstream dam): {}".format(
            ", ".join(nor)))
    return lines


# ---------------------------------------------------------------- writers ---
def _write_csv(path, fields, rows):
    path = prepare_output_path(path)
    tmp = path + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    os.replace(tmp, path)
    return path


def write_csv(res, folder):
    """Dams_In_Series_Screen.csv (the pairs) and Dams_In_Series_Screen_Dams.csv (one row
    per dam: location, rasters, status, who it floods / is flooded by) in
    `folder`. Returns {"csv", "dams_csv"}."""
    os.makedirs(folder, exist_ok=True)
    return {"csv": _write_csv(os.path.join(folder, CSV_FILE), SERIES_FIELDS, res.rows),
            "dams_csv": _write_csv(os.path.join(folder, DAMS_CSV_FILE), DAM_FIELDS, res.dams)}


def _html(res, title="Dams in series", rows=None, dams=None, chains=None):
    rows = res.rows if rows is None else rows
    dams = res.dams if dams is None else dams
    chains = res.chains if chains is None else chains
    esc = html.escape
    p = res.params
    cols = ["Upstream", "Downstream", "Depth_ft", "First_Arrival_hr", "Velocity_fps",
            "Sample_Distance_ft", "Upstream_ND", "Upstream_TM11", "Downstream_ND",
            "Downstream_TM11", "Flags", "Message"]
    out = ["<!DOCTYPE html><html><head><meta charset='utf-8'><title>{}</title><style>"
           "body{{font-family:sans-serif;margin:16px}}table{{border-collapse:collapse;"
           "margin-bottom:16px}}td,th{{border:1px solid #bbb;padding:3px 8px;font-size:13px;"
           "vertical-align:top}}th{{background:#eee}}.warn{{background:#ffe0b2}}"
           ".hot{{background:#ffcdd2}}.note{{background:#fff8e1;border:1px solid #f9a825;"
           "padding:6px 10px}}</style></head><body>".format(esc(title)),
           "<h2>{}</h2>".format(esc(title)),
           "<p class='note'><b>{}</b></p>".format(esc(DISCLAIMER)),
           "<p>Screening values (chosen by the engineer): cascade candidate when the depth is "
           "above {:g} ft; nearest wet cell searched within {:g} ft of the downstream dam's "
           "point; depth flagged on the {} value. {} v{}, {}.</p>".format(
               p.get("depth_threshold_ft", 0), p.get("tolerance_ft", 0),
               "largest within the tolerance" if p.get("flag_on") == "max" else "nearest wet cell",
               esc(PLUGIN_NAME), esc(PLUGIN_VERSION), time.strftime("%Y-%m-%d %H:%M"))]
    out.append("<p>{}</p>".format("<br>".join(esc(x) for x in summary_lines(res)[1:3])))
    if chains:
        out.append("<h3>Chains (by arrival time after the upstream dam's breach)</h3><ul>")
        out += ["<li>{}</li>".format(esc(c["text"])) for c in chains]
        out.append("</ul>")
    out.append("<h3>Pairs: downstream dam inside the upstream dam's breach flood</h3>")
    if rows:
        out.append("<table><tr>" + "".join("<th>{}</th>".format(esc(c)) for c in cols) + "</tr>")
        for r in rows:
            cls = " class='hot'" if r["Cascade_Candidate"] == "YES" else \
                " class='warn'" if r["Class_Order_Flag"] == "YES" else ""
            out.append("<tr{}>".format(cls) + "".join(
                "<td>{}</td>".format(esc(str(r.get(c, "")))) for c in cols) + "</tr>")
        out.append("</table>")
    else:
        out.append("<p>No dam lies in another dam's breach flood.</p>")
    out.append("<h3>Dams</h3><table><tr>" + "".join(
        "<th>{}</th>".format(esc(c)) for c in ("Dam", "Location", "Location_Source", "ND",
                                               "TM11", "Status", "Floods", "Flooded_By"))
               + "</tr>")
    for d in dams:
        out.append("<tr>" + "".join("<td>{}</td>".format(esc(str(d.get(c, "")))) for c in (
            "Dam", "Location", "Location_Source", "ND", "TM11", "Status", "Floods",
            "Flooded_By")) + "</tr>")
    out.append("</table></body></html>")
    return "\n".join(out)


def write_html(res, folder, filename=HTML_FILE):
    os.makedirs(folder, exist_ok=True)
    path = prepare_output_path(os.path.join(folder, filename))
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(_html(res))
    return path


def write_reports(res, folder):
    """Dams_In_Series_Screen.csv, Dams_In_Series_Screen_Dams.csv and Dams_In_Series_Screen.html in
    `folder`. Returns {"csv", "dams_csv", "html"}."""
    out = write_csv(res, folder)
    out["html"] = write_html(res, folder)
    return out


def qa_folder_of(dam_folder):
    return os.path.join(dam_folder, OUTPUT_FOLDER_NAME, SUBDIR_QA)


def write_qa(res, also_downstream=False):
    """Into each dam's own QA folder (<dam folder>/outputs/QA/): Dams_In_Series_Screen.csv
    and .html with the rows where THAT dam is the upstream dam (an empty
    table when no dam is in its flood - the screening was made). With
    also_downstream the rows where the dam is the downstream dam are written
    as Dams_Upstream_Of_This_Dam.csv. Only dams that were screened (a depth
    raster was read) and have a folder get files. Returns {dam name: paths}."""
    out = {}
    for d in res.dams:
        folder = d.get("Folder")
        if not folder or not d["Status"].startswith("screened"):
            continue
        qa = qa_folder_of(folder)
        mine = res.rows_for_upstream(d["Dam"])
        paths = {"csv": _write_csv_in(qa, CSV_FILE, SERIES_FIELDS, mine)}
        chains = [c for c in res.chains if c["root"] == d["Dam"]]
        sub = SeriesResult()
        sub.params = res.params
        paths["html"] = _write_html_sub(sub, qa, d["Dam"], mine, chains, res)
        if also_downstream:
            paths["upstream_csv"] = _write_csv_in(
                qa, UPSTREAM_CSV_FILE, SERIES_FIELDS, res.rows_for_downstream(d["Dam"]))
        out[d["Dam"]] = paths
    return out


def _write_csv_in(folder, name, fields, rows):
    os.makedirs(folder, exist_ok=True)
    return _write_csv(os.path.join(folder, name), fields, rows)


def _write_html_sub(sub, folder, dam, rows, chains, res):
    sub.rows, sub.dams, sub.chains = rows, [d for d in res.dams if d["Dam"] == dam], chains
    os.makedirs(folder, exist_ok=True)
    path = prepare_output_path(os.path.join(folder, HTML_FILE))
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(_html(sub, "Dams in series - {}".format(dam), rows, sub.dams, chains))
    return path
