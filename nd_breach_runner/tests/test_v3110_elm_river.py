"""Tests of 3.11.0, the fixes from the Elm River Dam 1 run: the near-dam FV1 run closes the edges
that face the reservoir, lasts the hydrograph plus the travel time, and is checked before it is
merged; the dam line is raised to the top of dam in the model DEM; the clock is lengthened when
the flood is still moving; the refine settings no longer force a rerun of the finished runs."""
import json
import os
import shutil
import tempfile

import numpy as np

from nd_breach_runner.tests.registry import test
from nd_breach_runner.tests.test_v320 import fake_models, _dam, _settings

FT = 0.3048


def _tmp():
    return tempfile.mkdtemp(prefix="ndbr_v3110_")


@test
def v3110_upstream_edges_face_away_from_the_flow():
    from nd_breach_runner.core import refine
    assert refine.upstream_edges((0.0, 1.0)) == ("S",)
    assert refine.upstream_edges((0.0, -1.0)) == ("N",)
    assert refine.upstream_edges((1.0, 0.0)) == ("W",)
    assert refine.upstream_edges((-1.0, 0.0)) == ("E",)
    assert refine.upstream_edges((0.7071, 0.7071)) == ("S", "W")
    assert refine.upstream_edges((0.7071, -0.7071)) == ("N", "W")
    # mostly east with a little north: the side edges stay free
    assert refine.upstream_edges((0.95, 0.31)) == ("W",)


@test
def v3110_closed_edges_are_left_out_of_the_bci_file():
    from nd_breach_runner.core import lisflood as lf
    from nd_breach_runner.core.breach import Hydrograph
    from nd_breach_runner.core.grid import Grid
    d = _tmp()
    try:
        dem = Grid(np.full((20, 20), 100.0), 0.0, 400.0, 20.0)
        hy = Hydrograph(np.array([0.0, 600.0, 1200.0]), np.array([10.0, 10.0, 0.0]), "t")
        lines = {}
        for tag, closed in (("open", ()), ("closed", ("S", "w"))):
            st = lf.LisfloodSettings("exe", solver=lf.SOLVER_FV1, closed_edges=closed)
            lf.write_inputs(os.path.join(d, tag), dem, (200.0, 200.0), hy, st)
            lines[tag] = open(os.path.join(d, tag, "run.bci")).read().splitlines()
        free = lambda ls: sorted(l.split()[0] for l in ls if l.endswith("FREE"))  # noqa: E731
        assert free(lines["open"]) == ["E", "N", "S", "W"]
        assert free(lines["closed"]) == ["E", "N"]
        assert any(l.startswith("P ") for l in lines["closed"])
        try:
            lf.LisfloodSettings("exe", closed_edges=("X",))
            assert False, "expected ModelError"
        except lf.ModelError as e:
            assert "unknown domain edge" in str(e)
    finally:
        shutil.rmtree(d)


def _mass(path, rows):
    with open(path, "w") as f:
        f.write("Time Tstep MinTstep NumTsteps Area Vol Qin Hds Qout Qerror Verror Rain\n")
        for t, vol, qin, qout in rows:
            f.write("{} 1 1 10 100 {} {} 0 {} 0 0 0\n".format(t, vol, qin, qout))


