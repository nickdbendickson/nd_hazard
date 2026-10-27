"""Crossings the bare-earth DEM turns into a dam (3.20.0).

Feature 14 of the review: take the NBI bridges and the surveyed crossings of the offline pack
into the built-in engine's terrain as gaps in embankments, the way the HEC-RAS export treats
culverts.

What this does and does not do, and why
--------------------------------------
The machinery to cut a gap already exists and is not reinvented here: a
`structures.Structure` of kind bridge / notch is cut through the embankment in the model DEM
for LISFLOOD-FP and the built-in engine alike (structures.structure_edits), and a culvert keeps
its roof for ANUGA's culvert operator. What was missing is the step BEFORE that: knowing that a
crossing is there at all, and with what opening. So this module

  * reports, for every inventory crossing inside the flood corridor, how high the model's
    embankment stands above the channel there - "this road blocks flow by 8 ft at an NBI bridge"
    is the sentence the engineer needs, and it needs no hydraulics to be true;
  * builds the Structure objects for the crossings whose opening is actually surveyed (a span
    and a rise), so they can be cut - but only when the engineer turns it on.

Cutting them is NOT the default, and that is a deliberate choice about conservatism rather than
about effort. Opening a gap in an embankment lets water through: the pool behind the road falls
and the water downstream of it arrives sooner and deeper. The first of those is a result moving
in the LESS conservative direction for anybody upstream of the road, and this suite's rule is
that a release never does that on its own. A crossing with a guessed opening is worse again: an
8 ft box culvert invented for a 60 ft bridge changes both numbers and nobody can tell from the
output. So:

  * a crossing with a surveyed span and rise can be cut, with the option on;
  * a crossing without one is reported, never cut, exactly as culvert candidates already are
    (structures.culvert_candidates, 3.13.0);
  * the run report lists every blocking embankment at a known crossing whether the option is on
    or off, so a run that did not cut them still says what it did not do.

    rows = blocking_rows(crossings, roads, read_dem, cell_m)
    structs, notes = structures_from(rows, cell_m)       # only the surveyed ones

3.26.8: each row also carries row["barrel"] (barrel_toes: where the channel leaves the road
embankment on each side, beyond the outermost carriageway, searched along the road either
side of the crossing point, and the channel bed there), which only the HEC-RAS export uses,
to lay a culvert's barrel from the channel on one side to the other where both sides found
it (hecras_model.crossing_barrel; else the export keeps the run's line). The models cut the
row's whole gap line, as before, and a failure in barrel_toes leaves only that row without
a barrel.

`crossings` are plain dicts, as the caller reads them from the dam's data layers
(damdata roles "bridges" = NBI_bridges and "crossings" = FRA grade crossings) or from the
engineer's own surveyed layer:
    {"name", "x", "y", "kind" ("bridge" / "culvert"), "span_ft", "rise_ft", "barrels",
     "shape", "invert_up_ft", "invert_dn_ft", "source"}
x / y are in the work CRS, like everything the workflow handles.

Pure numpy; no QGIS.
"""
import math

import numpy as np

from . import structures as structmod
from .units import FT

MATCH_M = 40.0            # a crossing further than this from a road line is not on it
CORRIDOR_M = 25.0         # half-width of the road corridor the crown is taken from
REACH_M = 60.0            # how far either side of the road the channel bed is looked for
MIN_SPAN_FT = 1.0         # a smaller opening is not a crossing we can cut
# 3.26.8 (HEC-RAS export only, barrel_toes): a culvert barrel runs from the channel where it
# leaves the road embankment on one side to the other. Beyond the outermost carriageway (a
# road line within CARRIAGEWAY_ANGLE_DEG of the crossing's road), out to TOE_WALK_M, the toe
# is the first ground TOE_TOL_FT below the road and within TOE_TOL_FT of the lowest ground
# CLEAR_M beyond it (terrain all the way); its bed the lowest ground from there to TOE_BED_M
# further out. Toes are found every grid cell along the road, TOE_SEARCH_M either side of
# the crossing line (more on a wide fill, up to TOE_SEARCH_MAX_M), and the channel is the
# valley of them (at least CHANNEL_DEPTH_FT deep) nearest the line, a toe TOE_NEAR_SLOPE
# per metre further from the road counting higher. Only such a valley is a channel: without
# one (a field, a cross-slope along the road), or with a column next to it that has no
# terrain, is still falling at the end of the walk or meets another road (a road line, or a
# bend of this one, more than CARRIAGEWAY_ANGLE_DEG to this one, within CLEAR_M of the walk
# from the carriageway to CLEAR_M beyond its toe: that toe can be on the other road's fill),
# the side has no channel and the export keeps the run's line. A toe whose ground rises
# MERGE_RISE_M again within CLEAR_M up to a further road line is the median of a divided
# highway (up to MAX_MERGES more carriageways). The barrel's end is the lowest terrain cell
# from the toe to TOE_BED_M beyond it and either side, a cell END_NEAR_SLOPE per metre further
# out counting higher. The terrain is read SEARCH_PAD_M beyond the area searched.
TOE_TOL_FT = 1.0
TOE_BED_M = 5.0
CARRIAGEWAY_ANGLE_DEG = 30.0
CLEAR_M = 10.0
TOE_WALK_M = 150.0
TOE_SEARCH_M = 40.0
TOE_SEARCH_MAX_M = 100.0
TOE_NEAR_SLOPE = 0.05
END_NEAR_SLOPE = 0.01
CHANNEL_DEPTH_FT = 1.0
SEARCH_PAD_M = 30.0
MERGE_RISE_M = 1.0
MAX_MERGES = 2
BLOCK_NOTE = ("A crossing that the terrain closes: bare-earth LiDAR leaves the road "
              "embankment as a solid wall where the bridge or culvert opening is. The model "
              "treats it as ground unless the crossing is given as a structure.")
