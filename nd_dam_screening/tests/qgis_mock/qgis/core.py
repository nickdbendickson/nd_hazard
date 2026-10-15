"""Test double for the part of qgis.core the plugin's processing tools use.

Single CRS (every layer already in EPSG:5070), shapely-free geometry.  It checks the
tools' own logic and data flow, not the real QGIS API.
"""
import math
import os

import numpy as np


# ---------------------------------------------------------------- basics
class QgsProcessingException(Exception):
    pass


import enum as _enum

from qgis.PyQt.QtCore import FLAVOR as _FLAVOR, QMetaType as _QMetaType

# FAKE_QGIS_FLAVOR=4.0: QGIS 4 / Qt6 - only the scoped spellings exist (Qgis.WkbType.Point,
# Qgis.ProcessingSourceType.VectorPoint, ...); the QGIS 3 flat names below are removed, so a
# plugin line that still uses one fails the tests. 3.28 / 3.40: both spellings exist where the
# real QGIS has both (3.28 lacks most Qgis.* processing enums).
QGIS4 = _FLAVOR.startswith("4")
_WKB_POINT, _WKB_LINE, _WKB_POLY, _WKB_MLINE = 1, 2, 3, 5


class Qgis:
    QGIS_VERSION_INT = 40200 if QGIS4 else (34000 if _FLAVOR == "3.40" else 32800)
    QGIS_VERSION = {40200: "4.2.0", 34000: "3.40.0", 32800: "3.28.0"}[QGIS_VERSION_INT]
    MessageLevel = _enum.IntEnum("MessageLevel", {"Info": 0, "Warning": 1, "Critical": 2, "Success": 3})


if QGIS4 or _FLAVOR == "3.40":
    Qgis.ProcessingNumberParameterType = _enum.IntEnum("ProcessingNumberParameterType", {"Integer": 0, "Double": 1})
    Qgis.ProcessingFileParameterBehavior = _enum.IntEnum("ProcessingFileParameterBehavior", {"File": 0, "Folder": 1})
    Qgis.ProcessingSourceType = _enum.IntEnum("ProcessingSourceType", {
        "VectorAnyGeometry": -1, "VectorPoint": 0, "VectorLine": 1, "VectorPolygon": 2})
    Qgis.ProcessingFieldParameterDataType = _enum.IntEnum("ProcessingFieldParameterDataType", {"Any": -1, "Numeric": 0})
    Qgis.ProcessingParameterFlag = _enum.IntFlag("ProcessingParameterFlag", {"Advanced": 2, "Optional": 8, "Hidden": 16})
    Qgis.GeometryType = _enum.IntEnum("GeometryType", {"Point": 0, "Line": 1, "Polygon": 2})
    Qgis.WkbType = _enum.IntEnum("WkbType", {"Point": 1, "LineString": 2, "Polygon": 3, "MultiLineString": 5})
if not QGIS4:
    Qgis.Info, Qgis.Warning, Qgis.Critical, Qgis.Success = 0, 1, 2, 3


class QgsProcessing:
    TEMPORARY_OUTPUT = "TEMPORARY_OUTPUT"


class QgsWkbTypes:
    @staticmethod
    def geometryType(w):
        return {1: 0, 2: 1, 5: 1, 3: 2}.get(int(w), 0)


if not QGIS4:
    QgsProcessing.TypeVectorPoint, QgsProcessing.TypeVectorLine = 0, 1
    QgsProcessing.TypeVectorPolygon, QgsProcessing.TypeVectorAnyGeometry = 2, -1
    QgsWkbTypes.Point, QgsWkbTypes.LineString, QgsWkbTypes.Polygon, QgsWkbTypes.MultiLineString = 1, 2, 3, 5
    QgsWkbTypes.PointGeometry, QgsWkbTypes.LineGeometry, QgsWkbTypes.PolygonGeometry = 0, 1, 2

    @staticmethod
    def geometryType(w):
        return {1: 0, 2: 1, 5: 1, 3: 2}.get(w, 0)


