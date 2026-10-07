# -*- coding: utf-8 -*-
"""v2.10: small pure-Python helpers shared by the dam-suite exports
(runnerculverts.py, suiteexport.py): the EPSG:5070 projection, geometry
functions on GeoJSON-like dicts and a GeoPackage writer / reader.

No QGIS here (sqlite3, struct, math only), so the exports run and are tested
the same way inside and outside QGIS, straight from the offline data pack.

Projection: NAD83 / Conus Albers (EPSG:5070) - Albers equal-area conic on the
GRS 1980 ellipsoid, standard parallels 29.5 N and 45.5 N, origin 23 N 96 W,
no false easting / northing (Snyder 1987, USGS PP 1395, eqs. 14-3 to 14-21).
Longitude / latitude from the pack are WGS 84; they are projected as NAD83
without a datum shift (the two differ by about 1 m in North Dakota - far
below what these screening layers need). EPSG:5070 is the standard CRS of the
ND Dam Breach Screening data package.

GeoPackage (OGC 12-128r15, version 1.2): application_id "GPKG", user_version
10200, the three required system tables, one feature table per layer (fid
INTEGER PRIMARY KEY AUTOINCREMENT, geometry column "geom" with the
GeoPackage binary header and ISO WKB, little endian, 2D) and optional
attribute tables (data_type "attributes"). No spatial index is written:
QGIS / GDAL read the file without one (QGIS: Layer Properties > Source >
Create spatial index, when a layer is large). Nothing here has been checked
with a real GDAL / QGIS; the tests read the files back with read_features().
"""

import math
import os
import sqlite3
import struct

# ------------------------------------------------------------ projection --
GRS80_A = 6378137.0
GRS80_INVF = 298.257222101


class AlbersEqualArea(object):
    """Albers equal-area conic on an ellipsoid (Snyder 1987, ch. 14)."""

    def __init__(self, lat1, lat2, lat0, lon0, a=GRS80_A, invf=GRS80_INVF, x0=0.0, y0=0.0,
                 e2=None):
        self.a = float(a)
        self.e2 = float(e2) if e2 is not None else (2.0 / invf - 1.0 / invf ** 2)
        self.e = math.sqrt(self.e2)
        self.lon0 = math.radians(lon0)
        self.x0, self.y0 = float(x0), float(y0)
        p1, p2, p0 = (math.radians(v) for v in (lat1, lat2, lat0))
        m1, m2 = self._m(p1), self._m(p2)
        q1, q2, q0 = self._q(p1), self._q(p2), self._q(p0)
        self.n = (m1 * m1 - m2 * m2) / (q2 - q1) if abs(p1 - p2) > 1e-12 else math.sin(p1)
        self.C = m1 * m1 + self.n * q1
        self.rho0 = self.a * math.sqrt(self.C - self.n * q0) / self.n

    def _m(self, phi):
        s = math.sin(phi)
        return math.cos(phi) / math.sqrt(1.0 - self.e2 * s * s)

    def _q(self, phi):
        s, e = math.sin(phi), self.e
        if e < 1e-12:
            return 2.0 * s
        return (1.0 - self.e2) * (s / (1.0 - self.e2 * s * s)
                                  - (1.0 / (2.0 * e)) * math.log((1.0 - e * s) / (1.0 + e * s)))

    def forward(self, lon, lat):
        """(x, y) metres of a longitude / latitude in degrees."""
        phi = math.radians(lat)
        rho = self.a * math.sqrt(max(self.C - self.n * self._q(phi), 0.0)) / self.n
        theta = self.n * (math.radians(lon) - self.lon0)
        return (self.x0 + rho * math.sin(theta), self.y0 + self.rho0 - rho * math.cos(theta))

    def inverse(self, x, y):
        """(lon, lat) degrees of a projected point (iterative latitude, eq. 3-16)."""
        x, y = x - self.x0, y - self.y0
        dy = self.rho0 - y
        rho = math.hypot(x, dy)
        theta = math.atan2(x, dy) if self.n > 0 else math.atan2(-x, -dy)
        q = (self.C - (rho * self.n / self.a) ** 2) / self.n
        lon = math.degrees(self.lon0 + theta / self.n)
        e, e2 = self.e, self.e2
        phi = math.asin(max(-1.0, min(1.0, q / 2.0)))
        for _i in range(20):
            s = math.sin(phi)
            c = math.cos(phi)
            if abs(c) < 1e-15:
                break
            nxt = phi + (1.0 - e2 * s * s) ** 2 / (2.0 * c) * (
                q / (1.0 - e2) - s / (1.0 - e2 * s * s)
                + (1.0 / (2.0 * e)) * math.log((1.0 - e * s) / (1.0 + e * s)))
            if abs(nxt - phi) < 1e-13:
                phi = nxt
                break
            phi = nxt
        return lon, math.degrees(phi)