NOT_CUT = ("Not cut: opening the embankment lowers the water behind it, which this release "
           "does not do on its own. Turn on 'Cut surveyed crossings as gaps' to cut the ones "
           "whose span and rise are surveyed.")


# 3.26.7: the NBI's own field names and codes (the offline pack's NBI_bridges layer, as the
# DamSafety Runner's culvert_guess.inventory_record reads them). Item 43B = 19 is a culvert,
# every other main structure type a bridge; item 48 (maximum span) is in metres; item 45 (main
# unit spans) is the number of barrels for a culvert. The NBI has no culvert rise.
NBI_CULVERT_43B = "19"
INVENTORY_MATCH_M = 75.0  # a culvert candidate this close to an inventory crossing is that one


def kind_from(value, field=""):
    """'culvert' / 'bridge' from a kind attribute: an NBI item 43B code (the field is
    STRUCTURE_TYPE_043B, or the value is a one- or two-digit code) is a culvert only for 19;
    otherwise 'bridge' in the text is a bridge, any other text a culvert, no text a bridge."""
    txt = str(value if value is not None else "").strip().strip("'").lower()
    if "043b" in str(field or "").lower() or (txt.isdigit() and len(txt) <= 2):
        if not txt:
            return structmod.KIND_BRIDGE
        return (structmod.KIND_CULVERT if txt.lstrip("0") == NBI_CULVERT_43B
                else structmod.KIND_BRIDGE)
    if "bridge" in txt:
        return structmod.KIND_BRIDGE
    return structmod.KIND_CULVERT if txt else structmod.KIND_BRIDGE


def is_metres_field(field):
    """True for a field the NBI writes in metres (MAX_SPAN_LEN_MT_048, ..._MT_...)."""
    words = str(field or "").lower().replace("-", "_").split("_")
    return any(w in ("mt", "m", "metre", "metres", "meter", "meters") for w in words[1:])


def length_ft(value, field=""):
    """A span / rise attribute in feet; metres fields (is_metres_field) are converted."""
    v = _num(value)
    if v is None:
        return None
    return round(v / FT, 2) if is_metres_field(field) else v


def assume_rise(crossings, rise_ft):
    """3.26.7: give the inventory culverts that have a span and no rise (every NBI culvert)
    the engineer's rise, so they can be cut as gaps. Off (rise_ft 0 / None) by default: the
    size is then half guessed, which is why the crossing's source says so, the row carries
    rise_assumed, and blocking_rows keeps the rise below the embankment's own height.
    Returns how many crossings took it."""
    r = _num(rise_ft)
    if not r or r <= 0:
        return 0
    n = 0
    for c in crossings or []:
        if (str(c.get("kind") or "") == structmod.KIND_CULVERT and _num(c.get("span_ft"))
                and not _num(c.get("rise_ft"))):
            c["rise_ft"] = float(r)
            c["rise_assumed"] = True
            c["source"] = "{} (rise {:g} ft assumed)".format(c.get("source") or "inventory",
                                                            float(r))
            n += 1
    return n


def inventory_near(candidates, crossings, match_m=INVENTORY_MATCH_M):
    """3.26.7 (as the DamSafety Runner's culvert_guess.match_inventory, report only): each
    culvert candidate (a road across the flow path) gets the nearest inventory crossing within
    match_m - its name, kind, span, rise and barrels - so the candidate list says "NBI culvert,
    2 x 12 ft" where there is one. An inventory crossing names at most one candidate. Nothing
    is put in the model from this. Returns how many candidates were named."""
    pairs = []
    for ci, c in enumerate(candidates or []):
        for xi, x in enumerate(crossings or []):
            if x.get("x") is None or x.get("y") is None:
                continue
            d = math.hypot(float(x["x"]) - float(c["x"]), float(x["y"]) - float(c["y"]))
            if d <= match_m:
                pairs.append((d, ci, xi))
    pairs.sort()
    used_c, used_x = set(), set()
    for d, ci, xi in pairs:
        if ci in used_c or xi in used_x:
            continue
        x = crossings[xi]
        candidates[ci]["inventory"] = {
            "name": str(x.get("name") or ""), "kind": str(x.get("kind") or ""),
            "source": str(x.get("source") or ""), "distance_m": round(d, 1),
            "span_ft": _num(x.get("span_ft")),
            "rise_ft": None if x.get("rise_assumed") else _num(x.get("rise_ft")),
            "barrels": int(_num(x.get("barrels")) or 1)}
        used_c.add(ci)
        used_x.add(xi)
    return len(used_c)


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _nearest_on_line(line, x, y):
    """(distance, (px, py), (ux, uy)) - nearest point on a polyline and its unit direction."""
    pts = np.asarray(line, dtype=float)
    if len(pts) < 2:
        return None
    best = None
    for a, b in zip(pts[:-1], pts[1:]):
        v = b - a
        n2 = float(v[0] ** 2 + v[1] ** 2)
        if n2 <= 0.0:
            continue
        t = max(0.0, min(1.0, float(((x - a[0]) * v[0] + (y - a[1]) * v[1]) / n2)))
        p = a + t * v
        d = math.hypot(x - p[0], y - p[1])
        if best is None or d < best[0]:
            n = math.sqrt(n2)
            best = (d, (float(p[0]), float(p[1])), (float(v[0] / n), float(v[1] / n)))
    return best


