"""v0.10: the popup window (Plugins > ND Dam Breach Screening): core/workflow.py (work-folder
layout, step state, the parameters filled into every tool), the dialog driven through the Qt test
doubles, and the plugin's menu action / single-instance / unload.

Run: PYTHONPATH=tests/qgis_mock:<parent of the plugin> python3 tests/test_popup.py
Nothing here runs in a real QGIS: the widgets are the behaviour-recording doubles of
tests/qgis_mock/qgis/PyQt.
"""
import json, os, sys, tempfile, time

# 0.16.0: a home folder of its own - the suite's ~/.ndsuite/config.json (Dams folder, data folder)
# written by another run must not reach these checks
os.environ["HOME"] = tempfile.mkdtemp(prefix="popup_home_")
for _k in ("NDSUITE_DATA", "NDSUITE_DAMS"):
    os.environ.pop(_k, None)

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(HERE)
PARENT = os.path.dirname(PLUGIN)
sys.path.insert(0, os.path.join(HERE, "qgis_mock"))
sys.path.insert(0, PARENT)
sys.path.insert(0, HERE)
import qgis  # noqa: E402
from qgis import processing  # noqa: E402
from qgis.core import QgsProject  # noqa: E402
from qgis.PyQt.QtWidgets import EVENTS, QFileDialog, QMessageBox  # noqa: E402
from qgis.PyQt import QtGui  # noqa: E402
import nd_dam_screening as pkg  # noqa: E402
from nd_dam_screening.core import workflow as wf  # noqa: E402
from nd_dam_screening.damsuite import dam_record as dr  # noqa: E402
import _gpkg  # noqa: E402

work = tempfile.mkdtemp(prefix="popup_")


def touch(*parts, age=0.0):
    p = os.path.join(*parts)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    open(p, "w").close()
    if age:
        t = time.time() - age
        os.utime(p, (t, t))
    return p


def state(rows, key):
    return [r for r in rows if r[0] == key][0]


# ------------------------------------------------ 1. the layout and the order of the steps
assert wf.KEYS == ("0", "16", "19", "1", "2", "3", "4", "18", "5", "6", "7", "7b", "8", "9", "10", "11", "12", "13", "14", "15", "17")
assert set(wf.LAYOUT) == set(wf.KEYS) and set(wf.ALG) == set(wf.KEYS)
assert wf.OPTIONAL == {"0", "6", "7b", "9", "11", "12", "13", "14", "15", "16", "17", "18", "19"}
assert wf.CHAIN_SKIP == {"7b", "11", "12", "13", "14", "15", "17"}
assert wf.algorithm_id("7b") == "nddam:runnerhandoff" and wf.algorithm_id("8") == "nddam:hazardclassification"

W = os.path.join(work, "w1")
os.makedirs(W)
S = {}
rows = wf.statuses(W, S)
assert state(rows, "1")[2] == "ready" and state(rows, "0")[2] == "optional"
assert state(rows, "2")[2] == "waiting" and "needs step 1" in state(rows, "2")[3]
assert wf.next_step(W) == "1"
assert state(wf.statuses(os.path.join(work, "nowhere"), S), "1")[2] in ("ready", "waiting")   # no crash

touch(W, "1_dams.gpkg", age=500)
assert wf.next_step(W) == "2" and state(wf.statuses(W, S), "1")[2] == "done"
touch(W, "2_terrain", "dam_DWR-1.tif", age=400)
rows = wf.statuses(W, S)
assert state(rows, "2")[2] == "done" and wf.next_step(W) == "3"
assert "no NHD waterbodies layer" in state(rows, "3")[3]          # 3 needs waterbodies
assert wf.is_done(W, "2") and not wf.is_done(W, "3")
# an empty terrain folder is not 'done'
os.makedirs(os.path.join(W, "x_empty"))
assert not wf.exists(W, "3", "CURVES")                               # folder missing
# the next step after 3 (needs 1 and 2) then 4
touch(W, "3_dams_storage.gpkg", age=300)
os.makedirs(os.path.join(W, "3_stage_storage"))
touch(W, "3_stage_storage", "curve_DWR-1.csv", age=300)
assert wf.next_step(W) == "4"
touch(W, "4_breach_points.gpkg", age=200)
touch(W, "5_dams_breach.gpkg", age=150)
assert wf.next_step(W) == "7"                                        # 6 is optional
assert state(wf.statuses(W, S), "6")[2] == "optional"
touch(W, "7_simulations.gpkg", age=100)
touch(W, "8_hazard.gpkg", age=90)
assert wf.next_step(W) == "10"
touch(W, "10_reports", "index.html", age=10)
assert wf.next_step(W) is None and state(wf.statuses(W, S), "10")[2] == "done"
# re-running tool 2 after 3 makes 3 (and what came after) out of date
touch(W, "2_terrain", "dam_DWR-2.tif")
rows = wf.statuses(W, S)
assert state(rows, "3")[2] == "stale" and "step 2" in state(rows, "3")[3] and state(rows, "2")[2] == "done"
assert state(rows, "1")[2] == "done"

# ------------------------------------------------ 2. the parameters filled in
for k in wf.KEYS:                                                    # outputs always given
    p = wf.defaults(k, W, S)
    for param, rel in wf.LAYOUT[k].items():
        if param != "DAMS_FOLDER" and k not in wf.FIXED_OUTPUTS:
            assert p[param] == os.path.join(W, rel), (k, param)
assert wf.defaults("18", W, S) == {"WORK": W}                       # 18 writes fixed names
d2 = wf.defaults("2", W, S)
assert d2["DAMS"] == os.path.join(W, "1_dams.gpkg") and d2["SRC"] == 0 and "LOCAL" not in d2
assert wf.defaults("2", W, {"terrain_source": 1, "local_dem": "/lidar"})["LOCAL"] == "/lidar"
assert "WB" not in wf.defaults("3", W, S)
d5 = wf.defaults("5", W, S)
assert d5["DAMS"].endswith("3_dams_storage.gpkg") and d5["CURVES"].endswith("3_stage_storage")
assert d5["BPTS"].endswith("4_breach_points.gpkg")
assert "DAMS" not in wf.defaults("6", os.path.join(work, "w_empty"), S)      # nothing before it: not guessed
d7 = wf.defaults("7", W, {"engine": 2, "workers": 3, "larger_clips": 1, "scenario": 1, "exe": "/x/lisflood"})
assert d7["DAMS"].endswith("5_dams_breach.gpkg") and d7["ENGINE"] == 2 and d7["WORKERS"] == 3
assert d7["TSRC"] == 1 and d7["SCEN"] == 1 and d7["EXE"] == "/x/lisflood"
assert "WORKERS" not in wf.defaults("7", W, {"workers": 0})                 # 0 = the tool's default
touch(W, "6_triage.gpkg", age=-0.0)
assert wf.defaults("7", W, S)["DAMS"].endswith("6_triage.gpkg")             # triage output wins over tool 5

