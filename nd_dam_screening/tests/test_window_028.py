"""0.28.0 in the window: Run county / Run one dam, the time estimate, the reason a button is
grey, the dam table's flags and the statewide Priority tab (dialog.py).

The logic these fit together is tested without Qt in tests/test_v0280.py (core/runplan.py,
core/resume.py, core/incomplete.py, core/preflight.py, damsuite/priority.py). What is tested here
is only the wiring: that the window asks those modules and shows what they answer, and that no
callback raises - a dam safety engineer gets a wrong answer from a window that stays silent, not
from one that throws.

Run: FAKE_QGIS_FLAVOR=3.40 PYTHONPATH=tests/qgis_mock:<parent of the plugin> python3 tests/test_window_028.py
"""
import configparser
import os
import sys
import tempfile

os.environ["HOME"] = tempfile.mkdtemp(prefix="w028_home_")        # no suite config of another run
for _k in ("NDSUITE_DATA", "NDSUITE_DAMS"):
    os.environ.pop(_k, None)

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(HERE, "qgis_mock"))
sys.path.insert(0, os.path.dirname(PLUGIN))
sys.path.insert(0, HERE)

import _gpkg  # noqa: E402
from qgis.PyQt.QtWidgets import QFileDialog  # noqa: E402
import nd_dam_screening as pkg  # noqa: E402
from nd_dam_screening.core import preflight as pf, resume as rs, runplan as rp, workflow as wf  # noqa: E402
from nd_dam_screening.damsuite import dam_record as DR, priority as PR  # noqa: E402
from nd_dam_screening.damsuite import settings as DS, suite_home as SH  # noqa: E402
from nd_dam_screening.dialog import DAM_COLUMNS, NDDamScreeningDialog, RED  # noqa: E402

ROOT = tempfile.mkdtemp(prefix="w028_")
pf.Env.url_ok = lambda self, url: (True, "stubbed")               # no network in the tests
DS.set_current_path("", remember=True)
plug = pkg.classFactory(None)
plug.initGui()
for a in plug.provider.algs:
    a.initAlgorithm()

W = os.path.join(ROOT, "work")
os.makedirs(W)
dlg = NDDamScreeningDialog()
dlg.work_edit.setText(W)
dlg.refresh()

# ============================================ 5. "Run one dam" beside "Run county", in one place
# "Run all" read like "run all the tools" and sat on its own: the two buttons an engineer uses are
# now the same row of the Simple tab, named after what they screen.
assert dlg.run_all_btn._text == "Run county"
assert dlg.one_dam_btn._text == "Run one dam..." and dlg.sel_dams_btn._text == "Run selected dams..."
assert "one_dam" in dlg.one_dam_btn.toolTip() and "both loading cases" in dlg.one_dam_btn.toolTip()
assert "county" in dlg.run_all_btn.toolTip().lower()
print("OK (5: Run county and Run one dam, side by side)")

# ================================================= 6. how long it will take, before it is started
# nothing screened yet: the window says what to do to get an estimate instead of guessing
assert "Run step 1" in dlg.estimate_label.text()
pt0 = ("Point", (0.0, 0.0))
_gpkg.write(wf.path(W, "1", "OUT"), {"dams": ("POINT", [("dam_id", "TEXT")],
                                      [(pt0, {"dam_id": "DWR-{}".format(i)}) for i in range(1, 13)])})
dlg.refresh()
est = dlg.estimate_label.text()
assert wf.dam_count(W) == 12 and est.startswith("12 dam(s) x about ")
assert "164 ft (50 m)" in est and "= about " in est, est
assert dlg.estimate_label.toolTip() == rp.cell_label(dlg.settings())
# the estimate follows the choices that cost the time, and the label says which ones
dlg.both_scenarios.setChecked(True)
dlg.refresh()
both = dlg.estimate_label.text()
assert "both scenarios" in both and both != est
dlg.both_scenarios.setChecked(False)
# an interrupted county: only the dams that are left are estimated, and it says so
outdir = wf.path(W, "7", "OUTDIR")
done = os.path.join(outdir, "d.tif")
os.makedirs(outdir, exist_ok=True)
open(done, "w").write("x")
for i in (1, 2, 3):
    rs.record(outdir, "DWR-{}".format(i), "ok", scenario="td", rasters=[done])
