"""3.27.2 / nd_hazard 1.3.0: US and State highways are at least Medium (REG_05.2026 5.2.1.2(b)).

5.2.1.2 (p. 23): "a. Private roads and roads with AADT < 500 vehicles: ... low hazard ...
b. U.S. Highways, State Highways, or roads with AADT 500-2,000 vehicles: ... medium hazard ...
c. Interstates or roads with AADT > 2,000: ... high hazard". The Runner gave an overtopped US /
State highway with a counted AADT < 500 Low (it had no highway flag); now it is at least Medium,
and the AADT < 500 Low rule is left for local, county and private roads."""
import json
import os
import shutil

from nd_breach_runner.core import nd_hazard as nh
from nd_breach_runner.tests.registry import test

US_STATE = ("US 2", "U.S. Highway 85", "US-85", "US HWY 52", "us52", "ND 23", "ND-1804",
            "ND HWY 22", "State Hwy 22", "State Route 5", "North Dakota Highway 200",
            "HIGHWAY 23", "Hwy 2", "U", "S", "trunk", "USHY", "STHY", "S WASHINGTON ST USHY",
            "OLD HIGHWAY 10")
NOT_US_STATE = ("COUNTY HIGHWAY 12", "Co Hwy 12", "CO. HWY 5", "Township Highway 3",
                "39TH ST NW", "2ND AVE 5", "151ST AVE SE", "County Road 8", "I-94",
                "Interstate 29", "", None, "BUS 2", "Highway Ave", "Industrial Rd",
                "residential", "secondary", "M", "C", "I", "Primary", "primary", "CORD",
                "BUSHY RD")


@test
def highway_floor_route_texts():
    for t in US_STATE:
        assert nh.us_state_highway_from_text(t), t
    for t in NOT_US_STATE:
        assert not nh.us_state_highway_from_text(t), t
    # 'primary' is a US / State highway only as an OSM highway= / fclass value (NG9-1-1
    # RoadClass 'Primary' is not a route designation)
    assert nh.us_state_highway_from_text("primary", osm=True)
    assert nh.us_state_highway_from_text("primary_link", osm=True)
    assert not nh.us_state_highway_from_text("secondary", osm=True)
    # an interstate is its own (High) rule, never a US / State highway
    for t in ("I-94", "Interstate 94", "I"):
        assert nh.interstate_from_text(t) and not nh.us_state_highway_from_text(t), t


@test
def highway_floor_road_class_5_2_1_2():
    st = nh.Settings()
    R = nh.Road
    cases = [  # (road, class, words in the reason)
        (R("ND 23", 1.0, aadt=300, us_state=True), nh.MEDIUM, "5.2.1.2(b)"),
        (R("US 2", 1.0, aadt=10, us_state=True), nh.MEDIUM, "US / State highway"),
        (R("ND 23", 1.0, aadt=1500, us_state=True), nh.MEDIUM, "5.2.1.2(b)"),
        (R("ND 23", 1.0, aadt=8501, us_state=True), nh.HIGH, "> 2,000"),
        (R("I-94", 1.0, aadt=100, interstate=True, us_state=False), nh.HIGH, "interstate"),
        (R("local", 1.0, aadt=300), nh.LOW, "< 500"),               # 5.2.1.2(a) unchanged
        (R("private", 1.0, private=True), nh.LOW, "private"),
        (R("county", 1.0, aadt=499), nh.LOW, "< 500"),
        (R("county", 1.0, aadt=500), nh.MEDIUM, "500-2,000"),
        (R("county", 1.0, aadt=300, sole_access=True), nh.MEDIUM, "sole access"),
    ]
    for r, want, words in cases:
        lvl, why, gap = nh.road_class(r, st)
        assert lvl == want and words in why, (r, lvl, why)
        assert not gap, (r, gap)
    # a highway without AADT: at least Medium whatever the assumption, a data gap (verify)
    for unknown, want in ((nh.LOW, nh.MEDIUM), (nh.MEDIUM, nh.MEDIUM), (nh.HIGH, nh.HIGH)):
        lvl, why, gap = nh.road_class(R("ND 23", 1.0, us_state=True),
                                      nh.Settings(unknown_aadt=unknown))
        assert lvl == want and "5.2.1.2(b)" in why and "AADT not" not in gap, (unknown, lvl)
        assert "High if AADT > 2,000" in gap, gap
    # a local road without AADT keeps the assumption (Low allowed by setting)
    assert nh.road_class(R("x", 1.0), nh.Settings(unknown_aadt=nh.LOW))[0] == nh.LOW


