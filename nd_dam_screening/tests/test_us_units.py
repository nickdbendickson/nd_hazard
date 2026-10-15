"""0.18.0 US units: the twin-field rules, the sink that fills them, migrated settings, the
feet parameters and the summary / hydrograph numbers."""
import os, sys
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "qgis_mock"))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))          # parent of the plugin package
from nd_dam_screening.core import us_units as uu, workflow as wf  # noqa: E402

# ---- twin names
T = lambda n: (uu.twin(n) or (None,))[0]
assert T("height_m") == "height_ft" and T("td_qp_m3s") == "td_qp_cfs" and T("td_vw_m3") == "td_vw_acft"
assert T("inv_area_m2") == "inv_area_ac" and T("max_vel_ms") == "max_vel_fps" and T("max_dv_m2s") == "max_dv_ft2s"
assert T("qp_f95") == "qp_f95_cfs" and T("td_qp_tr60") == "td_qp_tr60_cfs"
assert T("dam_id") is None and T("sim_notes") is None and T("dem_m") is None and T("m") is None
# an existing field of the twin's name is reused (the sink refreshes it)
assert ("height_m", "height_ft", uu.FT) in uu.twin_pairs(["height_m", "height_ft"])
assert abs(uu.convert(3.048, uu.FT) - 10.0) < 1e-9 and uu.convert("x", uu.FT) is None
assert uu.convert(None, uu.FT) is None and uu.convert(float("nan"), uu.FT) is None
assert uu.say(3.048) == "10 ft" and uu.say(1233.48183754752, "m3") == "1 ac-ft"

# ---- settings saved in metres are converted; the new key wins
m = wf.migrate_settings({"dem_cell": 1.0, "grid_m": 10.0, "simh": 6.0})
assert "dem_cell" not in m and "grid_m" not in m and m["dem_cell_ft"] == 3.3 and m["grid_ft"] == 32.8 and m["simh"] == 6.0
m = wf.migrate_settings({"grid_m": 10.0, "grid_ft": 50.0})
assert m["grid_ft"] == 50.0
# 0.28.0: the cached DEM's cell follows the SOURCE cell (3DEP and the suite's downloads are 10 m
# = 32.8 ft); it used to resample a 10 m source to 1 m
assert wf.DEFAULT_SETTINGS["dem_cell_ft"] == 32.8 and wf.DEFAULT_SETTINGS["grid_ft"] == 33.0
assert "dem_cell" not in wf.DEFAULT_SETTINGS and "grid_m" not in wf.DEFAULT_SETTINGS
p = wf.defaults("7", "/tmp/w", {"grid_m": 20.0})
assert abs(p["CELL_FT"] - 65.6) < 0.01 and "CELL" not in p, p
p = wf.defaults("2", "/tmp/w", {})
assert p["CELL_FT"] == 32.8                            # 0.28.0: 10 m, the source cell
assert wf.defaults("2", "/tmp/w", {"dem_cell_ft": 3.3})["CELL_FT"] == 3.3    # a 1 m LiDAR folder

# ---- the sink fills the twins (qgis mock)
from qgis.core import QgsFields, QgsField, QgsFeature, QgsGeometry, QgsPointXY  # noqa: E402
from nd_dam_screening.algorithms import _common as cm  # noqa: E402
from nd_dam_screening.compat import FT_DOUBLE, FT_STRING  # noqa: E402

fl = QgsFields()
for n, t in (("dam_id", FT_STRING), ("height_m", FT_DOUBLE), ("td_qp_m3s", FT_DOUBLE), ("td_vw_m3", FT_DOUBLE)):
    fl.append(QgsField(n, t))
f2 = cm.with_us(fl)
assert f2.names() == ["dam_id", "height_m", "td_qp_m3s", "td_vw_m3", "height_ft", "td_qp_cfs", "td_vw_acft"], f2.names()


class Sink:
    def __init__(self): self.features = []
    def addFeature(self, f, *a): self.features.append(f); return True
    extra = "passed on"


s = Sink(); us = cm.UsSink(s, f2)
f = QgsFeature(fl); f.setAttributes(["A", 3.048, 28.316846592, 1233.48183754752])
us.addFeature(f)
g = s.features[0]
assert abs(g["height_ft"] - 10.0) < 1e-9 and abs(g["td_qp_cfs"] - 1000.0) < 1e-6 and abs(g["td_vw_acft"] - 1.0) < 1e-9
assert g["height_m"] == 3.048 and us.extra == "passed on"
f = QgsFeature(fl); f.setAttributes(["B", None, None, None]); us.addFeature(f)
assert s.features[1]["height_ft"] is None

# ---- feet inputs; a saved model with the old metre parameter still works
class A:
    def parameterAsDouble(self, params, k, ctx): return float(params[k])


a = A()
assert abs(cm.distance_m(a, {"CELL_FT": 33.0}, None, "CELL_FT", "CELL", 33.0) - 10.0584) < 1e-6
assert cm.distance_m(a, {"CELL": 10.0}, None, "CELL_FT", "CELL", 33.0) == 10.0            # old parameter
assert cm.distance_m(a, {"CELL": 10.0, "CELL_FT": 66.0}, None, "CELL_FT", "CELL", 33.0) == 66.0 * 0.3048  # ft wins
assert cm.distance_m(a, {"CELL_FT": 0.0}, None, "CELL_FT", "CELL", 33.0) == 0.0            # 0 = automatic
assert cm.distance_m_opt(a, {}, None, "CELL_FT", "CELL") is None
assert cm.distance_m_opt(a, {"CELL_FT": None}, None, "CELL_FT", "CELL") is None
assert abs(cm.distance_m_opt(a, {"CELL_FT": 50.0}, None, "CELL_FT", "CELL") - 15.24) < 1e-9
assert cm.distance_m_opt(a, {"CELL": 12.0}, None, "CELL_FT", "CELL") == 12.0
# the terrain cache is still reused for a 3.3 ft request over a 1 m cut
import tempfile, json  # noqa: E402
d = tempfile.mkdtemp(); dst = os.path.join(d, "dam_1.tif"); open(dst, "w").close()
ext = [0.0, 0.0, 100.0, 100.0]
json.dump({"cell": 1.0, "extent": ext, "source": "3dep"}, open(dst + ".json", "w"))
ok, why = cm.clip_reusable(dst, ext, 3.3 * 0.3048, "3dep")
assert ok, why
ok, why = cm.clip_reusable(dst, ext, 10.0, "3dep")
assert not ok and "ft" in why, why
print("OK (us units: twins, settings migration, sink)")
