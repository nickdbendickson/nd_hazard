"""3.26.8: Build a HEC-RAS 2D model and cells the perimeter cuts in two. HEC-RAS 6.6 stopped
the Absaraka (DWR-2801) culvert model with '1 face errors detected on the perimeter of the
mesh': a grid point beside a narrow notch of the perimeter had a Voronoi cell that the
perimeter split into two pieces. Every cell that reaches the perimeter is checked; an export
with none is unchanged. When some are cut in two, their points are removed in repeated passes
(a removed point's neighbours across the notch take its area and can be cut in turn), and the
2D area is drawn once more with its narrow notches closed; that one is used only when it is
no worse on every count and needs fewer points removed. Any cells left are reported.

The split cells are also counted here with an independent check: every point's Voronoi cell
clipped to the perimeter, with no shortcut on which cells can reach it."""
import os
import re
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


def _own_cells(per, pts):
    """Independent of hecras_model's checks: every point's Voronoi cell (closed by four far
    points), clipped to the perimeter. Returns (indices of the cells in two or more pieces,
    indices of the cells in one piece with more than 8 corners)."""
    from osgeo import ogr
    from scipy.spatial import Voronoi
    per = np.asarray(per, float)
    pts = np.asarray(pts, float)
    span = float(np.ptp(per, axis=0).max())
    lo, hi = per.min(axis=0) - span, per.max(axis=0) + span
    vor = Voronoi(np.vstack([pts, [(lo[0], lo[1]), (hi[0], lo[1]), (hi[0], hi[1]),
                                   (lo[0], hi[1])]]))
    poly = hm._polygon(per)
    split, over = [], []
    for i in range(len(pts)):
        reg = vor.regions[vor.point_region[i]]
        assert reg and -1 not in reg
        g = hm._polygon(vor.vertices[reg]).Intersection(poly)
        if g is None or g.IsEmpty():
            continue
        t = ogr.GT_Flatten(g.GetGeometryType())
        parts = [g] if t == ogr.wkbPolygon else [
            g.GetGeometryRef(k) for k in range(g.GetGeometryCount())
            if ogr.GT_Flatten(g.GetGeometryRef(k).GetGeometryType()) == ogr.wkbPolygon]
        parts = [q for q in parts if q.GetArea() > 0]
        if len(parts) > 1:
            split.append(i)
        elif parts and parts[0].GetGeometryRef(0).GetPointCount() - 1 > 8:
            over.append(i)
    return split, over


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


def _srs():
    from osgeo import osr
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(2266)
    return srs


def _slot(x0=1080.0, w=10.0, depth=600.0):
    """The same square with the slot off the grid's bisector (x = x0 .. x0 + w), thinned as
    build_model thins its perimeter, and the grid build_model lays inside it."""
    srs = _srs()
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
    assert sorted(split.tolist()) == _own_cells(per, pts)[0]
    left, n, still = hm.drop_perimeter_split_points(per, pts, CELL)
    assert n >= len(split) and len(left) == len(pts) - n and len(still) == 0
    assert _own_cells(per, left)[0] == []


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
    """No injected point: the grid build_model lays beside a 10 ft slot. One pass of removal
    is not enough (the neighbours across the slot take the area and are cut in turn); the
    passes clear them all, by removing points only. A cell grown by removed neighbours can
    reach the perimeter from more than 3 cells away: the independent check of every cell
    confirms none is left."""
    if _no_scipy():
        return
    for x0 in (1020.0, 1060.0, 1080.0, 1085.0):
        per, pts, _ = _slot(x0)
        first = hm.perimeter_split_cells(per, pts, CELL)
        assert len(first) > 0 and sorted(first.tolist()) == _own_cells(per, pts)[0]
        assert len(_own_cells(per, np.delete(pts, first, axis=0))[0]) > 0
        left, n, still = hm.drop_perimeter_split_points(per, pts, CELL)
        assert n > len(first) and len(left) == len(pts) - n
        assert len(still) == 0
        assert _own_cells(per, left)[0] == [], x0
        have = {tuple(p) for p in pts.tolist()}
        assert all(tuple(p) in have for p in left.tolist())
    per, pts, _ = _slot()
    first = hm.perimeter_split_cells(per, pts, CELL)
    # out of passes: the cells still cut in two are returned, indices into the points left
    left1, n1, still1 = hm.drop_perimeter_split_points(per, pts, CELL, passes=1)
    assert n1 == len(first) and len(still1) > 0
    assert sorted(still1.tolist()) == _own_cells(per, left1)[0]
    # never below min_points: nothing removed, the split cells reported
    left2, n2, still2 = hm.drop_perimeter_split_points(per, pts, CELL, min_points=len(pts))
    assert n2 == 0 and len(left2) == len(pts) and np.array_equal(still2, first)
    # stopped by min_points after the first pass: what is left comes from a check of every
    # cell, not only of the removed points' neighbours
    left3, n3, still3 = hm.drop_perimeter_split_points(per, pts, CELL,
                                                       min_points=len(pts) - len(first) - 1)
    assert n3 == len(first) and len(still3) > 1
    assert sorted(still3.tolist()) == _own_cells(per, left3)[0]