# the data package: tool 0's output gives the layers, a chosen layer wins
PKG = _gpkg.write(os.path.join(W, "0_data_package.gpkg"), {
    n: ("POINT", [("x", "TEXT")], []) for n in
    ("structures", "structure_points", "roads", "railroads", "flowlines", "waterbodies", "dam_lines",
     "lifelines", "dams")})
assert wf.package_path(W) == PKG and "waterbodies" in wf.package_layers(PKG)
assert wf.slot_source("waterbodies", W, {}) == PKG + "|layername=waterbodies"
assert wf.slot_source("waterbodies", W, {"waterbodies": "layer_77"}) == "layer_77"
assert wf.slot_source("nlcd", W, {}) is None and wf.slot_source("dwr", W, {}) is None
d3 = wf.defaults("3", W, S)
assert d3["WB"] == PKG + "|layername=waterbodies" and d3["DEMDIR"].endswith("2_terrain")
assert wf.defaults("3", W, {"waterbodies": "mine"})["WB"] == "mine"
assert "no NHD waterbodies layer" not in state(wf.statuses(W, S), "3")[3]
d8 = wf.defaults("8", W, {"crit": 1, "chart_file": "/c.json"})
assert d8["SIM"].endswith("7_simulations.gpkg") and d8["BLD"] == PKG + "|layername=structures"
assert d8["OCC"] == "occ_cls" and d8["PRIM"] == "prim_occ" and d8["RAIL_PAX"] == "passenger"
assert d8["RAIL_TRAINS"] == "trains_day" and d8["CRIT"] == 1 and d8["CHART_FILE"] == "/c.json"
assert "DAMS_FOLDER" not in d8                                              # no folder yet: tool 8 creates none
os.makedirs(os.path.join(W, "Dams", "DWR-1"))
assert wf.defaults("8", W, S)["DAMS_FOLDER"] == os.path.join(W, "Dams")
# a structures layer of the engineer's own: its field names are unknown, so none are filled in
d8b = wf.defaults("8", W, {"structures": "my_bldg"})
assert d8b["BLD"] == "my_bldg" and "OCC" not in d8b and "PRIM" not in d8b
d7b = wf.defaults("7b", W, {"runner_dem": "/dem.tif", "dams_folder": "/runs/dams"})
assert d7b["DAMS"].endswith("8_hazard.gpkg") and d7b["DEM"] == "/dem.tif"
assert d7b["DAMS_FOLDER"] == "/runs/dams" and d7b["BLD"] == PKG + "|layername=structure_points"
assert d7b["S_HABITABLE"] == "habitable" and d7b["R_AADT"] == "aadt" and d7b["T_PASSENGER"] == "passenger"
assert wf.defaults("7b", W, {})["DAMS_FOLDER"] == os.path.join(W, "Dams")
d0 = wf.defaults("0", W, {"package": PKG, "structures": "bldg", "roads": "rd", "nid": "nid", "clip": "nd"})
assert d0["PACKAGE"] == PKG and d0["BLD"] == "bldg" and d0["ROADS"] == "rd" and d0["NIDD"] == "nid"
assert d0["CLIP"] == "nd" and d0["OUT"].endswith("0_data_package.gpkg")
assert "PACKAGE" not in wf.defaults("0", W, {"package": os.path.join(W, "nope.gpkg")})
d10 = wf.defaults("10", W, S)
assert d10["HAZ"].endswith("8_hazard.gpkg") and "SENS" not in d10 and d10["OUTDIR"].endswith("10_reports")
assert d10["BPTS"].endswith("4_breach_points.gpkg") and d10["DEMDIR"].endswith("2_terrain")

# every name the window fills in is a real parameter of the tool (a typo would be silent)
plug0 = pkg.classFactory(None)
plug0.initGui()
algs = {a.name(): a for a in plug0.provider.algs}
for a in algs.values():
    a.initAlgorithm()
assert {wf.ALG[k] for k in wf.KEYS} | {"downloaddem", "getdamdata"} == set(algs)   # 20: its own box; 0.16.0 getdamdata: Suite data
settings_all = {"waterbodies": "a", "structures": "b", "roads": "c", "rail": "d", "lifelines": "e", "streams": "f",
                "dam_lines": "g", "nlcd": "h", "dwr": "i", "nid": "j", "clip": "k", "workers": 2,
                "local_dem": "/l", "exe": "/e", "runner_dem": "/r", "chart_file": "/c", "package": PKG}
for variant in (settings_all, {}):
    for k in wf.KEYS:
        names = set(algs[wf.ALG[k]]._params)
        for param in wf.defaults(k, W, variant):
            assert param in names, "tool {} has no parameter {}".format(k, param)
        for param in wf.LAYOUT[k]:
            assert param in names or k in wf.FIXED_OUTPUTS, (k, param)
# the choices offered in the window are the tools' own lists
from nd_dam_screening.algorithms import run_breach_sim as rbs  # noqa: E402
assert list(algs["runbreachsim"]._params["SCEN"].options) == rbs.SCENARIOS
assert list(algs["runbreachsim"]._params["ENGINE"].options) == rbs.ENGINES

# ------------------------------------------------ 3. results text
assert wf.summary_counts(W) is None and "No report yet" in wf.counts_text(None)
os.makedirs(os.path.join(W, "10_reports"), exist_ok=True)
with open(os.path.join(W, "10_reports", "summary.csv"), "w", newline="") as fh:
    fh.write("dam_id,name,hz_class,review\nA,a,High,1\nB,b,Low,\nC,c,High,yes\nD,d,,0\n")
