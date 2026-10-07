# -*- coding: utf-8 -*-
"""v2.8: downstream termination point of the incremental hazard.

FEMA P-333 asks for the incremental consequences of a dam failure: the
impacts that exist only because the dam fails. Going down the channel the
breach flood dies out - its depth (or, against a no-breach baseline, its
RISE over the baseline flood) gets small enough that it no longer adds a
hazard. The plugin classifies what lies inside the model; this module says
where the flood stops mattering, and warns when the model does not reach
that point (then structures, roads or a second dam may lie beyond it).

Method (uses the travel-time stations of traveltime.py: river mile 0 at the
dam, a station every TRAVEL_STATION_MI miles, the value of a station = the
deepest wet cell within TRAVEL_SEARCH_FT of it):

  1. below(station) = rise < rise_ft, where rise = breach max depth - baseline
     max depth (a station the baseline does not cover is judged by the breach
     depth alone); with no baseline: breach max depth < depth_ft.
  2. The search starts at the first station where the flood is NOT below the
     threshold (so a dry reservoir or a NoData dam cell at river mile 0 does
     not end it), and ends at the first station that is below and stays
     below for the following `confirm` stations - one dry or shallow cell
     in the middle of a wet reach does not end it.
  3. A station outside the raster, or on its edge where the buffer is cut by
     the raster edge, cannot show that the flood ended. If the data run out
     before a confirmed end, the point is not reported as found: the result
     says the model extent ends before the flood does (status "extent_ends")
     and gives the last station and its depth.
  4. If the centerline itself ends right after the run of below stations (the
     travel-time table drops the dry channel beyond the flood), fewer than
     `confirm` stations may confirm the end; the result says so.

rise_ft, depth_ft and confirm are SCREENING VALUES CHOSEN BY THE ENGINEER
(2.0 ft, 2.0 ft, 3 stations), not requirements of REG_05.2026, FEMA P-333 or
TM 11. The termination point is a screening aid for where to stop looking;
the engineer decides the study limit.

The decision core (find) works on plain station dicts and needs no QGIS.
"""

import csv
import os

import numpy as np

from .constants import SUBDIR_QA, TRAVEL_STATION_MI, TRAVEL_SEARCH_FT
from . import traveltime as TT

# SCREENING VALUES CHOSEN BY THE ENGINEER - not rules
DEFAULT_RISE_FT = 2.0
DEFAULT_DEPTH_FT = 2.0
DEFAULT_CONFIRM = 3

CSV_NAME = "Termination_Point.csv"
CSV_STATIONS = "Termination_Stations.csv"
LAYER_NAME = "EAP_Termination_Point"

ST_FOUND, ST_EXTENT, ST_NEVER, ST_NONE = "found", "extent_ends", "never_above", "no_stations"
STATUS_TEXT = {ST_FOUND: "Found", ST_EXTENT: "Not reached - model extent ends",
               ST_NEVER: "Never above the threshold", ST_NONE: "No stations"}

HEAD = ["Status", "River_Mile", "X", "Y", "Breach_Depth_ft", "Baseline_Depth_ft", "Rise_ft",
        "Criterion", "Threshold_ft", "Stations_Confirmed", "Stations_Required",
        "Last_Station_RM", "Last_Station_Depth_ft", "Warning", "Note"]
STATION_HEAD = ["Order", "River_Mile", "X", "Y", "Breach_Depth_ft", "Baseline_Depth_ft",
                "Rise_ft", "Below_Threshold", "Data", "Is_Termination_Point"]


# ------------------------------------------------------------------ the decision --
def _metric(s, has_base, rise_ft, depth_ft):
    """(below, value, criterion) of one station: below = True / False / None
    (cannot be judged: no data, or the buffer is cut by the raster edge)."""
    d = s.get("depth")
    b = s.get("base_depth")
    if has_base and b is not None and d is not None:
        val, crit, thr = d - b, "rise", rise_ft
    else:
        val, crit, thr = d, "depth", depth_ft
    if d is None or s.get("state") == "outside":
        return None, val, crit
    below = val < thr
    if below and s.get("state") == "edge":
        return None, val, crit           # dry only because the raster stops here
    return below, val, crit


