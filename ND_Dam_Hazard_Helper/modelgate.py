# -*- coding: utf-8 -*-
"""v2.10: model gate - checks of the MODEL RESULTS before they are classified.

A classification is only as good as the flood rasters it reads. The gate looks at the
rasters (and, when the ND Dam Breach Runner made them, at its run report and manifest)
and gives one row per finding, each PASS / WARN / FAIL with the reason:

  edge       the flood at the model edge (boundary.check: the model boundary polygon
             when one is given, else the outer rows / columns of the depth raster; with a
             no-breach baseline the breach's ADDED depth there). WARN when the edge is
             wet (with a baseline: the breach adds >= incremental_ft there); FAIL only
             when you set edge_fail_ft and the (added) depth on the edge exceeds it.
  holes      no-data cells INSIDE the wet area: a no-data gap at most hole_max_gap_cells
             wide with wet cells on both sides (left and right, or above and below) -
             missing terrain cells, tile seams, or a tiny dry island. WARN when there is
             any; FAIL when they are more than hole_fail_fraction of the wet cells.
  mass       the ND Dam Breach Runner's run report (<dam>/_breach_model/run_report.json):
             the LISFLOOD-FP volume (mass balance) error of its last run - WARN above
             mass_warn_pct, FAIL above mass_fail_pct - and every warning the run wrote
             (one WARN row each).
  breach     the largest velocity within breach_radius_ft of the dam point (dam.json via
             damsuite.dam_record, or the legacy dam_location.json): WARN "check the model
             near the breach" above breach_vel_warn_fps. Never FAIL.
  units      checks.check_rasters (depth that looks like water-surface elevation or
             metres, arrival in minutes / seconds, velocity missing where it is wet, CRS):
             its ERROR -> FAIL, WARNING -> WARN, OK -> PASS.
  manifest   the runner's run_manifest.json (damsuite.chain): its own digest
             (manifest_ok; edited -> WARN) and the rasters being classified against it
             (compare_rasters: MATCH -> PASS, DIFFERENT / NOT IN MANIFEST -> WARN, a
             raster that is gone -> FAIL).

A check that cannot be made (no velocity raster, no run report, no dam point ...) is a
SKIP row: it never makes the gate PASS or FAIL. The gate's status is the worst row
(FAIL > WARN > PASS; only SKIP rows -> SKIP).

EVERY threshold is a screening value the engineer chooses (DEFAULTS, with the value used
written in each row); none is a regulation or a published limit. mass_warn_pct = 1 % is
the value the ND Dam Breach Runner itself warns at (its LISFLOOD-FP message); the other
defaults are starting values of this plugin.

gate(target, params) -> {"status", "rows", "lines", "csv", "stop"}; target is a pipeline
cfg (its breach scenarios and baselines), a dict of raster paths, or a dam folder (the
rasters are found by name). With params["stop_on_fail"] (default off) "stop" is True
when the status is FAIL - pipeline_step() then raises, so a run does not classify
rasters that failed the gate. Nothing here has been run in a real QGIS.
"""

import csv
import json
import math
import os

import numpy as np

PASS, WARN, FAIL, SKIP = "PASS", "WARN", "FAIL", "SKIP"
_RANK = {SKIP: 0, PASS: 1, WARN: 2, FAIL: 3}
CSV_FILE = "Model_Gate.csv"
FIELDS = ["Check", "Scenario", "Status", "Value", "Threshold", "Reason"]
RUN_REPORT = "run_report.json"
MODEL_FOLDER = "_breach_model"
MAX_WARNING_ROWS = 25

# screening values (engineer's choice; stated defaults, not requirements)
DEFAULTS = {
    "edge_warn_ft": 0.0,          # edge (added) depth above this -> WARN (0: any wet cell)
    "edge_fail_ft": None,         # edge (added) depth above this -> FAIL (None: never)
    "edge_incr_ft": 1.0,          # with a baseline: what the breach must add on the edge
    "hole_max_gap_cells": 3,      # widest no-data gap (cells) still called a hole
    "hole_fail_fraction": 0.05,   # hole cells / wet cells above this -> FAIL (None: never)
    "mass_warn_pct": 1.0,         # runner's volume error above this -> WARN (runner's own)
    "mass_fail_pct": 5.0,         # ... above this -> FAIL
    "breach_radius_ft": 1000.0,   # "near the breach": within this of the dam point
    "breach_vel_warn_fps": 30.0,  # max velocity there above this -> WARN
    "stop_on_fail": False,        # FAIL stops the classification (pipeline_step)
}
# the same settings as pipeline cfg keys (for the lead: DEFAULTS.update(CFG_DEFAULTS))
CFG_PREFIX = "gate_"
CFG_DEFAULTS = dict({"model_gate": False},
                    **{CFG_PREFIX + k: v for k, v in DEFAULTS.items()})


