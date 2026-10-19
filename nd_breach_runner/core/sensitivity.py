"""Sensitivity / uncertainty runs around a base breach model.

The breach parameters (width, formation time) and the roughness are the
least certain inputs and the regression equations disagree with one another
by a factor of two or more. ND REG_05.2026 asks for a defensible breach, not
a single number, so this runs the base case plus low/high variations and
alternative parameter methods on the *same* domain, and reports:

* per-case peak outflow, flooded area, deepest and fastest water and, with
  receptor layers, what each case reaches;
* rasters of the low / median / high depth envelope, the fastest water and
  the earliest arrival over all cases, ready to classify;
* whether the answer that matters (a structure flooded or not, and the hazard
  class when the classification plugin can be called) changes between the
  low and high cases.

Every case is LISFLOOD-FP only (ANUGA multiplies the run time by the number
of cases), and shares the base run's inflow point and domain so the rasters
line up cell for cell.

v3.4.0 cell-size convergence check (SENS_CELL): the dam is run again on the same
domain at half the cell size (or the next coarser factor that fits the cell
budget, and the report says so). The two depth rasters are compared on the coarse
grid (benchmark.compare_fields: fit index F, depth bias) and the class of the
Runner's own ND 5.2 preview is compared ("class_stable_grid: yes/no"). Every row
also carries that preview class and the deepest / fastest water at the receptors.
The preview has no attributes (every structure habitable, no AADT), so it can
over-classify; the official classification is ND DWR's.
"""
import copy
import csv
import dataclasses
import os

import numpy as np

from . import breach as brmod
from . import impacts as impmod
from . import workflow
from .units import FT

GROUP_WIDTH, GROUP_TIME, GROUP_N, GROUP_METHOD, GROUP_BASE = (
    "breach width", "formation time", "roughness", "method", "base")
GROUP_GRID = "grid"                       # v3.4.0: the cell-size check
DEFAULT_MAX_CELLS = 4000000               # the engineer's cell budget for the finer run
GRID_FACTORS = (0.5, 0.6, 0.7, 0.8)       # half first, then the next coarser that fits


def cell_variant(cell_m, base_cells, max_cells=DEFAULT_MAX_CELLS, factors=GRID_FACTORS):
    """Pure: which finer cell size to run for the convergence check. Half the cell when
    its cell count fits `max_cells`; else the next coarser factor that fits; else none.
    Returns {"ok", "cell_m", "factor", "cells", "note"}."""
    base_cells = max(1, int(base_cells))
    for f in factors:
        cells = int(round(base_cells / (f * f)))
        if cells <= max_cells:
            if f == 0.5:
                note = "half the base cell size ({:g} m -> {:g} m, {:,} cells)".format(
                    cell_m, cell_m * f, cells)
            else:
                note = ("half the cell size would need {:,} cells, over the budget of {:,}; "
                        "the next coarser factor that fits is x{:g} ({:g} m -> {:g} m, {:,} "
                        "cells)".format(int(round(base_cells * 4)), int(max_cells), f, cell_m,
                                        cell_m * f, cells))
            return {"ok": True, "cell_m": round(cell_m * f, 3), "factor": f, "cells": cells,
                    "note": note}
    return {"ok": False, "cell_m": None, "factor": None, "cells": None,
            "note": "no finer grid fits the budget of {:,} cells (the base run has {:,}; even "
                    "x{:g} needs {:,}). Raise the budget or coarsen the base run.".format(
                        int(max_cells), base_cells, factors[-1],
                        int(round(base_cells / factors[-1] ** 2)))}


def fine_on_coarse(coarse_grid, fine_grid, fine_si):
    """The fine run's arrays scattered onto the coarse grid with the nested merge rules
    (maximum depth / velocity / WSE, earliest arrival). Pure."""
    from . import nested
    out = {}
    mask = np.ones(fine_grid.data.shape, bool)
    for k, a in fine_si.items():
        tgt = coarse_grid.like(np.full(coarse_grid.data.shape, np.nan), np.nan)
        nested._scatter(tgt, fine_grid, np.asarray(a, float), mask,
                        "min" if k == "arrival" else "max")
        out[k] = tgt.data
    return out


