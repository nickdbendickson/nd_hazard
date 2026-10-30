"""The 'ndhazardclass' Processing tool (ND 5.2 / ACER TM 11) through the fake QGIS, and its
place in the Word submittal report.

The charts used here are the invented TEST SHAPES of test_nd_hazard.test_charts(), written to a
temporary chart file: they exercise the zone logic only, they are not the ACER TM 11 figures."""
import csv
import json
import os
import re
import shutil
import tempfile
import xml.dom.minidom

import numpy as np

from nd_breach_runner.tests.registry import test

# ---------------------------------------------------------------------------------------
# a synthetic flood on 8 x 8 cells of 10 units, top-left corner at (0, 80):
#   cell centres x = 5 + 10 c, y = 75 - 10 r
# row 0 (y 75): structures A (c0: 4 ft, 1 ft/s: judgment on the TEST house chart),
#               B (c1: 1 ft, 1 ft/s: low danger), C (c2: dry), barn (c4: 3 ft, occupancy
#               'Shed'), blank occupancy (c5: 0.5 ft), habitable 'no' (c6: 5 ft, 5 ft/s)
# row 1 (y 65): polygon P over c0-c1 (1 ft at 9 ft/s, 2 ft at 0.5 ft/s); polygon Q, smaller
#               than a cell, in c4 (1.5 ft, 1 ft/s); Farm Rd crosses c3 (0.7 ft)
# row 2 (y 55): Hwy 1 (AADT 2,400), wet at c2 (0.5 ft)
# row 3 (y 45): I- 94 (TIGER RTTYP 'I'), wet at c3 (0.2 ft)
# row 4 (y 35): County 5 (no AADT), wet at c1 (0.3 ft); Dry Rd over c5-c7
# row 5 (y 25): BNSF (12 trains a day), wet at c6 (0.4 ft)
# row 6 (y 15): Spur (passenger code 'A'), wet at c2 (0.3 ft); Short line (3 trains) wet at
#               c5 (0.25 ft)
# row 7 (y 5):  substation point c0 (0.6 ft); pump house polygon c2-c3 (0.05 ft: not wet);
#               downstream dams: ND00123 itself at c5 (3 ft), D3 at c6 (dry), D2 at c7 (1.2 ft)
# ---------------------------------------------------------------------------------------
DAM = "ND00123"


def _arrays():
    d = np.zeros((8, 8))
    v = np.full((8, 8), 1.0)
    d[0, 0], d[0, 1], d[0, 4], d[0, 5], d[0, 6] = 4.0, 1.0, 3.0, 0.5, 5.0
    v[0, 5], v[0, 6] = 0.5, 5.0
    d[1, 0], v[1, 0], d[1, 1], v[1, 1] = 1.0, 9.0, 2.0, 0.5
    d[1, 3], d[1, 4] = 0.7, 1.5
    d[2, 2], d[3, 3], d[4, 1], d[5, 6] = 0.5, 0.2, 0.3, 0.4
    d[6, 2], d[6, 5] = 0.3, 0.25
    d[7, 0], d[7, 3], d[7, 5], d[7, 7] = 0.6, 0.05, 3.0, 1.2
    d[3, 0] = np.nan                                   # no-data counts as dry
    return d, v


def _grid(data=None):
    from nd_breach_runner.core.grid import Grid
    return Grid(np.zeros((8, 8)) if data is None else data, 0.0, 80.0, 10.0,
                crs_wkt="EPSG:26914")


def _load_tool():
    import importlib
    from nd_breach_runner.tests import fake_qgis as fq
    fq.install()
    import nd_breach_runner.algorithms as algs
    importlib.reload(algs)
    import nd_breach_runner.algorithms_tools as tools
    importlib.reload(tools)
    import nd_breach_runner.algorithms_consequences as ac
    importlib.reload(ac)
    import nd_breach_runner.algorithms_ndhazard as an
    importlib.reload(an)
    return an, fq, algs


class _Harness:
    """A dam folder whose rasters are served by a patched gdal_io (as test_consequences)."""

    def __init__(self, velocity=True):
        from nd_breach_runner.core import gdal_io, workflow
        self.root = tempfile.mkdtemp()
        self.folder = os.path.join(self.root, DAM)
        os.makedirs(self.folder)
        d, v = _arrays()
        self.arrays = {}
        for kind, a in (("depth", d), ("velocity", v)):
            if kind == "velocity" and not velocity:
                continue
            p = os.path.join(self.folder, workflow.FINAL_NAMES[kind])
            open(p, "w").write("x")
            self.arrays[p] = a
        self.gdal_io = gdal_io
        self.real = (gdal_io.read_grid, gdal_io.read_like, gdal_io.epsg_of)
        gdal_io.read_grid = lambda p: _grid(self.arrays[p])
        gdal_io.read_like = lambda p, g, alg="bilinear": self.arrays[p]
        gdal_io.epsg_of = lambda wkt: "EPSG:26914"

    def chart_file(self):
        from nd_breach_runner.core import nd_hazard as nh
        from nd_breach_runner.tests.test_nd_hazard import test_charts
        return nh.save_charts(os.path.join(self.root, "test_charts.json"), test_charts())

    def close(self):
        self.gdal_io.read_grid, self.gdal_io.read_like, self.gdal_io.epsg_of = self.real
        shutil.rmtree(self.root, ignore_errors=True)


