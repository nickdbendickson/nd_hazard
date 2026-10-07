"""A proposed embankment drawn as a crest line, on the pre-dam ground.

The source DEM is never modified. `insert_embankment` returns a new array: the crest is
held at the design elevation and the faces fall at the given slopes until they meet
natural ground. Nothing is excavated.

Dam height is the design crest minus the lowest natural ground on the line (the
streambed). The breach point is that place. Storage is measured by filling the ground
behind the embankment stage by stage — LiDAR here is the real bed, not a hydro-flattened
pool — and a rim lower than the crest, where the pool would leave the basin, is reported.

`PHRASE` is the sentence every report carries.
"""
import math

import numpy as np

try:
    from scipy import ndimage as ndi
except Exception:  # pragma: no cover
    ndi = None

PHRASE = "proposed — embankment inserted from the drawn line"
FT = 0.3048
AF_M3 = 1233.48183754752
AC_M2 = 4046.8564224
_NODATA = -9998.0
_CROSS = np.array([[0, 1, 0], [1, 1, 1], [0, 1, 0]], dtype=int)


class ProposedError(ValueError):
    """The line, the crest or the ground cannot make a dam. The message says what to change."""


def _clean(dem):
    """Finite ground, metres. A -9999 fill that was not declared no-data is no-data."""
    z = np.array(dem, dtype=float, copy=True)
    z[z <= _NODATA] = np.nan
    return z


def _centers(gt, shape):
    ny, nx = shape
    xs = gt[0] + (np.arange(nx) + 0.5) * gt[1]
    ys = gt[3] + (np.arange(ny) + 0.5) * gt[5]
    return np.meshgrid(xs, ys)


def _rc(gt, x, y, shape):
    c = int(math.floor((x - gt[0]) / gt[1]))
    r = int(math.floor((y - gt[3]) / gt[5]))
    if 0 <= r < shape[0] and 0 <= c < shape[1]:
        return r, c
    return None


def polyline_geom(px, py, line):
    """Distance, signed distance (positive = left of the travel direction) and along-line
    distance. `px` and `py` are arrays of any shape; `line` is (N, 2)."""
    line = np.asarray(line, dtype=float)
    px = np.asarray(px, dtype=float)
    py = np.asarray(py, dtype=float)
    dist = np.full(px.shape, np.inf)
    signed = np.zeros(px.shape)
    along = np.zeros(px.shape)
    s0 = 0.0
    for i in range(len(line) - 1):
        ax, ay = line[i]
        bx, by = line[i + 1]
        dx, dy = bx - ax, by - ay
        seg2 = dx * dx + dy * dy
        if seg2 == 0.0:
            continue
        seg = math.sqrt(seg2)
        t = np.clip(((px - ax) * dx + (py - ay) * dy) / seg2, 0.0, 1.0)
        qx, qy = ax + t * dx, ay + t * dy
        d = np.hypot(px - qx, py - qy)
        cross = dx * (py - ay) - dy * (px - ax)
        better = d < dist
        dist = np.where(better, d, dist)
        signed = np.where(better, np.where(cross >= 0.0, d, -d), signed)
        along = np.where(better, s0 + t * seg, along)
        s0 += seg
    return dist, signed, along


def line_length(line):
    line = np.asarray(line, dtype=float)
    if len(line) < 2:
        return 0.0
    return float(np.hypot(np.diff(line[:, 0]), np.diff(line[:, 1])).sum())


def _sample(z, gt, x, y):
    rc = _rc(gt, x, y, z.shape)
    if rc is None:
        return None
    v = z[rc]
    return None if not np.isfinite(v) else float(v)