def match_to_roads(crossing, roads, match_m=MATCH_M):
    """(point on the road, direction along it, distance) for a crossing, or None.

    The gap has to be cut ACROSS the embankment, so the road's own direction at the crossing is
    what decides the line; a gap cut at a guessed angle would miss the channel."""
    x, y = _num(crossing.get("x")), _num(crossing.get("y"))
    if x is None or y is None:
        return None
    best = None
    for line in roads or []:
        got = _nearest_on_line(line, x, y)
        if got is None:
            continue
        if best is None or got[0] < best[0]:
            best = got
    if best is None or best[0] > match_m:
        return None
    return {"point": best[1], "direction": best[2], "distance_m": best[0]}


def gap_line(point, direction, length_m):
    """The line to cut: across the road through `point`, `length_m` long (upstream end first
    is decided by the caller; for a gap the two ends are interchangeable)."""
    ux, uy = direction
    nx, ny = -uy, ux                       # perpendicular to the road
    half = 0.5 * float(length_m)
    return np.array([[point[0] - nx * half, point[1] - ny * half],
                     [point[0] + nx * half, point[1] + ny * half]], dtype=float)


def _crown_and_bed(grid, point, direction, corridor_m=CORRIDOR_M, reach_m=REACH_M):
    """(crown, bed) metres: the highest ground along the road at the crossing, and the lowest
    ground within `reach_m` either side of it off the road."""
    z = grid.masked()
    X, Y = np.meshgrid(grid.x_centers(), grid.y_centers())
    ux, uy = direction
    along = (X - point[0]) * ux + (Y - point[1]) * uy
    across = (X - point[0]) * (-uy) + (Y - point[1]) * ux
    on_road = (np.abs(along) <= corridor_m) & (np.abs(across) <= 0.5 * corridor_m) & \
        np.isfinite(z)
    off_road = (np.abs(along) <= corridor_m) & (np.abs(across) > 0.5 * corridor_m) & \
        (np.abs(across) <= reach_m) & np.isfinite(z)
    crown = float(np.nanmax(z[on_road])) if on_road.any() else None
    bed = float(np.nanmin(z[off_road])) if off_road.any() else None
    return crown, bed


def _road_segments(roads, direction, max_angle_deg=CARRIAGEWAY_ANGLE_DEG, box=None,
                   side=False):
    """3.26.8: (road index, segment starts, segment ends) of the road lines' segments that run
    with the crossing's road (within max_angle_deg of `direction`): the carriageways a barrel
    passes under. A road meeting the crossing line end-on is not one. With `box` (xmin, ymin,
    xmax, ymax), only the segments that reach into it (the area searched). With `side`, the
    other segments instead: the roads that meet or cross this one."""
    cos_min = math.cos(math.radians(max_angle_deg))
    ids, C, D = [], [], []
    for ri, road in enumerate(roads or []):
        try:
            pts = np.asarray(road, dtype=float)
        except (TypeError, ValueError):
            continue
        if pts.ndim != 2 or len(pts) < 2 or pts.shape[1] < 2:
            continue
        pts = pts[:, :2]
        v = pts[1:] - pts[:-1]
        nv = np.hypot(v[:, 0], v[:, 1])
        with np.errstate(invalid="ignore", divide="ignore"):
            along = np.abs(v[:, 0] * direction[0] + v[:, 1] * direction[1]) / \
                np.where(nv > 0.0, nv, 1.0) >= cos_min
            keep = (nv > 0.0) & (~along if side else along)
        keep &= np.isfinite(pts[:-1]).all(axis=1) & np.isfinite(pts[1:]).all(axis=1)
        if box is not None:
            with np.errstate(invalid="ignore"):
                keep &= ((np.maximum(pts[:-1, 0], pts[1:, 0]) >= box[0]) &
                         (np.minimum(pts[:-1, 0], pts[1:, 0]) <= box[2]) &
                         (np.maximum(pts[:-1, 1], pts[1:, 1]) >= box[1]) &
                         (np.minimum(pts[:-1, 1], pts[1:, 1]) <= box[3]))
        k = np.nonzero(keep)[0]
        ids += [ri] * len(k)
        C.append(pts[:-1][k])
        D.append(pts[1:][k])
    if not ids:
        return np.zeros(0, int), np.zeros((0, 2)), np.zeros((0, 2))
    return np.asarray(ids, int), np.vstack(C), np.vstack(D)


def _crossings(p0, p1, segs, only=None):
    """3.26.8: [(metres from p0 along p0-p1, road index)] where the segment p0-p1 crosses the
    road segments `segs` (_road_segments), only those of the roads in `only` when given."""
    ids, C, D = segs
    if only is not None:
        k = np.isin(ids, np.asarray(sorted(only), int))
        ids, C, D = ids[k], C[k], D[k]
    if not len(ids):
        return []
    p0 = np.asarray(p0, float)
    r = np.asarray(p1, float) - p0
    sv = D - C
    den = r[0] * sv[:, 1] - r[1] * sv[:, 0]
    ok = np.abs(den) > 1e-12
    den = np.where(ok, den, 1.0)
    q = C - p0[None, :]
    t = (q[:, 0] * sv[:, 1] - q[:, 1] * sv[:, 0]) / den
    w = (q[:, 0] * r[1] - q[:, 1] * r[0]) / den
    hit = ok & (t >= -1e-9) & (t <= 1.0 + 1e-9) & (w >= -1e-9) & (w <= 1.0 + 1e-9)
    n = float(np.hypot(*r))
    return [(float(t[i]) * n, int(ids[i])) for i in np.nonzero(hit)[0]]


