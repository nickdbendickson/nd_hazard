"""Tests for 0.24.0 standardised local DEM: local_tif_stem, write_sidecar, warp_to_local.

Synthetic test: a small ND-feet grid (EPSG:3721) warped to 10 m EPSG:5070 float32 GeoTIFF.
Runs without QGIS; uses real GDAL for the warp test (falls back to SKIP if unavailable).
"""
import os
import sys
import tempfile
import shutil

HERE = os.path.dirname(os.path.abspath(__file__))
MOCK = os.path.join(HERE, "qgis_mock")

# Remove mock osgeo so we get the real GDAL (same pattern as test_dwr_https.py)
sys.path[:] = [p for p in sys.path if os.path.abspath(p or ".") != MOCK]
try:
    from osgeo import gdal, osr
    gdal.UseExceptions()
    HAS_GDAL = hasattr(gdal, "Warp")
except Exception:
    gdal, osr, HAS_GDAL = None, None, False

# Now add mock back so nd_dam_screening can import QGIS stubs
sys.path.insert(0, MOCK)
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))

from nd_dam_screening.core.dem_download import local_tif_stem, write_sidecar, warp_to_local, tag_local_tif  # noqa: E402

# ---------------------------------------------------------------------------
# 1. local_tif_stem

assert local_tif_stem(0, 10.0, dam_id="DWR-77") == "DWR-77_10m_5070"
assert local_tif_stem(0, 1.0,  dam_id="DWR-77") == "DWR-77_1m_5070"
assert local_tif_stem(0, 10.0, dam_id="")        == "dam_10m_5070"
assert local_tif_stem(0, 10.0, dam_id="",
                      point=(-97.0, 47.0))        == "dam_m97p0000_47p0000_10m_5070"
assert local_tif_stem(1, 10.0, county="Cass")     == "Cass_10m_5070"
assert local_tif_stem(1, 1.0,  county="Cass")     == "Cass_1m_5070"
assert local_tif_stem(2, 10.0)                    == "north_dakota_10m_5070"
# special chars in dam_id are replaced with _
assert local_tif_stem(0, 10.0, dam_id="Elm River #1") == "Elm_River__1_10m_5070"
print("OK (local_tif_stem)")

# ---------------------------------------------------------------------------
# 2. write_sidecar

tmp = tempfile.mkdtemp()
try:
    tif = os.path.join(tmp, "DWR-77_10m_5070.tif")
    open(tif, "w").close()
    sid = write_sidecar(tif, "USGS 3DEP 10 m", 10.0, "3dep")
    assert sid == os.path.join(tmp, "DWR-77_10m_5070_dem.txt")
    assert os.path.isfile(sid)
    content = open(sid).read()
    assert "Source: USGS 3DEP 10 m" in content
    assert "CRS: EPSG:5070" in content
    assert "Units: metres" in content
    assert "File type: GeoTIFF float32" in content
    assert "Cell: 10 m" in content
    assert "EPSG:5070" in content            # native facts for 3dep

    sid2 = write_sidecar(tif, "ND DWR LiDAR", 1.0, "dwr")
    content2 = open(sid2).read()
    assert "EPSG:3721" in content2 or "Erdas" in content2
    assert "Cell: 1 m" in content2
    assert tag_local_tif(tif, "ND DWR LiDAR", 1.0) is False        # 0.26.0: not a raster: no tags, no error
    print("OK (write_sidecar)")
finally:
    shutil.rmtree(tmp, ignore_errors=True)

# ---------------------------------------------------------------------------
# 2b. 0.26.0: the sidecar's facts also go on the GeoTIFF itself (SOURCE, CRS, UNITS, CELL_M)

