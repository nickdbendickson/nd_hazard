"""Keep the computer awake while a flood model runs (1.8.0).

A dam-breach run takes hours (Elm River Dam 1 on a 10 m grid: 221 min), and a laptop that goes
to sleep stops the model. `keep_awake()` asks the operating system not to sleep for as long as
its `with` block lasts:

    with keepawake.keep_awake("LISFLOOD-FP", log=log):
        proc = subprocess.Popen([exe, ...])      # the solver stays the plugin's own child
        ...

It runs BESIDE the solver, never around it. The solver is still the process the plugin
started, so Cancel (proc.kill) reaches it, and a missing or broken sleep blocker can never stop
a run: the problem is logged once and the run goes on.

  Windows  SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED) on the calling thread,
           cleared when the block ends (Windows also drops it when the thread ends).
  macOS    `caffeinate -i -s -w <QGIS process id>` as a side process, stopped when the block
           ends; -w also ends it if QGIS quits. Closing a laptop's lid still puts it to sleep.
  Linux    `systemd-inhibit --what=idle:sleep` holding a small shell loop that ends with QGIS,
           started only after a test call succeeds: systemd-inhibit can be installed without a
           running systemd / login bus (containers, WSL) and then fails with "Failed to
           connect to bus". Without it nothing is done.

ND_DAM_KEEP_AWAKE=0 in the environment switches it off. `probe()` tries it once for a
self-test. Nested and parallel blocks share one
side process (a count); on Windows each thread sets its own flag.

Pure Python standard library. Not tested on Windows or macOS (Linux only: the side process,
the failed test call and the switch).
"""
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from contextlib import contextmanager

ENV_SWITCH = "ND_DAM_KEEP_AWAKE"
WHO = "ND Dam Suite"
_ES_CONTINUOUS = 0x80000000
_ES_SYSTEM_REQUIRED = 0x00000001
_NO_WINDOW = 0x08000000                 # CREATE_NO_WINDOW

_lock = threading.Lock()
_state = {"count": 0, "proc": None, "method": None, "checked": None, "warned": False}
_thread = threading.local()


def enabled():
    """False when ND_DAM_KEEP_AWAKE is 0 / false / no / off."""
    return str(os.environ.get(ENV_SWITCH, "1")).strip().lower() not in ("0", "false", "no",
                                                                       "off")


def _platform():
    if os.name == "nt":
        return "windows"
    return "macos" if sys.platform == "darwin" else "linux"


def _side_command(why):
    """(command, method, None) for the side process of this computer, or (None, None, reason)."""
    pid = str(os.getpid())
    if _platform() == "macos":
        exe = shutil.which("caffeinate") or (
            "/usr/bin/caffeinate" if os.path.isfile("/usr/bin/caffeinate") else None)
        if not exe:
            return None, None, "caffeinate not found"
        return [exe, "-i", "-s", "-w", pid], "caffeinate", None
    exe = shutil.which("systemd-inhibit")
    if not exe:
        return None, None, "systemd-inhibit not found"
    base = [exe, "--what=idle:sleep", "--who=" + WHO, "--mode=block"]
    try:
        r = subprocess.run(base + ["--why=test", "true"], stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=10)
    except (OSError, subprocess.SubprocessError) as e:
        return None, None, "systemd-inhibit test call failed ({})".format(e)
    if r.returncode != 0:
        err = (r.stderr or b"").decode("utf-8", "replace").strip().splitlines()
        return None, None, "systemd-inhibit does not work here ({})".format(
            err[-1] if err else "exit code {}".format(r.returncode))
    loop = "while kill -0 {} 2>/dev/null; do sleep 15; done".format(pid)
    return base + ["--why=" + why, "sh", "-c", loop], "systemd-inhibit", None


def _stop(proc):
    """End the side process and anything it started (its own process group)."""
    try:
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except (AttributeError, OSError):
                proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except (AttributeError, OSError):
                    proc.kill()
                proc.wait(timeout=5)
    except Exception:                                            # noqa: never raises
        pass


def _say(log, msg):
    if log:
        try:
            log(msg)
        except Exception:                                        # noqa
            pass


