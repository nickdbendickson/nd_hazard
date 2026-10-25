"""Tests that run without QGIS:

    python3 -m nd_breach_runner.tests.run_tests            (fast tests)
    python3 -m nd_breach_runner.tests.run_tests --models   (also runs LISFLOOD-FP / ANUGA)

Model tests need NDBREACH_LISFLOOD (path to the patched lisflood executable)
and, for ANUGA, NDBREACH_ANUGA_PYTHON."""
import json
import math
import os
import shutil
import sys
import csv
import tempfile
import traceback

import numpy as np

# 3.13.0: the fixtures run the workflow with stand-in executables (the Python interpreter, shell
# scripts) and a patched lisflood.run, so the pre-run 1-second LISFLOOD-FP check is off here;
# test_v3130 tests that check itself.
os.environ.setdefault("ND_DAM_PRE_RUN_SMOKE", "0")

from nd_breach_runner.core import breach as br
from nd_breach_runner.core import terrain
from nd_breach_runner.core.grid import Grid
from nd_breach_runner.core.units import ACFT, CFS, FT
from nd_breach_runner.tests.synthetic import SyntheticTerrain, npy_writer, valley, TOP_M, H_M

from nd_breach_runner.tests.registry import TESTS, MODEL_TESTS, test, model_test  # noqa: E402


def close(a, b, rel=1e-6, abs_=0.0):
    assert abs(a - b) <= max(abs_, rel * max(abs(a), abs(b))), "{} != {}".format(a, b)


def dam(**k):
    d = dict(dam_id="T1", dam_height_ft=25.0, storage_top_acft=500.0, top_of_dam_ft=1850.0,
             surface_area_top_ac=60.0)
    d.update(k)
    return br.DamData(**d)


# ---------------------------------------------------------------------------- breach
@test
def froehlich_2008_values():
    vw, hb = 500 * ACFT, 25 * FT
    b, tf, z = br.froehlich_2008(vw, hb, "piping")
    close(b, 0.27 * vw ** 0.32 * hb ** 0.04)
    close(tf, 63.2 * math.sqrt(vw / (9.80665 * hb ** 2)))
    assert z == 0.7
    b2, _, z2 = br.froehlich_2008(vw, hb, "overtopping")
    close(b2, 1.3 * b)
    assert z2 == 1.0


@test
def tr60_rules():
    # Br = Vs Hw / A ; Q = 1100 Br^1.35
    q, rule, brf = br.tr60_peak_cfs(500.0, 25.0, 2175.0)
    close(brf, 500 * 25 / 2175.0)
    close(q, 1100 * brf ** 1.35)
    assert rule == "1100 Br^1.35"
    # lower limit 3.2 Hw^2.5
    q, rule, _ = br.tr60_peak_cfs(1.0, 25.0, 1e6)
    close(q, 3.2 * 25 ** 2.5)
    assert "lower" in rule
    # upper limit 65 Hw^1.85
    q, rule, _ = br.tr60_peak_cfs(1e6, 25.0, 100.0)
    close(q, 65 * 25 ** 1.85)
    # Hw >= 103 ft
    q, rule, _ = br.tr60_peak_cfs(10.0, 110.0, 1e5)
    close(q, 65 * 110 ** 1.85)
    # narrow valley replaces the upper limit
    q, rule, _ = br.tr60_peak_cfs(1e6, 25.0, 100.0, valley_width_ft=50.0)
    close(q, 0.416 * 50 * 25 ** 1.5)
    assert "narrow" in rule
    # no area -> None
    assert br.tr60_peak_cfs(500.0, 25.0, None)[0] is None


@test
def reservoir_curve():
    r = br.Reservoir(100.0, 110.0, 1e6, 2e5)
    close(r.m, 2.0)
    close(float(r.volume(110.0)), 1e6)
    close(float(r.volume(105.0)), 1e6 * 0.25)
    close(float(r.wse(0.25e6)), 105.0)
    assert float(r.volume(99.0)) == 0.0
    r2 = br.Reservoir(100.0, 110.0, 1e6, None)
    assert r2.m == 2.0 and r2.notes


@test
def starting_wse_rule():
    d = dam(pmf_max_wse_ft=1848.0)
    assert br.starting_wse_ft(d)[0] == 1848.0
    d = dam(pmf_max_wse_ft=1852.0)
    assert br.starting_wse_ft(d)[0] == 1850.0
    h = br.piping_hydrograph(dam(pmf_max_wse_ft=1845.0))
    assert h.params["hw_ft"] == 20.0
    assert h.params["storage_at_failure_acft"] < 500.0


@test
def piping_hydrograph_volume_and_shape():
    h = br.piping_hydrograph(dam())
    assert abs(h.volume_m3 / ACFT - 500.0) < 5.0, h.volume_m3 / ACFT
    assert h.q_cms[0] == 0.0 and h.q_cms[-1] == 0.0
    # peak near the end of breach formation, between the regression checks' order
    tf = h.params["formation_time_hr"]
    assert 0.5 * tf <= h.time_to_peak_hr <= 1.5 * tf
    assert 0.5 * h.params["froehlich_1995_peak_cfs"] < h.peak_cfs < \
        2.0 * h.params["froehlich_1995_peak_cfs"]


@test
def piping_overrides():
    h = br.piping_hydrograph(dam(), br.BreachOverrides(avg_width_ft=40.0, formation_hr=1.0,
                                                       side_slope_h=1.0))
    assert h.params["avg_breach_width_ft"] == 40.0
    assert h.params["formation_time_hr"] == 1.0
    assert "user" in h.params["breach_parameters"]


@test
def tr60_hydrograph_volume():
    h = br.tr60_hydrograph(dam(crest_width_ft=12, us_slope_h=3, ds_slope_h=3))
    close(h.volume_m3, 500 * ACFT, rel=1e-6)
    close(h.peak_cfs, h.params["tr60_peak_cfs"], rel=1e-3)
    try:
        br.tr60_hydrograph(dam())
        raise AssertionError("TR-60 without embankment geometry should fail")
    except br.BreachInputError:
        pass


@test
def user_hydrograph_csv():
    d = tempfile.mkdtemp()
    p = os.path.join(d, "h.csv")
    with open(p, "w") as f:
        f.write("time_hr,q_cfs\n0,0\n0.5,1000\n1.0,500\n2.0,100\n")
    h = br.make_hydrograph(dam(), br.METHOD_USER, user_csv=p)
    assert h.q_cms[-1] == 0.0
    close(h.peak_cfs, 1000.0, rel=1e-9)
    shutil.rmtree(d)


@test
def validation_messages():
    try:
        br.piping_hydrograph(dam(dam_height_ft=0))
        raise AssertionError("expected an error")
    except br.BreachInputError as e:
        assert "height" in str(e)


@test
def raised_breach_bottom_keeps_dead_storage():
    d = dam(dam_height_ft=30.0, top_of_dam_ft=1000.0, storage_top_acft=500.0,
            breach_bottom_ft=985.0)
    h = br.piping_hydrograph(d)
    p = h.params
    assert p["dead_storage_acft"] > 20.0, p
    close(p["releasable_acft"], p["storage_at_failure_acft"] - p["dead_storage_acft"], rel=1e-3)
    assert p["released_acft"] <= p["releasable_acft"] + 0.5
    assert h.volume_m3 / ACFT < p["storage_at_failure_acft"] - p["dead_storage_acft"] + 1.0
    # a lower breach bottom than the reservoir bottom is raised, with a note
    h2 = br.piping_hydrograph(dam(breach_bottom_ft=1000.0))
    assert any("raised" in n for n in h2.notes)


@test
def short_hydrograph_keeps_its_peak_in_model_files():
    d = dam(dam_height_ft=40.0, storage_top_acft=5.0, surface_area_top_ac=2.0,
            crest_width_ft=12, us_slope_h=3, ds_slope_h=3)
    h = br.tr60_hydrograph(d)
    assert h.duration_s < 30.0          # shorter than the 30 s grid
    t, q = h.resampled(30.0)
    close(q.max(), h.q_cms.max(), rel=1e-9)
    close(float(np.trapezoid(q, t)) if hasattr(np, "trapezoid") else float(np.trapz(q, t)),
          h.volume_m3, rel=1e-6)
    hp = br.piping_hydrograph(dam())
    t, q = hp.resampled(30.0)
    close(q.max(), hp.q_cms.max(), rel=1e-9)


@test
def volume_until():
    h = br.tr60_hydrograph(dam(crest_width_ft=12, us_slope_h=3, ds_slope_h=3))
    close(h.volume_until(1e9), h.volume_m3, rel=1e-9)
    assert h.volume_until(h.duration_s / 2) < h.volume_m3


@test
def long_recession_is_reported_not_silently_cut():
    d = dam(dam_height_ft=6.0, storage_top_acft=40000.0, surface_area_top_ac=8000.0)
    h = br.piping_hydrograph(d, max_hours=100.0)
    assert h.duration_s <= 101 * 3600.0
    assert any("still in the reservoir" in n for n in h.notes), h.notes
    # given enough time the whole releasable storage leaves
    h = br.piping_hydrograph(d, max_hours=2000.0)
    assert h.params["released_acft"] > 0.98 * h.params["releasable_acft"]


@test
def pmf_start_delays_nothing_at_water_level():
    top = br.piping_hydrograph(dam())
    pmf = br.piping_hydrograph(dam(pmf_max_wse_ft=1845.0))
    first = pmf.t_s[np.nonzero(pmf.q_cms)[0][0]]
    assert first < 30.0, "the opening starts at the water surface, so flow starts at once"
    assert pmf.peak_cfs < top.peak_cfs


@test
def lisflood_inputs_use_local_coordinates_and_all_breakpoints():
    from nd_breach_runner.core import lisflood as lf
    d = tempfile.mkdtemp()
    x0, y0 = 512345.0, 5234567.0
    g = Grid(np.zeros((40, 60)), x0, y0, 10.0)
    h = br.tr60_hydrograph(dam(dam_height_ft=40.0, storage_top_acft=5.0, surface_area_top_ac=2.0,
                               crest_width_ft=12, us_slope_h=3, ds_slope_h=3))
    st = lf.LisfloodSettings("x")
    lf.write_inputs(d, g, (x0 + 305.0, y0 - 205.0), h, st, inflow_line=[(x0 + 305.0, y0 - 180.0),
                                                                        (x0 + 305.0, y0 - 230.0)])
    bci = open(os.path.join(d, "run.bci")).read().splitlines()
    pts = [l for l in bci if l.startswith("P")]
    assert len(pts) >= 3
    for l in pts:
        _, x, y, *_ = l.split()
        assert 0 < float(x) < 600 and 0 < float(y) < 400, l     # local, not UTM
    asc = open(os.path.join(d, "run.dem.asc")).read().splitlines()[:6]
    assert "xllcorner 0.000000" in asc and "yllcorner 0.000000" in asc
    bdy = [l.split() for l in open(os.path.join(d, "run.bdy")).read().splitlines()[3:]]
    peak = max(float(v) for v, _ in bdy)
    close(peak * g.dx * len(pts), h.q_cms.max(), rel=1e-3)     # the peak is in the file
    shutil.rmtree(d)


