"""v0.9 (ND dam suite): the data package from ND Dam Hazard in tool 0, dam.json written by tool 7b
(damsuite.dam_record), the suite version checks (damsuite.versions) in plugin.py and tool 7b.

Run: PYTHONPATH=tests/qgis_mock:<parent of the plugin> python3 tests/test_suite09.py
The ND Dam Hazard part (a real export by ND Dam Hazard's suiteexport.py under its own test harness,
in a separate process) runs only when that plugin and its harness are found.
"""
import json, os, shutil, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(HERE)
PARENT = os.path.dirname(PLUGIN)
sys.path.insert(0, os.path.join(HERE, "qgis_mock"))
sys.path.insert(0, PARENT)
sys.path.insert(0, HERE)
from qgis.core import (QgsGeometry, QgsPointXY, QgsProcessingContext, QgsProcessingFeedback,  # noqa: E402
                       QgsProcessingException, QgsVectorFileWriter, QgsMessageLog, make_layer)
from qgis.PyQt.QtCore import QVariant  # noqa: E402
from qgis import processing, utils as qutils  # noqa: E402
from nd_dam_screening.algorithms.data_package import DataPackage  # noqa: E402
from nd_dam_screening.algorithms.runner_handoff import RunnerHandoff  # noqa: E402
from nd_dam_screening.core import damrecord_io as dri, handoff as ho, package as pk  # noqa: E402
from nd_dam_screening.damsuite import dam_record as dr  # noqa: E402
from nd_dam_screening.plugin import NDDamScreeningPlugin  # noqa: E402
import _gpkg  # noqa: E402

work = tempfile.mkdtemp(prefix="suite09_")
D, S, I = "double", "string", "int"
pt = lambda x, y: QgsGeometry.fromPointXY(QgsPointXY(x, y))
ND = "/home/claude/ND_Dam_Hazard_Helper"
HARNESS = "/home/claude/harness"

# ---------------------------------------------------------------- 1. dam record helpers (pure)
lon, lat = dri.lonlat_from_5070(-343705.1336915329, 2675829.785565597)   # (-100.5 W, 47 N) forward
assert abs(lon + 100.5) < 1e-9 and abs(lat - 47.0) < 1e-9, (lon, lat)
idn = dri.identity({"dam_id": "DWR-5289", "name": "Test Dam", "nid_id": "nd00123", "haz_existing": "Significant"})
assert idn == {"dam_id": "DWR-5289", "name": "Test Dam", "nid_id": "ND00123", "dwr_structure_id": "5289",
               "existing_hazard_class": "Significant"}, idn
assert dri.identity({"dam_id": "NID-ND00999", "hazard_cla": "Low"}) == {
    "dam_id": "NID-ND00999", "nid_id": "ND00999", "existing_hazard_class": "Low"}
assert dri.plugin_version() == "0.20.0"
assert dri.screening_result({"tri_result": "clear", "hz_class": "Low"}, "tool 8")["triage"] == "clear"
assert dri.screening_result({}, "tool 7b") == {"triage": "not run", "hz_class": None, "border": None,
                                              "gaps": None, "by": "tool 7b"}
print("OK (dam record helpers)")

# ---------------------------------------------------------------- 2. tool 0 with an ND Dam Hazard package
MP = lambda x0, y0, w=10: ("MultiPolygon", [[[(x0, y0), (x0 + w, y0), (x0 + w, y0 + w), (x0, y0 + w), (x0, y0)]]])
LN = lambda y: ("MultiLineString", [[(0.0, y), (1000.0, y)]])
SF = [("habitable", "INTEGER"), ("mobile", "INTEGER"), ("occ_cls", "TEXT"), ("prim_occ", "TEXT"),
      ("nd_habitable", "TEXT")]
