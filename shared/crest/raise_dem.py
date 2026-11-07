"""Burn the crest into the model DEM so breach water cannot spill back over.

The source DEM is not changed: only the model copy gets the dam line raised
to the top of dam.  Two modes: a polyline (line_xy) that hits every cell under
it, or a point + length rectangle perpendicular to the inflow direction.
"""
import numpy as np


def _near_line(x, y, line_xy, width_m):
    """True if (x, y) is within width_m/2 of any segment of the dam line."""
    half = width_m / 2.0
    pts = list(line_xy)
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        vx, vy = x1 - x0, y1 - y0
        L2 = vx * vx + vy * vy
        t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((x - x0) * vx + (y - y0) * vy) / L2))
        d = ((x - (x0 + t * vx)) ** 2 + (y - (y0 + t * vy)) ** 2) ** 0.5
        if d <= half:
            return True
    return False


def raise_crest(dem, crest_xy, inflow_xy, top_m, length_m=200.0, width_m=12.0,
                line_xy=None):
    """Raise the crest to the top of dam in the model grid only.

    The dam line is burned across its length.  A point and a length are only
    the fallback (that fallback was raising one cell on a long embankment).
    Returns count of raised cells.
    """
    if dem is None or top_m is None or (crest_xy is None and not line_xy):
        return 0
    use_line = line_xy is not None and len(list(line_xy)) >= 2
    vx = vy = px = py = 0.0
    half = 0.0
    if not use_line:
        vx = (inflow_xy[0] - crest_xy[0]) if inflow_xy else 0.0
        vy = (inflow_xy[1] - crest_xy[1]) if inflow_xy else 1.0
        n = (vx * vx + vy * vy) ** 0.5 or 1.0
        px, py = -vy / n, vx / n
        vx, vy = vx / n, vy / n
        half = max(length_m, dem.dx) / 2.0
    data = dem.data
    xs = dem.x_centers()[None, :]
    ys = dem.y_centers()[:, None]
    if use_line:
        half_line = max(width_m, dem.dx) / 2.0
        hit = np.zeros(data.shape, dtype=bool)
        pts = list(line_xy)
        for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
            sx, sy = x1 - x0, y1 - y0
            L2 = sx * sx + sy * sy
            if L2 == 0:
                t = 0.0
            else:
                t = np.clip(((xs - x0) * sx + (ys - y0) * sy) / L2, 0.0, 1.0)
            d = ((xs - (x0 + t * sx)) ** 2 + (ys - (y0 + t * sy)) ** 2) ** 0.5
            hit |= d <= half_line
    else:
        dx, dy = xs - crest_xy[0], ys - crest_xy[1]
        along = dx * px + dy * py
        across = np.abs(dx * vx + dy * vy)
        hit = (np.abs(along) <= half) & (across <= width_m / 2.0)
    raise_mask = hit & (data == data) & (data < top_m)
    data[raise_mask] = top_m
    return int(np.count_nonzero(raise_mask))
