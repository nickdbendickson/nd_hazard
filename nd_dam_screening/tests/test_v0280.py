"""0.28.0: the usability fixes and the new functions of this release.

  1. the run flags in plain words (core/incomplete.py) and the "cached clips only" truncation
  2. the cell size step 7 really uses (core/runplan.py) and the window's honest label
  3. the preflight: the 0_suite_data clips the data step writes are OK, not WARN
  4. the cached DEM's cell follows the source cell
  5/6/7. the window: Run county / Run one dam, the time estimate, the reason a button is grey
  8. the statewide priority list (the Priority tab, damsuite.priority)
  9. both loading cases in one run (core/scenarios.py, tool 7, tool 8)
 10. the breach-parameter sensitivity row (core/breach_band.py, tool 8)
 11. arrival-time contours and the warning time per structure (core/arrival.py, tool 8)
 12. resuming a county run (core/resume.py)

Run: FAKE_QGIS_FLAVOR=3.40 PYTHONPATH=tests/qgis_mock:<parent of the plugin> python3 tests/test_v0280.py
Nothing here runs in a real QGIS (the Qt and Processing test doubles of tests/qgis_mock).
"""
import configparser
import json
import os
import sys
import tempfile

os.environ["HOME"] = tempfile.mkdtemp(prefix="v0280_home_")       # no suite config of another run
for _k in ("NDSUITE_DATA", "NDSUITE_DAMS"):
    os.environ.pop(_k, None)

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(HERE, "qgis_mock"))
sys.path.insert(0, os.path.dirname(PLUGIN))
sys.path.insert(0, HERE)

import numpy as np  # noqa: E402
import _gpkg  # noqa: E402
from qgis.core import (QgsGeometry, QgsPointXY, QgsProcessingContext, QgsProcessingFeedback,  # noqa: E402
                       make_layer)
from qgis.PyQt.QtWidgets import QFileDialog  # noqa: E402
from nd_dam_screening.core import damrecord_io as dri  # noqa: E402
from nd_dam_screening.core import (arrival as ar, breach_band as bb, incomplete as ic,  # noqa: E402
                                   preflight as pf, resume as rs, runplan as rp,
                                   scenarios as sc, workflow as wf)
from nd_dam_screening.algorithms import _common as cm  # noqa: E402
from nd_dam_screening.algorithms.hazard_classification import HazardClassification  # noqa: E402
from nd_dam_screening.algorithms.run_breach_sim import RunBreachSim, TERRAIN  # noqa: E402
from nd_dam_screening.damsuite import priority as PR  # noqa: E402

ROOT = tempfile.mkdtemp(prefix="v0280_")
FT = 0.3048
D, S = "double", "string"


def touch(*parts):
    p = os.path.join(*parts)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as fh:
        fh.write("x")
    return p


# ======================================================= 1. the run flags (core/incomplete.py)
cut = {"sim_status": "ok", "edge_flag": "YES", "hz_class": "Medium"}
assert ic.truncated(cut) and ic.reasons(cut) == [ic.EDGE]
assert ic.text(cut).startswith("flood truncated at the terrain edge")
assert ic.short(cut) == "flood truncated at the terrain edge"
# every reason this module shows is a reason damsuite.priority groups as "incomplete": the window
# and the review list must not disagree about the same dam
for why in (ic.EDGE, ic.NO_TERRAIN, ic.SPREADING):
    assert ic.known_to_priority(why), why
grown = {"sim_status": "ok", "edge_flag": "no",
         "sim_notes": "no terrain beyond the clip; could not enlarge it"}
assert ic.reasons(grown) == [ic.NO_TERRAIN] and ic.truncated(grown)
assert ic.reasons({"sim_status": "FAILED", "sim_notes": "3DEP timed out"}) == [ic.FAILED]
assert ic.reasons({"sim_status": "ok", "hz_class": "NoData"}) == [ic.NO_RESULT]
# a dam that was never meant to be modelled is a result, not a truncated run
for st in ("triage", "noflood", "excluded"):
    assert ic.reasons({"sim_status": st, "edge_flag": "YES"}) == [], st
