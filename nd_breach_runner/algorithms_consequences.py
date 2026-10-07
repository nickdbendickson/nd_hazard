"""Life-safety and economic consequences tool of the provider (id 'consequences').

Import it through `algorithms` (which the provider does), like the other tool modules."""
import numpy as np
from qgis.core import (QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsPointXY,
                       QgsProcessingException, QgsProcessingOutputFile,
                       QgsProcessingOutputString, QgsProcessingParameterEnum,
                       QgsProcessingParameterFeatureSource)

from . import qgis_io as qio
from .algorithms import _floats
from .algorithms_tools import _ToolBase
from .compat import SOURCE_LINE, SOURCE_POINT, SOURCE_POLYGON, set_advanced
from .core import consequences as cons
from .core import impacts as impmod

FIELD_SYNONYMS = {
    "F_ID": ("id", "structure_id", "struct_id", "fd_id", "objectid", "fid", "name",
             "bldg_id"),
    "F_OCC": ("occtype", "occupancy", "occ_type", "occ", "use", "st_damcat"),
    "F_NIGHT": ("occupants_night", "pop_night", "pop_2am", "night_pop", "occupants", "pop",
                "population", "persons"),
    "F_DAY": ("occupants_day", "pop_day", "pop_2pm", "day_pop"),
    "F_UNITS": ("units", "dwelling_units", "du", "num_units", "housing_units"),
    "F_VALUE": ("val_struct", "structure_value", "struct_value", "value", "bldg_value",
                "bldgvalue", "improvement_value"),
    "F_CONTENTS": ("val_cont", "contents_value", "cont_value", "contents"),
    "F_CONTENTS_PCT": ("contents_pct", "cont_pct", "contents_percent"),
    "F_FFH": ("found_ht", "ffh", "ffh_ft", "first_floor_ht", "first_floor_height",
              "first_floor_ft", "ffe_above_grade"),
    "F_FOUNDATION": ("found_type", "foundation", "foundation_type", "found"),
}
ROAD_SYNONYMS = {"R_AADT": ("aadt", "adt", "aadt_2023", "traffic", "vehicles_per_day"),
                 "R_NAME": ("name", "road_name", "route", "fullname", "id")}

POPULATIONS = [("night", "Night (occupants at night; the usual basis for homes)"),
               ("day", "Day (occupants during the day)"),
               ("max", "Higher of the night and day counts")]


def _txt(v):
    if v is None or (hasattr(v, "isNull") and v.isNull()):
        return ""
    return str(v).strip()


def _attr(f, name):
    if not name:
        return None
    try:
        return f[name]
    except (KeyError, IndexError):
        return None