def _layers(fq):
    def pt(x, y, **at):
        return fq.FakeFeature(fq.FakeGeometry("point", [[(x, y)]]), **at)

    def line(pts, **at):
        return fq.FakeFeature(fq.FakeGeometry("line", [pts]), **at)

    def poly(ring, **at):
        return fq.FakeFeature(fq.FakeGeometry("polygon", [ring]), **at)
    structures = fq.FakeSource([
        pt(5, 75, fd_id="A", occtype="RES1-1SNB"),
        pt(15, 75, fd_id="B", occtype="COM1"),
        pt(25, 75, fd_id="C", occtype="RES1"),
        pt(45, 75, fd_id="barn", occtype="Shed"),
        pt(55, 75, fd_id="blank", occtype=""),
        pt(65, 75, fd_id="nohab", occtype="RES1", habitable="no"),
        pt(90, 90, fd_id="edge", occtype="RES1"),
        pt(5000, 5000, fd_id="far", occtype="RES1")], source_name="NSI structures")
    roads = fq.FakeSource([
        line([(-50, 55), (200, 55)], NAME="Hwy 1", AADT=2400.0, RTTYP="S", private=""),
        line([(0, 45), (80, 45)], NAME="I- 94", AADT=None, RTTYP="I", private=""),
        line([(0, 35), (40, 35)], NAME="County 5", AADT=None, RTTYP="C", private=""),
        line([(50, 35), (80, 35)], NAME="Dry Rd", AADT=50.0, RTTYP="M", private=""),
        line([(35, 50), (35, 80)], NAME="Farm Rd", AADT=None, RTTYP="M", private="Y")],
        source_name="roads")
    rail = fq.FakeSource([
        line([(0, 25), (80, 25)], RROWNER1="BNSF", trains_per_day=12.0, PASSNGR=""),
        line([(0, 15), (35, 15)], RROWNER1="Spur", trains_per_day=None, PASSNGR="A"),
        line([(40, 15), (80, 15)], RROWNER1="Short line", trains_per_day=3.0, PASSNGR="")],
        source_name="FRA rail")
    lifelines = fq.FakeSource([
        pt(5, 5, name="Sub 1", type="substation"),
        poly([(20, 0), (40, 0), (40, 10), (20, 10)], name="Pump", type="water")],
        source_name="lifelines")
    dams = fq.FakeSource([
        pt(55, 5, NIDID=DAM, HAZARD="H"), pt(65, 5, NIDID="D3", HAZARD="H"),
        pt(75, 5, NIDID="D2", HAZARD="S"), pt(9000, 9000, NIDID="D9", HAZARD="H")],
        source_name="ND dams")
    return structures, roads, rail, lifelines, dams


def _run(an, fq, params):
    ctx, fb = fq.QgsProcessingContext(), fq.QgsProcessingFeedback()
    a = an.NDHazardAlgorithm()
    a.initAlgorithm()
    return a.processAlgorithm(params, ctx, fb), ctx, fb


def _find(js, kind, ident):
    got = [f for f in js["findings"] if f["kind"] == kind and f["id"] == ident]
    assert len(got) == 1, (kind, ident, got)
    return got[0]


