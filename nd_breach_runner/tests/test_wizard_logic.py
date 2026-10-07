"""Tests of wizard_logic.py (pure logic, with fakes) and a stub-import smoke test of wizard.py.

Qt cannot be run where the plugin is developed, so wizard.py is only compiled, checked for
Python 3.9 syntax and built against stand-in Qt classes that accept any call; what the
wizard decides lives in wizard_logic.py and is tested here."""
import ast
import importlib
import os
import py_compile
import shutil
import sys
import tempfile
import types

from nd_breach_runner import wizard_logic as wl
from nd_breach_runner.core import estimate as estmod
from nd_breach_runner.core import workflow
from nd_breach_runner.tests.registry import test

PLUGIN = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Tmp(object):
    def __enter__(self):
        self.d = tempfile.mkdtemp()
        return self.d

    def __exit__(self, *a):
        shutil.rmtree(self.d, ignore_errors=True)


class Null(object):
    """A QVariant NULL."""

    def isNull(self):
        return True


def valid_answers(**over):
    a = wl.default_answers("/tmp/dams")
    a.update(dam={"DAM_ID": "ND00123", "DAM_NAME": "Test Dam", "HEIGHT_FT": 25.0,
                  "STORAGE_ACFT": 500.0, "TOP_FT": 1850.0, "AREA_AC": 60.0},
             point=(350000.0, 5230000.0, "EPSG:26914"), dem_id="dem_1", dem_name="DEM",
             z_units="meters")
    a.update(over)
    return a


# ------------------------------------------------------------------------------- the dam
@test
def wizard_maps_fields_by_name():
    names = ["OBJECTID", "NIDID", "DAM_NAME", "DAM_HEIGHT", "MAX_STORAGE", "SURFACE_AREA",
             "TOP_OF_DAM", "PMF_ELEV", "CREST_WIDTH", "POOL"]
    r = wl.field_mapping(names)
    m = r["mapping"]
    assert m["F_ID"] == "NIDID" and m["F_NAME"] == "DAM_NAME" and m["F_HEIGHT"] == "DAM_HEIGHT"
    assert m["F_STORAGE"] == "MAX_STORAGE" and m["F_AREA"] == "SURFACE_AREA"
    assert m["F_TOP"] == "TOP_OF_DAM" and m["F_PMF"] == "PMF_ELEV" and m["F_CREST"] == "CREST_WIDTH"
    assert "F_POOL" not in m and r["missing"] == [] and len(r["how"]) >= 8
    assert set(m) <= set(wl.FIELD_KEYS)
    r = wl.field_mapping(["FID", "SOMETHING"])
    assert r["mapping"] == {} and r["missing"] == [
        "Dam ID (NID ID)", "Dam height (ft)", "Storage at the top of dam (ac-ft)"]
    assert wl.field_mapping([])["missing"]


@test
def wizard_reads_a_features_attributes():
    mapping = {"F_ID": "ID", "F_NAME": "NAME", "F_HEIGHT": "H", "F_STORAGE": "S", "F_TOP": "T",
               "F_AREA": "A", "F_CREST": "C"}
    attrs = {"ID": " ND1 ", "NAME": Null(), "H": "1,250.5", "S": 300, "T": "n/a", "A": float("nan"),
             "C": None}
    v = wl.dam_values_from_attrs(attrs, mapping)
    assert v == {"DAM_ID": "ND1", "HEIGHT_FT": 1250.5, "STORAGE_ACFT": 300.0}, v
    # an attribute object with .get() or a missing field is fine too
    class Row(object):
        def get(self, k):
            return {"ID": 7, "H": "12"}.get(k)
    v = wl.dam_values_from_attrs(Row(), {"F_ID": "ID", "F_HEIGHT": "H", "F_STORAGE": "gone"})
    assert v == {"DAM_ID": "7", "HEIGHT_FT": 12.0}
    assert wl.dam_values_from_attrs({"ID": "null"}, {"F_ID": "ID"}) == {}
    assert wl.clean_number("x", 5) == 5 and wl.clean_number(" 3.5 ") == 3.5
    assert wl.clean_number(float("inf")) is None and wl.clean_number(Null(), 1) == 1


