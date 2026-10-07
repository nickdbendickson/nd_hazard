"""Per-dam simulation driver and parallel batch runner.  numpy + subprocess only.

The QGIS tool supplies two callbacks, so this module never touches GDAL/QGIS:
    load_dem(center_xy, half_size_m, cell) -> (dem, gt)     conditioned terrain, EPSG:5070
    load_manning(gt, shape) -> array or None

For one dam `simulate()`:
  1. picks the cell size (automatic by reservoir size, or fixed);
  2. loads terrain, traces the downstream path and masks a corridor around it
     (everything else is a wall);
  3. runs the engine (LISFLOOD-FP ACC / FV1, built-in, or built-in with the
     reservoir in the grid);
  4. while the flood is still spreading at the end, runs longer (the built-in
     engine simply continues; LISFLOOD-FP is re-run);
  5. if the flood reaches the corridor wall, widens the corridor; if it
     reaches the terrain edge, loads a larger terrain clip; then repeats;
  6. checks every other dam line inside the flood: overtopped dams are
     reported, and (cascade = "breach") their own breach hydrograph is added,
     starting when the flood reaches them, and the run is repeated.
"""
import os
import shutil
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np

from . import domain as dm, solver_io as sio, triage as tg, reservoir as rsv
from .fv_engine import Engine
try:
    from ..damsuite.keepawake import keep_awake
except ImportError:                                   # core/ itself on sys.path (tests)
    from damsuite.keepawake import keep_awake
from .gridutil import xy_to_rc, rc_to_xy, rasterize_lines, rasterize_polygons, NODATA, dilate

ENGINES = ("lisflood-acc", "lisflood-fv1", "builtin", "builtin-reservoir")
BUILDING_MODES = ("none", "raise", "manning")
M3S_PER_CFS = 0.3048 ** 3                  # 1 ft3/s = 0.028316846592 m3/s
FT = 0.3048

# buildings / baseline defaults are engineer-chosen screening values (YOUR values), not standards
DEFAULTS = dict(engine="lisflood-acc", exe=None, cell="auto", corridor=True,
                corridor_hw_m=600.0, radius_m=1500.0, radius_max_m=12000.0,
                sim_s=None, sim_max_s=96 * 3600.0, fv1_save_s=120.0,
                manning_default=0.05, n_mult=1.0, timeout_s=3600.0,
                cascade="flag", max_cascade=3, max_attempts=8, workdir=None, keep=False,
                threads_per_run=1, dem_cell=1.0, wet_m=0.05,
                baseline=False, baseline_cfs=0.0,
                buildings="none", bld_polys=None, building_raise_m=10.0 * FT, building_n=0.15,
                trace_grow=False)    # v0.20.0: clip growth from the trace is opt-in (see simulate)


class Cancelled(Exception):
    pass


# ---------------------------------------------------------------- LISFLOOD-FP
def _stop(p, grace_s=10.0):
    """Terminate the solver, kill it if it has not ended after `grace_s`, and wait for it, so
    that its files are released before the run folder is removed (Windows locks open files)."""
    try:
        p.terminate()
        p.wait(timeout=grace_s)
    except Exception:                                            # noqa
        try:
            p.kill()
            p.wait(timeout=grace_s)
        except Exception:                                        # noqa
            pass


def _tail(path, chars=300):
    try:
        with open(path, errors="replace") as f:
            return f.read()[-chars:]
    except OSError:
        return ""


def run_lisflood(exe, par, cwd, timeout_s, cancel=None, threads=1):
    """Run LISFLOOD-FP in `cwd`. v0.11.1: its output goes to <cwd>/lisflood.log instead of a
    pipe that was only read at the end - a long run fills the pipe and the solver then waits
    for ever (until the timeout). Cancel / timeout now wait for the process to end."""
    env = dict(os.environ, OMP_NUM_THREADS=str(max(int(threads), 1)))
    log_path = os.path.join(cwd, "lisflood.log")
    t0 = time.time()
    with open(log_path, "w") as logf:
        p = subprocess.Popen([exe, par], cwd=cwd, stdout=logf, stderr=subprocess.STDOUT, env=env)
        while p.poll() is None:
            if cancel is not None and cancel():
                _stop(p)
                raise Cancelled("cancelled")
            if time.time() - t0 > timeout_s:
                _stop(p, grace_s=2.0)
                raise TimeoutError(f"solver exceeded {timeout_s / 60:.0f} min")
            time.sleep(0.5)
    if p.returncode != 0:
        raise RuntimeError(f"solver exit {p.returncode}: {_tail(log_path)}")