# ---------------------------------------------------------------------------------------
@test
def ndhazard_tool_helpers_habitable_mobile_roads_rail():
    an, fq, _ = _load_tool()
    pats = an.nh.UNINHABITED_KEYWORDS
    for occ in ("RES1-1SNB", "RES2", "COM4", "AGR1", "Residential", "Agriculture",
                "Single Family Dwelling", "Manufactured Home", "Campground", "EDU1", "Heavy",
                "Temporary Lodging", "Duplex", "Utility and Misc", "Storage"):
        assert an.habitable_from(None, occ, pats) == (True, "class"), occ
    for occ in ("Shed", "Garage", "Barn", "Grain bin", "Oil well pad"):
        assert an.habitable_from(None, occ, pats) == (False, "class"), occ
    for occ in ("", "Unclassified", "unknown", None):
        assert an.habitable_from(None, occ, pats) == (True, "unknown"), occ
    assert an.habitable_from("no", "RES1", pats) == (False, "field")
    assert an.habitable_from(True, "Shed", pats) == (True, "field")
    assert an.habitable_from(0, "RES1", pats) == (False, "field")
    assert an.habitable_from("Uninhabited", "RES1", pats) == (False, "field")
    assert an.habitable_from("Vacant", "RES1", pats) == (True, "class")   # assumed occupied
    assert an.habitable_from("", "Shed", pats) == (False, "class")      # blank flag: the class
    assert an.habitable_from(None, "Silo", ("silo",)) == (False, "class")  # the list is editable
    assert an.is_mobile(None, "RES2") and an.is_mobile("Manufactured Home", "Residential")
    assert an.is_mobile("yes", "") and an.is_mobile(None, "mobile home park")
    assert not an.is_mobile("Single Family Dwelling", "RES1") and not an.is_mobile("no", "RES1")
    assert not an.is_mobile(None, "RES21")
    for c, n in (("Interstate", ""), ("I", ""), ("", "I-94"), ("", "I 29"), ("", "I- 94"),
                 ("", "Interstate 94 W")):
        assert an.is_interstate(c, n), (c, n)
    for c, n in (("US", "US-2"), ("", "Industrial Rd"), ("", "I-Street"), ("S", "ND 1804")):
        assert not an.is_interstate(c, n), (c, n)
    assert an.is_private("Y") and an.is_private("Private") and not an.is_private("Public")
    codes = {"A", "B", "C"}
    assert an.is_passenger("A", codes) and an.is_passenger("yes", codes)
    assert an.is_passenger("true", codes) and an.is_passenger("X, C", codes)
    assert not an.is_passenger("", codes) and not an.is_passenger("F", codes)
    assert not an.is_passenger("0", codes)
    assert not an.is_passenger("T", codes) and not an.is_passenger("X", codes)   # tourist, other
    assert not an.is_private("Public") and not an.is_private("yes please")
    assert "access" not in an.ROAD_SYNONYMS["R_PRIVATE"]
    assert "ownership" not in an.ROAD_SYNONYMS["R_PRIVATE"]
    assert an.dam_class("S") == "Medium" and an.dam_class("h") == "High"
    assert an.dam_class("Medium") == "Medium" and an.dam_class("U") == "U"


@test
def ndhazard_tool_sampler_points_polygons_lines():
    an, fq, _ = _load_tool()
    d, v = _arrays()
    s = an.Sampler(_grid(d), d, v, wet_ft=0.1)
    p = s.points([(5, 75)])
    assert p["depth_ft"] == 4.0 and p["vel_fts"] == 1.0 and (p["x"], p["y"]) == (5.0, 75.0)
    assert s.points([(-1, 75)]) is None and s.points([(5, 81)]) is None
    assert s.points([(5, 45)])["depth_ft"] == 0.0                         # NaN -> dry
    # polygon P: deepest cell c1 (2 ft at 0.5 ft/s); velocity is the footprint's maximum over
    # wet cells (9 ft/s at c0), the conservative choice
    P = s.polygon([np.array([(0, 60), (20, 60), (20, 70), (0, 70)], float)], (10, 65))
    assert P["depth_ft"] == 2.0 and P["vel_fts"] == 9.0 and P["vel_at_max_fts"] == 0.5
    assert (P["x"], P["y"]) == (15.0, 65.0) and P["cells"] == 2
    # a footprint smaller than a cell takes the cell under its centroid
    Q = s.polygon([np.array([(41, 61), (44, 61), (44, 64), (41, 64)], float)], (42.5, 62.5))
    assert Q["depth_ft"] == 1.5 and "centroid" in Q["how"]
    assert s.polygon([np.array([(900, 900), (910, 900), (910, 910)], float)], (905, 905)) is None
    # an L-shaped footprint leaves out the cell in its notch (centre (15, 75))
    L = s.polygon([np.array([(0, 60), (20, 60), (20, 70), (10, 70), (10, 80), (0, 80)],
                            float)], (8, 70))
    assert L["cells"] == 3 and L["depth_ft"] == 4.0 and (L["x"], L["y"]) == (5.0, 75.0)
    assert L["vel_fts"] == 9.0
    # wet footprint cells only for the maximum velocity: a dry fast cell does not count
    d2, v2 = d.copy(), v.copy()
    v2[1, 2] = 30.0
    s2 = an.Sampler(_grid(d2), d2, v2)
    P2 = s2.polygon([np.array([(0, 60), (30, 60), (30, 70), (0, 70)], float)], (15, 65))
    assert P2["vel_fts"] == 9.0 and P2["cells"] == 3
    # lines: sampled every half cell, the deepest sample, partly outside the grid
    ln = s.lines([np.array([(-50, 55), (200, 55)], float)])
    assert ln["depth_ft"] == 0.5 and 20 <= ln["x"] <= 30 and ln["y"] == 55
    assert s.lines([np.array([(0, 35.0), (3.0, 35.0)])])["depth_ft"] == 0.0
    assert s.lines([np.array([(500, 500), (600, 600)], float)]) is None
    # a line that only clips the corner of a cell still finds it (half-cell sampling)
    assert s.lines([np.array([(16, 72), (22, 66)], float)])["depth_ft"] == 2.0
    # no velocity raster: zeros
    assert an.Sampler(_grid(d), d, None).points([(5, 75)])["vel_fts"] == 0.0


