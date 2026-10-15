"""0.28.1: the items ported from the October 13 Grok audit.

  1. a downloaded LiDAR zip that would write outside its folder is refused (core/dem_download.py)
  2. a GeoPackage layer or field name holding a quote is read by the preflight (core/preflight.py)
  3. the Simple tab's line of what the run will read, and from where (wf.found_layers_text)
  4. one Continue button that opens the plugin the selected dam needs next, read from its
     dam.json (wf.continue_target), on the Simple tab and the Results tab

Run: FAKE_QGIS_FLAVOR=3.40 PYTHONPATH=tests/qgis_mock:<parent of the plugin> python3 tests/test_v0281.py
"""
import os
import sqlite3
import sys
import tempfile
import zipfile

os.environ["HOME"] = tempfile.mkdtemp(prefix="v0281_home_")       # no suite config of another run
for _k in ("NDSUITE_DATA", "NDSUITE_DAMS"):
    os.environ.pop(_k, None)

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(HERE, "qgis_mock"))
sys.path.insert(0, os.path.dirname(PLUGIN))
sys.path.insert(0, HERE)

import nd_dam_screening as pkg  # noqa: E402
from nd_dam_screening.core import dem_download as dd, preflight as pf, workflow as wf  # noqa: E402
from nd_dam_screening.damsuite import dam_record as DR, settings as DS  # noqa: E402
from nd_dam_screening.dialog import NDDamScreeningDialog  # noqa: E402

ROOT = tempfile.mkdtemp(prefix="v0281_")

# ===================================================================== 1. zip-slip guard
good = os.path.join(ROOT, "good.zip")
with zipfile.ZipFile(good, "w") as z:
    z.writestr("dem/tile.img", b"x")
dest = os.path.join(ROOT, "good")
os.makedirs(dest)
with zipfile.ZipFile(good) as z:
    dd.safe_extract(z, dest)
assert os.path.isfile(os.path.join(dest, "dem", "tile.img"))
for name in ("../escape.img", "dem/../../escape.img"):
    bad = os.path.join(ROOT, "bad.zip")
    with zipfile.ZipFile(bad, "w") as z:
        z.writestr("dem/ok.img", b"x")
        z.writestr(name, b"x")
    dest = os.path.join(ROOT, "bad")
    os.makedirs(dest, exist_ok=True)
    try:
        with zipfile.ZipFile(bad) as z:
            dd.safe_extract(z, dest)
        raise AssertionError("an entry outside the folder was extracted: " + name)
    except ValueError as e:
        assert "outside the destination" in str(e)
    # nothing at all is written when one entry is unsafe
    assert not os.path.exists(os.path.join(ROOT, "escape.img"))
    assert not os.path.exists(os.path.join(dest, "dem", "ok.img"))
print("OK (1: an unsafe LiDAR zip is refused, nothing written)")

# ================================================= 2. quoted GeoPackage names in the preflight
assert pf._q("roads") == '"roads"' and pf._q('a"b') == '"a""b"'
con = sqlite3.connect(":memory:")
con.execute('CREATE TABLE {} ({} TEXT, fid INTEGER)'.format(pf._q('dams "v2"'), pf._q('na"me')))
con.execute('INSERT INTO {} VALUES (?, 1)'.format(pf._q('dams "v2"')), ("Absaraka",))
cols = [r[1] for r in con.execute("PRAGMA table_info({})".format(pf._q('dams "v2"')))]
assert cols == ['na"me', "fid"]
assert con.execute("SELECT {} FROM {}".format(pf._q('na"me'), pf._q('dams "v2"'))).fetchone()[0] == "Absaraka"
print("OK (2: a quote in a layer or field name is read, not a broken query)")

# ============================================== 3. what the run will read, before it starts
assert wf.found_layers_text("", {}) == ""
found = {s: None for s in wf.FILL_SLOTS}
assert wf.found_layers_text("w", {}, found).startswith("No input layers found")
found.update(dwr="suite pack", structures="suite pack", roads="suite pack", dam_lines="dwr folder")
text = wf.found_layers_text("w", {}, found)
assert text.startswith("Found: DWR dams, structures, roads (offline pack); dam lines (dwr/ folder).")
assert "Not found: NID dams, railroads, lifelines, waterbodies." in text, text
# the real lookup: a layer chosen by hand is found as "chosen", nothing is clipped
W = os.path.join(ROOT, "work")
os.makedirs(W)
got = wf.found_layers(W, {"roads": "/data/roads.gpkg"})
assert got["roads"] == "chosen" and set(got) == set(wf.FILL_SLOTS)
assert not os.path.isdir(os.path.join(W, wf.SUITE_DIR))
print("OK (3: the found-layers line)")

# ======================================================= 4. Continue, from the dam's record
assert wf.continue_target("", {}, "DWR-1")[0] == ""
plugin, label, why = wf.continue_target(W, {}, "")
assert plugin == "" and label == "Continue" and "Select a dam" in why
dams = wf.dams_folder(W, {})
folder = os.path.join(dams, "DWR-2801")
os.makedirs(folder)
assert wf.continue_target(W, {}, "DWR-2801")[0] == ""                     # no dam.json yet
rec = DR.load(folder, create=True)
rec.add_status("screened", "ndscreen", "0.28.1", {"needs_model": True}, when="2026-10-13T10:00:00")
rec.save()
plugin, label, why = wf.continue_target(W, {}, "DWR-2801")
assert plugin == "ndbreach" and label == "Continue: model DWR-2801 in the Runner", (plugin, label)
assert "breach model" in why
rec = DR.load(folder)
rec.add_status("modelled", "ndbreach", "3.20.1", {}, when="2026-10-13T11:00:00")
rec.save()
plugin, label, _w = wf.continue_target(W, {}, "DWR-2801")
assert plugin == "ndhazard" and label == "Continue: classify DWR-2801 in the Hazard Helper"
# a bare DWR number finds DWR-<number>, as the hand-off buttons do
assert wf.continue_target(W, {}, "2801")[0] == "ndhazard"
print("OK (4a: Continue reads the next step from dam.json)")

DS.set_current_path("", remember=True)
plug = pkg.classFactory(None)
plug.initGui()
dlg = NDDamScreeningDialog()
dlg.work_edit.setText(W)
dlg.refresh()
for b in (dlg.continue_btn, dlg.results_continue_btn):
    assert b.text() == "Continue" and not b.isEnabled() and "Select a dam" in b.toolTip()
assert dlg.found_label.text().startswith(("Found:", "No input layers found")), dlg.found_label.text()
opened = []
dlg._open_runner = lambda: opened.append("runner")
dlg._open_hazard = lambda: opened.append("hazard")
dlg._selected_dam_id = lambda fallback=False: "DWR-2801"
dlg.refresh()
for b in (dlg.continue_btn, dlg.results_continue_btn):
    assert b.isEnabled() and b.text() == "Continue: classify DWR-2801 in the Hazard Helper", b.text()
dlg._continue()
assert opened == ["hazard"]
dlg._selected_dam_id = lambda fallback=False: "DWR-9999"
dlg._continue_follow()
assert not dlg.continue_btn.isEnabled() and "no dam record" in dlg.continue_btn.toolTip()
print("OK (4b: the Continue buttons in the window)")
print("ALL OK")
