# -*- coding: utf-8 -*-
"""v2.8 UI (mixed into the main dialog, built into the Hazard Classification
tab): the optional analyses after the classification (sensitivity table,
downstream termination point, second population source, evacuation timing),
the download cache and quick re-run options, the creep-watch worker count and
the dams-in-series screen. The analyses themselves are in pipeline_extras.py
and their own modules; this file only collects the choices.

Every screening value shown here is a parameter chosen by the engineer - the
defaults are starting values, not requirements."""

import os
import re
import traceback

from qgis.PyQt.QtWidgets import (
    QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QGroupBox, QFormLayout,
    QSpinBox, QDoubleSpinBox, QCheckBox, QComboBox, QWidget, QMessageBox, QFileDialog,
    QLineEdit,
)
from qgis.core import QgsProject, QgsVectorLayer, QgsRasterLayer

from .compat import GEOM_POLYGON, MSG_CRITICAL, MSG_WARNING, MB_YES, MB_NO
from .helpers import log_message

_NONE = "__none__"
_CACHE_AUTO = "auto"
# the plugin's own result layers are never offered as a population source
_OWN_RESULT_RE = re.compile(
    r"^(EAP_|Structure_|Habitable|Future_Development|Hazard_|Inventory_QA|Access_Cut|"
    r"Roads_Inundated|Rail_Inundated|Model_Boundary|Lifeline_Facilities|Dams_In_Series)", re.I)