assert ic.reasons({"sim_status": "ok", "edge_flag": "no"}) == [] and not ic.text({})
assert ic.short({}) == "" and ic.reasons(None) == []
assert ic.short({"sim_status": "ok", "edge_flag": "YES", "sim_notes": "still spreading"}).endswith("(+1)")
# the setting that caused it, named BEFORE a run
assert ic.settings_reason({}, 0) == ic.CACHED_ONLY and "cut off" in ic.CACHED_ONLY
assert ic.settings_reason({}, 1) == "" and ic.settings_reason({"larger_clips": 0}) == ic.CACHED_ONLY
assert ic.settings_reason({}) == ""                       # the default is no longer cached-only
rows = [cut, grown, {"sim_status": "ok", "edge_flag": "no"}, {"sim_status": "triage"}]
sm = ic.summary(rows)
assert sm == {"dams": 4, "flagged": 2, "reasons": {ic.EDGE: 1, ic.NO_TERRAIN: 1}}, sm
assert ic.summary_text(rows).startswith("2 of 4 dam(s) incomplete - their class is a FLOOR")
assert ic.summary_text([{"sim_status": "ok"}]) == ""
# read straight out of a step 7 / step 8 GeoPackage, with sqlite3 and no QGIS
pt0 = ("Point", (0.0, 0.0))
g = os.path.join(ROOT, "7_simulations.gpkg")
_gpkg.write(g, {"sim": ("POINT", [("dam_id", "TEXT"), ("sim_status", "TEXT"), ("edge_flag", "TEXT")],
                        [(pt0, {"dam_id": "DWR-1", "sim_status": "ok", "edge_flag": "YES"}),
                         (pt0, {"dam_id": "DWR-2", "sim_status": "ok", "edge_flag": "no"})])})
got = ic.from_gpkg(g)
assert len(got) == 2 and got[0]["dam_id"] == "DWR-1" and got[0]["hz_class"] is None
assert ic.summary(got)["flagged"] == 1
assert ic.from_gpkg(os.path.join(ROOT, "nope.gpkg")) == []
print("OK (1: the run flags, in the same words as the review list)")

# ============================================= 2. the cell size step 7 really uses (runplan.py)
# Quick screen is what every run has used since 0.23.0, whatever the Grid size box said
e = rp.effective_cell({})
assert e["how"] == "preset" and e["cell_m"] == 50.0 and abs(e["cell_ft"] - 164.0) < 0.5
assert "164 ft (50 m)" in e["text"] and "not this box" in e["text"]
assert rp.effective_cell({"preset": 1})["cell_m"] == 20.0
assert rp.effective_cell({"preset": 2})["cell_m"] == 10.0
e = rp.effective_cell({"preset": 3, "grid_ft": 33.0})
assert e["how"] == "grid box" and abs(e["cell_m"] - 10.06) < 0.01 and "this box" in e["text"]
e = rp.effective_cell({"preset": 3, "grid_ft": 0.0})
assert e["how"] == "automatic" and e["cell_m"] is None and "reservoir size" in e["text"]
assert rp.preset_index({"preset": 9}) == 0 and rp.preset_index({"preset": None}) == 0
# the time: coarser is faster, finer is slower, and the doubling options double it
s50, s20, s10 = (rp.seconds_per_dam(c) for c in (50.0, 20.0, 10.0))
assert abs(s50 - 12.0) < 0.01 and abs(s20 - 120.0) < 0.01 and abs(s10 - 900.0) < 0.01
assert s50 < rp.seconds_per_dam(30.0) < s20 < s10
assert rp.seconds_per_dam(50.0, baseline=True) == 2 * s50
assert rp.seconds_per_dam(50.0, both_scenarios=True) == 2 * s50
assert rp.seconds_per_dam(50.0, engine=0) < s50                      # LISFLOOD-FP: a guessed factor
t = rp.run_time_text(37, {"preset": 0, "engine": 2, "workers": 4})
assert "37 dam(s)" in t and "12 s/dam" in t and "164 ft (50 m)" in t and "4 at a time" in t
assert "Order of magnitude only" in t
t = rp.run_time_text(10, {"preset": 0, "engine": 2}, pending=4)
assert "6 already have a result and are not re-run" in t
t = rp.run_time_text(10, {"preset": 0, "engine": 2, "both_scenarios": True, "pregrow": True})
assert "includes both scenarios and clip pre-growth" in t
assert rp.run_time_text(0, {}) == "" and rp.run_time_text(None, {}) == ""
assert rp.count_rows(g) == 2 and rp.count_rows(os.path.join(ROOT, "nope.gpkg")) is None
assert rp.fmt_duration(45) == "45 s" and rp.fmt_duration(600) == "10 min"
print("OK (2: the cell size the engine will use, and how long a county takes)")

