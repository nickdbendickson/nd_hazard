# -*- coding: utf-8 -*-
"""v2.10: Processing algorithms of the dam-suite exports (for the "ndhazard"
provider; the lead registers them in processing_provider.Provider.loadAlgorithms):

  ndhazard:runnerculverts  culvert / bridge lines for ND Dam Breach Runner
                           (runnerculverts.py, dam suite item 8)
  ndhazard:suiteexport     one GeoPackage in ND Dam Breach Screening's data
                           package schema (suiteexport.py, dam suite item 12)

This file does not import processing_provider (so the provider may import it at
module level without a circular import); the few enum spellings it needs are
resolved here the same way (compat._first). Nothing here has been run inside a
real QGIS.
"""

import os

from qgis.core import (
    Qgis, QgsProcessingAlgorithm, QgsProcessingException, QgsProcessing,
    QgsProcessingParameterVectorLayer, QgsProcessingParameterFile,
    QgsProcessingParameterBoolean, QgsProcessingParameterNumber, QgsProcessingParameterEnum,
    QgsProcessingParameterString, QgsProcessingOutputFile, QgsProcessingOutputString,
    QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsProject,
)

from .compat import _first

FT_M = 0.3048


def _hidden(param):
    """A parameter kept for old models / histories but not shown (QGIS 3.x FlagHidden, 3.36+ / 4
    Qgis.ProcessingParameterFlag.Hidden; the test double has no flags)."""
    flag = None
    try:
        from qgis.core import Qgis
        flag = Qgis.ProcessingParameterFlag.Hidden
    except Exception:
        try:
            from qgis.core import QgsProcessingParameterDefinition as _D
            flag = _D.FlagHidden
        except Exception:
            flag = None
    if flag is not None:
        try:
            param.setFlags(param.flags() | flag)
        except Exception:
            pass
    return param

try:                                    # not in every test double
    from qgis.core import QgsProcessingParameterFileDestination as _FileDest
except ImportError:                     # pragma: no cover - depends on the build
    _FileDest = None
try:
    from qgis.core import QgsProcessingParameterExtent as _Extent
except ImportError:                     # pragma: no cover
    _Extent = None

NUM_DOUBLE = _first(lambda: Qgis.ProcessingNumberParameterType.Double,
                    lambda: QgsProcessingParameterNumber.Type.Double,
                    lambda: QgsProcessingParameterNumber.Double)
FILE_FILE = _first(lambda: Qgis.ProcessingFileParameterBehavior.File,
                   lambda: QgsProcessingParameterFile.Behavior.File,
                   lambda: QgsProcessingParameterFile.File)
FILE_FOLDER = _first(lambda: Qgis.ProcessingFileParameterBehavior.Folder,
                     lambda: QgsProcessingParameterFile.Behavior.Folder,
                     lambda: QgsProcessingParameterFile.Folder)
SRC_LINE = _first(lambda: Qgis.ProcessingSourceType.VectorLine,
                  lambda: QgsProcessing.SourceType.TypeVectorLine,
                  lambda: QgsProcessing.TypeVectorLine)
SRC_POINT = _first(lambda: Qgis.ProcessingSourceType.VectorPoint,
                   lambda: QgsProcessing.SourceType.TypeVectorPoint,
                   lambda: QgsProcessing.TypeVectorPoint)
SRC_POLYGON = _first(lambda: Qgis.ProcessingSourceType.VectorPolygon,
                     lambda: QgsProcessing.SourceType.TypeVectorPolygon,
                     lambda: QgsProcessing.TypeVectorPolygon)
NO_THREADING = _first(lambda: Qgis.ProcessingAlgorithmFlag.NoThreading,
                      lambda: QgsProcessingAlgorithm.Flag.FlagNoThreading,
                      lambda: QgsProcessingAlgorithm.FlagNoThreading, default=0)


