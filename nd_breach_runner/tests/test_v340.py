"""Tests of the 3.4.0 items: the cell-size convergence check of the sensitivity tool (item 1),
the near-dam FV1 refine (item 2), buildings as obstructions (item 3) and the feet audit of the
parameter labels (item 4). No LISFLOOD-FP and no QGIS: the model runs go through
workflow.run_dam with the LISFLOOD-FP call and its result reading stubbed (test_v320.fake_models)
on the synthetic valley, and the Processing tools run against tests/fake_qgis.py."""
import csv
import json
import os
import re
import shutil
import sys
import tempfile

import numpy as np

from nd_breach_runner.tests.registry import test
from nd_breach_runner.tests.test_v320 import fake_models, _dam, _settings, _FakeRaster

FT = 0.3048


def _tmp():
    return tempfile.mkdtemp(prefix="ndbr_v340_")


def _grid(nx, ny, dx, x0=0.0, y0=None):
    from nd_breach_runner.core.grid import Grid
    return Grid(np.zeros((ny, nx)), x0, ny * dx / 2.0 if y0 is None else y0, dx)


def _band(grid, depth_m, half_m, x_from=0.0):
    X, Y = np.meshgrid(grid.x_centers(), grid.y_centers())
    d = np.where((X > x_from) & (np.abs(Y) < half_m), depth_m, np.nan)
    return {"depth": d, "velocity": np.where(np.isfinite(d), 1.0, np.nan),
            "arrival": np.where(np.isfinite(d), X / 3600.0, np.nan),
            "wse": np.where(np.isfinite(d), 100.0 + d, np.nan)}


# ============================================================= item 1: cell-size check
@test
def lisflood_solver_keyword_is_written_to_the_par_file():
    from nd_breach_runner.core import lisflood as lf
    from nd_breach_runner.core.breach import Hydrograph
    from nd_breach_runner.core.grid import Grid
    assert lf.SOLVERS == ("acceleration", "fv1")
    st = lf.LisfloodSettings("x")
    assert st.solver == lf.SOLVER_ACC
    try:
        lf.LisfloodSettings("x", solver="dg9")
        raise AssertionError("expected ModelError")
    except lf.ModelError as e:
        assert "unknown LISFLOOD-FP solver" in str(e)
    d = _tmp()
    try:
        dem = Grid(np.full((10, 20), 100.0), 0.0, 200.0, 20.0)
        hy = Hydrograph(np.array([0.0, 600.0]), np.array([10.0, 0.0]), "t")
        for solver in lf.SOLVERS:
            wd = os.path.join(d, solver)
            par = lf.write_inputs(wd, dem, (30.0, 100.0), hy,
                                  lf.LisfloodSettings("x", solver=solver))
            lines = open(par).read().splitlines()
            assert solver in lines and (set(lf.SOLVERS) - {solver}).isdisjoint(lines), lines
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
def cell_variant_picks_half_or_the_next_coarser_factor_that_fits_the_budget():
    from nd_breach_runner.core import sensitivity as sens
    assert sens.DEFAULT_MAX_CELLS == 4000000
    p = sens.cell_variant(10.0, 100000, 4000000)
    assert p["ok"] and p["cell_m"] == 5.0 and p["factor"] == 0.5 and p["cells"] == 400000
    assert p["note"].startswith("half the base cell size")
    # half would need 400,000 cells: over a 300,000 budget; x0.6 needs 277,778
    p = sens.cell_variant(10.0, 100000, 300000)
    assert p["ok"] and p["factor"] == 0.6 and p["cell_m"] == 6.0, p
    assert "over the budget" in p["note"] and "next coarser factor" in p["note"], p["note"]
    p = sens.cell_variant(10.0, 100000, 100000)
    assert not p["ok"] and p["cell_m"] is None and "no finer grid fits" in p["note"], p


@test
def compare_grids_gives_the_fit_index_and_the_preview_class_is_from_the_runners_nd52():
    from nd_breach_runner.core import sensitivity as sens, impacts, nd_hazard as nh
    coarse = _grid(20, 10, 20.0)                 # x 0..400, y -100..100
    fine = _grid(40, 20, 10.0)
    cs = _band(coarse, 1.0, 40.0)
    fs = _band(fine, 1.0, 40.0)
    m = sens.compare_grids(coarse, cs, fine, fs, 1.0)
    assert m["fit_index_F"] == 1.0 and m["coarse_cell_m"] == 20.0 and m["fine_cell_m"] == 10.0
    assert m["depth_ft"]["bias"] == 0.0
    # the fine flood is wider and deeper: F drops, the bias is negative (coarse shallower)
    fs2 = _band(fine, 1.5, 90.0)
    m2 = sens.compare_grids(coarse, cs, fine, fs2, 1.0)
    assert m2["fit_index_F"] < 0.7 and m2["depth_ft"]["bias"] < -1.0, m2
    assert m2["wet_acres_reference"] > m2["wet_acres_ours"]
    # the fine arrays are scattered with the nested rules: maximum depth per coarse cell
    fs3 = _band(fine, 1.0, 40.0)
    fs3["depth"][10, 5] = 3.0
    fc = sens.fine_on_coarse(coarse, fine, fs3)
    assert fc["depth"][5, 2] == 3.0
    # preview class: no receptors -> none; a wet structure -> High or Medium per nd_hazard
    assert sens.preview_class(None, coarse, cs) == ("", "no receptor layers: no preview class")
    rec = impacts.Receptors()
    rec.add_points([[100.0, 0.0]], ids=["h1"])
    rec.add_lines([[(50.0, -200.0), (50.0, 200.0)]], impacts.KIND_ROAD, ids=["r1"])
    c, note = sens.preview_class(rec, coarse, cs)
    assert c in (nh.HIGH, nh.MEDIUM) and note.startswith("ND 5.2 preview"), (c, note)
    dry = {k: np.full(coarse.data.shape, np.nan) for k in cs}
    assert sens.preview_class(rec, coarse, dry)[0] == nh.LOW
    assert sens.receptor_maxima(rec, coarse, cs) == (round(1.0 / FT, 2), round(1.0 / FT, 2))
    assert sens.receptor_maxima(None, coarse, cs) == (None, None)
    stable, txt = sens.grid_check_verdict("High", "High", m, {"flooded_ac": 10},
                                          {"flooded_ac": 10})
    assert stable == "yes" and "class_stable_grid: yes" in txt and "CHANGES" not in txt
    stable, txt = sens.grid_check_verdict("Medium", "High", m2, {}, {})
    assert stable == "no" and "CHANGES THE ANSWER" in txt
    stable, txt = sens.grid_check_verdict("", "", m2, {}, {})
    assert stable.startswith("n/a") and "extents differ noticeably" in txt


