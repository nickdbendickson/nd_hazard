"""3.26.1: HEC-RAS depth from the terrain under each grid cell, not the cell's minimum.

A 150 ft HEC-RAS cell holding a ditch: the pre-3.26.1 export (max WSE minus 'Cells Minimum
Elevation') gave the whole cell the ditch's depth; terrain_depth makes only the ditch deep and
leaves the high ground dry, with the wet mask and the arrival time on the same depth. Also the
re-export of a finished run from its plan HDF and terrain (hecras.reexport_results), without
GDAL or h5py (stand-ins), into a new folder that it never overwrites unasked, and the
'hecras-reexport' command."""
import json
import os
import shutil
import tempfile

import numpy as np

from nd_breach_runner.core import hecras
from nd_breach_runner.core.grid import Grid
from nd_breach_runner.core.units import FT
from nd_breach_runner.tests.registry import test
from nd_breach_runner.tests.synthetic import npy_writer
from nd_breach_runner.tests.test_hecras import _FakeH5, _has_h5py, _write_h5

CELL = 150.0                       # ft, HEC-RAS cells (3 x 3)
DX = 10.0                          # ft, output grid (45 x 45)
NP = int(CELL / DX)                # pixels per cell side
WKT = 'PROJCS["fake_ft",GEOGCS["x"],UNIT["ft",0.3048]]'
HIGH, DITCH_A, DITCH_B, WSE_C = 100.0, 90.0, 93.0, 95.0
AREA = "Perimeter 1"
GEO = "Geometry/2D Flow Areas/{}/".format(AREA)
OUT = "Results/Unsteady/Output/Output Blocks/Base Output/"
SUMM = OUT + "Summary Output/2D Flow Areas/{}/".format(AREA)
TS = OUT + "Unsteady Time Series/"


def _terrain():
    """(45, 45) ground, ft, row 0 at the top: 100 ft high ground, and in the centre cell a
    two-pixel ditch, one column at 90 ft and one at 93 ft (x 220-240 ft)."""
    t = np.full((3 * NP, 3 * NP), HIGH)
    rows = slice(NP, 2 * NP)
    t[rows, NP + 7] = DITCH_A
    t[rows, NP + 8] = DITCH_B
    return t


def _layout():
    """3 x 3 cells of 150 ft (centres 75 + 150 i); the centre cell's minimum elevation is the
    ditch bottom (90 ft) and its max WSE 95 ft, rising linearly from 90 ft over 2 h; the other
    cells are high ground, dry (WSE = their 100 ft minimum)."""
    ii, jj = [a.ravel() for a in np.meshgrid(np.arange(3), np.arange(3))]
    x, y = 75.0 + CELL * ii, 75.0 + CELL * jj
    centre = (ii == 1) & (jj == 1)
    elev = np.where(centre, DITCH_A, HIGH)
    wse = np.where(centre, WSE_C, HIGH)
    t_hr = np.arange(5.0)
    frac = np.clip(t_hr / 2.0, 0.0, 1.0)[:, None]
    wse_t = elev[None, :] + (wse - elev)[None, :] * frac
    L = {GEO + "Cells Center Coordinate": (np.column_stack([x, y]), {}),
         GEO + "Cells Minimum Elevation": (elev, {"Units": np.bytes_(b"ft")}),
         SUMM + "Maximum Water Surface": (np.vstack([wse, np.full(9, 2.0 / 24.0)]),
                                          {"Units": np.bytes_(b"ft")}),
         SUMM + "Maximum Cell Velocity": (np.where(centre, 3.0, 0.0),
                                          {"Units": np.bytes_(b"ft/s")}),
         TS + "Time": (t_hr / 24.0, {"Time": np.bytes_(b"Days")}),
         TS + "2D Flow Areas/{}/Water Surface".format(AREA): (wse_t,
                                                             {"Units": np.bytes_(b"ft")})}
    root = {"Projection": np.bytes_(WKT.encode()), "Units System": "US Customary",
            "File Type": "HEC-RAS Results"}
    return L, root


def _fake():
    return _FakeH5(*_layout())


def _grid_args():
    return 0.0, 3 * CELL, DX, 3 * NP, 3 * NP          # x0, ytop, dx, nx, ny


@test
def hecras_ditch_cell_old_method_floods_the_whole_cell():
    """The pre-3.26.1 export: every pixel of the centre cell is 5 ft deep."""
    mesh = hecras.read_mesh(_fake())
    assert mesh.info["depth_basis"] == hecras.DEPTH_BASIS_CELL_MIN
    a = hecras.rasterise(mesh, *_grid_args())
    d = a["depth"]
    assert int(np.isfinite(d).sum()) == NP * NP, int(np.isfinite(d).sum())
    assert np.allclose(d[np.isfinite(d)], WSE_C - DITCH_A)


