"""Tests of the probabilistic (Monte Carlo) breach runs: core/probabilistic.py and
algorithms_probabilistic.py. Fast tests need numpy only; model tests run LISFLOOD-FP on
the synthetic valley (NDBREACH_LISFLOOD)."""
import ast
import csv
import importlib
import json
import os
import py_compile
import shutil
import statistics
import tempfile
import warnings

import numpy as np

from nd_breach_runner.tests.registry import test, model_test

PLUGIN = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _pm():
    from nd_breach_runner.core import probabilistic as pm
    return pm


def _close(a, b, tol=1e-9):
    assert abs(a - b) <= tol * max(1.0, abs(a), abs(b)), "{} != {}".format(a, b)


# ---------------------------------------------------------------------------------------
# sampling and distributions
# ---------------------------------------------------------------------------------------
@test
def probabilistic_lhs_samples_each_stratum_once_and_is_reproducible():
    pm = _pm()
    for n in (1, 2, 7, 25):
        u = pm.latin_hypercube(n, 4, np.random.default_rng(5))
        assert u.shape == (n, 4) and np.all(u >= 0) and np.all(u < 1)
        for j in range(4):
            assert sorted(np.floor(u[:, j] * n).astype(int)) == list(range(n)), (n, j, u[:, j])
    spec = pm.ProbSpec(manning=pm.Dist("lognormal", 1.0, 0.2))
    a = pm.draw_samples(spec, 10, 11, "piping")
    b = pm.draw_samples(spec, 10, 11, "piping")
    c = pm.draw_samples(spec, 10, 12, "piping")
    assert a == b and a != c
    assert [s["run"] for s in a] == list(range(1, 11))
    # the inverse CDF keeps the strata: each factor has one draw per probability stratum
    for key, d in (("width_factor", spec.width), ("time_factor", spec.time),
                   ("n_factor", spec.manning)):
        v = np.array([s[key] for s in a])
        k = np.floor(np.clip(d.cdf(v), 0, 1 - 1e-12) * 10 + 1e-9).astype(int)
        assert sorted(k) == list(range(10)), (key, k)
    # the recorded u are the draws
    for s in a:
        _close(float(spec.width.ppf(s["u"][0])), s["width_factor"])
        assert s["method"] == "piping"


@test
def probabilistic_method_draw_follows_the_weights():
    pm = _pm()
    spec = pm.ProbSpec(methods=pm.parse_methods("piping 2, overtopping 1, MLM 1"))
    s = pm.draw_samples(spec.validate(), 8, 3, "vtg1990")
    got = [x["method"] for x in s]
    assert got.count("piping") == 4 and got.count("overtopping") == 2 and \
        got.count("mlm1984") == 2, got
    assert pm.draw_samples(pm.ProbSpec(), 3, 1, "tr60")[0]["method"] == "tr60"
    assert list(pm.pick([0.0, 0.49, 0.5, 0.74, 0.75, 0.999], [0.5, 0.25, 0.25])) == \
        [0, 0, 1, 1, 2, 2]


