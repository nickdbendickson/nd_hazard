"""v3.3.0: the popup window (Plugins > ND Dam Breach Runner > Open the Runner window...):
popup_logic.py (folder status, suggestion, the parameters filled into every tool), the dialog
driven through behaviour-recording Qt doubles (tests/qt_fakes), and the plugin's menu entry /
single window / unload.

Nothing here runs in a real QGIS: the widgets are test doubles, so how the window looks and lays
out has not been seen."""
import contextlib
import importlib
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import time
import types

from nd_breach_runner.tests.registry import test

HERE = os.path.dirname(os.path.abspath(__file__))
FAKES = os.path.join(HERE, "qt_fakes")


class Tmp(object):
    def __enter__(self):
        self.d = tempfile.mkdtemp(prefix="ndbr_popup_")
        return self.d

    def __exit__(self, *a):
        shutil.rmtree(self.d, ignore_errors=True)


def touch(*parts, mtime=None):
    p = os.path.join(*parts)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    open(p, "w").close()
    return p


def make_dam(dams, dam_id, **have):
    """A dam folder with the final rasters (and whatever else is asked for)."""
    from nd_breach_runner.core import workflow as wf
    f = os.path.join(dams, dam_id)
    os.makedirs(f)
    for k in have.get("rasters", ("depth", "velocity", "arrival")):
        touch(f, wf.FINAL_NAMES[k])
    return f


# ---------------------------------------------------------------------------- the logic
@test
def popup_covers_every_tool_once():
    from nd_breach_runner import algorithms as A
    from nd_breach_runner import popup_logic as pl
    names = [t[0] for t in pl.ALL_TOOLS]
    assert len(names) == len(set(names))
    # 3.9.0: and the provider's shared "Get data for this dam" (not in algorithms.ALGORITHMS)
    tools = {c().name() for c in A.ALGORITHMS} | {"getdamdata"}
    assert set(names) - {pl.WIZARD} == tools, set(names) ^ tools
    assert names[0] == pl.WIZARD and [t for t, _x in pl.TABS] == ["Dam", "Model", "Data", "Results", "Checks"]
    assert pl.TABS[0][1][1][0] == "rundam"


@test
def popup_dam_folder_and_list():
    from nd_breach_runner import popup_logic as pl
    with Tmp() as d:
        assert pl.dam_folder("", "X") == "" and pl.dam_folder(d, "  ") == ""
        assert pl.dam_folder(d, "DWR-5289") == os.path.join(d, "DWR-5289")
        assert pl.dam_folder(d, "A/B:C") == os.path.join(d, "A_B_C")          # the tools' own rule
        assert pl.list_dams(os.path.join(d, "none")) == []
        make_dam(d, "DWR-2")
        os.makedirs(os.path.join(d, "DWR-1", "_breach_model"))
        os.makedirs(os.path.join(d, "DWR-3"))
        touch(d, "DWR-3", "dam.json")
        os.makedirs(os.path.join(d, "empty"))
        touch(d, "loose.txt")
        assert pl.list_dams(d) == ["DWR-1", "DWR-2", "DWR-3"]


