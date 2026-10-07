"""0.19.0: DWR unknown structures and dugouts, the storage leak guard, DEM height / water estimates,
estimated values in dam.json and the suite data folder. Pure Python (no QGIS)."""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "core"))
sys.path.insert(0, os.path.join(HERE, ".."))
import numpy as np  # noqa: E402
import inventory as inv  # noqa: E402
import storage as st  # noqa: E402

FT, AF = inv.FT_M, inv.AF_M3

# ---- which DWR export ---------------------------------------------------------------------------
UNK_FIELDS = ["cp_index", "structure_id", "dam_name", "max_dam_height", "max_pool_vol", "normal_vol",
              "normal_area", "fed_hazard_class", "status", "national_id", "dam_length", "rev_structure_id"]
DUG_FIELDS = ["structure_id", "name", "max_storage", "max_depth", "max_height", "max_surface_area",
              "depth_of_cut", "service_level_storage", "service_level_surface_area", "fed_hazard_class",
              "structure_status", "storage_above_grade_acft", "purpose"]
DAM_FIELDS = ["structure_id", "dam_name", "max_dam_height", "max_pool_vol", "normal_vol", "normal_area",
              "hazard_class", "status", "national_id", "dam_length", "fed_hazard_class"]
assert inv.detect_kind(UNK_FIELDS) == inv.KIND_UNKNOWN
assert inv.detect_kind(DUG_FIELDS) == inv.KIND_DUGOUT
assert inv.detect_kind(DAM_FIELDS) == inv.KIND_DAM
assert inv.detect_kind(DAM_FIELDS, "DWR_Unknown_Structures") == inv.KIND_UNKNOWN      # the name wins
assert inv.detect_kind([], "dwr_dugouts.gpkg") == inv.KIND_DUGOUT
assert inv.detect_kind(list(inv.DWR_DEFAULTS.values())) == inv.KIND_DAM                # the dBase export

# ---- field names: the dBase defaults are replaced by the export's own names --------------------
fld, notes = inv.resolve_fields(dict(inv.DWR_DEFAULTS), DAM_FIELDS, inv.KIND_DAM)
assert fld["ID"] == "structure_id" and fld["H"] == "max_dam_height" and fld["MS"] == "max_pool_vol"
assert fld["HZ"] == "hazard_class" and any("structure_" in n for n in notes)
fld, _n = inv.resolve_fields(dict(inv.DWR_DEFAULTS), list(inv.DWR_DEFAULTS.values()), inv.KIND_DAM)
assert fld == dict(inv.DWR_DEFAULTS)                                       # nothing changes for the dBase
fld, _n = inv.resolve_fields({}, UNK_FIELDS, inv.KIND_UNKNOWN)
assert fld["HZ"] == "fed_hazard_class" and fld["NAME"] == "dam_name"
dfld, _n = inv.resolve_fields({}, DUG_FIELDS, inv.KIND_DUGOUT)
assert dfld["H"] is None and dfld["LEN"] is None and dfld["STATUS"] == "structure_status"
assert dfld["ABOVE"] == "storage_above_grade_acft" and dfld["MS"] == "max_storage"

# ---- a dugout releases only its above-grade storage ---------------------------------------------
row = {"structure_id": 2000, "name": "T. Padilla Pond 2", "max_storage": 66.7, "max_depth": 25.0,
       "max_height": 13.0, "max_surface_area": 3.74, "storage_above_grade_acft": 23.3,
       "structure_status": "Intact", "fed_hazard_class": "Low", "service_level_storage": 0.0,
       "service_level_surface_area": 0.0}
a = inv.standardize(row.get, dfld, kind=inv.KIND_DUGOUT)
assert a["id"] == "2000" and a["kind"] == inv.KIND_DUGOUT and a["h"] is None
assert abs(a["ms"] - 23.3 * AF) < 1e-6 and abs(a["total"] - 66.7 * AF) < 1e-6 and a["ns"] is None
assert abs(a["sa"] - 3.74 * inv.AC_M2) < 1e-6 and abs(a["dug_h"] - 13 * FT) < 1e-9
row["storage_above_grade_acft"] = 0.0                                      # not recorded
a = inv.standardize(row.get, dfld, kind=inv.KIND_DUGOUT)
assert a["ms"] is None and a["above"] is None and a["total"] is not None    # never the total