def layer_features(layer, extent=None, feedback=None):
    """[(GeoJSON-like feature, geometry in EPSG:5070)] of a QGIS vector layer: each geometry
    is transformed to EPSG:4326 by QGIS, then projected with suitegpkg (the same path as
    the pack's features). extent: lon / lat box - features outside are skipped."""
    from . import suitegpkg as G
    from .helpers import transform_geometry, _wkb_bytes, is_null
    from .offline_pack import intersects_rect
    from .suiteexport import projected
    if layer is None:
        return []
    xf = QgsCoordinateTransform(layer.crs(), QgsCoordinateReferenceSystem("EPSG:4326"),
                                QgsProject.instance().transformContext())
    names = [f.name() for f in layer.fields()]
    out, bad = [], 0
    for feat in layer.getFeatures():
        if feedback is not None and feedback.isCanceled():
            raise QgsProcessingException("cancelled")
        g = transform_geometry(feat.geometry(), xf)
        if g is None or g.isNull() or g.isEmpty():
            continue
        try:
            try:
                from qgis.core import QgsWkbTypes, QgsGeometry
                if QgsWkbTypes.isCurvedType(g.wkbType()):
                    g = QgsGeometry(g.constGet().segmentize())
            except Exception:
                pass
            geom = G._read_wkb(_wkb_bytes(g))[0]
        except Exception:
            bad += 1
            continue
        if extent is not None and not intersects_rect(geom, extent):
            continue
        props = {n: (None if is_null(v) else v) for n, v in zip(names, feat.attributes())}
        out.append({"type": "Feature", "geometry": geom, "properties": props})
    if bad and feedback is not None:
        feedback.pushInfo("{}: {} geometr{} could not be read and were skipped".format(
            layer.name(), bad, "y" if bad == 1 else "ies"))
    return projected(out)


SUITE_GROUP = ("Dam suite exports", "damsuite")    # Processing group (name, id)


class _SuiteBase(QgsProcessingAlgorithm):
    def group(self):
        return SUITE_GROUP[0]

    def groupId(self):
        return SUITE_GROUP[1]

    def createInstance(self):
        return type(self)()

    def flags(self):
        return super().flags() | NO_THREADING

    # ---- shared parameters
    def _add_source_params(self):
        P = self.addParameter
        P(QgsProcessingParameterFile(
            "PACK", "ND Dam Hazard offline data pack (.zip / .sqlite; empty = the pack chosen "
            "on the Data tab, if any)", behavior=FILE_FILE, optional=True,
            fileFilter="ND Dam Hazard offline data pack (*.zip *.sqlite);;All files (*.*)"))
        P(QgsProcessingParameterFile(
            "DOWNLOADS", "or: a folder of ND Dam Hazard downloads (a dam output folder, its "
            "_downloads folder or a folder of dams)", behavior=FILE_FOLDER, optional=True))
        if _Extent is not None:
            P(_Extent("EXTENT", "Area (empty = North Dakota)", optional=True))
        else:
            P(QgsProcessingParameterString(
                "EXTENT", "Area 'xmin,xmax,ymin,ymax [EPSG:4326]' (empty = North Dakota)",
                defaultValue="", optional=True))

    def _add_output(self, name, desc):
        if _FileDest is not None:
            self.addParameter(_FileDest(name, desc, "GeoPackage (*.gpkg)"))
        else:
            self.addParameter(QgsProcessingParameterString(name, desc, defaultValue=""))

    def _output_path(self, parameters, name, context, default_name):
        if _FileDest is not None:
            path = self.parameterAsFileOutput(parameters, name, context)
        else:
            path = str(parameters.get(name) or "")
        if not path:
            raise QgsProcessingException("Choose the output GeoPackage")
        if os.path.isdir(path):
            path = os.path.join(path, default_name)
        if not path.lower().endswith(".gpkg"):
            path += ".gpkg"
        return path

    def _extent(self, parameters, context):
        """(lon_min, lat_min, lon_max, lat_max) or None (North Dakota)."""
        v = parameters.get("EXTENT")
        if v is None or (isinstance(v, str) and not v.strip()):
            return None
        if _Extent is not None and hasattr(self, "parameterAsExtent"):
            try:
                r = self.parameterAsExtent(parameters, "EXTENT", context,
                                           QgsCoordinateReferenceSystem("EPSG:4326"))
                if r is not None and not r.isNull() and r.width() > 0 and r.height() > 0:
                    return (r.xMinimum(), r.yMinimum(), r.xMaximum(), r.yMaximum())
            except Exception:
                pass
        return parse_extent(v)

    def _source(self, parameters, context, feedback, required):
        from .suiteexport import open_source, ExportError
        path = self.parameterAsFile(parameters, "PACK", context) or \
            self.parameterAsFile(parameters, "DOWNLOADS", context)
        chosen = bool(path)
        if not path:
            try:
                from .download import offline_config
                path = offline_config()[0]
            except Exception:
                path = ""
        if not path:
            if required:
                raise QgsProcessingException(
                    "No offline data pack or downloads folder: choose one (or choose an "
                    "offline data pack on the Data tab)")
            return None
        try:
            src = open_source(path, log=feedback.pushInfo)
        except ExportError as e:
            if required or chosen:
                raise QgsProcessingException(str(e))
            feedback.pushInfo("Offline data pack not used: {}".format(e))
            return None
        feedback.pushInfo("Data: {} {}".format(src.kind, src.path))
        return src

    @staticmethod
    def _progress(feedback):
        def cb(p, msg=""):
            if feedback.isCanceled():
                raise QgsProcessingException("cancelled")
            feedback.setProgress(max(0.0, min(100.0, float(p))))
            if msg:
                feedback.setProgressText(msg)
        return cb


