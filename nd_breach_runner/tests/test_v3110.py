"""Tests of 3.11.0: the bundled LISFLOOD-FP and the built-in flood engine."""
import os
import shutil
import sys
import tempfile

import numpy as np

from nd_breach_runner.core import lisflood as lf, nd_data
from nd_breach_runner.tests.registry import test


@test
def v3110_builtin_words_and_resolution():
    for w in ("builtin", "Built-In", " internal "):
        assert lf.resolve_exe(w) == lf.BUILTIN_EXE and lf.is_builtin(lf.resolve_exe(w))
    assert lf.resolve_exe("/x/lisflood") == "/x/lisflood"
    assert os.path.isfile(lf.BUILTIN_EXE)
    assert "built-in" in lf.describe_exe(lf.BUILTIN_EXE)


@test
def v3110_detect_falls_back_to_the_builtin_engine():
    only_builtin = lambda p: p == lf.BUILTIN_EXE
    assert nd_data.detect_lisflood(check=only_builtin, fallback=True) == lf.BUILTIN_EXE
    assert nd_data.detect_lisflood(check=only_builtin, fallback=False) == ""


@test
def v3110_bundled_files_are_complete():
    b = lf.BUNDLED_DIR
    for p in ("windows-x64/lisflood.exe", "linux-x64/lisflood", "COPYING-GPL-2.txt", "README.txt",
              "source/LISFLOOD-FP-8.2-nd-breach-patched-source.zip"):
        assert os.path.getsize(os.path.join(b, p)) > 1000, p
    with open(os.path.join(b, "windows-x64", "lisflood.exe"), "rb") as f:
        assert f.read(2) == b"MZ"
    with open(os.path.join(b, "linux-x64", "lisflood"), "rb") as f:
        assert f.read(4) == b"\x7fELF"


@test
def v3110_check_runs_with_the_builtin_engine_and_conserves_volume():
    tmp = tempfile.mkdtemp(prefix="v3110_")
    try:
        r = lf.check_lisflood(lf.BUILTIN_EXE, os.path.join(tmp, "b"))
        assert r["ok"] and r["patched"], r
        assert "built-in" in r["message"]
        masses = np.loadtxt(os.path.join(tmp, "b", "results", "run.mass"), skiprows=1)
        assert abs(masses[-1, 9]) < 1.0 or abs(masses[-1, 9]) < 0.01 * masses[-1, 4], masses[-1]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@test
def v3110_check_runs_with_the_bundled_program_when_it_runs_here():
    exe = lf.bundled_exe()
    if not exe:
        return
    tmp = tempfile.mkdtemp(prefix="v3110_")
    try:
        r = lf.check_lisflood(exe, os.path.join(tmp, "b"))
        assert r["ok"] and r["patched"], r
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@test
def v3110_selftest_check_accepts_the_builtin_engine():
    from nd_breach_runner.core import selftest
    tmp = tempfile.mkdtemp(prefix="v3110_")
    try:
        msg = selftest.check_lisflood_exe(lf.BUILTIN_EXE, tmp)
        assert "built-in" in msg, msg
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