@test
def sensitivity_cell_check_runs_the_finer_grid_on_the_same_domain_and_reports():
    from nd_breach_runner.core import sensitivity as sens, impacts
    from nd_breach_runner.tests.synthetic import SyntheticTerrain, npy_writer
    d = _tmp()
    try:
        rec = impacts.Receptors()
        rec.add_points([[800.0, 0.0]])
        st = _settings(receptors=rec, max_iterations=1, products=False)
        cases = [sens.Case("Width x0.7", sens.GROUP_WIDTH, width_factor=0.7)]
        calls = []
        with fake_models(calls=calls):
            out = sens.run_sensitivity(_dam(), (0.0, 0.0), st, SyntheticTerrain(), npy_writer,
                                       os.path.join(d, "T1"), cases=cases)
        names = [os.path.relpath(c, d) for c in calls]
        assert names[-1] == os.path.join("T1", "Sensitivity", "Cell_25_m", "_breach_model",
                                         "lisflood"), names
        rows = {r["case"]: r for r in out.rows}
        assert set(rows) == {"Base", "Width x0.7", "Cell 25 m"}
        g = rows["Cell 25 m"]
        assert g["group"] == sens.GROUP_GRID and g["cell_m"] == 25.0 and rows["Base"]["cell_m"] == 50.0
        assert g["fit_index_F"] == 1.0 and g["class_stable_grid"] == "yes", g
        assert rows["Base"]["class_stable_grid"] == "yes" and rows["Base"]["fit_index_F"] == 1.0
        assert "class_stable_grid" not in rows["Width x0.7"]
        for r in out.rows:
            assert r["preview_class"] in ("High", "Medium", "Low"), r
            assert r["max_depth_at_receptors_ft"] > 0 and r["max_velocity_at_receptors_fps"] > 0
        assert out.grid_check["pick"]["factor"] == 0.5 and out.grid_check["compare"]["fit_index_F"] == 1.0
        assert any("class_stable_grid: yes" in f for f in out.flags), out.flags
        hdr = open(out.summary_csv).readline().strip().split(",")
        for col in ("cell_m", "preview_class", "max_depth_at_receptors_ft",
                    "max_velocity_at_receptors_fps", "fit_index_F", "class_stable_grid"):
            assert col in hdr, (col, hdr)
        csv_rows = list(csv.DictReader(open(out.summary_csv)))
        assert [r["case"] for r in csv_rows] == ["Base", "Width x0.7", "Cell 25 m"]
        html = open(out.report_html, encoding="utf-8").read()
        assert "Cell-size convergence (v3.4.0)" in html and "class_stable_grid: yes" in html
        assert "not part of the envelope" in html and "preview_class:" in html
        # the finer run is not in the envelope (its grid differs): the envelope is the base grid
        env = np.load(os.path.splitext(out.envelope["Sensitivity Depth (High).tif"])[0] + ".npy")
        assert env.shape == out.base.grid.data.shape
        # over the budget: the next coarser factor that fits, and the report says so
        base_cells = out.base.grid.data.size
        none = [sens.Case("Base", sens.GROUP_BASE)]          # an empty list means "defaults"
        with fake_models():
            out2 = sens.run_sensitivity(_dam(), (0.0, 0.0), st, SyntheticTerrain(), npy_writer,
                                        os.path.join(d, "T2"), cases=none,
                                        max_cells=int(base_cells * 3.5))
        g2 = [r for r in out2.rows if r["group"] == sens.GROUP_GRID]
        assert len(g2) == 1 and g2[0]["cell_m"] == 30.0, g2
        assert "next coarser factor" in out2.grid_check["pick"]["note"]
        html2 = open(out2.report_html, encoding="utf-8").read()
        assert "next coarser factor that fits is x0.6" in html2
        # no budget at all: skipped with a warning; and the check can be switched off
        with fake_models():
            out3 = sens.run_sensitivity(_dam(), (0.0, 0.0), st, SyntheticTerrain(), npy_writer,
                                        os.path.join(d, "T3"), cases=none, max_cells=1000)
        assert not [r for r in out3.rows if r["group"] == sens.GROUP_GRID]
        assert any(w.startswith("Cell-size check skipped: no finer grid fits") for w in out3.warnings)
        with fake_models():
            out4 = sens.run_sensitivity(_dam(), (0.0, 0.0), st, SyntheticTerrain(), npy_writer,
                                        os.path.join(d, "T4"), cases=none, cell_check=False)
        assert out4.grid_check is None and len(out4.rows) == 1
        # a finer grid that floods nothing: the class moves and the flag says so
        with fake_models(vary=lambda wd: 0.0 if "Cell_" in wd else 1.0):
            out5 = sens.run_sensitivity(_dam(), (0.0, 0.0), st, SyntheticTerrain(), npy_writer,
                                        os.path.join(d, "T5"), cases=none)
        g5 = [r for r in out5.rows if r["group"] == sens.GROUP_GRID][0]
        assert g5["class_stable_grid"] == "no" and g5["preview_class"] == "Low", g5
        assert g5["fit_index_F"] == 0.0
        assert any("CHANGES THE ANSWER: the grid alone" in f for f in out5.flags), out5.flags
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ============================================================= item 2: near-dam refine
@test
def refine_counts_receptors_near_the_breach_and_decides():
    from nd_breach_runner.core import refine, impacts, structures, workflow, lisflood as lf
    rec = impacts.Receptors()
    rec.add_points([[800.0, 0.0], [5000.0, 0.0]], ids=["near", "far"])
    rec.add_lines([[(1200.0, -100.0), (1200.0, 100.0)]], impacts.KIND_ROAD)
    rec.add_lines([[(4000.0, -100.0), (4000.0, 100.0)]], impacts.KIND_RAIL)
    cul = structures.Structure("culvert", np.array([[900.0, -30.0], [900.0, 30.0]]))
    c = refine.receptors_within(rec, [cul], (0.0, 0.0), 1609.344)
    assert c["structures"] == 1 and c["road_samples"] > 0 and c["rail_samples"] == 0
    assert c["crossings"] == 1 and c["total"] == 2 + c["road_samples"]
    assert c["nearest_ft"] == round(800.0 / FT, 0)
    c0 = refine.receptors_within(None, [], (0.0, 0.0), 1609.344)
    assert c0["total"] == 0 and c0["nearest_ft"] is None
    st = workflow.RunSettings(cell_size_m=10.0)
    assert st.refine_near is False and st.refine_dist_mi == 1.0 and st.refine_cell_m == 0.0
    assert st.lisflood_solver == lf.SOLVER_ACC
    assert refine.fine_cell(st) == 5.0
    st.refine_cell_m = 3.0
    assert refine.fine_cell(st) == 3.0
    dec = refine.decide(st, "LISFLOOD-FP", rec, [cul], (0.0, 0.0))
    assert not dec["refine"] and dec["reason"].startswith("off")
    st.refine_near = True
    dec = refine.decide(st, "LISFLOOD-FP", rec, [cul], (0.0, 0.0))
    assert dec["refine"] and dec["fine_cell_m"] == 3.0 and dec["solver"] == "fv1"
    assert "within 1 mi of the breach" in dec["reason"] and "unreliable" in dec["reason"]
    assert not refine.decide(st, "ANUGA", rec, [cul], (0.0, 0.0))["refine"]
    assert "no structure, road, rail or crossing layer" in refine.decide(
        st, "LISFLOOD-FP", None, [], (0.0, 0.0))["reason"]
    far = rec.subset(rec.ident == "far")
    assert "no receptor lies within 1 mi" in refine.decide(st, "LISFLOOD-FP", far, [],
                                                            (0.0, 0.0))["reason"]
    st.refine_cell_m = 10.0
    assert "not smaller than the main cell" in refine.decide(st, "LISFLOOD-FP", rec, [],
                                                              (0.0, 0.0))["reason"]
    st.refine_cell_m = 0.0
    st.lisflood_solver = lf.SOLVER_FV1
    assert "already uses the fv1 solver" in refine.decide(st, "LISFLOOD-FP", rec, [],
                                                          (0.0, 0.0))["reason"]
    st.lisflood_solver = lf.SOLVER_ACC
    # the fine settings: FV1, half cell, one run, no recursion, the hydrographs handed over
    f = refine.fine_settings(st, 1.0, 5.0, 4 * 3600.0, "HY", "HYB")
    assert f.lisflood_solver == "fv1" and f.cell_size_m == 5.0 and f.max_iterations == 1
    assert f.refine_near is False and f.user_hydrograph_obj == "HY" and f.user_baseline_obj == "HYB"
    assert f.initial_length_mi == 1.3 and f.half_width_mi == 0.5 and f.receptors is None
    assert f.products is False and f.dam_record is None and f.sim_time_hr == 4.0
    assert st.lisflood_solver == lf.SOLVER_ACC                 # the original is untouched


