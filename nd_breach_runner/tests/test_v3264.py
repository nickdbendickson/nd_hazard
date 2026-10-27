"""3.26.4: Build a HEC-RAS 2D model, five fixes found by running the model in HEC-RAS 6.6.

(1) the normal-depth BC line lies just outside the 2D perimeter (HEC-RAS: 'BC Line intersects
the perimeter ... must be entirely outside'); (2) finer cells below the breach and the adaptive
(Courant) time step (with one cell size HEC-RAS hit its iteration limit at the breach every
step); (3) the dam weir and the breach bottom are not below the 2D cells next to them
(HEC-RAS: 'weir elevation lower than the cells they are connected to'); (4) a dam point off
the embankment (Absaraka's is on the pool, 92 ft from the crest) moves to the crest found nearby
and the dam line runs along the embankment, not through the reservoir (also a pool point near
a crest, a crest the 10 m DEM reads low and a high pool); but a dam point on the crest (also one
the 10 m DEM reads low) or its side slope, an abutment hillside and a top of dam above the DEM
crest leave the line alone (the crest band, the on-crest rules, the hecras_crest._judge test
and the turn / beside / flooded-side guards), and a moved line the model cannot be built from
falls back to the dam point; (5) the reservoir's elevation-volume rows are written only while
they rise."""
import json
import math
import os
import shutil
import tempfile

import numpy as np

from nd_breach_runner.core import conditioning as cond
from nd_breach_runner.core import hecras, hecras_model as hm
from nd_breach_runner.core.units import FT
from nd_breach_runner.tests import synthetic as S
from nd_breach_runner.tests import test_hecras_model as T
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


def _embankment(top=130.0):
    """A north-south embankment (crest x in [190, 230], top `top`, 3:1 sides) between a pool at
    105 (west) and a valley floor at 100 (east), on 5 ft cells; x0 = 0, ytop = 1000."""
    step = 5.0
    xs = (np.arange(200) + 0.5) * step
    z = np.where(xs < 210, 105.0, 100.0)
    z = np.maximum(z, top - np.maximum(np.abs(xs - 210.0) - 20.0, 0.0) / 3.0)
    return np.tile(z, (200, 1)), step


# H = 30 ft, crest 40 ft wide: toe = 3 x 30 + 20 = 110 ft, prominence max(2 ft, 3 ft)
_SNAP = dict(top=130.0, tol=3.0, search=330.0, half=220.0, toe=110.0, prom=3.0)


def _snap(z, step, P, d=None, crest_w=40.0, **kw):
    a = dict(_SNAP, **kw)
    return hm._crest_snap(z, 0.0, 1000.0, step, P, a["top"], a["tol"], a["search"], a["half"],
                          a["toe"], a["prom"], d=d, crest_w=crest_w, u="ft")


@test
def v3264_crest_snap_moves_an_off_crest_dam_point_onto_the_embankment():
    z, step = _embankment()
    r = _snap(z, step, (120.0, 500.0))
    assert r is not None and r["why"] == "", r
    q, t = r["point"], r["axis"]
    # on the crest band's centre line (the crest x 190 to 230), across from the dam point
    assert 205.0 <= q[0] <= 215.0 and abs(q[1] - 500.0) <= step, q
    assert abs(abs(t[1]) - 1.0) < 1e-6, t
    assert 55.0 <= r["dist"] <= 70.0, r["dist"]          # to the band's near edge


@test
def v3264_crest_snap_from_the_pool_turns_up_to_65_degrees():
    # DWR-2801: the dam point on the pool 92 ft from the crest centre, the direction to the
    # run's inflow point 65 degrees off the embankment's normal: snapped, the normal downstream
    z, step = _embankment()
    a = math.radians(65.0)
    r = _snap(z, step, (118.0, 500.0), d=np.array([math.cos(a), math.sin(a)]))
    assert r is not None and r["why"] == "", r
    assert np.allclose(r["normal"], (1.0, 0.0)), r["normal"]
    assert abs(r["turn"] - 65.0) < 1.0, r["turn"]
    # the same crest seen from a flow direction along the embankment: rejected
    a = math.radians(85.0)
    r = _snap(z, step, (118.0, 500.0), d=np.array([math.cos(a), math.sin(a)]))
    assert r is not None and "degrees from the flow direction" in r["why"], r


@test
def v3264_crest_snap_leaves_a_crest_point_hills_and_far_ground_alone():
    z, step = _embankment()
    # on the crest already
    assert _snap(z, step, (210.0, 500.0)) is None
    # on the upstream side slope 12 ft off the crest's edge: the ground there is below the
    # crest band, but crest-band ground is within half the crest width and one cell of it
    # and it stands above the slope below it (round 3 of the review: rule (a2))
    assert _snap(z, step, (178.0, 500.0)) is None
    # the embankment is farther than the search distance
    assert _snap(z, step, (20.0, 500.0), search=100.0) is None
    # a round hill has no long axis: not an embankment
    xs = (np.arange(200) + 0.5) * step
    gx, gy = np.meshgrid(xs, 1000.0 - xs)
    hill = 130.0 - np.hypot(gx - 500.0, gy - 500.0) / 3.0
    r = _snap(hill, step, (500.0, 400.0))
    assert r is not None and "no clear long axis" in r["why"], r