c = wf.summary_counts(W)
assert c["dams"] == 4 and c["High"] == 2 and c["Low"] == 1 and c["(none)"] == 1 and c["review"] == 2
t = wf.counts_text(c)
assert t.startswith("4 dam(s): 2 High, 1 Low, 1 (none)") and "2 flagged" in t and "Department decides" in t
assert "no dams folder" in wf.dam_record_text(os.path.join(work, "none"))
df = os.path.join(W, "Dams")
assert "no dam folder with a record" in wf.dam_record_text(df)
for n, tool in (("DWR-1", "ndscreen"), ("DWR-2", "ndscreen")):
    rec = dr.load(os.path.join(df, n), create=True) if os.path.isdir(os.path.join(df, n)) else None
    if rec is None:
        os.makedirs(os.path.join(df, n))
        rec = dr.load(os.path.join(df, n), create=True)
    rec.set("dam_id", n, tool, "0.10.0")
    rec.add_status("screened", tool, "0.10.0", {"triage": "needs 2D"})
    rec.save()
os.makedirs(os.path.join(df, "BAD"))
with open(os.path.join(df, "BAD", "dam.json"), "w") as fh:
    fh.write("{not json")
txt = wf.dam_record_text(df)
assert txt.startswith("dam.json: 2 dam(s)") and "2 screened" in txt and "1 record(s) could not be read" in txt, txt
titles = [t for t, _p in wf.result_layers(W)]
assert titles[:2] == ["1 - Prepare dam inventory", "3 - Estimate reservoir storage"], titles
assert not any(t.startswith("0 ") for t in titles)                 # the package is an input
assert "4 - Breach point and flow direction (breach points)" in titles
assert "8 - Hazard classification" in titles and not any("(IMP)" in t or "(OUTP)" in t for t in titles)
assert all(os.path.isfile(p) for _t, p in wf.result_layers(W)) and not any("10" == t.split(" ")[0]
                                                                          for t, _p in wf.result_layers(W))

# ------------------------------------------------ 4. the dialog
from nd_dam_screening.dialog import NDDamScreeningDialog, STATE_TEXT  # noqa: E402

QgsProject.instance().__dict__.pop("_entries", None)
dlg = NDDamScreeningDialog()
assert set(dlg.step_button) == set(wf.KEYS) and dlg.tabs.count() == 6 and len(dlg.step_button) == 21
assert [dlg.tabs.tabText(i) for i in range(6)] == ["Simple", "All steps", "Inputs", "Settings", "Review", "Results"]
assert not dlg.step_button["1"].isEnabled() and not dlg.next_btn.isEnabled()      # no work folder yet
assert "Choose or create a work folder" in dlg.hint_label.text()
W2 = os.path.join(work, "w2")
dlg.work_edit.setText(W2)
dlg._open_step("1")                                                                # no folder yet: created
assert os.path.isdir(W2) and processing.DIALOGS[-1][0] == "nddam:preparedams"
assert processing.DIALOGS[-1][1]["OUT"] == os.path.join(W2, "1_dams.gpkg")
assert ">>> Opened nddam:preparedams" in "\n".join(dlg.log_edit.lines)
dlg.refresh()
assert dlg.step_state["1"].text() == STATE_TEXT["ready"] and dlg.step_state["0"].text() == STATE_TEXT["optional"]
assert dlg.step_state["2"].text() == STATE_TEXT["waiting"] and "needs step 1" in dlg.step_note["2"].text()
assert dlg.step_button["1"].isEnabled() and dlg.next_btn.isEnabled()
assert "Next: 1 - Prepare dam inventory" in dlg.hint_label.text()
n = len(processing.DIALOGS)
dlg._open_next()
assert len(processing.DIALOGS) == n + 1 and processing.DIALOGS[-1][0] == "nddam:preparedams"
touch(W2, "1_dams.gpkg")
dlg.refresh()
assert dlg.step_state["1"].text() == STATE_TEXT["done"] and "Next: 2 - Cache terrain" in dlg.hint_label.text()
dlg._open_next()
assert processing.DIALOGS[-1][0] == "nddam:terraincache" and processing.DIALOGS[-1][1]["DAMS"].endswith("1_dams.gpkg")

# settings from the widgets reach the tools, and are saved with the project
dlg.engine.setCurrentIndex(2)
dlg.workers.setValue(5)
dlg.scenario.setCurrentIndex(1)
dlg.larger_clips.setCurrentIndex(2)
dlg.local_dem.setText("/lidar")
dlg.dams_folder.setText(os.path.join(W2, "MyDams"))
dlg._changed()
s = dlg.settings()
assert s["engine"] == 2 and s["workers"] == 5 and s["scenario"] == 1 and s["larger_clips"] == 2
assert s["local_dem"] == "/lidar" and s["work"] == W2
touch(W2, "5_dams_breach.gpkg")
for k, nm in (("3", "3_dams_storage.gpkg"), ("4", "4_breach_points.gpkg")):
    touch(W2, nm)
touch(W2, "2_terrain", "dam_x.tif")
dlg._open_step("7")
aid, p = processing.DIALOGS[-1]
assert aid == "nddam:runbreachsim" and p["ENGINE"] == 2 and p["WORKERS"] == 5 and p["SCEN"] == 1
assert p["TSRC"] == 2 and p["LOCAL"] == "/lidar" and p["DAMS"].endswith("5_dams_breach.gpkg")
text, ok = QgsProject.instance().readEntry("NDDamScreening", "state", "")
saved = json.loads(text)
assert ok and saved["engine"] == 2 and saved["work"] == W2 and saved["dams_folder"].endswith("MyDams")

# a new window of the same project brings the choices back
dlg2 = NDDamScreeningDialog()
s2 = dlg2.settings()
assert s2["engine"] == 2 and s2["workers"] == 5 and s2["work"] == W2 and s2["scenario"] == 1
assert dlg2.step_state["1"].text() == STATE_TEXT["done"]
# damaged saved state is ignored
QgsProject.instance().writeEntry("NDDamScreening", "state", "{oops")
assert NDDamScreeningDialog().settings()["engine"] == 2          # 0.13: the built-in engine is the default
QgsProject.instance().writeEntry("NDDamScreening", "state", json.dumps(saved))

# project layers fill the lists; a choice survives a refresh, also a file and a vanished layer
class Lyr:
    def __init__(self, i, n): self._i, self._n = i, n
    def id(self): return self._i
    def name(self): return self._n


