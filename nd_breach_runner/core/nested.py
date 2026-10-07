"""Nested grids: a fine LISFLOOD-FP grid near the dam, a coarse one downstream.

LISFLOOD-FP has one cell size per run. Near the breach the flow is fast and the geometry
(embankment, breach channel, tailrace, road crossings) needs a fine grid; ten miles
downstream a coarse grid is enough and far cheaper. Two-stage one-way nesting:

1. FINE run: the ordinary workflow over a short domain (the first `reach_mi` downstream, plus a
   margin so the free boundary is away from the measurement) at `fine_cell_m`, with a discharge
   gauge across the flow at the interface.
2. The interface: the gauge's discharge Q(t) (with the breach and, for the incremental result,
   without it) becomes the inflow of
3. the COARSE run: the ordinary workflow (its domain extension, receptor stop rule, ANUGA off)
   started at the interface, at `coarse_cell_m`. The inflow is spread over the width the fine
   flood covers at the interface, not squeezed into the breach width.
4. Merge: fine results upstream of the interface plane, coarse results downstream of it, on one
   coarse-resolution grid (fine cells scattered to the coarse cell containing them: maximum
   depth, speed and WSE, earliest arrival). The fine rasters and the coarse rasters are also
   kept in their own folders. The incremental rasters are worked out from the merged with- and
   without-breach arrays.

Limits: one-way (the coarse model cannot push water back up into the fine domain), the
interface is a straight plane across the local flow direction, and the water that is standing
in the fine domain at the end is not passed on (only the flow through the interface is). The
handover is checked: the volume through the interface against the breach volume is reported.
"""
import copy
import dataclasses
import math
import os

import numpy as np

from . import breach as brmod
from . import flood as floodmod
from . import workflow
from .grid import Grid
from .units import FT, MILE, CFS, ACFT


class NestedResult:
    def __init__(self):
        self.fine = None                  # workflow.RunResult
        self.coarse = None
        self.grid = None                  # merged Grid (coarse resolution)
        self.final_si = None              # merged arrays, SI, on `grid`
        self.base_si = None
        self.final = {}                   # kind -> path
        self.baseline = {}
        self.increment = {}
        self.interface = {}               # xy, direction, width_m, peak_cfs, volume_acft ...
        self.warnings = []
        self.report_md = ""
        self.folder = ""
        self.summary = {}


def interface_point(path, reach_m):
    """The point `reach_m` along the path (list of (x, y)) and the flow direction there."""
    p = np.asarray(path, float)
    seg = np.hypot(*np.diff(p, axis=0).T)
    cum = np.concatenate([[0.0], np.cumsum(seg)])
    d = min(max(reach_m, 0.0), float(cum[-1]))
    i = int(np.clip(np.searchsorted(cum, d, side="right") - 1, 0, len(seg) - 1))
    t = (d - cum[i]) / seg[i] if seg[i] > 0 else 0.0
    q = p[i] + t * (p[i + 1] - p[i])
    a, b = p[max(0, i - 3)], p[min(len(p) - 1, i + 4)]
    v = b - a
    n = float(np.hypot(*v)) or 1.0
    return (float(q[0]), float(q[1])), (float(v[0] / n), float(v[1] / n)), float(cum[-1])


def wet_extent_at(grid, depth, point, direction, thr_m=0.1, half_search_m=None):
    """Width and centre of the wet part of the cross-section through `point`, perpendicular to
    `direction`, from the fine run's maximum depth. Returns (centre_xy, width_m) or None."""
    ux, uy = direction
    nx_, ny_ = -uy, ux                     # across the flow
    half = half_search_m or 0.5 * max(grid.nx, grid.ny) * grid.dx
    step = grid.dx * 0.5
    ts = np.arange(-half, half + step, step)
    xs, ys = point[0] + ts * nx_, point[1] + ts * ny_
    rc = [grid.rc(x, y) for x, y in zip(xs, ys)]
    vals = np.array([depth[r, c] if grid.inside(r, c) else np.nan for r, c in rc])
    wet = np.isfinite(vals) & (vals >= thr_m)
    if not wet.any():
        return None
    # the wet run that contains (or is nearest to) the path point
    idx = np.nonzero(wet)[0]
    mid = int(np.argmin(np.abs(ts[idx])))
    k0 = idx[mid]
    lo = hi = k0
    gap = int(round(2.0 * grid.dx / step))              # bridge dry gaps up to two cells
    j = k0
    while j > 0:
        j -= 1
        if wet[j]:
            lo = j
        elif k0 - j > 0 and not wet[max(0, j - gap):j + 1].any():
            break
    j = k0
    while j < len(ts) - 1:
        j += 1
        if wet[j]:
            hi = j
        elif not wet[j:min(len(ts), j + gap + 1)].any():
            break
    t_c = 0.5 * (ts[lo] + ts[hi])
    return (point[0] + t_c * nx_, point[1] + t_c * ny_), float(ts[hi] - ts[lo] + grid.dx)


