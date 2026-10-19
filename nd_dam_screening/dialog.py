"""The ND Dam Breach Screening popup window (Plugins > ND Dam Suite > ND Dam Breach Screening).

0.13: a Simple tab (work folder, what to screen, terrain, engine, criterion, settings file, the
steps in plain words, Check before running, Run all, Run one dam), the settings grid (every
engineer-chosen value in one table backed by the shared settings file), a Review tab (the dams
that need a look, with the reviewer's decisions written into the layers), and the results as one
layer group. The All steps tab is the 0.12 window: a button per tool that opens its Processing
dialog with the parameters filled in.

The logic (file layout, step state, parameters, preflight, review) is core/workflow.py,
core/preflight.py and core/review.py; this file only holds the widgets. The window's choices are
saved with the QGIS project; the shareable ones also go to the settings file.
"""
import json
import os

from qgis.PyQt.QtCore import QUrl
from qgis.PyQt.QtGui import QDesktopServices
from qgis.PyQt.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QGridLayout, QTabWidget, QWidget, QLabel, QLineEdit,
    QPushButton, QComboBox, QSpinBox, QDoubleSpinBox, QTextEdit, QGroupBox, QFileDialog, QMessageBox,
    QCheckBox, QRadioButton, QTableWidget, QTableWidgetItem, QInputDialog, QScrollArea, QProgressBar,
    QHeaderView, QAbstractItemView,
)

from .core import workflow as wf
from .core import settings_groups as SG
from .core import preflight as pf
from .core import review as rv
from .core import incomplete as ic          # 0.28.0: the run flags, in red
from .core import resume as rs              # 0.28.0: what an interrupted run got through
from .core import runplan as rp             # 0.28.0: the cell size and the time estimate
from .damsuite import settings as DS
from .damsuite import priority as PR         # 0.28.0: the statewide review list

TITLE = "ND Dam Breach Screening"
SCOPE = "NDDamScreening"          # QgsProject entry scope
ENTRY = "state"
STATE_TEXT = {"done": "✓ done", "stale": "↻ out of date", "ready": "● ready",
              "waiting": "… waiting", "optional": "○ optional"}
DISCLAIMER = ("Screening-level results for prioritising reviews. Every threshold is a screening "
              "value you choose; the official hazard classification is the North Dakota "
              "Department of Water Resources'. Nothing here has been checked against the State's "
              "forms.")
SCREEN_WHAT = ("DWR dam inventory (steps 1-4)", "Ponds and dugouts from a water layer (step 16)",
               "Both (steps 1-4 and 16, merged)")
HELP_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "help", "how_it_decides.html")
PRESETS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "presets")
REVIEW_COLUMNS = ("Dam", "Name", "Why", "From", "Decision")
# 0.28.0: the Results tab's dam table gains the run flags, and the Priority tab is the statewide
# review list (damsuite.priority)
DAM_COLUMNS = ("Dam ID", "Name", "Class", "Storage (ac-ft)", "Confidence", "Flags")
RED = "color: #b00020"
FOLLOW_TERRAIN_LABEL = "Follow the terrain source (recommended)"
RUN_COUNTY_TIP = ("Runs every step that is not done, in order, in the background, for every dam of "
                  "the chosen county (or of the whole work folder), with the values of this window "
                  "and the settings file. Stops at the first problem. An interrupted run carries on "
                  "where it stopped. One dam instead: 'Run one dam...' beside this.")

# GUI port (stage 2a, from DamSafety Screening): the pipeline strip Data -> DEM -> Screen ->
# Report. Each stage is a flat button that shows the LIVE state of one nd_ step - derived from the
# SAME wf.statuses the Simple tab's step list already uses - and jumps to the tab that owns it.
# (key, label, the step whose state it shows, the tab-key it jumps to).
PIPELINE = (("data", "Data", "1", "simple"), ("dem", "DEM", "2", "simple"),
            ("screen", "Screen", "7", "simple"), ("report", "Report", None, "results"))
PIPE_MARK = {"done": "✓", "ready": "●", "running": "◐", "problem": "!",
             "stale": "↻", "waiting": "○", "optional": "○"}
PIPE_COLOR = {"done": "#2e7d32", "ready": "#1a73e8", "running": "#1a73e8", "problem": "#c62828",
              "stale": "#b26a00"}
# a pipeline tab-key -> the tab TITLE it jumps to (resolved to a live index at click time, so nd_'s
# tab order can change without touching this)
PIPE_TAB = {"simple": "Simple", "inputs": "Inputs", "review": "Review", "results": "Results"}


def _escape_tab_mnemonics(widget):
    """0.26.0: a single '&' in a tab title is Qt's keyboard-shortcut marker and is not shown
    ('Dams & data' read 'Dams data'); '&&' shows one '&'. Applied once the tabs are built."""
    try:
        from qgis.PyQt.QtWidgets import QTabWidget
        for tw in widget.findChildren(QTabWidget):
            for i in range(tw.count()):
                t = tw.tabText(i)
                if "&" in t and "&&" not in t:
                    tw.setTabText(i, t.replace("&", "&&"))
    except Exception:                                            # noqa
        pass


def _scroll_tab(w):
    """Wrap a tab widget in a QScrollArea so tall pages are reachable on small screens."""
    sa = QScrollArea()
    sa.setWidgetResizable(True)
    sa.setWidget(w)
    return sa


def plugin_version():
    try:
        from .core import damrecord_io
        return damrecord_io.plugin_version()
    except Exception:                                            # noqa
        return ""


class _DamPicker(QDialog):
    """0.26.0: the 'Run selected dams...' list: one check box per dam or pond of the work folder
    (wf.dam_choices), All / None, OK / Cancel. `checked_ids()` are the IDs ticked."""

    def __init__(self, parent, choices):
        super().__init__(parent)
        self.setWindowTitle(TITLE + " - Run selected dams")
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("Tick the dams or ponds to run. The whole chain runs for them alone, into "
                             "one folder under <work folder>/one_dam; the batch is left as it is."))
        inner = QWidget()
        il = QVBoxLayout(inner)
        self.boxes = []
        for did, label in choices:
            cb = QCheckBox(str(label))
            cb.setChecked(False)
            il.addWidget(cb)
            self.boxes.append((str(did), cb))
        il.addStretch(1)
        lay.addWidget(_scroll_tab(inner), 1)
        row = QHBoxLayout()
        self.all_btn = QPushButton("All")
        self.all_btn.clicked.connect(lambda _c=False: self.set_all(True))
        self.none_btn = QPushButton("None")
        self.none_btn.clicked.connect(lambda _c=False: self.set_all(False))
        self.ok_btn = QPushButton("OK")
        self.ok_btn.setDefault(True)
        self.ok_btn.clicked.connect(lambda _c=False: self.accept())
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.clicked.connect(lambda _c=False: self.reject())
        row.addWidget(self.all_btn)
        row.addWidget(self.none_btn)
        row.addStretch(1)
        row.addWidget(self.ok_btn)
        row.addWidget(self.cancel_btn)
        lay.addLayout(row)
        self.resize(460, 520)

    def set_all(self, on):
        for _did, cb in self.boxes:
            cb.setChecked(bool(on))

    def checked_ids(self):
        return [did for did, cb in self.boxes if cb.isChecked()]


class QgisEnv(pf.Env):
    """The preflight's environment inside QGIS: project layers as well as files."""

    def __init__(self, read_layer):
        self._read_layer = read_layer

    def layer_info(self, ref):
        info = pf.Env.layer_info(self, ref)
        if info is not None:
            return info
        lyr = self._read_layer(ref)
        if lyr is None:
            return None
        try:
            fields = list(lyr.fields().names())
            n = lyr.featureCount()
            crs = ""
            try:
                crs = lyr.crs().authid()
            except Exception:                                    # noqa
                try:
                    crs = lyr.sourceCrs().authid()
                except Exception:                                # noqa
                    crs = ""
            geom = ""
            try:
                from qgis.core import QgsWkbTypes
                geom = {0: "point", 1: "line", 2: "polygon"}.get(int(QgsWkbTypes.geometryType(lyr.wkbType())), "")
            except Exception:                                    # noqa
                geom = ""
            return {"fields": fields, "count": int(n), "crs": crs or "EPSG:?", "geometry": geom, "name": ref}
        except Exception:                                        # noqa
            return None

    def values(self, ref, field):
        v = pf.Env.values(self, ref, field)
        if v is not None:
            return v
        lyr = self._read_layer(ref)
        if lyr is None or field not in lyr.fields().names():
            return None
        return {str(f[field]).strip() for f in lyr.getFeatures() if f[field] not in (None, "")}

    def rows(self, ref, fields):
        r = pf.Env.rows(self, ref, fields)
        if r is not None:
            return r
        lyr = self._read_layer(ref)
        if lyr is None:
            return None
        names = lyr.fields().names()
        return [tuple(f[x] if x in names else None for x in fields) for f in lyr.getFeatures()]


