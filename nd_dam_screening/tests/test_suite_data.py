"""damsuite 1.5.0: the suite data folder, the server catalog, the terrain tile cache and "Get data
for this dam" (pure Python: no QGIS, no GDAL, no network - servers are fakes).

The same file is in ND Dam Breach Runner's and ND Dam Breach Screening's tests (it finds the
plugin it sits in). Run: python3 tests/test_suite_data.py   (the Runner's run_tests.py also picks
it up through tests.registry).
"""
import importlib
import json
import os
import shutil
import struct
import sys
import tempfile
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_DIR = os.path.dirname(HERE)
PLUGIN = os.path.basename(PLUGIN_DIR)
if os.path.dirname(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, os.path.dirname(PLUGIN_DIR))
DS = importlib.import_module(PLUGIN + ".damsuite")
SV, SD, TC, DD, DR, OP, G = (DS.services, DS.suitedata, DS.terrain_cache, DS.damdata,
                             DS.dam_record, DS.offline_pack, DS.suitegpkg)
CHECKS = []


def check(fn):
    CHECKS.append(fn)
    return fn


class Env(object):
    """A temporary data folder and suite config (no QGIS settings involved)."""

    def __enter__(self):
        self.tmp = tempfile.mkdtemp(prefix="suitedata_")
        self.old = (os.environ.get(SD.ENV_DATA), os.environ.get(SD.ENV_DAMS), SD.CONFIG)
        SD.CONFIG = os.path.join(self.tmp, "cfg", "config.json")
        self.data = os.path.join(self.tmp, "data")
        os.environ[SD.ENV_DATA] = self.data
        os.environ.pop(SD.ENV_DAMS, None)
        return self

    def __exit__(self, *a):
        for k, v in ((SD.ENV_DATA, self.old[0]), (SD.ENV_DAMS, self.old[1])):
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        SD.CONFIG = self.old[2]
        shutil.rmtree(self.tmp, ignore_errors=True)


# ------------------------------------------------------------------- services --
@check
def builtin_services_are_well_formed():
    ids = [s["id"] for s in SV.BUILTIN]
    assert len(ids) == len(set(ids)), "duplicate built-in ids"
    for s in SV.BUILTIN:
        assert s["kind"] in SV.KINDS, s
        assert s["role"] in SV.ROLES, s
        assert s["url"].startswith("https://"), s
        assert s["confidence"] in ("documented", "inferred"), s
        assert s["source"], s
        if s["kind"] in ("wms", "wcs") and not s["layer"]:
            assert "Browse layers" in s["notes"], "a WMS / WCS entry names its layer: " + s["id"]
    cat = SV.Catalog()
    for role, (_label, rtype, _used) in SV.ROLES.items():
        if role in ("dwr_dam_lines", "reference"):
            continue                    # DWR publishes no dam-lines service; reference = display
        c = cat.chosen(role)
        assert c is not None, "no default service for " + role
        if rtype != "display":
            assert SV.data_capable(c, role), (role, c["id"])
    assert cat.chosen("dem")["id"] == "usgs_3dep_image"
    assert cat.chosen("flowlines")["url"].endswith("/3DHP_all/MapServer/50")      # 1.15.0: 3DHP (was NHD)
    assert cat.chosen("waterbodies")["url"].endswith("/3DHP_all/MapServer/60")


@check
def kinds_are_recognised_from_addresses():
    k = SV.detect_kind
    assert k("https://x.gov/arcgis/rest/services/A/B/MapServer/6") == "arcgis_feature"
    assert k("https://x.gov/arcgis/rest/services/A/MapServer") == "arcgis_map"
    assert k("https://x.gov/arcgis/rest/services/A/FeatureServer") == "arcgis_feature"
    assert k("https://x.gov/arcgis/rest/services/A/ImageServer?f=json") == "arcgis_image"
    assert k("https://x.gov/arcgis/services/A/MapServer/WMSServer") == "wms"
    assert k("https://x.gov/geoserver/ows?service=WCS&request=GetCapabilities") == "wcs"
    assert k("https://x.gov/wmts/1.0.0/WMTSCapabilities.xml") == "wmts"
    assert k("https://x.gov/tile/{z}/{y}/{x}") == "xyz"
    assert k("") == "" and k("ftp://x") == ""
    assert SV.split_arcgis("https://x/rest/services/A/MapServer/12/") == (
        "https://x/rest/services/A/MapServer", 12)
    s = {"kind": "wms", "url": "https://x/wms?map=a&SERVICE=WMS"}
    cap = SV.capabilities_url(s)
    assert "request=GetCapabilities" in cap and "map=a" in cap and cap.lower().count("service=") == 1
    assert SV.capabilities_url({"kind": "arcgis_feature",
                                "url": "https://x/rest/services/A/MapServer/3"}) == \
        "https://x/rest/services/A/MapServer/3?f=json"