ALBERS_5070 = AlbersEqualArea(29.5, 45.5, 23.0, -96.0)
EPSG_5070 = 5070
EPSG_4326 = 4326


def to_5070(lon, lat):
    return ALBERS_5070.forward(lon, lat)


def from_5070(x, y):
    return ALBERS_5070.inverse(x, y)


WKT = {
    5070: 'PROJCS["NAD83 / Conus Albers",GEOGCS["NAD83",DATUM["North_American_Datum_1983",'
          'SPHEROID["GRS 1980",6378137,298.257222101,AUTHORITY["EPSG","7019"]],'
          'AUTHORITY["EPSG","6269"]],PRIMEM["Greenwich",0,AUTHORITY["EPSG","8901"]],'
          'UNIT["degree",0.0174532925199433,AUTHORITY["EPSG","9122"]],AUTHORITY["EPSG","4269"]],'
          'PROJECTION["Albers_Conic_Equal_Area"],PARAMETER["latitude_of_center",23],'
          'PARAMETER["longitude_of_center",-96],PARAMETER["standard_parallel_1",29.5],'
          'PARAMETER["standard_parallel_2",45.5],PARAMETER["false_easting",0],'
          'PARAMETER["false_northing",0],UNIT["metre",1,AUTHORITY["EPSG","9001"]],'
          'AXIS["Easting",EAST],AXIS["Northing",NORTH],AUTHORITY["EPSG","5070"]]',
    4326: 'GEOGCS["WGS 84",DATUM["WGS_1984",SPHEROID["WGS 84",6378137,298.257223563,'
          'AUTHORITY["EPSG","7030"]],AUTHORITY["EPSG","6326"]],PRIMEM["Greenwich",0,'
          'AUTHORITY["EPSG","8901"]],UNIT["degree",0.0174532925199433,AUTHORITY["EPSG","9122"]],'
          'AXIS["Latitude",NORTH],AXIS["Longitude",EAST],AUTHORITY["EPSG","4326"]]',
}
SRS_NAMES = {5070: "NAD83 / Conus Albers", 4326: "WGS 84"}

# ------------------------------------------------------------ geometry --
# A geometry is a GeoJSON-like dict {"type": ..., "coordinates": ...};
# a feature is {"geom": geometry or None, "props": {name: value}}.


def map_coords(geom, fn):
    """The geometry with fn(x, y) -> (x, y) applied to every position."""
    if not geom:
        return None
    t = geom.get("type")
    c = geom.get("coordinates")

    def pt(p):
        return tuple(fn(float(p[0]), float(p[1])))
    try:
        if t == "Point":
            return {"type": t, "coordinates": pt(c)}
        if t in ("LineString", "MultiPoint"):
            return {"type": t, "coordinates": [pt(p) for p in c]}
        if t in ("Polygon", "MultiLineString"):
            return {"type": t, "coordinates": [[pt(p) for p in r] for r in c]}
        if t == "MultiPolygon":
            return {"type": t, "coordinates": [[[pt(p) for p in r] for r in poly] for poly in c]}
        if t == "GeometryCollection":
            parts = [map_coords(g, fn) for g in geom.get("geometries") or ()]
            return {"type": t, "geometries": [g for g in parts if g]}
    except (TypeError, ValueError, IndexError):
        return None
    return None


def project_5070(geom):
    """A lon / lat geometry in EPSG:5070 metres (None when it cannot be read)."""
    return map_coords(geom, to_5070)


