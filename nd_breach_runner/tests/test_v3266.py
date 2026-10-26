"""3.26.6: Build a HEC-RAS 2D model, three fixes found by running the model in HEC-RAS 6.6.

(1) the normal-depth BC line lies just outside the 2D perimeter (HEC-RAS: 'BC Line intersects
the perimeter ... must be entirely outside'); (2) finer cells below the breach and the adaptive
(Courant) time step (with one cell size HEC-RAS hit its iteration limit at the breach every
step); (3) the dam weir and the breach bottom are not below the 2D cells next to them
(HEC-RAS: 'weir elevation lower than the cells they are connected to'); (4) a dam point off
the embankment (Absaraka's is on the pool, 92 ft from the crest) moves to the crest found nearby
and the dam line runs along the embankment, not through the reservoir."""
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
def v3266_bc_line_is_outside_the_2d_perimeter():
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
def v3266_offset_outward_follows_the_ring_orientation():
    sq = np.array([(0, 0), (10, 0), (10, 10), (0, 10)], float)
    ring = np.asarray(hm.densify(sq, 1.0, closed=True))
    for r_ in (ring, ring[::-1]):
        k = int(np.argmin(np.hypot(*(r_ - (5.0, 0.0)).T)))
        p = hm._offset_outward(r_, [k], 2.0)[0]
        assert np.allclose(p, (5.0, -2.0)), p


@test
def v3266_finer_cells_below_the_breach_and_adaptive_step():
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
def v3266_connection_cell_mins_raise_weir_and_breach():
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


def _embankment(top=130.0):
    """A north-south embankment (crest x in [190, 230], top `top`, 3:1 sides) between a pool at
    105 (west) and a valley floor at 100 (east), on 5 ft cells; x0 = 0, ytop = 1000."""
    step = 5.0
    xs = (np.arange(200) + 0.5) * step
    z = np.where(xs < 210, 105.0, 100.0)
    z = np.maximum(z, top - np.maximum(np.abs(xs - 210.0) - 20.0, 0.0) / 3.0)
    return np.tile(z, (200, 1)), step


@test
def v3266_crest_snap_moves_an_off_crest_dam_point_onto_the_embankment():
    z, step = _embankment()
    r = hm._crest_snap(z, 0.0, 1000.0, step, (120.0, 500.0), 130.0, 3.0, 300.0, 150.0)
    assert r is not None
    q, t = r
    assert 180.0 <= q[0] <= 200.0 and abs(q[1] - 500.0) <= step, q
    assert abs(abs(t[1]) - 1.0) < 1e-6, t


@test
def v3266_crest_snap_leaves_a_crest_point_hills_and_far_ground_alone():
    z, step = _embankment()
    # on the crest already
    assert hm._crest_snap(z, 0.0, 1000.0, step, (210.0, 500.0), 130.0, 3.0, 300.0,
                          150.0) is None
    # the embankment is farther than the search distance
    assert hm._crest_snap(z, 0.0, 1000.0, step, (20.0, 500.0), 130.0, 3.0, 100.0,
                          150.0) is None
    # a round hill has no long axis: not an embankment
    xs = (np.arange(200) + 0.5) * step
    gx, gy = np.meshgrid(xs, 1000.0 - xs)
    hill = 130.0 - np.hypot(gx - 500.0, gy - 500.0) / 3.0
    assert hm._crest_snap(hill, 0.0, 1000.0, step, (500.0, 400.0), 130.0, 3.0, 300.0,
                          150.0) is None


@test
def v3266_dam_point_on_the_crest_is_not_moved():
    if not _has_gdal():
        print("  (skipped: no GDAL)")
        return
    d, r = _model()
    try:
        assert not any("from the embankment crest" in n for n in r["notes"]), r["notes"]
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
def v3266_reservoir_volumes_rise_as_written():
    """HEC-RAS stopped the smoke-test dam with 'Volume Elevation relationship does not have
    increasing volume': the pool bottom wrote 0.00, 0.00, 0.00 acre-ft."""
    rows = [(1825.0, 0.0), (1825.13, 0.001), (1825.36, 0.004), (1825.67, 0.01),
            (1826.03, 0.04), (1850.0, 500.0), (1850.0, 500.5), (1851.57, 600.09)]
    kept = hm.written_increasing(rows)
    assert kept[0] == (1825.0, 0.0)
    assert (1825.13, 0.001) not in kept and (1825.36, 0.004) not in kept
    assert (1825.67, 0.01) in kept and (1850.0, 500.5) not in kept
    w = [(float(hm.f8(e)), float(hm.f8(v))) for e, v in kept]
    assert all(b[0] > a[0] and b[1] > a[1] for a, b in zip(w, w[1:]))
    assert hm.written_increasing([]) == []