def _side(grid, point, direction):
    """Boolean array: True where the cell centre is downstream of the interface plane."""
    X, Y = np.meshgrid(grid.x_centers(), grid.y_centers())
    return ((X - point[0]) * direction[0] + (Y - point[1]) * direction[1]) > 0.0


def _scatter(target, src_grid, values, mask, how):
    """Scatter the cells of `src_grid` selected by `mask` into `target` (Grid) with max/min."""
    r, c = np.nonzero(mask & np.isfinite(values))
    if not len(r):
        return
    x = src_grid.x0 + (c + 0.5) * src_grid.dx
    y = src_grid.y0 - (r + 0.5) * src_grid.dx
    tc = np.floor((x - target.x0) / target.dx).astype(int)
    tr = np.floor((target.y0 - y) / target.dx).astype(int)
    ok = (tr >= 0) & (tr < target.ny) & (tc >= 0) & (tc < target.nx)
    tr, tc, v = tr[ok], tc[ok], values[r, c][ok]
    if how == "max":
        cur = np.where(np.isfinite(target.data), target.data, -np.inf)
        np.maximum.at(cur, (tr, tc), v)
        target.data[:] = np.where(np.isfinite(cur), cur, np.nan)
    else:
        cur = np.where(np.isfinite(target.data), target.data, np.inf)
        np.minimum.at(cur, (tr, tc), v)
        target.data[:] = np.where(np.isfinite(cur), cur, np.nan)


def merge_results(fine_grid, fine_arrays, coarse_grid, coarse_arrays, point, direction,
                  keys=("depth", "velocity", "wse", "arrival")):
    """Merge onto a grid at the coarse cell size covering both. Fine cells upstream of the
    interface plane, coarse cells downstream. Returns (Grid, {kind: array})."""
    dx = coarse_grid.dx
    x0 = min(fine_grid.x0, coarse_grid.x0)
    y0 = max(fine_grid.y0, coarse_grid.y0)
    x1 = max(fine_grid.x0 + fine_grid.nx * fine_grid.dx, coarse_grid.x0 + coarse_grid.nx * dx)
    y1 = min(fine_grid.y0 - fine_grid.ny * fine_grid.dx, coarse_grid.y0 - coarse_grid.ny * dx)
    # keep the coarse grid's alignment
    x0 = coarse_grid.x0 - math.ceil((coarse_grid.x0 - x0) / dx - 1e-9) * dx
    y0 = coarse_grid.y0 + math.ceil((y0 - coarse_grid.y0) / dx - 1e-9) * dx
    nx = int(math.ceil((x1 - x0) / dx - 1e-9))
    ny = int(math.ceil((y0 - y1) / dx - 1e-9))
    grid = Grid(np.full((ny, nx), np.nan), x0, y0, dx, np.nan, coarse_grid.crs_wkt)
    out = {}
    up_f = ~_side(fine_grid, point, direction)
    dn_c = _side(coarse_grid, point, direction)
    for k in keys:
        tgt = grid.like(np.full((ny, nx), np.nan), np.nan)
        how = "min" if k == "arrival" else "max"
        _scatter(tgt, fine_grid, fine_arrays[k], up_f, how)
        _scatter(tgt, coarse_grid, coarse_arrays[k], dn_c, how)
        out[k] = tgt.data
    return grid, out


def _series(gauges, name):
    g = gauges.get(name)
    if g is None:
        return None
    t, q = np.asarray(g[0], float), np.asarray(g[1], float)
    k = np.concatenate([[True], np.diff(t) > 1e-6])
    return t[k], q[k]


