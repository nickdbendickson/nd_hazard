"""Tests of 3.9.0: the tools, the window and the wizard fill their EMPTY inputs from the dam
folder (dam.json's data section, written by "Get data for this dam" / the window's DEM download),
the Get data tool in the provider, the DEM download box, the suite's Dams folder, styled tool
results, the ANUGA Python file picker and the SVG converter check. Simulated QGIS
(tests/fake_qgis.py) and the Qt doubles of test_popup; no GDAL, no network, no model run."""
import importlib
import json
import os
import shutil
import sys
import tempfile
import types

from nd_breach_runner.tests.registry import test


def _fake():
    from nd_breach_runner.tests import fake_qgis
    if "qgis.core" not in sys.modules or not hasattr(sys.modules["qgis.core"], "QgsProcessingAlgorithm"):
        fake_qgis.install()
    return fake_qgis


def _dam(dams, did, data=None, fields=None, folder_name=None):
    """A dam folder with a dam.json; `data` {role: relative path} files are created."""
    folder = os.path.join(dams, folder_name or did)
    os.makedirs(os.path.join(folder, "data"), exist_ok=True)
    rec = {"format": "nd_dam_suite/dam_record", "format_version": 1,
           "fields": dict(fields or {}, dam_id=did), "sources": {}, "status": [], "conflicts": []}
    if data:
        rec["data"] = {}
        for role, rel in data.items():
            rec["data"][role] = {"path": rel, "source": "test", "when": "2026-10-05T00:00:00"}
            p = os.path.join(folder, rel.split("|")[0])
            if not os.path.exists(p):
                open(p, "w").close()
    with open(os.path.join(folder, "dam.json"), "w") as f:
        json.dump(rec, f)
    return folder


ALL_DATA = {"dem": "data/dem.vrt", "structures": "data/suite_package.gpkg|layername=structures",
            "structure_points": "data/suite_package.gpkg|layername=structure_points",
            "roads": "data/suite_package.gpkg|layername=roads",
            "rail": "data/suite_package.gpkg|layername=railroads", "nlcd": "data/nlcd.tif",
            "waterbodies": "data/waterbodies.gpkg", "lifelines": "data/lifelines.gpkg",
            "nid_dams": "data/nid_dams.gpkg", "bridges": "data/bridges.gpkg"}


def _provider():
    _fake()
    import nd_breach_runner.algorithms as A
    import nd_breach_runner.provider as P
    return A, P


def _wrapped(P, name):
    return [c for c in P.ALGORITHMS if c().name() == name][0]


def _own(alg):
    """The tool's own class that runs it (under the settings / dam-folder wrappers)."""
    return [k for k in type(alg).__mro__ if "processAlgorithm" in k.__dict__ and
            "_nd_data_aware" not in k.__dict__][0]


class _Record(object):
    """Replaces a tool's own processAlgorithm (the model does not run): records the parameters
    the dam-folder wrapper hands on."""

    def __init__(self, cls):
        self.cls, self.seen = cls, []

    def __enter__(self):
        self.old = self.cls.__dict__["processAlgorithm"]
        seen = self.seen
        self.cls.processAlgorithm = lambda alg, parameters, context, feedback: \
            seen.append(dict(parameters)) or {}
        return self

    def __exit__(self, *a):
        self.cls.processAlgorithm = self.old