@test
def wizard_points_round_trip():
    assert wl.point_text(350000.0, 5230000.0, "EPSG:26914") == "350000.000,5230000.000 [EPSG:26914]"
    assert wl.point_text(-100.7, 47.2) == "-100.70000000,47.20000000"
    assert wl.parse_point("350000.000, 5230000.000 [EPSG:26914]") == (350000.0, 5230000.0)
    assert wl.parse_point("1;2") == (1.0, 2.0) and wl.parse_point("1 2") == (1.0, 2.0)
    for bad in ("", "1", "a, b", "1,2,3", None):
        assert wl.parse_point(bad) is None, bad


# ------------------------------------------------------------------------------- the parameters
@test
def wizard_builds_the_tool_parameters():
    p = wl.build_rundam_params(valid_answers())
    assert p["DAM_ID"] == "ND00123" and p["HEIGHT_FT"] == 25.0 and p["STORAGE_ACFT"] == 500.0
    assert p["DAM_POINT"] == "350000.000,5230000.000 [EPSG:26914]"
    assert p["DEM"] == "dem_1" and p["DEM_Z"] == wl.Z_IDS.index("meters")
    assert p["METHOD"] == 0 and p["SCENARIO"] == 0 and p["TIER"] == 0 and p["CELL"] == 10.0
    assert p["MANNING"] == 0.06 and p["DAMS_FOLDER"] == "/tmp/dams"
    for k in ("INFLOW_CSV", "SPILLWAY_CSV", "TRIGGER", "MANNING_RASTER", "NLCD_RASTER", "PMF_FT"):
        assert k not in p, k                                  # not asked: the tool's default
    f = wl.build_rundam_params(valid_answers(
        scenario="flood", inflow_csv="/x/in.csv", spillway_csv="/x/sp.csv", normal_pool_ft=1845,
        trigger="wse", trigger_value=1851.5, method="tr60", tier="lisflood+anuga", cell_m=20,
        nlcd_id="nlcd", manning_id="", z_units="us_feet"))
    assert f["SCENARIO"] == 1 and f["INFLOW_CSV"] == "/x/in.csv" and f["SPILLWAY_CSV"] == "/x/sp.csv"
    assert f["NORMAL_POOL_FT"] == 1845.0 and f["TRIGGER"] == wl.TRIGGER_IDS.index("wse")
    assert f["TRIGGER_VALUE"] == 1851.5 and f["METHOD"] == wl.METHOD_IDS.index("tr60")
    assert f["TIER"] == 1 and f["CELL"] == 20.0 and f["NLCD_RASTER"] == "nlcd"
    assert f["DEM_Z"] == wl.Z_IDS.index("us_feet")
    # flood-only answers are dropped for a sunny-day run
    s = wl.build_rundam_params(valid_answers(scenario="sunny", inflow_csv="/x/in.csv",
                                             normal_pool_ft=1.0))
    assert "INFLOW_CSV" not in s and "NORMAL_POOL_FT" not in s and "TRIGGER" not in s


def _algorithms():
    from nd_breach_runner.tests import fake_qgis
    fake_qgis.install()
    import nd_breach_runner.algorithms as algs
    importlib.reload(algs)
    return algs