@test
def v3264_crest_snap_rejects_an_abutment_hillside():
    # review of 3.26.4: a valley floor at 100 running east (the flow, d = +x) with a hillside
    # rising north of y = 600 at 15 %, through the top of dam and far above it. The ground
    # above the top is not crest; the contour band at the top runs along the valley and
    # stands above only its downhill side
    step = 5.0
    xs = (np.arange(200) + 0.5) * step
    gx, gy = np.meshgrid(xs, 1000.0 - xs)
    z = np.maximum(100.0, 100.0 + (gy - 600.0) * 0.15)
    r = _snap(z, step, (500.0, 500.0), d=np.array([1.0, 0.0]))
    assert r is not None and r["why"], r
    assert "degrees from the flow direction" in r["why"], r["why"]
    # (round 2 of the review: the highest ground across the band is taken on the cross-section
    # through it, as hecras_crest._judge does, so the hillside is an abutment, not a crest
    # that 'does not stand above both sides')
    assert "abutment: the ridge across it rises above the top of dam" in r["why"], r["why"]
    # without a flow direction the hillside still fails the embankment test
    r = _snap(z, step, (500.0, 500.0))
    assert r is not None and "abutment: the ridge" in r["why"], r
    # a hillside that levels off at the top of dam (a terrace): the ridge is in the crest band
    # but does not stand above the ground on the uphill side
    t = np.minimum(z, 131.0)
    r = _snap(t, step, (500.0, 500.0), d=np.array([0.0, 1.0]))
    assert r is not None and "does not stand" in r["why"], r
    assert "abutment:" not in r["why"] and "degrees from the flow" not in r["why"], r["why"]
    # a dam point beside the end of an embankment (on the valley floor past its end) is not
    # upstream or downstream of it
    e, step = _embankment()
    e[:60, :] = np.where(np.arange(200)[None, :] * step < 210, 105.0, 100.0)  # ends at y = 700
    r = _snap(e, step, (215.0, 780.0), d=np.array([1.0, 0.0]))
    assert r is not None and "beside" in r["why"], r


@test
def v3264_crest_band_is_not_ground_above_the_top_of_dam():
    # the crest band is |z - top| <= tol: ground well above the top of dam is not crest. A
    # cliff up to a bench 20 ft above the top of dam has no crest-band ground at all
    step = 5.0
    xs = (np.arange(200) + 0.5) * step
    z = np.tile(np.where(xs < 300.0, 100.0, 150.0), (200, 1))
    assert _snap(z, step, (200.0, 500.0), d=np.array([1.0, 0.0])) is None
    # an embankment whose DEM crest is 10 ft above the top of dam: its sides cross the band,
    # but the ridge across them rises above it: an abutment, not the crest
    z, step = _embankment(top=140.0)
    r = _snap(z, step, (120.0, 500.0), d=np.array([1.0, 0.0]))
    assert r is not None and "abutment: the ridge across it rises above the top of dam" in \
        r["why"], r
    assert abs(r["z_crest"] - 140.0) < 0.5, r


def _grid_dam(step, x_c, crest_w, top, toe_z, pool_z, hill_half=None):
    """A north-south embankment on `step` cells, its crest (crest_w wide, at `top`, 3:1
    sides) centred at x = x_c, the pool at pool_z west of x_c - 98 ft, the valley floor at
    toe_z; with hill_half, abutments rising at 15 % from top - 6.6 ft beyond |y - 500| >
    hill_half. x0 = 0, ytop = 1000."""
    n = int(1000.0 / step)
    xs = (np.arange(n) + 0.5) * step
    gx, gy = np.meshgrid(xs, 1000.0 - xs)
    z = np.maximum(toe_z, top - np.maximum(np.abs(gx - x_c) - 0.5 * crest_w, 0.0) / 3.0)
    z = np.where(gx < x_c - 98.0, np.maximum(z, pool_z), z)
    if hill_half is not None:
        hill = top - 6.6 + (np.abs(gy - 500.0) - hill_half) * 0.15
        z = np.where(np.abs(gy - 500.0) > hill_half, np.maximum(z, hill), z)
    return z


@test
def v3264_crest_snap_on_a_10_m_dem():
    # round 2 of the review, on 10 m (32.8 ft) cells. (M1) DWR-2801: the dam point on the pool
    # 92 ft upstream of the centre of a 20 ft crest, the pool 8 ft below the top, the inflow
    # direction 65 degrees off. The first fix's on-crest window (half the crest and two cells,
    # 75.6 ft) reached the crest band's upstream edge and the point was 'on the crest': no
    # snap. Half the crest and one DEM cell (42.8 ft) does not reach it
    step = 10.0 / 0.3048
    x_c = 15.5 * step + 17.4                   # a cell centre 17.4 ft upstream of the centre
    z = _grid_dam(step, x_c, 20.0, 130.0, 100.0, 122.0)
    y_p = 1000.0 - 15.5 * step
    a = math.radians(65.0)
    kw = dict(top=130.0, tol=3.0, search=300.0, half=200.0, toe=100.0, prom=3.0)
    r = _snap(z, step, (x_c - 92.0, y_p), d=np.array([math.cos(a), math.sin(a)]),
              crest_w=20.0, **kw)
    assert r is not None and r["why"] == "", r
    assert abs(r["point"][0] - x_c) <= 0.5 * step + 1.0, (r["point"], x_c)
    assert abs(r["axis"][0]) < 0.05 and abs(r["turn"] - 65.0) < 2.0, r
    assert abs(r["r_near"] - (10.0 + step)) < 1e-6, r["r_near"]
    # (H1) a 12 ft dam with an 8 ft crest, the dam point on its centre line, between two DEM
    # cells: the DEM reads the crest 4.1 ft low, more than the 3 ft tolerance, but the dam
    # point is on a ridge standing above both sides along the flow direction, within
    # (32.8 - 8) / (2 x 3) ft of the tolerance: on the crest, never moved (the first fix moved
    # it onto the abutment hillside, along the valley)
    x_c = 16.0 * step
    z = _grid_dam(step, x_c, 8.0, 130.0, 118.0, 126.7, hill_half=197.0)
    kw = dict(top=130.0, tol=3.0, search=300.0, half=150.0, toe=40.0, prom=2.0)
    a = math.radians(-30.0)
    d30 = np.array([math.cos(a), math.sin(a)])
    P = (x_c, 1000.0 - 15.5 * step)
    rows = np.abs(500.0 - (np.arange(z.shape[0]) + 0.5) * step) < 100.0
    assert 130.0 - float(z[rows][:, 15:17].max()) > 4.0     # the DEM's highest crest cells
    assert _snap(z, step, P, d=d30, crest_w=8.0, **kw) is None
    # the same crest with the top of dam 6 ft above it is not read low by the DEM alone: the
    # snap is considered, and the abutment hillside rejected
    r = _snap(z, step, P, d=d30, crest_w=8.0, **dict(kw, top=136.0))
    assert r is not None and "abutment" in r["why"], r


