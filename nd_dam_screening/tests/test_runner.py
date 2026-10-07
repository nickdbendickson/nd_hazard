"""Per-dam driver on a synthetic valley: corridor, clip growth, run extension, cascade,
reservoir-in-grid, parallel batch.  LISFLOOD_EXE optional (adds an ACC run)."""
import os, sys, time
import numpy as np
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from core import runner as rn, breach_hydro as bh, gridutil as gu  # noqa: E402

# ---- a 8 km x 2.5 km valley draining south, 5 m base grid, EPSG:5070-like coordinates
base = 5.0
NY, NX = 1600, 500
X0, YTOP = 500000.0, 2000000.0
yy, xx = np.mgrid[0:NY, 0:NX]
xm = (xx - NX / 2) * base
Z = 400.0 - 0.0025 * yy * base + 0.0006 * np.abs(xm) ** 1.55
# upper dam at row 200 (crest 8 m above the valley floor), lower dam at row 700 (3 m high)
def embank(Z, row, h):
    zc = Z[row, NX // 2] + h
    Z[row - 2:row + 3, :] = np.maximum(Z[row - 2:row + 3, :], zc)
    return zc
Zd = Z.copy()
crest1 = embank(Zd, 200, 8.0)
crest2 = embank(Zd, 700, 3.0)
# hydro-flattened normal pool behind the upper dam, 2 m below crest
pool1 = crest1 - 2.0
flat = (yy < 198) & (Zd < pool1)
Zd = np.where(flat, pool1, Zd)


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
    m = np.where((b <= gu.NODATA + 1).any(axis=(1, 3)), gu.NODATA, b.mean(axis=(1, 3)))
    gt = (X0 + c0 * base, cell, 0.0, YTOP - r0 * base, 0.0, -cell)
    return m, gt


def xy(r, c):
    return X0 + (c + 0.5) * base, YTOP - (r + 0.5) * base


# upper dam storage: measured from the true terrain (below-pool part known)
res_cells = (yy < 198) & (Z < crest1)
vw = float(((crest1 - Z) * res_cells).clip(min=0).sum() * base * base)
v_below = float(((pool1 - Z) * ((yy < 198) & (Z < pool1))).clip(min=0).sum() * base * base)
hb = crest1 - Z[200, NX // 2]
elev = np.linspace(Z[200, NX // 2], crest1, 25)
vols = [float(((e - Z) * res_cells).clip(min=0).sum() * base * base) for e in elev]
td = bh.analyze_dam(elev, vols, pool=pool1, crest=crest1, base=float(Z[200, NX // 2]),
                    bed_elev=float(Z[200, NX // 2]))["td"]
line1 = [xy(200, 0), xy(200, NX - 1)]
line2 = [xy(700, 0), xy(700, NX - 1)]
case = {"dam_id": "DWR-1", "breach_xy": xy(200, NX // 2), "flow_az": 180.0,
        "t": td["t"], "q": td["q"], "hb_m": td["hb_m"], "bb_m": td["bb_m"], "bavg_m": td["bavg_m"],
        "vw_m3": td["vw_m3"], "tf_s": td["tf_s"], "start_elev": crest1,
        "invert": float(Z[200, NX // 2]), "dam_line_xy": line1, "pool_elev": pool1,
        "v_below_m3": v_below,
        "others": [{"dam_id": "DWR-2", "line_xy": line2, "breach_xy": xy(700, NX // 2),
                    "flow_az": 180.0, "hb_m": 3.0, "bb_m": 8.0,
                    "t": np.array([0, 600, 1800, 1801.0]), "q": np.array([0, 40, 0, 0.0])}]}
print("upper dam: Vw %.2e m3 (terrain %.2e), hb %.1f m, Qp %.0f m3/s, tf %.0f min"
      % (td["vw_m3"], vw, hb, td["qp_route"], td["tf_s"] / 60))

opts = dict(engine="builtin", cell=20.0, radius_m=1200.0, radius_max_m=3000.0,
            corridor_hw_m=150.0, timeout_s=1800, cascade="flag")
logs = []
t0 = time.time()
r = rn.simulate(case, load_dem, opts=opts, log=logs.append)
print("built-in: %s in %.0f s, attempts %d, radius %.0f, corridor %s, sim %.1f h, cell %.0f"
      % (r["status"], time.time() - t0, r["attempts"], r["radius_m"], r["corridor_hw_m"],
         r["sim_s"] / 3600, r["cell_m"]))
for m in logs: print("   ", m)
print("   cascade:", r["cascade"], "| notes:", r["notes"], "| mass err %.1e%%" % r["mass"]["error_pct"])
assert r["status"] == "ok"
# 0.20.0: the enlargement on the downstream trace is opt-in (trace_grow); by default the clip
# grows after a run whose flood reaches the terrain edge, as before 0.15
assert any("corridor" in m for m in logs) and not any("trace leaves the clip" in m for m in logs)
assert any("flood reached the terrain edge; enlarging clip" in m for m in logs)
assert not any("clip enlarged to" in n for n in r["notes"]) and r["radius_m"] == 3000.0
# 0.15 (opt-in from 0.20.0): with trace_grow the clip is enlarged on the downstream trace before
# the first run (the flood no longer has to reach the terrain edge first); it is in the notes
logs_g = []
rg = rn.simulate(case, load_dem, opts=dict(opts, trace_grow=True), log=logs_g.append)
print("built-in, trace_grow: %s, attempts %d, radius %.0f" % (rg["status"], rg["attempts"],
                                                              rg["radius_m"]))
assert rg["status"] == "ok" and any("trace leaves the clip" in m for m in logs_g)
assert any("clip enlarged to 9,843 ft" in n for n in rg["notes"]) and rg["radius_m"] == 3000.0
assert rg["attempts"] <= r["attempts"]
assert r["cascade"] and r["cascade"][0]["dam_id"] == "DWR-2" and not r["cascade"][0]["breached"]
assert abs(r["mass"]["error_pct"]) < 1e-6
flag_depth = r["depth"].copy(); gt_flag = r["gt"]

# cascade = breach: the lower dam's hydrograph is added when the flood reaches it
r2 = rn.simulate(case, load_dem, opts=dict(opts, cascade="breach"), log=logs.append)
c2 = r2["cascade"][0]
print("cascade breach: %s, arrival %.2f h, over crest %.2f m, attempts %d"
      % (r2["status"], c2["arrival_h"], c2["depth_over_crest_m"], r2["attempts"]))
assert r2["status"] == "ok" and c2["breached"]
assert r2["mass"]["injected_m3"] > r["mass"]["injected_m3"] + 0.9 * 40 * 900

# reservoir in the grid: bed estimated under the flat pool, gradual breach
r3 = rn.simulate(case, load_dem, opts=dict(opts, engine="builtin-reservoir", cell=40.0), log=logs.append)
print("reservoir-in-grid: %s, in-grid %s, notes %s, mass err %.1e%%"
      % (r3["status"], r3["reservoir_in_grid"], r3["notes"], r3["mass"]["error_pct"]))
assert r3["status"] == "ok" and r3["reservoir_in_grid"]
assert any("bed estimated" in n for n in r3["notes"])
rw = int((YTOP - xy(450, 0)[1] - (YTOP - r3["gt"][3])) / r3["gt"][1])
assert r3["depth"].max() > 0.5

# LISFLOOD-FP ACC through the same driver (if available)
exe = os.environ.get("LISFLOOD_EXE")
if exe and os.path.exists(exe):
    r4 = rn.simulate(case, load_dem, opts=dict(opts, engine="lisflood-acc", exe=exe,
                                               workdir=None), log=logs.append)
    print("lisflood-acc: %s, attempts %d, radius %.0f, sim %.1f h, notes %s"
          % (r4["status"], r4["attempts"], r4["radius_m"], r4["sim_s"] / 3600, r4["notes"]))
    assert r4["status"] == "ok" and r4["cascade"]

# parallel batch: two dams, two workers, results for both
cases = [case, dict(case, dam_id="DWR-1b")]
t0 = time.time()
out = rn.run_batch(cases, lambda c: rn.simulate(c, load_dem, opts=dict(opts, radius_m=1200,
                                                                        corridor=False)), workers=2)
print("batch of 2 with 2 workers: %.0f s, statuses %s" % (time.time() - t0, [o["status"] for o in out]))
assert sorted(o["dam_id"] for o in out) == ["DWR-1", "DWR-1b"] and all(o["status"] == "ok" for o in out)

# failure is reported, not raised
bad = rn.simulate(dict(case, breach_xy=(0.0, 0.0)), load_dem, opts=opts)
assert bad["status"] == "FAILED" and bad["notes"]
print("OK")