class QgsPointXY:
    def __init__(self, x, y=None):
        if y is None:
            x, y = x.x(), x.y()
        self._x, self._y = float(x), float(y)
    def x(self): return self._x
    def y(self): return self._y


class QgsRectangle:
    def __init__(self, x0, y0, x1, y1): self.b = (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))
    def intersects(self, o): return not (o.b[0] > self.b[2] or o.b[2] < self.b[0] or o.b[1] > self.b[3] or o.b[3] < self.b[1])
    def xMinimum(self): return self.b[0]
    def yMinimum(self): return self.b[1]
    def xMaximum(self): return self.b[2]
    def yMaximum(self): return self.b[3]


def _seg_dist(px, py, a, b):
    dx, dy = b[0] - a[0], b[1] - a[1]; dd = dx * dx + dy * dy
    u = 0 if dd == 0 else max(0, min(1, ((px - a[0]) * dx + (py - a[1]) * dy) / dd))
    return math.hypot(px - a[0] - u * dx, py - a[1] - u * dy)


class QgsGeometry:
    def __init__(self, kind=None, parts=None):
        if isinstance(kind, QgsGeometry):                 # copy constructor QgsGeometry(g)
            kind, parts = kind.kind, [list(pl) for pl in kind.parts]
        self.kind, self.parts = kind, parts or []       # parts: list of list of (x,y)
    def isNull(self): return self.kind is None
    @staticmethod
    def fromPointXY(p): return QgsGeometry("point", [[(p.x(), p.y())]])
    @staticmethod
    def fromPolylineXY(pts): return QgsGeometry("line", [[(p.x(), p.y()) for p in pts]])
    @staticmethod
    def fromPolygonXY(rings): return QgsGeometry("polygon", [[(p.x(), p.y()) for p in rings[0]]])
    @staticmethod
    def fromMultiPolylineXY(parts): return QgsGeometry("line", [[(p.x(), p.y()) for p in pl] for pl in parts])
    @staticmethod
    def fromMultiPointXY(pts): return QgsGeometry("point", [[(p.x(), p.y())] for p in pts])
    @staticmethod
    def fromMultiPolygonXY(polys):     # [[exterior ring], ...]; interior rings ignored
        return QgsGeometry("polygon", [[(p.x(), p.y()) for p in rings[0]] for rings in polys])
    @staticmethod
    def unaryUnion(gs):
        return QgsGeometry("polygon", [pt for g in gs for pt in g.parts])
    def copy(self): return QgsGeometry(self.kind, [list(p) for p in self.parts])
    def isEmpty(self): return not self.parts
    def type(self): return {"point": 0, "line": 1, "polygon": 2}[self.kind]
    def isMultipart(self): return len(self.parts) > 1
    def asPoint(self):
        if len(self.parts) > 1:
            raise TypeError("asPoint() on a multi-point geometry")   # as QGIS does
        return QgsPointXY(*self.parts[0][0])
    def asMultiPoint(self): return [QgsPointXY(*pl[0]) for pl in self.parts]
    def asPolygon(self): return [[QgsPointXY(*p) for p in self.parts[0]]]
    def asMultiPolygon(self): return [[[QgsPointXY(*p) for p in ring]] for ring in self.parts]
    def asPolyline(self): return [QgsPointXY(*p) for p in self.parts[0]]
    def asMultiPolyline(self): return [[QgsPointXY(*p) for p in pl] for pl in self.parts]
    def transform(self, tr): return 0
    def vertices(self):
        for pl in self.parts:
            for p in pl:
                yield QgsPointXY(*p)
    def _all(self): return np.array([p for pl in self.parts for p in pl], float)
    def boundingBox(self):
        a = self._all(); return QgsRectangle(a[:, 0].min(), a[:, 1].min(), a[:, 0].max(), a[:, 1].max())
    def centroid(self):
        a = self._all(); return QgsGeometry("point", [[(float(a[:, 0].mean()), float(a[:, 1].mean()))]])
    def pointOnSurface(self): return self.centroid()
    def densifyByDistance(self, d):
        out = []
        for pl in self.parts:
            q = [pl[0]]
            for a, b in zip(pl[:-1], pl[1:]):
                n = max(int(math.hypot(b[0] - a[0], b[1] - a[1]) / d), 1)
                q += [(a[0] + (b[0] - a[0]) * k / n, a[1] + (b[1] - a[1]) * k / n) for k in range(1, n + 1)]
            out.append(q)
        return QgsGeometry(self.kind, out)
    def distance(self, other):
        if self.kind == "point" and other.kind == "point":
            (a,), (b,) = self.parts[0], other.parts[0]
            return math.hypot(a[0] - b[0], a[1] - b[1])
        pt, ln = (other, self) if other.kind == "point" else (self, other)
        if ln.kind == "polygon" and pt.kind == "point" and ln.contains(pt):
            return 0.0
        px, py = pt.parts[0][0]
        best = 1e30
        for pl in ln.parts:
            if len(pl) == 1:
                best = min(best, math.hypot(px - pl[0][0], py - pl[0][1]))
            for a, b in zip(pl[:-1], pl[1:]):
                best = min(best, _seg_dist(px, py, a, b))
        return best
    def intersects(self, other): return self.boundingBox().intersects(other.boundingBox())
    # --- polygons (tool 3 / the self-test): area, WKB for OGR, point-in-polygon distance
    def area(self):
        if self.kind != "polygon": return 0.0
        a = 0.0
        for ring in self.parts:
            x = [p[0] for p in ring]; y = [p[1] for p in ring]
            a += 0.5 * abs(sum(x[i] * y[(i + 1) % len(x)] - x[(i + 1) % len(x)] * y[i] for i in range(len(x))))
        return a
    def contains(self, other):
        """point in the first ring (even-odd)."""
        if self.kind != "polygon" or other.kind != "point": return False
        px, py = other.parts[0][0]; ring = self.parts[0]; inside = False
        for i in range(len(ring)):
            (x1, y1), (x2, y2) = ring[i], ring[(i + 1) % len(ring)]
            if (y1 > py) != (y2 > py) and px < x1 + (py - y1) * (x2 - x1) / (y2 - y1):
                inside = not inside
        return inside
    def asWkb(self):
        import struct
        def pts(pl): return struct.pack("<I", len(pl)) + b"".join(struct.pack("<dd", *p) for p in pl)
        if self.kind == "point":
            return struct.pack("<bI", 1, 1) + struct.pack("<dd", *self.parts[0][0])
        if self.kind == "line":
            if len(self.parts) == 1: return struct.pack("<bI", 1, 2) + pts(self.parts[0])
            return struct.pack("<bI", 1, 5) + struct.pack("<I", len(self.parts)) + b"".join(
                struct.pack("<bI", 1, 2) + pts(pl) for pl in self.parts)
        rings = [list(r) if r[0] == r[-1] else list(r) + [r[0]] for r in self.parts]
        return struct.pack("<bI", 1, 3) + struct.pack("<I", len(rings)) + b"".join(pts(r) for r in rings)


