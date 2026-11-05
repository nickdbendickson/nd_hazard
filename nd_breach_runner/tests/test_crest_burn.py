"""3.28.0: crest-burn terrain prep (core/crestburn.py) and the automatic finer grid
(workflow.run_dam when the grid-spread signature fires).

Crest burn: each model cell a road or rail line crosses is raised to the highest 1 m elevation
inside it (never lowered; bridges, the traced main channel and > 5 m raises left alone; reads
before the flow trace pass straight through). DWR-2801: 10 m extent 1.46x -> 1.33x of DSS-WISE
at > 0.5 ft with the cell unchanged.

Finer grid: a run at a cell coarser than 10 m whose flood shows the over-spread signature is run
again on the same domain at 10 m (the record) and at 5 m (a check in a subfolder)."""
import json
import os
import tempfile

import numpy as np

from nd_breach_runner.core import crestburn as cb
from nd_breach_runner.core import workflow as wf
from nd_breach_runner.core.grid import Grid
from nd_breach_runner.tests.registry import test


class _Flat:
    """terrain_src: flat ground at 100 m."""
    z_to_m = 1.0
    z_note = "metres"
    src_cell_m = 10.0

    def __init__(self):
        self.reads = 0

    def read(self, bounds, cell):
        self.reads += 1
        nx = int(round((bounds[2] - bounds[0]) / cell))
        ny = int(round((bounds[3] - bounds[1]) / cell))
        return Grid(np.full((ny, nx), 100.0), bounds[0], bounds[3], cell)


class _Embankment:
    """Fine source: a 1 m road crest at 101.5 m along y = 205 (x any), 100 m elsewhere; a hill
    of 120 m around x = 900; records the boxes it was asked for."""

    def __init__(self):
        self.calls = []

    def max_grid(self, bounds, cell, shape):
        self.calls.append((tuple(bounds), cell))
        ny, nx = shape
        x0, y1 = bounds[0], bounds[3]
        yc_top = y1 - np.arange(ny) * cell          # top edge of each row
        out = np.full((ny, nx), 100.0)
        rows = (yc_top > 205.0) & (yc_top - cell <= 205.0)
        out[rows, :] = 101.5
        xc = x0 + (np.arange(nx) + 0.5) * cell
        out[:, np.abs(xc - 900.0) < cell] = 120.0
        return out


@test
def crest_burn_line_cells_marks_every_cell_a_line_crosses():
    m = cb.line_cells((10, 10), 0.0, 100.0, 10.0, [[(0.0, 55.0), (100.0, 55.0)]])
    assert m[4].all() and m.sum() == 10                # row 4 spans y 50-60
    d = cb.line_cells((10, 10), 0.0, 100.0, 10.0, [[(0.0, 100.0), (100.0, 0.0)]])
    assert all(d[i, i] for i in range(10))             # a diagonal reaches every diagonal cell
    assert cb.line_cells((5, 5), 0.0, 50.0, 10.0, [[(500.0, 500.0), (600.0, 600.0)]]).sum() == 0


@test
def crest_burn_raises_only_and_respects_the_guards():
    base = np.full((3, 4), 100.0)
    fine = np.array([[101.0, 99.0, 106.0, np.nan]] * 3)
    lines = np.zeros((3, 4), dtype=bool)
    lines[1, :] = True
    keep = np.zeros((3, 4), dtype=bool)
    out, st = cb.burn(base, fine, lines, keep)
    assert out[1, 0] == 101.0                          # raised to the 1 m crest
    assert out[1, 1] == 100.0                          # never lowered
    assert out[1, 2] == 100.0 and st["skipped_over_max"] == 1     # +6 m is not an embankment
    assert out[1, 3] == 100.0                          # no fine data: unchanged
    assert (out[0] == 100.0).all() and (out[2] == 100.0).all()    # off the line: unchanged
    assert st["raised"] == 1 and abs(st["mean_raise_m"] - 1.0) < 1e-9
    keep[1, 0] = True
    out2, st2 = cb.burn(base, fine, lines, keep)
    assert out2[1, 0] == 100.0 and st2["raised"] == 0 and st2["kept_open"] == 1