# ---- status filter: auto, blank, cut statuses ---------------------------------------------------
assert inv.keep_for("auto", inv.KIND_DAM) == "Intact"
k = inv.parse_status_list(inv.keep_for("AUTO", inv.KIND_UNKNOWN))
assert inv.status_ok("Unknown", k) and inv.status_ok("", k) and inv.status_ok(None, k)
assert inv.status_ok("Intact", k) and not inv.status_ok("Breached", k)
k = inv.parse_status_list(inv.keep_for("auto", inv.KIND_DUGOUT))
assert inv.status_ok("Pending", k) and not inv.status_ok("Not Built", k) and not inv.status_ok("", k)
assert not inv.status_ok("", inv.parse_status_list("Intact"))               # blank needs "(blank)"
assert inv.status_ok("Location Inconclusiv", inv.parse_status_list("Location Inconclusive"))
assert inv.keep_for("Intact, Breached", inv.KIND_UNKNOWN) == "Intact, Breached"   # a typed list stays
assert inv.ID_PREFIX[inv.KIND_UNKNOWN] == "UNK-" and inv.ID_PREFIX[inv.KIND_DUGOUT] == "DUG-"


# ---- storage leak guard -------------------------------------------------------------------------
def basin(n=120, cell=2.0, saddle=103.0):
    """A bowl (rim at 105 m) around a pond at 100 m, with a saddle at `saddle` m on the east side
    that opens onto a wide plain at 101 m."""
    yy, xx = np.mgrid[0:n, 0:n].astype(float)
    r = np.hypot(yy - n / 2, xx - n / 3) * cell
    dem = np.where(r < 40, 99.0 + r / 40.0, 105.0)                         # bowl, rim 105
    dem[:, int(n * 0.62):] = 101.0                                          # the plain beyond the rim
    band = (np.abs(yy - n / 2) < 3) & (xx > n / 3 + 18) & (xx < n * 0.62)
    dem[band] = saddle                                                      # the saddle in the rim
    water = r < 15
    dem[water] = 100.0
    return dem, water, cell


dem, water, cell = basin()
curve_free = st.above_pool_curve(dem, water, 100.0, 105.0, cell)            # no guard: leaks
c, spill, note = st.above_pool_curve_guarded(dem, water, 100.0, 105.0, cell)
assert spill is not None and 102.8 < spill <= 103.0, spill
assert c[-1][1] < 0.25 * curve_free[-1][1], (c[-1][1], curve_free[-1][1])   # the plain is not storage
assert "spill level" in note and "edge of the terrain clip" not in note
assert all(b[1] >= a_[1] for a_, b in zip(c, c[1:]))                        # still a rising curve
dem2, water2, _ = basin(saddle=106.0)                                        # no low point in the rim
c2, spill2, _n = st.above_pool_curve_guarded(dem2, water2, 100.0, 105.0, cell)
assert spill2 is None and abs(c2[-1][1] - st.above_pool_curve(dem2, water2, 100.0, 105.0, cell)[-1][1]) < 1e-6
r = st.estimate_storage(dem, water, cell, (60, 40), known_height_m=5.0)
# a dam of known height: the curve goes on past the spill level to the crest with the area at the spill
assert r.crest_elev > 103.0 and any("leak at the spill level" in x for x in r.notes) and r.confidence == "low"
assert all(b[1] >= a_[1] for a_, b in zip(r.curve, r.curve[1:])) and abs(r.curve[-1][0] - r.crest_elev) < 1e-6
r_off = st.estimate_storage(dem, water, cell, (60, 40), known_height_m=5.0, leak_guard=False)
assert r_off.total_m3 > 2 * r.total_m3
r_nh = st.estimate_storage(dem, water, cell, (60, 40))                       # no height: held at the spill
assert r_nh.crest_elev <= 103.0 + 1e-6 and any("held at the spill level" in x for x in r_nh.notes)
# DWR maximum storage is the volume at the top of dam
cv0 = [(100.0, 50.0), (101.0, 80.0), (102.0, 140.0)]
up, k = st.set_top(cv0, 500.0)
assert up[0] == (100.0, 50.0) and abs(up[-1][1] - 500.0) < 1e-9 and k > 1
dn, k = st.set_top(cv0, 30.0)
assert abs(dn[-1][1] - 30.0) < 1e-9