class ExtrasTabMixin(object):
    """Methods for the 'extra analyses and speed' group of the Hazard
    Classification tab and its buttons."""

    # ------------------------------------------------------------------ UI --
    def _build_extras_group(self):
        from .pipeline import DEFAULTS as D
        from .popsource import METHODS as POP_METHODS, RASTER_UNITS
        grp = QGroupBox("6. Extra analyses and speed (advanced - all optional)")
        lay = QVBoxLayout()
        note = QLabel(
            "Screening analyses run after the classification and never change it. The values "
            "are the engineer's own choices (starting values, not requirements). Nothing here "
            "is needed for a normal run.")
        note.setWordWrap(True)
        lay.addWidget(note)
        form = QFormLayout()

        # -- sensitivity
        self.sens_check = QCheckBox("Sensitivity table: re-classify with the depth / velocity "
                                    "scaled by the factors below")
        self.sens_check.setToolTip(
            "Shows how far the modelled depths and velocities would have to be wrong before a "
            "class changes. Re-classifies from the values already read for every structure "
            "(no raster is read again). Writes QA/Sensitivity.csv and "
            "QA/Sensitivity_Structures.csv.")
        form.addRow("", self.sens_check)
        self.sens_factors_edit = QLineEdit()
        self.sens_factors_edit.setText(str(D["sensitivity_factors"]))
        self.sens_factors_edit.setToolTip("Comma-separated factors, 1.0 = as modelled (added "
                                          "automatically). Example: 0.8, 0.9, 1.1, 1.2")
        form.addRow("Sensitivity factors:", self.sens_factors_edit)
        self.sens_split_check = QCheckBox("Scale depth and velocity separately as well")
        form.addRow("", self.sens_split_check)

        # -- termination point
        self.term_check = QCheckBox("Downstream termination point (where the breach flood no "
                                    "longer adds a meaningful rise)")
        self.term_check.setToolTip(
            "Walks down the channel from the dam and reports where the flood stays below the "
            "rise / depth values below for the number of stations given. The values are "
            "screening choices - the engineer decides where the study reach ends.")
        form.addRow("", self.term_check)
        self.term_rise_spin = QDoubleSpinBox()
        self.term_rise_spin.setRange(0.0, 50.0)
        self.term_rise_spin.setSingleStep(0.5)
        self.term_rise_spin.setValue(float(D["term_rise_ft"]))
        self.term_rise_spin.setSuffix(" ft")
        form.addRow("Termination - rise below:", self.term_rise_spin)
        self.term_depth_spin = QDoubleSpinBox()
        self.term_depth_spin.setRange(0.0, 50.0)
        self.term_depth_spin.setSingleStep(0.5)
        self.term_depth_spin.setValue(float(D["term_depth_ft"]))
        self.term_depth_spin.setSuffix(" ft")
        form.addRow("Termination - depth below:", self.term_depth_spin)
        self.term_confirm_spin = QSpinBox()
        self.term_confirm_spin.setRange(1, 50)
        self.term_confirm_spin.setValue(int(D["term_confirm"]))
        self.term_confirm_spin.setSuffix(" stations")
        form.addRow("Termination - confirmed over:", self.term_confirm_spin)

        # -- second population source
        self.pop_source_combo = QComboBox()
        self.pop_source_combo.setToolTip(
            "A census-block / tract polygon layer or a gridded population raster. The "
            "population at risk from it is compared with the plugin's own estimate (from the "
            "structures). The comparison is a reason to look; it does not change the class.")
        self.pop_source_combo.currentIndexChanged.connect(self._pop_source_changed)
        form.addRow("Second population source:", self.pop_source_combo)
        self.pop_field_combo = QComboBox()
        form.addRow("  Population field:", self.pop_field_combo)
        self.pop_day_combo = QComboBox()
        form.addRow("  Daytime field (optional):", self.pop_day_combo)
        self.pop_housing_combo = QComboBox()
        form.addRow("  Housing units field (optional):", self.pop_housing_combo)
        self.pop_period_combo = QComboBox()
        self.pop_period_combo.addItem("Night / residence (main field)", "night")
        self.pop_period_combo.addItem("Day (main field is the daytime population)", "day")
        form.addRow("  The main field stands for:", self.pop_period_combo)
        self.pop_units_combo = QComboBox()
        for key, val in RASTER_UNITS.items():
            self.pop_units_combo.addItem(val[0] if isinstance(val, (tuple, list)) else str(key),
                                         key)
        form.addRow("  Raster values are:", self.pop_units_combo)
        self.pop_method_combo = QComboBox()
        labels = {"auto": "Automatic", "centre": "Cell centre", "fraction": "Fraction of cell"}
        for m in POP_METHODS:
            self.pop_method_combo.addItem(labels.get(m, m), m)
        form.addRow("  Raster method:", self.pop_method_combo)
        self.pop_mindepth_spin = QDoubleSpinBox()
        self.pop_mindepth_spin.setRange(0.0, 20.0)
        self.pop_mindepth_spin.setSingleStep(0.5)
        self.pop_mindepth_spin.setValue(float(D["pop_min_depth_ft"]))
        self.pop_mindepth_spin.setSuffix(" ft")
        form.addRow("  Count where depth is above:", self.pop_mindepth_spin)

        # -- evacuation timing
        self.evac_check = QCheckBox("Evacuation timing (latest safe departure against the "
                                    "flood arrival along the road network)")
        self.evac_check.setToolTip(
            "Needs an arrival-time raster and a road layer that is saved as a file (not a "
            "temporary layer). Writes QA/Evacuation_Timing.csv. A screening estimate, not an "
            "evacuation model.")
        form.addRow("", self.evac_check)
        self.evac_prep_spin = QDoubleSpinBox()
        self.evac_prep_spin.setRange(0.0, 600.0)
        self.evac_prep_spin.setSingleStep(5.0)
        self.evac_prep_spin.setValue(float(D["evac_prep_min"]))
        self.evac_prep_spin.setSuffix(" min")
        form.addRow("  Preparation time after the warning:", self.evac_prep_spin)
        self.evac_passable_spin = QDoubleSpinBox()
        self.evac_passable_spin.setRange(0.1, 10.0)
        self.evac_passable_spin.setSingleStep(0.25)
        self.evac_passable_spin.setValue(float(D["evac_passable_ft"]))
        self.evac_passable_spin.setSuffix(" ft")
        form.addRow("  A road is closed at a depth of:", self.evac_passable_spin)
        self.evac_include_combo = QComboBox()
        self.evac_include_combo.addItem("Flooded structures only", "flooded")
        self.evac_include_combo.addItem("Flooded and nearly flooded", "near")
        self.evac_include_combo.addItem("All habitable structures", "all")
        form.addRow("  Structures included:", self.evac_include_combo)
        self.evac_safe_spin = QDoubleSpinBox()
        self.evac_safe_spin.setRange(0.0, 26400.0)
        self.evac_safe_spin.setSingleStep(100.0)
        self.evac_safe_spin.setValue(float(D["evac_dry_safe_ft"]))
        self.evac_safe_spin.setSuffix(" ft")
        self.evac_safe_spin.setToolTip("A dry place this far from any flooding counts as safe "
                                       "(0 = the first dry road node).")
        form.addRow("  Dry-safe distance:", self.evac_safe_spin)

        # -- speed and data cache
        self.cache_check = QCheckBox("Keep downloaded data tiles in the QGIS profile and "
                                     "re-use them while they are recent")
        self.cache_check.setToolTip(
            "NSI and address downloads are kept per tile with their date; a later run (or the "
            "creep watch) fetches only tiles older than the age below. On by default since "
            "2.17.0, so a rerun the next day does not download them again; untick it (or tick "
            "'Refresh every tile this run') for a run that must read current data. The other "
            "data sets come from the offline pack in the suite data folder when there is one.")
        # 2.17.0: on by default (DATA_AUTOFILL plan quick fix): tiles are re-used only while
        # younger than the age below (7 days by default); projects saved before keep theirs
        self.cache_check.setChecked(True)
        form.addRow("", self.cache_check)
        self.cache_age_spin = QSpinBox()
        self.cache_age_spin.setRange(0, 3650)
        self.cache_age_spin.setValue(int(D["data_max_age_days"]))
        self.cache_age_spin.setSuffix(" days")
        form.addRow("  Tiles are re-fetched when older than:", self.cache_age_spin)
        self.cache_refresh_check = QCheckBox("Refresh every tile this run")
        form.addRow("  ", self.cache_refresh_check)
        crow = QWidget()
        chl = QHBoxLayout(crow)
        chl.setContentsMargins(0, 0, 0, 0)
        self.cache_info_label = QLabel("")
        chl.addWidget(self.cache_info_label, stretch=3)
        self.cache_clear_btn = QPushButton("Clear tile cache")
        self.cache_clear_btn.clicked.connect(self._clear_tile_cache)
        chl.addWidget(self.cache_clear_btn)
        form.addRow("  ", crow)
        self.quick_check = QCheckBox("Quick re-run: skip the PDF report and the map book "
                                     "(everything else is rewritten)")
        self.quick_check.setToolTip(
            "For working through review decisions or settings: the classification, tables and "
            "layers are all redone; only the slow PDF report and map book are left as the "
            "last full run made them. Untick for the final run.")
        form.addRow("", self.quick_check)
        self.watch_workers_spin = QSpinBox()
        self.watch_workers_spin.setRange(1, 16)
        self.watch_workers_spin.setValue(1)
        self.watch_workers_spin.setToolTip(
            "Creep watch: dams classified at the same time. More than 1 starts qgis_process "
            "worker processes (the plugin must be enabled in that QGIS profile); the watch "
            "falls back to one dam at a time when qgis_process is not found.")
        form.addRow("Creep watch workers:", self.watch_workers_spin)
        lay.addLayout(form)
        grp.setLayout(lay)
        self._fill_pop_fields()
        self._update_cache_info()
        return grp

    # ------------------------------------------------- population layer combo --
    def _pop_candidates(self):
        """Polygon vector layers and raster layers that could hold a census /
        gridded population (never the plugin's own outputs)."""
        from .processing import is_plugin_output_name, is_downloaded_data_layer
        out = []
        for lyr in QgsProject.instance().mapLayers().values():
            try:
                if not lyr.isValid() or is_plugin_output_name(lyr.name()) or \
                        _OWN_RESULT_RE.search(lyr.name()) or is_downloaded_data_layer(lyr):
                    continue
            except Exception:
                continue
            if isinstance(lyr, QgsVectorLayer):
                if lyr.geometryType() == GEOM_POLYGON:
                    out.append(lyr)
            elif isinstance(lyr, QgsRasterLayer):
                out.append(lyr)
        return sorted(out, key=lambda x: x.name().lower())

    def _refresh_pop_combo(self):
        """Refill the layer list (keeping the choice) - called with the other
        layer combos."""
        combo = getattr(self, "pop_source_combo", None)
        if combo is None:
            return
        prev = combo.currentData()
        self._pop_updating = True
        combo.blockSignals(True)
        try:
            combo.clear()
            combo.addItem("(none)", _NONE)
            for lyr in self._pop_candidates():
                combo.addItem(lyr.name(), lyr.id())
            for i in range(combo.count()):
                if combo.itemData(i) == prev:
                    combo.setCurrentIndex(i)
                    break
        finally:
            combo.blockSignals(False)
            self._pop_updating = False
        self._fill_pop_fields(keep=True)

    def _pop_layer(self):
        d = self.pop_source_combo.currentData()
        if d in (None, _NONE):
            return None
        return QgsProject.instance().mapLayer(d)

    def _pop_source_changed(self, *args):
        if getattr(self, "_pop_updating", False):
            return                       # the list is being refilled: keep the field choices
        self._fill_pop_fields(keep=False)

    def _fill_pop_fields(self, keep=False):
        """Field pickers of the chosen polygon layer (suggested by name only -
        the engineer chooses); for a raster or no layer they are disabled."""
        from . import popsource as PS
        lyr = self._pop_layer()
        polygons = isinstance(lyr, QgsVectorLayer)
        raster = lyr is not None and not polygons
        want = {}
        if keep:
            want = {"pop": self.pop_field_combo.currentData(),
                    "day": self.pop_day_combo.currentData(),
                    "housing": self.pop_housing_combo.currentData()}
        names = PS.numeric_fields(lyr) if polygons else []
        sug = PS.suggest_fields(lyr) if polygons else {}
        for role, combo, optional in (("pop", self.pop_field_combo, False),
                                      ("day", self.pop_day_combo, True),
                                      ("housing", self.pop_housing_combo, True)):
            combo.blockSignals(True)
            try:
                combo.clear()
                combo.addItem("(choose a field)" if not optional else "(none)", "")
                for n in names:
                    combo.addItem(n, n)
                if want.get(role) in names:
                    pick = want.get(role)                  # the engineer's choice stays
                elif keep:
                    pick = None          # a refresh never puts a suggestion back over "(none)"
                else:
                    pick = sug.get(role) if role == "pop" else None   # a new layer: suggest
                idx = combo.findData(pick) if pick else -1
                if idx >= 0:
                    combo.setCurrentIndex(idx)
            finally:
                combo.blockSignals(False)
            combo.setEnabled(polygons)
        self.pop_units_combo.setEnabled(raster)
        self.pop_method_combo.setEnabled(raster)
        self.pop_mindepth_spin.setEnabled(lyr is not None)
        self.pop_period_combo.setEnabled(lyr is not None)

    # ------------------------------------------------------------- data cache --
    def _cache_folder_for_info(self):
        try:
            from .download import default_cache_folder
            return default_cache_folder()
        except Exception:
            return ""

    def _update_cache_info(self):
        try:
            from .download import tile_cache_stats
            st = tile_cache_stats(self._cache_folder_for_info())
            mb = st["bytes"] / 1048576.0
            self.cache_info_label.setText("Cache: {} tile(s), {:.1f} MB".format(st["files"], mb))
        except Exception:
            self.cache_info_label.setText("")

    def _clear_tile_cache(self, *args):
        from .download import clear_tile_cache
        folder = self._cache_folder_for_info()
        ans = QMessageBox.question(
            self, "Tile cache", "Delete every downloaded data tile kept in\n{}\n?\n\nThe next "
            "run downloads what it needs again.".format(folder or "(not available)"),
            MB_YES | MB_NO)
        if ans != MB_YES:
            return
        n, freed = clear_tile_cache(folder) if folder else (0, 0)
        self._log(">>> Tile cache cleared: {} file(s), {:.1f} MB".format(n, freed / 1048576.0))
        self._update_cache_info()

    def _cache_cfg(self):
        """Cache and speed keys for the pipeline / download cfg."""
        return {
            "data_cache_folder": _CACHE_AUTO if self.cache_check.isChecked() else "",
            "data_max_age_days": int(self.cache_age_spin.value()),
            "refresh_data": bool(self.cache_refresh_check.isChecked()),
        }

    # ------------------------------------------------------------ cfg keys --
    def _extras_config(self):
        """The v2.8 keys of the classification cfg."""
        cfg = {
            "sensitivity": self.sens_check.isChecked(),
            "sensitivity_factors": self.sens_factors_edit.text().strip(),
            "sensitivity_split": self.sens_split_check.isChecked(),
            "termination": self.term_check.isChecked(),
            "term_rise_ft": float(self.term_rise_spin.value()),
            "term_depth_ft": float(self.term_depth_spin.value()),
            "term_confirm": int(self.term_confirm_spin.value()),
            "pop_source": self._pop_layer(),
            "pop_field": self.pop_field_combo.currentData() or "",
            "pop_day_field": self.pop_day_combo.currentData() or "",
            "pop_housing_field": self.pop_housing_combo.currentData() or "",
            "pop_period": self.pop_period_combo.currentData() or "night",
            "pop_raster_units": self.pop_units_combo.currentData() or "per_cell",
            "pop_method": self.pop_method_combo.currentData() or "auto",
            "pop_min_depth_ft": float(self.pop_mindepth_spin.value()),
            "evacuation": self.evac_check.isChecked(),
            "evac_prep_min": float(self.evac_prep_spin.value()),
            "evac_passable_ft": float(self.evac_passable_spin.value()),
            "evac_include": self.evac_include_combo.currentData() or "flooded",
            "evac_dry_safe_ft": float(self.evac_safe_spin.value()),
            "quick_rerun": self.quick_check.isChecked(),
        }
        cfg.update(self._cache_cfg())
        return cfg

    def _extras_problems(self):
        """Problems with the extra choices (for validate_all_inputs)."""
        problems = []
        if self.sens_check.isChecked():
            from .pipeline_extras import parse_factors
            try:
                if not parse_factors(self.sens_factors_edit.text()):
                    problems.append("Sensitivity: no valid factor in the factors field")
            except Exception:
                problems.append("Sensitivity: the factors could not be read (use numbers "
                                "separated by commas)")
        lyr = self._pop_layer()
        if lyr is not None and isinstance(lyr, QgsVectorLayer) and \
                not self.pop_field_combo.currentData():
            problems.append("Second population source: choose the population field")
        if self.evac_check.isChecked():
            paths = getattr(self, "_raster_paths", lambda: {})() or {}
            if not paths.get("arr"):
                problems.append("Evacuation timing needs an arrival-time raster (Setup tab)")
        return problems

    # ---------------------------------------------------------- dams in series --
    def _run_series(self, *args):
        """v2.8: dams-in-series screen over a folder of studied dams
        (series.py) - a background task; writes Dams_In_Series_Screen.csv / .html
        into the folder and each dam's own QA folder."""
        if self._busy:
            return
        start = os.path.dirname(self.config_dam_folder.rstrip("/\\")) if getattr(
            self, "config_dam_folder", "") else ""
        parent = QFileDialog.getExistingDirectory(
            self, "Folder containing the dam folders", start or "")
        if not parent:
            return
        from . import series as SER
        from .runner import detach_layers
        nid = None
        try:
            from .classify_tab import _NONE as _N
            d = self.dam_layer_combo.currentData()
            if d not in (None, _N):
                nid = detach_layers({"dam_layer": QgsProject.instance().mapLayer(d)}
                                    )["dam_layer"]
        except Exception:
            nid = None
        ctx = QgsProject.instance().transformContext()
        self._log(">>> Dams in series screen: {}".format(parent))

        def work(progress, log):
            res = SER.screen_folder(parent, nid_layer=nid, ctx=ctx,
                                    progress=lambda p, m: progress(p, m))
            paths = SER.write_reports(res, parent)
            try:
                SER.write_qa(res)
            except Exception as e:                     # optional per-dam copies
                log_message(traceback.format_exc(), MSG_WARNING)
                log(">>> WARNING: the per-dam files could not all be written ({})".format(e))
            return res, paths

        def done(outcome):
            if outcome.cancelled:
                self._update_status("Dams in series screen cancelled", False)
                return
            if outcome.error is not None:
                self._log(outcome.traceback)
                log_message(outcome.traceback, MSG_CRITICAL)
                QMessageBox.warning(self, "Dams in series", str(outcome.error))
                self._update_status("Dams in series screen failed: {}".format(outcome.error),
                                    False)
                return
            res, paths = outcome.result
            lines = SER.summary_lines(res)
            for line in lines:
                self._log(">>> " + line)
            self._update_status("Dams in series: " + (lines[0] if lines else "done"), True)
            QMessageBox.information(self, "Dams in series", "\n".join(lines[:14]) + "\n\n" +
                                    paths.get("html", ""))
            self._open_path(paths.get("html"))
        self._start_background("Dams in series screen...", work, done, project_bound=False)
