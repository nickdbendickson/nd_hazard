"""GUI port (stage 2a) in the ND Dam Breach Screening window (dialog.py): the additive display
pieces brought over from DamSafety Screening WITHOUT removing or restructuring any nd_ tab.

What is checked here is only the wiring of the new shell, and that nothing nd_ already had was
dropped:
  (a) the pipeline strip Data -> DEM -> Screen -> Report exists and its per-stage glyph / colour /
      state FOLLOW nd_'s own wf.statuses (and the report file);
  (b) the work folder + county header is folded into a Workspace group, county_combo kept;
  (c) the log is folded to one line with a Show log toggle, and step_progress is preserved.
And the hard "do not regress" constraints: the Priority and Inputs tabs, the Results 'Flags'
column, both_scenarios / both_one_dam, the run-time estimate and found-layers labels, nd_'s window
title and nd_'s _scroll_tab wrapping (NOT DamSafety's ui_fit / wrap_tabs / title), the Settings tab
left in place.

Real PyQt5 is not importable in this environment (PyQt5.sip is missing), so - exactly as every
other window test here does - this builds the REAL dialog against the shipped qgis_mock (a
behaviour-recording Qt double) and runs it offscreen. Run:

  QT_QPA_PLATFORM=offscreen \
    PYTHONPATH=tests/qgis_mock:<parent of the plugin> python3 tests/test_ui_shell.py

It is flavor-clean: FAKE_QGIS_FLAVOR=3.28 (Qt5 legacy), 3.40 (Qt5 scoped) and 4.0 (Qt6) all pass,
because the ported pieces use no Qt enums.
"""
import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["HOME"] = tempfile.mkdtemp(prefix="ui_shell_home_")    # no suite config of another run
for _k in ("NDSUITE_DATA", "NDSUITE_DAMS"):
    os.environ.pop(_k, None)

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(HERE, "qgis_mock"))
sys.path.insert(0, os.path.dirname(PLUGIN))
sys.path.insert(0, HERE)

import _gpkg  # noqa: E402
import nd_dam_screening as pkg  # noqa: E402
from nd_dam_screening.core import preflight as pf, workflow as wf  # noqa: E402
from nd_dam_screening.damsuite import settings as DS  # noqa: E402
import nd_dam_screening.dialog as D  # noqa: E402
from nd_dam_screening.dialog import (DAM_COLUMNS, NDDamScreeningDialog, PIPELINE, PIPE_MARK,
                                     PIPE_COLOR, TITLE)  # noqa: E402

ROOT = tempfile.mkdtemp(prefix="ui_shell_")
pf.Env.url_ok = lambda self, url: (True, "stubbed")               # no network in the tests
DS.set_current_path("", remember=True)
plug = pkg.classFactory(None)
plug.initGui()
for a in plug.provider.algs:
    a.initAlgorithm()


def _by(dlg):
    """The step->state dict straight from nd_'s wf.statuses, the SAME source the strip reads."""
    work = dlg.work_edit.text().strip()
    return {k: st for k, _t, st, _n in wf.statuses(work, dlg.settings())}


# ====================================================================== 0. the dialog builds
dlg = NDDamScreeningDialog()
print("OK (0: the dialog builds without error)")

# ======================================================= 1. the pipeline strip exists, 4 stages
assert set(dlg.pipe_btn) == {"data", "dem", "screen", "report"}, sorted(dlg.pipe_btn)
assert [k for k, _l, _s, _t in PIPELINE] == ["data", "dem", "screen", "report"]
for key, label, _step, _tab in PIPELINE:              # 0.32.0: mark before the word
    assert dlg.pipe_btn[key].text().endswith(label), dlg.pipe_btn[key].text()
print("OK (1: Data -> DEM -> Screen -> Report buttons exist)")

# ============================================= 2. with no work folder every stage reads 'waiting'
#   (data/dem/screen are non-optional steps, so wf.statuses' fallback is 'waiting'; report has no
#   file yet). The strip's glyph is PIPE_MARK of that state.
assert dlg.pipe_state == {"data": "waiting", "dem": "waiting", "screen": "waiting",
                          "report": "waiting"}, dlg.pipe_state
for key, _l, _s, _t in PIPELINE:
    assert dlg.pipe_btn[key].text().startswith(PIPE_MARK["waiting"]), dlg.pipe_btn[key].text()
print("OK (2: no work folder -> every stage waiting, glyph follows PIPE_MARK)")

# ============================================= 3. the stage state FOLLOWS wf.statuses on a real run
W = os.path.join(ROOT, "work")
os.makedirs(W)
dlg.work_edit.setText(W)
dlg.refresh()
by = _by(dlg)
# the three step-backed stages mirror wf.statuses exactly
for key, _label, step, _tab in PIPELINE:
    if step is not None:
        assert dlg.pipe_state[key] == by[step], (key, dlg.pipe_state[key], by.get(step))
        assert dlg.pipe_btn[key].text().startswith(PIPE_MARK.get(by[step], "\u25cb"))

