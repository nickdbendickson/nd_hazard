"""Tool 14 (self-test) through the mock: the synthetic valley runs tools 1-8 and 10 and the
report says PASS for every step; a broken step is reported with its step number."""
import os, shutil, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "qgis_mock"))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
from qgis.core import (QgsProcessingContext, QgsProcessingFeedback, QgsGeometry, QgsPointXY,  # noqa: E402
                       make_layer)
from qgis.PyQt.QtCore import QVariant  # noqa: E402
from qgis import processing  # noqa: E402
import nd_dam_screening as pkg  # noqa: E402
from nd_dam_screening.core import selftest as st  # noqa: E402
from nd_dam_screening.algorithms.selftest import SelfTest  # noqa: E402

plug = pkg.classFactory(None); plug.initGui()
algs = {a.name(): a for a in plug.provider.algs}
for a in algs.values():
    a.initAlgorithm()
processing.REGISTRY.update(algs)
assert "selftest" in algs and algs["selftest"].displayName().startswith("14 - Self-test")

TYPES = {"string": "string", "double": "double", "int": "int"}


def mock_layers(specs):
    out = {}
    for name, (kind, fields, feats) in specs.items():
        rows = []
        for (gkind, coords), attrs in feats:
            if gkind == "point":
                g = QgsGeometry.fromPointXY(QgsPointXY(*coords))
            elif gkind == "line":
                g = QgsGeometry.fromPolylineXY([QgsPointXY(*p) for p in coords])
            else:
                g = QgsGeometry.fromPolygonXY([[QgsPointXY(*p) for p in coords]])
            rows.append((g, attrs))
        out[name] = make_layer([(f, TYPES[t]) for f, t in fields], rows, kind, name)
    return out


# the world itself
specs = st.layer_specs()
assert set(specs) == {"dwr", "waterbodies", "damlines", "streams", "structures", "roads", "rail"}
zd, z, crest, pool = st.build_terrain()
assert zd.shape == (st.NY, st.NX) and crest - pool == 2.0 and zd[st.DAM_ROW, st.DAM_COL] == crest
dwr_attrs = specs["dwr"][2][0][1]
assert dwr_attrs["structure_"] == 1 and 22 < dwr_attrs["max_dam_he"] < 24       # 7 m in ft
assert dwr_attrs["normal_vol"] > 0 and dwr_attrs["normal_are"] > 0

# the tool, mock layers in place of memory layers, processing.run of the mock
work = tempfile.mkdtemp(prefix="selftest_")
ctx, fb = QgsProcessingContext(), QgsProcessingFeedback()
alg = algs["selftest"].createInstance()
alg.make_layers = mock_layers
report = os.path.join(work, "selftest_report.txt")
res = alg.run_mock({"ENGINE": 2, "CELL": 20.0, "SIMH": 0.5, "KEEP": True, "WORK": os.path.join(work, "w"),
                    "REPORT": report}, ctx, fb)
text = open(report, encoding="utf-8").read()
print(text)
assert res["OK"] is True, res
assert res["FAILED_STEP"] == "" and res["WORK"].endswith("w")
assert text.startswith("ND Dam Breach Screening self-test - PASSED")
for step in ("1", "2", "3", "4", "5", "6", "7", "8", "10", "16"):
    assert "PASS step {:>2s}".format(step) in text, step
assert "plugin: " in text and "python: " in text
assert os.path.isfile(os.path.join(work, "w", "7_simulations", "DWR-1_td_depth.tif"))
assert os.path.isfile(os.path.join(work, "w", "10_reports", "index.html"))

# a broken tool is reported with its step and the later steps are skipped
calls = []


def broken_run(aid, params):
    calls.append(aid)
    if aid == "nddam:estimatestorage":
        raise RuntimeError("GDAL could not open the DEM")
    return processing.run(aid, params, ctx, fb)


layers = mock_layers(specs)
w2 = os.path.join(work, "w2"); os.makedirs(w2)
dem_folder, _p = st.write_dem(os.path.join(w2, "dem"))
r = st.run_selftest(w2, layers, dem_folder, broken_run, log=lambda m: None, sim_h=0.5)
assert r["ok"] is False and [x["status"] for x in r["rows"]] == ["PASS", "PASS", "FAIL"]
assert "RuntimeError: GDAL could not open the DEM" in r["rows"][2]["problem"]
assert "FAILED" in r["report"] and "skip step  4" in r["report"] and "skip step 10" in r["report"]
assert calls == ["nddam:preparedams", "nddam:terraincache", "nddam:estimatestorage"]

# the report file is optional (TEMPORARY_OUTPUT) and the temp folder is removed unless kept
alg2 = algs["selftest"].createInstance(); alg2.make_layers = mock_layers
res2 = alg2.run_mock({"ENGINE": 2, "CELL": 20.0, "SIMH": 0.5, "KEEP": False, "WORK": "", "REPORT": "TEMPORARY_OUTPUT"},
                     ctx, QgsProcessingFeedback())
assert res2["OK"] is True and res2["WORK"] == ""
shutil.rmtree(work)
print("OK (self-test)")