@test
def wizard_parameters_match_the_algorithm_definition():
    algs = _algorithms()
    a = algs.RunDamAlgorithm()
    a.initAlgorithm()
    defined = set(p.name() for p in a.parameterDefinitions())
    # every key the wizard can produce is a parameter of the tool
    p = wl.build_rundam_params(valid_answers(
        scenario="flood", inflow_csv="a", spillway_csv="b", normal_pool_ft=1, trigger="time",
        trigger_value=2, nlcd_id="n", manning_id="m", tier="lisflood+anuga"))
    dam = dict((f[1], 1.0) for f in wl.DAM_FIELDS)
    dam["DAM_ID"] = dam["DAM_NAME"] = "x"
    p.update(wl.build_rundam_params(valid_answers(dam=dam)))
    missing = sorted(k for k in p if k not in defined)
    assert not missing, "the wizard sets parameters the tool does not have: {}".format(missing)
    for _key, param, _l, _k, _r in wl.DAM_FIELDS:
        assert param in defined, param
    # the enum indices mean the same thing in the tool's option lists
    def options(name):
        return a.parameterDefinition(name).args[0]
    assert wl.METHOD_IDS == tuple(m[0] for m in algs.METHODS)
    assert wl.TIER_IDS == tuple(t[0] for t in algs.TIERS)
    assert wl.SCENARIO_IDS == tuple(s[0] for s in algs.SCENARIOS)
    assert wl.Z_IDS == tuple(z[0] for z in algs.ZUNITS)
    assert wl.TRIGGER_IDS == tuple(t[0] for t in algs.TRIGGERS)
    for name, ids in (("METHOD", wl.METHOD_IDS), ("TIER", wl.TIER_IDS),
                      ("SCENARIO", wl.SCENARIO_IDS), ("DEM_Z", wl.Z_IDS),
                      ("TRIGGER", wl.TRIGGER_IDS)):
        assert len(options(name)) == len(ids), name
    # the tool's own labels are the ones the wizard shows
    assert list(options("SCENARIO")) == [algs.SCENARIOS[i][1] for i in range(2)]
    assert set(wl.TIER_LABELS) == set(wl.TIER_IDS) and set(wl.SCENARIO_LABELS) == set(wl.SCENARIO_IDS)
    assert set(wl.Z_LABELS) == set(wl.Z_IDS) and set(wl.TRIGGER_LABELS) == set(wl.TRIGGER_IDS)
    assert wl.ALGORITHM_ID == algs.PROVIDER_ID + ":" + algs.RunDamAlgorithm().name()
    # the defaults the wizard starts from are the tool's defaults
    d = wl.default_answers()
    assert d["cell_m"] == a.parameterDefinition("CELL").kwargs["defaultValue"]
    assert d["manning_n"] == a.parameterDefinition("MANNING").kwargs["defaultValue"]
    assert wl.METHOD_IDS[a.parameterDefinition("METHOD").kwargs["defaultValue"]] == d["method"]
    assert wl.SCENARIO_IDS[a.parameterDefinition("SCENARIO").kwargs["defaultValue"]] == d["scenario"]
    assert wl.TIER_IDS[a.parameterDefinition("TIER").kwargs["defaultValue"]] == d["tier"]


# ------------------------------------------------------------------------------- checking pages
def _msgs(page, a, **kw):
    e, w = wl.validate_page(page, a, **kw)
    return "\n".join(e), "\n".join(w)


@test
def wizard_validates_the_dam_page():
    e, w = _msgs("dam", valid_answers())
    assert e == "" and "surface area" not in w
    e, w = _msgs("dam", valid_answers(dam={"HEIGHT_FT": 25.0, "STORAGE_ACFT": 5.0}, point=None))
    assert "dam ID" in e and "location" in e and "top of dam elevation" in w and "surface area" in w
    e, _w = _msgs("dam", valid_answers(dam={"DAM_ID": "x", "HEIGHT_FT": 0, "STORAGE_ACFT": -1}))
    assert "Dam height (ft) must be greater than 0" in e and "Storage at the top of dam (ac-ft) must" in e
    e, _w = _msgs("dam", valid_answers(dam={"DAM_ID": "x", "HEIGHT_FT": 5.0}))
    assert "Give the value for: Storage" in e
    e, _w = _msgs("dam", valid_answers(dam={"DAM_ID": "x", "HEIGHT_FT": 5.0, "STORAGE_ACFT": 1.0,
                                             "TOP_FT": 100.0, "BOTTOM_FT": 100.0, "CREST_WIDTH": -3}))
    assert "below the top of dam" in e and "Crest width (ft) cannot be negative" in e


