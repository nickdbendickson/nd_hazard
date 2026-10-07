# -*- coding: utf-8 -*-
"""v2.9.0 fast sampling: the raster cells of a structure's evaluation area
(whole area / buffer ring / band along the wall) without GEOS.

numpy + the standard library only (no QGIS, GEOS, GDAL, scipy or shapely),
so it can be imported and tested anywhere.

WHAT IT COMPUTES
----------------
For each structure: the cells of one window of the raster (the structure's
bbox grown by `reach`, clipped to the raster) whose CENTRE lies in the
evaluation region of the requested mode. With P the footprint (polygon /
multipolygon with holes, or a disc of radius r around a point), d(c) the
exact Euclidean distance from the centre c to the boundary of P, and
inside(c) the even-odd point-in-polygon test (union over the parts):

  area : inside(c) or d(c) <= reach
  ring : not inside(c) and d(c) <= reach
  band : (not inside(c) and d(c) <= reach) or (inside(c) and d(c) <= edge)
         (outlines: reach == edge, so simply d(c) <= edge)
  disc : area |c-p| <= reach; ring r < |c-p| <= reach;
         band r - edge < |c-p| <= reach

which is the exact-distance definition of what structures.py builds with
GEOS: buffer(P, reach) [minus P (ring) | minus buffer(P, -edge) (band)], or
for a point buffer(p, reach) minus buffer(p, r) / buffer(p, r - edge). As
there, a point whose inner radius is <= 0 (and an outline with reach <= 0
(ring) or edge <= 0 (band)) has no cut: its region IS the whole area
(CellSet.whole, the GEOS path's `_sample_region() -> None`).

TIE RULE (a centre exactly on a boundary)
-----------------------------------------
The plugin selects cells by rasterising the GEOS region with GDAL
(RasterizeLayer, cell centres, no ALL_TOUCHED). GDAL's scanline fill counts
an edge for a row when min(y) <= y_centre < max(y) (pixel rows) and fills a
cell when its centre is strictly right of the crossing - i.e. a boundary
centre is treated as if nudged an infinitesimal step towards -column (and
an even smaller step towards +row). The same nudge decides every tie here:
  * inside(): GDAL's rule exactly (half-open rows, strict crossing x), in
    pixel orientation. North-up raster: a centre on a north or east wall is
    inside, on a south or west wall outside.
  * d(c) == threshold (reach / edge / disc radius): counts as within iff
    the nudge brings it closer, i.e. with v = c - q (q the nearest boundary
    point; the disc centre for discs) in pixel orientation (x = column,
    y = row): v.x > 0, or v.x == 0 and v.y < 0.
On the straight parts of the region's boundary (offsets of the walls,
which GEOS computes exactly) this reproduces GDAL's result for the GEOS
polygon. Not emulated: GDAL additionally burns cells whose centre lies
exactly on a HORIZONTAL region edge that runs towards -column (depends on
the ring orientation GEOS outputs). Ties are detected on float64 values: a
centre within rounding error (~1e-9 m) of a boundary is decided by the
rounding, as in GEOS / GDAL.

DIFFERENCES FROM THE GEOS PATH (quadsegs = 8, structures.py buffer(d, 8))
-------------------------------------------------------------------------
GEOS replaces every arc of the buffer by chords whose vertices lie ON the
arc, so the GEOS region lies inside the exact one along arcs:
  * convex corners of the outline (area / ring / band outer boundary) and
    the whole circle of a point (reach): the fast path adds the cells whose
    centre is in the arc/chord gap;
  * band inner boundary of an outline (the eroded outline has arcs around
    reflex corners): the fast path adds the gap cells to the band;
  * point ring / band inner disc (r / r - edge): GEOS cuts a 32-gon inside
    the circle, the fast path removes the gap cells from the ring / band.
Radial width of the gap: R * (1 - cos(pi / (4 * quadsegs))) for right-angle
corners and discs (0.0048153 R: 7.3 mm at 5 ft, 36.7 mm at a 25-ft point
reach); GEOS rounds the number of fillet segments (nSegs = round(angle /
quantum)), so at a shallow corner (turn 8.4..16.9 deg) the step can reach
1.5 quanta: <= R * (1 - cos(3 pi / (8 quadsegs))) = 0.010823 R (16.5 mm at
5 ft). Gap area per full turn: (pi - 16 sin(pi/16)) R^2 = 0.020147 R^2
(0.047 m^2 for a rectangle at 5 ft, 1.17 m^2 for a 25-ft point disc).
See geos_arc_gap(). Otherwise identical cell sets, except: invalid
geometry (GEOS may repair or drop a self-intersecting lobe; here even-odd
per part, parts unioned), GEOS's noding / snapping rounding, and the
horizontal-edge GDAL tie above. Multipart: parts are united (inside = any part; distance to all edges); an
overlapping (invalid) multipolygon has no defined GEOS erosion - the band then
leaves out the cells deeper than the edge inside ANY part (disjoint parts:
identical to GEOS). Identical: holes, valid multipart unions, walls
thinner than the band (GEOS: empty erosion -> whole area; here the band is
then the whole area too - only the region_kind label differs).

USE
---
    shp = shape_from_geometry(rec["geom"], r_disc)       # None -> GEOS path
    cs = structure_cells(shp, gt, reach, edge, "ring", (xsize, ysize))
    cs.mask / cs.area: bool (h, w) windows at raster (cs.r0, cs.c0);
    cs.rows_cols(), cs.centres(gt), cs.empty, cs.whole
    many at once: structures_cells([...], gt, reaches, edge, mode, size)
    with a RasterSampler: sample_cells({sid: (shape, reach)}, dep, mode, edge)
All functions return None (never raise for bad input) when the fast path
cannot be used: rotated / sheared / degenerate geotransform, non-finite or
unsupported geometry (curves, collections) - the caller uses GEOS.

INTEGRATION (structures.evaluate_structures): see sample_cells().
"""