# ======================================================== 12. resuming a county run (resume.py)
out7 = os.path.join(ROOT, "7_simulations")
os.makedirs(out7)
assert rs.load(out7)["dams"] == {} and rs.summary_text(out7) == ""
r1 = [touch(out7, "DWR-1_td_{}.tif".format(k)) for k in ("depth", "vel")]
assert rs.record(out7, "DWR-1", "ok", scenario="td", rasters=r1)
assert rs.record(out7, "DWR-2", "FAILED", note="3DEP timed out")
assert rs.record(out7, "DWR-3", "triage")
assert os.path.isfile(rs.path_for(out7))
assert rs.done_ids(out7) == ["DWR-1"]
assert rs.counts(out7) == {"dams": 3, "done": 1, "failed": 1, "skipped": 1, "other": 0}
todo, skip = rs.pending(["DWR-1", "DWR-2", "DWR-3", "DWR-4"], out7)
assert todo == ["DWR-2", "DWR-3", "DWR-4"] and skip == ["DWR-1"]     # a FAILED dam is tried again
assert rs.pending(["DWR-1"], out7, skip_done=False)[0] == ["DWR-1"]  # the switch that forces a re-run
# the manifest never decides on its own: without the rasters the dam is run again
os.remove(r1[0])
assert rs.done_ids(out7) == [] and rs.pending(["DWR-1"], out7)[0] == ["DWR-1"]
assert rs.has_result({"state": "ok", "rasters": r1}, check_rasters=False) is True
txt = rs.summary_text(out7, total=10)
assert "already have a result" in txt and "1 failed and will be tried again" in txt
assert rs.pending_count(out7, 10) == 9 and rs.pending_count(out7, 0) is None
assert rs.load(os.path.join(ROOT, "no_such_folder"))["dams"] == {}
print("OK (12: the per-dam manifest; the rasters, not the manifest, decide)")

# ============================================= 9. both loading cases in one run (scenarios.py)
gov = sc.governing({"td": "Medium", "sd": "High"})
assert gov["hazard_class"] == "High" and gov["scenario"] == "sd" and gov["raised"] is True
assert "sunny day governs" in gov["why"] and "INCREMENTAL" in gov["why"]
gov = sc.governing({"td": "High", "sd": "Medium"})
assert gov["hazard_class"] == "High" and gov["scenario"] == "td" and gov["raised"] is True
gov = sc.governing({"td": "Medium", "sd": "Medium"})
assert gov["hazard_class"] == "Medium" and gov["scenario"] == "td" and gov["raised"] is False
assert "both scenarios give Medium" in gov["why"]
# a scenario that did not run never counts as Low
gov = sc.governing({"td": "Medium", "sd": ""})
assert gov["hazard_class"] == "Medium" and "sunny day gave no class" in gov["why"]
assert sc.governing({})["hazard_class"] == "" and sc.governing({"td": "?"})["hazard_class"] == ""
assert sc.rank("Significant") == sc.rank("Medium") == 1 and sc.rank("") is None
assert sc.both_default(True) is True and sc.both_default(False) is False
assert sc.pairs({}) == ("td",) and sc.pairs({"both_scenarios": True}) == ("td", "sd")
assert sc.pairs({"scenario": 1, "both_scenarios": True}) == ("sd", "td")
assert "two loading cases run" in sc.note(sc.governing({"td": "Medium", "sd": "High"}))
assert sc.note(sc.governing({"td": "Medium"})) == ""
print("OK (9: the worse loading case governs)")