pkg_path = _gpkg.write(os.path.join(work, "nd_package.gpkg"), {
    "structures": ("MULTIPOLYGON", SF, [
        (MP(0, 0), {"habitable": 0, "mobile": 0, "occ_cls": "Agriculture", "prim_occ": "Barn", "nd_habitable": "No"}),
        (MP(50, 0), {"habitable": 1, "mobile": 0, "occ_cls": None, "prim_occ": "grain bin office",
                     "nd_habitable": "Unverified"}),            # ND's flag wins over the words rule
        (MP(100, 0), {"habitable": None, "mobile": None, "occ_cls": "Residential",
                      "prim_occ": "Manufactured Home", "nd_habitable": None})]),
    "roads": ("MULTILINESTRING", [("name", "TEXT"), ("aadt", "REAL"), ("road_class", "TEXT"), ("level", "TEXT")], [
        (LN(0), {"name": "Main St", "aadt": None, "road_class": "Local/County", "level": "Low"}),
        (LN(100), {"name": "US Hwy 2", "aadt": 4500.0, "road_class": "US Highway", "level": "High"}),
        (LN(200), {"name": "I-94", "aadt": None, "road_class": "Interstate", "level": "High"})]),
    "railroads": ("MULTILINESTRING", [("railroad", "TEXT"), ("passenger", "INTEGER"), ("trains_day", "REAL")], [
        (LN(300), {"railroad": "BNSF", "passenger": 1, "trains_day": 12.0}),
        (LN(400), {"railroad": "YY", "passenger": 0, "trains_day": None})]),
    "flowlines": ("MULTILINESTRING", [("gnis_name", "TEXT")], [(LN(500), {"gnis_name": "Spring Creek"})]),
    "lifelines": ("POINT", [("name", "TEXT"), ("category", "TEXT"), ("kind", "TEXT")],
                  [(("Point", (5.0, 5.0)), {"name": "Mercy", "category": "Hospital", "kind": "Hospital"})]),
    "dams": ("POINT", [("nid_id", "TEXT"), ("hazard", "TEXT")],
             [(("Point", (9.0, 9.0)), {"nid_id": "ND00123", "hazard": "High"})]),
}, info={"format": pk.ND_FORMAT, "created_by": "ND Dam Hazard 2.10.0 (suiteexport.py)",
         "counts": json.dumps({"structures": 3}),
         "missing": json.dumps(["waterbodies (NHD waterbodies: the pack holds flowlines only) - not in ND "
                                "Dam Hazard's pack"])})
assert set(pk.gpkg_layers(pkg_path)) == {"structures", "roads", "railroads", "flowlines", "lifelines", "dams",
                                         "suite_export_info"}
ctx = QgsProcessingContext()


def tool0(**kw):
    fb = QgsProcessingFeedback()
    QgsVectorFileWriter.written.clear()
    r = DataPackage().run_mock(dict(kw), ctx, fb)
    return {k[1]: v for k, v in QgsVectorFileWriter.written.items() if k[0] == r["OUT"]}, fb


w, fb = tool0(PACKAGE=pkg_path, OUT=os.path.join(work, "out.gpkg"))
print("tool 0 from the package:", {k: len(v.feats) for k, v in w.items()})
assert set(w) == {"structures", "structure_points", "roads", "railroads", "flowlines", "lifelines", "dams"}
sv = [f.attributes() for f in w["structures"].feats]
assert [a[:2] for a in sv] == [[0, 0], [1, 0], [1, 1]], sv          # ND flags kept; blank flag computed
assert [f.geometry().kind for f in w["structure_points"].feats] == ["point"] * 3
rv = [f.attributes() for f in w["roads"].feats]
assert rv == [[None, "Local/County", "Medium"], [4500.0, "US Highway", "High"], [None, "Interstate", "High"]], rv
tv = [f.attributes() for f in w["railroads"].feats]
assert tv == [[1, 12.0, "High"], [0, None, "Medium"]], tv
assert w["lifelines"].feats[0]["category"] == "Hospital" and w["dams"].feats[0]["nid_id"] == "ND00123"
assert any(m.startswith("Not in the package: waterbodies") for m in fb.log)
assert any("created_by: ND Dam Hazard 2.10.0" in m for m in fb.log)
assert any(m.startswith("Neither given nor in the package: waterbodies, dam_lines") for m in fb.log)
# an explicit layer wins over the package's
ROADS = make_layer([("AADT_2023", D), ("ROUTE", S)], [(QgsGeometry.fromPolylineXY(
    [QgsPointXY(0, 0), QgsPointXY(10, 0)]), {"AADT_2023": 800.0, "ROUTE": "County road"})], "line", "roads")
w, fb = tool0(PACKAGE=pkg_path, ROADS=ctx.add(ROADS), AADT="AADT_2023", RCLS="ROUTE",
              OUT=os.path.join(work, "out2.gpkg"))
assert [f.attributes() for f in w["roads"].feats] == [[800.0, "County road", "Medium"]]
assert any(m == "Given explicitly (the package's layer not used): roads" for m in fb.log)
assert len(w["structures"].feats) == 3
for kw in ({"PACKAGE": os.path.join(work, "none.gpkg"), "OUT": os.path.join(work, "x.gpkg")},   # no file
           {"PACKAGE": pkg_path, "OUT": pkg_path}):                                          # overwrite it
    try:
        tool0(**kw)
        raise SystemExit("accepted " + str(kw))
    except QgsProcessingException:
        pass
