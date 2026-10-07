"""LISFLOOD-FP 8.x input writers and result readers.  numpy only, no QGIS/GDAL.

Verified against a real LISFLOOD-FP 8.1 build (see tests/test_solver_io.py):
  * .bci   "P x y QVAR id"  point sources; several P lines may share one id.
  * .bdy   QVAR values for point sources are PER UNIT WIDTH (m2/s); the code
           multiplies by the cell size.  So value = Q / (n_points * cell).
  * ACC solver (keyword `acceleration` + `hazard`) writes .max (depth),
    .maxVc (max cell velocity), .maxVcd, .inittm (arrival, hours, -9999 = dry).
  * FV1 solver (full shallow-water) writes only .max plus snapshot grids
    (-NNNN.wd / .Vx / .Vy with `voutput`), so max velocity and arrival are
    assembled from snapshots and are only as fine as `saveint`.
"""
import glob
import math
import os

import numpy as np

NODATA = -9999.0

# NLCD class -> Manning's n.  Typical published values; EDIT for your standard.
NLCD_N = {11: 0.035, 12: 0.030, 21: 0.040, 22: 0.080, 23: 0.120, 24: 0.150,
          31: 0.030, 41: 0.100, 42: 0.110, 43: 0.100, 52: 0.050, 71: 0.035,
          81: 0.033, 82: 0.037, 90: 0.100, 95: 0.070}


# ------------------------------------------------------------------ grids
def write_ascii_grid(path, arr, xll, yll, cell, nodata=NODATA):
    a = np.where(np.isfinite(arr), arr, nodata)
    with open(path, "w") as f:
        f.write(f"ncols {a.shape[1]}\nnrows {a.shape[0]}\nxllcorner {xll:.6f}\n"
                f"yllcorner {yll:.6f}\ncellsize {cell:.6f}\nNODATA_value {nodata:g}\n")
        np.savetxt(f, a, fmt="%.4f")


def read_ascii_grid(path):
    with open(path) as f:
        hdr = {}
        for _ in range(6):
            k, v = f.readline().split()
            hdr[k.lower()] = float(v)
    arr = np.loadtxt(path, skiprows=6, ndmin=2)
    return arr, hdr


def manning_from_nlcd(nlcd, default=0.05, table=None):
    t = table or NLCD_N
    out = np.full(nlcd.shape, default, dtype="float64")
    for k, v in t.items():
        out[nlcd == k] = v
    return out