def _start_side(what, log):
    """Count one more user of the side process; start it for the first. Never raises."""
    with _lock:
        _state["count"] += 1
        if _state["count"] > 1 or _state["checked"] is False:
            return
        cmd, method, reason = _side_command("{} is running".format(what))
        if cmd is None:
            _state["checked"] = False
            if not _state["warned"]:
                _state["warned"] = True
                _say(log, "Could not keep the computer awake: {}. If the computer sleeps, the "
                          "run stops; change its power settings for long runs.".format(reason))
            return
        try:
            _state["proc"] = subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                                              stdout=subprocess.DEVNULL,
                                              stderr=subprocess.DEVNULL, start_new_session=True)
            _state["method"], _state["checked"] = method, True
            _say(log, "Keeping the computer awake while {} runs ({}).".format(what, method))
        except (OSError, ValueError) as e:
            _state["checked"] = False
            if not _state["warned"]:
                _state["warned"] = True
                _say(log, "Could not keep the computer awake ({}).".format(e))


def _end_side():
    with _lock:
        _state["count"] = max(0, _state["count"] - 1)
        if _state["count"] == 0 and _state["proc"] is not None:
            _stop(_state["proc"])
            _state["proc"] = None


def _windows_set(on):
    """Set or clear this thread's 'system required' flag. Returns True when it worked."""
    try:
        import ctypes
        fn = ctypes.windll.kernel32.SetThreadExecutionState
        fn.restype = ctypes.c_uint
        flags = _ES_CONTINUOUS | (_ES_SYSTEM_REQUIRED if on else 0)
        return bool(fn(ctypes.c_uint(flags)))
    except Exception:                                            # noqa
        return False


@contextmanager
def keep_awake(what="the flood model", log=None):
    """Keep the computer from sleeping while the block runs. `what` names the model in the log
    line; `log` is a callable(str) or None. Never raises because of the sleep blocker, and
    exceptions of the block pass through unchanged."""
    if not enabled():
        yield False
        return
    if _platform() == "windows":
        depth = getattr(_thread, "depth", 0)
        ok = True
        if depth == 0:
            ok = _windows_set(True)
            if ok:
                _say(log, "Keeping the computer awake while {} runs.".format(what))
            elif not _state["warned"]:
                _state["warned"] = True
                _say(log, "Could not keep the computer awake (SetThreadExecutionState failed). "
                          "If the computer sleeps, the run stops.")
        _thread.depth = depth + 1
        try:
            yield ok
        finally:
            _thread.depth -= 1
            if _thread.depth == 0 and ok:
                _windows_set(False)
        return
    _start_side(what, log)
    try:
        yield _state["proc"] is not None
    finally:
        _end_side()


def probe(wait=0.5):
    """(works, method or reason): try the sleep blocker once without touching the shared
    state, for self-tests. On Linux and macOS the side process is started, checked after
    `wait` seconds and stopped; on Windows the flag is set and cleared on this thread."""
    if not enabled():
        return False, "switched off ({}=0)".format(ENV_SWITCH)
    if _platform() == "windows":
        if getattr(_thread, "depth", 0) > 0:
            return True, "SetThreadExecutionState (in use by a run)"
        ok = _windows_set(True)
        if ok:
            _windows_set(False)
        return ok, "SetThreadExecutionState" if ok else "SetThreadExecutionState failed"
    cmd, method, reason = _side_command("the self-test is running")
    if cmd is None:
        return False, reason
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, start_new_session=True)
    except (OSError, ValueError) as e:
        return False, "{} could not start ({})".format(method, e)
    time.sleep(wait)
    alive = proc.poll() is None
    code = proc.returncode
    _stop(proc)
    if not alive:
        return False, "{} ended at once (exit code {})".format(method, code)
    return True, method


def status():
    """{"enabled", "platform", "method", "active"} for self-tests and the log."""
    with _lock:
        proc = _state["proc"]
        return {"enabled": enabled(), "platform": _platform(), "method": _state["method"],
                "active": bool(proc is not None and proc.poll() is None)}
