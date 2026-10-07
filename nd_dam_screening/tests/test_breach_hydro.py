import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "core"))
import breach_hydro as bh  # noqa: E402

# 1. Froehlich (2008) against a hand calculation (Vw = 1e6 m3, hb = 10 m, piping)
p = bh.froehlich_2008(1e6, 10.0, "piping")
hand_b = 0.27 * 1.0 * 1e6 ** 0.32 * 10 ** 0.04
hand_t = 63.2 * np.sqrt(1e6 / (9.81 * 100.0))
print("Bavg %.1f m, tf %.0f s (%.0f min)" % (p["bavg_m"], p["tf_s"], p["tf_s"] / 60))
assert abs(p["bavg_m"] - hand_b) < 1e-9 and abs(p["tf_s"] - hand_t) < 1e-9
assert 20 < p["bavg_m"] < 30 and 20 * 60 < p["tf_s"] < 60 * 60   # plausible magnitudes

# 2. TR-60: 10 m of water = 32.8 ft -> about 41,000 cfs ~ 1,170 m3/s; not stated below 10.5 ft
q = bh.tr60_peak(10.0)
print("TR-60 @10 m: %.0f m3/s" % q)
assert 1100 < q < 1250
assert bh.tr60_peak(3.0) is None

# 3. Prismatic reservoir: 200,000 m2 x 10 m, bed at 0, crest 10, breach to bed
area = 2e5
elev = np.linspace(0, 10, 11)
vol = area * elev
res = bh.analyze_dam(elev[-6:], vol[-6:], pool=5.0, crest=10.0, base=0.0,
                     mode="piping", bed_elev=0.0, below_exp=1.0, sunny=True)
td, sd = res["td"], res["sd"]
for k, r in (("top of dam", td), ("sunny day", sd)):
    print("%-10s Vw %.2e  hb %.1f  Bavg %.1f  tf %.0f min  Qp(route) %.0f  Qp(F95) %.0f  "
          "released %.2e" % (k, r["vw_m3"], r["hb_m"], r["bavg_m"], r["tf_s"] / 60,
                             r["qp_route"], r["qp_f95"], r["released_m3"]))
assert abs(td["vw_m3"] - 2e6) < 1
assert td["released_m3"] <= td["vw_m3"] * 1.0001          # cannot release more than stored
assert td["released_m3"] > 0.9 * td["vw_m3"]              # empties (down to breach invert)
assert 0.3 < td["qp_route"] / td["qp_f95"] < 3.0          # same order as the empirical peak
assert sd["qp_route"] < td["qp_route"]                    # lower start level -> smaller flood
assert abs(sd["vw_m3"] - 1e6) < 1

# 4. Hydrograph starts at zero and returns towards zero
assert td["q"][0] == 0.0 and td["q"][-1] < 0.05 * td["qp_route"]

# 5. Extending below normal pool conserves the pool volume
e, v = bh.prep_curve([5, 6, 7], [3e5, 4e5, 5e5])
e2, v2 = bh.extend_below_pool(e, v, bed_elev=1.0, exponent=2.0)
assert abs(np.interp(5.0, e2, v2) - 3e5) < 1e-6 and v2[0] == 0.0 and e2[0] == 1.0

# 6. Crest-length clamp
res = bh.analyze_dam(elev, vol, 10.0, 10.0, 0.0, crest_len=20.0, bed_elev=0.0)
print("clamped:", res["td"]["bavg_m"], res["td"]["notes"])
assert res["td"]["notes"] and res["td"]["bavg_m"] + res["td"]["z"] * 10 <= 20.0 + 1e-9 or \
    res["td"]["bavg_m"] >= 0.3 * 20.0

# 7. Too-short embankment is skipped, not a crash
res = bh.analyze_dam(elev, vol, 10.0, 10.0, 9.9, bed_elev=0.0)
assert "skipped" in res["td"]
print("OK")

# ---- alternative breach equations (sensitivity) --------------------------
import breach_hydro as _bh
vw, hb = 1.0e6, 10.0
f95 = _bh.froehlich_1995(vw, hb)
assert abs(f95["bavg_m"] - 0.1803 * vw ** 0.32 * hb ** 0.19) < 1e-9
assert abs(f95["tf_s"] / 3600 - 0.00254 * vw ** 0.53 * hb ** -0.9) < 1e-9
v1 = _bh.von_thun_gillette(vw, hb, erodible=True)
v2 = _bh.von_thun_gillette(2.0e6, hb, erodible=False)
assert v1["bavg_m"] == 2.5 * 10 + 6.1 and abs(v1["tf_s"] - 0.15 * 3600) < 1e-6
assert v2["bavg_m"] == 2.5 * 10 + 18.3 and abs(v2["tf_s"] - 0.45 * 3600) < 1e-6
peaks = {}
for m in _bh.BREACH_METHODS:
    r = _bh.analyze_dam(elev[-6:], vol[-6:], pool=5.0, crest=10.0, base=0.0, bed_elev=0.0, method=m)["td"]
    assert r["method"] == m and r["released_m3"] > 0
    peaks[m] = r["qp_route"]
print("peaks by method:", {k: round(p, 1) for k, p in peaks.items()})
assert peaks["VTG-E"] > peaks["VTG-R"]            # faster breach -> higher peak
print("OK (methods)")
