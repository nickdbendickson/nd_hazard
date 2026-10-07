# -*- coding: utf-8 -*-
"""v2.10 rules comparison: ND Dam Hazard's structure results re-classified with
the rules of the other two tools (ND Dam Breach Runner / ND Dam Breach
Screening, core/nd_hazard.py - byte-identical in both), so the engineer sees
where and WHY the screening previews and the classification authority differ.

For each structure that either rule set counts as habitable and flooded:

* ND Dam Hazard's values as the run left them: maximum depth and velocity over
  the outline grown by the 5-ft ring (or a point's disc), the ACER zone from
  the worst cell (depth and velocity paired cell by cell, both curves), the
  habitability / ACER type from its inventory rules and the ND outcome.
* nd_hazard's values: the same depth / velocity rasters sampled with
  nd_hazard.sample_footprint (deepest cell whose centre is inside the outline,
  maximum velocity over its wet cells) or sample_point (a point structure),
  habitability and mobile home from the occupancy text
  (nd_hazard.habitable_from_text / mobile_from_text: blank or unknown =
  habitable), and nd_hazard.structure_finding with
    - "curves" (default): ND Dam Hazard's own TM 11 curves exported as
      nd_hazard polygons (chartsign.charts_from_curves) - so only sampling,
      habitability and the rules around the chart differ. This is a
      comparison device, not a signed-off chart (the table says so);
    - "conservative": nd_hazard's no-chart method (every flooded habitable
      structure potentially lethal);
    - a chart file path: an engineer's signed-off chart file (must load as
      ready).
* Whether the two differ (structure level or ACER zone) and why:
  "sampling area" (depth / velocity or wet / dry differ), "habitability",
  "mobile home type", "chart / method" (same values, other zone: ND pairs
  depth and velocity per cell, nd_hazard pairs the maximum depth with the
  maximum velocity; velocity beyond the chart; nd_hazard's digitizing
  tolerance band; ND's near-threshold option; the conservative method) and
  "judgment zone rule" (ND counts the judgment zone as probable loss of life
  only when its setting says so; nd_hazard always does, 5.3).
* The dam class from each (nd_hazard.classify on the compared structures and,
  when available, ND's overtopped roads / railroads re-classified with
  nd_hazard.road_class / rail_class from ND's own depths and AADT / train
  data) and a one-line verdict.

Output: <outputs>/QA/Rules_Comparison.csv and .html. Two entry points:
compare_result(res) with the pipeline result dict (the dialog) and
compare_folder(outputs_folder) from the files a run wrote (Processing). Not
yet run in a real QGIS.
"""

import csv
import datetime
import html
import json
import math
import os
import struct

import numpy as np

from .constants import (
    PLUGIN_NAME, PLUGIN_VERSION, SUBDIR_QA, SUBDIR_CLASSIFICATION, CLASS_HIGH, CLASS_JUDGMENT,
    CLASS_LOW, OSM_EDGE_BUFFER_FT, CURVE_V_MAX,
)
from . import chartsign as CS

METHOD_CURVES = "curves"
METHOD_CONSERVATIVE = "conservative"
METHODS = (METHOD_CURVES, METHOD_CONSERVATIVE)
METHOD_LABELS = {
    METHOD_CURVES: "nd_hazard chart method with ND Dam Hazard's TM 11 curves as polygons "
                   "(comparison only - not a signed-off chart)",
    METHOD_CONSERVATIVE: "nd_hazard conservative method (no chart: every flooded habitable "
                         "structure potentially lethal)",
}
CSV_NAME, HTML_NAME = "Rules_Comparison.csv", "Rules_Comparison.html"
ROADS_CSV_NAME = "Rules_Comparison_Roads.csv"

CAUSE_SAMPLING = "sampling area"
CAUSE_HABITABILITY = "habitability"
CAUSE_MOBILE = "mobile home type"
CAUSE_METHOD = "chart / method"
CAUSE_JUDGMENT = "judgment zone rule"
CAUSE_OTHER = "other"
CAUSES = (CAUSE_SAMPLING, CAUSE_HABITABILITY, CAUSE_MOBILE, CAUSE_METHOD, CAUSE_JUDGMENT)

VALUE_TOL = 0.01              # ft and ft/s: smaller differences are rounding
ZONE_NAME = {CLASS_HIGH: CS.ZONE_HIGH, CLASS_JUDGMENT: CS.ZONE_JUDGMENT, CLASS_LOW: CS.ZONE_LOW}
_OUT_PROBABLE = "Probable loss of life"
_OUT_JUDGMENT = "Judgment zone"
_OUT_ECONOMIC = "Inundated (economic loss)"
_OUT_DRY = "Not inundated"
_RANK = {"": -1, "Low": 0, "Medium": 1, "High": 2}

COLUMNS = ["Struct_ID", "Address", "ND_Habitable", "ND_ACER_Type", "ND_Depth_ft", "ND_Vel_fps",
           "ND_Zone", "ND_Outcome", "ND_Level", "NH_Habitable", "NH_Mobile", "NH_Depth_ft",
           "NH_Vel_fps", "NH_Sampling", "NH_Zone", "NH_Level", "NH_Reason",
           "NH_Zone_at_ND_values", "Differs", "Causes", "Detail"]
ROAD_COLUMNS = ["Kind", "ID", "Depth_ft", "ND_Class", "ND_Basis", "NH_Class", "NH_Reason",
                "Differs", "Cause"]


class _CurveCharts(object):
    """ChartSet stand-in for the comparison: ND Dam Hazard's curves as nd_hazard
    polygons. It is NOT a digitized-and-checked chart set and says so."""
    ready = True
    status = "comparison only"

    def __init__(self, nh, charts):
        self.charts = {}
        for key, c in charts.items():
            self.charts[key] = nh.DangerChart(
                key=key, title=c["title"], figure=c["figure"], v_max_fts=float(c["v_max_fts"]),
                d_max_ft=float(c["d_max_ft"]), high=[tuple(p) for p in c["high"]],
                judgment=[tuple(p) for p in c["judgment"]],
                tolerance=float(c.get("tolerance", CS.DEFAULT_TOLERANCE))).validate()
        self.path, self.sha256 = "", ""

    def chart(self, mobile):
        return self.charts[CS.MOBILE if mobile else CS.HOUSE]

    def info(self):
        return {"status": "comparison only: ND Dam Hazard's REG_05.2026 curve table as "
                          "polygons, not a signed-off chart file", "source": CS.CURVE_ORIGIN.format(
                              step=CS.DEFAULT_STEP_FTS)[:200],
                "digitized_from": "ND Dam Hazard constants.HAZARD_COEFFICIENTS "
                                  "(damsuite.tm11_curves table)",
                "digitized_by": "", "digitized_on": "", "checked_by": "", "file": "",
                "sha256": ""}


