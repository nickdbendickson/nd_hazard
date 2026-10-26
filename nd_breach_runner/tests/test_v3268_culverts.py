"""3.26.8: a culvert cut as a gap from the crossings (CROSSING_GAPS).

LISFLOOD-FP, the built-in engine and ANUGA get exactly what 3.26.7 gave them: the whole gap
line (120 m) with the inventory's inverts only. A 50 m line with the channel beds beside the
road as inverts (the first 3.26.8 change) stopped on the slope of a wide fill (toe more than
about 35 m from the road), was then 'datum shifted' up that slope as if the beds were inventory
elevations, and left the second carriageway of a divided highway standing: the built-in
engine passed 31-44 % less water (none at all on the divided highway) and ANUGA none.

Only the HEC-RAS export changed: its barrel runs from the toe of the embankments on one side
(beyond the outermost carriageway the gap line crosses) to the other, upstream on the higher
channel bed's side, with the channel bed at each toe as its inverts, at least 1.5 cells long,
centred on the road crest where HEC-RAS lays it, its ends in two different cells and its
inverts no lower than those cells (HEC-RAS refuses either)."""
import json
import math
import os
import shutil
import tempfile

import numpy as np

from nd_breach_runner.core import conditioning as cond
from nd_breach_runner.core import gaps as gp
from nd_breach_runner.core import hecras_model as hm
from nd_breach_runner.core import structures as sm
from nd_breach_runner.core.grid import Grid
from nd_breach_runner.core.units import FT
from nd_breach_runner.tests.registry import test


# ------------------------------------------------------------ synthetic road crossings --
def _embankment(H=5.0, S=6.0, CD=2.0, TW=6.0, twin=0.0, slope=0.0005):
    """A road along y = 0 (and a second carriageway along y = -twin), its crown H m above a
    field at 100 + slope * y, side slopes S:1, top 2 TW wide; a channel CD m deep and 8 m wide
    runs north to south at x = 0 under both. The field and channel fall to the south."""
    def z_at(X, Y):
        field = 100.0 + slope * Y
        ground = np.where(np.abs(X) < 4.0, field - CD, field)
        emb = 100.0 + H - np.maximum(0.0, np.abs(Y) - TW) / S
        if twin:
            emb = np.maximum(emb, 100.0 + H - np.maximum(0.0, np.abs(Y + twin) - TW) / S)
        return np.maximum(ground, emb)
    roads = [np.array([[-300.0, 0.0], [300.0, 0.0]])]
    if twin:
        roads.append(np.array([[-300.0, -twin], [300.0, -twin]]))
    return z_at, roads


def _reader(z_at, sub_m=1.0):
    """read_dem(bounds, cell) for the workflow's calls: a block average of the ground (as a
    resampled LiDAR)."""
    def read_dem(bounds, cell):
        x0, y0, x1, y1 = bounds
        nx = max(4, int(math.ceil((x1 - x0) / cell)))
        ny = max(4, int(math.ceil((y1 - y0) / cell)))
        sub = max(1, int(round(cell / sub_m)))
        xs = x0 + (np.arange(nx * sub) + 0.5) * cell / sub
        ys = (y0 + ny * cell) - (np.arange(ny * sub) + 0.5) * cell / sub
        X, Y = np.meshgrid(xs, ys)
        z = z_at(X, Y).reshape(ny, sub, nx, sub).mean(axis=(1, 3))
        return Grid(z, x0, y0 + ny * cell, cell)
    return read_dem


# the auditors' cases: a wide fill (7 m over the bed at 6:1, toe 48 m out) and a divided
# highway (two 3 m carriageways 35 m apart at 4:1)
CASES = {"wide fill": dict(H=5.0, S=6.0), "divided highway": dict(H=3.0, S=4.0, twin=35.0)}
CROSSING = {"name": "NBI 1", "x": 0.0, "y": 1.0, "kind": "culvert", "span_ft": 10.0,
            "rise_ft": 4.0, "barrels": 2, "source": "NBI (rise 4 ft assumed)"}


