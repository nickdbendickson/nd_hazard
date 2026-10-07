"""Tests of 3.12.0: keeping the computer awake beside the solver (damsuite.keepawake) and the
near-dam refine without an FV1 velocity (LISFLOOD-FP 8.1 writes no .maxVc for FV1)."""
import importlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time

import numpy as np

from nd_breach_runner.core.units import FT
from nd_breach_runner.tests.registry import test


def _script(folder, name, body):
    p = os.path.join(folder, name)
    with open(p, "w") as f:
        f.write("#!/bin/sh\n" + body + "\n")
    os.chmod(p, os.stat(p).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return p


class _Path:
    """Put `folder` first on PATH (and set the switch) for the block."""

    def __init__(self, folder, switch=None):
        self.folder, self.switch = folder, switch

    def __enter__(self):
        self.old = os.environ.get("PATH", ""), os.environ.get("ND_DAM_KEEP_AWAKE")
        os.environ["PATH"] = self.folder + os.pathsep + self.old[0]
        if self.switch is not None:
            os.environ["ND_DAM_KEEP_AWAKE"] = self.switch
        else:
            os.environ.pop("ND_DAM_KEEP_AWAKE", None)

    def __exit__(self, *a):
        os.environ["PATH"] = self.old[0]
        if self.old[1] is None:
            os.environ.pop("ND_DAM_KEEP_AWAKE", None)
        else:
            os.environ["ND_DAM_KEEP_AWAKE"] = self.old[1]


def _fresh():
    from nd_breach_runner.damsuite import keepawake
    return importlib.reload(keepawake)


def _posix():
    return os.name != "nt" and sys.platform != "darwin"


@test
def v3120_keepawake_runs_beside_the_solver_and_is_shared():
    if not _posix():
        return
    d = tempfile.mkdtemp(prefix="v3120_")
    try:
        # a working systemd-inhibit: skips its --options and runs the command it is given
        _script(d, "systemd-inhibit", 'while [ $# -gt 0 ]; do case "$1" in --*) shift;; '
                                      '*) break;; esac; done\nexec "$@"')
        ka = _fresh()
        logs = []
        with _Path(d):
            with ka.keep_awake("LISFLOOD-FP", log=logs.append) as ok:
                assert ok and ka.status()["active"] and ka.status()["method"] == "systemd-inhibit"
                pid = ka._state["proc"].pid
                with ka.keep_awake("ANUGA", log=logs.append):
                    assert ka._state["proc"].pid == pid          # one side process for both
                assert ka.status()["active"]                     # still held by the outer block
            assert not ka.status()["active"] and ka._state["proc"] is None
            assert logs == ["Keeping the computer awake while LISFLOOD-FP runs "
                            "(systemd-inhibit)."], logs
            # an error inside the block passes through and the side process still stops
            try:
                with ka.keep_awake("x"):
                    raise KeyError("boom")
            except KeyError:
                pass
            else:
                raise AssertionError("the block's exception was swallowed")
            assert ka._state["count"] == 0 and ka._state["proc"] is None
        # no loop left behind waiting for this process
        ps = subprocess.run(["ps", "-eo", "args"], capture_output=True, text=True).stdout
        assert "kill -0 {} ".format(os.getpid()) not in ps
    finally:
        shutil.rmtree(d, ignore_errors=True)
        _fresh()


@test
def v3120_keepawake_without_a_session_bus_logs_once_and_the_run_goes_on():
    """Grok's wrapper ran the solver THROUGH systemd-inhibit, so on a Linux machine without a
    systemd bus every flood run failed ("Failed to connect to bus"). Here the run goes on."""
    if not _posix():
        return
    d = tempfile.mkdtemp(prefix="v3120_")
    try:
        _script(d, "systemd-inhibit", 'echo "Failed to connect to bus: No such file or '
                                      'directory" >&2\nexit 1')
        ka = _fresh()
        logs = []
        with _Path(d):
            ran = []
            for _ in range(3):
                with ka.keep_awake("LISFLOOD-FP", log=logs.append) as ok:
                    ran.append(ok)
        assert ran == [False, False, False]
        assert len(logs) == 1 and "Failed to connect to bus" in logs[0], logs
        assert "the run stops" in logs[0]
        # switched off: nothing is tried, nothing is logged
        ka = _fresh()
        logs = []
        with _Path(d, switch="0"):
            with ka.keep_awake("LISFLOOD-FP", log=logs.append) as ok:
                assert ok is False
        assert logs == [] and ka._state["checked"] is None
    finally:
        shutil.rmtree(d, ignore_errors=True)
        _fresh()


@test
def v3120_cancel_still_kills_lisflood_while_the_computer_is_kept_awake():
    if not _posix():
        return
    from nd_breach_runner.core import lisflood as lf
    d = tempfile.mkdtemp(prefix="v3120_")
    try:
        _script(d, "systemd-inhibit", 'while [ $# -gt 0 ]; do case "$1" in --*) shift;; '
                                      '*) break;; esac; done\nexec "$@"')
        exe = _script(d, "fake_lisflood", "echo $$ > solver.pid\nexec sleep 120")
        wd = os.path.join(d, "run")
        os.makedirs(os.path.join(wd, "results"))

        class St:
            pass
        st = St()
        st.exe, st.threads, st.sim_time_s = exe, 1, 3600.0
        t0 = time.time()
        _fresh()
        with _Path(d):
            try:
                lf.run(wd, os.path.join(wd, "run.par"), st,
                       cancel=lambda: time.time() - t0 > 1.5)
            except lf.Cancelled:
                pass
            else:
                raise AssertionError("not cancelled")
        assert time.time() - t0 < 20
        pid = int(open(os.path.join(wd, "solver.pid")).read())
        try:
            os.kill(pid, 0)
            alive = True
        except OSError:
            alive = False
        assert not alive, "the solver kept running after Cancel"
        from nd_breach_runner.damsuite import keepawake
        assert keepawake._state["count"] == 0 and keepawake._state["proc"] is None
    finally:
        shutil.rmtree(d, ignore_errors=True)
        _fresh()


@test
def v3120_damsuite_lists_keepawake_and_is_intact():
    from nd_breach_runner import damsuite
    assert damsuite.SUITE_VERSION == "1.8.0" and "keepawake" in damsuite.__all__
    assert "keepawake.py" in json.load(open(os.path.join(os.path.dirname(damsuite.__file__),
                                                         "FILES.json")))
    assert damsuite.selfcheck() == []


@test
def v3120_refine_is_off_by_default_in_the_run_tools():
    from nd_breach_runner.tests import fake_qgis
    fake_qgis.install()
    import nd_breach_runner.algorithms as algs
    importlib.reload(algs)
    for cls in (algs.RunDamAlgorithm, algs.BatchAlgorithm):
        a = cls()
        a.initAlgorithm()
        p = a.parameterDefinition("REFINE_NEAR")
        assert p.kwargs["defaultValue"] is False and "off by default" in p.description


@test
def v3120_refine_keeps_the_acc_velocity_when_fv1_wrote_none():
    """LISFLOOD-FP 8.1 writes no .maxVc for FV1: the fine box used to get 0 ft/s in every wet
    cell (Eddy County: DWR-4664 24.74 -> 5.54 ft/s). Now depth comes from the fine run and the
    ACC velocity stays."""
    from nd_breach_runner.core import workflow, impacts
    from nd_breach_runner.core import lisflood as lf
    from nd_breach_runner.tests.synthetic import SyntheticTerrain, npy_writer
    from nd_breach_runner.tests.test_v340 import _settings, _dam, fake_models, _tmp
    d = _tmp()
    saved = lf.read_results
    try:
        rec = impacts.Receptors()
        rec.add_points([[800.0, 0.0], [5000.0, 0.0]])
        calls, logs = [], []
        st = _settings(receptors=rec, refine_near=True, max_iterations=1)
        with fake_models(calls=calls, vary=lambda wd: 1.0):
            orig = lf.read_results                    # the stub's reader

            def read(wd, dem, stt, hy, name="run"):
                env = orig(wd, dem, stt, hy, name)
                if "refine_near_dam" in wd:           # what LISFLOOD-FP 8.1 FV1 gives
                    env.velocity.data[:] = np.where(np.isfinite(env.velocity.data), 0.0, np.nan)
                    env.stats["velocity_written"] = False
                    env.depth.data[:] = np.where(np.isfinite(env.depth.data),
                                                 env.depth.data + 0.5, np.nan)
                return env
            lf.read_results = read
            r = workflow.run_dam(_dam(), (0.0, 0.0), st, SyntheticTerrain(), npy_writer,
                                 os.path.join(d, "T1"), log=logs.append)
        info = json.load(open(r.report_json))["refine_near_dam"]
        assert info["applied"] and info["velocity_kept_acc"] is True
        assert "ACC velocities are kept near the dam" in info["warning"]
        assert info["summary"].startswith("Depths (ACC velocities kept: FV1 wrote none) within")
        assert any("ACC velocities are kept near the dam" in m for m in logs), logs
        vel = np.load(os.path.splitext(r.final["velocity"])[0] + ".npy")
        dep = np.load(os.path.splitext(r.final["depth"])[0] + ".npy")
        g = r.grid
        X, Y = np.meshgrid(g.x_centers(), g.y_centers())
        near = (X > 100) & (X < 1500) & (np.abs(Y) < 200)
        assert abs(np.nanmax(vel[near]) - 1.2 / FT) < 0.01, np.nanmax(vel[near])
        assert np.nanmin(vel[near & np.isfinite(dep) & (dep > 0.1)]) > 0.0   # never zeroed
        assert info["zone_after_fv1"]["max_depth_ft"] > info["zone_before_acc"]["max_depth_ft"]
    finally:
        lf.read_results = saved
        shutil.rmtree(d, ignore_errors=True)


@test
def v3120_merge_without_velocity_keeps_the_main_velocity():
    from nd_breach_runner.core import refine
    from nd_breach_runner.tests.test_v340 import _grid, _band
    main = _grid(40, 20, 10.0)
    fine = _grid(40, 20, 2.5, x0=50.0, y0=25.0)
    ma = _band(main, 1.0, 60.0)
    fa = _band(fine, 2.0, 60.0, x_from=-1.0)
    fa["velocity"][:] = np.where(np.isfinite(fa["depth"]), 0.0, np.nan)
    keys = tuple(k for k in refine.MERGE_KEYS if k != "velocity")
    out, n = refine._merge_keep(main, ma, fine, fa, (120.0, 0.0), (1.0, 0.0), keys)
    x, y = main.x_centers(), main.y_centers()
    inside = (x[None, :] > 50) & (x[None, :] < 120) & (np.abs(y[:, None]) < 25)
    assert np.all(out["depth"][inside] == 2.0) and np.all(out["velocity"][inside] == 1.0)
    assert set(out) == set(refine.MERGE_KEYS) and n == 42
    assert out["velocity"] is not ma["velocity"]                  # a copy, not the main array


@test
def v3120_keepawake_macos_uses_caffeinate_waiting_on_qgis():
    """macOS is not available here: the platform is forced and caffeinate is a stand-in that
    records its arguments and waits, as caffeinate -w does."""
    if os.name == "nt":
        return
    d = tempfile.mkdtemp(prefix="v3120_")
    try:
        args = os.path.join(d, "args.txt")
        _script(d, "caffeinate", 'echo "$@" > "{}"\nexec sleep 120'.format(args))
        ka = _fresh()
        ka._platform = lambda: "macos"
        logs = []
        with _Path(d):
            with ka.keep_awake("LISFLOOD-FP", log=logs.append) as ok:
                assert ok and ka.status()["method"] == "caffeinate"
                proc = ka._state["proc"]
                for _ in range(50):
                    if os.path.exists(args):
                        break
                    time.sleep(0.1)
        assert open(args).read().split() == ["-i", "-s", "-w", str(os.getpid())]
        assert proc.poll() is not None and ka._state["proc"] is None
        assert logs == ["Keeping the computer awake while LISFLOOD-FP runs (caffeinate)."], logs
    finally:
        shutil.rmtree(d, ignore_errors=True)
        _fresh()


@test
def v3120_keepawake_windows_sets_and_clears_the_thread_flag_once_per_thread():
    """Windows is not available here: the platform is forced and SetThreadExecutionState is
    replaced by a recorder. Nested blocks set the flag once; each thread sets its own."""
    import threading
    ka = _fresh()
    try:
        ka._platform = lambda: "windows"
        calls = []
        ka._windows_set = lambda on: calls.append((threading.get_ident(), on)) or True
        logs = []
        with _Path(os.getcwd()):
            with ka.keep_awake("ANUGA", log=logs.append) as ok:
                assert ok
                with ka.keep_awake("LISFLOOD-FP", log=logs.append):
                    pass
                assert calls == [(threading.get_ident(), True)]
                seen = []

                def other():
                    with ka.keep_awake("built-in engine"):
                        seen.append(True)
                t = threading.Thread(target=other)
                t.start()
                t.join()
                assert seen == [True]
            me = threading.get_ident()
            assert [c for c in calls if c[0] == me] == [(me, True), (me, False)], calls
            assert len(calls) == 4 and ka._state["proc"] is None
            assert logs == ["Keeping the computer awake while ANUGA runs."], logs
            # a failed call: logged once, the run goes on, nothing to clear
            calls[:] = []
            ka._windows_set = lambda on: (calls.append(on), False)[1]
            logs = []
            for _ in range(2):
                with ka.keep_awake("ANUGA", log=logs.append) as ok:
                    assert ok is False
            assert calls == [True, True] and len(logs) == 1 and "the run stops" in logs[0]
    finally:
        _fresh()


@test
def v3120_selftest_says_whether_the_computer_can_be_kept_awake():
    """probe() tries the blocker once and leaves the shared state alone, so a self-test that
    finds it broken does not silence the warning in a later run's log."""
    if not _posix():
        return
    from nd_breach_runner.core import selftest
    d = tempfile.mkdtemp(prefix="v3120_")
    try:
        good = os.path.join(d, "good")
        bad = os.path.join(d, "bad")
        os.makedirs(good)
        os.makedirs(bad)
        _script(good, "systemd-inhibit", 'while [ $# -gt 0 ]; do case "$1" in --*) shift;; '
                                         '*) break;; esac; done\nexec "$@"')
        _script(bad, "systemd-inhibit", 'echo "Failed to connect to bus" >&2\nexit 1')
        ka = _fresh()
        with _Path(good):
            assert ka.probe(wait=0.2) == (True, "systemd-inhibit")
            assert "systemd-inhibit" in selftest.check_keep_awake()
        with _Path(bad):
            ok, why = ka.probe()
            assert not ok and "Failed to connect to bus" in why
            try:
                selftest.check_keep_awake()
            except selftest.Skip as e:
                assert "Runs still work" in str(e)
            else:
                raise AssertionError("no Skip without a working blocker")
            assert ka._state == {"count": 0, "proc": None, "method": None, "checked": None,
                                 "warned": False}
        with _Path(good, switch="0"):
            assert ka.probe() == (False, "switched off (ND_DAM_KEEP_AWAKE=0)")
        r = selftest.Report()
        selftest.run_all(r, os.path.join(d, "w"), run_model=False,
                         before=(), after=())
        assert r.status_of("Keep the computer awake during runs") in (selftest.PASS,
                                                                      selftest.SKIP)
    finally:
        shutil.rmtree(d, ignore_errors=True)
        _fresh()