# ----------------------------------------------------------------- geometry --
def _wkb_bytes(geom):
    wkb = geom.asWkb()
    if isinstance(wkb, (bytes, bytearray)):
        return bytes(wkb)
    if hasattr(wkb, "data"):
        return bytes(wkb.data())
    return bytes(wkb)


def wkb_parts(b):
    """[("point", (x, y)) | ("polygon", [ring, ...])] of a WKB geometry (ISO or
    EWKB, 2D / Z / M / ZM, either byte order). Raises ValueError on other types."""
    out = []

    def read(o):
        bo = "<" if b[o] == 1 else ">"
        t = struct.unpack_from(bo + "I", b, o + 1)[0]
        o += 5
        dims = 2
        if t & 0x80000000:
            dims += 1
        if t & 0x40000000:
            dims += 1
        if t & 0x20000000:                     # EWKB SRID
            o += 4
        t &= 0x0FFFFFFF
        if t >= 3000:
            dims, t = 4, t - 3000
        elif t >= 2000:
            dims, t = max(dims, 3), t - 2000
        elif t >= 1000:
            dims, t = max(dims, 3), t - 1000

        def pts(o, n):
            ps = []
            for _i in range(n):
                x, y = struct.unpack_from(bo + "dd", b, o)
                ps.append((x, y))
                o += 8 * dims
            return ps, o
        if t == 1:
            (p,), o = pts(o, 1)
            out.append(("point", p))
        elif t == 3:
            nr = struct.unpack_from(bo + "I", b, o)[0]
            o += 4
            rings = []
            for _r in range(nr):
                n = struct.unpack_from(bo + "I", b, o)[0]
                ring, o = pts(o + 4, n)
                rings.append(ring)
            out.append(("polygon", rings))
        elif t in (4, 6, 7):
            n = struct.unpack_from(bo + "I", b, o)[0]
            o += 4
            for _i in range(n):
                o = read(o)
        else:
            raise ValueError("geometry type {} not supported".format(t))
        return o
    read(0)
    return out


class _Parts(object):
    """Pre-split geometry parts (see _parts_of), e.g. moved into the raster CRS."""

    def __init__(self, parts):
        self.parts = parts

    def isNull(self):
        return not self.parts


def _parts_of(geom):
    if isinstance(geom, _Parts):
        return geom.parts
    try:
        if geom is None or geom.isNull():
            return []
        return wkb_parts(_wkb_bytes(geom))
    except Exception:
        try:
            c = geom.centroid().asPoint()
            return [("point", (c.x(), c.y()))]
        except Exception:
            return []


# ------------------------------------------------------------------- raster --
class Raster(object):
    """One band of a north-up raster read window by window (osgeo.gdal).
    No-data, NaN and values <= -9999 read as NaN; negative values too
    (`negative_nan`)."""

    def __init__(self, path, negative_nan=True):
        from osgeo import gdal
        self.path = path
        ds = gdal.Open(path)
        if ds is None:
            raise ValueError("raster could not be opened: {}".format(path))
        self.ds = ds
        gt = ds.GetGeoTransform()
        if abs(gt[2]) > 1e-12 or abs(gt[4]) > 1e-12 or gt[5] >= 0:
            raise ValueError("{}: rotated or south-up rasters are not supported here".format(
                os.path.basename(path)))
        self.x0, self.y_top, self.dx, self.dy = gt[0], gt[3], gt[1], -gt[5]
        self.nx, self.ny = ds.RasterXSize, ds.RasterYSize
        self.band = ds.GetRasterBand(1)
        self.nodata = self.band.GetNoDataValue()
        sc, off = self.band.GetScale(), self.band.GetOffset()
        self.scale = 1.0 if sc in (None, 0) else float(sc)
        self.offset = 0.0 if off is None else float(off)
        self.negative_nan = negative_nan
        try:
            self.wkt = ds.GetProjection() or ""
        except Exception:
            self.wkt = ""

    @property
    def square(self):
        return abs(self.dx - self.dy) <= 1e-9 * max(abs(self.dx), 1.0)

    def _clean(self, a):
        a = np.asarray(a, dtype=np.float64)
        bad = ~np.isfinite(a) | (a <= -9999.0)
        if self.nodata is not None:
            bad |= a == self.nodata
        a = a * self.scale + self.offset
        if self.negative_nan:
            bad |= a < 0
        return np.where(bad, np.nan, a)

    def window(self, xmin, ymin, xmax, ymax, pad=1):
        """(array, x0, y_top) of the cells covering the box (+ pad cells), or
        None when the box is off the raster."""
        c0 = int(math.floor((xmin - self.x0) / self.dx)) - pad
        c1 = int(math.floor((xmax - self.x0) / self.dx)) + pad
        r0 = int(math.floor((self.y_top - ymax) / self.dy)) - pad
        r1 = int(math.floor((self.y_top - ymin) / self.dy)) + pad
        c0, r0 = max(c0, 0), max(r0, 0)
        c1, r1 = min(c1, self.nx - 1), min(r1, self.ny - 1)
        if c1 < c0 or r1 < r0:
            return None
        a = self.band.ReadAsArray(c0, r0, c1 - c0 + 1, r1 - r0 + 1)
        return self._clean(a), self.x0 + c0 * self.dx, self.y_top - r0 * self.dy

    def values_at(self, xs, ys, max_window_cells=None):
        """Nearest-cell values at points (NaN off the raster). v2.12: the
        points are read in windows of at most `max_window_cells` cells
        (constants.READ_BLOCK_TARGET_CELLS) instead of one window over all
        of them - the same values, without the whole raster in memory."""
        xs, ys = np.asarray(xs, float), np.asarray(ys, float)
        out = np.full(xs.shape, np.nan)
        if xs.size == 0:
            return out
        if max_window_cells is None:
            from . import constants as _C
            max_window_cells = int(_C.READ_BLOCK_TARGET_CELLS)
        flat_x, flat_y = xs.ravel(), ys.ravel()
        flat = out.reshape(-1)
        finite = np.isfinite(flat_x) & np.isfinite(flat_y)
        with np.errstate(invalid="ignore"):
            c_all = np.floor(np.where(finite, (flat_x - self.x0) / self.dx, -1.0)).astype(int)
            r_all = np.floor(np.where(finite, (self.y_top - flat_y) / self.dy, -1.0)).astype(int)
        inside = finite & (c_all >= 0) & (c_all < self.nx) & (r_all >= 0) & (r_all < self.ny)
        idx = np.nonzero(inside)[0]
        if idx.size == 0:
            return out
        order = idx[np.lexsort((c_all[idx], r_all[idx]))]
        stack = [order]
        while stack:
            sel = stack.pop()
            r0, r1 = int(r_all[sel].min()), int(r_all[sel].max())
            c0, c1 = int(c_all[sel].min()), int(c_all[sel].max())
            if (c1 - c0 + 1) * (r1 - r0 + 1) > max_window_cells and sel.size > 1:
                half = sel.size // 2
                stack.append(sel[:half])
                stack.append(sel[half:])
                continue
            # one cell of margin, then the cell index relative to the window's own
            # origin as window() / the one-window read computed it
            c0, r0 = max(0, c0 - 1), max(0, r0 - 1)
            c1, r1 = min(self.nx - 1, c1 + 1), min(self.ny - 1, r1 + 1)
            a = self._clean(self.band.ReadAsArray(c0, r0, c1 - c0 + 1, r1 - r0 + 1))
            x0w, ytw = self.x0 + c0 * self.dx, self.y_top - r0 * self.dy
            c = np.floor((flat_x[sel] - x0w) / self.dx).astype(int)
            r = np.floor((ytw - flat_y[sel]) / self.dy).astype(int)
            ok = (r >= 0) & (r < a.shape[0]) & (c >= 0) & (c < a.shape[1])
            flat[sel[ok]] = a[r[ok], c[ok]]
        return out

    def same_grid(self, other):
        return other is not None and abs(self.x0 - other.x0) < 1e-6 * self.dx and \
            abs(self.y_top - other.y_top) < 1e-6 * self.dy and \
            abs(self.dx - other.dx) < 1e-9 * self.dx and abs(self.dy - other.dy) < 1e-9 * self.dy