@test
def v3110_water_coming_in_after_the_hydrograph_is_measured_from_the_mass_file():
    from nd_breach_runner.core import lisflood as lf
    d = _tmp()
    try:
        p = os.path.join(d, "run.mass")
        # hydrograph to 1000 s; afterwards no inflow
        _mass(p, [(0, 0, 0, 0), (500, 5e3, 10, 0), (1000, 1e4, 10, 0), (1500, 1e4, 0, 1),
                  (2000, 1e4, 0, 1)])
        assert lf.late_inflow(p, 1000.0) == (0.0, 0.0, 0.0), lf.late_inflow(p, 1000.0)
        # the edge imports 20 m3/s from 1500 s: 20 x 500 s after the one at 1500 s
        _mass(p, [(0, 0, 0, 0), (1000, 1e4, 10, 0), (1500, 1e4, 20, 0), (2000, 2e4, 20, 0)])
        vol, qmax, tmax = lf.late_inflow(p, 1000.0)
        assert qmax == 20.0 and 9.0e3 < vol <= 2.0e4 and tmax == 1500.0, (vol, qmax, tmax)
        # under the 0.5 m3/s tolerance nothing counts
        _mass(p, [(0, 0, 0, 0), (1000, 1e4, 10, 0), (2000, 1e4, 0.2, 0)])
        assert lf.late_inflow(p, 1000.0)[0] == 0.0
    finally:
        shutil.rmtree(d)


@test
def v3110_the_fine_run_is_rejected_when_the_box_holds_more_water_than_went_in():
    from nd_breach_runner.core import refine
    good = {"injected_acft": 7870.0, "max_domain_volume_ratio": 0.98,
            "late_boundary_inflow_pct": 0.0, "max_volume_error_pct": 0.1, "max_depth_ft": 22.0,
            "initial_water_acft": 0.0}
    assert refine.check_fine(good, 21.5) == []
    # Elm River Dam 1 (40 m): 6.5e10 m3 in the box against 9.7e6 m3 injected
    blown = dict(good, max_domain_volume_ratio=6700.0, late_boundary_inflow_pct=90000.0,
                 max_depth_ft=39137.0)
    why = refine.check_fine(blown, 21.5)
    assert len(why) == 3 and any("ac-ft against" in w for w in why), why
    assert any("came in through the edges" in w for w in why)
    assert any("maximum depth is 39,137" in w for w in why)
    # a pond that starts full is not water the edge brought in
    pond = dict(good, max_domain_volume_ratio=1.5, initial_water_acft=4000.0)
    assert refine.check_fine(pond, 21.5) == []
    assert refine.check_fine({}, 21.5)               # no statistics: not trusted


@test
def v3110_the_fine_run_lasts_the_hydrograph_plus_travel_time_not_the_main_clock():
    from nd_breach_runner.core import refine
    from nd_breach_runner.core.breach import Hydrograph
    from nd_breach_runner.core.units import MILE
    st = _settings()
    hy = Hydrograph(np.array([0.0, 10000.0, 16760.0]), np.array([0.0, 1000.0, 0.0]), "t")
    t = refine.fine_sim_time(st, hy, 1.0, 70 * 3600.0)
    expect = 16760.0 + 2.0 * 1.3 * MILE / 0.5 + 3600.0
    assert abs(t - expect) < 1.0 and t < 10 * 3600.0, t
    assert refine.fine_sim_time(st, hy, 1.0, 3 * 3600.0) == 3 * 3600.0       # never longer
    short = Hydrograph(np.array([0.0, 60.0]), np.array([0.0, 0.0]), "t")
    assert refine.fine_sim_time(_settings(front_speed_mps=50.0), short, 0.05, 70 * 3600.0) \
        == 2 * 3600.0                                                        # at least 2 h
    f = refine.fine_settings(st, 1.0, 25.0, t, hy, None)
    assert f.close_upstream_edges is True and st.close_upstream_edges is False
    assert abs(f.sim_time_hr - t / 3600.0) < 1e-9


def _ridge(nx=120, ny=120, dx=10.0, crest=100.0, low_at=None):
    """A N-S dam crest along x = 600 m with 10 m of ground dropping away each side, a low
    spot (smoothed crest) at row `low_at`, ending at rows 25 and 95 (the valley walls rise)."""
    from nd_breach_runner.core.grid import Grid
    xs = (np.arange(nx) + 0.5) * dx
    ys = ny * dx - (np.arange(ny) + 0.5) * dx
    X, Y = np.meshgrid(xs, ys)
    z = 90.0 + np.zeros_like(X)
    on = (np.abs(X - 600.0) < 60.0) & (Y > 250.0) & (Y < 950.0)
    z = np.where(on, np.maximum(z, crest - np.abs(X - 600.0) / 6.0), z)
    if low_at is not None:
        z[low_at, 58:63] -= 1.5
    return Grid(z, 0.0, ny * dx, dx)


