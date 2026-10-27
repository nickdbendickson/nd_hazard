"""Tests of the HEC-RAS model builder (core/hecras_model.py).

The text writers are checked against the layout of HEC's own example project
(BaldEagleCrkMulti2D, geometry 'SA to 2D Connection', plan 'SA to 2D Dam Break Run'):
keywords, fixed-width fields and counts. The GDAL part builds a model from a synthetic
finished run whose DEM, flood raster and model CRS are three different coordinate systems.
No HEC-RAS was available to open the files."""
import json
import math
import os
import shutil
import tempfile

import numpy as np

from nd_breach_runner.core import breach as br, hecras, hecras_model as hm
from nd_breach_runner.core.units import ACFT, CFS, FT
from nd_breach_runner.tests.registry import test
from nd_breach_runner.tests.synthetic import valley, TOP_M, H_M

X0, Y0 = 420000.0, 5200000.0          # UTM 14N: about 98.0 W, 46.9 N (North Dakota)


def _has_gdal():
    try:
        from osgeo import gdal, ogr, osr  # noqa: F401
        return True
    except ImportError:
        return False


def _dam(**k):
    d = dict(dam_id="ND-T1", dam_height_ft=25.0, storage_top_acft=500.0, top_of_dam_ft=1850.0,
             surface_area_top_ac=60.0, crest_length_ft=2600.0, crest_width_ft=15.0,
             ds_slope_h=3.0)
    d.update(k)
    return br.DamData(**d)


def _tif(path, a, x0, ytop, cell, epsg, unit=None):
    from osgeo import gdal, osr
    ds = gdal.GetDriverByName("GTiff").Create(path, a.shape[1], a.shape[0], 1, gdal.GDT_Float32)
    ds.SetGeoTransform((x0, cell, 0, ytop, 0, -cell))
    s = osr.SpatialReference()
    s.ImportFromEPSG(epsg)
    ds.SetProjection(s.ExportToWkt())
    b = ds.GetRasterBand(1)
    b.SetNoDataValue(-9999)
    if unit:
        b.SetUnitType(unit)
    b.WriteArray(np.where(np.isfinite(a), a, -9999).astype(np.float32))
    ds = None


