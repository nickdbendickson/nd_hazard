"""Chain check: breach_hydro -> hydrograph CSV -> solver_io -> real LISFLOOD-FP.

The volume found in the flooded basin must equal the volume the hydrograph released,
which proves units (m3/s -> per-unit-width) survive the whole hand-off.
"""
import glob, os, shutil, subprocess, sys, tempfile
import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "core"))
import breach_hydro as bh, solver_io as sio  # noqa: E402

exe = os.environ.get("LISFLOOD_EXE", "")
if not (exe and os.path.exists(exe)):
    print("LISFLOOD_EXE not set: skipping"); sys.exit(0)

# reservoir: 20,000 m2 x 10 m = 2e5 m3, breach to the bed
area = 2e4; elev = np.linspace(0, 10, 11)
res = bh.analyze_dam(elev, area * elev, pool=10.0, crest=10.0, base=0.0, bed_elev=0.0)
td = res["td"]; released = td["released_m3"]
print("hydrograph: Qp %.0f m3/s, released %.3e m3, Bb %.1f m" % (td["qp_route"], released, td["bb_m"]))

# write the CSV exactly as the QGIS tool does, then read it back like tool 6 does
d = tempfile.mkdtemp(); csvp = os.path.join(d, "h.csv")
t, q = td["t"], td["q"]
idx = np.unique(np.concatenate((np.linspace(0, len(t) - 1, min(len(t), 600)).astype(int), [int(np.argmax(q))])))
with open(csvp, "w") as f:
    f.write("time_s,discharge_m3s\n")
    for i in idx: f.write(f"{t[i]:.1f},{q[i]:.3f}\n")
ts, qs = sio.read_hydrograph_csv(csvp)
csv_vol = float(np.trapezoid(qs, ts))
print("CSV volume %.3e (%.2f%% of released)" % (csv_vol, 100 * csv_vol / released))
assert abs(csv_vol - released) / released < 0.02          # downsampling loses < 2%

# basin DEM, 10 m cells, big enough to hold the flood
nx, ny, cell = 100, 100, 10.0
x = (np.arange(nx) + .5) * cell; y = (np.arange(ny) + .5) * cell; X, Y = np.meshgrid(x, y)
Z = (100 + 1e-4 * ((X - 500) ** 2 + (Y - 500) ** 2))[::-1]
pts = sio.injection_points((420.0, 500.0), 90.0, td["bb_m"], td["hb_m"], cell, 0, 0, nx, ny)
sio.write_ascii_grid(os.path.join(d, "dem.asc"), Z, 0, 0, cell)
sio.write_bci(os.path.join(d, "run.bci"), pts)
sio.write_bdy(os.path.join(d, "run.bdy"), "bc1", ts, qs, len(pts), cell)
os.makedirs(os.path.join(d, "out"))
sio.write_par(os.path.join(d, "run.par"), "dem.asc", "run", "out", 4 * 3600, 4 * 3600, "acc")
r = subprocess.run([exe, "run.par"], cwd=d, capture_output=True, text=True, timeout=900)
assert r.returncode == 0
wd, _ = sio.read_ascii_grid(sorted(glob.glob(os.path.join(d, "out", "run-*.wd")))[-1])
vol = wd.sum() * cell * cell
out = sio.read_results(os.path.join(d, "out"), "run", "acc", 4 * 3600)
print("basin volume %.3e vs CSV %.3e (%.2f%%); max depth %.2f m, max vel %.2f m/s, injection cells %d"
      % (vol, csv_vol, 100 * (vol - csv_vol) / csv_vol, out["depth"].max(), out["vel"].max(), len(pts)))
assert abs(vol - csv_vol) / csv_vol < 0.02
shutil.rmtree(d); print("OK")