# --------------------------------------------------------------------------- the provider
@test
def v390_run_tools_fill_empty_inputs_from_the_dam_folder():
    A, P = _provider()
    fq = _fake()
    tmp = tempfile.mkdtemp(prefix="ndbr390_")
    try:
        f = _dam(tmp, "ND/7", ALL_DATA, {"normal_pool_ft": 1650.5}, folder_name="ND_7")
        for name, cls in (("rundam", A.RunDamAlgorithm), ("sensitivity", A.SensitivityAlgorithm),
                          ("estimate", A.EstimateAlgorithm)):
            w = _wrapped(P, name)
            assert w._nd_original.__name__ == cls.__name__ and w._nd_data_aware
            alg = w()
            alg.initAlgorithm()
            assert getattr(alg.parameterDefinition("DEM"), "optional", False), name
            with _Record(_own(alg)) as r:
                fb = fq.QgsProcessingFeedback()
                # the Dam ID 'ND/7' has the folder ND_7, as the tool names it
                alg.processAlgorithm({"DAMS_FOLDER": tmp, "DAM_ID": "ND/7", "DEM": None}, None, fb)
                got = r.seen[-1]
                assert got["DEM"] == os.path.join(f, "data", "dem.vrt"), got
                assert got["RECEPTOR_POINTS"].endswith("layername=structures")
                assert got["RECEPTOR_ROADS"].endswith("layername=roads")
                assert got["RECEPTOR_RAIL"].endswith("layername=railroads")
                assert got["NLCD_RASTER"].endswith("nlcd.tif") and "PONDS" not in got
                assert got["NORMAL_POOL_FT"] == 1650.5
                # culvert / bridge LINES are not the suite's bridge points
                assert "STRUCTURES" not in got
                assert any("DEM from the dam folder" in n for n in fb.info), fb.info
                # what the user gives is never replaced
                alg.processAlgorithm({"DAMS_FOLDER": tmp, "DAM_ID": "ND/7", "DEM": "/my/dem.tif",
                                      "NORMAL_POOL_FT": 1600.0, "RECEPTOR_ROADS": "/my/roads.shp"},
                                     None, fb)
                got = r.seen[-1]
                assert got["DEM"] == "/my/dem.tif" and got["NORMAL_POOL_FT"] == 1600.0
                assert got["RECEPTOR_ROADS"] == "/my/roads.shp"
        # no DEM anywhere: a clear stop, before the model
        _dam(tmp, "EMPTY")
        alg = _wrapped(P, "rundam")()
        alg.initAlgorithm()
        try:
            alg.processAlgorithm({"DAMS_FOLDER": tmp, "DAM_ID": "EMPTY"}, None,
                                 fq.QgsProcessingFeedback())
        except fq.QgsProcessingException as e:
            assert "DEM needed" in str(e) and "Get data for this dam" in str(e), e
        else:
            raise AssertionError("no error without a DEM")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@test
def v390_results_tools_fill_their_layers_and_the_catalog_is_unchanged():
    A, P = _provider()
    fq = _fake()
    from nd_breach_runner import suite_settings_tools as ST
    from nd_breach_runner import popup_logic as pl
    tmp = tempfile.mkdtemp(prefix="ndbr390_")
    try:
        f = _dam(tmp, "D1", ALL_DATA)
        w = _wrapped(P, "consequences")
        alg = w()
        alg.initAlgorithm()
        assert getattr(alg.parameterDefinition("STRUCTURES"), "optional", False)
        with _Record(_own(alg)) as r:
            alg.processAlgorithm({"DAM_FOLDER": f}, None, fq.QgsProcessingFeedback())
            got = r.seen[-1]
            # life safety reads NSI-style points (occupants, values)
            assert got["STRUCTURES"].endswith("layername=structure_points"), got
            assert got["ROADS"].endswith("layername=roads") and got["RAIL"].endswith("layername=railroads")
        w = _wrapped(P, "ndhazardclass")
        assert w._nd_data_mapping == {"STRUCTURES": "structures", "ROADS": "roads", "RAIL": "rail",
                                      "LIFELINES": "lifelines", "DOWNSTREAM_DAMS": "nid_dams"}
        # tools without one dam folder are not wrapped
        for name in ("batch", "cascade", "animation", "conditiondem", "fillgaps", "nlcdmanning"):
            assert not getattr(_wrapped(P, name), "_nd_data_aware", False), name
        # the settings grid: the same catalog from the wrapped tools as from the originals
        cat = ST.full_catalog(P.ALGORITHMS, pl.PLUGIN_KEY, pl.settings_group, pl.TAB_ORDER)
        raw = ST.full_catalog(A.ALGORITHMS, pl.PLUGIN_KEY, pl.settings_group, pl.TAB_ORDER)
        assert [s.id for s in cat] == [s.id for s in raw] and len(cat) > 30
        assert [ST.original(c).__name__ for c in P.ALGORITHMS] == [c.__name__ for c in A.ALGORITHMS]
        assert not any(s.tool == "getdamdata" for s in cat)
        # the Get data tool: registered next to them, in the tools group
        g = P.GET_DATA()
        g.initAlgorithm()
        assert g.name() == "getdamdata" and g.groupId() == "tools"
        assert {"DAMS_FOLDER", "DAM_ID", "DAM_FOLDER", "DATA_FOLDER"} <= set(g.params)
        prov = P.Provider()
        added = []
        prov.addAlgorithm = lambda a: added.append(a.name())
        prov.loadAlgorithms()
        assert len(added) == len(P.ALGORITHMS) + 1 and added.count("getdamdata") == 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@test