import math
import struct

import numpy as np

AREA, RING, BAND = "area", "ring", "band"        # == structures.SAMPLE_AREA / RING / BAND
MODES = (AREA, RING, BAND)
GEOS_QUADSEGS = 8                                 # structures.py: geom.buffer(d, 8)
CHUNK_PAIRS = 1 << 17                             # cell x edge pairs per numpy block
ONEPASS_MIN = 8                                   # as structures.ONEPASS_MIN
_EPS_PIX = 1e-7                                   # window slack (pixels): ties stay inside


def geos_arc_gap(radius, quadsegs=GEOS_QUADSEGS, worst=False):
    """Largest radial distance between an arc of `radius` and the chords
    GEOS draws for it (buffer(d, quadsegs)): R (1 - cos(pi / (4 quadsegs)))
    for right-angle corners and point discs; with worst=True the bound for
    any corner (GEOS may stretch a fillet step to 1.5 quanta)."""
    half = (3.0 if worst else 2.0) * math.pi / (8.0 * quadsegs)
    return float(radius) * (1.0 - math.cos(half))


# ------------------------------------------------------------------ shapes --
class Shape(object):
    """A structure footprint in map (raster CRS) coordinates: an outline
    (kind "poly": xy (n, 2) vertices of every ring of every part, each ring
    implicitly closed; rings / parts = vertex offsets) or a point with a
    disc radius (kind "disc": x, y, radius). Build with polygon_shape(),
    disc_shape() or shape_from_geometry()."""
    __slots__ = ("kind", "xy", "rings", "parts", "x", "y", "radius", "bbox")

    def __init__(self, kind):
        self.kind = kind
        self.xy = None
        self.rings = self.parts = ()
        self.x = self.y = self.radius = None
        self.bbox = None

    @property
    def n_edges(self):
        return 0 if self.xy is None else int(self.xy.shape[0])

    def edges(self):
        """(ax, ay, bx, by): every edge A -> B of every ring."""
        nxt = np.arange(1, self.n_edges + 1)
        for a, b in zip(self.rings[:-1], self.rings[1:]):
            nxt[b - 1] = a
        return self.xy[:, 0], self.xy[:, 1], self.xy[nxt, 0], self.xy[nxt, 1]


def disc_shape(x, y, radius):
    """A point structure: the disc of `radius` (map units) around (x, y).
    None when a value is not finite."""
    try:
        x, y, radius = float(x), float(y), float(radius)
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(x) and math.isfinite(y) and math.isfinite(radius)):
        return None
    s = Shape("disc")
    s.x, s.y, s.radius = x, y, radius
    s.bbox = (x, y, x, y)
    return s


def _pt(p):
    if hasattr(p, "x") and callable(p.x):          # QgsPointXY / QgsPoint
        return float(p.x()), float(p.y())
    return float(p[0]), float(p[1])


def _is_pt(o):
    if hasattr(o, "x") and callable(getattr(o, "x")):
        return True
    try:
        float(o[0])
        return True
    except (TypeError, ValueError, IndexError):
        return False


def _ring_array(ring):
    if isinstance(ring, np.ndarray):
        if ring.ndim != 2 or ring.shape[1] < 2:
            raise ValueError("a ring must be an (n, 2) array")
        if ring.dtype == np.float64 and ring.shape[1] == 2 and ring.flags.c_contiguous:
            return ring
        return np.ascontiguousarray(ring[:, :2], dtype=np.float64)
    return np.array([_pt(p) for p in ring], dtype=np.float64).reshape(-1, 2)


def _nest_depth(obj):
    """1: a ring, 2: a polygon (rings), 3: a multipolygon (polygons);
    0 when empty / not understood."""
    d, o = 0, obj
    while not _is_pt(o):
        try:
            o = o[0]
        except (TypeError, IndexError, KeyError):
            return 0
        d += 1
        if d > 3:
            return 0
    return d


def polygon_shape(parts):
    """An outline from rings in map coordinates: a ring ((n, 2) array or a
    sequence of (x, y) / QgsPointXY), a polygon (exterior ring + holes) or a
    multipolygon (list of polygons). Rings may be open or closed; empty
    rings are skipped. None when a coordinate is not finite or nothing is
    left."""
    try:
        depth = _nest_depth(parts)
    except Exception:
        return None
    if depth == 1:
        return _build_poly([[parts]])
    if depth == 2:
        return _build_poly([parts])
    if depth == 3:
        return _build_poly(parts)
    return None


def _build_poly(polys):
    """Shape of [[ring, ...], ...] (see polygon_shape)."""
    try:
        rings, roff, poff, n = [], [0], [0], 0
        for poly in polys:
            for ring in poly:
                a = _ring_array(ring)
                m = a.shape[0]
                if m > 1 and a[0].tolist() == a[m - 1].tolist():
                    m -= 1                                  # closed ring: drop the repeat
                if m == 0:
                    continue
                rings.append(a[:m])
                n += m
                roff.append(n)
            if n > poff[-1]:
                poff.append(n)
        if n == 0:
            return None
        xy = rings[0] if len(rings) == 1 else np.concatenate(rings)
        lo, hi = xy.min(axis=0).tolist(), xy.max(axis=0).tolist()
    except (TypeError, ValueError, IndexError):
        return None
    bbox = (lo[0], lo[1], hi[0], hi[1])
    if not all(math.isfinite(v) for v in bbox):             # NaN / inf anywhere
        return None
    s = Shape("poly")
    s.xy, s.rings, s.parts, s.bbox = xy, tuple(roff), tuple(poff), bbox
    return s


