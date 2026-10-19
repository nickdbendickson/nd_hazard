"""Built-in flood engine (3.11.0): runs the LISFLOOD-FP input files the Runner writes
(.par, .dem.asc, .bci, .bdy, optional .n.asc / .start / .gauge) with the same
local-inertial equations as LISFLOOD-FP's default "acceleration" solver (Bates, Horritt
and Fewtrell 2010), in numpy, and writes the result files the Runner reads (.max, .maxVc,
.inittm, .maxtm, .mxe, .mass, .discharge, depth snapshots).

It is the fallback when no LISFLOOD-FP executable can be found: nothing to install, but
slower than LISFLOOD-FP (one core) and not LISFLOOD-FP itself. The Runner uses it when the
LISFLOOD-FP executable setting is the word "builtin", or when the setting is empty and no
LISFLOOD-FP was bundled for this platform. The "fv1" keyword is run with the same inertial
scheme (the log says so).

The scheme, per time step dt = alpha dx / sqrt(g h_max):
- discharge per unit width on every cell face,
  q = (q - g h_f dt dS) / (1 + g dt n^2 |q| / h_f^(7/3)), h_f = max(eta) - max(z) over
  the two cells, dS the water-surface slope; no flow where h_f < 1 mm;
- FREE edges: LISFLOOD-FP's free outflow (edge depth, water-surface slope to the inner
  cell, always outward), depths over 10 m capped as LISFLOOD-FP caps them on faces;
- the time step is never longer than the .par file's initial_tstep, as in LISFLOOD-FP;
- a donor-cell limiter scales a cell's outflows so it never sends more water than it
  holds, so depths stay >= 0 and mass is conserved to round-off;
- point sources (QVAR, per unit width x cell size) interpolated linearly in time.
Only the wetted part of the grid (plus a margin) is computed each step.

Run it alone: python builtin_lisflood.py -v run.par   (in the folder of the .par file)."""
import os
import sys
import time

import numpy as np

G = 9.80665
ALPHA = 0.7
DRY_M = 1e-3
MAX_HFLOW = 10.0       # LISFLOOD-FP's MaxHflow
VEL_MIN_DEPTH_M = 0.01
NODATA = -9999.0
NAME = "ND Dam Breach Runner built-in engine (local inertial, LISFLOOD-FP compatible)"


class EngineError(RuntimeError):
    pass


class Cancelled(RuntimeError):
    pass


# -- input files ---------------------------------------------------------------------------

def _read_asc(path):
    hdr = {}
    with open(path) as f:
        for _ in range(6):
            pos = f.tell()
            parts = f.readline().split()
            if len(parts) != 2 or parts[0][0].isdigit() or parts[0][0] == "-":
                f.seek(pos)
                break
            hdr[parts[0].lower()] = float(parts[1])
        data = np.loadtxt(f, ndmin=2)
    ny, nx = int(hdr["nrows"]), int(hdr["ncols"])
    dx = hdr["cellsize"]
    xll = hdr.get("xllcorner", hdr.get("xllcenter", 0.0) - 0.5 * dx)
    yll = hdr.get("yllcorner", hdr.get("yllcenter", 0.0) - 0.5 * dx)
    return data.reshape(ny, nx), xll, yll, dx, hdr.get("nodata_value", NODATA)


def _write_asc(path, a, xll, yll, dx, fmt="%.3f"):
    header = ("ncols {}\nnrows {}\nxllcorner {:.6f}\nyllcorner {:.6f}\ncellsize {:.6f}\n"
              "NODATA_value {:g}".format(a.shape[1], a.shape[0], xll, yll, dx, NODATA))
    np.savetxt(path, a, fmt=fmt, header=header, comments="")


def read_par(path):
    """{keyword: value or True} from a LISFLOOD-FP .par file."""
    par = {}
    with open(path) as f:
        for line in f:
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            p = line.split(None, 1)
            par[p[0].lower()] = p[1].strip() if len(p) > 1 else True
    return par


def read_bdy(path):
    """{name: (t_s, value)} from a .bdy file (values per unit width for QVAR)."""
    out = {}
    with open(path) as f:
        lines = [ln.strip() for ln in f]
    i = 1                                               # line 1 is a comment
    while i < len(lines):
        name = lines[i]
        i += 1
        if not name or i >= len(lines):
            continue
        p = lines[i].split()
        i += 1
        n, unit = int(float(p[0])), (p[1].lower() if len(p) > 1 else "seconds")
        scale = {"hours": 3600.0, "days": 86400.0}.get(unit, 1.0)
        vals, ts = [], []
        for _ in range(n):
            v, t = lines[i].split()[:2]
            vals.append(float(v))
            ts.append(float(t) * scale)
            i += 1
        out[name] = (np.asarray(ts), np.asarray(vals))
    return out