def run_nested(dam, dam_xy, st, terrain_src, writer, out_folder, inflow_xy=None,
               fine_cell_m=3.0, reach_mi=1.5, margin=0.3, fine_half_width_mi=0.35,
               progress=None, log=None, cancel=None):
    """Nested run for one dam. `st.cell_size_m` is the coarse cell size. Returns NestedResult."""
    progress = progress or (lambda p, m="": None)
    log = log or (lambda m: None)
    cancel = cancel or (lambda: False)
    if fine_cell_m >= st.cell_size_m:
        raise brmod.BreachInputError("the fine cell size ({} m) must be smaller than the coarse "
                                     "one ({} m)".format(fine_cell_m, st.cell_size_m))
    if reach_mi <= 0.05:
        raise brmod.BreachInputError("the fine reach must be longer than 0.05 mi")
    os.makedirs(out_folder, exist_ok=True)
    out = NestedResult()
    out.folder = os.path.join(out_folder, "Nested")
    os.makedirs(out.folder, exist_ok=True)
    reach_m = reach_mi * MILE

    base = copy.copy(st)
    base.tier = workflow.TIER_LISFLOOD
    base.keep_arrays = True
    base.animation = False
    base.snapshot_frames = 0
    base.resume = False

    # 1. where the interface is: the flow path the fine run will follow
    pf = copy.copy(base)
    pf.cell_size_m = fine_cell_m
    pf.planning_cell_m = max(30.0, st.planning_cell_m)
    pv = workflow.preview(dam, dam_xy, pf, terrain_src, inflow_xy)
    if not pv.get("breach_occurs", True):
        out.warnings.append("The dam does not fail in this flood; nothing to nest.")
        return out
    ipoint, idir, path_len = interface_point(pv["path"], reach_m)
    if path_len < 1.2 * reach_m:
        out.warnings.append(
            "The flow path from the breach is only {:.2f} mi long, not much beyond the fine "
            "reach ({:.2f} mi); a plain run at the coarse cell size may serve as well.".format(
                path_len / MILE, reach_mi))
    log("Interface at {:.0f}, {:.0f}, {:.2f} mi along the flow path".format(
        ipoint[0], ipoint[1], min(reach_m, path_len) / MILE))

    # 2. fine run, gauge at the interface
    fine = copy.copy(base)
    fine.cell_size_m = fine_cell_m
    fine.initial_length_mi = reach_mi * (1.0 + margin)
    fine.max_iterations = 1
    # run past the automatic time: the interface flow is still receding when the front arrives
    fine.sim_time_hr = max(2.0, 2.0 * float(pv.get("sim_time_s") or 0.0) / 3600.0)
    fine.half_width_mi = min(base.half_width_mi, fine_half_width_mi)
    fine.gauges = list(base.gauges) + [{"name": "interface", "xy": ipoint}]
    fine.receptors = None
    fine.gauge_massint_s = min(base.gauge_massint_s, 60.0)
    fine.products = False
    progress(1, "fine run near the dam")
    out.fine = workflow.run_dam(dam, dam_xy, fine, terrain_src, writer,
                                os.path.join(out.folder, "near_dam"), inflow_xy,
                                workflow._span(progress, 1, 45), log, cancel)
    fr = out.fine
    if fr.plan is None or fr.final_si is None:
        out.warnings.append("The dam does not fail in this flood; nothing to nest.")
        return out
    out.warnings += ["fine run: " + w for w in fr.warnings]
    with_q = _series(fr.gauges, "interface")
    if with_q is None or with_q[1].max() <= 0:
        out.warnings.append("The flood does not reach the interface {:.2f} mi below the dam "
                            "inside the fine domain (no discharge at the gauge); the fine run "
                            "is the result.".format(reach_mi))
        out.final = dict(fr.final)
        out.grid, out.final_si = fr.grid, fr.final_si
        return out
    base_q = _series(fr.gauges_base, "interface")

    # 3. hand over the interface flow
    depth = fr.final_si["depth"]
    dmax = float(np.nanmax(np.nan_to_num(depth)))
    ext = wet_extent_at(fr.grid, np.nan_to_num(depth), ipoint, idir,
                        thr_m=max(0.1, 0.1 * dmax))       # the flow-carrying part, not thin overbank water
    if ext is None:
        centre, width = ipoint, max(3 * st.cell_size_m, 30.0)
        out.warnings.append("The fine flood is not wet at the interface plane in the maximum "
                            "depth; the coarse inflow uses a {:.0f} m section.".format(width))
    else:
        centre, width = ext
        width = max(width, 3 * st.cell_size_m)
    t_ref = fr.t_ref_hr * 3600.0
    hy = brmod.Hydrograph(with_q[0], with_q[1], "handed over from the fine run",
                          {"source": "fine LISFLOOD-FP run, gauge across the flow at {:.2f} mi"
                           .format(reach_mi)}, [], t_ref_s=t_ref)
    hy_b = None
    if base_q is not None:
        hy_b = brmod.Hydrograph(base_q[0], base_q[1], "baseline handed over from the fine run",
                                {}, [], t_ref_s=t_ref)
    vol = hy.volume_m3
    breach_vol = float(fr.summary.get("storage_at_failure_acft") or 0.0) * ACFT
    out.interface = {"xy": centre, "path_xy": ipoint, "direction": idir, "width_m": width,
                     "peak_cfs": round(hy.peak_cfs, 0), "volume_acft": round(vol / ACFT, 1),
                     "fine_peak_breach_cfs": fr.summary.get("peak_breach_cfs"),
                     "reach_mi": reach_mi}
    tail = float(with_q[1][-1])
    if tail > 0.05 * float(with_q[1].max()):
        out.warnings.append(
            "The discharge at the interface was still {:.0f} cfs ({:.0f}% of its peak) when the "
            "fine run ended, so the coarse run gets a truncated hydrograph and the downstream "
            "flood is under-estimated. Lengthen the simulation time.".format(
                tail / CFS, 100.0 * tail / float(with_q[1].max())))
    if breach_vol > 0 and vol < 0.85 * breach_vol and fr.summary.get("scenario") != workflow.SCENARIO_FLOOD:
        out.warnings.append(
            "The volume through the interface ({:.0f} ac-ft) is well below the storage at "
            "failure ({:.0f} ac-ft): part of the flood is still in the fine domain, spread "
            "beyond its side edges, or the run ended early.".format(
                vol / ACFT, breach_vol / ACFT))
    if breach_vol > 0 and vol > 1.15 * breach_vol:
        out.warnings.append("The volume through the interface ({:.0f} ac-ft) is more than the "
                            "storage at failure ({:.0f} ac-ft): flood inflow or ambient flow "
                            "adds to it.".format(vol / ACFT, breach_vol / ACFT))

    # 4. coarse run from the interface
    # a stand-in "dam" at the interface: the run only needs its ID and a point; the top of dam
    # is read from the DEM there
    cdam = brmod.DamData(dam_id="{}_downstream".format(dam.dam_id), dam_height_ft=10.0,
                         storage_top_acft=max(1000.0, 10 * vol / ACFT), top_of_dam_ft=None)
    coarse = copy.copy(base)
    coarse.scenario = workflow.SCENARIO_SUNNY
    coarse.flood = None
    coarse.ambient_cfs = 0.0
    coarse.user_hydrograph_obj = hy
    coarse.user_baseline_obj = hy_b
    coarse.inflow_width_m = width
    coarse.breach_xy = None
    coarse.gauges = list(base.gauges)
    coarse.edits = copy.deepcopy(base.edits) if base.edits is not None else None
    coarse.products = False
    progress(46, "coarse run downstream")
    out.coarse = workflow.run_dam(cdam, centre, coarse, terrain_src, writer,
                                  os.path.join(out.folder, "downstream"), centre,
                                  workflow._span(progress, 46, 90), log, cancel)
    cr = out.coarse
    out.warnings += ["coarse run: " + w for w in cr.warnings if "handed over" not in w and "Top of dam not given" not in w]
    if cr.final_si is None:
        out.warnings.append("The coarse run produced no results.")
        return out

    # 5. merge
    progress(91, "merging")
    grid, fin = merge_results(fr.grid, fr.final_si, cr.grid, cr.final_si, ipoint, idir)
    out.grid, out.final_si = grid, fin
    if fr.base_si is not None and cr.base_si is not None:
        _g, bas = merge_results(fr.grid, fr.base_si, cr.grid, cr.base_si, ipoint, idir)
        out.base_si = bas
    if base.write_final:
        for kind, arr in fin.items():
            p = os.path.join(out_folder, workflow.FINAL_NAMES[kind])
            writer(grid.like(workflow._to_us(kind, arr), np.nan), p)
            out.final[kind] = p
        if out.base_si is not None:
            for kind, arr in out.base_si.items():
                p = os.path.join(out_folder, workflow.BASELINE_NAMES[kind])
                writer(grid.like(workflow._to_us(kind, arr), np.nan), p)
                out.baseline[kind] = p
            thr = [t * FT for t in st.incr_thresholds_ft]
            inc_d, inc_w, _stats = floodmod.incremental(fin["depth"], out.base_si["depth"],
                                                        fin["wse"], out.base_si["wse"], thr)
            for kind, arr in (("depth", inc_d), ("wse", inc_w)):
                p = os.path.join(out_folder, workflow.INCREMENT_NAMES[kind])
                writer(grid.like(workflow._to_us(kind, arr), np.nan), p)
                out.increment[kind] = p
    d = fin["depth"]
    cell_ac = grid.dx ** 2 / 4046.8564224
    out.summary = {
        "dam_id": dam.dam_id, "final_model": "LISFLOOD-FP nested ({:g} m near the dam, {:g} m "
        "downstream)".format(fine_cell_m, st.cell_size_m),
        "flooded_acres": round(float(np.isfinite(d).sum()) * cell_ac, 1),
        "max_depth_ft": round(float(np.nanmax(d)) / FT, 2) if np.isfinite(d).any() else 0,
        "max_velocity_fps": (round(float(np.nanmax(fin["velocity"])) / FT, 2)
                             if np.isfinite(fin["velocity"]).any() else 0),
        "latest_arrival_hr": (round(float(np.nanmax(fin["arrival"])), 2)
                              if np.isfinite(fin["arrival"]).any() else None),
        "peak_breach_cfs": fr.summary.get("peak_breach_cfs"),
        "interface_peak_cfs": out.interface["peak_cfs"],
        "fine_cells": int(fr.grid.data.size), "coarse_cells": int(cr.grid.data.size),
    }
    out.report_md = _write_report(out, dam, st, fine_cell_m, out_folder)
    progress(100, "done")
    return out


