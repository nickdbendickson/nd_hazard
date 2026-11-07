"""sensitivity._scale_structures must classify a scaled structure on the SAME peak pair
(peak depth, peak velocity over the wet cells) that evaluate_structures/peak_pair uses for the
unscaled base row - not the pre-2.22.3 per-cell minimum. This checks the vectorised reduceat
peak-velocity logic reproduces peak_pair exactly, structure by structure, including the case a
dry (d==0) cell carries the highest velocity (where a plain fmax over all cells would differ),
and that the combined zone equals min(zone(peak pair), per-cell minimum). Pure Python; no QGIS.

    python3 ND_Dam_Hazard_Helper/tests/test_sensitivity_peak_pair.py
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "ND_Dam_Hazard_Helper"))

import numpy as np  # noqa: E402
from ND_Dam_Hazard_Helper.structures import peak_pair, classify_values, zone_margins  # noqa: E402
try:
    from ND_Dam_Hazard_Helper.sensitivity import curve_pair
except ImportError:
    from ND_Dam_Hazard_Helper.structures import curve_pair  # noqa: E402


def _vectorised(D, V, starts, has_v_arr, cv):
    """The exact block sensitivity._scale_structures now uses, lifted for the test."""
    maxd = np.maximum.reduceat(D, starts)
    neg = -np.inf
    vwet = np.maximum.reduceat(np.where((D > 0) & np.isfinite(V), V, neg), starts)
    vfin = np.maximum.reduceat(np.where(np.isfinite(V), V, neg), starts)
    vpk = np.where(vwet > neg, vwet, np.where(vfin > neg, vfin, 0.0))
    vpk = np.where(has_v_arr, vpk, 0.0)
    percell_min = np.minimum.reduceat(classify_values(D, V, *cv), starts)
    zpk = classify_values(maxd, vpk, *cv)
    mh, mj = zone_margins(maxd, vpk, *cv)
    return np.minimum(zpk, percell_min), mh, mj, maxd, vpk, percell_min


def test_scaled_peak_pair_matches_peak_pair_oracle():
    cv = curve_pair("found")
    rng = np.random.default_rng(1)
    structs = []
    # a spread of shapes, plus two crafted edge cases
    for _ in range(40):
        n = int(rng.integers(1, 6))
        d = rng.uniform(0.0, 18.0, n).astype(np.float64)
        has_v = bool(rng.integers(0, 2))
        v = rng.uniform(0.0, 14.0, n).astype(np.float64) if has_v else None
        structs.append((d, v))
    # dry cell carries the highest velocity: peak_pair must EXCLUDE it (wet = d>0), a plain
    # fmax over all cells would wrongly pick it up.
    structs.append((np.array([0.0, 4.5]), np.array([13.0, 2.0])))
    # deep and slow, no velocity raster -> depth-only (vpk 0)
    structs.append((np.array([14.0, 2.0]), None))

    D = np.concatenate([d for d, _ in structs]).astype(np.float64)
    lens = [d.size for d, _ in structs]
    starts = np.concatenate(([0], np.cumsum(lens)[:-1])).astype(np.intp)
    has_v_arr = np.array([v is not None for _, v in structs], dtype=bool)
    V = np.concatenate([(v if v is not None else np.full(d.shape, np.nan))
                        for d, v in structs]).astype(np.float64)

    zone, mh, mj, maxd, vpk, percell_min = _vectorised(D, V, starts, has_v_arr, cv)

    saw_dry_high = False
    for k, (d, v) in enumerate(structs):
        vel_ok = v is not None
        dpk_o, vpk_o = peak_pair(d, v if v is not None else np.zeros(0), vel_ok)
        # peak depth and peak velocity reproduced exactly by the vectorised reduceat
        assert maxd[k] == dpk_o[0], (k, maxd[k], dpk_o[0])
        assert vpk[k] == vpk_o[0], (k, vpk[k], vpk_o[0])
        # combined zone == min(zone(peak pair from the oracle), the code's per-cell minimum)
        zpk_o = int(classify_values(dpk_o, vpk_o, *cv)[0])
        assert int(zone[k]) == min(zpk_o, int(percell_min[k])), (k, int(zone[k]), zpk_o)
        if vel_ok:
            mh_o, mj_o = zone_margins(dpk_o, vpk_o, *cv)
            assert abs(mh[k] - float(mh_o[0])) < 1e-9 and abs(mj[k] - float(mj_o[0])) < 1e-9
        # confirm the dry-cell-high-velocity case is actually exercised: peak velocity is the
        # 2.0 wet-cell value, not the 13.0 on the dry cell
        if vel_ok and d.size == 2 and d[0] == 0.0 and v is not None and v[0] == 13.0:
            assert vpk[k] == 2.0 and float(np.fmax.reduceat(V, starts)[k]) == 13.0
            saw_dry_high = True
    assert saw_dry_high, "dry-cell-high-velocity case not exercised"


if __name__ == "__main__":
    test_scaled_peak_pair_matches_peak_pair_oracle()
    print("ok test_sensitivity_peak_pair")