@test
def probabilistic_distributions_respect_their_parameters():
    pm = _pm()
    rng = np.random.default_rng(1)
    u = pm.latin_hypercube(20000, 1, rng)[:, 0]
    # triangular: min / max are the ends, the mode is where the CDF reaches (c-a)/(b-a)
    t = pm.Dist("tri", 0.5, 1.0, 2.0)
    assert t.ppf(0.0) == 0.5 and t.ppf(1.0) == 2.0
    _close(float(t.ppf(1.0 / 3.0)), 1.0, 1e-12)
    x = t.ppf(u)
    assert x.min() >= 0.5 and x.max() <= 2.0
    _close(float(x.mean()), 3.5 / 3.0, 2e-3)
    h, edges = np.histogram(x, bins=np.arange(0.5, 2.0001, 0.1))
    assert edges[np.argmax(h)] <= 1.0 <= edges[np.argmax(h) + 1] + 1e-9, (h, edges)
    assert np.all(np.diff(t.ppf(np.linspace(0, 1, 101))) >= 0)
    _close(float(t.cdf(t.ppf(0.8))), 0.8, 1e-9)
    # a mode at an end is fine
    e = pm.Dist("tri", 1.0, 1.0, 1.5)
    assert e.ppf(0.0) == 1.0 and e.ppf(1.0) == 1.5 and np.all(e.ppf(u) >= 1.0)
    assert pm.Dist("tri", 1.2, 1.2, 1.2).ppf(0.3) == 1.2 and not pm.Dist(
        "tri", 1.2, 1.2, 1.2).varies()
    # uniform
    un = pm.Dist("uniform", 0.5, 2.0)
    _close(float(un.ppf(0.25)), 0.875)
    _close(float(un.ppf(u).mean()), 1.25, 1e-3)
    # lognormal: median and spread
    ln = pm.Dist("lognormal", 1.2, 0.3)
    _close(float(ln.ppf(0.5)), 1.2, 1e-12)
    _close(float(np.median(ln.ppf(u))), 1.2, 2e-3)
    _close(float(ln.ppf(statistics.NormalDist().cdf(1.0))), 1.2 * np.exp(0.3), 1e-9)
    _close(float(np.std(np.log(ln.ppf(u)))), 0.3, 1e-2)
    assert np.all(np.isfinite(ln.ppf([0.0, 1.0])))          # tails are truncated, not inf
    # fixed
    assert pm.Dist("fixed", 1.3).ppf(0.7) == 1.3 and pm.Dist("fixed", 1.0).varies() is False
    for bad in (("tri", 1.5, 1.0, 0.5), ("tri", 1.0, 2.0), ("uniform", -1.0, 2.0),
                ("lognormal", 1.0, -0.1), ("lognormal", 0.0, 0.1), ("beta", 1.0, 2.0),
                ("fixed", 0.0), ("tri", 0.5, 1.0, float("nan"))):
        try:
            pm.Dist(*bad)
            raise AssertionError("accepted {}".format(bad))
        except ValueError:
            pass


@test
def probabilistic_spec_parsing_and_round_trip():
    pm = _pm()
    d = pm.ProbSpec()
    assert d.width == pm.Dist("tri", 0.5, 1.0, 1.5) and d.time == pm.Dist("tri", 0.5, 1.0, 2.0)
    assert d.manning == pm.Dist("tri", 0.75, 1.0, 1.3) and d.methods == []
    assert "placeholders" in pm.DEFAULTS_NOTE and "NOT published" in pm.DEFAULTS_NOTE
    s = pm.ProbSpec.from_text("width: tri 0.5 1 1.5; time: uniform 0.5, 2\n"
                              "Manning's n: lognormal(1, 0.15); methods: piping 0.6, "
                              "Froehlich 1995 overtopping 0.4")
    assert s.width == pm.Dist("triangular", 0.5, 1.0, 1.5)
    assert s.time == pm.Dist("uniform", 0.5, 2.0) and s.manning == pm.Dist("lognormal", 1, 0.15)
    assert s.methods == [("piping", 0.6), ("froehlich1995_overtopping", 0.4)]
    assert [round(p, 3) for _m, p in s.method_choices("x")] == [0.6, 0.4]
    # what is not given keeps its default; 'off' = not varied; bare numbers are fine
    s2 = pm.ProbSpec.from_text("n: off\nwidth: 0.7 1 1.4")
    assert s2.manning == pm.Dist("fixed", 1.0) and not s2.manning.varies()
    assert s2.width == pm.Dist("tri", 0.7, 1.0, 1.4) and s2.time == pm.default_time()
    assert pm.Dist.from_text("1.1 1.3") == pm.Dist("uniform", 1.1, 1.3)
    assert pm.Dist.from_text("") is None
    # the Processing tool's one text per input (empty = not varied, a 'name:' prefix is ok)
    p = pm.ProbSpec.from_parts("tri 0.5 1 1.5", "", "width: uniform 0.8 1.2", "MLM, vtg 2")
    assert p.time == pm.Dist("fixed", 1.0) and p.manning == pm.Dist("uniform", 0.8, 1.2)
    assert p.methods == [("mlm1984", 1.0), ("vtg1990", 2.0)]
    # dict round trip (also through JSON)
    for sp in (s, s2, p, d):
        assert pm.ProbSpec.from_dict(json.loads(json.dumps(sp.to_dict()))) == sp
    assert pm.ProbSpec.from_dict({"n": "tri 0.8 1 1.2", "methods": [["tr60", 1]]}).methods == \
        [("tr60", 1.0)]
    assert pm.method_id("TR-60") == "tr60" and pm.method_id("f95 piping") == \
        "froehlich1995_piping"
    for bad, word in (("pool: 1", "pool level"), ("foo: tri 1 2 3", "unknown"),
                      ("width tri 1 2 3", "name: distribution"), ("methods: user", "user"),
                      ("methods: piping, piping", "twice"), ("methods: piping 0", "above 0"),
                      ("width: tri 1.5 1 0.5", "Breach width"), ("time: gamma 1 2", "unknown"),
                      ("methods: kaboom", "unknown breach method"), ("width:", "no distribution")):
        try:
            pm.ProbSpec.from_text(bad)
            raise AssertionError("accepted " + bad)
        except ValueError as e:
            assert word in str(e), (bad, str(e))
    try:
        pm.ProbSpec.from_dict({"pool_offset_ft": 2.0})
        raise AssertionError("pool offset accepted")
    except ValueError as e:
        assert "pool level at failure is not varied" in str(e)


