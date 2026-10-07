"""Small raster helpers shared by the terrain, triage and solver modules.  numpy only.

Geotransform convention (GDAL): gt = (x_left, cell, 0, y_top, 0, -cell).
Row 0 is the top (north) row.
"""
import math

import numpy as np

NODATA = -9999.0


def xy_to_rc(gt, x, y):
    return int((y - gt[3]) / gt[5]), int((x - gt[0]) / gt[1])


def rc_to_xy(gt, r, c):
    return gt[0] + (c + 0.5) * gt[1], gt[3] + (r + 0.5) * gt[5]


def inside(shape, r, c):
    return 0 <= r < shape[0] and 0 <= c < shape[1]


def polyline_cells(xy, gt, shape):
    """Ordered, 4-connected list of (r, c) cells visited by a polyline.

    Diagonal steps get an intermediate cell so that a one-cell-wide channel
    carved along the list conducts water in solvers that only exchange flow
    across cell faces (LISFLOOD-FP, the built-in engine).
    """
    xy = np.asarray(xy, float)
    if len(xy) < 2:
        return []
    step = abs(gt[1]) * 0.4
    out = []
    for (x0, y0), (x1, y1) in zip(xy[:-1], xy[1:]):
        n = max(int(math.hypot(x1 - x0, y1 - y0) / step), 1)
        for k in range(n + 1):
            x = x0 + (x1 - x0) * k / n
            y = y0 + (y1 - y0) * k / n
            r, c = xy_to_rc(gt, x, y)
            if not inside(shape, r, c):
                continue
            if out and out[-1] == (r, c):
                continue
            if out:
                pr, pc = out[-1]
                if abs(pr - r) + abs(pc - c) == 2 and abs(pr - r) == 1:     # diagonal step
                    out.append((pr, c))
                elif abs(pr - r) + abs(pc - c) > 2:                           # gap (leaves/re-enters)
                    pass
            out.append((r, c))
    # remove immediate repeats created by the diagonal fix
    clean = []
    for rc in out:
        if not clean or clean[-1] != rc:
            clean.append(rc)
    return clean


def rasterize_lines(lines_xy, gt, shape):
    m = np.zeros(shape, bool)
    for xy in lines_xy:
        for r, c in polyline_cells(xy, gt, shape):
            m[r, c] = True
    return m


def rasterize_polygons(polys_xy, gt, shape):
    """Cells whose CENTRE is inside a polygon (even-odd rule, exterior rings only).

    polys_xy: list of rings [(x, y), ...] (closed or not).  numpy only: for each ring the
    crossing-number test is evaluated on the cell centres of the ring's bounding box.  A ring
    smaller than a cell marks no cell unless a centre falls inside it (as a flood model sees it).
    """
    m = np.zeros(shape, bool)
    ny, nx = shape
    cell = abs(gt[1])
    for ring in polys_xy:
        xy = np.asarray(ring, float).reshape(-1, 2)
        if len(xy) < 3:
            continue
        if not (xy[0] == xy[-1]).all():
            xy = np.vstack([xy, xy[:1]])
        c0 = max(int((xy[:, 0].min() - gt[0]) / cell), 0)
        c1 = min(int((xy[:, 0].max() - gt[0]) / cell) + 1, nx)
        r0 = max(int((gt[3] - xy[:, 1].max()) / cell), 0)
        r1 = min(int((gt[3] - xy[:, 1].min()) / cell) + 1, ny)
        if c1 <= c0 or r1 <= r0:
            continue
        cx = gt[0] + (np.arange(c0, c1) + 0.5) * cell
        cy = gt[3] - (np.arange(r0, r1) + 0.5) * cell
        X, Y = np.meshgrid(cx, cy)
        inside = np.zeros(X.shape, bool)
        x0, y0 = xy[:-1, 0], xy[:-1, 1]
        x1, y1 = xy[1:, 0], xy[1:, 1]
        for ax, ay, bx, by in zip(x0, y0, x1, y1):
            if ay == by:
                continue                                   # horizontal edge: no crossing
            cross = (ay > Y) != (by > Y)
            xi = ax + (Y - ay) * (bx - ax) / (by - ay)     # x of the edge at the centre's y
            inside ^= cross & (X < xi)
        m[r0:r1, c0:c1] |= inside
    return m


def dilate(mask, iterations):
    """Approximate disk dilation (alternating square / cross), numpy only."""
    m = mask.copy()
    for i in range(int(iterations)):
        n = m.copy()
        n[1:, :] |= m[:-1, :]; n[:-1, :] |= m[1:, :]
        n[:, 1:] |= m[:, :-1]; n[:, :-1] |= m[:, 1:]
        if i % 2 == 1:                       # add diagonals every other pass -> octagon
            n[1:, 1:] |= m[:-1, :-1]; n[:-1, :-1] |= m[1:, 1:]
            n[1:, :-1] |= m[:-1, 1:]; n[:-1, 1:] |= m[1:, :-1]
        m = n
    return m


def boundary_ring(valid):
    """Cells of `valid` that touch a non-valid cell or the array edge."""
    v = valid
    inner = v.copy()
    inner[0, :] = inner[-1, :] = False
    inner[:, 0] = inner[:, -1] = False
    inner[1:, :] &= v[:-1, :]; inner[:-1, :] &= v[1:, :]
    inner[:, 1:] &= v[:, :-1]; inner[:, :-1] &= v[:, 1:]
    return v & ~inner


def crop_box(mask, pad=2):
    rows = np.where(mask.any(axis=1))[0]
    cols = np.where(mask.any(axis=0))[0]
    if len(rows) == 0:
        return None
    r0, r1 = max(rows[0] - pad, 0), min(rows[-1] + pad + 1, mask.shape[0])
    c0, c1 = max(cols[0] - pad, 0), min(cols[-1] + pad + 1, mask.shape[1])
    return r0, r1, c0, c1


def hillshade(dem, cell, az=315.0, alt=45.0):
    z = np.where(np.isfinite(dem) & (dem > NODATA + 1), dem, np.nan)
    fill = np.nanmean(z) if np.isfinite(z).any() else 0.0
    z = np.where(np.isnan(z), fill, z)
    gy, gx = np.gradient(z, cell)
    slope = np.arctan(np.hypot(gx, gy))
    aspect = np.arctan2(-gx, gy)
    a, e = math.radians(az), math.radians(alt)
    hs = np.sin(e) * np.cos(slope) + np.cos(e) * np.sin(slope) * np.cos(a - aspect)
    return np.clip(hs, 0, 1)
