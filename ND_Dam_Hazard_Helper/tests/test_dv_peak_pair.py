"""2.35.5 (audit 2026-11-03, option C): DV is the peak depth x the peak wet-cell velocity, never a
DV raster's value; None when the velocity is unknown at every wet cell. Runs
tests/realqt_dv_peak_pair.py (real QGIS, offscreen) in its own process; skipped without a real
PyQGIS.

    python3 -m ND_Dam_Hazard_Helper.tests.run_tests dv_peak
"""
import os
import subprocess
import sys
import tempfile

from ND_Dam_Hazard_Helper.tests.registry import Skip, test

HERE = os.path.dirname(os.path.abspath(__file__))


@test
def dv_peak_pair_dv_is_peak_depth_times_peak_wet_velocity_and_none_when_unknown():
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen",
               HOME=tempfile.mkdtemp(prefix="dvpeak_home_"))
    env.pop("NDSUITE_SETTINGS", None)
    r = subprocess.run([sys.executable, os.path.join(HERE, "realqt_dv_peak_pair.py")], env=env,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=300)
    out = r.stdout.decode("utf-8", "replace")
    if "SKIP: no real QGIS" in out:
        raise Skip("needs a real PyQGIS")
    assert r.returncode == 0 and "ALL OK" in out, out[-3000:]