# ---------------------------------------------------------------------------------------
# statistics
# ---------------------------------------------------------------------------------------
def _stack(n=12, shape=(5, 6), seed=2):
    rng = np.random.default_rng(seed)
    d = rng.gamma(2.0, 0.6, (n,) + shape).astype(np.float32)
    d[rng.random(d.shape) < 0.35] = np.nan                 # dry in some runs
    d[:, 0, 0] = np.nan                                    # never wet
    v = np.where(np.isfinite(d), rng.random(d.shape) * 3, np.nan).astype(np.float32)
    a = np.where(np.isfinite(d) & (d >= 0.3048), rng.random(d.shape) * 5, np.nan).astype(
        np.float32)
    a[:, 1, 1] = np.nan
    a[0, 1, 1] = 2.0                                       # reached in 1 of 12 runs (< 10%)
    a[:, 1, 2] = np.nan
    a[:2, 1, 2] = [3.0, 1.0]                               # 2 of 12 (>= 10%)
    return d, v, a


@test
def probabilistic_blockwise_statistics_match_numpy():
    pm = _pm()
    d, v, a = _stack()
    n = d.shape[0]
    work = tempfile.mkdtemp()
    try:
        mm = {}
        for kind, arr in (("depth", d), ("velocity", v), ("arrival", a)):
            mm[kind] = []
            for i in range(n):
                p = os.path.join(work, "{}_{}.npy".format(kind, i))
                np.save(p, arr[i])
                mm[kind].append(np.load(p, mmap_mode="r"))
        thr = [0.3048, 0.9144]
        d0 = np.nan_to_num(d, nan=0.0)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            ref_a = np.nanpercentile(a, 10, axis=0)
        k = np.isfinite(a).sum(axis=0)
        ref_a = np.where(k >= 0.1 * n, ref_a, np.nan)
        for rows in (1, 2, None):
            with warnings.catch_warnings():
                warnings.simplefilter("error", RuntimeWarning)
                r = pm.blockwise_stats(mm["depth"], mm["velocity"], mm["arrival"], d.shape[1:],
                                       thr, block_rows=rows)
            for i, t in enumerate(thr):
                assert np.allclose(r["prob"][i], np.mean(d0 >= np.float32(t), axis=0)), i
                assert r["prob"][i].min() >= 0 and r["prob"][i].max() <= 1
            for q in (10.0, 50.0, 90.0):
                assert np.allclose(r["depth"][q], np.percentile(d0, q, axis=0), rtol=1e-5,
                                   atol=1e-6), q
            assert np.allclose(r["velocity"], np.percentile(np.nan_to_num(v), 90, axis=0),
                               rtol=1e-5, atol=1e-6)
            assert np.allclose(r["arrival"], ref_a, rtol=1e-5, atol=1e-6, equal_nan=True)
            assert np.array_equal(r["reached"], k)
            assert np.isnan(r["arrival"][1, 1]) and np.isfinite(r["arrival"][1, 2])
            _close(float(r["arrival"][1, 2]), 1.2, 1e-6)       # 10th pct of (1, 3)
            assert r["depth"][10.0][0, 0] == 0.0 and r["prob"][0][0, 0] == 0.0
            assert np.all(r["depth"][10.0] <= r["depth"][50.0] + 1e-6) and \
                np.all(r["depth"][50.0] <= r["depth"][90.0] + 1e-6)
        # percentile_sorted on its own, with varying counts
        s = np.sort(np.array([[1.0, np.nan], [3.0, np.nan], [2.0, np.nan]]), axis=0)
        got = pm.percentile_sorted(s, np.array([3, 0]), 50.0)
        assert got[0] == 2.0 and np.isnan(got[1])
        del mm
    finally:
        shutil.rmtree(work, ignore_errors=True)