@test
def refine_merge_replaces_only_the_fine_zone_upstream_of_the_plane():
    from nd_breach_runner.core import refine
    main = _grid(40, 20, 10.0)                   # x 0..400, y -100..100
    fine = _grid(40, 20, 2.5, x0=50.0, y0=25.0)  # x 50..150, y -25..25
    ma = _band(main, 1.0, 60.0)
    fa = _band(fine, 2.0, 60.0, x_from=-1.0)
    fa["velocity"][:] = np.where(np.isfinite(fa["depth"]), 5.0, np.nan)
    out, replaced = refine.merge_onto(main, ma, fine, fa, (120.0, 0.0), (1.0, 0.0))
    x = main.x_centers()
    y = main.y_centers()
    inside = (x[None, :] > 50) & (x[None, :] < 120) & (np.abs(y[:, None]) < 25)
    assert np.all(out["depth"][inside] == 2.0) and np.all(out["velocity"][inside] == 5.0)
    # cleared: main cells whose centre lies in the fine bounds (edges included) upstream
    cover = (x[None, :] >= 50) & (x[None, :] <= 120) & (np.abs(y[:, None]) <= 25)
    assert replaced == int(cover.sum()) == 42
    outside_dn = (x[None, :] > 130) & (np.abs(y[:, None]) < 60)
    assert np.all(out["depth"][outside_dn] == 1.0)                # main downstream
    beside = (x[None, :] > 50) & (x[None, :] < 120) & (np.abs(y[:, None]) > 30) & (
        np.abs(y[:, None]) < 60)
    assert np.all(out["depth"][beside] == 1.0)                    # main beside the fine domain
    assert np.all(out["depth"][:, x < 50][np.abs(y) < 60] == 1.0)  # main upstream of fine
    z = refine.zone_stats(main, out, (0.0, 0.0), 100.0)
    assert z["max_velocity_fps"] == round(5.0 / FT, 2) and z["max_depth_ft"] == round(2.0 / FT, 2)


