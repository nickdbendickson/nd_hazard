"""3.26.5: NBI crossings as the NBI writes them.

The offline pack's NBI_bridges layer keeps the NBI's own field names: STRUCTURE_TYPE_043B
(19 = culvert), MAX_SPAN_LEN_MT_048 (metres), MAIN_UNIT_SPANS_045 (barrels). Before 3.26.5
none of them matched the crossings field synonyms, so every NBI culvert was read as an
unnamed bridge with no span. The NBI has no culvert rise, so an NBI culvert is cut only with
an assumed rise (CROSSING_RISE_FT, off by default). Culvert candidates name the inventory
crossing near them (as the DamSafety Runner's culvert_guess.match_inventory)."""
import os
import shutil
import tempfile

import numpy as np

from nd_breach_runner.core import gaps as gp
from nd_breach_runner.core import structures as structmod
from nd_breach_runner.core.grid import Grid
from nd_breach_runner.tests.registry import test
from nd_breach_runner.tests.test_v3200 import _algs


def _read_dem(bounds, cell):
    """A 3 m road embankment along y = 0 on flat ground at 100 m."""
    x0, y0, x1, y1 = bounds
    nx = max(4, int((x1 - x0) / cell))
    ny = max(4, int((y1 - y0) / cell))
    g = Grid(np.zeros((ny, nx)), x0, y1, cell)
    _X, Y = np.meshgrid(g.x_centers(), g.y_centers())
    return Grid(np.where(np.abs(Y) < 8.0, 103.0, 100.0), x0, y1, cell)


@test
def v3265_nbi_item_43b_19_is_a_culvert_and_other_codes_are_bridges():
    assert gp.kind_from("19", "STRUCTURE_TYPE_043B") == structmod.KIND_CULVERT
    assert gp.kind_from("'19'", "") == structmod.KIND_CULVERT
    assert gp.kind_from("02", "STRUCTURE_TYPE_043B") == structmod.KIND_BRIDGE
    assert gp.kind_from("4", "") == structmod.KIND_BRIDGE
    assert gp.kind_from("", "STRUCTURE_TYPE_043B") == structmod.KIND_BRIDGE
    # plain text keeps the 3.20.0 reading
    assert gp.kind_from("Box culvert", "type") == structmod.KIND_CULVERT
    assert gp.kind_from("BRIDGE", "type") == structmod.KIND_BRIDGE
    assert gp.kind_from(None, "type") == structmod.KIND_BRIDGE


@test
def v3265_nbi_span_in_metres_is_converted_to_feet():
    assert gp.is_metres_field("MAX_SPAN_LEN_MT_048")
    assert not gp.is_metres_field("span_ft") and not gp.is_metres_field("diameter")
    assert abs(gp.length_ft(3.7, "MAX_SPAN_LEN_MT_048") - 12.14) < 0.01
    assert gp.length_ft(12.0, "span_ft") == 12.0
    assert gp.length_ft(None, "MAX_SPAN_LEN_MT_048") is None


@test
def v3265_the_nbi_layer_reads_name_kind_span_and_barrels():
    algs, _mod, fq = _algs()
    a = algs.RunDamAlgorithm()
    a.initAlgorithm()
    assert a.parameterDefinition("CROSSING_RISE_FT") is not None
    nbi = dict(STRUCTURE_NUMBER_008="000000009116181", STRUCTURE_TYPE_043B="19",
               MAIN_UNIT_SPANS_045=2, MAX_SPAN_LEN_MT_048=3.0, FEATURES_DESC_006A="SWAN CREEK")
    br = dict(STRUCTURE_NUMBER_008="000000009117190", STRUCTURE_TYPE_043B="04",
              MAIN_UNIT_SPANS_045=1, MAX_SPAN_LEN_MT_048=11.3, FEATURES_DESC_006A="SWAN CREEK")
    src = fq.FakeSource([fq.FakeFeature(fq.FakeGeometry("point", [[(100.0, 0.0)]]), **nbi),
                         fq.FakeFeature(fq.FakeGeometry("point", [[(300.0, 0.0)]]), **br)])
    tf = lambda s: (fq.QgsCoordinateTransform(), fq.QgsCoordinateTransform())
    st = algs.workflow.RunSettings(cell_size_m=20.0)
    notes = a.apply_crossings(st, {"CROSSINGS": src, "CROSSING_GAPS": True},
                              fq.QgsProcessingContext(), tf, (0.0, 0.0), 5000.0)
    by = {c["name"]: c for c in st.crossings}
    culv, bridge = by["000000009116181"], by["000000009117190"]
    assert culv["kind"] == "culvert" and culv["barrels"] == 2
    assert abs(culv["span_ft"] - 9.84) < 0.01 and culv["rise_ft"] is None
    assert bridge["kind"] == "bridge" and abs(bridge["span_ft"] - 37.07) < 0.01
    assert "0 with a surveyed span and rise" in notes[0], notes
    assert not any("assumed" in n for n in notes)
    # with a rise to assume, the culvert (not the bridge) takes it and says so
    st = algs.workflow.RunSettings(cell_size_m=20.0)
    notes = a.apply_crossings(st, {"CROSSINGS": src, "CROSSING_GAPS": True,
                                   "CROSSING_RISE_FT": 6.0},
                              fq.QgsProcessingContext(), tf, (0.0, 0.0), 5000.0)
    by = {c["name"]: c for c in st.crossings}
    assert by["000000009116181"]["rise_ft"] == 6.0 and by["000000009116181"]["rise_assumed"]
    assert "assumed" in by["000000009116181"]["source"]
    assert by["000000009117190"]["rise_ft"] is None
    assert any("1 inventory culvert" in n and "6 ft" in n for n in notes), notes