def make_run(folder, dem_feet=False, flood_crs=5070, scenario="sunny", method=br.METHOD_PIPING,
             rot_deg=0.0, hills=False, zfn=None, dam_kw=None):
    """A finished run: dam folder with Depth (Max).tif, _breach_model/run_report.json and
    breach_hydrograph.csv, and a DEM in UTM 14N. `rot_deg` turns the valley (flow towards
    that bearing from east, counter-clockwise); `hills` puts ground above the top of dam
    beyond 400 m from the valley axis, as abutments. `zfn` replaces the terrain
    (synthetic.valley) and `dam_kw` changes the dam's data (_dam)."""
    from osgeo import gdal
    cell = 10.0
    xs = np.arange(-6000, 9000, cell) + cell / 2
    ys = np.arange(6000, -6000, -cell) - cell / 2
    Xg, Yg = np.meshgrid(xs, ys)
    t = math.radians(rot_deg)
    X = Xg * math.cos(t) + Yg * math.sin(t)          # along the valley
    Y = -Xg * math.sin(t) + Yg * math.cos(t)         # across it
    z = (zfn or valley)(X, Y)
    if hills:
        z = z + np.where(np.abs(Y) > 400, (np.abs(Y) - 400) * 0.08, 0.0)
    dem = os.path.join(folder, "dem.tif")
    _tif(dem, z / FT if dem_feet else z, X0 - 6000, Y0 + 6000, cell, 26914,
         "ft" if dem_feet else "metre")
    # a flood that fades out 6 km below the dam
    bed = TOP_M - H_M - 0.001 * np.clip(X, 0, None)
    wse = bed + 3.5 * np.exp(-np.clip(X, 0, None) / 2500.0) + 0.6
    dep = np.where((X > 40) & (X < 6000), wse - z, np.nan)
    dep = np.where(dep > 0.003, dep / FT, np.nan)
    tmp = os.path.join(folder, "dep_utm.tif")
    _tif(tmp, dep, X0 - 6000, Y0 + 6000, cell, 26914)
    os.makedirs(os.path.join(folder, "DAM"), exist_ok=True)
    dfold = os.path.join(folder, "DAM")
    from osgeo import osr
    s = osr.SpatialReference()
    s.ImportFromEPSG(flood_crs)
    gdal.Warp(os.path.join(dfold, "Depth (Max).tif"), tmp,
              options=gdal.WarpOptions(dstSRS=s.ExportToWkt(), xRes=10, yRes=10,
                                       srcNodata=-9999, dstNodata=-9999))
    dam = _dam(**(dam_kw or {}))
    hy = br.make_hydrograph(dam, method)
    md = os.path.join(dfold, hecras.MODEL_FOLDER)
    os.makedirs(md, exist_ok=True)
    hy.to_csv(os.path.join(md, hecras.HYDROGRAPH_CSV))
    params = dict(hy.params)
    params["peak_cfs"] = hy.peak_cfs
    params["method"] = hy.method
    report = {"dam": br.dam_to_dict(dam), "dam_xy": [X0, Y0], "scenario": scenario,
              "settings": {"cell_size_m": 10.0, "manning_n": 0.06,
                           "input_files": {"DEM": dem}, "breach_xy": None},
              "inflow_point": {"xy": [X0 + 120.0 * math.cos(t), Y0 + 120.0 * math.sin(t)]},
              "hydrograph": params,
              "dem_elevation_units": "feet (raster unit 'ft')" if dem_feet else
              "metres (raster unit 'metre')",
              "work_crs": "EPSG:26914",
              "summary": {"latest_arrival_hr": 3.0, "max_velocity_fps": 8.0,
                          "max_depth_ft": 12.0}}
    with open(os.path.join(md, hecras.RUN_REPORT_JSON), "w") as f:
        json.dump(report, f)
    return dfold, dem, dam, hy


def _check_model(m, P, flow_dir):
    """Geometry rules every model must meet."""
    A, B = np.array(m.conn_line[0]), np.array(m.conn_line[-1])
    L = np.hypot(*(B - A))
    # the dam line goes through the dam point, across the flow, left to right looking down
    assert abs(np.cross(B - A, P - A)) / L < 1.0
    u = (B - A) / L
    assert abs(np.dot(u, flow_dir)) < 1e-6
    assert np.cross(flow_dir, u) < 0           # B is to the right of the flow
    per = list(m.perimeter) + [m.perimeter[0]]
    assert hm.ring_area(m.perimeter) > 0
    for q in m.conn_line:
        assert hm.point_segment_distance(np.array([q[0]]), np.array([q[1]]), per)[0] < 0.5
    # 3.26.6: the BC line runs a quarter cell outside the perimeter (HEC-RAS refuses it on it)
    for q in m.bc_line:
        dq = hm.point_segment_distance(np.array([q[0]]), np.array([q[1]]), per)[0]
        assert 0.5 * hm.BC_OFFSET_CELLS * m.cell < dq < 2.0 * hm.BC_OFFSET_CELLS * m.cell, dq
    poly = hm._polygon(m.perimeter)
    assert poly.IsValid()
    for x, y in m.points[::53]:
        assert poly.Contains(hm._point(x, y))
    # nothing of the 2D area upstream of the dam line near the dam
    pa = np.asarray(m.perimeter)
    s = (pa - P) @ flow_dir
    lat = np.abs((pa - P) @ u)
    assert (s[lat < 0.45 * L] > -1.0).all()
    # the reservoir is upstream
    r = np.asarray(m.reservoir)
    assert ((r - P) @ flow_dir).mean() < 0
    w = np.asarray(m.weir)
    assert (np.diff(w[:, 0]) > 0).all() and len(w) <= 500
    seg = np.asarray(m.perimeter)
    assert len(m.perimeter) >= 4
    b = m.breach
    half = 0.5 * b["bottom_width"]
    assert half <= b["center_station"] <= L - half