@test
def wizard_validates_terrain_and_scenario_pages():
    with Tmp() as d:
        e, w = _msgs("terrain", valid_answers(dams_folder=d))
        assert e == "" and "Elevation units" not in w
        e, w = _msgs("terrain", valid_answers(dem_id="", dams_folder="", manning_n=2.0,
                                              z_units="auto", nlcd_id="a", manning_id="b"))
        assert "Choose the DEM" in e and "between 0.005 and 1" in e
        assert "n raster is used and the NLCD raster ignored" in w and "Elevation units" in w
        # 3.9.0: the Dams folder is asked (and checked) on the dam page
        assert "Choose the Dams folder" not in e
        assert "Choose the Dams folder" in _msgs("dam", valid_answers(dams_folder=""))[0]
        f = os.path.join(d, "afile")
        open(f, "w").write("x")
        e, _w = _msgs("dam", valid_answers(dams_folder=f))
        assert "is a file, not a folder" in e
        assert _msgs("dam", valid_answers(dams_folder=os.path.join(d, "new")))[0] == ""
        assert _msgs("terrain", valid_answers(dams_folder=f))[0] == ""
        # scenario
        inflow = os.path.join(d, "in.csv")
        open(inflow, "w").write("0,0\n")
        assert _msgs("scenario", valid_answers())[0] == ""                    # sunny needs nothing
        e, w = _msgs("scenario", valid_answers(scenario="flood"))
        assert "PMF inflow hydrograph" in e and "normal pool elevation or a spillway" in e
        e, w = _msgs("scenario", valid_answers(scenario="flood", inflow_csv=inflow,
                                               normal_pool_ft=1845.0))
        assert e == "" and "No spillway rating" in w and "No crest length" in w
        e, _w = _msgs("scenario", valid_answers(scenario="flood", inflow_csv=inflow + "x",
                                                spillway_csv=inflow + "y", trigger="time"))
        assert "inflow CSV does not exist" in e and "spillway CSV does not exist" in e
        assert "failure time" in e
        e, _w = _msgs("scenario", valid_answers(scenario="flood", inflow_csv=inflow,
                                                spillway_csv=inflow, trigger="wse",
                                                trigger_value=1850.0))
        assert e == ""
        # the file checks are injectable
        e, _w = _msgs("scenario", valid_answers(scenario="flood", inflow_csv="nowhere.csv",
                                                normal_pool_ft=1.0), exists=lambda p: True)
        assert e == ""


@test
def wizard_validates_the_model_page():
    assert _msgs("model", valid_answers())[0] == ""
    e, w = _msgs("model", valid_answers(cell_m=0.5, tier="lisflood+anuga"))
    assert "at least 1 m" in e and "ANUGA adds" in w
    e, w = _msgs("model", valid_answers(cell_m=3.0))
    assert e == "" and "under 5 m" in w
    e, _w = _msgs("model", valid_answers(tier="lisflood+anuga"),
                  models={"lisflood": False, "anuga": False})
    assert "LISFLOOD-FP was not found" in e and "ANUGA Python environment" in e
    e, _w = _msgs("model", valid_answers(), models={"lisflood": True, "anuga": False})
    assert e == ""
    try:
        wl.validate_page("nope", valid_answers())
        raise AssertionError("expected ValueError")
    except ValueError:
        pass
    errs, warns = wl.validate_all(valid_answers(dem_id="", scenario="flood"))
    assert any(m.startswith("terrain: Choose the DEM") for m in errs)
    assert any(m.startswith("scenario: The flood scenario needs") for m in errs)
    assert warns and all(":" in m for m in warns)


# ------------------------------------------------------------------------------- the estimate
PLAN = {"breach_occurs": True, "cells": 400000, "size_mi": (7.0, 2.0), "cell_m": 10.0,
        "sim_time_s": 6.7 * 3600, "peak_cfs": 4000.0, "inflow_xy": (1.0, 2.0), "baseline": False,
        "trace_mi": 5.0, "warnings": []}


