"""Hazard Helper 2.35.11: DV chart zone colours match ND Dam Safety Standards REG_05.2026.

REG_05.2026 Figures 5.1 / 5.2 (pp. 21-22) fill the zones High #EF9897, Judgment #F9F4A3 and
Low #C4E0AD (sampled from the published figures; the text names no colours) and label them
HIGH DANGER ZONE / JUDGMENT ZONE / LOW DANGER ZONE. The Helper's report charts
(ND_Dam_Hazard_Helper/dvsheet.py chart_svg) must use those fills at full opacity, fill the
High zone (not leave it white), use the standard's dark-red / orange-brown / dark-green label
text, and must not draw the Excel gold title banner.

Mirrors DWR_Plugins DamSafety_Hazard/tests/test_zone_colors_reg05.py for the Helper chart path.
Runner / Screening core/nd_hazard.py chart_svg already uses the same fills (checked here too
when those plugins sit next to Helper in the repo).

    python3 -m ND_Dam_Hazard_Helper.tests.run_tests zone_colors_reg05
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_DIR = os.path.dirname(HERE)
REPO = os.path.dirname(PLUGIN_DIR)
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from ND_Dam_Hazard_Helper.tests.registry import test  # noqa: E402

STD = {"high": "#EF9897", "judgment": "#F9F4A3", "low": "#C4E0AD"}
TEXT = {"high": "#951b21", "judg": "#b26222", "low": "#17632b"}
ZONE_NAMES = ("HIGH DANGER ZONE", "JUDGMENT ZONE", "LOW DANGER ZONE")
# Excel / template palette that must not remain in the Helper DV chart
OLD = ("#FFC000", "#999371", "#FFC040", "#C00000", "#C55A11", "#375623",
       "#FBE7B5", "#F4B6B0")


def _read(rel):
    p = os.path.join(REPO, rel)
    if not os.path.exists(p):
        return None
    with open(p, encoding="utf-8") as f:
        return f.read()


def _fills(svg):
    return [m.lower() for m in re.findall(r'fill="(#[0-9a-fA-F]{6})"', svg)]


@test
def zone_colors_dvsheet_constants_are_reg05():
    s = _read("ND_Dam_Hazard_Helper/dvsheet.py")
    assert s is not None
    body = s[s.index("# REG_05.2026 Fig 5.1"):s.index("def _nice_step")]
    for key, hx in (("HIGH", STD["high"]), ("JUDG", STD["judgment"]), ("LOW", STD["low"])):
        assert re.search(r'_{}_FILL = "{}"'.format(key, hx), body, re.I), (key, body[:400])
    for key, hx in TEXT.items():
        assert '"{}"'.format(hx) in body, (key, hx)
    for old in ("#FFC000", "#999371", "#FFC040"):
        assert old.lower() not in s.lower(), "dvsheet still has Excel palette {}".format(old)


@test
def zone_colors_dvsheet_chart_svg_fills_and_labels():
    from ND_Dam_Hazard_Helper import dvsheet as D
    rows = [{"id": 1, "kind": D.HOUSE, "vel": 4.0, "depth": 3.0, "arr_hr": 0.5,
             "zone": "JUDGMENT", "outcome": ""}]
    svg = D.chart_svg(D.HOUSE, rows, D.curve_at_tm11(D.HOUSE))
    fills = _fills(svg)
    for hx in STD.values():
        assert hx.lower() in fills, (hx, fills)
    # High must be filled (not only judgment / low)
    assert fills.count(STD["high"].lower()) >= 1
    for name in ZONE_NAMES:
        assert name in svg, name
    for hx in TEXT.values():
        assert hx.lower() in svg.lower(), hx
    assert "#ffc000" not in svg.lower()
    for old in OLD:
        assert old.lower() not in svg.lower(), "rendered chart still has {}".format(old)
    # no gold title banner rect
    assert not re.search(r'<rect[^>]*fill="#FFC000"', svg, re.I)


@test
def zone_colors_runner_screening_chart_svg_already_reg05():
    """Sibling plugins' review charts already use the standard fills (DWR #126 twin)."""
    for rel in ("nd_breach_runner/core/nd_hazard.py", "nd_dam_screening/core/nd_hazard.py"):
        s = _read(rel)
        if s is None:
            continue
        body = s[s.index("def chart_svg"):]
        for hx in STD.values():
            assert body.lower().count(hx.lower()) >= 2, (rel, hx)  # zone + legend
        for name in ZONE_NAMES:
            assert '"{}"'.format(name) in body, (rel, name)
