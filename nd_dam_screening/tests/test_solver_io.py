"""End-to-end check of the solver input writers/readers against a real LISFLOOD-FP.

Set LISFLOOD_EXE to the executable; the test is skipped if it is not available.
"""
import glob
import os
import shutil
import subprocess
import sys
import tempfile

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "core"))
import solver_io as sio  # noqa: E402

exe = os.environ.get("LISFLOOD_EXE", "")

# ---- pure-python checks (always run) -------------------------------------
nx, ny, cell = 100, 60, 10.0
xll, yll = 0.0, 0.0
pts = sio.injection_points((100.0, 300.0), 90.0, bb_m=40.0, hb_m=8.0, cell=cell,
                           xll=xll, yll=yll, ncols=nx, nrows=ny)
print("injection cells:", len(pts), "first", pts[0], "last", pts[-1])
assert 3 <= len(pts) <= 6 and len(set(pts)) == len(pts)          # unique cells
assert all(abs(p[0] - (100 + 30)) < 6 for p in pts)               # toe = max(2.5*8+5, 3 cells) = 30 m east
assert all(0 < p[0] < nx * cell and 0 < p[1] < ny * cell for p in pts)
assert sio.injection_points((5.0, 5.0), 270.0, 10, 5, cell, xll, yll, nx, ny) == []  # off-grid
n = sio.manning_from_nlcd(np.array([[11, 24], [82, 99]]), default=0.05)
assert n[0, 1] == 0.15 and n[1, 1] == 0.05
inner = np.zeros((6, 6)); inner[2:4, 2:4] = 1.0
edge = np.zeros((6, 6)); edge[0, 3] = 1.0
assert not sio.edge_flooded(inner) and sio.edge_flooded(edge)

if not (exe and os.path.exists(exe)):
    print("LISFLOOD_EXE not set: skipping solver run"); print("OK"); sys.exit(0)

# ---- real solver run ---------------------------------------------------------
x = (np.arange(nx) + 0.5) * cell; y = (np.arange(ny) + 0.5) * cell
X, Y = np.meshgrid(x, y)
Z = (100 - 0.005 * X + 0.02 * np.abs(Y - 300))[::-1]
t = np.array([0, 600, 1800, 3600.0]); q = np.array([0, 100, 0, 0.0])
vol_in = float(np.trapezoid(q, t))

for solver in ("acc", "fv1"):
    d = tempfile.mkdtemp()
    sio.write_ascii_grid(os.path.join(d, "dem.asc"), Z, xll, yll, cell)
    sio.write_bci(os.path.join(d, "run.bci"), pts)
    sio.write_bdy(os.path.join(d, "run.bdy"), "bc1", t, q, len(pts), cell)
    os.makedirs(os.path.join(d, "out"))
    sio.write_par(os.path.join(d, "run.par"), "dem.asc", "run", "out", 3600, 300, solver)
    r = subprocess.run([exe, "run.par"], cwd=d, capture_output=True, text=True, timeout=600)
    assert r.returncode == 0, r.stdout[-500:]
    res = sio.read_results(os.path.join(d, "out"), "run", solver, 300)
    last = sorted(glob.glob(os.path.join(d, "out", "run-*.wd")))[-1]
    wd, _ = sio.read_ascii_grid(last)
    vol_out = wd.sum() * cell * cell
    dmax, vmax = res["depth"].max(), res["vel"].max()
    wet = res["depth"] > 0.05
    arr = res["arrive_h"]
    print("%s: volume %.0f/%.0f  max depth %.2f  max vel %.2f  wet %d cells"
          % (solver, vol_out, vol_in, dmax, vmax, wet.sum()))
    assert abs(vol_out - vol_in) / vol_in < 0.02          # mass conserved through the writers
    assert 1.0 < dmax < 6.0 and 0.3 < vmax < 8.0
    assert wet.sum() > 100
    # arrival time grows downstream (east) along the valley axis
    row = ny // 2
    a = arr[row, 14:60]; a = a[a > -1]        # downstream of the injection cells (col 13)
    assert len(a) > 10 and np.all(np.diff(a) >= -1e-9) and a[-1] > a[0]
    shutil.rmtree(d)

# ---- edge flag: a flood held inside a basin is False; a valley flood that runs on is True ----
def run_case(Zg, inj, scale):
    d = tempfile.mkdtemp()
    sio.write_ascii_grid(os.path.join(d, "dem.asc"), Zg, xll, yll, cell)
    sio.write_bci(os.path.join(d, "run.bci"), inj)
    sio.write_bdy(os.path.join(d, "run.bdy"), "bc1", t, q * scale, len(inj), cell)
    os.makedirs(os.path.join(d, "out"))
    sio.write_par(os.path.join(d, "run.par"), "dem.asc", "run", "out", 3600, 300, "acc")
    subprocess.run([exe, "run.par"], cwd=d, capture_output=True, timeout=600)
    r = sio.read_results(os.path.join(d, "out"), "run", "acc", 300)
    shutil.rmtree(d)
    return sio.edge_flooded(r["depth"]), float(r["depth"].max())

basin = (100 + 1e-4 * ((X - 500) ** 2 + (Y - 300) ** 2))[::-1]
bpts = sio.injection_points((450.0, 300.0), 90.0, 40.0, 8.0, cell, xll, yll, nx, ny)
in_basin, bdepth = run_case(basin, bpts, 1.0)
in_valley, _ = run_case(Z, pts, 1.0)
print("edge flag: basin %s (max depth %.1f m), valley %s" % (in_basin, bdepth, in_valley))
assert (not in_basin) and in_valley
print("OK")
