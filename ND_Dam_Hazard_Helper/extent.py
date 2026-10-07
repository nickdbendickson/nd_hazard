# -*- coding: utf-8 -*-
"""Study area: the depth raster's extent grown by a distance in feet.

Every structure source (OSM outlines, NSI, ND GIS Hub address points, FEMA,
POI and the downloads) is limited to this area. The test is done in the
raster's CRS, so it is exact whatever CRS a layer uses; a bounding box in
the layer's own CRS is only used to let the data provider skip far-away
features quickly.
"""

import math

from qgis.core import (
    QgsRectangle, QgsGeometry, QgsCoordinateTransform, QgsCoordinateReferenceSystem,
    QgsPointXY,
)

from .compat import DIST_DEGREES, MSG_WARNING
from .helpers import buffer_distance_in_layer_units, log_message, transform_geometry

_WEB_MERCATOR_IDS = ("EPSG:3857", "EPSG:900913", "EPSG:102100", "EPSG:102113", "EPSG:3785",
                     "ESRI:102100", "ESRI:102113")
_WGS84 = "EPSG:4326"


def is_web_mercator(crs):
    try:
        if (crs.authid() or "").upper() in _WEB_MERCATOR_IDS:
            return True
        desc = (crs.description() or "").lower()
        return "pseudo-mercator" in desc or "pseudo mercator" in desc or \
            "web mercator" in desc
    except Exception:
        return False


def is_geographic(crs):
    try:
        return bool(crs.isGeographic())
    except Exception:
        try:
            return crs.mapUnits() == DIST_DEGREES
        except Exception:
            return False


def center_latitude(rect, crs, transform_ctx):
    """Latitude (degrees) of the rectangle centre; None if unknown."""
    try:
        cx = (rect.xMinimum() + rect.xMaximum()) / 2.0
        cy = (rect.yMinimum() + rect.yMaximum()) / 2.0
        xf = QgsCoordinateTransform(crs, QgsCoordinateReferenceSystem(_WGS84), transform_ctx)
        lat = cy if xf.isShortCircuited() else xf.transform(QgsPointXY(cx, cy)).y()
        return float(lat) if math.isfinite(lat) and -90.0 <= lat <= 90.0 else None
    except Exception:
        return None


def distance_xy_in_crs(distance_ft, crs, lat=None):
    """(dx, dy) in CRS units that cover `distance_ft` on the ground near
    latitude `lat`.

    - projected CRS in feet / metres: the plain unit conversion
    - geographic CRS: a degree of longitude shrinks with cos(latitude)
    - Web Mercator: map units are stretched by 1 / cos(latitude)
      (~1.46x in North Dakota), so a plain conversion would be too small.
    """
    d = buffer_distance_in_layer_units(distance_ft, crs)
    if lat is None:
        return d, d
    c = math.cos(math.radians(max(-85.0, min(85.0, float(lat)))))
    if c <= 1e-6:
        return d, d
    if is_geographic(crs):
        return d / c, d
    if is_web_mercator(crs):
        return d / c, d / c
    return d, d