def v390_a_local_datum_normal_pool_from_dam_json_is_refused():
    A, P = _provider()
    from nd_breach_runner.core import breach as brmod
    from nd_breach_runner.damsuite import dam_record as DR
    tmp = tempfile.mkdtemp(prefix="ndbr390_")
    try:
        rec = DR.DamRecord(tmp)
        rec.set("normal_pool_ft", 100.0, "test", "1")
        rec.set("top_of_dam_datum", "local", "test", "1")
        alg = A.RunDamAlgorithm()
        alg.initAlgorithm()
        alg._dam_rec = rec
        inflow = os.path.join(tmp, "in.csv")
        open(inflow, "w").write("0,0\n1,100\n")
        try:
            alg.flood_inputs({"INFLOW_CSV": inflow, "NORMAL_POOL_FT": 100.0}, None, None)
        except brmod.BreachInputError as e:
            assert "datum is 'local'" in str(e), e
        else:
            raise AssertionError("a local-datum pool was used as an elevation")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@test
def v390_tool_results_are_styled_and_anuga_python_is_a_file_picker():
    A, P = _provider()
    fq = _fake()
    from nd_breach_runner import suite_styles
    tmp = tempfile.mkdtemp(prefix="ndbr390_")
    seen = []
    old = suite_styles.attach
    try:
        suite_styles.attach = lambda context, dest, qml=None, path_hint=None: seen.append(
            (dest, suite_styles.style_for_path(path_hint))) or True
        p = os.path.join(tmp, "Depth (Max).tif")
        open(p, "w").close()
        ctx = fq.QgsProcessingContext()
        A.RunDamAlgorithm().load(ctx, p, "D1 Depth (Max)")
        A.RunDamAlgorithm().load(ctx, os.path.join(tmp, "missing.tif"), "x")
        assert ctx.loaded == [p] and seen == [(p, "depth_ft.qml")], seen
    finally:
        suite_styles.attach = old
        shutil.rmtree(tmp, ignore_errors=True)
    for cls in (A.RunDamAlgorithm, A.CheckAlgorithm, A.BatchAlgorithm):
        alg = cls()
        alg.initAlgorithm()
        assert type(alg.parameterDefinition("ANUGA_PYTHON")).__name__ == "QgsProcessingParameterFile", cls
    assert not os.path.exists(os.path.join(os.path.dirname(A.__file__), "algorithms.py.orig"))


