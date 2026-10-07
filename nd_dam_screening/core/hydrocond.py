"""Hydro-conditioning: cut culvert gaps through road fills that block streams.

LiDAR DEMs show road embankments but not the culverts or bridges under them,
so a flood model ponds water behind every road until it overtops.  Along each
stream line (NHD flowlines) this module finds short "barriers" - places where
the ground rises above the lowest elevation already seen upstream and then
drops again - and lowers a one-cell-wide, 4-connected channel through them to
that upstream level.

Safeguards
  * barriers longer than `max_len_m` or higher than `max_rise_m` are left alone
    (real ridges, wrong-direction lines, big embankments);
  * cells inside `protect` (rasterised DWR dam lines, buffered) are never cut,
    so dams stay in the terrain;
  * optionally only barriers that coincide with a road (`roads` mask) are cut.
numpy only.
"""
import numpy as np

from .gridutil import polyline_cells, NODATA


def _orient(z):
    k = max(len(z) // 10, 1)
    return np.median(z[:k]) < np.median(z[-k:])   # True -> reverse (start is lower)


def breach_barriers(dem, lines_xy, gt, protect=None, roads=None, max_rise_m=8.0,
                    max_len_m=150.0, tol_m=0.05):
    """Return (conditioned_dem, info).  `dem` is not modified."""
    out = dem.astype("float64").copy()
    cell = abs(gt[1])
    max_len = max(int(round(max_len_m / cell)), 1)
    info = {"cuts": 0, "skipped_long": 0, "skipped_high": 0, "skipped_protected": 0,
            "skipped_no_road": 0, "max_cut_m": 0.0, "cells": 0}
    for xy in lines_xy:
        cells = polyline_cells(xy, gt, dem.shape)
        if len(cells) < 3:
            continue
        rr = np.array([c[0] for c in cells]); cc = np.array([c[1] for c in cells])
        z = out[rr, cc]
        valid = z > NODATA + 1
        if valid.sum() < 3:
            continue
        if _orient(z[valid]):
            rr, cc, z, valid = rr[::-1], cc[::-1], z[::-1], valid[::-1]
        zc = z.copy()
        run_min = np.inf
        for i in range(len(z)):
            if valid[i]:
                run_min = min(run_min, z[i])
            zc[i] = run_min
        above = valid & (z > zc + tol_m)
        i, n = 0, len(z)
        while i < n:
            if not above[i]:
                i += 1; continue
            j = i
            while j < n and above[j]:
                j += 1
            if j >= n:                     # never drops back below: not a barrier
                break
            seg = slice(i, j)
            rise = float((z[seg] - zc[seg]).max())
            if j - i > max_len:
                info["skipped_long"] += 1
            elif rise > max_rise_m:
                info["skipped_high"] += 1
            elif protect is not None and protect[rr[seg], cc[seg]].any():
                info["skipped_protected"] += 1
            elif roads is not None and not roads[rr[seg], cc[seg]].any():
                info["skipped_no_road"] += 1
            else:
                out[rr[seg], cc[seg]] = np.minimum(out[rr[seg], cc[seg]], zc[seg])
                info["cuts"] += 1; info["cells"] += j - i
                info["max_cut_m"] = max(info["max_cut_m"], rise)
            i = j
    return out, info
