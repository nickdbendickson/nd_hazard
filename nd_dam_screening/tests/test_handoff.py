"""Tool 7b (hand-off to ND Dam Breach Runner): the pure mapping in core/handoff.py, the parameter
names checked against the runner's own algorithm definitions, and the tool itself with test
doubles of 'ndbreach:rundam' / 'ndbreach:ndhazardclass' behind the mocked processing.run.

Run: PYTHONPATH=tests/qgis_mock:<parent of the plugin> python3 tests/test_handoff.py
"""
import json, os, subprocess, sys, tempfile, shutil

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(HERE)
PARENT = os.path.dirname(PLUGIN)
sys.path.insert(0, os.path.join(HERE, "qgis_mock"))
sys.path.insert(0, PARENT)
from qgis.core import (QgsGeometry, QgsPointXY, QgsProcessingContext, QgsProcessingFeedback,  # noqa: E402
                       QgsProcessingException, make_layer)
from qgis.PyQt.QtCore import QVariant  # noqa: E402
from qgis import processing  # noqa: E402
from nd_dam_screening.core import handoff as ho, nd_hazard as nh  # noqa: E402
from nd_dam_screening.algorithms.runner_handoff import RunnerHandoff  # noqa: E402

work = tempfile.mkdtemp(prefix="hoff_")
FT, AF = 0.3048, 1233.4818375

# ---- 1. mapping function (pure)
attrs = {"dam_id": "DWR-5289", "name": "Test Dam", "height_m": 7.0, "td_vw_m3": 1543 * AF,
         "cal_total_m3": 1.0e6, "inv_max_m3": 2.0e6, "inv_area_m2": 5.0e5, "est_area_m2": 4.0e5}
p, notes = ho.rundam_params(attrs, (400123.4567, 1800456.789), "EPSG:5070", "dem.vrt", "/dams")
assert p == {"DAM_ID": "DWR-5289", "DAM_NAME": "Test Dam", "DAM_POINT": "400123.457,1800456.789 [EPSG:5070]",
             "HEIGHT_FT": round(7.0 / FT, 3), "STORAGE_ACFT": 1543.0, "DEM": "dem.vrt",
             "DAMS_FOLDER": "/dams"}, p
assert "TOP_FT" not in p and "AREA_AC" not in p                         # no DWR elevations, no pool area
assert any("td_vw_m3" in n for n in notes) and any("normal-pool areas are not passed" in n for n in notes)
p, _ = ho.rundam_params(dict(attrs, td_vw_m3=None), (0, 0), "EPSG:5070", "d", "f", cell_m=5.0,
                        user_csv="/x/inputs/breach_hydrograph.csv", area_top_m2=10 * 4046.8564224)
assert abs(p["STORAGE_ACFT"] - 1.0e6 / AF) < 1e-3 and p["CELL"] == 5.0 and p["AREA_AC"] == 10.0
assert p["METHOD"] == ho.USER_METHOD_INDEX and p["USER_CSV"].endswith("breach_hydrograph.csv")
p, n2 = ho.rundam_params({"dam_id": "X", "td_hb_m": 3.048, "inv_max_m3": 100 * AF}, (0, 0), "EPSG:5070", "d", "f")
assert p["HEIGHT_FT"] == 10.0 and p["STORAGE_ACFT"] == 100.0 and "td_hb_m" in n2[0]
for bad in ({"dam_id": "X", "td_vw_m3": 5.0}, {"dam_id": "X", "height_m": 3.0}, {"height_m": 3, "td_vw_m3": 9e3}):
    try:
        ho.rundam_params(bad, (0, 0), "EPSG:5070", "d", "f")
        raise SystemExit("accepted " + str(bad))
    except ho.HandoffError:
        pass
assert set(p) <= set(ho.RUNDAM_KEYS)
hp = ho.ndhazard_params("/dams/X", {"STRUCTURES": "bld", "ROADS": None, "DOWNSTREAM_DAMS": "dams"},
                        {"S_OCC": "OCC_CLS", "R_AADT": "", "D_ID": "dam_id", "D_CLASS": "haz_existing"},
                        1, 1, "", "A, B", 2, 0.03048, "barn, shed")