@test
def ndhazard_tool_chart_method_end_to_end():
    an, fq, _ = _load_tool()
    from nd_breach_runner.core import nd_hazard as nh
    h = _Harness()
    try:
        s, r, t, l, dm = _layers(fq)
        cf = h.chart_file()
        res, ctx, fb = _run(an, fq, {"DAM_FOLDER": h.folder, "STRUCTURES": s, "ROADS": r,
                                     "RAIL": t, "LIFELINES": l, "DOWNSTREAM_DAMS": dm,
                                     "CHART_FILE": cf})
        assert res["CLASS"] == nh.HIGH
        for k in ("REPORT", "JSON", "CSV", "GEOJSON"):
            assert os.path.exists(res[k]) and os.path.dirname(res[k]) == h.folder, k
        js = json.load(open(res["JSON"]))
        assert js["hazard_class"] == "High" and js["dam_id"] == DAM
        assert js["inputs"] == {"structures": "NSI structures", "roads": "roads",
                                "railroads": "FRA rail", "lifelines": "lifelines",
                                "downstream_dams": "ND dams"}
        assert js["fields"]["structures"]["S_OCC"] == "occtype"
        assert js["fields"]["railroads"]["T_PASSENGER"] == "PASSNGR"
        assert js["fields"]["roads"]["R_CLASS"] == "RTTYP"
        assert js["charts"]["status"] == "digitized" and js["charts"]["file"] == cf
        assert js["settings"]["lethality"] == nh.LETHALITY_CHARTS
        # structures
        a = _find(js, "structure", "A")
        # judgment zone: MEDIUM by rule (Nick 10/30/2026), not 5.3 borderline HIGH
        assert a["level"] == "Medium" and a["zone"] == nh.ZONE_JUDGMENT and not a["borderline"]
        assert "5.3" not in a["section"] and "MEDIUM by rule" in a["reason"]
        assert (a["x"], a["y"]) == (5.0, 75.0)
        assert js["settings"]["judgment_zone_high"] is False
        b = _find(js, "structure", "B")
        assert b["level"] == "Medium" and b["zone"] == nh.ZONE_LOW
        assert _find(js, "structure", "barn")["level"] == "Low"             # 'Shed'
        assert _find(js, "structure", "blank")["level"] == "Medium"         # unknown: habitable
        nohab = _find(js, "structure", "nohab")
        assert nohab["level"] == "Low" and "uninhabited" in nohab["reason"]
        ids = {f["id"] for f in js["findings"] if f["kind"] == "structure"}
        assert not ids & {"C", "far", "edge"}                                # dry; outside
        assert js["counts"]["structures_wet"] == 5 and js["counts"]["habitable_wet"] == 3
        assert js["counts"]["judgment_zone"] == 1
        # roads, railroads, lifelines, downstream dams
        lv = lambda k, i: _find(js, k, i)["level"]
        assert lv("road", "Hwy 1") == "High" and lv("road", "I- 94") == "High"
        assert "interstate" in _find(js, "road", "I- 94")["reason"]
        assert lv("road", "County 5") == "Medium" and lv("road", "Farm Rd") == "Low"
        assert _find(js, "road", "Hwy 1")["depth_ft"] == 0.5
        assert not [f for f in js["findings"] if f["id"] == "Dry Rd"]
        assert lv("railroad", "BNSF") == "High" and lv("railroad", "Spur") == "High"
        assert "passenger" in _find(js, "railroad", "Spur")["reason"]
        assert lv("railroad", "Short line") == "Medium"
        assert lv("lifeline", "Sub 1") == "Medium" and "substation" in \
            _find(js, "lifeline", "Sub 1")["reason"]
        assert not [f for f in js["findings"] if f["id"] == "Pump"]         # 0.05 ft: not wet
        d2 = _find(js, "dam", "D2")
        assert d2["level"] == "Medium" and "1.2 ft" in d2["reason"]
        assert (d2["x"], d2["y"]) == (75.0, 5.0)
        assert not [f for f in js["findings"] if f["kind"] == "dam" and f["id"] != "D2"]
        assert any("County 5" in g and "AADT" in g for g in js["data_gaps"])
        assert any("railroad" in g for g in js["data_gaps"]) is False       # all counted
        gov = {(f["kind"], f["id"]) for f in js["governing"]}
        assert ("structure", "A") not in gov and ("road", "Hwy 1") in gov   # A is Medium by rule
        assert not js["borderline_raised"]                    # Hwy 1 etc. are not borderline
        # CSV and GeoJSON
        rows = list(csv.DictReader(open(res["CSV"], newline="")))
        assert list(rows[0].keys()) == nh.FINDING_COLUMNS and len(rows) == len(js["findings"])
        gj = json.load(open(res["GEOJSON"]))
        assert gj["type"] == "FeatureCollection" and gj["crs"]["properties"]["name"] == \
            "EPSG:26914"
        assert len(gj["features"]) == len(js["findings"])                  # all have a place
        fa = next(f for f in gj["features"] if f["properties"]["id"] == "A")
        assert fa["geometry"] == {"type": "Point", "coordinates": [5.0, 75.0]}
        assert fa["properties"]["habitable_from"] == "class" and "x" not in fa["properties"]
        assert fa["properties"]["occupancy"] == "RES1-1SNB"
        assert any(p.endswith(".geojson") for p in ctx.loaded)
        # markdown
        md = open(res["REPORT"]).read()
        assert md.startswith("# Hazard classification (ND 5.2): " + DAM)
        assert "**Class: HIGH**" in md and "## Data gaps" in md and "## Inputs" in md
        assert "NSI structures" in md and "Department of Water Resources" in md
        # the chart figures: houses A, B, blank; no mobile home in this layer
        svgs = {k: os.path.join(h.folder, an.OUTPUT_NAMES[k]) for k in ("svg_house",
                                                                          "svg_mobile")}
        for k, n in (("svg_house", 3), ("svg_mobile", 0)):
            txt = open(svgs[k]).read()
            xml.dom.minidom.parseString(txt)
            assert txt.count("<circle") == n, (k, txt.count("<circle"))
        assert "TEST house chart" in open(svgs["svg_house"]).read()
        assert js["figures"] == [an.OUTPUT_NAMES["svg_house"], an.OUTPUT_NAMES["svg_mobile"]]
        # the log
        info = "\n".join(fb.info)
        assert "Structures fields: id = fd_id" in info and "occ = occtype" in info
        assert "Railroads fields:" in info and "passenger = PASSNGR" in info
        assert "Hazard class: HIGH" in info and "Governing: " in info
        assert "ND00123 is this dam; left out" in info
        assert "Structures: 1 feature(s) outside the rasters" in info
        assert "Downstream dams: 2 inside the rasters, 1 reached by the flood" in info
        assert any(e.startswith("Data gap: road County 5") for e in fb.errors)
        assert "Note: No allowance for evacuation" in info
    finally:
        h.close()


