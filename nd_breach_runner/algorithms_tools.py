"""Data-preparation, comparison and hand-off algorithms of the provider."""
import os

from qgis.core import (QgsPointXY, QgsProcessingException, QgsProcessingOutputFile,
                       QgsProcessingOutputFolder, QgsProcessingOutputString,
                       QgsProcessingParameterEnum, QgsProcessingParameterExtent,
                       QgsProcessingParameterFileDestination, QgsProcessingParameterPoint,
                       QgsProcessingParameterRasterLayer, QgsProcessingParameterString)

from .compat import ALG_NO_THREADING
from .algorithms import _Base
from .core import (benchmark, conditioning as condmod, gdal_io, handoff, nd_data, products,
                   structures as structmod, workflow)
from .core.units import FT, MILE


class _ToolBase(_Base):
    def group(self):
        return "Data preparation and tools"

    def groupId(self):
        return "tools"

    def _need_gdal(self):
        try:
            from osgeo import gdal  # noqa: F401
        except ImportError:
            raise QgsProcessingException("GDAL's Python bindings are not available; run this "
                                         "from QGIS.")

    def _out(self, parameters, context, name="OUTPUT"):
        p = self.parameterAsFileOutput(parameters, name, context)
        if not p:
            raise QgsProcessingException("Choose an output file")
        os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
        return p


# ============================================================================
class BuildVrtAlgorithm(_ToolBase):
    def name(self):
        return "buildvrt"

    def displayName(self):
        return "Build a statewide LiDAR DEM mosaic (VRT)"

    def shortHelpString(self):
        return ("Mosaics a folder of LiDAR DEM tiles into one virtual raster (VRT) without "
                "copying any data, so the breach tools can read any dam from the statewide "
                "set offline. Tiles in a different CRS than most are left out and listed; "
                "differing cell sizes or elevation units are reported. Use the VRT as the DEM "
                "of the other tools.")

    def initAlgorithm(self, config=None):
        self._file("TILES", "Folder of DEM tiles (searched recursively)", folder=True,
                   optional=False)
        self._num("NODATA", "No-data value (empty = taken from the tiles)", optional=True)
        self.addParameter(QgsProcessingParameterFileDestination("OUTPUT", "DEM mosaic (VRT)",
                                                                "VRT files (*.vrt)"))
        self.addOutput(QgsProcessingOutputFile("VRT", "DEM mosaic"))
        self.addOutput(QgsProcessingOutputString("RESULT", "Result"))

    def processAlgorithm(self, parameters, context, feedback):
        self._need_gdal()
        out = self._out(parameters, context)
        folder = self.parameterAsFile(parameters, "TILES", context)
        try:
            r = gdal_io.build_vrt(folder, out, self._opt(parameters, "NODATA", context))
        except (ValueError, RuntimeError) as e:
            raise QgsProcessingException(str(e))
        msg = "{} tiles, CRS {}, cell size {} m, elevation units {}".format(
            r["tiles"], r["crs"], r["cell_sizes"], r["elevation_units"] or "not recorded")
        feedback.pushInfo(msg)
        for n in r["notes"] + ["Could not open: {}".format(p["file"]) for p in r["problems"]]:
            feedback.pushWarning("Check: " + n)
        self.load(context, out, "LiDAR DEM mosaic")
        return {"VRT": out, "RESULT": msg}