@test
def refine_runs_an_fv1_fine_grid_and_merges_it_when_receptors_sit_near_the_breach():
    from nd_breach_runner.core import workflow, impacts, provenance
    from nd_breach_runner.tests.synthetic import SyntheticTerrain, npy_writer
    d = _tmp()
    try:
        rec = impacts.Receptors()
        rec.add_points([[800.0, 0.0], [5000.0, 0.0]])
        calls, logs = [], []
        st = _settings(receptors=rec, refine_near=True, max_iterations=1)
        # the fine stub run is faster (x3) so the merge is visible in the zone statistics
        with fake_models(calls=calls, vary=lambda wd: 1.0):
            from nd_breach_runner.core import lisflood as lf
            orig = lf.read_results

            def read(wd, dem, stt, hy, name="run"):
                env = orig(wd, dem, stt, hy, name)
                if "refine_near_dam" in wd:
                    env.velocity.data[:] = np.where(np.isfinite(env.velocity.data), 3.6, np.nan)
                return env
            lf.read_results = read
            r = workflow.run_dam(_dam(), (0.0, 0.0), st, SyntheticTerrain(), npy_writer,
                                 os.path.join(d, "T1"), log=logs.append)
        names = [os.path.relpath(c, d) for c in calls]
        assert names == [os.path.join("T1", "_breach_model", "lisflood"),
                         os.path.join("T1", "_breach_model", "refine_near_dam", "_breach_model",
                                      "lisflood")], names
        par_main = open(os.path.join(calls[0], "run.par")).read().splitlines()
        par_fine = open(os.path.join(calls[1], "run.par")).read().splitlines()
        assert "acceleration" in par_main and "fv1" in par_fine and "fv1" not in par_main
        rep = json.load(open(r.report_json))
        info = rep["refine_near_dam"]
        assert info["applied"] and info["fine_cell_m"] == 25.0 and info["main_cell_m"] == 50.0
        assert info["solver"] == "fv1" and info["counts"]["total"] == 1
        assert info["cells_replaced"] > 0 and info["fine_cells"] > 0
        assert info["zone_before_acc"]["max_velocity_fps"] == round(1.2 / FT, 2)
        assert info["zone_after_fv1"]["max_velocity_fps"] == round(3.6 / FT, 2)
        assert info["summary"].startswith("ACC velocities within 1 mi of the breach replaced by "
                                          "an FV1 fine-grid run (25 m cells); 1 receptors inside")
        assert rep["summary"]["refine_near_dam"] == info["summary"]
        assert any(m.startswith("Near-dam refine: 1 receptor(s) within 1 mi") for m in logs), logs
        assert any(m.startswith("Near-dam refine: ACC velocities within") for m in logs)
        assert any(w.startswith("Near-dam refine: ACC velocities") for w in r.warnings)
        assert not any(w.startswith("Stopped after 1 runs") for w in info["fine_warnings"])
        md = open(r.report_md).read()
        assert "## Near-dam refine (v3.4.0)" in md and "| Near the dam (v3.4.0) |" in md
        assert "| Max velocity within 1 mi | 3.94 ft/s | 11.81 ft/s |" in md, md
        man = json.load(open(r.manifest))
        x = man["extra"]["refine_near_dam"]
        assert x["applied"] and x["solver"] == "fv1" and x["receptors_inside"] == 1
        assert man["extra"]["lisflood_solver"] == "acceleration"
        assert provenance.verify_manifest(r.manifest)["ok"]
        # the final velocity raster carries the fine values near the dam, the ACC ones beyond
        vel = np.load(os.path.splitext(r.final["velocity"])[0] + ".npy")
        g = r.grid
        X, Y = np.meshgrid(g.x_centers(), g.y_centers())
        near = (X > 100) & (X < 1500) & (np.abs(Y) < 200)
        far = (X > 2000) & (np.abs(Y) < 200)
        assert abs(np.nanmax(vel[near]) - 3.6 / FT) < 0.01 and abs(np.nanmax(vel[far]) - 1.2 / FT) < 0.01
        assert os.path.exists(os.path.join(info["folder"], "Depth (Max).tif"))
        # not applied: the receptor is too far
        far_only = rec.subset(rec.x > 4000)
        calls, logs = [], []
        with fake_models(calls=calls):
            r2 = workflow.run_dam(_dam(), (0.0, 0.0), _settings(
                receptors=far_only, refine_near=True, max_iterations=1), SyntheticTerrain(),
                npy_writer, os.path.join(d, "T2"), log=logs.append)
        assert len(calls) == 1
        rep2 = json.load(open(r2.report_json))
        assert rep2["refine_near_dam"]["applied"] is False
        assert "no receptor lies within 1 mi" in rep2["refine_near_dam"]["reason"]
        assert any(m.startswith("Near-dam refine not applied: no receptor lies within")
                   for m in logs), logs
        assert "Not applied: no receptor lies within" in open(r2.report_md).read()
        man2 = json.load(open(r2.manifest))
        assert man2["extra"]["refine_near_dam"]["applied"] is False
        # off (default): nothing in the report or the manifest
        with fake_models():
            r3 = workflow.run_dam(_dam(), (0.0, 0.0), _settings(receptors=rec, max_iterations=1),
                                  SyntheticTerrain(), npy_writer, os.path.join(d, "T3"))
        rep3 = json.load(open(r3.report_json))
        assert "refine_near_dam" not in rep3 and "refine_near_dam" not in json.load(
            open(r3.manifest))["extra"]
        # with a no-breach baseline (ambient flow): the fine run has one too and the merged
        # baseline / incremental rasters are written
        calls = []
        with fake_models(calls=calls):
            r4 = workflow.run_dam(_dam(), (0.0, 0.0), _settings(
                receptors=rec, refine_near=True, max_iterations=1, ambient_cfs=100.0),
                SyntheticTerrain(), npy_writer, os.path.join(d, "T4"))
        tails = [os.path.basename(c) for c in calls]
        assert tails == ["lisflood", "lisflood_base", "lisflood", "lisflood_base"], calls
        assert r4.increment and os.path.exists(r4.increment["depth"])
        rep4 = json.load(open(r4.report_json))
        assert rep4["refine_near_dam"]["applied"] and "warning" not in rep4["refine_near_dam"]
        # a failing fine run keeps the ACC results and says so
        from nd_breach_runner.core import lisflood as lf

        def boom(wd, par, stt, progress=None, cancel=None, log=None, name="run"):
            if "refine_near_dam" in wd:
                raise lf.ModelError("fv1 crashed")
            return 60.0
        with fake_models():
            old = lf.run
            lf.run = boom
            try:
                r5 = workflow.run_dam(_dam(), (0.0, 0.0), _settings(
                    receptors=rec, refine_near=True, max_iterations=1), SyntheticTerrain(),
                    npy_writer, os.path.join(d, "T5"))
            finally:
                lf.run = old
        assert any(w.startswith("Near-dam refine failed (ModelError: fv1 crashed)")
                   for w in r5.warnings), r5.warnings
        assert json.load(open(r5.report_json))["refine_near_dam"]["applied"] is False
        assert os.path.exists(r5.final["depth"])
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ============================================================= item 3: buildings
@test
def building_footprints_raise_the_dem_or_set_the_roughness():
    from nd_breach_runner.core import conditioning as cond
    from nd_breach_runner.tests.synthetic import SyntheticTerrain
    terr = SyntheticTerrain()
    bounds = (400.0, -400.0, 1400.0, 400.0)
    raw = terr.read(bounds, 20.0)
    foot = np.array([[700.0, -30.0], [760.0, -30.0], [760.0, 30.0], [700.0, 30.0]])
    tiny = np.array([[1001.0, 101.0], [1003.0, 101.0], [1003.0, 103.0], [1001.0, 103.0]])
    assert cond.building_edits([foot], cond.BUILDINGS_NONE) == []
    try:
        cond.building_edits([foot], "walls")
        raise AssertionError("expected ValueError")
    except ValueError:
        pass
    ed = cond.Edits(buildings=cond.building_edits([foot, tiny], cond.BUILDINGS_RAISE, 10.0))
    assert not ed.empty() and not ed.has_roughness() and not ed.has_water()
    new, init, counts = ed.apply(raw)
    assert init is None and counts["buildings:building 1"] == 9 and counts["buildings:building 2"] == 1
    inside = cond.points_in_polygon(*np.meshgrid(raw.x_centers(), raw.y_centers()), foot)
    top = np.nanmax(raw.masked()[inside]) + 10.0 * FT
    assert np.allclose(new.data[inside], top)                       # one flat block
    assert np.all(new.data[~inside] == raw.data[~inside]) or np.sum(
        new.data[~inside] != raw.data[~inside]) == 1                # only the tiny one else
    r, c = raw.rc(1002.0, 102.0)
    assert new.data[r, c] >= raw.data[r, c] + 10.0 * FT - 1e-9
    # roughness: the ground is unchanged, the n grid is made from the default n
    ed2 = cond.Edits(buildings=cond.building_edits([foot], cond.BUILDINGS_ROUGHNESS, n=0.15))
    assert ed2.has_roughness()
    new2, _, c2 = ed2.apply(raw)
    assert np.allclose(new2.data, raw.data) and c2["buildings:building 1"] == 0
    man, mc = ed2.apply_manning(raw, None, 0.06)
    assert mc["buildings:building 1"] == 9 and man.data.shape == raw.data.shape
    assert np.all(man.data[inside] == 0.15) and np.all(man.data[~inside] == 0.06)
    base_n = raw.like(np.full(raw.data.shape, 0.04))
    man2, _ = ed2.apply_manning(raw, base_n, 0.06)
    assert np.all(man2.data[~inside] == 0.04) and np.all(man2.data[inside] == 0.15)
    assert cond.Edits().apply_manning(raw, None, 0.06) == (None, {})
    txt = cond.describe_buildings(ed, counts)
    assert txt.startswith("Buildings: 2 footprint(s) raised to the highest ground inside each "
                          "plus 10.0 ft") and "(10 cells on the last grid read)" in txt
    assert "screening values you choose" in txt and "point structures are ignored" in txt
    assert cond.describe_buildings(ed2, mc).startswith("Buildings: Manning's n = 0.15 inside 1 "
                                                      "footprint(s) (9 cells")
    assert cond.describe_buildings(None) == "Buildings: not represented (bare terrain)."
    assert cond.BUILDING_RAISE_FT_DEFAULT == 10.0 and cond.BUILDING_N_DEFAULT == 0.15
    # the conditioned terrain hands the models the raised DEM and the n grid
    ct = cond.ConditionedTerrain(terr, ed2, default_n=0.05)
    assert np.allclose(ct.read(bounds, 20.0).data, raw.data)
    m = ct.manning(bounds, 20.0)
    assert np.all(m.data[inside] == 0.15) and np.all(m.data[~inside] == 0.05)
    assert ct.last_manning_counts == {"buildings:building 1": 9}
    ct1 = cond.ConditionedTerrain(terr, ed)
    assert ct1.manning(bounds, 20.0) is None and np.allclose(ct1.read(bounds, 20.0).data, new.data)


