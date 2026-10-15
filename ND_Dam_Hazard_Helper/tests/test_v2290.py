"""2.29.0 (suite 1.15.0): USGS 3DHP flowlines and waterbodies replace NHD, the aquatic barrier layers,
and the pack builder that makes the whole pack (layer files, land cover).
Run: python3 -m ND_Dam_Hazard_Helper.tests.run_tests (from the folder above the plugin)."""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_DIR = os.path.dirname(HERE)
if os.path.dirname(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, os.path.dirname(PLUGIN_DIR))
from ND_Dam_Hazard_Helper.tests import fake_qgis  # noqa: E402
from ND_Dam_Hazard_Helper.tests.registry import test  # noqa: E402

fake_qgis.install()
from ND_Dam_Hazard_Helper.damsuite import hydro3dhp as H  # noqa: E402
from ND_Dam_Hazard_Helper.damsuite import gpkgpack as GP  # noqa: E402
from ND_Dam_Hazard_Helper.damsuite import offline_pack as OP  # noqa: E402
from ND_Dam_Hazard_Helper.damsuite import services as SV  # noqa: E402
from ND_Dam_Hazard_Helper import build_nd_pack as B  # noqa: E402
from ND_Dam_Hazard_Helper import packfiles as PF  # noqa: E402
from ND_Dam_Hazard_Helper import constants as C  # noqa: E402


def _flow(**kw):
    p = {"id3dhp": "12R9W", "featuredate": 1694649600000, "gnisid": 1028179,
         "gnisidlabel": "Buffalo Creek", "featuretype": 1, "featuretypelabel": "Channel Line",
         "lengthkm": 0.025, "streamorder": 3}
    p.update(kw)
    return {"type": "Feature", "geometry": {"type": "LineString",
                                            "coordinates": [[-97.4, 46.8], [-97.39, 46.81]]},
            "properties": p}


def _wb(**kw):
    p = {"id3dhp": "IKZ7J", "featuredate": 1694649600000, "gnisid": None, "gnisidlabel": None,
         "featuretype": 3, "featuretypelabel": "Lake", "areasqkm": 0.04}
    p.update(kw)
    return {"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [
        [[-97.4, 46.8], [-97.39, 46.8], [-97.39, 46.81], [-97.4, 46.8]]]}, "properties": p}


@test
def hydro3dhp_gives_3dhp_features_the_nhd_properties():
    p = H.normalise_flowline(_flow())["properties"]
    assert p["gnis_name"] == "Buffalo Creek" and p["gnis_id"] == 1028179
    assert p["ftype"] == 460 and p["fcode"] == 46000 and p["lengthkm"] == 0.025
    assert p["featuredate"] == "2023-09-14" and p["id3dhp"] == "12R9W" and p["streamorder"] == 3
    # unnamed: the name is None (traveltime / runnerculverts treat None and "" alike)
    assert H.normalise_flowline(_flow(gnisidlabel=None, gnisid=None))["properties"]["gnis_name"] is None
    for label, code, want in (("Canal", 2, 336), ("Surface Connector", 4, 334),
                              ("Waterbody Connector", 5, 558), ("Channel Line", 1, 460),
                              ("Pipeline", 9, 428), ("Underground Conduit", 9, 420),
                              ("Something new", 77, 0), (None, 4, 334), (None, None, 0)):
        assert H.ftype_of(label, code, True) == want, (label, code)
    w = H.normalise_waterbody(_wb())["properties"]
    assert w["ftype"] == 390 and w["fcode"] == 39000 and w["areasqkm"] == 0.04
    assert H.normalise_waterbody(_wb(featuretypelabel="River"))["properties"]["ftype"] == 460
    assert H.normalise_waterbody(_wb(featuretypelabel="Ocean or Great Lake"))["properties"]["ftype"] == 390
    # NHD features (an old pack, an NHD service) pass through untouched
    old = {"type": "Feature", "geometry": None, "properties": {"gnis_name": "X", "ftype": 460}}
    assert H.normalise_flowline(old) is old and H.normalise_waterbody(old) is old
    # the original feature is not changed
    f = _flow()
    H.normalise_flowline(f)
    assert "gnis_name" not in f["properties"] and f["properties"]["featuredate"] == 1694649600000