class FillGapsAlgorithm(_ToolBase):
    def name(self):
        return "fillgaps"

    def displayName(self):
        return "Fill no-data gaps in a DEM"

    def shortHelpString(self):
        return ("Fills no-data holes (water bodies, missing returns, tile seams) by "
                "interpolating from their edges, over the whole raster or an extent. LISFLOOD-FP "
                "treats no-data as very high ground, so a hole in the flood path dams the "
                "flood. The whole area is read into memory, so give an extent around the dam "
                "and its flood path for a statewide mosaic. Fills only holes up to the search "
                "distance; the rest are counted and reported.")

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterRasterLayer("DEM", "DEM"))
        self.addParameter(QgsProcessingParameterExtent("EXTENT", "Area to fill (empty = all)",
                                                       optional=True))
        self._num("MAX_DIST", "Fill holes up to this many cells across", 100, minimum=1,
                  integer=True)
        self._num("SMOOTH", "Smoothing passes over the filled cells", 0, minimum=0, integer=True,
                  advanced=True)
        self.addParameter(QgsProcessingParameterFileDestination("OUTPUT", "Filled DEM",
                                                                "GeoTIFF (*.tif)"))
        self.addOutput(QgsProcessingOutputFile("FILLED", "Filled DEM"))
        self.addOutput(QgsProcessingOutputString("RESULT", "Result"))

    def processAlgorithm(self, parameters, context, feedback):
        self._need_gdal()
        dem = self.parameterAsRasterLayer(parameters, "DEM", context)
        out = self._out(parameters, context)
        ext = None
        if parameters.get("EXTENT"):
            r = self.parameterAsExtent(parameters, "EXTENT", context, dem.crs())
            if r is not None and not r.isNull():
                ext = (r.xMinimum(), r.yMinimum(), r.xMaximum(), r.yMaximum())
        try:
            r = gdal_io.fill_gaps(dem.source(), out, ext,
                                  int(self.parameterAsInt(parameters, "MAX_DIST", context)),
                                  int(self.parameterAsInt(parameters, "SMOOTH", context)))
        except (ValueError, RuntimeError) as e:
            raise QgsProcessingException(str(e))
        msg = "{:,} cells filled, {:,} still no-data".format(r["cells_filled"], r["cells_left"])
        feedback.pushInfo(msg)
        if r["cells_left"]:
            feedback.reportError("Check: holes wider than the search distance remain; raise it "
                                 "or fill them from another source.", fatalError=False)
        self.load(context, out, "Filled DEM")
        return {"FILLED": out, "RESULT": msg}


class NlcdManningAlgorithm(_ToolBase):
    def name(self):
        return "nlcdmanning"

    def displayName(self):
        return "Manning's n raster from NLCD land cover"

    def shortHelpString(self):
        table = "; ".join("{} {} = {}".format(c, n, v) for c, n, v in nd_data.lut_table())
        return ("Converts an NLCD land-cover raster to a Manning's n raster for the breach "
                "tools (offline). Typical floodplain values from the usual references; they are "
                "starting points, so calibrate where a known flood exists and override any "
                "class with JSON like {\"82\": 0.05}. Classes not in the table get the default. "
                "You can also give the NLCD raster straight to the breach tools instead.\n\n"
                + table)

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterRasterLayer("NLCD", "NLCD land-cover raster"))
        self.addParameter(QgsProcessingParameterRasterLayer(
            "LIKE", "Match this raster's grid (e.g. the DEM); empty = the NLCD grid",
            optional=True))
        self._text("NLCD_N", "Your own n for some classes, JSON like {\"82\": 0.05}")
        self._num("DEFAULT_N", "n for classes not in the table", 0.06, minimum=0.005, maximum=1)
        self.addParameter(QgsProcessingParameterFileDestination("OUTPUT", "Manning's n raster",
                                                                "GeoTIFF (*.tif)"))
        self.addOutput(QgsProcessingOutputFile("MANNING", "Manning's n"))

    def processAlgorithm(self, parameters, context, feedback):
        self._need_gdal()
        out = self._out(parameters, context)
        nlcd = self.parameterAsRasterLayer(parameters, "NLCD", context)
        like = self.parameterAsRasterLayer(parameters, "LIKE", context)
        try:
            lut = nd_data.manning_lut(self._str(parameters, "NLCD_N", context),
                                      self.parameterAsDouble(parameters, "DEFAULT_N", context))
            r = gdal_io.nlcd_to_manning_raster(nlcd.source(), out, lut,
                                               like.source() if like else None)
        except (ValueError, RuntimeError) as e:
            raise QgsProcessingException(str(e))
        feedback.pushInfo("Classes found: {}".format(
            ", ".join("{} ({})".format(c, nd_data.NLCD_NAMES.get(c, "?")) for c in r["classes"])))
        if r["unknown_classes"]:
            feedback.reportError("Check: classes {} are not in the table and got the default n."
                                 .format(r["unknown_classes"]), fatalError=False)
        self.load(context, out, "Manning n from NLCD")
        return {"MANNING": out}


