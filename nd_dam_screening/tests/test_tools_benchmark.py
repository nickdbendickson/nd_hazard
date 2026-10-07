"""0.11: tool 11 'Compare with a reference model' and core/benchmark.py.

The metric functions are copies of the ND Dam Breach Runner's core/benchmark.py; when the
Runner is on the machine both modules are run on the same arrays and must give identical
results. The tool runs against the QGIS / GDAL test doubles in tests/qgis_mock.
"""
import csv, os, sys, tempfile, shutil
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "qgis_mock"))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
from qgis.core import (QgsGeometry, QgsPointXY, QgsProcessingContext, QgsProcessingFeedback,  # noqa: E402
                       QgsProcessingException, make_layer)
from qgis.PyQt.QtCore import QVariant  # noqa: E402
from nd_dam_screening.core import benchmark as bm, gridutil as gu  # noqa: E402
from nd_dam_screening.algorithms import _common as cm  # noqa: E402
from nd_dam_screening.algorithms.compare_reference import CompareReference  # noqa: E402

D, S = "double", "string"
rng = np.random.default_rng(3)

# ---------------------------------------------------------------- 1. synthetic flood pair (ft)
# (the odd constants keep every depth away from the 1 / 3 ft thresholds: the rasters are float32)
ny, nx = 60, 80
yy, xx = np.mgrid[0:ny, 0:nx]
depth_a = np.clip(6.013 - 0.15 * np.abs(yy - 30) - 0.02 * xx, 0.0, None)        # ours, ft
depth_b = np.clip(6.517 - 0.17 * np.abs(yy - 28) - 0.02 * xx, 0.0, None)        # reference, ft
depth_b[depth_b <= 0.2] = np.nan                                              # reference dry = nodata
vel_a = np.where(depth_a > 0, 1.0 + 0.3 * depth_a, 0.0); vel_b = np.where(np.isfinite(depth_b), 0.9 + 0.3 * np.nan_to_num(depth_b), np.nan)
arr_a = np.where(depth_a > 0, 0.1 + 0.02 * xx, np.nan); arr_b = np.where(np.isfinite(depth_b), 0.15 + 0.02 * xx, np.nan)
ours = {"depth": depth_a, "velocity": vel_a, "arrival": arr_a}
ref = {"depth": depth_b, "velocity": vel_b, "arrival": arr_b}
m = bm.compare_fields(ours, ref, 0.1, 1.0)
assert 0 < m["fit_index_F"] < 1 and m["hit_rate"] > 0.5 and m["depth_ft"]["n"] > 0
assert m["velocity_fps"]["bias"] > 0 and m["arrival_hr"]["bias"] < 0
assert m["verdict"].startswith("extents") and "depth_class_matrix" in m
arrays = bm.difference_arrays(ours, ref, 1.0)
code = arrays["Benchmark Extent Agreement.tif"]
assert set(np.unique(code[np.isfinite(code)])) <= {1.0, 2.0, 3.0}
assert bm.verdict({"fit_index_F": 0.95}).startswith("extents agree closely")
assert bm.verdict({"fit_index_F": 0.85}).startswith("extents agree well")
assert "noticeably" in bm.verdict({"fit_index_F": 0.7}) and "a lot" in bm.verdict({"fit_index_F": 0.3})

# ---------------------------------------------------------------- 2. identical to the Runner's module
runner = None
for cand in ("/home/claude/suite", os.path.dirname(os.path.dirname(HERE))):
    if os.path.exists(os.path.join(cand, "nd_breach_runner", "core", "benchmark.py")):
        if cand not in sys.path:
            sys.path.insert(0, cand)
        try:
            from nd_breach_runner.core import benchmark as runner  # noqa: E402
        except Exception as e:  # noqa: BLE001
            print("Runner import failed:", e)
        break
if runner is None:
    print("SKIP: the ND Dam Breach Runner is not on this machine; equality with its benchmark not checked")
