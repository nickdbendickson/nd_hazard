"""v3.12.0 tests: the run's culverts, bridges and notches in the HEC-RAS model
(core/hecras_model.py: structure_connection, structure_block, build_model).

The text is checked against the layout of HEC's own example geometries (HEC-RAS 6.6 example
projects): the internal connection of BaldEagleDamBrk.g09 ('Sayers Dam': both sides the same
2D area) and the culvert group of Diamond.g01 and Beaver_LS_SAConn.g01 ('Connection Culv=':
shape, rise, span, length, n, entrance loss, exit loss, chart, scale, upstream invert,
downstream invert, barrels, 12-character name, 0, blank; then the barrel stations in
8-character fields; 'Conn Culv Bottom n='). The rise / span order was read from the geometry
HDF that HEC-RAS 6.3 wrote for Beaver_LS_SAConn (rise 6, span 20 for '2,6,20,...').
No HEC-RAS was available to open the files."""
import datetime
import hashlib
import json
import os
import shutil
import tempfile
import types

import numpy as np

from nd_breach_runner.core import breach as br, hecras, hecras_model as hm
from nd_breach_runner.core.units import FT
from nd_breach_runner.tests.registry import test

NOW = datetime.datetime(2026, 1, 2, 3, 4, 5)


# ---------------------------------------------------------------------- a road in a valley
def _road_ft(xy):
    """Ground in feet: a valley running east (+x) falling 1 ft in 1000, sides rising 2 ft in
    100, crossed at x = 1000 by a road embankment 15 ft above the bed that climbs 0.4 ft in
    100 each way along the road (y)."""
    xy = np.asarray(xy, dtype=float).reshape(-1, 2)
    x, y = xy[:, 0], xy[:, 1]
    valley = 1800.0 + 0.02 * np.abs(y) - 0.001 * x
    road = 1815.0 + 0.004 * np.abs(y) - np.clip(np.abs(x - 1000.0) - 15.0, 0.0, None) * 0.5
    return np.maximum(valley, road)


def _ground(units):
    """The same ground in the units of `units` (coordinates and elevations)."""
    k = units.L / FT                       # feet per project unit
    return lambda xy: _road_ft(np.asarray(xy, dtype=float) * k) / k


PER_FT = [(0.0, -3000.0), (6000.0, -3000.0), (6000.0, 3000.0), (0.0, 3000.0)]


def _place(units, s, line_ft, neighbours=(), avoid=(), cell_ft=50.0):
    k = units.L / FT
    per = [(x / k, y / k) for x, y in PER_FT]
    return hm.structure_connection(s, np.asarray(line_ft, float) / k, _ground(units), per,
                                   cell_ft / k, 3.0 / k, units, "", avoid, neighbours)


def _culvert(shape="box", barrels=2, w_ft=6.0, h_ft=4.0, inv_ft=(1798.5, 1798.0), name="Road 12"):
    from nd_breach_runner.core import structures as sm
    line = np.array([[960.0, 10.0], [1040.0, 10.0]]) * FT          # metres, as the Runner has it
    return sm.Structure("culvert", line, w_ft * FT, h_ft * FT, barrels, shape,
                        None if inv_ft[0] is None else inv_ft[0] * FT,
                        None if inv_ft[1] is None else inv_ft[1] * FT, name=name)


CULV_LINE_FT = [(960.0, 10.0), (1040.0, 10.0)]


def _fields(line):
    return line.split("=", 1)[1].split(",")


def _block(c):
    c = dict(c)
    c["name"] = c["name"] or "Test"
    return hm.structure_block(c, NOW)


def _check_geometry(c, units):
    """The connection line crosses the culvert line at its crest, at right angles, drawn left
    to right looking downstream (+x), and its weir is the ground along it."""
    k = units.L / FT
    A, B = np.array(c["line"][0]), np.array(c["line"][1])
    assert abs(A[0] - B[0]) < 1e-6 and A[1] > B[1]               # across the flow, left first
    assert abs(A[0] * k - 1000.0) < 2.0, A                        # on the road crest
    w = np.array(c["weir"])
    L = np.hypot(*(B - A))
    assert w[0, 0] == 0 and abs(w[-1, 0] - L) < 0.02 and (np.diff(w[:, 0]) >= 0).all()
    st = np.linspace(0, L, 50)
    z = _ground(units)(A[None, :] + (B - A)[None, :] * (st / L)[:, None])
    assert np.abs(np.interp(st, w[:, 0], w[:, 1]) - z).max() < 0.2 / k + 0.02
    # it runs along the road until the ground is 10 ft above the road at the culvert, or
    # 1000 ft: here the road climbs 0.4 ft in 100, so 1000 ft each way
    assert abs(L * k - 2000.0) < 4.0, L * k


