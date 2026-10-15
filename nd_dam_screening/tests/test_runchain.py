"""Tool 15 (headless chain) and tool 13 (inventory diff) through the mock."""
import csv, json, os, shutil, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "qgis_mock"))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
sys.path.insert(0, HERE)
from qgis.core import QgsProcessingContext, QgsProcessingFeedback, QgsProcessingException, QgsGeometry, QgsPointXY, make_layer  # noqa: E402
from qgis.PyQt.QtCore import QVariant  # noqa: E402
from qgis import processing  # noqa: E402
import nd_dam_screening as pkg  # noqa: E402
from nd_dam_screening.core import workflow as wf  # noqa: E402
from nd_dam_screening.algorithms import run_chain as rc  # noqa: E402
from nd_dam_screening.damsuite import dam_record as dr  # noqa: E402
import _gpkg  # noqa: E402

plug = pkg.classFactory(None); plug.initGui()
algs = {a.name(): a for a in plug.provider.algs}
for a in algs.values():
    a.initAlgorithm()
processing.REGISTRY.update(algs)
work = tempfile.mkdtemp(prefix="runchain_")
ctx, fb = QgsProcessingContext(), QgsProcessingFeedback()

# ---------------------------------------------------------------- settings loading
assert rc.load_settings("")["engine"] == 2 and rc.load_settings("")["work"] == ""
assert rc.load_settings('{"engine": 3, "dwr": "/d.gpkg"}')["engine"] == 3
sp = os.path.join(work, "s.json"); json.dump({"workers": 2}, open(sp, "w"))
assert rc.load_settings(sp)["workers"] == 2
for bad in ("{oops", "[1, 2]"):
    try:
        rc.load_settings(bad); raise AssertionError(bad)
    except QgsProcessingException:
        pass

# ---------------------------------------------------------------- the chain tool, stubbed steps
W = os.path.join(work, "w"); os.makedirs(W)
calls = []
real_run = processing.run


def stub_run(aid, params, context=None, feedback=None, is_child_algorithm=False):
    calls.append((aid, dict(params)))
    step = next(k for k in wf.KEYS if wf.algorithm_id(k) == aid)
    for param, rel in wf.LAYOUT[step].items():
        if param == "DAMS_FOLDER":
            continue
        p = os.path.join(W, rel)
        if os.path.splitext(p)[1]:
            open(p, "wb").close()
        else:
            os.makedirs(p, exist_ok=True); open(os.path.join(p, "x"), "w").close()
    if step == "10":
        open(os.path.join(W, "10_reports", "index.html"), "w").close()
    time.sleep(0.01)
    return {}


rc.processing.run = stub_run
alg = algs["runchain"].createInstance()
own = {"dwr": "/d.gpkg", "waterbodies": "/w.gpkg", "dam_lines": "/l.gpkg", "structures": "/b.gpkg"}
# 0.13: the chain runs the preflight first; no network here, and the stub layers are not files, so the
# check would refuse - the chain itself is tested with PREFLIGHT off, the gate separately below
from nd_dam_screening.core import preflight as pf  # noqa: E402
pf.Env.url_ok = lambda self, url: (True, "stubbed")
own["crit"] = 2
# dry run: lists the plan, starts nothing
res = alg.run_mock({"WORK": W, "SETTINGS": json.dumps(own), "INCLUDE": [1], "DRY_RUN": True,
                    "LOG": os.path.join(work, "dry.txt")}, ctx, fb)
assert res["OK"] is True and res["RAN"] == "" and res["RESULT"] == "dry run" and not calls
assert "Plan: 1, 2, 3, 4, 5, 6, 7, 8, 10" in open(res["LOG"]).read()
# 0.22.1: qgis_process with no --INCLUDE: QGIS answers parameterAsEnums with [0] (step 0); the
# chain must not take that as "include step 0"
_real_enums = alg.parameterAsEnums
alg.parameterAsEnums = lambda params, k, context: [0] if k == "INCLUDE" and not params.get(k) \
    else _real_enums(params, k, context)
res = alg.run_mock({"WORK": W, "SETTINGS": json.dumps(own), "DRY_RUN": True,
                    "LOG": os.path.join(work, "dry0.txt")}, ctx, fb)
