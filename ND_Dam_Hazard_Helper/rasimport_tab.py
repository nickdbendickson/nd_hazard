# -*- coding: utf-8 -*-
"""v2.5: 'HEC-RAS plan' tab - read a HEC-RAS 6.x plan results file
(<project>.p##.hdf) and make the Setup rasters from its 2D flow areas
without the RAS Mapper export step (rasimport.py).

v2.9: "Use this plan for the classification" puts the plan itself in the
Setup tab's depth, velocity and arrival boxes (D x V cleared): the rasters
are made from it when the classification starts, in the background run,
with this tab's options (_ras_plan_settings) and cached
(rasimport.plan_to_rasters_cached in _ras_cache_root) - a re-run plan or
changed options are picked up by the next run, and nothing blocks QGIS now.
"Import" still writes rasters to look at first."""

import os
import traceback

from qgis.PyQt.QtWidgets import (
    QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QWidget, QLineEdit, QCheckBox, QTextEdit,
    QFileDialog, QMessageBox, QFormLayout, QGroupBox, QDoubleSpinBox,
)
from qgis.core import QgsRasterLayer

from . import rasimport as RI
from .compat import MSG_CRITICAL
from .helpers import (
    log_message, add_layer_to_project, plugin_group, dam_display_name, CancelledByUser,
)
from .constants import GRP_HYDRAULICS
from .styles import style_depth


