"""2.34.6: compat.legend_manual on a real QGIS (3.x: setAutoUpdateModel(False); 4.x:
setSyncMode(Manual), no DeprecationWarning). Run by tests/test_legend_sync.py in its own
process, or directly:

    QT_QPA_PLATFORM=offscreen python3 ND_Dam_Hazard_Helper/tests/realqt_legend_sync.py
"""
import os
import sys
import warnings

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
try:
    from qgis.core import (QgsApplication, QgsLayout, QgsLayoutItemLegend, QgsProject,
                           QgsVectorLayer)
except ImportError:
    print("SKIP: no real QGIS")
    sys.exit(0)

app = QgsApplication([], False)
app.initQgis()
from ND_Dam_Hazard_Helper.compat import legend_manual  # noqa: E402

FAILS = []


def check(what, ok, got=""):
    print("{:4} {}{}".format("ok" if ok else "FAIL", what, "" if ok else "  -> {!r}".format(got)))
    if not ok:
        FAILS.append(what)


proj = QgsProject.instance()
a = QgsVectorLayer("Point?crs=EPSG:4326", "a", "memory")
b = QgsVectorLayer("Point?crs=EPSG:4326", "b", "memory")
proj.addMapLayers([a, b])
layout = QgsLayout(proj)
leg = QgsLayoutItemLegend(layout)
with warnings.catch_warnings(record=True) as got:
    warnings.simplefilter("always")
    legend_manual(leg)
dep = [str(w.message) for w in got if issubclass(w.category, DeprecationWarning)]
check("no DeprecationWarning", not dep, dep)
if hasattr(leg, "syncMode"):
    check("QGIS 4: sync mode is Manual", "Manual" in repr(leg.syncMode()), leg.syncMode())
else:
    check("QGIS 3: auto update off", leg.autoUpdateModel() is False)
root = leg.model().rootGroup()
root.removeAllChildren()
root.addLayer(a)
proj.addMapLayer(QgsVectorLayer("Point?crs=EPSG:4326", "c", "memory"))
names = [n.name() for n in root.children()]
check("the legend keeps its own layer list", names == ["a"], names)
print("\n{} failure(s)".format(len(FAILS)) if FAILS else "\nall checks passed")
sys.stdout.flush()
os._exit(1 if FAILS else 0)                # offscreen QGIS can crash on teardown
