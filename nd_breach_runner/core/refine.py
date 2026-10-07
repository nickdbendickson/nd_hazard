"""v3.4.0 near-dam refine: an FV1 fine grid where receptors sit close to the breach.

LISFLOOD-FP's default "acceleration" (local inertial) solver is meant for gradually varied,
subcritical floodplain flow. Just below a breach the flow is fast and often supercritical, so
the velocities it gives there are not reliable (the depth-velocity charts of ND 5.2 then rest on
a weak number). When a structure, road or rail receptor lies within the flagged distance of the
breach point AND the final rasters come from the ACC run, this option re-runs the first stretch
on a fine grid with the FV1 shock-capturing solver and merges the fine results into the final
rasters the way the nested tool does (fine cells upstream of an interface plane across the flow
replace the coarse cells they fall in: maximum depth, speed and WSE, earliest arrival).

The flagged distance and the fine cell size are the engineer's screening values (defaults 1 mi
and half the main cell), not requirements of ND REG_05.2026. Nothing here has been run against
a real LISFLOOD-FP or in a real QGIS: the tests stub the model.

v3.11.0, from the Elm River Dam 1 run (LISFLOOD-FP 8.1, QGIS 3.34): the FV1 free boundary
brought water INTO the fine box where breach water stood against its upstream edge, growing
exponentially until the box held 6.5e10 m3 against 9.7e6 m3 injected, and the garbage was
merged. So now (1) the edges that face the reservoir are closed walls, (2) the dam line is
raised in the fine DEM so water cannot pond back there, (3) the fine run lasts the hydrograph
plus the travel time to the interface, not the main run's whole clock, and (4) the fine run is
checked (`check_fine`) and the ACC results are kept when it fails.
"""
import copy
import math
import os

import numpy as np

from . import nested
from . import lisflood as lfmod
from .units import FT, MILE

DEFAULT_DIST_MI = 1.0        # the engineer's screening distance (YOUR value)
MARGIN = 0.3                 # the fine domain runs this far past the flagged distance (as nested)
MIN_HALF_WIDTH_MI = 0.35     # nested's fine half-width
FOLDER = "refine_near_dam"   # under <dam folder>/_breach_model/
NOTE = ("Near-dam refine is an engineer's choice, not a requirement of ND REG_05.2026: the "
        "flagged distance and the fine cell size are screening values you choose. The ACC "
        "(local inertial) velocities near a breach are not reliable in fast or supercritical "
        "flow; the FV1 solver is shock-capturing. In LISFLOOD-FP 8.1 a free FV1 edge can bring "
        "water into the model, so the fine run closes the edges that face the reservoir, is "
        "checked before it is merged, and is rejected (the ACC results kept) when it fails.")

# the fine box's edges that face the reservoir are closed when the flow component along that
# axis is at least this (sin 22.5 degrees); the downstream and side edges stay free
UPSTREAM_COMPONENT = 0.38
# the fine run lasts the breach hydrograph plus this many times the time the flood front takes
# to reach the interface at the screening front speed, plus an hour
TRAVEL_FACTOR = 2.0
MIN_FINE_HR = 2.0
# the fine run is rejected when ...
MAX_VOLUME_RATIO = 1.10      # ... the water in the box exceeds what went in (plus the starting
                             # water) by more than this
MAX_LATE_INFLOW_PCT = 2.0    # ... water came in through the edges after the hydrograph, % of it
MAX_DEPTH_HEIGHTS = 2.0      # ... max depth exceeds this many dam heights (plus 20 ft)


