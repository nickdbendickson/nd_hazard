"""The live progress line, the plain-words end-of-run block, the change report and the data
vintage (1.4.0) - all of them readings of a pack manifest, so a hand-written manifest is the
whole fixture."""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
if os.path.dirname(os.path.dirname(HERE)) not in sys.path:
    sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
from nd_data_updater import runreport as RR  # noqa: E402
from nd_data_updater.tests.registry import test  # noqa: E402


def _entry(label, count, fetched="2026-02-11", status="complete", source="an agency", **kw):
    e = {"label": label, "count": count, "fetched": fetched, "status": status, "source": source}
    e.update(kw)
    return e


def _manifest(**kw):
    man = {"format": "nd-dam-suite-pack", "datasets": {
        "NSI_structures": _entry("USACE National Structure Inventory", 412913),
        "ND_roads_NG911": _entry("ND GIS Hub NG9-1-1 road centerlines", 310000),
        "NID_dams": _entry("National Inventory of Dams", 940),
        "NDDOT_traffic_counts": _entry("NDDOT traffic counts", 0, status="not available",
                                       note="HTTP 403 from the service"),
        "OSM_places": _entry("OpenStreetMap places", 1800),
    }, "rasters": {"NLCD_landcover": _entry("NLCD 2021 land cover", None,
                                            source="MRLC WCS")},
        "left_out": {"HIFLD_fire_stations": "no HIFLD file in the data folder"}}
    man.update(kw)
    return man