@test
def mass_reading_sums_interval_errors():
    from nd_breach_runner.core import lisflood as lf
    d = tempfile.mkdtemp()
    p = os.path.join(d, "x.mass")
    with open(p, "w") as f:
        f.write("Time Tstep MinTstep NumTsteps Area Vol Qin Hds Qout Qerror Verror Rain\n")
        for i, e in enumerate([1.0, -1.0, 2.0]):
            f.write("{} 1 1 1 1 {} 5 0 0 0 {} 0\n".format(60 * (i + 1), 100 * (i + 1), e))
    t, cum, vol, qin, worst = lf._read_mass(p)
    assert (t, cum, vol, qin, worst) == (180.0, 2.0, 300.0, 5.0, 2.0)
    shutil.rmtree(d)


@test
def breach_parameter_formulas():
    vw, hb, hw = 500 * ACFT, 25 * FT, 25 * FT
    b, tf, z = br.froehlich_1995(vw, hb, "piping")
    close(b, 0.1803 * vw ** 0.32 * hb ** 0.19)
    close(tf, 0.00254 * vw ** 0.53 * hb ** -0.9 * 3600.0)
    assert z == 0.9
    b2, _, z2 = br.froehlich_1995(vw, hb, "overtopping")
    close(b2, 1.4 * b)
    assert z2 == 1.4
    # Von Thun & Gillette: Cb by reservoir size
    for acft, cb in ((500, 6.1), (2000, 18.3), (7000, 42.7), (20000, 54.9)):
        b, tf, z = br.vtg_1990(acft * ACFT, hw, True)
        close(b, 2.5 * hw + cb)
        close(tf, 0.015 * hw * 3600.0)
    _, tf, _ = br.vtg_1990(500 * ACFT, hw, False)
    close(tf, (0.02 * hw + 0.25) * 3600.0)
    # MacDonald & Langridge-Monopolis
    ver = 0.0261 * (vw * hw) ** 0.769
    b, tf, z = br.mlm_1984(vw, hw, 200.0)
    close(b, ver / 200.0)
    close(tf, 0.0179 * ver ** 0.364 * 3600.0)


@test
def every_routed_method_conserves_volume():
    d = dam(crest_width_ft=12, us_slope_h=3, ds_slope_h=3)
    peaks = {}
    for m in br.METHODS:
        if m == br.METHOD_USER:
            continue
        h = br.make_hydrograph(d, m)
        assert abs(h.volume_m3 / ACFT - 500.0) < 8.0, (m, h.volume_m3 / ACFT)
        assert h.q_cms[-1] == 0.0 and h.peak_cfs > 1000, m
        peaks[m] = h.peak_cfs
    # overtopping breaches are wider than piping breaches, so their peak is higher
    assert peaks[br.METHOD_OVERTOPPING] > peaks[br.METHOD_PIPING]
    assert peaks[br.METHOD_F95_OVERTOPPING] > peaks[br.METHOD_F95_PIPING]
    # TR-60 curvilinear has the same peak and volume as the triangle
    close(peaks[br.METHOD_TR60_CURVE], peaks[br.METHOD_TR60], rel=1e-3)


@test
def curvilinear_shape_keeps_volume_and_peak():
    h = br.tr60_hydrograph(dam(crest_width_ft=12, us_slope_h=3, ds_slope_h=3),
                           shape="curvilinear")
    close(h.volume_m3, 500 * ACFT, rel=2e-3)
    close(h.peak_cfs, h.params["tr60_peak_cfs"], rel=1e-3)
    assert h.method == br.METHOD_TR60_CURVE and "curvilinear" in h.params["shape"]
    # a tiny volume cannot take that shape and falls back to a triangle with a note
    small = dam(dam_height_ft=40.0, storage_top_acft=0.5, surface_area_top_ac=0.5,
                crest_width_ft=12, us_slope_h=3, ds_slope_h=3)
    h2 = br.tr60_hydrograph(small, shape="curvilinear")
    assert h2.method == br.METHOD_TR60 or h2.method == br.METHOD_TR60_CURVE


@test
def pipe_phase_starts_slowly_then_speeds_up():
    d = dam()
    weir = br.piping_hydrograph(d)
    orf = br.make_hydrograph(d, br.METHOD_PIPING_ORIFICE)
    p = orf.params
    tc = p["pipe_phase_hr"] * 3600.0
    assert 0.3 * p["formation_time_hr"] * 3600 < tc < 0.7 * p["formation_time_hr"] * 3600
    # before the roof collapses the pipe passes far less than the weir opening does
    q_orf = np.interp(0.9 * tc, orf.t_s, orf.q_cms)
    q_weir = np.interp(0.9 * tc, weir.t_s, weir.q_cms)
    assert q_orf < 0.25 * q_weir, (q_orf, q_weir)
    # after collapse the flow jumps up and the peak is near the weir model's
    assert np.interp(1.2 * tc, orf.t_s, orf.q_cms) > 3 * q_orf
    close(orf.peak_cfs, weir.peak_cfs, rel=0.15)
    assert any("Pipe phase" in n for n in orf.notes)
    # pipe parameters can be overridden
    o2 = br.make_hydrograph(d, br.METHOD_PIPING_ORIFICE,
                            br.BreachOverrides(pipe_time_fraction=0.8))
    assert o2.params["pipe_phase_hr"] > p["pipe_phase_hr"]


@test
def mlm_without_geometry_warns():
    h = br.make_hydrograph(dam(), br.METHOD_MLM)
    assert any("12 ft crest" in n for n in h.notes)
    h = br.make_hydrograph(dam(crest_width_ft=20, us_slope_h=2, ds_slope_h=2), br.METHOD_MLM)
    assert not any("12 ft crest" in n for n in h.notes)


@test
def thinning_keeps_peak_and_volume():
    t = np.linspace(0, 40 * 3600.0, 20000)
    q = 900.0 * np.exp(-t / 9000.0) * (1 + 0.02 * np.sin(t / 50.0)) + 5.0
    q[7000] = 1500.0
    h = br.Hydrograph(t, q, "x")
    th = h.thinned(max_points=800)
    assert len(th.t_s) < len(t) / 5
    close(th.peak_cms, 1500.0, rel=1e-9)
    close(th.volume_m3, h.volume_m3, rel=3e-3)


@test
def spillway_rating():
    from nd_breach_runner.core import flood
    sp = flood.Spillway.from_weir(100.0, 10.0, c_us=3.0)
    close(sp.q(90.0 * FT), 0.0, abs_=1e-12)
    close(sp.q((100.0 + 2.0) * FT) / CFS, 3.0 * 10.0 * 2.0 ** 1.5, rel=1e-6)
    beyond = sp.q((100.0 + 40.0) * FT)
    assert beyond > sp.q((100.0 + 30.0) * FT)
    d = tempfile.mkdtemp()
    p = os.path.join(d, "rating.csv")
    with open(p, "w") as f:
        f.write("elev_ft,q_cfs\n1846,0\n1848,100\n1847,30\n1850,400\n")
    s2 = flood.Spillway.from_csv(p)
    close(s2.q(1849.0 * FT) / CFS, 250.0, rel=1e-9)
    shutil.rmtree(d)


@test
def flood_routing_with_and_without_breach():
    from nd_breach_runner.core import flood
    d = dam(crest_width_ft=12, us_slope_h=3, ds_slope_h=3, crest_length_ft=800.0)
    fi = flood.FloodInputs.triangle(6000.0, 6.0, 30.0, spillway=flood.Spillway.from_weir(
        1846.0, 20.0), normal_pool_ft=1846.0)
    for m in (br.METHOD_PIPING, br.METHOD_OVERTOPPING, br.METHOD_TR60):
        r = flood.route_flood(d, m, None, fi)
        assert r.failed
        wb, nb, bo = r.with_breach, r.no_breach, r.breach_only
        # breach starts at the peak pool level
        assert abs(wb.t_ref_s - 6.3 * 3600.0) < 0.5 * 3600.0
        assert nb.peak_cfs < 0.35 * wb.peak_cfs
        # before the breach the two runs are the same
        tt = np.linspace(0.0, 0.9 * wb.t_ref_s, 20)
        np.testing.assert_allclose(np.interp(tt, wb.t_s, wb.q_cms),
                                   np.interp(tt, nb.t_s, nb.q_cms), rtol=0.02, atol=1.0)
        # mass balance: outflow = inflow + the storage above the breach bottom that was lost
        stored0 = 500.0 * (21.0 / 25.0) ** 3
        expect = fi.inflow_volume_m3 / ACFT + stored0
        if m != br.METHOD_TR60:
            close(wb.volume_m3 / ACFT, expect, rel=0.015)
        assert bo.peak_cfs > 0.5 * wb.peak_cfs
        assert r.summary["peak_outflow_with_breach_cfs"] > r.summary["peak_outflow_no_breach_cfs"]
    # the pool never reaches the trigger level: no breach
    fi2 = flood.FloodInputs.triangle(6000.0, 6.0, 30.0, spillway=fi.spillway,
                                     normal_pool_ft=1846.0, trigger="wse", trigger_value=1900.0)
    r = flood.route_flood(d, br.METHOD_PIPING, None, fi2)
    assert not r.failed and r.with_breach is None and r.no_breach is not None
    # breach at a set time
    fi3 = flood.FloodInputs.triangle(6000.0, 6.0, 30.0, spillway=fi.spillway,
                                     normal_pool_ft=1846.0, trigger="time", trigger_value=3.0)
    r = flood.route_flood(d, br.METHOD_PIPING, None, fi3)
    assert abs(r.with_breach.t_ref_s - 3 * 3600.0) < 60.0
    # the scenario needs a pool level
    try:
        flood.route_flood(d, br.METHOD_PIPING, None,
                          flood.FloodInputs.triangle(100.0, 1.0, 5.0))
        raise AssertionError("expected an error")
    except br.BreachInputError as e:
        assert "normal pool" in str(e)