dlg.refresh()
left = dlg.estimate_label.text()
assert "9 already have a result and are not re-run" not in left                 # 12 - 3 = 9 to run
assert "3 already have a result and are not re-run" in left and "Resuming: " in left
dlg.skip_done.setChecked(False)                      # forced re-run: all 12 again, and it says so
dlg.refresh()
again = dlg.estimate_label.text()
assert "are not re-run" not in again and again.startswith("12 dam(s) x about ")
assert "RE-RUN" in again and "Resuming" not in again
dlg.skip_done.setChecked(True)
print("OK (6: the time estimate, and what an interrupted run has left)")

# ========================================================= 7. a grey Run button says why it is grey
empty = NDDamScreeningDialog()
empty.work_edit.setText("")
empty.refresh()
assert not empty.run_all_btn.isEnabled()
why = empty.run_all_btn.toolTip()
assert why.startswith("Cannot run: ") and "no work folder chosen" in why
assert empty.hint_label.text().startswith("Choose or create a work folder first.")
assert "Also: Suite folder: not set" in empty.hint_label.text()      # what it would stop on next
assert empty.simple_hint.text() == empty.hint_label.text()
assert empty.simple_hint._style == RED
for b in (empty.chain_btn, empty.one_dam_btn, empty.sel_dams_btn):
    assert b.toolTip() == why, b.text()
gone = NDDamScreeningDialog()
gone.work_edit.setText(os.path.join(ROOT, "not_there"))
gone.refresh()
assert "does not exist" in gone.run_all_btn.toolTip()
# a work folder that is there: the button works again and the tooltip is about the button
assert dlg.run_all_btn.isEnabled() and not dlg.run_all_btn.toolTip().startswith("Cannot run")
assert dlg.hint_label.text().startswith("Next: ")
print("OK (7: the reason a button is grey, in the tooltip and the status line)")

# ================================= 1 (window side). the flags on the Results tab's dam table
_gpkg.write(wf.path(W, "8", "OUT"), {"class": ("POINT", [("dam_id", "TEXT"), ("name", "TEXT"),
                                                 ("hz_class", "TEXT"), ("hz_conf", "TEXT"),
                                                 ("store_af", "DOUBLE"), ("sim_status", "TEXT"),
                                                 ("edge_flag", "TEXT"), ("sim_notes", "TEXT")],
                                       [(pt0, {"dam_id": "DWR-1", "name": "Truncated", "hz_class": "Medium",
                                               "hz_conf": "review", "store_af": 3937.0,
                                               "sim_status": "ok", "edge_flag": "YES", "sim_notes": ""}),
                                        (pt0, {"dam_id": "DWR-2", "name": "Whole", "hz_class": "Low",
                                               "hz_conf": "medium", "store_af": 120.0,
                                               "sim_status": "ok", "edge_flag": "no", "sim_notes": ""})])})
dlg.refresh()
t = dlg.dam_table
assert len(DAM_COLUMNS) == 6 and DAM_COLUMNS[5] == "Flags"   # 0.28.0: the new column
rows = dict((t.item(r, 0).text(), r) for r in range(t.rowCount()))
assert set(rows) == {"DWR-1", "DWR-2"}
flag = t.item(rows["DWR-1"], 5)
assert flag.text() == "flood truncated at the terrain edge"
assert "were not counted" in flag.toolTip()          # the whole reason, not only the first words
assert t.item(rows["DWR-2"], 5).text() == ""
assert t.item(rows["DWR-1"], 3).text() == "3,937" and t.item(rows["DWR-1"], 2).text() == "Medium"
assert dlg.dam_flags_label.text().startswith("1 of 2 dam(s) incomplete")
assert dlg.dam_flags_label._style == RED
# the same flag on the Simple tab's step line, in red, while the run is still on the screen
assert "1 of 2 dam(s) incomplete" in dlg.simple_note["8"].text()
assert dlg.simple_note["8"]._style == RED
print("OK (1: the truncated flood is on the screen, not only in the CSV)")

