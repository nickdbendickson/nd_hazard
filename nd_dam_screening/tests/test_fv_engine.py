"""Built-in shallow-water engine: analytical and conservation checks."""
import os, sys, math, time
import numpy as np
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from core import fv_engine as fe, breach_hydro as bh  # noqa: E402

ND = fe.NODATA

# 1. lake at rest over a bumpy bed with dry islands: must stay at rest
rng = np.random.default_rng(1)
z = rng.uniform(0, 3, (40, 40)); z[15:20, 15:20] = 12.0
e = fe.Engine(z, 5.0, manning=0.03)
e.set_reservoir(np.ones(z.shape, bool), 10.0)
e.h[0, :] = e.h[-1, :] = 0; e.h[:, 0] = e.h[:, -1] = 0     # keep water off the open edge
e.wall[0, :] = e.wall[-1, :] = e.wall[:, 0] = e.wall[:, -1] = True
e.h[e.wall] = 0
r = e.run(60.0)
print("lake at rest: max |q| %.2e, mass err %.2e %%" % (np.hypot(e.qx, e.qy).max(), r["mass"]["error_pct"]))
assert np.hypot(e.qx, e.qy).max() < 1e-8 and abs(r["mass"]["error_pct"]) < 1e-9

# 2. Ritter dry-bed dam break (1D strip between walls), frictionless
nx, dx, h0, t = 400, 5.0, 10.0, 40.0
z = np.zeros((5, nx)); z[0, :] = z[-1, :] = ND
e = fe.Engine(z, dx, manning=0.0)
m = np.zeros(z.shape, bool); m[:, :200] = True
e.set_reservoir(m, h0)
r = e.run(t)
x = (np.arange(nx) + 0.5) * dx - 1000.0
c0 = math.sqrt(9.81 * h0)
ritter = np.where(x < -c0 * t, h0, np.where(x > 2 * c0 * t, 0.0,
                  4 / (9 * 9.81) * (c0 - x / (2 * t)) ** 2))
hnum = e.h[2]
mid = (x > -0.7 * c0 * t) & (x < 1.2 * c0 * t)
err = np.abs(hnum[mid] - ritter[mid]).max() / h0
front = x[np.where(hnum > 0.1)[0][-1]]                    # where the flood is 0.1 m deep
exact_front = 2 * t * (c0 - math.sqrt(0.1 * 9 * 9.81 / 4))
print("Ritter: max err %.1f%% of h0 in the rarefaction, h(dam) %.2f (exact %.2f), 0.1 m front %.0f m (exact %.0f), mass %.2e%%"
      % (100 * err, hnum[199:201].mean(), 4 / 9 * h0, front, exact_front, r["mass"]["error_pct"]))
assert err < 0.06 and abs(hnum[199:201].mean() - 4 / 9 * h0) < 0.3
assert 0.9 * exact_front < front < 1.1 * exact_front
assert abs(r["mass"]["error_pct"]) < 1e-6

# 3. point-source inflow into a closed basin: all injected water accounted for
z = np.fromfunction(lambda i, j: 0.01 * ((i - 30) ** 2 + (j - 30) ** 2), (61, 61))
z[0, :] = z[-1, :] = z[:, 0] = z[:, -1] = ND
e = fe.Engine(z, 10.0, manning=0.05)
e.add_source([(30, 30), (30, 31)], [0, 600, 1200, 1201], [0, 50, 0, 0])
r = e.run(1800.0)
print("closed basin: injected %.0f m3, stored %.0f m3, mass err %.2e%%"
      % (r["mass"]["injected_m3"], r["mass"]["final_m3"], r["mass"]["error_pct"]))
assert abs(r["mass"]["injected_m3"] - 50 * 600) < 50 * 2
assert abs(r["mass"]["error_pct"]) < 1e-6

