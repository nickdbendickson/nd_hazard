"""Tests of core/consequences.py and the 'consequences' Processing tool."""
import csv
import json
import os
import shutil
import tempfile

import numpy as np

from nd_breach_runner.tests.registry import test

# ---------------------------------------------------------------------------------------
# a small synthetic flood: 4 x 4 cells of 10 units, top-left corner at (0, 40)
#   cell centres: x = 5 + 10 c, y = 35 - 10 r
#   A (r0 c0): 3.0 ft deep, 1 ft/s, arrives after 2 h
#   B (r0 c1): 5.0 ft, 2 ft/s, arrives after 0.1 h (6 min)
#   C (r0 c2): 2.0 ft, 40 ft/s (depth x velocity 80), arrives after 0.5 h
#   D (r0 c3): dry
# ---------------------------------------------------------------------------------------


def _grid():
    from nd_breach_runner.core.grid import Grid
    return Grid(np.zeros((4, 4)), 0.0, 40.0, 10.0)


def _fields(depth_scale=1.0):
    d = np.zeros((4, 4))
    v = np.full((4, 4), 1.0)
    a = np.full((4, 4), np.nan)
    d[0, :3] = [3.0, 5.0, 2.0]
    v[0, :3] = [1.0, 2.0, 40.0]
    a[0, :3] = [2.0, 0.1, 0.5]
    return {"depth": d * depth_scale, "velocity": v, "arrival": a}


def _base_fields():
    """The same flood without the breach: only A is wet, 1 ft deep, arriving at 3 h."""
    d = np.zeros((4, 4))
    d[0, 0] = 1.0
    a = np.full((4, 4), np.nan)
    a[0, 0] = 3.0
    return {"depth": d, "velocity": np.full((4, 4), 1.0), "arrival": a}


def _structs(params=None):
    from nd_breach_runner.core import consequences as cs
    p = params or cs.ConsequenceParams()
    return [
        cs.make_structure("A", 5, 35, p, occupancy="residential", occupants_night=4,
                          structure_value=200000, ffh_ft=1.0),
        cs.make_structure("B", 15, 35, p, occupancy="commercial", occupants_night=10,
                          structure_value=1000000, contents_value=500000, ffh_ft=0.5),
        cs.make_structure("C", 25, 35, p, occupancy="residential", units=2,
                          structure_value=100000, ffh_ft=1.0),
        cs.make_structure("D", 35, 35, p, occupancy="commercial", occupants_night=3,
                          structure_value=50000),
    ]


def _close(a, b, tol=1e-6):
    assert abs(a - b) <= tol * max(1.0, abs(a), abs(b)), "{} != {}".format(a, b)


def _row(rows, ident):
    return next(r for r in rows if r["id"] == ident)


# ---------------------------------------------------------------------------------------
@test
def consequences_curve_interpolation():
    from nd_breach_runner.core import consequences as cs
    xs, ys = [0, 1, 2, 4], [0, 10, 30, 50]
    _close(cs.interp_curve(0.5, xs, ys), 5.0)
    _close(cs.interp_curve(3.0, xs, ys), 40.0)
    _close(cs.interp_curve(1.0, xs, ys), 10.0)
    assert cs.interp_curve(-0.5, xs, ys) == 0.0            # below the curve: no damage
    assert cs.interp_curve(9.0, xs, ys) == 50.0            # above it: the last value
    assert cs.interp_curve(float("nan"), xs, ys) == 0.0
    out = cs.interp_curve(np.array([0.5, 3.0, -1.0]), xs, ys)
    assert list(np.round(out, 6)) == [5.0, 40.0, 0.0]
    # the generic residential curve: 2 ft -> 25%, 3 ft -> 32.5%
    c = cs.default_curves()["residential"]
    _close(cs.interp_curve(3.0, c["depth_ft"], c["structure_pct"]), 32.5)
    assert c["source"] == cs.CURVE_SOURCE_GENERIC
    assert "PLACEHOLDER" in cs.DISCLAIMER and "MUST be checked" in cs.DISCLAIMER