@test
def incremental_depth_and_rise():
    from nd_breach_runner.core import flood
    dw = np.array([[2.0, 1.0, 0.0, np.nan]])
    dn = np.array([[1.0, 1.0, np.nan, np.nan]])
    ww = np.array([[12.0, 11.0, np.nan, np.nan]])
    wn = np.array([[11.0, 11.0, np.nan, np.nan]])
    inc, rise, stats = flood.incremental(dw, dn, ww, wn, [0.5, 1.0, 1.5])
    assert inc[0, 0] == 1.0 and inc[0, 1] == 0.0 and np.isnan(inc[0, 3])
    assert rise[0, 0] == 1.0 and rise[0, 1] == 0.0
    assert stats == {0.5: 1, 1.0: 1, 1.5: 0}


# ---------------------------------------------------------------------------- grid/terrain
# ------------------------------------------------------------------ fast: geometry helpers
@test
def conditioning_edits():
    from nd_breach_runner.core import conditioning as cond
    g = Grid(np.full((60, 80), 100.0), 0.0, 600.0, 10.0)
    z = g.data
    z[:, 40] = 106.0                                   # a road embankment across the grid
    z[30, 10:30] = 99.0                                # a pit in the channel
    line = np.array([[5.0, 305.0], [795.0, 305.0]])
    n = cond.Notch(np.array([[400.0, 355.0], [400.0, 255.0]]), 20.0)
    out, init, counts = cond.Edits(notches=[n]).apply(g)
    assert out.data[30, 40] <= 100.0 + 1e-9 and out.data[10, 40] == 106.0
    assert init is None
    ch = cond.ChannelBurn(line, 20.0, 1.0)
    out, _, counts = cond.Edits(channels=[ch]).apply(g)
    prof = out.data[30, :]
    assert np.all(np.diff(prof[5:75]) <= 1e-9), "burned channel must fall steadily"
    poly = np.array([[100, 100], [200, 100], [200, 200], [100, 200]], float)
    pond = cond.PondLower(poly, depth_m=2.0, wse_m=100.0)
    out, init, _ = cond.Edits(ponds=[pond]).apply(g)
    inside = init.data[42, 15]
    assert abs(inside - 2.0) < 1e-9 and abs(out.data[42, 15] - 98.0) < 1e-9
    assert init.data[5, 70] == 0
    ct = cond.ConditionedTerrain(_Src(g), cond.Edits(ponds=[pond]))
    assert ct.initial_depth((0, 0, 800, 600), 10.0).data.max() == 2.0


class _Src:
    z_to_m = 1.0
    z_note = "m"

    def __init__(self, g):
        self.g = g

    def read(self, bounds, cell):
        return self.g.like(self.g.data.copy())


@test
def polygons_from_masks():
    from nd_breach_runner.core import products
    a = np.zeros((40, 60), bool)
    a[5:20, 5:30] = True
    a[10:14, 10:14] = False
    a[30:35, 40:50] = True
    a[22, 3] = True
    g = Grid(np.zeros((40, 60)), 1000.0, 5000.0, 10.0)
    polys = products.mask_polygons(g, a, min_cells=4)
    assert len(polys) == 2 and sorted(len(h) for _, h in polys) == [0, 1]
    close(sum(products.polygon_area_m2(p) for p in polys), (a.sum() - 1) * 100.0)
    # cells that touch only at a corner stay separate
    b = np.zeros((8, 8), bool)
    b[2, 2] = b[3, 3] = True
    assert len(products.mask_polygons(g.like(np.zeros((8, 8))), b, min_cells=1)) == 2
    md = products.markdown_to_html("# T\n\n| a | b |\n|---|---|\n| 1 | **2** |\n\n- x\n")
    assert "<th>a</th>" in md and "<b>2</b>" in md and "<li>x</li>" in md


@test
def receptor_summary_and_edges():
    from nd_breach_runner.core import impacts
    g = Grid(np.zeros((50, 50)), 0.0, 500.0, 10.0)
    rec = impacts.Receptors()
    rec.add_points([[100.0, 400.0], [450.0, 100.0], [900.0, 100.0]], ids=["a", "b", "c"])
    rec.add_lines([np.array([[0.0, 250.0], [499.0, 250.0]])], kind="road", ids=["r1"])
    depth = np.zeros((50, 50))
    depth[5:15, 5:20] = 2.0
    depth[20:30, :] = 0.6
    fin = {"depth": np.where(depth > 0, depth, np.nan),
           "velocity": np.where(depth > 0, 1.0, np.nan),
           "arrival": np.where(depth > 0, 0.5, np.nan)}
    s, rows = impacts.summarize(rec, g, fin, 0.3)
    assert s["structure"]["flooded"] == 1 and s["road"]["flooded"] > 0.4
    assert {r["id"] for r in rows if r["kind"] == "structure"} == {"a"}
    assert "2 structures" in s["receptors_in_model_area"] and "1 structure" in s["receptors_flooded"]
    (x, y), d, span = impacts.edge_cells(g, np.where(depth > 0, depth, 0.0), "E", 0.1)
    assert d == 0.6 and span == 100.0


@test
def nd_data_lookups():
    from nd_breach_runner.core import nd_data
    a = np.array([[11, 82, 23], [41, 999, np.nan]])
    n = nd_data.nlcd_to_manning(a, nd_data.manning_lut({"82": 0.05}, default=0.07))
    assert n[0, 1] == 0.05 and n[0, 2] == 0.10 and n[1, 1] == 0.07 and n[1, 2] == 0.07
    try:
        nd_data.manning_lut("not json")
        raise AssertionError("expected an error")
    except ValueError:
        pass
    m, how = nd_data.suggest_fields(["OBJECTID", "NIDID", "Dam_Name", "NID_HEIGHT", "MAX_STORAGE",
                                     "SURFACE_AREA", "Crest_Elev", "Dam_Height_ft"])
    assert m["F_ID"] == "NIDID" and m["F_STORAGE"] == "MAX_STORAGE" and m["F_AREA"] == "SURFACE_AREA"
    assert m["F_HEIGHT"] in ("NID_HEIGHT", "Dam_Height_ft") and m["F_TOP"] == "Crest_Elev"
    assert len(set(m.values())) == len(m), "a field must not be used twice"
    d = tempfile.mkdtemp()
    os.makedirs(os.path.join(d, "inputs"))
    open(os.path.join(d, "inputs", "pmf_inflow.csv"), "w").write("0,0\n")
    open(os.path.join(d, "spillway_rating.csv"), "w").write("0,0\n")
    f = nd_data.dam_input_files(d)
    assert set(f) == {"pmf_inflow", "spillway"}
    shutil.rmtree(d)


@test
def benchmark_scores():
    from nd_breach_runner.core import benchmark
    rng = np.random.default_rng(1)
    d = np.where(rng.random((60, 80)) < 0.6, rng.random((60, 80)) * 8.0, np.nan)
    same = benchmark.compare_fields({"depth": d}, {"depth": d.copy()}, 0.02)
    assert same["fit_index_F"] == 1.0 and same["depth_ft"]["rmse"] == 0.0
    assert same["depth_class_agreement"] == 1.0 and "agree closely" in same["verdict"]
    shifted = np.roll(d, 6, axis=1)
    m = benchmark.compare_fields({"depth": d, "velocity": np.where(np.isfinite(d), 2.0, np.nan)},
                                 {"depth": shifted, "velocity": np.where(
                                     np.isfinite(shifted), 3.0, np.nan)}, 0.02)
    assert m["fit_index_F"] < 0.7 and "differ" in m["verdict"]
    assert abs(m["velocity_fps"]["bias"] + 1.0) < 1e-9
    arr = benchmark.difference_arrays({"depth": d}, {"depth": shifted})
    code = arr["Benchmark Extent Agreement.tif"]
    assert set(np.unique(code[np.isfinite(code)])) <= {1.0, 2.0, 3.0}
    dd = tempfile.mkdtemp()
    out = benchmark.write_outputs(dd, m, arr, npy_writer, Grid(d, 0, 60, 1.0))
    assert os.path.exists(out["csv"]) and "Depth classes" in open(out["html"]).read()
    shutil.rmtree(dd)


class _P:
    def __init__(self, name, typ, desc="", optional=False, dest=False, default=None):
        self._n, self._t, self._d, self.optional, self._dest, self._def = (
            name, typ, desc, optional, dest, default)

    def name(self): return self._n
    def type(self): return self._t
    def description(self): return self._d
    def flags(self): return 0
    def isDestination(self): return self._dest
    def defaultValue(self): return self._def


class _Alg:
    def __init__(self, aid, params):
        self._id, self._p = aid, params

    def id(self): return self._id
    def parameterDefinitions(self): return self._p


class _Reg:
    def __init__(self, algs): self.algs = {a.id(): a for a in algs}
    def algorithmById(self, i): return self.algs.get(i)
    def algorithms(self): return list(self.algs.values())


@test
def handoff_fills_parameters_by_name():
    from nd_breach_runner.core import handoff
    d = tempfile.mkdtemp()
    for n in ("Depth (Max).tif", "Velocity (Max).tif", "Arrival Time.tif", "WSE (Max).tif"):
        open(os.path.join(d, n), "w").write("x")
    alg = _Alg("ndhazard:classify", [
        _P("DEPTH", "raster", "Max depth raster"), _P("VEL", "raster", "Velocity raster"),
        _P("ARR", "raster", "Arrival time"), _P("WSE", "raster", "WSE (water surface)"),
        _P("DAMID", "string", "Dam ID"), _P("OUT", "fileDestination", "Result", dest=True),
        _P("STYLE", "enum", "Style", default=0)])
    reg = _Reg([alg])
    assert handoff.find_algorithm("classify", registry=reg) is alg
    p = handoff.build_parameters(alg, "ND-1", d, os.path.dirname(d), handoff.rasters_in(d))
    assert p["DEPTH"].endswith("Depth (Max).tif") and p["ARR"].endswith("Arrival Time.tif")
    assert p["WSE"].endswith("WSE (Max).tif") and p["DAMID"] == "ND-1" and "OUT" in p
    assert "STYLE" not in p
    bad = _Alg("ndhazard:classify", [_P("MYSTERY", "extent", "Study area")])
    try:
        handoff.build_parameters(bad, "x", d, d, {})
        raise AssertionError("expected an error")
    except handoff.HandoffError as e:
        assert "MYSTERY" in str(e)
    p2 = handoff.build_parameters(bad, "x", d, d, {}, {"MYSTERY": "{folder}"})
    assert p2["MYSTERY"] == d
    assert handoff.find_class({"CLASS": "High", "OUT": "/x/y.csv"}) == "High"
    b, c, o = (os.path.join(d, n) for n in ("b.csv", "c.csv", "o.csv"))
    open(b, "w").write("Dam,Status\nND-1,done\nND-2,done\n")
    open(c, "w").write("Dam ID,Hazard Class\nnd-1,High\n")
    handoff.merge_csv(b, c, o)
    rows = open(o).read().splitlines()
    assert rows[0] == "Dam,Status,Hazard Class" and rows[1].endswith("High") and rows[2].endswith(",")
    shutil.rmtree(d)