@test
def v3264_rings_skip_an_empty_polygon():
    # round 2 of the review (H1b): a snap onto an abutment hillside left a reservoir one cell
    # wide; Simplify made it an empty polygon and _rings read its missing exterior ring
    # (AttributeError 'NoneType' object has no attribute 'GetPoints'). The empty polygon has
    # no rings, and the reservoir keeps the unsimplified outline
    if not _has_gdal():
        print("  (skipped: no GDAL)")
        return
    from osgeo import ogr
    assert hm._rings(ogr.CreateGeometryFromWkt("POLYGON EMPTY")) == []
    mask = np.zeros((5, 60), bool)
    mask[2, 5:53] = True
    rp = hm._poly_from_mask(mask, 0.0, 0.0, 30.0, hm.srs_from("EPSG:5070"))
    g = rp.Simplify(30.0)
    assert g.IsEmpty() and hm._rings(g) == []
    assert len(hm._rings(rp)) == 1


@test
def v3264_dam_point_on_the_crest_is_not_moved():
    if not _has_gdal():
        print("  (skipped: no GDAL)")
        return
    d, r = _model()
    try:
        assert not any("from the embankment crest" in n for n in r["notes"]), r["notes"]
    finally:
        shutil.rmtree(d, ignore_errors=True)


# --- the crest snap on the synthetic 10 m DEM, between abutments -----------------------------
def _short_valley(half_m, crest_m=None, slope=0.15):
    """synthetic.valley for a short dam: abutments rising at `slope` from 2 m below the top of
    dam beyond |y| > half_m (so the hillside is inside the crest snap's search radius). With
    `crest_m`, the embankment has a flat crest that wide, at the top of dam +/- 1 ft (the 10 m
    DEM reads it), and the pool is 8 ft below the top of dam."""
    def fn(x, y):
        if crest_m is None:
            z = S.valley(x, y)
        else:
            bed = S.TOP_M - S.H_M - 0.001 * np.clip(x, 0, None)
            z = bed + 0.004 * np.abs(y) - np.where(np.abs(y) < 12, 1.0 - np.abs(y) / 12, 0.0)
            z = z + 0.3 * np.sin(x / 700.0) * np.cos(y / 500.0)
            res = np.where(x < -40, np.maximum(z, S.TOP_M - 8.0 * FT), z)
            cw = 0.5 * crest_m
            crest = S.TOP_M + 0.3 * np.sin(y / 50.0)
            emb = crest - np.maximum(np.abs(x) - cw, 0.0) / 40.0 * (S.TOP_M - bed)
            on = (np.abs(x) <= 40 + cw) & (np.abs(y) < 400)
            z = np.where(on, np.maximum(res, emb), res)
        hill = S.TOP_M - 2.0 + (np.abs(y) - half_m) * slope
        return np.where(np.abs(y) > half_m, np.maximum(z, hill), z)
    return fn


def _snap_model(zfn, dam_kw, dam_xy=None, inflow_xy=None):
    """build_model on make_run's run with the terrain `zfn`; dam_xy / inflow_xy (metres from
    the crest centre, UTM) move the dam point and the run's inflow point. Returns (tmp folder,
    result, dam point, crest centre, unit valley axis, unit direction to the inflow point),
    the points in the model CRS."""
    d = tempfile.mkdtemp()
    dfold, dem, dam, hy = T.make_run(d, flood_crs=5070, zfn=zfn, dam_kw=dam_kw)
    rep_path = os.path.join(dfold, hecras.MODEL_FOLDER, hecras.RUN_REPORT_JSON)
    with open(rep_path) as f:
        rep = json.load(f)
    P0 = [T.X0 + (dam_xy or (0.0, 0.0))[0], T.Y0 + (dam_xy or (0.0, 0.0))[1]]
    I0 = [T.X0 + (inflow_xy or (120.0, 0.0))[0], T.Y0 + (inflow_xy or (120.0, 0.0))[1]]
    rep["dam_xy"], rep["inflow_point"]["xy"] = P0, I0
    with open(rep_path, "w") as f:
        json.dump(rep, f)
    r = hm.build_model(dfold)
    f = hm.transformer(hm.srs_from("EPSG:26914"), hm.srs_from(r["info"]["crs"]))
    P, I, C, O, E = f([P0, I0, [T.X0, P0[1]], [T.X0, T.Y0], [T.X0 + 1000.0, T.Y0]])
    unit = lambda v: v / np.hypot(*v)
    return d, r, P, C, unit(E - O), unit(I - P)


def _dam_line(m):
    A, B = np.array(m.conn_line[0]), np.array(m.conn_line[-1])
    return A, B, (B - A) / np.hypot(*(B - A))