def receptors_within(receptors, structures, breach_xy, dist_m):
    """How many receptors lie within `dist_m` of the breach point. Pure.

    receptors: impacts.Receptors or None (points: structures; line samples: road / rail);
    structures: [structures.Structure] (culverts, bridges, notches: lines; counted too).
    Returns {"structures", "road_samples", "rail_samples", "crossings", "total",
    "nearest_ft"} (nearest_ft None when nothing is within the distance)."""
    out = {"structures": 0, "road_samples": 0, "rail_samples": 0, "crossings": 0, "total": 0,
           "nearest_ft": None}
    nearest = np.inf
    bx, by = float(breach_xy[0]), float(breach_xy[1])
    if receptors is not None and len(receptors):
        d = np.hypot(receptors.x - bx, receptors.y - by)
        near = d <= dist_m
        if near.any():
            nearest = min(nearest, float(d[near].min()))
        for key, kind in (("structures", "structure"), ("road_samples", "road"),
                          ("rail_samples", "rail")):
            out[key] = int((near & (receptors.kind == kind)).sum())
    for s in structures or []:
        line = np.asarray(getattr(s, "line", []), float).reshape(-1, 2)
        if not len(line):
            continue
        d = np.hypot(line[:, 0] - bx, line[:, 1] - by)
        if d.min() <= dist_m:
            out["crossings"] += 1
            nearest = min(nearest, float(d.min()))
    out["total"] = out["structures"] + out["road_samples"] + out["rail_samples"] + \
        out["crossings"]
    if np.isfinite(nearest):
        out["nearest_ft"] = round(nearest / FT, 0)
    return out


def fine_cell(st):
    """The fine cell size (m): st.refine_cell_m, or half the main cell when 0 / None."""
    c = float(getattr(st, "refine_cell_m", 0.0) or 0.0)
    return c if c > 0 else 0.5 * float(st.cell_size_m)


def decide(st, final_model, receptors, structures, breach_xy):
    """Whether to refine, and why. Pure. Returns a dict with "refine" (bool), "reason",
    "dist_mi", "fine_cell_m", "solver" and the receptor counts."""
    dist_mi = float(getattr(st, "refine_dist_mi", DEFAULT_DIST_MI) or DEFAULT_DIST_MI)
    info = {"refine": False, "dist_mi": dist_mi, "fine_cell_m": fine_cell(st),
            "solver": lfmod.SOLVER_FV1, "counts": None}
    if not getattr(st, "refine_near", False):
        info["reason"] = "off (REFINE_NEAR not set)"
        return info
    if final_model != "LISFLOOD-FP":
        info["reason"] = ("the final rasters come from {}, not from the LISFLOOD-FP ACC run; "
                          "its velocities near the dam are not the ones in question".format(
                              final_model))
        return info
    if getattr(st, "lisflood_solver", lfmod.SOLVER_ACC) != lfmod.SOLVER_ACC:
        info["reason"] = "the main run already uses the {} solver".format(st.lisflood_solver)
        return info
    if (receptors is None or not len(receptors)) and not structures:
        info["reason"] = ("no structure, road, rail or crossing layer was given, so nothing can "
                          "be flagged within {:g} mi of the breach".format(dist_mi))
        return info
    counts = receptors_within(receptors, structures, breach_xy, dist_mi * MILE)
    info["counts"] = counts
    if counts["total"] == 0:
        info["reason"] = "no receptor lies within {:g} mi of the breach point".format(dist_mi)
        return info
    if info["fine_cell_m"] >= float(st.cell_size_m):
        info["reason"] = ("the fine cell ({:g} m) is not smaller than the main cell ({:g} m)"
                          .format(info["fine_cell_m"], st.cell_size_m))
        return info
    info["refine"] = True
    info["reason"] = ("{} receptor(s) within {:g} mi of the breach (nearest {:,.0f} ft): {} "
                      "structure(s), {} road and {} rail sample(s), {} crossing(s); the ACC "
                      "velocities there are unreliable".format(
                          counts["total"], dist_mi, counts["nearest_ft"] or 0,
                          counts["structures"], counts["road_samples"], counts["rail_samples"],
                          counts["crossings"]))
    return info


