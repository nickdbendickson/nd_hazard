"""ANUGA worker: runs in the separate ANUGA Python environment, not inside
QGIS. It only needs numpy, scipy, matplotlib and anuga.

    python anuga_worker.py job.npz result.npz

job.npz holds (all SI, coordinates in the model CRS):
  dem, dem_x0, dem_y0, dem_dx          model DEM (row 0 = top)
  manning (optional grid) or manning_n
  boundary        (N, 2) outer polygon of the mesh
  fine            (M, 2) polygon for the refined near-dam area (may be empty)
  fine_area, coarse_area               maximum triangle areas, m2
  inlet           (2, 2) line across the flow just below the dam
  hyd_t, hyd_q    breach hydrograph, s and m3/s
  sim_time, yieldstep, arrival_depth, velocity_min_depth, algorithm, threads
  out_x0, out_y0, out_dx, out_nx, out_ny   grid the results are written on
optional:
  init_depth      grid like dem: initial water depth (m), ponds that start full
  culverts_json   list of culvert specs (end_points, invert_elevations, width,
                  height, barrels, shape, manning, losses, apron)
  breaklines_json list of polylines that become mesh edges (embankment crests)
  regions_json    extra refined polygons [{"poly": [[x, y]...], "area": m2}]

result.npz: depth, velocity, arrival_hr, wse (NaN = dry / outside), plus
stats as a JSON string. Progress goes to stdout as lines starting "PROGRESS".
"""
import json
import sys
import time

import numpy as np


def bilinear(dem, x0, y0, dx, x, y):
    a = dem
    ny, nx = a.shape
    fc = (x - x0) / dx - 0.5
    fr = (y0 - y) / dx - 0.5
    c0 = np.clip(np.floor(fc).astype(int), 0, nx - 2)
    r0 = np.clip(np.floor(fr).astype(int), 0, ny - 2)
    tc = np.clip(fc - c0, 0.0, 1.0)
    tr = np.clip(fr - r0, 0.0, 1.0)
    v = (a[r0, c0] * (1 - tc) * (1 - tr) + a[r0, c0 + 1] * tc * (1 - tr)
         + a[r0 + 1, c0] * (1 - tc) * tr + a[r0 + 1, c0 + 1] * tc * tr)
    return v


def fill_nan(a):
    """Replace NaN with the nearest valid value so the mesh never sees no-data."""
    from scipy.ndimage import distance_transform_edt
    bad = ~np.isfinite(a)
    if not bad.any():
        return a
    idx = distance_transform_edt(bad, return_distances=False, return_indices=True)
    return a[tuple(idx)]


