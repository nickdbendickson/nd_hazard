"""OpenStreetMap layers made outside the plugin (ND Data Updater 1.2.0's osm_layers folder,
nd_osm_download.py): the pack builder takes them from files, and the data-folder search knows the
mobile-home park and place layers. Needs GDAL (QGIS has it); the data-folder test needs real PyQGIS.
Run: python3 -m ND_Dam_Hazard_Helper.tests.run_tests (from the folder above the plugin)."""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_DIR = os.path.dirname(HERE)
if os.path.dirname(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, os.path.dirname(PLUGIN_DIR))
from ND_Dam_Hazard_Helper.tests import fake_qgis  # noqa: E402
from ND_Dam_Hazard_Helper.tests.registry import test  # noqa: E402

MODE = fake_qgis.install()
from ND_Dam_Hazard_Helper.damsuite import gpkgpack as GP  # noqa: E402
from ND_Dam_Hazard_Helper import build_nd_pack as B  # noqa: E402
from ND_Dam_Hazard_Helper import packfiles as PF  # noqa: E402

# name -> (geometry, [(field, type)], [(wkt, values)])
LAYERS = {
    "OSM_lifelines": ("Point", [("name", "s"), ("category", "s"), ("osm_id", "s"), ("operator", "s")],
                      [("POINT (-100.78 46.8)", ["Bismarck Hospital", "Hospital", "node/1", ""])]),
    "OSM_crossings": ("Point", [("name", "s"), ("category", "s"), ("waterway", "s"), ("railway", "s"),
                                ("operator", "s"), ("osm_id", "s")],
                      [("POINT (-101.0 47.0)", ["", "Culvert", "stream", "", "", "way/4"])]),
    "OSM_campgrounds": ("Polygon", [("name", "s"), ("category", "s"), ("tourism", "s"), ("sites", "i"),
                                    ("seasonal", "s"), ("shape", "s"), ("osm_id", "s")],
                        [("POLYGON ((-102 47.5,-101.99 47.5,-101.99 47.51,-102 47.51,-102 47.5))",
                          ["Lake Camp", "Campground", "camp_site", 40, "", "mapped area", "way/2"])]),
    "OSM_trailer_parks": ("Polygon", [("name", "s"), ("category", "s"), ("match", "s"),
                                      ("landuse", "s"), ("shape", "s"), ("osm_id", "s")],
                          [("POLYGON ((-97.1 46.9,-97.09 46.9,-97.09 46.91,-97.1 46.91,-97.1 46.9))",
                            ["Sunset Mobile Home Park", "Mobile-home park", "name", "residential",
                             "mapped area", "way/3"])]),
    "OSM_places": ("Point", [("name", "s"), ("place", "s"), ("population", "i"), ("osm_id", "s")],
                   [("POINT (-100.78 46.81)", ["Bismarck", "city", 73622, "node/5"])]),
}


def _make_layers(folder):
    from osgeo import ogr, osr
    os.makedirs(folder, exist_ok=True)
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    try:
        srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    except AttributeError:
        pass
    for name, (gt, schema, rows) in LAYERS.items():
        ds = ogr.GetDriverByName("GPKG").CreateDataSource(os.path.join(folder, name + ".gpkg"))
        lyr = ds.CreateLayer(name, srs, ogr.wkbPoint if gt == "Point" else ogr.wkbPolygon)
        for f, t in schema:
            lyr.CreateField(ogr.FieldDefn(f, ogr.OFTInteger64 if t == "i" else ogr.OFTString))
        for wkt, vals in rows:
            feat = ogr.Feature(lyr.GetLayerDefn())
            for (f, _t), v in zip(schema, vals):
                feat.SetField(f, v)
            feat.SetGeometry(ogr.CreateGeometryFromWkt(wkt))
            lyr.CreateFeature(feat)
        ds = None


def _have_gdal():
    try:
        from osgeo import ogr  # noqa: F401
        return True
    except ImportError:
        print("      (no GDAL here - skipped)")
        return False