def _blocks(text):
    return text.split("\r\n")


def _count_block(lines, key, per_line, width):
    """Values after 'key= N' (N pairs), read as fixed-width fields."""
    i = next(k for k, ln in enumerate(lines) if ln.startswith(key))
    n = int(lines[i].split("=")[1].split(",")[0])
    vals = []
    j = i + 1
    while len(vals) < 2 * n:
        ln = lines[j]
        assert "=" not in ln, (key, ln)
        assert len(ln.rstrip()) <= per_line * width, (key, ln)
        vals += [float(ln[k:k + width]) for k in range(0, len(ln.rstrip()), width)]
        j += 1
    assert len(vals) == 2 * n, (key, len(vals), n)
    return np.asarray(vals).reshape(-1, 2), j


# ---------------------------------------------------------------------- formatting
@test
def hecras_model_fixed_width_fields():
    assert hm.f8(1834.256) == " 1834.26" and len(hm.f8(123456.789)) == 8
    s = hm.f16(2009315.70791633)
    assert len(s) == 16 and s[0] == " " and abs(float(s) - 2009315.70791633) < 1e-6
    assert len(hm.f16(-123.5)) == 16
    assert hm.comp_interval(7.3) == (6, "6SEC")
    assert hm.comp_interval(0.05) == (0.1, "0.1SEC")
    assert hm.comp_interval(5000) == (600, "10MIN")
    assert hm.nice_cell(37) == 40 and hm.nice_cell(3) == 5
    assert hm.ras_date(26.5) == ("02JAN2000", "0230")
    assert hm.safe_name("ND-0123 Big dam!") == "ND_0123_Big_dam"
    try:
        hm.name16("x" * 17)
        assert False
    except hm.HecRasModelError:
        pass


@test
def hecras_model_units_follow_the_crs():
    us, si = hm.Units(False), hm.Units(True)
    assert us.label == "English Units" and si.label == "SI Units"
    assert abs(us.volume(ACFT) - 1.0) < 1e-12 and abs(si.volume(1000.0) - 1.0) < 1e-12
    assert abs(us.flow(CFS) - 1.0) < 1e-12 and si.flow(2.0) == 2.0
    assert abs(us.from_ft(10.0) - 10.0) < 1e-12 and abs(si.from_ft(10.0) - 3.048) < 1e-12
    assert abs(si.weir_coef(3.1) - 3.1 * math.sqrt(0.3048)) < 1e-12 and us.weir_coef(2.6) == 2.6


@test
def hecras_model_storage_curve_is_the_plugins():
    dam = _dam()
    ev, res = hm.storage_curve(dam, hm.Units(False))
    e = np.array([a for a, _ in ev])
    v = np.array([b for _, b in ev])
    assert (np.diff(e) > 0).all() and (np.diff(v) >= 0).all()
    assert abs(e[0] - (1850.0 - 25.0)) < 0.01 and v[0] == 0.0
    i = int(np.argmin(np.abs(e - 1850.0)))
    assert abs(e[i] - 1850.0) < 1e-6 and abs(v[i] - 500.0) < 0.01
    assert e[-1] >= 1850.0 + 5.0
    evs, _ = hm.storage_curve(dam, hm.Units(True))
    i = int(np.argmin(np.abs(np.array([a for a, _ in evs]) - 1850.0 * FT)))
    assert abs(evs[i][1] - 500.0 * ACFT / 1000.0) < 0.01


@test
def hecras_model_profile_simplify_keeps_shape():
    st = np.linspace(0, 1000, 2001)
    el = np.where((st > 400) & (st < 500), 10.0, 20.0)
    s2, e2 = hm.simplify_profile(st, el, 0.05, 450)
    assert len(s2) < 20 and e2.min() == 10.0 and s2[0] == 0 and s2[-1] == 1000
    s3, _ = hm.simplify_profile(st, np.sin(st / 3.0), 0.0001, 100)
    assert len(s3) <= 100


