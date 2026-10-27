"""3.26.8: Build a HEC-RAS 2D model and cells the perimeter cuts in two. HEC-RAS 6.6 stopped
the Absaraka (DWR-2801) culvert model with '1 face errors detected on the perimeter of the
mesh': a grid point beside a narrow notch of the perimeter had a Voronoi cell that the
perimeter split into two pieces. Every cell that reaches the perimeter is checked; an export
with none is unchanged. When some are cut in two, their points are removed in repeated passes
(a removed point's neighbours across the notch take its area and can be cut in turn), the
cells still cut in two or with more than 8 faces are mended (a point put back, removed, moved
or added), and the 2D area is drawn once more with its narrow notches closed. The better of
the two is written: fewer problems that stop HEC-RAS first, then less flooded ground left
outside, the dam line, fewer points removed. Never more problems than the 2D area as drawn
had with its grid points as laid before 3.26.8 (none removed, or removed once). Any problems
left are reported.

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
    out, done, split, over = hm._mend(per, left, CELL, gone, still, np.zeros(0, np.int64))
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
    faces."""
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
    out, done, split2, over2 = hm._mend(per, cur, CELL, gone, split, over)
    assert done == {"split": 0, "over8": 1, "back": 0, "removed": 1, "moved": 0,
                    "added": 0}, done
    assert len(split2) == 0 and len(over2) == 0
    assert _own_cells(per, out) == ([], [])
    assert len(out) == len(cur) - 1 and not np.any(np.all(out == cur[over[0]], axis=1))
    # no tries: nothing is done
    out, done, split2, over2 = hm._mend(per, cur, CELL, gone, split, over, tries=0)
    assert not any(done.values()) and np.array_equal(out, cur) and len(over2) == 1


def _saw(*spans, step=12.5, amp=10.0):
    """A 2,000 ft square whose south side is a saw (teeth `step` apart, `amp` high) under
    each span (x0, x1): the grid point above it gets a cell of more than 8 faces."""
    south = [(0.0, 0.0)]
    for x0, x1 in spans:
        k = int(round((x1 - x0) / step))
        south += [(x0 + j * step, amp if j % 2 else 0.0) for j in range(k + 1)]
    return np.array(south + [(2000, 0), (2000, 2000), (0, 2000)], float)


@test
def v3268_mending_adds_a_point_where_nothing_else_helps():
    """A cell of more than 8 faces along a saw-toothed stretch of the perimeter: putting a
    point back (there is none to put back), removing its point (the neighbours take the saw)
    or moving it does not help; a point added in the cell splits the saw between two cells.
    Each such cell is one try: with fewer tries than cells, some are left."""
    if _no_scipy():
        return
    per = _saw((900.0, 1000.0))
    pts = _grid()
    split, over = hm._perimeter_cells(per, pts, CELL)[:2]
    assert len(split) == 0 and pts[over].tolist() == [[950.0, 50.0]]
    assert _own_cells(per, pts) == ([], over.tolist())
    for alt in (np.delete(pts, over, axis=0),):
        assert len(_own_cells(per, alt)[1]) > 0           # removing it does not help
    out, done, s2, o2 = hm._mend(per, pts, CELL, np.zeros((0, 2)), split, over)
    assert done == {"split": 0, "over8": 1, "back": 0, "removed": 0, "moved": 0,
                    "added": 1}, done
    assert len(s2) == 0 and len(o2) == 0 and _own_cells(per, out) == ([], [])
    assert np.array_equal(out[:len(pts)], pts) and len(out) == len(pts) + 1
    q = out[-1]
    ring = list(map(tuple, per)) + [tuple(per[0])]
    assert hm._polygon(per).Contains(hm._point(*q))
    assert hm.point_segment_distance(q[:1], q[1:], ring)[0] >= hm.ADD_PERIMETER_CELLS * CELL
    assert np.hypot(*(pts - q).T).min() >= 0.25 * CELL
    assert hm.ADD_PERIMETER_CELLS == 0.2
    # three such cells take three tries
    per3 = _saw((300.0, 400.0), (900.0, 1000.0), (1500.0, 1600.0))
    split, over = hm._perimeter_cells(per3, pts, CELL)[:2]
    assert len(over) == 3
    out, done, s2, o2 = hm._mend(per3, pts, CELL, np.zeros((0, 2)), split, over)
    assert done["added"] == 3 and len(o2) == 0 and _own_cells(per3, out) == ([], [])
    out, done, s2, o2 = hm._mend(per3, pts, CELL, np.zeros((0, 2)), split, over, tries=2)
    assert done["added"] == 2 and len(o2) == 1 and len(out) == len(pts) + 2


def _mended(per, pts, spare, passes=None, **kw):
    """_mend after the passes (when given), checked: the points left are the ones given
    plus those added or put back, none of them outside the perimeter, closer than
    ADD_PERIMETER_CELLS cells to it or a quarter cell to another point, and the counts are a
    check of every cell (the independent one, where the perimeter does not cross itself)."""
    if passes is not None:
        left, _, split, over = hm._drop_split(per, pts, CELL, passes=passes)
        spare = np.array([q for q in pts.tolist() if q not in left.tolist()]).reshape(-1, 2)
        pts = left
    else:
        split, over = hm._perimeter_cells(per, pts, CELL)[:2]
    out, done, s2, o2 = hm._mend(per, pts, CELL, spare, split, over, **kw)
    s3, o3 = hm._perimeter_cells(per, out, CELL)[:2]
    assert np.array_equal(s2, s3) and np.array_equal(o2, o3)
    if hm._polygon(per).IsValid():
        assert (sorted(s2.tolist()), sorted(o2.tolist())) == _own_cells(per, out)
    have = {tuple(q) for q in np.asarray(pts).tolist()} | {
        tuple(q) for q in np.asarray(spare).tolist()}
    new = np.array([q for q in out.tolist() if tuple(q) not in have]).reshape(-1, 2)
    ring = list(map(tuple, per)) + [tuple(per[0])]
    if len(new):
        assert hm.point_segment_distance(new[:, 0], new[:, 1], ring).min() >= \
            hm.ADD_PERIMETER_CELLS * CELL - 1e-9
        inside = hm._valid(hm._polygon(per))
        assert all(inside.Contains(hm._point(*q)) for q in new)
    for q in new:
        dq = np.hypot(*(out - q).T)
        assert np.sort(dq)[1] >= 0.25 * CELL - 1e-9
    return out, {k: v for k, v in done.items() if v}, s2, o2, split, over