# =================================== 10. the breach-parameter sensitivity row (breach_band.py)
bands = bb.params(1e6, 10.0)
assert sorted(bands) == ["high", "low", "mid"]
assert bands["low"]["bavg_m"] < bands["mid"]["bavg_m"] < bands["high"]["bavg_m"]
assert bands["high"]["tf_s"] <= bands["mid"]["tf_s"] and bands["low"]["tf_s"] >= bands["mid"]["tf_s"]
assert "Froehlich (2008)" == bands["mid"]["from"]
assert abs(bands["mid"]["bavg_ft"] - bands["mid"]["bavg_m"] / FT) < 0.01
assert bb.params(None, 10.0) == {} and bb.params(1e6, 0.0) == {}
elev = np.linspace(490.0, 500.0, 21)
vol = 1e6 * ((elev - 490.0) / 10.0) ** 2
bands = bb.peaks(bands, elev, vol, 500.0, 490.0)
qs = [bands[b]["peak_m3s"] for b in bb.BANDS]
assert all(q and q > 0 for q in qs) and qs[0] < qs[1] < qs[2]        # low < mid < high outflow
df, vf = bb.scale_factors(bands["high"]["peak_m3s"], bands["mid"]["peak_m3s"])
assert df > 1.3 and vf > 1.2 and abs(df * vf - bands["high"]["peak_m3s"] / bands["mid"]["peak_m3s"]) < 1e-9
assert bb.scale_factors(None, 5.0) == (1.0, 1.0) and bb.scale_factors(5.0, 0.0) == (1.0, 1.0)
rows = bb.classes(bands, lambda d, v: "High" if d > 1.2 else "Medium")
v = bb.verdict(rows)
assert v["stable"] is False and v["class_min"] == "Medium" and v["class_max"] == "High"
assert v["text"].startswith(bb.UNCERTAIN) and "with the high breach" in v["text"]
v2 = bb.verdict(bb.classes(bands, lambda d, vv: "Medium"))
assert v2["stable"] is True and v2["text"].startswith(bb.STABLE)
assert bb.verdict({})["stable"] is None and bb.verdict(bb.classes(bands, lambda d, v: ""))["stable"] is None
assert bb.classes(bands, lambda d, v: 1 / 0)["mid"]["hazard_class"] == ""     # never raises
det = bb.detail_text(rows)
assert det.startswith("low ") and "cfs" in det and "-> Medium" in det and "-> High" in det
print("OK (10: low / mid / high breach, and the class under each)")

# ============================= 11. arrival-time contours and the warning time (arrival.py)
gt = (0.0, 10.0, 0.0, 100.0, 0.0, -10.0)
arrive = np.full((10, 10), -9999.0)
arrive[2, 2] = 0.2                       # 12 minutes: under 15
arrive[2, 5] = 0.75                      # 45 minutes
arrive[6, 6] = 3.0                       # 3 hours
wet = arrive > -9998.0
assert abs(ar.sample(arrive, gt, 25.0, 75.0) - 0.2) < 1e-9
assert ar.sample(arrive, gt, 5.0, 95.0) is None and ar.sample(arrive, gt, -500.0, 0.0) is None
assert ar.sample(None, gt, 0, 0) is None
assert ar.band_of(0.2) == "under15" and ar.band_of(0.75) == "15to60" and ar.band_of(3.0) == "over60"
assert ar.band_of(None) == "" and ar.band_text(None) == ""
assert ar.band_text(0.2) == "12 min (under 15 minutes)" and ar.band_text(3.0).startswith("3.0 h")
assert ar.bands() == ("under15", "15to60", "over60")
masks = ar.contour_masks(arrive, wet)
assert [h for _l, h, _m in masks] == [0.5, 1.0, 2.0]
assert [lab for lab, _h, _m in masks] == ["30 min", "1 h", "2 h"]
assert [int(m.sum()) for _l, _h, m in masks] == [1, 2, 2]            # cumulative
assert ar.contour_masks(np.full((4, 4), -9999.0), np.ones((4, 4), bool)) == []
assert ar.worst_band([3.0, 0.2]) == "under15" and ar.worst_band([]) == ""
assert "soonest 12 min (under 15 minutes) at 3 flooded feature(s)" == ar.summary_text([0.2, 0.75, 3.0])
print("OK (11: arrival contours at 30 min / 1 h / 2 h, warning bands)")

