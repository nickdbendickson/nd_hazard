"""0.22.1: with the user's own DWR layer in the DWR slot, the suite's NID dams (offline pack, nid/
folder or download) are clipped to that layer's dams padded by workflow.NID_PAD_MI (and the chosen
counties), instead of every NID dam of the work area (statewide when the work folder has no dams).
Qt test doubles only (qgis_mock).

Run: PYTHONPATH=tests/qgis_mock:<parent of the plugin> python3 tests/test_nid_clip_0221.py
"""
import json, os, shutil, sys, tempfile

os.environ["HOME"] = tempfile.mkdtemp(prefix="nid0221_home_")
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

T = tempfile.mkdtemp(prefix="nid0221_")


def pt(x, y, **props):
    return {"type": "Feature", "geometry": {"type": "Point", "coordinates": [x, y]},
            "properties": props}


# NID: one dam at the user's DWR dam (Raleigh), one 0.3 mi away, two far away (Garrison, Fargo)
NID = [pt(-101.36937, 46.34799, NIDID="ND00098", NAME="Raleigh"),
       pt(-101.36937 + 0.3 / 48.0, 46.34799, NIDID="ND00999", NAME="Near"),
       pt(-101.43, 47.50, NIDID="ND00137", NAME="Garrison"),
       pt(-96.80, 46.88, NIDID="ND00500", NAME="Fargo")]
pack = os.path.join(T, "ND_Dam_Suite_Pack")
m = GP.new_manifest(OP.ND_BBOX, created_by="test_nid_clip_0221")
GP.add_dataset(pack, m, "NID_dams", lambda: iter(NID), {"label": "NID", "source": "test"})
GP.add_dataset(pack, m, "DWR_dams", lambda: iter([pt(-101.36937, 46.34799, structure_id=2710),
                                                  pt(-96.8, 46.88, structure_id=1)]),
               {"label": "DWR", "source": "test"})
GP.write_manifest(pack, m)
data = os.path.join(T, "suite_data")
SD.ensure_layout(data)
SD.set_data_folder(data)
SD.set_pack_location(pack)
assert SD.active_pack() == pack

# the user's own DWR layer: one dam
dwr = os.path.join(T, "raleigh_dwr.gpkg")
DD.write_features(dwr, "dwr", [pt(-101.36937, 46.34799, structure_id=2710)], "test")
S = {"dwr": dwr}


def names(v):
    p, _, lay = v.partition("|layername=")
    return sorted(a.get("NAME") for _g, a in G.read_features(p, lay or G.list_layers(p)[0]["name"]))


# ---- the box: the DWR dams padded by NID_PAD_MI ---------------------------------------------
b = wf.nid_clip_box(S)
assert b and b[0] < -101.36937 < b[2] and b[1] < 46.34799 < b[3], b
assert (b[3] - b[1]) * 69.0 < 2 * wf.NID_PAD_MI + 0.05, b       # about 1 mi tall
assert wf.nid_clip_box({}) is None and wf.nid_clip_box({"dwr": os.path.join(T, "none.gpkg")}) is None
# a county away from the DWR dams: nothing in common, no clip
assert wf.nid_clip_box(dict(S, counties="Cass")) is None
cb = wf.nid_clip_box(dict(S, counties="Grant"))
assert cb == b, (cb, b)

# ---- the pack: only the NID dams near the DWR dam -------------------------------------------
work = os.path.join(T, "work")
os.makedirs(work)
log = []
v, origin = wf.slot_origin("nid", work, S, prepare=True, log=log.append)
assert origin == "suite pack", (origin, log)
assert names(v) == ["Near", "Raleigh"], (names(v), log)
assert any("within 0.5 mi of your DWR" in x for x in log), log
box = json.load(open(os.path.join(work, "0_suite_data", "nid.gpkg.box.json")))
assert box["from"] == "pack" and tuple(box["box"]) == b, box
# again: kept (not written again)
t0 = os.path.getmtime(os.path.join(work, "0_suite_data", "nid.gpkg"))
log = []
wf.slot_origin("nid", work, S, prepare=True, log=log.append)
assert not any("written to" in x for x in log), log
assert os.path.getmtime(os.path.join(work, "0_suite_data", "nid.gpkg")) == t0

# ---- no DWR layer: the work area as before (statewide here: the work folder has no dams) ----
v, origin = wf.slot_origin("nid", work, {}, prepare=True)
assert origin == "suite pack" and names(v) == ["Fargo", "Garrison", "Near", "Raleigh"], names(v)
# and back to the DWR layer: the statewide clip covers the box but is written again (exact)
v, origin = wf.slot_origin("nid", work, S, prepare=True)
assert names(v) == ["Near", "Raleigh"], names(v)
# other slots keep the work area even with the DWR layer chosen
v, origin = wf.slot_origin("dwr", work, S, prepare=True)
assert origin == "chosen" and v == dwr

# ---- no pack: the nid/ folder file, clipped the same way ------------------------------------
SD.set_pack_location("")
os.makedirs(os.path.join(data, "nid"), exist_ok=True)
nidf = os.path.join(data, "nid", "NID_dams.gpkg")
DD.write_features(nidf, "NID_dams", NID, "test")
assert SD.active_pack() == "" and wf.nid_dropin(data) == nidf
work2 = os.path.join(T, "work2")
os.makedirs(work2)
log = []
v, origin = wf.slot_origin("nid", work2, S, prepare=True, log=log.append)
assert origin == "nid folder" and v.endswith("0_suite_data/nid.gpkg|layername=nid"), (v, log)
assert names(v) == ["Near", "Raleigh"], names(v)
v, origin = wf.slot_origin("nid", work2, {}, prepare=True)
assert origin == "nid folder" and v == nidf                      # no DWR layer: the whole file

# ---- Download: fetched for the DWR box only ---------------------------------------------------
SD.set_pack_location(pack)
SD.set_data_source("download")
calls = []


def fake_fetch(role, box, catalog=None, http_get=None, cancel=None):
    calls.append((role, tuple(box)))
    return ([f for f in NID if DD._touches(f["geometry"], box)], "Test NID server", "Test")


real = (DD.fetch_role, wf._download_possible)
DD.fetch_role = fake_fetch
try:
    work3 = os.path.join(T, "work3")
    os.makedirs(work3)
    v, origin = wf.slot_origin("nid", work3, S, prepare=True)
    assert origin == "download" and calls and calls[-1] == ("nid_dams", b), (origin, calls)
    assert names(v) == ["Near", "Raleigh"], names(v)
finally:
    DD.fetch_role, wf._download_possible = real
    SD.set_data_source("pack")
shutil.rmtree(T, ignore_errors=True)
print("test_nid_clip_0221: OK")
