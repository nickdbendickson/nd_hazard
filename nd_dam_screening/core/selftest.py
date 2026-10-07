"""The self-test (tool 14): a synthetic valley with one dam, run through tools 1-8 and 10.

The first thing to run in a real QGIS: it builds a small world in a temporary folder (a 10 m DEM
of a valley with an embankment, a pool, a road, a few structures, a railroad, a stream, the dam
line, a DWR-style inventory row), then runs the tools one after the other through
processing.run, checks that each wrote what the next one needs, and writes a plain-text report
with PASS / FAIL per step, timings and the software versions. A failure says which step and why.

Pure Python + numpy + GDAL here; the QGIS layers are made by the caller (algorithms/selftest.py
makes memory layers; the tests use the mock's layers), so this module runs in both.
"""
import datetime
import os
import platform
import time
import traceback

import numpy as np

CELL, NY, NX = 10.0, 320, 160                   # a smaller valley than the tests' (speed)
X0, YT = 400000.0, 1800000.0                   # EPSG:5070-like coordinates
DAM_ROW, DAM_COL = 60, NX // 2
DAM_HEIGHT_M = 7.0
ROAD_ROW = 150
RAIL_ROW = 290
DAM_ID = "DWR-1"


def xy(r, c):
    return X0 + (c + 0.5) * CELL, YT - (r + 0.5) * CELL


def build_terrain():
    """(dem array with the embankment, bare valley array, crest elevation m, pool elevation m)."""
    yy, xx = np.mgrid[0:NY, 0:NX]
    z = 300.0 - 0.003 * yy * CELL + 0.0009 * np.abs((xx - NX / 2) * CELL) ** 1.5
    zd = z.copy()
    crest = z[DAM_ROW, DAM_COL] + DAM_HEIGHT_M
    zd[DAM_ROW - 1:DAM_ROW + 2, :] = np.maximum(zd[DAM_ROW - 1:DAM_ROW + 2, :], crest)
    road = z[ROAD_ROW, DAM_COL] + 3.0
    zd[ROAD_ROW - 1:ROAD_ROW + 2, :] = np.maximum(zd[ROAD_ROW - 1:ROAD_ROW + 2, :], road)
    pool = crest - 2.0
    zd = np.where((yy < DAM_ROW - 1) & (zd < pool), pool, zd)        # hydro-flattened pool
    return zd, z, float(crest), float(pool)


def write_dem(folder):
    """The regional DEM GeoTIFF (float32 metres, EPSG:5070) for the 'local DEM folder' terrain
    source. Returns (folder, path)."""
    from osgeo import gdal, osr
    zd, _z, _c, _p = build_terrain()
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, "nd_selftest_dem.tif")
    ds = gdal.GetDriverByName("GTiff").Create(path, NX, NY, 1, gdal.GDT_Float32)
    if ds is None:
        raise RuntimeError("cannot create " + path)
    ds.SetGeoTransform((X0, CELL, 0, YT, 0, -CELL))
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(5070)
    ds.SetProjection(srs.ExportToWkt())
    ds.GetRasterBand(1).WriteArray(zd.astype("float32"))
    ds.FlushCache()
    ds = None
    return folder, path


