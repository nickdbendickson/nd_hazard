"""Hazard 2.34.5: a record's "inputs" handed over as the run read them - QGIS-free.

A record of either fork without "inputs_format" (ND Hazard before 2.34.5, DamSafety Hazard
before 2.18.62) lists every download in the order the downloads FINISHED; reproduce and the
ensemble put it as the run read it, from the whole record. Runs on the real PyQGIS when it
imports and on tests/fake_qgis.py otherwise (nothing here opens a layer):
  * datafolder.local_items: every recorded shape;
  * the marker: run_settings_dict writes it; old / new records of this fork AND of the other
    fork (plan_from_package, as_layers=False);
  * _traffic_as_run: all files present (in order: no note), NDDOT only, county only, the
    newest HPMS year gone (missing - no older year in its place; plan_from_package and
    ensemble.dam_settings);
  * old records' bridges (NBI before OSM) and lifelines (data folder, agency, OSM), and
    pipeline._used_inputs writing them in that order;
  * datafolder.open_config_all / check_local_data: ONE note for a one-file role that fails;
  * reviewpkg checklist item C13 names the package data not loaded.

    python3 -m ND_Dam_Hazard_Helper.tests.run_tests v2345_
"""
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

MODE = fake_qgis.install()
from ND_Dam_Hazard_Helper import datafolder as DF  # noqa: E402
from ND_Dam_Hazard_Helper import ensemble as E  # noqa: E402
from ND_Dam_Hazard_Helper import pipeline as PL  # noqa: E402
from ND_Dam_Hazard_Helper import reproduce as R  # noqa: E402
from ND_Dam_Hazard_Helper import reviewpkg as RV  # noqa: E402

# (plugin, a version before the marker, the version that writes it): this fork, the other one
MINE = ("ND Dam Hazard Classification Helper", "2.34.4", "2.34.5")
OTHER = ("DamSafety Hazard", "2.18.61", "2.18.62")

# ----------------------------------------------------------------- the fixture ---
TMP = tempfile.mkdtemp(prefix="repro_as_run_")
D = "20261020_abcdef12.geojson"                               # download._cache_path


def _f(name, make=True):
    p = os.path.join(TMP, "src", name)
    if make:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as fh:
            fh.write("{}")
    return p


T = {"nddot": _f("NDDOT_traffic_counts_" + D), "county": _f("ND_county_roads_" + D),
     "hpms23": _f("HPMS_ND_2023_" + D), "hpms24": _f("HPMS_ND_2024_" + D),
     "nbi": _f("NBI_bridges_" + D), "xosm": _f("OSM_crossings_" + D),
     "hosp": _f("lifeline_nd_hospitals_" + D), "nurse": _f("lifeline_nd_skilled_nursing_" + D),
     "osml": _f("OSM_lifelines_" + D), "mine": _f("County_hospitals.gpkg"),
     "roads": _f("ND_roads_NG911_" + D), "gone24": _f("gone/HPMS_ND_2024_" + D, make=False)}
B = {k: os.path.basename(v) for k, v in T.items()}
# a record without the marker: the downloads in the order they FINISHED, the data folder's
# lifeline after them (old pipeline._used_inputs)
OLD = {"aadt": [T["nddot"], T["county"], T["hpms23"], T["hpms24"]],
       "bridges": [T["xosm"], T["nbi"]],
       "lifelines": [T["osml"], [T["nurse"], "Nursing home"], [T["hosp"], "Hospital"],
                     [T["mine"], "Hospital"]],
       "roads": T["roads"]}
AS_RUN = {"aadt": [[T["hpms24"], None], [T["county"], None]],
          "bridges": [[T["nbi"], None], [T["xosm"], None]],
          "lifelines": [[T["mine"], "Hospital"], [T["hosp"], "Hospital"],
                        [T["nurse"], "Nursing home"], [T["osml"], None]],
          "roads": [[T["roads"], None]]}
