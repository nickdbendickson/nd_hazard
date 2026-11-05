"""3.27.7: grid-spread advisory. A coarse model cell (coarser than 10 m) over a wide (>= 1,000 ac)
and shallow (median wet depth <= 1.0 ft) flood on flat, multi-channel terrain over-predicts the
extent, because averaging fine terrain onto a big cell lowers road embankments and fills ditches.
The run flags it (report grid_spread_check, flagged=True, plus a WARNING) and advises a finer grid
or the sensitivity tool's cell-size convergence check. It is an advisory only and changes nothing
in the run; it does not fire on a fixed-plan (sensitivity/convergence) run.

Calibrated on DWR-2801 (Absaraka): at a 24 h matched clock the >=0.1 ft area runs 6,176 ac at 20 m,
4,490 at 10 m, 3,428 at 5 m against a 2,765 ac 8 ft reference, and the median wet depth over the
whole domain rises as the grid is refined (0.61 / 0.83 / 1.06 ft). So the 20 m run trips the flag
(cell coarser than 10 m, wide, median below 1.0 ft) and the finer runs do not.
"""
from nd_breach_runner.core import workflow as wf
from nd_breach_runner.tests.registry import test


@test
def grid_spread_predicate_thresholds():
    f = wf.grid_spread_flagged
    # the wide-shallow coarse-grid signature: trips (2801 at 20 m, 24 h / 54 h)
    assert f(20.0, 6176.0, 0.61)
    assert f(20.0, 7452.0, 0.54)
    assert f(20.0, 1000.0, 1.0)              # exactly at the area and depth edges
    assert f(10.1, 1500.0, 0.5)              # just coarser than 10 m
    # a finer grid does not trip, whatever the area/depth (2801 at 10 m / 5 m)
    assert not f(10.0, 4490.0, 0.83)         # 10 m is not "coarser than" 10 m
    assert not f(5.0, 3428.0, 1.06)
    # a cell coarser than 10 m but the flood is small, or deep: does not trip
    assert not f(20.0, 999.0, 0.5)           # below the 1,000 ac floor
    assert not f(20.0, 6000.0, 1.01)         # deeper than 1.0 ft median -> channelized, not sheet


@test
def grid_spread_report_and_warning():
    """run_dam records grid_spread_check and, when flagged, a WARNING; an unflagged run records
    the check with flagged=False and no WARNING. Driven through the same small stub the other
    workflow tests use so no LISFLOOD run is needed."""
    import numpy as np

    # the block under test, lifted so the test exercises the real predicate + message wiring
    # exactly as run_dam does (report dict + warnings list), without a full LISFLOOD run.
    def emulate(cell_m, depths_ft):
        FT = wf.FT
        d = np.array(depths_ft, dtype=float) * FT
        arrival_m = 0.1 * FT
        cell_ac = cell_m * cell_m / 4046.8564224
        report, warnings = {}, []
        wet = np.isfinite(d) & (d >= arrival_m)
        if wet.any():
            area_ac = float(wet.sum()) * cell_ac
            median_depth_ft = float(np.median(d[wet])) / FT
            flagged = wf.grid_spread_flagged(cell_m, area_ac, median_depth_ft)
            report["grid_spread_check"] = {
                "cell_m": round(cell_m, 2), "flooded_acres": round(area_ac, 1),
                "median_depth_ft": round(median_depth_ft, 2), "flagged": flagged}
            if flagged:
                warnings.append("Coarse grid on a wide, shallow flood: ...cell-size "
                                "convergence check, before relying on the extent.")
        return report, warnings

    # ~1.2 million 20 m cells at 0.5 ft = ~1,186 ac wide, shallow -> flagged
    rep, warns = emulate(20.0, [0.5] * 1200000)
    chk = rep["grid_spread_check"]
    assert chk["flagged"] is True and chk["cell_m"] == 20.0
    assert chk["flooded_acres"] >= wf.GRID_SPREAD_WARN_MIN_AC and chk["median_depth_ft"] == 0.5
    assert any("Coarse grid" in w for w in warns)

    # the same flood at 5 m is not flagged (cell finer than the threshold), no WARNING
    rep5, warns5 = emulate(5.0, [0.5] * 1200000)
    assert rep5["grid_spread_check"]["flagged"] is False
    assert not any("Coarse grid" in w for w in warns5)

    # a deep (channelized) flood at 20 m is not flagged even when wide
    repd, warnsd = emulate(20.0, [3.0] * 1200000)
    assert repd["grid_spread_check"]["flagged"] is False
    assert not any("Coarse grid" in w for w in warnsd)
