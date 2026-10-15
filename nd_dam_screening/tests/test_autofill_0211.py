"""0.21.1: tool 1's field boxes found in the layer (DWR GeoPackage and NID names), the NID read with
empty boxes, tool 1's DWR / NID slots falling back to the suite data in the window, the remembered
last outputs and the window's note when 1_dams.gpkg is missing. Qt test doubles only (qgis_mock).

Run: PYTHONPATH=tests/qgis_mock:<parent of the plugin> python3 tests/test_autofill_0211.py
"""
import json, os, sys, tempfile

os.environ["HOME"] = tempfile.mkdtemp(prefix="af0211_home_")
for _k in ("NDSUITE_DATA", "NDSUITE_DAMS"):
    os.environ.pop(_k, None)
HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(HERE, "qgis_mock"))
sys.path.insert(0, os.path.dirname(PLUGIN))

from nd_dam_screening.core import inventory as inv  # noqa: E402
from nd_dam_screening.core import workflow as wf  # noqa: E402
from nd_dam_screening.algorithms import prepare_dams as PD  # noqa: E402
from nd_dam_screening.algorithms import _common as cm  # noqa: E402

# the field names of the exports in the project's layers folder (DWR_Dams.gpkg, NID_dams.gpkg)
DWR_GPKG = ["cp_index", "national_id", "dam_name", "hazard_class", "max_dam_height", "dam_length",
            "max_pool_area", "max_pool_vol", "normal_vol", "normal_area", "structure_id", "status",
            "county"]
NID = ["OBJECTID", "NIDID", "NAME", "DAM_HEIGHT", "HYDRAULIC_HEIGHT", "STRUCTURAL_HEIGHT",
       "NID_HEIGHT", "DAM_LENGTH", "NID_STORAGE", "MAX_STORAGE", "NORMAL_STORAGE", "SURFACE_AREA",
       "HAZARD_POTENTIAL", "OPERATIONAL_STATUS"]

# ---- the NID's fields, found without case ----------------------------------------------------
f, notes = inv.resolve_fields({}, NID, inv.KIND_DAM, inv.NID_FIELD_CANDIDATES)
assert f == {"ID": "NIDID", "NAME": "NAME", "H": "NID_HEIGHT", "NS": "NORMAL_STORAGE",
             "MS": "MAX_STORAGE", "SA": "SURFACE_AREA", "HZ": "HAZARD_POTENTIAL", "STATUS": None,
             "NIDREF": None, "LEN": "DAM_LENGTH"}, f
assert notes == []
# a chosen field the layer has is kept
f, _n = inv.resolve_fields({"H": "DAM_HEIGHT"}, NID, inv.KIND_DAM, inv.NID_FIELD_CANDIDATES)
assert f["H"] == "DAM_HEIGHT"
# the DWR table is unchanged (default candidates)
f, _n = inv.resolve_fields({}, DWR_GPKG)
assert f["ID"] == "structure_id" and f["H"] == "max_dam_height" and f["MS"] == "max_pool_vol"
# an NID record read with the found fields has its ID, height and storage
row = {"NIDID": "ND00123", "NAME": "Elm River Dam 1", "NID_HEIGHT": 30.0, "NORMAL_STORAGE": 500.0,
       "MAX_STORAGE": 1200.0, "SURFACE_AREA": 80.0, "HAZARD_POTENTIAL": "High", "DAM_LENGTH": 900.0}
f, _n = inv.resolve_fields({}, NID, inv.KIND_DAM, inv.NID_FIELD_CANDIDATES)
rec = inv.standardize(lambda n: row.get(n), f, False, "NID", 0)
assert rec["id"] == "ND00123" and abs(rec["h"] - 30 * inv.FT_M) < 1e-9
assert abs(rec["ms"] - 1200 * inv.AF_M3) < 1e-6 and rec["hz"] == "High"
# before 0.21.0 (empty boxes, no resolving) the same record had no ID or values
old = inv.standardize(lambda n: row.get(n), {k: None for k in f}, False, "NID", 0)
assert old["id"] == "NID0" and old["h"] is None and old["ms"] is None

# ---- the dialog's fill: the boxes that are empty or name a field the layer lacks -------------
cur = {k: v for k, v in PD.FIELD_DEFAULTS.items()}                 # DWR_ID = structure_ ...
cur = {k[4:]: v for k, v in cur.items()}
got = PD.fill_fields("DWR", DWR_GPKG, "DWR_Dams", 0, cur)
assert got["DWR_ID"] == "structure_id" and got["DWR_H"] == "max_dam_height", got
assert got["DWR_SA"] == "normal_area" and got["DWR_MS"] == "max_pool_vol", got
assert "DWR_NAME" not in got and "DWR_NS" not in got                # dam_name / normal_vol are there
got = PD.fill_fields("NID", NID, "NID_dams", 0, {})
assert got["NID_ID"] == "NIDID" and got["NID_MS"] == "MAX_STORAGE" and "NID_STATUS" not in got
got = PD.fill_fields("NID", NID, "NID_dams", 0, {"H": "DAM_HEIGHT"})   # the user's choice stays
assert "NID_H" not in got
assert PD.fill_fields("DWR", [], "", 0, {}) == {}
# a dugout layer: no dam length, no height
DUG = ["structure_id", "name", "max_storage", "max_height", "max_surface_area", "depth_of_cut",
       "structure_status", "storage_above_grade_acft", "service_level_storage"]