class ConsequencesAlgorithm(_ToolBase):
    def name(self):
        return "consequences"

    def displayName(self):
        return "Life-safety and damage estimate (incremental)"

    def shortHelpString(self):
        return ("Estimates damage (structure and contents), population at risk, warning time "
                "and loss of life for the structures a dam's flood reaches, with the breach, "
                "without it (the 'Baseline ...' rasters of a flood-scenario run) and the "
                "incremental difference (with minus without, never negative, structure by "
                "structure; the idea behind ND REG_05.2026 6.4, no criterion of that section "
                "is checked). Reads 'Depth (Max).tif', 'Velocity (Max).tif' and 'Arrival "
                "Time.tif' from the dam folder. Every structure attribute is optional: an "
                "unset field is matched by name (NSI-style names work), else defaults "
                "are used and listed in the notes column.\n\n"
                "READ FIRST. The built-in depth-damage curves are GENERIC PLACEHOLDERS, not a "
                "published source: give your own curves as CSV (occupancy, depth_ft, "
                "structure_pct, contents_pct). The built-in fatality rates are APPROXIMATE "
                "DEFAULTS from memory of the Graham (1999) / Reclamation RCEM style of guidance "
                "and MUST be checked against the current published tables before submittal; "
                "give your own as CSV (severity, none, short, some, adequate: fractions of the "
                "population at risk; severity low / medium / high). The 'destroyed', severity "
                "and population-at-risk thresholds are adjustable placeholders, not standards. "
                "Warning time = arrival time minus your detection / notification lag, both "
                "counted from breach initiation. Loss of life is an expected value for "
                "screening. Structures are sampled at their point (polygons at the centroid).\n\n"
                "Writes 'Consequences (Structures).csv', 'Consequences Summary.md' and '.json', "
                "and 'Consequences (Affected Structures).geojson' into the dam folder.")

    def initAlgorithm(self, config=None):
        self._file("DAM_FOLDER", "Dam folder with 'Depth (Max).tif' etc.", folder=True,
                   optional=False)
        self.addParameter(QgsProcessingParameterFeatureSource(
            "STRUCTURES", "Structures (points, or polygons sampled at the centroid)",
            [SOURCE_POINT, SOURCE_POLYGON], optional=False))
        for key, desc, numeric in (
                ("F_ID", "  identifier field", False),
                ("F_OCC", "  occupancy type field (residential, commercial, industrial, "
                          "public, agricultural, other; NSI codes work)", False),
                ("F_NIGHT", "  occupants at night field", True),
                ("F_DAY", "  occupants by day field", True),
                ("F_UNITS", "  residential unit count field (times persons per unit)", True),
                ("F_VALUE", "  structure value field ($)", True),
                ("F_CONTENTS", "  contents value field ($)", True),
                ("F_CONTENTS_PCT", "  contents value as % of the structure value field", True),
                ("F_FFH", "  first-floor height above grade field (ft)", True),
                ("F_FOUNDATION", "  foundation type field (slab, crawlspace, basement, "
                                 "pier)", False)):
            self._field(key, "Structures:" + desc, "STRUCTURES", numeric=numeric, advanced=True)
        self.addParameter(set_advanced(QgsProcessingParameterEnum(
            "OCC_DEFAULT", "Occupancy when there is no occupancy field",
            [o.capitalize() for o in cons.OCCUPANCIES], defaultValue=0)))
        self._layer("ROADS", "Roads (lines): flooded length and vehicles exposed", SOURCE_LINE)
        self._field("R_AADT", "Roads: AADT field (vehicles per day)", "ROADS", advanced=True)
        self._field("R_NAME", "Roads: identifier field", "ROADS", numeric=False, advanced=True)
        self._layer("RAIL", "Railways (lines): flooded length", SOURCE_LINE)
        self._file("CURVES_CSV", "Depth-damage curves CSV (occupancy, depth_ft, structure_pct, "
                   "contents_pct); replaces the generic placeholder of that occupancy",
                   ext="csv")
        self._file("FATALITY_CSV", "Fatality rate table CSV (severity, none, short, some, "
                   "adequate); replaces the built-in approximate default", ext="csv")
        d = cons.ConsequenceParams()
        self._num("PERSONS_PER_UNIT", "Persons per residential unit (when only a unit count "
                  "is given)", d.persons_per_unit, minimum=0)
        self._num("DEFAULT_UNITS", "Units assumed for a residential structure with no "
                  "occupant or unit data", d.default_units_residential, minimum=0,
                  advanced=True)
        self.addParameter(QgsProcessingParameterEnum(
            "POPULATION", "Population basis for PAR and loss of life",
            [p[1] for p in POPULATIONS], defaultValue=0))
        self._num("CONTENTS_RATIO", "Contents value / structure value when neither is given",
                  d.contents_ratio, minimum=0, advanced=True)
        self._num("LAG_HR", "Detection / notification lag after breach initiation (hours)",
                  d.lag_hr, minimum=0)
        self._num("BASE_LAG_HR", "Same lag for the no-breach flood (empty = the same)",
                  optional=True, minimum=0, advanced=True)
        self._text("WARN_EDGES", "Warning classes: end of 'short' and of 'some' warning "
                   "(minutes)", "15, 60")
        self._num("PAR_DEPTH_FT", "Occupants are at risk where the depth is at least (ft; the "
                  "starting value is the engineer's, a screening value you choose)",
                  d.par_depth_ft, minimum=0)
        self._num("DESTROY_DV", "Structure destroyed (100% damage) at depth x velocity of "
                  "(ft2/s); a user-adjustable placeholder, not a standard", d.destroy_dv_ft2s,
                  minimum=0)
        self._num("DESTROY_DEPTH_FT", "... or at a depth of (ft; the starting value is the "
                  "engineer's placeholder, not a standard)", d.destroy_depth_ft, minimum=0)
        self._num("SEV_MED_DEPTH_FT", "Flood severity medium from a depth of (ft; the starting "
                  "value is the engineer's placeholder)", d.sev_med_depth_ft, minimum=0,
                  advanced=True)
        self._num("SEV_MED_DV", "... or depth x velocity of (ft2/s)", d.sev_med_dv_ft2s,
                  minimum=0, advanced=True)
        self._num("SEV_HIGH_DEPTH_FT", "Flood severity high from a depth of (ft; the starting "
                  "value is the engineer's placeholder)", d.sev_high_depth_ft, minimum=0,
                  advanced=True)
        self._num("SEV_HIGH_DV", "... or depth x velocity of (ft2/s)", d.sev_high_dv_ft2s,
                  minimum=0, advanced=True)
        self._num("ROAD_WET_FT", "Road / rail counts as flooded from a depth of (ft; the "
                  "starting value is the engineer's, a screening value you choose)",
                  d.road_wet_ft, minimum=0, advanced=True)
        self._num("VEHICLE_HR", "Traffic exposed at a flooded road: hours of AADT / 24",
                  d.vehicle_window_hr, minimum=0, advanced=True)
        self._bool("INCLUDE_DRY", "Keep structures the flood never reaches in the CSV",
                   False, advanced=True)
        self._bool("WRITE_GEOJSON", "Write the affected structures as GeoJSON", True)
        self.addOutput(QgsProcessingOutputFile("CSV", "Consequences (Structures).csv"))
        self.addOutput(QgsProcessingOutputFile("REPORT", "Consequences Summary.md"))
        self.addOutput(QgsProcessingOutputFile("SUMMARY_JSON", "Consequences Summary.json"))
        self.addOutput(QgsProcessingOutputFile("GEOJSON", "Affected structures (GeoJSON)"))
        self.addOutput(QgsProcessingOutputString("RESULT", "Result"))

    # ------------------------------------------------------------------------------
    def _params(self, parameters, context):
        g = lambda n: self.parameterAsDouble(parameters, n, context)
        try:
            edges = _floats(self._str(parameters, "WARN_EDGES", context), (15, 60))
            base_lag = self._opt(parameters, "BASE_LAG_HR", context)
            return cons.ConsequenceParams(
                persons_per_unit=g("PERSONS_PER_UNIT"), default_units_residential=g(
                    "DEFAULT_UNITS"),
                population=POPULATIONS[self.parameterAsEnum(parameters, "POPULATION",
                                                            context)][0],
                contents_ratio=g("CONTENTS_RATIO"),
                default_occupancy=cons.OCCUPANCIES[self.parameterAsEnum(
                    parameters, "OCC_DEFAULT", context)],
                par_depth_ft=g("PAR_DEPTH_FT"), destroy_dv_ft2s=g("DESTROY_DV"),
                destroy_depth_ft=g("DESTROY_DEPTH_FT"), sev_med_depth_ft=g("SEV_MED_DEPTH_FT"),
                sev_med_dv_ft2s=g("SEV_MED_DV"), sev_high_depth_ft=g("SEV_HIGH_DEPTH_FT"),
                sev_high_dv_ft2s=g("SEV_HIGH_DV"), lag_hr=g("LAG_HR"), baseline_lag_hr=base_lag,
                warning_edges_min=tuple(edges), road_wet_ft=g("ROAD_WET_FT"),
                vehicle_window_hr=g("VEHICLE_HR"),
                include_dry=self.parameterAsBool(parameters, "INCLUDE_DRY", context)).validate()
        except ValueError as e:
            raise QgsProcessingException(str(e))

    def _fields(self, parameters, context, names, keys, synonyms):
        """{key: field name}: the chosen field, else a name match on the synonyms, else ''."""
        low = {n.lower(): n for n in names}
        out = {}
        for k in keys:
            chosen = self._str(parameters, k, context)
            if not chosen:
                chosen = next((low[s] for s in synonyms.get(k, ()) if s in low), "")
            out[k] = chosen
        return out

    @staticmethod
    def _raster_crs(wkt):
        if not wkt:
            return QgsCoordinateReferenceSystem("")
        from .core import gdal_io
        try:
            epsg = gdal_io.epsg_of(wkt)
        except Exception:
            epsg = None
        if epsg:
            return QgsCoordinateReferenceSystem(epsg)
        crs = QgsCoordinateReferenceSystem()
        try:
            crs.createFromWkt(wkt)
        except Exception:
            pass
        return crs

    def _read_structures(self, parameters, context, grid, crs, params, feedback):
        src = self.parameterAsSource(parameters, "STRUCTURES", context)
        if src is None:
            raise QgsProcessingException("Choose the structures layer")
        names = qio.field_names(src)
        keys = list(FIELD_SYNONYMS)
        fld = self._fields(parameters, context, names, keys, FIELD_SYNONYMS)
        to_r = QgsCoordinateTransform(src.sourceCrs(), crs, context.transformContext())
        to_l = QgsCoordinateTransform(crs, src.sourceCrs(), context.transformContext())
        xmin, ymin, xmax, ymax = grid.bounds
        center = (0.5 * (xmin + xmax), 0.5 * (ymin + ymax))
        radius = 0.5 * float(np.hypot(xmax - xmin, ymax - ymin))
        used = {k: v for k, v in fld.items() if v}
        feedback.pushInfo("Structure fields: " + (", ".join(
            "{} = {}".format(k[2:].lower(), v) for k, v in used.items()) or "none matched"))
        structs = []
        for f in qio._features(src, center, radius, to_l):
            g = f.geometry()
            if g is None or g.isEmpty():
                continue
            p = to_r.transform(g.centroid().asPoint())
            num = lambda key: qio.num(_attr(f, fld[key]))
            ident = _txt(_attr(f, fld["F_ID"])) or str(f.id())
            structs.append(cons.make_structure(
                ident, p.x(), p.y(), params, occupancy=_txt(_attr(f, fld["F_OCC"])) or None,
                occupants_night=num("F_NIGHT"), occupants_day=num("F_DAY"),
                units=num("F_UNITS"), structure_value=num("F_VALUE"),
                contents_value=num("F_CONTENTS"), contents_pct=num("F_CONTENTS_PCT"),
                ffh_ft=num("F_FFH"), foundation=_txt(_attr(f, fld["F_FOUNDATION"])) or None))
        return structs

    def _read_lines(self, parameters, context, grid, crs, layer, kind, rec, aadt):
        src = self.parameterAsSource(parameters, layer, context)
        if src is None:
            return
        names = qio.field_names(src)
        if kind == impmod.KIND_ROAD:
            fld = self._fields(parameters, context, names, list(ROAD_SYNONYMS), ROAD_SYNONYMS)
        else:
            fld = {"R_AADT": "", "R_NAME": ""}
        to_r = QgsCoordinateTransform(src.sourceCrs(), crs, context.transformContext())
        to_l = QgsCoordinateTransform(crs, src.sourceCrs(), context.transformContext())
        xmin, ymin, xmax, ymax = grid.bounds
        center = (0.5 * (xmin + xmax), 0.5 * (ymin + ymax))
        radius = 0.5 * float(np.hypot(xmax - xmin, ymax - ymin))
        lines = qio.lines(src, to_r, to_l, center, radius,
                          fields=[x for x in (fld["R_AADT"], fld["R_NAME"]) if x])
        unit_m = _ground_m_per_unit(crs, center, context)
        for i, (verts, at) in enumerate(lines):
            ident = _txt(at.get(fld["R_NAME"])) if fld["R_NAME"] else ""
            ident = ident or "{} {}".format(kind, i + 1)
            rec.add_lines([verts], kind, ids=[ident], unit_m=unit_m)
            v = qio.num(at.get(fld["R_AADT"])) if fld["R_AADT"] else None
            if v is not None:
                aadt[ident] = max(aadt.get(ident, 0.0), v)

    def processAlgorithm(self, parameters, context, feedback):
        folder = self.parameterAsFile(parameters, "DAM_FOLDER", context)
        params = self._params(parameters, context)
        curves, table, table_src = None, None, None
        try:
            cpath = self._fileparam(parameters, "CURVES_CSV", context)
            if cpath:
                curves = cons.merge_curves(cons.load_curves_csv(cpath))
            fpath = self._fileparam(parameters, "FATALITY_CSV", context)
            if fpath:
                table = cons.load_fatality_csv(fpath)
                table_src = fpath
            grid, with_f, base_f, notes = cons.read_dam_folder(folder)
        except ImportError:
            raise QgsProcessingException("GDAL's Python bindings are not available; run this "
                                         "from QGIS.")
        except (OSError, ValueError, RuntimeError) as e:
            raise QgsProcessingException(str(e))
        for n in notes:
            feedback.pushInfo(n)
        crs = self._raster_crs(grid.crs_wkt)
        structs = self._read_structures(parameters, context, grid, crs, params, feedback)
        if not structs:
            raise QgsProcessingException("No structures fall inside the dam folder's rasters "
                                         "(check the layer and its CRS)")
        rec, aadt = impmod.Receptors(), {}
        self._read_lines(parameters, context, grid, crs, "ROADS", impmod.KIND_ROAD, rec, aadt)
        self._read_lines(parameters, context, grid, crs, "RAIL", impmod.KIND_RAIL, rec, aadt)
        try:
            rows, summary = cons.assess(structs, grid, with_f, base_f, params, curves, table,
                                        "us", rec if len(rec) else None, aadt or None,
                                        fatality_source=table_src)
        except ValueError as e:
            raise QgsProcessingException(str(e))
        summary["notes"] = notes + summary["notes"]
        epsg = None
        try:
            from .core import gdal_io
            epsg = gdal_io.epsg_of(grid.crs_wkt) if grid.crs_wkt else None
        except Exception:
            epsg = None
        try:
            paths = cons.write_outputs(
                folder, rows, summary, epsg,
                geojson=self.parameterAsBool(parameters, "WRITE_GEOJSON", context))
        except OSError as e:
            raise QgsProcessingException("Could not write the results: {}".format(e))
        wb, inc = summary["with_breach"], summary["incremental"]
        msg = ("{} structures flooded; damage ${:,.0f} (incremental ${:,.0f}); population at "
               "risk {:.0f} (incremental {:.0f}); estimated loss of life {:.2f} (incremental "
               "{:.2f}). Screening estimate: check the curves and fatality table first."
               .format(wb["structures_flooded"], wb["damage_usd"], inc["damage_usd"],
                       wb["par"], inc["par"], wb["estimated_lol"], inc["estimated_lol"]))
        feedback.pushInfo(msg)
        for n in summary["notes"]:
            feedback.reportError("Check: " + n, fatalError=False)
        if paths.get("geojson"):
            self.load(context, paths["geojson"], "Consequences: affected structures")
        return {"CSV": paths["csv"], "REPORT": paths["markdown"],
                "SUMMARY_JSON": paths["json"], "GEOJSON": paths["geojson"], "RESULT": msg}