@test
def consequences_hand_checked_damage_par_and_loss_of_life():
    from nd_breach_runner.core import consequences as cs
    rows, s = cs.assess(_structs(), _grid(), _fields())
    assert [r["id"] for r in rows] == ["A", "B", "C"]      # D is dry
    a, b, c = _row(rows, "A"), _row(rows, "B"), _row(rows, "C")
    # A: 3 ft deep, floor 1 ft up -> 2 ft over the floor -> 25% of 200,000; contents are
    # 50% of the structure value (100,000) x 25%
    _close(a["depth_above_floor_ft"], 2.0)
    _close(a["structure_dmg_pct"], 25.0)
    assert a["damage_usd"] == 75000 and a["contents_value"] == 100000
    assert a["par"] == 4 and a["severity"] == "medium" and a["warning_class"] == "adequate"
    _close(a["est_lol"], 4 * 0.03, 1e-3)
    # B: 4.5 ft over the floor: commercial structure 35% + 0.25 x 15 = 38.75%, contents
    # 50% + 0.25 x 20 = 55%
    _close(b["structure_dmg_pct"], 38.75)
    _close(b["contents_dmg_pct"], 55.0)
    assert b["damage_usd"] == 387500 + 275000
    assert b["warning_class"] == "short" and b["severity"] == "medium"   # 6 min; DV 10
    _close(b["est_lol"], 10 * 0.15, 1e-3)
    # C: depth x velocity 80 ft2/s -> destroyed: 100% of structure and contents
    assert c["destroyed"] is True and c["structure_dmg_pct"] == 100.0
    assert c["damage_usd"] == 100000 + 50000
    assert c["occupants_used"] == 5.0                      # 2 units x 2.5 persons
    assert c["severity"] == "high" and c["warning_class"] == "some"   # 30 min
    _close(c["est_lol"], 5 * 0.15, 1e-3)
    w = s["with_breach"]
    assert w["damage_usd"] == 75000 + 662500 + 150000
    assert w["structures_flooded"] == 3 and w["structures_destroyed"] == 1
    _close(w["par"], 19.0)
    _close(w["estimated_lol"], 0.12 + 1.5 + 0.75, 1e-3)
    assert s["baseline"] is None and s["incremental"]["damage_usd"] == w["damage_usd"]
    assert any("placeholder" in n.lower() for n in s["notes"])
    assert any("Graham" in n for n in s["notes"])


@test
def consequences_structure_defaults_and_occupants():
    from nd_breach_runner.core import consequences as cs
    p = cs.ConsequenceParams(persons_per_unit=3.0, default_units_residential=2)
    s = cs.make_structure("h", 0, 0, p)                    # nothing but a location
    assert s.occupancy == "residential" and s.occupants_night == 6.0
    assert s.ffh_ft == cs.FFH_BY_OCCUPANCY_FT["residential"] and s.structure_value is None
    assert "no_structure_value" in s.notes and "occupants_from_default_units" in s.notes
    s = cs.make_structure("f", 0, 0, p, occupancy="COM4", structure_value=1000,
                          foundation="B", contents_pct=25)
    assert s.occupancy == "commercial" and s.occupants_night == 0.0
    assert s.ffh_ft == cs.FFH_BY_FOUNDATION_FT["basement"]           # foundation beats occupancy
    assert s.contents_value == 250.0
    s = cs.make_structure("g", 0, 0, p, occupancy="industrial", occupants_day=7, ffh_ft=2.5)
    assert s.occupants_day == 7 and s.occupants_night == 7 and s.ffh_ft == 2.5
    assert cs.normalize_occupancy("RES1-1SNB") == "residential"
    assert cs.normalize_occupancy("Farm building") == "agricultural"
    assert cs.normalize_occupancy("???") == "other"
    assert cs.normalize_foundation("C") == "crawlspace" and cs.normalize_foundation("") == "other"
    # day / max population basis
    day = cs.ConsequenceParams(population="day")
    st = [cs.make_structure("x", 5, 35, day, occupancy="commercial", occupants_night=1,
                            occupants_day=9, structure_value=1000)]
    rows, summ = cs.assess(st, _grid(), _fields(), params=day)
    assert rows[0]["occupants_used"] == 9.0
    mx = cs.ConsequenceParams(population="max")
    rows, _ = cs.assess(st, _grid(), _fields(), params=mx)
    assert rows[0]["occupants_used"] == 9.0