# write step 1's output: wf.statuses now calls step 1 done, and the Data stage must move with it
_gpkg.write(wf.path(W, "1", "OUT"),
            {"dams": ("POINT", [("dam_id", "TEXT")],
                      [(("Point", (0.0, 0.0)), {"dam_id": "DWR-1"})])})
dlg.refresh()
by = _by(dlg)
assert by["1"] == "done", by["1"]                  # sanity: wf.statuses agrees
assert dlg.pipe_state["data"] == "done", dlg.pipe_state
assert dlg.pipe_btn["data"].text() == PIPE_MARK["done"] + " Data", dlg.pipe_btn["data"].text()
assert "step 1" in dlg.pipe_btn["data"].toolTip() and "go to the tab" in dlg.pipe_btn["data"].toolTip()
# the stages that are still waiting did not move
assert dlg.pipe_state["dem"] == by["2"] and dlg.pipe_state["screen"] == by["7"]
print("OK (3: stage state follows wf.statuses; Data -> done after step 1)")

# ========================================================== 4. a running chain shows 'running'
class _FakeChain(object):
    active = True
    current = "7"


dlg.chain = _FakeChain()
dlg._refresh_pipeline(_by(dlg), busy=True, have=True, work=W)
assert dlg.pipe_state["screen"] == "running", dlg.pipe_state
assert dlg.pipe_btn["screen"].text() == PIPE_MARK["running"] + " Screen"
dlg.chain = None
print("OK (4: a running step 7 lights the Screen stage)")

# ========================================================== 5. the Report stage follows its file
report = os.path.join(W, wf.REPORT_INDEX)
os.makedirs(os.path.dirname(report), exist_ok=True)
open(report, "w").write("<html></html>")
dlg.refresh()
assert dlg.pipe_state["report"] == "done", dlg.pipe_state
assert dlg.pipe_btn["report"].text() == PIPE_MARK["done"] + " Report"
os.remove(report)
dlg.refresh()
assert dlg.pipe_state["report"] == "waiting", dlg.pipe_state
print("OK (5: Report stage tracks index.html)")

# =============================================== 6. _goto_stage / gear shortcut select real tabs
titles = list(dlg.tabs.titles)
assert dlg._select_tab("Settings") is True                       # the gear shortcut's target exists
assert dlg._select_tab("Nope") is False
dlg._goto_stage("report")                                        # must not raise; owns the Results tab
dlg._goto_stage("data")
print("OK (6: tab shortcuts resolve by title)")

# ============================================= 7. the Workspace group folds nd_'s header, kept
#   (county_combo is nd_'s own and still present and wired)
assert dlg.county_combo.count() >= 1
assert hasattr(dlg, "work_edit") and hasattr(dlg, "work_browse") and hasattr(dlg, "work_open")
assert hasattr(dlg, "help_btn")
print("OK (7: workspace header folded, county_combo kept)")

# ============================================================ 8. the folded log + the toggle
assert dlg.log_section.is_expanded() is False                    # starts folded (Collapsible)
assert dlg.log_line.text() == "Log: -"
dlg._log("first line\nsecond and last line")
assert dlg.log_line.text() == "Log: second and last line", dlg.log_line.text()
dlg._toggle_log()
assert dlg.log_section.is_expanded() is True
dlg._toggle_log()
assert dlg.log_section.is_expanded() is False
assert hasattr(dlg, "step_progress")                             # the progress bar is preserved
print("OK (8: the log folds to one line and toggles; step_progress kept)")

# ========================= 9. NONE of nd_'s richer tabs / features were removed (hard constraint)
for t in ("Simple", "All steps", "Inputs", "Settings", "Review", "Results", "Priority"):
    assert t in titles, (t, titles)
assert hasattr(dlg, "priority_table") and hasattr(dlg, "_build_priority_tab")   # statewide Priority
assert "Flags" in DAM_COLUMNS                                    # Results 'Flags' column
assert hasattr(dlg, "both_scenarios") and hasattr(dlg, "both_one_dam")          # both-loading-case
assert hasattr(dlg, "estimate_label") and hasattr(dlg, "found_label")           # the extra labels
assert hasattr(dlg, "dam_flags_label")                                          # resume/incomplete red
print("OK (9: Priority, Inputs, Flags, both-loading-case, estimate/found labels all intact)")

# ===================================== 10. nd_'s title + nd_'s _scroll_tab kept; DamSafety's NOT
assert TITLE == "ND Dam Breach Screening", TITLE
assert hasattr(D, "_scroll_tab")                                 # nd_'s wrapping
assert not hasattr(D, "wrap_tabs") and not hasattr(D, "fit_window")   # DamSafety's ui_fit not adopted
assert not hasattr(D, "window_title")                            # DamSafety's title helper not adopted
print("OK (10: nd_ title + _scroll_tab kept; DamSafety ui_fit/title not adopted)")

print("\nALL OK - GUI port stage 2a: additive display, no nd_ tab removed")
