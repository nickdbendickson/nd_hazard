"""Where the flood crosses a road or a railroad, and for how long (3.20.0).

Feature 13 of the review: the county wants a table it can post - which roads go under water,
when the water gets there, how deep and how fast, how long they stay wet and which ones have to
be closed. The consequences tool already reports flooded road LENGTH per feature, which answers
"how much road" but not "where do I put the barricade".

A crossing here is one unbroken stretch of a road or rail line that the flood wets. The
receptor samples (impacts.Receptors) of one line are in order along it and each stands for a
known length, so a run of consecutive wet samples is exactly that stretch, and its ends are
where the water starts and stops. One row per stretch, so a road that dips into the flood
twice gives two rows - two barricades.

    rows = crossing_rows(rec, grid, fields, sim_end_hr=36.0)
    paths = write_outputs(folder, rows, crs="EPSG:26914")

Columns: road / rail name, first wet (hours after breach initiation), maximum depth and
velocity over the stretch, depth x velocity, how long it stays wet, the wet length, and the
closure flag.

Decisions:
  * Duration. The run keeps maximum depth and arrival time, not a depth time series, so the
    duration is `end of the simulation - first wet` unless the caller passes depth snapshots
    (the animation tool's time steps). That is an UPPER bound - the water may well have gone
    down before the clock ran out - and every row says which basis it used
    ("snapshots" or "to the end of the run"). An upper bound is the conservative side for a
    road closure, and it is labelled rather than quietly presented as the answer.
  * Closure. A stretch is flagged closed when the depth over it reaches `closure_depth_ft`
    (0.5 ft: the usual "turn around, don't drown" depth for a car, and the engineer's value to
    change) or the depth x velocity product reaches `closure_dv_ft2s` (7 ft2/s, the figure
    impacts.py already uses for a person being swept off their feet). The flag is advice for an
    EAP, not a legal closure order, and the table says so.
  * A stretch wet by less than `wet_ft` is not a crossing: a hundredth of a foot of water on a
    gravel road is a raster artefact, not a closure.
  * Nothing here decides whether the road is a route out of the flooded area: that is the
    hazard classification's question (sole-access roads, ND 5.2), and the ND hazard tool asks
    it with its own R_SOLE field.

Pure numpy and text; no QGIS (the GeoJSON goes through products.write_geojson).
"""
import os

import numpy as np

from . import impacts as impmod
from . import products as prodmod
from .units import FT

WET_FT = 0.1                 # below this a stretch is not wet (raster noise)
CLOSURE_DEPTH_FT = 0.5       # the engineer's value: a car is in trouble above it
CLOSURE_DV_FT2S = impmod.DV_DANGER_FT2S     # 7 ft2/s, as impacts.py uses for people
DURATION_SNAPSHOTS = "snapshots"
DURATION_TO_END = "to the end of the run"
DURATION_UNKNOWN = "unknown"
KINDS = (impmod.KIND_ROAD, impmod.KIND_RAIL)
COLUMNS = ("id", "kind", "crossing", "x", "y", "first_wet_hr", "max_depth_ft",
           "max_velocity_fps", "max_dv_ft2s", "duration_wet_hr", "duration_basis",
           "wet_length_ft", "samples", "closed", "closure_reason")
OUTPUT_NAMES = {"csv": "Road and Rail Crossings.csv",
                "geojson": "Road and Rail Crossings.geojson"}
TABLE_NOTE = ("One row per unbroken stretch of road or railroad that the breach flood wets. "
              "Times are hours after breach initiation. 'Closed' is advice for an emergency "
              "action plan - depth at or above {:g} ft, or depth x velocity at or above "
              "{:g} ft2/s - not a legal closure.".format(CLOSURE_DEPTH_FT, CLOSURE_DV_FT2S))


def _us(fields, units):
    """{depth ft, velocity ft/s, arrival hours} from the run's arrays."""
    out = {}
    for key, factor in (("depth", 1.0 / FT), ("velocity", 1.0 / FT), ("arrival", 1.0)):
        a = (fields or {}).get(key)
        if a is None:
            out[key] = None
            continue
        a = np.asarray(a, dtype=float)
        out[key] = a * factor if units == "si" and factor != 1.0 else a
    return out


def _sample(grid, x, y, arr):
    """`arr` at each point, NaN outside the grid ("" when there is no such raster)."""
    if arr is None:
        return np.full(len(x), np.nan)
    c = np.floor((np.asarray(x) - grid.x0) / grid.dx).astype(int)
    r = np.floor((grid.y0 - np.asarray(y)) / grid.dx).astype(int)
    ok = (r >= 0) & (r < grid.ny) & (c >= 0) & (c < grid.nx)
    out = np.full(len(x), np.nan)
    if ok.any():
        out[ok] = np.asarray(arr)[r[ok], c[ok]]
    return out