def kind(geom):
    """"point", "line", "polygon" or None."""
    t = (geom or {}).get("type") or ""
    if "Point" in t:
        return "point"
    if "LineString" in t:
        return "line"
    if "Polygon" in t:
        return "polygon"
    if t == "GeometryCollection":
        kinds = {kind(g) for g in geom.get("geometries") or ()} - {None}
        return next(iter(kinds)) if len(kinds) == 1 else None
    return None


def points_of(geom):
    """Every position [(x, y)] of the geometry."""
    t = (geom or {}).get("type")
    c = (geom or {}).get("coordinates")
    if t == "Point":
        return [tuple(c[:2])]
    if t in ("LineString", "MultiPoint"):
        return [tuple(p[:2]) for p in c]
    if t in ("Polygon", "MultiLineString"):
        return [tuple(p[:2]) for r in c for p in r]
    if t == "MultiPolygon":
        return [tuple(p[:2]) for poly in c for r in poly for p in r]
    if t == "GeometryCollection":
        return [p for g in geom.get("geometries") or () for p in points_of(g)]
    return []


def lines_of(geom):
    """[[(x, y), ...]] - the parts of a (multi)line geometry."""
    t = (geom or {}).get("type")
    c = (geom or {}).get("coordinates")
    if t == "LineString":
        return [[tuple(p[:2]) for p in c]]
    if t == "MultiLineString":
        return [[tuple(p[:2]) for p in r] for r in c]
    if t == "GeometryCollection":
        return [ln for g in geom.get("geometries") or () for ln in lines_of(g)]
    return []


def polygons_of(geom):
    """[[ring, ring, ...], ...] - the polygons (exterior ring first)."""
    t = (geom or {}).get("type")
    c = (geom or {}).get("coordinates")
    if t == "Polygon":
        return [[[tuple(p[:2]) for p in r] for r in c]]
    if t == "MultiPolygon":
        return [[[tuple(p[:2]) for p in r] for r in poly] for poly in c]
    if t == "GeometryCollection":
        return [pl for g in geom.get("geometries") or () for pl in polygons_of(g)]
    return []


def bbox(geom):
    pts = points_of(geom)
    if not pts:
        return None
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return (min(xs), min(ys), max(xs), max(ys))


def ring_area(ring):
    """Signed shoelace area (counter-clockwise positive)."""
    s = 0.0
    for i in range(len(ring) - 1):
        s += ring[i][0] * ring[i + 1][1] - ring[i + 1][0] * ring[i][1]
    if ring and tuple(ring[0]) != tuple(ring[-1]):
        s += ring[-1][0] * ring[0][1] - ring[0][0] * ring[-1][1]
    return 0.5 * s


def polygon_area(geom):
    """Area of a (multi)polygon in CRS units squared (holes subtracted)."""
    total = 0.0
    for poly in polygons_of(geom):
        if not poly:
            continue
        total += abs(ring_area(poly[0])) - sum(abs(ring_area(r)) for r in poly[1:])
    return max(total, 0.0)


def point_in_ring(x, y, ring):
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        xi, yi = ring[i]
        xj, yj = ring[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-300) + xi:
            inside = not inside
        j = i
    return inside


def point_in_polygon(x, y, poly):
    """poly = [exterior, hole, ...]."""
    if not poly or not point_in_ring(x, y, poly[0]):
        return False
    return not any(point_in_ring(x, y, h) for h in poly[1:])


def point_on_surface(geom):
    """(x, y) inside a polygon (its centroid when that is inside, else the middle of the
    widest horizontal run through the middle height), a line's middle vertex, a point."""
    k = kind(geom)
    if k == "point":
        pts = points_of(geom)
        return pts[0] if pts else None
    if k == "line":
        parts = [p for p in lines_of(geom) if p]
        if not parts:
            return None
        longest = max(parts, key=line_length)
        return point_at(longest, line_length(longest) / 2.0)
    polys = [p for p in polygons_of(geom) if p and len(p[0]) >= 3]
    if not polys:
        return None
    poly = max(polys, key=lambda p: abs(ring_area(p[0])))
    ring = poly[0]
    a = ring_area(ring)
    if abs(a) > 0:
        cx = cy = 0.0
        for i in range(len(ring) - 1):
            f = ring[i][0] * ring[i + 1][1] - ring[i + 1][0] * ring[i][1]
            cx += (ring[i][0] + ring[i + 1][0]) * f
            cy += (ring[i][1] + ring[i + 1][1]) * f
        cx, cy = cx / (6.0 * a), cy / (6.0 * a)
        if point_in_polygon(cx, cy, poly):
            return (cx, cy)
    ys = [p[1] for p in ring]
    y = (min(ys) + max(ys)) / 2.0
    xs = []
    for r in poly:
        for i in range(len(r) - 1):
            (x0, y0), (x1, y1) = r[i], r[i + 1]
            if (y0 > y) != (y1 > y):
                xs.append(x0 + (y - y0) * (x1 - x0) / (y1 - y0))
    xs.sort()
    best = None
    for i in range(0, len(xs) - 1, 2):
        w = xs[i + 1] - xs[i]
        if best is None or w > best[0]:
            best = (w, (xs[i] + xs[i + 1]) / 2.0)
    return (best[1], y) if best else tuple(ring[0])