def _wkb_read(buf, off):
    """(kind, value, new offset) of one WKB geometry: ("point", (x, y)),
    ("polygon", [ring arrays]), ("multipolygon", [[ring arrays], ...]),
    ("multipoint", [(x, y), ...]). ISO / OGC codes with Z / M / ZM and the
    EWKB flags; other types raise ValueError."""
    order = buf[off]
    e = "<" if order == 1 else ">"
    (t,) = struct.unpack_from(e + "I", buf, off + 1)
    off += 5
    has_z, has_m = bool(t & 0x80000000), bool(t & 0x40000000)
    if t & 0x20000000:                                   # EWKB SRID
        off += 4
    t &= 0x0FFFFFFF
    base, iso = t % 1000, t // 1000
    has_z = has_z or iso in (1, 3)
    has_m = has_m or iso in (2, 3)
    dims = 2 + int(has_z) + int(has_m)
    if base == 1:
        v = struct.unpack_from(e + "%dd" % dims, buf, off)
        return "point", (v[0], v[1]), off + 8 * dims
    if base == 3:
        (nr,) = struct.unpack_from(e + "I", buf, off)
        off += 4
        fmt = np.dtype(e + "f8")
        rings = []
        for _i in range(nr):
            (npt,) = struct.unpack_from(e + "I", buf, off)
            off += 4
            a = np.frombuffer(buf, dtype=fmt, count=npt * dims, offset=off).reshape(npt, dims)
            off += 8 * npt * dims
            rings.append(np.ascontiguousarray(a[:, :2], dtype=np.float64))
        return "polygon", rings, off
    if base in (4, 6):
        (n,) = struct.unpack_from(e + "I", buf, off)
        off += 4
        items = []
        for _i in range(n):
            kind, val, off = _wkb_read(buf, off)
            if kind != ("point" if base == 4 else "polygon"):
                raise ValueError("unexpected part in a multi geometry")
            items.append(val)
        return ("multipoint" if base == 4 else "multipolygon"), items, off
    raise ValueError("WKB type {} is not supported".format(t))


def shape_from_wkb(wkb, radius=None):
    """Shape of WKB bytes: Polygon / MultiPolygon -> outline; Point (or a
    one-point MultiPoint) -> disc of `radius` (None: no disc -> None).
    None for anything else, empty or non-finite geometry."""
    try:
        buf = bytes(wkb)
        if len(buf) < 5:
            return None
        kind, val, _off = _wkb_read(buf, 0)
    except (ValueError, struct.error, IndexError, TypeError):
        return None
    if kind == "multipoint":
        if len(val) != 1:
            return None
        kind, val = "point", val[0]
    if kind == "point":
        return None if radius is None else disc_shape(val[0], val[1], radius)
    if kind == "polygon":
        return _build_poly([val]) if val else None
    if kind == "multipolygon":
        polys = [p for p in val if p]
        return _build_poly(polys) if polys else None
    return None


def shape_from_geometry(geom, radius=None):
    """Shape of a QgsGeometry (map / raster CRS), read through its WKB (no
    per-vertex Python objects): outlines as they are, a point as the disc of
    `radius` (structures.py's r_disc; None for an outline). None when the
    geometry is null / empty / curved / unsupported - use GEOS for it."""
    try:
        if geom is None or geom.isNull() or geom.isEmpty():
            return None
        wkb = geom.asWkb()
    except Exception:
        return None
    if isinstance(wkb, (bytes, bytearray)):
        data = bytes(wkb)
    elif hasattr(wkb, "data"):                             # PyQt QByteArray
        data = bytes(wkb.data())
    else:
        try:
            data = bytes(wkb)
        except Exception:
            return None
    return shape_from_wkb(data, radius)


# -------------------------------------------------------------------- grid --
class _Grid(object):
    __slots__ = ("gt", "sx", "sy", "sgx", "sgy", "xsize", "ysize")


def _grid(gt, size=None):
    """Axis-aligned raster grid, or None (rotated / sheared / degenerate)."""
    try:
        g = tuple(float(v) for v in gt)
    except (TypeError, ValueError):
        return None
    if len(g) != 6 or not all(math.isfinite(v) for v in g):
        return None
    if g[2] != 0.0 or g[4] != 0.0 or g[1] == 0.0 or g[5] == 0.0:
        return None
    gr = _Grid()
    gr.gt = g
    gr.sx, gr.sy = abs(g[1]), abs(g[5])
    gr.sgx = 1.0 if g[1] > 0 else -1.0
    gr.sgy = 1.0 if g[5] > 0 else -1.0
    gr.xsize = gr.ysize = None
    if size is not None:
        gr.xsize, gr.ysize = int(size[0]), int(size[1])
    return gr


def grid_ok(gt):
    """True when the geotransform can be used (no rotation / shear)."""
    return _grid(gt) is not None


def _windows(gr, bb, reach):
    """(r0, c0, h, w) int arrays: the cells whose centre can lie within
    `reach` of the bboxes bb (S, 4), clipped to the raster."""
    g = gr.gt
    xa, ya, xb, yb = bb[:, 0] - reach, bb[:, 1] - reach, bb[:, 2] + reach, bb[:, 3] + reach
    u1, u2 = (xa - g[0]) / g[1], (xb - g[0]) / g[1]
    v1, v2 = (ya - g[3]) / g[5], (yb - g[3]) / g[5]
    c0 = np.ceil(np.minimum(u1, u2) - 0.5 - _EPS_PIX)
    c1 = np.floor(np.maximum(u1, u2) - 0.5 + _EPS_PIX)
    r0 = np.ceil(np.minimum(v1, v2) - 0.5 - _EPS_PIX)
    r1 = np.floor(np.maximum(v1, v2) - 0.5 + _EPS_PIX)
    if gr.xsize is not None:
        c0, c1 = np.maximum(c0, 0), np.minimum(c1, gr.xsize - 1)
        r0, r1 = np.maximum(r0, 0), np.minimum(r1, gr.ysize - 1)
    w = np.maximum(c1 - c0 + 1, 0)
    h = np.maximum(r1 - r0 + 1, 0)
    return r0.astype(np.int64), c0.astype(np.int64), h.astype(np.int64), w.astype(np.int64)