@test
def v3264_on_crest_dam_between_abutments_keeps_its_line():
    # review of 3.26.4: the PR's own synthetic dam (point on the crest, top of dam = the DEM
    # crest) with abutments 200 ft from the dam point. The 10 m DEM reads the pointed crest
    # 3.1 ft low, more than the 3 ft tolerance, and the abutment ground above the top of dam
    # passed as 'crest-high ground with a long axis': the dam line went 230 ft up the hillside
    # and along the valley. It stays through the dam point, across the valley.
    if not _has_gdal():
        print("  (skipped: no GDAL)")
        return
    d, r, P, C, valley_axis, flow = _snap_model(_short_valley(60.0),
                                                {"crest_length_ft": 400.0})
    try:
        assert not any("from the embankment crest" in n for n in r["notes"]), r["notes"]
        # round 2 of the review: the dam point is on a crest the DEM reads low, so no snap
        # was considered and the README has no 'considered and rejected' note about it
        assert not any("rejected" in n for n in r["notes"]), r["notes"]
        T._check_model(r["model"], P, flow)
        A, B, u = _dam_line(r["model"])
        assert abs(float(np.dot(u, valley_axis))) < 0.02, u
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
def v3264_top_of_dam_above_the_dem_crest_does_not_turn_the_line():
    # dam.json top of dam 4 ft above the DEM crest (the Runner accepts it with a warning): the
    # crest band is the abutment hillside's contour, along the valley; the snap is rejected
    # and said so in the README
    if not _has_gdal():
        print("  (skipped: no GDAL)")
        return
    d, r, P, C, valley_axis, flow = _snap_model(
        _short_valley(60.0), {"crest_length_ft": 400.0, "top_of_dam_ft": 1854.0})
    try:
        assert not any("from the embankment crest" in n for n in r["notes"]), r["notes"]
        rej = [n for n in r["notes"] if "rejected" in n and "crest" in n]
        assert rej, r["notes"]
        T._check_model(r["model"], P, flow)
        A, B, u = _dam_line(r["model"])
        assert abs(float(np.dot(u, valley_axis))) < 0.02, u
        with open(r["paths"]["readme"], encoding="utf-8") as f:
            assert rej[0] in f.read()
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
def v3264_dam_point_on_the_pool_snaps_to_the_crest_across_the_valley():
    # the case 3.26.4 is for (DWR-2801): the dam point on the pool upstream of the crest and
    # the direction to the run's inflow point 65 degrees off the embankment's normal. The
    # crest (40 ft wide, at the top of dam +/- 1 ft) is found, between abutments, and the dam
    # line goes along it, across the valley
    if not _has_gdal():
        print("  (skipped: no GDAL)")
        return
    ang = math.radians(65.0)
    P_off = (-46.0, -190.0)                        # 151 ft upstream of the crest centre
    L = 90.0 / math.cos(ang)
    I_off = (P_off[0] + L * math.cos(ang), P_off[1] + L * math.sin(ang))
    d, r, P, C, valley_axis, flow = _snap_model(
        _short_valley(300.0, crest_m=12.0),
        {"crest_length_ft": 2000.0, "crest_width_ft": 40.0}, dam_xy=P_off, inflow_xy=I_off)
    try:
        snap = [n for n in r["notes"] if "from the embankment crest" in n]
        assert snap, r["notes"]
        assert not any("rejected" in n for n in r["notes"]), r["notes"]
        A, B, u = _dam_line(r["model"])
        # across the valley (not across the 65-degree direction to the inflow point) ...
        assert abs(float(np.dot(u, valley_axis))) < math.sin(math.radians(3.0)), u
        assert abs(float(np.dot(u, flow))) > 0.8, (u, flow)
        # ... through the crest, not the dam point on the pool
        off = lambda q: abs(float(np.cross(B - A, q - A))) / np.hypot(*(B - A))
        assert off(C) < 15.0, off(C)
        assert off(P) > 120.0, off(P)
    finally:
        shutil.rmtree(d, ignore_errors=True)


# --- round 2 of the review: low dams, a steep abutment, the pool point on a 10 m DEM ---------
def _low_dam(half_m, slope, crest_ft, h_ft=12.0):
    """A low dam on make_run's 10 m DEM: a flat crest crest_ft wide h_ft above the synthetic
    valley floor (top of dam 1825 + h_ft ft), 3:1 sides, the pool 1 m below the crest beyond
    30 m upstream, abutments rising at `slope` from 2 m below the crest beyond |y| > half_m."""
    def fn(x, y):
        bed0 = S.TOP_M - S.H_M
        bed = bed0 - 0.001 * np.clip(x, 0, None)
        z = bed + 0.004 * np.abs(y) - np.where(np.abs(y) < 12, 1.0 - np.abs(y) / 12, 0.0)
        z = z + 0.3 * np.sin(x / 700.0) * np.cos(y / 500.0)
        top = bed0 + h_ft * FT
        res = np.where(x < -30, np.maximum(z, top - 1.0), z)
        cw = 0.5 * crest_ft * FT
        emb = top - np.maximum(np.abs(x) - cw, 0.0) / 3.0
        on = (np.abs(x) <= cw + 3 * h_ft * FT) & (np.abs(y) < 400)
        z = np.where(on, np.maximum(res, emb), res)
        hill = top - 2.0 + (np.abs(y) - half_m) * slope
        return np.where(np.abs(y) > half_m, np.maximum(z, hill), z)
    return fn


def _low_dam_model(top_above_ft, half_m, slope, crest_ft, skew_deg=-30.0, dam_y_m=10.0):
    """_snap_model on _low_dam (12 ft): the dam point on the crest centre line at y = dam_y_m,
    the run's inflow point 120 m away at skew_deg off the valley axis, the top of dam
    top_above_ft above the DEM crest."""
    a = math.radians(skew_deg)
    kw = {"crest_length_ft": 2 * half_m / FT, "dam_height_ft": 12.0, "crest_width_ft": crest_ft,
          "top_of_dam_ft": (S.TOP_M - S.H_M) / FT + 12.0 + top_above_ft}
    return _snap_model(_low_dam(half_m, slope, crest_ft), kw, dam_xy=(0.0, dam_y_m),
                       inflow_xy=(120.0 * math.cos(a), dam_y_m + 120.0 * math.sin(a)))


