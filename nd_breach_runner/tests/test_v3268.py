"""3.26.8: Build a HEC-RAS 2D model and cells the perimeter cuts in two. HEC-RAS 6.6 stopped
the Absaraka (DWR-2801) culvert model with '1 face errors detected on the perimeter of the
mesh': a grid point beside a narrow notch of the perimeter had a Voronoi cell that the
perimeter split into two pieces. Notches narrower than about a cell are closed before the
grid is laid; points whose cell is still cut in two are removed in repeated passes (a removed
point's neighbours across the notch take its area and can be cut in turn), and any left are
reported."""
import os
import shutil
import tempfile

import numpy as np

from nd_breach_runner.core import hecras_model as hm
from nd_breach_runner.tests import test_hecras_model as T
from nd_breach_runner.tests.registry import test

CELL = 100.0


def _no_scipy():
    if hm._voronoi() is None:
        print("  (skipped: no scipy)")
        return True
    return False


def _grid(n=2000.0, cell=CELL):
    xs = np.arange(cell / 2.0, n, cell)
    gx, gy = np.meshgrid(xs, xs)
    return np.column_stack([gx.ravel(), gy.ravel()])


def _notched():
    """A 2,000 ft square with a 10 ft wide, 600 ft deep notch cut in from the south side,
    a 100 ft grid, and one extra point beside the notch whose cell straddles it (the notch
    runs right through that cell, so the perimeter cuts it in two)."""
    per = np.array([(0, 0), (995, 0), (995, 600), (1005, 600), (1005, 0), (2000, 0),
                    (2000, 2000), (0, 2000)], float)
    return per, np.vstack([_grid(), [(1012.0, 300.0)]])


def _slot(x0=1080.0, w=10.0, depth=600.0):
    """The same square with the slot off the grid's bisector (x = 1080-1090), thinned as
    build_model thins its perimeter, and the grid build_model lays inside it."""
    from osgeo import osr
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(2266)
    per = np.array([(0, 0), (x0, 0), (x0, depth), (x0 + w, depth), (x0 + w, 0), (2000, 0),
                    (2000, 2000), (0, 2000)], float)
    per = hm.thin_ring(per, 0.9 * CELL)
    return per, hm._grid_points(per, CELL, srs), srs


@test
def v3268_a_cell_the_perimeter_cuts_in_two_is_found():
    if _no_scipy():
        return
    per, pts = _notched()
    split = hm.perimeter_split_cells(per, pts, CELL)
    assert split is not None and len(split) >= 1
    assert any(np.allclose(pts[i], (1012.0, 300.0)) for i in split)
    left, n, still = hm.drop_perimeter_split_points(per, pts, CELL)
    assert n >= len(split) and len(left) == len(pts) - n and len(still) == 0
    assert len(hm.perimeter_split_cells(per, left, CELL)) == 0


@test
def v3268_a_plain_grid_has_no_split_cells():
    if _no_scipy():
        return
    per = np.array([(0, 0), (2000, 0), (2000, 2000), (0, 2000)], float)
    pts = _grid()
    assert len(hm.perimeter_split_cells(per, pts, CELL)) == 0
    left, n, still = hm.drop_perimeter_split_points(per, pts, CELL)
    assert n == 0 and len(still) == 0 and np.array_equal(left, pts)


@test
def v3268_repeated_passes_clear_a_notch_off_the_grid_bisector():
    """No injected point: the grid build_model lays beside a 10 ft slot at x = 1080-1090.
    One pass of removal is not enough (the neighbours across the slot take the area and are
    cut in turn); the passes clear them all, by removing points only."""
    if _no_scipy():
        return
    per, pts, _ = _slot()
    first = hm.perimeter_split_cells(per, pts, CELL)
    assert len(first) > 0
    assert len(hm.perimeter_split_cells(per, np.delete(pts, first, axis=0), CELL)) > 0
    left, n, still = hm.drop_perimeter_split_points(per, pts, CELL)
    assert n > len(first) and len(left) == len(pts) - n
    assert len(still) == 0
    assert len(hm.perimeter_split_cells(per, left, CELL)) == 0
    have = {tuple(p) for p in pts.tolist()}
    assert all(tuple(p) in have for p in left.tolist())
    # out of passes: the cells still cut in two are returned, indices into the points left
    left1, n1, still1 = hm.drop_perimeter_split_points(per, pts, CELL, passes=1)
    assert n1 == len(first) and len(still1) > 0
    assert np.array_equal(np.sort(still1), np.sort(hm.perimeter_split_cells(per, left1, CELL)))
    # never below min_points: nothing removed, the split cells reported
    left2, n2, still2 = hm.drop_perimeter_split_points(per, pts, CELL, min_points=len(pts))
    assert n2 == 0 and len(left2) == len(pts) and np.array_equal(still2, first)