@test
def consequences_warning_class_assignment():
    from nd_breach_runner.core import consequences as cs
    assert cs.WARNING_KEYS == ("none", "short", "some", "adequate")
    for minutes, want in ((-30.0, 0), (0.0, 0), (0.1, 1), (14.9, 1), (15.0, 2), (59.9, 2),
                          (60.0, 3), (600.0, 3), (float("nan"), 0)):
        assert cs.warning_class(minutes) == want, (minutes, want)
    assert cs.warning_class(10.0, (5.0, 30.0)) == 2        # class edges are parameters
    got = cs.warning_class(np.array([-1.0, 10.0, 20.0, 90.0]))
    assert list(got) == [0, 1, 2, 3]
    # the lag is subtracted from the arrival time: A arrives at 2 h
    lagged = cs.ConsequenceParams(lag_hr=1.75)             # 15 min of warning left
    rows, _ = cs.assess(_structs(lagged), _grid(), _fields(), params=lagged)
    a = _row(rows, "A")
    _close(a["warning_min"], 15.0, 1e-3)
    assert a["warning_class"] == "some"
    late = cs.ConsequenceParams(lag_hr=2.5)                # notified after the water arrives
    rows, _ = cs.assess(_structs(late), _grid(), _fields(), params=late)
    assert _row(rows, "A")["warning_class"] == "none" and _row(rows, "A")["warning_min"] < 0
    assert cs.warning_labels()["some"] == "15 to 60 min"


@test
def consequences_unknown_arrival_counts_as_no_warning_and_missing_velocity_is_reported():
    from nd_breach_runner.core import consequences as cs
    f = _fields()
    f["arrival"] = None
    f["velocity"] = None
    rows, s = cs.assess(_structs(), _grid(), f)
    assert all(r["warning_class"] == "none" for r in rows)
    assert s["data_gaps"]["arrival_unknown"] == 3 and s["data_gaps"]["no_velocity"] == 3
    assert not any(r["destroyed"] for r in rows)           # no velocity: the DV rule is off
    assert any("no arrival" in n for n in s["notes"]) and any("velocity" in n for n in s["notes"])


@test
def consequences_depth_rules_and_parameters():
    from nd_breach_runner.core import consequences as cs
    # a deep flood destroys by depth alone
    p = cs.ConsequenceParams(destroy_depth_ft=4.0, destroy_dv_ft2s=1e9)
    rows, _ = cs.assess(_structs(p), _grid(), _fields(), params=p)
    assert _row(rows, "B")["destroyed"] and not _row(rows, "A")["destroyed"]
    # PAR needs the depth threshold: A is 3 ft deep
    p = cs.ConsequenceParams(par_depth_ft=3.5)
    rows, _ = cs.assess(_structs(p), _grid(), _fields(), params=p)
    assert _row(rows, "A")["par"] == 0 and _row(rows, "B")["par"] == 10
    # invalid parameters are refused
    for bad in (dict(population="noon"), dict(warning_edges_min=(60, 15)),
                dict(lag_hr=-1), dict(par_depth_ft=-1)):
        try:
            cs.ConsequenceParams(**bad).validate()
            assert False, bad
        except ValueError:
            pass


@test
def consequences_incremental_is_with_minus_without_and_never_negative():
    from nd_breach_runner.core import consequences as cs
    rows, s = cs.assess(_structs(), _grid(), _fields(), _base_fields())
    a = _row(rows, "A")
    # without the breach A sees 1 ft: no damage (the floor is 1 ft up), PAR 4, low severity,
    # 180 min of warning
    assert a["base_damage_usd"] == 0 and a["base_par"] == 4
    _close(a["base_lol"], 4 * 0.0003, 1e-2)
    assert a["inc_damage_usd"] == 75000 and a["inc_par"] == 0
    _close(a["inc_lol"], 0.12 - 0.0012, 1e-3)
    assert _row(rows, "B")["inc_damage_usd"] == 662500 and _row(rows, "C")["inc_par"] == 5
    inc, base = s["incremental"], s["baseline"]
    assert inc["damage_usd"] == 887500 and base["damage_usd"] == 0
    _close(inc["par"], 15.0)
    _close(inc["estimated_lol"], 0.1188 + 1.5 + 0.75, 1e-3)
    assert inc["structures_with_incremental_damage"] == 3
    assert not any("incremental equals" in n for n in s["notes"])
    # the baseline equal to the breach: nothing is incremental
    rows, s = cs.assess(_structs(), _grid(), _fields(), _fields())
    assert s["incremental"]["damage_usd"] == 0 and s["incremental"]["par"] == 0
    assert s["incremental"]["estimated_lol"] == 0
    assert all(r["inc_damage_usd"] == 0 and r["inc_par"] == 0 and r["inc_lol"] == 0
               for r in rows)
    assert s["baseline"]["damage_usd"] == s["with_breach"]["damage_usd"]
    # a baseline worse than the breach never gives a negative increment
    rows, s = cs.assess(_structs(), _grid(), _fields(), _fields(depth_scale=2.0))
    assert all(r["inc_damage_usd"] >= 0 and r["inc_par"] >= 0 and r["inc_lol"] >= 0
               for r in rows)
    assert s["incremental"]["damage_usd"] >= 0
    assert s["baseline"]["damage_usd"] > s["with_breach"]["damage_usd"]
    assert s["incremental"]["damage_usd"] == 0


