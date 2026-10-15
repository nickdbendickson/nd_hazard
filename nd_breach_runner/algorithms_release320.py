"""The 3.20.0 tools of the provider: the crossing closure table (feature 13), the cascade check
(feature 4), two runs compared (feature 15), a queue over a list of dams (feature 16) and the
sign-off of the fatality rate table (feature 12).

Each one is a thin shell: the arithmetic lives in core/crossings.py, core/cascadecheck.py,
core/compareruns.py, core/batchqueue.py and core/lifeloss.py, which are importable without QGIS
and are what the tests exercise. What is here is reading layers, transforming coordinates and
saying plainly in the log what was and was not done.

Two choices that are about conservatism rather than effort, and are stated in the tools' own
help text as well as here:

  * the cascade tool RECORDS this run's breach outflow in the downstream dam's inputs folder and
    writes the next job out; it does not start that run and it does not replace an existing
    pmf_inflow.csv, because a PMF inflow may well be the larger flow and overwriting it would
    quietly make the next run milder. Where a PMF inflow is already there, the SUM of the two is
    offered as a separate file - a sum is never the milder of the two.
  * the queue runs two dams side by side at one thread each. That is a speed choice measured on
    this machine (1.76x against one dam on two threads); it changes no number in any result.
"""
import datetime
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np
from qgis.core import (QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsPointXY,
                       QgsProcessingException, QgsProcessingOutputFile,
                       QgsProcessingOutputFolder, QgsProcessingOutputNumber,
                       QgsProcessingOutputString, QgsProcessingParameterFeatureSource,
                       QgsProcessingParameterFileDestination)

from . import qgis_io as qio
from .algorithms import (RunDamAlgorithm, _Base, _Feedback, _attr_of, _safe_name)
from .algorithms_consequences import ROAD_SYNONYMS, _ground_m_per_unit, _txt
from .algorithms_tools import _ToolBase
from .compat import ALG_NO_THREADING, SOURCE_LINE, SOURCE_POINT, SOURCE_POLYGON
from .core import (batchqueue, breach as brmod, cascadecheck, compareruns, consequences as cons,
                   crossings as crmod, damjson, impacts as impmod, lifeloss, nd_hazard as nh,
                   workflow)
from .core.lisflood import Cancelled, ModelError

STRUCT_ID_SYNONYMS = ("id", "structure_id", "struct_id", "objectid", "fid", "name", "bldg_id")
MOBILE_FIELDS = ("mobile", "mobile_home", "manufactured", "occtype", "st_damcat", "bldgtype")


def _raster_crs(wkt):
    """The rasters' CRS as a QgsCoordinateReferenceSystem (the dam folder's rasters carry the
    DEM's CRS). An empty CRS when it cannot be read: the caller then says so."""
    if not wkt:
        return QgsCoordinateReferenceSystem("")
    from .core import gdal_io
    try:
        epsg = gdal_io.epsg_of(wkt)
    except Exception:                                            # noqa
        epsg = None
    if epsg:
        return QgsCoordinateReferenceSystem(epsg)
    crs = QgsCoordinateReferenceSystem()
    try:
        crs.createFromWkt(wkt)
    except Exception:                                            # noqa
        pass
    return crs


def _box(grid):
    """(centre, radius) of a grid, for the layer readers' spatial filter."""
    xmin, ymin, xmax, ymax = grid.bounds
    return (0.5 * (xmin + xmax), 0.5 * (ymin + ymax)), \
        0.5 * float(np.hypot(xmax - xmin, ymax - ymin))


def _fields_by_name(alg, parameters, context, names, synonyms):
    low = {n.lower(): n for n in names}
    out = {}
    for k, syns in synonyms.items():
        chosen = alg._str(parameters, k, context)
        out[k] = chosen or next((low[s] for s in syns if s in low), "")
    return out