# ======================================== 1 / 2 / 4 / 9 / 12 in the chain (core/workflow.py)
W = os.path.join(ROOT, "work")
os.makedirs(W)
touch(W, "1_dams.gpkg")
touch(W, "5_dams_breach.gpkg")
# the larger-clip source follows the terrain source; "cached clips only" stays a deliberate choice
assert wf.DEFAULT_SETTINGS["larger_clips"] == wf.CLIPS_FOLLOW == 3
assert wf.larger_clips_index({}) == wf.CLIPS_3DEP
assert wf.larger_clips_index({"terrain_source": 1, "local_dem": "/lidar"}) == wf.CLIPS_LOCAL
assert wf.larger_clips_index({"terrain_source": 1}) == wf.CLIPS_3DEP     # no folder given: not local
assert wf.larger_clips_index({"larger_clips": 0}) == wf.CLIPS_CACHE
assert "follows the terrain source" in wf.larger_clips_text({})
assert "CUT OFF" in wf.larger_clips_text({"larger_clips": 0})
d7 = wf.defaults("7", W, dict(wf.DEFAULT_SETTINGS))
assert d7["TSRC"] == wf.CLIPS_3DEP and d7["PREGROW"] is True and d7["PRESET"] == 0
assert "SCEN_BOTH" not in d7 and "SKIPDONE" not in d7
d7 = wf.defaults("7", W, dict(wf.DEFAULT_SETTINGS, both_scenarios=True, skip_done=False, preset=3))
assert d7["SCEN_BOTH"] is True and d7["SKIPDONE"] is False and d7["PRESET"] == 3
# the cached DEM's cell follows the source cell, and tool 12 estimates the cell step 7 will use
assert wf.DEFAULT_SETTINGS["dem_cell_ft"] == 32.8
assert wf.defaults("2", W, dict(wf.DEFAULT_SETTINGS))["CELL_FT"] == 32.8
assert abs(wf.defaults("12", W, dict(wf.DEFAULT_SETTINGS))["CELL_FT"] - 164.04) < 0.1
assert abs(wf.defaults("12", W, dict(wf.DEFAULT_SETTINGS, preset=3, grid_ft=33.0))["CELL_FT"] - 33.0) < 1e-9
# a one-dam run models both loading cases; a county keeps one
S1 = wf.one_dam_settings(dict(wf.DEFAULT_SETTINGS), W, "DWR-1")
assert S1["both_scenarios"] is True and wf.DEFAULT_SETTINGS["both_scenarios"] is False
assert wf.one_dam_settings(dict(wf.DEFAULT_SETTINGS, both_one_dam=False), W, "DWR-1")["both_scenarios"] is False
assert wf.dam_count(W) is None                        # 1_dams.gpkg is a stub, not a GeoPackage
assert wf.dam_count(os.path.join(ROOT, "nothing")) is None
print("OK (1 / 4 / 9 / 12: the chain passes the new choices)")

# ============================== 3. the preflight: the clips the data step writes are not a WARN
clip = os.path.join(W, wf.SUITE_DIR, "dwr.gpkg")
assert pf.pending_suite_clip(clip + "|layername=dwr", W) is True
assert pf.pending_suite_clip("", W) is False and pf.pending_suite_clip(clip, "") is False
touch(clip)
assert pf.pending_suite_clip(clip + "|layername=dwr", W) is False     # it is there: a real problem


class Env(pf.Env):
    def url_ok(self, url):
        return True, "test"

    def layer_info(self, ref):
        return None if wf.SUITE_DIR in str(ref) else pf.Env.layer_info(self, ref)


S = dict(wf.DEFAULT_SETTINGS, work=W, dwr=os.path.join(W, wf.SUITE_DIR, "pending.gpkg"))
checks = pf.run_checks(W, S, steps=["1", "7"], env=Env())
by = dict((c["title"], c) for c in checks)
assert by["ND DWR dams"]["status"] == pf.OK
assert by["ND DWR dams"]["detail"] == "written by the data step when the run starts (pending.gpkg)"
# the truncation this release is about is named before the run, and the cell size with it
assert by["Flood cut off at the clip edge"]["status"] == pf.OK
cached = pf.run_checks(W, dict(S, larger_clips=0), steps=["7"], env=Env())
cut = dict((c["title"], c) for c in cached)["Flood cut off at the clip edge"]
assert cut["status"] == pf.WARN and cut["detail"] == ic.CACHED_ONLY and cut["fix"]
assert "164 ft (50 m)" in dict((c["title"], c) for c in cached)["Flood grid cell size"]["detail"]
# a local LiDAR folder is finer than the 3DEP source this default follows, and step 2 cannot read
# the tiles' cell from here: the preflight says so before the run instead of quietly coarsening it
loc = os.path.join(ROOT, "lidar")
touch(loc, "t1.tif")
lid = pf.run_checks(W, dict(wf.DEFAULT_SETTINGS, work=W, terrain_source=1, local_dem=loc),
                    steps=["2"], env=Env())
c = dict((x["title"], x) for x in lid)["Cached DEM cell size"]
assert c["status"] == pf.WARN and "32.8 ft (10.0 m)" in c["detail"] and "3.3 ft" in c["fix"]
ok3 = pf.run_checks(W, dict(wf.DEFAULT_SETTINGS, work=W, terrain_source=1, local_dem=loc,
                            dem_cell_ft=3.3), steps=["2"], env=Env())
assert "Cached DEM cell size" not in [x["title"] for x in ok3]
# 7. a reason for a grey button: the suite's blocking items and the preflight's failures
reasons = pf.blocking_reasons(checks=[pf.check(pf.FAIL, "Work folder", "none chosen", "Choose one.")],
                              suite_items=[], work="", busy=False)
