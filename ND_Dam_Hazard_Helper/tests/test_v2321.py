"""2.32.1: fixes from a check of an earlier code audit against the source.

  * downloadplan.download_plan - one download list for the run and the pre-run estimate (the
    estimate had drifted from pipeline._collect_inputs);
  * pipeline.evaluation_key holds the offline pack and the "offline only" switch;
  * rasimport plan cache key by content, not date;
  * settings profiles: a newer plugin's settings no longer mark a run "MODIFIED";
  * review merge overrides recorded as data (MergePlan.overridable);
  * pipeline._read_places reads a places layer without "name" / "place";
  * structures.mobile_rules_from_cfg reads text "false" as False;
  * linear.motorists - one motorist formula for the run and the sensitivity table.
Run: python3 -m ND_Dam_Hazard_Helper.tests.run_tests (from the folder above the plugin)."""
import itertools
import json
import os
import random
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_DIR = os.path.dirname(HERE)
if os.path.dirname(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, os.path.dirname(PLUGIN_DIR))
from ND_Dam_Hazard_Helper.tests.registry import test  # noqa: E402

_SWITCHES = ["download_nsi", "download_address", "download_usa_structures", "download_rail",
             "download_crossings", "download_roads", "download_dams", "download_lifelines",
             "download_crossings_structures", "download_camps", "download_trailer_parks",
             "download_places", "download_flowlines", "eap", "eap_travel", "termination"]
_ROLES = ["nsi", "address", "usa", "roads", "aadt", "rail", "crossings", "dams", "bridges",
          "camps", "parks", "places", "centerline", "lifelines"]
# pipeline.LOCAL_CFG_KEYS: a data-folder layer of these roles is put into the cfg
_ROLE_CFG = {"roads": "road_layer", "aadt": "aadt_layer", "rail": "rail_layer",
             "crossings": "crossing_layer", "centerline": "centerline_layer",
             "dams": "dam_layer"}
_DEDICATED = {"nsi", "address", "usa", "nid", "life_osm", "nbi", "xosm", "camps", "parks",
              "places"}


def _rules_2320(cfg, loc, local_roles, have_hub, fema):
    """pipeline._collect_inputs' download rules as 2.32.0 wrote them (key, label, the
    ArcGIS download name, area) - download_plan must give the same list."""
    from ND_Dam_Hazard_Helper import constants as C
    j = []
    if cfg["download_nsi"] and "nsi" not in loc:
        j.append(("nsi", "NSI", None, "study"))
    if not have_hub and cfg["download_address"]:
        j.append(("address", "ND GIS Hub address points", None, "study"))
    if cfg["download_usa_structures"] and "usa" not in loc and not fema:
        j.append(("usa", "FEMA USA Structures", None, "study"))
    if cfg["rail_layer"] is None and cfg["download_rail"]:
        j.append(("rail", "Rail lines", "NARN_rail_lines", "study"))
    if cfg["crossing_layer"] is None and cfg["download_crossings"]:
        j.append(("cross", "Grade crossings", "FRA_grade_crossings", "crossings"))
    if cfg["road_layer"] is None and cfg.get("download_roads"):
        j.append(("road", "ND GIS Hub roads", "ND_roads_NG911", "study"))
        if cfg["aadt_layer"] is None:
            for url in C.HPMS_ND_URLS:
                y = "".join(ch for ch in url.split("HPMS_FULL_ND_")[-1][:4] if ch.isdigit())
                j.append(("hpms_" + y, "HPMS {}".format(y), "HPMS_ND_{}".format(y), "study"))
            if C.NDDOT_AADT_URL:
                j.append(("nddot", "NDDOT traffic counts", "NDDOT_traffic_counts", "study"))
            j.append(("county", "ND county roads (AADT)", "ND_county_roads", "study"))
    if cfg.get("download_dams") and "dams" not in local_roles:
        j.append(("nid", "National Inventory of Dams", None, "study"))
    if cfg.get("download_lifelines"):
        for key, label, _url, _c in C.lifeline_downloads():
            j.append(("life_" + key, label, "lifeline_" + key, "study"))
        j.append(("life_osm", "OpenStreetMap lifelines", None, "study"))
    if cfg.get("download_crossings_structures") and "bridges" not in loc:
        j += [("nbi", "National Bridge Inventory", None, "study"),
              ("xosm", "OpenStreetMap crossings", None, "study")]
    if cfg.get("download_camps") and "camps" not in loc:
        j.append(("camps", "OpenStreetMap campgrounds", None, "study"))
    if cfg.get("download_trailer_parks", True) and "parks" not in loc:
        j.append(("parks", "OpenStreetMap mobile-home parks", None, "study"))
    travel = bool(cfg.get("eap")) and bool(cfg.get("eap_travel"))
    if travel and cfg.get("download_places") and "places" not in loc:
        j.append(("places", "OpenStreetMap places", None, "places"))
    if (travel or cfg.get("termination")) and cfg.get("centerline_layer") is None and \
            cfg.get("download_flowlines"):
        j.append(("flow", "USGS 3DHP flowlines", "3DHP_flowlines", "study"))
    return j


