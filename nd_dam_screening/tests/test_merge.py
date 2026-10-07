"""0.13: step 18 'Merge dams and ponds' (core/merge.py, tool 18) and the chain with both kinds.

Run: FAKE_QGIS_FLAVOR=3.40 PYTHONPATH=tests/qgis_mock:<parent of the plugin> python3 tests/test_merge.py
"""
import os
import sqlite3
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "qgis_mock"))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
sys.path.insert(0, HERE)

import _gpkg  # noqa: E402
from qgis.core import QgsProcessingContext, QgsProcessingFeedback, QgsVectorLayer  # noqa: E402
from nd_dam_screening.core import merge as mg, workflow as wf  # noqa: E402
from nd_dam_screening.algorithms.merge_ponds import MergePonds  # noqa: E402

W = tempfile.mkdtemp(prefix="merge_")


def touch(*parts):
    p = os.path.join(*parts)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as fh:
        fh.write("x")
    return p


# ---- nothing to merge yet
try:
    mg.merge(W)
    raise AssertionError("merge without files")
except FileNotFoundError as e:
    assert "steps 1-3" in str(e)

# ---- the dams side (3, 4, 2) and the ponds side (16)
DF = [("dam_id", "TEXT"), ("name", "TEXT"), ("height_m", "REAL"), ("stor_method", "TEXT")]
_gpkg.write(wf.path(W, "3", "OUT"), {"dams": ("Point", DF, [
    (("Point", (-100.1, 47.1)), {"dam_id": "DWR-1", "name": "A", "height_m": 5.0, "stor_method": "calibrated"}),
    (("Point", (-100.2, 47.2)), {"dam_id": "DWR-2", "name": "B", "height_m": 7.0, "stor_method": "estimated"})])})
PF = DF + [("pond_type", "TEXT"), ("no_flood", "TEXT")]
_gpkg.write(wf.path(W, "16", "OUT"), {"ponds": ("Point", PF, [
    (("Point", (-100.3, 47.3)), {"dam_id": "POND-1", "name": "", "height_m": 2.0, "stor_method": "estimated",
                                 "pond_type": "embankment pond", "no_flood": ""}),
    (("Point", (-100.4, 47.4)), {"dam_id": "POND-2", "name": "", "height_m": 0.2, "stor_method": "estimated",
                                 "pond_type": "dugout", "no_flood": "YES"})])})
BF = [("dam_id", "TEXT"), ("flow_az", "REAL"), ("dir_conf", "TEXT")]
_gpkg.write(wf.path(W, "4", "OUTP"), {"bp": ("Point", BF, [(("Point", (1.0, 2.0)), {"dam_id": "DWR-1", "flow_az": 90.0, "dir_conf": "high"}),
                                                           (("Point", (3.0, 4.0)), {"dam_id": "DWR-2", "flow_az": 180.0, "dir_conf": "low"})])})
_gpkg.write(wf.path(W, "16", "OUTP"), {"bp": ("Point", BF, [(("Point", (5.0, 6.0)), {"dam_id": "POND-1", "flow_az": 45.0, "dir_conf": "medium"})])})
_gpkg.write(wf.path(W, "4", "OUTL"), {"ar": ("LineString", [("dam_id", "TEXT")], [(("LineString", [(1, 2), (2, 3)]), {"dam_id": "DWR-1"})])})
_gpkg.write(wf.path(W, "16", "OUTL"), {"ar": ("LineString", [("dam_id", "TEXT")], [(("LineString", [(5, 6), (6, 7)]), {"dam_id": "POND-1"})])})
touch(W, "3_stage_storage", "curve_DWR-1.csv")
touch(W, "3_stage_storage", "curve_DWR-2.csv")
touch(W, "16_stage_storage", "curve_POND-1.csv")
touch(W, "2_terrain", "dam_DWR-1.tif")
touch(W, "16_terrain", "dam_POND-1.tif")
touch(W, "16_terrain", "notes.txt")