w, fb = tool0(OUT=os.path.join(work, "empty.gpkg"))                  # no package: works as before
assert w == {}
print("OK (tool 0 with a package)")

# ---------------------------------------------------------------- 3. a real ND Dam Hazard export
if os.path.exists(os.path.join(ND, "suiteexport.py")) and \
        os.path.exists(os.path.join(HARNESS, "tests", "test_v2100_suiteexport.py")):
    code = r'''
import json, os, sys
from ND_Dam_Hazard_Helper import suiteexport as SE
import test_v2100_suiteexport as T
out = sys.argv[1]
pack = T.make_pack(os.path.join(out, "pack"), T.dataset())
rep = SE.export(os.path.join(out, "nd_export.gpkg"), SE.open_source(pack),
                tuple(T.ll(-5000, -5000) + T.ll(5000, 6000)))
print(json.dumps({"path": rep["path"], "counts": rep["counts"]}))
'''
    keep = [p for p in os.environ.get("PYTHONPATH", "").split(os.pathsep) if p and "qgis_mock" not in p]
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1",
               PYTHONPATH=os.pathsep.join([os.path.join(HARNESS, "fakes"), os.path.dirname(ND),
                                           os.path.join(HARNESS, "tests")] + keep))
    res = subprocess.run([sys.executable, "-c", code, work], env=env, capture_output=True, text=True,
                         timeout=600, cwd=work)
    assert res.returncode == 0, res.stderr[-3000:]
    nd = json.loads(res.stdout.strip().splitlines()[-1])
    w, fb = tool0(PACKAGE=nd["path"], OUT=os.path.join(work, "from_nd.gpkg"))
    got = {k: len(v.feats) for k, v in w.items()}
    print("tool 0 from ND Dam Hazard's export:", got)
    assert got == {k: v for k, v in nd["counts"].items()}, (got, nd["counts"])
    st = {f["prim_occ"]: f.attributes()[:2] for f in w["structures"].feats if f["prim_occ"]}
    assert st["Barn"] == [0, 0] and st["Manufactured Home"] == [1, 1]
    roads = {f.attributes()[0]: f.attributes() for f in w["roads"].feats if f.attributes()[0] is not None}
    assert roads[800.0][2] == "Medium" and roads[4500.0][2] == "High"
    assert sum(1 for f in w["roads"].feats if f.attributes()[0] is None) == 2     # unknown AADT stays empty
    rails = [f.attributes() for f in w["railroads"].feats]
    assert [1, 12.0, "High"] in rails
    assert any(m.startswith("Not in the package: dam_lines") for m in fb.log)
    print("OK (ND Dam Hazard export -> tool 0)")
else:
    print("SKIP ND Dam Hazard export: plugin or harness not found")

# ---------------------------------------------------------------- 4. tool 7b writes dam.json
RUNNER_METHODS = ["m%d" % i for i in range(9)] + ["My own hydrograph (CSV: time hr, discharge cfs)"]


class _Def:
    def options(self): return RUNNER_METHODS


class FakeRunDam:
    @classmethod
    def createInstance(cls): return cls()
    def parameterDefinition(self, name): return _Def() if name == "METHOD" else None
    def run_mock(self, p, context, feedback):
        folder = os.path.join(p["DAMS_FOLDER"], ho.safe_name(p["DAM_ID"]))
        assert os.path.isfile(os.path.join(folder, "dam.json")) or p["DAM_ID"] == "DWR-3", \
            "dam.json must be written before the run"
        os.makedirs(folder, exist_ok=True)
        return {"FOLDER": folder}


class FakeHazard:
    @classmethod
    def createInstance(cls): return cls()
    def run_mock(self, p, context, feedback):
        js = os.path.join(p["DAM_FOLDER"], ho.RUNNER_JSON)
        json.dump({"hazard_class": "High", "borderline_raised": False, "data_gaps": []}, open(js, "w"))
        return {"JSON": js}


processing.REGISTRY.update({ho.RUNNER_ALG: FakeRunDam, ho.HAZARD_ALG: FakeHazard})
AF = 1233.4818375
F = [("dam_id", S), ("name", S), ("nid_id", S), ("height_m", D), ("td_vw_m3", D), ("hz_class", S),
     ("hz_border", S), ("hz_gaps", S), ("hz_vs_existing", S), ("haz_existing", S), ("tri_result", S),
     ("tri_reason", S)]
