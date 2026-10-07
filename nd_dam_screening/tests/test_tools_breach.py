"""0.11: breach equations copied from the ND Dam Breach Runner (MacDonald & Langridge-Monopolis
1984, NRCS TR-60) and user-specified breach parameters in tool 5.

The numeric functions are compared with the Runner's own module when the Runner is on the
machine (/home/claude/suite/nd_breach_runner or next to this plugin); otherwise that part is
skipped with a message. The tool itself runs against the QGIS test doubles in tests/qgis_mock.
"""
import os, sys, tempfile, shutil
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "qgis_mock"))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
from qgis.core import (QgsGeometry, QgsPointXY, QgsProcessingContext, QgsProcessingFeedback,  # noqa: E402
                       make_layer)
from qgis.PyQt.QtCore import QVariant  # noqa: E402
from nd_dam_screening.core import breach_hydro as bh  # noqa: E402
from nd_dam_screening.algorithms.breach_hydrograph import BreachHydrograph, PARAM_SOURCES  # noqa: E402
from nd_dam_screening.algorithms.sensitivity import VARIANTS  # noqa: E402

D, S = "double", "string"

# ---------------------------------------------------------------- 1. same numbers as the Runner
runner = None
for cand in ("/home/claude/suite", os.path.dirname(os.path.dirname(HERE))):
    if os.path.exists(os.path.join(cand, "nd_breach_runner", "core", "breach.py")):
        if cand not in sys.path:
            sys.path.insert(0, cand)
        try:
            from nd_breach_runner.core import breach as runner  # noqa: E402
        except Exception as e:  # noqa: BLE001
            print("Runner import failed:", e)
        break
if runner is None:
    print("SKIP: the ND Dam Breach Runner is not on this machine; equality with its functions not checked")
else:
    for vw, hw, a in ((1.0e6, 10.0, 500.0), (2.5e5, 4.0, 120.0), (3.0e7, 25.0, 2000.0)):
        assert bh.mlm_1984(vw, hw, a) == runner.mlm_1984(vw, hw, a)
    for h, c, u, d in ((25.0, 12.0, 3.0, 3.0), (8.0, 10.0, 2.5, 2.0)):
        assert bh.embankment_area_ft2(h, c, u, d) == runner.embankment_area_ft2(h, c, u, d)
    for args in ((1000.0, 30.0, 2000.0), (50.0, 12.0, 400.0), (20000.0, 60.0, 5000.0),
                 (1000.0, 30.0, 2000.0, 100.0), (1000.0, 110.0, None), (1000.0, 30.0, None)):
        assert bh.tr60_peak_cfs(*args) == runner.tr60_peak_cfs(*args), args
    a1 = bh.curvilinear_shape(500.0, 600.0, 2.0e6); a2 = runner.curvilinear_shape(500.0, 600.0, 2.0e6)
    assert np.array_equal(a1[0], a2[0]) and np.array_equal(a1[1], a2[1])
    assert bh.curvilinear_shape(500.0, 600.0, 1.0e5) is None and runner.curvilinear_shape(500.0, 600.0, 1.0e5) is None
    # Froehlich: the same formulas; Screening uses g = 9.81, the Runner 9.80665 (tf within 0.02%)
    for vw, hb in ((1.0e6, 10.0), (4.0e5, 6.0)):
        for mode in ("piping", "overtopping"):
            b, tf, z = runner.froehlich_2008(vw, hb, mode); p = bh.froehlich_2008(vw, hb, mode)
            assert abs(p["bavg_m"] - b) < 1e-9 and abs(p["tf_s"] / tf - 1) < 2e-4 and p["z"] == z
            b, tf, z = runner.froehlich_1995(vw, hb, mode); p = bh.froehlich_1995(vw, hb, mode)
            assert abs(p["bavg_m"] - b) < 1e-9 and abs(p["tf_s"] - tf) < 1e-6 and p["z"] == z
    # the labels of the two new methods are the Runner's
    assert runner.METHOD_LABELS[runner.METHOD_MLM].startswith("MacDonald & Langridge-Monopolis (1984)")
    assert "TR-60" in runner.METHOD_LABELS[runner.METHOD_TR60] and "triangular" in runner.METHOD_LABELS[runner.METHOD_TR60]
    print("OK (MLM, TR-60, curvilinear shape and Froehlich agree with the Runner's breach.py)")