def _lisflood_case(dem, gt, cell, sources, man, sim_s, o, cancel, tag):
    ny, nx = dem.shape
    xll, yll = gt[0], gt[3] + ny * gt[5]
    run = tempfile.mkdtemp(prefix=f"lf_{tag}_", dir=o["workdir"])
    try:
        os.makedirs(os.path.join(run, "out"))
        sio.write_ascii_grid(os.path.join(run, "dem.asc"), dem, xll, yll, cell)
        if man is not None:
            sio.write_ascii_grid(os.path.join(run, "n.asc"), man, xll, yll, cell)
        srcs = [dict(pts=[rc_to_xy(gt, r, c) for r, c in s["cells"]], t=s["t"], q=s["q"],
                     start_s=s.get("start_s", 0.0)) for s in sources]
        sio.write_sources(os.path.join(run, "run.bci"), os.path.join(run, "run.bdy"), srcs, cell)
        solver = "fv1" if o["engine"] == "lisflood-fv1" else "acc"
        save = o["fv1_save_s"] if solver == "fv1" else sim_s
        sio.write_par(os.path.join(run, "run.par"), "dem.asc", "run", "out", sim_s, save, solver,
                      o["manning_default"] * o["n_mult"], "n.asc" if man is not None else None)
        run_lisflood(o["exe"], "run.par", run, o["timeout_s"], cancel, o["threads_per_run"])
        res = sio.read_results(os.path.join(run, "out"), "run", solver, save)
        return res
    finally:
        if not o["keep"]:
            shutil.rmtree(run, ignore_errors=True)


# ---------------------------------------------------------------- helpers
def injection_cells(gt, shape, breach_xy, flow_az, bb, hb, cell):
    ny, nx = shape
    xll, yll = gt[0], gt[3] + ny * gt[5]
    pts = sio.injection_points(breach_xy, flow_az, bb, hb, cell, xll, yll, nx, ny)
    return [xy_to_rc(gt, x, y) for x, y in pts]


def _at_border(rc, shape, margin=2):
    """True when cell rc lies within `margin` cells of the grid border."""
    r, c = rc
    return r < margin or c < margin or r >= shape[0] - margin or c >= shape[1] - margin


def _hydro_end(t, q):
    q = np.asarray(q); t = np.asarray(t)
    nz = np.where(q > 1e-6)[0]
    return float(t[nz[-1]]) if len(nz) else float(t[-1])


def simulate(case, load_dem, load_manning=None, opts=None, log=None, cancel=None):
    """Run one dam.  Returns a result dict (never raises for model problems).
    v0.20.0: the computer is kept awake while it runs (damsuite.keepawake, beside the solver:
    LISFLOOD-FP stays this plugin's own child, so Cancel still stops it)."""
    o = dict(DEFAULTS); o.update(opts or {})
    what = "the built-in flood engine" if o["engine"].startswith("builtin") else "LISFLOOD-FP"
    with keep_awake(what, log=log):
        return _simulate(case, load_dem, load_manning, opts, log, cancel)