class QgsField:
    def __init__(self, name, typ=None):
        # QGIS < 3.38 (Qt5) takes QVariant.Type only, as the real class does
        if _FLAVOR == "3.28" and isinstance(typ, _QMetaType.Type):
            raise TypeError("QgsField(): argument 2 has unexpected type 'QMetaType.Type'")
        self._n, self._t = name, typ
    def name(self): return self._n
    def type(self): return self._t


class QgsFields:
    def __init__(self, other=None): self._f = list(other._f) if other is not None else []
    def append(self, f): self._f.append(f); return True
    def names(self): return [f.name() for f in self._f]
    def indexOf(self, n):
        nm = self.names(); return nm.index(n) if n in nm else -1
    def count(self): return len(self._f)
    def __iter__(self): return iter(self._f)


class QgsFeature:
    _next = [1]
    def __init__(self, fields=None):
        self._fields = fields or QgsFields(); self._geom = QgsGeometry(); self._attrs = [None] * self._fields.count()
        self._id = QgsFeature._next[0]; QgsFeature._next[0] += 1
    def setGeometry(self, g): self._geom = g.copy()
    def geometry(self): return self._geom.copy()
    def hasGeometry(self): return not self._geom.isEmpty()
    def setAttributes(self, a): self._attrs = list(a)
    def attributes(self): return list(self._attrs)
    def fields(self): return self._fields
    def id(self): return self._id
    def __getitem__(self, k):
        i = self._fields.indexOf(k)
        if i < 0: raise KeyError(k)
        return self._attrs[i] if i < len(self._attrs) else None