def _chain(name, cs=10.0, extra_roads=()):
    """The workflow's calls: blocking_rows(.., min(cs, 3)), structures_from(rows, cs),
    reconcile_inverts(new, read, 3 ft, min(cs, 3))."""
    z_at, roads = _embankment(**CASES[name])
    read = _reader(z_at)
    rows = gp.blocking_rows([dict(CROSSING)], roads + list(extra_roads), read, min(cs, 3.0))
    structs, _ = gp.structures_from(rows, cs)
    rnotes = sm.reconcile_inverts(structs, read, 3.0 * FT, min(cs, 3.0))
    return z_at, read, rows, structs, rnotes


def _as_3267(rows):
    """The structures 3.26.7 (and 2a3ca38) cut from these rows: the row's gap line and the
    inventory's inverts."""
    out = []
    for r in rows:
        if not r.get("surveyed") or not r.get("line"):
            continue
        out.append(sm.Structure(
            kind=r.get("kind") or sm.KIND_BRIDGE, line=np.asarray(r["line"], dtype=float),
            width_m=float(r["span_ft"]) * FT, height_m=float(r["rise_ft"]) * FT,
            barrels=max(1, int(r.get("barrels") or 1)), shape=str(r.get("shape") or "box"),
            invert_up_m=(None if r.get("invert_up_ft") is None else r["invert_up_ft"] * FT),
            invert_dn_m=(None if r.get("invert_dn_ft") is None else r["invert_dn_ft"] * FT),
            name="{} ({})".format(r["name"], r.get("source") or "surveyed crossing")))
    return out


