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
import traceback

from . import wizard_logic as wl
from .core import breach as brmod
from .core import nd_data

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

    class Page(W.QWizardPage):
        def answers(self):
            return self.wizard().answers

        def show_problems(self, errors):
            _message("warning", "Please fix:\n\n- " + "\n- ".join(errors), parent=self)

        def check(self, page):
            """Validate this page's answers; show the errors and return False if any."""
            wiz = self.wizard()
            kw = {"models": _find_models()} if page == "model" else {}
            errors, _warnings = wl.validate_page(page, wiz.answers, **kw)
            if errors:
                self.show_problems(errors)
                return False
            return True

    # -- 1. the dam ---------------------------------------------------------------------
    class DamPage(Page):
        def __init__(self):
            Page.__init__(self)
            self.setTitle("The dam")
            self.setSubTitle("Pick the layer and the dam in it; the fields are matched by name "
                             "and you can correct any value.")
            self.layer_crs = ""
            outer = W.QVBoxLayout(self)
            form = W.QFormLayout()
            self.layer = W.QComboBox()
            self.feature = W.QComboBox()
            form.addRow("Dam layer", self.layer)
            form.addRow("Dam (feature)", self.feature)
            outer.addLayout(form)
            self.note = W.QLabel("")
            self.note.setWordWrap(True)
            outer.addWidget(self.note)
            grid = W.QGridLayout()
            grid.addWidget(W.QLabel("<b>Data</b>"), 0, 0)
            grid.addWidget(W.QLabel("<b>Layer field</b>"), 0, 1)
            grid.addWidget(W.QLabel("<b>Value</b>"), 0, 2)
            self.field_combo, self.value_edit = {}, {}
            for i, (key, param, label, _kind, _req) in enumerate(wl.DAM_FIELDS):
                combo, edit = W.QComboBox(), W.QLineEdit()
                self.field_combo[key], self.value_edit[param] = combo, edit
                grid.addWidget(W.QLabel(label), i + 1, 0)
                grid.addWidget(combo, i + 1, 1)
                grid.addWidget(edit, i + 1, 2)
                combo.currentIndexChanged.connect(self.values_from_feature)
            n = len(wl.DAM_FIELDS) + 1
            self.point_edit = W.QLineEdit()
            self.point_label = W.QLabel("Location (x, y)")
            grid.addWidget(self.point_label, n, 0)
            grid.addWidget(self.point_edit, n, 2)
            outer.addLayout(grid)
            # 3.9.0: the Dams folder on this page (was on Terrain): the dam's dam.json fills the
            # empty values before this page's check
            form2 = W.QFormLayout()
            box, self.folder = path_row(self, folder=True)
            form2.addRow("Dams folder", box)
            outer.addLayout(form2)
            self.record_note = W.QLabel("")
            self.record_note.setWordWrap(True)
            outer.addWidget(self.record_note)
            self.layer.currentIndexChanged.connect(self.layer_changed)
            self.feature.currentIndexChanged.connect(self.values_from_feature)
            self.folder.editingFinished.connect(self.values_from_record)
            self.value_edit["DAM_ID"].editingFinished.connect(self.values_from_record)

        @_safe(what="the dam page")
        def initializePage(self):
            a = self.answers()
            self.folder.setText(a.get("dams_folder") or os.path.join(
                os.path.expanduser("~"), "ndbreach_dams"))
            fill_layers(self.layer, "vector", True, a.get("layer_id", ""))
            self.layer_changed()
            # 3.9.0: the dam the Runner window gave, when no layer feature gave one
            did = str((a.get("dam") or {}).get("DAM_ID") or "")
            if did and not self.value_edit["DAM_ID"].text().strip():
                self.value_edit["DAM_ID"].setText(did)
            self.values_from_record()

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
            if lyr is not None:
                id_field = guess["mapping"].get("F_ID")
                selected = list(lyr.selectedFeatureIds())
                for k, f in enumerate(lyr.getFeatures()):
                    if k >= 5000:
                        break
                    label = str(f[id_field]) if id_field else "feature {}".format(f.id())
                    self.feature.addItem(label, f.id())
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
                    "Fields matched: {}.{}".format(
                        len(guess["mapping"]), " Not found: {}.".format(", ".join(miss))
                        if miss else ""))
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

        @_safe(default=False, what="the dam page")
        def validatePage(self):
            a, problems = self.answers(), []
            dam = {}
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
            form = W.QFormLayout(self)
            self.dem, self.z = W.QComboBox(), W.QComboBox()
            for zid in wl.Z_IDS:
                self.z.addItem(wl.Z_LABELS[zid], zid)
            self.nlcd, self.manning = W.QComboBox(), W.QComboBox()
            self.n = W.QDoubleSpinBox()
            self.n.setDecimals(3)
            self.n.setRange(0.005, 1.0)
            self.n.setSingleStep(0.005)
            self.n.setValue(0.06)
            self.dem_note = W.QLabel("")      # 3.9.0: "(from dam folder)"
            self.dem_note.setWordWrap(True)
            form.addRow("DEM", self.dem)
            form.addRow("", self.dem_note)
            form.addRow("DEM elevation units", self.z)
            form.addRow("NLCD land cover (optional)", self.nlcd)
            form.addRow("or a Manning's n raster (optional)", self.manning)
            form.addRow("Manning's n where there is no raster", self.n)

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
            self.scenario.currentIndexChanged.connect(self.toggle)

        @_safe(what="the scenario page")
        def toggle(self, *_):
            self.flood.setEnabled(self.scenario.currentData() == "flood")

        @_safe(what="the scenario page")
        def initializePage(self):
            a = self.answers()
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
            self.button = W.QPushButton("Estimate the run time")
            self.result = W.QLabel("")
            self.result.setWordWrap(True)
            outer.addWidget(self.button)
            outer.addWidget(self.result)
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
            if errors:
                text = "PROBLEMS TO FIX (go Back):\n  - " + "\n  - ".join(errors) + "\n\n" + text
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
            self.setWindowTitle("{}: model one dam".format(TITLE))
            # 3.9.0: `answers` from the Runner window (wizard_logic.initial_answers)
            self.answers = answers if answers is not None else wl.default_answers()
            self.session = EstimateSession()
            for page in (DamPage(), TerrainPage(), ScenarioPage(), ModelPage(), ReviewPage()):
                self.addPage(page)
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
