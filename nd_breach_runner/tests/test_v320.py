"""Tests of the 3.2.0 items: per-run rasters for the class probability (item 6), the class-stop
rule of the domain extension (item 7), one EAP map (item 15), dam.json (item 2) and the
suite version checks (item 5). No LISFLOOD-FP: the model runs go through workflow.run_dam
with the LISFLOOD-FP call and its result reading stubbed (as test_report_docx does), on the
synthetic valley."""
import ast
import contextlib
import importlib
import json
import os
import py_compile
import shutil
import sys
import tempfile

import numpy as np

from nd_breach_runner.tests.registry import test

PLUGIN = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FT = 0.3048


def _tmp():
    return tempfile.mkdtemp(prefix="ndbr_v320_")


# ----------------------------------------------------------------------------------- helpers
@contextlib.contextmanager
def fake_models(depth_m=0.50, base_m=0.40, calls=None, vary=None):
    """LISFLOOD-FP stubbed: a flood band |y| < 300 m east of the dam, `depth_m` deep with the
    breach and `base_m` without it (the "_base" run folders). `vary(wd)` -> factor on the
    depth (per-run differences)."""
    from nd_breach_runner.core import lisflood as lf

    def fake_run(wd, par, st, progress=None, cancel=None, log=None, name="run"):
        if calls is not None:
            calls.append(wd)
        return 60.0

    def fake_read(wd, dem, st, hydrograph, name="run"):
        X, Y = np.meshgrid(dem.x_centers(), dem.y_centers())
        v = base_m if wd.rstrip(os.sep).endswith("_base") else depth_m
        if vary is not None:
            v = v * vary(wd)
        d = np.where((X > 0) & (np.abs(Y) < 300), v, np.nan)
        vel = np.where(np.isfinite(d), 1.2, np.nan)
        arr = np.where(np.isfinite(d), X / 1.0 / 3600.0 + 0.1, np.nan)
        wse = np.where(np.isfinite(d), dem.data + np.nan_to_num(d), np.nan)
        stats = {"max_volume_error_pct": 0.1, "peak_inflow_cfs": 1000.0}
        return lf.Envelopes(dem.like(d, np.nan), dem.like(vel, np.nan), dem.like(arr, np.nan),
                            dem.like(wse, np.nan), stats, [], {})
    old = (lf.run, lf.read_results)
    lf.run, lf.read_results = fake_run, fake_read
    try:
        yield
    finally:
        lf.run, lf.read_results = old


def _dam(**k):
    from nd_breach_runner.core import breach as br
    d = dict(dam_id="T1", dam_height_ft=25.0, storage_top_acft=500.0, top_of_dam_ft=1850.0,
             surface_area_top_ac=60.0)
    d.update(k)
    return br.DamData(**d)


def _settings(**k):
    from nd_breach_runner.core import workflow
    d = dict(cell_size_m=50.0, planning_cell_m=90.0, initial_length_mi=1.0, half_width_mi=0.5,
             expand_step_mi=1.0, max_iterations=2, lisflood_exe=sys.executable, sim_time_hr=4.0,
             raise_crest=False)        # v3.11.0: the legacy tests are about other things
    d.update(k)
    return workflow.RunSettings(**d)


def _run(folder, st, dam=None, **fm):
    from nd_breach_runner.core import workflow
    from nd_breach_runner.tests.synthetic import SyntheticTerrain, npy_writer
    with fake_models(**fm):
        return workflow.run_dam(dam or _dam(), (0.0, 0.0), st, SyntheticTerrain(), npy_writer,
                                folder)


def _npy(path):
    return np.load(os.path.splitext(path)[0] + ".npy")


# =================================================================== item 7: class-stop rule
def _band_grid(nx=120, ny=40, dx=20.0):
    from nd_breach_runner.core.grid import Grid
    g = Grid(np.zeros((ny, nx)), 0.0, ny * dx / 2.0, dx)
    X, Y = np.meshgrid(g.x_centers(), g.y_centers())
    return g, X, Y


@test
def stoprule_defaults_are_nd_dam_hazards_termination_defaults():
    from nd_breach_runner.core import stoprule as sr, workflow
    assert (sr.DEFAULT_THRESHOLD_FT, sr.DEFAULT_CONFIRM, sr.DEFAULT_STATION_MI,
            sr.DEFAULT_SEARCH_FT) == (2.0, 3, 0.5, 150.0)
    assert "chosen by the engineer" in sr.DEFAULTS_NOTE and "not requirements" in sr.DEFAULTS_NOTE
    st = workflow.RunSettings()
    assert st.class_stop is False and st.class_stop_threshold_ft == 2.0
    assert st.class_stop_confirm == 3 and st.eap_map == "auto" and st.dam_record is None
    # the checkpoint key of a default run is the 3.1.x key (resume keeps working); the
    # v3.4.0 settings (near-dam refine, solver) are left out while at their defaults too
    # (v3.11.0: the raise-the-dam-line setting is on by default, so it is in the key)
    old = {k: v for k, v in st.as_dict().items() if k not in (
        "resume", "eap_map", "dam_record", "lisflood_solver", "close_upstream_edges")
        and not k.startswith("class_stop")
        and k not in workflow._REFINE_KEYS}          # refine_lines (ANUGA, 3.0) stays
    assert workflow.key_settings(st) == old
    assert "raise_crest" in old
    assert "raise_crest" not in workflow.key_settings(workflow.RunSettings(raise_crest=False))
    # v3.11.0: the refine settings never change the main runs, so they are not in the key
    assert "refine_near" not in workflow.key_settings(workflow.RunSettings(refine_near=True))
    assert "lisflood_solver" in workflow.key_settings(workflow.RunSettings(lisflood_solver="fv1"))
    assert "class_stop" in workflow.key_settings(workflow.RunSettings(class_stop=True))
    assert "eap_map" not in workflow.key_settings(workflow.RunSettings(eap_map="svg"))


@test
def stoprule_stations_take_the_deepest_wet_cell_within_the_search_distance():
    from nd_breach_runner.core import stoprule as sr
    g, X, Y = _band_grid()
    depth = np.where(np.abs(Y) < 60, 1.0, np.nan)
    depth[20, 50] = 3.0                         # one deep cell near x = 1010, y = -10
    path = [(10.0, 0.0), (2390.0, 0.0)]
    st = sr.stations(path, g, depth, spacing_m=500.0, radius_m=45.0)
    assert [s["rm"] for s in st][:3] == [0.0, round(500 / 1609.344, 3), round(1000 / 1609.344, 3)]
    assert st[-1]["is_edge"] and abs(st[-1]["rm"] - 2380 / 1609.344) < 1e-3
    assert all(s["base_ft"] is None for s in st)
    by = {round(s["x"]): s for s in st}
    assert abs(by[1010]["depth_ft"] - 3.0 / FT) < 1e-3          # the deep cell is within 45 m
    assert abs(by[510]["depth_ft"] - 1.0 / FT) < 1e-3
    base = np.where(np.isfinite(depth), 0.8, np.nan)
    st2 = sr.stations(path, g, depth, base, 500.0, 45.0)
    assert abs(st2[1]["value_ft"] - 0.2 / FT) < 1e-3 and st2[1]["base_ft"] is not None
    # a big grid is traced on a block-minimum copy that keeps the channel
    from nd_breach_runner.core.grid import Grid
    big = Grid(np.ones((300, 400)) * 5.0, 0.0, 3000.0, 10.0)
    big.data[150, :] = 1.0
    c = sr.coarse_grid(big, max_cells=10000)
    assert c.nx * c.ny <= 12000 and c.dx > 10.0 and np.nanmin(c.data) == 1.0
    assert sr.coarse_grid(big, max_cells=10 ** 7) is big