@test
def v3268_crossing_culverts_reach_the_models_as_in_3267():
    for name in CASES:
        cs = 10.0
        z_at, read, rows, structs, rnotes = _chain(name, cs)
        ref = _as_3267(rows)
        assert len(structs) == 1 and len(ref) == 1
        s = structs[0]
        # the whole gap line, no inverts: nothing for the LiDAR check to move
        assert s.to_dict() == ref[0].to_dict(), name
        assert abs(sm.polyline_len(s.line) - 120.0) < 1e-6
        assert s.invert_up_m is None and s.invert_dn_m is None and s.invert_note == ""
        assert rnotes == [] and s.invert_inventory is None, rnotes
        # LISFLOOD-FP / built-in: the same notch, which opens every carriageway to the bed
        k = int(200 // cs)
        g = read((-(k + 0.5) * cs, -(k + 0.5) * cs, (k + 0.5) * cs, (k + 0.5) * cs), cs)
        for model in ("lisflood", "anuga"):
            a, b = (sm.structure_edits(x, cs, model) for x in (structs, ref))
            assert [(n.line.tolist(), n.width_m, n.invert_up_m, n.invert_dn_m) for n in a] == \
                [(n.line.tolist(), n.width_m, n.invert_up_m, n.invert_dn_m) for n in b], model
        cut, _, _ = cond.Edits(notches=sm.structure_edits(structs, cs, "lisflood")).apply(g)
        z = cut.masked()
        col = int((0.0 - cut.x0) // cs)
        ys = cut.y_centers()
        along = z[(np.abs(ys) <= 100.0), col]
        ends = z[np.abs(np.abs(ys) - 100.0) <= cs, col]
        # no barrier along the channel: the notch goes through the whole embankment(s)
        assert float(along.max()) <= float(ends.max()) + 0.3, (name, along.max(), ends)
        # ANUGA: the same culvert, its ends at the gap line's ends, inverts from the DEM there
        dem = read((-200.0, -200.0, 200.0, 200.0), 5.0)
        outer = np.array([[-200.0, -200.0], [200.0, -200.0], [200.0, 200.0], [-200.0, 200.0]])
        sa, na = sm.anuga_culverts(structs, dem, outer)
        sb, nb = sm.anuga_culverts(ref, dem, outer)
        assert sa == sb and na == nb == [] and len(sa) == 1, (na, nb)
        assert np.allclose(sa[0]["end_points"], np.asarray(rows[0]["line"]))


@test
def v3268_the_barrel_toes_lie_beyond_the_outer_carriageway():
    z_at, _, rows, _, _ = _chain("wide fill")
    b = rows[0]["barrel"]
    assert b["roads"] == 1 and b["clear"] == [True, True], b
    (xs, ys_), (xn, yn) = b["ends"]
    assert abs(xs) < 1e-6 and abs(xn) < 1e-6
    # the toe of a 7 m fill at 6:1 is 48 m out (the 3.26.8 line stopped at 25 m)
    assert -51.0 <= ys_ <= -45.0 and 45.0 <= yn <= 51.0, b["ends"]
    for (x, y), bed in zip(b["ends"], b["beds_ft"]):
        assert abs(bed * FT - float(z_at(np.array(0.0), np.array(y)))) <= 1.0 * FT, (y, bed)
    # a road meeting the crossing line at 60 degrees is not a carriageway
    diag = np.array([[-100.0, 40.0 - 100.0 * math.sqrt(3)], [100.0, 40.0 + 100.0 * math.sqrt(3)]])
    _, _, rows2, _, _ = _chain("wide fill", extra_roads=[diag])
    assert rows2[0]["barrel"] == b
    # the bed at the toe is the channel there, not the lowest ground of the whole walk: on a
    # channel falling 1 % the walk's lowest ground is 0.6 m lower, 60 m further out
    z_at, roads = _embankment(H=4.0, S=3.0, slope=0.01)
    rows3 = gp.blocking_rows([dict(CROSSING)], roads, _reader(z_at), 3.0)
    for (x, y), bed in zip(rows3[0]["barrel"]["ends"], rows3[0]["barrel"]["beds_ft"]):
        chan = 100.0 + 0.01 * y - 2.0
        assert abs(bed * FT - chan) <= 0.15, (y, bed * FT, chan)
    # a fill wider than the walk (12 m over the bed at 6:1, toe 78 m out): still on its slope
    # at the end of the walk, so its beds do not say which way the water runs
    z_at, roads = _embankment(H=10.0, S=6.0)
    rows4 = gp.blocking_rows([dict(CROSSING)], roads, _reader(z_at), 3.0)
    assert rows4[0]["barrel"]["clear"] == [False, False], rows4[0]["barrel"]
    # divided highway: both carriageways, the toes beyond the outer ones (y = 26 and -61)
    z_at, _, rows, _, _ = _chain("divided highway")
    b = rows[0]["barrel"]
    assert b["roads"] == 2 and b["stations_m"] == [-35.0, 0.0], b
    (_, ys_), (_, yn) = b["ends"]
    assert -64.0 <= ys_ <= -58.0 and 23.0 <= yn <= 29.0, b["ends"]
    assert b["clear"] == [True, True]
    for (x, y), bed in zip(b["ends"], b["beds_ft"]):
        assert abs(bed * FT - float(z_at(np.array(0.0), np.array(y)))) <= 1.0 * FT, (y, bed)


def _ident(a):
    return np.asarray(a, dtype=float).reshape(-1, 2)


def _cell_of(pts, xy):
    return int(np.argmin(((pts - np.asarray(xy, float)) ** 2).sum(axis=1)))


def _cell_min(pts, k, ground, step=0.5):
    """Independent: the lowest ground over the points nearer to pts[k] than to any other."""
    p = pts[k]
    r = 2.0 * float(np.sort(np.hypot(*(pts - p).T))[1])
    g = np.arange(-r, r + 1e-9, step)
    X, Y = np.meshgrid(p[0] + g, p[1] + g)
    xy = np.column_stack([X.ravel(), Y.ravel()])
    near = pts[np.hypot(*(pts - p).T) <= 2.0 * r]
    own = np.argmin(((xy[:, None, :] - near[None, :, :]) ** 2).sum(-1), axis=1) == \
        int(np.argmin(np.hypot(*(near - p).T)))
    return float(np.nanmin(ground(xy[own])))


_RUNS = {}


def _export(name, cell, off=0.0):
    """The export's calls for the crossing: crossing_barrel -> structure_connection ->
    fit_barrel, on a computation-point grid of `cell` (SI, model CRS = run CRS)."""
    u = hm.Units(True)
    if name not in _RUNS:
        z_at, read, rows, structs, _ = _chain(name)
        _RUNS[name] = (rows, structs, read((-400.0, -400.0, 400.0, 400.0), 1.0))
    rows, structs, G = _RUNS[name]
    s, b = structs[0], rows[0]["barrel"]

    def ground(xy):
        xy = np.asarray(xy, float).reshape(-1, 2)
        return G.sample_bilinear(xy[:, 0], xy[:, 1])
    per = np.array([[-390.0, -390.0], [390.0, -390.0], [390.0, 390.0], [-390.0, 390.0]])
    xs = np.arange(-390.0 + (0.5 + off) * cell, 390.0, cell)
    gx, gy = np.meshgrid(xs, xs)
    pts = np.column_stack([gx.ravel(), gy.ravel()])
    line, bar, notes = hm.crossing_barrel(s, b, _ident, u, cell)
    c, why = hm.structure_connection(s, line, ground, per, cell, 2.0, u, "C", barrel=bar)
    assert why is None, why
    fnotes, why = hm.fit_barrel(c, pts, ground, per, cell, 2.0, u)
    assert why is None, why
    return b, line, c, notes + c["notes"] + fnotes, pts, ground


@test
def v3268_hecras_lays_the_barrel_toe_to_toe_from_the_higher_bed():
    for name in CASES:
        for cell_ft in (100.0, 200.0):
            for off in (0.0, 0.5):
                cell = cell_ft * FT
                b, line, c, notes, pts, ground = _export(name, cell, off)
                cv = c["culvert"]
                (_, y_s), (_, y_n) = b["ends"]
                bed_s, bed_n = (v * FT for v in b["beds_ft"])
                # upstream = the north toe (the higher channel bed), downstream the south toe
                assert line[0][1] > 0 > line[-1][1], line
                assert bed_n > bed_s
                assert cv["inverts_from"] == hm.BED_LABEL + " / " + hm.BED_LABEL
                raised = [n for n in notes if "invert was raised" in n]
                for inv, bed, word in ((cv["inv_up"], bed_n, "upstream"),
                                       (cv["inv_dn"], bed_s, "downstream")):
                    # the channel bed at the toe, unless raised to its cell
                    assert abs(inv - bed) < 0.006 or (inv > bed and any(
                        word in n for n in raised)), (name, cell_ft, off, inv, bed, notes)
                # toe to toe, at least 1.5 cells, and both ends at or beyond the toes
                assert cv["length"] >= y_n - y_s - 0.01 and cv["length"] >= 1.5 * cell - 0.01
                for pu, pd in hm.barrel_ends(c):
                    assert pu[1] >= y_n - 1.0 and pd[1] <= y_s + 1.0, (pu, pd, y_n, y_s)
                    ku, kd = _cell_of(pts, pu), _cell_of(pts, pd)
                    assert ku != kd
                    # no invert below the lowest ground of its cell
                    assert cv["inv_up"] >= _cell_min(pts, ku, ground) - 1e-6, (name, cell_ft)
                    assert cv["inv_dn"] >= _cell_min(pts, kd, ground) - 1e-6, (name, cell_ft)
                assert "higher channel bed" in notes[0] and "not an inventory" in notes[0]
                if name == "divided highway":
                    assert "beyond the outer of 2 carriageways" in notes[0]


@test
def v3268_a_barrel_invert_below_its_cell_is_raised_and_a_short_barrel_lengthened():
    u = hm.Units(True)

    def ground(xy):
        xy = np.asarray(xy, float).reshape(-1, 2)
        return 100.0 + 0.0 * xy[:, 0]                     # flat ground at 100 m
    per = np.array([[-300.0, -300.0], [300.0, -300.0], [300.0, 300.0], [-300.0, 300.0]])
    xs = np.arange(-285.0, 300.0, 30.0)
    gx, gy = np.meshgrid(xs, xs)
    pts = np.column_stack([gx.ravel(), gy.ravel()])
    # a connection across a culvert running south, inverts 1 m below the cells, barrel 6 m
    c = {"name": "C", "line": [(-25.0, 15.0), (55.0, 15.0)],
         "culvert": {"length": 6.0, "stations": [40.0], "inv_up": 99.0, "inv_dn": 99.0}}
    notes, why = hm.fit_barrel(c, pts, ground, per, 30.0, 1.0, u)
    assert why is None
    cv = c["culvert"]
    ku, kd = (_cell_of(pts, q) for q in hm.barrel_ends(c)[0])
    assert ku != kd and cv["length"] == 36.0, cv                  # 6 m: one cell, 2 steps
    assert any("lengthened" in n and "different 2D cells" in n for n in notes), notes
    want = round(math.ceil((100.0 + 0.05 * FT) * 100.0) / 100.0, 2)
    assert cv["inv_up"] == cv["inv_dn"] == want, cv
    assert sum("invert was raised from 99.00 to {:.2f}".format(want) in n for n in notes) == 2
    # the lowest ground of a cell is at its low corner, not at its point: ground falling 1 %
    # east, the cell of (15, 15) is 15 m either side, lowest at x = 30
    k = _cell_of(pts, (15.0, 15.0))

    def sloped(xy):
        xy = np.asarray(xy, float).reshape(-1, 2)
        return 100.0 - 0.01 * xy[:, 0]
    assert abs(hm.cell_min_ground(pts, k, sloped, per, 1.0) - 99.7) < 0.01
    # ... and inside the 2D area only: a cell cut by the perimeter at x = 20
    cut = np.array([[-300.0, -300.0], [20.0, -300.0], [20.0, 300.0], [-300.0, 300.0]])
    assert abs(hm.cell_min_ground(pts, k, sloped, cut, 1.0) - 99.8) < 0.01
    # an invert already above its cell stays
    c["culvert"]["inv_up"] = 100.5
    notes, why = hm.fit_barrel(c, pts, ground, per, 30.0, 1.0, u)
    assert why is None and cv["inv_up"] == 100.5 and not notes
    # a barrel that would end outside the 2D area is left out
    c2 = {"name": "C2", "line": [(-40.0, 280.0), (40.0, 280.0)],
          "culvert": {"length": 60.0, "stations": [40.0], "inv_up": 100.5, "inv_dn": 100.5}}
    notes, why = hm.fit_barrel(c2, pts, ground, per, 30.0, 1.0, u)
    assert notes is None and "outside the 2D area" in why


@test
def v3268_the_barrel_turns_with_the_flow_path_when_the_beds_cannot_tell():
    u = hm.Units(True)
    # the run's gap line runs south to north; the inventory's inverts are tied to its ends
    s = sm.Structure("culvert", np.array([[0.0, -60.0], [0.0, 60.0]]), 3.0, 1.2, 1,
                     invert_up_m=96.0, invert_dn_m=95.0, name="C")
    b = {"ends": [[0.0, -30.0], [0.0, 30.0]], "beds_ft": [320.0, 321.0],
         "clear": [False, True], "roads": 1}
    south_flow = np.array([[0.0, 500.0], [0.0, -500.0]])
    line, bar, notes = hm.crossing_barrel(s, b, _ident, u, 30.0, south_flow)
    # turned: the north toe first, each run invert kept at its own end
    assert line[0][1] == 30.0 and line[-1][1] == -30.0
    assert bar["inverts"] == [95.0, 96.0] and bar["from"] == ["run", "run"]
    assert "flow path" in notes[0]
    line, bar, notes = hm.crossing_barrel(s, b, _ident, u, 30.0, south_flow[::-1])
    assert line[0][1] == -30.0 and bar["inverts"] == [96.0, 95.0]
    # no flow path, or one along the road: the run's line decides
    for path in (None, np.array([[-500.0, 0.0], [500.0, 0.0]])):
        line, bar, notes = hm.crossing_barrel(s, b, _ident, u, 30.0, path)
        assert line[0][1] == -30.0 and "crossing line starts" in notes[0]
    # both toes clear: the higher bed is upstream whatever the path says
    b["clear"] = [True, True]
    line, bar, notes = hm.crossing_barrel(s, b, _ident, u, 30.0, south_flow[::-1])
    assert line[0][1] == 30.0 and "higher channel bed" in notes[0]
    # no inventory inverts: the beds at the toes, labelled as such
    s.invert_up_m = s.invert_dn_m = None
    line, bar, notes = hm.crossing_barrel(s, b, _ident, u, 30.0)
    assert np.allclose(bar["inverts"], [321.0 * FT, 320.0 * FT])
    assert bar["from"] == [hm.BED_LABEL] * 2 and "not an inventory elevation" in notes[0]
    # toes closer than 1.5 cells: the barrel is 1.5 cells long about their middle
    b["ends"] = [[0.0, -10.0], [0.0, 10.0]]
    line, bar, notes = hm.crossing_barrel(s, b, _ident, u, 30.0)
    assert abs(np.hypot(*(line[1] - line[0])) - 45.0) < 1e-6 and "1.5 x the 2D cell" in notes[1]
    # no toe on a side: nothing to lay
    b["ends"] = [None, [0.0, 10.0]]
    assert hm.crossing_barrel(s, b, _ident, u, 30.0)[0] is None


# ------------------------------------------------------------ the built HEC-RAS model --
ROAD_X, TWIN_DX = 2000.0, 50.0       # carriageways 2 km below the dam, 50 m apart (run metres)


def _divided_dem(dem):
    """Two carriageways across the synthetic valley of test_hecras_model.make_run, 4 m above
    the bed, 2:1 side slopes, 10 m tops."""
    from osgeo import gdal
    from nd_breach_runner.tests.test_hecras_model import X0, Y0
    from nd_breach_runner.tests.synthetic import TOP_M, H_M
    ds = gdal.Open(dem, gdal.GA_Update)
    gt = ds.GetGeoTransform()
    a = ds.GetRasterBand(1).ReadAsArray().astype(float)
    xs = gt[0] + (np.arange(a.shape[1]) + 0.5) * gt[1] - X0
    ys = gt[3] + (np.arange(a.shape[0]) + 0.5) * gt[5] - Y0
    X, Y = np.meshgrid(xs, ys)
    for rx in (ROAD_X, ROAD_X + TWIN_DX):
        crest = TOP_M - H_M - 0.001 * rx + 4.0 + 0.002 * np.abs(Y)
        road = crest - np.clip(np.abs(X - rx) - 5.0, 0.0, None) * 0.5
        ok = a > -9000
        a[ok] = np.maximum(a, road)[ok]
    ds.GetRasterBand(1).WriteArray(a.astype(np.float32))
    ds = None


def _tif_reader(path):
    """read_dem(bounds, cell) from a GeoTIFF (bilinear), as the run reads its terrain."""
    from osgeo import gdal
    ds = gdal.Open(path)
    gt = ds.GetGeoTransform()
    a = ds.GetRasterBand(1).ReadAsArray().astype(float)
    a[a < -9000] = np.nan
    src = Grid(a, gt[0], gt[3], gt[1])

    def read_dem(bounds, cell):
        x0, y0, x1, y1 = bounds
        nx = max(4, int(math.ceil((x1 - x0) / cell)))
        ny = max(4, int(math.ceil((y1 - y0) / cell)))
        g = Grid(np.zeros((ny, nx)), x0, y0 + ny * cell, cell)
        X, Y = np.meshgrid(g.x_centers(), g.y_centers())
        return Grid(src.sample_bilinear(X, Y), x0, y0 + ny * cell, cell)
    return read_dem


@test
def v3268_the_hecras_export_lays_a_divided_highways_culvert_toe_to_toe():
    from nd_breach_runner.tests.test_hecras_model import _has_gdal, make_run, X0, Y0
    if not _has_gdal():
        print("  (skipped: no GDAL)")
        return
    from osgeo import gdal
    d = tempfile.mkdtemp()
    try:
        dfold, dem, dam, hy = make_run(d)
        _divided_dem(dem)
        read = _tif_reader(dem)
        roads = [np.array([[X0 + rx, Y0 - 600.0], [X0 + rx, Y0 + 600.0]])
                 for rx in (ROAD_X, ROAD_X + TWIN_DX)]
        cross = [{"name": "NBI 1", "x": X0 + ROAD_X + 2.0, "y": Y0 + 1.0, "kind": "culvert",
                  "span_ft": 8.0, "rise_ft": 4.0, "barrels": 2, "source": "NBI"}]
        rows = gp.blocking_rows(cross, roads, read, 3.0)
        structs, _ = gp.structures_from(rows, 10.0)
        assert sm.reconcile_inverts(structs, read, 3.0 * FT, 3.0) == []
        b = rows[0]["barrel"]
        assert b["roads"] == 2 and b["clear"] == [True, True], b
        p = os.path.join(dfold, "_breach_model", "run_report.json")
        with open(p) as f:
            rep = json.load(f)
        rep["structures_used"] = [s.to_dict() for s in structs]
        rep["crossing_gaps"] = {"rows": rows, "cut": True, "blocked": 1, "cut_count": 1}
        with open(p, "w") as f:
            json.dump(rep, f)
        r = hm.build_model(dfold, os.path.join(d, "out"))
        m = r["model"]
        assert [c["kind"] for c in m.structures] == ["culvert"], r["info"]["structures"]
        c = m.structures[0]
        cv = c["culvert"]
        assert cv["inverts_from"] == hm.BED_LABEL + " / " + hm.BED_LABEL
        # the toes in the model CRS (ND State Plane, feet): upstream is the west (higher) bed
        from osgeo import osr
        src, dst = osr.SpatialReference(), osr.SpatialReference()
        src.ImportFromEPSG(26914)
        dst.ImportFromEPSG(2266)
        for x in (src, dst):
            x.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        to_model = hm.transformer(src, dst)
        (wx, wy), (ex, ey) = to_model(b["ends"]) if b["ends"][0][0] < b["ends"][1][0] else \
            to_model(b["ends"][::-1])
        west_bed, east_bed = (sorted(zip((e[0] for e in b["ends"]), b["beds_ft"]))[i][1]
                              for i in (0, 1))
        assert west_bed > east_bed
        toe_len = float(np.hypot(ex - wx, ey - wy))
        assert toe_len * FT > TWIN_DX + 20.0                 # spans both carriageways
        assert cv["length"] >= toe_len - 0.01 and cv["length"] >= 1.5 * m.cell - 0.01
        tif = r["paths"]["terrain_tif"]
        tread = gdal.Open(tif)
        gt = tread.GetGeoTransform()
        ta = tread.GetRasterBand(1).ReadAsArray().astype(float)
        nd = tread.GetRasterBand(1).GetNoDataValue()
        if nd is not None:
            ta[ta == nd] = np.nan
        tread = None

        def ground(xy):
            return hm._sample(ta, gt[0], gt[3], gt[1], xy)
        pts = np.asarray(m.points, float)
        for pu, pd in hm.barrel_ends(c):
            assert pu[0] <= wx + 1.0 and pd[0] >= ex - 1.0, (pu, pd, wx, ex)
            ku, kd = _cell_of(pts, pu), _cell_of(pts, pd)
            assert ku != kd
            assert cv["inv_up"] >= _cell_min(pts, ku, ground, 1.0) - 1e-6
            assert cv["inv_dn"] >= _cell_min(pts, kd, ground, 1.0) - 1e-6
        # the inverts are the channel beds at the toes (feet), unless raised to their cells,
        # and the cell at each end reaches down to within 1 ft of that bed (the HEC-RAS
        # terrain smooths the embankment past the run's toe: the barrel was lengthened)
        for inv, bed, word, cm in ((cv["inv_up"], west_bed, "upstream", cv["cell_min"][0]),
                                   (cv["inv_dn"], east_bed, "downstream", cv["cell_min"][1])):
            assert abs(inv - bed) < 0.06 or (inv > bed and any(
                word + " invert was raised" in n for n in c["notes"])), (inv, bed, c["notes"])
            assert cm <= bed + 1.0, (word, cm, bed)
        assert cv["inv_up"] > cv["inv_dn"], cv            # the barrel falls downstream
        with open(r["paths"]["readme"], encoding="utf-8") as fh:
            rd = fh.read()
        assert "beyond the outer of 2 carriageways" in rd and "higher channel bed" in rd
        assert "'{}' the ground at the toe".format(hm.BED_LABEL) in rd
        assert "not an inventory elevation" in rd
    finally:
        shutil.rmtree(d)