@test
def consequences_si_arrays_give_the_same_answer():
    from nd_breach_runner.core import consequences as cs
    from nd_breach_runner.core.units import FT
    f = _fields()
    si = {"depth": f["depth"] * FT, "velocity": f["velocity"] * FT, "arrival": f["arrival"]}
    r1, s1 = cs.assess(_structs(), _grid(), f)
    r2, s2 = cs.assess(_structs(), _grid(), si, units="si")
    assert s1["with_breach"] == s2["with_breach"]
    assert [r["damage_usd"] for r in r1] == [r["damage_usd"] for r in r2]
    try:
        cs.assess(_structs(), _grid(), {"depth": np.zeros((2, 2))})
        assert False, "a wrong-sized array must be refused"
    except ValueError:
        pass


@test
def consequences_user_csv_curves_and_fatality_table_override():
    from nd_breach_runner.core import consequences as cs
    d = tempfile.mkdtemp()
    cp = os.path.join(d, "curves.csv")
    with open(cp, "w") as f:
        f.write("occupancy,depth_ft,structure_pct,contents_pct\n"
                "residential,2,100,50\nresidential,0,0,0\n"
                "res9-special,0,0,0\nres9-special,1,10,10\n")
    user = cs.load_curves_csv(cp)
    assert list(user["residential"]["depth_ft"]) == [0.0, 2.0]        # sorted
    curves = cs.merge_curves(user)
    assert curves["commercial"]["source"] == cs.CURVE_SOURCE_GENERIC      # others stay generic
    assert curves["residential"]["source"] == "curves.csv"
    rows, s = cs.assess(_structs(), _grid(), _fields(), curves=curves)
    a = _row(rows, "A")                       # 2 ft over the floor: 100% / 50% of 100,000
    assert a["structure_dmg_pct"] == 100.0 and a["damage_usd"] == 200000 + 50000
    assert _row(rows, "B")["structure_dmg_pct"] == 38.75              # commercial unchanged
    assert s["curves"]["user_curves_used"] == ["residential"]
    assert "commercial" in s["curves"]["generic_placeholders_used"]
    # a structure is matched by its own occupancy text first
    st = cs.make_structure("Z", 5, 35, None, occupancy="RES9-SPECIAL", structure_value=1000,
                           ffh_ft=2.0)
    rows, _ = cs.assess([st], _grid(), _fields(), curves=curves)
    assert rows[0]["curve"] == "res9-special" and rows[0]["structure_dmg_pct"] == 10.0
    # the fatality table
    tp = os.path.join(d, "fat.csv")
    with open(tp, "w") as f:
        f.write("severity,none,short,some,adequate\nlow,0.1,0.1,0.1,0.1\n"
                "Med,0.5,0.5,0.5,0.5\nhigh,0.9,0.9,0.9,0.9\n")
    table = cs.load_fatality_csv(tp)
    rows, s = cs.assess(_structs(), _grid(), _fields(), fatality_table=table,
                        fatality_source="fat.csv")
    _close(_row(rows, "A")["est_lol"], 4 * 0.5, 1e-3)                 # medium: 0.5
    _close(_row(rows, "C")["est_lol"], 5 * 0.9, 1e-3)                 # high: 0.9
    assert s["fatality_table"]["source"] == "fat.csv"
    assert not any("built-in fatality table" in n for n in s["notes"])
    # bad files are refused with a message
    bad = os.path.join(d, "bad.csv")
    for text, key in (("occupancy,depth_ft,structure_pct,contents_pct\nresidential,1,150,0\n",
                       "0 to 100"),
                      ("occupancy,depth_ft,structure_pct,contents_pct\nresidential,1,5,0\n"
                       "residential,1,6,0\n", "twice"),
                      ("occupancy,depth_ft,structure_pct\nresidential,1,5\n", "contents_pct")):
        open(bad, "w").write(text)
        try:
            cs.load_curves_csv(bad)
            assert False, text
        except ValueError as e:
            assert key in str(e), str(e)
    open(bad, "w").write("severity,none,short,some,adequate\nlow,2,0,0,0\nmedium,0,0,0,0\n"
                         "high,0,0,0,0\n")
    try:
        cs.load_fatality_csv(bad)
        assert False
    except ValueError as e:
        assert "0 to 1" in str(e)
    open(bad, "w").write("severity,none,short,some,adequate\nlow,0,0,0,0\n")
    try:
        cs.load_fatality_csv(bad)
        assert False
    except ValueError as e:
        assert "medium" in str(e)
    shutil.rmtree(d)