@test
def wizard_rescales_the_plan_like_the_estimator():
    p2 = wl.rescale_plan(PLAN, 20.0)
    assert p2["cells"] == 100000 and p2["cell_m"] == 20.0 and PLAN["cells"] == 400000
    p5 = wl.rescale_plan(PLAN, 5.0)
    assert p5["cells"] == 1600000
    rate = 5.0e7
    real = estmod.estimate(p2, rate, False)
    lines = wl.estimate_lines(p2, rate, "lisflood")
    assert lines == estmod.describe(p2, real)
    assert any("100,000 cells at 20 m" in ln for ln in lines), lines
    # coarser is faster, finer is (much) slower
    t = lambda p: estmod.estimate(p, rate)["one_run_min"]
    assert t(p2) < t(PLAN) < t(p5)
    both = wl.estimate_lines(PLAN, rate, "lisflood+anuga")
    assert any("ANUGA adds" in ln for ln in both) and len(both) == len(
        wl.estimate_lines(PLAN, rate, "lisflood")) + 1
    assert wl.estimate_lines({"breach_occurs": False}, rate, "lisflood") == [
        "The dam does not fail in this flood: there is nothing to model."]


@test
def wizard_estimate_cache_reuses_plan_and_rate():
    calls = {"plan": 0, "rate": 0}

    def make_plan(params):
        calls["plan"] += 1
        assert "CELL" in params and params["DAM_ID"] == "ND00123"
        plan = dict(PLAN)
        plan["cell_m"] = params["CELL"]
        return plan, {"exe": "/x/lisflood"}

    def rate(ctx):
        calls["rate"] += 1
        assert ctx == {"exe": "/x/lisflood"}
        return 5.0e7
    c = wl.EstimateCache(make_plan, rate)
    a = valid_answers()
    assert c.lines(a) is None and not c.has_plan(a)
    first = c.compute(a)
    assert calls == {"plan": 1, "rate": 1} and any("400,000 cells at 10 m" in ln for ln in first)
    assert c.lines(a) == first
    # only the cell size or the tier changed: rescaled, nothing recomputed
    a2 = valid_answers(cell_m=20.0)
    assert c.has_plan(a2)
    l2 = c.lines(a2)
    assert l2 is not None and any("100,000 cells at 20 m" in ln for ln in l2)
    assert c.compute(a2) == l2 and calls == {"plan": 1, "rate": 1}
    a3 = valid_answers(tier="lisflood+anuga", manning_n=0.08, dams_folder="/elsewhere")
    assert c.has_plan(a3) and any("ANUGA adds" in ln for ln in c.lines(a3))
    # a different dam or scenario needs a new plan, but the measured rate is kept
    a4 = valid_answers(scenario="flood", inflow_csv="/x.csv", normal_pool_ft=1.0)
    assert not c.has_plan(a4) and c.lines(a4) is None
    c.compute(a4)
    assert calls == {"plan": 2, "rate": 1}
    c.compute(a4, force=True)
    assert calls["plan"] == 3
    c.reset()
    assert c.lines(a) is None


@test
def wizard_estimate_cache_reports_failures_and_no_breach():
    def boom(params):
        raise RuntimeError("Set the LISFLOOD-FP executable in Processing > Options")
    c = wl.EstimateCache(boom, lambda ctx: 1.0)
    try:
        c.compute(valid_answers())
        raise AssertionError("expected EstimateError")
    except wl.EstimateError as e:
        assert "Set the LISFLOOD-FP executable" in str(e)
    # an EstimateError raised by the caller's own code is passed on unchanged
    def own(params):
        raise wl.EstimateError("no DEM data at the dam point")
    try:
        wl.EstimateCache(own, lambda ctx: 1.0).compute(valid_answers())
        raise AssertionError("expected EstimateError")
    except wl.EstimateError as e:
        assert str(e) == "no DEM data at the dam point"
    for bad_rate, word in ((None, "test flood did not run"), (0, "test flood did not run")):
        c = wl.EstimateCache(lambda p: (dict(PLAN), {}), lambda ctx, r=bad_rate: r)
        try:
            c.compute(valid_answers())
            raise AssertionError("expected EstimateError")
        except wl.EstimateError as e:
            assert word in str(e)
    def failing_rate(ctx):
        raise OSError("lisflood: permission denied")
    try:
        wl.EstimateCache(lambda p: (dict(PLAN), {}), failing_rate).compute(valid_answers())
        raise AssertionError("expected EstimateError")
    except wl.EstimateError as e:
        assert "permission denied" in str(e)
    # a dam that does not fail needs no timing at all
    timed = []
    c = wl.EstimateCache(lambda p: ({"breach_occurs": False}, {}), lambda ctx: timed.append(1) or 1.0)
    assert c.compute(valid_answers()) == ["The dam does not fail in this flood: there is nothing "
                                          "to model."] and timed == []