def parse_extent(text):
    """'xmin,xmax,ymin,ymax [EPSG:4326]' (the QGIS extent text; the CRS must be EPSG:4326 or
    left out) -> (xmin, ymin, xmax, ymax)."""
    t = str(text).strip()
    crs = ""
    if "[" in t:
        t, crs = t.split("[", 1)
        crs = crs.strip(" ]").upper()
    if crs and crs not in ("EPSG:4326", "OGC:CRS84"):
        raise QgsProcessingException("Give the area in longitude / latitude (EPSG:4326); "
                                     "got {}".format(crs))
    try:
        xmin, xmax, ymin, ymax = [float(v) for v in t.replace(";", ",").split(",")]
    except ValueError:
        raise QgsProcessingException("Area '{}' is not 'xmin,xmax,ymin,ymax'".format(text))
    if not (xmin < xmax and ymin < ymax):
        raise QgsProcessingException("Area '{}' is empty".format(text))
    return (xmin, ymin, xmax, ymax)


class RunnerCulvertsAlgorithm(_SuiteBase):
    """Culvert / bridge lines for ND Dam Breach Runner (runnerculverts.py)."""

    def name(self):
        return "runnerculverts"

    def displayName(self):
        return "Culvert and bridge lines for ND Dam Breach Runner"

    def shortHelpString(self):
        return (
            "Writes the lines ND Dam Breach Runner opens through road and rail embankments "
            "('Culverts, bridges and notches': lines, upstream end first; fields kind, span_ft, "
            "rise_ft, barrels, shape, invert_up, invert_dn, name, matched by name) from the "
            "National Bridge Inventory, OpenStreetMap crossings and, when given, NDDOT's bridge "
            "inventory (Bridge_Inventory_External) and centerline pipes (Center_Line_Pipes).\n"
            "Each structure is placed where a stream crosses a road or rail line within the "
            "search distance of its point (the nearest crossing; rail first for railroad "
            "bridges). The line follows the stream, upstream end first (3DHP flowlines are "
            "digitised in the flow direction; tick 'reversed' for streams drawn upstream), and "
            "is (embankment top width + 2 x side margin) long, divided by the sine of the "
            "crossing angle (at least 30 degrees). The top width is the NBI deck / roadway "
            "width when known, else your road / rail width.\n"
            "NDDOT: the bridge inventory's culvert description ('DOUBLE, 10X7X88\' RCB') is "
            "read for the barrels, span x rise, barrel length and shape (box / pipe / arch); "
            "the centerline pipes give diameter (or arch span x rise), barrels, length and the "
            "elevation of each end. The two end elevations are written as the inverts: NDDOT's "
            "left and right are as seen looking from the lower station number to the higher "
            "(that direction is read from the neighbouring pipes on the route, else HWY_DIR), "
            "and the side the stream comes from is the inlet (an inlet below the outlet is "
            "noted). Their datum is not stated, so ND Dam Breach Runner compares them with "
            "the LiDAR bed before using "
            "them (its 'inventory elevation tolerance', 3 ft to start: within it they are used, "
            "both ends off alike is corrected as a datum shift, otherwise the LiDAR inverts are "
            "used and the run report says so). A line with an inventory barrel length is that "
            "long plus the margins (the barrel already runs toe to toe).\n"
            "Unknown values stay EMPTY - the NBI and OpenStreetMap give no rise, shape or "
            "inverts, the NBI span and barrels only - and the Runner then uses its own "
            "defaults (a DEM invert, its default span, a box). Each line records its source, "
            "the inventory text and the match distance. Structures that could not be placed "
            "(no stream / road crossing near, not over a waterway, a duplicate of another "
            "record) are listed in the layer 'culverts_not_placed' and a CSV beside the "
            "GeoPackage.\n"
            "Layers left empty are read from the offline data pack (or downloads folder) for "
            "the area: NBI_bridges, OSM_crossings, 3DHP_flowlines, ND_roads_NG911 (else HPMS), "
            "NARN_rail_lines. Search distance, widths and margin are screening parameters "
            "chosen by the engineer (defaults are placeholders, not standards). Output in "
            "EPSG:5070.")

    def _metres(self, parameters, context, ft_key, m_key, default_m):
        """A distance in metres from its feet parameter; a saved metre value alone (a model from
        before 2.13) is honoured."""
        if m_key in parameters and parameters.get(m_key) not in (None, "") and ft_key not in parameters:
            return float(self.parameterAsDouble(parameters, m_key, context))
        ft = self.parameterAsDouble(parameters, ft_key, context)
        if ft is None or (ft == 0 and ft_key not in parameters):
            return float(default_m)
        return float(ft) * FT_M

    def initAlgorithm(self, config=None):
        from . import runnerculverts as RC
        D = RC.DEFAULTS
        P = self.addParameter
        P(QgsProcessingParameterVectorLayer("NBI", "National Bridge Inventory (points; "
                                            "NBI_bridges)", [SRC_POINT], optional=True))
        P(QgsProcessingParameterVectorLayer("OSM", "OpenStreetMap crossings (points; "
                                            "OSM_crossings)", [SRC_POINT], optional=True))
        P(QgsProcessingParameterVectorLayer("NDDOT", "NDDOT bridge inventory (points; "
                                            "Bridge_Inventory_External - bridges and the large "
                                            "culverts with their description)",
                                            [SRC_POINT, SRC_POLYGON], optional=True))
        P(QgsProcessingParameterVectorLayer("NDDOT_PIPES", "NDDOT centerline pipes (points; "
                                            "Center_Line_Pipes - diameter, length and the two "
                                            "end elevations)", [SRC_POINT], optional=True))
        P(QgsProcessingParameterVectorLayer("STREAMS", "Streams (3DHP flowlines or a "
                                            "centerline)", [SRC_LINE], optional=True))
        P(QgsProcessingParameterBoolean("STREAM_REVERSED", "Stream lines are digitised "
                                        "UPSTREAM (not NHD): reverse them",
                                        defaultValue=D["stream_reversed"]))
        P(QgsProcessingParameterVectorLayer("ROADS", "Road lines", [SRC_LINE], optional=True))
        P(QgsProcessingParameterVectorLayer("RAIL", "Rail lines", [SRC_LINE], optional=True))
        self._add_source_params()
        # 2.13: the engineer sees feet; the metre parameters stay, hidden, for saved models
        for ft_key, m_key, label, dkey, lo in (
                ("SEARCH_FT", "SEARCH_M", "Search distance from a structure's point to a stream / "
                 "road crossing (ft; YOUR value)", "search_m", 3.0),
                ("ROAD_WIDTH_FT", "ROAD_WIDTH_M", "Road embankment top width when the record has "
                 "none (ft; YOUR value)", "road_width_m", 0.0),
                ("RAIL_WIDTH_FT", "RAIL_WIDTH_M", "Rail embankment top width (ft; YOUR value)",
                 "rail_width_m", 0.0),
                ("MARGIN_FT", "MARGIN_M", "Added on each side for the side slopes (ft; YOUR value)",
                 "margin_m", 0.0)):
            P(QgsProcessingParameterNumber(ft_key, label, type=NUM_DOUBLE,
                                           defaultValue=round(D[dkey] / FT_M, 1), minValue=lo))
            P(_hidden(QgsProcessingParameterNumber(m_key, "(before 2.13) {} (m)".format(label.split("(")[0].strip()),
                                                   type=NUM_DOUBLE, optional=True)))
        P(QgsProcessingParameterBoolean("USE_NBI_WIDTH", "Use the NBI deck / roadway width "
                                        "when the record has one",
                                        defaultValue=D["use_nbi_width"]))
        P(QgsProcessingParameterBoolean("ALLOW_NO_EMBANKMENT", "Place a structure on the "
                                        "stream alone when no road / rail line crosses it "
                                        "nearby", defaultValue=D["allow_no_embankment"]))
        P(QgsProcessingParameterBoolean("PASS_ELEVATIONS", "Write the NDDOT end elevations as "
                                        "the inverts, in feet - the Runner checks them against "
                                        "the LiDAR bed before using them; off: the Runner reads "
                                        "the DEM", defaultValue=D["pass_elevations"]))
        self._add_output("OUTPUT", "Culvert lines (GeoPackage)")
        self.addOutput(QgsProcessingOutputFile("CSV", "Structures not placed (CSV)"))
        self.addOutput(QgsProcessingOutputString("SUMMARY", "Summary"))

    def processAlgorithm(self, parameters, context, feedback):
        from . import runnerculverts as RC
        from .suiteexport import projected, HPMS_SETS
        out = self._output_path(parameters, "OUTPUT", context, "runner_culverts.gpkg")
        extent = self._extent(parameters, context)
        from .offline_pack import ND_BBOX
        ext = extent or ND_BBOX
        layers = {k: self.parameterAsVectorLayer(parameters, k, context)
                  for k in ("NBI", "OSM", "NDDOT", "NDDOT_PIPES", "STREAMS", "ROADS", "RAIL")}
        need = [k for k in ("NBI", "OSM", "STREAMS", "ROADS", "RAIL") if layers[k] is None]
        src = self._source(parameters, context, feedback, required=False) if need else None
        got, sources = {}, {}
        pack_names = {"NBI": ("NBI_bridges",), "OSM": ("OSM_crossings",),
                      "STREAMS": ("3DHP_flowlines",), "ROADS": ("ND_roads_NG911",) + HPMS_SETS,
                      "RAIL": ("NARN_rail_lines",)}
        for k, lyr in layers.items():
            if lyr is not None:
                got[k] = layer_features(lyr, extent, feedback)
                sources[k] = "layer {} ({} features)".format(lyr.name(), len(got[k]))
            elif src is not None and k in pack_names:
                name = next((n for n in pack_names[k] if src.has(n)), None)
                feats = src.features(name, ext) if name else None
                got[k] = projected(feats or [])
                sources[k] = "{} ({} features, {})".format(name, len(got[k]), src.note(name, ext)) \
                    if name else "not in the {}".format(src.kind)
            else:
                got[k] = []
                sources[k] = "not given"
            feedback.pushInfo("{}: {}".format(k, sources[k]))
        nddot = list(got["NDDOT"]) + list(got["NDDOT_PIPES"])
        if not got["NBI"] and not got["OSM"] and not nddot:
            raise QgsProcessingException("No structures: give the NBI, the OpenStreetMap "
                                         "crossings or an NDDOT layer (or an offline data pack "
                                         "that holds them)")
        params = {"search_m": self._metres(parameters, context, "SEARCH_FT", "SEARCH_M", RC.DEFAULTS["search_m"]),
                  "road_width_m": self._metres(parameters, context, "ROAD_WIDTH_FT", "ROAD_WIDTH_M",
                                               RC.DEFAULTS["road_width_m"]),
                  "rail_width_m": self._metres(parameters, context, "RAIL_WIDTH_FT", "RAIL_WIDTH_M",
                                               RC.DEFAULTS["rail_width_m"]),
                  "margin_m": self._metres(parameters, context, "MARGIN_FT", "MARGIN_M", RC.DEFAULTS["margin_m"]),
                  "use_nbi_width": self.parameterAsBool(parameters, "USE_NBI_WIDTH", context),
                  "allow_no_embankment": self.parameterAsBool(parameters, "ALLOW_NO_EMBANKMENT",
                                                              context),
                  "stream_reversed": self.parameterAsBool(parameters, "STREAM_REVERSED",
                                                          context),
                  "pass_elevations": self.parameterAsBool(parameters, "PASS_ELEVATIONS", context)
                  if "PASS_ELEVATIONS" in parameters else RC.DEFAULTS["pass_elevations"]}
        try:
            rep = RC.run(out, got["NBI"], got["OSM"], nddot, got["STREAMS"], got["ROADS"],
                         got["RAIL"], params, sources, log=feedback.pushInfo)
        except ValueError as e:
            raise QgsProcessingException(str(e))
        return {"OUTPUT": rep["path"], "CSV": rep["csv"],
                "SUMMARY": "{} placed, {} not placed".format(rep["placed"], rep["not_placed"])}


