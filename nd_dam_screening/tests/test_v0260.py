"""0.26.0: the last-resort inventory stage-storage curve (tool 3), the Run selected dams button
and the selected dam of the Results table (dialog.py, core/workflow.py), tool 7's WRITE_POLYGONS
switch, and the version / changelog.

Run: FAKE_QGIS_FLAVOR=3.40 PYTHONPATH=tests/qgis_mock:<parent of the plugin> python3 tests/test_v0260.py
Nothing here runs in a real QGIS (the Qt and Processing test doubles of tests/qgis_mock).
"""
import configparser
import json
import os
import sys
import tempfile

os.environ["HOME"] = tempfile.mkdtemp(prefix="v0260_home_")       # no suite config of another run
for _k in ("NDSUITE_DATA", "NDSUITE_DAMS"):
    os.environ.pop(_k, None)

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(HERE, "qgis_mock"))
sys.path.insert(0, os.path.dirname(PLUGIN))
sys.path.insert(0, HERE)

import numpy as np  # noqa: E402
import _gpkg  # noqa: E402
from osgeo import gdal  # noqa: E402
from qgis.core import (QgsGeometry, QgsPointXY, QgsProcessingContext, QgsProcessingFeedback,  # noqa: E402
                       make_layer)
from qgis.PyQt.QtWidgets import EVENTS, QLineEdit, QTableWidgetItem  # noqa: E402
from qgis import utils as qutils  # noqa: E402
import nd_dam_screening as pkg  # noqa: E402
from nd_dam_screening.core import storage as st, workflow as wf, preflight as pf  # noqa: E402
from nd_dam_screening.algorithms.estimate_storage import EstimateStorage  # noqa: E402
from nd_dam_screening.algorithms.run_breach_sim import RunBreachSim  # noqa: E402
from nd_dam_screening.damsuite import settings as DS  # noqa: E402
from nd_dam_screening import dialog as DLG  # noqa: E402

ROOT = tempfile.mkdtemp(prefix="v0260_")
AF, FT = st.AF_M3, st.FT_M
D, S = "double", "string"
pt = lambda x, y: QgsGeometry.fromPointXY(QgsPointXY(x, y))


def touch(*parts):
    p = os.path.join(*parts)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as fh:
        fh.write("x")
    return p


# ---------------------------------------------------------------- 1. the curve itself (pure)
dem = 500.0 + 0.01 * np.arange(40)[None, :] + 0.005 * np.arange(40)[:, None]      # a plane, no basin
r = st.inventory_curve(dem, (20, 20), 20 * FT, 5240 * AF, "maximum")
assert r is not None and r.confidence == "low" and len(r.curve) == 11
bed = float(np.median(dem[18:23, 18:23]))
assert abs(r.pool_elev - bed) < 1e-9 and abs(r.crest_elev - (bed + 20 * FT)) < 1e-9
assert r.curve[0] == (bed, 0.0) and abs(r.curve[-1][1] - 5240 * AF) < 1e-6 and abs(r.curve[-1][0] - r.crest_elev) < 1e-9
assert abs(r.curve[5][1] - 0.25 * 5240 * AF) < 1e-6                 # quadratic: a quarter at half height
assert r.total_m3 == r.above_pool_m3 == 5240 * AF and r.below_pool_m3 == 0.0 and r.surface_area_m2 == 0.0
assert abs(r.max_depth_m - 20 * FT) < 1e-12 and r.shore_slope_deg == 0.0
assert r.notes[0].startswith("ASSUMPTION: no basin in the DEM within the search distance; stage-storage curve "
                             "assumed from the inventory maximum storage 5,240 ac-ft and height 20.0 ft (quadratic); "
                             "verify the location and the DWR values"), r.notes
assert "inventory normal storage" in st.inventory_curve(dem, (20, 20), 3.0, 1000.0, "normal").notes[0]
# refused: no height / too low, no volume, no ground, a point off the DEM
assert st.inventory_curve(dem, (20, 20), None, 1000.0) is None
assert st.inventory_curve(dem, (20, 20), 0.5, 1000.0) is None
assert st.inventory_curve(dem, (20, 20), 3.0, 0.0) is None
assert st.inventory_curve(np.full((40, 40), np.nan), (20, 20), 3.0, 1000.0) is None
assert st.inventory_curve(dem, (-3, 20), 3.0, 1000.0) is None and st.inventory_curve(dem, (20, 40), 3.0, 1000.0) is None
nan_dem = dem.copy(); nan_dem[19:22, 19:22] = np.nan                 # NaN ignored in the 5 x 5 median
assert st.inventory_curve(nan_dem, (20, 20), 3.0, 1000.0) is not None
print("OK (inventory_curve)")