# crest higher than pool + dam height is capped
r = st.estimate_storage(dem2, water2, cell, (60, 40), known_height_m=2.0)
assert r.crest_elev <= 100.0 + 2.0 + 1e-6 and any("more than the dam height" in x for x in r.notes)

# ---- inventory maximum: hold or scale -----------------------------------------------------------
cv = [(100.0, 0.0), (101.0, 100.0), (102.0, 300.0), (103.0, 600.0)]
held, e = st.hold_at(cv, 200.0)
assert abs(e - 101.5) < 1e-9 and held[-1] == (101.5, 200.0) and len(held) == 3
assert st.hold_at(cv, 1000.0) == (cv, None)
sc, k, note = st.scale_to_total(cv, 1200.0)
assert k == 2.0 and sc[-1][1] == 1200.0 and "scaled" in note
sc, k, note = st.scale_to_total(cv, 6000.0 * 20)
assert k == 10.0 and "clamped" in note

# ---- height from the DEM ------------------------------------------------------------------------
yy, xx = np.mgrid[0:80, 0:80].astype(float)
d = np.full((80, 80), 300.0)
d[:, 40:43] = 305.0                                                          # embankment, crest 305
d[:, 43:] = 299.0                                                            # downstream valley 299
w = np.zeros((80, 80), bool)
w[20:60, 10:38] = True
h, note = st.estimate_height(d, w, 1.0, (40, 41), 305.0, radius_m=20.0)
assert abs(h - 6.0) < 1e-9 and "ESTIMATED" in note
h, note = st.estimate_height(np.full((80, 80), 305.0), w, 1.0, (40, 41), 305.0)
assert h is None and "not estimated" in note

# ---- water body from the hydro-flattened DEM ----------------------------------------------------
d = np.add.outer(np.arange(200) * 0.05, np.arange(200) * 0.05) + 300.0
d[80:120, 90:130] = 301.0
m, note = st.flat_water_mask(d, 1.0, (100, 85), 60.0)
assert m is not None and m.sum() == 1600 and "taken from the DEM" in note
m, note = st.flat_water_mask(d, 1.0, (190, 190), 20.0)
assert m is None and "no flat" in note
d[80:120, 90:130] += np.linspace(0, 0.5, 40)[None, :]                        # a sloping field, not water
m, note = st.flat_water_mask(d, 1.0, (100, 85), 60.0)
assert m is None

# ---- dam.json: estimated values say so -----------------------------------------------------------
import importlib  # noqa: E402
dri = importlib.import_module("core.damrecord_io")
assert dri.estimate_notes({"h_src": "estimated (DEM)", "stor_src": "inventory"}) == {"height_ft": dri.ESTIMATE_NOTE}
assert dri.estimate_notes({"h_src": "inventory"}) == {}
with tempfile.TemporaryDirectory() as tmp:
    vals = {"dam_id": "UNK-2819", "name": "139-080-23BD", "height_ft": 12.5, "storage_acft": 3.2}
    path, log = dri.write(tmp, vals, {"triage": "not run", "hz_class": "Low"}, "0.19.0",
                          field_notes=dri.estimate_notes({"h_src": "estimated (DEM)", "stor_src": "estimated (DEM)"}))
    j = json.load(open(path))
    assert j["fields"]["height_ft"] == 12.5 and j["sources"]["height_ft"]["note"] == dri.ESTIMATE_NOTE
    assert j["sources"]["storage_acft"]["note"] == dri.ESTIMATE_NOTE and j["sources"]["name"]["note"] == "screening"
    assert not log

