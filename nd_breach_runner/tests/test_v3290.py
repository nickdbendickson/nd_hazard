"""3.29.0: the main run's LISFLOOD-FP solver is a choice (ACC stays the default).

FV1, FV2 and MWDG2 can be picked with the tools' Solver parameter, the CLI's
model.lisflood_solver, the NDBREACH_LISFLOOD_SOLVER environment variable or the provider
setting. FV2 / MWDG2 run with the slope limiter. LISFLOOD-FP 8.1 writes no maximum velocity and
no arrival time for FV1, so those come from an ACC run of the same domain (a wet cell never
reads 0 ft/s while an ACC velocity is in reach)."""
import contextlib
import json
import os
import shutil
import tempfile

import numpy as np

from nd_breach_runner.tests.registry import test, model_test
from nd_breach_runner.tests.test_v320 import _dam, _settings
from nd_breach_runner.tests.test_v3281 import _par_lines


def _tmp():
    return tempfile.mkdtemp(prefix="ndbr_v3290_")


@contextlib.contextmanager
def _env(**kv):
    old = {k: os.environ.get(k) for k in kv}
    try:
        for k, v in kv.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@test
def v3290_solver_names_and_env_default():
    from nd_breach_runner.core import lisflood as lf, workflow
    assert lf.SOLVERS == ("acceleration", "fv1", "fv2", "mwdg2")
    assert lf.solver_from_text("ACC") == lf.SOLVER_ACC
    assert lf.solver_from_text(" Fv2-LL ") == lf.SOLVER_FV2
    assert lf.solver_from_text("mwdg2") == lf.SOLVER_MWDG2
    assert lf.solver_from_text("", lf.SOLVER_FV1) == lf.SOLVER_FV1
    try:
        lf.solver_from_text("dg2")
        raise AssertionError("dg2 must be refused (it needs slope DEMs)")
    except lf.ModelError:
        pass
    with _env(NDBREACH_LISFLOOD_SOLVER=None):
        assert workflow.RunSettings().lisflood_solver == lf.SOLVER_ACC
    with _env(NDBREACH_LISFLOOD_SOLVER="fv2"):
        st = workflow.RunSettings()
        assert st.lisflood_solver == lf.SOLVER_FV2
        assert not getattr(st, "lisflood_solver_error", "")
    with _env(NDBREACH_LISFLOOD_SOLVER="bogus"):
        st = workflow.RunSettings()
        assert st.lisflood_solver == lf.SOLVER_ACC
        assert "bogus" in st.lisflood_solver_error


@test
def v3290_par_keywords():
    from nd_breach_runner.core import lisflood as lf

    def lines(**k):
        return _par_lines(lf.LisfloodSettings("lisflood", **k))
    acc = lines()
    assert "acceleration" in acc and "limitslopes" not in acc
    fv1 = lines(solver=lf.SOLVER_FV1)
    assert "fv1" in fv1 and "limitslopes" not in fv1
    for s in (lf.SOLVER_FV2, lf.SOLVER_MWDG2):
        ls = lines(solver=s)
        assert s in ls and "limitslopes" in ls, ls
        assert ls.index("limitslopes") == ls.index(s) + 1
        assert not any(x.startswith(("theta", "cfl")) for x in ls)
        assert "limitslopes" not in lines(solver=s, limit_slopes=False)


@test
def v3290_checkpoint_key_unchanged_at_defaults():
    from nd_breach_runner.core import lisflood as lf, workflow
    with _env(NDBREACH_LISFLOOD_SOLVER=None):
        k = workflow.key_settings(workflow.RunSettings())
        assert "lisflood_solver" not in k and "lisflood_limit_slopes" not in k
        k2 = workflow.key_settings(workflow.RunSettings(lisflood_solver=lf.SOLVER_FV2))
        assert k2.get("lisflood_solver") == lf.SOLVER_FV2 and "lisflood_limit_slopes" not in k2
        k3 = workflow.key_settings(workflow.RunSettings(lisflood_solver=lf.SOLVER_FV2,
                                                        lisflood_limit_slopes=False))
        assert k3.get("lisflood_limit_slopes") is False
    with _env(NDBREACH_LISFLOOD_SOLVER="bogus"):
        assert "lisflood_solver_error" not in workflow.key_settings(workflow.RunSettings())