# ============================================================================
class ConditionDemAlgorithm(_ToolBase):
    def name(self):
        return "conditiondem"

    def displayName(self):
        return "Preview the DEM conditioning around a dam"

    def shortHelpString(self):
        return ("Writes the DEM as the models will see it after the channels are burned, "
                "ponds lowered and culverts, bridges and notches cut, around a dam, so it can "
                "be checked before a long run (compare it with the original). Also writes the "
                "ponds' initial water depth. The source DEM is never changed; the breach tools "
                "apply the same edits on the fly.")

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterRasterLayer("DEM", "DEM"))
        self.addParameter(QgsProcessingParameterEnum(
            "DEM_Z", "DEM elevation units", ["Detect from the raster", "Feet", "US survey feet",
                                              "Meters"], defaultValue=0))
        self.addParameter(QgsProcessingParameterPoint("DAM_POINT", "Dam (point on the crest)"))
        self._num("RADIUS_MI", "Radius around the dam (mi)", 2.0, minimum=0.1)
        self._num("CELL", "Cell size (m; 10 m = 32.8 ft, the raster cell)", 10.0, minimum=1.0)
        self.add_feature_params()
        self.addParameter(QgsProcessingParameterFileDestination(
            "OUTPUT", "Conditioned DEM (elevations in metres, as the models read it)",
            "GeoTIFF (*.tif)"))
        self.addOutput(QgsProcessingOutputFile("CONDITIONED", "Conditioned DEM"))
        self.addOutput(QgsProcessingOutputFile("MANNING_OUT", "Manning's n with the building "
                                                              "footprints (v3.4.0, roughness "
                                                              "option only)"))
        self.addOutput(QgsProcessingOutputString("RESULT", "Result"))

    def processAlgorithm(self, parameters, context, feedback):
        self._need_gdal()
        out = self._out(parameters, context)
        dem = self.parameterAsRasterLayer(parameters, "DEM", context)
        pt = self.parameterAsPoint(parameters, "DAM_POINT", context, dem.crs())
        src, writer, to_work, work = self.gis(parameters, context, dem.crs(), pt)
        xy = to_work.transform(QgsPointXY(pt))
        st = workflow.RunSettings()
        st.max_extent_mi = 2.0 * self.parameterAsDouble(parameters, "RADIUS_MI", context)
        for n in self.apply_features(st, parameters, context, work, (xy.x(), xy.y())):
            feedback.pushInfo(n)
        r = self.parameterAsDouble(parameters, "RADIUS_MI", context) * MILE
        cell = self.parameterAsDouble(parameters, "CELL", context)
        bounds = (xy.x() - r, xy.y() - r, xy.x() + r, xy.y() + r)
        edits = st.edits if st.edits is not None else condmod.Edits()
        g = src.read(bounds, cell)
        cuts = structmod.structure_edits(st.structures, cell, "lisflood")
        all_edits = condmod.Edits(edits.channels, edits.ponds, list(edits.notches) + cuts,
                                  buildings=edits.buildings)
        new, init, counts = all_edits.apply(g)
        writer(new, out)
        if init is not None:
            writer(init, os.path.splitext(out)[0] + "_initial_depth.tif")
        man_out = ""
        if all_edits.has_roughness():          # v3.4.0: the n grid the models would read
            base_n = src.manning(bounds, cell) if hasattr(src, "manning") else None
            man, mcounts = all_edits.apply_manning(g, base_n, st.manning_n)
            counts.update({"manning " + k: v for k, v in mcounts.items()})
            man_out = os.path.splitext(out)[0] + "_manning_n.tif"
            writer(man, man_out)
        msg = "; ".join("{}: {} cells".format(k, v) for k, v in counts.items()) or "no edits"
        msg += ". " + condmod.describe_buildings(all_edits, counts)
        feedback.pushInfo(msg)
        self.load(context, out, "Conditioned DEM")
        if man_out:
            self.load(context, man_out, "Manning's n with buildings")
        return {"CONDITIONED": out, "MANNING_OUT": man_out, "RESULT": msg}


