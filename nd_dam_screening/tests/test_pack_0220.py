"""0.22.0 (damsuite 1.10.0): the offline pack as a folder with one GeoPackage per layer (gpkgpack),
read exactly like the one-file pack; the data source (offline pack or download) and the pack
location shared by the plugins (suitedata); the DWR files and land cover taken from the pack; the
window's suite-data fallback in Download mode. Qt test doubles only (qgis_mock).

Run: PYTHONPATH=tests/qgis_mock:<parent of the plugin> python3 tests/test_pack_0220.py
"""
import json, os, shutil, sqlite3, sys, tempfile, zipfile

os.environ["HOME"] = tempfile.mkdtemp(prefix="pk0220_home_")
for _k in ("NDSUITE_DATA", "NDSUITE_DAMS", "NDSUITE_PACK", "NDSUITE_SOURCE"):
    os.environ.pop(_k, None)
HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(HERE, "qgis_mock"))
sys.path.insert(0, os.path.dirname(PLUGIN))

from nd_dam_screening.damsuite import offline_pack as OP  # noqa: E402
from nd_dam_screening.damsuite import gpkgpack as GP  # noqa: E402
from nd_dam_screening.damsuite import suitedata as SD  # noqa: E402
from nd_dam_screening.damsuite import suitegpkg as G  # noqa: E402
from nd_dam_screening.damsuite import damdata as DD  # noqa: E402
from nd_dam_screening.core import workflow as wf  # noqa: E402

T = tempfile.mkdtemp(prefix="pk0220_")
BBOX = (-104.15, 45.83, -96.45, 49.1)


def feat(_g, **props):
    return {"type": "Feature", "geometry": _g, "properties": props}


# ---- a small one-file pack (format 1) -------------------------------------------------------
db = os.path.join(T, "work", OP.DB_NAME)
os.makedirs(os.path.dirname(db))
w = OP.PackWriter(db, BBOX, created_by="test")
sets = {
    # odd properties: an empty text, a bool, a nested list, a field mixing numbers and text,
    # names a GeoPackage cannot take as they are (fid, geom, Name next to NAME), a big integer
    "NID_dams": [feat({"type": "Point", "coordinates": [-98.0 + i * 0.01, 47.0]},
                      NIDID="ND%05d" % i, NAME="Dam %d" % i, Name="lower", fid=i, geom="x",
                      NID_HEIGHT=10.5 + i, OWNER="" if i % 2 else None, PRIVATE=bool(i % 2),
                      TAGS=[i, "a"], MIXED=(i if i % 3 else "n/a"), BIG=2 ** 40 + i)
                 for i in range(30)],
    "NHD_waterbodies": [feat({"type": "Polygon", "coordinates": [[[-98.1, 47.1], [-98.0, 47.1],
                                                                 [-98.0, 47.2], [-98.1, 47.1]]]},
                             gnis_name="Pond A", ftype=390, areasqkm=0.5),
                        feat({"type": "MultiPolygon", "coordinates": [[[[-97.0, 46.0], [-96.9, 46.0],
                                                                        [-96.9, 46.1], [-97.0, 46.0]]]]},
                             gnis_name=None, ftype=436, areasqkm=1.25)],
    "NHD_flowlines": [feat({"type": "LineString", "coordinates": [[-98.2, 47.0], [-97.8, 47.3]]},
                           gnis_name="Elm River", ftype=460)],
}
for name, feats in sets.items():
    w.begin(name, name.replace("_", " "), "https://example.test/" + name)
    w.add(feats, lambda f, n=name: "{}:{}".format(n, json.dumps(f["properties"], sort_keys=True,
                                                                default=str)))
    w.finish(note="test note" if name == "NID_dams" else "")
w.begin("HPMS_ND_2020", "HPMS 2020", "https://example.test/hpms")
w.fail("HPMS_ND_2020", "403 Forbidden")
man = w.manifest()
w.close(finalize=True)
v1 = OP.PackReader(db)
assert set(v1.datasets) == set(sets), v1.datasets.keys()