class ExtentClip(object):
    """Raster extent + `grow_ft`, in the raster CRS.

    contains_point / intersects_geometry take a feature in any CRS: get
    the transform once per layer with transform_from(layer.crs()) and pass
    it as `xform`. request_rect gives a slightly larger rectangle in a
    layer's CRS for QgsFeatureRequest.setFilterRect."""

    def __init__(self, raster_layer, grow_ft, transform_ctx, rect=None, crs=None):
        self.crs = crs if crs is not None else raster_layer.crs()
        base = QgsRectangle(rect if rect is not None else raster_layer.extent())
        self.base = base
        self.grow_ft = float(grow_ft or 0.0)
        self.ctx = transform_ctx
        self.lat = center_latitude(base, self.crs, transform_ctx)
        dx, dy = distance_xy_in_crs(self.grow_ft, self.crs, self.lat)
        self.rect = QgsRectangle(base.xMinimum() - dx, base.yMinimum() - dy,
                                 base.xMaximum() + dx, base.yMaximum() + dy)
        self.geom = QgsGeometry.fromRect(self.rect)
        self._xf = {}
        if is_web_mercator(self.crs) or is_geographic(self.crs):
            log_message("Depth raster CRS {} is {}: distances in feet are corrected for "
                        "latitude {:.2f}; a projected CRS (UTM / State Plane) is more "
                        "accurate for hazard work".format(
                            self.crs.authid() or "(custom)",
                            "geographic" if is_geographic(self.crs) else "Web Mercator",
                            self.lat if self.lat is not None else float("nan")),
                        MSG_WARNING)

    # -- helpers ---------------------------------------------------------------
    def describe(self):
        return "raster extent + {:g} ft".format(self.grow_ft)

    def transform_from(self, src_crs):
        """Cached transform src_crs -> clip CRS (None = same CRS)."""
        key = ""
        if src_crs is not None:
            key = src_crs.authid() or src_crs.toWkt()
        if key not in self._xf:
            xf = None
            try:
                if src_crs is not None and src_crs.isValid() and self.crs.isValid():
                    t = QgsCoordinateTransform(src_crs, self.crs, self.ctx)
                    xf = None if t.isShortCircuited() else t
            except Exception:
                xf = None
            self._xf[key] = xf
        return self._xf[key]

    def contains_xy(self, x, y):
        r = self.rect
        return r.xMinimum() <= x <= r.xMaximum() and r.yMinimum() <= y <= r.yMaximum()

    def contains_point(self, pt, src_crs=None, xform=None):
        """pt: QgsPointXY in src_crs (or already in the clip CRS)."""
        if pt is None:
            return False
        xf = xform if xform is not None else (
            self.transform_from(src_crs) if src_crs is not None else None)
        if xf is not None:
            try:
                pt = xf.transform(pt)
            except Exception:
                return False       # outside the CRS's area of use = far away
        try:
            return self.contains_xy(pt.x(), pt.y())
        except Exception:
            return False

    def intersects_geometry(self, geom, src_crs=None, xform=None):
        """Any part of `geom` (in src_crs) inside the clip rectangle."""
        if geom is None or geom.isNull() or geom.isEmpty():
            return False
        xf = xform if xform is not None else (
            self.transform_from(src_crs) if src_crs is not None else None)
        g = transform_geometry(geom, xf) if xf is not None else geom
        if g is None:
            return False
        bb = g.boundingBox()
        r = self.rect
        if not bb.intersects(r):
            return False
        if (bb.xMinimum() >= r.xMinimum() and bb.xMaximum() <= r.xMaximum()
                and bb.yMinimum() >= r.yMinimum() and bb.yMaximum() <= r.yMaximum()):
            return True
        try:
            return bool(g.intersects(self.geom))
        except Exception:
            return True

    def request_rect(self, target_crs, margin=0.02):
        """Clip rectangle in target_crs, grown by `margin` of its size so the
        provider pre-filter never drops a feature the exact test keeps.
        None = do not pre-filter (transformation failed)."""
        try:
            if target_crs is None or not target_crs.isValid() or not self.crs.isValid():
                return None
            xf = QgsCoordinateTransform(self.crs, target_crs, self.ctx)
            if not xf.isValid() and self.crs != target_crs:
                return None      # invalid is NOT 'same CRS' (isShortCircuited() is True for both): no pre-filter
            out = QgsRectangle(self.rect) if xf.isShortCircuited() \
                else xf.transformBoundingBox(self.rect)
            if out is None or out.isEmpty():
                return None
            vals = (out.xMinimum(), out.yMinimum(), out.xMaximum(), out.yMaximum())
            if not all(math.isfinite(v) for v in vals):
                return None
            mx = (out.xMaximum() - out.xMinimum()) * margin
            my = (out.yMaximum() - out.yMinimum()) * margin
            return QgsRectangle(out.xMinimum() - mx, out.yMinimum() - my,
                                out.xMaximum() + mx, out.yMaximum() + my)
        except Exception:
            return None

    def bbox_4326(self, extra_ft=0.0):
        """(xmin, ymin, xmax, ymax) in EPSG:4326 for the web downloads,
        optionally grown by `extra_ft`. None when the raster has no CRS or
        the result is not a valid lon/lat box (nothing is downloaded then)."""
        if self.crs is None or not self.crs.isValid():
            return None
        rect = QgsRectangle(self.rect)
        if extra_ft:
            dx, dy = distance_xy_in_crs(extra_ft, self.crs, self.lat)
            rect = QgsRectangle(rect.xMinimum() - dx, rect.yMinimum() - dy,
                                rect.xMaximum() + dx, rect.yMaximum() + dy)
        try:
            xf = QgsCoordinateTransform(self.crs, QgsCoordinateReferenceSystem(_WGS84),
                                        self.ctx)
            r = rect if xf.isShortCircuited() else xf.transformBoundingBox(rect)
            box = (r.xMinimum(), r.yMinimum(), r.xMaximum(), r.yMaximum())
        except Exception:
            return None
        if not all(math.isfinite(v) for v in box):
            return None
        if not (-180.0 <= box[0] < box[2] <= 180.0 and -90.0 <= box[1] < box[3] <= 90.0):
            return None
        return box


def make_extent_clip(depth_layer, grow_ft, transform_ctx):
    """ExtentClip for the depth raster (None if the raster is unusable)."""
    try:
        if depth_layer is None or not depth_layer.isValid():
            return None
        ext = depth_layer.extent()
        if ext is None or ext.isEmpty():
            return None
        return ExtentClip(depth_layer, grow_ft, transform_ctx)
    except Exception as e:
        log_message("Study area could not be built ({}); layers are not clipped".format(e),
                    MSG_WARNING)
        return None
