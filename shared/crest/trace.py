"""Trace the dam crest on the DEM.

Two algorithms:

trace_crest_dem      Simple DEM ridge tracer (nd crest.py v3.11.0).
                     Starts at the highest cell near the dam point, finds the
                     direction of highest mean elevation, follows the ridge
                     both ways while both sides drop away.

trace_crest_hecras   HEC-RAS embankment tracer (DWR hecras_crest.py v3.10.22).
                     Snaps to the ridge, walks with prominence and tolerance
                     checks, steps over spillway gaps, picks breach location.

Both return a polyline in map coordinates.

Polyline utilities (cumlen through bearing_deg) are shared helpers used by the
tracers and by other crest code (e.g. hecras_model.crest_dam_line).
"""
import math

import numpy as np

FT_M = 0.3048

# ---------------------------------------------------------------------------
# Constants: simple DEM ridge tracer
# ---------------------------------------------------------------------------
STEP_CELLS = 1.0
LATERAL_CELLS = 2.0
SIDE_OFFSET_M = 30.0
MIN_RELIEF_FRAC = 0.12
SHORT_FRAC = 0.7
STOP_DIST_M = 90.0
DEFAULT_HALF_LEN_M = 1500.0
MAX_HALF_LEN_M = 4000.0

# ---------------------------------------------------------------------------
# Constants: HEC-RAS embankment tracer
# ---------------------------------------------------------------------------
DAM_AXES = ("crest", "straight")
BREACH_AT = ("auto", "toe", "lowest_crest", "inflow")
TURN_MAX_DEG = 30.0
TURN_FIRST_DEG = 55.0
FIRST_STEPS = 3
PROM_MIN_FT = 2.0
PROM_FRAC = 0.1
CENTRE_BIAS = 1e-3
PLATEAU_FRAC = 0.05


class CrestTraceError(ValueError):
    """The crest could not be traced; the message says why."""


# ===================================================================
# Polyline utilities
# ===================================================================
def cumlen(line):
    p = np.asarray(line, float).reshape(-1, 2)
    if len(p) < 2:
        return np.zeros(len(p))
    return np.r_[0.0, np.cumsum(np.hypot(*np.diff(p, axis=0).T))]


def point_at(line, s):
    """Points at stations s (scalar or array) along the polyline."""
    p = np.asarray(line, float).reshape(-1, 2)
    c = cumlen(p)
    s_ = np.clip(np.asarray(s, float), 0.0, c[-1])
    out = np.column_stack([np.interp(np.ravel(s_), c, p[:, 0]),
                           np.interp(np.ravel(s_), c, p[:, 1])])
    return out[0] if np.ndim(s) == 0 else out


def segment_at(line, s):
    p = np.asarray(line, float).reshape(-1, 2)
    c = cumlen(p)
    return int(np.clip(np.searchsorted(c, float(s), side="right") - 1, 0, len(p) - 2))


def tangent_at(line, s):
    """Unit tangent of the segment that holds station s."""
    p = np.asarray(line, float).reshape(-1, 2)
    k = segment_at(p, s)
    t = p[k + 1] - p[k]
    return t / (np.hypot(*t) or 1.0)


def left_normal(t):
    t = np.asarray(t, float)
    return np.array([-t[1], t[0]])


def station_of(line, xy):
    """Station of the point's projection on the polyline."""
    p = np.asarray(line, float).reshape(-1, 2)
    q = np.asarray(xy, float)
    best, st, acc = np.inf, 0.0, 0.0
    for a, b in zip(p[:-1], p[1:]):
        ab = b - a
        L2 = float(ab @ ab)
        t = 0.0 if L2 <= 0 else float(np.clip((q - a) @ ab / L2, 0.0, 1.0))
        dd = float(np.hypot(*(a + t * ab - q)))
        if dd < best:
            best, st = dd, acc + t * math.sqrt(L2)
        acc += math.sqrt(L2)
    return st