class QgsFeatureRequest:
    def __init__(self): self.rect = None
    def setFilterRect(self, r): self.rect = r; return self


class QgsCoordinateReferenceSystem:
    def __init__(self, s="EPSG:5070"): self.s = s


class QgsProject:
    _i = None
    @classmethod
    def instance(cls):
        cls._i = cls._i or QgsProject(); return cls._i
    def transformContext(self): return None
    # project entries / layers (the popup window saves its state here; tests can look)
    def writeEntry(self, scope, key, value):
        self.__dict__.setdefault("_entries", {})[(scope, key)] = value; return True
    def readEntry(self, scope, key, default=""):
        e = self.__dict__.setdefault("_entries", {})
        return (e[(scope, key)], True) if (scope, key) in e else (default, False)
    def mapLayers(self): return dict(self.__dict__.setdefault("_layers", {}))
    def addMapLayer(self, layer, add_to_legend=True):
        self.__dict__.setdefault("_layers", {})[layer.id()] = layer; return layer


class QgsCoordinateTransform:
    def __init__(self, a=None, b=None, c=None): pass
    def transformBoundingBox(self, r): return r


# ---------------------------------------------------------------- layers
class FakeSource:
    """Stands in for QgsProcessingFeatureSource / a vector layer."""
    def __init__(self, fields, feats, wkb=_WKB_POINT, name="layer"):
        self._fields, self._feats, self._wkb, self.name = fields, feats, wkb, name
    def fields(self): return self._fields
    def featureCount(self): return len(self._feats)
    def wkbType(self): return self._wkb
    def sourceCrs(self): return QgsCoordinateReferenceSystem()
    def selectedFeatureIds(self): return list(getattr(self, "_selected", []))
    def selectByIds(self, ids): self._selected = list(ids)
    def getFeatures(self, req=None):
        for f in self._feats:
            if req is not None and req.rect is not None and not f.geometry().isEmpty() \
                    and not f.geometry().boundingBox().intersects(req.rect):
                continue
            yield f


def make_layer(fields_types, rows, kind, name="layer"):
    """rows: list of (geometry, {attr: value})."""
    fl = QgsFields()
    for n, t in fields_types: fl.append(QgsField(n, t))
    feats = []
    for g, attrs in rows:
        f = QgsFeature(fl); f.setGeometry(g); f.setAttributes([attrs.get(n) for n, _ in fields_types])
        feats.append(f)
    wkb = {"point": _WKB_POINT, "line": _WKB_LINE, "polygon": _WKB_POLY}[kind]
    return FakeSource(fl, feats, wkb, name)


class FakeSink:
    def __init__(self, fields, wkb): self.fields, self.wkb, self.features = fields, wkb, []
    def addFeature(self, f): self.features.append(f); return True
    def as_source(self): return FakeSource(self.fields, self.features, self.wkb)


class QgsProcessingContext:
    def __init__(self): self.layers = {}; self._n = 0
    def add(self, src):
        self._n += 1; k = f"mem{self._n}"; self.layers[k] = src; return k


class QgsProcessingFeedback:
    def __init__(self, verbose=False): self.log, self.errors, self.warnings, self.verbose, self._cancel = [], [], [], verbose, False
    def pushInfo(self, m):
        self.log.append(m)
        if self.verbose: print("   [info]", m)
    def pushWarning(self, m):
        self.warnings.append(m)
        if self.verbose: print("   [warn]", m)
    def reportError(self, m, fatal=False):
        self.errors.append(m); print("   [error]", m)
    def setProgress(self, p): pass
    def setProgressText(self, t): pass
    def isCanceled(self): return self._cancel
    def cancel(self): self._cancel = True


