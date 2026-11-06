"""3.28.1: LISFLOOD-FP's ACC solver re-run with stable settings when it loses its mass balance.

With LISFLOOD-FP's defaults (theta 1.0, cfl 0.7) the ACC scheme made water on the Runner's
default 10 m grid: DWR-2865 (Van Osting) +567% of the breach volume after 8.3 h (the water
level checkerboarded +-3 m in a deep pond 2 km down; flood 838 ac instead of 213 ac) and
DWR-4031 (Ward County) +36% while the hydrograph went in (fast shallow flow at the breach).
theta 0.7 with cfl 0.5 gives 0.0% and -0.2% on the same inputs. A run whose volume error is
over 1% is run again with them, and every later run of that dam uses them; a run that
balances is not changed. LISFLOOD-FP is stubbed as in test_v320."""
import contextlib
import json
import os
import shutil
import tempfile

import numpy as np

from nd_breach_runner.tests.registry import test
from nd_breach_runner.tests.test_v320 import _dam, _settings


def _tmp():
    return tempfile.mkdtemp(prefix="ndbr_v3281_")


def _par_lines(st):
    from nd_breach_runner.core import lisflood as lf
    from nd_breach_runner.core.breach import Hydrograph
    from nd_breach_runner.core.grid import Grid
    d = _tmp()
    try:
        dem = Grid(np.full((10, 20), 100.0), 0.0, 200.0, 20.0)
        hy = Hydrograph(np.array([0.0, 600.0]), np.array([10.0, 0.0]), "t")
        par = lf.write_inputs(os.path.join(d, "w"), dem, (30.0, 100.0), hy, st)
        with open(par) as f:
            return f.read().splitlines()
    finally:
        shutil.rmtree(d, ignore_errors=True)


@contextlib.contextmanager
def _models(calls, bad_error_pct=50.0, stable_error_pct=0.0, always_bad=False):
    """LISFLOOD-FP stubbed: a flood band east of the dam. The volume error is `bad_error_pct`
    with the default ACC settings and `stable_error_pct` with theta < 1 (unless always_bad).
    `calls` gets (folder, theta, cfl, par lines) for every run."""
    from nd_breach_runner.core import lisflood as lf

    def fake_run(wd, par, st, progress=None, cancel=None, log=None, name="run"):
        with open(par) as f:
            calls.append((os.path.basename(wd), st.theta, st.cfl, f.read().splitlines()))
        return 60.0

    def fake_read(wd, dem, st, hydrograph, name="run"):
        X, Y = np.meshgrid(dem.x_centers(), dem.y_centers())
        d = np.where((X > 0) & (X < 1000) & (np.abs(Y) < 300), 0.5, np.nan)
        vel = np.where(np.isfinite(d), 1.2, np.nan)
        arr = np.where(np.isfinite(d), 0.5, np.nan)
        wse = np.where(np.isfinite(d), dem.data + 0.5, np.nan)
        stable = st.theta < 1.0 and not always_bad
        err = stable_error_pct if stable else bad_error_pct
        stats = {"max_volume_error_pct": err, "peak_inflow_cfs": 1000.0}
        return lf.Envelopes(dem.like(d, np.nan), dem.like(vel, np.nan), dem.like(arr, np.nan),
                            dem.like(wse, np.nan), stats, [], {})
    old = (lf.run, lf.read_results)
    lf.run, lf.read_results = fake_run, fake_read
    try:
        yield
    finally:
        lf.run, lf.read_results = old


def _run(folder, st, **kw):
    from nd_breach_runner.core import workflow as wf
    from nd_breach_runner.tests.synthetic import SyntheticTerrain, npy_writer
    calls = []
    with _models(calls, **kw):
        res = wf.run_dam(_dam(), (0.0, 0.0), st, SyntheticTerrain(), npy_writer, folder)
    with open(res.report_json, encoding="utf-8") as f:
        return json.load(f), calls


