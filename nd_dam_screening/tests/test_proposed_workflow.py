"""0.14: step 19 stands in for steps 1-4, and its outputs win over a pond layer.

Run: PYTHONPATH=tests/qgis_mock:<parent of the plugin> python3 tests/test_proposed_workflow.py
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "qgis_mock"))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
from nd_dam_screening.core import workflow as wf  # noqa: E402
from nd_dam_screening.algorithms.propose_dam import proposed_id  # noqa: E402


def touch(*parts):
    p = os.path.join(*parts)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    open(p, "w").close()


assert proposed_id("", 3) == "PROP-3"
assert proposed_id("12", 1) == "PROP-12"
assert proposed_id("West Pond", 1) == "West_Pond"
assert "19" in wf.OPTIONAL and "19" in wf.CHAIN_OPTIONAL and "19" not in wf.CHAIN_SKIP
assert wf.ALG["19"] == "proposedam"
assert wf.REQUIRES["19"] == ()

W = tempfile.mkdtemp(prefix="propwf_")
touch(W, "19_proposed.gpkg")
touch(W, "19_stage_storage", "curve_PROP-1.csv")
touch(W, "19_terrain", "dam_PROP-1.tif")
touch(W, "19_terrain", "dam_PROP-1_natural.tif")
touch(W, "19_breach_points.gpkg")
touch(W, "19_crest_lines.gpkg")
assert wf.is_done(W, "19") and all(wf.is_done(W, k) for k in ("1", "2", "3", "4"))
assert all(wf.covered_by_proposed(W, k) for k in ("1", "2", "3", "4"))
assert not any(wf.covered_by_ponds(W, k) for k in ("1", "2", "3", "4"))
rows = wf.statuses(W, {})
assert "step 19" in [r for r in rows if r[0] == "1"][0][3]
assert wf.next_step(W) == "5"
p5 = wf.defaults("5", W, {})
assert p5["DAMS"].endswith("19_proposed.gpkg")
assert p5["CURVES"].endswith("19_stage_storage")
assert p5["BPTS"].endswith("19_breach_points.gpkg")
assert wf.defaults("7", W, {})["DEMDIR"].endswith("19_terrain")
assert wf.defaults("7", W, {})["DAMLINES"].endswith("19_crest_lines.gpkg")
assert wf.defaults("7b", W, {})["DEM"].endswith("dam_PROP-1.tif")
assert "_natural" not in wf.defaults("7b", W, {})["DEM"]

# an existing step-3 layer is not replaced by step 19
touch(W, "3_dams_storage.gpkg")
touch(W, "3_stage_storage", "curve_x.csv")
assert wf.defaults("5", W, {})["DAMS"].endswith("3_dams_storage.gpkg")

# ponds still win when step 19 is absent
P = tempfile.mkdtemp(prefix="pondonly_")
touch(P, "16_ponds.gpkg")
touch(P, "16_stage_storage", "c.csv")
touch(P, "16_pond_breach_points.gpkg")
touch(P, "16_terrain", "dam_POND-1.tif")
assert wf.defaults("5", P, {})["DAMS"].endswith("16_ponds.gpkg")
assert "step 16" in [r for r in wf.statuses(P, {}) if r[0] == "2"][0][3]

# a chain that includes 19 does not also schedule steps 1-4
plan = wf.chain_plan(tempfile.mkdtemp(prefix="emptyprop_"), {}, include=("19",))
assert "19" in plan and "1" not in plan and "16" not in plan, plan
print("proposed workflow ok")