def compare_grids(coarse_grid, coarse_si, fine_grid, fine_si, thr_ft=1.0):
    """Pure: benchmark.compare_fields of the coarse run ("ours") against the fine run
    ("reference") on the coarse grid, in US units. Returns the metrics dict (fit_index_F,
    depth_ft bias / p90, area bias ...)."""
    from . import benchmark
    fine_c = fine_on_coarse(coarse_grid, fine_grid, fine_si)

    def us(si):
        return {"depth": si["depth"] / FT, "velocity": si["velocity"] / FT,
                "wse": si["wse"] / FT, "arrival": si["arrival"]}
    cell_ac = coarse_grid.dx ** 2 / 4046.8564224
    m = benchmark.compare_fields(us(coarse_si), us(fine_c), cell_ac, thr_ft)
    m["coarse_cell_m"], m["fine_cell_m"] = coarse_grid.dx, fine_grid.dx
    return m


def receptor_maxima(receptors, grid, final_si):
    """Pure: deepest and fastest water at the receptors (ft, ft/s), or (None, None)."""
    if receptors is None or not len(receptors) or final_si is None:
        return None, None
    d = receptors.sample(grid, final_si["depth"])
    v = receptors.sample(grid, final_si["velocity"])
    dmax = float(np.nanmax(d)) / FT if np.isfinite(d).any() else 0.0
    vmax = float(np.nanmax(v)) / FT if np.isfinite(v).any() else 0.0
    return round(dmax, 2), round(vmax, 2)


def preview_class(receptors, grid, final_si, wet_ft=0.1):
    """Pure: the class of the Runner's own ND 5.2 preview (core/nd_hazard.py) for a run,
    from the receptors sampled on its rasters. Every structure is taken as a habitable
    house (no attributes here), roads with unknown AADT, rail with unknown traffic, so
    this can over-classify. Returns (class or "", note)."""
    from . import nd_hazard as nh
    if receptors is None or not len(receptors) or final_si is None:
        return "", "no receptor layers: no preview class"
    d = receptors.sample(grid, final_si["depth"]) / FT
    v = receptors.sample(grid, final_si["velocity"]) / FT
    d = np.where(np.isfinite(d), d, 0.0)
    structs, roads, rails = [], {}, {}
    for i in range(len(receptors)):
        k, ident = receptors.kind[i], str(receptors.ident[i])
        if k == impmod.KIND_STRUCTURE:
            structs.append(nh.Structure(ident, float(d[i]),
                                        float(v[i]) if np.isfinite(v[i]) else None,
                                        x=float(receptors.x[i]), y=float(receptors.y[i])))
        elif k == impmod.KIND_ROAD:
            roads[ident] = max(roads.get(ident, 0.0), float(d[i]))
        elif k == impmod.KIND_RAIL:
            rails[ident] = max(rails.get(ident, 0.0), float(d[i]))
    road_objs = [nh.Road(k, dd) for k, dd in roads.items()]
    rail_objs = [nh.Railroad(k, dd) for k, dd in rails.items()]
    try:
        c = nh.classify(structs, road_objs, rail_objs,
                        settings=nh.Settings(lethality=nh.LETHALITY_CHARTS, wet_ft=wet_ft))
        note = "ND 5.2 preview with the ACER TM 11 charts; no structure attributes (every " \
               "structure a habitable house, unknown AADT / trains)"
    except nh.ChartsNotDigitized:
        c = nh.classify(structs, road_objs, rail_objs,
                        settings=nh.Settings(lethality=nh.LETHALITY_ALL_WET, wet_ft=wet_ft))
        note = "ND 5.2 preview; the charts are not digitized and checked, so every wet " \
               "structure counts as potentially lethal (over-classifies); no attributes"
    return c.hazard_class, note