# ============================================================================
class BenchmarkAlgorithm(_ToolBase):
    def name(self):
        return "benchmark"

    def displayName(self):
        return "Compare with a reference model (e.g. HEC-RAS)"

    def shortHelpString(self):
        return ("Compares a dam's breach rasters with another model's on the same grid: "
                "fit index (extent agreement), hit and false-alarm rates, depth bias / RMSE / "
                "90th-percentile error, depth-class agreement, and velocity and arrival-time "
                "differences. Writes a difference raster, an extent agreement raster "
                "(1 = only this tool, 2 = only the reference, 3 = both), a CSV and an HTML "
                "report. Reference rasters are read onto this tool's grid.")

    def initAlgorithm(self, config=None):
        self._file("DAM_FOLDER", "Dam folder with 'Depth (Max).tif' etc.", folder=True,
                   optional=False)
        for key, desc in (("REF_DEPTH", "Reference max depth raster"),
                          ("REF_VELOCITY", "Reference max velocity raster"),
                          ("REF_ARRIVAL", "Reference arrival time raster"),
                          ("REF_WSE", "Reference max water surface elevation raster")):
            self.addParameter(QgsProcessingParameterRasterLayer(key, desc,
                                                                optional=key != "REF_DEPTH"))
        self.addParameter(QgsProcessingParameterEnum(
            "REF_UNITS", "Reference units", ["US customary (ft, ft/s, hours)",
                                             "Metric (m, m/s, hours)"], defaultValue=0))
        self._num("THRESH_FT", "Wet where the depth is at least (ft; starting value 1 is the "
                  "engineer's, a screening value you choose)", 1.0, minimum=0.01)
        self._file("OUT_FOLDER", "Output folder (empty = <dam folder>/_benchmark)", folder=True)
        self.addOutput(QgsProcessingOutputFile("SUMMARY", "Benchmark_Summary.csv"))
        self.addOutput(QgsProcessingOutputFile("REPORT", "Benchmark report (HTML)"))
        self.addOutput(QgsProcessingOutputString("VERDICT", "Verdict"))

    def processAlgorithm(self, parameters, context, feedback):
        self._need_gdal()
        folder = self.parameterAsFile(parameters, "DAM_FOLDER", context)
        paths = {k: os.path.join(folder, v) for k, v in workflow.FINAL_NAMES.items()}
        if not os.path.exists(paths["depth"]):
            raise QgsProcessingException("No '{}' in {}".format(workflow.FINAL_NAMES["depth"],
                                                                folder))
        g = gdal_io.read_grid(paths["depth"])
        ours = {"depth": g.data}
        ref, si = {}, self.parameterAsEnum(parameters, "REF_UNITS", context) == 1
        f = 1.0 / FT if si else 1.0
        for key, kind, scale in (("REF_DEPTH", "depth", f), ("REF_VELOCITY", "velocity", f),
                                 ("REF_ARRIVAL", "arrival", 1.0), ("REF_WSE", "wse", f)):
            lyr = self.parameterAsRasterLayer(parameters, key, context)
            if lyr is None:
                continue
            ref[kind] = gdal_io.read_like(lyr.source(), g) * scale
            if kind != "depth" and os.path.exists(paths[kind]):
                ours[kind] = gdal_io.read_like(paths[kind], g, "near")
        unit = gdal_io.linear_unit_m(g.crs_wkt) or 1.0
        cell_ac = (g.dx * unit) ** 2 / 4046.8564224
        thr = self.parameterAsDouble(parameters, "THRESH_FT", context)
        m = benchmark.compare_fields(ours, ref, cell_ac, thr)
        out_dir = self._fileparam(parameters, "OUT_FOLDER", context) or os.path.join(
            folder, "_benchmark")
        writer = gdal_io.GeoTiffWriter(g.crs_wkt)
        r = benchmark.write_outputs(out_dir, m, benchmark.difference_arrays(ours, ref, thr),
                                    writer, g)
        feedback.pushInfo(m["verdict"])
        for p in r["rasters"].values():
            self.load(context, p, os.path.basename(p)[:-4])
        return {"SUMMARY": r["csv"], "REPORT": r["html"], "VERDICT": m["verdict"]}