# ---------------------------------------------------------------------- whole model
def _build(**kw):
    d = tempfile.mkdtemp()
    run = kw.pop("run", {})
    dfold, dem, dam, hy = make_run(d, **run)
    r = hm.build_model(dfold, **kw)
    return d, dfold, r


@test
def hecras_model_builds_every_file_with_three_crs():
    if not _has_gdal():
        print("  (skipped: no GDAL)")
        return
    d, dfold, r = _build(run={"flood_crs": 5070})
    try:
        p = r["paths"]
        for k in ("prj", "g01", "u01", "p01", "rasmap", "projection", "terrain_tif", "layout",
                  "readme"):
            assert os.path.exists(p[k]), k
        info = r["info"]
        # DEM in UTM metres, flood in Albers, run in UTM: the model is ND State Plane South ft
        assert info["crs"] == "EPSG:2266", info["crs"]
        assert info["units"] == "English Units"
        with open(p["prj"], newline="") as f:
            prj = f.read()
        assert "English Units" in prj and "\r\n" in prj and "Plan File=p01" in prj
        m = r["model"]
        # the dam point sits on the dam line, in State Plane feet
        from osgeo import osr
        a, b = osr.SpatialReference(), osr.SpatialReference()
        a.ImportFromEPSG(26914)
        b.ImportFromEPSG(2266)
        for s in (a, b):
            s.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        ct = osr.CoordinateTransformation(a, b)
        P = np.array(ct.TransformPoint(X0, Y0)[:2])
        Q = np.array(ct.TransformPoint(X0 + 120.0, Y0)[:2])
        _check_model(m, P, (Q - P) / np.hypot(*(Q - P)))
        A, B = np.array(m.conn_line[0]), np.array(m.conn_line[-1])
        dist = abs(np.cross(B - A, P - A)) / np.hypot(*(B - A))
        assert dist < 1.0, dist
        # the dam spans the valley: the synthetic embankment is 800 m long, the flood plus
        # 1000 ft each side is wider than the flood
        L = np.hypot(*(B - A))
        assert 1500 < L < 9000, L
        # weir: top of dam where the ground is lower, ground where higher
        w = np.array(m.weir)
        assert abs(w[:, 1].min() - 1850.0) < 0.05, w[:, 1].min()
        assert w[0, 0] == 0 and abs(w[-1, 0] - L) < 1.0
        # all 2D points inside the perimeter and away from it
        from osgeo import ogr
        poly = hm._polygon(m.perimeter)
        for x, y in m.points[::97]:
            assert poly.Contains(hm._point(x, y))
        assert len(m.points) > 1000
        # perimeter counter-clockwise, connection on the perimeter
        assert hm.ring_area(m.perimeter) > 0
        per = list(m.perimeter) + [m.perimeter[0]]
        for q in m.conn_line:
            assert hm.point_segment_distance(np.array([q[0]]), np.array([q[1]]), per)[0] < 0.5
        for q in m.bc_line:               # 3.26.6: a quarter cell outside the perimeter
            dq = hm.point_segment_distance(np.array([q[0]]), np.array([q[1]]), per)[0]
            assert 0.5 * hm.BC_OFFSET_CELLS * m.cell < dq < 2.0 * hm.BC_OFFSET_CELLS * m.cell
        # the boundary line is at the downstream end (far east of the dam)
        bc = np.array(m.bc_line)
        assert bc[:, 0].mean() > P[0] + 3.0 * 5000 / 0.3048 / 3, (bc[:, 0].mean() - P[0])
        assert 1e-4 <= m.bc_slope <= 0.003, m.bc_slope
        # reservoir upstream (west) of the dam
        res = np.array(m.reservoir)
        assert res[:, 0].mean() < P[0]
        # breach values
        b = m.breach
        assert abs(b["bottom_elev"] - 1825.0) < 0.05
        assert b["bottom_width"] > 0 and b["piping"] is True
        assert b["piping_elev"] is not None and 1825 < b["piping_elev"] < 1850
        assert 0 < b["center_station"] < L
        assert m.initial_wse == 1850.0 and abs(b["trigger_wse"] - 1849.99) < 1e-6
        # terrain GeoTIFF: State Plane, feet, covers the 2D area
        from osgeo import gdal
        ds = gdal.Open(p["terrain_tif"])
        s = osr.SpatialReference(wkt=ds.GetProjection())
        assert s.GetAuthorityCode(None) == "2266"
        assert ds.GetRasterBand(1).GetUnitType() == "ft"
        gt = ds.GetGeoTransform()
        xmin, ymax = gt[0], gt[3]
        xmax, ymin = xmin + gt[1] * ds.RasterXSize, ymax + gt[5] * ds.RasterYSize
        pa = np.array(m.perimeter)
        assert xmin <= pa[:, 0].min() and xmax >= pa[:, 0].max()
        assert ymin <= pa[:, 1].min() and ymax >= pa[:, 1].max()
        zt = ds.GetRasterBand(1).ReadAsArray()
        zt = zt[zt > -9000]
        assert 1780 < zt.min() < 1830 and zt.max() < 1900, (zt.min(), zt.max())
        ds = None
        # projection file is ESRI WKT
        with open(p["projection"]) as f:
            txt = f.read()
        assert txt.startswith("PROJCS[") and "North_Dakota_South" in txt and "Foot_US" not in txt
    finally:
        shutil.rmtree(d)