def find(stations, rise_ft=DEFAULT_RISE_FT, depth_ft=DEFAULT_DEPTH_FT,
         confirm=DEFAULT_CONFIRM):
    """Termination point from station dicts ordered downstream from the dam:
    {"rm", "x", "y", "depth" (ft, None = no data), "base_depth" (ft or None),
    "state" ("ok" default / "edge" / "outside")}. Returns the result dict
    (see summary_lines / to_layer)."""
    confirm = max(1, int(confirm))
    st = [dict(s) for s in (stations or [])]
    st.sort(key=lambda s: s.get("rm", 0.0))
    has_base = any(s.get("base_depth") is not None for s in st)
    res = {"status": ST_NONE, "found": False, "index": None, "rm": None, "x": None, "y": None,
           "depth": None, "base_depth": None, "rise": None, "criterion": "rise" if has_base
           else "depth", "threshold": rise_ft if has_base else depth_ft, "confirm": confirm,
           "confirmed_by": 0, "n_stations": len(st), "last_rm": None, "last_depth": None,
           "last_rise": None, "warnings": [], "notes": [], "has_baseline": has_base,
           "stations": []}
    if not st:
        res["warnings"].append("no stations along the channel - no centerline")
        return res
    rows = []
    for s in st:
        below, val, crit = _metric(s, has_base, rise_ft, depth_ft)
        rows.append((below, val, crit))
        s["below"] = below
        s["value"] = val
        s["criterion"] = crit
    res["stations"] = st
    usable = [i for i, (b, _v, _c) in enumerate(rows) if b is not None]
    if usable:
        last = st[usable[-1]]
        res["last_rm"], res["last_depth"] = last.get("rm"), last.get("depth")
        res["last_rise"] = rows[usable[-1]][1] if has_base else None
    n_nb = sum(1 for s in st if has_base and s.get("base_depth") is None
               and s.get("depth") is not None)
    if n_nb:
        res["warnings"].append("the baseline raster does not cover {} station(s); the "
                               "breach depth alone (< {:g} ft) was used there".format(
                                   n_nb, depth_ft))
    first_above = next((i for i, (b, _v, _c) in enumerate(rows) if b is False), None)
    if first_above is None:
        res["status"] = ST_NEVER
        res["warnings"].append(
            "the flood is not above the threshold ({:g} ft {}) at any station: there is "
            "nothing to terminate on this centerline - check that the centerline follows "
            "the flood".format(res["threshold"], "rise over the baseline" if has_base
                               else "depth"))
        return res
    outside_at = next((s for s in st[first_above:] if s.get("state") == "outside"
                       or s.get("depth") is None), None)
    hit = None
    ended_by_data = False
    for i in range(first_above + 1, len(st)):
        if rows[i][0] is not True:
            continue
        tail = list(range(i + 1, min(len(st), i + 1 + confirm)))
        bad = next((j for j in tail if rows[j][0] is not True), None)
        if bad is None:
            hit = i
            break
        if rows[bad][0] is None:
            ended_by_data = True          # the data stop before the end is confirmed
            break
    if hit is not None:
        s = st[hit]
        n_after = min(len(st), hit + 1 + confirm) - hit - 1
        res.update({"status": ST_FOUND, "found": True, "index": hit, "rm": s.get("rm"),
                    "x": s.get("x"), "y": s.get("y"), "depth": s.get("depth"),
                    "base_depth": s.get("base_depth") if has_base else None,
                    "rise": rows[hit][1] if rows[hit][2] == "rise" else None,
                    "criterion": rows[hit][2], "confirmed_by": n_after})
        res["threshold"] = rise_ft if rows[hit][2] == "rise" else depth_ft
        if n_after < confirm:
            res["notes"].append(
                "confirmed by {} following station(s) (required {}) because the centerline "
                "ends {} (the travel-time table drops the dry channel beyond the flood); "
                "check the channel beyond if it continues".format(
                    n_after, confirm, "there" if n_after == 0 else "soon after"))
        return res
    # not found: the model / centerline ends before the flood does
    res["status"] = ST_EXTENT
    li = usable[-1] if usable else first_above
    ls = st[li]
    if ended_by_data or outside_at is not None:
        edge_rm = outside_at.get("rm") if outside_at is not None else None
        why = "the raster edge / NoData is reached{}".format(
            "" if edge_rm is None else " at river mile {:.1f}".format(edge_rm))
    else:
        why = "the centerline ends at river mile {:.1f}".format(st[-1].get("rm", 0.0))
    res["x"], res["y"], res["rm"] = ls.get("x"), ls.get("y"), ls.get("rm")
    res["depth"] = ls.get("depth")
    res["base_depth"] = ls.get("base_depth") if has_base else None
    res["rise"] = rows[li][1] if rows[li][2] == "rise" else None
    res["criterion"] = rows[li][2]
    last_val = rows[li][1]
    if rows[li][0] is False:
        res["warnings"].append(
            "the model extent ends before the flood does: {} and at the last station "
            "(river mile {:.1f}) the {} is still {:.1f} ft (threshold {:g} ft) - the "
            "termination point lies beyond the modelled area; extend the model downstream "
            "before relying on the classification of what lies beyond".format(
                why, ls.get("rm", 0.0), "rise over the baseline" if rows[li][2] == "rise"
                else "depth", last_val if last_val is not None else float("nan"),
                res["threshold"]))
    else:
        res["warnings"].append(
            "the flood falls below the threshold near river mile {:.1f} but {} before {} "
            "further station(s) can confirm it: the model may not reach the termination "
            "point; check the channel beyond".format(ls.get("rm", 0.0), why, confirm))
    return res


