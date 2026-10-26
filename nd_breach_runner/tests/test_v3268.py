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
def v3268_a_cut_culvert_runs_from_the_higher_bed_and_is_the_roads_width_long():
    """Absaraka NBI 9117181: the gap line (120 m) went to HEC-RAS as a 394 ft barrel whose
    inlet sat on a field 10 ft above the channel, so no water went through it."""
    from nd_breach_runner.core import gaps as gp
    from nd_breach_runner.core.grid import Grid

    def read_dem(bounds, cell):
        x0, y0, x1, y1 = bounds
        nx, ny = max(4, int((x1 - x0) / cell)), max(4, int((y1 - y0) / cell))
        g = Grid(np.zeros((ny, nx)), x0, y1, cell)
        _X, Y = np.meshgrid(g.x_centers(), g.y_centers())
        # a road along y = 0; the ground falls to the south (bed 98 m south, 100 m north)
        return Grid(np.where(np.abs(Y) < 8.0, 104.0, np.where(Y > 0, 100.0, 98.0)), x0, y1,
                    cell)

    roads = [np.array([[-200.0, 0.0], [200.0, 0.0]])]
    rows = gp.blocking_rows([{"name": "C", "x": 0.0, "y": 1.0, "kind": "culvert",
                              "span_ft": 10.0, "rise_ft": 4.0, "barrels": 2}],
                            roads, read_dem, 3.0)
    assert rows[0]["side_beds_ft"] is not None
    structs, _ = gp.structures_from(rows, 3.0)
    s = structs[0]
    line = np.asarray(s.line)
    assert line[0][1] > 0 > line[-1][1]                 # north (higher bed) end first
    assert abs(np.hypot(*(line[-1] - line[0])) - 2 * gp.CORRIDOR_M) < 1e-6
    assert abs(s.invert_up_m - 100.0) < 0.01 and abs(s.invert_dn_m - 98.0) < 0.01
