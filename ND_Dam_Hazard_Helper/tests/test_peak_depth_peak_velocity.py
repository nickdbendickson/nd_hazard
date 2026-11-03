"""Hazard Helper 2.35.2 (Nick via Docs & QA, 2026-11-01, option C): "TM 11 practice uses the
absolute peak velocity and peak depth regardless of whether they coincide." A structure is
classified on its peak depth AND the peak velocity over its wet cells, even when they come
from different cells (structures.peak_pair). Synthetic cells only (no DWR data, no rasters).

    python3 -m ND_Dam_Hazard_Helper.tests.run_tests peak_pair
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_DIR = os.path.dirname(HERE)
if os.path.dirname(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, os.path.dirname(PLUGIN_DIR))
from ND_Dam_Hazard_Helper.tests.registry import test  # noqa: E402

D = (5.0, 1.0, 0.0)          # deepest cell slow, a shallower wet cell fast, a dry cell fastest
V = (0.5, 9.0, 15.0)


def _mods():
    import numpy as np
    from ND_Dam_Hazard_Helper import structures as ST
    from ND_Dam_Hazard_Helper.constants import CLASS_HIGH
    from ND_Dam_Hazard_Helper.helpers import classify_values, curve_pair
    return np, ST, CLASS_HIGH, classify_values, curve_pair("found")


@test
def peak_pair_is_the_deepest_depth_with_the_fastest_wet_velocity():
    np, ST, _H, _cv, _c = _mods()
    dpk, vpk = ST.peak_pair(np.array(D), np.array(V), True)
    assert (float(dpk[0]), float(vpk[0])) == (5.0, 9.0), (dpk, vpk)


@test
def peak_pair_from_different_cells_is_high_when_no_single_cell_is():
    np, ST, CLASS_HIGH, classify_values, cv = _mods()
    d, v = np.array(D), np.array(V)
    dpk, vpk = ST.peak_pair(d, v, True)
    per_cell = int(classify_values(d, v, *cv).min())
    peak = int(classify_values(dpk, vpk, *cv)[0])
    assert peak <= per_cell and peak == CLASS_HIGH and per_cell != CLASS_HIGH, (peak, per_cell)


@test
def peak_pair_without_velocity_or_with_nan_velocity():
    np, ST, _H, _cv, _c = _mods()
    assert float(ST.peak_pair(np.array(D), np.array(V), False)[1][0]) == 0.0
    assert float(ST.peak_pair(np.array([2.0, 1.0]), np.array([np.nan, 3.0]), True)[1][0]) == 3.0


@test
def peak_pair_method_label_names_peak_depth_with_peak_velocity():
    from ND_Dam_Hazard_Helper import hazard as HZ
    assert "peak depth with peak velocity" in HZ.METHOD_LABEL
    assert "same cell" not in HZ.METHOD_LABEL