@test
def hecras_model_geometry_text_layout():
    if not _has_gdal():
        print("  (skipped: no GDAL)")
        return
    d, dfold, r = _build()
    try:
        with open(r["paths"]["g01"], newline="") as f:
            g = f.read()
        assert "\r\n" in g and "\n" not in g.replace("\r\n", "")
        lines = _blocks(g)
        m = r["model"]
        assert lines[0].startswith("Geom Title=") and lines[1] == "Program Version=6.10"
        # 2D area: perimeter 1 point a line in 16-character fields
        i = lines.index(next(ln for ln in lines if ln.startswith("Storage Area=Downstream")))
        assert lines[i].split("=")[1].split(",")[0] == "Downstream      "
        n = int(lines[i + 1].split("=")[1])
        assert n == len(m.perimeter)
        x = float(lines[i + 2][:16])
        y = float(lines[i + 2][16:32])
        assert abs(x - m.perimeter[0][0]) < 1e-3 and abs(y - m.perimeter[0][1]) < 1e-3
        assert "Storage Area Is2D=-1" in lines
        pts, j = _count_block(lines, "Storage Area 2D Points=", 4, 16)
        assert len(pts) == len(m.points) and np.allclose(pts, m.points, atol=1e-3)
        assert lines[j].startswith("Storage Area 2D PointsPerimeterTime=")
        # reservoir: elevation-volume pairs in 8-character fields, 10 to a line
        ev, _ = _count_block(lines, "Storage Area Vol Elev=", 10, 8)
        kept = hm.written_increasing(m.elev_volume)          # 3.26.6: rising as written
        assert len(ev) == len(kept) and np.allclose(ev, kept, atol=0.01)
        assert np.all(np.diff(np.asarray(ev)[:, 1]) > 0) and np.all(np.diff(np.asarray(ev)[:, 0]) > 0)
        # connection
        assert "Connection Up SA=Reservoir       " in lines
        assert "Connection Dn SA=Downstream      " in lines
        cl, _ = _count_block(lines, "Connection Line=", 4, 16)
        assert np.allclose(cl, m.conn_line, atol=1e-3)
        w, _ = _count_block(lines, "Conn Weir SE=", 10, 8)
        assert np.allclose(w, m.weir, atol=0.01) and len(w) <= 500
        assert "BC Line Storage Area=Downstream      " in lines
        bc, _ = _count_block(lines, "BC Line Arc=", 4, 16)
        assert len(bc) == len(m.bc_line)
        assert all(len(ln) <= 80 for ln in lines if not ln.startswith(("Storage Area=",
                   "Viewing", "Connection", "Geom Title", "BC Line", "Storage Area 2D Points")))
    finally:
        shutil.rmtree(d)