def layer_specs():
    """{name: (kind, [(field, type)], [(geometry spec, attrs)])} of the synthetic layers.
    geometry spec: ("point", (x, y)) | ("line", [(x, y), ...]) | ("polygon", [(x, y), ...]).
    Field types: "string" / "double" / "int" (the caller maps them to QVariant)."""
    zd, z, crest, pool = build_terrain()
    S, D, I = "string", "double", "int"
    M3_PER_ACFT, M2_PER_AC, FT = 1233.48, 4046.86, 0.3048
    # the pool: cells below the pool level above the dam
    yy = np.arange(NY)
    pool_rows = [r for r in range(0, DAM_ROW - 1) if (zd[r] <= pool + 1e-6).any()]
    r_top = min(pool_rows) if pool_rows else DAM_ROW - 20
    half_w = 0
    for r in pool_rows:
        cols = np.where(zd[r] <= pool + 1e-6)[0]
        half_w = max(half_w, int(cols.max() - DAM_COL), int(DAM_COL - cols.min()))
    half_w = max(half_w, 3)
    poly = [xy(r_top, DAM_COL - half_w), xy(r_top, DAM_COL + half_w), xy(DAM_ROW - 1, DAM_COL + half_w),
            xy(DAM_ROW - 1, DAM_COL - half_w), xy(r_top, DAM_COL - half_w)]
    vol_m3 = float(((pool - z) * ((np.arange(NY)[:, None] < DAM_ROW - 1) & (z < pool))).clip(min=0).sum()
                   * CELL * CELL)
    area_m2 = float(((np.arange(NY)[:, None] < DAM_ROW - 1) & (z < pool)).sum() * CELL * CELL)
    dwr = [("point", xy(DAM_ROW, DAM_COL)), {
        "structure_": 1, "dam_name": "Self-test Dam", "max_dam_he": round(DAM_HEIGHT_M / FT, 1),
        "normal_vol": round(vol_m3 / M3_PER_ACFT, 1), "max_pool_v": round(1.3 * vol_m3 / M3_PER_ACFT, 1),
        "normal_are": round(area_m2 / M2_PER_AC, 1), "hazard_cla": "Low", "status": "Intact",
        "national_i": "ND00001", "dam_length": round(NX * CELL * 0.6 / FT, 0)}]
    line = lambda pts: ("line", list(pts))                                  # noqa: E731
    return {
        "dwr": ("point", [("structure_", I), ("dam_name", S), ("max_dam_he", D), ("normal_vol", D),
                          ("max_pool_v", D), ("normal_are", D), ("hazard_cla", S), ("status", S),
                          ("national_i", S), ("dam_length", D)], [dwr]),
        "waterbodies": ("polygon", [("gnis_name", S)], [(("polygon", poly), {"gnis_name": "Self-test Pool"})]),
        "damlines": ("line", [("structure_id", I)],
                     [(line([xy(DAM_ROW, 5), xy(DAM_ROW, NX - 6)]), {"structure_id": 1})]),
        "streams": ("line", [("gnis_name", S)],
                    [(line([xy(0, DAM_COL), xy(NY - 1, DAM_COL)]), {"gnis_name": "Self-test Creek"})]),
        "structures": ("point", [("OCC_CLS", S), ("PRIM_OCC", S)], [
            (("point", xy(110, DAM_COL + 3)), {"OCC_CLS": "Residential", "PRIM_OCC": "Single Family Dwelling"}),
            (("point", xy(118, DAM_COL - 5)), {"OCC_CLS": "Residential", "PRIM_OCC": "Manufactured Home"}),
            (("point", xy(115, DAM_COL + 2)), {"OCC_CLS": "Agriculture", "PRIM_OCC": "Barn"}),
            (("point", xy(110, 8)), {"OCC_CLS": "Residential", "PRIM_OCC": "Single Family Dwelling"})]),
        "roads": ("line", [("AADT_2023", D), ("ROUTE", S)],
                  [(line([xy(ROAD_ROW, 0), xy(ROAD_ROW, NX - 1)]), {"AADT_2023": 800.0, "ROUTE": "County road"})]),
        "rail": ("line", [("PASSNGR", S), ("RROWNER1", S)],
                 [(line([xy(RAIL_ROW, 0), xy(RAIL_ROW, NX - 1)]), {"PASSNGR": "B", "RROWNER1": "BNSF"})]),
    }


# ----------------------------------------------------------------------------- the steps --
def _nonempty_folder(p):
    try:
        if not os.path.isdir(p):
            return False
        with os.scandir(p) as it:
            return any(True for _ in it)
    except OSError:
        return False


def _pond_problem(curves, terrain, res):
    """"" when tool 16 wrote the pool's stage-storage curve and a terrain clip (it can breach)."""
    def names(folder, prefix, ext):
        try:
            return [n for n in os.listdir(folder) if n.startswith(prefix) and n.endswith(ext)]
        except OSError:
            return []
    if not res.get("OUT"):
        return "no 'OUT' output"
    if not names(curves, "curve_POND-", ".csv"):
        return "no stage-storage curve for the pool (16_stage_storage/curve_POND-*.csv)"
    if not names(terrain, "dam_POND-", ".tif"):
        return ("no terrain clip: the pool was not classed as held above the ground by the embankment "
                "(16_terrain/dam_POND-*.tif)")
    return ""