# ------------------------------------------------------------------------------- the review
@test
def wizard_review_text_lists_what_will_be_filled_in():
    a = valid_answers(scenario="flood", inflow_csv="/x/in.csv", normal_pool_ft=1845.0,
                      trigger="wse", trigger_value=1851.5, tier="lisflood+anuga", cell_m=15.0,
                      nlcd_id="n1", nlcd_name="NLCD 2021", z_units="us_feet")
    t = wl.review_text(a, ["First domain 7.0 x 2.0 mi"], ["scenario: No spillway rating"])
    for want in ("ND00123", "Test Dam", "25", "500", "350000.000,5230000.000 [EPSG:26914]",
                 "DEM: DEM", "US survey feet", "NLCD land cover: NLCD 2021", "/x/in.csv",
                 "Normal pool: 1845 ft", "when the pool reaches an elevation", "(1851.5)",
                 "LISFLOOD-FP for the extent, then ANUGA", "Cell size: 15 m",
                 "First domain 7.0 x 2.0 mi", "Check before running", "No spillway rating",
                 "Open the tool"):
        assert want in t, want
    sunny = wl.review_text(valid_answers())
    assert "Inflow" not in sunny and "Run time" not in sunny and "Check before" not in sunny


# ------------------------------------------------------------------------------- wizard.py
@test
def wizard_module_compiles_and_is_python39():
    for name in ("wizard.py", "wizard_logic.py"):
        path = os.path.join(PLUGIN, name)
        py_compile.compile(path, cfile=os.path.join(tempfile.gettempdir(), name + "c"),
                           doraise=True)
        src = open(path, encoding="utf-8").read()
        ast.parse(src, feature_version=(3, 9))
        tree = ast.parse(src)
        for node in ast.walk(tree):
            assert not isinstance(node, getattr(ast, "Match", ())), "match statement in " + name
    # nothing imports Qt or QGIS at module level, so the plugin can import them anywhere
    for name in ("wizard.py", "wizard_logic.py"):
        tree = ast.parse(open(os.path.join(PLUGIN, name), encoding="utf-8").read())
        for node in tree.body:
            mods = []
            if isinstance(node, ast.Import):
                mods = [n.name for n in node.names]
            elif isinstance(node, ast.ImportFrom):
                mods = [node.module or ""]
            assert not any(m.startswith(("qgis", "PyQt", "processing")) for m in mods), (name, mods)


class _StubBase(object):
    """Stands in for any Qt class: constructible, subclassable, every attribute callable."""

    def __init__(self, *a, **k):
        pass

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return _StubBase()

    def __call__(self, *a, **k):
        return _StubBase()


class _StubModule(types.ModuleType):
    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        cls = type(name, (_StubBase,), {})
        setattr(self, name, cls)
        return cls