@test
def v3268_mending_puts_back_moves_or_adds_a_point_only_where_that_helps():
    """Each way of mending, where it is the one that leaves fewer cells cut in two or of more
    than 8 faces over the whole 2D area: a removed point put back, a point moved to the
    middle of its cell, a point added at the middle of a piece of a cell cut in two. Nothing
    is done where nothing helps (a point would have to go closer than ADD_PERIMETER_CELLS
    cells to the perimeter), and no change that leaves as many is kept."""
    if not T._has_gdal() or _no_scipy():
        return
    # put back: the cell beside a removed point takes over the saw under it
    per = _saw((900.0, 1000.0), step=25.0, amp=5.0)
    gone = np.array([(1050.0, 50.0)])
    pts = _grid()
    pts = pts[~np.all(pts == gone[0], axis=1)]
    out, done, s2, o2, _, over = _mended(per, pts, gone)
    assert len(over) == 1 and done == {"over8": 1, "back": 1} and len(o2) == 0, done
    assert np.array_equal(out[:-1], pts) and np.array_equal(out[-1], gone[0])
    # moved: beside a slot 30 ft wide, after two passes; the other cells still cut in two
    # there are mended only by a point added around them (a tenth of a cell apart, in a
    # neighbouring cell too)
    per, pts, _ = _slot(1080.0, w=30.0)
    out, done, s2, o2, split, _ = _mended(per, pts, None, passes=2)
    assert done == {"split": 4, "moved": 1, "added": 3} and len(split) == 4, done
    assert len(s2) == len(o2) == 0
    new = [q for q in out.tolist() if q not in pts.tolist()]
    assert len(new) == 4                    # the one moved and the three added
    # added anywhere in the other piece of a cell cut in two (none of the spots tried first
    # helps): across the slot, 25 ft wide, from its point
    per, pts, _ = _slot(1040.0, w=25.0)
    out, done, s2, o2, split, _ = _mended(per, pts, None, passes=2)
    assert done == {"split": 1, "added": 1} and len(s2) == 1, done
    new = [q for q in out.tolist() if q not in pts.tolist()]
    assert len(new) == 1 and new[0][0] < 1040.0, new
    # added in the other piece of a cell cut in two, or around it (and removed points put
    # back)
    per, pts, _ = _slot(1080.0, w=10.0)
    out, done, s2, o2, split, _ = _mended(per, pts, None, passes=2)
    assert done == {"split": 6, "back": 3, "added": 3} and len(split) == 6 and len(s2) == 0, done
    # a V notch: one point added in the cell of more than 8 faces at its tip, no more
    per = hm.thin_ring(np.array([(0, 0), (1000, 0), (1010, 600), (1020, 0), (2000, 0),
                                 (2000, 2000), (0, 2000)], float), 0.9 * CELL)
    pts = hm._grid_points(per, CELL, _srs())
    out, done, s2, o2, split, over = _mended(per, pts, None, passes=25)
    assert len(split) == 0 and len(over) == 1 and done == {"over8": 1, "added": 1}, done
    assert len(s2) == len(o2) == 0
    # nothing helps: the only points that would are too close to the perimeter
    per = _saw((900.0, 1000.0), step=12.5, amp=20.0)
    pts = _grid()
    out, done, s2, o2, _, over = _mended(per, pts, np.zeros((0, 2)))
    assert len(over) == 1 and done == {} and np.array_equal(out, pts) and len(o2) == 1


def _two_notches(seed):
    """A 2,000 ft square with two notches 1.2-1.6 cells wide from its south side and a spit
    0.3-1 cell wide between them, at small angles (random, from `seed`), as build_model
    draws its perimeter (simplified, thinned), and the grid it lays."""
    import math
    rng = np.random.default_rng(seed)
    size = 20 * CELL
    g = hm._polygon([(0, 0), (size, 0), (size, size), (0, size)])
    w1, w2 = rng.uniform(1.2, 1.6, 2)
    spit = rng.uniform(0.3, 1.0)
    x1 = rng.uniform(0.3, 0.5) * size
    x2 = x1 + (w1 / 2 + spit + w2 / 2) * CELL
    a1 = rng.uniform(-12, 12)
    a2 = a1 + rng.uniform(-4, 4)
    d1, d2 = rng.uniform(5, 12, 2) * CELL
    for x, dep, w, a in ((x1, d1, w1, a1), (x2, d2, w2, a2)):
        t = math.radians(a)
        v, nrm = np.array([math.sin(t), math.cos(t)]), np.array([math.cos(t), -math.sin(t)])
        c, ts = np.array([x, -50.0]), np.linspace(0, 1, 24)[:, None]
        side = c + v * ts * (dep + 50.0)
        g = g.Difference(hm._polygon(np.vstack([side - nrm * w * CELL / 2,
                                                (side + nrm * w * CELL / 2)[::-1]])))
    per = np.asarray(hm._rings(g.Simplify(CELL / 4.0))[0][1])
    per = hm.thin_ring(per if hm.ring_area(per) > 0 else per[::-1], 0.9 * CELL)
    return per, hm._grid_points(per, CELL, _srs())


@test
def v3268_mending_keeps_a_change_only_when_it_leaves_fewer_bad_cells():
    """Two notches with a thin spit between them, after the passes: a change is kept only
    when the whole 2D area has fewer cells cut in two or of more than 8 faces after it (a
    point moved that leaves as many is not kept; the first perimeter, thinned, crosses
    itself, as a 2D area as drawn can), and no point is added or moved closer than
    ADD_PERIMETER_CELLS cells to the perimeter, even where that would mend a cell."""
    if not T._has_gdal() or _no_scipy():
        return
    per, pts = _two_notches(103)
    out, done, s2, o2, split, over = _mended(per, pts, None, passes=hm.SPLIT_PASSES)
    assert (len(split), len(over)) == (5, 0), (split, over)
    assert done == {"split": 4, "back": 1, "added": 3}, done
    assert (len(s2), len(o2)) == (1, 0)
    per, pts = _two_notches(59)
    out, done, s2, o2, split, over = _mended(per, pts, None, passes=hm.SPLIT_PASSES)
    assert len(split) == 4 and done == {"split": 2, "added": 2} and len(s2) == 2, done