@test
def probabilistic_finished_runs_are_found_only_for_the_same_key_and_grid():
    pm = _pm()
    work = tempfile.mkdtemp()
    try:
        arrays = {k: np.full((3, 4), i + 0.5) for i, k in enumerate(pm.KINDS)}
        row = {"run": 1, "status": "done", "flooded_ac": 12.5}
        pm._save_run(work, "run_001", "k1", arrays, row, {"run": 1, "width_factor": 1.1})
        assert not [f for f in os.listdir(work) if f.endswith(".part")]
        assert pm._load_done(work, "run_001", "k1", (3, 4)) == row
        assert np.load(pm._npy(work, "run_001", "depth")).dtype == np.float32
        assert pm._load_done(work, "run_001", "k2", (3, 4)) is None         # other sample
        assert pm._load_done(work, "run_001", "k1", (4, 3)) is None         # other grid
        assert pm._load_done(work, "run_002", "k1", (3, 4)) is None         # never run
        os.remove(pm._npy(work, "run_001", "velocity"))
        assert pm._load_done(work, "run_001", "k1", (3, 4)) is None         # incomplete
    finally:
        shutil.rmtree(work, ignore_errors=True)


@test
def probabilistic_run_settings_scale_the_drawn_methods_own_values():
    pm = _pm()
    from nd_breach_runner.core import workflow, breach as br
    st = workflow.RunSettings(manning_n=0.05, resume=True, keep_model_files=True,
                              tier=workflow.TIER_BOTH)
    st.overrides = br.BreachOverrides(side_slope_h=1.0)
    plan = {"bounds": (0, 0, 10, 10), "sim_time": 3600.0, "inflow_xy": (1, 1), "path": [],
            "direction": (1, 0)}
    params = {"piping": {"avg_breach_width_ft": 60.0, "formation_time_hr": 0.5},
              "overtopping": {"avg_breach_width_ft": 90.0, "formation_time_hr": 0.4},
              "tr60": {"rise_time_hr": 0.6}}
    smp = {"run": 1, "width_factor": 1.2, "time_factor": 0.5, "n_factor": 1.1,
           "method": "piping"}
    c = pm._settings_for(st, smp, params, plan, False)
    _close(c.overrides.avg_width_ft, 72.0)
    _close(c.overrides.formation_hr, 0.25)
    assert c.overrides.side_slope_h == 1.0                  # the user's override stays
    _close(c.manning_n, 0.055)
    _close(c.manning_scale, 1.1)                            # the n raster is scaled too
    assert c.fixed_plan is plan and c.tier == workflow.TIER_LISFLOOD
    assert not (c.resume or c.keep_model_files or c.baseline or c.write_final or c.products)
    assert pm._settings_for(st, smp, params, plan, True).write_final
    c2 = pm._settings_for(st, dict(smp, method="overtopping"), params, plan, False)
    assert c2.breach_method == "overtopping" and c2.overrides.side_slope_h is None
    _close(c2.overrides.avg_width_ft, 108.0)
    _close(c2.overrides.formation_hr, 0.2)
    c3 = pm._settings_for(st, dict(smp, method="tr60"), params, plan, False)
    _close(c3.overrides.tr60_rise_hr, 0.3)
    assert c3.overrides.avg_width_ft is None
    assert st.overrides.avg_width_ft is None and st.manning_n == 0.05 and st.resume