# ------------------------------------------------------------------- station data --
def stations_from_rows(rows, depth_key="depth"):
    """Station dicts from traveltime.build()["rows"] (kind "Station"). The
    travel-time table has no data / dry difference (depth None = no wet cell
    within the search distance): None is taken as dry (0 ft) and the extent of
    the raster is not known - use stations_from_line for that."""
    out = []
    for r in rows or []:
        if r.get("kind") != "Station":
            continue
        d = r.get(depth_key)
        out.append({"rm": r.get("rm", r["ch"] / 5280.0 if "ch" in r else 0.0),
                    "x": r.get("x"), "y": r.get("y"),
                    "depth": 0.0 if d is None else float(d), "state": "ok"})
    return out


def _raster_bbox(sampler):
    gt, w, h = sampler.gt, sampler.xsize, sampler.ysize
    xs, ys = [], []
    for c, r in ((0, 0), (w, 0), (0, h), (w, h)):
        xs.append(gt[0] + c * gt[1] + r * gt[2])
        ys.append(gt[3] + c * gt[4] + r * gt[5])
    return min(xs), min(ys), max(xs), max(ys)


def station_depth(sampler, x, y, radius, xform=None):
    """(state, depth) at a station: state "ok" / "edge" (the search circle is
    cut by the raster edge) / "outside"; depth = the deepest wet cell within
    `radius` of (x, y) (analysis CRS; `xform` analysis -> sampler CRS), 0.0
    when dry, None when there is no raster data. The rule of traveltime.build."""
    from qgis.core import QgsGeometry, QgsPointXY
    from .helpers import transform_geometry
    if sampler is None or not sampler.ok or sampler.gt is None:
        return "outside", None
    g = QgsGeometry.fromPointXY(QgsPointXY(x, y)).buffer(radius, 8)
    g = transform_geometry(g, xform) if xform is not None else g
    if g is None or g.isNull() or g.isEmpty():
        return "outside", None
    bb = g.boundingBox()
    rx0, ry0, rx1, ry1 = _raster_bbox(sampler)
    cx, cy = (bb.xMinimum() + bb.xMaximum()) / 2.0, (bb.yMinimum() + bb.yMaximum()) / 2.0
    if not (rx0 <= cx <= rx1 and ry0 <= cy <= ry1):
        return "outside", None
    state = "ok" if (bb.xMinimum() >= rx0 and bb.xMaximum() <= rx1 and
                     bb.yMinimum() >= ry0 and bb.yMaximum() <= ry1) else "edge"
    _xs, _ys, d = sampler.cells_in_geometry(g)
    if d.size == 0:                       # a cell coarser than the circle
        d = sampler.values_at([cx], [cy])
    wet = d[np.isfinite(d) & (d > 0)]
    return state, (float(wet.max()) if wet.size else 0.0)


def stations_from_line(line, depth_sampler, analysis_crs, transform_ctx, lat=None, *,
                       base_sampler=None, spacing_mi=TRAVEL_STATION_MI,
                       search_ft=TRAVEL_SEARCH_FT):
    """Stations along `line` ([(x, y)...] in the analysis CRS, ordered from the
    dam downstream - traveltime.build()["line"]) every spacing_mi miles, with the
    breach depth (depth_sampler) and, with base_sampler, the no-breach depth,
    each the deepest wet cell within search_ft. Uses traveltime.stations /
    point_at / line_length."""
    from .structures import _ft_units
    unit = _ft_units(1.0, analysis_crs, lat)            # CRS units per foot
    if unit <= 0 or not line or len(line) < 2:
        return []
    radius = search_ft * unit
    xb = TT._xform(analysis_crs, base_sampler, transform_ctx) \
        if base_sampler is not None else None
    out = []
    for ch in TT.stations(TT.line_length(line), spacing_mi * 5280.0 * unit):
        x, y = TT.point_at(line, ch)
        state, d = station_depth(depth_sampler, x, y, radius)
        s = {"rm": ch / unit / 5280.0, "ch": ch, "x": x, "y": y, "depth": d, "state": state}
        if base_sampler is not None:
            _bs, b = station_depth(base_sampler, x, y, radius, xb)
            s["base_depth"] = b
        out.append(s)
    return out


