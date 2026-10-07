"""OGR test double: just enough for the tools' in-memory rasterization (a Memory data source,
one layer, features with geometries read from WKB)."""
import struct

wkbPolygon, wkbPoint, wkbLineString = 3, 1, 2


class Geometry:
    def __init__(self, kind, points=None, rings=None):
        self.kind, self.points, self.rings = kind, points or [], rings or []


def _read(b, o):
    end = "<" if b[o] == 1 else ">"
    (code,) = struct.unpack_from(end + "I", b, o + 1)
    o += 5
    base = (code & 0x0FFFFFFF) % 1000

    def pts(o):
        (n,) = struct.unpack_from(end + "I", b, o)
        o += 4
        out = []
        for _ in range(n):
            x, y = struct.unpack_from(end + "dd", b, o)
            o += 16
            out.append((x, y))
        return out, o

    if base == 1:
        x, y = struct.unpack_from(end + "dd", b, o)
        return Geometry("point", [(x, y)]), o + 16
    if base == 2:
        p, o = pts(o)
        return Geometry("line", p), o
    if base == 3:
        (nr,) = struct.unpack_from(end + "I", b, o)
        o += 4
        rings = []
        for _ in range(nr):
            r, o = pts(o)
            rings.append(r)
        return Geometry("polygon", rings=rings), o
    (n,) = struct.unpack_from(end + "I", b, o)
    o += 4
    parts = []
    for _ in range(n):
        g, o = _read(b, o)
        parts.append(g)
    if parts and parts[0].kind == "polygon":
        return Geometry("polygon", rings=[r for g in parts for r in g.rings]), o
    return Geometry(parts[0].kind if parts else "point", [p for g in parts for p in g.points]), o


def CreateGeometryFromWkb(wkb):
    return _read(bytes(wkb), 0)[0]


class Feature:
    def __init__(self, defn):
        self.geom = None

    def SetGeometry(self, g):
        self.geom = g


class Layer:
    def __init__(self):
        self.features = []

    def GetLayerDefn(self):
        return None

    def CreateFeature(self, f):
        self.features.append(f)


class DataSource:
    def CreateLayer(self, name, srs=None, geom_type=None):
        return Layer()


class _Driver:
    def CreateDataSource(self, name):
        return DataSource()


def GetDriverByName(name):
    return _Driver()