class QgsProcessingMultiStepFeedback:
    """Forwards to the parent feedback; records the steps it was set to."""
    def __init__(self, n, parent): self.n, self.parent, self.steps = n, parent, []
    def setCurrentStep(self, i): self.steps.append(i)
    def pushInfo(self, m): self.parent.pushInfo(m)
    def pushWarning(self, m): self.parent.pushWarning(m)
    def reportError(self, m, fatal=False): self.parent.reportError(m, fatal)
    def setProgress(self, p): pass
    def setProgressText(self, t): pass
    def isCanceled(self): return self.parent.isCanceled()


class QgsProcessingUtils:
    @staticmethod
    def mapLayerFromString(k, context): return context.layers.get(k)


# ---------------------------------------------------------------- parameters
class _Param:
    if not QGIS4:
        Folder, File = 1, 0
        Double, Integer = 1, 0
        Numeric, Any = 0, -1
        FlagAdvanced, FlagOptional, FlagHidden = 2, 8, 16
    _flags = 0
    def flags(self): return self._flags
    def setFlags(self, f): self._flags = f
    def setDefaultValue(self, v): self.defaultValue = v      # 0.13: the settings file sets defaults
    def isDestination(self): return type(self).__name__.endswith(("Sink", "Destination"))
    def __init__(self, name, description="", *a, **kw):
        self.name, self.description = name, description
        self.defaultValue = kw.get("defaultValue")
        self.optional = kw.get("optional", False)
        self.options = None
        self.allowMultiple = kw.get("allowMultiple", False)
        self.fileFilter = kw.get("fileFilter")             # 0.17.0: File / FileDestination filters
        # positional default for Number(name, desc, type, default) and Enum(name, desc, options)
        if type(self).__name__ == "QgsProcessingParameterNumber" and len(a) >= 2:
            self.defaultValue = a[1]
        if type(self).__name__ == "QgsProcessingParameterEnum":
            self.options = a[0] if a else kw.get("options")
        if type(self).__name__ == "QgsProcessingParameterFeatureSource" and len(a) >= 2:
            self.defaultValue = a[1]
        if type(self).__name__ == "QgsProcessingParameterFeatureSink" and len(a) >= 1:
            pass


for _n in ("QgsProcessingParameterFeatureSource", "QgsProcessingParameterFile", "QgsProcessingParameterEnum",
           "QgsProcessingParameterNumber", "QgsProcessingParameterBoolean", "QgsProcessingParameterRasterLayer",
           "QgsProcessingParameterFolderDestination", "QgsProcessingParameterFeatureSink",
           "QgsProcessingParameterField", "QgsProcessingParameterString", "QgsProcessingParameterFileDestination",
           "QgsProcessingParameterVectorLayer", "QgsProcessingOutputFile"):
    globals()[_n] = type(_n, (_Param,), {})


class QgsProcessingParameterDefinition(_Param):
    if QGIS4 or _FLAVOR == "3.40":
        Flag = Qgis.ProcessingParameterFlag
        if not QGIS4:
            FlagHidden, FlagAdvanced, FlagOptional = 16, 2, 8


class QgsProcessingProvider:
    def __init__(self): self.algs = []
    def addAlgorithm(self, a): self.algs.append(a)