def _runs(mask):
    """[(start, stop)] of the True stretches of a boolean array (stop exclusive)."""
    out = []
    start = None
    for i, v in enumerate(mask):
        if v and start is None:
            start = i
        elif not v and start is not None:
            out.append((start, i))
            start = None
    if start is not None:
        out.append((start, len(mask)))
    return out


def _duration(snapshots, grid, x, y, first_hr, sim_end_hr, wet_ft=WET_FT):
    """(hours wet, basis) for one stretch. `snapshots` depths are in metres, as the workflow
    keeps its arrays; a time step counts when any sample of the stretch is wet in it."""
    if snapshots:
        times = sorted(float(t) for t in snapshots)
        wet_m = wet_ft * FT
        wet_times = []
        for t in times:
            d = _sample(grid, x, y, np.asarray(snapshots[t], dtype=float))
            if np.isfinite(d).any() and float(np.nanmax(d)) >= wet_m:
                wet_times.append(t)
        if wet_times:
            step = (times[-1] - times[0]) / max(1, len(times) - 1)
            return round(len(wet_times) * step, 2), DURATION_SNAPSHOTS
        return 0.0, DURATION_SNAPSHOTS
    if sim_end_hr is None or first_hr is None or not np.isfinite(first_hr):
        return "", DURATION_UNKNOWN
    return round(max(0.0, float(sim_end_hr) - float(first_hr)), 2), DURATION_TO_END