def _origin(gr, r0, c0):
    """Map x, y of a window's top-left corner (the plugin's _window_gt).
    Local (pixel-oriented, metric) coordinates are x' = (x - xw) sgx and
    y' = (y - yw) sgy; the centre of window cell (i, j) is at
    ((j + 0.5) sx, (i + 0.5) sy)."""
    g = gr.gt
    return g[0] + c0 * g[1] + r0 * g[2], g[3] + c0 * g[4] + r0 * g[5]


# ------------------------------------------------------------------ result --
class CellSet(object):
    """The cells of one structure, on the window of rows r0..r0+h-1 and
    columns c0..c0+w-1 of the raster.
      mask  bool (h, w): centre in the region of `mode`
      area  bool (h, w): centre in the whole area (outline + reach) - what the
            plugin reads instead when the ring / band holds no (valid) cell
      whole True when the mode has no cut by definition (mask is area)
      empty True when mask holds no cell (grid coarser than the ring / band)
    """
    __slots__ = ("r0", "c0", "mask", "area", "mode", "whole")

    def __init__(self, r0, c0, mask, area, mode, whole=False):
        self.r0, self.c0 = int(r0), int(c0)
        self.mask, self.area, self.mode, self.whole = mask, area, mode, bool(whole)

    @property
    def shape(self):
        return self.mask.shape

    @property
    def empty(self):
        return not self.mask.any()

    @property
    def area_empty(self):
        return not self.area.any()

    def rows_cols(self, which="mask"):
        """Raster (row, col) index arrays of the cells, row-major."""
        rr, cc = np.nonzero(self.area if which == "area" else self.mask)
        return rr + self.r0, cc + self.c0

    def centres(self, gt, which="mask"):
        """Map x, y of the cell centres (the plugin's formula)."""
        rr, cc = self.rows_cols(which)
        colc, rowc = cc + 0.5, rr + 0.5
        return (gt[0] + colc * gt[1] + rowc * gt[2], gt[3] + colc * gt[4] + rowc * gt[5])


# -------------------------------------------------------------------- core --
class _Edges(object):
    """Local edge arrays (G, E) of a block and their constant terms."""
    __slots__ = ("AX", "AY", "BX", "BY", "EX", "EY", "L2", "L2s", "X1", "Y1", "Y2", "SLOPE")

    def __init__(self, AX, AY, BX, BY):
        self.AX, self.AY, self.BX, self.BY = AX, AY, BX, BY
        self.EX = BX - AX
        self.EY = BY - AY
        self.L2 = self.EX * self.EX + self.EY * self.EY
        self.L2s = np.where(self.L2 > 0, self.L2, 1.0)
        up = AY < BY                                   # GDAL: (x1, y1) = lower-y end
        self.X1 = np.where(up, AX, BX)
        self.Y1 = np.where(up, AY, BY)
        self.Y2 = np.where(up, BY, AY)
        self.SLOPE = np.where(up, BX, AX) - self.X1    # x2 - x1

    def sub(self, g=None, e=None):
        """The edges of rows g (fancy / slice) and columns e (slice)."""
        out = _Edges.__new__(_Edges)
        for k in _Edges.__slots__:
            v = getattr(self, k)
            if g is not None:
                v = v[g]
            if e is not None:
                v = v[..., e]
            setattr(out, k, v)
        return out


def _block(PX, PY, ed):
    """Min squared distance to the edges and the even-odd parity of the
    window cells: PX (W,), PY (H,) local centre coordinates, edges (G, E).
    Returns d2, odd: (G, H, W).

    Separable: everything that depends on the column only (E, G, W) or on
    the row only (E, G, H) is computed once; each edge then costs a few
    passes over the (G, H, W) slab (accumulated in place, no 4-D arrays).
    Parity: GDAL's scanline rule - an edge counts for a row when
    y1 <= y < y2 (y1 < y2 its end rows), and crosses left of the centre when
    x1 + (y - y1)(x2 - x1)/(y2 - y1) < x (GDAL's formula).
    Distance: min over the vertices |P - A|^2 (every edge's B is the next
    edge's A in its ring) and, where the perpendicular foot is strictly
    inside the edge ((P - A).E > 0 and (P - B).E < 0, decided exactly),
    cross^2 / L^2 - exact whenever the true value is representable (no
    rounded projection parameter), so ties are exact."""
    G, E = ed.AX.shape
    H, W = PY.size, PX.size
    T = lambda v: v.T[:, :, None]                       # (G, E) -> (E, G, 1)
    X, Y = PX[None, None, :], PY[None, None, :]
    dx, dy = X - T(ed.AX), Y - T(ed.AY)                 # (E, G, W), (E, G, H)
    ex, ey = T(ed.EX), T(ed.EY)
    dx2, dy2 = dx * dx, dy * dy
    dxex, dxey, dyex, dyey = dx * ex, dx * ey, dy * ex, dy * ey
    nbyey = -((Y - T(ed.BY)) * ey)                      # (P - B).E < 0 <=> bxex < nbyey
    bxex = (X - T(ed.BX)) * ex
    nayey = -dyey                                       # (P - A).E > 0 <=> dxex > nayey
    Y1, Y2 = T(ed.Y1), T(ed.Y2)
    with np.errstate(divide="ignore", invalid="ignore"):
        xi = (Y - Y1) * T(ed.SLOPE)
        xi /= (Y2 - Y1)
        xi += T(ed.X1)
    xi = np.where((Y1 <= Y) & (Y < Y2), xi, np.inf)    # (E, G, H)
    l2s = ed.L2s.T[:, :, None, None]
    d2 = np.full((G, H, W), np.inf)
    odd = np.zeros((G, H, W), dtype=bool)
    dA = np.empty((G, H, W))
    c = np.empty((G, H, W))
    inter = np.empty((G, H, W), dtype=bool)
    tmp = np.empty((G, H, W), dtype=bool)
    for e in range(E):
        np.less(xi[e][:, :, None], PX[None, None, :], out=tmp)
        odd ^= tmp
        np.greater(dxex[e][:, None, :], nayey[e][:, :, None], out=inter)
        np.less(bxex[e][:, None, :], nbyey[e][:, :, None], out=tmp)
        inter &= tmp
        np.subtract(dyex[e][:, :, None], dxey[e][:, None, :], out=c)
        c *= c
        c /= l2s[e]
        np.add(dy2[e][:, :, None], dx2[e][:, None, :], out=dA)
        np.minimum(dA, c, out=dA, where=inter)
        np.minimum(d2, dA, out=d2)
    return d2, odd


