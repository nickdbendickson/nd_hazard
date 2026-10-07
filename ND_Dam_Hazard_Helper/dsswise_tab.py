# -*- coding: utf-8 -*-
"""'DSS-WISE Lite' tab: import a DSS-WISE Lite Simulation Results Package
(zip or unzipped folder), confirm what each file is, convert the rasters to
the plugin's units and use them as the Setup tab rasters (or as an extra
breach scenario on the Hazard Classification tab)."""

import os
import re
import traceback

from qgis.PyQt.QtWidgets import (
    QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QWidget, QTableWidget, QTableWidgetItem,
    QComboBox, QLineEdit, QCheckBox, QTextEdit, QFileDialog, QMessageBox, QFormLayout,
    QGroupBox,
)
from qgis.core import QgsRasterLayer, QgsVectorLayer

from . import dsswise as DW
from .compat import MSG_CRITICAL
from .helpers import (
    log_message, add_layer_to_project, plugin_group, dam_display_name, CancelledByUser,
)
from .constants import GRP_DSSWISE
from .styles import style_depth

DSW_COLS = ["File", "Role", "Units", "How the units were found"]


class DssWiseTabMixin(object):
    def _build_dsswise_tab(self):
        tab = QWidget()
        lay = QVBoxLayout(tab)
        info = QLabel(
            "<h3>DSS-WISE Lite results</h3>Import the <i>Simulation Results Package</i> "
            "downloaded from DSS-WISE Lite (NCCHE / FEMA). Each file is recognised by its "
            "name (e.g. <i>..._FloodDepth_in_Ft-..._UTM.tif</i>); check the roles and units "
            "below before importing. Rasters are converted to ft, ft/s, ft²/s and hours.")
        info.setWordWrap(True)
        lay.addWidget(info)
        grp = QGroupBox("Package")
        form = QFormLayout()
        self.dsw_path_edit = QLineEdit()
        self.dsw_path_edit.setPlaceholderText("results package .zip or its unzipped folder")
        b1 = QPushButton("Zip...")
        b1.clicked.connect(self._dsw_browse_zip)
        b2 = QPushButton("Folder...")
        b2.clicked.connect(self._dsw_browse_folder)
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        h.addWidget(self.dsw_path_edit, stretch=3)
        h.addWidget(b1)
        h.addWidget(b2)
        form.addRow("Package:", w)
        self.dsw_name_edit = QLineEdit()
        self.dsw_name_edit.setPlaceholderText("scenario name (default: package name)")
        form.addRow("Scenario name:", self.dsw_name_edit)
        self.dsw_scan_btn = QPushButton("Scan package")
        self.dsw_scan_btn.clicked.connect(self._dsw_scan)
        form.addRow("", self.dsw_scan_btn)
        grp.setLayout(form)
        lay.addWidget(grp)
        self.dsw_table = QTableWidget()
        self.dsw_table.setColumnCount(len(DSW_COLS))
        self.dsw_table.setHorizontalHeaderLabels(DSW_COLS)
        lay.addWidget(self.dsw_table)
        opts = QHBoxLayout()
        self.dsw_use_setup_check = QCheckBox("Use as the Setup tab rasters")
        self.dsw_use_setup_check.setChecked(True)
        self.dsw_load_check = QCheckBox("Load the converted layers")
        self.dsw_load_check.setChecked(True)
        self.dsw_add_scenario_check = QCheckBox("Add as a scenario in the Hazard "
                                                "Classification tab's table")
        self.dsw_add_scenario_check.setToolTip(
            "A further breach scenario (or, with a name like 'no breach', ... set its role "
            "there) next to the Setup tab rasters")
        for c in (self.dsw_use_setup_check, self.dsw_add_scenario_check, self.dsw_load_check):
            opts.addWidget(c)
        opts.addStretch()
        lay.addLayout(opts)
        self.dsw_import_btn = QPushButton("Import")
        self.dsw_import_btn.clicked.connect(self._dsw_import)
        self.dsw_import_btn.setEnabled(False)
        lay.addWidget(self.dsw_import_btn)
        self.dsw_result_text = QTextEdit()
        self.dsw_result_text.setReadOnly(True)
        lay.addWidget(self.dsw_result_text)
        self._dsw_items = []
        self._dsw_folder = None
        return tab

    def _dsw_browse_zip(self, *args):
        path, _f = QFileDialog.getOpenFileName(self, "DSS-WISE Lite results package",
                                               getattr(self, "config_dam_folder", "") or "",
                                               "Zip (*.zip);;All Files (*)")
        if path:
            self.dsw_path_edit.setText(path)

    def _dsw_browse_folder(self, *args):
        path = QFileDialog.getExistingDirectory(self, "Unzipped DSS-WISE Lite package",
                                                getattr(self, "config_dam_folder", "") or "")
        if path:
            self.dsw_path_edit.setText(path)

    def _dsw_name(self):
        name = self.dsw_name_edit.text().strip()
        if not name:
            base = os.path.basename(self.dsw_path_edit.text().strip().rstrip("/\\"))
            name = os.path.splitext(base)[0] or "DSS-WISE Lite"
        return name

    def _dsw_out(self):
        slug = re.sub(r"[^A-Za-z0-9]+", "_", self._dsw_name()).strip("_")[:40] or "package"
        root = getattr(self, "config_output_folder", "") or os.path.dirname(
            self.dsw_path_edit.text().strip())
        return os.path.join(root, "DSSWISE_" + slug)

    def _dsw_scan(self, *args):
        src = self.dsw_path_edit.text().strip()
        if not src or not os.path.exists(src):
            QMessageBox.warning(self, "DSS-WISE Lite", "Select the results package first.")
            return
        try:
            out = self._dsw_out()
            os.makedirs(out, exist_ok=True)
            self._dsw_folder = DW.open_package(src, out)
            self._dsw_items = DW.scan(self._dsw_folder)
        except Exception as e:
            QMessageBox.critical(self, "DSS-WISE Lite", "Could not read the package: {}".format(e))
            return
        t = self.dsw_table
        t.setRowCount(len(self._dsw_items))
        for i, it in enumerate(self._dsw_items):
            t.setItem(i, 0, QTableWidgetItem(os.path.relpath(it.path, self._dsw_folder)))
            role = QComboBox()
            for r in DW.ALL_ROLES:
                role.addItem(r, r)
            role.setCurrentIndex(DW.ALL_ROLES.index(it.role))
            t.setCellWidget(i, 1, role)
            units = QComboBox()
            for u in sorted({u for us in DW.UNIT_CHOICES.values() for u in us}):
                units.addItem(u, u)
            units.addItem("-", None)
            want = it.units if it.units else "-"
            for k in range(units.count()):
                if units.itemText(k) == want:
                    units.setCurrentIndex(k)
            t.setCellWidget(i, 2, units)
            t.setItem(i, 3, QTableWidgetItem(it.basis or ""))
        n = sum(1 for it in self._dsw_items if it.role in DW.RASTER_ROLES)
        msg = ["{} file(s) found, {} raster(s). Check the roles and units, then Import.".format(
            len(self._dsw_items), n)]
        for role, names in DW.duplicate_roles(self._dsw_items).items():
            msg.append("- {} files are recognised as '{}' ({}); {} will be used - set the "
                       "others to (ignore) to choose.".format(
                           len(names), role, ", ".join(names),
                           DW.chosen(self._dsw_items, role).name))
        for it in DW.assumed_units(self._dsw_items):
            msg.append("- Units of {} are not in its name: {} assumed - check the DSS-WISE "
                       "report and correct the Units column if needed.".format(it.name,
                                                                              it.units))
        self.dsw_result_text.setPlainText("\n".join(msg))
        self.dsw_import_btn.setEnabled(bool(self._dsw_items))

    def _dsw_table_items(self):
        """Items with the roles / units from the table, and a list of rows
        whose units do not fit their role (nothing is guessed for them)."""
        t = self.dsw_table
        bad = []
        for i, it in enumerate(self._dsw_items):
            it.role = t.cellWidget(i, 1).currentData()
            u = t.cellWidget(i, 2).currentData()
            if it.role in DW.UNIT_CHOICES:
                if u not in DW.UNIT_CHOICES[it.role]:
                    bad.append("{} ({}: '{}' - choose one of {})".format(
                        it.name, it.role, u or "-", ", ".join(DW.UNIT_CHOICES[it.role])))
                    continue
                if u != it.units:
                    it.basis = "set by user"
                it.units = u
        return self._dsw_items, bad

    def _dsw_import(self, *args):
        if not self._dsw_items:
            return
        items, bad = self._dsw_table_items()
        if bad:
            QMessageBox.warning(self, "DSS-WISE Lite", "Units do not match the role for:\n" +
                                "\n".join(bad))
            return
        name = self._dsw_name()
        out = self._dsw_out()
        self._set_busy(True)
        self._progress = self._make_progress("Importing DSS-WISE Lite results...")
        try:
            res = DW.convert(items, out, name=name, progress=self._progress_tick)
        except CancelledByUser:
            self._finish_progress()
            self._set_busy(False)
            self._update_status("DSS-WISE import cancelled", False)
            self._log(">>> DSS-WISE Lite import cancelled")
            return
        except Exception as e:
            self._finish_progress()
            self._set_busy(False)
            log_message(traceback.format_exc(), MSG_CRITICAL)
            QMessageBox.critical(self, "DSS-WISE Lite", str(e) or repr(e))
            return
        self._finish_progress()
        self._set_busy(False)
        p = res["paths"]
        if self.dsw_use_setup_check.isChecked():
            # fields the package has no raster for are cleared on purpose: keeping
            # another model's velocity or arrival would mix two data sets
            self.depth_edit.setText(p[DW.R_DEPTH])
            self.vel_edit.setText(p.get(DW.R_VEL) or "")
            self.arr_edit.setText(p.get(DW.R_ARR) or "")
            self.dv_edit.setText(p.get(DW.R_DV) or "")
            # time steps of another model must not be mixed in, and the 1D /
            # 2D choice follows what the package contains
            want = "1d" if res["depth_only"] else "2d"
            i = self.model_combo.findData(want) if hasattr(self.model_combo, "findData") \
                else (1 if want == "1d" else 0)
            self.model_combo.setCurrentIndex(i if i is not None and i >= 0 else
                                             (1 if want == "1d" else 0))
        if res.get("hcom") and hasattr(self, "hcom_edit"):
            # the classification compares its population at risk with DSS-WISE's own
            self.hcom_edit.setText(";".join(res["hcom"]))
        if self.dsw_add_scenario_check.isChecked() and hasattr(self, "_scn_add_row"):
            self._scn_add_row({"name": name, "depth": p[DW.R_DEPTH],
                               "velocity": p.get(DW.R_VEL), "arrival": p.get(DW.R_ARR),
                               "dv": p.get(DW.R_DV)})
            self._log(">>> DSS-WISE '{}' added to the scenario table (Hazard Classification "
                      "tab, advanced options)".format(name))
        if self.dsw_load_check.isChecked():
            grp = plugin_group(dam_display_name(self.config_dam_folder), GRP_DSSWISE)
            for role in DW.RASTER_ROLES:
                if role in p:
                    lyr = QgsRasterLayer(p[role], "{} - {}".format(name, role))
                    if lyr.isValid():
                        if role == DW.R_DEPTH:
                            style_depth(lyr)
                        add_layer_to_project(lyr, visibility=role == DW.R_DEPTH, group=grp)
            vecs = ([(res["boundary"], "inundation boundary")] if res.get("boundary") else []) \
                + [(p_, "hazard zones ({})".format(os.path.splitext(os.path.basename(p_))[0]))
                   for p_ in res.get("hazard_zones") or []]
            for path_, label in vecs:
                b = QgsVectorLayer(path_, "{} - {}".format(name, label), "ogr")
                if b.isValid():
                    add_layer_to_project(b, group=grp)
        text = ["Imported to {}".format(out), ""] + res["lines"] + [""] + \
            ["NOTE: " + n for n in res["notes"]]
        if res.get("report"):
            text.append("DSS-WISE report: {}".format(res["report"]))
        self.dsw_result_text.setPlainText("\n".join(text))
        self._log(">>> DSS-WISE Lite '{}' imported ({})".format(name, ", ".join(
            os.path.basename(v) for v in p.values())))
        self._update_button_states()