got = PD.fill_fields("DWR", DUG, "DWR_Dugouts", 0, {})
assert got["DWR_ID"] == "structure_id" and "DWR_LEN" not in got and "DWR_H" not in got, got
# run-log lines name the fields found
assert PD._found_notes({"ID": None}, {"ID": "NIDID"}) == ["ID field: 'NIDID' (found in the layer)"]
assert PD._found_notes({"ID": "NIDID"}, {"ID": "NIDID"}) == []
# the field boxes say an empty one is found
alg = PD.PrepareDams()
alg.initAlgorithm()
d = alg.parameterDefinition("NID_H")
desc = d.description() if callable(d.description) else d.description
assert "(empty = found in the layer)" in desc, desc

# ---- tool 1 in the window: an empty DWR slot falls back to the suite data ----------------------
tmp = tempfile.mkdtemp(prefix="af0211_")
data = os.path.join(tmp, "data")
os.makedirs(os.path.join(data, "dwr"))
dwr_file = os.path.join(data, "dwr", "DWR_Dams.geojson")
with open(dwr_file, "w") as fh:
    json.dump({"type": "FeatureCollection", "features": []}, fh)
work = os.path.join(tmp, "work")
os.makedirs(work)
wf.suite_data_folder = lambda: data
logged = []
p = wf.defaults("1", work, {}, log=logged.append)
assert p.get("DWR") == dwr_file, p
assert any("dwr/" in m for m in logged), logged
p = wf.defaults("1", work, {"dwr": "/my/choice.gpkg"})
assert p["DWR"] == "/my/choice.gpkg"                                 # a chosen layer always wins
# the NID: no pack, a file in nid/ (the fill button and tool 1 both use it)
assert "NID" not in wf.defaults("1", work, {})
os.makedirs(os.path.join(data, "nid"))
nid_file = os.path.join(data, "nid", "NID_dams.gpkg")
open(nid_file, "w").close()
open(os.path.join(data, "nid", "_old.gpkg"), "w").close()          # "_" files are skipped
assert wf.nid_dropin(data) == nid_file
assert wf.defaults("1", work, {})["NID"] == nid_file
got = wf.fill_empty_slots(work, {}, prepare=False, slots=("dwr", "nid"))
assert got == {"dwr": (dwr_file, "dwr folder"), "nid": (nid_file, "nid folder")}, got
assert wf.fill_empty_slots(work, {"nid": "/mine.gpkg"}, prepare=False, slots=("nid",)) == {}
wf.suite_data_folder = lambda: ""
assert "DWR" not in wf.defaults("1", work, {}) and "NID" not in wf.defaults("1", work, {})

# ---- tools 2 - 4: the window's note, the remembered outputs ------------------------------------
assert "DAMS" not in wf.defaults("2", work, {})
note = wf.missing_dams_note(work)
assert "1_dams.gpkg" in note and work in note
gp = os.path.join(work, "1_dams.gpkg")
open(gp, "w").close()
assert wf.defaults("2", work, {})["DAMS"] == gp
cm.remember_output("dams", gp)
assert cm.last_output("dams") == gp
cm.remember_output("dams3", os.path.join(work, "gone.gpkg"))
assert cm.last_output("dams3") is None                               # forgotten when the file is gone
assert cm.is_temporary("Standardized_dams_0a1b2c") and cm.is_temporary("memory:x")
assert not cm.is_temporary(gp) and not cm.is_temporary(gp + "|layername=dams")
# ---- 0.21.1: the county --------------------------------------------------------------------
from nd_dam_screening.core import nd_counties as cty  # noqa: E402
assert len(cty.names()) == 53 and cty.names()[0] == "Adams"
assert cty.normalize("cass county") == "Cass" and cty.normalize("Nowhere") == ""
assert cty.parse("Eddy, cass, Eddy") == ["Eddy", "Cass"] and cty.parse("") == [] and cty.parse(cty.ALL) == []
try:
    cty.parse("Eddy, Atlantis")
    raise AssertionError("an unknown county must be refused")
except ValueError as e:
    assert "Atlantis" in str(e)
assert cty.county_of(-96.79, 46.88) == "Cass"            # Fargo
assert cty.county_of(-100.78, 46.81) == "Burleigh"       # Bismarck
assert cty.county_of(-98.90, 47.75) == "Eddy"            # New Rockford
assert cty.county_of(-90.0, 40.0) == ""
b = cty.box(["Eddy"])
assert b[0] < -98.9 < b[2] and b[1] < 47.75 < b[3] and cty.box([]) is None
assert [f["properties"]["NAME"] for f in cty.features(["Eddy"])] == ["Eddy"]
# the window: the county goes to tool 1 (as the list's indexes) and narrows the work area
w2 = os.path.join(tmp, "work_county")
os.makedirs(w2)
assert wf.chosen_counties({"counties": "Eddy"}) == ["Eddy"]
assert wf.chosen_counties({"counties": cty.ALL}) == [] and wf.chosen_counties({}) == []
p = wf.defaults("1", w2, {"counties": "Eddy"})
assert p["COUNTIES"] == [cty.names().index("Eddy")], p
assert "COUNTIES" not in wf.defaults("1", w2, {})
wf_area = wf.work_area(w2, {"counties": "Eddy"})
assert wf_area[0] < b[0] and wf_area[2] > b[2] and wf_area[2] - wf_area[0] < 2.0, wf_area
assert wf.work_area(w2, {}) == tuple(wf._ds("offline_pack").ND_BBOX)
# step 0 is clipped to the county outline (named only; written when the window opens the tool)
assert wf.county_clip(w2, {}) is None
assert wf.defaults("0", w2, {"counties": "Eddy"})["CLIP"] == os.path.join(w2, wf.COUNTY_CLIP) + "|layername=county"
assert wf.defaults("0", w2, {"counties": "Eddy", "clip": "/my/clip.gpkg"})["CLIP"] == "/my/clip.gpkg"
print("test_autofill_0211: all checks passed")
