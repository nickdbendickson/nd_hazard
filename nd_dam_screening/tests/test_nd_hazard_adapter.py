"""Tool 8 through core/nd_hazard.py (ND 5.2 / 5.3, ACER TM 11 charts) and the adapter in
core/hazard.py.

The charts used here are INVENTED TEST SHAPES (copied from the runner's
tests/test_nd_hazard.test_charts): they exercise the zone logic only and are NOT the ACER TM 11
figures. Run: PYTHONPATH=tests/qgis_mock:<parent of the plugin> python3 tests/test_nd_hazard_adapter.py
"""
import os, sys, tempfile, shutil

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(HERE, "qgis_mock"))
sys.path.insert(0, os.path.dirname(PLUGIN))
from qgis.core import (QgsGeometry, QgsPointXY, QgsProcessingContext, QgsProcessingFeedback,  # noqa: E402
                       QgsProcessingException, make_layer)
from qgis.PyQt.QtCore import QVariant  # noqa: E402
import numpy as np  # noqa: E402
from nd_dam_screening.core import hazard as hz, nd_hazard as nh  # noqa: E402
from nd_dam_screening.algorithms import _common as cm  # noqa: E402
from nd_dam_screening.algorithms.hazard_classification import HazardClassification  # noqa: E402

work = tempfile.mkdtemp(prefix="ndhz_")
NAN = float("nan")

# ---- 1. the copies are byte-identical to the runner's (skip when the runner is not here)
runner_core = os.environ.get("ND_BREACH_RUNNER_CORE") or os.path.join(
    os.path.dirname(PLUGIN), "nd_breach_runner", "core")
if os.path.exists(os.path.join(runner_core, "nd_hazard.py")):
    for fn in ("nd_hazard.py", "acer_tm11_charts.json"):
        a = open(os.path.join(PLUGIN, "core", fn), "rb").read()
        b = open(os.path.join(runner_core, fn), "rb").read()
        assert a == b, fn + " differs from the ND Dam Breach Runner copy - re-copy it, never edit it"
    print("OK (copies identical to", runner_core + ")")
else:
    print("SKIP copy check: no runner at", runner_core)
assert hz.nh is nh and nh.MODULE_VERSION == "1.1.1"


# ---- invented TEST charts (not the ACER TM 11 figures)
def test_charts(tol=0.01):
    # high danger above the line (0 ft/s, 6 ft) - (20 ft/s, 1 ft); judgment between that and
    # (0, 3) - (20, 0.5) - (25, 0.5). Invented shapes for testing only.
    house = nh.DangerChart(
        "house", "TEST house chart", "test", 25.0, 10.0,
        high=[(0, 6), (20, 1), (25, 1), (25, 10), (0, 10)],
        judgment=[(0, 3), (20, 0.5), (25, 0.5), (25, 1), (20, 1), (0, 6)], tolerance=tol)
    mobile = nh.DangerChart(
        "mobile", "TEST mobile chart", "test", 16.0, 3.0,
        high=[(0, 2), (10, 0.5), (16, 0.5), (16, 3), (0, 3)],
        judgment=[(0, 1), (10, 0.25), (16, 0.25), (16, 0.5), (10, 0.5), (0, 2)], tolerance=tol)
    return nh.ChartSet(status="digitized", digitized_by="test", checked_by="test",
                       notes="INVENTED TEST SHAPES - not the ACER TM 11 figures",
                       charts={"house": house.validate(), "mobile": mobile.validate()},
                       path="(test)", sha256="0" * 64)


chart_file = nh.save_charts(os.path.join(work, "TEST_charts_invented.json"), test_charts())
CS = hz.load_ready_charts(chart_file)
assert CS.ready and CS.path == chart_file and len(CS.sha256) == 64
FT = 0.3048

