# -*- coding: utf-8 -*-
"""Dialog UI and orchestration for the ND Dam Hazard Helper."""

import os
import html
import time
import traceback

from qgis.PyQt.QtWidgets import (
    QApplication, QProgressDialog, QDialog, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QLineEdit, QFileDialog, QMessageBox, QGroupBox,
    QFormLayout, QTabWidget, QTextEdit, QWidget, QComboBox, QScrollArea, QCheckBox,
)

from qgis.core import QgsProject, QgsRasterLayer

from .compat import MSG_INFO, MSG_WARNING, MSG_CRITICAL, WINDOW_MODAL
from .helpers import log_message, CancelledByUser, plugin_group, dam_display_name
from .ui_check_panel import CheckPanel

from .constants import (
    PLUGIN_NAME, PLUGIN_VERSION, OUTPUT_FOLDER_NAME,
    BTN_ACTIVE_STYLE, BTN_INACTIVE_STYLE, README_TEXT, PROGRESS_MIN_INTERVAL_S,
)
from .extent import make_extent_clip
from . import preflight as PRE
from .classify_tab import ClassificationTabMixin
from .extras_tab import ExtrasTabMixin
from .extras290_tab import Extras290TabMixin
from .suite_tab import SuiteTabMixin
from .review_tab import ReviewTabMixin
from .dsswise_tab import DssWiseTabMixin
from .rasimport_tab import RasImportTabMixin
from .data_tab import DataTabMixin
from .datafolder_tab import DataFolderTabMixin
from .facility_review_tab import FacilityReviewTabMixin
from .suite_settings_tab import SuiteSettingsTabMixin
from . import facility_review_tab as FT
from .settings import save_state, load_state, widget_value, set_widget_value

def _scroll_tab(w):
    """Wrap a tab widget in a QScrollArea so tall pages are reachable on small screens."""
    sa = QScrollArea()
    sa.setWidgetResizable(True)
    sa.setWidget(w)
    return sa


def _escape_tab_mnemonics(widget):
    """1.18: a single '&' in a tab title is Qt's keyboard-shortcut marker and is not shown
    ('Setup & Run' read 'Setup Run'); '&&' shows one '&'. Applied once the tabs are built."""
    try:
        from qgis.PyQt.QtWidgets import QTabWidget
        for tw in widget.findChildren(QTabWidget):
            for i in range(tw.count()):
                t = tw.tabText(i)
                if "&" in t and "&&" not in t:
                    tw.setTabText(i, t.replace("&", "&&"))
    except Exception:                                            # noqa
        pass


def _same_folder(a, b):
    """True when two folder paths name the same folder (False when one is empty)."""
    if not a or not b:
        return False
    try:
        return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))
    except Exception:                                            # noqa
        return a == b


