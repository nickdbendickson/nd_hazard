"""Tests of core/hydrology.py and the rainfall -> inflow tool algorithm."""
import math
import os
import shutil
import tempfile

import numpy as np

from nd_breach_runner.core import hydrology as hy
from nd_breach_runner.core.units import ACFT, CFS
from nd_breach_runner.tests.registry import test


def _close(a, b, rel=1e-6, abs_=0.0):
    assert abs(a - b) <= max(abs_, rel * max(abs(a), abs(b))), "{} != {}".format(a, b)


def _base(**k):
    d = dict(area_mi2=10.0, storm_depth_in=8.0, duration_hr=24.0, cn=85.0, tc_hr=4.0)
    d.update(k)
    return d


def _raises(fn, text=""):
    try:
        fn()
    except hy.HydrologyError as e:
        assert text in str(e), str(e)
        return
    raise AssertionError("expected HydrologyError")


@test
def hydrology_type2_cumulative_is_monotone_from_0_to_1():
    x = np.linspace(0.0, 1.0, 2401)
    f = hy.scs_type2_fraction(x)
    assert f[0] == 0.0 and abs(f[-1] - 1.0) < 1e-12
    assert np.all(np.diff(f) >= 0.0)
    assert len(hy.SCS_TYPE_II_HR) == len(hy.SCS_TYPE_II_CUM)
    assert np.all(np.diff(hy.SCS_TYPE_II_HR) > 0) and np.all(np.diff(hy.SCS_TYPE_II_CUM) > 0)
    _close(float(hy.scs_type2_fraction(0.5)), 0.663)           # the intense hour is 11.75-12.25
    for p in (0.25, 0.5, 0.75):
        g = hy.triangular_fraction(x, p)
        assert g[0] == 0.0 and abs(g[-1] - 1.0) < 1e-12 and np.all(np.diff(g) >= 0.0)
        _close(float(hy.triangular_fraction(p, p)), p)           # continuous at the peak
    # front-loaded has more rain early than back-loaded
    assert hy.triangular_fraction(0.5, 0.25) > hy.triangular_fraction(0.5, 0.75)


@test
def hydrology_hand_check_cn100_uniform_runoff_equals_rain():
    # CN 100: no losses. 1 mi2, 1 in over 2 hr.
    r = hy.hydrograph_from_settings(dict(area_mi2=1.0, storm_depth_in=1.0, duration_hr=2.0,
                                         distribution="uniform", cn=100.0, tc_hr=1.0))
    _close(r.runoff_depth_in, 1.0)
    _close(r.runoff_coeff, 1.0)
    # 1 in over 1 mi2 = 640 ac / 12 = 53.333 ac-ft
    _close(r.direct_volume_acft, 640.0 / 12.0, rel=1e-3)
    _close(r.volume_acft, 640.0 / 12.0, rel=1e-3)
    assert abs(r.volume_error) < 1e-9
    # same answer with loss_method none
    r2 = hy.hydrograph_from_settings(dict(area_mi2=1.0, storm_depth_in=1.0, duration_hr=2.0,
                                          distribution="uniform", loss_method="none", tc_hr=1.0))
    _close(r2.runoff_depth_in, 1.0)
    _close(r2.peak_cfs, r.peak_cfs)
    # the CFS-hour of 1 in over 1 mi2 (the 484 hydrograph area)
    _close(hy.CFS_HR_PER_MI2_IN, 645.333, rel=1e-5)


@test
def hydrology_unit_hydrograph_area_is_one_inch_over_the_basin():
    area = 7.5
    one_inch_m3 = area * 1609.344 ** 2 * 0.0254
    for kw in (dict(method="scs", lag_hr=2.4), dict(method="scs", lag_hr=2.4, prf=300.0),
               dict(method="lag", lag_hr=0.7, prf=600.0),
               dict(method="clark", clark_tc_hr=3.0, clark_r_hr=2.0)):
        for dt in (300.0, 900.0):
            t, u = hy.unit_hydrograph(area, dt, **kw)
            _close(float(u.sum()) * dt, one_inch_m3, rel=1e-9)
            assert u[0] == 0.0 and np.all(u >= 0.0) and abs(t[1] - dt) < 1e-9
            assert u.max() > 10 * u[-1]                                    # the tail is cut low
    # 484: the gamma shape M is the textbook ~3.7 and the peak is close to 484 A Q / tp
    m = hy.gamma_shape_from_prf(484.0)
    assert 3.5 < m < 4.0, m
    t, u = hy.unit_hydrograph(100.0, 60.0, "scs", lag_hr=10.0, prf=484.0)
    tp_hr = 10.0 + 60.0 / 7200.0
    qp_cfs = 484.0 * 100.0 / tp_hr
    _close(u.max() / CFS, qp_cfs, rel=0.02)