# ============================================================================ feature 13
class CrossingsAlgorithm(_ToolBase):
    def name(self):
        return "crossings"

    def displayName(self):
        return "Road and railroad crossings in the flood (closure table)"

    def shortHelpString(self):
        return ("One row per unbroken stretch of road or railroad the breach flood wets: when "
                "it first gets wet, how deep and how fast the water is there, how long it "
                "stays wet and whether the crossing counts as closed to traffic. Reads 'Depth "
                "(Max).tif', 'Velocity (Max).tif' and 'Arrival Time.tif' from the dam folder "
                "and the road and railroad lines you give, and writes 'Road and Rail "
                "Crossings.csv', '.geojson' (a layer you can style and post) and '.md' into "
                "the dam folder.\n\n"
                "The table is a screening product off the maximum-value rasters: the deepest "
                "water and the fastest water at a crossing need not happen at the same moment, "
                "so a row's depth and velocity are an envelope, not a snapshot. 'How long wet' "
                "is the time from the first wet moment to the end of the simulated run unless "
                "the run kept snapshots, and the column says which of the two it is - an upper "
                "bound is marked as one rather than passed off as a measurement.\n\n"
                "Closed to traffic is YOUR rule, not a published standard: the starting "
                "values are a depth of {:g} ft or a depth x velocity of {:g} ft2/s (the same "
                "depth x velocity the Runner already treats as dangerous to a person in the "
                "flow). Set them to your agency's numbers.".format(
                    crmod.CLOSURE_DEPTH_FT, crmod.CLOSURE_DV_FT2S))

    def initAlgorithm(self, config=None):
        self._file("DAM_FOLDER", "Dam folder with 'Depth (Max).tif' etc.", folder=True,
                   optional=False)
        self.addParameter(QgsProcessingParameterFeatureSource(
            "ROADS", "Roads (lines)", [SOURCE_LINE], optional=True))
        self._field("R_NAME", "Roads: identifier field", "ROADS", numeric=False, advanced=True)
        self._field("R_AADT", "Roads: AADT field (vehicles per day)", "ROADS", advanced=True)
        self.addParameter(QgsProcessingParameterFeatureSource(
            "RAIL", "Railroads (lines)", [SOURCE_LINE], optional=True))
        self._field("RAIL_NAME", "Railroads: identifier field", "RAIL", numeric=False,
                    advanced=True)
        self._num("WET_FT", "A stretch counts as wet from a depth of (ft; the starting value "
                  "{:g} is the engineer's, a screening value you choose, just above raster "
                  "noise)".format(crmod.WET_FT), crmod.WET_FT, minimum=0.001)
        self._num("CLOSURE_FT", "Closed to traffic at a depth of (ft; the starting value {:g} "
                  "is the engineer's, not a published standard)".format(crmod.CLOSURE_DEPTH_FT),
                  crmod.CLOSURE_DEPTH_FT, minimum=0.0)
        self._num("CLOSURE_DV", "... or at a depth x velocity of (ft2/s, that is ft x ft/s; "
                  "the starting value {:g} is the engineer's, not a published standard)".format(
                      crmod.CLOSURE_DV_FT2S), crmod.CLOSURE_DV_FT2S, minimum=0.0)
        self._num("SIM_END_HR", "Simulated time of the run (hr; empty = from the run report), "
                  "for the upper bound on how long a crossing stays wet", optional=True,
                  minimum=0.0, advanced=True)
        self._bool("WRITE_GEOJSON", "Write the crossings as GeoJSON as well as CSV", True)
        self.addOutput(QgsProcessingOutputFile("CSV", crmod.OUTPUT_NAMES["csv"]))
        self.addOutput(QgsProcessingOutputFile("GEOJSON", "Crossings (GeoJSON)"))
        self.addOutput(QgsProcessingOutputFile("REPORT", "Crossings (Markdown)"))
        self.addOutput(QgsProcessingOutputNumber("CLOSED", "Crossings closed"))
        self.addOutput(QgsProcessingOutputString("RESULT", "Result"))

    def _read_lines(self, parameters, context, grid, crs, layer, kind, rec, names_field):
        src = self.parameterAsSource(parameters, layer, context)
        if src is None:
            return
        names = qio.field_names(src)
        fld = _fields_by_name(self, parameters, context, names,
                              {names_field: ROAD_SYNONYMS["R_NAME"]})
        to_r = QgsCoordinateTransform(src.sourceCrs(), crs, context.transformContext())
        to_l = QgsCoordinateTransform(crs, src.sourceCrs(), context.transformContext())
        center, radius = _box(grid)
        got = qio.lines(src, to_r, to_l, center, radius,
                        fields=[fld[names_field]] if fld[names_field] else [])
        unit_m = _ground_m_per_unit(crs, center, context)
        for i, (verts, at) in enumerate(got):
            ident = _txt(at.get(fld[names_field])) if fld[names_field] else ""
            rec.add_lines([verts], kind, ids=[ident or "{} {}".format(kind, i + 1)],
                          unit_m=unit_m)

    def processAlgorithm(self, parameters, context, feedback):
        folder = self.parameterAsFile(parameters, "DAM_FOLDER", context)
        try:
            grid, with_f, _base_f, notes = cons.read_dam_folder(folder)
        except ImportError:
            raise QgsProcessingException("GDAL's Python bindings are not available; run this "
                                         "from QGIS.")
        except (OSError, ValueError, RuntimeError) as e:
            raise QgsProcessingException(str(e))
        for n in notes:
            feedback.pushInfo(n)
        crs = _raster_crs(grid.crs_wkt)
        rec = impmod.Receptors()
        self._read_lines(parameters, context, grid, crs, "ROADS", impmod.KIND_ROAD, rec,
                         "R_NAME")
        self._read_lines(parameters, context, grid, crs, "RAIL", impmod.KIND_RAIL, rec,
                         "RAIL_NAME")
        if not len(rec):
            raise QgsProcessingException(
                "No road or railroad line falls inside the dam folder's rasters: give the "
                "roads or railroads layer (and check its CRS).")
        g = lambda n: self.parameterAsDouble(parameters, n, context)
        sim_end = self._opt(parameters, "SIM_END_HR", context)
        if sim_end is None:
            sim_end = compareruns.run_info(folder).get("sim_time_hr")
            if sim_end is not None:
                feedback.pushInfo("Simulated time from the run report: {} hr".format(sim_end))
            else:
                feedback.pushInfo("The run report does not say how long the run was simulated, "
                                  "so 'how long wet' is left unknown rather than guessed.")
        try:
            rows = crmod.crossing_rows(rec, grid, with_f, "us", wet_ft=g("WET_FT"),
                                       closure_depth_ft=g("CLOSURE_FT"),
                                       closure_dv_ft2s=g("CLOSURE_DV"),
                                       sim_end_hr=sim_end)
        except (ValueError, RuntimeError) as e:
            raise QgsProcessingException(str(e))
        summ = crmod.summary(rows, g("WET_FT"), g("CLOSURE_FT"), g("CLOSURE_DV"))
        epsg = None
        try:
            from .core import gdal_io
            epsg = gdal_io.epsg_of(grid.crs_wkt) if grid.crs_wkt else None
        except Exception:                                        # noqa
            epsg = None
        try:
            paths = crmod.write_outputs(
                folder, rows, epsg,
                geojson=self.parameterAsBool(parameters, "WRITE_GEOJSON", context))
            md = os.path.join(folder, "Road and Rail Crossings.md")
            with open(md, "w", encoding="utf-8") as fh:
                fh.write("\n".join(crmod.markdown_lines(rows, summ)))
        except OSError as e:
            raise QgsProcessingException("Could not write the crossing table: {}".format(e))
        msg = ("{} wet stretch(es) of road or railroad; {} closed to traffic by your rule "
               "({:g} ft deep or {:g} ft2/s)".format(summ["crossings"], summ["closed"],
                                                     g("CLOSURE_FT"), g("CLOSURE_DV")))
        feedback.pushInfo(msg)
        if summ.get("bounded_duration"):
            feedback.pushWarning(
                "Check: 'how long wet' for at least one crossing is the time from the first wet "
                "moment to the END of the simulated run, which is an UPPER bound inside the run "
                "(the water may recede sooner) and may be short of the real closure (the flood "
                "may still be there when the run ends). It is not a measurement: the run keeps "
                "maximum depth and arrival time, not a depth time series. Run the animation "
                "tool's time steps to measure it.")
        if paths.get("geojson"):
            self.load(context, paths["geojson"], "Road and rail crossings")
        return {"CSV": paths["csv"], "GEOJSON": paths.get("geojson", ""), "REPORT": md,
                "CLOSED": summ["closed"], "RESULT": msg}


