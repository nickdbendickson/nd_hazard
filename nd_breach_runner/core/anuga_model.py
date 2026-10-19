"""ANUGA (Geoscience Australia / ANU) tier. ANUGA runs in its own Python
environment (conda-forge), launched as a separate process so nothing has to
be installed into QGIS's Python.

The mesh covers the area LISFLOOD-FP flooded, widened by a buffer, with
small triangles near the dam where the flow is fastest and larger ones
downstream."""
import json
import os
import queue
import subprocess
import threading
import time

import numpy as np

from .lisflood import Envelopes, ModelError, Cancelled
from . import terrain
from . import conditioning as cond
from . import structures as structmod
from ..damsuite.keepawake import keep_awake

WORKER = os.path.join(os.path.dirname(__file__), "anuga_worker.py")


class AnugaSettings:
    def __init__(self, python, threads=0, manning_n=0.06, manning_grid=None,
                 arrival_depth_m=0.3048, sim_time_s=6 * 3600.0, yieldstep_s=300.0,
                 near_field_radius_m=1609.344, fine_area_m2=100.0, coarse_area_m2=1000.0,
                 buffer_m=500.0, algorithm="DE0", velocity_min_depth_m=0.03):
        self.python = python
        self.threads = threads
        self.manning_n = manning_n
        self.manning_grid = manning_grid
        self.arrival_depth_m = arrival_depth_m
        self.sim_time_s = sim_time_s
        self.yieldstep_s = yieldstep_s
        self.near_field_radius_m = near_field_radius_m
        self.fine_area_m2 = fine_area_m2
        self.coarse_area_m2 = coarse_area_m2
        self.buffer_m = buffer_m
        self.algorithm = algorithm
        self.velocity_min_depth_m = velocity_min_depth_m


def inlet_line(inflow_xy, direction, width_m):
    """A line across the flow, centred on the inflow point."""
    ux, uy = direction
    px, py = -uy, ux
    h = 0.5 * width_m
    x, y = inflow_xy
    return np.array([[x - px * h, y - py * h], [x + px * h, y + py * h]])


def mesh_polygons(dem, flooded, inflow_xy, st):
    """Outer mesh polygon (flooded area + buffer, inside the DEM) and the
    refined near-field polygon."""
    region = terrain.dilate(flooded, st.buffer_m, dem.dx)
    # The simplified outline can wander up to `tol` from the region edge, so keep
    # the region that far (plus a cell) away from the DEM edge and from no-data;
    # otherwise the mesh could cover terrain that was filled in, not measured.
    tol = min(st.buffer_m / 10.0, 3.0 * dem.dx)
    tol = max(tol, dem.dx)
    valid_pad = np.pad(dem.valid(), 1, constant_values=False)
    near_bad = terrain.dilate(~valid_pad, tol + dem.dx, dem.dx)[1:-1, 1:-1]
    region &= ~near_bad
    r, c = dem.rc(*inflow_xy)
    if not (dem.inside(r, c) and region[r, c]):
        # the inflow cell itself must be inside the mesh
        region |= terrain.disk_mask(dem, inflow_xy, 2.5 * dem.dx) & dem.valid()
    region = terrain.largest_component(region, (r, c))
    outer = terrain.mask_outline(dem, region, simplify_m=tol)
    near = terrain.disk_mask(dem, inflow_xy, st.near_field_radius_m)
    fine_mask = terrain.dilate(flooded & near, min(200.0, st.buffer_m / 2.0), dem.dx) & region
    shrink = terrain.dilate(~region, max(3 * dem.dx, st.buffer_m / 4.0), dem.dx)
    fine_mask &= ~shrink
    fine = np.zeros((0, 2))
    if fine_mask.sum() > 20:
        fine_mask = terrain.largest_component(fine_mask, (r, c) if fine_mask[r, c] else None)
        fine = terrain.mask_outline(dem, fine_mask, simplify_m=max(2 * dem.dx, 20.0))
    return outer, fine


def octagon(center, radius, n=8):
    a = np.linspace(0.0, 2.0 * np.pi, n, endpoint=False)
    return np.column_stack([center[0] + radius * np.cos(a), center[1] + radius * np.sin(a)])


def clip_lines_to_polygon(lines, polygon, min_len=1):
    """Pieces of the polylines that lie inside `polygon` (vertex test)."""
    out = []
    for ln in lines:
        ln = np.asarray(ln, dtype=float)
        if len(ln) < 2:
            continue
        ins = cond.points_in_polygon(ln[:, 0], ln[:, 1], polygon)
        run = []
        for p, ok in zip(ln, ins):
            if ok:
                run.append(p)
            else:
                if len(run) > min_len:
                    out.append(np.array(run))
                run = []
        if len(run) > min_len:
            out.append(np.array(run))
    return out