# 4. reservoir-in-grid breach vs the level-pool hydrograph (same breach parameters)
cell = 10.0
ny, nx = 120, 90
yy, xx = np.mgrid[0:ny, 0:nx]
zv = 100.0 - 0.004 * yy * cell + 0.002 * (np.abs(xx - nx / 2) * cell) ** 1.5
dam_row = 50
crest, invert = zv[dam_row, nx // 2] + 8.0, zv[dam_row, nx // 2]
zd = zv.copy(); zd[dam_row - 1:dam_row + 2, :] = np.maximum(zv[dam_row - 1:dam_row + 2, :], crest)
zd[0, :] = ND                                     # wall off the upstream edge (closed valley head)
t0 = time.time()
res_mask = (yy < dam_row - 1) & (zd < crest) & (zd > ND + 1)
pool = crest                                      # ND standard: water at the top of dam
b = bh.froehlich_2008(float(((pool - zd) * res_mask).clip(min=0).sum() * cell ** 2), crest - invert)
bw = max(int(round(b["bavg_m"] / cell)), 1)
breach = np.zeros(zd.shape, bool)
breach[dam_row - 1:dam_row + 2, nx // 2 - bw // 2: nx // 2 - bw // 2 + bw] = True
eng = fe.Engine(zd, cell, manning=0.04)
eng.set_reservoir(res_mask, pool)
eng.set_breach(breach, invert, 0.0, b["tf_s"])
# run in chunks to sample discharge across row dam_row+3
eng2 = fe.Engine(zd, cell, manning=0.04)
eng2.set_reservoir(res_mask, pool)
eng2.set_breach(breach, invert, 0.0, b["tf_s"])
t_chunks = np.arange(20.0, 4 * b["tf_s"], 20.0)
vol_prev = float((eng2.h * res_mask).sum()) * cell ** 2
qs = []
for tc in t_chunks:
    eng2.run(tc)
    vol = float((eng2.h[:dam_row - 1]).sum()) * cell ** 2
    qs.append((vol_prev - vol) / 20.0); vol_prev = vol
qp_2d = max(qs)
# level-pool routing with a curve measured from the same terrain
elev = np.linspace(invert, crest, 30)
vols = [float(((e_ - zd) * res_mask).clip(min=0).sum() * cell ** 2) for e_ in elev]
rt = bh.route_breach(elev, np.array(vols), pool, invert, b["bavg_m"], b["tf_s"], b["z"])
m2 = eng2.mass
print("reservoir breach: 2D outflow peak %.0f m3/s vs level-pool %.0f m3/s (tf %.0f min), mass err %.1e%%, %.1f s"
      % (qp_2d, rt["peak_m3s"], b["tf_s"] / 60, m2["error_pct"], time.time() - t0))
assert 0.6 < qp_2d / rt["peak_m3s"] < 1.4
assert abs(m2["error_pct"]) < 1e-6
assert eng2.hmax[dam_row + 20, nx // 2] > 0.3 and eng2.dvmax.max() > 0
print("OK")


# 5. active window vs whole grid: identical results (v0.11)
def _same(label, a, b, tol=1e-12):
    worst = 0.0
    for k in ("depth", "vel", "dv", "arrive_h", "final"):
        d = float(np.abs(a[k] - b[k]).max())
        worst = max(worst, d)
    for k in ("initial_m3", "injected_m3", "final_m3", "left_domain_m3"):
        assert abs(a["mass"][k] - b["mass"][k]) <= 1e-9 * max(1.0, abs(b["mass"][k])), (label, k)
    print("%-38s max |diff| %.1e (%s), steps %d/%d" % (label, worst, "bitwise" if worst == 0 else "within tol",
                                                     a["steps"], b["steps"]))
    assert worst <= tol and a["steps"] == b["steps"], label


def _pair(build, t_end, **kw):
    ew, ef = build(window=True, **kw), build(window=False, **kw)
    t0 = time.time(); rw = ew.run(t_end); tw = time.time() - t0
    t0 = time.time(); rf = ef.run(t_end); tf = time.time() - t0
    return ew, ef, rw, rf, tw, tf


# 5a. open plane: the flood grows to the whole domain and leaves through the terrain edge
def plane(**kw):
    zz = np.fromfunction(lambda i, j: 0.004 * j * 8.0 + 0.001 * i, (50, 70))
    e = fe.Engine(zz, 8.0, manning=0.04, **kw)
    e.add_source([(25, 60), (26, 60)], [0, 300, 900, 901], [0, 60, 0, 0])
    return e
ew, ef, rw, rf, tw, tf = _pair(plane, 1500.0)
_same("window vs full: open plane, edge reached", rw, rf)
assert rw["mass"]["left_domain_m3"] > 0.1 * rw["mass"]["injected_m3"]        # water really left
assert (rw["depth"][:, 0] > 0.05).any()                                         # and touched the edge
assert ew.window_rebuilds > 1 and ew.window_cells < ef.window_cells            # the window did work
print("   window %.1f s, full grid %.1f s (%.1fx); window cells %.0f%% of full" %
      (tw, tf, tf / max(tw, 1e-9), 100.0 * ew.window_cells / ef.window_cells))

# 5b. closed basin filled from a corner until the flood covers the whole domain
def basin(**kw):
    zz = np.fromfunction(lambda i, j: 0.002 * ((i - 30) ** 2 + (j - 40) ** 2), (61, 81))
    zz[0, :] = zz[-1, :] = zz[:, 0] = zz[:, -1] = ND
    e = fe.Engine(zz, 10.0, manning=0.05, **kw)
    e.add_source([(5, 5)], [0, 100, 2400, 2401], [0, 400, 400, 0])
    return e
ew, ef, rw, rf, tw, tf = _pair(basin, 3000.0)
_same("window vs full: basin, whole domain wet", rw, rf)
inner = rw["depth"][1:-1, 1:-1]
assert (inner > 0.05).mean() > 0.95                                             # flood covers the domain
print("   window %.1f s, full grid %.1f s (%.1fx)" % (tw, tf, tf / max(tw, 1e-9)))

# 5c. reservoir in the grid with a gradual breach (walls, breach lowering, continued runs)
def resv(**kw):
    e = fe.Engine(zd, cell, manning=0.04, **kw)
    e.set_reservoir(res_mask, pool)
    e.set_breach(breach, invert, 0.0, b["tf_s"])
    return e
ew, ef = resv(window=True), resv(window=False)
t0 = time.time(); ew.run(300.0); rw = ew.run(900.0); tw = time.time() - t0
t0 = time.time(); ef.run(300.0); rf = ef.run(900.0); tf = time.time() - t0
_same("window vs full: reservoir breach, 2 runs", rw, rf)
print("   window %.1f s, full grid %.1f s (%.1fx)" % (tw, tf, tf / max(tw, 1e-9)))

# 5d. a receding flood: the window must shrink again (every window_every steps) and stay identical
def pulse(**kw):
    zz = np.fromfunction(lambda i, j: 0.01 * j * 5.0, (30, 120))
    zz[0, :] = zz[-1, :] = ND
    e = fe.Engine(zz, 5.0, manning=0.03, window_every=50, **kw)
    e.add_source([(15, 110)], [0, 60, 120, 121], [0, 30, 0, 0])
    return e
ew, ef, rw, rf, tw, tf = _pair(pulse, 600.0)
_same("window vs full: pulse down a slope", rw, rf)
print("OK (window)")
