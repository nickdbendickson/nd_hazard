# -*- coding: utf-8 -*-
"""v2.10 UI: group 8 "Dam suite" on the Hazard Classification tab - the tools shared
with ND Dam Breach Runner and ND Dam Breach Screening (the dam.json record, the model
gate, class probability, quick creep screen, rules comparison, chart sign-off, the
reviewer's view, culvert lines and the Screening data package).

Every tool is a Processing algorithm of this plugin (ndhazard:...); the buttons open its
Processing dialog with the dam's folders filled in, so the engineer sees and can change
every parameter before it runs, and the same tool runs in batch, in models and from
qgis_process. The model-gate choices are settings of the classification run (cfg keys
model_gate / gate_stop_on_fail) and are saved with the project.

Dialog contract (what the host dialog provides): _log(msg), config_dam_folder,
config_output_folder. The lead adds _build_suite_group() to the tab, SETTING_WIDGETS_2100
to _SETTING_WIDGETS, BUSY_WIDGETS_2100 to _set_busy and **self._suite_config() to the
classification cfg.
"""

import os

from qgis.PyQt.QtWidgets import (
    QVBoxLayout, QHBoxLayout, QGridLayout, QPushButton, QLabel, QGroupBox, QCheckBox,
    QMessageBox,
)

from .helpers import log_message
from .compat import MSG_WARNING

# button attribute -> (label, Processing algorithm, tooltip)
TOOLS = (
    ("suite_gate_btn", "Model gate...", "modelgate",
     "Checks of the flood model before it is classified: flood at the model edge, no-data "
     "holes in the wet area, the ND Dam Breach Runner's mass balance, warnings and run "
     "manifest, velocities near the breach, units. PASS / WARN / FAIL with the reason."),
    ("suite_ensemble_btn", "Class probability...", "ensemble",
     "Classifies every run of a model ensemble (ND Dam Breach Runner: Probabilistic run "
     "with 'Write each run's rasters') with this dam's settings and data and gives "
     "P(High / Medium / Low). Only as good as the ensemble's parameter distributions."),
    ("suite_creep_btn", "Quick creep screen...", "quickcreep",
     "A NEW structures layer against every dam's classified flood: new structures in each "
     "flood area, 're-run recommended'. Nothing is re-classified."),
    ("suite_rules_btn", "Rules comparison...", "rulecompare",
     "Re-classifies this run's structures with the ND Dam Breach Runner / Screening rules "
     "(their nd_hazard.py) and explains every difference. This plugin stays the authority."),
    ("suite_charts_btn", "Chart sign-off...", "chartsignoff",
     "Writes the ACER TM 11 chart file the Runner and Screening need, from this plugin's "
     "curves, with overlays on your scans of Figs 2 and 3; 'digitized' only when two "
     "different people are named. The tool records the names - it cannot confirm the check."),
    ("suite_review_btn", "Review a package...", "reviewpackage",
     "Reviewer's view of a submittal package: every file against MANIFEST.csv, the model "
     "chain, optional reproduction and a checklist. Nothing is written into the package."),
    ("suite_culverts_btn", "Culverts for the Runner...", "runnerculverts",
     "Culvert / bridge lines (upstream end first) for the ND Dam Breach Runner from the NBI, "
     "OpenStreetMap and NDDOT structures where a stream crosses a road or railroad. Unknown "
     "sizes stay empty."),
    ("suite_export_btn", "Export for Screening...", "suiteexport",
     "One GeoPackage in the ND Dam Breach Screening tool-0 schema from the offline pack or "
     "your downloads: structures with this plugin's habitability, roads with AADT, "
     "railroads, flowlines, lifelines, NID dams; for this dam folder also its NHD "
     "waterbodies, DWR dam lines / dams and DEM path (Get data for this dam)."),
    # v2.12
    ("suite_reviewed_btn", "Mark reviewed...", "markreviewed",
     "The reviewer's sign-off: records reviewer, decision (Accepted / Accepted with comments "
     "/ Returned), comments, open items and the SHA-256 of the reviewed package's "
     "MANIFEST.csv as the 'reviewed' status in dam.json, and a line in QA/Review_Log.csv. "
     "No other value in dam.json changes. The dashboard shows 'review' while items are open."),
)


def _newest_package(out_folder):
    """The newest *.zip in <outputs>/Submittal (submittal.py's packages), or ''."""
    try:
        from .submittal import SUBMITTAL_DIR
    except Exception:                                        # noqa
        SUBMITTAL_DIR = "Submittal"
    folder = os.path.join(out_folder, SUBMITTAL_DIR)
    try:
        zips = [os.path.join(folder, f) for f in os.listdir(folder) if f.lower().endswith(".zip")]
    except OSError:
        return ""
    if not zips:
        return ""
    return max(zips, key=lambda f: os.path.getmtime(f))


