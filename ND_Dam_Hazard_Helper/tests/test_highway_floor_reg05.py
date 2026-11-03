"""Hazard Helper 2.35.4: US / State highways are at least Medium (REG_05.2026 5.2.1.2(b)).

  * Roads: a US or State highway is at least Medium whatever its AADT (5.2.1.2(b), p. 23); the
    private / AADT < 500 Low rule (5.2.1.2(a)) is for local, county and private roads. A bare
    'Highway 23' / 'Hwy 2' (ND NG9-1-1 names) and 'North Dakota Highway 200' read as State
    highways, not 'County Highway 12', and the criteria table has a 5.2.1.2(b) row.
  * Parity with the nd_breach_runner / nd_dam_screening rules module (core/nd_hazard.py 1.3.0),
    when those plugins are next to this one: the same route texts are US / State highways and
    interstates, and nd_road_class gives the class nd_hazard.road_class gives.

    python3 -m ND_Dam_Hazard_Helper.tests.run_tests highway
"""
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
from ND_Dam_Hazard_Helper.tests.registry import Skip, test  # noqa: E402

FAILS = []
US_STATE = ("US 2", "U.S. Highway 85", "US-85", "US HWY 52", "ND 23", "ND-1804", "ND HWY 22",
            "State Hwy 22", "State Route 5", "North Dakota Highway 200", "HIGHWAY 23", "Hwy 2",
            "OLD HIGHWAY 10")
LOCAL = ("COUNTY HIGHWAY 12", "Co Hwy 12", "CO. HWY 5", "Township Highway 3", "39TH ST NW",
         "2ND AVE 5", "151ST AVE SE", "County Road 8", "BUS 2", "Highway Ave", "Industrial Rd")
INTERSTATE = ("I-94", "Interstate 29", "I 94")


def check(what, ok, got=""):
    print("{:4} {}{}".format("ok" if ok else "FAIL", what, "" if ok else "  -> {!r}".format(got)))
    if not ok:
        FAILS.append(what)


def _nd_hazard():
    for p in ("nd_breach_runner", "nd_dam_screening"):
        f = os.path.join(ROOT, p, "core", "nd_hazard.py")
        if os.path.isfile(f):
            spec = importlib.util.spec_from_file_location("nd_hazard_parity", f)
            m = importlib.util.module_from_spec(spec)
            sys.modules["nd_hazard_parity"] = m
            spec.loader.exec_module(m)
            return m
    return None


def road_classes(texts):
    from qgis.core import QgsFeature, QgsField, QgsVectorLayer
    from ND_Dam_Hazard_Helper.compat import FT_STRING
    from ND_Dam_Hazard_Helper import linear as LN
    lyr = QgsVectorLayer("LineString?crs=EPSG:5070", "roads", "memory")
    lyr.dataProvider().addAttributes([QgsField("FULLNAME", FT_STRING)])
    lyr.updateFields()
    rd = LN.AttrReader(lyr)
    out = {}
    for t in texts:
        f = QgsFeature(lyr.fields())
        f.setAttributes([t])
        out[t] = rd.road_class(f.attributes())[0]
    return out


def road(name, rclass, aadt, nd_cls=None):
    from ND_Dam_Hazard_Helper import linear as LN
    cls, basis, judge = LN.nd_road_class(rclass, aadt)
    return {"fid": 1, "name": name, "road_class": rclass, "class_basis": "", "paved": "Yes",
            "paved_basis": "", "bridge": False, "aadt": aadt, "aadt_year": 2024 if aadt else None,
            "aadt_source": "", "nd_class": nd_cls or cls, "nd_basis": basis, "nd_judgment": judge,
            "vehicle_zone": None, "persons": None, "length_ft": 500.0, "max_depth": 0.5,
            "max_vel": 0.5}