def trace_line(depth_sampler, arr_sampler, analysis_crs, transform_ctx, lat, **kw):
    """(line, notes) of the channel centerline, made by traveltime.build (a
    centerline layer, or the NHD flowlines traced downstream from the dam;
    same orientation and river mile 0). kw: centerline_layer, flowline_layer,
    dam_point, spacing_mi, search_ft, max_mi, snap_ft, clip."""
    r = TT.build(depth_sampler, arr_sampler, None, analysis_crs, transform_ctx, lat, **kw)
    if not r:
        return [], ["no centerline could be made"]
    return r.get("line") or [], r.get("notes") or []


def analyze(line, depth_sampler, analysis_crs, transform_ctx, lat=None, *,
            base_sampler=None, rise_ft=DEFAULT_RISE_FT, depth_ft=DEFAULT_DEPTH_FT,
            confirm=DEFAULT_CONFIRM, spacing_mi=TRAVEL_STATION_MI,
            search_ft=TRAVEL_SEARCH_FT):
    """Stations along the centerline, then find(). With base_sampler (the
    no-breach depth raster of the scenario's baseline) the rise over it is
    used, else the breach depth."""
    st = stations_from_line(line, depth_sampler, analysis_crs, transform_ctx, lat,
                            base_sampler=base_sampler, spacing_mi=spacing_mi,
                            search_ft=search_ft)
    res = find(st, rise_ft, depth_ft, confirm)
    res["crs"] = analysis_crs
    return res


def from_travel(tt_result, rise_ft=DEFAULT_RISE_FT, depth_ft=DEFAULT_DEPTH_FT,
                confirm=DEFAULT_CONFIRM, base_rows=None):
    """find() on the station rows of traveltime.build() (breach depth only, or
    with base_rows - the rows of the same table made on the baseline depth
    raster and centerline - the rise). No extent information (see
    stations_from_rows)."""
    st = stations_from_rows((tt_result or {}).get("rows"))
    if base_rows:
        base = stations_from_rows(base_rows)
        for s, b in zip(st, base):
            s["base_depth"] = b["depth"]
    return find(st, rise_ft, depth_ft, confirm)


# ------------------------------------------------------------------------ output --
def _num(v, n=2):
    return "" if v is None else round(float(v), n)


def summary_lines(result):
    """Lines for the summary text (classification.summary_lines extra key
    "termination")."""
    if not result:
        return []
    out = []
    crit = ("rise over the no-breach baseline < {:g} ft".format(result["threshold"])
            if result.get("criterion") == "rise"
            else "flood depth < {:g} ft".format(result["threshold"]))
    head = "Downstream termination of the incremental hazard (screening; {} for {} " \
           "consecutive station(s), values chosen by the engineer)".format(
               crit, result.get("confirm"))
    if result["status"] == ST_FOUND:
        out.append("{}: river mile {:.1f} at ({:.1f}, {:.1f}) - breach depth {} ft{}{}".format(
            head, result["rm"], result["x"], result["y"], _num(result["depth"], 1),
            ", baseline {} ft".format(_num(result["base_depth"], 1))
            if result.get("base_depth") is not None else "",
            ", rise {} ft".format(_num(result["rise"], 1)) if result.get("rise") is not None
            else ""))
    elif result["status"] == ST_EXTENT:
        out.append("{}: NOT REACHED within the model - last station river mile {} still {} ft "
                   "deep".format(head, _num(result.get("last_rm"), 1),
                                 _num(result.get("last_depth"), 1)))
    elif result["status"] == ST_NEVER:
        out.append("{}: the flood is below the threshold at every station".format(head))
    else:
        out.append("{}: not made (no stations)".format(head))
    for w in result.get("warnings") or []:
        out.append("WARNING: " + w)
    for n in result.get("notes") or []:
        out.append("Note: " + n)
    if result["status"] in (ST_FOUND, ST_EXTENT):
        out.append("QA/{} (point layer {})".format(CSV_NAME, LAYER_NAME))
    return out


