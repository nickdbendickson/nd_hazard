"""LISFLOOD-FP (Bates et al.; University of Bristol) tier: write the inputs,
run the executable, read the envelopes back.

Uses the inertial ("acceleration") floodplain solver on a raster, with the
breach hydrograph as a point source just downstream of the dam and free
(uniform-flow) outflow on all four domain edges.

Arrival time needs the `inittm_depth` keyword added by the patch shipped
with this plugin (scripts/lisflood-nd-breach.patch). Without it LISFLOOD-FP
ignores the keyword and records arrival at its internal wet/dry depth (about
1 mm); `check_lisflood()` tells the two apart."""
import os
import subprocess
import sys
import time

import numpy as np

from .grid import Grid
from .units import ACFT, ACRE, CFS, FT
from ..damsuite.keepawake import keep_awake


ARRIVAL_TOL_M = 0.001    # LISFLOOD-FP writes depth to 3 decimals

# The line LISFLOOD-FP prints when it does not have the arrival-depth patch (it then ignores
# the inittm_depth keyword and records arrival at the first wetting, about 1 mm).
PATCH_MISSING_MARKER = "Unknown parameter ignored: inittm_depth"
PATCH_MISSING_MSG = (
    "This LISFLOOD-FP build does not have the arrival-depth patch (it ignored "
    "'inittm_depth'), so arrival times would mark the first millimetre of water "
    "instead of the arrival depth. Rebuild it with scripts/build_lisflood_macos.sh "
    "(or scripts/patch_lisflood.py) and set the new executable in Processing > "
    "Options > Providers > ND Dam Breach Runner, or tick 'Accept an unpatched "
    "LISFLOOD-FP build' there to run anyway with unreliable arrival times.")
PATCH_MISSING_WARNING = (
    "LISFLOOD-FP has no arrival-depth patch and 'Accept an unpatched build' is on: "
    "arrival times mark the first wetting (about 1 mm), NOT the arrival depth, so any "
    "hazard warning time from this run is unreliable. Build a patched LISFLOOD-FP for a "
    "defensible run.")


def patch_missing(logtxt):
    """True when a LISFLOOD-FP log shows it ignored the arrival-depth keyword."""
    return PATCH_MISSING_MARKER in (logtxt or "")

# v3.4.0: the floodplain solver keyword written to the .par file. "acceleration" is the
# local-inertial solver used everywhere by default; "fv1" is the first-order finite-volume
# (shallow-water, shock-capturing) solver of LISFLOOD-FP 8, whose velocities near the breach
# are not limited by the inertial approximation (used by the near-dam refine option). Neither
# has been run against a real executable here: nothing in this plugin was run in a real QGIS
# or with a real LISFLOOD-FP.
EDGE_LETTERS = ("N", "S", "E", "W")
SOLVER_ACC = "acceleration"
SOLVER_FV1 = "fv1"
SOLVERS = (SOLVER_ACC, SOLVER_FV1)


# 3.11.0: LISFLOOD-FP without installing it. resolve_exe() turns the executable setting into
# the program to run: a path you give is used as it is; an empty setting takes the patched
# LISFLOOD-FP bundled with the plugin for this platform (bin/<platform>/), else the built-in
# engine (core/builtin_lisflood.py, the same local-inertial equations in numpy); the word
# "builtin" always takes the built-in engine. The built-in engine's "executable" is its .py
# file, so every "is the executable there" check and the run manifest keep working.
HERE = os.path.dirname(os.path.abspath(__file__))
BUILTIN_EXE = os.path.join(HERE, "builtin_lisflood.py")
BUNDLED_DIR = os.path.join(os.path.dirname(HERE), "bin")
BUILTIN_WORDS = ("builtin", "built-in", "built in", "internal")