def read_bci(path):
    """(point sources [(x, y, name)], free edges {'N','S','E','W'})."""
    pts, free = [], set()
    with open(path) as f:
        for line in f:
            p = line.split()
            if not p:
                continue
            k = p[0].upper()
            if k == "P" and len(p) >= 5 and p[3].upper() == "QVAR":
                pts.append((float(p[1]), float(p[2]), p[4]))
            elif k in ("N", "S", "E", "W") and len(p) >= 4 and p[3].upper() == "FREE":
                free.add(k)
    return pts, free


def read_gauges(path):
    with open(path) as f:
        n = int(f.readline().split()[0])
        out = []
        for _ in range(n):
            p = f.readline().split()
            out.append((float(p[0]), float(p[1]), p[2].upper(), float(p[3])))
    return out


# -- the model -----------------------------------------------------------------------------

class Model:
    def __init__(self, z, n, h0, dx, free, sources, xll, yll):
        self.valid = np.isfinite(z) & (z != NODATA)
        self.z = np.where(self.valid, z, 0.0)
        self.n2 = np.where(self.valid, n, 0.0) ** 2
        self.h = np.where(self.valid, np.maximum(h0, 0.0), 0.0)
        self.ny, self.nx = z.shape
        self.dx = float(dx)
        self.free = free
        self.sources = sources                  # [(rows, cols, t_s, q_cms per cell)]
        self.qx = np.zeros((self.ny, self.nx + 1))
        self.qy = np.zeros((self.ny + 1, self.nx))  # positive toward increasing row (south)
        self.xll, self.yll = xll, yll
        self.src_mask = np.zeros(z.shape, bool)
        for rr, cc, _, _ in sources:
            self.src_mask[rr, cc] = True
        self.out_vol = 0.0
        self.clip_vol = 0.0

    def window(self, margin):
        wet = (self.h > 0) | self.src_mask
        rows = np.flatnonzero(wet.any(axis=1))
        cols = np.flatnonzero(wet.any(axis=0))
        if not len(rows):
            return None
        return (max(0, rows[0] - margin), min(self.ny, rows[-1] + margin + 1),
                max(0, cols[0] - margin), min(self.nx, cols[-1] + margin + 1))

    def source_rate(self, t, win):
        """m3/s per cell of every source at time t, on the window's cells (a source past the
        end of its series gives nothing)."""
        r0, r1, c0, c1 = win
        add = np.zeros((r1 - r0, c1 - c0))
        for rr, cc, ts, qs in self.sources:
            q = 0.0 if t > ts[-1] else float(np.interp(t, ts, qs))
            np.add.at(add, (rr - r0, cc - c0), q)
        return add

    def step(self, dt, win, src):
        r0, r1, c0, c1 = win
        dx = self.dx
        z = self.z[r0:r1, c0:c1]
        h = self.h[r0:r1, c0:c1]
        ok = self.valid[r0:r1, c0:c1]
        n2 = self.n2[r0:r1, c0:c1]
        eta = z + h
        qx = self.qx[r0:r1, c0:c1 + 1]          # views: faces c0 .. c1
        qy = self.qy[r0:r1 + 1, c0:c1]
        # interior x faces (between columns j-1 and j of the window)
        hf = np.maximum(eta[:, 1:], eta[:, :-1]) - np.maximum(z[:, 1:], z[:, :-1])
        wet = (hf > DRY_M) & ok[:, 1:] & ok[:, :-1]
        q = qx[:, 1:-1]
        nf = 0.5 * (n2[:, 1:] + n2[:, :-1])
        hfs = np.where(wet, np.minimum(hf, MAX_HFLOW), 1.0)
        qn = (q - G * hfs * dt * (eta[:, 1:] - eta[:, :-1]) / dx) / (
            1.0 + G * dt * nf * np.abs(q) / hfs ** (7.0 / 3.0))
        q[:] = np.where(wet, qn, 0.0)
        hf = np.maximum(eta[1:, :], eta[:-1, :]) - np.maximum(z[1:, :], z[:-1, :])
        wet = (hf > DRY_M) & ok[1:, :] & ok[:-1, :]
        q = qy[1:-1, :]
        nf = 0.5 * (n2[1:, :] + n2[:-1, :])
        hfs = np.where(wet, np.minimum(hf, MAX_HFLOW), 1.0)
        qn = (q - G * hfs * dt * (eta[1:, :] - eta[:-1, :]) / dx) / (
            1.0 + G * dt * nf * np.abs(q) / hfs ** (7.0 / 3.0))
        q[:] = np.where(wet, qn, 0.0)
        # domain edges inside the window: FREE (normal depth, outward) or closed
        self._edge(qx, qy, z, h, ok, n2, win, dt)
        # donor-cell limiter: a cell sends at most the water it holds
        out = (np.maximum(qx[:, 1:], 0) + np.maximum(-qx[:, :-1], 0)
               + np.maximum(qy[1:, :], 0) + np.maximum(-qy[:-1, :], 0)) * dt * dx
        avail = h * dx * dx + src * dt
        scale = np.where(out > avail, avail / np.where(out > 0, out, 1.0), 1.0)
        if (scale < 1.0).any():
            sx = np.ones_like(qx)
            sx[:, 1:] = np.where(qx[:, 1:] > 0, scale, 1.0)
            sx[:, :-1] = np.minimum(sx[:, :-1], np.where(qx[:, :-1] < 0, scale, 1.0))
            qx *= sx
            sy = np.ones_like(qy)
            sy[1:, :] = np.where(qy[1:, :] > 0, scale, 1.0)
            sy[:-1, :] = np.minimum(sy[:-1, :], np.where(qy[:-1, :] < 0, scale, 1.0))
            qy *= sy
        dh = dt / dx * (qx[:, :-1] - qx[:, 1:] + qy[:-1, :] - qy[1:, :]) \
            + src * dt / (dx * dx)
        hn = h + dh
        neg = hn < 0
        if neg.any():
            self.clip_vol += float(-hn[neg].sum()) * dx * dx
            hn[neg] = 0.0
        h[:] = np.where(ok, hn, 0.0)

    def _edge(self, qx, qy, z, h, ok, n2, win, dt):
        """Domain edges in the window. FREE, as LISFLOOD-FP's acceleration solver does it
        with no slope given: the outward discharge from the edge cell's depth and the
        water-surface slope to its inner neighbour, both taken as positive (always out);
        q = (|q0| + g dt h |S|) / (1 + g dt n^2 |q0| / h^(7/3))."""
        r0, r1, c0, c1 = win
        dx = self.dx

        def free(q0, hc, etac, etain, n2c, okc):
            q0 = np.abs(q0)
            hs = np.where(hc > DRY_M, np.minimum(hc, MAX_HFLOW), 1.0)
            q = (q0 + G * dt * hs * np.abs(etac - etain) / dx) / (
                1.0 + G * dt * n2c * q0 / hs ** (7.0 / 3.0))
            return np.where((hc > DRY_M) & okc, q, 0.0)

        eta = z + h
        one_c = z.shape[1] > 1
        one_r = z.shape[0] > 1
        if c0 == 0 and "W" in self.free and one_c:
            qx[:, 0] = -free(qx[:, 0], h[:, 0], eta[:, 0], eta[:, 1], n2[:, 0], ok[:, 0])
        else:
            qx[:, 0] = 0.0
        if c1 == self.nx and "E" in self.free and one_c:
            qx[:, -1] = free(qx[:, -1], h[:, -1], eta[:, -1], eta[:, -2], n2[:, -1], ok[:, -1])
        else:
            qx[:, -1] = 0.0
        if r0 == 0 and "N" in self.free and one_r:
            qy[0, :] = -free(qy[0, :], h[0, :], eta[0, :], eta[1, :], n2[0, :], ok[0, :])
        else:
            qy[0, :] = 0.0
        if r1 == self.ny and "S" in self.free and one_r:
            qy[-1, :] = free(qy[-1, :], h[-1, :], eta[-1, :], eta[-2, :], n2[-1, :], ok[-1, :])
        else:
            qy[-1, :] = 0.0

    def edge_outflow(self):
        """m3/s leaving through the domain edges with the current (limited) discharges."""
        dx = self.dx
        return dx * float(np.sum(np.maximum(-self.qx[:, 0], 0)) + np.sum(np.maximum(self.qx[:, -1], 0))
                          + np.sum(np.maximum(-self.qy[0, :], 0)) + np.sum(np.maximum(self.qy[-1, :], 0)))

    def cell_speed(self, win):
        """Largest face velocity magnitude per cell of the window (m/s): max |u| of its x
        faces and of its y faces, combined. Faces shallower than 1 cm are left out."""
        r0, r1, c0, c1 = win
        z, h = self.z[r0:r1, c0:c1], self.h[r0:r1, c0:c1]
        qx, qy = self.qx[r0:r1, c0:c1 + 1], self.qy[r0:r1 + 1, c0:c1]
        eta = z + h
        ux = np.zeros_like(qx)
        hf = np.maximum(eta[:, 1:], eta[:, :-1]) - np.maximum(z[:, 1:], z[:, :-1])
        ux[:, 1:-1] = np.where(hf > VEL_MIN_DEPTH_M, np.abs(qx[:, 1:-1])
                               / np.maximum(hf, VEL_MIN_DEPTH_M), 0.0)
        ux[:, 0] = np.where(h[:, 0] > VEL_MIN_DEPTH_M, np.abs(qx[:, 0])
                            / np.maximum(h[:, 0], VEL_MIN_DEPTH_M), 0.0)
        ux[:, -1] = np.where(h[:, -1] > VEL_MIN_DEPTH_M, np.abs(qx[:, -1])
                             / np.maximum(h[:, -1], VEL_MIN_DEPTH_M), 0.0)
        uy = np.zeros_like(qy)
        hf = np.maximum(eta[1:, :], eta[:-1, :]) - np.maximum(z[1:, :], z[:-1, :])
        uy[1:-1, :] = np.where(hf > VEL_MIN_DEPTH_M, np.abs(qy[1:-1, :])
                               / np.maximum(hf, VEL_MIN_DEPTH_M), 0.0)
        uy[0, :] = np.where(h[0, :] > VEL_MIN_DEPTH_M, np.abs(qy[0, :])
                            / np.maximum(h[0, :], VEL_MIN_DEPTH_M), 0.0)
        uy[-1, :] = np.where(h[-1, :] > VEL_MIN_DEPTH_M, np.abs(qy[-1, :])
                             / np.maximum(h[-1, :], VEL_MIN_DEPTH_M), 0.0)
        vx = np.maximum(ux[:, 1:], ux[:, :-1])
        vy = np.maximum(uy[1:, :], uy[:-1, :])
        return np.hypot(vx, vy)