# a record WITH the marker whose files came from a data folder (read as listed, all of them):
# put in 'run order' it would lose HPMS 2023 and swap the crossings / lifelines
FOLDER = {"aadt": [[T["hpms23"], None], [T["hpms24"], None], [T["county"], None]],
          "bridges": [[T["xosm"], None], [T["nbi"], None]],
          "lifelines": [[T["nurse"], "Nursing home"], [T["hosp"], "Hospital"]]}


def _record(inputs, plugin, version, marker):
    rec = {"format": R.RUN_SETTINGS_FORMAT, "format_version": R.FORMAT_VERSION,
           "plugin": plugin, "plugin_version": version, "dam_name": "Synthetic",
           "settings": {"osm_layer": None}, "scenarios": [], "layers": {}, "point_sources": [],
           "files": {}, "fingerprints": {}, "inputs": inputs}
    if marker:
        rec["inputs_format"] = R.INPUTS_FORMAT
    return rec


def _plan(rec, tag):
    pkg = os.path.join(TMP, "pkg_" + tag)
    os.makedirs(os.path.join(pkg, R.RECORD_DIR))
    with open(os.path.join(pkg, R.RECORD_DIR, R.RUN_SETTINGS_FILE), "w") as fh:
        json.dump(rec, fh)
    return R.plan_from_package(pkg, os.path.join(TMP, "work_" + tag), as_layers=False)


def _order_notes(notes):
    return [n for n in notes if "handed over" in n]


# ------------------------------------------------------------------- the tests ---
@test
def v2345_local_items_takes_every_recorded_shape():
    li = DF.local_items
    p, q = "/d/ND_roads_NG911_x.geojson", "/d/NARN_rail_lines_x.gpkg"
    assert li(p) == ([(p, None)], [])
    assert li([p]) == ([(p, None)], [])                         # the old one-item list
    assert li([[p]]) == ([(p, None)], [])                       # the old double wrap
    assert li([[p, None]]) == ([(p, None)], [])                 # [[path, category], ...]
    assert li([p, q]) == ([(p, None), (q, None)], [])           # two paths, not path + cat
    assert li([p, "Hospital"]) == ([(p, "Hospital")], [])       # one unwrapped pair
    assert li([[p, "Hospital"], q]) == ([(p, "Hospital"), (q, None)], [])
    assert li((p, q)) == ([(p, None), (q, None)], [])
    assert li([{"path": p, "category": "Hospital"}]) == ([(p, "Hospital")], [])
    assert li([[[p, None]], [p, None]]) == ([(p, None)], [])    # nested + duplicate
    assert li(p + "|layername=roads")[0] == [(p + "|layername=roads", None)]
    assert li(None) == ([], []) and li("") == ([], []) and li([]) == ([], [])
    items, probs = li([p, 123, ""])
    assert items == [(p, None)] and len(probs) == 2, (items, probs)


@test
def v2345_the_record_carries_the_marker_and_only_the_marker_counts():
    rec = R.run_settings_dict({}, {"aadt": [T["hpms24"], T["county"]]}, hash_files=False)
    assert rec["inputs_format"] == R.INPUTS_FORMAT == 2, rec
    assert R.inputs_as_run(rec) and R.inputs_as_run(json.loads(json.dumps(rec)))
    for plugin, version in (MINE[:2], (MINE[0], MINE[2]), OTHER[:2], (OTHER[0], OTHER[2]),
                            (MINE[0], "9.99.0")):
        assert not R.inputs_as_run(_record({}, plugin, version, False)), (plugin, version)
        assert R.inputs_as_run(_record({}, plugin, version, True)), (plugin, version)
    assert not R.inputs_as_run({"inputs_format": "x"}) and not R.inputs_as_run(None)


@test
def v2345_a_record_without_the_marker_is_put_as_the_run_read_it_either_fork():
    for tag, (plugin, version) in (("mine", MINE[:2]), ("other", OTHER[:2])):
        plan = _plan(_record(OLD, plugin, version, False), "old_" + tag)
        assert plan["cfg"]["local_data"] == AS_RUN, (tag, plan["cfg"]["local_data"])
        notes = _order_notes(plan["notes"])
        assert len(notes) == 3, (tag, notes)
        aadt = [n for n in notes if "'aadt'" in n][0]
        assert B["nddot"] in aadt and B["hpms23"] in aadt and B["hpms24"] not in aadt, aadt
        assert any("'bridges'" in n and "NBI" in n for n in notes), notes
        assert any("'lifelines'" in n and "data-folder" in n for n in notes), notes
        assert plan["missing"] == [], plan["missing"]