@test
def v3290_fill_from_acc_gives_wet_cells_a_velocity():
    from nd_breach_runner.core import lisflood as lf
    from nd_breach_runner.core.grid import Grid

    def env(depth, vel, arr, written):
        g = Grid(np.zeros(depth.shape), 0.0, 100.0, 10.0)
        return lf.Envelopes(g.like(depth, nodata=np.nan), g.like(vel, nodata=np.nan),
                            g.like(arr, nodata=np.nan), g.like(depth, nodata=np.nan),
                            {"velocity_written": written})
    d = np.zeros((5, 8))
    d[2, 1:7] = 0.5                       # FV1 wet along a row
    ad = np.zeros((5, 8))
    ad[2, 1:5] = 0.5                      # ACC wet over part of it
    av = np.where(ad > 0, 2.0, np.nan)
    av[2, 4] = 3.0
    aa = np.where(ad > 0, 1.5, np.nan)
    fv1 = env(d, np.zeros_like(d), np.full(d.shape, np.nan), False)
    acc = env(ad, av, aa, True)
    st = lf.LisfloodSettings("x")
    info = lf.fill_from_acc(fv1, acc, st, passes=1)
    v = fv1.velocity.data
    assert v[2, 1] == 2.0 and v[2, 4] == 3.0
    assert v[2, 5] == 3.0, "a cell wet only in FV1 takes the largest ACC velocity next to it"
    assert v[2, 6] == 0.0 and info["velocity_cells_unfilled"] == 1      # out of reach (1 pass)
    assert fv1.stats["velocity_from"] == "ACC" and info["velocity"] and info["arrival"]
    assert fv1.arrival_hr.data[2, 2] == 1.5 and fv1.arrival_hr.data[2, 5] == 1.5
    assert np.isnan(fv1.arrival_hr.data[2, 6]) and info["arrival_cells_unfilled"] == 1
    assert np.all(v[d == 0] == 0.0)
    fv1b = env(d, np.zeros_like(d), np.full(d.shape, np.nan), False)
    lf.fill_from_acc(fv1b, acc, st, passes=5)
    assert fv1b.velocity.data[2, 6] == 3.0
    assert lf.needs_acc_fill(env(d, np.zeros_like(d), np.full(d.shape, np.nan), False))
    assert not lf.needs_acc_fill(env(ad, av, aa, True))


@contextlib.contextmanager
def _fv1_models(calls):
    """LISFLOOD-FP stubbed: FV1 writes depth only (as 8.1 does), ACC writes everything."""
    from nd_breach_runner.core import lisflood as lf

    def fake_run(wd, par, st, progress=None, cancel=None, log=None, name="run"):
        calls.append((os.path.basename(wd), st.solver, st.theta, st.cfl))
        return 60.0

    def fake_read(wd, dem, st, hydrograph, name="run"):
        X, Y = np.meshgrid(dem.x_centers(), dem.y_centers())
        d = np.where((X > 0) & (X < 1000) & (np.abs(Y) < 300), 0.5, np.nan)
        fv1 = st.solver == lf.SOLVER_FV1
        vel = np.zeros(d.shape) if fv1 else np.where(np.isfinite(d), 1.2, np.nan)
        arr = np.full(d.shape, np.nan) if fv1 else np.where(np.isfinite(d), 0.5, np.nan)
        wse = np.where(np.isfinite(d), dem.data + 0.5, np.nan)
        stats = {"cells": int(d.size), "cell_size_m": dem.dx, "sim_time_hr": 2.0,
                 "max_depth_ft": 1.6, "max_velocity_fps": 0.0 if fv1 else 3.9,
                 "flooded_acres": 10.0, "peak_inflow_cfs": 100.0,
                 "max_volume_error_pct": 0.0, "worst_interval_error_pct": 0.0,
                 "injected_acft": 10.0, "in_domain_acft": 10.0, "initial_water_acft": 0.0,
                 "velocity_written": not fv1, "solver": lf.SOLVER_SHORT[st.solver]}
        return lf.Envelopes(dem.like(np.where(np.isfinite(d), d, 0.0), nodata=np.nan),
                            dem.like(vel, nodata=np.nan), dem.like(arr, nodata=np.nan),
                            dem.like(wse, nodata=np.nan), stats, [])

    old = lf.run, lf.read_results
    lf.run, lf.read_results = fake_run, fake_read
    try:
        yield
    finally:
        lf.run, lf.read_results = old