assert reasons[0] == "no work folder chosen"
assert reasons[1] == "Work folder: none chosen - Choose one."
assert pf.blocking_text(reasons).startswith("Cannot run: ")
assert pf.blocking_text([]) == ""
assert pf.blocking_reasons(checks=[], suite_items=[], work=W, busy=True)[0].startswith("a run is going on")
assert pf.blocking_reasons(checks=[], suite_items=[], work=os.path.join(ROOT, "gone")) == [
    "the work folder does not exist: {}".format(os.path.join(ROOT, "gone"))]
print("OK (3 / 7: pending suite clips are OK; a grey button has a reason)")

# ================================= tool 7's own defaults (the truncation, and the new switches)
alg = RunBreachSim()
alg._params = {}
alg.initAlgorithm()
assert TERRAIN[1].startswith("USGS 3DEP")
assert alg._params["TSRC"].defaultValue == 1, "tool 7 must not default to cached clips only"
assert alg._params["PREGROW"].defaultValue is True
assert alg._params["SCEN_BOTH"].defaultValue is False
assert alg._params["SKIPDONE"].defaultValue is True
help7 = alg.shortHelpString()
assert "TERRAIN FOR LARGER CLIPS (0.28.0)" in help7 and "BOTH LOADING CASES" in help7
assert "RESUME (0.28.0)" in help7 and "arrival_contour" in help7
print("OK (tool 7: 3DEP and pre-growth by default, both scenarios and resume available)")

# ============== 9 / 10 / 11 through tool 8: the governing case, the band row, the warning time
cell = 10.0
GT = (0.0, cell, 0.0, 100.0, 0.0, -cell)
xy = lambda r, c: (GT[0] + (c + 0.5) * cell, GT[3] - (r + 0.5) * cell)
pt = lambda r, c: QgsGeometry.fromPointXY(QgsPointXY(*xy(r, c)))
line = lambda pts: QgsGeometry.fromPolylineXY([QgsPointXY(*p) for p in pts])


def rasters(name, house_ft, arrive_h=None):
    """(depth, velocity, arrival) GeoTIFFs: a house cell at `house_ft` feet, a road overtopped."""
    depth, vel = np.zeros((10, 10)), np.zeros((10, 10))
    depth[2, 2], vel[2, 2] = house_ft * FT, 1.0 * FT
    depth[6, :] = 0.4                                            # the road, overtopped
    arr = np.full((10, 10), -9999.0)
    if arrive_h is not None:
        arr[2, 2], arr[6, :] = arrive_h, arrive_h + 1.0
    out = []
    for key, a, nd in (("d", depth, None), ("v", vel, None), ("a", arr, -9999.0)):
        p = os.path.join(ROOT, "{}_{}.tif".format(name, key))
        cm.write_tif(p, a, GT, nodata=nd)
        out.append(p)
    return out


td_d, td_v, td_a = rasters("td", 1.5, 0.6)                       # 1.5 ft at the house: not lethal
sd_d, sd_v, sd_a = rasters("sd", 4.0, 0.2)                       # 4 ft, 12 minutes' warning
# the stage-storage curve tool 3 writes, so the bands can be routed
curves = os.path.join(ROOT, "3_stage_storage")
os.makedirs(curves)
with open(os.path.join(curves, "curve_DWR-1.csv"), "w") as fh:
    fh.write("elev_m,storage_m3,elev_ft,storage_acft\n")
    for z in np.linspace(490.0, 500.0, 21):
        fh.write("{},{},0,0\n".format(z, 1e6 * ((z - 490.0) / 10.0) ** 2))
SIMF = [("dam_id", S), ("scenario", S), ("sim_status", S), ("depth_tif", S), ("vel_tif", S),
        ("arrive_tif", S), ("edge_flag", S), ("haz_existing", S),
        ("scen2", S), ("scen2_status", S), ("scen2_depth_tif", S), ("scen2_vel_tif", S),
        ("scen2_arrive_tif", S), ("scen2_edge_flag", S),
        ("td_vw_m3", D), ("td_hb_m", D), ("td_start_m", D), ("base_m", D),
        ("sim_cell_m", D), ("brch_eq", S), ("brch_mode", S)]
BASE = {"dam_id": "DWR-1", "scenario": "td", "sim_status": "ok", "depth_tif": td_d,
        "vel_tif": td_v, "arrive_tif": td_a, "edge_flag": "no", "haz_existing": "Low",
        "td_vw_m3": 1e6, "td_hb_m": 10.0, "td_start_m": 500.0, "base_m": 490.0,
        "sim_cell_m": 20.0, "brch_eq": "F2008", "brch_mode": "piping"}
