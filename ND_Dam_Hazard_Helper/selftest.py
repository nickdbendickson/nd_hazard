# -*- coding: utf-8 -*-
"""Self-test: runs the full processing pipeline on a small synthetic data
set in a temporary folder and reports PASS/FAIL per step.

Nothing is added to the current QGIS project. Run it from the plugin's
Readme tab ("Run Self-Test"), or from the QGIS Python console:

    from ND_Dam_Hazard_Helper.selftest import run_self_test
    ok, lines = run_self_test(); print("\\n".join(lines))
"""

import os
import shutil
import tempfile
import traceback

import numpy as np
from osgeo import gdal, osr

from qgis.core import (
    QgsProject, QgsRasterLayer, QgsVectorLayer, QgsFeature, QgsGeometry,
    QgsPointXY, QgsCoordinateReferenceSystem, QgsCoordinateTransform,
    QgsField, QgsRectangle,
)

from .compat import WKB_POINT, WKB_LINESTRING, WKB_POLYGON, FT_INT, FT_DOUBLE, FT_STRING
from .constants import CLASS_HIGH, CLASS_JUDGMENT, CLASS_LOW, CLASS_NODATA
from .helpers import (
    classify_scalar, export_layer_to_formats, create_memory_layer, CancelledByUser, gdal_exceptions,
)
from .processing import generate_hazard_rasters, make_samplers, close_samplers
from .structures import (
    build_inventory, evaluate_structures, structures_to_layer, TYPE_MOBILE, TYPE_HOUSE,
    TYPE_HABITABLE,
)
from .linear import analyze_roads, analyze_rail, FeatureIndex
from .classification import classify_nd, classify_tm11, extra_criteria
from .extent import make_extent_clip, distance_xy_in_crs

# ndhazard:<name> of every Processing algorithm the provider registers (2.35.1)
_EXPECTED_ALGORITHMS = (
    "acerzones", "batch", "buildpack", "chartsignoff", "classify", "creepwatch", "damsinseries",
    "dsswise", "ensemble", "fillform", "getdamdata", "hecrasplan", "markreviewed", "modelgate",
    "portfolio", "quickcreep", "reproduce", "reviewpackage", "reviewsummary", "rulecompare",
    "runbreach", "runnerculverts", "screendam", "suitechain", "suiteexport")

# Synthetic grid: NAD83 / UTM 14N (metres), 1 m cells, 120 x 90.
# Columns 0-39 depth 1 ft, 40-79 depth 3 ft, 80-119 depth 7 ft.
# Rows 0-9 are NoData. Velocity 6 ft/s on a coarser, shifted grid (forces
# the re-alignment path). Arrival 1.5 h on the depth grid.
EPSG = 26914
X0, Y0 = 500000.0, 5200000.0
NCOLS, NROWS = 120, 90
VEL = 6.0
BUFFER_FT = 25          # = 7.62 m in this CRS


class _Report(object):
    def __init__(self):
        self.ok = True
        self.lines = []

    def check(self, name, cond, detail=""):
        cond = bool(cond)
        self.ok = self.ok and cond
        line = ("PASS  " if cond else "FAIL  ") + name
        if detail and not cond:
            line += " - " + str(detail)
        self.lines.append(line)
        return cond

    def error(self, name, exc_text):
        self.ok = False
        self.lines.append("FAIL  {} - {}".format(name, exc_text.strip().splitlines()[-1]))
        self.lines.extend("      " + x for x in exc_text.strip().splitlines()[-6:])


def _write_tif(path, arr, origin_x, origin_y, cell, nodata=None):
    with gdal_exceptions():
        drv = gdal.GetDriverByName("GTiff")
        ds = drv.Create(path, int(arr.shape[1]), int(arr.shape[0]), 1, gdal.GDT_Float32)
        ds.SetGeoTransform((origin_x, cell, 0.0, origin_y, 0.0, -cell))
        srs = osr.SpatialReference()
        srs.ImportFromEPSG(EPSG)
        ds.SetProjection(srs.ExportToWkt())
        band = ds.GetRasterBand(1)
        if nodata is not None:
            band.SetNoDataValue(nodata)
        band.WriteArray(arr.astype(np.float32))
        band.FlushCache()
        band = None
        ds = None


def _read_tif(path):
    with gdal_exceptions():
        ds = gdal.Open(path)
        arr = ds.GetRasterBand(1).ReadAsArray()
        ds = None
    return arr


def _make_rasters(folder):
    depth = np.empty((NROWS, NCOLS), dtype=np.float32)
    depth[:, :40] = 1.0
    depth[:, 40:80] = 3.0
    depth[:, 80:] = 7.0
    depth[:10, :] = -9999.0
    vel = np.full((47, 62), VEL, dtype=np.float32)      # 2 m cells
    arr = np.full((NROWS, NCOLS), 1.5, dtype=np.float32)
    paths = {k: os.path.join(folder, "{}.tif".format(k)) for k in ("depth", "vel", "arr")}
    _write_tif(paths["depth"], depth, X0, Y0, 1.0, -9999.0)
    _write_tif(paths["vel"], vel, X0 - 2.0, Y0 + 2.0, 2.0, -9999.0)
    _write_tif(paths["arr"], arr, X0, Y0, 1.0, -9999.0)
    names = {"depth": "Depth", "vel": "Velocity", "arr": "Arrival"}
    layers = {k: QgsRasterLayer(p, names[k]) for k, p in paths.items()}
    return paths, layers