@test
def probabilistic_modules_are_python39():
    for name in ("core/probabilistic.py", "algorithms_probabilistic.py",
                 "tests/test_probabilistic.py"):
        path = os.path.join(PLUGIN, name)
        py_compile.compile(path, cfile=os.path.join(tempfile.gettempdir(),
                                                    os.path.basename(name) + "c"), doraise=True)
        src = open(path, encoding="utf-8").read()
        tree = ast.parse(src, feature_version=(3, 9))
        for node in ast.walk(tree):
            assert not isinstance(node, getattr(ast, "Match", ())), name
            ann = []
            if isinstance(node, (ast.AnnAssign, ast.arg)):
                ann = [node.annotation]
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                ann = [node.returns]
            for a in ann:
                for sub in ast.walk(a) if a is not None else ():
                    assert not (isinstance(sub, ast.BinOp) and isinstance(sub.op, ast.BitOr)), \
                        "X | Y annotation in " + name
    # the core module needs numpy and the standard library only
    tree = ast.parse(open(os.path.join(PLUGIN, "core", "probabilistic.py")).read())
    for node in ast.walk(tree):
        mods = []
        if isinstance(node, ast.Import):
            mods = [n.name for n in node.names]
        elif isinstance(node, ast.ImportFrom) and not node.level:
            mods = [node.module or ""]
        assert not any(m.split(".")[0] in ("scipy", "qgis", "osgeo", "pandas") for m in mods), mods


# ---------------------------------------------------------------------------------------
# the Processing tool
# ---------------------------------------------------------------------------------------
def _load_tool():
    from nd_breach_runner.tests import fake_qgis as fq
    fq.install()
    import nd_breach_runner.algorithms as algs
    importlib.reload(algs)
    import nd_breach_runner.algorithms_probabilistic as ap
    importlib.reload(ap)
    return algs, ap, fq


