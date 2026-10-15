"""2.28.0: the plugin version comes from metadata.txt, the flood cut-off marker in the summary,
Dam suite home starting an installed-but-not-loaded plugin, and the export gap wording.
Run: python3 -m ND_Dam_Hazard_Helper.tests.run_tests (from the folder above the plugin)."""
import configparser
import json
import os
import shutil
import sys
import tempfile
import types

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_DIR = os.path.dirname(HERE)
if os.path.dirname(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, os.path.dirname(PLUGIN_DIR))
from ND_Dam_Hazard_Helper.tests import fake_qgis  # noqa: E402
from ND_Dam_Hazard_Helper.tests.registry import test  # noqa: E402

fake_qgis.install()


def _metadata_version():
    cp = configparser.ConfigParser(interpolation=None, strict=False)
    with open(os.path.join(PLUGIN_DIR, "metadata.txt"), encoding="utf-8-sig") as fh:
        cp.read_file(fh)
    return cp.get("general", "version").strip()


# ------------------------------------------------------------------- version --
@test
def plugin_version_is_the_metadata_version():
    from ND_Dam_Hazard_Helper import constants as C
    want = _metadata_version()
    assert want and want[0].isdigit(), want
    assert C.PLUGIN_VERSION == want, (C.PLUGIN_VERSION, want)
    assert C.HTTP_USER_AGENT.endswith("/" + want), C.HTTP_USER_AGENT
    # the modules that print the version carry the same one
    from ND_Dam_Hazard_Helper import classification, chartsign
    assert classification.PLUGIN_VERSION == want
    assert chartsign.PLUGIN_VERSION == want
    # the reader itself: a missing file gives the fallback; an indented "version=" in the
    # changelog is not the version
    assert C._version_from_metadata("9.9.9", path=os.path.join(PLUGIN_DIR, "no_such.txt")) == "9.9.9"
    d = tempfile.mkdtemp()
    try:
        p = os.path.join(d, "metadata.txt")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write("[general]\nname=x\nchangelog=\n    version=0.0.1 - not it\nversion = 4.5.6 \n")
        assert C._version_from_metadata("x", path=p) == "4.5.6"
    finally:
        shutil.rmtree(d)


# ------------------------------------------------------------ flood cut-off --
def _nd():
    return {"cls": "High", "reasons": ["HIGH - loss of life probable: 2 dwelling(s) [5.2.1]"],
            "judgment": [], "criteria": []}


def _tm11():
    return {"cls": "High", "lij": 7.0, "lij_structures_high": 7, "lij_structures_judgment": 0,
            "judgment_included": True, "lij_vehicles_high": 0.0, "lij_vehicles_judgment": 0.0,
            "cls_without_judgment": "High", "notes": []}


def _summary(folder, sources=None):
    """The summary text the way the pipeline builds it: the Runner's report read from the dam
    folder (modelchain.read_flood_cut_off) and handed to classification.summary_lines."""
    from ND_Dam_Hazard_Helper import modelchain as MC
    from ND_Dam_Hazard_Helper.classification import summary_lines
    info = MC.read_flood_cut_off(folder)
    extra = {"flood_cut_off": info} if info else {}
    nd = _nd()
    lines = summary_lines(nd, _tm11(), [], [], [], "Test Dam", list(sources or []), False,
                          extra=extra)
    assert nd["cls"] == "High"                       # the class itself is never changed
    return info, lines


def _class_line(lines):
    hits = [x for x in lines if x.startswith("ND HAZARD CLASSIFICATION")]
    assert len(hits) == 1, hits
    return hits[0]


def _write(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh)


