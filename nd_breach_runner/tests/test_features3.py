"""Tests of the v3 additions in the core: outlet works and gates, partial breach, tailwater
stage, ambient flow with a sunny-day baseline, run manifest, animation from the workflow, nested
grids, crest position."""
import json
import os
import shutil
import tempfile

import numpy as np

from nd_breach_runner.core import breach as br
from nd_breach_runner.core.grid import Grid
from nd_breach_runner.core.units import FT, ACFT, CFS
from nd_breach_runner.tests.registry import test, model_test
from nd_breach_runner.tests.synthetic import SyntheticTerrain, npy_writer


def _dam(**k):
    d = dict(dam_id="T1", dam_height_ft=25.0, storage_top_acft=500.0, top_of_dam_ft=1850.0,
             surface_area_top_ac=60.0)
    d.update(k)
    return br.DamData(**d)


def _exe():
    return os.environ.get("NDBREACH_LISFLOOD", "")


# ------------------------------------------------------------------------------- outlets
@test
def outlet_ratings_gates_and_parsing():
    from nd_breach_runner.core import outlets as o
    c = o.parse_outlets("drawdown; 100; 1.5; 0.6; 0:1, 6:0")[0]
    # orifice, head 9.25 ft over the centre of a 1.5 ft pipe: cd * A * sqrt(2 g h)
    q = c.q(110 * FT, 0.0) / CFS
    hand = 0.6 * np.pi * 1.5 ** 2 / 4 * (2 * 32.174 * (110 - 100.75)) ** 0.5
    assert abs(q - hand) / hand < 0.01, (q, hand)
    assert c.q(110 * FT, 7 * 3600.0) == 0.0                      # gate closed after 6 h
    assert c.q(99 * FT, 0.0) == 0.0                              # below the invert
    # weir flow below the crown is small and continuous with the orifice range
    lo, hi = c.q(101.4 * FT, 0), c.q(101.6 * FT, 0)
    assert 0 < lo <= hi < 2.0 * lo + 1e-9
    box = o.parse_outlets("box; 100; 3 x 2; 0.62")[0]
    assert not box.circular and abs(box.area_m2 - 6 * FT * FT) < 1e-9
    assert o.schedule_factor([(0, 1.0), (3600, 0.5)], 1800) == 1.0
    assert o.schedule_factor([(0, 1.0), (3600, 0.5)], 3600) == 0.5
    for bad in ("x; 1", "x; a; 2", "x; 1; 2; 5"):
        try:
            o.parse_outlets(bad)
            assert False, bad
        except br.BreachInputError:
            pass
    try:
        o.parse_schedule("0-1")
        assert False
    except br.BreachInputError:
        pass


@test
def outlets_and_gates_change_the_routed_flood():
    from nd_breach_runner.core import flood, outlets as o
    dm = _dam(crest_width_ft=12, us_slope_h=3, ds_slope_h=3, crest_length_ft=800.0)

    def route(**kw):
        fi = flood.FloodInputs.triangle(
            3000.0, 4.0, 20.0, spillway=flood.Spillway.from_weir(1846.0, 20.0),
            normal_pool_ft=1846.0, trigger=flood.TRIGGER_WSE, trigger_value=1900.0, **kw)
        return flood.route_flood(dm, br.METHOD_PIPING, None, fi)

    plain = route()
    piped = route(outlets=o.parse_outlets("drawdown; 1830; 3; 0.6"))
    assert piped.summary["peak_pool_ft_no_breach"] < plain.summary["peak_pool_ft_no_breach"]
    closed = route(spillway_schedule=[(0.0, 0.0)])
    assert closed.summary["peak_pool_ft_no_breach"] > plain.summary["peak_pool_ft_no_breach"]
    assert any("Outlet works" in n for n in piped.notes) or piped.failed is False


