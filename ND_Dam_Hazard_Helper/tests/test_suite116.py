"""damsuite 1.16.0: the shared modules behind the suite's front door - suitecheck (is the suite
ready to run), suitedata.adopt_folder (one folder chosen once), priority (computed class against
the class on record), packdiff (what the pack refresh changed and whose results it makes stale),
cascade (does the flood reach another dam) and loss (population at risk).

These files are byte-identical in all four plugins, so this is the one place they are tested.
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
from ND_Dam_Hazard_Helper.damsuite import cascade as CA  # noqa: E402
from ND_Dam_Hazard_Helper.damsuite import loss as LO  # noqa: E402
from ND_Dam_Hazard_Helper.damsuite import packdiff as PD  # noqa: E402
from ND_Dam_Hazard_Helper.damsuite import priority as PR  # noqa: E402
from ND_Dam_Hazard_Helper.damsuite import suitecheck as SC  # noqa: E402
from ND_Dam_Hazard_Helper.damsuite import suitedata as SD  # noqa: E402
from ND_Dam_Hazard_Helper.damsuite import versions as VS  # noqa: E402

RELEASE = {"ndhazard": "2.34.5", "ndbreach": "3.26.9", "ndscreen": "0.32.4",
           "ndupdater": "1.7.0"}


def _row(did, cls="", existing="", **kw):
    r = {"dam_id": did, "folder": os.path.join("/x", did), "name": kw.pop("name", ""),
         "class": cls, "existing_class": existing, "screened": "", "modelled": "",
         "classified": "", "reviewed": "", "next": "none", "next_text": "", "plugin": "",
         "disputed": 0, "problem": "", "has_rasters": True, "has_manifest": True,
         "has_record": True}
    r.update(kw)
    return r


class _Isolated(object):
    """The suite settings read a real QgsSettings when one exists, which would carry another
    QGIS session's folders into the test. Inside this block they come from a temporary file."""

    def __enter__(self):
        self.home = tempfile.mkdtemp()
        self.folder = tempfile.mkdtemp()
        self._env = {k: os.environ.pop(k, None) for k in
                     ("NDSUITE_DATA", "NDSUITE_PACK", "NDSUITE_SOURCE", "NDSUITE_DAMS")}
        self._config, SD.CONFIG = SD.CONFIG, os.path.join(self.home, ".ndsuite", "config.json")
        self._qs, SD._qsettings = SD._qsettings, lambda: None
        SD._PACK_MANIFESTS.clear()
        return self

    def __exit__(self, *a):
        SD.CONFIG = self._config
        SD._qsettings = self._qs
        SD._PACK_MANIFESTS.clear()
        for k, v in self._env.items():
            if v is not None:
                os.environ[k] = v
        return False


# ------------------------------------------------------------------ versions --
@test
def suite116_release_names_all_four_plugins_and_the_updater_is_no_handoff_partner():
    assert VS.RELEASE == RELEASE, VS.RELEASE
    assert "ndupdater" not in VS.PLUGINS      # nothing requires a version of the updater
    assert VS.ALL["ndupdater"] == "nd_data_updater"
    assert VS.LABELS["ndupdater"] == "ND Data Updater"
    # the three hand-off partners still check against each other as before
    assert VS.check("ndbreach", versions={"ndhazard": "2.1.0"})[0][0] == "error"
    assert VS.check("ndbreach", versions=RELEASE) == []


# -------------------------------------------------------------- adopt_folder --
@test
def suite116_adopt_folder_sets_up_the_whole_suite_from_one_path():
    with _Isolated() as iso:
        got = SD.adopt_folder(iso.folder)
        assert sorted(got["created"]) == sorted(SD.SUBFOLDERS), got["created"]
        for sub in SD.SUBFOLDERS:
            assert os.path.isdir(os.path.join(iso.folder, sub)), sub
        assert SD.data_folder() == iso.folder
        assert got["dams"] == os.path.join(iso.folder, "Dams")
        assert os.path.isdir(got["dams"])