@test
def stoprule_accepts_an_edge_where_the_rise_dies_out_and_says_why():
    from nd_breach_runner.core import stoprule as sr
    g, X, Y = _band_grid()                      # 2400 m long, the path leaves through E
    path = [(10.0, 0.0), (2390.0, 0.0)]
    depth = np.where(np.abs(Y) < 100, 2.0, np.nan)     # 6.6 ft everywhere
    # rise: 3 ft near the dam, below 2 ft on the last 1300 m
    base = np.where(np.isfinite(depth), np.where(X < 1100, 2.0 - 3 * FT, 2.0 - 0.5 * FT),
                    np.nan)
    dec = sr.judge_edge("E", g, depth, path, base, 2.0, 3, 0.25, 150.0)
    assert dec["accept"] and dec["criterion"] == "rise", dec["reason"]
    assert dec["reason"].startswith("edge accepted: rise over the baseline below 2 ft for ")
    assert "from station" in dec["reason"] and "nothing beyond the edge was checked" in \
        dec["reason"]
    assert dec["stations_below"] >= 4 and dec["from_rm"] > 0.6 and dec["edge_value_ft"] < 2.0
    assert any("Rise (ft)" in ln for ln in sr.station_table(dec))
    # the depth alone (no baseline) is 6.6 ft: kept
    dec = sr.judge_edge("E", g, depth, path, None, 2.0, 3, 0.25, 150.0)
    assert not dec["accept"] and dec["criterion"] == "depth"
    assert "below 2 ft for only 0 station(s)" in dec["reason"], dec["reason"]
    # too few stations below: confirm + 1 are needed
    base2 = np.where(np.isfinite(depth), np.where(X < 1900, 2.0 - 3 * FT, 2.0 - 0.5 * FT),
                     np.nan)
    dec = sr.judge_edge("E", g, depth, path, base2, 2.0, 3, 0.25, 150.0)
    assert not dec["accept"] and "needed 4" in dec["reason"], dec["reason"]
    assert sr.judge_edge("E", g, depth, path, base2, 2.0, 1, 0.25, 150.0)["accept"]
    # deep water leaving elsewhere on the same edge (a side channel) keeps the edge
    side = base.copy()
    side[2, -1] = 0.0                            # no baseline there: rise = 2 m
    depth2 = depth.copy()
    depth2[2, -1] = 2.0
    dec = sr.judge_edge("E", g, depth2, path, side, 2.0, 3, 0.25, 150.0)
    assert not dec["accept"] and "deepest water leaving through the E edge" in dec["reason"]
    # only the edge the flow path leaves through is judged
    dec = sr.judge_edge("N", g, depth, path, base, 2.0, 3, 0.25, 150.0)
    assert not dec["accept"] and "through the E edge" in dec["reason"], dec["reason"]
    # (b): receptors ahead keep the edge without computing (a)
    dec = sr.judge_edge("E", g, depth, path, base, receptors={
        "extend": True, "reason": "receptors downstream: 3 structures"})
    assert not dec["accept"] and "(rule (b))" in dec["reason"] and not dec["stations"]
    dec = sr.judge_edge("E", g, depth, path, base, 2.0, 3, 0.25, 150.0, receptors={
        "extend": False, "reason": "no structures, roads or rail within 5.0 mi downstream"})
    assert dec["accept"] and "no structures, roads or rail within 5.0 mi" in dec["reason"]


@test
def stoprule_exit_edge_and_flow_path_on_a_valley():
    from nd_breach_runner.core import stoprule as sr
    from nd_breach_runner.tests.synthetic import SyntheticTerrain
    dem = SyntheticTerrain().read((-500.0, -800.0, 2500.0, 800.0), 50.0)
    path = sr.flow_path(dem, (60.0, 0.0))
    assert sr.exit_edge(dem, path) == "E", path[-3:]
    assert sr.exit_edge(dem, [(1000.0, 0.0), (1050.0, 0.0)]) is None
    assert sr.exit_edge(dem, [(1000.0, 0.0)]) is None


