"""How long will tool 7 take? An ORDER-OF-MAGNITUDE estimate, not a promise. numpy only.

The built-in engine is an explicit shallow-water solver: its cost is about
(active cells) x (time steps), and the time step is the CFL limit
    dt = cfl * cell / (|u| + sqrt(g h))     (fv_engine.Engine.run: cfl 0.45, dt <= 5 s).
So for one dam the estimate is
    cells  : the terrain clip (2 r / cell)^2, cut to the downstream corridor when tool 7
             models a corridor (the same rules as the runner: domain.auto_cell for the cell
             size, the tool-2 clip half-width r, domain.corridor_mask's half-width);
    steps  : simulated time / dt at a representative depth (domain.initial_sim_time for the
             run length when none is given);
    time   : cells x steps x ACTIVE (the engine only works on the wet window, so a share of
             the cells) / rate, where rate (cell-steps per second) is MEASURED here with a
             tiny calibration run of the built-in engine on a 60 x 60 synthetic grid;
    range  : one run (low) to GROWTH_RUNS x one run (high): tool 7 re-runs a dam when the
             flood reaches the corridor wall or the terrain edge, or is still spreading.
LISFLOOD-FP: the same count times LISFLOOD_FACTOR (default 0.1 = ten times faster than the
built-in engine, the lower end of the README's 10-50x). That factor is a GUESS until you time
LISFLOOD-FP on your computer; set it from one timed run (built-in run_s / LISFLOOD run_s).

Everything that the runner decides at run time (the flood reaching the wall, cascades, the
3DEP download of a larger clip, other programs using the CPU) is outside this estimate; expect
the real time to fall within about a factor of 3 of the typical value, and read the low / high
columns as that spread.
"""
import math
import time

import numpy as np

from . import domain as dm

G = 9.81
CFL = 0.45
DT_MAX_S = 5.0
ACTIVE_SHARE = 0.5           # share of the corridor cells the engine's wet window covers (typical)
GROWTH_RUNS = 4.0            # high estimate: re-runs after widening / enlarging / extending
LISFLOOD_FACTOR = 0.1        # LISFLOOD-FP wall time / built-in wall time: A GUESS (see above)
RADIUS_DEFAULT_M = 1500.0    # tool 2's default clip half-width
PATH_FACTOR = 1.5            # the downstream path winds: about 1.5 x the clip half-width
KEEP_RADIUS_M = 150.0        # domain.corridor_mask's disk around the breach


def representative_depth(hb_m):
    """A typical flow depth in the corridor: 0.4 x breach height, kept between 0.3 and 4 m."""
    return float(min(max(0.4 * (hb_m or 3.0), 0.3), 4.0))


def time_step(cell_m, depth_m):
    """CFL time step (s) at a dam-break front: velocity ~ sqrt(g h), so |u| + c ~ 2 sqrt(g h)."""
    lam = 2.0 * math.sqrt(G * max(depth_m, 0.01))
    return float(min(CFL * cell_m / lam, DT_MAX_S))


def hydro_end_s(t, q):
    """Last time with flow in a hydrograph (the runner's rule)."""
    q = np.asarray(q, float); t = np.asarray(t, float)
    nz = np.where(q > 1e-6)[0]
    return float(t[nz[-1]]) if len(nz) else float(t[-1])


def plan_dam(vw_m3=None, bavg_m=None, hb_m=None, t_hydro_end_s=3600.0, dem_cell_m=1.0,
             cell_m=None, radius_m=None, corridor=True, corridor_hw_m=600.0, sim_s=None,
             sim_max_s=36 * 3600.0):
    """The domain tool 7 would start with, for one dam. Returns a dict (cells, steps, ...)."""
    cell = dm.auto_cell(vw_m3, bavg_m, dem_cell_m) if not cell_m else float(cell_m)
    r = float(radius_m or RADIUS_DEFAULT_M)
    n = max(int(math.ceil(2.0 * r / cell)), 1)
    clip_cells = n * n
    path_len = PATH_FACTOR * r
    if corridor:
        corr = (2.0 * corridor_hw_m * path_len + math.pi * KEEP_RADIUS_M ** 2) / (cell * cell)
        cells = int(min(corr, clip_cells))
    else:
        cells = clip_cells
    if not sim_s:
        sim_s = dm.initial_sim_time(t_hydro_end_s, path_len)
    sim_s = float(min(sim_s, sim_max_s))
    h = representative_depth(hb_m)
    dt = time_step(cell, h)
    steps = int(math.ceil(sim_s / dt))
    return {"cell_m": cell, "radius_m": r, "clip_cells": clip_cells, "cells": cells,
            "corridor": bool(corridor), "sim_s": sim_s, "depth_rep_m": h, "dt_s": dt, "steps": steps,
            "cell_steps": float(cells) * steps}


def seconds_for(plan, rate, engine="builtin", active=ACTIVE_SHARE, lisflood_factor=LISFLOOD_FACTOR):
    """(low, typical, high) wall seconds for one dam from a plan and a measured rate
    (cell-steps per second of the built-in engine)."""
    one = plan["cell_steps"] * active / max(rate, 1.0)
    if str(engine).startswith("lisflood"):
        one *= lisflood_factor
    return one, one * 2.0, one * GROWTH_RUNS


def batch_total(per_dam_s, workers=1):
    """Wall time of a batch run with `workers` dams at once (perfect sharing, which tool 7's
    thread pool roughly achieves until the CPU is full)."""
    tot = float(sum(per_dam_s))
    return tot / max(int(workers), 1)


def fmt_duration(s):
    if s is None or not math.isfinite(s):
        return "?"
    if s < 90:
        return f"{s:.0f} s"
    if s < 5400:
        return f"{s / 60:.0f} min"
    if s < 2 * 86400:
        return f"{s / 3600:.1f} h"
    return f"{s / 86400:.1f} days"


