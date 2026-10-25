"""HEC-RAS interchange algorithms: export a breach to HEC-RAS, import 2D results as rasters.

Register with `HECRAS_ALGORITHMS` (see the end of the file)."""
import os

import numpy as np
from qgis.core import (QgsProcessingContext, QgsProcessingException, QgsProcessingOutputFile,
                       QgsProcessingOutputFolder, QgsProcessingOutputString,
                       QgsProcessingParameterBoolean, QgsProcessingParameterCrs,
                       QgsProcessingParameterEnum, QgsProcessingParameterExtent,
                       QgsProcessingParameterRasterLayer)

from .algorithms_tools import _ToolBase
from .core import benchmark, gdal_io, hecras, hecras_model, workflow

STEPS = [(hecras.ras_interval_label(m), m) for m in hecras.RAS_INTERVALS_MIN]
STEP_DEFAULT = [m for _, m in STEPS].index(5)
IMPORT_NAMES = hecras.IMPORT_NAMES
US_SI = ["Detect from the file (US customary when it says nothing)", "US customary (ft)",
         "SI metric (m)"]


class HecRasExportAlgorithm(_ToolBase):
    def name(self):
        return "hecrasexport"

    def displayName(self):
        return "Export the breach to HEC-RAS (hydrograph and breach table)"

    def shortHelpString(self):
        return ("Reads a finished run's breach hydrograph and parameters from its dam folder "
                "and writes (1) the hydrograph as an unsteady-flow text block for a HEC-RAS "
                "'.u##' file ('Flow Hydrograph= N', 10 values a line, 8-character fields) and "
                "as a CSV, on the time step you choose (the peak is kept exactly), and (2) "
                "the values HEC-RAS's breach editor asks for (final bottom elevation and "
                "width, side slopes, weir coefficient, formation time, trigger) as CSV and "
                "markdown. The values marked 'Plugin estimate' come from regression equations "
                "and are not design values; the breach center station is left blank. Use the "
                "same data interval in the HEC-RAS boundary condition as the step chosen "
                "here. Output goes to '<dam folder>/_hecras' unless you choose a folder. For "
                "a complete HEC-RAS model (reservoir, dam with the breach and 2D area) use "
                "'Build a HEC-RAS 2D dam-breach model from the run'.")

    def initAlgorithm(self, config=None):
        self._file("DAM_FOLDER", "Dam folder of a finished run", folder=True, optional=False)
        self.addParameter(QgsProcessingParameterEnum(
            "STEP", "Hydrograph time step (HEC-RAS data interval)", [s[0] for s in STEPS],
            defaultValue=STEP_DEFAULT))
        self._file("OUT_FOLDER", "Output folder (empty = <dam folder>/_hecras)", folder=True)
        self.addOutput(QgsProcessingOutputFile("HYDROGRAPH_TXT", "Hydrograph, .u## block"))
        self.addOutput(QgsProcessingOutputFile("HYDROGRAPH_CSV", "Hydrograph CSV"))
        self.addOutput(QgsProcessingOutputFile("BREACH_TABLE", "Breach parameters CSV"))
        self.addOutput(QgsProcessingOutputFile("BREACH_TABLE_MD", "Breach parameters markdown"))
        self.addOutput(QgsProcessingOutputString("RESULT", "Result"))

    def processAlgorithm(self, parameters, context, feedback):
        folder = self.parameterAsFile(parameters, "DAM_FOLDER", context)
        step = STEPS[self.parameterAsEnum(parameters, "STEP", context)]
        out = self._fileparam(parameters, "OUT_FOLDER", context) or None
        try:
            r = hecras.export_run(folder, out, step[1], workflow.MODEL_FOLDER)
        except (hecras.HecRasError, OSError, ValueError) as e:
            raise QgsProcessingException(str(e))
        i = r["info"]
        msg = ("{} values every {}; peak {:,.0f} cfs at {:.2f} h (the record's peak is at "
               "{:.2f} h); volume {:,.1f} ac-ft ({:+.2f}% against the record)".format(
                   i["n"], step[0], i["peak_cfs"], i["peak_time_exported_hr"], i["peak_time_hr"],
                   i["volume_exported_acft"], i["volume_change_pct"]))
        feedback.pushInfo(msg)
        feedback.pushInfo("Set the boundary condition's data interval to {}.".format(step[0]))
        for n in i["notes"]:
            feedback.pushWarning("Check: " + n)
        feedback.reportError("Check: the breach values are the plugin's estimates; the center "
                             "station is blank. See the basis column.", fatalError=False)
        return {"HYDROGRAPH_TXT": r["hydrograph_txt"], "HYDROGRAPH_CSV": r["hydrograph_csv"],
                "BREACH_TABLE": r["table_csv"], "BREACH_TABLE_MD": r["table_md"],
                "RESULT": msg}