def upstream_edges(direction):
    """The edges of an axis-aligned box that face upstream for a flow `direction` (unit
    vector, x east, y north): ("S",) for flow to the north, ("S", "W") for flow to the
    north-east, and so on. A component weaker than UPSTREAM_COMPONENT closes nothing on
    that axis, so the side edges stay free. Pure."""
    dx, dy = float(direction[0]), float(direction[1])
    out = []
    if dy >= UPSTREAM_COMPONENT:
        out.append("S")
    if dy <= -UPSTREAM_COMPONENT:
        out.append("N")
    if dx >= UPSTREAM_COMPONENT:
        out.append("W")
    if dx <= -UPSTREAM_COMPONENT:
        out.append("E")
    return tuple(out)


def fine_sim_time(st, hy, dist_mi, main_sim_time_s):
    """Seconds the fine run lasts: the breach hydrograph, plus TRAVEL_FACTOR times the time the
    flood front needs to cross the fine box at the screening front speed, plus an hour; at
    least MIN_FINE_HR and never more than the main run's clock. Pure."""
    box_m = dist_mi * (1.0 + MARGIN) * MILE
    speed = max(0.05, float(getattr(st, "front_speed_mps", 0.5) or 0.5))
    t = float(hy.duration_s) + TRAVEL_FACTOR * box_m / speed + 3600.0
    return min(float(main_sim_time_s), max(MIN_FINE_HR * 3600.0, t))


def check_fine(stats, dam_height_ft):
    """Whether the fine run's numbers are sane enough to merge. `stats` are the fine run's
    final LISFLOOD-FP statistics (lisflood.read_results). Returns a list of what is wrong;
    empty means fine. Pure."""
    bad = []
    if not stats:
        return ["the fine run reported no statistics"]
    ratio = stats.get("max_domain_volume_ratio")
    init = float(stats.get("initial_water_acft") or 0.0)
    inj = float(stats.get("injected_acft") or 0.0)
    if ratio is not None and inj > 0:
        held = ratio * inj
        if held > MAX_VOLUME_RATIO * (inj + init):
            bad.append("the box held {:,.0f} ac-ft against {:,.0f} ac-ft put in".format(
                held, inj + init))
    late = stats.get("late_boundary_inflow_pct")
    if late is not None and late > MAX_LATE_INFLOW_PCT:
        bad.append("{:.1f}% of the breach volume came in through the edges after the "
                   "hydrograph ended (limit {:g}%)".format(late, MAX_LATE_INFLOW_PCT))
    err = stats.get("max_volume_error_pct")
    if err is not None and err > 5.0:
        bad.append("the mass-balance error is {:.1f}% of the injected volume".format(err))
    depth = stats.get("max_depth_ft")
    limit = MAX_DEPTH_HEIGHTS * float(dam_height_ft or 0.0) + 20.0
    if depth is not None and depth > limit:
        bad.append("the maximum depth is {:,.1f} ft (more than {:g} dam heights + 20 ft = "
                   "{:,.0f} ft)".format(depth, MAX_DEPTH_HEIGHTS, limit))
    return bad


def fine_settings(st, dist_mi, cell_m, sim_time_s, hy, hy_nb):
    """RunSettings copy for the fine FV1 run. Pure (no I/O). `sim_time_s` is how long the fine
    run lasts (fine_sim_time)."""
    import dataclasses
    f = copy.copy(st)
    f.overrides = dataclasses.replace(st.overrides)
    if st.edits is not None:
        f.edits = st.edits.copy()          # the fine run sets its own raised dam line
    f.cell_size_m = float(cell_m)
    f.lisflood_solver = lfmod.SOLVER_FV1
    f.tier = "lisflood"
    f.initial_length_mi = dist_mi * (1.0 + MARGIN)
    f.half_width_mi = min(float(st.half_width_mi), max(MIN_HALF_WIDTH_MI, 0.5 * dist_mi))
    f.max_iterations = 1
    f.sim_time_hr = max(1.0, float(sim_time_s) / 3600.0)
    f.close_upstream_edges = True          # v3.11.0: FV1 free edges import water (see above)
    f.receptors = None
    f.gauges = []
    f.products = False
    f.dam_record = None
    f.refine_near = False                 # no recursion
    f.keep_arrays = True
    f.write_final = True                  # fine-resolution rasters in its own folder
    f.resume = False
    f.animation = False
    f.snapshot_frames = 0
    f.class_stop = False
    f.fixed_plan = None
    f.user_hydrograph_obj = hy
    f.user_baseline_obj = hy_nb
    f.user_hydrograph = None
    f.scenario = "sunny"                  # the hydrographs are given; no routing again
    f.flood = None
    f.eap_map = "mapbook"
    return f