@test
def ndhazard_tool_polygon_footprints_usa_structures():
    an, fq, _ = _load_tool()
    from nd_breach_runner.core import nd_hazard as nh
    h = _Harness()
    try:
        poly = lambda ring, **at: fq.FakeFeature(fq.FakeGeometry("polygon", [ring]), **at)
        src = fq.FakeSource([
            poly([(0, 60), (20, 60), (20, 70), (0, 70)], BUILD_ID="P", OCC_CLS="Residential",
                 PRIM_OCC="Single Family Dwelling"),
            poly([(41, 61), (44, 61), (44, 64), (41, 64)], BUILD_ID="Q", OCC_CLS="Residential",
                 PRIM_OCC="Manufactured Home"),
            poly([(20, 70), (30, 70), (30, 80), (20, 80)], BUILD_ID="dry",
                 OCC_CLS="Unclassified", PRIM_OCC="")])
        cf = h.chart_file()
        res, ctx, fb = _run(an, fq, {"DAM_FOLDER": h.folder, "STRUCTURES": src,
                                     "CHART_FILE": cf})
        js = json.load(open(res["JSON"]))
        assert js["fields"]["structures"] == {"S_ID": "BUILD_ID", "S_HABITABLE": "",
                                              "S_OCC": "OCC_CLS", "S_MOBILE": "PRIM_OCC"}
        # P: deepest cell 2 ft; at that cell's 0.5 ft/s it is low danger (Medium), with the
        # footprint's maximum velocity (9 ft/s) it is in the judgment zone -> Medium by rule
        p = _find(js, "structure", "P")
        assert p["level"] == "Medium" and p["zone"] == nh.ZONE_JUDGMENT
        assert p["depth_ft"] == 2.0 and p["vel_fts"] == 9.0 and (p["x"], p["y"]) == (15.0, 65.0)
        # Q is a mobile home: 1.5 ft at 1 ft/s is judgment on the mobile chart
        q = _find(js, "structure", "Q")
        assert q["level"] == "Medium" and "Fig. 5.2" in q["section"] and "mobile" in q["reason"]
        # the old option: the judgment zone counts as potentially lethal -> High
        res, ctx, fb = _run(an, fq, {"DAM_FOLDER": h.folder, "STRUCTURES": src,
                                     "CHART_FILE": cf, "JUDGMENT_HIGH": True})
        js = json.load(open(res["JSON"]))
        assert js["settings"]["judgment_zone_high"] is True
        assert _find(js, "structure", "P")["level"] == "High"
        assert _find(js, "structure", "Q")["level"] == "High"
        gj = json.load(open(res["GEOJSON"]))
        gp = next(f for f in gj["features"] if f["properties"]["id"] == "P")
        assert gp["properties"]["vel_at_max_depth_fts"] == 0.5
        assert gp["properties"]["sampled"].startswith("polygon: deepest of 2 cell(s)")
        gq = next(f for f in gj["features"] if f["properties"]["id"] == "Q")
        assert gq["properties"]["mobile"] is True and "centroid" in gq["properties"]["sampled"]
        mob = open(os.path.join(h.folder, an.OUTPUT_NAMES["svg_mobile"])).read()
        house = open(os.path.join(h.folder, an.OUTPUT_NAMES["svg_house"])).read()
        assert mob.count("<circle") == 1 and house.count("<circle") == 1
        assert "unknown (taken as habitable) 1" in "\n".join(fb.info)
        assert res["CLASS"] == "High"
    finally:
        h.close()


