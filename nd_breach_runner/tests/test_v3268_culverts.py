"""3.26.8: a culvert cut as a gap from the crossings (CROSSING_GAPS).

LISFLOOD-FP, the built-in engine and ANUGA get exactly what 3.26.7 gave them: the whole gap
line (120 m) with the inventory's inverts only. A 50 m line with the channel beds beside the
road as inverts (the first 3.26.8 change) stopped on the slope of a wide fill (toe more than
about 35 m from the road), was then 'datum shifted' up that slope as if the beds were inventory
elevations, and left the second carriageway of a divided highway standing: the built-in
engine passed 31-44 % less water (none at all on the divided highway) and ANUGA none.

Only the HEC-RAS export changed: its barrel runs from where the channel leaves the road
embankment on one side (beyond the outermost carriageway) to the other, found in the terrain
along the road either side of the inventory's point (the point can be off the channel, the
channel skewed to the road or bent at it), upstream on the side the run's flow path comes
from (else the higher ground at the toe), with the channel bed at each end as its inverts, at
least one cell long, centred on a carriageway crest where HEC-RAS lays it, its ends in two
different cells and its inverts no lower than those cells, read both between the terrain's
pixels and from the pixels themselves (HEC-RAS refuses either). Where no end is found (no
terrain before the toe, a toe beyond the search, a run before 3.26.8) the culvert keeps the
run's line, as 2a3ca38 exported it, with a WARNING."""
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
def _embankment(H=5.0, S=6.0, CD=2.0, TW=6.0, twin=0.0, slope=0.0005, chan_x=None, half=4.0,
                ditch=None):
    """A road along y = 0 (and a second carriageway along y = -twin), its crown H m above a
    field at 100 + slope * y, side slopes S:1, top 2 TW wide; a channel CD m deep and 2 half
    wide runs north to south under both, its centre at x = chan_x(y) (0 by default). The field
    and channel fall to the south. `ditch` (y0, y1, z): a borrow ditch along the road."""
    def z_at(X, Y):
        field = 100.0 + slope * Y
        cx = 0.0 if chan_x is None else chan_x(Y)
        ground = np.where(np.abs(X - cx) < half, field - CD, field)
        if ditch is not None:
            ground = np.where((Y >= ditch[0]) & (Y <= ditch[1]), np.minimum(ground, ditch[2]),
                              ground)
        emb = 100.0 + H - np.maximum(0.0, np.abs(Y) - TW) / S
        if twin:
            emb = np.maximum(emb, 100.0 + H - np.maximum(0.0, np.abs(Y + twin) - TW) / S)
        return np.maximum(ground, emb)
    roads = [np.array([[-300.0, 0.0], [300.0, 0.0]])]
    if twin:
        roads.append(np.array([[-300.0, -twin], [300.0, -twin]]))
    return z_at, roads


def _reader(z_at, sub_m=1.0, mask=None):
    """read_dem(bounds, cell) for the workflow's calls: a block average of the ground (as a
    resampled LiDAR), NaN where `mask(X, Y)`."""
    def read_dem(bounds, cell):
        x0, y0, x1, y1 = bounds
        nx = max(4, int(math.ceil((x1 - x0) / cell)))
        ny = max(4, int(math.ceil((y1 - y0) / cell)))
        sub = max(1, int(round(cell / sub_m)))
        xs = x0 + (np.arange(nx * sub) + 0.5) * cell / sub
        ys = (y0 + ny * cell) - (np.arange(ny * sub) + 0.5) * cell / sub
        X, Y = np.meshgrid(xs, ys)
        z = z_at(X, Y)
        if mask is not None:
            z = np.where(mask(X, Y), np.nan, z)
        z = z.reshape(ny, sub, nx, sub).mean(axis=(1, 3))
        return Grid(z, x0, y0 + ny * cell, cell)
    return read_dem


# the auditors' cases: a wide fill (7 m over the bed at 6:1, toe 48 m out) and a divided
# highway (two 3 m carriageways 35 m apart at 4:1)
CASES = {"wide fill": dict(H=5.0, S=6.0), "divided highway": dict(H=3.0, S=4.0, twin=35.0)}
CROSSING = {"name": "NBI 1", "x": 0.0, "y": 1.0, "kind": "culvert", "span_ft": 10.0,
            "rise_ft": 4.0, "barrels": 2, "source": "NBI (rise 4 ft assumed)"}
SOUTH = np.array([[0.0, 500.0], [0.0, -500.0]])      # the run's flow path: north to south


def _chain(name, cs=10.0, extra_roads=(), crossing=None, **kw):
    """The workflow's calls: blocking_rows(.., min(cs, 3)), structures_from(rows, cs),
    reconcile_inverts(new, read, 3 ft, min(cs, 3))."""
    z_at, roads = _embankment(**dict(CASES.get(name, {}), **kw))
    read = _reader(z_at)
    rows = gp.blocking_rows([dict(crossing or CROSSING)], roads + list(extra_roads), read,
                            min(cs, 3.0))
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


