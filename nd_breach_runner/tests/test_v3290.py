"""3.29.0: the main run's LISFLOOD-FP solver is a choice (ACC stays the default).

Only solvers that write a maximum velocity and honour the arrival-depth patch (inittm_depth)
can be chosen: ACC, FV2 and MWDG2 (Nick, 2026-11-06). FV1 and DG2 are refused, never swapped
for another solver. FV2 / MWDG2 are checked on the synthetic channel against the LISFLOOD-FP
build before the first run; a build without the patch or without velocity stops the run.
They can be picked with the tools' Solver parameter, the CLI's model.lisflood_solver, the
NDBREACH_LISFLOOD_SOLVER environment variable or the provider setting, and run with the slope
limiter."""
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
    assert lf.MAIN_SOLVERS == ("acceleration", "fv2", "mwdg2")
    assert lf.solver_from_text("ACC") == lf.SOLVER_ACC
    assert lf.solver_from_text(" Fv2-LL ") == lf.SOLVER_FV2
    assert lf.solver_from_text("mwdg2") == lf.SOLVER_MWDG2
    assert lf.solver_from_text("", lf.SOLVER_FV2) == lf.SOLVER_FV2
    for bad, why in (("fv1", "velocity"), ("dg2", "slope"), ("bogus", "")):
        try:
            lf.solver_from_text(bad)
            raise AssertionError(bad + " must be refused")
        except lf.ModelError as e:
            assert bad in str(e) and why in str(e) and "FV2" in str(e), str(e)
    with _env(NDBREACH_LISFLOOD_SOLVER=None):
        assert workflow.RunSettings().lisflood_solver == lf.SOLVER_ACC
    with _env(NDBREACH_LISFLOOD_SOLVER="fv2"):
        st = workflow.RunSettings()
        assert st.lisflood_solver == lf.SOLVER_FV2
        assert not getattr(st, "lisflood_solver_error", "")
    for bad in ("bogus", "fv1"):
        with _env(NDBREACH_LISFLOOD_SOLVER=bad):
            st = workflow.RunSettings()
            assert bad in st.lisflood_solver_error


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


@contextlib.contextmanager
def _check_result(res, calls=None):
    """lisflood.check_lisflood stubbed to return `res`; the solver-check cache emptied."""
    from nd_breach_runner.core import lisflood as lf, workflow

    def fake(exe, wd, solver=lf.SOLVER_ACC):
        if calls is not None:
            calls.append(solver)
        return dict(res)
    old = lf.check_lisflood
    lf.check_lisflood = fake
    workflow._SOLVER_CHECKED.clear()
    try:
        yield
    finally:
        lf.check_lisflood = old
        workflow._SOLVER_CHECKED.clear()


def _refused(st, d, *words):
    from nd_breach_runner.core import breach as br, workflow
    try:
        workflow._check_solver(st, d)
    except br.BreachInputError as e:
        assert all(w in str(e) for w in words), (words, str(e))
        return
    raise AssertionError("not refused: " + str(words))


@test
def v3290_solver_is_checked_against_the_build_before_the_run():
    from nd_breach_runner.core import lisflood as lf, workflow
    d = _tmp()
    good = {"ok": True, "patched": True, "velocity": True, "message": ""}
    try:
        calls = []
        with _check_result(good, calls):
            workflow._check_solver(_settings(), d)                 # ACC: nothing to check
            assert calls == []
            st = _settings(lisflood_solver=lf.SOLVER_FV2)
            workflow._check_solver(st, d)
            workflow._check_solver(st, d)                          # checked once per build
            assert calls == [lf.SOLVER_FV2]
            workflow._check_solver(_settings(lisflood_solver=lf.SOLVER_MWDG2), d)
            assert calls == [lf.SOLVER_FV2, lf.SOLVER_MWDG2]
            _refused(_settings(lisflood_solver=lf.SOLVER_FV1), d, "fv1", "ACC, FV2 or MWDG2")
            _refused(_settings(lisflood_solver=lf.SOLVER_FV2, lisflood_exe=lf.BUILTIN_EXE), d,
                     "built-in", "FV2")
            fine = _settings(lisflood_solver=lf.SOLVER_FV1)
            fine.near_dam_fv1 = True                 # the near-dam refine's own FV1 run
            workflow._check_solver(fine, d)
            assert calls == [lf.SOLVER_FV2, lf.SOLVER_MWDG2]
            assert "near_dam_fv1" not in workflow.key_settings(fine)
        with _check_result(dict(good, patched=False)):
            _refused(_settings(lisflood_solver=lf.SOLVER_FV2), d, "FV2", "inittm_depth")
        with _check_result(dict(good, velocity=False)):
            _refused(_settings(lisflood_solver=lf.SOLVER_MWDG2), d, "MWDG2", "no maximum velocity")
        with _check_result({"ok": False, "patched": False, "velocity": False,
                            "message": "boom"}):
            _refused(_settings(lisflood_solver=lf.SOLVER_FV2), d, "did not run", "boom")
        with _check_result(good):
            with _env(NDBREACH_LISFLOOD_SOLVER="dg2"):
                _refused(_settings(), d, "dg2")
    finally:
        shutil.rmtree(d, ignore_errors=True)