def write_job(path, dem, outer, fine, inlet, hydrograph, st, out_grid, initial_depth=None,
              culverts=None, breaklines=None, regions=None):
    job = {
        "dem": dem.masked().astype(np.float32), "dem_x0": dem.x0, "dem_y0": dem.y0,
        "dem_dx": dem.dx, "manning_n": st.manning_n,
        "manning": (st.manning_grid.masked().astype(np.float32) if st.manning_grid is not None
                    else np.zeros(0)),
        "boundary": outer, "fine": fine, "inlet": inlet,
        "fine_area": st.fine_area_m2, "coarse_area": st.coarse_area_m2,
        "hyd_t": hydrograph.t_s, "hyd_q": hydrograph.q_cms,
        "sim_time": st.sim_time_s, "yieldstep": st.yieldstep_s,
        "arrival_depth": st.arrival_depth_m, "velocity_min_depth": st.velocity_min_depth_m,
        "algorithm": st.algorithm, "threads": int(st.threads or 0),
        "out_x0": out_grid.x0, "out_y0": out_grid.y0, "out_dx": out_grid.dx,
        "out_nx": out_grid.nx, "out_ny": out_grid.ny,
    }
    if initial_depth is not None:
        job["init_depth"] = initial_depth.masked().astype(np.float32)
    if culverts:
        job["culverts_json"] = json.dumps(culverts)
    if breaklines:
        job["breaklines_json"] = json.dumps([np.asarray(b).tolist() for b in breaklines])
    if regions:
        job["regions_json"] = json.dumps(regions)
    np.savez(path, **job)


# QGIS sets these for its own Python; the ANUGA interpreter must not inherit them. 3.18.0:
# PYTHONNOUSERSITE too - a QGIS started with the user site off would pass it on, and an ANUGA
# installed with pip --user is then not found.
DROPPED_ENV = ("PYTHONHOME", "PYTHONPATH", "QGIS_PREFIX_PATH", "PYTHONNOUSERSITE")


def worker_env(threads=0, base=None):
    """The environment handed to the ANUGA interpreter (_launch and check_anuga): this
    process's, without QGIS's own Python settings (DROPPED_ENV); OMP_NUM_THREADS when a thread
    count is given."""
    env = dict(os.environ if base is None else base)
    for k in DROPPED_ENV:
        env.pop(k, None)
    if threads:
        env["OMP_NUM_THREADS"] = str(int(threads))
    return env


def _launch(st, job, result, workdir, progress, cancel):
    """Run the worker in the ANUGA interpreter; returns (process, log path)."""
    env = worker_env(st.threads)
    log_path = os.path.join(workdir, "anuga.log")
    t0 = time.time()
    with open(log_path, "w") as logf:
        proc = subprocess.Popen([st.python, "-u", WORKER, job, result], cwd=workdir, env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                bufsize=1)
        lines = queue.Queue()

        def pump():
            for ln in proc.stdout:
                lines.put(ln)
            lines.put(None)

        threading.Thread(target=pump, daemon=True).start()
        done = False
        while not done:
            if cancel and cancel():      # also while ANUGA is silent (meshing, start-up)
                proc.kill()
                proc.wait()
                raise Cancelled("cancelled")
            try:
                line = lines.get(timeout=0.5)
            except queue.Empty:
                continue
            if line is None:
                done = True
                continue
            logf.write(line)
            logf.flush()
            if line.startswith("PROGRESS") and progress:
                parts = line.split(None, 2)
                try:
                    progress(float(parts[1]), "ANUGA " + (parts[2].strip() if len(parts) > 2
                                                          else ""))
                except ValueError:
                    pass
        proc.wait()
    return proc, log_path


def _tail_text(path, chars=2000):
    """The last `chars` characters of a text file ('' when it cannot be read)."""
    try:
        with open(path, errors="replace") as f:
            return f.read()[-chars:]
    except OSError:
        return ""


def run(workdir, dem, flooded, inflow_xy, direction, inlet_width_m, hydrograph, st,
        progress=None, cancel=None, log=None, structures=None, initial_depth=None,
        refine_lines=None, reuse=False):
    # v3.12.0: the computer is kept awake while ANUGA runs (damsuite.keepawake, beside the
    # worker process, which stays this plugin's own child so Cancel still kills it)
    with keep_awake("ANUGA", log=log):
        return _run(workdir, dem, flooded, inflow_xy, direction, inlet_width_m, hydrograph,
                    st, progress, cancel, log, structures, initial_depth, refine_lines, reuse)


