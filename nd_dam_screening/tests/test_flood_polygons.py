"""0.26.0: flood polygons from the step 7 grids (core/flood_polygons.py): the masks (pure numpy,
always checked) and the GeoPackage with the extent, velocity_class and arrival layers (real GDAL;
SKIP without it, as in test_dem_local_024.py).

Run: PYTHONPATH=tests/qgis_mock:<parent of the plugin> python3 tests/test_flood_polygons.py
"""
import os
import shutil
import sys
import tempfile

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
MOCK = os.path.join(HERE, "qgis_mock")

# the real GDAL first (the test double has no Polygonize and no GeoPackage driver), then the
# QGIS doubles for the package import
sys.path[:] = [p for p in sys.path if os.path.abspath(p or ".") != MOCK]
try:
    from osgeo import gdal, ogr, osr
    HAS_GDAL = hasattr(gdal, "Polygonize") and ogr.GetDriverByName("GPKG") is not None
except Exception:
    gdal = ogr = osr = None
    HAS_GDAL = False
sys.path.insert(0, MOCK)
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))

from nd_dam_screening.core import flood_polygons as fp  # noqa: E402
from nd_dam_screening.core import hazard as hz  # noqa: E402

# ---------------------------------------------------------------- the synthetic grids
# 20 x 20 cells of 10 m (EPSG:5070 numbers); a 6 x 6 wet block, one shallow cell wet by the 0.1 ft
# rule (0.05 m), one too shallow (0.02 m), no-data along the top row
NY = NX = 20
CELL = 10.0
GT = (500000.0, CELL, 0.0, 2000000.0, 0.0, -CELL)
depth = np.zeros((NY, NX))
depth[5:11, 5:11] = 1.0
depth[2, 2] = 0.05                       # 0.16 ft: wet (the suite's 0.1 ft), dry by a 1 ft rule
depth[2, 4] = 0.02                       # 0.07 ft: dry
depth[0, :] = fp.NODATA
vel = np.zeros((NY, NX))
vel[5:11, 5:7] = 0.3                     # 1.0 ft/s  -> 0-2 ft/s
vel[5:11, 7:9] = 1.0                     # 3.3 ft/s  -> 2-5 ft/s
vel[5:11, 9:11] = 2.0                    # 6.6 ft/s  -> over 5 ft/s
vel[2, 2] = 0.1
vel[2, 4] = 3.0                          # fast but dry: never counted
arrive = np.full((NY, NX), fp.NODATA)
arrive[5:7, 5:11] = 0.25
arrive[7:9, 5:11] = 1.5
arrive[9:11, 5:11] = 6.0
arrive[2, 2] = 0.4
arrive[2, 4] = 0.1

# ---------------------------------------------------------------- 1. the masks (numpy only)
assert abs(fp.DEFAULT_WET_M - 0.1 * 0.3048) < 1e-12 and fp.DEFAULT_WET_M == hz.DEFAULT_WET_M   # 0.1 ft, not 1 ft
wet = fp.wet_mask(depth)
assert wet.sum() == 37 and wet[2, 2] and not wet[2, 4] and not wet[0].any()
assert fp.wet_mask(depth, wet_m=0.3048).sum() == 36                     # a 1 ft rule would drop the shallow cell
vc = fp.velocity_class_masks(vel, wet)
assert [lab for lab, _m in vc] == ["0-2 ft/s", "2-5 ft/s", "over 5 ft/s"]
assert vc[0][1].sum() == 13 and vc[1][1].sum() == 12 and vc[2][1].sum() == 12
assert not vc[2][1][2, 4]                                                 # dry cells never count
am = fp.arrival_masks(arrive, wet)
assert [h for h, _m in am] == [0.5, 1.0, 2.0, 4.0, 8.0]
assert [int(m.sum()) for _h, m in am] == [13, 13, 25, 25, 37]            # cumulative, no-data left out
assert fp.GPKG_NAME == "7_flood_polygons.gpkg"
print("OK (masks: wet at 0.1 ft, three velocity classes, five arrival hours)")