@dataclasses.dataclass
class Case:
    name: str
    group: str
    width_factor: float = 1.0
    time_factor: float = 1.0
    n_factor: float = 1.0
    method: str = ""              # empty = the base method
    note: str = ""


def default_cases(base_method, width=(0.7, 1.4), time=(0.5, 2.0), manning=(0.75, 1.3),
                  methods=True, has_geometry=True):
    """Low/high variations, then the other parameter methods. The factors are
    typical scatter of the regression equations (Froehlich 2008: width about
    x/÷1.5, time about x/÷2) and of floodplain roughness."""
    cs = [Case("Base", GROUP_BASE)]
    for f in width:
        cs.append(Case("Width x{:g}".format(f), GROUP_WIDTH, width_factor=f))
    for f in time:
        cs.append(Case("Time x{:g}".format(f), GROUP_TIME, time_factor=f))
    for f in manning:
        cs.append(Case("Manning x{:g}".format(f), GROUP_N, n_factor=f))
    if methods:
        for m in (brmod.METHOD_PIPING, brmod.METHOD_OVERTOPPING, brmod.METHOD_F95_PIPING,
                  brmod.METHOD_MLM, brmod.METHOD_VTG, brmod.METHOD_TR60):
            if m == base_method:
                continue
            if m in (brmod.METHOD_MLM, brmod.METHOD_TR60) and not has_geometry:
                continue
            cs.append(Case(brmod.METHOD_LABELS.get(m, m).split(":")[0].strip(), GROUP_METHOD,
                           method=m))
    return cs


def _folder_name(s):
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in s).strip("_") or "case"


def _case_settings(st, case, base_params, plan):
    c = copy.copy(st)
    c.overrides = dataclasses.replace(st.overrides)
    c.tier = workflow.TIER_LISFLOOD
    c.animation = False              # no frames for every case
    c.snapshot_frames = 0
    c.keep_arrays = True
    c.write_final = True
    c.products = False
    c.fixed_plan = plan
    c.gauges = []
    c.dam_record = None              # v3.2.0: a case folder is not a dam folder (no dam.json)
    c.eap_map = "mapbook"            # products are off for cases anyway
    c.manning_n = st.manning_n * case.n_factor
    c.manning_scale = getattr(st, "manning_scale", 1.0) * case.n_factor
    if case.method:
        c.breach_method = case.method
        c.overrides = brmod.BreachOverrides(
            depth_fraction=getattr(st.overrides, "depth_fraction", None))
    else:
        w = base_params.get("avg_breach_width_ft")
        tf = base_params.get("formation_time_hr")
        if case.width_factor != 1.0 and w:
            c.overrides.avg_width_ft = w * case.width_factor
        if case.time_factor != 1.0 and tf:
            c.overrides.formation_hr = tf * case.time_factor
    return c


def _stats(res, st, case, dam):
    s = res.summary or {}
    row = {"case": case.name, "group": case.group, "method": s.get("breach_method", ""),
           "peak_cfs": s.get("peak_breach_cfs"), "flooded_ac": s.get("flooded_acres"),
           "flooded_ac_over_arrival_depth": s.get("flooded_acres_over_arrival_depth"),
           "max_depth_ft": s.get("max_depth_ft"), "max_velocity_fps": s.get("max_velocity_fps"),
           "latest_arrival_hr": s.get("latest_arrival_hr")}
    return row


def _receptor_counts(res, st):
    if st.receptors is None or not len(st.receptors) or res.final_si is None:
        return {}
    summ, _ = impmod.summarize(st.receptors, res.grid, res.final_si, st.arrival_depth_ft * FT)
    out = {"structures_flooded": summ.get("structure", {}).get("flooded", 0),
           "road_km_flooded": summ.get("road", {}).get("flooded", 0.0),
           "rail_km_flooded": summ.get("rail", {}).get("flooded", 0.0),
           "structures_depth_ge_3ft": summ.get("structure", {}).get("depth_ge_3ft", 0)}
    return out