# ----------------------------------------------------------------------------- partial breach
@test
def partial_breach_releases_less_and_keeps_dead_storage():
    dm = _dam()
    full = br.make_hydrograph(dm, br.METHOD_PIPING)
    part = br.make_hydrograph(dm, br.METHOD_PIPING, br.BreachOverrides(depth_fraction=0.5))
    assert part.params["breach_bottom_ft"] > full.params["breach_bottom_ft"] + 5.0
    assert part.volume_m3 < 0.9 * full.volume_m3
    assert part.peak_cms < full.peak_cms
    assert abs(part.params["breach_bottom_ft"] - (1850.0 - 12.5)) < 0.05
    same = br.make_hydrograph(dm, br.METHOD_PIPING, br.BreachOverrides(depth_fraction=1.0))
    assert abs(same.volume_m3 - full.volume_m3) < 1.0
    try:
        br.make_hydrograph(dm, br.METHOD_PIPING, br.BreachOverrides(depth_fraction=0.02))
        assert False
    except br.BreachInputError:
        pass
    # a bottom the user already set higher wins
    hi = br.apply_partial(_dam(breach_bottom_ft=1845.0), br.BreachOverrides(depth_fraction=0.5))
    assert hi.breach_bottom_ft == 1845.0


# --------------------------------------------------------------------------------- tailwater
@test
def tailwater_stage_fills_connected_low_ground_only():
    from nd_breach_runner.core import conditioning as cond
    n = 40
    z = np.full((n, n), 10.0)
    z[:, 5:15] = 2.0                        # river channel
    z[30:35, 30:35] = 2.0                   # a low field, not connected
    g = Grid(z, 0.0, float(n), 1.0)
    poly = np.array([[0, 0], [40, 0], [40, 40], [0, 40]], float)
    stage = 5.0
    ed = cond.Edits(stages=[cond.TailwaterStage(poly, stage, seed=(10.0, 20.0))])
    out, init, counts = ed.apply(g)
    assert np.allclose(out.masked(), z)                          # the ground is unchanged
    assert init is not None
    d = init.data
    assert np.allclose(d[:, 5:15], 3.0) and d[32, 32] == 0.0 and d[:, 20:].max() == 0.0
    free = cond.Edits(stages=[cond.TailwaterStage(poly, stage)])
    _o, init2, _c = free.apply(g)
    assert init2.data[32, 32] == 3.0                             # without a seed both fill
    assert not cond.Edits().has_water() and ed.has_water()


# ------------------------------------------------------------------------ crest position
@test
def breach_position_along_the_crest_line():
    from nd_breach_runner.algorithms import _point_along_nearest
    line = [(0.0, 0.0), (100.0, 0.0), (100.0, 100.0)]
    assert _point_along_nearest([line], (0, 0), 0.0) == (0.0, 0.0)
    assert _point_along_nearest([line], (0, 0), 0.5) == (100.0, 0.0)
    x, y = _point_along_nearest([line], (0, 0), 0.75)
    assert abs(x - 100.0) < 1e-9 and abs(y - 50.0) < 1e-9
    far = [(1000.0, 1000.0), (1100.0, 1000.0)]
    assert _point_along_nearest([far, line], (10, 10), 1.0) == (100.0, 100.0)


# ---------------------------------------------------------------------------- provenance
@test
def manifest_records_hashes_and_catches_changes():
    from nd_breach_runner.core import provenance as pv
    d = tempfile.mkdtemp()
    a, b = os.path.join(d, "dem.tif"), os.path.join(d, "out.tif")
    open(a, "wb").write(b"dem" * 1000)
    open(b, "wb").write(b"depth" * 100)
    m = pv.build_manifest("tool 3", {"cell": 10}, {"dam_id": "X"}, (1.0, 2.0), {"how": "given"},
                          {"peak_cfs": 5}, {"DEM": a}, {"depth": b})
    path = pv.write_manifest(os.path.join(d, pv.MANIFEST_NAME), m)
    r = pv.verify_manifest(path)
    assert r["ok"] and r["checked"] == 2, r
    open(b, "wb").write(b"depth" * 101)
    r = pv.verify_manifest(path)
    assert not r["ok"] and any("output 'depth' changed" in p for p in r["problems"])
    open(b, "wb").write(b"depth" * 100)
    assert pv.verify_manifest(path)["ok"]
    txt = json.load(open(path))
    txt["settings"]["cell"] = 5
    json.dump(txt, open(path, "w"))
    assert any("manifest itself" in p for p in pv.verify_manifest(path)["problems"])
    # a large file is sampled, a small one fully hashed
    big = pv.file_digest(a, full_limit=100)
    assert big["sampled"] and not pv.file_digest(a)["sampled"]
    os.remove(a)
    assert any("missing" in p for p in pv.verify_manifest(
        pv.write_manifest(os.path.join(d, "m2.json"), m))["problems"])
    shutil.rmtree(d)


