"""12 - Estimate the run time of tool 7 (order of magnitude) for a dams layer."""
import csv
import os

from qgis.core import (QgsProcessingAlgorithm, QgsProcessingParameterFeatureSource,
                       QgsProcessingParameterFile, QgsProcessingParameterEnum,
                       QgsProcessingParameterNumber, QgsProcessingParameterBoolean,
                       QgsProcessingParameterFileDestination)

from ..core import estimate as est, runner as rn, solver_io as sio
from ..core.fv_engine import Engine
from . import _common as cm
from .run_breach_sim import ENGINES, SCENARIOS
from ..compat import (FILE_FOLDER, NUM_DOUBLE, NUM_INT, SRC_POINT)


class EstimateRuntime(QgsProcessingAlgorithm):
    def name(self): return "estimateruntime"
    def displayName(self): return "12 - Estimate the run time of the simulations"
    def group(self): return "Dam screening"
    def groupId(self): return "dams"
    def createInstance(self): return EstimateRuntime()

    def shortHelpString(self):
        return ("ORDER-OF-MAGNITUDE estimate of how long tool 7 will take for these dams, before you "
                "start it. For every dam it works out what tool 7 would start with - the automatic "
                "cell size (by storage, >= 2 cells across the breach), the tool-2 clip (its half-width "
                "from the cached DEM when the folder is given, else 1,500 m), the corridor along the "
                "flow path, the run length from the hydrograph and the path - then the number of "
                "time steps (CFL limit at a representative depth) and the time from a tiny "
                "CALIBRATION RUN of the built-in engine on a 60 x 60 synthetic grid on this "
                "computer (a second or two). LISFLOOD-FP: the same count times a factor you set "
                "(default 0.1, i.e. ten times faster: a GUESS until you time it here).\n"
                "Low = one run per dam; typical = two; high = four (tool 7 re-runs a dam when the "
                "flood reaches the corridor wall or the terrain edge or is still spreading). "
                "Reality is usually within a factor of about 3 of 'typical'. Not counted: cascades, "
                "3DEP downloads, other programs using the CPU.\n"
                "Output: a CSV with one row per dam (cell size, cells, steps, low / typical / high "
                "seconds) and the totals in the log. Dams cleared by triage are skipped as tool 7 "
                "would skip them. Not run in a real QGIS during development (test doubles only).")

    def initAlgorithm(self, config=None):
        P = self.addParameter
        P(QgsProcessingParameterFeatureSource("DAMS", "Dams (output of tool 5 or tool 6)",
                                              [SRC_POINT]))
        P(QgsProcessingParameterEnum("SCEN", "Scenario", SCENARIOS, defaultValue=0))
        P(QgsProcessingParameterEnum("ENGINE", "Flood engine (as tool 7)", ENGINES, defaultValue=0))
        P(QgsProcessingParameterFile("DEMDIR", "Folder of cached dam DEMs (tool 2; for the clip size, optional)",
                                     behavior=FILE_FOLDER, optional=True))
        cm.distance_params(P, "CELL_FT", "CELL", "Model cell size (ft), 0 = automatic by reservoir size (as tool 7)",
                           33.0, min_ft=0.0)
        P(QgsProcessingParameterNumber("SIMH", "Simulated time (h), 0 = automatic (as tool 7)",
                                       NUM_DOUBLE, 12.0, minValue=0.0))
        P(QgsProcessingParameterNumber("SIMMAX", "Longest allowed simulated time (h; as tool 7)",
                                       NUM_DOUBLE, 96.0, minValue=1.0))
        P(QgsProcessingParameterBoolean("CORR", "Model only a corridor along the flow path (as tool 7)",
                                        defaultValue=True))
        cm.distance_params(P, "CORRW_FT", "CORRW", "Starting corridor half-width (ft; as tool 7)", 2000.0,
                           min_ft=150.0)
        P(QgsProcessingParameterNumber("WORKERS", "Dams to run at the same time (as tool 7)",
                                       NUM_INT,
                                       max((os.cpu_count() or 2) - 1, 1), minValue=1, maxValue=64))
        P(QgsProcessingParameterBoolean("SKIPTRI", "Skip dams cleared by triage (tool 6; as tool 7)",
                                        defaultValue=True))
        P(QgsProcessingParameterNumber("LF_FACTOR", "LISFLOOD-FP time / built-in time (a guess; 0.1 = ten "
                                       "times faster)", NUM_DOUBLE,
                                       est.LISFLOOD_FACTOR, minValue=0.001, maxValue=10.0))
        P(QgsProcessingParameterBoolean("CALIBRATE", "Time the built-in engine on a 60 x 60 grid now "
                                        "(a second or two; off = assume 1e6 cell-steps/s)", defaultValue=True))
        P(QgsProcessingParameterFileDestination("OUT_CSV", "Estimate per dam (CSV)", fileFilter="CSV (*.csv)"))

    def processAlgorithm(self, params, context, feedback):
        dams = self.parameterAsSource(params, "DAMS", context)
        scen = ["td", "sd"][self.parameterAsEnum(params, "SCEN", context)]
        ei = self.parameterAsEnum(params, "ENGINE", context)
        engine = rn.ENGINES[ei]
        demdir = self.parameterAsString(params, "DEMDIR", context)
        cell = cm.distance_m(self, params, context, "CELL_FT", "CELL", 33.0)
        simh = self.parameterAsDouble(params, "SIMH", context)
        simmax = self.parameterAsDouble(params, "SIMMAX", context)
        corr = self.parameterAsBool(params, "CORR", context)
        corrw = cm.distance_m(self, params, context, "CORRW_FT", "CORRW", 2000.0)
        workers = self.parameterAsInt(params, "WORKERS", context)
        skiptri = self.parameterAsBool(params, "SKIPTRI", context)
        lf = self.parameterAsDouble(params, "LF_FACTOR", context)
        do_cal = self.parameterAsBool(params, "CALIBRATE", context)
        out_csv = self.parameterAsFileOutput(params, "OUT_CSV", context)

        cal = None
        rate = 1.0e6
        if do_cal:
            feedback.pushInfo("calibration: running the built-in engine on a 60 x 60 synthetic grid ...")
            cal = est.calibrate(Engine)
            rate = cal["rate"]
            feedback.pushInfo(f"calibration: {cal['steps']} steps in {cal['wall_s']:.2f} s = {rate:,.0f} cell-steps/s")
        else:
            feedback.pushInfo(f"no calibration: assuming {rate:,.0f} cell-steps/s")

        names = dams.fields().names()
        get = lambda f, k, c=None: cm.fval(f, names, k, c)
        items = []
        for f in dams.getFeatures():
            if feedback.isCanceled():
                break
            dam_id = str(get(f, "dam_id") or f.id())
            d = {"dam_id": dam_id}
            tri = str(get(f, "tri_result") or "")
            if cm.excluded(f, names)[0]:
                d["skip"] = "excluded by review"
            elif cm.no_flood(f, names)[0]:
                d["skip"] = "no breach flood (tool 16)"
            elif skiptri and tri == "clear" and str(get(f, "tri_audit") or "") != "yes":
                d["skip"] = "cleared by triage"
            csvp = get(f, f"{scen}_csv")
            t_end = None
            if csvp and os.path.exists(str(csvp)):
                try:
                    t, q = sio.read_hydrograph_csv(str(csvp))
                    t_end = est.hydro_end_s(t, q)
                except Exception as e:  # noqa: BLE001
                    feedback.reportError(f"dam {dam_id}: hydrograph CSV not readable ({e}); 1 h assumed")
            elif not d.get("skip"):
                tf = get(f, f"{scen}_tf_min", float)
                if tf:
                    t_end = 4.0 * tf * 60.0                 # no CSV: about four formation times
                else:
                    d["skip"] = f"no {scen} hydrograph (tool 5 skipped this dam)"
            d.update({"vw_m3": get(f, f"{scen}_vw_m3", float), "bavg_m": get(f, f"{scen}_bavg_m", float),
                      "hb_m": get(f, f"{scen}_hb_m", float), "t_hydro_end_s": t_end})
            radius, dem_cell = None, 1.0
            if demdir:
                p = os.path.join(demdir, f"dam_{dam_id}.tif")
                if os.path.exists(p):
                    try:
                        ds = cm.gdal_open(p); g = ds.GetGeoTransform()
                        radius = 0.5 * min(ds.RasterXSize * g[1], ds.RasterYSize * abs(g[5])) - 50.0
                        radius = max(radius, 300.0)
                        dem_cell = abs(g[1])
                    except Exception:  # noqa: BLE001
                        radius = None
            d["radius_m"] = radius
            d["dem_cell_m"] = dem_cell
            items.append(d)

        rows = []
        for d in items:
            r, _tot = est.estimate_batch([d], rate, engine, 1, lf, dem_cell_m=d.pop("dem_cell_m", 1.0),
                                         cell_m=cell if cell > 0 else None, corridor=corr, corridor_hw_m=corrw,
                                         sim_s=simh * 3600.0 if simh > 0 else None, sim_max_s=simmax * 3600.0)
            rows.extend(r)
        totals = {k: est.batch_total([r[k + "_s"] for r in rows], workers) for k in ("low", "typical", "high")}
        totals.update({"n_dams": sum(1 for r in rows if not r["skip"]), "n_skipped": sum(1 for r in rows if r["skip"]),
                       "cells": sum(r["cells"] for r in rows), "workers": workers, "engine": engine,
                       "serial_typical_s": float(sum(r["typical_s"] for r in rows))})
        os.makedirs(os.path.dirname(out_csv) or ".", exist_ok=True)
        with open(out_csv, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=est.CSV_COLUMNS)
            w.writeheader()
            for r in est.rows_for_csv(rows):
                w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in est.CSV_COLUMNS})
        for line in est.summary_lines(totals, cal, ENGINES[ei], lf):
            feedback.pushInfo(line)
        big = sorted((r for r in rows if not r["skip"]), key=lambda r: -r["typical_s"])[:5]
        if big:
            feedback.pushInfo("largest: " + "; ".join(f"{r['dam_id']} {est.fmt_duration(r['typical_s'])} "
                                                       f"({r['cells']:,} cells at {r['cell_m']:g} m, {r['steps']:,} steps)"
                                                       for r in big))
        return {"OUT_CSV": out_csv, "TYPICAL_H": totals["typical"] / 3600.0, "LOW_H": totals["low"] / 3600.0,
                "HIGH_H": totals["high"] / 3600.0, "RATE": rate, "N_DAMS": totals["n_dams"]}
