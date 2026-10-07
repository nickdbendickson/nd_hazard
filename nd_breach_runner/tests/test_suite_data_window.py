"""The "Suite data and servers" window (suite_data.py, the same in the three plugins) with the
behaviour-recording Qt doubles of test_popup: it opens on the suite folders, lists every kind of
data with its servers, keeps a server choice, adds a server of the user's, and runs Get data for
the chosen dam. No network: the added server is a tile address (nothing to list), and the dam
data step is replaced by a recorder."""
import importlib
import json
import os
import shutil
import sys
import tempfile

from nd_breach_runner.tests.registry import test


def _with_window(fn):
    from nd_breach_runner.tests import fake_qgis
    if "qgis.core" not in sys.modules or not hasattr(sys.modules["qgis.core"], "QgsProcessingAlgorithm"):
        fake_qgis.install()
    from nd_breach_runner.tests.test_popup import qt_fakes
    from nd_breach_runner.damsuite import suitedata as SD
    tmp = tempfile.mkdtemp(prefix="suitewin_")
    old_env = (os.environ.get(SD.ENV_DATA), os.environ.get(SD.ENV_DAMS), SD.CONFIG)
    data, dams = os.path.join(tmp, "data"), os.path.join(tmp, "dams")
    os.makedirs(os.path.join(dams, "ND00123"))
    with open(os.path.join(dams, "ND00123", "dam.json"), "w") as f:
        json.dump({"format": "nd_dam_suite/dam_record", "format_version": 1,
                   "fields": {"dam_id": "ND00123", "lon": -100.5, "lat": 47.1}, "sources": {},
                   "status": [], "conflicts": []}, f)
    SD.ensure_layout(data)
    os.environ[SD.ENV_DATA], os.environ[SD.ENV_DAMS] = data, dams
    SD.CONFIG = os.path.join(tmp, "config.json")
    try:
        with qt_fakes() as q:
            sys.modules.pop("nd_breach_runner.suite_data", None)
            ui = importlib.import_module("nd_breach_runner.suite_data")
            try:
                fn(q, ui, data, dams)
            finally:
                sys.modules.pop("nd_breach_runner.suite_data", None)
    finally:
        for k, v in ((SD.ENV_DATA, old_env[0]), (SD.ENV_DAMS, old_env[1])):
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        SD.CONFIG = old_env[2]
        shutil.rmtree(tmp, ignore_errors=True)


@test
def suite_data_window_lists_servers_keeps_choices_and_gets_dam_data():
    def body(q, ui, data, dams):
        SV, DD = ui.SV, ui.DD
        holder = type("P", (), {})()
        dlg = ui.open_suite_data(holder, None, "ndbreach",
                                 dam_folder=os.path.join(dams, "ND00123"))
        assert dlg.data_edit.text() == data and dlg.dams_edit.text() == dams
        assert dlg.dam_combo.count() == 1 and dlg.dam_combo.currentText() == "ND00123"
        assert dlg.table.rowCount() == len(SV.ROLES)
        # a server choice is saved in the data folder's services.json
        combo = dlg.combos["nlcd"]
        assert combo.count() >= 2
        combo.setCurrentIndex(1)
        saved = json.load(open(os.path.join(data, SV.SERVICES_FILE)))
        assert saved["choice"]["nlcd"] == combo.itemData(1)
        # the user's own tile server for imagery (no layer list to ask the server for)
        row = list(SV.ROLES).index("imagery")
        dlg.table.setCurrentCell(row, 0)
        label = "{} ({})".format(SV.ROLES["imagery"][0], "imagery")
        q.QtWidgets.QInputDialog.NEXT[:] = ["https://tiles.example.org/{z}/{x}/{y}.png", label,
                                            "My tiles"]
        s = dlg.add_server()
        assert s and s["kind"] == "xyz" and SV.load().chosen("imagery")["id"] == s["id"]
        # Get data for this dam runs the dam data step with the window's choices
        calls = []

        def fake(folder, **k):
            calls.append((folder, k))
            return {"done": {"dem": {}}, "failed": {"roads": "server down"}, "skipped": {}}
        old = DD.get_data_for_dam
        DD.get_data_for_dam = fake
        try:
            dlg.overwrite_chk.setChecked(True)
            assert dlg.get_data(all_dams=False)
        finally:
            DD.get_data_for_dam = old
        assert len(calls) == 1 and calls[0][0].endswith("ND00123")
        assert calls[0][1]["data_folder"] == data and calls[0][1]["overwrite"] is True
        log = "\n".join(str(x) for x in getattr(dlg.log_text, "lines", []))
        assert "1 written, 1 failed" in log and "roads failed: server down" in log, log
        # the same window comes back for the same plugin object
        assert ui.open_suite_data(holder, None, "ndbreach") is dlg
    _with_window(body)