@test
def the_shared_download_plan_keeps_the_runs_rules():
    from ND_Dam_Hazard_Helper.downloadplan import download_plan
    rng = random.Random(2321)
    for _trial in range(3000):
        cfg = {k: rng.random() < 0.6 for k in _SWITCHES}
        roles = {r for r in _ROLES if rng.random() < 0.25}
        for k in ("road_layer", "aadt_layer", "rail_layer", "crossing_layer",
                  "centerline_layer"):
            cfg[k] = "a chosen layer" if rng.random() < 0.2 else None
        for r, k in _ROLE_CFG.items():                 # pipeline._apply_local_data
            if r in roles:
                cfg[k] = "a data-folder layer"
        loc = {r for r in roles if r not in _ROLE_CFG}
        have_hub = "address" in roles or rng.random() < 0.2
        fema = rng.random() < 0.3
        new = [(k, label, name if k not in _DEDICATED else None, area)
               for k, name, label, _kind, _url, area in download_plan(cfg, roles, have_hub,
                                                                       fema)]
        assert new == _rules_2320(cfg, loc, roles, have_hub, fema), (cfg, roles)


@test
def the_estimate_counts_the_downloads_the_run_makes():
    from ND_Dam_Hazard_Helper import estimate as EST
    saved = (EST._local_roles, EST._have_hub, EST._outlines_are_usa)
    try:
        EST._local_roles = lambda cfg: set(cfg.get("_roles", ()))
        EST._have_hub = lambda cfg, roles: "address" in roles
        area = {"extent_4326": (-100.0, 47.0, -99.9, 47.1)}
        cfg = {k: True for k in _SWITCHES}
        cfg.update(termination=False, road_layer=None, aadt_layer=None, rail_layer=None,
                   crossing_layer=None, centerline_layer=None)
        EST._outlines_are_usa = lambda cfg: False
        names = [n for n, *_r in EST.planned_downloads(cfg, area)]
        assert names[:6] == ["NSI_structures", "NDGISHUB_address_points", "USA_Structures",
                             "NARN_rail_lines", "FRA_grade_crossings", "ND_roads_NG911"], names
        # 2.32.0 counted these although the run does not download them
        EST._outlines_are_usa = lambda cfg: True
        names = [n for n, *_r in EST.planned_downloads(cfg, area)]
        assert "USA_Structures" not in names
        names = [n for n, *_r in EST.planned_downloads(dict(cfg, _roles=["parks", "places"]),
                                                      area)]
        assert "OSM_trailer_parks" not in names and "OSM_places" not in names, names
    finally:
        EST._local_roles, EST._have_hub, EST._outlines_are_usa = saved


@test
def the_evaluation_key_holds_the_offline_pack_and_switch():
    from ND_Dam_Hazard_Helper import download as DL
    from ND_Dam_Hazard_Helper import pipeline as PL
    d = tempfile.mkdtemp()
    pack = os.path.join(d, "nd_offline_pack.zip")
    with open(pack, "wb") as fh:
        fh.write(b"x" * 100)
    cfg = dict(PL.DEFAULTS, output_folder=d, dam_folder=d, scenarios=[])
    now = {"v": (pack, False)}
    saved = DL.offline_config
    try:
        DL.offline_config = lambda fallback=True: now["v"]
        k_pack = PL.evaluation_key(cfg)
        assert PL.evaluation_key(cfg) == k_pack
        now["v"] = (pack, True)
        assert PL.evaluation_key(cfg) != k_pack            # "offline only" on
        now["v"] = ("", False)
        assert PL.evaluation_key(cfg) != k_pack            # Data source = Download
        now["v"] = (pack, False)
        with open(pack, "wb") as fh:
            fh.write(b"x" * 120)
        assert PL.evaluation_key(cfg) != k_pack            # the pack was rebuilt

        def broken(fallback=True):
            raise RuntimeError("no settings")
        DL.offline_config = broken
        assert "error" in PL._offline_sig() and PL.evaluation_key(cfg)   # never raises
    finally:
        DL.offline_config = saved


@test
def the_plan_cache_key_follows_the_content_not_the_date():
    from ND_Dam_Hazard_Helper import rasimport as RI
    d = tempfile.mkdtemp()
    f = os.path.join(d, "dam.p01.hdf")
    with open(f, "wb") as fh:
        fh.write(b"A" * 3000000)
    st = os.stat(f)
    k = RI._content_sig(f)
    os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns + 5 * 10 ** 9))      # touched only
    assert RI._content_sig(f) == k
    with open(f, "r+b") as fh:                                         # a middle edit,
        fh.seek(1500000)                                               # same size and date
        fh.write(b"B")
    os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert RI._content_sig(f) != k
    try:
        RI._content_sig(os.path.join(d, "missing.hdf"))
        raise AssertionError("a missing plan must raise OSError")
    except OSError:
        pass