assert hp == {"DAM_FOLDER": "/dams/X", "LETHALITY": 1, "STRUCTURES": "bld", "S_OCC": "OCC_CLS",
              "DOWNSTREAM_DAMS": "dams", "D_ID": "dam_id", "D_CLASS": "haz_existing", "UNKNOWN_AADT": 1,
              "UNKNOWN_TRAINS": 2, "WET_FT": 0.1, "UNINHABITED_WORDS": "barn, shed",
              "PASSENGER_CODES": "A, B"}, hp
assert set(hp) <= set(ho.NDHAZARD_KEYS)
assert ho.safe_name("DWR-5289") == "DWR-5289" and ho.safe_name("a/b:c") == "a_b_c"
# tool-5 hydrograph (time_s, discharge_m3s) -> runner CSV (time hr, discharge cfs)
src = os.path.join(work, "hydro_X_td.csv")
open(src, "w").write("time_s,discharge_m3s\n0.0,0.000\n1800.0,10.000\n3600.0,2.000\n")
dst, peak = ho.write_runner_hydrograph(src, os.path.join(work, "X", ho.RUNNER_HYDRO_CSV))
rows = [l.strip().split(",") for l in open(dst)]
assert rows[0] == ["time_hr", "discharge_cfs"] and rows[2] == ["0.50000", "353.147"] and rows[3][0] == "1.00000"
assert abs(peak - 353.1467) < 1e-3
# surface area near the top from a tool-3 curve: V = 5000 m2 x (e - 100) -> 5000 m2
cv = os.path.join(work, "curve_X.csv")
open(cv, "w").write("elev_m,storage_m3\n" + "".join("{:.1f},{:.1f}\n".format(100 + k, 5000.0 * k) for k in range(6)))
assert abs(ho.area_at_top_m2(cv, 104.0) - 5000.0) < 1e-6 and ho.area_at_top_m2(os.path.join(work, "no.csv")) is None
# selection and agreement
A = lambda cls, border="no", vs="agree": {"hz_class": cls, "hz_border": border, "hz_vs_existing": vs}
assert ho.selected(A("High"), ho.SELECT_HIGH_MEDIUM)[0] and ho.selected(A("Medium"), ho.SELECT_HIGH_MEDIUM)[0]
assert not ho.selected(A("Low"), ho.SELECT_HIGH_MEDIUM)[0]
for unknown in ("NoData", "", None):                     # failed simulation = unknown class = send
    ok, why = ho.selected(A(unknown), ho.SELECT_HIGH_MEDIUM)
    assert ok and "no screening class" in why, (unknown, why)
assert ho.selected(A("Low"), ho.SELECT_HIGH_MEDIUM)[1] == "screening class Low"
assert ho.selected(A("High"), ho.SELECT_HIGH_MEDIUM)[1] == "screening class High"
assert ho.selected(A("High"), ho.SELECT_HIGH)[0] and not ho.selected(A("Medium"), ho.SELECT_HIGH)[0]
assert ho.selected(A("Low", "yes"), ho.SELECT_CHANGED)[0] and ho.selected(A("Low", vs="lower than existing"), ho.SELECT_CHANGED)[0]
assert not ho.selected(A("High"), ho.SELECT_CHANGED)[0]
assert ho.selected({}, ho.SELECT_ALL)[0] and ho.selected({}, ho.SELECT_SELECTED, True)[0]
assert not ho.selected({}, ho.SELECT_SELECTED, False)[0]
assert ho.agreement("High", "Medium") == "runner higher" and ho.agreement("Low", "Medium") == "runner lower"
assert ho.agreement("Medium", "Medium") == "agree" and ho.agreement("High", None) == "n/a"
print("OK (mapping)")