# ---------------------------------------------------------------------- culvert text
@test
def hecras_structures_box_culvert_block_in_feet():
    u = hm.Units(False)
    c, why = _place(u, _culvert(), CULV_LINE_FT)
    assert why is None, why
    _check_geometry(c, u)
    L = _block(c)
    assert L[0].startswith("Connection=Test            ,")
    assert "Connection Up SA=Downstream      " in L and "Connection Dn SA=Downstream      " in L
    assert "Conn Routing Type= 1 " in L and "Conn OverFlow Method 2D=False" in L  # 3.26.4
    assert "Conn Weir Coef=2.6" in L and L[-1] == "Conn Outlet Rating Curve= 0 ,False,,"
    culv = next(ln for ln in L if ln.startswith("Connection Culv="))
    # box (2), rise 4, span 6, 80 ft long, n, Ke 0.4, Kx 1, chart 8 scale 1, inverts, 2 barrels
    want = "Connection Culv=2,4,6,80,0.013,0.4,1,8,1,1798.5,1798, 2 ,Culvert #1  , 0 ,"
    assert culv == want, culv
    f = _fields(culv)
    assert len(f) == 15 and len(f[12]) == 12
    i = L.index(culv)
    st = L[i + 1]
    assert "=" not in st and len(st) == 32
    v = [float(st[j:j + 8]) for j in range(0, 32, 8)]
    sc = c["line"][0][1] - c["crest"][1]                       # station of the crest point
    assert v[0] == v[1] and v[2] == v[3] and abs(0.5 * (v[0] + v[2]) - sc) < 0.01
    assert abs(v[2] - v[0] - 6.6) < 0.011                       # 1.1 x the span apart
    assert L[i + 2] == "Conn Culv Bottom n=0.013"
    # weir: fixed-width pairs, as many as the count says
    j = next(k for k, ln in enumerate(L) if ln.startswith("Conn Weir SE="))
    n = int(L[j].split("=")[1])
    vals = []
    for ln in L[j + 1:]:
        if ln.startswith("Connection Culv="):
            break
        vals += [float(ln[k:k + 8]) for k in range(0, len(ln), 8)]
    assert len(vals) == 2 * n == 2 * len(c["weir"])


@test
def hecras_structures_pipe_culvert_block_in_feet():
    u = hm.Units(False)
    c, why = _place(u, _culvert("pipe", 1, 3.0, 3.0, (None, None)), CULV_LINE_FT)
    assert why is None, why
    _check_geometry(c, u)
    culv = next(ln for ln in _block(c) if ln.startswith("Connection Culv="))
    # circular (1), diameter 3 in the rise field, span blank (as Diamond.g01 '1,5,,50,...'),
    # concrete pipe chart 1 scale 1, Ke 0.5; inverts from the ground at the line's ends
    zu, zd = _road_ft([CULV_LINE_FT[0], CULV_LINE_FT[1]])
    assert culv == "Connection Culv=1,3,,80,0.013,0.5,1,1,1,{},{}, 1 ,Culvert #1  , 0 ,".format(
        hm.num(zu, 2), hm.num(zd, 2)), culv
    assert c["culvert"]["inverts_from"] == "terrain / terrain"
    i = _block(c).index(culv)
    assert len(_block(c)[i + 1]) == 16


@test
def hecras_structures_culverts_in_metres():
    u = hm.Units(True)
    c, why = _place(u, _culvert(), CULV_LINE_FT)
    assert why is None, why
    _check_geometry(c, u)
    L = _block(c)
    culv = next(ln for ln in L if ln.startswith("Connection Culv="))
    f = _fields(culv)
    assert f[:3] == ["2", "1.219", "1.829"], f                  # rise 4 ft, span 6 ft in m
    assert f[3] == "24.38" and f[7:9] == ["8", "1"]
    assert abs(float(f[9]) - 1798.5 * FT) < 0.006 and abs(float(f[10]) - 1798.0 * FT) < 0.006
    assert "Conn Weir Coef=1.435" in L                          # 2.6 ft^0.5/s in SI
    c2, why = _place(u, _culvert("pipe", 3, 3.0, 3.0), CULV_LINE_FT)
    f = _fields(next(ln for ln in _block(c2) if ln.startswith("Connection Culv=")))
    assert f[:3] == ["1", "0.914", ""] and f[11] == " 3 ", f
    st = _block(c2)[[k for k, ln in enumerate(_block(c2))
                     if ln.startswith("Connection Culv=")][0] + 1]
    assert len(st) == 48                                        # 3 barrels, 6 stations