def params_from_cfg(cfg):
    """The gate parameters of a pipeline cfg (keys gate_<name>; missing -> DEFAULTS)."""
    out = dict(DEFAULTS)
    cfg = cfg or {}
    for k in DEFAULTS:
        if CFG_PREFIX + k in cfg:
            out[k] = cfg[CFG_PREFIX + k]
    return out


def _params(params):
    p = dict(DEFAULTS)
    p.update({k: v for k, v in (params or {}).items() if k in DEFAULTS})
    return p


def _num(v, default=None):
    try:
        f = float(v)
        return f if math.isfinite(f) else default
    except (TypeError, ValueError):
        return default


def _fmt(v, nd=2):
    if v is None:
        return ""
    if isinstance(v, float):
        return "{:.{}f}".format(v, nd).rstrip("0").rstrip(".") if nd else "{:.0f}".format(v)
    return str(v)


def row(check, status, reason, value=None, threshold=None, scenario=""):
    return {"Check": check, "Scenario": scenario or "", "Status": status,
            "Value": _fmt(value) if not isinstance(value, str) else value,
            "Threshold": _fmt(threshold) if not isinstance(threshold, str) else threshold,
            "Reason": reason}


def worst(rows):
    """FAIL > WARN > PASS > SKIP; no rows -> SKIP."""
    best = SKIP
    for r in rows or []:
        s = r.get("Status")
        if _RANK.get(s, 0) > _RANK[best]:
            best = s
    return best


# ============================================================ targets ==
def _targets(target):
    """[{"name", "depth", "vel", "arr", "dv", "base_depth", "base_vel", "base_arr"}],
    dam folder, output folder, boundary layer - from a cfg, a paths dict or a folder."""
    dam = out = None
    boundary = None
    items = []
    if isinstance(target, str):
        from .autodetect import find_rasters
        from .constants import OUTPUT_FOLDER_NAME
        dam = target
        out = os.path.join(target, OUTPUT_FOLDER_NAME)
        f = find_rasters(target)
        if f.get("depth"):
            items.append(dict({k: f.get(k) for k in ("depth", "vel", "arr", "dv", "base_depth",
                                                     "base_vel", "base_arr")}, name="Breach"))
        return items, dam, out, boundary
    target = dict(target or {})
    dam = target.get("dam_folder")
    out = target.get("output_folder")
    boundary = target.get("model_boundary_layer") or target.get("boundary_layer")
    scen = target.get("scenarios")
    if scen:
        by_name = {getattr(s, "name", ""): s for s in scen}
        for s in scen:
            if getattr(s, "role", "breach") != "breach":
                continue
            base = by_name.get(getattr(s, "baseline", None) or "")
            items.append({"name": s.name, "depth": s.depth, "vel": s.velocity,
                          "arr": s.arrival, "dv": s.dv,
                          "base_depth": base.depth if base is not None else None,
                          "base_vel": base.velocity if base is not None else None,
                          "base_arr": base.arrival if base is not None else None})
    elif target.get("depth"):
        items.append({"name": target.get("name") or "Breach",
                      "depth": target.get("depth"),
                      "vel": target.get("vel") or target.get("velocity"),
                      "arr": target.get("arr") or target.get("arrival"),
                      "dv": target.get("dv"), "base_depth": target.get("base_depth"),
                      "base_vel": target.get("base_vel"), "base_arr": target.get("base_arr")})
    return items, dam, out, boundary


def _sampler(path, name):
    if not path or not os.path.isfile(str(path)):
        return None
    from qgis.core import QgsRasterLayer
    from .helpers import RasterSampler
    lyr = QgsRasterLayer(path, name)
    if not lyr.isValid():
        return None
    smp = RasterSampler(lyr)
    return smp if smp.ok else None


def _close(*smps):
    for s in smps:
        if s is not None:
            try:
                s.close()
            except Exception:
                pass


