"""Tests of 3.18.0: the wizard's DEM download button (its logic in wizard_logic and the terrain
page through the recording Qt doubles), the depth / velocity / arrival rasters written after
every finished LISFLOOD-FP pass, the "Results cut off" record and report section, the tool
version read from metadata.txt, and the ANUGA worker environment without PYTHONNOUSERSITE.
LISFLOOD-FP is stubbed as in test_v320 (fake_models)."""
import configparser
import contextlib
import importlib
import json
import os
import shutil
import sys
import tempfile
import types

import numpy as np

from nd_breach_runner.tests.registry import test
from nd_breach_runner.tests.test_v320 import fake_models, _dam, _settings

PLUGIN = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _tmp():
    return tempfile.mkdtemp(prefix="ndbr_v3180_")


def _recording_writer(calls):
    """npy_writer that also records (file name, array shape) of every raster written."""
    from nd_breach_runner.tests.synthetic import npy_writer

    def w(grid, path):
        calls.append((os.path.basename(path), tuple(grid.data.shape)))
        npy_writer(grid, path)
    return w


def _shapes(calls, name):
    return [shape for n, shape in calls if n == name]


def _touch(path):
    """A stand-in for a downloaded GeoTIFF."""
    with open(path, "wb") as f:
        f.write(b"tif")


def _json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


@contextlib.contextmanager
def _inside_models(arr_hr):
    """LISFLOOD-FP stubbed with a flood that stays inside the first domain (0 - 1000 m east of
    the dam, |y| < 300 m), 0.5 m deep, arriving at `arr_hr` everywhere."""
    from nd_breach_runner.core import lisflood as lf

    def fake_run(wd, par, st, progress=None, cancel=None, log=None, name="run"):
        return 60.0

    def fake_read(wd, dem, st, hydrograph, name="run"):
        X, Y = np.meshgrid(dem.x_centers(), dem.y_centers())
        dd = np.where((X > 0) & (X < 1000) & (np.abs(Y) < 300), 0.5, np.nan)
        vel = np.where(np.isfinite(dd), 1.0, np.nan)
        arr = np.where(np.isfinite(dd), float(arr_hr), np.nan)
        wse = np.where(np.isfinite(dd), dem.data + 0.5, np.nan)
        return lf.Envelopes(dem.like(dd, np.nan), dem.like(vel, np.nan), dem.like(arr, np.nan),
                            dem.like(wse, np.nan),
                            {"max_volume_error_pct": 0.1, "peak_inflow_cfs": 1000.0}, [], {})
    old = (lf.run, lf.read_results)
    lf.run, lf.read_results = fake_run, fake_read
    try:
        yield
    finally:
        lf.run, lf.read_results = old


