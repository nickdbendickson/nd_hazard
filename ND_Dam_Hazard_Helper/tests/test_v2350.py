"""v2.35.0 (Nick, 10/30/2026): "any house in the judgement zone should be automatically
labeled as medium hazard". A habitable structure in the ACER judgment zone makes the dam ND
MEDIUM by rule - not probable loss of life, not a provisional High. Houses in the high danger
zone still make it High. TM 11 is not changed. The old setting (judgment zone counted as
probable loss of life, High) stays as an option that is off by default (Nick decided).
Synthetic structures only (the depth / velocity of the two review houses; no DWR data, no
rasters).

    python3 -m ND_Dam_Hazard_Helper.tests.run_tests v2350
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_DIR = os.path.dirname(HERE)
if os.path.dirname(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, os.path.dirname(PLUGIN_DIR))
from ND_Dam_Hazard_Helper.tests.registry import test  # noqa: E402

HOUSE = "House on foundation"
H2801 = (4010, 3.10, 0.11)     # DWR-2801 review house: 3.10 ft at 0.11 ft/s
H4031 = (9953, 2.111, 6.95)    # DWR-4031 review house: 2.111 ft at 6.95 ft/s - Struct_ID 9953 in
                               # the 30 m toe run (was "16775"; IDs differ by footprint set, 16775 is dry there)


def _mods():
    from ND_Dam_Hazard_Helper import classification as CL
    from ND_Dam_Hazard_Helper.damsuite import tm11_curves as TC
    from ND_Dam_Hazard_Helper import constants as C
    from ND_Dam_Hazard_Helper import structures as ST
    return CL, TC, C, ST


def _house(sid, depth, vel, persons=3):
    CL, TC, C, ST = _mods()
    z = TC.zone("house", depth, vel)
    lo, hi = TC.thresholds("house", vel)
    rec = {"id": sid, "habitable": "Yes", "acer_type": HOUSE, "persons": persons,
           "max_depth": depth, "max_vel": vel, "margin_judg": lo - depth,
           "margin_high": hi - depth}
    if z == TC.HIGH:
        rec.update(zone=C.CLASS_HIGH, outcome=ST.OUT_PROBABLE)
    elif z == TC.JUDGMENT:
        rec.update(zone=C.CLASS_JUDGMENT, outcome=ST.OUT_JUDGMENT)
    else:
        raise AssertionError("not in a High / Judgment zone: {}".format(rec))
    return rec


def _run(structs, **kw):
    CL = _mods()[0]
    nd = CL.classify_nd(structs, [], [], **kw)
    CL.extra_criteria(nd, structs, {}, {})
    return nd


def _result(nd):
    return [c for c in nd["criteria"] if c["section"] == "Result"][0]["result"]


@test
def v2350_review_houses_are_judgment_zone():
    TC = _mods()[1]
    for sid, d, v in (H2801, H4031):
        assert TC.zone("house", d, v) == TC.JUDGMENT, (sid, TC.thresholds("house", v))


@test
def v2350_judgment_zone_house_is_medium_by_rule():
    C = _mods()[2]
    for h in (H2801, H4031):
        nd = _run([_house(*h)])
        assert nd["cls"] == C.ND_MEDIUM and not nd.get("provisional"), (h, nd["reasons"])
        assert _result(nd) == "MEDIUM" and nd["judgment_rule"] == "medium"
        row = [c for c in nd["criteria"] if "judgment zone" in str(c.get("criterion", ""))
               and c["section"] == "5.2.1.1"]
        assert row and row[0]["result"] == C.ND_MEDIUM + " (by rule)", row
        text = "\n".join(nd["reasons"] + nd["judgment"])
        assert "classed MEDIUM by rule" in text, text
        assert "HIGH" not in "\n".join(nd["reasons"]) and "provisional" not in text.lower()
        assert "would make the dam HIGH" not in text and "5.3(f)" not in text, text


@test
def v2350_tm11_unchanged_and_high_zone_still_high():
    CL, TC, C, ST = _mods()
    tm = CL.classify_tm11([_house(*H2801)], [])
    assert tm["lij_structures_judgment"] == 3, tm
    assert str(tm["cls"]).lower() == "significant", tm["cls"]
    nd = _run([_house(1, 6.0, 6.0), _house(*H2801)])
    assert nd["cls"] == C.ND_HIGH and not nd.get("provisional"), nd["reasons"]


@test
def v2350_old_option_kept_off_by_default():
    CL, TC, C, ST = _mods()
    nd = _run([_house(*H2801)], judgment_as_probable=True)
    assert nd["cls"] == C.ND_HIGH and nd.get("provisional") and nd["judgment_rule"] == "probable"
    from ND_Dam_Hazard_Helper import pipeline as PL
    assert PL.DEFAULTS["nd_judgment_as_probable"] is False
    assert PL.DEFAULTS["judgment_as_probable"] is True        # TM 11 only
    from ND_Dam_Hazard_Helper import history as HI
    assert "nd_judgment_as_probable" in HI.SETTING_KEYS
    s = [_house(*H2801)]
    L = CL.summary_lines(_run(s), CL.classify_tm11(s, []), s, [], [], "synthetic", [], False)
    blob = "\n".join(L)
    assert "classed MEDIUM by rule" in blob and "HIGH is provisional" not in blob, blob