@test
def v2345_a_record_with_the_marker_is_handed_over_as_recorded_either_fork():
    for tag, (plugin, version) in (("mine", (MINE[0], MINE[2])), ("other", (OTHER[0],
                                                                          OTHER[2]))):
        plan = _plan(_record(FOLDER, plugin, version, True), "new_" + tag)
        assert plan["cfg"]["local_data"] == FOLDER, (tag, plan["cfg"]["local_data"])
        assert _order_notes(plan["notes"]) == [], plan["notes"]


@test
def v2345_traffic_as_run_cases():
    h24, cty, nd = T["hpms24"], T["county"], T["nddot"]
    n = []
    assert R._traffic_as_run([(h24, None), (cty, None)], n) == [[h24, None], [cty, None]]
    assert n == [], n                                           # already as read: no note
    assert R._traffic_as_run([(nd, None), (cty, None)], n) == [[nd, None], [cty, None]]
    assert n == [], n                                           # NDDOT only (no HPMS year)
    assert R._traffic_as_run([(cty, None)], n) == [(cty, None)] and n == []    # county only
    assert R._traffic_as_run([(cty, None), (nd, None)], n) == [[nd, None], [cty, None]]
    assert len(n) == 1 and "not read" not in n[0], n            # reordered, nothing dropped
    n = []
    got = R._traffic_as_run([(p, None) for p in OLD["aadt"]], n)
    assert got == [[h24, None], [cty, None]], got
    assert len(n) == 1 and B["nddot"] in n[0] and B["hpms23"] in n[0], n
    mine = "/d/my_aadt.gpkg"                                    # no download names: as it is
    assert R._traffic_as_run([(mine, None), (cty, None)], n) == [(mine, None), (cty, None)]


@test
def v2345_the_newest_hpms_year_gone_is_missing_not_replaced():
    old = dict(OLD, aadt=[T["nddot"], T["county"], T["hpms23"], T["gone24"]])
    plan = _plan(_record(old, OTHER[0], OTHER[1], False), "gone")
    assert plan["cfg"]["local_data"]["aadt"] == [[T["county"], None]], \
        plan["cfg"]["local_data"]["aadt"]                       # never HPMS 2023
    assert any(B["gone24"] in m for m in plan["missing"]), plan["missing"]
    aadt = [n for n in _order_notes(plan["notes"]) if "'aadt'" in n]
    assert len(aadt) == 1 and B["hpms23"] in aadt[0] and B["nddot"] in aadt[0], aadt
    # the ensemble reads the dam's QA/Run_Settings.json the same way
    dam = os.path.join(TMP, "dam_gone")
    os.makedirs(E._qa(dam))
    with open(os.path.join(E._qa(dam), R.RUN_SETTINGS_FILE), "w") as fh:
        json.dump(_record(old, MINE[0], MINE[1], False), fh)
    kw, info = E.dam_settings(dam)
    assert kw["local_data"]["aadt"] == [[T["county"], None]], kw["local_data"]["aadt"]
    assert kw["local_data"]["bridges"] == AS_RUN["bridges"], kw["local_data"]
    assert kw["local_data"]["lifelines"] == AS_RUN["lifelines"], kw["local_data"]
    txt = "\n".join(info["notes"])
    assert B["gone24"] in txt and "is gone" in txt, txt
    assert "recorded but not read by the run: " in txt and B["hpms23"] in txt, txt