@test
def class_stop_rule_stops_the_extension_and_reuses_the_baseline():
    d = _tmp()
    try:
        # rule off (default): the flood leaves E 1.6 ft deep, the domain grows
        calls = []
        r = _run(os.path.join(d, "off", "T1"), _settings(ambient_cfs=100.0), calls=calls)
        rep = json.load(open(r.report_json))
        assert len(rep["lisflood_runs"]) == 2 and "class_stop_rule" not in rep
        assert sum(1 for c in calls if c.endswith("lisflood_base")) == 1
        # rule on: the rise over the ambient-flow baseline is 0.33 ft: accepted after run 1,
        # and the no-breach run made for the rule is the final baseline (not run twice)
        calls = []
        r = _run(os.path.join(d, "on", "T1"), _settings(
            ambient_cfs=100.0, class_stop=True, class_stop_station_mi=0.25), calls=calls)
        rep = json.load(open(r.report_json))
        assert len(rep["lisflood_runs"]) == 1, rep["lisflood_runs"]
        assert [os.path.basename(c) for c in calls] == ["lisflood", "lisflood_base"], calls
        run1 = rep["lisflood_runs"][0]
        assert run1["edges_over_tolerance"] == ["E"] and run1["edges_over_after_class_stop"] == []
        dec = run1["class_stop"]["E"]
        assert dec["accept"] and dec["criterion"] == "rise"
        assert rep["class_stop_rule"]["decisions"][0]["edges"]["E"]["accept"]
        assert any(w.startswith("Class-stop rule, E edge") and "edge accepted: rise over the "
                   "baseline below 2 ft for" in w for w in rep["warnings"]), rep["warnings"]
        md = open(r.report_md).read()
        assert "## Class-stop rule (v3.2.0)" in md and "| Station (mi) |" in md
        assert "not requirements of REG_05.2026" in md
        assert os.path.exists(r.baseline["depth"])
        # the Word report: the accepted edge is a check for the engineer, not a failure
        from nd_breach_runner.core import report_docx as R
        from nd_breach_runner.tests.test_report_docx import _status
        res = R.build_submittal(r.folder, "", os.path.join(d, "on.docx"),
                                R.SubmittalOptions(include_run_report=False))
        t = R.inspect_docx(res.path)["text"]
        assert _status(t, "Downstream extent criterion (5.1.1(f)) met") == "Check", t[-3000:]
        assert "class-stop rule accepted the edge" in t
        # sunny day without a baseline: the depth (1.6 ft) is judged
        r = _run(os.path.join(d, "sun", "T1"), _settings(class_stop=True,
                                                          class_stop_station_mi=0.25))
        rep = json.load(open(r.report_json))
        assert len(rep["lisflood_runs"]) == 1
        assert rep["lisflood_runs"][0]["class_stop"]["E"]["criterion"] == "depth"
        # a deeper flood (2.3 ft) is extended as before
        r = _run(os.path.join(d, "deep", "T1"), _settings(class_stop=True,
                                                           class_stop_station_mi=0.25),
                 depth_m=0.7)
        rep = json.load(open(r.report_json))
        assert len(rep["lisflood_runs"]) == 2
        assert not rep["lisflood_runs"][0]["class_stop"]["E"]["accept"]
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
def class_stop_rule_with_receptor_layers_needs_nothing_beyond_the_edge():
    from nd_breach_runner.core import impacts
    d = _tmp()
    try:
        rec = impacts.Receptors()           # a structure just beyond the east edge, on the path
        rec.add_points([(2700.0, 0.0), (500.0, 0.0)], ids=["beyond", "inside"])
        r = _run(os.path.join(d, "T1"), _settings(class_stop=True, class_stop_station_mi=0.25,
                                                  receptors=rec))
        rep = json.load(open(r.report_json))
        assert len(rep["lisflood_runs"]) == 2
        dec = rep["lisflood_runs"][0]["class_stop"]["E"]
        assert not dec["accept"] and "(rule (b))" in dec["reason"], dec["reason"]
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ======================================================= item 6: per-run rasters (probability)
def _prob(folder, n=3, run_rasters=True, logs=None, **k):
    from nd_breach_runner.core import probabilistic as pm
    from nd_breach_runner.tests.synthetic import SyntheticTerrain, npy_writer
    st = _settings(max_iterations=1, ambient_cfs=100.0, **k)
    with fake_models(vary=lambda wd: 1.0 + 0.1 * (hash(wd) % 5)):
        return pm.run_probabilistic(_dam(), (0.0, 0.0), st, SyntheticTerrain(), npy_writer,
                                    folder, spec=pm.ProbSpec(), n=n, seed=7,
                                    log=(logs.append if logs is not None else None),
                                    run_rasters=run_rasters)


@test
def probabilistic_writes_each_runs_rasters_for_the_class_probability():
    from nd_breach_runner.core import probabilistic as pm, workflow
    d = _tmp()
    try:
        folder = os.path.join(d, "T1")
        logs = []
        r = _prob(folder, logs=logs)
        assert r.completed == 3, (r.rows, r.warnings)
        runs = os.path.join(folder, "Probabilistic", "Runs")
        assert r.runs_folder == runs and r.runs_index == os.path.join(runs, "index.json")
        assert any("about" in m and "per run" in m and "float32" in m for m in logs), logs
        idx = json.load(open(r.runs_index))
        assert idx["format"] == "ndbreach/probabilistic_runs"
        assert [x["folder"] for x in idx["runs"]] == ["run_001", "run_002", "run_003"]
        assert all(abs(x["weight"] - 1.0 / 3) < 1e-12 for x in idx["runs"])
        for i in (1, 2, 3):
            tag = "run_{:03d}".format(i)
            f = os.path.join(runs, tag)
            meta = json.load(open(os.path.join(f, "run.json")))
            work = json.load(open(os.path.join(r.work_folder, tag + ".json")))
            assert meta["key"] == work["key"] and meta["run"] == i and meta["status"] == "done"
            assert abs(meta["weight"] - 1.0 / 3) < 1e-12 and "Latin hypercube" in \
                meta["weight_note"]
            assert meta["sample"]["width_factor"] == r.samples[i - 1]["width_factor"]
            assert meta["units"] == {"depth": "ft", "velocity": "ft/s",
                                     "arrival": "hours after breach initiation"}
            # the rasters are the _work arrays in US units, named for ND Dam Hazard
            for kind, name in (("depth", "Depth (Max).tif"), ("velocity", "Velocity (Max).tif"),
                               ("arrival", "Arrival Time.tif")):
                p = os.path.join(f, name)
                assert os.path.exists(p), p
                w = np.load(pm._npy(r.work_folder, tag, kind))
                want = w / FT if kind != "arrival" else w
                assert np.allclose(_npy(p), want, equal_nan=True, rtol=1e-5), (tag, kind)
            assert np.nanmax(_npy(os.path.join(f, "Depth (Max).tif"))) > 1.0
            # the baseline is referenced, not copied
            assert not os.path.exists(os.path.join(f, "Baseline Depth (Max).tif"))
            ref = meta["baseline"]["depth"]
            assert ref == "../../../Baseline Depth (Max).tif", ref
            assert os.path.exists(os.path.normpath(os.path.join(f, ref)))
        # resumable: a lost raster is written again, no model runs
        os.remove(os.path.join(runs, "run_002", "Velocity (Max).tif"))
        logs = []
        r2 = _prob(folder, logs=logs)
        assert r2.resumed == 3 and not [m for m in logs if m.startswith("=== probabilistic")]
        assert os.path.exists(os.path.join(runs, "run_002", "Velocity (Max).tif"))
        assert any("(1 written now)" in m for m in logs), logs
        # a smaller study: the folder of run 3 is no longer counted
        r3 = _prob(folder, n=2)
        idx = json.load(open(r3.runs_index))
        assert [x["folder"] for x in idx["runs"]] == ["run_001", "run_002"]
        assert all(abs(x["weight"] - 0.5) < 1e-12 for x in idx["runs"])
        assert not os.path.exists(os.path.join(runs, "run_003", "run.json"))
        assert not os.path.exists(os.path.join(runs, "run_003", "Depth (Max).tif"))
        rep = open(r3.report_md, encoding="utf-8").read()
        assert "class probability" in rep and "index.json" in rep
        man = json.load(open(r3.manifest))
        assert man["outputs"]["runs_index"] == r3.runs_index
        assert workflow.FINAL_NAMES["arrival"] == "Arrival Time.tif"
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
def probabilistic_per_run_rasters_are_off_by_default():
    d = _tmp()
    try:
        folder = os.path.join(d, "T1")
        r = _prob(folder, n=2, run_rasters=False)
        assert r.completed == 2 and r.runs_index == "" and r.run_folders == []
        for i in (1, 2):
            f = os.path.join(r.runs_folder, "run_{:03d}".format(i))
            assert not os.path.exists(os.path.join(f, "run.json"))
            assert not os.path.exists(os.path.join(f, "Depth (Max).tif"))
        assert not os.path.exists(os.path.join(r.runs_folder, "index.json"))
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
def probabilistic_tool_has_the_run_rasters_option():
    from nd_breach_runner.tests import fake_qgis as fq
    fq.install()
    import nd_breach_runner.algorithms as algs
    importlib.reload(algs)
    import nd_breach_runner.algorithms_probabilistic as ap
    importlib.reload(ap)
    a = ap.ProbabilisticAlgorithm()
    a.initAlgorithm()
    p = a.parameterDefinition("PROB_RUN_RASTERS")
    assert p is not None and p.kwargs["defaultValue"] is False
    assert "class probability" in p.description
    assert "class probability tool" in a.shortHelpString()


