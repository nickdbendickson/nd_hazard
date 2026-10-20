"""Audit H1 (ported from the sibling DamSafety_Hazard, commit 1d6b76d): a 911 address
point with no building outline within the address snap distance and no NSI record
(lone_address) stays habitable by the ND site address rule.

This is conservative: a new rural ND house is often in neither FEMA USA Structures nor
the NSI yet, so its 911 address point is the evidence it exists. The missing building is
only noted and listed for the engineer's review (field access, vacant lot, tower or
utility site?). An earlier build marked such points Unverified, which never raises the
hazard class - a dam that should screen High could read "LOW - no habitable structures".

Runs without QGIS: _interpret is pure dict manipulation. tests/fake_qgis.py (installed by
run_tests before this module imports) stands in for PyQGIS when there is no real one.

    python3 -m ND_Dam_Hazard_Helper.tests.run_tests lone_911
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_DIR = os.path.dirname(HERE)
if os.path.dirname(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, os.path.dirname(PLUGIN_DIR))
from ND_Dam_Hazard_Helper.tests.registry import test  # noqa: E402
from ND_Dam_Hazard_Helper import structures as ST  # noqa: E402


def _addr911_src():
    """An ND GIS Hub 911 site-address point (the evidence), with no footprint of its own."""
    return {"family": "NDGISHUB", "texts": [], "address": True, "addr_key": "123 farm rd",
            "unit": "", "placement": None, "place_kind": None, "nena": True,
            "place_type": "", "fema": "", "snapped": None, "nguid": "ND-911-1"}


@test
def lone_911_address_point_stays_habitable_h1():
    # a Place_Type that would otherwise read as not-habitable must NOT hold the point down:
    # the ND site address rule still counts it, and keeps the Place_Type in a note.
    src = dict(_addr911_src(), place_type="PARKING", place_kind=ST._PT_NONHAB)
    rec = {"id": 9, "footprint": False, "area_ft2": None, "families": {"NDGISHUB"},
           "notes": [], "sources": [src], "lone_address": True}
    ST._interpret(rec)
    assert rec["category"] in (ST.CAT_DWELLING, ST.CAT_OCCUPIED), rec["category"]
    assert rec["habitable"] == "Yes", rec["habitable"]
    # the missing building is still surfaced for review, not silently dropped
    assert "no building outline" in (rec.get("site_note") or ""), rec.get("site_note")