def _point_layer(name, crs, fields, rows, xform=None):
    lyr = create_memory_layer(name, WKB_POINT, crs, fields)
    feats = []
    for (x, y), attrs in rows:
        pt = QgsPointXY(x, y)
        if xform is not None:
            pt = xform.transform(pt)
        f = QgsFeature(lyr.fields())
        f.setGeometry(QgsGeometry.fromPointXY(pt))
        f.setAttributes(list(attrs))
        feats.append(f)
    lyr.dataProvider().addFeatures(feats)
    lyr.updateExtents()
    return lyr


def _square(x0, y0, x1, y1):
    return QgsGeometry.fromRect(QgsRectangle(x0, y0, x1, y1))


def _features_by(layer, field):
    return {f[field]: f for f in layer.getFeatures()}


def _line_layer(name, crs, fields, rows):
    lyr = create_memory_layer(name, WKB_LINESTRING, crs, fields)
    feats = []
    for coords, attrs in rows:
        f = QgsFeature(lyr.fields())
        f.setGeometry(QgsGeometry.fromPolylineXY([QgsPointXY(x, y) for x, y in coords]))
        f.setAttributes(list(attrs))
        feats.append(f)
    lyr.dataProvider().addFeatures(feats)
    return lyr


def _v2_checks(rep, rasters, utm, ctx, tmp, structs):
    """Consequences, sole access, review file, report layout / PDF,
    Processing provider, DSS-WISE recognition."""
    import numpy as _np
    from . import consequences as CQ, access as AC, review as RV
    from . import dsswise as DW
    from .autodetect import find_rasters
    ts = os.path.join(tmp, "timesteps")
    os.makedirs(ts)
    ones = _np.ones((10, 10), dtype=_np.float32)
    _write_tif(os.path.join(ts, "Depth (01JAN2000 10 00 00).tif"), ones, X0, Y0, 1.0, -9999.0)
    _write_tif(os.path.join(ts, "Depth (Max).tif"), ones * 5.0, X0, Y0, 1.0, -9999.0)
    found = find_rasters(ts)
    rep.check("Raster search: 'Depth (Max)' taken, a time-step raster left out",
              (found.get("depth") or "").endswith("Depth (Max).tif"), found.get("depth"))
    rep.check("Graham 1999: medium severity, 15-60 min, precise = 0.02",
              CQ.graham_rate(CQ.SEV_MEDIUM, CQ.WARN_15_60, "precise")[0] == 0.02)
    rep.check("EGM 01-03 one-story, 2 ft above first floor = 32.1 % / 17.9 %",
              CQ.damage_percent("one", 2.0) == (32.1, 17.9))
    # sole access: dry island whose only road crosses the flood
    isl = _np.full((60, 60), 2.0, dtype=_np.float32)
    isl[25:35, 25:35] = -9999.0
    ip = os.path.join(tmp, "island.tif")
    _write_tif(ip, isl, X0, Y0 + 60.0, 1.0, -9999.0)
    from .helpers import RasterSampler
    isamp = RasterSampler(QgsRasterLayer(ip, "island"))
    roads = _line_layer("SelfTest island roads", utm, [QgsField("name", FT_STRING)], [
        (((X0 + 27, Y0 + 30), (X0 + 33, Y0 + 30)), ("loop",)),
        (((X0 + 33, Y0 + 30), (X0 + 70, Y0 + 30)), ("causeway",))])
    house = create_memory_layer("SelfTest island house", WKB_POLYGON, utm,
                                [QgsField("building", FT_STRING)])
    f = QgsFeature(house.fields())
    f.setGeometry(_square(X0 + 29, Y0 + 30.5, X0 + 31, Y0 + 32.5))
    f.setAttributes(["house"])
    house.dataProvider().addFeatures([f])
    ist = build_inventory(house, [], utm, ctx)
    AC.access_analysis(roads, ist, isamp, utm, ctx, access_ft=50)
    isamp.close()
    rep.check("Sole access: dry house on an island is cut off", ist and ist[0]["cut_off"] is True,
              ist[0].get("access_note") if ist else "no structure")
    store = RV.ReviewStore(os.path.join(tmp, RV.REVIEW_FILE))
    store.set_decision(ist[0], utm, ctx, category="Not habitable", note="self-test",
                       reviewer="selftest")
    store.save()
    again = build_inventory(house, [], utm, ctx)
    n = RV.ReviewStore(store.path).attach(again, utm, ctx)
    from .structures import reinterpret
    reinterpret(again)
    rep.check("Engineer review decision saved and applied again",
              n == 1 and again[0]["habitable"] == "No", (n, again[0]["habitable"]))
    rep.check("DSS-WISE Lite file names recognised (depth ft, arrival h)",
              DW.detect_role("NA1_FloodDepth_in_Ft-05-03-2017_UTM.tif") == DW.R_DEPTH
              and DW.detect_units("NA1_FloodArrivalTime_in_Hrs-05-03-2017_UTM.tif",
                                  DW.R_ARR)[0] == "h")
    try:
        from .processing_provider import Provider
        prov = Provider()
        prov.refreshAlgorithms()
        # acerzones, classify, batch, dsswise and (v2.5) hecrasplan ... (2.17.0: getdamdata).
        # 2.35.1: checked by name, not a bare count (the count went stale at 21 when the
        # suite-chain algorithms brought it to 25 - full test 2026-10-30)
        names = sorted(a.name() for a in prov.algorithms())
        missing = sorted(set(_EXPECTED_ALGORITHMS) - set(names))
        extra = sorted(set(names) - set(_EXPECTED_ALGORITHMS))
        rep.check("Processing provider loads its {} algorithms".format(len(_EXPECTED_ALGORITHMS)),
                  not missing and not extra, {"missing": missing, "unexpected": extra})
    except Exception:
        rep.error("Processing provider", traceback.format_exc())
    try:
        from .report import build_report
        from .extent import make_extent_clip
        from .scenarios import Scenario
        from .structures import structures_to_layer
        lyr = structures_to_layer(structs, utm)
        nd_r = classify_nd(structs, [], [])
        extra_criteria(nd_r, structs, {}, {})
        res = {"nd": nd_r, "tm11": classify_tm11(structs, []),
               "layers": {"Habitable_Structures": lyr}}
        pdf, _note = build_report(tmp, "SelfTest", res, Scenario("Breach", "", "", ""), [],
                                  structs, make_extent_clip(rasters["depth"], 25, ctx),
                                  {"depth": rasters["depth"]}, max_pages=2,
                                  add_to_project=False)
        rep.check("Print layout exported to PDF", pdf and os.path.getsize(pdf) > 0, pdf)
        rep.check("Report has the ND 5.2 criteria table (last row = the ND class)",
                  nd_r.get("criteria") and nd_r["criteria"][-1]["result"] ==
                  nd_r["cls"].upper(), [c["result"] for c in nd_r.get("criteria", [])])
    except Exception:
        rep.error("Report PDF", traceback.format_exc())
    _v21_checks(rep, rasters, utm, structs)


