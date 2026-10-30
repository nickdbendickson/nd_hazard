# -*- coding: utf-8 -*-
"""'Hazard Classification' tab: UI (mixed into the main dialog). The run
itself is pipeline.run_classification (shared with the Processing
algorithm): habitable structures, overtopped roads (most recent AADT) and
railroads (trains/day), consequences, optional no-breach baseline and
additional breach scenarios, ND Dam Safety Standards (REG_05.2026, Sec. 5.2)
and ACER TM 11 Table 1."""

import html
import os
import re
import traceback

from qgis.PyQt.QtWidgets import (
    QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QGroupBox, QFormLayout,
    QSpinBox, QDoubleSpinBox, QCheckBox, QComboBox, QTextEdit, QWidget, QMessageBox,
    QFileDialog, QTableWidget, QTableWidgetItem, QScrollArea, QLineEdit,
)
from qgis.core import QgsProject, QgsVectorLayer

from .compat import (GEOM_POINT, GEOM_LINE, GEOM_POLYGON, MSG_CRITICAL, MSG_WARNING,
                     MB_YES, MB_NO, MB_CANCEL)
from .constants import (
    BTN_ACTIVE_STYLE, BTN_INACTIVE_STYLE,
    TM11_PERSONS_PER_DWELLING, DEFAULT_MIN_UNVERIFIED_AREA_FT2,
    DEFAULT_VEHICLE_OCCUPANCY, DEFAULT_VEHICLE_SPEED_MPH, vehicle_curve_available,
    DEFAULT_NEAR_THRESHOLD_FT, DEFAULT_ACCESS_SEARCH_FT,
    DEFAULT_WARNING_ISSUED_HR, MAPBOOK_SCALES, MAPBOOK_DEFAULT_SCALE,
    TRAVEL_STATION_MI, DEFAULT_PARCEL_MAX_ACRES,
)
from .helpers import log_message, CancelledByUser
from .structures import SAMPLE_AREAS
from .processing import is_plugin_output_name, is_downloaded_data_layer
from .scenarios import (ROLE_BREACH, ROLE_BASELINE, MAIN_NAME, clean_rows, raster_reuse_problems,
                        rows_to_scenarios,
                        comparison_rows, COMPARE_COLS)

_AUTO, _NONE, _DOWNLOAD = "__auto__", "__none__", "__download__"
_ROAD_RE = re.compile(r"road|street|hpms|highway|hwy|route|aadt|traffic", re.I)
_RAIL_RE = re.compile(r"rail|narn|track", re.I)
_CROSS_RE = re.compile(r"crossing", re.I)
_PLUGIN_POI_RE = re.compile(r"_POI$", re.I)       # the <dam>_POI.gpkg layer
_PARCEL_RE = re.compile(r"parcel", re.I)
_CENTERLINE_RE = re.compile(r"center ?line|centreline|thalweg|channel|flow ?line|stream", re.I)
# the plugin's own results are never picked as inputs by auto-detect
_PARCEL_EXCLUDE = re.compile(r"^Future_Development", re.I)
_CENTER_EXCLUDE = re.compile(_ROAD_RE.pattern + r"|^EAP_", re.I)
RESULT_COLS = ["Section", "Criterion / item", "Found", "Result"]
SCN_COLS = ["Name", "Role", "Baseline (no breach)", "Depth raster", "Velocity raster",
            "Arrival raster", "D x V raster"]
_SCN_PATH_COLS = {3: "depth", 4: "velocity", 5: "arrival", 6: "dv"}
_NO_BASELINE = ""