def sub_line(line, s0, s1):
    """The part of the polyline from station s0 to s1."""
    p = np.asarray(line, float).reshape(-1, 2)
    c = cumlen(p)
    s0, s1 = max(0.0, float(s0)), min(float(c[-1]), float(s1))
    mid = [q for q, cc in zip(p, c) if s0 + 1e-9 < cc < s1 - 1e-9]
    return np.vstack([point_at(p, s0)] + mid + [point_at(p, s1)]) if mid else \
        np.vstack([point_at(p, s0), point_at(p, s1)])


def frame_at(line, s):
    """(origin, along, downstream) straight frame at station s."""
    t = tangent_at(line, s)
    return point_at(line, s) - t * float(s), t, left_normal(t)


def simplify(line, tol):
    """Douglas-Peucker (ends kept)."""
    p = np.asarray(line, float).reshape(-1, 2)
    if len(p) < 3:
        return p
    keep = np.zeros(len(p), bool)
    keep[[0, -1]] = True
    stack = [(0, len(p) - 1)]
    while stack:
        i, j = stack.pop()
        if j <= i + 1:
            continue
        a, b = p[i], p[j]
        ab = b - a
        L = float(np.hypot(*ab))
        q = p[i + 1:j]
        if L <= 0:
            dd = np.hypot(*(q - a).T)
        else:
            dd = np.abs((q[:, 0] - a[0]) * ab[1] - (q[:, 1] - a[1]) * ab[0]) / L
        k = int(np.argmax(dd))
        if dd[k] > tol:
            keep[i + 1 + k] = True
            stack += [(i, i + 1 + k), (i + 1 + k, j)]
    return p[keep]


def smooth(line, window):
    """Moving average of the vertices (odd window; the ends stay)."""
    p = np.asarray(line, float).reshape(-1, 2)
    w = int(window) // 2
    if w < 1 or len(p) < 3:
        return p
    out = p.copy()
    for i in range(1, len(p) - 1):
        k = min(w, i, len(p) - 1 - i)
        out[i] = p[i - k:i + k + 1].mean(axis=0)
    return out


def min_spacing(line, step, keep=()):
    """Vertices closer than `step` to the last kept one go."""
    p = np.asarray(line, float).reshape(-1, 2)
    keep = set(int(k) for k in keep) | {0, len(p) - 1}
    out = [0]
    for i in range(1, len(p) - 1):
        if i in keep or np.hypot(*(p[i] - p[out[-1]])) >= step:
            out.append(i)
    while len(out) > 1 and out[-1] not in keep and np.hypot(*(p[-1] - p[out[-1]])) < step:
        out.pop()
    out.append(len(p) - 1)
    return p[out]


def straighten(line, s0, s1):
    """Replace vertices between s0 and s1 with a straight chord."""
    p = np.asarray(line, float).reshape(-1, 2)
    c = cumlen(p)
    s0, s1 = max(0.0, float(s0)), min(float(c[-1]), float(s1))
    before = [q for q, cc in zip(p, c) if cc < s0 - 1e-9]
    after = [q for q, cc in zip(p, c) if cc > s1 + 1e-9]
    return np.vstack(before + [point_at(p, s0), point_at(p, s1)] + after)


def monotonic(line, axis):
    """True when the vertices advance along `axis` all the way."""
    p = np.asarray(line, float).reshape(-1, 2)
    return bool(np.all(np.diff(p @ np.asarray(axis, float)) > 0))


def angle_deg(u, v):
    """Angle between two directions as lines (0-90 degrees)."""
    u = np.asarray(u, float) / (np.hypot(*u) or 1.0)
    v = np.asarray(v, float) / (np.hypot(*v) or 1.0)
    return float(np.degrees(np.arccos(min(1.0, abs(float(u @ v))))))


def bearing_deg(v):
    return float(np.degrees(np.arctan2(v[0], v[1])) % 360.0)