# ============================================================================ feature 4
class CascadeCheckAlgorithm(_ToolBase):
    def name(self):
        return "cascadecheck"

    def displayName(self):
        return "Cascade check: does this flood reach another dam?"

    def shortHelpString(self):
        return ("Tests the flood footprint of a finished run ('Flood Extent.geojson' in the dam "
                "folder) against the dam inventory and lists the dams the flood reaches, "
                "nearest first. The inventory is every dam folder of the dams folder (its "
                "dam.json) plus any dam layer you give.\n\n"
                "For each dam found it writes the next job out: '_cascade/next_jobs.json' in "
                "this dam's folder, and a copy of this run's breach outflow hydrograph as "
                "'inputs/upstream_breach_inflow.csv' in the downstream dam's own folder. Where "
                "that dam already has an 'inputs/pmf_inflow.csv', the SUM of the two is written "
                "beside it as 'inputs/pmf_inflow_plus_upstream.csv'.\n\n"
                "READ THIS. The inflow is RECORDED, not used: this tool does not start the next "
                "run and never replaces an existing pmf_inflow.csv. A PMF inflow may be the "
                "larger of the two flows, so overwriting it could quietly make the downstream "
                "run milder, and this suite does not move a result that way on its own. To use "
                "the hand-off, run the downstream dam yourself with the written CSV as its PMF "
                "inflow (the sum file when both apply). 'Cascade failure of dams in a chain' is "
                "the separate tool that models a chain in one go.")

    def initAlgorithm(self, config=None):
        self._file("DAM_FOLDER", "Dam folder of the finished run", folder=True, optional=False)
        self._file("DAMS_FOLDER", "Dams folder holding the other dams' folders (empty = the "
                   "folder above the dam folder)", folder=True, optional=True)
        self.addParameter(QgsProcessingParameterFeatureSource(
            "DAMS", "Dam inventory (points), as well as the dam folders", [SOURCE_POINT],
            optional=True))
        self._field("D_ID", "Dam inventory: identifier field", "DAMS", numeric=False,
                    advanced=True)
        self._field("D_NAME", "Dam inventory: name field", "DAMS", numeric=False, advanced=True)
        self._file("HYDROGRAPH", "Breach outflow hydrograph CSV (empty = the run's own "
                   "breach_hydrograph.csv)", "csv")
        self._bool("WRITE_JOBS", "Write the next jobs and the inflow CSVs", True)
        self.addOutput(QgsProcessingOutputFile("JOBS", cascadecheck.JOB_NAME))
        self.addOutput(QgsProcessingOutputNumber("DAMS_FOUND", "Dams in the flood"))
        self.addOutput(QgsProcessingOutputString("RESULT", "Result"))

    def _inventory(self, parameters, context, feedback):
        """Extra dam dicts from a dam layer, in longitude / latitude (damsuite.cascade's own
        units). An unreadable feature is skipped, never fatal."""
        src = self.parameterAsSource(parameters, "DAMS", context)
        if src is None:
            return []
        names = qio.field_names(src)
        fld = _fields_by_name(self, parameters, context, names,
                              {"D_ID": ("nidid", "nid_id", "dam_id", "id", "structure_number"),
                               "D_NAME": ("name", "dam_name", "damname")})
        tf = QgsCoordinateTransform(src.sourceCrs(), QgsCoordinateReferenceSystem("EPSG:4326"),
                                    context.transformContext())
        out = []
        for i, f in enumerate(src.getFeatures()):
            try:
                g = f.geometry()
                if g is None or g.isEmpty():
                    continue
                p = tf.transform(g.centroid().asPoint())
            except Exception:                                    # noqa - one dam
                continue
            out.append({"id": _txt(_attr_of(f, fld["D_ID"])) or "dam {}".format(i + 1),
                        "name": _txt(_attr_of(f, fld["D_NAME"])),
                        "lon": p.x(), "lat": p.y(), "source": "dam inventory layer"})
        feedback.pushInfo("Dam inventory layer: {} dam(s) read".format(len(out)))
        return out

    def processAlgorithm(self, parameters, context, feedback):
        folder = self.parameterAsFile(parameters, "DAM_FOLDER", context)
        if not os.path.isdir(folder):
            raise QgsProcessingException("No dam folder at {}".format(folder))
        dams_folder = self._fileparam(parameters, "DAMS_FOLDER", context) or \
            os.path.dirname(os.path.normpath(folder))
        dam_id = os.path.basename(os.path.normpath(folder))
        rec, _problem = damjson.load(folder)
        source = None
        got, note = damjson.point_from_record(rec)
        if got is not None:
            kind, x, y, rcrs = got
            try:
                tf = QgsCoordinateTransform(QgsCoordinateReferenceSystem(rcrs),
                                            QgsCoordinateReferenceSystem("EPSG:4326"),
                                            context.transformContext())
                p = tf.transform(QgsPointXY(x, y))
                source = (p.x(), p.y())
                feedback.pushInfo(note)
            except Exception:                                    # noqa
                source = None
        if source is None:
            feedback.pushInfo("dam.json gives no location for this dam, so the distances to the "
                              "dams found are left out (which dams are in the flood is "
                              "unaffected).")
        to_lonlat = self._to_lonlat(folder, context, feedback)
        try:
            info = cascadecheck.check(folder, dams_folder, dam_id, source, to_lonlat,
                                      self._inventory(parameters, context, feedback))
        except (OSError, ValueError) as e:
            raise QgsProcessingException(str(e))
        hyd = self._fileparam(parameters, "HYDROGRAPH", context)
        if not hyd:
            for cand in (os.path.join(folder, workflow.MODEL_FOLDER, "breach_hydrograph.csv"),
                         os.path.join(folder, "inputs", "breach_hydrograph.csv")):
                if os.path.isfile(cand):
                    hyd = cand
                    break
        written = None
        if info["hits"] and self.parameterAsBool(parameters, "WRITE_JOBS", context):
            try:
                written = cascadecheck.write_jobs(folder, info, dams_folder, hyd,
                                                  workflow.VERSION)
            except OSError as e:
                raise QgsProcessingException("Could not write the cascade jobs: {}".format(e))
        for line in cascadecheck.report_lines(info, written):
            feedback.pushInfo(line)
        feedback.pushWarning("Check: " + cascadecheck.NOT_CONSUMED)
        if info.get("problem"):
            feedback.reportError(info["problem"], fatalError=False)
        return {"JOBS": (written or {}).get("path", ""), "DAMS_FOUND": len(info["hits"]),
                "RESULT": info["text"]}

    def _to_lonlat(self, folder, context, feedback):
        """x, y of the flood extent (the rasters' CRS) to longitude / latitude, or None when
        the extent is already in longitude / latitude."""
        dp = compareruns.depth_path(folder)
        wkt = ""
        if os.path.isfile(dp):
            try:
                from .core import gdal_io
                wkt = gdal_io.read_grid(dp).crs_wkt or ""
            except Exception as e:                               # noqa
                feedback.pushInfo("The depth raster's CRS could not be read ({}): the flood "
                                  "extent is taken as longitude / latitude.".format(e))
        crs = _raster_crs(wkt)
        if not wkt or not crs.isValid():
            return None
        tf = QgsCoordinateTransform(crs, QgsCoordinateReferenceSystem("EPSG:4326"),
                                    context.transformContext())

        def to_lonlat(x, y):
            p = tf.transform(QgsPointXY(float(x), float(y)))
            return p.x(), p.y()

        return to_lonlat