@test
def suite116_adopt_folder_adopts_a_pack_in_the_folder_and_clears_one_that_is_gone():
    with _Isolated() as iso:
        SD.set_pack_location(os.path.join(iso.folder, "nope"))
        SD.adopt_folder(iso.folder)
        assert SD.pack_location() == "", SD.pack_location()
        pack = os.path.join(iso.folder, "ND_Dam_Suite_Pack")
        os.makedirs(pack)
        with open(os.path.join(pack, "manifest.json"), "w", encoding="utf-8") as f:
            f.write('{"datasets": {}}')
        SD._PACK_MANIFESTS.clear()
        got = SD.adopt_folder(iso.folder)
        assert got["pack"] == pack, got["pack"]
        assert SD.data_source() == SD.SOURCE_PACK


@test
def suite116_adopt_folder_never_takes_a_dams_folder_the_user_already_chose():
    with _Isolated() as iso:
        mine = os.path.join(iso.home, "MyDams")
        os.makedirs(mine)
        SD.set_dams_folder(mine)
        assert SD.adopt_folder(iso.folder)["dams"] == mine


@test
def suite116_subfolder_info_explains_every_subfolder():
    with _Isolated() as iso:
        SD.adopt_folder(iso.folder)
        got = SD.subfolder_info(iso.folder)
        assert sorted(n for n, *_r in got) == sorted(SD.SUBFOLDERS)
        for name, path, label, why, _where, count in got:
            assert os.path.isdir(path), name
            assert label and why, name
            assert count == 0


# ---------------------------------------------------------------- suitecheck --
@test
def suite116_no_suite_folder_is_the_one_thing_that_stops_everything():
    items = SC.check("", versions={}, charts=False, plugins=False)
    assert SC.by_key(items)["folder"]["level"] == "error"
    assert SC.blocking(items)
    assert "stop a run" in SC.summary_text(items)


@test
def suite116_a_fresh_folder_lets_the_updater_run_but_not_the_screen():
    with _Isolated() as iso:
        SD.adopt_folder(iso.folder)
        items = SC.check(iso.folder, versions=RELEASE, charts=False)
        steps = {s["key"]: s for s in SC.step_rows(items, versions=RELEASE)}
        # step 1 is what builds the pack, so a missing pack must not block it
        assert steps["data"]["ready"], steps["data"]["blocked_by"]
        assert steps["model"]["ready"], steps["model"]["blocked_by"]
        assert not steps["screen"]["ready"]
        assert "Offline pack" in steps["screen"]["blocked_by"]
        assert not steps["classify"]["ready"]


@test
def suite116_a_step_whose_plugin_is_missing_says_so():
    with _Isolated() as iso:
        SD.adopt_folder(iso.folder)
        vers = dict(RELEASE, ndbreach=None)
        items = SC.check(iso.folder, versions=vers, charts=False)
        steps = {s["key"]: s for s in SC.step_rows(items, versions=vers)}
        assert not steps["model"]["ready"]
        assert "not installed" in steps["model"]["blocked_by"]


@test
def suite116_an_old_plugin_is_a_warning_that_names_this_release():
    with _Isolated() as iso:
        SD.adopt_folder(iso.folder)
        items = SC.check(iso.folder, versions={"ndhazard": "2.20.0"}, charts=False)
        row = SC.by_key(items)["plugin_ndhazard"]
        assert row["level"] == "warn", row
        assert "2.34.5" in row["text"]


@test
def suite116_the_files_a_user_supplies_are_warnings_not_errors():
    # a novice who only wants one dam must not be stopped by a missing DWR export
    with _Isolated() as iso:
        SD.adopt_folder(iso.folder)
        items = SC.by_key(SC.check(iso.folder, versions=RELEASE, charts=False))
        assert items["dwr_dams"]["level"] == "warn"
        assert items["hifld"]["level"] == "warn"
        assert items["nlcd"]["level"] == "warn"
        assert items["dwr_dugouts"]["level"] == "info"       # optional
        assert items["dem"]["level"] == "info"               # a run can download terrain
        for key in ("dwr_dams", "hifld", "nlcd"):
            assert items[key]["fix"], key
            assert items[key]["folder"], key