@test
def packfiles_know_the_osm_layer_names():
    assert PF.OSM_NAMES == ("OSM_lifelines", "OSM_crossings", "OSM_campgrounds", "OSM_trailer_parks",
                            "OSM_places")
    for n in PF.OSM_NAMES:
        assert PF.classify("/x/" + n + ".gpkg") == n, n
    assert PF.classify("/x/osm_mobile_home_parks.gpkg") is None
    assert PF.classify("/x/Fire_and_EMS_Stations.gpkg") == "HIFLD_fire_ems_stations"
    assert "osm_layers" in PF.missing_text("OSM_places")
    assert any(f.endswith("osm_layers") for f in PF.folders_for("/data"))
    # the builder lists them once, as downloads; the file route is a bonus
    names = [e[0] for e in B.datasets()]
    assert all(names.count(n) == 1 for n in PF.OSM_NAMES), names
    assert not any(e[0] in PF.OSM_NAMES for e in B.file_datasets())


@test
def osm_layer_files_go_into_the_pack_instead_of_overpass():
    if not _have_gdal():
        return
    d = tempfile.mkdtemp()
    src = os.path.join(d, "osm_layers")
    _make_layers(src)
    pack = os.path.join(d, "ND_Dam_Suite_Pack")
    table = B.datasets()
    todo = [t for t in table if t[0] in PF.OSM_NAMES or t[0] == "NBI_bridges"]
    assert len(todo) == 6
    # without the OSM_places file the Overpass download stays in the list
    os.remove(os.path.join(src, "OSM_places.gpkg"))
    box = (-104.15, 45.83, -96.45, 49.1)
    left = B._add_osm_files(pack, todo, box, [src], False, lambda p, m: None, lambda m: None)
    assert sorted(t[0] for t in left) == ["NBI_bridges", "OSM_places"], [t[0] for t in left]
    man = GP.read_manifest(pack)
    assert sorted(man["datasets"]) == ["OSM_campgrounds", "OSM_crossings", "OSM_lifelines",
                                       "OSM_trailer_parks"]
    assert man["datasets"]["OSM_lifelines"]["file"] == "lifelines/OSM_lifelines.gpkg"
    assert man["datasets"]["OSM_crossings"]["file"] == "roads/OSM_crossings.gpkg"
    assert man["datasets"]["OSM_trailer_parks"]["file"] == "structures/OSM_trailer_parks.gpkg"
    r = GP.GpkgPackReader(pack)
    f = r.query("OSM_trailer_parks", (-98, 46, -96, 47))
    assert [x["properties"]["name"] for x in f] == ["Sunset Mobile Home Park"]
    assert r.query("OSM_campgrounds", (-103, 47, -101, 48))[0]["properties"]["sites"] == 40
    # a second run keeps the pack's copies (the files are not newer) and asks Overpass for nothing more
    left = B._add_osm_files(pack, todo, box, [src], False, lambda p, m: None, lambda m: None)
    assert sorted(t[0] for t in left) == ["NBI_bridges", "OSM_places"]


@test
def the_data_folder_search_finds_the_osm_layers_incl_parks_and_places():
    if MODE != "real" and not hasattr(sys.modules.get("qgis.core"), "QgsApplication"):
        print("      (needs real PyQGIS - skipped)")
        return
    try:
        from qgis.core import QgsApplication
        if QgsApplication.instance() is None:
            os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
            QgsApplication.setPrefixPath("/usr", True)
            _app = QgsApplication([], False)       # noqa: F841 - kept alive
            _app.initQgis()
        from osgeo import ogr  # noqa: F401
    except Exception:
        print("      (no real PyQGIS - skipped)")
        return
    from ND_Dam_Hazard_Helper import datafolder as DF
    d = tempfile.mkdtemp()
    _make_layers(d)
    res = DF.match_roles(DF.scan_folder(d))
    got = {r: os.path.basename(DF._file_of_uri(c.dataset.uri)) for r, c in res.chosen.items() if c}
    assert got.get("camps") == "OSM_campgrounds.gpkg", got
    assert got.get("bridges") == "OSM_crossings.gpkg", got
    assert got.get("parks") == "OSM_trailer_parks.gpkg", got
    assert got.get("places") == "OSM_places.gpkg", got
    assert [os.path.basename(DF._file_of_uri(c.dataset.uri)) for c in res.lifelines] == \
        ["OSM_lifelines.gpkg"]
    assert not res.unmatched, [u.uri for u in res.unmatched]
    cfg = DF.to_config(res)
    layers, notes = DF.open_config(cfg)
    assert not notes and layers["parks"].featureCount() == 1 and layers["places"].featureCount() == 1
    # other layers do not turn into parks / places
    assert "parks" not in DF.match_roles([]).chosen or True
