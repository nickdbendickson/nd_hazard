"""v3.11.0 raise the dam line in the model DEM.

The breach hydrograph is put into the model just below the dam. A DEM smooths a dam's crest
(a 10 m 3DEP cell is a mean of crest and slope, 4 ft under the top of dam at Elm River Dam 1),
so water from the breach spills back over the low crest and fills the empty reservoir basin.
That water is missing downstream, and in the FV1 near-dam run it was the pond that started an
unstable boundary inflow. The fix is to lift the cells under the dam line to the top of dam in
the model DEM (the source DEM is not changed), the same way the proposed-embankment edit does.

The dam line is the CREST_LINE layer when one is given. Otherwise it is traced from the DEM:
start at the highest cell by the dam point, take the direction along which the ground stays
highest, and follow the ridge both ways until it stops being a ridge (ground on both sides no
longer drops away from it) or the crest length is used up. Nothing here needs a layer, so it
works for a dam that has only a point."""
import math

import numpy as np

from .units import FT

STEP_CELLS = 1.0            # trace step in cells
LATERAL_CELLS = 2.0         # how far either side the next crest cell is looked for
SIDE_OFFSET_M = 30.0        # ground this far either side is compared with the crest
MIN_RELIEF_FRAC = 0.12      # a ridge drops at least this fraction of the dam height both sides (a 10 m DEM
                            # smooths a crest: Elm River Dam 1 drops 3 ft of its 21.5 ft at 30 m)
SHORT_FRAC = 0.7            # a trace shorter than this share of the known crest length is flagged
STOP_DIST_M = 90.0          # this much ground without a ridge ends the trace (at least 3 steps)
DEFAULT_HALF_LEN_M = 1500.0
MAX_HALF_LEN_M = 4000.0


def _at(z, grid, x, y):
    r, c = grid.rc(x, y)
    if 0 <= r < z.shape[0] and 0 <= c < z.shape[1]:
        return float(z[r, c])
    return float("nan")


def _smooth_max_axis(z, grid, p, half_len, n_angles=36):
    """Unit vector along which the mean elevation within `half_len` of `p` is highest: the
    direction of the crest through the point."""
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
    """Crest elevation at `p` minus the ground `off` away on each side along the normal `n`:
    (drop one side, drop the other)."""
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
        # the highest cell across the ridge at the next station
        best, bq = -np.inf, None
        for s in np.arange(-lat, lat + 0.1, 0.5 * c):
            cand = (q[0] + s * n[0], q[1] + s * n[1])
            v = _at(z, grid, cand[0], cand[1])
            if np.isfinite(v) and v > best:
                best, bq = v, cand
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


def trace_crest(grid, dam_xy, height_m, crest_length_m=None):
    """Trace the dam line through `dam_xy` on `grid` (elevations in metres). Returns
    (line (N, 2) array or None, note). `height_m` is the dam height; `crest_length_m` the
    crest length when it is known (each way is traced up to that length, so a dam point away
    from the middle is still covered)."""
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
    length_ft = float(np.hypot(*np.diff(line, axis=0).T).sum()) / FT
    note = "traced {:,.0f} ft of crest from the DEM".format(length_ft)
    if crest_length_m and length_ft < SHORT_FRAC * crest_length_m / FT:
        note += (" (the crest is {:,.0f} ft: the DEM shows a ridge along only part of it, so the "
                 "rest is not raised; give the crest line in CREST_LINE to raise all of it)").format(
                     crest_length_m / FT)
    return line, note