@test
def buildings_reach_the_model_dem_and_manning_and_are_recorded_in_report_and_manifest():
    from nd_breach_runner.core import conditioning as cond, workflow
    from nd_breach_runner.core.grid import Grid
    from nd_breach_runner.tests.synthetic import SyntheticTerrain, npy_writer
    d = _tmp()
    try:
        foot = np.array([[700.0, -60.0], [820.0, -60.0], [820.0, 60.0], [700.0, 60.0]])
        ed = cond.Edits(buildings=cond.building_edits([foot], cond.BUILDINGS_RAISE, 12.0))
        calls = []
        with fake_models(calls=calls):
            r = workflow.run_dam(_dam(), (0.0, 0.0), _settings(edits=ed, max_iterations=1),
                                 SyntheticTerrain(), npy_writer, os.path.join(d, "T1"))
        rep = json.load(open(r.report_json))
        assert rep["settings"]["edits"]["buildings"] == 1
        dc = rep["dem_conditioning"]
        assert dc["edits"]["buildings:building 1"] > 0 and dc["buildings"].startswith(
            "Buildings: 1 footprint(s) raised to the highest ground inside each plus 12.0 ft")
        assert rep["summary"]["buildings"].startswith("Buildings: 1 footprint(s) raised")
        md = open(r.report_md).read()
        assert "## DEM conditioning" in md and "| Buildings (v3.4.0) |" in md
        man = json.load(open(r.manifest))
        assert man["extra"]["dem_conditioning"]["buildings"] == dc["buildings"]
        assert man["extra"]["dem_conditioning"]["edits"] == dc["edits"]
        # the DEM LISFLOOD-FP read is raised at the footprint by at least 12 ft
        dem = Grid.read_asc(os.path.join(calls[0], "run.dem.asc"))
        raw = SyntheticTerrain().read(r.grid.bounds, r.grid.dx)
        X, Y = np.meshgrid(raw.x_centers(), raw.y_centers())
        inside = cond.points_in_polygon(X, Y, foot)
        assert inside.sum() >= 4
        assert np.all(dem.data[inside] >= raw.data[inside] + 12.0 * FT - 2e-3)   # .asc: mm
        assert np.allclose(dem.data[~inside], raw.data[~inside], atol=2e-3)
        par = open(os.path.join(calls[0], "run.par")).read()
        assert "manningfile" not in par
        # roughness: a Manning file is written with the footprint's n
        ed2 = cond.Edits(buildings=cond.building_edits([foot], cond.BUILDINGS_ROUGHNESS, n=0.2))
        calls = []
        with fake_models(calls=calls):
            r2 = workflow.run_dam(_dam(), (0.0, 0.0), _settings(edits=ed2, max_iterations=1,
                                                               manning_n=0.045),
                                  SyntheticTerrain(), npy_writer, os.path.join(d, "T2"))
        par = open(os.path.join(calls[0], "run.par")).read()
        assert "manningfile run.n.asc" in par
        n = Grid.read_asc(os.path.join(calls[0], "run.n.asc"))
        X, Y = np.meshgrid(r2.grid.x_centers(), r2.grid.y_centers())   # its own domain
        inside = cond.points_in_polygon(X, Y, foot)
        assert abs(n.data[inside].max() - 0.2) < 1e-6 and abs(n.data[~inside].max() - 0.045) < 1e-6
        rep2 = json.load(open(r2.report_json))
        assert rep2["dem_conditioning"]["manning_edits"]["buildings:building 1"] == int(inside.sum())
        assert rep2["dem_conditioning"]["buildings"].startswith("Buildings: Manning's n = 0.2")
        # the checkpoint key of a run without buildings is unchanged by the new field
        st0 = _settings(edits=cond.Edits(ponds=[cond.PondLower(foot, 1.0)]))
        assert "buildings" not in workflow.key_settings(st0)["edits"]
        assert workflow.key_settings(_settings(edits=ed))["edits"]["buildings"] == 1
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
def qgis_io_reads_points_and_polygon_footprints_from_one_structures_layer():
    from nd_breach_runner.tests import fake_qgis as fq
    fq.install()
    import importlib
    from nd_breach_runner import qgis_io as qio
    importlib.reload(qio)
    sq = fq.FakeGeometry("polygon", [[(10.0, 10.0), (30.0, 10.0), (30.0, 30.0), (10.0, 30.0),
                                      (10.0, 10.0)]])
    pt = fq.FakeGeometry("point", [[(100.0, 100.0)]])
    ln = fq.FakeGeometry("line", [[(0.0, 0.0), (5.0, 5.0)]])
    src = fq.FakeSource([fq.FakeFeature(sq, ID="A"), fq.FakeFeature(pt, ID="B"),
                         fq.FakeFeature(ln, ID="C")])
    xy, ids, foot, counts = qio.points_and_footprints(src, fq.QgsCoordinateTransform(),
                                                      id_field="ID")
    assert counts == {"points": 1, "polygons": 1, "other": 1}
    assert ids == ["A", "B"] and xy.shape == (2, 2)
    assert abs(xy[0][0] - 18.0) < 1e-9 and tuple(xy[1]) == (100.0, 100.0)   # centroid, point
    assert len(foot) == 1 and foot[0].shape == (5, 2)
    assert qio.geometry_kind(sq) == "polygon" and qio.geometry_kind(ln) == "line"