class QgsProcessingAlgorithm:
    def __init__(self):
        self._params = {}
        self.sinks = {}
    def addParameter(self, p): self._params[p.name] = p; return True
    # 0.16.0: what suite_data_tools.data_aware reads (the definitions as added)
    def parameterDefinition(self, name): return self._params.get(name)
    def parameterDefinitions(self): return list(self._params.values())
    def addOutput(self, o): return True
    def _v(self, params, k):
        if k not in self._params:
            raise KeyError(f"unknown parameter {k}")
        v = params.get(k, None)
        return self._params[k].defaultValue if v is None else v
    def parameterAsSource(self, params, k, context):
        v = self._v(params, k)
        if v is None or v == "": return None
        if isinstance(v, str):
            if v in context.layers:
                return context.layers[v]
            path = v.split("|")[0]
            if os.path.isfile(path):                   # a GeoPackage path (the headless chain / files)
                lyr = QgsVectorLayer(v, os.path.basename(path), "ogr")
                if lyr.isValid():
                    return lyr
            raise KeyError(v)
        return v
    parameterAsRasterLayer = parameterAsSource
    parameterAsVectorLayer = parameterAsSource
    def parameterAsString(self, params, k, context):
        v = self._v(params, k); return "" if v is None else str(v)
    parameterAsFileOutput = parameterAsString
    def parameterAsDouble(self, params, k, context): return float(self._v(params, k))
    def parameterAsInt(self, params, k, context): return int(self._v(params, k))
    def parameterAsBool(self, params, k, context): return bool(self._v(params, k))
    def parameterAsEnum(self, params, k, context): return int(self._v(params, k) or 0)
    def parameterAsEnums(self, params, k, context):
        v = self._v(params, k); return list(v) if isinstance(v, (list, tuple)) else [int(v)]
    def parameterAsSink(self, params, k, context, fields, wkb, crs):
        s = FakeSink(QgsFields(fields), wkb)
        key = context.add(s)          # registered; replaced by a source on read
        context.layers[key] = s
        self.sinks[k] = s
        return s, key
    def run_mock(self, params, context, feedback):
        self._params = {}; self.initAlgorithm()
        res = self.processAlgorithm(params, context, feedback)
        for k, v in list(res.items()):
            if isinstance(v, str) and isinstance(context.layers.get(v), FakeSink):
                context.layers[v] = context.layers[v].as_source()
        return res


class QgsVectorFileWriter:
    class WriterError(_enum.IntEnum):
        NoError = 0
    class ActionOnExistingFile(_enum.IntEnum):
        CreateOrOverwriteFile, CreateOrOverwriteLayer = 1, 2
    if not QGIS4:
        NoError, CreateOrOverwriteFile, CreateOrOverwriteLayer = 0, 1, 2
    written = {}
    class SaveVectorOptions:
        driverName = layerName = None; actionOnExistingFile = 0
    @classmethod
    def create(cls, path, fields, wkb, crs, tctx, opts):
        w = cls(); w.fields = fields; w.feats = []
        cls.written[(path, opts.layerName)] = w
        if opts.actionOnExistingFile == cls.ActionOnExistingFile.CreateOrOverwriteFile:
            open(path, "w").close()
        return w
    def hasError(self): return 0
    def errorMessage(self): return ""
    def addFeature(self, f): self.feats.append(f); return True


class QgsSettings:
    """A process-wide dict (the real one is the QGIS profile's settings)."""
    _store = {}
    def value(self, key, default=None): return QgsSettings._store.get(key, default)
    def setValue(self, key, v): QgsSettings._store[key] = v
    def remove(self, key): QgsSettings._store.pop(key, None)


class QgsMessageLog:
    LOG = []
    @staticmethod
    def logMessage(msg, tag="", level=None): QgsMessageLog.LOG.append((tag, level, msg))


def _wkb_read(b, o=0):
    """(kind, parts, next offset) of little / big endian ISO WKB (2D; Z / M skipped)."""
    import struct
    end = "<" if b[o] == 1 else ">"
    (code,) = struct.unpack_from(end + "I", b, o + 1); o += 5
    dim = (code % 10000) // 1000
    base = (code & 0x0FFFFFFF) % 1000
    step = 8 * (2 + (dim in (1, 3)) + (dim in (2, 3)))

    def seq(o):
        (n,) = struct.unpack_from(end + "I", b, o); o += 4
        return [struct.unpack_from(end + "dd", b, o + step * i) for i in range(n)], o + step * n
    if base == 1:
        return "point", [[struct.unpack_from(end + "dd", b, o)]], o + step
    if base == 2:
        pts, o = seq(o); return "line", [pts], o
    if base == 3:
        (n,) = struct.unpack_from(end + "I", b, o); o += 4
        rings = []
        for _ in range(n):
            pts, o = seq(o); rings.append(pts)
        return "polygon", rings[:1], o                  # interior rings ignored (as the mock)
    (n,) = struct.unpack_from(end + "I", b, o); o += 4
    kind, parts = None, []
    for _ in range(n):
        kind, pp, o = _wkb_read(b, o); parts += pp
    return kind, parts, o