class stub_qt(object):
    """qgis.PyQt (and nothing else of QGIS) replaced by stubs, wizard freshly imported."""
    NAMES = ("qgis", "qgis.PyQt", "qgis.PyQt.QtWidgets", "qgis.PyQt.QtCore", "qgis.PyQt.QtGui",
             "qgis.core", "processing", "nd_breach_runner.wizard")

    def __enter__(self):
        self.saved = dict((n, sys.modules.get(n)) for n in self.NAMES)
        for n in self.NAMES:
            sys.modules.pop(n, None)
        qgis = types.ModuleType("qgis")
        pyqt = types.ModuleType("qgis.PyQt")
        for sub in ("QtWidgets", "QtCore", "QtGui"):
            m = _StubModule("qgis.PyQt." + sub)
            sys.modules["qgis.PyQt." + sub] = m
            setattr(pyqt, sub, m)
        qgis.PyQt = pyqt
        sys.modules["qgis"], sys.modules["qgis.PyQt"] = qgis, pyqt
        self.mod = importlib.import_module("nd_breach_runner.wizard")
        return self.mod

    def __exit__(self, *a):
        for n in self.NAMES:
            sys.modules.pop(n, None)
            if self.saved[n] is not None:
                sys.modules[n] = self.saved[n]
        import nd_breach_runner
        if hasattr(nd_breach_runner, "wizard"):
            del nd_breach_runner.wizard


@test
def wizard_builds_against_stub_qt():
    with stub_qt() as mod:
        cls = mod._classes()
        assert set(cls) == {"Wizard", "DamPage", "TerrainPage", "ScenarioPage", "ModelPage",
                            "ReviewPage"}
        assert mod._classes() is cls                                  # built once
        w = cls["Wizard"](None)                                       # constructs every page
        assert w.answers == wl.default_answers()
        assert isinstance(w.session, mod.EstimateSession)
        assert w.session.cache.lines(w.answers) is None
        # every page has the hooks QWizard calls
        for name in ("DamPage", "TerrainPage", "ScenarioPage", "ModelPage", "ReviewPage"):
            page = cls[name]
            assert callable(page.initializePage) and callable(page.validatePage)


@test
def wizard_errors_become_messages_not_exceptions():
    with stub_qt() as mod:
        seen = []
        mod._message = lambda kind, text, detail="", parent=None: seen.append((kind, text, detail))

        class Thing(object):
            @mod._safe(default="fallback", what="a test")
            def boom(self):
                raise KeyError("missing thing")

            @mod._safe(default=False, what="another test")
            def fine(self):
                return True
        assert Thing().boom() == "fallback" and Thing().fine() is True
        kind, text, detail = seen[0]
        assert kind == "critical" and "Something went wrong in a test" in text
        assert "missing thing" in text and "KeyError" in detail
        # the message box itself failing (no Qt) falls back to stderr instead of raising
        import io
        import contextlib
        err = io.StringIO()
        importlib.reload(mod)                                          # the real _message again
        keep = sys.modules["qgis.PyQt.QtWidgets"]
        sys.modules["qgis.PyQt.QtWidgets"] = None                      # importing Qt now fails
        try:
            with contextlib.redirect_stderr(err):
                mod._message("warning", "hello", "detail")
        finally:
            sys.modules["qgis.PyQt.QtWidgets"] = keep
        assert "hello" in err.getvalue()


@test
def wizard_opens_the_tool_with_the_answers():
    with stub_qt() as mod:
        calls, seen = [], []
        proc = types.ModuleType("processing")
        proc.execAlgorithmDialog = lambda alg, params: calls.append((alg, params)) or {}
        sys.modules["processing"] = proc
        mod._message = lambda kind, text, detail="", parent=None: seen.append((kind, text))
        answers = valid_answers(scenario="flood", inflow_csv="/x/in.csv", normal_pool_ft=1845.0,
                                cell_m=20.0)

        class FakeWizard(object):
            result = 1

            def __init__(self, parent=None):
                self.answers = answers
                self.parent = parent

            def exec_(self):
                return FakeWizard.result

        class Iface(object):
            def mainWindow(self):
                return "main window"
        mod._classes()["Wizard"] = FakeWizard
        params = mod.run_wizard(Iface())
        assert params == wl.build_rundam_params(answers)
        assert calls == [("ndbreach:rundam", params)], calls
        assert params["SCENARIO"] == 1 and params["CELL"] == 20.0
        # cancelled: nothing opens
        FakeWizard.result = 0
        del calls[:]
        assert mod.run_wizard(None) is None and calls == []
        # the dialog cannot open: the user gets the values in the message
        FakeWizard.result = 1
        proc.execAlgorithmDialog = lambda alg, params: (_ for _ in ()).throw(RuntimeError("no gui"))
        params = mod.run_wizard(None)
        assert params is not None
        kind, text = seen[-1]
        assert kind == "critical" and "no gui" in text and "DAM_ID = ND00123" in text
        # the wizard cannot even be built
        mod._classes()["Wizard"] = lambda parent=None: (_ for _ in ()).throw(ValueError("no Qt"))
        assert mod.run_wizard(None) is None and "no Qt" in seen[-1][1]