@test
def consequences_roads_flooded_length_and_vehicles_exposed():
    from nd_breach_runner.core import consequences as cs
    from nd_breach_runner.core import impacts
    d = np.zeros((4, 4))
    d[1, :2] = 0.6                                         # row 1 (y 20-30): the road, 2 cells wet
    fields = {"depth": d}
    rec = impacts.Receptors()
    rec.add_lines([np.array([(0.0, 25.0), (40.0, 25.0)])], "road", ids=["Hwy 1"], spacing=10.0)
    rec.add_lines([np.array([(0.0, 5.0), (40.0, 5.0)])], "road", ids=["Dry Rd"], spacing=10.0)
    p = cs.ConsequenceParams(vehicle_window_hr=2.0)
    r = cs.assess_roads(rec, _grid(), fields, None, p, aadt={"Hwy 1": 2400, "Dry Rd": 500})
    _close(r["road"]["flooded_km"], 0.02)                  # two samples of 10 m
    assert r["road"]["incremental_km"] == r["road"]["flooded_km"]
    assert r["vehicles"]["exposed"] == 200.0               # 2400 / 24 x 2 h; Dry Rd not flooded
    assert [f["id"] for f in r["features"]] == ["Hwy 1"]
    # the same road flooded without the breach: nothing incremental
    r = cs.assess_roads(rec, _grid(), fields, fields, p, aadt={"Hwy 1": 2400})
    assert r["road"]["incremental_km"] == 0 and r["vehicles"]["incremental"] == 0.0
    # through assess(), and in the report
    rows, s = cs.assess(_structs(), _grid(), _fields(), None, roads=rec,
                        road_aadt={"Hwy 1": 2400})
    rows, s = cs.assess(_structs(), _grid(), {"depth": d}, None, roads=rec,
                        road_aadt={"Hwy 1": 2400})
    assert s["roads"]["road"]["flooded_km"] > 0
    assert "Roads and rail" in cs.markdown_summary(s)
    empty = impacts.Receptors()
    assert cs.assess_roads(empty, _grid(), fields) == {}


@test
def consequences_outputs_csv_markdown_geojson():
    from nd_breach_runner.core import consequences as cs
    rows, s = cs.assess(_structs(), _grid(), _fields(), _base_fields())
    d = tempfile.mkdtemp()
    paths = cs.write_outputs(d, rows, s, "EPSG:26914")
    with open(paths["csv"]) as f:
        got = list(csv.DictReader(f))
    assert [r["id"] for r in got] == ["A", "B", "C"] and list(got[0].keys()) == cs.ROW_COLUMNS
    md = open(paths["markdown"]).read()
    assert "GENERIC PLACEHOLDERS" in md and "MUST be checked" in md
    assert "Incremental" in md and "built-in approximate default" in md
    assert "$887,500" in md
    js = json.load(open(paths["json"]))
    assert js["incremental"]["damage_usd"] == 887500
    gj = json.load(open(paths["geojson"]))
    assert gj["type"] == "FeatureCollection" and len(gj["features"]) == 3
    assert gj["crs"]["properties"]["name"] == "EPSG:26914"
    f0 = gj["features"][0]
    assert f0["geometry"] == {"type": "Point", "coordinates": [5.0, 35.0]}
    assert f0["properties"]["inc_damage_usd"] == 75000 and "x" not in f0["properties"]
    # dry structures only when asked; no GeoJSON when not wanted
    keep = cs.ConsequenceParams(include_dry=True)
    rows2, _ = cs.assess(_structs(), _grid(), _fields(), params=keep)
    assert len(rows2) == 4
    paths = cs.write_outputs(d, rows, s, None, geojson=False)
    assert paths["geojson"] == ""
    shutil.rmtree(d)