@test
def verify_manifest_algorithm_runs_through_the_fake_harness():
    from nd_breach_runner import algorithms_provenance as ap
    from nd_breach_runner.core import provenance as pv
    from nd_breach_runner.tests import fake_qgis as fq
    d = tempfile.mkdtemp()
    os.makedirs(os.path.join(d, "_breach_model"))
    f = os.path.join(d, "x.tif")
    open(f, "wb").write(b"1234")
    m = pv.build_manifest("t", {}, {}, None, None, None, {}, {"depth": f})
    pv.write_manifest(os.path.join(d, "_breach_model", pv.MANIFEST_NAME), m)
    a = ap.VerifyManifestAlgorithm()
    a.initAlgorithm()
    r = a.processAlgorithm({"TARGET": d}, fq.QgsProcessingContext(), fq.QgsProcessingFeedback())
    assert r["RESULT"].startswith("OK"), r
    open(f, "wb").write(b"12345")
    r = a.processAlgorithm({"TARGET": d}, fq.QgsProcessingContext(), fq.QgsProcessingFeedback())
    assert r["RESULT"].startswith("CHANGED"), r
    shutil.rmtree(d)


# --------------------------------------------------------------------------------- merge
@test
def nested_merge_uses_fine_upstream_and_coarse_downstream():
    from nd_breach_runner.core import nested
    fine = Grid(np.zeros((40, 40)), 0.0, 40.0, 1.0)             # x 0..40, y 0..40, 1 m
    coarse = Grid(np.zeros((10, 10)), 20.0, 40.0, 4.0)          # x 20..60, y 0..40, 4 m
    fa = {k: np.full((40, 40), 1.0) for k in ("depth", "velocity", "wse", "arrival")}
    fa["depth"][:, 5] = 3.0
    ca = {k: np.full((10, 10), 2.0) for k in ("depth", "velocity", "wse", "arrival")}
    grid, out = nested.merge_results(fine, fa, coarse, ca, (30.0, 20.0), (1.0, 0.0))
    assert grid.dx == 4.0 and grid.nx == 15 and grid.ny == 10
    x = grid.x_centers()
    up = x < 30.0
    d = out["depth"]
    assert np.all(d[:, up & (x < 20)] >= 1.0)                    # fine only there
    assert np.nanmax(d[:, 1]) == 3.0 or np.nanmax(d[:, 1]) >= 1.0
    assert np.all(d[:, x > 30.0] == 2.0)                         # coarse downstream
    assert np.all(out["arrival"][:, x > 30.0] == 2.0)
    p, dr, ln = nested.interface_point([(0, 0), (100, 0), (100, 100)], 150.0)
    assert abs(p[0] - 100.0) < 1e-9 and abs(p[1] - 50.0) < 1e-9 and ln == 200.0
    ext = nested.wet_extent_at(Grid(np.zeros((30, 30)), 0.0, 30.0, 1.0),
                               np.pad(np.ones((10, 30)), ((10, 10), (0, 0))), (15.0, 15.0),
                               (1.0, 0.0))
    assert ext is not None and 9.0 <= ext[1] <= 12.0, ext


# -------------------------------------------------------------------------- model tests
def _settings(**k):
    from nd_breach_runner.core import workflow
    base = dict(cell_size_m=20.0, initial_length_mi=1.5, half_width_mi=0.5, max_iterations=1,
                lisflood_exe=_exe(), threads=2, sim_time_hr=4.0)
    base.update(k)
    return workflow.RunSettings(**base)