@test
def rundam_tool_reads_footprints_and_the_building_and_refine_parameters():
    from nd_breach_runner.tests.run_tests import _patched_algs
    from nd_breach_runner.core import conditioning as cond
    algs, fq = _patched_algs()
    from nd_breach_runner import algorithms_tools as at
    for cls in (algs.RunDamAlgorithm, algs.BatchAlgorithm, algs.SensitivityAlgorithm,
                algs.CascadeAlgorithm, at.ConditionDemAlgorithm):
        a = cls()
        a.initAlgorithm()
        for key in ("BUILDINGS", "BUILDING_RAISE_FT", "BUILDING_N"):
            assert a.parameterDefinition(key) is not None, (cls.__name__, key)
        p = a.parameterDefinition("RECEPTOR_POINTS")
        assert "polygon" in p.description and len(p.args[0]) == 2, (cls.__name__, p.args)
        assert a.parameterDefinition("BUILDING_RAISE_FT").kwargs["defaultValue"] == 10.0
        assert a.parameterDefinition("BUILDING_N").kwargs["defaultValue"] == 0.15
        assert a.parameterDefinition("BUILDINGS").args[0] == [
            cond.BUILDINGS_LABELS[m] for m in cond.BUILDINGS_MODES]
    for cls in (algs.RunDamAlgorithm, algs.BatchAlgorithm):
        a = cls()
        a.initAlgorithm()
        # v3.12.0: off by default again (on from 3.8.0 to 3.11.x; see test_v3120)
        assert a.parameterDefinition("REFINE_NEAR").kwargs["defaultValue"] is False
        assert a.parameterDefinition("REFINE_DIST_MI").kwargs["defaultValue"] == 1.0
        assert a.parameterDefinition("REFINE_CELL").kwargs["defaultValue"] == 0.0
    d = _tmp()
    try:
        sq = fq.FakeGeometry("polygon", [[(700.0, -40.0), (780.0, -40.0), (780.0, 40.0),
                                          (700.0, 40.0), (700.0, -40.0)]])
        src = fq.FakeSource([fq.FakeFeature(sq, ID="house"),
                             fq.FakeFeature(fq.FakeGeometry("point", [[(900.0, 0.0)]]), ID="p")])
        common = {"DAM_ID": "B1", "DAM_POINT": fq.QgsPointXY(0, 0), "HEIGHT_FT": 25,
                  "STORAGE_ACFT": 500, "TOP_FT": 1850, "AREA_AC": 60, "DEM": _FakeRaster(),
                  "CELL": 50.0, "LENGTH_MI": 1.0, "HALF_WIDTH_MI": 0.5, "MAX_RUNS": 1,
                  "DURATION_HR": 4.0, "LISFLOOD_EXE": sys.executable, "DAMS_FOLDER": d,
                  "RAISE_CREST": False,
                  "PRODUCTS": False, "RECEPTOR_POINTS": src}
        a = algs.RunDamAlgorithm()
        a.initAlgorithm()
        fb = fq.QgsProcessingFeedback()
        with fake_models():
            r = a.processAlgorithm(dict(common, BUILDINGS=1, BUILDING_RAISE_FT=8.0,
                                        REFINE_NEAR=True, REFINE_DIST_MI=0.75, REFINE_CELL=10.0),
                                   fq.QgsProcessingContext(), fb)
        assert any(m.startswith("Structures layer: 1 polygon footprint(s)") for m in fb.info), fb.info
        assert any(m.startswith("Buildings: 1 footprint(s) raised") and "8.0 ft" in m
                   for m in fb.info), fb.info
        rep = json.load(open(os.path.join(r["FOLDER"], "_breach_model", "run_report.json")))
        assert rep["settings"]["edits"]["buildings"] == 1
        assert rep["settings"]["receptors"] == 2                   # centroid + point
        assert rep["settings"]["refine_near"] is True and rep["settings"]["refine_dist_mi"] == 0.75
        assert rep["settings"]["refine_cell_m"] == 10.0
        assert rep["refine_near_dam"]["applied"] and rep["refine_near_dam"]["fine_cell_m"] == 10.0
        assert rep["refine_near_dam"]["counts"]["structures"] == 2
        assert rep["dem_conditioning"]["edits"]["buildings:building 1"] > 0
        # roughness asked for with a points-only layer: said, not done
        pts = fq.FakeSource([fq.FakeFeature(fq.FakeGeometry("point", [[(900.0, 0.0)]]), ID="p")])
        fb = fq.QgsProcessingFeedback()
        with fake_models():
            r = a.processAlgorithm(dict(common, DAM_ID="B2", RECEPTOR_POINTS=pts, BUILDINGS=2),
                                   fq.QgsProcessingContext(), fb)
        assert any("has no polygon footprints (points cannot be used for this)" in m
                   for m in fb.info), fb.info
        rep = json.load(open(os.path.join(r["FOLDER"], "_breach_model", "run_report.json")))
        assert rep["settings"]["edits"] is None and "dem_conditioning" not in rep
        # the sensitivity tool: the cell check is on by default and can be turned off
        s = algs.SensitivityAlgorithm()
        s.initAlgorithm()
        assert s.parameterDefinition("SENS_CELL").kwargs["defaultValue"] is True
        assert s.parameterDefinition("SENS_MAX_CELLS").kwargs["defaultValue"] == 4000000
        fb = fq.QgsProcessingFeedback()
        with fake_models():
            r = s.processAlgorithm(dict(common, DAM_ID="S1", SENS_WIDTH="0.7", SENS_TIME="",
                                        SENS_N="", SENS_METHODS=False),
                                   fq.QgsProcessingContext(), fb)
        rows = list(csv.DictReader(open(r["SUMMARY"])))
        assert [x["case"] for x in rows] == ["Base", "Width x0.7", "Cell 25 m"], rows
        assert rows[2]["class_stable_grid"] == "yes" and rows[2]["fit_index_F"] == "1.0"
        assert any("class_stable_grid: yes" in m for m in fb.info)
        with fake_models():
            r = s.processAlgorithm(dict(common, DAM_ID="S2", SENS_WIDTH="0.7", SENS_TIME="",
                                        SENS_N="", SENS_METHODS=False, SENS_CELL=False),
                                   fq.QgsProcessingContext(), fq.QgsProcessingFeedback())
        rows = list(csv.DictReader(open(r["SUMMARY"])))
        assert [x["case"] for x in rows] == ["Base", "Width x0.7"], rows
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
def conditioning_preview_tool_shows_the_buildings_and_writes_the_manning_grid():
    from nd_breach_runner.tests.run_tests import _patched_algs
    algs, fq = _patched_algs()
    import importlib
    from nd_breach_runner import algorithms_tools as at
    importlib.reload(at)
    from nd_breach_runner.tests.synthetic import SyntheticTerrain, npy_writer
    old_gdal = at._ToolBase._need_gdal
    at._ToolBase._need_gdal = lambda self: None
    d = _tmp()
    try:
        sq = fq.FakeGeometry("polygon", [[(700.0, -40.0), (780.0, -40.0), (780.0, 40.0),
                                          (700.0, 40.0), (700.0, -40.0)]])
        src = fq.FakeSource([fq.FakeFeature(sq, ID="house")])
        a = at.ConditionDemAlgorithm()
        a.initAlgorithm()
        out = os.path.join(d, "cond.tif")
        fb = fq.QgsProcessingFeedback()
        r = a.processAlgorithm({"DEM": _FakeRaster(), "DAM_POINT": fq.QgsPointXY(0, 0),
                                "RADIUS_MI": 0.6, "CELL": 20.0, "RECEPTOR_POINTS": src,
                                "BUILDINGS": 2, "BUILDING_N": 0.12, "OUTPUT": out},
                               fq.QgsProcessingContext(), fb)
        assert "Buildings: Manning's n = 0.12 inside 1 footprint(s)" in r["RESULT"], r["RESULT"]
        assert r["MANNING_OUT"].endswith("cond_manning_n.tif")
        n = np.load(os.path.splitext(r["MANNING_OUT"])[0] + ".npy")
        assert abs(np.nanmax(n) - 0.12) < 1e-9 and abs(np.nanmin(n) - 0.06) < 1e-9
        r = a.processAlgorithm({"DEM": _FakeRaster(), "DAM_POINT": fq.QgsPointXY(0, 0),
                                "RADIUS_MI": 0.6, "CELL": 20.0, "RECEPTOR_POINTS": src,
                                "BUILDINGS": 1, "OUTPUT": out},
                               fq.QgsProcessingContext(), fq.QgsProcessingFeedback())
        assert "buildings:building 1:" in r["RESULT"] and r["MANNING_OUT"] == ""
        assert "raised to the highest ground inside each plus 10.0 ft" in r["RESULT"]
    finally:
        at._ToolBase._need_gdal = old_gdal
        shutil.rmtree(d, ignore_errors=True)


