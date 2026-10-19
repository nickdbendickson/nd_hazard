"""The ND Dam Breach Runner popup window (Plugins > ND Dam Breach Runner).

One window for a dam: the Dams folder and the dam ID at the top, what is in that dam's folder, and
every tool of the plugin on five tabs (Dam, Model, Data, Results, Checks). A button opens the
tool's own Processing dialog with the dam's folder (and the DEM chosen here) filled in, so every
value can be seen and changed before anything runs - the window runs nothing itself. The guided
wizard is the first button of the Dam tab.

The logic (folder layout, status, parameters) is popup_logic.py; this file only holds widgets. The
window's choices are saved with the QGIS project. The tools, the wizard and the Processing
provider are unchanged.

3.9.0: an empty DEM choice means the dam folder's DEM ("Get data for this dam" or the DEM
download box record it in dam.json), shown as "(from dam folder)"; an empty Dams folder starts as
the suite's (Suite data and servers) and the first one chosen becomes the suite's; the wizard
opens with the window's Dams folder, dam and DEM; the DEM download box takes the dam point from
dam.json, writes into <dam folder>/data/ and puts the DEM into the DEM choice, the map and
dam.json.

3.20.0, so the window fits a laptop screen (it asked for 902 px and wanted 1,433):
  * the 8-row DEM download box is a collapsible group and collapses itself once the dam has a
    DEM - which is most visits, since a DEM is downloaded once per dam. The tool buttons are
    the point of this window and they now start above the fold, "Run breach model" included.
  * the four model-run settings (grid size, simulated time, longest simulated time, force
    ANUGA) moved off the always-visible header onto an Advanced tab. They are engineering
    choices that are set once and then left alone, and every one of them can still be changed
    in the tool's own dialog.
  * the grey "(from dam folder) ..." note has its own full-width row, so it no longer draws
    over the row under it, and the long path is in its tooltip instead of on the row.
  * the scroll areas and the log may shrink, so the window's own minimum height does.
  * every grey Run button says why in its tooltip and in the status line under the tabs
    (core/runblock.py: the suite's set-up errors plus what this plugin can see cheaply).
  * when the dam was handed over by the screening, the page says what the screen ran and
    where that came from (core/screenhandoff.py).
"""
import json
import os

from qgis.PyQt.QtCore import QUrl
from qgis.PyQt.QtGui import QDesktopServices
from qgis.PyQt.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QGridLayout, QTabWidget, QWidget, QLabel, QLineEdit,
    QPushButton, QComboBox, QTextEdit, QFileDialog, QMessageBox, QDoubleSpinBox, QCheckBox,
    QGroupBox, QScrollArea,
)

from . import popup_logic as pl

try:                                    # Qt5 and Qt6 (QGIS 4) spell the answer differently
    _YES = QMessageBox.StandardButton.Yes
except AttributeError:                  # pragma: no cover
    _YES = QMessageBox.Yes

TITLE = "ND Dam Breach Runner"
ADVANCED_TAB = "Advanced"         # 3.20.0: the run settings that used to sit on the header
SCOPE = "NDBreachRunner"          # QgsProject entry scope
ENTRY = "state"
DISCLAIMER = ("Screening / engineering model results. Every threshold and parameter is the "
              "engineer's choice. ND Dam Hazard is the classification authority; the Department "
              "of Water Resources decides the official hazard class.")


def plugin_version():
    try:
        from .core import damjson
        return damjson.plugin_version()
    except Exception:                                            # noqa
        return ""


TAB_MIN_H = 170           # 3.20.0: a scroll area may shrink to this, so the window may too
LOG_MIN_H = 44
# 3.20.0: the window must fit a 768 px laptop screen with room for the title bar and the task
# bar; the layout's own minimum decides the rest
MIN_HEIGHT = 560


def _scroll_tab(w):
    """Wrap a tab widget in a QScrollArea so tall pages are reachable on small screens.

    3.20.0: with a small minimum height of its own - a scroll area that refuses to shrink
    makes the whole window refuse to shrink, which is how the window came to ask for 902 px."""
    sa = QScrollArea()
    sa.setWidgetResizable(True)
    sa.setWidget(w)
    try:
        sa.setMinimumHeight(TAB_MIN_H)
    except Exception:                                            # pragma: no cover - noqa
        pass
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