# ============================================================ 1 edge ==
def check_edge(item, p, boundary_layer=None):
    """Flood at the model edge (boundary.check)."""
    from qgis.core import QgsProject
    from . import boundary as BND
    name = item["name"]
    dep = _sampler(item.get("depth"), "Depth")
    if dep is None:
        return [row("edge", SKIP, "no readable depth raster", scenario=name)]
    arr = _sampler(item.get("arr"), "Arrival")
    base = _sampler(item.get("base_depth"), "Baseline depth")
    try:
        res = BND.check(dep, dep.crs, QgsProject.instance().transformContext(),
                        boundary_layer=boundary_layer, base_sampler=base, arr_sampler=arr,
                        min_incr_ft=float(_num(p["edge_incr_ft"], 1.0)))
    except Exception as e:
        return [row("edge", WARN, "model edge not checked ({}) - check by hand whether the "
                    "flood leaves the model".format(e), scenario=name)]
    finally:
        _close(dep, arr, base)
    text = BND.describe(res)
    if not res.get("wet"):
        return [row("edge", PASS, text, 0.0, p["edge_warn_ft"], name)]
    if res.get("baseline") and not res.get("flagged"):
        return [row("edge", PASS, text, res.get("max_incr"), p["edge_incr_ft"], name)]
    # the side the flood reaches last is downstream (a wet upstream / reservoir edge is
    # normal); without arrival times every wet side counts
    sides = res.get("per_side") or {}
    down = None
    with_arr = [(s["arrival"], k) for k, s in sides.items() if s.get("arrival") is not None]
    if with_arr:
        down = max(with_arr)[1]
    use_incr = res.get("baseline")
    key = "max_incr" if use_incr else "max"
    if down is not None:
        val = sides[down].get(key)
    else:
        val = max((s.get(key) or 0.0) for s in sides.values()) if sides else \
            res.get("max_depth")
    val = _num(val, 0.0)
    what = "added depth" if use_incr else "depth"
    where = " on the {} side (reached last - downstream)".format(
        BND.SIDE_NAMES.get(down, down)) if down else ""
    fail = _num(p.get("edge_fail_ft"))
    warn = _num(p.get("edge_warn_ft"), 0.0)
    reason = "{}. Largest {}{}: {:.2f} ft".format(text, what, where, val)
    if fail is not None and val > fail:
        return [row("edge", FAIL, reason + " - above your FAIL value; extend the model "
                    "downstream (or document why the rest does not matter)", val,
                    "{} ft (FAIL)".format(_fmt(fail)), name)]
    if val > warn:
        return [row("edge", WARN, reason + " - consequences beyond the model are not "
                    "evaluated", val, "{} ft".format(_fmt(warn)), name)]
    return [row("edge", PASS, reason, val, "{} ft".format(_fmt(warn)), name)]


# =========================================================== 2 holes ==
def _any_within(mask, g, axis, sign):
    """True where `mask` holds within 1..g cells in one direction along `axis`."""
    out = np.zeros(mask.shape, dtype=bool)
    n = mask.shape[axis]
    for k in range(1, g + 1):
        if k >= n:
            break
        if axis == 1:
            if sign < 0:
                out[:, k:] |= mask[:, :-k]
            else:
                out[:, :-k] |= mask[:, k:]
        else:
            if sign < 0:
                out[k:, :] |= mask[:-k, :]
            else:
                out[:-k, :] |= mask[k:, :]
    return out