@test
def hecras_model_breakline_block_format():
    # _breakline_name strips '=' and line breaks and collapses whitespace
    assert hm._breakline_name("Culvert A = B\n  x") == "Culvert A B x"
    assert hm._breakline_name("") == "Breakline"
    bl = {"name": "Dam crest",
          "polyline": [(1000.0, 2000.0), (1100.5, 2100.25), (1200.0, 2200.0)],
          "cell_min": None, "cell_max": None}
    L = hm.breakline_block(bl)
    assert L[0] == "BreakLine Name=Dam crest"
    assert L[1] == "BreakLine CellSize Min=" and L[2] == "BreakLine CellSize Max="
    assert L[3] == "BreakLine Near Repeats=0" and L[4] == "BreakLine Protection Radius=-1"
    assert L[5] == "BreakLine Polyline= 3 "
    pts = []
    for ln in L[6:]:
        assert "=" not in ln
        pts += [float(ln[k:k + 16]) for k in range(0, len(ln.rstrip()), 16)]
    assert np.allclose(np.asarray(pts).reshape(-1, 2), bl["polyline"], atol=1e-3)
    # a set cell size is written; an empty one is blank
    L2 = hm.breakline_block({"name": "Road", "polyline": [(0.0, 0.0), (1.0, 1.0)],
                             "cell_min": 25.0, "cell_max": 50.0})
    assert L2[1].startswith("BreakLine CellSize Min=") and L2[1] != "BreakLine CellSize Min="


@test
def hecras_model_breaklines_written():
    if not _has_gdal():
        print("  (skipped: no GDAL)")
        return
    d, dfold, r = _build()
    try:
        m = r["model"]
        assert m.breaklines, "no breaklines were built"
        assert "Dam crest" in [bl["name"] for bl in m.breaklines]
        assert "Dam crest" in (r["info"].get("breaklines") or [])
        with open(r["paths"]["g01"], newline="") as f:
            lines = _blocks(f.read())
        bi = lines.index("BreakLine Name=Dam crest")
        ci = next(k for k, ln in enumerate(lines) if ln.startswith("Connection="))
        assert bi < ci, "breaklines must come before the connections"
        poly, _ = _count_block(lines, "BreakLine Polyline=", 4, 16)
        assert np.allclose(poly, m.conn_line, atol=1e-3)
    finally:
        shutil.rmtree(d)
    # the checkbox turns them off
    d, dfold, r = _build(options={"breaklines": False})
    try:
        assert not r["model"].breaklines
        with open(r["paths"]["g01"], newline="") as f:
            assert "BreakLine Name=" not in f.read()
    finally:
        shutil.rmtree(d)