WMS_XML = b"""<?xml version="1.0"?>
<WMS_Capabilities version="1.3.0" xmlns="http://www.opengis.net/wms">
 <Service><Name>WMS</Name></Service>
 <Capability><Layer><Title>Root</Title><CRS>EPSG:3857</CRS>
   <Layer><Name>0</Name><Title>Imagery</Title><CRS>EPSG:4326</CRS></Layer>
   <Layer><Title>Group</Title><Layer><Name>dams</Name><Title>Dams</Title></Layer></Layer>
 </Layer></Capability></WMS_Capabilities>"""
WCS_XML = b"""<wcs:Capabilities xmlns:wcs="http://www.opengis.net/wcs/2.0">
 <wcs:Contents><wcs:CoverageSummary><wcs:CoverageId>nlcd_2021</wcs:CoverageId>
 </wcs:CoverageSummary></wcs:Contents></wcs:Capabilities>"""


@check
def server_answers_are_read():
    lay = SV.parse_wms_capabilities(WMS_XML)
    assert [l["name"] for l in lay] == ["0", "dams"]
    assert "EPSG:3857" in lay[1]["crs"]                          # inherited from the parent
    assert SV.parse_wcs_capabilities(WCS_XML) == [{"name": "nlcd_2021", "title": "nlcd_2021"}]
    svc = json.dumps({"mapName": "nhd", "layers": [{"id": 6, "name": "Flowline",
                                                     "geometryType": "esriGeometryPolyline"},
                                                    {"id": 12, "name": "Waterbody"}]})
    info = SV.parse_arcgis_service(svc)
    assert [l["id"] for l in info["layers"]] == [6, 12]
    lyr = json.dumps({"name": "Flowline", "type": "Feature Layer", "fields": [
        {"name": "gnis_name"}, {"name": "FType"}, {"name": "fcode"}]})
    assert SV.parse_arcgis_service(lyr)["fields"] == ["gnis_name", "FType", "fcode"]
    try:
        SV.parse_arcgis_service('{"error": {"code": 400, "message": "Invalid URL"}}')
        assert False
    except SV.ServiceError as e:
        assert "Invalid URL" in str(e)
    try:
        SV.parse_wms_capabilities(b"<ServiceExceptionReport><ServiceException>bad"
                                  b"</ServiceException></ServiceExceptionReport>")
        assert False
    except SV.ServiceError as e:
        assert "bad" in str(e)


@check
def a_service_is_tested_against_its_listing():
    answers = {}
    get = lambda url: answers[url.split("?")[0]]                 # noqa
    s = {"kind": "wms", "url": "https://x/wms", "layer": "dams", "name": "x"}
    answers["https://x/wms"] = WMS_XML
    ok, msg, layers = SV.test_service(s, get)
    assert ok and len(layers) == 2, msg
    ok, msg, _l = SV.test_service(dict(s, layer="roads"), get)
    assert not ok and "roads" in msg
    f = {"kind": "arcgis_feature", "url": "https://x/rest/services/A/MapServer/12", "name": "w",
         "fields": ["gnis_name", "areasqkm"]}
    answers["https://x/rest/services/A/MapServer/12"] = json.dumps(
        {"name": "Waterbody", "fields": [{"name": "GNIS_NAME"}, {"name": "AreaSqKm"}]}).encode()
    ok, msg, _l = SV.test_service(f, get)
    assert ok, msg                                               # field names case-insensitive
    ok, msg, _l = SV.test_service(dict(f, fields=["ftype"]), get)
    assert not ok and "ftype" in msg
    ok, msg, _l = SV.test_service(s, lambda u: (_ for _ in ()).throw(IOError("refused")))
    assert not ok and "refused" in msg