@test
def hydrology_scs_curve_number_hand_values():
    # P = 5 in, CN 80: S = 2.5, Ia = 0.5, Q = 4.5^2 / 7 = 2.8929 in
    _close(float(hy.scs_runoff_in(5.0, 80.0)), 4.5 ** 2 / 7.0)
    # Ia ratio changes it: Ia = 0.05 S = 0.125, Q = 4.875^2 / (4.875 + 2.5)
    _close(float(hy.scs_runoff_in(5.0, 80.0, 0.05)), 4.875 ** 2 / 7.375)
    assert float(hy.scs_runoff_in(0.4, 80.0)) == 0.0
    _close(hy.adjust_cn(80.0, "III"), 23 * 80 / (10 + 0.13 * 80))
    _close(hy.adjust_cn(80.0, "I"), 4.2 * 80 / (10 - 0.058 * 80))
    assert hy.adjust_cn(100.0, "III") == 100.0 and abs(hy.adjust_cn(100.0, "I") - 100.0) < 1e-9
    # through the generator: incremental excess sums to the cumulative Q
    r = hy.hydrograph_from_settings(_base(storm_depth_in=5.0, cn=80.0, distribution="uniform"))
    _close(r.runoff_depth_in, 4.5 ** 2 / 7.0, rel=1e-9)
    _close(float(r.rain_in.sum()), 5.0, rel=1e-12)
    r3 = hy.hydrograph_from_settings(_base(storm_depth_in=5.0, cn=80.0, distribution="uniform",
                                           amc="III"))
    assert r3.runoff_depth_in > r.runoff_depth_in


@test
def hydrology_constant_losses_hand_check():
    # 2 in uniform over 4 hr = 0.5 in/hr; Ia 0.5 in gone in 1 hr; then 3 hr at 0.25 in/hr lost
    r = hy.hydrograph_from_settings(dict(
        area_mi2=2.0, storm_depth_in=2.0, duration_hr=4.0, distribution="uniform",
        loss_method="constant", initial_loss_in=0.5, loss_rate_in_hr=0.25, tc_hr=1.0))
    _close(r.runoff_depth_in, 0.75, rel=1e-9)
    # losses larger than the rain: nothing runs off, and it is reported
    r = hy.hydrograph_from_settings(dict(
        area_mi2=2.0, storm_depth_in=2.0, duration_hr=4.0, distribution="uniform",
        loss_method="constant", initial_loss_in=3.0, tc_hr=1.0))
    assert r.runoff_depth_in == 0.0 and r.peak_cfs == 0.0
    assert any("no runoff" in w.lower() for w in r.warnings)


@test
def hydrology_volume_is_conserved_for_every_method():
    combos = []
    for dist in ("scs_type2", "uniform", "triangular"):
        for uh in ("scs", "clark", "lag"):
            for loss in ("cn", "constant", "none"):
                combos.append((dist, uh, loss))
    for dist, uh, loss in combos:
        k = _base(distribution=dist, uh_method=uh, loss_method=loss, initial_loss_in=0.4,
                  loss_rate_in_hr=0.05, lag_hr=2.0, clark_r_hr=3.0, base_flow_cfs=50.0,
                  duration_hr=24.0 if dist == "scs_type2" else 6.0)
        r = hy.hydrograph_from_settings(k)
        assert abs(r.volume_error) < hy.VOLUME_TOLERANCE, (dist, uh, loss, r.volume_error)
        want = float(r.excess_in.sum()) * 0.0254 * 10.0 * 1609.344 ** 2
        got = float(np.trapezoid(r.q_direct_cms, r.t_s)) if hasattr(np, "trapezoid") \
            else float(np.trapz(r.q_direct_cms, r.t_s))
        _close(got, want, rel=1e-3)
        _close(r.runoff_depth_in, float(r.excess_in.sum()), rel=1e-12)
        assert r.t_s[0] == 0.0 and np.all(np.diff(r.t_s) > 0)
        assert abs(r.q_cms[-1] - 50.0 * CFS) < 1e-9                  # ends on the base flow
        # total volume = direct + base flow over the record
        _close(r.volume_acft * ACFT, r.direct_volume_acft * ACFT + 50.0 * CFS * r.t_s[-1],
               rel=1e-9)