@test
def v3268_the_passes_never_end_with_more_cells_cut_in_two_than_they_had():
    """Beside a V notch the removals can spread instead of dying out (cut in two: 3, then 2,
    then 3 again). When the passes run out there, the points are those of the pass that left
    fewest cells cut in two, and the mending then clears those."""
    if not T._has_gdal() or _no_scipy():
        return
    per = hm.thin_ring(np.array([(0, 0), (1000, 0), (1020, 300), (1040, 0), (2000, 0),
                                 (2000, 2000), (0, 2000)], float), 0.9 * CELL)
    pts = hm._grid_points(per, CELL, _srs())
    first = hm.perimeter_split_cells(per, pts, CELL)
    one = np.delete(pts, first, axis=0)
    second = hm.perimeter_split_cells(per, one, CELL)
    two = np.delete(one, second, axis=0)
    assert len(first) == 3 and len(second) == 2 and len(_own_cells(per, two)[0]) == 3
    left, n, still = hm.drop_perimeter_split_points(per, pts, CELL, passes=2)
    assert n == 3 and np.array_equal(left, one)
    assert sorted(still.tolist()) == _own_cells(per, left)[0] and len(still) == 2
    gone = pts[first]
    out, done, split, over = hm._mend(per, left, CELL, np.zeros((0, 2)), gone, still,
                                      np.zeros(0, np.int64))
    assert done["split"] >= 1 and len(split) < len(still)
    assert sorted(split.tolist()) == _own_cells(per, out)[0]


@test
def v3268_a_perimeter_that_crosses_itself_is_checked_too():
    """A notch whose sides cross (as thinning a narrow notch can leave): GEOS refuses to clip
    a cell to such a perimeter, so those cells went unchecked. They are clipped to the
    perimeter made valid, and the cells the notch cuts in two are found and cleared."""
    if not T._has_gdal() or _no_scipy():
        return
    per = np.array([(0, 0), (1080, 0), (1090, 600), (1080, 600), (1090, 0), (2000, 0),
                    (2000, 2000), (0, 2000)], float)
    assert not hm._polygon(per).IsValid()
    pts = hm._grid_points(per, CELL, _srs())
    split = hm.perimeter_split_cells(per, pts, CELL)
    assert sorted(map(tuple, pts[split].tolist())) == [(1050.0, y) for y in
                                                       (50.0, 150.0, 250.0, 350.0, 450.0, 550.0)]
    left, n, still = hm.drop_perimeter_split_points(per, pts, CELL)
    assert n >= len(split) and len(still) == 0
    assert len(hm.perimeter_split_cells(per, left, CELL)) == 0


@test
def v3268_closing_fills_notches_narrower_than_1_2_cells_and_only_adds():
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
    assert _own_cells(per2, pts2)[0] == []
    # the width: a notch of 1.1 cells is closed, one of 1.3 cells stays open
    assert hm.NOTCH_CLOSE_CELLS == 1.2
    for w, shut in ((1.1 * CELL, True), (1.3 * CELL, False)):
        g = hm._polygon([(0, 0), (1000 - w / 2, 0), (1000 - w / 2, 600), (1000 + w / 2, 600),
                         (1000 + w / 2, 0), (2000, 0), (2000, 2000), (0, 2000)])
        assert hm.close_notches(g, hm.NOTCH_CLOSE_CELLS * CELL).Contains(
            hm._point(1000.0, 300.0)) == shut, w
    # nothing usable from GEOS: the geometry as it was
    from osgeo import ogr
    empty = ogr.Geometry(ogr.wkbPolygon)
    assert hm.close_notches(empty, 120.0) is empty


