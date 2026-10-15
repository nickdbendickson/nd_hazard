"""The pack source's build plan (1.4.0): "fetch this one layer again" and "only the missing
layers", which are the two things the window can now ask for. A fake builder stands in for
ndpack/build_nd_pack.py, so no GDAL and no network are needed."""
import datetime
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
if os.path.dirname(os.path.dirname(HERE)) not in sys.path:
    sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
from nd_data_updater import packsource as PS  # noqa: E402
from nd_data_updater.tests.registry import test  # noqa: E402

TABLE = [
    ("NSI_structures", "USACE National Structure Inventory", "nsi", "nsi", {}),
    ("ND_roads_NG911", "ND roads", "roads", "arcgis", {}),
    ("NID_dams", "National Inventory of Dams", "nid", "arcgis", {}),
    ("DWR_dams", "DWR dams", "dwr", "file", {}),
    ("NLCD_landcover", "NLCD 2021 land cover", "landcover", "raster", {}),
]


class Builder:
    """Only datasets() is needed to make a plan."""

    @staticmethod
    def datasets():
        return list(TABLE)


def _source(folder, **kw):
    return PS.PackSource("pack", "Offline pack", "ND_Dam_Suite_Pack", folder,
                         builder=Builder(), **kw)


def _write(pack, datasets, rasters=None, replaced=()):
    os.makedirs(pack, exist_ok=True)
    fresh = datetime.datetime.now().isoformat(timespec="seconds")
    man = {"format": "nd-dam-suite-pack", "format_version": 2, "layout": "one-gpkg-per-dataset",
           "datasets": {}, "rasters": rasters or {}, "left_out": {}}
    for name, when in datasets.items():
        entry = {"label": name, "count": 10, "status": "complete",
                 "fetched": fresh if when is None else when}
        if name in (replaced or ()):
            entry.update({"replaced": True, "replaced_at": "2026-10-13T09:15:00",
                          "replaced_by": "edamsafety"})
        man["datasets"][name] = entry
    with open(os.path.join(pack, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(man, f)
    return man


@test
def test_plan_builds_what_is_missing_and_refreshes_what_is_old():
    with tempfile.TemporaryDirectory() as d:
        pack = os.path.join(d, "ND_Dam_Suite_Pack")
        old = (datetime.datetime.now() - datetime.timedelta(days=90)).isoformat(timespec="seconds")
        _write(pack, {"NSI_structures": None, "ND_roads_NG911": old})
        missing, stale, why = _source(d).plan(pack)
        assert "NID_dams" in missing
        assert stale == ["ND_roads_NG911"]
        assert "not in the pack yet" in why and "older than 30 days" in why


@test
def test_missing_only_leaves_every_layer_it_has_alone():
    with tempfile.TemporaryDirectory() as d:
        pack = os.path.join(d, "ND_Dam_Suite_Pack")
        old = (datetime.datetime.now() - datetime.timedelta(days=400)).isoformat(timespec="seconds")
        _write(pack, {"NSI_structures": old, "ND_roads_NG911": old})
        missing, stale, why = _source(d, missing_only=True).plan(pack)
        assert "NID_dams" in missing
        assert stale == []
        assert "only the missing layers" in why


@test
def test_only_layers_asks_for_exactly_the_named_layers():
    with tempfile.TemporaryDirectory() as d:
        pack = os.path.join(d, "ND_Dam_Suite_Pack")
        _write(pack, {"NSI_structures": None, "ND_roads_NG911": None})
        src = _source(d, only_layers=["ND_roads_NG911", "NID_dams", "not_a_layer"])
        missing, stale, why = src.plan(pack)
        assert stale == ["ND_roads_NG911"]         # it is there: fetched again
        assert missing == ["NID_dams"]             # it is not: built
        assert why == "2 layer(s) you asked for again"


@test
def test_only_layers_knows_a_raster_it_already_has():
    with tempfile.TemporaryDirectory() as d:
        pack = os.path.join(d, "ND_Dam_Suite_Pack")
        _write(pack, {"NSI_structures": None}, rasters={"NLCD_landcover": {"count": None}})
        missing, stale, _why = _source(d, only_layers=["NLCD_landcover"]).plan(pack)
        assert (missing, stale) == ([], ["NLCD_landcover"])


@test
def test_the_run_choices_are_not_part_of_the_config_key():
    """A one-shot "fetch this again" must not make every other layer look out of date."""
    with tempfile.TemporaryDirectory() as d:
        plain = _source(d).config_key
        assert _source(d, only_layers=["NID_dams"]).config_key == plain
        assert _source(d, missing_only=True).config_key == plain
        assert _source(d, refresh_days=60).config_key != plain


@test
def test_minutes_3dhp_has_a_floor_and_a_default():
    with tempfile.TemporaryDirectory() as d:
        assert _source(d).minutes_3dhp == PS.DEFAULT_3DHP_MINUTES
        assert _source(d, minutes_3dhp=0).minutes_3dhp == PS.DEFAULT_3DHP_MINUTES
        assert _source(d, minutes_3dhp=12).minutes_3dhp == 12


@test
def test_replace_info_records_the_users_file_as_the_source():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "county_roads_2026.gpkg")
        with open(path, "wb") as f:
            f.write(b"x")
        info = PS.replace_info("ND_county_roads", path, table=TABLE)
        assert info["url"] == "county_roads_2026.gpkg"
        assert "your own file" in info["source"] and path in info["source"]
        assert info["status"] == "complete" and info["replaced"] is True
        assert info["fetched"] == datetime.date.fromtimestamp(
            os.path.getmtime(path)).isoformat()
        assert PS.replace_info("ND_roads_NG911", path, table=TABLE)["label"] == "ND roads"
        assert PS.replace_info("unknown_layer", path)["label"] == "unknown_layer"


@test
def test_vintage_and_layer_rows_are_reachable_from_packsource():
    with tempfile.TemporaryDirectory() as d:
        pack = os.path.join(d, "ND_Dam_Suite_Pack")
        _write(pack, {"NSI_structures": None})
        assert [r["name"] for r in PS.layer_rows(pack)] == ["NSI_structures"]
        assert PS.vintage_rows(pack)[0][0] == "NSI_structures"


# ------------------------------------------------- a layer holding the user's own file
@test
def test_a_replaced_layer_survives_a_refresh_that_would_have_fetched_it():
    """The user put their own county roads file in; day 31 must not throw it away."""
    with tempfile.TemporaryDirectory() as d:
        pack = os.path.join(d, "ND_Dam_Suite_Pack")
        old = (datetime.datetime.now() - datetime.timedelta(days=400)).isoformat(timespec="seconds")
        _write(pack, {"NSI_structures": old, "ND_roads_NG911": old},
               replaced=["ND_roads_NG911"])
        src = _source(d)
        missing, stale, why = src.plan(pack)
        assert stale == ["NSI_structures"]              # the downloaded layer is refreshed
        assert "ND_roads_NG911" not in stale            # the user's file is left alone
        assert "ND_roads_NG911" not in missing
        assert src.kept_replaced == ["ND_roads_NG911"]
        assert "left alone (your own file: ND_roads_NG911)" in why
        # "fetch every layer again" and "only missing layers" leave it alone as well
        full = _source(d, full=True)
        assert "ND_roads_NG911" not in full.plan(pack)[1]
        only_missing = _source(d, missing_only=True)
        assert only_missing.plan(pack)[1] == []
        assert "ND_roads_NG911" not in only_missing.plan(pack)[0]


@test
def test_an_explicit_fetch_again_still_overwrites_the_users_file():
    with tempfile.TemporaryDirectory() as d:
        pack = os.path.join(d, "ND_Dam_Suite_Pack")
        _write(pack, {"ND_roads_NG911": None}, replaced=["ND_roads_NG911"])
        missing, stale, why = _source(d, only_layers=["ND_roads_NG911"]).plan(pack)
        assert stale == ["ND_roads_NG911"] and missing == []
        assert why == "1 layer(s) you asked for again"


@test
def test_fetching_a_replaced_layer_again_clears_the_flag():
    """After the user asks for the downloaded version, the layer is back in the normal cycle."""
    with tempfile.TemporaryDirectory() as d:
        pack = os.path.join(d, "ND_Dam_Suite_Pack")
        old = (datetime.datetime.now() - datetime.timedelta(days=400)).isoformat(timespec="seconds")
        _write(pack, {"ND_roads_NG911": old, "NSI_structures": old},
               replaced=["ND_roads_NG911"])
        from nd_data_updater import runreport as RR
        assert RR.clear_replaced(pack, ["ND_roads_NG911"]) == ["ND_roads_NG911"]
        assert RR.clear_replaced(pack, ["ND_roads_NG911"]) == []       # only once
        src = _source(d)
        _missing, stale, why = src.plan(pack)
        assert sorted(stale) == ["ND_roads_NG911", "NSI_structures"]
        assert src.kept_replaced == [] and "left alone" not in why
        entry = RR.read_manifest(pack)["datasets"]["ND_roads_NG911"]
        assert "replaced" not in entry and "replaced_by" not in entry
        assert entry["count"] == 10                                    # nothing else was touched


@test
def test_a_replaced_drop_in_layer_is_not_rebuilt_from_a_newer_file():
    """A DWR layer the user replaced is not taken from <data folder>/dwr again."""
    with tempfile.TemporaryDirectory() as d:
        pack = os.path.join(d, "ND_Dam_Suite_Pack")
        _write(pack, {"DWR_dams": None}, replaced=["DWR_dams"])
        missing, stale, _why = _source(d).plan(pack)
        assert "DWR_dams" not in missing and "DWR_dams" not in stale
        _write(pack, {"DWR_dams": None})
        # without the flag the DWR layer is left to the ordinary rules again
        assert _source(d).kept_replaced == []
