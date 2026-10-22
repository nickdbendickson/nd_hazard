"""Guided wizard: five pages that fill in "Run breach model for one dam" for one dam.

    Dam -> Terrain -> Scenario -> Models and cell size (live run-time estimate) -> Review

then opens the tool's own dialog (processing.execAlgorithmDialog('ndbreach:rundam', ...)) with
everything filled in, so nothing the wizard does is hidden from the user.

Wiring (plugin.py):
    from . import wizard
    ... action.triggered.connect(lambda: wizard.run_wizard(self.iface))

3.9.0: from the Runner window, run_wizard(iface, dams_folder=, dam_id=, dem=) starts with the
window's Dams folder, dam and DEM; the Dams folder is asked on page 1, so the dam's dam.json fills
the height, storage ... before page 1's check; an empty DEM is the dam folder's ("Get data for
this dam"), shown as "(from dam folder)".

Qt and QGIS are imported lazily through qgis.PyQt / qgis.core inside the functions, so this
module imports without them. Every QGIS call is wrapped: an exception becomes a message box
with the error, never a silent failure or a crashed QGIS. The decisions (what is valid, what
the parameters are, the estimate) are in wizard_logic.py, which is unit-tested; the widgets
here are thin and have only been compiled and smoke-tested with stubs, not shown on a screen.
"""
import functools
import os
import shutil
import sys
import tempfile
import time
import traceback

from . import wizard_logic as wl
from .tool_dialog import exec_tool_dialog
from .core import breach as brmod
from .core import nd_data
from .core import nd_counties as cty

TITLE = "ND Dam Breach Runner"


# ------------------------------------------------------------------------------- messages
def _message(kind, text, detail="", parent=None):
    """A message box (kind: information / warning / critical); falls back to stderr."""
    try:
        from qgis.PyQt import QtWidgets
        box = getattr(QtWidgets.QMessageBox, kind)
        box(parent, TITLE, text + ("\n\n" + detail if detail else ""))
    except Exception:
        sys.stderr.write("{}: {}\n{}\n".format(TITLE, text, detail))


def _safe(default=None, what="the wizard"):
    """Decorator: an exception in a slot or page method is shown, not raised into Qt."""
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except Exception as e:
                parent = args[0] if args and hasattr(args[0], "window") else None
                _message("critical", "Something went wrong in {}: {}".format(what, e),
                         traceback.format_exc(), parent)
                return default
        return wrapper
    return deco


def _busy(on):
    try:
        from qgis.PyQt import QtCore, QtWidgets
        if on:
            shape = getattr(QtCore.Qt, "WaitCursor", None) or QtCore.Qt.CursorShape.WaitCursor
            from qgis.PyQt import QtGui
            QtWidgets.QApplication.setOverrideCursor(QtGui.QCursor(shape))
        else:
            QtWidgets.QApplication.restoreOverrideCursor()
    except Exception:
        pass


# ------------------------------------------------------------------------------- QGIS access
def _project_layers(kind):
    """[(layer id, name)] of the project's "vector" or "raster" layers."""
    from qgis.core import QgsProject, QgsRasterLayer, QgsVectorLayer
    want = QgsVectorLayer if kind == "vector" else QgsRasterLayer
    out = [(lyr.id(), lyr.name()) for lyr in QgsProject.instance().mapLayers().values()
           if isinstance(lyr, want)]
    return sorted(out, key=lambda t: t[1].lower())


def _layer(layer_id):
    from qgis.core import QgsProject
    return QgsProject.instance().mapLayer(layer_id) if layer_id else None


# 3.18.0: the DEM download of the terrain page (the same tool the Runner window's DEM box runs)
def _to_lonlat(x, y, crs):
    """x / y in `crs` to WGS 84 lon / lat with QGIS's transform."""
    from qgis.core import (QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsPointXY,
                           QgsProject)
    tf = QgsCoordinateTransform(QgsCoordinateReferenceSystem(crs),
                                QgsCoordinateReferenceSystem("EPSG:4326"), QgsProject.instance())
    p = tf.transform(QgsPointXY(x, y))
    return p.x(), p.y()


def _algorithm_missing(alg_id):
    """True when Processing's registry says there is no such algorithm; False when it has it
    or cannot be asked (running it then says what is wrong)."""
    try:
        from qgis.core import QgsApplication
        return QgsApplication.processingRegistry().algorithmById(alg_id) is None
    except Exception:
        return False


def _gdal_translate(src, dst):
    """A VRT (or any raster) as a compressed GeoTIFF, with QGIS's GDAL (as dialog.py does)."""
    from osgeo import gdal
    ds = gdal.Translate(dst, src, format="GTiff",
                        creationOptions=["COMPRESS=DEFLATE", "TILED=YES", "BIGTIFF=IF_SAFER"])
    if ds is None:
        raise RuntimeError("GDAL could not write {}".format(dst))
    ds = None


def _process_events():
    try:
        from qgis.PyQt.QtWidgets import QApplication
        QApplication.processEvents()
    except Exception:
        pass


def _county_test(lyr, county, mapping):
    """3.13.1: f -> True when feature f of `lyr` is in `county` (its point inside the county's
    outline; a point outside every county - just over the state line - goes by the layer's
    county field). Everything passes with no county."""
    if not county:
        return lambda f: True
    from qgis.core import (QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsGeometry,
                           QgsProject)
    tr = QgsCoordinateTransform(lyr.crs(), QgsCoordinateReferenceSystem("EPSG:4326"),
                                QgsProject.instance())
    low = {f.name().lower(): f.name() for f in lyr.fields()}
    cfield = low.get("county") or low.get("countystate") or low.get("county_name")

    def test(f):
        g = f.geometry()
        if g is None or g.isEmpty():
            return False
        try:
            g = QgsGeometry(g)                   # a copy: the layer's feature is not changed
            g.transform(tr)
            p = g.centroid().asPoint()           # a point's centroid is the point
            c = cty.county_of(p.x(), p.y())
        except Exception:                                        # noqa - unreadable geometry
            c = ""
        if not c and cfield:
            c = cty.normalize(str(f[cfield] or "").split(",")[0])
        return c == county
    return test