class NDDamScreeningDialog(QDialog):
    """The popup window. `iface` is optional (used to open the ND Dam Hazard window and the map)."""

    def __init__(self, parent=None, iface=None):
        super().__init__(parent)
        self.iface = iface
        self._loading = True
        self.setWindowTitle("{} {}".format(TITLE, plugin_version()).strip())
        self.setMinimumWidth(960)
        try:
            from qgis.PyQt.QtWidgets import QApplication
            self.setMaximumHeight(max(700, QApplication.primaryScreen().geometry().height() - 80))
        except Exception:
            pass
        self.step_state, self.step_button, self.step_note = {}, {}, {}
        self.simple_state, self.simple_name, self.simple_note = {}, {}, {}
        self.layer_combo = {}
        self.chain = None                 # the run in order that is going (core/workflow.Chain)
        self._task = None
        self._keep = []                   # task / context / feedback kept alive while it runs
        self._one_dam = None              # (dam id, folder) of the one-dam run that is going
        self.review_rows = []
        self.priority_rows = []           # 0.28.0: the Priority tab's last read of the Dams folder
        self.last_checks = []
        self.grid = None
        self._build()
        _escape_tab_mnemonics(self)
        self._load_state()
        self._loading = False
        self._fill_layers()
        self._load_settings_file(quiet=True)
        self.refresh()
        try:
            from qgis.PyQt.QtCore import QTimer
            self._tick_timer = QTimer(self)
            self._tick_timer.setInterval(1000)
            self._tick_timer.timeout.connect(self._tick)
            self._tick_timer.start()
            self._auto_pf_timer = QTimer(self)
            self._auto_pf_timer.setSingleShot(True)
            self._auto_pf_timer.setInterval(900)
            self._auto_pf_timer.timeout.connect(self._auto_preflight)
            self.work_edit.editingFinished.connect(self._auto_pf_timer.start)
            self.terrain_source.currentIndexChanged.connect(self._auto_pf_timer.start)
            self.engine.currentIndexChanged.connect(self._auto_pf_timer.start)
        except Exception:
            pass

    # ------------------------------------------------------------------ build --
    def _build(self):
        lay = QVBoxLayout()
        info = QLabel(DISCLAIMER)
        info.setWordWrap(True)
        lay.addWidget(info)

        # GUI port (stage 2a): nd_'s existing header row (work folder + county) folded into a
        # Workspace group hoisted above the tabs. Every widget and its wiring are nd_'s own - the
        # county_combo is kept intact; only the container changed.
        ws = QGroupBox("Workspace")
        row = QHBoxLayout()
        row.addWidget(QLabel("Work folder:"))
        self.work_edit = QLineEdit()
        self.work_edit.setToolTip(
            "One folder for everything this window's steps write (0_data_package.gpkg, "
            "1_dams.gpkg, 2_terrain, ...). The window reads it to show which steps are done. "
            "Use a folder per county / per run.")
        self.work_edit.editingFinished.connect(self._changed)
        self.work_browse = QPushButton("Browse...")
        self.work_browse.clicked.connect(lambda _c=False: self._browse_folder(self.work_edit))
        self.work_open = QPushButton("Open folder")
        self.work_open.clicked.connect(self._open_work_folder)
        self.help_btn = QPushButton("How it decides...")
        self.help_btn.setToolTip("Pictures of the rules: the rim / toe of a pond, the triage envelope, the "
                                 "ND 5.2 / 5.3 decision tree, and what every threshold does.")
        self.help_btn.clicked.connect(lambda _c=False: self._open_help())
        # GUI port (stage 2a): a gear SHORTCUT that only SELECTS the Settings tab (no separate
        # window - the Settings-window move is deferred to a later stage; nd_'s Settings tab stays).
        self.settings_shortcut_btn = QPushButton("⚙ Settings")
        self.settings_shortcut_btn.setToolTip("Jump to the Settings tab: every engineer-chosen value, "
                                              "the settings file and the run options.")
        self.settings_shortcut_btn.clicked.connect(lambda _c=False: self._select_tab("Settings"))
        row.addWidget(self.work_edit, 1)
        row.addWidget(self.work_browse)
        row.addWidget(self.work_open)
        # 0.21.1: the county: tool 1 keeps its dams only, the suite data and step 0 are clipped to it
        from .core import nd_counties as cty
        row.addWidget(QLabel("County:"))
        self.county_combo = QComboBox()
        self.county_combo.addItem(cty.ALL, "")
        for name in cty.names():
            self.county_combo.addItem(name, name)
        self.county_combo.setToolTip(
            "Screen one county: tool 1 keeps only the dams whose point is in it (so every later "
            "step works on them), and the suite data and step 0 are clipped to it. Run tool 1 "
            "again after changing it. Use a work folder per county.")
        self.county_combo.currentIndexChanged.connect(lambda *_a: self._changed())
        row.addWidget(self.county_combo)
        row.addWidget(self.help_btn)
        row.addWidget(self.settings_shortcut_btn)
        ws.setLayout(row)
        lay.addWidget(ws)
        # GUI port (stage 2a): the pipeline strip sits between the workspace header and the tabs
        lay.addWidget(self._build_pipeline())

        self.tabs = QTabWidget()
        self.tabs.addTab(_scroll_tab(self._build_simple_tab()), "Simple")
        self.tabs.addTab(_scroll_tab(self._build_workflow_tab()), "All steps")
        self.tabs.addTab(_scroll_tab(self._build_inputs_tab()), "Inputs")
        self.tabs.addTab(_scroll_tab(self._build_settings_tab()), "Settings")
        self.tabs.addTab(_scroll_tab(self._build_review_tab()), "Review")
        self.tabs.addTab(_scroll_tab(self._build_results_tab()), "Results")
        self.tabs.addTab(_scroll_tab(self._build_priority_tab()), "Priority")    # 0.28.0
        lay.addWidget(self.tabs, 1)
        # 0.28.0: what each Run button says when it CAN be pressed, so the reason it is grey can
        # be put in its place and taken back out again (refresh)
        self._run_tips = dict((id(b), b.toolTip()) for b in
                              (self.chain_btn, self.one_dam_btn, self.sel_dams_btn))

        # GUI port (stage 2a): the log folded to a one-line 'Log: <last line>' with a 'Show log'
        # toggle, replacing nd_'s always-open box. The full box is hidden until asked for; the
        # Simple tab's step_progress bar is left where it is (preserved).
        self.log_edit = QTextEdit()
        self.log_edit.setReadOnly(True)
        self.log_edit.setMaximumHeight(110)
        self.log_edit.setVisible(False)
        lay.addWidget(self.log_edit)
        row = QHBoxLayout()
        self.log_line = QLabel("Log: -")
        self.log_line.setStyleSheet("color: #4a5560;")
        self.log_toggle = QPushButton("Show log")
        self.log_toggle.setToolTip("Show or hide the whole log.")
        self.log_toggle.clicked.connect(lambda _c=False: self._toggle_log())
        self.close_btn = QPushButton("Close")
        self.close_btn.clicked.connect(self.close)
        row.addWidget(self.log_line, 1)
        row.addWidget(self.log_toggle)
        row.addStretch(0)
        row.addWidget(self.close_btn)
        lay.addLayout(row)
        self.setLayout(lay)

    # -- GUI port (stage 2a): the pipeline strip, the tab shortcuts and the folded log
    def _build_pipeline(self):
        """Data -> DEM -> Screen -> Report; each a flat button that jumps to its owning tab."""
        bar = QGroupBox("")
        row = QHBoxLayout()
        self.pipe_btn = {}
        for i, (key, label, _step, _tab) in enumerate(PIPELINE):
            if i:
                row.addWidget(QLabel("→"))
            b = QPushButton("{} ○".format(label))
            b.setFlat(True)
            b.clicked.connect(lambda _c=False, k=key: self._goto_stage(k))
            self.pipe_btn[key] = b
            row.addWidget(b)
        row.addStretch(1)
        bar.setLayout(row)
        self.pipeline_bar = bar
        return bar

    def _select_tab(self, title):
        """Select the tab with this title (robust to nd_'s tab order and the '&&' mnemonic escape)."""
        try:
            for i in range(self.tabs.count()):
                if self.tabs.tabText(i).replace("&&", "&") == title:
                    self.tabs.setCurrentIndex(i)
                    return True
        except Exception:                                        # noqa
            pass
        return False

    def _goto_stage(self, key):
        """A pipeline button: jump to the tab that owns the stage (adapted to nd_'s tab order)."""
        tab = dict((k, t) for k, _l, _s, t in PIPELINE).get(key, "simple")
        self._select_tab(PIPE_TAB.get(tab, "Simple"))

    def _refresh_pipeline(self, by, busy, have, work):
        """Fill the strip from nd_'s OWN wf.statuses (the `by` dict the Simple tab's step list is
        built from) plus the report file. Each stage shows a live glyph + colour and a 'why'
        tooltip. Never the cause of a crash in refresh()."""
        self.pipe_state = {}
        cur = getattr(self.chain, "current", None) if busy else None
        for key, label, step, _tab in PIPELINE:
            if step is None:
                st = "done" if have and os.path.isfile(os.path.join(work, wf.REPORT_INDEX)) else "waiting"
                why = ("Report: index.html written by step 10" if st == "done"
                       else "Report: no report yet")
            else:
                st = by.get(step) or "waiting"
                if busy and (cur == step or (step == "7" and cur in ("5", "6", "7", "8"))):
                    st = "running"
                why = "{}: step {} - {} ({})".format(label, step, wf.TITLE.get(step, ""), st)
            self.pipe_state[key] = st
            b = self.pipe_btn[key]
            b.setText("{} {}".format(label, PIPE_MARK.get(st, "○")))
            b.setToolTip(why + ". Click to go to the tab.")
            b.setStyleSheet("color: {};{}".format(PIPE_COLOR.get(st, "#5f6b76"),
                                                   " font-weight: bold;" if st in ("done", "ready") else ""))

    def _toggle_log(self):
        on = not self.log_edit.isVisible()
        self.log_edit.setVisible(on)
        self.log_toggle.setText("Hide log" if on else "Show log")

    # -- Simple
    def _build_simple_tab(self):
        from .algorithms.run_breach_sim import ENGINES
        from .algorithms.hazard_classification import CRIT_LABELS
        w = QWidget()
        lay = QVBoxLayout()
        grid = QGridLayout()
        r = 0
        grid.addWidget(QLabel("What to screen"), r, 0)
        box = QHBoxLayout()
        self.screen_radio = []
        for i, label in enumerate(SCREEN_WHAT):
            rb = QRadioButton(label)
            rb.toggled.connect(lambda on, idx=i: self._screen_toggled(idx, on))
            self.screen_radio.append(rb)
            box.addWidget(rb)
        box.addStretch(1)
        grid.addLayout(box, r, 1)
        r += 1
        grid.addWidget(QLabel("Data package (step 0)"), r, 0)
        self.package_edit = QLineEdit()
        self.package_edit.setToolTip(
            "Optional: the GeoPackage ND Dam Hazard's 'Export for Screening' writes, or your own "
            "package from step 0. Every input left empty on the Inputs tab is taken from it.")
        self.package_edit.editingFinished.connect(self._changed)
        b = QPushButton("File...")
        b.clicked.connect(lambda _c=False: self._browse_file(self.package_edit, "GeoPackage (*.gpkg)"))
        grid.addWidget(self.package_edit, r, 1)
        grid.addWidget(b, r, 2)
        r += 1
        self.terrain_source = self._combo(["USGS 3DEP service (needs internet)", "Local DEM folder"])
        self.terrain_source.setToolTip("Where the LiDAR comes from: the USGS 3DEP service (once per dam, needs "
                                       "internet) or your own folder of DEM tiles (Settings > Run options).")
        self.terrain_source.currentIndexChanged.connect(self._changed)
        grid.addWidget(QLabel("Terrain"), r, 0)
        grid.addWidget(self.terrain_source, r, 1)
        r += 1
        self.engine = self._combo(ENGINES)
        self.engine.setToolTip("The built-in engine needs no install; LISFLOOD-FP needs its executable "
                               "(Settings > Run options).")
        self.engine.currentIndexChanged.connect(self._changed)
        grid.addWidget(QLabel("Flood engine"), r, 0)
        grid.addWidget(self.engine, r, 1)
        r += 1
        self.crit = self._combo(CRIT_LABELS)
        self.crit.currentIndexChanged.connect(self._changed)
        grid.addWidget(QLabel("Hazard criterion"), r, 0)
        grid.addWidget(self.crit, r, 1)
        r += 1
        grid.addWidget(QLabel("Settings file"), r, 0)
        self.settings_file_edit = QLineEdit()
        self.settings_file_edit.setToolTip(
            "The suite settings workbook (damsuite_settings.xlsx, or .csv): every engineer-chosen value of "
            "the tools, shared by the three plugins. Edit it on the Settings tab or in Excel; the tools open "
            "with its values. Empty = the tools' own starting values.")
        self.settings_file_edit.editingFinished.connect(lambda: self._settings_file_edited())
        b = QPushButton("File...")
        b.clicked.connect(lambda _c=False: self._pick_settings_file())
        self.new_settings_btn = QPushButton("New...")
        self.new_settings_btn.setToolTip("Write a new settings file with the current values (then edit it on the "
                                         "Settings tab).")
        self.new_settings_btn.clicked.connect(lambda _c=False: self._new_settings_file())
        box = QHBoxLayout()
        box.addWidget(self.settings_file_edit, 1)
        box.addWidget(b)
        box.addWidget(self.new_settings_btn)
        grid.addLayout(box, r, 1, 1, 2)
        r += 1
        grid.addWidget(QLabel("Preset"), r, 0)
        box = QHBoxLayout()
        self.preset_combo = QComboBox()
        self.preset_combo.setToolTip("Named settings files that come with the plugin (and any in the settings "
                                     "file's folder). 'Apply' copies a preset's values into the current settings.")
        self.apply_preset_btn = QPushButton("Apply")
        self.apply_preset_btn.clicked.connect(lambda _c=False: self._apply_preset())
        box.addWidget(self.preset_combo, 1)
        box.addWidget(self.apply_preset_btn)
        grid.addLayout(box, r, 1, 1, 2)
        r += 1
        lay.addLayout(grid)
        note = QLabel("Everything else comes from the settings file (Settings tab) and the layers on the Inputs "
                      "tab; left empty, the layers come from the data package.")
        note.setWordWrap(True)
        lay.addWidget(note)
        # 0.28.1: what the run will read and from where, before it starts (wf.found_layers_text)
        self.found_label = QLabel("")
        self.found_label.setWordWrap(True)
        lay.addWidget(self.found_label)

        grp = QGroupBox("Steps")
        g = QGridLayout()
        for i, key in enumerate(SG.MAIN_STEPS):
            if key == "17":
                continue
            st = QLabel("")
            nm = QLabel(SG.plain(key, wf.TITLE[key]))
            nm.setToolTip("{} - {}\n{}".format(key, wf.TITLE[key], wf.HINT[key]))
            nt = QLabel("")
            nt.setWordWrap(True)
            self.simple_state[key], self.simple_name[key], self.simple_note[key] = st, nm, nt
            g.addWidget(st, i, 0)
            g.addWidget(nm, i, 1)
            g.addWidget(nt, i, 2)
        grp.setLayout(g)
        lay.addWidget(grp)

        # 0.28.0: how long this will take, BEFORE it starts (core/runplan.run_time_text)
        self.estimate_label = QLabel("")
        self.estimate_label.setWordWrap(True)
        lay.addWidget(self.estimate_label)

        self.step_progress = QProgressBar()
        self.step_progress.setRange(0, 100)
        self.step_progress.setVisible(False)
        self.step_progress.setMaximumHeight(16)
        self.step_progress.setFormat("Step %s: %%p%%")
        lay.addWidget(self.step_progress)

        row = QHBoxLayout()
        self.preflight_btn = QPushButton("Check before running")
        self.preflight_btn.setToolTip("A few seconds: work folder, disk, terrain service, engine, layers and "
                                      "fields, charts, settings - what would otherwise fail a run halfway.")
        self.preflight_btn.clicked.connect(lambda _c=False: self._preflight())
        # 0.28.0: "Run county" - what it does. "Run all" read like "run all the tools", and the
        # one-dam buttons beside it were the only way a novice found a single dam.
        self.run_all_btn = QPushButton("Run county")
        self.run_all_btn.setToolTip(RUN_COUNTY_TIP)
        self.run_all_btn.clicked.connect(lambda _c=False: self._run_chain(preflight=True))
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(lambda _c=False: self._stop_chain())
        self.one_dam_btn = QPushButton("Run one dam...")
        self.one_dam_btn.setToolTip("Pick one dam or pond - by ID, or the row selected in the Results tab's dam "
                                    "table - and run the whole chain for it alone (a minute with the built-in "
                                    "engine) into <work folder>/one_dam/<ID>; its report opens at the end. "
                                    "0.28.0: a one-dam run models both loading cases.")
        self.one_dam_btn.clicked.connect(lambda _c=False: self._run_one_dam())
        # 0.26.0: several dams at once, like Run one dam (their own folder, the batch untouched)
        self.sel_dams_btn = QPushButton("Run selected dams...")
        self.sel_dams_btn.setToolTip("Tick several dams or ponds and run the whole chain for them alone into "
                                     "<work folder>/one_dam/<IDs> (the step 1 filter is set to their IDs; the "
                                     "window's own settings are not changed).")
        self.sel_dams_btn.clicked.connect(lambda _c=False: self._run_selected_dams())
        row.addWidget(self.preflight_btn)
        row.addWidget(self.run_all_btn)
        row.addWidget(self.stop_btn)
        row.addWidget(self.one_dam_btn)
        row.addWidget(self.sel_dams_btn)
        row.addStretch(1)
        lay.addLayout(row)
        # 0.28.1: one button for the next plugin the selected dam needs (wf.continue_target)
        self.continue_btn = self._continue_button()
        row = QHBoxLayout()
        row.addWidget(self.continue_btn)
        row.addStretch(1)
        lay.addLayout(row)
        self.preflight_label = QLabel("")
        self.preflight_label.setWordWrap(True)
        lay.addWidget(self.preflight_label)
        self.preflight_text = QTextEdit()
        self.preflight_text.setReadOnly(True)
        self.preflight_text.setMaximumHeight(150)
        lay.addWidget(self.preflight_text)
        self.simple_hint = QLabel("")
        self.simple_hint.setWordWrap(True)
        lay.addWidget(self.simple_hint)
        lay.addStretch(1)
        w.setLayout(lay)
        return w

    # -- All steps (the 0.12 window)
    def _build_workflow_tab(self):
        w = QWidget()
        lay = QVBoxLayout()
        intro = QLabel(
            "Each button opens the tool's Processing dialog with the work folder's files, the settings "
            "file's values and the choices of this window filled in. Check the values, run it there, "
            "then come back: the list updates when this window is activated. Every step can also be run "
            "from the Processing Toolbox (ND Dam Breach Screening > Screening steps / Advanced).")
        intro.setWordWrap(True)
        lay.addWidget(intro)
        grid = QGridLayout()
        for i, key in enumerate(wf.KEYS):
            state = QLabel("")
            btn = QPushButton("{} - {}...".format(key, wf.TITLE[key]))
            btn.setToolTip("{}\n(Processing: {})".format(wf.HINT[key], wf.algorithm_id(key)))
            btn.clicked.connect(lambda _c=False, k=key: self._open_step(k))
            note = QLabel("")
            note.setWordWrap(True)
            self.step_state[key], self.step_button[key], self.step_note[key] = state, btn, note
            grid.addWidget(state, i, 0)
            grid.addWidget(btn, i, 1)
            grid.addWidget(note, i, 2)
        lay.addLayout(grid)
        grp = QGroupBox("Run the steps in order")
        g = QVBoxLayout()
        txt = QLabel(
            "Runs every step that is not done (or out of date) one after the other, in the "
            "background, with the values this window would fill in - no dialogs. It stops at the "
            "first step that fails or lacks an input, and checks each step wrote its output. "
            "Step 7b (ND Dam Breach Runner) is never part of it. The Simple tab's 'Run all' is the same "
            "run with the preflight check first.")
        txt.setWordWrap(True)
        g.addWidget(txt)
        row = QHBoxLayout()
        self.chain_check = {}
        for key, label in (("0", "also build the data package (0)"), ("6", "also the quick screen (6)"),
                           ("9", "also the sensitivity (9)"),
                           ("16", "ponds and dugouts (16)"), ("18", "merge dams and ponds (18)"),
                           ("19", "a proposed dam (19)")):
            c = QCheckBox(label)
            c.toggled.connect(self._chain_check_toggled)
            self.chain_check[key] = c
            row.addWidget(c)
        g.addLayout(row)
        row = QHBoxLayout()
        self.chain_btn = QPushButton("Run the steps in order")
        self.chain_btn.clicked.connect(lambda _c=False: self._run_chain())
        self.chain_stop_btn = QPushButton("Stop")
        self.chain_stop_btn.setEnabled(False)
        self.chain_stop_btn.clicked.connect(lambda _c=False: self._stop_chain())
        self.export_btn = QPushButton("Export settings (JSON)...")
        self.export_btn.setToolTip("Writes this window's choices as the JSON the headless tool (15) also "
                                   "reads; the suite settings file (.xlsx) is the usual way now.")
        self.export_btn.clicked.connect(lambda _c=False: self._export_settings())
        row.addWidget(self.chain_btn)
        row.addWidget(self.chain_stop_btn)
        row.addWidget(self.export_btn)
        row.addStretch(1)
        g.addLayout(row)
        grp.setLayout(g)
        lay.addWidget(grp)
        row = QHBoxLayout()
        self.next_btn = QPushButton("Open the next step")
        self.next_btn.setToolTip("The first step that is not done and whose earlier steps are "
                                 "done; optional steps are skipped.")
        self.next_btn.clicked.connect(self._open_next)
        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.clicked.connect(lambda _c=False: self.refresh())
        row.addWidget(self.next_btn)
        row.addWidget(self.refresh_btn)
        row.addStretch(1)
        lay.addLayout(row)
        self.hint_label = QLabel("")
        self.hint_label.setWordWrap(True)
        lay.addWidget(self.hint_label)
        lay.addStretch(1)
        w.setLayout(lay)
        return w

    # -- Inputs
    def _build_inputs_tab(self):
        w = QWidget()
        lay = QVBoxLayout()
        info = QLabel(
            "The data the tools read. A layer chosen here wins; left empty, the tools use the "
            "layer of the same meaning in the data package that step 0 wrote (structures, roads, "
            "railroads, flowlines, waterbodies, dam lines, lifelines). Pick project layers or "
            "'File...'. With neither, the dam folder's data (Get data for this dam), the suite "
            "offline pack or your files in the suite data folder's dwr/ are used - set them up in "
            "Plugins > ND Dam Suite > Suite data and servers; the log says which one was used.")
        info.setWordWrap(True)
        lay.addWidget(info)
        grp = QGroupBox("Layers")
        grid = QGridLayout()
        slots = list(wf.LAYER_SLOTS) + [("clip", "Clip to (step 0; e.g. the ND boundary)", None)]
        for i, (slot, label, _pkg) in enumerate(slots):
            combo = QComboBox()
            combo.currentIndexChanged.connect(self._changed)
            btn = QPushButton("File...")
            btn.clicked.connect(lambda _c=False, s=slot: self._browse_layer(s))
            self.layer_combo[slot] = combo
            grid.addWidget(QLabel(label), i, 0)
            grid.addWidget(combo, i, 1)
            grid.addWidget(btn, i, 2)
        grp.setLayout(grid)
        lay.addWidget(grp)
        self.layers_btn = QPushButton("Refresh the layer lists")
        self.layers_btn.clicked.connect(lambda _c=False: self._fill_layers())
        lay.addWidget(self.layers_btn)
        # 0.21.0: show the suite data in the empty slots before a run (wf.fill_empty_slots)
        self.suite_fill_btn = QPushButton("Fill empty layers from the suite data")
        self.suite_fill_btn.setToolTip(
            "Fills each EMPTY layer above from the dam folder's data, the suite offline pack "
            "(clipped to the work area) or your files in the suite data folder's dwr/ - the same "
            "sources a tool falls back to, shown here before the run. A layer you chose is left "
            "alone. DWR unknown structures and dugouts are never taken as the dams. Stream lines "
            "and land cover are not filled: they change the model.")
        self.suite_fill_btn.clicked.connect(lambda _c=False: self._fill_from_suite())
        lay.addWidget(self.suite_fill_btn)
        self.suite_fill_note = QLabel("")
        self.suite_fill_note.setWordWrap(True)
        lay.addWidget(self.suite_fill_note)
        lay.addStretch(1)
        w.setLayout(lay)
        return w

    # -- Settings: run options + the grid
    def _build_run_options(self):
        from .algorithms.run_breach_sim import TERRAIN, SCENARIOS, PRESETS
        w = QWidget()
        lay = QVBoxLayout()
        info = QLabel("Choices of this window that the tools' dialogs are filled with (files of this computer, "
                      "engine options). Every one can still be changed in the dialog. The parameters of the "
                      "tools themselves are on the other tabs.")
        info.setWordWrap(True)
        lay.addWidget(info)
        grid = QGridLayout()
        self.local_dem = QLineEdit()
        # 0.28.0: a fourth entry, the default - "follow the terrain source". Appended, not
        # inserted, so the saved index of an older project still means what it said.
        self.larger_clips = self._combo(list(TERRAIN) + [FOLLOW_TERRAIN_LABEL])
        self.larger_clips.setToolTip(
            "Where step 7 gets terrain when the flood reaches the edge of the step 2 clip. With "
            "'Cached clips only' the flood STOPS at that edge and the run still reports a class: "
            "a 3,937 ac-ft dam screened Medium with 0 buildings off a 3 km clip, and High with 27 "
            "buildings and 86 roads once the clip could grow. Choose it only when you are offline "
            "or you know the clip is big enough.")
        self.exe = QLineEdit()
        self.scenario = self._combo(SCENARIOS)
        self.workers = QSpinBox()
        self.workers.setRange(0, 64)
        self.dem_cell_ft = QDoubleSpinBox()
        self.dem_cell_ft.setRange(1.6, 330.0)
        self.dem_cell_ft.setDecimals(1)
        self.dem_cell_ft.setSingleStep(1.0)
        self.dem_cell_ft.setToolTip("Step 2: the cell size of the cached DEM. It follows the SOURCE cell: "
                                    "32.8 ft (10 m) for 3DEP and for the suite's DEM downloads. Set it to "
                                    "3.3 ft (1 m) for a local 1 m LiDAR folder - resampling finer than the "
                                    "source only makes the file bigger (a 3 km box at 1 m is 9 million "
                                    "cells), and coarser than the source throws terrain away. The flood "
                                    "does not have to use this size.")
        self.grid_ft = QDoubleSpinBox()
        self.grid_ft.setRange(0.0, 1640.0)
        self.grid_ft.setDecimals(1)
        self.grid_ft.setSingleStep(3.0)
        self.grid_ft.setToolTip("Step 7: the flood grid, USED ONLY WITH THE CUSTOM PRESET. 0 = chosen per "
                                "dam from the reservoir size. It can be coarser than the DEM. Before "
                                "0.28.0 this box was passed to step 7 but the resolution preset overruled "
                                "it, so every flood ran at the preset's 50 m whatever it said.")
        # 0.28.0: the preset is what decides the cell size; the window now passes it, and says
        # which size the engine will use next to the box (core/runplan.effective_cell)
        self.preset = self._combo(PRESETS)
        self.preset.setToolTip("Step 7's resolution. Quick screen is what every run has used since "
                               "0.23.0, whatever the Grid size box said. Custom uses the boxes below.")
        self.cell_label = QLabel("")
        self.cell_label.setWordWrap(True)
        self.simh = QDoubleSpinBox()
        self.simh.setRange(0.0, 240.0)
        self.simh.setDecimals(1)
        self.simh.setToolTip("Step 7: how many hours of flood to simulate. 0 = the tool estimates it. "
                             "The starting value is 12.")
        self.simmax = QDoubleSpinBox()
        self.simmax.setRange(1.0, 240.0)
        self.simmax.setDecimals(1)
        self.simmax.setToolTip("Step 7: the clock is lengthened, up to this many hours, while the flood is "
                               "still spreading. The starting value is 96.")
        self.chart_file = QLineEdit()
        self.runner_dem = QLineEdit()
        self.dams_folder = QLineEdit()
        for e in (self.local_dem, self.exe, self.chart_file, self.runner_dem, self.dams_folder):
            e.editingFinished.connect(self._changed)
        self.workers.valueChanged.connect(self._changed)
        self.workers.setToolTip("0 = leave the tool's own default (CPU cores - 1).")
        self.chart_file.setToolTip("The digitized ACER TM 11 chart file (.json). Empty = the tools' "
                                   "own default. ND Dam Hazard's 'Chart sign-off' writes one.")
        self.dams_folder.setToolTip("The folder of dam folders (one per dam) that tool 7b writes and "
                                    "ND Dam Breach Runner reads; tool 8 adds a 'screened' status to "
                                    "dam.json there. Empty = <work folder>/Dams.")
        self.baseline = QCheckBox("Step 7: also run a no-breach baseline (increment rasters)")
        self.baseline.setToolTip("A second simulation of each dam with only the steady ambient flow, no "
                                 "breach; the increment (breach minus baseline) is written too. A screening "
                                 "stand-in for ND's flood-scenario comparison; the real PMF routing is the "
                                 "Runner's.")
        self.baseline_cfs = QDoubleSpinBox()
        self.baseline_cfs.setRange(0.0, 1e7)
        self.baseline_cfs.setDecimals(1)
        self.baseline_cfs.setToolTip("Steady ambient flow through the dam site for the baseline (cfs; 0 = dry). "
                                     "YOUR value - the tool does not know the spillway flow.")
        self.buildings = self._combo(["Not represented (bare terrain)", "Raise the DEM at building footprints",
                                      "Manning's n at building footprints"])
        self.buildings.setToolTip("Step 7: how polygon footprints of the structures layer enter the terrain. "
                                  "Points are ignored. Neither treatment is right everywhere - YOUR choice.")
        self.building_raise_ft = QDoubleSpinBox()
        self.building_raise_ft.setRange(0.1, 200.0)
        self.building_raise_ft.setDecimals(1)
        self.building_n = QDoubleSpinBox()
        self.building_n.setRange(0.01, 5.0)
        self.building_n.setDecimals(3)
        # 0.28.0: both loading cases, clip pre-growth and resume
        self.both_scenarios = QCheckBox("Step 7: run BOTH loading cases in a county run (top of dam and "
                                        "sunny day)")
        self.both_scenarios.setToolTip("Two floods per dam instead of one, and the worse class governs "
                                       "(a hazard class follows the incremental consequences of a breach, "
                                       "and the sunny-day flood arrives on a dry day). It doubles a county "
                                       "run, which is why it is off here and on for one dam.")
        self.both_one_dam = QCheckBox("Run one dam / selected dams: run both loading cases")
        self.both_one_dam.setToolTip("On by default: one dam is a minute of model, so there is no reason "
                                     "to look at only one loading case.")
        self.pregrow = QCheckBox("Step 7: enlarge the clip before the run when the downstream trace "
                                 "leaves it")
        self.pregrow.setToolTip("On by default (0.28.0). The clip is sized from the valley before a flood "
                                "is paid for that can only hit the edge. It costs run time: switch it off "
                                "for a large county and let the clip grow after a run that reaches the "
                                "edge instead.")
        self.skip_done = QCheckBox("Step 7: skip dams that already have a result (resume an interrupted "
                                   "run)")
        self.skip_done.setToolTip("On by default. The output folder keeps run_dams.json, one entry per "
                                  "dam, so an interrupted county run carries on where it stopped. Switch "
                                  "it off to model every dam again.")
        self.incremental = QCheckBox("Step 8: count impacts from the incremental depth")
        self.incremental.setToolTip("Needs step 7's baseline. A structure counts as flooded by the BREACH only "
                                    "where breach depth minus baseline depth meets the threshold.")
        self.ref_folder = QLineEdit()
        self.ref_folder.setToolTip("Step 11: HEC-RAS / DSS-WISE reference rasters, in per-dam sub-folders or "
                                   "files named by dam ID.")
        self.ref_units = self._combo(["US customary (ft, ft/s, hours)", "Metric (m, m/s, hours)"])
        self.old_inventory = QLineEdit()
        self.old_inventory.setToolTip("Step 13: the previous inventory (a tool-1 GeoPackage or DWR export) "
                                      "to compare the current 1_dams.gpkg with.")
        self.reviewer_edit = QLineEdit()
        self.reviewer_edit.setToolTip("Your name or initials, written with every decision on the Review tab.")
        for e in (self.ref_folder, self.old_inventory, self.reviewer_edit):
            e.editingFinished.connect(self._changed)
        for c in (self.baseline, self.incremental, self.both_scenarios, self.both_one_dam,
                  self.pregrow, self.skip_done):
            c.toggled.connect(self._changed)
        self.preset.currentIndexChanged.connect(self._changed)
        for sp in (self.baseline_cfs, self.building_raise_ft, self.building_n,
                   self.dem_cell_ft, self.grid_ft, self.simh, self.simmax):
            sp.valueChanged.connect(self._changed)
        self.buildings.currentIndexChanged.connect(self._changed)
        self.ref_units.currentIndexChanged.connect(self._changed)
        self.larger_clips.currentIndexChanged.connect(self._changed)
        self.scenario.currentIndexChanged.connect(self._changed)
        rows = (
            ("Local DEM folder (LiDAR tiles)", self.local_dem, "dir"),
            ("Step 7 terrain for larger clips", self.larger_clips, None),
            ("LISFLOOD-FP executable", self.exe, "file"),
            ("Step 7 scenario", self.scenario, None),
            ("DEM cell size (ft)", self.dem_cell_ft, None),       # 0.20.0: was labelled (m)
            ("Step 7 resolution preset", self.preset, None),       # 0.28.0
            ("Grid size (ft) - Custom preset only", self.grid_ft, None),
            ("", self.cell_label, None),                           # 0.28.0: what will really be used
            ("Simulated time (h)", self.simh, None),
            ("Longest simulated time (h)", self.simmax, None),
            ("Dams at the same time (0 = tool default)", self.workers, None),
            ("ACER TM 11 chart file (.json)", self.chart_file, "json"),
            ("Step 7b DEM for the Runner", self.runner_dem, "raster"),
            ("Dams folder (7b, dam.json)", self.dams_folder, "dir"),
            ("", self.baseline, None),
            ("Baseline ambient flow (cfs)", self.baseline_cfs, None),
            ("Step 7 buildings in the terrain", self.buildings, None),
            ("Buildings: raise the DEM by (ft)", self.building_raise_ft, None),
            ("Buildings: Manning's n", self.building_n, None),
            ("", self.incremental, None),
            ("", self.both_scenarios, None),                       # 0.28.0
            ("", self.both_one_dam, None),
            ("", self.pregrow, None),
            ("", self.skip_done, None),
            ("Step 11 reference rasters folder", self.ref_folder, "dir"),
            ("Step 11 reference units", self.ref_units, None),
            ("Step 13 previous inventory", self.old_inventory, "file"),
            ("Reviewer (Review tab)", self.reviewer_edit, None),
        )
        for i, (label, widget, kind) in enumerate(rows):
            grid.addWidget(QLabel(label), i, 0)
            grid.addWidget(widget, i, 1)
            if kind:
                b = QPushButton("Browse...")
                b.clicked.connect(lambda _c=False, e=widget, k=kind: self._browse_for(e, k))
                grid.addWidget(b, i, 2)
        lay.addLayout(grid)
        lay.addWidget(self._dem_download_box())
        lay.addStretch(1)
        w.setLayout(lay)
        return w


    def _dem_download_box(self):
        from .core import dem_download as dd
        box = QGroupBox("Download a DEM (USGS 3DEP or ND DWR LiDAR)")
        grid = QGridLayout()
        self.dem_source = QComboBox()
        self.dem_source.addItems(["USGS 3DEP", "ND DWR LiDAR (lidar.dwr.nd.gov)"])
        self.dem_source.setToolTip("ND DWR LiDAR: LiDAR_NorthDakota_QL2 ASCII_Grids_ft and DEM_Imagine "
                                   "tiles for the dam buffer, county or state over HTTPS, or the FTP "
                                   "package from the map site when a link is pasted.")
        self.dem_ftp = QLineEdit()
        self.dem_ftp.setPlaceholderText("optional: ftp://lidarftp.swc.nd.gov/temp/<id> (empty = tiles for the area over HTTPS)")
        self.dem_ascii = QCheckBox("ASCII_Grids_ft")
        self.dem_ascii.setChecked(False)
        self.dem_imagine = QCheckBox("DEM_Imagine")
        self.dem_imagine.setChecked(True)
        self.dem_scope = QComboBox()
        self.dem_scope.addItems(["Dam", "County", "State"])
        self.dem_scope.setToolTip("Dam uses the point and buffer. County is one North Dakota county. "
                                  "State is all of North Dakota and is a long download at a fine cell.")
        self.dem_point = QLineEdit()
        self.dem_point.setPlaceholderText("longitude,latitude")
        self.dem_point.setToolTip("Dam only. Example: -97.46396,47.31054")
        self.dem_buffer = QDoubleSpinBox()
        self.dem_buffer.setRange(1.0, 50.0)
        self.dem_buffer.setValue(10.0)
        self.dem_buffer.setSuffix(" mi")
        self.dem_county = QComboBox()
        self.dem_county.addItems(dd.county_names())
        self.dem_county.setCurrentText("Cass")
        # 0.24.0: resolution choice; output is always EPSG:5070 float32 GeoTIFF metres
        self.dem_resolution = QComboBox()
        self.dem_resolution.addItems(["10 m", "1 m"])
        self.dem_resolution.setToolTip(
            "10 m — USGS 3DEP: ~12 s/dam.  ND DWR QL2: native 1 m tiles resampled to 10 m.\n"
            "1 m  — USGS 3DEP: fine resolution, much larger.  ND DWR QL2: native 1 m tiles.")
        self.dem_dam_id = QLineEdit()
        self.dem_dam_id.setPlaceholderText("dam ID (optional)")
        self.dem_dam_id.setToolTip("Names the file (<dam ID>_dem_10m.vrt). When the Dams folder has a "
                                   "folder for this dam, the DEM goes into <dam folder>/data/ and its "
                                   "dam.json, and an empty dam point is taken from dam.json.")
        self.dem_out = QLineEdit()
        self.dem_out.setPlaceholderText("folder for the tiles and the VRT")
        browse = QPushButton("Browse...")
        browse.clicked.connect(lambda _c=False: self._browse_for(self.dem_out, "dir"))
        counties = QPushButton("Counties on the map")
        counties.clicked.connect(self._show_counties)
        go = QPushButton("Download DEM...")
        go.clicked.connect(self._open_dem_download)
        grid.addWidget(QLabel("Source"), 0, 0)
        grid.addWidget(self.dem_source, 0, 1)
        grid.addWidget(self.dem_ascii, 0, 2)
        grid.addWidget(self.dem_imagine, 0, 3)
        grid.addWidget(QLabel("ND LiDAR path"), 1, 0)
        grid.addWidget(self.dem_ftp, 1, 1, 1, 3)
        grid.addWidget(QLabel("Area"), 2, 0)
        grid.addWidget(self.dem_scope, 2, 1)
        grid.addWidget(QLabel("Resolution"), 2, 2)
        grid.addWidget(self.dem_resolution, 2, 3)
        grid.addWidget(QLabel("Dam point"), 3, 0)
        grid.addWidget(self.dem_point, 3, 1)
        grid.addWidget(QLabel("Buffer"), 3, 2)
        grid.addWidget(self.dem_buffer, 3, 3)
        grid.addWidget(QLabel("County"), 4, 0)
        grid.addWidget(self.dem_county, 4, 1)
        grid.addWidget(counties, 4, 2, 1, 2)
        grid.addWidget(QLabel("Folder"), 5, 0)
        grid.addWidget(self.dem_out, 5, 1)
        grid.addWidget(browse, 5, 2)
        grid.addWidget(go, 5, 3)
        grid.addWidget(QLabel("Dam ID"), 6, 0)
        grid.addWidget(self.dem_dam_id, 6, 1)
        # items 2+7: inline format facts + time estimate label
        self.dem_info_label = QLabel("")
        self.dem_info_label.setWordWrap(True)
        self.dem_info_label.setStyleSheet("color: gray;")
        grid.addWidget(self.dem_info_label, 7, 0, 1, 4)
        self.dem_source.currentIndexChanged.connect(self._update_dem_info)
        self.dem_resolution.currentIndexChanged.connect(self._update_dem_info)
        box.setLayout(grid)
        self._load_dem_box()
        self._update_dem_info()
        return box

    # 0.16.0: the DEM box remembers its values between sessions (QGIS settings, this profile)
    DEM_BOX_KEY = "ndscreen/dem_box/"

    def _dem_box_widgets(self):
        return {"source": self.dem_source, "scope": self.dem_scope, "county": self.dem_county,
                "resolution": self.dem_resolution, "point": self.dem_point, "ftp": self.dem_ftp,
                "folder": self.dem_out, "dam_id": self.dem_dam_id, "buffer": self.dem_buffer,
                "ascii": self.dem_ascii, "imagine": self.dem_imagine}

    def _load_dem_box(self):
        try:
            from qgis.core import QgsSettings
            st = QgsSettings()
        except Exception:                                        # noqa
            return
        for name, w in self._dem_box_widgets().items():
            try:
                v = st.value(self.DEM_BOX_KEY + name, None)
                if v in (None, ""):
                    continue
                if isinstance(w, QComboBox):
                    if name in ("source", "scope", "resolution"):
                        i = int(v)
                        if 0 <= i < w.count():
                            w.setCurrentIndex(i)
                    else:
                        w.setCurrentText(str(v))
                elif isinstance(w, QDoubleSpinBox):
                    w.setValue(float(v))
                elif isinstance(w, QCheckBox):
                    w.setChecked(str(v).lower() in ("true", "1", "yes"))
                else:
                    w.setText(str(v))
            except Exception:                                    # noqa - a stale value is skipped
                continue

    def _save_dem_box(self):
        try:
            from qgis.core import QgsSettings
            st = QgsSettings()
        except Exception:                                        # noqa
            return
        for name, w in self._dem_box_widgets().items():
            try:
                if isinstance(w, QComboBox):
                    v = w.currentIndex() if name in ("source", "scope", "resolution") else w.currentText()
                elif isinstance(w, QDoubleSpinBox):
                    v = float(w.value())
                elif isinstance(w, QCheckBox):
                    v = bool(w.isChecked())
                else:
                    v = w.text().strip()
                st.setValue(self.DEM_BOX_KEY + name, v)
            except Exception:                                    # noqa
                continue

    def _dem_box_dams(self):
        """The Dams folder for the DEM box: the window's, else the suite's, else <work>/Dams."""
        try:
            s = self.settings()
            work = (s.get("work") or "").strip()
            mine = (s.get("dams_folder") or "").strip()
        except Exception:                                        # noqa
            work, mine = "", ""
        if mine:
            return mine
        try:
            from .damsuite import suitedata as SD
            if SD.dams_folder():
                return SD.dams_folder()
        except Exception:                                        # noqa
            pass
        return wf.path(work, "7b", "DAMS_FOLDER") if work else ""


    def _show_counties(self):
        from qgis.core import (QgsVectorLayer, QgsFeature, QgsGeometry, QgsPointXY,
                               QgsProject, QgsFillSymbol, QgsSingleSymbolRenderer)
        from nd_dam_screening.core import dem_download as dd
        layer = QgsVectorLayer("Polygon?crs=EPSG:4326&field=county:string",
                               "ND counties (download clips)", "memory")
        prov = layer.dataProvider()
        feats = []
        for name, ring in dd.county_features():
            f = QgsFeature(layer.fields())
            f.setAttribute("county", name)
            f.setGeometry(QgsGeometry.fromPolygonXY([[QgsPointXY(x, y) for x, y in ring]]))
            feats.append(f)
        prov.addFeatures(feats)
        layer.updateExtents()
        try:
            sym = QgsFillSymbol.createSimple({"color": "0,80,160,40", "outline_color": "0,60,120",
                                              "outline_width": "0.6"})
            layer.setRenderer(QgsSingleSymbolRenderer(sym))
        except Exception:
            pass
        QgsProject.instance().addMapLayer(layer)
        try:
            layer.selectionChanged.connect(lambda *_a: self._county_from_map(layer))
        except Exception:
            pass
        canvas = getattr(self.iface, "mapCanvas", lambda: None)()
        if canvas is not None:
            canvas.setExtent(layer.extent())
            canvas.refresh()
        self.log_edit.append("County clips are on the map. Select one and the county list follows it.")

    def _county_from_map(self, layer):
        names = [f["county"] for f in layer.selectedFeatures()]
        if not names:
            return
        self.dem_source.setCurrentIndex(1)
        self.dem_scope.setCurrentIndex(1)
        self.dem_county.setCurrentText(names[0])

    def _open_dem_download(self):
        try:
            import processing
        except ImportError:
            from qgis import processing
        folder = self.dem_out.text().strip()
        dam_id = self.dem_dam_id.text().strip()
        dams = self._dem_box_dams() if dam_id else ""
        from .core import handoff as ho
        dam_folder = os.path.join(dams, ho.safe_name(dam_id)) if dams and dam_id else ""
        dam_folder = dam_folder if dam_folder and os.path.isdir(dam_folder) else ""
        if not folder and not dam_folder:
            QMessageBox.warning(self, TITLE, "Choose a folder for the DEM (or the ID of a dam that has a "
                                             "folder in the Dams folder).")
            return
        scope = self.dem_scope.currentIndex()
        if scope == 2:
            n = QMessageBox.question(self, TITLE, "A statewide DEM is tiled and can take a long time. Continue?")
            if n != self._yes():                     # 0.16.0: Qt6 enums (QMessageBox.StandardButton)
                return
        self._save_dem_box()
        params = {
            "SOURCE": self.dem_source.currentIndex(),
            "SCOPE": scope,
            "DAM_POINT": self.dem_point.text().strip(),
            "BUFFER_MI": float(self.dem_buffer.value()),
            "COUNTY": self.dem_county.currentText(),
            "RESOLUTION": self.dem_resolution.currentIndex(),  # 0.24.0: 0=10m, 1=1m
            "DWR_PATH": self.dem_ftp.text().strip(),
            "ASCII_FT": self.dem_ascii.isChecked(),
            "IMAGINE": self.dem_imagine.isChecked(),
        }
        if dam_id:
            params["DAM_ID"] = dam_id
        if dam_folder:
            params["DAMS_FOLDER"] = dams
            self._log("DEM download for {}: written to {} (and its dam.json)".format(
                dam_id, os.path.join(dam_folder, "data")))
        if folder:
            params["OUT"] = folder
        processing.execAlgorithmDialog("nddam:downloaddem", params)
        return params

    def _build_settings_tab(self):
        from . import suite_settings as SS
        from .algorithms import _settings as ST
        try:
            from . import provider as prov
            catalog = ST.full_catalog(prov.ALGORITHMS)
        except Exception as e:                                   # noqa - a tool that cannot be built here
            catalog = []
            self._log("!!! settings grid: {}".format(e))
        try:
            from .damsuite import versions as vs
            # 0.21.0: this plugin's own version (damsuite's release table is still 0.20.0's)
            versions = {SG.PLUGIN: plugin_version() or vs.RELEASE.get(SG.PLUGIN, "")}
        except Exception:                                        # noqa
            versions = {}
        self.grid = SS.SettingsGrid(SG.PLUGIN, catalog, DS.Settings(), parent=self,
                                    on_change=self._grid_changed, on_file=self._grid_file,
                                    versions=versions, tab_order=SG.TAB_ORDER,
                                    extra_tabs=[("Run options", self._build_run_options())])
        return self.grid

    # -- Review
    def _build_review_tab(self):
        w = QWidget()
        lay = QVBoxLayout()
        info = QLabel("The dams that need a look: uncertain breach direction (no dam line), uncertain or "
                      "unusual pond type, borderline class, the report's review queue. Zoom to one, decide, "
                      "and the decision is written into the layers (rev_decision / rev_by / rev_date) and "
                      "to review_decisions.csv. An excluded dam is skipped by steps 5-8 from then on. Your "
                      "name: Settings > Run options > Reviewer.")
        info.setWordWrap(True)
        lay.addWidget(info)
        self.review_table = QTableWidget()
        self.review_table.setColumnCount(len(REVIEW_COLUMNS))
        try:
            self.review_table.setHorizontalHeaderLabels(list(REVIEW_COLUMNS))
            self.review_table.setSelectionBehavior(self.review_table.SelectionBehavior.SelectRows)
        except Exception:                                        # noqa
            pass
        lay.addWidget(self.review_table, 1)
        row = QHBoxLayout()
        self.review_refresh_btn = QPushButton("Refresh")
        self.review_refresh_btn.clicked.connect(lambda _c=False: self.refresh_review())
        self.zoom_btn = QPushButton("Zoom to")
        self.zoom_btn.clicked.connect(lambda _c=False: self._zoom_to_selected())
        self.accept_btn = QPushButton("Accept")
        self.accept_btn.setToolTip("Screened and looked at; keep the result as it is.")
        self.accept_btn.clicked.connect(lambda _c=False: self._decide("accept"))
        self.exclude_btn = QPushButton("Exclude")
        self.exclude_btn.setToolTip("Not a dam / not to be screened: steps 5-8 skip it from now on.")
        self.exclude_btn.clicked.connect(lambda _c=False: self._decide("exclude"))
        self.type_combo = QComboBox()
        for t in rv.POND_TYPES:
            self.type_combo.addItem(t, t)
        self.set_type_btn = QPushButton("Set pond type")
        self.set_type_btn.clicked.connect(lambda _c=False: self._decide("set_type", self.type_combo.currentData()))
        self.move_btn = QPushButton("Move breach point...")
        self.move_btn.setToolTip("Give the new point as 'x, y' in EPSG:5070 metres (the map canvas centre is "
                                 "offered when a map is open); dir_conf becomes 'reviewed'.")
        self.move_btn.clicked.connect(lambda _c=False: self._move_breach())
        self.note_btn = QPushButton("Add note...")
        self.note_btn.clicked.connect(lambda _c=False: self._add_note())
        for b in (self.review_refresh_btn, self.zoom_btn, self.accept_btn, self.exclude_btn):
            row.addWidget(b)
        row.addWidget(self.type_combo)
        row.addWidget(self.set_type_btn)
        row.addWidget(self.move_btn)
        row.addWidget(self.note_btn)
        row.addStretch(1)
        lay.addLayout(row)
        self.review_label = QLabel("")
        self.review_label.setWordWrap(True)
        lay.addWidget(self.review_label)
        w.setLayout(lay)
        return w

    # -- Results
    def _build_results_tab(self):
        w = QWidget()
        lay = QVBoxLayout()
        self.counts_label = QLabel("")
        self.counts_label.setWordWrap(True)
        self.record_label = QLabel("")
        self.record_label.setWordWrap(True)
        lay.addWidget(self.counts_label)
        lay.addWidget(self.record_label)
        row = QHBoxLayout()
        self.report_btn = QPushButton("Open the report (index.html)")
        self.report_btn.clicked.connect(self._open_report)
        self.add_btn = QPushButton("Add the results to the project")
        self.add_btn.setToolTip("One layer group 'ND Dam Screening - <work folder>' with the dams by class, "
                                "the flood extents, the breach points and arrows, and the review queue; "
                                "every layer styled.")
        self.add_btn.clicked.connect(lambda _c=False: self._add_results())
        self.runner_btn = QPushButton("Open Runner...")
        self.runner_btn.setToolTip(
            "Open ND Dam Breach Runner with the dams folder pre-filled — send priority dams to the full "
            "detail model. The Runner reads the same dam.json records Screening wrote.")
        self.runner_btn.clicked.connect(self._open_runner)
        self.hazard_btn = QPushButton("Open Hazard Helper...")
        self.hazard_btn.setToolTip("The classification authority: take the dams that matter there "
                                   "(its 'Dam suite' group reads the same dam.json records).")
        self.hazard_btn.clicked.connect(self._open_hazard)
        row.addWidget(self.report_btn)
        row.addWidget(self.add_btn)
        row.addWidget(self.runner_btn)
        row.addWidget(self.hazard_btn)
        lay.addLayout(row)
        self.results_continue_btn = self._continue_button()       # 0.28.1: the same as the Simple tab's
        row = QHBoxLayout()
        row.addWidget(self.results_continue_btn)
        row.addStretch(1)
        lay.addLayout(row)

        # Sortable dam table (item 9: populated when step 8 output is present)
        self.dam_table = QTableWidget(0, len(DAM_COLUMNS))
        self.dam_table.setHorizontalHeaderLabels(list(DAM_COLUMNS))
        self.dam_table.setSortingEnabled(True)
        # 0.26.0: scoped enum names (Qt6 / QGIS 4 has no QAbstractItemView.SelectRows), as line ~978
        sel = getattr(QAbstractItemView, "SelectionBehavior", QAbstractItemView)
        trig = getattr(QAbstractItemView, "EditTrigger", QAbstractItemView)
        mode = getattr(QHeaderView, "ResizeMode", QHeaderView)
        self.dam_table.setSelectionBehavior(sel.SelectRows)
        self.dam_table.setEditTriggers(trig.NoEditTriggers)
        hdr = self.dam_table.horizontalHeader()
        hdr.setSectionResizeMode(0, mode.ResizeToContents)
        hdr.setSectionResizeMode(1, mode.Stretch)
        hdr.setSectionResizeMode(2, mode.ResizeToContents)
        hdr.setSectionResizeMode(3, mode.ResizeToContents)
        hdr.setSectionResizeMode(4, mode.ResizeToContents)
        hdr.setSectionResizeMode(5, mode.Stretch)                # 0.28.0: the run flags, in red
        self.dam_table.setMaximumHeight(260)
        try:                                     # 0.28.1: Continue follows the selected dam
            self.dam_table.itemSelectionChanged.connect(self._continue_follow)
        except Exception:                                        # noqa - test doubles
            pass
        # 0.28.0: the same red line as the Simple tab's step line, above the table
        self.dam_flags_label = QLabel("")
        self.dam_flags_label.setWordWrap(True)
        self.dam_flags_label.setStyleSheet(RED)
        lay.addWidget(self.dam_flags_label)
        lay.addWidget(self.dam_table)

        self.one_dam_label = QLabel("")
        self.one_dam_label.setWordWrap(True)
        lay.addWidget(self.one_dam_label)
        row = QHBoxLayout()
        self.one_dam_combo = QComboBox()
        self.one_dam_report_btn = QPushButton("Open this one-dam report")
        self.one_dam_report_btn.clicked.connect(lambda _c=False: self._open_one_dam_report())
        row.addWidget(self.one_dam_combo, 1)
        row.addWidget(self.one_dam_report_btn)
        lay.addLayout(row)
        lay.addStretch(1)
        w.setLayout(lay)
        return w

    # -- Priority (0.28.0): which dams somebody has to deal with, across the whole Dams folder
    def _build_priority_tab(self):
        w = QWidget()
        lay = QVBoxLayout()
        intro = QLabel(
            "Every dam of a Dams folder ranked by the computed class AGAINST THE CLASS ON RECORD, "
            "worst disagreement first: the dams screened HIGHER than DWR has them, then the runs "
            "that are incomplete (so their class is a floor), then the rest. This is the whole "
            "folder, not only this run's county - a dam screened last month still counts. "
            "'Medium' and DWR's 'Significant' are the same tier and count as agreement.")
        intro.setWordWrap(True)
        lay.addWidget(intro)
        row = QHBoxLayout()
        row.addWidget(QLabel("Dams folder:"))
        self.priority_edit = QLineEdit()
        self.priority_edit.setToolTip(
            "The folder of dam folders (one per dam, each with its dam.json) that the suite shares. "
            "Empty = the Dams folder of this work folder.")
        b = QPushButton("Browse...")
        b.clicked.connect(lambda _c=False: self._browse_folder(self.priority_edit))
        self.priority_refresh_btn = QPushButton("Refresh the list")
        self.priority_refresh_btn.clicked.connect(lambda _c=False: self.refresh_priority())
        self.priority_export_btn = QPushButton("Export to Excel...")
        self.priority_export_btn.setToolTip("The whole list on one sheet and the review list (the dams "
                                            "that screen higher, and the incomplete runs) on another.")
        self.priority_export_btn.clicked.connect(lambda _c=False: self._export_priority())
        self.priority_export_btn.setEnabled(False)
        row.addWidget(self.priority_edit, 1)
        row.addWidget(b)
        row.addWidget(self.priority_refresh_btn)
        row.addWidget(self.priority_export_btn)
        lay.addLayout(row)
        self.priority_label = QLabel("Press 'Refresh the list' to read the Dams folder.")
        self.priority_label.setWordWrap(True)
        lay.addWidget(self.priority_label)
        self.priority_table = QTableWidget(0, len(PR.COLUMNS))
        self.priority_table.setHorizontalHeaderLabels(list(PR.COLUMNS))
        self.priority_table.setSortingEnabled(True)
        sel = getattr(QAbstractItemView, "SelectionBehavior", QAbstractItemView)
        trig = getattr(QAbstractItemView, "EditTrigger", QAbstractItemView)
        mode = getattr(QHeaderView, "ResizeMode", QHeaderView)
        self.priority_table.setSelectionBehavior(sel.SelectRows)
        self.priority_table.setEditTriggers(trig.NoEditTriggers)
        hdr = self.priority_table.horizontalHeader()
        for i in range(len(PR.COLUMNS)):
            hdr.setSectionResizeMode(i, mode.Stretch if PR.COLUMNS[i] in ("Name", "Why", "Next")
                                     else mode.ResizeToContents)
        lay.addWidget(self.priority_table, 1)
        lay.addStretch(0)
        w.setLayout(lay)
        return w

    def priority_folder(self):
        """The Dams folder the Priority tab reads: the box, else this work folder's."""
        chosen = self.priority_edit.text().strip()
        if chosen:
            return chosen
        work = self.work_edit.text().strip()
        return wf.dams_folder(work, self.settings()) if work else ""

    def _priority_rows(self, folder):
        """The ranked rows of a Dams folder. damsuite.priority reads the run flags out of each
        row's "note", which the folder scan has no field for, so they are filled in from the dam
        records first (core/incomplete.note_rows) - otherwise a truncated flood ranks as "agree"
        and the list hides exactly what this release is about."""
        try:
            from .damsuite import suite_home as SH
            return PR.rank(ic.note_rows(SH.scan(folder)))
        except Exception:                                        # noqa - fall back to plain
            return PR.scan_and_rank(folder)

    def refresh_priority(self):
        """Read the Dams folder and fill the table (damsuite.priority). Never raises: a folder
        that cannot be read says so."""
        try:
            folder = self.priority_folder()
            if not folder or not os.path.isdir(folder):
                self.priority_rows = []
                self.priority_table.setRowCount(0)
                self.priority_export_btn.setEnabled(False)
                self.priority_label.setText(
                    "No Dams folder yet: {}. It is written by step 8 (and by 'Run selected dams...'); "
                    "or choose another folder above.".format(folder or "none chosen"))
                return []
            rows = self._priority_rows(folder)
            self.priority_rows = rows
            table = PR.table(rows)[1:]
            self.priority_table.setSortingEnabled(False)
            self.priority_table.setRowCount(0)
            for r, line in zip(rows, table):
                n = self.priority_table.rowCount()
                self.priority_table.insertRow(n)
                for c, v in enumerate(line):
                    item = QTableWidgetItem("" if v is None else str(v))
                    if r.get("verdict") in ("raise", "incomplete"):
                        try:
                            from qgis.PyQt.QtGui import QBrush, QColor
                            item.setForeground(QBrush(QColor(176, 0, 32)))
                        except Exception:                        # noqa
                            pass
                    self.priority_table.setItem(n, c, item)
            self.priority_table.setSortingEnabled(True)
            self.priority_export_btn.setEnabled(bool(rows))
            self.priority_label.setText("{}  ({})".format(PR.summary_text(rows), folder))
            self._log("Priority list: {} dam(s) read from {}".format(len(rows), folder))
            return rows
        except Exception as e:                                   # noqa - a list is never a crash
            try:
                self.priority_label.setText("The Dams folder could not be read: {}".format(e))
            except Exception:                                    # noqa
                pass
            return []

    def _export_priority(self):
        """The list as a spreadsheet (damsuite.priority.write_xlsx: every dam, then the review
        list on its own sheet)."""
        try:
            rows = getattr(self, "priority_rows", None) or self.refresh_priority()
            if not rows:
                QMessageBox.information(self, TITLE, "Nothing to export yet: press 'Refresh the list'.")
                return False
            start = os.path.join(self.priority_folder() or os.path.expanduser("~"),
                                 "dam_priority_review.xlsx")
            path, _f = QFileDialog.getSaveFileName(self, "Export the priority list", start,
                                                   "Excel workbook (*.xlsx)")
            if not path:
                return False
            if not path.lower().endswith(".xlsx"):
                path += ".xlsx"
            PR.write_xlsx(path, rows)
            self._log("Priority list written to {}".format(path))
            QMessageBox.information(self, TITLE, "Written: {}\n\n{} dam(s), {} on the review "
                                    "list.".format(path, len(rows), len(PR.attention(rows))))
            return True
        except Exception as e:                                   # noqa
            QMessageBox.warning(self, TITLE, "The priority list could not be written: {}".format(e))
            return False

    @staticmethod
    def _combo(items):
        c = QComboBox()
        for i, t in enumerate(items):
            c.addItem(t, i)
        return c

    # ---------------------------------------------------------------- settings --
    _EDITS = ("local_dem", "exe", "chart_file", "runner_dem", "dams_folder", "ref_folder", "old_inventory")
    _COMBOS = ("terrain_source", "larger_clips", "engine", "scenario", "crit", "buildings", "ref_units",
               "preset")                                           # 0.28.0
    _CHECKS = ("baseline", "incremental", "both_scenarios", "both_one_dam", "pregrow", "skip_done")
    _SPINS = ("baseline_cfs", "building_raise_ft", "building_n", "dem_cell_ft", "grid_ft", "simh", "simmax")

    def settings(self):
        """The window's choices as the dict core/workflow.py takes."""
        s = dict(wf.DEFAULT_SETTINGS)
        s["work"] = self.work_edit.text().strip()
        s["package"] = self.package_edit.text().strip()
        for k in self._EDITS:
            s[k] = getattr(self, k).text().strip()
        for k in self._COMBOS:
            d = getattr(self, k).currentData()
            s[k] = int(d) if d is not None else 0
        s["workers"] = int(self.workers.value())
        for k in self._CHECKS:
            s[k] = bool(getattr(self, k).isChecked())
        for k in self._SPINS:
            s[k] = float(getattr(self, k).value())
        s["chain"] = [k for k, c in self.chain_check.items() if c.isChecked()]
        s["screen_what"] = self.screen_what()
        s["settings_file"] = self.settings_file_edit.text().strip()
        s["reviewer"] = self.reviewer_edit.text().strip()
        s["counties"] = self.county_combo.currentData() or ""          # 0.21.1
        for slot, combo in self.layer_combo.items():
            s[slot] = combo.currentData() or ""
        return s

    def screen_what(self):
        for i, rb in enumerate(self.screen_radio):
            if rb.isChecked():
                return i
        return 0

    def _apply(self, s):
        s = wf.migrate_settings(s)
        self.work_edit.setText(s.get("work", ""))
        self.package_edit.setText(s.get("package", ""))
        for k in self._EDITS:
            getattr(self, k).setText(s.get(k, ""))
        for k in self._COMBOS:
            combo = getattr(self, k)
            i = combo.findData(int(s.get(k, 0) or 0))
            combo.setCurrentIndex(i if i >= 0 else 0)
        self.workers.setValue(int(s.get("workers", 0) or 0))
        for k in self._CHECKS:
            getattr(self, k).setChecked(bool(s.get(k, wf.DEFAULT_SETTINGS.get(k, False))))
        for k in self._SPINS:
            try:
                getattr(self, k).setValue(float(s.get(k, wf.DEFAULT_SETTINGS[k])))
            except (TypeError, ValueError):
                getattr(self, k).setValue(float(wf.DEFAULT_SETTINGS[k]))
        for k, c in self.chain_check.items():
            c.setChecked(k in (s.get("chain") or []))
        try:                                                           # 0.21.1: the county
            from .core import nd_counties as cty
            first = (str(s.get("counties") or "").split(",") + [""])[0]
            i = self.county_combo.findData(cty.normalize(first))
            self.county_combo.setCurrentIndex(i if i > 0 else 0)
        except Exception:                                              # noqa
            pass
        what = int(s.get("screen_what", 0) or 0)
        for i, rb in enumerate(self.screen_radio):
            rb.setChecked(i == what)
        self.settings_file_edit.setText(s.get("settings_file", "") or "")
        self.reviewer_edit.setText(s.get("reviewer", "") or "")
        self._pending_layers = {slot: s.get(slot, "") for slot in self.layer_combo}

    def _save_state(self):
        if self._loading:
            return
        try:
            from qgis.core import QgsProject
            QgsProject.instance().writeEntry(SCOPE, ENTRY, json.dumps(self.settings()))
        except Exception:                                        # noqa
            pass

    def _load_state(self):
        s = dict(wf.DEFAULT_SETTINGS)
        try:
            from qgis.core import QgsProject
            text, ok = QgsProject.instance().readEntry(SCOPE, ENTRY, "")
            if ok and text:
                loaded = json.loads(text)
                if isinstance(loaded, dict):
                    s.update(wf.migrate_settings(loaded))
        except Exception:                                        # noqa
            pass
        if not (s.get("settings_file") or "").strip():
            s["settings_file"] = DS.current_path() or ""
        self._apply(s)

    def _changed(self, *args):
        self._save_state()
        self._autosave_settings()
        self.refresh()

    def _screen_toggled(self, idx, on):
        """The Simple tab's radios drive the chain's 16 / 18 boxes."""
        if not on or self._loading:
            return
        for i, rb in enumerate(self.screen_radio):
            if i != idx and rb.isChecked():
                rb.setChecked(False)
        was = self._loading
        self._loading = True
        try:
            self.chain_check["16"].setChecked(idx in (1, 2))
            self.chain_check["18"].setChecked(idx == 2)
        finally:
            self._loading = was
        self._changed()

    def _chain_check_toggled(self, *args):
        if self._loading:
            return
        was = self._loading
        self._loading = True
        try:
            c16, c18 = self.chain_check["16"].isChecked(), self.chain_check["18"].isChecked()
            what = 2 if (c16 and c18) else (1 if c16 else 0)
            for i, rb in enumerate(self.screen_radio):
                rb.setChecked(i == what)
        finally:
            self._loading = was
        self._changed()

    # ------------------------------------------------------------ settings file --
    def _settings_file_edited(self):
        self._load_settings_file(quiet=False)
        self._changed()

    def _pick_settings_file(self):
        start = self.settings_file_edit.text().strip() or self.work_edit.text().strip() or ""
        p, _f = QFileDialog.getOpenFileName(self, "Settings file", start,
                                            "Settings workbook (*.xlsx);;Settings table (*.csv);;All files (*)")
        if p:
            self.settings_file_edit.setText(p)
            self._settings_file_edited()

    def _new_settings_file(self):
        work = self.work_edit.text().strip()
        start = os.path.join(work, DS.DEFAULT_NAME) if work else DS.DEFAULT_NAME
        p, _f = QFileDialog.getSaveFileName(self, "New settings file", start,
                                            "Settings workbook (*.xlsx);;Settings table (*.csv)")
        if not p:
            return ""
        if not os.path.splitext(p)[1]:
            p += ".xlsx"
        self.settings_file_edit.setText(p)
        if self.grid is not None:
            wf.window_settings_to(self.grid.settings, self.settings())
            self.grid.settings.path = p
            self.grid.save(p, name=os.path.splitext(os.path.basename(p))[0].replace("_", " "))
        DS.set_current_path(p)
        self._log("Settings file written: {}".format(p))
        self._fill_presets()
        self._save_state()
        self.refresh()
        return p

    def _load_settings_file(self, quiet=True):
        """Read the settings file named on the Simple tab into the grid and the window."""
        p = self.settings_file_edit.text().strip()
        DS.set_current_path(p)
        if self.grid is None:
            return False
        if not p:
            self.grid.settings = DS.Settings()
            self.grid.fill()
            self._fill_presets()
            return False
        if not os.path.isfile(p):
            if not quiet:
                QMessageBox.warning(self, TITLE, "The settings file does not exist: {}".format(p))
            self._log("!!! settings file not found: {}".format(p))
            self._fill_presets()
            return False
        ok = self.grid.load(p, quiet=quiet)
        if ok:
            win = wf.window_settings_from(self.grid.settings)
            if win:
                was = self._loading
                self._loading = True
                try:
                    s = self.settings()
                    s.update(win)
                    self._apply(s)
                    self._fill_layers()
                finally:
                    self._loading = was
            self._log("Settings file: {} ({} value(s) differ from the starting values)".format(
                p, len(self.grid.settings.changed(self.grid.catalog))))
        self._fill_presets()
        return ok

    def _grid_changed(self, settings):
        """An edit in the grid: the file is saved at once when there is one (the tools read it)."""
        self._autosave_settings()

    def _grid_file(self, path):
        if path and path != self.settings_file_edit.text().strip():
            self.settings_file_edit.setText(path)
            DS.set_current_path(path)
            self._save_state()
        self._fill_presets()

    def _autosave_settings(self):
        if self._loading or self.grid is None:
            return
        p = self.settings_file_edit.text().strip()
        if not p:
            return
        try:
            wf.window_settings_to(self.grid.settings, self.settings())
            self.grid.settings.path = p
            self.grid.settings.save(p, catalog=self.grid.catalog, versions=self.grid.versions or None)
            DS.set_current_path(p)
        except DS.SettingsError as e:
            self._log("!!! settings not saved: {}".format(e))

    def _fill_presets(self):
        folders = [PRESETS_DIR]
        p = self.settings_file_edit.text().strip()
        if p:
            folders.append(os.path.dirname(p))
        self.presets = DS.list_presets(folders)
        self.preset_combo.clear()
        self.preset_combo.addItem("(choose a preset)", "")
        for name, path in self.presets:
            if p and os.path.abspath(path) == os.path.abspath(p):
                continue
            self.preset_combo.addItem(name, path)

    def _apply_preset(self):
        path = self.preset_combo.currentData()
        if not path:
            return False
        try:
            preset = DS.Settings.load(path)
        except DS.SettingsError as e:
            QMessageBox.warning(self, TITLE, str(e))
            return False
        n = len([k for k in preset.values if k[0] == SG.PLUGIN])
        if QMessageBox.question(self, TITLE, "Copy the {} value(s) of the preset '{}' into the current settings?"
                                "\n{}".format(n, self.preset_combo.currentText(), preset.meta.get("note", ""))) \
                != self._yes():
            return False
        if self.grid is not None:
            for (pl, tool, key), v in preset.values.items():
                if pl == SG.PLUGIN:
                    self.grid.settings.set(pl, tool, key, v)
            self.grid.fill()
            win = wf.window_settings_from(self.grid.settings)
            if win:
                s = self.settings()
                s.update(win)
                was = self._loading
                self._loading = True
                try:
                    self._apply(s)
                    self._fill_layers()
                finally:
                    self._loading = was
        self._log("Preset applied: {} ({})".format(self.preset_combo.currentText(), path))
        self._changed()
        return True

    # ------------------------------------------------------------------ layers --
    @staticmethod
    def project_layers():
        """[(name, id)] of the layers in the project (empty when there is no project)."""
        rows = []
        try:
            from qgis.core import QgsProject
            for lyr in QgsProject.instance().mapLayers().values():
                nm = lyr.name() if callable(lyr.name) else lyr.name
                rows.append((str(nm), lyr.id()))
        except Exception:                                        # noqa
            return []
        return sorted(rows, key=lambda r: r[0].lower())

    def _read_layer(self, ref):
        """A QgsVectorLayer for a project layer id or a file ('path' / 'path|layername=x'); None
        when it cannot be opened."""
        if not ref:
            return None
        try:
            from qgis.core import QgsProject, QgsVectorLayer
        except Exception:                                        # noqa
            return None
        try:
            lyr = QgsProject.instance().mapLayers().get(ref)
            if lyr is not None:
                return lyr
            lyr = QgsVectorLayer(ref, os.path.basename(str(ref).split("|")[0]), "ogr")
            return lyr if lyr.isValid() else None
        except Exception:                                        # noqa
            return None

    def _read_rows(self, ref, fields):
        lyr = self._read_layer(ref)
        if lyr is None:
            return None
        names = lyr.fields().names()
        return [tuple(f[x] if x in names else None for x in fields) for f in lyr.getFeatures()]

    def _fill_layers(self):
        """Fill every layer list with the project's layers, keeping what is chosen (also a file
        chosen with 'File...' or a layer that is no longer in the project)."""
        was_loading = self._loading
        self._loading = True
        try:
            layers = self.project_layers()
            pending = getattr(self, "_pending_layers", {})
            for slot, combo in self.layer_combo.items():
                keep = pending.get(slot) if slot in pending else (combo.currentData() or "")
                names = {}
                combo.clear()
                combo.addItem("(the package's layer / none)", "")
                for nm, lid in layers:
                    combo.addItem(nm, lid)
                    names[lid] = nm
                if keep:
                    i = combo.findData(keep)
                    if i < 0:
                        combo.addItem(os.path.basename(keep.split("|")[0]) or keep, keep)
                        i = combo.count() - 1
                    combo.setCurrentIndex(i)
                else:
                    combo.setCurrentIndex(0)
            self._pending_layers = {}
        finally:
            self._loading = was_loading

    def _fill_from_suite(self):
        """0.21.0: fill the empty layer slots from the suite data (wf.fill_empty_slots)."""
        work = self.work_edit.text().strip()
        if not work:
            QMessageBox.information(self, TITLE, "Choose a work folder first: the offline pack "
                                    "is clipped into it.")
            return {}
        if not wf.suite_data_folder():
            d = QFileDialog.getExistingDirectory(self, "Suite data folder (set once for the three "
                                                 "plugins)", "")
            if not d:
                return {}
            try:
                from .damsuite import suitedata as SD
                SD.set_data_folder(d)
                self._log("Suite data folder set: {}".format(d))
            except Exception as e:                               # noqa
                self._log("!!! The suite data folder was not set: {}".format(e))
                return {}
        try:
            os.makedirs(work, exist_ok=True)
        except OSError:
            pass
        got = wf.fill_empty_slots(work, self.settings(), log=self._log, prepare=True)
        labels = dict((k, l) for k, l, _p in wf.LAYER_SLOTS)
        for slot, (value, origin) in got.items():
            combo = self.layer_combo[slot]
            i = combo.findData(value)
            if i < 0:
                base = os.path.basename(value.split("|")[0])
                lname = value.split("layername=")[-1] if "layername=" in value else ""
                combo.addItem("{}{} ({})".format(base, ":" + lname if lname else "", origin), value)
                i = combo.count() - 1
            combo.setCurrentIndex(i)
            self._log("{}: {} ({})".format(labels.get(slot, slot), value, wf.ORIGIN_TEXT[origin]))
        empty = [labels[s].split(" (")[0] for s in wf.FILL_SLOTS
                 if not self.layer_combo[s].currentData()]
        self.suite_fill_note.setText("Filled: {}. Still empty: {}.".format(
            ", ".join(labels[s].split(" (")[0] for s in got) or "nothing",
            ", ".join(empty) or "none"))
        self._changed()
        return got

    def _browse_layer(self, slot):
        path, _f = QFileDialog.getOpenFileName(
            self, "Layer file", self.work_edit.text().strip() or "",
            "Data (*.gpkg *.shp *.geojson *.json *.tif *.tiff *.vrt);;All files (*)")
        if not path:
            return
        combo = self.layer_combo[slot]
        i = combo.findData(path)
        if i < 0:
            combo.addItem(os.path.basename(path), path)
            i = combo.count() - 1
        combo.setCurrentIndex(i)            # -> _changed

    # ------------------------------------------------------------------ browse --
    def _browse_folder(self, edit):
        d = QFileDialog.getExistingDirectory(self, "Folder", edit.text().strip() or "")
        if d:
            edit.setText(d)
            self._changed()

    def _browse_file(self, edit, flt="All files (*)"):
        p, _f = QFileDialog.getOpenFileName(self, "File", edit.text().strip() or "", flt)
        if p:
            edit.setText(p)
            self._changed()

    def _browse_for(self, edit, kind):
        if kind == "dir":
            self._browse_folder(edit)
        else:
            flt = {"json": "JSON (*.json)", "raster": "Raster (*.tif *.tiff *.vrt)",
                   "file": "All files (*)"}.get(kind, "All files (*)")
            self._browse_file(edit, flt)

    # --------------------------------------------------------------------- log --
    def _log(self, msg):
        self.log_edit.append(str(msg))
        # GUI port (stage 2a): keep the folded one-line 'Log: <last line>' in step
        line = getattr(self, "log_line", None)
        if line is not None:
            text = str(msg).strip().splitlines()
            line.setText("Log: " + (text[-1][:120] if text else "-"))

    # ----------------------------------------------------------------- refresh --
    # ---------------------------------------------------------- 0.28.0: why, and how long --
    def _blocking(self, work, busy):
        """Why a Run button is grey, or what would stop a run: the suite's own blocking items
        (damsuite.suitecheck) and the failures of the last preflight. Never raises."""
        try:
            checks = self.last_checks if getattr(self, "last_checks", None) else None
            return pf.blocking_reasons(checks=checks, work=work, busy=busy)
        except Exception:                                        # noqa - a reason is never a crash
            return []

    def _run_flags(self, work):
        """{step: red line} of the run flags of steps 7 and 8 ({} when there are none). Read with
        sqlite3, so it works while a run is going on."""
        out = {}
        try:
            for step in ("7", "8"):
                p = wf.out(work, step)
                text = ic.summary_text(ic.from_gpkg(p)) if p else ""
                if text:
                    out[step] = text
        except Exception:                                        # noqa
            return out
        return out

    def _show_estimate(self, work, settings, have):
        """"37 dam(s) x about 12 s/dam at 164 ft (50 m) = about 2 min (4 at a time)" next to the
        elapsed timer, and what is left of an interrupted run (core/runplan, core/resume)."""
        try:
            if not have:
                self.estimate_label.setText("")
                return
            n = wf.dam_count(work)
            if not n:
                self.estimate_label.setText("Run step 1 to see how long a run of this county takes.")
                return
            left = None
            outdir = wf.path(work, "7", "OUTDIR")
            if settings.get("skip_done", True):
                left = rs.pending_count(outdir, n)
            text = rp.run_time_text(n, settings, pending=left)
            was = rs.summary_text(outdir, total=n)
            if was:
                # an earlier run of this county left a manifest: say whether those dams are being
                # picked up or thrown away, because the switch decides hours of model time
                was = ("Resuming: " + was) if settings.get("skip_done", True) else \
                    ("Starting again: " + was + " They are being RE-RUN ('skip dams that already "
                     "have a result' is off).")
                text = (text + " " + was) if text else was
            self.estimate_label.setText(text)
            self.estimate_label.setToolTip(rp.cell_label(settings))
        except Exception:                                        # noqa
            pass

    def refresh(self, *args):
        """Re-read the work folder: the state of every step and the results."""
        work = self.work_edit.text().strip()
        s = self.settings()
        have = bool(work and os.path.isdir(work))
        rows = wf.statuses(work, s) if have else [
            (k, wf.TITLE[k], "optional" if k in wf.OPTIONAL else "waiting", "") for k in wf.KEYS]
        what = s.get("screen_what", 0)
        flags = self._run_flags(work) if have else {}
        for key, _title, state, note in rows:
            self.step_state[key].setText(STATE_TEXT.get(state, state))
            self.step_note[key].setText(note)
            if key in self.simple_state:
                self.simple_state[key].setText(STATE_TEXT.get(state, state))
                # 0.28.0: a truncated flood is not a finished answer - the reason belongs here,
                # in red, not only in the CSV report (core/incomplete.py)
                flag = flags.get(key, "")
                self.simple_note[key].setText(
                    ((note + " - ") if note and flag else note) + flag)
                self.simple_note[key].setStyleSheet(RED if flag else "")
                # the steps the chosen path does not use are shown greyed, not removed
                used = not ((key in wf.POND_COVERS and what == 1) or (key == "16" and what == 0) or
                            (key == "18" and what != 2))
                self.simple_name[key].setEnabled(used)
                self.simple_state[key].setEnabled(used)
        nxt = wf.next_step(work, s) if have else None
        busy = self.chain is not None and self.chain.active
        # GUI port (stage 2a): the pipeline strip, fed from the SAME wf.statuses rows above
        try:
            by = {k: state for k, _t, state, _n in rows}
            self._refresh_pipeline(by, busy, have, work)
        except Exception:                                        # noqa - the strip is never a crash
            pass
        for k in wf.KEYS:
            self.step_button[k].setEnabled(have and not busy)
        self.next_btn.setEnabled(bool(nxt) and not busy)
        # 0.28.0: a grey Run button says WHY, in its tooltip and in the status line - the suite's
        # own blocking items (damsuite.suitecheck) and the last preflight's failures
        can_run = have and not busy
        for b in (self.chain_btn, self.run_all_btn, self.one_dam_btn, self.sel_dams_btn,
                  self.preflight_btn):
            b.setEnabled(can_run)
        # 0.28.0: the reasons EXPLAIN a grey button. They do not grey one out: a run with layers
        # chosen by hand works without the suite folder, and always has. What the suite is missing
        # is a line of the preflight instead (core/preflight.run_checks).
        reasons = self._blocking(work, busy) if not can_run else []
        why = pf.blocking_text(reasons)
        self.run_all_btn.setToolTip(why or RUN_COUNTY_TIP)
        # the reason replaces the button's own tooltip while it is grey, and gives it back when
        # the problem is fixed (a stale "Cannot run" on a working button is its own bug)
        for b in (self.chain_btn, self.one_dam_btn, self.sel_dams_btn):
            b.setToolTip(why or self._run_tips.get(id(b), ""))
        self.chain_stop_btn.setEnabled(busy)
        self.stop_btn.setEnabled(busy)
        # 0.28.0: without a work folder the first sentence is still the one thing to do, and the
        # other reasons follow it - they are what the run would stop on next
        rest = [r for r in reasons if "work folder" not in r.lower()] if not have else []
        text = ("Choose or create a work folder first."
                + (" Also: " + "; ".join(rest) if rest else "") if not have else
                (why if why else
                 "Next: {} - {}. {}".format(nxt, wf.TITLE[nxt], wf.HINT[nxt]) if nxt else
                 "Nothing required is left: every non-optional step that can start is done."))
        self.hint_label.setText(text)
        self._show_estimate(work, s, have)
        self.simple_hint.setText(text if (not have or why) else
                                 ("Next: {}. {}".format(SG.plain(nxt, wf.TITLE[nxt]), wf.HINT[nxt]) if nxt
                                  else "Everything required is done. Results tab: report and layers."))
        self.simple_hint.setStyleSheet(RED if why else "")
        try:                                     # 0.28.1: what the run will read, and from where
            self.found_label.setText(wf.found_layers_text(work if have else "", s))
        except Exception:                                        # noqa
            self.found_label.setText("")
        self._refresh_continue(work if have else "", busy)
        try:                                     # 0.28.0: the cell size step 7 will really use
            self.cell_label.setText(rp.cell_label(s))
            self.grid_ft.setEnabled(rp.preset_index(s) == rp.CUSTOM)
        except Exception:                                        # noqa
            pass
        self.counts_label.setText(wf.counts_text(wf.summary_counts(work)))
        self.record_label.setText(wf.dam_record_text(wf.dams_folder(work, s)) if have else "")
        self.report_btn.setEnabled(have and os.path.isfile(os.path.join(work, wf.REPORT_INDEX)))
        self.add_btn.setEnabled(have and bool(wf.result_layers(work)))
        runs = wf.one_dam_runs(work) if have else []
        self.one_dam_label.setText("One-dam runs: {}".format(", ".join(d for d, _f in runs) if runs else "none yet"))
        cur = self.one_dam_combo.currentData()
        self.one_dam_combo.clear()
        for d, folder in runs:
            self.one_dam_combo.addItem(d, folder)
        if cur:
            i = self.one_dam_combo.findData(cur)
            if i >= 0:
                self.one_dam_combo.setCurrentIndex(i)
        self.one_dam_report_btn.setEnabled(bool(runs))
        if have:
            self.refresh_review()
            self._refresh_dam_table(work)
            self._refresh_continue(work, busy)      # the table's selection may have moved

    def changeEvent(self, event):
        try:
            super().changeEvent(event)
        except Exception:                                        # noqa
            pass
        try:
            if self.isActiveWindow():
                self.refresh()
        except Exception:                                        # noqa
            pass

    # ------------------------------------------------------------------- tools --
    def _open_step(self, key):
        work = self.work_edit.text().strip()
        if not work:
            QMessageBox.information(self, TITLE, "Choose a work folder first.")
            return False
        if not os.path.isdir(work):
            try:
                os.makedirs(work)
                self._log("Created the work folder {}".format(work))
            except OSError as e:
                QMessageBox.warning(self, TITLE, "The work folder cannot be created: {}".format(e))
                return False
        # 0.16.0: empty data slots fall back to the dam folder / suite offline pack / dwr/ (logged)
        params = wf.defaults(key, work, self.settings(), log=self._log, prepare=True)
        aid = wf.algorithm_id(key)
        if key in ("2", "3", "4") and "DAMS" not in params:      # 0.21.0: say why it is empty
            self._log(wf.missing_dams_note(work))
        try:
            try:
                import processing
            except ImportError:
                from qgis import processing
            processing.execAlgorithmDialog(aid, params)
        except Exception as e:                                   # noqa
            self._log("!!! {} could not be opened: {}".format(aid, e))
            QMessageBox.information(
                self, TITLE,
                "The Processing dialog of {} could not be opened here ({}).\n\nOpen it from the "
                "Processing Toolbox: ND Dam Breach Screening > {}.".format(aid, e, wf.TITLE[key]))
            return False
        self._log(">>> Opened {} ({})".format(aid, ", ".join(
            "{}={}".format(k, os.path.basename(str(v)) if os.path.isabs(str(v).split("|")[0]) else v)
            for k, v in sorted(params.items())) or "no values filled"))
        self._save_state()
        return True

    def _open_next(self):
        work = self.work_edit.text().strip()
        nxt = wf.next_step(work, self.settings()) if work and os.path.isdir(work) else None
        if nxt:
            self._open_step(nxt)

    def _open_help(self):
        if os.path.isfile(HELP_FILE):
            QDesktopServices.openUrl(QUrl.fromLocalFile(HELP_FILE))
            return True
        QMessageBox.information(self, TITLE, "The help page is missing from this install ({}).".format(HELP_FILE))
        return False

    # --------------------------------------------------------------- preflight --
    def _preflight(self, steps=None, settings=None, work=None):
        """Run the checks and show them on the Simple tab. Returns the checks."""
        work = work or self.work_edit.text().strip()
        S = settings or self.settings()
        include = tuple(k for k in wf.CHAIN_OPTIONAL if k in (S.get("chain") or []))
        if steps is None and work and os.path.isdir(work):
            steps = wf.chain_plan(work, S, include)
        checks = pf.run_checks(work, S, steps=steps, env=QgisEnv(self._read_layer))
        self.last_checks = checks
        lines = pf.report_lines(checks)
        self.preflight_text.setPlainText("\n".join(lines))
        self.preflight_label.setText("Check: " + pf.summary(checks))
        for line in lines[:1]:
            self._log(line)
        return checks

    # ------------------------------------------------------------ run in order --
    def _yes(self):
        sb = getattr(QMessageBox, "StandardButton", QMessageBox)
        return sb.Yes

    def _definitions(self, aid):
        """[(name, description, optional, has_default, is_destination)] of a tool's parameters
        (core/workflow.required_missing). A hook: the tests replace it."""
        from qgis.core import QgsApplication, QgsProcessingParameterDefinition as D
        reg = QgsApplication.processingRegistry()
        alg = reg.createAlgorithmById(aid)
        try:                                  # QGIS 4 / PyQt6: scoped enum
            flag = D.Flag.FlagOptional
        except AttributeError:                # QGIS 3.x
            flag = D.FlagOptional
        out = []
        for d in alg.parameterDefinitions():
            out.append((d.name(), d.description(), bool(d.flags() & flag),
                        d.defaultValue() is not None, bool(d.isDestination())))
        return out

    def _chain_problems(self, step, aid, params):
        try:
            return ["{} is empty".format(m) for m in wf.required_missing(self._definitions(aid), params)]
        except Exception as e:                                   # noqa
            self._log("(parameters of {} could not be checked: {})".format(aid, e))
            return []

    def _start_algorithm(self, step, aid, params, on_done):
        """Run a tool in a background task and call on_done(ok, message) when it ends."""
        from qgis.core import (QgsApplication, QgsProcessingAlgRunnerTask, QgsProcessingContext,
                               QgsProcessingFeedback, QgsProject)
        alg = QgsApplication.processingRegistry().createAlgorithmById(aid)
        if alg is None:
            return on_done(False, "{} is not available".format(aid))
        ctx = QgsProcessingContext()
        ctx.setProject(QgsProject.instance())
        dlg = self

        class _Feedback(QgsProcessingFeedback):
            def pushInfo(self, info):
                dlg._bridge_log(info)
                super().pushInfo(info)

            def pushWarning(self, warning):
                dlg._bridge_log("WARNING: " + str(warning))
                super().pushWarning(warning)

            def reportError(self, error, fatalError=False):
                dlg._bridge_log("ERROR: " + str(error))
                super().reportError(error, fatalError)

            def setProgress(self, progress):
                super().setProgress(progress)
                # item 11: route step 7 (flood simulations) progress to the bar
                if step == "7":
                    try:
                        from qgis.PyQt.QtCore import QTimer
                        QTimer.singleShot(0, lambda p=progress: dlg._update_progress(step, p))
                    except Exception:                            # noqa
                        pass

        fb = _Feedback()
        ok, why = alg.checkParameterValues(params, ctx)
        if not ok:
            return on_done(False, why or "the values were refused")
        task = QgsProcessingAlgRunnerTask(alg, params, ctx, fb)

        def finished(successful, results=None):
            self._keep = []
            self._task = None
            on_done(bool(successful), "" if successful else "see the log above and the Processing log")

        task.executed.connect(finished)
        self._keep = [alg, ctx, fb, task]
        self._task = task
        QgsApplication.taskManager().addTask(task)

    def _bridge_log(self, text):
        """Feedback arrives on the task's thread: hand it to the window's thread."""
        try:
            from qgis.PyQt.QtCore import QTimer
            QTimer.singleShot(0, lambda t=str(text): self._log("    " + t))
        except Exception:                                        # noqa
            pass

    def _run_chain(self, preflight=False):
        """Run the steps in order; with `preflight` (the Simple tab's Run all) the check list runs
        first and a FAIL stops it."""
        work = self.work_edit.text().strip()
        if not work or not os.path.isdir(work):
            QMessageBox.information(self, TITLE, "Choose a work folder first.")
            return False
        if self.chain is not None and self.chain.active:
            return False
        S = self.settings()
        include = tuple(k for k in wf.CHAIN_OPTIONAL if k in S.get("chain", []))
        # what would run, and which data it would run without
        first, problem = wf.chain_next(work, S, include, ())
        if first is None and problem:
            QMessageBox.information(self, TITLE, "Nothing can run: " + problem + ".")
            return False
        steps_planned = wf.chain_plan(work, S, include)
        if not steps_planned:
            QMessageBox.information(self, TITLE, "Nothing to run: every required step is done.")
            return False
        if preflight:
            checks = self._preflight(steps=steps_planned, settings=S)
            if not pf.ready(checks):
                QMessageBox.warning(self, TITLE, "Not ready: " + pf.summary(checks) +
                                    "\n\nSee the check list on the Simple tab and fix it first.")
                return False
        plan = ["{} - {}".format(st, wf.TITLE[st]) for st in steps_planned]
        notes = []
        for st in steps_planned:
            notes += wf.chain_summary(st, work, S)
        text = "Run these steps one after the other?\n\n" + "\n".join(plan)
        if notes:
            text += "\n\nData the steps will use:\n" + "\n".join(notes)
        if QMessageBox.question(self, TITLE, text) != self._yes():
            return False
        self._save_state()
        try:
            wf.write_run_settings(work, S, self._log)
        except Exception as e:                                   # noqa
            self._log("settings record not written: {}".format(e))
        self._one_dam = None
        self.chain = wf.Chain(work, self.settings, include, self._start_algorithm, log=self._log,
                              problems=self._chain_problems, on_end=self._chain_ended)
        self.chain.begin()
        self.refresh()
        return True

    def _export_settings(self, path=None):
        """The settings JSON for the headless chain tool (algorithms/run_chain.load_settings)."""
        if path is None:
            work = self.work_edit.text().strip()
            path, _f = QFileDialog.getSaveFileName(
                self, "Settings JSON", os.path.join(work, "screening_settings.json") if work else "",
                "JSON (*.json)")
        if not path:
            return ""
        s = {k: v for k, v in self.settings().items() if k != "work"}
        try:
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(s, fh, indent=1, sort_keys=True)
        except OSError as e:
            QMessageBox.warning(self, TITLE, "Settings not written: {}".format(e))
            return ""
        self._log("Settings written to {} (for 15 - Run the steps in order (headless) / qgis_process "
                  "nddam:runchain --WORK=<work folder> --SETTINGS={})".format(path, path))
        return path

    def _stop_chain(self):
        if self.chain is not None and self.chain.active:
            self.chain.cancel()
            self._log("Stopping after the step that is running ...")
            task = self._task
            try:
                if task is not None:
                    task.cancel()
            except Exception:                                    # noqa
                pass

    def _chain_ended(self, ok, text):
        one = self._one_dam
        self._one_dam = None
        try:
            self.step_progress.setVisible(False)
            self.step_progress.setValue(0)
        except Exception:                                        # noqa
            pass
        self.refresh()
        if not ok:
            QMessageBox.warning(self, TITLE, text)
            return
        if one:
            dam_id, folder = one
            self._log("One-dam run finished: {} -> {}".format(dam_id, folder))
            report = os.path.join(folder, wf.REPORT_INDEX)
            if os.path.isfile(report):
                QDesktopServices.openUrl(QUrl.fromLocalFile(report))
            self._add_results(work=folder, title="ND Dam Screening - {}".format(dam_id))

    # ----------------------------------------------------------------- one dam --
    def _run_selected_dams(self, dam_ids=None):
        """0.26.0: several dams or ponds ticked in a list (wf.dam_choices: step 1's Standardized
        dams, else the DWR layer, else the ponds), run like 'Run one dam' into one folder under
        one_dam/ with ONLY_IDS = the IDs joined by ', '. Like the one-dam run, the chain gets a
        copy of the window's settings with only_ids set; the window's own setting is not touched.
        Nothing ticked (or Cancel): nothing happens. `dam_ids` given skips the list."""
        work = self.work_edit.text().strip()
        if not work or not os.path.isdir(work):
            QMessageBox.information(self, TITLE, "Choose a work folder first.")
            return False
        if self.chain is not None and self.chain.active:
            return False
        if dam_ids is None:
            choices = wf.dam_choices(work, self.settings(), self._read_rows)
            if not choices:
                QMessageBox.information(self, TITLE, "No dams to choose from yet: run step 1 (or pick the DWR "
                                                     "or water-body layer on the Inputs tab) first.")
                return False
            dam_ids = self._pick_dams(choices)
        ids = [str(x).strip() for x in (dam_ids or []) if str(x).strip()]
        if not ids:
            return False
        return self._run_one_dam(dam_ids=ids)

    def _pick_dams(self, choices):
        """0.26.0: the checkable list; the IDs ticked, [] when cancelled or nothing ticked."""
        picker = _DamPicker(self, choices)
        try:
            accepted = picker.exec()
        except AttributeError:                                   # an older Qt5 binding: exec_
            accepted = picker.exec_()
        return picker.checked_ids() if accepted else []

    def _run_one_dam(self, dam_id=None, dam_ids=None):
        """Run the whole chain for one dam in <work>/one_dam/<ID>. 0.26.0: `dam_ids` (several
        chosen dams) runs them together in one folder named after them, ONLY_IDS = the IDs joined
        by ', '."""
        work = self.work_edit.text().strip()
        if not work or not os.path.isdir(work):
            QMessageBox.information(self, TITLE, "Choose a work folder first.")
            return False
        if self.chain is not None and self.chain.active:
            return False
        S = self.settings()
        name = None
        if dam_ids:                                              # 0.26.0: Run selected dams...
            dam_id = wf.only_ids_text(dam_ids)
            name = wf.selected_dams_name(dam_ids)
            if not dam_id:
                return False
        elif dam_id is None:
            choices = wf.dam_choices(work, S, self._read_rows)
            if choices:
                labels = [lab for _d, lab in choices]
                picked, ok = QInputDialog.getItem(self, TITLE, "Which dam or pond? (type to search)", labels, 0, True)
                if not ok or not picked:
                    return False
                dam_id = dict((lab, d) for d, lab in choices).get(picked, picked.split()[0] if picked.strip() else "")
            else:
                dam_id, ok = QInputDialog.getText(self, TITLE, "Dam ID (DWR structure ID such as 5289, DWR-5289, "
                                                               "or a pond ID / water-body ID):")
                if not ok or not str(dam_id).strip():
                    return False
        dam_id = str(dam_id).strip()
        n_ids = len([x for x in dam_id.split(",") if x.strip()])
        label = "{} dams ({})".format(n_ids, dam_id) if dam_ids and n_ids > 1 else dam_id
        folder = wf.one_dam_work(work, name or dam_id)
        try:
            os.makedirs(folder, exist_ok=True)
        except OSError as e:
            QMessageBox.warning(self, TITLE, "The one-dam folder cannot be created: {}".format(e))
            return False
        S1 = wf.one_dam_settings(S, work, dam_id)
        what = S.get("screen_what", 0)
        include = tuple(k for k in ("16", "18") if (k == "16" and what in (1, 2)) or (k == "18" and what == 2))
        if "0" in (S.get("chain") or []) and not wf.package_path(work):
            include = ("0",) + include
        plan = wf.chain_plan(folder, S1, include)
        if not plan:
            QMessageBox.information(self, TITLE, "Nothing to run for {}: every step is done in {}.".format(label, folder))
            return False
        text = "Run the whole chain for {} alone?\n\nFolder: {}\nSteps: {}\n\nThe quick screen (6) is skipped so " \
               "the flood is simulated; the result is a screening of {}, not the batch.".format(
                   label, folder, ", ".join(SG.plain(k, wf.TITLE[k]) for k in plan),
                   "these dams" if n_ids > 1 else "one dam")
        if QMessageBox.question(self, TITLE, text) != self._yes():
            return False
        self._save_state()
        try:
            wf.write_run_settings(folder, S1, self._log)
        except Exception:                                        # noqa
            pass
        self._one_dam = (label, folder)
        self.chain = wf.Chain(folder, lambda: wf.one_dam_settings(self.settings(), work, dam_id), include,
                              self._start_algorithm, log=self._log, problems=self._chain_problems,
                              on_end=self._chain_ended)
        self.chain.begin()
        self.refresh()
        return True

    def _open_one_dam_report(self):
        folder = self.one_dam_combo.currentData()
        if folder:
            p = os.path.join(folder, wf.REPORT_INDEX)
            if os.path.isfile(p):
                QDesktopServices.openUrl(QUrl.fromLocalFile(p))
            else:
                QMessageBox.information(self, TITLE, "No report yet in {}.".format(folder))

    # ------------------------------------------------------------------ review --
    def refresh_review(self):
        work = self.work_edit.text().strip()
        self.review_rows = rv.queue(work, self._read_rows) if work and os.path.isdir(work) else []
        t = self.review_table
        t.setRowCount(len(self.review_rows))
        for i, r in enumerate(self.review_rows):
            for j, key in enumerate(("dam_id", "name", "why", "source", "decision")):
                t.setItem(i, j, QTableWidgetItem(str(r.get(key) or "")))
        try:
            t.resizeColumnsToContents()
        except Exception:                                        # noqa
            pass
        n_dec = sum(1 for r in self.review_rows if r["decision"])
        excl = rv.excluded_ids(work) if work and os.path.isdir(work) else []
        self.review_label.setText("{} dam(s) to look at, {} decided; {} excluded in total.".format(
            len(self.review_rows), n_dec, len(excl)))
        has = bool(self.review_rows)
        for b in (self.zoom_btn, self.accept_btn, self.exclude_btn, self.set_type_btn, self.move_btn, self.note_btn):
            b.setEnabled(has)

    def _selected_review(self):
        i = self.review_table.currentRow()
        if i is None or i < 0 or i >= len(self.review_rows):
            try:
                item = self.review_table.item(i, 0)
            except Exception:                                    # noqa
                item = None
            if item is None:
                return None
        try:
            item = self.review_table.item(i, 0)
            did = item.text() if item is not None else None
        except Exception:                                        # noqa
            did = None
        for r in self.review_rows:
            if did and r["dam_id"] == did:
                return r
        return self.review_rows[i] if 0 <= i < len(self.review_rows) else None

    def _decide(self, action, value="", note=""):
        r = self._selected_review()
        if r is None:
            QMessageBox.information(self, TITLE, "Select a dam in the list first.")
            return None
        work = self.work_edit.text().strip()
        reviewer = self.reviewer_edit.text().strip()
        if not reviewer:
            reviewer, ok = QInputDialog.getText(self, TITLE, "Your name or initials (kept in Settings > Run options):")
            if not ok or not reviewer.strip():
                return None
            self.reviewer_edit.setText(reviewer.strip())
            self._save_state()
        try:
            row, done = rv.decide(work, r["dam_id"], action, value, reviewer, note)
        except (ValueError, OSError) as e:
            QMessageBox.warning(self, TITLE, str(e))
            return None
        changed = [(os.path.basename(p), n) for p, n in done]
        self._log("Review: {} {} {} -> {}".format(r["dam_id"], action, value or "", "; ".join(
            "{} ({})".format(p, n) for p, n in changed) or "no layer written"))
        if any(isinstance(n, str) for _p, n in done):
            QMessageBox.information(self, TITLE, "Recorded in review_decisions.csv, but not every layer could be "
                                                 "written:\n" + "\n".join("{}: {}".format(p, n) for p, n in changed
                                                                         if isinstance(n, str)))
        self.refresh_review()
        return row

    def _move_breach(self):
        r = self._selected_review()
        if r is None:
            QMessageBox.information(self, TITLE, "Select a dam in the list first.")
            return None
        start = ""
        try:
            c = self.iface.mapCanvas().center() if self.iface is not None else None
            if c is not None:
                # 0.17.0: the canvas centre is in the canvas CRS; the box asks for EPSG:5070
                from qgis.core import QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsProject
                tr = QgsCoordinateTransform(self.iface.mapCanvas().mapSettings().destinationCrs(),
                                            QgsCoordinateReferenceSystem("EPSG:5070"), QgsProject.instance())
                c = tr.transform(c)
                start = "{:.1f}, {:.1f}".format(c.x(), c.y())
        except Exception:                                        # noqa
            start = ""
        text, ok = QInputDialog.getText(self, TITLE, "New breach point for {} as 'x, y' in EPSG:5070 metres:".format(
            r["dam_id"]), text=start) if start else QInputDialog.getText(
            self, TITLE, "New breach point for {} as 'x, y' in EPSG:5070 metres:".format(r["dam_id"]))
        if not ok or not str(text).strip():
            return None
        return self._decide("move_breach", str(text).strip())

    def _add_note(self):
        r = self._selected_review()
        if r is None:
            QMessageBox.information(self, TITLE, "Select a dam in the list first.")
            return None
        text, ok = QInputDialog.getText(self, TITLE, "Note for {}:".format(r["dam_id"]))
        if not ok or not str(text).strip():
            return None
        return self._decide("note", "", str(text).strip())

    def _zoom_to_selected(self):
        r = self._selected_review()
        if r is None or self.iface is None:
            return False
        work = self.work_edit.text().strip()
        ref = wf.out(work, "18", "OUTP") or wf.out(work, "4", "OUTP") or wf.out(work, "16", "OUTP")
        lyr = self._read_layer(ref) if ref else None
        if lyr is None:
            return False
        try:
            from qgis.core import (QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsProject,
                                   QgsRectangle)
            names = lyr.fields().names()
            for f in lyr.getFeatures():
                if "dam_id" in names and str(f["dam_id"]) == r["dam_id"]:
                    g = f.geometry()
                    # 0.17.0: +-400 m around the dam in EPSG:5070, then into the canvas CRS (400
                    # canvas units were 400 degrees on a lon/lat canvas, 400 ft on a State Plane one)
                    alb = QgsCoordinateReferenceSystem("EPSG:5070")
                    g.transform(QgsCoordinateTransform(lyr.crs(), alb, QgsProject.instance()))
                    p = g.centroid().asPoint()
                    half = 400.0
                    tr = QgsCoordinateTransform(alb, self.iface.mapCanvas().mapSettings().destinationCrs(),
                                                QgsProject.instance())
                    box = tr.transformBoundingBox(QgsRectangle(p.x() - half, p.y() - half,
                                                               p.x() + half, p.y() + half))
                    self.iface.mapCanvas().setExtent(box)
                    self.iface.mapCanvas().refresh()
                    return True
        except Exception as e:                                   # noqa
            self._log("zoom: {}".format(e))
        return False

    # ----------------------------------------------------------------- results --
    def _open_work_folder(self):
        work = self.work_edit.text().strip()
        if work and os.path.isdir(work):
            QDesktopServices.openUrl(QUrl.fromLocalFile(work))

    def _open_report(self):
        p = os.path.join(self.work_edit.text().strip(), wf.REPORT_INDEX)
        if os.path.isfile(p):
            QDesktopServices.openUrl(QUrl.fromLocalFile(p))

    def _add_results(self, work=None, title=None):
        """Every result layer of the work folder, styled, in one layer group (0.13); the plain
        add when the project has no layer tree here."""
        work = work or self.work_edit.text().strip()
        added = 0
        try:
            from qgis.core import QgsProject, QgsVectorLayer
        except Exception as e:                                   # noqa
            self._log("!!! layers cannot be added here: {}".format(e))
            return 0
        try:
            from . import suite_styles
        except Exception:                                        # noqa
            suite_styles = None
        group = None
        try:
            root = QgsProject.instance().layerTreeRoot()
            name = title or "ND Dam Screening - {}".format(os.path.basename(os.path.normpath(work)) or work)
            group = root.findGroup(name) or root.insertGroup(0, name)
        except Exception:                                        # noqa
            group = None
        for title_l, p in wf.result_layers(work):
            try:
                layer = QgsVectorLayer(p, title_l, "ogr")
                if layer.isValid():
                    if suite_styles is not None:
                        suite_styles.apply(layer, suite_styles.style_for_path(p), log=self._log)
                    if group is not None:
                        QgsProject.instance().addMapLayer(layer, False)
                        group.addLayer(layer)
                    else:
                        QgsProject.instance().addMapLayer(layer)
                    added += 1
                else:
                    self._log("!!! {} could not be read as a layer".format(p))
            except Exception as e:                               # noqa
                self._log("!!! {}: {}".format(p, e))
        self._log("Added {} result layer(s) to the project{}".format(added, " in one group" if group is not None else ""))
        self._fill_layers()
        return added

    # item 2+7: inline DEM format facts + rough time estimate
    def _update_dem_info(self):
        try:
            src = self.dem_source.currentIndex()   # 0=3DEP, 1=DWR
            res = self.dem_resolution.currentIndex()  # 0=10 m, 1=1 m
            if src == 0:
                if res == 0:
                    txt = "Output: EPSG:5070 float32 GeoTIFF, 10 m, metres.  ~12 s/dam."
                else:
                    txt = "Output: EPSG:5070 float32 GeoTIFF, 1 m, metres.  ~90 s/dam (large file)."
            else:
                if res == 0:
                    txt = "Output: EPSG:5070 float32 GeoTIFF, 10 m, metres.  QL2 .img warped from feet.  ~30 s/dam."
                else:
                    txt = "Output: EPSG:5070 float32 GeoTIFF, 1 m, metres.  QL2 .img warped from feet.  ~60 s/dam."
            self.dem_info_label.setText(txt)
        except Exception:                                        # noqa
            pass

    # item 3: tick the running step's elapsed label every second
    def _tick(self):
        try:
            import time as _time
            if self.chain is None or not self.chain.active:
                return
            step = getattr(self.chain, "_current_step", None)
            t0 = getattr(self.chain, "_step_t0", None)
            if step is None or t0 is None:
                return
            elapsed = int(_time.time() - t0)
            if step in self.simple_note:
                prev = self.simple_note[step].text()
                # don't overwrite a meaningful note with just timing
                if not prev or prev.startswith("running") or prev.startswith("…"):
                    self.simple_note[step].setText("… {}s".format(elapsed))
        except Exception:                                        # noqa
            pass

    # item 5+6: open Runner pre-filled with this work folder as dams folder
    def _selected_dam_id(self, fallback=False):
        """0.26.0: the dam ID of the row selected in the Results tab's dam table; with `fallback`
        and no selection, the one-dam run shown in the one-dam list (the dam just screened); else ""."""
        try:
            i = self.dam_table.currentRow()
            if i is not None and i >= 0:
                item = self.dam_table.item(i, 0)
                if item is not None and str(item.text() or "").strip():
                    return str(item.text()).strip()
        except Exception:                                        # noqa
            pass
        if not fallback:
            return ""
        try:
            if self.one_dam_combo.currentData():
                text = str(self.one_dam_combo.currentText() or "").strip()
                if text and "_" not in text:        # one dam; a chosen-dams run joins its IDs with "_"
                    return text
        except Exception:                                        # noqa
            pass
        return ""

    @staticmethod
    def _point_window(dlg, key, dams_folder, row):
        """0.26.0: suite_home.point_window_at - the suite home's way of pointing another plugin's
        window at a dam (duck typing on its fields). What it set; [] when it could not."""
        try:
            from . import suite_home
            return suite_home.point_window_at(dlg, key, dams_folder, row)
        except Exception:                                        # noqa
            return []

    def _open_runner(self):
        try:
            from . import suite_home
            plug, started = suite_home.load_plugin_object("nd_breach_runner")
            if plug is None or not hasattr(plug, "run_plugin"):
                raise KeyError("ND Dam Breach Runner is not installed, or it could not be started")
            if started:
                self._log(">>> ND Dam Breach Runner was installed but not loaded: it has been started")
            work = self.work_edit.text().strip()
            dlg = plug.run_plugin()
            dam_id = self._selected_dam_id(fallback=True)
            if work and dam_id:
                # 0.26.0: the dam selected in the Results table: the Dams folder the chain hands
                # dams over to and the dam's folder in it
                root, row = wf.dam_row_for(work, self.settings(), dam_id)
                self._point_window(dlg if dlg is not None else getattr(plug, "dialog", None),
                                   "ndbreach", root, row)
            elif dlg is not None and work:
                try:                                             # 0.26.0: the Dams folder when it exists
                    root = wf.dams_folder(work, self.settings())
                    dlg.dams_edit.setText(root if os.path.isdir(root) else work)
                    dlg.refresh()
                except Exception:                                # noqa
                    pass
        except Exception as e:                                   # noqa
            QMessageBox.information(self, TITLE, "ND Dam Breach Runner could not be opened ({}).".format(e))

    # item 8: auto-preflight (debounced, skips if chain active)
    def _auto_preflight(self):
        try:
            if self.chain is not None and self.chain.active:
                return
            work = self.work_edit.text().strip()
            if not work or not os.path.isdir(work):
                return
            self._preflight(steps=None, settings=self.settings())
        except Exception:                                        # noqa
            pass

    # item 9: populate dam table from step 8 GeoPackage
    def _refresh_dam_table(self, work):
        """The Results tab's dam table: step 8's dams of the work folder and of every one-dam /
        chosen-dams run under one_dam/ (0.26.0: it read a *class*.gpkg that no step writes, so it
        stayed empty). Each row keeps its run folder (Qt.UserRole of the Dam ID cell)."""
        try:
            from qgis.PyQt.QtCore import Qt
            from qgis.core import QgsVectorLayer
            runs = [("", work)] + list(wf.one_dam_runs(work))
            rows = []
            for label, folder in runs:
                gpkg = wf.out(folder, "8")
                if not gpkg or not os.path.isfile(gpkg):
                    continue
                lyr = QgsVectorLayer(gpkg, "tmp_class", "ogr")
                if not lyr.isValid():
                    continue
                names = lyr.fields().names()

                def col(*cands):
                    return next((f for f in cands if f in names), None)
                c_id, c_name = col("dam_id", "id"), col("name", "dam_name")
                c_cls, c_stor, c_conf = col("hz_class"), col("store_af", "stor_used_acft"), col("hz_conf")
                for feat in lyr.getFeatures():
                    def val(c, fmt=None):
                        if not c:
                            return ""
                        v = feat[c]
                        if v is None or (hasattr(v, "isNull") and v.isNull()):
                            return ""
                        if fmt and isinstance(v, (int, float)):
                            return fmt.format(v)
                        return str(v)
                    # 0.28.0: the run flags of this dam (truncated flood, no model run ...)
                    row = dict((f, val(col(f))) for f in ic.READ_FIELDS if col(f))
                    rows.append((val(c_id), val(c_name), val(c_cls), val(c_stor, "{:,.0f}"),
                                 val(c_conf), ic.short(row), folder, label, ic.text(row)))
            keep = ""                                            # the selection survives a refresh
            try:
                cur = self.dam_table.currentRow()
                if cur is not None and cur >= 0 and self.dam_table.item(cur, 0) is not None:
                    keep = self.dam_table.item(cur, 0).text()
            except Exception:                                    # noqa
                pass
            self.dam_table.setSortingEnabled(False)
            self.dam_table.setRowCount(0)
            for r in rows:
                n = self.dam_table.rowCount()
                self.dam_table.insertRow(n)
                for c, v in enumerate(r[:len(DAM_COLUMNS)]):
                    item = QTableWidgetItem(v)
                    if c == 0:
                        item.setData(Qt.UserRole, r[6])
                        item.setToolTip("one-dam run {}".format(r[7]) if r[7] else "the work folder's screening")
                    if c == 5 and v:                             # 0.28.0: the flags, in red
                        item.setToolTip(r[8])
                        try:
                            from qgis.PyQt.QtGui import QBrush, QColor
                            item.setForeground(QBrush(QColor(176, 0, 32)))
                        except Exception:                        # noqa - colour is not the point
                            pass
                    self.dam_table.setItem(n, c, item)
            self.dam_table.setSortingEnabled(True)
            try:
                flagged = [r for r in rows if r[5]]
                self.dam_flags_label.setText(
                    "{} of {} dam(s) incomplete - their class is a FLOOR, not an answer. Hover a "
                    "red cell for the reason.".format(len(flagged), len(rows)) if flagged else "")
            except Exception:                                    # noqa
                pass
            if keep:
                for n in range(self.dam_table.rowCount()):
                    if self.dam_table.item(n, 0) is not None and self.dam_table.item(n, 0).text() == keep:
                        self.dam_table.selectRow(n)
                        self.dam_table.setCurrentCell(n, 0)
                        break
        except Exception:                                        # noqa
            pass

    # item 11: progress bar update called from _Feedback.setProgress
    def _update_progress(self, step, pct):
        try:
            self.step_progress.setVisible(True)
            self.step_progress.setFormat("Step {} – %p%%".format(step))
            self.step_progress.setValue(int(pct))
        except Exception:                                        # noqa
            pass

    def _continue_button(self):
        b = QPushButton("Continue")
        b.setEnabled(False)
        b.clicked.connect(lambda _c=False: self._continue())
        return b

    def _continue_state(self, work=None):
        """(plugin, label, why) for the selected dam (wf.continue_target)."""
        work = self.work_edit.text().strip() if work is None else work
        try:
            return wf.continue_target(work, self.settings(), self._selected_dam_id(fallback=True))
        except Exception as e:                                   # noqa - never breaks refresh
            return "", "Continue", "Continue is not available ({}).".format(e)

    def _refresh_continue(self, work, busy):
        plugin, label, why = self._continue_state(work)
        for b in (getattr(self, "continue_btn", None), getattr(self, "results_continue_btn", None)):
            if b is None:
                continue
            b.setText(label)
            b.setEnabled(bool(plugin) and not busy)
            b.setToolTip(why)

    def _continue_follow(self, *args):
        busy = self.chain is not None and self.chain.active
        self._refresh_continue(self.work_edit.text().strip(), busy)

    def _continue(self):
        """0.28.1: open the plugin the selected dam needs next, pointed at that dam."""
        plugin, _label, why = self._continue_state()
        if plugin == "ndbreach":
            self._open_runner()
        elif plugin == "ndhazard":
            self._open_hazard()
        else:
            QMessageBox.information(self, TITLE, why)

    def _open_hazard(self):
        try:
            from . import suite_home
            plug, started = suite_home.load_plugin_object("ND_Dam_Hazard_Helper")
            if plug is None or not hasattr(plug, "run_plugin"):
                raise KeyError("ND Dam Hazard is not installed, or it could not be started")
            if started:
                self._log(">>> ND Dam Hazard was installed but not loaded: it has been started")
            dlg = plug.run_plugin()
            work = self.work_edit.text().strip()
            dam_id = self._selected_dam_id(fallback=True)
            if work and dam_id:                                  # 0.26.0: the selected dam's folder
                root, row = wf.dam_row_for(work, self.settings(), dam_id)
                self._point_window(dlg if dlg is not None else getattr(plug, "dialog", None),
                                   "ndhazard", root, row)
        except Exception as e:                                   # noqa
            QMessageBox.information(self, TITLE, "ND Dam Hazard could not be opened ({}).".format(e))

    def show_settings(self):
        """Open the window on its Settings tab (the suite menu's 'Suite settings...')."""
        try:
            self.tabs.setCurrentIndex(3)
        except Exception:                                        # noqa
            pass

    def closeEvent(self, event):
        self._save_state()
        try:
            event.accept()
        except Exception:                                        # noqa
            pass
