"""0.13: the popup's Simple tab, settings grid + file, presets, preflight, run one dam, Review tab,
results group (dialog.py, suite_settings.py, core/review.py).

Run: FAKE_QGIS_FLAVOR=3.40 PYTHONPATH=tests/qgis_mock:<parent of the plugin> python3 tests/test_popup_013.py
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "qgis_mock"))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
sys.path.insert(0, HERE)

import _gpkg  # noqa: E402
from qgis.core import QgsProject, QgsVectorLayer  # noqa: E402
from qgis.PyQt.QtWidgets import EVENTS, QFileDialog, QInputDialog, QMessageBox  # noqa: E402
from qgis.PyQt import QtGui  # noqa: E402
import nd_dam_screening as pkg  # noqa: E402
from nd_dam_screening.core import workflow as wf, preflight as pf, review as rv, settings_groups as SG  # noqa: E402
from nd_dam_screening.damsuite import settings as DS  # noqa: E402
from nd_dam_screening.dialog import NDDamScreeningDialog, STATE_TEXT  # noqa: E402
from nd_dam_screening import suite_settings as SS  # noqa: E402

ROOT = tempfile.mkdtemp(prefix="popup013_")
pf.Env.url_ok = lambda self, url: (True, "stubbed")            # no network in the tests
plug = pkg.classFactory(None)
plug.initGui()
algs = {a.name(): a for a in plug.provider.algs}
for a in algs.values():
    a.initAlgorithm()
from qgis import processing  # noqa: E402
processing.REGISTRY.update(algs)


def touch(*parts):
    p = os.path.join(*parts)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as fh:
        fh.write("x")
    return p


def new_work(name):
    d = os.path.join(ROOT, name)
    os.makedirs(d, exist_ok=True)
    return d


DS.set_current_path("", remember=True)
W = new_work("w1")
dlg = NDDamScreeningDialog()
dlg.work_edit.setText(W)
dlg._changed()

# ---------------------------------------------------------------- 1. the tabs and the grid
assert [dlg.tabs.tabText(i) for i in range(dlg.tabs.count())] == ["Simple", "All steps", "Inputs", "Settings", "Review", "Results"]
g = dlg.grid
assert isinstance(g, SS.SettingsGrid) and g.tabs.titles[0] == "Run options"
assert g.tabs.titles[1:] == [t for t in SG.TAB_ORDER if t in g.groups], g.tabs.titles
assert len(g.catalog) > 100 and all(s.plugin == "ndscreen" for s in g.catalog)
head = ("ndscreen", "findponds", "HEAD_FT")
assert head in g.items and g.value(head) == 1.0 and g.by_id[head].unit == "ft" and g.by_id[head].your
tbl, row = g.items[head]
assert tbl.item(row, SS.C_LABEL).text().endswith(SS.YOUR_MARK) and tbl.item(row, SS.C_UNIT).text() == "ft"
assert tbl.item(row, SS.C_DEFAULT).text() == "1" and tbl.item(row, SS.C_VALUE).text() == "1"
# the simple tab lists the main steps in plain words, numbered buttons stay on All steps
assert set(dlg.simple_name) == set(k for k in SG.MAIN_STEPS if k != "17")
assert dlg.simple_name["1"].text() == "Prepare the dams" and dlg.step_button["1"]._text.startswith("1 - ")
assert dlg.settings()["engine"] == 2                                   # the built-in engine is the default

# ---------------------------------------------------------------- 2. what to screen <-> the chain boxes
assert dlg.screen_what() == 0 and not dlg.chain_check["16"].isChecked()
dlg.screen_radio[1].setChecked(True)
assert dlg.screen_what() == 1 and dlg.chain_check["16"].isChecked() and not dlg.chain_check["18"].isChecked()
assert dlg.settings()["screen_what"] == 1 and "16" in dlg.settings()["chain"]
assert not dlg.simple_name["1"].isEnabled() and dlg.simple_name["16"].isEnabled()        # greyed, not removed
dlg.screen_radio[2].setChecked(True)
assert dlg.chain_check["16"].isChecked() and dlg.chain_check["18"].isChecked() and dlg.simple_name["18"].isEnabled()
dlg.chain_check["18"].setChecked(False)
assert dlg.screen_what() == 1                                           # the boxes drive the radios too
dlg.screen_radio[0].setChecked(True)
assert not dlg.chain_check["16"].isChecked() and dlg.settings()["screen_what"] == 0

# ---------------------------------------------------------------- 3. a new settings file, edits saved at once
sf = os.path.join(W, "damsuite_settings.xlsx")
QFileDialog.NEXT.append(sf)
assert dlg._new_settings_file() == sf and os.path.isfile(sf)
assert DS.current_path() == sf and dlg.settings()["settings_file"] == sf
loaded = DS.Settings.load(sf)
assert loaded.window("ndscreen")["engine"] == 2 and loaded.meta["versions"]["ndscreen"]
assert g.set_value(head, "1.5") == "" and g.value(head) == 1.5
assert DS.Settings.load(sf).get("ndscreen", "findponds", "HEAD_FT") == 1.5          # autosaved
assert tbl.item(row, SS.C_VALUE).text() == "1.5" and tbl.item(row, SS.C_VALUE)._font.bold
assert "(1 changed)" in g.file_label.text()
# a wrapped tool opens with the file's value as its default
fresh = algs["findponds"].createInstance()
fresh.initAlgorithm()
assert fresh._params["HEAD_FT"].defaultValue == 1.5
# bad input: refused with a message, the row shows the value in effect
EVENTS["messages"].clear()
assert "not a number" in g.set_value(head, "tall", from_widget=True)
assert EVENTS["messages"][-1][0] == "information" and g.value(head) == 1.5
# an enum through its combo, a bool through its check state
src = ("ndscreen", "findponds", "SRC")
g.combos[src].setCurrentIndex(1)
assert g.value(src) == 1 and DS.Settings.load(sf).get("ndscreen", "findponds", "SRC") == "Local DEM folder"
keep = ("ndscreen", "findponds", "KEEP_DEP")
t2, r2 = g.items[keep]
t2.item(r2, SS.C_VALUE).setCheckState(SS.CHECKED)
assert g.value(keep) is True and DS.Settings.load(sf).typed(g.by_id[keep]) is True       # TRUE in the file
# empty = back to the default
assert g.set_value(head, "") == "" and g.value(head) == 1.0 and head not in DS.Settings.load(sf).values
# filters
g.only_your.setChecked(True)
assert all(g.by_id[i].your for i in g.visible_ids()) and head in g.visible_ids()
g.only_your.setChecked(False)
g.find_edit.setText("dugout")
vis = g.visible_ids()
assert ("ndscreen", "findponds", "DUG_DEG") in vis and ("ndscreen", "triage", "AUDIT") not in vis
g.find_edit.setText("")
g.only_changed.setChecked(True)
assert set(g.visible_ids()) == {src, keep}
g.only_changed.setChecked(False)
# reset a tab and all
g.reset_tab("Ponds & dugouts")
assert g.value(src) == 0 and g.value(keep) is False and not g.settings.changed(g.catalog)
g.set_value(("ndscreen", "triage", "AUDIT"), 25)
g.reset_all()
assert g.value(("ndscreen", "triage", "AUDIT")) == 5.0
# the window's choices go to the file's window rows
dlg.engine.setCurrentIndex(3)
assert DS.Settings.load(sf).window("ndscreen")["engine"] == 3
# the preflight tool / chain read the same file
assert wf.defaults("17", W, dlg.settings())["SETTINGS"] == sf and wf.defaults("15", W, dlg.settings())["SETTINGS"] == sf

# ---------------------------------------------------------------- 4. presets
names = [dlg.preset_combo.itemText(i) for i in range(dlg.preset_combo.count())]
assert names[0] == "(choose a preset)" and {"County screening", "Pond survey", "Single dam, detailed"} <= set(names)
dlg.preset_combo.setCurrentIndex(names.index("Pond survey"))
QMessageBox.QUESTION_ANSWER = QMessageBox.StandardButton.Yes if hasattr(QMessageBox, "StandardButton") else QMessageBox.Yes
assert dlg._apply_preset() is True
assert dlg.screen_what() == 1 and dlg.chain_check["16"].isChecked() and dlg.settings()["engine"] == 2
assert g.value(("ndscreen", "findponds", "MAX_AC")) == 50 and DS.Settings.load(sf).window("ndscreen")["screen_what"] == 1
dlg.screen_radio[0].setChecked(True)

# ---------------------------------------------------------------- 5. the preflight on the Simple tab
checks = dlg._preflight()
assert dlg.preflight_label.text().startswith("Check: not ready") and "ND DWR dams" in dlg.preflight_text.toPlainText()
assert any(c["title"] == "USGS 3DEP service" and c["detail"] == "stubbed" for c in checks)
EVENTS["messages"].clear()
assert dlg._run_chain(preflight=True) is False and EVENTS["messages"][-1][0] == "warning" and "Not ready" in EVENTS["messages"][-1][2]
# with the layers given (files), the check passes to the question
dwr = _gpkg.write(os.path.join(W, "dwr.gpkg"), {"dams": ("Point", [("structure_", "INTEGER"), ("dam_name", "TEXT"), ("max_dam_he", "REAL"),
                                                           ("normal_vol", "REAL"), ("max_pool_v", "REAL"), ("hazard_cla", "TEXT"), ("status", "TEXT")],
                                                 [(("Point", (0.0, 0.0)), {"structure_": 5289, "dam_name": "Alpha"}),
                                                  (("Point", (10.0, 0.0)), {"structure_": 5290, "dam_name": "Beta"})])})
lines_p = _gpkg.write(os.path.join(W, "lines.gpkg"), {"l": ("LineString", [("structure_id", "INTEGER")],
                                                        [(("LineString", [(0, 0), (1, 1)]), {"structure_id": 5289})])})
wb = _gpkg.write(os.path.join(W, "wb.gpkg"), {"wb": ("Polygon", [("gnis_name", "TEXT")], [(("Polygon", [[(0, 0), (1, 0), (1, 1), (0, 0)]]), {})])})
bld = _gpkg.write(os.path.join(W, "bld.gpkg"), {"b": ("Point", [("OCC_CLS", "TEXT"), ("PRIM_OCC", "TEXT")], [(("Point", (0, 0)), {})])})
for slot, path in (("dwr", dwr), ("dam_lines", lines_p), ("waterbodies", wb), ("structures", bld)):
    cb = dlg.layer_combo[slot]
    cb.addItem(path, path)
    cb.setCurrentIndex(cb.count() - 1)
checks = dlg._preflight()
assert not pf.ready(checks) and "ACER TM 11 charts" in pf.summary(checks)      # not digitized in this build
dlg.crit.setCurrentIndex(2)                                                    # the conservative method
checks = dlg._preflight()
assert pf.ready(checks), pf.summary(checks)
assert any(c["title"] == "Dams without a dam line" and "1 of 2" in c["detail"] for c in checks)

# ---------------------------------------------------------------- 6. run one dam
calls = []


def fake_start(step, aid, params, on_done):
    calls.append((step, aid, dict(params), params.get("ONLY_IDS")))
    work1 = os.path.dirname(params.get("OUT") or params.get("OUTDIR") or params.get("OUTP") or params.get("WORK") or "")
    for param, rel in wf.LAYOUT[step].items():
        if param == "DAMS_FOLDER":
            continue
        pth = os.path.join(work1, rel)
        if os.path.splitext(pth)[1]:
            touch(pth)
        else:
            os.makedirs(pth, exist_ok=True)
            touch(pth, "x_%s.dat" % step)
    if step == "10":
        touch(work1, "10_reports", "index.html")
    on_done(True, "")


dlg._start_algorithm = fake_start
dlg._definitions = lambda aid: []
QInputDialog.NEXT.append("DWR-5290 Beta")
QtGui.OPENED_URLS.clear()
assert dlg._run_one_dam() is True and not dlg.chain.active and dlg.chain.ok, dlg.chain.result
one = wf.one_dam_work(W, "DWR-5290")
assert [c[0] for c in calls] == ["1", "2", "3", "4", "5", "7", "8", "10"]
assert calls[0][3] == "DWR-5290" and calls[0][2]["OUT"].startswith(one) and calls[0][2]["DWR"] == dwr
assert all(c[2].get(k, one).startswith(one) for c in calls for k in ("OUT", "OUTDIR") if k in c[2])
assert os.path.isfile(os.path.join(one, wf.REPORT_INDEX)) and wf.one_dam_runs(W) == [("DWR-5290", one)]
assert QtGui.OPENED_URLS and QtGui.OPENED_URLS[-1].endswith("index.html")
assert os.path.isfile(os.path.join(one, wf.RUN_SETTINGS))
assert dlg.one_dam_combo.count() == 1 and dlg.one_dam_report_btn.isEnabled()
# the choices offered: 1_dams.gpkg when it exists, else the DWR layer
assert wf.dam_choices(W, dlg.settings(), dlg._read_rows) == [("DWR-5289", "DWR-5289 Alpha"), ("DWR-5290", "DWR-5290 Beta")]
# a typed ID when nothing is listed
W2 = new_work("w2")
dlg.work_edit.setText(W2)
QInputDialog.NEXT.append("5289")
del calls[:]
assert dlg._run_one_dam() is True and calls[0][3] == "5289" and wf.one_dam_runs(W2)[0][0] == "5289"
dlg.work_edit.setText(W)

# ---------------------------------------------------------------- 7. the Review tab
W3 = new_work("w3")
dlg.work_edit.setText(W3)
DF = [("dam_id", "TEXT"), ("name", "TEXT"), ("pond_type", "TEXT"), ("pond_conf", "TEXT"), ("no_flood", "TEXT")]
_gpkg.write(wf.path(W3, "16", "OUT"), {"ponds": ("Point", DF, [
    (("Point", (-100.1, 47.1)), {"dam_id": "POND-1", "name": "", "pond_type": "embankment pond", "pond_conf": "low", "no_flood": ""}),
    (("Point", (-100.2, 47.2)), {"dam_id": "POND-2", "name": "", "pond_type": "dugout", "pond_conf": "medium", "no_flood": ""}),
    (("Point", (-100.3, 47.3)), {"dam_id": "POND-3", "name": "", "pond_type": "embankment pond", "pond_conf": "medium", "no_flood": ""})])})
BF = [("dam_id", "TEXT"), ("flow_az", "REAL"), ("dir_conf", "TEXT"), ("dir_method", "TEXT")]
_gpkg.write(wf.path(W3, "16", "OUTP"), {"bp": ("Point", BF, [
    (("Point", (100.0, 200.0)), {"dam_id": "POND-1", "flow_az": 90.0, "dir_conf": "medium", "dir_method": "rim"}),
    (("Point", (300.0, 400.0)), {"dam_id": "POND-3", "flow_az": 10.0, "dir_conf": "low", "dir_method": "centroid"})])})
HF = [("dam_id", "TEXT"), ("name", "TEXT"), ("hz_class", "TEXT"), ("hz_border", "TEXT"), ("hz_conf", "TEXT")]
_gpkg.write(wf.path(W3, "8", "OUT"), {"hz": ("Point", HF, [
    (("Point", (-100.3, 47.3)), {"dam_id": "POND-3", "name": "", "hz_class": "Medium", "hz_border": "yes", "hz_conf": "medium"})])})
dlg.refresh()
q = {r["dam_id"]: r for r in dlg.review_rows}
assert set(q) == {"POND-1", "POND-2", "POND-3"}
assert "pond type / crest uncertain" in q["POND-1"]["why"] and "dugout" in q["POND-2"]["why"]
assert "breach direction uncertain" in q["POND-3"]["why"] and "borderline class (Medium)" in q["POND-3"]["why"]
assert q["POND-3"]["source"] == "breach points, hazard" and dlg.review_table.rowCount() == 3
assert "3 dam(s) to look at, 0 decided" in dlg.review_label.text()
# decide: the reviewer's name is asked once, then written with the decision
dlg.review_table.setCurrentCell(1, 0)                      # POND-2
QInputDialog.NEXT.append("JD")
row = dlg._decide("exclude")
assert row["action"] == "exclude" and row["reviewer"] == "JD" and dlg.reviewer_edit.text() == "JD"
assert rv.excluded_ids(W3) == ["POND-2"] and os.path.isfile(rv.log_path(W3))
lyr = QgsVectorLayer(wf.path(W3, "16", "OUT"), "p", "ogr")
feats = {str(f["dam_id"]): f for f in lyr.getFeatures()}
assert feats["POND-2"]["rev_decision"] == "exclude" and feats["POND-2"]["rev_by"] == "JD" and feats["POND-1"]["rev_decision"] is None
assert q and dlg.review_rows[1]["decision"].startswith("excluded from the screening (JD")
# set the pond type
dlg.review_table.setCurrentCell(0, 0)                      # POND-1
dlg.type_combo.setCurrentIndex(1)
row = dlg._decide("set_type", dlg.type_combo.currentData())
feats = {str(f["dam_id"]): f for f in QgsVectorLayer(wf.path(W3, "16", "OUT"), "p", "ogr").getFeatures()}
assert feats["POND-1"]["pond_type"] == "dugout" and feats["POND-1"]["rev_decision"] == "set_type"
# move the breach point
dlg.review_table.setCurrentCell(2, 0)                      # POND-3
QInputDialog.NEXT.append("350.5, 420.25")
row = dlg._move_breach()
assert row["action"] == "move_breach"
bp = {str(f["dam_id"]): f for f in QgsVectorLayer(wf.path(W3, "16", "OUTP"), "b", "ogr").getFeatures()}
p3 = bp["POND-3"].geometry().asPoint()
assert abs(p3.x() - 350.5) < 1e-9 and abs(p3.y() - 420.25) < 1e-9 and bp["POND-3"]["dir_conf"] == "reviewed"
assert bp["POND-1"].geometry().asPoint().x() == 100.0
# a note and the log
QInputDialog.NEXT.append("looked at the 2024 imagery")
assert dlg._add_note()["action"] == "note"
log = rv.read_log(W3)
assert [r["action"] for r in log] == ["exclude", "set_type", "move_breach", "note"]
assert rv.latest_decisions(W3)["POND-3"]["action"] == "move_breach"
# the tools skip an excluded dam
from nd_dam_screening.algorithms import _common as cm  # noqa: E402
names = lyr.fields().names() + ["rev_decision", "rev_by", "rev_date", "rev_note"]
ex, why = cm.excluded(feats["POND-2"], QgsVectorLayer(wf.path(W3, "16", "OUT"), "p", "ogr").fields().names())
assert ex and why.startswith("excluded by review (JD")
assert not cm.excluded(feats["POND-1"], names)[0]
# nothing selected -> a message, no decision
dlg.review_table.setCurrentCell(-1, -1)
EVENTS["messages"].clear()
assert dlg._decide("accept") is None and "Select a dam" in EVENTS["messages"][-1][2]

# ---------------------------------------------------------------- 8. results as a group (plain add in the mock)
n = dlg._add_results()
assert n >= 2 and any("Added" in m for m in dlg.log_edit.lines)
# the help page exists and opens
QtGui.OPENED_URLS.clear()
assert dlg._open_help() is True and QtGui.OPENED_URLS[-1].endswith("how_it_decides.html")
html = open(os.path.join(os.path.dirname(pkg.__file__), "help", "how_it_decides.html"), encoding="utf-8").read()
assert "<svg" in html and "HEAD_FT" in html and "Not run in a real QGIS" in html
# the state survives a new window (project entry), including the settings file and reviewer
d2 = NDDamScreeningDialog()
assert d2.settings_file_edit.text() == sf and d2.reviewer_edit.text() == "JD" and d2.work_edit.text() == W3
assert d2.grid.value(("ndscreen", "findponds", "MAX_AC")) == 50       # the file was read at start
DS.set_current_path("", remember=True)
print("OK (popup 0.13: simple tab, grid + file, presets, preflight, one dam, review, results, help)")