def _simulate(case, load_dem, load_manning=None, opts=None, log=None, cancel=None):
    o = dict(DEFAULTS); o.update(opts or {})
    log = log or (lambda m: None)
    notes = []
    t_start = time.time()
    res = {"dam_id": case["dam_id"], "status": "FAILED", "engine": o["engine"], "notes": notes,
           "cascade": [], "attempts": 0}
    try:
        if o["engine"].startswith("lisflood") and not (o["exe"] and os.path.exists(o["exe"])):
            raise FileNotFoundError("LISFLOOD-FP executable not found")
        cell = (dm.auto_cell(case.get("vw_m3"), case.get("bavg_m"), o["dem_cell"])
                if o["cell"] == "auto" else float(o["cell"]))
        radius, corr_hw = float(o["radius_m"]), float(o["corridor_hw_m"])
        use_corr = bool(o["corridor"])
        extra = []                                 # cascade sources
        reservoir_mode = o["engine"] == "builtin-reservoir"
        sim_s = None
        last_valid, grew = 0, False
        first_load, trace_grown = True, None
        for attempt in range(1, o["max_attempts"] + 1):
            res["attempts"] = attempt
            if cancel is not None and cancel():
                raise Cancelled("cancelled")
            # 0.15 audit: the trace-driven enlargement reloads inside this loop so it does not
            # use up max_attempts (kept for corridor widening, clip growth after a run and
            # cascades), and it only fires when the path reaches the grid border - a no-data
            # hole inside the clip (water mask, LiDAR void) is not a reason to enlarge.
            while True:
                if cancel is not None and cancel():
                    raise Cancelled("cancelled")
                dem, gt = load_dem(case["breach_xy"], radius, cell)
                valid0 = dem > NODATA + 1
                nvalid = int(valid0.sum()) * cell * cell
                if grew and nvalid < 1.02 * last_valid:
                    # the larger clip added no terrain: the source has nothing beyond - stop growing
                    o["radius_max_m"] = radius
                    notes.append("no terrain beyond the clip; could not enlarge it")
                last_valid, grew = nvalid, False
                if first_load and valid0.mean() < 0.5:
                    raise ValueError("less than half of the terrain clip has data")
                first_load = False
                main = injection_cells(gt, dem.shape, case["breach_xy"], case["flow_az"],
                                       case.get("bb_m") or 10.0, case.get("hb_m") or 5.0, cell)
                if not main:
                    raise ValueError("breach injection point falls outside the terrain")
                cr = int(np.mean([c[0] for c in main])); cc = int(np.mean([c[1] for c in main]))
                path, end_reason = tg.trace_path(dem, (cr, cc))
                path_len = len(path) * cell
                # Size the clip from the valley before paying for a flood that can
                # only hit the edge. Doubling happens on the trace, not after a run.
                # v0.20.0: opt-in (trace_grow). A downhill trace nearly always reaches the
                # clip edge, so it took most dams to the largest clip before the first run
                # (Absaraka: 40,000 ft, and the flood was still cut off); without it the clip
                # grows after a run whose flood reaches the edge, as before 0.15.0.
                if o.get("trace_grow") and end_reason == "edge" and path \
                        and radius < o["radius_max_m"] - cell \
                        and _at_border(path[-1], dem.shape):
                    grown = min(radius * 2.0, float(o["radius_max_m"]))
                    if grown > radius + cell:
                        radius = grown
                        grew = True
                        trace_grown = radius
                        log(f"{case['dam_id']}: downstream trace leaves the clip; "
                            f"enlarging to {radius:.0f} m before the flood run")
                        continue
                break

            # ---- reservoir in the grid (built-in engine only)
            rset = None
            if reservoir_mode:
                anotes = []                         # per-attempt notes (not repeated on retries)
                rset = _reservoir_setup(dem, gt, cell, case, anotes)
                res["attempt_notes"] = anotes
                if rset is None:
                    reservoir_mode = False
                    notes.append("reservoir-in-grid set-up failed; used hydrograph injection instead")
                elif rset == "grow":
                    if radius >= o["radius_max_m"]:
                        reservoir_mode = False
                        notes.append("reservoir larger than the maximum clip; used injection instead")
                    else:
                        radius = min(radius * 2, o["radius_max_m"]); continue

            # ---- corridor
            if use_corr:
                mask = dm.corridor_mask(dem.shape, cell, path, (cr, cc), corr_hw)
                if rset:
                    mask |= dilate(rset["reservoir"] | rset["dam"], 3)
                dem_m = dm.apply_mask(dem, mask & valid0)
            else:
                dem_m = dem
            valid = dem_m > NODATA + 1
            man = load_manning(gt, dem.shape) if load_manning else None
            if man is not None:
                man = man * o["n_mult"]
            # ---- buildings as obstructions (raised DEM or rough cells at the footprints)
            dem_m, man, bld = _apply_buildings(dem_m, man, gt, valid, main, rset, o)
            sources = [] if (rset and not isinstance(rset, str)) else \
                [{"cells": main, "t": case["t"], "q": case["q"], "start_s": 0.0}]
            for s in extra:
                cells = injection_cells(gt, dem.shape, s["breach_xy"], s["flow_az"], s["bb_m"],
                                        s["hb_m"], cell)
                if cells:
                    sources.append({"cells": cells, "t": s["t"], "q": s["q"], "start_s": s["start_s"]})

            # ---- run, extending while the flood is still spreading
            if sim_s is None:
                t_end = max(_hydro_end(case["t"], case["q"]),
                            (case.get("tf_s") or 0) * 3 if reservoir_mode else 0.0)
                sim_s = float(o["sim_s"]) if o["sim_s"] else dm.initial_sim_time(
                    t_end + max([s["start_s"] for s in extra] or [0.0]), path_len)
            eng = None
            while True:
                if o["engine"].startswith("lisflood"):
                    out = _lisflood_case(dem_m, gt, cell, sources, man, sim_s, o, cancel,
                                         str(case["dam_id"]).replace("/", "_"))
                else:
                    if eng is None:
                        eng = Engine(dem_m, cell, manning=man if man is not None
                                     else o["manning_default"] * o["n_mult"])
                        for s in sources:
                            eng.add_source(s["cells"], s["t"], s["q"], s.get("start_s", 0.0))
                        if rset and not isinstance(rset, str):
                            eng.set_reservoir(rset["reservoir"], case["start_elev"], rset["bed"])
                            eng.set_breach(rset["breach"], case["invert"], 0.0, case["tf_s"])
                    remaining = o["timeout_s"] - (time.time() - t_start)
                    out = eng.run(sim_s, max_wall_s=max(remaining, 1.0), cancel=cancel)
                    out["dv_kind"] = "exact (every time step)"
                    if rset and not isinstance(rset, str):     # report the downstream flood only
                        keep = ~out["initial_wet"]
                        out = dict(out)
                        for k in ("depth", "vel", "dv"):
                            out[k] = np.where(keep, out[k], 0.0)
                        out["arrive_h"] = np.where(keep, out["arrive_h"], NODATA)
                depth, arrive = out["depth"], out["arrive_h"]
                if dm.still_growing(np.where(arrive == NODATA, 0.0, arrive), depth, sim_s) \
                        and sim_s < o["sim_max_s"]:
                    sim_s = min(sim_s * 1.5, o["sim_max_s"])
                    log(f"{case['dam_id']}: flood still spreading, extending run to {sim_s / 3600:.1f} h")
                    continue
                break

            on_edge, on_wall = dm.touches_wall(depth, valid, o["wet_m"], terrain=valid0)
            if on_wall and use_corr:
                if corr_hw * 2 >= radius:
                    use_corr = False
                    log(f"{case['dam_id']}: flood reached the corridor wall; using the full clip")
                else:
                    corr_hw *= 2
                    log(f"{case['dam_id']}: flood reached the corridor wall; widening to {corr_hw:.0f} m")
                continue
            if on_edge and radius < o["radius_max_m"]:
                radius = min(radius * 2, o["radius_max_m"]); grew = True
                log(f"{case['dam_id']}: flood reached the terrain edge; enlarging clip to {radius:.0f} m")
                continue

            # ---- cascade check
            new = _cascade(case, gt, dem.shape, out, o, res, extra)
            if new and len(extra) < o["max_cascade"]:
                extra.extend(new)
                sim_s = None                       # re-estimate: the later failures need time too
                log(f"{case['dam_id']}: cascade - adding failure of {', '.join(n['dam_id'] for n in new)}")
                continue
            break

        notes.extend(res.pop("attempt_notes", []))
        if trace_grown:
            notes.append(f"clip enlarged to {trace_grown / FT:,.0f} ft half-width before the flood run "
                         "(downstream trace reached the clip edge)")
        res.update({"status": "ok", "cell_m": cell, "sim_s": sim_s, "gt": gt,
                    "depth": out["depth"], "vel": out["vel"], "dv": out["dv"],
                    "arrive_h": out["arrive_h"], "dv_kind": out.get("dv_kind", ""),
                    "edge_flag": on_edge, "corridor_hw_m": corr_hw if use_corr else None,
                    "radius_m": radius, "mass": out.get("mass"),
                    "reservoir_in_grid": bool(rset and not isinstance(rset, str)),
                    "buildings": bld, "base_run": False})
        if on_edge:
            notes.append("flood reaches the terrain edge at the maximum clip - result truncated")
        if res.get("mass") and abs(res["mass"]["error_pct"]) > 0.5:
            notes.append(f"mass balance error {res['mass']['error_pct']:.2f}%")
        if bld["mode"] != "none":
            notes.append(bld["note"])
        # ---- no-breach baseline (steady ambient flow only) and the incremental flood
        if o["baseline"]:
            base = _baseline(dem_m, gt, cell, man, main, rset, case, sim_s, o, cancel, t_start, log)
            res.update({"base_run": True, "baseline_cfs": float(o["baseline_cfs"]),
                        "base_depth": base["depth"], "base_vel": base["vel"],
                        "incr_depth": np.clip(out["depth"] - base["depth"], 0.0, None),
                        "base_sim_s": base["sim_s"]})
            notes.append(base["note"])
            if base.get("mass") and abs(base["mass"]["error_pct"]) > 0.5:
                notes.append(f"baseline mass balance error {base['mass']['error_pct']:.2f}%")
    except Cancelled:
        res["status"] = "cancelled"
    except Exception as e:  # keep the batch going
        notes.extend(res.pop("attempt_notes", []))
        notes.append(str(e))
    res["run_s"] = time.time() - t_start
    return res