@test
def hydro3dhp_names_urls_and_aliases():
    assert H.FLOWLINE_URL.endswith("/3DHP_all/MapServer/50") and H.WATERBODY_URL.endswith("/60")
    assert C.NHD_FLOWLINE_URL == H.FLOWLINE_URL and C.NHD_WATERBODY_URL == H.WATERBODY_URL
    assert H.layer_of_url(H.FLOWLINE_URL + "/") == 50 and H.layer_of_url(H.WATERBODY_URL) == 60
    assert H.layer_of_url("https://hydro.nationalmap.gov/arcgis/rest/services/nhd/MapServer/6") == 0
    assert H.normaliser_for_url(H.FLOWLINE_URL) is H.normalise_flowlines
    assert H.normaliser_for_url(H.WATERBODY_URL) is H.normalise_waterbodies
    assert H.normaliser_for_url("https://x/arcgis/rest/services/nhd/MapServer/6") is None
    assert H.real_name("3DHP_flowlines", {"NHD_flowlines"}) == "NHD_flowlines"
    assert H.real_name("NHD_flowlines", {"3DHP_flowlines"}) == "3DHP_flowlines"
    assert H.real_name("3DHP_flowlines", {"3DHP_flowlines", "NHD_flowlines"}) == "3DHP_flowlines"
    assert H.real_name("NID_dams", {"NHD_flowlines"}) == "NID_dams"
    assert H.first_available(("3DHP_waterbodies", "NHD_waterbodies"), {"NHD_waterbodies"}) == \
        "NHD_waterbodies"
    assert H.first_available(("3DHP_waterbodies",), {"NID_dams"}) is None
    assert H.dedupe(["3DHP_flowlines", "NHD_flowlines", "X"]) == ["3DHP_flowlines", "X"]
    assert H.dedupe(["NHD_flowlines", "3DHP_flowlines"]) == ["NHD_flowlines"]
    assert H.dedupe(["a", "b"]) == ["a", "b"]
    assert GP.group_of("3DHP_flowlines") == "hydro" == GP.group_of("NHD_flowlines")
    assert GP.group_of("SARP_dams") == "barriers"


def _pack_with(names_feats, folder):
    man = GP.new_manifest((-98.5, 46.0, -96.5, 48.0), created_by="test")
    for name, feats in names_feats.items():
        GP.add_dataset(folder, man, name, lambda f=feats: iter(f),
                       {"label": name, "fetched": "2026-10-12", "status": "complete", "url": "x"})
    GP.write_manifest(folder, man)
    return man


@test
def format_2_pack_answers_to_the_old_and_the_new_name():
    d = tempfile.mkdtemp()
    old = os.path.join(d, "old")
    _pack_with({"NHD_flowlines": [H.normalise_flowline(_flow())]}, old)
    r = GP.GpkgPackReader(old)
    ext = (-97.5, 46.7, -97.3, 46.9)
    for nm in ("NHD_flowlines", "3DHP_flowlines"):
        assert r.has(nm) and r.info(nm)["count"] == 1 and r.covers(ext, nm)
        got = r.query(nm, ext)
        assert len(got) == 1 and got[0]["properties"]["gnis_name"] == "Buffalo Creek"
        assert r.uri(nm).endswith("|layername=NHD_flowlines")
        assert r.path(nm).endswith("NHD_flowlines.gpkg") and r.index_kind(nm) == "rtree"
    assert not r.has("3DHP_waterbodies") and not r.has("NID_dams")
    new = os.path.join(d, "new")
    _pack_with({"3DHP_waterbodies": [H.normalise_waterbody(_wb())]}, new)
    r = GP.GpkgPackReader(new)
    for nm in ("NHD_waterbodies", "3DHP_waterbodies"):
        assert r.has(nm) and len(r.query(nm, ext)) == 1
    assert "NHD_waterbodies" not in r.datasets      # the listing shows the real names only
    assert OP.open_pack(new).has("NHD_waterbodies")