def line_length(pts):
    return sum(math.hypot(pts[i + 1][0] - pts[i][0], pts[i + 1][1] - pts[i][1])
               for i in range(len(pts) - 1))


def point_at(pts, dist):
    """(x, y) at `dist` along a polyline (clamped to its ends)."""
    if not pts:
        return None
    if dist <= 0:
        return tuple(pts[0])
    run = 0.0
    for i in range(len(pts) - 1):
        (x0, y0), (x1, y1) = pts[i], pts[i + 1]
        seg = math.hypot(x1 - x0, y1 - y0)
        if seg > 0 and run + seg >= dist:
            t = (dist - run) / seg
            return (x0 + t * (x1 - x0), y0 + t * (y1 - y0))
        run += seg
    return tuple(pts[-1])


def project_on_line(pts, x, y):
    """(chainage, distance, (px, py), segment index) of the point of a polyline nearest
    to (x, y)."""
    best = None
    run = 0.0
    for i in range(len(pts) - 1):
        (x0, y0), (x1, y1) = pts[i], pts[i + 1]
        dx, dy = x1 - x0, y1 - y0
        l2 = dx * dx + dy * dy
        t = 0.0 if l2 <= 0 else max(0.0, min(1.0, ((x - x0) * dx + (y - y0) * dy) / l2))
        px, py = x0 + t * dx, y0 + t * dy
        d = math.hypot(x - px, y - py)
        if best is None or d < best[1]:
            best = (run + t * math.sqrt(l2), d, (px, py), i)
        run += math.sqrt(l2)
    if best is None and pts:
        p = tuple(pts[0])
        return (0.0, math.hypot(x - p[0], y - p[1]), p, 0)
    return best


def segment_intersection(a, b, c, d):
    """(t, u, (x, y)) where segment a-b meets segment c-d (t, u in [0, 1]), else None."""
    r = (b[0] - a[0], b[1] - a[1])
    s = (d[0] - c[0], d[1] - c[1])
    den = r[0] * s[1] - r[1] * s[0]
    if abs(den) < 1e-12:
        return None
    qp = (c[0] - a[0], c[1] - a[1])
    t = (qp[0] * s[1] - qp[1] * s[0]) / den
    u = (qp[0] * r[1] - qp[1] * r[0]) / den
    if -1e-9 <= t <= 1 + 1e-9 and -1e-9 <= u <= 1 + 1e-9:
        return t, u, (a[0] + t * r[0], a[1] + t * r[1])
    return None


class SegmentGrid(object):
    """Uniform grid over the segments of many polylines: items are (key, part index,
    segment index); near() returns the candidates around a point."""

    def __init__(self, cell):
        self.cell = max(float(cell), 1e-6)
        self.cells = {}

    def _k(self, v):
        return int(math.floor(v / self.cell))

    def add_line(self, key, parts):
        for pi, pts in enumerate(parts):
            for si in range(len(pts) - 1):
                (x0, y0), (x1, y1) = pts[si], pts[si + 1]
                for i in range(self._k(min(x0, x1)), self._k(max(x0, x1)) + 1):
                    for j in range(self._k(min(y0, y1)), self._k(max(y0, y1)) + 1):
                        self.cells.setdefault((i, j), []).append((key, pi, si))

    def near(self, x, y, radius):
        r = int(math.ceil(radius / self.cell))
        ki, kj = self._k(x), self._k(y)
        seen = set()
        for i in range(ki - r, ki + r + 1):
            for j in range(kj - r, kj + r + 1):
                for it in self.cells.get((i, j), ()):
                    if it not in seen:
                        seen.add(it)
                        yield it

    def keys_near(self, x, y, radius):
        return sorted({it[0] for it in self.near(x, y, radius)})