# -- driver --------------------------------------------------------------------------------

def run_par(par_path, progress=None, cancel=None, log=None):
    """Run a .par file (paths in it are relative to its folder). progress(t_s, sim_s) is
    called about every 2 s; cancel() -> True stops with Cancelled. Returns a dict."""
    wd = os.path.dirname(os.path.abspath(par_path))
    par = read_par(par_path)

    def path(key):
        return os.path.join(wd, str(par[key]))

    def say(msg):
        if log:
            log(msg)

    for key in ("demfile", "sim_time"):
        if key not in par:
            raise EngineError("the .par file has no '{}'".format(key))
    z, xll, yll, dx, nod = _read_asc(path("demfile"))
    z = np.where(z == nod, np.nan, z)
    ny, nx = z.shape
    sim = float(par["sim_time"])
    massint = float(par.get("massint", 300.0))
    saveint = float(par.get("saveint", sim + 10.0))
    arr_depth = float(par.get("inittm_depth", 0.0))
    n_def = float(par.get("fpfric", 0.06))
    n = np.full(z.shape, n_def)
    if "manningfile" in par:
        ng, _, _, _, nn = _read_asc(path("manningfile"))
        n = np.where((ng == nn) | ~np.isfinite(ng) | (ng <= 0), n_def, ng)
    h0 = np.zeros(z.shape)
    if "startfile" in par:
        s, _, _, _, sn = _read_asc(path("startfile"))
        h0 = np.where((s == sn) | ~np.isfinite(s), 0.0, s)
    pts, free = read_bci(path("bcifile")) if "bcifile" in par else ([], set())
    bdy = read_bdy(path("bdyfile")) if "bdyfile" in par else {}
    ytop = yll + ny * dx
    by_name = {}
    for x, y, name in pts:
        c = int(np.floor((x - xll) / dx))
        r = int(np.floor((ytop - y) / dx))
        if not (0 <= r < ny and 0 <= c < nx):
            raise EngineError("point source {} ({:.1f}, {:.1f}) is outside the DEM".format(
                name, x, y))
        by_name.setdefault(name, []).append((r, c))
    sources = []
    for name, cells in by_name.items():
        if name not in bdy:
            raise EngineError("the .bdy file has no series '{}'".format(name))
        ts, vals = bdy[name]
        rr = np.array([c[0] for c in cells])
        cc = np.array([c[1] for c in cells])
        sources.append((rr, cc, ts, vals * dx))       # QVAR is per unit width
    gauges = read_gauges(path("gaugefile")) if "gaugefile" in par else []
    solver = "fv1" if "fv1" in par else "acceleration"
    if solver == "fv1":
        say("The built-in engine runs 'fv1' with its local-inertial scheme.")
    m = Model(z, n, h0, dx, free, sources, xll, yll)

    res = os.path.join(wd, str(par.get("dirroot", "results")))
    os.makedirs(res, exist_ok=True)
    root = os.path.join(res, str(par.get("resroot", "run")))
    hmax = m.h.copy()
    vmax = np.zeros(z.shape)
    tmax = np.full(z.shape, NODATA)
    arrival = np.full(z.shape, NODATA)
    hit = m.h > arr_depth
    arrival[hit] = 0.0

    gauge_cells = []
    for gx, gy, d, w in gauges:
        k = max(1, int(round(w / dx)))
        if d in ("E", "W"):
            c = int(np.floor((gx - xll) / dx))
            r = int(np.floor((ytop - gy) / dx))
            gauge_cells.append(("x", np.clip(np.arange(r, r + k), 0, ny - 1),
                                np.clip(c, 0, nx), 1.0 if d == "E" else -1.0))
        else:
            c = int(np.floor((gx - xll) / dx))
            r = int(np.floor((ytop - gy) / dx))
            gauge_cells.append(("y", np.clip(r, 0, ny), np.clip(np.arange(c, c + k), 0, nx - 1),
                                -1.0 if d == "N" else 1.0))

    def gauge_q():
        out = []
        for kind, a, b, sign in gauge_cells:
            if kind == "x":
                out.append(sign * float(m.qx[a, b].sum()) * dx)
            else:
                out.append(sign * float(m.qy[a, b].sum()) * dx)
        return out

    mass_f = open(root + ".mass", "w")
    mass_f.write("Time         Tstep      MinTstep   NumTsteps    Area         Vol         "
                 "Qin         Hds        Qout          Qerror         Verror       "
                 "Rain-Inf+Evap\n")
    dis_f = open(root + ".discharge", "w") if gauges else None
    if dis_f:
        dis_f.write("Time" + "".join("  gauge{}".format(i + 1) for i in range(len(gauges)))
                    + "\n")

    t = 0.0
    dt0 = float(par.get("initial_tstep", 10.0))
    steps, min_dt, last_dt = 0, 1e9, dt0
    next_mass = massint
    snap_k = 0
    next_snap = 0.0 if saveint <= sim else None
    vol0 = float(m.h.sum()) * dx * dx
    vin = vout = 0.0
    qin = qout = 0.0
    win = None
    wall = time.time()
    last_prog = wall
    t_start = time.time()

    def write_mass(tt):
        vol = float(m.h.sum()) * dx * dx
        verr = vol - (vol0 + vin - vout)
        area = float((m.h > DRY_M).sum()) * dx * dx
        mass_f.write("{:<12.3f} {:<10.4f} {:<10.4f} {:<12d} {:<12.1f} {:<12.3f} {:<12.4f} "
                     "{:<10.4f} {:<13.4f} {:<14.6f} {:<12.4f} {:<12.4f}\n".format(
                         tt, last_dt, min_dt if steps else last_dt, steps, area, vol, qin,
                         0.0, qout, 0.0, verr - write_mass.last_err, 0.0))
        write_mass.last_err = verr
        mass_f.flush()
        if dis_f:
            dis_f.write("{:.3f}".format(tt) + "".join(" {:.4f}".format(v) for v in gauge_q())
                        + "\n")
            dis_f.flush()
    write_mass.last_err = 0.0

    try:
        while t < sim - 1e-9:
            if cancel and cancel():
                raise Cancelled("cancelled")
            if next_snap is not None and t >= next_snap - 1e-9:
                _write_asc("{}-{:04d}.wd".format(root, snap_k), m.h, xll, yll, dx)
                snap_k += 1
                next_snap = snap_k * saveint if snap_k * saveint <= sim + 1e-9 else None
            if steps % 10 == 0 or win is None:
                win = m.window(12)
                if win is None:
                    win = (0, 1, 0, 1)
            hm = float(m.h[win[0]:win[1], win[2]:win[3]].max())
            dt = min(dt0, ALPHA * dx / np.sqrt(G * hm)) if hm > DRY_M else dt0
            dt = min(dt, sim - t)
            if next_snap is not None:
                dt = min(dt, max(next_snap - t, 1e-6))
            dt = min(dt, max(next_mass - t, 1e-6))
            src = m.source_rate(t + 0.5 * dt, win)
            qin = float(src.sum())
            m.step(dt, win, src)
            qout = m.edge_outflow()
            vin += qin * dt
            vout += qout * dt
            t += dt
            steps += 1
            last_dt = dt
            min_dt = min(min_dt, dt)
            r0, r1, c0, c1 = win
            hw = m.h[r0:r1, c0:c1]
            deeper = hw > hmax[r0:r1, c0:c1]
            if deeper.any():
                hmax[r0:r1, c0:c1] = np.where(deeper, hw, hmax[r0:r1, c0:c1])
                tm = tmax[r0:r1, c0:c1]
                tm[deeper] = t / 3600.0
            newly = (hw > arr_depth) & (arrival[r0:r1, c0:c1] == NODATA)
            if newly.any():
                a = arrival[r0:r1, c0:c1]
                a[newly] = t / 3600.0
            # Sample the max velocity EVERY step (not every 5th): it feeds the ACER TM-11
            # depth-velocity danger chart, whose High zone is largely velocity-driven, so a peak
            # missed on a non-sampled step biases velocity low and can under-classify. Depth
            # already updates every step; match it for velocity.
            vw = vmax[r0:r1, c0:c1]
            np.maximum(vw, m.cell_speed(win), out=vw)
            if t >= next_mass - 1e-6:
                write_mass(t)
                next_mass += massint
            now = time.time()
            if progress and now - last_prog > 2.0:
                progress(t, sim)
                last_prog = now
        vw = vmax[win[0]:win[1], win[2]:win[3]]
        np.maximum(vw, m.cell_speed(win), out=vw)
        if next_mass - massint < t - 1e-6:
            write_mass(t)
        if next_snap is not None and t >= next_snap - 1e-9:
            _write_asc("{}-{:04d}.wd".format(root, snap_k), m.h, xll, yll, dx)
    finally:
        mass_f.close()
        if dis_f:
            dis_f.close()

    valid = m.valid
    out_max = np.where(valid, hmax, NODATA)
    _write_asc(root + ".max", out_max, xll, yll, dx)
    _write_asc(root + ".maxVc", np.where(valid, vmax, NODATA), xll, yll, dx)
    _write_asc(root + ".inittm", np.where(valid, arrival, NODATA), xll, yll, dx, fmt="%.4f")
    _write_asc(root + ".maxtm", np.where(valid & (hmax > 0), tmax, NODATA), xll, yll, dx,
               fmt="%.4f")
    _write_asc(root + ".mxe", np.where(valid & (hmax > 0), m.z + hmax, NODATA), xll, yll, dx)
    vol = float(m.h.sum()) * dx * dx
    stats = {"engine": NAME, "steps": steps, "sim_time_s": t,
             "wall_s": round(time.time() - t_start, 1),
             "injected_m3": vin, "outflow_m3": vout, "final_volume_m3": vol,
             "mass_error_m3": vol - (vol0 + vin - vout), "clipped_m3": m.clip_vol}
    say("Built-in engine: {} steps, {:.2f} h simulated in {:.1f} min; volume error {:.3g} m3 "
        "of {:.4g} m3 injected.".format(steps, t / 3600.0, stats["wall_s"] / 60.0,
                                        stats["mass_error_m3"], vin))
    return stats


def main(argv):
    args = [a for a in argv[1:] if not a.startswith("-")]
    if "-version" in argv or not args:
        print(NAME)
        return 0
    try:
        run_par(args[-1], log=print,
                progress=lambda t, s: print("t = {:.2f} h".format(t / 3600.0)) if "-v" in argv
                else None)
    except (EngineError, OSError, ValueError) as e:
        print("ERROR: {}".format(e))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