# ---------------------------------------------------------------- 2. the method table
assert list(bh.BREACH_METHODS)[:4] == ["F2008", "F1995", "VTG-E", "VTG-R"]      # old indices unchanged
assert list(bh.BREACH_METHODS)[4:] == ["MLM", "TR60", "TR60-C"]
assert BreachHydrograph.METHODS == list(bh.BREACH_METHODS)
assert [v[2] for v in VARIANTS[:6]] == ["base: Froehlich 2008, n x1", "Froehlich 1995",
                                         "Von Thun & Gillette, easily erodible",
                                         "Von Thun & Gillette, erosion resistant", "Manning n x0.75",
                                         "Manning n x1.25"]
assert VARIANTS[6][0] == "MLM" and VARIANTS[7][0] == "TR60"
m = bh.macdonald(1.0e6, 10.0)
a_m2 = bh.embankment_area_ft2(10.0 / 0.3048, 12.0, 3.0, 3.0) * 0.3048 ** 2
assert abs(m["bavg_m"] - bh.mlm_1984(1.0e6, 10.0, a_m2)[0]) < 1e-9 and m["z"] == 1.0 and "assumed" in m["note"]

# ---------------------------------------------------------------- 3. resolve_params / source text
eq = {"bavg_m": 20.0, "tf_s": 900.0, "z": 0.7}
p, src = bh.resolve_params(eq, None, "F2008")
assert p == {"bavg_m": 20.0, "tf_s": 900.0, "z": 0.7, "invert_m": None}
assert bh.source_summary(src, "F2008") == "equation F2008"
p, src = bh.resolve_params(eq, {"bavg_m": (30.0, "fields"), "tf_s": 0.0, "z": (0.5, "value 0.5 H:1V"),
                                "invert_m": (12.0, "fields")}, "F2008")
assert p == {"bavg_m": 30.0, "tf_s": 900.0, "z": 0.5, "invert_m": 12.0}
assert src == {"bavg_m": "fields", "tf_s": "equation F2008", "z": "value 0.5 H:1V", "invert_m": "fields"}
assert bh.source_summary(src, "F2008") == "fields: width, bottom; equation F2008: time; value 0.5 H:1V: slope"
p, src = bh.resolve_params(None, {"bavg_m": 30.0}, "TR60")      # TR-60: no equation parameters
assert p["bavg_m"] == 30.0 and p["tf_s"] is None and src == {"bavg_m": "user"}

# ---------------------------------------------------------------- 4. analyze_dam with user values
elev = np.linspace(0, 10, 11); vol = 2.0e4 * elev
base = bh.analyze_dam(elev[-6:], vol[-6:], 5.0, 10.0, 0.0, bed_elev=0.0)["td"]
r = bh.analyze_dam(elev[-6:], vol[-6:], 5.0, 10.0, 0.0, bed_elev=0.0,
                   user={"bavg_m": (2 * base["bavg_m"], "fields")})