def row(result):
    """One dict keyed by HEAD (QA/Termination_Point.csv)."""
    r = result or {}
    return {"Status": STATUS_TEXT.get(r.get("status"), ""),
            "River_Mile": _num(r.get("rm")), "X": _num(r.get("x"), 3),
            "Y": _num(r.get("y"), 3), "Breach_Depth_ft": _num(r.get("depth")),
            "Baseline_Depth_ft": _num(r.get("base_depth")), "Rise_ft": _num(r.get("rise")),
            "Criterion": r.get("criterion") or "", "Threshold_ft": _num(r.get("threshold")),
            "Stations_Confirmed": r.get("confirmed_by", ""),
            "Stations_Required": r.get("confirm", ""),
            "Last_Station_RM": _num(r.get("last_rm")),
            "Last_Station_Depth_ft": _num(r.get("last_depth")),
            "Warning": " | ".join(r.get("warnings") or []),
            "Note": " | ".join(r.get("notes") or [])}


def station_rows(result):
    out = []
    idx = (result or {}).get("index") if (result or {}).get("status") == ST_FOUND else None
    for i, s in enumerate((result or {}).get("stations") or []):
        data = s.get("state") or "ok"
        if s.get("depth") is None:
            data = "no data"
        out.append({"Order": i + 1, "River_Mile": _num(s.get("rm")), "X": _num(s.get("x"), 3),
                    "Y": _num(s.get("y"), 3), "Breach_Depth_ft": _num(s.get("depth")),
                    "Baseline_Depth_ft": _num(s.get("base_depth")),
                    "Rise_ft": _num(s.get("value")) if s.get("criterion") == "rise" else "",
                    "Below_Threshold": "" if s.get("below") is None
                    else ("Yes" if s["below"] else "No"),
                    "Data": data, "Is_Termination_Point": "Yes" if i == idx else ""})
    return out


def _write(path, head, rows):
    from .helpers import prepare_output_path
    path = prepare_output_path(path)
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=head)
        w.writeheader()
        for r in rows:
            w.writerow(r)
    return path


def write_csv(folder, result):
    """QA/Termination_Point.csv ("folder" is the QA folder): one row. Returns
    the path, or None when there is no result."""
    if not result or result.get("status") == ST_NONE:
        return None
    return _write(os.path.join(folder, CSV_NAME), HEAD, [row(result)])


def write_stations_csv(folder, result):
    """QA/Termination_Stations.csv - every station with its depths, the
    verdict of the threshold and the termination point marked (audit trail)."""
    if not result or not result.get("stations"):
        return None
    return _write(os.path.join(folder, CSV_STATIONS), STATION_HEAD, station_rows(result))


def qa_folder(out_folder):
    return os.path.join(out_folder, SUBDIR_QA)


LAYER_FIELDS = (("Status", "str"), ("River_Mile", "dbl"), ("Criterion", "str"),
                ("Threshold_ft", "dbl"), ("Breach_Depth_ft", "dbl"),
                ("Baseline_Depth_ft", "dbl"), ("Rise_ft", "dbl"), ("Confirmed_By", "int"),
                ("Required", "int"), ("Warning", "str"))


def to_layer(result, crs, name=LAYER_NAME):
    """Memory point layer with ONE feature: the termination point - or, when
    the model ends before the flood does, the last station the flood reaches
    (Status says which). None when there is no point to show."""
    if not result or result.get("x") is None or result.get("status") in (ST_NONE, ST_NEVER):
        return None
    from qgis.core import QgsFeature, QgsField, QgsGeometry, QgsPointXY
    from .compat import FT_INT, FT_DOUBLE, FT_STRING, NULL, WKB_POINT
    from .helpers import create_memory_layer
    ft = {"int": FT_INT, "dbl": FT_DOUBLE, "str": FT_STRING}
    lyr = create_memory_layer(name, WKB_POINT, crs, [QgsField(n, ft[t])
                                                    for n, t in LAYER_FIELDS])

    def nz(v):
        return NULL if v is None else round(float(v), 2)
    f = QgsFeature(lyr.fields())
    f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(result["x"], result["y"])))
    f.setAttributes([STATUS_TEXT[result["status"]], nz(result.get("rm")),
                     result.get("criterion") or NULL, nz(result.get("threshold")),
                     nz(result.get("depth")), nz(result.get("base_depth")),
                     nz(result.get("rise")), int(result.get("confirmed_by") or 0),
                     int(result.get("confirm") or 0),
                     " | ".join(result.get("warnings") or []) or NULL])
    lyr.dataProvider().addFeatures([f])
    lyr.updateExtents()
    return lyr