def sample_structure(nh, parts, depth, vel, wet_ft):
    """nd_hazard sampling of one structure: (depth, velocity, how) - the part
    (polygon or point) with the deepest water. Velocity on another grid than
    the depth is read at the depth cells' centres (nearest)."""
    best = None
    for kind, data in parts:
        if kind == "polygon":
            ring = data[0] if data else []
            if len(ring) < 3:
                continue
            xs, ys = [p[0] for p in ring], [p[1] for p in ring]
            box = (min(xs), min(ys), max(xs), max(ys))
        else:
            ring = None
            box = (data[0], data[1], data[0], data[1])
        w = depth.window(*box, pad=1)
        if w is None:
            continue
        d_arr, x0, yt = w
        if vel is None:
            v_arr = None
        elif vel.same_grid(depth):
            vw = vel.window(*box, pad=1)
            v_arr = vw[0] if vw is not None and vw[0].shape == d_arr.shape else None
        else:
            rr, cc = np.mgrid[0:d_arr.shape[0], 0:d_arr.shape[1]]
            v_arr = vel.values_at(x0 + (cc + 0.5) * depth.dx, yt - (rr + 0.5) * depth.dy)
        if v_arr is None and vel is not None:
            v_arr = np.full(d_arr.shape, np.nan)
        if ring is not None:
            d, v, how = nh.sample_footprint(d_arr, v_arr, x0, yt, depth.dx, ring, wet_ft=wet_ft)
        else:
            d, v = nh.sample_point(d_arr, v_arr if v_arr is not None else d_arr * np.nan,
                                   x0, yt, depth.dx, data[0], data[1])
            how = "cell under the point"
        if d is None:
            continue
        if best is None or d > best[0]:
            best = (d, v, how)
    return best if best is not None else (None, None, "not on the depth raster")


# --------------------------------------------------------------- the rules --
def _nd_level(rec, judge):
    """(level, note) of one structure under ND Dam Hazard's own rules."""
    out = rec.get("outcome") or _OUT_DRY
    if out == _OUT_DRY or rec.get("max_depth") is None:
        return "", "dry"
    hab = rec.get("habitable")
    if hab != "Yes":
        return "Low", ("unverified - not counted, listed for judgment" if hab == "Unverified"
                       else "not habitable")
    if out == _OUT_PROBABLE:
        return "High", "probable loss of life"
    if out == _OUT_JUDGMENT:
        return ("High", "judgment zone counted as probable loss of life") if judge else \
            ("Medium", "judgment zone - engineering judgment (not counted as probable)")
    return "Medium", "inundated: economic loss"


def _texts(rec):
    t = [str(rec.get("occupancy") or ""), str(rec.get("fema_category") or "")]
    for s in rec.get("sources") or ():
        t += [str(x) for x in (s.get("texts") or ())]
    out = []
    for x in t:
        x = x.strip()
        if x and x not in out:
            out.append(x)
    return " ; ".join(out)