@test
def asc_roundtrip_and_bilinear():
    d = tempfile.mkdtemp()
    a = np.arange(12, dtype=float).reshape(3, 4)
    a[1, 1] = np.nan
    g = Grid(a, 100.0, 50.0, 5.0)
    p = os.path.join(d, "g.asc")
    g.write_asc(p, fmt="%.2f")
    h = Grid.read_asc(p)
    assert h.data.shape == (3, 4) and h.x0 == 100.0 and h.y0 == 50.0 and h.dx == 5.0
    assert h.data[1, 1] == h.nodata
    v = g.sample_bilinear(np.array([102.5 + 5]), np.array([47.5 - 5]))  # centre of [1,1]
    assert np.isnan(v[0])          # no-data cell stays no-data
    # halfway between the centres of [0, 2] (2.0) and [0, 3] (3.0)
    v = g.sample_bilinear(np.array([115.0]), np.array([47.5]))
    close(float(v[0]), 2.5)
    assert np.isnan(g.sample_bilinear(np.array([0.0]), np.array([0.0]))[0])
    shutil.rmtree(d)


@test
def priority_flood_drains():
    z = np.full((20, 20), 10.0)
    z[5:15, 5:15] = 5.0          # a pit
    f = terrain.priority_flood(z)
    assert f[10, 10] >= 10.0 - 1e-9
    assert np.all(f >= z - 1e-12)


@test
def trace_and_inflow_on_synthetic_valley():
    src = SyntheticTerrain()
    g = src.read((-2000, -3000, 8000, 3000), 30.0)
    pt, why = terrain.find_inflow_point(src.read((-150, -150, 150, 150), 10.0), (0, 0), H_M,
                                        TOP_M)
    assert pt is not None and pt[0] > 30, (pt, why)
    path, L = terrain.trace_downstream(g, pt, 5000.0)
    assert path[-1][0] > path[0][0] + 3000, path[-1]     # goes east, down the valley
    ux, uy = terrain.flow_direction(path)
    assert ux > 0.9


@test
def mask_outline_area():
    g = Grid(np.zeros((100, 100)), 0.0, 1000.0, 10.0)
    m = terrain.disk_mask(g, (500.0, 500.0), 300.0)
    ring = terrain.mask_outline(g, m, simplify_m=5.0)
    area = abs(terrain._area(ring))
    close(area, math.pi * 300 ** 2, rel=0.03)
    assert terrain._area(ring) > 0          # counter-clockwise


@test
def edges_and_expansion():
    d = np.zeros((10, 20))
    d[5, -1] = 0.5
    e = terrain.edge_depths(d)
    assert e["E"] == 0.5 and e["W"] == 0.0
    b = terrain.expand_bounds((0, 0, 100, 50), ["E", "N"], 10)
    assert b == (0, 0, 110, 60)


# ---------------------------------------------------------------------------- QGIS layer
@test
def processing_algorithms_initialise():
    from nd_breach_runner.tests import fake_qgis
    fake_qgis.install()
    import importlib
    import nd_breach_runner.algorithms as algs
    importlib.reload(algs)
    for cls in algs.ALGORITHMS:
        a = cls()
        a.initAlgorithm()
        assert a.name() and a.displayName() and a.shortHelpString()
        assert len(a.params) >= (1 if a.name() == "verifymanifest" else 2)
    import nd_breach_runner.provider as prov
    importlib.reload(prov)
    p = prov.Provider()
    assert p.id() == "ndbreach"
    p.loadAlgorithms()


@test
def hydrograph_algorithm_runs():
    from nd_breach_runner.tests import fake_qgis
    fake_qgis.install()
    import importlib
    import nd_breach_runner.algorithms as algs
    importlib.reload(algs)
    a = algs.HydrographAlgorithm()
    a.initAlgorithm()
    d = tempfile.mkdtemp()
    out = os.path.join(d, "h.csv")
    fb = fake_qgis.QgsProcessingFeedback()
    r = a.processAlgorithm({"HEIGHT_FT": 25, "STORAGE_ACFT": 500, "AREA_AC": 60,
                            "OUTPUT": out}, fake_qgis.QgsProcessingContext(), fb)
    assert os.path.exists(r["OUTPUT"])
    assert any("peak_cfs" in m for m in fb.info)
    shutil.rmtree(d)


# ---------------------------------------------------------------------------- models
def _exe():
    return os.environ.get("NDBREACH_LISFLOOD", "")


@model_test
def lisflood_check():
    from nd_breach_runner.core.lisflood import check_lisflood
    d = tempfile.mkdtemp()
    r = check_lisflood(_exe(), d)
    assert r["ok"] and r["patched"], r
    shutil.rmtree(d)


@model_test
def stock_lisflood_is_refused():
    stock = os.environ.get("NDBREACH_LISFLOOD_STOCK", "")
    if not stock:
        print("   (skipped: NDBREACH_LISFLOOD_STOCK not set)")
        return
    from nd_breach_runner.core.lisflood import check_lisflood, ModelError
    from nd_breach_runner.core import workflow
    r = check_lisflood(stock, tempfile.mkdtemp())
    assert not r["ok"] and "patch" in r["message"], r
    d = tempfile.mkdtemp()
    st = workflow.RunSettings(cell_size_m=20.0, initial_length_mi=1.0, max_iterations=1,
                              lisflood_exe=stock)
    try:
        workflow.run_dam(dam(), (0.0, 0.0), st, SyntheticTerrain(), npy_writer,
                         os.path.join(d, "T1"))
        raise AssertionError("an unpatched build must be refused")
    except ModelError as e:
        assert "patch" in str(e)
    shutil.rmtree(d)


@model_test
def wrong_dem_units_are_caught():
    from nd_breach_runner.core import workflow

    class FeetAsMetres(SyntheticTerrain):
        def read(self, bounds, cell):
            g = SyntheticTerrain.read(self, bounds, cell)
            g.data = g.data / FT * 1.0        # elevations in feet but treated as metres
            return g

    d = tempfile.mkdtemp()
    st = workflow.RunSettings(cell_size_m=20.0, initial_length_mi=1.0, max_iterations=1,
                              lisflood_exe=_exe())
    try:
        workflow.run_dam(dam(), (0.0, 0.0), st, FeetAsMetres(), npy_writer,
                         os.path.join(d, "T1"))
        raise AssertionError("expected a unit error")
    except br.BreachInputError as e:
        assert "elevation units" in str(e) or "DEM elevation" in str(e), str(e)
    shutil.rmtree(d)


@model_test
def short_breach_reaches_lisflood():
    from nd_breach_runner.core import workflow
    d = tempfile.mkdtemp()
    dm = dam(dam_height_ft=25.0, storage_top_acft=5.0, surface_area_top_ac=2.0,
             crest_width_ft=12, us_slope_h=3, ds_slope_h=3)
    st = workflow.RunSettings(breach_method=br.METHOD_TR60, cell_size_m=10.0,
                              initial_length_mi=1.0, half_width_mi=0.3, max_iterations=1,
                              lisflood_exe=_exe(), threads=2)
    r = workflow.run_dam(dm, (0.0, 0.0), st, SyntheticTerrain(), npy_writer,
                         os.path.join(d, "T1"))
    assert r.summary["max_depth_ft"] > 1.0, r.summary
    shutil.rmtree(d)


@model_test
def workflow_lisflood_extends_domain():
    from nd_breach_runner.core import workflow
    d = tempfile.mkdtemp()
    st = workflow.RunSettings(cell_size_m=20.0, initial_length_mi=1.0, half_width_mi=0.5,
                              expand_step_mi=1.0, max_iterations=3, lisflood_exe=_exe(),
                              threads=2)
    r = workflow.run_dam(dam(), (0.0, 0.0), st, SyntheticTerrain(), npy_writer,
                         os.path.join(d, "T1"))
    rep = json.load(open(r.report_json))
    runs = rep["lisflood_runs"]
    assert len(runs) >= 2, "the 1-mile domain should have been extended"
    assert runs[1]["size_mi"][0] > runs[0]["size_mi"][0]
    assert runs[-1]["stats"]["max_volume_error_pct"] < 1.0
    for k in ("depth", "velocity", "arrival", "wse"):
        assert os.path.exists(r.final[k])
    depth = np.load(os.path.splitext(r.final["depth"])[0] + ".npy")
    arr = np.load(os.path.splitext(r.final["arrival"])[0] + ".npy")
    # arrival only where depth reached the arrival depth
    assert np.all(depth[np.isfinite(arr)] >= 1.0 - 1e-6)
    # reservoir side (west of the dam) stays dry
    js = json.load(open(os.path.splitext(r.final["depth"])[0] + ".json"))
    xs = js["x0"] + (np.arange(depth.shape[1]) + 0.5) * js["dx"]
    assert not np.isfinite(depth[:, xs < -60]).any()
    shutil.rmtree(d)


@model_test
def workflow_anuga_agrees_with_lisflood():
    py = os.environ.get("NDBREACH_ANUGA_PYTHON", "")
    if not py:
        print("   (skipped: NDBREACH_ANUGA_PYTHON not set)")
        return
    from nd_breach_runner.core import workflow
    d = tempfile.mkdtemp()
    st = workflow.RunSettings(tier=workflow.TIER_BOTH, cell_size_m=20.0, initial_length_mi=2.0,
                              half_width_mi=0.5, max_iterations=1, sim_time_hr=2.0,
                              lisflood_exe=_exe(), anuga_python=py, threads=2,
                              anuga_fine_area_m2=400, anuga_coarse_area_m2=4000,
                              anuga_near_field_mi=0.5)
    r = workflow.run_dam(dam(), (0.0, 0.0), st, SyntheticTerrain(), npy_writer,
                         os.path.join(d, "T1"))
    rep = json.load(open(r.report_json))
    c = rep["comparison"]
    assert c["fit_index_F"] > 0.8, c
    s = rep["anuga_runs"][-1]["stats"]
    assert abs(s["volume_in_domain_m3"] - s["injected_m3"]) < 0.01 * s["injected_m3"], s
    assert r.summary["final_model"] == "ANUGA"
    shutil.rmtree(d)


def _flood_settings(**k):
    from nd_breach_runner.core import workflow, flood
    fi = flood.FloodInputs.triangle(4000.0, 4.0, 20.0, spillway=flood.Spillway.from_weir(
        1846.0, 20.0), normal_pool_ft=1846.0)
    base = dict(cell_size_m=20.0, initial_length_mi=1.5, half_width_mi=0.5, max_iterations=1,
                lisflood_exe=_exe(), threads=2, scenario=workflow.SCENARIO_FLOOD, flood=fi,
                sim_time_hr=6.0)
    base.update(k)
    return workflow.RunSettings(**base)


