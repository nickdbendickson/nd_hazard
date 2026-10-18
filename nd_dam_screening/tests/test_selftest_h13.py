"""H13: tool 14 (self-test) must never delete the user's WORK folder or anything it already holds.
With KEEP off it may remove only what the self-test itself created. This reproduces the old
shutil.rmtree(work, ...) bug, which wiped the whole user-chosen WORK root - pre-existing files and
all - whenever KEEP was off (the default)."""
import os, shutil, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "qgis_mock"))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
from qgis.core import (QgsProcessingContext, QgsProcessingFeedback, QgsGeometry, QgsPointXY,  # noqa: E402
                       make_layer)
from qgis import processing  # noqa: E402
import nd_dam_screening as pkg  # noqa: E402
from nd_dam_screening.core import selftest as st  # noqa: E402

plug = pkg.classFactory(None); plug.initGui()
algs = {a.name(): a for a in plug.provider.algs}
for a in algs.values():
    a.initAlgorithm()
processing.REGISTRY.update(algs)
assert "selftest" in algs

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


root = tempfile.mkdtemp(prefix="selftest_h13_")
try:
    # a user-chosen WORK folder that already holds the engineer's own work, run with KEEP off
    user_work = os.path.join(root, "my_project")
    os.makedirs(user_work)
    keep_file = os.path.join(user_work, "important.txt")
    with open(keep_file, "w", encoding="utf-8") as fh:
        fh.write("do not delete me")
    keep_dir = os.path.join(user_work, "my_data")
    os.makedirs(keep_dir)
    with open(os.path.join(keep_dir, "x.csv"), "w", encoding="utf-8") as fh:
        fh.write("1,2,3")

    ctx, fb = QgsProcessingContext(), QgsProcessingFeedback()
    alg = algs["selftest"].createInstance(); alg.make_layers = mock_layers
    res = alg.run_mock({"ENGINE": 2, "CELL": 20.0, "SIMH": 0.5, "KEEP": False,
                        "WORK": user_work, "REPORT": "TEMPORARY_OUTPUT"}, ctx, fb)

    # the user's folder and everything that was already in it must survive (the bug deleted them)
    assert os.path.isdir(user_work), "the self-test deleted the user's WORK folder"
    assert os.path.isfile(keep_file), "the self-test deleted a pre-existing user file"
    assert open(keep_file, encoding="utf-8").read() == "do not delete me", "pre-existing user file was altered"
    assert os.path.isfile(os.path.join(keep_dir, "x.csv")), "the self-test deleted a pre-existing user subfolder"
    # KEEP off still means the self-test tidies up what IT wrote (write_dem always makes <work>/dem)
    assert not os.path.isdir(os.path.join(user_work, "dem")), "self-test left its own 'dem' output behind"
    assert res["WORK"] == "", res

    # a temp folder the self-test created itself is still removed wholesale with KEEP off
    ctx2, fb2 = QgsProcessingContext(), QgsProcessingFeedback()
    alg2 = algs["selftest"].createInstance(); alg2.make_layers = mock_layers
    res2 = alg2.run_mock({"ENGINE": 2, "CELL": 20.0, "SIMH": 0.5, "KEEP": False,
                          "WORK": "", "REPORT": "TEMPORARY_OUTPUT"}, ctx2, fb2)
    assert res2["WORK"] == "", res2
finally:
    shutil.rmtree(root, ignore_errors=True)

print("OK (self-test H13: the user's WORK folder is never deleted)")