# ===================================================================
# Simple DEM ridge tracer (nd crest.py)
# ===================================================================
def _at(z, grid, x, y):
    r, c = grid.rc(x, y)
    if 0 <= r < z.shape[0] and 0 <= c < z.shape[1]:
        return float(z[r, c])
    return float("nan")


def _smooth_max_axis(z, grid, p, half_len, n_angles=36):
    """Unit vector along which the mean elevation within `half_len` of `p` is
    highest: the direction of the crest through the point."""
    best, best_v = -np.inf, (1.0, 0.0)
    step = max(grid.dx, half_len / 30.0)
    ss = np.arange(-half_len, half_len + 0.1, step)
    for k in range(n_angles):
        a = math.pi * k / n_angles
        ux, uy = math.cos(a), math.sin(a)
        vals = np.array([_at(z, grid, p[0] + s * ux, p[1] + s * uy) for s in ss])
        if np.isfinite(vals).sum() < 0.6 * len(vals):
            continue
        m = float(np.nanmean(vals))
        if m > best:
            best, best_v = m, (ux, uy)
    return best_v


def _relief(z, grid, p, n, off):
    """Crest elevation at `p` minus the ground `off` away on each side."""
    zc = _at(z, grid, p[0], p[1])
    a = _at(z, grid, p[0] + off * n[0], p[1] + off * n[1])
    b = _at(z, grid, p[0] - off * n[0], p[1] - off * n[1])
    return zc - a, zc - b


def _follow(z, grid, start, tangent, height_m, max_len_m):
    """Points along the ridge from `start` in the direction `tangent`."""
    c = grid.dx
    step = STEP_CELLS * c
    lat = LATERAL_CELLS * c
    off = max(SIDE_OFFSET_M, 3.0 * c)
    min_relief = MIN_RELIEF_FRAC * height_m
    pts, bad = [], 0
    stop_after = max(3, int(round(STOP_DIST_M / step)))
    p = (float(start[0]), float(start[1]))
    t = (float(tangent[0]), float(tangent[1]))
    travelled = 0.0
    while travelled < max_len_m:
        q = (p[0] + step * t[0], p[1] + step * t[1])
        n = (-t[1], t[0])
        best_z, bq = -np.inf, None
        for s in np.arange(-lat, lat + 0.1, 0.5 * c):
            cand = (q[0] + s * n[0], q[1] + s * n[1])
            v = _at(z, grid, cand[0], cand[1])
            if np.isfinite(v) and v > best_z:
                best_z, bq = v, cand
        if bq is None:
            break
        da, db = _relief(z, grid, bq, n, off)
        if not (np.isfinite(da) and np.isfinite(db)) or min(da, db) < min_relief:
            bad += 1
            if bad >= stop_after:
                break
        else:
            bad = 0
            pts.append(bq)
        nt = (bq[0] - p[0], bq[1] - p[1])
        ln = math.hypot(*nt)
        if ln > 0:
            nt = (nt[0] / ln, nt[1] / ln)
            t = (0.7 * t[0] + 0.3 * nt[0], 0.7 * t[1] + 0.3 * nt[1])
            ln = math.hypot(*t)
            t = (t[0] / ln, t[1] / ln)
        travelled += step
        p = bq
    return pts