@test
def hecras_ditch_cell_terrain_depth_wets_only_the_ditch():
    t = _terrain()
    out, info = hecras.terrain_depth(_fake(), *_grid_args(), t)
    assert info["depth_basis"] == hecras.DEPTH_BASIS_TERRAIN
    d = out["depth"]
    wet = np.isfinite(d)
    # only the two ditch columns of the centre cell are wet
    assert int(wet.sum()) == 2 * NP, int(wet.sum())
    assert np.array_equal(wet, t < HIGH)
    assert np.allclose(d[t == DITCH_A], WSE_C - DITCH_A)
    assert np.allclose(d[t == DITCH_B], WSE_C - DITCH_B)
    # the high ground of the same cell is dry (the old method had it 5 ft deep)
    old = info["old"]
    hi = (t == HIGH) & (old > 0)
    assert int(hi.sum()) == NP * NP - 2 * NP and not np.isfinite(d[hi]).any()
    assert info["no_terrain_cells"] == 0


@test
def hecras_terrain_depth_wet_mask_and_arrival_follow_the_depth():
    t = _terrain()
    out, _ = hecras.terrain_depth(_fake(), *_grid_args(), t, arrival_depth_ft=0.1, t0_hr=0.0)
    d, arr = out["depth"], out["arrival"]
    wet = np.isfinite(d)
    for k in ("wse", "velocity", "time_of_max"):
        assert np.array_equal(np.isfinite(out[k]), wet), k
    assert np.array_equal(np.isfinite(arr), wet & (d >= 0.1))
    # the cell's WSE rises 90 -> 95 ft over 2 h: 0.1 ft over 90 ft at 0.04 h, over 93 ft
    # (WSE 93.1) at 1.24 h - per pixel terrain, where the old method gave 0.04 h to all
    assert np.allclose(arr[t == DITCH_A], 0.1 / 5.0 * 2.0, atol=1e-9)
    assert np.allclose(arr[t == DITCH_B], 3.1 / 5.0 * 2.0, atol=1e-9)
    # t0 shifts it; arrival depth deeper than a pixel's max depth: no arrival there
    out2, _ = hecras.terrain_depth(_fake(), *_grid_args(), t, arrival_depth_ft=2.5, t0_hr=1.0)
    a2 = out2["arrival"]
    assert np.allclose(a2[t == DITCH_A], 2.5 / 5.0 * 2.0 - 1.0, atol=1e-9)
    assert not np.isfinite(a2[t == DITCH_B]).any()


@test
def hecras_terrain_depth_arrival_between_output_steps_uses_time_of_max():
    """Max WSE 95 ft (computation steps) but the saved series peaks at 93.05 ft: the 93 ft
    ditch column is 2 ft deep yet never 0.1 ft in the series; it takes the time of the max."""
    L, root = _layout()
    k = TS + "2D Flow Areas/{}/Water Surface".format(AREA)
    ser = L[k][0].copy()
    ser[:, 4] = np.minimum(ser[:, 4], 93.05)
    L[k] = (ser, L[k][1])
    t = _terrain()
    out, info = hecras.terrain_depth(_FakeH5(L, root), *_grid_args(), t, arrival_depth_ft=0.1)
    d, arr = out["depth"], out["arrival"]
    assert np.array_equal(np.isfinite(arr), np.isfinite(d) & (d >= 0.1))
    assert np.allclose(arr[t == DITCH_A], 0.04, atol=1e-9)
    assert np.allclose(arr[t == DITCH_B], 2.0, atol=1e-9)
    assert any("time of the maximum" in n for n in info["notes"]), info["notes"]


@test
def hecras_first_reach_per_point_matches_brute_force_over_row_blocks():
    """Per-point arrival (cols=) read a few rows at a time equals a brute-force scan."""
    rng = np.random.default_rng(7)
    nt, n = 23, 5
    t_hr = np.arange(nt) * 0.25
    ts = 90.0 + np.cumsum(rng.uniform(0.0, 0.6, (nt, n)), axis=0)
    cols = np.array([0, 0, 1, 3, 4, 4, 2, 1])
    elev = 90.0 + rng.uniform(0.0, 6.0, len(cols))
    thr = 0.1
    want = np.full(len(cols), np.nan)
    for k, (c, e) in enumerate(zip(cols, elev)):
        d = ts[:, c] - e
        j = int(np.argmax(d >= thr)) if (d >= thr).any() else -1
        if j == 0:
            want[k] = t_hr[0]
        elif j > 0:
            want[k] = t_hr[j - 1] + (thr - d[j - 1]) / (d[j] - d[j - 1]) * (t_hr[j] - t_hr[j - 1])
    for rows in (2, 3, 7, None):
        got = hecras._first_reach(ts, t_hr, elev, thr, n, cols=cols, rows=rows)
        assert np.array_equal(np.isnan(got), np.isnan(want)), (rows, got, want)
        ok = ~np.isnan(want)
        assert np.allclose(got[ok], want[ok]), (rows, got, want)


