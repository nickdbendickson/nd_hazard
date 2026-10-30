"""2.35.1 (full test 2026-10-30): the report and map book legends keep their own layer list on
QGIS 4. report.py / mapbook.py called QgsLayoutItemLegend.setAutoUpdateModel(False), which
QGIS 4 deprecates (setSyncMode); compat.legend_manual uses setSyncMode(Manual) where it exists
and setAutoUpdateModel(False) on QGIS 3. Runs tests/realqt_legend_sync.py (real QGIS,
offscreen) in its own process; skipped without a real PyQGIS.

    python3 -m ND_Dam_Hazard_Helper.tests.run_tests legend
"""
import os
import subprocess
import sys
import tempfile

from ND_Dam_Hazard_Helper.tests.registry import Skip, test

HERE = os.path.dirname(os.path.abspath(__file__))


@test
def legend_keeps_its_own_layers_without_deprecation():
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen",
               HOME=tempfile.mkdtemp(prefix="legend_home_"))
    env.pop("NDSUITE_SETTINGS", None)
    r = subprocess.run([sys.executable, os.path.join(HERE, "realqt_legend_sync.py")], env=env,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=300)
    out = r.stdout.decode("utf-8", "replace")
    if "SKIP: no real QGIS" in out:
        raise Skip("needs a real PyQGIS")
    assert r.returncode == 0 and "all checks passed" in out, out[-3000:]