def _min_angle(ring):
    a = np.asarray(ring, float)
    best = 180.0
    for i in range(len(a)):
        v1, v2 = a[i - 1] - a[i], a[(i + 1) % len(a)] - a[i]
        n1, n2 = np.hypot(*v1), np.hypot(*v2)
        if n1 > 1e-9 and n2 > 1e-9:
            best = min(best, np.degrees(np.arccos(np.clip(np.dot(v1, v2) / n1 / n2, -1, 1))))
    return best


@test
def v3268_closing_only_where_cells_are_cut_and_leaves_no_slit():
    """The 2D area's notches are closed only near the cells cut in two (the rest of the 2D
    area, its other notches and the dam line, stays as drawn), and the fill leaves no slit
    of zero width along a notch's bent side (a needle of the perimeter into a cell, which
    gave it more than 8 faces)."""
    if _no_scipy():
        return
    sq = hm._polygon([(0, 0), (3000, 0), (3000, 3000), (0, 3000)])
    for bend in (20.0, 50.0):
        t = np.radians(90.0 - bend)
        a1 = np.array([1500.0, 600.0])
        bent = hm._line([(1500.0, -50.0), a1, a1 + 800.0 * np.array([np.cos(t), np.sin(t)])])
        other = hm._line([(500.0, -50.0), (500.0, 700.0)])
        g = sq.Difference(bent.Buffer(30.0, 1)).Difference(other.Buffer(30.0, 1)).Simplify(25.0)
        c = hm.close_notches(g, hm.NOTCH_CLOSE_CELLS * CELL, near=[tuple(a1)],
                             reach=2.0 * CELL)
        assert c.IsValid() and g.Difference(c).GetArea() < 1e-6 * g.GetArea()
        assert c.Contains(hm._point(1500.0, 300.0))             # this notch is filled
        assert not c.Contains(hm._point(500.0, 300.0))          # the other one is not
        assert _min_angle(hm._rings(c)[0][1]) > 10.0, bend
    # nothing near: nothing filled
    assert hm.close_notches(g, hm.NOTCH_CLOSE_CELLS * CELL, near=[(2900.0, 2900.0)],
                            reach=2.0 * CELL) is g


@test
def v3268_no_grid_point_on_the_perimeter():
    """A notch narrower than a cell whose edge runs along a row of grid points: the four
    neighbours of those points are inside, so the sliver test missed them and they were laid
    on the perimeter itself. Now no point is closer than NEAR_PERIMETER_CELLS cells."""
    if not T._has_gdal():
        print("  (skipped: no GDAL)")
        return
    per = np.array([(0, 0), (2000, 0), (2000, 1050.001), (1000, 1050.001), (1000, 1090),
                    (2000, 1090), (2000, 2000), (0, 2000)], float)
    ring = list(map(tuple, per)) + [tuple(per[0])]
    near0 = hm.NEAR_PERIMETER_CELLS
    try:
        hm.NEAR_PERIMETER_CELLS = 0.0
        before = hm._grid_points(per, CELL, _srs())
    finally:
        hm.NEAR_PERIMETER_CELLS = near0
    d0 = hm.point_segment_distance(before[:, 0], before[:, 1], ring)
    assert (d0 < 0.01).sum() >= 5            # the case: points on the notch's edge
    pts = hm._grid_points(per, CELL, _srs())
    d = hm.point_segment_distance(pts[:, 0], pts[:, 1], ring)
    assert d.min() >= hm.NEAR_PERIMETER_CELLS * CELL
    # only those go
    assert len(pts) == len(before) - (d0 < hm.NEAR_PERIMETER_CELLS * CELL).sum()
    # a plain square: the same points as the sliver test alone gives
    sq = np.array([(0, 0), (2000, 0), (2000, 2000), (0, 2000)], float)
    assert np.array_equal(hm._grid_points(sq, CELL, _srs()), _grid()[np.lexsort(
        (_grid()[:, 0], -_grid()[:, 1]))])