QgsProject.instance().addMapLayer(Lyr("bld_1", "USA Structures"))
QgsProject.instance().addMapLayer(Lyr("rd_1", "Roads NDDOT"))
dlg2._fill_layers()
cb = dlg2.layer_combo["structures"]
assert [cb.itemText(i) for i in range(cb.count())] == ["(the package's layer / none)", "Roads NDDOT", "USA Structures"]
cb.setCurrentIndex(cb.findData("bld_1"))
assert dlg2.settings()["structures"] == "bld_1"
assert json.loads(QgsProject.instance().readEntry("NDDamScreening", "state", "")[0])["structures"] == "bld_1"
QFileDialog.NEXT.append("/data/my_roads.gpkg")
dlg2._browse_layer("roads")
assert dlg2.settings()["roads"] == "/data/my_roads.gpkg"
dlg2._fill_layers()
assert dlg2.settings()["roads"] == "/data/my_roads.gpkg" and dlg2.settings()["structures"] == "bld_1"
assert wf.defaults("8", W2, dlg2.settings())["BLD"] == "bld_1"                        # the choice reaches the tool
assert NDDamScreeningDialog().settings()["roads"] == "/data/my_roads.gpkg"              # and was saved
QgsProject.instance()._layers.clear()
dlg2._fill_layers()
assert dlg2.settings()["structures"] == "bld_1"                                         # layer gone: choice kept, shown by id
assert dlg2.layer_combo["structures"].currentText() == "bld_1"

# Processing cannot open the dialog: said in the log and to the user, nothing raised
processing.FAIL.append("no GUI")
EVENTS["messages"].clear()
assert dlg2._open_step("2") is False
assert any("could not be opened" in m for m in dlg2.log_edit.lines) and EVENTS["messages"]
processing.FAIL.clear()
# no work folder chosen
dlg3 = NDDamScreeningDialog()
dlg3.work_edit.setText("")
EVENTS["messages"].clear()
assert dlg3._open_step("1") is False and EVENTS["messages"][-1][2] == "Choose a work folder first."

# results tab: report, folder, result layers
QtGui.OPENED_URLS.clear()
dlg2.work_edit.setText(W)
dlg2.dams_folder.setText("")                     # -> <work>/Dams
dlg2.refresh()
assert dlg2.report_btn.isEnabled() and "4 dam(s)" in dlg2.counts_label.text()
assert dlg2.record_label.text().startswith("dam.json: 2 dam(s)")
dlg2._open_report()
dlg2._open_work_folder()
assert QtGui.OPENED_URLS == [os.path.join(W, "10_reports", "index.html"), W]
assert dlg2.add_btn.isEnabled()
before = len(QgsProject.instance().mapLayers())
n_added = dlg2._add_results()             # the files here are empty placeholders: reported, none added
assert n_added == 0 and len(QgsProject.instance().mapLayers()) == before
assert any("could not be read as a layer" in m for m in dlg2.log_edit.lines)
assert "Added 0 result layer(s)" in dlg2.log_edit.lines[-1]
# a readable result is added to the project, once per call
W3 = os.path.join(work, "w3")
os.makedirs(W3)
_gpkg.write(os.path.join(W3, "1_dams.gpkg"), {"dams": ("POINT", [("dam_id", "TEXT")], [])})
dlg2.work_edit.setText(W3)
assert dlg2._add_results() == 1 and len(QgsProject.instance().mapLayers()) == before + 1
assert "Added 1 result layer(s)" in dlg2.log_edit.lines[-1]
added_layer = list(QgsProject.instance().mapLayers().values())[-1]
assert added_layer.styles and added_layer.styles[0].endswith(os.path.join("styles", "dams_points.qml"))
assert any("style dams_points.qml applied" in m for m in dlg2.log_edit.lines)
# the suite styles module: names -> QML, every QML present and well-formed
from nd_dam_screening import suite_styles as SS  # noqa: E402
import xml.etree.ElementTree as ET  # noqa: E402
assert SS.style_for_path("/x/7_simulations/DWR-1_td_depth.tif") == "depth_m.qml"
assert SS.style_for_path("/x/7_simulations/DWR-1_td_incr_depth.tif") == "depth_m.qml"
assert SS.style_for_path("/x/7_simulations/DWR-1_td_vel.tif") == "velocity_ms.qml"
assert SS.style_for_path("/x/7_simulations/DWR-1_td_arrive.tif") == "arrival_h.qml"
assert SS.style_for_path("/x/Depth (Max).tif") == "depth_ft.qml" and SS.style_for_path("/x/Arrival Time.tif") == "arrival_h.qml"
assert SS.style_for_path("/x/8_hazard.gpkg") == "hazard_class_points.qml" and SS.style_for_path("/x/6_triage.gpkg") == "triage_points.qml"
assert SS.style_for_path("/x/HECRAS Velocity (Max).tif") == "velocity_fps.qml"
assert SS.style_for_path("/x/Hazard_Foundations.tif") == "acer_zones.qml" and SS.style_for_path("/x/random.tif") is None
assert SS.style_for_path("/x/0_data_package.gpkg|layername=roads") is None
for q in SS.available():
    ET.parse(os.path.join(SS.styles_folder(), q))
assert {"depth_ft.qml", "depth_m.qml", "hazard_class_points.qml", "acer_zones.qml", "triage_points.qml"} <= set(SS.available())
assert SS.attach(object(), "x", "depth_m.qml") is False                         # post-processors: real QGIS only
dlg2.close()

# ------------------------------------------------ 5. the plugin: menu action, one window, unload
class Sig:
    def __init__(self): self.slots = []
    def connect(self, f): self.slots.append(f)
    def disconnect(self, f=None):
        if f is None: self.slots = []
        elif f in self.slots: self.slots.remove(f)
        else: raise TypeError("not connected")


class Bar:
    msgs = []
    def pushMessage(self, *a, **k): self.msgs.append(a)


class Iface:
    def __init__(self):
        self.menu, self.removed, self.initializationCompleted = [], [], Sig()
    def mainWindow(self): return None
    def addPluginToMenu(self, name, action): self.menu.append((name, action))
    def removePluginMenu(self, name, action): self.removed.append((name, action))
    def messageBar(self): return Bar()