@test
def crest_burn_guard_keeps_the_flow_path_and_bridges_open():
    keep = cb.guard_cells((20, 20), 0.0, 200.0, 10.0, paths=[[(5.0, 100.0), (195.0, 100.0)]],
                          points=[(105.0, 35.0, 0.0)])
    assert keep[9, 10] and keep[10, 10]                # cells beside the traced channel
    assert not keep[0, 0] and not keep[19, 0]
    assert keep[16, 10]                                 # the bridge cell (y 30-40)
    wide = cb.guard_cells((20, 20), 0.0, 200.0, 10.0, points=[(105.0, 35.0, 40.0)])
    assert wide.sum() > keep[14:20].sum()               # a long span keeps more cells open
    pts = cb.bridge_points([{"kind": "bridge", "x": 1, "y": 2, "span_ft": 100},
                            {"kind": "culvert", "x": 5, "y": 5}, {"kind": "bridge"}])
    assert len(pts) == 1 and abs(pts[0][2] - 15.24) < 1e-6


@test
def crest_burn_terrain_passes_through_until_armed_and_burns_after():
    base = _Flat()
    fine = _Embankment()
    road = [[(0.0, 205.0), (1000.0, 205.0)]]
    t = cb.CrestBurnTerrain(base, road, fine)
    g = t.read((0.0, 0.0, 400.0, 400.0), 10.0)
    assert (g.data == 100.0).all() and not fine.calls  # the trace / planning reads are plain
    t.arm(paths=[[(300.0, 0.0), (300.0, 400.0)]])
    g = t.read((0.0, 0.0, 400.0, 400.0), 10.0)
    r = g.rc(50.0, 205.0)[0]
    assert np.allclose(g.data[r, :25], 101.5)          # the road row carries the 1 m crest
    assert (g.data[r, 29:32] == 100.0).all()           # ... except across the traced channel
    others = np.delete(g.data, r, axis=0)
    assert (others == 100.0).all()
    assert t.stats[10.0]["raised"] > 0
    fine_reads = len(fine.calls)
    t.read((0.0, 0.0, 400.0, 400.0), 10.0)
    assert len(fine.calls) == fine_reads               # aligned chunks are reused
    g3 = t.read((0.0, 195.0, 10.0, 215.0), 3.0)        # a fine read (the crest near the dam)
    assert (g3.data == 100.0).all()


@test
def crest_burn_chunked_read_matches_a_direct_read():
    base = _Flat()
    t = cb.CrestBurnTerrain(base, [[(0.0, 205.0), (6000.0, 205.0)]], _Embankment())
    t.arm()
    big = t.read((-1000.0, -500.0, 5000.0, 900.0), 10.0)       # spans several 2.56 km chunks
    direct = _Embankment().max_grid(big.bounds, 10.0, big.data.shape)
    mask = cb.line_cells(big.data.shape, big.x0, big.y0, 10.0, t.lines)
    exp, _ = cb.burn(np.full(big.data.shape, 100.0), direct, mask)
    assert np.array_equal(big.data, exp)
    # an unaligned grid is read in one piece and burns the same way
    t2 = cb.CrestBurnTerrain(_Flat(), t.lines, _Embankment())
    t2.arm()
    g = t2.read((3.0, 3.0, 403.0, 403.0), 10.0)
    assert (g.data.max() == 101.5) and len(t2.fine.calls) == 1


@test
def crest_burn_settings_default_on_and_in_the_checkpoint_key():
    st = wf.RunSettings()
    assert st.crest_burn is True and st.grid_refine is True
    assert st.grid_refine_cell_m == 10.0 and st.grid_check_cell_m == 5.0
    k = wf.key_settings(st)
    assert k["crest_burn"] is True and "crest_burn_http" not in k and "grid_refine" not in k
    off = wf.key_settings(wf.RunSettings(crest_burn=False))
    assert "crest_burn" not in off and "crest_burn_lines" not in off
    d = wf.RunSettings(crest_burn_lines=[[(0, 0), (1, 1)]], crest_burn_http=len).as_dict()
    assert d["crest_burn_lines"] == 1 and d["crest_burn_http"] is True
    json.dumps(d)