assert "Plan: 1, 2, 3, 4, 5, 7, 8, 10" in open(res["LOG"]).read(), open(res["LOG"]).read()
alg.parameterAsEnums = _real_enums
# the real thing (stubbed steps): 6 included, 7b / 11-15 never
res = alg.run_mock({"WORK": W, "SETTINGS": json.dumps(own), "INCLUDE": [1], "DRY_RUN": False,
                    "LOG": os.path.join(work, "run.txt")}, ctx, fb)
assert res["OK"] is True and res["RAN"] == "1,2,3,4,5,6,7,8,10", res
assert "Preflight:" in open(res["LOG"]).read() and os.path.isfile(os.path.join(W, wf.RUN_SETTINGS))
assert [c[0] for c in calls] == ["nddam:" + wf.ALG[k] for k in ("1", "2", "3", "4", "5", "6", "7", "8", "10")]
assert calls[0][1]["DWR"] == "/d.gpkg" and calls[6][1]["DAMS"].endswith("6_triage.gpkg")
assert "Nothing left to run" in res["RESULT"]
log = open(res["LOG"]).read()
assert ">>> Step 8 - Hazard classification" in log and "step 10 done" in log
# nothing left
res = alg.run_mock({"WORK": W, "SETTINGS": json.dumps(own), "INCLUDE": [], "DRY_RUN": False, "LOG": ""}, ctx, fb)
assert res["OK"] is True and res["RAN"] == "" and "nothing to run" in res["RESULT"]
# a missing input stops before the step; a failing step stops the chain
W2 = os.path.join(work, "w2"); os.makedirs(W2)
W, calls = W2, []
# with the preflight on, the missing water-body layer stops the chain before anything starts (0.13)
P2 = {"WORK": W2, "SETTINGS": json.dumps({"dwr": "/d.gpkg", "crit": 2}), "INCLUDE": [],
      "DRY_RUN": False, "LOG": ""}
res = alg.run_mock(dict(P2, FAIL_ON_ERROR=False), ctx, fb)
assert res["OK"] is False and res["RAN"] == "" and res["RESULT"].startswith("preflight: not ready") and not calls
assert "Water bodies" in res["RESULT"] or "ND DWR dams" in res["RESULT"], res
# 0.28.3: by default the tool FAILS instead, so qgis_process exits non-zero
try:
    alg.run_mock(dict(P2), ctx, fb)
    raise AssertionError("a chain stopped by the preflight did not fail the tool")
except QgsProcessingException as e:
    assert "the chain did not finish: preflight: not ready" in str(e) and "none" in str(e), str(e)
assert not calls
# with it off, the chain's own check stops at the step that lacks the input
P3 = {"WORK": W2, "SETTINGS": json.dumps({"dwr": "/d.gpkg"}), "INCLUDE": [], "DRY_RUN": False,
      "LOG": "", "PREFLIGHT": False}
res = alg.run_mock(dict(P3, FAIL_ON_ERROR=False), ctx, fb)
assert res["OK"] is False and res["RAN"] == "1,2" and "Step 3 (Estimate reservoir storage) cannot run" in res["RESULT"]
try:                                                    # 0.28.3: and it fails the tool
    alg.run_mock(dict(P3), ctx, fb)
    raise AssertionError("a chain that stopped at a step did not fail the tool")
except QgsProcessingException as e:
    assert "Step 3 (Estimate reservoir storage) cannot run" in str(e), str(e)
W3 = os.path.join(work, "w3"); os.makedirs(W3)
W, calls = W3, []


def failing_run(aid, params, context=None, feedback=None, is_child_algorithm=False):
    if aid == "nddam:breachdirection":
        raise QgsProcessingException("no dam line within 300 m")
    return stub_run(aid, params, context, feedback, is_child_algorithm)


rc.processing.run = failing_run
res = alg.run_mock({"WORK": W3, "SETTINGS": json.dumps(own), "INCLUDE": [], "DRY_RUN": False,
                    "LOG": "", "FAIL_ON_ERROR": False}, ctx, fb)
