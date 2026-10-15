"""2.28.1: the chart sign-off needs one engineer, and the suite ships the chart file signed off
by Nicholas Bendickson (damsuite.tm11_curves.default_chart_file).
Run: python3 -m ND_Dam_Hazard_Helper.tests.run_tests (from the folder above the plugin)."""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_DIR = os.path.dirname(HERE)
if os.path.dirname(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, os.path.dirname(PLUGIN_DIR))
from ND_Dam_Hazard_Helper.tests import fake_qgis  # noqa: E402
from ND_Dam_Hazard_Helper.tests.registry import test  # noqa: E402

fake_qgis.install()
from ND_Dam_Hazard_Helper.damsuite import tm11_curves as tm  # noqa: E402


@test
def default_chart_file_is_signed_off_by_the_engineer():
    d = tm.default_chart_file()
    assert d["status"] == "digitized"
    assert d["signed_off_by"] == "Nicholas Bendickson" == d["digitized_by"] == tm.SIGNED_OFF_BY
    assert d["signed_off_on"] == "2026-10-12" == tm.SIGNED_OFF_ON and d["checked_by"] == ""
    assert "Signed off by Nicholas Bendickson on 2026-10-12" in d["notes"]
    assert set(d["charts"]) == {"house", "mobile"}
    assert tm.chart_file()["status"] == "not digitized"              # nobody named
    assert tm.chart_file(checked_by="A. Engineer")["status"] == "digitized"
    one = tm.chart_file(digitized_by="A. Engineer", digitized_on="2026-01-02")
    assert one["signed_off_by"] == "A. Engineer" and one["signed_off_on"] == "2026-01-02"


@test
def chart_sign_off_tool_accepts_one_engineer():
    from ND_Dam_Hazard_Helper import chartsign as CS
    assert CS.REQUIRE_DIFFERENT_CHECKER is False
    assert CS.signoff_status("A", "")[0] == "digitized"
    assert CS.signoff_status("A", "a")[0] == "digitized"
    assert CS.signoff_status("A", "B")[0] == "digitized"
    assert CS.signoff_status("", "")[0] == "not digitized"
    assert CS.signoff_status("A", "", require_different=True)[0] == "not digitized"
    assert CS.signoff_status("A", "A", require_different=True)[0] == "not digitized"
    assert CS.signoff_status("A", "B", require_different=True)[0] == "digitized"
    d = tempfile.mkdtemp()
    info = CS.write_chart_file(d, digitized_by="A. Engineer", digitized_on="2026-01-02")
    j = json.load(open(info["path"], encoding="utf-8"))
    assert info["ready_expected"] and j["status"] == "digitized"
    assert j["signed_off_by"] == "A. Engineer" and j["signed_off_on"] == "2026-01-02"
    # install_in_plugins copies the file where it is told to and reports what it could not
    fake = {"nd_breach_runner": os.path.join(d, "r", "core", "acer_tm11_charts.json")}
    os.makedirs(os.path.dirname(fake["nd_breach_runner"]))
    res = CS.install_in_plugins(info["path"], paths=fake)
    assert res["installed"] == [fake["nd_breach_runner"]] and res["missing"] == ["nd_dam_screening"]
    assert json.load(open(fake["nd_breach_runner"], encoding="utf-8"))["signed_off_by"] == "A. Engineer"


@test
def sibling_plugins_ship_the_default_chart_file():
    base = os.path.dirname(PLUGIN_DIR)
    found = 0
    for name in ("nd_breach_runner", "nd_dam_screening"):
        p = os.path.join(base, name, "core", "acer_tm11_charts.json")
        if not os.path.isfile(p):
            continue                                   # only when installed next to this plugin
        j = json.load(open(p, encoding="utf-8"))
        assert j["status"] == "digitized" and j["signed_off_by"] == "Nicholas Bendickson", p
        assert j["charts"] == tm.default_chart_file()["charts"], p
        found += 1
    print("sibling chart files checked: {}".format(found))