# ---------------------------------------------------------------- 2. tool 3 on a DEM with no basin
work = os.path.join(ROOT, "t3")
demdir = os.path.join(work, "dem")
os.makedirs(demdir)
N, CELL = 60, 10.0
plane = 500.0 + 0.01 * np.arange(N)[None, :] * CELL + 0.005 * np.arange(N)[:, None] * CELL
for did in ("DWR-1", "DWR-2", "DWR-3"):
    ds = gdal.GetDriverByName("GTiff").Create(os.path.join(demdir, "dam_{}.tif".format(did)), N, N, 1, 6)
    ds.SetGeoTransform((0.0, CELL, 0.0, N * CELL, 0.0, -CELL))
    ds.GetRasterBand(1).WriteArray(plane)
    ds.FlushCache()
F = [("dam_id", S), ("height_m", D), ("inv_max_m3", D), ("inv_norm_m3", D), ("stor_src", S), ("h_src", S)]
centre = pt(N * CELL / 2.0, N * CELL / 2.0)
dams = make_layer(F, [
    (centre, dict(dam_id="DWR-1", height_m=20 * FT, inv_max_m3=5240 * AF, inv_norm_m3=3000 * AF)),
    (centre, dict(dam_id="DWR-2", height_m=None, inv_max_m3=5240 * AF)),           # no height: fails as before
    (centre, dict(dam_id="DWR-3", height_m=12 * FT, inv_max_m3=None, inv_norm_m3=800 * AF))], "point", "dams")
wbs = make_layer([("gnis_name", S)], [], "polygon", "wb")                           # no water body anywhere
ctx, fb = QgsProcessingContext(), QgsProcessingFeedback()
curves = os.path.join(work, "curves")
res = EstimateStorage().run_mock(dict(DAMS=ctx.add(dams), WB=ctx.add(wbs), DEMDIR=demdir, SEARCH_FT=500.0,
                                      SHAPE=1.0, CURVES=curves, OUT="memory:"), ctx, fb)
out = {str(f["dam_id"]): f for f in ctx.layers[res["OUT"]].getFeatures()}
f1 = out["DWR-1"]
assert f1["stor_conf"] == "low" and f1["stor_method"] == "inventory curve", (f1["stor_conf"], f1["stor_method"])
assert f1["stor_src"] == "inventory (assumed curve)"
assert str(f1["stor_notes"]).startswith("ASSUMPTION: no basin in the DEM within the search distance"), f1["stor_notes"]
assert "maximum storage 5,240 ac-ft and height 20.0 ft" in f1["stor_notes"]
assert "DWR maximum storage" not in f1["stor_notes"] and "CHECK" not in f1["stor_notes"]   # no rescale: the top is the inventory
assert abs(f1["est_total_m3"] / AF - 5240) / 5240 < 0.01 and abs(f1["stor_used_m3"] / AF - 5240) / 5240 < 0.01
assert abs(f1["est_crest_m"] - f1["est_pool_m"] - 20 * FT) < 1e-9 and f1["est_area_m2"] == 0.0 and f1["est_below_m3"] == 0.0
assert f1["h_src"] is None and f1["est_h_m"] is None                               # the height was given, not estimated
with open(os.path.join(curves, "curve_DWR-1.csv")) as fh:
    rows = [l.strip().split(",") for l in fh.read().strip().splitlines()]
assert rows[0] == ["elev_m", "storage_m3", "elev_ft", "storage_acft"] and len(rows) == 12
assert abs(float(rows[-1][3]) - 5240) / 5240 < 0.01 and float(rows[1][1]) == 0.0
assert abs(float(rows[-1][2]) - float(rows[1][2]) - 20.0) < 0.05
assert any("DWR-1" in w and w.split(": ", 1)[1].startswith("ASSUMPTION") for w in fb.warnings), fb.warnings
# no height: FAILED as before, the reason kept
f2 = out["DWR-2"]
assert f2["stor_method"] == "none" and str(f2["stor_notes"]).startswith("FAILED") and \
    "no waterbody within search distance" in f2["stor_notes"], f2["stor_notes"]