def _v21_checks(rep, rasters, utm, structs):
    """2.1: layer-tree groups (on a detached tree - the project is not
    touched), located sampling, sample-point layers."""
    try:
        from qgis.core import QgsLayerTreeGroup, QgsPointXY
        from .helpers import ensure_group, plugin_group, _insert_index, RasterSampler
        from .constants import GRP_RESULTS, GRP_ZONES, GRP_SAMPLES
        root = QgsLayerTreeGroup()
        ensure_group(plugin_group("SelfTest", GRP_ZONES), root=root)
        ensure_group(plugin_group("SelfTest", GRP_RESULTS), root=root)
        g = ensure_group(plugin_group("SelfTest", GRP_SAMPLES), root=root)
        top = root.children()[0]
        order = [ch.name() for ch in top.children()]
        mem = create_memory_layer("SelfTest points", WKB_POINT, utm, [])
        node = g.insertLayer(_insert_index(g, mem), mem)
        rep.check("Layer groups: dam group with categories in order, layer added",
                  order == [GRP_RESULTS, GRP_SAMPLES, GRP_ZONES] and node is not None and
                  len(top.findLayers()) == 1, order)
        s = RasterSampler(rasters["depth"], 25)
        try:
            p = QgsPointXY(X0 + 95.5, Y0 - 5.0)             # NoData row, valid cells 5 m away
            loc = s.sample_located(p)
            rep.check("Located sampling: nearest valid cell reported (OSM sample points)",
                      loc is not None and loc[3] and loc[0] == s.sample(p) and
                      loc[2] <= Y0 - 10.0, loc)
        finally:
            s.close()
        from .structures import structure_sample_points_layer
        lyr = structure_sample_points_layer(structs, utm)
        n = lyr.featureCount() if lyr is not None else 0
        rep.check("Structure sample points written (where depth / velocity / zone were read)",
                  n > 0, n)
    except Exception:
        rep.error("2.1 checks", traceback.format_exc())