@test
def v3264_low_on_crest_dam_between_abutments_keeps_its_line():
    # round 2 of the review (H1): a 12 ft dam with an 8 ft crest, the dam point on it, top of
    # dam = the DEM crest, abutments 50 m from the dam point and the inflow direction 30
    # degrees off the valley. The 10 m DEM reads the crest 4.1 ft low (more than the 3 ft
    # tolerance) and the first fix moved the dam line 201 ft onto the abutment hillside, along
    # the valley (23,125 ft long). The dam point is on a crest the DEM reads low: the line
    # stays through it, across the flow direction (60 degrees to the valley), and no snap was
    # considered, so there is no note
    if not _has_gdal():
        print("  (skipped: no GDAL)")
        return
    d, r, P, C, valley_axis, flow = _low_dam_model(0.0, 60.0, 0.15, 8.0)
    try:
        assert not any("embankment crest" in n or "rejected" in n for n in r["notes"]), \
            r["notes"]
        T._check_model(r["model"], P, flow)
        A, B, u = _dam_line(r["model"])
        assert abs(abs(float(np.dot(u, valley_axis))) - math.sin(math.radians(30.0))) < 0.05, u
        assert np.hypot(*(B - A)) < 5000.0, np.hypot(*(B - A))
    finally:
        shutil.rmtree(d, ignore_errors=True)
    # the top of dam 4 ft above that crest: the DEM does not show it; the abutment hillside is
    # considered and rejected (its ridge rises above the top of dam), the line stays
    d, r, P, C, valley_axis, flow = _low_dam_model(4.0, 60.0, 0.15, 8.0)
    try:
        assert not any("from the embankment crest" in n for n in r["notes"]), r["notes"]
        rej = [n for n in r["notes"] if "rejected" in n]
        assert rej and "abutment: the ridge across it rises above the top of dam" in rej[0], \
            r["notes"]
        T._check_model(r["model"], P, flow)
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
def v3264_steep_abutment_builds_with_the_line_through_the_dam_point():
    # round 2 of the review (H1b): a 25 % abutment, 12 ft crest, top of dam 2 ft above the DEM
    # crest. The first fix snapped the dam line onto the hillside (along the valley) and
    # build_model crashed in _rings ('NoneType' object has no attribute 'GetPoints': the
    # reservoir behind that line was one cell wide and simplified to an empty polygon). The
    # dam point is on the crest: the model builds with the line through it
    if not _has_gdal():
        print("  (skipped: no GDAL)")
        return
    d, r, P, C, valley_axis, flow = _low_dam_model(2.0, 60.0, 0.25, 12.0)
    try:
        assert not any("embankment crest" in n or "rejected" in n for n in r["notes"]), \
            r["notes"]
        T._check_model(r["model"], P, flow)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _pool_dam(crest_ft, pool_ft, half_m):
    """A 25 ft dam on make_run's 10 m DEM: a flat crest crest_ft wide at the top of dam
    (+/- 0.1 m), 3:1 sides, the pool pool_ft below the top upstream of the crest centre line,
    abutments rising at 15 % from 2 m below the top beyond |y| > half_m."""
    def fn(x, y):
        bed0 = S.TOP_M - S.H_M
        bed = bed0 - 0.001 * np.clip(x, 0, None)
        z = bed + 0.004 * np.abs(y) - np.where(np.abs(y) < 12, 1.0 - np.abs(y) / 12, 0.0)
        z = z + 0.3 * np.sin(x / 700.0) * np.cos(y / 500.0)
        res = np.where(x < 0, np.maximum(z, S.TOP_M - pool_ft * FT), z)
        cw = 0.5 * crest_ft * FT
        emb = S.TOP_M + 0.09 * np.sin(y / 50.0) - np.maximum(np.abs(x) - cw, 0.0) / 3.0
        on = (np.abs(x) <= cw + 3 * S.H_M) & (np.abs(y) < 2000)
        z = np.where(on, np.maximum(res, emb), res)
        hill = S.TOP_M - 2.0 + (np.abs(y) - half_m) * 0.15
        return np.where(np.abs(y) > half_m, np.maximum(z, hill), z)
    return fn