# ---- 2. unit conversion m, m/s -> ft, ft/s; unknown velocity stays None
assert abs(hz.m_to_ft(0.6096) - 2.0) < 1e-12 and hz.m_to_ft(None) is None
s = hz.nd_structure("a", 4 * FT, 1 * FT, mobile=True)
assert abs(s.depth_ft - 4.0) < 1e-12 and abs(s.vel_fts - 1.0) < 1e-12 and s.mobile and s.habitable
assert hz.nd_structure("a", 1.0, None).vel_fts is None
r = hz.nd_road("r", 0.3048, None, "")
assert abs(r.max_depth_ft - 1.0) < 1e-12 and r.aadt is None and not r.interstate
assert hz.nd_road("r", 1, NAN, "").aadt is None and hz.nd_road("r", 1, 0, "").aadt is None
assert hz.nd_road("r", 1, None, "", name="I 94").interstate
print("OK (units)")

# ---- 3. sampling = nd_hazard's samplers on the screening grids
gt = (0.0, 10.0, 0.0, 100.0, 0.0, -10.0)                   # 10 x 10 cells of 10 m
D = np.zeros((10, 10)); V = np.zeros((10, 10))
D[1, 1], V[1, 1] = 2.0, 0.5                                # cell centre (15, 85)
D[1, 2], V[1, 2] = 1.0, 3.0                                # (25, 85): shallower but faster
D[5, 5], V[5, 5] = 0.5, NAN                                # velocity unknown
D[8, 0:10] = 0.2                                           # a wet row for lines
D[0, 9] = -9999.0                                          # no data -> dry
g = hz.Grid(D, V, gt)
assert g.point(15, 85) == (2.0, 0.5) and g.point(-5, 50) == (None, None)
assert g.point(95, 95) == (None, 0.0)                      # no-data depth -> None (dry)
assert g.point(55, 45) == (0.5, None)                      # NaN velocity -> None
s = g.sample("point", [(55, 45), (15, 85)])
assert s["depth_m"] == 2.0 and s["vel_ms"] == 0.5 and (s["x"], s["y"]) == (15, 85)
ring = [(10, 90), (30, 90), (30, 80), (10, 80)]            # covers cells (1,1) and (1,2)
s = g.sample("polygon", [ring])
assert s["depth_m"] == 2.0 and s["vel_ms"] == 3.0          # max velocity over the wet cells
small = [(41, 59), (44, 59), (44, 56), (41, 56)]           # no cell centre inside: centroid cell
assert g.sample("polygon", [small])["depth_m"] == 0.0
s = g.sample("polygon", [small, ring])                     # MultiPolygon: the deepest part
assert s["depth_m"] == 2.0 and s["vel_ms"] == 3.0
s = g.sample("line", [[(0, 35), (100, 35)], [(0, 15), (100, 15)]])
assert abs(s["depth_m"] - 0.2) < 1e-12 and s["y"] == 15
assert g.sample("line", [[(0, 35), (100, 35)]])["depth_m"] == 0.0
try:
    hz.Grid(D, V, (0, 10, 0, 100, 0, -5))
    raise SystemExit("non-square cells accepted")
except AssertionError:
    pass
print("OK (sampling)")

# ---- 4. structures on the TEST charts, through nd_hazard.classify
S = lambda i, d_ft, v_fts, mobile=False, hab=True: {
    "id": i, "depth_m": d_ft * FT, "vel_ms": None if v_fts is None else v_fts * FT,
    "dv_m2s": None if v_fts is None else d_ft * v_fts * FT * FT, "mobile": mobile, "habitable": hab}