assert not os.path.exists(os.path.join(curves, "curve_DWR-2.csv"))
assert any("DWR-2" in e for e in fb.errors)
# normal storage only: the note says so
f3 = out["DWR-3"]
assert "inventory normal storage 800 ac-ft and height 12.0 ft" in f3["stor_notes"] and f3["stor_method"] == "inventory curve"
assert abs(f3["est_total_m3"] / AF - 800) / 800 < 0.01 and f3["stor_conf"] == "low"
print("OK (tool 3: assumed inventory curve; no height still fails)")

# ---------------------------------------------------------------- 3. workflow helpers for chosen dams
assert wf.only_ids_text(["DWR-5289", " DWR-5290 ", "", None, "dwr-5289"]) == "DWR-5289, DWR-5290"
assert wf.only_ids_text([]) == "" and wf.only_ids_text(None) == ""
assert wf.selected_dams_name(["DWR-1", "DWR-2"]) == "DWR-1_DWR-2" and wf.selected_dams_name([]) == ""
assert wf.selected_dams_name(["DWR-1", "DWR-2", "DWR-3", "DWR-4", "DWR-5"]) == "DWR-1_and_4_more"
assert wf.selected_dams_name(["POND 7/a"]) == "POND_7_a"
S1 = wf.one_dam_settings({"only_ids": "KEEP ME"}, "/w", wf.only_ids_text(["A", "B"]))
assert S1["only_ids"] == "A, B" and S1["package_work"] == "/w"
# the IDs are what tools 1 / 16 split on commas
assert {x.strip().upper() for x in S1["only_ids"].split(",") if x.strip()} == {"A", "B"}
W = os.path.join(ROOT, "w1")
os.makedirs(W)
root, row = wf.dam_row_for(W, {}, "DWR-5289")
assert root == os.path.join(W, "Dams") and row == {"dam_id": "DWR-5289", "folder": os.path.join(W, "Dams", "DWR-5289")}
assert wf.dam_row_for(W, {}, "") == ("", None)
touch(W, "Dams", "DWR-5289", "dam.json")
assert wf.dam_row_for(W, {}, "5289")[1]["folder"] == os.path.join(W, "Dams", "DWR-5289")      # a bare DWR number
other = os.path.join(ROOT, "elsewhere")
touch(other, "DWR-7", "dam.json")
assert wf.dam_row_for(W, {"dams_folder": other}, "DWR-7") == (other, {"dam_id": "DWR-7", "folder": os.path.join(other, "DWR-7")})
print("OK (only_ids_text, selected_dams_name, dam_row_for)")

# ---------------------------------------------------------------- 4. the window: Run selected dams...
pf.Env.url_ok = lambda self, url: (True, "stubbed")
plug = pkg.classFactory(None)
plug.initGui()
algs = {a.name(): a for a in plug.provider.algs}
for a in algs.values():
    a.initAlgorithm()
from qgis import processing  # noqa: E402
processing.REGISTRY.update(algs)
DS.set_current_path("", remember=True)
dlg = DLG.NDDamScreeningDialog()
dlg.work_edit.setText(W)
dlg._changed()
assert dlg.sel_dams_btn._text == "Run selected dams..." and dlg.one_dam_btn._text == "Run one dam..."
DF = [("dam_id", "TEXT"), ("name", "TEXT")]
_gpkg.write(wf.path(W, "1", "OUT"), {"dams": ("Point", DF, [
    (("Point", (-100.1, 47.1)), {"dam_id": "DWR-5289", "name": "Alpha"}),
    (("Point", (-100.2, 47.2)), {"dam_id": "DWR-5290", "name": "Beta"}),
    (("Point", (-100.3, 47.3)), {"dam_id": "DWR-5291", "name": "Gamma"})])})
choices = wf.dam_choices(W, dlg.settings(), dlg._read_rows)
assert choices == [("DWR-5289", "DWR-5289 Alpha"), ("DWR-5290", "DWR-5290 Beta"), ("DWR-5291", "DWR-5291 Gamma")]
# the picker: ticks give IDs, All / None
picker = DLG._DamPicker(dlg, choices)
assert picker.checked_ids() == [] and [d for d, _c in picker.boxes] == [d for d, _l in choices]
picker.boxes[1][1].setChecked(True)
picker.boxes[2][1].setChecked(True)
assert picker.checked_ids() == ["DWR-5290", "DWR-5291"]
picker.set_all(True)
assert picker.checked_ids() == ["DWR-5289", "DWR-5290", "DWR-5291"]
picker.set_all(False)
assert picker.checked_ids() == []
assert dlg._pick_dams(choices) == []                   # the test double's exec() accepts nothing