def _ties(PX, PY, ed, thr2):
    """For tie centres (d2min == thr2): True when a boundary point at exactly
    that distance is approached by the GDAL nudge, i.e. v = P - q has
    v.x > 0, or v.x == 0 and v.y < 0 (signs taken exactly). Same arithmetic
    as _block, so equal values compare equal. PX, PY (T, 1); edges (T, E)
    or (1, E); thr2 (T, 1) or scalar."""
    dx = PX - ed.AX
    dy = PY - ed.AY
    c = dy * ed.EX
    c -= dx * ed.EY
    inter = (dx * ed.EX > -(dy * ed.EY)) & ((PX - ed.BX) * ed.EX < -((PY - ed.BY) * ed.EY))
    dA = dy * dy
    dA += dx * dx
    dP = c * c
    dP /= ed.L2s
    near_a = (dx > 0) | ((dx == 0) & (dy < 0))            # v = P - A
    sc = np.sign(c)
    vxs = -sc * np.sign(ed.EY)                             # v ~ (c / L2) (-EY, EX)
    vys = sc * np.sign(ed.EX)
    near_p = (vxs > 0) | ((vxs == 0) & (vys < 0))
    hit = ((dA == thr2) & near_a) | (inter & (dP == thr2) & near_p)
    return hit.any(axis=-1)


def _modes(inside, wr, we_fn, mode, reach, edge):
    """(mask, area, whole) from inside / within-reach (and, for the band,
    a callable giving within-edge)."""
    area = inside | wr
    if mode == AREA:
        return area, area, False
    if mode == RING:
        if not reach > 0:
            return area, area, True
        return wr & ~inside, area, False
    if not edge > 0:
        return area, area, True
    return (wr & ~inside) | (we_fn() & inside), area, False


def _whole(shape, reach, edge, mode):
    """True when the ring / band of `mode` has no cut (GEOS _sample_region
    -> None): the region is the whole area."""
    if mode == AREA:
        return False
    if shape.kind == "disc":
        inner = shape.radius if mode == RING else shape.radius - edge
        return not inner > 0
    return not (reach if mode == RING else edge) > 0


def _local_edges(gr, shape, r0, c0):
    xw, yw = _origin(gr, r0, c0)
    ax, ay, bx, by = shape.edges()
    return _Edges(((ax - xw) * gr.sgx)[None], ((ay - yw) * gr.sgy)[None],
                  ((bx - xw) * gr.sgx)[None], ((by - yw) * gr.sgy)[None])