# ---------------------------------------------------------------- 2. the GeoPackage (real GDAL)
if HAS_GDAL:
    assert fp.available()
    tmp = tempfile.mkdtemp(prefix="floodpoly_")
    try:
        gpkg = os.path.join(tmp, fp.GPKG_NAME)
        n = fp.write_flood_polygons(gpkg, "DWR-1", "td", depth, vel, arrive, GT)
        assert n == {"extent": 1, "velocity_class": 3, "arrival": 5, "arrival_contour": 3, "deleted": 0}, n
        assert os.path.isfile(gpkg)

        def read(layer):
            ds = ogr.Open(gpkg)
            lyr = ds.GetLayerByName(layer)
            assert lyr is not None, layer
            srs = lyr.GetSpatialRef()
            assert srs is not None and srs.GetAuthorityCode(None) == "5070", layer
            names = [lyr.GetLayerDefn().GetFieldDefn(i).GetName() for i in range(lyr.GetLayerDefn().GetFieldCount())]
            rows = []
            for f in lyr:
                g = f.GetGeometryRef()
                kind = g.GetGeometryName()
                rows.append((dict((k, f.GetField(k)) for k in names),
                             g.GetArea() if "POLYGON" in kind else g.Length(), kind))
            ds = None
            return names, rows

        names, rows = read("extent")
        assert names == ["dam_id", "scenario"] and len(rows) == 1
        assert rows[0][0] == {"dam_id": "DWR-1", "scenario": "td"} and abs(rows[0][1] - 37 * CELL * CELL) < 1e-6
        assert rows[0][2] == "MULTIPOLYGON"
        names, rows = read("velocity_class")
        assert names == ["dam_id", "scenario", "vel_class"]
        area = dict((r[0]["vel_class"], r[1]) for r in rows)
        assert abs(area["0-2 ft/s"] - 1300.0) < 1e-6 and abs(area["2-5 ft/s"] - 1200.0) < 1e-6
        assert abs(area["over 5 ft/s"] - 1200.0) < 1e-6
        names, rows = read("arrival")
        assert names == ["dam_id", "scenario", "within_hr"]
        area = dict((float(r[0]["within_hr"]), r[1]) for r in rows)
        assert sorted(area) == [0.5, 1.0, 2.0, 4.0, 8.0]
        assert abs(area[0.5] - 1300.0) < 1e-6 and abs(area[2.0] - 2500.0) < 1e-6 and abs(area[8.0] - 3700.0) < 1e-6
        # the polygons sit on the grid (the wet block's corner is at cell (5, 5))
        ds = ogr.Open(gpkg)
        lyr = ds.GetLayerByName("extent")
        feat = next(iter(lyr))
        env = feat.GetGeometryRef().GetEnvelope()                           # (xmin, xmax, ymin, ymax)
        feat = lyr = ds = None
        assert abs(env[0] - (GT[0] + 2 * CELL)) < 1e-6 and abs(env[1] - (GT[0] + 11 * CELL)) < 1e-6, env
        assert abs(env[3] - (GT[3] - 2 * CELL)) < 1e-6 and abs(env[2] - (GT[3] - 11 * CELL)) < 1e-6, env

        # a rerun of the same dam and scenario replaces, never duplicates
        n2 = fp.write_flood_polygons(gpkg, "DWR-1", "td", depth, vel, arrive, GT)
        assert n2 == {"extent": 1, "velocity_class": 3, "arrival": 5, "arrival_contour": 3, "deleted": 12}, n2
        assert len(read("extent")[1]) == 1 and len(read("velocity_class")[1]) == 3 and len(read("arrival")[1]) == 5
        assert len(read(fp.LAYER_CONTOUR)[1]) == 3
        # another scenario and another dam add to the same file; a dry dam adds nothing
        n3 = fp.write_flood_polygons(gpkg, "DWR-1", "sd", depth * 0.5, vel, arrive, GT)
        n4 = fp.write_flood_polygons(gpkg, "DWR-2", "td", depth, vel * 0.0, arrive, GT)
        n5 = fp.write_flood_polygons(gpkg, "DWR-3", "td", np.zeros((NY, NX)), vel, arrive, GT)
        assert n3["extent"] == 1 and n4["velocity_class"] == 1
        assert n5 == {"extent": 0, "velocity_class": 0, "arrival": 0, "arrival_contour": 0, "deleted": 0}, n5
        rows = read("extent")[1]
        assert sorted((r[0]["dam_id"], r[0]["scenario"]) for r in rows) == [("DWR-1", "sd"), ("DWR-1", "td"), ("DWR-2", "td")]
        assert all(r[0]["vel_class"] == "0-2 ft/s" for r in read("velocity_class")[1] if r[0]["dam_id"] == "DWR-2")
        # 0.28.0: the arrival contours are LINES at 30 min, 1 h, 2 h, each the outline of its
        # "reached within" area, and they carry the label an EAP map is read with
        names, rows = read(fp.LAYER_CONTOUR)
        assert names == ["dam_id", "scenario", "within_hr", "label"], names
        got = dict((float(r[0]["within_hr"]), (r[0]["label"], r[2])) for r in rows
                   if r[0]["dam_id"] == "DWR-1" and r[0]["scenario"] == "td")
        assert sorted(got) == [0.5, 1.0, 2.0], sorted(got)
        assert [got[h][0] for h in (0.5, 1.0, 2.0)] == ["30 min", "1 h", "2 h"], got
        assert all(got[h][1] == "MULTILINESTRING" for h in got), got
        ds = ogr.Open(gpkg)
        lyr = ds.GetLayerByName(fp.LAYER_CONTOUR)
        lyr.SetAttributeFilter("dam_id = 'DWR-1' AND scenario = 'td' AND within_hr = 0.5")
        feat = next(iter(lyr))
        length = feat.GetGeometryRef().Length()
        feat = lyr = ds = None
        assert abs(length - 200.0) < 1e-6, length        # the outline of the 13 cells reached by 0.5 h
        print("OK (GeoPackage: extent, velocity_class, arrival, arrival_contour in EPSG:5070; "
              "rerun replaces)")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
else:
    print("SKIP (GeoPackage: real GDAL with Polygonize and the GPKG driver not available)")
print("OK")