calls = []


def fake_start(step, aid, params, on_done):
    calls.append((step, aid, dict(params), params.get("ONLY_IDS")))
    work1 = os.path.dirname(params.get("OUT") or params.get("OUTDIR") or params.get("OUTP") or params.get("WORK") or "")
    for param, rel in wf.LAYOUT[step].items():
        if param == "DAMS_FOLDER":
            continue
        pth = os.path.join(work1, rel)
        if os.path.splitext(pth)[1]:
            touch(pth)
        else:
            os.makedirs(pth, exist_ok=True)
            touch(pth, "x_%s.dat" % step)
    if step == "10":
        touch(work1, "10_reports", "index.html")
    on_done(True, "")


dlg._start_algorithm = fake_start
dlg._definitions = lambda aid: []
# the layers the chain needs (files), as test_popup_013 gives them
dwr = _gpkg.write(os.path.join(W, "dwr.gpkg"), {"dams": ("Point", [("structure_", "INTEGER"), ("dam_name", "TEXT")],
                                                 [(("Point", (0.0, 0.0)), {"structure_": 5289, "dam_name": "Alpha"})])})
lines_p = _gpkg.write(os.path.join(W, "lines.gpkg"), {"l": ("LineString", [("structure_id", "INTEGER")],
                                                        [(("LineString", [(0, 0), (1, 1)]), {"structure_id": 5289})])})
wb = _gpkg.write(os.path.join(W, "wb.gpkg"), {"wb": ("Polygon", [("gnis_name", "TEXT")],
                                               [(("Polygon", [[(0, 0), (1, 0), (1, 1), (0, 0)]]), {})])})
bld = _gpkg.write(os.path.join(W, "bld.gpkg"), {"b": ("Point", [("occtype", "TEXT"), ("bldgtype", "TEXT")],
                                                 [(("Point", (0, 0)), {})])})
for slot, path in (("dwr", dwr), ("dam_lines", lines_p), ("waterbodies", wb), ("structures", bld)):
    cb = dlg.layer_combo[slot]
    cb.addItem(path, path)
    cb.setCurrentIndex(cb.count() - 1)
before = dict(dlg.settings())
# nothing ticked: nothing happens
dlg._pick_dams = lambda ch: []
assert dlg._run_selected_dams() is False and not calls and dlg.chain is None
# two dams ticked: one run, ONLY_IDS = the IDs joined by ', ', its own folder, the window untouched
dlg._pick_dams = lambda ch: ["DWR-5290", "DWR-5289"]
assert dlg._run_selected_dams() is True and not dlg.chain.active and dlg.chain.ok, dlg.chain.result
folder = wf.one_dam_work(W, "DWR-5290_DWR-5289")
assert [c[0] for c in calls] == ["1", "2", "3", "4", "5", "7", "8", "10"]
assert all(c[3] == "DWR-5290, DWR-5289" for c in calls if c[0] in ("1", "16")), [c[3] for c in calls]
assert calls[0][2]["OUT"].startswith(folder) and os.path.isfile(os.path.join(folder, wf.REPORT_INDEX))
assert all(c[2].get(k, folder).startswith(folder) for c in calls for k in ("OUT", "OUTDIR") if k in c[2])
assert dlg.settings() == before and dlg.settings().get("only_ids", "") == before.get("only_ids", "")
assert ("DWR-5290_DWR-5289", folder) in wf.one_dam_runs(W)
# IDs given directly (no list), repeats dropped
del calls[:]
assert dlg._run_selected_dams(["DWR-5291", "DWR-5291"]) is True and calls[0][3] == "DWR-5291"
assert calls[0][2]["OUT"].startswith(wf.one_dam_work(W, "DWR-5291"))
# no work folder: a message, nothing run
dlg.work_edit.setText("")
EVENTS["messages"].clear()
del calls[:]
assert dlg._run_selected_dams(["DWR-5289"]) is False and not calls and "work folder" in EVENTS["messages"][-1][2]
dlg.work_edit.setText(W)
print("OK (Run selected dams)")