@test
def popup_dam_status_and_suggestion():
    from nd_breach_runner import popup_logic as pl
    from nd_breach_runner.core import workflow as wf
    with Tmp() as d:
        assert pl.dam_status(os.path.join(d, "none")) == [] and pl.dam_status("") == []
        f = os.path.join(d, "D1")
        os.makedirs(f)
        rows = pl.dam_status(f)
        st = {k: (s, det) for k, _l, s, det in rows}
        assert st["run"] == ("no", "not run yet") and st["handoff"][0] == "no"
        assert pl.suggest(rows) == ("rundam", "Run the breach model.")
        touch(f, wf.FINAL_NAMES["depth"])
        st = {k: (s, det) for k, _l, s, det in pl.dam_status(f)}
        assert st["run"] == ("part", "only depth")
        for k in ("velocity", "arrival"):
            touch(f, wf.FINAL_NAMES[k])
        touch(f, wf.BASELINE_NAMES["depth"])
        touch(f, wf.INCREMENT_NAMES["depth"])
        touch(f, "_breach_model", "run_manifest.json")
        touch(f, "Probabilistic", "Probabilistic_Summary.csv")
        touch(f, "Sensitivity", "x.csv")
        rows = pl.dam_status(f)
        st = {k: (s, det) for k, _l, s, det in rows}
        assert st["run"][0] == "yes" and st["baseline"] == ("part", "only depth")
        assert st["incremental"][0] == "yes" and st["manifest"][0] == "yes" and st["report"][0] == "no"
        assert st["ensemble"][0] == "yes" and "no per-run rasters" in st["ensemble"][1]
        assert st["sensitivity"][0] == "yes" and st["nested"][0] == "no"
        touch(f, "Probabilistic", "Runs", "index.json")
        assert {k: det for k, _l, _s, det in pl.dam_status(f)}["ensemble"] == ""
        lines = pl.status_lines(rows)
        assert lines[0].startswith("✓ Flood rasters") and any(l.startswith("◐ Baseline") for l in lines)
        # the rasters exist, nothing handed to the classification yet
        assert pl.suggest(rows) == ("classify", "Hand the results to the ND Dam Hazard classification.")
        touch(f, "_classification", "x.json")
        rows = pl.dam_status(f)
        assert pl.suggest(rows)[0] is None and "Nothing is waiting" in pl.suggest(rows)[1]
        assert pl.suggest(rows, "model")[0] == "rundam"             # dam.json says a re-run is due
        assert pl.suggest(rows, "classify")[0] == "classify"
        assert "reviewer" in pl.suggest(rows, "review")[1]


@test
def popup_dam_json_line():
    from nd_breach_runner import popup_logic as pl
    from nd_breach_runner.damsuite import dam_record as dr
    with Tmp() as d:
        assert pl.dam_record_text(os.path.join(d, "none")) == ("dam.json: no dam folder", "")
        f = os.path.join(d, "D1")
        os.makedirs(f)
        t, c = pl.dam_record_text(f)
        assert t.startswith("dam.json: none yet") and c == ""
        rec = dr.load(f, create=True)
        rec.set("dam_id", "D1", "ndbreach", "3.3.0")
        rec.add_status("screened", "ndscreen", "0.10.0", {"triage": "needs 2D"})
        rec.save()
        t, c = pl.dam_record_text(f)
        assert "screened" in t and "ndscreen" in t and c == "model", (t, c)
        with open(os.path.join(f, "dam.json"), "w") as fh:
            fh.write("{broken")
        t, c = pl.dam_record_text(f)
        assert t.startswith("dam.json: cannot be read") and c == ""


@test
def popup_parameters_filled_into_each_tool():
    from nd_breach_runner import algorithms as A
    from nd_breach_runner import popup_logic as pl
    with Tmp() as d:
        dams = os.path.join(d, "Dams")
        f = make_dam(dams, "DWR-1")
        S = {"dem": "layer_dem_1"}
        p = pl.defaults("rundam", dams, "DWR-1", S)
        assert p == {"DAMS_FOLDER": dams, "DAM_ID": "DWR-1", "DEM": "layer_dem_1",
                     "CELL": 10.0, "DURATION_HR": 0.0, "DURATION_MAX_HR": 96.0}
        assert pl.defaults("rundam", dams, "", {}) == {
            "DAMS_FOLDER": dams, "CELL": 10.0, "DURATION_HR": 0.0, "DURATION_MAX_HR": 96.0}
        assert pl.defaults("rundam", "", "", {}) == {
            "CELL": 10.0, "DURATION_HR": 0.0, "DURATION_MAX_HR": 96.0}
        b = pl.defaults("batch", dams, "DWR-1", S)
        assert b == {"DAMS_FOLDER": dams, "DEM": "layer_dem_1"}               # a batch has no one dam
        assert pl.defaults("cascade", dams, "DWR-1", S)["DAMS_FOLDER"] == dams
        for alg in ("consequences", "ndhazardclass", "benchmark", "hecrasexport", "animation"):
            assert pl.defaults(alg, dams, "DWR-1", {})["DAM_FOLDER"] == f, alg
        assert pl.defaults("animation", dams, "DWR-1", S)["DEM"] == "layer_dem_1"
        assert pl.defaults("products", dams, "DWR-1", {}) == {"DAM_FOLDER": f, "DAM_ID": "DWR-1"}
        assert pl.defaults("submittal", dams, "DWR-1", {}) == {"DAM_FOLDER": f, "DAM_ID": "DWR-1"}
        assert pl.defaults("classify", dams, "DWR-1", {}) == {"MODE": 0, "FOLDER": f, "DAM_ID": "DWR-1"}
        assert pl.defaults("verifymanifest", dams, "DWR-1", {}) == {"TARGET": f}
        assert pl.defaults("conditiondem", dams, "DWR-1", S) == {"DEM": "layer_dem_1"}
        assert pl.defaults("hecrasimport", dams, "DWR-1", {}) == {"DAM_FOLDER": f}
        # a dam with no folder yet: nothing to point a results tool at
        assert "DAM_FOLDER" not in pl.defaults("consequences", dams, "NEW", {})
        assert pl.defaults("classify", dams, "NEW", {}) == {}
        assert pl.defaults("verification", dams, "DWR-1", S) == {} == pl.defaults("check", dams, "DWR-1", S)
        assert "BENCH_FOLDER" not in pl.defaults("validationrecord", dams, "DWR-1", {})
        touch(f, "Benchmark_Summary.csv")
        assert pl.defaults("validationrecord", dams, "DWR-1", {}) == {"BENCH_FOLDER": f, "DAM_ID": "DWR-1"}
        # every name filled in is a real parameter of the tool
        algs = {c().name(): c() for c in A.ALGORITHMS}
        for a in algs.values():
            a.initAlgorithm()
        for alg in algs:
            names = set(algs[alg].params)
            for variant in ({"dem": "x"}, {}):
                for did in ("DWR-1", "NEW", ""):
                    for k in pl.defaults(alg, dams, did, variant):
                        assert k in names, "{} has no parameter {}".format(alg, k)