else:
    for thr in (0.5, 1.0, 2.0):
        for o, r in ((ours, ref), ({"depth": depth_a}, {"depth": depth_b}),
                     ({"depth": depth_a, "wse": depth_a + 100}, {"depth": depth_b, "wse": depth_b + 100.5})):
            m1 = bm.compare_fields(o, r, 0.0247, thr, (1.0, 3.0, 6.0))
            m2 = runner.compare_fields(o, r, 0.0247, thr, (1.0, 3.0, 6.0))
            assert m1 == m2, (thr, m1, m2)
            a1 = bm.difference_arrays(o, r, thr); a2 = runner.difference_arrays(o, r, thr)
            assert a1.keys() == a2.keys()
            for k in a1:
                assert np.array_equal(a1[k], a2[k], equal_nan=True)
    assert bm.verdict({"fit_index_F": 0.7, "depth_ft": {"bias": -0.3, "p90_abs": 1.2}}) == \
        runner.verdict({"fit_index_F": 0.7, "depth_ft": {"bias": -0.3, "p90_abs": 1.2}})
    # the copied source text is the same, function by function
    import inspect
    for fn in ("_wet", "_err", "compare_fields", "verdict", "difference_arrays"):
        assert inspect.getsource(getattr(bm, fn)) == inspect.getsource(getattr(runner, fn)), fn
    print("OK (compare_fields, verdict and difference_arrays are the Runner's, byte for byte)")

# ---------------------------------------------------------------- 3. units, names, folders
u = bm.to_us({"depth": np.array([0.3048, -9999.0, np.nan, 2.0]), "velocity": np.array([0.3048]),
              "arrival": np.array([1.5, -9999.0])}, "metric", nodata=-9999.0)
assert abs(u["depth"][0] - 1.0) < 1e-12 and np.isnan(u["depth"][1]) and np.isnan(u["depth"][2])
assert abs(u["velocity"][0] - 1.0) < 1e-12 and u["arrival"][0] == 1.5 and np.isnan(u["arrival"][1])
u = bm.to_us({"depth": np.array([2.0, -1.0e38])}, "us")
assert u["depth"][0] == 2.0 and np.isnan(u["depth"][1])
assert bm.kind_of("Depth (Max).tif") == "depth" and bm.kind_of("Velocity (Max).tif") == "velocity"
assert bm.kind_of("Arrival Time (hours).tif") == "arrival" and bm.kind_of("DWR-5289_td_depth.tif") == "depth"
assert bm.kind_of("DWR-5289_td_vel.tif") == "velocity" and bm.kind_of("DWR-5289_td_arrive.tif") == "arrival"
assert bm.kind_of("DWR-5289_td_dv.tif") is None and bm.kind_of("WSE (Max).tif") is None
work = tempfile.mkdtemp(prefix="bench_")
rf = os.path.join(work, "ref"); os.makedirs(os.path.join(rf, "DWR-1"))
for n in ("Depth (Max).tif", "Velocity (Max).tif", "Arrival Time (hours).tif", "WSE (Max).tif", "notes.txt"):
    open(os.path.join(rf, "DWR-1", n), "w").close()
for n in ("DWR-2_depth.tif", "depth_DWR-22.tif", "DWR-2_velocity.tif", "DWR-3_vel.tif"):
    open(os.path.join(rf, n), "w").close()
r1 = bm.find_reference(rf, "DWR-1")
assert r1 == {"depth": os.path.join(rf, "DWR-1", "Depth (Max).tif"), "velocity": os.path.join(rf, "DWR-1", "Velocity (Max).tif"),
              "arrival": os.path.join(rf, "DWR-1", "Arrival Time (hours).tif")}
r2 = bm.find_reference(rf, "DWR-2")
assert r2 == {"depth": os.path.join(rf, "DWR-2_depth.tif"), "velocity": os.path.join(rf, "DWR-2_velocity.tif")}
assert bm.find_reference(rf, "DWR-3") is None and bm.find_reference(rf, "DWR-22")["depth"].endswith("depth_DWR-22.tif")
assert bm.find_reference(os.path.join(work, "nope"), "DWR-1") is None
assert bm.dams_with_reference(rf, ["DWR-1", "DWR-2", "DWR-3", "DWR-22"]) == ["DWR-1", "DWR-2", "DWR-22"]
flat = dict(bm.flatten_metrics(m))
assert "depth_ft.bias" in flat and "depth_class_matrix" not in " ".join(flat)
h = bm.report_html(m, "T", "DWR-1", [("a", "/x")], ["/y"], 1.0)
assert "<table>" in h and "DWR-1" in h and m["verdict"] in h.replace("&#x27;", "'") and "Depth classes" in h
print("OK (units, file-name kinds, reference folder layouts, report)")