def trace_crest_dem(grid, dam_xy, height_m, crest_length_m=None, ft=FT_M):
    """Trace the dam line through `dam_xy` on `grid` (elevations in metres).

    Returns (line (N, 2) array or None, note).  `height_m` is the dam height;
    `crest_length_m` the known crest length (each way traces up to that
    length, covering off-centre points); `ft` is metres per foot.
    """
    z = grid.masked()
    c = grid.dx
    r0, c0 = grid.rc(*dam_xy)
    if not grid.inside(r0, c0):
        return None, "the dam point is outside the DEM"
    rad = max(30.0, 2.0 * c)
    X, Y = np.meshgrid(grid.x_centers(), grid.y_centers())
    sel = np.hypot(X - dam_xy[0], Y - dam_xy[1]) <= rad
    if not np.isfinite(z[sel]).any():
        return None, "no DEM data at the dam point"
    k = int(np.nanargmax(np.where(sel, z, np.nan)))
    start = (float(X.flat[k]), float(Y.flat[k]))
    max_len = min(MAX_HALF_LEN_M, 1.1 * crest_length_m if crest_length_m else DEFAULT_HALF_LEN_M)
    axis = _smooth_max_axis(z, grid, start, min(max_len, 300.0))
    fwd = _follow(z, grid, start, axis, height_m, max_len)
    back = _follow(z, grid, start, (-axis[0], -axis[1]), height_m, max_len)
    pts = list(reversed(back)) + [start] + fwd
    if len(pts) < 3:
        return None, ("no ridge was found by the dam point (the ground does not drop away on "
                      "both sides by {:.0f}% of the dam height)".format(100 * MIN_RELIEF_FRAC))
    line = np.asarray(pts, dtype=float)
    length_ft = float(np.hypot(*np.diff(line, axis=0).T).sum()) / ft
    note = "traced {:,.0f} ft of crest from the DEM".format(length_ft)
    if crest_length_m and length_ft < SHORT_FRAC * crest_length_m / ft:
        note += (" (the crest is {:,.0f} ft: the DEM shows a ridge along only part of it, so the "
                 "rest is not raised; give the crest line in CREST_LINE to raise all of it)").format(
                     crest_length_m / ft)
    return line, note


# ===================================================================
# HEC-RAS embankment tracer (DWR hecras_crest.py)
# ===================================================================
def _ridge(sample, Y, nu, half, ds):
    tt = np.arange(-half, half + 1e-9, ds)
    zz = sample(Y[None, :] + tt[:, None] * nu[None, :])
    if not np.isfinite(zz).any():
        return None
    zb = np.where(np.isfinite(zz), zz, -np.inf)
    k = int(np.argmax(zb - CENTRE_BIAS * np.abs(tt)))
    lvl = zb[k] - PLATEAU_FRAC * ds
    i, j = k, k
    while i > 0 and zb[i - 1] >= lvl:
        i -= 1
    while j < len(zb) - 1 and zb[j + 1] >= lvl:
        j += 1
    if j > i:
        R = Y + 0.5 * (tt[i] + tt[j]) * nu
        zr = float(sample(R[None, :])[0])
        if np.isfinite(zr):
            return R, zr
    return Y + tt[k] * nu, float(zz[k])


def _judge(sample, R, nu, zr, top, tol, toe, prom):
    """('', z) on the crest, else (why, z)."""
    if not np.isfinite(zr):
        return "edge of the DEM"
    if zr > top + tol:
        return "abutment: the ridge rises above the top of dam"
    if zr < top - tol:
        return "end of the embankment: the ridge falls below the crest"
    tt = np.linspace(0.5 * toe, toe, 4)
    lo_l = sample(R[None, :] + tt[:, None] * nu[None, :])
    lo_r = sample(R[None, :] - tt[:, None] * nu[None, :])
    sides = [np.nanmin(v) if np.isfinite(v).any() else np.nan for v in (lo_l, lo_r)]
    hi = np.nanmax(sides) if np.isfinite(sides).any() else np.nan
    if not np.isfinite(hi) or zr - hi < prom:
        return "abutment tie-in: the ground beside the crest is as high as the crest"
    return ""