@test
def v3264_pool_point_92_ft_upstream_on_a_10_m_dem_snaps():
    # round 2 of the review (M1): DWR-2801's case on the 10 m DEM: the dam point on the pool
    # 92 ft upstream of the centre of a 20 ft crest, the pool 8 ft below the top, the inflow
    # direction 65 degrees off. The first fix's on-crest window (75.6 ft) reached the crest
    # band and left the dam line through the pool (5,038 ft, 25 degrees to the valley, no
    # note). The window is half the crest and one DEM cell (42.8 ft): the line goes along the
    # crest, across the valley
    if not _has_gdal():
        print("  (skipped: no GDAL)")
        return
    ang = math.radians(65.0)
    P_off = (-92.0 * FT, -100.0)
    L = 90.0 / math.cos(ang)
    I_off = (P_off[0] + L * math.cos(ang), P_off[1] + L * math.sin(ang))
    d, r, P, C, valley_axis, flow = _snap_model(
        _pool_dam(20.0, 8.0, 300.0),
        {"crest_length_ft": 600.0 / FT, "dam_height_ft": 25.0, "crest_width_ft": 20.0},
        dam_xy=P_off, inflow_xy=I_off)
    try:
        snap = [n for n in r["notes"] if "from the embankment crest" in n]
        assert snap, r["notes"]
        assert not any("rejected" in n for n in r["notes"]), r["notes"]
        A, B, u = _dam_line(r["model"])
        assert abs(float(np.dot(u, valley_axis))) < math.sin(math.radians(3.0)), u
        off = lambda q: abs(float(np.cross(B - A, q - A))) / np.hypot(*(B - A))
        assert off(C) < 20.0, off(C)
        assert off(P) > 60.0, off(P)
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
def v3264_snap_with_the_flood_on_the_other_side_is_rejected():
    # build_model's second check: the crest found must have the flood on the side the flow
    # goes to. A dam point 30 m below the crest centre (on the toe) with the run's inflow
    # point between it and the crest: the flow direction points back upstream, the crest's
    # flooded side is downstream, 180 degrees from it. Rejected with a note; the line stays
    # through the dam point and the model builds
    if not _has_gdal():
        print("  (skipped: no GDAL)")
        return
    d, r, P, C, valley_axis, flow = _snap_model(
        _short_valley(300.0, crest_m=12.0), {"crest_length_ft": 2000.0, "crest_width_ft": 40.0},
        dam_xy=(30.0, -100.0), inflow_xy=(10.0, -100.0))
    try:
        assert not any("from the embankment crest" in n for n in r["notes"]), r["notes"]
        rej = [n for n in r["notes"] if "rejected" in n]
        assert rej and "the flooded side of it is not the side the flow goes to" in rej[0], \
            r["notes"]
        A, B, u = _dam_line(r["model"])
        assert abs(float(np.cross(B - A, P - A))) / np.hypot(*(B - A)) < 1.0
        assert abs(float(np.dot(u, flow))) < 1e-6
        with open(r["paths"]["readme"], encoding="utf-8") as f:
            assert rej[0] in f.read()
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
def v3264_a_snap_that_cannot_be_built_falls_back_to_the_dam_point():
    # round 2 of the review (H1b): a snapped dam line the 2D area cannot be built from (here a
    # 'crest' beyond the end of the flooded area and off to its side: the dam line does not
    # cross the flood) never makes the export fail: the model is built again with the line
    # through the dam point, and the README says so
    if not _has_gdal():
        print("  (skipped: no GDAL)")
        return
    orig = hm._crest_snap

    def far(z, x0, y0, step, P, top, tol, search, half, toe, prom, d=None, **k):
        d = np.asarray(d, float)
        n = np.array([-d[1], d[0]])
        return {"point": np.asarray(P, float) + 40000.0 * d + 20000.0 * n, "axis": n,
                "normal": d, "dist": 44721.0, "z_near": top - 10.0, "r_near": 40.0,
                "z_crest": top, "turn": 0.0, "why": ""}
    hm._crest_snap = far
    try:
        d, r, P, C, valley_axis, flow = _snap_model(_short_valley(300.0, crest_m=12.0),
                                                    {"crest_length_ft": 2000.0})
    finally:
        hm._crest_snap = orig
    try:
        fb = [n for n in r["notes"] if "could not be built along it" in n]
        assert fb and "the dam line through the dam point" in fb[0], r["notes"]
        assert not any("from the embankment crest" in n for n in r["notes"]), r["notes"]
        T._check_model(r["model"], P, flow)
        with open(r["paths"]["readme"], encoding="utf-8") as f:
            assert fb[0] in f.read()
    finally:
        shutil.rmtree(d, ignore_errors=True)


# --- round 3 of the review: false negatives on a 10 m DEM (the Runner's default download) ----
_M10 = 10.0 / 0.3048                           # a 10 m DEM cell in feet


def _pool_grid(step, crest_w, top=130.0, toe_z=100.0, pool_z=122.0, x_c=None, mound=None):
    """A north-south embankment on `step` cells: its crest crest_w wide at `top`, 3:1 sides,
    centred on a cell boundary (x_c, default 16 cells: the DEM reads a narrow crest lowest
    there), the pool at pool_z (None: dry) against its upstream (west) face, the valley floor
    at toe_z. `mound` (x, y, height, radius) puts a low round mound on the pool. x0 = 0,
    ytop = 1000. Returns (z, x_c)."""
    n = int(1000.0 / step)
    x_c = 16.0 * step if x_c is None else x_c
    xs = (np.arange(n) + 0.5) * step
    gx, gy = np.meshgrid(xs, 1000.0 - xs)
    z = np.maximum(toe_z, top - np.maximum(np.abs(gx - x_c) - 0.5 * crest_w, 0.0) / 3.0)
    if pool_z is not None:
        z = np.where(gx < x_c, np.maximum(z, pool_z), z)
    if mound is not None:
        mx, my, mh, mr = mound
        z = np.maximum(z, pool_z + mh * np.clip(1.0 - np.hypot(gx - mx, gy - my) / mr, 0, 1))
    return z, x_c


def _snap10(z, step, crest_w, P, skew_deg=65.0, h=25.0, **kw):
    """_crest_snap as build_model calls it for a dam h ft high with a crest crest_w wide (3:1
    sides) on `step` cells, the flow direction skew_deg off the embankment's normal."""
    toe = 3.0 * h + 0.5 * crest_w
    a = dict(top=130.0, tol=max(3.0, 0.1 * h), search=max(3 * toe, 300.0),
             half=max(2 * toe, 150.0), toe=toe, prom=max(2.0, 0.1 * h))
    a.update(kw)
    s = math.radians(skew_deg)
    return hm._crest_snap(z, 0.0, 1000.0, step, P, a["top"], a["tol"], a["search"], a["half"],
                          a["toe"], a["prom"], d=np.array([math.cos(s), math.sin(s)]),
                          crest_w=crest_w, u="ft", dem_cell=step, side_h=3.0)


def _snapped_to(r, x_c, step):
    """The crest snap moved the dam line onto the crest at x_c, along it."""
    assert r is not None and r["why"] == "" and not r["on_crest"], r
    assert abs(r["point"][0] - x_c) <= 0.5 * step + 1.0, (r["point"], x_c)
    assert abs(r["axis"][0]) < 0.05, r["axis"]