@test
def a_newer_plugins_profile_is_not_modified_by_loading_it():
    from ND_Dam_Hazard_Helper import settings as ST
    cur = {"near_spin": 1.0, "judgment_check": True, "model_combo": "acer"}
    prof = dict(cur, setting_of_a_newer_plugin=3)
    path = os.path.join(tempfile.mkdtemp(), "firm" + ST.PROFILE_EXT)
    with open(path, "w") as fh:
        json.dump({"format": ST.PROFILE_FORMAT, "format_version": 1, "name": "firm",
                   "sha256": ST.profile_hash(prof), "state": prof}, fh)
    state, meta = ST.load_profile(path, known_keys=list(cur))
    assert meta["unknown_keys"] == ["setting_of_a_newer_plugin"]
    st = ST.profile_status(cur, state, meta)
    assert not st["modified"] and st["changed_keys"] == [], st
    st = ST.profile_status(dict(cur, near_spin=2.0), state, meta)
    assert st["modified"] and st["changed_keys"] == ["near_spin"], st
    # a choice the project's list did not offer is named, not counted as applied
    assert ST.not_applied(dict(cur, model_combo="other"), state, meta) == ["model_combo"]


@test
def a_merge_records_which_refusals_may_be_overridden():
    from ND_Dam_Hazard_Helper import review_exchange as RX
    d = tempfile.mkdtemp()
    mine = os.path.join(d, "Mine", "outputs", "QA", "Structure_Review_Decisions.csv")
    os.makedirs(os.path.dirname(mine))
    theirs = os.path.join(d, "theirs.csv")
    with open(theirs, "w") as fh:
        fh.write("Lon,Lat,Category\n-100.0,47.0,Dwelling\n")
    hdr = {"sha256": "0" * 64, "dam": "Other Dam", "rows": 1}
    plan = RX.import_plan(mine, theirs, dam_name="Mine", header=hdr)
    assert plan.refused and plan.overridable == {"allow_other_dam", "accept_modified"}
    plan = RX.import_plan(mine, theirs, dam_name="Mine", header=hdr, allow_other_dam=True,
                          accept_modified=True)
    assert not plan.refused and plan.overridable == set()


@test
def a_places_layer_without_name_or_place_fields_is_read():
    import qgis.core as QC
    from ND_Dam_Hazard_Helper import pipeline as PL

    class Field(object):
        def __init__(self, n):
            self._n = n

        def name(self):
            return self._n

    class Point(object):
        def x(self):
            return 1.0

        def y(self):
            return 2.0

    class Geometry(object):
        def asPoint(self):
            return Point()

    class Feature(dict):
        def geometry(self):
            return Geometry()

    class Layer(object):
        def __init__(self, fields, feats):
            self._f, self._x = fields, feats

        def fields(self):
            return [Field(n) for n in self._f]

        def crs(self):
            return None

        def getFeatures(self):
            return iter(self._x)

    class Transform(object):
        def __init__(self, *a):
            pass

        def transform(self, p):
            return p
    saved = getattr(QC, "QgsCoordinateTransform", None)
    QC.QgsCoordinateTransform = Transform
    try:
        out = PL._read_places(Layer(["NAME", "osm_id"], [Feature(NAME="Fargo")]), None, None)
        assert out == [{"x": 1.0, "y": 2.0, "name": "Fargo", "place": ""}], out
        out = PL._read_places(Layer(["name", "place"], [Feature(name="Minot", place="city")]),
                              None, None)
        assert out[0]["name"] == "Minot" and out[0]["place"] == "city", out
    finally:
        QC.QgsCoordinateTransform = saved


@test
def manufactured_home_switches_read_text_false_as_off():
    from ND_Dam_Hazard_Helper import structures as SS
    keys = [k for k, v in SS.MOBILE_RULE_DEFAULTS.items() if isinstance(v, bool)]
    assert keys
    assert all(v is False for k, v in SS.mobile_rules_from_cfg(
        {k: "false" for k in keys}).items() if k in keys)
    assert all(v is True for k, v in SS.mobile_rules_from_cfg(
        {k: "Yes" for k in keys}).items() if k in keys)
    got = SS.mobile_rules_from_cfg({k: "maybe" for k in keys})
    assert all(got[k] == SS.MOBILE_RULE_DEFAULTS[k] for k in keys)


@test
def the_run_and_the_sensitivity_table_count_motorists_alike():
    from ND_Dam_Hazard_Helper import linear as LN
    from ND_Dam_Hazard_Helper import sensitivity as SX
    for aadt, length, mph, occ in itertools.product((40, 2500, 18000), (0.0, 300.0, 5280.0),
                                                    (0.5, 35, 65), (1.0, 1.5)):
        want = aadt / 24.0 * (length / 5280.0) / max(mph, 1.0) * occ      # the 2.32.0 formula
        assert abs(LN.motorists(aadt, length, mph, occ) - want) < 1e-12
        got = SX._persons({"aadt": aadt, "length_ft": length, "paved": "Yes"}, SX.CLASS_HIGH,
                          {"veh": True, "speed_mph": mph, "occupancy": occ})
        assert (not got and not want) or abs(got - want) < 1e-12, (got, want)