@test
def wizard_estimate_session_uses_the_tools_own_setup():
    """EstimateSession._plan builds the tool's parameters and asks workflow.preview; checked
    with the fake QGIS classes and a synthetic terrain (no Qt, no GDAL)."""
    from nd_breach_runner.tests import fake_qgis
    from nd_breach_runner.tests.synthetic import SyntheticTerrain, npy_writer
    fake_qgis.install()
    fake_qgis._Param.defaultValue = lambda self: self.kwargs.get("defaultValue")
    core = sys.modules["qgis.core"]
    core.QgsProject = type("QgsProject", (), {"instance": staticmethod(lambda: object())})
    core.QgsProcessingContext.setProject = lambda self, p: None
    import nd_breach_runner.algorithms as algs
    importlib.reload(algs)
    import nd_breach_runner.wizard as mod
    importlib.reload(mod)

    def gis(self, parameters, context, crs, pt):
        return (SyntheticTerrain(), npy_writer, fake_qgis.QgsCoordinateTransform(),
                fake_qgis.QgsCoordinateReferenceSystem("EPSG:26914"))
    keep_gis = algs._Base.gis
    algs._Base.gis = gis
    exe = os.path.join(tempfile.gettempdir(), "fake_lisflood_for_wizard_test")
    open(exe, "w").write("x")
    try:
        class Raster(object):
            def crs(self):
                return fake_qgis.QgsCoordinateReferenceSystem("EPSG:26914")
        params = wl.build_rundam_params(valid_answers(
            dam={"DAM_ID": "W1", "HEIGHT_FT": 25.0, "STORAGE_ACFT": 20.0, "TOP_FT": 1850.0,
                 "AREA_AC": 4.0}, cell_m=20.0))
        params["DEM"] = Raster()
        params["DAM_POINT"] = fake_qgis.QgsPointXY(0, 0)
        params["LISFLOOD_EXE"] = exe
        params["LENGTH_MI"], params["HALF_WIDTH_MI"] = 1.0, 0.3
        plan, ctx = mod.EstimateSession._plan(params)
        assert plan["breach_occurs"] and plan["cell_m"] == 20.0 and plan["cells"] > 1000
        assert plan["size_mi"][0] < 2.0 and ctx["exe"] == exe
        # and through the cache: the rate comes from lisflood.calibrate (patched here)
        keep_rate = mod.EstimateSession._rate
        mod.EstimateSession._rate = staticmethod(lambda c: 5.0e7)
        try:
            s = mod.EstimateSession()
            s.cache.make_plan = lambda p: mod.EstimateSession._plan(dict(p, DEM=Raster(),
                                                                        DAM_POINT=fake_qgis.QgsPointXY(0, 0),
                                                                        LISFLOOD_EXE=exe,
                                                                        LENGTH_MI=1.0, HALF_WIDTH_MI=0.3))
            lines = s.cache.compute(valid_answers(
                dam={"DAM_ID": "W1", "HEIGHT_FT": 25.0, "STORAGE_ACFT": 20.0, "TOP_FT": 1850.0,
                     "AREA_AC": 4.0}, cell_m=20.0))
            assert any("at 20 m" in ln for ln in lines), lines
            assert any("One LISFLOOD-FP run" in ln for ln in lines)
        finally:
            mod.EstimateSession._rate = keep_rate
    finally:
        algs._Base.gis = keep_gis
        os.remove(exe)
        sys.modules.pop("nd_breach_runner.wizard", None)