def merge_onto(main_grid, main_arrays, fine_grid, fine_arrays, point, direction,
               keys=("depth", "velocity", "wse", "arrival")):
    """Fine results replace the main results where the fine domain covers, upstream of the
    interface plane through `point` across `direction` (nested's scatter rules: max depth,
    speed and WSE, earliest arrival). Everything else keeps the main run's values. Returns
    ({kind: array on main_grid}, cells replaced)."""
    out = {}
    up_f = ~nested._side(fine_grid, point, direction)
    # main cells to clear: inside the fine domain's bounds and upstream of the plane
    X, Y = np.meshgrid(main_grid.x_centers(), main_grid.y_centers())
    fx0, fy0, fx1, fy1 = fine_grid.bounds
    cover = (X >= fx0) & (X <= fx1) & (Y >= fy0) & (Y <= fy1) & ~nested._side(
        main_grid, point, direction)
    for k in keys:
        tgt = main_grid.like(np.array(main_arrays[k], dtype=float, copy=True), np.nan)
        tgt.data[cover] = np.nan
        how = "min" if k == "arrival" else "max"
        nested._scatter(tgt, fine_grid, fine_arrays[k], up_f, how)
        out[k] = tgt.data
    return out, int(cover.sum())


MERGE_KEYS = ("depth", "velocity", "wse", "arrival")


def _merge_keep(main_grid, main_arrays, fine_grid, fine_arrays, point, direction, keys):
    """merge_onto for `keys`; every other kind of MERGE_KEYS keeps the main run's values
    (v3.12.0: the ACC velocity when the fine run wrote none). Returns (arrays, cells
    replaced)."""
    out, replaced = merge_onto(main_grid, main_arrays, fine_grid, fine_arrays, point,
                               direction, keys)
    for k in MERGE_KEYS:
        if k not in out and k in main_arrays:
            out[k] = np.array(main_arrays[k], dtype=float, copy=True)
    return out, replaced


def zone_stats(grid, arrays, breach_xy, dist_m):
    """Maximum depth (ft) and velocity (ft/s) within `dist_m` of the breach. Pure."""
    X, Y = np.meshgrid(grid.x_centers(), grid.y_centers())
    zone = np.hypot(X - breach_xy[0], Y - breach_xy[1]) <= dist_m
    d = np.where(zone, arrays["depth"], np.nan)
    v = np.where(zone, arrays["velocity"], np.nan)
    return {"max_depth_ft": round(float(np.nanmax(d)) / FT, 2) if np.isfinite(d).any() else 0.0,
            "max_velocity_fps": (round(float(np.nanmax(v)) / FT, 2)
                                 if np.isfinite(v).any() else 0.0),
            "wet_cells": int(np.isfinite(d).sum())}