bld = make_layer([("OCC_CLS", S), ("PRIM_OCC", S)],
                 [(pt(2, 2), {"OCC_CLS": "Residential", "PRIM_OCC": "Single Family"})], "point", "bld")
roads = make_layer([("aadt", D), ("road_class", S)],
                   [(line([xy(6, 0), xy(6, 9)]), {"aadt": 600.0, "road_class": "County 12"})],
                   "line", "roads")
ctx, fb = QgsProcessingContext(), QgsProcessingFeedback()
BLD, ROADS = ctx.add(bld), ctx.add(roads)


def tool8(sim_rows, **kw):
    sim = make_layer(SIMF, [(pt(0, 0), r) for r in sim_rows], "point", "sim")
    r = HazardClassification().run_mock(dict(dict(SIM=ctx.add(sim), BLD=BLD, ROADS=ROADS), **kw),
                                        ctx, fb)
    out = ctx.layers[r["OUT"]]
    names = out.fields().names()
    rows = {str(f["dam_id"]): {k: f[k] for k in names} for f in out.getFeatures()}
    return rows, list(ctx.layers[r["IMP"]].getFeatures())


# -- one loading case: the 1.5 ft house is not lethal under the ND 1D rule, the road is Medium
rows, imp = tool8([BASE], CRIT=1, RULE=2.0, BREACH_BAND=False)
d1 = rows["DWR-1"]
assert d1["hz_class"] == "Medium" and d1["hz_scen"] == "td", (d1["hz_class"], d1["hz_scen"])
assert d1["hz_scen_class"] is None and not d1["bp_detail"]
# 11. the warning time rides along with every impacted feature
got = sorted((f["kind"], f["scenario"], round(f["arrive_hr"] or 0, 2), f["warn_time"]) for f in imp)
assert got == [("road", "td", 1.6, "96 min (over 60 minutes)"),
               ("structure", "td", 0.6, "36 min (15 to 60 minutes)")], got
assert d1["hz_warn"] == "soonest 36 min (15 to 60 minutes) at 2 flooded feature(s)", d1["hz_warn"]

# -- both loading cases: the sunny-day flood is 4 ft at the house, so IT governs and raises the
#    class. The class only ever moves up: running one case can miss consequences, never add them.
both = dict(BASE, scen2="sd", scen2_status="ok", scen2_depth_tif=sd_d, scen2_vel_tif=sd_v,
            scen2_arrive_tif=sd_a, scen2_edge_flag="no")
rows, imp = tool8([both], CRIT=1, RULE=2.0, BREACH_BAND=False)
d1 = rows["DWR-1"]
assert d1["hz_class"] == "High" and d1["hz_scen"] == "sd", (d1["hz_class"], d1["hz_scen"])
assert d1["hz_scen_class"] == "td Medium; sd High", d1["hz_scen_class"]
assert "sunny day governs" in d1["hz_scen_why"] and "INCREMENTAL" in d1["hz_scen_why"]
assert "two loading cases run" in d1["hz_notes"]
assert all(f["scenario"] == "sd" for f in imp)                   # the governing case's receptors
assert d1["hz_warn"] == "soonest 12 min (under 15 minutes) at 2 flooded feature(s)", d1["hz_warn"]
# the second case asked for but not run: the first one's class stands, and it is a data gap
rows, _ = tool8([dict(BASE, scen2="sd", scen2_status="FAILED")], CRIT=1, RULE=2.0, BREACH_BAND=False)
d1 = rows["DWR-1"]
assert d1["hz_class"] == "Medium" and "no flood raster" in d1["hz_gaps"]

# -- 10. the breach-parameter row: the same dam is Medium with the narrow breach and High with the
#    wide one, so it is marked instead of being given one number
rows, _ = tool8([BASE], CRIT=1, RULE=2.0, CURVES=curves)
d1 = rows["DWR-1"]
assert d1["bp_stable"] == "no" and d1["bp_class_lo"] == "Medium" and d1["bp_class_hi"] == "High"
assert d1["bp_detail"].startswith("low ") and "-> High" in d1["bp_detail"]
assert bb.UNCERTAIN in d1["hz_notes"] and d1["hz_conf"] == "review"
assert d1["hz_class"] == "Medium"                # the row explains the class; it never changes it
# without the curves there is no routing, so the widths and times are reported and nothing is
# called stable (a false "stable" would be worse than no row at all)
rows, _ = tool8([BASE], CRIT=1, RULE=2.0)
d1 = rows["DWR-1"]
assert d1["bp_detail"] and not d1["bp_stable"] and bb.UNCERTAIN not in (d1["hz_notes"] or "")
assert "ft /" in d1["bp_detail"] and "cfs" not in d1["bp_detail"]
# a truncated flood says so in the dam's own notes, in the words the review list knows
rows, _ = tool8([dict(BASE, edge_flag="YES")], CRIT=1, RULE=2.0, BREACH_BAND=False)
d1 = rows["DWR-1"]
assert d1["hz_conf"] == "review" and "flood truncated at the terrain edge" in d1["hz_notes"]
assert ic.known_to_priority(d1["hz_notes"])
print("OK (9 / 10 / 11: the worse case governs, the breach band is reported, warning times)")