@test
def hecras_structures_bridge_is_a_gap_in_the_weir():
    from nd_breach_runner.core import structures as sm
    u = hm.Units(False)
    s = sm.Structure("bridge", np.array(CULV_LINE_FT) * FT, 40 * FT, 10 * FT, 1, "box",
                     1799.0 * FT, None, name="Bridge A")
    c, why = _place(u, s, CULV_LINE_FT)
    assert why is None, why
    L = _block(c)
    assert "Conn Routing Type= 1 " in L and not any(ln.startswith("Connection Culv") for ln in L)
    w = np.array(c["weir"])
    gap = w[w[:, 1] < 1810.0]
    floor = c["gap"]["floor"]
    # floor: from the upstream invert (the run's) to the lowest ground near the downstream end
    zd = float(np.min(_road_ft([(x, 10.0) for x in np.arange(1040.0, 999.0, -3.0)
                                if x >= 1040.0 - 40.0])))
    assert abs(floor - (1799.0 + (zd - 1799.0) * 0.5)) < 0.1, (floor, zd)
    assert np.allclose(gap[:, 1], floor) and abs(gap[-1, 0] - gap[0, 0] - 40.0) < 0.02
    assert c["gap"]["inverts_from"] == "run / terrain"


# ---------------------------------------------------------------------- left out
@test
def hecras_structures_outside_or_without_size_are_left_out():
    from nd_breach_runner.core import structures as sm
    u = hm.Units(False)
    up = [(-200.0, 10.0), (-120.0, 10.0)]                       # upstream of the 2D area
    c, why = _place(u, _culvert(), up)
    assert c is None and why == "outside the downstream 2D area", why
    s = _culvert()
    s.width_m = None
    assert _place(u, s, CULV_LINE_FT) == (None, "no size (span / rise)")
    s = sm.Structure("culvert", np.zeros((1, 2)), 1.0, 1.0)
    assert _place(u, s, [(1000.0, 0.0)])[1] == "needs a line with two ends"
    # too close to the edge of the 2D area for a line across the road
    edge = [(960.0, 2980.0), (1040.0, 2980.0)]
    assert _place(u, _culvert(), edge)[1].startswith("too close to the edge of the 2D area")
    # two culverts 20 ft apart on one road: no room for a connection each
    c1, _ = _place(u, _culvert(), CULV_LINE_FT)
    other = [(960.0, 30.0), (1040.0, 30.0)]
    c2, why = _place(u, _culvert(name="Twin"), other, neighbours=[("Road 12", c1["crest"])])
    assert c2 is None and why.startswith("too close to Road 12"), why
    # far enough apart: each gets its own, and the lines stay apart
    far = [(960.0, 400.0), (1040.0, 400.0)]
    a, _ = _place(u, _culvert(), CULV_LINE_FT, neighbours=[("B", (1000.0, 400.0))])
    b, why = _place(u, _culvert(name="B"), far, neighbours=[("Road 12", a["crest"])],
                    avoid=[("Road 12", a["line"])])
    assert why is None and a["line"][0][1] < b["line"][1][1], (a["line"], b["line"])


@test
def hecras_structures_names_are_unique_hecras_names():
    used = {hm.CONN_NAME}
    assert hm.connection_name("Dam", used) == "Dam 2"
    assert hm.connection_name("County Road 12, twin box = new", used) == "County Road 12 t"
    assert hm.connection_name("County Road 12, twin box = new", used) == "County Road 12 2"
    assert hm.connection_name("", used) == "Structure"
    for n in used:
        hm.name16(n)


