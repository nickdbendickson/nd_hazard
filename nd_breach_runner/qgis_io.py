"""Reading vector layers into the plain arrays the core uses.

Features are read inside a box around the dam (a statewide road or structure
layer has far more features than any dam's flood can touch) and transformed to
the model CRS."""
import numpy as np

from qgis.core import QgsFeatureRequest, QgsRectangle


def _box(center_xy, radius_m, work_to_layer):
    r = QgsRectangle(center_xy[0] - radius_m, center_xy[1] - radius_m,
                     center_xy[0] + radius_m, center_xy[1] + radius_m)
    try:
        return work_to_layer.transformBoundingBox(r)
    except Exception:
        return r


def _features(source, center_xy, radius_m, work_to_layer):
    if source is None:
        return []
    if center_xy is None:
        return list(source.getFeatures())
    req = QgsFeatureRequest()
    req.setFilterRect(_box(center_xy, radius_m, work_to_layer))
    return list(source.getFeatures(req))


def _tf(tf, pt):
    q = tf.transform(pt)
    return (q.x(), q.y())


def points(source, to_work, work_to_layer=None, center_xy=None, radius_m=0.0, id_field=None):
    """([(x, y)], [id]) of the point features in the box."""
    xy, ids = [], []
    for f in _features(source, center_xy, radius_m, work_to_layer):
        g = f.geometry()
        if g is None or g.isEmpty():
            continue
        pts = g.asMultiPoint() if g.isMultipart() else [g.asPoint()]
        for p in pts:
            xy.append(_tf(to_work, p))
            ids.append(str(f[id_field]) if id_field else str(f.id()))
    return np.asarray(xy, dtype=float).reshape(-1, 2), ids


def geometry_kind(g):
    """'point' / 'line' / 'polygon' / '' from QgsGeometry.type() (an int in Qt5 builds, a
    scoped Qgis.GeometryType in Qt6 / QGIS 3.30+: both handled)."""
    try:
        t = g.type()
        t = int(getattr(t, "value", t))
    except Exception:
        return ""
    return {0: "point", 1: "line", 2: "polygon"}.get(t, "")


def points_and_footprints(source, to_work, work_to_layer=None, center_xy=None, radius_m=0.0,
                          id_field=None):
    """v3.4.0: a structures layer of points OR polygons. Points are receptors as before; a
    polygon gives its centroid as the receptor point and its outer ring(s) as a building
    footprint (holes ignored). Returns ([(x, y)], [id], [footprint ring (N, 2)], counts)
    with counts = {"points", "polygons", "other"}."""
    xy, ids, foot = [], [], []
    counts = {"points": 0, "polygons": 0, "other": 0}
    for f in _features(source, center_xy, radius_m, work_to_layer):
        g = f.geometry()
        if g is None or g.isEmpty():
            continue
        kind = geometry_kind(g)
        fid = str(f[id_field]) if id_field else str(f.id())
        if kind == "polygon":
            polys = g.asMultiPolygon() if g.isMultipart() else [g.asPolygon()]
            rings = [np.asarray([_tf(to_work, p) for p in rings[0]], dtype=float)
                     for rings in polys if rings and len(rings[0]) >= 3]
            if not rings:
                continue
            counts["polygons"] += 1
            foot.extend(rings)
            c = g.centroid()
            xy.append(_tf(to_work, c.asPoint()))
            ids.append(fid)
        elif kind == "line":
            counts["other"] += 1
        else:
            pts = g.asMultiPoint() if g.isMultipart() else [g.asPoint()]
            counts["points"] += 1
            for p in pts:
                xy.append(_tf(to_work, p))
                ids.append(fid)
    return np.asarray(xy, dtype=float).reshape(-1, 2), ids, foot, counts


def lines(source, to_work, work_to_layer=None, center_xy=None, radius_m=0.0, fields=()):
    """[(vertices (N, 2), {field: value})] for the line features in the box;
    a multipart line gives one entry per part."""
    out = []
    for f in _features(source, center_xy, radius_m, work_to_layer):
        g = f.geometry()
        if g is None or g.isEmpty():
            continue
        parts = g.asMultiPolyline() if g.isMultipart() else [g.asPolyline()]
        attrs = {}
        for name in fields:
            if name:
                try:
                    attrs[name] = f[name]
                except (KeyError, IndexError):
                    attrs[name] = None
        for part in parts:
            if len(part) >= 2:
                out.append((np.asarray([_tf(to_work, p) for p in part], dtype=float), attrs))
    return out


def polygons(source, to_work, work_to_layer=None, center_xy=None, radius_m=0.0, fields=()):
    """[(outer ring (N, 2), {field: value})] for the polygon features in the box
    (interior rings are ignored: a hole in a pond is an island)."""
    out = []
    for f in _features(source, center_xy, radius_m, work_to_layer):
        g = f.geometry()
        if g is None or g.isEmpty():
            continue
        polys = g.asMultiPolygon() if g.isMultipart() else [g.asPolygon()]
        attrs = {}
        for name in fields:
            if name:
                try:
                    attrs[name] = f[name]
                except (KeyError, IndexError):
                    attrs[name] = None
        for rings in polys:
            if rings and len(rings[0]) >= 3:
                out.append((np.asarray([_tf(to_work, p) for p in rings[0]], dtype=float), attrs))
    return out


def field_names(source):
    try:
        return list(source.fields().names())
    except Exception:
        return []


def num(v, default=None):
    """A field value as a float, or default for NULL / text / NaN."""
    if v is None or (hasattr(v, "isNull") and v.isNull()) or v == "":
        return default
    try:
        x = float(v)
    except (TypeError, ValueError):
        return default
    return default if x != x else x
