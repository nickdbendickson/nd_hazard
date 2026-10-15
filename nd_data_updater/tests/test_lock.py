"""The lock file: a killed run must not lock the data folder for six hours (1.4.0).

Killing QGIS in the middle of a 90-minute pack build left <data folder>/.update.lock behind and
every later run refused to start. The decision is pure logic (core.lock_decision) so it can be
tested both ways round - cleared when the process is gone, kept when it is alive."""
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
if os.path.dirname(os.path.dirname(HERE)) not in sys.path:
    sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
from nd_data_updater import core  # noqa: E402
from nd_data_updater.tests.registry import test  # noqa: E402


def _dead_pid():
    """A pid that certainly no longer runs: a child that has been waited for."""
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


@test
def test_pid_alive_knows_this_process():
    assert core.pid_alive(os.getpid()) is True
    assert core.pid_alive(_dead_pid()) is False
    assert core.pid_alive(None) is False
    assert core.pid_alive("not a pid") is False


@test
def test_read_lock_reads_the_pid_and_the_age():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, core.LOCK_NAME)
        assert core.read_lock(path) == {}
        with open(path, "w") as f:
            f.write("4242 2026-10-13T09:00:00-05:00")
        info = core.read_lock(path)
        assert info["pid"] == 4242
        assert info["age"] < 60
        with open(path, "w") as f:                 # an older version wrote no pid
            f.write("")
        assert core.read_lock(path)["pid"] is None


@test
def test_lock_decision_clears_a_lock_whose_process_is_gone():
    what, why = core.lock_decision({"pid": 4242, "age": 10.0}, alive=lambda pid: False)
    assert what == "clear"
    assert "no longer running" in why


@test
def test_lock_decision_refuses_a_live_run():
    what, why = core.lock_decision({"pid": 4242, "age": 10.0}, alive=lambda pid: True)
    assert what == "refuse"
    assert "4242" in why


@test
def test_lock_decision_clears_a_very_old_lock_even_when_the_pid_is_alive():
    # the pid may have been reused by something else entirely
    what, why = core.lock_decision({"pid": 4242, "age": 7 * 3600.0}, alive=lambda pid: True)
    assert what == "clear"
    assert "longer than an update can take" in why


@test
def test_lock_decision_handles_a_lock_with_no_pid_and_no_lock_at_all():
    assert core.lock_decision({})[0] == "free"
    assert core.lock_decision({"pid": None, "age": 5.0})[0] == "refuse"
    assert core.lock_decision({"pid": None, "age": 7 * 3600.0})[0] == "clear"
    assert core.lock_decision({"pid": os.getpid(), "age": 5.0})[0] == "clear"


@test
def test_updater_clears_a_stale_lock_and_says_so_once():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, core.LOCK_NAME)
        with open(path, "w") as f:
            f.write(str(_dead_pid()))
        said = []
        feedback = core.Feedback(log=said.append)
        updater = core.Updater(d, [], None)
        results = updater.check_and_update(feedback)
        assert results == []
        assert sum(1 for line in said if "Cleared the left-over lock" in line) == 1
        assert not os.path.exists(path)            # and the run removed its own lock


@test
def test_updater_refuses_while_another_run_holds_the_lock():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, core.LOCK_NAME)
        with open(path, "w") as f:
            f.write(str(os.getppid() or os.getpid()))
        updater = core.Updater(d, [], None)
        try:
            updater.check_and_update(core.Feedback())
        except core.UpdateError as e:
            assert "already running" in str(e)
            assert core.LOCK_NAME in str(e)        # and it names the file to delete
        else:
            raise AssertionError("a live lock must stop the run")
        assert os.path.exists(path)                # the other run's lock is left alone