# ------------------------------------------------------------------------ the window
@contextlib.contextmanager
def qt_fakes():
    """The behaviour-recording Qt doubles in place of qgis.PyQt (and a recording `processing`,
    a minimal QgsProject), with the plugin's window and plugin modules imported fresh."""
    pyqt = sys.modules["qgis.PyQt"]
    core = sys.modules["qgis.core"]
    pkg = sys.modules["nd_breach_runner"]
    names = ("QtCore", "QtWidgets", "QtGui", "sip")
    saved = {n: (sys.modules.get("qgis.PyQt." + n), getattr(pyqt, n, None)) for n in names}
    mods = ("nd_breach_runner.dialog", "nd_breach_runner.plugin")
    old_mods = {m: (sys.modules.get(m), getattr(pkg, m.rsplit(".", 1)[1], None)) for m in mods}
    old_core = {n: getattr(core, n, None) for n in ("QgsProject", "QgsRasterLayer", "QgsApplication")}
    old_proc = sys.modules.get("processing")
    for n in names:
        spec = importlib.util.spec_from_file_location("qgis.PyQt." + n, os.path.join(FAKES, n + ".py"))
        m = importlib.util.module_from_spec(spec)
        sys.modules["qgis.PyQt." + n] = m
        setattr(pyqt, n, m)
        spec.loader.exec_module(m)

    class QgsRasterLayer(object):
        def __init__(self, i, n=None):
            self._i, self._n = i, n if n is not None else os.path.basename(str(i))
            self.styles = []

        def id(self):
            return self._i

        def name(self):
            return self._n

        def source(self):
            return self._i

        def isValid(self):
            return True

        def loadNamedStyle(self, path):
            self.styles.append(path)
            return ("", True)

        def triggerRepaint(self):
            pass

    class QgsVectorLayer(QgsRasterLayer):
        def __init__(self, path, name, provider="ogr"):
            QgsRasterLayer.__init__(self, path, name)

        def fields(self):
            class F(object):
                def names(self_inner):
                    return []
            return F()

    class _Group(object):
        def __init__(self, name):
            self.name, self.layers = name, []

        def addLayer(self, layer):
            self.layers.append(layer)

    class _Root(object):
        def __init__(self):
            self.groups = {}

        def findGroup(self, name):
            return self.groups.get(name)

        def addGroup(self, name):
            self.groups[name] = _Group(name)
            return self.groups[name]

    class QgsProject(object):
        _inst = None

        @classmethod
        def instance(cls):
            cls._inst = cls._inst or cls()
            return cls._inst

        def __init__(self):
            self.entries, self.layers = {}, {}
            self.root = _Root()
            self.added = []

        def layerTreeRoot(self):
            return self.root

        def addMapLayer(self, layer, legend=True):
            self.layers[layer.id()] = layer
            self.added.append((layer, legend))
            return layer

        def writeEntry(self, scope, key, value):
            self.entries[(scope, key)] = value
            return True

        def readEntry(self, scope, key, default=""):
            return (self.entries[(scope, key)], True) if (scope, key) in self.entries else (default, False)

        def mapLayers(self):
            return dict(self.layers)

    class Registry(object):
        providers = []

        def addProvider(self, p):
            self.providers.append(p)

        def removeProvider(self, p):
            self.providers.remove(p)

    class QgsApplication(object):
        reg = Registry()

        @staticmethod
        def processingRegistry():
            return QgsApplication.reg

    old_core["QgsVectorLayer"] = getattr(core, "QgsVectorLayer", None)
    core.QgsProject, core.QgsRasterLayer, core.QgsApplication = QgsProject, QgsRasterLayer, QgsApplication
    core.QgsVectorLayer = QgsVectorLayer
    proc = types.ModuleType("processing")
    proc.DIALOGS, proc.FAIL = [], []

    def exec_dialog(alg_id, params=None):
        if proc.FAIL:
            raise RuntimeError(proc.FAIL[0])
        proc.DIALOGS.append((alg_id, dict(params or {})))
    proc.execAlgorithmDialog = exec_dialog
    sys.modules["processing"] = proc
    for m in mods:
        sys.modules.pop(m, None)
    # 3.9.0: the window reads / sets the suite's Dams folder - a private store per test (the
    # NDSUITE_* variables a test sets itself still win)
    from nd_breach_runner.damsuite import suitedata as SD
    suite_tmp = tempfile.mkdtemp(prefix="ndbr_popup_suite_")
    old_suite = (SD.CONFIG, getattr(core, "QgsSettings", None))
    SD.CONFIG = os.path.join(suite_tmp, "config.json")
    if hasattr(core, "QgsSettings"):
        core.QgsSettings = type("QgsSettings", (core.QgsSettings,), {"_store": {}})
    try:
        yield types.SimpleNamespace(
            proc=proc, project=QgsProject.instance(), Raster=QgsRasterLayer, core=core,
            QtWidgets=sys.modules["qgis.PyQt.QtWidgets"], QtGui=sys.modules["qgis.PyQt.QtGui"],
            dialog=importlib.import_module("nd_breach_runner.dialog"),
            plugin=importlib.import_module("nd_breach_runner.plugin"))
    finally:
        for m in mods:
            sys.modules.pop(m, None)
            old, attr = old_mods[m]
            if old is not None:
                sys.modules[m] = old
            if attr is not None:
                setattr(pkg, m.rsplit(".", 1)[1], attr)
        for n, (mod, attr) in saved.items():
            if mod is not None:
                sys.modules["qgis.PyQt." + n] = mod
            else:
                sys.modules.pop("qgis.PyQt." + n, None)
            if attr is not None:
                setattr(pyqt, n, attr)
        for n, v in old_core.items():
            if v is not None:
                setattr(core, n, v)
            elif hasattr(core, n):
                delattr(core, n)
        if old_proc is not None:
            sys.modules["processing"] = old_proc
        else:
            sys.modules.pop("processing", None)
        SD.CONFIG = old_suite[0]
        if old_suite[1] is not None:
            core.QgsSettings = old_suite[1]
        shutil.rmtree(suite_tmp, ignore_errors=True)


