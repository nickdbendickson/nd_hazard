"""3.20.3: the three Runner fixes from the Absaraka comparison (2026-10-13).

  1. a top of dam this plugin estimated from the DEM is marked in dam.json and estimated again
     by the next run, instead of being read back as a given crest (core/damjson)
  2. the crest window is read at the DEM's own pixel when that is coarser than 3 m, and a run
     that estimates a crest off a DEM coarser than 5 m says the estimate is probably low
     (core/workflow; core/gdal_io._pixel_m)
  3. a GeoJSON keeps its CRS when the working CRS is WKT: the EPSG code is looked up, and a
     custom CRS goes beside the file as a .prj (core/products)
"""
import json
import os
import tempfile

from nd_breach_runner.tests.registry import test


@test
def v3203_a_dem_estimate_of_the_top_of_dam_is_not_read_back_as_given():
    from nd_breach_runner.core import damjson
    from nd_breach_runner.damsuite import dam_record as dr
    folder = tempfile.mkdtemp(prefix="v3203_rec_")
    rec = dr.DamRecord(folder)

    # what a run writes when it estimated the crest itself
    vals = damjson.values_used(
        type("D", (), {"dam_id": "DWR-2801", "dam_height_ft": 27.5, "top_of_dam_ft": 1062.0,
                       "storage_acft": None, "storage_top_acft": 1615.5})(), None, "dem")
    assert vals["top_of_dam_datum"] == damjson.DEM_ESTIMATE_DATUM
    note = damjson.DEM_ESTIMATE_NOTE.format("DEM near the dam point (98th percentile, 30 m)")
    rec.set_many({k: v for k, v in vals.items() if k in ("top_of_dam_ft", "top_of_dam_datum")},
                 damjson.TOOL, "3.20.3", note=note)
    rec.save()

    rec2, problem = damjson.load(folder)
    assert not problem and rec2.get("top_of_dam_ft") == 1062.0
    assert damjson.dem_estimate(rec2) and not damjson.local_datum(rec2)
    values, notes, used = damjson.fill({"TOP_FT": None, "HEIGHT_FT": None}, rec2)
    assert values["TOP_FT"] is None and "TOP_FT" not in used
    assert any("earlier run's estimate from the DEM" in n for n in notes), notes

    # a value someone GAVE is read back as before, and carries no estimate datum
    given = dr.DamRecord(tempfile.mkdtemp(prefix="v3203_given_"))
    gv = damjson.values_used(
        type("D", (), {"dam_id": "X", "dam_height_ft": 27.5, "top_of_dam_ft": 1070.0})(),
        None, "parameter")
    assert "top_of_dam_datum" not in gv
    given.set_many({"top_of_dam_ft": 1070.0, "top_of_dam_datum": "NAVD88 ft"}, "ndhazard", "2.30.3")
    assert not damjson.dem_estimate(given)
    values, notes, used = damjson.fill({"TOP_FT": None}, given)
    assert values["TOP_FT"] == 1070.0 and used["TOP_FT"] == "top_of_dam_ft"

    # a crest measured by another tool (the Screener's proposed embankment: datum "DEM") stays
    other = dr.DamRecord(tempfile.mkdtemp(prefix="v3203_other_"))
    other.set_many({"top_of_dam_ft": 1480.0, "top_of_dam_datum": "DEM"}, "ndscreen", "0.28.3",
                   note="proposed embankment")
    assert not damjson.dem_estimate(other)
    assert damjson.fill({"TOP_FT": None}, other)[0]["TOP_FT"] == 1480.0

    # a record written by releases up to 3.20.2, which had no datum of their own
    old = dr.DamRecord(tempfile.mkdtemp(prefix="v3203_old_"))
    old.set_many({"top_of_dam_ft": 1059.0}, damjson.TOOL, "3.20.1",
                 note="highest DEM point within 30 m of the dam point")
    assert damjson.dem_estimate(old)
    assert damjson.fill({"TOP_FT": None}, old)[0]["TOP_FT"] is None
    # ... but not one some other tool wrote with the same wording
    alien = dr.DamRecord(tempfile.mkdtemp(prefix="v3203_alien_"))
    alien.set_many({"top_of_dam_ft": 1059.0}, "ndhazard", "2.30.1",
                   note="highest DEM point within 30 m of the dam point")
    assert not damjson.dem_estimate(alien)