@test
def v3268_closing_fills_a_narrow_notch_and_only_adds():
    if _no_scipy():
        return
    per, _, srs = _slot()
    poly = hm._polygon(per)
    closed = hm.close_notches(poly, hm.NOTCH_CLOSE_CELLS * CELL)
    assert closed.IsValid()
    assert poly.Difference(closed).GetArea() < 1e-6 * poly.GetArea()       # nothing lost
    assert 0 < closed.GetArea() - poly.GetArea() < 0.01 * poly.GetArea()
    assert closed.Contains(hm._point(1085.0, 300.0))                       # the slot is filled
    assert not closed.Contains(hm._point(1000.0, -50.0))
    per2 = hm._rings(closed)[0][1]
    pts2 = hm._grid_points(per2, CELL, srs)
    assert len(hm.perimeter_split_cells(per2, pts2, CELL)) == 0
    # a notch wider than the closing stays open
    wide = hm._polygon([(0, 0), (900, 0), (900, 600), (1100, 600), (1100, 0), (2000, 0),
                        (2000, 2000), (0, 2000)])
    assert not hm.close_notches(wide, hm.NOTCH_CLOSE_CELLS * CELL).Contains(
        hm._point(1000.0, 300.0))
    # nothing usable from GEOS: the geometry as it was
    from osgeo import ogr
    empty = ogr.Geometry(ogr.wkbPolygon)
    assert hm.close_notches(empty, 120.0) is empty


def _carve_inlet(dfold, W, X1, y_end=-50.0):
    """A dry inlet in the run's Depth (Max).tif from the north side of the valley, W m wide
    from X1 m below the dam, down to y_end m from the valley axis (UTM 14N offsets from the
    dam), as a road embankment that keeps a strip dry would leave."""
    from osgeo import gdal, osr
    p = os.path.join(dfold, "Depth (Max).tif")
    ds = gdal.Open(p, gdal.GA_Update)
    gt = ds.GetGeoTransform()
    a = ds.GetRasterBand(1).ReadAsArray()
    s = osr.SpatialReference()
    s.ImportFromWkt(ds.GetProjection())
    s.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    u = osr.SpatialReference()
    u.ImportFromEPSG(26914)
    u.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    tr = osr.CoordinateTransformation(s, u)
    ny, nx = a.shape
    X, Y = np.meshgrid(gt[0] + (np.arange(nx) + 0.5) * gt[1],
                       gt[3] + (np.arange(ny) + 0.5) * gt[5])
    q = np.array(tr.TransformPoints(np.column_stack([X.ravel(), Y.ravel()])))[:, :2]
    ux = q[:, 0].reshape(a.shape) - T.X0
    uy = q[:, 1].reshape(a.shape) - T.Y0
    ds.GetRasterBand(1).WriteArray(np.where((ux > X1) & (ux < X1 + W) & (uy > y_end), -9999, a))
    ds = None


def _g01_points(path):
    with open(path, newline="") as f:
        for ln in f.read().split("\r\n"):
            if ln.startswith("Storage Area 2D Points="):
                return int(ln.split("=")[1])
    raise AssertionError("no 'Storage Area 2D Points=' in " + path)


OPTS = {"side_buffer_ft": 100.0, "cell_ft": 100.0}