def calibration_world(n=60, cell_m=10.0):
    """A tiny V-shaped valley (n x n cells) sloping south with a source at its head."""
    yy, xx = np.mgrid[0:n, 0:n]
    dem = 100.0 - 0.004 * yy * cell_m + 0.002 * np.abs(xx - n / 2) * cell_m
    src = [(3, n // 2 - 1), (3, n // 2), (3, n // 2 + 1)]
    return dem, src


def calibrate(engine_cls, n=60, cell_m=10.0, sim_s=600.0, q_m3s=60.0, min_wall_s=0.3):
    """Measure this computer's built-in-engine speed: cell-steps per second on an n x n
    synthetic grid (every grid cell counted, as plan_dam counts corridor cells). The run is
    repeated with a longer simulated time until it takes at least `min_wall_s`, so the number
    is not pure start-up cost. Returns {"rate", "steps", "wall_s", "cells", "sim_s"}."""
    dem, src = calibration_world(n, cell_m)
    while True:
        eng = engine_cls(dem, cell_m, manning=0.05)
        eng.add_source(src, [0.0, sim_s * 0.5, sim_s], [q_m3s, q_m3s, 0.0])
        t0 = time.perf_counter()
        out = eng.run(sim_s)
        wall = time.perf_counter() - t0
        steps = int(out.get("steps") or eng.steps)
        if wall >= min_wall_s or sim_s >= 8 * 3600.0:
            break
        sim_s *= 3.0
    return {"rate": float(n * n * steps / max(wall, 1e-6)), "steps": steps, "wall_s": wall,
            "cells": n * n, "sim_s": sim_s}


def estimate_batch(dams, rate, engine="builtin", workers=1, lisflood_factor=LISFLOOD_FACTOR,
                   **plan_kw):
    """dams: [{"dam_id", "vw_m3", "bavg_m", "hb_m", "t_hydro_end_s", "radius_m", "skip"}]
    -> (rows, totals). Each row has the plan plus low_s / typical_s / high_s; a dam with
    "skip" set gets zeros and the reason. Totals: wall seconds of the batch with `workers`."""
    rows = []
    for d in dams:
        row = {"dam_id": d.get("dam_id"), "skip": d.get("skip") or ""}
        if row["skip"]:
            row.update({"cell_m": None, "cells": 0, "steps": 0, "sim_s": 0.0, "low_s": 0.0,
                        "typical_s": 0.0, "high_s": 0.0})
            rows.append(row); continue
        kw = dict(plan_kw)
        if d.get("radius_m"):
            kw["radius_m"] = d["radius_m"]
        p = plan_dam(d.get("vw_m3"), d.get("bavg_m"), d.get("hb_m"), d.get("t_hydro_end_s") or 3600.0, **kw)
        lo, typ, hi = seconds_for(p, rate, engine, lisflood_factor=lisflood_factor)
        row.update(p); row.update({"low_s": lo, "typical_s": typ, "high_s": hi})
        rows.append(row)
    totals = {k: batch_total([r[k + "_s"] for r in rows], workers) for k in ("low", "typical", "high")}
    totals.update({"n_dams": sum(1 for r in rows if not r["skip"]), "n_skipped": sum(1 for r in rows if r["skip"]),
                   "cells": sum(r["cells"] for r in rows), "workers": int(workers), "engine": engine,
                   "serial_typical_s": float(sum(r["typical_s"] for r in rows))})
    return rows, totals


CSV_COLUMNS = ["dam_id", "skip", "cell_m", "radius_m", "clip_cells", "cells", "corridor", "sim_s", "depth_rep_m",
               "dt_s", "steps", "low_s", "typical_s", "high_s", "typical_text"]


def rows_for_csv(rows):
    out = []
    for r in rows:
        o = {k: r.get(k) for k in CSV_COLUMNS}
        o["typical_text"] = fmt_duration(r.get("typical_s")) if not r.get("skip") else ""
        for k in ("sim_s", "low_s", "typical_s", "high_s", "dt_s", "cell_m", "radius_m", "depth_rep_m"):
            if isinstance(o.get(k), float):
                o[k] = round(o[k], 3)
        out.append(o)
    return out


def summary_lines(totals, cal, engine_label, lisflood_factor=LISFLOOD_FACTOR):
    lines = [f"ORDER-OF-MAGNITUDE run-time estimate for tool 7 ({engine_label}): "
             f"{totals['n_dams']} dam(s), {totals['n_skipped']} skipped, {totals['workers']} at a time.",
             f"Typical about {fmt_duration(totals['typical'])} of wall time (low {fmt_duration(totals['low'])}, "
             f"high {fmt_duration(totals['high'])}); {fmt_duration(totals['serial_typical_s'])} of CPU time "
             f"one after the other."]
    if cal:
        lines.append(f"Calibration: built-in engine on a {int(round(math.sqrt(cal['cells'])))} x "
                     f"{int(round(math.sqrt(cal['cells'])))} grid, {cal['steps']} steps in {cal['wall_s']:.2f} s "
                     f"= {cal['rate']:,.0f} cell-steps/s on this computer.")
    if str(totals.get("engine", "")).startswith("lisflood"):
        lines.append(f"LISFLOOD-FP times = built-in times x {lisflood_factor:g}: that factor is a GUESS "
                     "(the README says LISFLOOD-FP is about 10-50x faster); time one dam with both "
                     "engines and set the factor from it.")
    lines.append("Not counted: corridor widening / clip growth beyond the high value, cascades, 3DEP "
                 "downloads, other programs on the CPU. Expect reality within about a factor of 3 of "
                 "'typical'.")
    return lines