# ===================== the hand-off to ND Dam Breach Runner: what the screening actually ran
# Its core/screenhandoff.py reads the newest "screened" status of dam.json and fills only the
# inputs the engineer left empty, so a 20 m screen stops becoming a 10 m detailed run with the
# Runner's own breach. The three keys are its spellings, not ours.
sr = dri.screening_result({"tri_result": "needs 2D", "hz_class": "Medium", "sim_cell_m": 20.0,
                           "brch_eq": "F2008", "brch_mode": "piping", "scenario": "td"}, "tool 8")
assert sr["cell_m"] == 20.0 and sr["method"] == "piping" and sr["scenario"] == "sunny"
assert sr["loading_case"] == "top of dam"
# both loading cases in one run: the case that GOVERNED the class is the one handed over
gov = dri.screening_result({"hz_class": "High", "sim_cell_m": 20.0, "brch_eq": "F1995",
                            "brch_mode": "overtopping", "scenario": "td", "scen2": "sd",
                            "hz_scen": "sd"}, "tool 8")
assert gov["loading_case"] == "sunny day" and gov["method"] == "froehlich1995_overtopping"
assert gov["scenario"] == "sunny" and gov["cell_m"] == 20.0
# every method the screening offers maps onto a key the Runner knows, and the mode only moves
# the two Froehlich equations
for eq, mode, want in (("F2008", "overtopping", "overtopping"), ("F1995", "piping", "froehlich1995_piping"),
                       ("VTG-E", "piping", "vtg1990"), ("VTG-R", "overtopping", "vtg1990"),
                       ("MLM", "piping", "mlm1984"), ("TR60", "piping", "tr60"),
                       ("TR60-C", "piping", "tr60_curvilinear"), ("F2008", "", "piping")):
    assert dri.runner_method({"brch_eq": eq, "brch_mode": mode}) == want, (eq, mode)
assert set(bb.SPREAD_METHODS) - {"F2008", "F1995", "VTG-E", "VTG-R", "MLM"} == set()
# a dam with no flood has no cell and no case: nothing is invented (the Runner says what is
# missing instead of starting from a number nothing ran at)
clear = dri.screening_result({"tri_result": "clear"}, "tool 8")
assert "cell_m" not in clear and "method" not in clear and "scenario" not in clear
assert dri.runner_cell_m({"sim_cell_m": 0.0}) is None and dri.runner_scenario({}) == ""
# a re-run at another cell / breach / case is a new status entry: the Runner reads the newest one
assert not dri.same_substance(dict(sr), dict(sr, cell_m=10.0))
assert not dri.same_substance(dict(sr), dict(sr, method="overtopping"))
assert not dri.same_substance(dict(sr), dict(sr, loading_case="sunny day"))
print("OK (hand-off: cell_m, method, scenario of the run that produced the class)")

# ... and the same three keys through tool 8 itself, into the dam folder's dam.json
dams8 = os.path.join(ROOT, "Dams8")
os.makedirs(os.path.join(dams8, "DWR-1"))
rows, _ = tool8([dict(BASE, scen2="sd", scen2_status="ok", scen2_depth_tif=sd_d,
                      scen2_vel_tif=sd_v, scen2_arrive_tif=sd_a, scen2_edge_flag="no")],
                CRIT=1, RULE=2.0, BREACH_BAND=False, DAMS_FOLDER=dams8)
from nd_dam_screening.damsuite import dam_record as DR8  # noqa: E402
got = DR8.load(os.path.join(dams8, "DWR-1")).latest("screened")["result"]
assert got["cell_m"] == 20.0 and got["method"] == "piping" and got["scenario"] == "sunny"
assert got["loading_case"] == "sunny day"            # the sunny-day case governed (4 ft house)
assert rows["DWR-1"]["hz_scen"] == "sd"
print("OK (hand-off: tool 8 writes them into dam.json)")
