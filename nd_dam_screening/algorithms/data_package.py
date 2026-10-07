"""Build one offline GeoPackage of standardized statewide layers."""
import os

from qgis.core import (QgsProcessingAlgorithm, QgsProcessingParameterFeatureSource,
                       QgsProcessingParameterField, QgsProcessingParameterString,
                       QgsProcessingParameterFileDestination, QgsProcessingParameterFile,
                       QgsFields, QgsField,
                       QgsFeature, QgsVectorFileWriter, QgsProject, QgsVectorLayer,
                       QgsGeometry, QgsProcessingException)

from ..core import hazard as hz
from ..core import package as pk
from . import _common as cm
from ..compat import (FT_DOUBLE, FT_INT, FT_STRING, GEOM_POINT, SRC_LINE, SRC_POINT, SRC_POLYGON, WKB_POINT, WRITER_NO_ERROR, WRITER_OVERWRITE_FILE, WRITER_OVERWRITE_LAYER)

# (input key, package layer): an input left empty is taken from the package
SLOTS = (("FLOW", "flowlines"), ("WB", "waterbodies"), ("DAML", "dam_lines"),
         ("BLD", "structures"), ("ROADS", "roads"), ("RAIL", "railroads"),
         ("LIFE", "lifelines"), ("NIDD", "dams"))
# the field parameters of a slot, and the standard field a package layer has for each
PKG_FIELDS = {"BLD": {"OCC": "occ_cls", "PRIM": "prim_occ"},
              "ROADS": {"AADT": "aadt", "RCLS": "road_class"},
              "RAIL": {"PAX": "passenger", "TRAINS": "trains_day"}}