assert abs(r["td"]["bavg_m"] - 2 * base["bavg_m"]) < 1e-9 and r["td"]["qp_route"] > base["qp_route"]
assert r["td"]["src_text"] == "fields: width; equation F2008: time, slope"
r = bh.analyze_dam(elev[-6:], vol[-6:], 5.0, 10.0, 0.0, bed_elev=0.0, user={"invert_m": 4.0})
assert r["invert_m"] == 4.0 and abs(r["td"]["hb_m"] - 6.0) < 1e-9
assert r["td"]["released_m3"] < base["released_m3"] and any("breach bottom from user" in n for n in r["notes"])
r = bh.analyze_dam(elev[-6:], vol[-6:], 5.0, 10.0, 0.0, bed_elev=0.0, user={"invert_m": -3.0})
assert r["invert_m"] == 0.0 and any("below the toe" in n for n in r["notes"])
for meth in ("TR60", "TR60-C"):
    r = bh.analyze_dam(elev[-6:], vol[-6:], 5.0, 10.0, 0.0, bed_elev=0.0, method=meth)["td"]
    v_hyd = float(np.trapezoid(r["q"], r["t"]) if hasattr(np, "trapezoid") else np.trapz(r["q"], r["t"]))
    # the hydrograph volume is the releasable storage (whole reservoir here: invert = bed)
    assert abs(v_hyd - r["released_m3"]) / r["released_m3"] < 0.02, (meth, v_hyd, r["released_m3"])
    assert abs(r["released_m3"] - 2.0e5) < 1.0 and r["bb_m"] is None and r["bavg_m"] is None
    q_cfs, rule, _ = bh.tr60_peak_cfs(2.0e5 / bh.AF_M3, 10.0 / 0.3048,
                                      bh.embankment_area_ft2(10.0 / 0.3048, 12.0, 3.0, 3.0))
    assert abs(r["qp_route"] - q_cfs * bh.CFS_M3S) < 1e-6 and rule in r["notes"][0]
    assert r["method"] == meth and r["src_text"] == f"equation {meth}"
r = bh.analyze_dam(elev[-6:], vol[-6:], 5.0, 10.0, 0.0, bed_elev=0.0, method="TR60-C")["td"]
assert len(r["t"]) > 3                                # curvilinear: many ordinates, triangle: 3
r = bh.analyze_dam(elev[-6:], vol[-6:], 5.0, 10.0, 0.0, bed_elev=0.0, method="TR60", user={"tf_s": 100.0})
assert any("not used" in n for n in r["td"]["notes"]) and r["td"]["src_text"] == "user: time"
try:
    bh.analyze_dam(elev[-6:], vol[-6:], 5.0, 10.0, 0.0, bed_elev=0.0, method="nope"); raise SystemExit("no error")
except KeyError:
    pass
print("OK (analyze_dam with user parameters and TR-60 hydrographs)")

# ---------------------------------------------------------------- 5. the tool against the test doubles
work = tempfile.mkdtemp(prefix="brch_")
curves = os.path.join(work, "curves"); os.makedirs(curves)
with open(os.path.join(curves, "curve_DWR-1.csv"), "w") as fh:
    fh.write("elev_m,storage_m3\n" + "".join(f"{e:.3f},{v:.1f}\n" for e, v in zip(elev[-6:], vol[-6:])))
shutil.copy(os.path.join(curves, "curve_DWR-1.csv"), os.path.join(curves, "curve_DWR-2.csv"))
F = [("dam_id", S), ("height_m", D), ("est_pool_m", D), ("est_crest_m", D), ("est_maxdep_m", D),
     ("stor_conf", S), ("stor_method", S), ("crest_len_m", D), ("b_ft", D), ("tf_hr", D), ("bot_ft", D)]
pt = QgsGeometry.fromPointXY(QgsPointXY(0.0, 0.0))
dams = make_layer(F, [
    (pt, dict(dam_id="DWR-1", height_m=10.0, est_pool_m=5.0, est_crest_m=10.0, est_maxdep_m=5.0, stor_conf="medium",
              stor_method="inventory", crest_len_m=600.0, b_ft=120.0, tf_hr=0.5, bot_ft=4.0 / 0.3048)),
    (pt, dict(dam_id="DWR-2", height_m=10.0, est_pool_m=5.0, est_crest_m=10.0, est_maxdep_m=5.0, stor_conf="medium",
              stor_method="inventory", crest_len_m=600.0, b_ft=0.0, tf_hr=None, bot_ft=None))], "point", "dams")