def seg_distance(px, py, a, b):
    dx, dy = b[0] - a[0], b[1] - a[1]
    l2 = dx * dx + dy * dy
    t = 0.0 if l2 <= 0 else max(0.0, min(1.0, ((px - a[0]) * dx + (py - a[1]) * dy) / l2))
    return math.hypot(px - a[0] - t * dx, py - a[1] - t * dy)


def dist_to_lines(x, y, parts):
    best = None
    for pts in parts:
        if len(pts) == 1:
            d = math.hypot(x - pts[0][0], y - pts[0][1])
            best = d if best is None else min(best, d)
        for i in range(len(pts) - 1):
            d = seg_distance(x, y, pts[i], pts[i + 1])
            best = d if best is None else min(best, d)
    return best


def min_area_rect(pts):
    """(width, length) of the minimum-area enclosing rectangle of the points (rotating
    calipers over the convex hull), width <= length; None for fewer than 3 points."""
    hull = convex_hull(pts)
    if len(hull) < 3:
        return None
    best = None
    n = len(hull)
    for i in range(n):
        (x0, y0), (x1, y1) = hull[i], hull[(i + 1) % n]
        ln = math.hypot(x1 - x0, y1 - y0)
        if ln <= 0:
            continue
        ux, uy = (x1 - x0) / ln, (y1 - y0) / ln
        us = [(p[0] - x0) * ux + (p[1] - y0) * uy for p in hull]
        vs = [-(p[0] - x0) * uy + (p[1] - y0) * ux for p in hull]
        w, h = max(us) - min(us), max(vs) - min(vs)
        if best is None or w * h < best[0]:
            best = (w * h, w, h)
    if best is None:
        return None
    a, b = sorted((best[1], best[2]))
    return (a, b)


def convex_hull(pts):
    pts = sorted(set((float(x), float(y)) for x, y in pts))
    if len(pts) < 3:
        return pts

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])
    lower, upper = [], []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]


# ------------------------------------------------------------ GeoPackage --
GPKG_APPLICATION_ID = 0x47504B47          # "GPKG"
GPKG_USER_VERSION = 10200
_WKB = {"Point": 1, "LineString": 2, "Polygon": 3, "MultiPoint": 4, "MultiLineString": 5,
        "MultiPolygon": 6}
_WKB_NAMES = {v: k for k, v in _WKB.items()}
GEOM_TYPES = ("POINT", "LINESTRING", "POLYGON", "MULTIPOINT", "MULTILINESTRING",
              "MULTIPOLYGON", "GEOMETRY")
FIELD_TYPES = {"text": "TEXT", "real": "REAL", "int": "INTEGER", "date": "DATE"}


class GpkgError(Exception):
    pass


def _wkb(geom):
    t = geom["type"]
    c = geom["coordinates"]
    code = _WKB.get(t)
    if code is None:
        raise GpkgError("geometry type {} cannot be written".format(t))
    out = [struct.pack("<BI", 1, code)]

    def pts(seq):
        out.append(struct.pack("<I", len(seq)))
        for p in seq:
            out.append(struct.pack("<dd", float(p[0]), float(p[1])))
    if t == "Point":
        out.append(struct.pack("<dd", float(c[0]), float(c[1])))
    elif t == "LineString":
        pts(c)
    elif t == "Polygon":
        out.append(struct.pack("<I", len(c)))
        for r in c:
            pts(r)
    else:
        sub = {"MultiPoint": "Point", "MultiLineString": "LineString",
               "MultiPolygon": "Polygon"}[t]
        out.append(struct.pack("<I", len(c)))
        for part in c:
            out.append(_wkb({"type": sub, "coordinates": part}))
    return b"".join(out)