@test
def v3268_the_local_count_for_a_point_added_is_exact():
    """_add_gain counts what adding a point changes (its own cell and its Voronoi
    neighbours') from a diagram of the points around it only, taking more of them until those
    cells are exact: the same as a check of every cell, also beside a hole of removed points
    along a notch, where a diagram of the points within 4 cells alone makes the cells around
    the hole larger than they are and cuts them in two."""
    if _no_scipy():
        return
    per = np.array([(0, 0), (995, 0), (995, 600), (1005, 600), (1005, 0), (2000, 0),
                    (2000, 2000), (0, 2000)], float)
    pts = _grid()
    pts = pts[~((np.abs(pts[:, 0] - 1000.0) < 300.0) & (pts[:, 1] < 800.0))]
    split, over = hm._perimeter_cells(per, pts, CELL)[:2]
    bad = np.r_[split, over]
    poly = hm._polygon(per)
    rng = np.random.default_rng(1)
    qs = [q for q in rng.uniform((700.0, 100.0), (1300.0, 900.0), (30, 2))
          if poly.Contains(hm._point(*q))]
    full, local, close = [], [], []
    for q in qs:
        s2, o2 = hm._perimeter_cells(per, np.vstack([pts, q[None, :]]), CELL)[:2]
        full.append(len(s2) + len(o2) - len(bad))
        local.append(hm._add_gain(per, pts, CELL, q, bad))
        near = np.hypot(*(pts - q).T) <= 4.0 * CELL
        kept = np.cumsum(near) - 1
        close.append(hm._add_gain(per, pts[near], CELL, q, kept[bad[near[bad]]]))
    assert local == full, (local, full)
    assert any(f > 0 for f in full) and any(f == 0 for f in full), full
    assert sum(c_ != f for c_, f in zip(close, full)) >= 5, (close, full)
    # a wider hole, the diagram started from the points within one cell: each corner of
    # those cells is closer to the points taken than to any left out
    pts = _grid()
    pts = pts[~((np.abs(pts[:, 0] - 1000.0) < 500.0) & (pts[:, 1] < 1000.0))]
    split, over = hm._perimeter_cells(per, pts, CELL)[:2]
    bad = np.r_[split, over]
    rng = np.random.default_rng(2)
    for q in rng.uniform((500.0, 50.0), (1500.0, 1100.0), (40, 2)):
        if poly.Contains(hm._point(*q)):
            s2, o2 = hm._perimeter_cells(per, np.vstack([pts, q[None, :]]), CELL)[:2]
            assert hm._add_gain(per, pts, CELL, q, bad, start=CELL) == \
                len(s2) + len(o2) - len(bad), q


@test
def v3268_layout_measures_count_what_stops_hec_ras():
    """What the two 2D areas are compared on: cells cut in two or of more than 8 faces (as
    given), points on, outside or within NEAR_PERIMETER_CELLS of the perimeter, the boundary
    line crossing the perimeter, a perimeter that crosses itself, and the flooded ground left
    outside."""
    if _no_scipy():
        return
    per = np.array([(0, 0), (2000, 0), (2000, 2000), (0, 2000)], float)
    pts = _grid()
    bc = [(2025.0, 500.0), (2025.0, 1500.0)]                  # a quarter cell outside
    wet = hm._polygon([(0, 0), (2100, 0), (2100, 2000), (0, 2000)])
    A, B = (0.0, 2000.0), (0.0, 0.0)
    m = hm._layout_measures(per, pts, CELL, bc, A, B, wet, 7, np.array([3, 4]),
                            np.array([5]))
    assert m["removed"] == 7 and m["split_left"] == 2 and m["over8"] == 1
    assert m["valid"] and not m["bc_cross"] and m["near"] == 0
    assert abs(m["wet_out"] - 100.0 * 2000.0) < 1.0 and abs(m["wet"] - 2100.0 * 2000.0) < 1.0
    assert m["area"] == 4e6 and m["dam_len"] == 2000.0 and m["stop"] == 3
    # a point 2 ft from the edge, one 6 ft in (kept), one outside
    for extra, near in (([(1998.0, 1000.0)], 1), ([(1994.0, 1000.0)], 0),
                        ([(2010.0, 1000.0)], 1), ([(1998.0, 1000.0), (2010.0, 50.0)], 2)):
        m = hm._layout_measures(per, np.vstack([pts, extra]), CELL, bc, A, B, wet, 0, [], [])
        assert m["near"] == near and m["stop"] == near, (extra, m["near"])
    assert hm.NEAR_PERIMETER_CELLS == 0.05
    # the boundary line across the perimeter
    m = hm._layout_measures(per, pts, CELL, [(2025.0, 500.0), (1990.0, 1500.0)], A, B, wet,
                            0, [], [])
    assert m["bc_cross"] and m["stop"] == 1
    # a perimeter that crosses itself (a bow tie): counted, and measured made valid
    bow = np.array([(0, 0), (2000, 2000), (2000, 0), (0, 2000)], float)
    m = hm._layout_measures(bow, pts[:4], CELL, bc, A, B, wet, 0, [], [])
    assert not m["valid"] and m["stop"] >= 1 and not np.isnan(m["wet_out"])
    # nothing flooded known: not measured
    m = hm._layout_measures(per, pts, CELL, bc, A, B, None, 0, [], [])
    assert np.isnan(m["wet_out"]) and m["wet"] == 0.0


@test
def v3268_perimeter_distance_is_exact_or_a_lower_bound():
    if _no_scipy():
        return
    per = np.array([(0, 0), (2000, 0), (2000, 2000), (0, 2000)], float)
    rng = np.random.default_rng(7)
    xy = rng.uniform(-300.0, 2300.0, (2000, 2))
    exact = hm.point_segment_distance(xy[:, 0], xy[:, 1],
                                      list(map(tuple, per)) + [tuple(per[0])])
    for upto in (-1.0, 37.0, np.inf):
        d = hm._perimeter_distance(per, xy, CELL, upto=upto)
        assert (d <= exact + 1e-9).all()
        assert np.allclose(d[d <= upto], exact[d <= upto])
    assert np.allclose(hm._perimeter_distance(per, xy, CELL), exact)


def _meas(**k):
    m = {"removed": 10, "split_left": 0, "over8": 0, "valid": True, "bc_cross": False,
         "near": 0, "wet_out": 1000.0, "wet": 1e7, "A": np.array([0.0, 0.0]),
         "B": np.array([0.0, -500.0]), "dam_len": 500.0, "found": 5, "area": 1e7}
    m.update(k)
    m["stop"] = hm._stop(m)
    return m