def densify(line, spacing):
    line = np.asarray(line, dtype=float)
    if len(line) < 2:
        return line.copy()
    seg = np.hypot(np.diff(line[:, 0]), np.diff(line[:, 1]))
    s = np.concatenate([[0.0], np.cumsum(seg)])
    if s[-1] <= 0:
        return line[:1].copy()
    n = max(2, int(math.ceil(s[-1] / max(spacing, 1e-3))) + 1)
    ss = np.linspace(0.0, s[-1], n)
    return np.column_stack([np.interp(ss, s, line[:, 0]), np.interp(ss, s, line[:, 1])])


def side_of_point(line, xy):
    """'left' or 'right' of the drawn direction, from which side `xy` is on."""
    _d, signed, _a = polyline_geom(np.array([xy[0]]), np.array([xy[1]]), line)
    return "left" if float(signed[0]) >= 0.0 else "right"


def _bearing(dx, dy):
    return math.degrees(math.atan2(dx, dy)) % 360.0


def _tangent(samples, i):
    if len(samples) < 2:
        return (1.0, 0.0)
    j = min(max(i, 1), len(samples) - 1)
    dx = samples[j, 0] - samples[j - 1, 0]
    dy = samples[j, 1] - samples[j - 1, 1]
    n = math.hypot(dx, dy) or 1.0
    return dx / n, dy / n


def _check_inputs(line, crest_m, crest_width_m, us_h, ds_h, upstream):
    line = np.asarray(line, dtype=float)
    if line.ndim != 2 or line.shape[0] < 2 or line.shape[1] < 2:
        raise ProposedError("The crest line needs at least two points.")
    if not np.isfinite(line[:, :2]).all():
        raise ProposedError("The crest line has a point with no coordinates.")
    if line_length(line[:, :2]) < 1.0:
        raise ProposedError("The crest line is shorter than 1 m.")
    if not np.isfinite(crest_m):
        raise ProposedError("The design crest elevation is not a number.")
    if crest_width_m < 0 or not np.isfinite(crest_width_m):
        raise ProposedError("The crest width cannot be negative.")
    if not (us_h > 0 and ds_h > 0 and np.isfinite(us_h) and np.isfinite(ds_h)):
        raise ProposedError("The upstream and downstream slopes must be steeper than flat "
                            "(horizontal run per 1 vertical, greater than 0).")
    if upstream not in ("left", "right"):
        raise ProposedError("The reservoir side is 'left' or 'right' of the drawn direction.")
    return line[:, :2]