def _dam_flood():
    return dam(crest_width_ft=12, us_slope_h=3, ds_slope_h=3, crest_length_ft=800.0)


@model_test
def workflow_flood_scenario_baseline_incremental_products():
    import xml.etree.ElementTree as ET
    from nd_breach_runner.core import workflow
    d = tempfile.mkdtemp()
    st = _flood_settings()
    r = workflow.run_dam(_dam_flood(), (0.0, 0.0), st, SyntheticTerrain(), npy_writer,
                         os.path.join(d, "T1"))
    rep = json.load(open(r.report_json))
    assert rep["scenario"] == "flood" and "flood" in rep and "incremental" in rep
    dep = lambda p: np.load(os.path.splitext(p)[0] + ".npy")
    wb, nb, inc = dep(r.final["depth"]), dep(r.baseline["depth"]), dep(r.increment["depth"])
    assert np.nansum(wb > 0.0) > np.nansum(nb > 0.0), "the breach must add flooding"
    both = np.isfinite(wb) & np.isfinite(inc)
    assert np.all(inc[both] <= wb[both] + 1e-4)
    assert np.nanmax(inc) > 1.0
    assert os.path.basename(r.increment["depth"]) == "Incremental Depth (Max).tif"
    # vector products, EAP map, report
    assert os.path.exists(r.vectors["depth_polygons"]) and os.path.exists(r.vectors["extent"])
    fc = json.load(open(r.vectors["extent"]))
    assert fc["features"][0]["geometry"]["type"] == "MultiPolygon"
    ET.parse(r.eap_map)
    ET.parse(r.vectors["hydrograph_plot"])
    assert os.path.exists(r.report_html) and "Incremental effect" in open(r.report_html).read()
    # the breach starts at the flood's peak pool, and arrival counts from there
    assert rep["hydrograph"]["time_to_peak_hr"] < 2.0
    shutil.rmtree(d)


@model_test
def workflow_no_breach_when_trigger_not_reached():
    from nd_breach_runner.core import workflow, flood
    d = tempfile.mkdtemp()
    fi = flood.FloodInputs.triangle(4000.0, 4.0, 20.0, spillway=flood.Spillway.from_weir(
        1846.0, 20.0), normal_pool_ft=1846.0, trigger="wse", trigger_value=1900.0)
    r = workflow.run_dam(_dam_flood(), (0.0, 0.0), _flood_settings(flood=fi), SyntheticTerrain(),
                         npy_writer, os.path.join(d, "T1"))
    assert r.summary["breach_occurs"] is False and not r.final
    assert "does not fail" in open(r.report_md).read()
    shutil.rmtree(d)


def _road_valley(x, y):
    """The valley with a road embankment across it at x = 2500 m, 1.5 m below the top of dam.
    A road above the reservoir's level would make the filled DEM spill back through the
    reservoir, so the flow trace (and the domain) would run upstream."""
    z = valley(x, y)
    return np.where(np.abs(x - 2500.0) < 12.0, np.maximum(z, TOP_M - 1.5), z)


@model_test
def culvert_and_notch_let_water_through_the_road():
    from nd_breach_runner.core import workflow, structures
    d = tempfile.mkdtemp()
    kw = dict(cell_size_m=20.0, initial_length_mi=2.5, half_width_mi=0.5, max_iterations=1,
              lisflood_exe=_exe(), threads=2, sim_time_hr=6.0, products=False)
    def go(tag, structs):
        st = workflow.RunSettings(structures=structs, **kw)
        r = workflow.run_dam(dam(), (0.0, 0.0), st, SyntheticTerrain(_road_valley),
                             npy_writer, os.path.join(d, tag))
        depth = np.load(os.path.splitext(r.final["depth"])[0] + ".npy")
        js = json.load(open(os.path.splitext(r.final["depth"])[0] + ".json"))
        xs = js["x0"] + (np.arange(depth.shape[1]) + 0.5) * js["dx"]
        return r, np.nanmax(np.where(xs > 2600, depth, np.nan)) if np.isfinite(
            depth[:, xs > 2600]).any() else 0.0
    r0, down0 = go("blocked", [])
    cul = structures.Structure(structures.KIND_CULVERT, np.array([[2530.0, 0.0], [2470.0, 0.0]]),
                               width_m=3.0, height_m=2.0, name="CR-12 culvert")
    r1, down1 = go("culvert", [cul])
    assert down0 < 0.2 and down1 > 0.3, (down0, down1)
    rep1 = json.load(open(r1.report_json))
    assert any("CR-12" in x for x in rep1["structures"])
    # v3.12.0: the structures as the run used them, for the HEC-RAS model export
    used = rep1["structures_used"]
    assert [u["name"] for u in used] == ["CR-12 culvert"] and used[0]["width_m"] == 3.0
    assert used[0]["line"] == [[2530.0, 0.0], [2470.0, 0.0]]
    shutil.rmtree(d)


@model_test
def receptors_decide_whether_the_domain_keeps_growing():
    from nd_breach_runner.core import workflow, impacts
    d = tempfile.mkdtemp()
    def go(tag, pts):
        rec = impacts.Receptors()
        rec.add_points(pts)
        st = workflow.RunSettings(cell_size_m=20.0, initial_length_mi=1.0, half_width_mi=0.5,
                                  expand_step_mi=1.0, max_iterations=3, lisflood_exe=_exe(),
                                  threads=2, receptors=rec, impact_look_ahead_mi=3.0,
                                  products=False)
        r = workflow.run_dam(dam(), (0.0, 0.0), st, SyntheticTerrain(), npy_writer,
                             os.path.join(d, tag))
        return r, json.load(open(r.report_json))
    r_far, rep_far = go("far", [[1000.0, 30000.0]])            # 30 km off the flow path
    assert len(rep_far["lisflood_runs"]) == 1, "nothing lies downstream: no extension"
    assert any("ND 5.1.1(f)" in w for w in r_far.warnings)
    r_on, rep_on = go("on", [[3200.0, 0.0]])                    # a farm on the flow path
    assert len(rep_on["lisflood_runs"]) >= 2
    assert rep_on["lisflood_runs"][0]["edge_decisions"]["E"]["extend"] is True
    shutil.rmtree(d)


@model_test
def gauge_records_the_flood_wave():
    from nd_breach_runner.core import workflow
    d = tempfile.mkdtemp()
    st = workflow.RunSettings(cell_size_m=20.0, initial_length_mi=1.5, half_width_mi=0.5,
                              max_iterations=1, lisflood_exe=_exe(), threads=2, sim_time_hr=3.0,
                              gauges=[{"name": "county road", "xy": (1200.0, 0.0)}],
                              products=False)
    r = workflow.run_dam(dam(), (0.0, 0.0), st, SyntheticTerrain(), npy_writer,
                         os.path.join(d, "T1"))
    t, q = r.gauges["county road"]
    peak_in = r.summary["peak_breach_cfs"] * CFS
    assert 0.4 * peak_in < q.max() < 1.1 * peak_in, (q.max(), peak_in)
    assert os.path.exists(os.path.join(r.folder, "_breach_model", "gauge_county_road.csv"))
    shutil.rmtree(d)


@model_test
def pond_starts_full_and_depth_is_what_the_flood_adds():
    from nd_breach_runner.core import workflow, conditioning as cond
    d = tempfile.mkdtemp()
    poly = np.array([[900, -150], [1300, -150], [1300, 150], [900, 150]], float)
    ed = cond.Edits(ponds=[cond.PondLower(poly, depth_m=1.5)])
    st = workflow.RunSettings(cell_size_m=20.0, initial_length_mi=1.5, half_width_mi=0.5,
                              max_iterations=1, lisflood_exe=_exe(), threads=2, sim_time_hr=3.0,
                              edits=ed, products=False)
    r = workflow.run_dam(dam(), (0.0, 0.0), st, SyntheticTerrain(_pond_valley), npy_writer,
                         os.path.join(d, "T1"))
    js = json.load(open(os.path.splitext(r.final["depth"])[0] + ".json"))
    depth = np.load(os.path.splitext(r.final["depth"])[0] + ".npy")
    xs = js["x0"] + (np.arange(depth.shape[1]) + 0.5) * js["dx"]
    ys = js["y0"] - (np.arange(depth.shape[0]) + 0.5) * js["dx"]
    X, Y = np.meshgrid(xs, ys)
    inside = (np.abs(X - 1100) < 150) & (np.abs(Y) < 100)
    mx = np.nanmax(depth[inside])
    assert mx < 12.0, "the pond water is not counted as flood depth ({})".format(mx)
    rep = json.load(open(r.report_json))
    assert rep["lisflood_runs"][-1]["stats"]["max_volume_error_pct"] < 1.5
    shutil.rmtree(d)


@model_test
def anuga_with_culvert_breakline_pond_and_baseline():
    py = os.environ.get("NDBREACH_ANUGA_PYTHON", "")
    if not py:
        print("   (skipped: NDBREACH_ANUGA_PYTHON not set)")
        return
    from nd_breach_runner.core import workflow, structures, conditioning as cond, flood
    d = tempfile.mkdtemp()
    cul = structures.Structure(structures.KIND_CULVERT, np.array([[2530.0, 0.0], [2470.0, 0.0]]),
                               width_m=3.0, height_m=2.0, name="CR-12")
    poly = np.array([[900, -150], [1300, -150], [1300, 150], [900, 150]], float)
    fi = flood.FloodInputs.triangle(4000.0, 4.0, 20.0, spillway=flood.Spillway.from_weir(
        1846.0, 20.0), normal_pool_ft=1846.0)
    st = workflow.RunSettings(
        tier=workflow.TIER_BOTH, cell_size_m=20.0, initial_length_mi=2.5, half_width_mi=0.5,
        max_iterations=1, sim_time_hr=3.0, lisflood_exe=_exe(), anuga_python=py, threads=2,
        anuga_fine_area_m2=400, anuga_coarse_area_m2=4000, anuga_near_field_mi=0.5,
        structures=[cul], edits=cond.Edits(ponds=[cond.PondLower(poly, depth_m=1.0)]),
        refine_lines=[np.array([[2500.0, -600.0], [2500.0, 600.0]])], products=False,
        scenario=workflow.SCENARIO_FLOOD, flood=fi)
    r = workflow.run_dam(_dam_flood(), (0.0, 0.0), st, SyntheticTerrain(_road_valley),
                         npy_writer, os.path.join(d, "T1"))
    rep = json.load(open(r.report_json))
    s = rep["anuga_runs"][-1]["stats"]
    assert s.get("culverts") == 1 and not s.get("culvert_errors"), s
    assert s.get("breaklines_used", 0) >= 1, s
    assert s.get("initial_volume_m3", 0) > 0, s
    assert abs(s["volume_in_domain_m3"] - (s["injected_m3"] + s["initial_volume_m3"])) < 0.02 * (
        s["injected_m3"] + s["initial_volume_m3"]), s
    assert r.summary["final_model"] == "ANUGA" and r.baseline and r.increment
    assert "diff" in r.qa
    shutil.rmtree(d)