# ------------------------------------------------------------------------------ the window
@test
def v390_popup_fills_an_empty_dem_from_the_dam_folder():
    from nd_breach_runner import popup_logic as pl
    tmp = tempfile.mkdtemp(prefix="ndbr390_")
    try:
        f = _dam(tmp, "D1", {"dem": "data/dem_10m.tif"})
        dem = os.path.join(f, "data", "dem_10m.tif")
        assert pl.dam_data_dem(f) == dem and pl.dem_for({}, f) == (dem, True)
        assert pl.dem_for({"dem": "layer1"}, f) == ("layer1", False)        # a choice wins
        assert pl.defaults("rundam", tmp, "D1", {})["DEM"] == dem
        assert pl.defaults("rundam", tmp, "D1", {"dem": "layer1"})["DEM"] == "layer1"
        assert pl.defaults("conditiondem", tmp, "D1", {})["DEM"] == dem
        # many dams, or the model's own DEM: not from one dam folder
        assert "DEM" not in pl.defaults("batch", tmp, "D1", {})
        assert "DEM" not in pl.defaults("animation", tmp, "D1", {})
        assert pl.defaults("getdamdata", tmp, "D1", {}) == {"DAMS_FOLDER": tmp, "DAM_ID": "D1"}
        os.remove(dem)                                                     # a missing file is no DEM
        assert pl.dem_for({}, f) == ("", False) and "DEM" not in pl.defaults("rundam", tmp, "D1", {})
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@test
def v390_dem_download_helpers_point_size_and_finish():
    from nd_breach_runner import popup_logic as pl
    from nd_breach_runner.damsuite import damdata as DD
    tmp = tempfile.mkdtemp(prefix="ndbr390_")
    try:
        assert pl.dem_box_half_mi() == 30.0 and pl.dem_box_half_mi(60) == 30.0
        assert pl.dem_box_half_mi(20) == 10.0 and pl.dem_box_half_mi(500) == 50.0
        assert pl.dem_box_half_mi(1) == 1.0 and pl.dem_box_half_mi("x") == 30.0
        a = _dam(tmp, "A", fields={"lon": -100.5, "lat": 47.25})
        b = _dam(tmp, "B", fields={"x": 350000.0, "y": 5230000.0, "crs": "EPSG:26914"})
        assert pl.dam_lonlat(a) == (-100.5, 47.25)
        assert pl.dam_lonlat(b) is None                                     # no transform given
        assert pl.dam_lonlat(b, lambda x, y, crs: (-99.0, 47.0) if crs == "EPSG:26914" else None) \
            == (-99.0, 47.0)
        assert pl.lonlat_text((-100.5, 47.25)) == "-100.500000,47.250000" and pl.lonlat_text(None) == ""
        assert pl.dem_download_target(a, 10.0) == os.path.join(a, "data", "dem_10m.tif")
        assert pl.dem_download_target(a, 2.5).endswith("dem_2.5m.tif")
        # a GeoTIFF in the data folder is renamed and recorded
        src = os.path.join(a, "data", "A_dem_10m.tif")
        open(src, "w").write("tif")
        path, notes = pl.finish_dem_download(src, a, 10.0)
        assert path == os.path.join(a, "data", "dem_10m.tif") and os.path.isfile(path)
        assert not os.path.exists(src)
        assert DD.dam_data(a)["dem"] == path and any("dam.json: DEM recorded" in n for n in notes)
        # a VRT is written as the GeoTIFF by the translate step (GDAL in QGIS) ...
        vrt = os.path.join(a, "data", "A_dem_5m.vrt")
        open(vrt, "w").write("vrt")
        path, _n = pl.finish_dem_download(vrt, a, 5.0, translate=lambda s, d: shutil.copyfile(s, d))
        assert path.endswith("dem_5m.tif") and DD.dam_data(a)["dem"] == path
        # ... and kept as it is without one (or when it fails)
        path, notes = pl.finish_dem_download(vrt, a, 5.0, translate=lambda s, d: 1 / 0)
        assert path == vrt and DD.dam_data(a)["dem"] == vrt and any("stays" in n for n in notes)
        # a DEM outside the dam folder: used where it is; no dam folder: nothing recorded
        other = os.path.join(tmp, "elsewhere.tif")
        open(other, "w").write("x")
        assert pl.finish_dem_download(other, a, 10.0)[0] == other and os.path.isfile(other)
        assert pl.finish_dem_download(other, "", 10.0) == (other, [])
        assert pl.finish_dem_download(os.path.join(tmp, "none.vrt"), a, 10.0)[0] == ""
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@test
def v390_the_suite_dams_folder_is_read_and_set_only_when_empty():
    _fake()
    from nd_breach_runner import popup_logic as pl
    from nd_breach_runner.damsuite import suitedata as SD
    core = sys.modules["qgis.core"]
    tmp = tempfile.mkdtemp(prefix="ndbr390_")
    old = (SD.CONFIG, os.environ.pop(SD.ENV_DAMS, None), getattr(core, "QgsSettings", None))
    try:
        SD.CONFIG = os.path.join(tmp, "config.json")
        if old[2] is not None:
            core.QgsSettings = type("QgsSettings", (old[2],), {"_store": {}})
        dams, other = os.path.join(tmp, "dams"), os.path.join(tmp, "other")
        os.makedirs(dams)
        os.makedirs(other)
        assert pl.suite_dams_folder() == ""
        assert not pl.remember_dams_folder(os.path.join(tmp, "missing"))
        assert pl.remember_dams_folder(dams) and pl.suite_dams_folder() == dams
        assert not pl.remember_dams_folder(other) and pl.suite_dams_folder() == dams   # kept
    finally:
        SD.CONFIG = old[0]
        if old[1] is not None:
            os.environ[SD.ENV_DAMS] = old[1]
        if old[2] is not None:
            core.QgsSettings = old[2]
        shutil.rmtree(tmp, ignore_errors=True)