class SensitivityResult:
    def __init__(self):
        self.rows = []
        self.envelope = {}          # name -> path
        self.flags = []
        self.summary_csv = ""
        self.report_html = ""
        self.base = None
        self.folder = ""
        self.warnings = []
        self.grid_check = None      # v3.4.0: {"budget_cells", "base_cells", "pick", "compare"...}
        self.preview_note = ""      # how the ND 5.2 preview class was made


def grid_check_verdict(base_class, fine_class, metrics, base_row, fine_row):
    """Pure: the "class_stable_grid" answer and the flag text of the cell-size check."""
    f = metrics.get("fit_index_F")
    if base_class or fine_class:
        stable = "yes" if base_class == fine_class else "no"
        cls_txt = "preview class {} at {:g} m, {} at {:g} m".format(
            base_class or "-", metrics["coarse_cell_m"], fine_class or "-",
            metrics["fine_cell_m"])
    else:
        stable = "n/a (no receptor layers)"
        cls_txt = "no receptor layers, so no preview class to compare"
    d = metrics.get("depth_ft") or {}
    txt = ("Cell-size check: {:g} m vs {:g} m on the same domain: fit index F = {} (1 = same "
           "extent; above about 0.8 is a close match), flooded {:,} vs {:,} ac, max depth "
           "{} vs {} ft, max velocity {} vs {} ft/s{}; {}. class_stable_grid: {}.".format(
               metrics["coarse_cell_m"], metrics["fine_cell_m"], f,
               base_row.get("flooded_ac") or 0, fine_row.get("flooded_ac") or 0,
               base_row.get("max_depth_ft"), fine_row.get("max_depth_ft"),
               base_row.get("max_velocity_fps"), fine_row.get("max_velocity_fps"),
               ", depth bias {:+.2f} ft (90% within {:.2f} ft) where both are wet".format(
                   d["bias"], d["p90_abs"]) if d else "", cls_txt, stable))
    if stable == "no":
        txt += (" CHANGES THE ANSWER: the grid alone moves the preview class; refine the "
                "cell size (or use the nested / near-dam refine tools) before classifying.")
    elif f is not None and f < 0.8:
        txt += (" The extents differ noticeably between the two grids; the base cell size "
                "may be too coarse for this valley.")
    return stable, txt


def _grid_check_rows(out, base, grid_run, st):
    case, r = grid_run
    rows = {x["case"]: x for x in out.rows}
    base_row, fine_row = rows.get("Base"), rows.get(case.name)
    if base_row is None or fine_row is None:
        return
    m = compare_grids(base.grid, base.final_si, r.grid, r.final_si, st.arrival_depth_ft)
    stable, txt = grid_check_verdict(base_row.get("preview_class", ""),
                                     fine_row.get("preview_class", ""), m, base_row, fine_row)
    fine_row["fit_index_F"] = m["fit_index_F"]
    fine_row["class_stable_grid"] = stable
    base_row["fit_index_F"] = 1.0
    base_row["class_stable_grid"] = stable
    out.grid_check.update({"compare": m, "class_stable_grid": stable, "text": txt,
                           "fine_case": case.name, "fine_folder": os.path.dirname(
                               r.final["depth"]) if r.final else ""})
    out.flags.append(txt)