@test
def crest_burn_setup_reports_why_it_did_not_apply():
    st = wf.RunSettings()
    rep, warns, logs = {}, [], []
    assert wf._crest_burn_setup(st, _Flat(), None, rep, warns, logs.append, None) is None
    assert rep["crest_burn"]["applied"] is False and "no road or rail" in rep["crest_burn"]["why"]
    assert not warns                                   # no lines given: nothing to warn about
    st.crest_burn_lines = [[(0.0, 0.0), (10.0, 0.0)]]
    st.crest_burn_download = False
    rep = {}
    assert wf._crest_burn_setup(st, _Flat(), None, rep, warns, logs.append, None) is None
    assert rep["crest_burn"]["applied"] is False and warns and "not applied" in warns[0]
    st.crest_burn = False
    rep = {}
    assert wf._crest_burn_setup(st, _Flat(), None, rep, warns, logs.append, None) is None
    assert "crest_burn" not in rep


@test
def crest_burn_pick_source_uses_a_fine_run_dem_and_refuses_a_coarse_named_one():
    try:
        from osgeo import gdal, osr
    except Exception:                                   # noqa - GDAL-less fast gate
        return
    d = tempfile.mkdtemp()

    def mk(name, px):
        p = os.path.join(d, name)
        ds = gdal.GetDriverByName("GTiff").Create(p, 20, 20, 1, gdal.GDT_Float32)
        ds.SetGeoTransform((500000.0, px, 0, 5200000.0, 0, -px))
        s = osr.SpatialReference()
        s.ImportFromEPSG(26914)
        ds.SetProjection(s.ExportToWkt())
        b = ds.GetRasterBand(1)
        a = np.full((20, 20), 300.0, dtype=np.float32)
        a[10, :] = 302.0
        b.WriteArray(a)
        b.SetNoDataValue(-9999)
        b.SetUnitType("m")
        ds = None
        return p
    one = mk("one.tif", 1.0)
    ten = mk("ten.tif", 10.0)
    src, note = cb.pick_source(ten, None, "EPSG:26914")
    assert src is None and "1.0" not in note and "needs" in note
    src, note = cb.pick_source(one, None, "EPSG:26914")
    assert src is not None and cb.SRC_PARAMETER in note
    m = src.max_grid((500000.0, 5199980.0, 500020.0, 5200000.0), 10.0, (2, 2))
    assert np.allclose(m[1], 302.0) and np.allclose(m[0], 300.0)   # max of the 1 m pixels in a cell

    class _Run(_Flat):
        dem_path = one
        src_cell_m = 1.0
    src, note = cb.pick_source("", _Run(), "EPSG:26914")
    assert src is not None and cb.SRC_RUN_DEM in note
    src, note = cb.pick_source("", _Flat(), "EPSG:26914", data_folder="", allow_download=False)
    assert src is None and "no 1 m terrain" in note