@test
def v3268_a_cell_that_removing_points_gave_more_than_8_faces_is_mended():
    """A 4 ft wide V notch 600 ft deep: the passes remove the points around it, and the cell
    left at its tip gets more than 8 faces (HEC-RAS reports it). The mending removes that
    point too (its neighbours take its area) and no cell is cut in two or has more than 8
    faces; a cell that already had more than 8 faces in the 2D area as drawn is left alone."""
    if not T._has_gdal() or _no_scipy():
        return
    per = hm.thin_ring(np.array([(0, 0), (1015, 0), (1017, 600), (1019, 0), (2000, 0),
                                 (2000, 2000), (0, 2000)], float), 0.9 * CELL)
    pts = hm._grid_points(per, CELL, _srs())
    assert _own_cells(per, pts)[1] == []
    cur, n, split, over = hm._drop_split(per, pts, CELL)
    assert n > 0 and len(split) == 0 and len(over) == 1
    assert _own_cells(per, cur) == ([], over.tolist())
    gone = np.array([q for q in pts.tolist() if q not in cur.tolist()])
    out, done, split2, over2 = hm._mend(per, cur, CELL, np.zeros((0, 2)), gone, split, over)
    assert done == {"split": 0, "over8": 1, "back": 0, "removed": 1, "moved": 0}, done
    assert len(split2) == 0 and len(over2) == 0
    assert _own_cells(per, out) == ([], [])
    assert len(out) == len(cur) - 1 and not np.any(np.all(out == cur[over[0]], axis=1))
    # the same cell, there before any removal: not this check's to mend
    out, done, split2, over2 = hm._mend(per, cur, CELL, cur[over], gone, split, over)
    assert not any(done.values()) and np.array_equal(out, cur) and len(over2) == 1


def _meas(**k):
    m = {"removed": 10, "split_left": 0, "over8": 0, "valid": True, "bc_cross": False,
         "bc_gap": 25.0,
         "near": 0, "wet_out": 1000.0, "wet": 1e7, "A": np.array([0.0, 0.0]),
         "B": np.array([0.0, -500.0]), "dam_len": 500.0, "found": 5}
    m.update(k)
    return m


@test
def v3268_the_closed_2d_area_is_kept_only_when_no_worse():
    p = _meas()
    why, better = hm._closed_worse(_meas(removed=0), p, CELL)
    assert why == [] and better
    why, better = hm._closed_worse(_meas(), p, CELL)          # the same: not worth it
    assert why == [] and not better
    worse = {"valid": False, "split_left": 1, "over8": 1, "bc_cross": True, "bc_gap": 5.0,
             "near": 1,
             "wet_out": 1100.0, "A": np.array([0.0, 30.0]), "B": np.array([0.0, -530.0]),
             "dam_len": 530.0}
    for k, v in worse.items():
        kw = {"removed": 0, k: v}
        if k in ("A", "B"):
            kw["dam_len"] = 530.0 if k == "B" else 500.0
        why, _ = hm._closed_worse(_meas(**kw), p, CELL)
        assert len(why) == 1, (k, why)
    # within the tolerances
    assert hm._closed_worse(_meas(removed=0, bc_gap=21.0, A=np.array([0.0, 20.0]),
                                  dam_len=520.0, wet_out=1005.0), p, CELL)[0] == []
    # better on the cells left even with more points removed, but not with a worse count
    assert hm._closed_worse(_meas(removed=50, split_left=0), _meas(split_left=2), CELL) == \
        ([], True)
    assert hm._closed_worse(_meas(removed=0, over8=1), p, CELL)[1]
    assert hm._closed_worse(_meas(removed=0, over8=1), p, CELL)[0]
    # a boundary line that crossed as drawn and does not: better; a perimeter that crossed
    # itself as drawn and does not: better
    assert hm._closed_worse(_meas(), _meas(bc_cross=True, bc_gap=0.0), CELL) == ([], True)
    assert hm._closed_worse(_meas(), _meas(valid=False), CELL) == ([], True)
    # what could not be measured for the closed one counts against it, not for the other
    nan = float("nan")
    assert len(hm._closed_worse(_meas(removed=0, wet_out=nan), p, CELL)[0]) == 1
    assert len(hm._closed_worse(_meas(removed=0, bc_gap=nan), p, CELL)[0]) == 1
    assert hm._closed_worse(_meas(removed=0), _meas(wet_out=nan, bc_gap=nan), CELL) == \
        ([], True)