@test
def hecras_structures_from_the_run_report():
    rep = {"settings": {"structures": [{"kind": "culvert", "line": [[0, 0], [10, 0]],
                                        "width_m": 1.0, "height_m": 1.0, "barrels": 1,
                                        "shape": "pipe", "invert_up_m": 5.0,
                                        "invert_dn_m": None, "manning": 0.012,
                                        "losses": 1.0, "name": "a"}]}}
    ss, src = hm.run_structures(rep)
    assert len(ss) == 1 and ss[0].invert_up_m == 5.0 and "given to the run" in src
    rep["structures_used"] = [dict(rep["settings"]["structures"][0], invert_up_m=4.0)]
    ss, src = hm.run_structures(rep)
    assert ss[0].invert_up_m == 4.0 and ss[0].manning == 0.012 and "as the run used" in src
    assert hm.run_structures({}) == ([], "")


# ---------------------------------------------------------------------- nothing changes
def _hand_model(si):
    """A model filled by hand (no GDAL), as build_model fills it."""
    m = hm.Model()
    m.name, m.title, m.units = "ND_T1", "ND-T1 breach", hm.Units(si)
    m.perimeter = [(0.0, 0.0), (5000.0, 0.0), (5000.0, 3000.0), (0.0, 3000.0)]
    m.points = np.array([[x, y] for x in range(250, 5000, 500) for y in range(250, 3000, 500)],
                        dtype=float)
    m.cell = 500.0
    m.reservoir = [(-3000.0, 500.0), (-100.0, 500.0), (-100.0, 2500.0), (-3000.0, 2500.0)]
    m.elev_volume = [(1825.0, 0.0), (1840.0, 120.0), (1850.0, 500.0), (1855.0, 812.5)]
    m.conn_line = [(0.0, 2500.0), (0.0, 1500.0), (0.0, 500.0)]
    m.weir = [(0.0, 1855.0), (1000.0, 1850.0), (2000.0, 1855.0)]
    m.bc_line = [(5000.0, 1000.0), (5000.0, 1500.0), (5000.0, 2000.0)]
    m.bc_slope = 0.0012
    m.initial_wse = 1850.0
    m.breach = {"center_station": 1000.0, "bottom_width": 100.0, "bottom_elev": 1825.0,
                "left_slope": 0.5, "right_slope": 0.5, "piping": True, "piping_coef": 0.5,
                "piping_elev": 1837.5, "formation_hr": 1.5, "weir_coef": 3.1,
                "trigger_wse": 1849.99}
    m.terrain_name = "ND_T1 Terrain"
    m.description = ["Built by hand for the tests."]
    return m


def _hand_texts(si):
    m = _hand_model(si)
    out = [hm.project_text(m), hm.geometry_text(m, now=NOW), hm.unsteady_text(m),
           hm.plan_text(m), hm.rasmap_text(m)]
    d = tempfile.mkdtemp()
    try:
        dam = br.DamData(dam_id="ND-T1", dam_height_ft=25.0, storage_top_acft=500.0,
                         top_of_dam_ft=1850.0, surface_area_top_ac=60.0)
        info = {"crs": "EPSG:2266", "crs_why": "chosen", "units": m.units.label,
                "len_name": m.units.len_name, "cell": 500.0, "cell_chosen": True, "cells": 60,
                "area": 1.5e7, "dam_line": 2000.0, "terrain_cell": 10.0, "dem_z": "feet",
                "comp_interval": "5SEC", "sim_hours": 24.0, "bc_slope": 0.0012,
                "bc_slope_how": "by hand", "centre_how": "by hand", "initial_wse": 1850.0,
                "breach": m.breach, "side_buffer": 1000.0, "end_buffer": 2640.0,
                "weir_points": 3, "storage_exponent": 2.5, "manning_n": 0.06,
                "equation": "Shallow water equations (SWE-ELM)", "scenario": "sunny",
                "edits": None, "dem": "dem.tif", "mesh_check": None}
        p = hm.write_readme(os.path.join(d, "README_HECRAS.md"), m, info, ["a note"], dam,
                            {"peak_cfs": 12345.0})
        with open(p, encoding="utf-8") as f:
            out.append(f.read())
    finally:
        shutil.rmtree(d)
    return [hashlib.sha256(t.encode("utf-8")).hexdigest()[:16] for t in out]