def _walk(sample, X0, tau0, max_len, step, toe, top, tol, prom, gap):
    pts = [np.asarray(X0, float)]
    X, tau = pts[0], np.asarray(tau0, float) / (np.hypot(*tau0) or 1.0)
    walked, run, last_ok, why, nstep = 0.0, 0.0, 0, "the dam length", 0
    ds = 0.5 * step
    while walked < max_len - 1e-9:
        h = min(step, max_len - walked)
        if h < 0.25 * step:
            break
        Y = X + tau * h
        nu = left_normal(tau)
        ang = TURN_FIRST_DEG if nstep < FIRST_STEPS else TURN_MAX_DEG
        rr = _ridge(sample, Y, nu, h * math.tan(math.radians(ang)), ds)
        nstep += 1
        bad = "edge of the DEM" if rr is None else _judge(sample, rr[0], nu, rr[1], top, tol,
                                                          toe, prom)
        if not bad:
            R = rr[0]
            v = R - X
            v = v / (np.hypot(*v) or 1.0)
            tau = tau + v
            tau = tau / (np.hypot(*tau) or 1.0)
            walked += float(np.hypot(*(R - X)))
            X = R
            pts.append(R)
            last_ok, run = len(pts) - 1, 0.0
        else:
            run += h
            if run > gap + 1e-9 or bad == "edge of the DEM":
                why = bad
                break
            X = Y
            walked += h
            pts.append(Y)
    return np.vstack(pts[:last_ok + 1]), why, walked


def trace_crest_hecras(sample, P, axis, top, height, step, toe, max_len, gap,
                       tol=None, prom=None, search=None, ft=1.0):
    """Trace the crest polyline through the dam point for the HEC-RAS model.

    sample(xy (n,2)) -> z (n,) in model units; axis: unit vector along the dam
    from A (left end looking downstream) to B; top, height: top of dam and dam
    height; step: walk step; toe: crest-to-toe distance; max_len: dam length;
    gap: stretches off the crest stepped over; ft: model units per foot.

    Returns dict with 'line', 'p0', 'z0', 'snap', 'ends', 'tol', 'prom'.
    Raises CrestTraceError.
    """
    axis = np.asarray(axis, float) / (np.hypot(*axis) or 1.0)
    tol = float(tol if tol is not None else max(3.0 * ft, 0.1 * height))
    prom = float(prom if prom is not None else max(PROM_MIN_FT * ft, PROM_FRAC * height))
    search = float(search if search is not None else max(toe, 3.0 * step))
    P = np.asarray(P, float)
    nu = left_normal(axis)
    rr = _ridge(sample, P, nu, search, 0.5 * step)
    if rr is None:
        raise CrestTraceError("no DEM around the dam point")
    p0, z0 = rr
    why = _judge(sample, p0, nu, z0, top, tol, toe, prom)
    if why:
        raise CrestTraceError("no embankment crest within {:.0f} of the dam point across the "
                              "dam ({}; highest ground {:.2f}, top of dam {:.2f})".format(
                                  search, why, z0, top))
    a, why_a, _ = _walk(sample, p0, -axis, max_len, step, toe, top, tol, prom, gap)
    b, why_b, _ = _walk(sample, p0, axis, max_len, step, toe, top, tol, prom, gap)
    line = np.vstack([a[::-1], b[1:]])
    la, lb = float(cumlen(a)[-1]), float(cumlen(b)[-1])
    if la + lb < 2.0 * step:
        raise CrestTraceError("the crest could not be followed from the dam point ({} / {})"
                              .format(why_a, why_b))
    return {"line": line, "p0": p0, "z0": z0, "snap": float(np.hypot(*(p0 - P))),
            "ends": {"A": (why_a, la), "B": (why_b, lb)}, "tol": tol, "prom": prom}