@test
def summary_marks_a_flood_model_the_runner_cut_off():
    from ND_Dam_Hazard_Helper import modelchain as MC
    d = tempfile.mkdtemp()
    try:
        dam = os.path.join(d, "ND00001")
        report = os.path.join(dam, "_breach_model", "run_report.json")
        # no dam folder / no report: nothing
        assert MC.read_flood_cut_off(None) is None
        assert MC.read_flood_cut_off(os.path.join(d, "nowhere")) is None
        os.makedirs(dam)
        info, lines = _summary(dam)
        assert info is None
        assert _class_line(lines) == "ND HAZARD CLASSIFICATION (screening): HIGH"
        # a report without the key: nothing changes
        _write(report, {"dam": {"dam_id": "ND00001"}, "settings": {"duration_h": 6}})
        info, lines = _summary(dam)
        assert info is None
        assert _class_line(lines) == "ND HAZARD CLASSIFICATION (screening): HIGH"
        assert not any("INCOMPLETE" in x or "cut off" in x for x in lines), lines
        # the key: the class line is marked and the data sources say why
        _write(report, {"dam": {"dam_id": "ND00001"},
                        "cut_off": {"reason": "duration cap",
                                    "detail": "water still moving at 6 h (max 6 h)"}})
        info, lines = _summary(dam, sources=["Depth raster: depth.tif"])
        assert info and info["reason"] == "duration cap", info
        assert info["path"] == report
        assert _class_line(lines) == ("ND HAZARD CLASSIFICATION (screening): HIGH  |  "
                                      "incomplete: flood cut off (duration cap)")
        text = "\n".join(lines)
        i = lines.index("DATA SOURCES")
        src = lines[i + 1:]
        assert any("stopped the run while water was still moving (duration cap; water still "
                   "moving at 6 h (max 6 h))" in x for x in src), src
        assert any("may be low" in x for x in src), src
        assert text.count("Flood model INCOMPLETE") == 1, text
        assert "Depth raster: depth.tif" in text
        # the pipeline already added the sentence to the sources: not twice
        from ND_Dam_Hazard_Helper.classification import flood_cut_off_note
        note = flood_cut_off_note(info)
        _info, lines2 = _summary(dam, sources=[note])
        assert "\n".join(lines2).count("Flood model INCOMPLETE") == 1
        # the portfolio / form fill still read the class alone from the marked line
        from ND_Dam_Hazard_Helper import portfolio as PF
        sf = PF.summary_facts(PF.parse_summary(text))
        assert sf.get("nd") == "High", sf
        # no em-dash anywhere in what the engineer reads
        assert "\u2014" not in text
    finally:
        shutil.rmtree(d)


@test
def runner_report_is_found_through_dam_json_first():
    """dam.json's newest "modelled" status names the report (the Runner's result "run_report"
    is relative to the dam folder); that one wins over _breach_model/run_report.json."""
    from ND_Dam_Hazard_Helper import modelchain as MC
    d = tempfile.mkdtemp()
    try:
        dam = os.path.join(d, "ND00002")
        _write(os.path.join(dam, "_breach_model", "run_report.json"), {"dam": {}})
        _write(os.path.join(dam, "runs", "r2", "run_report.json"),
               {"cut_off": {"reason": "largest domain side", "detail": "20 mi"}})
        rec = {"format": "nd_dam_suite/dam_record", "format_version": 1,
               "fields": {"dam_id": "ND00002"}, "sources": {}, "conflicts": [],
               "status": [{"step": "modelled", "tool": "ndbreach", "version": "3.0.0",
                           "when": "2026-01-01T00:00:00",
                           "result": {"run_report": "_breach_model/run_report.json"}},
                          {"step": "modelled", "tool": "ndbreach", "version": "3.0.0",
                           "when": "2026-02-01T00:00:00",
                           "result": {"run_report": "runs/r2/run_report.json"}}]}
        _write(os.path.join(dam, "dam.json"), rec)
        assert MC.find_run_report(dam) == os.path.join(dam, "runs", "r2", "run_report.json")
        info = MC.read_flood_cut_off(dam)
        assert info["reason"] == "largest domain side" and info["detail"] == "20 mi", info
        # a report file given directly, and a bare true value
        _write(os.path.join(dam, "x", "run_report.json"), {"cut_off": True})
        assert MC.read_flood_cut_off(os.path.join(dam, "x", "run_report.json"))["reason"] == \
            "reason not given"
        # a broken dam.json: the model folder is still looked at
        with open(os.path.join(dam, "dam.json"), "w") as fh:
            fh.write("{not json")
        assert MC.find_run_report(dam) == os.path.join(dam, "_breach_model", "run_report.json")
        assert MC.read_flood_cut_off(dam) is None
    finally:
        shutil.rmtree(d)