# ---------------------------------------------------------------- 4. the tool with the test doubles
shutil.rmtree(rf); os.makedirs(os.path.join(rf, "DWR-1"))
cell = 10.0
gt = (500000.0, cell, 0.0, 1800000.0, 0.0, -cell)
simdir = os.path.join(work, "sim"); os.makedirs(simdir)
FT = 0.3048
for did, da, va, aa in (("DWR-1", depth_a, vel_a, arr_a), ("DWR-2", depth_a * 0.8, vel_a, arr_a)):
    cm.write_tif(os.path.join(simdir, f"{did}_td_depth.tif"), da * FT, gt)              # metres, as tool 7
    cm.write_tif(os.path.join(simdir, f"{did}_td_vel.tif"), va * FT, gt)
    cm.write_tif(os.path.join(simdir, f"{did}_td_dv.tif"), da * va * FT * FT, gt)
    cm.write_tif(os.path.join(simdir, f"{did}_td_arrive.tif"), np.where(np.isfinite(aa), aa, gu.NODATA), gt,
                 nodata=gu.NODATA)
# the reference on a finer grid (5 m), feet, dry = -9999; DWR-1 in a sub-folder, DWR-2 as named files
fine = (gt[0], 5.0, 0.0, gt[3], 0.0, -5.0)
up = lambda a: np.kron(np.where(np.isfinite(a), a, -9999.0), np.ones((2, 2)))
cm.write_tif(os.path.join(rf, "DWR-1", "Depth (Max).tif"), up(depth_b), fine, nodata=-9999.0)
cm.write_tif(os.path.join(rf, "DWR-1", "Velocity (Max).tif"), up(vel_b), fine, nodata=-9999.0)
cm.write_tif(os.path.join(rf, "DWR-1", "Arrival Time (hours).tif"), up(arr_b), fine, nodata=-9999.0)
cm.write_tif(os.path.join(rf, "DWR-2_depth.tif"), up(depth_b), fine, nodata=-9999.0)
# a metric copy of DWR-1's reference for the units option
rfm = os.path.join(work, "ref_m"); os.makedirs(rfm)
cm.write_tif(os.path.join(rfm, "DWR-1_depth.tif"), np.where(up(depth_b) > -9000, up(depth_b) * FT, -9999.0), fine,
             nodata=-9999.0)

F7 = [("dam_id", S), ("sim_status", S), ("depth_tif", S), ("vel_tif", S), ("dv_tif", S), ("arrive_tif", S)]
pt = QgsGeometry.fromPointXY(QgsPointXY(500400.0, 1799700.0))
sim = make_layer(F7, [(pt, dict(dam_id=d, sim_status="ok", depth_tif=os.path.join(simdir, f"{d}_td_depth.tif"),
                                 vel_tif=os.path.join(simdir, f"{d}_td_vel.tif"), dv_tif=os.path.join(simdir, f"{d}_td_dv.tif"),
                                 arrive_tif=os.path.join(simdir, f"{d}_td_arrive.tif"))) for d in ("DWR-1", "DWR-2")] +
                [(pt, dict(dam_id="DWR-3", sim_status="triage"))], "point", "sim")
ctx, fb = QgsProcessingContext(), QgsProcessingFeedback()
SIM = ctx.add(sim)


def run(**p):
    return CompareReference().run_mock(p, ctx, fb)


# every dam with a reference, from the results layer
out1 = os.path.join(work, "b1")
r = run(SIM=SIM, REF_FOLDER=rf, THRESH_FT=1.0, OUTDIR=out1)
assert r["N"] == 2 and os.path.exists(r["ALL_CSV"])
for d in ("DWR-1", "DWR-2"):
    for n in (bm.SUMMARY_CSV, bm.REPORT_HTML, bm.DIFF_TIF, bm.CODE_TIF):
        assert os.path.exists(os.path.join(out1, d, n)), (d, n)
with open(os.path.join(out1, "Benchmark_All.csv")) as fh:
    rows = list(csv.DictReader(fh))
assert [x["dam_id"] for x in rows] == ["DWR-1", "DWR-2"] and rows[0]["scenario"] == "td"
assert rows[0]["velocity_bias_fps"] != "" and rows[1]["velocity_bias_fps"] == ""      # DWR-2: depth only
with open(os.path.join(out1, "DWR-1", bm.SUMMARY_CSV)) as fh:
    s1 = {a: b for a, b in csv.reader(fh)}