def _feature_label(f, _id_field, mapping):
    """3.13.0: 'ID  name (height ft, storage ac-ft)' for the dam list; a DWR row with no NID ID
    is DWR-<structure_id>."""
    try:
        v = wl.dam_values_from_attrs(f, mapping)
    except Exception:                                            # noqa - a label only
        v = {}
    did = v.get("DAM_ID") or "feature {}".format(f.id())
    parts = [did]
    if v.get("DAM_NAME"):
        parts.append(v["DAM_NAME"])
    nums = []
    if v.get("HEIGHT_FT") is not None:
        nums.append("{:g} ft".format(v["HEIGHT_FT"]))
    if v.get("STORAGE_ACFT") is not None:
        nums.append("{:,.0f} ac-ft".format(v["STORAGE_ACFT"]))
    return "  ".join(parts) + (" ({})".format(", ".join(nums)) if nums else "")


def _suite_dwr_layer():
    """(layer id, note) of the suite data folder's DWR dam inventory: the project's layer of that
    file, else the file added to the project. ('', '') when there is none."""
    try:
        from .damsuite import suitedata as SD
        path = SD.dwr_layers(SD.data_folder()).get("dwr_dams") or ""
    except Exception:                                            # noqa
        return "", ""
    if not path or not os.path.isfile(path):
        return "", ""
    from qgis.core import QgsProject, QgsVectorLayer
    want = os.path.normcase(os.path.abspath(path))
    for lyr in QgsProject.instance().mapLayers().values():
        src = str(lyr.source() or "").split("|")[0]
        if isinstance(lyr, QgsVectorLayer) and src and \
                os.path.normcase(os.path.abspath(src)) == want:
            return lyr.id(), "The DWR dam inventory of the suite data folder is chosen."
    lyr = QgsVectorLayer(path, "DWR dams (suite data)", "ogr")
    if not lyr.isValid():
        return "", ""
    QgsProject.instance().addMapLayer(lyr)
    return lyr.id(), "The DWR dam inventory of the suite data folder was added to the project."


def _find_models():
    """{"lisflood": bool, "anuga": bool}: the provider settings, then auto-detection (the same
    order the tools use)."""
    exe = py = ""
    try:
        from .algorithms import SET_ANUGA, SET_LISFLOOD, _setting
        exe, py = _setting(SET_LISFLOOD), _setting(SET_ANUGA)
    except Exception:
        pass
    if not exe or not os.path.isfile(exe):
        exe = nd_data.detect_lisflood()
    if not py or not os.path.isfile(py):
        py = nd_data.detect_anuga_python()
    return {"lisflood": bool(exe and os.path.isfile(exe)),
            "anuga": bool(py and os.path.isfile(py))}