@test
def suite116_check_my_setup_is_pasteable_text():
    with _Isolated() as iso:
        SD.adopt_folder(iso.folder)
        items = SC.check(iso.folder, versions=RELEASE, charts=False)
        text = SC.text_report(items, iso.folder, SC.step_rows(items, versions=RELEASE))
        assert "ND dam suite set-up" in text
        assert "Suite folder: " + iso.folder in text
        assert "STOP" in text                  # no pack yet
        assert "1. Data" in text
        assert text.endswith("\n")


@test
def suite116_the_chart_signoff_is_read_from_the_chart_file_itself():
    # the charts are the default hazard criterion, so "is it signed off" has to be answered
    # from the file the plugins actually read, not from the sign-off tool's own module
    row = SC.by_key(SC._chart_items())["charts"]
    assert row["level"] == "ok", row
    assert "signed off by" in row["text"], row["text"]
    with _Isolated() as iso:                   # and the whole check carries it
        SD.adopt_folder(iso.folder)
        got = SC.by_key(SC.check(iso.folder, versions=RELEASE))
        assert "charts" in got, sorted(got)
        assert got["charts"]["level"] == "ok"


@test
def suite116_an_unsigned_chart_file_is_a_warning_naming_the_sign_off_tool():
    core = os.path.join(tempfile.mkdtemp(), "core")
    os.makedirs(core)
    path = os.path.join(core, SC.CHART_FILE_NAME)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"status": "not digitized", "signed_off_by": ""}, f)
    paths, SC._chart_paths = SC._chart_paths, lambda: [path]
    try:
        row = SC.by_key(SC._chart_items())["charts"]
        assert row["level"] == "warn", row
        assert "not signed off" in row["text"]
        assert "Chart sign-off" in row["fix"]
        assert "conservative rule" in row["fix"]     # and what happens meanwhile
    finally:
        SC._chart_paths = paths


# ------------------------------------------------------------------ priority --
@test
def suite116_medium_and_significant_are_the_same_tier():
    # the ND rule says Medium where DWR says Significant: counting those as a mismatch would
    # fill the review list with hundreds of dams that actually agree
    assert PR.tier("Medium") == PR.tier("Significant") == 2
    assert PR.verdict(_row("A", "Medium", "Significant"))[0] == "agree"
    assert PR.tier("Significant (DWR)") == 2
    assert PR.tier("") is None and PR.tier("unclassified") is None


@test
def suite116_the_review_list_puts_a_raised_class_first_and_incomplete_second():
    rows = [_row("agree1", "Low", "Low"), _row("raise1", "High", "Low"),
            _row("lower1", "Low", "High"),
            _row("inc1", "Medium", "Medium", problem="flood cut off (duration cap)"),
            _row("none1")]
    got = PR.rank(rows)
    assert [r["dam_id"] for r in got] == ["raise1", "inc1", "lower1", "agree1", "none1"]
    assert [r["order"] for r in got] == [1, 2, 3, 4, 5]
    assert [r["dam_id"] for r in PR.attention(got)] == ["raise1", "inc1"]
    assert "screen higher" in PR.summary_text(got)


@test
def suite116_an_incomplete_run_is_a_floor_even_when_it_agrees():
    got = PR.verdict(_row("x", "Medium", "Significant",
                          next_text="truncated at terrain edge"))
    assert got[0] == "incomplete"
    assert "no model run" in PR._flags(_row("y", "High", "High", has_rasters=False,
                                            has_manifest=False))


@test
def suite116_the_priority_table_and_csv_carry_every_column():
    rows = PR.rank([_row("A", "High", "Low", name='He said "hi", ok')])
    assert PR.table(rows)[0] == list(PR.COLUMNS)
    text = PR.to_csv(rows)
    assert text.startswith("Dam ID,Name,")
    assert '"He said ""hi"", ok"' in text


# ------------------------------------------------------------------ packdiff --
def _man(**sets):
    return {"datasets": dict(sets)}