# ============================================================================ feature 15
class CompareRunsAlgorithm(_ToolBase):
    def name(self):
        return "compareruns"

    def displayName(self):
        return "Compare two runs (difference raster and what changed)"

    def shortHelpString(self):
        return ("Two finished runs side by side: what each one was (dam, scenario, grid, breach "
                "method, simulated time, from their run reports), a difference raster of the "
                "maximum depth (B minus A, feet; dry in both runs is no data) with its "
                "statistics, and - with a structures layer - a table of the structures whose "
                "ACER TM 11 danger zone is not the same in the two runs, and which way it "
                "moved.\n\n"
                "Use it to see what a change of grid size, Manning's n, breach parameters or "
                "DEM conditioning actually did. The coarser run is sampled onto the finer "
                "grid by nearest cell, so the difference raster is as fine as the finer run and "
                "a cell of it is not more accurate than the coarser run behind it.\n\n"
                "The zone columns need the ACER TM 11 charts signed off (ND Dam Hazard > Chart "
                "sign-off, or your own chart file); without them the depths and velocities are "
                "still compared and the table says why the zones are empty. Writes 'Run "
                "Comparison.md', '.json', 'Run Comparison (Structures).csv' and 'Depth "
                "Difference (B minus A).tif'.")

    def initAlgorithm(self, config=None):
        self._file("FOLDER_A", "Run A: dam folder (the run you are comparing against)",
                   folder=True, optional=False)
        self._file("FOLDER_B", "Run B: dam folder (the new run)", folder=True, optional=False)
        self.addParameter(QgsProcessingParameterFeatureSource(
            "STRUCTURES", "Structures (points, or polygons sampled at the centroid): which ones "
            "change danger zone", [SOURCE_POINT, SOURCE_POLYGON], optional=True))
        self._field("S_ID", "Structures: identifier field", "STRUCTURES", numeric=False,
                    advanced=True)
        self._field("S_MOBILE", "Structures: mobile-home field (any 'yes' / 'mobile' value "
                    "uses ND Fig. 5.2)", "STRUCTURES", numeric=False, advanced=True)
        self._file("CHARTS", "ACER TM 11 chart file (empty = the plugin's own "
                   "core/acer_tm11_charts.json)", "json")
        self._file("OUT_FOLDER", "Where to write the comparison (empty = '_compare' in run B's "
                   "folder)", folder=True, optional=True)
        self._bool("WRITE_DIFF", "Write the difference raster", True)
        self.addOutput(QgsProcessingOutputFile("REPORT", compareruns.MARKDOWN_NAME))
        self.addOutput(QgsProcessingOutputFile("CSV", compareruns.ROWS_NAME))
        self.addOutput(QgsProcessingOutputFile("DIFFERENCE", compareruns.DIFF_NAME))
        self.addOutput(QgsProcessingOutputFolder("FOLDER", "Comparison folder"))
        self.addOutput(QgsProcessingOutputString("RESULT", "Result"))

    def _points(self, parameters, context, grid, crs):
        src = self.parameterAsSource(parameters, "STRUCTURES", context)
        if src is None:
            return []
        names = qio.field_names(src)
        fld = _fields_by_name(self, parameters, context, names,
                              {"S_ID": STRUCT_ID_SYNONYMS, "S_MOBILE": MOBILE_FIELDS})
        to_r = QgsCoordinateTransform(src.sourceCrs(), crs, context.transformContext())
        to_l = QgsCoordinateTransform(crs, src.sourceCrs(), context.transformContext())
        center, radius = _box(grid)
        out = []
        for f in qio._features(src, center, radius, to_l):
            g = f.geometry()
            if g is None or g.isEmpty():
                continue
            try:
                p = to_r.transform(g.centroid().asPoint())
            except Exception:                                    # noqa - one structure
                continue
            out.append({"id": _txt(_attr_of(f, fld["S_ID"])) or str(f.id()),
                        "x": p.x(), "y": p.y(),
                        "mobile": nh.mobile_from_text(_txt(_attr_of(f, fld["S_MOBILE"])))
                        if hasattr(nh, "mobile_from_text")
                        else "mobile" in _txt(_attr_of(f, fld["S_MOBILE"])).lower()})
        return out

    def processAlgorithm(self, parameters, context, feedback):
        self._need_gdal()
        from .core import gdal_io
        a_folder = self.parameterAsFile(parameters, "FOLDER_A", context)
        b_folder = self.parameterAsFile(parameters, "FOLDER_B", context)
        if os.path.normpath(a_folder) == os.path.normpath(b_folder):
            raise QgsProcessingException("Run A and run B are the same folder")
        try:
            info, diff = compareruns.compare(a_folder, b_folder, gdal_io.read_grid)
        except (OSError, ValueError, RuntimeError) as e:
            raise QgsProcessingException(str(e))
        for w in info["warnings"]:
            feedback.pushWarning("Check: " + w)
        if diff is None:
            raise QgsProcessingException(
                "Both runs need '{}': {}".format(workflow.FINAL_NAMES["depth"],
                                                 "; ".join(info["warnings"]) or "not found"))
        # the structures, on the finer of the two grids (the difference raster's own grid)
        crs = _raster_crs(diff.crs_wkt)
        points = self._points(parameters, context, diff, crs)
        if points:
            charts = None
            try:
                charts = nh.load_charts(self._fileparam(parameters, "CHARTS", context) or None)
            except Exception as e:                               # noqa - unsigned or missing
                feedback.pushInfo("The chart file could not be read ({}): the depths and "
                                  "velocities are still compared.".format(e))
            a_f = {"depth": gdal_io.read_grid(compareruns.depth_path(a_folder)),
                   "velocity": self._maybe(gdal_io, compareruns.velocity_path(a_folder))}
            b_f = {"depth": gdal_io.read_grid(compareruns.depth_path(b_folder)),
                   "velocity": self._maybe(gdal_io, compareruns.velocity_path(b_folder))}
            rows, counts = compareruns.zone_changes(points, a_f, b_f, charts)
            info["rows"], info["counts"] = rows, counts
            if counts.get("chart_problem"):
                feedback.pushWarning("Check: the danger zones are empty: " +
                                     counts["chart_problem"])
        out_folder = self._fileparam(parameters, "OUT_FOLDER", context) or \
            os.path.join(b_folder, "_compare")
        writer = None
        if self.parameterAsBool(parameters, "WRITE_DIFF", context):
            writer = gdal_io.GeoTiffWriter(diff.crs_wkt or "EPSG:4326")
        try:
            paths = compareruns.write_outputs(out_folder, info, diff, writer)
        except OSError as e:
            raise QgsProcessingException("Could not write the comparison: {}".format(e))
        st = info["stats"]
        msg = ("B minus A on {} cell(s) of {:g} m: deepest increase {} ft, deepest decrease "
               "{} ft, mean change {} ft; flooded {} ac in A and {} ac in B; {} of {} "
               "structure(s) change danger zone".format(
                   st.get("cells_compared", 0), st.get("cell_m", 0),
                   st.get("max_increase_ft", 0), st.get("max_decrease_ft", 0),
                   st.get("mean_change_ft", 0), st.get("a_flooded_ac", 0),
                   st.get("b_flooded_ac", 0), info["counts"].get("changed", 0),
                   info["counts"].get("structures", 0)))
        feedback.pushInfo(msg)
        if paths.get("difference"):
            self.load(context, paths["difference"], "Depth difference (B minus A)")
        return {"REPORT": paths["markdown"], "CSV": paths["csv"],
                "DIFFERENCE": paths.get("difference", ""), "FOLDER": out_folder,
                "RESULT": msg}

    @staticmethod
    def _maybe(gdal_io, path):
        if not os.path.isfile(path):
            return None
        try:
            return gdal_io.read_grid(path)
        except Exception:                                        # noqa - velocity is optional
            return None


