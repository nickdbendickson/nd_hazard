"""3.26.8: Build a HEC-RAS 2D model drops computation points whose cell the perimeter cuts in
two. HEC-RAS 6.6 stopped the Absaraka (DWR-2801) culvert model with '1 face errors detected
on the perimeter of the mesh': a grid point beside a narrow notch of the perimeter had a
Voronoi cell that the perimeter split into two pieces."""
import numpy as np

from nd_breach_runner.core import hecras_model as hm
from nd_breach_runner.tests.registry import test


def _notched():
    """A 2,000 ft square with a 10 ft wide, 600 ft deep notch cut in from the south side,
    a 100 ft grid, and one extra point beside the notch whose cell straddles it (the notch
    runs right through that cell, so the perimeter cuts it in two)."""
    per = np.array([(0, 0), (995, 0), (995, 600), (1005, 600), (1005, 0), (2000, 0),
                    (2000, 2000), (0, 2000)], float)
    xs = np.arange(50, 2000, 100.0)
    gx, gy = np.meshgrid(xs, xs)
    pts = np.column_stack([gx.ravel(), gy.ravel()])
    return per, np.vstack([pts, [(1012.0, 300.0)]])


@test
def v3268_a_cell_the_perimeter_cuts_in_two_is_found():
    try:
        import scipy  # noqa: F401
    except ImportError:
        print("  (skipped: no scipy)")
        return
    per, pts = _notched()
    split = hm.perimeter_split_cells(per, pts, 100.0)
    assert split is not None and len(split) >= 1
    assert any(np.allclose(pts[i], (1012.0, 300.0)) for i in split)
    left = np.delete(pts, split, axis=0)
    assert len(hm.perimeter_split_cells(per, left, 100.0)) == 0


@test
def v3268_a_plain_grid_has_no_split_cells():
    try:
        import scipy  # noqa: F401
    except ImportError:
        print("  (skipped: no scipy)")
        return
    per = np.array([(0, 0), (2000, 0), (2000, 2000), (0, 2000)], float)
    xs = np.arange(50, 2000, 100.0)
    gx, gy = np.meshgrid(xs, xs)
    pts = np.column_stack([gx.ravel(), gy.ravel()])
    assert len(hm.perimeter_split_cells(per, pts, 100.0)) == 0


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