class DataPackage(QgsProcessingAlgorithm):
    def name(self): return "datapackage"
    def displayName(self): return "0 - Build offline data package"
    def group(self): return "Dam screening"
    def groupId(self): return "dams"
    def createInstance(self): return DataPackage()

    def shortHelpString(self):
        return ("Copies the statewide layers you downloaded once into a single GeoPackage in EPSG:5070, "
                "optionally clipped to North Dakota, with standard field names the other tools pick up "
                "automatically:\n"
                "structures: habitable (1/0: 0 only when OCC_CLS / PRIM_OCC name an uninhabited "
                "building - barn, shed, garage ...; ND 5.2.1.1 / 5.3; a habitable yes/no field of the "
                "layer wins), mobile (1/0, mobile / manufactured / trailer, or a mobile yes/no field), "
                "occ_cls, prim_occ; structure_points: the same structures as points (point on "
                "surface) for ND Dam Breach Runner's receptors\n"
                "roads: aadt (missing, zero or NaN = empty), road_class, level (Low/Medium/High by "
                "ND 5.2.1.2 as tool 8 rates it; no AADT = Medium by assumption)\n"
                "railroads: passenger (1/0, FRA PASSNGR codes or yes/1), trains_day, level\n"
                "flowlines, waterbodies, dam_lines, lifelines and dams are copied as they are (geometry "
                "reprojected).\n"
                "OR: A PACKAGE EXPORTED BY ND DAM HAZARD (its 'Export data package for ND Dam Breach "
                "Screening', v2.10 or later): every input left empty is taken from the package's layer "
                "of the same name (structures, roads, railroads, flowlines, lifelines, dams - and "
                "waterbodies / dam_lines if the package has them; ND Dam Hazard's pack has neither). "
                "Layers you give explicitly always win. The package's habitable / mobile flags (ND Dam "
                "Hazard's own reading) and aadt / passenger / trains_day are kept; level is rated "
                "again with this plugin's rule. What the package lacks is reported. This tool works "
                "without ND Dam Hazard.\n"
                "Suggested sources: FEMA USA Structures (North Dakota), NDDOT traffic volumes (AADT), "
                "FRA North American Rail Network lines, USGS NHD High Resolution, the DWR dam lines. "
                "After this, the whole workflow runs offline except for the 3DEP terrain option.")

    def initAlgorithm(self, config=None):
        P = self.addParameter
        L, A, PT = [SRC_LINE], [SRC_POLYGON], \
            [SRC_POINT, SRC_POLYGON]
        P(QgsProcessingParameterFile("PACKAGE", "or: a data package exported by ND Dam Hazard (GeoPackage; "
                                     "fills every input left empty)", extension="gpkg", optional=True))
        P(QgsProcessingParameterFeatureSource("CLIP", "Clip to (e.g. ND boundary, optional)", A, optional=True))
        P(QgsProcessingParameterFeatureSource("FLOW", "NHD flowlines", L, optional=True))
        P(QgsProcessingParameterFeatureSource("WB", "NHD waterbodies", A, optional=True))
        P(QgsProcessingParameterFeatureSource("DAML", "DWR dam lines", L, optional=True))
        P(QgsProcessingParameterFeatureSource("BLD", "Structures (USA Structures)", PT, optional=True))
        P(QgsProcessingParameterField("OCC", "Structures: occupancy class field", defaultValue="OCC_CLS",
                                      parentLayerParameterName="BLD", optional=True))
        P(QgsProcessingParameterField("PRIM", "Structures: primary occupancy field", defaultValue="PRIM_OCC",
                                      parentLayerParameterName="BLD", optional=True))
        P(QgsProcessingParameterString("UNINHABITED", "Structures: occupancy words that mark an UNINHABITED "
                                       "building (as tool 8)", defaultValue=", ".join(hz.UNINHABITED)))
        P(QgsProcessingParameterFeatureSource("ROADS", "Roads", L, optional=True))
        P(QgsProcessingParameterField("AADT", "Roads: AADT field", parentLayerParameterName="ROADS",
                                      optional=True))
        P(QgsProcessingParameterField("RCLS", "Roads: class / route type field", parentLayerParameterName="ROADS",
                                      optional=True))
        P(QgsProcessingParameterFeatureSource("RAIL", "Railroads", L, optional=True))
        P(QgsProcessingParameterField("PAX", "Railroads: passenger field", defaultValue="PASSNGR",
                                      parentLayerParameterName="RAIL", optional=True))
        P(QgsProcessingParameterString("PAXCODES", "Passenger codes = active passenger line",
                                       defaultValue=", ".join(hz.PAX_CODES)))
        P(QgsProcessingParameterField("TRAINS", "Railroads: trains per day field", parentLayerParameterName="RAIL",
                                      optional=True))
        P(QgsProcessingParameterFeatureSource("LIFE", "Lifeline facilities (points or polygons)", PT,
                                              optional=True))
        P(QgsProcessingParameterFeatureSource("NIDD", "Dams (NID, points)", [SRC_POINT],
                                              optional=True))
        P(QgsProcessingParameterFileDestination("OUT", "GeoPackage", "GeoPackage (*.gpkg)"))

    def processAlgorithm(self, params, context, feedback):
        out = self.parameterAsFileOutput(params, "OUT", context)
        if not out.lower().endswith(".gpkg"):
            out += ".gpkg"
        pkg = self.parameterAsString(params, "PACKAGE", context) or None
        if pkg and os.path.abspath(pkg) == os.path.abspath(out):
            raise QgsProcessingException("The output must not be the package you read from")
        pkg_layers, pkg_info = {}, {}
        if pkg:
            try:
                pkg_layers = pk.gpkg_layers(pkg)
                pkg_info = pk.export_info(pkg)
            except pk.PackageError as e:
                raise QgsProcessingException(str(e))
            feedback.pushInfo("Package {}: layers {}".format(pkg, ", ".join(sorted(pkg_layers)) or "none"))
            for line in pk.info_lines(pkg_info):
                feedback.pushInfo("  " + line)
        if os.path.exists(out):
            os.remove(out)
        clip_src = self.parameterAsSource(params, "CLIP", context)
        clip = None
        if clip_src is not None:
            ta = cm.to_albers(clip_src)
            geoms = []
            for f in clip_src.getFeatures():
                g = f.geometry(); g.transform(ta); geoms.append(g)
            clip = QgsGeometry.unaryUnion(geoms)
        S = lambda k: self.parameterAsString(params, k, context) or None
        uninhabited = hz.words(S("UNINHABITED")) or hz.UNINHABITED
        paxcodes = hz.words(S("PAXCODES")) or hz.PAX_CODES
        first = [True]
        written, origin = [], {}

        # every slot: the layer given, else the package's layer of that name
        sources = {}
        for key, lname in SLOTS:
            src = self.parameterAsSource(params, key, context)
            if src is not None:
                sources[key] = src
                origin[key] = "given"
            elif lname in pkg_layers:
                lyr = QgsVectorLayer("{}|layername={}".format(pkg, lname), lname, "ogr")
                if lyr is not None and lyr.isValid():
                    sources[key] = lyr
                    origin[key] = "package"
                else:
                    feedback.reportError("Package layer {} could not be opened".format(lname))
        if pkg:
            used = [lname for key, lname in SLOTS if origin.get(key) == "package"]
            kept = [lname for key, lname in SLOTS if origin.get(key) == "given" and lname in pkg_layers]
            lacking = [lname for key, lname in SLOTS if key not in sources]
            feedback.pushInfo("From the package: {}".format(", ".join(used) or "nothing"))
            if kept:
                feedback.pushInfo("Given explicitly (the package's layer not used): {}".format(", ".join(kept)))
            if lacking:
                feedback.pushInfo("Neither given nor in the package: {}".format(", ".join(lacking)))

        def field(key, fkey):
            """The field chosen for a slot; a package layer uses its standard field name."""
            if origin.get(key) == "package":
                return PKG_FIELDS[key][fkey]
            return S(fkey)

        def writer(layer_name, fields, wkb):
            opts = QgsVectorFileWriter.SaveVectorOptions()
            opts.driverName = "GPKG"; opts.layerName = layer_name
            opts.actionOnExistingFile = (WRITER_OVERWRITE_FILE if first[0]
                                         else WRITER_OVERWRITE_LAYER)
            w = QgsVectorFileWriter.create(out, fields, wkb, cm.albers(),
                                           QgsProject.instance().transformContext(), opts)
            if w.hasError() != WRITER_NO_ERROR:
                raise QgsProcessingException(f"{layer_name}: {w.errorMessage()}")
            first[0] = False
            return w

        def dump(key, layer_name, extra, fn, points_layer=None):
            src = sources.get(key)
            if src is None:
                return
            fields = QgsFields(src.fields()) if not extra else QgsFields()
            for n, t in (extra or []):
                fields.append(QgsField(n, t))
            w = writer(layer_name, fields, src.wkbType())
            wp = writer(points_layer, fields, WKB_POINT) if points_layer else None
            ta = cm.to_albers(src)
            names = src.fields().names()
            n = 0
            total = src.featureCount() or 1
            for i, f in enumerate(src.getFeatures()):
                if feedback.isCanceled(): break
                if i % 5000 == 0:
                    feedback.pushInfo(f"{layer_name}: {i:,}/{total:,}")
                g = f.geometry()
                if g.isEmpty():
                    continue
                g.transform(ta)
                if clip is not None and not clip.intersects(g):
                    continue
                attrs = list(f.attributes()) if not extra else fn(f, names)
                o = QgsFeature(fields); o.setGeometry(g)
                o.setAttributes(attrs)
                w.addFeature(o); n += 1
                if wp is not None:
                    p = g if g.type() == GEOM_POINT and not g.isMultipart() \
                        else g.pointOnSurface()
                    if p is not None and not p.isEmpty():
                        op = QgsFeature(fields); op.setGeometry(p); op.setAttributes(attrs)
                        wp.addFeature(op)
            del w
            if wp is not None:
                del wp
            tag = " from the package" if origin.get(key) == "package" else ""
            written.append(f"{layer_name} ({n:,}{tag})")
            if points_layer:
                written.append(f"{points_layer} ({n:,})")

        dump("FLOW", "flowlines", None, None)
        dump("WB", "waterbodies", None, None)
        dump("DAML", "dam_lines", None, None)
        occ, prim = field("BLD", "OCC"), field("BLD", "PRIM")

        def bld(f, names):
            oc = f[occ] if occ in names else None
            po = f[prim] if prim in names else None
            flag = f["habitable"] if "habitable" in names else None
            mob = f["mobile"] if "mobile" in names else None
            hab = hz.is_habitable(oc, po, flag=_plain(flag), uninhabited=uninhabited)
            return [1 if hab else 0, 1 if hz.is_mobile(po, oc, _plain(mob)) else 0,
                    None if oc is None else str(oc), None if po is None else str(po)]
        dump("BLD", "structures", [("habitable", FT_INT), ("mobile", FT_INT),
                                   ("occ_cls", FT_STRING), ("prim_occ", FT_STRING)], bld,
             points_layer="structure_points")
        aadt, rcls = field("ROADS", "AADT"), field("ROADS", "RCLS")

        def road(f, names):
            a = hz.nh.aadt_value(_plain(f[aadt])) if aadt in names else None
            c = str(f[rcls]) if rcls in names and _plain(f[rcls]) not in (None, "") else ""
            return [a, c, hz.road_level(a, c)]
        dump("ROADS", "roads", [("aadt", FT_DOUBLE), ("road_class", FT_STRING),
                                ("level", FT_STRING)], road)
        pax, trains = field("RAIL", "PAX"), field("RAIL", "TRAINS")

        def rr(f, names):
            p = hz.rail_passenger(_plain(f[pax]), paxcodes) if pax in names else False
            t = hz.nh.trains_value(_plain(f[trains])) if trains in names else None
            return [1 if p else 0, t, hz.rail_level(t, p)]
        dump("RAIL", "railroads", [("passenger", FT_INT), ("trains_day", FT_DOUBLE),
                                   ("level", FT_STRING)], rr)
        dump("LIFE", "lifelines", None, None)
        dump("NIDD", "dams", None, None)
        feedback.pushInfo("written: " + ", ".join(written))
        if pkg:
            for m in pk.missing_lines(pkg_info):
                feedback.pushInfo("Not in the package: " + m)
        return {"OUT": out}


def _plain(v):
    """None for NULL / empty values (a QVariant NULL is not None in QGIS)."""
    if v is None or (hasattr(v, "isNull") and v.isNull()):
        return None
    return v