def insert_embankment(dem, gt, line, crest_m, crest_width_m, us_h, ds_h, upstream="left",
                      measure=True):
    """A new DEM (the array passed in is not changed) with the embankment on it.

    Faces are `us_h` and `ds_h` horizontal per 1 vertical. A cell is raised only where the
    design surface is above natural ground, so the toe dies out on the hillside and high
    ground on the line is left as it is.
    """
    line = _check_inputs(line, crest_m, crest_width_m, us_h, ds_h, upstream)
    natural = _clean(dem)
    X, Y = _centers(gt, natural.shape)
    _dist, signed, _along = polyline_geom(X, Y, line)
    d_up = signed if upstream == "left" else -signed
    half = 0.5 * float(crest_width_m)
    cell = abs(float(gt[1]))
    z_des = np.full(natural.shape, -np.inf)
    on_crest = np.abs(d_up) <= half + 1e-6
    up_face = d_up > half
    dn_face = d_up < -half
    z_des[on_crest] = crest_m
    z_des[up_face] = crest_m - (d_up[up_face] - half) / float(us_h)
    z_des[dn_face] = crest_m - (-d_up[dn_face] - half) / float(ds_h)
    # the face has met the ground where the design surface has fallen to it or below
    raise_it = np.isfinite(natural) & np.isfinite(z_des) & (z_des > natural + 1e-6) & (z_des <= crest_m + 1e-6)
    out = natural.copy()
    out[raise_it] = z_des[raise_it]

    samples = densify(line, max(cell, 1.0))
    ground = np.array([_sample(natural, gt, x, y) for x, y in samples], dtype=float)
    finite = np.isfinite(ground)
    if not finite.any():
        if measure:
            raise ProposedError(
                "The crest line does not cross any DEM cell with a ground elevation.")
        bed, height, pick = float(crest_m), 0.0, len(samples) // 2
    else:
        bed_i = int(np.nanargmin(ground))
        bed = float(ground[bed_i])
        height = float(crest_m) - bed
        if measure and height < 0.3:
            raise ProposedError(
                "The design crest ({:.2f} ft) is only {:.2f} ft above the lowest ground on the "
                "line ({:.2f} ft). A breach needs the crest at least about 1 ft above that "
                "point.".format(crest_m / FT, height / FT, bed / FT))
        # among cells essentially at the thalweg, take the one nearest the middle of the
        # below-crest length so a flat channel does not pin the breach to one abutment
        near = finite & (ground <= bed + max(0.05, 0.25 * cell))
        below = finite & (ground < crest_m)
        if below.any():
            mid = float(np.median(np.flatnonzero(below)))
        else:
            mid = 0.5 * (len(samples) - 1)
        pick = int(np.flatnonzero(near)[np.argmin(np.abs(np.flatnonzero(near) - mid))])
    breach_xy = (float(samples[pick, 0]), float(samples[pick, 1]))
    tx, ty = _tangent(samples, pick)
    # left of travel is (-ty, tx)
    ux, uy = (-ty, tx) if upstream == "left" else (ty, -tx)
    dx, dy = -ux, -uy
    held = finite & (ground < crest_m)
    if held.any() and len(samples) > 1:
        seg = np.hypot(np.diff(samples[:, 0]), np.diff(samples[:, 1]))
        crest_len = float(seg[held[:-1] | held[1:]].sum()) if seg.size else 0.0
    else:
        crest_len = 0.0
    abut = finite & (ground >= crest_m)
    abut_len = 0.0
    if abut.any() and len(samples) > 1:
        seg = np.hypot(np.diff(samples[:, 0]), np.diff(samples[:, 1]))
        abut_len = float(seg[abut[:-1] | abut[1:]].sum()) if seg.size else 0.0

    return {
        "dem": out,
        "natural": natural,
        "raised": raise_it,
        "d_up": d_up,
        "breach_xy": breach_xy,
        "breach_rc": _rc(gt, breach_xy[0], breach_xy[1], out.shape),
        "bed_m": bed,
        "height_m": height,
        "crest_m": float(crest_m),
        "crest_width_m": float(crest_width_m),
        "us_h": float(us_h),
        "ds_h": float(ds_h),
        "upstream": upstream,
        "upstream_vec": (ux, uy),
        "crest_len_m": crest_len,
        "drawn_len_m": line_length(line),
        "abutment_m": abut_len,
        "flow_az": _bearing(dx, dy),
        "dam_az": _bearing(tx, ty),
        "n_raised": int(raise_it.sum()),
        "line": line,
    }


def _label_wet(below, seed):
    if ndi is None:
        raise ProposedError("scipy is required to fill the reservoir behind the embankment.")
    if not below[seed]:
        return np.zeros(below.shape, dtype=bool)
    lab, _n = ndi.label(below, structure=_CROSS)
    mark = lab[seed]
    if mark == 0:
        return np.zeros(below.shape, dtype=bool)
    return lab == mark


def _seed(emb, gt):
    dem = emb["dem"]
    d_up = emb["d_up"]
    X, Y = _centers(gt, dem.shape)
    bx, by = emb["breach_xy"]
    dist = np.hypot(X - bx, Y - by)
    cell = abs(float(gt[1]))
    half = 0.5 * emb["crest_width_m"]
    reach = max(80.0, 20.0 * cell)
    finite = np.isfinite(dem)
    cand = (d_up > half + 0.5 * cell) & (dist <= reach) & finite & (dem < emb["crest_m"] - 0.05)
    if not cand.any():
        cand = (d_up > 0.5 * cell) & (dist <= reach) & finite & (dem < emb["crest_m"] - 0.05)
    if not cand.any():
        return None
    masked = np.where(cand, dem, np.inf)
    return np.unravel_index(int(np.argmin(masked)), dem.shape)