# ===================================================== 8. the statewide priority list (the tab)
dams = os.path.join(ROOT, "Dams")


def dam(did, name, computed, on_record, note=""):
    """A dam folder as the suite leaves one: dam.json with a computed class, and the flood raster
    that says the class came from a model (without it every dam is "no model run")."""
    folder = os.path.join(dams, did)
    os.makedirs(folder, exist_ok=True)
    rec = DR.load(folder, create=True)
    rec.set_many({"dam_id": did, "name": name, "existing_hazard_class": on_record},
                 "test", "0.28.0")
    rec.add_status("classified", "ndscreen", "0.28.0",
                   {"hazard_class": computed, "hz_notes": note})
    rec.save()
    open(os.path.join(folder, SH.FINAL_DEPTH), "w").write("x")
    return folder


dam("DWR-1", "Screens higher", "High", "Low")                       # a raise: the top of the list
dam("DWR-2", "Agrees", "Medium", "Significant")                     # ND Medium == DWR Significant
dam("DWR-3", "Cut off", "Medium", "Medium", "flood truncated at the terrain edge")
dlg.priority_edit.setText(dams)
rows = dlg.refresh_priority()
assert [r["dam_id"] for r in rows] == ["DWR-1", "DWR-3", "DWR-2"], [r["dam_id"] for r in rows]
assert rows[0]["verdict"] == "raise" and rows[1]["verdict"] == "incomplete"
assert rows[2]["verdict"] == "agree"                 # "Medium" against "Significant" is agreement
pt = dlg.priority_table
assert pt.rowCount() == 3 and len(PR.COLUMNS) > 5
assert [pt.item(0, i).text() for i in (0, 1)] == ["DWR-1", "Screens higher"]
assert dlg.priority_label.text().startswith(PR.summary_text(rows)[:20])
assert dams in dlg.priority_label.text() and dlg.priority_export_btn.isEnabled()
# the whole folder, not this run's county: these three dams are in no work folder of this test
assert len(PR.attention(rows)) == 2
# Excel export: every dam on one sheet, the review list on another (damsuite.priority.write_xlsx)
xl = os.path.join(ROOT, "priority.xlsx")
QFileDialog.NEXT.append(xl)
assert dlg._export_priority() is True and os.path.isfile(xl)
import zipfile  # noqa: E402
with zipfile.ZipFile(xl) as z:
    book = z.read("xl/workbook.xml").decode("utf-8")
assert "Priority" in book and "Review list" in book
# a folder that is not there is a message, never a crash; and the tab survives it
dlg.priority_edit.setText(os.path.join(ROOT, "nothing"))
assert dlg.refresh_priority() == [] and dlg.priority_table.rowCount() == 0
assert "No Dams folder yet" in dlg.priority_label.text()
assert not dlg.priority_export_btn.isEnabled()
QFileDialog.NEXT.append(os.path.join(ROOT, "never.xlsx"))
assert dlg._export_priority() is False and not os.path.isfile(os.path.join(ROOT, "never.xlsx"))
dlg.priority_edit.setText("")
assert dlg.priority_folder() == wf.dams_folder(W, dlg.settings())
print("OK (8: the statewide priority list and its spreadsheet)")

# ================================================================= the release this all belongs to
cfg = configparser.ConfigParser(interpolation=None)         # a changelog with a '%' in it
cfg.read(os.path.join(PLUGIN, "metadata.txt"), encoding="utf-8")
ver = cfg["general"]["version"]
assert ver == "0.30.0", ver
log = cfg["general"]["changelog"].lstrip()
assert log.startswith(ver + ":") and "0.28.0:" in log, log[:40]
# the release notes must say which results move, and in which direction - the suite's own rule
assert "MOVES RESULTS" in log and "conservative direction only" in log
assert "0.27.0:" in log                                     # the older entries are still there
print("OK (metadata: 0.28.0 with a changelog entry)")
print("All window 0.28.0 tests passed.")