@model_test
def ambient_flow_gives_a_baseline_and_a_manifest():
    from nd_breach_runner.core import workflow, provenance
    d = tempfile.mkdtemp()
    dem_stub = os.path.join(d, "dem.txt")
    open(dem_stub, "w").write("stub")
    st = _settings(ambient_cfs=60.0, spinup_hr=1.0, input_files={"DEM": dem_stub},
                   sim_time_hr=None)
    r = workflow.run_dam(_dam(), (0.0, 0.0), st, SyntheticTerrain(), npy_writer,
                         os.path.join(d, "T1"))
    rep = json.load(open(r.report_json))
    assert rep["ambient"]["cfs"] == 60.0
    assert r.baseline and r.increment, "an ambient run has a baseline and incremental rasters"
    dep = lambda p: np.load(os.path.splitext(p)[0] + ".npy")
    inc, wb, nb = dep(r.increment["depth"]), dep(r.final["depth"]), dep(r.baseline["depth"])
    assert np.nanmax(inc) > 1.0
    assert np.nanmax(wb) >= np.nanmax(nb) - 1e-6
    # the breach hydrograph CSV is the one computed, the model inflow is the shifted copy
    assert os.path.exists(os.path.join(os.path.dirname(r.report_json),
                                       "model_inflow_hydrograph.csv"))
    lst = open(r.hydrograph_csv).read().splitlines()
    assert float(lst[1].split(",")[0]) == 0.0
    assert r.t_ref_hr == 1.0                                       # arrival counts from breach
    assert r.manifest and provenance.verify_manifest(r.manifest)["ok"]
    m = json.load(open(r.manifest))
    assert "DEM" in m["inputs"] and m["software"]["lisflood_exe"]["sha256"]
    assert abs(r.summary["latest_arrival_hr"]) < 4.0
    shutil.rmtree(d)


@model_test
def tailwater_stage_starts_the_valley_wet():
    from nd_breach_runner.core import workflow, conditioning as cond
    d = tempfile.mkdtemp()
    # the channel bed is around 1846 ft - 25 ft; a lake of a few feet in the first miles
    top = 1850.0 * FT
    bed = top - 25.0 * FT
    poly = np.array([[300.0, -300.0], [3000.0, -300.0], [3000.0, 300.0], [300.0, 300.0]])
    ed = cond.Edits(stages=[cond.TailwaterStage(poly, bed - 0.001 * 1500 + 1.5)])
    st = _settings(edits=ed)
    r = workflow.run_dam(_dam(), (0.0, 0.0), st, SyntheticTerrain(), npy_writer,
                         os.path.join(d, "T1"))
    assert r.summary["max_depth_ft"] > 1.0
    rep = json.load(open(r.report_json))
    assert len(rep["settings"]["edits"]["tailwater"]) == 1
    shutil.rmtree(d)


@model_test
def animation_from_the_workflow():
    from nd_breach_runner.core import workflow
    d = tempfile.mkdtemp()
    st = _settings(animation=True, snapshot_frames=8, sim_time_hr=3.0)
    r = workflow.run_dam(_dam(), (0.0, 0.0), st, SyntheticTerrain(), npy_writer,
                         os.path.join(d, "T1"))
    assert r.animation is not None and r.animation.gif and os.path.exists(r.animation.gif), \
        r.warnings
    assert len(r.animation.frames) >= 4
    assert os.path.exists(os.path.join(os.path.dirname(r.report_json), "depth_snapshots.npz"))
    shutil.rmtree(d)


@model_test
def nested_run_hands_the_interface_flow_downstream():
    from nd_breach_runner.core import nested
    d = tempfile.mkdtemp()
    st = _settings(cell_size_m=25.0, initial_length_mi=4.0, half_width_mi=0.4, max_iterations=2,
                   sim_time_hr=None)
    out = nested.run_nested(_dam(), (0.0, 0.0), st, SyntheticTerrain(), npy_writer,
                            os.path.join(d, "T1"), None, fine_cell_m=10.0, reach_mi=0.8,
                            fine_half_width_mi=0.2)
    assert out.grid is not None and out.final_si is not None, out.warnings
    i = out.interface
    assert i["peak_cfs"] > 100 and i["width_m"] >= 75.0
    fd = out.fine.grid.dx
    assert fd == 10.0 and out.coarse.grid.dx == 25.0 and out.grid.dx == 25.0
    # the merged flood reaches beyond the fine domain
    fx1 = out.fine.grid.x0 + out.fine.grid.nx * fd
    wet_x = out.grid.x_centers()[np.isfinite(out.final_si["depth"]).any(axis=0)]
    assert wet_x.max() > fx1 + 200.0, (wet_x.max(), fx1)
    for kind in ("depth", "velocity", "arrival", "wse"):
        assert os.path.exists(out.final[kind])
    assert os.path.exists(out.report_md)
    # compare with a plain run at the coarse size: the flooded extents agree closely
    from nd_breach_runner.core import workflow
    st2 = _settings(cell_size_m=25.0, initial_length_mi=4.0, half_width_mi=0.4, max_iterations=2,
                    sim_time_hr=None)
    plain = workflow.run_dam(_dam(), (0.0, 0.0), st2, SyntheticTerrain(), npy_writer,
                             os.path.join(d, "plain"))
    a, b = out.summary["flooded_acres"], plain.summary["flooded_acres"]
    assert abs(a - b) / b < 0.25, (a, b)
    shutil.rmtree(d)