iface = Iface()
plug = pkg.classFactory(iface)
plug.initGui()
# 0.16.0: + "Suite data and servers..." in the suite menu (object name NDSuiteData, once for the three)
assert [m[0] for m in iface.menu] == ["ND Dam Breach Screening"] * 2 + ["ND Dam Suite"] * 4 and plug.action is iface.menu[0][1]
assert plug.home_action is iface.menu[1][1]                            # 0.11: Dam suite home...
assert len(plug.suite_actions) == 4                                     # 0.13: the shared suite menu
assert plug.suite_actions[3].triggered._slots == [plug.run_suite_data]
assert plug.action.triggered._slots == [plug.run_plugin]
assert plug.provider is not None                                       # the toolbox provider is still there
plug.initGui()                                                          # a second initGui adds no second entry
assert len(iface.menu) == 6
plug_suite_data = plug.suite_actions[3]
plug.action.triggered.emit()
first = plug.dialog
assert first is not None and first.isVisible()
plug.run_plugin()
assert plug.dialog is first                                             # one window
first.deleteLater()                                                     # Qt destroyed it
plug.run_plugin()
assert plug.dialog is not first and plug.dialog.isVisible()
plug.unload()
assert iface.removed and iface.removed[0][0] == "ND Dam Breach Screening" and plug.action is None
assert ("ND Dam Suite", plug_suite_data) in iface.removed and plug.suite_actions == []   # 0.16.0
assert plug.dialog is None and plug.provider is None
plug.unload()                                                           # safe twice
# no interface (tests, qgis_process): no menu entry, no error
p2 = pkg.classFactory(None)
p2.initGui()
assert p2.action is None
# a window that cannot be built does not stop the plugin from loading
import nd_dam_screening.dialog as dmod  # noqa: E402
orig = dmod.NDDamScreeningDialog
dmod.NDDamScreeningDialog = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
try:
    p3 = pkg.classFactory(Iface())
    p3.initGui()
    try:
        p3.run_plugin()
        raise AssertionError("run_plugin should raise so QGIS shows the traceback")
    except RuntimeError:
        pass
finally:
    dmod.NDDamScreeningDialog = orig


# ------------------------------------------------ 5b. 0.11: the new steps, settings and manifests
d7 = wf.defaults("7", W, {"baseline": True, "baseline_cfs": 120.0, "buildings": 2, "structures": "bld",
                          "building_raise_ft": 12.0, "building_n": 0.2})
assert d7["BASELINE"] is True and d7["BASELINE_CFS"] == 120.0 and d7["BUILDINGS"] == 2 and d7["BLD"] == "bld"
assert d7["BUILDING_RAISE_FT"] == 12.0 and d7["BUILDING_N"] == 0.2
d7 = wf.defaults("7", W, {})
assert "BASELINE" not in d7 and "BUILDINGS" not in d7 and "BLD" not in d7
# 0.20.0: the clip grows from the downstream trace only when the settings ask for it
assert "PREGROW" not in d7 and wf.DEFAULT_SETTINGS["pregrow"] is False
assert wf.defaults("7", W, {"pregrow": True})["PREGROW"] is True
assert wf.defaults("8", W, {"incremental": True})["INCREMENTAL"] is True and "INCREMENTAL" not in wf.defaults("8", W, {})
d11 = wf.defaults("11", W, {"ref_folder": "/ref", "ref_units": 1, "scenario": 0})
assert d11["SIM"].endswith("7_simulations.gpkg") and d11["SIMDIR"].endswith("7_simulations") if wf.exists(W, "7", "OUTDIR") else True
assert d11["REF_FOLDER"] == "/ref" and d11["REF_UNITS"] == 1 and d11["OUTDIR"].endswith("11_benchmark")
d12 = wf.defaults("12", W, {"engine": 1, "workers": 3})
assert d12["DAMS"].endswith("6_triage.gpkg") and d12["ENGINE"] == 1 and d12["WORKERS"] == 3
assert d12["OUT_CSV"].endswith("12_runtime_estimate.csv")
d13 = wf.defaults("13", W, {"old_inventory": "/old/1_dams.gpkg"})
assert d13["NEW"].endswith("1_dams.gpkg") and d13["OLD"] == "/old/1_dams.gpkg" and d13["DAMS_FOLDER"] == os.path.join(W, "Dams")
assert d13["OUT_CSV"].endswith("13_inventory_changes.csv") and d13["OUT"].endswith("13_changed_dams.gpkg")
assert wf.defaults("14", W, {})["REPORT"].endswith("14_selftest_report.txt")
d15 = wf.defaults("15", W, {"engine": 2})
assert d15["WORK"] == W and json.loads(d15["SETTINGS"])["engine"] == 2 and "work" not in json.loads(d15["SETTINGS"])
assert wf.chain_summary("7", os.path.join(work, "w_empty2"), {"baseline": True, "baseline_cfs": 50, "buildings": 1})[:2] == [
    "step 7: no-breach baseline run too (ambient 50 cfs); increment rasters",
    "step 7: buildings raise the DEM (NO footprint layer - nothing will be done)"]
assert wf.chain_summary("7", W, {"buildings": 2})[0] == \
    "step 7: buildings Manning's n (footprints = 0_data_package.gpkg|layername=structures)"
# every filled name is a parameter of the new tools too (algs re-read with the 0.11 provider)
for k in ("11", "12", "13", "14", "15"):
    names = set(algs[wf.ALG[k]]._params)
    for param in wf.defaults(k, W, settings_all):
        assert param in names, "tool {} has no parameter {}".format(k, param)
    assert state(wf.statuses(W, S), k)[2] in ("optional", "waiting"), k
assert state(wf.statuses(W, S), "11")[2] == "optional"                 # 7 is done in W
# the new steps are not chained
assert "11" not in wf.chain_plan(W, settings_all, ("0", "6", "9")) and wf.chain_next(W, settings_all, ("11",), ())[0] != "11"

# manifests: a step with a manifest is out of date when an INPUT's content changed, not its date
from nd_dam_screening.core import manifest as mf  # noqa: E402
WM = os.path.join(work, "w_manifest")
os.makedirs(WM)
src = touch(WM, "1_dams.gpkg")
open(src, "wb").write(b"version one")
out3 = touch(WM, "3_dams_storage.gpkg")
os.makedirs(os.path.join(WM, "3_stage_storage")); touch(WM, "3_stage_storage", "curve_A.csv")
written = mf.record("nddam:estimatestorage", {"DAMS": src, "WB": "layer_77", "SHAPE": 1.0,
                                               "OUT": out3, "CURVES": os.path.join(WM, "3_stage_storage")},
                    {"OUT": out3, "CURVES": os.path.join(WM, "3_stage_storage")})