@test
def v390_window_shows_the_dam_folder_dem_and_takes_a_downloaded_dem():
    _fake()
    from nd_breach_runner.tests.test_popup import qt_fakes
    from nd_breach_runner.damsuite import damdata as DD
    tmp = tempfile.mkdtemp(prefix="ndbr390_")
    try:
        with qt_fakes() as q:
            f = _dam(tmp, "D1", {"dem": "data/dem.vrt"}, {"lon": -100.5, "lat": 47.25})
            dlg = q.dialog.NDBreachRunnerDialog()
            # the DEM box widgets (ND Dam Breach Screening is not importable here)
            dlg.dem_point, dlg.dem_out = q.QtWidgets.QLineEdit(), q.QtWidgets.QLineEdit()
            dlg.dams_edit.setText(tmp)
            dlg.dam_edit.setText("D1")
            dlg._changed()
            dem = os.path.join(f, "data", "dem.vrt")
            # 3.20.0: the row shows the file name and the tooltip the whole path (a 1,100 px
            # path on the row drew over the row below it)
            assert dlg.dem_note.text() == "(from dam folder) dem.vrt"
            assert dlg.dem_note.toolTip() == dem
            assert dlg.settings()["dem"] == ""                         # the choice stays empty
            assert dlg.dem_point.text() == "-100.500000,47.250000"
            assert dlg.dem_out.text() == os.path.join(f, "data")
            dlg._open_tool("rundam")
            assert q.proc.DIALOGS[-1][1]["DEM"] == dem
            # a typed point is kept; the filled one follows the dam
            dlg.dem_point.setText("-99,47")
            dlg.refresh()
            assert dlg.dem_point.text() == "-99,47"
            # the download finished: dem_10m.tif in data/, dam.json, the map and the DEM choice
            got = os.path.join(f, "data", "D1_dem_10m.tif")
            open(got, "w").write("tif")
            path = dlg._dem_downloaded(got, os.path.join(f, "data"), 10.0)
            assert path == os.path.join(f, "data", "dem_10m.tif") and DD.dam_data(f)["dem"] == path
            assert path in q.project.layers and dlg.settings()["dem"] == path
            assert dlg.dem_note.text() == ""                           # now the user's choice
            dlg.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@test
def v390_plugin_has_the_suite_data_entry_once_and_removes_it():
    _fake()
    from nd_breach_runner.tests.test_popup import qt_fakes
    with qt_fakes() as q:
        class Iface(object):
            def __init__(self): self.menu, self.removed = [], []
            def mainWindow(self): return None
            def addPluginToMenu(self, name, action): self.menu.append((name, action))
            def removePluginMenu(self, name, action): self.removed.append((name, action))
            def messageBar(self): return types.SimpleNamespace(pushMessage=lambda *a, **k: None)
        iface = Iface()
        p = q.plugin.NDBreachRunnerPlugin(iface)
        p.initGui()
        own = [a for m, a in iface.menu if m == q.plugin.MENU and a is p.data_action]
        from nd_breach_runner import suite_home
        suite = [a for m, a in iface.menu if m == suite_home.SUITE_MENU and
                 p.run_suite_data in a.triggered._slots]
        assert len(own) == 1 and len(suite) == 1 and suite[0] in p.suite_actions
        calls = []
        sd = types.ModuleType("nd_breach_runner.suite_data")
        sd.open_suite_data = lambda plugin, iface_, key, parent=None, dam_folder="": calls.append(
            (plugin, key, dam_folder)) or "window"
        old = sys.modules.get("nd_breach_runner.suite_data")
        pkg = sys.modules["nd_breach_runner"]
        old_attr = getattr(pkg, "suite_data", None)
        sys.modules["nd_breach_runner.suite_data"] = sd
        pkg.suite_data = sd
        try:
            assert p.run_suite_data() == "window" and calls[-1] == (p, "ndbreach", "")
            tmp = tempfile.mkdtemp(prefix="ndbr390_")
            try:
                f = _dam(tmp, "D1")
                p.run_plugin()
                p.dialog.dams_edit.setText(tmp)
                p.dialog.dam_edit.setText("D1")
                p.run_suite_data()
                assert calls[-1] == (p, "ndbreach", f)                 # the window's dam
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
        finally:
            if old is not None:
                sys.modules["nd_breach_runner.suite_data"] = old
            else:
                sys.modules.pop("nd_breach_runner.suite_data", None)
            if old_attr is not None:
                pkg.suite_data = old_attr
        p.unload()
        assert p.data_action is None and not p.suite_actions
        assert len(iface.removed) == len(iface.menu)