# ================================================================== item 15: one EAP map
@test
def eap_preview_is_drawn_only_when_nd_dam_hazard_is_not_installed():
    from nd_breach_runner.core import products as pr
    assert pr.eap_preview_wanted("auto", None) is True
    assert pr.eap_preview_wanted("auto", "2.10.0") is False
    assert pr.eap_preview_wanted("mapbook", None) is False
    assert pr.eap_preview_wanted("svg", "2.10.0") is True
    assert pr.eap_preview_wanted("auto") is True            # no QGIS here: not installed
    assert pr.EAP_PREVIEW_NAME == "EAP Inundation Map (preview).svg"
    d = _tmp()
    try:
        folder = os.path.join(d, "T1")
        os.makedirs(folder)
        open(os.path.join(folder, pr.EAP_OLD_NAME), "w").write("<svg/>")      # a 3.1.x map
        r = _run(folder, _settings(max_iterations=1, eap_map="mapbook"))
        assert r.eap_map == "" and not os.path.exists(os.path.join(folder, pr.EAP_PREVIEW_NAME))
        assert not os.path.exists(os.path.join(folder, pr.EAP_OLD_NAME))     # stale: removed
        rep = json.load(open(r.report_json))
        assert "map book" in rep["eap_map_note"] and "eap_map" not in rep["outputs"]
        assert "EAP map: the EAP map is ND Dam Hazard's map book" in open(r.report_md).read()
        r = _run(folder, _settings(max_iterations=1, eap_map="svg"))
        assert os.path.basename(r.eap_map) == pr.EAP_PREVIEW_NAME and os.path.exists(r.eap_map)
        assert "EAP inundation map preview" in open(r.report_md).read()
        assert "map book" in open(r.report_html, encoding="utf-8").read()
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
def eap_map_parameter_on_the_run_tools():
    from nd_breach_runner.tests import fake_qgis as fq
    fq.install()
    import nd_breach_runner.algorithms as algs
    importlib.reload(algs)
    import nd_breach_runner.algorithms_tools as at
    importlib.reload(at)
    from nd_breach_runner.core import products as pr
    for cls in (algs.RunDamAlgorithm, algs.BatchAlgorithm, at.ProductsAlgorithm):
        a = cls()
        a.initAlgorithm()
        p = a.parameterDefinition("EAP_MAP")
        assert p is not None, cls
        opts = p.args[0]
        assert opts[1] == "Leave to ND Dam Hazard's map book (recommended)"
        assert opts[2] == "Also draw this tool's SVG preview"
        assert algs.EAP_MAP_CHOICES[p.kwargs["defaultValue"]][0] == pr.EAP_AUTO
    a = algs.RunDamAlgorithm()
    a.initAlgorithm()
    ctx = fq.QgsProcessingContext()
    st = a.run_settings({"EAP_MAP": 1, "STOP_RULE": True, "STOP_CONFIRM": 2}, ctx,
                        need_models=False)
    assert st.eap_map == "mapbook" and st.class_stop and st.class_stop_confirm == 2
    assert st.class_stop_threshold_ft == 2.0 and st.class_stop_station_mi == 0.5
    st = a.run_settings({}, ctx, need_models=False)
    assert st.eap_map == "auto" and st.class_stop is False and st.class_stop_search_ft == 150.0


def _report_folder():
    from nd_breach_runner.tests.test_report_docx import _copy_folder
    return _copy_folder(True)


@test
def word_report_lists_nd_dam_hazards_map_book_as_the_eap_map():
    from nd_breach_runner.core import report_docx as R
    from nd_breach_runner.tests.test_report_docx import _status
    folder = _report_folder()
    try:
        assert os.path.exists(os.path.join(folder, "EAP Inundation Map (preview).svg"))
        opt = R.SubmittalOptions(include_run_report=False)
        # the preview only
        res = R.build_submittal(folder, "", os.path.join(folder, "a.docx"), opt)
        t = R.inspect_docx(res.path)["text"]
        assert _status(t, "EAP inundation map produced") == "Yes"
        assert "EAP Inundation Map (preview).svg (this tool's preview" in t
        assert "EAP inundation map" in {f["name"] for f in res.figures}
        assert not [m for m in res.missing if m.startswith("EAP")]
        # a 3.1.x folder: the old name is still found
        os.rename(os.path.join(folder, "EAP Inundation Map (preview).svg"),
                  os.path.join(folder, "EAP Inundation Map.svg"))
        res = R.build_submittal(folder, "", os.path.join(folder, "b.docx"), opt)
        t = R.inspect_docx(res.path)["text"]
        assert "EAP inundation map" in {f["name"] for f in res.figures}
        assert "EAP Inundation Map.svg (this tool's preview" in t
        # ND Dam Hazard's map book: listed as the EAP map instead of the preview
        eap = os.path.join(folder, "outputs", "EAP")
        os.makedirs(eap)
        open(os.path.join(eap, "EAP_Map_Book.pdf"), "wb").write(b"%PDF-1.4\n%%EOF\n")
        assert R.find_mapbook(folder) == os.path.join(eap, "EAP_Map_Book.pdf")
        res = R.build_submittal(folder, "", os.path.join(folder, "c.docx"), opt)
        t = R.inspect_docx(res.path)["text"]
        assert "ND Dam Hazard map book: outputs/EAP/EAP_Map_Book.pdf" in t
        assert "map book on aerial imagery: outputs/EAP/EAP_Map_Book.pdf" in t
        assert "EAP inundation map" not in {f["name"] for f in res.figures}
        # a time-stamped book (the usual name was locked) is found too; the newest wins
        alt = os.path.join(eap, "EAP_Map_Book_20260930_101500.pdf")
        open(alt, "wb").write(b"%PDF-1.4\n%%EOF\n")
        os.utime(alt, (2e9, 2e9))
        assert R.find_mapbook(folder) == alt
        # nothing at all: reported missing
        shutil.rmtree(os.path.join(folder, "outputs"))
        os.remove(os.path.join(folder, "EAP Inundation Map.svg"))
        res = R.build_submittal(folder, "", os.path.join(folder, "d.docx"), opt)
        t = R.inspect_docx(res.path)["text"]
        assert _status(t, "EAP inundation map produced") == "No"
        assert any(m.startswith("EAP map (ND Dam Hazard") for m in res.missing), res.missing
    finally:
        shutil.rmtree(os.path.dirname(os.path.dirname(folder)), ignore_errors=True)


