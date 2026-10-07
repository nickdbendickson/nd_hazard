# -*- coding: utf-8 -*-
"""ACER TM 11 zone helpers.

Since 2.5.1 the ACER zone of a cell or a structure comes from its maximum
depth together with its maximum velocity (Dmax / Vmax) - the practice the
ND reviewers expect. The zone rasters (Hazard_Foundations.tif,
Hazard_MobileHomes.tif, Hazard_Vehicles.tif) are written by
processing.generate_hazard_rasters on the depth grid with the class
numbering 1 High, 2 Judgment, 3 Low, -9999 NoData. The D x V-bound and
time-step methods of 2.0 - 2.5.0 were removed in 2.6.
"""

import numpy as np
from osgeo import gdal

from .constants import CLASS_HIGH, CLASS_JUDGMENT, CLASS_LOW
from .helpers import evaluate_polynomial, get_layer_path

METHOD_MAXMAX = "maxmax"          # recorded in run snapshots / logs
METHOD_LABEL = "max depth with max velocity (Dmax / Vmax, same cell)"
ZONE_FILES = {"found": "Hazard_Foundations.tif", "mobile": "Hazard_MobileHomes.tif",
              "vehicle": "Hazard_Vehicles.tif"}


def zone_margins(d, v, low, high, v_max=None):
    """(margin_to_high, margin_to_judgment) in ft for arrays of (d, v): the
    depth still missing to reach the boundary (<= 0: already reached)."""
    vv = np.asarray(v, dtype=np.float64)
    if v_max is not None:
        vv = np.clip(vv, 0.0, v_max)
    d = np.asarray(d, dtype=np.float64)
    return evaluate_polynomial(high, vv) - d, evaluate_polynomial(low, vv) - d


def zone_label(cls):
    return {CLASS_HIGH: "HIGH", CLASS_JUDGMENT: "JUDGMENT", CLASS_LOW: "LOW"}.get(cls, "DRY")


def layer_path(layer_or_path):
    if layer_or_path is None:
        return None
    if isinstance(layer_or_path, str):
        return layer_or_path
    return get_layer_path(layer_or_path)


# ------------------------------------------------------------- strip reader --
def _aligned(ds_ref, ds, nodata):
    """`ds` on the grid of `ds_ref` (lazy VRT warp, nearest neighbour)."""
    gt_r, gt = ds_ref.GetGeoTransform(), ds.GetGeoTransform()
    if (ds_ref.RasterXSize == ds.RasterXSize and ds_ref.RasterYSize == ds.RasterYSize
            and all(abs(a - b) < 1e-9 for a, b in zip(gt_r, gt))
            and ds_ref.GetProjection() == ds.GetProjection()):
        return ds
    from .processing import _aligned_velocity
    return _aligned_velocity(ds_ref, ds, nodata)


def _scale_offset(band):
    scale, offset = band.GetScale(), band.GetOffset()
    return (1.0 if scale in (None, 0) else float(scale)), (0.0 if offset is None else float(offset))


class _Grid(object):
    """One raster aligned to a reference grid, read strip by strip (EAP
    arrival bands and other whole-raster passes)."""

    def __init__(self, ds_ref, path):
        self.ds = gdal.Open(path, gdal.GA_ReadOnly)
        if self.ds is None:
            raise RuntimeError("Could not open {}".format(path))
        src_band = self.ds.GetRasterBand(1)
        self.nodata = src_band.GetNoDataValue()
        self.scale, self.offset = _scale_offset(src_band)
        self.al = _aligned(ds_ref, self.ds, self.nodata)
        self.band = self.al.GetRasterBand(1)
        nd = self.band.GetNoDataValue()
        self.nd = self.nodata if nd is None else nd

    def read(self, y0, h, width):
        """(values, valid) of rows y0 .. y0 + h (scale / offset applied)."""
        a = self.band.ReadAsArray(0, y0, width, h)
        if a is None:
            raise RuntimeError("read failed")
        a = np.asarray(a, dtype=np.float64)
        valid = np.isfinite(a) & (a > -9999)
        if self.nd is not None:
            valid &= a != self.nd
        if self.scale != 1.0 or self.offset != 0.0:
            a = a * self.scale + self.offset
        return a, valid

    def close(self):
        self.band = None
        self.al = None
        self.ds = None