expected = bm.compare_fields(ours, ref, cell * cell / 4046.8564224, 1.0)
assert abs(float(s1["fit_index_F"]) - expected["fit_index_F"]) < 1e-9, (s1["fit_index_F"], expected["fit_index_F"])
assert abs(float(s1["depth_ft.bias"]) - expected["depth_ft"]["bias"]) < 1e-9
assert abs(float(s1["velocity_fps.bias"]) - expected["velocity_fps"]["bias"]) < 1e-9
assert abs(float(s1["arrival_hr.bias"]) - expected["arrival_hr"]["bias"]) < 1e-9
assert s1["compared"] == "depth, velocity, arrival" and s1["cell_m"] == "10.0"
code_a, _ = cm.read_tif(os.path.join(out1, "DWR-1", bm.CODE_TIF))
exp_code = bm.difference_arrays(ours, ref, 1.0)["Benchmark Extent Agreement.tif"]
assert np.array_equal(np.where(code_a == gu.NODATA, np.nan, code_a), exp_code, equal_nan=True)
diff_a, _ = cm.read_tif(os.path.join(out1, "DWR-1", bm.DIFF_TIF))
exp_diff = bm.difference_arrays(ours, ref, 1.0)["Benchmark Depth Difference (ft).tif"]
assert np.allclose(np.where(diff_a == gu.NODATA, np.nan, diff_a), exp_diff, equal_nan=True, atol=1e-5)
print("tool 11 DWR-1:", s1["verdict"])

# one dam from the folder, with single reference rasters, metric units
r = run(SIMDIR=simdir, DAM_ID="DWR-1", REF_DEPTH=os.path.join(rfm, "DWR-1_depth.tif"), REF_UNITS=1,
        THRESH_FT=1.0, OUTDIR=os.path.join(work, "b2"))
assert r["N"] == 1 and "ALL_CSV" not in r
with open(os.path.join(work, "b2", "DWR-1", bm.SUMMARY_CSV)) as fh:
    s2 = {a: b for a, b in csv.reader(fh)}
assert abs(float(s2["fit_index_F"]) - expected["fit_index_F"]) < 1e-9 and s2["compared"] == "depth"
# a higher threshold changes the extents (fewer wet cells on both sides)
r = run(SIMDIR=simdir, DAM_ID="DWR-1", REF_FOLDER=rf, THRESH_FT=3.0, CLASSES_FT="2, 4", OUTDIR=os.path.join(work, "b3"))
with open(os.path.join(work, "b3", "DWR-1", bm.SUMMARY_CSV)) as fh:
    s3 = {a: b for a, b in csv.reader(fh)}
assert float(s3["wet_acres_ours"]) < float(s1["wet_acres_ours"]) and s3["threshold_ft"] == "3.0"
with open(os.path.join(work, "b3", "DWR-1", bm.REPORT_HTML)) as fh:
    assert "2-4" in fh.read()
# errors: no inputs, unknown dam, no reference at all
for p, word in ((dict(REF_FOLDER=rf, OUTDIR=os.path.join(work, "e")), "tool-7"),
                (dict(SIMDIR=simdir, OUTDIR=os.path.join(work, "e")), "reference"),
                (dict(SIMDIR=simdir, DAM_ID="DWR-9", REF_FOLDER=rf, OUTDIR=os.path.join(work, "e")), "DWR-9"),
                (dict(SIMDIR=simdir, REF_FOLDER=os.path.join(work, "empty"), OUTDIR=os.path.join(work, "e")), "No dam"),
                (dict(SIMDIR=simdir, DAM_ID="DWR-1", REF_FOLDER=rf, CLASSES_FT="a,b", OUTDIR=os.path.join(work, "e")), "numbers")):
    os.makedirs(os.path.join(work, "empty"), exist_ok=True)
    try:
        run(**p); raise SystemExit(f"no error for {p}")
    except QgsProcessingException as e:
        assert word in str(e), (word, str(e))
# a dam of the results layer without a reference is skipped, the others compared
r = run(SIM=SIM, REF_FOLDER=rfm, THRESH_FT=1.0, REF_UNITS=1, OUTDIR=os.path.join(work, "b4"))
assert r["N"] == 1 and os.path.exists(os.path.join(work, "b4", "DWR-1", bm.SUMMARY_CSV))
alg = CompareReference(); alg.initAlgorithm()
assert "ft" in alg._params["THRESH_FT"].description and alg._params["THRESH_FT"].defaultValue == 1.0
print("OK (tool 11 against the test doubles)")
shutil.rmtree(work)
print("OK")