# ---- converted to a folder pack (format 2) ---------------------------------------------------
pack = os.path.join(T, "ND_Dam_Suite_Pack")
m = GP.convert_v1(db, pack, extra_info={"NID_dams": {"source": "USACE NID (test)"}})
assert m["format_version"] == 2 and m["layout"] == "gpkg"
assert m["datasets"]["NID_dams"]["file"] == "hydro/NID_dams.gpkg"
assert m["datasets"]["NHD_waterbodies"]["geometry"] == "MULTIPOLYGON"
assert m["datasets"]["NHD_flowlines"]["geometry"] == "LINESTRING"
assert "HPMS_ND_2020" in m["left_out"] and "HPMS_ND_2020" not in m["datasets"]
for name in sets:
    p = os.path.join(pack, *m["datasets"][name]["file"].split("/"))
    assert os.path.isfile(p)
    lays = G.list_layers(p)
    assert [x["name"] for x in lays] == [name] and lays[0]["srs_id"] == 4326, lays
    con = sqlite3.connect(p)
    try:      # the GeoPackage R*Tree index and its row count
        n_rt = con.execute('SELECT count(*) FROM "rtree_{}_geom"'.format(name)).fetchone()[0]
        ext = con.execute("SELECT extension_name FROM gpkg_extensions").fetchall()
        trig = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='trigger'")]
    finally:
        con.close()
    assert n_rt == len(sets[name]) and ext == [("gpkg_rtree_index",)] and len(trig) == 6, trig
cols = [c for c, _n, _k in m["datasets"]["NID_dams"]["fields"]]
assert "fid" not in cols and "geom" not in cols and len({c.lower() for c in cols}) == len(cols)
kinds = {n: k for _c, n, k in m["datasets"]["NID_dams"]["fields"]}
assert kinds["PRIVATE"] == "bool" and kinds["TAGS"] == "json" and kinds["MIXED"] == "json"
assert kinds["NID_HEIGHT"] == "real" and kinds["BIG"] == "int" and kinds["OWNER"] == "text"

v2 = OP.open_pack(pack)
assert isinstance(v2, GP.GpkgPackReader) and v2.index_kind("NID_dams") == "rtree"
assert set(v2.datasets) == set(sets) and v2.info("NID_dams")["note"] == "test note"
assert v2.covers((-98, 47, -97, 48), "NID_dams") and not v2.covers((-120, 47, -97, 48))
# every query returns what the one-file pack returns: same features, order, properties
for name in sets:
    for box in (BBOX, (-98.05, 46.9, -97.85, 47.05), (-98.15, 47.12, -98.05, 47.15),
                (-97.05, 45.95, -96.95, 46.05), (-90, 40, -89, 41)):
        a, b = v1.query(name, box), v2.query(name, box)
        assert len(a) == len(b), (name, box, len(a), len(b))
        for fa, fb in zip(a, b):
            pa = dict((fa.get("properties") or {}))
            assert fb["properties"] == {k: pa.get(k) for k in fb["properties"]}, (fa, fb)
            assert set(pa) <= set(fb["properties"])
            ga, gb = fa["geometry"], fb["geometry"]
            if ga["type"] == gb["type"]:
                assert ga["coordinates"] == gb["coordinates"], (ga, gb)
            else:                                    # single -> multi in a multi layer
                assert gb["type"] == "Multi" + ga["type"] and gb["coordinates"] == [ga["coordinates"]]
one = v2.query("NID_dams", (-97.995, 46.99, -97.985, 47.01))
assert len(one) == 1 and one[0]["properties"]["OWNER"] == "" and one[0]["properties"]["fid"] == 1
assert one[0]["properties"]["PRIVATE"] is True and one[0]["properties"]["TAGS"] == [1, "a"]
assert one[0]["properties"]["Name"] == "lower" and one[0]["properties"]["NAME"] == "Dam 1"
assert isinstance(one[0]["geometry"]["coordinates"], list)
# a file inside the pack folder opens the pack; describe says what it is
assert isinstance(OP.open_pack(os.path.join(pack, "LAYERS.txt")), GP.GpkgPackReader)
assert "one GeoPackage per layer" in OP.describe(pack)
# LAYERS.txt: each layer, its file and its source; what is not in the pack
txt = open(os.path.join(pack, "LAYERS.txt"), encoding="utf-8").read()
assert "hydro/NID_dams.gpkg  (30 features, point" in txt and "Source:  USACE NID (test)" in txt
assert "Address: https://example.test/NHD_flowlines" in txt and "HPMS_ND_2020: failed" in txt
# a zipped pack folder is unpacked once and read the same
zp = os.path.join(T, "pack.zip")
with zipfile.ZipFile(zp, "w") as z:
    for root, _d, files in os.walk(pack):
        for f in files:
            full = os.path.join(root, f)
            z.write(full, os.path.join("ND_Dam_Suite_Pack", os.path.relpath(full, pack)))