def _num(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _fmt(v, nd=2):
    return "" if v is None else "{:.{}f}".format(v, nd)


def compare_records(nh, recs, depth, vel, judge=True, method=METHOD_CURVES, charts=None,
                    wet_ft=0.1, use_nd_types=False):
    """Rows (dicts, COLUMNS keys plus a few private ones) for ND structure
    records. recs: dicts with id, geom, habitable, acer_type, max_depth,
    max_vel, zone, outcome, occupancy / sources, zone_note, address."""
    st = nh.Settings(lethality=nh.LETHALITY_ALL_WET if method == METHOD_CONSERVATIVE
                     else nh.LETHALITY_CHARTS, wet_ft=wet_ft).validate()
    rows, nh_structs = [], []
    skipped = {"dry_both": 0, "uninhabited_both": 0}
    for rec in recs:
        parts = _parts_of(rec.get("geom"))
        d, v, how = sample_structure(nh, parts, depth, vel, wet_ft)
        text = _texts(rec)
        if use_nd_types:
            nh_hab = rec.get("habitable") == "Yes"
            nh_mob = rec.get("acer_type") == "Mobile home"
        else:
            nh_hab = nh.habitable_from_text(text)
            nh_mob = nh.mobile_from_text(text)
        s = nh.Structure(id=str(rec.get("id")), depth_ft=d if d is not None else float("nan"),
                         vel_fts=v, habitable=nh_hab, mobile=nh_mob)
        f = nh.structure_finding(s, st, charts)
        nd_lvl, nd_note = _nd_level(rec, judge)
        nd_wet = nd_lvl != ""
        nh_wet = f is not None
        nd_hab = rec.get("habitable") == "Yes"
        if not nd_wet and not nh_wet:
            skipped["dry_both"] += 1
            continue
        if not nd_hab and not nh_hab:
            skipped["uninhabited_both"] += 1
            if nh_wet:
                nh_structs.append(s)
            continue
        if nh_wet:
            nh_structs.append(s)
        nd_d, nd_v = _num(rec.get("max_depth")), _num(rec.get("max_vel"))
        nd_zone = ZONE_NAME.get(rec.get("zone"), "") if nd_wet else ""
        nd_mob = rec.get("acer_type") == "Mobile home"
        nh_lvl = f.level if f else ""
        nh_zone = f.zone if f else ""
        # nd_hazard's zone for ND Dam Hazard's own values and type (method attribution)
        cross_zone, cross_note = "", ""
        if method == METHOD_CURVES and nd_wet and nd_d is not None and nd_v is not None:
            cross_zone, cross_note = charts.chart(nd_mob).zone(nd_d, nd_v)
        differs = nd_lvl != nh_lvl or (bool(nd_zone) and bool(nh_zone) and nd_zone != nh_zone)
        causes, detail = [], []
        vals_differ = nd_wet != nh_wet or (nd_d is not None and d is not None and (
            abs(nd_d - d) > VALUE_TOL or (nd_v is not None and v is not None and
                                          abs(nd_v - v) > VALUE_TOL)))
        if differs:
            if nd_wet != nh_wet:
                causes.append(CAUSE_SAMPLING)
                detail.append("wet for {} only".format("ND Dam Hazard" if nd_wet else "nd_hazard"))
            if nd_hab != nh_hab and (nd_wet or nh_wet):
                causes.append(CAUSE_HABITABILITY)
                detail.append("ND Dam Hazard: habitable '{}'; nd_hazard: {} (from the "
                              "occupancy text '{}'{})".format(
                                  rec.get("habitable"), "habitable" if nh_hab else "uninhabited",
                                  text[:60], "; blank / unknown counts as habitable"
                                  if nh_hab else ""))
            if nd_hab and nh_hab and nd_wet and nh_wet:
                if nd_mob != nh_mob:
                    causes.append(CAUSE_MOBILE)
                    detail.append("ND Dam Hazard type '{}'; nd_hazard {} (text '{}')".format(
                        rec.get("acer_type"), "mobile home" if nh_mob else "house chart",
                        text[:60]))
                if vals_differ and CAUSE_SAMPLING not in causes:
                    causes.append(CAUSE_SAMPLING)
                    detail.append("ND Dam Hazard {} ft / {} ft/s over the outline + {:g}-ft ring; "
                                  "nd_hazard {} ft / {} ft/s ({})".format(
                                      _fmt(nd_d), _fmt(nd_v), OSM_EDGE_BUFFER_FT, _fmt(d),
                                      _fmt(v), how))
                if method == METHOD_CONSERVATIVE:
                    if nh_lvl == "High" and nd_lvl != "High":
                        causes.append(CAUSE_METHOD)
                        detail.append("nd_hazard's conservative method counts every flooded "
                                      "habitable structure as potentially lethal")
                elif cross_zone and cross_zone != nd_zone and nd_mob == nh_mob:
                    causes.append(CAUSE_METHOD)
                    detail.append(_method_detail(rec, nd_d, nd_v, nd_zone, cross_zone,
                                                 cross_note, nd_mob))
                if nd_zone == CS.ZONE_JUDGMENT and not judge and nh_lvl == "High" and \
                        (nh_zone == CS.ZONE_JUDGMENT or method == METHOD_CONSERVATIVE):
                    causes.append(CAUSE_JUDGMENT)
                    detail.append("ND Dam Hazard's setting leaves the judgment zone to the "
                                  "engineer; nd_hazard always counts it as potentially lethal "
                                  "(5.3 borderline)")
            if not causes:
                causes.append(CAUSE_OTHER)
        addr = rec.get("address")
        if addr is None:
            addr = _address(rec)
        rows.append({
            "Struct_ID": rec.get("id"), "Address": addr or "",
            "ND_Habitable": rec.get("habitable") or "", "ND_ACER_Type": rec.get("acer_type") or "",
            "ND_Depth_ft": _fmt(nd_d), "ND_Vel_fps": _fmt(nd_v), "ND_Zone": nd_zone,
            "ND_Outcome": rec.get("outcome") or "", "ND_Level": nd_lvl or "dry",
            "NH_Habitable": "Yes" if nh_hab else "No", "NH_Mobile": "Yes" if nh_mob else "No",
            "NH_Depth_ft": _fmt(d), "NH_Vel_fps": _fmt(v), "NH_Sampling": how,
            "NH_Zone": nh_zone, "NH_Level": nh_lvl or "dry",
            "NH_Reason": f.reason if f else "dry (below {:g} ft)".format(wet_ft),
            "NH_Zone_at_ND_values": cross_zone, "Differs": "Yes" if differs else "No",
            "Causes": "; ".join(causes), "Detail": " | ".join(detail),
            "_nd_note": nd_note, "_causes": causes})
    rows.sort(key=lambda r: (r["Differs"] != "Yes", -_RANK.get(max(
        r["ND_Level"], r["NH_Level"], key=lambda x: _RANK.get(x, -1)), -1), str(r["Struct_ID"])))
    return rows, nh_structs, skipped


def _method_detail(rec, d, v, nd_zone, cross_zone, cross_note, mobile):
    vmax = CURVE_V_MAX.get("mobile" if mobile else "found")
    if vmax is not None and v is not None and v > vmax:
        return ("velocity {:.1f} ft/s beyond the chart ({:g} ft/s): ND Dam Hazard evaluates the "
                "curve at the axis limit, nd_hazard takes high danger".format(v, vmax))
    if rec.get("zone_note"):
        return "ND Dam Hazard's near-threshold option: zone " + str(rec["zone_note"])
    if cross_note and "tolerance" in cross_note:
        return ("{:.2f} ft at {:.2f} ft/s is within nd_hazard's digitizing tolerance of the {} "
                "zone (5.3: the higher zone); ND Dam Hazard: {}".format(d, v, cross_zone, nd_zone))
    return ("ND Dam Hazard pairs depth and velocity cell by cell (worst cell: {}); nd_hazard "
            "pairs the maximum depth with the maximum velocity ({:.2f} ft, {:.2f} ft/s: {})".format(
                nd_zone, d, v, cross_zone))


def _address(rec):
    try:
        from .structures import address_of
        a, c, z = address_of(rec)
        return ", ".join(x for x in (a, c, z) if x)
    except Exception:
        return ""


def compare_linear(nh, roads=(), rails=(), wet_ft=0.1):
    """Rows and nd_hazard Road / Railroad objects for ND Dam Hazard's overtopped
    road and rail stretches, re-classified with nd_hazard.road_class /
    rail_class from ND's own depth and AADT / train data (so only the rules
    differ; sole access is not re-evaluated)."""
    st = nh.Settings(wet_ft=wet_ft).validate()
    rows, nh_roads, nh_rails = [], [], []
    for r in roads or ():
        depth = _num(r.get("max_depth"))
        rid = str(r.get("name") or "road fid {}".format(r.get("fid")))
        rc = str(r.get("road_class") or "")
        o = nh.Road(id=rid, max_depth_ft=depth if depth is not None else float("nan"),
                    aadt=r.get("aadt"), interstate=rc == "Interstate" or
                    nh.interstate_from_text(r.get("name") or ""), private=rc == "Private")
        wet = depth is not None and depth >= wet_ft
        lvl, why, _gap = nh.road_class(o, st) if wet else ("", "below {:g} ft".format(wet_ft), "")
        if wet:
            nh_roads.append(o)
        nd = r.get("nd_class") or ""
        cause = ""
        if nd != lvl:
            if not wet:
                cause = "wet threshold"
            elif nh.aadt_value(r.get("aadt")) is None:
                cause = "unknown AADT assumption"
            else:
                cause = "road class rule"
        rows.append({"Kind": "road", "ID": rid, "Depth_ft": _fmt(depth), "ND_Class": nd,
                     "ND_Basis": r.get("nd_basis") or "", "NH_Class": lvl, "NH_Reason": why,
                     "Differs": "Yes" if nd != lvl else "No", "Cause": cause})
    for r in rails or ():
        depth = _num(r.get("max_depth"))
        rid = str(r.get("railroad") or r.get("subdivision") or "rail fid {}".format(r.get("fid")))
        o = nh.Railroad(id=rid, max_depth_ft=depth if depth is not None else float("nan"),
                        trains_per_day=r.get("trains"), passenger=bool(r.get("passenger")))
        wet = depth is not None and depth >= wet_ft
        lvl, why, _gap = nh.rail_class(o, st) if wet else ("", "below {:g} ft".format(wet_ft), "")
        if wet:
            nh_rails.append(o)
        nd = r.get("nd_class") or ""
        rows.append({"Kind": "railroad", "ID": rid, "Depth_ft": _fmt(depth), "ND_Class": nd,
                     "ND_Basis": r.get("nd_basis") or "", "NH_Class": lvl, "NH_Reason": why,
                     "Differs": "Yes" if nd != lvl else "No",
                     "Cause": "" if nd == lvl else ("wet threshold" if not wet
                                                    else "railroad rule")})
    return rows, nh_roads, nh_rails


# ------------------------------------------------------------------ driver --
def _charts_for(nh, method, chart_file=None):
    if chart_file:
        cs = nh.load_charts(chart_file)
        if not cs.ready:
            raise ValueError("chart file {} is not digitized and checked (status '{}') - use "
                             "the default comparison with ND Dam Hazard's curves".format(
                                 chart_file, cs.status))
        return cs, "nd_hazard chart method with the chart file {} (status {}, digitized by {}, " \
                   "checked by {})".format(chart_file, cs.status, cs.digitized_by, cs.checked_by)
    if method == METHOD_CONSERVATIVE:
        return None, METHOD_LABELS[method]
    return _CurveCharts(nh, CS.charts_from_curves()), METHOD_LABELS[METHOD_CURVES]


def run_compare(recs, depth_path, vel_path, nd_class="", judge=True, method=METHOD_CURVES,
                chart_file=None, nh_path=None, wet_ft=0.1, roads=(), rails=(),
                out_folder=None, dam_name="", source="", one_d=False, use_nd_types=False,
                transform=None):
    """The comparison for structure records and the rasters; writes the CSV and
    HTML into out_folder (outputs/QA) when given. transform: optional function
    (x, y) -> (x, y) from the structures' CRS to the rasters' CRS."""
    if method not in METHODS:
        raise ValueError("method must be one of {}".format(", ".join(METHODS)))
    nh, info = CS.find_nd_hazard(nh_path)
    if nh is None:
        raise ValueError(info.get("problem") or "nd_hazard.py not found")
    if not depth_path or not os.path.isfile(depth_path):
        raise ValueError("depth raster not found: {}".format(depth_path))
    depth = Raster(depth_path)
    if not depth.square:
        raise ValueError("{}: nd_hazard's sampling needs square cells ({} x {})".format(
            os.path.basename(depth_path), depth.dx, depth.dy))
    vel = Raster(vel_path) if vel_path and os.path.isfile(vel_path) else None
    charts, method_text = _charts_for(nh, method, chart_file)
    if transform is not None:
        recs = [_moved(r, transform) for r in recs]
    rows, nh_structs, skipped = compare_records(nh, recs, depth, vel, judge, method, charts,
                                                wet_ft, use_nd_types)
    lin_rows, nh_roads, nh_rails = compare_linear(nh, roads, rails, wet_ft)
    st = nh.Settings(lethality=nh.LETHALITY_ALL_WET if method == METHOD_CONSERVATIVE and
                     not chart_file else nh.LETHALITY_CHARTS, wet_ft=wet_ft).validate()
    cls_s = nh.classify(structures=nh_structs, settings=st, charts=charts)
    cls_all = nh.classify(structures=nh_structs, roads=nh_roads, railroads=nh_rails, settings=st,
                          charts=charts) if (nh_roads or nh_rails) else cls_s
    nd_struct = max((r["ND_Level"] for r in rows if r["ND_Level"] != "dry"),
                    key=lambda x: _RANK.get(x, -1), default="Low")
    counts = {c: sum(1 for r in rows if c in r["_causes"]) for c in CAUSES + (CAUSE_OTHER,)}
    n_diff = sum(1 for r in rows if r["Differs"] == "Yes")
    verdict = _verdict(nd_class, cls_all.hazard_class, nd_struct, cls_s.hazard_class, n_diff,
                       len(rows), counts, rows, bool(roads or rails))
    notes = [
        "nd_hazard.py: {} (module {}, SHA-256 {}){}.".format(
            info["file"], info["module_version"], info["sha256"][:16],
            "" if info.get("identical_copies") in (None, True)
            else " - WARNING: the installed copies of nd_hazard.py are not identical"),
        "Method: " + method_text + ".",
        "ND Dam Hazard samples the outline grown by a {:g}-ft ring (a point structure: its "
        "assumed disc) and zones every cell with its own depth and velocity; nd_hazard samples "
        "the cells whose centre is inside the outline (a point: the cell under it) and pairs "
        "the maximum depth with the maximum velocity.".format(OSM_EDGE_BUFFER_FT),
        "nd_hazard habitability and mobile home type come from the occupancy text only "
        "(blank / unknown = habitable); ND Dam Hazard uses its inventory rules, review "
        "decisions and footprint size.",
        "ND Dam Hazard counted the judgment zone as probable loss of life: {}.".format(
            "yes" if judge else "no"),
        "'Any depth' for nd_hazard: {:g} ft or more.".format(wet_ft),
        "Structures dry for both: {}; flooded but uninhabited for both: {} (not listed).".format(
            skipped["dry_both"], skipped["uninhabited_both"]),
    ]
    if one_d:
        notes.append("The ND Dam Hazard run used the 1D (depth > 2 ft) criterion; this "
                     "comparison uses nd_hazard's 2D methods - expect differences.")
    if roads or rails:
        notes.append("Roads / railroads: ND Dam Hazard's overtopped stretches with its own "
                     "depths, AADT and train data, re-classified with nd_hazard.road_class / "
                     "rail_class (sole access not re-evaluated).")
    else:
        notes.append("Roads / railroads: none in the ND Dam Hazard result (or not available in "
                     "file mode) - structures only.")
    if nd_class:
        notes.append("ND Dam Hazard's dam class also covers criteria not re-evaluated here "
                     "(lifelines, dams in series, extra criteria).")
    out = {"rows": rows, "road_rows": lin_rows, "nd_class": nd_class,
           "nh_class": cls_all.hazard_class, "nh_class_structures": cls_s.hazard_class,
           "nd_class_structures": nd_struct, "verdict": verdict, "counts": counts,
           "differ": n_diff, "notes": notes, "method": method, "method_text": method_text,
           "nd_hazard": info, "skipped": skipped, "classification": cls_all,
           "dam_name": dam_name, "source": source, "csv": None, "html": None,
           "roads_csv": None}
    if out_folder:
        write_outputs(out, out_folder)
    return out


def _moved(rec, fn):
    """A shallow copy of rec whose geometry is replaced by parts in the raster CRS."""
    parts = []
    for kind, data in _parts_of(rec.get("geom")):
        if kind == "point":
            parts.append(("point", fn(*data)))
        else:
            parts.append(("polygon", [[fn(x, y) for x, y in ring] for ring in data]))
    r = dict(rec)
    r["geom"] = _Parts(parts)
    return r


def _verdict(nd_cls, nh_cls, nd_struct, nh_struct, n_diff, n, counts, rows, linear):
    why = ", ".join("{} {}".format(counts[c], c) for c in CAUSES + (CAUSE_OTHER,)
                    if counts.get(c))
    diff_txt = "{} of {} compared structure(s) differ ({})".format(n_diff, n, why) if n_diff \
        else "no structure differs"
    base = nd_cls or nd_struct
    if base and base == nh_cls:
        return "Same dam class from both rule sets ({}); {}.".format(base, diff_txt)
    if not base:
        return "nd_hazard rules give {}; ND Dam Hazard's class not available; {}.".format(
            nh_cls, diff_txt)
    gov = [r for r in rows if r["Differs"] == "Yes" and
           (r["ND_Level"] == max(nd_struct, nh_struct, key=lambda x: _RANK.get(x, -1)) or
            r["NH_Level"] == max(nd_struct, nh_struct, key=lambda x: _RANK.get(x, -1)))]
    gc = sorted({c for r in gov for c in r["_causes"]})
    tail = ""
    if nd_struct == nh_struct and nd_cls and nd_cls != nh_cls:
        tail = " The structures agree ({}); the difference comes from criteria outside the " \
               "structures{}.".format(nd_struct, " (roads / railroads)" if linear else "")
    return "Dam class differs: ND Dam Hazard {}, nd_hazard rules {}; {}{}.{}".format(
        base, nh_cls, diff_txt, "; governing cause(s): " + ", ".join(gc) if gc else "", tail)


# ------------------------------------------------------------------ output --
def write_outputs(out, out_folder):
    """Rules_Comparison.csv (the structure table only, one header row, so it
    opens as a table in QGIS / Excel), Rules_Comparison_Roads.csv when roads /
    railroads were compared, and Rules_Comparison.html (verdict, classes,
    tables, notes)."""
    os.makedirs(out_folder, exist_ok=True)
    p_csv = os.path.join(out_folder, CSV_NAME)
    p_html = os.path.join(out_folder, HTML_NAME)
    p_roads = os.path.join(out_folder, ROADS_CSV_NAME)
    _write_csv(p_csv, COLUMNS, out["rows"])
    if out["road_rows"]:
        _write_csv(p_roads, ROAD_COLUMNS, out["road_rows"])
    elif os.path.isfile(p_roads):
        os.remove(p_roads)                     # never leave a previous run's table
        p_roads = None
    else:
        p_roads = None
    with open(p_html + ".tmp", "w", encoding="utf-8") as fh:
        fh.write(render_html(out))
    os.replace(p_html + ".tmp", p_html)
    out["csv"], out["html"], out["roads_csv"] = p_csv, p_html, p_roads
    return p_csv, p_html


def _write_csv(path, columns, rows):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(columns)
        for r in rows:
            w.writerow([r.get(c, "") for c in columns])
    os.replace(tmp, path)


def _e(v):
    return html.escape("" if v is None else str(v), quote=True)


def render_html(out):
    t = ["<!doctype html>", '<html lang="en"><head><meta charset="utf-8">',
         '<meta name="viewport" content="width=device-width, initial-scale=1">',
         "<title>Rules comparison</title>",
         "<style>body{font-family:Helvetica,Arial,sans-serif;font-size:13px;margin:16px;"
         "color:#111;background:#fff}table{border-collapse:collapse;margin:8px 0 18px}"
         "th,td{border:1px solid #bbb;padding:3px 6px;vertical-align:top;text-align:left}"
         "th{background:#eee}tr.d td{background:#fff4e0}.v{font-size:15px;font-weight:bold;"
         "margin:10px 0}.wrap{overflow-x:auto}</style></head><body>",
         "<h1>Rules comparison: ND Dam Hazard vs nd_hazard (Breach Runner / Screening)</h1>",
         "<p>{}{} - written {} by {} {}.</p>".format(
             _e(out.get("dam_name") or ""), " (" + _e(out["source"]) + ")"
             if out.get("source") else "", _e(datetime.datetime.now().isoformat(
                 timespec="seconds")), _e(PLUGIN_NAME), _e(PLUGIN_VERSION)),
         '<p class="v">{}</p>'.format(_e(out["verdict"])),
         "<table><tr><th></th><th>ND Dam Hazard</th><th>nd_hazard rules</th></tr>",
         "<tr><td>Dam class</td><td>{}</td><td>{}</td></tr>".format(
             _e(out["nd_class"] or "-"), _e(out["nh_class"])),
         "<tr><td>From the compared structures</td><td>{}</td><td>{}</td></tr></table>".format(
             _e(out["nd_class_structures"]), _e(out["nh_class_structures"])),
         "<h2>Structures</h2>",
         "<p>Causes: {}.</p>".format(_e(", ".join("{} {}".format(k, v) for k, v in
                                                   out["counts"].items() if v) or "none")),
         '<div class="wrap"><table><tr>{}</tr>'.format(
             "".join("<th>{}</th>".format(_e(c)) for c in COLUMNS))]
    for r in out["rows"]:
        t.append('<tr{}>{}</tr>'.format(' class="d"' if r["Differs"] == "Yes" else "",
                                        "".join("<td>{}</td>".format(_e(r.get(c, "")))
                                                for c in COLUMNS)))
    t.append("</table></div>")
    if out["road_rows"]:
        t.append("<h2>Roads and railroads</h2>")
        t.append('<div class="wrap"><table><tr>{}</tr>'.format(
            "".join("<th>{}</th>".format(_e(c)) for c in ROAD_COLUMNS)))
        for r in out["road_rows"]:
            t.append('<tr{}>{}</tr>'.format(' class="d"' if r["Differs"] == "Yes" else "",
                                            "".join("<td>{}</td>".format(_e(r.get(c, "")))
                                                    for c in ROAD_COLUMNS)))
        t.append("</table></div>")
    t.append("<h2>Notes</h2><ul>")
    t += ["<li>{}</li>".format(_e(n)) for n in out["notes"]]
    t.append("<li>ND Dam Hazard is the classification authority; nd_hazard's results are "
             "screening previews. The official classification is made by the ND Department of "
             "Water Resources. Not yet run in a real QGIS.</li></ul></body></html>")
    return "\n".join(t) + "\n"


# ------------------------------------------------------ the two entry points --
def _raster_transform(struct_crs, raster_path):
    """(x, y) -> (x, y) from struct_crs to the raster's CRS, or None when they
    are the same or cannot be compared (QGIS needed)."""
    if struct_crs is None:
        return None
    try:
        from qgis.core import (QgsRasterLayer, QgsCoordinateTransform, QgsProject,
                               QgsPointXY)
        lyr = QgsRasterLayer(raster_path, "depth")
        rcrs = lyr.crs() if lyr is not None and lyr.isValid() else None
        if rcrs is None or not rcrs.isValid() or rcrs == struct_crs:
            return None
        ct = QgsCoordinateTransform(struct_crs, rcrs, QgsProject.instance().transformContext())

        def fn(x, y):
            p = ct.transform(QgsPointXY(x, y))
            return p.x(), p.y()
        return fn
    except Exception:
        return None


def compare_result(res, scenario=None, method=METHOD_CURVES, chart_file=None, nh_path=None,
                   wet_ft=0.1, out_folder=None, judgment_as_probable=None, write=True,
                   use_nd_types=False, depth_path=None, vel_path=None):
    """Comparison for a pipeline result dict (run_classification): structures of
    the governing breach scenario (or `scenario`), its depth / velocity
    rasters, ND's roads / railroads and dam class. Writes outputs/QA/
    Rules_Comparison.csv / .html unless write=False."""
    results = res.get("results") or {}
    name = scenario or res.get("governing") or (next(iter(results)) if results else None)
    sres = results.get(name) or {}
    sc = sres.get("scenario")
    depth_path = depth_path or (getattr(sc, "depth", None) if sc is not None else None)
    vel_path = vel_path or (getattr(sc, "velocity", None) if sc is not None else None)
    snap = sres.get("snap") or {}
    recs = []
    for rec in res.get("structs") or []:
        r = dict(rec)
        r.update(snap.get(rec.get("id"), {}))         # this scenario's values
        recs.append(r)
    run = res.get("run")
    cfg = getattr(run, "cfg", None) or {}
    judge = cfg.get("judgment_as_probable", True) if judgment_as_probable is None \
        else bool(judgment_as_probable)
    if out_folder is None and write:
        base = getattr(run, "out", None) or cfg.get("output_folder")
        out_folder = os.path.join(base, SUBDIR_QA) if base else None
    nd = sres.get("nd") or res.get("nd") or {}
    return run_compare(recs, depth_path, vel_path, nd_class=nd.get("cls", ""), judge=judge,
                       method=method, chart_file=chart_file, nh_path=nh_path, wet_ft=wet_ft,
                       roads=sres.get("roads") or (), rails=sres.get("rails") or (),
                       out_folder=out_folder if write else None,
                       dam_name=res.get("dam_name") or "", source="scenario '{}'".format(name),
                       one_d=bool(cfg.get("one_d")), use_nd_types=use_nd_types,
                       transform=_raster_transform(res.get("crs"), depth_path))


def _find_structures_file(out_folder, name=None):
    folder = os.path.join(out_folder, SUBDIR_CLASSIFICATION)
    if name:
        p = name if os.path.isabs(name) else os.path.join(folder, name)
        return p if os.path.isfile(p) else None
    p = os.path.join(folder, "Habitable_Structures.gpkg")
    if os.path.isfile(p):
        return p
    hits = sorted((os.path.join(folder, f) for f in os.listdir(folder)
                   if f.startswith("Habitable_Structures") and f.endswith(".gpkg")),
                  key=os.path.getmtime) if os.path.isdir(folder) else []
    return hits[-1] if hits else None


def _read_run_settings(out_folder):
    p = os.path.join(out_folder, SUBDIR_QA, "Run_Settings.json")
    try:
        with open(p, encoding="utf-8-sig") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _nd_class_from_summary(out_folder):
    try:
        from . import portfolio as PF
        p = PF.newest(out_folder, "Hazard_Classification_Summary*.txt")
        if not p:
            return ""
        return PF.summary_facts(PF.parse_summary(PF.read_text(p))).get("nd", "") or ""
    except Exception:
        return ""


_ZONE_OF_LABEL = {"HIGH": CLASS_HIGH, "JUDGMENT": CLASS_JUDGMENT, "LOW": CLASS_LOW}


def read_structures_layer(path):
    """ND structure records from a Habitable_Structures GeoPackage (QGIS)."""
    from qgis.core import QgsVectorLayer
    lyr = QgsVectorLayer(path, "structures", "ogr")
    if lyr is None or not lyr.isValid():
        raise ValueError("structure layer could not be opened: {}".format(path))
    names = [f.name() for f in lyr.fields()]
    recs = []
    for feat in lyr.getFeatures():
        a = dict(zip(names, feat.attributes()))
        g = lambda k: _clean_attr(a.get(k))          # noqa: E731
        zone = g("ACER_Zone")
        try:
            zone = int(zone) if zone is not None else None
        except (TypeError, ValueError):
            zone = _ZONE_OF_LABEL.get(str(g("ACER_Zone_Label") or "").upper())
        addr = ", ".join(str(x) for x in (g("Address"), g("City"), g("ZIP")) if x)
        recs.append({"id": g("Struct_ID"), "geom": feat.geometry(), "habitable": g("Habitable"),
                     "acer_type": g("ACER_Type"), "max_depth": _num(g("Max_Depth_ft")),
                     "max_vel": _num(g("Max_Vel_fps")), "zone": zone,
                     "outcome": g("ND_Outcome") or _OUT_DRY,
                     "occupancy": g("Occupancy") or "", "fema_category": g("FEMA_Category") or "",
                     "zone_note": "moved" if "zone moved" in str(g("Notes") or "") else "",
                     "address": addr})
    crs = lyr.crs()
    return recs, crs


def _clean_attr(v):
    if v is None:
        return None
    try:
        if v.isNull():                 # QVariant NULL
            return None
    except AttributeError:
        pass
    if type(v).__name__ in ("_Null", "QVariant", "NULL"):
        return None
    return v


def compare_folder(out_folder, depth_path=None, vel_path=None, method=METHOD_CURVES,
                   chart_file=None, nh_path=None, wet_ft=0.1, structures_file=None,
                   judgment_as_probable=None, write=True):
    """Comparison from the files a run wrote: <outputs>/Classification/
    Habitable_Structures.gpkg (fields Max_Depth_ft, Max_Vel_fps, ACER_Zone,
    Habitable, ACER_Type, ND_Outcome, Occupancy ...), the breach rasters named
    in QA/Run_Settings.json (or given), the ND class from the summary. Point
    structures are in that layer as their evaluation disc, so nd_hazard samples
    the disc (said in the notes)."""
    if not out_folder or not os.path.isdir(out_folder):
        raise ValueError("outputs folder not found: {}".format(out_folder))
    if not os.path.isdir(os.path.join(out_folder, SUBDIR_CLASSIFICATION)) and \
            os.path.isdir(os.path.join(out_folder, "outputs")):
        out_folder = os.path.join(out_folder, "outputs")
    sfile = _find_structures_file(out_folder, structures_file)
    if not sfile:
        raise ValueError("no Habitable_Structures GeoPackage in {} - run the classification "
                         "first".format(os.path.join(out_folder, SUBDIR_CLASSIFICATION)))
    rs = _read_run_settings(out_folder)
    if not depth_path:
        for sc in rs.get("scenarios") or []:
            if (sc.get("role") or "breach") == "breach" and sc.get("depth"):
                depth_path, vel_path = sc.get("depth"), vel_path or sc.get("velocity")
                break
    if not depth_path:
        raise ValueError("no depth raster given and none named in QA/Run_Settings.json")
    settings = rs.get("settings") or {}
    judge = settings.get("judgment_as_probable", True) if judgment_as_probable is None \
        else bool(judgment_as_probable)
    recs, crs = read_structures_layer(sfile)
    out = run_compare(recs, depth_path, vel_path, nd_class=_nd_class_from_summary(out_folder),
                      judge=judge, method=method, chart_file=chart_file, nh_path=nh_path,
                      wet_ft=wet_ft, out_folder=os.path.join(out_folder, SUBDIR_QA)
                      if write else None, dam_name=rs.get("dam_name") or "",
                      source=os.path.basename(sfile), one_d=bool(settings.get("one_d")),
                      transform=_raster_transform(crs, depth_path))
    out["notes"].append("File mode: structures from {} (habitable / unverified structures "
                        "only; point structures appear as their evaluation disc and are "
                        "sampled as that disc by nd_hazard).".format(os.path.basename(sfile)))
    if write:
        write_outputs(out, os.path.join(out_folder, SUBDIR_QA))
    return out