@test
def probabilistic_algorithm_initialises_through_the_fake_harness():
    algs, ap, fq = _load_tool()
    assert ap.PROBABILISTIC_ALGORITHMS == (ap.ProbabilisticAlgorithm,)
    a = ap.ProbabilisticAlgorithm()
    a.initAlgorithm()
    assert isinstance(a, algs.SensitivityAlgorithm)
    assert a.name() == "probabilistic"
    assert a.displayName() == "Probabilistic breach runs (Monte Carlo)"
    assert a.createInstance().name() == "probabilistic"
    for k in ("DAM_ID", "DAM_POINT", "HEIGHT_FT", "METHOD", "SCENARIO", "INFLOW_CSV",
              "RECEPTOR_POINTS", "STRUCTURES", "DEM", "CELL", "DAMS_FOLDER", "PROB_N",
              "PROB_SEED", "PROB_THRESHOLDS", "PROB_WIDTH", "PROB_TIME", "PROB_MANNING",
              "PROB_METHODS", "CLASSIFY", "CLASS_ALG", "CLASS_MAP"):
        assert a.parameterDefinition(k) is not None, k
    assert a.parameterDefinition("SENS_WIDTH") is None
    for k in ("PROB_RUNS", "PROB_SUMMARY", "PROB_REPORT", "PROB_FOLDER", "FOLDER"):
        assert k in a.outputs, k
    h = a.shortHelpString()
    assert "PLACEHOLDERS" in h and "pool level" in h and "Probability Depth at least" in h
    pm = _pm()
    ctx = fq.QgsProcessingContext()
    assert a.spec_from_params({}, ctx) == pm.ProbSpec()          # left out: the defaults
    s = a.spec_from_params({"PROB_WIDTH": "", "PROB_METHODS": "overtopping"}, ctx)
    assert s.width == pm.Dist("fixed", 1.0) and s.time == pm.default_time()   # empty: not varied
    assert s.methods == [("overtopping", 1.0)]
    # a bad distribution stops the tool before any model runs (no DEM needed)
    try:
        a.processAlgorithm({"PROB_WIDTH": "tri 2 1 0.5"}, fq.QgsProcessingContext(),
                           fq.QgsProcessingFeedback())
        raise AssertionError("expected an error")
    except fq.QgsProcessingException as e:
        assert "Breach width" in str(e), str(e)


# ---------------------------------------------------------------------------------------
# model tests (LISFLOOD-FP on the synthetic valley)
# ---------------------------------------------------------------------------------------
def _exe():
    return os.environ.get("NDBREACH_LISFLOOD", "")


def _dam(**k):
    from nd_breach_runner.core import breach as br
    d = dict(dam_id="T1", dam_height_ft=25.0, storage_top_acft=500.0, top_of_dam_ft=1850.0,
             surface_area_top_ac=60.0, crest_width_ft=12, us_slope_h=3, ds_slope_h=3)
    d.update(k)
    return br.DamData(**d)


def _load(path):
    return np.load(os.path.splitext(path)[0] + ".npy")