@test
def hecras_terrain_depth_falls_back_to_cell_minimum_without_terrain():
    t = _terrain()
    t[:NP, :NP] = np.nan                             # no terrain under a dry corner cell
    t[NP:2 * NP, NP:NP + 3] = np.nan                 # nor under part of the centre cell
    out, info = hecras.terrain_depth(_fake(), *_grid_args(), t)
    assert info["no_terrain_cells"] == NP * NP + 3 * NP
    d = out["depth"]
    assert np.allclose(d[NP:2 * NP, NP:NP + 3], WSE_C - DITCH_A)
    assert not np.isfinite(d[:NP, :NP]).any()
    assert any("minimum elevation" in n for n in info["notes"])


@test
def hecras_terrain_on_grid_is_feet_from_a_metre_terrain():
    from nd_breach_runner.core import gdal_io
    like = Grid(np.zeros((2, 2)), 0.0, 20.0, DX, crs_wkt=WKT)
    saved = (gdal_io.read_like, gdal_io.z_to_m_factor)
    try:
        gdal_io.read_like = lambda p, g, alg="bilinear": np.full((2, 2), 30.48) if alg == \
            "average" else None
        gdal_io.z_to_m_factor = lambda p, choice="auto": (1.0, "metres")
        assert np.allclose(hecras.terrain_on_grid("t.tif", like), 100.0)
    finally:
        gdal_io.read_like, gdal_io.z_to_m_factor = saved


@test
def hecras_reexport_from_a_synthetic_hdf_into_a_new_folder():
    from nd_breach_runner.core import gdal_io
    tmp = tempfile.mkdtemp()
    saved = []

    def patch(obj, name, val):
        saved.append((obj, name, getattr(obj, name)))
        setattr(obj, name, val)
    try:
        run = os.path.join(tmp, "DAM-1")
        proj = os.path.join(tmp, "proj")
        hdf = os.path.join(proj, "run", "DAM_1.p01.hdf")
        for d in (os.path.join(run, "_hecras"), os.path.join(proj, "Terrain"),
                  os.path.dirname(hdf)):
            os.makedirs(d)
        terrain = os.path.join(proj, "Terrain", "DAM_1 Terrain.tif")
        for p in (terrain, os.path.join(proj, "Terrain", "Terrain.DAM_1 Terrain.tif")):
            open(p, "w").write("stand-in")
        old_depth = os.path.join(run, "Depth (Max).tif")
        open(old_depth, "w").write("the run's own raster")
        layout, root = _layout()
        if _has_h5py():
            _write_h5(hdf, layout, root)
        else:
            open(hdf, "w").write("stand-in")
            real_open = hecras._open_hdf
            patch(hecras, "_open_hdf", lambda h: (_FakeH5(layout, root), (lambda: None))
                  if str(h) == hdf else real_open(h))
        like = Grid(np.zeros((3 * NP, 3 * NP)), 0.0, 3 * CELL, DX, crs_wkt=WKT)
        seen = []

        def read_like(p, g, alg="bilinear"):
            seen.append((p, alg))
            return _terrain() * FT                        # a metre terrain
        grids = []
        patch(gdal_io, "read_grid", lambda p: grids.append(p) or like)
        patch(gdal_io, "read_like", read_like)
        patch(gdal_io, "z_to_m_factor", lambda p, choice="auto": (1.0, "metres"))
        patch(gdal_io, "GeoTiffWriter", lambda crs: npy_writer)
        patch(gdal_io, "linear_unit_m", lambda wkt: FT)

        rep = hecras.reexport_results(hdf, dam_folder=run)
        out = os.path.dirname(rep["outputs"]["depth"])
        assert os.path.dirname(out) == os.path.join(run, "_hecras"), out
        assert os.path.basename(out).startswith("reexport_")
        assert grids == [old_depth], grids                  # the dam folder's grid
        assert seen == [(terrain, "average")], seen         # the model terrain, not HEC's copy
        assert open(old_depth).read() == "the run's own raster"
        assert rep["depth_basis"] == hecras.DEPTH_BASIS_TERRAIN
        assert rep["results_hdf"] == os.path.abspath(hdf)
        d = np.load(os.path.join(out, "HECRAS Depth (Max).npy"))
        assert int(np.isfinite(d).sum()) == 2 * NP
        assert np.allclose(d[_terrain() == DITCH_A], WSE_C - DITCH_A, atol=1e-4)
        arr = np.load(os.path.join(out, "HECRAS Arrival Time.npy"))
        assert np.array_equal(np.isfinite(arr), np.isfinite(d))
        px_ac = DX * DX * FT * FT / 4046.8564224
        assert abs(rep["areas_old_cell_min"]["ge_1ft_ac"] - NP * NP * px_ac) < 1e-9
        assert abs(rep["areas_new"]["ge_1ft_ac"] - 2 * NP * px_ac) < 1e-9
        assert json.load(open(os.path.join(out, hecras.REEXPORT_JSON)))["areas_new"]
        # the same folder again: refused unless overwrite
        try:
            hecras.reexport_results(hdf, dam_folder=run, out_dir=out)
            raise AssertionError("re-export overwrote without overwrite=True")
        except hecras.HecRasError as e:
            assert "overwrite" in str(e)
        hecras.reexport_results(hdf, dam_folder=run, out_dir=out, overwrite=True)
        # no grid to write on: refused
        try:
            hecras.reexport_results(hdf, out_dir=os.path.join(tmp, "x"))
            raise AssertionError("re-export without a grid")
        except hecras.HecRasError as e:
            assert "grid" in str(e)
    finally:
        for obj, name, val in reversed(saved):
            setattr(obj, name, val)
        shutil.rmtree(tmp, ignore_errors=True)