def _classification_checks(rep, rasters, utm, ctx):
    """Houses A (7 ft) and B (1 ft) + garage G as outlines; NSI points: RES2
    mobile home M and RES1 house H in the 3 ft band, RES1 C inside A.
    ND GIS Hub (NENA) address points: a driveway point 15 m south of B
    (moved onto B), a PARKING point P (habitable by the ND-address rule) and two stacked
    apartment units U. Expected at V = 6 ft/s: A HIGH, M HIGH (mobile
    curve), H JUDGMENT, B LOW (wet), G not habitable."""
    osm = create_memory_layer("SelfTest_OSM_bldg", WKB_POLYGON, utm,
                              [QgsField("osm_id", FT_STRING),
                               QgsField("building", FT_STRING)])
    feats = []
    for oid, tag, r in (("A", "house", (X0 + 90, Y0 - 70, X0 + 110, Y0 - 50)),
                        ("B", "house", (X0 + 5, Y0 - 70, X0 + 25, Y0 - 50)),
                        ("G", "garage", (X0 + 95, Y0 - 86, X0 + 105, Y0 - 80))):
        f = QgsFeature(osm.fields())
        f.setGeometry(_square(*r))
        f.setAttributes([oid, tag])
        feats.append(f)
    osm.dataProvider().addFeatures(feats)
    nsi = _point_layer(
        "SelfTest_NSI_pts", utm,
        [QgsField("fd_id", FT_INT), QgsField("occtype", FT_STRING),
         QgsField("bldgtype", FT_STRING), QgsField("pop2amu65", FT_DOUBLE)],
        [((X0 + 60.5, Y0 - 40.5), (1, "RES2", "H", 2.0)),
         ((X0 + 50.5, Y0 - 70.5), (2, "RES1-1SNB", "W", 3.0)),
         ((X0 + 100.5, Y0 - 60.5), (3, "RES1-1SNB", "W", 3.0))])
    nena = [QgsField("Add_Number", FT_INT), QgsField("St_Name", FT_STRING),
            QgsField("Unit", FT_STRING), QgsField("Placement", FT_STRING),
            QgsField("Place_Type", FT_STRING)]
    addr = _point_layer(
        "SelfTest_NDGISHUB_SiteStructure_Address_Points", utm, nena,
        [((X0 + 15, Y0 - 85), (101, "MAIN", None, "Property Access", "SINGLE FAMILY HOME")),
         ((X0 + 55.5, Y0 - 82.5), (200, "MAIN", None, "Site", "PARKING")),
         ((X0 + 30.5, Y0 - 40.5), (300, "MAIN", "1", "Structure", "APARTMENT")),
         ((X0 + 30.5, Y0 - 40.5), (300, "MAIN", "2", "Structure", "APARTMENT"))])
    samplers = make_samplers(rasters["depth"], rasters["vel"], rasters["arr"], 0)
    try:
        clip = make_extent_clip(rasters["depth"], 25, ctx)
        structs = build_inventory(osm, [("NSI", nsi), ("NDGISHUB", addr)], utm, ctx,
                                  clip=clip)
        evaluate_structures(structs, samplers, utm, ctx)
        zones = {}
        for s in structs:
            refs = " ".join(src.get("ref", "") for src in s["sources"])
            texts = " ".join(t for src in s["sources"] for t in src["texts"])
            tag = ("G" if "garage" in texts else
                   "P" if "PARKING" in texts else
                   "U" if "APARTMENT" in texts else
                   "M" if "NSI:1" in refs and not s["footprint"] else
                   "H" if "NSI:2" in refs and not s["footprint"] else
                   "A" if "NSI:3" in refs else "B")
            zones[tag] = s
        rep.check("Structures merged (NSI point inside outline A, driveway address on B)",
                  len(structs) == 7 and sorted(zones) == ["A", "B", "G", "H", "M", "P", "U"],
                  (len(structs), sorted(zones)))
        rep.check("Address point at the driveway moved onto outline B",
                  "NDGISHUB" in zones["B"]["families"], sorted(zones["B"]["families"]))
        # audit H1 (c1c9e5f, tests/test_lone_address_h1.py): an ND 911 address point with
        # no building (no outline within the address snap distance, no NSI point) stays
        # habitable by the ND site address rule - conservative - with a review note. The
        # v2.20.1 "unverified" expectation here predated H1 (2.35.1).
        rep.check("ND address point with no building (Place_Type PARKING) habitable by the "
                  "site address rule (noted for review)",
                  zones["P"]["habitable"] == "Yes" and bool(zones["P"].get("site_note")),
                  (zones["P"]["habitable"], zones["P"].get("site_note")))
        rep.check("ND address structures typed 'Habitable' (house curve), NSI-only RES1 house "
                  "stays 'House on foundation'",
                  zones["U"]["acer_type"] == TYPE_HABITABLE and
                  zones["B"]["acer_type"] == TYPE_HABITABLE and
                  zones["H"]["acer_type"] == TYPE_HOUSE,
                  (zones["U"]["acer_type"], zones["B"]["acer_type"], zones["H"]["acer_type"]))
        rep.check("FEMA category from the NSI occupancy (reference)",
                  zones["H"].get("fema_category") == "RES1 Single family dwelling",
                  zones["H"].get("fema_category"))
        rep.check("Two stacked apartment units = 2 dwellings (6 persons)",
                  zones["U"]["persons"] == 6, zones["U"]["persons"])
        rep.check("Garage outline not habitable", zones["G"]["habitable"] == "No",
                  zones["G"]["habitable"])
        rep.check("RES2 point typed as mobile home", zones["M"]["acer_type"] == TYPE_MOBILE,
                  zones["M"]["acer_type"])
        exp = {"A": CLASS_HIGH, "M": CLASS_HIGH, "H": CLASS_JUDGMENT, "B": CLASS_LOW}
        got = {k: zones[k]["zone"] for k in exp}
        rep.check("ACER zones A/M/H/B = HIGH/HIGH(mobile)/JUDGMENT/LOW", got == exp,
                  "{} != {}".format(got, exp))
        rep.check("RES1 point evaluated on the house-on-foundation curve",
                  zones["H"]["acer_type"] == TYPE_HOUSE, zones["H"]["acer_type"])

        roads_lyr = _line_layer(
            "SelfTest roads", utm, [QgsField("name", FT_STRING),
                                    QgsField("AADT", FT_INT),
                                    QgsField("AADT_YEAR", FT_INT)],
            [(((X0 + 45, Y0 - 20), (X0 + 75, Y0 - 20)), ("County Rd 12", 1500, 2024)),
             (((X0 - 500, Y0 + 500), (X0 - 400, Y0 + 500)), ("I-94", 20000, 2024)),
             (((X0 + 5, Y0 - 30), (X0 + 35, Y0 - 30)), ("Township Rd", 300, 2023))])
        rail_lyr = _line_layer(
            "SelfTest rail", utm, [QgsField("RROWNER1", FT_STRING),
                                   QgsField("PASSNGR", FT_STRING),
                                   QgsField("SUBDIV", FT_STRING)],
            [(((X0 + 85, Y0 - 15), (X0 + 115, Y0 - 15)), ("BNSF", "", "Test Sub"))])
        xing = _point_layer(
            "SelfTest crossings", utm,
            [QgsField("CrossingID", FT_STRING), QgsField("RailroadCode", FT_STRING),
             QgsField("TotalDaylightThruTrains", FT_INT),
             QgsField("TotalNighttimeThruTrains", FT_INT),
             QgsField("AnnualAverageDailyTrafficCount", FT_INT),
             QgsField("AnnualAverageDailyTrafficYear", FT_INT)],
            [((X0 + 2000, Y0 - 15), ("123456A", "BNSF", 8, 6, 900, 2021)),
             ((X0 + 60, Y0 - 21), ("654321B", "CP", 20, 10, 2500, 2019))])
        xidx = FeatureIndex(xing, utm, ctx, 5280.0)
        roads = analyze_roads(roads_lyr, samplers, utm, ctx, crossing_index=xidx)
        rails = analyze_rail(rail_lyr, samplers, utm, ctx, crossing_index=xidx)
        names = sorted(r["name"] for r in roads)
        rep.check("Overtopped roads found (dry I-94 ignored)",
                  names == ["County Rd 12", "Township Rd"], names)
        r1 = next((r for r in roads if r["name"] == "County Rd 12"), None)
        if r1 is not None:
            rep.check("Most recent AADT kept (2024 road value over 2019 crossing)",
                      r1["aadt"] == 1500 and r1["aadt_year"] == 2024,
                      (r1["aadt"], r1["aadt_year"], r1["aadt_source"]))
            rep.check("ND road class: AADT 1,500 -> Medium", r1["nd_class"] == "Medium",
                      r1["nd_class"])
        rep.check("Rail trains/day from same-railroad crossing (8 + 6)",
                  len(rails) == 1 and rails[0]["trains"] == 14,
                  [r["trains"] for r in rails])
        nd = classify_nd(structs, roads, rails)
        rep.check("ND classification = High", nd["cls"] == "High", nd["cls"])
        tm = classify_tm11(structs, roads)
        # H (RES1) and P (an ND address point with no building, habitable by the site
        # address rule since audit H1) are both in the 3-ft judgment band: 3 + 3 persons
        rep.check("TM 11: structures 6 high + 6 judgment, motorists on CR 12 counted -> High",
                  tm["lij_structures_high"] == 6 and tm["lij_structures_judgment"] == 6
                  and tm["lij_vehicles_high"] > 0 and tm["cls"] == "High",
                  (tm["lij_structures_high"], tm["lij_structures_judgment"],
                   round(tm["lij_vehicles_high"], 3), tm["cls"]))
        if r1 is not None:
            rep.check("Vehicle zone on County Rd 12 (3 ft at 6 ft/s) = HIGH (Fig 4)",
                      r1["vehicle_zone"] == CLASS_HIGH, r1["vehicle_zone"])
        # 1D model: depth only (no velocity or arrival raster)
        d_only = make_samplers(rasters["depth"], None, None, 0)
        try:
            names1 = sorted(r["name"] for r in analyze_roads(roads_lyr, d_only, utm, ctx))
        except Exception as e:                        # noqa: BLE001 - reported below
            names1 = "error: {!r}".format(e)
        finally:
            close_samplers(d_only)
        rep.check("Roads sampled without velocity / arrival rasters (1D)",
                  names1 == ["County Rd 12", "Township Rd"], names1)
    finally:
        close_samplers(samplers)
    return structs