def _pond_valley(x, y):
    z = valley(x, y)
    pond = (np.abs(x - 1100) < 200) & (np.abs(y) < 200)
    return np.where(pond, np.minimum(z, z.min()) * 0 + valley(np.array(1100.0), np.array(0.0)),
                    z)


@model_test
def resume_skips_finished_runs_and_cancel_stops_quickly():
    import time
    from nd_breach_runner.core import workflow, lisflood as lfmod
    d = tempfile.mkdtemp()
    st = dict(cell_size_m=20.0, initial_length_mi=1.5, half_width_mi=0.5, max_iterations=1,
              lisflood_exe=_exe(), threads=2, sim_time_hr=3.0, products=False)
    out = os.path.join(d, "T1")
    logs = []
    r1 = workflow.run_dam(dam(), (0.0, 0.0), workflow.RunSettings(resume=True, **st),
                          SyntheticTerrain(), npy_writer, out, log=logs.append)
    logs2 = []
    t0 = time.time()
    r2 = workflow.run_dam(dam(), (0.0, 0.0), workflow.RunSettings(resume=True, **st),
                          SyntheticTerrain(), npy_writer, out, log=logs2.append)
    assert any("Resuming" in m for m in logs2) and any("loaded from the checkpoint" in m
                                                       for m in logs2)
    assert not any(m.startswith("LISFLOOD-FP run") for m in logs2)
    assert r1.summary["flooded_acres"] == r2.summary["flooded_acres"]
    # a changed setting means a fresh start
    logs3 = []
    workflow.run_dam(dam(), (0.0, 0.0), workflow.RunSettings(resume=True, **dict(
        st, manning_n=0.07)), SyntheticTerrain(), npy_writer, out, log=logs3.append)
    assert any("starting over" in m for m in logs3)
    # cancel
    start = time.time()
    try:
        workflow.run_dam(dam(), (0.0, 0.0), workflow.RunSettings(**dict(st, cell_size_m=10.0)),
                         SyntheticTerrain(), npy_writer, os.path.join(d, "C"),
                         cancel=lambda: time.time() - start > 2.0)
        raise AssertionError("expected a cancel")
    except lfmod.Cancelled:
        pass
    assert time.time() - start < 30.0
    shutil.rmtree(d)


@model_test
def sensitivity_runs_share_the_domain_and_make_an_envelope():
    from nd_breach_runner.core import workflow, sensitivity as sens, impacts
    d = tempfile.mkdtemp()
    rec = impacts.Receptors()
    rec.add_points([[800.0, 0.0], [1400.0, 0.0]])
    st = workflow.RunSettings(cell_size_m=20.0, initial_length_mi=1.5, half_width_mi=0.5,
                              max_iterations=1, lisflood_exe=_exe(), threads=2, sim_time_hr=3.0,
                              receptors=rec, products=False)
    cases = [sens.Case("Width x0.6", sens.GROUP_WIDTH, width_factor=0.6),
             sens.Case("Width x1.6", sens.GROUP_WIDTH, width_factor=1.6),
             sens.Case("TR-60", sens.GROUP_METHOD, method=br.METHOD_TR60)]
    out = os.path.join(d, "T1")
    r = sens.run_sensitivity(dam(crest_width_ft=12, us_slope_h=3, ds_slope_h=3), (0.0, 0.0), st,
                             SyntheticTerrain(), npy_writer, out, cases=cases, cell_check=False,
                             classify=lambda folder: "High" if os.path.exists(os.path.join(
                                 folder, "Depth (Max).tif")) else "?")
    assert [x["case"] for x in r.rows][0] == "Base" and len(r.rows) == 4
    peaks = {x["case"]: x["peak_cfs"] for x in r.rows}
    assert peaks["Width x1.6"] > peaks["Width x0.6"]
    load = lambda n: np.load(os.path.splitext(r.envelope[n])[0] + ".npy")
    lo, med, hi = (load("Sensitivity Depth ({})".format(k) + ".tif") for k in
                   ("Low", "Median", "High"))
    ok = np.isfinite(lo) & np.isfinite(hi)
    assert np.all(lo[ok] <= med[ok] + 1e-6) and np.all(med[ok] <= hi[ok] + 1e-6)
    assert np.nansum(hi > 0) >= np.nansum(lo > 0)
    assert os.path.exists(r.summary_csv) and os.path.exists(r.report_html)
    assert any("hazard class is High in every case" in f for f in r.flags), r.flags
    assert all(x.get("structures_flooded") is not None for x in r.rows)
    shutil.rmtree(d)


def _two_dam_valley(x, y):
    """The valley with a second, smaller dam and reservoir at x = 2600 m."""
    z = valley(x, y)
    bed_b = valley(np.array(2600.0), np.array(0.0)) + 0.0
    top_b = TOP_M - 3.0
    emb = np.where(np.abs(x - 2600.0) < 30.0, top_b - np.abs(x - 2600.0) / 30.0 * 2.0, -1e9)
    return np.where((np.abs(x - 2600.0) < 30.0) & (np.abs(y) < 900.0), np.maximum(z, emb), z)


@model_test
def cascade_follows_the_wave_to_the_next_dam():
    from nd_breach_runner.core import workflow, cascade, flood
    d = tempfile.mkdtemp()
    a = dam(dam_id="A")
    b = dam(dam_id="B", dam_height_ft=10.0, storage_top_acft=60.0, top_of_dam_ft=TOP_M / FT - 3.0 / FT,
            surface_area_top_ac=15.0)
    st = workflow.RunSettings(cell_size_m=20.0, initial_length_mi=1.0, half_width_mi=0.5,
                              expand_step_mi=1.0, max_iterations=3, lisflood_exe=_exe(),
                              threads=2, sim_time_hr=4.0, products=False, impact_look_ahead_mi=3.0)
    chain = [cascade.CascadeDam(a, (0.0, 0.0)), cascade.CascadeDam(b, (2600.0, 0.0))]
    r = cascade.run_cascade(chain, st, SyntheticTerrain(_two_dam_valley), npy_writer,
                            os.path.join(d, "casc"), fail_rule="assume")
    assert len(r.runs) == 2 and r.runs[1][1] is not None, r.decisions
    assert r.decisions[0]["fails"] and "assumed" in r.decisions[0]["reason"]
    up = r.runs[0][1]
    assert "to_B" in up.gauges and up.gauges["to_B"][1].max() > 0
    assert r.runs[1][1].cascade_offset_h > 0.0
    assert os.path.exists(r.combined["Cascade Depth (Max).tif"]) and os.path.exists(r.report_md)
    # a big reservoir with a spillway absorbs the wave: the downstream dam holds
    big = dam(dam_id="B2", dam_height_ft=10.0, storage_top_acft=100000.0,
              top_of_dam_ft=TOP_M / FT - 3.0 / FT, surface_area_top_ac=5000.0,
              crest_width_ft=12, us_slope_h=3, ds_slope_h=3, crest_length_ft=2000.0)
    fi = flood.FloodInputs.triangle(10.0, 1.0, 10.0, spillway=flood.Spillway.from_weir(
        TOP_M / FT - 6.0 / FT, 50.0), normal_pool_ft=TOP_M / FT - 5.0 / FT)
    chain = [cascade.CascadeDam(a, (0.0, 0.0)), cascade.CascadeDam(big, (2600.0, 0.0), flood=fi)]
    r2 = cascade.run_cascade(chain, st, SyntheticTerrain(_two_dam_valley), npy_writer,
                             os.path.join(d, "casc2"), fail_rule="overtop")
    assert r2.decisions[0]["fails"] is False and "does not overtop" in r2.decisions[0]["reason"], \
        r2.decisions
    shutil.rmtree(d)


@model_test
def runtime_estimate_is_the_right_order():
    import time
    from nd_breach_runner.core import workflow, estimate, lisflood as lfmod
    rate = lfmod.calibrate(_exe(), tempfile.mkdtemp(), threads=2)
    assert rate and rate > 1e5
    st = workflow.RunSettings(cell_size_m=10.0, initial_length_mi=1.0, half_width_mi=0.5,
                              max_iterations=1, lisflood_exe=_exe(), threads=2, products=False,
                              sim_time_hr=3.0)
    p = workflow.preview(dam(), (0.0, 0.0), st, SyntheticTerrain())
    est = estimate.estimate(p, rate)
    d = tempfile.mkdtemp()
    t0 = time.time()
    workflow.run_dam(dam(), (0.0, 0.0), st, SyntheticTerrain(), npy_writer, os.path.join(d, "T"))
    actual = (time.time() - t0) / 60.0
    assert 0.2 * actual < est["one_run_min"] < 5.0 * actual, (est, actual)
    assert "domain" in estimate.describe(p, est)[0]
    shutil.rmtree(d)


class _FakeRaster:
    def crs(self):
        from nd_breach_runner.tests import fake_qgis
        return fake_qgis.QgsCoordinateReferenceSystem("EPSG:26914")

    def source(self):
        return "synthetic"


def _patched_algs():
    from nd_breach_runner.tests import fake_qgis
    fake_qgis.install()
    import importlib
    import nd_breach_runner.algorithms as algs
    importlib.reload(algs)

    def gis(self, parameters, context, crs, pt):
        return (SyntheticTerrain(), npy_writer, fake_qgis.QgsCoordinateTransform(),
                fake_qgis.QgsCoordinateReferenceSystem("EPSG:26914"))
    algs._Base.gis = gis
    return algs, fake_qgis


@model_test
def rundam_algorithm_end_to_end():
    algs, fq = _patched_algs()
    a = algs.RunDamAlgorithm()
    a.initAlgorithm()
    d = tempfile.mkdtemp()
    fb = fq.QgsProcessingFeedback()
    ctx = fq.QgsProcessingContext()
    r = a.processAlgorithm({"DAM_ID": "ND 123/A", "DAM_POINT": fq.QgsPointXY(0, 0),
                            "HEIGHT_FT": 25, "STORAGE_ACFT": 500, "TOP_FT": 1850,
                            "AREA_AC": 60, "DEM": _FakeRaster(), "CELL": 20.0,
                            "LENGTH_MI": 1.0, "HALF_WIDTH_MI": 0.5, "STEP_MI": 1.0,
                            "MAX_RUNS": 2, "LISFLOOD_EXE": _exe(), "DAMS_FOLDER": d}, ctx, fb)
    assert r["FOLDER"].endswith("ND 123_A"), r["FOLDER"]
    assert os.path.exists(r["DEPTH"]) and os.path.exists(r["REPORT"])
    assert len(ctx.loaded) >= 4
    assert any(m.startswith("peak_breach_cfs") for m in fb.info)
    shutil.rmtree(d)