def _write_report(out, dam, st, fine_cell_m, folder):
    i, s = out.interface, out.summary
    lines = ["# Nested breach run: {}".format(dam.dam_id), "",
             "Fine LISFLOOD-FP grid ({:g} m) from the dam to {:.2f} mi, coarse grid ({:g} m) "
             "from there downstream. One-way: the flow through the interface is handed over."
             .format(fine_cell_m, i["reach_mi"], st.cell_size_m), "",
             "| | |", "|---|---|",
             "| Interface | {:.0f}, {:.0f} (flow direction {:.2f}, {:.2f}) |".format(
                 i["path_xy"][0], i["path_xy"][1], i["direction"][0], i["direction"][1]),
             "| Section the coarse inflow is spread over | {:.0f} m |".format(i["width_m"]),
             "| Peak breach outflow (fine run) | {} cfs |".format(s["peak_breach_cfs"]),
             "| Peak flow through the interface | {:,.0f} cfs |".format(i["peak_cfs"]),
             "| Volume through the interface | {:,.1f} ac-ft |".format(i["volume_acft"]),
             "| Cells (fine / coarse) | {:,} / {:,} |".format(s["fine_cells"], s["coarse_cells"]),
             "| Flooded area (merged) | {:,} ac |".format(s["flooded_acres"]),
             "| Maximum depth / velocity | {} ft / {} ft/s |".format(s["max_depth_ft"],
                                                                   s["max_velocity_fps"]),
             "| Latest arrival | {} h after breach initiation |".format(s["latest_arrival_hr"]), ""]
    if out.warnings:
        lines += ["## Check these", ""] + ["- " + w for w in out.warnings] + [""]
    lines += ["## Method and limits", "",
              "- The merged rasters are on the coarse grid: fine cells upstream of the interface "
              "plane are scattered into the coarse cell that contains them (maximum depth, "
              "speed and WSE; earliest arrival). The fine-resolution rasters are in the "
              "`near_dam` folder.",
              "- The interface is a straight plane across the local flow direction. Water "
              "standing in the fine domain when the run ends is not passed on; only the "
              "discharge through the interface is.",
              "- The coarse model cannot send water back into the fine domain.", ""]
    p = os.path.join(out.folder, "Nested_Report.md")
    with open(p, "w") as f:
        f.write("\n".join(lines))
    return p