class RasImportTabMixin(object):
    def _build_ras_tab(self):
        tab = QWidget()
        lay = QVBoxLayout(tab)
        info = QLabel(
            "<h3>HEC-RAS plan results</h3>Reads a HEC-RAS 6.x plan results file "
            "(<i>project.p01.hdf</i>) and makes the maximum depth, velocity, water surface, "
            "arrival-time and time-of-peak rasters from its 2D flow areas - no RAS Mapper "
            "export needed. Give the terrain raster for depths on the terrain grid (as RAS "
            "Mapper does); without it depth is measured from each cell's lowest point. "
            "Check the rasters against RAS Mapper for your first plans.")
        info.setWordWrap(True)
        lay.addWidget(info)
        grp = QGroupBox("Plan")
        form = QFormLayout()
        self.ras_plan_edit = QLineEdit()
        self.ras_plan_edit.setPlaceholderText("<project>.p##.hdf (run with 2D output)")
        b1 = QPushButton("Browse...")
        b1.clicked.connect(self._ras_browse_plan)
        form.addRow("Plan results (.hdf):", self._ras_row(self.ras_plan_edit, b1))
        self.ras_dem_edit = QLineEdit()
        self.ras_dem_edit.setPlaceholderText("optional but recommended - the RAS terrain "
                                             "(GeoTIFF), same coordinate system and "
                                             "vertical units as the model")
        b2 = QPushButton("Browse...")
        b2.clicked.connect(self._ras_browse_dem)
        form.addRow("Terrain raster:", self._ras_row(self.ras_dem_edit, b2))
        self.ras_breach_spin = QDoubleSpinBox()
        self.ras_breach_spin.setRange(0.0, 10000.0)
        self.ras_breach_spin.setDecimals(2)
        self.ras_breach_spin.setSuffix(" h after simulation start")
        self.ras_breach_spin.setToolTip("Arrival and time of peak are given in hours after "
                                        "this moment (the breach start). 0 = simulation "
                                        "start.")
        form.addRow("Breach starts at:", self.ras_breach_spin)
        self.ras_arrival_spin = QDoubleSpinBox()
        self.ras_arrival_spin.setRange(0.01, 10.0)
        self.ras_arrival_spin.setDecimals(2)
        self.ras_arrival_spin.setValue(0.1)
        self.ras_arrival_spin.setSuffix(" ft")
        self.ras_arrival_spin.setToolTip("A cell has 'arrived' when its depth (water surface "
                                         "minus the cell's lowest point) first exceeds this.")
        form.addRow("Arrival depth:", self.ras_arrival_spin)
        self.ras_cell_spin = QDoubleSpinBox()
        self.ras_cell_spin.setRange(0.0, 1000.0)
        self.ras_cell_spin.setDecimals(1)
        self.ras_cell_spin.setSuffix(" ft (0 = half the typical cell size)")
        self.ras_cell_spin.setToolTip("Pixel size when no terrain raster is given (with one, "
                                      "its grid is used).")
        form.addRow("Pixel size without terrain:", self.ras_cell_spin)
        self.ras_scan_btn = QPushButton("Read plan")
        self.ras_scan_btn.clicked.connect(self._ras_scan)
        form.addRow("", self.ras_scan_btn)
        grp.setLayout(form)
        lay.addWidget(grp)
        opts = QHBoxLayout()
        self.ras_use_setup_check = QCheckBox("Use as the Setup tab rasters")
        self.ras_use_setup_check.setChecked(True)
        self.ras_load_check = QCheckBox("Load the rasters")
        self.ras_load_check.setChecked(True)
        for c in (self.ras_use_setup_check, self.ras_load_check):
            opts.addWidget(c)
        opts.addStretch()
        lay.addLayout(opts)
        self.ras_import_btn = QPushButton("Import")
        self.ras_import_btn.clicked.connect(self._ras_import)
        lay.addWidget(self.ras_import_btn)
        # v2.9: the plan itself as the classification input
        self.ras_use_plan_btn = QPushButton("Use this plan for the classification")
        self.ras_use_plan_btn.setToolTip(
            "Puts the plan file in the Setup tab's depth, velocity and arrival boxes: the "
            "rasters are made from it when the classification starts (in the background, "
            "with the options above) and re-used while the plan and the options are "
            "unchanged. Nothing is loaded now - use Import to look at the rasters first.")
        self.ras_use_plan_btn.clicked.connect(self._ras_use_plan)
        lay.addWidget(self.ras_use_plan_btn)
        self.ras_result_text = QTextEdit()
        self.ras_result_text.setReadOnly(True)
        lay.addWidget(self.ras_result_text)
        return tab

    def _ras_row(self, edit, btn):
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        h.addWidget(edit, stretch=3)
        h.addWidget(btn)
        return w

    def _ras_browse_plan(self, *args):
        path, _f = QFileDialog.getOpenFileName(
            self, "HEC-RAS plan results", getattr(self, "config_dam_folder", "") or "",
            "HEC-RAS plan HDF (*.p??.hdf *.hdf);;All Files (*)")
        if path:
            self.ras_plan_edit.setText(path)

    def _ras_browse_dem(self, *args):
        start = os.path.dirname(self.ras_plan_edit.text().strip()) or \
            getattr(self, "config_dam_folder", "") or ""
        path, _f = QFileDialog.getOpenFileName(
            self, "Terrain raster", start, "Rasters (*.tif *.tiff *.vrt *.img);;All Files (*)")
        if path:
            self.ras_dem_edit.setText(path)

    def _ras_plan_settings(self):
        """v2.9: this tab's import options (rasimport.PLAN_DEFAULTS keys) -
        the ones a plan in the Setup tab / scenario table is converted with."""
        return {"dem": self.ras_dem_edit.text().strip() or None,
                "breach_hr": float(self.ras_breach_spin.value()),
                "arrival_ft": float(self.ras_arrival_spin.value()),
                "cell_ft": float(self.ras_cell_spin.value())}

    def _ras_cache_root(self):
        """v2.9: <outputs>/HECRAS_plan_cache."""
        return RI.default_cache_root(getattr(self, "config_output_folder", "") or None)

    def _ras_use_plan(self, *args):
        """v2.9: the plan as the Setup tab's depth / velocity / arrival input
        (converted at run time). Checks what can be checked cheaply now."""
        if getattr(self, "_busy", False):
            return
        plan = self.ras_plan_edit.text().strip()
        if not plan or not os.path.isfile(plan):
            QMessageBox.warning(self, "HEC-RAS plan", "Select the plan results file (.hdf).")
            return
        if not RI.PLAN_RE.search(os.path.basename(plan)):
            QMessageBox.warning(self, "HEC-RAS plan",
                                "Only plan results files named <project>.p##.hdf (as HEC-RAS "
                                "writes them) can be used directly - use Import for this file.")
            return
        dem = self.ras_dem_edit.text().strip() or None
        if dem and not os.path.isfile(dem):
            QMessageBox.warning(self, "HEC-RAS plan", "Terrain raster not found: " + dem)
            return
        if not RI.is_hdf5(plan):
            QMessageBox.warning(self, "HEC-RAS plan", "{} is not an HDF5 file.".format(
                os.path.basename(plan)))
            return
        try:
            h = RI.open_hdf(plan)
            try:
                info = RI.plan_info(h)
            finally:
                h.close()
        except Exception as e:
            QMessageBox.critical(self, "HEC-RAS plan", str(e) or repr(e))
            return
        if not info["with_results"]:
            QMessageBox.warning(self, "HEC-RAS plan",
                                "No 2D flow area results in {} (areas: {}) - run the plan with "
                                "2D output first.".format(os.path.basename(plan),
                                                          ", ".join(info["areas"]) or "none"))
            return
        if not info["projection"] and not dem:
            QMessageBox.warning(self, "HEC-RAS plan",
                                "The plan has no coordinate system (set it in RAS Mapper) - give "
                                "the terrain raster so its coordinate system can be used.")
            return
        # rasters of another model must not be mixed in (as Import does)
        self.depth_edit.setText(plan)
        self.vel_edit.setText(plan)
        self.arr_edit.setText(plan)
        self.dv_edit.setText("")
        i = self.model_combo.findData("2d")
        if i is not None and i >= 0:
            self.model_combo.setCurrentIndex(i)
        root = self._ras_cache_root()
        hit = RI.plan_cache_lookup(plan, root, self._ras_plan_settings())
        text = ["Setup tab: depth, velocity and arrival now come from this plan:", "  " + plan,
                "",
                "They are made when the classification starts - with the terrain raster, breach "
                "start, arrival depth and pixel size above - and re-used while the plan and "
                "these options are unchanged (a re-run plan is converted again).",
                "Cache: " + root, "",
                "Already converted with these options: {}".format(hit["folder"]) if hit else
                "Not converted yet: the first run converts it (large models take minutes).",
                "Time-series output steps: {}{}".format(
                    info["time_steps"], "" if info["time_steps"] else
                    " - no arrival raster without them"),
                "", "Use Import to look at the rasters in QGIS first."]
        self.ras_result_text.setPlainText("\n".join(text))
        self._log(">>> Setup tab: HEC-RAS plan {} as depth / velocity / arrival (converted at "
                  "run time{}, breach start {:g} h, arrival depth {:g} ft)".format(
                      os.path.basename(plan), ", terrain " + os.path.basename(dem) if dem
                      else ", no terrain raster", float(self.ras_breach_spin.value()),
                      float(self.ras_arrival_spin.value())))
        self._update_button_states()

    def _ras_out(self):
        plan = self.ras_plan_edit.text().strip()
        root = getattr(self, "config_output_folder", "") or os.path.dirname(plan)
        return os.path.join(root, "HECRAS_" + RI.slug(plan))

    def _ras_scan(self, *args):
        plan = self.ras_plan_edit.text().strip()
        if not plan or not os.path.isfile(plan):
            QMessageBox.warning(self, "HEC-RAS plan", "Select the plan results file (.hdf).")
            return
        try:
            h = RI.open_hdf(plan)
            try:
                info = RI.plan_info(h)
            finally:
                h.close()
        except Exception as e:
            QMessageBox.critical(self, "HEC-RAS plan", str(e))
            return
        text = ["Plan: {}".format(info["title"] or os.path.basename(plan)),
                "Read with: {}".format(info["reader"]),
                "Units: {}".format(info["units"] or "not stated (US Customary assumed)"),
                "Coordinate system: {}".format("stored in the plan" if info["projection"]
                                               else "NOT stored - give the terrain raster"),
                "2D flow areas: {}".format(", ".join(info["areas"]) or "none"),
                "With results: {}".format(", ".join(info["with_results"]) or "none"),
                "Time-series output steps: {}{}".format(
                    info["time_steps"], "" if info["time_steps"] else
                    " - no arrival raster without them")]
        self.ras_result_text.setPlainText("\n".join(text))

    def _ras_import(self, *args):
        plan = self.ras_plan_edit.text().strip()
        if not plan or not os.path.isfile(plan):
            QMessageBox.warning(self, "HEC-RAS plan", "Select the plan results file (.hdf).")
            return
        dem = self.ras_dem_edit.text().strip() or None
        if dem and not os.path.isfile(dem):
            QMessageBox.warning(self, "HEC-RAS plan", "Terrain raster not found: " + dem)
            return
        out = self._ras_out()
        self._set_busy(True)
        self._progress = self._make_progress("Importing HEC-RAS plan results...")
        try:
            res = RI.import_plan(plan, out, dem_path=dem,
                                 cell_ft=float(self.ras_cell_spin.value()),
                                 breach_hr=float(self.ras_breach_spin.value()),
                                 arrival_ft=float(self.ras_arrival_spin.value()),
                                 progress=self._progress_tick)
        except CancelledByUser:
            self._finish_progress()
            self._set_busy(False)
            self._update_status("HEC-RAS import cancelled", False)
            return
        except Exception as e:
            self._finish_progress()
            self._set_busy(False)
            log_message(traceback.format_exc(), MSG_CRITICAL)
            QMessageBox.critical(self, "HEC-RAS plan", str(e) or repr(e))
            return
        self._finish_progress()
        self._set_busy(False)
        p = res["paths"]
        name = "HEC-RAS " + RI.slug(plan)
        if self.ras_use_setup_check.isChecked():
            # rasters of another model must not be mixed in (see DSS-WISE import)
            self.depth_edit.setText(p["depth"])
            self.vel_edit.setText(p.get("vel") or "")
            self.arr_edit.setText(p.get("arr") or "")
            self.dv_edit.setText("")
            i = self.model_combo.findData("2d")
            if i is not None and i >= 0:
                self.model_combo.setCurrentIndex(i)
        if self.ras_load_check.isChecked():
            grp = plugin_group(dam_display_name(self.config_dam_folder), GRP_HYDRAULICS)
            for key, label in (("depth", "max depth"), ("vel", "max velocity"),
                               ("arr", "arrival time"), ("peak", "time of peak"),
                               ("wse", "max water surface")):
                if p.get(key):
                    lyr = QgsRasterLayer(p[key], "{} - {}".format(name, label))
                    if lyr.isValid():
                        if key == "depth":
                            style_depth(lyr)
                        add_layer_to_project(lyr, visibility=key == "depth", group=grp)
        text = ["Imported to {} ({:.1f} ft pixels)".format(out, res.get("pixel_ft") or 0), ""] \
            + res["lines"] + [""] + ["NOTE: " + n for n in res["notes"]] + \
            ["", "Arrival and time of peak: hours after {:g} h of simulation time.".format(
                float(self.ras_breach_spin.value()))]
        self.ras_result_text.setPlainText("\n".join(text))
        self._log(">>> HEC-RAS plan {} imported ({})".format(os.path.basename(plan), ", ".join(
            os.path.basename(v) for v in p.values())))
        self._update_button_states()