@model_test
def batch_algorithm_end_to_end():
    algs, fq = _patched_algs()

    class Geom:
        def __init__(self, x, y):
            self.p = fq.QgsPointXY(x, y)

        def isEmpty(self):
            return False

        def centroid(self):
            return self

        def asPoint(self):
            return self.p

    class Feat(dict):
        def __init__(self, fid, x, y, **attrs):
            dict.__init__(self, attrs)
            self._g, self._id = Geom(x, y), fid

        def geometry(self):
            return self._g

        def id(self):
            return self._id

    class Src:
        def __init__(self, feats):
            self.feats = feats

        def getFeatures(self):
            return iter(self.feats)

        def sourceCrs(self):
            return fq.QgsCoordinateReferenceSystem("EPSG:26914")

    feats = [Feat(1, 0, 0, NIDID="ND00001", H=25.0, S=500.0, SA=60.0, TOP=1850.0),
             Feat(2, 0, 0, NIDID="ND00002", H=None, S=500.0, SA=60.0, TOP=1850.0)]
    a = algs.BatchAlgorithm()
    a.initAlgorithm()
    d = tempfile.mkdtemp()
    fb = fq.QgsProcessingFeedback()
    r = a.processAlgorithm({"DAMS": Src(feats), "F_ID": "NIDID", "F_HEIGHT": "H",
                            "F_STORAGE": "S", "F_AREA": "SA", "F_TOP": "TOP",
                            "DEM": _FakeRaster(), "CELL": 20.0, "LENGTH_MI": 1.0,
                            "HALF_WIDTH_MI": 0.5, "MAX_RUNS": 1, "LISFLOOD_EXE": _exe(),
                            "DAMS_FOLDER": d, "SKIP_DONE": True},
                           fq.QgsProcessingContext(), fb)
    import csv as _csv
    rows = list(_csv.DictReader(open(r["SUMMARY"])))
    st = {row["Dam"]: row["Status"] for row in rows}
    assert st == {"ND00001": "done", "ND00002": "failed"}, st
    assert "height" in [row for row in rows if row["Dam"] == "ND00002"][0]["Message"]
    # a second run skips the finished dam
    r = a.processAlgorithm({"DAMS": Src(feats[:1]), "F_ID": "NIDID", "F_HEIGHT": "H",
                            "F_STORAGE": "S", "DEM": _FakeRaster(), "LISFLOOD_EXE": _exe(),
                            "DAMS_FOLDER": d, "SKIP_DONE": True},
                           fq.QgsProcessingContext(), fb)
    assert "1 skipped" in r["COUNTS"], r["COUNTS"]
    shutil.rmtree(d)


def _write_csv(d, name, text):
    p = os.path.join(d, name)
    open(p, "w").write(text)
    return p


def _pt(x, y, **a):
    from nd_breach_runner.tests import fake_qgis as fq
    return fq.FakeFeature(fq.FakeGeometry("point", [[(x, y)]]), **a)


def _line(pts, **a):
    from nd_breach_runner.tests import fake_qgis as fq
    return fq.FakeFeature(fq.FakeGeometry("line", [pts]), **a)


def _poly(pts, **a):
    from nd_breach_runner.tests import fake_qgis as fq
    return fq.FakeFeature(fq.FakeGeometry("polygon", [pts]), **a)


@model_test
def rundam_algorithm_flood_with_layers():
    algs, fq = _patched_algs()
    a = algs.RunDamAlgorithm()
    a.initAlgorithm()
    d = tempfile.mkdtemp()
    fb = fq.QgsProcessingFeedback()
    ctx = fq.QgsProcessingContext()
    inflow = _write_csv(d, "in.csv", "time_hr,cfs\n0,0\n4,4000\n20,0\n")
    spill = _write_csv(d, "sp.csv", "elev_ft,cfs\n1846,0\n1848,500\n1852,4000\n")
    r = a.processAlgorithm({
        "DAM_ID": "F1", "DAM_NAME": "Flood Test Dam", "DAM_POINT": fq.QgsPointXY(0, 0),
        "HEIGHT_FT": 25, "STORAGE_ACFT": 500, "TOP_FT": 1850, "AREA_AC": 60,
        "CREST_WIDTH": 12, "US_SLOPE": 3, "DS_SLOPE": 3, "CREST_LENGTH": 800,
        "SCENARIO": 1, "INFLOW_CSV": inflow, "SPILLWAY_CSV": spill, "NORMAL_POOL_FT": 1846,
        "BASELINE": True, "DEM": _FakeRaster(), "CELL": 20.0, "LENGTH_MI": 1.5,
        "HALF_WIDTH_MI": 0.5, "MAX_RUNS": 1, "DURATION_HR": 6.0, "LISFLOOD_EXE": _exe(),
        "DAMS_FOLDER": d,
        "RECEPTOR_POINTS": fq.FakeSource([_pt(800, 0, name="a"), _pt(1300, 40, name="b")]),
        "RECEPTOR_ROADS": fq.FakeSource([_line([(0, 60), (2000, 60)])]),
        "STRUCTURES": fq.FakeSource([_line([(1530, 0), (1470, 0)], type="culvert",
                                           width=3, height=3, name="CR-1")]),
        "PONDS": fq.FakeSource([_poly([(900, -150), (1300, -150), (1300, 150), (900, 150)])]),
        "GAUGE_POINTS": fq.FakeSource([_pt(1200, 0, nm="road")]), "G_NAME": "nm",
    }, ctx, fb)
    rep = json.load(open(os.path.join(r["FOLDER"], "_breach_model", "run_report.json")))
    assert rep["scenario"] == "flood" and "incremental" in rep and rep["receptors"]
    assert rep["structures"] and "CR-1" in rep["structures"][0]
    assert os.path.exists(r["EAP_MAP"]) and os.path.exists(r["REPORT_HTML"])
    assert any("Incremental" in x for x in ctx.loaded), ctx.loaded
    assert os.path.exists(os.path.join(r["FOLDER"], "_breach_model", "gauge_road.csv"))
    assert os.path.exists(os.path.join(r["FOLDER"], "Incremental Depth (Max).tif"))
    shutil.rmtree(d)


@model_test
def batch_matches_fields_by_name_and_reads_dam_inputs():
    algs, fq = _patched_algs()
    d = tempfile.mkdtemp()
    os.makedirs(os.path.join(d, "ND00001", "inputs"))
    _write_csv(os.path.join(d, "ND00001", "inputs"), "pmf_inflow.csv",
         "time_hr,cfs\n0,0\n4,4000\n20,0\n")
    _write_csv(os.path.join(d, "ND00001", "inputs"), "spillway_rating.csv",
         "elev_ft,cfs\n1846,0\n1848,500\n1852,4000\n")
    from nd_breach_runner.tests import fake_qgis as fq
    def dm(fid, nid):
        return fq.FakeFeature(fq.FakeGeometry("point", [[(0, 0)]]), NIDID=nid, DAM_HEIGHT=25.0,
                              MAX_STORAGE=500.0, SURFACE_AREA=60.0, TOP_OF_DAM=1850.0,
                              NORMAL_POOL=1846.0, CREST_WIDTH=12.0, CREST_LENGTH=800.0)
    src = fq.FakeSource([dm(1, "ND00001"), dm(2, "ND00002")])
    a = algs.BatchAlgorithm()
    a.initAlgorithm()
    fb = fq.QgsProcessingFeedback()
    r = a.processAlgorithm({"DAMS": src, "SCENARIO": 2, "DEM": _FakeRaster(), "CELL": 20.0,
                            "LENGTH_MI": 1.5, "HALF_WIDTH_MI": 0.5, "MAX_RUNS": 1,
                            "DURATION_HR": 5.0, "LISFLOOD_EXE": _exe(), "DAMS_FOLDER": d,
                            "F_POOL": "NORMAL_POOL", "PRODUCTS": False, "SKIP_DONE": False},
                           fq.QgsProcessingContext(), fb)
    import csv as _csv
    rows = {x["Dam"]: x for x in _csv.DictReader(open(r["SUMMARY"]))}
    assert rows["ND00001"]["Scenario"] == "flood" and rows["ND00001"]["Status"] == "done", rows
    assert rows["ND00002"]["Scenario"] == "sunny" and rows["ND00002"]["Status"] == "done", rows
    assert any("matched by name" in m for m in fb.info)
    assert os.path.exists(os.path.join(d, "ND00001", "Incremental Depth (Max).tif"))
    assert not os.path.exists(os.path.join(d, "ND00002", "Incremental Depth (Max).tif"))
    shutil.rmtree(d)


@model_test
def sensitivity_and_estimate_and_check_algorithms():
    algs, fq = _patched_algs()
    d = tempfile.mkdtemp()
    common = {"DAM_ID": "S1", "DAM_POINT": fq.QgsPointXY(0, 0), "HEIGHT_FT": 25,
              "STORAGE_ACFT": 500, "TOP_FT": 1850, "AREA_AC": 60, "DEM": _FakeRaster(),
              "CELL": 25.0, "LENGTH_MI": 1.5, "HALF_WIDTH_MI": 0.5, "MAX_RUNS": 1,
              "DURATION_HR": 3.0, "LISFLOOD_EXE": _exe(), "DAMS_FOLDER": d, "PRODUCTS": False,
              "RECEPTOR_POINTS": fq.FakeSource([_pt(800, 0)])}
    a = algs.SensitivityAlgorithm()
    a.initAlgorithm()
    fb = fq.QgsProcessingFeedback()
    r = a.processAlgorithm(dict(common, SENS_WIDTH="0.7", SENS_TIME="", SENS_N="1.3",
                                SENS_METHODS=False, SENS_CELL=False), fq.QgsProcessingContext(), fb)
    import csv as _csv
    rows = list(_csv.DictReader(open(r["SUMMARY"])))
    assert [x["case"] for x in rows] == ["Base", "Width x0.7", "Manning x1.3"], rows
    assert any("Structures flooded" in m for m in fb.info)
    e = algs.EstimateAlgorithm()
    e.initAlgorithm()
    fb = fq.QgsProcessingFeedback()
    r = e.processAlgorithm(common, fq.QgsProcessingContext(), fb)
    assert any("One LISFLOOD-FP run" in m for m in fb.info), fb.info
    c = algs.CheckAlgorithm()
    c.initAlgorithm()
    fb = fq.QgsProcessingFeedback()
    r = c.processAlgorithm({}, fq.QgsProcessingContext(), fb)      # found through NDBREACH_LISFLOOD
    assert r["RESULT"].startswith("OK LISFLOOD-FP"), r["RESULT"]
    shutil.rmtree(d)