def _apply_buildings(dem_m, man, gt, valid, main, rset, o):
    """Buildings as obstructions (screening option, default off).

    'raise': the DEM is lifted by building_raise_m at the cells whose centre is inside a
    footprint (water flows around the block, a shadow forms behind it); 'manning': the cells
    get Manning's n = building_n x n_mult instead (water passes, slowed).  Cells of the breach
    injection, the modelled reservoir and the dam are never changed.  Returns (dem, man, info).
    """
    mode = o.get("buildings") or "none"
    polys = o.get("bld_polys") or []
    info = {"mode": mode, "cells": 0, "footprints": len(polys), "note": ""}
    if mode == "none" or not polys:
        if mode != "none":
            info["note"] = f"buildings: '{mode}' chosen but no footprint polygons near the dam - bare terrain"
        return dem_m, man, info
    if mode not in BUILDING_MODES:
        raise ValueError(f"unknown buildings mode {mode!r}")
    bm = rasterize_polygons(polys, gt, dem_m.shape) & valid
    for r, c in main:
        bm[r, c] = False
    if rset and not isinstance(rset, str):
        bm &= ~(rset["reservoir"] | rset["dam"])
    info["cells"] = int(bm.sum())
    if mode == "raise":
        dz = float(o["building_raise_m"])
        dem_m = np.where(bm, dem_m + dz, dem_m)
        info["note"] = (f"buildings: DEM raised {dz / FT:.0f} ft at {info['cells']} cells "
                        f"({len(polys)} footprints near the dam; screening approximation)")
    else:
        nb = float(o["building_n"]) * float(o["n_mult"])
        if man is None:
            man = np.full(dem_m.shape, float(o["manning_default"]) * float(o["n_mult"]))
        man = np.where(bm, nb, man)
        info["note"] = (f"buildings: Manning n {nb:.3f} at {info['cells']} cells ({len(polys)} footprints "
                        f"near the dam; screening approximation)")
    return dem_m, man, info


