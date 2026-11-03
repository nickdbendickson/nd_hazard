"""Hazard Helper 2.35.5 (audit 2026-11-03, option C): a structure's DV is its peak depth x the peak velocity
over its wet cells - the pair its zone is classified on - never a DV raster's value; with the
velocity unknown at every wet cell DV is None (depth-only severity) and the Notes say so; a
NaN velocity on a wet cell is filled with the structure's peak velocity. Synthetic rasters only.

    QT_QPA_PLATFORM=offscreen python3 ND_Dam_Hazard_Helper/tests/realqt_dv_peak_pair.py

Run by tests/test_dv_peak_pair.py (its own process, real QGIS).

Needs PyQGIS (inits a headless QgsApplication) and GDAL.
"""
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from qgis.core import QgsApplication  # noqa: E402
except ImportError:
    print("SKIP: no real QGIS")
    sys.exit(0)
_app = QgsApplication.instance() or QgsApplication([], False)
_app.setPrefixPath(os.environ.get("QGIS_PREFIX_PATH", "/home/box/miniforge3/envs/qgis334"), True)
_app.initQgis()

import numpy as np  # noqa: E402
from osgeo import gdal, osr  # noqa: E402
from qgis.core import (  # noqa: E402
    QgsCoordinateReferenceSystem, QgsCoordinateTransformContext, QgsFeature, QgsField,
    QgsGeometry, QgsPointXY, QgsRasterLayer, QgsRectangle,
)

from ND_Dam_Hazard_Helper import consequences as CQ, structures as ST  # noqa: E402
from ND_Dam_Hazard_Helper.compat import FT_INT, FT_STRING, WKB_POINT, WKB_POLYGON  # noqa: E402
from ND_Dam_Hazard_Helper.helpers import RasterSampler, create_memory_layer  # noqa: E402
from ND_Dam_Hazard_Helper.processing import make_samplers  # noqa: E402

CRS = QgsCoordinateReferenceSystem("EPSG:5070")
CTX = QgsCoordinateTransformContext()
X0, Y0 = 100000.0, 2600000.0
NODATA = -9999.0


def _tif(path, arr):
    ds = gdal.GetDriverByName("GTiff").Create(path, arr.shape[1], arr.shape[0], 1,
                                              gdal.GDT_Float32)
    ds.SetGeoTransform((X0, 1.0, 0.0, Y0 + arr.shape[0], 0.0, -1.0))
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(5070)
    ds.SetProjection(srs.ExportToWkt())
    ds.GetRasterBand(1).SetNoDataValue(NODATA)
    ds.GetRasterBand(1).WriteArray(arr.astype(np.float32))
    ds = None


def _house():
    lyr = create_memory_layer("FEMA_Structures", WKB_POLYGON, CRS,
                              [QgsField("building", FT_STRING)])
    f = QgsFeature(lyr.fields())
    f.setGeometry(QgsGeometry.fromRect(QgsRectangle(X0 + 10, Y0 + 10, X0 + 20, Y0 + 20)))
    f.setAttributes(["house"])
    lyr.dataProvider().addFeatures([f])
    nena = [("Add_Number", FT_INT), ("St_Name", FT_STRING), ("Placement", FT_STRING),
            ("Place_Type", FT_STRING)]
    pts = create_memory_layer("NDGISHUB_address_points", WKB_POINT, CRS,
                              [QgsField(n, t) for n, t in nena])
    p = QgsFeature(pts.fields())
    p.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(X0 + 15, Y0 + 15)))
    p.setAttributes([9250, "56TH", "Unknown", "Residence"])
    pts.dataProvider().addFeatures([p])
    return lyr, pts


def _run(tmp, vel, dv=None):
    depth = np.zeros((30, 30))
    depth[5:25, 5:25] = 2.0
    depth[12, 12] = 3.0                         # the deepest wet cell
    dpath, vpath = os.path.join(tmp, "d.tif"), os.path.join(tmp, "v.tif")
    _tif(dpath, depth)
    _tif(vpath, vel)
    house, pts = _house()
    structs = ST.build_inventory(house, [("NDGISHUB", pts)], CRS, CTX)
    assert len(structs) == 1, structs
    smp = make_samplers(QgsRasterLayer(dpath, "d"), QgsRasterLayer(vpath, "v"),
                        QgsRasterLayer(vpath, "a"), 0)
    if dv is not None:
        dvpath = os.path.join(tmp, "dv.tif")
        _tif(dvpath, dv)
        smp["dv"] = RasterSampler(QgsRasterLayer(dvpath, "dv"), 0)
    ST.evaluate_structures(structs, smp, CRS, CTX)
    return structs[0]


def main():
    tmp = tempfile.mkdtemp(prefix="dv_peak_")
    try:
        # 1. DV = peak depth x peak wet-cell velocity, even from different cells; a DV raster
        #    (here 999 everywhere) is not used for dv_max
        vel = np.full((30, 30), 1.0)
        vel[20, 20] = 4.0                       # the fastest wet cell, not the deepest
        rec = _run(tmp, vel, dv=np.full((30, 30), 999.0))
        assert rec["max_depth"] is not None and abs(rec["max_depth"] - 3.0) < 1e-6, rec
        assert rec["dv_max"] is not None and abs(rec["dv_max"] - 3.0 * 4.0) < 1e-6, rec["dv_max"]
        sev, why = CQ.flood_severity(rec["dv_max"], rec["max_depth"])
        assert why.startswith("DV 12 "), why
        print("ok   DV = peak depth x peak wet-cell velocity (3.0 x 4.0 = 12), DV raster not used")
        # 2. a NaN velocity on the deepest cell is filled with the structure's peak, noted
        vel2 = vel.copy()
        vel2[12, 12] = NODATA
        rec = _run(tmp, vel2)
        assert rec.get("vel_missing") and not rec.get("vel_unknown"), rec
        assert abs(rec["dv_max"] - 12.0) < 1e-6, rec["dv_max"]
        notes = "; ".join(ST.structure_notes(rec))
        assert ("filled with the structure's peak velocity, else 0) - verify" in notes), notes
        print("ok   NaN velocity filled with the structure's peak velocity (noted)")
        # 3. velocity unknown at every wet cell: no DV, depth-only severity, said in Notes
        rec = _run(tmp, np.full((30, 30), NODATA))
        assert rec.get("vel_unknown") and rec["dv_max"] is None, (rec.get("vel_unknown"),
                                                                  rec["dv_max"])
        notes = "; ".join(ST.structure_notes(rec))
        assert "velocity unknown at every wet cell - depth only, no DV - verify" in notes, notes
        sev, why = CQ.flood_severity(rec["dv_max"], rec["max_depth"])
        assert "velocity unknown - no DV" in why, why
        print("ok   velocity unknown: DV None, depth-only severity, noted")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("ALL OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