# ---- 2. parameter names against the runner's own definitions (its fake QGIS, own process)
runner = os.path.join(PARENT, "nd_breach_runner")
if os.path.exists(os.path.join(runner, "algorithms.py")) and os.path.exists(os.path.join(runner, "tests", "fake_qgis.py")):
    code = r'''
import json
from nd_breach_runner.tests import fake_qgis; fake_qgis.install()
from nd_breach_runner import algorithms as A
from nd_breach_runner.algorithms_ndhazard import NDHazardAlgorithm
out = {}
for cls in (A.RunDamAlgorithm, NDHazardAlgorithm):
    a = cls(); a.initAlgorithm()
    out[a.name()] = {p.name(): [type(p).__name__, [str(x) for x in (p.args[0] if p.args and isinstance(p.args[0], (list, tuple)) else [])]]
                     for p in a.parameterDefinitions()}
print(json.dumps(out))
'''
    env = dict(os.environ, PYTHONPATH=PARENT, PYTHONDONTWRITEBYTECODE="1")   # never write into the runner
    res = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=300,
                         cwd=work)
    assert res.returncode == 0, res.stderr[-2000:]
    defs = json.loads(res.stdout.strip().splitlines()[-1])
    rd, hd = defs["rundam"], defs["ndhazardclass"]
    missing = [k for k in ho.RUNDAM_KEYS if k not in rd] + [k for k in ho.NDHAZARD_KEYS if k not in hd]
    assert not missing, "parameter names not in the runner: {}".format(missing)
    types = {"DAM_ID": "String", "DAM_POINT": "Point", "HEIGHT_FT": "Number", "STORAGE_ACFT": "Number",
             "AREA_AC": "Number", "DEM": "RasterLayer", "DAMS_FOLDER": "File", "CELL": "Number",
             "METHOD": "Enum", "USER_CSV": "File", "RECEPTOR_POINTS": "FeatureSource"}
    for k, t in types.items():
        assert rd[k][0] == "QgsProcessingParameter" + t, (k, rd[k][0])
    assert "own hydrograph" in rd["METHOD"][1][ho.USER_METHOD_INDEX].lower(), rd["METHOD"][1]
    assert hd["LETHALITY"][1] == [nh.LETHALITY_LABELS[m] for m in nh.LETHALITY_METHODS]
    assert hd["UNKNOWN_AADT"][1] == list(nh.CLASSES)
    assert hd["DAM_FOLDER"][0] == "QgsProcessingParameterFile"
    assert "HABITABLE_CLASSES" not in hd and hd["UNINHABITED_WORDS"][0] == "QgsProcessingParameterString"
    assert hd["DOWNSTREAM_DAMS"][0] == "QgsProcessingParameterFeatureSource"
    assert hd["WET_FT"][0] == "QgsProcessingParameterNumber" and hd["UNKNOWN_TRAINS"][1] == list(nh.CLASSES)
    for k in ("D_ID", "D_CLASS", "R_SOLE", "L_KIND"):
        assert hd[k][0] == "QgsProcessingParameterField", k
    print("OK (all {} rundam and {} ndhazardclass parameter names verified by importing the runner's "
          "algorithms under its fake QGIS)".format(len(ho.RUNDAM_KEYS), len(ho.NDHAZARD_KEYS)))
else:
    print("SKIP runner parameter check: no runner at", runner)

# ---- 3. tool 7b with test doubles of the runner
RUNNER_METHODS = ["Piping failure, Froehlich (2008) parameters, routed"] + ["m%d" % i for i in range(1, 9)] + \
                 ["My own hydrograph (CSV: time hr, discharge cfs)"]
RUNNER_CLASS = {"DWR-1": ("High", True, []), "DWR-5": ("Medium", False, ["road 3: AADT not given; "
                                                                         "classified Medium by assumption"])}


class _Def:
    def __init__(self, opts): self._o = opts
    def options(self): return self._o


class FakeRunDam:
    fail = {"DWR-2"}
    cancel_after = None
    @classmethod
    def createInstance(cls): return cls()
    def parameterDefinition(self, name): return _Def(RUNNER_METHODS) if name == "METHOD" else None
    def run_mock(self, p, context, feedback):
        for k in p:
            assert k in ho.RUNDAM_KEYS, k
        if p["DAM_ID"] in self.fail:
            raise QgsProcessingException("LISFLOOD-FP stopped (test double)")
        folder = os.path.join(p["DAMS_FOLDER"], ho.safe_name(p["DAM_ID"]))
        os.makedirs(folder, exist_ok=True)
        if FakeRunDam.cancel_after == p["DAM_ID"]:
            feedback.parent._cancel = True
        return {"FOLDER": folder}


