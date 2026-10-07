"""0.12: step 16 'Find ponds and dugouts' in the work-folder logic (core/workflow.py): it counts as
steps 1-4 for the later steps, its outputs are what tools 5-10 are given, and a chain with it
included runs 16 instead of 1-4.

Run: PYTHONPATH=tests/qgis_mock:<parent of the plugin> python3 tests/test_ponds_workflow.py
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "qgis_mock"))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
from nd_dam_screening.core import workflow as wf  # noqa: E402


def touch(*parts):
    p = os.path.join(*parts)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    open(p, "w").close()
    return p


def state(rows, key):
    return [r for r in rows if r[0] == key][0]


# ---- the table
assert wf.KEYS[:4] == ("0", "16", "19", "1") and "16" in wf.OPTIONAL and "16" not in wf.CHAIN_SKIP
assert wf.ALG["16"] == "findponds" and wf.algorithm_id("16") == "nddam:findponds"
assert set(wf.LAYOUT["16"]) == {"OUT", "OUTP", "OUTL", "OUTX", "TERRAIN", "CURVES"} and wf.DONE_MARK["16"] == "OUT"
assert wf.POND_COVERS == ("1", "2", "3", "4") and "16" in wf.CHAIN_OPTIONAL
assert wf.NEEDS_LAYER["16"] == ("waterbodies",) and wf.REQUIRES["16"] == ()

# ---- an empty work folder: 1-4 are not done, 16 is optional
W = tempfile.mkdtemp(prefix="pondswf_")
S = {}
rows = wf.statuses(W, S)
assert state(rows, "16")[2] == "optional" and state(rows, "1")[2] == "ready"
assert not any(wf.is_done(W, k) for k in ("1", "2", "3", "4", "16"))
assert "no NHD waterbodies layer" in state(rows, "16")[3]

# ---- with tool 16's output: steps 1-4 count as done, made by 16
touch(W, "16_ponds.gpkg")
touch(W, "16_stage_storage", "curve_POND-1.csv")
touch(W, "16_terrain", "dam_POND-1.tif")
touch(W, "16_pond_breach_points.gpkg")
assert wf.is_done(W, "16") and all(wf.is_done(W, k) for k in wf.POND_COVERS)
assert all(wf.covered_by_ponds(W, k) for k in wf.POND_COVERS) and not wf.covered_by_ponds(W, "5")
rows = wf.statuses(W, S)
for k in wf.POND_COVERS:
    assert state(rows, k)[2] == "done" and "step 16" in state(rows, k)[3], k
assert state(rows, "16")[2] == "done"
assert wf.next_step(W) == "5"                                      # 5 needs 3, which 16 stands in for
assert state(rows, "5")[2] == "ready"

# ---- the parameters given to the later tools are tool 16's outputs
p5 = wf.defaults("5", W, S)
assert p5["DAMS"] == os.path.join(W, "16_ponds.gpkg") and p5["CURVES"] == os.path.join(W, "16_stage_storage")
assert p5["BPTS"] == os.path.join(W, "16_pond_breach_points.gpkg")
touch(W, "5_dams_breach.gpkg")
for step in ("6", "7", "9", "12"):
    p = wf.defaults(step, W, S)
    assert p["DEMDIR"] == os.path.join(W, "16_terrain"), (step, p)
for step in ("6", "7", "9"):
    assert wf.defaults(step, W, S)["BPTS"] == os.path.join(W, "16_pond_breach_points.gpkg")
assert wf.defaults("7", W, S)["CURVES"] == os.path.join(W, "16_stage_storage")
assert wf.defaults("9", W, S)["DAMS"] == os.path.join(W, "16_ponds.gpkg")
# the tool's own outputs and inputs
p16 = wf.defaults("16", W, {"waterbodies": "/data/nwi.gpkg", "terrain_source": 1, "local_dem": "/dem"})
assert p16["WB"] == "/data/nwi.gpkg" and p16["SRC"] == 1 and p16["LOCAL"] == "/dem"
assert p16["OUT"] == os.path.join(W, "16_ponds.gpkg") and p16["TERRAIN"] == os.path.join(W, "16_terrain")
# a DWR run's own files win when both exist in one folder (use a folder of its own for ponds)
touch(W, "3_dams_storage.gpkg")
assert wf.defaults("5", W, S)["DAMS"] == os.path.join(W, "3_dams_storage.gpkg")
assert not wf.covered_by_ponds(W, "3") and wf.covered_by_ponds(W, "1")

# ---- results to add to the project
titles = [t for t, _p in wf.result_layers(W)]
assert any(t.startswith("16 - Find ponds and dugouts") for t in titles)

# ---- a chain with 16 included runs 16 and not 1-4
W2 = tempfile.mkdtemp(prefix="pondswf2_")
S2 = {"waterbodies": "/data/nwi.gpkg"}
step, problem = wf.chain_next(W2, S2, include=("16",))
assert (step, problem) == ("16", "")
plan = wf.chain_plan(W2, S2, include=("16",))
assert plan[0] == "16" and not set(plan) & {"1", "2", "3", "4"} and plan[1:3] == ["5", "7"], plan
assert wf.chain_plan(W2, S2)[:4] == ["1", "2", "3", "4"]             # without it: the DWR path
assert wf.chain_problems("16", W2, {}) == ["no NHD waterbodies layer"] and wf.chain_problems("16", W2, S2) == []
# after 16 has run, the next step of the chain is 5
touch(W2, "16_ponds.gpkg")
assert wf.chain_next(W2, S2, include=("16",), ran=("16",))[0] == "5"

print("OK (step 16: layout, covers 1-4, defaults for 5-12, chain plan)")
