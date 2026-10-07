"""Terrain helpers: where to put the breach inflow, which way the flood goes,
and how big the model domain has to be."""
import heapq
import math

import numpy as np

from .grid import Grid

_NB = [(-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1)]


def priority_flood(dem):
    """Fill depressions so every cell drains to the grid edge (Barnes et al.
    2014, with a tiny increment so flats drain). NaN cells act as outlets."""
    a = np.asarray(dem, dtype=float)
    ny, nx = a.shape
    filled = a.copy()
    closed = np.zeros(a.shape, dtype=bool)
    heap = []
    nan = ~np.isfinite(a)
    edge = np.zeros(a.shape, dtype=bool)
    edge[0, :] = edge[-1, :] = edge[:, 0] = edge[:, -1] = True
    # cells next to NaN are outlets too
    if nan.any():
        from scipy.ndimage import binary_dilation
        edge |= binary_dilation(nan, structure=np.ones((3, 3), bool))
    edge &= ~nan
    closed[nan] = True
    for r, c in zip(*np.nonzero(edge)):
        heapq.heappush(heap, (filled[r, c], r, c))
        closed[r, c] = True
    eps = 1e-5
    while heap:
        z, r, c = heapq.heappop(heap)
        for dr, dc in _NB:
            rr, cc = r + dr, c + dc
            if 0 <= rr < ny and 0 <= cc < nx and not closed[rr, cc]:
                closed[rr, cc] = True
                if filled[rr, cc] <= z:
                    filled[rr, cc] = z + eps
                heapq.heappush(heap, (filled[rr, cc], rr, cc))
    return filled


def trace_downstream(grid, start_xy, max_length_m):
    """Steepest-descent path on the depression-filled DEM from start_xy until
    max_length_m or the grid edge. Returns a list of (x, y)."""
    filled = priority_flood(grid.masked())
    r, c = grid.rc(*start_xy)
    if not grid.inside(r, c):
        raise ValueError("the start point is outside the planning DEM")
    path = [grid.xy(r, c)]
    length = 0.0
    visited = {(r, c)}
    ny, nx = filled.shape
    while length < max_length_m:
        best, best_slope = None, 0.0
        for dr, dc in _NB:
            rr, cc = r + dr, c + dc
            if not (0 <= rr < ny and 0 <= cc < nx) or (rr, cc) in visited:
                continue
            if not np.isfinite(filled[rr, cc]):
                continue
            dist = math.hypot(dr, dc) * grid.dx
            s = (filled[r, c] - filled[rr, cc]) / dist
            if s > best_slope:
                best, best_slope = (rr, cc), s
        if best is None:
            break
        length += math.hypot(best[0] - r, best[1] - c) * grid.dx
        r, c = best
        visited.add(best)
        path.append(grid.xy(r, c))
        if r in (0, ny - 1) or c in (0, nx - 1):
            break
    return path, length


def find_inflow_point(grid, dam_xy, dam_height_m, top_m, radius_m=None):
    """The breach inflow goes in just downstream of the embankment. Look for
    the lowest DEM cell within a radius of the dam point that is clearly below
    the reservoir (at least half the dam height below the top of dam).

    Returns ((x, y), note) or (None, reason)."""
    if radius_m is None:
        radius_m = max(90.0, 8.0 * dam_height_m)
    xs = grid.x_centers()
    ys = grid.y_centers()
    X, Y = np.meshgrid(xs, ys)
    d = np.hypot(X - dam_xy[0], Y - dam_xy[1])
    z = grid.masked()
    sel = (d <= radius_m) & np.isfinite(z)
    if not sel.any():
        return None, "no DEM cells within {:.0f} m of the dam point".format(radius_m)
    zs = np.where(sel, z, np.inf)
    r, c = np.unravel_index(np.argmin(zs), zs.shape)
    zmin = z[r, c]
    if zmin > top_m - 0.5 * dam_height_m:
        return None, ("the lowest ground within {:.0f} m of the dam point is only {:.1f} ft "
                      "below the top of dam; give the inflow point below the dam"
                      .format(radius_m, (top_m - zmin) / 0.3048))
    return (float(X[r, c]), float(Y[r, c])), (
        "lowest ground within {:.0f} m of the dam point, {:.1f} ft below the top of dam"
        .format(radius_m, (top_m - zmin) / 0.3048))