def _gpkg_geometry(blob):
    if blob is None:
        return QgsGeometry()
    blob = bytes(blob)
    flags = blob[3]
    env = {0: 0, 1: 32, 2: 48, 3: 48, 4: 64}[(flags >> 1) & 7]
    kind, parts, _o = _wkb_read(blob, 8 + env)
    return QgsGeometry(kind, [list(p) for p in parts])


class QgsVectorLayer(FakeSource):
    """'<file.gpkg>|layername=<name>' read with sqlite3 (GeoPackage binary geometry + WKB)."""
    def __init__(self, uri="", name="", provider="ogr"):
        import sqlite3
        self._uri = str(uri)
        path, _, opts = str(uri).partition("|")
        lname = opts.split("layername=", 1)[1] if "layername=" in opts else None
        self._valid = False
        FakeSource.__init__(self, QgsFields(), [], _WKB_POINT, name)
        try:
            con = sqlite3.connect(path)
            if lname is None:
                lname = con.execute("SELECT table_name FROM gpkg_contents").fetchone()[0]
            gcol, gtype = con.execute("SELECT column_name, geometry_type_name FROM gpkg_geometry_columns "
                                      "WHERE table_name=?", (lname,)).fetchone()
            cols = [(r[1], r[2]) for r in con.execute('PRAGMA table_info("{}")'.format(lname))]
            names = [c for c, _t in cols if c not in ("fid", gcol)]
            fl = QgsFields()
            for c, t in cols:
                if c in names:
                    fl.append(QgsField(c, "double" if t.upper() in ("REAL", "DOUBLE", "FLOAT")
                                       else "int" if "INT" in t.upper() else "string"))
            feats = []
            for row in con.execute('SELECT "{}", {} FROM "{}" ORDER BY fid'.format(
                    gcol, ", ".join('"{}"'.format(n) for n in names) or "1", lname)):
                f = QgsFeature(fl); f.setGeometry(_gpkg_geometry(row[0]))
                f.setAttributes(list(row[1:1 + len(names)])); feats.append(f)
            con.close()
        except Exception:
            return
        wkb = {"POINT": _WKB_POINT, "MULTIPOINT": _WKB_POINT,
               "LINESTRING": _WKB_LINE, "MULTILINESTRING": _WKB_MLINE,
               "POLYGON": _WKB_POLY, "MULTIPOLYGON": _WKB_POLY}.get(gtype.upper(), 1)
        FakeSource.__init__(self, fl, feats, wkb, lname)
        self._valid = True
    def isValid(self): return self._valid
    def id(self): return "layer_%x" % id(self)
    def source(self): return self._uri
    def loadNamedStyle(self, path):
        self.styles = getattr(self, "styles", []) + [path]
        return ("", True)
    def triggerRepaint(self): pass


class QgsApplication:
    _reg = None
    @classmethod
    def processingRegistry(cls):
        class R:
            providers = []
            def addProvider(self, p): self.providers.append(p); p.loadAlgorithms(); return True
            def removeProvider(self, p): self.providers.remove(p)
            def algorithmById(self, alg_id):
                """Full ids registered in qgis.processing.REGISTRY (test doubles of other
                providers), or the screening's own tools ('nddam:<name>')."""
                from qgis import processing
                if alg_id in processing.REGISTRY:
                    return processing.REGISTRY[alg_id].createInstance()
                if alg_id.startswith("nddam:") and alg_id[6:] in processing.REGISTRY:
                    return processing.REGISTRY[alg_id[6:]].createInstance()
                return None
        cls._reg = cls._reg or R(); return cls._reg


def __getattr__(name):            # other names used only by tools this harness does not run
    if name.startswith("Qgs"):
        return type(name, (_Param,), {})
    raise AttributeError(name)