@test
def v3268_build_model_closes_a_narrow_dry_inlet_and_removes_what_is_still_split():
    """A dry inlet 84 m wide with a 100 ft side buffer leaves a notch about 23 m wide in the
    2D area. build_model closes it: no cell is cut in two and none has more than 8 faces.
    Without the closing (the PR's first version), the cells beside it are cut in two, and
    build_model's passes remove those points: the note says how many, and the .g01 carries
    the points that are left."""
    if not T._has_gdal():
        print("  (skipped: no GDAL)")
        return
    if _no_scipy():
        return
    d = tempfile.mkdtemp()
    close0, drop0 = hm.close_notches, hm.drop_perimeter_split_points
    try:
        dfold, _dem, _dam, _hy = T.make_run(d)
        _carve_inlet(dfold, 84.0, 3000.0)
        added = []

        def close_spy(geom, width):
            g = close0(geom, width)
            added.append(g.GetArea() - geom.GetArea())
            return g
        hm.close_notches = close_spy
        r = hm.build_model(dfold, os.path.join(d, "closed"), options=OPTS)
        m = r["model"]
        per, pts = np.asarray(m.perimeter), np.asarray(m.points)
        assert len(added) == 1 and added[0] > 0
        assert len(hm.perimeter_split_cells(per, pts, m.cell)) == 0
        assert hm.mesh_check(per, pts, m.cell)["over_8_faces"] == 0
        assert not [n for n in r["notes"] if "pre-check" in n or "WARNING" in n], r["notes"]
        assert _g01_points(r["paths"]["g01"]) == len(pts)

        # the backstop alone: no closing
        calls = []

        def drop_spy(perimeter, points, cell, **kw):
            out = drop0(perimeter, points, cell, **kw)
            calls.append((len(points), out[1], out[2]))
            return out
        hm.close_notches = lambda geom, width: geom
        hm.drop_perimeter_split_points = drop_spy
        r = hm.build_model(dfold, os.path.join(d, "open"), options=OPTS)
        m = r["model"]
        per, pts = np.asarray(m.perimeter), np.asarray(m.points)
        assert len(calls) == 1
        n_in, removed, still = calls[0]
        assert removed > 0 and len(still) == 0
        assert len(pts) == n_in - removed
        note = [n for n in r["notes"] if n.startswith("Perimeter pre-check")]
        assert len(note) == 1 and note[0].startswith(
            "Perimeter pre-check: {} computation point(s)".format(removed)), r["notes"]
        assert not [n for n in r["notes"] if "WARNING" in n]
        assert len(hm.perimeter_split_cells(per, pts, m.cell)) == 0
        assert _g01_points(r["paths"]["g01"]) == len(pts)
        with open(r["paths"]["readme"], encoding="utf-8") as f:
            assert note[0] in f.read()
    finally:
        hm.close_notches, hm.drop_perimeter_split_points = close0, drop0
        shutil.rmtree(d, ignore_errors=True)


@test
def v3268_build_model_warns_of_cells_still_cut_in_two():
    """When the passes leave cells cut in two, build_model still writes the model, with a
    WARNING note naming the points (HEC-RAS would stop on them)."""
    if not T._has_gdal():
        print("  (skipped: no GDAL)")
        return
    if _no_scipy():
        return
    d = tempfile.mkdtemp()
    drop0 = hm.drop_perimeter_split_points
    seen = []

    def fake(perimeter, points, cell, **kw):
        pts = np.asarray(points, float)
        seen.append(len(pts))
        return pts[3:], 3, np.array([0, 1], np.int64)
    try:
        dfold, _dem, _dam, _hy = T.make_run(d)
        hm.drop_perimeter_split_points = fake
        r = hm.build_model(dfold, os.path.join(d, "model"), options=OPTS)
        m = r["model"]
        assert len(m.points) == seen[0] - 3
        assert _g01_points(r["paths"]["g01"]) == len(m.points)
        warn = [n for n in r["notes"] if n.startswith("WARNING")]
        assert len(warn) == 1, r["notes"]
        assert "2 cell(s) along the perimeter are still cut in two" in warn[0]
        assert "face errors detected on the perimeter of the mesh" in warn[0]
        assert "({:.1f}, {:.1f})".format(*m.points[0]) in warn[0]
        assert "({:.1f}, {:.1f})".format(*m.points[1]) in warn[0]
        assert any(n.startswith("Perimeter pre-check: 3 computation point(s)")
                   for n in r["notes"])
        with open(r["paths"]["readme"], encoding="utf-8") as f:
            assert warn[0] in f.read()
    finally:
        hm.drop_perimeter_split_points = drop0
        shutil.rmtree(d, ignore_errors=True)