def geometry_blob(geom, srs_id):
    """GeoPackage binary geometry: header ("GP", version 0, flags, srs_id, envelope) + WKB."""
    if geom is None:
        return None
    wkb = _wkb(geom)
    if geom["type"] == "Point":
        return struct.pack("<2sBBi", b"GP", 0, 0x01, int(srs_id)) + wkb
    b = bbox(geom)
    if b is None:
        # an empty geometry: empty flag, no envelope
        return struct.pack("<2sBBi", b"GP", 0, 0x11, int(srs_id)) + wkb
    return struct.pack("<2sBBi4d", b"GP", 0, 0x03, int(srs_id), b[0], b[2], b[1], b[3]) + wkb


def _read_wkb(b, o=0):
    end = "<" if b[o] == 1 else ">"
    (code,) = struct.unpack_from(end + "I", b, o + 1)
    o += 5
    has_z = bool(code & 0x80000000) or (code % 10000) // 1000 in (1, 3)
    has_m = bool(code & 0x40000000) or (code % 10000) // 1000 in (2, 3)
    base = (code & 0x0FFFFFFF) % 1000
    step = 8 * (2 + has_z + has_m)

    def pts(o):
        (n,) = struct.unpack_from(end + "I", b, o)
        o += 4
        seq = [struct.unpack_from(end + "dd", b, o + step * i) for i in range(n)]
        return seq, o + step * n
    if base == 1:
        return {"type": "Point", "coordinates": struct.unpack_from(end + "dd", b, o)}, o + step
    if base == 2:
        seq, o = pts(o)
        return {"type": "LineString", "coordinates": seq}, o
    if base == 3:
        (n,) = struct.unpack_from(end + "I", b, o)
        o += 4
        rings = []
        for _r in range(n):
            seq, o = pts(o)
            rings.append(seq)
        return {"type": "Polygon", "coordinates": rings}, o
    if base in (4, 5, 6):
        (n,) = struct.unpack_from(end + "I", b, o)
        o += 4
        parts = []
        for _p in range(n):
            g, o = _read_wkb(b, o)
            parts.append(g["coordinates"])
        return {"type": _WKB_NAMES[base], "coordinates": parts}, o
    raise GpkgError("WKB type {} not supported by this reader".format(code))


def parse_blob(blob):
    """(geometry dict, srs_id) of a GeoPackage geometry blob."""
    if blob is None:
        return None, None
    blob = bytes(blob)
    if blob[:2] != b"GP":
        raise GpkgError("not a GeoPackage geometry")
    flags = blob[3]
    end = "<" if flags & 1 else ">"
    (srs,) = struct.unpack_from(end + "i", blob, 4)
    env = (flags >> 1) & 7
    size = {0: 0, 1: 32, 2: 48, 3: 48, 4: 64}.get(env)
    if size is None:
        raise GpkgError("bad GeoPackage envelope code {}".format(env))
    geom, _o = _read_wkb(blob, 8 + size)
    return geom, srs


def _qi(name):
    return '"{}"'.format(str(name).replace('"', '""'))