def _baseline(dem_m, gt, cell, man, main, rset, case, sim_s, o, cancel, t_start, log):
    """No-breach baseline on the SAME domain: only a steady ambient flow (baseline_cfs) released
    at the breach injection cells (with the reservoir-in-grid engine the reservoir stays full
    and intact).  0 cfs = dry baseline, nothing is run.  Screening approximation of ND's
    flood-scenario comparison (breach flood vs the same flood without the breach); the real
    routing of the design flood is the Runner's / the engineer's.  Returns dict(depth, vel,
    sim_s, mass, note); depth and vel are the maxima of the run (steady state when it is
    reached: check the note and base_max_m)."""
    q = float(o["baseline_cfs"]) * M3S_PER_CFS
    shape = dem_m.shape
    if q <= 0.0:
        return {"depth": np.zeros(shape), "vel": np.zeros(shape), "sim_s": 0.0, "mass": None,
                "note": "no-breach baseline: dry (0 cfs) - the incremental depth equals the breach depth "
                        "(screening approximation of ND's flood-scenario comparison)"}
    valid = dem_m > NODATA + 1
    t_run = float(sim_s)
    src = {"cells": main, "t": [0.0, t_run], "q": [q, q], "start_s": 0.0}
    in_grid = bool(rset and not isinstance(rset, str))
    eng = None
    while True:
        if o["engine"].startswith("lisflood"):
            src["t"] = [0.0, t_run]
            out = _lisflood_case(dem_m, gt, cell, [src], man, t_run, o, cancel,
                                 str(case["dam_id"]).replace("/", "_") + "_base")
        else:
            if eng is None:
                eng = Engine(dem_m, cell, manning=man if man is not None
                             else o["manning_default"] * o["n_mult"])
                eng.add_source(main, [0.0, o["sim_max_s"] * 4.0], [q, q])   # steady for any run length
                if in_grid:
                    eng.set_reservoir(rset["reservoir"], case["start_elev"], rset["bed"])
            remaining = o["timeout_s"] - (time.time() - t_start)
            out = eng.run(t_run, max_wall_s=max(remaining, 1.0), cancel=cancel)
            if in_grid:                               # downstream flood only, as the breach run
                keep = ~out["initial_wet"]
                out = dict(out)
                for k in ("depth", "vel"):
                    out[k] = np.where(keep, out[k], 0.0)
                out["arrive_h"] = np.where(keep, out["arrive_h"], NODATA)
        arr = np.where(out["arrive_h"] == NODATA, 0.0, out["arrive_h"])
        if dm.still_growing(arr, out["depth"], t_run) and t_run < o["sim_max_s"]:
            t_run = min(t_run * 1.5, o["sim_max_s"])
            log(f"{case['dam_id']}: baseline flow still spreading, extending to {t_run / 3600:.1f} h")
            continue
        break
    depth = np.where(valid, out["depth"], 0.0)
    vel = np.where(valid, out["vel"], 0.0)
    note = (f"no-breach baseline: steady ambient flow {o['baseline_cfs']:g} cfs at the breach "
            f"site, run {t_run / 3600:.1f} h, max depth {float(depth.max()) / FT:.1f} ft (screening approximation "
            f"of ND's flood-scenario comparison; the real design-flood routing is the Runner's)")
    return {"depth": depth, "vel": vel, "sim_s": t_run, "mass": out.get("mass"), "note": note}