c = hz.classify_nd([S("low", 1.0, 1.0)], charts=CS)
assert c.hazard_class == "Medium" and c.governing[0].section == "5.2.2" and not c.borderline_raised
c = hz.classify_nd([S("low", 1.0, 1.0), S("judg", 4.0, 1.0)], charts=CS)
assert c.hazard_class == "Medium" and not c.borderline_raised               # judgment -> Medium by rule
assert c.counts["judgment_zone"] == 1 and c.counts["habitable_lethal"] == 0
assert any(f.zone == nh.ZONE_JUDGMENT and "MEDIUM by rule" in f.reason for f in c.governing)
c = hz.classify_nd([S("low", 1.0, 1.0), S("judg", 4.0, 1.0)], charts=CS, judgment_zone_high=True)
assert c.hazard_class == "High" and c.borderline_raised                     # old option: 5.3 -> High
assert c.governing[0].zone == nh.ZONE_JUDGMENT and c.counts["judgment_zone"] == 1
c = hz.classify_nd([S("high", 8.0, 1.0), S("judg", 4.0, 1.0)], charts=CS)
assert c.hazard_class == "High" and not c.borderline_raised
c = hz.classify_nd([S("mh", 1.5, 1.0, mobile=True)], charts=CS)             # mobile chart: judgment
assert c.hazard_class == "Medium" and "Fig. 5.2" in c.governing[0].section
c = hz.classify_nd([S("nv", 0.5, None)], charts=CS)                         # unknown velocity
assert c.hazard_class == "High" and c.borderline_raised and "no velocity" in c.data_gaps[0]
c = hz.classify_nd([S("barn", 5.0, 5.0, hab=False)], charts=CS)            # uninhabited -> Low
assert c.hazard_class == "Low" and c.counts["structures_wet"] == 1 and c.counts["habitable_wet"] == 0
assert hz.classify_nd([S("a", 2.5, 0.0)], method=nh.LETHALITY_1D).hazard_class == "High"
assert hz.classify_nd([S("a", 1.9, 9.0)], method=nh.LETHALITY_1D).hazard_class == "Medium"
c = hz.classify_nd([S("a", 0.2, 0.0)], method=nh.LETHALITY_ALL_WET)
assert c.hazard_class == "High" and c.borderline_raised
# 'any depth' = 0.1 ft by default
assert hz.classify_nd([{"id": "d", "depth_m": 0.03, "vel_ms": 0}], method=nh.LETHALITY_1D).hazard_class == "Low"
assert hz.classify_nd([{"id": "w", "depth_m": 0.0305, "vel_ms": 0}], method=nh.LETHALITY_1D).hazard_class == "Medium"
try:
    hz.classify_nd([], method=nh.LETHALITY_1D, wet_m=0.0)
    raise SystemExit("wet depth 0 accepted")
except ValueError:
    pass
print("OK (structures through nd_hazard, TEST charts)")

# ---- 5. roads, railroads, lifelines, downstream dams
R = lambda i, aadt, cls, d=0.3, sole=False: {"id": i, "depth_m": d, "aadt": aadt, "road_class": cls,
                                            "sole_access": sole}
L1 = nh.LETHALITY_1D
assert hz.classify_nd(roads=[R("1", 300, "county road")], method=L1).hazard_class == "Low"
assert hz.classify_nd(roads=[R("1", 300, "", sole=True)], method=L1).hazard_class == "Medium"
assert hz.classify_nd(roads=[R("1", 800, "")], method=L1).hazard_class == "Medium"
assert hz.classify_nd(roads=[R("1", 2500, "county")], method=L1).hazard_class == "High"
assert hz.classify_nd(roads=[R("1", None, "I 94")], method=L1).hazard_class == "High"
c = hz.classify_nd(roads=[R("17", NAN, "county road")], method=L1)
assert c.hazard_class == "Medium" and c.data_gaps == ["road 17: AADT not given; classified Medium by assumption"]
c = hz.classify_nd(roads=[R("17", None, "State Highway 6")], method=L1, unknown_aadt="Low")
assert c.hazard_class == "Low", "a state highway without AADT must not be promoted by its name"
T = lambda i, trains, pax: {"id": i, "depth_m": 0.1, "trains_per_day": trains, "passenger": pax}
assert hz.classify_nd(railroads=[T("1", 3, False)], method=L1).hazard_class == "Medium"
assert hz.classify_nd(railroads=[T("1", 12, False)], method=L1).hazard_class == "High"
c = hz.classify_nd(railroads=[T("9", NAN, False)], method=L1, unknown_trains="High")
assert c.hazard_class == "High" and "railroad 9: trains a day not given" in c.data_gaps[0]
c = hz.classify_nd(lifelines=[{"id": "sub", "depth_m": 0.2, "kind": "substation"}], method=L1)
assert c.hazard_class == "Medium" and c.governing[0].kind == "lifeline" and c.counts["lifelines_wet"] == 1
c = hz.classify_nd(downstream_dams=[hz.nd_downstream_dam("DWR-7", "High", 1.0)], method=L1)
assert c.hazard_class == "High" and c.governing[0].section == "5.3 cascading"
c = hz.classify_nd(downstream_dams=[hz.nd_downstream_dam("DWR-8", "", 1.0)], method=L1)
assert c.hazard_class == "Medium" and "not recognised" in c.data_gaps[0]
gaps = ["road {}: AADT not given; classified Medium by assumption".format(i) for i in range(40)]
t = hz.limit_text(gaps)
assert len(t) <= hz.FIELD_MAX and t.endswith("more)") and t.startswith("road 0:")
print("OK (roads, railroads, lifelines, downstream dams, data gaps)")