# SHA-256 (first 16 hex digits) of the project, geometry, unsteady, plan, RAS Mapper and
# README text of the hand-made model, written by the 3.11.0 code (before structures)
# the plan text (4th) changed in 3.26.4: the adaptive (Courant) time step lines
GOLDEN_3110 = {
    False: ["db1469db6a372a6f", "95802a0a6d74fd27", "3d4a44bc8d7c130b", "0df1f610d3c0a923",
            "97664151919e2834", "f88edff79c7eab1c"],
    True: ["3b1f1ea3c0eb48b8", "95802a0a6d74fd27", "3d4a44bc8d7c130b", "25cbda86d08a2fb5",
           "3a09dd23f63da7fc", "34d7cb5cd4f1e867"]}


@test
def hecras_structures_none_leaves_the_files_unchanged():
    for si in (False, True):
        assert _hand_texts(si) == GOLDEN_3110[si], (si, _hand_texts(si))
    # and an empty structure list in the model or the README info adds nothing
    m = _hand_model(False)
    m.structures = []
    assert hm.geometry_text(m, now=NOW) == hm.geometry_text(_hand_model(False), now=NOW)
    assert hm.structures_readme(m, {"source": "", "exported": [], "skipped": []}) == []


# ---------------------------------------------------------------------- the whole model
ROAD_X = 2000.0                    # a road across the valley 2 km below the dam (run metres)


def _road_dem(dem):
    """Adds a road embankment across the synthetic valley of test_hecras_model.make_run."""
    from osgeo import gdal
    ds = gdal.Open(dem, gdal.GA_Update)
    gt = ds.GetGeoTransform()
    a = ds.GetRasterBand(1).ReadAsArray().astype(float)
    from nd_breach_runner.tests.test_hecras_model import X0, Y0
    from nd_breach_runner.tests.synthetic import TOP_M, H_M
    xs = gt[0] + (np.arange(a.shape[1]) + 0.5) * gt[1] - X0
    ys = gt[3] + (np.arange(a.shape[0]) + 0.5) * gt[5] - Y0
    X, Y = np.meshgrid(xs, ys)
    crest = TOP_M - H_M - 0.001 * ROAD_X + 4.0 + 0.002 * np.abs(Y)
    road = crest - np.clip(np.abs(X - ROAD_X) - 5.0, 0.0, None) * 0.5
    ok = a > -9000
    a[ok] = np.maximum(a, road)[ok]
    ds.GetRasterBand(1).WriteArray(a.astype(np.float32))
    ds = None


def _with_structures(dfold, key="structures_used"):
    """Puts a box culvert and a pipe on the road, a notch below it and a culvert in the
    reservoir (outside the 2D area) into the run report, in the run's CRS (UTM metres)."""
    from nd_breach_runner.tests.test_hecras_model import X0, Y0
    from nd_breach_runner.tests.synthetic import TOP_M, H_M
    bed = TOP_M - H_M - 0.001 * ROAD_X

    def line(x0, x1, y):
        return [[X0 + x0, Y0 + y], [X0 + x1, Y0 + y]]
    ss = [{"kind": "culvert", "line": line(ROAD_X - 15, ROAD_X + 15, 0.0), "width_m": 6 * FT,
           "height_m": 4 * FT, "barrels": 2, "shape": "box", "invert_up_m": bed - 0.3,
           "invert_dn_m": bed - 0.35, "manning": 0.013, "losses": 1.0, "name": "CR 12 box"},
          {"kind": "culvert", "line": line(ROAD_X - 15, ROAD_X + 15, 300.0), "width_m": 3 * FT,
           "height_m": 3 * FT, "barrels": 1, "shape": "pipe", "invert_up_m": None,
           "invert_dn_m": None, "manning": 0.024, "losses": 1.0, "name": "CR 12 relief"},
          {"kind": "notch", "line": line(3000.0, 3040.0, 0.0), "width_m": 20.0,
           "height_m": 3.0, "barrels": 1, "shape": "box", "invert_up_m": None,
           "invert_dn_m": None, "manning": 0.013, "losses": 1.0, "name": "Old grade"},
          {"kind": "culvert", "line": line(-600.0, -560.0, 0.0), "width_m": 1.0,
           "height_m": 1.0, "barrels": 1, "shape": "pipe", "invert_up_m": None,
           "invert_dn_m": None, "manning": 0.013, "losses": 1.0, "name": "In the pool"}]
    p = os.path.join(dfold, hecras.MODEL_FOLDER, hecras.RUN_REPORT_JSON)
    with open(p) as f:
        rep = json.load(f)
    if key == "settings":
        rep["settings"]["structures"] = ss
    else:
        rep[key] = ss
    with open(p, "w") as f:
        json.dump(rep, f)
    return ss