r = mg.merge(W)
assert r["n_dams"] == 2 and r["n_ponds"] == 2 and r["n_curves"] == 3 and r["n_terrain"] == 2, r
assert r["dams"] == wf.path(W, "18", "OUT") and os.path.isfile(r["bpts"]) and os.path.isfile(r["arrows"])
assert any("added column pond_type" in n for n in r["notes"])
lyr = QgsVectorLayer(r["dams"], "all", "ogr")
assert lyr.isValid()
rows = {str(f["dam_id"]): f for f in lyr.getFeatures()}
assert set(rows) == {"DWR-1", "DWR-2", "POND-1", "POND-2"}
assert rows["POND-2"]["pond_type"] == "dugout" and rows["POND-2"]["no_flood"] == "YES"
assert rows["DWR-1"]["pond_type"] is None and rows["DWR-1"]["stor_method"] == "calibrated"
assert abs(rows["POND-1"].geometry().asPoint().x() - (-100.3)) < 1e-9       # geometry copied as is
bp = {str(f["dam_id"]): f for f in QgsVectorLayer(r["bpts"], "bp", "ogr").getFeatures()}
assert set(bp) == {"DWR-1", "DWR-2", "POND-1"} and bp["POND-1"]["flow_az"] == 45.0
con = sqlite3.connect(r["dams"])
assert con.execute("SELECT min_x FROM gpkg_contents").fetchone()[0] is None     # extent left to QGIS
con.close()
assert sorted(os.listdir(r["curves"])) == ["curve_DWR-1.csv", "curve_DWR-2.csv", "curve_POND-1.csv"]
assert sorted(os.listdir(r["terrain"])) == ["dam_DWR-1.tif", "dam_POND-1.tif"]
# the sources are untouched
assert len(list(QgsVectorLayer(wf.path(W, "3", "OUT"), "d", "ogr").getFeatures())) == 2
# running it again replaces the 18_* files (no doubling)
r2 = mg.merge(W)
assert r2["n_dams"] == 2 and r2["n_ponds"] == 2
assert len(list(QgsVectorLayer(r2["dams"], "all", "ogr").getFeatures())) == 4

# ---- steps 5-10 take the merged files
S = {}
d5 = wf.defaults("5", W, S)
assert d5["DAMS"] == wf.path(W, "18", "OUT") and d5["CURVES"] == wf.path(W, "18", "CURVES")
assert d5["BPTS"] == wf.path(W, "18", "OUTP")
touch(W, "5_dams_breach.gpkg")
assert wf.defaults("7", W, S)["DEMDIR"] == wf.path(W, "18", "TERRAIN")
assert wf.is_done(W, "18") and wf.DONE_MARK["18"] == "OUT" and wf.REQUIRES["18"] == ("3", "4", "16")
assert wf.KEYS.index("18") == wf.KEYS.index("4") + 1 and "18" in wf.OPTIONAL and "18" in wf.CHAIN_OPTIONAL

# ---- the chain: "both" keeps 1-4 and adds 16 then 18
W2 = tempfile.mkdtemp(prefix="merge2_")
plan = wf.chain_plan(W2, {"waterbodies": "/w.gpkg"}, include=("16", "18"))
assert plan[:7] == ["16", "1", "2", "3", "4", "18", "5"], plan
plan_p = wf.chain_plan(W2, {"waterbodies": "/w.gpkg"}, include=("16",))
assert not set(plan_p) & {"1", "2", "3", "4"}
# step 18 sits after 4 in the step order, so the chain reaches it once 16 and 1-4 are done
touch(W2, "16_ponds.gpkg")
nxt, problem = wf.chain_next(W2, {"waterbodies": "/w.gpkg", "dwr": "/d.gpkg"}, include=("16", "18"), ran=("16",))
assert nxt == "1", (nxt, problem)                                  # 18 waits for 3 and 4

# ---- the tool
ctx, fb = QgsProcessingContext(), QgsProcessingFeedback()
res = MergePonds().run_mock({"WORK": W}, ctx, fb)
assert res["N_DAMS"] == 2 and res["N_PONDS"] == 2 and res["OUT"].endswith("18_dams_all.gpkg")
try:
    MergePonds().run_mock({"WORK": W2}, ctx, fb)
    raise AssertionError("merge without 3_dams_storage")
except Exception as e:                                           # noqa
    assert "steps 1-3" in str(e)
print("OK (merge: union of columns, geometry copied, curves and terrain linked, 18 wins for 5-10, chain 'both')")