ctx, fb = QgsProcessingContext(), QgsProcessingFeedback()
DAMS = ctx.add(dams)


def run(**params):
    base_p = dict(DAMS=DAMS, CURVES=curves, MODE=0, METHOD=0, SUNNY=False, EXP=2.0,
                  OUTDIR=os.path.join(work, "hydro"))
    base_p.update(params)
    r = BreachHydrograph().run_mock(base_p, ctx, fb)
    src = ctx.layers[r["OUT"]]
    n = src.fields().names()
    return {f["dam_id"]: {k: f[k] for k in n} for f in src.getFeatures()}


t0 = run()                                                    # old call: no new parameter given
assert all(r["brch_src"] == "equation F2008" and r["brch_eq"] == "F2008" for r in t0.values())
assert abs(t0["DWR-1"]["base_m"] - 0.0) < 1e-9
t1 = run(PARAM_SOURCE=1, USER_B_FIELD="b_ft", USER_TF_FIELD="tf_hr", USER_BOT_FIELD="bot_ft")
assert abs(t1["DWR-1"]["td_bavg_m"] - 120.0 * 0.3048) < 1e-9 and abs(t1["DWR-1"]["td_tf_min"] - 30.0) < 1e-9
assert abs(t1["DWR-1"]["base_m"] - 4.0) < 1e-6 and abs(t1["DWR-1"]["td_hb_m"] - 6.0) < 1e-6
assert t1["DWR-1"]["brch_src"] == "fields: width, time, bottom; equation F2008: slope", t1["DWR-1"]["brch_src"]
assert t1["DWR-2"]["brch_src"] == "equation F2008" and abs(t1["DWR-2"]["td_bavg_m"] - t0["DWR-2"]["td_bavg_m"]) < 1e-9
t2 = run(PARAM_SOURCE=2, USER_B_FIELD="b_ft", USER_B_FT=60.0, USER_Z=0.5, USER_TF_HR=0.0)
assert abs(t2["DWR-1"]["td_bavg_m"] - 120.0 * 0.3048) < 1e-9               # the field wins where filled
assert abs(t2["DWR-2"]["td_bavg_m"] - 60.0 * 0.3048) < 1e-9 and t2["DWR-2"]["td_z"] == 0.5
assert t2["DWR-2"]["brch_src"] == "value 60 ft: width; equation F2008: time; value 0.5 H:1V: slope", t2["DWR-2"]["brch_src"]
assert t2["DWR-1"]["brch_src"].startswith("fields: width;")
t3 = run(PARAM_SOURCE=1, USER_B_FIELD="no_such_field")
assert any("no_such_field" in e for e in fb.errors) and t3["DWR-1"]["brch_src"] == "equation F2008"
t4 = run(METHOD=list(bh.BREACH_METHODS).index("TR60"))
assert t4["DWR-1"]["brch_eq"] == "TR60" and t4["DWR-1"]["td_bb_m"] is None and os.path.exists(t4["DWR-1"]["td_csv"])
assert "TR-60" in t4["DWR-1"]["brch_notes"]
t5 = run(METHOD=list(bh.BREACH_METHODS).index("MLM"))
assert t5["DWR-1"]["brch_eq"] == "MLM" and "assumed 12 ft crest" in t5["DWR-1"]["brch_notes"]
assert len(PARAM_SOURCES) == 3
alg = BreachHydrograph(); alg.initAlgorithm()
for k in ("PARAM_SOURCE", "USER_B_FIELD", "USER_B_FT", "USER_TF_FIELD", "USER_TF_HR", "USER_Z_FIELD", "USER_Z",
          "USER_BOT_FIELD", "USER_BOT_FT"):
    assert k in alg._params, k
for k in ("USER_B_FT", "USER_BOT_FT"):
    assert "ft" in alg._params[k].description                  # rule 3: user-facing values in feet
print("OK (tool 5: fields win, then typed values, then the equation; brch_src recorded)")
shutil.rmtree(work)
print("OK")