def _pack(man=None):
    d = tempfile.mkdtemp()
    with open(os.path.join(d, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(_manifest() if man is None else man, f)
    return d


class Result:
    def __init__(self, name, status, message=""):
        self.name, self.status, self.message = name, status, message


# ------------------------------------------------------------------ the progress line
@test
def test_parse_step_reads_the_builders_log_line():
    assert RR.parse_step("[12/29] USACE National Structure Inventory") == (
        12, 29, "USACE National Structure Inventory")
    assert RR.parse_step("  [3/4] ND roads") == (3, 4, "ND roads")
    assert RR.parse_step("downloading county 12 of 53") is None
    assert RR.parse_step("[0/0] nothing") is None
    assert RR.parse_step(None) is None


@test
def test_step_tracker_makes_the_one_line_message():
    steps = RR.StepTracker()
    assert steps.line("something") == "something"          # before the first step
    steps.note("[12/29] USACE National Structure Inventory")
    assert steps.line() == "layer 12 of 29: USACE National Structure Inventory"
    # the builder repeats the label in its sub-message; it is not said twice
    line = steps.line("USACE National Structure Inventory: county 12 of 53")
    assert line == "layer 12 of 29: USACE National Structure Inventory - county 12 of 53"
    steps.note("  done: 412,913 features in 904 s")        # not a step: the layer does not move
    assert steps.line().startswith("layer 12 of 29")
    steps.note("[13/29] ND GIS Hub NG9-1-1 road centerlines")
    assert steps.line("writing") == "layer 13 of 29: ND GIS Hub NG9-1-1 road centerlines - writing"


# ------------------------------------------------------------------ the end-of-run block
@test
def test_plain_name_uses_everyday_words():
    assert RR.plain_name("NDDOT_traffic_counts") == "traffic counts"
    assert RR.plain_name("OSM_crossings").startswith("unsurveyed crossings")
    assert RR.plain_name("Something_new", "Its own label") == "Its own label"
    assert RR.plain_name("Something_new") == "Something_new"


@test
def test_layer_rows_cover_the_layers_the_rasters_and_what_was_left_out():
    pack = _pack()
    rows = {r["name"]: r for r in RR.layer_rows(pack)}
    assert len(rows) == 7
    assert rows["NSI_structures"]["count"] == 412913
    assert rows["NLCD_landcover"]["kind"] == "raster"
    assert rows["HIFLD_fire_stations"]["kind"] == "missing"
    assert rows["NSI_structures"]["role"] == "consequence"
    assert rows["ND_roads_NG911"]["role"] == "access"


@test
def test_pack_summary_lines_name_what_is_missing_and_what_it_costs():
    lines = RR.pack_summary_lines(_pack())
    assert lines[0] == "Offline pack: 5 of 7 layers built."
    missing = [x for x in lines if x.startswith("Not fetched")][0]
    assert "traffic counts" in missing
    assert "HIFLD_fire_stations" in missing or "HIFLD" in missing
    plugins = [x for x in lines if x.startswith("The plugins run without these")][0]
    assert "roads, railroads and access" in plugins or "lifelines" in plugins
    assert any('no traffic counts' in x for x in lines)


@test
def test_pack_summary_says_so_when_everything_is_there():
    man = _manifest()
    man["datasets"]["NDDOT_traffic_counts"]["status"] = "complete"
    man["left_out"] = {}
    lines = RR.pack_summary_lines(_pack(man))
    assert lines[0] == "Offline pack: 6 of 6 layers built."
    assert lines[1].startswith("Every layer")


@test
def test_run_summary_counts_the_datasets_and_names_the_failures():
    lines = RR.run_summary([Result("NSI", "updated"), Result("OpenStreetMap", "current"),
                            Result("Microsoft footprints", "offline", "using local copy")],
                           _pack())
    assert lines[0] == "Datasets: 1 downloaded, 1 already up to date, 1 could not be reached."
    assert any("Microsoft footprints" in x and "still used" in x for x in lines)
    assert any(x.startswith("Offline pack:") for x in lines)


@test
def test_run_summary_tolerates_no_pack_and_no_results():
    assert RR.run_summary([], "") == ["Nothing to do."]
    with tempfile.TemporaryDirectory() as d:
        assert RR.pack_summary_lines(d) == ["Offline pack: not built yet."]


# ------------------------------------------------------------------ the change report
@test
def test_change_report_needs_a_kept_copy_first():
    pack = _pack()
    report = RR.change_report(pack, dams="")
    assert report["have_old"] is False
    assert "nothing to compare" in report["summary"]


@test
def test_change_report_names_the_new_structures_and_the_stale_dams():
    pack = _pack()
    old = RR.read_manifest(pack)
    assert RR.save_previous(pack, old).endswith(RR.PREVIOUS_NAME)
    new = _manifest()
    new["datasets"]["NSI_structures"]["count"] = 420000      # 7,000 new structures
    new["datasets"]["ND_address_points"] = _entry("ND address points", 55000)
    with open(os.path.join(pack, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(new, f)
    rows = [{"dam_id": "ND00123", "class": "High", "folder": "/dams/ND00123"},
            {"dam_id": "ND00124", "class": "", "folder": "/dams/ND00124"}]
    report = RR.change_report(pack, scan=lambda: rows)
    assert report["have_old"] is True
    names = {c["name"]: c for c in report["changes"]}
    assert names["NSI_structures"]["kind"] == "grew"
    assert names["NSI_structures"]["delta"] == 7087
    assert names["ND_address_points"]["kind"] == "added"
    assert len(report["stale"]) == 1                          # only the classified dam
    assert report["stale"][0][0]["dam_id"] == "ND00123"
    assert "ND00123" in report["stale_text"]
    assert "NSI_structures" in report["text"]
    assert "Already-classified dams" in report["text"]
    path = RR.save_report(pack, report["text"])
    assert os.path.isfile(path) and path.endswith(RR.REPORT_NAME)


@test
def test_change_report_says_nothing_changed_when_nothing_did():
    pack = _pack()
    RR.save_previous(pack)
    report = RR.change_report(pack, scan=lambda: [])
    assert report["changes"] == []
    assert "Nothing changed" in report["summary"]
    assert "No classified dam is affected" in report["stale_text"]


@test
def test_change_report_covers_the_rasters_too():
    pack = _pack()
    RR.save_previous(pack)
    new = _manifest()
    new["rasters"].pop("NLCD_landcover")
    with open(os.path.join(pack, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(new, f)
    report = RR.change_report(pack, scan=lambda: [])
    assert [c["name"] for c in report["changes"]] == ["NLCD_landcover"]
    assert report["changes"][0]["kind"] == "removed"
    assert report["changes"][0]["role"] == "model"


# ------------------------------------------------------------------ the data vintage
@test
def test_vintage_rows_are_what_a_report_stamps_itself_with():
    rows = dict((r[0], r) for r in RR.vintage_rows(_pack()))
    assert "HIFLD_fire_stations" not in rows                   # not in the pack: not stamped
    name, source, when, count = rows["NSI_structures"]
    assert (source, when, count) == ("an agency", "2026-02-11", 412913)
    assert rows["NLCD_landcover"][1] == "MRLC WCS"
    text = RR.vintage_text(pack=_pack())
    assert "NSI_structures" in text and "412,913 features" in text
    assert RR.vintage_text([]) .startswith("No data vintage")


@test
def test_pack_age_days_is_the_oldest_layer():
    import datetime
    man = _manifest()
    man["datasets"]["NSI_structures"]["fetched"] = (
        datetime.datetime.now() - datetime.timedelta(days=40)).isoformat(timespec="seconds")
    age = RR.pack_age_days(_pack(man), now=datetime.datetime.now())
    assert age is not None and age > 39.0


# ------------------------------------------------- the layers holding the user's own file
@test
def test_layer_rows_and_replaced_layers_show_who_put_the_file_in_and_when():
    man = _manifest()
    man["datasets"]["ND_county_roads"] = _entry(
        "ND GIS Hub county roads", 48000, source="your own file, put in by hand: /x/roads.gpkg",
        replaced=True, replaced_at="2026-10-13T09:15:00", replaced_by="edamsafety")
    pack = _pack(man)
    rows = {r["name"]: r for r in RR.layer_rows(pack)}
    assert rows["ND_county_roads"]["replaced"] is True
    assert rows["ND_county_roads"]["replaced_by"] == "edamsafety"
    assert rows["NSI_structures"]["replaced"] is False
    kept = RR.replaced_layers(pack)
    assert kept == [("county roads", "ND_county_roads", "2026-10-13", "edamsafety")]


@test
def test_the_end_of_run_block_says_a_replaced_layer_was_left_alone():
    man = _manifest()
    man["datasets"]["ND_county_roads"] = _entry("county roads", 48000, replaced=True,
                                                replaced_at="2026-10-13T09:15:00",
                                                replaced_by="edamsafety")
    lines = RR.pack_summary_lines(_pack(man))
    kept = [x for x in lines if x.startswith("Left alone (your own file)")]
    assert len(kept) == 1
    assert "county roads" in kept[0]
    assert "Fetch again" in kept[0] and "downloaded version" in kept[0]
    # and it is said even when nothing at all was missing
    man["datasets"]["NDDOT_traffic_counts"]["status"] = "complete"
    man["left_out"] = {}
    lines = RR.pack_summary_lines(_pack(man))
    assert lines[1].startswith("Every layer")
    assert lines[-1].startswith("Left alone (your own file)")


@test
def test_clear_replaced_only_touches_the_layers_it_is_given():
    man = _manifest()
    man["datasets"]["ND_county_roads"] = _entry("county roads", 1, replaced=True,
                                                replaced_at="2026-10-13T09:15:00",
                                                replaced_by="edamsafety")
    man["rasters"]["NLCD_landcover"]["replaced"] = True
    pack = _pack(man)
    assert RR.clear_replaced(pack, ["ND_county_roads", "not_a_layer"]) == ["ND_county_roads"]
    after = RR.read_manifest(pack)
    assert "replaced" not in after["datasets"]["ND_county_roads"]
    assert after["rasters"]["NLCD_landcover"]["replaced"] is True    # not asked for: untouched
    assert after["datasets"]["NSI_structures"]["count"] == 412913
    assert RR.clear_replaced(pack, []) == []
    with tempfile.TemporaryDirectory() as d:
        assert RR.clear_replaced(d, ["anything"]) == []              # no pack: no crash