@model_test
def probabilistic_runs_on_the_synthetic_valley_and_resume():
    from nd_breach_runner.core import workflow, impacts
    from nd_breach_runner.core.units import FT
    from nd_breach_runner.tests.synthetic import SyntheticTerrain, npy_writer
    pm = _pm()
    d = tempfile.mkdtemp()
    rec = impacts.Receptors()
    rec.add_points([[800.0, 0.0], [1400.0, 0.0], [1400.0, 330.0]], ids=["a", "b", "c"])
    st = workflow.RunSettings(cell_size_m=20.0, initial_length_mi=1.5, half_width_mi=0.5,
                              max_iterations=1, lisflood_exe=_exe(), threads=2, sim_time_hr=3.0,
                              receptors=rec, products=False)
    spec = pm.ProbSpec.from_text("width: tri 0.5 1 1.5; time: tri 0.5 1 2; n: tri 0.75 1 1.3")
    out = os.path.join(d, "T1")
    classify = lambda folder: "High" if os.path.exists(os.path.join(
        folder, "Depth (Max).tif")) else "?"
    logs = []
    try:
        r = pm.run_probabilistic(_dam(), (0.0, 0.0), st, SyntheticTerrain(), npy_writer, out,
                                 spec=spec, n=4, seed=2026, thresholds_ft=(1.0, 3.0),
                                 log=logs.append, classify=classify)
        assert r.completed == 4 and r.resumed == 0 and r.seed == 2026, r.warnings
        assert any("should take about" in m for m in logs), logs
        assert [x["status"] for x in r.rows] == ["done"] * 4
        assert all(x["hazard_class"] == "High" for x in r.rows), r.rows
        assert r.samples == pm.draw_samples(spec, 4, 2026, st.breach_method)
        # the base run's domain is every run's
        assert all(os.path.exists(pm._npy(r.work_folder, "run_{:03d}".format(i), "depth"))
                   for i in range(1, 5))
        dep = [np.load(pm._npy(r.work_folder, "run_{:03d}".format(i), "depth"), mmap_mode="r")
               for i in range(1, 5)]
        assert all(x.shape == (r.base.grid.ny, r.base.grid.nx) for x in dep)
        assert not os.path.exists(os.path.join(r.runs_folder, "run_001", "_breach_model",
                                               "lisflood", "results"))
        # rasters: probabilities in [0, 1] and equal to the share of runs; P10 <= P50 <= P90
        names = pm.raster_names((1.0, 3.0))
        p1 = _load(r.rasters[names[("prob", 1.0)]])
        p3 = _load(r.rasters[names[("prob", 3.0)]])
        for p in (p1, p3):
            assert np.nanmin(p) > 0 and np.nanmax(p) <= 1.0 and np.isfinite(p).any()
        d0 = np.stack([np.nan_to_num(np.asarray(x), nan=0.0) for x in dep])
        share = (d0 >= np.float32(FT)).mean(axis=0)
        assert np.allclose(np.nan_to_num(p1), share)
        assert np.all(np.nan_to_num(p3) <= np.nan_to_num(p1) + 1e-9)
        q = {k: np.nan_to_num(_load(r.rasters[names[("depth", k)]])) for k in (10.0, 50.0, 90.0)}
        assert np.all(q[10.0] <= q[50.0] + 1e-6) and np.all(q[50.0] <= q[90.0] + 1e-6)
        assert np.allclose(q[90.0], np.where(np.percentile(d0, 90, axis=0) > 1e-4,
                                             np.percentile(d0, 90, axis=0), 0) / FT, atol=1e-5)
        arr = _load(r.rasters[names[("arrival", 10.0)]])
        v90 = _load(r.rasters[names[("velocity", 90.0)]])
        assert np.isfinite(arr).any() and np.nanmin(arr) >= 0 and np.nanmax(v90) > 0
        # tables, plot, report, manifest
        rows = list(csv.DictReader(open(r.runs_csv)))
        assert len(rows) == 4 and rows[0]["structures_flooded"] != ""
        summ = {x["metric"]: x for x in csv.DictReader(open(r.summary_csv))}
        assert float(summ["flooded_ac"]["p10"]) <= float(summ["flooded_ac"]["p90"])
        assert summ["hazard class = High"]["mean"] == "1.0"
        recs = {x["id"]: x for x in csv.DictReader(open(r.receptors_csv))}
        assert set(recs) == {"a", "b", "c"}
        for x in recs.values():
            assert 0.0 <= float(x["p_depth_ge_1ft"]) <= 1.0
            assert float(x["p_depth_ge_3ft"]) <= float(x["p_depth_ge_1ft"])
        assert float(recs["a"]["p_depth_ge_1ft"]) == 1.0
        assert open(r.convergence_svg).read().startswith("<svg")
        html = open(r.report_html, encoding="utf-8").read()
        assert "What the spread means" in html and "placeholders" in html and "<svg" in html
        man = json.load(open(r.manifest))
        assert man["seed"] == 2026 and len(man["samples"]) == 4
        assert man["spec"] == spec.to_dict() and "pool" in man["pool_level"]
        before = {k: _load(p) for k, p in r.rasters.items()}
        del dep
        # resume: an interrupted study (run 2 not finished) only runs run 2
        os.remove(os.path.join(r.work_folder, "run_002.json"))
        logs2 = []
        r2 = pm.run_probabilistic(_dam(), (0.0, 0.0), st, SyntheticTerrain(), npy_writer, out,
                                  spec=spec, n=4, seed=2026, log=logs2.append, classify=classify)
        started = [m for m in logs2 if m.startswith("=== probabilistic run")]
        assert len(started) == 1 and "run 2/4" in started[0], started
        assert r2.resumed == 3 and r2.completed == 4
        assert [x["resumed"] for x in r2.rows] == [True, False, True, True]
        for k, p in r2.rasters.items():
            assert np.allclose(_load(p), before[k], equal_nan=True, atol=1e-4), k
    finally:
        shutil.rmtree(d, ignore_errors=True)