def _run(workdir, dem, flooded, inflow_xy, direction, inlet_width_m, hydrograph, st,
         progress=None, cancel=None, log=None, structures=None, initial_depth=None,
         refine_lines=None, reuse=False):
    if not st.python or not os.path.isfile(st.python):
        raise ModelError("ANUGA Python interpreter not found: {}".format(st.python))
    os.makedirs(workdir, exist_ok=True)
    outer, fine = mesh_polygons(dem, flooded, inflow_xy, st)
    inlet = inlet_line(inflow_xy, direction, inlet_width_m)
    job = os.path.join(workdir, "anuga_job.npz")
    result = os.path.join(workdir, "anuga_result.npz")
    culverts, cnotes = structmod.anuga_culverts(structures or [], dem, outer)
    regions = []
    for c in culverts:      # small triangles around both ends of each culvert
        r = max(3.0 * c["width"], 25.0)
        for e in c["end_points"]:
            regions.append({"poly": octagon(e, r).tolist(), "area": min(st.fine_area_m2, 25.0)})
    step = max(5.0, float(np.sqrt(max(st.fine_area_m2, 1.0))))
    breaklines = clip_lines_to_polygon([cond.densify(np.asarray(b, float), step)
                                        for b in (refine_lines or [])], outer)
    write_job(job, dem, outer, fine, inlet, hydrograph, st, dem, initial_depth, culverts,
              breaklines, regions)
    np.savetxt(os.path.join(workdir, "mesh_boundary.csv"), outer, delimiter=",",
               header="x,y", comments="", fmt="%.3f")
    if len(fine):
        np.savetxt(os.path.join(workdir, "mesh_fine_area.csv"), fine, delimiter=",",
                   header="x,y", comments="", fmt="%.3f")
    have = bool(reuse) and os.path.exists(result)
    if os.path.exists(result) and not have:
        os.remove(result)
    t0 = time.time()
    if have:
        log_path = os.path.join(workdir, "anuga.log")

        class _Done:
            returncode = 0
        proc = _Done()
    else:
        proc, log_path = _launch(st, job, result, workdir, progress, cancel)
    if proc.returncode != 0 or not os.path.exists(result):
        tail = _tail_text(log_path)
        raise ModelError("ANUGA stopped with code {}:\n{}".format(proc.returncode, tail))
    # 3.4.1: read the archive inside `with` - an open .npz keeps the file locked on Windows
    # until garbage collection, which stopped the work folder from being removed
    with np.load(result) as r:
        stats = json.loads(str(r["stats"]))
        depth, vel, arr, wse = r["depth"], r["velocity"], r["arrival_hr"], r["wse"]
    stats.update({
        "max_depth_ft": round(float(np.nanmax(depth)) / 0.3048, 2),
        "max_velocity_fps": round(float(np.nanmax(vel)) / 0.3048, 2),
        "flooded_acres": round(float(np.nansum(depth >= st.arrival_depth_m)) * dem.dx ** 2
                               / 4046.8564224, 1),
        "max_edge_depth_ft": round(stats["max_edge_depth_m"] / 0.3048, 2),
    })
    if log:
        log("ANUGA finished in {:.1f} min ({} triangles)".format((time.time() - t0) / 60.0,
                                                               stats.get("triangles")))
    notes = list(cnotes)
    for e in stats.get("culvert_errors", []):
        notes.append("ANUGA culvert not applied: " + e)
    if refine_lines and not stats.get("breaklines_used") and breaklines:
        notes.append("ANUGA: the embankment lines could not be used as mesh edges (they cross or "
                     "touch each other); the mesh was made without them.")
    return Envelopes(dem.like(depth, np.nan), dem.like(vel, np.nan), dem.like(arr, np.nan),
                     dem.like(wse, np.nan), stats, notes), outer, fine


def check_anuga(python, workdir):
    """Import ANUGA in the given interpreter and report its version."""
    if not python or not os.path.isfile(python):
        return {"ok": False, "message": "ANUGA Python interpreter not found: {}".format(python)}
    env = worker_env()
    code = ("import anuga, numpy, scipy, matplotlib; "
            "import sys; print(getattr(anuga,'__version__','?'), sys.version.split()[0])")
    try:
        out = subprocess.run([python, "-c", code], capture_output=True, text=True, env=env,
                             timeout=300)
    except Exception as e:
        return {"ok": False, "message": str(e)}
    if out.returncode != 0:
        return {"ok": False, "message": (out.stdout + out.stderr)[-1500:]}
    ver = out.stdout.strip().splitlines()[-1]
    return {"ok": True, "message": "ANUGA {} (Python {})".format(*ver.split()[:2])}