@model_test
def cascade_algorithm_orders_dams_and_writes_rasters():
    algs, fq = _patched_algs()
    d = tempfile.mkdtemp()
    def dm(x, nid, order, h, s, top):
        return fq.FakeFeature(fq.FakeGeometry("point", [[(x, 0)]]), NIDID=nid, DAM_HEIGHT=h,
                              MAX_STORAGE=s, SURFACE_AREA=30.0, TOP_OF_DAM=top, ORD=order)
    src = fq.FakeSource([dm(2600, "B", 2, 10.0, 60.0, TOP_M / FT - 3.0 / FT),
                         dm(0, "A", 1, 25.0, 500.0, 1850.0)])       # listed out of order
    class Terr(SyntheticTerrain):
        pass
    orig = algs._Base.gis
    def gis(self, parameters, context, crs, pt):
        return (SyntheticTerrain(_two_dam_valley), npy_writer, fq.QgsCoordinateTransform(),
                fq.QgsCoordinateReferenceSystem("EPSG:26914"))
    algs._Base.gis = gis
    a = algs.CascadeAlgorithm()
    a.initAlgorithm()
    fb = fq.QgsProcessingFeedback()
    r = a.processAlgorithm({"DAMS": src, "F_ORDER": "ORD", "FAIL_RULE": 1, "DEM": _FakeRaster(),
                            "CELL": 20.0, "LENGTH_MI": 1.0, "HALF_WIDTH_MI": 0.5,
                            "STEP_MI": 1.0, "MAX_RUNS": 3, "DURATION_HR": 4.0,
                            "LISFLOOD_EXE": _exe(), "DAMS_FOLDER": d, "PRODUCTS": False},
                           fq.QgsProcessingContext(), fb)
    txt = open(r["REPORT"]).read()
    assert txt.index("Dams upstream to downstream: A, B") > 0
    assert os.path.exists(os.path.join(r["FOLDER"], "Cascade Depth (Max).tif"))
    algs._Base.gis = orig
    shutil.rmtree(d)


# The parameters of ndhazard:classify (ND Dam Hazard Classification Helper 2.9.x), as that
# plugin defines them: name, type, description, optional.
_NDHAZARD_CLASSIFY = [
    ("DAM_FOLDER", "file", "Dam project folder", False),
    ("DEPTH", "raster", "Breach: maximum depth (ft) - empty: found in the dam folder", True),
    ("VELOCITY", "raster", "Breach: maximum velocity (ft/s)", True),
    ("ARRIVAL", "raster", "Breach: arrival time (h)", True),
    ("DXV", "raster", "Breach: max depth x velocity (ft2/s)", True),
    ("BASE_DEPTH", "raster", "No-breach baseline: max depth", True),
    ("BASE_VELOCITY", "raster", "No-breach baseline: max velocity", True),
    ("BASE_ARRIVAL", "raster", "No-breach baseline: arrival", True),
    ("ROADS", "vector", "Road lines", True),
    ("SUBJECT_NID", "string", "NID ID of the dam being classified", True),
    ("CONFIG_JSON", "file", "Advanced: JSON file with pipeline settings", True),
]


def _runner_dam_folder():
    d = tempfile.mkdtemp()
    for n in ("Depth (Max).tif", "Velocity (Max).tif", "Arrival Time.tif", "WSE (Max).tif",
              "Baseline Depth (Max).tif", "Baseline Velocity (Max).tif",
              "Baseline Arrival Time.tif", "Incremental Depth (Max).tif"):
        open(os.path.join(d, n), "w").write("x")
    return d


@test
def handoff_to_ndhazard_passes_the_dam_folder_only():
    """3.1.1: the breach depth went to BASE_DEPTH and DXV by keyword before - the breach
    was its own baseline and the classifier charged nothing to the dam."""
    from nd_breach_runner.core import handoff
    d = _runner_dam_folder()
    alg = _Alg("ndhazard:classify", [_P(n, t, desc, optional=o)
                                     for n, t, desc, o in _NDHAZARD_CLASSIFY])
    notes = []
    p = handoff.build_parameters(alg, "ND00123", d, os.path.dirname(d), handoff.rasters_in(d),
                                 notes=notes)
    assert p == {"DAM_FOLDER": d, "SUBJECT_NID": "ND00123"}, p
    assert any(n.startswith("BASE_DEPTH: left to the classifier") for n in notes), notes
    # v3.2.0: a dam ID that is not an NID ID is not handed over as the NID
    p = handoff.build_parameters(alg, "DWR-5289", d, os.path.dirname(d), handoff.rasters_in(d))
    assert p == {"DAM_FOLDER": d}, p
    shutil.rmtree(d)


@test
def handoff_never_gives_the_breach_raster_as_baseline_or_product():
    from nd_breach_runner.core import handoff
    d = _runner_dam_folder()
    r = handoff.rasters_in(d)
    # no folder parameter: rasters are filled, each with its own file
    alg = _Alg("x:classify", [
        _P("DEPTH", "raster", "Breach: maximum depth"),
        _P("BASE_DEPTH", "raster", "No-breach baseline: max depth"),
        _P("BASE_VEL", "raster", "Velocity without breach", optional=True),
        _P("DXV", "raster", "max depth x velocity", optional=True),
        _P("INC", "raster", "Incremental depth", optional=True)])
    p = handoff.build_parameters(alg, "X", d, d, r)
    assert p["DEPTH"].endswith(os.sep + "Depth (Max).tif")
    assert p["BASE_DEPTH"].endswith("Baseline Depth (Max).tif")
    assert p["BASE_VEL"].endswith("Baseline Velocity (Max).tif")
    assert "DXV" not in p and "INC" not in p
    # a required product raster is never guessed
    alg2 = _Alg("x:classify", [_P("DEPTH", "raster", "Depth"),
                               _P("DV", "raster", "Depth x velocity")])
    try:
        handoff.build_parameters(alg2, "X", d, d, r)
        raise AssertionError("expected an error")
    except handoff.HandoffError as e:
        assert "DV" in str(e) and "not filled by keyword" in str(e)
    # a required baseline with no baseline raster in the folder is an error, not the breach
    e2 = tempfile.mkdtemp()
    open(os.path.join(e2, "Depth (Max).tif"), "w").write("x")
    alg3 = _Alg("x:classify", [_P("BASE_DEPTH", "raster", "Baseline depth")])
    try:
        handoff.build_parameters(alg3, "X", e2, e2, handoff.rasters_in(e2))
        raise AssertionError("expected an error")
    except handoff.HandoffError as e:
        assert "BASE_DEPTH" in str(e)
    # the mapping still wins
    p3 = handoff.build_parameters(alg3, "X", e2, e2, {}, {"BASE_DEPTH": "{folder}/b.tif"})
    assert p3["BASE_DEPTH"] == e2 + "/b.tif"
    # the ND class is read before the TM 11 class
    assert handoff.find_class({"OUTPUT_FOLDER": d, "TM11_CLASS": "Significant",
                               "ND_CLASS": "High"}) == "High"
    shutil.rmtree(d)
    shutil.rmtree(e2)


@test
def classify_algorithm_hands_off_and_merges():
    from nd_breach_runner.tests import fake_qgis as fq
    fq.install()
    import importlib
    import nd_breach_runner.algorithms as algs
    importlib.reload(algs)
    import nd_breach_runner.algorithms_tools as tools
    importlib.reload(tools)
    from nd_breach_runner.core import handoff
    d = tempfile.mkdtemp()
    for n in ("Depth (Max).tif", "Velocity (Max).tif", "Arrival Time.tif", "WSE (Max).tif"):
        open(os.path.join(d, n), "w").write("x")
    alg = _Alg("ndhazard:classify", [_P("DEPTH", "raster", "Depth"), _P("OUT", "fileDestination",
                                                                        "Out", dest=True)])
    handoff._reg = lambda: _Reg([alg])
    handoff._run = lambda a, p, fb=None: {"HAZARD_CLASS": "Significant", "OUT": p["OUT"]}
    a = tools.ClassifyAlgorithm()
    a.initAlgorithm()
    r = a.processAlgorithm({"MODE": 0, "FOLDER": d, "DAM_ID": "X"}, fq.QgsProcessingContext(),
                           fq.QgsProcessingFeedback())
    assert "Significant" in r["RESULT"]
    shutil.rmtree(d)


@test
def merge_csv_prefers_the_dam_id_column_and_mapping_errors_are_clear():
    from nd_breach_runner.core import handoff
    d = tempfile.mkdtemp()
    a, b, o = (os.path.join(d, n) for n in ("a.csv", "b.csv", "o.csv"))
    open(a, "w").write("Dam,Peak\nND00001,100\nND00002,200\n")
    open(b, "w").write("Dam_Name,NID_ID,Class\nFoo,ND00002,High\nBar,ND00001,Low\n")
    handoff.merge_csv(a, b, o)
    rows = list(csv.DictReader(open(o)))
    assert [r["Class"] for r in rows] == ["Low", "High"], rows
    assert [r["Dam_Name"] for r in rows] == ["Bar", "Foo"], rows

    class P:
        def __init__(s, n): s.n = n
        def name(s): return s.n
        def description(s): return s.n
        def type(s): return "string"
        def defaultValue(s): return None
        def flags(s): return 0
    class A:
        def id(s): return "x:y"
        def parameterDefinitions(s): return [P("K")]
    try:
        handoff.build_parameters(A(), "d", d, d, {}, {"K": "{velocity}"})
        assert False, "expected HandoffError"
    except handoff.HandoffError as e:
        assert "velocity" in str(e)
    shutil.rmtree(d)


def _load_extra_test_modules():
    """tests/test_*.py register their tests through tests.registry."""
    import importlib
    import pkgutil
    from nd_breach_runner import tests as pkg
    for m in sorted(pkgutil.iter_modules(pkg.__path__), key=lambda m: m.name):
        if m.name.startswith("test_"):
            importlib.import_module("nd_breach_runner.tests." + m.name)


def main():
    _load_extra_test_modules()
    run_models = "--models" in sys.argv
    todo = TESTS + (MODEL_TESTS if run_models else [])
    failed = 0
    for fn in todo:
        try:
            fn()
            print("ok    ", fn.__name__)
        except Exception:
            failed += 1
            print("FAIL  ", fn.__name__)
            traceback.print_exc()
    print("\n{} tests, {} failed".format(len(todo), failed))
    return 1 if failed else 0


if __name__ == "__main__":
    code = main()
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)              # offscreen QGIS can crash on teardown; the result stands