@test
def hecras_model_plan_and_flow_text():
    if not _has_gdal():
        print("  (skipped: no GDAL)")
        return
    d, dfold, r = _build()
    try:
        m = r["model"]
        with open(r["paths"]["p01"], newline="") as f:
            p = _blocks(f.read())
        assert "Geom File=g01" in p and "Flow File=u01" in p
        loc = next(ln for ln in p if ln.startswith("Breach Loc="))
        parts = loc.split("=", 1)[1].split(",")
        assert [len(x) for x in parts[:3]] == [16, 16, 8] and parts[3] == "True"
        assert parts[4] == "Dam             "
        geom = next(ln for ln in p if ln.startswith("Breach Geom=")).split("=")[1].split(",")
        assert len(geom) == 10 and geom[5] == "True"
        b = m.breach
        assert abs(float(geom[0]) - b["center_station"]) < 0.01
        assert abs(float(geom[2]) - b["bottom_elev"]) < 0.01
        assert abs(float(geom[9]) - 3.1) < 1e-9
        st = next(ln for ln in p if ln.startswith("Breach Start="))
        assert st.startswith("Breach Start=True,1849.99,,,False,,,0"), st
        sim = next(ln for ln in p if ln.startswith("Simulation Date="))
        assert sim.startswith("Simulation Date=01JAN2000,0000,")
        assert any(ln == "UNET D2 Name=Downstream      " for ln in p)
        assert sum(ln.startswith("UNET D2 Equation= 1") for ln in p) == 2
        ci = next(ln for ln in p if ln.startswith("Computation Interval=")).split("=")[1]
        assert ci in [lab for _, lab in hm.COMP_INTERVALS]
        with open(r["paths"]["u01"], newline="") as f:
            u = _blocks(f.read())
        assert "Initial Storage Elev=Reservoir       ,1850" in u
        bl = [ln for ln in u if ln.startswith("Boundary Location=")]
        assert len(bl) == 2
        f0 = bl[0].split("=", 1)[1].split(",")
        f1 = bl[1].split("=", 1)[1].split(",")
        assert [len(x) for x in f0] == [16, 16, 8, 8, 16, 16, 16, 32]
        assert f0[5] == "Reservoir       " and f1[5] == "Downstream      "
        assert f1[7].strip() == hm.BC_NAME
        fs = next(ln for ln in u if ln.startswith("Friction Slope="))
        assert abs(float(fs.split("=")[1].split(",")[0]) - m.bc_slope) < 1e-6
        lat = u.index(next(ln for ln in u if ln.startswith("Lateral Inflow Hydrograph=")))
        n = int(u[lat].split("=")[1])
        assert n >= m.sim_hours + 1
        with open(r["paths"]["rasmap"], newline="") as f:
            rm = f.read()
        assert "GIS\\Projection.prj" in rm and "English Units" in rm
        import xml.dom.minidom
        xml.dom.minidom.parseString(rm)
    finally:
        shutil.rmtree(d)


@test
def hecras_model_si_when_the_crs_is_metric():
    if not _has_gdal():
        print("  (skipped: no GDAL)")
        return
    d, dfold, r = _build(crs="EPSG:26914")
    try:
        info, m = r["info"], r["model"]
        assert info["units"] == "SI Units" and info["crs"] == "EPSG:26914"
        assert abs(m.initial_wse - 1850 * FT) < 1e-6
        assert abs(m.breach["bottom_elev"] - 1825 * FT) < 0.02
        assert abs(m.breach["weir_coef"] - 3.1 * math.sqrt(FT)) < 1e-9
        w = np.array(m.weir)
        assert abs(w[:, 1].min() - 1850 * FT) < 0.02
        with open(r["paths"]["p01"]) as f:
            assert "UNET Gravity=9.80665" in f.read()
    finally:
        shutil.rmtree(d)


@test
def hecras_model_feet_dem_and_overtopping():
    if not _has_gdal():
        print("  (skipped: no GDAL)")
        return
    d, dfold, r = _build(run={"dem_feet": True, "method": br.METHOD_OVERTOPPING}
                         if hasattr(br, "METHOD_OVERTOPPING") else {"dem_feet": True})
    try:
        from osgeo import gdal
        ds = gdal.Open(r["paths"]["terrain_tif"])
        zt = ds.GetRasterBand(1).ReadAsArray()
        zt = zt[zt > -9000]
        assert 1780 < zt.min() < 1830 and zt.max() < 1900, (zt.min(), zt.max())
        assert "feet" in r["info"]["dem_z"]
    finally:
        shutil.rmtree(d)


@test
def hecras_model_turned_valley_with_abutments():
    if not _has_gdal():
        print("  (skipped: no GDAL)")
        return
    d = tempfile.mkdtemp()
    try:
        dfold, dem, dam, hy = make_run(d, rot_deg=35.0, hills=True, flood_crs=26914)
        r = hm.build_model(dfold, crs="EPSG:26914")
        m = r["model"]
        t = math.radians(35.0)
        fd = np.array([math.cos(t), math.sin(t)])
        _check_model(m, np.array([X0, Y0]), fd)
        # abutments: the weir follows the ground above the top of dam at the ends
        w = np.asarray(m.weir)
        assert w[0, 1] > 1850 * FT + 1 and w[-1, 1] > 1850 * FT + 1
        assert abs(w[:, 1].min() - 1850 * FT) < 0.01
        # the breach centre is on the valley axis (where the inflow point is)
        A = np.array(m.conn_line[0])
        c = A + (np.array(m.conn_line[-1]) - A) / np.hypot(*(np.array(m.conn_line[-1]) - A)) \
            * m.breach["center_station"]
        assert np.hypot(*(c - np.array([X0, Y0]))) < 5.0, c
        mc = r["info"]["mesh_check"]
        assert mc is None or mc["over_8_faces"] == 0, mc
    finally:
        shutil.rmtree(d)