def _carve(dfold, dry):
    """Dry ground in the run's Depth (Max).tif where dry(ux, uy) (UTM 14N offsets from the
    dam: ux down the valley, uy across it), as a road embankment that keeps a strip dry
    would leave."""
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
    ds.GetRasterBand(1).WriteArray(np.where(dry(ux, uy), -9999, a))
    ds = None


def _inlet(W, X1, y_end=-50.0):
    """A dry inlet from the north side of the valley, W m wide from X1 m below the dam, down
    to y_end m from the valley axis."""
    return lambda ux, uy: (ux > X1) & (ux < X1 + W) & (uy > y_end)


def _g01_points(path):
    with open(path, newline="") as f:
        for ln in f.read().split("\r\n"):
            if ln.startswith("Storage Area 2D Points="):
                return int(ln.split("=")[1])
    raise AssertionError("no 'Storage Area 2D Points=' in " + path)


def _geometry(r):
    m = r["model"]
    return [np.asarray(v, float) for v in (m.perimeter, m.points, m.bc_line, m.conn_line,
                                           m.weir, m.reservoir)]


def _same(r1, r2):
    a, b = _geometry(r1), _geometry(r2)
    return all(x.shape == y.shape and np.array_equal(x, y) for x, y in zip(a, b))


def _readme(r):
    with open(r["paths"]["readme"], encoding="utf-8") as f:
        return f.read()


def _terrain_ok(r):
    folder = os.path.dirname(r["paths"]["terrain_tif"])
    assert os.path.exists(r["paths"]["terrain_tif"])
    assert not [f for f in os.listdir(folder) if "notches" in f], os.listdir(folder)


OPTS = {"side_buffer_ft": 100.0, "cell_ft": 100.0}


def _skip_build():
    if not T._has_gdal():
        print("  (skipped: no GDAL)")
        return True
    return _no_scipy()


@test
def v3268_an_export_with_no_split_cell_is_unchanged():
    """No cell cut in two: the 2D area is never drawn again with its notches closed, no point
    is removed and nothing is noted; the export is the one the checks leave alone."""
    if _skip_build():
        return
    d = tempfile.mkdtemp()
    close0, drop0, cells0 = hm.close_notches, hm._drop_split, hm._perimeter_cells
    calls = []
    try:
        dfold, _dem, _dam, _hy = T.make_run(d)

        def no_close(geom, width, **kw):
            raise AssertionError("the notches were closed")

        def no_drop(*a, **k):
            raise AssertionError("points were removed")

        def spy(*a, **k):
            out = cells0(*a, **k)
            calls.append(len(out[0]))
            return out
        hm.close_notches, hm._drop_split, hm._perimeter_cells = no_close, no_drop, spy
        r = hm.build_model(dfold, os.path.join(d, "a"), options=OPTS)
        assert calls == [0]
        assert not [n for n in r["notes"] if "pre-check" in n or "WARNING" in n], r["notes"]
        # the same export with the check itself taken out
        hm._perimeter_cells = lambda per, pts, cell, **k: (
            np.zeros(0, np.int64), np.zeros(0, np.int64), np.zeros(len(pts), bool))
        r2 = hm.build_model(dfold, os.path.join(d, "b"), options=OPTS)
        assert _same(r, r2)
        assert _g01_points(r["paths"]["g01"]) == len(r["model"].points)
        _terrain_ok(r)
    finally:
        hm.close_notches, hm._drop_split, hm._perimeter_cells = close0, drop0, cells0
        shutil.rmtree(d, ignore_errors=True)


