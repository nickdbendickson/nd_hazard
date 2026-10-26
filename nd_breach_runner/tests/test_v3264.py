"""3.26.4: Build a HEC-RAS 2D model, three fixes found by running the model in HEC-RAS 6.6.

(1) the normal-depth BC line lies just outside the 2D perimeter (HEC-RAS: 'BC Line intersects
the perimeter ... must be entirely outside'); (2) finer cells below the breach and the adaptive
(Courant) time step (with one cell size HEC-RAS hit its iteration limit at the breach every
step); (3) the dam weir and the breach bottom are not below the 2D cells next to them
(HEC-RAS: 'weir elevation lower than the cells they are connected to')."""
import shutil

import numpy as np

from nd_breach_runner.core import conditioning as cond
from nd_breach_runner.core import hecras_model as hm
from nd_breach_runner.tests.registry import test
from nd_breach_runner.tests.test_hecras_model import _build, _has_gdal


def _model():
    d, dfold, r = _build(run={"flood_crs": 5070})
    return d, r


@test
def v3264_bc_line_is_outside_the_2d_perimeter():
    if not _has_gdal():
        print("  (skipped: no GDAL)")
        return
    d, r = _model()
    try:
        m = r["model"]
        per = np.asarray(m.perimeter, float)
        bc = np.asarray(m.bc_line, float)
        assert len(bc) >= 2
        inside = cond.points_in_polygon(bc[:, 0], bc[:, 1], per)
        assert not np.asarray(inside).any()
        dist = hm.point_segment_distance(bc[:, 0], bc[:, 1],
                                         list(map(tuple, per)) + [tuple(per[0])])
        cell = r["info"]["cell"]
        assert np.allclose(dist, hm.BC_OFFSET_CELLS * cell, rtol=0.2, atol=0.05 * cell), dist
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
def v3264_offset_outward_follows_the_ring_orientation():
    sq = np.array([(0, 0), (10, 0), (10, 10), (0, 10)], float)
    ring = np.asarray(hm.densify(sq, 1.0, closed=True))
    for r_ in (ring, ring[::-1]):
        k = int(np.argmin(np.hypot(*(r_ - (5.0, 0.0)).T)))
        p = hm._offset_outward(r_, [k], 2.0)[0]
        assert np.allclose(p, (5.0, -2.0)), p


@test
def v3264_finer_cells_below_the_breach_and_adaptive_step():
    if not _has_gdal():
        print("  (skipped: no GDAL)")
        return
    d, r = _model()
    try:
        info, m = r["info"], r["model"]
        ref = info.get("refine") or []
        assert ref and ref[0]["cell"] < info["cell"], ref
        pts = np.asarray(m.points, float)
        c = np.asarray(m.breach_xy, float) if getattr(m, "breach_xy", None) is not None else None
        # nearest-neighbour spacing: the finest points are a refined cell apart
        sub = pts[np.linspace(0, len(pts) - 1, min(len(pts), 3000)).astype(int)]
        dd = np.hypot(sub[:, None, 0] - pts[None, :, 0], sub[:, None, 1] - pts[None, :, 1])
        dd[dd == 0] = np.inf
        nn = dd.min(axis=1)
        assert nn.min() <= ref[0]["cell"] * 1.01 and np.median(nn) >= 0.99 * info["cell"], (
            nn.min(), np.median(nn), c)
        p01 = open(r["paths"]["p01"], encoding="utf-8").read()
        assert "Computation Time Step Use Courant=        1" in p01
        assert "Computation Time Step Max Courant= 1" in p01
        readme = open(r["paths"]["readme"], encoding="utf-8").read()
        assert "Finer cells below the breach" in readme
        # the breach opening is cut into the terrain to the breach bottom, which stays put
        assert any(n.startswith("Terrain: the breach opening") for n in r["notes"])
        assert not any("breach bottom was raised" in n for n in r["notes"]), r["notes"]
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
def v3264_connection_cell_mins_raise_weir_and_breach():
    # a flat 2D area at 10 below a dam line along y = 0, with a mound at x in [40, 60]
    step, cell = 1.0, 10.0
    x0, ytop = -50.0, 0.0
    z = np.full((60, 200), 10.0)
    xs = x0 + (np.arange(200) + 0.5) * step
    z[:, (xs > 40) & (xs < 60)] = 15.0
    gx, gy = np.meshgrid(np.arange(-45.0, 150.0, cell), -np.arange(5.0, 55.0, cell))
    pts = np.column_stack([gx.ravel(), gy.ravel()])
    f = hm._connection_cell_mins(pts, (0.0, 0.0), (100.0, 0.0), (0.0, -1.0), z, x0, ytop,
                                 step, cell)
    line = np.column_stack([np.linspace(0, 100, 101), np.zeros(101)])
    c = f(line)
    assert np.nanmin(c) >= 10.0 - 1e-6
    assert c[50] >= 15.0 - 1e-6 and c[10] < 11.0
    assert abs(f.span_max(10.0, 30.0) - 10.0) < 1e-6
    assert f.span_max(30.0, 70.0) >= 15.0 - 1e-6