@test
def consequences_assess_run_result_uses_the_workflow_arrays():
    import types
    from nd_breach_runner.core import consequences as cs
    from nd_breach_runner.core.units import FT
    f, b = _fields(), _base_fields()
    si = lambda x: {"depth": x["depth"] * FT, "velocity": x["velocity"] * FT,
                    "arrival": x["arrival"]}
    res = types.SimpleNamespace(grid=_grid(), final_si=si(f), base_si=si(b), folder="")
    rows, s = cs.assess_run_result(res, _structs())
    _, ref = cs.assess(_structs(), _grid(), f, b)
    assert s["incremental"] == ref["incremental"] and s["with_breach"] == ref["with_breach"]


# ---------------------------------------------------------------------------------------
# the Processing tool, through the fake QGIS
# ---------------------------------------------------------------------------------------
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
    return ac, fq


@test
def consequences_algorithm_is_defined_with_the_expected_inputs():
    ac, fq = _load_tool()
    assert ac.CONSEQUENCE_ALGORITHMS == (ac.ConsequencesAlgorithm,)
    a = ac.ConsequencesAlgorithm()
    a.initAlgorithm()
    assert a.name() == "consequences"
    assert a.displayName() == "Life-safety and damage estimate (incremental)"
    for k in ("DAM_FOLDER", "STRUCTURES", "F_OCC", "F_NIGHT", "F_DAY", "F_UNITS", "F_VALUE",
              "F_CONTENTS", "F_CONTENTS_PCT", "F_FFH", "F_FOUNDATION", "ROADS", "R_AADT",
              "RAIL", "CURVES_CSV", "FATALITY_CSV", "LAG_HR", "WARN_EDGES", "PAR_DEPTH_FT",
              "DESTROY_DV", "DESTROY_DEPTH_FT", "SEV_MED_DEPTH_FT", "SEV_HIGH_DV"):
        assert a.parameterDefinition(k) is not None, k
    for k in ("CSV", "REPORT", "GEOJSON", "RESULT"):
        assert k in a.outputs
    h = a.shortHelpString()
    assert "PLACEHOLDERS" in h and "MUST be checked" in h
    assert a.createInstance().name() == "consequences"