@test
def v3110_the_crest_raise_lifts_only_the_cells_under_the_line_and_leaves_the_dem_alone():
    from nd_breach_runner.core import conditioning as cond
    g = _ridge(low_at=60)
    before = g.data.copy()
    line = np.array([[600.0, 250.0], [600.0, 950.0]])
    ed = cond.Edits(crests=[cond.CrestRaise(line, 100.0)])
    out, _init, counts = ed.apply(g)
    assert np.array_equal(g.data, before)                       # the source grid is untouched
    n = counts["crests:dam line raised to top of dam"]
    assert n > 0
    X, Y = np.meshgrid(g.x_centers(), g.y_centers())
    under = (np.abs(X - 600.0) <= 0.75 * g.dx) & (Y >= 240.0) & (Y <= 960.0)
    assert np.all(out.data[under] >= 100.0 - 1e-9)
    changed = out.data != before
    assert np.all(under[changed])                               # nothing else moved
    # higher ground is never lowered
    g2 = _ridge(crest=105.0)
    out2, _i, _c = cond.Edits(crests=[cond.CrestRaise(line, 100.0)]).apply(g2)
    mid = (Y > 260.0) & (Y < 940.0)
    assert np.array_equal(out2.data[mid], g2.data[mid])
    assert not cond.Edits().empty() is False and not ed.empty()
    assert ed.copy().crests == ed.crests and ed.copy().crests is not ed.crests


@test
def v3110_the_dam_line_is_traced_along_the_ridge_and_stops_at_the_abutments():
    from nd_breach_runner.core import crest
    g = _ridge(low_at=60)
    line, note = crest.trace_crest(g, (600.0, 600.0), 10.0, 700.0 / 0.3048)
    assert line is not None, note
    assert np.all(np.abs(line[:, 0] - 600.0) < 25.0), line[:, 0]
    assert line[:, 1].min() < 330.0 and line[:, 1].max() > 880.0       # runs almost the whole dam
    assert line[:, 1].min() > 200.0 and line[:, 1].max() < 1000.0      # and no farther
    assert "traced" in note
    # flat ground: no ridge, no line
    from nd_breach_runner.core.grid import Grid
    flat = Grid(np.full((60, 60), 90.0), 0.0, 600.0, 10.0)
    line, note = crest.trace_crest(flat, (300.0, 300.0), 10.0, None)
    assert line is None and "no ridge" in note