@test
def highway_floor_classify_before_after():
    """The dam whose only receptor is a State highway with AADT 300: Low before, Medium now."""
    old = nh.classify(roads=[nh.Road("ND 23", 0.8, aadt=300)])          # no highway flag
    new = nh.classify(roads=[nh.Road("ND 23", 0.8, aadt=300, us_state=True)])
    assert old.hazard_class == nh.LOW and new.hazard_class == nh.MEDIUM, (old.hazard_class,
                                                                          new.hazard_class)
    assert any("5.2.1.2(b)" in r for r in new.reasons()), new.reasons()
    assert not new.data_gaps and not new.borderline_raised
    # dry (below wet_ft) is still not impacted
    dry = nh.classify(roads=[nh.Road("ND 23", 0.05, aadt=300, us_state=True)])
    assert dry.hazard_class == nh.LOW
    # the Road dataclass keeps its positional order (us_state is the last field)
    r = nh.Road("a", 1.0, 300, False, False, False, 1.0, 2.0)
    assert (r.x, r.y, r.us_state) == (1.0, 2.0, False)


@test
def highway_floor_runner_tool_reads_class_field_and_name():
    from nd_breach_runner.tests.test_ndhazard_tool import _Harness, _load_tool, _run
    an, fq, _ = _load_tool()
    for c, n in (("S", ""), ("U", ""), ("", "HIGHWAY 23"), ("", "ND 1804"), ("", "US-2")):
        assert an.is_us_state(c, n), (c, n)
    for c, n in (("C", "COUNTY HIGHWAY 12"), ("M", "39TH ST NW"), ("I", "I-94"), ("", ""),
                 ("Primary", "107M AVE SE"), ("CORD", "100TH AVE NE")):
        assert not an.is_us_state(c, n), (c, n)
    assert an.is_us_state("primary", "", "highway") and an.is_us_state("primary", "", "fclass")
    assert not an.is_us_state("primary", "", "RoadClass")
    assert "full_name" in an.ROAD_SYNONYMS["R_ID"] and "shield" in an.ROAD_SYNONYMS["R_CLASS"]
    h = _Harness()
    try:
        i1d = nh.LETHALITY_METHODS.index(nh.LETHALITY_1D)
        line = lambda pts, **kw: fq.FakeFeature(fq.FakeGeometry("line", [pts]), **kw)
        roads = fq.FakeSource([
            line([(-50, 55), (200, 55)], NAME="HIGHWAY 23", AADT=300.0, RTTYP="M", private="")],
            source_name="roads")
        res, ctx, fb = _run(an, fq, {"DAM_FOLDER": h.folder, "ROADS": roads, "LETHALITY": i1d})
        js = json.load(open(res["JSON"]))
        f = [x for x in js["findings"] if x["kind"] == "road"][0]
        assert res["CLASS"] == "Medium" and f["level"] == "Medium", (res["CLASS"], f)
        assert "5.2.1.2(b)" in f["reason"], f
        # the same road as a local road: Low (5.2.1.2(a) unchanged)
        roads = fq.FakeSource([
            line([(-50, 55), (200, 55)], NAME="30TH ST SE", AADT=300.0, RTTYP="M", private="")],
            source_name="roads")
        res, ctx, fb = _run(an, fq, {"DAM_FOLDER": h.folder, "ROADS": roads, "LETHALITY": i1d})
        assert res["CLASS"] == "Low", res["CLASS"]
        # the suite's ND NG9-1-1 roads: FULL_NAME and SHIELD (no NAME / class field); US 83
        # through a town is 'S WASHINGTON ST' with SHIELD USHY
        ng = lambda name, shield, aadt: fq.FakeSource([line(
            [(-50, 55), (200, 55)], OBJECTID=7, FULL_NAME=name, SHIELD=shield, RoadClass="Primary",
            AADT=aadt)], source_name="ND_roads_NG911")
        for name, shield, aadt, want in (("S WASHINGTON ST", "USHY", 300.0, "Medium"),
                                         ("HIGHWAY 23", "STHY", 300.0, "Medium"),
                                         ("INTERSTATE 94", "I", 300.0, "High"),
                                         ("100TH AVE NE", "CORD", 300.0, "Low"),
                                         ("107M AVE SE", "", 300.0, "Low")):
            res, ctx, fb = _run(an, fq, {"DAM_FOLDER": h.folder, "ROADS": ng(name, shield, aadt),
                                         "LETHALITY": i1d})
            js = json.load(open(res["JSON"]))
            assert js["fields"]["roads"]["R_ID"] == "FULL_NAME", js["fields"]["roads"]
            assert js["fields"]["roads"]["R_CLASS"] == "SHIELD", js["fields"]["roads"]
            assert res["CLASS"] == want, (name, shield, res["CLASS"])
            assert [f["id"] for f in js["findings"] if f["kind"] == "road"] == [name]
    finally:
        h.close()