@test
def v3290_fv1_run_takes_velocity_and_arrival_from_acc():
    from nd_breach_runner.core import lisflood as lf, workflow
    from nd_breach_runner.tests.synthetic import SyntheticTerrain, npy_writer
    calls = []
    d = _tmp()
    try:
        st = _settings(lisflood_solver=lf.SOLVER_FV1)
        with _fv1_models(calls):
            r = workflow.run_dam(_dam(), (0.0, 0.0), st, SyntheticTerrain(), npy_writer,
                                 os.path.join(d, "T1"))
        with open(r.report_json, encoding="utf-8") as f:
            rep = json.load(f)
        solvers = [c[1] for c in calls]
        assert lf.SOLVER_FV1 in solvers and lf.SOLVER_ACC in solvers, calls
        acc = [c for c in calls if c[1] == lf.SOLVER_ACC]
        assert all(c[0].endswith("_acc") and c[2] == lf.STABLE_THETA and c[3] == lf.STABLE_CFL
                   for c in acc), acc
        assert rep["lisflood_velocity_from_acc"]["solver"] == "FV1"
        assert rep["summary"]["lisflood_solver"] == "FV1"
        assert any("ACC run of the same domain" in w for w in rep.get("warnings", [])), \
            rep.get("warnings")
        v = np.load(os.path.splitext(r.final["velocity"])[0] + ".npy")
        assert np.nanmax(v) > 0.0, "velocity filled from ACC"
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _real_exe():
    return os.environ.get("NDBREACH_LISFLOOD", "")


@model_test
def v3290_every_solver_runs_on_the_real_lisflood():
    """The bundled LISFLOOD-FP runs each solver and the run writes depth and velocity."""
    from nd_breach_runner.core import lisflood as lf, workflow
    from nd_breach_runner.tests.synthetic import SyntheticTerrain, npy_writer
    exe = _real_exe()
    assert exe, "set NDBREACH_LISFLOOD"
    out = {}
    for s in lf.SOLVERS:
        d = _tmp()
        try:
            st = workflow.RunSettings(cell_size_m=20.0, initial_length_mi=1.0,
                                      half_width_mi=0.4, max_iterations=1, lisflood_exe=exe,
                                      threads=2, sim_time_hr=1.0, lisflood_solver=s)
            r = workflow.run_dam(_dam(), (0.0, 0.0), st, SyntheticTerrain(), npy_writer,
                                 os.path.join(d, "T1"))
            with open(r.report_json, encoding="utf-8") as f:
                rep = json.load(f)
            dep = np.load(os.path.splitext(r.final["depth"])[0] + ".npy")
            vel = np.load(os.path.splitext(r.final["velocity"])[0] + ".npy")
            out[s] = (rep["summary"]["lisflood_solver"], float(np.nanmax(dep)),
                      float(np.nanmax(vel)))
            assert np.nanmax(dep) > 0.5 and np.nanmax(vel) > 0.1, (s, out[s])
        finally:
            shutil.rmtree(d, ignore_errors=True)
    assert [v[0] for v in out.values()] == ["ACC", "FV1", "FV2", "MWDG2"], out