# ---------------------------------------------------------------- 5. the selected dam of the Results table
assert dlg._selected_dam_id() == ""
dlg.dam_table.setRowCount(2)
dlg.dam_table.setItem(0, 0, QTableWidgetItem("DWR-5289"))
dlg.dam_table.setItem(1, 0, QTableWidgetItem("DWR-5290"))
dlg.dam_table.setCurrentCell(1, 0)
assert dlg._selected_dam_id() == "DWR-5290"


class FakeRunnerWindow(object):
    def __init__(self):
        self.dams_edit, self.dam_edit, self.refreshed = QLineEdit(), QLineEdit(), 0

    def refresh(self):
        self.refreshed += 1


class FakeHazardWindow(object):
    def __init__(self):
        self.dam_folder_edit, self.refreshed = QLineEdit(), 0

    def refresh(self):
        self.refreshed += 1


class FakePlugin(object):
    def __init__(self, window):
        self.dialog, self.opened = window, 0

    def run_plugin(self):
        self.opened += 1
        return self.dialog


runner, hazard = FakePlugin(FakeRunnerWindow()), FakePlugin(FakeHazardWindow())
qutils.plugins["nd_breach_runner"], qutils.plugins["ND_Dam_Hazard_Helper"] = runner, hazard
dlg._open_runner()
assert runner.opened == 1 and runner.dialog.dams_edit.text() == os.path.join(W, "Dams")
assert runner.dialog.dam_edit.text() == "DWR-5290" and runner.dialog.refreshed == 1
dlg._open_hazard()
assert hazard.opened == 1 and hazard.dialog.dam_folder_edit.text() == os.path.join(W, "Dams", "DWR-5290")
assert hazard.dialog.refreshed == 1
# no row selected and no current one-dam run: the Runner gets the Dams folder when the chain made
# one, else the work folder; the Hazard Helper just opens
dlg.dam_table.setCurrentCell(-1, -1)
dlg.one_dam_combo.setCurrentIndex(-1)
assert dlg._selected_dam_id() == "" and dlg._selected_dam_id(fallback=True) == ""
runner.dialog.dam_edit.setText("")
dlg._open_runner()
dams_dir = os.path.join(W, "Dams")
assert runner.dialog.dams_edit.text() == (dams_dir if os.path.isdir(dams_dir) else W)
assert runner.dialog.dam_edit.text() == "" and runner.opened == 2
hazard.dialog.dam_folder_edit.setText("")
dlg._open_hazard()
assert hazard.dialog.dam_folder_edit.text() == "" and hazard.opened == 2
# no row selected but a single-dam run shown in the one-dam list: that dam stands in (0.26.1);
# a chosen-dams run (IDs joined by "_") does not
def _find_item(cb, text):
    try:
        for k in range(cb.count()):
            if cb.itemText(k) == text:
                return k
    except Exception:                                            # noqa
        pass
    return -1


single = _find_item(dlg.one_dam_combo, "DWR-5291")
if single >= 0:
    dlg.one_dam_combo.setCurrentIndex(single)
    assert dlg._selected_dam_id() == "" and dlg._selected_dam_id(fallback=True) == "DWR-5291"
    dlg._open_runner()
    assert runner.dialog.dam_edit.text() == "DWR-5291" and runner.dialog.dams_edit.text() == dams_dir
joined = _find_item(dlg.one_dam_combo, "DWR-5290_DWR-5289")
if joined >= 0:
    dlg.one_dam_combo.setCurrentIndex(joined)
    assert dlg._selected_dam_id(fallback=True) == ""
# the plugin missing: a message, no error
del qutils.plugins["nd_breach_runner"]
EVENTS["messages"].clear()
dlg._open_runner()
assert EVENTS["messages"][-1][0] == "information" and "Runner" in EVENTS["messages"][-1][2]
print("OK (Open Runner / Open Hazard Helper carry the selected dam)")

# ---------------------------------------------------------------- 6. tool 7's switch, the version
alg = RunBreachSim()
alg.initAlgorithm()
assert alg._params["WRITE_POLYGONS"].defaultValue is True and "7_flood_polygons.gpkg" in alg._params["WRITE_POLYGONS"].description
cp = configparser.ConfigParser(interpolation=None)
cp.read(os.path.join(PLUGIN, "metadata.txt"), encoding="utf-8")
ver = cp["general"]["version"]
assert ver >= "0.26.0" and cp["general"]["changelog"].lstrip().startswith(ver + ":"), ver   # 0.26.1: no pin
assert DLG.plugin_version() == ver
plug.unload()
print("OK (0.26.0)")