@test
def v2345_old_bridges_and_lifelines_order():
    n = []
    assert R._old_inputs_as_run("bridges", [(T["nbi"], None), (T["xosm"], None)], n) == \
        [[T["nbi"], None], [T["xosm"], None]] and n == [], n
    assert R._old_inputs_as_run("bridges", [(T["xosm"], None)], n) == [(T["xosm"], None)]
    assert R._old_inputs_as_run("bridges", [(T["xosm"], None), (T["nbi"], None)], n) == \
        [[T["nbi"], None], [T["xosm"], None]] and len(n) == 1, n
    n = []
    life = DF.local_items(OLD["lifelines"])[0]
    assert R._old_inputs_as_run("lifelines", life, n) == AS_RUN["lifelines"] and len(n) == 1
    assert R._old_inputs_as_run("roads", [("/a.shp", None), ("/b.shp", None)], n) == \
        [("/a.shp", None), ("/b.shp", None)]                    # other roles as recorded


@test
def v2345_used_inputs_writes_the_files_as_the_run_reads_them():
    lyr = object()
    got = {}
    for key, p in (("xosm", T["xosm"]), ("life_osm", T["osml"]), ("hpms_2023", T["hpms23"]),
                   ("county", T["county"]), ("life_nd_skilled_nursing", T["nurse"]),
                   ("nbi", T["nbi"]), ("hpms_2024", T["hpms24"]), ("nddot", T["nddot"]),
                   ("life_nd_hospitals", T["hosp"]), ("road", T["roads"])):
        got[key] = (lyr, 1, p)                                  # in completion order
    cfg = {"local_data": {"lifelines": [[T["mine"], "Hospital"]]}}
    used = PL._used_inputs(cfg, got, {})
    assert used["aadt"] == [T["hpms24"], T["county"]], used
    assert used["bridges"] == [T["nbi"], T["xosm"]], used
    assert used["lifelines"] == [(T["mine"], "Hospital"), (T["hosp"], "Hospital"),
                                 (T["nurse"], "Nursing home"), T["osml"]], used
    assert used["roads"] == [T["roads"]], used
    got["hpms_2024"] = got["hpms_2023"] = (None, 0, None)       # no HPMS year answered
    assert PL._used_inputs({}, got, {})["aadt"] == [T["nddot"], T["county"]]
    # the record of these is handed over unchanged (the marker)
    rec = R.run_settings_dict({}, PL._used_inputs(cfg, dict(got, hpms_2024=(lyr, 1,
                                                                           T["hpms24"])), {}),
                              hash_files=False)
    plan = _plan(rec, "written")
    assert plan["cfg"]["local_data"] == AS_RUN, plan["cfg"]["local_data"]
    assert _order_notes(plan["notes"]) == [], plan["notes"]


@test
def v2345_a_one_file_role_that_fails_is_one_note():
    bad, bad2 = _f("not_a_layer.geojson"), _f("not_a_layer_2.geojson")
    keep = DF.open_uri
    DF.open_uri = lambda *a, **k: None                          # nothing opens
    try:
        for value, why in (([bad], "could not be opened"),
                           ([os.path.join(TMP, "gone.geojson")], "no longer exists"),
                           ([123], "not a file")):
            _l, notes = DF.open_config_all({"rail": value})
            assert len(notes) == 1 and why in notes[0], notes
        _l, notes = DF.open_config_all({"bridges": [bad, bad2]})
        assert len(notes) == 3 and "none of its 2 entries" in notes[2], notes
        loaded, not_loaded = R.check_local_data({"rail": [[bad, None]], "roads": [[bad2,
                                                                                    None]]})
        assert loaded == {} and len(not_loaded) == 2, not_loaded
    finally:
        DF.open_uri = keep


@test
def v2345_c13_names_the_package_data_not_loaded():
    repro = {"status": "IDENTICAL", "lines": ["IDENTICAL - same result"], "missing": [],
             "not_loaded": []}
    row = RV._c_repro(repro, True)
    assert row["Flag"] == "OK" and "NOT loaded" not in row["Automatic_Finding"], row
    repro["not_loaded"] = ["rail (Rail lines): x.geojson could not be opened - NOT used"]
    row = RV._c_repro(repro, True)
    assert "1 package data file(s) / role(s) NOT loaded" in row["Automatic_Finding"], row
    repro["missing"] = ["input 'usa' (gone.geojson)"]
    row = RV._c_repro(repro, True)
    assert "1 input(s) missing" in row["Automatic_Finding"] and \
        "NOT loaded" in row["Automatic_Finding"], row