def flow_direction(path):
    """Unit vector of the first ~200 m of a traced path."""
    if len(path) < 2:
        return (1.0, 0.0)
    x0, y0 = path[0]
    acc = 0.0
    x1, y1 = path[-1]
    for i in range(1, len(path)):
        acc += math.hypot(path[i][0] - path[i - 1][0], path[i][1] - path[i - 1][1])
        if acc >= 200.0:
            x1, y1 = path[i]
            break
    dx, dy = x1 - x0, y1 - y0
    n = math.hypot(dx, dy) or 1.0
    return (dx / n, dy / n)


def path_bounds(path, buffer_m, snap=1.0):
    xs = [p[0] for p in path]
    ys = [p[1] for p in path]
    b = [min(xs) - buffer_m, min(ys) - buffer_m, max(xs) + buffer_m, max(ys) + buffer_m]
    return tuple(math.floor(v / snap) * snap if i < 2 else math.ceil(v / snap) * snap
                 for i, v in enumerate(b))


# ----------------------------------------------------------------------------
# Polygons from masks (for the ANUGA mesh)
# ----------------------------------------------------------------------------
def douglas_peucker(points, tol):
    pts = np.asarray(points, dtype=float)
    if len(pts) < 3:
        return pts
    keep = np.zeros(len(pts), dtype=bool)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        i, j = stack.pop()
        if j <= i + 1:
            continue
        a, b = pts[i], pts[j]
        ab = b - a
        n = np.hypot(*ab)
        seg = pts[i + 1:j]
        if n == 0:
            d = np.hypot(*(seg - a).T)
        else:
            d = np.abs(ab[0] * (seg[:, 1] - a[1]) - ab[1] * (seg[:, 0] - a[0])) / n
        k = int(np.argmax(d))
        if d[k] > tol:
            m = i + 1 + k
            keep[m] = True
            stack.append((i, m))
            stack.append((m, j))
    return pts[keep]


def _have_contourpy():
    try:
        import contourpy  # noqa: F401
        return True
    except ImportError:
        return False


def _outline_contourpy(grid, mask):
    import contourpy
    m = np.pad(mask.astype(float), 1)
    xs = np.concatenate([[grid.x_centers()[0] - grid.dx], grid.x_centers(),
                         [grid.x_centers()[-1] + grid.dx]])
    ys = np.concatenate([[grid.y_centers()[0] + grid.dx], grid.y_centers(),
                         [grid.y_centers()[-1] - grid.dx]])
    gen = contourpy.contour_generator(xs, ys, m)
    lines = gen.lines(0.5)
    if not lines:
        raise ValueError("empty mask")
    return max(lines, key=lambda l: abs(_area(l)))