@contextlib.contextmanager
def _models(calls, velocity=True):
    """LISFLOOD-FP stubbed (run + read_results)."""
    from nd_breach_runner.core import lisflood as lf

    def fake_run(wd, par, st, progress=None, cancel=None, log=None, name="run"):
        calls.append((os.path.basename(wd), st.solver, st.limit_slopes))
        return 60.0

    def fake_read(wd, dem, st, hydrograph, name="run"):
        X, Y = np.meshgrid(dem.x_centers(), dem.y_centers())
        d = np.where((X > 0) & (X < 1000) & (np.abs(Y) < 300), 0.5, 0.0)
        wet = d > 0
        vel = np.where(wet, 1.2 if velocity else 0.0, np.nan)
        arr = np.where(wet, 0.5, np.nan)
        wse = np.where(wet, dem.data + 0.5, np.nan)
        stats = {"cells": int(d.size), "cell_size_m": dem.dx, "sim_time_hr": 2.0,
                 "max_depth_ft": 1.6, "max_velocity_fps": 3.9 if velocity else 0.0,
                 "flooded_acres": 10.0, "peak_inflow_cfs": 100.0,
                 "max_volume_error_pct": 0.0, "worst_interval_error_pct": 0.0,
                 "injected_acft": 10.0, "in_domain_acft": 10.0, "initial_water_acft": 0.0,
                 "velocity_written": velocity, "solver": lf.SOLVER_SHORT[st.solver]}
        return lf.Envelopes(dem.like(d, nodata=np.nan), dem.like(vel, nodata=np.nan),
                            dem.like(arr, nodata=np.nan), dem.like(wse, nodata=np.nan),
                            stats, [])

    old = lf.run, lf.read_results
    lf.run, lf.read_results = fake_run, fake_read
    try:
        yield
    finally:
        lf.run, lf.read_results = old


@test
def v3290_fv2_run_uses_fv2_and_reports_it():
    from nd_breach_runner.core import lisflood as lf, workflow
    from nd_breach_runner.tests.synthetic import SyntheticTerrain, npy_writer
    good = {"ok": True, "patched": True, "velocity": True, "message": ""}
    calls = []
    d = _tmp()
    try:
        st = _settings(lisflood_solver=lf.SOLVER_FV2)
        with _check_result(good), _models(calls):
            r = workflow.run_dam(_dam(), (0.0, 0.0), st, SyntheticTerrain(), npy_writer,
                                 os.path.join(d, "T1"))
        with open(r.report_json, encoding="utf-8") as f:
            rep = json.load(f)
        assert calls and all(c[1] == lf.SOLVER_FV2 and c[2] for c in calls), calls
        assert rep["summary"]["lisflood_solver"] == "FV2"
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
def v3290_solver_that_stops_writing_velocity_stops_the_run():
    from nd_breach_runner.core import lisflood as lf, workflow
    from nd_breach_runner.tests.synthetic import SyntheticTerrain, npy_writer
    good = {"ok": True, "patched": True, "velocity": True, "message": ""}
    d = _tmp()
    try:
        st = _settings(lisflood_solver=lf.SOLVER_MWDG2)
        with _check_result(good), _models([], velocity=False):
            try:
                workflow.run_dam(_dam(), (0.0, 0.0), st, SyntheticTerrain(), npy_writer,
                                 os.path.join(d, "T1"))
                raise AssertionError("must stop")
            except lf.ModelError as e:
                assert "no maximum velocity" in str(e), str(e)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _real_exe():
    return os.environ.get("NDBREACH_LISFLOOD", "")


@model_test
def v3290_every_solver_runs_on_the_real_lisflood():
    """The bundled LISFLOOD-FP passes the FV2 / MWDG2 check (patched, velocity) and the run
    writes depth and velocity with each offered solver; FV1 fails the velocity check."""
    from nd_breach_runner.core import lisflood as lf, workflow
    from nd_breach_runner.tests.synthetic import SyntheticTerrain, npy_writer
    exe = _real_exe()
    assert exe, "set NDBREACH_LISFLOOD"
    d = _tmp()
    try:
        for s in (lf.SOLVER_FV2, lf.SOLVER_MWDG2):
            chk = lf.check_lisflood(exe, os.path.join(d, "chk_" + s), s)
            assert chk["ok"] and chk["patched"] and chk["velocity"], (s, chk)
        chk = lf.check_lisflood(exe, os.path.join(d, "chk_fv1"), lf.SOLVER_FV1)
        assert not chk["velocity"], chk
    finally:
        shutil.rmtree(d, ignore_errors=True)
    workflow._SOLVER_CHECKED.clear()
    out = {}
    for s in lf.MAIN_SOLVERS:
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
    assert [v[0] for v in out.values()] == ["ACC", "FV2", "MWDG2"], out