@test
def v3265_an_assumed_rise_is_cut_but_never_deeper_than_the_embankment():
    roads = [np.array([[-200.0, 0.0], [200.0, 0.0]])]
    crossings = [{"name": "NBI culvert", "x": 0.0, "y": 2.0, "kind": "culvert",
                  "span_ft": 12.0, "rise_ft": None, "barrels": 2, "source": "NBI"},
                 {"name": "NBI bridge", "x": 80.0, "y": 1.0, "kind": "bridge",
                  "span_ft": 37.0, "source": "NBI"}]
    rows = gp.blocking_rows([dict(c) for c in crossings], roads, _read_dem, 3.0)
    assert not any(r["surveyed"] for r in rows)
    off = "\n".join(gp.markdown_lines(rows))
    assert "span 12, no rise" in off
    cs = [dict(c) for c in crossings]
    assert gp.assume_rise(cs, 0.0) == 0 and gp.assume_rise(cs, None) == 0
    assert gp.assume_rise(cs, 20.0) == 1                   # the culvert only
    rows = gp.blocking_rows(cs, roads, _read_dem, 3.0)
    culv = [r for r in rows if r["name"] == "NBI culvert"][0]
    assert culv["surveyed"] and culv["rise_assumed"]
    assert culv["rise_ft"] == culv["blocks_ft"] < 20.0     # capped at the 3 m embankment
    structs, _notes = gp.structures_from(rows, 3.0)
    assert len(structs) == 1 and structs[0].kind == "culvert" and structs[0].barrels == 2
    assert abs(structs[0].width_m - 12.0 * 0.3048) < 1e-6
    on = "\n".join(gp.markdown_lines(rows, cut=True))
    assert "(rise assumed)" in on


@test
def v3265_culvert_candidates_name_the_nearest_inventory_crossing_once():
    cands = [{"name": "candidate 1", "x": 0.0, "y": 0.0, "along_mi": 0.5, "road": "road 1",
              "angle_deg": 80.0, "source": "candidate"},
             {"name": "candidate 2", "x": 30.0, "y": 0.0, "along_mi": 0.6, "road": "road 1",
              "angle_deg": 80.0, "source": "candidate"},
             {"name": "candidate 3", "x": 2000.0, "y": 0.0, "along_mi": 1.8, "road": "road 2",
              "angle_deg": 85.0, "source": "candidate"}]
    crossings = [{"name": "9116181", "kind": "culvert", "x": 25.0, "y": 5.0, "span_ft": 9.84,
                  "rise_ft": 6.0, "rise_assumed": True, "barrels": 2, "source": "NBI"}]
    assert gp.inventory_near(cands, crossings) == 1
    assert "inventory" not in cands[0] and "inventory" not in cands[2]
    inv = cands[1]["inventory"]
    assert inv["name"] == "9116181" and inv["barrels"] == 2 and inv["span_ft"] == 9.84
    assert inv["rise_ft"] is None                          # an assumed rise is not inventory
    d = tempfile.mkdtemp()
    try:
        p = os.path.join(d, "c.csv")
        structmod.write_candidates_csv(p, cands)
        lines = open(p).read().splitlines()
        assert lines[0].endswith("inventory_barrels,inventory_distance_m")
        assert '"9116181",culvert,9.84,,2,' in lines[2], lines[2]
        assert lines[1].endswith('no,"",,,,,')
    finally:
        shutil.rmtree(d, ignore_errors=True)