@test
def ndhazard_tool_not_digitized_1d_rule_and_conservative():
    an, fq, _ = _load_tool()
    from nd_breach_runner.core import nd_hazard as nh
    h = _Harness()
    try:
        s, r, t, l, dm = _layers(fq)
        base = {"DAM_FOLDER": h.folder, "STRUCTURES": s}
        shipped = nh.load_charts()
        if not shipped.ready:
            # the chart file shipped with the plugin is not digitized: a clear message
            try:
                _run(an, fq, dict(base))
                raise AssertionError("expected a QgsProcessingException")
            except fq.QgsProcessingException as e:
                msg = str(e)
                assert "not digitized" in msg and "chart method cannot run" in msg
                assert "only allowed for a 1D model" in msg
                assert nh.LETHALITY_LABELS[nh.LETHALITY_ALL_WET] in msg
            assert not os.path.exists(os.path.join(h.folder, an.OUTPUT_NAMES["json"]))
        # a chart file that cannot be read
        bad = os.path.join(h.root, "bad.json")
        open(bad, "w").write("{not json")
        try:
            _run(an, fq, dict(base, CHART_FILE=bad))
            raise AssertionError("expected a QgsProcessingException")
        except fq.QgsProcessingException as e:
            assert "Could not read the chart file" in str(e)
        # a chart run first leaves figures; a later 1D run removes them (stale)
        _run(an, fq, dict(base, CHART_FILE=h.chart_file()))
        svg = os.path.join(h.folder, an.OUTPUT_NAMES["svg_house"])
        assert os.path.exists(svg)
        i1d = nh.LETHALITY_METHODS.index(nh.LETHALITY_1D)
        res, ctx, fb = _run(an, fq, dict(base, LETHALITY=i1d, ROADS=r, RAIL=t, LIFELINES=l,
                                         DOWNSTREAM_DAMS=dm, UNKNOWN_AADT=2))
        assert not os.path.exists(svg)
        js = json.load(open(res["JSON"]))
        assert js["settings"]["lethality"] == nh.LETHALITY_1D and js["charts"] == {}
        assert js["figures"] == []
        a = _find(js, "structure", "A")                 # 4 ft > 2 ft
        assert a["level"] == "High" and "1D rule" in a["reason"]
        assert _find(js, "structure", "B")["level"] == "Medium"             # 1 ft
        assert _find(js, "road", "County 5")["level"] == "High"             # unknown AADT: High
        assert res["CLASS"] == "High"
        # 1D rule with only the shallow structure: Medium, with the Section 1.5 note
        one = fq.FakeSource([fq.FakeFeature(fq.FakeGeometry("point", [[(15, 75)]]), fd_id="B")])
        res, ctx, fb = _run(an, fq, {"DAM_FOLDER": h.folder, "STRUCTURES": one,
                                     "LETHALITY": i1d})
        assert res["CLASS"] == "Medium"
        assert any("Section 1.5" in m for m in fb.info)
        # conservative: every inundated habitable structure counts; borderline raised
        iall = nh.LETHALITY_METHODS.index(nh.LETHALITY_ALL_WET)
        res, ctx, fb = _run(an, fq, {"DAM_FOLDER": h.folder, "STRUCTURES": one,
                                     "LETHALITY": iall})
        js = json.load(open(res["JSON"]))
        assert res["CLASS"] == "High" and js["borderline_raised"]
        assert any("over-classifies" in n for n in js["notes"])
        assert any("borderline" in e for e in fb.errors)
        # no receptor layer at all: Low, and said
        res, ctx, fb = _run(an, fq, {"DAM_FOLDER": h.folder, "LETHALITY": i1d})
        assert res["CLASS"] == "Low" and any("no receptor layer" in e for e in fb.errors)
        # a wet depth of 1 ft: B (1 ft) is still wet, County 5 (0.3 ft) is not overtopped
        res, ctx, fb = _run(an, fq, dict(base, LETHALITY=i1d, ROADS=r, WET_FT=1.0))
        js = json.load(open(res["JSON"]))
        assert _find(js, "structure", "B")["level"] == "Medium"
        assert not [f for f in js["findings"] if f["kind"] == "road"]
        # errors are Processing exceptions
        for bad_p in ({"DAM_FOLDER": os.path.join(h.root, "nowhere"), "LETHALITY": i1d},
                      dict(base, LETHALITY=9)):
            try:
                _run(an, fq, bad_p)
                raise AssertionError(bad_p)
            except fq.QgsProcessingException:
                pass
    finally:
        h.close()
    # the chart method needs the velocity raster
    h = _Harness(velocity=False)
    try:
        s = _layers(fq)[0]
        try:
            _run(an, fq, {"DAM_FOLDER": h.folder, "STRUCTURES": s, "CHART_FILE": h.chart_file()})
            raise AssertionError("expected a QgsProcessingException")
        except fq.QgsProcessingException as e:
            assert "Velocity (Max).tif" in str(e)
        res, ctx, fb = _run(an, fq, {"DAM_FOLDER": h.folder, "STRUCTURES": s,
                                     "LETHALITY": nh.LETHALITY_METHODS.index(nh.LETHALITY_1D)})
        assert res["CLASS"] == "High" and any("Velocity (Max).tif" in e for e in fb.errors)
    finally:
        h.close()


