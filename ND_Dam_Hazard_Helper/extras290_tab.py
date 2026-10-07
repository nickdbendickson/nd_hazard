# -*- coding: utf-8 -*-
"""v2.9 UI (mixed into the main dialog, built into the Hazard Classification
tab): the optional v2.9 choices - mobile-home typing, speed, report imagery,
consequence sensitivity, EAP extras, parcel habitability hints - and the row of
tool buttons (run estimate, settings profiles, reproduce a package, portfolio
dashboard, form template). The work itself is in structures.py, fastsample.py,
sensitivity.py, temporal.py, webexport.py, parcelhints.py, estimate.py,
settings.py, reproduce.py, portfolio.py and formfill.py; this file only
collects the choices and starts the tools.

Every number shown here is a SCREENING value chosen by the engineer. The
defaults are starting values, not requirements of REG_05.2026 or ACER TM 11.

Dialog contract (what the host dialog provides): _log(msg), _start_background(
title, work, done, project_bound=False), _busy, config_output_folder, parcel_combo,
_classification_config(ctx), _current_state(), _apply_state(state),
_SETTING_WIDGETS. The lead adds to the dialog:
  * _build_extras290_group() into the Hazard Classification tab;
  * **self._extras290_config() into the classification cfg, and
    self._extras290_problems() into the input validation;
  * SETTING_WIDGETS_290 to _SETTING_WIDGETS, BUSY_WIDGETS_290 to _set_busy;
  * _refresh_parcel_fields290() from _refresh_layer_combos and when parcel_combo
    changes; cfg["settings_profile"] = self._settings_profile_for_cfg().
"""

import datetime
import functools
import math
import os
import traceback

from qgis.PyQt.QtWidgets import (
    QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QGroupBox, QFormLayout,
    QSpinBox, QDoubleSpinBox, QCheckBox, QComboBox, QWidget, QMessageBox, QFileDialog,
    QLineEdit, QInputDialog,
)
from qgis.core import QgsProject, QgsVectorLayer

from .compat import GEOM_POLYGON, MSG_CRITICAL, MSG_WARNING, MB_YES, MB_NO, MB_CANCEL
from .helpers import log_message

# literal starting values (used when pipeline.DEFAULTS does not carry the key yet)
_LITERAL = {
    "mobile_shape": True, "mobile_max_width_ft": 18.0, "mobile_min_ratio": 2.5,
    "mobile_min_length_ft": 30.0, "mobile_max_length_ft": 90.0, "mobile_lot_units": True,
    "download_trailer_parks": True,
    "fast_sampling": False, "input_overviews": False, "zone_overviews": True,
    "report_imagery": True,
    "dv_spreadsheet": True, "dv_template": "",
    "conseq_sensitivity": False, "conseq_sens_warning_hr": "-0.5, 0.5, 1.0",
    "conseq_sens_ppd": "2.0, 2.5, 3.0", "conseq_sens_population": True,
    "conseq_sens_severity": True,
    "evac_routes": True, "temporal_animation": False, "temporal_breach_start": "",
    "temporal_step_min": 15, "web_export": False,
    "parcel_hints": False, "parcel_landuse_field": "", "parcel_improvement_field": "",
    "parcel_ref_field": "", "parcel_hint_vacant_keywords": "",
    "parcel_hint_nonres_keywords": "", "parcel_hint_res_keywords": "",
    "parcel_hint_empty_is_zero": True,
}
_TIME_FORMATS = ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S")


def _default(key):
    """Starting value of a cfg key: pipeline.DEFAULTS when it has the key, else
    the literal above."""
    try:
        from .pipeline import DEFAULTS
        return DEFAULTS.get(key, _LITERAL[key])
    except Exception:
        return _LITERAL[key]


def parse_start_time(text):
    """datetime of "YYYY-MM-DD HH:MM[:SS]" (a "T" instead of the blank is
    accepted); None when the text is not in that form."""
    t = str(text or "").strip().replace("T", " ")
    for fmt in _TIME_FORMATS:
        try:
            return datetime.datetime.strptime(t, fmt)
        except ValueError:
            continue
    return None


def _number_tokens(text):
    """([floats], [bad tokens]) of a comma / semicolon / blank separated list."""
    good, bad = [], []
    for tok in str(text or "").replace(";", " ").replace(",", " ").split():
        try:
            v = float(tok)
        except ValueError:
            bad.append(tok)
            continue
        if math.isfinite(v):
            good.append(v)
        else:
            bad.append(tok)
    return good, bad


def _guarded(title):
    """Button handlers never raise into Qt: an error is logged and shown."""
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(self, *args):
            try:
                return fn(self)
            except Exception as e:                       # noqa - last line of defence
                tb = traceback.format_exc()
                log_message(tb, MSG_CRITICAL)
                try:
                    self._log(tb)
                except Exception:
                    pass
                try:
                    QMessageBox.critical(self, title, str(e) or repr(e))
                except Exception:
                    pass
                return None
        return wrapper
    return deco