def bundled_exe():
    """The LISFLOOD-FP bundled for this computer, or None (macOS, other CPUs). On Linux the
    execute permission lost by an install from ZIP is restored."""
    import platform
    import stat
    mach = platform.machine().lower()
    if mach not in ("x86_64", "amd64"):
        return None
    if os.name == "nt":
        p = os.path.join(BUNDLED_DIR, "windows-x64", "lisflood.exe")
    elif sys.platform.startswith("linux"):
        p = os.path.join(BUNDLED_DIR, "linux-x64", "lisflood")
    else:
        return None
    if not os.path.isfile(p):
        return None
    if os.name != "nt" and not os.access(p, os.X_OK):
        try:
            os.chmod(p, os.stat(p).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        except OSError:
            return None
    return p


def is_builtin(exe):
    return bool(exe) and os.path.abspath(str(exe)) == BUILTIN_EXE


def resolve_exe(exe):
    """The LISFLOOD-FP program for a setting or parameter value (see above)."""
    v = (str(exe).strip() if exe else "")
    if v.lower() in BUILTIN_WORDS:
        return BUILTIN_EXE
    if v:
        return v
    return bundled_exe() or BUILTIN_EXE


def describe_exe(exe):
    """One line for logs and reports: which program runs the floods."""
    if is_builtin(exe):
        return "the built-in engine (local inertial, in numpy; no LISFLOOD-FP)"
    if exe and os.path.abspath(str(exe)).startswith(BUNDLED_DIR + os.sep):
        return "LISFLOOD-FP bundled with the plugin ({})".format(exe)
    return "LISFLOOD-FP at {}".format(exe)


class ModelError(RuntimeError):
    pass


class Cancelled(RuntimeError):
    pass


class LisfloodSettings:
    def __init__(self, exe, threads=0, manning_n=0.06, manning_grid=None,
                 arrival_depth_m=0.3048, sim_time_s=6 * 3600.0, massint_s=300.0,
                 initial_tstep_s=10.0, velocity_min_depth_m=0.03, extra_par=None,
                 initial_depth=None, gauges=None, snapshot_s=None, extra_sources=None,
                 solver=SOLVER_ACC, closed_edges=(), allow_unpatched=False):
        if solver not in SOLVERS:
            raise ModelError("unknown LISFLOOD-FP solver '{}' (one of {})".format(
                solver, ", ".join(SOLVERS)))
        self.solver = solver                    # v3.4.0: "acceleration" (default) or "fv1"
        # v3.11.0: domain edges written as closed walls ("N", "S", "E", "W") instead of free
        # outflow. The near-dam FV1 run closes the edges that face the reservoir: with FV1 a
        # free edge across ponded water brings water INTO the model (see core/refine.py).
        self.closed_edges = tuple(sorted({str(e).upper() for e in (closed_edges or ())}))
        bad = [e for e in self.closed_edges if e not in EDGE_LETTERS]
        if bad:
            raise ModelError("unknown domain edge(s) to close: {}".format(", ".join(bad)))
        self.exe = exe
        self.threads = threads
        self.manning_n = manning_n
        self.manning_grid = manning_grid
        self.arrival_depth_m = arrival_depth_m
        self.sim_time_s = sim_time_s
        self.massint_s = massint_s
        self.initial_tstep_s = initial_tstep_s
        self.velocity_min_depth_m = velocity_min_depth_m
        self.extra_par = extra_par or []
        self.initial_depth = initial_depth      # Grid of initial water depth (m) or None
        self.gauges = gauges or []              # [{"name", "xy", "direction", "width_m"}]
        self.snapshot_s = snapshot_s            # write depth snapshots this often (animation)
        # more point sources: [{"name", "xy", "t_s", "q_cms", optional "line"}], e.g. ambient
        # river flow entering the domain
        self.extra_sources = extra_sources or []
        # v3.26.0: accept a LISFLOOD-FP build WITHOUT the arrival-depth patch (it ignores
        # inittm_depth). Off by default so _run() refuses such a build; on, the run proceeds
        # with a loud warning and `unpatched_used` is set, because arrival times then mark the
        # first wetting (~1 mm) rather than the arrival depth. For users who cannot build a
        # patched LISFLOOD-FP (e.g. a locked-down Mac).
        self.allow_unpatched = allow_unpatched
        self.unpatched_used = False


class Envelopes:
    """Maximum depth (m), maximum speed (m/s), arrival (hr), max WSE (m) on the
    model grid, plus run statistics."""

    def __init__(self, depth, velocity, arrival_hr, wse, stats=None, notes=None, gauges=None):
        self.gauges = gauges or {}       # name -> (t_s, q_cms)
        self.depth = depth
        self.velocity = velocity
        self.arrival_hr = arrival_hr
        self.wse = wse
        self.stats = stats or {}
        self.notes = notes or []


def inflow_cells(dem, inflow_xy, line=None):
    """Cells that share the breach inflow: the cells under `line` (a segment
    across the flow, about the breach width) or just the inflow cell. Spreading
    the inflow avoids an unrealistic velocity spike in a single cell."""
    cells = []
    r, c = dem.rc(*inflow_xy)
    if not dem.inside(r, c) or not dem.valid()[r, c]:
        raise ModelError("the breach inflow point is outside the DEM or on no-data")
    if line is not None:
        (x0, y0), (x1, y1) = line
        n = max(2, int(np.hypot(x1 - x0, y1 - y0) / (0.5 * dem.dx)) + 1)
        for t in np.linspace(0.0, 1.0, n):
            rr, cc = dem.rc(x0 + t * (x1 - x0), y0 + t * (y1 - y0))
            if dem.inside(rr, cc) and dem.valid()[rr, cc] and (rr, cc) not in cells:
                cells.append((rr, cc))
    if (r, c) not in cells:
        cells.append((r, c))
    return cells


def local_grid(dem):
    """The same cells with the lower-left corner at (0, 0). The default
    LISFLOOD-FP build is single precision, which resolves only 0.5 m at UTM
    northings of 5,000,000 m; local coordinates keep the boundary cells exact.
    Results are arrays, so they map straight back onto the real DEM."""
    return Grid(dem.data, 0.0, dem.ny * dem.dx, dem.dx, dem.nodata, dem.crs_wkt)


def gauge_section(center_xy, flow_dir, width_m):
    """A gauge that measures the flow along `flow_dir` (unit vector) across a
    section of `width_m` centred on `center_xy`. LISFLOOD-FP sections run along
    a grid axis: east/west gauges span north-south, north/south gauges span
    east-west. Returns a gauge dict."""
    ux, uy = flow_dir
    if abs(ux) >= abs(uy):
        direction = "E" if ux >= 0 else "W"
    else:
        direction = "N" if uy >= 0 else "S"
    return {"xy": tuple(center_xy), "direction": direction, "width_m": float(width_m)}


def write_inputs(workdir, dem_real, inflow_xy, hydrograph, st, name="run", inflow_line=None):
    os.makedirs(workdir, exist_ok=True)
    cells = inflow_cells(dem_real, inflow_xy, inflow_line)
    dem = local_grid(dem_real)
    dem.write_asc(os.path.join(workdir, name + ".dem.asc"))
    xmin, ymin, xmax, ymax = dem.bounds
    with open(os.path.join(workdir, name + ".bci"), "w") as f:
        for rr, cc in cells:
            px, py = dem.xy(rr, cc)
            f.write("P {:.3f} {:.3f} QVAR breach\n".format(px, py))
        closed = getattr(st, "closed_edges", ())      # a closed edge is simply not listed
        for edge, lo, hi in (("N", xmin, xmax), ("S", xmin, xmax),
                             ("E", ymin, ymax), ("W", ymin, ymax)):
            if edge not in closed:
                f.write("{} {:.3f} {:.3f} FREE\n".format(edge, lo, hi))
    # QVAR at a point source is per unit width (m2/s); the code multiplies by
    # dx. Each of the n inflow cells carries 1/n of the breach outflow.
    t, q = hydrograph.resampled(30.0)      # keeps every original breakpoint
    n = len(cells)
    extra = []
    for k, src in enumerate(st.extra_sources):
        sname = "src{}".format(k + 1)
        try:
            sc = inflow_cells(dem_real, src["xy"], src.get("line"))
        except ModelError:
            raise ModelError("inflow '{}' is outside the DEM or on no-data".format(
                src.get("name", sname)))
        extra.append((sname, sc, np.asarray(src["t_s"], float), np.asarray(src["q_cms"], float)))
        with open(os.path.join(workdir, name + ".bci"), "a") as f:
            for rr, cc in sc:
                px, py = dem.xy(rr, cc)
                f.write("P {:.3f} {:.3f} QVAR {}\n".format(px, py, sname))
    with open(os.path.join(workdir, name + ".bdy"), "w") as f:
        f.write("breach hydrograph: m3/s / (cell size x {} inflow cells)\n".format(n))
        f.write("breach\n")
        f.write("{} seconds\n".format(len(t)))
        for tv, qv in zip(t, q):
            f.write("{:.6f} {:.4f}\n".format(qv / (dem.dx * n), tv))
        for sname, sc, ts_, qs_ in extra:
            f.write("{}\n".format(sname))
            f.write("{} seconds\n".format(len(ts_)))
            for tv, qv in zip(ts_, qs_):
                f.write("{:.6f} {:.4f}\n".format(qv / (dem.dx * len(sc)), tv))
    lines = [
        "DEMfile {}.dem.asc".format(name),
        "resroot {}".format(name),
        "dirroot results",
        "sim_time {:.0f}".format(st.sim_time_s),
        "initial_tstep {:.3f}".format(st.initial_tstep_s),
        "massint {:.0f}".format(st.massint_s),
        "saveint {:.0f}".format(st.snapshot_s if st.snapshot_s else st.sim_time_s + 10.0),
        "bcifile {}.bci".format(name),
        "bdyfile {}.bdy".format(name),
        getattr(st, "solver", SOLVER_ACC),
        "hazard",
        "elevoff",
        "inittm_depth {:.4f}".format(st.arrival_depth_m),
    ]
    if not st.snapshot_s:
        lines.insert(lines.index("elevoff") + 1, "depthoff")
    if st.manning_grid is not None:
        st.manning_grid.write_asc(os.path.join(workdir, name + ".n.asc"), fmt="%.4f")
        lines.append("manningfile {}.n.asc".format(name))
        lines.append("fpfric {:.4f}".format(st.manning_n))
    else:
        lines.append("fpfric {:.4f}".format(st.manning_n))
    if st.initial_depth is not None:
        init = st.initial_depth.data
        if init.shape != dem.data.shape:
            raise ModelError("the initial depth grid does not match the DEM")
        init_grid = dem.like(np.where(np.isfinite(init), init, 0.0))
        init_grid.write_asc(os.path.join(workdir, name + ".start"), fmt="%.3f")
        lines.append("startfile {}.start".format(name))
    if st.gauges:
        xmin_l, ymin_l = dem.x0, dem.yll
        with open(os.path.join(workdir, name + ".gauge"), "w") as f:
            f.write("{}\n".format(len(st.gauges)))
            for g in st.gauges:
                gx, gy = g["xy"][0] - dem_real.x0, g["xy"][1] - dem_real.yll
                half = 0.5 * g["width_m"]
                if g["direction"] in ("E", "W"):
                    gy += half           # the section runs south from here
                else:
                    gx -= half           # the section runs east from here
                f.write("{:.3f} {:.3f} {} {:.3f}\n".format(gx, gy, g["direction"],
                                                          g["width_m"]))
        lines.append("gaugefile {}.gauge".format(name))
    lines += list(st.extra_par)
    par = os.path.join(workdir, name + ".par")
    with open(par, "w") as f:
        f.write("# written by ND Dam Breach Runner\n")
        f.write("\n".join(lines) + "\n")
    return par


def _read_mass(path):
    """Last time (s), cumulative volume error (m3, signed sum of the interval
    errors), last domain volume (m3), largest inflow (m3/s), largest single
    interval error (m3)."""
    last_t, cum, vol, qin_max, worst = 0.0, 0.0, 0.0, 0.0, 0.0
    try:
        with open(path) as f:
            next(f)
            for line in f:
                p = line.split()
                if len(p) >= 11:
                    last_t = float(p[0])
                    vol = float(p[5])
                    qin_max = max(qin_max, float(p[6]))
                    e = float(p[10])
                    cum += e
                    worst = max(worst, abs(e))
    except (OSError, StopIteration, ValueError):
        pass
    return last_t, cum, vol, qin_max, worst


def _read_series(path):
    """The mass file as arrays (time s, domain volume m3, inflow m3/s, outflow m3/s)."""
    t, v, qi, qo = [], [], [], []
    try:
        with open(path) as f:
            next(f)
            for line in f:
                p = line.split()
                if len(p) >= 11:
                    t.append(float(p[0]))
                    v.append(float(p[5]))
                    qi.append(float(p[6]))
                    qo.append(float(p[8]))
    except (OSError, StopIteration, ValueError):
        pass
    return np.asarray(t), np.asarray(v), np.asarray(qi), np.asarray(qo)


def late_inflow(path, t_end_s, tol_cms=0.5):
    """Water that comes in through the domain edges after the hydrograph has ended, from
    the mass file. Returns (volume m3, largest inflow m3/s, time of the largest inflow s).
    Once the point source has stopped, the only inflow left is what a boundary adds, so
    anything above `tol_cms` is the boundary importing water (LISFLOOD-FP 8.1's FV1 free
    edge does this where water stands against it)."""
    t, _v, qi, _qo = _read_series(path)
    if len(t) < 2:
        return 0.0, 0.0, 0.0
    late = t > t_end_s
    if not late.any():
        return 0.0, 0.0, 0.0
    tl, ql = t[late], qi[late]
    dt = np.diff(np.concatenate([[max(t_end_s, t[~late][-1] if (~late).any() else 0.0)], tl]))
    vol = float(np.sum(np.clip(ql - tol_cms, 0.0, None) * dt))
    k = int(np.argmax(ql))
    if ql[k] <= tol_cms:
        return 0.0, 0.0, 0.0
    return vol, float(ql[k]), float(tl[k])


def _run_builtin(workdir, par, st, progress=None, cancel=None, log=None):
    from . import builtin_lisflood as bl
    t0 = time.time()
    lines = []

    def say(msg):
        lines.append(msg)
        if log:
            log(msg)

    def prog(t, sim):
        if progress:
            progress(min(100.0, 100.0 * t / sim), "built-in engine t = {:.2f} h".format(t / 3600.0))
    try:
        bl.run_par(par, progress=prog, cancel=cancel, log=say)
    except bl.Cancelled:
        raise Cancelled("cancelled")
    except bl.EngineError as e:
        raise ModelError("built-in engine: {}".format(e))
    finally:
        with open(os.path.join(workdir, "lisflood.log"), "w") as f:
            f.write(bl.NAME + "\n" + "\n".join(lines) + "\n")
    return time.time() - t0


def run(workdir, par, st, progress=None, cancel=None, log=None, name="run"):
    # v3.12.0: the computer is kept awake while the model runs (damsuite.keepawake, beside
    # the solver: LISFLOOD-FP stays this plugin's own child, so Cancel still kills it)
    what = "the built-in engine" if is_builtin(st.exe) else "LISFLOOD-FP"
    with keep_awake(what, log=log):
        return _run(workdir, par, st, progress, cancel, log, name)


def _run(workdir, par, st, progress=None, cancel=None, log=None, name="run"):
    exe = st.exe
    if is_builtin(exe):
        return _run_builtin(workdir, par, st, progress, cancel, log)
    if not exe or not os.path.isfile(exe):
        raise ModelError("LISFLOOD-FP executable not found: {}".format(exe))
    env = dict(os.environ)
    if st.threads:
        env["OMP_NUM_THREADS"] = str(int(st.threads))
    log_path = os.path.join(workdir, "lisflood.log")
    mass = os.path.join(workdir, "results", name + ".mass")
    t0 = time.time()
    with open(log_path, "w") as logf:
        # 3.11.0: no console window on Windows
        flags = 0x08000000 if os.name == "nt" else 0
        proc = subprocess.Popen([exe, "-v", os.path.basename(par)], cwd=workdir, env=env,
                                stdout=logf, stderr=subprocess.STDOUT, creationflags=flags)
        while proc.poll() is None:
            time.sleep(1.0)
            if cancel and cancel():
                proc.kill()
                proc.wait()
                raise Cancelled("cancelled")
            if progress and os.path.exists(mass):
                tt = _read_mass(mass)[0]   # (time is the first item)
                progress(min(100.0, 100.0 * tt / st.sim_time_s),
                         "LISFLOOD-FP t = {:.2f} h".format(tt / 3600.0))
    with open(log_path, errors="replace") as f:
        logtxt = f.read()
    if proc.returncode != 0:
        raise ModelError("LISFLOOD-FP stopped with code {}:\n{}".format(proc.returncode,
                                                                      logtxt[-1500:]))
    if patch_missing(logtxt):
        if getattr(st, "allow_unpatched", False):
            st.unpatched_used = True
            if log:
                log("WARNING: " + PATCH_MISSING_WARNING)
        else:
            raise ModelError(PATCH_MISSING_MSG)
    if log:
        log("LISFLOOD-FP finished in {:.1f} min".format((time.time() - t0) / 60.0))
    return time.time() - t0


def read_gauges(workdir, st, name="run"):
    """Discharge time series of the virtual gauges: {gauge name: (t_s, q_cms)}.
    The .discharge columns are in m3/s (checked against a steady inflow)."""
    path = os.path.join(workdir, "results", name + ".discharge")
    if not st.gauges or not os.path.exists(path):
        return {}
    rows = []
    with open(path) as f:
        for line in f:
            p = line.split()
            if len(p) == 1 + len(st.gauges):
                try:
                    rows.append([float(v) for v in p])
                except ValueError:
                    continue
    if not rows:
        return {}
    a = np.asarray(rows)
    out = {}
    for i, g in enumerate(st.gauges):
        out[g.get("name", "gauge{}".format(i + 1))] = (a[:, 0], np.clip(a[:, 1 + i], 0.0, None))
    return out


def read_results(workdir, dem, st, hydrograph, name="run"):
    res = os.path.join(workdir, "results")

    def load(ext, required=True):
        p = os.path.join(res, name + ext)
        if not os.path.exists(p):
            if required:
                raise ModelError("LISFLOOD-FP did not write {}".format(p))
            return None
        g = Grid.read_asc(p)
        a = g.data
        a[a == g.nodata] = np.nan
        if a.shape != dem.data.shape:
            raise ModelError("{} has shape {} but the DEM is {}".format(p, a.shape,
                                                                        dem.data.shape))
        return a

    hmax = load(".max")
    vmax = load(".maxVc", required=False)
    arr = load(".inittm", required=False)
    wse = load(".mxe", required=False)
    notes = []
    hmax = np.where(np.isfinite(hmax), hmax, 0.0)
    wet = hmax >= st.arrival_depth_m - ARRIVAL_TOL_M
    velocity_written = vmax is not None          # v3.12.0: the near-dam refine reads this
    if vmax is None:
        notes.append("LISFLOOD-FP wrote no maximum velocity (.maxVc).")
        vmax = np.full(hmax.shape, np.nan)
    vmax = np.where(np.isfinite(vmax) & (hmax >= st.velocity_min_depth_m), vmax, 0.0)
    if arr is None:
        arr = np.full(hmax.shape, np.nan)
    arr = np.where(np.isfinite(arr) & (arr >= 0) & wet, arr, np.nan)
    if wse is None:
        wse = np.where(hmax > 0, dem.data + hmax, np.nan)
    wse = np.where(hmax > 0, wse, np.nan)
    last_t, verr, vol, qin_max, worst = _read_mass(os.path.join(res, name + ".mass"))
    injected = hydrograph.volume_until(last_t) or hydrograph.volume_m3
    injected += sum(source_volume(src, last_t) for src in st.extra_sources)
    stats = {
        "cells": int(dem.data.size), "cell_size_m": dem.dx,
        "sim_time_hr": round(last_t / 3600.0, 2),
        "max_depth_ft": round(float(np.nanmax(hmax)) / FT, 2),
        "max_velocity_fps": round(float(np.nanmax(vmax)) / FT, 2),
        "flooded_acres": round(float((hmax >= st.arrival_depth_m).sum()) * dem.dx ** 2
                               / ACRE, 1),
        "peak_inflow_cfs": round(qin_max / CFS, 0),
        "max_volume_error_pct": round(100.0 * abs(verr) / injected, 3) if injected else None,
        "worst_interval_error_pct": round(100.0 * worst / injected, 3) if injected else None,
        "injected_acft": round(injected / ACFT, 2),
        "in_domain_acft": round(vol / ACFT, 2),
        "initial_water_acft": 0.0,
        "velocity_written": velocity_written,
    }
    if st.initial_depth is not None:
        init_d = st.initial_depth.data
        stats["initial_water_acft"] = round(float(np.nansum(np.where(np.isfinite(init_d),
                                            init_d, 0.0))) * dem.dx ** 2 / ACFT, 2)
    # v3.11.0: what the model holds against what was put in. A free FV1 edge can import water
    # (it grew to 6.5e10 m3 against 9.7e6 m3 injected at Elm River Dam 1); the near-dam refine
    # checks these before it merges anything. Only meaningful without ambient sources.
    t_ser, v_ser, _qi, qo_ser = _read_series(os.path.join(res, name + ".mass"))
    if injected and len(v_ser):
        stats["max_domain_volume_ratio"] = round(float(v_ser.max()) / injected, 3)
    # Water that left the model through its downstream edges: the cumulative boundary outflow
    # (LISFLOOD-FP Qout). The domain-extension rule only looks at how DEEP the water is on each
    # edge (> 1 ft); a wide, shallow sheet can carry a large share of the breach volume out
    # while every edge stays under the depth rule, so the flood area and consequences are
    # understated with nothing to say so. Reported here and flagged in workflow.run_dam.
    if len(qo_ser) and len(t_ser) == len(qo_ser):
        trap = getattr(np, "trapezoid", None) or np.trapz
        out_vol = float(trap(np.clip(qo_ser, 0.0, None), t_ser))
        stats["boundary_outflow_acft"] = round(out_vol / ACFT, 2)
        if injected:
            stats["boundary_outflow_pct"] = round(100.0 * out_vol / injected, 2)
    if not st.extra_sources:
        lv, lq, lt = late_inflow(os.path.join(res, name + ".mass"), float(hydrograph.duration_s))
        stats["late_boundary_inflow_acft"] = round(lv / ACFT, 2)
        stats["late_boundary_inflow_max_cfs"] = round(lq / CFS, 1)
        stats["late_boundary_inflow_pct"] = round(100.0 * lv / injected, 2) if injected else None
        if injected and lv > 0.02 * injected:
            notes.append(
                "Water came IN through the domain edges after the breach hydrograph ended "
                "({:,.0f} ac-ft, {:.1f}% of the {:,.0f} ac-ft injected; largest inflow {:,.0f} "
                "cfs at {:.1f} h). The edge is bringing water into the model, so the volumes "
                "and depths are not reliable. Close that edge or use the ACC solver.".format(
                    lv / ACFT, 100.0 * lv / injected,
                    injected / ACFT, lq / CFS, lt / 3600.0))
    if stats["max_volume_error_pct"] and stats["max_volume_error_pct"] > 1.0:
        notes.append("LISFLOOD-FP mass-balance error is {}% of the injected volume (limit 1%); "
                     "try a smaller cell size or initial time step.".format(
                         stats["max_volume_error_pct"]))
    return Envelopes(dem.like(hmax, nodata=np.nan), dem.like(vmax, nodata=np.nan),
                     dem.like(arr, nodata=np.nan), dem.like(wse, nodata=np.nan), stats, notes,
                     read_gauges(workdir, st, name))


def source_volume(src, t_end):
    """Volume (m3) a constant or time-varying extra source adds up to t_end."""
    t = np.asarray(src["t_s"], float)
    q = np.asarray(src["q_cms"], float)
    tt = np.unique(np.concatenate([t[t < t_end], [min(t_end, t[-1])]]))
    qq = np.interp(tt, t, q)
    return float(np.sum(0.5 * (qq[1:] + qq[:-1]) * np.diff(tt))) if len(tt) > 1 else 0.0


def snapshot_files(workdir, name="run"):
    """Depth snapshots LISFLOOD-FP wrote (saveint): [(index, path)] in time order.
    File k holds the depth at k x saveint seconds."""
    res = os.path.join(workdir, "results")
    out = []
    if not os.path.isdir(res):
        return out
    pre = name + "-"
    for fn in os.listdir(res):
        if fn.startswith(pre) and fn.endswith(".wd"):
            try:
                out.append((int(fn[len(pre):-3]), os.path.join(res, fn)))
            except ValueError:
                continue
    return sorted(out)


def collect_snapshots(workdir, st, out_path, name="run", step=1, remove=True):
    """Pack the depth snapshots into one compressed .npz (float32, metres, NaN
    dry) with their times and delete the text files. Returns (times_s, path) or
    (None, None) when there are none."""
    files = snapshot_files(workdir, name)
    if not files or not st.snapshot_s:
        return None, None
    arrays, times = [], []
    for k, (idx, p) in enumerate(files):
        if k % max(1, int(step)) == 0:
            g = Grid.read_asc(p)
            a = g.data.astype(np.float32)
            a[(a == g.nodata) | (a <= 0.0)] = np.nan
            arrays.append(a)
            times.append(idx * st.snapshot_s)
        if remove:
            for ext in (".wd", ".Vx", ".Vy", ".Qx", ".Qy", ".elev", ".Vc", ".Hz"):
                try:
                    os.remove(p[:-3] + ext)
                except OSError:
                    pass
    np.savez_compressed(out_path, times_s=np.asarray(times), depth=np.stack(arrays))
    return np.asarray(times), out_path


def check_lisflood(exe, workdir):
    """Run a 2 km synthetic channel for 20 minutes. Returns a dict with 'ok',
    'patched' (arrival-depth keyword honoured) and 'message'."""
    from .breach import Hydrograph
    os.makedirs(workdir, exist_ok=True)
    nr, nc, dx = 20, 100, 20.0
    X, Y = np.meshgrid((np.arange(nc) + 0.5) * dx, (np.arange(nr) + 0.5) * dx)
    z = 100.0 - 0.002 * X + 0.02 * np.abs(Y - nr * dx / 2)
    dem = Grid(z[::-1], 0.0, nr * dx, dx)
    hy = Hydrograph(np.array([0.0, 600.0, 1200.0]), np.array([30.0, 30.0, 0.0]), "test")
    st = LisfloodSettings(exe, arrival_depth_m=0.3, sim_time_s=1200.0, massint_s=60.0,
                          initial_tstep_s=1.0)
    try:
        par = write_inputs(workdir, dem, (30.0, nr * dx / 2 + 5), hy, st)
        run(workdir, par, st)
        env = read_results(workdir, dem, st, hy)
    except Exception as e:  # report, don't raise
        return {"ok": False, "patched": False, "message": str(e)}
    raw = Grid.read_asc(os.path.join(workdir, "results", "run.inittm")).data
    raw_wet = int((raw >= 0).sum())
    thr_wet = int(np.isfinite(env.arrival_hr.data).sum())
    # Patched: arrival is recorded only where the depth passed 0.3 m. Unpatched:
    # it is recorded wherever there was any water, which is far more cells.
    deep = int((env.depth.data >= 0.3 - ARRIVAL_TOL_M).sum())
    patched = raw_wet <= 1.03 * deep + 3
    msg = ("{} ran: {} cells reached 0.3 m.".format(describe_exe(exe), thr_wet))
    if not patched:
        msg += (" The arrival-depth patch is NOT active (arrival time will be the first "
                "wetting, about 1 mm). Rebuild with scripts/build_lisflood_macos.sh.")
    return {"ok": True, "patched": patched, "message": msg}


def calibrate(exe, workdir, threads=0, cell_m=10.0, sim_s=1800.0):
    """Time a synthetic breach flood to measure this computer's LISFLOOD-FP
    speed. Returns cell-seconds of simulation per wall second at `cell_m`
    (steps scale with 1/cell size), or None when it could not run."""
    import time
    from .breach import Hydrograph
    os.makedirs(workdir, exist_ok=True)
    nr, nc = 160, 400
    X, Y = np.meshgrid((np.arange(nc) + 0.5) * cell_m, (np.arange(nr) + 0.5) * cell_m)
    z = 100.0 - 0.001 * X + 0.004 * np.abs(Y - nr * cell_m / 2)
    dem = Grid(z[::-1], 0.0, nr * cell_m, cell_m)
    hy = Hydrograph(np.array([0.0, 600.0, sim_s]), np.array([300.0, 300.0, 0.0]), "calibration")
    st = LisfloodSettings(exe, threads=threads, arrival_depth_m=0.3, sim_time_s=sim_s,
                          massint_s=300.0)
    try:
        par = write_inputs(workdir, dem, (40.0, nr * cell_m / 2 + 5), hy, st)
        t0 = time.time()
        run(workdir, par, st)
        dt = time.time() - t0
    except Exception:
        return None
    return nr * nc * sim_s / max(dt, 1e-3)