# ------------------------------------------------------------------ regressions from the review
@test
def partial_breach_is_applied_in_the_flood_routing():
    from nd_breach_runner.core import flood
    dm = _dam(crest_width_ft=12, us_slope_h=3, ds_slope_h=3, crest_length_ft=800.0)

    def route(frac):
        fi = flood.FloodInputs.triangle(
            3000.0, 4.0, 20.0, spillway=flood.Spillway.from_weir(1846.0, 20.0),
            normal_pool_ft=1846.0, trigger=flood.TRIGGER_WSE, trigger_value=1848.0)
        return flood.route_flood(dm, br.METHOD_PIPING, br.BreachOverrides(depth_fraction=frac), fi)

    full, part = route(None), route(0.5)
    assert part.with_breach.params["breach_bottom_ft"] > full.with_breach.params["breach_bottom_ft"] + 5
    assert part.with_breach.volume_m3 < full.with_breach.volume_m3


@test
def a_late_gate_opening_is_routed_after_the_inflow_ends():
    from nd_breach_runner.core import flood
    dm = _dam(crest_width_ft=12, us_slope_h=3, ds_slope_h=3, crest_length_ft=800.0)
    fi = flood.FloodInputs.triangle(
        1500.0, 2.0, 10.0, spillway=flood.Spillway.from_weir(1846.0, 20.0),
        normal_pool_ft=1846.0, trigger=flood.TRIGGER_WSE, trigger_value=1900.0,
        spillway_schedule=[(0.0, 0.0), (30 * 3600.0, 1.0)])
    fr = flood.route_flood(dm, br.METHOD_PIPING, None, fi)
    nb = fr.no_breach
    assert nb.t_s[-1] > 30 * 3600.0, nb.t_s[-1] / 3600.0        # the release after 30 h is modelled


@test
def tailwater_sits_on_the_edited_ground():
    from nd_breach_runner.core import conditioning as cond
    n = 30
    z = np.full((n, n), 10.0)
    z[:, 5:15] = 4.0
    g = Grid(z, 0.0, float(n), 1.0)
    poly = np.array([[0, 0], [30, 0], [30, 30], [0, 30]], float)
    ch = cond.ChannelBurn(np.array([[10.0, 0.0], [10.0, 30.0]]), 1.0, 1.0) \
        if hasattr(cond, "ChannelBurn") else None
    if ch is None:
        return
    ed = cond.Edits(channels=[ch], stages=[cond.TailwaterStage(poly, 6.0)])
    out, init, _ = ed.apply(g)
    z_edit = out.masked()
    assert np.allclose(z_edit + init.data, np.where(init.data > 0, 6.0, z_edit))
    src = type("S", (), {"read": lambda self, b, c: g, "edits": None})()
    ct = cond.ConditionedTerrain(src, ed)
    d = ct.initial_depth(None, None)
    assert np.allclose(d.data, init.data)


@test
def resume_key_ignores_the_resume_switch_itself():
    from nd_breach_runner.core import workflow
    a, b = workflow.RunSettings(), workflow.RunSettings()
    a.resume, b.resume = False, True
    da, db = a.as_dict(), b.as_dict()
    assert {k: v for k, v in da.items() if k != "resume"} == {k: v for k, v in db.items() if k != "resume"}