@test
def popup_window_shows_the_dam_and_opens_tools():
    from nd_breach_runner import popup_logic as pl
    from nd_breach_runner.core import workflow as wf
    with Tmp() as d, qt_fakes() as q:
        dams = os.path.join(d, "Dams")
        os.makedirs(dams)
        q.project.layers["dem1"] = q.Raster("dem1", "ND LiDAR 1m")
        q.project.layers["dem2"] = q.Raster("dem2", "Another DEM")
        dlg = q.dialog.NDBreachRunnerDialog()
        # 3.20.0: the run settings moved off the header onto an Advanced tab
        assert dlg.tabs.count() == 7 and [dlg.tabs.tabText(i) for i in range(7)] == \
            [t for t, _x in pl.TABS] + [q.dialog.ADVANCED_TAB, "Settings"]
        # 3.5: the suite settings grid, one tab per popup tab, the tools' defaults from the file
        assert dlg.grid.tabs.titles == [t for t in pl.TAB_ORDER if t in dlg.grid.groups]
        assert len(dlg.grid.catalog) > 30 and all(s.plugin == "ndbreach" for s in dlg.grid.catalog)
        cell = ("ndbreach", "rundam", "CELL")
        assert cell in dlg.grid.items and dlg.grid.by_id[cell].unit == "m" and dlg.grid.value(cell) == 10.0
        crest = ("ndbreach", "rundam", "CREST_WIDTH")
        assert dlg.grid.by_id[crest].unit == "ft"
        sf = os.path.join(d, "damsuite_settings.csv")
        q.QtWidgets.QFileDialog.NEXT.append(sf)
        assert dlg._new_settings_file() == sf and os.path.isfile(sf) and dlg.settings()["settings_file"] == sf
        assert dlg.grid.set_value(cell, "5") == "" and dlg.grid.value(cell) == 5
        from nd_breach_runner.damsuite import settings as DS
        assert DS.Settings.load(sf).get("ndbreach", "rundam", "CELL") == 5 and DS.current_path() == sf
        from nd_breach_runner import provider as prov
        wrapped = [c for c in prov.ALGORITHMS if c._nd_original.__name__ == "RunDamAlgorithm"][0]()
        wrapped.initAlgorithm()
        assert wrapped.parameterDefinition("CELL").defaultValue() == 5 and dict(wrapped._nd_applied)["CELL"] == 5
        orig = wrapped._nd_original()
        orig.initAlgorithm()
        assert orig.parameterDefinition("CELL").defaultValue() == 10.0
        DS.set_current_path("", remember=True)
        dlg.settings_file_edit.setText("")
        assert set(dlg.tool_button) == {t[0] for t in pl.ALL_TOOLS}
        assert dlg.status_label.text() == "Choose the Dams folder and the dam ID."
        assert dlg.models_label.text().startswith("LISFLOOD-FP: ") and "ANUGA: " in dlg.models_label.text()
        assert not dlg.suggest_btn.isEnabled() and not dlg.folder_btn.isEnabled()
        # a tool can be opened before a dam is chosen: the dialog's own empty fields (and the
        # window's grid size and simulated times, added to the window after this test was written)
        run = {"CELL": 10.0, "DURATION_HR": 0.0, "DURATION_MAX_HR": 96.0}
        assert dlg._open_tool("rundam") is True and q.proc.DIALOGS[-1] == ("ndbreach:rundam", run)
        dlg.dams_edit.setText(dams)
        dlg._changed()
        assert "Choose the Dams folder" in dlg.status_label.text()
        dlg.dam_edit.setText("DWR-1")
        dlg._changed()
        assert "No folder for dam DWR-1 in the Dams folder yet" in dlg.status_label.text()
        assert dlg.suggest_label.text() == "Suggestion: Run the breach model." and dlg.suggest_btn.isEnabled()
        dlg._open_suggested()
        assert q.proc.DIALOGS[-1] == ("ndbreach:rundam", dict(run, DAMS_FOLDER=dams, DAM_ID="DWR-1"))
        # the DEM list holds the project's rasters; a choice reaches the tools and is saved
        cb = dlg.dem_combo
        assert [cb.itemText(i) for i in range(cb.count())] == ["(choose in the tool)", "Another DEM", "ND LiDAR 1m"]
        cb.setCurrentIndex(cb.findData("dem1"))
        assert dlg.settings()["dem"] == "dem1"
        dlg._open_tool("batch")
        assert q.proc.DIALOGS[-1] == ("ndbreach:batch", {"DAMS_FOLDER": dams, "DEM": "dem1"})
        saved = json.loads(q.project.entries[("NDBreachRunner", "state")])
        assert saved == {"dams_folder": dams, "dam_id": "DWR-1", "dem": "dem1", "settings_file": "",
                         "grid_m": 10.0, "simh": 0.0, "simmax": 96.0, "force_anuga": False}
        # the dam gets a folder: its contents are listed, the suggestion moves on
        f = make_dam(dams, "DWR-1")
        dlg.refresh()
        assert "Dam folder " + f in dlg.status_label.text() and "✓ Flood rasters" in dlg.status_label.text()
        assert dlg.record_label.text().startswith("dam.json: none yet")
        assert "Hand the results to the ND Dam Hazard classification" in dlg.suggest_label.text()
        assert dlg.folder_btn.isEnabled()
        dlg._open_suggested()
        assert q.proc.DIALOGS[-1] == ("ndbreach:classify", {"MODE": 0, "FOLDER": f, "DAM_ID": "DWR-1"})
        dlg._open_tool("consequences")
        assert q.proc.DIALOGS[-1][1] == {"DAM_FOLDER": f}
        # the list of existing dams picks a dam
        assert dlg.dam_combo.count() == 2 and dlg.dam_combo.itemText(1) == "DWR-1"
        make_dam(dams, "DWR-9")
        dlg.dam_edit.setText("")
        dlg.refresh()
        dlg.dam_combo.setCurrentIndex(dlg.dam_combo.findData("DWR-9"))
        assert dlg.dam_edit.text() == "DWR-9" and "DWR-9" in dlg.status_label.text()
        assert dlg.dam_combo.currentIndex() == 0                     # back to the prompt
        # the wizard
        calls = []
        wiz = importlib.import_module("nd_breach_runner.wizard")
        orig = wiz.run_wizard
        wiz.run_wizard = lambda iface=None, **k: calls.append((iface, k))
        try:
            # 3.9.0: the wizard starts with the window's Dams folder, dam and DEM
            assert dlg._open_tool(pl.WIZARD) is True
            assert calls == [(None, {"dams_folder": dams, "dam_id": "DWR-9", "dem": "dem1"})], calls
        finally:
            wiz.run_wizard = orig
        # Processing cannot open the dialog: logged and said, nothing raised
        q.proc.FAIL.append("no GUI")
        q.QtWidgets.EVENTS["messages"].clear()
        assert dlg._open_tool("check") is False
        assert any("could not be opened" in m for m in dlg.log_edit.lines) and q.QtWidgets.EVENTS["messages"]
        q.proc.FAIL.clear()
        # the folder and the README
        q.QtGui.OPENED_URLS.clear()
        dlg._open_dam_folder()
        dlg._open_help()
        assert q.QtGui.OPENED_URLS[0] == os.path.join(dams, "DWR-9")
        assert q.QtGui.OPENED_URLS[1].endswith("README.md")
        # a new window of the same project brings the choices back; damaged state is ignored
        dlg2 = q.dialog.NDBreachRunnerDialog()
        assert dlg2.settings()["dams_folder"] == dams and dlg2.settings()["dem"] == "dem1"
        q.project.entries[("NDBreachRunner", "state")] = "{oops"
        # 3.9.0: the first Dams folder chosen became the suite's (it had none): a window without
        # one of its own starts there
        assert pl.suite_dams_folder() == dams
        assert q.dialog.NDBreachRunnerDialog().settings() == {
            "dams_folder": dams, "dam_id": "", "dem": "", "settings_file": "",
            "grid_m": 10.0, "simh": 0.0, "simmax": 96.0, "force_anuga": False}
        # a layer that is gone from the project is kept, shown by its id
        q.project.layers.clear()
        dlg._fill_dem()
        assert dlg.settings()["dem"] == "dem1" and dlg.dem_combo.currentText() == "dem1"
        dlg.close()