@test
def format_1_pack_answers_to_the_old_and_the_new_name():
    d = tempfile.mkdtemp()
    db = os.path.join(d, "p.sqlite")
    w = OP.PackWriter(db, (-98.5, 46.0, -96.5, 48.0), created_by="t")
    w.begin("NHD_flowlines", "NHD flowlines", "x")
    w.add([H.normalise_flowline(_flow())], lambda f: f["properties"]["id3dhp"])
    w.finish()
    w.close(finalize=True)
    r = OP.PackReader(db)
    ext = (-97.5, 46.7, -97.3, 46.9)
    for nm in ("NHD_flowlines", "3DHP_flowlines"):
        assert r.has(nm) and r.info(nm)["count"] == 1 and r.covers(ext, nm)
        assert len(r.query(nm, ext)) == 1
        assert r.index_kind(nm) in ("rtree", "btree")


@test
def services_default_to_3dhp_and_keep_nhd_listed():
    cat = SV.Catalog(None, {})
    assert cat.chosen("flowlines")["id"] == "usgs_3dhp_flowline"
    assert cat.chosen("waterbodies")["id"] == "usgs_3dhp_waterbody"
    assert SV.layer_url(cat.chosen("flowlines")) == C.NHD_FLOWLINE_URL
    assert SV.layer_url(cat.chosen("waterbodies")) == C.NHD_WATERBODY_URL
    ids = [s["id"] for s in cat.services("flowlines")]
    assert "usgs_nhd_flowline" in ids and "usgs_nhdplushr_flowline" in ids
    assert "usgs_nhd_waterbody" in [s["id"] for s in cat.services("waterbodies")]
    # the default adds no override; choosing the NHD service does
    assert SV.hazard_overrides(cat) == {}
    cat.choice["flowlines"] = "usgs_nhd_flowline"
    assert SV.hazard_overrides(cat)["NHD_FLOWLINE_URL"].endswith("/nhd/MapServer/6")
    # aquatic barriers
    assert cat.chosen("barrier_dams")["url"] == C.SARP_DAMS_URL
    assert cat.chosen("barrier_crossings")["url"] == C.SARP_CROSSINGS_URL
    assert SV.ROLES["barrier_dams"][1] == "vector"


@test
def service_fetch_adds_the_nhd_properties_to_3dhp_features():
    cat = SV.Catalog(None, {})
    calls = []

    def fake_get(url):
        calls.append(url)
        return json.dumps({"type": "FeatureCollection", "features": [_flow()]}).encode()
    got = SV.fetch_features(cat.chosen("flowlines"), (-97.5, 46.7, -97.3, 46.9),
                            http_get=fake_get)
    assert got[0]["properties"]["gnis_name"] == "Buffalo Creek" and got[0]["properties"]["ftype"] == 460
    assert "/3DHP_all/MapServer/50/query" in calls[0]
    got = SV.fetch_features(cat.get("usgs_nhd_flowline"), (-97.5, 46.7, -97.3, 46.9),
                            http_get=lambda u: json.dumps({"features": [
                                {"type": "Feature", "geometry": None,
                                 "properties": {"gnis_name": "N", "ftype": 460}}]}).encode())
    assert got[0]["properties"] == {"gnis_name": "N", "ftype": 460}


@test
def builder_lists_3dhp_barriers_files_and_land_cover():
    rows = {t[0]: t for t in B.datasets()}
    assert "NHD_flowlines" not in rows and "NHD_waterbodies" not in rows
    f, w = rows["3DHP_flowlines"], rows["3DHP_waterbodies"]
    assert f[2] == w[2] == "hydro" and f[3] == "arcgis"
    assert f[4]["url"] == C.NHD_FLOWLINE_URL and f[4]["transform"] is H.normalise_flowlines
    assert w[4]["transform"] is H.normalise_waterbodies and "areasqkm" in w[4]["fields"]
    assert "gnisidlabel" in f[4]["fields"] and "id3dhp" in f[4]["fields"]
    s = rows["SARP_dams"]
    assert s[2] == "barriers" and s[4]["url"] == C.SARP_DAMS_URL and "NIDID" in s[4]["fields"]
    assert rows["SARP_surveyed_crossings"][4]["url"] == C.SARP_CROSSINGS_URL
    assert "SARP_unsurveyed_crossings" not in rows
    for n in PF.NAMES:
        assert rows[n][3] == "file", n
    assert rows["DWR_dams"][4]["fallback"]["url"] == C.DWR_DAMS_URL
    assert rows[B.NLCD_NAME][3] == "raster" and rows[B.NLCD_NAME][2] == "landcover"
    # old group names still select the new layers
    assert B._pick("nhd", list(rows.values())) == {"hydro"}
    assert B._pick("NHD_flowlines,sarp", list(rows.values())) == {"3DHP_flowlines", "barriers"}
    # a data-source file that points the address at the older NHD service gets that service's fields
    spec = B._hydro_spec("https://hydro.nationalmap.gov/arcgis/rest/services/nhd/MapServer/6", True)
    assert "transform" not in spec and spec["fields"].startswith("gnis_name")
    # the signature of a stored chunk changes with the transform (so old chunks are not reused)
    assert B._signature("arcgis", f[4], None) != B._signature("arcgis", {"fields": f[4]["fields"],
                                                                         "precision": 6}, None)


