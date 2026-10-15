"""0.28.3: the three Screening fixes from the Absaraka comparison (2026-10-13).

  1. tool 3: a basin FOUND IN THE DEM that is much shallower than the dam is not the reservoir,
     so the full-height inventory curve is used instead of stretching the shallow one to the
     DWR maximum (algorithms/estimate_storage, SHALLOW_BASIN). THIS MOVES THE PEAK FLOW of a
     dry dam: the stretched curve put nearly all the storage in the top few feet.
  2. the hand-off sends no surface area when the curve spans much less than the dam height
     (core/handoff.area_for_runner), so the Runner assumes storage ~ depth squared instead of
     reading a storage-area exponent at its cap of 6 off a 4 ft-deep curve.
  3. a run can name the suite data folder in its settings ("suite_folder"), for a computer with
     no QGIS setting (core/workflow.settings_data_folder).

FAIL_ON_ERROR (the chain and the hand-off failing when a run fails) is in test_runchain.py and
test_handoff.py, beside the behaviour it changed.

Run: FAKE_QGIS_FLAVOR=3.40 PYTHONPATH=tests/qgis_mock:<parent of the plugin> python3 tests/test_v0283.py
"""
import json
import os
import sys
import tempfile

import numpy as np
from osgeo import gdal

os.environ["HOME"] = tempfile.mkdtemp(prefix="v0283_home_")       # no suite config of another run
for _k in ("NDSUITE_DATA", "NDSUITE_DAMS", "NDSUITE_PACK"):
    os.environ.pop(_k, None)

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(HERE, "qgis_mock"))
sys.path.insert(0, os.path.dirname(PLUGIN))

from qgis.core import (QgsGeometry, QgsPointXY, QgsProcessingContext,  # noqa: E402
                       QgsProcessingFeedback, make_layer)
from nd_dam_screening.core import handoff as ho, storage as st, workflow as wf  # noqa: E402
from nd_dam_screening.algorithms.estimate_storage import EstimateStorage, SHALLOW_BASIN  # noqa: E402

ROOT = tempfile.mkdtemp(prefix="v0283_")
AF, FT = st.AF_M3, st.FT_M
D, S = "double", "string"

# ============================================ 1. a shallow DEM basin is not the dam's reservoir
# Absaraka: a dry flood-control dam 27.5 ft high, DWR maximum 1,615 ac-ft, and the only water in
# the DEM a shallow dish a few feet deep. The DEM here is a plane with a 1 ft-deep flat dish at
# the dam point - the shape that gave a 383 ac surface area at the top of dam.
N, CELL = 80, 10.0
HEIGHT_FT, MAXV_AF = 27.5, 1615.5
plane = 500.0 + 0.002 * np.arange(N)[None, :] * CELL + 0.001 * np.arange(N)[:, None] * CELL
dem = plane.copy()
dem[36:44, 36:44] = plane[36:44, 36:44].min() - 1.0 * FT          # a 1 ft-deep flat dish
demdir = os.path.join(ROOT, "dem")
os.makedirs(demdir)
for did in ("DWR-2801", "DWR-SHALLOW-NOINV"):
    ds = gdal.GetDriverByName("GTiff").Create(os.path.join(demdir, "dam_{}.tif".format(did)),
                                              N, N, 1, 6)
    ds.SetGeoTransform((0.0, CELL, 0.0, N * CELL, 0.0, -CELL))
    ds.GetRasterBand(1).WriteArray(dem)
    ds.FlushCache()
    ds = None

F = [("dam_id", S), ("height_m", D), ("inv_max_m3", D), ("inv_norm_m3", D), ("stor_src", S),
     ("h_src", S)]
centre = QgsGeometry.fromPointXY(QgsPointXY(N * CELL / 2.0, N * CELL / 2.0))
dams = make_layer(F, [
    (centre, dict(dam_id="DWR-2801", height_m=HEIGHT_FT * FT, inv_max_m3=MAXV_AF * AF)),
    # the same dam without inventory numbers: nothing to fall back to, so the DEM basin stands
    (centre, dict(dam_id="DWR-SHALLOW-NOINV", height_m=HEIGHT_FT * FT))], "point", "dams")
wbs = make_layer([("gnis_name", S)], [], "polygon", "wb")          # no mapped water body
ctx, fb = QgsProcessingContext(), QgsProcessingFeedback()
curves = os.path.join(ROOT, "curves")
res = EstimateStorage().run_mock(dict(DAMS=ctx.add(dams), WB=ctx.add(wbs), DEMDIR=demdir,
                                      SEARCH_FT=500.0, SHAPE=1.0, CURVES=curves, OUT="memory:"),
                                 ctx, fb)
out = {str(f["dam_id"]): f for f in ctx.layers[res["OUT"]].getFeatures()}