@test
def ndhazard_tool_is_defined_registered_and_ids_are_unique():
    an, fq, algs = _load_tool()
    from nd_breach_runner.core import nd_hazard as nh
    from nd_breach_runner.core import report_docx as R
    assert an.NDHAZARD_ALGORITHMS == (an.NDHazardAlgorithm,)
    a = an.NDHazardAlgorithm()
    a.initAlgorithm()
    assert a.name() == "ndhazardclass" and a.createInstance().name() == "ndhazardclass"
    assert a.displayName() == "ND hazard classification (Section 5.2, ACER TM 11 charts)"
    assert a.group() == "Data preparation and tools" and a.groupId() == "tools"
    for k in ("DAM_FOLDER", "STRUCTURES", "S_ID", "S_HABITABLE", "S_OCC", "UNINHABITED_WORDS",
              "S_MOBILE", "ROADS", "R_ID", "R_AADT", "R_CLASS", "R_PRIVATE", "R_SOLE", "RAIL",
              "T_ID", "T_TRAINS", "T_PASSENGER", "PASSENGER_CODES", "LIFELINES", "L_ID",
              "L_KIND", "DOWNSTREAM_DAMS", "D_ID", "D_CLASS", "LETHALITY", "WET_FT",
              "UNKNOWN_AADT", "UNKNOWN_TRAINS", "CHART_FILE"):
        assert a.parameterDefinition(k) is not None, k
    assert set(a.outputs) == {"CLASS", "REPORT", "JSON", "CSV", "GEOJSON"}
    le = a.parameterDefinition("LETHALITY")
    assert le.args[0] == [nh.LETHALITY_LABELS[m] for m in nh.LETHALITY_METHODS]
    assert nh.LETHALITY_METHODS[le.kwargs["defaultValue"]] == nh.LETHALITY_CHARTS
    for k in ("UNKNOWN_AADT", "UNKNOWN_TRAINS"):
        assert nh.CLASSES[a.parameterDefinition(k).kwargs["defaultValue"]] == nh.MEDIUM
    assert a.parameterDefinition("WET_FT").kwargs["defaultValue"] == 0.1
    h = a.shortHelpString()
    assert "READ FIRST" in h and "signed off" in h and "Department of Water Resources" in h
    # registered once, every algorithm id is unique, and every menu item names a real one
    names = [cls().name() for cls in algs.ALGORITHMS]
    assert len(names) == len(set(names)), sorted(n for n in names if names.count(n) > 1)
    assert names.count("ndhazardclass") == 1 and "classify" in names
    menu = open(os.path.join(os.path.dirname(algs.__file__), "plugin.py")).read()
    ids = re.findall(r'"ndbreach:([a-z0-9_]+)"', menu)
    # 3.9.0: plus the provider's shared "Get data for this dam" (not in algorithms.ALGORITHMS)
    assert "ndhazardclass" in ids and not set(ids) - set(names) - {"getdamdata"}, \
        set(ids) - set(names)
    assert ids.index("ndhazardclass") == ids.index("consequences") + 1
    # the report reads the files by the names the tool writes
    assert R.HAZARD_FILES == an.OUTPUT_NAMES


