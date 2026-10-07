"""Cross-check: built-in engine vs LISFLOOD-FP (FV1 and ACC) on the same injected breach
flood down a valley, and check that LISFLOOD-FP treats no-data cells as walls (corridor mask).
Needs LISFLOOD_EXE; skipped otherwise.
"""
import glob, os, shutil, subprocess, sys, tempfile, time
import numpy as np
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from core import breach_hydro as bh, solver_io as sio, fv_engine as fe  # noqa: E402

exe = os.environ.get("LISFLOOD_EXE", "")
if not (exe and os.path.exists(exe)):
    print("LISFLOOD_EXE not set: skipping"); sys.exit(0)

area = 1.5e5; elev = np.linspace(0, 8, 9)
td = bh.analyze_dam(elev, area * elev, pool=8.0, crest=8.0, base=0.0, bed_elev=0.0)["td"]
t, q = td["t"], td["q"]
cell, ny, nx = 10.0, 220, 100
yy, xx = np.mgrid[0:ny, 0:nx]
Z = 100.0 - 0.003 * yy * cell + 0.0008 * (np.abs(xx - nx / 2) * cell) ** 1.6
Z[0, :] = Z[-1, :] = sio.NODATA; Z[:, 0] = Z[:, -1] = sio.NODATA      # closed box
xll, yll = 0.0, 0.0
breach_xy = (nx / 2 * cell, (ny - 12) * cell)                            # near the top, flowing south
pts = sio.injection_points(breach_xy, 180.0, td["bb_m"], td["hb_m"], cell, xll, yll, nx, ny)
sim = 3600.0


def lisflood(solver):
    d = tempfile.mkdtemp()
    sio.write_ascii_grid(os.path.join(d, "dem.asc"), Z, xll, yll, cell)
    sio.write_bci(os.path.join(d, "run.bci"), pts)
    sio.write_bdy(os.path.join(d, "run.bdy"), "bc1", t, q, len(pts), cell)
    os.makedirs(os.path.join(d, "out"))
    save = 60.0 if solver == "fv1" else sim
    sio.write_par(os.path.join(d, "run.par"), "dem.asc", "run", "out", sim, save, solver, 0.05)
    t0 = time.time()
    r = subprocess.run([exe, "run.par"], cwd=d, capture_output=True, text=True, timeout=1800)
    assert r.returncode == 0, r.stdout[-500:]
    out = sio.read_results(os.path.join(d, "out"), "run", solver, save)
    wds = sorted(glob.glob(os.path.join(d, "out", "run-*.wd")))
    final, _ = sio.read_ascii_grid(wds[-1])
    shutil.rmtree(d)
    return out, np.where(final > 0, final, 0), time.time() - t0


rows_rc = [(int((yll + ny * cell - y) // cell), int((x - xll) // cell)) for x, y in pts]
e = fe.Engine(Z, cell, manning=0.05)
e.add_source(rows_rc, t, q)
t0 = time.time(); rb = e.run(sim); tb = time.time() - t0
res = {"built-in": (rb, rb["final"], tb)}
for s in ("fv1", "acc"):
    res[s] = lisflood(s)

tt = np.linspace(0, sim, 20001)
inj = float(np.trapezoid(np.interp(tt, t, q, right=0.0), tt))          # volume injected within the run
sections = [60, 110, 160]
col = nx // 2
print("injected within %.0f s: %.3e m3" % (sim, inj))
for k, (o, fin, secs) in res.items():
    vol = fin.sum() * cell * cell
    print("%-9s run %5.1f s  stored %.3e m3 (%+.1f%%)  max depth at sections %s  wet cells %d  outside-box %s"
          % (k, secs, vol, 100 * (vol - inj) / inj,
             [round(float(o["depth"][r, col]), 2) for r in sections], int((o["depth"] > 0.05).sum()),
             bool((o["depth"][0, :] > 0).any() or (o["depth"][:, 0] > 0).any())))


def fstat(a, b):
    A, B = a > 0.05, b > 0.05
    return (A & B).sum() / (A | B).sum()


bi, fv, ac = res["built-in"][0]["depth"], res["fv1"][0]["depth"], res["acc"][0]["depth"]
f1, f2 = fstat(bi, fv), fstat(bi, ac)
dd = [abs(bi[r, col] - fv[r, col]) / fv[r, col] for r in sections]
print("flood-extent agreement (F) built-in vs FV1 %.2f, vs ACC %.2f; section depth diff vs FV1 %s"
      % (f1, f2, [f"{100 * x:.0f}%" for x in dd]))
assert f1 > 0.85 and f2 > 0.8 and max(dd) < 0.2
for k, (o, fin, _) in res.items():            # nodata walls hold the water (mass stays in the box)
    assert abs(fin.sum() * cell * cell - inj) / inj < 0.03, k
print("OK")