# ------------------------------------------------------- pass rasters and the cut-off record
@test
def v3180_each_pass_leaves_its_rasters_and_the_run_limit_is_a_cut_off():
    from nd_breach_runner.core import workflow as wf
    from nd_breach_runner.damsuite import dam_record as DR
    from nd_breach_runner.tests.synthetic import SyntheticTerrain
    d = _tmp()
    try:
        writes, logs = [], []
        # the stub flood runs east without end: both passes leave the east edge, the second is
        # the last the settings allow
        with fake_models():
            r = wf.run_dam(_dam(), (0.0, 0.0), _settings(max_iterations=2, dam_record={}),
                           SyntheticTerrain(), _recording_writer(writes), os.path.join(d, "T1"),
                           log=logs.append)
        rep = _json(r.report_json)
        assert len(rep["lisflood_runs"]) == 2
        # one write of depth, velocity and arrival per pass, then the final write
        for name in ("Depth (Max).tif", "Velocity (Max).tif", "Arrival Time.tif"):
            shapes = _shapes(writes, name)
            assert len(shapes) == 3, (name, shapes)
            assert shapes[0][1] < shapes[1][1] == shapes[2][1], shapes   # pass 2 is wider
        assert len(_shapes(writes, "WSE (Max).tif")) == 1                 # the final write only
        assert rep["products_pass"] == 2
        # 3.20.0: the pass log names the rasters that can already be opened
        pass_logs = [m for m in logs if "you can already open" in m]
        assert len(pass_logs) == 2 and "Depth (Max).tif" in pass_logs[0] \
            and "Arrival Time.tif" in pass_logs[0] and "Velocity (Max).tif" in pass_logs[0]
        assert rep["products_pass_files"] == ["Arrival Time.tif", "Depth (Max).tif",
                                              "Velocity (Max).tif"]
        depth = np.load(os.path.splitext(r.final["depth"])[0] + ".npy")
        assert depth.shape == _shapes(writes, "Depth (Max).tif")[-1]
        # the cut-off: the warning is kept and recorded with its reason
        co = rep["cut_off"]
        assert co["reason"] == wf.CUT_OFF_RUNS == "domain-run limit"
        assert co["detail"].startswith("Stopped after 2 runs with the flood leaving the domain "
                                       "through E") and co["detail"] in rep["warnings"]
        md = open(r.report_md, encoding="utf-8").read()
        assert "## Results cut off" in md and "## Check these" in md
        assert md.index("## Results cut off") < md.index("## Check these")
        assert "Reason: domain-run limit." in md and co["detail"] in md
        assert rep["tool"] == "ND Dam Breach Runner " + wf.VERSION
        # dam.json's run entry says so too
        rec = DR.load(os.path.join(d, "T1"))
        st = rec.latest("modelled")
        assert st["result"]["cut_off"] == co, st["result"]
        assert st["version"] == wf.VERSION
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
def v3180_a_cancelled_run_keeps_the_last_finished_pass():
    from nd_breach_runner.core import workflow as wf
    from nd_breach_runner.core import lisflood as lf
    from nd_breach_runner.tests.synthetic import SyntheticTerrain
    d = _tmp()
    try:
        calls, writes = [], []
        folder = os.path.join(d, "T2")
        with fake_models(calls=calls):
            try:
                wf.run_dam(_dam(), (0.0, 0.0), _settings(max_iterations=3), SyntheticTerrain(),
                           _recording_writer(writes), folder,
                           cancel=lambda: len(calls) >= 1)      # Cancel after the first pass
                raise AssertionError("the run should have been cancelled")
            except lf.Cancelled:
                pass
        assert len(calls) == 1
        for name in ("Depth (Max).tif", "Velocity (Max).tif", "Arrival Time.tif"):
            assert os.path.isfile(os.path.join(folder, name)), name
            assert len(_shapes(writes, name)) == 1
        assert not os.path.exists(os.path.join(folder, "WSE (Max).tif"))
        assert not os.path.exists(os.path.join(folder, wf.MODEL_FOLDER, "run_report.json"))
        depth = np.load(os.path.join(folder, "Depth (Max).npy"))
        assert depth.shape == _shapes(writes, "Depth (Max).tif")[0]
        assert np.isfinite(depth).any()
        # feet: the stub's 0.5 m band is 1.64 ft
        assert abs(np.nanmax(depth) - 0.5 / 0.3048) < 1e-6
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
def v3180_largest_domain_side_and_duration_cap_are_cut_offs():
    from nd_breach_runner.core import workflow as wf
    from nd_breach_runner.core import lisflood as lf
    from nd_breach_runner.tests.synthetic import SyntheticTerrain
    d = _tmp()
    try:
        # the first extension would pass the largest domain side
        with fake_models():
            r = wf.run_dam(_dam(), (0.0, 0.0), _settings(max_iterations=3, max_extent_mi=1.0),
                           SyntheticTerrain(), _recording_writer([]), os.path.join(d, "T3"))
        rep = _json(r.report_json)
        assert rep["cut_off"]["reason"] == wf.CUT_OFF_EXTENT == "largest domain side"
        assert rep["cut_off"]["detail"].startswith("The domain reached the maximum extent (1 mi)")
        assert rep["products_pass"] == 1 and len(rep["lisflood_runs"]) == 1
        assert "Reason: largest domain side." in open(r.report_md, encoding="utf-8").read()

        # a flood that stays inside the domain but is still moving when the chosen 4 h end
        with _inside_models(3.9):
            r = wf.run_dam(_dam(), (0.0, 0.0), _settings(max_iterations=2), SyntheticTerrain(),
                           _recording_writer([]), os.path.join(d, "T4"))
        rep = _json(r.report_json)
        assert rep["cut_off"]["reason"] == wf.CUT_OFF_DURATION == "duration cap"
        assert "still advancing at the end of the 4.0 h simulation" in rep["cut_off"]["detail"]
        assert rep["lisflood_runs"][0]["edges_over_tolerance"] == []
        assert rep["products_pass"] == 1
        # a clean finish records no cut-off; without write_final nothing goes to the dam folder
        writes = []
        with _inside_models(0.5):
            r = wf.run_dam(_dam(), (0.0, 0.0), _settings(max_iterations=2, write_final=False),
                           SyntheticTerrain(), _recording_writer(writes), os.path.join(d, "T5"))
        rep = _json(r.report_json)
        assert "cut_off" not in rep and "products_pass" not in rep
        assert len(rep["lisflood_runs"]) == 1 and len(rep["warnings"]) == 0, rep["warnings"]
        assert not os.path.exists(os.path.join(d, "T5", "Depth (Max).tif"))
        assert not any(n == "Depth (Max).tif" for n, _s in writes), writes
        assert "## Results cut off" not in open(r.report_md, encoding="utf-8").read()
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
def v3180_cut_off_section_from_a_report_dict():
    from nd_breach_runner.core import workflow as wf
    from nd_breach_runner.tests.synthetic import SyntheticTerrain
    assert wf.cut_off_lines(None) == [] and wf.cut_off_lines({}) == []
    for reason in (wf.CUT_OFF_DURATION, wf.CUT_OFF_RUNS, wf.CUT_OFF_EXTENT):
        lines = wf.cut_off_lines({"reason": reason, "detail": "The flood is still moving."})
        assert lines[0] == "## Results cut off" and "Reason: {}.".format(reason) in lines[2]
        assert "The flood is still moving." in lines and lines[-1] == ""
        assert not any("—" in ln for ln in lines)                 # no em-dashes
    # through render_report on a real report dict: before "## Check these"
    d = _tmp()
    try:
        with _inside_models(0.5):
            r = wf.run_dam(_dam(), (0.0, 0.0), _settings(max_iterations=1), SyntheticTerrain(),
                           _recording_writer([]), os.path.join(d, "T1"))
        rep = _json(r.report_json)
        assert "cut_off" not in rep                              # one run was enough here
        md = wf.render_report(rep)
        assert "## Results cut off" not in md
        rep["cut_off"] = {"reason": "duration cap", "detail": "The flood is still advancing "
                                                              "after 336 h; results stop there."}
        rep["warnings"] = ["something else to check"]
        md = wf.render_report(rep)
        i, j = md.index("## Results cut off"), md.index("## Check these")
        assert 0 < i < j and "Reason: duration cap." in md[i:j] and "after 336 h" in md[i:j]
        assert "## Result\n" in md[:i]
        rep["warnings"] = []
        assert "## Results cut off" in wf.render_report(rep)     # with nothing else to check
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ------------------------------------------------------------------------------- the version
@test
def v3180_version_comes_from_metadata():
    from nd_breach_runner.core import workflow as wf
    from nd_breach_runner.core import damjson
    from nd_breach_runner.core import probabilistic          # noqa: F401 - uses workflow.VERSION
    cp = configparser.ConfigParser(interpolation=None, strict=False)
    with open(os.path.join(PLUGIN, "metadata.txt"), encoding="utf-8-sig") as f:
        cp.read_file(f)
    v = cp.get("general", "version").strip()
    assert v and wf.VERSION == wf.metadata_version() == damjson.plugin_version() == v
    assert wf.VERSION_FALLBACK == v, "keep the fallback literal in step with metadata.txt"
    assert cp.get("general", "changelog").strip().startswith(v + " ")
    # the file cannot be read: the fallback
    d = _tmp()
    try:
        old = wf.__file__
        wf.__file__ = os.path.join(d, "core", "workflow.py")
        try:
            assert wf.metadata_version("9.9.9") == "9.9.9"
            assert wf.metadata_version() == wf.VERSION_FALLBACK
            assert damjson.plugin_version() == wf.VERSION
        finally:
            wf.__file__ = old
        assert wf.metadata_version() == v
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------------- the ANUGA worker env
@test
def v3180_anuga_worker_does_not_inherit_the_user_site_switch():
    from nd_breach_runner.core import anuga_model as am
    assert "PYTHONNOUSERSITE" in am.DROPPED_ENV
    base = {"PYTHONHOME": "a", "PYTHONPATH": "b", "QGIS_PREFIX_PATH": "c", "PYTHONNOUSERSITE": "1",
            "PATH": "/bin"}
    assert am.worker_env(0, base=base) == {"PATH": "/bin"}
    assert am.worker_env(4, base={})["OMP_NUM_THREADS"] == "4"
    d = _tmp()
    old_env = {k: os.environ.get(k) for k in ("PYTHONNOUSERSITE", "PYTHONPATH")}
    os.environ["PYTHONNOUSERSITE"], os.environ["PYTHONPATH"] = "1", "/somewhere"
    seen = {}

    def fake_run(cmd, **kw):
        seen["run"] = kw
        return types.SimpleNamespace(returncode=0, stdout="3.1.9 3.12.3\n", stderr="")

    class FakeProc(object):
        def __init__(self, cmd, cwd=None, env=None, **kw):
            seen["popen"] = env
            self.stdout = iter(["PROGRESS 50 meshing\n"])
            self.returncode = 0

        def wait(self):
            return 0

        def kill(self):
            pass
    old = (am.subprocess.run, am.subprocess.Popen)
    am.subprocess.run, am.subprocess.Popen = fake_run, FakeProc
    try:
        r = am.check_anuga(sys.executable, d)
        assert r["ok"] and r["message"] == "ANUGA 3.1.9 (Python 3.12.3)", r
        prog = []
        st = types.SimpleNamespace(python=sys.executable, threads=2)
        am._launch(st, "job.npz", "result.npz", d, lambda p, m: prog.append((p, m)), None)
        assert prog == [(50.0, "ANUGA meshing")]
        for env in (seen["run"]["env"], seen["popen"]):
            for k in am.DROPPED_ENV:
                assert k not in env, (k, sorted(env))
        assert seen["popen"]["OMP_NUM_THREADS"] == "2"
    finally:
        am.subprocess.run, am.subprocess.Popen = old
        for k, v in old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(d, ignore_errors=True)