rows = [("DWR-1", "High", "needs 2D", "Significant", "ND00001"), ("DWR-2", "Medium", "needs 2D", "Low", None),
        ("DWR-3", "Medium", "needs 2D", None, None), ("DWR-4", "Low", "clear", "Low", None)]
dams = make_layer(F, [(pt(400000 + 1000 * i, 1800000), {
    "dam_id": d, "name": d + " dam", "nid_id": nid, "height_m": 6.096, "td_vw_m3": 500 * AF, "hz_class": c,
    "hz_border": "no", "hz_gaps": "road 3: AADT not given" if d == "DWR-1" else "", "hz_vs_existing": "agree",
    "haz_existing": ex, "tri_result": tri, "tri_reason": "x"}) for i, (d, c, tri, ex, nid) in enumerate(rows)],
    "point", "dams")
BX, BY = -343705.1336915329, 2675829.785565597
bpts = make_layer([("dam_id", S), ("flow_az", D)], [(pt(BX + 10 * i, BY), {"dam_id": d, "flow_az": 180.0})
                                                   for i, (d, *_r) in enumerate(rows)], "point", "bpts")
DAMS, BPTS = ctx.add(dams), ctx.add(bpts)
folder = os.path.join(work, "Dams")
# DWR-2: ND Dam Hazard already recorded another height; DWR-3: a broken dam.json
r2 = dr.load(os.path.join(folder, "DWR-2"), create=True)
r2.set("height_ft", 21.5, "ndhazard", "2.10.0")
r2.save()
os.makedirs(os.path.join(folder, "DWR-3"))
open(os.path.join(folder, "DWR-3", "dam.json"), "w").write("{broken")


def run7b(**kw):
    fb = QgsProcessingFeedback()
    base = dict(DAMS=DAMS, BPTS=BPTS, DEM="nd_dem.vrt", DAMS_FOLDER=folder, LETHALITY=1, WHICH=ho.SELECT_ALL)
    r = RunnerHandoff().run_mock(dict(base, **kw), ctx, fb)
    out = ctx.layers[r["OUT"]]
    return {f["dam_id"]: f["rn_status"] for f in out.getFeatures()}, fb


status, fb = run7b()
assert set(status.values()) == {"done"}, status
rec = dr.load(os.path.join(folder, "DWR-1"))
f1 = rec.fields
assert (f1["dam_id"], f1["dwr_structure_id"], f1["nid_id"], f1["name"]) == ("DWR-1", "1", "ND00001", "DWR-1 dam")
assert abs(f1["height_ft"] - 20.0) < 1e-6 and abs(f1["storage_acft"] - 500.0) < 1e-6
assert (f1["x"], f1["y"], f1["crs"]) == (round(BX, 3), round(BY, 3), "EPSG:5070")
assert abs(f1["lon"] + 100.5) < 1e-6 and abs(f1["lat"] - 47.0) < 1e-6
assert f1["existing_hazard_class"] == "Significant" and "surface_area_ac" not in f1
assert rec.sources["height_ft"]["tool"] == "ndscreen" and rec.sources["height_ft"]["version"] == "0.20.0"
st = rec.latest("screened")
assert st["tool"] == "ndscreen" and st["result"]["triage"] == "needs 2D" and st["result"]["hz_class"] == "High"
assert st["result"]["border"] == "no" and st["result"]["gaps"] == "road 3: AADT not given"
assert dr.next_action(rec)[0] == "model"
assert dr.next_action(dr.load(os.path.join(folder, "DWR-4")))[0] == "none"          # triage cleared it
r2 = dr.load(os.path.join(folder, "DWR-2"))
assert r2.fields["height_ft"] == 21.5 and r2.sources["height_ft"]["tool"] == "ndhazard"   # never overwritten
assert r2.conflicts and r2.conflicts[-1]["field"] == "height_ft" and r2.conflicts[-1]["applied"] is False
assert any("dam DWR-2: dam.json: height_ft kept at 21.5 (from ndhazard)" in m for m in fb.log), fb.log
assert open(os.path.join(folder, "DWR-3", "dam.json")).read() == "{broken"             # never replaced
assert any("dam DWR-3: dam.json not updated" in m for m in fb.log)
assert any(m.startswith("Suite: ND Dam Breach Runner not installed") for m in fb.log)
n_status = len(dr.load(os.path.join(folder, "DWR-1")).status)
run7b()
assert len(dr.load(os.path.join(folder, "DWR-1")).status) == n_status          # same result: not added again
print("OK (7b dam.json)")