class Extras290TabMixin(object):
    """Methods for the 'Version 2.9 options' group of the Hazard Classification
    tab and its tool buttons."""

    # every button (the lead adds these to _set_busy)
    BUSY_WIDGETS_290 = (
        "estimate_btn", "profile_save_btn", "profile_load_btn", "profile_compare_btn",
        "reproduce_btn", "portfolio_btn", "formfill_btn", "formfill_keys_btn",
        "dv_template_btn",
    )
    # every persisted widget (the lead adds these to _SETTING_WIDGETS); the field
    # combos come last: their items depend on the parcel layer
    SETTING_WIDGETS_290 = (
        "mobile_shape_check", "mobile_max_width_spin", "mobile_min_ratio_spin",
        "mobile_min_length_spin", "mobile_max_length_spin", "mobile_lot_check",
        "trailer_parks_check",
        "fast_sampling_check", "input_overviews_check", "zone_overviews_check",
        "report_imagery_check", "dv_spreadsheet_check", "dv_template_edit",
        "conseq_sens_check", "conseq_warning_edit", "conseq_ppd_edit", "conseq_pop_check",
        "conseq_severity_check",
        "evac_routes_check", "temporal_check", "temporal_start_edit", "temporal_step_spin",
        "web_export_check",
        "parcel_hints_check", "parcel_vacant_kw_edit", "parcel_nonres_kw_edit",
        "parcel_res_kw_edit", "parcel_empty_zero_check",
        "parcel_landuse_combo", "parcel_improvement_combo", "parcel_ref_combo",
    )

    # ------------------------------------------------------------------ UI --
    def _build_extras290_group(self):
        from . import parcelhints as PH
        grp = QGroupBox("7. Version 2.9 options (advanced - all optional)")
        lay = QVBoxLayout()
        note = QLabel(
            "Optional v2.9 choices. Every number is the engineer's own screening value: the "
            "defaults are starting values, not requirements of REG_05.2026 or ACER TM 11. "
            "Nothing here is needed for a normal run.")
        note.setWordWrap(True)
        lay.addWidget(note)
        form = QFormLayout()

        # -- mobile homes
        self.mobile_shape_check = QCheckBox("Type mobile homes from the footprint shape")
        self.mobile_shape_check.setChecked(bool(_default("mobile_shape")))
        self.mobile_shape_check.setToolTip(
            "Only when no data source, park or address unit says anything about the type: a "
            "dwelling-like outline whose oriented bounding box is narrow, long and inside the "
            "length range below is typed as a single-wide manufactured (mobile) home, which "
            "uses the mobile-home stability curve. Explicit house words in the attributes win "
            "over the shape (the structure is flagged instead). The numbers below are your "
            "screening choices, not requirements of REG_05.2026 or TM 11. The structure is "
            "listed for review.")
        form.addRow("", self.mobile_shape_check)
        self.mobile_max_width_spin = self._spin290(1.0, 100.0, 1.0, "mobile_max_width_ft", " ft")
        self.mobile_max_width_spin.setToolTip(
            "Widest outline (smaller side of the oriented bounding box) still read as a "
            "single-wide manufactured home. Screening value of yours; default 18 ft (about "
            "the transportable width).")
        form.addRow("  Mobile home - widest:", self.mobile_max_width_spin)
        self.mobile_min_ratio_spin = self._spin290(1.0, 20.0, 0.1, "mobile_min_ratio", " : 1")
        self.mobile_min_ratio_spin.setToolTip(
            "Smallest length-to-width ratio of the outline. Screening value of yours; "
            "default 2.5.")
        form.addRow("  Mobile home - length / width at least:", self.mobile_min_ratio_spin)
        self.mobile_min_length_spin = self._spin290(1.0, 500.0, 5.0, "mobile_min_length_ft",
                                                    " ft")
        self.mobile_min_length_spin.setToolTip(
            "Shortest outline length (longer side) read as a single-wide. Screening value of "
            "yours; default 30 ft.")
        form.addRow("  Mobile home - length from:", self.mobile_min_length_spin)
        self.mobile_max_length_spin = self._spin290(1.0, 500.0, 5.0, "mobile_max_length_ft",
                                                    " ft")
        self.mobile_max_length_spin.setToolTip(
            "Longest outline length read as a single-wide. Screening value of yours; default "
            "90 ft. Must not be shorter than the length above.")
        form.addRow("  Mobile home - length to:", self.mobile_max_length_spin)
        self.mobile_lot_check = QCheckBox("Address units Lot/Space/Trlr mean a mobile-home "
                                          "park")
        self.mobile_lot_check.setChecked(bool(_default("mobile_lot_units")))
        self.mobile_lot_check.setToolTip(
            "An ND GIS Hub (NENA) address point whose unit reads like 'Lot 12', 'Trlr 4' or "
            "'Space 7' is taken as evidence of a lot in a mobile-home park. Park / lot "
            "evidence never types an occupied non-residential building (park office, club "
            "house). Screening rule of yours.")
        form.addRow("", self.mobile_lot_check)
        self.trailer_parks_check = QCheckBox("Use OpenStreetMap mobile-home parks (internet)")
        self.trailer_parks_check.setChecked(bool(_default("download_trailer_parks")))
        self.trailer_parks_check.setToolTip(
            "Downloads the OpenStreetMap areas tagged as mobile-home / trailer parks; a "
            "building inside one is read as park evidence. Needs internet access (skipped "
            "offline or when the offline pack answers). Volunteer-mapped data: check them.")
        form.addRow("", self.trailer_parks_check)

        # -- speed
        self.fast_sampling_check = QCheckBox("Fast cell selection for the structures (no "
                                             "GEOS buffers)")
        self.fast_sampling_check.setChecked(bool(_default("fast_sampling")))
        self.fast_sampling_check.setToolTip(
            "Selects the raster cells of each structure's evaluation area with exact "
            "distances (numpy) instead of GEOS buffers and rasterising - much faster for a "
            "town with thousands of structures. The results can differ from the standard "
            "path at the arc-approximation level: GEOS replaces the arcs of a buffer by "
            "chords, so a cell in the gap between arc and chord is counted here and not "
            "there. Off by default; the class rarely changes, but compare once before "
            "relying on it (see the fastsample.py notes).")
        form.addRow("", self.fast_sampling_check)
        self.input_overviews_check = QCheckBox("Build pyramids (.ovr) for large input "
                                               "rasters")
        self.input_overviews_check.setChecked(bool(_default("input_overviews")))
        self.input_overviews_check.setToolTip(
            "Builds .ovr overview files next to large depth / velocity / arrival rasters "
            "that have none, so QGIS draws them quickly when zoomed out. This writes NEW "
            "files in the data folder (the rasters themselves are not touched). It changes "
            "no result: the plugin reads every value at full resolution.")
        form.addRow("", self.input_overviews_check)
        self.zone_overviews_check = QCheckBox("Build pyramids for the ACER zone rasters")
        self.zone_overviews_check.setChecked(bool(_default("zone_overviews")))
        self.zone_overviews_check.setToolTip(
            "Adds overviews inside the Hazard_*.tif zone rasters the run writes, for fast "
            "drawing. Drawing only - no result changes.")
        form.addRow("", self.zone_overviews_check)

        # -- maps
        self.report_imagery_check = QCheckBox("Aerial imagery under the maps of the PDF "
                                              "report and EAP map book")
        self.report_imagery_check.setChecked(bool(_default("report_imagery")))
        self.report_imagery_check.setToolTip(
            "Puts the aerial imagery layer (the one the Review tab uses) under every map of "
            "the PDF report and of the EAP map book. Skipped when offline or when the "
            "imagery service does not answer - the maps are then made without it. A picture "
            "for looking at; no result uses it.")
        form.addRow("", self.report_imagery_check)
        # v2.20.0: the structures on a copy of the depth-velocity spreadsheet
        self.dv_spreadsheet_check = QCheckBox("Plot the structures on a copy of the "
                                              "depth-velocity spreadsheet and in the report")
        self.dv_spreadsheet_check.setChecked(bool(_default("dv_spreadsheet")))
        self.dv_spreadsheet_check.setToolTip(
            "After the classification, every flooded habitable structure (the report's "
            "structures table) is written into a COPY of the hazard spreadsheet and plotted on "
            "its first tab, on the Houses on Foundation and Mobile Homes charts. The copy is "
            "saved in outputs/Classification as <template>_<dam>_<date>.xlsx; the template "
            "is never changed. The same charts go into the PDF report.")
        form.addRow("", self.dv_spreadsheet_check)
        self.dv_template_edit = QLineEdit(str(_default("dv_template") or ""))
        self.dv_template_edit.setPlaceholderText("empty: the spreadsheet that comes with the "
                                                 "plugin")
        self.dv_template_edit.setToolTip(
            "Your own copy of the hazard spreadsheet (.xlsx) to fill instead of the one that "
            "comes with the plugin. Its first tab must keep the 'Houses on Foundation', "
            "'Mobile Homes' and 'QGIS csv Export' blocks and their two charts.")
        self.dv_template_btn = QPushButton("...")
        self.dv_template_btn.setMaximumWidth(32)
        self.dv_template_btn.clicked.connect(self._pick_dv_template290)
        box = QWidget()
        row = QHBoxLayout(box)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(self.dv_template_edit)
        row.addWidget(self.dv_template_btn)
        form.addRow("Spreadsheet template:", box)

        # -- consequence sensitivity
        self.conseq_sens_check = QCheckBox("Consequence sensitivity: re-estimate the lives in "
                                           "jeopardy under other assumptions")
        self.conseq_sens_check.setChecked(bool(_default("conseq_sensitivity")))
        self.conseq_sens_check.setToolTip(
            "Re-runs the population-at-risk / lives-in-jeopardy estimate with other warning "
            "times, persons per dwelling, day / night population and severity method, and "
            "reports how far the answer moves. The result is a RANGE, not a prediction, and "
            "it changes no class. Writes a table to the QA folder. The values are your "
            "screening choices.")
        form.addRow("", self.conseq_sens_check)
        self.conseq_warning_edit = QLineEdit()
        self.conseq_warning_edit.setText(str(_default("conseq_sens_warning_hr")))
        self.conseq_warning_edit.setToolTip(
            "Shifts of the warning time in hours, comma-separated: negative = warned earlier "
            "than the setting above, positive = later. Example: -0.5, 0.5, 1.0. Screening "
            "values of yours.")
        form.addRow("  Warning time shifts (hours):", self.conseq_warning_edit)
        self.conseq_ppd_edit = QLineEdit()
        self.conseq_ppd_edit.setText(str(_default("conseq_sens_ppd")))
        self.conseq_ppd_edit.setToolTip(
            "Persons per dwelling to try, comma-separated positive numbers. Example: 2.0, "
            "2.5, 3.0. Screening values of yours.")
        form.addRow("  Persons per dwelling:", self.conseq_ppd_edit)
        self.conseq_pop_check = QCheckBox("Also try the other time of day (day / night "
                                          "population)")
        self.conseq_pop_check.setChecked(bool(_default("conseq_sens_population")))
        self.conseq_pop_check.setToolTip(
            "Also re-estimates with the other time of day (day instead of night population, "
            "or the reverse). Part of the range; changes no class.")
        form.addRow("  ", self.conseq_pop_check)
        self.conseq_severity_check = QCheckBox("Also try the other severity method")
        self.conseq_severity_check.setChecked(bool(_default("conseq_sens_severity")))
        self.conseq_severity_check.setToolTip(
            "Also re-estimates with the other depth-velocity severity method (Graham 1999 "
            "or RCEM bands). Part of the range; changes no class.")
        form.addRow("  ", self.conseq_severity_check)

        # -- EAP extras
        self.evac_routes_check = QCheckBox("Draw the evacuation routes and road closures "
                                           "(EAP layers)")
        self.evac_routes_check.setChecked(bool(_default("evac_routes")))
        self.evac_routes_check.setToolTip(
            "Keeps the routes of the evacuation timing (needs that analysis above) as EAP "
            "map layers. The route drawn for a structure is the LATEST-DEPARTURE route - the "
            "one that leaves the most time - not necessarily the shortest.")
        form.addRow("", self.evac_routes_check)
        self.temporal_check = QCheckBox("Play the flood arrival in the QGIS Temporal "
                                        "Controller")
        self.temporal_check.setChecked(bool(_default("temporal_animation")))
        self.temporal_check.setToolTip(
            "Sets the EAP arrival contours, notification list and road closures up for the "
            "QGIS Temporal Controller animation: each feature appears at its arrival time "
            "after breach start. The raster arrival bands stay static.")
        form.addRow("", self.temporal_check)
        self.temporal_start_edit = QLineEdit()
        self.temporal_start_edit.setText(str(_default("temporal_breach_start")))
        self.temporal_start_edit.setPlaceholderText("YYYY-MM-DD HH:MM")
        self.temporal_start_edit.setToolTip(
            "NOMINAL display anchor of the animation clock, NOT the real breach time. Empty "
            "= the nominal 2000-01-01 00:00, so the clock reads the elapsed time (01-01 "
            "02:30 = 2 h 30 min after breach start). Format YYYY-MM-DD HH:MM.")
        form.addRow("  Animation clock starts at:", self.temporal_start_edit)
        self.temporal_step_spin = QSpinBox()
        self.temporal_step_spin.setRange(1, 1440)
        self.temporal_step_spin.setValue(int(_default("temporal_step_min")))
        self.temporal_step_spin.setSuffix(" min")
        self.temporal_step_spin.setToolTip(
            "Length of one animation frame. A display choice only - no result depends on it.")
        form.addRow("  Animation frame step:", self.temporal_step_spin)
        self.web_export_check = QCheckBox("KMZ / GeoJSON copies of the EAP layers for Google "
                                          "Earth and web maps (EAP/Web)")
        self.web_export_check.setChecked(bool(_default("web_export")))
        self.web_export_check.setToolTip(
            "Writes a GeoJSON (WGS 84) and a KMZ copy of each EAP layer, plus one KMZ with "
            "every layer, to the EAP/Web folder. Copies only - the EAP layers do not change.")
        form.addRow("", self.web_export_check)

        # -- parcel habitability hints
        self.parcel_hints_check = QCheckBox("Parcel habitability hints (from the parcel layer "
                                            "of the future-development screening)")
        self.parcel_hints_check.setChecked(bool(_default("parcel_hints")))
        self.parcel_hints_check.setToolTip(
            "A structure whose parcel has no building value, or a vacant / agricultural / "
            "commercial land use, gets a short hint in the Review tab list ('check "
            "habitability'). It is only a hint: it never changes a class, a category or the "
            "persons. Needs the parcel layer chosen in the Data tab and the fields below "
            "(suggested by name - you confirm them). Keyword lists are your screening "
            "choices.")
        form.addRow("", self.parcel_hints_check)
        self.parcel_landuse_combo = QComboBox()
        self.parcel_landuse_combo.setToolTip(
            "Text field with the land use / property class. Suggested by name from the "
            "parcel layer; change it if the suggestion is wrong.")
        form.addRow("  Land-use field:", self.parcel_landuse_combo)
        self.parcel_improvement_combo = QComboBox()
        self.parcel_improvement_combo.setToolTip(
            "Field with the improvement (building) value. 0 - or empty, see below - means "
            "the tax roll carries no building on the parcel. The roll lags new construction.")
        form.addRow("  Improvement value field:", self.parcel_improvement_combo)
        self.parcel_ref_combo = QComboBox()
        self.parcel_ref_combo.setToolTip("Parcel number, quoted in the hint (optional).")
        form.addRow("  Parcel number field (optional):", self.parcel_ref_combo)
        self.parcel_vacant_kw_edit = QLineEdit()
        self.parcel_vacant_kw_edit.setPlaceholderText("(built-in list)")
        self.parcel_vacant_kw_edit.setToolTip(
            "Land-use words meaning no building, comma-separated, matched at the start of a "
            "word. Empty = the built-in list: " + ", ".join(PH.NO_BUILDING_KEYWORDS))
        form.addRow("  'No building' words:", self.parcel_vacant_kw_edit)
        self.parcel_nonres_kw_edit = QLineEdit()
        self.parcel_nonres_kw_edit.setPlaceholderText("(built-in list)")
        self.parcel_nonres_kw_edit.setToolTip(
            "Land-use words meaning a non-residential parcel (a farm parcel very often "
            "carries a farm house - it is only a hint). Empty = the built-in list: " +
            ", ".join(PH.NON_RESIDENTIAL_KEYWORDS))
        form.addRow("  'Non-residential' words:", self.parcel_nonres_kw_edit)
        self.parcel_res_kw_edit = QLineEdit()
        self.parcel_res_kw_edit.setPlaceholderText("(built-in list)")
        self.parcel_res_kw_edit.setToolTip(
            "Land-use words meaning a dwelling - they cancel a 'non-residential' hint. "
            "Empty = the built-in list: " + ", ".join(PH.RESIDENTIAL_KEYWORDS))
        form.addRow("  'Residential' words:", self.parcel_res_kw_edit)
        self.parcel_empty_zero_check = QCheckBox("An empty improvement value counts as 0")
        self.parcel_empty_zero_check.setChecked(bool(_default("parcel_hint_empty_is_zero")))
        self.parcel_empty_zero_check.setToolTip(
            "Untick when your parcel data leave the value empty for parcels that do have a "
            "building.")
        form.addRow("  ", self.parcel_empty_zero_check)

        lay.addLayout(form)

        # -- tools
        self.estimate_btn = QPushButton("Estimate run...")
        self.estimate_btn.setToolTip(
            "Looks at the rasters and the download choices and tells what this run would "
            "read, download and write - study area, raster cells, data tiles (already in the "
            "cache or to fetch), offline pack, rough time and disk. Nothing is downloaded or "
            "written; takes about a second. The times and sizes are ROUGH (assumed costs, "
            "not measurements).")
        self.estimate_btn.clicked.connect(self._on_estimate290)
        self.profile_save_btn = QPushButton("Save settings profile...")
        self.profile_save_btn.setToolTip(
            "Saves the classification options of this dialog as a small file to share (a "
            "firm's standard settings). Left out: everything of ONE dam or ONE project - "
            "folders, rasters, scenarios, layer choices, paths.")
        self.profile_save_btn.clicked.connect(self._on_profile_save290)
        self.profile_load_btn = QPushButton("Load settings profile...")
        self.profile_load_btn.setToolTip(
            "Applies a settings profile. Dam- or project-specific entries found in the file "
            "are ignored. The run log records the profile's name and hash, and whether you "
            "changed anything afterwards.")
        self.profile_load_btn.clicked.connect(self._on_profile_load290)
        self.profile_compare_btn = QPushButton("Compare profiles...")
        self.profile_compare_btn.setToolTip(
            "Lists the settings that differ between two profile files.")
        self.profile_compare_btn.clicked.connect(self._on_profile_compare290)
        self.reproduce_btn = QPushButton("Reproduce a package...")
        self.reproduce_btn.setToolTip(
            "Re-runs a classification from its submittal package (folder or .zip) in an "
            "EMPTY work folder - with the settings, rasters and data of that run, nothing "
            "downloaded, nothing added to this project - and checks that the same result "
            "comes out. Takes as long as the original run.")
        self.reproduce_btn.clicked.connect(self._on_reproduce290)
        self.portfolio_btn = QPushButton("Portfolio dashboard...")
        self.portfolio_btn.setToolTip(
            "One web page with a row per dam of a folder of dams: class, last run, oldest "
            "data, open review items, creep alert. Read from the files the runs left; "
            "nothing is re-run. The classes are screening results as last run.")
        self.portfolio_btn.clicked.connect(self._on_portfolio290)
        self.formfill_btn = QPushButton("Fill form template...")
        self.formfill_btn.setToolTip(
            "Fills YOUR form template (.docx, .txt, .md, .html or .csv) where it has "
            "{{KEY}} placeholders, with values from the last run. No form field is guessed: "
            "unknown placeholders and placeholders without a value are left visible and "
            "listed. Check every value that goes on a signed form.")
        self.formfill_btn.clicked.connect(self._on_formfill290)
        self.formfill_keys_btn = QPushButton("Save form key list")
        self.formfill_keys_btn.setToolTip(
            "Writes Form_Values.csv (KEY, value, description) to the outputs folder, for "
            "copying values into any form by hand (for example a fillable PDF).")
        self.formfill_keys_btn.clicked.connect(self._on_formfill_keys290)
        for buttons in ((self.estimate_btn, self.profile_save_btn, self.profile_load_btn,
                         self.profile_compare_btn),
                        (self.reproduce_btn, self.portfolio_btn, self.formfill_btn,
                         self.formfill_keys_btn)):
            row = QWidget()
            hl = QHBoxLayout(row)
            hl.setContentsMargins(0, 0, 0, 0)
            for b in buttons:
                hl.addWidget(b)
            lay.addWidget(row)

        for chk in (self.mobile_shape_check, self.conseq_sens_check, self.temporal_check,
                    self.parcel_hints_check):
            chk.toggled.connect(self._update_enabled290)
        grp.setLayout(lay)
        if "_profile_status" not in self.__dict__:
            self._profile_status = None
        self._update_enabled290()
        self._refresh_parcel_fields290()
        return grp

    @staticmethod
    def _spin290(lo, hi, step, key, suffix):
        s = QDoubleSpinBox()
        s.setRange(lo, hi)
        s.setSingleStep(step)
        s.setDecimals(2)
        s.setValue(float(_default(key)))
        s.setSuffix(suffix)
        return s

    def _update_enabled290(self, *args):
        """Options that depend on a tick box are greyed out without it (their
        values are still saved and read)."""
        def on(name):
            w = self.__dict__.get(name)
            return bool(w.isChecked()) if isinstance(w, QCheckBox) else None

        def group(flag, names):
            state = on(flag)
            if state is None:
                return
            for n in names:
                w = self.__dict__.get(n)
                if w is not None:
                    w.setEnabled(state)
        group("mobile_shape_check", ("mobile_max_width_spin", "mobile_min_ratio_spin",
                                     "mobile_min_length_spin", "mobile_max_length_spin"))
        group("conseq_sens_check", ("conseq_warning_edit", "conseq_ppd_edit",
                                    "conseq_pop_check", "conseq_severity_check"))
        group("temporal_check", ("temporal_start_edit", "temporal_step_spin"))
        group("parcel_hints_check", ("parcel_landuse_combo", "parcel_improvement_combo",
                                     "parcel_ref_combo", "parcel_vacant_kw_edit",
                                     "parcel_nonres_kw_edit", "parcel_res_kw_edit",
                                     "parcel_empty_zero_check"))

    # ------------------------------------------------------ parcel fields --
    def _pick_dv_template290(self):
        path, _f = QFileDialog.getOpenFileName(
            self, "Depth-velocity spreadsheet template", self.dv_template_edit.text().strip(),
            "Excel workbook (*.xlsx)")
        if path:
            self.dv_template_edit.setText(path)

    def _widget290(self, name, cls):
        """The widget `name` when it is built (the host may be a QWidget whose
        unknown attributes answer with a no-op object)."""
        w = self.__dict__.get(name)
        return w if isinstance(w, cls) else None

    def _parcel_layer290(self):
        """The parcel layer chosen in the dialog (auto-detect: the first project
        polygon layer whose name has 'parcel'); None when none / downloaded."""
        combo = self._widget290("parcel_combo", QComboBox)
        if combo is None:
            return None
        from .classify_tab import _AUTO, _NONE, _DOWNLOAD, _PARCEL_RE, _PARCEL_EXCLUDE
        from .processing import is_plugin_output_name, is_downloaded_data_layer
        data = combo.currentData()
        folder_lyr = self._folder_parcels290(data)
        if folder_lyr is not None:
            return folder_lyr
        if data in (None, _NONE, _DOWNLOAD):
            return None
        project = QgsProject.instance()
        if data != _AUTO:
            lyr = project.mapLayer(data)
            return lyr if isinstance(lyr, QgsVectorLayer) else None
        for lyr in project.mapLayers().values():
            try:
                if (isinstance(lyr, QgsVectorLayer) and lyr.isValid()
                        and lyr.geometryType() == GEOM_POLYGON and _PARCEL_RE.search(lyr.name())
                        and not _PARCEL_EXCLUDE.search(lyr.name())
                        and not is_plugin_output_name(lyr.name())
                        and not is_downloaded_data_layer(lyr)):
                    return lyr
            except Exception:
                continue
        return None

    def _folder_parcels290(self, data):
        """The data folder's parcel layer when the run will use it (the parcel
        list holds no layer picked by hand) - the same rule as
        _local_data_for_run. Opened once per file (read-only)."""
        from .classify_tab import _AUTO, _NONE, _DOWNLOAD
        if data not in (None, _NONE, _AUTO, _DOWNLOAD):
            return None
        getter = getattr(self, "_local_data_for_run", None)
        if getter is None:
            return None
        try:
            uri = (getter() or {}).get("parcels")
        except Exception:                                    # noqa
            return None
        if not uri or not isinstance(uri, str):
            return None
        cached = self.__dict__.get("_folder_parcels_cache290")
        if cached and cached[0] == uri:
            return cached[1]
        from . import datafolder as DF
        lyr = DF.open_uri(uri)
        self._folder_parcels_cache290 = (uri, lyr)
        return lyr

    def _refresh_parcel_fields290(self, *args):
        """Refill the three parcel field pickers from the parcel layer chosen in
        the dialog. A choice stays when the field still exists; for a NEW layer
        the field names are only SUGGESTED (parcelhints.suggest_fields) - the
        engineer confirms. Safe to call before the widgets are built."""
        from . import parcelhints as PH
        specs = (("landuse", "parcel_landuse_combo"),
                 ("improvement", "parcel_improvement_combo"),
                 ("ref", "parcel_ref_combo"))
        combos = [(role, self._widget290(name, QComboBox)) for role, name in specs]
        if any(c is None for _r, c in combos):
            return
        try:
            lyr = self._parcel_layer290()
        except Exception as e:                               # noqa
            log_message("Parcel layer not read: {}".format(e), MSG_WARNING)
            lyr = None
        key = lyr.id() if lyr is not None else None
        try:
            names = [f.name() for f in lyr.fields()] if lyr is not None else []
        except Exception:
            names = []
        sug = PH.suggest_fields(lyr) if lyr is not None else {}
        new_layer = key != self.__dict__.get("_parcel_key290", "\0unset")
        for role, combo in combos:
            prev = combo.currentData() or ""
            combo.blockSignals(True)
            try:
                combo.clear()
                combo.addItem("(none)", "")
                for n in names:
                    combo.addItem(n, n)
                if prev in names:
                    pick = prev                               # the engineer's choice stays
                elif new_layer and sug.get(role):
                    pick = sug[role][0]                       # a new layer: suggest
                else:
                    pick = ""
                idx = combo.findData(pick)
                if idx >= 0:
                    combo.setCurrentIndex(idx)
            finally:
                combo.blockSignals(False)
        self._parcel_key290 = key

    def _restore_parcel_fields290(self, state):
        """After a settings state / profile was applied: refill the parcel field
        pickers and select the saved fields that exist in the layer."""
        self._refresh_parcel_fields290()
        for name in ("parcel_landuse_combo", "parcel_improvement_combo", "parcel_ref_combo"):
            combo = self._widget290(name, QComboBox)
            if combo is not None and name in (state or {}):
                idx = combo.findData(state[name] or "")
                if idx >= 0:
                    combo.setCurrentIndex(idx)

    # ------------------------------------------------------------ cfg keys --
    def _extras290_config(self):
        """The v2.9 keys of the classification cfg (proper types)."""
        return {
            "mobile_shape": bool(self.mobile_shape_check.isChecked()),
            "mobile_max_width_ft": float(self.mobile_max_width_spin.value()),
            "mobile_min_ratio": float(self.mobile_min_ratio_spin.value()),
            "mobile_min_length_ft": float(self.mobile_min_length_spin.value()),
            "mobile_max_length_ft": float(self.mobile_max_length_spin.value()),
            "mobile_lot_units": bool(self.mobile_lot_check.isChecked()),
            "download_trailer_parks": bool(self.trailer_parks_check.isChecked()),
            "fast_sampling": bool(self.fast_sampling_check.isChecked()),
            "input_overviews": bool(self.input_overviews_check.isChecked()),
            "zone_overviews": bool(self.zone_overviews_check.isChecked()),
            "report_imagery": bool(self.report_imagery_check.isChecked()),
            "dv_spreadsheet": bool(self.dv_spreadsheet_check.isChecked()),
            "dv_template": self.dv_template_edit.text().strip(),
            "conseq_sensitivity": bool(self.conseq_sens_check.isChecked()),
            "conseq_sens_warning_hr": self.conseq_warning_edit.text().strip(),
            "conseq_sens_ppd": self.conseq_ppd_edit.text().strip(),
            "conseq_sens_population": bool(self.conseq_pop_check.isChecked()),
            "conseq_sens_severity": bool(self.conseq_severity_check.isChecked()),
            "evac_routes": bool(self.evac_routes_check.isChecked()),
            "temporal_animation": bool(self.temporal_check.isChecked()),
            "temporal_breach_start": self.temporal_start_edit.text().strip(),
            "temporal_step_min": int(self.temporal_step_spin.value()),
            "web_export": bool(self.web_export_check.isChecked()),
            "parcel_hints": bool(self.parcel_hints_check.isChecked()),
            "parcel_landuse_field": self.parcel_landuse_combo.currentData() or "",
            "parcel_improvement_field": self.parcel_improvement_combo.currentData() or "",
            "parcel_ref_field": self.parcel_ref_combo.currentData() or "",
            "parcel_hint_vacant_keywords": self.parcel_vacant_kw_edit.text().strip(),
            "parcel_hint_nonres_keywords": self.parcel_nonres_kw_edit.text().strip(),
            "parcel_hint_res_keywords": self.parcel_res_kw_edit.text().strip(),
            "parcel_hint_empty_is_zero": bool(self.parcel_empty_zero_check.isChecked()),
        }

    def _extras290_problems(self):
        """Problems with the v2.9 choices (for validate_all_inputs). Options are
        only checked while their tick box is on."""
        problems = []
        if self.mobile_shape_check.isChecked():
            if self.mobile_min_length_spin.value() > self.mobile_max_length_spin.value():
                problems.append("Mobile homes: the shortest length ({:g} ft) is longer than "
                                "the longest ({:g} ft)".format(
                                    self.mobile_min_length_spin.value(),
                                    self.mobile_max_length_spin.value()))
        if self.conseq_sens_check.isChecked():
            for label, edit, positive in (
                    ("warning time shifts", self.conseq_warning_edit, False),
                    ("persons per dwelling", self.conseq_ppd_edit, True)):
                good, bad = _number_tokens(edit.text())
                if bad:
                    problems.append("Consequence sensitivity: '{}' in the {} is not a "
                                    "number (use numbers separated by commas)".format(
                                        bad[0], label))
                elif positive and good and not any(v > 0 for v in good):
                    problems.append("Consequence sensitivity: the {} need at least one "
                                    "number above 0".format(label))
        if self.temporal_check.isChecked():
            txt = self.temporal_start_edit.text().strip()
            if txt and parse_start_time(txt) is None:
                problems.append("Animation clock start '{}' is not in the form YYYY-MM-DD "
                                "HH:MM (leave it empty for the nominal start)".format(txt))
        if self.parcel_hints_check.isChecked():
            if self._parcel_layer290() is None:
                problems.append("Parcel hints: choose a parcel layer (Data tab, parcels for "
                                "the future-development screening)")
            elif not (self.parcel_landuse_combo.currentData() or
                      self.parcel_improvement_combo.currentData()):
                problems.append("Parcel hints: choose a land-use field or an improvement "
                                "value field")
        return problems

    # ----------------------------------------------------------- utilities --
    def _say290(self, msg):
        try:
            self._log(msg)
        except Exception:
            pass

    def _status290(self, msg, ok=True):
        try:
            self._update_status(msg, ok)
        except Exception:
            pass

    def _text290(self, name):
        try:
            v = getattr(self, name)
        except Exception:
            return ""
        return v if isinstance(v, str) else ""

    def _start_dir290(self):
        return (self.__dict__.get("_tools_dir290") or self._text290("config_dam_folder") or
                self._text290("config_output_folder") or os.path.expanduser("~"))

    def _remember_dir290(self, path):
        try:
            folder = path if os.path.isdir(path) else os.path.dirname(path)
            if folder:
                self._tools_dir290 = folder
        except Exception:
            pass

    @staticmethod
    def _layer_ids290():
        try:
            return list(QgsProject.instance().mapLayers().keys())
        except Exception:
            return []

    def _open_file290(self, path):
        from qgis.PyQt.QtCore import QUrl
        from qgis.PyQt.QtGui import QDesktopServices
        if not path or not os.path.exists(path):
            return False
        return bool(QDesktopServices.openUrl(QUrl.fromLocalFile(path)))

    def _outputs_folder290(self, title):
        out = self._text290("config_output_folder")
        if not out or not os.path.isdir(out):
            QMessageBox.warning(self, title, "There is no outputs folder yet - run the "
                                "classification first (the values are read from its files).")
            return ""
        return out

    def _busy290(self):
        return bool(self.__dict__.get("_busy", False))

    # ------------------------------------------------------------ estimate --
    @_guarded("Estimate run")
    def _on_estimate290(self):
        """Pre-run estimate (estimate.py): no downloads, about a second, so on
        the main thread."""
        if self._busy290():
            return
        from . import estimate as EST
        try:
            cfg = self._classification_config(QgsProject.instance().transformContext())
        except Exception as e:
            log_message(traceback.format_exc(), MSG_WARNING)
            QMessageBox.warning(self, "Estimate run", "The settings are not complete enough "
                                "for an estimate yet:\n{}".format(e))
            return
        est = EST.estimate_for_cfg(cfg)
        lines = [str(x) for x in EST.estimate_lines(est)]
        for line in lines:
            self._say290(">>> " + line)
        show = QMessageBox.information if est.get("ok", True) else QMessageBox.warning
        text = "\n".join(lines[:30])
        if len(lines) > 30:
            text += "\n... (all {} lines are in the log)".format(len(lines))
        show(self, "Estimate run", text)

    # ------------------------------------------------------------ profiles --
    @_guarded("Settings profile")
    def _on_profile_save290(self):
        if self._busy290():
            return
        from . import settings as ST
        start = os.path.join(self._start_dir290(), "settings" + ST.PROFILE_EXT)
        path, _f = QFileDialog.getSaveFileName(
            self, "Save settings profile", start, "Settings profile (*{})".format(ST.PROFILE_EXT))
        if not path:
            return
        if not path.lower().endswith(ST.PROFILE_EXT):
            if path.lower().endswith(".json"):
                path = path[:-5]
            path += ST.PROFILE_EXT
        default = os.path.basename(path)[:-len(ST.PROFILE_EXT)] or "settings"
        name, ok = QInputDialog.getText(self, "Settings profile", "Name of the profile:",
                                        text=default)
        if not ok or not str(name).strip():
            return
        note, ok2 = QInputDialog.getText(self, "Settings profile",
                                         "Note (optional - what these settings are for):")
        note = str(note).strip() if ok2 else ""
        try:
            meta = ST.save_profile(path, self._current_state(), str(name).strip(), note,
                                   layer_ids=self._layer_ids290())
        except (ST.ProfileError, OSError) as e:
            QMessageBox.warning(self, "Settings profile", "The profile was not saved:\n{}"
                                .format(e))
            return
        self._remember_dir290(path)
        self._say290(">>> Settings profile saved: '{}' ({} settings, sha256 {}) - {}".format(
            meta["name"], meta["keys"], meta["sha256"][:16], meta["path"]))
        if meta["excluded_keys"]:
            self._say290(">>> Left out of the profile (they belong to one dam or project): "
                         + ", ".join(meta["excluded_keys"]))
        self._status290("Settings profile saved: {}".format(os.path.basename(path)), True)

    @_guarded("Settings profile")
    def _on_profile_load290(self):
        if self._busy290():
            return
        from . import settings as ST
        path, _f = QFileDialog.getOpenFileName(
            self, "Load settings profile", self._start_dir290(),
            "Settings profile (*{});;JSON (*.json)".format(ST.PROFILE_EXT))
        if not path:
            return
        known = list(self._SETTING_WIDGETS) + ["scenario_rows", "main_baseline"]
        ids = self._layer_ids290()
        try:
            state, meta = ST.load_profile(path, known_keys=known, layer_ids=ids)
        except ST.ProfileError as e:
            QMessageBox.warning(self, "Settings profile", str(e))
            return
        self._apply_state(state)
        self._restore_parcel_fields290(state)
        self._remember_dir290(path)
        self._profile_state290 = state
        self._profile_meta290 = meta
        try:
            self._profile_status = ST.profile_status(self._current_state(), state, meta, ids)
        except Exception as e:                               # noqa
            log_message("Profile status: {}".format(e), MSG_WARNING)
            self._profile_status = None
        self._say290(">>> Settings profile loaded: '{}' ({} settings, sha256 {})".format(
            meta["name"], meta["keys"], meta["sha256"][:16]))
        for w in meta.get("warnings") or []:
            self._say290(">>> Profile note: " + w)
        if meta.get("dropped_keys"):
            self._say290(">>> Profile entries left out: " + ", ".join(
                "{} ({})".format(k, v) for k, v in sorted(meta["dropped_keys"].items())))
        text = "Loaded '{}': {} setting(s) applied.".format(meta["name"] or "?", meta["keys"])
        if meta.get("note"):
            text += "\n\nNote of the profile: {}".format(meta["note"])
        if meta.get("warnings"):
            text += "\n\n" + "\n".join("- " + w for w in meta["warnings"])
        (QMessageBox.warning if meta.get("warnings") else QMessageBox.information)(
            self, "Settings profile", text)
        self._status290("Settings profile loaded: {}".format(meta["name"] or
                                                              os.path.basename(path)), True)

    def _settings_profile_for_cfg(self):
        """dict for cfg["settings_profile"] - the loaded profile (name, sha256,
        modified, changed_keys, path), re-checked against the dialog's settings
        NOW; None when no profile was loaded."""
        state = self.__dict__.get("_profile_state290")
        meta = self.__dict__.get("_profile_meta290")
        if state is None:
            return self.__dict__.get("_profile_status") or None
        try:
            from . import settings as ST
            self._profile_status = ST.profile_status(self._current_state(), state, meta,
                                                     self._layer_ids290())
        except Exception as e:                               # noqa
            log_message("Profile status: {}".format(e), MSG_WARNING)
        return self.__dict__.get("_profile_status") or None

    @_guarded("Compare profiles")
    def _on_profile_compare290(self):
        if self._busy290():
            return
        from . import settings as ST
        paths = []
        for n, what in enumerate(("first", "second")):
            p, _f = QFileDialog.getOpenFileName(
                self, "Compare profiles - choose the {} profile".format(what),
                self._start_dir290(), "Settings profile (*{});;JSON (*.json)".format(
                    ST.PROFILE_EXT))
            if not p:
                return
            paths.append(p)
        try:
            lines = ST.diff_lines(paths[0], paths[1])
        except ST.ProfileError as e:
            QMessageBox.warning(self, "Compare profiles", str(e))
            return
        self._remember_dir290(paths[1])
        head = "{}  <->  {}".format(os.path.basename(paths[0]), os.path.basename(paths[1]))
        self._say290(">>> Profile comparison: " + head)
        for line in lines:
            self._say290("    " + line)
        if not lines:
            QMessageBox.information(self, "Compare profiles",
                                    head + "\n\nThe two profiles hold the same settings.")
        else:
            more = "\n... (all {} in the log)".format(len(lines)) if len(lines) > 40 else ""
            QMessageBox.information(
                self, "Compare profiles", "{}\n\n{} setting(s) differ (first -> second):\n{}{}"
                .format(head, len(lines), "\n".join(lines[:40]), more))

    # ----------------------------------------------------------- reproduce --
    @_guarded("Reproduce a package")
    def _on_reproduce290(self):
        if self._busy290():
            return
        from . import reproduce as REP
        title = "Reproduce a package"
        kind = QMessageBox.question(
            self, title, "Is the submittal package a .zip file?\n\nYes = choose the .zip file\n"
            "No = choose the package folder", MB_YES | MB_NO | MB_CANCEL)
        if kind == MB_CANCEL:
            return
        if kind == MB_YES:
            package, _f = QFileDialog.getOpenFileName(
                self, "Submittal package (.zip)", self._start_dir290(), "Zip (*.zip)")
        else:
            package = QFileDialog.getExistingDirectory(self, "Submittal package folder",
                                                       self._start_dir290())
        if not package:
            return
        work = QFileDialog.getExistingDirectory(
            self, "EMPTY work folder for the reproduction", os.path.dirname(package.rstrip("/\\"))
            or self._start_dir290())
        if not work:
            return
        if os.path.isdir(work) and os.listdir(work) and not os.path.isfile(
                os.path.join(work, REP.WORK_MARKER)):
            QMessageBox.warning(self, title, "The work folder is not empty:\n{}\n\nChoose an "
                                "empty (or new) folder - the reproduction writes there and "
                                "must never touch a dam's real outputs.".format(work))
            return
        rasters = None
        if QMessageBox.question(
                self, title, "Read the scenario rasters from another folder?\n\nOnly needed "
                "when the package does not hold them (the folder is searched by file name).",
                MB_YES | MB_NO) == MB_YES:
            rasters = QFileDialog.getExistingDirectory(self, "Folder with the rasters",
                                                       self._start_dir290()) or None
        self._remember_dir290(package)
        self._say290(">>> Reproducing {} in {}".format(package, work))

        def job(progress, log):
            return REP.reproduce(package, work, progress=progress, log=log,
                                 rasters_folder=rasters)

        def done(outcome):
            if outcome.cancelled:
                self._status290("Reproduction cancelled", False)
                return
            if outcome.error is not None:
                self._say290(outcome.traceback)
                log_message(outcome.traceback, MSG_CRITICAL)
                self._status290("Reproduction FAILED: {}".format(outcome.error), False)
                QMessageBox.critical(self, title, str(outcome.error))
                return
            res = outcome.result or {}
            lines = [str(x) for x in (res.get("lines") or [])]
            status = res.get("status") or "?"
            for line in lines:
                self._say290(">>> " + line)
            self._status290("Reproduction: {}".format(status), bool(res.get("identical")))
            (QMessageBox.information if res.get("identical") else QMessageBox.warning)(
                self, title, "Result: {}\n\n{}".format(status, "\n".join(lines[:30])))
        self._start_background("Reproducing the package...", job, done, project_bound=False)

    # ----------------------------------------------------------- portfolio --
    @_guarded("Portfolio dashboard")
    def _on_portfolio290(self):
        if self._busy290():
            return
        from . import portfolio as PF
        title = "Portfolio dashboard"
        dam = self._text290("config_dam_folder").rstrip("/\\")
        start = os.path.dirname(dam) if dam else self._start_dir290()
        parent = QFileDialog.getExistingDirectory(self, "Folder containing the dam folders",
                                                  start or "")
        if not parent:
            return
        self._remember_dir290(parent)
        self._say290(">>> Portfolio dashboard: {}".format(parent))

        def job(progress, log):
            return PF.build(parent)

        def done(outcome):
            if outcome.cancelled:
                self._status290("Portfolio dashboard cancelled", False)
                return
            if outcome.error is not None:
                self._say290(outcome.traceback)
                log_message(outcome.traceback, MSG_CRITICAL)
                self._status290("Portfolio dashboard FAILED: {}".format(outcome.error), False)
                QMessageBox.critical(self, title, str(outcome.error))
                return
            html_path, csv_path, rows = outcome.result
            self._say290(">>> Portfolio: {} dam(s) - {}".format(len(rows or []), html_path))
            self._say290(">>> Portfolio table: {}".format(csv_path))
            self._status290("Portfolio dashboard: {} dam(s)".format(len(rows or [])),
                            bool(rows))
            if not rows:
                QMessageBox.information(self, title, "No dam folders were found in\n{}"
                                        .format(parent))
            self._open_file290(html_path)
        self._start_background("Building the portfolio dashboard...", job, done,
                               project_bound=False)

    # ----------------------------------------------------------- form fill --
    @_guarded("Fill form template")
    def _on_formfill290(self):
        if self._busy290():
            return
        from . import formfill as FF
        title = "Fill form template"
        out_folder = self._outputs_folder290(title)
        if not out_folder:
            return
        template, _f = QFileDialog.getOpenFileName(
            self, "Form template with {{KEY}} placeholders", self._start_dir290(),
            "Templates (*.docx *.txt *.md *.html *.htm *.csv)")
        if not template:
            return
        ext = os.path.splitext(template)[1]
        suggested = os.path.join(out_folder, os.path.splitext(os.path.basename(template))[0]
                                 + "_filled" + ext)
        out, _f2 = QFileDialog.getSaveFileName(self, "Save the filled form as", suggested,
                                               "*" + ext)
        if not out:
            return
        try:
            written, unknown, missing = FF.fill_template(
                template, FF.available_values(out_folder), out)
        except (ValueError, OSError) as e:
            QMessageBox.warning(self, title, "The form was not filled:\n{}".format(e))
            return
        self._remember_dir290(template)
        self._say290(">>> Form filled: {}".format(written))
        text = ["Filled form: {}".format(written)]
        if unknown:
            text.append("Placeholders the plugin has no such KEY for (left as they are): "
                        + ", ".join(unknown))
        if missing:
            text.append("Placeholders whose value this run did not record (left as they "
                        "are): " + ", ".join(missing))
        if not unknown and not missing:
            text.append("Every placeholder was filled.")
        text.append("Check every value before the form is signed - they are screening "
                    "results.")
        for line in text[1:-1]:
            self._say290(">>> " + line)
        self._status290("Form filled: {}".format(os.path.basename(written)),
                        not (unknown or missing))
        (QMessageBox.warning if (unknown or missing) else QMessageBox.information)(
            self, title, "\n\n".join(text))

    @_guarded("Save form key list")
    def _on_formfill_keys290(self):
        if self._busy290():
            return
        from . import formfill as FF
        title = "Save form key list"
        out_folder = self._outputs_folder290(title)
        if not out_folder:
            return
        path = FF.write_values_csv(out_folder)
        self._say290(">>> Form values written: {}".format(path))
        self._status290("Form key list saved: {}".format(os.path.basename(path)), True)
        QMessageBox.information(self, title, "Saved:\n{}\n\nKEY, value and description per "
                                "row; a KEY with no value was not recorded by the last run."
                                .format(path))
