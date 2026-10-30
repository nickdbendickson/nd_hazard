"""0.33.1: the one-dam (Crest ND00199) chain findings of 2026-10-30, as in DamSafety Screening 0.18.1.

(b) the preflight disk estimate counts only the only_ids dams, not the whole DWR layer;
(c) tool 15 writes its chain log line by line, so a killed run leaves one.
(Step 1 here already took DWR / NID from slot_source.)
"""
import json, os, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "qgis_mock"))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
sys.path.insert(0, HERE)
from qgis.core import QgsProcessingContext, QgsProcessingFeedback  # noqa: E402
from qgis import processing  # noqa: E402
import nd_dam_screening as pkg  # noqa: E402
from nd_dam_screening.core import workflow as wf, preflight as pf  # noqa: E402
from nd_dam_screening.algorithms import run_chain as rc  # noqa: E402

W = tempfile.mkdtemp(prefix="onedam_")
DWR = os.path.join(W, "DWR_dams.gpkg") + "|layername=DWR_dams"

# ---- (b) the disk estimate: the only_ids dams, not the whole layer
assert pf.only_ids_count({"only_ids": "ND00199"}) == 1
assert pf.only_ids_count({"only_ids": " nd00199, DWR-2801 ,ND00199"}) == 2
assert pf.only_ids_count({"only_ids": ["a", "b"]}) == 2
assert pf.only_ids_count({"only_ids": ""}) == 0 and pf.only_ids_count({}) == 0


class Env(pf.Env):
    def url_ok(self, url):
        return True, "stubbed"

    def disk_free(self, path):
        return 1 * 1024 ** 3                                     # 1 GB: short for 10,088 dams

    def layer_info(self, ref):
        if ref == DWR:
            return {"fields": ["structure_"], "count": 10088, "crs": "EPSG:4326", "geometry": "point"}
        return None


def disk(settings):
    c = {x["title"]: x for x in pf.run_checks(W, dict(wf.DEFAULT_SETTINGS, **settings), steps=["1"], env=Env())}
    return c["Disk space"]


d = disk({"dwr": DWR})
assert "10088 dams" in d["detail"] and d["status"] == pf.WARN, d
d = disk({"dwr": DWR, "only_ids": "ND00199"})
assert "for 1 dams" in d["detail"] and "10088" not in d["detail"], d

# ---- (c) the chain log while the chain runs
plug = pkg.classFactory(None); plug.initGui()
algs = {a.name(): a for a in plug.provider.algs}
for a in algs.values():
    a.initAlgorithm()
processing.REGISTRY.update(algs)
LOG = os.path.join(W, "chain_log.txt")
seen = []


def killed(aid, params, context=None, feedback=None, is_child_algorithm=False):
    seen.append((aid, dict(params), open(LOG, encoding="utf-8").read()))
    raise RuntimeError("killed")


real_run = rc.processing.run
rc.processing.run = killed
try:
    alg = algs["runchain"].createInstance()
    res = alg.run_mock({"WORK": W, "SETTINGS": json.dumps({"dwr": DWR}), "DAM_ID": "ND00199",
                        "PREFLIGHT": False, "DRY_RUN": False, "FAIL_ON_ERROR": False, "LOG": LOG},
                       QgsProcessingContext(), QgsProcessingFeedback())
finally:
    rc.processing.run = real_run
assert res["OK"] is False and seen, res
aid, params, mid = seen[0]
assert aid.endswith(wf.ALG["1"]) and params["ONLY_IDS"] == "ND00199", params
assert "DAM_ID filter: ND00199" in mid and "Plan:" in mid, mid     # on disk before step 1 ended
assert "killed" in open(LOG, encoding="utf-8").read()
print("ok test_onedam_v0325")