def _seg_distance(p0, p1, C, D):
    """3.26.8: the shortest distance from the segment p0-p1 to each segment C[i]-D[i]."""
    p0, p1 = np.asarray(p0, float), np.asarray(p1, float)

    def to_seg(q, a, b):                               # points q to segments a-b
        ab = b - a
        L2 = (ab ** 2).sum(axis=-1)
        t = np.clip(((q - a) * ab).sum(axis=-1) / np.where(L2 > 0.0, L2, 1.0), 0.0, 1.0)
        w = a + ab * t[..., None] - q
        return np.hypot(w[..., 0], w[..., 1])
    m = len(C)
    out = np.minimum.reduce([to_seg(np.broadcast_to(p0, (m, 2)), C, D),
                             to_seg(np.broadcast_to(p1, (m, 2)), C, D),
                             to_seg(C, np.broadcast_to(p0, (m, 2)), np.broadcast_to(p1, (m, 2))),
                             to_seg(D, np.broadcast_to(p0, (m, 2)), np.broadcast_to(p1, (m, 2)))])
    r, sv = p1 - p0, D - C
    den = r[0] * sv[:, 1] - r[1] * sv[:, 0]
    ok = np.abs(den) > 1e-12
    q = C - p0[None, :]
    with np.errstate(invalid="ignore", divide="ignore"):
        t = (q[:, 0] * sv[:, 1] - q[:, 1] * sv[:, 0]) / np.where(ok, den, 1.0)
        w = (q[:, 0] * r[1] - q[:, 1] * r[0]) / np.where(ok, den, 1.0)
    out[ok & (t >= 0.0) & (t <= 1.0) & (w >= 0.0) & (w <= 1.0)] = 0.0
    return out


def _search_grid(have, read_dem, corners, cell):
    """3.26.8: the terrain barrel_toes searches, kept in `have` ({"grid": the row's grid at
    first}): that grid when it covers the box of `corners`, else read_dem(that box, cell)
    (the run's own reader), which then replaces it; the grid it has if the read fails (and
    no more reads after that)."""
    corners = np.asarray(corners, float).reshape(-1, 2)
    need = (float(corners[:, 0].min()) - 2.0 * cell, float(corners[:, 1].min()) - 2.0 * cell,
            float(corners[:, 0].max()) + 2.0 * cell, float(corners[:, 1].max()) + 2.0 * cell)
    grid = have["grid"]
    gb = grid.bounds
    if read_dem is None or have.get("failed") or (
            gb[0] <= need[0] and gb[1] <= need[1] and gb[2] >= need[2] and gb[3] >= need[3]):
        return grid
    try:
        g = read_dem(need, float(cell))
    except Exception:                                   # noqa - the grid it has will do
        g = None
    if g is None or not float(getattr(g, "dx", 0.0) or 0.0) > 0.0:
        have["failed"] = True
        return grid
    have["grid"] = g
    return g


def _column_toes(Z, tol, clear, bed_rows):
    """3.26.8: the toe of the embankment down each column of Z (rows = stations every grid
    cell outward from the outermost carriageway, row 0 on it): the first station `tol` below
    the road at the top of the column and within `tol` of the lowest ground from it to
    `clear` rows (CLEAR_M) beyond it, with terrain all the way there; in a column that never
    gets `tol` below its road (a road at grade), the first station within `tol` of that
    lowest ground. Returns (status per column: 'ok', 'nodata' (no terrain before a toe was
    found) or 'beyond' (still falling at the end of the walk), toe row, bed: the lowest
    ground from the toe to `bed_rows` (TOE_BED_M) beyond it)."""
    nd, nv = Z.shape
    status = np.array(["beyond"] * nv, dtype=object)
    toe = np.full(nv, -1, int)
    bed = np.full(nv, np.nan)
    fin = np.isfinite(Z)
    status[~fin.all(axis=0)] = "nodata"
    if nd <= clear:
        return status, toe, bed
    with np.errstate(invalid="ignore"):
        wmin = np.lib.stride_tricks.sliding_window_view(Z, clear + 1, axis=0).min(axis=-1)
        prefix = np.cumprod(fin, axis=0).astype(bool)
        zi = Z[:nd - clear]
        near = np.isfinite(wmin) & prefix[clear:] & (zi <= wmin + tol)
        desc = zi <= Z[0][None, :] - tol
    for j in range(nv):
        if not fin[0, j]:
            continue
        cand = np.nonzero(near[:, j] & desc[:, j])[0]
        if not len(cand) and float(Z[0, j]) - float(np.min(Z[prefix[:, j], j])) < tol:
            cand = np.nonzero(near[:, j])[0]              # a road at grade
        if not len(cand):
            continue
        i = int(cand[0])
        toe[j] = i
        bed[j] = float(np.min(Z[i:i + bed_rows + 1, j]))
        status[j] = "ok"
    return status, toe, bed