# ============================================================================
class ClassifyAlgorithm(_ToolBase):
    def flags(self):
        # short, and it calls another plugin's algorithm: run it on the main thread
        return super().flags() | ALG_NO_THREADING

    def name(self):
        return "classify"

    def displayName(self):
        return "Hand the results to the ND hazard classification"

    def shortHelpString(self):
        return ("Runs the ND Dam Hazard plugin's classification (algorithm ndhazard:classify, "
                "or ndhazard:batch for a whole dams folder) on the rasters this tool wrote. The "
                "plugin's parameters are read at run time and filled from the dam folder by "
                "name; anything it cannot fill is named so you can give it in the mapping JSON, "
                "e.g. {\"PARAM\": \"{depth}\"} with the placeholders {depth} {velocity} "
                "{arrival} {wse} {folder} {parent} {dam_id} {out}. For a batch the breach summary "
                "CSV and the classification CSV are merged into one.")

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterEnum(
            "MODE", "Classify", ["One dam folder", "Every dam in a dams folder (batch)"],
            defaultValue=0))
        self._file("FOLDER", "Dam folder, or the dams folder for a batch", folder=True,
                   optional=False)
        self._text("DAM_ID", "Dam ID (empty = the folder name)")
        self._text("ALG_ID", "Classification algorithm ID (empty = ndhazard:classify / "
                   "ndhazard:batch)", advanced=True)
        self._text("MAPPING", "Parameter mapping, JSON", advanced=True)
        self.addOutput(QgsProcessingOutputString("RESULT", "Result"))
        self.addOutput(QgsProcessingOutputFile("MERGED", "Merged CSV (batch)"))

    def processAlgorithm(self, parameters, context, feedback):
        folder = self.parameterAsFile(parameters, "FOLDER", context)
        alg = self._str(parameters, "ALG_ID", context)
        try:
            mp = handoff.load_mapping(self._str(parameters, "MAPPING", context))
            if self.parameterAsEnum(parameters, "MODE", context) == 0:
                r = handoff.classify_folder(folder, self._str(parameters, "DAM_ID", context),
                                            alg, mp, feedback)
                msg = "Hazard class: {}".format(r["class"] or "(see the algorithm's outputs)")
                feedback.pushInfo(msg)
                for k, v in r["outputs"].items():
                    feedback.pushInfo("{}: {}".format(k, v))
                return {"RESULT": msg, "MERGED": ""}
            r = handoff.classify_batch(folder, alg, mp, feedback)
        except handoff.HandoffError as e:
            raise QgsProcessingException(str(e))
        merged = ""
        breach_csv = os.path.join(folder, "Batch_Breach_Runs.csv")
        csvs = [v for v in r["outputs"].values() if isinstance(v, str)
                and v.lower().endswith(".csv") and os.path.exists(v)]
        if csvs and os.path.exists(breach_csv):
            merged = handoff.merge_csv(breach_csv, csvs[0], os.path.join(
                folder, "Batch_Breach_Runs_Classified.csv"))
            feedback.pushInfo("Merged: " + merged)
        return {"RESULT": "classified", "MERGED": merged}