CONSEQUENCE_ALGORITHMS = (ConsequencesAlgorithm,)


def _ground_m_per_unit(crs, center, context, step=100.0):
    """3.10.0: ground metres per unit of the rasters' CRS at the grid centre (the dam folder's
    rasters are in the DEM's CRS, which may be in feet or Web Mercator), from a short east and
    north step measured on the ellipsoid; 1.0 when it cannot be measured."""
    try:
        to_ll = QgsCoordinateTransform(crs, QgsCoordinateReferenceSystem("EPSG:4326"),
                                       context.transformContext())
        x, y = center
        lon0, lat0 = _xy(to_ll.transform(QgsPointXY(x, y)))
        out = []
        for dx, dy in ((step, 0.0), (0.0, step)):
            lon, lat = _xy(to_ll.transform(QgsPointXY(x + dx, y + dy)))
            out.append(_haversine_m(lon0, lat0, lon, lat) / step)
        k = float(np.mean(out))
        return k if np.isfinite(k) and k > 0 else 1.0
    except Exception:
        return 1.0


def _xy(p):
    return float(p.x()), float(p.y())


def _haversine_m(lon0, lat0, lon1, lat1, r=6371008.8):
    p0, p1 = np.radians(lat0), np.radians(lat1)
    a = (np.sin((p1 - p0) / 2) ** 2
         + np.cos(p0) * np.cos(p1) * np.sin(np.radians(lon1 - lon0) / 2) ** 2)
    return float(2 * r * np.arcsin(np.sqrt(a)))