# ------------------------------------------------------------------- the wizard's DEM button
def _answers(dams, dam_id="D1", point=(-100.5, 47.25, "EPSG:4326")):
    from nd_breach_runner import wizard_logic as wl
    a = wl.default_answers(dams)
    a["dam"]["DAM_ID"] = dam_id
    a["point"] = point
    return a


@test
def v3180_wizard_dem_download_logic():
    from nd_breach_runner import wizard_logic as wl
    from nd_breach_runner import popup_logic as pl
    from nd_breach_runner.damsuite import damdata as DD
    from nd_breach_runner.damsuite import dam_record as DR
    # the download tool's file names (its local_tif_stem rule)
    assert wl.dem_file_name("DWR-1234", 10.0) == "DWR-1234_10m_5070.tif"
    assert wl.dem_file_name("ND 001/23", 1) == "ND_001_23_1m_5070.tif"
    assert wl.dem_file_name("  _x_ ", 10) == "x_10m_5070.tif"
    assert wl.dem_file_name("", 10, (-100.5, 47.25)) == "dam_m100p5000_47p2500_10m_5070.tif"
    assert wl.dem_file_name(None, 10) == "dam_10m_5070.tif"
    assert wl.dem_cell_m(0) == 10.0 and wl.dem_cell_m(1) == 1.0 and wl.dem_cell_m("x") == 10.0
    assert [lbl for _c, lbl in wl.DEM_RESOLUTIONS] == ["10 m", "1 m"]
    assert wl.DEM_DOWNLOAD_ALG == "nddam:downloaddem"
    d = _tmp()
    try:
        dams = os.path.join(d, "Dams")
        folder = os.path.join(dams, "D1")
        os.makedirs(os.path.join(folder, "data"))
        a = _answers(dams)
        assert wl.dam_folder_of(a) == folder and wl.dam_folder_of({}) == ""
        assert wl.dam_folder_of(_answers(dams, "A/B:C")) == os.path.join(dams, "A_B_C")
        # the dam point: lon / lat as given, a projected point through the transform, dam.json
        assert wl.dam_lonlat(a) == (-100.5, 47.25)
        b = _answers(dams, point=(500000.0, 5200000.0, "EPSG:26914"))
        assert wl.dam_lonlat(b) is None                                   # no transform, no record
        assert wl.dam_lonlat(b, lambda x, y, crs: (-99.0, 46.9)) == (-99.0, 46.9)
        assert wl.dam_lonlat(b, lambda x, y, crs: (_ for _ in ()).throw(ValueError("x"))) is None
        rec = DR.load(folder, create=True)
        rec.set_many({"lon": -101.25, "lat": 48.5}, "ndhazard", "2.0.0")
        rec.save()
        assert wl.dam_lonlat(b) == (-101.25, 48.5)
        c = _answers(dams, point=None)
        assert wl.dam_lonlat(c) == (-101.25, 48.5)
        # the parameters: the Runner window's choices for this dam
        params, problems = wl.dem_download_params(a, 0)
        assert problems == []
        assert params == {"SOURCE": 0, "SCOPE": 0, "DAM_POINT": "-100.500000,47.250000",
                          "BUFFER_MI": pl.dem_box_half_mi(pl.settings_max_extent_mi()),
                          "COUNTY": "", "RESOLUTION": 0, "DWR_PATH": "", "ASCII_FT": False,
                          "IMAGINE": True, "OUT": os.path.join(folder, "data"),
                          "DAMS_FOLDER": dams, "DAM_ID": "D1"}, params
        p1, _ = wl.dem_download_params(a, 1, buffer_mi=15)
        assert p1["RESOLUTION"] == 1 and p1["BUFFER_MI"] == 15.0
        params, problems = wl.dem_download_params(wl.default_answers(""), 0)
        assert params == {} and len(problems) == 3
        assert problems[0].startswith("Choose the Dams folder") and \
            problems[1].startswith("Give the dam ID") and "dam point" in problems[2]
        _p, problems = wl.dem_download_params(_answers(dams, "D9", point=None), 0)
        assert problems == ["The dam point is not known: pick the dam from the layer or type its "
                            "location on the dam page."]
        # the tool's result
        assert wl.dem_download_result({"VRT": "/x/a.tif|layername=a", "OUT": "/x"}) == "/x/a.tif"
        assert wl.dem_download_result({}) == "" and wl.dem_download_result(None) == ""
        assert wl.dem_download_result("/x/b.tif") == "/x/b.tif"
        # what the download leaves, and choosing it as the wizard's DEM
        tif = os.path.join(folder, "data", "D1_10m_5070.tif")
        _touch(tif)
        assert wl.find_downloaded_dem(a, 10.0) == tif
        assert wl.find_downloaded_dem(a, 10.0, since=os.path.getmtime(tif) + 100) == ""
        assert wl.find_downloaded_dem(a, 1.0) == "" and wl.find_downloaded_dem({}, 10.0) == ""
        a["dem_id"], a["dem_name"] = "layer_before", "before"
        path, notes = wl.finish_dem_download(a, {"VRT": tif, "OUT": os.path.dirname(tif)}, 10.0)
        target = os.path.join(folder, "data", "dem_10m.tif")
        assert path == target and os.path.isfile(target) and not os.path.exists(tif)
        assert any(n.startswith("dam.json: DEM recorded") for n in notes), notes
        assert a["dem_id"] == target and a["dem_from_dam_folder"] and \
            a["dem_name"] == "dem_10m.tif (from dam folder)"
        assert os.path.normcase(DD.dam_data(folder)["dem"]) == os.path.normcase(target)
        assert wl.build_rundam_params(a)["DEM"] == target
        # nothing came back (the tool's window was closed): the choice stays
        a["dem_id"], a["dem_name"], a["dem_from_dam_folder"] = "chosen", "mine", False
        os.remove(target)
        assert wl.finish_dem_download(a, {}, 10.0) == ("", ["The download gave no DEM file."])
        assert wl.finish_dem_download(a, None, 1.0)[0] == ""
        assert a["dem_id"] == "chosen" and a["dem_name"] == "mine" and not a["dem_from_dam_folder"]
        # an older QGIS returns nothing but the file is there: found by its name and time
        _touch(tif)
        path, _n = wl.finish_dem_download(a, {}, 10.0, since=os.path.getmtime(tif) - 5)
        assert path == target and a["dem_id"] == target
        # a 1 m download goes to dem_1m.tif beside it
        tif1 = os.path.join(folder, "data", "D1_1m_5070.tif")
        _touch(tif1)
        path, _n = wl.finish_dem_download(a, {"VRT": tif1}, 1.0)
        assert path == os.path.join(folder, "data", "dem_1m.tif") and a["dem_id"] == path
        assert os.path.isfile(target)
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
def v3180_wizard_terrain_page_downloads_and_chooses_the_dem():
    """The terrain page through the recording Qt doubles of test_popup: the button builds the
    tool's parameters, opens the tool's dialog, takes the file it wrote and chooses it."""
    from nd_breach_runner.tests import fake_qgis
    if "qgis.core" not in sys.modules or not hasattr(sys.modules["qgis.core"],
                                                      "QgsProcessingAlgorithm"):
        fake_qgis.install()
    from nd_breach_runner.tests.test_popup import qt_fakes
    from nd_breach_runner import wizard_logic as wl
    d = _tmp()
    try:
        dams = os.path.join(d, "Dams")
        os.makedirs(os.path.join(dams, "D1"))
        with qt_fakes():
            wiz = importlib.reload(importlib.import_module("nd_breach_runner.wizard"))
            W = sys.modules["qgis.PyQt.QtWidgets"]
            proc = sys.modules["processing"]
            try:
                del W.EVENTS["messages"][:]
                cls = wiz._classes()
                page = cls["TerrainPage"]()
                holder = W.QWizard()
                holder.answers = _answers(dams)
                holder.addPage(page)
                assert page.wizard() is holder
                page.initializePage()
                assert page.dem_download.toolTip().startswith("USGS 3DEP") and \
                    page.dem_res.currentText() == "10 m" and page.dem_res.count() == 2
                # nothing to download for: the note says what is missing, no dialog opens
                holder.answers["dam"]["DAM_ID"] = ""
                calls = []
                proc.execAlgorithmDialog = lambda alg, params: calls.append((alg, params)) or {}
                page.download_dem()
                assert calls == [] and page.dem_note.text().startswith(
                    "The DEM cannot be downloaded yet. Give the dam ID")
                # the download: the tool writes <stem>_10m_5070.tif into <dam folder>/data
                holder.answers["dam"]["DAM_ID"] = "D1"

                def exec_dialog(alg, params):
                    calls.append((alg, params))
                    assert os.path.isdir(params["OUT"]) and "Downloading the 10 m DEM for D1" in \
                        page.dem_note.text()
                    assert not page.dem_download.isEnabled()
                    tif = os.path.join(params["OUT"], wl.dem_file_name(params["DAM_ID"], 10))
                    _touch(tif)
                    return {"VRT": tif, "OUT": params["OUT"]}
                proc.execAlgorithmDialog = exec_dialog
                page.download_dem()
                assert len(calls) == 1 and calls[0][0] == "nddam:downloaddem"
                p = calls[0][1]
                assert p["DAM_ID"] == "D1" and p["DAMS_FOLDER"] == dams and p["RESOLUTION"] == 0 \
                    and p["OUT"] == os.path.join(dams, "D1", "data") and \
                    p["DAM_POINT"] == "-100.500000,47.250000"
                target = os.path.join(dams, "D1", "data", "dem_10m.tif")
                assert os.path.isfile(target)
                assert page.dem.currentData() == target, page.dem.currentData()
                assert page.dem.currentText() == "dem_10m.tif (from dam folder)"
                assert page.dem_note.text().startswith("DEM downloaded and chosen: " + target)
                assert page.dem_download.isEnabled()
                a = holder.answers
                assert a["dem_id"] == target and a["dem_from_dam_folder"]
                assert page.validatePage() is True and a["dem_id"] == target
                # 1 m chosen: the tool's resolution index follows
                page.dem_res.setCurrentIndex(1)
                os.remove(target)

                def exec_1m(alg, params):
                    calls.append((alg, params))
                    tif = os.path.join(params["OUT"], wl.dem_file_name("D1", 1))
                    _touch(tif)
                    return {"VRT": tif}
                proc.execAlgorithmDialog = exec_1m
                page.download_dem()
                assert calls[-1][1]["RESOLUTION"] == 1
                assert page.dem.currentData() == os.path.join(dams, "D1", "data", "dem_1m.tif")
                # the tool's window closed without a download: the choice stays (the files
                # from before are older than the click, so they are not taken for a download)
                t_old = os.path.getmtime(os.path.join(dams, "D1", "data", "dem_1m.tif")) - 60.0
                os.utime(os.path.join(dams, "D1", "data", "dem_1m.tif"), (t_old, t_old))
                proc.execAlgorithmDialog = lambda alg, params: {}
                page.download_dem()
                assert page.dem_note.text().startswith("No DEM was downloaded")
                assert page.dem.currentData() == os.path.join(dams, "D1", "data", "dem_1m.tif")
                # the tool is missing: said in the note, nothing opened
                core = sys.modules["qgis.core"]
                old_reg = core.QgsApplication.processingRegistry
                core.QgsApplication.processingRegistry = staticmethod(
                    lambda: types.SimpleNamespace(algorithmById=lambda _i: None))
                try:
                    n = len(calls)
                    page.download_dem()
                    assert len(calls) == n and page.dem_note.text() == wl.DEM_NOT_INSTALLED
                finally:
                    core.QgsApplication.processingRegistry = old_reg
                assert W.EVENTS["messages"] == [], W.EVENTS["messages"]   # no _safe failure
            finally:
                wiz._CLASSES.clear()
    finally:
        shutil.rmtree(d, ignore_errors=True)