def _pick_channel(status, toe, bed, d, v):
    """3.26.8: the column of the channel among the toes found across the road (`v` m along it
    from the gap line): the valley (toes at least CHANNEL_DEPTH_FT below the toes on both
    sides of it) nearest the gap line, and in it the lowest toe, a toe further from the road
    counting TOE_NEAR_SLOPE per metre higher (so that the toe is where the channel leaves the
    embankment, not further down the channel); without a valley, the lowest toe (not a
    channel: barrel_toes calls that side 'ground'). Equal toes: the one nearest the gap line.
    Returns (column or None, valley found, 'nodata' / 'road' / 'beyond' / None: a column
    without terrain, on another road or still falling next to that valley, or anywhere
    without one)."""
    ok = status == "ok"

    def unknown(sl):
        st = status[sl]
        for word in ("nodata", "road", "beyond"):
            if (st == word).any():
                return word
        return None
    if not ok.any():
        return None, False, unknown(slice(None)) or "nodata"
    cost = np.full(len(v), np.nan)
    cost[ok] = bed[ok] + TOE_NEAR_SLOPE * d[toe[ok]]
    low = np.where(ok, cost, -np.inf)
    left = np.maximum.accumulate(low)
    right = np.maximum.accumulate(low[::-1])[::-1]
    inval = ok & (np.minimum(left, right) - np.where(ok, cost, np.inf) >=
                  CHANNEL_DEPTH_FT * FT)
    runs, j = [], 0
    while j < len(v):
        if inval[j]:
            k = j
            while k + 1 < len(v) and inval[k + 1]:
                k += 1
            runs.append((j, k))
            j = k + 1
        else:
            j += 1

    def lowest(js):
        js = np.asarray(js, int)
        cmin = float(np.min(cost[js]))
        tie = js[cost[js] <= cmin + 0.02]
        return int(tie[np.argmin(np.abs(v[tie]))])
    if runs:
        def dist(r):
            a, b = float(v[r[0]]), float(v[r[1]])
            return (0.0 if a <= 0.0 <= b else min(abs(a), abs(b)),
                    float(np.min(cost[r[0]:r[1] + 1])))
        r = min(runs, key=dist)
        return (lowest(range(r[0], r[1] + 1)), True,
                unknown(slice(max(0, r[0] - 2), r[1] + 3)))
    return lowest(np.nonzero(ok)[0]), False, unknown(slice(None))


def _toe_low(g, toe, bed, outward, nrm):
    """3.26.8: the barrel's end at a toe: the lowest terrain cell (its own value, as the
    LiDAR has it, not a value between cells) from the toe point to TOE_BED_M beyond it and
    TOE_BED_M either side (the middle of a channel that crosses the road at a skew, a ditch
    or scour hole at the toe), a cell END_NEAR_SLOPE per metre further out counting higher
    (on a falling channel the end stays at the toe). Returns (xy, elevation); the toe and
    `bed` without a cell there."""
    toe = np.asarray(toe, float)
    h = float(g.dx)
    r = TOE_BED_M + h
    c0 = max(0, int(math.floor((toe[0] - r - g.x0) / h)))
    c1 = min(g.nx, int(math.ceil((toe[0] + r - g.x0) / h)) + 1)
    r0 = max(0, int(math.floor((g.y0 - toe[1] - r) / h)))
    r1 = min(g.ny, int(math.ceil((g.y0 - toe[1] + r) / h)) + 1)
    if c1 <= c0 or r1 <= r0:
        return toe, bed
    X, Y = np.meshgrid(g.x_centers()[c0:c1], g.y_centers()[r0:r1])
    z = g.masked()[r0:r1, c0:c1]
    al = (X - toe[0]) * outward[0] + (Y - toe[1]) * outward[1]
    ac = (X - toe[0]) * nrm[0] + (Y - toe[1]) * nrm[1]
    sel = (al >= -0.5 * h) & (al <= TOE_BED_M + 0.5 * h) & \
        (np.abs(ac) <= TOE_BED_M + 0.5 * h) & np.isfinite(z)
    if not sel.any():
        return toe, bed
    cost = np.where(sel, z + END_NEAR_SLOPE * np.maximum(al, 0.0), np.inf)
    k = np.unravel_index(int(np.argmin(cost)), cost.shape)
    return np.array([X[k], Y[k]]), float(z[k])