def run_sensitivity(dam, dam_xy, st, terrain_src, writer, out_folder, inflow_xy=None,
                    cases=None, progress=None, log=None, cancel=None, classify=None,
                    max_cases=None, cell_check=True, max_cells=DEFAULT_MAX_CELLS):
    """`classify(folder) -> str` (optional) returns the hazard class computed
    from the rasters in a case folder; see handoff.make_classifier.
    v3.4.0 `cell_check`: also run the base case at half the cell size (within
    `max_cells`) and report the fit index and whether the preview class holds."""
    progress = progress or (lambda p, m="": None)
    log = log or (lambda m: None)
    cancel = cancel or (lambda: False)
    out = SensitivityResult()
    out.folder = os.path.join(out_folder, "Sensitivity")
    os.makedirs(out.folder, exist_ok=True)

    # the base run: the same domain everywhere else comes from it
    base_st = copy.copy(st)
    base_st.keep_arrays = True
    progress(0, "base run")
    base = workflow.run_dam(dam, dam_xy, base_st, terrain_src, writer, out_folder, inflow_xy,
                            workflow._span(progress, 0, 40), log, cancel)
    out.base = base
    if base.plan is None or base.final_si is None:
        out.warnings.append("The dam does not fail in the base scenario; no sensitivity runs.")
        return out
    plan = dict(base.plan)
    base_params = _load_params(base)
    has_geom = bool(dam.crest_width_ft and dam.us_slope_h and dam.ds_slope_h)
    cases = cases or default_cases(st.breach_method, has_geometry=has_geom)
    cases = [c for c in cases if c.group != GROUP_BASE]
    keep = []
    for c in cases:       # width/time factors need a parametric breach (not the TR-60 peak)
        if not c.method and ((c.width_factor != 1.0 and not base_params.get("avg_breach_width_ft"))
                             or (c.time_factor != 1.0
                                 and not base_params.get("formation_time_hr"))):
            out.warnings.append("{}: skipped; the {} breach method has no breach width or "
                                "formation time to vary.".format(c.name, st.breach_method))
            continue
        keep.append(c)
    cases = keep
    if max_cases:
        cases = cases[:max_cases]
    runs = [(Case("Base", GROUP_BASE), base)]
    n = len(cases)
    for i, case in enumerate(cases):
        if cancel():
            raise workflow.lfmod.Cancelled("cancelled")
        log("=== sensitivity case {}/{}: {}".format(i + 1, n, case.name))
        cst = _case_settings(st, case, base_params, plan)
        folder = os.path.join(out.folder, _folder_name(case.name))
        try:
            r = workflow.run_dam(dam, dam_xy, cst, terrain_src, writer, folder, inflow_xy,
                                 workflow._span(progress, 40 + 55.0 * i / n,
                                                40 + 55.0 * (i + 1) / n), log, cancel)
        except (brmod.BreachInputError,) as e:
            out.warnings.append("{}: {}".format(case.name, e))
            continue
        if r.final_si is None:
            out.warnings.append("{}: the dam does not fail in this case.".format(case.name))
            continue
        runs.append((case, r))
    # v3.4.0: the cell-size convergence run (same domain, finer cells)
    grid_run = None
    out.grid_check = None
    if cell_check:
        pick = cell_variant(st.cell_size_m, base.grid.data.size, max_cells)
        out.grid_check = {"budget_cells": int(max_cells), "base_cells": int(base.grid.data.size),
                          "base_cell_m": float(st.cell_size_m), "pick": pick}
        if not pick["ok"]:
            out.warnings.append("Cell-size check skipped: " + pick["note"])
        else:
            if cancel():
                raise workflow.lfmod.Cancelled("cancelled")
            case = Case("Cell {:g} m".format(pick["cell_m"]), GROUP_GRID, note=pick["note"])
            log("=== sensitivity cell-size check: {} ({})".format(case.name, pick["note"]))
            cst = _case_settings(st, case, base_params, plan)
            cst.cell_size_m = pick["cell_m"]
            folder = os.path.join(out.folder, _folder_name(case.name))
            try:
                r = workflow.run_dam(dam, dam_xy, cst, terrain_src, writer, folder, inflow_xy,
                                     workflow._span(progress, 95, 96), log, cancel)
                if r.final_si is None:
                    out.warnings.append("{}: the dam does not fail in this case.".format(
                        case.name))
                else:
                    grid_run = (case, r)
                    runs.append(grid_run)
            except (brmod.BreachInputError, workflow.lfmod.ModelError) as e:
                out.warnings.append("Cell-size check failed ({}): {}".format(case.name, e))
    # per-case table
    for case, r in runs:
        row = _stats(r, st, case, dam)
        row["cell_m"] = float(r.grid.dx) if r.grid is not None else st.cell_size_m
        row.update(_receptor_counts(r, st))
        row["max_depth_at_receptors_ft"], row["max_velocity_at_receptors_fps"] = \
            receptor_maxima(st.receptors, r.grid, r.final_si)
        row["preview_class"], out.preview_note = preview_class(st.receptors, r.grid, r.final_si)
        if classify is not None:
            try:
                row["hazard_class"] = classify(os.path.dirname(r.final["depth"]))
            except Exception as e:
                row["hazard_class"] = "error: {}".format(str(e)[:80])
        for w in r.warnings:
            if "leaves the base domain" in w:
                row["domain_too_small"] = True
                out.warnings.append("{}: {}".format(case.name, w))
        out.rows.append(row)
    if grid_run is not None:
        _grid_check_rows(out, base, grid_run, st)
    # envelope on the shared grid (the cell-size run is on another grid: left out)
    progress(96, "envelope rasters")
    runs = [(c, r) for c, r in runs if c.group != GROUP_GRID]
    grid = base.grid
    dep = np.stack([np.nan_to_num(r.final_si["depth"], nan=0.0) for _c, r in runs])
    vel = np.stack([np.nan_to_num(r.final_si["velocity"], nan=0.0) for _c, r in runs])
    arr = np.stack([np.where(np.isfinite(r.final_si["arrival"]), r.final_si["arrival"], np.inf)
                    for _c, r in runs])
    wse = np.stack([np.nan_to_num(r.final_si["wse"], nan=-1e9) for _c, r in runs])
    lo, med, hi = dep.min(axis=0), np.median(dep, axis=0), dep.max(axis=0)

    def put(name, a, scale=1.0):
        p = os.path.join(out.folder, name)
        writer(grid.like(np.where(a > 1e-4, a / scale, np.nan), np.nan), p)
        out.envelope[name] = p

    put("Sensitivity Depth (Low).tif", lo, FT)
    put("Sensitivity Depth (Median).tif", med, FT)
    put("Sensitivity Depth (High).tif", hi, FT)
    p = os.path.join(out.folder, "Sensitivity Velocity (Max).tif")
    writer(grid.like(np.where(hi > 1e-4, vel.max(axis=0) / FT, np.nan), np.nan), p)
    out.envelope["Sensitivity Velocity (Max).tif"] = p
    earliest = arr.min(axis=0)
    p = os.path.join(out.folder, "Sensitivity Arrival (Earliest).tif")
    writer(grid.like(np.where(np.isfinite(earliest), earliest, np.nan), np.nan), p)
    out.envelope["Sensitivity Arrival (Earliest).tif"] = p
    p = os.path.join(out.folder, "Sensitivity WSE (Max).tif")
    writer(grid.like(np.where(hi > 1e-4, wse.max(axis=0) / FT, np.nan), np.nan), p)
    out.envelope["Sensitivity WSE (Max).tif"] = p
    cell_ac = grid.dx ** 2 / 4046.8564224
    thr = st.arrival_depth_ft * FT
    lo_ac, hi_ac = float((lo >= thr).sum()) * cell_ac, float((hi >= thr).sum()) * cell_ac
    out.flags.append("Flooded area deeper than {:g} ft: {:,.0f} ac in the lowest case, {:,.0f} ac "
                     "in the highest (base {:,.0f}).".format(
                         st.arrival_depth_ft, lo_ac, hi_ac,
                         float((dep[0] >= thr).sum()) * cell_ac))
    sc = [r.get("structures_flooded") for r in out.rows if r.get("structures_flooded") is not None]
    if sc:
        lo_s, hi_s, b_s = min(sc), max(sc), out.rows[0].get("structures_flooded")
        out.flags.append("Structures flooded: {} to {} across the cases (base {}).".format(
            lo_s, hi_s, b_s))
        if (lo_s == 0) != (hi_s == 0):
            out.flags.append("CHANGES THE ANSWER: some cases flood no structure and others do; "
                             "the breach uncertainty decides whether people are at risk. Review "
                             "the breach parameters and the hazard classification.")
    cls = [r.get("hazard_class") for r in out.rows if r.get("hazard_class")]
    if cls:
        uniq = sorted(set(cls))
        if len(uniq) > 1:
            out.flags.append("CHANGES THE HAZARD CLASS: the cases give {}.".format(
                ", ".join(uniq)))
        else:
            out.flags.append("The hazard class is {} in every case.".format(uniq[0]))
    write_outputs(out, st)
    progress(100, "done")
    return out