# ------------------------------------------------------------------ automatic finer grid --
class _Res:
    def __init__(self, folder, cell, area, median, flagged):
        self.folder = folder
        self.summary = {"flooded_acres_over_arrival_depth": area}
        self.warnings = []
        md = os.path.join(folder, wf.MODEL_FOLDER)
        os.makedirs(md, exist_ok=True)
        self.report_json = os.path.join(md, "run_report.json")
        self.report_md = os.path.join(md, "run_report.md")
        rep = {"summary": dict(self.summary), "warnings": [], "settings": {"cell_size_m": cell}}
        if flagged is not None:
            rep["grid_spread_check"] = {"cell_m": cell, "flooded_acres": area,
                                        "median_depth_ft": median, "flagged": flagged}
        with open(self.report_json, "w") as f:
            json.dump(rep, f)
        n = int(round(1000.0 / cell))
        d = np.zeros((n, n))
        d[: n // 2, :] = 1.0
        self.final_si = {"depth": d}
        self.plan = {"inflow_xy": (0.0, 0.0), "path": [(0.0, 0.0), (10.0, 0.0)],
                     "direction": (1.0, 0.0), "bounds": (0.0, 0.0, 1000.0, 1000.0),
                     "sim_time": 3600.0}


def _fake_runs(flagged, areas=None):
    calls = []
    areas = areas or {20.0: 6000.0, 10.0: 4500.0, 5.0: 3000.0}

    def once(dam, dam_xy, st, terrain_src, writer, out_folder, inflow_xy=None, progress=None,
             log=None, cancel=None):
        calls.append((st.cell_size_m, out_folder, st.fixed_plan is not None, st.products,
                      st.dam_record))
        return _Res(out_folder, st.cell_size_m, areas[st.cell_size_m], 0.6,
                    flagged if st.fixed_plan is None else None)
    return once, calls


@test
def grid_refine_reruns_at_10_m_for_the_record_and_checks_at_5_m():
    out = tempfile.mkdtemp()
    once, calls = _fake_runs(True)
    real = wf._run_dam_once
    wf._run_dam_once = once
    try:
        st = wf.RunSettings(cell_size_m=20.0)
        st.dam_record = {"point": {}}
        res = wf.run_dam(None, (0, 0), st, None, None, out)
    finally:
        wf._run_dam_once = real
    assert [c[0] for c in calls] == [20.0, 10.0, 5.0]
    assert calls[1][1] == out and calls[1][2] is True         # the record: same folder, same plan
    assert calls[2][1] == os.path.join(out, "Grid check 5 m") and calls[2][3] is False
    assert calls[2][4] is None                                # the check writes no dam.json entry
    rep = json.load(open(res.report_json))
    gr = rep["grid_refine"]
    assert gr["coarse_cell_m"] == 20.0 and gr["record_cell_m"] == 10.0
    assert gr["check"]["cell_m"] == 5.0 and gr["check"]["area_change_pct"] == round(
        100.0 * (3000.0 - 4500.0) / 4500.0, 1)
    assert gr["check"]["csi_record_vs_check"] == 1.0
    assert any("Finer grid used automatically" in w for w in rep["warnings"])
    assert any("has not settled" in w for w in rep["warnings"])    # -33% is over the 25% bar
    assert os.path.isfile(os.path.join(out, wf.MODEL_FOLDER, "coarse_20m_run_report.json"))
    assert st.cell_size_m == 20.0 and st.keep_arrays is False     # the caller's settings kept


@test
def grid_refine_leaves_unflagged_fine_and_fixed_plan_runs_alone():
    for cell, flagged, plan, on in ((20.0, False, None, True), (10.0, True, None, True),
                                    (20.0, True, {"x": 1}, True), (20.0, True, None, False)):
        out = tempfile.mkdtemp()
        once, calls = _fake_runs(flagged)
        real = wf._run_dam_once
        wf._run_dam_once = once
        try:
            st = wf.RunSettings(cell_size_m=cell, grid_refine=on)
            st.fixed_plan = plan
            res = wf.run_dam(None, (0, 0), st, None, None, out)
        finally:
            wf._run_dam_once = real
        assert len(calls) == 1, (cell, flagged, plan, on)
        assert "grid_refine" not in json.load(open(res.report_json))
    st = wf.RunSettings(cell_size_m=20.0, grid_check_cell_m=0.0)
    out = tempfile.mkdtemp()
    once, calls = _fake_runs(True)
    real = wf._run_dam_once
    wf._run_dam_once = once
    try:
        res = wf.run_dam(None, (0, 0), st, None, None, out)
    finally:
        wf._run_dam_once = real
    assert [c[0] for c in calls] == [20.0, 10.0]               # check off: the record only
    assert "check" not in json.load(open(res.report_json))["grid_refine"]