# ------------------------------------------------------------------------------ the wizard
@test
def v390_wizard_starts_with_the_windows_dam_and_the_dam_folder_dem():
    from nd_breach_runner import wizard_logic as wl
    tmp = tempfile.mkdtemp(prefix="ndbr390_")
    try:
        f = _dam(tmp, "D1", {"dem": "data/dem.vrt"}, {"height_ft": 25.0, "storage_acft": 500.0})
        dem = os.path.join(f, "data", "dem.vrt")
        a = wl.initial_answers(tmp, "D1", "")
        assert a["dams_folder"] == tmp and a["dam"]["DAM_ID"] == "D1"
        assert a["dem_id"] == dem and a["dem_from_dam_folder"] and "(from dam folder)" in a["dem_name"]
        assert wl.initial_answers(tmp, "D1", "layer9")["dem_id"] == "layer9"     # the window's DEM
        assert wl.initial_answers() == wl.default_answers()
        # page 1: dam.json gives height and storage before its check (the Dams folder is there)
        a["point"] = (1.0, 2.0, "EPSG:26914")
        wl.apply_dam_record(a)
        assert a["dam"]["HEIGHT_FT"] == 25.0 and a["dam"]["STORAGE_ACFT"] == 500.0
        assert wl.validate_page("dam", a)[0] == []
        # a DEM taken from the dam folder follows a change of dam; a chosen one never does
        _dam(tmp, "D2")
        a["dam"]["DAM_ID"] = "D2"
        wl.apply_dam_folder_dem(a)
        assert a["dem_id"] == "" and not a["dem_from_dam_folder"]
        b = wl.initial_answers(tmp, "D2", "")
        b["dem_id"], b["dam"]["DAM_ID"] = "chosen", "D1"
        assert wl.apply_dam_folder_dem(b) == "" and b["dem_id"] == "chosen"
        assert wl.build_rundam_params(wl.initial_answers(tmp, "D1", ""))["DEM"] == dem
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@test
def v390_run_wizard_hands_the_windows_values_to_the_wizard():
    from nd_breach_runner.tests.test_wizard_logic import stub_qt
    from nd_breach_runner import wizard_logic as wl
    with stub_qt() as mod:
        got = []

        class FakeWizard(object):
            def __init__(self, parent=None, answers=None):
                got.append(answers)
                self.answers = answers or wl.default_answers()

            def exec_(self):
                return 0
        mod._classes()["Wizard"] = FakeWizard
        assert mod.run_wizard(None, dams_folder="/d", dam_id="X1", dem="layer1") is None
        assert got[-1]["dams_folder"] == "/d" and got[-1]["dam"] == {"DAM_ID": "X1"}
        assert got[-1]["dem_id"] == "layer1"
        mod.run_wizard(None)
        assert got[-1] is None                                         # the wizard's own start


# ------------------------------------------------------------------------- SVG converters
@test
def v390_libreoffice_without_draw_is_no_svg_converter():
    from nd_breach_runner.core import report_docx as R
    tmp = tempfile.mkdtemp(prefix="ndbr390_")
    try:
        prog = os.path.join(tmp, "program")
        os.makedirs(prog)
        exe = os.path.join(prog, "soffice")
        open(exe, "w").close()
        open(os.path.join(prog, "libsdbtlo.so"), "w").close()          # not Draw
        assert R._soffice_has_draw(exe) is False
        open(os.path.join(prog, "libsdlo.so"), "w").close()
        assert R._soffice_has_draw(exe) is True
        os.remove(os.path.join(prog, "libsdlo.so"))
        open(os.path.join(prog, "sdlo.dll"), "w").close()
        assert R._soffice_has_draw(exe) is True
        # a program folder that cannot be looked at is not ruled out
        assert R._soffice_has_draw(os.path.join(tmp, "nowhere", "soffice")) is True
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