# ---- 6. the shipped chart file is signed off by one engineer (0.26.1); a file without a signing
# engineer gets nd_hazard's wording: the conservative method, not the 1D rule
shipped = hz.load_ready_charts()
assert shipped.ready and shipped.status == "digitized" and shipped.signer == "Nicholas Bendickson"
assert shipped.signed_on == "2026-10-12" and shipped.path.endswith("acer_tm11_charts.json")
unsigned = test_charts(); unsigned.digitized_by = unsigned.checked_by = unsigned.signed_off_by = ""
assert not unsigned.ready
msg = hz.charts_not_digitized_message(unsigned)
assert hz.METHOD_LABELS[nh.LETHALITY_ALL_WET] in msg and "not signed off" in msg
assert hz.METHOD_LABELS[nh.LETHALITY_1D] not in msg and "only allowed for a 1D model" in msg
assert "signed off by ''" in msg
one = test_charts(); one.checked_by = ""
assert one.ready and one.signer == "test"                     # one engineer is enough
try:
    hz.classify_nd([S("a", 4.0, 1.0)], charts=None)
    raise SystemExit("chart method without charts accepted")
except nh.ChartsNotDigitized:
    pass
assert hz.chart_text(CS) == "digitized " + CS.sha256[:12] and hz.chart_text(None) == "not used"
print("OK (ChartsNotDigitized)")

# ---- 7. comparison-only criteria still go through nd_hazard for the rest, and say so
c = hz.classify_nd([S("f", 4.1, 0.3)], [R("5", None, "")], method=hz.COMPARISON_FEINBERG)
assert c.hazard_class == "High" and c.governing[0].section == "comparison: feinberg"
assert c.settings["lethality"] == "feinberg" and "comparison only" in c.notes[0]
assert c.data_gaps and c.counts["habitable_lethal"] == 1
assert hz.classify_nd([S("f", 3.0, 0.3)], method=hz.COMPARISON_FEINBERG).hazard_class == "Medium"
print("OK (comparison criteria)")

# ---- 8. tool 8 end to end through the QGIS test doubles
cell = 10.0
gt = (0.0, cell, 0.0, 100.0, 0.0, -cell)
depth = np.zeros((10, 10)); vel = np.zeros((10, 10))
xy = lambda r, c: (gt[0] + (c + 0.5) * cell, gt[3] - (r + 0.5) * cell)
depth[2, 2], vel[2, 2] = 4.0 * FT, 1.0 * FT          # house in the TEST judgment zone
depth[2, 5], vel[2, 5] = 1.0 * FT, 1.0 * FT          # house, low danger
depth[2, 7], vel[2, 7] = 3.0, 3.0                    # a barn: uninhabited
depth[4, 4] = 0.2                                    # lifeline (substation)
depth[6, :] = 0.4                                    # road with a NaN AADT, overtopped
depth[8, 8] = 0.5                                    # another dam's point (DWR-7, High)
dtif, vtif = os.path.join(work, "d.tif"), os.path.join(work, "v.tif")
cm.write_tif(dtif, depth, gt); cm.write_tif(vtif, vel, gt)
Dd, Sx = "double", "string"
pt = lambda r, c: QgsGeometry.fromPointXY(QgsPointXY(*xy(r, c)))
SIMF = [("dam_id", Sx), ("sim_status", Sx), ("depth_tif", Sx), ("vel_tif", Sx), ("haz_existing", Sx),
        ("tri_reason", Sx)]