def stage_storage(emb, gt, steps=24):
    """Fill behind the embankment. Returns the curve and the rim check.

    The curve is the water that sits on the reservoir side of the crest, on the ground the
    embankment was built on (the upstream face included, the downstream face not). A saddle
    or the edge of the clip below the design crest is the rim: the pool leaves the basin
    there. The curve is still reported up to the design crest; stages above the rim are
    marked not holdable.
    """
    if steps < 2:
        raise ProposedError("The stage-storage curve needs at least two stages.")
    dem = emb["dem"]
    cell = abs(float(gt[1]))
    # a north-up grid can have a negative y cell; the area is the product of the sizes
    area = abs(float(gt[1]) * float(gt[5]))
    half_band = 0.25 * cell
    reservoir = emb["d_up"] >= -half_band
    seed = _seed(emb, gt)
    bed = emb["bed_m"]
    crest = emb["crest_m"]
    notes = []
    if seed is None:
        raise ProposedError(
            "No ground upstream of the embankment is below the design crest. Check which side "
            "of the line the reservoir is on (the upstream point) and that the crest is above "
            "the valley.")
    border = np.zeros(dem.shape, dtype=bool)
    border[0, :] = border[-1, :] = border[:, 0] = border[:, -1] = True
    raised = emb["raised"]

    def _wet(stage):
        below = reservoir & np.isfinite(dem) & (dem < stage)
        wet = _label_wet(below, seed)
        if not wet.any():
            return wet, False, False, None
        hit_edge = bool(np.any(wet & border & ~raised))
        dil = ndi.binary_dilation(wet, structure=_CROSS)
        saddle = dil & ~reservoir & ~raised & np.isfinite(dem) & (dem < stage)
        return wet, hit_edge, bool(saddle.any()), saddle

    def _first(kind):
        """Lowest stage at which `kind` ('edge' or 'saddle') is true, or None."""
        def hit(stage):
            _w, edge, sad, _s = _wet(stage)
            return edge if kind == "edge" else sad
        if not hit(crest - 1e-3):
            return None
        lo = bed + 0.02
        if hit(lo):
            return lo
        hi = crest - 1e-3
        for _i in range(18):
            mid = 0.5 * (lo + hi)
            if hit(mid):
                hi = mid
            else:
                lo = mid
        return hi

    edge_stage = _first("edge")
    saddle_stage = _first("saddle")
    candidates = [(s, k) for s, k in ((saddle_stage, "saddle"), (edge_stage, "clip_edge"))
                  if s is not None and s < crest - 0.05]
    if not candidates:
        rim_m, rim_kind = None, "contained"
    else:
        rim_m, rim_kind = min(candidates, key=lambda t: t[0])
    rim_xy = None
    if rim_m is not None:
        _w, _e, _s, saddle = _wet(min(crest - 1e-4, rim_m + 1e-3))
        if rim_kind == "saddle" and saddle is not None and saddle.any():
            rr, cc = np.nonzero(saddle)
            k = int(np.argmin(dem[rr, cc]))
            xs = gt[0] + (cc[k] + 0.5) * gt[1]
            ys = gt[3] + (rr[k] + 0.5) * gt[5]
            rim_xy = (float(xs), float(ys))
        elif rim_kind == "clip_edge":
            wet, _e, _s, _sd = _wet(min(crest - 1e-4, rim_m + 1e-3))
            hit = wet & border & ~raised
            if hit.any():
                rr, cc = np.nonzero(hit)
                k = int(np.argmin(dem[rr, cc]))
                rim_xy = (float(gt[0] + (cc[k] + 0.5) * gt[1]),
                          float(gt[3] + (rr[k] + 0.5) * gt[5]))

    pool = bed + min(0.05, 0.02 * (crest - bed))
    stages = list(np.linspace(pool, crest, int(steps) + 1))
    if rim_m is not None and pool < rim_m < crest:
        stages.append(rim_m)
        stages = sorted(set(round(s, 4) for s in stages))
    curve = []
    for stage in stages:
        wet, _e, _s, _sd = _wet(stage)
        if not wet.any():
            vol, surf = 0.0, 0.0
        else:
            vol = float((stage - dem[wet]).sum() * area)
            surf = float(wet.sum() * area)
        holdable = rim_m is None or stage <= rim_m + 1e-6
        curve.append((float(stage), vol, surf, holdable))
    if not curve:
        raise ProposedError("The stage-storage curve is empty.")
    at_crest = curve[-1]
    hold = [c for c in curve if c[3]]
    at_hold = hold[-1] if hold else curve[0]
    return {
        "curve": curve,                          # (elev_m, storage_m3, area_m2, holdable)
        "pool_m": float(pool),
        "storage_crest_m3": at_crest[1],
        "area_crest_m2": at_crest[2],
        "holdable_m3": at_hold[1],
        "holdable_area_m2": at_hold[2],
        "holdable_stage_m": at_hold[0],
        "rim_m": None if rim_m is None else float(rim_m),
        "rim_kind": rim_kind,
        "rim_xy": rim_xy,
        "seed_rc": seed,
        "notes": notes,
    }