@test
def consequences_algorithm_end_to_end_through_the_fake_harness():
    ac, fq = _load_tool()
    from nd_breach_runner.core import gdal_io
    from nd_breach_runner.core.grid import Grid
    from nd_breach_runner.core import workflow
    d = tempfile.mkdtemp()
    arrays = {}
    for names, f in ((workflow.FINAL_NAMES, _fields()), (workflow.BASELINE_NAMES, _base_fields())):
        for kind in ("depth", "velocity", "arrival"):
            p = os.path.join(d, names[kind])
            open(p, "w").write("x")
            arrays[p] = f[kind]
    real = (gdal_io.read_grid, gdal_io.read_like, gdal_io.epsg_of)
    gdal_io.read_grid = lambda p: Grid(arrays[p], 0.0, 40.0, 10.0, crs_wkt="EPSG:26914")
    gdal_io.read_like = lambda p, g, alg="bilinear": arrays[p]
    gdal_io.epsg_of = lambda wkt: "EPSG:26914"

    def pt(x, y, **at):
        return fq.FakeFeature(fq.FakeGeometry("point", [[(x, y)]]), **at)
    poly = fq.FakeFeature(fq.FakeGeometry("polygon", [[(10, 30), (20, 30), (20, 40), (10, 40)]]),
                          occtype="COM1", val_struct=1000000.0, val_cont=500000.0, found_ht=0.5,
                          occupants_night=10.0, fid="B")
    src = fq.FakeSource([
        pt(5, 35, occtype="RES1-1SNB", val_struct=200000.0, found_ht=1.0,
           occupants_night=4.0, fid="A"),
        poly,
        pt(25, 35, occtype="RES1", val_struct=100000.0, found_ht=1.0, units=2.0, fid="C"),
        pt(35, 35, occtype="COM1", val_struct=50000.0, found_ht=0.5, fid="D"),
        pt(5000, 5000, occtype="RES1", val_struct=1.0, fid="far")])
    road = fq.FakeFeature(fq.FakeGeometry("line", [[(0, 25), (40, 25)]]), name="Hwy 1",
                          aadt=2400.0)
    try:
        ctx = fq.QgsProcessingContext()
        fb = fq.QgsProcessingFeedback()
        a = ac.ConsequencesAlgorithm()
        a.initAlgorithm()
        r = a.processAlgorithm({"DAM_FOLDER": d, "STRUCTURES": src, "LAG_HR": 0.0,
                                "ROADS": fq.FakeSource([road])}, ctx, fb)
        for k in ("CSV", "REPORT", "SUMMARY_JSON", "GEOJSON"):
            assert os.path.exists(r[k]), (k, r[k])
        assert r["CSV"].startswith(d) and r["CSV"].endswith("Consequences (Structures).csv")
        s = json.load(open(r["SUMMARY_JSON"]))
        # A and C as in the hand check; B is a polygon sampled at its centroid (15, 35);
        # the structure 7 km away is outside the rasters and never read
        assert s["structures"] == 4, s["structures"]
        assert s["with_breach"]["damage_usd"] == 75000 + 662500 + 150000
        assert s["incremental"]["damage_usd"] == 887500 and s["baseline"]["damage_usd"] == 0
        assert abs(s["with_breach"]["estimated_lol"] - 2.37) < 1e-3
        # the road: two 10-unit samples flooded (0.6 ft is not deep here: cells r1 are dry, so
        # the road at y = 25 crosses dry cells) -> nothing flooded, 2400 AADT not exposed
        assert s["roads"]["road"]["flooded_km"] == 0.0 and s["roads"]["vehicles"]["exposed"] == 0
        rows = list(csv.DictReader(open(r["CSV"])))
        assert sorted(x["id"] for x in rows) == ["A", "B", "C"]
        assert next(x for x in rows if x["id"] == "A")["occupancy"] == "residential"
        gj = json.load(open(r["GEOJSON"]))
        assert len(gj["features"]) == 3 and gj["crs"]["properties"]["name"] == "EPSG:26914"
        assert any("Structure fields" in m for m in fb.info)
        assert any("built-in fatality table" in e for e in fb.errors)   # the loud caveat
        assert "damage $887,500 (incremental $887,500)" in r["RESULT"], r["RESULT"]
        assert any(p.endswith(".geojson") for p in ctx.loaded)
        assert "Consequences Summary" in r["REPORT"]

        # user CSVs, chosen fields and parameters
        curves = os.path.join(d, "my_curves.csv")
        open(curves, "w").write("occupancy,depth_ft,structure_pct,contents_pct\n"
                                "residential,0,0,0\nresidential,1,100,100\n")
        fat = os.path.join(d, "my_table.csv")
        open(fat, "w").write("severity,none,short,some,adequate\nlow,0,0,0,0\n"
                             "medium,0.2,0.2,0.2,0.2\nhigh,1,1,1,1\n")
        r2 = a.processAlgorithm({"DAM_FOLDER": d, "STRUCTURES": src, "CURVES_CSV": curves,
                                 "FATALITY_CSV": fat, "F_NIGHT": "occupants_night",
                                 "F_VALUE": "val_struct", "WRITE_GEOJSON": False,
                                 "POPULATION": 2, "WARN_EDGES": "10, 45"}, ctx,
                                fq.QgsProcessingFeedback())
        s2 = json.load(open(r2["SUMMARY_JSON"]))
        assert s2["fatality_table"]["source"] == fat
        assert s2["curves"]["user_curves_used"] == ["residential"]
        assert s2["parameters"]["warning_edges_min"] == [10.0, 45.0]
        assert r2["GEOJSON"] == ""

        # errors are Processing exceptions, not tracebacks
        for bad in ({"DAM_FOLDER": os.path.join(d, "nowhere"), "STRUCTURES": src},
                    {"DAM_FOLDER": d, "STRUCTURES": src, "WARN_EDGES": "60, 15"},
                    {"DAM_FOLDER": d, "STRUCTURES": fq.FakeSource([pt(9000, 9000)])},
                    {"DAM_FOLDER": d, "STRUCTURES": src, "CURVES_CSV": os.path.join(d, "none.csv")}):
            try:
                a.processAlgorithm(bad, ctx, fq.QgsProcessingFeedback())
                assert False, bad
            except fq.QgsProcessingException:
                pass
    finally:
        gdal_io.read_grid, gdal_io.read_like, gdal_io.epsg_of = real
        shutil.rmtree(d)
