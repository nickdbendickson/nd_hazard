"""v0.32.4: tool 8 writes the top-of-dam surface area from the tool-3 curve (as tool 7b sends
AREA_AC) and removes a normal-pool surface_area_ac that an older screening left in dam.json.

Before this, a dam screened on 0.32.2 or older and screened again on 0.32.3 kept its old
surface_area_ac (Absaraka DWR-2801: 0.939 ac of mapped water), so the Runner still read the
pool area as the top-of-dam area: storage-area exponent 1 and a peak breach flow about a third
low.

Run: PYTHONPATH=tests/qgis_mock:<parent of the plugin> python3 tests/test_v0324.py
"""
import json, os, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(HERE)
PARENT = os.path.dirname(PLUGIN)
sys.path.insert(0, os.path.join(HERE, "qgis_mock"))
sys.path.insert(0, PARENT)
from nd_dam_screening.core import damrecord_io as dri, handoff as ho  # noqa: E402

FT, AF, AC = 0.3048, 1233.4818375, 4046.8564224
H_M = 27.5 * FT

attrs = {"dam_id": "DWR-2801", "name": "Absaraka", "height_m": H_M,
         "est_total_m3": 1615.0 * AF, "est_area_m2": 0.94 * AC, "inv_area_m2": 0.94 * AC}

# 1. tool8_values: the curve's top-of-dam area is written, the pool area still is not
v = dri.tool8_values(attrs, area_top_m2=150.0 * AC)
assert abs(v["surface_area_ac"] - 150.0) < 1e-3, v
assert "surface_area_ac" not in dri.tool8_values(attrs), "no curve area -> no area"
assert "surface_area_ac" not in dri.tool8_values(attrs, area_top_m2=0.0)

# 2. the same area tool 7b sends: a linear-area curve (area 600,000 m2 at the crest)
tmp = tempfile.mkdtemp()
curve = os.path.join(tmp, "curve_DWR-2801.csv")
A_TOP, base = 600000.0, 300.0
with open(curve, "w") as fh:            # area = A_TOP * d / H, storage = A_TOP * d^2 / (2 H)
    fh.write("elev_m,storage_m3\n")
    for i in range(0, 101):
        d = H_M * i / 100.0
        fh.write("{:.4f},{:.3f}\n".format(base + d, A_TOP * d * d / (2 * H_M)))
area, note = ho.area_for_runner(curve, base + H_M, H_M)
assert area is not None and note == "" and abs(area - A_TOP) / A_TOP < 0.02, (area, note)
v = dri.tool8_values(attrs, area_top_m2=area)
p, _ = ho.rundam_params(attrs, (0.0, 0.0), "EPSG:5070", "dem.tif", tmp, area_top_m2=area)
assert v["surface_area_ac"] == p["AREA_AC"], (v, p)            # tool 8 == tool 7b

# a curve over a shallow basin gives no area (0.28.3 rule), so tool 8 writes none
shallow = os.path.join(tmp, "curve_shallow.csv")
with open(shallow, "w") as fh:
    fh.write("elev_m,storage_m3\n300.0,0\n301.0,50000\n301.2,70000\n")
area_s, note_s = ho.area_for_runner(shallow, base + H_M, H_M)
assert area_s is None and "spans" in note_s, (area_s, note_s)

# 3. a dam.json written by screening 0.32.2 with the pool area: screening again removes it
dfold = os.path.join(tmp, "DWR-2801")
dri.write(dfold, dri.tool8_values(attrs), dri.screening_result(attrs, "tool 8"), "0.32.2")
rec = json.load(open(os.path.join(dfold, "dam.json")))
rec["fields"]["surface_area_ac"] = 0.939                    # what 0.32.2 wrote
rec["sources"]["surface_area_ac"] = {"tool": "ndscreen", "version": "0.32.2", "when": "x", "note": ""}
json.dump(rec, open(os.path.join(dfold, "dam.json"), "w"))
path, log = dri.write(dfold, dri.tool8_values(attrs), dri.screening_result(attrs, "tool 8"), "0.32.4")
rec = json.load(open(path))
assert "surface_area_ac" not in rec["fields"] and "surface_area_ac" not in rec["sources"], rec
assert any("surface_area_ac" in line and "removed" in line for line in log), log

# ... and with a curve area it is replaced by the top-of-dam area
rec["fields"]["surface_area_ac"] = 0.939
rec["sources"]["surface_area_ac"] = {"tool": "ndscreen", "version": "0.32.2", "when": "x", "note": ""}
json.dump(rec, open(path, "w"))
dri.write(dfold, dri.tool8_values(attrs, area_top_m2=area), dri.screening_result(attrs, "tool 8"), "0.32.4")
rec = json.load(open(path))
assert rec["fields"]["surface_area_ac"] == p["AREA_AC"], rec["fields"]
assert rec["sources"]["surface_area_ac"]["version"] == "0.32.4"

# 4. a surface area from the Runner or the engineer is never removed
for tool in ("ndbreach", ""):
    rec["fields"]["surface_area_ac"] = 154.5
    rec["sources"]["surface_area_ac"] = {"tool": tool, "version": "3.26.1", "when": "x", "note": ""}
    json.dump(rec, open(path, "w"))
    _, log = dri.write(dfold, dri.tool8_values(attrs), dri.screening_result(attrs, "tool 8"), "0.32.4")
    rec = json.load(open(path))
    assert rec["fields"]["surface_area_ac"] == 154.5, (tool, rec["fields"])
    assert not any("removed" in line for line in log), log

# 5. the other fields are untouched by the clean-up
assert abs(rec["fields"]["height_ft"] - 27.5) < 0.01 and rec["fields"]["dam_id"] == "DWR-2801"

print("OK (tool 8 writes the curve's top-of-dam area and drops the stale normal-pool area)")