@test
def ndhazard_tool_results_in_the_word_submittal_report():
    an, fq, _ = _load_tool()
    from nd_breach_runner.core import report_docx as R
    h = _Harness()
    try:
        s, r, t, l, dm = _layers(fq)
        _run(an, fq, {"DAM_FOLDER": h.folder, "STRUCTURES": s, "ROADS": r, "RAIL": t,
                      "LIFELINES": l, "DOWNSTREAM_DAMS": dm, "CHART_FILE": h.chart_file()})
        out = os.path.join(h.root, "report.docx")
        res = R.build_submittal(h.folder, DAM, out, {"svg_backends": ["cairosvg"],
                                                     "include_toc": False})
        assert os.path.exists(out)
        t_ = R.inspect_docx(out)["text"]
        titles = [x for _n, x in res.sections]
        assert "Receptors and impacts" in titles, titles
        for needle in ("Hazard classification (ND 5.2)", "Hazard class (screening)", "HIGH",
                       "Governing findings", "road with AADT 2,400 (> 2,000) overtopped",
                       "Data gaps", "road County 5: AADT not given", "Notes",
                       "ND Department of Water Resources",
                       "ND Fig. 5.1 (ACER TM 11 Fig. 2)", "ND Fig. 5.2 (ACER TM 11 Fig. 3)",
                       "ACER TM 11 chart with the houses reached (ND Fig. 5.1)",
                       "Hazard classification findings (points)"):
            assert needle in t_, needle
        figs = {f["name"]: f for f in res.figures}
        assert set(figs) == {an.OUTPUT_NAMES["svg_house"], an.OUTPUT_NAMES["svg_mobile"]}
        assert not any(f["embedded"] for f in figs.values())        # no cairosvg here
        m = re.search(r"Hazard classification screened \(ND 5\.2\)\n(Yes|No|Check|N/A)\n"
                      r"([^\n]*)", t_)
        assert m and m.group(1) == "Yes" and "class HIGH" in m.group(2), m and m.groups()
        assert "1 data gap(s)" in m.group(2)
        m = re.search(r"Hazard classification made by the engineer\n(\w+)\n([^\n]*)", t_)
        assert m.group(1) == "Check" and "screening class HIGH" in m.group(2)
        # switched off with the consequences: no section, the checklist still sees the file
        out2 = os.path.join(h.root, "report2.docx")
        R.build_submittal(h.folder, DAM, out2, {"include_consequences": False,
                                                "include_figures": False})
        t2 = R.inspect_docx(out2)["text"]
        assert "Governing findings" not in t2 and "Hazard class (screening)" not in t2
        assert re.search(r"Hazard classification screened \(ND 5\.2\)\nYes\n", t2)
        # no classification in the folder: the checklist says so
        for n in os.listdir(h.folder):
            if n.startswith(("Hazard Classification", "ND Fig")):
                os.remove(os.path.join(h.folder, n))
        out3 = os.path.join(h.root, "report3.docx")
        R.build_submittal(h.folder, DAM, out3, {"include_figures": False})
        t3 = R.inspect_docx(out3)["text"]
        assert re.search(r"Hazard classification screened \(ND 5\.2\)\nNo\nnot run", t3)
        assert re.search(r"Hazard classification made by the engineer\nCheck\nthis tool "
                         r"gives a screening class only", t3)
    finally:
        h.close()


@test
def ndhazard_tool_results_in_a_full_run_folder_report():
    """A real workflow folder (test_report_docx's) with the classification added: the summary
    gets the class, the sections stay as they were."""
    from nd_breach_runner.core import nd_hazard as nh
    from nd_breach_runner.core import report_docx as R
    from nd_breach_runner.tests import test_report_docx as T
    folder = T._copy_folder(True, extras=True)
    try:
        c = nh.classify([nh.Structure("h1", 1.0, 1.0)], roads=[nh.Road("r", 0.5)],
                        settings=nh.Settings(lethality=nh.LETHALITY_1D))
        d = c.to_dict()
        d.update({"dam_id": "ND00123", "lethality_label": nh.LETHALITY_LABELS[nh.LETHALITY_1D]})
        json.dump(d, open(os.path.join(folder, R.HAZARD_FILES["json"]), "w"))
        open(os.path.join(folder, R.HAZARD_FILES["markdown"]), "w").write(nh.markdown(c))
        out = os.path.join(folder, "s.docx")
        res = R.build_submittal(folder, "", out, {"include_figures": False})
        assert not res.warnings, res.warnings
        titles = [x for _n, x in res.sections]
        assert titles == ["Summary of results", "Dam and reservoir data",
                          "Breach parameters and method", "Flood scenario", "Hydraulic model",
                          "Results", "Receptors and impacts",
                          "Sensitivity, cascade and probabilistic results",
                          "Assumptions and limitations", "Reproducibility", "Appendix"], titles
        t_ = R.inspect_docx(out)["text"]
        assert "Hazard class (ND 5.2, screening)\nMEDIUM" in t_
        assert "habitable structure inundated 1.0 ft: economic loss" in t_
        assert "Consequences documented" in t_ and "Population from 2020 census" in t_
        m = re.search(r"Hazard classification screened \(ND 5\.2\)\nYes\n([^\n]*)", t_)
        assert m and "class MEDIUM" in m.group(1) and "1D model" in m.group(1)
        m = re.search(r"Hazard classification made by the engineer\nCheck\n([^\n]*)", t_)
        assert m and "screening class MEDIUM" in m.group(1), m and m.group(1)
    finally:
        shutil.rmtree(os.path.dirname(os.path.dirname(folder)), ignore_errors=True)