class HecRasImportAlgorithm(_ToolBase):
    def name(self):
        return "hecrasimport"

    def displayName(self):
        return "Import HEC-RAS 2D results as rasters"

    def shortHelpString(self):
        return ("Reads the maximum results of a HEC-RAS 2D plan HDF (.p##.hdf) and writes "
                "GeoTIFFs 'HECRAS Depth (Max).tif', 'HECRAS Velocity (Max).tif', 'HECRAS "
                "Arrival Time.tif' and 'HECRAS WSE (Max).tif' in ft, ft/s, hours and ft. Give the "
                "terrain the model used: depth is then the cell's maximum water surface minus "
                "the terrain under each grid cell, and the wet area and arrival time follow "
                "that depth. Without a terrain, depth is the maximum water surface minus the "
                "cell's minimum elevation, which gives a cell's ditch depth to the whole cell "
                "(too deep and too wide on coarse cells). Velocity is "
                "the per-cell maximum when the file has one, else the maximum face velocities "
                "averaged onto the cells. Arrival is the first time the depth reaches the "
                "arrival depth, from the water surface time series, counted from the "
                "'time of breach initiation' you give (hours after the start of the HEC-RAS "
                "run). Cells are sampled by nearest cell centre onto the grid of the 'like' "
                "raster (or of the dam folder's Depth (Max).tif), else onto a grid you set, "
                "else onto one at the median cell spacing. Coordinates must be in the "
                "same system as the like raster. Needs h5py in QGIS's Python. Tick 'compare' "
                "to run the benchmark against the dam folder's rasters. The layout of HDF "
                "files varies between HEC-RAS versions and was not tested against real "
                "files: if it cannot find the datasets it lists what the file contains.")

    def initAlgorithm(self, config=None):
        self._file("HDF", "HEC-RAS 2D plan results (.p##.hdf)", "hdf", optional=False)
        self._file("DAM_FOLDER", "Dam folder (its Depth (Max).tif sets the grid; outputs "
                   "go to <dam folder>/_hecras)", folder=True)
        self.addParameter(QgsProcessingParameterRasterLayer(
            "TERRAIN", "Terrain the HEC-RAS model used (depth = water surface minus this "
            "terrain; empty = the cell's minimum elevation)", optional=True))
        self.addParameter(QgsProcessingParameterRasterLayer(
            "LIKE", "Grid of this raster (empty = the dam folder's, else set below)",
            optional=True))
        self._num("CELL", "Cell size when there is no raster to match (in the plan's CRS "
                  "units, usually US ft or m; 0 = median cell spacing)", 0.0, minimum=0.0)
        self.addParameter(QgsProcessingParameterExtent(
            "EXTENT", "Extent when there is no raster to match (empty = the 2D area)",
            optional=True))
        self._text("AREA", "2D flow area name (empty = the largest)")
        self.addParameter(QgsProcessingParameterEnum("UNITS", "HEC-RAS model units", US_SI,
                                                     defaultValue=0))
        self._num("ARRIVAL_FT", "Arrival depth (ft; starting value 1 is the engineer's, a "
                  "screening value you choose)", 1.0, minimum=0.01)
        self._num("T0_HR", "Time of breach initiation in the HEC-RAS run (hr after its start)",
                  0.0, minimum=0.0)
        self.addParameter(QgsProcessingParameterBoolean(
            "COMPARE", "Compare with the dam folder's rasters (benchmark)", False))
        self._num("THRESH_FT", "Benchmark: wet where the depth is at least (ft; starting "
                  "value 1 is the engineer's, a screening value you choose)", 1.0,
                  minimum=0.01)
        self._file("OUT_FOLDER", "Output folder (empty = <dam folder>/_hecras)", folder=True)
        self.addOutput(QgsProcessingOutputFolder("FOLDER", "Output folder"))
        self.addOutput(QgsProcessingOutputFile("DEPTH", "HECRAS Depth (Max)"))
        self.addOutput(QgsProcessingOutputFile("SUMMARY", "Benchmark_Summary.csv"))
        self.addOutput(QgsProcessingOutputString("RESULT", "Result"))

    def processAlgorithm(self, parameters, context, feedback):
        self._need_gdal()
        hdf = self.parameterAsFile(parameters, "HDF", context)
        dam_folder = self._fileparam(parameters, "DAM_FOLDER", context)
        out_dir = self._fileparam(parameters, "OUT_FOLDER", context) or (
            os.path.join(dam_folder, "_hecras") if dam_folder else "")
        if not out_dir:
            raise QgsProcessingException("Give a dam folder or an output folder")
        like = self.parameterAsRasterLayer(parameters, "LIKE", context)
        depth_path = os.path.join(dam_folder, workflow.FINAL_NAMES["depth"]) if dam_folder \
            else ""
        compare = self.parameterAsBool(parameters, "COMPARE", context)
        if compare and not os.path.exists(depth_path):
            raise QgsProcessingException("To compare, the dam folder needs '{}'".format(
                workflow.FINAL_NAMES["depth"]))
        like_grid = None
        try:
            if like is not None:
                like_grid = gdal_io.read_grid(like.source())
            elif os.path.exists(depth_path):
                like_grid = gdal_io.read_grid(depth_path)
            kw = {"units": (None, "us", "si")[self.parameterAsEnum(parameters, "UNITS",
                                                                     context)],
                  "arrival_depth_ft": self.parameterAsDouble(parameters, "ARRIVAL_FT", context),
                  "t0_hr": self.parameterAsDouble(parameters, "T0_HR", context)}
            mesh = hecras.read_mesh(hdf, area=self._str(parameters, "AREA", context) or None,
                                    **kw)
            if like_grid is not None:
                x0, ytop, dx, nx, ny = (like_grid.x0, like_grid.y0, like_grid.dx,
                                        like_grid.nx, like_grid.ny)
            else:
                ext = None
                if parameters.get("EXTENT"):
                    r = self.parameterAsExtent(parameters, "EXTENT", context)
                    if r is not None and not r.isNull():
                        ext = (r.xMinimum(), r.yMinimum(), r.xMaximum(), r.yMaximum())
                x0, ytop, dx, nx, ny = hecras.grid_spec(
                    mesh, self._opt(parameters, "CELL", context) or None, ext)
            crs = (like_grid.crs_wkt if like_grid is not None else None) or mesh.crs_wkt
            if not crs:
                raise QgsProcessingException(
                    "The HDF file records no projection; give a raster to match so the "
                    "rasters can be georeferenced.")
            from .core.grid import Grid
            grid = Grid(np.zeros((ny, nx)), x0, ytop, dx, crs_wkt=crs)
            terrain = self.parameterAsRasterLayer(parameters, "TERRAIN", context)
            if terrain is not None:
                # 3.26.1: depth, wet area and arrival from the terrain of each grid cell
                t_ft = hecras.terrain_on_grid(terrain.source(), grid)
                arrays, tinfo = hecras.terrain_depth(
                    hdf, x0, ytop, dx, nx, ny, t_ft,
                    area=self._str(parameters, "AREA", context) or None, **kw)
                tinfo.pop("old", None)
                mesh.info = tinfo
            else:
                arrays = hecras.rasterise(mesh, x0, ytop, dx, nx, ny)
                mesh.info["notes"].append(
                    "No terrain given: depth is the water surface minus each cell's minimum "
                    "elevation, so a ditch deepens its whole cell.")
        except (hecras.HecRasError, OSError, ValueError, RuntimeError) as e:
            raise QgsProcessingException(str(e))
        writer = gdal_io.GeoTiffWriter(crs)
        os.makedirs(out_dir, exist_ok=True)
        paths = {}
        for k, a in arrays.items():
            paths[k] = os.path.join(out_dir, IMPORT_NAMES[k])
            writer(grid.like(a, np.nan), paths[k])
            self.load(context, paths[k], IMPORT_NAMES[k][:-4])
        info = mesh.info
        feedback.pushInfo("Area '{}', {:,} cells, units read as {}; grid {} x {} at {:g}.".format(
            info["area"], info["n_cells"], info["units"], nx, ny, dx))
        feedback.pushInfo("Used: " + "; ".join("{}: {}".format(k, v) for k, v
                                               in info["used"].items()))
        for n in info["notes"]:
            feedback.pushWarning("Check: " + n)
        summary, msg = "", "wrote {}".format(", ".join(sorted(arrays)))
        if compare:
            try:
                summary, verdict = self._compare(parameters, context, dam_folder, grid, arrays,
                                                 out_dir, writer, feedback)
            except (ValueError, RuntimeError, OSError) as e:
                raise QgsProcessingException(str(e))
            msg += "; " + verdict
        return {"FOLDER": out_dir, "DEPTH": paths.get("depth", ""), "SUMMARY": summary,
                "RESULT": msg}

    def _compare(self, parameters, context, dam_folder, grid, arrays, out_dir, writer,
                 feedback):
        """This tool's rasters in the dam folder against the imported ones (same grid)."""
        names = workflow.FINAL_NAMES
        ours, ref = {}, {}
        for kind in ("depth", "velocity", "arrival", "wse"):
            p = os.path.join(dam_folder, names[kind])
            if os.path.exists(p) and kind in arrays:
                ours[kind] = gdal_io.read_like(p, grid, "near")
                ref[kind] = arrays[kind]
        unit = gdal_io.linear_unit_m(grid.crs_wkt) or 1.0
        cell_ac = (grid.dx * unit) ** 2 / 4046.8564224
        thr = self.parameterAsDouble(parameters, "THRESH_FT", context)
        m = benchmark.compare_fields(ours, ref, cell_ac, thr)
        r = benchmark.write_outputs(os.path.join(out_dir, "benchmark"), m,
                                    benchmark.difference_arrays(ours, ref, thr), writer, grid,
                                    "Benchmark against HEC-RAS")
        feedback.pushInfo(m["verdict"])
        for p in r["rasters"].values():
            self.load(context, p, os.path.basename(p)[:-4])
        return r["csv"], m["verdict"]