@test
def v3268_the_better_2d_area_is_written():
    """_closed_better: fewer problems that stop HEC-RAS first, then less flooded ground left
    outside (within WET_OUT_TIE of the flooded area a tie), then the dam line (no end, nor its
    length, moving more than NOTCH_AB_TOL_CELLS cells), then fewer points removed; the one as
    drawn when none differs. Never one leaving more than WET_OUT_BOUND of the flooded area
    more outside, nor one with more problems than the 2D area as drawn had before."""
    p = _meas()
    use, why = hm._closed_better(_meas(), p, CELL)              # the same: not worth it
    assert not use and why == ["it is no better"]
    use, why = hm._closed_better(_meas(removed=0), p, CELL)
    assert use and why == ["it needs fewer points removed (0 against 10 as drawn)"]
    assert hm._closed_better(_meas(removed=11), p, CELL)[0] is False
    # each problem that stops HEC-RAS counts, and decides before anything else
    worse = {"split_left": 1, "over8": 1, "near": 1, "bc_cross": True, "valid": False}
    for k, v in worse.items():
        use, why = hm._closed_better(_meas(removed=0, wet_out=0.0, **{k: v}), p, CELL)
        assert not use and why[0].startswith("it has more problems that stop HEC-RAS (1 "
                                             "against 0 as drawn: "), (k, why)
        use, why = hm._closed_better(_meas(removed=50, wet_out=1500.0, A=np.array([0.0, 90.0])),
                                     _meas(**{k: v}), CELL)
        assert use and why[0].startswith("it has fewer problems that stop HEC-RAS (0 against "
                                         "1 as drawn: "), (k, why)
    assert hm._stop(_meas(split_left=2, over8=3, near=4, bc_cross=True, valid=False)) == 11
    # flooded ground left outside: within the tie, fewer points removed decide; beyond it,
    # less wins over fewer points removed
    tie = hm.WET_OUT_TIE * 1e7
    assert hm.WET_OUT_TIE == 0.0005 and hm.WET_OUT_BOUND == 0.005
    assert hm._closed_better(_meas(removed=0, wet_out=1000.0 + 0.9 * tie), p, CELL)[0]
    use, why = hm._closed_better(_meas(removed=0, wet_out=1000.0 + 1.1 * tie), p, CELL)
    assert not use and why[0].startswith("it leaves more flooded ground outside"), why
    use, why = hm._closed_better(_meas(removed=30, wet_out=1000.0 - 1.1 * tie), p, CELL)
    assert use and why[0].startswith("it leaves less flooded ground outside"), why
    # never more than WET_OUT_BOUND of the flooded area more outside, even with fewer problems
    bound = hm.WET_OUT_BOUND * 1e7
    pb = _meas(over8=1)
    assert hm._closed_better(_meas(wet_out=1000.0 + 0.9 * bound), pb, CELL)[0]
    use, why = hm._closed_better(_meas(wet_out=1000.0 + 1.1 * bound), pb, CELL)
    assert not use and "more than 0.5% of the flooded area more" in why[0], why
    # the dam line: either end or its length moving more than a quarter cell counts against it
    for kw in ({"A": np.array([0.0, 30.0])}, {"B": np.array([0.0, -460.0])},
               {"dam_len": 530.0}, {"A": np.array([26.0, 0.0]), "B": np.array([26.0, -500.0])}):
        use, why = hm._closed_better(_meas(removed=0, **kw), p, CELL)
        assert not use and why[0].startswith("the dam line would change by "), (kw, why)
    assert hm._closed_better(_meas(removed=0, A=np.array([0.0, 20.0]),
                                   B=np.array([0.0, -480.0]), dam_len=520.0), p, CELL)[0]
    assert hm.NOTCH_AB_TOL_CELLS == 0.25
    # never more problems than the 2D area as drawn had with its points as laid
    pa = _meas(split_left=2, base_stop=1)
    assert hm._closed_better(_meas(over8=1), pa, CELL)[0]
    use, why = hm._closed_better(_meas(over8=1, near=1), pa, CELL)
    assert not use and "more than the 2D area as drawn with its grid points as laid (1)" in \
        why[0], why
    # what could not be measured for the closed one counts against it, not for the other
    nan = float("nan")
    use, why = hm._closed_better(_meas(removed=0, wet_out=nan), p, CELL)
    assert not use and why == ["the flooded ground outside it could not be measured"]
    assert hm._closed_better(_meas(removed=0), _meas(wet_out=nan), CELL)[0]


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
    note that says why and how much larger the 2D area written is. With the closing doing
    nothing the second 2D area is no better: the one as drawn is written, its points
    removed."""
    if _skip_build():
        return
    d = tempfile.mkdtemp()
    close0, wet0, thin0 = hm.close_notches, hm._wet_ground, hm.thin_ring
    try:
        dfold, _dam, _dem, _hy = T.make_run(d)
        _carve(dfold, _inlet(84.0, 3000.0))
        widths, reach, untangle = [], [], []

        def spy(geom, width, **kw):
            widths.append(width)
            reach.append(kw.get("reach"))
            return close0(geom, width, **kw)

        def thin(ring, min_step, keep=(), **kw):
            untangle.append(kw.get("untangle", False))
            return thin0(ring, min_step, keep, **kw)
        wet = []

        def wet_spy(*a, **k):
            g = wet0(*a, **k)
            wet.append(g is not None and g.GetArea() > 0)
            return g
        hm.close_notches, hm._wet_ground, hm.thin_ring = spy, wet_spy, thin
        r = hm.build_model(dfold, os.path.join(d, "closed"), options=OPTS)
        hm.thin_ring = thin0
        m = r["model"]
        assert widths == [hm.NOTCH_CLOSE_CELLS * m.cell] == [120.0]
        # the one as drawn is thinned as before; the closed one so as not to cross itself
        assert untangle == [False, True]
        assert reach == [2.0 * m.cell]
        assert wet == [True, True]       # the flooded ground on the run's own cells, twice
        per, pts = np.asarray(m.perimeter), np.asarray(m.points)
        split, over = _own_cells(per, pts)
        assert split == [] and over == []
        note = [n for n in r["notes"] if "pre-check" in n]
        assert len(note) == 1, r["notes"]
        assert note[0].startswith("Perimeter pre-check: the 2D area as drawn had ") and \
            "its notches narrower than 120 ft there closed, before the dam line was cut, and " \
            "that one is written: it needs fewer points removed (0 against " in note[0] and \
            note[0].endswith("with the notches closed none is."), note[0]
        assert "takes in" not in note[0] and "dam line's ends" not in note[0]
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
        # and the note says by how much the 2D area written is larger than the one as drawn
        grow = abs(hm.ring_area(per)) - abs(hm.ring_area(per2))
        assert grow > 0 and "It is {:,.0f} ft2 ({:.2f}%) larger than as drawn.".format(
            grow, 100.0 * grow / abs(hm.ring_area(per2))) in note[0], (grow, note[0])
    finally:
        hm.close_notches, hm._wet_ground, hm.thin_ring = close0, wet0, thin0
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
        assert pc[0].endswith("closed there was not used: it has more problems that stop "
                              "HEC-RAS (1 against 0 as drawn: the boundary line crossing the "
                              "perimeter)."), pc[0]
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
def v3268_build_model_warns_of_cells_still_cut_in_two_or_over_8_faces():
    """When cells are still cut in two or have more than 8 faces, or a point is on the
    perimeter, after the passes and the mending, build_model still writes the model, with a
    WARNING naming them (HEC-RAS would stop on them). The 2D area with its notches closed is
    drawn only near the cells cut in two: those whose points were removed and those still
    cut. When the one as drawn is left with those and the second one (here: the same 2D area,
    its own passes) with none, the second one is written."""
    if _skip_build():
        return
    d = tempfile.mkdtemp()
    drop0, close0, mend0, base0 = hm._drop_split, hm.close_notches, hm._mend, hm._base_layouts
    cells0 = hm._perimeter_cells
    seen, near, checks = [], [], []

    def fake(per, points, cell, **kw):
        pts = np.asarray(points, float)
        seen.append(pts)
        if len(seen) > 1 and only_first:
            return drop0(per, points, cell, **kw)
        return np.vstack([pts[3:], np.asarray(per, float)[:1]]), 3, np.array([0, 1], np.int64), \
            np.array([2], np.int64)
    only_first = False

    def first_has_8(per, points, cell, **kw):
        # the first check of the 2D area as drawn also finds point 5 (point 2 once the
        # first 3 are removed) with more than 8 faces: that one was there before
        s_, o_, nb = cells0(per, points, cell, **kw)
        if not checks and not kw:
            o_ = np.union1d(o_, [5]).astype(np.int64)
        checks.append(len(points))
        return s_, o_, nb

    def no_close(geom, width, **kw):
        near.append((kw["near"], kw["reach"]))
        return geom
    try:
        dfold, _dem, _dam, _hy = T.make_run(d)
        _carve(dfold, _inlet(84.0, 3000.0))
        hm._drop_split, hm.close_notches, hm._perimeter_cells = fake, no_close, first_has_8
        hm._mend = lambda per, pts, cell, spare, split, over, tries=None: (
            pts, dict.fromkeys(("split", "over8", "back", "removed", "moved", "added"), 0),
            split, over)
        # the 2D area as drawn had as many problems before (4): it is not written instead
        hm._base_layouts = lambda *a, **k: [{"stop": 4}]
        r = hm.build_model(dfold, os.path.join(d, "model"), options=OPTS)
        m = r["model"]
        # as drawn, and closed: 3 points removed, 1 on the perimeter
        assert len(seen) == 2 and len(m.points) == len(seen[0]) - 2
        assert _g01_points(r["paths"]["g01"]) == len(m.points)
        # the closing: near the 3 points removed and the 2 cells still cut in two
        (xy, reach), = near
        assert reach == 2.0 * m.cell
        assert sorted(map(tuple, xy.tolist())) == sorted(
            map(tuple, np.vstack([seen[0][:3], seen[0][3:5]]).tolist()))
        warn = [n for n in r["notes"] if n.startswith("WARNING")]
        assert len(warn) == 3, r["notes"]
        assert warn[2] == "WARNING: 1 computation point(s) are on, outside or closer than " \
            "0.05 cell to the perimeter; HEC-RAS reports them - move or delete them in RAS " \
            "Mapper.", warn[2]
        assert "2 cell(s) along the perimeter are still cut in two" in warn[0]
        assert "face errors detected on the perimeter of the mesh" in warn[0]
        assert "({:.1f}, {:.1f})".format(*m.points[0]) in warn[0]
        assert "({:.1f}, {:.1f})".format(*m.points[1]) in warn[0]
        assert "({:.1f}, {:.1f})".format(*m.points[2]) not in warn[0]
        assert warn[1] == "WARNING: 1 cell(s) along the perimeter have more than 8 faces (1 of " \
            "them with the points as laid, before this pre-check), which HEC-RAS reports as " \
            "errors - move or delete those points in RAS Mapper: ({:.1f}, {:.1f}).".format(
                *m.points[2]), warn[1]
        assert np.array_equal(m.points[2], seen[0][5])
        assert any(n.startswith("Perimeter pre-check: 3 computation point(s)")
                   for n in r["notes"])
        assert not [n for n in r["notes"] if "mended" in n]
        text = _readme(r)
        assert warn[0] in text and warn[1] in text and warn[2] in text

        # the one as drawn left with those, the second one with its own passes: that one
        del seen[:], near[:]
        only_first = True
        r2 = hm.build_model(dfold, os.path.join(d, "model2"), options=OPTS)
        pc = [n for n in r2["notes"] if "pre-check" in n]
        assert re.search(r"that one is written: it has fewer problems that stop HEC-RAS \(0 "
                         r"against 4 as drawn: 2 cell\(s\) cut in two, 1 cell\(s\) of more than "
                         r"8 faces, 1 point\(s\) on, outside or closer than 0\.05 cell to the "
                         r"perimeter\)\..* As drawn, 3 point\(s\) had to be removed, leaving 2 "
                         r"cell\(s\) cut in two, .*; with the notches closed (\d+) point\(s\) are "
                         r"removed\.$", pc[0]), pc
        assert int(re.search(r"closed (\d+) point\(s\) are removed", pc[0]).group(1)) > 3
        assert not [n for n in r2["notes"] if n.startswith("WARNING")], r2["notes"]
    finally:
        hm._drop_split, hm.close_notches, hm._mend, hm._base_layouts = drop0, close0, mend0, base0
        hm._perimeter_cells = cells0
        shutil.rmtree(d, ignore_errors=True)


@test
def v3268_build_model_ranks_by_the_problems_that_stop_hec_ras_first():
    """The 84 m dry inlet. As drawn, after the passes, one cell is left with more than 8 faces
    (made so here): the 2D area with its notches closed has none, so it is written, and the
    README says why and what the one as drawn had left. With no problem left in either, less
    flooded ground left outside decides before the points removed, and the README says how
    much flooded ground the 2D area written takes in. What is compared is what each 2D area
    is written with: the points and cells after the passes and the mending."""
    if _skip_build():
        return
    d = tempfile.mkdtemp()
    mend0, meas0 = hm._mend, hm._layout_measures
    calls, made = [], []

    def mend(per, pts, cell, spare, split, over, tries=None):
        out = mend0(per, pts, cell, spare, split, over, tries)
        if not made:                                   # the 2D area as drawn
            out = out[:3] + (np.r_[out[3], [0]].astype(np.int64),)
        made.append(out)
        return out

    def meas(per, pts, cell, bc, A, B, wet, removed, split_left, over8, **kw):
        calls.append((np.asarray(pts), removed, np.asarray(split_left), np.asarray(over8)))
        return meas0(per, pts, cell, bc, A, B, wet, removed, split_left, over8, **kw)
    try:
        dfold, _dam, _dem, _hy = T.make_run(d)
        _carve(dfold, _inlet(84.0, 3000.0))
        hm._mend, hm._layout_measures = mend, meas
        r = hm.build_model(dfold, os.path.join(d, "a"), options=OPTS)
        assert len(calls) == len(made) == 2
        for (pts_, _, s_, o_), out in zip(calls, made):
            assert np.array_equal(pts_, out[0]) and np.array_equal(s_, out[2]) and \
                np.array_equal(o_, out[3])
        assert len(calls[0][3]) == 1 and calls[0][1] > 0 and calls[1][1] == 0
        m = r["model"]
        assert _own_cells(np.asarray(m.perimeter), np.asarray(m.points)) == ([], [])
        note = [n for n in r["notes"] if "pre-check" in n]
        assert len(note) == 1 and "that one is written: it has fewer problems that stop " \
            "HEC-RAS (0 against 1 as drawn: 1 cell(s) of more than 8 faces)." in note[0], note
        assert re.search(r"As drawn, \d+ point\(s\) had to be removed, leaving 1 cell\(s\) of "
                         r"more than 8 faces; with the notches closed none is\.$", note[0]), note
        assert not [n for n in r["notes"] if "WARNING" in n]

        # no problem left in either: the closed one leaves less flooded ground outside
        hm._mend = mend0
        del calls[:]
        less = []

        def meas2(*a, **kw):
            out = meas0(*a, **kw)
            if not less:                     # as drawn: more flooded ground left outside
                out["wet_out"] += 0.01 * out["wet"]
            less.append(out)
            return out
        hm._layout_measures = meas2
        r2 = hm.build_model(dfold, os.path.join(d, "b"), options=OPTS)
        p, c = less
        assert p["stop"] == c["stop"] == 0
        note = [n for n in r2["notes"] if "pre-check" in n]
        assert len(note) == 1 and "that one is written: it leaves less flooded ground outside " \
            "({:,.0f} ft2 against {:,.0f} ft2 as drawn). It is ".format(
                c["wet_out"], p["wet_out"]) in note[0], note
        assert "; it takes in {:,.0f} ft2 of flooded ground that the 2D area as drawn left " \
            "outside. As drawn".format(p["wet_out"] - c["wet_out"]) in note[0], note
        assert note[0] in _readme(r2)
    finally:
        hm._mend, hm._layout_measures = mend0, meas0
        shutil.rmtree(d, ignore_errors=True)


@test
def v3268_build_model_writes_the_one_as_drawn_when_the_closed_one_fails():
    """Building the 2D area with its notches closed raises: the one as drawn is written (the
    same export as when the closing does nothing), with a note, and no candidate terrain is
    left."""
    if _skip_build():
        return
    d = tempfile.mkdtemp()
    close0 = hm.close_notches
    try:
        dfold, _dam, _dem, _hy = T.make_run(d)
        _carve(dfold, _inlet(84.0, 3000.0))
        hm.close_notches = lambda geom, width, **kw: geom
        r0 = hm.build_model(dfold, os.path.join(d, "plain"), options=OPTS)

        def boom(geom, width, **kw):
            raise RuntimeError("boom")
        hm.close_notches = boom
        r = hm.build_model(dfold, os.path.join(d, "a"), options=OPTS)
        assert _same(r, r0)
        pc = [n for n in r["notes"] if "pre-check" in n]
        assert pc[0].endswith("closed there was not used: it could not be built "
                              "(RuntimeError: boom)."), pc
        _terrain_ok(r)
    finally:
        hm.close_notches = close0
        shutil.rmtree(d, ignore_errors=True)


def _split_once_2a(per, pts, cell, reach=3.0):
    """3.26.8's first check, as it was written: the points within `reach` cells of the
    perimeter whose Voronoi cell, clipped to it, is in two or more pieces."""
    from scipy.spatial import Voronoi
    per, pts = np.asarray(per, float), np.asarray(pts, float)
    poly = hm._polygon(per)
    lo, hi = per.min(axis=0) - 10 * cell, per.max(axis=0) + 10 * cell
    vor = Voronoi(np.vstack([pts, [(lo[0], lo[1]), (hi[0], lo[1]), (hi[0], hi[1]),
                                   (lo[0], hi[1])]]))
    d = hm.point_segment_distance(pts[:, 0], pts[:, 1], list(map(tuple, per)) + [tuple(per[0])])
    out = []
    for i in np.nonzero(d < reach * cell)[0]:
        reg = vor.regions[vor.point_region[i]]
        if not reg or -1 in reg:
            continue
        g = hm._polygon(vor.vertices[reg]).Intersection(poly)
        if g is not None and not g.IsEmpty() and len(hm._rings(g)) > 1:
            out.append(int(i))
    return np.asarray(out, np.int64)


@test
def v3268_never_more_problems_than_the_2d_area_as_drawn_had_before():
    """If the passes and the mending left more problems that stop HEC-RAS than the 2D area as
    drawn had with its grid points as laid (none removed, or those whose cell is cut in two
    removed once, as 3.26.8 first did), the better of those is written, with a note and a
    WARNING naming what is left (made so here: the mending reports 30 cells of more than 8
    faces in both 2D areas)."""
    if _skip_build():
        return
    per0, pts0 = _notched()
    assert np.array_equal(hm._split_once(per0, pts0, CELL), _split_once_2a(per0, pts0, CELL))
    for x0 in (1020.0, 1080.0):
        per, pts, _ = _slot(x0)
        one = hm._split_once(per, pts, CELL)
        assert len(one) and np.array_equal(one, _split_once_2a(per, pts, CELL))
        # with those gone, and the next ones, cells of points more than a cell from the slot
        # are cut in two: found as well (within 3 cells)
        left = np.delete(np.delete(pts, one, axis=0),
                         hm._split_once(per, np.delete(pts, one, axis=0), CELL), axis=0)
        three = hm._split_once(per, left, CELL)
        d = hm.point_segment_distance(left[three, 0], left[three, 1],
                                      list(map(tuple, per)) + [tuple(per[0])])
        assert np.array_equal(three, _split_once_2a(per, left, CELL)) and (d > CELL).any()
    d = tempfile.mkdtemp()
    mend0, drop0 = hm._mend, hm._drop_split
    laid = []

    def drop(per, pts, cell, **kw):
        laid.append(np.asarray(pts))
        return drop0(per, pts, cell, **kw)

    def spoiled(per, pts, cell, spare, split, over, tries=None):
        out = mend0(per, pts, cell, spare, split, over, tries)
        return out[:3] + (np.union1d(out[3], np.arange(30)).astype(np.int64),)
    try:
        dfold, _dam, _dem, _hy = T.make_run(d)
        _carve(dfold, _inlet(84.0, 3000.0))
        hm._mend, hm._drop_split = spoiled, drop
        r = hm.build_model(dfold, os.path.join(d, "a"), options=OPTS)
        m = r["model"]
        per, pts = np.asarray(m.perimeter), np.asarray(m.points)
        assert len(laid) == 1          # as drawn (closed, no cell is cut in two)
        once = _split_once_2a(per, laid[0], m.cell)
        assert len(once) > 0
        stops = [sum(map(len, _own_cells(per, q))) for q in (
            np.delete(laid[0], once, axis=0), laid[0])]
        assert stops[0] <= stops[1] < 30
        assert np.array_equal(pts, np.delete(laid[0], once, axis=0))
        split, over = _own_cells(per, pts)
        assert len(split) + len(over) == stops[0]
        pc = [n for n in r["notes"] if "pre-check" in n]
        assert pc[0].endswith("closed there was not used: it would have 30 problem(s) that "
                              "stop HEC-RAS (30 cell(s) of more than 8 faces), more than the "
                              "2D area as drawn with its grid points as laid ({}).".format(
                                  stops[0])), pc
        assert pc[1] == "Perimeter pre-check: with the points removed and mended the 2D area " \
            "would have 30 problem(s) that stop HEC-RAS (30 cell(s) of more than 8 faces), " \
            "more than with its grid points as laid and those whose cell the perimeter cuts in " \
            "two removed once ({}), so those are written.".format(stops[0] or "none"), pc
        assert pc[2].startswith("Perimeter pre-check: {} computation point(s) whose cell the "
                                "perimeter cut in two were removed".format(len(once))), pc
        warn = [n for n in r["notes"] if n.startswith("WARNING")]
        assert len(warn) == bool(split) + bool(over), (warn, split, over)
        if split:
            assert "{} cell(s) along the perimeter are still cut in two".format(len(split)) in \
                warn[0]
        assert not [n for n in r["notes"] if " mended (" in n]
        _terrain_ok(r)
    finally:
        hm._mend, hm._drop_split = mend0, drop0
        shutil.rmtree(d, ignore_errors=True)


@test
def v3268_closing_with_nothing_near_closes_nothing():
    if _no_scipy():
        return
    per, _, _ = _slot()
    g = hm._polygon(per)
    assert hm.close_notches(g, hm.NOTCH_CLOSE_CELLS * CELL, near=np.zeros((0, 2)),
                            reach=2.0 * CELL) is g
    assert hm.close_notches(g, hm.NOTCH_CLOSE_CELLS * CELL, near=[(1085.0, 300.0)],
                            reach=2.0 * CELL).Contains(hm._point(1085.0, 300.0))


@test
def v3268_the_closed_2d_area_is_drawn_so_as_not_to_cross_itself():
    """Thinning the perimeter to vertices 0.9 cell apart can make it cross itself where it
    hooks back on itself within a cell. Untangled, the vertices it skipped along the sides
    that cross are put back, there only."""
    ring = np.array([(700, 0), (300, 0), (0, 0), (-60, -160), (-330, -160), (-140, -120),
                     (-140, 20), (-1300, 60), (-1400, -2000), (800, -2000)], float)
    assert hm._polygon(ring).IsValid()
    t = hm.thin_ring(ring, 180.0)
    assert not hm._polygon(t).IsValid()
    assert hm._ring_crossings(t) == [2, 4]
    u = hm.thin_ring(ring, 180.0, untangle=True)
    assert hm._polygon(u).IsValid()
    assert u.tolist() == t[:3].tolist() + [[-60.0, -160.0]] + t[3:5].tolist() + \
        [[-140.0, 20.0]] + t[5:].tolist()
    assert np.array_equal(hm.thin_ring(ring, 180.0, untangle=False), t)
    sq = np.array([(0, 0), (2000, 0), (2000, 2000), (0, 2000)], float)
    assert hm._ring_crossings(sq) == []
    assert np.array_equal(hm.thin_ring(sq, 90.0, untangle=True), sq)


@test
def v3268_mesh_check_counts_the_cells_the_pre_check_named():
    """A cell grown by removed neighbours reaches the perimeter from more than 1.5 cells in:
    the pre-check counts its faces, and so does mesh_check when given the cells it named, so
    the README gives one count."""
    if _no_scipy():
        return
    per = _saw((700.0, 1300.0), step=12.5, amp=5.0)
    pts = _grid()
    pts = pts[~((pts[:, 1] < 200.0) & (pts[:, 0] > 700.0) & (pts[:, 0] < 1300.0))]
    split, over = hm._perimeter_cells(per, pts, CELL)[:2]
    d = hm.point_segment_distance(pts[:, 0], pts[:, 1], list(map(tuple, per)) + [tuple(per[0])])
    assert len(over) and (d[over] > 1.5 * CELL).any()
    assert sorted(over.tolist()) == sorted(_own_cells(per, pts)[1])
    plain = hm.mesh_check(per, pts, CELL)
    named = hm.mesh_check(per, pts, CELL, also=np.r_[split, over])
    assert plain["over_8_faces"] < len(over) == named["over_8_faces"]
    assert named["cells_checked"] > plain["cells_checked"]
    # a cell near the perimeter that does not reach it, with 10 faces, is counted too
    sq = np.array([(0, 0), (2000, 0), (2000, 2000), (0, 2000)], float)
    t = np.linspace(0.0, 2.0 * np.pi, 10, endpoint=False)
    ring10 = np.column_stack([1000.0 + 70.0 * np.cos(t), 1880.0 + 70.0 * np.sin(t)])
    g = _grid()
    g = np.vstack([g[np.hypot(g[:, 0] - 1000.0, g[:, 1] - 1880.0) > 150.0], ring10,
                   [(1000.0, 1880.0)]])
    split, over = hm._perimeter_cells(sq, g, CELL)[:2]
    k = len(g) - 1
    assert k in over.tolist() and hm.mesh_check(sq, g, CELL)["max_faces"] >= 10
    assert sorted(over.tolist()) == sorted(_own_cells(sq, g)[1])


def _side_notch(x, y_tip, sd, ang, w):
    """Dry in a strip `w` m wide from the point (x, sd * y_tip) (UTM 14N offsets from the dam,
    m) out through the north (sd = 1) or south (-1) side of the flood, turned `ang` degrees."""
    t = np.radians(ang)
    v = np.array([np.sin(t), sd * np.cos(t)])
    c = np.array([x, sd * y_tip])

    def dry(ux, uy):
        a = (ux - c[0]) * v[0] + (uy - c[1]) * v[1]
        b = -(ux - c[0]) * v[1] + (uy - c[1]) * v[0]
        return (a > 0) & (np.abs(b) < w / 2.0)
    return dry


@test
def v3268_build_model_mends_a_cell_by_adding_a_point():
    """Two dry inlets 0.7 cell wide beyond the side buffers, 300 ft cells, with the notches
    left open: the passes clear the cells cut in two but leave one cell of more than 8 faces
    that no point put back, removed or moved mends. A point added in it does; the README says
    so and the export has no cell cut in two or of more than 8 faces."""
    if _skip_build():
        return
    d = tempfile.mkdtemp()
    close0, drop0 = hm.close_notches, hm._drop_split
    laid = []

    def drop(per, pts, cell, **kw):
        laid.append(np.asarray(pts))
        return drop0(per, pts, cell, **kw)
    try:
        dfold, _dam, _dem, _hy = T.make_run(d)
        w = 2.0 * 100.0 * 0.3048 + 0.7 * 300.0 * 0.3048
        n1, n2 = _side_notch(2200.0, 40.0, 1, 0.0, w), _side_notch(3800.0, -30.0, -1, 30.0, w)
        _carve(dfold, lambda ux, uy: n1(ux, uy) | n2(ux, uy))
        hm.close_notches, hm._drop_split = (lambda geom, width, **kw: geom), drop
        r = hm.build_model(dfold, os.path.join(d, "a"),
                           options={"side_buffer_ft": 100.0, "cell_ft": 300.0})
        m = r["model"]
        per, pts = np.asarray(m.perimeter), np.asarray(m.points)
        assert _own_cells(per, pts) == ([], [])
        have = {tuple(q) for q in laid[0].tolist()}
        new = np.array([q for q in pts.tolist() if tuple(q) not in have])
        assert len(new) == 1, new
        ring = list(map(tuple, per)) + [tuple(per[0])]
        assert hm.point_segment_distance(new[:, 0], new[:, 1], ring)[0] >= 0.2 * m.cell
        mend = [n for n in r["notes"] if " mended (" in n]
        assert mend == ["Perimeter pre-check: 1 cell(s) along the perimeter with more than 8 "
                        "faces mended (0 removed point(s) put back, 0 point(s) removed, 0 "
                        "moved to the middle of its cell, 1 added)."], r["notes"]
        assert mend[0] in _readme(r)
        assert not [n for n in r["notes"] if "WARNING" in n]
        assert _g01_points(r["paths"]["g01"]) == len(pts)
    finally:
        hm.close_notches, hm._drop_split = close0, drop0
        shutil.rmtree(d, ignore_errors=True)

@test
def v3268_a_cut_culvert_without_a_channel_keeps_the_runs_line_and_is_whole_in_the_models():
    """Absaraka NBI 9117181: the gap line (120 m) went to HEC-RAS as a 394 ft barrel whose
    inlet sat on a field 10 ft above the channel, so no water went through it. The HEC-RAS
    barrel now runs from the channel on one side of the road to the other, but only where
    the channel was found on both sides: here the ground beside the road is only a step (no
    channel, 'ground'), so the export keeps the run's line as before, with a WARNING.
    LISFLOOD-FP, the built-in engine and ANUGA still get the whole gap line with the
    inventory's inverts (none), as in 3.26.7."""
    from nd_breach_runner.core import gaps as gp
    from nd_breach_runner.core.grid import Grid

    def z_at(Y):
        # a road along y = 0; the ground falls to the south (bed 98 m south, 100 m north)
        return np.where(np.abs(Y) < 8.0, 104.0, np.where(Y > 0, 100.0, 98.0))

    def read_dem(bounds, cell):
        x0, y0, x1, y1 = bounds
        nx, ny = max(4, int((x1 - x0) / cell)), max(4, int((y1 - y0) / cell))
        g = Grid(np.zeros((ny, nx)), x0, y1, cell)
        _X, Y = np.meshgrid(g.x_centers(), g.y_centers())
        return Grid(z_at(Y), x0, y1, cell)

    roads = [np.array([[-200.0, 0.0], [200.0, 0.0]])]
    rows = gp.blocking_rows([{"name": "C", "x": 0.0, "y": 1.0, "kind": "culvert",
                              "span_ft": 10.0, "rise_ft": 4.0, "barrels": 2}],
                            roads, read_dem, 3.0)
    b = rows[0]["barrel"]
    # no channel either side, only a step in the ground: 'ground', not 'channel'
    assert b["status"] == ["ground", "ground"] and b["roads"] == 1, b
    structs, _ = gp.structures_from(rows, 3.0)
    s = structs[0]
    # the models: the whole gap line, no inverts (3.26.7)
    assert np.allclose(s.line, rows[0]["line"]) and s.invert_up_m is None is s.invert_dn_m
    assert abs(np.hypot(*(np.asarray(s.line)[-1] - np.asarray(s.line)[0])) - 120.0) < 1e-6
    # HEC-RAS: no barrel (no channel was found), and why
    u = hm.Units(True)
    line, bar, notes = hm.crossing_barrel(s, b, lambda a: np.asarray(a, float).reshape(-1, 2),
                                          u, 10.0)
    assert line is None and bar is None, (line, bar)
    assert notes == ["no channel (a valley at least 1 ft deep in the toes of the embankment) "
                     "was found beside the road on each side within {:,.0f} m of the "
                     "inventory's point".format(max(b["search_m"]))], notes