class ClassificationTabMixin(object):
    """Methods for the Hazard Classification tab (needs the main dialog's
    _load_rasters, _make_progress, _progress_tick, _finish_progress, _log,
    _update_status, _set_busy, _study_area, config_* attributes)."""

    # ------------------------------------------------------------------ UI --
    def _build_classification_tab(self):
        tab = QWidget()
        self._class_tab = tab
        outer = QVBoxLayout(tab)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body = QWidget()
        lay = QVBoxLayout(body)
        intro = QLabel(
            "<h3>Hazard Classification</h3>"
            "Finds habitable structures, overtopped roads (with the most recent AADT) and "
            "inundated railroads (with trains/day), estimates consequences and classifies the "
            "dam under the <b>ND Dam Safety Standards Policy REG_05.2026, Section 5.2</b> and "
            "<b>USBR ACER TM 11</b> (ACER zones from max depth with max velocity). Uses the "
            "dam folder and rasters from the Setup tab and the data chosen on the Data &amp; "
            "Output tab.")
        intro.setWordWrap(True)
        lay.addWidget(intro)

        # v2.5.1: one box shows the advanced groups (criteria, consequences and
        # EAP outputs); their defaults apply while they are hidden
        self.advanced_check = QCheckBox("Show advanced options (criteria, consequences and "
                                        "EAP outputs - the ND defaults are preset)")
        self.advanced_check.setChecked(False)
        lay.addWidget(self.advanced_check)
        crit = QGroupBox("1. Criteria (advanced - ND defaults)")
        cform = QFormLayout()
        # 2.35.0 (Nick, 10/30/2026): an ACER judgment-zone house makes the dam MEDIUM by
        # rule. The old setting stays as an option, off by default; the TM 11 tick keeps its
        # meaning for lives in jeopardy only.
        self.nd_judgment_check = QCheckBox(
            "ND: count ACER judgment-zone houses as probable loss of life (HIGH) - old "
            "option; off = MEDIUM by rule")
        self.nd_judgment_check.setChecked(False)
        cform.addRow("", self.nd_judgment_check)
        self.judgment_check = QCheckBox(
            "TM 11: count ACER judgment-zone persons in lives in jeopardy")
        self.judgment_check.setChecked(True)
        cform.addRow("", self.judgment_check)
        self.near_spin = QDoubleSpinBox()
        self.near_spin.setRange(0.0, 5.0)
        self.near_spin.setSingleStep(0.1)
        self.near_spin.setValue(DEFAULT_NEAR_THRESHOLD_FT)
        self.near_spin.setSuffix(" ft")
        self.near_spin.setToolTip("Structures this close below the next ACER boundary are "
                                  "flagged as borderline (ND 5.3(f)).")
        cform.addRow("Borderline distance:", self.near_spin)
        self.borderline_check = QCheckBox("Count borderline structures in the higher zone")
        cform.addRow("", self.borderline_check)
        self.ppd_spin = QSpinBox()
        self.ppd_spin.setRange(1, 20)
        self.ppd_spin.setValue(TM11_PERSONS_PER_DWELLING)
        cform.addRow("Persons per dwelling (TM 11):", self.ppd_spin)
        self.min_area_spin = QSpinBox()
        self.min_area_spin.setRange(0, 10000)
        self.min_area_spin.setValue(int(DEFAULT_MIN_UNVERIFIED_AREA_FT2))
        self.min_area_spin.setSuffix(" ft²")
        self.min_area_spin.setToolTip("Untagged building outlines smaller than this are "
                                      "treated as sheds / bins (not habitable).")
        cform.addRow("Min. area, untagged outline:", self.min_area_spin)
        self.sample_area_combo = QComboBox()
        for _val, _label in SAMPLE_AREAS:
            self.sample_area_combo.addItem(_label, _val)
        self.sample_area_combo.setToolTip(
            "Where around a structure the maximum depth and velocity are read. The default "
            "reads every raster cell in the building outline plus its 5-ft buffer (interior "
            "cells included), which gives the highest values. 'Buffer ring only' reads just "
            "the ring outside the wall (water against the wall); 'Band' reads 5 ft either side "
            "of the wall - the maximum points then lie along the buffer / outline. A structure "
            "with no cell centre in the ring or band (grid coarser than the ring) is read over "
            "the whole area. The layer Structure_Evaluation_Areas shows the area used. "
            "Changing this re-reads the rasters on the next run.")
        cform.addRow("Max depth / velocity read from:", self.sample_area_combo)
        self.occ_spin = QDoubleSpinBox()
        self.occ_spin.setRange(0.5, 6.0)
        self.occ_spin.setSingleStep(0.1)
        self.occ_spin.setValue(DEFAULT_VEHICLE_OCCUPANCY)
        cform.addRow("Persons per vehicle (TM 11 motorists):", self.occ_spin)
        self.speed_spin = QDoubleSpinBox()
        self.speed_spin.setRange(5.0, 85.0)
        self.speed_spin.setValue(DEFAULT_VEHICLE_SPEED_MPH)
        self.speed_spin.setSuffix(" mph")
        cform.addRow("Travel speed (motorist estimate):", self.speed_spin)
        self.lifeline_medium_check = QCheckBox(
            "Inundated lifeline facilities make the dam at least ND Medium (5.2.2)")
        self.lifeline_medium_check.setChecked(True)
        cform.addRow("", self.lifeline_medium_check)
        self.parcel_acres_spin = QDoubleSpinBox()
        self.parcel_acres_spin.setRange(0.1, 640.0)
        self.parcel_acres_spin.setSingleStep(0.5)
        self.parcel_acres_spin.setValue(DEFAULT_PARCEL_MAX_ACRES)
        self.parcel_acres_spin.setSuffix(" ac")
        self.parcel_acres_spin.setToolTip("Future development: vacant parcels up to this size "
                                          "are screened (larger ones are usually farmland).")
        cform.addRow("Future development - max parcel size:", self.parcel_acres_spin)
        crit.setLayout(cform)
        lay.addWidget(crit)

        rep = QGroupBox("2. Report and map outputs")
        rform = QFormLayout()
        self.report_check = QCheckBox("PDF report with maps (print layout kept in the project)")
        self.report_check.setChecked(True)
        rform.addRow("", self.report_check)
        self.pages_spin = QSpinBox()
        self.pages_spin.setRange(0, 40)
        self.pages_spin.setValue(8)
        rform.addRow("Zoom map pages (max):", self.pages_spin)
        self.compare_check = QCheckBox("Compare with the last run (what changed - "
                                       "QA/Changes_Since_Last_Run.csv)")
        self.compare_check.setChecked(True)
        rform.addRow("", self.compare_check)
        # v2.6: the map products of the former Main Consolidation
        self.zone_rasters_check = QCheckBox("ACER zone rasters - houses, mobile homes, vehicles "
                                            "(Classification/Hazard_*.tif)")
        self.zone_rasters_check.setChecked(True)
        self.zone_rasters_check.setToolTip("Every cell classified from its max depth with its "
                                           "max velocity (TM 11 Figs 2-4). Needs the velocity "
                                           "raster.")
        rform.addRow("", self.zone_rasters_check)
        self.sampled_cells_check = QCheckBox("Layer of the raster cells read for each structure "
                                             "(QA/Structure_Sampled_Cells)")
        self.sampled_cells_check.setChecked(True)
        self.sampled_cells_check.setToolTip("The evaluation areas (outline + 5-ft edge ring) "
                                            "are always written to QA/Structure_Evaluation_Areas;"
                                            " this adds the cells behind them.")
        rform.addRow("", self.sampled_cells_check)
        self.reuse_check = QCheckBox("Re-use the last run's downloads, inventory and raster "
                                     "sampling when they are unchanged (fast re-runs after a "
                                     "review or a setting change)")
        self.reuse_check.setChecked(True)
        self.reuse_check.setToolTip(
            "A run first checks the rasters and files by their CONTENT (SHA-256 digest, "
            "the same digest the ND Dam Breach Runner writes into its run manifest - a file "
            "touched or copied without a change is unchanged; one rewritten with other values "
            "is changed even when its size and date are the same), the chosen layers, the "
            "download choices and the settings that shape the sampling. When nothing changed since "
            "the last run (in this QGIS session, or an earlier one - the evaluation is kept in "
            "outputs/QA for up to 7 days), that evaluation is re-used and only the "
            "review decisions, rules, consequences, classification and outputs are redone "
            "(seconds instead of minutes). The summary says when this happened. Untick "
            "for a full run.")
        rform.addRow("", self.reuse_check)
        rep.setLayout(rform)
        lay.addWidget(rep)

        cons = QGroupBox("3. Consequences (advanced - screening defaults)")
        kform = QFormLayout()
        self.lol_check = QCheckBox("Loss-of-life estimate (Graham 1999, DSO-99-06)")
        self.lol_check.setChecked(True)
        kform.addRow("", self.lol_check)
        self.warn_spin = QDoubleSpinBox()
        self.warn_spin.setRange(0.0, 48.0)
        self.warn_spin.setSingleStep(0.25)
        self.warn_spin.setValue(DEFAULT_WARNING_ISSUED_HR)
        self.warn_spin.setSuffix(" h after breach start")
        kform.addRow("Warning issued:", self.warn_spin)
        self.understanding_combo = QComboBox()
        self.understanding_combo.addItem("Vague", "vague")
        self.understanding_combo.addItem("Precise", "precise")
        kform.addRow("Flood severity understanding:", self.understanding_combo)
        self.severity_combo = QComboBox()
        self.severity_combo.addItem("Graham 1999 (DV 50 ft²/s; 10 ft without velocity)",
                                    "graham")
        self.severity_combo.addItem("RCEM 2015 bands (DV 50 / 160 ft²/s)", "rcem")
        kform.addRow("Flood severity:", self.severity_combo)
        self.population_combo = QComboBox()
        self.population_combo.addItem("Night (NSI 2 am)", "night")
        self.population_combo.addItem("Day (NSI 2 pm)", "day")
        kform.addRow("Population:", self.population_combo)
        self.damage_curves_edit = QLineEdit()
        self.damage_curves_edit.setPlaceholderText(
            "optional - CSV of depth-damage curves for commercial / industrial / other "
            "structures")
        self.damage_curves_edit.setToolTip(
            "The plugin has no curves for non-residential structures (which curves to use is "
            "your choice: Hazus, USACE ...). Without a file only their value exposed is "
            "reported. 'Template' writes a file showing the format.")
        b_browse = QPushButton("Browse...")
        b_browse.clicked.connect(self._damage_curves_browse)
        b_tmpl = QPushButton("Template")
        b_tmpl.clicked.connect(self._damage_curves_template)
        wrow = QWidget()
        hrow = QHBoxLayout(wrow)
        hrow.setContentsMargins(0, 0, 0, 0)
        hrow.addWidget(self.damage_curves_edit, stretch=3)
        hrow.addWidget(b_browse)
        hrow.addWidget(b_tmpl)
        kform.addRow("Non-residential damage curves:", wrow)
        self.hcom_edit = QLineEdit()
        self.hcom_edit.setPlaceholderText(
            "optional - DSS-WISE HCOM table (filled by the DSS-WISE import); ';' between files")
        self.hcom_edit.setToolTip(
            "Compares the population at risk with DSS-WISE's own human-consequence table "
            "(day and night population by census block). A big difference is a reason to look "
            "at the structure data; it does not change the classification.")
        b_hcom = QPushButton("Browse...")
        b_hcom.clicked.connect(self._hcom_browse)
        wrow2 = QWidget()
        hrow2 = QHBoxLayout(wrow2)
        hrow2.setContentsMargins(0, 0, 0, 0)
        hrow2.addWidget(self.hcom_edit, stretch=3)
        hrow2.addWidget(b_hcom)
        kform.addRow("DSS-WISE HCOM table (cross-check):", wrow2)
        self.access_check = QCheckBox("Sole-access screening on the road network")
        self.access_check.setChecked(True)
        kform.addRow("", self.access_check)
        self.access_spin = QSpinBox()
        self.access_spin.setRange(50, 5280)
        self.access_spin.setValue(int(DEFAULT_ACCESS_SEARCH_FT))
        self.access_spin.setSuffix(" ft")
        kform.addRow("Structure to road, max:", self.access_spin)
        self.access_medium_check = QCheckBox(
            "Cut-off habitable structures make the dam at least ND Medium (5.2.1.2)")
        self.access_medium_check.setChecked(True)
        kform.addRow("", self.access_medium_check)
        cons.setLayout(kform)
        lay.addWidget(cons)

        # v2.5: every EAP output can be switched off (all of them with the
        # first box)
        eapg = QGroupBox("4. Emergency Action Plan outputs (advanced)")
        eform = QFormLayout()
        self.eap_check = QCheckBox("Make Emergency Action Plan outputs")
        self.eap_check.setChecked(True)
        self.eap_check.setToolTip("Untick to skip every EAP output below (faster runs while "
                                  "the classification is being worked out).")
        eform.addRow("", self.eap_check)
        self.eap_list_check = QCheckBox("Notification list (sorted by arrival time, "
                                        "priority for schools / care homes) and arrival-time "
                                        "bands and contours")
        self.eap_list_check.setChecked(True)
        eform.addRow("", self.eap_list_check)
        self.eap_mapbook_check = QCheckBox("Inundation map book (PDF: index + numbered map "
                                           "sheets with the notification items)")
        self.eap_mapbook_check.setChecked(True)
        self.eap_mapbook_check.setToolTip("Also makes the notification list and arrival maps "
                                          "(the sheets show them).")
        eform.addRow("", self.eap_mapbook_check)
        self.mapbook_scale_combo = QComboBox()
        for text, sc in MAPBOOK_SCALES:
            self.mapbook_scale_combo.addItem(text, sc)
        i = self.mapbook_scale_combo.findData(MAPBOOK_DEFAULT_SCALE)
        self.mapbook_scale_combo.setCurrentIndex(i if i >= 0 else 0)
        eform.addRow("Map book scale:", self.mapbook_scale_combo)
        self.eap_travel_check = QCheckBox("Travel-time table along the channel (river miles, "
                                          "towns, bridges, road closures)")
        self.eap_travel_check.setChecked(True)
        eform.addRow("", self.eap_travel_check)
        self.station_spin = QDoubleSpinBox()
        self.station_spin.setRange(0.1, 10.0)
        self.station_spin.setSingleStep(0.1)
        self.station_spin.setValue(TRAVEL_STATION_MI)
        self.station_spin.setSuffix(" mi")
        eform.addRow("Station spacing:", self.station_spin)
        eapg.setLayout(eform)
        lay.addWidget(eapg)
        scn_group = self._build_scenario_group()
        lay.addWidget(scn_group)
        extras_group = self._build_extras_group()            # v2.8
        lay.addWidget(extras_group)
        extras290_group = self._build_extras290_group()      # v2.9
        lay.addWidget(extras290_group)
        if hasattr(self, "_build_suite_group"):
            self.suite_group = self._build_suite_group()     # v2.10 (always shown)
            lay.addWidget(self.suite_group)
        try:
            self.parcel_combo.currentIndexChanged.connect(self._refresh_parcel_fields290)
        except Exception:
            pass
        self._eap_parts = (self.eap_list_check, self.eap_mapbook_check,
                           self.mapbook_scale_combo, self.eap_travel_check, self.station_spin)
        self.eap_check.toggled.connect(self._eap_enable)
        self.eap_mapbook_check.toggled.connect(self._eap_enable)
        self.eap_travel_check.toggled.connect(self._eap_enable)
        self._advanced_groups = (crit, cons, eapg, scn_group, extras_group, extras290_group)
        for g in self._advanced_groups:
            g.setVisible(False)
        self.advanced_check.toggled.connect(
            lambda on: [g.setVisible(bool(on)) for g in self._advanced_groups])
        lay.addStretch()
        scroll.setWidget(body)
        outer.addWidget(scroll)

        row = QHBoxLayout()
        self.run_class_btn = QPushButton("Run Hazard Classification")
        self.run_class_btn.clicked.connect(self._on_run_classification)
        row.addWidget(self.run_class_btn)
        self.open_report_btn = QPushButton("Open PDF report")
        self.open_report_btn.setToolTip("Opens the report written by the last run (its "
                                        "file name changes when the old PDF was open)")
        self.open_report_btn.setEnabled(False)
        self.open_report_btn.clicked.connect(self._open_last_report)
        row.addWidget(self.open_report_btn)
        self.submittal_btn = QPushButton("Make submittal package")
        self.submittal_btn.setToolTip(
            "Gathers the last run's report, summary, run log, review decisions, change log, "
            "map book and result tables into a dated folder (and zip) under outputs/Submittal, "
            "with a manifest and a cover-letter template")
        self.submittal_btn.clicked.connect(self._make_submittal)
        row.addWidget(self.submittal_btn)
        # 2.33.0: the five tools that open their own window sit on a second row, so the
        # page fits a 1080p screen without a sideways scroll (docs/gui-grammar.md)
        row2 = QHBoxLayout()
        self.watch_btn = QPushButton("Creep watch...")
        self.watch_btn.setToolTip(
            "Re-classifies every dam in a folder with fresh data and lists the dams whose "
            "class changed or that gained flooded structures since their last run. Runs in "
            "this QGIS session (it can take long for many dams - for those, schedule the "
            "Processing algorithm 'Hazard creep watch' with qgis_process).")
        self.watch_btn.clicked.connect(self._run_watch)
        row2.addWidget(self.watch_btn)
        self.series_btn = QPushButton("Dams in series...")
        self.series_btn.setToolTip(
            "Screens a folder of studied dams: which dam's breach flood reaches which other "
            "dam (depth, arrival time), cascade candidates and class-order flags. A screening "
            "table only - it does not route a combined breach. Writes Dams_In_Series_Screen.csv / "
            ".html.")
        self.series_btn.clicked.connect(self._run_series)
        row2.addWidget(self.series_btn)
        # 2.30.0: the one-page EAP map, the owner / county letter drafts and the class history
        self.eap_map_btn = QPushButton("EAP map (1 page)...")
        self.eap_map_btn.setToolTip(
            "Fills a print layout from this dam folder in one click - title block, the "
            "inundation shaded by arrival time, structures, roads, legend and the data "
            "vintages - and exports EAP/EAP_Map.pdf and a GeoPDF. The layout stays in the "
            "project's Layout Manager for editing.")
        self.eap_map_btn.clicked.connect(self._build_eap_map)
        row2.addWidget(self.eap_map_btn)
        self.letters_btn = QPushButton("Owner / county letters...")
        self.letters_btn.setToolTip(
            "Drafts the dam-owner letter and the county notification from the last run "
            "(class, the reason, what is downstream, what changed since the last "
            "classification), to edit and export as .docx or .md. Drafts only: no legal "
            "language is written for you.")
        self.letters_btn.clicked.connect(self._open_letters)
        row2.addWidget(self.letters_btn)
        self.history_btn = QPushButton("Class history...")
        self.history_btn.setToolTip(
            "Every classification run recorded for this dam: the class over time with the "
            "data vintage and the plugin version that produced it "
            "(<dam folder>/Classification_History.json).")
        self.history_btn.clicked.connect(self._show_history)
        row2.addWidget(self.history_btn)
        self.open_outputs_btn = QPushButton("Open outputs folder")
        self.open_outputs_btn.clicked.connect(self._open_outputs_folder)
        row.addWidget(self.open_outputs_btn)
        row.addStretch()
        outer.addLayout(row)
        row2.addStretch()
        outer.addLayout(row2)
        self.result_label = QLabel("No classification run yet")
        self.result_label.setStyleSheet("font-size: 14px; font-weight: bold; padding: 4px;")
        outer.addWidget(self.result_label)
        self.result_table = QTableWidget()
        self.result_table.setColumnCount(len(RESULT_COLS))
        self.result_table.setHorizontalHeaderLabels(RESULT_COLS)
        self.result_table.setRowCount(0)
        self.result_table.setToolTip("Double-click a row to zoom to the structures, roads or "
                                     "facilities behind it")
        self.result_table.cellDoubleClicked.connect(self._result_zoom)
        outer.addWidget(self.result_table)
        self.scenario_result_table = QTableWidget()
        self.scenario_result_table.setColumnCount(len(COMPARE_COLS))
        self.scenario_result_table.setHorizontalHeaderLabels(COMPARE_COLS)
        self.scenario_result_table.setMaximumHeight(110)
        self.scenario_result_table.setToolTip("Every breach scenario side by side (with a "
                                              "baseline: what the breach adds)")
        self.scenario_result_table.setVisible(False)
        outer.addWidget(self.scenario_result_table)
        self.class_result_text = QTextEdit()
        self.class_result_text.setReadOnly(True)
        self.class_result_text.setMaximumHeight(120)
        self.class_result_text.setStyleSheet(
            "font-family: 'Menlo', 'Courier New', monospace; font-size: 11px;")
        outer.addWidget(self.class_result_text)
        self._refresh_layer_combos()
        self._eap_enable()
        return tab

    def _damage_curves_browse(self, *args):
        start = os.path.dirname(self.damage_curves_edit.text().strip()) or getattr(
            self, "config_dam_folder", "") or ""
        path, _f = QFileDialog.getOpenFileName(self, "Non-residential depth-damage curves",
                                               start, "CSV (*.csv);;All Files (*)")
        if path:
            self.damage_curves_edit.setText(path)

    def _hcom_browse(self, *args):
        start = os.path.dirname((self.hcom_edit.text().split(";")[0]).strip()) or getattr(
            self, "config_dam_folder", "") or ""
        path, _f = QFileDialog.getOpenFileName(self, "DSS-WISE HCOM table", start,
                                               "Tables (*.xlsx *.xls *.csv);;All Files (*)")
        if path:
            self.hcom_edit.setText(path)

    def _damage_curves_template(self, *args):
        from . import damage_curves as DC
        folder = getattr(self, "config_dam_folder", "") or ""
        if not folder or not os.path.isdir(folder):
            QMessageBox.information(self, "Damage curves", "Choose the dam folder first - the "
                                    "template is written there.")
            return
        try:
            path = DC.write_template(folder)
        except OSError as e:
            QMessageBox.warning(self, "Damage curves", "Could not write the template: "
                                "{}".format(e))
            return
        self.damage_curves_edit.setText(path)
        self._log(">>> Curve template written: {} - fill it in, then run".format(path))
        self._open_path(path)

    def _eap_enable(self, *args):
        on = self.eap_check.isChecked()
        for w in self._eap_parts:
            w.setEnabled(on)
        if on:
            self.mapbook_scale_combo.setEnabled(self.eap_mapbook_check.isChecked())
            self.station_spin.setEnabled(self.eap_travel_check.isChecked())

    def _browse_into(self, edit, kind):
        start = os.path.dirname(edit.text().strip()) or getattr(self, "config_dam_folder", "")
        if kind == "folder":
            path = QFileDialog.getExistingDirectory(self, "Select folder", start or "")
        else:
            path, _f = QFileDialog.getOpenFileName(
                self, "Select raster", start or "",
                "Raster Files (*.tif *.tiff *.TIF *.TIFF *.img *.asc *.vrt *.flt *.bil);;"
                "All Files (*)")
        if path:
            edit.setText(path)

    # ------------------------------------------------------ scenario table --
    # v2.7: the Setup tab rasters are the main breach scenario; more breach
    # scenarios (sunny-day, PMF ...) and no-breach baselines are added here.
    def _build_scenario_group(self):
        grp = QGroupBox("5. Breach scenarios and no-breach baseline (advanced)")
        lay = QVBoxLayout()
        info = QLabel(
            "The rasters on the Setup tab are the main breach scenario. Add further breach "
            "scenarios (e.g. sunny-day and PMF failure) - each is classified separately and "
            "the most severe governs - and a <b>no-breach baseline</b> (the same flood "
            "without the dam failing): with a baseline only the consequences the breach adds "
            "are charged to the dam (FEMA P-333 incremental analysis). Double-click a raster "
            "cell to choose the file.")
        info.setWordWrap(True)
        lay.addWidget(info)
        form = QFormLayout()
        self.main_name_edit = QLineEdit()
        self.main_name_edit.setText(MAIN_NAME)
        self.main_name_edit.setToolTip("Name of the scenario made from the Setup tab rasters")
        form.addRow("Setup tab rasters are scenario:", self.main_name_edit)
        self.main_baseline_combo = QComboBox()
        self.main_baseline_combo.addItem("(no baseline)", _NO_BASELINE)
        self.main_baseline_combo.setToolTip("A baseline row of the table to compare this "
                                            "scenario with")
        form.addRow("Its no-breach baseline:", self.main_baseline_combo)
        lay.addLayout(form)
        self.scenario_table = QTableWidget()
        self.scenario_table.setColumnCount(len(SCN_COLS))
        self.scenario_table.setHorizontalHeaderLabels(SCN_COLS)
        self.scenario_table.setRowCount(0)
        self.scenario_table.setMaximumHeight(170)
        self.scenario_table.cellDoubleClicked.connect(self._scn_browse)
        self.scenario_table.cellChanged.connect(self._scn_names_changed)
        lay.addWidget(self.scenario_table)
        row = QHBoxLayout()
        self.scn_add_btn = QPushButton("Add scenario")
        self.scn_add_btn.clicked.connect(lambda *a: self._scn_add_row())
        row.addWidget(self.scn_add_btn)
        self.scn_folder_btn = QPushButton("Add from a folder...")
        self.scn_folder_btn.setToolTip("Look through a folder of one model run for its "
                                       "depth, velocity, arrival and D x V rasters")
        self.scn_folder_btn.clicked.connect(self._scn_add_from_folder)
        row.addWidget(self.scn_folder_btn)
        self.scn_baseline_btn = QPushButton("Find baseline in the dam folder")
        self.scn_baseline_btn.setToolTip("Adds the no-breach rasters found in the dam "
                                         "folder (names such as 'no breach', 'base flow')")
        self.scn_baseline_btn.clicked.connect(self._scn_find_baseline)
        row.addWidget(self.scn_baseline_btn)
        self.scn_remove_btn = QPushButton("Remove selected")
        self.scn_remove_btn.clicked.connect(self._scn_remove_selected)
        row.addWidget(self.scn_remove_btn)
        row.addStretch()
        lay.addLayout(row)
        grp.setLayout(lay)
        return grp

    def _scn_add_row(self, row=None):
        """Append a row (dict of ROW_KEYS) to the table; returns its index."""
        t = self.scenario_table
        row = row or {}
        r = t.rowCount()
        t.setRowCount(r + 1)
        t.setItem(r, 0, QTableWidgetItem(row.get("name") or "Scenario {}".format(r + 1)))
        role = QComboBox()
        role.addItem("Breach", ROLE_BREACH)
        role.addItem("No-breach baseline", ROLE_BASELINE)
        role.setCurrentIndex(1 if row.get("role") == ROLE_BASELINE else 0)
        role.currentIndexChanged.connect(lambda *a: self._scn_refresh_baselines())
        t.setCellWidget(r, 1, role)
        t.setCellWidget(r, 2, QComboBox())
        for col, key in _SCN_PATH_COLS.items():
            t.setItem(r, col, QTableWidgetItem(row.get(key) or ""))
        self._scn_refresh_baselines(want={r: row.get("baseline") or _NO_BASELINE})
        return r

    def _scn_cell_text(self, r, c):
        it = self.scenario_table.item(r, c)
        return it.text().strip() if it is not None else ""

    def _scn_names_changed(self, r, c, *args):
        if c == 0 and not getattr(self, "_scn_busy", False):
            self._scn_refresh_baselines()

    def _scn_refresh_baselines(self, want=None):
        """The baseline lists follow the names of the baseline rows; each
        list keeps its choice when that baseline still exists."""
        if getattr(self, "_scn_busy", False):
            return
        self._scn_busy = True
        try:
            t = self.scenario_table
            names = []
            for r in range(t.rowCount()):
                role = t.cellWidget(r, 1)
                if role is not None and role.currentData() == ROLE_BASELINE:
                    n = self._scn_cell_text(r, 0)
                    if n:
                        names.append(n)
            want = dict(want or {})

            def fill(combo, keep):
                combo.clear()
                combo.addItem("(no baseline)", _NO_BASELINE)
                for n in names:
                    combo.addItem(n, n)
                for i in range(combo.count()):
                    if combo.itemData(i) == keep:
                        combo.setCurrentIndex(i)
                        return
                combo.setCurrentIndex(0)
            fill(self.main_baseline_combo, self.main_baseline_combo.currentData())
            for r in range(t.rowCount()):
                combo, role = t.cellWidget(r, 2), t.cellWidget(r, 1)
                if combo is None:
                    continue
                keep = want.get(r, combo.currentData())
                fill(combo, keep)
                combo.setEnabled(role is None or role.currentData() == ROLE_BREACH)
        finally:
            self._scn_busy = False

    def _scn_browse(self, r, c, *args):
        if c not in _SCN_PATH_COLS:
            return
        start = os.path.dirname(self._scn_cell_text(r, c)) or getattr(
            self, "config_dam_folder", "") or ""
        path, _f = QFileDialog.getOpenFileName(
            self, "Select raster", start,
            "Raster Files (*.tif *.tiff *.TIF *.TIFF *.img *.asc *.vrt *.flt *.bil);;"
            "All Files (*)")
        if path:
            self.scenario_table.setItem(r, c, QTableWidgetItem(path))

    def _scn_remove_selected(self, *args):
        t = self.scenario_table
        picked = {i.row() for i in (t.selectedItems() or [])}
        if not picked:
            cur = t.currentRow()
            if cur is not None and cur >= 0:
                picked = {cur}
        rows = sorted(picked, reverse=True)
        keep = self._table_scenarios()
        for r in rows:
            if 0 <= r < len(keep):
                del keep[r]
        self._set_table_scenarios(keep)

    def _scn_add_from_folder(self, *args):
        folder = QFileDialog.getExistingDirectory(
            self, "Folder of one model run (its rasters)",
            getattr(self, "config_dam_folder", "") or "")
        if not folder:
            return
        from .autodetect import find_rasters
        found = find_rasters(folder)
        if not found.get("depth"):
            QMessageBox.information(self, "Scenarios", "No depth raster found in that folder.")
            return
        self._scn_add_row({"name": os.path.basename(folder.rstrip("/\\")) or "Scenario",
                           "depth": found["depth"], "velocity": found.get("vel"),
                           "arrival": found.get("arr"), "dv": found.get("dv")})

    def _scn_find_baseline(self, *args):
        from .autodetect import find_rasters
        found = find_rasters(getattr(self, "config_dam_folder", "") or "")
        if not found.get("base_depth"):
            QMessageBox.information(self, "Scenarios",
                                    "No no-breach raster found in the dam folder.")
            return
        r = self._scn_add_row({"name": "No breach", "role": ROLE_BASELINE,
                               "depth": found["base_depth"], "velocity": found.get("base_vel"),
                               "arrival": found.get("base_arr")})
        self._scn_refresh_baselines()
        i = self.main_baseline_combo.findData("No breach") if hasattr(
            self.main_baseline_combo, "findData") else -1
        if i is not None and i >= 0:
            self.main_baseline_combo.setCurrentIndex(i)
        return r

    def _table_scenarios(self):
        """The table's rows as dicts (scenarios.ROW_KEYS)."""
        t = self.scenario_table
        rows = []
        for r in range(t.rowCount()):
            role, base = t.cellWidget(r, 1), t.cellWidget(r, 2)
            row = {"name": self._scn_cell_text(r, 0),
                   "role": role.currentData() if role is not None else ROLE_BREACH,
                   "baseline": (base.currentData() or "") if base is not None else ""}
            for col, key in _SCN_PATH_COLS.items():
                row[key] = self._scn_cell_text(r, col)
            rows.append(row)
        return rows

    def _set_table_scenarios(self, rows):
        self._scn_busy = True
        try:
            self.scenario_table.setRowCount(0)
        finally:
            self._scn_busy = False
        rows = [dict(r) for r in (rows or [])]
        for row in rows:
            self._scn_add_row(dict(row, baseline=""))
        # the choices need every baseline name in the lists first
        self._scn_refresh_baselines(want={i: (row.get("baseline") or _NO_BASELINE)
                                          for i, row in enumerate(rows)})

    def _scenario_state(self):
        return {"scenario_rows": self._table_scenarios(),
                "main_baseline": self.main_baseline_combo.currentData() or ""}

    def _apply_scenario_state(self, state):
        if "scenario_rows" in state:
            self._set_table_scenarios(state["scenario_rows"] or [])
        if "main_baseline" in state:
            want = state["main_baseline"] or ""
            for i in range(self.main_baseline_combo.count()):
                if self.main_baseline_combo.itemData(i) == want:
                    self.main_baseline_combo.setCurrentIndex(i)
                    break

    def _main_scenario(self):
        paths = self._raster_paths()
        return {"name": self.main_name_edit.text().strip() or MAIN_NAME,
                "depth": paths["depth"], "velocity": paths["vel"], "arrival": paths["arr"],
                "dv": self._dv_path() or "",
                "baseline": self.main_baseline_combo.currentData() or ""}

    def _scenario_problems(self):
        """Problems with the table rows (for validate_all_inputs)."""
        rows = self._table_scenarios()
        has_rows = bool(clean_rows(rows))
        try:
            scs, problems, _notes = rows_to_scenarios(self._main_scenario(),
                                                      rows if has_rows else [],
                                                      **self._plan_kwargs())
        except (ValueError, TypeError, AttributeError):
            return []
        # v2.9.1: one raster given twice (a breach as its own baseline, depth as D x V)
        return (problems if has_rows else []) + raster_reuse_problems(scs)

    def _plan_kwargs(self):
        """v2.9: the HEC-RAS plan tab's conversion options and cache folder, for
        a plan (.p##.hdf) used directly as a scenario's input."""
        out = {}
        if hasattr(self, "_ras_plan_settings"):
            try:
                out["plan_settings"] = self._ras_plan_settings()
                out["cache_root"] = self._ras_cache_root()
            except (AttributeError, RuntimeError, ValueError):
                pass
        return out

    # ------------------------------------------------------------ combos --
    def _refresh_layer_combos(self, *args):
        layers = [lyr for lyr in QgsProject.instance().mapLayers().values()
                  if isinstance(lyr, QgsVectorLayer) and lyr.isValid()]
        lines = [lyr for lyr in layers if lyr.geometryType() == GEOM_LINE]
        points = [lyr for lyr in layers if lyr.geometryType() == GEOM_POINT]
        polys = [lyr for lyr in layers if lyr.geometryType() == GEOM_POLYGON
                 and "consolidated" not in lyr.name().lower()
                 and "habitable_structures" not in lyr.name().lower()]

        def fill(combo, specials, candidates):
            prev = combo.currentData()
            combo.clear()
            for text, data in specials:
                combo.addItem(text, data)
            for lyr in sorted(candidates, key=lambda x: x.name().lower()):
                combo.addItem(lyr.name(), lyr.id())
            if prev is not None:
                for i in range(combo.count()):
                    if combo.itemData(i) == prev:
                        combo.setCurrentIndex(i)
                        break

        fill(self.outline_combo, [("(none)", _NONE)], polys)
        fill(self.dam_layer_combo, [("(none)", _NONE)], points)
        fill(self.lifeline_combo, [("(none)", _NONE)], points + polys)
        fill(self.boundary_combo, [("(none - check the depth raster edge)", _NONE)], polys)
        if hasattr(self, "parcel_combo"):
            fill(self.parcel_combo, [("(none)", _NONE), ("(auto-detect 'parcel' layer)", _AUTO)],
                 polys)
        if hasattr(self, "centerline_combo"):
            fill(self.centerline_combo, [("(none - trace USGS 3DHP flowlines)", _NONE),
                                         ("(auto-detect centerline layer)", _AUTO)], lines)
        fill(self.road_combo, [("(auto-detect road layer, else download ND roads + traffic)",
                                _AUTO), ("(download ND GIS Hub roads + HPMS traffic)", _DOWNLOAD),
                               ("(none)", _NONE)], lines)
        fill(self.aadt_combo, [("(none)", _NONE), ("(auto-detect)", _AUTO)], lines + points)
        fill(self.rail_combo, [("(download national rail network - FRA/BTS)", _DOWNLOAD),
                               ("(auto-detect rail layer)", _AUTO), ("(none)", _NONE)], lines)
        fill(self.crossing_combo, [("(download FRA grade crossings)", _DOWNLOAD),
                                   ("(auto-detect crossings layer)", _AUTO), ("(none)", _NONE)],
             points)
        if hasattr(self, "_refresh_pop_combo"):
            self._refresh_pop_combo()
        if hasattr(self, "_refresh_parcel_fields290"):
            self._refresh_parcel_fields290()

    def _resolve_layer(self, combo, pattern, geom_types, exclude=None):
        data = combo.currentData()
        if data in (None, _NONE, _DOWNLOAD):
            return None
        project = QgsProject.instance()
        if data == _AUTO:
            for lyr in project.mapLayers().values():
                if (isinstance(lyr, QgsVectorLayer) and lyr.isValid()
                        and lyr.geometryType() in geom_types and pattern.search(lyr.name())
                        and not (exclude and exclude.search(lyr.name()))
                        and not is_plugin_output_name(lyr.name())
                        and not is_downloaded_data_layer(lyr)
                        and "inundated" not in lyr.name().lower()
                        and not _PLUGIN_POI_RE.search(lyr.name())):
                    return lyr
            return None
        return project.mapLayer(data)

    def _classification_button_state(self):
        """2.30.0: the button says WHY it is grey, naming the missing input (preflight.py);
        the same text is on 'Run everything' and in the status line."""
        if not hasattr(self, "run_class_btn"):
            return
        ok = self._inputs_valid_for_main() and not self._busy
        reasons = []
        try:
            from . import preflight as PRE
            reasons = self._run_reasons() if hasattr(self, "_run_reasons") else []
            ok = ok and not reasons
            self.run_class_btn.setToolTip(PRE.tooltip(reasons) if reasons else
                                          "Every input is ready - runs the inventory, the ACER "
                                          "zones, the ND criteria, the report and the EAP "
                                          "outputs")
        except Exception:                                        # noqa - never stop the window
            pass
        self.run_class_btn.setEnabled(ok)
        self.run_class_btn.setStyleSheet(BTN_ACTIVE_STYLE if ok else BTN_INACTIVE_STYLE)

    # ----------------------------------------------------------------- RUN --
    def _on_run_classification(self):
        if self._busy:
            return
        if hasattr(self, "_log_run_reasons"):
            reasons = self._log_run_reasons()
            if reasons:
                from . import preflight as PRE
                QMessageBox.warning(self, "Hazard classification - inputs need fixing",
                                    PRE.tooltip(reasons))
                return
        valid, errors = self.validate_all_inputs()
        if not valid:
            QMessageBox.warning(self, "Input Errors",
                                "\n".join("- {}".format(e) for e in errors))
            return
        self._launch_classification()

    def _launch_classification(self, download_first=False, everything=False):
        """v2.6: the classification runs in a background task (with the
        data-set download first when asked); its project work follows on the
        main thread. Errors are shown, never raised."""
        from .pipeline import run_classification
        from .runner import detach_layers, release_outputs
        self._log(">>> Hazard classification...")
        ok, msg = self._load_rasters()
        if not ok:
            QMessageBox.critical(self, "Hazard Classification Error", msg)
            self._update_status("CLASSIFICATION FAILED: {}".format(msg), False)
            return
        ctx = QgsProject.instance().transformContext()
        cfg = self._classification_config(ctx)
        self._save_settings()
        self._study_area(ctx)            # logs the study area
        # "auto" inputs are resolved here, on the main thread, so the worker
        # never looks at the project (and the re-use check sees the layers)
        from .processing import discover_project_layers
        from .osm_processing import get_osm_candidates
        if cfg.get("point_sources") == "auto":
            found = discover_project_layers()
            cfg["point_sources"] = [(fam, lyr) for fam in ("NSI", "NDGISHUB")
                                    for lyr in found.get(fam, [])]
        if cfg.get("osm_layer") == "auto":
            cfg["osm_layer"] = get_osm_candidates(self.config_output_folder,
                                                  self.config_dam_folder)
        cfg = detach_layers(cfg)         # the worker reads copies of the project layers
        n = release_outputs(self.config_output_folder, data_sets=download_first)
        if n:
            self._log(">>> {} layer(s) of the last run taken out of the project (they are "
                      "written again)".format(n))
        dl = self._download_job() if download_first else None
        sink = []

        def work(progress, log):
            lo = 0.0
            if dl is not None:
                from .datafetch import download_data
                lo = 10.0
                try:
                    download_data(dl["cfg"], dl["clip"], dl["depth"], dl["out"], dl["dam"],
                                  progress=lambda p, m: progress(0.1 * p, m), log=log,
                                  add_layer=lambda path, base: sink.append((path, base)))
                except CancelledByUser:
                    raise
                except Exception as e:
                    # optional step: the run downloads what it needs itself
                    log_message(traceback.format_exc(), MSG_WARNING)
                    log(">>> WARNING: saving the data sets to outputs/Data failed ({}) - "
                        "continuing".format(e))
            return run_classification(
                cfg, progress=lambda p, m: progress(lo + (100.0 - lo) * p / 100.0, m), log=log,
                deferred=True)

        def done(outcome):
            if outcome.cancelled:
                self._update_status("Cancelled by user" if everything else
                                    "Hazard classification cancelled", False)
                self._log(">>> Cancelled by user" if everything else
                          ">>> Hazard classification cancelled by user")
                return
            if outcome.error is not None:
                self._log(outcome.traceback)
                log_message(outcome.traceback, MSG_CRITICAL)
                self._update_status("CLASSIFICATION FAILED: {}".format(outcome.error), False)
                QMessageBox.critical(self, "Hazard Classification Error",
                                     str(outcome.error) or repr(outcome.error))
                return
            res = outcome.result
            try:
                if sink:
                    self._add_downloaded(sink)
                self._finish_deferred(res, "Finishing: layers, map book, report...")
            except CancelledByUser:
                self._update_status("Cancelled by user" if everything else
                                    "Hazard classification cancelled", False)
                self._log(">>> Cancelled by user")
                return
            except Exception as e:
                tb = traceback.format_exc()
                self._log(tb)
                log_message(tb, MSG_CRITICAL)
                self._update_status("CLASSIFICATION FAILED: {}".format(e), False)
                QMessageBox.critical(self, "Hazard Classification Error", str(e) or repr(e))
                return
            self._after_classification(res)
            if everything:
                self._log(">>> Run everything finished")
        self._start_background("Run everything..." if everything else
                               "Running hazard classification...", work, done)

    def _after_classification(self, res):
        self._last_run = res
        if hasattr(self, "_refresh_dam_record"):
            self._refresh_dam_record()                 # v2.10: the new "classified" status
        self.open_report_btn.setEnabled(bool(res.get("report")) and
                                        os.path.isfile(res.get("report") or ""))
        lines = res["lines"]
        self.class_result_text.setPlainText("\n".join(lines))
        for line in lines:
            log_message(line)
        nd, tm11 = res["nd"], res["tm11"]
        self._update_status("Hazard classification: ND {} / TM 11 {}".format(
            nd["cls"], tm11["cls"]), True)
        self._log(">>> Hazard classification finished: ND {}, TM 11 {}".format(
            nd["cls"], tm11["cls"]))
        if hasattr(self, "_populate_review"):
            self._populate_review()
        if hasattr(self, "_populate_facility_review"):
            self._populate_facility_review()
        self._show_results(res)
        notes = res.get("notes") or []
        if notes:
            QMessageBox.warning(self, "Hazard Classification",
                                "<br>".join(html.escape(n) for n in notes))
        try:
            self.tab_widget.setCurrentIndex(self.tab_widget.indexOf(self._class_tab))
        except Exception:
            pass

    def _scenarios(self):
        """The Setup tab rasters as the main breach scenario, then the
        table's scenarios and baselines."""
        scenarios, _problems, notes = rows_to_scenarios(self._main_scenario(),
                                                        self._table_scenarios(),
                                                        **self._plan_kwargs())
        for n in notes:
            self._log(">>> " + n)
        return scenarios

    def _classification_config(self, ctx):
        from .pipeline import config
        rail = self._resolve_layer(self.rail_combo, _RAIL_RE, (GEOM_LINE,))
        cross = self._resolve_layer(self.crossing_combo, _CROSS_RE, (GEOM_POINT,))
        road = self._resolve_layer(self.road_combo, _ROAD_RE, (GEOM_LINE,), exclude=_RAIL_RE)
        aadt = self._resolve_layer(self.aadt_combo, _ROAD_RE, (GEOM_LINE, GEOM_POINT),
                                   exclude=_RAIL_RE)

        def picked(combo):
            d = combo.currentData()
            return QgsProject.instance().mapLayer(d) if d not in (None, _NONE) else None
        extra = picked(self.outline_combo)
        parcels = self._resolve_layer(self.parcel_combo, _PARCEL_RE, (GEOM_POLYGON,),
                                      exclude=_PARCEL_EXCLUDE)
        center = self._resolve_layer(self.centerline_combo, _CENTERLINE_RE, (GEOM_LINE,),
                                     exclude=_CENTER_EXCLUDE)
        data = self._data_cfg()             # download choices (Data & Output tab)
        for k in ("data_area", "flood_buffer_ft", "add_to_project"):
            data.pop(k, None)
        return config(
            output_folder=self.config_output_folder, dam_folder=self.config_dam_folder,
            scenarios=self._scenarios(), extent_buffer_ft=float(self.extent_spin.value()),
            keep_dry=self.keep_dry_check.isChecked(),
            road_layer=road, aadt_layer=aadt, rail_layer=rail, crossing_layer=cross,
            extra_outline_layer=extra,
            one_d=self.model_combo.currentData() == "1d",
            judgment_as_probable=self.judgment_check.isChecked(),
            nd_judgment_as_probable=self.nd_judgment_check.isChecked(),
            persons_per_dwelling=int(self.ppd_spin.value()),
            min_area_ft2=float(self.min_area_spin.value()),
            occupancy=float(self.occ_spin.value()), speed_mph=float(self.speed_spin.value()),
            near_ft=float(self.near_spin.value()),
            borderline_higher=self.borderline_check.isChecked(),
            lol=self.lol_check.isChecked(), warning_issued_hr=float(self.warn_spin.value()),
            understanding=self.understanding_combo.currentData(),
            severity_method=self.severity_combo.currentData(),
            population=self.population_combo.currentData(),
            access=self.access_check.isChecked(), access_ft=float(self.access_spin.value()),
            access_as_medium=self.access_medium_check.isChecked(),
            report_pdf=self.report_check.isChecked(), report_pages=int(self.pages_spin.value()),
            spreadsheets=self.spreadsheets_check.isChecked(),
            dam_layer=picked(self.dam_layer_combo),
            subject_dam_nid=self.subject_nid_edit.text().strip(),
            lifeline_layer=picked(self.lifeline_combo),
            lifeline_as_medium=self.lifeline_medium_check.isChecked(),
            model_boundary_layer=picked(self.boundary_combo),
            eap=self.eap_check.isChecked(),
            parcel_layer=parcels, parcel_max_acres=float(self.parcel_acres_spin.value()),
            eap_list=self.eap_list_check.isChecked(),
            eap_mapbook=self.eap_mapbook_check.isChecked(),
            mapbook_scale=int(self.mapbook_scale_combo.currentData() or
                              MAPBOOK_DEFAULT_SCALE),
            eap_travel=self.eap_travel_check.isChecked(), centerline_layer=center,
            travel_station_mi=float(self.station_spin.value()),
            compare_last_run=self.compare_check.isChecked(),
            zone_rasters=self.zone_rasters_check.isChecked(),
            sampled_cells=self.sampled_cells_check.isChecked(),
            sample_area=self.sample_area_combo.currentData() or "area",
            reuse_evaluation=self.reuse_check.isChecked(),
            damage_curves_file=self.damage_curves_edit.text().strip(),
            hcom_files=self.hcom_edit.text().strip(),
            **self._extras_config(), **self._extras290_config(),
            **(self._suite_config() if hasattr(self, "_suite_config") else {}),
            settings_profile=self._settings_profile_for_cfg(), **data)

    def _show_results(self, res):
        """Result panel: the classes on top, then every criterion and every
        judgment item; double-click zooms to what is behind a row."""
        nd, tm11 = res["nd"], res["tm11"]
        gov = "" if len([r for r in res["results"].values() if r.get("nd")]) <= 1 else \
            "  (governing scenario: {})".format(res["governing"])
        veh = "" if vehicle_curve_available() else "  - motorists not counted"
        self.result_label.setText("ND: {}    ACER TM 11: {} (lives in jeopardy {:.1f}){}{}"
                                  .format(nd["cls"].upper(), tm11["cls"].upper(), tm11["lij"],
                                          gov, veh))
        items = res.get("result_items") or []
        self._result_rows = items
        t = self.result_table
        t.setRowCount(0)
        t.setRowCount(len(items))
        for r, it in enumerate(items):
            zoom = " \u2316" if it.get("geoms") else ""
            for c, v in enumerate((it["section"], it["item"] + zoom, it["found"],
                                   it["result"])):
                t.setItem(r, c, QTableWidgetItem(v))
        try:
            t.resizeColumnsToContents()
        except Exception:
            pass
        self._show_scenario_comparison(res)

    def _show_scenario_comparison(self, res):
        """Every breach scenario side by side (only when there is more than
        one, or a baseline was used)."""
        t = self.scenario_result_table
        rows = comparison_rows(res.get("results"), res.get("governing"))
        show = len(rows) > 1 or any(r["Baseline"] != "-" for r in rows)
        t.setVisible(bool(show))
        t.setRowCount(0)
        if not show:
            return
        t.setRowCount(len(rows))
        for i, row in enumerate(rows):
            for c, col in enumerate(COMPARE_COLS):
                text = row[col] + (" (governs)" if col == "Scenario" and row["governs"] and
                                   len(rows) > 1 else "")
                t.setItem(i, c, QTableWidgetItem(text))
        try:
            t.resizeColumnsToContents()
        except Exception:
            pass

    def _result_zoom(self, row, *args):
        rows = getattr(self, "_result_rows", None) or []
        if row is None or row < 0 or row >= len(rows) or not rows[row].get("geoms"):
            return False
        geoms = rows[row]["geoms"]
        try:
            from qgis.utils import iface
            canvas = iface.mapCanvas()
        except Exception:
            canvas = None
        if canvas is None:
            QMessageBox.information(self, "Hazard Classification",
                                    "The map canvas is not available.")
            return False
        from qgis.core import QgsCoordinateTransform, QgsRectangle
        from .helpers import buffer_distance_in_layer_units
        crs = self._last_run["crs"]
        boxes = [g.boundingBox() for g in geoms]
        bb = QgsRectangle(min(b.xMinimum() for b in boxes), min(b.yMinimum() for b in boxes),
                          max(b.xMaximum() for b in boxes), max(b.yMaximum() for b in boxes))
        pad = max(bb.width() * 0.2, bb.height() * 0.2,
                  buffer_distance_in_layer_units(200.0, crs))
        bb = QgsRectangle(bb.xMinimum() - pad, bb.yMinimum() - pad, bb.xMaximum() + pad,
                          bb.yMaximum() + pad)
        try:
            xf = QgsCoordinateTransform(crs, canvas.mapSettings().destinationCrs(),
                                        QgsProject.instance().transformContext())
            canvas.setExtent(bb if xf.isShortCircuited() else xf.transformBoundingBox(bb))
            canvas.refresh()
            try:
                canvas.flashGeometries(geoms[:50], crs)
            except Exception:
                pass
        except Exception as e:
            log_message("Zoom failed: {}".format(e))
            return False
        return True

    def _open_path(self, path):
        from qgis.PyQt.QtCore import QUrl
        from qgis.PyQt.QtGui import QDesktopServices
        if not path or not os.path.exists(path):
            QMessageBox.information(self, "Open", "Not found: {}".format(path or "-"))
            return False
        return bool(QDesktopServices.openUrl(QUrl.fromLocalFile(path)))

    def _run_watch(self, *args):
        """v2.7: hazard-creep watch over a folder of dams (watch.py). Runs on
        the main thread with a progress bar, like the other whole-folder steps."""
        if self._busy:
            return
        start = os.path.dirname(self.config_dam_folder.rstrip("/\\")) if getattr(
            self, "config_dam_folder", "") else ""
        parent = QFileDialog.getExistingDirectory(
            self, "Folder containing the dam folders", start or "")
        if not parent:
            return
        from . import watch as WT
        self._set_busy(True)
        self._progress = self._make_progress("Hazard creep watch...")
        info = {}
        try:
            rows, paths = WT.run_watch(parent, progress=self._progress_tick, log=self._log,
                                       workers=int(self.watch_workers_spin.value()), info=info)
        except CancelledByUser:
            self._finish_progress()
            self._set_busy(False)
            self._update_status("Creep watch cancelled", False)
            return
        except Exception as e:
            self._finish_progress()
            self._set_busy(False)
            log_message(traceback.format_exc(), MSG_CRITICAL)
            QMessageBox.critical(self, "Creep watch", str(e) or repr(e))
            return
        self._finish_progress()
        self._set_busy(False)
        text = WT.alerts_text(rows)
        if info.get("text"):
            self._log(">>> Creep watch ran: " + info["text"])
        self._log(">>> Creep watch: {} - {}".format(text, paths["html"]))
        self._update_status("Creep watch: " + text, True)
        QMessageBox.information(self, "Creep watch", "{}\n\n{}".format(text, paths["html"]))
        self._open_path(paths["html"])

    # ------------------------------- 2.30.0: the EAP map, the letters, the history --
    def _build_eap_map(self, *args):
        """The one-page EAP map layout, filled from this dam folder (mapbook.build_layout).
        Needs no finished run: whatever the last run left in the outputs folder is drawn."""
        if self._busy:
            return
        try:
            from qgis.PyQt.QtCore import QUrl
            from qgis.PyQt.QtGui import QDesktopServices
            from qgis.core import QgsRasterLayer
            from .constants import SUBDIR_EAP, GRP_EAP
            from .helpers import plugin_group, dam_display_name
            from . import eap as EAP
            from . import mapbook as MB
            from .autodetect import find_rasters
            folder = self.config_dam_folder
            if not folder or not os.path.isdir(folder):
                QMessageBox.information(self, "EAP map", "Choose the dam folder first "
                                                         "(Setup & Run, step 1).")
                return
            sources = EAP.layout_sources(folder, self.config_output_folder,
                                         depth=self.depth_edit.text().strip(),
                                         find=find_rasters)
            ok, why = EAP.layout_ready(sources)
            if not ok:
                QMessageBox.information(self, "EAP map", why)
                return
            d_eap = os.path.join(self.config_output_folder or folder, SUBDIR_EAP)
            os.makedirs(d_eap, exist_ok=True)
            res = getattr(self, "_last_run", None) or {}
            card = res.get("card") or {}
            dam = res.get("dam_name") or dam_display_name(folder)
            lat = None
            clip = res.get("clip")
            crs = res.get("crs")
            if crs is None:
                lyr = QgsRasterLayer(sources.get("layers", {}).get("bands")
                                     or sources.get("depth"), "flood")
                if not lyr.isValid():
                    QMessageBox.information(self, "EAP map",
                                            "The inundation raster could not be opened.")
                    return
                crs = lyr.crs()
            if clip is not None:
                lat = getattr(clip, "lat", None)
            self._log(">>> EAP map layout...")
            pdf, geo, lines = MB.build_layout(
                d_eap, dam, sources, crs, lat, QgsProject.instance().transformContext(),
                hazard_class=card.get("cls") or sources.get("class") or "",
                scenario=res.get("governing") or "",
                add_to_project=True, group=plugin_group(dam, GRP_EAP))
            for line in lines:
                self._log("    " + line)
            self._update_status("EAP map: {}".format(
                os.path.basename(pdf) if pdf else "not written - see the log"), bool(pdf))
            if pdf:
                QMessageBox.information(self, "EAP map", "\n".join(lines))
                QDesktopServices.openUrl(QUrl.fromLocalFile(pdf))
            else:
                QMessageBox.warning(self, "EAP map", "\n".join(lines))
        except Exception as e:                                   # noqa - never raise in a slot
            log_message("EAP map layout failed: {}".format(e))
            self._log(">>> EAP map not made: {}".format(e))
            QMessageBox.warning(self, "EAP map", "The map could not be made: {}".format(e))

    def _open_letters(self, *args):
        """The dam-owner letter and the county notification as editable drafts."""
        try:
            from . import letterwindow as LW
            from . import history as HIS
            res = getattr(self, "_last_run", None)
            if not res:
                QMessageBox.information(self, "Letters",
                                        "Run the Hazard Classification first - the drafts are "
                                        "built from its result.")
                return
            nid = ""
            if hasattr(self, "subject_nid_edit"):
                nid = self.subject_nid_edit.text().strip()
            LW.open_letters(self, res, self.config_output_folder or self.config_dam_folder,
                            dam_name=res.get("dam_name") or "", nid_id=nid,
                            history=HIS.read_entries(self.config_dam_folder))
        except Exception as e:                                   # noqa - never raise in a slot
            log_message("Letter drafts failed: {}".format(e))
            QMessageBox.warning(self, "Letters", "The drafts could not be opened: {}".format(e))

    def _show_history(self, *args):
        """Every classification run recorded for this dam, newest last."""
        try:
            from . import history as HIS
            folder = self.config_dam_folder
            entries = HIS.read_entries(folder)
            if not entries:
                QMessageBox.information(
                    self, "Class history",
                    "No classification run is recorded for this dam yet. Every run from "
                    "2.30.0 on is added to {} in the dam folder.".format(HIS.HISTORY_FILE))
                return
            lines = HIS.history_lines(entries)
            for line in lines:
                self._log("    " + line)
            csv_path = ""
            try:
                csv_path = HIS.write_history_csv(
                    os.path.join(self.config_output_folder or folder, "QA",
                                 "Classification_History.csv"), entries)
            except Exception as e:                               # noqa - the table is extra
                log_message("Class history CSV not written: {}".format(e))
            QMessageBox.information(self, "Class history", "\n".join(lines) + (
                "\n\nTable: {}".format(csv_path) if csv_path else ""))
        except Exception as e:                                   # noqa - never raise in a slot
            log_message("Class history failed: {}".format(e))
            QMessageBox.warning(self, "Class history",
                                "The history could not be read: {}".format(e))

    def _make_submittal(self, *args):
        """v2.7: bundle the last run's files for the reviewer (submittal.py)."""
        if self._busy:
            return
        out = self.config_output_folder
        if not out or not os.path.isdir(out):
            QMessageBox.information(self, "Submittal package",
                                    "Run the Hazard Classification first.")
            return
        from . import submittal as SUB
        answer = QMessageBox.question(
            self, "Submittal package",
            "Make the submittal package from the last run in\n{}\n\nAlso include the GIS "
            "layers (GeoPackages and zone rasters)? They make the package much larger."
            .format(out), MB_YES | MB_NO | MB_CANCEL)
        if answer == MB_CANCEL:
            return
        gis = answer == MB_YES
        # v2.9: with the data the run read, the reviewer (or you, later) can reproduce it
        want_data = QMessageBox.question(
            self, "Submittal package",
            "Also include the input data the run read (the layers and files listed in "
            "QA/Run_Settings.json), so the run can be reproduced from the package?\n\n"
            "Can be large. Data that is only on the internet is not copied.",
            MB_YES | MB_NO) == MB_YES
        want_rasters = False
        if want_data:
            want_rasters = QMessageBox.question(
                self, "Submittal package",
                "Include the flood rasters too? They are usually the largest files.",
                MB_YES | MB_NO) == MB_YES
        dam = getattr(self, "_last_run", None) or {}
        profile = self._settings_profile_for_cfg() \
            if hasattr(self, "_settings_profile_for_cfg") else None

        def work(progress, log):
            progress(10, "Gathering the files...")
            return SUB.build(out, dam_name=dam.get("dam_name"), zip_it=True, include_gis=gis,
                             profile=profile or None, include_data=want_data,
                             include_rasters=want_rasters)

        def done(outcome):
            if outcome.cancelled:
                return
            if outcome.error is not None:
                self._log(outcome.traceback)
                QMessageBox.warning(self, "Submittal package", str(outcome.error))
                self._update_status("Submittal package failed: {}".format(outcome.error), False)
                return
            res = outcome.result
            self._log(">>> Submittal package: {} ({} files){}".format(
                res["folder"], len(res["files"]),
                " - not in it: " + "; ".join(res["missing"]) if res["missing"] else ""))
            self._update_status("Submittal package made: {}".format(
                os.path.basename(res["folder"])), True)
            text = "Submittal package made:\n{}\n{}\n\n{} files, with MANIFEST.csv and a " \
                "cover-letter template.".format(res["folder"], res["zip"] or "",
                                                  len(res["files"]))
            if res["missing"]:
                text += "\n\nNot in it (the last run did not write them):\n- " + \
                    "\n- ".join(res["missing"])
            QMessageBox.information(self, "Submittal package", text)
            self._open_path(res["folder"])
        self._start_background("Making the submittal package...", work, done,
                               project_bound=False)

    def _open_last_report(self, *args):
        res = getattr(self, "_last_run", None) or {}
        self._open_path(res.get("report"))

    def _open_outputs_folder(self, *args):
        self._open_path(self.config_output_folder)