@test
def hydrology_peak_factor_sensitivity():
    peaks, vols = [], []
    for prf in (300.0, 400.0, 484.0, 600.0):
        r = hy.hydrograph_from_settings(_base(prf=prf, distribution="triangular"))
        peaks.append(r.peak_cfs)
        vols.append(r.direct_volume_acft)
    assert peaks == sorted(peaks) and peaks[0] < 0.95 * peaks[-1], peaks
    _close(vols[0], vols[-1], rel=1e-6)                         # same runoff volume
    # a bigger storm, a bigger area and a shorter tc all raise the peak
    p0 = hy.hydrograph_from_settings(_base()).peak_cfs
    assert hy.hydrograph_from_settings(_base(storm_depth_in=12.0)).peak_cfs > p0
    assert hy.hydrograph_from_settings(_base(area_mi2=20.0)).peak_cfs > p0
    assert hy.hydrograph_from_settings(_base(tc_hr=2.0, distribution="triangular")).peak_cfs > \
        hy.hydrograph_from_settings(_base(tc_hr=8.0, distribution="triangular")).peak_cfs
    # Clark: a smaller R gives a sharper, higher peak
    a = hy.hydrograph_from_settings(_base(uh_method="clark", clark_r_hr=1.0)).peak_cfs
    b = hy.hydrograph_from_settings(_base(uh_method="clark", clark_r_hr=6.0)).peak_cfs
    assert a > b


@test
def hydrology_time_of_concentration_formulas():
    length, slope, cn = 5000.0, 2.0, 75.0
    s = 1000.0 / cn - 10.0
    lag = length ** 0.8 * (s + 1.0) ** 0.7 / (1900.0 * math.sqrt(slope))
    _close(hy.tc_scs_lag_hr(length, slope, cn), lag / 0.6)
    _close(lag, 0.9455, rel=2e-3)                                # hand value
    _close(hy.tc_kirpich_hr(length, slope), 0.0078 * length ** 0.77 * 0.02 ** -0.385 / 60.0)
    _close(hy.tc_kirpich_hr(length, slope) * 60.0, 24.8, rel=5e-3)     # minutes, hand value
    r = hy.hydrograph_from_settings(_base(tc_method="scs_lag", flow_length_ft=length,
                                          slope_pct=slope, cn=cn))
    _close(r.tc_hr, lag / 0.6)
    _close(r.lag_hr, lag)
    assert "SCS lag equation" in r.markdown
    r = hy.hydrograph_from_settings(_base(tc_method="kirpich", flow_length_ft=length,
                                          slope_pct=slope))
    _close(r.tc_hr, hy.tc_kirpich_hr(length, slope))
    assert "Kirpich" in r.markdown
    _raises(lambda: hy.hydrograph_from_settings(_base(tc_method="kirpich")), "flow length")
    _raises(lambda: hy.hydrograph_from_settings(_base(tc_method="scs_lag", cn=None,
                                                      flow_length_ft=1, slope_pct=1)), "curve number")


@test
def hydrology_warnings_and_input_errors():
    # storm shorter than tc; Type II off 24 hr
    r = hy.hydrograph_from_settings(_base(duration_hr=2.0, tc_hr=6.0))
    assert any("shorter than the time of concentration" in w for w in r.warnings)
    assert any("Type II" in w for w in r.warnings)
    # low CN for a big storm
    r = hy.hydrograph_from_settings(_base(cn=55.0, storm_depth_in=3.0))
    assert any("curve number" in w.lower() and "%" in w for w in r.warnings)
    # storm inside the initial abstraction
    r = hy.hydrograph_from_settings(_base(cn=50.0, storm_depth_in=1.0))
    assert r.peak_cfs == 0.0 and any("initial abstraction" in w for w in r.warnings)
    # peak rate factor out of the usual range, base flow tail note
    r = hy.hydrograph_from_settings(_base(prf=200.0, base_flow_cfs=100.0))
    assert any("300-600" in w for w in r.warnings)
    assert any("base flow" in w for w in r.warnings)
    # clean run has no warnings
    r = hy.hydrograph_from_settings(_base())
    assert r.warnings == [], r.warnings
    # errors
    _raises(lambda: hy.hydrograph_from_settings({}), "required")
    _raises(lambda: hy.hydrograph_from_settings(_base(bogus=1)), "unknown setting")
    _raises(lambda: hy.hydrograph_from_settings(_base(cn=120)), "cn")
    _raises(lambda: hy.hydrograph_from_settings(_base(area_mi2=0)), "area_mi2")
    _raises(lambda: hy.hydrograph_from_settings(_base(cn=None)), "curve number")
    _raises(lambda: hy.hydrograph_from_settings(_base(tc_hr=None)), "time of concentration")
    _raises(lambda: hy.hydrograph_from_settings(_base(distribution="nope")), "distribution")
    _raises(lambda: hy.hydrograph_from_settings(_base(uh_method="clark")), "Clark")
    _raises(lambda: hy.hydrograph_from_settings(_base(uh_method="lag")), "lag")
    _raises(lambda: hy.hydrograph_from_settings(_base(distribution="user")), "CSV")
    _raises(lambda: hy.hydrograph_from_settings(_base(storm_depth_in="x")), "number")