@test
def builder_keeps_only_the_fields_the_layer_has():
    logs = []
    info = {"fields": [{"name": "Name"}, {"name": "State"}, {"name": "OBJECTID"}]}
    assert B._known_fields(info, "name,State,Missing,Other", logs.append) == "Name,State"
    assert "Missing, Other" in logs[0]
    assert B._known_fields(info, "*", logs.append) == "*"
    assert B._known_fields(info, "Zip", logs.append) == "*"
    assert B._known_fields({}, "A,B", logs.append) == "A,B"


@test
def builder_transforms_each_batch_before_it_is_stored():
    stored = []

    class W(object):
        def __init__(self):
            self._cur = {"n": 0}

        def chunks(self):
            return {}

        def add(self, feats, uid, chunk=None, payload=None):
            stored.extend(feats)
            self._cur["n"] += len(feats)
            return len(feats)

        def drop_chunks(self, keys):
            pass

    class Ctx(object):
        workers = 1
        refresh = True
        status = "complete"
        gaps = []

        def __init__(self):
            self.writer = W()
            self.log = lambda m: None
            self.sub = lambda p, m="": None

    answers = {"": {"objectIdField": "OBJECTID", "maxRecordCount": 2000,
                    "fields": [{"name": "OBJECTID", "type": "esriFieldTypeOID"}]}}
    orig_json, orig_ids, orig_chunk = B.http_json, B.arcgis_ids, B._arcgis_chunk
    B.http_json = lambda url, params=None, **k: answers[""]
    B.arcgis_ids = lambda url, bbox, **k: {1, 2}
    seen_fields = []
    B._arcgis_chunk = lambda url, ids, out_fields, precision, retries=5: (
        seen_fields.append(out_fields) or [dict(_flow(), id=i) for i in ids])
    try:
        B.fetch_arcgis(Ctx(), "https://x/MapServer/50", (0, 0, 1, 1), "gnisidlabel", 6,
                       H.normalise_flowlines)
    finally:
        B.http_json, B.arcgis_ids, B._arcgis_chunk = orig_json, orig_ids, orig_chunk
    assert len(stored) == 2 and all(f["properties"]["ftype"] == 460 for f in stored)


@test
def a_heavy_layer_is_read_in_tiles_paged_by_offset():
    """3DHP: the state-wide id list times out, small boxes answer - tile_deg reads tiles, pages each
    tile by offset, and keeps a feature that touches two tiles once."""
    stored, keys = {}, []

    class W(object):
        def __init__(self):
            self._cur = {"n": 0}

        def chunks(self):
            return {}

        def add(self, feats, uid, chunk=None, payload=None):
            keys.append(chunk)
            for f in feats:
                stored[uid(f)] = f
            self._cur["n"] = len(stored)
            return len(feats)

        def drop_chunks(self, keys):
            pass

    class Ctx(object):
        workers = 1
        refresh = True
        status = "complete"
        gaps = []

        def __init__(self):
            self.writer = W()
            self.log = lambda m: None
            self.sub = lambda p, m="": None

    info = {"objectIdField": "OBJECTID", "maxRecordCount": 2,
            "fields": [{"name": "OBJECTID", "type": "esriFieldTypeOID"}]}
    calls = []

    def fake(url, params=None, **k):
        if not params or params.get("f") == "json" and "geometry" not in params:
            return info
        calls.append((params["geometry"], params["resultOffset"]))
        off = params["resultOffset"]
        rows = [1, 2, 3]                 # the same three features in every tile (shared edges)
        page = rows[off:off + params["resultRecordCount"]]
        return {"features": [dict(_flow(), id=i) for i in page],
                "exceededTransferLimit": off + len(page) < len(rows)}
    orig = B.http_json
    B.http_json = fake
    try:
        note = B.fetch_arcgis(Ctx(), "https://x/MapServer/50", (0, 0, 1, 0.5), "*", 6,
                              H.normalise_flowlines, tile_deg=0.5)
    finally:
        B.http_json = orig
    assert note == "" and sorted(stored) == [1, 2, 3]
    assert len(keys) == 2 and len(calls) == 4          # 2 tiles x 2 pages
    assert all(f["properties"]["ftype"] == 460 for f in stored.values())