def _engine_inputs(read, structs, rnotes, cs=10.0):
    """Everything LISFLOOD-FP, the built-in engine and ANUGA get from the cut crossings."""
    k = int(200 // cs)
    g = read((-(k + 0.5) * cs, -(k + 0.5) * cs, (k + 0.5) * cs, (k + 0.5) * cs), cs)
    out = {"structs": [s.to_dict() for s in structs], "rnotes": list(rnotes)}
    for model in ("lisflood", "anuga"):
        out["edits_" + model] = [(n.line.tolist(), n.width_m, n.invert_up_m, n.invert_dn_m)
                                 for n in sm.structure_edits(structs, cs, model)]
    cut, _, _ = cond.Edits(notches=sm.structure_edits(structs, cs, "lisflood")).apply(g)
    out["dem"] = np.nan_to_num(cut.masked(), nan=-1e9).tolist()
    dem = read((-200.0, -200.0, 200.0, 200.0), 5.0)
    outer = np.array([[-200.0, -200.0], [200.0, -200.0], [200.0, 200.0], [-200.0, 200.0]])
    out["anuga_culverts"] = sm.anuga_culverts(structs, dem, outer)
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
def v3268_a_failure_finding_the_toes_leaves_the_engines_as_they_were():
    """barrel_toes is for the HEC-RAS export only: an exception in it leaves that row without
    a barrel ({'problem': ..}) and every other value of every row, and so everything the
    engines get, as it was; the export then keeps the run's line, with a WARNING."""
    for name in CASES:
        z_at, read, rows, structs, rnotes = _chain(name)
        want = _engine_inputs(read, structs, rnotes)
        real = gp.barrel_toes

        def broken(*a, **k):
            raise ValueError("broken on purpose")
        gp.barrel_toes = broken
        try:
            _, _, rows2, structs2, rnotes2 = _chain(name)
        finally:
            gp.barrel_toes = real
        assert [{k: v for k, v in r.items() if k != "barrel"} for r in rows2] == \
            [{k: v for k, v in r.items() if k != "barrel"} for r in rows], name
        assert "broken on purpose" in rows2[0]["barrel"]["problem"], rows2[0]["barrel"]
        assert _engine_inputs(read, structs2, rnotes2) == want, name
        line, bar, notes = hm.crossing_barrel(structs2[0], rows2[0]["barrel"], _ident,
                                              hm.Units(True), 30.0, SOUTH)
        assert line is None and bar is None and "broken on purpose" in notes[0]


def _chan_bed(y, slope=0.0005, CD=2.0):
    return 100.0 + slope * y - CD


@test
def v3268_the_barrel_ends_lie_where_the_channel_leaves_the_outer_carriageway():
    z_at, _, rows, _, _ = _chain("wide fill")
    b = rows[0]["barrel"]
    assert b["roads"] == 1 and b["status"] == ["channel", "channel"], b
    assert b["clear"] == [True, True]
    (xs, ys_), (xn, yn) = b["ends"]
    assert abs(xs) <= 4.0 and abs(xn) <= 4.0                 # in the 8 m channel
    # the toe of a 7 m fill at 6:1 is 48 m out (the 3.26.8 line stopped at 25 m)
    assert -52.0 <= ys_ <= -45.0 and 45.0 <= yn <= 52.0, b["ends"]
    for (x, y), bed in zip(b["ends"], b["beds_ft"]):
        assert abs(bed * FT - _chan_bed(y)) <= 0.15, (y, bed * FT)
    # a road meeting the crossing line at 60 degrees is not a carriageway
    diag = np.array([[-100.0, 40.0 - 100.0 * math.sqrt(3)], [100.0, 40.0 + 100.0 * math.sqrt(3)]])
    _, _, rows2, _, _ = _chain("wide fill", extra_roads=[diag])
    assert rows2[0]["barrel"] == b
    # the bed at the toe is the channel there, not the lowest ground of the whole walk: on a
    # channel falling 1 % the walk's lowest ground is 0.6 m lower, 60 m further out
    _, _, rows3, _, _ = _chain("", H=4.0, S=3.0, slope=0.01)
    for (x, y), bed in zip(rows3[0]["barrel"]["ends"], rows3[0]["barrel"]["beds_ft"]):
        assert abs(bed * FT - _chan_bed(y, 0.01)) <= 0.15, (y, bed * FT)
    # a fill wider than the run's 60 m reach (12 m over the bed at 6:1, toe 78 m out): the
    # walk goes on (150 m), reading more terrain from the run's reader
    _, _, rows4, _, _ = _chain("", H=10.0, S=6.0)
    b4 = rows4[0]["barrel"]
    assert b4["status"] == ["channel", "channel"], b4
    assert all(75.0 <= abs(e[1]) <= 82.0 for e in b4["ends"]), b4["ends"]
    for (x, y), bed in zip(b4["ends"], b4["beds_ft"]):
        assert abs(bed * FT - _chan_bed(y)) <= 0.15, (y, bed * FT)
    # ... and a toe beyond the walk (26 m over the bed: 162 m out) is 'beyond', not an end
    _, _, rows5, _, _ = _chain("", H=24.0, S=6.0)
    b5 = rows5[0]["barrel"]
    assert b5["status"] == ["beyond", "beyond"] and b5["ends"] == [None, None], b5
    assert b5["clear"] == [False, False]
    # divided highway: both carriageways, the ends beyond the outer ones (y = 26 and -61)
    z_at, _, rows, _, _ = _chain("divided highway")
    b = rows[0]["barrel"]
    assert b["roads"] == 2 and b["stations_m"] == [-35.0, 0.0], b
    (_, ys_), (_, yn) = b["ends"]
    assert -66.0 <= ys_ <= -58.0 and 23.0 <= yn <= 31.0, b["ends"]
    assert b["status"] == ["channel", "channel"]
    for (x, y), bed in zip(b["ends"], b["beds_ft"]):
        assert abs(bed * FT - _chan_bed(y)) <= 0.15, (y, bed * FT)


@test
def v3268_the_barrel_finds_the_channel_off_the_point_skewed_or_bent():
    """Absaraka's symptom: the NBI point 10-20 m along the road from the channel, the channel
    skewed to the road or bent at its toe put the inlet on a field 4-10 ft above the channel.
    The ends are now where the channel leaves the embankment, found along the road either
    side of the point."""
    tan30 = math.tan(math.radians(30.0))
    cases = {
        # the point on the channel's edge or beside it (a band only as wide as the barrels
        # missed it: the inlet up to 0.9 m higher)
        "3 m off": (dict(), dict(x=3.0), lambda y: 0.0),
        "7 m off": (dict(), dict(x=7.0), lambda y: 0.0),
        "15 m off": (dict(), dict(x=15.0), lambda y: 0.0),
        "20 m off": (dict(H=4.0, S=3.0), dict(x=20.0), lambda y: 0.0),
        "skewed 30": (dict(chan_x=lambda Y: tan30 * Y), dict(), lambda y: tan30 * y),
        "skewed 30, 8 m fill": (dict(H=8.0, S=6.0, chan_x=lambda Y: tan30 * Y), dict(),
                                lambda y: tan30 * y),
        "bent at the toe": (dict(H=4.0, S=4.0, chan_x=lambda Y: np.where(Y > 20.0,
                                                                          Y - 20.0, 0.0)),
                            dict(), lambda y: max(y - 20.0, 0.0)),
    }
    for name, (kw, cr, cx) in cases.items():
        _, _, rows, _, _ = _chain("", crossing=dict(CROSSING, **cr), **dict(
            dict(H=5.0, S=6.0), **kw))
        b = rows[0]["barrel"]
        assert b["status"] == ["channel", "channel"], (name, b)
        for (x, y), bed in zip(b["ends"], b["beds_ft"]):
            assert abs(x - cx(y)) <= 4.0 + 3.0, (name, x, y, cx(y))      # in the channel
            assert abs(bed * FT - _chan_bed(y)) <= 0.15, (name, y, bed * FT)
    # no channel within the search: the lowest ground at the toe, 'ground', not 'channel'
    _, _, rows, _, _ = _chain("", H=5.0, S=6.0, CD=0.0)
    assert rows[0]["barrel"]["status"] == ["ground", "ground"], rows[0]["barrel"]


@test
def v3268_no_terrain_before_the_toe_is_not_an_end():
    """No-data or the edge of the DEM just past the toe: the side has no end ('nodata'), so
    the export keeps the run's line as 2a3ca38 did, which leaves this one out (no terrain at
    an end of its line); before, the toe was 'clear' on the crown or the slope, 5-7 m above
    the channel, labelled a channel bed, and turned against the flow."""
    z_at, roads = _embankment(H=5.0, S=6.0)
    u = hm.Units(True)
    for name, mask in (("south half", lambda X, Y: Y < -10.0),
                       ("north beyond 20 m", lambda X, Y: Y > 20.0),
                       ("channel beyond 20 m", lambda X, Y: (np.abs(X) < 5.0) & (np.abs(Y) > 20.0)),
                       ("south beyond 52 m", lambda X, Y: Y < -52.0)):
        read = _reader(z_at, mask=mask)
        rows = gp.blocking_rows([dict(CROSSING)], roads, read, 3.0)
        b = rows[0]["barrel"]
        assert "nodata" in b["status"], (name, b)
        assert all(e is None for e, st in zip(b["ends"], b["status"]) if st == "nodata")
        structs, _ = gp.structures_from(rows, 10.0)
        line, bar, notes = hm.crossing_barrel(structs[0], b, _ident, u, 30.0, SOUTH)
        assert line is None and "no data before the toe" in notes[0], (name, notes)
        G = read((-400.0, -400.0, 400.0, 400.0), 1.0)

        def ground(xy, G=G):
            xy = np.asarray(xy, float).reshape(-1, 2)
            return G.sample_bilinear(xy[:, 0], xy[:, 1])
        per = np.array([[-390.0, -390.0], [390.0, -390.0], [390.0, 390.0], [-390.0, 390.0]])
        run, how = hm.oriented_line(_ident(structs[0].line), SOUTH, u)
        assert run[0][1] > run[-1][1] and "flow path" in how         # turned to run south
        c, why = hm.structure_connection(structs[0], run, ground, per, 30.0, 1.0, u, "C")
        assert c is None and "no terrain at an end" in why, (name, why)


def _ident(a):
    return np.asarray(a, dtype=float).reshape(-1, 2)


def _cell_of(pts, xy):
    return int(np.argmin(((pts - np.asarray(xy, float)) ** 2).sum(axis=1)))


def _cell_min(pts, k, ground, step=0.5):
    """Independent: the lowest ground over the points nearer to pts[k] than to any other (the
    test grids are square: the cell lies within 0.75 x the spacing of its point)."""
    p = pts[k]
    r = 0.75 * float(np.sort(np.hypot(*(pts - p).T))[1])
    g = np.arange(-r, r + 1e-9, step)
    X, Y = np.meshgrid(p[0] + g, p[1] + g)
    xy = np.column_stack([X.ravel(), Y.ravel()])
    near = pts[np.hypot(*(pts - p).T) <= 3.0 * r]
    own = np.argmin(((xy[:, None, :] - near[None, :, :]) ** 2).sum(-1), axis=1) == \
        int(np.argmin(np.hypot(*(near - p).T)))
    return float(np.nanmin(ground(xy[own])))


def _pix_min(pts, k, G):
    """Independent: the lowest terrain pixel of grid G whose centre is in the cell of pts[k]."""
    p = pts[k]
    r = 0.75 * float(np.sort(np.hypot(*(pts - p).T))[1])
    xc, yc = G.x_centers(), G.y_centers()
    cs_ = np.nonzero(np.abs(xc - p[0]) <= r)[0]
    rs_ = np.nonzero(np.abs(yc - p[1]) <= r)[0]
    X, Y = np.meshgrid(xc[cs_], yc[rs_])
    xy = np.column_stack([X.ravel(), Y.ravel()])
    z = G.masked()[np.ix_(rs_, cs_)].ravel()
    near = pts[np.hypot(*(pts - p).T) <= 3.0 * r]
    own = np.argmin(((xy[:, None, :] - near[None, :, :]) ** 2).sum(-1), axis=1) == \
        int(np.argmin(np.hypot(*(near - p).T)))
    return float(np.nanmin(z[own]))


_RUNS = {}


def _export(name, cell, off=0.0, path=SOUTH, crossing=None, **kw):
    """The export's calls for the crossing (build_model's): crossing_barrel ->
    structure_connection -> fit_barrel with the terrain's pixels, on a computation-point grid
    of `cell` (SI, model CRS = run CRS)."""
    u = hm.Units(True)
    key = (name, json.dumps(crossing, sort_keys=True), json.dumps(
        {k: str(v) for k, v in kw.items()}, sort_keys=True))
    if key not in _RUNS:
        z_at, read, rows, structs, _ = _chain(name, crossing=crossing, **kw)
        _RUNS[key] = (rows, structs, read((-400.0, -400.0, 400.0, 400.0), 1.0))
    rows, structs, G = _RUNS[key]
    s, b = structs[0], rows[0]["barrel"]

    def ground(xy):
        xy = np.asarray(xy, float).reshape(-1, 2)
        return G.sample_bilinear(xy[:, 0], xy[:, 1])
    per = np.array([[-390.0, -390.0], [390.0, -390.0], [390.0, 390.0], [-390.0, 390.0]])
    xs = np.arange(-390.0 + (0.5 + off) * cell, 390.0, cell)
    gx, gy = np.meshgrid(xs, xs)
    pts = np.column_stack([gx.ravel(), gy.ravel()])
    line, bar, notes = hm.crossing_barrel(s, b, _ident, u, cell, path)
    assert line is not None, notes
    c, why = hm.structure_connection(s, line, ground, per, cell, 1.0, u, "C", barrel=bar)
    assert why is None, why
    fnotes, why = hm.fit_barrel(c, pts, ground, per, cell, 1.0, u,
                                raster=(G.masked(), G.x0, G.y0, G.dx))
    assert why is None, why
    return b, line, c, notes + c["notes"] + fnotes, pts, ground, G


def _check_barrel(name, b, line, c, notes, pts, ground, G, cell):
    """The barrel's ends at or beyond the ends found, in two cells, its inverts the ground
    there unless raised (with a note) to max(bilinear, pixel-centre) cell minimum + margin."""
    cv = c["culvert"]
    raised = [n for n in notes if "invert was raised" in n]
    E = np.asarray(b["ends"], float)
    iu = int(np.argmin(np.hypot(*(E - np.asarray(line[0])).T)))
    bed_up, bed_dn = b["beds_ft"][iu] * FT, b["beds_ft"][1 - iu] * FT
    for inv, bed, word in ((cv["inv_up"], bed_up, "upstream"),
                           (cv["inv_dn"], bed_dn, "downstream")):
        assert abs(inv - bed) < 0.006 or (inv > bed and any(word in n for n in raised)), \
            (name, inv, bed, notes)
    up, dn = np.asarray(line[0]), np.asarray(line[-1])
    across = np.asarray([0.0, -1.0])
    for pu, pd in hm.barrel_ends(c):
        # each end at or beyond the end found (across the road), on the barrel's line
        assert (np.asarray(pu) - up) @ across <= 1.0 and (np.asarray(pd) - dn) @ across >= -1.0
        ax = (dn - up) / np.hypot(*(dn - up))
        side = 0.5 * (cv["barrels"] - 1) * 1.1 * cv["span"] + 1.0
        for q, e in ((pu, up), (pd, dn)):
            w = np.asarray(q) - e
            assert abs(w[0] * ax[1] - w[1] * ax[0]) <= side, (name, q, e)
        ku, kd = _cell_of(pts, pu), _cell_of(pts, pd)
        assert ku != kd, name
        for inv, k in ((cv["inv_up"], ku), (cv["inv_dn"], kd)):
            lowest = max(_cell_min(pts, k, ground), _pix_min(pts, k, G))
            assert inv >= lowest + hm.BARREL_FIT_MARGIN_FT * FT - 1e-6, (name, cell, inv,
                                                                         lowest)
    assert cv["length"] >= hm.BARREL_MIN_CELLS * cell - 0.01


@test
def v3268_hecras_lays_the_barrel_from_channel_to_channel_with_the_flow():
    assert hm.BED_LABEL == "channel bed beside the road"
    assert hm.GROUND_LABEL == "ground beside the road (no channel found)"
    for name in CASES:
        for cell_ft in (100.0, 200.0):
            for off in (0.0, 0.5):
                cell = cell_ft * FT
                b, line, c, notes, pts, ground, G = _export(name, cell, off)
                cv = c["culvert"]
                # upstream = the north end (the flow path runs south), downstream the south
                assert line[0][1] > 0 > line[-1][1], line
                assert cv["inverts_from"] == "channel bed beside the road / channel bed " \
                    "beside the road"
                _check_barrel(name, b, line, c, notes, pts, ground, G, cell)
                assert "flow path comes from" in notes[0] and "not an inventory" in notes[0]
                assert cv["shift"] == 0.0 or abs(cv["shift"]) < 0.06, cv["shift"]
                if name == "divided highway":
                    assert "beyond the outer of 2 carriageways" in notes[0]
                    # the connection on the downstream carriageway: HEC-RAS centres the barrel
                    # on it, and the upstream end is at the upstream toe, not a median's width
                    # up the channel
                    assert any("crest of the downstream carriageway" in n for n in notes)
                    assert all(pu[1] - line[0][1] <= 0.26 * cell + 1.0
                               for pu, _ in hm.barrel_ends(c)), (hm.barrel_ends(c), line)
    # the channel off the point, skewed or bent (the inlet was on a field 4-10 ft above it):
    # each end in the channel, its invert within 0.5 ft of the channel bed there
    tan30 = math.tan(math.radians(30.0))
    for label, cr, kw, cx in (("15 m off", dict(x=15.0), dict(H=5.0, S=6.0), lambda y: 0.0),
                              ("skewed 30", None, dict(H=5.0, S=6.0, chan_x=lambda Y: tan30 * Y),
                               lambda y: tan30 * y),
                              ("bent at the toe", None, dict(
                                  H=4.0, S=4.0, chan_x=lambda Y: np.where(Y > 20.0, Y - 20.0,
                                                                          0.0)),
                               lambda y: max(y - 20.0, 0.0))):
        for cell_ft in (50.0, 150.0):
            cell = cell_ft * FT
            b, line, c, notes, pts, ground, G = _export(
                "", cell, 0.0, crossing=None if cr is None else dict(CROSSING, **cr), **kw)
            cv = c["culvert"]
            _check_barrel(label, b, line, c, notes, pts, ground, G, cell)
            for inv, e in ((cv["inv_up"], line[0]), (cv["inv_dn"], line[-1])):
                assert abs(e[0] - cx(e[1])) <= 4.0 + 3.0, (label, e)
                assert -0.01 <= inv - _chan_bed(e[1]) <= 0.5 * FT, (label, cell_ft, inv, e)
            if label == "skewed 30":
                assert 25.0 <= math.degrees(math.atan(abs(cv["shift"]))) <= 35.0, cv["shift"]
                up, dn = zip(*hm.barrel_stations(cv))
                assert all(abs(a - b_) > 10.0 for a, b_ in zip(up, dn)), (up, dn)
                assert any("skewed 30 degrees" in n for n in notes), notes


@test
def v3268_the_barrel_turns_with_the_flow_path_and_keeps_inverts_at_their_ends():
    u = hm.Units(True)
    # the run's gap line runs south to north; the inventory's inverts are upstream 96 and
    # downstream 95 (physical ends, whichever end of the line is which)
    s = sm.Structure("culvert", np.array([[0.0, -60.0], [0.0, 60.0]]), 3.0, 1.2, 1,
                     invert_up_m=96.0, invert_dn_m=95.0, name="C")
    b = {"ends": [[0.0, -30.0], [0.0, 30.0]], "beds_ft": [320.0, 321.0],
         "status": ["channel", "channel"], "clear": [True, True], "roads": 1,
         "offsets_m": [0.0, 0.0], "search_m": [40.0, 40.0]}
    north = SOUTH[::-1]
    line, bar, notes = hm.crossing_barrel(s, b, _ident, u, 30.0, SOUTH)
    assert line[0][1] == 30.0 and line[-1][1] == -30.0
    assert bar["inverts"] == [96.0, 95.0] and bar["from"] == ["run", "run"]
    assert bar["across"] == [0.0, -1.0] and "flow path comes from" in notes[0]
    assert not any("lower on its upstream side" in n for n in notes)
    # the flow path decides even where the ground at the toes says otherwise (a ditch or
    # pond upstream lower than the channel downstream, a bend), with a note
    line, bar, notes = hm.crossing_barrel(s, b, _ident, u, 30.0, north)
    assert line[0][1] == -30.0 and bar["inverts"] == [96.0, 95.0]
    assert any("lower on its upstream side ({:.2f} m)".format(320.0 * FT) in n
               for n in notes), notes
    # no flow path: the higher ground at the toe is upstream
    for path, why in ((None, "no flow path"),
                      (np.array([[-500.0, 0.0], [500.0, 0.0]]), "runs along the road"),
                      (SOUTH + [400.0, 0.0], "passes 400 m from it")):
        line, bar, notes = hm.crossing_barrel(s, b, _ident, u, 30.0, path)
        assert line[0][1] == 30.0 and "higher ground at the toe" in notes[0], notes
        assert why in notes[0], (why, notes[0])
    # ... and with the ground the same both sides: the gap line's first end, a WARNING
    b2 = dict(b, beds_ft=[320.0, 320.0])
    line, bar, notes = hm.crossing_barrel(s, b2, _ident, u, 30.0)
    assert line[0][1] == -30.0 and any(n.startswith("WARNING: which way it runs is a guess")
                                       for n in notes), notes
    # an end the inventory does not give (the run's LiDAR check filled it in at an end of its
    # arbitrary line, here the north end's terrain as 'downstream'): the ground at that end
    s2 = sm.Structure("culvert", s.line, 3.0, 1.2, 1, invert_up_m=96.0, invert_dn_m=97.5)
    line, bar, notes = hm.crossing_barrel(s2, dict(b, given_ft=[96.0 / FT, None]), _ident, u,
                                          30.0, SOUTH)
    assert line[0][1] == 30.0 and bar["inverts"] == [96.0, 320.0 * FT], bar
    assert bar["from"] == ["run", "channel bed beside the road"]
    # inventory inverts that rise downstream are kept at their ends, with a note
    s3 = sm.Structure("culvert", s.line, 3.0, 1.2, 1, invert_up_m=95.0, invert_dn_m=96.0)
    line, bar, notes = hm.crossing_barrel(s3, b, _ident, u, 30.0, SOUTH)
    assert bar["inverts"] == [95.0, 96.0] and any("adverse slope" in n for n in notes)
    # no inventory inverts: the channel bed at each end, labelled as such
    s.invert_up_m = s.invert_dn_m = None
    line, bar, notes = hm.crossing_barrel(s, b, _ident, u, 30.0, SOUTH)
    assert np.allclose(bar["inverts"], [321.0 * FT, 320.0 * FT])
    assert bar["from"] == ["channel bed beside the road"] * 2
    assert "not an inventory elevation" in notes[0]
    # no channel on a side: the ground there, labelled so, with a WARNING
    b4 = dict(b, status=["ground", "channel"])
    line, bar, notes = hm.crossing_barrel(s, b4, _ident, u, 30.0, SOUTH)
    assert bar["from"] == ["channel bed beside the road",
                           "ground beside the road (no channel found)"]
    assert any(n.startswith("WARNING: no channel was found beside the road on its downstream")
               for n in notes), notes
    # no end on a side: nothing to lay, and why
    for st, why in ((["beyond", "channel"], "still falling 150 m from the road"),
                    (["nodata", "channel"], "no data before the toe")):
        b5 = dict(b, ends=[None, [0.0, 30.0]], status=st, walk_m=150.0)
        line, bar, notes = hm.crossing_barrel(s, b5, _ident, u, 30.0, SOUTH)
        assert line is None and bar is None and why in notes[0], notes
    assert hm.crossing_barrel(s, {"problem": "X failed"}, _ident, u, 30.0)[2] == ["X failed"]
    assert "older form" in hm.crossing_barrel(s, {"ends": b["ends"], "clear": [True, True]},
                                              _ident, u, 30.0)[2][0]
    # a run before 3.26.8: its rows have no barrel, so its culverts keep the run's line
    rep = {"crossing_gaps": {"cut": True, "rows": [{"line": s.line.tolist()}]}}
    got = hm.barrel_of(s, hm.crossing_barrels(rep))
    assert got == {"problem": hm.PRE_3268}, got
    assert hm.crossing_barrel(s, got, _ident, u, 30.0)[2] == [hm.PRE_3268]
    # with the crossings only reported, nothing
    assert hm.crossing_barrels({"crossing_gaps": {"cut": False, "rows": rep["crossing_gaps"]
                                                  ["rows"]}}) == []


def _grid30():
    per = np.array([[-300.0, -300.0], [300.0, -300.0], [300.0, 300.0], [-300.0, 300.0]])
    xs = np.arange(-285.0, 300.0, 30.0)
    gx, gy = np.meshgrid(xs, xs)
    return per, np.column_stack([gx.ravel(), gy.ravel()])


@test
def v3268_a_barrel_invert_below_its_cell_is_raised_and_a_short_barrel_lengthened():
    u = hm.Units(True)
    per, pts = _grid30()

    def ground(xy):
        xy = np.asarray(xy, float).reshape(-1, 2)
        return 100.0 + 0.0 * xy[:, 0]                     # flat ground at 100 m
    # a connection across a culvert running north, inverts 1 m below the cells, barrel 6 m
    c = {"name": "C", "line": [(-25.0, 15.0), (55.0, 15.0)],
         "culvert": {"length": 6.0, "stations": [40.0], "inv_up": 99.0, "inv_dn": 99.0}}
    notes, why = hm.fit_barrel(c, pts, ground, per, 30.0, 1.0, u)
    assert why is None
    cv = c["culvert"]
    ku, kd = (_cell_of(pts, q) for q in hm.barrel_ends(c)[0])
    assert ku != kd and cv["length"] == 36.0, cv                  # 6 m: one cell, 2 steps
    assert any("lengthened" in n and "different 2D cells" in n for n in notes), notes
    # no longer for cells that never reach the inverts (it was up to 4 cells more)
    assert not any("reach nearer" in n for n in notes), notes
    want = round(math.ceil((100.0 + 0.05 * FT) * 100.0) / 100.0, 2)
    assert cv["inv_up"] == cv["inv_dn"] == want, cv
    assert sum("invert was raised from 99.00 to {:.2f}".format(want) in n for n in notes) == 2
    assert all("do not reach the ground the invert was found on" in n
               for n in notes if "invert was raised" in n)
    # the lowest ground of a cell is at its low corner, not at its point: ground falling 1 %
    # east, the cell of (15, 15) is 15 m either side, lowest at x = 30 (read between pixels;
    # without the pixels, the ground 1.5 pixels inside the cell bounds the pixels' lowest)
    k = _cell_of(pts, (15.0, 15.0))

    def sloped(xy):
        xy = np.asarray(xy, float).reshape(-1, 2)
        return 100.0 - 0.01 * xy[:, 0]
    assert 99.7 - 1e-6 <= hm.cell_min_ground(pts, k, sloped, per, 1.0) <= 99.7 + 0.02
    # ... and inside the 2D area only: a cell cut by the perimeter at x = 20
    cut = np.array([[-300.0, -300.0], [20.0, -300.0], [20.0, 300.0], [-300.0, 300.0]])
    assert 99.8 - 1e-6 <= hm.cell_min_ground(pts, k, sloped, cut, 1.0) <= 99.8 + 0.02
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
def v3268_each_barrel_end_fits_its_own_cell_and_the_terrains_pixels():
    u = hm.Units(True)
    per, pts = _grid30()

    def sloped(xy):
        xy = np.asarray(xy, float).reshape(-1, 2)
        return 100.0 - 0.01 * xy[:, 0]
    # two barrels either side of the cell boundary at x = 30 (barrel 1 at x = 32, barrel 2 at
    # x = 28): each invert at least the higher of the two barrels' cells (99.40 and 99.70)
    c = {"name": "C", "line": [(55.0, 15.0), (-25.0, 15.0)],
         "culvert": {"length": 40.0, "stations": [23.0, 27.0], "inv_up": 99.0, "inv_dn": 99.0}}
    notes, why = hm.fit_barrel(c, pts, sloped, per, 30.0, 1.0, u)
    assert why is None
    cv = c["culvert"]
    xs_ = sorted(round(pu[0]) for pu, _ in hm.barrel_ends(c))
    assert xs_ == [28, 32], xs_
    for pu, pd in hm.barrel_ends(c):
        for inv, q in ((cv["inv_up"], pu), (cv["inv_dn"], pd)):
            assert inv >= _cell_min(pts, _cell_of(pts, q), sloped) + 0.05 * FT - 1e-6, (q, inv)
    assert cv["inv_up"] >= 99.70 + 0.05 * FT and cv["inv_dn"] >= 99.70 + 0.05 * FT, cv
    # a sharp step on a cell edge (a channel wall at x = 30, the cell's pixels all 100 m, the
    # next cell's 98 m): between the pixels the ground reads 99 m at the edge, the pixels in
    # the cell 100 m. The invert goes to the pixels' lowest, which HEC-RAS may take
    a = np.where((np.arange(600) - 300 + 0.5)[None, :] > 30.0, 98.0, 100.0) * np.ones((600, 1))
    G = Grid(a, -300.0, 300.0, 1.0)

    def ground(xy):
        xy = np.asarray(xy, float).reshape(-1, 2)
        return G.sample_bilinear(xy[:, 0], xy[:, 1])
    assert abs(float(ground([[30.0, 15.0]])[0]) - 99.0) < 1e-6
    k = _cell_of(pts, (15.0, 15.0))
    raster = (G.masked(), G.x0, G.y0, G.dx)
    assert abs(hm.cell_min_ground(pts, k, ground, per, 1.0, raster=raster) - 100.0) < 1e-6
    assert abs(hm.cell_min_ground(pts, k, ground, per, 1.0) - 100.0) < 1e-6
    c = {"name": "C", "line": [(-25.0, 10.0), (55.0, 10.0)],
         "culvert": {"length": 60.0, "stations": [40.0], "inv_up": 99.0, "inv_dn": 99.0}}
    notes, why = hm.fit_barrel(c, pts, ground, per, 30.0, 1.0, u, raster=raster)
    assert why is None and c["culvert"]["inv_up"] >= 100.0 + 0.05 * FT - 1e-9, c["culvert"]
    assert c["culvert"]["cell_min"] == [100.0, 100.0]


@test
def v3268_each_barrel_end_moves_onto_the_lowest_terrain_pixel_near_it():
    """10 m terrain pixels: the channel is one row of pixels (98 m) in a field (100 m). An end
    the run found 6 m off that row (in its own finer read) is in a 2D cell whose pixels miss
    the channel, and its invert would be raised 2 m to the field: it is moved onto the
    channel's pixel. An end with no lower pixel near it stays where it is."""
    u = hm.Units(True)
    a = np.full((60, 60), 100.0)
    a[30, :] = 98.0                          # pixel centres at x, y = -300 + 10 j, 300 - 10 i
    raster = (a, -305.0, 305.0, 10.0)
    line, moved, zs = hm.snap_barrel_ends([[0.0, 6.0], [40.0, -3.0]], raster, u)
    assert np.allclose(line, [[0.0, 0.0], [40.0, 0.0]]) and np.allclose(moved, [6.0, 3.0])
    assert zs == [98.0, 98.0]
    line, moved, zs = hm.snap_barrel_ends([[0.0, 20.0], [41.0, -60.0]], raster, u)
    assert np.allclose(line, [[0.0, 20.0], [40.0, -60.0]]) and np.allclose(moved, [0.0, 1.0])
    assert hm.snap_barrel_ends([[0.0, 6.0], [1.0, 1.0]], None, u)[1:] == ([0.0, 0.0],
                                                                         [None, None])
    # the invert the run found there (its 3 m read fills the channel up a little) goes down to
    # the model's pixel; the inventory's stays as it is
    bar = {"inverts": [98.4, 98.1], "from": [hm.BED_LABEL, "run"], "across": [0.0, -1.0]}
    line, bar2, notes = hm.place_barrel([[0.0, 6.0], [40.0, -3.0]], bar, raster, u)
    assert bar2["inverts"] == [98.0, 98.1] and bar["inverts"] == [98.4, 98.1]
    assert len(notes) == 1 and "upstream invert is the lowest ground of the model's" in notes[0]


@test
def v3268_a_barrel_is_lengthened_only_where_that_reaches_the_channel():
    u = hm.Units(True)
    per, pts = _grid30()

    def channel_at(d, low=98.0):
        def ground(xy):
            xy = np.asarray(xy, float).reshape(-1, 2)
            return np.where(np.abs(xy[:, 1]) >= d, low, 101.0)
        return ground
    # the cell at the upstream end (the south one here) is all embankment; half a cell
    # further each way (a cell in all) its end is in the channel's cell
    c = {"name": "C", "line": [(-25.0, 10.0), (55.0, 10.0)],
         "culvert": {"length": 60.0, "stations": [40.0], "inv_up": 98.0, "inv_dn": 98.0}}
    notes, why = hm.fit_barrel(c, pts, channel_at(40.0), per, 30.0, 1.0, u)
    assert why is None and c["culvert"]["length"] == 90.0, (c["culvert"], notes)
    assert any("lengthened from 60 to 90 m" in n and "upstream end 101.00 to 98.00 m" in n
               for n in notes), notes
    assert c["culvert"]["inv_up"] < 98.1 and c["culvert"]["inv_dn"] < 98.1, c["culvert"]
    # the channel further out than a cell more would reach: not lengthened (it was, by up
    # to 4 cells: a 217 ft toe-to-toe barrel became 1,520 ft), the inverts raised instead
    c = {"name": "C", "line": [(-25.0, 10.0), (55.0, 10.0)],
         "culvert": {"length": 60.0, "stations": [40.0], "inv_up": 98.0, "inv_dn": 98.0}}
    notes, why = hm.fit_barrel(c, pts, channel_at(100.0), per, 30.0, 1.0, u)
    assert why is None and c["culvert"]["length"] == 60.0, c["culvert"]
    assert not any("lengthened" in n for n in notes), notes
    assert c["culvert"]["inv_up"] > 101.0 and c["culvert"]["inv_dn"] > 101.0
    # ... nor where the cell a cell further out is only a little lower (0.05 m, under 0.25 ft)
    c = {"name": "C", "line": [(-25.0, 10.0), (55.0, 10.0)],
         "culvert": {"length": 60.0, "stations": [40.0], "inv_up": 98.0, "inv_dn": 98.0}}
    notes, why = hm.fit_barrel(c, pts, channel_at(40.0, 100.95), per, 30.0, 1.0, u)
    assert why is None and c["culvert"]["length"] == 60.0, (c["culvert"], notes)
    assert not any("lengthened" in n for n in notes), notes


@test
def v3268_a_skewed_barrel_has_its_own_upstream_and_downstream_stations():
    """A channel crossing the road at 30 degrees: the connection line runs along the road
    over the crest, the barrel from the channel on one side to the other, its upstream and
    downstream centreline stations apart along the line, so that HEC-RAS lays its ends in the
    channel (and the cells there are the channel's)."""
    u = hm.Units(True)
    tan30 = math.tan(math.radians(30.0))
    z_at, _ = _embankment(H=4.0, S=3.0, chan_x=lambda Y: -tan30 * Y)

    def ground(xy):
        xy = np.asarray(xy, float).reshape(-1, 2)
        return z_at(xy[:, 0], xy[:, 1])
    per = np.array([[-300.0, -300.0], [300.0, -300.0], [300.0, 300.0], [-300.0, 300.0]])
    s = sm.Structure("culvert", np.array([[0.0, 60.0], [0.0, -60.0]]), 3.0, 1.2, 1, name="C")
    line = np.array([[-tan30 * 30.0, 30.0], [tan30 * 30.0, -30.0]])
    bar = {"inverts": [98.015, 97.985], "from": [hm.BED_LABEL] * 2, "across": [0.0, -1.0]}
    c, why = hm.structure_connection(s, line, ground, per, 30.0, 0.5, u, "C", barrel=bar)
    assert why is None, why
    A, B = np.asarray(c["line"][0]), np.asarray(c["line"][-1])
    assert abs(A[1] - B[1]) < 1e-6 and abs(c["crest"][1]) < 1.0     # along the road crest
    cv = c["culvert"]
    assert abs(cv["shift"] + tan30) < 0.01 or abs(cv["shift"] - tan30) < 0.01, cv["shift"]
    assert abs(cv["length"] - 60.0) < 0.6, cv["length"]           # toe to toe across the road
    (pu, pd), = hm.barrel_ends(c)
    assert np.hypot(*(np.asarray(pu) - line[0])) < 1.0 and \
        np.hypot(*(np.asarray(pd) - line[1])) < 1.0, (pu, pd, line)
    (s_up, s_dn), = hm.barrel_stations(cv)
    assert abs(abs(s_up - s_dn) - 2.0 * tan30 * 30.0) < 0.5, (s_up, s_dn)
    g = hm.structure_block(c, __import__("datetime").datetime(2026, 1, 1))
    i = next(j for j, ln in enumerate(g) if ln.startswith("Connection Culv="))
    vals = [float(v) for v in g[i + 1].split()]
    assert np.allclose(vals, [s_up, s_dn], atol=0.01), (g[i + 1], s_up, s_dn)
    assert any("skewed 30 degrees" in n for n in c["notes"]), c["notes"]
    # a channel along the road (more than 60 degrees): not laid
    flat = np.array([[-60.0, 10.0], [60.0, -10.0]])
    c, why = hm.structure_connection(s, flat, ground, per, 30.0, 0.5, u, "C", barrel=bar)
    assert c is None and "more than 60" in why, why


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
def v3268_the_hecras_export_lays_a_divided_highways_culvert_channel_to_channel():
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
        assert b["roads"] == 2 and b["status"] == ["channel", "channel"], b
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
        assert cv["length"] >= toe_len - 0.01 and cv["length"] >= m.cell - 0.01
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
        assert "beyond the outer of 2 carriageways" in rd
        assert "flow path comes from" in rd or "higher ground at the toe" in rd
        assert "'channel bed beside the road' the channel bed where the channel leaves the " \
            "road embankment" in rd
        assert "not an inventory elevation" in rd and "WARNING" not in rd
        # a run made before 3.26.8 (its rows have no barrel): the run's line, as before, its
        # inverts the terrain at the line's ends, and a WARNING that says so
        barrels0 = [json.loads(json.dumps(row["barrel"]))
                    for row in rep["crossing_gaps"]["rows"]]
        for row in rep["crossing_gaps"]["rows"]:
            row.pop("barrel", None)
        with open(p, "w") as f:
            json.dump(rep, f)
        r = hm.build_model(dfold, os.path.join(d, "out_old"))
        c = r["model"].structures[0]
        assert c["culvert"]["inverts_from"] == "terrain / terrain", c["culvert"]
        assert "shift" not in c["culvert"]
        assert abs(c["culvert"]["length"] - 120.0 / FT) < 0.5, c["culvert"]["length"]
        with open(r["paths"]["readme"], encoding="utf-8") as fh:
            rd = fh.read()
        assert "WARNING: a culvert the run cut as a gap through the road: its barrel could " \
            "not be laid" in rd and hm.PRE_3268 in rd, rd
        # a barrel that cannot be exported (its ends would make it cross the road at more
        # than 60 degrees): the run's line, as before, with a WARNING that says why
        for row, bb in zip(rep["crossing_gaps"]["rows"], barrels0):
            bb["ends"][1] = [bb["ends"][1][0], bb["ends"][1][1] + 200.0]
            row["barrel"] = bb
        with open(p, "w") as f:
            json.dump(rep, f)
        r = hm.build_model(dfold, os.path.join(d, "out_skew"))
        c = r["model"].structures[0]
        assert c["culvert"]["inverts_from"] == "terrain / terrain", c["culvert"]
        assert any(n.startswith("WARNING: a culvert the run cut as a gap through the road") and
                   "more than 60" in n for n in c["notes"]), c["notes"]
    finally:
        shutil.rmtree(d)