def run_refine(dam, dam_xy, st, raw_terrain, writer, model_dir, hy, hy_nb, inflow_xy, path,
               sim_time_s, dem, fin, base_arrays, decision, progress=None, log=None,
               cancel=None, crest_line=None):
    """Run the fine FV1 stretch and merge it into `fin` (and `base_arrays`). Returns
    (fin_merged, base_merged or None, info). `raw_terrain` is the terrain BEFORE the run's
    DEM edits were wrapped on (run_dam wraps them again). Raises what workflow.run_dam raises."""
    from . import workflow
    progress = progress or (lambda p, m="": None)
    log = log or (lambda m: None)
    dist_mi = decision["dist_mi"]
    cell_m = decision["fine_cell_m"]
    fine_s = fine_sim_time(st, hy, dist_mi, sim_time_s)
    fst = fine_settings(st, dist_mi, cell_m, fine_s, hy, hy_nb if base_arrays is not None
                        else None)
    fst.crest_line_xy = crest_line          # the dam line the main run raised (or traced)
    folder = os.path.join(model_dir, FOLDER)
    log("Near-dam refine: {}. Running the first {:.2f} mi below the breach on a {:g} m grid "
        "with the FV1 solver for {:.1f} h (the main run is {:.1f} h).".format(
            decision["reason"], fst.initial_length_mi, cell_m, fine_s / 3600.0,
            sim_time_s / 3600.0))
    fr = workflow.run_dam(dam, dam_xy, fst, raw_terrain, writer, folder, inflow_xy,
                          progress, log, cancel)
    info = {"applied": False, "reason": decision["reason"], "dist_mi": dist_mi,
            "fine_cell_m": cell_m, "main_cell_m": float(st.cell_size_m),
            "solver": lfmod.SOLVER_FV1, "counts": decision.get("counts"), "folder": folder,
            "note": NOTE}
    info["fine_sim_time_hr"] = round(fine_s / 3600.0, 2)
    if fr.final_si is None or fr.grid is None:
        info["warning"] = "the fine run gave no results; the ACC results are kept"
        return fin, base_arrays, info
    fstats = dict(getattr(fr, "lisflood_stats", None) or {})
    info["fine_stats"] = {k: fstats.get(k) for k in (
        "max_depth_ft", "max_velocity_fps", "injected_acft", "in_domain_acft",
        "max_domain_volume_ratio", "late_boundary_inflow_acft", "late_boundary_inflow_pct",
        "max_volume_error_pct", "sim_time_hr")}
    problems = check_fine(fstats, getattr(dam, "dam_height_ft", None))
    if problems:
        info["rejected"] = problems
        info["warning"] = ("the fine FV1 run was rejected and NOT merged, the ACC results are "
                           "kept everywhere: " + "; ".join(problems))
        log("Near-dam refine: " + info["warning"])
        return fin, base_arrays, info
    ipoint, idir, plen = nested.interface_point(path, dist_mi * MILE)
    # v3.12.0: LISFLOOD-FP 8.1 writes no maximum velocity for FV1 (.maxVc), and the merge then
    # put 0 ft/s into every wet cell of the fine box (Eddy County runs, Elm River Dam 1). When
    # the fine run has no velocity, only depth, WSE and arrival are merged and the ACC
    # velocities stay - unreliable in fast flow, but not zero.
    keys = MERGE_KEYS
    if fstats.get("velocity_written") is False:
        keys = tuple(k for k in MERGE_KEYS if k != "velocity")
        info["velocity_kept_acc"] = True
        info["warning"] = ("LISFLOOD-FP wrote no maximum velocity for the FV1 fine run, so "
                           "the ACC velocities are kept near the dam; depth, water surface and "
                           "arrival come from the fine run")
        log("Near-dam refine: " + info["warning"])
    merged, replaced = _merge_keep(dem, fin, fr.grid, fr.final_si, ipoint, idir, keys)
    before = zone_stats(dem, fin, dam_xy, dist_mi * MILE)
    after = zone_stats(dem, merged, dam_xy, dist_mi * MILE)
    base_m = None
    if base_arrays is not None and fr.base_si is not None:
        base_m, _ = _merge_keep(dem, base_arrays, fr.grid, fr.base_si, ipoint, idir, keys)
    elif base_arrays is not None:
        info["warning"] = "; ".join(w for w in (
            info.get("warning"), "the fine run has no no-breach baseline; the baseline and "
            "incremental rasters keep the ACC results near the dam") if w)
        base_m = base_arrays
    # the fine domain is deliberately short: the flood leaving its downstream edge is expected
    fine_warn = [w for w in fr.warnings if not w.startswith("Stopped after 1 runs")
                 and "handed over" not in w and "still advancing" not in w]
    info.update({"applied": True, "interface_xy": [round(ipoint[0], 1), round(ipoint[1], 1)],
                 "interface_direction": [round(idir[0], 3), round(idir[1], 3)],
                 "fine_cells": int(fr.grid.data.size), "cells_replaced": replaced,
                 "zone_before_acc": before, "zone_after_fv1": after,
                 "fine_warnings": fine_warn, "fine_report": fr.report_json,
                 "summary": "{} within {:g} mi of the breach replaced by an FV1 "
                            "fine-grid run ({:g} m cells); {} receptors inside. Zone maximum "
                            "velocity {} -> {} ft/s, depth {} -> {} ft.".format(
                                # v3.12.0: say so when the ACC velocities were kept
                                "Depths (ACC velocities kept: FV1 wrote none)"
                                if info.get("velocity_kept_acc") else "ACC velocities",
                                dist_mi, cell_m, (decision.get("counts") or {}).get("total", 0),
                                before["max_velocity_fps"], after["max_velocity_fps"],
                                before["max_depth_ft"], after["max_depth_ft"])})
    log("Near-dam refine: " + info["summary"])
    return merged, base_m, info