class FakeHazard:
    @classmethod
    def createInstance(cls): return cls()
    def run_mock(self, p, context, feedback):
        for k in p:
            assert k in ho.NDHAZARD_KEYS, k
        dam = os.path.basename(p["DAM_FOLDER"])
        cls, border, gaps = RUNNER_CLASS.get(dam, ("Low", False, []))
        js = os.path.join(p["DAM_FOLDER"], ho.RUNNER_JSON)
        json.dump({"hazard_class": cls, "borderline_raised": border, "data_gaps": gaps,
                   "lethality_label": "test"}, open(js, "w"))
        return {"CLASS": cls, "JSON": js}


def install_runner():
    processing.REGISTRY.update({ho.RUNNER_ALG: FakeRunDam, ho.HAZARD_ALG: FakeHazard})


def remove_runner():
    for k in (ho.RUNNER_ALG, ho.HAZARD_ALG):
        processing.REGISTRY.pop(k, None)


D, S = "double", "string"
pt = lambda x, y: QgsGeometry.fromPointXY(QgsPointXY(x, y))
hyd = os.path.join(work, "hydro_DWR-5_td.csv")
open(hyd, "w").write("time_s,discharge_m3s\n0,0\n600,50\n1200,5\n")
F = [("dam_id", S), ("name", S), ("height_m", D), ("td_vw_m3", D), ("td_csv", S), ("hz_class", S),
     ("hz_border", S), ("hz_vs_existing", S), ("haz_existing", S)]
dam_rows = [("DWR-1", 7.0, "High"), ("DWR-2", 5.0, "Medium"), ("DWR-3", 2.0, "Low"),
            ("DWR-4", 4.0, "Medium"), ("DWR-5", 6.0, "Medium"), ("DWR-6", 3.0, "NoData")]
dams = make_layer(F, [(pt(1000 * i, 0), {"dam_id": d, "name": d + " dam", "height_m": h, "td_vw_m3": 500 * AF,
                                          "td_csv": hyd if d == "DWR-5" else None, "hz_class": c,
                                          "hz_border": "no", "hz_vs_existing": "agree"})
                   for i, (d, h, c) in enumerate(dam_rows)], "point", "dams")
bpts = make_layer([("dam_id", S), ("flow_az", D)],
                  [(pt(1000 * i + 11, 22), {"dam_id": d, "flow_az": 180.0})
                   for i, (d, _, _) in enumerate(dam_rows) if d != "DWR-4"], "point", "bpts")
bld = make_layer([("OCC_CLS", S)], [(pt(5, 5), {"OCC_CLS": "Residential"})], "point", "bld")
ctx = QgsProcessingContext()
DAMS, BPTS, BLD = ctx.add(dams), ctx.add(bpts), ctx.add(bld)
folder = os.path.join(work, "Dams")


def run7b(fb=None, **kw):
    fb = fb or QgsProcessingFeedback()
    base = dict(DAMS=DAMS, BPTS=BPTS, DEM="nd_dem.vrt", DAMS_FOLDER=folder, LETHALITY=1)
    r = RunnerHandoff().run_mock(dict(base, **kw), ctx, fb)
    out = ctx.layers[r["OUT"]]
    n = out.fields().names()
    return {f["dam_id"]: {k: f[k] for k in n} for f in out.getFeatures()}, fb


install_runner()
del processing.CALLS[:]
res, fb = run7b(BLD=BLD, S_OCC="OCC_CLS", R_SOLE="sole")
for d, r in res.items():
    print("7b", d, r["rn_status"], r["rn_class"], r["scr_class"], r["rn_agree"], "|", r["rn_inputs"])