class _Frozen(datetime.datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 1, 2, 3, 4, 5)


def _frozen_build(dfold, out, **kw):
    """build_model with the clock stopped (the geometry records when it was written)."""
    real = hm.datetime
    hm.datetime = types.SimpleNamespace(datetime=_Frozen, timedelta=datetime.timedelta)
    try:
        return hm.build_model(dfold, out, **kw)
    finally:
        hm.datetime = real


def _read(p):
    with open(p, "rb") as f:
        return f.read()


@test
def hecras_structures_in_the_built_model():
    from nd_breach_runner.tests.test_hecras_model import _has_gdal, make_run, _count_block
    if not _has_gdal():
        print("  (skipped: no GDAL)")
        return
    d = tempfile.mkdtemp()
    try:
        dfold, dem, dam, hy = make_run(d)
        _road_dem(dem)
        plain = _frozen_build(dfold, os.path.join(d, "plain"))
        ss = _with_structures(dfold)
        r = _frozen_build(dfold, os.path.join(d, "with"))
        m, si = r["model"], r["info"]["structures"]
        names = [c["source_name"] for c in si["exported"]]
        assert names == ["CR 12 box", "CR 12 relief", "Old grade"], (names, si["skipped"])
        assert [(x["name"], x["why"]) for x in si["skipped"]] == [
            ("In the pool", "outside the downstream 2D area")]
        assert "as the run used it" in si["source"]
        with open(r["paths"]["g01"], newline="") as f:
            g = f.read()
        lines = g.split("\r\n")
        # every structure block sits between the dam connection and the boundary line
        i_dam = lines.index(next(ln for ln in lines if ln.startswith("Connection=Dam ")))
        i_bc = lines.index(next(ln for ln in lines if ln.startswith("BC Line Name=")))
        conns = [k for k, ln in enumerate(lines) if ln.startswith("Connection=")]
        assert len(conns) == 4 and all(i_dam <= k < i_bc for k in conns)
        assert sum(ln == "Connection Up SA=Downstream      " for ln in lines) == 3
        culv = [ln for ln in lines if ln.startswith("Connection Culv=")]
        assert len(culv) == 2
        f = _fields(culv[0])
        assert f[:3] == ["2", "4", "6"] and f[7:9] == ["8", "1"] and f[11] == " 2 ", f
        # inverts as the run used them, in feet (the model is ND State Plane, feet)
        assert abs(float(f[9]) - ss[0]["invert_up_m"] / FT) < 0.006
        f = _fields(culv[1])
        assert f[:3] == ["1", "3", ""] and f[4] == "0.024", f
        # the structure blocks read back like the dam's: line and weir fields
        for c in m.structures:
            k = lines.index(next(ln for ln in lines if ln.startswith(
                "Connection=" + hm.name16(c["name"]))))
            blk = lines[k:]
            cl, _ = _count_block(blk, "Connection Line=", 4, 16)
            assert np.allclose(cl, c["line"], atol=1e-3)
            w, _ = _count_block(blk, "Conn Weir SE=", 10, 8)
            assert np.allclose(w, c["weir"], atol=0.01) and len(w) <= 500
            # inside the 2D area, crossing the structure line near its crest
            poly = hm._polygon(m.perimeter)
            for q in c["line"]:
                assert poly.Contains(hm._point(*q))
            sl = np.asarray(c["structure_line"])
            a = hm._line(c["line"]).Intersection(hm._line(sl))
            assert not a.IsEmpty(), c["name"]
        # the culvert's connection line is on the road crest (2000 m below the dam)
        from osgeo import osr
        ct = osr.CoordinateTransformation(*[_srs(e) for e in (26914, 2266)])
        from nd_breach_runner.tests.test_hecras_model import X0, Y0
        rx = np.array(ct.TransformPoint(X0 + ROAD_X, Y0)[:2])
        c0 = m.structures[0]
        assert hm.point_segment_distance(np.array([rx[0]]), np.array([rx[1]]),
                                         c0["line"])[0] < 15.0
        # the weir follows the road: lowest at the box culvert, about 4 m above the bed
        w = np.array(c0["weir"])
        bed_ft = (ss[0]["invert_up_m"] + 0.3) / FT
        assert abs(w[:, 1].min() - (bed_ft + 4.0 / FT)) < 1.0, (w[:, 1].min(), bed_ft)
        # the two culverts on one road do not share faces
        l0, l1 = (hm._line(c["line"]) for c in m.structures[:2])
        assert l0.Distance(l1) >= m.cell
        # the notch: an open gap down to the ground, in a weir with routing type 1
        notch = m.structures[2]
        assert notch["gap"] and notch["culvert"] is None
        # README and layout
        with open(r["paths"]["readme"], encoding="utf-8") as fh:
            rd = fh.read()
        assert "## Culverts, bridges and notches" in rd and "NOT been opened in HEC-RAS" in rd
        assert "- In the pool (culvert): outside the downstream 2D area." in rd
        assert "| CR 12 box | culvert `CR 12 box` | culvert group: box (shape 2)" in rd
        from osgeo import ogr
        ds = ogr.Open(r["paths"]["layout"])
        assert ds.GetLayerByName("Structure connections").GetFeatureCount() == 3
        assert ds.GetLayerByName("Structures from the run").GetFeatureCount() == 4
        ds = None
        # without structures (switched off) the files are the plain model's, byte for byte
        r2 = _frozen_build(dfold, os.path.join(d, "off"), options={"structures": False})
        for k in ("prj", "g01", "u01", "p01", "rasmap", "readme"):
            assert _read(r2["paths"][k]) == _read(plain["paths"][k]), k
        assert r2["info"]["structures"] == {"source": "", "exported": [], "skipped": []}
        ds = ogr.Open(r2["paths"]["layout"])
        assert ds.GetLayerByName("Structure connections") is None
        ds = None
    finally:
        shutil.rmtree(d)