# ============================================================= item 4: feet audit
_DEPTH_WORDS = re.compile(r"\b(depth|stage|elevation|height|rise|wse|water surface|level|"
                          r"invert|diameter|span|width)\b", re.I)
# parameters whose "depth/height/width" is not a length in feet: a share, a product, a
# rainfall depth in inches (the hydrology tool's own unit), a slope, or a cell-count field
_NOT_LENGTHS = {"PARTIAL_DEPTH", "DESTROY_DV", "SEV_MED_DV", "SEV_HIGH_DV", "DEPTH_IN",
                "INITIAL_LOSS_IN", "US_SLOPE", "DS_SLOPE", "SIDE_SLOPE", "PIPE_FRACTION",
                "MAX_DIST", "SMOOTH", "MAX_PX", "FIG_WIDTH", "CELL", "FINE_CELL",
                "BREACH_POSITION", "PEAK_POSITION", "CN", "IA_RATIO", "LOSS_RATE", "F_US",
                "F_DS", "LAG_HR", "BASE_LAG_HR", "CLARK_TC_HR", "TC_HR", "T0_HR", "WARN_EDGES",
                "AREA_MI2", "SLOPE_PCT", "DEFAULT_UNITS", "SPILLWAY_GATE", "OUTLETS"}
_METRE_OK = {"CELL", "FINE_CELL", "ANUGA_FINE", "ANUGA_COARSE", "ANUGA_BUFFER", "REFINE_CELL"}


