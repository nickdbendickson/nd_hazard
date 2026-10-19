# -*- coding: utf-8 -*-
"""Clicking "Classify" while a run is in progress must be a no-op (Finding 13).

_classify_simple used to call _auto_fill_rasters(force=True) - which mutates the
raster edit boxes - before delegating to _on_run_all, with no busy guard, while
the Classify button stays enabled during a run. The busy guard belongs at the
TOP of _classify_simple so a mid-run click never touches the raster widgets.

Real PyQGIS is not importable in this sandbox, so the dialog module is loaded
against the fake_qgis stand-ins and the real _classify_simple function is driven
against a tiny stub "self" that records what it called. No Qt event loop needed.

    python3 ND_Dam_Hazard_Helper/tests/test_classify_busy_guard.py
"""
import os
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))           # folder holding ND_Dam_Hazard_Helper
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def _classify_simple():
    """The real NDDamHazardDialog._classify_simple function (or None to skip)."""
    try:
        from ND_Dam_Hazard_Helper.tests import fake_qgis
        fake_qgis.install()                      # real PyQGIS if present, else stand-ins
        from ND_Dam_Hazard_Helper.dialog import NDDamHazardDialog
    except Exception:                                            # noqa
        return None
    return NDDamHazardDialog.__dict__["_classify_simple"]


class _Edit(object):
    def __init__(self, text=""):
        self._t = text

    def text(self):
        return self._t


class _Stub(object):
    """Just the attributes _classify_simple touches; records the delegates."""

    def __init__(self, busy, depth=""):
        self._busy = busy
        self.depth_edit = _Edit(depth)
        self.calls = []

    def _auto_fill_rasters(self, force=False):
        self.calls.append(("auto_fill", force))

    def _on_run_all(self, *a):
        self.calls.append(("run_all",))


def test_busy_click_is_a_noop():
    """Busy + empty depth box: neither the raster-finding mutation nor the run
    may fire. This is the regression - it failed before the guard was added."""
    fn = _classify_simple()
    if fn is None:
        print("    SKIP test_busy_click_is_a_noop: dialog not importable")
        return
    stub = _Stub(busy=True, depth="")
    fn(stub)
    assert stub.calls == [], (
        "Classify while busy must not run; it called {}".format(stub.calls))


def test_idle_click_runs():
    """Not busy + empty depth box: it finds the rasters (force=True) then runs -
    the original behaviour is unchanged when idle."""
    fn = _classify_simple()
    if fn is None:
        print("    SKIP test_idle_click_runs: dialog not importable")
        return
    stub = _Stub(busy=False, depth="")
    fn(stub)
    assert stub.calls == [("auto_fill", True), ("run_all",)], stub.calls


def test_idle_with_depth_skips_autofill():
    """Not busy but the depth box is already filled: no raster-finding, just run."""
    fn = _classify_simple()
    if fn is None:
        print("    SKIP test_idle_with_depth_skips_autofill: dialog not importable")
        return
    stub = _Stub(busy=False, depth="C:/depth.tif")
    fn(stub)
    assert stub.calls == [("run_all",)], stub.calls


if __name__ == "__main__":
    n = fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            n += 1
            try:
                fn()
                print("ok    ", name)
            except Exception:
                fails += 1
                print("FAIL  ", name)
                traceback.print_exc()
    print("\n{} tests, {} failed".format(n, fails))
    sys.exit(1 if fails else 0)
