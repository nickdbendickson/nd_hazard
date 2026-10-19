"""0.16.0: the suite data in ND Dam Breach Screening - where an empty data slot falls back to (the dam
folder, the suite offline pack clipped to the work area, dwr/), the terrain requests through the
shared tile cache (fake fetcher, no GDAL), tool 20's file names and dam folder, the Runner hand-off's
data paths in dam.json, the provider's data-aware tools and getdamdata, and the DEM box.

Run: PYTHONPATH=tests/qgis_mock:<parent of the plugin> python3 tests/test_suite_data_016.py
Nothing here runs in a real QGIS, GDAL or network.
"""
import json, os, shutil, sys, tempfile

# a home folder and suite settings of its own (no ~/.ndsuite/config.json of another run)
os.environ["HOME"] = tempfile.mkdtemp(prefix="sd016_home_")
for _k in ("NDSUITE_DATA", "NDSUITE_DAMS"):
    os.environ.pop(_k, None)

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(HERE)
PARENT = os.path.dirname(PLUGIN)
sys.path.insert(0, os.path.join(HERE, "qgis_mock"))
sys.path.insert(0, PARENT)
sys.path.insert(0, HERE)

from nd_dam_screening.core import workflow as wf  # noqa: E402
from nd_dam_screening.core import terrain  # noqa: E402
from nd_dam_screening.core import damrecord_io as dri  # noqa: E402
from nd_dam_screening.damsuite import dam_record as DR  # noqa: E402
from nd_dam_screening.damsuite import damdata as DD  # noqa: E402
from nd_dam_screening.damsuite import offline_pack as OP  # noqa: E402
from nd_dam_screening.damsuite import suitedata as SD  # noqa: E402
from nd_dam_screening.damsuite import suitegpkg as G  # noqa: E402
from nd_dam_screening.damsuite import terrain_cache as TC  # noqa: E402

tmp = tempfile.mkdtemp(prefix="sd016_")
SD.CONFIG = os.path.join(tmp, "cfg", "config.json")


def make_pack(folder, sets):
    """An unpacked offline pack: sets = {name: [features]}."""
    os.makedirs(folder, exist_ok=True)
    w = OP.PackWriter(os.path.join(folder, OP.DB_NAME), OP.ND_BBOX, created_by="test")
    for name, feats in sets.items():
        w.begin(name, name, "https://example/" + name)
        w.add(feats, lambda f: json.dumps(f, sort_keys=True))
        w.finish()
    man = w.manifest()
    w.close(finalize=True)
    with open(os.path.join(folder, OP.MANIFEST_NAME), "w") as f:
        json.dump(man, f)
    return folder