assert sorted(os.path.basename(w) for w in written) == ["3_dams_storage.gpkg.manifest.json", "run_manifest.json"]
m = mf.read(written[0])
assert m["tool"].startswith("ND Dam Breach Screening") and m["algorithm"] == "nddam:estimatestorage"
assert set(m["inputs"]) == {"DAMS"} and m["inputs"]["DAMS"]["sha256"] and m["settings"]["WB"] == "layer_77"
assert set(m["outputs"]) == {"OUT", "CURVES"} and m["outputs"]["CURVES"]["folder"] is True
from nd_dam_screening.damsuite import chain as ch_  # noqa: E402
assert ch_.digest_of(m) == m["manifest_sha256"]
assert wf.changed_inputs(WM, "3") == [] and wf.changed_inputs(WM, "1") is None      # 1 has no manifest
t_old = os.path.getmtime(src)
open(src, "wb").write(b"version two")                     # same size, new content
os.utime(src, (t_old, t_old))                              # and the OLD date
assert wf.changed_inputs(WM, "3") == ["DAMS"]
touch(WM, "2_terrain", "x.tif")
rows = wf.statuses(WM, {})
assert state(rows, "3")[2] == "stale" and "input(s) changed since this step ran: DAMS" in state(rows, "3")[3]
open(src, "wb").write(b"version one")                     # content back: done again, whatever the dates
t_new = time.time() + 1000
os.utime(src, (t_new, t_new))
assert state(wf.statuses(WM, {}), "3")[2] == "done"
# a missing input is reported too; the per-dam manifest for the chain check
os.remove(src)
assert wf.changed_inputs(WM, "3") == ["DAMS (missing)"]
dam = os.path.join(WM, "Dams", "DWR-1"); os.makedirs(dam)
dep = touch(WM, "7_simulations", "DWR-1_td_depth.tif"); open(dep, "wb").write(b"depth")
mp = mf.dam_manifest(dam, "DWR-1", {"depth": dep, "velocity": None, "arrival": "/nope.tif"},
                     {"hz_class": "High", "hz_border": "no"}, {"CRIT": 0, "MIND_FT": 0.1, "OUT": "x"})
assert mp == os.path.join(dam, "_screening_model", "run_manifest.json")
dm = mf.read(mp)
assert set(dm["outputs"]) == {"depth"} and dm["extra"]["screening_result"]["hz_class"] == "High"
assert set(dm["settings"]) == {"CRIT", "MIND_FT"} and ch_.find_runner_manifest(dam) == mp
assert mf.dam_manifest(dam, "DWR-2", {"depth": None}, {}, {}) is None
assert mf.record("nddam:x", {}, {"OUT": "TEMPORARY_OUTPUT"}) == []                   # nothing on disk

# the export of the settings file for the headless tool
dlg_e = NDDamScreeningDialog()
dlg_e.work_edit.setText(W)
dlg_e.engine.setCurrentIndex(3)
pth = dlg_e._export_settings(os.path.join(W, "screening_settings.json"))
assert pth and json.load(open(pth))["engine"] == 3 and "work" not in json.load(open(pth))
assert any("nddam:runchain" in m for m in dlg_e.log_edit.lines)

# ------------------------------------------------ 6. run the steps in order
clock = [time.time() - 100000]
calls = []


def make_start(fail_at=None, silent_at=None, cancel_at=None, chain_ref=None):
    """A synchronous stand-in for the background task: writes the step's outputs."""
    def start(step, aid, params, on_done):
        calls.append((step, aid, dict(params)))
        if step == fail_at:
            return on_done(False, "boom")
        if step == cancel_at:
            chain_ref[0].cancel()
            return on_done(False, "")
        if step != silent_at:
            clock[0] += 10
            for param, rel in wf.LAYOUT[step].items():
                if param == "DAMS_FOLDER":
                    continue
                pth = os.path.join(W4, rel)
                if os.path.splitext(pth)[1]:
                    touch(pth)
                    os.utime(pth, (clock[0], clock[0]))
                else:
                    os.makedirs(pth, exist_ok=True)
                    f = touch(pth, "x_%s.dat" % step)
                    os.utime(f, (clock[0], clock[0]))
                    os.utime(pth, (clock[0], clock[0]))
            if step == "10":
                f = touch(W4, "10_reports", "index.html")
                os.utime(f, (clock[0], clock[0]))
        on_done(True, "")
    return start


def new_work(name):
    d = os.path.join(work, name)
    os.makedirs(d, exist_ok=True)
    return d


def run_chain(settings, include=(), **kw):
    log = []
    ref = [None]
    ch = wf.Chain(W4, lambda: settings, include, make_start(chain_ref=ref, **kw), log=log.append)
    ref[0] = ch
    del calls[:]
    ch.begin()
    return ch, log


own = {"dwr": "dwr_layer", "waterbodies": "wb_layer", "dam_lines": "dl_layer", "structures": "bld_layer"}
W4 = new_work("w4")
ch, log = run_chain(own)
assert ch.ok and ch.ran == ["1", "2", "3", "4", "5", "7", "8", "10"], (ch.ran, ch.result)
assert [c[0] for c in calls] == ch.ran and all(c[1] == "nddam:" + wf.ALG[c[0]] for c in calls)
assert calls[0][2]["DWR"] == "dwr_layer" and calls[2][2]["WB"] == "wb_layer"
assert calls[2][2]["DAMS"].endswith("1_dams.gpkg")                       # each step sees the one before
assert calls[5][2]["DAMS"].endswith("5_dams_breach.gpkg") and calls[6][2]["BLD"] == "bld_layer"
assert ch.result.startswith("Nothing left to run; ran step(s) 1, 2, 3") and not ch.active
assert any(">>> Step 8 - Hazard classification" in m for m in log)
assert any("step 8: no roads layer - it cannot affect the result" in m for m in log), log
assert any("step 8: structures = bld_layer" in m for m in log)
# a second run finds nothing to do
ch, log = run_chain(own)
assert ch.ok and ch.ran == [] and "all were done" in ch.result and not calls
# an earlier step run again makes what follows out of date: only those are re-run
clock[0] += 10
f = touch(W4, "2_terrain", "dam_new.tif")
os.utime(f, (clock[0], clock[0]))
os.utime(os.path.join(W4, "2_terrain"), (clock[0], clock[0]))
assert state(wf.statuses(W4, own), "3")[2] == "stale"
ch, log = run_chain(own)
assert ch.ok and ch.ran == ["3", "5", "7", "8", "10"], ch.ran
assert not [c for c in calls if c[0] in ("1", "2", "4")]
# optional steps only when asked
W4 = new_work("w4b")
ch, log = run_chain(own, include=("0", "6", "9"))
assert ch.ran == ["0", "1", "2", "3", "4", "5", "6", "7", "8", "9", "10"], ch.ran
assert calls[0][1] == "nddam:datapackage" and calls[0][2]["OUT"].endswith("0_data_package.gpkg")
assert [c for c in calls if c[0] == "7"][0][2]["DAMS"].endswith("6_triage.gpkg")
# 7b is never chained
assert "7b" not in ch.ran and wf.chain_next(new_work("w4c"), own, ("0", "6", "9", "7b"), ())[0] == "0"