@test
def v3268_build_model_closes_a_narrow_dry_inlet_when_that_is_better():
    """A dry inlet 84 m wide with a 100 ft side buffer leaves a notch about 23 m wide in the
    2D area, and cells beside it are cut in two. As drawn, the passes remove points; with the
    notch closed none has to go and nothing else is worse, so that one is written, with a
    note. With the closing doing nothing the second 2D area is no better: the one as drawn
    is written, its points removed."""
    if _skip_build():
        return
    d = tempfile.mkdtemp()
    close0, wet0 = hm.close_notches, hm._wet_ground
    try:
        dfold, _dam, _dem, _hy = T.make_run(d)
        _carve(dfold, _inlet(84.0, 3000.0))
        widths = []

        def spy(geom, width, **kw):
            widths.append(width)
            return close0(geom, width, **kw)
        wet = []

        def wet_spy(*a, **k):
            g = wet0(*a, **k)
            wet.append(g is not None and g.GetArea() > 0)
            return g
        hm.close_notches, hm._wet_ground = spy, wet_spy
        r = hm.build_model(dfold, os.path.join(d, "closed"), options=OPTS)
        m = r["model"]
        assert widths == [hm.NOTCH_CLOSE_CELLS * m.cell] == [120.0]
        assert wet == [True, True]       # the flooded ground on the run's own cells, twice
        per, pts = np.asarray(m.perimeter), np.asarray(m.points)
        split, over = _own_cells(per, pts)
        assert split == [] and over == []
        note = [n for n in r["notes"] if "pre-check" in n]
        assert len(note) == 1, r["notes"]
        assert note[0].startswith("Perimeter pre-check: the 2D area as drawn had ") and \
            "its notches narrower than 120 ft there were closed" in note[0] and \
            "with the notches closed none is." in note[0], note[0]
        assert note[0] in _readme(r)
        assert not [n for n in r["notes"] if "WARNING" in n]
        assert _g01_points(r["paths"]["g01"]) == len(pts)
        _terrain_ok(r)

        # the closing does nothing: the 2D area as drawn, with the passes
        hm.close_notches = lambda geom, width, **kw: geom
        r2 = hm.build_model(dfold, os.path.join(d, "open"), options=OPTS)
        m2 = r2["model"]
        per2, pts2 = np.asarray(m2.perimeter), np.asarray(m2.points)
        assert _own_cells(per2, pts2)[0] == []
        found = int(re.search(r" had (\d+) cell\(s\)", note[0]).group(1))
        removed = int(re.search(r"As drawn, (\d+) point\(s\)", note[0]).group(1))
        pc = [n for n in r2["notes"] if "pre-check" in n]
        assert len(pc) == 2, r2["notes"]
        assert pc[0].startswith("Perimeter pre-check: {} cell(s) along the 2D perimeter are "
                                "cut in two".format(found)) and pc[0].endswith(
            "closed there was not used: it is no better."), pc[0]
        assert pc[1].startswith("Perimeter pre-check: {} computation point(s) whose cell the "
                                "perimeter cut in two were removed".format(removed)), pc[1]
        assert removed > 0
        assert not [n for n in r2["notes"] if "WARNING" in n]
        assert _g01_points(r2["paths"]["g01"]) == len(pts2)
        assert pc[0] in _readme(r2) and pc[1] in _readme(r2)
        _terrain_ok(r2)
        # the closed one adds ground only at the notch: it holds the one as drawn
        assert hm._polygon(per2).Difference(hm._polygon(per)).GetArea() < \
            1e-3 * hm._polygon(per2).GetArea()
    finally:
        hm.close_notches, hm._wet_ground = close0, wet0
        shutil.rmtree(d, ignore_errors=True)