def barrel_toes(grid, line, roads, direction, width_m=0.0, reach_m=REACH_M,
                tol_m=TOE_TOL_FT * FT, read_dem=None):
    """3.26.8, for the HEC-RAS export only: where the channel leaves the road embankment on
    each side of a crossing, and its bed there, so that a culvert barrel can run from the
    channel on one side to the channel on the other.

    The carriageways are the crossing's road and every road line running with it (within
    CARRIAGEWAY_ANGLE_DEG) that the gap line crosses (both carriageways of a divided
    highway), and a further one whose embankment the toe found on a side runs into (the
    ground rising MERGE_RISE_M within CLEAR_M beyond the toe, up to a road line). Across the
    road, a column every grid cell along it up to the search width either side of the gap
    line (TOE_SEARCH_M, wider for a wide fill, at least half the barrels' `width_m`) runs
    outward from the outermost carriageway, TOE_WALK_M (at least `reach_m`), to the toe of
    the embankment (_column_toes, with `tol_m`). The channel is the valley of those toes
    nearest the gap line (_pick_channel): the NBI point can be off the channel, and the
    channel skewed to the road or bent at it; the barrel then follows it. The terrain is
    the row's `grid`, or `read_dem` of the area searched where the grid does not cover it.

    Each side is 'channel' (a valley of the toes: positive evidence of the channel there, the
    only status the HEC-RAS export lays a barrel to), 'ground' (no valley within the search:
    the end is the lowest ground at the toe, which can be a field at the edge of the search;
    not a channel), 'nodata' (no terrain before a toe was found, next to the valley or,
    without one, anywhere searched), 'road' (a column next to the valley, or without one
    anywhere searched, meets another road: a road line, or a bend of this one, more than
    CARRIAGEWAY_ANGLE_DEG to this one and on this side of the carriageway, within CLEAR_M of
    the walk from the carriageway to CLEAR_M beyond the toe, so the toe can be on that
    road's fill) or 'beyond' (still falling at the end of the walk). Returns {"ends": [[x,
    y] or None, ..] (the side of the gap line's first end, then of its second end),
    "beds_ft": [ft or None, ..] (the lowest ground from the toe to TOE_BED_M beyond it),
    "status": [.., ..], "clear": [an end was found, ..], "offsets_m": [along the road from
    the gap line, ..], "toes_m": [from the outermost carriageway, ..], "search_m": [half
    width, ..], "roads": carriageways, "stations_m": [first side, second side] (the
    outermost carriageway on each side, m from the line's middle), "walk_m"}; None for a
    line of no length."""
    line = np.asarray(line, dtype=float)
    a, b = line[0], line[-1]
    total = float(np.hypot(*(b - a)))
    if total <= 0.0:
        return None
    u = (b - a) / total
    nrm = np.array([-u[1], u[0]])
    mid = 0.5 * (a + b)
    h = float(grid.dx)
    clear = int(math.ceil(CLEAR_M / h - 1e-9))
    bed_rows = int(math.ceil(TOE_BED_M / h - 1e-9))
    R = max(float(reach_m), TOE_WALK_M)
    d = h * np.arange(int(math.ceil(R / h - 1e-9)) + 1, dtype=float)
    ring = SEARCH_PAD_M + h
    # only the road segments that reach into the area searched (a road layer can be large)
    far = 0.5 * total + R + max(TOE_SEARCH_MAX_M, 0.5 * float(width_m or 0.0)) + 2.0 * ring
    box = (mid[0] - far, mid[1] - far, mid[0] + far, mid[1] + far)
    segs = _road_segments(roads, direction, box=box)
    # the roads that meet or cross this one (a junction, or a bend of this one: segments more
    # than CARRIAGEWAY_ANGLE_DEG to it): a toe near one is not evidence of the channel (it can
    # be on that road's fill)
    sides = _road_segments(roads, direction, box=box, side=True)
    # the carriageways the gap line crosses (the crossing's own road at its middle)
    gap = {}
    for t, ri in _crossings(a, b, segs):
        gap.setdefault(ri, []).append(t - 0.5 * total)
    if not gap:                         # the line's middle is on its road, at a vertex
        for ri in sorted(set(segs[0].tolist())):
            try:
                got = _nearest_on_line(roads[ri], mid[0], mid[1])
            except (TypeError, ValueError, IndexError):
                got = None
            if got is not None and got[0] < 1e-3:
                gap[ri] = [0.0]
    used_all = set(gap)
    out = {"ends": [], "beds_ft": [], "status": [], "clear": [], "offsets_m": [],
           "toes_m": [], "search_m": [], "roads": 1, "stations_m": [], "walk_m": round(R, 1)}
    have = {"grid": grid}

    def walk(side, used, v, read=read_dem):
        st = [0.0] + [t for ts in used.values() for t in ts]
        edge0 = max(st) if side > 0 else min(st)
        span = (min(st) - float(np.max(np.abs(v))), max(st) + float(np.max(np.abs(v))))
        # each column's outermost carriageway on this side (a curved road moves along it)
        edge = np.full(len(v), np.nan)
        for j, vj in enumerate(v):
            base = mid + vj * nrm
            hits = [span[0] + t for t, _ in
                    _crossings(base + span[0] * u, base + span[1] * u, segs, used)]
            if vj == 0.0:
                hits.append(edge0)
            if hits:
                edge[j] = max(hits) if side > 0 else min(hits)
        lo = float(np.nanmin(edge)) - (R + ring if side < 0 else ring)
        hi = float(np.nanmax(edge)) + (R + ring if side > 0 else ring)
        wv = float(np.max(np.abs(v))) + ring
        g = _search_grid(have, read, [mid + s_ * u + w * nrm for s_ in (lo, hi)
                                      for w in (-wv, wv)], h)
        S = edge[None, :] + side * d[:, None]
        P = mid[None, None, :] + S[..., None] * u[None, None, :] + \
            v[None, :, None] * nrm[None, None, :]
        with np.errstate(invalid="ignore"):
            Z = g.sample_bilinear(np.nan_to_num(P[..., 0], nan=1e30),
                                  np.nan_to_num(P[..., 1], nan=1e30))
        status, toe, bed = _column_toes(Z, tol_m, clear, bed_rows)
        status[~np.isfinite(edge)] = "off"
        # a road that meets or crosses this one (or a bend of this one) on this side within
        # CLEAR_M of the walk from the carriageway to CLEAR_M beyond the toe: 'road', not a
        # toe of this embankment (that road's fill can cover the channel there)
        if len(sides[0]):
            C, D = sides[1], sides[2]
            pc, pd = (C - mid) @ u, (D - mid) @ u
            beyond = np.maximum(side * (pc - edge0), side * (pd - edge0)) > 0.0
            C, D, pc, pd = C[beyond], D[beyond], pc[beyond], pd[beyond]
            # only the part beyond the carriageway (the line through edge0 along the road):
            # a road meeting this one on the other side is not on this side's fill
            fc = np.where(side * (pc - edge0) < 0.0, (edge0 - pc) / np.where(
                pd != pc, pd - pc, 1.0), 0.0)
            fd = np.where(side * (pd - edge0) < 0.0, (edge0 - pd) / np.where(
                pc != pd, pc - pd, 1.0), 0.0)
            C, D = C + fc[:, None] * (D - C), D + fd[:, None] * (C - D)
            for j in np.nonzero(status == "ok")[0] if len(C) else []:
                k = min(int(toe[j]) + clear, len(d) - 1)
                if float(np.min(_seg_distance(P[0, j], P[k, j], C, D))) < CLEAR_M:
                    status[j] = "road"
        return g, P, Z, edge, edge0, status, toe, bed

    # the search width each side: wide enough for a channel skewed up to about 60 degrees to
    # reach the toe within it (from the toe on the gap line, in the row's own grid; the toe
    # of a channel that crosses at a skew lies further out than the field's)
    widths = []
    for side in (-1.0, 1.0):
        _, _, _, _, edge0, st0, toe0, _ = walk(side, gap, np.zeros(1), None)
        far = abs(edge0) + float(d[toe0[0]]) if st0[0] in ("ok", "road") else None
        widths.append(min(TOE_SEARCH_MAX_M, max(
            TOE_SEARCH_M, TOE_SEARCH_MAX_M if far is None else 20.0 + 2.5 * far,
            0.5 * float(width_m or 0.0) + h)))
    # one read of the terrain for the whole search, unless the row's grid covers it
    st = [0.0] + [t for ts in gap.values() for t in ts]
    wv = max(widths) + 2.0 * ring
    _search_grid(have, read_dem, [mid + s_ * u + w * nrm for s_ in (min(st) - R - 2.0 * ring,
                                                                     max(st) + R + 2.0 * ring)
                                  for w in (-wv, wv)], h)
    for side, W in zip((-1.0, 1.0), widths):
        used = {ri: list(ts) for ri, ts in gap.items()}
        n = int(math.ceil(W / h - 1e-9))
        v = h * np.arange(-n, n + 1, dtype=float)
        for it in range(MAX_MERGES + 1):
            g, P, Z, edge, edge0, status, toe, bed = walk(side, used, v)
            pick, valley, unknown = _pick_channel(status, toe, bed, d, v)
            if pick is None or unknown or it == MAX_MERGES:
                break
            # a further carriageway whose embankment the toe runs into (a divided highway
            # wider than the gap line): the ground rises again beyond the toe, up to a road
            i, j = int(toe[pick]), pick
            if float(np.max(Z[i:i + clear + 1, j])) < bed[j] + MERGE_RISE_M:
                break
            nxt = sorted((t, ri) for t, ri in _crossings(P[i, j], P[i, j] + side * R * u, segs)
                         if ri not in used)
            if not nxt:
                break
            t, ri = nxt[0]
            k = min(len(d) - 1, i + int(round(t / h)))
            between = Z[i:k + 1, j]
            if not np.isfinite(between).all() or float(np.min(between)) < bed[j] - tol_m or \
                    float(between[-1]) < bed[j] + MERGE_RISE_M:
                break
            used[ri] = [float(edge[j] + side * (d[i] + t))]
            used_all.add(ri)
        out["stations_m"].append(round(float(edge0), 2))
        out["search_m"].append(round(float(W), 1))
        if pick is None or unknown:
            for k in ("ends", "beds_ft", "offsets_m", "toes_m"):
                out[k].append(None)
            out["status"].append(unknown or "nodata")
            out["clear"].append(False)
            continue
        i = int(toe[pick])
        end, zb = _toe_low(g, P[i, pick], float(bed[pick]), side * u, nrm)
        out["ends"].append([round(float(end[0]), 3), round(float(end[1]), 3)])
        out["beds_ft"].append(round(zb / FT, 2))
        out["status"].append("channel" if valley else "ground")
        out["clear"].append(True)
        out["offsets_m"].append(round(float((end - mid) @ nrm), 1))
        out["toes_m"].append(round(float(d[i]), 1))
    out["roads"] = int(max(1, len(used_all)))
    return out


