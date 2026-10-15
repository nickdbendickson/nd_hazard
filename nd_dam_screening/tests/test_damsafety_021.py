"""0.21.0 (ideas from the DamSafety plugins audit): the window's "Fill empty layers from the suite
data" (workflow.fill_empty_slots), the 3DEP tile size, and tool 7's pre-run check helpers
(damsuite.solvegate, TerrainLoader.covers_point).

Run: PYTHONPATH=tests/qgis_mock:<parent of the plugin> python3 tests/test_damsafety_021.py
Nothing here runs in a real QGIS or network.
"""
import json, os, shutil, sys, tempfile

os.environ["HOME"] = tempfile.mkdtemp(prefix="ds021_home_")
for _k in ("NDSUITE_DATA", "NDSUITE_DAMS"):
    os.environ.pop(_k, None)

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(HERE, "qgis_mock"))
sys.path.insert(0, os.path.dirname(PLUGIN))
sys.path.insert(0, HERE)

from nd_dam_screening.core import workflow as wf  # noqa: E402
from nd_dam_screening.core import terrain  # noqa: E402
from nd_dam_screening.damsuite import offline_pack as OP  # noqa: E402
from nd_dam_screening.damsuite import solvegate as SG  # noqa: E402
from nd_dam_screening.damsuite import suitedata as SD  # noqa: E402

tmp = tempfile.mkdtemp(prefix="ds021_")
SD.CONFIG = os.path.join(tmp, "cfg", "config.json")


def poly(lon, lat, d=0.002, **p):
    ring = [[lon, lat], [lon + d, lat], [lon + d, lat + d], [lon, lat + d], [lon, lat]]
    return {"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [ring]}, "properties": p}


def make_pack(folder, sets):
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


# ------------------------------------------------------------------ 1. fill empty slots
data = os.path.join(tmp, "data")
SD.ensure_layout(data)
make_pack(os.path.join(data, "nd_offline_pack"), {
    "NHD_waterbodies": [poly(-100.70, 47.10, name="near")],
    "NHD_flowlines": [{"type": "Feature", "properties": {},
                       "geometry": {"type": "LineString", "coordinates": [[-100.7, 47.1], [-100.6, 47.0]]}}],
    "USA_Structures": [poly(-100.69, 47.11, OCC_CLS="Residential")]})
for name in ("dwr_dams.geojson", "dwr_dam_lines.geojson"):
    with open(os.path.join(data, "dwr", name), "w") as f:
        json.dump({"type": "FeatureCollection", "features": []}, f)
wf.suite_data_folder = lambda: data

work = os.path.join(tmp, "work")
os.makedirs(work)
logs = []
got = wf.fill_empty_slots(work, {}, log=logs.append)
assert got["dwr"] == (os.path.join(data, "dwr", "dwr_dams.geojson"), "dwr folder"), got
assert got["dam_lines"][0].endswith("dwr_dam_lines.geojson") and got["dam_lines"][1] == "dwr folder"
assert got["waterbodies"][1] == "suite pack" and got["structures"][1] == "suite pack"
assert os.path.exists(got["waterbodies"][0].split("|")[0])          # prepare=True wrote the clip
assert "streams" not in got and "nlcd" not in got                    # model inputs are never filled
assert "roads" not in got and "nid" not in got                       # nothing to fill them from
# a slot the user set is left alone; prepare=False only names the clip
got2 = wf.fill_empty_slots(work, {"dwr": "my_dwr_layer", "waterbodies": "layer_7"}, prepare=False)
assert "dwr" not in got2 and "waterbodies" not in got2 and got2["dam_lines"][1] == "dwr folder"
# no suite data folder: nothing to fill, no error
wf.suite_data_folder = lambda: ""
assert wf.fill_empty_slots(work, {}) == {}
wf.suite_data_folder = lambda: data
print("OK (fill empty slots: pack and dwr/, chosen slots kept, model inputs never filled)")

# ------------------------------------------------------------------ 2. 3DEP tile size
tiles = list(terrain.tile_extents(0.0, 0.0, 16093.0, 16093.0, 10.0))      # a 10-mile box at 10 m
assert SG.TILE_PX == 1200 and len(tiles) == 4, len(tiles)
assert all((t[2] - t[0]) / 10.0 <= 1200 and (t[3] - t[1]) / 10.0 <= 1200 for t in tiles)
assert len(list(terrain.tile_extents(0.0, 0.0, 16093.0, 16093.0, 10.0, max_px=4000))) == 1
print("OK (3DEP tiles of at most 1,200 px)")

# ------------------------------------------------------------------ 3. pre-run check helpers
assert SG.hydro_stats([0, 600, 1200], [0, 10, 0])["status"] == SG.OK
assert SG.hydro_stats([0, 600, 1200], [0, 0, 0])["status"] == SG.FAIL
msg = SG.message([SG.hydro_stats([0, 600, 1200], [0, 0, 0])])
assert msg.startswith("Stopped before the flood run (pre-run check)")
from nd_dam_screening.algorithms import run_breach_sim as rbs  # noqa: E402
assert rbs.SG is SG and hasattr(rbs, "_layer_overlap")
print("OK (pre-run check helpers)")

shutil.rmtree(tmp, ignore_errors=True)
print("ALL OK")