assert res["DWR-1"]["rn_status"] == "done" and res["DWR-1"]["rn_class"] == "High"
assert res["DWR-1"]["rn_agree"] == "agree" and res["DWR-1"]["rn_border"] == "yes"
assert res["DWR-1"]["rn_folder"] == os.path.join(folder, "DWR-1")
assert res["DWR-2"]["rn_status"].startswith("failed: LISFLOOD-FP stopped")          # batch went on
assert res["DWR-3"]["rn_status"] == "skipped: screening class Low" and res["DWR-3"]["rn_class"] is None
assert res["DWR-4"]["rn_status"] == "failed: no breach point (tool 4) for this dam_id"
assert res["DWR-5"]["rn_status"] == "done" and res["DWR-5"]["rn_agree"] == "agree"
assert "AADT not given" in res["DWR-5"]["rn_gaps"] and res["DWR-5"]["rn_border"] == "no"
assert any("dam DWR-2" in e for e in fb.errors) and any("dam DWR-4" in e for e in fb.errors)
runs = [(a, p) for a, p in processing.CALLS if a == ho.RUNNER_ALG]
haz = [(a, p) for a, p in processing.CALLS if a == ho.HAZARD_ALG]
assert [p["DAM_ID"] for _, p in runs] == ["DWR-1", "DWR-2", "DWR-5", "DWR-6"] and len(haz) == 3
assert res["DWR-6"]["rn_status"] == "done" and res["DWR-6"]["scr_class"] == "NoData"
assert res["DWR-6"]["rn_agree"] == "n/a" and res["DWR-6"]["rn_inputs"].startswith("sent: no screening class")
p1 = runs[0][1]
assert p1["DAM_POINT"] == "11.000,22.000 [EPSG:5070]"                                # the breach point
assert p1["HEIGHT_FT"] == round(7.0 / FT, 3) and p1["STORAGE_ACFT"] == 500.0 and "METHOD" not in p1
assert p1["RECEPTOR_POINTS"] == BLD and "TOP_FT" not in p1 and p1["DEM"] == "nd_dem.vrt"
assert haz[0][1] == {"DAM_FOLDER": os.path.join(folder, "DWR-1"), "LETHALITY": 1, "STRUCTURES": BLD,
                     "S_OCC": "OCC_CLS", "R_SOLE": "sole", "DOWNSTREAM_DAMS": DAMS, "D_ID": "dam_id",
                     "D_CLASS": "haz_existing", "UNKNOWN_AADT": 1, "UNKNOWN_TRAINS": 1, "WET_FT": 0.1,
                     "UNINHABITED_WORDS": ", ".join(nh.UNINHABITED_KEYWORDS),
                     "PASSENGER_CODES": "A, B, C"}, haz[0][1]
print("OK (7b: done / failed / skipped, the batch continues)")

# screening hydrograph as the runner's own-hydrograph CSV; a stricter runner class
RUNNER_CLASS["DWR-5"] = ("High", False, [])
del processing.CALLS[:]
res, _ = run7b(WHICH=ho.SELECT_ALL, HYDRO=ho.HYDRO_SCREENING, CELL=5.0)
p5 = [p for a, p in processing.CALLS if a == ho.RUNNER_ALG and p["DAM_ID"] == "DWR-5"][0]
assert p5["METHOD"] == 9 and p5["CELL"] == 5.0
assert p5["USER_CSV"] == os.path.join(folder, "DWR-5", "inputs", "breach_hydrograph.csv")
assert open(p5["USER_CSV"]).read().splitlines()[2] == "0.16667,1765.733"
assert res["DWR-5"]["rn_agree"] == "runner higher" and res["DWR-5"]["scr_class"] == "Medium"
assert res["DWR-1"]["rn_status"].startswith("failed: no tool-5 hydrograph")          # no td_csv
assert res["DWR-3"]["rn_status"].startswith("failed: no tool-5")   # 'all' attempts the Low dam too
print("OK (7b: screening hydrograph, all dams)")