@check
def features_are_paged_until_the_last_page():
    pages = [[{"type": "Feature", "geometry": {"type": "Point", "coordinates": [i, 0]},
               "properties": {"i": i}} for i in range(k, k + n)] for k, n in ((0, 2), (2, 2), (4, 1))]
    calls = []

    def get(url):
        calls.append(url)
        off = int(url.split("resultOffset=")[1].split("&")[0])
        page = pages[off // 2] if off // 2 < len(pages) else []
        return json.dumps({"type": "FeatureCollection", "features": page,
                           "exceededTransferLimit": off // 2 < 1}).encode()
    s = {"kind": "arcgis_feature", "url": "https://x/rest/services/A/FeatureServer/0", "name": "a"}
    feats = SV.fetch_features(s, (-100, 46, -99, 47), http_get=get, page=2)
    assert [f["properties"]["i"] for f in feats] == [0, 1, 2, 3, 4]
    assert len(calls) == 3 and "geometryType=esriGeometryEnvelope" in calls[0]
    try:
        SV.fetch_features(s, (0, 0, 1, 1), http_get=lambda u: b'{"error":{"code":498}}')
        assert False
    except SV.ServiceError:
        pass
    try:                                                         # never half a layer
        SV.fetch_features(s, (0, 0, 1, 1), page=2, max_pages=2, http_get=lambda u: json.dumps(
            {"features": pages[0], "exceededTransferLimit": True}).encode())
        assert False
    except SV.ServiceError as e:
        assert "more than" in str(e)


@check
def the_users_servers_and_choices_are_kept():
    with Env() as env:
        path = os.path.join(env.data, SV.SERVICES_FILE)
        cat = SV.load()
        assert cat.path == path and not cat.user
        s = cat.add("https://mapservice.example.gov/arcgis/rest/services/Dams/MapServer/3",
                    "dwr_dams", name="DWR dams")
        assert s["kind"] == "arcgis_feature" and s["id"] == "user_1"
        cat.choose("dwr_dams", s["id"])
        w = cat.add("https://x.gov/wms", "reference", kind="wms", layer="0")
        cat.remove("usgs_topo_wms")                              # a built-in is hidden, not lost
        cat.save()
        cat2 = SV.load()
        assert [u["id"] for u in cat2.user] == ["user_1", w["id"]]
        assert cat2.chosen("dwr_dams")["name"] == "DWR dams"
        assert cat2.get("usgs_topo_wms") is not None and \
            "usgs_topo_wms" not in [x["id"] for x in cat2.all()]
        assert cat2.chosen("topo")["id"] == "usgs_topo_xyz"
        try:
            cat2.add("not a url", "roads")
            assert False
        except SV.ServiceError:
            pass
        try:
            cat2.add("https://x.gov/data", "roads")              # kind unknown: must be given
            assert False
        except SV.ServiceError:
            pass
        cat2.remove("user_1")
        assert cat2.chosen("dwr_dams")["id"] == "ndhub_hsip_dams"   # back to the built-in
        # a broken file is reported, never overwritten by loading
        with open(path, "w") as f:
            f.write("{broken")
        bad = SV.load()
        assert bad.problem and not bad.user
        assert open(path).read() == "{broken"


@check
def hazard_gets_only_the_addresses_the_user_changed():
    cat = SV.Catalog()
    assert SV.hazard_overrides(cat) == {}
    cat.choose("structures", "fema_usa_structures")              # the default: nothing changes
    assert SV.hazard_overrides(cat) == {}
    cat.choose("flowlines", "usgs_nhdplushr_flowline")
    cat.choose("imagery", "usgs_imagery_wms")                    # WMS: Hazard needs tiles
    o = SV.hazard_overrides(cat)
    assert o == {"NHD_FLOWLINE_URL": "https://hydro.nationalmap.gov/arcgis/rest/services/"
                                     "NHDPlus_HR/MapServer/3"}, o
    m = cat.add("https://x.gov/arcgis/rest/services/R/MapServer", "roads", layer="4")
    cat.choose("roads", m["id"])
    assert SV.hazard_overrides(cat)["ND_ROADS_URL"] == "https://x.gov/arcgis/rest/services/R/" \
                                                       "MapServer/4"


@check
def qgis_sources_for_each_kind():
    for s in SV.BUILTIN:
        if s["kind"] in ("wms", "wcs") and not s["layer"]:
            continue                                         # layer picked with Browse layers
        prov, uri, name = SV.qgis_layer_source(s)
        assert prov in ("wms", "wcs", "arcgisfeatureserver", "arcgismapserver"), (s["id"], prov)
        assert uri and name
    p, uri, _n = SV.qgis_layer_source({"kind": "xyz", "url": "https://t/{z}/{y}/{x}", "name": "t"})
    assert p == "wms" and uri.startswith("type=xyz&") and "{z}" in uri
    try:
        SV.qgis_layer_source({"kind": "wms", "url": "https://x/wms", "name": "x"})
        assert False
    except SV.ServiceError:
        pass


# ------------------------------------------------------------------ data folder --
def _make_pack(folder, sets):
    """An unpacked offline pack: sets = {name: [features]}."""
    os.makedirs(folder, exist_ok=True)
    db = os.path.join(folder, OP.DB_NAME)
    w = OP.PackWriter(db, OP.ND_BBOX, created_by="test")
    for name, feats in sets.items():
        w.begin(name, name, "https://example/" + name)
        w.add(feats, lambda f: json.dumps(f, sort_keys=True))
        w.finish()
    man = w.manifest()
    w.close(finalize=True)
    with open(os.path.join(folder, OP.MANIFEST_NAME), "w") as f:
        json.dump(man, f)
    return folder


def _pt(lon, lat, **p):
    return {"type": "Feature", "geometry": {"type": "Point", "coordinates": [lon, lat]},
            "properties": p}


def _poly(lon, lat, d=0.001, **p):
    ring = [[lon, lat], [lon + d, lat], [lon + d, lat + d], [lon, lat + d], [lon, lat]]
    return {"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [ring]},
            "properties": p}


@check
def the_data_folder_is_set_once_and_described():
    with Env() as env:
        assert SD.data_folder() == env.data
        os.environ.pop(SD.ENV_DATA)
        assert SD.data_folder() == ""
        SD.set_data_folder(env.data)                             # outside QGIS: config file
        assert SD.data_folder() == env.data and os.path.isdir(os.path.join(env.data, "terrain"))
        SD.set_dams_folder("/dams")
        assert SD.dams_folder() == "/dams"
        st = SD.status()
        assert st["exists"] and not st["pack"] and st["problems"]
        _make_pack(os.path.join(env.data, "nd_offline_pack"), {"NID_dams": [_pt(-100.5, 47.0)]})
        open(os.path.join(env.data, "dwr", "DWR_Dams.shp"), "w").close()
        open(os.path.join(env.data, "dwr", "dam_lines.gpkg"), "w").close()
        open(os.path.join(env.data, "dem", "ND_1m_26914.vrt"), "w").close()
        st = SD.status()
        assert st["pack"].endswith("nd_offline_pack") and st["pack_sets"][0][0] == "NID_dams"
        assert st["pack_stale"] == []
        assert st["dwr"]["dwr_dams"].endswith("DWR_Dams.shp")
        assert st["dwr"]["dwr_dam_lines"].endswith("dam_lines.gpkg")
        assert st["dem"].endswith("ND_1m_26914.vrt")
        text = "\n".join(SD.describe(st))
        assert "all current" in text and "DWR dam lines" in text
        assert SD.stale([("a", "2020-01-01T00:00:00", 3, "complete"), ("b", "", 1, "complete"),
                         ("c", "2099-01-01", 1, "partial")]) == ["a", "b", "c"]


# ------------------------------------------------------------------ terrain --
def _fake_tiff(path, *_a):
    with open(path, "wb") as f:
        f.write(b"II*\x00" + struct.pack("<I", 8) + b"\x00" * 16)


@check
def terrain_tiles_are_downloaded_once_and_mosaicked():
    tmp = tempfile.mkdtemp(prefix="terrain_")
    try:
        cell = 10.0
        step = TC.TILE_PX * cell
        box = (step * 3 + 50, step * 7 + 5, step * 4 + 15, step * 7 + 900)   # 2 x 1 tiles
        assert len(TC.tiles_for(box, cell)) == 2
        assert TC.snap((1.5, 2.5, 19.9, 20.0), 10) == (0.0, 0.0, 20.0, 20.0)
        got = []

        def fetch(tb, c, size, out):
            got.append(tb)
            assert size == (TC.TILE_PX, TC.TILE_PX) and c == cell
            _fake_tiff(out)
        cdir = TC.cache_dir(tmp, "usgs_3dep_image", cell)
        assert cdir.endswith("usgs_3dep_image_10m")
        vrt = os.path.join(tmp, "a.vrt")
        r = TC.get(box, cell, cdir, fetch, vrt, normalize_tiles=False)
        assert r["fetched"] == 2 and r["cached"] == 0 and len(got) == 2
        root = ET.parse(vrt).getroot()
        sb = TC.snap(box, cell)
        assert int(root.get("rasterXSize")) == int(round((sb[2] - sb[0]) / cell))
        srcs = root.findall(".//ComplexSource")
        assert len(srcs) == 2
        widths = sum(int(s.find("DstRect").get("xSize")) for s in srcs)
        assert widths == int(root.get("rasterXSize"))
        # the second request downloads nothing
        r2 = TC.get(box, cell, cdir, fetch, os.path.join(tmp, "b.vrt"), normalize_tiles=False)
        assert r2["fetched"] == 0 and r2["cached"] == 2 and len(got) == 2
        # a failed tile stops the request and leaves no partial file
        big = (box[0], box[1], box[2] + step, box[3])

        def bad(tb, c, size, out):
            if tb[0] >= step * 5:
                with open(out, "wb") as f:
                    f.write(b"<html>busy</html>")
                return
            _fake_tiff(out)
        try:
            TC.get(big, cell, cdir, bad, os.path.join(tmp, "c.vrt"), normalize_tiles=False)
            assert False
        except TC.TerrainError as e:
            assert "1 of 3" in str(e)
        assert not [f for f in os.listdir(cdir) if f.endswith(".part")]
        assert not os.path.exists(os.path.join(tmp, "c.vrt"))
        assert TC.summary(tmp)[0][1] == 2
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------------------ dam data --
@check
def get_data_for_a_dam_fills_its_folder_and_dam_json():
    with Env() as env:
        SD.ensure_layout(env.data)
        lon, lat = -100.70, 47.10
        _make_pack(os.path.join(env.data, "nd_offline_pack"), {
            "NID_dams": [_pt(lon, lat, NIDID="ND00123", NAME="Test Dam", NID_HEIGHT=31.0,
                             NID_STORAGE=900.0, NORMAL_STORAGE=400.0, HAZARD_POTENTIAL="High",
                             COUNTY="Burleigh"),
                         _pt(lon + 0.05, lat, NIDID="ND00999", NAME="Other")],
            "NHD_waterbodies": [_poly(lon - 0.01, lat, gnis_name="Lake", ftype=390),
                                _poly(lon + 3.0, lat)],          # far away: not in the area
            "NARN_rail_lines": [{"type": "Feature", "geometry": {"type": "LineString",
                                 "coordinates": [[lon - 0.1, lat], [lon + 0.1, lat + 0.01]]},
                                 "properties": {"RROWNER1": "BNSF"}}],
        })
        # the DWR export in the drop-in folder, as a GeoPackage in EPSG:5070
        w = G.GpkgWriter(os.path.join(env.data, "dwr", "dwr_dams.gpkg"))
        lyr = w.layer("dams", "POINT", [("structure_id", "text")])
        x, y = G.to_5070(lon, lat)
        lyr.add({"type": "Point", "coordinates": [x, y]}, {"structure_id": "5289"})
        lyr.add({"type": "Point", "coordinates": [x + 500000, y]}, {"structure_id": "far"})
        w.close()
        dams = os.path.join(env.tmp, "Dams")
        folder = os.path.join(dams, "ND00123")
        rec = DR.DamRecord(folder)
        rec.set("dam_id", "ND00123", "test", "1")
        rec.set("nid_id", "ND00123", "test", "1")
        rec.set("height_ft", 35.0, "engineer", "1")              # the engineer's own value
        rec.save()
        calls = []

        def no_network(url):
            calls.append(url)
            raise IOError("no network in the test")
        res = DD.get_data_for_dam(folder, http_get=no_network,
                                  roles=["nid_dams", "waterbodies", "rail", "dwr_dams",
                                         "dwr_dam_lines", "roads"], version="1.5.0")
        assert "NID_dams record" in res["location"], res["location"]
        assert set(res["done"]) == {"nid_dams", "waterbodies", "rail", "dwr_dams"}, res
        assert "height_ft" in res["refused"]                     # 35 ft stays, 31 is a conflict
        assert "roads" in res["failed"] and "dwr_dam_lines" in res["skipped"], res
        rec = DR.load(folder)
        assert rec.get("height_ft") == 35.0 and rec.get("storage_acft") == 900.0   # max storage
        assert rec.get("name") == "Test Dam" and rec.get("existing_hazard_class") == "High"
        assert abs(rec.get("lon") - lon) < 1e-9 and rec.get("county") == "Burleigh"
        sec = rec.extra["data"]
        assert sec["waterbodies"]["path"] == "data/waterbodies.gpkg|layername=waterbodies"
        assert sec["waterbodies"]["count"] == 1 and "offline pack" in sec["waterbodies"]["source"]
        assert sec["dwr_dams"]["count"] == 1 and "your file" in sec["dwr_dams"]["source"]
        feats = G.read_features(os.path.join(folder, "data", "waterbodies.gpkg"), "waterbodies")
        assert len(feats) == 1 and feats[0][1]["gnis_name"] == "Lake"
        assert feats[0][0]["type"] == "MultiPolygon"
        assert abs(feats[0][0]["coordinates"][0][0][0][0] - G.to_5070(lon - 0.01, lat)[0]) < 1e-6
        # a second run keeps what is there
        res2 = DD.get_data_for_dam(folder, http_get=no_network, roles=["waterbodies"])
        assert res2["skipped"] == {"waterbodies": "already there (overwrite to replace)"}
        # autofill: empty inputs only
        vals, notes, used = DD.autofill(
            {"DEM": "", "RECEPTOR_RAIL": None, "PONDS": "/mine.gpkg", "NORMAL_POOL_FT": None},
            {"DEM": "dem", "RECEPTOR_RAIL": "rail", "PONDS": "waterbodies"}, folder,
            fields={"NORMAL_POOL_FT": "normal_pool_ft", "HEIGHT_FT": "height_ft"})
        assert vals["RECEPTOR_RAIL"].endswith(os.path.join("data", "rail.gpkg") + "|layername=rail")
        assert vals["PONDS"] == "/mine.gpkg" and vals["DEM"] == "" and vals["NORMAL_POOL_FT"] is None
        assert used == {"RECEPTOR_RAIL": "rail"} and len(notes) == 1
        # the data section survives an older plugin's load / save
        r = DR.load(folder)
        r.set("top_of_dam_ft", 1880.0, "ndbreach", "3.8.0")
        r.save()
        assert DR.load(folder).extra["data"]["rail"]["count"] == 1
        assert DD.dam_folders(dams) == [folder]


@check
def a_dam_without_a_location_is_explained():
    with Env():
        tmp = tempfile.mkdtemp(prefix="noloc_")
        try:
            rec = DR.DamRecord(tmp)
            rec.set("dam_id", "X1", "test", "1")
            rec.save()
            try:
                DD.get_data_for_dam(tmp, data_folder="", http_get=lambda u: b"")
                assert False
            except DD.DataError as e:
                assert "lon / lat" in str(e)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


@check
def dem_comes_from_the_terrain_cache():
    with Env() as env:
        SD.ensure_layout(env.data)
        folder = os.path.join(env.tmp, "Dams", "D1")
        rec = DR.DamRecord(folder)
        rec.set_many({"dam_id": "D1", "lon": -100.7, "lat": 47.1}, "test", "1")
        rec.save()
        n = []

        def get(url):
            assert "/exportImage?" in url and "bboxSR=5070" in url
            n.append(url)
            return b"II*\x00" + b"\x00" * 20
        old = TC.normalize
        TC.normalize = lambda p, nodata=TC.NODATA: False        # no GDAL in the test
        try:
            res = DD.get_data_for_dam(folder, http_get=get, roles=["dem"], dem_half_mi=2.0,
                                      dem_cell_m=10.0)
        finally:
            TC.normalize = old
        assert "dem" in res["done"], res
        e = DR.load(folder).extra["data"]["dem"]
        assert e["path"] == "data/dem.vrt" and "terrain cache" in e["source"]
        tiles = os.listdir(os.path.join(env.data, "terrain", "usgs_3dep_image_10m"))
        assert len(tiles) == len(n) >= 1


def run_all():
    failed = 0
    for fn in CHECKS:
        try:
            fn()
            print("ok    ", fn.__name__)
        except Exception:                                        # noqa
            import traceback
            failed += 1
            print("FAIL  ", fn.__name__)
            traceback.print_exc()
    print("{} checks, {} failed".format(len(CHECKS), failed))
    return failed


if PLUGIN == "nd_breach_runner" and __name__ != "__main__":
    from nd_breach_runner.tests.registry import test as _register
    for _fn in CHECKS:
        _register(_fn)

if __name__ == "__main__":
    sys.exit(1 if run_all() else 0)