# ============================================================================ feature 16
class QueueAlgorithm(_Base):
    def name(self):
        return "queue"

    def displayName(self):
        return "Run a queue of dams from a list (cancellable, resumable)"

    def shortHelpString(self):
        return ("Runs 'Run breach model for one dam' for each dam of a list, keeping a queue "
                "file so the work can be stopped and picked up again. The list is a CSV with a "
                "'Dam ID' column (the screening plugin's priority list works as it is) or a "
                "plain text file with one dam ID per line; the order is kept and repeats are "
                "dropped. Every dam's own values come from its dam.json in "
                "<Dams folder>/<Dam ID>/, which is what 'Get data for this dam' writes, so no "
                "dam layer is needed.\n\n"
                "Cancel and resume: '_queue/breach_queue.json' in the dams folder records each "
                "dam as waiting, running, done, skipped or failed. Cancel the tool and the dams "
                "that finished stay done; run it again with the same list and it carries on "
                "with the rest. 'Start again' clears the record first. A dam that fails is "
                "recorded with its message and the queue goes on.\n\n"
                "Two dams run side by side at one thread each by default, which measured 1.76x "
                "faster than one dam on two threads on the machine this was written on; more "
                "workers than cores is slower than fewer, and the log says so. This is a speed "
                "setting only: it changes nothing about any result.")

    def initAlgorithm(self, config=None):
        self._file("DAM_LIST", "List of dams: CSV with a 'Dam ID' column, or one dam ID per "
                   "line", optional=False)
        self.add_breach_params(per_dam=False)
        self.add_scenario_params(per_dam=False)
        self.add_feature_params()
        self.add_model_params()
        self._bool("SKIP_DONE", "Skip dams whose folder already holds a finished run", True)
        self._bool("RESTART", "Start again: clear the queue record before running", False)
        self._num("WORKERS", "Dams to run at the same time (0 = one per CPU core; the starting "
                  "value {} at one thread each measured 1.76x faster than one dam on two "
                  "threads)".format(batchqueue.DEFAULT_WORKERS), batchqueue.DEFAULT_WORKERS,
                  minimum=0, integer=True)
        self.addOutput(QgsProcessingOutputFile("QUEUE", batchqueue.QUEUE_NAME))
        self.addOutput(QgsProcessingOutputFile("SUMMARY", "Queue summary (CSV)"))
        self.addOutput(QgsProcessingOutputString("COUNTS", "Counts"))

    def flags(self):
        return super().flags() | ALG_NO_THREADING

    def _one(self, dam_id, parameters, context, feedback, fb):
        """(dam, args) for workflow.run_dam, prepared in this thread, or raises
        QgsProcessingException / BreachInputError. A fresh RunDamAlgorithm instance per dam:
        'Run breach model for one dam' is the definition of what one dam means, and nothing
        about it is reimplemented here."""
        alg = RunDamAlgorithm()
        alg.initAlgorithm()
        p = dict(parameters)
        p["DAM_ID"] = dam_id
        for k in ("DAM_LIST", "WORKERS", "RESTART", "SKIP_DONE"):
            p.pop(k, None)
        # 3.21.0: the set-up runs in this (main) thread, so it gets the real QGIS feedback;
        # `fb` is the adapter the model's own callbacks use in the worker thread. Passing `fb`
        # here used to raise AttributeError on prepare's pushInfo and stop the queue on dam 1.
        dam, st, src, writer, dam_xy, inflow, folder = alg.prepare(p, context, feedback)
        st.dam_record = dict(getattr(alg, "_rec_info", None) or {})
        return dam, (dam, dam_xy, st, src, writer, folder, inflow)

    def processAlgorithm(self, parameters, context, feedback):
        dams_folder = self.parameterAsFile(parameters, "DAMS_FOLDER", context)
        if not dams_folder:
            raise QgsProcessingException("Choose the dams folder")
        list_path = self._fileparam(parameters, "DAM_LIST", context)
        try:
            dam_ids, notes = batchqueue.read_list(list_path)
        except (OSError, ValueError) as e:
            raise QgsProcessingException("The list of dams could not be read: {}".format(e))
        for n in notes:
            feedback.pushInfo(n)
        if not dam_ids:
            raise QgsProcessingException("The list holds no dam ID: give a CSV with a 'Dam ID' "
                                         "column or one ID per line.")
        raw_workers = self.parameterAsInt(parameters, "WORKERS", context)
        workers = raw_workers if raw_workers > 0 else max((os.cpu_count() or 2) - 1, 1)
        if self.parameterAsBool(parameters, "RESTART", context):
            try:
                os.remove(batchqueue.queue_path(dams_folder))
                feedback.pushInfo("Start again: the previous queue record was removed.")
            except OSError:
                pass
        skip = self.parameterAsBool(parameters, "SKIP_DONE", context)
        q, note = batchqueue.resume(dams_folder, dam_ids,
                                    os.path.basename(list_path or ""), workers,
                                    version=workflow.VERSION)
        feedback.pushInfo(note)
        todo = batchqueue.pending(q, batchqueue.done_checker(dams_folder) if skip else None)
        feedback.pushInfo(batchqueue.summary_text(q))
        feedback.pushInfo(batchqueue.worker_note(workers))
        if not todo:
            path = batchqueue.save(q)
            return {"QUEUE": path, "SUMMARY": self._csv(q, dams_folder),
                    "COUNTS": "Nothing to do: " + batchqueue.summary_text(q)}
        lock = threading.Lock()

        def make_fb(parallel):
            f = _Feedback(feedback)
            if parallel:
                inner = f.log
                def safe(msg):
                    with lock:
                        inner(msg)
                f.log = safe
                f.progress = lambda pct, msg="": None
            return f

        ex = ThreadPoolExecutor(max_workers=workers) if workers > 1 else None
        pending = []                       # (future, dam_id, t0)
        try:
            for i, dam_id in enumerate(todo):
                if feedback.isCanceled():
                    feedback.pushInfo("Cancelled: the dams already finished stay done; run the "
                                      "tool again with the same list to carry on.")
                    break
                feedback.setProgress(100.0 * i / max(1, len(todo)))
                feedback.pushInfo("=== {} ({}/{})".format(dam_id, i + 1, len(todo)))
                batchqueue.mark(q, dam_id, batchqueue.RUNNING)
                batchqueue.save(q)
                t0 = time.time()
                fb = make_fb(ex is not None)
                try:
                    _dam, args = self._one(dam_id, parameters, context, feedback, fb)
                except (QgsProcessingException, brmod.BreachInputError, ValueError) as e:
                    self._failed(q, dam_id, str(e), t0, feedback)
                    continue
                except Exception as e:                    # 3.21.0: one dam's set-up error is
                    # recorded as failed and the queue carries on, not an uncaught traceback
                    # that stops the whole tool (and leaves the dam marked "running").
                    self._failed(q, dam_id, "set-up failed: {}".format(e), t0, feedback)
                    continue
                if ex is None:
                    self._finish(q, dam_id, args, fb, t0, feedback)
                else:
                    pending.append((ex.submit(workflow.run_dam, args[0], args[1], args[2],
                                              args[3], args[4], args[5], args[6], fb.progress,
                                              fb.log, fb.cancel), dam_id, t0))
                    if len(pending) >= workers:
                        self._collect(q, pending, feedback, keep=workers - 1)
            if pending:
                self._collect(q, pending, feedback, keep=0)
        finally:
            if ex is not None:
                ex.shutdown(wait=True)
            batchqueue.save(q)
        text = batchqueue.summary_text(q)
        feedback.pushInfo(text)
        return {"QUEUE": batchqueue.save(q), "SUMMARY": self._csv(q, dams_folder),
                "COUNTS": text}

    # -- the queue bookkeeping ------------------------------------------------------
    def _csv(self, q, dams_folder):
        path = os.path.join(dams_folder, batchqueue.QUEUE_FOLDER, "Queue_Summary.csv")
        try:
            return batchqueue.write_csv(path, q)
        except OSError:
            return ""

    def _failed(self, q, dam_id, message, t0, feedback):
        batchqueue.mark(q, dam_id, batchqueue.FAILED, message,
                        round((time.time() - t0) / 60.0, 1))
        batchqueue.save(q)
        feedback.reportError("{}: {}".format(dam_id, message), fatalError=False)

    def _record(self, q, dam_id, res, t0, feedback):
        minutes = round((time.time() - t0) / 60.0, 1)
        s = res.summary
        if not s.get("breach_occurs", True):
            batchqueue.mark(q, dam_id, batchqueue.SKIPPED,
                            "the dam does not fail in this flood", minutes)
        else:
            batchqueue.mark(q, dam_id, batchqueue.DONE,
                            "peak {} cfs, {} ac flooded".format(s.get("peak_breach_cfs", ""),
                                                                s.get("flooded_acres", "")),
                            minutes)
        for w in res.warnings:
            feedback.pushWarning("{}: {}".format(dam_id, w))
        batchqueue.save(q)

    def _finish(self, q, dam_id, args, fb, t0, feedback):
        try:
            res = workflow.run_dam(args[0], args[1], args[2], args[3], args[4], args[5],
                                   args[6], fb.progress, fb.log, fb.cancel)
        except Cancelled:
            batchqueue.mark(q, dam_id, batchqueue.WAITING, "cancelled")
            batchqueue.save(q)
            return
        except (ModelError, brmod.BreachInputError, OSError, ValueError, RuntimeError) as e:
            self._failed(q, dam_id, "{}: {}".format(type(e).__name__, e), t0, feedback)
            return
        self._record(q, dam_id, res, t0, feedback)

    def _collect(self, q, pending, feedback, keep=0):
        """Wait until at most `keep` runs are still going, recording each one as it ends."""
        while len(pending) > keep:
            fut, dam_id, t0 = pending.pop(0)
            try:
                res = fut.result()
            except Cancelled:
                batchqueue.mark(q, dam_id, batchqueue.WAITING, "cancelled")
                batchqueue.save(q)
                continue
            except Exception as e:                               # noqa - one dam of many
                self._failed(q, dam_id, "{}: {}".format(type(e).__name__, e), t0, feedback)
                continue
            self._record(q, dam_id, res, t0, feedback)