def blocking_rows(crossings, roads, read_dem, cell_m, match_m=MATCH_M,
                  corridor_m=CORRIDOR_M, reach_m=REACH_M):
    """One row per crossing: where it is, how much the terrain blocks it, and whether its
    opening is surveyed well enough to cut.

    `read_dem(bounds, cell)` returns a Grid of the model's terrain (the workflow's own reader).
    A crossing that is not on a road line, or has no DEM, is still reported - with the reason -
    because "we could not check this crossing" is itself worth reading."""
    rows = []
    for c in crossings or []:
        name = str(c.get("name") or c.get("id") or "crossing").strip()
        kind = str(c.get("kind") or structmod.KIND_BRIDGE).strip().lower()
        if kind not in structmod.KINDS:
            kind = structmod.KIND_BRIDGE
        span = _num(c.get("span_ft")) or _num(c.get("width_ft"))
        rise = _num(c.get("rise_ft")) or _num(c.get("height_ft"))
        row = {"name": name, "kind": kind, "source": str(c.get("source") or ""),
               "x": _num(c.get("x")), "y": _num(c.get("y")),
               "span_ft": span, "rise_ft": rise,
               "barrels": int(_num(c.get("barrels")) or 1),
               "shape": str(c.get("shape") or "box"),
               "invert_up_ft": _num(c.get("invert_up_ft")),
               "invert_dn_ft": _num(c.get("invert_dn_ft")),
               "blocks_ft": None, "crown_ft": None, "bed_ft": None,
               "on_road": False, "surveyed": bool(span and span >= MIN_SPAN_FT and rise),
               "rise_assumed": bool(c.get("rise_assumed")), "reason": ""}
        match = match_to_roads(c, roads, match_m)
        if match is None:
            row["reason"] = ("no road or railroad line within {:.0f} ft of it, so the "
                             "embankment could not be measured".format(match_m / FT))
            rows.append(row)
            continue
        row["on_road"] = True
        row["road_distance_ft"] = round(match["distance_m"] / FT, 1)
        length = max(4.0 * float(cell_m), 2.0 * reach_m)
        line = gap_line(match["point"], match["direction"], length)
        pad = reach_m + 2.0 * float(cell_m)
        bounds = (float(min(line[:, 0])) - pad, float(min(line[:, 1])) - pad,
                  float(max(line[:, 0])) + pad, float(max(line[:, 1])) + pad)
        try:
            grid = read_dem(bounds, float(cell_m))
        except Exception as e:                                   # noqa - one crossing
            row["reason"] = "no terrain here ({}: {})".format(type(e).__name__, e)
            rows.append(row)
            continue
        crown, bed = _crown_and_bed(grid, match["point"], match["direction"], corridor_m,
                                    reach_m)
        if crown is None or bed is None:
            row["reason"] = "the terrain around it is no-data"
            rows.append(row)
            continue
        row["crown_ft"] = round(crown / FT, 2)
        row["bed_ft"] = round(bed / FT, 2)
        row["blocks_ft"] = round(max(0.0, crown - bed) / FT, 2)
        if row["rise_assumed"] and rise and row["blocks_ft"] and rise > row["blocks_ft"]:
            # 3.26.7: an assumed rise never cuts deeper than the embankment stands
            row["rise_ft"] = row["blocks_ft"]
            row["surveyed"] = row["surveyed"] and bool(row["rise_ft"])
        # 3.26.8: the toes and channel beds either side, for the HEC-RAS export's culvert
        # barrel only (the models cut the gap line, as before). Never lets a row down: a
        # failure leaves this row without a barrel (the export says so) and every other
        # value of every row as it was
        try:
            row["barrel"] = barrel_toes(
                grid, line, roads, match["direction"],
                float(span or 0.0) * FT * row["barrels"] * 1.1, reach_m, read_dem=read_dem)
        except Exception as e:                                   # noqa - HEC-RAS only
            row["barrel"] = {"problem": "the toes of the embankment were not found ({}: {})"
                             .format(type(e).__name__, e)}
        row["line"] = line.tolist()
        row["point"] = list(match["point"])
        row["direction"] = list(match["direction"])
        if not row["surveyed"]:
            row["reason"] = ("no surveyed span and rise, so the opening is unknown: reported "
                             "only, never modelled (a guessed size moves the depths both ways)")
        rows.append(row)
    rows.sort(key=lambda r: (-(r.get("blocks_ft") or 0.0), r["name"]))
    return rows