class SuiteTabMixin(object):
    """Group 8 of the Hazard Classification tab (v2.10)."""

    SETTING_WIDGETS_2100 = ("suite_gate_check", "suite_gate_stop_check")
    BUSY_WIDGETS_2100 = tuple(t[0] for t in TOOLS) + ("suite_gate_check",
                                                       "suite_gate_stop_check",
                                                       "suite_record_btn")

    def _build_suite_group(self):
        grp = QGroupBox("8. Dam suite - ND Dam Breach Runner / Screening (v2.10)")
        lay = QVBoxLayout()
        info = QLabel(
            "Tools shared with ND Dam Breach Runner and ND Dam Breach Screening. Each opens its "
            "Processing dialog with this dam's folders filled in. Every threshold is a "
            "screening value YOU choose; none is a requirement of REG_05.2026 or TM 11.")
        info.setWordWrap(True)
        lay.addWidget(info)
        self.suite_gate_check = QCheckBox("Model gate before every classification run "
                                          "(QA/Model_Gate.csv)")
        self.suite_gate_check.setToolTip(TOOLS[0][3])
        self.suite_gate_stop_check = QCheckBox("Stop the run when the gate FAILs")
        self.suite_gate_stop_check.setToolTip(
            "Off: the gate's findings are written and the classification goes on. On: a FAIL "
            "stops the run before anything is downloaded.")
        self.suite_gate_stop_check.setEnabled(False)
        self.suite_gate_check.toggled.connect(self.suite_gate_stop_check.setEnabled)
        lay.addWidget(self.suite_gate_check)
        lay.addWidget(self.suite_gate_stop_check)
        grid = QGridLayout()
        for i, (attr, label, alg, tip) in enumerate(TOOLS):
            btn = QPushButton(label)
            btn.setToolTip(tip + "\n(Processing: ndhazard:{})".format(alg))
            btn.clicked.connect(lambda _c=False, a=alg: self._open_suite_tool(a))
            setattr(self, attr, btn)
            grid.addWidget(btn, i // 4, i % 4)
        # 2.17.0: the Suite data and servers window (suite_data.py) on this dam folder
        self.suite_data_btn = QPushButton("Data and servers...")
        self.suite_data_btn.setToolTip(
            "Plugins > ND Dam Suite > Suite data and servers, on this dam folder: the suite "
            "data folder (offline pack, terrain cache), the server of each kind of data and "
            "'Get data for this dam' (<dam folder>/data/; the Data & Output tab then fills "
            "the data sets you left empty from there).")
        self.suite_data_btn.clicked.connect(self._open_suite_data)
        grid.addWidget(self.suite_data_btn, len(TOOLS) // 4, len(TOOLS) % 4)
        lay.addLayout(grid)
        row = QHBoxLayout()
        self.suite_record_label = QLabel("dam.json: -")
        self.suite_record_label.setWordWrap(True)
        self.suite_record_btn = QPushButton("dam.json...")
        self.suite_record_btn.setToolTip(
            "The dam's record shared by the three plugins: values, which tool set them, the "
            "status (screened / modelled / classified / reviewed), values the tools did not "
            "agree on, and the next action.")
        self.suite_record_btn.clicked.connect(self._show_dam_record)
        row.addWidget(self.suite_record_label, 1)
        row.addWidget(self.suite_record_btn)
        lay.addLayout(row)
        grp.setLayout(lay)
        return grp

    # ----------------------------------------------------------------- cfg --
    def _suite_config(self):
        """cfg keys of the classification run (pipeline.DEFAULTS from modelgate)."""
        gate = self.__dict__.get("suite_gate_check")
        stop = self.__dict__.get("suite_gate_stop_check")
        on = bool(gate is not None and gate.isChecked())
        return {"model_gate": on,
                "gate_stop_on_fail": bool(on and stop is not None and stop.isChecked())}

    # --------------------------------------------------------------- tools --
    def _suite_defaults(self, alg):
        """Parameters filled in for algorithm `alg` from the dialog's folders."""
        dam = getattr(self, "config_dam_folder", "") or ""
        out = getattr(self, "config_output_folder", "") or ""
        p = {}
        if alg in ("modelgate", "ensemble", "markreviewed", "suiteexport") and dam:
            p["DAM_FOLDER"] = dam                      # 2.17.0: suiteexport too
        if alg == "ensemble" and dam:
            for name in ("Runs", "runs"):
                cand = os.path.join(dam, "Probabilistic", name)
                if os.path.isdir(cand):
                    p["ENSEMBLE_FOLDER"] = cand
                    break
        if alg == "quickcreep" and dam:
            p["PARENT"] = os.path.dirname(os.path.abspath(dam))
        if alg == "rulecompare" and out:
            p["OUTPUT_FOLDER"] = out
        if alg == "chartsignoff" and out:
            p["OUTPUT"] = os.path.join(out, "QA", "Chart_SignOff")
        if alg == "reviewpackage" and out:
            p["WORK_FOLDER"] = os.path.join(out, "Package_Review")
        if alg == "markreviewed" and out:
            # the newest submittal package zip next to the outputs, when there is one
            pkg = _newest_package(out)
            if pkg:
                p["PACKAGE"] = pkg
        return p

    def _open_suite_tool(self, alg):
        """Open the Processing dialog of ndhazard:<alg> with the folders filled in."""
        aid = "ndhazard:" + alg
        params = self._suite_defaults(alg)
        try:
            import processing
            processing.execAlgorithmDialog(aid, params)
            self._log(">>> Opened {} ({})".format(aid, ", ".join(
                "{}={}".format(k, v) for k, v in sorted(params.items())) or "no values filled"))
            return True
        except Exception as e:                                   # noqa
            log_message("{}: {}".format(aid, e), MSG_WARNING)
            QMessageBox.information(
                self, "Dam suite",
                "The Processing dialog of {} could not be opened here ({}).\n\nOpen it from "
                "the Processing Toolbox: ND Dam Hazard > {}.".format(aid, e, alg))
            return False

    def _open_suite_data(self, *args):
        """2.17.0: the Suite data and servers window on this dam folder - the same window as
        the menu entry (it is kept on the plugin object when QGIS knows it)."""
        from . import suite_data
        holder, iface = self, None
        try:
            import qgis.utils as QU
            holder = QU.plugins.get(__name__.split(".")[0]) or self
            iface = getattr(holder, "iface", None) or QU.iface
        except Exception:                                        # noqa
            pass
        parent = self
        try:
            if holder is not self and iface is not None:
                parent = iface.mainWindow()
        except Exception:                                        # noqa
            parent = self
        dam = getattr(self, "config_dam_folder", "") or ""
        try:
            return suite_data.open_suite_data(holder, iface, "ndhazard", parent,
                                              dam_folder=dam if os.path.isdir(dam) else "")
        except Exception as e:                                   # noqa
            log_message("Suite data and servers: {}".format(e), MSG_WARNING)
            QMessageBox.information(
                self, "Dam suite", "The Suite data and servers window could not be opened "
                "({}).\n\nOpen it from Plugins > ND Dam Suite.".format(e))
            return None

    # -------------------------------------------------------------- record --
    def _dam_record_text(self):
        """(one-line label, full text) of the dam folder's dam.json."""
        dam = getattr(self, "config_dam_folder", "") or ""
        if not dam or not os.path.isdir(dam):
            return "dam.json: no dam folder", ""
        try:
            from .damsuite import dam_record as DR
            rec = DR.load(dam)
        except ValueError as e:
            return "dam.json: cannot be read ({})".format(e), str(e)
        except Exception as e:                                   # noqa
            return "dam.json: -", str(e)
        if rec is None:
            return ("dam.json: none yet (written by the next classification run, the Runner "
                    "or Screening tool 7b)"), ""
        code, why = DR.next_action(rec)
        steps = []
        for step in DR.STEPS:
            e = rec.latest(step)
            if e:
                steps.append("{} {} ({})".format(step, (e.get("when") or "")[:10],
                                                 e.get("tool") or "?"))
        open_conf = [c for c in rec.conflicts if not c.get("applied")]
        try:                                                 # v2.12: the review's open items
            from . import markreviewed as MR
            facts = MR.review_facts(rec)
            if facts:
                code, why = DR.next_action(rec, facts)
        except Exception:                                    # noqa
            facts = {}
        label = "dam.json: {}; next: {}{}".format(
            ", ".join(steps) or "no status", code.upper(),
            " - {} value(s) disputed".format(len(open_conf)) if open_conf else "")
        lines = ["Dam record: {}".format(rec.path), "", "Next action: {} - {}".format(
            code.upper(), why)]
        if facts:
            lines.append(MR.summary_line(rec))
        lines += ["", "Values:"]
        for name in DR.FIELDS:
            v = rec.get(name)
            if v in (None, ""):
                continue
            src = rec.sources.get(name) or {}
            lines.append("  {} = {}   ({} {}{})".format(
                name, v, src.get("tool", "?"), src.get("version", ""),
                "; " + src["note"] if src.get("note") else ""))
        if rec.status:
            lines += ["", "Status:"]
            for e in rec.status:
                res = e.get("result") or {}
                lines.append("  {} {} by {} {}{}".format(
                    (e.get("when") or "")[:16], e.get("step"), e.get("tool"),
                    e.get("version", ""), " (preview)" if isinstance(res, dict) and
                    res.get("preview") is True else ""))
        if open_conf:
            lines += ["", "Values the tools did not agree on (not applied):"]
            for c in open_conf:
                lines.append("  {}: kept {} ({}), offered {} ({})".format(
                    c.get("field"), c.get("old"), (c.get("old_source") or {}).get("tool", "?"),
                    c.get("new"), (c.get("new_source") or {}).get("tool", "?")))
        return label, "\n".join(lines)

    def _refresh_dam_record(self, *args):
        label = self.__dict__.get("suite_record_label")
        if label is None:
            return
        text, _full = self._dam_record_text()
        label.setText(text)

    def _show_dam_record(self, *args):
        text, full = self._dam_record_text()
        self._refresh_dam_record()
        QMessageBox.information(self, "dam.json", full or text)