# ============================================================================ feature 12
class SignRatesAlgorithm(_ToolBase):
    def name(self):
        return "signrates"

    def displayName(self):
        return "Sign off the fatality rate table (loss of life)"

    def shortHelpString(self):
        return ("Writes the fatality rate table the consequences step uses to turn population "
                "at risk into a loss-of-life estimate, with the name of the engineer who signs "
                "it off, the source it was taken from and the date. It ships EMPTY and "
                "UNSIGNED, and until it is signed off every report says that loss of life is "
                "not estimated and why. The tool records the names it is given; it cannot "
                "confirm that a check was made, and it invents no numbers.\n\n"
                "A rate is a fraction of the population at risk, 0 to 1, for one flood "
                "severity class (high, medium, low) and one warning band (under15, 15to60, "
                "over60 minutes): nine cells in all. Give them as 'high/under15=0.9, "
                "high/15to60=0.5, ...'. A percentage like 75 is refused rather than read as "
                "7500%. A table that is signed off but has an empty cell still produces no "
                "number: a total that left those people out would be too low.\n\n"
                "Population at risk, the severity split and the warning-time split are reported "
                "whether or not the table is signed, because they need no rate. The numbers "
                "from this table are reported BESIDE the consequences tool's own loss-of-life "
                "estimate, never instead of it.")

    def initAlgorithm(self, config=None):
        self._text("RATES", "Fatality rates: 'severity/warning band = fraction', comma or line "
                   "separated, e.g. 'high/under15 = 0.9, medium/15to60 = 0.03' (fractions of "
                   "the population at risk, 0 to 1)", optional=True)
        self._text("SOURCE", "Where the rates come from: the publication, its edition and the "
                   "table in it", optional=True)
        self._text("SIGNED_OFF_BY", "Signed off by (the engineer taking responsibility; empty "
                   "= written as a draft, not signed)", optional=True)
        self._text("CHECKED_BY", "Checked by (optional; one engineer is enough)", optional=True)
        self._text("SIGNED_ON", "Signed off on (YYYY-MM-DD; empty = today)", optional=True)
        self._text("NOTES", "Notes recorded in the file", optional=True)
        self.addParameter(QgsProcessingParameterFileDestination(
            "OUTPUT", "Rate table file (empty = the plugin's own core/fatality_rates.json)",
            "JSON files (*.json)", optional=True))
        self.addOutput(QgsProcessingOutputFile("RATE_FILE", lifeloss.FILE_NAME))
        self.addOutput(QgsProcessingOutputString("STATUS", "Sign-off status"))
        self.addOutput(QgsProcessingOutputString("RESULT", "Result"))

    @staticmethod
    def parse_text(text):
        """({"severity/band": value}, [lines that made no sense]) from the parameter text."""
        out, bad = {}, []
        for part in str(text or "").replace("\n", ",").replace(";", ",").split(","):
            t = part.strip()
            if not t:
                continue
            key, _sep, val = t.partition("=")
            if not _sep:
                key, _sep, val = t.partition(":")
            if not _sep or not key.strip() or not val.strip():
                bad.append(t)
                continue
            out[key.strip()] = val.strip()
        return out, bad

    def processAlgorithm(self, parameters, context, feedback):
        path = self.parameterAsFileOutput(parameters, "OUTPUT", context) or lifeloss.plugin_file()
        rates, unreadable = self.parse_text(self._str(parameters, "RATES", context))
        for t in unreadable:
            feedback.reportError("Not a rate ('severity/band = fraction'): {}".format(t),
                                 fatalError=False)
        when = self._str(parameters, "SIGNED_ON", context)
        if when:
            try:
                datetime.date.fromisoformat(when)
            except ValueError:
                raise QgsProcessingException("'Signed off on' is not a date (YYYY-MM-DD): "
                                             "{}".format(when))
        try:
            r = lifeloss.write_rate_file(
                path, rates or None, self._str(parameters, "SIGNED_OFF_BY", context),
                self._str(parameters, "CHECKED_BY", context),
                self._str(parameters, "SOURCE", context), when,
                self._str(parameters, "NOTES", context))
        except OSError as e:
            raise QgsProcessingException("Could not write the rate table: {}".format(e))
        for b in r["bad"]:
            feedback.reportError("Refused: " + b, fatalError=False)
        table, problem = lifeloss.load(r["path"])
        if problem:
            feedback.reportError(problem, fatalError=False)
        usable, text = lifeloss.status_text(table or {})
        feedback.pushInfo("{} ({}): {}".format(os.path.basename(r["path"]), r["status"],
                                               r["reason"]))
        feedback.pushInfo("SHA-256 {}".format(r["sha256"]))
        if r["empty_cells"]:
            feedback.pushWarning("Check: {} of 9 cell(s) are empty ({}).".format(
                len(r["empty_cells"]), ", ".join(r["empty_cells"])))
        if usable:
            feedback.pushInfo("Loss of life will be estimated from this table.")
        else:
            feedback.pushWarning("Check: " + text)
            feedback.pushInfo(lifeloss.NOT_ESTIMATED)
        return {"RATE_FILE": r["path"], "STATUS": r["status"], "RESULT": text}


RELEASE320_ALGORITHMS = (CrossingsAlgorithm, CascadeCheckAlgorithm, CompareRunsAlgorithm,
                         QueueAlgorithm, SignRatesAlgorithm)