def _outline_cells(grid, mask):
    """Built-in outline (3.4.4): the cell-edge boundary of the largest region of `mask`
    (8-connected, holes filled, corner-only contacts closed so that the ring is a simple
    polygon). Used when contourpy is not installed - QGIS 3.28's Python has none (found by the
    real-QGIS smoke run on macOS, 2026-10-02). Follows cell edges where contourpy cuts corners
    at the cell-edge midpoints; after the simplification the two outlines lie within about one
    cell plus the simplification tolerance of each other (measured on blobs and a flood-shaped
    mask, 10 m cells: 9-12 m apart with a 10 m tolerance; 18 m where a corner-only contact was
    closed)."""
    m = np.asarray(mask, bool)
    if not m.any():
        raise ValueError("empty mask")
    try:
        from scipy import ndimage
        lab, n = ndimage.label(m, structure=np.ones((3, 3), bool))
        if n > 1:
            sizes = np.bincount(lab.ravel())
            sizes[0] = 0
            m = lab == int(np.argmax(sizes))
        m = ndimage.binary_fill_holes(m)
    except ImportError:                  # without scipy: the largest ring of the whole mask
        pass
    m = np.pad(m, 1)
    for _ in range(50):                  # close corner-only contacts (pinch vertices)
        a, b, c, d = m[:-1, :-1], m[:-1, 1:], m[1:, :-1], m[1:, 1:]
        p1 = a & d & ~b & ~c
        p2 = b & c & ~a & ~d
        if not (p1.any() or p2.any()):
            break
        m[:-1, 1:] |= p1
        m[:-1, :-1] |= p2
    # directed boundary edges between cell corners (row, col of the padded array): every
    # vertex then has exactly one outgoing edge, so the rings follow without ambiguity
    nxt = {}
    rr, cc = np.nonzero(m)
    for r, c in zip(rr.tolist(), cc.tolist()):
        if not m[r - 1, c]:
            nxt[(r, c)] = (r, c + 1)
        if not m[r, c + 1]:
            nxt[(r, c + 1)] = (r + 1, c + 1)
        if not m[r + 1, c]:
            nxt[(r + 1, c + 1)] = (r + 1, c)
        if not m[r, c - 1]:
            nxt[(r + 1, c)] = (r, c)
    best, best_area = None, -1.0
    x_left = float(grid.x_centers()[0]) - 0.5 * grid.dx
    y_top = float(grid.y_centers()[0]) + 0.5 * grid.dx
    while nxt:
        start, _ = next(iter(nxt.items()))
        ring, v = [], start
        while v in nxt:
            ring.append(v)
            v = nxt.pop(v)
        pts = np.array([[x_left + (c - 1) * grid.dx, y_top - (r - 1) * grid.dx] for r, c in ring])
        a = abs(_area(pts)) if len(pts) >= 3 else 0.0
        if a > best_area:
            best, best_area = pts, a
    if best is None or len(best) < 3:
        raise ValueError("empty mask")
    return best


def mask_outline(grid, mask, simplify_m, backend=None):
    """Outer boundary of the largest region of `mask` as an (N, 2) array of
    x, y (open ring, counter-clockwise). contourpy when it is installed, else the built-in
    cell-edge tracer (`backend` "contourpy" / "cells" forces one; tests)."""
    use = backend or ("contourpy" if _have_contourpy() else "cells")
    ring = _outline_contourpy(grid, mask) if use == "contourpy" else _outline_cells(grid, mask)
    if np.allclose(ring[0], ring[-1]):
        ring = ring[:-1]
    ring = douglas_peucker(np.vstack([ring, ring[:1]]), simplify_m)[:-1]
    if _area(ring) < 0:
        ring = ring[::-1]
    return ring


def _area(ring):
    x, y = ring[:, 0], ring[:, 1]
    return 0.5 * float(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1)))


def disk_mask(grid, center_xy, radius_m):
    X, Y = np.meshgrid(grid.x_centers(), grid.y_centers())
    return np.hypot(X - center_xy[0], Y - center_xy[1]) <= radius_m


def dilate(mask, dist_m, dx):
    from scipy.ndimage import distance_transform_edt
    if dist_m <= 0:
        return mask.copy()
    return distance_transform_edt(~mask) * dx <= dist_m


def largest_component(mask, seed_rc=None):
    from scipy.ndimage import label
    lab, n = label(mask, structure=np.ones((3, 3), bool))
    if n <= 1:
        return mask
    if seed_rc is not None and lab[seed_rc] > 0:
        return lab == lab[seed_rc]
    sizes = np.bincount(lab.ravel())
    sizes[0] = 0
    return lab == int(np.argmax(sizes))


# ----------------------------------------------------------------------------
# Domain edges: does the flood leave the model deeper than the tolerance?
# ----------------------------------------------------------------------------
EDGES = ("N", "S", "E", "W")


def edge_depths(depth, valid=None):
    """Largest depth on each edge row/column of the grid (m)."""
    d = np.where(np.isfinite(depth), depth, 0.0)
    if valid is not None:
        d = np.where(valid, d, 0.0)
    return {"N": float(d[0, :].max()), "S": float(d[-1, :].max()),
            "W": float(d[:, 0].max()), "E": float(d[:, -1].max())}


def expand_bounds(bounds, edges, step_m, limits=None):
    xmin, ymin, xmax, ymax = bounds
    if "W" in edges:
        xmin -= step_m
    if "E" in edges:
        xmax += step_m
    if "S" in edges:
        ymin -= step_m
    if "N" in edges:
        ymax += step_m
    return (xmin, ymin, xmax, ymax)


def bounds_size(bounds):
    return bounds[2] - bounds[0], bounds[3] - bounds[1]