def poly(lon, lat, d=0.002, **p):
    ring = [[lon, lat], [lon + d, lat], [lon + d, lat + d], [lon, lat + d], [lon, lat]]
    return {"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [ring]}, "properties": p}


def pt(lon, lat, **p):
    return {"type": "Feature", "geometry": {"type": "Point", "coordinates": [lon, lat]}, "properties": p}


def count(ref):
    path, _, lay = ref.partition("|")
    return sum(1 for _ in G.read_features(path, lay.replace("layername=", "")))


def dam_folder(root, dam_id, lon=-100.7, lat=47.1):
    f = os.path.join(root, dam_id)
    os.makedirs(f, exist_ok=True)
    rec = DR.load(f, create=True)
    rec.set("dam_id", dam_id, "ndscreen", "0.16.0")
    rec.set("lon", lon, "ndscreen", "0.16.0")
    rec.set("lat", lat, "ndscreen", "0.16.0")
    rec.save()
    return f


# ---------------------------------------------------------------------------------------------
# 1. slot fallback order: chosen > tool-0 package > dam folder > offline pack > dwr/
data = os.path.join(tmp, "data")
SD.ensure_layout(data)
make_pack(os.path.join(data, "nd_offline_pack"), {
    "NHD_waterbodies": [poly(-100.70, 47.10, name="near"), poly(-97.0, 48.8, name="far")],
    "NHD_flowlines": [{"type": "Feature", "properties": {},
                       "geometry": {"type": "LineString", "coordinates": [[-100.7, 47.1], [-100.6, 47.0]]}}],
    "USA_Structures": [poly(-100.69, 47.11, OCC_CLS="Residential")]})
with open(os.path.join(data, "dwr", "dwr_dams.geojson"), "w") as f:
    json.dump({"type": "FeatureCollection", "features": []}, f)
with open(os.path.join(data, "dwr", "dwr_dam_lines.geojson"), "w") as f:
    json.dump({"type": "FeatureCollection", "features": []}, f)
wf.suite_data_folder = lambda: data                       # the hook (no QGIS settings involved)

work = os.path.join(tmp, "work")
os.makedirs(work)
logs = []
# nothing chosen, no package, no dam folder: the pack (its data sets) and dwr/ (the others)
v, origin = wf.slot_origin("waterbodies", work, {})
assert origin == "suite pack" and v == os.path.join(work, wf.SUITE_DIR, "waterbodies.gpkg") + "|layername=waterbodies"
assert not os.path.exists(v.split("|")[0])                # only named: statuses() never writes files
assert wf.slot_source("waterbodies", work, {}, log=logs.append, prepare=True) == v
assert count(v) == 2                                       # no dams yet: the work area is the state
assert any("suite offline pack" in m for m in logs), logs
assert wf.slot_origin("dwr", work, {}) == (os.path.join(data, "dwr", "dwr_dams.geojson"), "dwr folder")
assert wf.slot_origin("dam_lines", work, {})[0].endswith("dwr_dam_lines.geojson")
assert wf.slot_origin("roads", work, {}) == (None, None) and wf.slot_source("nid", work, {}) is None
# the user's choice always wins
assert wf.slot_origin("waterbodies", work, {"waterbodies": "layer_7"}) == ("layer_7", "chosen")
assert wf.slot_origin("dwr", work, {"dwr": "my_dwr"}) == ("my_dwr", "chosen")
# a step that needs waterbodies is no longer waiting for a layer
assert "no NHD waterbodies layer" not in [r for r in wf.statuses(work, {}) if r[0] == "3"][0][3]
# model-changing inputs of the flood steps are NOT filled from the suite data
d7 = wf.defaults("7", work, {})
assert "STREAMS" not in d7 and "DAMLINES" not in d7 and "NLCD" not in d7, d7
assert wf.defaults("6", work, {}).get("STREAMS") is None
assert wf.defaults("16", work, {})["WB"] == v               # a data input is
# the tool-0 package beats the suite data (and its standard fields are filled in)
import _gpkg  # noqa: E402
pwork = os.path.join(tmp, "pwork")
os.makedirs(pwork)
_gpkg.write(os.path.join(pwork, "0_data_package.gpkg"),
            {"waterbodies": ("POLYGON", [("name", "TEXT")], [])})
assert wf.slot_origin("waterbodies", pwork, {})[1] == "package"
assert wf.slot_origin("streams", pwork, {})[1] == "suite pack"
assert not wf._from_package("streams", pwork, {}) and wf._from_package("waterbodies", pwork, {})

# a one-dam run whose dam has a folder: the dam folder's data first
dams = os.path.join(tmp, "Dams")
df = dam_folder(dams, "DWR-5289")
rec = DR.load(df)
os.makedirs(os.path.join(df, "data"))
wb_file = os.path.join(df, "data", "waterbodies.gpkg")
DD.write_features(wb_file, "waterbodies", [poly(-100.70, 47.10)], "test")
dem_file = os.path.join(df, "data", "dem.vrt")
open(dem_file, "w").write("<VRTDataset/>")
DD.set_entry(rec, "waterbodies", wb_file + "|layername=waterbodies", "test")
DD.set_entry(rec, "dem", dem_file, "test")
rec.save()
one = wf.one_dam_work(work, "5289")
os.makedirs(one)
S1 = wf.one_dam_settings({"dams_folder": dams}, work, "5289")     # a bare DWR number finds DWR-5289
assert wf.run_dam_folder(one, S1) == df
logs = []
assert wf.slot_source("waterbodies", one, S1, log=logs.append) == wb_file + "|layername=waterbodies"
assert "dam folder" in logs[0], logs
# ... and the slots the dam folder has no data for: the pack, clipped to the dam's area
v1 = wf.slot_source("structures", one, S1, prepare=True)
assert v1.startswith(os.path.join(one, wf.SUITE_DIR)) and count(v1) == 1
v2 = wf.slot_source("streams", one, S1, prepare=True)
assert count(v2) == 1
box = wf.work_area(one, S1)
assert box[0] < -100.7 < box[2] and box[2] - box[0] < 1.5        # the dam +- 15 mi, not the state
# step 7b: the Runner's DEM from the dam folder; the user's DEM wins
logs = []
assert wf.defaults("7b", one, S1, log=logs.append)["DEM"] == dem_file and any("dam folder" in m for m in logs)
assert wf.defaults("7b", one, dict(S1, runner_dem="/my/dem.tif"))["DEM"] == "/my/dem.tif"
# a work folder that IS a dam folder
assert wf.run_dam_folder(df, {}) == df and wf.slot_origin("waterbodies", df, {})[1] == "dam folder"
# the pack is written again only when it is newer than the clip
clip = v1.split("|")[0]
assert wf.prepare_pack_slot("structures", one, S1, os.path.join(data, "nd_offline_pack"),
                            ["USA_Structures"], clip) is None
# no suite data folder: as 0.15
wf.suite_data_folder = lambda: ""
assert wf.slot_origin("waterbodies", os.path.join(tmp, "w2"), {}) == (None, None)
wf.suite_data_folder = lambda: data
print("OK (slot fallback: chosen > package > dam folder > offline pack > dwr/, logged)")

# ---------------------------------------------------------------------------------------------
# 2. terrain through the shared cache (fake fetcher; GDAL hidden: the VRT path)
sys.modules["osgeo"] = None                                # no GDAL in this part
calls = []


def fetcher():
    def fetch(tb, cell, size, out_path):
        calls.append((tb, cell))
        with open(out_path, "wb") as f:
            f.write(b"II*\x00" + b"\x00" * 64)
    return fetch


root = os.path.join(data, SD.TERRAIN)
setup = (root, {"id": "usgs_3dep_image", "name": "3DEP", "kind": "arcgis_image"}, fetcher)
out_dir = os.path.join(tmp, "req")
os.makedirs(out_dir)
ext = (-310000.0, 2600000.0, -300000.0, 2610000.0)       # 10 km at 10 m = 1000 cells: 1-4 tiles
got = terrain.request_from_cache(ext, 10.0, out_dir, "d1", setup=setup, log=logs.append)
assert got == os.path.join(out_dir, "d1_c.vrt") and os.path.isfile(got)
n1 = len(calls)
assert n1 >= 1
cdir = TC.cache_dir(root, "usgs_3dep_image", 10.0)
tiles = sorted(os.listdir(cdir))
assert tiles and all(t.startswith("t_") for t in tiles)
txt = open(got).read()
assert os.path.join(cdir, tiles[0]) in txt                 # absolute paths to the cached tiles
os.remove(got)                                             # what callers do: the cache survives
assert sorted(os.listdir(cdir)) == tiles
again = terrain.request_from_cache(ext, 10.0, out_dir, "d2", setup=setup)
assert len(calls) == n1 and os.path.isfile(again)          # nothing downloaded twice
# fetch_3dep goes through the cache when it is set up, and hands back one request file
orig = terrain.cache_setup
terrain.cache_setup = lambda *a, **k: setup
paths = terrain.fetch_3dep(ext, 10.0, out_dir, "d3")
assert paths == [os.path.join(out_dir, "d3_c.vrt")] and len(calls) == n1
terrain.cache_setup = orig
# cache_setup: no data folder / a server that is not an ImageServer or WCS -> the old download
assert terrain.cache_setup(data_folder="") is None


class Cat:
    def __init__(self, s): self.s = s
    def chosen(self, role): return self.s


assert terrain.cache_setup(data_folder=data, catalog=Cat({"id": "f", "kind": "file", "url": "x"})) is None
cs = terrain.cache_setup(data_folder=data, catalog=Cat({"id": "w", "kind": "wcs", "url": "https://x", "layer": "c"}))
assert cs[0] == os.path.join(data, "terrain") and cs[1]["id"] == "w"
# tool 20's download(): the VRT named by the caller points at the cache, nothing copied
del sys.modules["osgeo"]
from nd_dam_screening.core import dem_download as dd  # noqa: E402  (the mock GDAL is fine here)
terrain.cache_setup = lambda *a, **k: setup
vrt = dd.download(ext, 10.0, os.path.join(tmp, "dl"), "Cass_dem_10m")
assert vrt == os.path.join(tmp, "dl", "Cass_dem_10m.vrt") and os.listdir(os.path.join(tmp, "dl")) == ["Cass_dem_10m.vrt"]
terrain.cache_setup = orig
print("OK (terrain: one download per tile, request files only handed out)")

# ---------------------------------------------------------------------------------------------
# 3. tool 20: names, dam folder, dam.json
assert dd.output_stem(0, 10.0, "DWR-5289") == "DWR-5289_dem_10m"
assert dd.output_stem(0, 2.5, "", point=(-97.46396, 47.31054)) == "dam_m97p4640_47p3105_dem_2p5m"
assert dd.output_stem(1, 10.0, county="Grand Forks") == "Grand_Forks_dem_10m"
assert dd.output_stem(2, 30.0) == "north_dakota_dem_30m"
assert dd.output_stem(0, 10.0, "A") != dd.output_stem(0, 10.0, "B")       # no more shared dam.vrt
from qgis.core import QgsProcessingContext, QgsProcessingFeedback  # noqa: E402
from nd_dam_screening.algorithms import download_dem as DDA  # noqa: E402
seen = {}


def fake_download(extent, cell, out, stem, feedback=None):
    seen.update(extent=extent, cell=cell, out=out, stem=stem)
    os.makedirs(out, exist_ok=True)
    p = os.path.join(out, stem + ".vrt")
    open(p, "w").write("<VRTDataset/>")
    return p


dd.download, dd.point_box = fake_download, (lambda lon, lat, mi: (lon, lat, lon + 1, lat + 1))
# 0.24.0 reprojects the downloaded VRT to a local GeoTIFF; the raster mock cannot warp a VRT, so
# stub warp_to_local to emit the dest .tif (dam.json recording is still exercised). Real GDAL
# covers the warp/standardisation in test_dem_local_024.py.
dd.warp_to_local = lambda src, dest, cell_m, feedback=None: (open(dest, "w").write("tif"), dest)[1]
d2 = dam_folder(dams, "DWR-77", lon=-99.5, lat=47.5)
alg = DDA.DownloadDem()
alg.initAlgorithm()
fb = QgsProcessingFeedback()
r = alg.run_mock({"SOURCE": 0, "SCOPE": 0, "CELL": 10.0, "BUFFER_MI": 10.0, "DAMS_FOLDER": dams,
                  "DAM_ID": "DWR-77", "OUT": os.path.join(tmp, "elsewhere")}, QgsProcessingContext(), fb)
assert seen["out"] == os.path.join(d2, "data") and seen["stem"] == "DWR-77_dem_10m"
assert seen["extent"][:2] == (-99.5, 47.5)                 # the dam point came from dam.json
assert r["VRT"] == os.path.join(d2, "data", "DWR-77_10m_5070.tif")
assert DD.dam_data(d2)["dem"] == r["VRT"]
assert DR.load(d2).extra["data"]["dem"]["path"] == "data/DWR-77_10m_5070.tif"
assert any("dam point from dam.json" in m for m in fb.log)
# no dam folder: the output folder and a county name
r = alg.run_mock({"SOURCE": 0, "SCOPE": 0, "CELL": 10.0, "BUFFER_MI": 10.0, "DAM_POINT": "-97.5,47.3",
                  "DAM_ID": "X-1", "OUT": os.path.join(tmp, "elsewhere")}, QgsProcessingContext(), fb)
assert r["VRT"] == os.path.join(tmp, "elsewhere", "X-1_10m_5070.tif")
print("OK (tool 20: dam / county names, dam folder and dam.json)")

# ---------------------------------------------------------------------------------------------
# 4. the Runner hand-off records the data it used, only where the data section has no entry
d3 = os.path.join(dams, "DWR-9")
os.makedirs(d3)
roads = os.path.join(tmp, "roads.gpkg")
DD.write_features(roads, "roads", [], "t")
dem = os.path.join(tmp, "nd_dem.vrt")
open(dem, "w").write("<VRTDataset/>")
pre = DR.load(d3, create=True)
DD.set_entry(pre, "roads", "/somewhere/else.gpkg", "Get data for this dam")
pre.save()
path, log = dri.write(d3, {"dam_id": "DWR-9"}, {"triage": "needs 2D"}, "0.16.0",
                      data={"dem": dem, "roads": roads + "|layername=roads", "rail": "mem3",
                            "structures": os.path.join(tmp, "missing.gpkg")})
sec = DR.load(d3).extra["data"]
assert sec["dem"]["path"] == dem and "7b" in sec["dem"]["source"]
assert sec["roads"]["path"] == "/somewhere/else.gpkg"      # an entry already there is kept
assert "rail" not in sec and "structures" not in sec        # not files: not recorded
assert any("DEM recorded" in m for m in log), log
# without data: as before (no data section written)
d4 = os.path.join(dams, "DWR-10")
dri.write(d4, {"dam_id": "DWR-10"}, {"triage": "clear"}, "0.16.0")
assert "data" not in DR.load(d4).extra
print("OK (7b hand-off: data paths in dam.json, never over an entry)")

# ---------------------------------------------------------------------------------------------
# 5. the provider: data-aware tools name the dam by ONLY_IDS; Get data for this dam is a tool
from nd_dam_screening import provider as prov  # noqa: E402
FP = [c for c in prov.ALGORITHMS if c().name() == "findponds"][0]
a = FP()
a.initAlgorithm()
from nd_dam_screening import suite_data_tools as SDT  # noqa: E402
_wb = a.parameterDefinition("WB")                          # optional in the dialog: the dam folder may fill it
assert _wb.optional or (SDT._optional_flag() is not None and _wb.flags() & SDT._optional_flag())
os.environ["NDSUITE_DAMS"] = dams
assert prov.dam_folder_of(a, {"ONLY_IDS": "DWR-5289"}) == df
assert prov.dam_folder_of(a, {"ONLY_IDS": "5289"}) == df
assert prov.dam_folder_of(a, {"ONLY_IDS": "DWR-5289, DWR-77"}) == ""      # not one dam
os.environ.pop("NDSUITE_DAMS")
params, notes, folder = __import__("nd_dam_screening.suite_data_tools", fromlist=["x"]).fill_parameters(
    a, {"ONLY_IDS": "DWR-5289", "DAMS_FOLDER": dams}, None, FP._nd_data_mapping, folder_of=prov.dam_folder_of)
assert folder == df and params["WB"] == wb_file + "|layername=waterbodies", (params, notes)
assert prov.GET_DATA().name() == "getdamdata" and prov.GET_DATA().group() == "Screening steps"
print("OK (provider: data-aware tools 1 and 16, getdamdata)")

# ---------------------------------------------------------------------------------------------
# 6. the DEM box: resolution combobox (0=10m, 1=1m), a dam ID with a folder, values remembered
from qgis import processing as qproc  # noqa: E402
from qgis.core import QgsSettings  # noqa: E402
from nd_dam_screening.dialog import NDDamScreeningDialog  # noqa: E402
os.environ["NDSUITE_DAMS"] = dams
dlg = NDDamScreeningDialog()
assert dlg.dem_resolution.currentIndex() == 0              # default 10 m
dlg.dem_dam_id.setText("DWR-77")
dlg.dem_scope.setCurrentIndex(2)                           # state: asks first (answered Yes)
del qproc.DIALOGS[:]
sent = dlg._open_dem_download()
assert qproc.DIALOGS and qproc.DIALOGS[-1][0] == "nddam:downloaddem"
assert sent["RESOLUTION"] == 0 and sent["DAM_ID"] == "DWR-77" and sent["DAMS_FOLDER"] == dams and "OUT" not in sent
assert QgsSettings().value("ndscreen/dem_box/dam_id") == "DWR-77"
dlg.dem_resolution.setCurrentIndex(1)                      # switch to 1 m
dlg._save_dem_box()
dlg2 = NDDamScreeningDialog()                              # next session: the box's values are back
assert dlg2.dem_resolution.currentIndex() == 1 and dlg2.dem_dam_id.text() == "DWR-77"
assert QgsSettings().value("ndscreen/dem_box/scope") == 2
# a dam ID without a folder and no output folder: asked for a folder, nothing opened
dlg2.dem_dam_id.setText("NOPE-1")
dlg2.dem_out.setText("")
del qproc.DIALOGS[:]
assert dlg2._open_dem_download() is None and not qproc.DIALOGS
os.environ.pop("NDSUITE_DAMS")
print("OK (DEM box: 10 m, dam folder, remembered values)")

shutil.rmtree(tmp, ignore_errors=True)
print("all checks passed")