def roads_part():
    from qgis.core import QgsFeature, QgsField, QgsVectorLayer
    from ND_Dam_Hazard_Helper.compat import FT_STRING
    from ND_Dam_Hazard_Helper import classification as CL, linear as LN
    from ND_Dam_Hazard_Helper.constants import ND_HIGH, ND_MEDIUM, ND_LOW
    if not hasattr(QgsVectorLayer("LineString?crs=EPSG:5070", "x", "memory"), "dataProvider"):
        raise Skip("needs real QGIS memory layers")
    got = road_classes(US_STATE + LOCAL + INTERSTATE)
    for t in US_STATE:
        check("Hazard reads {!r} as a US / State highway".format(t),
              got[t] in (LN.ROAD_US, LN.ROAD_STATE), got[t])
    for t in LOCAL:
        check("Hazard reads {!r} as a local road".format(t), got[t] == LN.ROAD_OTHER, got[t])
    for t in INTERSTATE:
        check("Hazard reads {!r} as an interstate".format(t), got[t] == LN.ROAD_INTERSTATE, got[t])
    # 5.2.1.2 by road class and AADT
    for rc in (LN.ROAD_US, LN.ROAD_STATE):
        for aadt in (None, 4, 300, 499, 500, 2000):
            cls, basis, _j = LN.nd_road_class(rc, aadt)
            check("{} AADT {} -> Medium citing 5.2.1.2(b)".format(rc, aadt),
                  cls == ND_MEDIUM and "5.2.1.2(b)" in basis, (cls, basis))
        check("{} AADT 8,501 -> High".format(rc), LN.nd_road_class(rc, 8501)[0] == ND_HIGH)
    check("local road AADT 300 -> Low (5.2.1.2(a))",
          LN.nd_road_class(LN.ROAD_OTHER, 300)[0] == ND_LOW)
    check("private road -> Low", LN.nd_road_class(LN.ROAD_PRIVATE, None)[0] == ND_LOW)
    # ND NG9-1-1 roads: SHIELD USHY / STHY (US 83 through a town is 'S WASHINGTON ST')
    lyr = QgsVectorLayer("LineString?crs=EPSG:5070", "ng911", "memory")
    lyr.dataProvider().addAttributes([QgsField("FULL_NAME", FT_STRING), QgsField("SHIELD", FT_STRING),
                                      QgsField("RoadClass", FT_STRING)])
    lyr.updateFields()
    rd = LN.AttrReader(lyr)
    for name, shield, want in (("S WASHINGTON ST", "USHY", LN.ROAD_US),
                               ("MAIN ST", "STHY", LN.ROAD_STATE),
                               ("100TH AVE NE", "CORD", LN.ROAD_OTHER),
                               ("107M AVE SE", "", LN.ROAD_OTHER)):
        f = QgsFeature(lyr.fields())
        f.setAttributes([name, shield, "Primary"])
        got_c = rd.road_class(f.attributes())[0]
        check("NG9-1-1 {!r} SHIELD {!r} RoadClass Primary -> {}".format(name, shield, want),
              got_c == want, got_c)

    # criteria table: the 5.2.1.2(b) row, and the class
    nd = CL.classify_nd([], [road("HIGHWAY 23", LN.ROAD_STATE, 300.0),
                             road("30TH ST SE", LN.ROAD_OTHER, 66.0)], [],
                        structs_evaluated=False)
    row = next((c for c in nd["criteria"] if c["section"] == "5.2.1.2(b)"), None)
    check("criteria table has the 5.2.1.2(b) US / State highway row", row is not None,
          [c["section"] for c in nd["criteria"]])
    if row:
        check("5.2.1.2(b) row: 1 stretch, at least Medium",
              row["found"] == "1 stretch(es)" and row["result"].startswith(ND_MEDIUM), row)
    check("State highway AADT 300 + local road AADT 66 -> Medium", nd["cls"] == ND_MEDIUM,
          nd["cls"])
    nd0 = CL.classify_nd([], [road("30TH ST SE", LN.ROAD_OTHER, 66.0)], [],
                         structs_evaluated=False)
    check("local road AADT 66 alone -> Low", nd0["cls"] == ND_LOW, nd0["cls"])
    row0 = next(c for c in nd0["criteria"] if c["section"] == "5.2.1.2(b)")
    check("5.2.1.2(b) row not met without highways", row0["result"] == "not met", row0)

    # parity with the Runner / Screening rules module
    nh = _nd_hazard()
    if nh is None or not hasattr(nh, "us_state_highway_from_text"):
        print("NOTE nd_breach_runner / nd_dam_screening nd_hazard 1.3.0 not next to this plugin: parity skipped")
        return
    for t in US_STATE + LOCAL + INTERSTATE:
        h_us = got[t] in (LN.ROAD_US, LN.ROAD_STATE)
        h_i = got[t] == LN.ROAD_INTERSTATE
        check("parity {!r}: US/State {} / interstate {}".format(t, h_us, h_i),
              (nh.us_state_highway_from_text(t), nh.interstate_from_text(t)) == (h_us, h_i),
              (nh.us_state_highway_from_text(t), nh.interstate_from_text(t)))
    st = nh.Settings()
    for rc in (LN.ROAD_INTERSTATE, LN.ROAD_US, LN.ROAD_STATE, LN.ROAD_PRIVATE, LN.ROAD_OTHER):
        for aadt in (None, 4, 300, 499, 500, 1500, 2000, 2001, 8501):
            if aadt is None and rc == LN.ROAD_OTHER:
                continue        # no AADT on a local road: Hazard asks to verify (Low), the
                # Runner assumes its unknown_aadt (Medium) - a known, separate judgment item
            r = nh.Road("r", 1.0, aadt=aadt, interstate=rc == LN.ROAD_INTERSTATE,
                        private=rc == LN.ROAD_PRIVATE,
                        us_state=rc in (LN.ROAD_US, LN.ROAD_STATE))
            a, b = LN.nd_road_class(rc, aadt)[0], nh.road_class(r, st)[0]
            check("parity {} AADT {}: Hazard {} = Runner/Screening {}".format(rc, aadt, a, b),
                  a == b, (a, b))


@test
def us_state_highways_at_least_medium_5_2_1_2_b():
    del FAILS[:]
    roads_part()
    assert not FAILS, FAILS


if __name__ == "__main__":
    from ND_Dam_Hazard_Helper.tests import fake_qgis
    fake_qgis.install()
    us_state_highways_at_least_medium_5_2_1_2_b()
    print("ALL OK")