# ===================================================================
# Post-trace: tidy, pick breach, extend
# ===================================================================
def tidy(line, step, cell, keep_len=None, centre=None):
    """Smooth, simplify, and trim the traced crest line."""
    p = np.asarray(line, float).reshape(-1, 2)
    win = max(3, int(round(0.5 * cell / max(step, 1e-9))) | 1)
    q = smooth(np.vstack([p[0], *(point_at(p, np.arange(step, cumlen(p)[-1], step))), p[-1]])
               if cumlen(p)[-1] > 2 * step else p, win)
    q = simplify(q, max(0.25 * step, 0.02 * cell))
    if keep_len is not None and cumlen(q)[-1] > keep_len + 1e-9:
        L = cumlen(q)[-1]
        s = station_of(q, centre) if centre is not None else 0.5 * L
        s = min(max(s, 0.5 * keep_len), L - 0.5 * keep_len)
        q = sub_line(q, s - 0.5 * keep_len, s + 0.5 * keep_len)
    return min_spacing(q, cell)


def pick_breach(line, sample, toe, band, half_bottom, half_top, top, tol,
                method="auto", step=None):
    """(station, how, detail) of the breach centre along the crest line."""
    p = np.asarray(line, float).reshape(-1, 2)
    L = float(cumlen(p)[-1])
    step = float(step or max(L / 400.0, 1e-6))
    s = np.arange(0.0, L + 1e-9, step)
    if len(s) < 3:
        return 0.5 * L, "the middle of the dam line (too short to choose)", {}
    xy = point_at(p, s)
    zc = sample(xy)
    win = max(1, int(round(half_bottom / step)))
    ker = np.ones(2 * win + 1)

    def avg(v):
        ok = np.isfinite(v).astype(float)
        num = np.convolve(np.where(np.isfinite(v), v, 0.0), ker, "same")
        den = np.convolve(ok, ker, "same")
        with np.errstate(invalid="ignore", divide="ignore"):
            return np.where(den > 0, num / den, np.nan)

    lo, hi = half_top, L - half_top
    inner = (s >= lo - 1e-9) & (s <= hi + 1e-9) if hi > lo else np.abs(s - 0.5 * L) <= step
    crest = inner & (np.abs(zc - top) <= tol)
    pool = crest if crest.any() else inner
    m = str(method or "auto").lower()
    detail = {}
    if m in ("auto", "toe"):
        n = np.array([left_normal(tangent_at(p, v)) for v in s])
        tt = np.linspace(toe, toe + band, 5)
        zt = np.nanmin(np.vstack([sample(xy + n * t) for t in tt]), axis=0) \
            if np.isfinite(sample(xy + n * toe)).any() else np.full(len(s), np.nan)
        za = avg(zt)
        if np.isfinite(za[pool]).any():
            k = np.nonzero(pool)[0][int(np.nanargmin(za[pool]))]
            detail = {"toe_ground": round(float(za[k]), 2),
                      "toe_ground_range": [round(float(np.nanmin(za[pool])), 2),
                                           round(float(np.nanmax(za[pool])), 2)]}
            return float(s[k]), ("the deepest dam section along the crest (the lowest ground at "
                                 "the downstream toe, where the old channel crosses)"), detail
        m = "lowest_crest"
    za = avg(zc)
    if np.isfinite(za[pool]).any():
        k = np.nonzero(pool)[0][int(np.nanargmin(za[pool]))]
        return float(s[k]), "the lowest crest along the traced dam line", {
            "crest_ground": round(float(za[k]), 2)}
    return 0.5 * L, "the middle of the dam line (no ground to choose by)", {}


def extend(line, span, axis, min_dot=0.5):
    """Extend the polyline `span` long at each end for cutting the 2D area."""
    p = np.asarray(line, float).reshape(-1, 2)
    axis = np.asarray(axis, float) / (np.hypot(*axis) or 1.0)
    ta = p[1] - p[0]
    ta = ta / (np.hypot(*ta) or 1.0)
    tb = p[-1] - p[-2]
    tb = tb / (np.hypot(*tb) or 1.0)
    if ta @ axis < min_dot:
        ta = axis
    if tb @ axis < min_dot:
        tb = axis
    return np.vstack([p[0] - ta * span, p, p[-1] + tb * span])