@test
def v3281_stable_settings_and_par_lines():
    from nd_breach_runner.core import lisflood as lf
    assert (lf.STABLE_THETA, lf.STABLE_CFL, lf.STABILITY_RETRY_PCT) == (0.7, 0.5, 1.0)
    st = lf.LisfloodSettings("x")
    assert st.theta == 1.0 and st.cfl is None
    lines = _par_lines(st)            # LISFLOOD-FP's own defaults: nothing written
    assert not any(l.startswith(("theta", "cfl")) for l in lines), lines
    lines = _par_lines(lf.LisfloodSettings("x", theta=0.7, cfl=0.5))
    assert "theta 0.700" in lines and "cfl 0.500" in lines and "acceleration" in lines, lines
    lines = _par_lines(lf.LisfloodSettings("x", solver=lf.SOLVER_FV1, theta=0.7, cfl=0.5))
    assert not any(l.startswith(("theta", "cfl")) for l in lines), lines
    for kw in ({"theta": 0.0}, {"theta": 1.5}, {"cfl": 0.0}, {"cfl": 2.0}):
        try:
            lf.LisfloodSettings("x", **kw)
            raise AssertionError("expected ModelError for {}".format(kw))
        except lf.ModelError:
            pass


@test
def v3281_retry_switch_stays_out_of_the_checkpoint_key_while_on():
    from nd_breach_runner.core import workflow as wf
    old = os.environ.pop("NDBREACH_STABILITY_RETRY", None)
    try:
        assert wf.RunSettings().lisflood_stability_retry is True
        assert "lisflood_stability_retry" not in wf.key_settings(wf.RunSettings())
        off = wf.RunSettings(lisflood_stability_retry=False)
        assert wf.key_settings(off)["lisflood_stability_retry"] is False
        os.environ["NDBREACH_STABILITY_RETRY"] = "0"
        assert wf.RunSettings().lisflood_stability_retry is False
    finally:
        os.environ.pop("NDBREACH_STABILITY_RETRY", None)
        if old is not None:
            os.environ["NDBREACH_STABILITY_RETRY"] = old


@test
def v3281_a_run_that_balances_is_not_changed():
    d = _tmp()
    try:
        r, calls = _run(d, _settings(max_iterations=2), bad_error_pct=0.1)
        assert calls and all(c[1] == 1.0 and c[2] is None for c in calls), calls
        assert all(not any(l.startswith(("theta", "cfl")) for l in c[3]) for c in calls)
        assert "lisflood_stability" not in r
        assert not any("lost its mass balance" in w for w in r["warnings"])
        assert os.path.isdir(os.path.join(d, "_breach_model", "lisflood"))
        assert not os.path.exists(os.path.join(d, "_breach_model", "lisflood",
                                               "stable_settings.json"))
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
def v3281_a_run_that_makes_water_is_run_again_with_stable_settings():
    d = _tmp()
    try:
        r, calls = _run(d, _settings(max_iterations=2))
        # the first run, then the same run again with theta 0.7 / cfl 0.5
        assert calls[0][1:3] == (1.0, None) and calls[1][1:3] == (0.7, 0.5), calls[:2]
        assert calls[0][0] == calls[1][0] == "lisflood"
        assert "theta 0.700" in calls[1][3] and "cfl 0.500" in calls[1][3]
        # every later run of the dam starts with them (no second default run)
        assert all(c[1:3] == (0.7, 0.5) for c in calls[1:]), calls
        ls = r["lisflood_stability"]
        assert ls["stable_settings"] and ls["theta"] == 0.7 and ls["cfl"] == 0.5
        assert len(ls["events"]) == 1
        ev = ls["events"][0]
        assert ev["error_pct"] == 50.0 and ev["stable_error_pct"] == 0.0, ev
        w = [x for x in r["warnings"] if "lost its mass balance" in x]
        assert len(w) == 1 and "50.0%" in w[0] and "theta 0.7" in w[0] and "0.00%" in w[0], w
        assert os.path.exists(os.path.join(d, "_breach_model", "lisflood",
                                           "stable_settings.json"))
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
def v3281_retry_off_or_still_bad_is_reported_not_hidden():
    d = _tmp()
    try:
        r, calls = _run(d, _settings(max_iterations=1, lisflood_stability_retry=False))
        assert len(calls) == 1 and calls[0][1] == 1.0
        assert "lisflood_stability" not in r
    finally:
        shutil.rmtree(d, ignore_errors=True)
    d = _tmp()
    try:
        r, calls = _run(d, _settings(max_iterations=1), always_bad=True)
        assert len(calls) == 2           # tried once more, never in a loop
        ev = r["lisflood_stability"]["events"][0]
        assert ev["stable_error_pct"] == 50.0
        assert any("volume error now 50.00%" in x for x in r["warnings"])
    finally:
        shutil.rmtree(d, ignore_errors=True)