class ProductsAlgorithm(_ToolBase):
    def name(self):
        return "products"

    def displayName(self):
        return "Flood polygons and EAP map from a dam's rasters"

    def shortHelpString(self):
        return ("Makes the flood-depth polygons, arrival zones, GeoPackage and EAP inundation "
                "map from the rasters in a dam folder ('Depth (Max).tif', 'Arrival Time.tif'), "
                "for runs made earlier or by another model. The dam point marks the dam on the "
                "map; give it in the raster's CRS.\n\n"
                "The EAP map is ND Dam Hazard's map book (a PDF on aerial imagery). This "
                "tool's map is a preview, 'EAP Inundation Map (preview).svg', drawn by "
                "default only when ND Dam Hazard is not installed (parameter 'EAP map').")

    def initAlgorithm(self, config=None):
        self._file("DAM_FOLDER", "Dam folder", folder=True, optional=False)
        self.addParameter(QgsProcessingParameterString("DAM_ID", "Dam ID", optional=True))
        self._text("DAM_NAME", "Dam name (map title)")
        self.addParameter(QgsProcessingParameterPoint("DAM_POINT", "Dam (in the raster's CRS)"))
        self._num("ARRIVAL_FT", "Flood extent: depth at least (ft; starting value 1 is the "
                  "engineer's, a screening value you choose)", 1.0, minimum=0.01)
        self._text("CONTOURS_HR", "Arrival zones (hours)", "0.25, 0.5, 1, 2, 4, 8")
        from .algorithms import EAP_MAP_CHOICES
        self.addParameter(QgsProcessingParameterEnum(
            "EAP_MAP", "EAP map (the EAP map is ND Dam Hazard's map book on aerial imagery)",
            [c[1] for c in EAP_MAP_CHOICES], defaultValue=0))
        self.addOutput(QgsProcessingOutputFolder("FOLDER", "Dam folder"))
        self.addOutput(QgsProcessingOutputFile("EAP_MAP", "EAP inundation map preview"))

    def processAlgorithm(self, parameters, context, feedback):
        from .algorithms import _floats, EAP_MAP_CHOICES
        self._need_gdal()
        folder = self.parameterAsFile(parameters, "DAM_FOLDER", context)
        eap = EAP_MAP_CHOICES[min(len(EAP_MAP_CHOICES) - 1, self.parameterAsEnum(
            parameters, "EAP_MAP", context))][0]
        pt = self.parameterAsPoint(parameters, "DAM_POINT", context)
        try:
            res = products.products_from_rasters(
                folder, self._str(parameters, "DAM_ID", context) or os.path.basename(folder),
                (pt.x(), pt.y()), self.parameterAsDouble(parameters, "ARRIVAL_FT", context),
                _floats(self._str(parameters, "CONTOURS_HR", context), (0.5, 1, 2, 4, 8)),
                self._str(parameters, "DAM_NAME", context), eap_map=eap)
        except (OSError, ValueError, RuntimeError) as e:
            raise QgsProcessingException(str(e))
        for k, p in res.vectors.items():
            feedback.pushInfo("{}: {}".format(k, p))
        for k in ("depth_polygons", "arrival_zones"):
            self.load(context, res.vectors.get(k), k.replace("_", " "))
        if not res.eap_map:
            feedback.pushInfo("EAP map: " + products.MAPBOOK_NOTE)
        return {"FOLDER": folder, "EAP_MAP": res.eap_map}


TOOL_ALGORITHMS = (BuildVrtAlgorithm, FillGapsAlgorithm, NlcdManningAlgorithm,
                   ConditionDemAlgorithm, BenchmarkAlgorithm, ClassifyAlgorithm,
                   ProductsAlgorithm)