def main(job_path, out_path):
    import anuga
    job = dict(np.load(job_path, allow_pickle=False))
    s = lambda k: float(job[k])
    x_ref, y_ref = s("dem_x0"), s("dem_y0") - job["dem"].shape[0] * s("dem_dx")
    # work in local coordinates (lower-left of the DEM = 0, 0)
    dem = fill_nan(job["dem"].astype(float))
    dx = s("dem_dx")
    lx0, ly0 = 0.0, dem.shape[0] * dx          # local left and top edges

    def to_local(p):
        p = np.asarray(p, dtype=float).reshape(-1, 2)
        return np.column_stack([p[:, 0] - x_ref, p[:, 1] - y_ref])

    boundary = to_local(job["boundary"])
    fine = to_local(job["fine"]) if job["fine"].size else None
    inlet = to_local(job["inlet"])
    nb = len(boundary)
    regions = [[fine.tolist(), s("fine_area")]] if fine is not None and len(fine) >= 3 else []
    for extra in (json.loads(str(job["regions_json"])) if "regions_json" in job else []):
        regions.append([to_local(extra["poly"]).tolist(), float(extra["area"])])
    regions = regions or None
    breaklines = [to_local(b).tolist() for b in
                  (json.loads(str(job["breaklines_json"])) if "breaklines_json" in job else [])]
    t0 = time.time()
    # A zero geo-reference keeps ANUGA's internal coordinates equal to ours
    # (by default it shifts them to the polygon's lower-left corner).
    georef = anuga.Geo_reference(xllcorner=0.0, yllcorner=0.0)
    kw = dict(boundary_tags={"outflow": list(range(nb))},
              maximum_triangle_area=s("coarse_area"), interior_regions=regions,
              mesh_geo_reference=georef, use_cache=False, verbose=False)
    def make_domain(extra):
        try:   # ANUGA 3.x writes the mesh to a file
            return anuga.create_domain_from_regions(
                boundary.tolist(), mesh_filename=job_path.replace(".npz", ".msh"), **dict(kw, **extra))
        except TypeError:
            return anuga.create_domain_from_regions(boundary.tolist(), **dict(kw, **extra))

    breaklines_used = False
    if breaklines:
        try:
            domain = make_domain({"breaklines": breaklines})
            breaklines_used = True
        except Exception as e:      # e.g. crossing lines: mesh without them
            print("PROGRESS 0 breaklines dropped: {}".format(str(e)[:120]), flush=True)
            domain = make_domain({})
    else:
        domain = make_domain({})
    ntri = len(domain)
    print("PROGRESS 0 mesh {} triangles".format(ntri), flush=True)

    domain.set_name("anuga_breach")
    domain.set_store(False)
    try:
        domain.set_flow_algorithm(str(job["algorithm"]))
    except Exception:
        domain.set_flow_algorithm("DE0")
    threads = int(job["threads"]) if "threads" in job else 0
    if threads > 0:
        setter = getattr(anuga, "set_omp_num_threads", None)
        if setter:
            try:
                setter(threads, verbose=False)
            except TypeError:
                setter(threads)
    domain.set_minimum_storable_height(0.001)

    domain.set_quantity("elevation", lambda x, y: bilinear(dem, lx0, ly0, dx, x, y),
                        location="centroids")
    if "manning" in job and job["manning"].size:
        man = fill_nan(job["manning"].astype(float))
        domain.set_quantity("friction", lambda x, y: bilinear(man, lx0, ly0, dx, x, y),
                            location="centroids")
    else:
        domain.set_quantity("friction", s("manning_n"))
    init = None
    if "init_depth" in job and job["init_depth"].size:
        init = np.where(np.isfinite(job["init_depth"]), job["init_depth"], 0.0).astype(float)
        domain.set_quantity("stage", lambda x, y: bilinear(dem, lx0, ly0, dx, x, y)
                            + bilinear(init, lx0, ly0, dx, x, y), location="centroids")
    else:
        domain.set_quantity("stage", expression="elevation", location="centroids")
    h0 = domain.quantities["stage"].centroid_values - domain.quantities["elevation"].centroid_values
    vol_initial = float(np.sum(np.clip(h0, 0, None) * domain.areas))

    # Free outflow on the whole outer boundary. A transmissive (zero-gradient)
    # boundary reports the depth at the edge without the bias of a drained edge
    # (a fixed stage far below the ground reads about 0.1 ft shallower).
    try:
        Bo = anuga.Transmissive_boundary(domain)
    except Exception:
        Bo = anuga.Dirichlet_boundary([float(np.nanmin(dem)) - 50.0, 0.0, 0.0])
    domain.set_boundary({"outflow": Bo})

    hyd_t = job["hyd_t"].astype(float)
    hyd_q = job["hyd_q"].astype(float)
    qfun = lambda t: float(np.interp(t, hyd_t, hyd_q, right=0.0))
    anuga.Inlet_operator(domain, inlet.tolist(), Q=qfun)
    culverts = json.loads(str(job["culverts_json"])) if "culverts_json" in job else []
    culvert_errors = []
    for c in culverts:
        ends = to_local(c["end_points"]).tolist()
        common = dict(losses=float(c["losses"]), barrels=float(c["barrels"]), end_points=ends,
                      invert_elevations=[float(v) for v in c["invert_elevations"]],
                      apron=float(c["apron"]), manning=float(c["manning"]),
                      use_momentum_jet=True, use_velocity_head=False, verbose=False)
        try:
            if c.get("shape") == "pipe":
                anuga.Boyd_pipe_operator(domain, diameter=float(c["width"]), **common)
            else:
                anuga.Boyd_box_operator(domain, width=float(c["width"]),
                                        height=float(c["height"]), **common)
        except Exception as e:
            culvert_errors.append("{}: {}".format(c.get("name") or "culvert", str(e)[:200]))

    arrival_depth = s("arrival_depth")
    vmin_depth = s("velocity_min_depth")

    def outflow_only_momentum(xm, ym, nx, ny):
        """Mirror the normal momentum where it points inward (would add water)."""
        xm, ym = np.asarray(xm, float), np.asarray(ym, float)
        nx, ny = np.asarray(nx, float), np.asarray(ny, float)
        n = np.hypot(nx, ny)
        n = np.where(n > 0, n, 1.0)
        nx, ny = nx / n, ny / n
        inward = xm * nx + ym * ny < 0
        if inward.any():
            # reflect the normal component
            proj = xm * nx + ym * ny
            xm = np.where(inward, xm - 2 * proj * nx, xm)
            ym = np.where(inward, ym - 2 * proj * ny, ym)
        return xm, ym

    def apply_outflow_boundary(domain, ids, vol_ids, edge_ids, centroid=False):
        """Apply outflow-only to a batch of edges. Returns the count of inward edges."""
        if ids is None or len(ids) == 0:
            return 0
        if centroid:
            q = np.array([domain.get_conserved_quantities(v) for v in vol_ids], float)
        else:
            q = np.array([domain.get_conserved_quantities(v, edge=e)
                          for v, e in zip(vol_ids, edge_ids)], float)
        n = np.array([domain.normals[v, 2 * e:2 * e + 2]
                      for v, e in zip(vol_ids, edge_ids)], float)
        xm, ym = outflow_only_momentum(q[:, 1], q[:, 2], n[:, 0], n[:, 1])
        inward = 0
        for i, (v, e) in enumerate(zip(vol_ids, edge_ids)):
            if abs(xm[i] - q[i, 1]) > 1e-12 or abs(ym[i] - q[i, 2]) > 1e-12:
                inward += 1
            domain.set_conserved_quantities(v, [q[i, 0], xm[i], ym[i]], edge=e)
        return inward

    class Outflow_boundary(anuga.Boundary):
        """Transmissive for water leaving, a wall for water that would enter."""

        def __init__(self, domain=None):
            anuga.Boundary.__init__(self)
            if domain is None:
                raise Exception("Domain must be specified for the outflow boundary")
            self.domain = domain
            self.inward_edges = 0

        def __repr__(self):
            return "Outflow_boundary"

        def evaluate(self, vol_id, edge_id):
            d = self.domain
            if d.centroid_transmissive_bc:
                qv = np.array(d.get_conserved_quantities(vol_id), float)
            else:
                qv = np.array(d.get_conserved_quantities(vol_id, edge=edge_id), float)
            n = d.normals[vol_id, 2 * edge_id:2 * edge_id + 2]
            xm, ym = outflow_only_momentum([qv[1]], [qv[2]], [n[0]], [n[1]])
            qv[1], qv[2] = xm[0], ym[0]
            return qv

        def evaluate_segment(self, domain, segment_edges):
            if segment_edges is None or domain is None:
                return
            ids = np.asarray(segment_edges)
            if ids.size == 0:
                return
            self.inward_edges = apply_outflow_boundary(
                domain, ids, domain.boundary_cells[ids], domain.boundary_edges[ids],
                centroid=bool(domain.centroid_transmissive_bc))

    Outflow_boundary.__module__ = __name__
    Outflow_boundary.__qualname__ = "Outflow_boundary"

    class Envelope(anuga.Operator):
        """Updates the maxima every time step, not just at yield steps."""

        def __init__(self, dom):
            anuga.Operator.__init__(self, dom, description="envelope", label="envelope")
            n = len(dom)
            self.hmax = np.zeros(n)
            self.vmax = np.zeros(n)
            self.wmax = np.full(n, -np.inf)
            self.arr = np.full(n, np.nan)
            self.xm = dom.quantities["xmomentum"].centroid_values
            self.ym = dom.quantities["ymomentum"].centroid_values

        def __call__(self):
            h = self.stage_c - self.elev_c
            np.maximum(self.hmax, h, out=self.hmax)
            np.maximum(self.wmax, np.where(h > 0.001, self.stage_c, -np.inf), out=self.wmax)
            deep = h >= vmin_depth
            sp = np.zeros_like(h)
            sp[deep] = np.hypot(self.xm[deep], self.ym[deep]) / h[deep]
            np.maximum(self.vmax, sp, out=self.vmax)
            new = np.isnan(self.arr) & (h >= arrival_depth)
            if new.any():
                self.arr[new] = self.domain.get_time() / 3600.0

        def parallel_safe(self):
            return True

        def statistics(self):
            return "Envelope operator"

        def timestepping_statistics(self):
            return ""

    env = Envelope(domain)
    sim_time = s("sim_time")
    ystep = s("yieldstep")
    for t in domain.evolve(yieldstep=ystep, finaltime=sim_time):
        print("PROGRESS {:.2f} t={:.0f}s".format(100.0 * t / sim_time, t), flush=True)

    # volume check
    h = domain.quantities["stage"].centroid_values - domain.quantities["elevation"].centroid_values
    vol_left = float(np.sum(np.clip(h, 0, None) * domain.areas))

    # rasterise the piecewise-constant centroid values onto the output grid
    import matplotlib.tri as mtri
    nodes = domain.nodes
    tris = domain.triangles
    tri = mtri.Triangulation(nodes[:, 0], nodes[:, 1], tris)
    finder = tri.get_trifinder()
    ox0, oy0, odx = s("out_x0") - x_ref, s("out_y0") - y_ref, s("out_dx")
    onx, ony = int(job["out_nx"]), int(job["out_ny"])
    gx = ox0 + (np.arange(onx) + 0.5) * odx
    gy = oy0 - (np.arange(ony) + 0.5) * odx
    GX, GY = np.meshgrid(gx, gy)
    ti = finder(GX, GY)
    inside = ti >= 0
    tidx = np.where(inside, ti, 0)

    def ras(v, dry_nan=True):
        out = np.where(inside, v[tidx], np.nan)
        return out

    depth = ras(env.hmax)
    wet = depth >= 0.001
    depth = np.where(inside, np.where(wet, depth, 0.0), np.nan)
    vel = np.where(inside, np.where(depth >= vmin_depth, ras(env.vmax), 0.0), np.nan)
    arr = np.where(inside, ras(env.arr), np.nan)
    wse = np.where(wet, ras(env.wmax), np.nan)
    wse[~np.isfinite(wse)] = np.nan

    # largest depth next to the mesh boundary (flood leaving the domain)
    bnd = domain.boundary
    edge_tris = np.unique([k[0] for k in bnd.keys()])
    edge_depth = float(env.hmax[edge_tris].max()) if len(edge_tris) else 0.0

    tt = np.unique(np.concatenate([hyd_t[hyd_t < sim_time], [min(sim_time, hyd_t[-1])]]))
    qq = np.interp(tt, hyd_t, hyd_q)          # only what enters before the run ends
    injected = float(np.trapezoid(qq, tt)) if hasattr(np, "trapezoid") \
        else float(np.trapz(qq, tt))
    stats = {"triangles": ntri, "runtime_min": round((time.time() - t0) / 60.0, 2),
             "max_edge_depth_m": edge_depth, "volume_in_domain_m3": vol_left,
             "initial_volume_m3": vol_initial, "breaklines_used": breaklines_used,
             "culverts": len(culverts) - len(culvert_errors), "culvert_errors": culvert_errors,
             "injected_m3": injected, "anuga_version": getattr(anuga, "__version__", "?"),
             "algorithm": str(job["algorithm"])}
    np.savez_compressed(out_path, depth=depth, velocity=vel, arrival_hr=arr, wse=wse,
                        stats=json.dumps(stats))
    print("PROGRESS 100 done", flush=True)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(2)
    main(sys.argv[1], sys.argv[2])