# ======================================================================== item 2: dam.json
def _record(folder, tool="ndhazard", version="2.10.0", **fields):
    from nd_breach_runner.damsuite import dam_record as dr
    rec = dr.load(folder, create=True)
    rec.set_many(fields, tool, version)
    rec.save()
    return rec


@test
def damjson_fills_empty_values_and_says_where_they_came_from():
    from nd_breach_runner.core import damjson
    d = _tmp()
    try:
        _record(d, height_ft=25.0, storage_acft=500.0, surface_area_ac=60.0,
                top_of_dam_ft=1850.0, top_of_dam_datum="NAVD88 ft", name="Test Dam",
                nid_id="ND00123", lon=-100.5, lat=47.2)
        rec, problem = damjson.load(d)
        assert problem == "" and rec is not None
        vals, notes, used = damjson.fill({"HEIGHT_FT": None, "STORAGE_ACFT": 400.0,
                                          "AREA_AC": None, "TOP_FT": None, "DAM_NAME": "",
                                          "NID_ID": None}, rec)
        assert vals["HEIGHT_FT"] == 25.0 and vals["STORAGE_ACFT"] == 400.0   # given wins
        assert vals["TOP_FT"] == 1850.0 and vals["DAM_NAME"] == "Test Dam"
        assert vals["NID_ID"] == "ND00123" and "STORAGE_ACFT" not in used
        h = [n for n in notes if n.startswith("Dam height (ft) from dam.json")][0]
        assert "(set by ndhazard 2.10.0 on " in h and h.endswith(": 25.0"), h
        assert any("(datum NAVD88 ft)" in n for n in notes)
        pt, note = damjson.point_from_record(rec)
        assert pt == ("lonlat", -100.5, 47.2, "EPSG:4326") and "lon -100.5" in note
        # a local-datum top of dam is not used as an elevation
        rec.set("top_of_dam_datum", "local", "ndhazard", "2.10.0", overwrite=True)
        vals, notes, used = damjson.fill({"TOP_FT": None}, rec)
        assert vals["TOP_FT"] is None and any("local datum" in n for n in notes)
        assert damjson.fill({"HEIGHT_FT": None}, None) == ({"HEIGHT_FT": None}, [], {})
        # a broken file is reported, never replaced
        bad = os.path.join(d, "bad")
        os.makedirs(bad)
        open(os.path.join(bad, "dam.json"), "w").write("{not json")
        rec, problem = damjson.load(bad)
        assert rec is None and "dam.json not read" in problem
        info = damjson.record_run(bad, {"height_ft": 3.0}, {"x": 1})
        assert info["error"] and open(os.path.join(bad, "dam.json")).read() == "{not json"
        assert damjson.folder_name("ND 001/a") == "ND 001_a"
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
def damjson_never_overwrites_another_tools_value_and_reports_both():
    from nd_breach_runner.core import damjson
    from nd_breach_runner.damsuite import dam_record as dr
    d = _tmp()
    try:
        _record(d, height_ft=26.0, name="Test Dam")
        info = damjson.record_run(d, {"dam_id": "T1", "height_ft": 25.0, "name": "Test Dam",
                                      "storage_acft": 500.0},
                                  {"final_rasters": ["Depth (Max).tif"]}, notes=["x"])
        assert info["written"] == ["dam_id", "storage_acft"] and info["kept_from_record"] == \
            ["name"], info
        c = info["conflicts"][0]
        assert c["field"] == "height_ft" and c["dam_json"] == 26.0 and c["this_run"] == 25.0
        w = info["warnings"][0]
        assert "dam.json keeps height_ft = 26" in w and "this run used 25" in w and \
            "set by ndhazard 2.10.0" in w, w
        rec = dr.load(d)
        assert rec.get("height_ft") == 26.0 and rec.sources["storage_acft"]["tool"] == "ndbreach"
        assert rec.sources["storage_acft"]["version"] == damjson.plugin_version() == "3.12.0"
        st = rec.latest("modelled")
        assert st["tool"] == "ndbreach" and st["result"]["final_rasters"] == ["Depth (Max).tif"]
        lines = "\n".join(damjson.report_lines(info))
        assert "CONFLICT height_ft: dam.json 26" in lines and "## dam.json" in lines
        # the tool may update its own value
        info = damjson.record_run(d, {"storage_acft": 520.0}, {})
        assert info["updated"] == ["storage_acft"] and not info["conflicts"]
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
def a_run_records_what_it_used_in_dam_json():
    from nd_breach_runner.damsuite import dam_record as dr
    d = _tmp()
    try:
        folder = os.path.join(d, "T1")
        # default: no dam.json is written (sensitivity cases, probabilistic runs, tests)
        _run(folder, _settings(max_iterations=1))
        assert not os.path.exists(os.path.join(folder, "dam.json"))
        _record(folder, height_ft=26.0, name="Test Dam", tool="ndscreen", version="0.9.0")
        st = _settings(max_iterations=1, dam_record={
            "read": ["Dam name from dam.json (set by ndscreen 0.9.0 on x): Test Dam"],
            "top_source": "parameter",
            "point": {"lon": -100.5, "lat": 47.2, "x": 0.0, "y": 0.0, "crs": "EPSG:26914"}})
        r = _run(folder, st, dam=_dam(top_of_dam_ft=None, name="Test Dam"))
        rec = dr.load(folder)
        assert rec.get("height_ft") == 26.0                      # another tool's: kept
        assert rec.get("storage_acft") == 500.0 and rec.sources["storage_acft"]["tool"] == \
            "ndbreach"
        assert rec.get("top_of_dam_datum") == "DEM" and "DEM" in \
            rec.sources["top_of_dam_ft"]["note"]
        assert rec.get("lon") == -100.5 and rec.get("crs") == "EPSG:26914"
        m = rec.latest("modelled")
        res = m["result"]
        assert m["tool"] == "ndbreach" and m["version"] == "3.12.0"
        assert "Depth (Max).tif" in res["final_rasters"] and res["breach_occurs"]
        assert res["manifest"] == "_breach_model/run_manifest.json"
        assert os.path.exists(os.path.join(folder, res["manifest"]))
        assert res["extent_ft"] > 1000 and len(res["domain_mi"]) == 2
        assert "class_preview" not in res
        rep = json.load(open(r.report_json))
        assert any("dam.json keeps height_ft = 26" in w for w in rep["warnings"])
        md = open(r.report_md).read()
        assert "## dam.json (shared dam record)" in md and "CONFLICT height_ft" in md
        assert "- Read: Dam name from dam.json" in md
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _algs():
    from nd_breach_runner.tests import fake_qgis as fq
    fq.install()
    import nd_breach_runner.algorithms as algs
    importlib.reload(algs)
    return algs, fq