@test
def popup_plugin_menu_entry_single_window_and_unload():
    with qt_fakes() as q:
        class Sig(object):
            def __init__(self): self.slots = []
            def connect(self, f): self.slots.append(f)
            def disconnect(self, f=None): self.slots.remove(f)

        class Iface(object):
            def __init__(self): self.menu, self.removed = [], []
            def mainWindow(self): return None
            def addPluginToMenu(self, name, action): self.menu.append((name, action))
            def removePluginMenu(self, name, action): self.removed.append((name, action))
            def messageBar(self): return types.SimpleNamespace(pushMessage=lambda *a, **k: None)

        iface = Iface()
        p = q.plugin.NDBreachRunnerPlugin(iface)
        p.initGui()
        first = iface.menu[0]
        assert first[0] == q.plugin.MENU and p.action is first[1]
        assert p.action.triggered._slots == [p.run_plugin]
        # 3.20.1: the tools moved into the "Breach tools" submenu: all of them still there, and the
        # menu itself is short
        tools = [a for a in p.tools_menu.items if a is not None]
        own = [a for m, a in iface.menu if m == q.plugin.MENU]
        assert len(tools) > 30 and len(own) < 10
        assert own[-3] is p.tools_menu.menuAction()
        p.action.triggered.emit()
        w = p.dialog
        assert w is not None and w.isVisible()
        p.run_plugin()
        assert p.dialog is w                                           # one window
        w.deleteLater()                                                # Qt destroyed it
        p.run_plugin()
        assert p.dialog is not w and p.dialog.isVisible()
        p.unload()
        assert p.action is None and p.dialog is None and not p.actions
        assert len(iface.removed) == len(iface.menu)
        p.unload()                                                     # safe twice
        # a window that cannot be built does not stop the plugin from loading
        q.dialog.NDBreachRunnerDialog = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
        p2 = q.plugin.NDBreachRunnerPlugin(Iface())
        p2.initGui()
        try:
            p2.run_plugin()
            raise AssertionError("expected the error to reach QGIS")
        except RuntimeError:
            pass