def _poly_single(shape, gr, r0, c0, h, w, reach, edge, mode, chunk):
    """One outline of any size, also multipart: blocks of rows x columns x
    edges of at most `chunk` pairs. Parts are united as GEOS buffers them:
    inside = any part's even-odd parity; outside, the distance to all edges
    is the distance to the union; the band's inner half is the union minus
    every part's erosion (only differs for invalid, overlapping parts)."""
    ed = _local_edges(gr, shape, r0, c0)
    E = shape.n_edges
    PXa = (np.arange(w) + 0.5) * gr.sx
    PYa = (np.arange(h) + 0.5) * gr.sy
    parts = shape.parts
    npart = len(parts) - 1
    d2p = [np.full((h, w), np.inf) for _p in range(npart)]
    insp = [np.zeros((h, w), dtype=bool) for _p in range(npart)]
    eb = max(1, min(E, 64, chunk))
    cells = max(1, chunk // eb)
    cb = max(1, min(w, cells))
    rb = max(1, cells // cb)
    for i0 in range(0, h, rb):
        i1 = min(h, i0 + rb)
        for j0 in range(0, w, cb):
            j1 = min(w, j0 + cb)
            for p in range(npart):
                dmin = d2p[p][i0:i1, j0:j1]
                odd = insp[p][i0:i1, j0:j1]
                for e0 in range(parts[p], parts[p + 1], eb):
                    sl = slice(e0, min(parts[p + 1], e0 + eb))
                    dm, od = _block(PXa[j0:j1], PYa[i0:i1], ed.sub(e=sl))
                    np.minimum(dmin, dm[0], out=dmin)
                    odd ^= od[0]
    d2, inside = d2p[0], insp[0]
    if npart > 1:
        d2 = np.minimum.reduce(d2p)
        inside = np.logical_or.reduce(insp)

    def within(thr, dd=d2, eds=ed):
        if not thr > 0:
            return np.zeros((h, w), dtype=bool)
        thr2 = thr * thr
        res = dd < thr2
        ii, jj = np.nonzero(dd == thr2)
        step = max(1, chunk // max(1, E))
        for a in range(0, ii.size, step):
            i, j = ii[a:a + step], jj[a:a + step]
            res[i, j] = _ties(PXa[j][:, None], PYa[i][:, None], eds, thr2)
        return res

    def band_inside():
        if npart == 1:
            return within(edge)
        eroded = np.zeros((h, w), dtype=bool)
        for p in range(npart):
            sl = slice(parts[p], parts[p + 1])
            eroded |= insp[p] & ~within(edge, d2p[p], ed.sub(e=sl))
        return ~eroded

    mask, area, whole = _modes(inside, within(reach), band_inside, mode, reach, edge)
    return CellSet(r0, c0, mask, area, mode, whole)


def _poly_group(run, gr, R0, C0, H, W, reach, edge, mode, gxy, gnxt, voff, ecnt, out):
    """Several small single-part outlines at once: (G, H, W, E) blocks,
    windows padded to the largest h and w, edges padded with zero-length
    edges at a vertex (they change neither the distance nor the parity)."""
    G = run.size
    h, w = H[run], W[run]
    Hm, Wm, E = int(h.max()), int(w.max()), int(ecnt[run].max())
    e = np.arange(E, dtype=np.int64)[None, :]
    cnt = ecnt[run][:, None]
    ia = voff[run][:, None] + np.minimum(e, cnt - 1)
    pad = e >= cnt
    ib = np.where(pad, ia, gnxt[ia])
    xw, yw = _origin(gr, R0[run], C0[run])
    xw, yw = xw[:, None], yw[:, None]
    ed = _Edges((gxy[ia, 0] - xw) * gr.sgx, (gxy[ia, 1] - yw) * gr.sgy,
                (gxy[ib, 0] - xw) * gr.sgx, (gxy[ib, 1] - yw) * gr.sgy)
    PX = (np.arange(Wm) + 0.5) * gr.sx
    PY = (np.arange(Hm) + 0.5) * gr.sy
    d2, inside = _block(PX, PY, ed)
    real = (np.arange(Hm)[None, :, None] < h[:, None, None]) & \
        (np.arange(Wm)[None, None, :] < w[:, None, None])
    d2[~real] = np.inf
    rch, edg = reach[run], edge[run]

    def within(thr):
        thr2 = (thr * thr)[:, None, None]
        ok = (thr > 0)[:, None, None]
        res = (d2 < thr2) & ok
        gi, ii, jj = np.nonzero((d2 == thr2) & ok)
        if gi.size:
            res[gi, ii, jj] = _ties(PX[jj][:, None], PY[ii][:, None], ed.sub(g=gi),
                                    thr2[gi, 0])
        return res

    wr = within(rch)
    area = inside | wr
    if mode == AREA:
        mask, whole = area, np.zeros(G, dtype=bool)
    elif mode == RING:
        whole = ~(rch > 0)
        mask = np.where(whole[:, None, None], area, wr & ~inside)
    else:
        whole = ~(edg > 0)
        mask = np.where(whole[:, None, None], area, (wr & ~inside) | (within(edg) & inside))
    for gi, s in enumerate(run.tolist()):
        hh, ww = int(h[gi]), int(w[gi])
        out[s] = CellSet(R0[s], C0[s], mask[gi, :hh, :ww], area[gi, :hh, :ww], mode,
                         bool(whole[gi]))


def _disc_group(shapes, run, gr, R0, C0, H, W, reach, edge, mode, out):
    """Several point discs at once: d2 = dx^2 + dy^2 on (G, H, W)."""
    G = run.size
    h, w = H[run], W[run]
    Hm, Wm = int(h.max()), int(w.max())
    PX = (np.arange(Wm) + 0.5) * gr.sx
    PY = (np.arange(Hm) + 0.5) * gr.sy
    xw, yw = _origin(gr, R0[run], C0[run])
    ids = run.tolist()
    px = np.array([shapes[s].x for s in ids])
    py = np.array([shapes[s].y for s in ids])
    rad = np.array([shapes[s].radius for s in ids])
    dx = PX[None, :] - ((px - xw) * gr.sgx)[:, None]          # (G, W)
    dy = PY[None, :] - ((py - yw) * gr.sgy)[:, None]          # (G, H)
    d2 = (dy * dy)[:, :, None] + (dx * dx)[:, None, :]
    near = (dx > 0)[:, None, :] | ((dx == 0)[:, None, :] & (dy < 0)[:, :, None])

    def within(r):
        r2 = (r * r)[:, None, None]
        return ((d2 < r2) | ((d2 == r2) & near)) & (r > 0)[:, None, None]

    area = within(reach[run])
    if mode == AREA:
        mask, whole = area, np.zeros(G, dtype=bool)
    else:
        inner = rad if mode == RING else rad - edge[run]
        whole = ~(inner > 0)
        mask = np.where(whole[:, None, None], area, area & ~within(np.maximum(inner, 0.0)))
    for gi, s in enumerate(ids):
        hh, ww = int(h[gi]), int(w[gi])
        out[s] = CellSet(R0[s], C0[s], mask[gi, :hh, :ww], area[gi, :hh, :ww], mode,
                         bool(whole[gi]))


def _groups(order, h, w, e, chunk, waste=1.4):
    """Split `order` (sorted by (e, h, w)) into runs with one edge count
    whose padded block G x max(h) x max(w) x e stays within `chunk` cell x
    edge pairs and wastes at most ~40 % on padding."""
    runs, start, L = [], 0, order.size
    hl, wl, el = h.tolist(), w.tolist(), e.tolist()
    oi = order.tolist()
    while start < L:
        s0 = oi[start]
        e0 = el[s0]
        hm, wm, tot = hl[s0], wl[s0], hl[s0] * wl[s0]
        end = start + 1
        while end < L:
            s = oi[end]
            if el[s] != e0:
                break
            hm2, wm2 = max(hm, hl[s]), max(wm, wl[s])
            tot2 = tot + hl[s] * wl[s]
            g = end - start + 1
            if g * hm2 * wm2 * max(e0, 1) > chunk or g * hm2 * wm2 > waste * tot2 + 64:
                break
            hm, wm, tot = hm2, wm2, tot2
            end += 1
        runs.append(order[start:end])
        start = end
    return runs


def structures_cells(shapes, gt, reach, edge, mode=AREA, size=None, chunk_pairs=CHUNK_PAIRS):
    """CellSets of many structures at once (list aligned with `shapes`;
    None where a shape is None). None altogether when the geotransform is
    rotated / sheared / degenerate (use the GEOS path).

    shapes: Shape objects (map coordinates of the raster CRS); reach, edge:
    map units, scalars or one value per shape (outlines: reach == edge;
    points: reach = radius + edge); mode: "area" / "ring" / "band"; size:
    (xsize, ysize) to clip the windows to the raster; chunk_pairs: the most
    cell x edge pairs in one numpy block (memory: ~30 bytes per window
    cell of a block, i.e. chunk_pairs / edges cells).
    Small single-part outlines are processed in blocks of many structures,
    large or multipart ones one by one in blocks of cells x edges; the
    result does not depend on chunk_pairs."""
    gr = _grid(gt, size)
    if gr is None:
        return None
    if mode not in MODES:
        mode = AREA
    chunk = max(1, int(chunk_pairs))
    S = len(shapes)
    out = [None] * S
    ok = [i for i, s in enumerate(shapes) if s is not None]
    if not ok:
        return out
    reach = np.broadcast_to(np.asarray(reach, dtype=np.float64), (S,)).copy()
    edge = np.broadcast_to(np.asarray(edge, dtype=np.float64), (S,)).copy()
    bb = np.zeros((S, 4))
    bb[ok] = [shapes[i].bbox for i in ok]
    rr = np.where(np.isfinite(reach) & (reach > 0), reach, 0.0)
    R0, C0, H, W = _windows(gr, bb, rr)
    n = H * W
    ecnt = np.zeros(S, dtype=np.int64)
    polys, discs = [], []
    for i in ok:
        sh = shapes[i]
        if not (math.isfinite(reach[i]) and math.isfinite(edge[i])):
            continue
        if n[i] == 0:                           # nothing of the raster around it
            empty = np.zeros((int(H[i]), int(W[i])), dtype=bool)
            out[i] = CellSet(R0[i], C0[i], empty, empty, mode,
                             _whole(sh, float(reach[i]), float(edge[i]), mode))
            continue
        if sh.kind == "disc":
            discs.append(i)
            continue
        ecnt[i] = sh.n_edges
        if len(sh.parts) > 2 or n[i] * ecnt[i] > chunk:
            out[i] = _poly_single(sh, gr, R0[i], C0[i], int(H[i]), int(W[i]), float(reach[i]),
                                  float(edge[i]), mode, chunk)
        else:
            polys.append(i)
    if polys:
        pidx = np.array(polys, dtype=np.int64)
        voff = np.zeros(S, dtype=np.int64)
        voff[pidx] = np.concatenate(([0], np.cumsum(ecnt[pidx])[:-1]))
        gxy = np.concatenate([shapes[i].xy for i in polys])
        gnxt = np.arange(1, gxy.shape[0] + 1, dtype=np.int64)
        starts, ends = [], []
        for i in polys:                          # close every ring: last -> first
            base, rg = int(voff[i]), shapes[i].rings
            for k in range(len(rg) - 1):
                starts.append(base + rg[k])
                ends.append(base + rg[k + 1] - 1)
        gnxt[np.array(ends, dtype=np.int64)] = np.array(starts, dtype=np.int64)
        order = pidx[np.lexsort((W[pidx], H[pidx], ecnt[pidx]))]
        for run in _groups(order, H, W, ecnt, chunk):
            _poly_group(run, gr, R0, C0, H, W, reach, edge, mode, gxy, gnxt, voff, ecnt, out)
    if discs:
        didx = np.array(discs, dtype=np.int64)
        order = didx[np.lexsort((W[didx], H[didx]))]
        ones = np.ones(S, dtype=np.int64)
        for run in _groups(order, H, W, ones, chunk):
            _disc_group(shapes, run, gr, R0, C0, H, W, reach, edge, mode, out)
    return out


def structure_cells(shape, gt, reach, edge, mode=AREA, size=None, chunk_pairs=CHUNK_PAIRS):
    """CellSet of one structure (see structures_cells), or None when the
    fast path cannot be used."""
    if shape is None:
        return None
    res = structures_cells([shape], gt, reach, edge, mode, size, chunk_pairs)
    return None if res is None else res[0]


# ----------------------------------------------------------------- reading --
def sample_cells(items, sampler, mode, edge, progress=None, chunk_pairs=CHUNK_PAIRS,
                 max_cells=None, strips=None):
    """Valid raster cells of many structures - the fast replacement for
    structures.py's GEOS regions + _onepass_cells + the ring / band fallback.

    items: {sid: (Shape, reach)} (map units of the raster CRS);
    sampler: a RasterSampler (uses .ok, .gt, .xsize, .ysize, .backend,
    .window_values); mode: "area" / "ring" / "band"; edge: the edge ring
    width (map units). Returns None when the geotransform cannot be used,
    else (cells, fell_back, whole):
      cells     {sid: (xs, ys, values)} valid cells (finite after NoData /
                scale), row-major, xs / ys by the plugin's centre formula -
                the ring / band cells, or the whole-area cells when the
                ring / band holds no valid cell (the sid is then in
                fell_back, as the GEOS path's "no cell centre in it - whole
                area used"). A sid without a valid cell in its whole area is
                absent (the caller's "cell under the centre" fallback).
      whole     sids whose ring / band IS the whole area by definition
                (point disc radius <= edge: GEOS _sample_region -> None).
    Strips of the union window (<= max_cells cells, default
    constants.READ_BLOCK_TARGET_CELLS) are read with the GDAL
    reader and >= ONEPASS_MIN structures, else one window per structure
    (strips=True / False forces it). progress(p, msg), p in 0..100.

    INTEGRATION (structures.evaluate_structures, setting "fast_sampling"):
    in step 1, after the _near_flood test:
        if use_fast:                          # fast_sampling and dep.ok
            shp = FS.shape_from_geometry(rec["geom"], r_disc)
            if shp is not None:
                fast[rec["id"]] = (shp, reach); fast_rd[rec["id"]] = r_disc
                continue                      # no GEOS buffer / difference
    before step 2:
        got = None
        if fast:
            try:
                got = FS.sample_cells(fast, dep, sample_area, edge, progress=...)
            except CancelledByUser: raise
            except Exception as e: log_message(...)
            if got is None:                   # rotated raster / error: GEOS
                for sid in fast: <the unchanged GEOS region code> -> regions / fulls
                fast = {}
    after `cells` of the GEOS regions (and their fulls fallback):
        cells.update(got[0]); label fell_back / ring-band as before (not for
        `whole`); regions[sid] = geom.buffer(reach, 8) only for the fast sids
        without cells (their centroid fallback) and, for display
        (rec["region"]), for the wet ones.
    """
    if mode not in MODES:
        mode = AREA
    if max_cells is None:                 # v2.12: one strip <= READ_BLOCK_TARGET_CELLS
        from . import constants as _C
        max_cells = int(_C.READ_BLOCK_TARGET_CELLS)
    if not items or sampler is None or not getattr(sampler, "ok", False):
        return {}, set(), set()
    gt = sampler.gt
    sids = list(items)
    shapes = [items[s][0] for s in sids]
    reaches = [items[s][1] for s in sids]
    res = structures_cells(shapes, gt, reaches, edge, mode, (sampler.xsize, sampler.ysize),
                           chunk_pairs)
    if res is None:
        return None
    sets = [(sid, cs) for sid, cs in zip(sids, res)
            if cs is not None and cs.mask.shape[0] > 0 and cs.mask.shape[1] > 0]
    want_area = mode != AREA
    got_m, got_a = {}, {}

    def take(sid, cs, vals, a, b, voff, coff):
        """Rows a..b-1 of cs from vals (vals row voff, col coff = cs.c0)."""
        sub = vals[a - voff:b - voff, coff:coff + cs.mask.shape[1]]
        fin = np.isfinite(sub)
        for m, store in ((cs.mask, got_m), (cs.area, got_a)):
            if store is got_a and (not want_area or cs.whole):
                continue
            sel = m[a - cs.r0:b - cs.r0] & fin
            if sel.any():
                rr, cc = np.nonzero(sel)
                store.setdefault(sid, []).append((rr + a, cc + cs.c0, sub[rr, cc]))

    if strips is None:
        strips = getattr(sampler, "backend", None) == "gdal" and len(sets) >= ONEPASS_MIN
    if strips and sets:
        R0 = min(cs.r0 for _s, cs in sets)
        R1 = max(cs.r0 + cs.mask.shape[0] for _s, cs in sets)
        C0 = min(cs.c0 for _s, cs in sets)
        C1 = max(cs.c0 + cs.mask.shape[1] for _s, cs in sets)
        Wd = C1 - C0
        rows = max(1, min(R1 - R0, int(max_cells // max(1, Wd))))
        buckets = {}
        for sid, cs in sets:
            f = (cs.r0 - R0) // rows
            l_ = (cs.r0 + cs.mask.shape[0] - 1 - R0) // rows
            for b in range(f, l_ + 1):
                buckets.setdefault(b, []).append((sid, cs))
        n_strips = int(math.ceil((R1 - R0) / float(rows)))
        for si in range(n_strips):
            if progress and si % 4 == 0:
                progress(100.0 * si / max(1, n_strips),
                         "Sampling structures: strip {} of {}".format(si + 1, n_strips))
            todo = buckets.get(si)
            if not todo:
                continue
            y0 = R0 + si * rows
            hh = min(rows, R1 - y0)
            vals = sampler.window_values(C0, y0, Wd, hh)
            if vals is None:
                continue
            for sid, cs in todo:
                a = max(y0, cs.r0)
                b = min(y0 + hh, cs.r0 + cs.mask.shape[0])
                if a < b:
                    take(sid, cs, vals, a, b, y0, cs.c0 - C0)
    else:
        for i, (sid, cs) in enumerate(sets):
            if progress and i % 500 == 0:
                progress(100.0 * i / max(1, len(sets)),
                         "Sampling structures: {} of {}".format(i, len(sets)))
            h, w = cs.mask.shape
            vals = sampler.window_values(cs.c0, cs.r0, w, h)
            if vals is not None:
                take(sid, cs, vals, cs.r0, cs.r0 + h, cs.r0, 0)
    cells, fell_back = {}, set()
    whole = set(sid for sid, cs in zip(sids, res) if cs is not None and cs.whole)
    for sid, cs in sets:
        parts = got_m.get(sid)
        if not parts and want_area and not cs.whole:
            parts = got_a.get(sid)
            if parts:
                fell_back.add(sid)
        if not parts:
            continue
        rr = np.concatenate([p[0] for p in parts])
        cc = np.concatenate([p[1] for p in parts])
        vv = np.concatenate([p[2] for p in parts])
        colc, rowc = cc + 0.5, rr + 0.5
        cells[sid] = (gt[0] + colc * gt[1] + rowc * gt[2],
                      gt[3] + colc * gt[4] + rowc * gt[5], vv)
    return cells, fell_back, whole