@test
def v3264_on_crest_zone_is_half_the_crest_and_a_cell_from_the_crest():
    # round 3 of the review (M-a): the on-crest window (any crest-band ground within half the
    # crest width and one DEM cell of the dam point) reached past the band's own reach down
    # the side slopes, so a pool point up to crest_w + 3 tol + one cell from the crest centre
    # was 'on the crest', silently: on a 10 m DEM a 20 ft crest with the dam point 40 to 60 ft
    # upstream on a pool 8 ft below the top, a 100 ft crest with it 100 to 120 ft upstream, and
    # on a 5 m DEM that crest with it 122 ft upstream. On a pool (flat) the dam point is not
    # on the embankment; it is on the crest only within half the crest width and one DEM cell
    # of the crest found (and then the README says so when it is off the crest's top)
    y = 1000.0 - 15.5 * _M10
    z, x_c = _pool_grid(_M10, 20.0)
    for dist in (55.0, 60.0, 65.0):
        _snapped_to(_snap10(z, _M10, 20.0, (x_c - dist, y)), x_c, _M10)
    r = _snap10(z, _M10, 20.0, (x_c - 40.0, y))
    assert r is not None and r["on_crest"] and r["why"] == "", r
    assert 10.0 < r["off_crest"] <= 10.0 + _M10, r["off_crest"]
    z, x_c = _pool_grid(_M10, 100.0)
    for dist in (100.0, 120.0):
        _snapped_to(_snap10(z, _M10, 100.0, (x_c - dist, y)), x_c, _M10)
    m5 = 5.0 / 0.3048
    z, x_c = _pool_grid(m5, 100.0, x_c=32.0 * m5)
    _snapped_to(_snap10(z, m5, 100.0, (x_c - 122.0, 1000.0 - 31.5 * m5)), x_c, m5)
    # the dam point on the upstream side slope next to the crest stays on the crest (a2), as
    # does one on the crest itself
    assert _snap10(z, m5, 100.0, (x_c - 62.0, 1000.0 - 31.5 * m5)) is None
    assert _snap10(z, m5, 100.0, (x_c, 1000.0 - 31.5 * m5)) is None
    # rule (b) took the end of its window on the rising upstream face for a ridge: a pool
    # point about 70 ft upstream of a 20 ft crest with the flow direction 0 to 15 degrees off
    # was 'on a crest the DEM reads low'. The ridge must be higher than both ends of its
    # window
    z, x_c = _pool_grid(_M10, 20.0)
    for skew in (0.0, 15.0):
        _snapped_to(_snap10(z, _M10, 20.0, (x_c - 70.0, y), skew_deg=skew), x_c, _M10)
    tt = np.array([-1.0, 0.0])
    assert hm._snap_ridge(z, 0.0, 1000.0, _M10, (x_c - 70.0, y), -tt, 10.0 + _M10,
                          0.5 * _M10, interior=True) is None
    assert hm._snap_ridge(z, 0.0, 1000.0, _M10, (x_c, y), -tt, 10.0 + _M10, 0.5 * _M10,
                          interior=True) is not None


@test
def v3264_narrow_crest_read_low_is_crest_band_ground():
    # round 3 of the review (M-c): on a 10 m DEM a crest narrower than about 15 ft reads more
    # than the tolerance low, so the crest band |z - top| <= tol had none of it and the pool
    # point 92 ft upstream got no snap and no note. The band reaches the read-low allowance
    # (dem_cell - crest_w) / (2 x side slope) lower when the first band gives no good snap,
    # and the judge allows the ridge that much lower
    y = 1000.0 - 15.5 * _M10
    for cw in (8.0, 12.0):
        z, x_c = _pool_grid(_M10, cw)
        low = hm._snap_low(_M10, cw, 3.0)
        assert 130.0 - float(z.max()) > 3.0 and low > 3.0, (z.max(), low)
        r = _snap10(z, _M10, cw, (x_c - 92.0, y))
        _snapped_to(r, x_c, _M10)
        assert r["band_low"] == low and abs(r["turn"] - 65.0) < 2.0, r
    assert hm._snap_low(_M10, 40.0, 3.0) == 0.0
    # the judge's lower bound moves with it, and says so
    why = hm._snap_judge(z, 0.0, 1000.0, _M10, np.array([x_c, y]), np.array([1.0, 0.0]),
                         130.0 - 3.0 - low - 0.5, 130.0, 3.0, 85.0, 2.5, u="ft", low=low)
    assert "below the crest" in why and "the DEM can read a crest low" in why, why


@test
def v3264_high_pool_crest_stands_with_the_read_low_credit():
    # round 3 of the review (M-b): a typical 4 ft freeboard on a 10 m DEM: the 20 ft crest
    # reads 2.1 ft low, so it stood only 1.9 ft above the pool, less than max(2 ft, 10% of
    # 25 ft), and every pool point was rejected ('does not stand 2.5 ft above ...'). The ridge
    # is credited the read-low allowance, up to the top of dam and to half the prominence:
    # the pool point 92 ft upstream snaps
    y = 1000.0 - 15.5 * _M10
    z, x_c = _pool_grid(_M10, 20.0, pool_z=126.0)
    _snapped_to(_snap10(z, _M10, 20.0, (x_c - 92.0, y)), x_c, _M10)
    # the credit never makes a flat stand: the ridge must stand half the prominence above
    # both sides as the DEM reads it
    R, nu = np.array([500.0, 500.0]), np.array([1.0, 0.0])
    flat = np.full((31, 31), 126.0)
    assert not hm._snap_stands(flat, 0.0, 1000.0, _M10, R, nu, 126.0, 130.0, 2.13, 85.0,
                               2.5)[0]
    ridge = lambda h: np.tile(np.where(np.abs((np.arange(31) + 0.5) * _M10 - 500.0) <
                                       _M10, 126.0 + h, 126.0), (31, 1))
    assert hm._snap_stands(ridge(1.6), 0.0, 1000.0, _M10, R, nu, 127.6, 130.0, 2.13, 85.0,
                           2.5)[0]
    assert not hm._snap_stands(ridge(1.6), 0.0, 1000.0, _M10, R, nu, 127.6, 130.0, 0.0, 85.0,
                               2.5)[0]
    assert not hm._snap_stands(ridge(1.0), 0.0, 1000.0, _M10, R, nu, 127.0, 130.0, 2.13, 85.0,
                               2.5)[0]