sim = make_layer(SIMF,
                 [(pt(0, 0), {"dam_id": "DWR-1", "sim_status": "ok", "depth_tif": dtif, "vel_tif": vtif,
                              "haz_existing": "Significant"}),
                  (pt(0, 1), {"dam_id": "DWR-9", "sim_status": "triage", "tri_reason": "nothing inside",
                              "haz_existing": "Low"}),
                  (pt(8, 8), {"dam_id": "DWR-7", "sim_status": "FAILED", "haz_existing": "High"})],
                 "point", "sim")
mp = lambda *rc: QgsGeometry.fromMultiPointXY([QgsPointXY(*xy(r, c)) for r, c in rc])
bld = make_layer([("OCC_CLS", Sx), ("PRIM_OCC", Sx)],
                 [(mp((2, 2), (9, 9)), {"OCC_CLS": "Heavy", "PRIM_OCC": None}),       # MultiPoint
                  (mp((2, 5)), {"OCC_CLS": "Temporary Lodging", "PRIM_OCC": ""}),
                  (mp((2, 7)), {"OCC_CLS": "Agriculture", "PRIM_OCC": "Barn"}),
                  (mp((8, 1)), {"OCC_CLS": "Residential", "PRIM_OCC": "Duplex"})], "point", "bld")
line = lambda pts: QgsGeometry.fromPolylineXY([QgsPointXY(*p) for p in pts])
roads = make_layer([("aadt", Dd), ("road_class", Sx)],
                   [(line([xy(6, 0), xy(6, 9)]), {"aadt": NAN, "road_class": "County 12"})], "line", "roads")
life = make_layer([("kind", Sx)], [(pt(4, 4), {"kind": "substation"})], "point", "life")
ctx, fb = QgsProcessingContext(), QgsProcessingFeedback()
SIM, BLD, ROADS, LIFE = ctx.add(sim), ctx.add(bld), ctx.add(roads), ctx.add(life)


def tool8(**kw):
    r = HazardClassification().run_mock(dict(dict(SIM=SIM, BLD=BLD, ROADS=ROADS), **kw), ctx, fb)
    out = ctx.layers[r["OUT"]]
    n = out.fields().names()
    return {f["dam_id"]: {k: f[k] for k in n} for f in out.getFeatures()}, list(ctx.layers[r["IMP"]].getFeatures())


rows, imp = tool8(CRIT=0, CHART_FILE=chart_file, LIFELINES=LIFE)
d1 = rows["DWR-1"]
print("tool 8 (TEST charts):", {k: d1[k] for k in ("hz_class", "hz_border", "hz_gaps", "hz_chart", "hz_reason")})
assert d1["hz_class"] == "High" and d1["hz_border"] == "no"        # a High downstream dam governs too
assert d1["hz_criterion"] == nh.LETHALITY_CHARTS and d1["hz_basis"] == hz.METHOD_LABELS[nh.LETHALITY_CHARTS]
assert d1["hz_chart"] == "digitized " + CS.sha256[:12]
assert "AADT not given; classified Medium by assumption" in d1["hz_gaps"]      # NaN AADT: not Low
assert d1["n_bldg_wet"] == 2 and d1["n_bldg_lethal"] == 0 and d1["n_road_wet"] == 1   # judgment: Medium
assert "5.3 cascading" in d1["hz_reason"] and "DWR-7" in d1["hz_reason"]
assert d1["hz_vs_existing"] == "higher than existing"
assert rows["DWR-9"]["hz_class"] == "Low" and rows["DWR-9"]["hz_conf"] == "triage"
assert rows["DWR-7"]["hz_class"] == "NoData"
lev = sorted((f["kind"], f["lethal"], f["level"]) for f in imp)
assert lev == [("dam", "n/a", "High"), ("lifeline", "n/a", "Medium"), ("road", "n/a", "Medium"),
               ("structure", "no", "Medium"), ("structure", "no (judgment zone, MEDIUM by rule)", "Medium"),
               ("uninhabited building", "n/a", "Low")], lev