class SuiteExportAlgorithm(_SuiteBase):
    """One GeoPackage in ND Dam Breach Screening's data package schema (suiteexport.py)."""

    STRUCTURE_CHOICES = (("auto", "Automatic: FEMA USA Structures, else NSI, else ND address "
                                  "points"),
                         ("USA_Structures", "FEMA USA Structures outlines"),
                         ("NSI_structures", "USACE National Structure Inventory points"),
                         ("NDGISHUB_address_points", "ND GIS Hub site address points"))

    def name(self):
        return "suiteexport"

    def displayName(self):
        return "Export data package for ND Dam Breach Screening"

    def shortHelpString(self):
        return (
            "Writes ONE GeoPackage (EPSG:5070) in the standard schema of ND Dam Breach "
            "Screening's '0 - Build offline data package', from ND Dam Hazard's offline data "
            "pack or a folder of its downloads: structures (habitable 1/0, mobile 1/0, occ_cls, "
            "prim_occ - ND Dam Hazard's own habitability and manufactured-home rules on each "
            "structure's attributes; 'Unverified' is written as habitable = 1), "
            "structure_points (the same as points, for ND Dam Breach Runner's receptors), "
            "roads (aadt, road_class, level - AADT joined the way ND Dam Hazard's "
            "classification joins it; unknown AADT stays empty, never 0), railroads "
            "(passenger, trains_day, level), flowlines, lifelines (category) and dams (NID).\n"
            "Not in the pack, so not exported: 3DHP waterbodies, the DWR dam lines and any "
            "DEM - give those to the Screening tools yourself. The run log lists the counts "
            "and what is missing; the table 'suite_export_info' in the GeoPackage keeps it.\n"
            "Dam folder (2.17.0, optional): for one dam, also 3DHP waterbodies (waterbodies), "
            "DWR dam lines (dam_lines) and DWR dams (dwr_dams) from the dam folder's data "
            "('Get data for this dam'), else from the pack when it has them, and the path of "
            "the dam's DEM (recorded in suite_export_info, not copied).\n"
            "Area: North Dakota's box by default (features touching it are exported whole).")

    def initAlgorithm(self, config=None):
        self._add_source_params()
        self.addParameter(QgsProcessingParameterEnum(
            "STRUCTURES", "Structures from", [c[1] for c in self.STRUCTURE_CHOICES],
            defaultValue=0))
        # 2.17.0: one dam's waterbodies, DWR dam lines / dams and DEM path (suiteexport.DAM_EXTRAS)
        self.addParameter(QgsProcessingParameterFile(
            "DAM_FOLDER", "Dam folder (optional: adds its waterbodies, DWR dam lines / dams and "
            "DEM path from 'Get data for this dam')", behavior=FILE_FOLDER, optional=True))
        self._add_output("OUTPUT", "Data package (GeoPackage)")
        self.addOutput(QgsProcessingOutputString("SUMMARY", "Summary"))

    def processAlgorithm(self, parameters, context, feedback):
        from . import suiteexport as SE
        out = self._output_path(parameters, "OUTPUT", context, "nd_screening_package.gpkg")
        src = self._source(parameters, context, feedback, required=True)
        extent = self._extent(parameters, context)
        choice = self.STRUCTURE_CHOICES[self.parameterAsEnum(parameters, "STRUCTURES", context)][0]
        dam = ""
        if parameters.get("DAM_FOLDER") not in (None, ""):
            dam = self.parameterAsFile(parameters, "DAM_FOLDER", context) or ""
        try:
            rep = SE.export(out, src, extent, structures=choice,
                            progress=self._progress(feedback), log=feedback.pushInfo,
                            dam_folder=dam or None)
        except SE.ExportError as e:
            raise QgsProcessingException(str(e))
        return {"OUTPUT": rep["path"], "SUMMARY": "; ".join(
            "{} {}".format(v, k) for k, v in rep["counts"].items()) or "nothing exported"}
