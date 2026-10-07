"""v0.11 runner options on a small synthetic valley (built-in engine): no-breach baseline and
incremental depth (BASELINE / BASELINE_CFS), buildings as obstructions (raised DEM / Manning n),
and gridutil.rasterize_polygons.  Pure numpy; no QGIS."""
import os, sys, time
import numpy as np
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from core import runner as rn, breach_hydro as bh, gridutil as gu  # noqa: E402

# ---- rasterize_polygons -------------------------------------------------------------------
gt = (1000.0, 10.0, 0.0, 2000.0, 0.0, -10.0)
shape = (20, 30)
sq = [(1020, 1980), (1060, 1980), (1060, 1940), (1020, 1940)]            # 4 x 4 cells, cols 2-5, rows 2-5
m = gu.rasterize_polygons([sq], gt, shape)
assert m.sum() == 16 and m[2:6, 2:6].all() and not m[1, :].any() and not m[6, :].any(), m.sum()
m = gu.rasterize_polygons([sq[::-1]], gt, shape)                          # orientation does not matter
assert m.sum() == 16
tri = [(1100, 1900), (1200, 1900), (1100, 1800)]                           # right triangle, 10 x 10 cells
m = gu.rasterize_polygons([tri], gt, shape)
assert 40 <= m.sum() <= 55 and m[11, 11] and not m[18, 18], m.sum()      # about half of 100 cells
tiny = [(1031, 1979), (1034, 1979), (1034, 1976)]                         # smaller than a cell, no centre inside
assert gu.rasterize_polygons([tiny], gt, shape).sum() == 0
L = [(1000, 2000), (1050, 2000), (1050, 1990), (1010, 1990), (1010, 1950), (1000, 1950)]   # concave L
m = gu.rasterize_polygons([L], gt, shape)
assert m[0, :5].all() and m[1:5, 0].all() and not m[1:5, 1:5].any(), m[:6, :6].astype(int)
out = [(1250, 2050), (1350, 2050), (1350, 1950), (1250, 1950)]           # partly outside the grid
m = gu.rasterize_polygons([out], gt, shape)
assert m.sum() == 5 * 5 and m[:5, 25:30].all()
assert gu.rasterize_polygons([], gt, shape).sum() == 0 and gu.rasterize_polygons([[(0, 0), (1, 1)]], gt, shape).sum() == 0
print("rasterize_polygons OK")

