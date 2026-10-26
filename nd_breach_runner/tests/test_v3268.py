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
