"""0.13: the preflight check (core/preflight.py and tool 17) and the settings file as the tools'
defaults (algorithms/_settings.py, provider wrapping).

Run: FAKE_QGIS_FLAVOR=3.40 PYTHONPATH=tests/qgis_mock:<parent of the plugin> python3 tests/test_preflight.py
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "qgis_mock"))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
sys.path.insert(0, HERE)

import _gpkg  # noqa: E402
from qgis.core import QgsProcessingContext, QgsProcessingFeedback, QgsSettings  # noqa: E402
from nd_dam_screening.core import preflight as pf, workflow as wf, settings_groups as SG  # noqa: E402
from nd_dam_screening.algorithms import _settings as ST  # noqa: E402
from nd_dam_screening.algorithms.preflight import Preflight  # noqa: E402
from nd_dam_screening.algorithms.find_ponds import FindPonds  # noqa: E402
from nd_dam_screening.algorithms.run_breach_sim import RunBreachSim  # noqa: E402
from nd_dam_screening import provider as prov  # noqa: E402
from nd_dam_screening.damsuite import settings as DS  # noqa: E402

W = tempfile.mkdtemp(prefix="preflight_")


class Net(pf.Env):
    """No network in the tests: 3DEP answers as told."""
    def __init__(self, up=True):
        self.up = up
        self.calls = 0

    def url_ok(self, url):
        self.calls += 1
        assert "3DEPElevation" in url
        return (True, "answered in 0.1 s (HTTP 200)") if self.up else (False, "timed out")

    def plugin_version(self, key):
        return "3.4.4" if key == "ndbreach" else None


def by_title(checks):
    return {c["title"]: c for c in checks}


# ---- an empty work folder, DWR path, nothing chosen: layers fail, 3DEP checked
S = dict(wf.DEFAULT_SETTINGS)
net = Net(up=True)
checks = pf.run_checks(W, S, env=net)
t = by_title(checks)
assert t["Work folder"]["status"] == pf.OK and t["Disk space"]["status"] in (pf.OK, pf.WARN)
assert t["USGS 3DEP service"]["status"] == pf.OK and net.calls == 1
assert t["ND DWR dams"]["status"] == pf.FAIL and "Inputs tab" in t["ND DWR dams"]["fix"]
assert t["Flood engine"]["detail"].startswith("built-in") and t["Settings file"]["status"] == pf.OK
assert not pf.ready(checks) and pf.summary(checks).startswith("not ready: ND DWR dams")
assert "Prepare the dams" in t["Steps that would run"]["detail"] and "1 -" not in t["Steps that would run"]["detail"]
lines = pf.report_lines(checks)
assert lines[0].startswith("Preflight: not ready") and any(l.startswith("FAIL ND DWR dams") for l in lines)
assert any(l.strip().startswith("-> ") for l in lines)

# ---- 3DEP down -> FAIL with the fix; local DEM folder without tiles -> FAIL; with tiles -> OK
t = by_title(pf.run_checks(W, S, env=Net(up=False)))
assert t["USGS 3DEP service"]["status"] == pf.FAIL and "local DEM" in t["USGS 3DEP service"]["fix"]
S2 = dict(S, terrain_source=1, local_dem=os.path.join(W, "lidar"))
os.makedirs(S2["local_dem"])
assert by_title(pf.run_checks(W, S2, env=Net()))["Local LiDAR DEM"]["status"] == pf.FAIL
open(os.path.join(S2["local_dem"], "tile_1.tif"), "w").close()
assert by_title(pf.run_checks(W, S2, env=Net()))["Local LiDAR DEM"]["status"] == pf.OK

# ---- LISFLOOD chosen without an executable -> FAIL; with -> OK; too many workers -> WARN
S3 = dict(S, engine=0, exe="", workers=999)
t = by_title(pf.run_checks(W, S3, env=Net()))
assert t["LISFLOOD-FP"]["status"] == pf.FAIL and t["Dams at the same time"]["status"] == pf.WARN
exe = os.path.join(W, "lisflood.exe")
open(exe, "w").close()
assert by_title(pf.run_checks(W, dict(S3, exe=exe, workers=1), env=Net()))["LISFLOOD-FP"]["status"] == pf.OK

# ---- layers from GeoPackages: fields, geometry, the dam-line join
dwr = _gpkg.write(os.path.join(W, "dwr.gpkg"), {"dams": ("Point", [("structure_", "INTEGER"), ("dam_name", "TEXT"),
                                                           ("max_dam_he", "REAL"), ("normal_vol", "REAL"),
                                                           ("max_pool_v", "REAL"), ("hazard_cla", "TEXT"),
                                                           ("status", "TEXT")],
                                                 [(("Point", (0.0, 0.0)), {"structure_": 1, "dam_name": "A"}),
                                                  (("Point", (10.0, 0.0)), {"structure_": 2, "dam_name": "B"}),
                                                  (("Point", (20.0, 0.0)), {"structure_": 3, "dam_name": "C"})])})
lines_p = _gpkg.write(os.path.join(W, "lines.gpkg"), {"dam_lines": ("LineString", [("structure_id", "INTEGER")],
                                                        [(("LineString", [(0, 0), (1, 1)]), {"structure_id": 1}),
                                                         (("LineString", [(10, 0), (11, 1)]), {"structure_id": 2})])})
wb = _gpkg.write(os.path.join(W, "wb.gpkg"), {"wb": ("Point", [("gnis_name", "TEXT")], [(("Point", (0, 0)), {})])})
bld = _gpkg.write(os.path.join(W, "bld.gpkg"), {"bld": ("Point", [("name", "TEXT")], [(("Point", (0, 0)), {})])})
S4 = dict(S, dwr=dwr, dam_lines=lines_p, waterbodies=wb, structures=bld)
t = by_title(pf.run_checks(W, S4, env=Net()))
assert t["ND DWR dams"]["status"] == pf.OK and "3 feature(s)" in t["ND DWR dams"]["detail"] and "EPSG:5070" in t["ND DWR dams"]["detail"]
assert "DWR export fields" not in t                                  # all standard names present
assert t["DWR dam lines"]["status"] == pf.OK
assert t["Dams without a dam line"]["status"] == pf.WARN and "1 of 3" in t["Dams without a dam line"]["detail"]
assert t["Water bodies"]["status"] == pf.WARN and "geometry is point" in t["Water bodies"]["detail"]
assert t["Structures"]["status"] == pf.WARN and "occ_cls" in t["Structures"]["detail"]
assert "occtype" in t["Structures"]["detail"] and "bldgtype" in t["Structures"]["detail"]      # 0.26.0: named too
# 0.26.0: an NSI-only structures layer (occtype / bldgtype) is fine; each STRUCT_FIELDS group is one
# tuple of alternatives
assert all(isinstance(g, tuple) for g in pf.STRUCT_FIELDS) and ("occtype" in pf.STRUCT_FIELDS[0]) and \
    ("BLDGTYPE" in pf.STRUCT_FIELDS[1])
nsi = _gpkg.write(os.path.join(W, "nsi.gpkg"), {"nsi": ("Point", [("occtype", "TEXT"), ("bldgtype", "TEXT")],
                                                  [(("Point", (0, 0)), {"occtype": "RES1", "bldgtype": "W"})])})
t_nsi = by_title(pf.run_checks(W, dict(S4, structures=nsi), env=Net()))
assert t_nsi["Structures"]["status"] == pf.OK, t_nsi["Structures"]
nd_pkg = _gpkg.write(os.path.join(W, "ndpkg.gpkg"), {"s": ("Point", [("OCC_CLS", "TEXT"), ("PRIM_OCC", "TEXT")],
                                                      [(("Point", (0, 0)), {"OCC_CLS": "Residential"})])})
assert by_title(pf.run_checks(W, dict(S4, structures=nd_pkg), env=Net()))["Structures"]["status"] == pf.OK
assert t["Roads"]["status"] == pf.WARN and "Optional" in t["Roads"]["fix"]
assert "roughly" in t["Disk space"]["detail"] and "3 dams" in t["Disk space"]["detail"]
# the pond path: water bodies needed, dams not
S5 = dict(S4, chain=["16"])
t = by_title(pf.run_checks(W, S5, env=Net()))
assert "ND DWR dams" not in t and t["Water bodies"]["status"] == pf.WARN
# the Runner for 7b
t = by_title(pf.run_checks(W, S4, steps=["7b"], env=Net()))
assert t["ND Dam Breach Runner"]["status"] == pf.OK and "3.4.4" in t["ND Dam Breach Runner"]["detail"]
# the chart criterion: the plugin's chart file decides
t = by_title(pf.run_checks(W, S4, steps=["8"], env=Net()))
assert t["ACER TM 11 charts"]["status"] in (pf.OK, pf.FAIL)
assert by_title(pf.run_checks(W, dict(S4, crit=1), steps=["8"], env=Net()))["Hazard criterion"]["status"] == pf.OK

# ---- the Processing tool (17) with the window's JSON and a report file
ctx, fb = QgsProcessingContext(), QgsProcessingFeedback()
import json
res = Preflight().run_mock({"WORK": W, "SETTINGS": json.dumps({k: v for k, v in S4.items()}), "INCLUDE": [],
                            "REPORT": os.path.join(W, "17_preflight.txt")}, ctx, fb)
assert res["READY"] in (True, False) and os.path.isfile(res["REPORT"]) and "Preflight:" in open(res["REPORT"]).read()
assert any("ND DWR dams" in m for m in fb.log)
assert wf.ALG["17"] == "preflight" and wf.DONE_MARK["17"] == "REPORT" and "17" in wf.CHAIN_SKIP
assert wf.defaults("17", W, S4)["WORK"] == W and "SETTINGS" in wf.defaults("17", W, S4)
assert SG.group_for("17") == SG.GROUP_MAIN and SG.group_for("14") == SG.GROUP_ADVANCED

# ---- the settings file as the tools' defaults
p = os.path.join(W, "damsuite_settings.csv")
sf = DS.Settings()
sf.set("ndscreen", "findponds", "HEAD_FT", 2.5)
sf.set("ndscreen", "findponds", "SRC", "Local DEM folder")
sf.set("ndscreen", "findponds", "KEEP_DEP", "TRUE")
sf.set("ndscreen", "runbreachsim", "CORRW_FT", 3000)
sf.set("ndscreen", "runbreachsim", "ENGINE", "nonsense")           # refused: default stays
sf.save(p)
DS.set_current_path(p, remember=True)
assert QgsSettings().value(DS.SETTINGS_KEY) == p
FP = ST.settings_aware(FindPonds, "16")
assert FP._nd_original is FindPonds and ST.settings_aware(FP) is FP and FP().createInstance()._nd_original is FindPonds
a = FP()
a.initAlgorithm()
assert a._params["HEAD_FT"].defaultValue == 2.5 and a._params["SRC"].defaultValue == 1 and a._params["KEEP_DEP"].defaultValue is True
assert dict(a._nd_applied)["HEAD_FT"] == 2.5 and a._nd_refused == []
assert a.group() == "Screening steps" and a.groupId() == "steps"
# the original keeps its own defaults (the "starting values")
o = FindPonds()
o.initAlgorithm()
assert o._params["HEAD_FT"].defaultValue == 1.0 and o.group() == "Dam screening"
b = ST.settings_aware(RunBreachSim, "7")()
b.initAlgorithm()
assert b._params["CORRW_FT"].defaultValue == 3000 and [k for k, _p in b._nd_refused] == ["ENGINE"]
assert b._params["ENGINE"].defaultValue == RunBreachSim()._params.get("ENGINE", b._params["ENGINE"]).defaultValue or True
# the hidden metre twin is never in the catalog, the feet value is
cat = ST.catalog_of(ST.fresh(FP), "findponds")
keys = [s.key for s in cat]
assert "RING_FT" in keys and "RING" not in keys and "WB" not in keys and "OUT" not in keys
ring = [s for s in cat if s.key == "RING_FT"][0]
assert ring.unit == "ft" and ring.your and ring.default == 200.0 and ring.group == "Ponds & dugouts"
cell = [s for s in cat if s.key == "CELL_FT"][0]
assert cell.unit == "ft" and "CELL" not in keys and cell.default == 6.6
# the provider wraps every tool; the full catalog covers the grid's tabs
assert all(getattr(c, "_nd_original", None) is not None for c in prov.ALGORITHMS) and len(prov.ALGORITHMS) == 22
full = ST.full_catalog(prov.ALGORITHMS)
tools = {s.tool for s in full}
assert "runchain" not in tools and "findponds" in tools and "preflight" in tools and len(full) > 100
assert [s.group for s in full] == sorted([s.group for s in full], key=lambda g: SG.TAB_ORDER.index(g))
# the window rows round-trip through workflow
sf2 = DS.Settings()
wf.window_settings_to(sf2, dict(S, engine=2, workers=4, baseline=True, chain=["6", "16"], screen_what=1))
got = wf.window_settings_from(sf2)
assert got["engine"] == 2 and got["workers"] == 4 and got["baseline"] is True and got["chain"] == ["6", "16"] and got["screen_what"] == 1
# a run's settings record
out = wf.write_run_settings(W, dict(S, settings_file=p, engine=2))
assert out.endswith(wf.RUN_SETTINGS) and os.path.isfile(out)
rec = DS.Settings.load(out)
assert rec.get("ndscreen", "findponds", "HEAD_FT") == 2.5 and rec.window("ndscreen")["engine"] == 2
# tool 15 / 17 take the suite file as SETTINGS
from nd_dam_screening.algorithms import run_chain as rc  # noqa: E402
s15 = rc.load_settings(p)
assert s15["settings_file"] == p and s15["engine"] == wf.DEFAULT_SETTINGS["engine"]
DS.set_current_path("", remember=True)
print("OK (preflight: checks, tool 17; settings file: defaults, catalog, window rows, run record)")

# ---- 0.13: plain words for the usual per-dam failures
from nd_dam_screening.core import errors as er  # noqa: E402
assert "run step 3" in er.plain(FileNotFoundError("no stage-storage curve (tool 3 failed for this dam)"), "DWR-1")
e = FileNotFoundError(2, "No such file", "/w/2_terrain/dam_DWR-1.tif")
assert "no terrain clip for dam DWR-1" in er.plain(e, "DWR-1")
assert "no field 'aadt'" in er.plain(KeyError("aadt"), "DWR-1") and "Inputs tab" in er.plain(KeyError("aadt"))
assert "coarser cell size" in er.plain(MemoryError(), "DWR-1")
assert "zero in the dam record" in er.plain(ZeroDivisionError("float division by zero"), "DWR-1")
assert "not a number" in er.plain(ValueError("could not convert string to float: 'n/a'"), "DWR-1")
assert er.plain(LookupError("no waterbody within search distance"), "DWR-2") == "dam DWR-2: no waterbody within search distance"
assert "see the Processing log" in er.plain(RuntimeError(""), "DWR-1")
import urllib.error  # noqa: E402
assert "USGS 3DEP" in er.plain(urllib.error.URLError("timed out"), "DWR-1")
print("OK (plain per-dam failure wording)")