@test
def popup_adds_the_dams_results_styled_and_the_suite_home_opens():
    from nd_breach_runner import popup_logic as pl
    from nd_breach_runner.core import workflow as wf
    with Tmp() as d, qt_fakes() as q:
        dams = os.path.join(d, "Dams")
        f = make_dam(dams, "DWR-1")
        touch(f, wf.BASELINE_NAMES["depth"])
        touch(f, "Flood Extent.geojson")
        files = pl.result_files(f)
        assert [k for k, _p in files] == ["raster"] * 4 + ["vector"]
        assert [os.path.basename(p) for _k, p in files] == ["Depth (Max).tif", "Velocity (Max).tif",
                                                           "Arrival Time.tif", "Baseline Depth (Max).tif",
                                                           "Flood Extent.geojson"]
        assert pl.result_files(os.path.join(d, "none")) == []
        dlg = q.dialog.NDBreachRunnerDialog()
        dlg.dams_edit.setText(dams)
        dlg.dam_edit.setText("DWR-1")
        dlg._changed()
        assert dlg.add_btn.isEnabled()
        n = dlg._add_results()
        assert n == 5 and len(q.project.added) == 5 and all(not legend for _l, legend in q.project.added)
        grp = q.project.root.groups["Breach: DWR-1"]
        assert len(grp.layers) == 5
        styles = {os.path.basename(l.source()): [os.path.basename(x) for x in l.styles] for l in grp.layers}
        assert styles["Depth (Max).tif"] == ["depth_ft.qml"] and styles["Velocity (Max).tif"] == ["velocity_fps.qml"]
        assert styles["Arrival Time.tif"] == ["arrival_h.qml"] and styles["Baseline Depth (Max).tif"] == ["depth_ft.qml"]
        assert styles["Flood Extent.geojson"] == ["flood_extent.qml"]
        assert any("style depth_ft.qml applied" in m for m in dlg.log_edit.lines)
        assert "Added 5 result layer(s)" in dlg.log_edit.lines[-1]
        # the suite's styles: every QML parses, the Runner's names map
        from nd_breach_runner import suite_styles as SS
        import xml.etree.ElementTree as ET
        for qml in SS.available():
            ET.parse(os.path.join(SS.styles_folder(), qml))
        assert len(SS.available()) == 15 and SS.style_for_path("Incremental Depth (Max).tif") == "depth_ft.qml"
        # the home window from this plugin's menu
        class Iface(object):
            def __init__(self): self.menu, self.removed = [], []
            def mainWindow(self): return None
            def addPluginToMenu(self, name, action): self.menu.append((name, action))
            def removePluginMenu(self, name, action): self.removed.append((name, action))
            def messageBar(self): return types.SimpleNamespace(pushMessage=lambda *a, **k: None)
        p = q.plugin.NDBreachRunnerPlugin(Iface())
        p.initGui()
        assert p.home_action is p.iface.menu[1][1]
        h = p.run_home()
        assert h is p.home_dialog and h.isVisible() and "Opened from ND Dam Breach Runner" in h.plugins_label.text()
        h.dams_edit.setText(dams)
        h._changed()
        assert h.table.rowCount() == 1 and h.table.item(0, 0).text() == "DWR-1"
        assert h.table.item(0, 8).text().startswith("classify:")        # rasters, no record
        assert p.run_home() is h
        p.unload()
        assert p.home_dialog is None and p.home_action is None