def find_holes(sampler, max_gap=3, rows_per_strip=None, max_points=5):
    """(hole cells, wet cells, [(x, y)] of a few holes) of a depth raster: no-data cells
    with wet (> 0) cells within max_gap cells on both sides along a row or a column.
    Read in strips of rows with a halo (about 4 million cells a strip), so memory stays
    small on large rasters."""
    g = max(1, int(max_gap))
    holes = wet_n = 0
    pts = []
    h, w = sampler.ysize, sampler.xsize
    if not rows_per_strip:
        rows_per_strip = max(16, min(1024, 4000000 // max(1, w)))
    for y0 in range(0, h, rows_per_strip):
        y1 = min(h, y0 + rows_per_strip)
        ya, yb = max(0, y0 - g), min(h, y1 + g)
        arr = sampler.window_values(0, ya, w, yb - ya)
        if arr is None:
            continue
        valid = np.isfinite(arr)
        with np.errstate(invalid="ignore"):
            wet = valid & (arr > 0)
        nod = ~valid
        horiz = _any_within(wet, g, 1, -1) & _any_within(wet, g, 1, 1)
        vert = _any_within(wet, g, 0, -1) & _any_within(wet, g, 0, 1)
        hole = nod & (horiz | vert)
        core = slice(y0 - ya, y0 - ya + (y1 - y0))
        hole = hole[core]
        holes += int(hole.sum())
        wet_n += int(wet[core].sum())
        if len(pts) < max_points and hole.any():
            rr, cc = np.nonzero(hole)
            for r, c in list(zip(rr, cc))[:max_points - len(pts)]:
                xs, ys = sampler.cell_centres(np.array([c]), np.array([r + y0]))
                pts.append((float(xs[0]), float(ys[0])))
    return holes, wet_n, pts


def check_holes(item, p):
    name = item["name"]
    dep = _sampler(item.get("depth"), "Depth")
    if dep is None:
        return [row("holes", SKIP, "no readable depth raster", scenario=name)]
    try:
        gap = int(_num(p.get("hole_max_gap_cells"), 3) or 3)
        n, wet, pts = find_holes(dep, gap)
    finally:
        _close(dep)
    if not wet:
        return [row("holes", SKIP, "the depth raster has no wet cell", scenario=name)]
    frac = n / float(wet)
    if not n:
        return [row("holes", PASS, "no no-data gap up to {} cell(s) wide inside the wet area "
                    "({:,} wet cells)".format(gap, wet), 0, "{} cells".format(gap), name)]
    where = "; e.g. at " + ", ".join("{:.1f} {:.1f}".format(x, y) for x, y in pts[:3])
    reason = ("{:,} no-data cell(s) inside the wet area ({:.2%} of {:,} wet cells: gaps up to "
              "{} cells with wet cells on both sides) - missing terrain cells, tile seams "
              "or small dry islands; check the terrain and the result there{}".format(
                  n, frac, wet, gap, where))
    fail = _num(p.get("hole_fail_fraction"))
    if fail is not None and frac > fail:
        return [row("holes", FAIL, reason, n, "{:.2%} of wet cells (FAIL)".format(fail), name)]
    return [row("holes", WARN, reason, n, "any", name)]


# ============================================================ 3 mass ==
def find_run_report(dam_folder=None, depth=None):
    """The runner's run_report.json for these rasters: next to the depth raster
    (<raster folder>/_breach_model/), in the dam folder, or one level down; None."""
    cands = []
    for folder in (os.path.dirname(depth) if depth else None, dam_folder):
        if not folder:
            continue
        cands.append(os.path.join(folder, MODEL_FOLDER, RUN_REPORT))
        cands.append(os.path.join(folder, RUN_REPORT))
    for c in cands:
        if os.path.isfile(c):
            return c
    if dam_folder and os.path.isdir(dam_folder):
        found = []
        for d in os.listdir(dam_folder):
            c = os.path.join(dam_folder, d, MODEL_FOLDER, RUN_REPORT)
            if os.path.isfile(c):
                found.append((os.path.getmtime(c), c))
        if found:
            return max(found)[1]
    return None


def check_run_report(path, p, name=""):
    if not path:
        return [row("mass", SKIP, "no ND Dam Breach Runner run report (_breach_model/"
                    "run_report.json) - mass balance and run warnings not checked (rasters "
                    "from another model: check its own run messages)", scenario=name)]
    try:
        with open(path, encoding="utf-8") as fh:
            rep = json.load(fh)
        if not isinstance(rep, dict):
            raise ValueError("not a JSON object")
    except (OSError, ValueError) as e:
        return [row("mass", WARN, "run report {} not readable: {}".format(path, e),
                    scenario=name)]
    out = []
    runs = rep.get("lisflood_runs") or []
    stats = (runs[-1].get("stats") or {}) if runs and isinstance(runs[-1], dict) else {}
    err = _num(stats.get("max_volume_error_pct"))
    warn, fail = _num(p.get("mass_warn_pct"), 1.0), _num(p.get("mass_fail_pct"))
    thr = "WARN {} % / FAIL {} %".format(_fmt(warn), _fmt(fail) if fail is not None else "-")
    if err is None:
        out.append(row("mass", SKIP, "the run report has no LISFLOOD-FP volume error "
                       "(max_volume_error_pct)", scenario=name))
    else:
        worst_i = _num(stats.get("worst_interval_error_pct"))
        txt = "LISFLOOD-FP volume (mass balance) error of the last run: {} % of the injected " \
              "volume{}".format(_fmt(err, 3), "" if worst_i is None else
                                "; worst interval {} %".format(_fmt(worst_i, 3)))
        final = ((rep.get("summary") or {}).get("final_model") or "")
        if final and "anuga" in str(final).lower():
            txt += " (the final rasters are from ANUGA; the runner reports no ANUGA volume error)"
        if fail is not None and err > fail:
            out.append(row("mass", FAIL, txt + " - above your FAIL value", err, thr, name))
        elif err > warn:
            out.append(row("mass", WARN, txt + " - check cell size / time step", err, thr, name))
        else:
            out.append(row("mass", PASS, txt, err, thr, name))
    summ = rep.get("summary") or {}
    if summ.get("breach_occurs") is False:
        out.append(row("run warnings", WARN, "the run report says NO breach occurs in this run",
                       scenario=name))
    warns = [str(w) for w in (rep.get("warnings") or []) if str(w).strip()]
    for w in warns[:MAX_WARNING_ROWS]:
        out.append(row("run warnings", WARN, "runner: " + w, scenario=name))
    if len(warns) > MAX_WARNING_ROWS:
        out.append(row("run warnings", WARN, "{} more runner warning(s) in {}".format(
            len(warns) - MAX_WARNING_ROWS, path), scenario=name))
    if not warns and summ.get("breach_occurs") is not False:
        out.append(row("run warnings", PASS, "the run report lists no warning ({})".format(
            os.path.basename(os.path.dirname(path)) + "/" + RUN_REPORT), 0, "", name))
    return out


# ========================================================== 4 breach ==
def dam_point(dam_folder):
    """((x, y), crs id, source text) of the dam / breach point, or (None, None, why)."""
    if not dam_folder or not os.path.isdir(dam_folder):
        return None, None, "no dam folder"
    try:
        from .damsuite import dam_record as DR
        rec = DR.load(dam_folder)
    except ValueError as e:
        return None, None, "dam.json not readable: {}".format(e)
    except Exception as e:                   # noqa: BLE001 - the gate never fails on this
        return None, None, "dam record not readable: {}".format(e)
    if rec is None:
        return None, None, "no dam.json / dam_location.json in the dam folder"
    x, y, crs = _num(rec.get("x")), _num(rec.get("y")), rec.get("crs")
    if x is not None and y is not None and crs:
        return (x, y), str(crs), "dam record x / y ({})".format(crs)
    lon, lat = _num(rec.get("lon")), _num(rec.get("lat"))
    if lon is not None and lat is not None:
        return (lon, lat), "EPSG:4326", "dam record lon / lat"
    return None, None, "the dam record has no location"


def check_breach_velocity(item, p, dam_folder):
    name = item["name"]
    pt, crs_id, why = dam_point(dam_folder)
    if pt is None:
        return [row("breach", SKIP, "velocity near the breach not checked: " + why,
                    scenario=name)]
    vel = _sampler(item.get("vel"), "Velocity")
    if vel is None:
        return [row("breach", SKIP, "no readable velocity raster", scenario=name)]
    try:
        from qgis.core import (QgsCoordinateReferenceSystem, QgsCoordinateTransform,
                               QgsPointXY, QgsProject, QgsRectangle)
        from .helpers import ground_distance_xy
        src = QgsCoordinateReferenceSystem(crs_id)
        x, y = pt
        if src.isValid() and vel.crs is not None and vel.crs.isValid():
            xf = QgsCoordinateTransform(src, vel.crs, QgsProject.instance().transformContext())
            if not xf.isShortCircuited():
                q = xf.transform(QgsPointXY(x, y))
                x, y = q.x(), q.y()
        radius_ft = _num(p.get("breach_radius_ft"), 1000.0)
        # 2.18.0: latitude-aware at the dam point - in Web Mercator a plain
        # conversion covered only cos(lat) of the radius (~685 of 1000 ft in
        # ND); in a geographic CRS the radius is an ellipse in degrees
        rx, ry = ground_distance_xy(radius_ft, vel.crs, rect=QgsRectangle(x, y, x, y),
                                    transform_ctx=QgsProject.instance().transformContext())
        r = max(rx, ry)
        win = vel._window_for_bbox(x - rx, y - ry, x + rx, y + ry)
        if win is None:
            return [row("breach", SKIP, "the dam point ({}) is outside the velocity raster"
                        .format(why), scenario=name)]
        c0, r0, w, h = win
        vals = vel.window_values(c0, r0, w, h)
        if vals is None:
            return [row("breach", SKIP, "velocity raster not readable near the dam point",
                        scenario=name)]
        cols = np.arange(c0, c0 + w)
        rows = np.arange(r0, r0 + h)
        cc, rr = np.meshgrid(cols, rows)
        xs, ys = vel.cell_centres(cc.ravel(), rr.ravel())
        if rx != ry and rx > 0 and ry > 0:
            d2 = (((xs - x) / rx) ** 2 + ((ys - y) / ry) ** 2).reshape(vals.shape)
            sel = np.isfinite(vals) & (d2 <= 1.0)
        else:
            d2 = ((xs - x) ** 2 + (ys - y) ** 2).reshape(vals.shape)
            sel = np.isfinite(vals) & (d2 <= r * r)
    finally:
        _close(vel)
    if not sel.any():
        return [row("breach", SKIP, "no velocity value within {} ft of the dam point ({})"
                    .format(_fmt(radius_ft), why), scenario=name)]
    vmax = float(np.nanmax(np.where(sel, vals, np.nan)))
    thr = _num(p.get("breach_vel_warn_fps"), 30.0)
    txt = "largest velocity within {} ft of the dam point ({}): {:.1f} ft/s".format(
        _fmt(radius_ft), why, vmax)
    if vmax > thr:
        return [row("breach", WARN, txt + " - check the model near the breach (mesh / cell "
                    "size, time step, breach geometry)", vmax, "{} ft/s".format(_fmt(thr)),
                    name)]
    return [row("breach", PASS, txt, vmax, "{} ft/s".format(_fmt(thr)), name)]


# =========================================================== 5 units ==
_LEVEL = {"ERROR": FAIL, "WARNING": WARN, "OK": PASS}


def check_units(item):
    from . import checks as CK
    name = item["name"]
    out = []
    try:
        res = CK.check_rasters(item.get("depth"), item.get("vel"), item.get("arr"),
                               item.get("dv"), label="Breach")
        if item.get("base_depth"):
            res += CK.check_rasters(item.get("base_depth"), item.get("base_vel"),
                                    item.get("base_arr"), label="No breach")
    except Exception as e:                   # noqa: BLE001
        return [row("units", WARN, "input checks failed: {}".format(e), scenario=name)]
    for level, text in res:
        out.append(row("units", _LEVEL.get(level, WARN), text, scenario=name))
    if item.get("depth") and not item.get("vel"):
        out.append(row("units", WARN, "no velocity raster: the ACER zones need max depth with "
                       "max velocity - the classification falls back to the depth-only (1D) "
                       "criterion", scenario=name))
    if item.get("depth") and not item.get("arr"):
        out.append(row("units", WARN, "no arrival raster - no warning times, the EAP list is "
                       "not sorted by time", scenario=name))
    return out or [row("units", SKIP, "nothing to check", scenario=name)]


# ======================================================== 6 manifest ==
_ROLE_NAMES = (("depth", "depth"), ("vel", "velocity"), ("arr", "arrival"), ("dv", "dv"),
               ("base_depth", "baseline depth"), ("base_vel", "baseline velocity"),
               ("base_arr", "baseline arrival"))


def check_manifest(item, dam_folder):
    from .damsuite import chain as CH
    name = item["name"]
    path = None
    for folder in (os.path.dirname(item["depth"]) if item.get("depth") else None, dam_folder):
        if folder:
            path = CH.find_runner_manifest(folder)
            if path:
                break
    if not path:
        return [row("manifest", SKIP, "no ND Dam Breach Runner run_manifest.json - the "
                    "rasters' origin is not checked", scenario=name)]
    try:
        man = CH.read_runner_manifest(path)
    except (OSError, ValueError) as e:
        return [row("manifest", WARN, "run manifest {} not readable: {}".format(path, e),
                    scenario=name)]
    out = []
    ok, text = CH.manifest_ok(man)
    out.append(row("manifest", PASS if ok else WARN, "{}: {}".format(
        os.path.relpath(path, dam_folder) if dam_folder else path, text), scenario=name))
    classified = {role: item[k] for k, role in _ROLE_NAMES if item.get(k)}
    for r in CH.compare_rasters(classified, man, dam_folder):
        st = {"MATCH": PASS, "MISSING": FAIL}.get(r["status"], WARN)
        out.append(row("manifest", st, "{} raster {}: {} - {}".format(
            r["role"], os.path.basename(r["classified_path"]), r["status"], r["note"]),
            r["status"], "", name))
    return out


# ============================================================== gate ==
def gate(target, params=None, write=True, progress=None, log=None):
    """All checks for every breach scenario of `target` (pipeline cfg, paths dict, or dam
    folder). Returns {"status", "rows", "lines", "csv", "stop"}. `write`: QA/Model_Gate.csv
    in the output folder (cfg["output_folder"] / <dam>/outputs), when there is one."""
    p = _params(params)
    items, dam, out, boundary = _targets(target)
    rows = []
    if not items:
        rows.append(row("inputs", FAIL, "no depth raster to check"))
    steps = [("units", lambda it: check_units(it)),
             ("edge", lambda it: check_edge(it, p, boundary)),
             ("holes", lambda it: check_holes(it, p)),
             ("mass", lambda it: check_run_report(find_run_report(dam, it.get("depth")), p,
                                                  it["name"])),
             ("breach", lambda it: check_breach_velocity(it, p, dam)),
             ("manifest", lambda it: check_manifest(it, dam))]
    n = max(1, len(items) * len(steps))
    k = 0
    for it in items:
        for label, fn in steps:
            if progress:
                progress(100.0 * k / n, "Model gate: {} ({})".format(label, it["name"]))
            k += 1
            try:
                rows.extend(fn(it))
            except Exception as e:           # noqa: BLE001 - one check never stops the gate
                from .helpers import CancelledByUser
                if isinstance(e, CancelledByUser):
                    raise
                rows.append(row(label, WARN, "check failed: {!r}".format(e),
                                scenario=it["name"]))
    status = worst(rows)
    lines = summary_lines(status, rows)
    path = None
    if write and out:
        try:
            path = write_csv(os.path.join(out, _qa_name()), rows)
        except OSError as e:
            lines.append("Model gate: {} not written ({})".format(CSV_FILE, e))
    if log:
        for ln in lines:
            log(ln)
    stop = status == FAIL and bool(p.get("stop_on_fail"))
    if progress:
        progress(100.0, "Model gate: {}".format(status))
    return {"status": status, "rows": rows, "lines": lines, "csv": path, "stop": stop,
            "params": p}


def _qa_name():
    from .constants import SUBDIR_QA
    return SUBDIR_QA


def summary_lines(status, rows):
    n = {s: sum(1 for r in rows if r["Status"] == s) for s in (FAIL, WARN, PASS, SKIP)}
    lines = [">>> MODEL GATE: {} ({} FAIL, {} WARN, {} PASS, {} not checked) - screening "
             "checks of the model results; every threshold is the engineer's choice".format(
                 status, n[FAIL], n[WARN], n[PASS], n[SKIP])]
    for r in rows:
        if r["Status"] in (FAIL, WARN):
            lines.append(">>> MODEL GATE {}: [{}{}] {}".format(
                r["Status"], r["Check"], " / " + r["Scenario"] if r["Scenario"] else "",
                r["Reason"]))
    return lines


def write_csv(folder, rows):
    """<folder>/Model_Gate.csv (UTF-8 with BOM for Excel). Returns the path."""
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, CSV_FILE)
    try:
        from .helpers import prepare_output_path
        path = prepare_output_path(path)
    except Exception:                        # noqa: BLE001 - plain write without QGIS
        pass
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)
    return path


def read_csv(path):
    with open(path, newline="", encoding="utf-8-sig") as fh:
        return list(csv.DictReader(fh))


def pipeline_step(cfg, log=None, progress=None):
    """For the pipeline (lead): run the gate on a cfg when cfg["model_gate"] is on; log
    its lines; raise RuntimeError when cfg["gate_stop_on_fail"] and the gate FAILs.
    Returns the gate result, or None when the gate is off."""
    if not (cfg or {}).get("model_gate"):
        return None
    res = gate(cfg, params_from_cfg(cfg), write=True, progress=progress, log=log)
    if res["stop"]:
        fails = [r["Reason"] for r in res["rows"] if r["Status"] == FAIL]
        raise RuntimeError("Model gate FAILED - the classification was stopped ('stop on "
                           "FAIL' is on): {}. See QA/{}.".format("; ".join(fails[:3]),
                                                                CSV_FILE))
    return res