vz = OP.open_pack(zp)
assert isinstance(vz, GP.GpkgPackReader) and len(vz.query("NID_dams", BBOX)) == 30
# the suite export of ND Dam Hazard and the dam data read the folder pack as a pack
assert DD.find_in_pack(v2, {"nid_id": "ND00003"})[2]["nidid"] == "ND00003"

# ---- files and rasters added to the pack ------------------------------------------------------
dwr = os.path.join(T, "dwr_src.gpkg")
gw = G.GpkgWriter(dwr)
lw = gw.layer("dams", "POINT", [("structure_id", "int"), ("dam_name", "text")], G.EPSG_4326)
lw.add({"type": "Point", "coordinates": [-98.0, 47.0]}, {"structure_id": 5289, "dam_name": "A"})
gw.close()


def add_file(folder, manifest, name, src, info, **kw):
    """add_vector_file needs GDAL; the test double copies the features with suitegpkg instead."""
    feats = [feat(g, **p) for g, p in G.read_features(src, G.list_layers(src)[0]["name"])]
    return GP.add_dataset(folder, manifest, name, lambda: iter(feats), info)


add_file(pack, m, "DWR_dams", dwr, {"label": "DWR dams", "source": "DWR export (test)"})
add_file(pack, m, "DWR_dam_lines", dwr, {"label": "DWR dam lines"})
tif = os.path.join(T, "nlcd.tif")
with open(tif, "wb") as fh:
    fh.write(b"II*\x00" + b"\x00" * 64)
GP.add_raster_file(pack, m, "NLCD_2021_landcover", tif, {"label": "NLCD"})
GP.write_manifest(pack, m)
assert m["datasets"]["DWR_dams"]["file"] == "dwr/DWR_dams.gpkg"
assert m["rasters"]["NLCD_2021_landcover"]["file"] == "landcover/NLCD_2021_landcover.tif"

# ---- the data source and the pack location (suitedata) ----------------------------------------
data = os.path.join(T, "suite_data")
SD.ensure_layout(data)
SD.set_data_folder(data)
assert SD.data_source() == "pack" and SD.pack_location() == ""
assert SD.active_pack() == ""                                   # nothing in the data folder yet
SD.set_pack_location(os.path.join(pack, "LAYERS.txt"))         # a file in the pack: its folder
assert SD.active_pack() == pack
assert SD.dwr_layers()["dwr_dams"] == os.path.join(pack, "dwr", "DWR_dams.gpkg")
assert SD.dwr_layers()["dwr_dam_lines"] == os.path.join(pack, "dwr", "DWR_dam_lines.gpkg")
assert SD.nlcd_raster() == os.path.join(pack, "landcover", "NLCD_2021_landcover.tif")
st = SD.status()
assert st["pack"] == pack and st["pack_layout"] == "gpkg" and st["source"] == "pack"
lines = SD.describe(st)
assert lines[0] == "Data source: Offline pack" and "one GeoPackage per layer" in lines[1], lines
# Download: the pack is not read (no pack, no DWR / land cover from it)
SD.set_data_source("download")
assert SD.active_pack() == "" and SD.dwr_layers()["dwr_dams"] == "" and SD.nlcd_raster() == ""
assert SD.describe(SD.status())[0].startswith("Data source: Download from the servers")
SD.set_data_source("pack")
# no pack location: the pack folder inside the data folder is found
SD.set_pack_location("")
shutil.copytree(pack, os.path.join(data, "ND_Dam_Suite_Pack"))
assert SD.active_pack() == os.path.join(data, "ND_Dam_Suite_Pack")
assert SD.find_pack(os.path.join(data, "ND_Dam_Suite_Pack")) == os.path.join(data, "ND_Dam_Suite_Pack")
# the environment wins (scripts)
os.environ["NDSUITE_SOURCE"] = "download"
assert SD.data_source() == "download"
os.environ.pop("NDSUITE_SOURCE")

