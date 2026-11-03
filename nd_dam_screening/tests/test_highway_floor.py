"""0.33.3 / nd_hazard 1.3.0: US and State highways are at least Medium (REG_05.2026 5.2.1.2(b)).

Tool 8 (hazard classification) and triage use core/nd_hazard.py, the Runner's rules module: an
overtopped US / State highway is at least Medium whatever its AADT; the private / AADT < 500 Low
rule (5.2.1.2(a)) is for local, county and private roads. The highway is read from the road
class field or, new, the road's name.

Run: PYTHONPATH=tests/qgis_mock:<parent of the plugin> python3 tests/test_highway_floor.py
"""
import os, shutil, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(HERE, "qgis_mock"))
sys.path.insert(0, os.path.dirname(PLUGIN))
from qgis.core import QgsGeometry, QgsPointXY, QgsProcessingContext, QgsProcessingFeedback, make_layer  # noqa: E402
import numpy as np  # noqa: E402
from nd_dam_screening.algorithms import _common as cm  # noqa: E402
from nd_dam_screening.algorithms.hazard_classification import HazardClassification  # noqa: E402
from nd_dam_screening.core import hazard as hz, nd_hazard as nh  # noqa: E402

# ---- 1. the rule (road_level / nd_road)
assert hz.road_level(300, "ND 23") == "Medium" and hz.road_level(10, "US 2") == "Medium"
assert hz.road_level(300, "State Highway 6") == "Medium" and hz.road_level(300, "S") == "Medium"
assert hz.road_level(300, "HIGHWAY 23") == "Medium"
assert hz.road_level(8501, "HIGHWAY 23") == "High" and hz.road_level(100, "I-94") == "High"
assert hz.road_level(300, "County Highway 12") == "Low" and hz.road_level(300, "") == "Low"
assert hz.road_level(None, "ND 23", unknown="Low") == "Medium"       # never Low for a highway
r = hz.nd_road("r", 0.3, 300, "", name="HIGHWAY 23")                 # the name counts too
assert r.us_state and nh.road_class(r, nh.Settings())[0] == "Medium"
assert not hz.nd_road("r", 0.3, 300, "M", name="30TH ST SE").us_state
print("OK (rule)")

# ---- 2. tool 8 end to end: a State highway with AADT 300 through the flood
work = tempfile.mkdtemp(prefix="hwy_")
Sx, Dd = "string", "double"
try:
    cell = 10.0
    gt = (0.0, cell, 0.0, 100.0, 0.0, -cell)
    depth = np.zeros((10, 10)); vel = np.zeros((10, 10))
    depth[2, :], vel[2, :] = 0.5, 0.5
    dtif, vtif = os.path.join(work, "d.tif"), os.path.join(work, "v.tif")
    cm.write_tif(dtif, depth, gt); cm.write_tif(vtif, vel, gt)
    xy = lambda r, c: (gt[0] + (c + 0.5) * cell, gt[3] - (r + 0.5) * cell)
    pt = lambda r, c: QgsGeometry.fromPointXY(QgsPointXY(*xy(r, c)))
    SIMF = [("dam_id", Sx), ("nid_id", Sx), ("sim_status", Sx), ("depth_tif", Sx), ("vel_tif", Sx),
            ("haz_existing", Sx)]
    sim = make_layer(SIMF, [
        (pt(0, 0), {"dam_id": "DWR-2", "nid_id": "ND00002", "sim_status": "ok", "depth_tif": dtif,
                    "vel_tif": vtif, "haz_existing": "Low"}),
        (pt(0, 1), {"dam_id": "DWR-1", "nid_id": "", "sim_status": "ok", "depth_tif": dtif,
                    "vel_tif": vtif, "haz_existing": "Low"})], "point", "sim")

    def run(road_class, name):
        roads = make_layer([("aadt", Dd), ("road_class", Sx), ("name", Sx)],
                           [(QgsGeometry.fromPolylineXY([QgsPointXY(*xy(2, 0)), QgsPointXY(*xy(2, 9))]),
                             {"aadt": 300.0, "road_class": road_class, "name": name})], "line", "roads")
        ctx, fb = QgsProcessingContext(), QgsProcessingFeedback()
        res = HazardClassification().run_mock({"SIM": ctx.add(sim), "ROADS": ctx.add(roads), "CRIT": 2},
                                              ctx, fb)
        out = ctx.layers[res["OUT"]]
        nms = out.fields().names()
        return {f["dam_id"]: {k: f[k] for k in nms} for f in out.getFeatures()}

    rows = run("State Highway", "ND 23")
    d = rows["DWR-1"]
    assert d["n_road_wet"] == 1 and d["hz_class"] == "Medium", d
    assert "5.2.1.2(b)" in (d["hz_reason"] or ""), d["hz_reason"]
    # by the name only (NG9-1-1 'HIGHWAY 23', no class field value)
    assert run("", "HIGHWAY 23")["DWR-1"]["hz_class"] == "Medium"
    # the suite's ND NG9-1-1 roads: FULL_NAME + SHIELD (US 83 through a town: 'S WASHINGTON ST',
    # SHIELD USHY); RoadClass 'Primary' alone is not a highway
    def run_ng(name, shield):
        roads = make_layer([("aadt", Dd), ("RoadClass", Sx), ("FULL_NAME", Sx), ("SHIELD", Sx)],
                           [(QgsGeometry.fromPolylineXY([QgsPointXY(*xy(2, 0)), QgsPointXY(*xy(2, 9))]),
                             {"aadt": 300.0, "RoadClass": "Primary", "FULL_NAME": name, "SHIELD": shield})],
                           "line", "ND_roads_NG911")
        ctx, fb = QgsProcessingContext(), QgsProcessingFeedback()
        res = HazardClassification().run_mock({"SIM": ctx.add(sim), "ROADS": ctx.add(roads), "CRIT": 2},
                                              ctx, fb)
        return {f["dam_id"]: f["hz_class"] for f in ctx.layers[res["OUT"]].getFeatures()}["DWR-1"]
    assert run_ng("S WASHINGTON ST", "USHY") == "Medium"
    assert run_ng("HIGHWAY 23", "") == "Medium"
    assert run_ng("107M AVE SE", "") == "Low" and run_ng("100TH AVE NE", "CORD") == "Low"
    # the same road as a local road: Low, as before (5.2.1.2(a))
    loc = run("Local", "30TH ST SE")["DWR-1"]
    assert loc["n_road_wet"] == 1 and loc["hz_class"] == "Low", loc
    print("OK (tool 8): State highway AADT 300 -> Medium; local road AADT 300 -> Low")

finally:
    shutil.rmtree(work, ignore_errors=True)
print("ALL OK")