def describe(emb, stor, upstream_given):
    """The stor_notes sentence, starting with PHRASE."""
    crest_ft = emb["crest_m"] / FT
    bed_ft = emb["bed_m"] / FT
    height_ft = emb["height_m"] / FT
    bits = [
        PHRASE,
        "Height {:.1f} ft = design crest {:.1f} ft minus the lowest natural ground on the line "
        "({:.1f} ft, the breach).".format(height_ft, crest_ft, bed_ft),
        "Crest width {:.1f} ft, upstream {:.2g}H:1V, downstream {:.2g}H:1V.".format(
            emb["crest_width_m"] / FT, emb["us_h"], emb["ds_h"]),
        "Storage at the design crest {:.1f} ac-ft over {:.2f} ac, measured on the pre-dam "
        "ground (nothing estimated under a water surface).".format(
            stor["storage_crest_m3"] / AF_M3, stor["area_crest_m2"] / AC_M2),
    ]
    if not upstream_given:
        bits.append("Reservoir side was taken as the left of the drawn direction; give an "
                    "upstream point if that is the downstream valley.")
    if emb["abutment_m"] > emb["crest_width_m"]:
        bits.append("{:.0f} ft of the drawn line is already above the design crest and was "
                    "not raised.".format(emb["abutment_m"] / FT))
    if stor["rim_kind"] == "contained":
        bits.append("Rim: the basin holds the design crest.")
    elif stor["rim_kind"] == "saddle":
        bits.append(
            "Rim low point {:.1f} ft is {:.1f} ft below the design crest{}; the pool leaves "
            "the basin there. Holdable storage {:.1f} ac-ft. The curve above that stage is the "
            "basin if the rim were raised; it is not water the dam can hold.".format(
                stor["rim_m"] / FT, (emb["crest_m"] - stor["rim_m"]) / FT,
                "" if not stor["rim_xy"] else " at {:.0f}, {:.0f}".format(*stor["rim_xy"]),
                stor["holdable_m3"] / AF_M3))
    else:
        bits.append(
            "The pool reaches the edge of the terrain clip at {:.1f} ft, {:.1f} ft below the "
            "design crest. That may be a real outlet or a clip that is too small — enlarge the "
            "clip before trusting the storage. Volume inside the clip at that stage is {:.1f} "
            "ac-ft.".format(stor["rim_m"] / FT, (emb["crest_m"] - stor["rim_m"]) / FT,
                            stor["holdable_m3"] / AF_M3))
    return " ".join(bits)


