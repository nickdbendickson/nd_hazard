# -*- coding: utf-8 -*-
"""The "Suite data and servers" window (1.5.0 of the suite: Hazard 2.17.0, Runner 3.9.0, Screening
0.16.0) - byte-identical in the three plugins, opened from Plugins > ND Dam Suite.

  * the suite data folder and the Dams folder, set once for the three plugins (damsuite.suitedata)
  * 1.10.0: the data source - the offline pack (and where it is) or download from the servers -
    shared by the three plugins
  * what the data folder holds, and "Update data" (the statewide offline pack, built by ND Dam
    Hazard's build_nd_pack when that plugin is installed; only stale data sets are fetched again)
  * the servers: for every kind of data the suite needs, which WMS / WMTS / WCS / ArcGIS REST
    service it comes from; "Add server..." (any WMS / ArcGIS REST address: its layers are listed
    from the server itself), "Test", "Add to map" (damsuite.services)
  * "Get data for this dam" / "... for all dams": <dam>/data/ and dam.json's data section, which
    every tool of the three plugins fills its empty inputs from (damsuite.damdata)

Logic is in damsuite (pure Python, tested); this file holds the widgets, the QGIS network access
(QgsBlockingNetworkRequest: QGIS's proxy and SSL settings apply) and the background tasks. Nothing
here has been shown in a real QGIS (Qt test doubles only).
"""
import importlib
import os
import sys
import traceback

from qgis.PyQt.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton, QTableWidget,
    QTableWidgetItem, QFileDialog, QMessageBox, QComboBox, QCheckBox, QTextEdit, QGroupBox,
    QInputDialog, QDoubleSpinBox, QFormLayout, QRadioButton, QButtonGroup,
)

from .damsuite import damdata as DD
from .damsuite import services as SV
from .damsuite import suitedata as SD
from .damsuite import versions as VS
from . import suite_data_tools as SDT

TITLE = "Suite data and servers"
try:                                    # Qt5 and Qt6 (QGIS 4) spell the answer differently
    _YES = QMessageBox.StandardButton.Yes
except AttributeError:                  # pragma: no cover
    _YES = QMessageBox.Yes
ROLE_COLUMNS = ("Data", "Used by", "Source", "Kind", "Checked")


# ------------------------------------------------------------------ network --
qgis_http_get = SDT.qgis_http_get          # QGIS's network stack (suite_data_tools)


# ------------------------------------------------------------- other plugins --
def _plugin_module(key, name):
    """A module of another suite plugin when that plugin is installed (None otherwise)."""
    folder = VS.PLUGINS.get(key)
    if not folder:
        return None
    try:
        return importlib.import_module("{}.{}".format(folder, name))
    except Exception:                                            # noqa
        return None


def make_package_fn(pack_path):
    """ND Dam Hazard's suite export (the standard schema with its habitability / AADT rules) as
    damdata's make_package, or None when ND Dam Hazard is not installed."""
    se = _plugin_module("ndhazard", "suiteexport")
    if se is None or not pack_path:
        return None

    def make(out_path, box):
        return se.export(out_path, se.open_source(pack_path), extent=box)
    return make


def reapply_hazard_sources():
    """After a server choice changed: ND Dam Hazard (when loaded) takes the new addresses."""
    mod = sys.modules.get("{}.constants".format(VS.PLUGINS["ndhazard"]))
    if mod is None:
        return False
    try:
        mod.apply_source_overrides()
        if hasattr(mod, "apply_suite_services"):
            mod.apply_suite_services()
        return True
    except Exception:                                            # noqa
        return False


# -------------------------------------------------------------------- tasks --
def run_in_background(description, fn, done):
    """fn(progress, cancelled) in a QgsTask; done(result, error_text) on the main thread. Without
    QgsTask (tests) fn runs at once."""
    try:
        from qgis.core import QgsApplication, QgsTask
    except Exception:                                            # noqa
        QgsTask = None
    if QgsTask is None or not hasattr(QgsTask, "fromFunction"):
        try:
            done(fn(lambda p, m="": None, lambda: False), None)
        except Exception as e:                                   # noqa
            done(None, "{}\n{}".format(e, traceback.format_exc()))
        return None

    def work(task):
        def prog(p, m=""):
            task.setProgress(max(0.0, min(100.0, float(p))))
        return fn(prog, task.isCanceled)

    def finished(exception, result=None):
        if exception is not None:
            done(None, str(exception))
        else:
            done(result, None)
    task = QgsTask.fromFunction(description, work, on_finished=finished)
    QgsApplication.taskManager().addTask(task)
    return task