@test
def suite116_the_pack_diff_sees_what_was_added_removed_and_grew():
    old = _man(NSI_structures={"count": 100, "fetched": "2026-01-01"},
               ND_ROADS={"count": 50, "fetched": "2026-01-01"},
               GONE={"count": 5, "fetched": "2026-01-01"})
    new = _man(NSI_structures={"count": 140, "fetched": "2026-06-01"},
               ND_ROADS={"count": 50, "fetched": "2026-06-01"},
               SARP_dams={"count": 7888, "fetched": "2026-06-01"})
    got = {c["name"]: c for c in PD.diff(old, new)}
    assert got["NSI_structures"]["kind"] == "grew" and got["NSI_structures"]["delta"] == 40
    assert got["GONE"]["kind"] == "removed"
    assert got["SARP_dams"]["kind"] == "added"
    assert got["ND_ROADS"]["kind"] == "refreshed"
    assert got["NSI_structures"]["affects"]


@test
def suite116_a_layer_knows_whether_it_changes_who_is_counted():
    assert PD.role("NSI_structures")[0] == "consequence"
    assert PD.role("NARN_rail")[0] == "access"
    assert PD.role("HIFLD_fire")[0] == "lifeline"
    assert PD.role("3DHP_flowlines")[0] == "model"
    assert PD.affects_result("DWR_dams")
    assert not PD.affects_result("NLCD_2021")


@test
def suite116_a_one_feature_wobble_in_a_hundred_thousand_is_not_growth():
    got = PD.diff(_man(X={"count": 100000, "fetched": "a"}),
                  _man(X={"count": 100001, "fetched": "b"}))
    assert got[0]["kind"] == "refreshed"
    same = _man(X={"count": 1, "fetched": "a"})
    assert PD.diff(same, same) == []
    assert len(PD.diff(same, same, include_unchanged=True)) == 1


@test
def suite116_only_a_dam_with_a_result_goes_stale_and_silence_never_clears_one():
    rows = [_row("A", "High", "Low"), _row("B")]
    people = PD.diff(_man(NSI={"count": 1, "fetched": "a"}),
                     _man(NSI={"count": 9, "fetched": "b"}))
    model = PD.diff(_man(NLCD={"count": 1, "fetched": "a"}),
                    _man(NLCD={"count": 2, "fetched": "b"}))
    assert [r["dam_id"] for r, _w in PD.stale_dams(rows, people)] == ["A"]
    assert [r["dam_id"] for r, _w in PD.stale_dams(rows, model)] == ["A"]
    assert PD.stale_dams(rows, []) == []
    # a caller that tested the flood footprints may clear a dam; saying nothing may not
    cleared = PD.stale_dams([_row("A", "High"), _row("B", "High")], people,
                            footprints={"A": False})
    assert [r["dam_id"] for r, _w in cleared] == ["B"]
    assert len(PD.stale_dams([_row("A", "High"), _row("B", "High")], people,
                             footprints={})) == 2


# ------------------------------------------------------------------- cascade --
_SQUARE = [[(-100.0, 46.0), (-99.0, 46.0), (-99.0, 47.0), (-100.0, 47.0)]]


@test
def suite116_a_dam_inside_the_flood_is_downstream_and_the_source_is_not():
    dams = [{"dam_id": "SRC", "lon": -99.9, "lat": 46.1},
            {"dam_id": "NEAR", "lon": -99.9001, "lat": 46.1},   # the same structure again
            {"dam_id": "DOWN", "lon": -99.2, "lat": 46.8},
            {"dam_id": "OUT", "lon": -98.0, "lat": 46.5}]
    got = CA.downstream(dams, _SQUARE, source=(-99.9, 46.1), source_id="SRC")
    assert [h["dam_id"] for h in got] == ["DOWN"]
    assert CA.in_ring((-100.0, 46.5), _SQUARE[0])               # a point on the edge counts
    assert not CA.in_ring((-98.5, 46.5), _SQUARE[0])