def report_lines(info):
    """Markdown lines for the run report."""
    if not info:
        return []
    lines = ["## Near-dam refine (v3.4.0)", ""]
    if info.get("rejected"):
        fs = info.get("fine_stats") or {}
        lines += ["**Rejected: the fine FV1 run failed its checks, so the ACC results are "
                  "kept everywhere.** The run was made because {}.".format(
                      info.get("reason", "")), ""]
        lines += ["- " + p for p in info["rejected"]]
        lines += ["", "| Fine run | |", "|---|---|"]
        for k, label in (("sim_time_hr", "Simulated (h)"), ("injected_acft", "Put in (ac-ft)"),
                         ("in_domain_acft", "In the box at the end (ac-ft)"),
                         ("max_domain_volume_ratio", "Largest box volume / put in"),
                         ("late_boundary_inflow_acft", "Came in through the edges after the "
                                                       "hydrograph (ac-ft)"),
                         ("max_depth_ft", "Max depth (ft)"),
                         ("max_velocity_fps", "Max velocity (ft/s)")):
            if fs.get(k) is not None:
                lines.append("| {} | {:,} |".format(label, fs[k]))
        lines += ["", "Fine run folder: {}.".format(info.get("folder", "")), "", info["note"], ""]
        return lines
    if not info.get("applied"):
        lines += ["Not applied: {}.".format(info.get("reason", "")), ""]
        if info.get("warning"):
            lines += ["- " + info["warning"], ""]
        return lines
    b, a = info["zone_before_acc"], info["zone_after_fv1"]
    lines += [info["summary"], "",
              "| | ACC main run | FV1 fine run (merged) |", "|---|---|---|",
              "| Cell size | {:g} m | {:g} m |".format(info["main_cell_m"], info["fine_cell_m"]),
              "| Max depth within {:g} mi | {} ft | {} ft |".format(info["dist_mi"],
                                                                     b["max_depth_ft"],
                                                                     a["max_depth_ft"]),
              "| Max velocity within {:g} mi | {} ft/s | {} ft/s |".format(
                  info["dist_mi"], b["max_velocity_fps"], a["max_velocity_fps"]),
              "| Cells replaced on the main grid | | {:,} |".format(info["cells_replaced"]),
              "", "Interface plane at {:.0f}, {:.0f}; fine results upstream of it, main results "
              "downstream (nested merge rules). Fine-resolution rasters: {}.".format(
                  info["interface_xy"][0], info["interface_xy"][1], info["folder"]),
              "", info["note"], ""]
    if info.get("warning"):
        lines += ["- " + info["warning"], ""]
    return lines
