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

from nd_dam_screening.core.dem_download import (  # noqa: E402
    local_tif_stem, write_sidecar, warp_to_local, tag_local_tif, build_vrts)

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
# 3. the foot-grid pipeline end to end: build_vrts (feet→metres) THEN warp_to_local
#    (reproject + standardise). warp_to_local only reprojects horizontally and never
#    converts vertical units, so the metres conversion must come from build_vrts first;
#    feeding a raw foot grid straight to warp_to_local would mislabel feet as metres.

if HAS_GDAL:
    tmp = tempfile.mkdtemp()
    try:
        # 5×5 foot grid in EPSG:3721 (ND SP North, US survey feet), 1640 ft ≈ 500 m. It must
        # live under an ASCII_Grids_ft/ folder so build_vrts recognises it as feet and scales.
        src_dir = os.path.join(tmp, "ASCII_Grids_ft")
        os.makedirs(src_dir)
        src_path = os.path.join(src_dir, "nd_feet.tif")
        driver = gdal.GetDriverByName("GTiff")
        ds = driver.Create(src_path, 5, 5, 1, gdal.GDT_Float32)
        srs = osr.SpatialReference()
        srs.ImportFromEPSG(3721)
        ds.SetProjection(srs.ExportToWkt())
        # ~1670 ft cell (≈510 m) so we have meaningful area to resample to 10 m
        ds.SetGeoTransform([2100000.0, 1670.0, 0, 370000.0, 0, -1670.0])
        band = ds.GetRasterBand(1)
        band.Fill(1640.0)
        band.SetNoDataValue(-9999.0)
        ds.FlushCache()
        ds = None

        # build_vrts does the feet→metres conversion (1640 ft × 0.3048 ≈ 499.9 m) and returns
        # the metres VRT; warp_to_local then standardises it to EPSG:5070 10 m float32.
        m_vrt = build_vrts([src_path], tmp)
        assert m_vrt and os.path.isfile(m_vrt), "build_vrts returned no metres VRT"
        dest_path = os.path.join(tmp, "result_10m_5070.tif")
        warp_to_local(m_vrt, dest_path, 10.0)
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
        # 1640 ft × 0.3048 ≈ 499.9 m — the conversion is build_vrts', not warp_to_local's
        arr = ds2.GetRasterBand(1).ReadAsArray()
        centre = float(arr[arr.shape[0] // 2, arr.shape[1] // 2])
        assert 490 < centre < 510, "elevation not ~500 m: {}".format(centre)
        ds2 = None

        # and warp_to_local refuses a source still tagged in feet instead of mislabelling it.
        # Only assert when the intermediate VRT really reports feet, so a GDAL build that does
        # not persist a VRT band unit simply skips this check rather than failing it.
        ft_vrt = os.path.join(tmp, "nd_lidar_ascii_ft.vrt")   # build_vrts' pre-scale foot VRT
        fds = gdal.Open(ft_vrt) if os.path.isfile(ft_vrt) else None
        ft_unit = (fds.GetRasterBand(1).GetUnitType() if fds is not None else "") or ""
        fds = None
        if ft_unit.strip().lower() == "ft":
            try:
                warp_to_local(ft_vrt, os.path.join(tmp, "bad.tif"), 10.0)
                raise AssertionError("warp_to_local should refuse a feet-tagged source")
            except ValueError as e:
                assert "metres" in str(e), e

        print("OK (foot-grid pipeline: build_vrts feet→metres → warp_to_local EPSG:5070 10 m)")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
else:
    print("SKIP (foot-grid pipeline: real GDAL not available)")

print("all dem_local_024 checks passed")