def _reservoir_setup(dem, gt, cell, case, notes):
    need = ("dam_line_xy", "start_elev", "invert", "tf_s", "bavg_m")
    if any(case.get(k) is None for k in need):
        notes.append("reservoir-in-grid needs the dam line, crest, invert and breach parameters")
        return None
    br = xy_to_rc(gt, *case["breach_xy"])
    breach, line = rsv.breach_cells(dem, case["dam_line_xy"], gt, case["breach_xy"], case["bavg_m"],
                                    crest_elev=case["start_elev"], invert=case["invert"])
    dam = dilate(line, 1)
    seed = rsv.seed_upstream(dem, br, case["flow_az"], cell, exclude=dam)
    if seed is None:
        notes.append("no reservoir cell found upstream of the breach"); return None
    resmask, why = rsv.find_reservoir(dem, dam, seed, case["start_elev"])
    if why == "reservoir reaches the terrain edge":
        return "grow"
    if why or not resmask.any():
        notes.append(f"reservoir: {why or 'empty'}"); return None
    # leak check: reservoir must not wrap round to the downstream side
    dn = injection_cells(gt, dem.shape, case["breach_xy"], case["flow_az"], case.get("bb_m") or 10,
                         case.get("hb_m") or 5, cell)
    if any(resmask[r, c] for r, c in dn):
        notes.append("reservoir at the failure pool spills around the dam in the terrain")
        return None
    bed, info = dem, {}
    if case.get("pool_elev") is not None and case.get("v_below_m3"):
        bed, info = rsv.estimate_bed(dem, resmask, case["pool_elev"], case["v_below_m3"], cell)
        if info.get("water_cells"):
            notes.append(f"reservoir bed estimated: max depth {info['depth_max_m'] / FT:.1f} ft")
    else:
        notes.append("no normal pool / storage: reservoir bed below the water surface not estimated")
    return {"reservoir": resmask, "breach": breach, "dam": dam, "bed": bed}