def _load_params(base):
    import json
    try:
        with open(base.report_json) as f:
            return json.load(f).get("hydrograph", {})
    except (OSError, ValueError):
        return {}


CSV_FIELDS = ["case", "group", "method", "cell_m", "peak_cfs", "flooded_ac",
              "flooded_ac_over_arrival_depth", "max_depth_ft", "max_velocity_fps",
              "latest_arrival_hr", "structures_flooded", "structures_depth_ge_3ft",
              "road_km_flooded", "rail_km_flooded", "max_depth_at_receptors_ft",
              "max_velocity_at_receptors_fps", "preview_class", "hazard_class",
              "fit_index_F", "class_stable_grid", "domain_too_small"]


def write_outputs(out, st):
    out.summary_csv = os.path.join(out.folder, "Sensitivity_Summary.csv")
    with open(out.summary_csv, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(out.rows)
    head = [f for f in CSV_FIELDS if any(r.get(f) not in (None, "") for r in out.rows)]
    md = ["# Sensitivity of the breach model", "",
          "Every case uses the base run's inflow point and domain; LISFLOOD-FP only.", ""]
    md += ["## What the spread means", ""] + ["- " + x for x in out.flags] + [""]
    if out.warnings:
        md += ["## Check these", ""] + ["- " + x for x in out.warnings] + [""]
    md += ["## Cases", "", "| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    for r in out.rows:
        md.append("| " + " | ".join(str(r.get(h, "")) for h in head) + " |")
    if out.preview_note:
        md += ["", "preview_class: " + out.preview_note + ". The official classification is "
               "ND DWR's; ND Dam Hazard classifies with the structure attributes."]
    gc = getattr(out, "grid_check", None)
    if gc is not None:
        md += ["", "## Cell-size convergence (v3.4.0)", ""]
        pick = gc.get("pick") or {}
        md.append("- Budget {:,} cells (a screening value you choose); base run {:,} cells at "
                  "{:g} m; finer run: {}.".format(gc["budget_cells"], gc["base_cells"],
                                                  gc["base_cell_m"], pick.get("note", "")))
        if gc.get("compare"):
            m = gc["compare"]
            md.append("- " + gc["text"])
            md.append("- Fine run on the coarse grid (nested merge rules: maximum depth per "
                      "coarse cell): {} ac wet at {:g} m, {} ac at {:g} m, both {} ac; hit rate "
                      "{}, false-alarm ratio {}, area bias {} %.".format(
                          m["wet_acres_ours"], m["coarse_cell_m"], m["wet_acres_reference"],
                          m["fine_cell_m"], m["wet_acres_both"], m["hit_rate"],
                          m["false_alarm_ratio"], m["area_bias_pct"]))
            md.append("- The finer run is not part of the envelope rasters (another grid); its "
                      "rasters are in {}.".format(gc.get("fine_folder", "")))
    md += ["", "## Rasters", ""] + ["- {}".format(p) for p in out.envelope.values()]
    from . import products
    out.report_html = os.path.join(out.folder, "Sensitivity_Report.html")
    with open(out.report_html, "w", encoding="utf-8") as f:
        f.write(products.markdown_to_html("\n".join(md), "Sensitivity of the breach model"))