# review fix 1: tool 8 after 7b -> Runner -> ND with the same screening result adds no status
# (results are compared on triage / hz_class / border / gaps, not on who wrote them)
f1dir = os.path.join(folder, "DWR-1")
rec = dr.load(f1dir)
rec.add_status("modelled", "ndbreach", "3.2.0", {"class": "High"}, when="2999-01-01T00:00:00")
rec.add_status("classified", "ndhazard", "2.10.0", {"class": "High"}, when="2999-01-02T00:00:00")
rec.save()
assert dr.next_action(dr.load(f1dir))[0] == "none"
a1 = {f.attributes()[0]: {n: f[n] for n in dams.fields().names()} for f in dams.getFeatures()}["DWR-1"]
n_before = len(dr.load(f1dir).status)
dri.write(f1dir, dri.tool8_values(a1, "haz_existing"), dri.screening_result(a1, "tool 8"), "0.9.0",
          create=False)
rec = dr.load(f1dir)
assert len(rec.status) == n_before and dr.next_action(rec)[0] == "none", rec.status
assert dri.same_substance({"triage": "clear", "gaps": "", "by": "tool 7b", "triage_reason": "a"},
                          {"triage": "clear", "gaps": None, "by": "tool 8", "triage_reason": "b"})
assert not dri.same_substance({"triage": "clear", "hz_class": "Low"}, {"triage": "clear", "hz_class": "Medium"})
changed = dict(a1, hz_class="Medium")                     # a new screening verdict IS recorded
dri.write(f1dir, {}, dri.screening_result(changed, "tool 8"), "0.9.0", create=False)
assert len(dr.load(f1dir).status) == n_before + 1

# review fix 2: tool 7b and tool 8 write the height with the same rounding (3 decimals, as the
# HEIGHT_FT sent to the Runner): no flip between runs, no conflict with the Runner's value
a7 = {"dam_id": "DWR-7", "name": "Seven", "height_m": 7.0, "td_vw_m3": 100 * AF}
p7, _n = ho.rundam_params(a7, (BX, BY), "EPSG:5070", "dem", folder)
d7 = os.path.join(folder, "DWR-7")
dri.write(d7, dri.handoff_values(a7, p7, (BX, BY)), dri.screening_result(a7, "tool 7b"), "0.9.0")
assert dr.load(d7).fields["height_ft"] == p7["HEIGHT_FT"] == 22.966
assert dri.tool8_values(a7)["height_ft"] == p7["HEIGHT_FT"]
dri.write(d7, dri.tool8_values(a7), dri.screening_result(a7, "tool 8"), "0.9.0", create=False)
r7 = dr.load(d7)
assert r7.fields["height_ft"] == 22.966 and not r7.conflicts and len(r7.status) == 1
r7.set("height_ft", p7["HEIGHT_FT"], "ndbreach", "3.2.0")        # the Runner records what it was sent
assert not r7.conflicts
print("OK (review fixes: no repeated status, one rounding)")

# ---------------------------------------------------------------- 5. version checks
qutils.PLUGIN_VERSIONS["nd_breach_runner"] = "3.0.5"
try:
    run7b()
    raise SystemExit("7b ran with a runner below the hard minimum")
except QgsProcessingException as e:
    assert "3.1.0 or newer" in str(e) and "3.0.5" in str(e), str(e)
qutils.PLUGIN_VERSIONS["nd_breach_runner"] = "3.1.4"
status, fb = run7b()
assert set(status.values()) == {"done"}
assert any("ND Dam Breach Runner 3.1.4 (needs 3.1.0+, recommended 3.7.0+: WARNING)" in m for m in fb.log)
qutils.PLUGIN_VERSIONS.clear()


class Bar:
    def __init__(self): self.msgs = []
    def pushMessage(self, title, text, level=None, duration=None): self.msgs.append((title, text, duration))


class Iface:
    def __init__(self): self.bar = Bar()
    def messageBar(self): return self.bar


iface = Iface()
plug = NDDamScreeningPlugin(iface)
msgs = plug.check_versions({"ndscreen": "0.9.0", "ndbreach": "3.0.0", "ndhazard": "2.9.1"})
assert [m[0] for m in msgs] == ["error", "warning"], msgs
assert len(iface.bar.msgs) == 2 and iface.bar.msgs[0][2] == 0 and "3.1.0" in iface.bar.msgs[0][1]
assert any(t == "ND dam suite" and "ND Dam Hazard" in m for t, _l, m in QgsMessageLog.LOG)
assert plug.check_versions() == []                               # once per session
assert NDDamScreeningPlugin(Iface()).check_versions({"ndscreen": "0.9.0"}) == []   # others not installed
print("OK (version checks)")
shutil.rmtree(work)
print("OK")