@test
def v3264_rule_b_needs_a_ridge_that_stands():
    # round 3 of the review (L): a pool 5 ft below the top of a 20 ft crest on a 10 m DEM is
    # within the tolerance plus the read-low allowance (3 + 2.1 ft) of the top, so the flat
    # pool around a pool point would pass rule (b)'s elevation test as 'a crest the DEM reads
    # low'. It is not a ridge (level to both ends of the window) and does not stand above
    # both sides: the pool point 92 ft upstream snaps to the crest, also with a low mound on
    # the pool at it (higher than both ends, but not standing the prominence above both sides)
    y = 1000.0 - 15.5 * _M10
    z, x_c = _pool_grid(_M10, 20.0, pool_z=125.0)
    P = (x_c - 92.0, y)
    assert float(hm._sample(z, 0.0, 1000.0, _M10, np.array([P]))[0]) >= \
        130.0 - 3.0 - hm._snap_low(_M10, 20.0, 3.0)
    for skew in (0.0, 65.0):
        _snapped_to(_snap10(z, _M10, 20.0, P, skew_deg=skew), x_c, _M10)
    zm, x_c = _pool_grid(_M10, 20.0, pool_z=125.0, mound=(P[0], P[1], 1.0, 60.0))
    rr = hm._snap_ridge(zm, 0.0, 1000.0, _M10, P, np.array([1.0, 0.0]), 10.0 + _M10,
                        0.5 * _M10, interior=True)
    assert rr is not None and rr[1] >= 130.0 - 3.0 - hm._snap_low(_M10, 20.0, 3.0), rr
    _snapped_to(_snap10(zm, _M10, 20.0, P, skew_deg=0.0), x_c, _M10)
    # a flat dry basin floor within that reach of the top, with no embankment, is not a
    # crest: no snap, and the README says why
    flat = np.full((30, 30), 125.0)
    r = _snap10(flat, _M10, 20.0, (500.0, 500.0))
    assert r is not None and "the flat the dam point is on" in r["why"], r


@test
def v3264_crest_axis_near_an_abutment():
    # round 3 of the review: a pool point 92 ft upstream and 45 m from an abutment (rising at
    # 15 % from 2 m below the top of dam): the band's contour up the hillside beside the crest
    # turned the long axis 5 to 8 degrees off the crest. The axis is taken again from the band
    # near the crest's line
    z, x_c = _pool_grid(_M10, 20.0)
    xs = (np.arange(z.shape[0]) + 0.5) * _M10
    gx, gy = np.meshgrid(xs, 1000.0 - xs)
    off = np.abs(gy - 500.0) - 197.0
    z = np.where(off > 0, np.maximum(z, 130.0 - 6.6 + off * 0.15), z)
    for skew in (0.0, 65.0):
        _snapped_to(_snap10(z, _M10, 20.0, (x_c - 92.0, 1000.0 - 19.5 * _M10),
                            skew_deg=skew), x_c, _M10)


@test
def v3264_off_crest_top_and_read_low_notes():
    # round 3 of the review: a dam point within half the crest width and one DEM cell of the
    # crest found, but off its top (here 40 ft upstream of the centre of a 20 ft crest, on the
    # pool), is not moved, and the README says so (no silent fallback)
    if not _has_gdal():
        print("  (skipped: no GDAL)")
        return
    ang = math.radians(65.0)
    P_off = (-40.0 * FT, -100.0)
    L = 90.0 / math.cos(ang)
    I_off = (P_off[0] + L * math.cos(ang), P_off[1] + L * math.sin(ang))
    d, r, P, C, valley_axis, flow = _snap_model(
        _pool_dam(20.0, 8.0, 300.0),
        {"crest_length_ft": 600.0 / FT, "dam_height_ft": 25.0, "crest_width_ft": 20.0},
        dam_xy=P_off, inflow_xy=I_off)
    try:
        assert not any("from the embankment crest" in n or "rejected" in n
                       for n in r["notes"]), r["notes"]
        kept = [n for n in r["notes"] if "off the line of the embankment crest" in n]
        assert kept and "the dam line is not moved" in kept[0], r["notes"]
        A, B, u = _dam_line(r["model"])
        assert abs(float(np.cross(B - A, P - A))) / np.hypot(*(B - A)) < 1.0
        with open(r["paths"]["readme"], encoding="utf-8") as f:
            assert kept[0] in f.read()
    finally:
        shutil.rmtree(d, ignore_errors=True)
    # the rejection note of a dam point that may be on a crest the DEM does not show (an 8 ft
    # crest the 10 m DEM reads 4.1 ft low, with a pool higher than that beside it) says so
    snap = {"z_near": 126.0, "r_near": 36.8, "low": 4.13, "dem_cell": 32.8}
    t = hm._near_text(snap, 130.0, 3.0, "ft")
    assert "may be on a crest the DEM does not show" in t and "4.1 ft" in t, t
    assert "may be on a crest" not in hm._near_text(dict(snap, z_near=120.0), 130.0, 3.0, "ft")


@test
def v3264_reservoir_volumes_rise_as_written():
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