def crossing_rows(rec, grid, fields, units="us", wet_ft=WET_FT,
                  closure_depth_ft=CLOSURE_DEPTH_FT, closure_dv_ft2s=CLOSURE_DV_FT2S,
                  sim_end_hr=None, snapshots=None):
    """[row] - one row per wet stretch of road or rail, earliest first.

    rec          impacts.Receptors with the road and rail lines added in order
    grid         core.grid.Grid the rasters are on
    fields       {"depth", "velocity", "arrival"} arrays (ft, ft/s, hours; units="si" for SI)
    sim_end_hr   the run's simulated time, for the duration upper bound
    snapshots    optional {hours: depth array} to measure the duration properly"""
    if rec is None or not len(rec):
        return []
    f = _us(fields, units)
    kinds = np.asarray(rec.kind)
    sel = np.zeros(len(rec), dtype=bool)
    for k in KINDS:
        sel |= (kinds == k)
    if not sel.any():
        return []
    x, y = np.asarray(rec.x, dtype=float), np.asarray(rec.y, dtype=float)
    depth = _sample(grid, x, y, f["depth"])
    vel = _sample(grid, x, y, f["velocity"])
    arr = _sample(grid, x, y, f["arrival"])
    length = np.asarray(rec.length_m, dtype=float)
    idents = [str(i) for i in rec.ident]
    wet = sel & np.isfinite(depth) & (depth >= wet_ft)
    rows = []
    # one group per (kind, ident) in the order the samples were added = along the line
    seen = []
    for i in np.nonzero(sel)[0]:
        key = (str(kinds[i]), idents[i])
        if key not in seen:
            seen.append(key)
    for kind, ident in seen:
        idx = np.array([i for i in range(len(rec))
                        if kinds[i] == kind and idents[i] == ident], dtype=int)
        if not len(idx):
            continue
        for n, (a, b) in enumerate(_runs([bool(wet[i]) for i in idx]), 1):
            part = idx[a:b]
            d = depth[part]
            v = vel[part]
            t = arr[part]
            first = float(np.nanmin(t)) if np.isfinite(t).any() else None
            dv = d * np.where(np.isfinite(v), v, 0.0)
            mid = part[len(part) // 2]
            dur, basis = _duration(snapshots, grid, x[part], y[part], first, sim_end_hr,
                                   wet_ft)
            md, mv, mdv = float(np.nanmax(d)), (float(np.nanmax(v)) if np.isfinite(v).any()
                                                else None), float(np.nanmax(dv))
            why = []
            if md >= closure_depth_ft:
                why.append("depth {:.1f} ft >= {:g} ft".format(md, closure_depth_ft))
            if mdv >= closure_dv_ft2s:
                why.append("depth x velocity {:.1f} ft2/s >= {:g}".format(mdv,
                                                                          closure_dv_ft2s))
            rows.append({"id": ident, "kind": kind, "crossing": n,
                         "x": round(float(x[mid]), 2), "y": round(float(y[mid]), 2),
                         "first_wet_hr": "" if first is None else round(first, 2),
                         "max_depth_ft": round(md, 2),
                         "max_velocity_fps": "" if mv is None else round(mv, 2),
                         "max_dv_ft2s": round(mdv, 1),
                         "duration_wet_hr": dur, "duration_basis": basis,
                         "wet_length_ft": round(float(length[part].sum()) / FT, 0),
                         "samples": int(len(part)),
                         "closed": "yes" if why else "no",
                         "closure_reason": "; ".join(why)})
    rows.sort(key=lambda r: (r["first_wet_hr"] if r["first_wet_hr"] != "" else 9e9,
                             r["kind"], str(r["id"])))
    return rows


def summary(rows, wet_ft=WET_FT, closure_depth_ft=CLOSURE_DEPTH_FT,
            closure_dv_ft2s=CLOSURE_DV_FT2S):
    """Counts for the run report and the tool's message."""
    out = {"crossings": len(rows or []), "note": TABLE_NOTE, "wet_ft": wet_ft,
           "closure_depth_ft": closure_depth_ft, "closure_dv_ft2s": closure_dv_ft2s}
    for kind in KINDS:
        got = [r for r in rows or [] if r["kind"] == kind]
        if got:
            out[kind] = {"crossings": len(got),
                         "closed": sum(1 for r in got if r["closed"] == "yes"),
                         "wet_length_ft": round(sum(float(r["wet_length_ft"]) for r in got), 0),
                         "earliest_wet_hr": min((r["first_wet_hr"] for r in got
                                                 if r["first_wet_hr"] != ""), default="")}
    out["closed"] = sum(1 for r in rows or [] if r["closed"] == "yes")
    out["bounded_duration"] = any((r.get("duration_basis") == DURATION_TO_END)
                                  for r in rows or [])
    return out


def markdown_lines(rows, summ=None, title="Road and railroad crossings in the flood"):
    """The report section."""
    summ = summ or summary(rows)
    lines = ["## " + title, "", "- " + TABLE_NOTE]
    if not rows:
        lines += ["- No road or railroad crossing of the flood was found (no road or rail "
                  "layer was given, or none of them gets wet).", ""]
        return lines
    lines.append("- {} crossing(s), {} of them flagged for closure.".format(
        summ.get("crossings", 0), summ.get("closed", 0)))
    if summ.get("bounded_duration"):
        lines.append("- The duration of a row marked \"{}\" is an UPPER bound: the run keeps "
                     "maximum depth and arrival time, not a depth time series. Run the "
                     "animation tool's time steps to measure it.".format(DURATION_TO_END))
    lines.append("")
    lines.append("| Road / rail | First wet (hr) | Max depth (ft) | Max velocity (ft/s) | "
                 "Wet for (hr) | Wet length (ft) | Closed |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- |")
    for r in rows[:50]:
        lines.append("| {} ({} {}) | {} | {} | {} | {} | {} | {} |".format(
            r["id"], r["kind"], r["crossing"], r["first_wet_hr"], r["max_depth_ft"],
            r["max_velocity_fps"], r["duration_wet_hr"], r["wet_length_ft"], r["closed"]))
    if len(rows) > 50:
        lines.append("| ... {} more in {} | | | | | | |".format(len(rows) - 50,
                                                                OUTPUT_NAMES["csv"]))
    lines.append("")
    return lines


def _cell(v):
    s = "" if v is None else str(v)
    return '"{}"'.format(s.replace('"', '""')) if ("," in s or '"' in s) else s


def write_csv(path, rows):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        fh.write(",".join(COLUMNS) + "\n")
        for r in rows or []:
            fh.write(",".join(_cell(r.get(c, "")) for c in COLUMNS) + "\n")
    return path


def features(rows):
    """GeoJSON point features at the middle of each wet stretch."""
    out = []
    for r in rows or []:
        props = {k: v for k, v in r.items() if k not in ("x", "y")}
        out.append({"type": "Feature",
                    "geometry": {"type": "Point", "coordinates": [r["x"], r["y"]]},
                    "properties": props})
    return out


def write_outputs(folder, rows, crs=None, geojson=True):
    """Road and Rail Crossings.csv (+ .geojson) in the dam folder. Returns {kind: path}."""
    os.makedirs(folder, exist_ok=True)
    out = {"csv": write_csv(os.path.join(folder, OUTPUT_NAMES["csv"]), rows), "geojson": ""}
    if geojson and rows:
        p = os.path.join(folder, OUTPUT_NAMES["geojson"])
        prodmod.write_geojson(p, features(rows), crs)
        out["geojson"] = p
    return out