@test
def rundam_takes_empty_values_from_dam_json():
    algs, fq = _algs()
    d = _tmp()
    try:
        folder = os.path.join(d, "ND00123")
        _record(folder, height_ft=25.0, storage_acft=500.0, surface_area_ac=60.0,
                name="Test Dam", x=1000.0, y=2000.0, crs="EPSG:26914")
        a = algs.RunDamAlgorithm()
        a.initAlgorithm()
        for k in ("HEIGHT_FT", "STORAGE_ACFT", "DAM_POINT"):
            assert a.parameterDefinition(k).kwargs.get("optional"), k
        ctx = fq.QgsProcessingContext()
        dam = a.dam_from_params({"DAM_ID": "ND00123", "DAMS_FOLDER": d, "STORAGE_ACFT": 450.0},
                                ctx)
        assert dam.dam_height_ft == 25.0 and dam.storage_top_acft == 450.0
        assert dam.surface_area_top_ac == 60.0 and dam.name == "Test Dam"
        assert dam.top_of_dam_ft is None                        # not in dam.json: from the DEM
        assert any(n.startswith("Dam height (ft) from dam.json (set by ndhazard 2.10.0 on ")
                   for n in a._rec_info["read"]), a._rec_info
        assert a._rec_info["top_source"] == "parameter"
        pt = a.dam_point({}, ctx, fq.QgsCoordinateReferenceSystem("EPSG:26914"))
        assert (pt.x(), pt.y()) == (1000.0, 2000.0)
        pt = a.dam_point({"DAM_POINT": fq.QgsPointXY(5, 6)}, ctx, None)
        assert (pt.x(), pt.y()) == (5.0, 6.0)
        info = a.point_info(fq.QgsPointXY(5, 6), fq.QgsCoordinateReferenceSystem("EPSG:26914"),
                            fq.QgsCoordinateReferenceSystem("EPSG:26914"), (5.0, 6.0), ctx)
        assert info["crs"] == "EPSG:26914" and info["x"] == 5.0
        info = a.point_info(fq.QgsPointXY(5, 6), None,
                            fq.QgsCoordinateReferenceSystem("EPSG:2265"), (5.0, 6.0), ctx)
        assert "x" not in info and "crs" not in info           # the record keeps another CRS
        # nothing given and nothing recorded: a clear error
        try:
            a.dam_from_params({"DAM_ID": "OTHER", "DAMS_FOLDER": d}, ctx)
            raise AssertionError("expected an error")
        except fq.QgsProcessingException as e:
            assert "dam.json" in str(e) and "Dam height" in str(e)
        a2 = algs.RunDamAlgorithm()
        a2.initAlgorithm()
        a2.dam_from_params({"DAM_ID": "OTHER", "DAMS_FOLDER": d, "HEIGHT_FT": 5,
                            "STORAGE_ACFT": 5}, ctx)
        try:
            a2.dam_point({}, ctx, None)
            raise AssertionError("expected an error")
        except fq.QgsProcessingException as e:
            assert "dam point" in str(e)
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
def batch_takes_empty_values_from_dam_json():
    algs, fq = _algs()
    d = _tmp()
    try:
        _record(os.path.join(d, "ND00002"), height_ft=25.0)

        class _R:
            def crs(self):
                return fq.QgsCoordinateReferenceSystem("EPSG:26914")

            def source(self):
                return "synthetic"
        feats = [fq.FakeFeature(fq.FakeGeometry("point", [[(0.0, 0.0)]]), NIDID=n, S=500.0)
                 for n in ("ND00001", "ND00002")]
        a = algs.BatchAlgorithm()
        a.initAlgorithm()
        fb = fq.QgsProcessingFeedback()
        r = a.processAlgorithm({"DAMS": fq.FakeSource(feats), "F_ID": "NIDID",
                                "F_STORAGE": "S", "DEM": _R(), "DAMS_FOLDER": d,
                                "LISFLOOD_EXE": sys.executable}, fq.QgsProcessingContext(), fb)
        import csv
        rows = {row["Dam"]: row for row in csv.DictReader(open(r["SUMMARY"]))}
        # no height field: not an error before the run any more; dam.json gives ND00002's
        assert any("each dam's dam.json must give it" in e for e in fb.errors), fb.errors
        assert rows["ND00001"]["Status"] == "failed" and "height" in rows["ND00001"]["Message"]
        assert "does not exist" in rows["ND00001"]["Message"]
        assert any(m.startswith("ND00002: Dam height (ft) from dam.json") for m in fb.info)
        # ND00002 got past the dam values (it then needs GDAL, not installed here)
        assert "height" not in rows["ND00002"]["Message"]
    finally:
        shutil.rmtree(d, ignore_errors=True)


class _FakeRaster:
    def crs(self):
        from nd_breach_runner.tests import fake_qgis
        return fake_qgis.QgsCoordinateReferenceSystem("EPSG:26914")

    def source(self):
        return "synthetic"