@test
def v3110_a_run_raises_the_dam_line_in_the_model_dem_and_says_so():
    from nd_breach_runner.core import workflow
    from nd_breach_runner.core.grid import Grid
    from nd_breach_runner.tests.synthetic import SyntheticTerrain, npy_writer, TOP_M
    d = _tmp()
    try:
        out = {}
        for tag, raise_ in (("off", False), ("on", True)):
            calls, logs = [], []
            with fake_models(calls=calls):
                r = workflow.run_dam(_dam(), (0.0, 0.0),
                                     _settings(raise_crest=raise_, max_iterations=1),
                                     SyntheticTerrain(), npy_writer, os.path.join(d, tag),
                                     log=logs.append)
            out[tag] = (Grid.read_asc(os.path.join(calls[0], "run.dem.asc")), r, logs)
        off, on = out["off"][0], out["on"][0]
        under = on.data != off.data                           # the cells the raise changed
        assert under.sum() > 8
        assert np.all(np.abs(on.data[under] - TOP_M) < 2e-3)  # now at the top of dam
        assert off.data[under].max() < TOP_M - 0.5            # the coarse DEM had a low crest
        rep = json.load(open(out["on"][1].report_json))
        cr = rep["crest_raise"]
        assert cr["applied"] and cr["cells_raised_on_the_model_grid"] > 0
        assert cr["top_of_dam_ft"] == 1850.0 and cr["line_ft"] > 500.0
        assert "traced" in cr["reason"]
        assert any(m.startswith("Dam line raised to the top of dam (1850.0 ft)")
                   for m in out["on"][2]), out["on"][2]
        assert rep["dem_conditioning"]["edits"]["crests:dam line raised to top of dam"] > 0
        assert json.load(open(out["off"][1].report_json))["crest_raise"]["applied"] is False
        # the line you give is used as it is, and a crest with no top of dam is not raised
        calls = []
        with fake_models(calls=calls):
            r = workflow.run_dam(_dam(), (0.0, 0.0), _settings(
                raise_crest=True, max_iterations=1,
                crest_line_xy=[(0.0, -200.0), (0.0, 200.0)]),
                SyntheticTerrain(), npy_writer, os.path.join(d, "given"))
        assert json.load(open(r.report_json))["crest_raise"]["reason"] == \
            "the dam crest line you gave"
        with fake_models():
            r = workflow.run_dam(_dam(top_of_dam_ft=None), (0.0, 0.0),
                                 _settings(raise_crest=True, max_iterations=1),
                                 SyntheticTerrain(), npy_writer, os.path.join(d, "notop"))
        cr = json.load(open(r.report_json))["crest_raise"]
        assert not cr["applied"] and "no top of dam" in cr["reason"]
    finally:
        shutil.rmtree(d)


@test
def v3110_a_settings_object_reused_for_the_next_dam_does_not_keep_the_old_dam_line():
    from nd_breach_runner.core import workflow
    from nd_breach_runner.tests.synthetic import SyntheticTerrain, npy_writer
    d = _tmp()
    try:
        st = _settings(raise_crest=True, max_iterations=1)
        with fake_models():
            workflow.run_dam(_dam(), (0.0, 0.0), st, SyntheticTerrain(), npy_writer,
                             os.path.join(d, "a"))
            assert st.edits is not None and len(st.edits.crests) == 1
            workflow.run_dam(_dam(top_of_dam_ft=None), (0.0, 0.0), st, SyntheticTerrain(),
                             npy_writer, os.path.join(d, "b"))
        assert st.edits is None or not st.edits.crests
    finally:
        shutil.rmtree(d)


@test
def v3110_the_clock_is_lengthened_while_the_flood_still_moves_instead_of_stopping():
    from nd_breach_runner.core import workflow
    from nd_breach_runner.tests.synthetic import SyntheticTerrain, npy_writer
    d = _tmp()
    try:
        # the stub's flood front reaches 0.79 h at the east end; a 0.5 h clock is "still moving"
        # but the water at the edge is shallower than the tolerance, so no edge is over
        for tag, mx, want_runs in (("lengthen", 6.0, 3), ("fixed", None, 1)):
            calls = []
            with fake_models(depth_m=0.2, calls=calls):
                r = workflow.run_dam(_dam(), (0.0, 0.0), _settings(
                    sim_time_hr=0.5, sim_max_hr=mx, max_iterations=4),
                    SyntheticTerrain(), npy_writer, os.path.join(d, tag))
            assert len(calls) == want_runs, (tag, calls)
            runs = json.load(open(r.report_json))["lisflood_runs"]
            if mx:
                assert [r_["sim_time_hr"] for r_ in runs] == [0.5, 0.75, 1.12], runs
            else:
                assert any("still advancing" in w for w in r.warnings)
    finally:
        shutil.rmtree(d)


