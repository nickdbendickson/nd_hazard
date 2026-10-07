# -*- coding: utf-8 -*-
"""v2.5.1: 'Data & Output' tab (v2.9: the fourth tab): the output options and every
data set the plugin can download from the internet or read from a work
server / project layer. One set of choices drives Main Consolidation, the
Hazard Classification and the 'Download data now' button, which saves the
data sets to <outputs>/Data for the study area or the flooded area
(datafetch.py)."""

import os

from qgis.PyQt.QtWidgets import (
    QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QGroupBox, QFormLayout, QSpinBox,
    QCheckBox, QComboBox, QWidget, QMessageBox, QLineEdit, QScrollArea, QFileDialog,
)
from qgis.core import QgsProject, QgsRasterLayer

from .compat import MSG_CRITICAL, MB_YES
from .constants import (
    DEFAULT_EXTENT_BUFFER_FT, ND_ADDRESS_POINTS_URL, NID_URL,
    SUBDIR_DATA,
)
from .helpers import log_message
from .datafetch import AREA_CHOICES, DEFAULT_FLOOD_BUFFER_FT


class DataTabMixin(object):
    """Needs the main dialog's _inline_row, browse_output, _load_rasters,
    _study_area, _make_progress, _progress_tick, _finish_progress, _log,
    _set_busy, _update_status, config_* attributes and the classification
    tab's _refresh_layer_combos / _resolve_layer."""

    def _build_data_tab(self):
        tab = QWidget()
        self._data_tab = tab
        outer = QVBoxLayout(tab)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body = QWidget()
        lay = QVBoxLayout(body)
        intro = QLabel(
            "<h3>Data &amp; Output</h3>Where the results go and which data are used. "
            "Everything ticked here is downloaded for the study area when the Hazard "
            "Classification needs it. <b>Download data now</b> also saves "
            "each data set to <i>outputs/{}</i> - for the study area or only where the flood "
            "is - and adds it to the project. Layers from a work server: add them to the "
            "project and pick them in the lists; a service address can be changed with "
            "<i>Edit data sources</i>. The study area comes from the Depth raster on the "
            "Setup tab.".format(SUBDIR_DATA))
        intro.setWordWrap(True)
        lay.addWidget(intro)

        # 1. output options (was Setup tab group 3)
        output_group = QGroupBox("1. Output options")
        form = QFormLayout()
        self.output_folder_edit = QLineEdit()
        self.output_folder_edit.setReadOnly(True)
        self.output_folder_edit.setPlaceholderText("<dam folder>/outputs - pick the dam folder "
                                                   "on the Setup tab")
        self.output_browse_btn = QPushButton("Browse...")
        self.output_browse_btn.clicked.connect(self.browse_output)
        form.addRow("Output folder:", self._inline_row(self.output_folder_edit,
                                                       self.output_browse_btn))
        self.extent_spin = QSpinBox()
        self.extent_spin.setRange(0, 5280)
        self.extent_spin.setValue(DEFAULT_EXTENT_BUFFER_FT)
        self.extent_spin.setSuffix(" ft")
        self.extent_spin.setToolTip(
            "Study area = the Depth raster's extent grown by this distance. Structures and "
            "all downloads outside it are left out; features inside it are kept.")
        form.addRow("Study area: raster extent +", self.extent_spin)
        self.keep_dry_check = QCheckBox(
            "Keep features inside the study area that have no flood values")
        self.keep_dry_check.setChecked(True)
        self.keep_dry_check.setToolTip(
            "Checked: every structure inside the study area is written, with empty depth / "
            "velocity / arrival where the rasters have no data.\nUnchecked: only structures "
            "with at least one raster value are written.")
        form.addRow("", self.keep_dry_check)
        self.spreadsheets_check = QCheckBox(
            "Also write CSV / XLSX copies of the result layers (GPKG is always written)")
        self.spreadsheets_check.setChecked(True)
        form.addRow("", self.spreadsheets_check)
        output_group.setLayout(form)
        lay.addWidget(output_group)

        # 2. download area
        area = QGroupBox("2. Download area (Download data now)")
        aform = QFormLayout()
        self.data_area_combo = QComboBox()
        for text, key in AREA_CHOICES:
            self.data_area_combo.addItem(text, key)
        self.data_area_combo.setToolTip(
            "Flooded area: structures, facilities, dams and towns only where the depth "
            "raster is wet, plus the flood buffer. Roads, rail and NHD flowlines always keep "
            "the whole study area (sole access and the travel-time trace need the network "
            "beyond the flood).")
        aform.addRow("Save data for:", self.data_area_combo)
        self.flood_buffer_spin = QSpinBox()
        self.flood_buffer_spin.setRange(0, 5280)
        self.flood_buffer_spin.setValue(int(DEFAULT_FLOOD_BUFFER_FT))
        self.flood_buffer_spin.setSuffix(" ft")
        self.flood_buffer_spin.setToolTip("Distance around the wet cells (flooded-area "
                                          "choice only).")
        aform.addRow("Flood buffer:", self.flood_buffer_spin)
        self.data_save_check = QCheckBox(
            "Run everything saves the data sets to outputs/{} first".format(SUBDIR_DATA))
        self.data_save_check.setChecked(True)
        aform.addRow("", self.data_save_check)
        area.setLayout(aform)
        lay.addWidget(area)
        self.data_area_combo.currentIndexChanged.connect(self._data_area_changed)

        # v2.9: search a folder of the computer for the data sets of groups 3-6
        lay.addWidget(self._build_datafolder_group())

        # 3. structures
        sgrp = QGroupBox("3. Structures")
        sform = QFormLayout()
        self.nsi_download_check = QCheckBox("USACE National Structure Inventory (NSI) points "
                                            "(internet)")
        self.nsi_download_check.setChecked(True)
        sform.addRow("", self.nsi_download_check)
        self.addr_download_check = QCheckBox(
            "ND GIS Hub site / structure address points when no ND GIS Hub layer is loaded "
            "(internet) - every addressed structure is habitable")
        self.addr_download_check.setChecked(True)
        self.addr_download_check.setToolTip(
            "NDGISHUB SiteStructure Address Points (NENA NG9-1-1), from\n" +
            ND_ADDRESS_POINTS_URL)
        sform.addRow("", self.addr_download_check)
        self.usa_download_check = QCheckBox(
            "FEMA USA Structures outlines - saved as <Dam>_FEMA_Structures.gpkg when that "
            "file is missing (internet)")
        self.usa_download_check.setChecked(True)
        self.usa_download_check.setToolTip(
            "The building outlines normally come from FEMA_Structures.gpkg in the outputs "
            "folder (FEMA USA Structures for ND). When that file is missing, the study area's "
            "outlines are downloaded once and saved as <Dam>_FEMA_Structures.gpkg.")
        sform.addRow("", self.usa_download_check)
        self.camps_download_check = QCheckBox(
            "Campgrounds and RV parks - overnight recreation (OpenStreetMap) (internet)")
        self.camps_download_check.setChecked(True)
        self.camps_download_check.setToolTip(
            "Campgrounds / RV parks are habitable under ND 5.2.1.1 (overnight recreation). "
            "Each one is evaluated over its whole area with the mobile-home curve; persons "
            "= sites x persons per dwelling when OSM gives the number of sites.")
        sform.addRow("", self.camps_download_check)
        self.outline_combo = QComboBox()
        sform.addRow("Extra building outlines (layer):", self.outline_combo)
        sgrp.setLayout(sform)
        lay.addWidget(sgrp)

        # 4. roads, rail, crossings
        rgrp = QGroupBox("4. Roads, rail and crossings")
        rform = QFormLayout()
        self.road_combo = QComboBox()
        self.aadt_combo = QComboBox()
        self.rail_combo = QComboBox()
        self.crossing_combo = QComboBox()
        rform.addRow("Road lines:", self.road_combo)
        rform.addRow("AADT layer (optional):", self.aadt_combo)
        rform.addRow("Rail lines:", self.rail_combo)
        rform.addRow("Rail crossings (trains/day):", self.crossing_combo)
        self.crossings_download_check = QCheckBox(
            "Bridges, culverts, low-water crossings and rail bridges (National Bridge "
            "Inventory, OpenStreetMap) (internet)")
        self.crossings_download_check.setChecked(True)
        rform.addRow("", self.crossings_download_check)
        rgrp.setLayout(rform)
        lay.addWidget(rgrp)

        # 5. dams, lifelines, other layers
        fgrp = QGroupBox("5. Dams, lifeline facilities and other layers")
        fform = QFormLayout()
        self.dams_download_check = QCheckBox(
            "National Inventory of Dams - other dams in the flood (dams in series, 5.3(c)) "
            "(internet)")
        self.dams_download_check.setChecked(True)
        self.dams_download_check.setToolTip("USACE NID points in the study area:\n" + NID_URL)
        fform.addRow("", self.dams_download_check)
        self.dam_layer_combo = QComboBox()
        fform.addRow("Dam points (optional):", self.dam_layer_combo)
        self.subject_nid_edit = QLineEdit()
        self.subject_nid_edit.setPlaceholderText("NID ID of the dam being classified, e.g. "
                                                 "ND00123 (optional)")
        self.subject_nid_edit.setToolTip("This dam is left out of the dams-in-series list. "
                                         "Without it, a dam with the same name is left out.")
        fform.addRow("This dam's NID ID:", self.subject_nid_edit)
        self.lifelines_download_check = QCheckBox(
            "Lifeline facilities - hospitals, care homes, schools, fire / EMS / police, "
            "utilities (ND GIS Hub, EIA, OpenStreetMap) (internet). HIFLD: download it from "
            "hsdl.org/hifld and search its folder (Data folder, above)")
        self.lifelines_download_check.setChecked(True)
        fform.addRow("", self.lifelines_download_check)
        self.lifeline_combo = QComboBox()
        self.lifeline_combo.setToolTip("Your own lifeline / critical facility layer (points or "
                                       "outlines). A 'category' or 'type' field is used as the "
                                       "facility type.")
        fform.addRow("Lifeline facilities (optional):", self.lifeline_combo)
        self.boundary_combo = QComboBox()
        self.boundary_combo.setToolTip(
            "2D flow area / model domain polygon. Flooded cells on its edge are reported "
            "(the flood may continue beyond the model). Without it, the depth raster's edge "
            "is checked.")
        fform.addRow("Model boundary polygon (optional):", self.boundary_combo)
        self.parcel_combo = QComboBox()
        self.parcel_combo.setToolTip(
            "County parcels (polygons). Vacant parcels up to the size limit where a new house "
            "would be in the danger zone are listed for the future-development review "
            "(5.3(b)). They never change the class.")
        fform.addRow("Parcels - future development (optional):", self.parcel_combo)
        fgrp.setLayout(fform)
        lay.addWidget(fgrp)

        # 6. channel (EAP travel-time table)
        cgrp = QGroupBox("6. Channel - EAP travel-time table")
        cform = QFormLayout()
        self.centerline_combo = QComboBox()
        self.centerline_combo.setToolTip(
            "A line along the channel from the dam downstream (parts are joined). Without "
            "one, USGS NHD flowlines are downloaded and traced downstream from the dam's NID "
            "point (or the first cell the flood reaches).")
        cform.addRow("Channel centerline:", self.centerline_combo)
        self.flowlines_download_check = QCheckBox(
            "USGS NHD flowlines and OpenStreetMap towns (internet)")
        self.flowlines_download_check.setChecked(True)
        cform.addRow("", self.flowlines_download_check)
        cgrp.setLayout(cform)
        lay.addWidget(cgrp)

        # 7. offline data pack (v2.6.1) - a setting of this computer, not the project
        ogrp = QGroupBox("7. Offline data pack - run without internet access")
        oform = QFormLayout()
        opack = QLabel(
            "A pack holds every data set above for all of North Dakota. Build it once on a "
            "computer that is online (<b>Build offline pack...</b>, or build_nd_pack.py in "
            "the plugin folder), copy the zip here and pick it. Data sets in the pack are "
            "read from it instead of the internet. Kept for this computer, not the project.")
        opack.setWordWrap(True)
        oform.addRow(opack)
        self.pack_path_edit = QLineEdit()
        self.pack_path_edit.setPlaceholderText("nd_offline_pack.zip (or its unpacked folder)")
        self.pack_path_edit.editingFinished.connect(self._pack_changed)
        self.pack_browse_btn = QPushButton("Browse...")
        self.pack_browse_btn.clicked.connect(self._browse_pack)
        self.pack_clear_btn = QPushButton("Clear")
        self.pack_clear_btn.clicked.connect(self._clear_pack)
        row7 = QHBoxLayout()
        row7.addWidget(self.pack_path_edit)
        row7.addWidget(self.pack_browse_btn)
        row7.addWidget(self.pack_clear_btn)
        w7 = QWidget()
        w7.setLayout(row7)
        row7.setContentsMargins(0, 0, 0, 0)
        oform.addRow("Pack:", w7)
        self.offline_only_check = QCheckBox(
            "Offline only - never contact the internet (the pack, project layers and "
            "earlier downloads only)")
        self.offline_only_check.setToolTip(
            "Unticked: data sets missing from the pack - or a study area reaching beyond "
            "the pack's area - are downloaded as usual when the internet answers.")
        self.offline_only_check.toggled.connect(self._pack_changed)
        oform.addRow("", self.offline_only_check)
        self.pack_status_label = QLabel("")
        self.pack_status_label.setWordWrap(True)
        oform.addRow("Status:", self.pack_status_label)
        self.pack_build_btn = QPushButton("Build offline pack...")
        self.pack_build_btn.setToolTip(
            "Downloads every data set for all of North Dakota into one zip (needs internet; "
            "an hour or more; a large file). An interrupted build goes on where it "
            "stopped when you start it again with the same file name.")
        self.pack_build_btn.clicked.connect(self._on_build_pack)
        oform.addRow("", self.pack_build_btn)
        ogrp.setLayout(oform)
        lay.addWidget(ogrp)
        self._load_pack_settings()
        lay.addStretch()
        scroll.setWidget(body)
        outer.addWidget(scroll)

        row = QHBoxLayout()
        # v2.9: the button moved here from the Setup & Run tab
        self.run_all_btn = QPushButton("Run everything")
        self.run_all_btn.setToolTip("Checks the inputs, saves the data sets to outputs/Data "
                                    "(when ticked on this tab) and runs the Hazard "
                                    "Classification - one inventory of every structure, the "
                                    "ACER zone rasters, the ND criteria, the report and the "
                                    "EAP outputs.")
        self.run_all_btn.clicked.connect(self._on_run_all)
        row.addWidget(self.run_all_btn)
        self.data_download_btn = QPushButton("Download data now")
        self.data_download_btn.setToolTip(
            "Downloads every data set ticked above for the study area (or the flooded area) "
            "and saves it to outputs/{} (one GeoPackage each, plus Downloaded_Data.csv)."
            .format(SUBDIR_DATA))
        self.data_download_btn.clicked.connect(self._on_download_data)
        row.addWidget(self.data_download_btn)
        self.refresh_layers_btn = QPushButton("Refresh layer lists")
        self.refresh_layers_btn.clicked.connect(self._refresh_layer_combos)
        row.addWidget(self.refresh_layers_btn)
        self.data_sources_btn = QPushButton("Edit data sources...")
        self.data_sources_btn.setToolTip(
            "Opens the file with every download address (NSI, ND GIS Hub, NID, HIFLD, NBI ...). "
            "Point an entry at your work server's service when it has the same layer, then "
            "restart QGIS.")
        self.data_sources_btn.clicked.connect(self._on_edit_sources)
        row.addWidget(self.data_sources_btn)
        self.open_data_btn = QPushButton("Open data folder")
        self.open_data_btn.clicked.connect(self._open_data_folder)
        row.addWidget(self.open_data_btn)
        row.addStretch()
        outer.addLayout(row)
        self._data_area_changed()
        return tab

    # ------------------------------------------------ offline pack (v2.6.1) --
    def _load_pack_settings(self):
        from .download import offline_config, suite_pack
        path, only = offline_config(fallback=False)
        sp = suite_pack()
        self.pack_path_edit.setPlaceholderText(
            "suite data folder: {}".format(sp) if sp else "nd_offline_pack.zip (or its unpacked folder)")
        self.pack_path_edit.blockSignals(True)
        self.offline_only_check.blockSignals(True)
        self.pack_path_edit.setText(path)
        self.offline_only_check.setChecked(only)
        self.pack_path_edit.blockSignals(False)
        self.offline_only_check.blockSignals(False)
        self._show_pack_status()

    def _pack_changed(self, *args):
        from .download import set_offline_config
        try:
            set_offline_config(self.pack_path_edit.text().strip(),
                               self.offline_only_check.isChecked())
        except Exception as e:
            log_message("Offline settings not saved: {}".format(e))
        self._show_pack_status()

    def _show_pack_status(self):
        from .offline_pack import describe
        from .download import suite_pack
        path = self.pack_path_edit.text().strip()
        if not path and suite_pack():
            path = suite_pack()
            text = "Suite data folder pack. " + describe(path)
        else:
            text = describe(path)
        if self.offline_only_check.isChecked():
            text += "  Offline only: no internet requests."
        self.pack_status_label.setText(text)

    def _browse_pack(self, *args):
        start = self.pack_path_edit.text().strip() or os.path.expanduser("~")
        path, _f = QFileDialog.getOpenFileName(
            self, "Offline data pack", start,
            "Offline data pack (*.zip *.sqlite);;All files (*)")
        if path:
            self.pack_path_edit.setText(path)
            self._pack_changed()

    def _clear_pack(self, *args):
        self.pack_path_edit.setText("")
        self._pack_changed()

    def _on_build_pack(self, *args):
        if self._busy:
            return
        start = self.pack_path_edit.text().strip()
        if not start.lower().endswith(".zip"):
            from .download import suite_pack
            from .damsuite import suitedata as _SD
            start = suite_pack()
            if not start.lower().endswith(".zip"):
                start = os.path.join(_SD.data_folder() or os.path.expanduser("~"),
                                     "nd_offline_pack.zip")
        path, _f = QFileDialog.getSaveFileName(self, "Build offline data pack", start,
                                               "Zip (*.zip)")
        if not path:
            return
        if not path.lower().endswith(".zip"):
            path += ".zip"
        if QMessageBox.question(
                self, "Build offline data pack",
                "Download every data set for all of North Dakota into\n{}\n\nThis needs "
                "internet access and takes a long time (an hour or more; a large file). "
                "QGIS stays usable. Start?".format(path)) != MB_YES:
            return
        from .build_nd_pack import build
        from .constants import sources_file_path

        def work(progress, log):
            src = sources_file_path()
            return build(path, progress=progress, log=log,
                         sources_file=src if os.path.isfile(src) else None)

        def done(outcome):
            if outcome.cancelled:
                self._update_status("Offline pack stopped - start it again with the same file "
                                    "to go on", False)
                return
            if outcome.error is not None:
                self._log(outcome.traceback)
                self._update_status("Offline pack FAILED: {}".format(outcome.error), False)
                QMessageBox.critical(self, "Build offline data pack", str(outcome.error))
                return
            man = outcome.result or {}
            ds = man.get("datasets") or {}
            bad = sorted(k for k, v in ds.items()
                         if v.get("status") not in ("complete", "partial", "not available"))
            part = sorted(k for k, v in ds.items()
                          if v.get("status") == "partial" or v.get("gaps"))
            self._update_status("Offline pack written: {}{}{}".format(
                os.path.basename(path), " - missing: " + ", ".join(bad) if bad else "",
                " - incomplete: " + ", ".join(part) if part else ""), not (bad or part))
            if QMessageBox.question(self, "Build offline data pack",
                                    "Use this pack on this computer too?") == MB_YES:
                self.pack_path_edit.setText(path)
                self._pack_changed()
        self._start_background("Building the offline data pack...", work, done,
                               project_bound=False)

    def _data_area_changed(self, *args):
        from .datafetch import AREA_FLOOD
        self.flood_buffer_spin.setEnabled(self.data_area_combo.currentData() == AREA_FLOOD)

    # ------------------------------------------------------------ config --
    def _data_cfg(self):
        """The download choices as pipeline cfg keys (shared by the
        classification and Download data now)."""
        from .classify_tab import _AUTO, _DOWNLOAD, _ROAD_RE, _RAIL_RE
        from .compat import GEOM_LINE
        road_choice = self.road_combo.currentData()
        road = self._resolve_layer(self.road_combo, _ROAD_RE, (GEOM_LINE,), exclude=_RAIL_RE)
        return {
            "download_nsi": self.nsi_download_check.isChecked(),
            "download_address": self.addr_download_check.isChecked(),
            "download_usa_structures": self.usa_download_check.isChecked(),
            "download_camps": self.camps_download_check.isChecked(),
            "download_roads": road is None and road_choice in (_AUTO, _DOWNLOAD),
            "download_rail": self.rail_combo.currentData() == _DOWNLOAD,
            "download_crossings": self.crossing_combo.currentData() == _DOWNLOAD,
            "download_crossings_structures": self.crossings_download_check.isChecked(),
            "download_dams": self.dams_download_check.isChecked(),
            "download_lifelines": self.lifelines_download_check.isChecked(),
            "download_flowlines": self.flowlines_download_check.isChecked(),
            "download_places": self.flowlines_download_check.isChecked(),
            "data_area": self.data_area_combo.currentData(),
            "flood_buffer_ft": float(self.flood_buffer_spin.value()),
            "add_to_project": True,
            "local_data": self._local_data_for_run(),      # v2.9: data folder picks
        }

    # ------------------------------------------------------------ actions --
    def _open_data_folder(self, *args):
        path = os.path.join(self.config_output_folder or "", SUBDIR_DATA)
        if not self.config_output_folder:
            QMessageBox.information(self, "Data", "Pick the dam folder on the Setup tab "
                                                  "first.")
            return
        os.makedirs(path, exist_ok=True)
        self._open_path(path)

    def _download_job(self):
        """Main-thread preparation of Download data now: the depth raster,
        the study area and the choices. Raises when there is no study area."""
        from .extent import make_extent_clip
        # only the depth raster is needed here (study area / flooded area);
        # a missing velocity raster is the classification's business
        path = self._raster_paths().get("depth") or ""
        depth = QgsRasterLayer(path, "Depth")
        if not depth.isValid():
            raise RuntimeError("The Depth raster failed to load: " + path)
        ctx = QgsProject.instance().transformContext()
        clip = make_extent_clip(depth, self.extent_spin.value(), ctx)
        if clip is None or clip.bbox_4326() is None:
            raise RuntimeError("No study area - the Depth raster has no usable extent / "
                               "coordinate system.")
        cfg = self._data_cfg()
        cfg["add_to_project"] = False           # the saved files are added by the caller
        if hasattr(self, "_cache_cfg"):
            cfg.update(self._cache_cfg())       # v2.8: per-tile download cache
        return {"cfg": cfg, "clip": clip, "depth": depth, "out": self.config_output_folder,
                "dam": self.config_dam_folder}

    def _add_downloaded(self, saved):
        """Add the data sets a background run saved (path, base) to the
        project's 'Downloaded data' group and refresh the layer lists."""
        from .constants import GRP_DATA
        from .helpers import add_layer_to_project
        from qgis.core import QgsVectorLayer
        for path, base in saved:
            lyr = QgsVectorLayer(path, base, "ogr")
            if lyr.isValid():
                add_layer_to_project(lyr, base, visibility=False, group=self._group(GRP_DATA))
        self._refresh_layer_combos()

    def _download_data_sync(self):
        """Download and save the data sets on the calling thread (scripts,
        tests). Returns the rows of datafetch.download_data."""
        from .datafetch import download_data
        job = self._download_job()
        job["cfg"]["add_to_project"] = True
        self._progress = self._make_progress("Downloading data...")
        rows = download_data(job["cfg"], job["clip"], job["depth"], job["out"], job["dam"],
                             progress=self._progress_tick, log=self._log)
        self._refresh_layer_combos()
        return rows

    def _on_download_data(self, *args):
        if self._busy:
            return
        if not self.config_output_folder:
            QMessageBox.warning(self, "Download data", "Pick the dam folder on the Setup tab "
                                                       "first.")
            return
        depth = self._raster_paths().get("depth")
        if not depth or not os.path.isfile(depth):
            QMessageBox.warning(self, "Download data", "The study area comes from the Depth "
                                                       "raster - select it on the Setup tab.")
            return
        try:
            job = self._download_job()
        except Exception as e:
            self._update_status("Download FAILED: {}".format(e), False)
            QMessageBox.critical(self, "Download data", str(e) or repr(e))
            return
        from .datafetch import download_data
        from .runner import release_outputs
        # the saved data sets are rewritten: a loaded layer would keep the
        # files locked (Windows) - taken out of the project, added again after
        n = release_outputs(self.config_output_folder, data_sets=True)
        if n:
            self._log(">>> {} layer(s) of outputs/{} taken out of the project (they are "
                      "written again)".format(n, SUBDIR_DATA))
        sink = []

        def work(progress, log):
            return download_data(job["cfg"], job["clip"], job["depth"], job["out"], job["dam"],
                                 progress=progress, log=log,
                                 add_layer=lambda path, base: sink.append((path, base)))

        def done(outcome):
            if outcome.cancelled:
                self._update_status("Download cancelled", False)
                self._log(">>> Download cancelled by user")
                return
            if outcome.error is not None:
                self._log(outcome.traceback)
                log_message(outcome.traceback, MSG_CRITICAL)
                self._update_status("Download FAILED: {}".format(outcome.error), False)
                QMessageBox.critical(self, "Download data",
                                     str(outcome.error) or repr(outcome.error))
                return
            self._add_downloaded(sink)
            self._report_downloads(outcome.result)
        self._start_background("Downloading data...", work, done)

    def _report_downloads(self, rows):
        saved = [r for r in rows if r[1]]
        failed = [r[0] for r in rows if r[1] is None and r[4] != "study area"
                  and r[4] != "flooded area"]
        self._update_status("Data: {} data set(s) saved to outputs/{}{}".format(
            len(saved), SUBDIR_DATA, " - {} failed".format(len(failed)) if failed else ""),
            not failed)