@test
def rundam_and_batch_tools_read_and_write_dam_json_end_to_end():
    """The Processing tools with the GIS side and LISFLOOD-FP stubbed (as the model tests
    stub the GIS side)."""
    from nd_breach_runner.tests.run_tests import _patched_algs
    from nd_breach_runner.damsuite import dam_record as dr
    import csv
    algs, fq = _patched_algs()
    d = _tmp()
    try:
        folder = os.path.join(d, "ND00123")
        _record(folder, height_ft=25.0, storage_acft=500.0, surface_area_ac=60.0,
                top_of_dam_ft=1850.0, top_of_dam_datum="NAVD88 ft", name="Test Dam",
                x=0.0, y=0.0, crs="EPSG:26914")
        a = algs.RunDamAlgorithm()
        a.initAlgorithm()
        fb, ctx = fq.QgsProcessingFeedback(), fq.QgsProcessingContext()
        with fake_models():
            r = a.processAlgorithm({"DAM_ID": "ND00123", "DEM": _FakeRaster(), "CELL": 50.0,
                                    "LENGTH_MI": 1.0, "HALF_WIDTH_MI": 0.5, "MAX_RUNS": 1,
                                    "DURATION_HR": 4.0, "LISFLOOD_EXE": sys.executable,
                                    "DAMS_FOLDER": d}, ctx, fb)
        assert r["FOLDER"] == folder and os.path.exists(r["DEPTH"])
        for want in ("Dam height (ft) from dam.json (set by ndhazard 2.10.0 on ",
                     "Top of dam elevation (ft) from dam.json", "Dam point from dam.json"):
            assert any(m.startswith(want) for m in fb.info), (want, fb.info)
        rec = dr.load(folder)
        m = rec.latest("modelled")
        assert m["tool"] == "ndbreach" and "Depth (Max).tif" in m["result"]["final_rasters"]
        assert rec.sources["height_ft"]["tool"] == "ndhazard"          # first source kept
        assert rec.get("top_of_dam_datum") == "NAVD88 ft"               # from the record
        assert rec.get("lat") is None or isinstance(rec.get("lat"), float)
        assert not rec.conflicts, rec.conflicts
        md = open(r["REPORT"]).read()
        assert "- Read: Dam height (ft) from dam.json" in md
        # a different height given by the engineer: the run uses it, dam.json keeps ndhazard's
        with fake_models():
            r = a.processAlgorithm({"DAM_ID": "ND00123", "HEIGHT_FT": 27.0, "DEM": _FakeRaster(),
                                    "CELL": 50.0, "LENGTH_MI": 1.0, "HALF_WIDTH_MI": 0.5,
                                    "MAX_RUNS": 1, "DURATION_HR": 4.0,
                                    "LISFLOOD_EXE": sys.executable, "DAMS_FOLDER": d}, ctx, fb)
        rec = dr.load(folder)
        assert rec.get("height_ft") == 25.0 and rec.conflicts[-1]["new"] == 27.0
        assert any("dam.json keeps height_ft = 25" in e for e in fb.errors), fb.errors
        # the batch: a dam without a height field or value takes it from dam.json
        feats = [fq.FakeFeature(fq.FakeGeometry("point", [[(0.0, 0.0)]]), NIDID="ND00123",
                                S=500.0)]
        b = algs.BatchAlgorithm()
        b.initAlgorithm()
        fb2 = fq.QgsProcessingFeedback()
        with fake_models():
            r = b.processAlgorithm({"DAMS": fq.FakeSource(feats), "F_ID": "NIDID",
                                    "F_STORAGE": "S", "DEM": _FakeRaster(), "CELL": 50.0,
                                    "LENGTH_MI": 1.0, "HALF_WIDTH_MI": 0.5, "MAX_RUNS": 1,
                                    "DURATION_HR": 4.0, "LISFLOOD_EXE": sys.executable,
                                    "DAMS_FOLDER": d, "SKIP_DONE": False},
                                   fq.QgsProcessingContext(), fb2)
        rows = list(csv.DictReader(open(r["SUMMARY"])))
        assert rows[0]["Status"] == "done", rows
        assert any(m.startswith("ND00123: Dam height (ft) from dam.json") for m in fb2.info)
        assert [x["step"] for x in dr.load(folder).status] == ["modelled"] * 3
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
def nd52_classification_marks_its_class_a_preview_in_dam_json():
    from nd_breach_runner.core import damjson
    from nd_breach_runner.damsuite import dam_record as dr
    d = _tmp()
    try:
        state, text = damjson.classified_preview(d)
        assert state == "skipped" and not os.path.exists(os.path.join(d, "dam.json"))
        _record(d, height_ft=25.0)
        state, text = damjson.classified_preview(d)
        assert state == "added" and "PREVIEW" in text
        c = dr.load(d).latest("classified")
        assert c["result"] == {"preview": True, "tool": "nd_hazard.py"} and c["tool"] == "ndbreach"
        src = open(os.path.join(PLUGIN, "algorithms_ndhazard.py"), encoding="utf-8").read()
        assert "damjson.classified_preview(folder)" in src
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
def wizard_fills_empty_values_from_dam_json():
    from nd_breach_runner import wizard_logic as wl
    d = _tmp()
    try:
        _record(os.path.join(d, "ND00123"), height_ft=25.0, storage_acft=500.0,
                surface_area_ac=60.0, x=10.0, y=20.0, crs="EPSG:26914")
        a = wl.default_answers()
        a["dam"] = {"DAM_ID": "ND00123", "STORAGE_ACFT": 450.0}
        e, w = wl.validate_page("dam", a)
        assert any("Dam height" in x for x in e) and any("location" in x for x in e)
        a["dams_folder"] = d
        e, w = wl.validate_page("dam", a)
        assert not e, e
        assert any("taken from dam.json (25)" in x for x in w) and \
            "Location: taken from dam.json." in w
        lines = wl.apply_dam_record(a)
        assert a["dam"]["HEIGHT_FT"] == 25.0 and a["dam"]["STORAGE_ACFT"] == 450.0
        assert a["dam"]["AREA_AC"] == 60.0 and a["point"] == (10.0, 20.0, "EPSG:26914")
        assert any(x.startswith("Dam height (ft) from dam.json") for x in lines), lines
        assert "From the dam's dam.json" in wl.review_text(a)
        p = wl.build_rundam_params(a)
        assert p["HEIGHT_FT"] == 25.0 and p["DAM_POINT"].endswith("[EPSG:26914]")
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ================================================================ item 5: version checks
@test
def version_checks_at_start_up_show_in_the_message_bar_and_log():
    from nd_breach_runner.tests import fake_qgis as fq
    fq.install()
    import qgis.core as qc
    logged = []

    class QgsMessageLog:
        @staticmethod
        def logMessage(text, tag, level):
            logged.append((text, tag, level))
    qc.QgsMessageLog = QgsMessageLog
    try:
        import nd_breach_runner.algorithms as algs
        importlib.reload(algs)
        import nd_breach_runner.plugin as plugin
        importlib.reload(plugin)
        bar = []

        class Bar:
            def pushMessage(self, title, text, level, duration):
                bar.append((title, text, level, duration))

        class Iface:
            def mainWindow(self):
                return None

            def messageBar(self):
                return Bar()

            def addPluginToMenu(self, *a):
                pass

            def removePluginMenu(self, *a):
                pass
        p = plugin.NDBreachRunnerPlugin(Iface())
        msgs = p.check_versions({"ndhazard": "2.9.0", "ndbreach": "3.4.0"})
        assert [k for k, _t in msgs if k != "info"] == ["error"]
        assert "2.9.1 or newer" in msgs[0][1] and len(bar) == 1 and bar[0][3] == 0
        assert any("ND Dam Breach Runner 3.4.0 (this plugin" in t for t, _g, _l in logged)
        bar.clear()
        msgs = p.check_versions({"ndhazard": "2.9.5"})
        assert [k for k, _t in msgs if k != "info"] == ["warning"] and bar[0][3] == 15
        bar.clear()
        # not installed / current: nothing in the message bar (3.9.0: "current" is the release
        # set's - ND Dam Hazard 2.11.0 is below the 2.17.0 the suite package of Get data needs)
        from nd_breach_runner.damsuite import versions as dv
        for v in ({}, {"ndhazard": dv.RELEASE["ndhazard"], "ndscreen": dv.RELEASE["ndscreen"]}):
            msgs = p.check_versions(v)
            assert all(k == "info" for k, _t in msgs) and not bar, (v, msgs)
        # initGui schedules the check (no QTimer in the stub: it runs at once) and loads
        # without ND Dam Hazard
        logged.clear()
        p.initGui()
        assert any("ND Dam Hazard Classification Helper not installed" in t
                   for t, _g, _l in logged), logged
        assert p.actions and p.provider is not None
        p.unload()
        assert plugin.version_messages({"ndhazard": "1"})[0][0] == "error"
    finally:
        del qc.QgsMessageLog