def steps(work, layers, dem_folder, engine=2, cell_m=20.0, sim_h=1.0, exe=""):
    """[(step, algorithm, params(prev_results) -> dict, check(results) -> problem text or "")].
    `layers`: {name: layer} as made by the caller; `prev` is {step: results dict}. 0.17.0: `exe`,
    the LISFLOOD-FP executable for step 7 (empty: not passed - the tool's own fallback)."""
    T = "TEMPORARY_OUTPUT"
    j = lambda *a: os.path.join(work, *a)                                   # noqa: E731

    def out_of(prev, step, key="OUT"):
        v = (prev.get(step) or {}).get(key)
        return v

    def check_layer(res, key, min_features=1):
        return "" if res.get(key) not in (None, "") else "no '{}' output".format(key)

    return [
        ("1", "preparedams",
         lambda prev: {"DWR": layers["dwr"], "DWR_UNITS": 0, "KEEP": "Intact", "MINH": 0, "MINV": 0,
                       "DIST_FT": 328.084, "OUT": T},
         lambda res: check_layer(res, "OUT")),
        ("2", "terraincache",
         lambda prev: {"DAMS": out_of(prev, "1"), "SRC": 1, "LOCAL": dem_folder, "RADIUS_FT": 3937.008,
                       "CELL": 10.0, "OUT": j("2_terrain")},
         lambda res: "" if os.path.isfile(j("2_terrain", "dam_{}.tif".format(DAM_ID))) else
         "2_terrain/dam_{}.tif not written".format(DAM_ID)),
        ("3", "estimatestorage",
         lambda prev: {"DAMS": out_of(prev, "1"), "WB": layers["waterbodies"], "DEMDIR": j("2_terrain"),
                       "SEARCH_FT": 492.126, "SHAPE": 1.0, "CURVES": j("3_stage_storage"), "OUT": T},
         lambda res: "" if _nonempty_folder(j("3_stage_storage")) and res.get("OUT") else
         "no stage-storage curve written"),
        ("4", "breachdirection",
         lambda prev: {"DAMS": out_of(prev, "3"), "LINES": layers["damlines"], "LID": "structure_id",
                       "WB": layers["waterbodies"], "DEMDIR": j("2_terrain"), "MATCH_FT": 984.252,
                       "ARROW_FT": 820.21, "OUTP": T, "OUTL": T},
         lambda res: check_layer(res, "OUTP")),
        ("5", "breachhydrograph",
         lambda prev: {"DAMS": out_of(prev, "3"), "CURVES": j("3_stage_storage"), "BPTS": out_of(prev, "4", "OUTP"),
                       "MODE": 0, "METHOD": 0, "SUNNY": False, "EXP": 2.0, "OUTDIR": j("5_hydrographs"),
                       "OUT": T},
         lambda res: "" if _nonempty_folder(j("5_hydrographs")) and res.get("OUT") else "no hydrograph written"),
        ("6", "triage",
         lambda prev: {"DAMS": out_of(prev, "5"), "BPTS": out_of(prev, "4", "OUTP"), "DEMDIR": j("2_terrain"),
                       "BLD": layers["structures"], "OCC": "OCC_CLS", "PRIM": "PRIM_OCC",
                       "ROADS": layers["roads"], "ROAD_AADT": "AADT_2023", "ROAD_CLASS": "ROUTE",
                       "RAIL": layers["rail"], "STREAMS": layers["streams"], "DAMLINES": layers["damlines"],
                       "N": 0.04, "MARGIN_FT": 0.5, "MFRAC": 0.3, "VF": 2.0, "BIG": 0, "AUDIT": 0,
                       "SEED": 1, "OUTDIR": j("6_triage_envelopes"), "OUT": T},
         lambda res: check_layer(res, "OUT")),
        ("7", "runbreachsim",
         lambda prev: {"DAMS": out_of(prev, "6"), "BPTS": out_of(prev, "4", "OUTP"), "DEMDIR": j("2_terrain"),
                       "TSRC": 2, "LOCAL": dem_folder, "ENGINE": engine, "SCEN": 0,
                       "CURVES": j("3_stage_storage"), "CELL": cell_m, "SIMH": sim_h, "SIMMAX": 2.0,
                       "CORR": True, "CORRW_FT": 984.252, "RMAX_FT": 6561.68, "STREAMS": layers["streams"],
                       "DAMLINES": layers["damlines"], "LID": "structure_id", "CASCADE": 0,
                       "NDEF": 0.05, "NMULT": 1.0, "WORKERS": 1, "TMO": 5.0, "SKIPTRI": False,
                       "KEEP": False, "OUTDIR": j("7_simulations"), "OUT": T,
                       **({"EXE": exe} if exe else {})},
         lambda res: "" if os.path.isfile(j("7_simulations", "{}_td_depth.tif".format(DAM_ID))) else
         "7_simulations/{}_td_depth.tif not written (status in the results layer)".format(DAM_ID)),
        ("8", "hazardclassification",
         lambda prev: {"SIM": out_of(prev, "7"), "BLD": layers["structures"], "OCC": "OCC_CLS",
                       "PRIM": "PRIM_OCC", "ROADS": layers["roads"], "ROAD_AADT": "AADT_2023",
                       "ROAD_CLASS": "ROUTE", "RAIL": layers["rail"], "RAIL_PAX": "PASSNGR",
                       "CRIT": 2, "MIND_FT": 0.1, "RDEP_FT": 0.1, "RULE": 2.0, "OUT": T, "IMP": T},
         lambda res: check_layer(res, "OUT")),
        ("10", "reports",
         lambda prev: {"HAZ": out_of(prev, "8"), "IMP": out_of(prev, "8", "IMP"),
                       "BPTS": out_of(prev, "4", "OUTP"), "DEMDIR": j("2_terrain"), "WHICH": 0,
                       "OUTDIR": j("10_reports")},
         lambda res: "" if os.path.isfile(j("10_reports", "index.html")) else "10_reports/index.html not written"),
        # 0.12: tool 16 on the same valley: the pool above the dam is a water body held above the
        # ground by an embankment, so it must come out breachable (a curve and a terrain clip)
        ("16", "findponds",
         lambda prev: {"WB": layers["waterbodies"], "WB_ID": "gnis_name", "SRC": 1, "LOCAL": dem_folder,
                       "MIN_AC": 0.05, "MAX_AC": 0.0, "CELL": CELL, "RADIUS_FT": 2624.672, "CLIP_CELL": CELL,
                       "TERRAIN": j("16_terrain"), "CURVES": j("16_stage_storage"), "ARROW_FT": 328.084,
                       "OUT": T, "OUTP": T, "OUTL": T, "OUTX": T},
         lambda res: _pond_problem(j("16_stage_storage"), j("16_terrain"), res)),
    ]