class EstimateSession(object):
    """The wizard's run-time estimate: the tool's own parameter handling
    (RunDamAlgorithm.prepare) and workflow.preview for the plan, lisflood.calibrate for this
    computer's speed, both cached in a wizard_logic.EstimateCache."""

    def __init__(self):
        self.cache = wl.EstimateCache(self._plan, self._rate)

    @staticmethod
    def _plan(params):
        from qgis.core import QgsProcessingContext, QgsProcessingFeedback, QgsProject
        from . import algorithms as algs
        from .core import workflow
        alg = algs.RunDamAlgorithm()
        alg.initAlgorithm()
        p = dict(params)
        p.setdefault("DAMS_FOLDER", tempfile.gettempdir())
        for d in alg.parameterDefinitions():
            if d.name() not in p and d.defaultValue() is not None:
                p[d.name()] = d.defaultValue()
        ctx = QgsProcessingContext()
        ctx.setProject(QgsProject.instance())
        dam, st, src, _writer, dam_xy, inflow, _folder = alg.prepare(p, ctx,
                                                                     QgsProcessingFeedback())
        plan = workflow.preview(dam, dam_xy, st, src, inflow, _folder)
        return plan, {"exe": st.lisflood_exe, "threads": st.threads}

    @staticmethod
    def _rate(context):
        from .core import lisflood
        tmp = tempfile.mkdtemp(prefix="ndbreach_wiz_")
        try:
            return lisflood.calibrate(context["exe"], tmp, context["threads"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------------------------------- the widgets
_CLASSES = {}


def _classes():
    """Build the Qt classes on first use (the base classes need qgis.PyQt)."""
    if _CLASSES:
        return _CLASSES
    from qgis.PyQt import QtWidgets as W

    def browse_file(parent, edit, caption, filt):
        start = edit.text() or os.path.expanduser("~")
        res = W.QFileDialog.getOpenFileName(parent, caption, start, filt)
        path = res[0] if isinstance(res, (tuple, list)) else res
        if path:
            edit.setText(path)

    def browse_folder(parent, edit):
        path = W.QFileDialog.getExistingDirectory(parent, "Dams folder",
                                                  edit.text() or os.path.expanduser("~"))
        if path:
            edit.setText(path)

    def path_row(page, filt=None, folder=False):
        """(container widget, line edit) with a Browse button."""
        box = W.QWidget()
        row = W.QHBoxLayout(box)
        row.setContentsMargins(0, 0, 0, 0)
        edit = W.QLineEdit()
        button = W.QPushButton("Browse...")
        if folder:
            button.clicked.connect(lambda *_: browse_folder(page, edit))
        else:
            button.clicked.connect(lambda *_: browse_file(page, edit, "Choose a file", filt))
        row.addWidget(edit)
        row.addWidget(button)
        return box, edit

    def fill_layers(combo, kind, optional, keep="", keep_label=""):
        combo.clear()
        if optional:
            combo.addItem("(none)", "")
        for lid, name in _project_layers(kind):
            combo.addItem(name, lid)
        if keep:
            i = combo.findData(keep)
            if i < 0 and os.path.exists(str(keep).split("|")[0]):     # 3.9.0: a file, not a layer
                combo.addItem(keep_label or os.path.basename(str(keep).split("|")[0]), keep)
                i = combo.count() - 1
            if i >= 0:
                combo.setCurrentIndex(i)

    def number_or_none(edit, what, problems):
        t = edit.text().strip()
        if not t:
            return None
        x = wl.clean_number(t)
        if x is None:
            problems.append("'{}' is not a number for: {}.".format(t, what))
        return x

    from .tokens import TOKENS
    from .ui_widgets import Collapsible, DamCard

    # 3.25.0, docs/gui-grammar.md: the row marks lead their row, coloured from tokens.py
    ROW_MARKS = {"ok": ("\u2713", TOKENS["success"]), "empty": ("\u25cb", TOKENS["off"]),
                 "flag": ("\u26a0", TOKENS["warn"]), "bad": ("\u2715", TOKENS["error"])}

    def mark_label():
        lab = W.QLabel("")
        lab.setMinimumWidth(16)
        return lab

    def set_mark(lab, state, tip=""):
        mark, colour = ROW_MARKS[state]
        lab.setText(mark)
        lab.setStyleSheet("color: {};".format(colour))
        lab.setToolTip(tip)

    def red_label():
        lab = W.QLabel("")
        lab.setWordWrap(True)
        lab.setStyleSheet("color: {};".format(TOKENS["error"]))
        lab.setVisible(False)
        return lab

    def muted_label():
        lab = W.QLabel("")
        lab.setWordWrap(True)
        lab.setStyleSheet("color: {};".format(TOKENS["muted"]))
        return lab

    class Page(W.QWizardPage):
        problems = None              # the page's red line (docs/gui-grammar.md: no message box)

        def answers(self):
            return self.wizard().answers

        def show_problems(self, errors):
            if self.problems is None:
                _message("warning", "Please fix:\n\n- " + "\n- ".join(errors), parent=self)
                return
            self.problems.setText("\n".join("\u2715 " + e for e in errors))
            self.problems.setVisible(bool(errors))

        def check(self, page):
            """Validate this page's answers; show the errors and return False if any."""
            wiz = self.wizard()
            kw = {"models": _find_models()} if page == "model" else {}
            errors, _warnings = wl.validate_page(page, wiz.answers, **kw)
            if errors:
                self.show_problems(errors)
                return False
            if self.problems is not None:
                self.show_problems([])
            return True

    # -- 1. the dam ---------------------------------------------------------------------
    class DamPage(Page):
        def __init__(self):
            Page.__init__(self)
            self.setTitle("The dam")
            self.setSubTitle("Pick the layer and the dam in it; the fields are matched by name "
                             "and you can correct any value. Required values first; the "
                             "others improve the estimate.")
            self.layer_crs = ""
            outer = W.QVBoxLayout(self)
            # 3.25.0: the DamCard sums the dam up (title, source, amber warnings); the page's
            # problems are a red line under it, seen without scrolling
            self.card = DamCard()
            for w in (self.card.chips_row, self.card.status_label, self.card.record_label,
                      self.card.suggest_label):
                w.setVisible(False)
            outer.addWidget(self.card)
            self.problems = red_label()
            outer.addWidget(self.problems)
            form = W.QFormLayout()
            self.layer = W.QComboBox()
            self.feature = W.QComboBox()
            # 3.13.1: list only the dams of one county
            self.county = W.QComboBox()
            self.county.addItem(cty.ALL, "")
            for name in cty.names():
                self.county.addItem(name, name)
            self.county.setToolTip("List only the dams whose point is in this county.")
            form.addRow("Dam layer", self.layer)
            form.addRow("County", self.county)
            form.addRow("Dam (feature)", self.feature)
            outer.addLayout(form)
            self.note = muted_label()
            outer.addWidget(self.note)
            # the values in two groups, Required and Improves the estimate, each row led by
            # its mark (3.25.0; before: one 'Data' table in DAM_FIELDS order)
            grid = W.QGridLayout()
            grid.addWidget(W.QLabel("<b>Layer field</b>"), 0, 2)
            grid.addWidget(W.QLabel("<b>Value</b>"), 0, 3)
            grid.setColumnStretch(2, 1)
            grid.setColumnStretch(3, 2)
            self.field_combo, self.value_edit, self.mark = {}, {}, {}
            row = 1
            for title, required in (("Required", True), ("Improves the estimate", False)):
                grid.addWidget(W.QLabel("<b>{}</b>".format(title)), row, 0, 1, 2)
                row += 1
                for key, param, label, _kind, req in wl.DAM_FIELDS:
                    if bool(req) != required:
                        continue
                    combo, edit, mark = W.QComboBox(), W.QLineEdit(), mark_label()
                    self.field_combo[key], self.value_edit[param] = combo, edit
                    self.mark[param] = mark
                    grid.addWidget(mark, row, 0)
                    grid.addWidget(W.QLabel(label), row, 1)
                    grid.addWidget(combo, row, 2)
                    grid.addWidget(edit, row, 3)
                    combo.currentIndexChanged.connect(self.values_from_feature)
                    edit.textChanged.connect(self.refresh)
                    row += 1
                if required:                     # the location is required too
                    self.point_edit = W.QLineEdit()
                    self.point_label = W.QLabel("Location (x, y)")
                    self.mark["point"] = mark_label()
                    grid.addWidget(self.mark["point"], row, 0)
                    grid.addWidget(self.point_label, row, 1)
                    grid.addWidget(self.point_edit, row, 3)
                    self.point_edit.textChanged.connect(self.refresh)
                    row += 1
            outer.addLayout(grid)
            # 3.9.0: the Dams folder on this page (was on Terrain): the dam's dam.json fills the
            # empty values before this page's check
            form2 = W.QFormLayout()
            box, self.folder = path_row(self, folder=True)
            form2.addRow("Dams folder", box)
            outer.addLayout(form2)
            self.record_note = muted_label()
            outer.addWidget(self.record_note)
            outer.addStretch(1)
            self.layer.currentIndexChanged.connect(self.layer_changed)
            self.county.currentIndexChanged.connect(self.layer_changed)
            self.feature.currentIndexChanged.connect(self.values_from_feature)
            self.folder.editingFinished.connect(self.values_from_record)
            self.value_edit["DAM_ID"].editingFinished.connect(self.values_from_record)

        @_safe(what="the dam page")
        def initializePage(self):
            a = self.answers()
            self.folder.setText(a.get("dams_folder") or os.path.join(
                os.path.expanduser("~"), "ndbreach_dams"))
            # 3.13.0: no layer chosen yet: the DWR dam inventory of the suite data folder (added
            # to the project when it is not there yet)
            self.dwr_note = ""
            keep = a.get("layer_id", "")
            if not keep:
                keep, self.dwr_note = _suite_dwr_layer()
            fill_layers(self.layer, "vector", True, keep)
            self.layer_changed()
            if self.dwr_note:
                self.note.setText(self.dwr_note + " " + self.note.text())
            # 3.9.0 / 3.18.0: the dam the Runner window gave: the layer's feature with that ID
            # when the list has it, else the record's values alone (before, the first feature of
            # the layer stayed selected, so the wizard opened on another dam than the window's)
            did = str((a.get("dam") or {}).get("DAM_ID") or "").strip()
            if did:
                wanted = {did.upper(), "DWR-" + did.upper()}
                found = -1
                for i in range(self.feature.count()):
                    head = (self.feature.itemText(i).split() or [""])[0].upper()
                    if head in wanted:
                        found = i
                        break
                if found >= 0:
                    self.feature.blockSignals(True)
                    self.feature.setCurrentIndex(found)
                    self.feature.blockSignals(False)
                    self.values_from_feature()
                else:
                    for _key, param, _label, _kind, _req in wl.DAM_FIELDS:
                        self.value_edit[param].setText("")
                    self.point_edit.setText("")
                    self.value_edit["DAM_ID"].setText(did)
            self.values_from_record()
            self.refresh()

        @_safe(what="reading the dam's dam.json")
        def values_from_record(self, *_):
            """3.9.0: EMPTY values from <Dams folder>/<Dam ID>/dam.json (typed values win)."""
            got, point, _notes = wl.dam_record_values(self.folder.text().strip(),
                                                      self.value_edit["DAM_ID"].text().strip())
            used = []
            for _key, param, label, kind, _req in wl.DAM_FIELDS:
                v = got.get(param)
                edit = self.value_edit[param]
                if v is None or edit.text().strip():
                    continue
                edit.setText(v if kind == "text" else "{:g}".format(v))
                used.append(label.split(" (")[0])
            self.record_note.setText(
                "From the dam's dam.json: {}.".format(", ".join(used)) if used else
                ("The location comes from the dam's dam.json." if point and
                 not self.point_edit.text().strip() else ""))

        def source(self):
            return _layer(self.layer.currentData() or "")

        @_safe(what="the dam layer")
        def layer_changed(self, *_):
            lyr = self.source()
            names = [f.name() for f in lyr.fields()] if lyr is not None else []
            guess = wl.field_mapping(names)
            for key, combo in self.field_combo.items():
                combo.blockSignals(True)
                combo.clear()
                combo.addItem("(none)", "")
                for n in names:
                    combo.addItem(n, n)
                i = combo.findData(guess["mapping"].get(key, ""))
                combo.setCurrentIndex(max(i, 0))
                combo.blockSignals(False)
            self.feature.blockSignals(True)
            self.feature.clear()
            county = self.county.currentData() or ""
            n_listed = 0
            if lyr is not None:
                id_field = guess["mapping"].get("F_ID")
                selected = list(lyr.selectedFeatureIds())
                keep = _county_test(lyr, county, guess["mapping"])
                for f in lyr.getFeatures():
                    if n_listed >= 5000:
                        break
                    if not keep(f):
                        continue
                    n_listed += 1
                    self.feature.addItem(_feature_label(f, id_field, guess["mapping"]), f.id())
                if selected:
                    i = self.feature.findData(selected[0])
                    if i >= 0:
                        self.feature.setCurrentIndex(i)
            self.feature.blockSignals(False)
            if lyr is None:
                self.layer_crs = self.project_crs()
                self.note.setText("No layer: type the values below. The location is in the "
                                  "project CRS ({}).".format(self.layer_crs))
            else:
                self.layer_crs = lyr.crs().authid()
                miss = guess["missing"]
                self.note.setText(
                    "Fields matched: {}.{}{}".format(
                        len(guess["mapping"]), " Not found: {}.".format(", ".join(miss))
                        if miss else "",
                        " {} dam(s) in {} County.".format(n_listed, county) if county else ""))
            self.point_label.setText("Location (x, y) in {}".format(self.layer_crs or "?"))
            self.values_from_feature()

        @staticmethod
        def project_crs():
            try:
                from qgis.core import QgsProject
                return QgsProject.instance().crs().authid()
            except Exception:
                return ""

        @_safe(what="reading the dam's attributes")
        def values_from_feature(self, *_):
            lyr = self.source()
            if lyr is None or self.feature.count() == 0:
                return
            fid = self.feature.currentData()
            feat = lyr.getFeature(fid)
            if not feat.isValid():
                return
            attrs = dict(zip([f.name() for f in lyr.fields()], feat.attributes()))
            mapping = dict((k, c.currentData()) for k, c in self.field_combo.items()
                           if c.currentData())
            values = wl.dam_values_from_attrs(attrs, mapping)
            for _key, param, _label, kind, _req in wl.DAM_FIELDS:
                v = values.get(param)
                self.value_edit[param].setText("" if v is None else
                                               (v if kind == "text" else "{:g}".format(v)))
            geom = feat.geometry()
            if geom is not None and not geom.isEmpty():
                p = geom.centroid().asPoint()
                self.point_edit.setText(wl.point_text(p.x(), p.y()).replace(",", ", "))

        def read(self):
            """(dam values, point, problems) from the boxes as they are."""
            problems, dam = [], {}
            for _key, param, label, kind, _req in wl.DAM_FIELDS:
                t = self.value_edit[param].text().strip()
                if not t:
                    continue
                if kind == "text":
                    dam[param] = t
                else:
                    x = wl.clean_number(t)
                    if x is None:
                        problems.append("'{}' is not a number for: {}.".format(t, label))
                    else:
                        dam[param] = x
            pt = wl.parse_point(self.point_edit.text())
            if self.point_edit.text().strip() and pt is None:
                problems.append("The location must be two numbers, x and y.")
            return dam, pt, problems

        @_safe(what="the dam page")
        def refresh(self, *_):
            """3.25.0: the row marks and the DamCard follow every edit."""
            dam, pt, _problems = self.read()
            for _key, param, _label, _kind, req in wl.DAM_FIELDS:
                t = self.value_edit[param].text().strip()
                if not t:
                    set_mark(self.mark[param], "bad" if req else "empty",
                             "Required" if req else "Optional: improves the estimate")
                elif param not in dam:
                    set_mark(self.mark[param], "bad", "Not a number")
                else:
                    set_mark(self.mark[param], "ok")
            if pt is not None:
                set_mark(self.mark["point"], "ok")
            else:
                set_mark(self.mark["point"], "bad", "Required: x, y of a point on the crest")
            parts = [dam.get("DAM_NAME") or dam.get("DAM_ID") or ""]
            if self.county.currentData():
                parts.append(self.county.currentData())
            if dam.get("HEIGHT_FT") is not None:
                parts.append("{:g} ft".format(dam["HEIGHT_FT"]))
            if dam.get("STORAGE_ACFT") is not None:
                parts.append("{:,.0f} ac-ft".format(dam["STORAGE_ACFT"]))
            title = " \u00b7 ".join(p for p in parts if p) if parts[0] else ""
            sub = []
            if dam.get("DAM_ID") and dam.get("DAM_NAME"):
                sub.append("ID {}".format(dam["DAM_ID"]))
            if self.layer.currentData() and self.layer.currentText():
                sub.append(self.layer.currentText())
            self.card.set_context(title, " \u00b7 ".join(sub))
            try:
                a = dict(self.answers())
                a["dam"], a["point"] = dam, ((pt[0], pt[1], self.layer_crs) if pt else None)
                _errors, warnings = wl.validate_page("dam", a)
            except Exception:                                    # noqa
                warnings = []
            self.card.set_warnings(warnings)         # amber, not red: the run can still go

        @_safe(default=False, what="the dam page")
        def validatePage(self):
            a = self.answers()
            dam, pt, problems = self.read()
            if problems:
                self.show_problems(problems)
                return False
            a["layer_id"] = self.layer.currentData() or ""
            a["feature_id"] = self.feature.currentData()
            a["mapping"] = dict((k, c.currentData()) for k, c in self.field_combo.items()
                                if c.currentData())
            a["dam"] = dam
            a["point"] = (pt[0], pt[1], self.layer_crs) if pt else None
            a["dams_folder"] = self.folder.text().strip()
            wl.apply_dam_record(a)       # v3.2.0: empty dam values from the dam's dam.json
            return self.check("dam")

    # -- 2. the terrain -----------------------------------------------------------------
    class TerrainPage(Page):
        def __init__(self):
            Page.__init__(self)
            self.setTitle("Terrain")
            self.setSubTitle("The LiDAR DEM (a GeoTIFF or a VRT mosaic loaded in QGIS), its "
                             "elevation units and the roughness.")
            outer = W.QVBoxLayout(self)
            form = W.QFormLayout()
            self.dem, self.z = W.QComboBox(), W.QComboBox()
            for zid in wl.Z_IDS:
                self.z.addItem(wl.Z_LABELS[zid], zid)
            self.nlcd, self.manning = W.QComboBox(), W.QComboBox()
            self.n = W.QDoubleSpinBox()
            self.n.setDecimals(3)
            self.n.setRange(0.005, 1.0)
            self.n.setSingleStep(0.005)
            self.n.setValue(0.06)
            self.dem_note = W.QLabel("")      # 3.9.0: "(from dam folder)"; 3.18.0: the download
            self.dem_note.setWordWrap(True)
            # 3.18.0: the Runner window's DEM download for this dam (ND Dam Breach Screening's
            # tool), 10 m unless 1 m is chosen; the file becomes the DEM above
            self.dem_res = W.QComboBox()
            for cell, label in wl.DEM_RESOLUTIONS:
                self.dem_res.addItem(label, cell)
            self.dem_res.setToolTip("10 m is the usual choice. 1 m over the whole area a run may "
                                    "need is a very large file and a long download.")
            self.dem_download = W.QPushButton("Download DEM for this dam")
            self.dem_download.setToolTip(
                "USGS 3DEP around the dam point, the same download as the Runner window's DEM "
                "box: written into the dam folder as data/dem_<cell>m.tif (EPSG:5070, metres), "
                "recorded in dam.json and chosen as the DEM. Needs ND Dam Breach Screening.")
            self.dem_download.clicked.connect(self.download_dem)
            dl = W.QWidget()
            dl_row = W.QHBoxLayout(dl)
            dl_row.setContentsMargins(0, 0, 0, 0)
            dl_row.addWidget(self.dem_download)
            dl_row.addWidget(self.dem_res)
            dl_row.addStretch(1)
            form.addRow("DEM", self.dem)
            # 3.25.0: the live coverage line: does the chosen DEM cover the dam point?
            self.coverage = W.QLabel("")
            self.coverage.setWordWrap(True)
            form.addRow("", self.coverage)
            form.addRow("", dl)
            form.addRow("", self.dem_note)
            # optional downstream bearing: shifts the downloaded box downstream instead of
            # centring it on the dam (blank = centred). You know the dam's downstream heading.
            self.dem_bearing = W.QLineEdit()
            self.dem_bearing.setPlaceholderText("deg from north; blank = centered on the dam")
            self.dem_bearing.setToolTip("Compass bearing (degrees clockwise from north) of the "
                                        "downstream direction. Blank downloads a square centred "
                                        "on the dam; a bearing shifts the same-size box so the "
                                        "dam sits near the upstream edge and the DEM reaches "
                                        "downstream.")
            form.addRow("Downstream bearing", self.dem_bearing)
            form.addRow("DEM elevation units", self.z)
            outer.addLayout(form)
            # roughness: optional, folded; the header shows the values so nothing hides
            rough = W.QWidget()
            f2 = W.QFormLayout(rough)
            f2.addRow("NLCD land cover (optional)", self.nlcd)
            f2.addRow("or a Manning's n raster (optional)", self.manning)
            f2.addRow("Manning's n where there is no raster", self.n)
            self.advanced = Collapsible("Advanced", rough)
            outer.addWidget(self.advanced)
            self.problems = red_label()
            outer.addWidget(self.problems)
            outer.addStretch(1)
            for sig in (self.nlcd.currentIndexChanged, self.manning.currentIndexChanged,
                        self.n.valueChanged):
                sig.connect(self.sync_advanced)
            self.dem.currentIndexChanged.connect(self.show_coverage)

        def sync_advanced(self, *_):
            parts = ["Manning's n {:.3f}".format(self.n.value())]
            if self.manning.currentData():
                parts.append("n raster " + self.manning.currentText())
            parts.append("NLCD " + self.nlcd.currentText() if self.nlcd.currentData()
                         else "no land cover")
            self.advanced.set_title("Advanced  \u00b7  " + " \u00b7 ".join(parts))

        def dem_layer(self):
            ref = str(self.dem.currentData() or "")
            try:
                lyr = _layer(ref)
                if lyr is None and ref and os.path.isfile(ref.split("|")[0]):
                    from qgis.core import QgsRasterLayer
                    lyr = QgsRasterLayer(ref, os.path.basename(ref.split("|")[0]))
                return lyr if lyr is not None and lyr.isValid() else None
            except Exception:                                    # noqa
                return None

        @_safe(what="the DEM coverage check")
        def show_coverage(self, *_):
            """\u2713 the DEM covers the dam point / \u2715 it does not / \u25cb nothing to check yet."""
            try:
                pt = self.answers().get("point")
            except Exception:                                    # noqa
                pt = None
            lyr = self.dem_layer()
            if lyr is None or not pt:
                state, text = "empty", ("Choose a DEM: its coverage of the dam is checked here."
                                        if lyr is None else "No dam location to check against.")
            else:
                from qgis.core import (QgsCoordinateReferenceSystem, QgsCoordinateTransform,
                                       QgsPointXY, QgsProject)
                p = QgsPointXY(pt[0], pt[1])
                src = QgsCoordinateReferenceSystem(pt[2]) if len(pt) > 2 and pt[2] else lyr.crs()
                if src.isValid() and src != lyr.crs():
                    p = QgsCoordinateTransform(src, lyr.crs(), QgsProject.instance()).transform(p)
                if lyr.extent().contains(p):
                    state, text = "ok", "The DEM covers the dam point."
                else:
                    state, text = "bad", ("The DEM does not cover the dam point: download one for "
                                          "this dam or choose another.")
            mark, colour = ROW_MARKS[state]
            self.coverage.setText("{} {}".format(mark, text))
            self.coverage.setStyleSheet("color: {};".format(colour if state != "empty"
                                                            else TOKENS["muted"]))

        @_safe(what="the terrain page")
        def initializePage(self):
            a = self.answers()
            wl.apply_dam_folder_dem(a)    # 3.9.0: an empty DEM from the dam folder's data
            fill_layers(self.dem, "raster", False, a.get("dem_id", ""), a.get("dem_name", ""))
            self.dem_note.setText("DEM from the dam folder: {}".format(a["dem_id"])
                                  if a.get("dem_from_dam_folder") else "")
            fill_layers(self.nlcd, "raster", True, a.get("nlcd_id", ""))
            fill_layers(self.manning, "raster", True, a.get("manning_id", ""))
            self.z.setCurrentIndex(max(self.z.findData(a.get("z_units", "auto")), 0))
            self.n.setValue(a.get("manning_n", 0.06))
            self.sync_advanced()
            self.show_coverage()

        @_safe(what="the DEM download")
        def download_dem(self, *_):
            """3.18.0: run the DEM download tool for the wizard's dam and take its file as the
            DEM. The tool's own dialog shows the progress (it runs the download in the
            background, as from the Runner window); this page's note says what happened."""
            a = self.answers()
            idx = max(0, self.dem_res.currentIndex())
            cell_m = wl.dem_cell_m(idx)
            params, problems = wl.dem_download_params(a, idx, to_lonlat=_to_lonlat)
            if problems:
                self.dem_note.setText("The DEM cannot be downloaded yet. " + " ".join(problems))
                return
            if _algorithm_missing(wl.DEM_DOWNLOAD_ALG):
                self.dem_note.setText(wl.DEM_NOT_INSTALLED)
                return
            bearing = self.dem_bearing.text().strip()
            if bearing:
                params["BEARING_DEG"] = bearing
            os.makedirs(params["OUT"], exist_ok=True)
            self.dem_note.setText("Downloading the {} DEM for {} into {}. The progress is in "
                                  "the tool's window; the wizard waits for it.".format(
                                      self.dem_res.currentText(), params["DAM_ID"],
                                      params["OUT"]))
            _process_events()
            import processing
            t0 = time.time()
            self.dem_download.setEnabled(False)
            try:
                result = exec_tool_dialog(processing, wl.DEM_DOWNLOAD_ALG, params, parent=self)
            finally:
                self.dem_download.setEnabled(True)
            path, notes = wl.finish_dem_download(a, result, cell_m, translate=_gdal_translate,
                                                 since=t0)
            if not path:
                self.dem_note.setText("No DEM was downloaded ({}). The DEM choice is "
                                      "unchanged.".format(" ".join(notes) or
                                                          "the tool's window was closed"))
                return
            fill_layers(self.dem, "raster", False, a.get("dem_id", ""), a.get("dem_name", ""))
            self.dem_note.setText("DEM downloaded and chosen: {}{}".format(
                path, "".join(" " + n for n in notes)))

        @_safe(default=False, what="the terrain page")
        def validatePage(self):
            a = self.answers()
            dem = self.dem.currentData() or ""
            if dem != a.get("dem_id"):
                a["dem_from_dam_folder"] = False
            a["dem_id"] = dem
            a["dem_name"] = self.dem.currentText()
            a["z_units"] = self.z.currentData() or "auto"
            a["nlcd_id"] = self.nlcd.currentData() or ""
            a["nlcd_name"] = self.nlcd.currentText()
            a["manning_id"] = self.manning.currentData() or ""
            a["manning_name"] = self.manning.currentText()
            a["manning_n"] = self.n.value()
            return self.check("terrain")

    # -- 3. the scenario ----------------------------------------------------------------
    class ScenarioPage(Page):
        def __init__(self):
            Page.__init__(self)
            self.setTitle("Scenario")
            self.setSubTitle("Sunny-day failure, or a flood (PMF) with and without the breach.")
            outer = W.QVBoxLayout(self)
            form = W.QFormLayout()
            self.method, self.scenario = W.QComboBox(), W.QComboBox()
            for m in wl.METHOD_IDS:
                self.method.addItem(brmod.METHOD_LABELS[m], m)
            for s in wl.SCENARIO_IDS:
                self.scenario.addItem(wl.SCENARIO_LABELS[s], s)
            form.addRow("Breach", self.method)
            form.addRow("Scenario", self.scenario)
            outer.addLayout(form)
            # 3.20.0: what ND Dam Breach Screening ran for this dam, and where that came from
            self.handoff = muted_label()
            outer.addWidget(self.handoff)
            self.flood = W.QGroupBox("Flood scenario inputs")
            f2 = W.QFormLayout(self.flood)
            box1, self.inflow = path_row(self, "CSV files (*.csv);;All files (*)")
            box2, self.spillway = path_row(self, "CSV files (*.csv);;All files (*)")
            self.pool, self.trigger_value = W.QLineEdit(), W.QLineEdit()
            self.trigger = W.QComboBox()
            for t in wl.TRIGGER_IDS:
                self.trigger.addItem(wl.TRIGGER_LABELS[t], t)
            f2.addRow("PMF inflow CSV (hr, cfs)", box1)
            f2.addRow("Spillway rating CSV (ft, cfs; optional)", box2)
            f2.addRow("Normal pool elevation (ft)", self.pool)
            f2.addRow("The dam fails", self.trigger)
            f2.addRow("Failure time (hr) or elevation (ft)", self.trigger_value)
            outer.addWidget(self.flood)
            self.problems = red_label()
            outer.addWidget(self.problems)
            outer.addStretch(1)
            self.scenario.currentIndexChanged.connect(self.toggle)

        @_safe(what="the scenario page")
        def toggle(self, *_):
            self.flood.setEnabled(self.scenario.currentData() == "flood")

        @_safe(what="the scenario page")
        def initializePage(self):
            a = self.answers()
            # 3.20.0: the screening's own method, scenario and cell size before the boxes are
            # filled, so the page opens on what the screen ran rather than on piping at 10 m
            wl.apply_screening_handoff(a)
            self.handoff.setText(" ".join(x for x in (a.get("handoff_note"),
                                                      a.get("handoff_missing")) if x))
            self.method.setCurrentIndex(max(self.method.findData(a.get("method")), 0))
            self.scenario.setCurrentIndex(max(self.scenario.findData(a.get("scenario")), 0))
            self.inflow.setText(a.get("inflow_csv", ""))
            self.spillway.setText(a.get("spillway_csv", ""))
            for edit, key in ((self.pool, "normal_pool_ft"), (self.trigger_value, "trigger_value")):
                v = a.get(key)
                edit.setText("" if v is None else "{:g}".format(v))
            self.trigger.setCurrentIndex(max(self.trigger.findData(a.get("trigger")), 0))
            self.toggle()

        @_safe(default=False, what="the scenario page")
        def validatePage(self):
            a, problems = self.answers(), []
            a["method"] = self.method.currentData()
            a["scenario"] = self.scenario.currentData()
            a["inflow_csv"] = self.inflow.text().strip()
            a["spillway_csv"] = self.spillway.text().strip()
            a["normal_pool_ft"] = number_or_none(self.pool, "normal pool elevation", problems)
            a["trigger"] = self.trigger.currentData()
            a["trigger_value"] = number_or_none(self.trigger_value, "failure time or elevation",
                                                problems)
            if problems:
                self.show_problems(problems)
                return False
            return self.check("scenario")

    # -- 4. models and cell size ----------------------------------------------------------
    class ModelPage(Page):
        def __init__(self):
            Page.__init__(self)
            self.setTitle("Models and cell size")
            self.setSubTitle("A finer cell is more accurate and much slower: LISFLOOD-FP's "
                             "run time grows about with the cube of 1/cell size.")
            outer = W.QVBoxLayout(self)
            form = W.QFormLayout()
            self.tier = W.QComboBox()
            for t in wl.TIER_IDS:
                self.tier.addItem(wl.TIER_LABELS[t], t)
            self.cell = W.QDoubleSpinBox()
            self.cell.setDecimals(1)
            self.cell.setRange(1.0, 200.0)
            self.cell.setSingleStep(1.0)
            self.cell.setValue(10.0)
            self.cell.setSuffix(" m")
            form.addRow("Models", self.tier)
            form.addRow("Cell size (m; 10 m = 32.8 ft)", self.cell)
            outer.addLayout(form)
            self.handoff = muted_label()                     # 3.20.0
            outer.addWidget(self.handoff)
            self.button = W.QPushButton("Estimate the run time")
            self.result = W.QLabel("")
            self.result.setWordWrap(True)
            outer.addWidget(self.button)
            outer.addWidget(self.result)
            self.problems = red_label()
            outer.addWidget(self.problems)
            outer.addStretch(1)
            self.button.clicked.connect(self.estimate)
            self.cell.valueChanged.connect(self.rescale)
            self.tier.currentIndexChanged.connect(self.rescale)

        def collect(self):
            a = self.answers()
            a["tier"] = self.tier.currentData()
            a["cell_m"] = float(self.cell.value())
            return a

        @_safe(what="the models page")
        def initializePage(self):
            a = self.answers()
            wl.apply_screening_handoff(a)                        # 3.20.0
            self.handoff.setText(" ".join(x for x in (a.get("handoff_note"),
                                                      a.get("handoff_missing")) if x))
            self.tier.setCurrentIndex(max(self.tier.findData(a.get("tier")), 0))
            self.cell.setValue(a.get("cell_m", 10.0))
            self.result.setText("Press the button to time a small test flood on this computer "
                                "and estimate this dam's run.")
            self.rescale()

        @_safe(what="the run-time estimate")
        def rescale(self, *_):
            a = self.collect()
            lines = self.wizard().session.cache.lines(a)
            if lines is not None:
                self.result.setText("\n".join(lines))

        @_safe(what="the run-time estimate")
        def estimate(self, *_):
            a = self.collect()
            wiz = self.wizard()
            self.result.setText("Setting up the dam and timing a test flood...")
            _busy(True)
            try:
                lines = wiz.session.cache.compute(a)
            except wl.EstimateError as e:
                lines = ["Could not estimate: {}".format(e)]
            finally:
                _busy(False)
            self.result.setText("\n".join(lines))

        @_safe(default=False, what="the models page")
        def validatePage(self):
            self.collect()
            return self.check("model")

    # -- 5. review ------------------------------------------------------------------------
    class ReviewPage(Page):
        def __init__(self):
            Page.__init__(self)
            self.setTitle("Review")
            self.setSubTitle("Check the values, then open the tool.")
            lay = W.QVBoxLayout(self)
            self.problems = red_label()
            lay.addWidget(self.problems)
            self.text = W.QPlainTextEdit()
            self.text.setReadOnly(True)
            lay.addWidget(self.text)

        @_safe(what="the review page")
        def initializePage(self):
            wiz = self.wizard()
            a = wiz.answers
            errors, warnings = wl.validate_all(a, models=_find_models())
            est = wiz.session.cache.lines(a)
            text = wl.review_text(a, est, warnings)
            self.show_problems(["Go Back and fix: " + e for e in errors])
            self.text.setPlainText(text)
            try:
                finish = getattr(W.QWizard, "FinishButton", None) or \
                    W.QWizard.WizardButton.FinishButton
                wiz.setButtonText(finish, "Open the tool")
            except Exception:
                pass

        @_safe(default=False, what="the review page")
        def validatePage(self):
            errors, _w = wl.validate_all(self.wizard().answers, models=_find_models())
            if errors:
                self.show_problems(errors)
                return False
            return True

    class Wizard(W.QWizard):
        def __init__(self, parent=None, answers=None):
            W.QWizard.__init__(self, parent)
            self.setWindowTitle("{} — Model one dam".format(TITLE))
            # 3.9.0: `answers` from the Runner window (wizard_logic.initial_answers)
            self.answers = answers if answers is not None else wl.default_answers()
            self.session = EstimateSession()
            for page in (DamPage(), TerrainPage(), ScenarioPage(), ModelPage(), ReviewPage()):
                self.addPage(page)
            try:
                from .ui_widgets import ellipsize
                ellipsize(self)                   # 3.23.0: one '…' on every opener button
            except Exception:                                    # noqa
                pass
            try:
                from .ui_fit import fit_window, wrap_wizard
                wrap_wizard(self)                 # 3.25.0: every page scrolls when it must
                fit_window(self, (800, 780))      # the dam page's two groups
            except Exception:                                    # noqa
                try:
                    self.resize(760, 640)
                except Exception:
                    pass

    _CLASSES.update({"Wizard": Wizard, "DamPage": DamPage, "TerrainPage": TerrainPage,
                     "ScenarioPage": ScenarioPage, "ModelPage": ModelPage,
                     "ReviewPage": ReviewPage})
    return _CLASSES


# ------------------------------------------------------------------------------- entry point
def open_tool(answers):
    """Open 'Run breach model for one dam' pre-filled from the wizard's answers."""
    import processing
    params = wl.build_rundam_params(answers)
    return processing.execAlgorithmDialog(wl.ALGORITHM_ID, params)


def run_wizard(iface=None, dams_folder="", dam_id="", dem=""):
    """Show the wizard; on Finish open the tool's dialog. Returns the parameters given to the
    tool, or None when the wizard was cancelled or could not run. 3.9.0: `dams_folder`, `dam_id`
    and `dem` (a layer id or a file) are where it starts (the Runner window gives them)."""
    try:
        parent = iface.mainWindow() if iface is not None else None
        if dams_folder or dam_id or dem:
            wiz = _classes()["Wizard"](parent, wl.initial_answers(dams_folder, dam_id, dem))
        else:
            wiz = _classes()["Wizard"](parent)
        run = getattr(wiz, "exec_", None) or getattr(wiz, "exec")
        if not run():
            return None
        params = wl.build_rundam_params(wiz.answers)
    except Exception as e:
        _message("critical", "The wizard could not start: {}".format(e), traceback.format_exc())
        return None
    try:
        open_tool(wiz.answers)
    except Exception as e:
        _message("critical", "The tool's dialog could not be opened: {}\n\nThe values were:\n{}"
                 .format(e, "\n".join("{} = {}".format(k, v) for k, v in sorted(params.items()))),
                 traceback.format_exc())
    return params