@test
def hydrology_user_cumulative_csv_and_triangular_positions():
    d = tempfile.mkdtemp()
    try:
        p = os.path.join(d, "dist.csv")
        open(p, "w").write("time_hr,cum_percent\n0,0\n3,50\n6,100\n")        # percent, header
        x, f, last = hy.read_cumulative_csv(p)
        assert last == 6.0 and list(x) == [0.0, 0.5, 1.0] and list(f) == [0.0, 0.5, 1.0]
        r = hy.hydrograph_from_settings(_base(distribution="user", dist_csv=p, duration_hr=6.0))
        _close(float(r.rain_in.sum()), 8.0)
        _close(r.rain_in[0], r.rain_in[-1], rel=1e-9)             # 50 % in each half: uniform
        # rescaled curve warns
        r = hy.hydrograph_from_settings(_base(distribution="user", dist_csv=p, duration_hr=12.0))
        assert any("rescaled" in w for w in r.warnings)
        for bad in ("0,0\n3,0.6\n6,0.4\n", "0,0\n3,0.4\n6,0.6\n", "0,0.1\n6,1\n"):
            open(p, "w").write(bad)
            _raises(lambda: hy.read_cumulative_csv(p))
        # front / centre / back: where the biggest step is
        peaks = {}
        for pos in ("front", "centre", "back"):
            r = hy.hydrograph_from_settings(_base(distribution="triangular", peak_position=pos))
            peaks[pos] = int(np.argmax(r.rain_in)) / len(r.rain_in)
        assert peaks["front"] < peaks["centre"] < peaks["back"], peaks
    finally:
        shutil.rmtree(d)


@test
def hydrology_csv_round_trips_through_the_flood_scenarios_reader():
    from nd_breach_runner.core import flood
    d = tempfile.mkdtemp()
    try:
        r = hy.hydrograph_from_settings(_base(base_flow_cfs=25.0))
        p = hy.write_csv(r, os.path.join(d, "pmf_inflow.csv"))
        head = open(p).read().splitlines()[0]
        assert head == "time_hr,discharge_cfs", head
        fi = flood.FloodInputs.from_files(p)
        assert len(fi.inflow_t_s) == len(r.t_s)
        assert np.max(np.abs(fi.inflow_t_s - r.t_s)) < 0.01                       # seconds
        assert np.max(np.abs(fi.inflow_q_cms - r.q_cms)) < 1e-4 * r.peak_cms
        _close(fi.inflow_volume_m3 / ACFT, r.volume_acft, rel=1e-4)
        _close(float(fi.inflow_q_cms.max()) / CFS, r.peak_cfs, rel=1e-4)
        assert fi.qin(0.0) > 0.0 and fi.qin(fi.inflow_end_s + 10.0) == 0.0
        # the plugin's own hydrograph reader takes it too
        from nd_breach_runner.core import breach as br
        h = br.read_user_hydrograph(p)
        _close(float(h.q_cms.max()), r.peak_cms, rel=1e-4)
    finally:
        shutil.rmtree(d)


@test
def hydrology_summary_and_markdown():
    r = hy.hydrograph_from_settings(_base(base_flow_cfs=10.0))
    s = r.summary
    for k in ("peak_cfs", "time_to_peak_hr", "volume_acft", "runoff_depth_in", "runoff_coeff",
              "tc_hr", "lag_hr", "dt_min", "n_warnings", "volume_error_pct"):
        assert k in s, k
    _close(s["peak_cfs"], r.peak_cfs, rel=1e-3)
    assert 0.0 < s["runoff_coeff"] <= 1.0
    assert "Peak inflow" in r.markdown and "no PMP value is generated" in r.markdown
    assert r.dt_s >= 60.0 and abs(r.dt_s * len(r.rain_in) - 24 * 3600.0) < 1e-6
    assert r.dt_s <= 24 * 3600.0 / 96.0 + 1e-9                    # resolves the Type II burst
    _close(hy.choose_time_step(0.5, 24.0)[0], 0.5 * 3600 / 5.0, rel=1e-9)     # tc / 5
    assert hy.choose_time_step(0.01, 24.0)[0] == 60.0                          # floor
    assert hy.choose_time_step(100.0, 24.0)[0] == 900.0                        # 1/96 of storm
    assert hy.choose_time_step(100.0, 240.0)[0] == 3600.0                      # ceiling
    assert hy.choose_time_step(1.0, 24.0, dt_min=7.0)[1] * hy.choose_time_step(
        1.0, 24.0, dt_min=7.0)[0] == 24 * 3600.0
    assert hy.choose_time_step(5.0, 24.0, clark_r_hr=0.1)[0] == 360.0          # R limits it


