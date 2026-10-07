"""suite_data_tools (1.5.0): tools fill their empty inputs from the dam folder, and the Get data
tool - with the simulated QGIS (tests/fake_qgis.py). Run: python3 tests/test_suite_data_tools.py
(the Runner's run_tests.py also picks it up through tests.registry)."""
import importlib
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_DIR = os.path.dirname(HERE)
if os.path.dirname(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, os.path.dirname(PLUGIN_DIR))
from nd_breach_runner.tests import fake_qgis  # noqa: E402

CHECKS = []


def check(fn):
    CHECKS.append(fn)
    return fn


def _modules():
    fake_qgis.install()
    import nd_breach_runner.suite_data_tools as SDT
    importlib.reload(SDT)
    from nd_breach_runner.damsuite import dam_record as DR, damdata as DD
    return SDT, DR, DD


def _dam(tmp, did="ND00123", data=None, fields=None):
    folder = os.path.join(tmp, did)
    os.makedirs(os.path.join(folder, "data"), exist_ok=True)
    rec = {"format": "nd_dam_suite/dam_record", "format_version": 1, "fields": dict(fields or {}),
           "sources": {}, "status": [], "conflicts": []}
    rec["fields"].setdefault("dam_id", did)
    if data:
        rec["data"] = data
        for e in data.values():
            p = os.path.join(folder, e["path"].split("|")[0])
            open(p, "w").close()
    with open(os.path.join(folder, "dam.json"), "w") as f:
        json.dump(rec, f)
    return folder


def _tool(SDT, required=("DEM",)):
    core = sys.modules["qgis.core"]

    class Tool(core.QgsProcessingAlgorithm):
        seen = None

        def name(self):
            return "tool"

        def initAlgorithm(self, config=None):
            self.addParameter(core.QgsProcessingParameterFile("DAMS_FOLDER", "Dams"))
            self.addParameter(core.QgsProcessingParameterString("DAM_ID", "ID"))
            self.addParameter(core.QgsProcessingParameterRasterLayer("DEM", "DEM"))
            self.addParameter(core.QgsProcessingParameterFeatureSource("STRUCTURES", "S",
                                                                       optional=True))
            self.addParameter(core.QgsProcessingParameterNumber("NORMAL_POOL_FT", "pool",
                                                                optional=True))

        def processAlgorithm(self, parameters, context, feedback):
            Tool.seen = dict(parameters)
            return {}

    return SDT.data_aware(Tool, {"DEM": "dem", "STRUCTURES": "structures", "NOPE": "roads"},
                          fields={"NORMAL_POOL_FT": "normal_pool_ft"}, required=required)


@check
def empty_inputs_fill_from_the_dam_folder_and_choices_are_kept():
    SDT, DR, DD = _modules()
    tmp = tempfile.mkdtemp()
    try:
        _dam(tmp, data={"dem": {"path": "data/dem.vrt"},
                        "structures": {"path": "data/suite_package.gpkg|layername=structures"}},
             fields={"normal_pool_ft": 1650.5})
        cls = _tool(SDT)
        alg = cls()
        alg.initAlgorithm()
        assert getattr(alg.parameterDefinition("DEM"), "optional", False), "DEM made optional"
        fb = fake_qgis.QgsProcessingFeedback()
        alg.processAlgorithm({"DAMS_FOLDER": tmp, "DAM_ID": "ND00123", "DEM": None}, None, fb)
        got = cls.seen
        assert got["DEM"].endswith(os.path.join("ND00123", "data", "dem.vrt")), got
        assert got["STRUCTURES"].endswith("suite_package.gpkg|layername=structures"), got
        assert got["NORMAL_POOL_FT"] == 1650.5, got
        assert "NOPE" not in got
        # the user's own DEM is never replaced
        alg.processAlgorithm({"DAMS_FOLDER": tmp, "DAM_ID": "ND00123", "DEM": "/my/dem.tif",
                              "NORMAL_POOL_FT": 1600.0}, None, fb)
        assert cls.seen["DEM"] == "/my/dem.tif" and cls.seen["NORMAL_POOL_FT"] == 1600.0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@check
def a_required_input_nobody_gives_stops_with_a_clear_message():
    SDT, DR, DD = _modules()
    tmp = tempfile.mkdtemp()
    try:
        _dam(tmp)                                           # no data section
        alg = _tool(SDT)()
        alg.initAlgorithm()
        try:
            alg.processAlgorithm({"DAMS_FOLDER": tmp, "DAM_ID": "ND00123"}, None,
                                 fake_qgis.QgsProcessingFeedback())
        except fake_qgis.QgsProcessingException as e:
            assert "DEM needed" in str(e) and "Get data for this dam" in str(e), e
        else:
            raise AssertionError("no error without a DEM")
        # no dam named at all: the tool runs as before when the user gives the DEM
        alg.processAlgorithm({"DEM": "/x.tif"}, None, fake_qgis.QgsProcessingFeedback())
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@check
def the_get_data_tool_runs_every_dam_of_a_folder():
    SDT, DR, DD = _modules()
    core = sys.modules["qgis.core"]
    tmp = tempfile.mkdtemp()
    calls = []
    old = DD.get_data_for_dam
    try:
        _dam(tmp, "A")
        _dam(tmp, "B")

        def fake(folder, **k):
            calls.append((os.path.basename(folder), k["data_folder"], k["overwrite"]))
            return {"done": {"dem": {}}, "failed": {"roads": "server down"}, "skipped": {}}
        DD.get_data_for_dam = fake
        cls = SDT.get_data_algorithm(core.QgsProcessingAlgorithm, "Dam suite", "suite")
        alg = cls()
        alg.initAlgorithm()
        assert alg.name() == "getdamdata" and alg.groupId() == "suite"
        res = alg.processAlgorithm({"DAMS_FOLDER": tmp, "DATA_FOLDER": tmp, "OVERWRITE": True},
                                   None, fake_qgis.QgsProcessingFeedback())
        assert [c[0] for c in calls] == ["A", "B"] and all(c[2] for c in calls), calls
        assert "roads failed: server down" in res["SUMMARY"] and res["DATA"] == "", res
        calls[:] = []
        res = alg.processAlgorithm({"DAMS_FOLDER": tmp, "DAM_ID": "B", "DATA_FOLDER": tmp},
                                   None, fake_qgis.QgsProcessingFeedback())
        assert [c[0] for c in calls] == ["B"] and res["DATA"].endswith(os.path.join("B", "data"))
    finally:
        DD.get_data_for_dam = old
        shutil.rmtree(tmp, ignore_errors=True)


def run_all():
    failed = 0
    for fn in CHECKS:
        try:
            fn()
            print("ok    ", fn.__name__)
        except Exception:                                        # noqa
            import traceback
            failed += 1
            print("FAIL  ", fn.__name__)
            traceback.print_exc()
    print("{} checks, {} failed".format(len(CHECKS), failed))
    return failed


if __name__ != "__main__":
    from nd_breach_runner.tests.registry import test as _register
    for _fn in CHECKS:
        _register(_fn)

if __name__ == "__main__":
    sys.exit(1 if run_all() else 0)