def _v2201_checks(rep, utm, ctx):
    """v2.20.1: FEMA USA Structures parcel data is no habitability evidence;
    on a parcel whose 911 address point is on the house, an NSI-residential
    shed is unverified and a barn nothing describes is not habitable; a 911
    point with no building is unverified; NaN ellipsoid areas fall back."""
    from .structures import CAT_NOT_HABITABLE
    from .helpers import finite_area_ft2
    fema = create_memory_layer("SelfTest_FEMA_USA_Structures", WKB_POLYGON, utm,
                               [QgsField("OCC_CLS", FT_STRING), QgsField("PRIM_OCC", FT_STRING),
                                QgsField("PROP_ADDR", FT_STRING), QgsField("PROP_ZIP", FT_STRING)])
    farm = ("Agriculture", "Agriculture", "100 MAIN ST", "58000")
    feats = []
    for r, a in (((0, 0, 15, 10), farm),                    # H house (911 point)
                 ((40, 0, 60, 15), farm),                   # S machine shed (NSI RES1)
                 ((0, 40, 20, 55), farm),                   # B barn (nothing)
                 ((80, 0, 85, 20), farm),                   # M mobile home (NSI RES2)
                 ((200, 0, 212, 8), ("Residential", "Single Family Dwelling",
                                     "200 MAIN ST", "58000"))):     # G garage, no points
        f = QgsFeature(fema.fields())
        f.setGeometry(_square(X0 + r[0], Y0 + r[1], X0 + r[2], Y0 + r[3]))
        f.setAttributes(list(a))
        feats.append(f)
    fema.dataProvider().addFeatures(feats)
    nsi = _point_layer("SelfTest_NSI_v2201", utm,
                       [QgsField("fd_id", FT_INT), QgsField("occtype", FT_STRING),
                        QgsField("bldgtype", FT_STRING)],
                       [((X0 + 50, Y0 + 7), (11, "RES1-1SNB", "S")),
                        ((X0 + 82, Y0 + 10), (12, "RES2", "H")),
                        ((X0 + 70, Y0 + 30), (13, "RES1-1SNB", "S"))])
    addr = _point_layer("SelfTest_NDGISHUB_v2201", utm,
                        [QgsField("Add_Number", FT_INT), QgsField("St_Name", FT_STRING),
                         QgsField("Placement", FT_STRING), QgsField("Place_Type", FT_STRING)],
                        [((X0 + 7, Y0 + 5), (100, "MAIN", "Structure", None)),
                         ((X0 + 500, Y0 + 500), (900, "MAIN", "Structure", None))])
    inv = build_inventory([("FEMA_STRUCTURES", fema)], [("NSI", nsi), ("NDGISHUB", addr)],
                          utm, ctx)
    at = {}
    for r in inv:
        c = r["geom"].centroid().asPoint()
        at[(int(round(c.x() - X0)), int(round(c.y() - Y0)))] = r
    h, sh, b, m, g = (at.get(k, {}) for k in ((8, 5), (50, 8), (10, 48), (82, 10), (206, 4)))
    lone, pt = at.get((500, 500), {}), at.get((70, 30), {})
    rep.check("v2.20.1 house with its 911 point habitable", h.get("habitable") == "Yes",
              h.get("habitable"))
    rep.check("v2.20.1 NSI-RES1 shed on the house's parcel unverified (note names the parcel)",
              sh.get("habitable") == "Unverified" and "100 MAIN ST" in (sh.get("site_note") or ""),
              (sh.get("habitable"), sh.get("site_note")))
    rep.check("v2.20.1 barn with only FEMA parcel data on that parcel not habitable",
              b.get("category") == CAT_NOT_HABITABLE and bool(b.get("site_note")),
              (b.get("category"), b.get("site_note")))
    rep.check("v2.20.1 NSI RES2 on that parcel stays a habitable mobile home",
              m.get("habitable") == "Yes" and m.get("acer_type") == TYPE_MOBILE,
              (m.get("habitable"), m.get("acer_type")))
    rep.check("v2.20.1 FEMA 'Residential / Single Family Dwelling' + PROP_ADDR alone is not "
              "habitable evidence (unverified)", g.get("habitable") == "Unverified",
              g.get("habitable"))
    rep.check("v2.20.1 NSI point without outline next to the parcel joins it (unverified)",
              pt.get("habitable") == "Unverified" and bool(pt.get("site_note")),
              (pt.get("habitable"), pt.get("site_note")))
    rep.check("911 point with no building habitable by the site address rule (audit H1), "
              "flagged lone_address",
              lone.get("habitable") == "Yes" and bool(lone.get("lone_address")),
              (lone.get("habitable"), lone.get("lone_address")))

    class _NaNArea(object):
        def measureArea(self, _g):
            return float("nan")

        def convertAreaMeasurement(self, v, _u):
            return v
    sq = _square(X0, Y0, X0 + 10, Y0 + 10)
    a = finite_area_ft2(_NaNArea(), sq, utm, ctx)
    rep.check("v2.20.1 NaN ellipsoidal area falls back to the planar area (100 m2 = 1076 ft2)",
              a is not None and abs(a - 1076.39) < 0.5, a)