def _algs():
    from nd_breach_runner.tests import fake_qgis
    fake_qgis.install()
    import importlib
    import nd_breach_runner.algorithms as algs
    importlib.reload(algs)
    import nd_breach_runner.algorithms_tools as tools
    importlib.reload(tools)
    import nd_breach_runner.algorithms_hydrology as ah
    importlib.reload(ah)
    return ah, fake_qgis


@test
def hydrology_algorithm_runs_through_the_fake_harness():
    ah, fq = _algs()
    assert ah.HYDROLOGY_ALGORITHMS == (ah.RainfallRunoffAlgorithm,)
    a = ah.RainfallRunoffAlgorithm()
    assert a.name() == "rainfallrunoff"
    assert a.displayName() == "Rainfall to inflow (PMF) hydrograph"
    assert a.group() and a.groupId() == "tools"
    a.initAlgorithm()
    assert a.shortHelpString() and len(a.params) >= 10
    assert isinstance(a.createInstance(), ah.RainfallRunoffAlgorithm)
    d = tempfile.mkdtemp()
    try:
        out = os.path.join(d, "sub", "pmf_inflow.csv")
        fb = fq.QgsProcessingFeedback()
        ctx = fq.QgsProcessingContext()
        r = a.processAlgorithm({"AREA_MI2": 12.0, "DEPTH_IN": 9.0, "DURATION_HR": 24.0,
                                "CN": 82.0, "TC_HR": 5.0, "BASE_FLOW_CFS": 30.0,
                                "OUTPUT": out}, ctx, fb)
        assert r["OUTPUT"] == out and os.path.exists(out)
        assert ctx.loaded == []                                   # loads nothing
        assert r["PEAK_CFS"] > 100 and r["VOLUME_ACFT"] > 0 and "Peak inflow" in r["RESULT"]
        assert any("peak_cfs" in m for m in fb.info)
        want = hy.hydrograph_from_settings(dict(area_mi2=12.0, storm_depth_in=9.0, cn=82.0,
                                                tc_hr=5.0, base_flow_cfs=30.0))
        _close(r["PEAK_CFS"], want.peak_cfs)
        from nd_breach_runner.core import flood
        fi = flood.FloodInputs.from_files(out)
        _close(float(fi.inflow_q_cms.max()) / CFS, want.peak_cfs, rel=1e-4)
        # other choices: Clark + triangular + constant losses + Kirpich
        out2 = os.path.join(d, "c.csv")
        r2 = a.processAlgorithm({"AREA_MI2": 3.0, "DEPTH_IN": 4.0, "DURATION_HR": 6.0,
                                 "DISTRIBUTION": 2, "PEAK_POSITION": 0.25, "LOSS_METHOD": 1,
                                 "INITIAL_LOSS_IN": 0.5, "LOSS_RATE": 0.1, "TC_METHOD": 2,
                                 "FLOW_LENGTH_FT": 12000, "SLOPE_PCT": 1.5, "UH_METHOD": 1,
                                 "CLARK_R_HR": 2.0, "OUTPUT": out2}, ctx, fb)
        assert os.path.exists(r2["OUTPUT"]) and r2["PEAK_CFS"] > 0
        # a bad input is a Processing error, not a traceback
        try:
            a.processAlgorithm({"AREA_MI2": 12.0, "DEPTH_IN": 9.0, "CN": 82.0,
                                "OUTPUT": os.path.join(d, "x.csv")}, ctx, fb)     # no tc
        except fq.QgsProcessingException as e:
            assert "time of concentration" in str(e)
        else:
            raise AssertionError("expected QgsProcessingException")
        # warnings reach the feedback
        fb2 = fq.QgsProcessingFeedback()
        a.processAlgorithm({"AREA_MI2": 1.0, "DEPTH_IN": 2.0, "DURATION_HR": 3.0, "CN": 82.0,
                            "TC_HR": 5.0, "OUTPUT": os.path.join(d, "w.csv")}, ctx, fb2)
        assert any("shorter than the time of concentration" in m for m in fb2.errors)
    finally:
        shutil.rmtree(d)
