"""Reservoir set-up for the reservoir-in-grid engine.  numpy only.

1. Reservoir extent: cells below the failure pool (top of dam) connected to a
   seed just upstream of the breach, never crossing the dam line.
2. Bed below the hydro-flattened water surface (LiDAR shows the water surface,
   not the bottom).  Same idea as DSS-WISE: depth grows with distance from the
   shore.  depth = D * (d / d_max) ** shape, with D chosen so the volume below
   normal pool equals the calibrated below-pool storage from tool 3.
3. Breach cells: the part of the embankment within half the breach width of
   the breach point, measured along the dam line.
"""
from collections import deque

import numpy as np

from .gridutil import NODATA, rasterize_lines, inside, dilate

N4 = [(-1, 0), (1, 0), (0, -1), (0, 1)]


def distance_to_edge(mask, max_iter=100000):
    """Cells-to-boundary (4-connected erosion count) inside `mask`."""
    d = np.zeros(mask.shape, float)
    cur = mask.copy()
    k = 0
    while cur.any() and k < max_iter:
        k += 1
        d[cur] = k
        er = cur.copy()
        er[1:, :] &= cur[:-1, :]; er[:-1, :] &= cur[1:, :]
        er[:, 1:] &= cur[:, :-1]; er[:, :-1] &= cur[:, 1:]
        er[0, :] = er[-1, :] = er[:, 0] = er[:, -1] = False
        cur = er
    return d


def find_reservoir(dem, dam_mask, seed_rc, wse, max_cells=5_000_000):
    """Connected cells with ground < wse reachable from seed without crossing the dam."""
    valid = (dem > NODATA + 1) & ~dam_mask
    r0, c0 = seed_rc
    res = np.zeros(dem.shape, bool)
    if not inside(dem.shape, r0, c0) or not valid[r0, c0] or dem[r0, c0] >= wse:
        return res, "seed not below the failure pool"
    q = deque([(r0, c0)]); res[r0, c0] = True
    n, edge = 1, False
    while q:
        r, c = q.popleft()
        for dr, dc in N4:
            rr, cc = r + dr, c + dc
            if not inside(dem.shape, rr, cc):
                edge = True; continue
            if res[rr, cc] or not valid[rr, cc] or dem[rr, cc] >= wse:
                continue
            res[rr, cc] = True; q.append((rr, cc)); n += 1
            if n > max_cells:
                return res, "reservoir too large"
    return res, ("reservoir reaches the terrain edge" if edge else "")


def seed_upstream(dem, breach_rc, flow_az_deg, cell, dist_m=(15, 30, 60, 100), exclude=None):
    """Lowest cell a short way upstream of the breach (against the flow direction).

    Cells in `exclude` (the dilated dam line) are skipped; on coarse grids the search
    also reaches 2-4 cells out so it clears the dam footprint.
    """
    a = np.radians(flow_az_deg)
    dr, dc = np.cos(a), -np.sin(a)          # upstream: row +cos (south is +row), col -sin
    best = None
    for d in tuple(dist_m) + (2 * cell, 3 * cell, 4 * cell):
        r = int(round(breach_rc[0] + dr * d / cell)); c = int(round(breach_rc[1] + dc * d / cell))
        if inside(dem.shape, r, c) and dem[r, c] > NODATA + 1 and not (
                exclude is not None and exclude[r, c]):
            if best is None or dem[r, c] < dem[best]:
                best = (r, c)
    return best


def estimate_bed(dem, res_mask, pool_elev, v_below_m3, cell, shape=1.0, flat_tol=0.15):
    """Carve a bed under the flat water surface so storage below pool = v_below_m3.

    Returns (bed, info).  Cells of the reservoir that are not on the flat water
    surface (above normal pool) keep their LiDAR ground.
    """
    water = res_mask & (np.abs(dem - pool_elev) <= flat_tol)
    info = {"water_cells": int(water.sum()), "depth_max_m": 0.0, "v_below_m3": 0.0}
    if not water.any() or not v_below_m3 or v_below_m3 <= 0:
        return dem.copy(), info
    d = distance_to_edge(water)
    f = (d / d.max()) ** shape
    s = float(f[water].sum()) * cell * cell
    D = v_below_m3 / s if s > 0 else 0.0
    bed = dem.copy()
    bed[water] = pool_elev - D * f[water]
    info.update({"depth_max_m": float(D), "v_below_m3": float(D * s)})
    return bed, info


def breach_cells(dem, dam_line_xy, gt, breach_xy, bavg_m, embank_halfwidth_m=None,
                 crest_elev=None, invert=None):
    """Embankment cells within bavg/2 of the breach point along the dam axis."""
    cell = abs(gt[1])
    line = rasterize_lines([dam_line_xy], gt, dem.shape)
    hw = embank_halfwidth_m
    if hw is None and crest_elev is not None and invert is not None:
        hw = max(3.0 * (crest_elev - invert), 2 * cell)     # ~3H:1V slopes each side
    hw = hw or 3 * cell
    foot = dilate(line, max(int(hw / cell), 1))
    rr, cc = np.mgrid[0:dem.shape[0], 0:dem.shape[1]]
    x = gt[0] + (cc + 0.5) * gt[1]; y = gt[3] + (rr + 0.5) * gt[5]
    xy = np.asarray(dam_line_xy, float)
    ax = xy[-1] - xy[0]
    ax = ax / (np.hypot(*ax) or 1.0)
    along = (x - breach_xy[0]) * ax[0] + (y - breach_xy[1]) * ax[1]
    m = foot & (np.abs(along) <= max(bavg_m / 2.0, cell))
    if invert is not None:
        m &= dem > invert
    return m, line
