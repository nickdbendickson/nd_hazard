"""Hazard Helper 2.35.2 (Nick via Docs & QA, 2026-11-01, option C): "TM 11 practice uses the absolute
peak velocity and peak depth regardless of whether they coincide." A structure is classified on
its peak depth AND the peak velocity over its wet cells, even when they come from different
cells (structures.peak_pair). Synthetic cells only (no DWR data, no rasters).

    QT_QPA_PLATFORM=offscreen python3 ND_Dam_Hazard_Helper/tests/test_peak_depth_peak_velocity.py
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qgis.core import QgsApplication  # noqa: E402
_app = QgsApplication.instance() or QgsApplication([], True)
_app.setPrefixPath(os.environ.get("QGIS_PREFIX_PATH", "/home/box/miniforge3/envs/qgis334"), True)
_app.initQgis()

import numpy as np  # noqa: E402

from ND_Dam_Hazard_Helper import hazard as HZ  # noqa: E402
from ND_Dam_Hazard_Helper import structures as ST  # noqa: E402
from ND_Dam_Hazard_Helper.constants import CLASS_HIGH  # noqa: E402
from ND_Dam_Hazard_Helper.helpers import classify_values, curve_pair  # noqa: E402

FAILS = []


def check(name, ok, info=""):
    print("{} {}{}".format("ok  " if ok else "FAIL", name, "" if ok else "  -> {}".format(info)))
    if not ok:
        FAILS.append(name)


cv = curve_pair("found")
# deepest cell slow, a shallower wet cell fast, a dry cell fastest
d = np.array([5.0, 1.0, 0.0])
v = np.array([0.5, 9.0, 15.0])
dpk, vpk = ST.peak_pair(d, v, True)
check("peak pair: deepest depth, fastest WET velocity (dry cell ignored)",
      (float(dpk[0]), float(vpk[0])) == (5.0, 9.0), (dpk, vpk))
per_cell = int(classify_values(d, v, *cv).min())
peak = int(classify_values(dpk, vpk, *cv)[0])
check("peak pair is at least as severe as every cell", peak <= per_cell, (peak, per_cell))
check("5 ft with 9 ft/s from different cells is High (each cell alone is not)",
      peak == CLASS_HIGH and per_cell != CLASS_HIGH, (peak, per_cell))
dpk, vpk = ST.peak_pair(d, v, False)
check("no velocity raster: depth alone", float(vpk[0]) == 0.0)
check("NaN velocity is skipped", float(ST.peak_pair(np.array([2.0, 1.0]),
                                                    np.array([np.nan, 3.0]), True)[1][0]) == 3.0)
check("method label says peak depth with peak velocity",
      "peak depth with peak velocity" in HZ.METHOD_LABEL and "same cell" not in HZ.METHOD_LABEL)

print("\n{} failure(s)".format(len(FAILS)) if FAILS else "\nall checks passed")
sys.exit(1 if FAILS else 0)