@test
def hecras_model_dem_in_state_plane_feet_is_used():
    if not _has_gdal():
        print("  (skipped: no GDAL)")
        return
    d = tempfile.mkdtemp()
    try:
        dfold, dem, dam, hy = make_run(d)
        from osgeo import gdal, osr
        s = osr.SpatialReference()
        s.ImportFromEPSG(2265)
        dem_ft = os.path.join(d, "dem_sp.tif")
        gdal.Warp(dem_ft, dem, options=gdal.WarpOptions(dstSRS=s.ExportToWkt(), xRes=30,
                                                         yRes=30, srcNodata=-9999,
                                                         dstNodata=-9999))
        r = hm.build_model(dfold, dem_path=dem_ft)
        assert r["info"]["crs"] == "EPSG:2265", r["info"]["crs"]
        assert "DEM" in r["info"]["crs_why"]
        assert abs(r["info"]["terrain_cell"] - 30.0) < 1e-6
        f = hm.transformer(hm.srs_from("EPSG:26914"), hm.srs_from("EPSG:2265"))
        P, Q = f([[X0, Y0], [X0 + 120.0, Y0]])
        _check_model(r["model"], P, (Q - P) / np.hypot(*(Q - P)))
    finally:
        shutil.rmtree(d)


@test
def hecras_model_algorithm_runs():
    if not _has_gdal():
        print("  (skipped: no GDAL)")
        return
    from nd_breach_runner.tests.test_hecras import _algs
    h, fq = _algs()
    d = tempfile.mkdtemp()
    try:
        dfold, dem, dam, hy = make_run(d)
        a = h.HecRasModelAlgorithm()
        a.initAlgorithm()
        ctx = fq.QgsProcessingContext()
        fb = fq.QgsProcessingFeedback()
        r = a.processAlgorithm({"DAM_FOLDER": dfold, "CELL_FT": 75.0, "SIDE_BUFFER_FT": 500.0,
                                "END_BUFFER_FT": 1000.0, "TARGET_CELLS": 100000,
                                "EQUATION": 1, "BASE_FLOW_CFS": 10.0}, ctx, fb)
        assert os.path.exists(r["PROJECT"]) and r["PROJECT"].endswith(".prj")
        assert os.path.dirname(r["PROJECT"]) == os.path.join(dfold, "_hecras", "model")
        assert "English Units" in r["RESULT"] and "75" in r["RESULT"]
        with open(r["PROJECT"][:-4] + ".p01") as f:
            assert "UNET D2 Equation= 0 " in f.read()
        with open(r["PROJECT"][:-4] + ".u01") as f:
            assert "   10.00   10.00" in f.read()
        with open(r["README"]) as f:
            assert "| Cell size | 75 ft | chosen |" in f.read()
        try:
            a.processAlgorithm({"DAM_FOLDER": d}, fq.QgsProcessingContext(),
                               fq.QgsProcessingFeedback())
            assert False, "expected QgsProcessingException"
        except fq.QgsProcessingException as e:
            assert "breach_hydrograph.csv" in str(e)
    finally:
        shutil.rmtree(d)


@test
def hecras_model_missing_run_pieces_are_clear_errors():
    d = tempfile.mkdtemp()
    try:
        try:
            hm.build_model(d)
            assert False
        except hecras.HecRasError as e:
            assert "Not a finished breach run" in str(e)
    finally:
        shutil.rmtree(d)