class GpkgWriter(object):
    """Writes a new GeoPackage (an existing file is replaced).

        w = GpkgWriter(path)
        lyr = w.layer("roads", "LINESTRING", [("name", "text"), ("aadt", "real")], 5070)
        lyr.add(geometry, {"name": "US 2", "aadt": 4100})
        w.table("info", [("key", "text"), ("value", "text")]).add(None, {...})
        w.close()
    Geometries are GeoJSON-like dicts in the layer's CRS. Field names must be unique
    (case-insensitively; "fid" and "geom" are taken)."""

    def __init__(self, path, overwrite=True):
        self.path = path
        folder = os.path.dirname(os.path.abspath(path))
        os.makedirs(folder, exist_ok=True)
        self.tmp = path + ".part"
        for p in (self.tmp, self.tmp + "-journal"):
            if os.path.exists(p):
                os.remove(p)
        if os.path.exists(path) and not overwrite:
            raise GpkgError("{} exists".format(path))
        self.con = sqlite3.connect(self.tmp)
        self.con.execute("PRAGMA application_id = {}".format(GPKG_APPLICATION_ID))
        self.con.execute("PRAGMA user_version = {}".format(GPKG_USER_VERSION))
        self.con.executescript("""
CREATE TABLE gpkg_spatial_ref_sys (srs_name TEXT NOT NULL, srs_id INTEGER NOT NULL PRIMARY KEY,
  organization TEXT NOT NULL, organization_coordsys_id INTEGER NOT NULL,
  definition TEXT NOT NULL, description TEXT);
CREATE TABLE gpkg_contents (table_name TEXT NOT NULL PRIMARY KEY, data_type TEXT NOT NULL,
  identifier TEXT UNIQUE, description TEXT DEFAULT '',
  last_change DATETIME NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  min_x DOUBLE, min_y DOUBLE, max_x DOUBLE, max_y DOUBLE, srs_id INTEGER,
  CONSTRAINT fk_gc_r_srs_id FOREIGN KEY (srs_id) REFERENCES gpkg_spatial_ref_sys(srs_id));
CREATE TABLE gpkg_geometry_columns (table_name TEXT NOT NULL, column_name TEXT NOT NULL,
  geometry_type_name TEXT NOT NULL, srs_id INTEGER NOT NULL, z TINYINT NOT NULL,
  m TINYINT NOT NULL, CONSTRAINT pk_geom_cols PRIMARY KEY (table_name, column_name),
  CONSTRAINT uk_gc_table_name UNIQUE (table_name),
  CONSTRAINT fk_gc_tn FOREIGN KEY (table_name) REFERENCES gpkg_contents(table_name),
  CONSTRAINT fk_gc_srs FOREIGN KEY (srs_id) REFERENCES gpkg_spatial_ref_sys (srs_id));
""")
        rows = [("Undefined cartesian SRS", -1, "NONE", -1, "undefined",
                 "undefined cartesian coordinate reference system"),
                ("Undefined geographic SRS", 0, "NONE", 0, "undefined",
                 "undefined geographic coordinate reference system")]
        rows += [(SRS_NAMES[k], k, "EPSG", k, WKT[k], "") for k in sorted(WKT)]
        self.con.executemany("INSERT INTO gpkg_spatial_ref_sys VALUES (?,?,?,?,?,?)", rows)
        self.layers = []
        self.closed = False

    def layer(self, name, geometry_type, fields, srs_id=EPSG_5070, description=""):
        geometry_type = geometry_type.upper()
        if geometry_type not in GEOM_TYPES:
            raise GpkgError("unknown geometry type {}".format(geometry_type))
        if int(srs_id) not in WKT:
            raise GpkgError("CRS EPSG:{} is not known to this writer".format(srs_id))
        lw = _LayerWriter(self, name, fields, geometry_type, int(srs_id), description)
        self.layers.append(lw)
        return lw

    def table(self, name, fields, description=""):
        lw = _LayerWriter(self, name, fields, None, None, description)
        self.layers.append(lw)
        return lw

    def close(self):
        if self.closed:
            return self.path
        for lw in self.layers:
            lw.finish()
        self.con.commit()
        self.con.close()
        self.closed = True
        os.replace(self.tmp, self.path)
        return self.path

    def abort(self):
        try:
            self.con.close()
        except Exception:
            pass
        self.closed = True
        for p in (self.tmp, self.tmp + "-journal"):
            try:
                os.remove(p)
            except OSError:
                pass