# stops: missing data, a failing step, no output written, cancelled
W4 = new_work("w4d")
ch, log = run_chain({})
assert not ch.ok and not calls and "Step 1 (Prepare dam inventory) cannot run: no ND DWR dams layer and no NID dams layer" in ch.result, ch.result
W4 = new_work("w4e")
ch, log = run_chain({"dwr": "d", "waterbodies": "w"}, fail_at="3")
assert not ch.ok and ch.ran == ["1", "2"] and ch.result == "Step 3 (Estimate reservoir storage) did not finish: boom"
assert not ch.active and not any(c[0] == "4" for c in calls)
assert wf.chain_next(W4, {"dwr": "d"}, (), ())[1] == ""
W4 = new_work("w4e2")
ch, log = run_chain({"dwr": "d", "waterbodies": "w"}, silent_at="1")
assert not ch.ok and "finished but its output is not in the work folder (1_dams.gpkg)" in ch.result and not ch.ran
W4 = new_work("w4f")
ch, log = run_chain(own, cancel_at="2")
assert not ch.ok and ch.ran == ["1"] and "was cancelled" in ch.result
# step 3 without a waterbodies layer: stops before it is started, says what to do
W4 = new_work("w4g")
ch, log = run_chain({"dwr": "d"})
assert not ch.ok and ch.ran == ["1", "2"] and "Step 3 (Estimate reservoir storage) cannot run: no NHD waterbodies layer" in ch.result
assert "Inputs / Settings tab" in ch.result and not any(c[0] == "3" for c in calls)
# a late callback after a stop is ignored
late = []
W4 = new_work("w4h")
ch2 = wf.Chain(W4, lambda: own, (), lambda st, aid, p, done: late.append(done))
ch2.begin()
ch2.cancel()
late[0](False, "")
assert not ch2.active and not ch2.ok and "was cancelled" in ch2.result
before = ch2.result
late[0](True, "")                          # a second, late call changes nothing
assert ch2.result == before and not ch2.active and ch2.ran == []
# required parameters
defs = [("A", "needed a", False, False, False), ("B", "needed b", False, True, False),
        ("C", "optional c", True, False, False), ("OUT", "output", False, False, True),
        ("D", "needed d", False, False, False)]
assert wf.required_missing(defs, {"A": "x"}) == ["needed d"]
assert wf.required_missing(defs, {"A": "x", "D": 0}) == []
assert wf.required_missing(defs, {"A": "", "D": None}) == ["needed a", "needed d"]
ch3 = wf.Chain(new_work("w4i"), lambda: own, (), make_start(), problems=lambda st, aid, p: ["X is empty"] if st == "2" else [])
W4 = ch3.work
del calls[:]
ch3.begin()
assert not ch3.ok and ch3.ran == ["1"] and "Step 2 (Cache terrain per dam) cannot run: X is empty" in ch3.result

# the same through the window
Pcls = {"QgsProcessingParameterFeatureSink", "QgsProcessingParameterFolderDestination",
        "QgsProcessingParameterFileDestination"}


def defs_of(aid):
    a = algs[aid.split(":")[1]]
    return [(n, p.description, bool(p.optional), p.defaultValue is not None, type(p).__name__ in Pcls)
            for n, p in a._params.items()]


W4 = new_work("w4j")
dlg5 = NDDamScreeningDialog()
dlg5.work_edit.setText(W4)
for slot, val in own.items():
    cb = dlg5.layer_combo[slot]
    cb.addItem(val, val)
    cb.setCurrentIndex(cb.count() - 1)
assert dlg5.settings()["dwr"] == "dwr_layer"
dlg5._definitions = defs_of
dlg5._start_algorithm = make_start()
EVENTS["messages"].clear()
del calls[:]
orig_q = QMessageBox.question
def _no(parent, title, text, *a):
    orig_q(parent, title, text)          # recorded in EVENTS like the real one
    return getattr(QMessageBox, "StandardButton", QMessageBox).No