# cancel: the run of DWR-1 cancels; the rest are skipped, not run
FakeRunDam.cancel_after = "DWR-1"
del processing.CALLS[:]
res, _ = run7b()
FakeRunDam.cancel_after = None
assert res["DWR-1"]["rn_status"].startswith("failed: cancelled")
assert [res[d]["rn_status"] for d in ("DWR-2", "DWR-4", "DWR-5", "DWR-6")] == ["skipped: cancelled"] * 4
assert res["DWR-3"]["rn_status"] == "skipped: screening class Low"
assert [p["DAM_ID"] for a, p in processing.CALLS if a == ho.RUNNER_ALG] == ["DWR-1"]
assert not [1 for a, _ in processing.CALLS if a == ho.HAZARD_ALG]
print("OK (7b: cancel)")

# selected features only
dams.selectByIds([f.id() for f in dams.getFeatures() if f["dam_id"] == "DWR-3"])
res, _ = run7b(WHICH=ho.SELECT_SELECTED)
assert res["DWR-3"]["rn_status"] == "done" and res["DWR-1"]["rn_status"] == "skipped: not selected"

# modes that need tool 8 on a tool-5/6/7 layer -> clear error
plain = make_layer([("dam_id", S), ("height_m", D), ("td_vw_m3", D)],
                   [(pt(0, 0), {"dam_id": "DWR-1", "height_m": 7.0, "td_vw_m3": 1e5})], "point", "t7")
try:
    run7b(DAMS=ctx.add(plain))
    raise SystemExit("mode 'High and Medium' accepted a layer without hz_class")
except QgsProcessingException as e:
    assert "hz_class" in str(e)
res, _ = run7b(DAMS=ctx.add(plain), WHICH=ho.SELECT_ALL)
assert res["DWR-1"]["rn_status"] == "done" and res["DWR-1"]["scr_class"] is None and res["DWR-1"]["rn_agree"] == "n/a"

# charts method with the shipped (not digitized) chart file -> stops before any run
del processing.CALLS[:]
try:
    run7b(LETHALITY=0)
    raise SystemExit("7b ran the chart method without digitized charts")
except QgsProcessingException as e:
    assert nh.LETHALITY_LABELS[nh.LETHALITY_ALL_WET] in str(e) and nh.LETHALITY_LABELS[nh.LETHALITY_1D] not in str(e)
assert not processing.CALLS

# 0.16.0: no DEM given - each dam's DEM from its dam folder (dam.json data section); the DEM a run
# was given is recorded in the data section where it has none
from nd_dam_screening.damsuite import dam_record as DR_, damdata as DD_  # noqa: E402
dem1 = os.path.join(work, "dwr1_dem.vrt")
open(dem1, "w").write("<VRTDataset/>")
r1 = DR_.load(os.path.join(folder, "DWR-1"), create=True)
DD_.set_entry(r1, "dem", dem1, "test")
r1.save()
del processing.CALLS[:]
res, fb0 = run7b(DEM="", WHICH=ho.SELECT_ALL)
sent = {p["DAM_ID"]: p["DEM"] for a, p in processing.CALLS if a == ho.RUNNER_ALG}
assert sent.get("DWR-1") == dem1, sent
assert res["DWR-3"]["rn_status"].startswith("failed: no DEM")                       # no folder DEM
statewide = os.path.join(work, "statewide.vrt")
open(statewide, "w").write("<VRTDataset/>")
run7b(DEM=statewide, WHICH=ho.SELECT_ALL)
assert DR_.load(os.path.join(folder, "DWR-1")).extra["data"]["dem"]["path"] == dem1    # kept
assert DR_.load(os.path.join(folder, "DWR-5")).extra["data"]["dem"]["path"] == statewide
empty = os.path.join(work, "EmptyDams")
os.makedirs(empty)
try:
    run7b(DEM="", DAMS_FOLDER=empty, WHICH=ho.SELECT_ALL)
    raise SystemExit("7b ran without any DEM")
except QgsProcessingException as e:
    assert "Get data for this dam" in str(e)
print("OK (7b: DEM from the dam folders, data paths in dam.json)")

# runner not installed
remove_runner()
try:
    run7b()
    raise SystemExit("ran without the runner")
except QgsProcessingException as e:
    assert "ND Dam Breach Runner v3.1" in str(e) and "ndbreach:rundam" in str(e)
    print("provider missing:", str(e)[:100], "...")
shutil.rmtree(work)
print("OK")