@test
def pipeline_step_logs_and_lists_the_cut_off():
    """pipeline._flood_cut_off_step with a stand-in run: the log and the data sources."""
    from ND_Dam_Hazard_Helper import pipeline as PL
    d = tempfile.mkdtemp()
    try:
        dam = os.path.join(d, "ND00003")
        _write(os.path.join(dam, "_breach_model", "run_report.json"),
               {"cut_off": {"reason": "domain-run limit", "detail": "8 domains"}})
        run = types.SimpleNamespace(sources=[], logged=[], flood_cut_off="stale")
        run.log = run.logged.append
        info = PL._flood_cut_off_step(run, {"dam_folder": dam})
        assert info["reason"] == "domain-run limit"
        assert run.flood_cut_off is info
        assert len(run.sources) == 1 and "domain-run limit; 8 domains" in run.sources[0]
        assert run.logged and run.logged[0].startswith(">>> WARNING: Flood model INCOMPLETE")
        # no report: nothing added, the stale value cleared
        run2 = types.SimpleNamespace(sources=[], logged=[], flood_cut_off="stale")
        run2.log = run2.logged.append
        os.remove(os.path.join(dam, "_breach_model", "run_report.json"))
        assert PL._flood_cut_off_step(run2, {"dam_folder": dam}) is None
        assert run2.sources == [] and run2.flood_cut_off is None
        assert PL._flood_cut_off_step(run2, {}) is None
    finally:
        shutil.rmtree(d)


# ----------------------------------------------------------------- suite home --
class _Label:
    def __init__(self):
        self._t = "Installed: ND Dam Breach Runner 3.0.0."

    def text(self):
        return self._t

    def setText(self, t):
        self._t = t


@test
def suite_home_starts_a_plugin_that_is_installed_but_not_loaded():
    from ND_Dam_Hazard_Helper import suite_home as SHW
    import qgis.utils as qu
    saved = {k: getattr(qu, k, None) for k in ("plugins", "loadPlugin", "startPlugin")}
    try:
        runner = types.SimpleNamespace(run_plugin=lambda: None)
        installed = {"nd_breach_runner": runner}
        calls = []
        qu.plugins = {}

        def loadPlugin(name):
            calls.append(("load", name))
            return name in installed

        def startPlugin(name):
            calls.append(("start", name))
            if name in installed and name not in qu.plugins:
                qu.plugins[name] = installed[name]
                return True
            return False
        qu.loadPlugin, qu.startPlugin = loadPlugin, startPlugin
        # installed, not loaded: loaded + started, and said so
        assert SHW.load_plugin_object("nd_breach_runner") == (runner, True)
        assert calls == [("load", "nd_breach_runner"), ("start", "nd_breach_runner")], calls
        # already loaded: handed back, nothing started
        assert SHW.load_plugin_object("nd_breach_runner") == (runner, False)
        assert len(calls) == 2
        # not installed: None, no start attempted
        assert SHW.load_plugin_object("nd_dam_screening") == (None, False)
        assert calls[-1] == ("load", "nd_dam_screening")
        # loadPlugin raising (an old QGIS, a broken plugin): None, never an exception
        qu.loadPlugin = lambda name: (_ for _ in ()).throw(RuntimeError("boom"))
        assert SHW.load_plugin_object("nd_dam_screening") == (None, False)
        # the window's status line
        win = types.SimpleNamespace(plugins_label=_Label())
        SHW.SuiteHomeDialog._note_started(win, "ndbreach")
        t = win.plugins_label.text()
        assert t.startswith("Installed: ND Dam Breach Runner 3.0.0.") and \
            "ND Dam Breach Runner was installed but not loaded: it has been started." in t, t
        # _plugin_object ties the two together
        qu.loadPlugin, qu.plugins = loadPlugin, {}
        win2 = types.SimpleNamespace(plugins_label=_Label(),
                                     _note_started=lambda key: SHW.SuiteHomeDialog._note_started(win2, key))
        assert SHW.SuiteHomeDialog._plugin_object(win2, "ndbreach") is runner
        assert "has been started" in win2.plugins_label.text()
    finally:
        for k, v in saved.items():
            setattr(qu, k, v)


# --------------------------------------------------------------------- export --
@test
def export_gap_line_no_longer_says_flowlines_only():
    from ND_Dam_Hazard_Helper import suiteexport as SE
    assert SE.NOT_IN_PACK[0] == "waterbodies (3DHP waterbodies: not in this export)", SE.NOT_IN_PACK
    assert "flowlines only" not in " ".join(SE.NOT_IN_PACK)
    assert any(layer == "waterbodies" and ds == "3DHP_waterbodies" for layer, _r, ds, _l in SE.DAM_EXTRAS)