@test
def packfiles_recognise_the_dwr_and_hifld_files_by_name():
    for name, want in (("DWR_dams.gpkg", "DWR_dams"), ("Dams.shp", "DWR_dams"),
                       ("ND_DWR_Dam_Lines_2026.shp", "DWR_dam_lines"),
                       ("DamLines.shp", "DWR_dam_lines"), ("Dugouts_poly.shp", "DWR_dugout_polygons"),
                       ("DWR_Dugouts.shp", "DWR_dugouts"),
                       ("unknown_structures.shp", "DWR_unknown_structures"),
                       ("Fire_and_Emergency_Medical_Service_(EMS)_Stations.geojson",
                        "HIFLD_fire_ems_stations"),
                       ("Local_Emergency_Operations_Centers_(EOC).geojson", "HIFLD_local_eoc"),
                       ("Local_Law_Enforcement_Locations.shp", "HIFLD_local_law_enforcement"),
                       ("Wastewater_Treatment_Plants.gpkg", "HIFLD_wastewater_treatment_plants"),
                       ("HIFLD_local_eoc.gpkg", "HIFLD_local_eoc"),
                       ("damage.shp", None), ("random.shp", None), ("notes.txt", None)):
        assert PF.classify(name) == want, (name, PF.classify(name), want)
    d = tempfile.mkdtemp()
    os.makedirs(os.path.join(d, "dwr", "_other"))
    os.makedirs(os.path.join(d, "hifld"))
    os.makedirs(os.path.join(d, "ND_Dam_Suite_Pack", "dwr"))
    for rel in ("dwr/Dams.shp", "dwr/_other/Dugouts.gpkg", "hifld/Wastewater_Treatment_Plants.gpkg",
                "dwr/readme.txt", "ND_Dam_Suite_Pack/dwr/DWR_dams.gpkg"):
        open(os.path.join(d, *rel.split("/")), "w").write("x")
    open(os.path.join(d, "ND_Dam_Suite_Pack", "manifest.json"), "w").write("{}")
    open(os.path.join(d, "ND_Dam_Suite_Pack", "LAYERS.txt"), "w").write("")
    found = PF.scan(PF.folders_for(d))
    assert sorted(found) == ["DWR_dams", "DWR_dugouts", "HIFLD_wastewater_treatment_plants"], found
    assert "ND_Dam_Suite_Pack" not in found["DWR_dams"]        # the pack itself is not read
    assert PF.scan([os.path.join(d, "nothing")]) == {}
    assert "dwr" in PF.missing_text("DWR_dam_lines") and "hifld" in PF.missing_text("HIFLD_local_eoc")


@test
def packfiles_write_a_layer_file_into_the_pack():
    try:
        from osgeo import ogr          # noqa: F401
    except ImportError:
        print("      (no GDAL here - skipped)")
        return
    d = tempfile.mkdtemp()
    src = os.path.join(d, "files")
    os.makedirs(src)
    fc = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"name": "in"},
         "geometry": {"type": "Point", "coordinates": [-97.4, 46.8]}},
        {"type": "Feature", "properties": {"name": "out"},
         "geometry": {"type": "Point", "coordinates": [-120.0, 40.0]}}]}
    path = os.path.join(src, "Local_Emergency_Operations_Centers_EOC.geojson")
    json.dump(fc, open(path, "w"))
    pack = os.path.join(d, "pack")
    found = PF.scan([src])
    assert found == {"HIFLD_local_eoc": path}
    added, problems = PF.add_files(pack, found, bbox=(-98.5, 46.0, -96.5, 48.0))
    assert added == {"HIFLD_local_eoc": 1} and not problems         # the point outside is clipped
    man = GP.read_manifest(pack)
    e = man["datasets"]["HIFLD_local_eoc"]
    assert e["file"] == "hifld/HIFLD_local_eoc.gpkg" and e["status"] == "complete"
    assert "HIFLD" in e["source"] and os.path.isfile(os.path.join(pack, "LAYERS.txt"))
    r = GP.GpkgPackReader(pack)
    assert [f["properties"]["name"] for f in r.query("HIFLD_local_eoc", (-98, 46, -97, 47))] == ["in"]
    # a second run keeps the layer (the file is not newer); replace=True writes it again
    added, _p = PF.add_files(pack, found, bbox=(-98.5, 46.0, -96.5, 48.0))
    assert added == {}
    added, _p = PF.add_files(pack, found, bbox=(-98.5, 46.0, -96.5, 48.0), replace=True)
    assert added == {"HIFLD_local_eoc": 1}