def _cascade(case, gt, shape, out, o, res, extra):
    new = []
    done = {e["dam_id"] for e in extra} | {c["dam_id"] for c in res["cascade"]}
    for od in case.get("others", []):
        if od["dam_id"] in done or od.get("line_xy") is None:
            continue
        cells = rasterize_lines([od["line_xy"]], gt, shape)
        if not cells.any():
            continue
        d_on = float(out["depth"][cells].max())
        if d_on <= o["wet_m"]:
            continue
        arr = out["arrive_h"][cells]
        arr = arr[arr != NODATA]
        t_arr = float(arr.min()) if len(arr) else None
        breached = (o["cascade"] == "breach" and od.get("t") is not None and t_arr is not None)
        res["cascade"].append({"dam_id": od["dam_id"], "depth_over_crest_m": d_on,
                               "arrival_h": t_arr, "breached": breached})
        if breached:
            new.append({"dam_id": od["dam_id"], "breach_xy": od["breach_xy"], "flow_az": od["flow_az"],
                        "bb_m": od.get("bb_m") or 10.0, "hb_m": od.get("hb_m") or 5.0,
                        "t": od["t"], "q": od["q"], "start_s": t_arr * 3600.0})
    return new


# ---------------------------------------------------------------- batch
def _guarded(worker, it):
    """worker(it); an exception the worker did not handle becomes that item's FAILED result
    (v0.11.1: in parallel mode it used to end the whole batch and lose the other dams)."""
    try:
        return worker(it)
    except Cancelled:
        return {"dam_id": it.get("dam_id") if isinstance(it, dict) else None, "status": "cancelled",
                "notes": ["cancelled"]}
    except Exception as e:                                       # noqa
        did = it.get("dam_id") if isinstance(it, dict) else None
        try:
            from . import errors as er
            note = er.plain(e, did, "7")
        except Exception:                                        # noqa
            note = "{}: {}".format(type(e).__name__, e)
        return {"dam_id": did, "status": "FAILED", "notes": [note]}


def run_batch(items, worker, workers=1, cancel=None, on_done=None):
    """Run worker(item) for all items with `workers` threads; on_done(item, result) in order of
    finishing. One item's error is that item's FAILED result, never the end of the batch."""
    results = []
    if workers <= 1:
        for it in items:
            if cancel is not None and cancel():
                break
            r = _guarded(worker, it); results.append(r)
            if on_done: on_done(it, r)
        return results
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_guarded, worker, it): it for it in items}
        try:
            for f in as_completed(futs):
                r = f.result(); results.append(r)
                if on_done: on_done(futs[f], r)
                if cancel is not None and cancel():
                    for g in futs: g.cancel()
                    break
        finally:
            for g in futs: g.cancel()
    return results