class NDBreachRunnerDialog(QDialog):
    """The popup window. `iface` is optional (used by the wizard)."""

    def __init__(self, parent=None, iface=None):
        super().__init__(parent)
        self.iface = iface
        self._loading = True
        self.setWindowTitle("{} {}".format(TITLE, plugin_version()).strip())
        self.setMinimumWidth(820)
        self.setMinimumHeight(MIN_HEIGHT)                        # 3.20.0: fits 768 px
        try:
            from qgis.PyQt.QtWidgets import QApplication
            self.setMaximumHeight(max(700, QApplication.primaryScreen().geometry().height() - 80))
        except Exception:
            pass
        self.tool_button = {}
        self.tool_tip = {}                # 3.20.0: each button's own text, before the reasons
        self._dem_box_collapsed_once = False
        self._dem_box_touched = False     # the user opened or closed it: never fight that
        self._pending_dem = ""
        self._auto_point = ""             # 3.9.0: what the DEM box filled in (replaced on a dam change)
        self._auto_out = ""
        self.dem_point = self.dem_out = None    # the DEM box (when Screening is installed)
        self._build()
        _escape_tab_mnemonics(self)
        self._load_state()
        self._loading = False
        try:
            self._load_settings_file(quiet=True)
        except Exception as e:                                   # noqa
            self._log("!!! settings file: {}".format(e))
        self._fill_dem()
        self.refresh()
        self._log_suite_versions()
        try:                                  # after the window is up
            from qgis.PyQt.QtCore import QTimer
            QTimer.singleShot(0, self._refresh_models)
        except Exception:                                        # noqa
            self._refresh_models()

    # ------------------------------------------------------------------ build --
    def _build(self):
        lay = QVBoxLayout()
        info = QLabel(DISCLAIMER)
        info.setWordWrap(True)
        lay.addWidget(info)

        grid = QGridLayout()
        self.dams_edit = QLineEdit()
        self.dams_edit.setToolTip("The Dams folder: each dam has a folder named after its ID in it "
                                  "(the Runner's own layout).")
        self.dams_edit.editingFinished.connect(self._changed)
        b = QPushButton("Browse...")
        b.clicked.connect(lambda _c=False: self._browse_folder(self.dams_edit))
        grid.addWidget(QLabel("Dams folder:"), 0, 0)
        grid.addWidget(self.dams_edit, 0, 1)
        grid.addWidget(b, 0, 2)

        self.dam_edit = QLineEdit()
        self.dam_edit.setToolTip("The dam ID: its folder is <Dams folder>/<Dam ID>.")
        self.dam_edit.editingFinished.connect(self._changed)
        self.dam_combo = QComboBox()
        self.dam_combo.setToolTip("Dams that already have a folder.")
        self.dam_combo.currentIndexChanged.connect(self._pick_dam)
        grid.addWidget(QLabel("Dam ID:"), 1, 0)
        grid.addWidget(self.dam_edit, 1, 1)
        grid.addWidget(self.dam_combo, 1, 2)

        self.dem_combo = QComboBox()
        self.dem_combo.setToolTip("The DEM filled into the model tools (a raster layer of the project "
                                  "or a file). Empty: choose it in the tool's dialog.")
        self.dem_combo.currentIndexChanged.connect(self._changed)
        b = QPushButton("File...")
        b.clicked.connect(lambda _c=False: self._browse_dem())
        grid.addWidget(QLabel("DEM:"), 2, 0)
        grid.addWidget(self.dem_combo, 2, 1)
        grid.addWidget(b, 2, 2)
        # 3.9.0: "(from dam folder) ..." when the choice is empty. 3.20.0: its own full-width
        # row (it used to share a row with the grid size and draw over it), grey, and the long
        # path lives in the tooltip instead of on the row.
        self.dem_note = QLabel("")
        self.dem_note.setWordWrap(True)
        self.dem_note.setStyleSheet("color: gray;")
        grid.addWidget(self.dem_note, 3, 0, 1, 3)
        # 3.20.0: where the values came from, when the screening handed this dam over
        self.handoff_note = QLabel("")
        self.handoff_note.setWordWrap(True)
        self.handoff_note.setStyleSheet("color: gray;")
        grid.addWidget(self.handoff_note, 4, 0, 1, 3)
        self.grid_m = QDoubleSpinBox()
        self.grid_m.setRange(0.5, 500.0)
        self.grid_m.setDecimals(1)
        self.grid_m.setSingleStep(1.0)
        self.grid_m.setToolTip("The flood grid. 10 m is the starting value. Coarser is faster; "
                               "finer than the DEM does not add terrain that was never surveyed.")
        self.simh = QDoubleSpinBox()
        self.simh.setRange(0.0, 240.0)
        self.simh.setDecimals(1)
        self.simh.setToolTip("How many hours of flood to simulate. 0 = the tool estimates it. "
                             "The starting value is 12.")
        self.simmax = QDoubleSpinBox()
        self.simmax.setRange(1.0, 336.0)
        self.simmax.setDecimals(1)
        self.simmax.setToolTip("The clock is lengthened, up to this many hours, while the flood "
                               "is still moving. The starting value is 96.")
        self.force_anuga = QCheckBox("Force ANUGA (final results from ANUGA)")
        self.force_anuga.setToolTip("Run ANUGA after LISFLOOD-FP finds the extent, and take the "
                                    "final depth, velocity and arrival from ANUGA, even if Models "
                                    "is set to LISFLOOD-FP only.")
        for sp in (self.grid_m, self.simh, self.simmax):
            sp.valueChanged.connect(self._changed)
        self.force_anuga.toggled.connect(self._changed)
        # 3.20.0: the four run settings are on the Advanced tab, not on the header; the header
        # keeps only what tells the window WHICH dam it is working on
        lay.addLayout(grid)
        lay.addWidget(self._dem_download_box())

        self.models_label = QLabel("")
        self.models_label.setWordWrap(True)
        lay.addWidget(self.models_label)

        self.tabs = QTabWidget()
        for tab, tools in pl.TABS:
            self.tabs.addTab(_scroll_tab(self._build_tab(tab, tools)), tab)
        self.tabs.addTab(_scroll_tab(self._build_advanced_tab()), ADVANCED_TAB)
        self.tabs.addTab(_scroll_tab(self._build_settings_tab()), "Settings")
        lay.addWidget(self.tabs, 1)

        # 3.20.0: why the grey buttons are grey, in words, under the tabs
        self.ready_label = QLabel("")
        self.ready_label.setWordWrap(True)
        lay.addWidget(self.ready_label)

        lay.addWidget(QLabel("Log"))
        self.log_edit = QTextEdit()
        self.log_edit.setReadOnly(True)
        self.log_edit.setMaximumHeight(110)
        try:
            self.log_edit.setMinimumHeight(LOG_MIN_H)            # 3.20.0: may shrink
        except Exception:                                        # pragma: no cover - noqa
            pass
        lay.addWidget(self.log_edit)
        row = QHBoxLayout()
        self.close_btn = QPushButton("Close")
        self.close_btn.clicked.connect(self.close)
        row.addStretch(1)
        row.addWidget(self.close_btn)
        lay.addLayout(row)
        self.setLayout(lay)


    def _dem_download_box(self):
        try:
            from nd_dam_screening.core import dem_download as dd
        except Exception:
            box = QGroupBox("Download a DEM")
            lay = QVBoxLayout()
            lay.addWidget(QLabel("ND Dam Breach Screening is not installed, so the DEM download is unavailable."))
            box.setLayout(lay)
            self.dem_box = box
            return box
        box = QGroupBox("Download a DEM (USGS 3DEP or ND DWR LiDAR)")
        grid = QGridLayout()
        self.dem_source = QComboBox()
        self.dem_source.addItems(["USGS 3DEP", "ND DWR LiDAR (lidar.dwr.nd.gov)"])
        self.dem_ftp = QLineEdit()
        self.dem_ftp.setPlaceholderText("ftp://lidarftp.swc.nd.gov/temp/<id>")
        self.dem_ascii = QCheckBox("ASCII_Grids_ft")
        self.dem_ascii.setChecked(False)
        self.dem_imagine = QCheckBox("DEM_Imagine")
        self.dem_imagine.setChecked(True)
        self.dem_scope = QComboBox()
        self.dem_scope.addItems(["Dam", "County", "State"])
        self.dem_point = QLineEdit()
        self.dem_point.setPlaceholderText("longitude,latitude")
        self.dem_point.setToolTip("3.9.0: filled from the dam's dam.json when the dam is chosen.")
        self.dem_buffer = QDoubleSpinBox()
        self.dem_buffer.setRange(1.0, pl.DEM_MAX_HALF_MI)
        # 3.9.0: half the run tool's largest domain side (it may grow to 60 mi), not 10 mi
        self.dem_buffer.setValue(pl.dem_box_half_mi(pl.settings_max_extent_mi()))
        self.dem_buffer.setSuffix(" mi")
        self.dem_buffer.setToolTip("Around the dam. The starting value is half the run tool's "
                                   "largest domain side (MAX_EXTENT_MI).")
        self.dem_county = QComboBox()
        self.dem_county.addItems(dd.county_names())
        # 0.24.0: resolution choice; output is always EPSG:5070 float32 GeoTIFF metres
        self.dem_resolution = QComboBox()
        self.dem_resolution.addItems(["10 m", "1 m"])
        self.dem_resolution.setToolTip(
            "Output: EPSG:5070, float32 GeoTIFF, metres.\n"
            "USGS 3DEP native: EPSG:5070, metres, GeoTIFF.\n"
            "ND DWR QL2 native: feet, EPSG:3721/2266, Erdas Imagine .img at 1 m — warped.")
        self.dem_out = QLineEdit()
        self.dem_out.setToolTip("Folder for the tiles and the standardised GeoTIFF.")
        browse = QPushButton("Browse...")
        browse.clicked.connect(self._browse_dem_folder)
        counties = QPushButton("Counties on the map")
        counties.clicked.connect(self._show_counties)
        go = QPushButton("Download DEM...")
        go.clicked.connect(self._open_dem_download)
        grid.addWidget(QLabel("Source"), 0, 0)
        grid.addWidget(self.dem_source, 0, 1)
        grid.addWidget(self.dem_ascii, 0, 2)
        grid.addWidget(self.dem_imagine, 0, 3)
        # 3.20.0: the ND LiDAR path row (an FTP placeholder) is only shown for Source = ND
        # LiDAR; with USGS 3DEP chosen it was an FTP address nobody should type
        self.dem_ftp_label = QLabel("ND LiDAR path")
        grid.addWidget(self.dem_ftp_label, 1, 0)
        grid.addWidget(self.dem_ftp, 1, 1, 1, 3)
        grid.addWidget(QLabel("Area"), 2, 0)
        grid.addWidget(self.dem_scope, 2, 1)
        grid.addWidget(QLabel("Resolution"), 2, 2)
        grid.addWidget(self.dem_resolution, 2, 3)
        grid.addWidget(QLabel("Dam point"), 3, 0)
        grid.addWidget(self.dem_point, 3, 1)
        grid.addWidget(self.dem_buffer, 3, 2)
        grid.addWidget(QLabel("County"), 4, 0)
        grid.addWidget(self.dem_county, 4, 1)
        grid.addWidget(counties, 4, 2, 1, 2)
        grid.addWidget(QLabel("Folder"), 5, 0)
        grid.addWidget(self.dem_out, 5, 1)
        grid.addWidget(browse, 5, 2)
        grid.addWidget(go, 5, 3)
        # item 2+7: inline format facts + time estimate label
        self.dem_info_label = QLabel("")
        self.dem_info_label.setWordWrap(True)
        self.dem_info_label.setStyleSheet("color: gray;")
        grid.addWidget(self.dem_info_label, 6, 0, 1, 4)
        self.dem_source.currentIndexChanged.connect(self._update_dem_info)
        self.dem_resolution.currentIndexChanged.connect(self._update_dem_info)
        # 3.20.0: the eight rows live in a body widget the group box can hide, so this box -
        # which matters once per dam - stops pushing the tool buttons off a laptop screen
        body = QWidget()
        body.setLayout(grid)
        outer = QVBoxLayout()
        outer.addWidget(body)
        box.setLayout(outer)
        self.dem_box, self.dem_body = box, body
        try:
            box.setCheckable(True)
            box.setChecked(True)
            box.setToolTip("A DEM is downloaded once per dam. Untick to fold this away and "
                           "give the tool buttons the room.")
            box.toggled.connect(self._dem_box_toggled)
        except Exception:                                        # noqa - an older Qt
            pass
        self._update_dem_info()
        return box

    def _dem_box_toggled(self, on):
        """The user opened or closed the DEM box: show or hide its rows, and remember that the
        choice is theirs now (the window never folds it again by itself)."""
        try:
            self._dem_box_touched = True
            self.dem_body.setVisible(bool(on))
        except Exception:                                        # noqa - never raise in a slot
            pass

    def _collapse_dem_box(self, have_dem):
        """Fold the DEM download box away once the dam has a DEM - once, and never against a
        choice the user has made."""
        box = getattr(self, "dem_box", None)
        if box is None or self._dem_box_touched or self._dem_box_collapsed_once:
            return False
        if not have_dem:
            return False
        try:
            box.setChecked(False)
            self.dem_body.setVisible(False)
        except Exception:                                        # noqa
            return False
        self._dem_box_collapsed_once = True
        self._dem_box_touched = False
        return True

    def _update_dem_info(self):
        try:
            self.dem_ftp_label.setVisible(self.dem_source.currentIndex() == 1)
            self.dem_ftp.setVisible(self.dem_source.currentIndex() == 1)
        except Exception:                                        # noqa - never raise in a slot
            pass
        try:
            src = self.dem_source.currentIndex()
            res = self.dem_resolution.currentIndex()
            if src == 0:
                txt = ("Output: EPSG:5070 float32 GeoTIFF, 10 m, metres.  ~12 s/dam." if res == 0
                       else "Output: EPSG:5070 float32 GeoTIFF, 1 m, metres.  ~90 s/dam (large file).")
            else:
                txt = ("Output: EPSG:5070 float32 GeoTIFF, 10 m, metres.  QL2 .img warped from feet.  ~30 s/dam." if res == 0
                       else "Output: EPSG:5070 float32 GeoTIFF, 1 m, metres.  QL2 .img warped from feet.  ~60 s/dam.")
            self.dem_info_label.setText(txt)
        except Exception:                                        # noqa
            pass

    def _browse_dem_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "DEM folder", self.dem_out.text())
        if folder:
            self.dem_out.setText(folder)


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

    # ------------------------------------------------------- the DEM box (3.9.0) --
    @staticmethod
    def _to_lonlat(x, y, crs):
        """x / y in `crs` to WGS 84 lon / lat with QGIS's transform."""
        from qgis.core import (QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsPointXY,
                               QgsProject)
        tf = QgsCoordinateTransform(QgsCoordinateReferenceSystem(crs),
                                    QgsCoordinateReferenceSystem("EPSG:4326"), QgsProject.instance())
        p = tf.transform(QgsPointXY(x, y))
        return p.x(), p.y()

    def _prefill_dem_box(self):
        """The dam point from dam.json and the folder <dam folder>/data, unless the user typed
        something else (what this method filled before is replaced on a dam change)."""
        if self.dem_point is None or self.dem_out is None:       # Screening is not installed
            return
        folder = self.folder()
        have = bool(folder and os.path.isdir(folder))
        point = pl.lonlat_text(pl.dam_lonlat(folder, self._to_lonlat)) if have else ""
        if self.dem_point.text().strip() in ("", self._auto_point):
            self.dem_point.setText(point)
            self._auto_point = point
        out = pl.dam_data_dir(folder) if have else ""
        if self.dem_out.text().strip() in ("", self._auto_out):
            self.dem_out.setText(out)
            self._auto_out = out

    @staticmethod
    def _gdal_translate(src, dst):
        """A VRT (or any raster) as a compressed GeoTIFF, with QGIS's GDAL."""
        from osgeo import gdal
        ds = gdal.Translate(dst, src, format="GTiff",
                            creationOptions=["COMPRESS=DEFLATE", "TILED=YES", "BIGTIFF=IF_SAFER"])
        if ds is None:
            raise RuntimeError("GDAL could not write {}".format(dst))
        ds = None

    def _dem_downloaded(self, result, out_folder, cell_m):
        """The download finished: the DEM into <dam folder>/data/dem_<cell>m.tif and dam.json
        (when it was downloaded into the dam's data folder), the map and the DEM choice."""
        folder = self.folder()
        mine = bool(folder and os.path.isdir(folder)) and os.path.normcase(
            os.path.abspath(out_folder)) == os.path.normcase(os.path.abspath(pl.dam_data_dir(folder)))
        try:
            from qgis.PyQt.QtWidgets import QApplication
            QApplication.processEvents()
        except Exception:                                        # noqa
            pass
        path, notes = pl.finish_dem_download(result, folder if mine else "", cell_m,
                                             translate=self._gdal_translate)
        for n in notes:
            self._log(n)
        if not path:
            return ""
        choice = path
        try:
            from qgis.core import QgsProject, QgsRasterLayer
            name = os.path.splitext(os.path.basename(path))[0]
            if mine:
                name = "{} {}".format(os.path.basename(os.path.normpath(folder)), name)
            layer = QgsRasterLayer(path, name)
            if layer.isValid():
                QgsProject.instance().addMapLayer(layer)
                choice = layer.id()
                self._log("DEM loaded: {}".format(path))
            else:
                self._log("!!! {} could not be read as a layer".format(path))
        except Exception as e:                                   # noqa
            self._log("!!! the DEM was not loaded: {}".format(e))
        self._pending_dem = choice
        self._fill_dem()                                         # the project's rasters + the new DEM
        self._changed()
        return path

    def _open_dem_download(self):
        import processing
        folder = self.dem_out.text().strip()
        if not folder:
            QMessageBox.warning(self, TITLE, "Choose a folder for the DEM.")
            return
        if self.dem_scope.currentIndex() == 2:
            n = QMessageBox.question(self, TITLE, "A statewide DEM is tiled and can take a long time. Continue?")
            if n != _YES:
                return
        try:
            os.makedirs(folder, exist_ok=True)                   # 3.9.0: <dam folder>/data
        except OSError:
            pass
        s = self.settings()
        resolution_idx = self.dem_resolution.currentIndex()
        result = processing.execAlgorithmDialog("nddam:downloaddem", {
            "SOURCE": self.dem_source.currentIndex(),
            "SCOPE": self.dem_scope.currentIndex(),
            "DAM_POINT": self.dem_point.text().strip(),
            "BUFFER_MI": float(self.dem_buffer.value()),
            "COUNTY": self.dem_county.currentText(),
            "RESOLUTION": resolution_idx,  # 0.24.0: 0=10m, 1=1m
            "DWR_PATH": self.dem_ftp.text().strip(),
            "ASCII_FT": self.dem_ascii.isChecked(),
            "IMAGINE": self.dem_imagine.isChecked(),
            "OUT": folder,
            # 3.9.0: the dam (ND Dam Breach Screening 0.16.0 and later use them; older ones
            # ignore them)
            "DAMS_FOLDER": s["dams_folder"] if folder == self._auto_out and self._auto_out else "",
            "DAM_ID": s["dam_id"],
        })
        # QGIS's execAlgorithmDialog returns the results of the last run (empty when cancelled)
        if isinstance(result, dict) and result.get("VRT"):
            cell_m = 10.0 if resolution_idx == 0 else 1.0
            return self._dem_downloaded(result["VRT"], folder, cell_m)
        return ""

    def _build_tab(self, tab, tools):
        w = QWidget()
        lay = QVBoxLayout()
        if tab == "Dam":
            self.status_label = QLabel("")
            self.status_label.setWordWrap(True)
            self.record_label = QLabel("")
            self.record_label.setWordWrap(True)
            self.suggest_label = QLabel("")
            self.suggest_label.setWordWrap(True)
            lay.addWidget(self.status_label)
            lay.addWidget(self.record_label)
            lay.addWidget(self.suggest_label)
            row = QHBoxLayout()
            self.suggest_btn = QPushButton("Open the suggested tool")
            self.suggest_btn.clicked.connect(lambda _c=False: self._open_suggested())
            self.folder_btn = QPushButton("Open the dam folder")
            self.folder_btn.clicked.connect(lambda _c=False: self._open_dam_folder())
            self.add_btn = QPushButton("Add the dam's results to the project")
            self.add_btn.setToolTip("The final, baseline and incremental rasters and the flood polygons of "
                                    "the dam folder, with the suite's styles (styles/*.qml), in a layer group "
                                    "named after the dam.")
            self.add_btn.clicked.connect(lambda _c=False: self._add_results())
            row.addWidget(self.suggest_btn)
            row.addWidget(self.folder_btn)
            row.addWidget(self.add_btn)
            row.addStretch(1)
            lay.addLayout(row)
        grid = QGridLayout()
        for i, (alg, label, tip) in enumerate(tools):
            btn = QPushButton(label)
            own = tip + ("" if alg == pl.WIZARD else
                         "\n(Processing: {}:{})".format(pl.PROVIDER, alg))
            btn.setToolTip(own)
            self.tool_tip[alg] = own          # 3.20.0: kept, so a reason can be added to it
            btn.clicked.connect(lambda _c=False, a=alg: self._open_tool(a))
            self.tool_button[alg] = btn
            grid.addWidget(btn, i // 2, i % 2)
        lay.addLayout(grid)
        if tab == "Results":
            self.hazard_btn = QPushButton("Open ND Dam Hazard...")
            self.hazard_btn.setToolTip("The classification authority; its 'Dam suite' group reads the "
                                       "same dam.json records.")
            self.hazard_btn.clicked.connect(lambda _c=False: self._open_other(
                "ND_Dam_Hazard_Helper", "ND Dam Hazard"))
            self.screening_btn = QPushButton("Open ND Dam Breach Screening...")
            self.screening_btn.clicked.connect(lambda _c=False: self._open_other(
                "nd_dam_screening", "ND Dam Breach Screening"))
            row = QHBoxLayout()
            row.addWidget(self.hazard_btn)
            row.addWidget(self.screening_btn)
            row.addStretch(1)
            lay.addLayout(row)
        if tab == "Checks":
            row = QHBoxLayout()
            self.paths_btn = QPushButton("Model paths (Processing options)...")
            self.paths_btn.clicked.connect(lambda _c=False: self._model_paths())
            self.help_btn = QPushButton("Help (README)")
            self.help_btn.clicked.connect(lambda _c=False: self._open_help())
            row.addWidget(self.paths_btn)
            row.addWidget(self.help_btn)
            row.addStretch(1)
            lay.addLayout(row)
        lay.addStretch(1)
        w.setLayout(lay)
        return w

    # ------------------------------------------------------- the advanced tab --
    def _build_advanced_tab(self):
        """3.20.0: the run settings that used to sit on the header above the tabs.

        They are engineering choices - the grid the flood is solved on, how long it is
        simulated, whether ANUGA gives the final results - set once for a dam and then left
        alone, and every one of them can still be changed in the tool's own dialog. Keeping
        them on the header cost four rows of a window that did not fit a laptop screen."""
        w = QWidget()
        lay = QVBoxLayout()
        head = QLabel("The values the run tools open with. The tool's own dialog can still "
                      "change every one of them before anything runs.")
        head.setWordWrap(True)
        lay.addWidget(head)
        grid = QGridLayout()
        grid.addWidget(QLabel("Grid size (m):"), 0, 0)
        grid.addWidget(self.grid_m, 0, 1)
        grid.addWidget(QLabel("Simulated time (h):"), 1, 0)
        grid.addWidget(self.simh, 1, 1)
        grid.addWidget(QLabel("Longest simulated time (h):"), 2, 0)
        grid.addWidget(self.simmax, 2, 1)
        grid.addWidget(self.force_anuga, 3, 1)
        lay.addLayout(grid)
        self.handoff_label = QLabel("")
        self.handoff_label.setWordWrap(True)
        lay.addWidget(self.handoff_label)
        row = QHBoxLayout()
        self.handoff_btn = QPushButton("Use the screening's values")
        self.handoff_btn.setToolTip("Set the grid size here to the cell size ND Dam Breach "
                                   "Screening recorded for this dam in dam.json.")
        self.handoff_btn.clicked.connect(lambda _c=False: self._apply_handoff())
        row.addWidget(self.handoff_btn)
        row.addStretch(1)
        lay.addLayout(row)
        lay.addWidget(self._life_loss_box())
        lay.addStretch(1)
        w.setLayout(lay)
        return w

    # ------------------------------------------------- loss of life (3.20.0) --
    def _life_loss_box(self):
        """Where an engineer enters and signs the fatality rate table (feature 12).

        The table is a file the plugin reads (core/fatality_rates.json), signed off exactly as
        the ACER TM 11 chart file is: this window only records the names it is given, and until
        a name is there the reports say loss of life is not estimated."""
        from .core import lifeloss as LL
        box = QGroupBox("Loss of life: fatality rate table")
        lay = QVBoxLayout()
        note = QLabel(
            "Population at risk, flood severity and warning time are always reported. Loss of "
            "life is estimated only from a rate table an engineer has entered and signed: a "
            "fatality rate is engineering judgement, not a plugin's value. Fractions of the "
            "population at risk, 0 to 1, in every cell.")
        note.setWordWrap(True)
        lay.addWidget(note)
        grid = QGridLayout()
        self.rate_edits = {}
        for c, band in enumerate(LL.LOSS.WARNING_BANDS):
            grid.addWidget(QLabel(LL.LOSS.WARNING_LABELS[band]), 0, c + 1)
        for r, sev in enumerate(LL.LOSS.SEVERITIES):
            grid.addWidget(QLabel(sev), r + 1, 0)
            for c, band in enumerate(LL.LOSS.WARNING_BANDS):
                e = QLineEdit()
                e.setPlaceholderText("0 to 1")
                self.rate_edits[(sev, band)] = e
                grid.addWidget(e, r + 1, c + 1)
        lay.addLayout(grid)
        row = QGridLayout()
        self.rate_signer = QLineEdit()
        self.rate_signer.setToolTip("The engineer who signs these rates off. One engineer is "
                                    "enough; a second checker is optional.")
        self.rate_checker = QLineEdit()
        self.rate_source = QLineEdit()
        self.rate_source.setPlaceholderText("where the rates come from (publication, table, "
                                            "date)")
        row.addWidget(QLabel("Signed off by:"), 0, 0)
        row.addWidget(self.rate_signer, 0, 1)
        row.addWidget(QLabel("Checked by:"), 1, 0)
        row.addWidget(self.rate_checker, 1, 1)
        row.addWidget(QLabel("Source:"), 2, 0)
        row.addWidget(self.rate_source, 2, 1)
        lay.addLayout(row)
        self.rate_status = QLabel("")
        self.rate_status.setWordWrap(True)
        lay.addWidget(self.rate_status)
        buttons = QHBoxLayout()
        b = QPushButton("Save and sign the rate table")
        b.clicked.connect(lambda _c=False: self._save_rates())
        buttons.addWidget(b)
        b2 = QPushButton("Open the rate table file")
        b2.clicked.connect(lambda _c=False: self._open_rate_file())
        buttons.addWidget(b2)
        buttons.addStretch(1)
        lay.addLayout(buttons)
        box.setLayout(lay)
        self._fill_rates()
        return box

    def _fill_rates(self):
        """Put the rate table file's values in the boxes and say what its status is."""
        try:
            from .core import lifeloss as LL
            table, problem = LL.load()
            if table is None:
                self.rate_status.setText(problem or "no rate table file")
                return
            rates = table.get("rates") or {}
            for (sev, band), e in (self.rate_edits or {}).items():
                v = (rates.get(sev) or {}).get(band)
                e.setText("" if v in (None, "") else "{:g}".format(float(v)))
            self.rate_signer.setText(table.get("signed_off_by") or "")
            self.rate_checker.setText(table.get("checked_by") or "")
            self.rate_source.setText(table.get("source") or "")
            usable, text = LL.status_text(table)
            self.rate_status.setText(("Loss of life IS estimated. " if usable else
                                      "Loss of life is not estimated. ") + text)
        except Exception as e:                                   # noqa - never raise in the UI
            self.rate_status.setText("the rate table could not be read ({})".format(e))

    def _save_rates(self):
        """Write the rate table file with what is in the boxes, then say what its status is."""
        try:
            from .core import lifeloss as LL
            values = {}
            for (sev, band), e in (self.rate_edits or {}).items():
                values.setdefault(sev, {})[band] = e.text().strip()
            info = LL.write_rate_file(LL.plugin_file(), values,
                                      signed_off_by=self.rate_signer.text().strip(),
                                      checked_by=self.rate_checker.text().strip(),
                                      source=self.rate_source.text().strip())
            for bad in info.get("bad") or []:
                self._log("!!! fatality rate not accepted: {}".format(bad))
            self._log("Fatality rate table written: {} ({})".format(info["path"],
                                                                     info["reason"]))
            self._fill_rates()
            return info
        except Exception as e:                                   # noqa
            self._log("!!! the rate table was not written ({})".format(e))
            QMessageBox.information(self, TITLE,
                                    "The fatality rate table was not written ({}).".format(e))
            return None

    def _open_rate_file(self):
        try:
            from .core import lifeloss as LL
            p = LL.plugin_file()
            if os.path.isfile(p):
                QDesktopServices.openUrl(QUrl.fromLocalFile(p))
        except Exception as e:                                   # noqa
            self._log("!!! the rate table file could not be opened ({})".format(e))

    # ------------------------------------------------------- the settings tab --
    def _build_settings_tab(self):
        """3.5: the suite settings file (every engineer-chosen value of the Runner's tools, shared
        with the other two plugins) as a grid - the tools open with its values."""
        from . import suite_settings as SS
        from . import suite_settings_tools as ST
        from .damsuite import settings as DS
        w = QWidget()
        lay = QVBoxLayout()
        row = QHBoxLayout()
        row.addWidget(QLabel("Settings file:"))
        self.settings_file_edit = QLineEdit()
        self.settings_file_edit.setToolTip(
            "The suite settings workbook (damsuite_settings.xlsx, or .csv) shared by the three plugins: "
            "the Runner's tools open with its values. Empty = the tools' own starting values. Edits in "
            "the grid are saved to the file at once.")
        self.settings_file_edit.editingFinished.connect(lambda: self._settings_file_edited())
        b = QPushButton("File...")
        b.clicked.connect(lambda _c=False: self._pick_settings_file())
        n = QPushButton("New...")
        n.setToolTip("Write a new settings file with the current values.")
        n.clicked.connect(lambda _c=False: self._new_settings_file())
        row.addWidget(self.settings_file_edit, 1)
        row.addWidget(b)
        row.addWidget(n)
        lay.addLayout(row)
        try:
            from . import provider as prov
            catalog = ST.full_catalog(prov.ALGORITHMS, pl.PLUGIN_KEY, pl.settings_group, pl.TAB_ORDER)
        except Exception as e:                                   # noqa
            catalog = []
            self._log("!!! settings grid: {}".format(e))
        try:
            from .damsuite import versions as vs
            versions = {pl.PLUGIN_KEY: vs.RELEASE.get(pl.PLUGIN_KEY, "")}
        except Exception:                                        # noqa
            versions = {}
        self.grid = SS.SettingsGrid(pl.PLUGIN_KEY, catalog, DS.Settings(), parent=self,
                                    on_change=lambda _s: self._autosave_settings(),
                                    on_file=self._grid_file, versions=versions, tab_order=pl.TAB_ORDER)
        lay.addWidget(self.grid, 1)
        w.setLayout(lay)
        return w

    def _settings_file_edited(self):
        self._load_settings_file(quiet=False)
        self._changed()

    def _pick_settings_file(self):
        start = self.settings_file_edit.text().strip() or self.dams_edit.text().strip() or ""
        p, _f = QFileDialog.getOpenFileName(self, "Settings file", start,
                                            "Settings workbook (*.xlsx);;Settings table (*.csv);;All files (*)")
        if p:
            self.settings_file_edit.setText(p)
            self._settings_file_edited()

    def _new_settings_file(self):
        from .damsuite import settings as DS
        dams = self.dams_edit.text().strip()
        start = os.path.join(dams, DS.DEFAULT_NAME) if dams else DS.DEFAULT_NAME
        p, _f = QFileDialog.getSaveFileName(self, "New settings file", start,
                                            "Settings workbook (*.xlsx);;Settings table (*.csv)")
        if not p:
            return ""
        if not os.path.splitext(p)[1]:
            p += ".xlsx"
        self.settings_file_edit.setText(p)
        self.grid.settings.path = p
        self.grid.save(p, name=os.path.splitext(os.path.basename(p))[0].replace("_", " "))
        DS.set_current_path(p)
        self._log("Settings file written: {}".format(p))
        self._save_state()
        return p

    def _load_settings_file(self, quiet=True):
        from .damsuite import settings as DS
        p = self.settings_file_edit.text().strip()
        DS.set_current_path(p)
        if not p:
            self.grid.settings = DS.Settings()
            self.grid.fill()
            return False
        if not os.path.isfile(p):
            if not quiet:
                QMessageBox.warning(self, TITLE, "The settings file does not exist: {}".format(p))
            self._log("!!! settings file not found: {}".format(p))
            return False
        ok = self.grid.load(p, quiet=quiet)
        if ok:
            self._log("Settings file: {} ({} value(s) differ from the starting values)".format(
                p, len(self.grid.settings.changed(self.grid.catalog))))
        return ok

    def _grid_file(self, path):
        from .damsuite import settings as DS
        if path and path != self.settings_file_edit.text().strip():
            self.settings_file_edit.setText(path)
            DS.set_current_path(path)
            self._save_state()

    def _autosave_settings(self):
        from .damsuite import settings as DS
        if self._loading:
            return
        p = self.settings_file_edit.text().strip()
        if not p:
            return
        try:
            self.grid.settings.path = p
            self.grid.settings.save(p, catalog=self.grid.catalog, versions=self.grid.versions or None)
            DS.set_current_path(p)
        except DS.SettingsError as e:
            self._log("!!! settings not saved: {}".format(e))

    # ---------------------------------------------------------------- settings --
    def settings(self):
        return {"dams_folder": self.dams_edit.text().strip(), "dam_id": self.dam_edit.text().strip(),
                "dem": self.dem_combo.currentData() or "",
                "settings_file": self.settings_file_edit.text().strip(),
                "grid_m": float(self.grid_m.value()), "simh": float(self.simh.value()),
                "simmax": float(self.simmax.value()), "force_anuga": bool(self.force_anuga.isChecked())}

    def _apply(self, s):
        # 3.9.0: no Dams folder of its own: the suite's (Suite data and servers)
        self.dams_edit.setText(s.get("dams_folder", "") or pl.suite_dams_folder())
        self.dam_edit.setText(s.get("dam_id", ""))
        self._pending_dem = s.get("dem", "") or ""
        sf = (s.get("settings_file") or "").strip()
        if not sf:
            try:
                from .damsuite import settings as DS
                sf = DS.current_path() or ""
            except Exception:                                    # noqa
                sf = ""
        self.settings_file_edit.setText(sf)
        self.grid_m.setValue(float(s.get("grid_m", pl.DEFAULT_SETTINGS["grid_m"])))
        self.simh.setValue(float(s.get("simh", pl.DEFAULT_SETTINGS["simh"])))
        self.simmax.setValue(float(s.get("simmax", pl.DEFAULT_SETTINGS["simmax"])))
        self.force_anuga.setChecked(bool(s.get("force_anuga", False)))

    def _save_state(self):
        if self._loading:
            return
        try:
            from qgis.core import QgsProject
            QgsProject.instance().writeEntry(SCOPE, ENTRY, json.dumps(self.settings()))
        except Exception:                                        # noqa
            pass

    def _load_state(self):
        s = dict(pl.DEFAULT_SETTINGS)
        try:
            from qgis.core import QgsProject
            text, ok = QgsProject.instance().readEntry(SCOPE, ENTRY, "")
            if ok and text:
                loaded = json.loads(text)
                if isinstance(loaded, dict):
                    s.update(loaded)
        except Exception:                                        # noqa
            pass
        self._apply(s)

    def _changed(self, *args):
        self._save_state()
        if not self._loading:
            pl.remember_dams_folder(self.dams_edit.text().strip())     # 3.9.0
        self.refresh()

    # --------------------------------------------------------------------- DEM --
    @staticmethod
    def raster_layers():
        """[(name, id)] of the project's raster layers."""
        rows = []
        try:
            from qgis.core import QgsProject, QgsRasterLayer
            for lyr in QgsProject.instance().mapLayers().values():
                if isinstance(lyr, QgsRasterLayer):
                    rows.append((str(lyr.name()), lyr.id()))
        except Exception:                                        # noqa
            return []
        return sorted(rows, key=lambda r: r[0].lower())

    def _fill_dem(self):
        was = self._loading
        self._loading = True
        try:
            keep = self._pending_dem or (self.dem_combo.currentData() or "")
            self.dem_combo.clear()
            self.dem_combo.addItem("(choose in the tool)", "")
            for nm, lid in self.raster_layers():
                self.dem_combo.addItem(nm, lid)
            if keep:
                i = self.dem_combo.findData(keep)
                if i < 0:
                    self.dem_combo.addItem(os.path.basename(keep) or keep, keep)
                    i = self.dem_combo.count() - 1
                self.dem_combo.setCurrentIndex(i)
            else:
                self.dem_combo.setCurrentIndex(0)
            self._pending_dem = ""
        finally:
            self._loading = was

    def _browse_dem(self):
        p, _f = QFileDialog.getOpenFileName(self, "DEM", self.dams_edit.text().strip() or "",
                                            "Raster (*.tif *.tiff *.vrt *.img);;All files (*)")
        if not p:
            return
        i = self.dem_combo.findData(p)
        if i < 0:
            self.dem_combo.addItem(os.path.basename(p), p)
            i = self.dem_combo.count() - 1
        self.dem_combo.setCurrentIndex(i)               # -> _changed

    # ------------------------------------------------------------------ browse --
    def _browse_folder(self, edit):
        d = QFileDialog.getExistingDirectory(self, "Folder", edit.text().strip() or "")
        if d:
            edit.setText(d)
            self._changed()

    def _pick_dam(self, *args):
        if self._loading:
            return
        name = self.dam_combo.currentData()
        if name:
            self.dam_edit.setText(name)
            self._changed()

    # --------------------------------------------------------------------- log --
    def _log(self, msg):
        self.log_edit.append(str(msg))

    def _log_suite_versions(self):
        """Suite problems (a too old ND Dam Hazard / Screening) are also in the message bar."""
        try:
            from .plugin import version_messages
            for level, text in version_messages():
                if level in ("error", "warning"):
                    self._log("{}: {}".format(level.upper(), text))
        except Exception:                                        # noqa
            pass

    # ----------------------------------------------------------------- refresh --
    def folder(self):
        s = self.settings()
        return pl.dam_folder(s["dams_folder"], s["dam_id"])

    def refresh(self, *args):
        """Re-read the Dams folder and the dam's folder."""
        s = self.settings()
        dams = s["dams_folder"]
        have_dams = bool(dams and os.path.isdir(dams))
        # the list of dams that have a folder
        was = self._loading
        self._loading = True
        try:
            keep = self.dam_edit.text().strip()
            self.dam_combo.clear()
            self.dam_combo.addItem("(existing dams)", "")
            for n in pl.list_dams(dams) if have_dams else []:
                self.dam_combo.addItem(n, n)
            self.dam_combo.setCurrentIndex(0)
            self.dam_edit.setText(keep)
        finally:
            self._loading = was
        folder = self.folder()
        have = bool(folder and os.path.isdir(folder))
        rows = pl.dam_status(folder) if have else []
        if have:
            self.status_label.setText("Dam folder {}:\n{}".format(folder, "\n".join(pl.status_lines(rows))))
        elif s["dam_id"] and have_dams:
            self.status_label.setText("No folder for dam {} in the Dams folder yet - the first run "
                                      "creates it.".format(s["dam_id"]))
        else:
            self.status_label.setText("Choose the Dams folder and the dam ID.")
        text, code = pl.dam_record_text(folder) if have else ("", "")
        self.record_label.setText(text)
        tool, why = pl.suggest(rows, code) if have or (s["dam_id"] and have_dams) else (None, "")
        self.suggest_label.setText(("Suggestion: " + why) if why else "")
        self._suggested = tool
        self.suggest_btn.setEnabled(bool(tool))
        self.folder_btn.setEnabled(have)
        self.add_btn.setEnabled(have and bool(pl.result_files(folder)))
        # 3.9.0: an empty DEM choice is the dam folder's DEM (never replaces a choice)
        dem, from_folder = pl.dem_for(s, folder if have else "")
        # 3.20.0: the row shows the file name, the whole path is in the tooltip - a 1,100 px
        # path on the row is what used to run over the row below it
        self.dem_note.setText("{} {}".format(pl.FROM_DAM_FOLDER, os.path.basename(dem))
                              if from_folder else "")
        self.dem_note.setToolTip(dem if from_folder else "")
        self.dem_combo.setToolTip(
            "The DEM filled into the model tools (a raster layer of the project or a file). "
            + ("Empty: the dam folder's DEM, {}.".format(dem) if from_folder else
               "Empty: the dam folder's DEM when 'Get data for this dam' has recorded one, else "
               "choose it in the tool's dialog."))
        self._prefill_dem_box()
        self._collapse_dem_box(bool(dem))                        # 3.20.0
        self._refresh_handoff(folder if have else "")            # 3.20.0
        self._refresh_ready(s, folder if have else "", dem)      # 3.20.0

    # -------------------------------------------- the screening hand-off (3.20.0) --
    def _refresh_handoff(self, folder):
        """Show what the screening ran for this dam, and offer its values.

        Reading is cheap (one dam.json and one manifest) and it never raises: a dam that was
        never screened simply shows nothing."""
        self._handoff = {}
        try:
            from .core import screenhandoff as SH
            info = SH.read(folder) if folder else {}
            self._handoff = info
            if SH.from_screening(info):
                text = "Screening: {} {}".format(SH.values_text(info), SH.source_text(info))
                missing = SH.missing_text(info)
                self.handoff_note.setText(text)
                self.handoff_label.setText(text + ("\n" + missing if missing else ""))
                self.handoff_btn.setEnabled(info.get("cell_m") is not None)
            else:
                self.handoff_note.setText("")
                self.handoff_label.setText(SH.missing_text(info) if folder else "")
                self.handoff_btn.setEnabled(False)
        except Exception as e:                                   # noqa - never raise in the UI
            self.handoff_note.setText("")
            self._log("!!! the screening hand-off could not be read: {}".format(e))

    def _apply_handoff(self):
        """Set the grid size to the cell size the screening recorded (the button on Advanced).

        Only the grid size lives in this window; the breach method and the scenario are the run
        tool's own parameters and are filled in when the tool opens (popup_logic.defaults)."""
        info = getattr(self, "_handoff", None) or {}
        cell = info.get("cell_m")
        if not cell:
            return False
        try:
            from .core import screenhandoff as SH
            self.grid_m.setValue(float(cell))
            self._log("Grid size set to {:g} m {}".format(float(cell), SH.source_text(info)))
            self._changed()
            return True
        except Exception as e:                                   # noqa
            self._log("!!! the screening's grid size was not applied: {}".format(e))
            return False

    # ---------------------------------------------- why a button is grey (3.20.0) --
    def _refresh_ready(self, s, folder, dem):
        """Enable or grey every tool button, and say why in its tooltip and the status line."""
        try:
            from .core import runblock as RB
            from .damsuite import suitecheck as SC
            try:
                items = SC.check()
            except Exception:                                    # noqa
                items = []
            h, st_acft = RB.dam_values(folder)       # one dam.json read for every button
            values = {"HEIGHT_FT": h, "STORAGE_ACFT": st_acft}
            states = {}
            for alg, btn in (self.tool_button or {}).items():
                got = RB.state(alg, folder=folder, dem=dem, dams_folder=s["dams_folder"],
                               dam_id=s["dam_id"], items=items, values=values)
                states[alg] = got
                btn.setEnabled(bool(got["ok"]))
                btn.setToolTip(RB.tooltip(alg, got["why"], self.tool_tip.get(alg, "")))
            # the status line says the run tool's reason first: that is the button people came
            # for, and "needs a DEM" is the answer to most visits
            first = next((states[a]["status"] for a in RB.RUN_TOOLS
                          if a in states and not states[a]["ok"]), "")
            if not first:
                first = next((g["status"] for g in states.values() if not g["ok"]), "")
            self.ready_label.setText(first or "Ready: every tool of this window can run for "
                                              "this dam.")
        except Exception as e:                                   # noqa - never raise in the UI
            self.ready_label.setText("")
            self._log("!!! the readiness check failed: {}".format(e))

    def _refresh_models(self):
        """Which of LISFLOOD-FP / ANUGA are found. Not part of refresh(): looking for a conda
        environment can take a second, and refresh() runs whenever the window is activated."""
        try:
            from . import wizard
            m = wizard._find_models()
            self.models_label.setText("LISFLOOD-FP: {}.  ANUGA: {}.  (Model paths: Checks tab.)".format(
                "found" if m.get("lisflood") else "NOT found",
                "found" if m.get("anuga") else "not found (only the ANUGA tier needs it)"))
        except Exception:                                        # noqa
            self.models_label.setText("")

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
    def _open_tool(self, alg):
        """Open the wizard or the Processing dialog of ndbreach:<alg>, the dam filled in."""
        if alg == pl.WIZARD:
            try:
                from . import wizard
                s = self.settings()
                # 3.9.0: the wizard starts with this window's Dams folder, dam and DEM
                wizard.run_wizard(self.iface, dams_folder=s["dams_folder"], dam_id=s["dam_id"],
                                  dem=pl.dem_for(s, self.folder())[0])
                self._log(">>> Opened the guided wizard")
                return True
            except Exception as e:                               # noqa
                self._log("!!! The wizard could not be opened: {}".format(e))
                QMessageBox.information(self, TITLE, "The wizard could not be opened here ({}).".format(e))
                return False
        s = self.settings()
        params = pl.defaults(alg, s["dams_folder"], s["dam_id"], s)
        aid = "{}:{}".format(pl.PROVIDER, alg)
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
                "Processing Toolbox: ND Dam Breach Runner > {}.".format(aid, e, pl.TOOL_LABEL.get(alg, alg)))
            return False
        self._log(">>> Opened {} ({})".format(aid, ", ".join(
            "{}={}".format(k, os.path.basename(str(v)) if os.path.isabs(str(v)) else v)
            for k, v in sorted(params.items())) or "no values filled"))
        self._save_state()
        return True

    def _open_suggested(self):
        if getattr(self, "_suggested", None):
            self._open_tool(self._suggested)

    def _add_results(self):
        """Load the dam folder's result files into the project, styled. Returns the count."""
        folder = self.folder()
        files = pl.result_files(folder) if folder else []
        if not files:
            return 0
        try:
            from qgis.core import QgsProject, QgsRasterLayer, QgsVectorLayer
        except Exception as e:                                   # noqa
            self._log("!!! layers cannot be added here: {}".format(e))
            return 0
        try:
            from . import suite_styles
        except Exception:                                        # noqa
            suite_styles = None
        project = QgsProject.instance()
        group = None
        try:
            root = project.layerTreeRoot()
            name = "Breach: {}".format(os.path.basename(os.path.normpath(folder)))
            group = root.findGroup(name) or root.addGroup(name)
        except Exception:                                        # noqa
            group = None
        added = 0
        for kind, path in files:
            try:
                title = os.path.splitext(os.path.basename(path))[0]
                layer = QgsRasterLayer(path, title) if kind == "raster" else QgsVectorLayer(path, title, "ogr")
                if not layer.isValid():
                    self._log("!!! {} could not be read as a layer".format(path))
                    continue
                if suite_styles is not None:
                    suite_styles.apply(layer, suite_styles.style_for_path(path), log=self._log)
                if group is not None:
                    project.addMapLayer(layer, False)
                    group.addLayer(layer)
                else:
                    project.addMapLayer(layer)
                added += 1
            except Exception as e:                               # noqa
                self._log("!!! {}: {}".format(path, e))
        self._log("Added {} result layer(s) of {} to the project".format(added, folder))
        return added

    def _open_dam_folder(self):
        folder = self.folder()
        if folder and os.path.isdir(folder):
            QDesktopServices.openUrl(QUrl.fromLocalFile(folder))

    def _open_other(self, folder_name, label):
        """Open a sibling plugin's window pointed at this window's dam (1.18: the window is
        started when it is installed but not loaded, and it gets the Dams folder and the dam)."""
        try:
            from . import suite_home
            plug, started = suite_home.load_plugin_object(folder_name)
            if plug is None or not hasattr(plug, "run_plugin"):
                raise KeyError("{} is not installed, or it could not be started".format(label))
            dlg = plug.run_plugin()
            if dlg is None:
                dlg = getattr(plug, "dialog", None)
            if started:
                self._log(">>> {} was installed but not loaded: it has been started".format(label))
            s = self.settings()
            key = {"ND_Dam_Hazard_Helper": "ndhazard", "nd_dam_screening": "ndscreen"}.get(folder_name, "")
            if dlg is not None and key and s["dams_folder"]:
                done = suite_home.point_window_at(dlg, key, s["dams_folder"],
                                                  {"folder": self.folder(), "dam_id": s["dam_id"]})
                if done:
                    self._log(">>> {} opened on {}".format(label, s["dam_id"] or s["dams_folder"]))
        except Exception as e:                                   # noqa
            QMessageBox.information(self, TITLE, "{} could not be opened ({}).".format(label, e))

    def _model_paths(self):
        try:
            try:
                self.iface.showOptionsDialog(self.iface.mainWindow(), currentPage="processingOptions")
            except TypeError:
                self.iface.showOptionsDialog(self.iface.mainWindow())
            self._refresh_models()
        except Exception as e:                                   # noqa
            QMessageBox.information(self, TITLE, "The options could not be opened ({}). Use Settings > "
                                    "Options > Processing > Providers > ND Dam Breach Runner.".format(e))

    def _open_help(self):
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "README.md")
        if os.path.isfile(p):
            QDesktopServices.openUrl(QUrl.fromLocalFile(p))

    def closeEvent(self, event):
        self._save_state()
        try:
            event.accept()
        except Exception:                                        # noqa
            pass