# ------------------------------------------------------------------- window --
class SuiteDataDialog(QDialog):
    def __init__(self, parent=None, iface=None, opened_from="", dam_folder=""):
        super().__init__(parent)
        self.iface = iface
        self.opened_from = opened_from
        self.catalog = SV.load()
        self.tasks = []
        self.setWindowTitle(TITLE)
        self.setMinimumWidth(920)
        try:
            from qgis.PyQt.QtWidgets import QApplication
            self.setMaximumHeight(max(600, QApplication.primaryScreen().geometry().height() - 80))
        except Exception:
            pass
        self._build()
        self.data_edit.setText(SD.data_folder())
        self.dams_edit.setText(SD.dams_folder())
        self._load_source()
        self.refresh()
        if dam_folder:
            self.select_dam(dam_folder)

    # ------------------------------------------------------------------ build --
    def _folder_row(self, lay, label, tip, browse):
        row = QHBoxLayout()
        lab = QLabel(label)
        edit = QLineEdit()
        edit.setToolTip(tip)
        b = QPushButton("Browse...")
        b.clicked.connect(lambda _c=False: browse())
        row.addWidget(lab)
        row.addWidget(edit, 1)
        row.addWidget(b)
        lay.addLayout(row)
        return edit

    def _build(self):
        lay = QVBoxLayout()
        intro = QLabel(
            "Choose the folders once: the three ND dam plugins read their data from the suite data "
            "folder and open on the same Dams folder. \"Get data for this dam\" fills the dam's "
            "data/ folder, and every tool then fills its empty inputs from it. Your own choices in "
            "a tool are never replaced.")
        intro.setWordWrap(True)
        lay.addWidget(intro)
        self.data_edit = self._folder_row(
            lay, "Suite data folder:", "Holds the offline pack, the terrain tile cache, your DEM "
            "(dem/), land cover (nlcd/) and the DWR / HIFLD files you drop in (dwr/, hifld/).",
            self._browse_data)
        self.data_edit.editingFinished.connect(self._data_changed)
        self.dams_edit = self._folder_row(
            lay, "Dams folder:", "One folder per dam, named after the dam ID (the suite's Dams "
            "folder: the three plugins open on it).", self._browse_dams)
        self.dams_edit.editingFinished.connect(self._dams_changed)

        # 1.10.0: where the data comes from (the three plugins share it)
        sbox0 = QGroupBox("Data source (shared by the three plugins)")
        s0 = QVBoxLayout()
        row = QHBoxLayout()
        self.source_pack_radio = QRadioButton("Offline pack")
        self.source_pack_radio.setToolTip("Every data set the pack holds is read from it (no "
                                          "internet needed for those).")
        self.source_download_radio = QRadioButton("Download from the servers")
        self.source_download_radio.setToolTip("The pack is not read: the data sets come from the "
                                              "servers listed below.")
        self._source_group = QButtonGroup(self)
        self._source_group.addButton(self.source_pack_radio)
        self._source_group.addButton(self.source_download_radio)
        row.addWidget(self.source_pack_radio)
        row.addWidget(self.source_download_radio)
        row.addStretch(1)
        s0.addLayout(row)
        row = QHBoxLayout()
        row.addWidget(QLabel("Pack location:"))
        self.pack_edit = QLineEdit()
        self.pack_edit.setPlaceholderText("the ND_Dam_Suite_Pack folder (empty = the pack in the "
                                          "suite data folder)")
        self.pack_edit.setToolTip("The offline pack: a folder with one GeoPackage per layer and "
                                  "LAYERS.txt (or an older nd_offline_pack.zip).")
        self.pack_edit.editingFinished.connect(self._pack_changed)
        row.addWidget(self.pack_edit, 1)
        for text, tip, slot in (
                ("Folder...", "Pick the pack folder (the one holding manifest.json and "
                 "LAYERS.txt).", self._browse_pack),
                ("Zip...", "Pick an older one-file pack (nd_offline_pack.zip).",
                 self._browse_pack_zip),
                ("Open LAYERS.txt", "The list of the pack's layers and their sources.",
                 self._open_layers_txt)):
            b = QPushButton(text)
            b.setToolTip(tip)
            b.clicked.connect(lambda _c=False, f=slot: f())
            row.addWidget(b)
            if text == "Open LAYERS.txt":
                self.layers_btn = b
            elif text == "Folder...":
                self.pack_folder_btn = b
            else:
                self.pack_zip_btn = b
        s0.addLayout(row)
        sbox0.setLayout(s0)
        lay.addWidget(sbox0)

        box = QGroupBox("What the data folder holds")
        bl = QVBoxLayout()
        self.status_text = QTextEdit()
        self.status_text.setReadOnly(True)
        self.status_text.setMaximumHeight(130)
        bl.addWidget(self.status_text)
        row = QHBoxLayout()
        self.update_btn = QPushButton("Update data...")
        self.update_btn.setToolTip("Build the statewide offline pack in the data folder, or fetch "
                                   "again only its data sets that are old or incomplete (needs ND "
                                   "Dam Hazard installed and internet access; it takes a long time "
                                   "the first time, and QGIS stays usable).")
        self.update_btn.clicked.connect(lambda _c=False: self.update_data())
        self.open_btn = QPushButton("Open the folder")
        self.open_btn.clicked.connect(lambda _c=False: self._open(self.data_edit.text()))
        row.addWidget(self.update_btn)
        row.addWidget(self.open_btn)
        row.addStretch(1)
        bl.addLayout(row)
        box.setLayout(bl)
        lay.addWidget(box)

        sbox = QGroupBox("Servers: where each kind of data comes from")
        sl = QVBoxLayout()
        self.table = QTableWidget()
        self.table.setColumnCount(len(ROLE_COLUMNS))
        try:
            self.table.setHorizontalHeaderLabels(list(ROLE_COLUMNS))
        except Exception:                                        # noqa
            pass
        sl.addWidget(self.table, 1)
        row = QHBoxLayout()
        for text, tip, slot in (
                ("Add server...", "Add a WMS, WMTS, WCS or ArcGIS REST address (or a file) for "
                 "one kind of data; its layers are listed from the server.", self.add_server),
                ("Test", "Ask the selected row's server whether it answers and offers the layer.",
                 self.test_selected),
                ("Browse layers...", "Pick another layer of the selected row's server.",
                 self.browse_selected),
                ("Add to map", "Add the selected row's service to the map as a layer.",
                 self.add_selected_to_map),
                ("Remove", "Remove your server (a built-in one is hidden).", self.remove_selected),
                ("Show built-in again", "Show the built-in servers you hid.", self.unhide)):
            b = QPushButton(text)
            b.setToolTip(tip)
            b.clicked.connect(lambda _c=False, f=slot: f())
            row.addWidget(b)
        row.addStretch(1)
        sl.addLayout(row)
        self.servers_note = QLabel("")
        self.servers_note.setWordWrap(True)
        sl.addWidget(self.servers_note)
        sbox.setLayout(sl)
        lay.addWidget(sbox, 1)

        dbox = QGroupBox("Get data for this dam")
        dl = QVBoxLayout()
        row = QHBoxLayout()
        row.addWidget(QLabel("Dam:"))
        self.dam_combo = QComboBox()
        self.dam_combo.setToolTip("The dam folders of the Dams folder (folders with a dam.json).")
        row.addWidget(self.dam_combo, 1)
        self.overwrite_chk = QCheckBox("Replace data already there")
        row.addWidget(self.overwrite_chk)
        dl.addLayout(row)
        form = QFormLayout()
        self.vec_spin = QDoubleSpinBox()
        self.vec_spin.setRange(1.0, 100.0)
        self.vec_spin.setValue(DD.DEFAULT_VECTOR_HALF_MI)
        self.vec_spin.setSuffix(" mi")
        self.vec_spin.setToolTip("Structures, roads, streams ... within this distance of the dam.")
        self.dem_spin = QDoubleSpinBox()
        self.dem_spin.setRange(1.0, 100.0)
        self.dem_spin.setValue(DD.DEFAULT_DEM_HALF_MI)
        self.dem_spin.setSuffix(" mi")
        self.dem_spin.setToolTip("Terrain within this distance of the dam (the Breach Runner's "
                                 "domain can grow to 60 mi along the valley).")
        self.cell_spin = QDoubleSpinBox()
        self.cell_spin.setRange(1.0, 30.0)
        self.cell_spin.setValue(DD.DEFAULT_DEM_CELL_M)
        self.cell_spin.setSuffix(" m")
        self.cell_spin.setToolTip("Cell size of terrain downloaded from a service (10 m = 32.8 ft). "
                                  "Your own DEM in dem/ keeps its own cells.")
        form.addRow("Data within:", self.vec_spin)
        form.addRow("Terrain within:", self.dem_spin)
        form.addRow("Terrain cell size (downloads):", self.cell_spin)
        dl.addLayout(form)
        row = QHBoxLayout()
        self.get_btn = QPushButton("Get data for this dam")
        self.get_btn.clicked.connect(lambda _c=False: self.get_data(all_dams=False))
        self.get_all_btn = QPushButton("Get data for all dams")
        self.get_all_btn.clicked.connect(lambda _c=False: self.get_data(all_dams=True))
        row.addWidget(self.get_btn)
        row.addWidget(self.get_all_btn)
        row.addStretch(1)
        dl.addLayout(row)
        dbox.setLayout(dl)
        lay.addWidget(dbox)

        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setMaximumHeight(120)
        lay.addWidget(self.log_text)
        self.setLayout(lay)

    # ---------------------------------------------------------------- folders --
    def log(self, text):
        try:
            self.log_text.append(str(text))
        except Exception:                                        # noqa
            pass

    def _open(self, path):
        if path and os.path.isdir(path):
            from qgis.PyQt.QtCore import QUrl
            from qgis.PyQt.QtGui import QDesktopServices
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def _browse_data(self):
        d = QFileDialog.getExistingDirectory(self, "Suite data folder",
                                             self.data_edit.text().strip() or os.path.expanduser("~"))
        if d:
            self.data_edit.setText(d)
            self._data_changed()

    def _browse_dams(self):
        d = QFileDialog.getExistingDirectory(self, "Dams folder", self.dams_edit.text().strip() or
                                             os.path.expanduser("~"))
        if d:
            self.dams_edit.setText(d)
            self._dams_changed()

    def _data_changed(self, *a):
        path = self.data_edit.text().strip()
        if path == SD.data_folder():
            return
        try:
            SD.set_data_folder(path)
        except OSError as e:
            QMessageBox.warning(self, TITLE, "The folder could not be created: {}".format(e))
            return
        self.catalog = SV.load()               # services.json lives in the data folder
        reapply_hazard_sources()
        self.refresh()

    def _dams_changed(self, *a):
        SD.set_dams_folder(self.dams_edit.text().strip())
        self.refresh_dams()

    # ------------------------------------------------------------ data source --
    def _load_source(self):
        download = SD.data_source() == SD.SOURCE_DOWNLOAD
        for w in (self.source_pack_radio, self.source_download_radio):
            w.blockSignals(True)
        self.source_pack_radio.setChecked(not download)
        self.source_download_radio.setChecked(download)
        for w in (self.source_pack_radio, self.source_download_radio):
            w.blockSignals(False)
        try:
            self.source_pack_radio.toggled.connect(self._source_changed)
        except Exception:                                        # noqa
            pass
        self.pack_edit.setText(SD.pack_location())
        self._enable_pack()

    def _enable_pack(self):
        on = self.source_pack_radio.isChecked()
        for w in (self.pack_edit, self.pack_folder_btn, self.pack_zip_btn):
            w.setEnabled(on)

    def _source_changed(self, *a):
        SD.set_data_source(SD.SOURCE_PACK if self.source_pack_radio.isChecked() else
                           SD.SOURCE_DOWNLOAD)
        self.log("Data source: {}".format(SD.SOURCE_LABELS[SD.data_source()]))
        self._enable_pack()
        self.refresh()

    def _pack_changed(self, *a):
        path = self.pack_edit.text().strip()
        if path == SD.pack_location():
            return
        SD.set_pack_location(path)
        self.refresh()

    def _browse_pack(self):
        d = QFileDialog.getExistingDirectory(self, "Offline pack folder",
                                             self.pack_edit.text().strip() or
                                             self.data_edit.text().strip() or os.path.expanduser("~"))
        if d:
            self.pack_edit.setText(d)
            self._pack_changed()

    def _browse_pack_zip(self):
        p, _f = QFileDialog.getOpenFileName(self, "Offline pack (older one-file pack)",
                                            self.data_edit.text().strip() or os.path.expanduser("~"),
                                            "Offline pack (*.zip *.sqlite);;All files (*)")
        if p:
            self.pack_edit.setText(p)
            self._pack_changed()

    def _open_layers_txt(self):
        pack = SD.active_pack(self.data_edit.text().strip()) if \
            SD.data_source() == SD.SOURCE_PACK else (SD.pack_location() or
                                                     SD.find_pack(self.data_edit.text().strip()))
        p = os.path.join(pack, "LAYERS.txt") if pack and os.path.isdir(pack) else ""
        if not p or not os.path.isfile(p):
            QMessageBox.information(self, TITLE, "No LAYERS.txt: the pack is not a folder pack "
                                                 "(one GeoPackage per layer), or no pack is "
                                                 "chosen.")
            return
        from qgis.PyQt.QtCore import QUrl
        from qgis.PyQt.QtGui import QDesktopServices
        QDesktopServices.openUrl(QUrl.fromLocalFile(p))

    # ---------------------------------------------------------------- refresh --
    def refresh(self):
        st = SD.status(self.data_edit.text().strip())
        self.status_text.setPlainText("\n".join(SD.describe(st)))
        self.update_btn.setEnabled(bool(st.get("exists")) or bool(SD.pack_location()))
        self.refresh_servers()
        self.refresh_dams()

    def refresh_dams(self):
        cur = self.dam_combo.currentData() if hasattr(self.dam_combo, "currentData") else None
        self.dam_combo.clear()
        for p in DD.dam_folders(self.dams_edit.text().strip()):
            self.dam_combo.addItem(os.path.basename(p), p)
        if cur:
            self.select_dam(cur)
        have = self.dam_combo.count() > 0
        self.get_btn.setEnabled(have)
        self.get_all_btn.setEnabled(have)

    def select_dam(self, folder):
        for i in range(self.dam_combo.count()):
            if os.path.normpath(str(self.dam_combo.itemData(i))) == os.path.normpath(folder):
                self.dam_combo.setCurrentIndex(i)
                return True
        return False

    def refresh_servers(self):
        roles = list(SV.ROLES)
        self.table.setRowCount(len(roles))
        self.combos = {}
        for i, role in enumerate(roles):
            label, rtype, used = SV.ROLES[role]
            self.table.setItem(i, 0, QTableWidgetItem(label))
            self.table.setItem(i, 1, QTableWidgetItem(used))
            combo = QComboBox()
            cands = self.catalog.services(role, data_only=(rtype != "display"))
            chosen = self.catalog.chosen(role)
            for s in cands:
                tag = "" if s.get("builtin") else " (yours)"
                if s.get("confidence") == "inferred":
                    tag += " - check with Test"
                combo.addItem(s["name"] + tag, s["id"])
            if not cands:
                combo.addItem("(no server: use a file in the data folder)", "")
            if chosen is not None:
                for j in range(combo.count()):
                    if combo.itemData(j) == chosen["id"]:
                        combo.setCurrentIndex(j)
            combo.currentIndexChanged.connect(lambda _i, r=role, c=combo: self._choose(r, c))
            self.combos[role] = combo
            self.table.setCellWidget(i, 2, combo)
            self.table.setItem(i, 3, QTableWidgetItem(chosen["kind"] if chosen else ""))
            self.table.setItem(i, 4, QTableWidgetItem(""))
        try:
            self.table.resizeColumnsToContents()
        except Exception:                                        # noqa
            pass
        note = "Servers marked \"check with Test\" were found in documentation but could not be " \
               "tried when this release was made; Test asks the server itself."
        if self.catalog.problem:
            note = self.catalog.problem + " - your servers were not loaded. " + note
        self.servers_note.setText(note)

    def _choose(self, role, combo):
        sid = combo.itemData(combo.currentIndex())
        try:
            self.catalog.choose(role, sid or None)
            self.catalog.save()
        except (SV.ServiceError, OSError) as e:
            QMessageBox.warning(self, TITLE, str(e))
            return
        reapply_hazard_sources()
        s = self.catalog.chosen(role)
        row = list(SV.ROLES).index(role)
        self.table.setItem(row, 3, QTableWidgetItem(s["kind"] if s else ""))
        self.table.setItem(row, 4, QTableWidgetItem(""))

    def _selected(self):
        row = self.table.currentRow()
        roles = list(SV.ROLES)
        if row is None or row < 0 or row >= len(roles):
            QMessageBox.information(self, TITLE, "Select a row of the servers table first.")
            return None, None
        role = roles[row]
        return role, self.catalog.chosen(role)

    # ---------------------------------------------------------------- servers --
    def add_server(self):
        url, ok = QInputDialog.getText(self, TITLE, "Server address (WMS / WMTS / WCS / ArcGIS REST "
                                                    "service or layer URL), or a file path:")
        url = (url or "").strip()
        if not ok or not url:
            return None
        kind = SV.detect_kind(url)
        if not kind:
            kinds = list(SV.KINDS)
            k, ok = QInputDialog.getItem(self, TITLE, "What kind of server is it?", kinds, 0, False)
            if not ok:
                return None
            kind = k
        labels = ["{} ({})".format(v[0], k) for k, v in SV.ROLES.items()]
        cur = self.table.currentRow()
        r, ok = QInputDialog.getItem(self, TITLE, "What data does it give?", labels,
                                     cur if cur is not None and 0 <= cur < len(labels) else 0, False)
        if not ok:
            return None
        role = list(SV.ROLES)[labels.index(r)]
        layer = ""
        tmp = {"kind": kind, "url": url, "name": url, "role": role, "layer": ""}
        if kind not in ("xyz", "file"):
            layer = self._pick_layer(tmp)
            if layer is None:
                return None
        name, ok = QInputDialog.getText(self, TITLE, "A name for it:", text=url.split("?")[0][-60:])
        if not ok:
            return None
        try:
            s = self.catalog.add(url, role, name=name, kind=kind, layer=layer)
            if SV.data_capable(s, role) or SV.ROLES[role][1] == "display":
                self.catalog.choose(role, s["id"])
            self.catalog.save()
        except (SV.ServiceError, OSError) as e:
            QMessageBox.warning(self, TITLE, str(e))
            return None
        reapply_hazard_sources()
        self.refresh_servers()
        self.log("Added {} for {}{}".format(s["name"], SV.ROLES[role][0],
                                            " (layer {})".format(layer) if layer else ""))
        return s

    def _pick_layer(self, service):
        """The layer the user picks from the server's own listing ("" = the service itself,
        None = cancelled or no answer)."""
        try:
            text = qgis_http_get(SV.capabilities_url(service))
            layers = SV.list_layers(service, text)
        except Exception as e:                                   # noqa
            QMessageBox.warning(self, TITLE, "The server did not list its layers: {}".format(e))
            return None
        if not layers:
            return ""
        if len(layers) == 1:
            return layers[0]["name"]
        items = [l["title"] if l["title"] == l["name"] or l["name"] in l["title"]
                 else "{} ({})".format(l["title"], l["name"]) for l in layers]
        it, ok = QInputDialog.getItem(self, TITLE, "Layer:", items, 0, False)
        if not ok:
            return None
        return layers[items.index(it)]["name"]

    def test_selected(self):
        role, s = self._selected()
        if s is None:
            return None
        ok, msg, _layers = SV.test_service(s, qgis_http_get)
        row = list(SV.ROLES).index(role)
        self.table.setItem(row, 4, QTableWidgetItem(("OK: " if ok else "PROBLEM: ") + msg))
        self.log("{} - {}: {}".format(s["name"], "OK" if ok else "PROBLEM", msg))
        return ok

    def browse_selected(self):
        role, s = self._selected()
        if s is None:
            return None
        if s.get("builtin"):
            QMessageBox.information(self, TITLE, "Built-in servers keep their layer. Use Add server "
                                                 "with the same address to pick another layer.")
            return None
        layer = self._pick_layer(s)
        if layer is None:
            return None
        self.catalog.update(s["id"], layer=layer)
        self.catalog.save()
        reapply_hazard_sources()
        self.refresh_servers()
        return layer

    def add_selected_to_map(self):
        _role, s = self._selected()
        if s is None:
            return None
        try:
            prov, uri, name = SV.qgis_layer_source(s)
        except SV.ServiceError as e:
            QMessageBox.information(self, TITLE, str(e))
            return None
        from qgis.core import QgsProject, QgsRasterLayer, QgsVectorLayer
        if prov in ("arcgisfeatureserver", "ogr"):
            lyr = QgsVectorLayer(uri, name, prov)
        else:
            lyr = QgsRasterLayer(uri, name, prov)
        if not lyr.isValid():
            QMessageBox.warning(self, TITLE, "QGIS could not open {} ({}).".format(name, prov))
            return None
        QgsProject.instance().addMapLayer(lyr)
        return lyr

    def remove_selected(self):
        role, s = self._selected()
        if s is None:
            return
        if QMessageBox.question(self, TITLE, "Remove {}?{}".format(
                s["name"], " (A built-in server is only hidden.)" if s.get("builtin") else "")) != \
                _YES:
            return
        self.catalog.remove(s["id"])
        self.catalog.save()
        reapply_hazard_sources()
        self.refresh_servers()

    def unhide(self):
        self.catalog.unhide_all()
        self.catalog.save()
        self.refresh_servers()

    # ------------------------------------------------------------------ data --
    def update_data(self):
        folder = self.data_edit.text().strip()
        bnp = _plugin_module("ndhazard", "build_nd_pack")
        if bnp is None:
            QMessageBox.information(self, TITLE, "Building the offline pack needs ND Dam Hazard "
                                                 "Classification Helper installed. You can also copy "
                                                 "a pack folder (ND_Dam_Suite_Pack) here and choose "
                                                 "it as the pack location.")
            return None
        # 1.10.0: the pack being used (its location, or the data folder's); a new pack is a
        # folder (one GeoPackage per layer) in the data folder
        pack = SD.active_pack(folder) or SD.pack_location() or \
            os.path.join(folder, SD.PACK_NAMES[0])
        st = SD.status(folder)
        stale = list(st.get("pack_stale") or [])
        if st.get("pack"):
            # data sets this ND Dam Hazard downloads that the pack does not hold yet (new in a
            # release, or a server chosen here since the pack was built)
            have = {t[0] for t in st.get("pack_sets") or []}
            try:
                stale += [t[0] for t in bnp.datasets() if t[0] not in have and t[0] not in stale]
            except Exception:                                    # noqa
                pass
        try:                     # 1.10.0: only what the builder fetches (not the DWR / HIFLD
            known = {t[0] for t in bnp.datasets()}      # files added to a pack from files)
            stale = [n for n in stale if n in known]
        except Exception:                                        # noqa
            pass
        if st.get("pack") and not stale:
            msg = "Every data set of the pack is current. Fetch everything again anyway?"
            only, refresh = None, True
        elif st.get("pack"):
            msg = "Fetch the {} data set(s) that are missing, old or incomplete:\n{}?".format(
                len(stale), ", ".join(stale))
            only, refresh = ",".join(stale), True
        else:
            msg = "Download every data set for all of North Dakota into\n{}\n\nThis needs internet " \
                  "access and takes a long time the first time (an hour or more; a large file). " \
                  "QGIS stays usable. Start?".format(pack)
            only, refresh = None, False
        if QMessageBox.question(self, TITLE, msg) != _YES:
            return None
        from .damsuite import gpkgpack as GP
        if os.path.isdir(pack) and not GP.is_gpkg_pack(pack):
            pack = os.path.join(folder, SD.PACK_NAMES[0])      # an unpacked one-file pack
        self.update_btn.setEnabled(False)
        self.log("Updating the offline pack: {}".format(pack))

        def work(progress, cancelled):
            def prog(p, m=""):
                if cancelled():
                    raise bnp.BuildCancelled("cancelled")
                progress(p, m)
            man = bnp.build(pack, only=only, refresh=refresh, progress=prog, log=lambda m: None)
            SD.note_update(folder, "offline_pack", source="build_nd_pack", count=len(
                (man or {}).get("datasets") or {}))
            return man

        def done(result, error):
            self.update_btn.setEnabled(True)
            if error:
                self.log("Update FAILED: " + error)
                QMessageBox.warning(self, TITLE, "Update data stopped: {}\n\nRun it again to go on "
                                                 "(finished data sets are kept).".format(
                                                     error.splitlines()[0]))
            else:
                bad = [k for k, v in ((result or {}).get("datasets") or {}).items()
                       if v.get("status") not in ("complete", "not available")]
                self.log("Offline pack updated{}".format(
                    " - not complete: " + ", ".join(sorted(bad)) if bad else ""))
            self.refresh()
        self.tasks.append(run_in_background("ND dam suite: update data", work, done))
        return True

    def get_data(self, all_dams=False):
        dams = self.dams_edit.text().strip()
        if all_dams:
            folders = DD.dam_folders(dams)
        else:
            f = self.dam_combo.itemData(self.dam_combo.currentIndex()) if \
                self.dam_combo.count() else None
            folders = [f] if f else []
        if not folders:
            QMessageBox.information(self, TITLE, "Choose the Dams folder and a dam first.")
            return None
        data = self.data_edit.text().strip()
        if not data:
            QMessageBox.information(self, TITLE, "Choose the suite data folder first.")
            return None
        if all_dams and QMessageBox.question(
                self, TITLE, "Get the data of {} dam(s)? Dams that already have their data keep "
                             "it{}.".format(len(folders), "" if not self.overwrite_chk.isChecked()
                                            else " - NO: 'Replace data already there' is ticked, "
                                                 "everything is fetched again")) != _YES:
            return None
        cat = self.catalog
        pack = SD.active_pack(data)                       # 1.10.0: "" in Download mode
        self.log("Data source: {}{}".format(SD.SOURCE_LABELS[SD.data_source()],
                                            " ({})".format(pack) if pack else ""))
        make = make_package_fn(pack)
        opts = dict(data_folder=data, catalog=cat, http_get=qgis_http_get, make_package=make,
                    vector_half_mi=self.vec_spin.value(), dem_half_mi=self.dem_spin.value(),
                    dem_cell_m=self.cell_spin.value(), overwrite=self.overwrite_chk.isChecked(),
                    version=_version_of(self.opened_from))
        self.get_btn.setEnabled(False)
        self.get_all_btn.setEnabled(False)

        def work(progress, cancelled):
            results = []
            for i, f in enumerate(folders):
                if cancelled():
                    break

                def prog(p, m="", i=i):
                    progress((100.0 * i + p) / len(folders), m)
                try:
                    r = DD.get_data_for_dam(f, progress=prog, cancel=cancelled, **opts)
                    results.append((f, r, None))
                except Exception as e:                           # noqa
                    results.append((f, None, str(e)))
            return results

        def done(results, error):
            self.get_btn.setEnabled(True)
            self.get_all_btn.setEnabled(True)
            if error:
                self.log("Get data FAILED: " + error)
                QMessageBox.warning(self, TITLE, error.splitlines()[0])
                return
            bad = 0
            for f, r, err in results or []:
                name = os.path.basename(f)
                if err:
                    bad += 1
                    self.log("{}: {}".format(name, err))
                    continue
                self.log("{}: {} written, {} failed, {} kept".format(
                    name, len(r["done"]), len(r["failed"]), len(r["skipped"])))
                for role, why in sorted(r["failed"].items()):
                    self.log("   {} failed: {}".format(DD.LABELS.get(role, role), why))
            if len(results or []) == 1 and not bad:
                self.log("The dam's tools now fill their empty inputs from {}".format(
                    os.path.join(results[0][0], DD.DATA_DIR)))
        self.tasks.append(run_in_background("ND dam suite: get data", work, done))
        return True


def _version_of(key):
    try:
        return VS.installed(key) or ""
    except Exception:                                            # noqa
        return ""


def open_suite_data(plugin, iface, opened_from, parent=None, dam_folder=""):
    """Open (or raise) the window for a plugin object; returns it."""
    dlg = getattr(plugin, "_suite_data_dialog", None)
    if dlg is not None:
        try:
            try:
                from qgis.PyQt import sip
            except ImportError:               # pragma: no cover
                import sip
            if sip.isdeleted(dlg):
                dlg = None
        except Exception:                                        # noqa - a test double
            pass
    if dlg is None:
        dlg = SuiteDataDialog(parent, iface, opened_from, dam_folder)
        plugin._suite_data_dialog = dlg
    else:
        dlg.refresh()
        if dam_folder:
            dlg.select_dam(dam_folder)
    dlg.show()
    try:
        dlg.raise_()
        dlg.activateWindow()
    except Exception:                                            # noqa
        pass
    return dlg