# ---- a 3 km x 1.4 km valley draining south, 10 m base grid ----------------------------------
base = 10.0
NY, NX = 300, 140
X0, YTOP = 500000.0, 2000000.0
yy, xx = np.mgrid[0:NY, 0:NX]
xm = (xx - NX / 2) * base
Z = 400.0 - 0.003 * yy * base + 0.0012 * np.abs(xm) ** 1.5
Zd = Z.copy()
row = 80
crest = Z[row, NX // 2] + 6.0
Zd[row - 1:row + 2, :] = np.maximum(Zd[row - 1:row + 2, :], crest)


def load_dem(center, half, cell):
    k = int(round(cell / base))
    c = int((center[0] - X0) / base); r = int((YTOP - center[1]) / base)
    h = int(half / base)
    r0, r1, c0, c1 = r - h, r + h, c - h, c + h
    out = np.full((r1 - r0, c1 - c0), gu.NODATA)
    rr0, rr1, cc0, cc1 = max(r0, 0), min(r1, NY), max(c0, 0), min(c1, NX)
    out[rr0 - r0:rr1 - r0, cc0 - c0:cc1 - c0] = Zd[rr0:rr1, cc0:cc1]
    ny, nx = out.shape[0] // k * k, out.shape[1] // k * k
    b = out[:ny, :nx].reshape(ny // k, k, nx // k, k)
    mm = np.where((b <= gu.NODATA + 1).any(axis=(1, 3)), gu.NODATA, b.mean(axis=(1, 3)))
    return mm, (X0 + c0 * base, cell, 0.0, YTOP - r0 * base, 0.0, -cell)


def xy(r, c):
    return X0 + (c + 0.5) * base, YTOP - (r + 0.5) * base


res_cells = (yy < row - 1) & (Z < crest)
elev = np.linspace(Z[row, NX // 2], crest, 20)
vols = [float(((e - Z) * res_cells).clip(min=0).sum() * base * base) for e in elev]
td = bh.analyze_dam(elev, vols, pool=crest, crest=crest, base=float(Z[row, NX // 2]),
                    bed_elev=float(Z[row, NX // 2]))["td"]
case = {"dam_id": "DWR-9", "breach_xy": xy(row, NX // 2), "flow_az": 180.0, "t": td["t"], "q": td["q"],
        "hb_m": td["hb_m"], "bb_m": td["bb_m"], "bavg_m": td["bavg_m"], "vw_m3": td["vw_m3"],
        "tf_s": td["tf_s"], "start_elev": crest, "invert": float(Z[row, NX // 2])}
opts = dict(engine="builtin", cell=20.0, radius_m=1000.0, radius_max_m=1000.0, corridor=False,
            timeout_s=900, cascade="off", sim_s=2400.0)
print("dam: Qp %.0f m3/s, tf %.0f min" % (td["qp_route"], td["tf_s"] / 60))

t0 = time.time()
r0 = rn.simulate(case, load_dem, opts=opts)
print("plain: %s in %.0f s, max depth %.2f m, notes %s" % (r0["status"], time.time() - t0, r0["depth"].max(), r0["notes"]))
assert r0["status"] == "ok" and not r0["base_run"] and r0["buildings"]["mode"] == "none"
assert "base_depth" not in r0 and not any("baseline" in n for n in r0["notes"])

# ---- baseline dry: increment == breach depth -------------------------------------------------
r1 = rn.simulate(case, load_dem, opts=dict(opts, baseline=True, baseline_cfs=0.0))
assert r1["status"] == "ok" and r1["base_run"] and r1["baseline_cfs"] == 0.0
assert np.array_equal(r1["depth"], r0["depth"])                     # the breach run itself is unchanged
assert not r1["base_depth"].any() and not r1["base_vel"].any()
assert np.array_equal(r1["incr_depth"], r1["depth"])
assert any("dry (0 cfs)" in n for n in r1["notes"]) and any("screening approximation" in n for n in r1["notes"])
print("baseline dry: increment == breach depth; note:", [n for n in r1["notes"] if "baseline" in n][0][:60], "...")

# ---- baseline with an ambient flow: increment < breach where the baseline is wet ----------------
cfs = 400.0
t0 = time.time()
logs = []
r2 = rn.simulate(case, load_dem, opts=dict(opts, baseline=True, baseline_cfs=cfs), log=logs.append)
print("baseline %g cfs: %s in %.0f s, base max %.2f m, breach max %.2f m, incr max %.2f m, base run %.1f h" %
      (cfs, r2["status"], time.time() - t0, r2["base_depth"].max(), r2["depth"].max(), r2["incr_depth"].max(),
       r2["base_sim_s"] / 3600))
for m_ in logs: print("   ", m_)
assert r2["status"] == "ok" and r2["base_run"] and r2["baseline_cfs"] == cfs
assert np.array_equal(r2["depth"], r0["depth"])
bd, dd, inc = r2["base_depth"], r2["depth"], r2["incr_depth"]
assert bd.max() > 0.2 and (bd > 0.05).sum() > 50                     # a real channel flow
assert (inc <= dd + 1e-12).all() and (inc >= 0).all()
assert inc.max() < dd.max()                                          # the deepest cell is on the baseline channel
wetb = bd > 0.05
assert (inc[wetb] < dd[wetb]).all()
assert np.allclose(inc[~wetb], dd[~wetb] - bd[~wetb])
assert (bd > 0.05).sum() < (dd > 0.05).sum()                         # the breach floods more than the ambient flow
# fewer cells pass a depth threshold with the increment than with the breach depth (what tool 8 counts)
thr = float(np.median(dd[wetb]))                                     # a depth the breach reaches on the channel
assert 0 < (inc > thr).sum() < (dd > thr).sum(), ((inc > thr).sum(), (dd > thr).sum())
assert any(f"{cfs:g} cfs" in n for n in r2["notes"])
# a steady source needs the engine's steady-state reach: the baseline is extended while still spreading
assert r2["base_sim_s"] >= opts["sim_s"]

# ---- buildings as obstructions -------------------------------------------------------------
# footprints in the flood path (20 m cells): one 60 x 40 m block right on the channel, one off the flood
gt20 = r0["gt"]
rc_wet = np.argwhere(r0["depth"] > 1.0)
rr, cc = rc_wet[len(rc_wet) // 2]                                    # a deep cell well below the dam
x, y = gu.rc_to_xy(gt20, rr, cc)
blk = [(x - 30, y + 20), (x + 30, y + 20), (x + 30, y - 20), (x - 30, y - 20)]
far = [(gt20[0] + 50, gt20[3] - 50), (gt20[0] + 90, gt20[3] - 50), (gt20[0] + 90, gt20[3] - 90), (gt20[0] + 50, gt20[3] - 90)]
polys = [blk, far]
r3 = rn.simulate(case, load_dem, opts=dict(opts, buildings="raise", bld_polys=polys))
b3 = r3["buildings"]
print("buildings raise: %s, %s" % (r3["status"], b3["note"]))
assert r3["status"] == "ok" and b3["mode"] == "raise" and 4 <= b3["cells"] <= 12 and b3["footprints"] == 2
assert "10 ft" in b3["note"] and any("DEM raised" in n for n in r3["notes"])
bm = gu.rasterize_polygons(polys, gt20, r0["depth"].shape)
assert r3["depth"][bm].max() < r0["depth"][bm].max() - 0.5           # the block stands above the flood
assert abs(r3["depth"].max() - r0["depth"].max()) < 1.0              # elsewhere the flood is similar
r4 = rn.simulate(case, load_dem, opts=dict(opts, buildings="manning", bld_polys=polys, building_n=0.2))
b4 = r4["buildings"]
print("buildings manning: %s, %s" % (r4["status"], b4["note"]))
assert r4["status"] == "ok" and b4["mode"] == "manning" and b4["cells"] == b3["cells"]
assert "0.200" in b4["note"] and any("Manning n" in n for n in r4["notes"])
assert r4["depth"][bm].max() > 0.5                                   # water passes through rough cells
assert not np.array_equal(r4["depth"], r0["depth"])
# a different raise height (user value in ft converted by the tool) is reported
r5 = rn.simulate(case, load_dem, opts=dict(opts, buildings="raise", bld_polys=polys, building_raise_m=5 * rn.FT))
assert "5 ft" in r5["buildings"]["note"]
# no polygons: bare terrain, said so
r6 = rn.simulate(case, load_dem, opts=dict(opts, buildings="raise", bld_polys=[]))
assert np.array_equal(r6["depth"], r0["depth"]) and any("no footprint polygons" in n for n in r6["notes"])
# unknown mode is a reported failure, not a crash
r7 = rn.simulate(case, load_dem, opts=dict(opts, buildings="float", bld_polys=polys))
assert r7["status"] == "FAILED" and any("buildings mode" in n for n in r7["notes"])
print("OK")