def blocking(rows, min_ft=1.0):
    """The rows where the terrain really does close a known crossing."""
    return [r for r in rows or [] if (r.get("blocks_ft") or 0.0) >= min_ft]


def structures_from(rows, cell_m, kind_override=""):
    """([structures.Structure], notes) for the crossings whose opening is surveyed.

    The Structure carries the surveyed span and rise, so the gap that is cut is the opening that
    was measured; the existing code does the cutting (structures.structure_edits for
    LISFLOOD-FP and the built-in engine, the culvert operator for ANUGA) and the existing
    invert reconciliation checks the elevations against the LiDAR
    (structures.reconcile_inverts). Nothing new is invented about the hydraulics."""
    out, notes = [], []
    for r in rows or []:
        if not r.get("surveyed") or not r.get("line"):
            if r.get("blocks_ft"):
                notes.append("{}: {}".format(r["name"], r.get("reason") or
                                             "not cut (no surveyed opening)"))
            continue
        span_m = float(r["span_ft"]) * FT
        rise_m = float(r["rise_ft"]) * FT
        kind = kind_override or r.get("kind") or structmod.KIND_BRIDGE
        # 3.26.8: the models cut the whole gap line with the inventory's inverts only, as in
        # 3.26.7. The barrel the HEC-RAS export lays from toe to toe (row["barrel"]) is not
        # used here: a 50 m line with the beds as inverts left the toe of a wide fill and a
        # second carriageway standing in LISFLOOD-FP, the built-in engine and ANUGA.
        s = structmod.Structure(
            kind=kind, line=np.asarray(r["line"], dtype=float), width_m=span_m,
            height_m=rise_m, barrels=max(1, int(r.get("barrels") or 1)),
            shape=str(r.get("shape") or "box"),
            invert_up_m=(float(r["invert_up_ft"]) * FT
                         if r.get("invert_up_ft") is not None else None),
            invert_dn_m=(float(r["invert_dn_ft"]) * FT
                         if r.get("invert_dn_ft") is not None else None),
            name="{} ({})".format(r["name"], r.get("source") or "surveyed crossing"))
        out.append(s)
        notes.append("{}: {:.0f} ft x {:.1f} ft {} cut as a gap in the embankment ({} blocked "
                     "{} ft)".format(r["name"], r["span_ft"], r["rise_ft"], kind,
                                     "the terrain", r.get("blocks_ft")))
    return out, notes


def markdown_lines(rows, cut=False, title="Road and railroad crossings as embankment gaps"):
    """The run report section (always written, whether the gaps were cut or not)."""
    lines = ["## " + title, ""]
    if not rows:
        lines += ["- No inventory crossing (NBI bridge, grade crossing, surveyed culvert) was "
                  "found near this flood's roads.", ""]
        return lines
    blocked = blocking(rows)
    lines.append("- " + BLOCK_NOTE)
    lines.append("- {} crossing(s) checked; {} where the terrain blocks the opening by 1 ft or "
                 "more; {} with a surveyed span and rise.".format(
                     len(rows), len(blocked), sum(1 for r in rows if r.get("surveyed"))))
    lines.append("- " + ("Surveyed crossings were cut as gaps in this run." if cut
                         else NOT_CUT))
    lines.append("")
    lines.append("| Crossing | Kind | Blocks (ft) | Span x rise (ft) | Cut | Note |")
    lines.append("| --- | --- | --- | --- | --- | --- |")
    for r in rows[:40]:
        lines.append("| {} | {} | {} | {} | {} | {} |".format(
            r["name"], r["kind"],
            "" if r.get("blocks_ft") is None else r["blocks_ft"],
            ("{:g} x {:g}{}".format(r["span_ft"], r["rise_ft"],
                                     " (rise assumed)" if r.get("rise_assumed") else "")
             if r.get("surveyed") else
             "span {:g}, no rise".format(r["span_ft"]) if r.get("span_ft")
             else "not surveyed"),
            "yes" if (cut and r.get("surveyed")) else "no", r.get("reason") or ""))
    if len(rows) > 40:
        lines.append("| ... {} more | | | | | |".format(len(rows) - 40))
    lines.append("")
    return lines