@test
def the_3dhp_national_file_is_cut_to_the_pack_box():
    """The National Map file is one national layer pair in an Albers system: only the box is read,
    reprojected to lon / lat, with the NHD-style properties added."""
    try:
        from osgeo import ogr, osr
    except ImportError:
        print("      (no GDAL here - skipped)")
        return
    d = tempfile.mkdtemp()
    src = os.path.join(d, "dl")
    os.makedirs(src)
    path = os.path.join(src, "3dhp_all_CONUS_20260112_GPKG.gpkg")
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(5070)
    wgs = osr.SpatialReference()
    wgs.ImportFromEPSG(4326)
    try:
        wgs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    except AttributeError:
        pass
    fwd = osr.CoordinateTransformation(wgs, srs)
    ds = ogr.GetDriverByName("GPKG").CreateDataSource(path)
    fl = ds.CreateLayer("hydro_3dhp_all_flowline", srs, ogr.wkbLineString)
    wb = ds.CreateLayer("hydro_3dhp_all_waterbody", srs, ogr.wkbPolygon)
    for lyr in (fl, wb):
        for nm, tp in (("id3dhp", ogr.OFTString), ("gnisidlabel", ogr.OFTString),
                       ("featuretype", ogr.OFTInteger), ("featuretypelabel", ogr.OFTString)):
            lyr.CreateField(ogr.FieldDefn(nm, tp))

    def line(lon, lat, label):
        x0, y0 = fwd.TransformPoint(lon, lat)[:2]
        f = ogr.Feature(fl.GetLayerDefn())
        f.SetField("id3dhp", label)
        f.SetField("gnisidlabel", "Buffalo Creek" if label == "in" else "Far Creek")
        f.SetField("featuretypelabel", "Stream/River")
        g = ogr.Geometry(ogr.wkbLineString)
        g.AddPoint_2D(x0, y0)
        g.AddPoint_2D(x0 + 500, y0 + 500)
        f.SetGeometry(g)
        fl.CreateFeature(f)
    line(-97.4, 46.8, "in")
    line(-120.0, 40.0, "out")
    x0, y0 = fwd.TransformPoint(-97.4, 46.82)[:2]
    ring = ogr.Geometry(ogr.wkbLinearRing)
    for dx, dy in ((0, 0), (300, 0), (300, 300), (0, 300), (0, 0)):
        ring.AddPoint_2D(x0 + dx, y0 + dy)
    poly = ogr.Geometry(ogr.wkbPolygon)
    poly.AddGeometry(ring)
    f = ogr.Feature(wb.GetLayerDefn())
    f.SetField("id3dhp", "lake")
    f.SetField("featuretypelabel", "Lake/Pond")
    f.SetGeometry(poly)
    wb.CreateFeature(f)
    ds = None
    assert PF.scan_3dhp([src]) == path and PF.scan_3dhp([os.path.join(d, "none")]) is None
    pack = os.path.join(d, "pack")
    added, problems = PF.add_3dhp(pack, path, bbox=(-98.5, 46.0, -96.5, 48.0))
    assert added == {"3DHP_flowlines": 1, "3DHP_waterbodies": 1} and not problems, (added, problems)
    r = GP.GpkgPackReader(pack)
    feats = r.query("3DHP_flowlines", (-98, 46, -97, 47))
    assert len(feats) == 1
    pr = feats[0]["properties"]
    assert pr["gnis_name"] == "Buffalo Creek" and pr["ftype"] == 460 and pr["fcode"] == 46000
    xs = [c[0] for c in feats[0]["geometry"]["coordinates"]]
    assert -97.5 < xs[0] < -97.3                                  # lon / lat, not metres
    assert len(r.query("3DHP_waterbodies", (-98, 46, -97, 47))) == 1
    added, _p = PF.add_3dhp(pack, path, bbox=(-98.5, 46.0, -96.5, 48.0))
    assert added == {}                                            # kept
    # the builder uses it before the web service
    left = B._add_3dhp_bulk(os.path.join(d, "pack2"),
                            [("3DHP_flowlines", "x", "hydro", "arcgis", {}),
                             ("SARP_dams", "y", "barriers", "arcgis", {})],
                            (-98.5, 46.0, -96.5, 48.0), [src], False, lambda p, m: None,
                            lambda m: None)
    assert [t[0] for t in left] == ["SARP_dams"]