class _LayerWriter(object):
    def __init__(self, w, name, fields, gtype, srs_id, description):
        self.w, self.name, self.gtype, self.srs_id = w, name, gtype, srs_id
        low = {"fid", "geom"} if gtype else {"fid"}
        cols = []
        for fname, ftype in fields:
            if fname.lower() in low:
                raise GpkgError("{}: duplicate field name {}".format(name, fname))
            low.add(fname.lower())
            if ftype not in FIELD_TYPES:
                raise GpkgError("{}: unknown field type {}".format(fname, ftype))
            cols.append((fname, ftype))
        self.fields = cols
        self.count = 0
        self.box = None
        defs = ["fid INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL"]
        if gtype:
            defs.append("geom {}".format(gtype))
        defs += ["{} {}".format(_qi(f), FIELD_TYPES[t]) for f, t in cols]
        w.con.execute("CREATE TABLE {} ({})".format(_qi(name), ", ".join(defs)))
        w.con.execute("INSERT INTO gpkg_contents (table_name, data_type, identifier, description, "
                      "srs_id) VALUES (?,?,?,?,?)",
                      (name, "features" if gtype else "attributes", name, description or "",
                       srs_id))
        if gtype:
            w.con.execute("INSERT INTO gpkg_geometry_columns VALUES (?,?,?,?,?,?)",
                          (name, "geom", gtype, srs_id, 0, 0))
        names = (["geom"] if gtype else []) + [f for f, _t in cols]
        self.sql = "INSERT INTO {} ({}) VALUES ({})".format(
            _qi(name), ", ".join(_qi(n) for n in names), ", ".join("?" * len(names)))

    def _value(self, v, ftype):
        if v is None:
            return None
        if isinstance(v, float) and not math.isfinite(v):
            return None
        if ftype == "real":
            try:
                x = float(v)
            except (TypeError, ValueError):
                return None
            return x if math.isfinite(x) else None
        if ftype == "int":
            if isinstance(v, bool):
                return int(v)
            try:
                return int(round(float(v)))
            except (TypeError, ValueError):
                return None
        s = str(v)
        return s if s != "" else None

    def add(self, geom, attrs):
        vals = []
        if self.gtype:
            g = _fit(geom, self.gtype)
            vals.append(geometry_blob(g, self.srs_id) if g else None)
            b = bbox(g) if g else None
            if b:
                self.box = b if self.box is None else (min(self.box[0], b[0]), min(self.box[1], b[1]),
                                                       max(self.box[2], b[2]), max(self.box[3], b[3]))
        attrs = attrs or {}
        vals += [self._value(attrs.get(f), t) for f, t in self.fields]
        self.w.con.execute(self.sql, vals)
        self.count += 1

    def finish(self):
        if self.gtype and self.box:
            self.w.con.execute("UPDATE gpkg_contents SET min_x=?, min_y=?, max_x=?, max_y=? "
                               "WHERE table_name=?", self.box + (self.name,))


def _fit(geom, gtype):
    """The geometry in the layer's declared type (single -> multi when the layer is multi)."""
    if geom is None:
        return None
    t = geom.get("type")
    if gtype == "GEOMETRY" or t.upper() == gtype:
        return geom
    if gtype.startswith("MULTI") and t.upper() == gtype[5:]:
        return {"type": "Multi" + t, "coordinates": [geom["coordinates"]]}
    if not gtype.startswith("MULTI") and t.upper() == "MULTI" + gtype and \
            len(geom["coordinates"]) == 1:
        return {"type": t[5:], "coordinates": geom["coordinates"][0]}
    raise GpkgError("a {} cannot be written to a {} layer".format(t, gtype))


# ------------------------------------------------------------ reading --
def list_layers(path):
    """[{"name", "data_type", "srs_id", "geometry_type"}] of a GeoPackage (sqlite3 only)."""
    if not os.path.isfile(path):
        raise GpkgError("GeoPackage not found: {}".format(path))
    con = sqlite3.connect("file:{}?mode=ro".format(path.replace("?", "%3f").replace("#", "%23")),
                          uri=True)
    try:
        gcols = {r[0]: (r[1], r[2]) for r in con.execute(
            "SELECT table_name, geometry_type_name, srs_id FROM gpkg_geometry_columns")}
        out = []
        for name, dtype, srs in con.execute(
                "SELECT table_name, data_type, srs_id FROM gpkg_contents ORDER BY table_name"):
            out.append({"name": name, "data_type": dtype, "srs_id": srs,
                        "geometry_type": (gcols.get(name) or (None,))[0]})
        return out
    except sqlite3.Error as e:
        raise GpkgError("{} is not a GeoPackage: {}".format(path, e))
    finally:
        con.close()


def read_features(path, layer):
    """[(geometry dict or None, {field: value})] of one layer (tests, small layers)."""
    con = sqlite3.connect(path)
    try:
        cols = [r[1] for r in con.execute("PRAGMA table_info({})".format(_qi(layer)))]
        gcol = None
        row = con.execute("SELECT column_name FROM gpkg_geometry_columns WHERE table_name=?",
                          (layer,)).fetchone()
        if row:
            gcol = row[0]
        out = []
        for r in con.execute("SELECT {} FROM {} ORDER BY fid".format(
                ", ".join(_qi(c) for c in cols), _qi(layer))):
            d = dict(zip(cols, r))
            g = parse_blob(d.pop(gcol))[0] if gcol else None
            d.pop("fid", None)
            out.append((g, d))
        return out
    finally:
        con.close()