# ---------------------------------------------------------- breach inflow
def injection_points(breach_xy, flow_az_deg, bb_m, hb_m, cell, xll, yll, ncols, nrows,
                     n_max=25):
    """Cell-centre point sources across the breach, just downstream of the toe.

    Injecting at the crest would pile the whole flood into one cell on top of
    the embankment, so the sources sit `toe` metres downstream, spread across
    the breach width.  Returns a list of (x, y) cell centres, unique per cell.
    """
    az = math.radians(flow_az_deg)
    dx, dy = math.sin(az), math.cos(az)             # downstream unit vector
    px, py = -dy, dx                                # perpendicular
    toe = min(max(2.5 * hb_m + 5.0, 3.0 * cell), 250.0)
    cx, cy = breach_xy[0] + dx * toe, breach_xy[1] + dy * toe
    width = min(max(bb_m, 2.0 * cell), 300.0)
    n = int(min(max(round(width / cell), 1), n_max))
    seen, pts = set(), []
    for i in range(n):
        off = (i + 0.5) / n * width - width / 2.0
        x, y = cx + px * off, cy + py * off
        col = int((x - xll) // cell)
        row = int((yll + nrows * cell - y) // cell)
        if not (0 <= col < ncols and 0 <= row < nrows):
            continue
        if (col, row) in seen:
            continue
        seen.add((col, row))
        pts.append((xll + (col + 0.5) * cell, yll + nrows * cell - (row + 0.5) * cell))
    return pts


def write_bci(path, pts, ident="bc1"):
    with open(path, "w") as f:
        for x, y in pts:
            f.write(f"P {x:.3f} {y:.3f} QVAR {ident}\n")


def write_bdy(path, ident, t_s, q_m3s, n_points, cell):
    t, q = np.asarray(t_s, float), np.asarray(q_m3s, float)
    if q[-1] != 0.0:               # LISFLOOD-FP holds the last value forever; force it to stop
        t, q = np.append(t, t[-1] + 1.0), np.append(q, 0.0)
    with open(path, "w") as f:
        f.write("breach hydrograph\n")
        f.write(f"{ident}\n{len(t)} seconds\n")
        for ti, qi in zip(t, q):
            f.write(f"{qi / (n_points * cell):.6f} {ti:.2f}\n")


def write_sources(bci_path, bdy_path, sources, cell):
    """Several inflow sources, each a dict(pts, t, q, start_s).  Idents bc1, bc2, ...

    A source that starts later (cascade: a downstream dam failing when the flood
    reaches it) gets zero inflow until start_s.
    """
    with open(bci_path, "w") as f:
        for k, s in enumerate(sources, 1):
            for x, y in s["pts"]:
                f.write(f"P {x:.3f} {y:.3f} QVAR bc{k}\n")
    with open(bdy_path, "w") as f:
        f.write("breach hydrographs\n")
        for k, s in enumerate(sources, 1):
            t = np.asarray(s["t"], float) + float(s.get("start_s", 0.0))
            q = np.asarray(s["q"], float)
            if t[0] > 0:
                t = np.concatenate(([0.0, max(t[0] - 1.0, 0.0)], t)); q = np.concatenate(([0.0, 0.0], q))
            if q[-1] != 0.0:
                t, q = np.append(t, t[-1] + 1.0), np.append(q, 0.0)
            f.write(f"bc{k}\n{len(t)} seconds\n")
            n = len(s["pts"])
            for ti, qi in zip(t, q):
                f.write(f"{qi / (n * cell):.6f} {ti:.2f}\n")


def read_hydrograph_csv(path):
    d = np.loadtxt(path, delimiter=",", skiprows=1, ndmin=2)
    return d[:, 0], d[:, 1]


def write_par(path, dem, resroot, dirroot, sim_time_s, saveint_s, solver="acc",
              fpfric=0.05, manningfile=None, bci="run.bci", bdy="run.bdy"):
    lines = [f"DEMfile {dem}", f"resroot {resroot}", f"dirroot {dirroot}",
             f"bcifile {bci}", f"bdyfile {bdy}", f"sim_time {sim_time_s:g}",
             "initial_tstep 1", f"saveint {saveint_s:g}", f"massint {saveint_s:g}"]
    lines.append(f"manningfile {manningfile}" if manningfile else f"fpfric {fpfric}")
    if solver == "acc":
        lines += ["acceleration", "hazard"]
    else:
        lines += ["fv1", "voutput"]
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")


# ---------------------------------------------------------------- results
def read_results(dirroot, resroot, solver, saveint_s, dry_thresh=0.03):
    """Return dict(depth, vel, arrive_h) as float arrays (arrive_h = -9999 if dry)."""
    base = os.path.join(dirroot, resroot)
    depth, _ = read_ascii_grid(base + ".max")
    depth = np.where(depth < 0, 0.0, depth)
    if solver == "acc":
        vel, _ = read_ascii_grid(base + ".maxVc")
        vel = np.where(vel < 0, 0.0, vel)
        arr, _ = read_ascii_grid(base + ".inittm")
        # ACC gives max velocity and the depth AT that moment, not max(depth x velocity):
        # max depth x max velocity is an upper bound (conservative); the other a lower bound
        dv = depth * vel
        try:
            vcd, _ = read_ascii_grid(base + ".maxVcd")
            dv_lo = np.where(vcd > 0, vcd, 0.0) * vel
        except (OSError, ValueError):
            dv_lo = None
        return {"depth": depth, "vel": vel, "arrive_h": arr, "dv": dv, "dv_lo": dv_lo,
                "dv_kind": "upper bound (max depth x max velocity)"}
    wds = sorted(glob.glob(base + "-*.wd"))
    vel = np.zeros_like(depth)
    dv = np.zeros_like(depth)
    arrive = np.full(depth.shape, NODATA)
    for i, wdp in enumerate(wds):
        wd, _ = read_ascii_grid(wdp)
        vx, _ = read_ascii_grid(wdp[:-3] + ".Vx")
        vy, _ = read_ascii_grid(wdp[:-3] + ".Vy")
        sp = np.hypot(np.where(vx < -999, 0, vx), np.where(vy < -999, 0, vy))
        vel = np.maximum(vel, np.where(wd > dry_thresh, sp, 0.0))
        dv = np.maximum(dv, np.where(wd > dry_thresh, sp * wd, 0.0))
        newly = (wd > dry_thresh) & (arrive == NODATA)
        arrive[newly] = i * saveint_s / 3600.0
    return {"depth": depth, "vel": vel, "arrive_h": arrive, "dv": dv, "dv_lo": None,
            "dv_kind": f"max over snapshots every {saveint_s:g} s"}


def edge_flooded(depth, thresh=0.05, margin=1):
    """True if flood water reaches the outer ring of the model domain."""
    m = margin
    ring = np.ones(depth.shape, bool)
    ring[m:-m, m:-m] = False
    return bool(np.any(depth[ring] > thresh))