def run_self_test(progress_cb=None):
    """Returns (all_passed, report_lines)."""
    rep = _Report()

    def tick(pct, msg):
        if progress_cb:
            progress_cb(pct, "Self-test: " + msg)

    tmp = tempfile.mkdtemp(prefix="nd_dam_selftest_")
    ctx = QgsProject.instance().transformContext()
    utm = QgsCoordinateReferenceSystem("EPSG:{}".format(EPSG))
    wgs = QgsCoordinateReferenceSystem("EPSG:4326")
    to_wgs = QgsCoordinateTransform(utm, wgs, ctx)
    rasters = samplers = None
    import numpy as _np
    try:
        # 1. classification known answers ---------------------------------
        tick(2, "classification")
        rep.check("ACER-11 H=3 V=6 is JUDGMENT",
                  classify_scalar(3, 6, "found_low", "found_high") == CLASS_JUDGMENT)
        rep.check("ACER-11 H=4 V=4 is JUDGMENT",
                  classify_scalar(4, 4, "found_low", "found_high") == CLASS_JUDGMENT)
        rep.check("ACER-11 H=1 V=6 is LOW",
                  classify_scalar(1, 6, "found_low", "found_high") == CLASS_LOW)
        rep.check("ACER-11 H=7 V=6 is HIGH",
                  classify_scalar(7, 6, "found_low", "found_high") == CLASS_HIGH)
        rep.check("Mobile home H=3 V=12 is HIGH (Fig 3)",
                  classify_scalar(3, 12, "mobile_low", "mobile_high") == CLASS_HIGH)
        rep.check("Vehicle H=2.5 V=2 is JUDGMENT, H=3.5 HIGH, H=1 V=10 LOW (Fig 4)",
                  classify_scalar(2.5, 2, "vehicle_low", "vehicle_high") == CLASS_JUDGMENT
                  and classify_scalar(3.5, 2, "vehicle_low", "vehicle_high") == CLASS_HIGH
                  and classify_scalar(1, 10, "vehicle_low", "vehicle_high") == CLASS_LOW)

        # 2. synthetic rasters --------------------------------------------
        tick(8, "creating rasters")
        _paths, rasters = _make_rasters(tmp)
        if not rep.check("Synthetic rasters load in QGIS",
                         all(x.isValid() for x in rasters.values())):
            return rep.ok, rep.lines

        # 3. hazard rasters (strip processing + velocity re-alignment) -----
        tick(15, "hazard rasters")
        try:
            hz = generate_hazard_rasters(tmp, rasters["depth"], rasters["vel"], False,
                                         add_to_project=False)
            found = _read_tif(hz["found"])
            mobile = _read_tif(hz["mobile"])
            rep.check("Hazard raster size", found.shape == (NROWS, NCOLS), found.shape)
            rep.check("Hazard NoData rows", int(found[5, 60]) == CLASS_NODATA,
                      int(found[5, 60]))
            exp = [classify_scalar(d, VEL, "found_low", "found_high") for d in (1, 3, 7)]
            got = [int(found[50, c]) for c in (10, 60, 100)]
            rep.check("Hazard_Foundations classes", got == exp, "{} != {}".format(got, exp))
            expm = [classify_scalar(d, VEL, "mobile_low", "mobile_high") for d in (1, 3, 7)]
            gotm = [int(mobile[50, c]) for c in (10, 60, 100)]
            rep.check("Hazard_MobileHomes classes", gotm == expm,
                      "{} != {}".format(gotm, expm))
            found = mobile = None
            hz2 = generate_hazard_rasters(tmp, rasters["depth"], rasters["vel"], False,
                                          add_to_project=False)
            rep.check("Hazard rasters can be regenerated (overwrite)",
                      os.path.isfile(hz2["found"]))
        except CancelledByUser:
            raise
        except Exception:
            rep.error("Hazard rasters", traceback.format_exc())

        # 4. raster sampling (reprojection, buffer) ------------------------
        tick(40, "raster sampling")
        samplers = make_samplers(rasters["depth"], rasters["vel"], rasters["arr"], BUFFER_FT)
        from .helpers import transform_xy
        to_utm = QgsCoordinateTransform(wgs, utm, ctx)
        pts_wgs = [to_wgs.transform(QgsPointXY(x, y)) for x, y in (
            (X0 + 10.5, Y0 - 50.5), (X0 + 100.5, Y0 - 50.5), (X0 + 60.5, Y0 - 5.5),
            (X0 + 60.5, Y0 - 2.5))]
        back = [to_utm.transform(p) for p in pts_wgs]
        d1, d2 = samplers["depth"].sample(back[0]), samplers["depth"].sample(back[1])
        rep.check("Depth sampled after reprojection", d1 is not None and d2 is not None and
                  abs(d1 - 1.0) < 1e-6 and abs(d2 - 7.0) < 1e-6, (d1, d2))
        vx, vy = transform_xy(_np.array([back[0].x()]), _np.array([back[0].y()]),
                              QgsCoordinateTransform(utm, samplers["vel"].crs, ctx))
        v1 = samplers["vel"].values_at(vx, vy)[0]
        rep.check("Velocity sampled on the shifted, coarser grid", abs(v1 - VEL) < 1e-6, v1)
        a1 = samplers["arr"].sample(back[0])
        rep.check("Arrival sampled", a1 is not None and abs(a1 - 1.5) < 1e-6, a1)
        d3 = samplers["depth"].sample(back[2])                  # NoData, 5 m from valid
        rep.check("Nearest valid cell inside 25 ft buffer", d3 is not None and
                  abs(d3 - 3.0) < 1e-6, d3)
        d4 = samplers["depth"].sample(back[3])                  # NoData, 8 m from valid
        rep.check("25 ft buffer converted to metres (8 m point has no depth)", d4 is None, d4)
        far = _point_layer("SelfTest_far", utm, [QgsField("Name", FT_STRING)],
                           [((X0 - 100.0, Y0 - 45.0), ("W",))])      # 100 m west, dry
        wide = make_extent_clip(rasters["depth"], 500, ctx)
        narrow = make_extent_clip(rasters["depth"], 25, ctx)
        kept = build_inventory(None, [("NSI", far)], utm, ctx, clip=wide)
        outside = build_inventory(None, [("NSI", far)], utm, ctx, clip=narrow)
        rep.check("Study area: dry point inside raster extent + 500 ft kept, outside "
                  "extent + 25 ft dropped", len(kept) == 1 and len(outside) == 0,
                  (len(kept), len(outside)))
        dx, dy = distance_xy_in_crs(25, QgsCoordinateReferenceSystem("EPSG:3857"), 47.0)
        rep.check("Web Mercator: 25 ft at 47 N = 11.17 map metres", abs(dx - 11.173) < 0.01,
                  round(dx, 3))

        # 5. building outlines: inventory, edge ring, points joined, export --
        tick(60, "building outlines")
        osm = create_memory_layer("SelfTest_OSM", WKB_POLYGON, utm,
                                  [QgsField("osm_id", FT_STRING)])
        feats = []
        for oid, geom in (("A", _square(X0 + 90, Y0 - 70, X0 + 110, Y0 - 50)),
                          ("B", _square(X0 + 5, Y0 - 70, X0 + 25, Y0 - 50)),
                          ("C", _square(X0 + 5000, Y0 - 70, X0 + 5020, Y0 - 50)),
                          ("D", _square(X0 + 122, Y0 - 50, X0 + 126, Y0 - 46)),   # 2 m out
                          ("E", _square(X0 + 129, Y0 - 50, X0 + 133, Y0 - 46))):  # 9 m out
            f = QgsFeature(osm.fields())
            f.setGeometry(geom)
            f.setAttributes([oid])
            feats.append(f)
        osm.dataProvider().addFeatures(feats)
        osm.updateExtents()
        nsi = _point_layer("SelfTest_NSI", wgs,
                           [QgsField("Name", FT_STRING), QgsField("occtype", FT_STRING)],
                           [((X0 + 10.5, Y0 - 50.5), ("P1", "RES1")),
                            ((X0 + 100.5, Y0 - 50.5), ("P2", "RES1"))], xform=to_wgs)
        inv = build_inventory(osm, [("NSI", nsi)], utm, ctx, clip=narrow)
        # the outlines are told apart by where they are (centroid x offset)
        _ox = {15: "B", 100: "A", 124: "D", 131: "E", 5010: "C"}

        def _oid(r):
            return _ox.get(int(round(r["geom"].centroid().asPoint().x() - X0)), "?")
        ids = sorted(_oid(r) for r in inv if r["footprint"])
        rep.check("Outlines inside raster extent + 25 ft kept, beyond dropped",
                  ids == ["A", "B", "D"], ids)
        joined = sum(1 for r in inv if r["footprint"] and "NSI" in r["families"])
        rep.check("Points joined to their outlines (P1 in B, P2 in A)", joined == 2, joined)
        tick(70, "edge ring")
        evaluate_structures(inv, samplers, utm, ctx)
        by = {_oid(r): r for r in inv if r["footprint"]}
        rep.check("Outline A: max depth 7 ft over the footprint and its 5-ft ring",
                  "A" in by and by["A"]["max_depth"] is not None and
                  abs(by["A"]["max_depth"] - 7.0) < 1e-6, by.get("A", {}).get("max_depth"))
        rep.check("ACER zones from Dmax / Vmax (A = HIGH, B = LOW)",
                  "A" in by and "B" in by and by["A"]["zone"] == CLASS_HIGH
                  and by["B"]["zone"] == CLASS_LOW,
                  (by.get("A", {}).get("zone"), by.get("B", {}).get("zone")))
        from .structures import evaluation_areas_layer
        areas = evaluation_areas_layer(inv, utm)
        n_areas = areas.featureCount() if areas is not None else 0
        rep.check("Evaluation areas layer: one outline per wet structure (footprint + ring)",
                  n_areas == sum(1 for r in inv if r.get("region") is not None), n_areas)
        tick(90, "export")
        lyr = structures_to_layer(inv, utm)
        for _attempt in (1, 2):                 # second pass exercises overwrite
            _ok, res, paths = export_layer_to_formats(lyr, tmp, "Habitable_Structures", ctx)
        rep.check("GPKG and CSV export (and overwrite)", res.get("gpkg") and res.get("csv"),
                  res)
        if not res.get("xlsx"):
            rep.lines.append("NOTE  XLSX export unavailable in this QGIS build")
        if paths.get("gpkg"):
            back = QgsVectorLayer(paths["gpkg"], "check", "ogr")
            rep.check("Exported GPKG reloads with every structure",
                      back.isValid() and back.featureCount() == lyr.featureCount(),
                      back.featureCount() if back.isValid() else "invalid")
            back = None

        # 6. hazard classification (structures, roads, rail) --------------
        tick(93, "hazard classification")
        structs = []
        try:
            structs = _classification_checks(rep, rasters, utm, ctx)
        except CancelledByUser:
            raise
        except Exception:
            rep.error("Hazard classification", traceback.format_exc())

        # 6b. v2.20.1: parcel / site evidence, 911 points with no building ----
        try:
            _v2201_checks(rep, utm, ctx)
        except CancelledByUser:
            raise
        except Exception:
            rep.error("v2.20.1 checks", traceback.format_exc())

        # 7. v2.0: same-time ACER, consequences, access, review, report ----
        tick(96, "v2 checks")
        try:
            _v2_checks(rep, rasters, utm, ctx, tmp, structs)
        except CancelledByUser:
            raise
        except Exception:
            rep.error("v2 checks", traceback.format_exc())
    except CancelledByUser:
        raise
    except Exception:
        rep.error("Self-test", traceback.format_exc())
    finally:
        close_samplers(samplers)
        samplers = rasters = structs = None
        try:                                  # layers kept for the report layout
            from . import report as _rp
            del _rp._KEEP[:]
        except Exception:
            pass
        import gc
        gc.collect()                          # release GeoTIFF handles (Windows)
        shutil.rmtree(tmp, ignore_errors=True)
        tick(100, "done")
    rep.lines.insert(0, "Self-test {}: {} checks".format(
        "PASSED" if rep.ok else "FAILED",
        sum(1 for x in rep.lines if x.startswith(("PASS", "FAIL")))))
    return rep.ok, rep.lines