f = out["DWR-2801"]
assert f["stor_method"] == "inventory curve", (f["stor_method"], f["stor_notes"])
assert f["stor_src"] == "inventory (assumed curve)" and f["stor_conf"] == "low"
assert "deep against a dam height of 27.5 ft, so it is not the reservoir" in str(f["stor_notes"])
assert "ASSUMPTION" in str(f["stor_notes"]) and "1,616 ac-ft" in str(f["stor_notes"])
# the curve now spans the whole dam and holds the DWR maximum at the top
assert abs(f["est_crest_m"] - f["est_pool_m"] - HEIGHT_FT * FT) < 1e-6
assert abs(f["stor_used_m3"] / AF - MAXV_AF) / MAXV_AF < 0.01
assert "DWR maximum storage" not in str(f["stor_notes"])           # no stretch was needed
assert any("DWR-2801" in w and "not the reservoir of this dam" in w for w in fb.warnings), fb.warnings
curve = ho.read_curve(os.path.join(curves, "curve_DWR-2801.csv"))
span = curve[-1][0] - curve[0][0]
assert abs(span - HEIGHT_FT * FT) < 1e-6, span
# what the Runner would make of it: area at the top = 2V/H, so its exponent is 2, not the cap
area = ho.area_at_top_m2(os.path.join(curves, "curve_DWR-2801.csv"), f["est_crest_m"])
m = area * span / curve[-1][1]
assert 1.8 < m < 2.2, m
print("OK (1: a shallow DEM basin falls back to the full-height inventory curve)")

# without inventory numbers there is nothing better, so the DEM basin is still used, as before
g = out["DWR-SHALLOW-NOINV"]
assert g["stor_method"] != "inventory curve" and g["stor_notes"], g["stor_method"]
assert g["est_crest_m"] - g["est_pool_m"] < SHALLOW_BASIN * HEIGHT_FT * FT
print("OK (1b: with no inventory storage the DEM basin stands)")

# ================================================== 2. no surface area off a too-short curve
short = os.path.join(ROOT, "curve_short.csv")
with open(short, "w") as fh:                     # 4.2 ft of a 27.5 ft dam, the Absaraka shape
    fh.write("elev_m,storage_m3,elev_ft,storage_acft\n")
    top = 4.2 * FT
    for i in range(11):
        z = i * top / 10.0
        fh.write("{:.3f},{:.1f},,\n".format(500.0 + z, MAXV_AF * AF * (i / 10.0) ** 2))
area, note = ho.area_for_runner(short, None, HEIGHT_FT * FT)
assert area is None and "4.2 ft of the 27.5 ft dam height" in note, (area, note)
assert "depth squared" in note
# the same curve with no height given, and a full-height curve: the area is sent as before
assert ho.area_for_runner(short, None, None)[0] is not None
full = os.path.join(ROOT, "curve_full.csv")
with open(full, "w") as fh:
    fh.write("elev_m,storage_m3,elev_ft,storage_acft\n")
    for i in range(11):
        z = i * HEIGHT_FT * FT / 10.0
        fh.write("{:.3f},{:.1f},,\n".format(500.0 + z, MAXV_AF * AF * (i / 10.0) ** 2))
area, note = ho.area_for_runner(full, None, HEIGHT_FT * FT)
assert note == "" and area is not None
assert abs(area * HEIGHT_FT * FT / (MAXV_AF * AF) - 2.0) < 0.1     # exponent 2, as the curve says
assert ho.area_for_runner(os.path.join(ROOT, "no.csv"), None, 1.0) == (None, "")
print("OK (2: no surface area from a curve far shorter than the dam)")

# ============================================ 3. the suite data folder from the run's settings
real_hook = wf.suite_data_folder
try:
    wf.suite_data_folder = lambda: "/computer/setting"             # what this computer is set to
    assert wf.settings_data_folder({}) == "/computer/setting"
    assert wf.settings_data_folder(None) == "/computer/setting"
    assert wf.settings_data_folder({"suite_folder": ""}) == "/computer/setting"
    assert wf.settings_data_folder({"suite_folder": " /from/settings "}) == "/from/settings"
    assert wf.settings_data_folder({"suite_folder": "~/nd"}) == os.path.expanduser("~/nd")
    assert "suite_folder" in wf.DEFAULT_SETTINGS and wf.DEFAULT_SETTINGS["suite_folder"] == ""

    # a run with no QGIS setting at all finds the pack named in its settings
    wf.suite_data_folder = lambda: ""
    data = os.path.join(ROOT, "suite_data")
    pack = os.path.join(data, "ND_Dam_Suite_Pack")
    os.makedirs(pack)
    with open(os.path.join(pack, "manifest.json"), "w") as fh:
        json.dump({"format": 2, "datasets": {"DWR_dams": {"status": "complete", "layer": "DWR_dams"}}},
                  fh)
    work = os.path.join(ROOT, "work")
    os.makedirs(work)
    assert wf.slot_origin("dwr", work, {})[1] != "suite pack"      # nothing set: not found
    value, origin = wf.slot_origin("dwr", work, {"suite_folder": data})
    assert origin == "suite pack", (value, origin)
    assert value.startswith(os.path.join(work, wf.SUITE_DIR, "dwr.gpkg")), value
finally:
    wf.suite_data_folder = real_hook
print("OK (3: suite_folder in the run's settings)")
print("ALL OK")