QMessageBox.question = staticmethod(_no)
assert dlg5._run_chain() is False and not calls and dlg5.chain is None           # declined: nothing runs
QMessageBox.question = orig_q
assert EVENTS["messages"][-1][0] == "question" and "1 - Prepare dam inventory" in EVENTS["messages"][-1][2]
assert "step 8: no railroads layer - it cannot affect the result" in EVENTS["messages"][-1][2]
assert "step 8: roads = my_roads.gpkg" in EVENTS["messages"][-1][2]    # restored from the project
assert "Data the steps will use" in EVENTS["messages"][-1][2]
assert dlg5._run_chain() is True
assert [c[0] for c in calls] == ["1", "2", "3", "4", "5", "7", "8", "10"] and not dlg5.chain.active
assert dlg5.chain_btn.isEnabled() and not dlg5.chain_stop_btn.isEnabled() and dlg5.step_button["1"].isEnabled()
assert dlg5.step_state["10"].text() == STATE_TEXT["done"] and "Nothing required is left" in dlg5.hint_label.text()
assert any(">>> Step 5 - Breach parameters and hydrograph" in m for m in dlg5.log_edit.lines)
assert dlg5._run_chain() is False and EVENTS["messages"][-1][2] == "Nothing to run: every required step is done."
# a chain that fails: the window is usable again and says why
W4 = new_work("w4k")
dlg5.work_edit.setText(W4)
dlg5._start_algorithm = make_start(fail_at="2")
EVENTS["messages"].clear()
assert dlg5._run_chain() is True and not dlg5.chain.active and not dlg5.chain.ok
assert EVENTS["messages"][-1][0] == "warning" and "Step 2 (Cache terrain per dam) did not finish: boom" in EVENTS["messages"][-1][2]
assert dlg5.chain_btn.isEnabled() and dlg5.step_button["2"].isEnabled()
# the optional steps ticked in the window are saved and used
dlg5.chain_check["6"].setChecked(True)
assert dlg5.settings()["chain"] == ["6"] and json.loads(QgsProject.instance().readEntry("NDDamScreening", "state", "")[0])["chain"] == ["6"]
# the checks of the tool's own required parameters stop a chain before it starts the tool
dlg5._definitions = lambda aid: [("NEEDED", "Something required", False, False, False)]
W4 = new_work("w4l")
dlg5.work_edit.setText(W4)
dlg5._start_algorithm = make_start()
del calls[:]
EVENTS["messages"].clear()
dlg5._run_chain()
assert not calls and "Something required is empty" in EVENTS["messages"][-1][2]
# a stop while a step runs
W4 = new_work("w4m")
dlg5.work_edit.setText(W4)
dlg5._definitions = defs_of
pending = []
dlg5._start_algorithm = lambda st, aid, p, done: pending.append(done)
dlg5._run_chain()
assert dlg5.chain.active and dlg5.chain_stop_btn.isEnabled() and not dlg5.chain_btn.isEnabled()
assert not dlg5.step_button["1"].isEnabled()                       # nothing else can be opened meanwhile
dlg5._stop_chain()
pending[0](False, "")
assert not dlg5.chain.active and "was cancelled" in dlg5.chain.result and dlg5.chain_btn.isEnabled()

print("popup window: all checks passed")

# ------------------------------------------------ 7. the Dam suite home window (0.11, shared)
from nd_dam_screening import suite_home as SHW  # noqa: E402
from nd_dam_screening.damsuite import dam_record as dr_  # noqa: E402
import qgis.utils as qutils_  # noqa: E402

HD = os.path.join(work, "home_dams")
for n, steps in (("DWR-1", [("screened", "ndscreen", {"triage": "needs 2D"})]),
                 ("DWR-2", [("screened", "ndscreen", {"triage": "needs 2D"}), ("modelled", "ndbreach", {})]),
                 ("DWR-3", [("modelled", "ndbreach", {}), ("classified", "ndhazard", {"hazard_class": "High"})])):
    os.makedirs(os.path.join(HD, n))
    rec = dr_.load(os.path.join(HD, n), create=True)
    rec.set("dam_id", n, "ndscreen", "0.11.0")
    for i, (st, tool, res) in enumerate(steps):
        rec.add_status(st, tool, "1", res, when="2026-0{}-01T10:00:00".format(i + 1))
    rec.save()
QgsProject.instance().__dict__.get("_entries", {}).pop(("NDDamSuite", "home_dams_folder"), None)
home = SHW.SuiteHomeDialog(opened_from="ndscreen")
assert home.summary_label.text() == "Choose the Dams folder."
home.dams_edit.setText(HD)
home._changed()
assert home.table.rowCount() == 3 and home.table.columnCount() == len(SHW.COLUMNS)
assert home.table.item(0, 0).text() == "DWR-1" and home.table.item(0, 8).text().startswith("model:")
assert home.table.item(1, 8).text().startswith("classify:") and home.table.item(2, 2).text() == "High"
assert home.table.item(0, 9).text() == "ND Dam Breach Runner" and home.table.item(1, 9).text() == "ND Dam Hazard Classification Helper"
assert "3 dam(s); next: 1 model, 1 classify, 1 none" in home.summary_label.text()
assert "Opened from ND Dam Breach Screening" in home.plugins_label.text()
assert QgsProject.instance().readEntry("NDDamSuite", "home_dams_folder", "")[0] == HD
# opening the plugin for a dam: via qgis.utils.plugins, the window pointed at the dam
calls = []


class FakeRunner(object):
    def __init__(self):
        class D(object):
            def __init__(s): s.dams_edit, s.dam_edit, s.refreshed = QLineEditStub(), QLineEditStub(), 0
            def refresh(s): s.refreshed += 1
        self.dialog = D()
    def run_plugin(self): calls.append("runner")


class QLineEditStub(object):
    def __init__(self): self.t = ""
    def setText(self, t): self.t = t
    def text(self): return self.t


qutils_.plugins["nd_breach_runner"] = FakeRunner()
home.table.setCurrentCell(0, 0)
assert home._open_for_row(0) is True and calls == ["runner"]
fr = qutils_.plugins["nd_breach_runner"].dialog
assert fr.dams_edit.t == HD and fr.dam_edit.t == "DWR-1" and fr.refreshed == 1
# the Screening window itself (this plugin), pointed at the dams folder
qutils_.plugins["nd_dam_screening"] = plug_s = type("P", (), {"dialog": NDDamScreeningDialog(),
                                                              "run_plugin": lambda self: calls.append("screen")})()
assert home.open_plugin("ndscreen", home.rows[0]) is True and plug_s.dialog.settings()["dams_folder"] == HD
# a plugin that is not installed: a message, no crash
EVENTS["messages"].clear()
assert home.open_plugin("ndhazard", home.rows[1]) is False and "not installed" in EVENTS["messages"][-1][2]
# the plugin entry point: one window, re-created when destroyed
plug_home = pkg.classFactory(Iface())
plug_home.initGui()
assert [m[0] for m in plug_home.iface.menu] == ["ND Dam Breach Screening"] * 2 + ["ND Dam Suite"] * 4
h1 = plug_home.run_home()
assert h1 is plug_home.home_dialog and h1.isVisible() and plug_home.run_home() is h1
h1.deleteLater()
assert plug_home.run_home() is not h1
plug_home.unload()
assert plug_home.home_dialog is None and plug_home.home_action is None
del qutils_.plugins["nd_breach_runner"]; del qutils_.plugins["nd_dam_screening"]
print("suite home: all checks passed")