@test
def v3268_build_model_keeps_the_2d_area_as_drawn_when_closing_is_worse():
    """The 84 m dry inlet again, but the 2D area with its notches closed also gets a narrow V
    notch under the downstream boundary line, as a notch left over by the closing would: its
    boundary line would cross the perimeter (HEC-RAS stops with 'BC Line intersects the
    perimeter'), which the 2D area as drawn does not. So the one as drawn is written, with
    its points removed (the same export as with no closing at all), and the README says
    why."""
    if _skip_build():
        return
    d = tempfile.mkdtemp()
    close0 = hm.close_notches
    try:
        dfold, _dam, _dem, _hy = T.make_run(d)
        _carve(dfold, _inlet(84.0, 3000.0))
        hm.close_notches = lambda geom, width, **kw: geom
        r0 = hm.build_model(dfold, os.path.join(d, "plain"), options=OPTS)
        m0 = r0["model"]
        bc, per0, cell = np.asarray(m0.bc_line), np.asarray(m0.perimeter), m0.cell
        b = bc[len(bc) // 2]
        dens = hm._ring_points(per0, cell / 20.0)
        q = dens[np.argmin(np.hypot(*(dens - b).T))]
        u = (q - b) / np.hypot(*(q - b))
        t = np.array([-u[1], u[0]])
        vee = hm._polygon([b - u * cell + 0.3 * cell * t, b - u * cell - 0.3 * cell * t,
                           q + 3.0 * cell * u])

        def spoiled(geom, width, **kw):
            return close0(geom, width, **kw).Difference(vee)
        hm.close_notches = spoiled
        r = hm.build_model(dfold, os.path.join(d, "a"), options=OPTS)
        m = r["model"]
        per, pts = np.asarray(m.perimeter), np.asarray(m.points)
        pc = [n for n in r["notes"] if "pre-check" in n]
        assert len(pc) == 2, r["notes"]
        assert pc[0].endswith("closed there was not used: the boundary line would cross "
                              "the perimeter."), pc[0]
        assert pc[1].startswith("Perimeter pre-check: 49 computation point(s) whose cell"), pc
        assert _same(r, r0)
        assert _own_cells(per, pts) == ([], [])
        assert not hm._line(m.bc_line).Intersects(hm._polygon(per))
        assert pc[0] in _readme(r)
        _terrain_ok(r)
    finally:
        hm.close_notches = close0
        shutil.rmtree(d, ignore_errors=True)


@test
def v3268_build_model_warns_of_cells_still_cut_in_two_or_made_over_8_faces():
    """When the passes leave cells cut in two, build_model still writes the model, with a
    WARNING note naming the points (HEC-RAS would stop on them); and a WARNING names the
    cells that removing points gave more than 8 faces."""
    if _skip_build():
        return
    d = tempfile.mkdtemp()
    drop0, close0 = hm._drop_split, hm.close_notches
    seen = []

    def fake(per, points, cell, **kw):
        pts = np.asarray(points, float)
        seen.append(len(pts))
        return pts[3:], 3, np.array([0, 1], np.int64), np.array([2], np.int64)
    try:
        dfold, _dem, _dam, _hy = T.make_run(d)
        _carve(dfold, _inlet(84.0, 3000.0))
        hm._drop_split = fake
        hm.close_notches = lambda geom, width, **kw: geom
        r = hm.build_model(dfold, os.path.join(d, "model"), options=OPTS)
        m = r["model"]
        assert len(seen) == 2 and len(m.points) == seen[0] - 3     # as drawn, and closed
        assert _g01_points(r["paths"]["g01"]) == len(m.points)
        warn = [n for n in r["notes"] if n.startswith("WARNING")]
        assert len(warn) == 2, r["notes"]
        assert "2 cell(s) along the perimeter are still cut in two" in warn[0]
        assert "face errors detected on the perimeter of the mesh" in warn[0]
        assert "({:.1f}, {:.1f})".format(*m.points[0]) in warn[0]
        assert "({:.1f}, {:.1f})".format(*m.points[1]) in warn[0]
        assert "({:.1f}, {:.1f})".format(*m.points[2]) not in warn[0]
        assert warn[1].startswith("WARNING: Removing the points whose cell the perimeter cut "
                                  "in two gave 1 cell(s) along the perimeter more than 8 "
                                  "faces"), warn[1]
        assert warn[1].endswith(": ({:.1f}, {:.1f}).".format(*m.points[2])), warn[1]
        assert any(n.startswith("Perimeter pre-check: 3 computation point(s)")
                   for n in r["notes"])
        text = _readme(r)
        assert warn[0] in text and warn[1] in text
    finally:
        hm._drop_split, hm.close_notches = drop0, close0
        shutil.rmtree(d, ignore_errors=True)