class _FakeRaster:
    def crs(self):
        from nd_breach_runner.tests import fake_qgis
        return fake_qgis.QgsCoordinateReferenceSystem("EPSG:26914")

    def source(self):
        return "synthetic"


@model_test
def probabilistic_algorithm_end_to_end_through_the_fake_harness():
    from nd_breach_runner.tests.synthetic import SyntheticTerrain, npy_writer
    algs, ap, fq = _load_tool()
    orig = algs._Base.gis

    def gis(self, parameters, context, crs, pt):
        return (SyntheticTerrain(), npy_writer, fq.QgsCoordinateTransform(),
                fq.QgsCoordinateReferenceSystem("EPSG:26914"))
    algs._Base.gis = gis
    d = tempfile.mkdtemp()
    try:
        a = ap.ProbabilisticAlgorithm()
        a.initAlgorithm()
        fb = fq.QgsProcessingFeedback()
        ctx = fq.QgsProcessingContext()
        pt = fq.FakeFeature(fq.FakeGeometry("point", [[(800.0, 0.0)]]), name="house")
        r = a.processAlgorithm({
            "DAM_ID": "P1", "DAM_POINT": fq.QgsPointXY(0, 0), "HEIGHT_FT": 25,
            "STORAGE_ACFT": 500, "TOP_FT": 1850, "AREA_AC": 60, "DEM": _FakeRaster(),
            "CELL": 25.0, "LENGTH_MI": 1.5, "HALF_WIDTH_MI": 0.5, "MAX_RUNS": 1,
            "DURATION_HR": 3.0, "LISFLOOD_EXE": _exe(), "DAMS_FOLDER": d, "PRODUCTS": False,
            "RECEPTOR_POINTS": fq.FakeSource([pt]), "PROB_N": 2, "PROB_SEED": 99,
            "PROB_THRESHOLDS": "0.5, 2", "PROB_WIDTH": "uniform 0.8 1.2", "PROB_TIME": "",
            "PROB_MANNING": "lognormal 1 0.1", "PROB_METHODS": "piping 1, overtopping 1"},
            ctx, fb)
        assert r["PROB_FOLDER"].endswith("Probabilistic") and r["FOLDER"].endswith("P1")
        rows = list(csv.DictReader(open(r["PROB_RUNS"])))
        assert sorted(x["method"] for x in rows) == ["overtopping", "piping"], rows
        assert all(float(x["time_factor"]) == 1.0 for x in rows)
        assert all(0.8 <= float(x["width_factor"]) <= 1.2 for x in rows)
        for n in ("Probability Depth at least 0.5 ft.tif", "Probability Depth at least 2 ft.tif",
                  "Depth P50 (ft).tif", "Arrival P10 (hr).tif", "Velocity P90 (ft-s).tif"):
            assert os.path.exists(os.path.join(r["PROB_FOLDER"], n)), n
        assert len([p for p in ctx.loaded if "Probabilistic" in p]) == 7, ctx.loaded
        assert os.path.exists(r["PROB_SUMMARY"]) and os.path.exists(r["PROB_REPORT"])
        assert any(m.startswith("Breach width: uniform") for m in fb.info), fb.info
        assert any("should take about" in m for m in fb.info), fb.info
        man = json.load(open(os.path.join(r["PROB_FOLDER"], "probabilistic_manifest.json")))
        assert man["seed"] == 99 and man["n"] == 2 and man["thresholds_ft"] == [0.5, 2.0]
        assert os.path.exists(os.path.join(r["PROB_FOLDER"], "Probabilistic_Receptors.csv"))
    finally:
        algs._Base.gis = orig
        shutil.rmtree(d, ignore_errors=True)