EQUATIONS = [hecras_model.EQ_SWE, hecras_model.EQ_DWE]


class HecRasModelAlgorithm(_ToolBase):
    def name(self):
        return "hecrasmodel"

    def displayName(self):
        return "Build a HEC-RAS 2D dam-breach model from the run"

    def shortHelpString(self):
        return ("Writes a complete HEC-RAS 6.x project for a finished run, laid out like "
                "HEC's tutorial 'Dam Breach - Simple 2D Geometry': the reservoir is a storage "
                "area with the plugin's elevation-volume curve, the dam is an SA/2D "
                "connection (a weir at the top of dam along the dam line) carrying the "
                "breach, and the valley below is a 2D flow area. The 2D area is the run's "
                "flooded extent widened by the side buffer, with the end buffer added at the "
                "downstream end of the inundation, cut straight along the dam line. A normal "
                "depth boundary sits where the valley leaves it. The run's culverts, bridges "
                "and notches (its culvert / bridge line layer, as the run report recorded "
                "them) that lie in the 2D area become connections inside the 2D area: a weir "
                "along the road from the terrain, with a culvert group of the run's size for "
                "a culvert and an open gap for a bridge or notch; the README lists each one, "
                "the ones left out and why, and what to check in HEC-RAS. Files: project (.prj), "
                "geometry (.g01, with the 2D computation points), unsteady flow (.u01), plan "
                "(.p01, with the breach), RAS Mapper file, the projection, the DEM as a "
                "GeoTIFF in the model's coordinate system and vertical units, a GeoPackage of "
                "the layout to check here, and README_HECRAS.md with every value, where it "
                "came from and the steps left in HEC-RAS (make the RAS terrain from the "
                "GeoTIFF, associate it, compute). Units follow the coordinate system: feet "
                "gives an English-units model, metres an SI model. Empty coordinate system: "
                "the DEM's when it is in feet, else North Dakota State Plane (international "
                "feet) for a dam in North Dakota, else the run's. Output goes to "
                "'<dam folder>/_hecras/model' unless you choose a folder. Not tested in "
                "HEC-RAS itself; the text files follow HEC's example projects.")

    def initAlgorithm(self, config=None):
        self._file("DAM_FOLDER", "Dam folder of a finished run", folder=True, optional=False)
        self.addParameter(QgsProcessingParameterRasterLayer(
            "DEM", "DEM (empty = the run's)", optional=True))
        self.addParameter(QgsProcessingParameterCrs(
            "CRS", "HEC-RAS coordinate system (empty = automatic, see help)", optional=True))
        self._num("SIDE_BUFFER_FT", "Buffer on the sides of the flooded area (ft)",
                  hecras_model.DEFAULTS["side_buffer_ft"], minimum=0.0)
        self._num("END_BUFFER_FT", "Buffer at the downstream end of the inundation (ft)",
                  hecras_model.DEFAULTS["end_buffer_ft"], minimum=0.0)
        self._num("CELL_FT", "2D cell size (ft; 0 = about the target number of cells)", 0.0,
                  minimum=0.0)
        self._num("TARGET_CELLS", "Target number of 2D cells when the cell size is 0",
                  hecras_model.DEFAULTS["target_cells"], minimum=1000, integer=True)
        self.addParameter(QgsProcessingParameterEnum(
            "EQUATION", "2D equation set",
            [hecras_model.EQUATIONS[k][1] for k in EQUATIONS], defaultValue=0))
        self._num("BASE_FLOW_CFS", "Inflow to the reservoir in a sunny-day run (cfs)", 0.0,
                  minimum=0.0)
        # v3.12.0: the run's culverts, bridges and notches
        self.addParameter(QgsProcessingParameterBoolean(
            "STRUCTURES", "Put the run's culverts, bridges and notches in the 2D area "
            "(connections inside it; not checked in HEC-RAS, see the README)", True))
        # v3.23.0: breaklines along the dam crest and road embankments
        self.addParameter(QgsProcessingParameterBoolean(
            "BREAKLINES", "Add breaklines along the dam crest and road embankments so the "
            "mesh faces line up on them when the computation points are regenerated", True))
        self._file("OUT_FOLDER", "Output folder (empty = <dam folder>/_hecras/model)",
                   folder=True)
        self.addOutput(QgsProcessingOutputFolder("FOLDER", "HEC-RAS project folder"))
        self.addOutput(QgsProcessingOutputFile("PROJECT", "HEC-RAS project file"))
        self.addOutput(QgsProcessingOutputFile("LAYOUT", "Layout GeoPackage"))
        self.addOutput(QgsProcessingOutputFile("README", "README_HECRAS.md"))
        self.addOutput(QgsProcessingOutputString("RESULT", "Result"))

    def processAlgorithm(self, parameters, context, feedback):
        self._need_gdal()
        folder = self.parameterAsFile(parameters, "DAM_FOLDER", context)
        dem = self.parameterAsRasterLayer(parameters, "DEM", context)
        crs = self.parameterAsCrs(parameters, "CRS", context)
        crs_text = None
        if crs is not None and crs.isValid():
            crs_text = crs.authid() or crs.toWkt()
        opts = {"side_buffer_ft": self.parameterAsDouble(parameters, "SIDE_BUFFER_FT", context),
                "end_buffer_ft": self.parameterAsDouble(parameters, "END_BUFFER_FT", context),
                "cell_ft": self.parameterAsDouble(parameters, "CELL_FT", context),
                "target_cells": self.parameterAsInt(parameters, "TARGET_CELLS", context),
                "equation": EQUATIONS[self.parameterAsEnum(parameters, "EQUATION", context)],
                "base_flow_cfs": self.parameterAsDouble(parameters, "BASE_FLOW_CFS", context),
                "structures": self.parameterAsBool(parameters, "STRUCTURES", context),  # v3.12.0
                "breaklines": self.parameterAsBool(parameters, "BREAKLINES", context)}  # v3.23.0
        out = self._fileparam(parameters, "OUT_FOLDER", context) or None
        try:
            r = hecras_model.build_model(
                folder, out, dem.source() if dem is not None else None, crs_text, opts,
                log=feedback.pushInfo,
                progress=lambda p, m="": (feedback.setProgress(p), feedback.setProgressText(m)))
        except (hecras.HecRasError, hecras_model.HecRasModelError, OSError, ValueError,
                RuntimeError) as e:
            raise QgsProcessingException(str(e))
        i = r["info"]
        msg = ("{units} model in {crs}: 2D area of {cells:,} cells of {cell:g} {L}, dam line "
               "{dam:,.0f} {L}, time step {dt}, {sim:g} h; open {prj}".format(
                   units=i["units"], crs=i["crs"], cells=i["cells"], cell=i["cell"],
                   L=i["len_name"],
                   dam=i["dam_line"], dt=i["comp_interval"], sim=i["sim_hours"],
                   prj=os.path.basename(r["paths"]["prj"])))
        si = i.get("structures") or {}                  # v3.12.0: culverts, bridges, notches
        if si.get("exported"):
            msg += "; {} culvert / bridge connection(s) in the 2D area".format(
                len(si["exported"]))
        bls = i.get("breaklines") or []                 # v3.23.0: breaklines
        if bls:
            msg += "; {} breakline(s)".format(len(bls))
        feedback.pushInfo(msg)
        for n in r["notes"]:
            feedback.pushWarning("Check: " + n)
        layers = []
        if si.get("exported"):
            feedback.pushInfo("Culverts, bridges and notches in the 2D area: {}. Not checked in "
                              "HEC-RAS: see README_HECRAS.md.".format(", ".join(
                                  c["name"] for c in si["exported"])))
            layers.append("Structure connections")
        for sk in si.get("skipped") or []:
            feedback.reportError("Check: {} ({}) is not in the HEC-RAS model: {}".format(
                sk["name"], sk["kind"], sk["why"]), fatalError=False)
        if si.get("exported") or si.get("skipped"):
            layers.append("Structures from the run")
        if bls:
            layers.append("Breaklines")
        feedback.pushInfo("Steps left in HEC-RAS: make the RAS terrain from Terrain/*.tif, "
                          "associate it with the geometry, compute. See README_HECRAS.md.")
        lay = r["paths"]["layout"]
        for name in ("2D flow area", "Reservoir storage area", "Dam connection",
                     "Normal depth boundary", "Breach centre", *layers):
            context.addLayerToLoadOnCompletion(
                "{}|layername={}".format(lay, name),
                QgsProcessingContext.LayerDetails("HEC-RAS " + name, context.project(),
                                                  "HEC-RAS " + name))
        return {"FOLDER": r["folder"], "PROJECT": r["paths"]["prj"], "LAYOUT": lay,
                "README": r["paths"]["readme"], "RESULT": msg}


HECRAS_ALGORITHMS = (HecRasExportAlgorithm, HecRasImportAlgorithm, HecRasModelAlgorithm)