def run_selftest(work, layers, dem_folder, run, log=None, engine=2, cell_m=20.0, sim_h=1.0,
                 versions=None, exe=""):
    """Run the steps. `run(alg_id, params)` -> results dict (processing.run); `layers` from
    layer_specs() made by the caller. Returns {"ok", "rows": [...], "report": text}."""
    log = log or (lambda m: None)
    rows, prev, ok = [], {}, True
    for step, alg, mk, check in steps(work, layers, dem_folder, engine, cell_m, sim_h, exe):
        t0 = time.time()
        aid = "nddam:" + alg
        try:
            params = mk(prev)
            missing = [k for k, v in params.items() if v is None and k in ("DAMS", "SIM", "HAZ", "BPTS")]
            if missing:
                raise RuntimeError("input(s) {} missing: an earlier step did not produce them".format(
                    ", ".join(missing)))
            res = run(aid, params) or {}
            prev[step] = res
            problem = check(res)
        except Exception as e:                                          # noqa
            res, problem = {}, "{}: {}".format(type(e).__name__, e)
            tb = traceback.format_exc()
            log(tb)
        dt = time.time() - t0
        status = "PASS" if not problem else "FAIL"
        ok = ok and not problem
        rows.append({"step": step, "algorithm": aid, "status": status, "seconds": round(dt, 1),
                     "problem": problem})
        log("{} step {} {} ({:.1f} s){}".format(status, step, aid, dt, " - " + problem if problem else ""))
        if problem:
            break                                              # the next steps need this output
    return {"ok": ok, "rows": rows, "report": report_text(ok, rows, versions, cell_m, sim_h)}


def report_text(ok, rows, versions=None, cell_m=None, sim_h=None):
    lines = ["ND Dam Breach Screening self-test - {}".format("PASSED" if ok else "FAILED"),
             datetime.datetime.now().isoformat(timespec="seconds"),
             "Python {} on {}".format(platform.python_version(), platform.platform())]
    for k, v in sorted((versions or {}).items()):
        lines.append("{}: {}".format(k, v))
    lines.append("")
    for r in rows:
        lines.append("{:4s} step {:>2s} {:<28s} {:6.1f} s  {}".format(r["status"], r["step"], r["algorithm"],
                                                                     r["seconds"], r["problem"]))
    done = {r["step"] for r in rows}
    for step in ("1", "2", "3", "4", "5", "6", "7", "8", "10", "16"):
        if step not in done:
            lines.append("skip step {:>2s} (not run: an earlier step failed)".format(step))
    lines += ["", "A synthetic valley with one dam (10 m DEM, 7 m embankment, a pool, a road, structures, a "
                  "railroad), tools 1-8 and 10 with the chosen engine on a {} m grid for {} h of flood, then tool 16 "
                  "(ponds and dugouts) on the pool above the dam. "
                  "Tools 0, 7b, 9 and 11-13 are not part of the self-test.".format(
                      "{:g}".format(cell_m) if cell_m else "?", "{:g}".format(sim_h) if sim_h else "?"),
              "Nothing about the dam is real; the test checks that the tools run and hand their outputs on in "
              "this QGIS. It says nothing about model accuracy."]
    return "\n".join(lines)