def _srs(epsg):
    from osgeo import osr
    s = osr.SpatialReference()
    s.ImportFromEPSG(epsg)
    s.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    return s


@test
def hecras_structures_older_run_and_si_model():
    """A run before 3.12.0 has its structures only in settings (inverts as given); an SI
    model writes them in metres."""
    from nd_breach_runner.tests.test_hecras_model import _has_gdal, make_run
    if not _has_gdal():
        print("  (skipped: no GDAL)")
        return
    d = tempfile.mkdtemp()
    try:
        dfold, dem, dam, hy = make_run(d)
        _road_dem(dem)
        ss = _with_structures(dfold, key="settings")
        r = hm.build_model(dfold, os.path.join(d, "si"), crs="EPSG:26914")
        si = r["info"]["structures"]
        assert "given to the run" in si["source"] and len(si["exported"]) == 3
        cv = si["exported"][0]["culvert"]
        assert abs(cv["rise"] - 4 * FT) < 1e-9 and abs(cv["span"] - 6 * FT) < 1e-9
        assert abs(cv["inv_up"] - ss[0]["invert_up_m"]) < 0.006
        assert abs(cv["length"] - 30.0) < 0.01
        with open(r["paths"]["g01"]) as f:
            g = f.read()
        assert "Connection Culv=2,1.219,1.829,30," in g
    finally:
        shutil.rmtree(d)


@test
def hecras_structures_algorithm_reports_them():
    from nd_breach_runner.tests.test_hecras_model import _has_gdal, make_run
    if not _has_gdal():
        print("  (skipped: no GDAL)")
        return
    from nd_breach_runner.tests.test_hecras import _algs
    h, fq = _algs()
    d = tempfile.mkdtemp()
    try:
        dfold, dem, dam, hy = make_run(d)
        _road_dem(dem)
        _with_structures(dfold)
        a = h.HecRasModelAlgorithm()
        a.initAlgorithm()
        assert "culverts, bridges" in a.shortHelpString()
        ctx = fq.QgsProcessingContext()
        fb = fq.QgsProcessingFeedback()
        r = a.processAlgorithm({"DAM_FOLDER": dfold, "CELL_FT": 75.0}, ctx, fb)
        assert "3 culvert / bridge connection(s) in the 2D area" in r["RESULT"], r["RESULT"]
        r = a.processAlgorithm({"DAM_FOLDER": dfold, "CELL_FT": 75.0, "STRUCTURES": False},
                               fq.QgsProcessingContext(), fq.QgsProcessingFeedback())
        assert "connection" not in r["RESULT"]
        with open(r["PROJECT"][:-4] + ".g01") as f:
            assert "Connection Culv=" not in f.read()
    finally:
        shutil.rmtree(d)