@test
def v3110_a_failed_fine_run_keeps_the_acc_results_and_the_report_says_why():
    from nd_breach_runner.core import workflow, impacts
    from nd_breach_runner.core import lisflood as lf
    from nd_breach_runner.tests.synthetic import SyntheticTerrain, npy_writer
    d = _tmp()
    try:
        rec = impacts.Receptors()
        rec.add_points([[800.0, 0.0]])
        calls, logs = [], []
        with fake_models(calls=calls):
            orig = lf.read_results

            def read(wd, dem, stt, hy, name="run"):
                env = orig(wd, dem, stt, hy, name)
                if "refine_near_dam" in wd:
                    env.velocity.data[:] = np.where(np.isfinite(env.velocity.data), 3.6, np.nan)
                    env.stats.update({"injected_acft": 1000.0, "max_domain_volume_ratio": 500.0,
                                      "late_boundary_inflow_pct": 400.0,
                                      "max_depth_ft": 39137.0})
                return env
            lf.read_results = read
            r = workflow.run_dam(_dam(), (0.0, 0.0), _settings(
                receptors=rec, refine_near=True, max_iterations=1),
                SyntheticTerrain(), npy_writer, os.path.join(d, "T1"), log=logs.append)
        rep = json.load(open(r.report_json))
        info = rep["refine_near_dam"]
        assert not info["applied"] and len(info["rejected"]) == 3
        assert "NOT merged" in info["warning"]
        assert any(w.startswith("Near-dam refine: the fine FV1 run was rejected") for w in
                   r.warnings), r.warnings
        vel = np.load(os.path.splitext(r.final["velocity"])[0] + ".npy")
        assert abs(np.nanmax(vel) - 1.2 / FT) < 0.01             # the ACC velocity, not 3.6
        md = open(r.report_md).read()
        assert "**Rejected: the fine FV1 run failed its checks" in md
        assert "refine_near_dam" in json.load(open(r.manifest))["extra"]
        assert rep["summary"].get("refine_near_dam") is None
    finally:
        shutil.rmtree(d)


@test
def v3110_the_near_dam_fv1_run_closes_its_upstream_edge_and_lasts_hours_not_the_main_clock():
    from nd_breach_runner.core import workflow, impacts
    from nd_breach_runner.tests.synthetic import SyntheticTerrain, npy_writer
    d = _tmp()
    try:
        rec = impacts.Receptors()
        rec.add_points([[800.0, 0.0]])
        calls = []
        with fake_models(calls=calls):
            workflow.run_dam(_dam(), (0.0, 0.0), _settings(
                receptors=rec, refine_near=True, max_iterations=1, sim_time_hr=70.0,
                sim_max_hr=None), SyntheticTerrain(), npy_writer, os.path.join(d, "T1"))
        main = open(os.path.join(calls[0], "run.bci")).read().splitlines()
        fine = open(os.path.join(calls[1], "run.bci")).read().splitlines()
        free = lambda ls: sorted(l.split()[0] for l in ls if l.endswith("FREE"))  # noqa: E731
        assert free(main) == ["E", "N", "S", "W"]
        assert "W" not in free(fine) and "E" in free(fine), fine     # the valley runs east
        par = {l.split()[0]: l.split()[1] for l in open(os.path.join(
            calls[1], "run.par")).read().splitlines() if len(l.split()) > 1}
        assert float(par["sim_time"]) < 20 * 3600.0, par["sim_time"]
        assert float(open(os.path.join(calls[0], "run.par")).read().split("sim_time")[1]
                     .split()[0]) == 70 * 3600.0
    finally:
        shutil.rmtree(d)


@test
def v3110_changing_only_the_refine_settings_keeps_the_checkpoint_key():
    from nd_breach_runner.core import workflow
    a = workflow.key_settings(workflow.RunSettings(refine_near=False))
    b = workflow.key_settings(workflow.RunSettings(refine_near=True, refine_dist_mi=2.0,
                                                   refine_cell_m=5.0))
    assert a == b
    assert workflow.key_settings(workflow.RunSettings(raise_crest=False)) != a