assert res["OK"] is False and res["RAN"] == "1,2,3" and "did not finish: no dam line within 300 m" in res["RESULT"]
rc.processing.run = real_run
# item 6: work folder is auto-created when it doesn't exist
nope = os.path.join(work, "nope")
assert not os.path.isdir(nope)
res = alg.run_mock({"WORK": nope, "SETTINGS": "", "INCLUDE": [], "DRY_RUN": True, "LOG": ""}, ctx, fb)
assert os.path.isdir(nope), "work folder not created"
assert res["RESULT"] == "dry run"
print("OK (tool 15 headless chain)")

# ---------------------------------------------------------------- tool 13: inventory diff
S, D, I = "string", "double", "int"
F1 = [("dam_id", S), ("name", S), ("source", S), ("nid_id", S), ("height_m", D), ("inv_norm_m3", D),
      ("inv_max_m3", D), ("inv_area_m2", D), ("haz_existing", S), ("status", S), ("crest_len_m", D), ("shared_vol", S)]
pt = lambda x, y: QgsGeometry.fromPointXY(QgsPointXY(x, y))  # noqa: E731


def dam(did, x, y, h, v, cls="Low", status="Intact", name=None):
    return (pt(x, y), dict(dam_id=did, name=name or did, source="DWR", nid_id="", height_m=h, inv_norm_m3=v,
                           inv_max_m3=v * 1.3, inv_area_m2=5000.0, haz_existing=cls, status=status,
                           crest_len_m=100.0, shared_vol=""))


old = make_layer(F1, [dam("DWR-1", 400000, 1800000, 7.0, 100000), dam("DWR-2", 401000, 1800000, 3.0, 20000),
                      dam("DWR-3", 402000, 1800000, 4.0, 30000)], "point", "old")
new = make_layer(F1, [dam("DWR-1", 400000, 1800000, 7.0, 100000),                       # unchanged
                      dam("DWR-2", 401000, 1800000, 3.0 + 1.0, 20000),                   # 1 m (3.3 ft) higher
                      dam("DWR-4", 403000, 1800000, 2.0, 5000),                          # new
                      ], "point", "new")
dams_folder = os.path.join(work, "Dams")
for n in ("DWR-1", "DWR-2", "DWR-3"):
    os.makedirs(os.path.join(dams_folder, n))
    rec = dr.load(os.path.join(dams_folder, n), create=True)
    rec.set("dam_id", n, "ndscreen", "0.11.0")
    rec.add_status("screened", "ndscreen", "0.11.0", {"triage": "clear"}, when="2026-01-01T10:00:00")
    rec.save()
OLD, NEW = ctx.add(old), ctx.add(new)
out_csv = os.path.join(work, "changes.csv")
r = algs["inventorydiff"].createInstance().run_mock({"OLD": OLD, "NEW": NEW, "UNITS": 0, "HTOL_FT": 1.0,
                                                     "STOL_PCT": 10.0, "STOL_AF": 5.0, "MOVE_M": 50.0,
                                                     "DAMS_FOLDER": dams_folder, "OUT_CSV": out_csv,
                                                     "OUT": "TEMPORARY_OUTPUT"}, ctx, fb)
assert r["N_NEW"] == 1 and r["N_REMOVED"] == 1 and r["N_CHANGED"] == 1, r
rows = list(csv.DictReader(open(out_csv, newline="")))
by = {}
for row in rows:
    by.setdefault(row["dam_id"], []).append(row)
assert set(by) == {"DWR-2", "DWR-3", "DWR-4"}, set(by)
assert any(x["field"] == "height_ft" for x in by["DWR-2"]) or any("height" in x["field"] for x in by["DWR-2"]), by["DWR-2"]
# dam.json of the changed / removed dams says so and the suite's next action is "refresh"
r2 = dr.load(os.path.join(dams_folder, "DWR-2"))
assert "inventory_change" in r2.extra and r2.extra["inventory_change"]["when"]
assert dr.next_action(r2)[0] == "refresh" and "height" in dr.next_action(r2)[1]
r1 = dr.load(os.path.join(dams_folder, "DWR-1"))
assert "inventory_change" not in r1.extra and dr.next_action(r1)[0] == "none"
assert r["N_DAMJSON"] >= 1
feats = list(ctx.layers[r["OUT"]].getFeatures()) if r["OUT"] in ctx.layers else []
assert len(feats) == 3
print("OK (tool 13 inventory diff)")
shutil.rmtree(work)
print("OK")