# ---- suite data: the dwr/ folder ----------------------------------------------------------------
sd = importlib.import_module("damsuite.suitedata")
with tempfile.TemporaryDirectory() as tmp:
    os.makedirs(os.path.join(tmp, "dwr", "_other"))
    for f in ("dwr/DWR_Dam_Lines.gpkg", "dwr/DWR_Dams.gpkg", "dwr/_other/DWR_Unknown_Structures.gpkg",
              "dwr/_other/DWR_Dugouts.gpkg", "dwr/_other/DWR_Dugouts_Polygons.gpkg", "dwr/_other/notes.gpkg"):
        open(os.path.join(tmp, f), "w").close()
    lay = sd.dwr_layers(tmp)
    assert lay["dwr_dams"].endswith("DWR_Dams.gpkg") and lay["dwr_dam_lines"].endswith("DWR_Dam_Lines.gpkg")
    assert lay["dwr_unknown"].endswith("DWR_Unknown_Structures.gpkg")
    assert lay["dwr_dugouts"].endswith("DWR_Dugouts.gpkg")
    assert lay["dwr_dugout_polygons"].endswith("DWR_Dugouts_Polygons.gpkg")
    os.rename(os.path.join(tmp, "dwr/_other/DWR_Unknown_Structures.gpkg"), os.path.join(tmp, "dwr/A_unknown.gpkg"))
    lay = sd.dwr_layers(tmp)                    # an unknown-structures file sorting first is not the dams
    assert lay["dwr_dams"].endswith("DWR_Dams.gpkg") and lay["dwr_unknown"].endswith("A_unknown.gpkg")

# ---- report: estimated inputs are named -----------------------------------------------------------
rep = importlib.import_module("core.report")
rec = {"dam_id": "UNK-2819", "kind": "unknown structure", "h_src": "estimated (DEM)", "stor_src": "estimated (DEM)",
       "hz_class": "Low", "hz_conf": "low"}
assert rep.estimated_inputs(rec) == ["height", "storage"]
assert any("ESTIMATED height and storage" in x for x in rep.review_reasons(rec))
page = rep.dam_page(rec)
assert "Estimated inputs." in page and "DWR unknown structure" in page
assert "kind" in rep.SUMMARY_COLS and "h_src" in rep.SUMMARY_COLS
# ---- dry basin: no water in the DEM, the nearest closed depression is the pond ------------------
yy, xx = np.mgrid[0:80, 0:80]
bowl = 100.0 + 0.002 * ((yy - 40.0) ** 2 + (xx - 40.0) ** 2)
bowl = np.minimum(bowl, 101.6)                 # a 1.6 m deep bowl on a plain
bowl += 0.0005 * xx                            # the plain drains to one side
mask, dnote = st.depression_mask(bowl, 3.0, (40, 40), 150.0)
assert mask is not None and mask[40, 40] and "dry basin" in dnote, dnote
flat = 100.0 + 0.01 * xx                       # a slope: no closed basin
mask2, _n = st.depression_mask(flat, 3.0, (40, 40), 150.0)
assert mask2 is None

# ---- a pool that leaks at its own level (perched pond, lower ground past the dam) ---------------
dem = np.full((60, 60), 105.0)
dem[:, 40:] = 98.0                             # the valley below the dam, lower than the pool
dem[20:30, 20:30] = 99.0                       # the pond bed
dem[20:30, 30:40] = 99.5                       # a DEM-coarse embankment the pool sees through
wm = np.zeros(dem.shape, bool); wm[20:30, 20:30] = True
curve, spill, note = st.above_pool_curve_guarded(dem, wm, 100.0, 102.0, 3.0, steps=4)
assert spill is None and "leaks at the pool level" in note, note
assert abs(curve[-1][1] - curve[0][1] - 100 * 9.0 * 2.0) < 1e-6        # prism: area x rise
res = st.estimate_storage(dem, wm, 3.0, dam_xy_idx=(25, 32), known_height_m=3.0)
assert res.confidence == "low" and any("leak" in n for n in res.notes), res.notes
print("OK")