# without the other dam's class the downstream dam is Medium + a data gap; lifeline raises to Medium
sim2 = make_layer(SIMF, [(pt(0, 0), {"dam_id": "DWR-1", "sim_status": "ok", "depth_tif": dtif, "vel_tif": vtif}),
                         (pt(8, 8), {"dam_id": "DWR-7", "sim_status": "FAILED"})], "point", "sim2")
rows, _ = tool8(SIM=ctx.add(sim2), BLD=None, ROADS=None, CRIT=2)
assert rows["DWR-1"]["hz_class"] == "Medium" and "DWR-7: class '' not recognised" in rows["DWR-1"]["hz_gaps"]
sim3 = make_layer(SIMF, [(pt(0, 0), {"dam_id": "DWR-1", "sim_status": "ok", "depth_tif": dtif, "vel_tif": vtif})],
                  "point", "sim3")
rows, _ = tool8(SIM=ctx.add(sim3), BLD=None, ROADS=None, LIFELINES=LIFE, CRIT=2)
assert rows["DWR-1"]["hz_class"] == "Medium" and "lifeline" in rows["DWR-1"]["hz_reason"]
rows, _ = tool8(SIM=ctx.add(sim3), BLD=None, ROADS=None, CRIT=2)
assert rows["DWR-1"]["hz_class"] == "Low"
# the 1D rule: allowed only for 1D models -> a warning and a note
n_err = len(fb.errors)
rows, _ = tool8(SIM=SIM, CRIT=1)
assert rows["DWR-1"]["hz_class"] == "High" and rows["DWR-1"]["hz_chart"] == "not used"
assert any("only for a 1D model" in e for e in fb.errors[n_err:]) and "1D rule on a 2D result" in rows["DWR-1"]["hz_notes"]
# the default (charts) with the shipped file: signed off by one engineer, so it runs (0.26.1)
rows, _ = tool8()
assert rows["DWR-1"]["hz_criterion"] == nh.LETHALITY_CHARTS and rows["DWR-1"]["hz_chart"].startswith("digitized ")
# a chart file without a signing engineer -> clear error, conservative method named, not the 1D rule
unsigned_file = nh.save_charts(os.path.join(work, "TEST_charts_unsigned.json"), unsigned)
try:
    tool8(CHART_FILE=unsigned_file)
    raise SystemExit("tool 8 ran the chart method with an unsigned chart file")
except QgsProcessingException as e:
    assert hz.METHOD_LABELS[nh.LETHALITY_ALL_WET] in str(e) and hz.METHOD_LABELS[nh.LETHALITY_1D] not in str(e)
    print("tool 8 unsigned charts:", str(e)[:120], "...")
# wet depth must be > 0
try:
    tool8(CRIT=2, MIND=0.0)
    raise SystemExit("MIND 0 accepted")
except QgsProcessingException:
    pass
a8 = HazardClassification(); a8.initAlgorithm()
assert a8._params["MIND"].defaultValue == a8._params["RDEP"].defaultValue == 0.1 * 0.3048
# comparison criterion (FEMA / Feinberg)
rows, _ = tool8(CRIT=3)
assert rows["DWR-1"]["hz_criterion"] == "feinberg" and rows["DWR-1"]["hz_basis"].startswith("Comparison only")
shutil.rmtree(work)
print("OK")