class NDDamHazardDialog(QDialog, DataTabMixin, DataFolderTabMixin, ClassificationTabMixin,
                        ExtrasTabMixin, Extras290TabMixin, SuiteTabMixin, ReviewTabMixin, SuiteSettingsTabMixin,
                        FacilityReviewTabMixin,
                        DssWiseTabMixin, RasImportTabMixin):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.config_dam_folder = ""
        self.config_output_folder = ""
        self._rasters = {}
        self._progress = None
        self._last_tick = 0.0
        self._last_pct = 0.0
        self._busy = False
        self._last_run = None
        self._chain = None              # (lo, hi) progress range of a nested sync step
        self._task = None               # v2.6: the background task while it runs
        self._finishing = False         # main-thread part of a background run
        self._project_gen = 0           # bumped when another project is opened
        self._restoring = False         # settings being restored: no raster search
        # 2.17.0: what the plugin (not the engineer) put in the raster boxes, and the dam
        # folder those values and the NID ID box belong to - cleared on a dam change
        self._auto_rasters = {}
        self._raster_dam = None
        self._nid_dam = None
        self.setup_ui()
        _escape_tab_mnemonics(self)
        self.dam_folder_edit.textChanged.connect(self._on_dam_folder_changed)
        for e in (self.depth_edit, self.vel_edit, self.arr_edit, self.dv_edit):
            e.textChanged.connect(self._update_button_states)
            e.textChanged.connect(self.check_panel.clear)   # old check results are stale
        try:
            self.model_combo.currentIndexChanged.connect(self._update_button_states)
            self.model_combo.currentIndexChanged.connect(self.check_panel.clear)
        except Exception:
            pass
        self._defaults = self._current_state()
        self._restore_settings()
        self._update_button_states()
        # the dialog outlives a Project > Open: re-read that project's settings
        try:
            QgsProject.instance().readProject.connect(self._on_project_changed)
            QgsProject.instance().cleared.connect(self._on_project_changed)
        except Exception:
            pass
        try:
            QgsProject.instance().writeProject.connect(self._on_write_project)
        except Exception:
            pass

    # ------------------------------------------------------------------ UI --
    def setup_ui(self):
        self.setWindowTitle("{} v{}".format(PLUGIN_NAME, PLUGIN_VERSION))
        self.setMinimumSize(900, 700)
        try:
            from qgis.PyQt.QtWidgets import QApplication
            self.setMaximumHeight(max(700, QApplication.primaryScreen().geometry().height() - 80))
        except Exception:
            pass
        main_layout = QVBoxLayout(self)
        self.tab_widget = QTabWidget()

        # --- 2.30.0 tab order: the window opens on "Setup & Run", the step a novice has to
        # do first; the two importers (DSS-WISE Lite, HEC-RAS plan) are tools for a model that
        # has already been run, so they come after Review. Before 2.30.0 the window opened on
        # the DSS-WISE Lite importer and "Setup & Run" was the third tab. ---

        # --- Setup tab ---
        setup_tab = QWidget()
        setup_layout = QVBoxLayout(setup_tab)

        # 2.30.0: the hand-off banner (hidden until another suite window points this window
        # at a dam): the rasters are found automatically and the whole run is one click here,
        # so Screening -> Runner -> Hazard is three clicks and not a hunt for a button.
        self.handoff_group = QGroupBox("Handed over from another window of the suite")
        handoff_layout = QHBoxLayout()
        self.handoff_label = QLabel("")
        self.handoff_label.setWordWrap(True)
        handoff_layout.addWidget(self.handoff_label, stretch=3)
        self.handoff_run_btn = QPushButton("Run everything now")
        self.handoff_run_btn.setToolTip("Runs the whole classification for the dam just handed "
                                        "over - the same as 'Run everything' on Data & Output.")
        self.handoff_run_btn.clicked.connect(self._on_handoff_run)
        handoff_layout.addWidget(self.handoff_run_btn)
        self.handoff_auto_check = QCheckBox("Run automatically next time")
        self.handoff_auto_check.setToolTip(
            "On a hand-off from the ND Dam Breach Runner or the suite home, find the rasters "
            "and start 'Run everything' without being asked. The run still stops with its "
            "reasons when an input is missing.")
        handoff_layout.addWidget(self.handoff_auto_check)
        self.handoff_group.setLayout(handoff_layout)
        self.handoff_group.setVisible(False)
        setup_layout.addWidget(self.handoff_group)

        dam_group = QGroupBox("1. Dam Project Folder")
        dam_layout = QHBoxLayout()
        self.dam_folder_edit = QLineEdit()
        self.dam_folder_edit.setPlaceholderText("Browse or paste the dam project folder")
        self.dam_browse_btn = QPushButton("Browse Dam Folder...")
        self.dam_browse_btn.clicked.connect(self.browse_dam_folder)
        self.find_rasters_btn = QPushButton("Find rasters")
        self.find_rasters_btn.setToolTip(
            "Look through the dam folder for HEC-RAS (RAS Mapper) / DSS-WISE exports: "
            "Depth (Max), Velocity (Max), Arrival Time and D * V (Max). Done automatically "
            "when the raster boxes are empty.")
        self.find_rasters_btn.clicked.connect(lambda *a: self._auto_fill_rasters(force=True))
        dam_layout.addWidget(self.dam_folder_edit, stretch=3)
        dam_layout.addWidget(self.dam_browse_btn)
        dam_layout.addWidget(self.find_rasters_btn)
        dam_group.setLayout(dam_layout)
        setup_layout.addWidget(dam_group)

        raster_group = QGroupBox("2. Raster Files (Required)")
        raster_layout = QFormLayout()
        self.depth_edit = QLineEdit()
        self.depth_browse_btn = QPushButton("Browse...")
        self.depth_browse_btn.clicked.connect(self.browse_depth)
        raster_layout.addRow("Depth (Hmax):",
                             self._inline_row(self.depth_edit, self.depth_browse_btn))
        self.vel_edit = QLineEdit()
        self.vel_browse_btn = QPushButton("Browse...")
        self.vel_browse_btn.clicked.connect(self.browse_velocity)
        raster_layout.addRow("Velocity (Vmax):",
                             self._inline_row(self.vel_edit, self.vel_browse_btn))
        self.arr_edit = QLineEdit()
        self.arr_browse_btn = QPushButton("Browse...")
        self.arr_browse_btn.clicked.connect(self.browse_arrival)
        raster_layout.addRow("Arrival Time:",
                             self._inline_row(self.arr_edit, self.arr_browse_btn))
        self.dv_edit = QLineEdit()
        self.dv_edit.setPlaceholderText("optional - RAS Mapper 'Depth x Velocity' (Max), "
                                        "DSS-WISE DV; flood severity for the loss-of-life "
                                        "estimate (ACER zones use Dmax / Vmax)")
        self.dv_browse_btn = QPushButton("Browse...")
        self.dv_browse_btn.clicked.connect(
            lambda: self._browse_raster(self.dv_edit, "Select Depth x Velocity (max) raster"))
        raster_layout.addRow("Depth x Velocity (ft\u00b2/s):",
                             self._inline_row(self.dv_edit, self.dv_browse_btn))
        # the breach model (was in the classification tab's Hydraulics group)
        self.model_combo = QComboBox()
        self.model_combo.addItem("2D model - ACER depth-velocity curves, Dmax / Vmax "
                                 "(ND Figs 5.1/5.2)", "2d")
        self.model_combo.addItem("1D model - habitable structure depth > 2 ft", "1d")
        self.model_combo.setToolTip("Set automatically by the DSS-WISE and HEC-RAS imports.")
        raster_layout.addRow("Breach model:", self.model_combo)
        raster_group.setLayout(raster_layout)
        setup_layout.addWidget(raster_group)

        btn_layout = QHBoxLayout()
        self.check_btn = QPushButton("Check inputs")
        self.check_btn.setToolTip("Checks the rasters (units, water-surface raster given as "
                                  "depth, arrival in minutes, missing velocity, coordinate "
                                  "systems), the outline file and the download services.")
        self.check_btn.clicked.connect(self._on_check_inputs)
        self.classify_btn = QPushButton("Classify")
        self.classify_btn.setMinimumHeight(40)
        self.classify_btn.setStyleSheet("font-size: 14px; font-weight: bold;")
        self.classify_btn.setToolTip("Finds the depth, velocity, and arrival rasters, "
                                     "checks them, and classifies.")
        self.classify_btn.clicked.connect(self._classify_simple)
        btn_layout.addWidget(self.check_btn)
        btn_layout.addWidget(self.classify_btn)
        btn_layout.addStretch()
        setup_layout.addLayout(btn_layout)
        # check results by severity, under the button row (ui_check_panel.py)
        self.check_panel = CheckPanel(self.check_btn, self.classify_btn, action="classifying")
        setup_layout.addWidget(self.check_panel)
        setup_layout.addStretch()
        self.tab_widget.addTab(_scroll_tab(setup_tab), "Setup & Run")
        self._setup_tab_index = self.tab_widget.count() - 1

        # --- Data & Output tab (data_tab.py); the "Run everything" button is here (v2.9) ---
        self.tab_widget.addTab(_scroll_tab(self._build_data_tab()), "Data & Output")

        # --- Hazard classification tab (classify_tab.py) ---
        self.tab_widget.addTab(_scroll_tab(self._build_classification_tab()), "Hazard Classification")
        self.tab_widget.currentChanged.connect(self._on_tab_changed)

        # --- Review tabs: structures, then roads / rail / bridges / lifelines ---
        self.tab_widget.addTab(_scroll_tab(self._build_review_tab()), "Review")
        self.tab_widget.addTab(_scroll_tab(self._build_facility_review_tab()), FT.TAB_TITLE)

        # --- the importers: a model that has already been run (2.30.0: after Review) ---
        self.tab_widget.addTab(_scroll_tab(self._build_dsswise_tab()), "DSS-WISE Lite")
        self.tab_widget.addTab(_scroll_tab(self._build_ras_tab()), "HEC-RAS plan")

        # --- v2.13: the suite settings file as a grid (suite_settings_tab.py) ---
        try:
            self.tab_widget.addTab(_scroll_tab(self._build_suite_settings_tab()), "Suite settings")
        except Exception as e:                                   # noqa - the grid must never stop the window
            log_message("Suite settings tab not built: {}".format(e))

        # --- Readme tab ---
        readme_tab = QWidget()
        readme_layout = QVBoxLayout(readme_tab)
        readme_text = QTextEdit()
        readme_text.setReadOnly(True)
        readme_text.setPlainText(README_TEXT)
        readme_text.setStyleSheet(
            "font-family: 'Menlo', 'Courier New', monospace; font-size: 12px;")
        readme_layout.addWidget(readme_text)
        selftest_row = QHBoxLayout()
        self.selftest_btn = QPushButton("Run Self-Test")
        self.selftest_btn.setToolTip("Runs the full pipeline on synthetic data in a "
                                     "temporary folder. Your project is not changed.")
        self.selftest_btn.clicked.connect(self._on_run_selftest)
        selftest_row.addWidget(self.selftest_btn)
        self.sources_btn = QPushButton("Edit data sources...")
        self.sources_btn.setToolTip("Opens the file with every download address (NSI, ND GIS "
                                    "Hub, NID, HIFLD, NBI ...). Change an address there when "
                                    "a service moves, then restart QGIS.")
        self.sources_btn.clicked.connect(self._on_edit_sources)
        selftest_row.addWidget(self.sources_btn)
        selftest_row.addStretch()
        readme_layout.addLayout(selftest_row)
        self.tab_widget.addTab(_scroll_tab(readme_tab), "Readme")

        main_layout.addWidget(self.tab_widget)

        self.status_label = QLabel("Ready")
        self.status_label.setStyleSheet("padding: 8px; background-color: #f5f5f5;")
        main_layout.addWidget(self.status_label)

        self.log_text = QTextEdit()
        self.log_text.setMaximumHeight(150)
        self.log_text.setReadOnly(True)
        main_layout.addWidget(self.log_text)

    def _on_edit_sources(self, *args):
        from .constants import write_sources_file, SOURCE_OVERRIDES
        from qgis.PyQt.QtCore import QUrl
        from qgis.PyQt.QtGui import QDesktopServices
        try:
            path = write_sources_file()
        except OSError as e:
            QMessageBox.warning(self, "Data sources", "Could not write the file: {}".format(e))
            return
        self._log(">>> Data-source file: {}{}".format(
            path, " (in use: {} change(s))".format(len(SOURCE_OVERRIDES))
            if SOURCE_OVERRIDES else ""))
        QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def _on_tab_changed(self, index):
        if self.tab_widget.tabText(index).replace("&&", "&") in ("Hazard Classification", "Data & Output"):
            self._refresh_layer_combos()

    def _inline_row(self, edit, btn):
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        h.addWidget(edit, stretch=3)
        h.addWidget(btn)
        return w

    # ------------------------------------------------------------ inputs ----
    def _raster_paths(self):
        return {"depth": self.depth_edit.text().strip(),
                "vel": self.vel_edit.text().strip(),
                "arr": self.arr_edit.text().strip()}

    def _dv_path(self):
        p = self.dv_edit.text().strip()
        return p if p and os.path.isfile(p) else None

    def _one_d_selected(self):
        combo = getattr(self, "model_combo", None)
        return combo is not None and combo.currentData() == "1d"

    def _required_rasters(self):
        """Depth is always needed; velocity too (ACER zones from Dmax / Vmax)
        except for the 1D (depth-only) criterion. Arrival is optional
        (arrival fields and warning times are then left empty)."""
        keys = ["depth"]
        if not self._one_d_selected():
            keys.append("vel")
        return keys

    def _inputs_valid_for_main(self):
        paths = self._raster_paths()
        return (bool(self.config_dam_folder) and os.path.isdir(self.config_dam_folder)
                and all(paths[k] and os.path.isfile(paths[k]) for k in self._required_rasters())
                and (not paths["vel"] or os.path.isfile(paths["vel"])))

    # ------------------------------------------- 2.30.0: why a Run button is grey --
    def _have_inputs(self):
        """What preflight.py needs to know about this window (plain booleans)."""
        paths = self._raster_paths()

        def ok(key):
            p = paths.get(key) or ""
            return bool(p and os.path.isfile(p))
        return {"dam_folder": bool(self.config_dam_folder
                                   and os.path.isdir(self.config_dam_folder)),
                "depth": ok("depth"), "vel": ok("vel"), "arr": ok("arr"),
                "one_d": self._one_d_selected(), "busy": bool(self._busy)}

    def _suite_blocking(self, max_age_s=10.0):
        """damsuite.suitecheck's blocking items, at most every max_age_s seconds (the check
        reads folders). Never raises: an unreadable suite folder gives no reason here, since
        the window's own inputs already name what is missing."""
        now = time.time()
        got = getattr(self, "_blocking_cache", None)
        if got is not None and now - got[0] < max_age_s:
            return got[1]
        items = []
        try:
            from .damsuite import suitecheck as SC
            items = SC.blocking(SC.check(plugins=False))
        except Exception as e:                                   # noqa - never stop the window
            log_message("Suite readiness not checked: {}".format(e))
            items = []
        self._blocking_cache = (now, items)
        return items

    def _run_reasons(self):
        """[(key, label, what to do)] - empty when a run can start."""
        try:
            have = self._have_inputs()
            blocking = [] if have["busy"] else self._suite_blocking()
            return PRE.reasons(have, blocking)
        except Exception as e:                                   # noqa - never stop the window
            log_message("Run reasons not built: {}".format(e))
            return []

    def _update_button_states(self, *args):
        """Both run buttons, their tooltips and the status line say the same thing: what is
        missing and what to do about it (before 2.30.0 one button had a 'Missing: ...'
        tooltip, the other none, and the status line said nothing)."""
        busy = self._busy
        reasons = self._run_reasons()
        ready = not reasons and self._inputs_valid_for_main() and not busy
        tip = PRE.tooltip(reasons) if not ready else (
            "Every input is ready - runs the data download (when ticked), the inventory, the "
            "ACER zones, the ND criteria, the report and the EAP outputs")
        for name in ("run_all_btn", "run_class_btn"):
            btn = getattr(self, name, None)
            if btn is None:
                continue
            btn.setEnabled(ready)
            btn.setStyleSheet(BTN_ACTIVE_STYLE if ready else BTN_INACTIVE_STYLE)
            btn.setToolTip(tip)
        if not busy and not getattr(self, "_last_run", None):
            # the status line is the window's one message: it names the missing input until
            # a run has produced a result of its own to show
            self._update_status(PRE.status_text(reasons), bool(ready))
        if hasattr(self, "result_label") and not getattr(self, "_last_run", None):
            self.result_label.setText(
                "No classification run yet - missing: {}".format(PRE.names(reasons))
                if reasons else "No classification run yet - every input is ready, press Run")
        self._classification_button_state()

    def _log_run_reasons(self):
        """The reasons in the log and a message box (a Run pressed from a hand-off)."""
        reasons = self._run_reasons()
        for line in PRE.log_lines(reasons):
            self._log(">>> " + line)
        return reasons

    # ------------------------------------------------- 2.30.0: the suite hand-off --
    def handoff(self, folder="", source="", run=None):
        """Another suite window has pointed this window at a dam (the Runner's hand-off, the
        suite home's "Classify"). Finds the rasters, shows the hand-off banner with "Run
        everything now", and starts the run itself when the engineer ticked that box
        (`run` True / False overrides the tick). Returns True when a run was started.

        Never raises: it is called from another plugin's code."""
        started = False
        try:
            folder = (folder or self.config_dam_folder or "").strip()
            if folder and not _same_folder(folder, self.config_dam_folder):
                self.dam_folder_edit.setText(folder)     # -> _on_dam_folder_changed
            n = self._auto_fill_rasters(force=True)       # "Find rasters", without being asked
            self._show_setup_tab()
            reasons = self._run_reasons()
            where = source or "another window of the suite"
            text = "Dam folder set from {}; {} raster box(es) filled.".format(where, n)
            if reasons:
                text += " " + PRE.status_text(reasons)
            else:
                text += " Everything is ready - press 'Run everything now'."
            self.handoff_label.setText(text)
            self.handoff_group.setVisible(True)
            self.handoff_run_btn.setEnabled(not reasons and not self._busy)
            self._log(">>> Hand-off from {}: {}".format(where, text))
            want = self.handoff_auto_check.isChecked() if run is None else bool(run)
            if want and not reasons and not self._busy:
                started = True
                self._on_handoff_run()
        except Exception as e:                                   # noqa - never stop a hand-off
            log_message("Hand-off not completed: {}".format(e))
        return started

    def refresh(self, *args):
        """The hook the shared suite home calls after it fills this window's dam folder
        (suite_home.point_window_at tries "_changed" then "refresh"): the hand-off above."""
        return self.handoff(source="the suite home")

    def _show_setup_tab(self):
        try:
            idx = getattr(self, "_setup_tab_index", None)
            if idx is None:
                for i in range(self.tab_widget.count()):
                    if self.tab_widget.tabText(i).replace("&&", "&") == "Setup & Run":
                        idx = i
                        break
            if idx is not None:
                self.tab_widget.setCurrentIndex(idx)
        except Exception:                                        # noqa
            pass

    def _on_handoff_run(self, *args):
        """"Run everything now" on the hand-off banner."""
        try:
            if self._busy:
                return
            if self._log_run_reasons():
                QMessageBox.warning(self, "Run everything - inputs need fixing",
                                    PRE.tooltip(self._run_reasons()))
                self._update_button_states()
                return
            self.handoff_group.setVisible(False)
            self._on_run_all()
        except Exception as e:                                   # noqa - never raise in a slot
            log_message("Hand-off run failed to start: {}".format(e))

    # ------------------------------------------------------------ BROWSERS --
    def browse_dam_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Select Dam Project Folder",
                                                  self.config_dam_folder or "")
        if folder:
            self.dam_folder_edit.setText(folder)   # -> _on_dam_folder_changed
        self._update_button_states()

    def _on_dam_folder_changed(self, text):
        folder = (text or "").strip()
        self.config_dam_folder = folder
        self.config_output_folder = os.path.join(folder, OUTPUT_FOLDER_NAME) if folder else ""
        self.output_folder_edit.setText(self.config_output_folder)
        if not self._restoring:
            if os.path.isdir(folder) and not _same_folder(folder, self._raster_dam):
                self._clear_auto_rasters()             # 2.17.0: not the previous dam's
            self._auto_fill_rasters(force=False)
            self._fill_from_dam_record(folder)         # 2.17.0: NID ID from dam.json
            if hasattr(self, "_df_dam_picks"):         # 2.17.0: the dam folder's data/
                try:
                    self._df_dam_picks(folder, log=True)
                except Exception as e:                 # noqa - never stop the window
                    log_message("Dam folder data not read: {}".format(e))
        self._update_button_states()
        self._refresh_dam_record()                     # v2.10: dam.json status line

    def _raster_edits(self):
        return [("depth", self.depth_edit), ("vel", self.vel_edit), ("arr", self.arr_edit),
                ("dv", self.dv_edit)]

    def _clear_auto_rasters(self):
        """2.17.0: empty the raster boxes the plugin filled for the previous dam folder
        (found there, or restored with its project settings) and that nobody changed since;
        a raster the engineer chose is kept. Returns the number cleared."""
        cleared = []
        for key, edit in self._raster_edits():
            text = edit.text().strip()
            if text and text == self._auto_rasters.get(key):
                edit.setText("")
                cleared.append(key)
        self._auto_rasters = {}
        self._raster_dam = None
        if cleared:
            self._log(">>> Rasters of the previous dam folder cleared ({}) - looking in the new "
                      "one".format(", ".join(cleared)))
        return len(cleared)

    def _mark_restored_rasters(self):
        """2.17.0: rasters restored with the project settings that lie in its dam folder
        belong to that dam: a change of dam folder clears them like found ones."""
        folder = self.config_dam_folder
        self._auto_rasters = {}
        self._raster_dam = folder if folder and os.path.isdir(folder) else None
        if self._raster_dam is None:
            return
        root = os.path.normcase(os.path.abspath(folder))
        for key, edit in self._raster_edits():
            text = edit.text().strip()
            try:
                inside = text and os.path.normcase(os.path.abspath(text)).startswith(
                    root.rstrip("\\/") + os.sep)
            except Exception:                                    # noqa
                inside = False
            if inside:
                self._auto_rasters[key] = text

    def _fill_from_dam_record(self, folder, restored=False):
        """2.17.0: the NID ID box belongs to one dam. On a change of dam folder it takes the
        new dam's nid_id from dam.json (empty when it has none) instead of keeping the previous
        dam's; a value typed before the first dam folder of the session is kept when dam.json
        has none. restored: after the project settings, only an empty box is filled."""
        edit = getattr(self, "subject_nid_edit", None)
        if edit is None or not folder or not os.path.isdir(folder):
            return
        if not restored and _same_folder(folder, self._nid_dam):
            return
        first = self._nid_dam is None
        self._nid_dam = folder
        rec = None
        try:
            from .damsuite import dam_record as DR
            rec = DR.load(folder)
        except Exception as e:                                   # noqa - a broken dam.json
            self._log(">>> dam.json not read ({})".format(e))
        nid = str(rec.get("nid_id") or "").strip() if rec is not None else ""
        name = str(rec.get("name") or "").strip() if rec is not None else ""
        old = edit.text().strip()
        if restored:
            if not old and nid:
                edit.setText(nid)
            return
        if nid:
            edit.setText(nid)
        elif old and not first:
            edit.setText("")
        if nid or name:
            self._log(">>> From dam.json: {}{}{}".format(
                "name '{}'".format(name) if name else "", ", " if name and nid else "",
                "NID ID {}".format(nid) if nid else ""))
        elif old and not first:
            self._log(">>> NID ID box cleared (it was the previous dam's; this dam's dam.json "
                      "has none)")

    def _auto_fill_rasters(self, force=False):
        """Fill the raster boxes from the files found in the dam folder: all
        of them with force (the "Find rasters" button), otherwise only when
        every raster box is still empty. Returns the number filled."""
        folder = self.config_dam_folder
        if not folder or not os.path.isdir(folder):
            return 0
        edits = self._raster_edits()
        base = []          # v2.5.1: no no-breach baseline / time steps in the dialog
        if not force and any(e.text().strip() for _k, e in edits):
            return 0
        from .autodetect import find_rasters, found_text
        found = find_rasters(folder)
        n = 0
        if force or not _same_folder(folder, self._raster_dam):
            self._auto_rasters = {}
        self._raster_dam = folder
        for key, edit in edits + base:
            if edit is None or not found.get(key):
                continue
            if force or not edit.text().strip():
                edit.setText(found[key])
                self._auto_rasters[key] = found[key]       # 2.17.0: cleared on a dam change
                n += 1
        self._log(">>> " + found_text(found, folder))
        for note in found["notes"]:
            self._log("    " + note + " - change it on the Setup tab if needed")
        self._update_button_states()
        return n

    def _browse_raster(self, edit, title):
        start = os.path.dirname(edit.text().strip()) or self.config_dam_folder or ""
        path, _ = QFileDialog.getOpenFileName(
            self, title, start,
            "Raster Files (*.tif *.tiff *.TIF *.TIFF *.img *.asc *.vrt *.flt *.bil);;"
            "All Files (*)")
        if path:
            edit.setText(path)
        self._update_button_states()

    def browse_depth(self):
        self._browse_raster(self.depth_edit, "Select Depth Raster")

    def browse_velocity(self):
        self._browse_raster(self.vel_edit, "Select Velocity Raster")

    def browse_arrival(self):
        self._browse_raster(self.arr_edit, "Select Arrival Raster")

    def browse_output(self):
        folder = QFileDialog.getExistingDirectory(self, "Select Output Folder",
                                                  self.config_output_folder or "")
        if folder:
            self.config_output_folder = folder
            self.output_folder_edit.setText(folder)
        self._update_button_states()

    # ------------------------------------------------------------ VALIDATION
    def validate_all_inputs(self):
        errors = []
        if not self.config_dam_folder or not os.path.isdir(self.config_dam_folder):
            errors.append("Dam folder not selected (or folder missing)")
        labels = {"depth": "Depth", "vel": "Velocity", "arr": "Arrival"}
        for key, path in self._raster_paths().items():
            if key in self._required_rasters() and (not path or not os.path.isfile(path)):
                errors.append("{} raster not selected (or file missing)".format(labels[key]))
            elif path and not os.path.isfile(path):
                errors.append("{} raster file missing".format(labels[key]))
        if hasattr(self, "scenario_table"):
            errors.extend(self._scenario_problems())     # v2.7: the scenario table
        if hasattr(self, "sens_check"):
            errors.extend(self._extras_problems())       # v2.8: the extra analyses
            errors.extend(self._extras290_problems())    # v2.9
        return len(errors) == 0, errors

    # ------------------------------------------------------------ STATUS/LOG
    def _update_status(self, message, is_success=True):
        color = "#2e7d32" if is_success else "#c62828"
        self.status_label.setText(message)
        self.status_label.setStyleSheet(
            "padding: 8px; background-color: #f5f5f5; color: {};".format(color))

    def _log(self, message):
        self.log_text.append(str(message))
        sb = self.log_text.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _load_rasters(self):
        """(Re)load the three rasters from the current paths."""
        try:
            specs = [("depth", "Depth"), ("vel", "Velocity"), ("arr", "Arrival")]
            paths = self._raster_paths()
            rasters, bad = {}, []
            for key, name in specs:
                if not paths[key] and key not in self._required_rasters():
                    rasters[key] = None
                    self._log(">>> {} raster not given - {}".format(
                        name, "arrival times and warning times are left empty"
                        if key == "arr" else "depth-only (1D) criterion"))
                    continue
                lyr = QgsRasterLayer(paths[key], name)
                rasters[key] = lyr
                if not lyr.isValid():
                    bad.append(name)
                elif not lyr.crs().isValid():
                    self._log(">>> WARNING: {} raster has no CRS - coordinates are "
                              "assumed to match the point layers".format(name))
            self._rasters = rasters
            if bad:
                return False, "These rasters failed to load: {}".format(", ".join(bad))
            return True, "OK"
        except Exception as e:
            return False, str(e)

    def _study_area(self, ctx):
        """ExtentClip for the loaded depth raster (None: no clipping) and
        the keep-dry choice; logs both."""
        clip = make_extent_clip(self._rasters.get("depth"), self.extent_spin.value(), ctx)
        keep_dry = self.keep_dry_check.isChecked()
        if clip is not None:
            self._log(">>> Study area: {} (features without flood values {})".format(
                clip.describe(), "kept" if keep_dry else "dropped"))
        else:
            self._log(">>> WARNING: study area unavailable - features limited to raster "
                      "coverage")
        return clip, keep_dry

    # ------------------------------------------------------------- PROGRESS
    def _make_progress(self, title, modal=True):
        if self._chain is not None and self._progress is not None:
            self._progress.setLabelText(title)          # one bar for a chained step
            return self._progress
        prog = QProgressDialog(title, "Cancel", 0, 100, self)
        prog.setWindowTitle(PLUGIN_NAME)
        # Modal (synchronous steps): blocks the plugin dialog and QGIS while
        # the step pumps events. Background runs (v2.6) use a non-modal bar:
        # QGIS stays usable, the dialog's buttons are disabled meanwhile.
        if modal:
            prog.setWindowModality(WINDOW_MODAL)
        prog.setMinimumDuration(0)
        prog.setAutoClose(False)
        prog.setAutoReset(False)
        prog.setValue(0)
        prog.show()
        self._last_tick = 0.0
        self._last_pct = 0.0
        QApplication.processEvents()
        return prog

    def _progress_tick(self, pct, message):
        if self._progress is None:
            return
        pct = min(max(float(pct), 0.0), 100.0)
        if self._chain is not None:
            lo, hi = self._chain
            pct = lo + (hi - lo) * pct / 100.0
        pct = max(self._last_pct, pct)
        now = time.monotonic()
        # Refreshing the UI is comparatively expensive; do it at most every
        # PROGRESS_MIN_INTERVAL_S unless the run is finishing.
        if pct < 100.0 and now - self._last_tick < PROGRESS_MIN_INTERVAL_S:
            return
        self._last_tick = now
        self._last_pct = pct
        self._progress.setValue(int(pct))
        self._progress.setLabelText(message)
        QApplication.processEvents()
        # A click on Cancel after the last step has finished is ignored
        if pct < 100.0 and self._progress.wasCanceled():
            raise CancelledByUser()

    def _finish_progress(self, force=False):
        if self._chain is not None and not force:
            return
        if self._progress:
            self._progress.close()
            self._progress.deleteLater()
            self._progress = None
        QApplication.processEvents()

    # ------------------------------------------------------- BACKGROUND RUNS
    def _start_background(self, title, work, on_done, project_bound=True):
        """v2.6: run work(progress, log) in a QGIS task - QGIS stays usable,
        the dialog is busy. on_done(outcome) runs on the main thread when it
        has finished (outcome.result / .error / .cancelled). Without a task
        manager (tests, headless) the work runs at once, same callbacks.
        project_bound=False (v2.6.2): the work does not belong to the current
        project (offline pack build) - on_done runs even when another
        project was opened meanwhile."""
        from .runner import start_task
        self._set_busy(True)
        self._chain = None
        self._progress = self._make_progress(title, modal=False)
        try:                                     # the bar's Cancel stops the task
            self._progress.canceled.connect(self._cancel_task)
        except Exception:
            pass

        gen = self._project_gen

        def done(outcome):
            self._task = None
            try:
                if project_bound and outcome.ok and gen != self._project_gen:
                    self._log(">>> The project changed while the run was working - the "
                              "outputs are written in {} but not added to this project"
                              .format(self.config_output_folder))
                    self._update_status("Finished - project changed during the run; outputs "
                                        "in the outputs folder", False)
                    return
                on_done(outcome)
            finally:
                self._finishing = False
                self._finish_progress(force=True)
                self._set_busy(False)
        try:
            self._task = start_task(title, work, self._task_progress, self._log, done)
        except Exception as e:                   # the task could not even be started
            tb = traceback.format_exc()
            self._log(tb)
            log_message(tb, MSG_CRITICAL)
            self._finish_progress(force=True)
            self._set_busy(False)
            self._update_status("FAILED: {}".format(e), False)
            QMessageBox.critical(self, "Error", str(e) or repr(e))

    def _cancel_task(self, *args):
        if self._task is not None:
            try:
                self._task.cancel()
            except Exception:
                pass

    def _task_progress(self, pct, message):
        """Progress from the worker (main thread slot) - or from the work
        itself when it runs synchronously."""
        if self._progress is None or self._finishing:
            return                       # (a late worker signal: the finish bar is up)
        pct = min(max(float(pct), 0.0), 100.0)
        pct = max(self._last_pct, pct)
        now = time.monotonic()
        if pct < 100.0 and now - self._last_tick < PROGRESS_MIN_INTERVAL_S:
            return
        self._last_tick = now
        self._last_pct = pct
        self._progress.setValue(int(pct))
        self._progress.setLabelText(message)
        if self._task is None:
            QApplication.processEvents()         # synchronous fallback
        if pct < 100.0 and self._progress.wasCanceled():
            if self._task is not None:
                self._task.cancel()
            else:
                raise CancelledByUser()

    def _finish_deferred(self, res, title):
        """Main-thread part of a background run (layers, map book, report)
        behind a modal progress bar (modality cannot change on a shown bar,
        so a new one replaces the task's)."""
        from .pipeline import finish_run
        self._finishing = True
        self._finish_progress(force=True)
        self._chain = None
        self._progress = self._make_progress(title, modal=True)
        return finish_run(res, progress=self._progress_tick, log=self._log)

    def _set_busy(self, busy):
        self._busy = busy
        for name in ("check_btn", "run_all_btn", "find_rasters_btn", "dam_browse_btn",
                     "depth_browse_btn", "vel_browse_btn", "arr_browse_btn",
                     "output_browse_btn", "selftest_btn", "refresh_layers_btn",
                     "dv_browse_btn", "review_zoom_btn",
                     "review_save_btn", "review_rerun_btn", "sources_btn",
                     "review_export_btn", "review_merge_btn", "series_btn",
                     "dsw_scan_btn", "dsw_import_btn", "ras_scan_btn", "ras_import_btn",
                     "data_download_btn", "data_sources_btn", "open_data_btn",
                     # v2.9
                     "datafolder_edit", "datafolder_browse_btn", "datafolder_search_btn",
                     "datafolder_hifld_btn", "datafolder_use_check",
                     *Extras290TabMixin.BUSY_WIDGETS_290,
                     *SuiteTabMixin.BUSY_WIDGETS_2100,
                     "facrev_kind_combo", "facrev_scenario_combo", "facrev_show_all_check",
                     "facrev_zoom_btn", "facrev_save_btn", "facrev_rerun_btn",
                     # v2.6.1: the pack must not change under a running task
                     "pack_path_edit", "pack_browse_btn", "pack_clear_btn",
                     "offline_only_check", "pack_build_btn",
                     # 2.24.0
                     "pack_zip_btn", "source_pack_radio", "source_download_radio"):
            w = getattr(self, name, None)
            if w is not None:
                w.setEnabled(not busy)
        if not busy and hasattr(self, "_enable_pack_widgets"):
            self._enable_pack_widgets()          # 2.24.0: Download mode keeps them off
        if not busy and hasattr(self, "dsw_import_btn"):
            self.dsw_import_btn.setEnabled(bool(getattr(self, "_dsw_items", None)))
        self._update_button_states()

    def _group(self, category):
        """Layer-tree group path of this dam for a category."""
        return plugin_group(dam_display_name(self.config_dam_folder), category)

    # -------------------------------------------------- CHECK / RUN EVERYTHING
    def _check_paths(self):
        paths = dict(self._raster_paths())
        paths["dv"] = self.dv_edit.text().strip() or None
        return paths

    def _check_cfg(self):
        cfg = dict(self._data_cfg())         # the Data & Output tab choices
        cfg["one_d"] = self._one_d_selected()
        return cfg

    def _run_checks(self, services):
        from .checks import check_all
        return check_all(self._check_paths(), self.config_output_folder,
                         self.config_dam_folder, self._check_cfg(), services=services,
                         progress=self._progress_tick if services else None)

    @staticmethod
    def _checks_html(results):
        colors = {"ERROR": "#c62828", "WARNING": "#e65100", "OK": "#2e7d32"}
        return "<br>".join("<span style='color:{}'><b>{}</b></span> {}".format(
            colors.get(lvl, "#000"), lvl, html.escape(t)) for lvl, t in results)

    def _classify_simple(self, *args):
        """Find the Runner rasters if the boxes are empty, then check and classify."""
        if getattr(self, "_busy", False):
            return                      # a run is in progress: do not mutate raster boxes
        if not self.depth_edit.text().strip():
            self._auto_fill_rasters(force=True)
        self._on_run_all()

    def _on_check_inputs(self, *args):
        if self._busy:
            return
        from .checks import summary
        self._set_busy(True)
        try:
            self._progress = self._make_progress("Checking inputs and data services...")
            results = self._run_checks(services=True)
        except CancelledByUser:
            self._log(">>> Input check cancelled")
            return
        except Exception as e:
            tb = traceback.format_exc()
            self._log(tb)
            log_message(tb, MSG_CRITICAL)
            self._update_status("Input check FAILED: {}".format(e), False)
            QMessageBox.critical(self, "Check Inputs Error",
                                 "{}\n\nFull details are in the log area below.".format(e))
            return
        finally:
            self._finish_progress()
            self._set_busy(False)
        for lvl, t in results:
            self._log(">>> CHECK {}: {}".format(lvl, t))
        n_err, n_warn = summary(results)
        self._update_status("Input check: {} error(s), {} warning(s)".format(n_err, n_warn),
                            n_err == 0)
        # the results are shown in the panel under the buttons (no pop-up)
        self.check_panel.set_results(results)
        return results

    def _on_run_all(self, *args):
        """Check inputs, then (v2.6, one background task) save the data sets
        when asked and run the classification."""
        if self._busy:
            return
        valid, errors = self.validate_all_inputs()
        if not valid:
            QMessageBox.warning(self, "Input Errors",
                                "\n".join("- {}".format(e) for e in errors))
            return
        from .checks import summary, ERROR
        results = self._run_checks(services=False)
        for lvl, t in results:
            if lvl != "OK":
                self._log(">>> CHECK {}: {}".format(lvl, t))
        self.check_panel.set_results(results)
        if summary(results)[0]:
            QMessageBox.warning(self, "Run everything - inputs need fixing",
                                self._checks_html([r for r in results if r[0] == ERROR]))
            return
        download_first = self.data_save_check.isChecked()
        if download_first:
            try:
                self._download_job()
            except Exception as e:
                # optional step: the classification downloads what it needs
                self._log(">>> WARNING: the data sets cannot be saved to outputs/Data ({}) - "
                          "continuing without".format(e))
                download_first = False
        self._launch_classification(download_first=download_first, everything=True)

    # ------------------------------------------------------------ SELF-TEST
    def _on_run_selftest(self):
        if self._busy:
            return
        from .selftest import run_self_test
        self._set_busy(True)
        self._progress = self._make_progress("Running self-test...")
        try:
            ok, lines = run_self_test(progress_cb=self._progress_tick)
        except CancelledByUser:
            self._update_status("Self-test cancelled", False)
            return
        except Exception:
            ok, lines = False, ["Self-test crashed:"] + traceback.format_exc().splitlines()
        finally:
            self._finish_progress()
            self._set_busy(False)
        for line in lines:
            self._log(line)
            log_message(line, MSG_INFO if ok else MSG_WARNING)
        self._update_status("Self-test {}".format("PASSED" if ok else "FAILED"), ok)
        box = QMessageBox.information if ok else QMessageBox.warning
        box(self, "Self-Test", "<br>".join(html.escape(line) for line in lines))

    # ------------------------------------------------------------ SETTINGS --
    _SETTING_WIDGETS = (
        "dam_folder_edit", "output_folder_edit", "depth_edit", "vel_edit", "arr_edit",
        "dv_edit",
        "extent_spin", "keep_dry_check",
        "nsi_download_check", "addr_download_check", "usa_download_check", "model_combo",
        "judgment_check", "near_spin", "borderline_check", "ppd_spin", "min_area_spin",
        "occ_spin", "speed_spin", "lol_check", "warn_spin", "understanding_combo",
        "severity_combo", "population_combo", "access_check", "access_spin",
        "access_medium_check", "report_check", "pages_spin", "road_combo", "aadt_combo",
        "rail_combo", "crossing_combo", "outline_combo", "dsw_path_edit",
        "dams_download_check", "dam_layer_combo", "subject_nid_edit",
        "lifelines_download_check", "lifeline_combo", "lifeline_medium_check",
        "boundary_combo", "eap_check",
        "crossings_download_check", "advanced_check", "spreadsheets_check",
        # v2.5
        "camps_download_check", "parcel_combo", "parcel_acres_spin", "compare_check",
        "eap_list_check", "eap_mapbook_check", "mapbook_scale_combo", "eap_travel_check",
        "centerline_combo", "flowlines_download_check", "station_spin",
        "ras_plan_edit", "ras_dem_edit", "ras_breach_spin", "ras_arrival_spin", "ras_cell_spin",
        # v2.5.1
        "data_area_combo", "flood_buffer_spin", "data_save_check",
        # v2.6
        "zone_rasters_check", "sampled_cells_check", "reuse_check",
        # v2.7
        "main_name_edit", "damage_curves_edit", "hcom_edit", "review_aerial_check",
        # v2.7.1
        "sample_area_combo",
        # v2.8 (the population layer before its field pickers: choosing the layer fills them)
        "sens_check", "sens_factors_edit", "sens_split_check", "term_check", "term_rise_spin",
        "term_depth_spin", "term_confirm_spin", "pop_source_combo", "pop_field_combo",
        "pop_day_combo", "pop_housing_combo", "pop_period_combo", "pop_units_combo",
        "pop_method_combo", "pop_mindepth_spin", "evac_check", "evac_prep_spin",
        "evac_passable_spin", "evac_include_combo", "evac_safe_spin", "cache_check",
        "cache_age_spin", "cache_refresh_check", "quick_check", "watch_workers_spin",
        # v2.9
        "datafolder_edit", "datafolder_use_check", "datafolder_picks_edit",
        # 2.30.0
        "handoff_auto_check",
    ) + Extras290TabMixin.SETTING_WIDGETS_290 + SuiteTabMixin.SETTING_WIDGETS_2100

    def _current_state(self):
        state = {}
        for name in self._SETTING_WIDGETS:
            w = getattr(self, name, None)
            if w is not None:
                v = widget_value(w)
                if v is not None:
                    state[name] = v
        if hasattr(self, "scenario_table"):
            state.update(self._scenario_state())            # v2.7: scenario table
        return state

    def _save_settings(self):
        state = self._current_state()
        # write only real changes (writing marks the project as modified)
        if state != load_state() and (state != self._defaults or load_state()):
            save_state(state)

    def _apply_state(self, state):
        self._restoring = True          # no raster search while settings are restored
        try:
            for name in self._SETTING_WIDGETS:
                if name in state and getattr(self, name, None) is not None:
                    set_widget_value(getattr(self, name), state[name])
            if hasattr(self, "scenario_table"):
                self._apply_scenario_state(state)
            if hasattr(self, "_restore_parcel_fields290"):
                self._restore_parcel_fields290(state)
        finally:
            self._restoring = False
        # 2.17.0: the restored rasters / NID ID belong to the restored dam folder
        self._mark_restored_rasters()
        self._nid_dam = None
        if self.config_dam_folder and os.path.isdir(self.config_dam_folder):
            self._fill_from_dam_record(self.config_dam_folder, restored=True)
        if hasattr(self, "_df_dam_picks"):
            try:
                self._df_dam_picks()
            except Exception as e:                                   # noqa
                log_message("Dam folder data not read: {}".format(e))
        # an output folder chosen by the user (after the dam folder, which
        # resets it to <dam>/outputs); the review decisions live there
        out = (state.get("output_folder_edit") or "").strip()
        if out and self.config_dam_folder:
            self.config_output_folder = out
            self.output_folder_edit.setText(out)
            self._update_button_states()

    def _restore_settings(self):
        state = load_state()
        if not state:
            return
        self._apply_state(state)
        self._log(">>> Settings restored from the QGIS project")

    def _on_project_changed(self, *args):
        """Another project was opened (or the project was closed): show that
        project's settings instead of the previous project's."""
        self._project_gen += 1          # a running task must not finish into it
        if self._busy:
            return
        self._apply_state(dict(self._defaults))
        self._last_run = None
        # nothing from the previous project's run may be edited or opened
        if hasattr(self, "review_table"):
            self._review_rows = []
            self.review_table.setRowCount(0)
            self.review_status.setText("Run Hazard Classification first")
        if hasattr(self, "_facrev_clear"):
            self._facrev_clear()
        if hasattr(self, "open_report_btn"):
            self.open_report_btn.setEnabled(False)
        # the layer choices of the new project can only be restored once the
        # combos list the new project's layers
        if hasattr(self, "_refresh_layer_combos"):
            try:
                self._refresh_layer_combos()
            except Exception as e:
                log_message("Layer lists not refreshed: {}".format(e))
        self._restore_settings()
        self._update_button_states()

    def _on_write_project(self, *args):
        """Project > Save: store the dialog settings with it."""
        if self._busy:
            return
        try:
            self._save_settings()
        except Exception as e:
            log_message("Settings not saved in the project: {}".format(e))

    def hideEvent(self, event):
        # Esc / the window's close box hide the dialog: keep its settings
        if not self._busy:
            try:
                self._save_settings()
            except Exception:
                pass
        super().hideEvent(event)

    def closeEvent(self, event):
        if self._busy:
            event.ignore()
            return
        try:
            self._save_settings()
        except Exception:
            pass
        super().closeEvent(event)