if HAS_GDAL:
    tmp = tempfile.mkdtemp()
    try:
        tif = os.path.join(tmp, "DWR-77_10m_5070.tif")
        ds = gdal.GetDriverByName("GTiff").Create(tif, 5, 5, 1, gdal.GDT_Float32)
        srs = osr.SpatialReference()
        srs.ImportFromEPSG(5070)
        ds.SetProjection(srs.ExportToWkt())
        ds.SetGeoTransform([-500000.0, 10.0, 0, 2500000.0, 0, -10.0])
        ds.GetRasterBand(1).Fill(500.0)
        ds.FlushCache()
        ds = None
        sid = write_sidecar(tif, "USGS 3DEP 10 m", 10.0, "3dep")
        assert os.path.isfile(sid)
        ds = gdal.Open(tif)
        meta = ds.GetMetadata()
        ds = None
        assert meta.get("SOURCE") == "USGS 3DEP 10 m", meta
        assert meta.get("CRS") == "EPSG:5070" and meta.get("UNITS") == "metres" and meta.get("CELL_M") == "10", meta
        assert tag_local_tif(tif, "ND DWR LiDAR (lidar.dwr.nd.gov)", 1.0) is True
        ds = gdal.Open(tif)
        assert ds.GetMetadataItem("SOURCE") == "ND DWR LiDAR (lidar.dwr.nd.gov)" and ds.GetMetadataItem("CELL_M") == "1"
        ds = None
        print("OK (GeoTIFF tags SOURCE / CRS / UNITS / CELL_M)")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
else:
    print("SKIP (GeoTIFF tags: real GDAL not available)")

# ---------------------------------------------------------------------------
# 3. warp_to_local: synthetic ND-feet grid (EPSG:3721) → EPSG:5070 float32

if HAS_GDAL:
    tmp = tempfile.mkdtemp()
    try:
        # 5×5 GeoTIFF in EPSG:3721 (ND SP North, US survey feet), values ~1640 ft ≈ 500 m
        src_path = os.path.join(tmp, "nd_feet.tif")
        driver = gdal.GetDriverByName("GTiff")
        ds = driver.Create(src_path, 5, 5, 1, gdal.GDT_Float32)
        srs = osr.SpatialReference()
        srs.ImportFromEPSG(3721)
        ds.SetProjection(srs.ExportToWkt())
        # ~1670 ft cell (≈510 m) so we have meaningful area to resample to 10 m
        ds.SetGeoTransform([2100000.0, 1670.0, 0, 370000.0, 0, -1670.0])
        import ctypes
        band = ds.GetRasterBand(1)
        band.Fill(1640.0)
        band.SetNoDataValue(-9999.0)
        ds.FlushCache()
        ds = None

        dest_path = os.path.join(tmp, "result_10m_5070.tif")
        warp_to_local(src_path, dest_path, 10.0)
        assert os.path.isfile(dest_path), "output GeoTIFF not created"

        ds2 = gdal.Open(dest_path)
        assert ds2 is not None
        srs2 = osr.SpatialReference(wkt=ds2.GetProjection())
        epsg = srs2.GetAuthorityCode(None)
        assert epsg == "5070", "CRS not EPSG:5070, got {}".format(epsg)
        assert ds2.GetRasterBand(1).DataType == gdal.GDT_Float32, "not float32"
        gt = ds2.GetGeoTransform()
        assert abs(abs(gt[1]) - 10.0) < 1.0 and abs(abs(gt[5]) - 10.0) < 1.0, \
            "cell size not ~10 m: {}".format(gt)
        # 1640 ft ÷ 3.28084 ≈ 499.9 m
        arr = ds2.GetRasterBand(1).ReadAsArray()
        centre = float(arr[arr.shape[0] // 2, arr.shape[1] // 2])
        assert 490 < centre < 510, "elevation not ~500 m: {}".format(centre)
        ds2 = None
        print("OK (warp_to_local: synthetic ND-feet EPSG:3721 → EPSG:5070 10 m float32)")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
else:
    print("SKIP (warp_to_local: real GDAL not available)")

print("all dem_local_024 checks passed")