@test
def feet_audit_every_depth_like_parameter_is_in_feet_and_says_whose_value_the_default_is():
    from nd_breach_runner.tests import fake_qgis as fq
    fq.install()
    import importlib
    import nd_breach_runner.algorithms as algs
    importlib.reload(algs)
    seen = {}
    for cls in algs.ALGORITHMS:
        a = cls()
        a.initAlgorithm()
        for p in a.parameterDefinitions():
            if type(p).__name__ in ("QgsProcessingParameterNumber",
                                    "QgsProcessingParameterString"):
                seen[(cls.__name__, p.name())] = (p.description, p.kwargs.get("defaultValue"))
    assert len(seen) > 150
    problems = []
    for (cls, name), (desc, default) in seen.items():
        low = desc.lower()
        if name in _METRE_OK:
            # a metre quantity: says "(m" and gives the feet equivalent
            if ("(m" not in desc and "crs units" not in low) or "ft" not in desc:
                problems.append((cls, name, "metre label without a ft equivalent", desc))
            continue
        if name in _NOT_LENGTHS or name.startswith("F_") or name.startswith("S_") and \
                name not in ("S_DEFAULT_WIDTH_FT", "S_DEFAULT_HEIGHT_FT"):
            continue
        if _DEPTH_WORDS.search(desc) and "field" not in low and "(mi" not in desc and \
                "factor" not in low:
            if not re.search(r"\(ft\b|\bft[;,)]|\bft\b", desc):
                problems.append((cls, name, "depth-like label not in feet", desc))
            if default not in (None, "", 0, 0.0) and name.endswith("_FT") or name in (
                    "INCR_FT", "PROB_THRESHOLDS", "THRESH_FT", "WET_FT"):
                if default not in (None, "") and not re.search(
                        r"engineer|you choose|placeholder|your ", low):
                    problems.append((cls, name, "depth-like default without 'engineer's value'",
                                     desc))
        if re.search(r"\(m\)|\bmetres?\b|\bmeters?\b", low) and "dem elevation units" not in low:
            problems.append((cls, name, "unexpected metre label", desc))
    assert not problems, "\n".join("{}.{}: {} -> {}".format(*p) for p in problems)
    # the rasters' own units are unchanged by the audit (file-level contract)
    from nd_breach_runner.core import workflow
    assert workflow.FINAL_NAMES["depth"] == "Depth (Max).tif"