# ---- the window's suite-data fallback: the folder pack, then Download ------------------------
work = os.path.join(T, "work_eddy")
os.makedirs(work)
S = {}
v, origin = wf.slot_origin("waterbodies", work, S, prepare=True)
assert origin == "suite pack" and v.endswith("0_suite_data/waterbodies.gpkg|layername=waterbodies"), (v, origin)
v, origin = wf.slot_origin("dwr", work, S, prepare=True)
assert origin == "suite pack", origin                           # DWR_dams of the pack
box = json.load(open(os.path.join(work, "0_suite_data", "waterbodies.gpkg.box.json")))
assert box["from"] == "pack"
# Download: the server chosen for the role, fetched for the work area
SD.set_data_source("download")
calls = []


def fake_fetch(role, box, catalog=None, http_get=None, cancel=None):
    calls.append((role, tuple(box)))
    return ([feat({"type": "Polygon", "coordinates": [[[-98.1, 47.1], [-98.0, 47.1], [-98.0, 47.2],
                                                      [-98.1, 47.1]]]}, gnis_name="Downloaded")],
            "Test server (https://example.test/wb)", "Test server")


real_fetch = DD.fetch_role
DD.fetch_role = fake_fetch
try:
    log = []
    v, origin = wf.slot_origin("waterbodies", work, S, prepare=True, log=log.append)
    assert origin == "download" and calls and calls[-1][0] == "waterbodies", (origin, calls, log)
    box = json.load(open(os.path.join(work, "0_suite_data", "waterbodies.gpkg.box.json")))
    assert box["from"] == "download"
    rows = G.read_features(os.path.join(work, "0_suite_data", "waterbodies.gpkg"), "waterbodies")
    assert len(rows) == 1 and rows[0][1]["gnis_name"] == "Downloaded"
    n = len(calls)
    wf.slot_origin("waterbodies", work, S, prepare=True)        # the same area: kept
    assert len(calls) == n
    assert "downloaded for the work area" in wf.ORIGIN_TEXT["download"]
finally:
    DD.fetch_role = real_fetch
# back to the pack: the downloaded clip is replaced by the pack's
SD.set_data_source("pack")
wf.slot_origin("waterbodies", work, S, prepare=True)
box = json.load(open(os.path.join(work, "0_suite_data", "waterbodies.gpkg.box.json")))
assert box["from"] == "pack"
rows = G.read_features(os.path.join(work, "0_suite_data", "waterbodies.gpkg"), "waterbodies")
assert {r[1].get("gnis_name") for r in rows} == {"Pond A", None}, rows

# ---- Get data for this dam: Download never opens the pack ------------------------------------
dam = os.path.join(T, "dams", "ND00003")
os.makedirs(dam)
from nd_dam_screening.damsuite import dam_record as DR  # noqa: E402
rec = DR.load(dam, create=True)
for k, v in (("dam_id", "ND00003"), ("nid_id", "ND00003"), ("lon", -98.05), ("lat", 47.15)):
    rec.set(k, v, "ndscreen", "0.22.0")
rec.save()
opened = []
real_open = OP.open_pack


def spy(path, log=None):
    opened.append(path)
    return real_open(path, log=log)


OP.open_pack = spy
try:
    SD.set_data_source("download")
    DD.get_data_for_dam(dam, data_folder=data, roles=["waterbodies"],
                        catalog=type("C", (), {"chosen": lambda self, r: None})())
    assert opened == [], opened
    SD.set_data_source("pack")
    r = DD.get_data_for_dam(dam, data_folder=data, roles=["waterbodies"], overwrite=True,
                            catalog=type("C", (), {"chosen": lambda self, r: None})())
    assert opened and r["done"]["waterbodies"]["source"].startswith("offline pack NHD_waterbodies"), r
finally:
    OP.open_pack = real_open
print("test_pack_0220: OK")