@test
def handoff_refuses_nd_dam_hazard_below_the_hard_minimum():
    from nd_breach_runner.core import handoff
    try:
        handoff.check_version("ndhazard:classify", {"ndhazard": "2.9.0"})
        raise AssertionError("expected HandoffError")
    except handoff.HandoffError as e:
        assert "2.9.1 or newer" in str(e) and "2.9.0" in str(e)
    handoff.check_version("ndhazard:classify", {"ndhazard": "2.9.1"})       # soft: passes
    handoff.check_version("ndhazard:classify", {})                           # not installed
    handoff.check_version("other:classify", {"ndhazard": "1.0"})             # not ndhazard
    handoff.check_version("", None)                                          # no QGIS here

    class Alg:
        def __init__(self, i="ndhazard:classify"):
            self.i = i

        def id(self):
            return self.i

        def parameterDefinitions(self):
            return []

    class Reg:
        def algorithmById(self, i):
            return Alg(i) if i in ("ndhazard:classify", "ndhazard:batch") else None

        def algorithms(self):
            return [Alg()]
    for fn, args in ((handoff.make_classifier, ()), (handoff.classify_folder, ("d",)),
                     (handoff.classify_batch, ("d",))):
        try:
            fn(*args, registry=Reg(), versions={"ndhazard": "2.8.0"})
            raise AssertionError("expected HandoffError from {}".format(fn.__name__))
        except handoff.HandoffError as e:
            assert "ND Dam Hazard" in str(e)
    assert callable(handoff.make_classifier(registry=Reg(), versions={"ndhazard": "2.10.0"}))


# ============================================================================ the release
@test
def release_is_3_2_0_and_loads_without_nd_dam_hazard():
    import configparser
    from nd_breach_runner.core import workflow
    from nd_breach_runner import damsuite
    from nd_breach_runner.damsuite import versions
    cp = configparser.ConfigParser(interpolation=None)
    cp.read(os.path.join(PLUGIN, "metadata.txt"), encoding="utf-8")
    assert cp.get("general", "version") == workflow.VERSION == versions.RELEASE["ndbreach"] \
        == "3.12.0"
    assert damsuite.selfcheck() == []
    assert versions.installed("ndhazard") is None           # outside QGIS: not installed
    readme = open(os.path.join(PLUGIN, "README.md"), encoding="utf-8").read()
    i = readme.index("## New in 3.2.0")
    new = readme[i:readme.index("## New in 3.1.1")]
    for needle in ("None of this has been run in a real QGIS", "LISFLOOD-FP", "dam.json",
                   "Probabilistic/Runs/run_NNN", "class probability", "EAP Inundation Map "
                   "(preview).svg", "termination point", "version"):
        assert needle in new, needle
    for name in ("core/stoprule.py", "core/damjson.py", "tests/test_v320.py", "plugin.py",
                 "core/probabilistic.py", "core/workflow.py", "core/report_docx.py",
                 "core/handoff.py", "core/products.py", "wizard_logic.py", "algorithms.py",
                 "algorithms_probabilistic.py", "algorithms_tools.py", "algorithms_ndhazard.py"):
        path = os.path.join(PLUGIN, name)
        py_compile.compile(path, cfile=os.path.join(tempfile.gettempdir(),
                                                    os.path.basename(name) + "c"), doraise=True)
        tree = ast.parse(open(path, encoding="utf-8").read(), feature_version=(3, 9))
        for node in ast.walk(tree):
            assert not isinstance(node, getattr(ast, "Match", ())), name
    # the core modules need no QGIS / GDAL
    for name in ("core/stoprule.py", "core/damjson.py"):
        tree = ast.parse(open(os.path.join(PLUGIN, name), encoding="utf-8").read())
        for node in ast.walk(tree):
            mods = []
            if isinstance(node, ast.Import):
                mods = [n.name for n in node.names]
            elif isinstance(node, ast.ImportFrom) and not node.level:
                mods = [node.module or ""]
            assert not any(m.split(".")[0] in ("qgis", "osgeo", "scipy", "PyQt5") for m in mods)


# ======================================= hand-off: an NID parameter gets an NID, not a dam ID
@test
def handoff_gives_nid_parameters_an_nid_never_another_dam_id():
    from nd_breach_runner.core import handoff
    from nd_breach_runner.tests.run_tests import (_NDHAZARD_CLASSIFY, _Alg, _P,
                                                  _runner_dam_folder)
    alg = _Alg("ndhazard:classify", [_P(n, t, desc, optional=o)
                                     for n, t, desc, o in _NDHAZARD_CLASSIFY])
    d = _runner_dam_folder()
    try:
        par = os.path.dirname(d)
        # an NID-like dam ID and no dam.json: the dam ID is the NID
        p = handoff.build_parameters(alg, "ND00123", d, par, handoff.rasters_in(d))
        assert p == {"DAM_FOLDER": d, "SUBJECT_NID": "ND00123"}, p
        assert handoff.build_parameters(alg, "nd00123", d, par, {})["SUBJECT_NID"] == "nd00123"
        # a DWR number is not an NID: the optional SUBJECT_NID stays empty and says why
        notes = []
        p = handoff.build_parameters(alg, "DWR-5289", d, par, handoff.rasters_in(d),
                                     notes=notes)
        assert p == {"DAM_FOLDER": d}, p
        assert any(n.startswith("SUBJECT_NID: left empty") for n in notes), notes
        for bad in ("ND0012", "ND001234", "N100123", "12345ND", ""):
            assert handoff.nid_for("", bad) == "", bad
        # dam.json's nid_id wins over the dam ID
        _record(d, nid_id="ND05289")
        p = handoff.build_parameters(alg, "DWR-5289", d, par, {})
        assert p["SUBJECT_NID"] == "ND05289", p
        assert handoff.build_parameters(alg, "ND00123", d, par, {})["SUBJECT_NID"] == "ND05289"
        # a broken dam.json never raises: the dam ID rule applies
        open(os.path.join(d, "dam.json"), "w").write("{broken")
        assert handoff.nid_for(d, "DWR-5289") == "" and handoff.nid_for(d, "ND00001") == \
            "ND00001"
        os.remove(os.path.join(d, "dam.json"))
        # a required NID parameter without a value is reported missing
        req = _Alg("x:classify", [_P("DAM_FOLDER", "file", "Dam project folder"),
                                  _P("NIDID", "string", "NID of the dam")])
        try:
            handoff.build_parameters(req, "DWR-5289", d, par, {})
            raise AssertionError("expected HandoffError")
        except handoff.HandoffError as e:
            assert "NIDID" in str(e) and "not one" in str(e), str(e)
        assert handoff.build_parameters(req, "ND00123", d, par, {})["NIDID"] == "ND00123"
        # a dam ID parameter (not an NID) still gets the dam ID
        did = _Alg("x:classify", [_P("DAM_FOLDER", "file", "Dam project folder"),
                                  _P("DAM_ID", "string", "Dam ID"),
                                  _P("NAME", "string", "Dam id of the dam", optional=True)])
        p = handoff.build_parameters(did, "DWR-5289", d, par, {})
        assert p["DAM_ID"] == "DWR-5289" and p["NAME"] == "DWR-5289", p
        # a batch (no dam folder, no dam ID): the optional NID stays empty
        p = handoff.build_parameters(alg, "", "", par, {}, out_dir=os.path.join(par, "_c"))
        assert "SUBJECT_NID" not in p
    finally:
        shutil.rmtree(d, ignore_errors=True)