@test
def suite116_the_nearest_downstream_dam_comes_first_and_the_hydrograph_travels():
    dams = [{"dam_id": "FAR", "lon": -99.1, "lat": 46.9},
            {"dam_id": "CLOSE", "lon": -99.8, "lat": 46.2}]
    got = CA.downstream(dams, _SQUARE, source=(-99.9, 46.1))
    assert [h["dam_id"] for h in got] == ["CLOSE", "FAR"]
    assert got[0]["distance_mi"] < got[1]["distance_mi"]
    assert "model it next" in CA.summary_text(got)
    assert "does not reach" in CA.summary_text([])
    job = CA.next_job(got[0], os.path.join("/dams"), hydrograph="/x/outflow.csv",
                      source_id="D1")
    assert job["folder"] == os.path.join("/dams", "CLOSE")
    assert job["inflow"] == "/x/outflow.csv" and job["inflow_role"] == CA.ROLE_INFLOW
    assert "D1" in job["note"]


@test
def suite116_a_flood_polygon_can_come_straight_from_geojson():
    gj = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [
            [[-100.0, 46.0], [-99.0, 46.0], [-99.0, 47.0], [-100.0, 47.0]]]}}]}
    rings = CA.rings_from_geojson(gj)
    assert len(rings) == 1
    assert CA.bbox(rings) == (-100.0, 46.0, -99.0, 47.0)
    assert CA.in_flood((-99.5, 46.5), rings)
    assert abs(CA.miles((-100.0, 46.0), (-100.0, 47.0)) - 69.0) < 0.5


# ---------------------------------------------------------------------- loss --
@test
def suite116_flood_severity_follows_depth_and_velocity():
    assert LO.severity(0.05, 10) == ""                 # below the counting depth: dry
    assert LO.severity(1.0, 1.0) == "low"
    assert LO.severity(10.0, 5.0) == "medium"          # DV 50
    assert LO.severity(10.0, 20.0) == "high"           # DV 200
    assert LO.severity(20.0, 0.1) == "high"            # deep and slow destroys a house too


@test
def suite116_warning_time_bands_and_population_day_and_night():
    assert LO.warning_band(5) == "under15"
    assert LO.warning_band(30) == "15to60"
    assert LO.warning_band(600) == "over60"
    assert LO.warning_band(None) == ""
    r = {"pop2amu65": 2, "pop2amo65": 1, "pop2pmu65": 0, "pop2pmo65": 1}
    assert LO.people(r, "night") == 3.0 and LO.people(r, "day") == 1.0
    assert LO.people({"habitable": True}) == LO.DEFAULT_PEOPLE
    assert LO.people({"habitable": False}) == 0.0


@test
def suite116_loss_of_life_waits_for_an_engineer_to_sign_a_rate_table():
    # the same rule the ACER TM 11 charts follow: a judgement belongs to a named engineer
    rows = [{"depth_ft": 10, "velocity_fts": 5, "arrival_min": 10, "pop2amu65": 4},
            {"depth_ft": 1, "velocity_fts": 1, "arrival_min": 200, "pop2amu65": 2},
            {"depth_ft": 0.0, "velocity_fts": 0, "arrival_min": 5, "pop2amu65": 9}]
    est = LO.estimate(rows)
    assert est["wet"] == 2 and est["par"] == 6.0
    assert est["fatalities"] is None and not est["rates_ok"]
    assert "not estimated" in "\n".join(LO.estimate_text(est))
    assert est["by_severity"]["medium"] == 4.0
    assert est["by_band"]["under15"] == 4.0
    unsigned = {"rates": {("high", "under15"): 0.75}}
    assert not LO.rate_table_status(unsigned)[0]
    assert LO.estimate(rows, rates=unsigned)["fatalities"] is None


@test
def suite116_a_signed_rate_table_is_used_and_says_who_signed_it():
    rates = {"signed_off_by": "N. Bendickson", "source": "test",
             "rates": {("high", "under15"): 0.5}}
    est = LO.estimate([{"depth_ft": 20, "velocity_fts": 20, "arrival_min": 1,
                        "pop2amu65": 10}], rates=rates)
    assert est["rates_ok"]
    assert abs(est["fatalities"] - 5.0) < 1e-9
    assert "N. Bendickson" in est["rates_text"]
    both = LO.both_times([{"depth_ft": 5, "velocity_fts": 1, "arrival_min": 10,
                           "pop2amu65": 3, "pop2pmu65": 1}])
    assert both["night"]["par"] == 3.0 and both["day"]["par"] == 1.0