@test
def builder_folder_write_replaces_nhd_with_3dhp():
    d = tempfile.mkdtemp()
    pack = os.path.join(d, "pack")
    _pack_with({"NHD_flowlines": [_flow(gnis_name="old")], "NHD_waterbodies": [_wb()]}, pack)
    db = os.path.join(d, "work.sqlite")
    w = OP.PackWriter(db, (-98.5, 46.0, -96.5, 48.0), created_by="t")
    w.begin("3DHP_flowlines", "3DHP flowlines", "x")
    w.add([H.normalise_flowline(_flow())], lambda f: f["properties"]["id3dhp"])
    w.finish()
    w.begin("3DHP_waterbodies", "3DHP waterbodies", "x")
    w.add([H.normalise_waterbody(_wb())], lambda f: f["properties"]["id3dhp"])
    w.finish()
    man = w.manifest()
    w.close(finalize=False)
    logs = []
    out = B._write_folder(db, pack, man, logs.append,
                          extra_info={"3DHP_flowlines": {"source": "3DHP test", "used_by": "t"}})
    assert set(out["datasets"]) == {"3DHP_flowlines", "3DHP_waterbodies"}, out["datasets"].keys()
    assert "replaced by 3DHP_flowlines" in out["left_out"]["NHD_flowlines"]
    assert not os.path.exists(os.path.join(pack, "hydro", "NHD_flowlines.gpkg"))
    assert os.path.isfile(os.path.join(pack, "hydro", "3DHP_flowlines.gpkg"))
    assert out["datasets"]["3DHP_flowlines"]["source"] == "3DHP test"
    txt = open(os.path.join(pack, "LAYERS.txt"), encoding="utf-8").read()
    assert "3DHP_flowlines" in txt and "replaced by 3DHP_flowlines" in txt
    # an old-name request is still answered
    assert GP.GpkgPackReader(pack).has("NHD_flowlines")


@test
def damdata_reads_a_pack_with_both_names_once():
    from ND_Dam_Hazard_Helper.damsuite import damdata as DD

    class R(object):
        datasets = {"3DHP_flowlines": {}, "NHD_flowlines": {}}

        def has(self, n):
            return n in H.ALIASES or n in self.datasets

        def covers(self, box, n):
            return True

        def query(self, n, box):
            return [_flow()]

        def info(self, n):
            return {"fetched": "2026-10-12"}
    assert DD.PACK_SETS["flowlines"] == ("3DHP_flowlines", "NHD_flowlines")
    assert DD.PACK_SETS["waterbodies"] == ("3DHP_waterbodies", "NHD_waterbodies")
    names = H.dedupe([n for n in DD.PACK_SETS["flowlines"] if R().has(n)])
    assert names == ["3DHP_flowlines"]


@test
def land_cover_web_coverage_request_names_the_grid_axes():
    s = SV.Catalog(None, {}).get("mrlc_nlcd2021_wcs")
    url = SV.wcs_coverage_url(s, (-250000, 2800000, -240000, 2810000), (333, 333))
    assert url.startswith("https://www.mrlc.gov/geoserver/mrlc_download/wcs?")
    assert "scaleSize=i%28333%29%2Cj%28333%29" in url         # X / Y: ScaleAxisUndefined (HTTP 404)
    other = dict(s, scale_axes=None)
    assert "scaleSize=X%28333%29%2CY%28333%29" in SV.wcs_coverage_url(
        other, (-250000, 2800000, -240000, 2810000), (333, 333))