@test
def v3203_the_dem_pixel_size_is_known_and_coarse_dems_are_called_out():
    import numpy as np
    from osgeo import gdal, osr
    from nd_breach_runner.core import gdal_io, workflow
    assert workflow.COARSE_DEM_M == 5.0
    d = tempfile.mkdtemp(prefix="v3203_dem_")

    def write(name, cell, epsg=26914, origin=(600000.0, 5200000.0)):
        path = os.path.join(d, name)
        n = 40
        ds = gdal.GetDriverByName("GTiff").Create(path, n, n, 1, gdal.GDT_Float32)
        ds.SetGeoTransform((origin[0], cell, 0.0, origin[1], 0.0, -cell))
        srs = osr.SpatialReference()
        srs.ImportFromEPSG(epsg)
        ds.SetProjection(srs.ExportToWkt())
        z = 300.0 + 0.01 * np.arange(n)[None, :] + np.zeros((n, 1))
        ds.GetRasterBand(1).WriteArray(z.astype("float32"))
        ds.FlushCache()
        return path

    for cell in (1.0, 10.0, 40.0):
        t = gdal_io.GdalTerrain(write("c{:.0f}.tif".format(cell), cell), "EPSG:26914")
        assert abs(t.src_cell_m - cell) < 1e-6, (cell, t.src_cell_m)
    # degrees are converted at the raster's own latitude (1/3 arc-second 3DEP is about 10 m)
    deg = gdal_io.GdalTerrain(write("deg.tif", 1.0 / 3600.0, 4326, (-97.5, 47.0)), "EPSG:26914")
    assert 15.0 < deg.src_cell_m < 35.0, deg.src_cell_m
    # the conditioned terrain passes it through, so the run sees it whatever the edits
    from nd_breach_runner.core import conditioning as cond
    wrapped = cond.ConditionedTerrain(deg, cond.Edits())
    assert wrapped.src_cell_m == deg.src_cell_m


@test
def v3203_a_geojson_keeps_its_crs_when_the_working_crs_is_wkt():
    from osgeo import osr
    from nd_breach_runner.core import products
    d = tempfile.mkdtemp(prefix="v3203_gj_")
    feat = products.geojson_polygons([([[0, 0], [1, 0], [1, 1], [0, 0]], [])], {"a": 1})

    s = osr.SpatialReference()
    s.ImportFromEPSG(26914)
    wkt = s.ExportToWkt()
    assert products.crs_name("EPSG:26914") == "EPSG:26914"
    assert products.crs_name(wkt) == "EPSG:26914"          # the code is looked up, not dropped
    assert products.crs_name("") == "" and products.crs_name(None) == ""

    p = products.write_geojson(os.path.join(d, "wkt.geojson"), [feat], wkt)
    with open(p) as f:
        fc = json.load(f)
    assert fc["crs"]["properties"]["name"] == "EPSG:26914", fc.get("crs")
    assert not os.path.exists(os.path.join(d, "wkt.prj"))  # labelled in the file: no sidecar

    # a CRS with no EPSG code at all: the .prj beside it is what QGIS and ArcGIS then read
    c = osr.SpatialReference()
    c.SetFromUserInput("+proj=tmerc +lat_0=0 +lon_0=-100.5 +k=1 +x_0=0 +y_0=0 +datum=NAD83 "
                       "+units=m +no_defs")
    cwkt = c.ExportToWkt()
    assert products.crs_name(cwkt) == ""
    p = products.write_geojson(os.path.join(d, "custom.geojson"), [feat], cwkt)
    with open(p) as f:
        assert "crs" not in json.load(f)
    with open(os.path.join(d, "custom.prj")) as f:
        assert f.read().startswith(("PROJCS", "PROJCRS"))

    # no CRS given: unchanged, and no stray sidecar
    products.write_geojson(os.path.join(d, "none.geojson"), [feat], None)
    assert not os.path.exists(os.path.join(d, "none.prj"))