@test
def hecras_reexport_cli_is_registered():
    from nd_breach_runner.tests.test_cli import cli as _cli
    cli = _cli()
    ns = cli.build_parser().parse_args(["hecras-reexport", "/x.p01.hdf", "--out", "/y",
                                        "--dam-folder", "/d", "--t0-hr", "1.5"])
    assert ns.func is cli.cmd_hecras_reexport and ns.hdf == "/x.p01.hdf" and ns.out == "/y"
    assert ns.dam_folder == "/d" and ns.t0_hr == 1.5 and ns.arrival_depth_ft == 1.0


@test
def hecras_import_names_shared_with_the_import_tool():
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "algorithms_hecras.py"), encoding="utf-8").read()
    assert "IMPORT_NAMES = hecras.IMPORT_NAMES" in src and '"TERRAIN"' in src
    assert "hecras.terrain_depth(" in src and "hecras.terrain_on_grid(" in src


@test
def hecras_import_algorithm_uses_the_terrain_when_given():
    from nd_breach_runner.tests.test_hecras import _algs, _Patch
    h, fq = _algs()
    d = tempfile.mkdtemp()
    like = Grid(np.zeros((3 * NP, 3 * NP)), 0.0, 3 * CELL, DX, crs_wkt=WKT)
    seen = []

    class Layer:
        def source(self):
            return "terrain.tif"

    def read_like(p, g, alg="bilinear"):
        seen.append((p, alg))
        return _terrain() * FT
    try:
        dam = os.path.join(d, "dam")
        os.makedirs(dam)
        open(os.path.join(dam, "Depth (Max).tif"), "w").write("x")
        with _Patch((hecras, "_open_hdf", lambda p: (_fake(), lambda: None)),
                    (h.gdal_io, "GeoTiffWriter", lambda crs: npy_writer),
                    (h.gdal_io, "read_grid", lambda p: like),
                    (h.gdal_io, "read_like", read_like),
                    (h.gdal_io, "z_to_m_factor", lambda p, choice="auto": (1.0, "metres")),
                    (h.gdal_io, "linear_unit_m", lambda wkt: FT),
                    (h._ToolBase, "_need_gdal", lambda self: None)):
            a = h.HecRasImportAlgorithm()
            a.initAlgorithm()
            for terrain, wet_px in ((Layer(), 2 * NP), (None, NP * NP)):
                fb = fq.QgsProcessingFeedback()
                p = {"HDF": "run.p01.hdf", "DAM_FOLDER": dam, "T0_HR": 0.0}
                if terrain is not None:
                    p["TERRAIN"] = terrain
                r = a.processAlgorithm(p, fq.QgsProcessingContext(), fb)
                dep = np.load(os.path.join(r["FOLDER"], "HECRAS Depth (Max).npy"))
                assert int(np.isfinite(dep).sum()) == wet_px, (terrain, np.isfinite(dep).sum())
            assert seen == [("terrain.tif", "average")], seen
            assert any("minimum elevation" in m for m in fb.errors), fb.errors
    finally:
        shutil.rmtree(d, ignore_errors=True)