def build(dem, gt, line, crest_m, crest_width_m, us_h, ds_h, upstream="left",
          upstream_given=False, steps=24):
    """Insert the embankment and measure the pool. The array passed in is not changed."""
    before = np.array(dem, copy=False)
    emb = insert_embankment(dem, gt, line, crest_m, crest_width_m, us_h, ds_h, upstream)
    if before is dem and np.shares_memory(emb["dem"], dem):
        raise ProposedError("internal: the embankment wrote onto the source DEM")
    stor = stage_storage(emb, gt, steps=steps)
    text = describe(emb, stor, upstream_given)
    conf = "high" if stor["rim_kind"] == "contained" and upstream_given else (
        "medium" if stor["rim_kind"] == "contained" else "low")
    return emb, stor, text, conf


def crest_check(z, xs, ys, dam_xy, crest_m, crest_width_m):
    """(crest_ft, error) for the Runner's top-of-dam check.

    A proposed crest is the design elevation, not the highest hillside within 30 m of the
    breach. The check passes when a cell on the inserted crest, next to the dam point, is
    actually at that elevation — so a run whose grid never received the embankment stops,
    and a hillside does not.
    """
    z = np.asarray(z, dtype=float)
    r = np.hypot(np.asarray(xs, dtype=float) - dam_xy[0], np.asarray(ys, dtype=float) - dam_xy[1])
    reach = max(float(crest_width_m) + 6.0, 15.0)
    vals = z[(r <= reach) & np.isfinite(z)]
    if vals.size == 0:
        return None, "no DEM data at the dam point"
    if not np.any(np.abs(vals - float(crest_m)) <= 0.25):
        return None, (
            "the proposed embankment is not in the grid within {:.0f} m of the dam point "
            "(ground there is {:.2f} to {:.2f} m; the design crest is {:.2f} m). The source "
            "DEM is not modified — the embankment is inserted when the model reads the grid. "
            "Check that the dam point is the breach on the drawn line and that the DEM is in "
            "the same CRS as the crest line (EPSG:5070, or a grid the line can be projected "
            "onto).".format(reach, float(np.min(vals)), float(np.max(vals)), float(crest_m)))
    return float(crest_m) / FT, None


def json_safe_spec(emb, stor, line, line_lonlat, dam_id):
    """The block stored on dam.json (and 19_proposed.json). Lists, not arrays."""
    def xy(p):
        return None if p is None else [round(float(p[0]), 2), round(float(p[1]), 2)]
    return {
        "phrase": PHRASE,
        "dam_id": dam_id,
        "crs": "EPSG:5070",
        "line_xy": [[round(float(x), 2), round(float(y), 2)] for x, y in np.asarray(line, float)],
        "line_lonlat": [[round(float(a), 7), round(float(b), 7)] for a, b in line_lonlat],
        "crest_m": round(emb["crest_m"], 3),
        "crest_width_m": round(emb["crest_width_m"], 3),
        "us_slope_h": emb["us_h"],
        "ds_slope_h": emb["ds_h"],
        "upstream": emb["upstream"],
        "breach_xy": xy(emb["breach_xy"]),
        "bed_m": round(emb["bed_m"], 3),
        "height_m": round(emb["height_m"], 3),
        "crest_len_m": round(emb["crest_len_m"], 2),
        "rim_m": None if stor["rim_m"] is None else round(stor["rim_m"], 3),
        "rim_kind": stor["rim_kind"],
        "rim_xy": xy(stor["rim_xy"]),
        "storage_at_crest_m3": round(stor["storage_crest_m3"], 1),
        "holdable_m3": round(stor["holdable_m3"], 1),
        "surface_area_m2": round(stor["area_crest_m2"], 1),
    }
