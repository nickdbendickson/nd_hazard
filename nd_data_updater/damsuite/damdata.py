"""The data of one dam (1.5.0): "Get data for this dam" and the inputs every tool fills from it.

dam.json gets a top-level "data" section (dam_record keeps unknown top-level keys, so plugins older
than this release keep it untouched):

    "data": {"<role>": {"path": "data/structures.gpkg|layername=structures",   relative to the
                        "source": "offline pack NID_dams (fetched 2026-09-30)", dam folder
                        "when": "2026-10-05T14:05:00", "count": 812, "note": ""}, ...}

Roles (DATA_ROLES): dem, nlcd, structures, structure_points, address_points, roads, rail,
crossings, bridges, lifelines, nid_dams, dwr_dams, dwr_dam_lines, flowlines, waterbodies,
flood_zones, counties. The files are written in <dam folder>/data/.

Where each role comes from (`get_data_for_dam`, first that has it):
  dem     the user's own DEM in the data folder's dem/ (a VRT window over it, no copy and no
          resampling), else the chosen terrain service through the shared tile cache
          (terrain_cache; a VRT over cached tiles)
  nlcd    a raster in the data folder's nlcd/ (VRT window), else the chosen land-cover service
          (one GeoTIFF for the area)
  structures, structure_points, roads, rail, flowlines, lifelines, nid_dams
          the suite package (ND Dam Hazard's "Export for Screening": the standard schema the
          Screening tools and the Runner's receptors read, with ND Dam Hazard's habitability and
          AADT rules) when the caller gives `make_package`; else the raw data set of the pack
  every vector role
          the offline pack (when it covers the area), else the drop-in folder (dwr/), else the
          chosen ArcGIS service (live download of the area)
  1.10.0: the pack is suitedata.active_pack() - none with Data source = Download (then every
          role comes from the servers); a format-2 pack also gives the land cover and DWR files

Decisions:
  * Dam values (name, height, storage, county, existing hazard class, location) found in the NID /
    DWR record of the dam are offered to dam.json with dam_record's own rules: an empty field
    takes the value, a different value already there is NOT replaced (it is recorded as a
    conflict). Source tool name: TOOL.
  * The dam's location is dam.json's lon / lat (or x / y / crs); else the NID record named by
    nid_id, else the DWR record named by dwr_structure_id. Without any, nothing is fetched and the
    message says how to give the location.
  * An existing data file is replaced only with overwrite=True (or a role that failed before).
  * One role failing never stops the others; the result lists what failed and why.
  * `autofill` fills EMPTY tool inputs only. A choice the user made is never replaced.
Pure Python; GDAL / OGR (osgeo) are used only for raster windows and for drop-in files that are
not GeoPackages, and only when installed.
"""
import datetime
import json
import math
import os

from . import dam_record as DR
from . import services as SV
from . import suitedata as SD
from . import suitegpkg as G
from . import terrain_cache as TC

DATA_KEY = "data"
DATA_DIR = "data"
TOOL = "ndsuite_data"
PACKAGE = "suite_package.gpkg"

DATA_ROLES = ("dem", "nlcd", "structures", "structure_points", "address_points", "roads", "rail",
              "crossings", "bridges", "lifelines", "nid_dams", "dwr_dams", "dwr_dam_lines",
              "flowlines", "waterbodies", "flood_zones", "counties")
LABELS = {"dem": "DEM", "nlcd": "land cover", "structures": "structures",
          "structure_points": "structure points", "address_points": "address points",
          "roads": "roads", "rail": "railroads", "crossings": "grade crossings",
          "bridges": "bridges and culverts", "lifelines": "lifeline facilities",
          "nid_dams": "NID dams", "dwr_dams": "DWR dams", "dwr_dam_lines": "DWR dam lines",
          "flowlines": "3DHP flowlines", "waterbodies": "3DHP waterbodies",
          "flood_zones": "FEMA flood zones", "counties": "counties"}
# role -> layer of the suite package (ND Dam Hazard's export for Screening)
PACKAGE_LAYERS = {"structures": "structures", "structure_points": "structure_points",
                  "roads": "roads", "rail": "railroads", "flowlines": "flowlines",
                  "lifelines": "lifelines", "nid_dams": "dams"}
# role -> data sets of the offline pack, first found wins
PACK_SETS = {"structures": ("USA_Structures",), "structure_points": ("NSI_structures",),
             "address_points": ("NDGISHUB_address_points",),
             "roads": ("ND_roads_NG911", "ND_county_roads"), "rail": ("NARN_rail_lines",),
             "crossings": ("FRA_grade_crossings",), "bridges": ("NBI_bridges",),
             "nid_dams": ("NID_dams",), "flowlines": ("3DHP_flowlines", "NHD_flowlines"),
             "waterbodies": ("3DHP_waterbodies", "NHD_waterbodies"), "dwr_dams": ("DWR_dams",),
             "dwr_dam_lines": ("DWR_dam_lines",), "counties": ("ND_counties",),
             "flood_zones": ("FEMA_flood_zones",), "lifelines": ()}
# role -> the suite service role (services.ROLES) used for a live download
SERVICE_ROLE = {"structure_points": None, "lifelines": None}
MI = 1609.344
DEFAULT_VECTOR_HALF_MI = 15.0      # structures, roads ... within this of the dam
DEFAULT_DEM_HALF_MI = 30.0         # the Runner's domain may grow to 60 mi along the valley
DEFAULT_DEM_CELL_M = 10.0          # terrain-service downloads (a local DEM keeps its own cells)
NLCD_CELL_M = 30.0


class DataError(Exception):
    """Getting a dam's data cannot start; the message says what to do."""


def _now():
    return datetime.datetime.now().isoformat(timespec="seconds")


# --------------------------------------------------------------- the record --
def data_section(rec):
    """{role: entry} of a DamRecord (empty dict when none)."""
    if rec is None:
        return {}
    d = (rec.extra or {}).get(DATA_KEY)
    return d if isinstance(d, dict) else {}


def _split(path):
    p, _, lay = (path or "").partition("|")
    return p, lay


def rel_path(folder, path):
    """A path relative to the dam folder when it is inside it (keeps |layername=...)."""
    p, lay = _split(path)
    try:
        r = os.path.relpath(os.path.abspath(p), os.path.abspath(folder))
        if not r.startswith(".."):
            p = r.replace(os.sep, "/")
    except ValueError:                                           # another drive (Windows)
        pass
    return p + ("|" + lay if lay else "")


def set_entry(rec, role, path, source="", count=None, note=""):
    if role not in DATA_ROLES:
        raise KeyError("unknown data role {!r}".format(role))
    sec = rec.extra.setdefault(DATA_KEY, {})
    if not isinstance(sec, dict):
        sec = rec.extra[DATA_KEY] = {}
    e = {"path": rel_path(rec.folder, path), "source": source, "when": _now()}
    if count is not None:
        e["count"] = int(count)
    if note:
        e["note"] = note
    sec[role] = e
    return e


def resolve(folder, entry_or_path):
    """Absolute path (with |layername=...) of a data entry, or "" when its file is missing."""
    path = entry_or_path.get("path", "") if isinstance(entry_or_path, dict) else entry_or_path
    if not path:
        return ""
    p, lay = _split(path)
    full = p if os.path.isabs(p) else os.path.normpath(os.path.join(folder, p))
    if not os.path.exists(full):
        return ""
    return full + ("|" + lay if lay else "")


def dam_data(folder, rec=None):
    """{role: absolute path} of the data files of a dam folder that exist."""
    if rec is None:
        try:
            rec = DR.load(folder)
        except ValueError:
            rec = None
    out = {}
    for role, e in data_section(rec).items():
        if role in DATA_ROLES and isinstance(e, dict):
            p = resolve(folder, e)
            if p:
                out[role] = p
    return out


def autofill(values, mapping, folder, rec=None, fields=None):
    """Fill the EMPTY tool inputs from the dam's data section (and dam.json fields).

    values: {parameter: current value} - only parameters listed here are filled;
    mapping: {parameter: data role} for layers / rasters; fields: {parameter: dam.json field}
    for numbers. Returns (values, notes, used)."""
    values = dict(values or {})
    notes, used = [], {}
    if not folder:
        return values, notes, used
    if rec is None:
        try:
            rec = DR.load(folder)
        except ValueError as e:
            return values, ["dam.json not read: {}".format(e)], used
    if rec is None:
        return values, notes, used
    data = dam_data(folder, rec)
    for param, role in (mapping or {}).items():
        if param not in values or values.get(param) not in (None, ""):
            continue
        if role in data:
            values[param] = data[role]
            used[param] = role
            notes.append("{} from the dam folder ({}): {}".format(
                param, LABELS.get(role, role), data[role]))
    for param, field in (fields or {}).items():
        if param not in values or values.get(param) not in (None, ""):
            continue
        v = rec.get(field)
        if v not in (None, ""):
            values[param] = v
            used[param] = field
            notes.append("{} from dam.json ({}): {}".format(param, field, v))
    return values, notes, used


# --------------------------------------------------------------- location --
def _lonlat_box(lon, lat, half_mi):
    dlat = half_mi / 69.0
    dlon = half_mi / (69.17 * max(0.1, math.cos(math.radians(lat))))
    return (lon - dlon, lat - dlat, lon + dlon, lat + dlat)


def _box_5070(lon, lat, half_mi):
    x, y = G.to_5070(lon, lat)
    h = half_mi * MI
    return (x - h, y - h, x + h, y + h)


def _norm_id(v):
    return "".join(ch for ch in str(v or "").upper() if ch.isalnum())


def _props(f):
    return {str(k).lower(): v for k, v in (f.get("properties") or {}).items()}


def find_in_pack(reader, rec, log=None):
    """(lon, lat, props, set name) of the dam's NID / DWR record in the pack, or None."""
    if reader is None or rec is None:
        return None
    from .offline_pack import ND_BBOX
    tries = [("NID_dams", ("nidid", "nid_id", "nid"), rec.get("nid_id")),
             ("DWR_dams", ("structure_id", "structureid", "dam_id", "damid", "struct_id",
                           "dwr_id", "id"), rec.get("dwr_structure_id"))]
    for name, keys, want in tries:
        if not want or not reader.has(name):
            continue
        w = _norm_id(want)
        for f in reader.query(name, ND_BBOX):
            p = _props(f)
            if any(_norm_id(p.get(k)) == w for k in keys if p.get(k) not in (None, "")):
                xy = G.point_on_surface(f.get("geometry"))
                if xy:
                    return xy[0], xy[1], p, name
    return None


def dam_location(rec, reader=None):
    """(lon, lat, how) of the dam, or (None, None, why not)."""
    if rec is None:
        return None, None, "the dam folder has no dam.json"
    lon, lat = rec.get("lon"), rec.get("lat")
    if isinstance(lon, (int, float)) and isinstance(lat, (int, float)):
        return float(lon), float(lat), "dam.json lon / lat"
    x, y, crs = rec.get("x"), rec.get("y"), str(rec.get("crs") or "").upper()
    if isinstance(x, (int, float)) and isinstance(y, (int, float)) and crs.endswith("5070"):
        lo, la = G.from_5070(float(x), float(y))
        return lo, la, "dam.json x / y (EPSG:5070)"
    if isinstance(x, (int, float)) and isinstance(y, (int, float)) and crs:
        try:
            from osgeo import osr
            s, t = osr.SpatialReference(), osr.SpatialReference()
            s.SetFromUserInput(crs)
            t.ImportFromEPSG(4326)
            for sr in (s, t):
                try:
                    sr.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
                except AttributeError:
                    pass
            lo, la, _z = osr.CoordinateTransformation(s, t).TransformPoint(float(x), float(y))
            return lo, la, "dam.json x / y ({})".format(crs)
        except Exception:                                        # noqa
            pass
    hit = find_in_pack(reader, rec)
    if hit:
        return hit[0], hit[1], "{} record in the offline pack".format(hit[3])
    return None, None, ("dam.json has no location: set lon / lat (Screening's hand-off or ND Dam "
                        "Hazard writes it), or give nid_id / dwr_structure_id with an offline pack")


def _num(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) and x > 0 else None


def dam_values(props):
    """dam.json fields from an NID / DWR record's attributes (lower-case keys)."""
    g = lambda *ks: next((props[k] for k in ks if props.get(k) not in (None, "")), None)  # noqa
    out = {"name": g("name", "dam_name", "damname"),
           "nid_id": g("nidid", "nid_id"),
           "county": g("county", "countyname", "county_name"),
           "height_ft": _num(g("nid_height", "dam_height", "height_ft", "height",
                               "structural_height")),
           # the largest storage the record gives: the conservative side for a breach
           "storage_acft": max([v for v in (_num(g("nid_storage")), _num(g("max_storage")),
                                            _num(g("storage_acft")), _num(g("storage")),
                                            _num(g("normal_storage"))) if v] or [None],
                               key=lambda v: v or 0),
           "surface_area_ac": _num(g("surface_area", "surface_area_ac", "surfacearea")),
           "dam_length_ft": _num(g("dam_length", "dam_length_ft", "crest_length")),
           "existing_hazard_class": g("hazard_potential", "hazard", "hazard_class",
                                      "haz_class", "hazardclass")}
    return {k: v for k, v in out.items() if v not in (None, "")}


# --------------------------------------------------------------- writing --
def _field_type(values):
    kinds = set()
    for v in values:
        if v is None or v == "":
            continue
        if isinstance(v, bool) or isinstance(v, int):
            kinds.add("int")
        elif isinstance(v, float):
            kinds.add("real")
        else:
            kinds.add("text")
    if not kinds or "text" in kinds:
        return "text"
    return "real" if "real" in kinds else "int"


def write_features(path, layer, feats, source=""):
    """Write lon / lat GeoJSON features to a GeoPackage layer in EPSG:5070. Returns the count."""
    rows = []
    for f in feats or ():
        g = G.project_5070(f.get("geometry"))
        if g is not None and G.points_of(g):
            rows.append((f, g))
    names, seen = [], set()
    for f, _g in rows:
        for k in (f.get("properties") or {}):
            kl = str(k).lower()
            if kl not in seen and kl not in ("fid", "geom", "suite_source"):
                seen.add(kl)
                names.append(str(k))
    fields = [(n, _field_type((f.get("properties") or {}).get(n) for f, _g in rows))
              for n in names[:200]]
    kinds = {G.kind(g) for _f, g in rows} - {None}
    gtype = {"polygon": "MULTIPOLYGON", "line": "MULTILINESTRING"}.get(
        next(iter(kinds)) if len(kinds) == 1 else "", "POINT")
    w = G.GpkgWriter(path)
    try:
        lyr = w.layer(layer, gtype, fields + [("suite_source", "text")],
                      description="ND dam suite data ({})".format(source))
        n = 0
        for f, g in rows:
            if gtype == "POINT" and G.kind(g) != "point":
                xy = G.point_on_surface(g)
                g = {"type": "Point", "coordinates": xy} if xy else None
            elif gtype != "POINT" and G.kind(g) == "point":
                continue
            a = dict((f.get("properties") or {}))
            a["suite_source"] = source
            lyr.add(g, a)
            n += 1
        w.close()
    except BaseException:
        w.abort()
        raise
    return n


def _plain_gpkg(p):
    """A GeoPackage whose feature layers are all EPSG:4326 / 5070 (read without GDAL)."""
    if not p.lower().endswith(".gpkg"):
        return False
    try:
        layers = [l for l in G.list_layers(p) if l["data_type"] == "features"]
    except Exception:                                            # noqa
        return False
    return bool(layers) and all(int(l["srs_id"] or 0) in (4326, 5070) for l in layers)


def read_vector_file(path, box_lonlat):
    """GeoJSON features (lon / lat) of a vector file that touch the box. GeoPackages written in
    EPSG:4326 / 5070 are read without GDAL; anything else needs OGR."""
    p, lay = _split(path)
    try:
        from osgeo import ogr, osr
    except Exception:                                            # noqa
        ogr = None
    if ogr is not None and not hasattr(ogr, "Open"):             # a stand-in osgeo (tests)
        ogr = None
    if ogr is None or _plain_gpkg(p):
        if not p.lower().endswith(".gpkg"):
            raise DataError("GDAL is needed to read {}".format(os.path.basename(p)))
        layers = [l for l in G.list_layers(p) if l["data_type"] == "features"]
        lay = lay.replace("layername=", "") if lay else (layers[0]["name"] if layers else "")
        info = next((l for l in layers if l["name"] == lay), None)
        if info is None:
            raise DataError("{} has no layer {}".format(p, lay))
        out = []
        for g, attrs in G.read_features(p, lay):
            if g is None:
                continue
            if int(info["srs_id"] or 0) == 5070:
                g = G.map_coords(g, G.from_5070)
            if g and _touches(g, box_lonlat):
                out.append({"type": "Feature", "geometry": g, "properties": attrs})
        return out
    from . import gdalsafe as GS
    ds = GS.open_vector(p)
    name = lay.replace("layername=", "") if lay else ""
    lyr = ds.GetLayerByName(name) if name else ds.GetLayer(0)
    if lyr is None:
        raise DataError("{} has no layer {}".format(p, name))
    wgs = osr.SpatialReference()
    wgs.ImportFromEPSG(4326)
    try:
        wgs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    except AttributeError:
        pass
    src = lyr.GetSpatialRef()
    tr = None
    if src is not None:
        try:
            src.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        except AttributeError:
            pass
        tr = osr.CoordinateTransformation(src, wgs)
        back = osr.CoordinateTransformation(wgs, src)
        xs, ys = [], []
        for x, y in ((box_lonlat[0], box_lonlat[1]), (box_lonlat[2], box_lonlat[1]),
                     (box_lonlat[0], box_lonlat[3]), (box_lonlat[2], box_lonlat[3])):
            a, b, _z = back.TransformPoint(x, y)
            xs.append(a)
            ys.append(b)
        lyr.SetSpatialFilterRect(min(xs), min(ys), max(xs), max(ys))
    else:
        lyr.SetSpatialFilterRect(*box_lonlat)
    out = []
    for f in lyr:
        geom = f.GetGeometryRef()
        if geom is None:
            continue
        geom = geom.Clone()
        if tr is not None:
            geom.Transform(tr)
        out.append({"type": "Feature", "geometry": json.loads(geom.ExportToJson()),
                    "properties": f.items()})
    return out


def _touches(geom, box):
    try:
        from .offline_pack import intersects_rect
        return intersects_rect(geom, box)
    except Exception:                                            # noqa
        return True


def raster_window(src, box_lonlat, out_vrt):
    """A VRT window over a raster (its own CRS and cells, nothing resampled). Needs GDAL."""
    try:
        from osgeo import gdal, osr
    except Exception:                                            # noqa
        raise DataError("GDAL is needed to cut a window of {}".format(os.path.basename(src)))
    from . import gdalsafe as GS
    ds = GS.open_raster(src)
    wkt = ds.GetProjection()
    if not wkt:
        raise DataError("{} has no coordinate system".format(src))
    s = osr.SpatialReference()
    s.ImportFromWkt(wkt)
    w = osr.SpatialReference()
    w.ImportFromEPSG(4326)
    for sr in (s, w):
        try:
            sr.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        except AttributeError:
            pass
    tr = osr.CoordinateTransformation(w, s)
    xs, ys = [], []
    for i in range(5):
        for j in range(5):
            lon = box_lonlat[0] + (box_lonlat[2] - box_lonlat[0]) * i / 4.0
            lat = box_lonlat[1] + (box_lonlat[3] - box_lonlat[1]) * j / 4.0
            x, y, _z = tr.TransformPoint(lon, lat)
            xs.append(x)
            ys.append(y)
    gt = ds.GetGeoTransform()
    rx0, ry1 = gt[0], gt[3]
    rx1, ry0 = gt[0] + ds.RasterXSize * gt[1], gt[3] + ds.RasterYSize * gt[5]
    x0, x1 = max(min(xs), rx0), min(max(xs), rx1)
    y0, y1 = max(min(ys), ry0), min(max(ys), ry1)
    if x1 <= x0 or y1 <= y0:
        raise DataError("{} does not cover the dam's area".format(os.path.basename(src)))
    os.makedirs(os.path.dirname(os.path.abspath(out_vrt)), exist_ok=True)
    GS.checked(gdal.Translate(out_vrt, ds, format="VRT", projWin=[x0, y1, x1, y0]),
               "cutting the raster window", gdal)
    covered = (x1 - x0) * (y1 - y0) / max(1e-9, (max(xs) - min(xs)) * (max(ys) - min(ys)))
    return out_vrt, covered


# --------------------------------------------------------------- the step --
def get_data_for_dam(folder, data_folder=None, catalog=None, http_get=None, make_package=None,
                     roles=None, vector_half_mi=DEFAULT_VECTOR_HALF_MI,
                     dem_half_mi=DEFAULT_DEM_HALF_MI, dem_cell_m=DEFAULT_DEM_CELL_M,
                     overwrite=False, log=None, progress=None, cancel=None, version=""):
    """Fill <dam folder>/data/ and dam.json's data section. Returns
    {"done": {role: entry}, "failed": {role: reason}, "skipped": {role: reason},
     "values": {field: value} offered to dam.json, "refused": [fields], "location": how,
     "lines": [text]}.

    make_package(out_path, box_lonlat) -> report: ND Dam Hazard's suite export when installed
    (the standard schema); None writes the pack's raw data sets instead."""
    log = log or (lambda m: None)
    prog = progress or (lambda p, m="": None)
    data_folder = SD.data_folder() if data_folder is None else data_folder
    catalog = catalog or SV.load()
    http_get = http_get or SV.urllib_get
    roles = [r for r in (roles or DATA_ROLES) if r in DATA_ROLES]
    if not folder or not os.path.isdir(folder):
        raise DataError("dam folder not found: {}".format(folder))
    try:
        rec = DR.load(folder, create=True)
    except ValueError as e:
        raise DataError(str(e))
    reader = None
    pack = SD.active_pack(data_folder or "")             # 1.10.0: "" in Download mode
    if pack:
        try:
            from .offline_pack import open_pack
            reader = open_pack(pack, log=log)
        except Exception as e:                                   # noqa
            log("offline pack not used: {}".format(e))
    lon, lat, how = dam_location(rec, reader)
    if lon is None:
        raise DataError(how)
    out = {"done": {}, "failed": {}, "skipped": {}, "values": {}, "refused": [],
           "location": how, "lines": []}
    say = lambda m: (out["lines"].append(m), log(m))             # noqa
    say("Dam {} at {:.5f}, {:.5f} ({})".format(rec.get("dam_id") or os.path.basename(folder),
                                              lon, lat, how))
    ddir = os.path.join(folder, DATA_DIR)
    os.makedirs(ddir, exist_ok=True)
    vbox = _lonlat_box(lon, lat, vector_half_mi)
    have = data_section(rec)

    def exists(role):
        return bool(have.get(role)) and bool(resolve(folder, have[role])) and not overwrite

    # dam values from the NID / DWR record
    hit = find_in_pack(reader, rec) if reader is not None else None
    if hit is None and rec.get("nid_id") and "nid_dams" in roles:
        s = catalog.chosen("nid_dams")
        if s and SV.data_capable(s, "nid_dams"):
            try:
                for f in SV.fetch_features(s, _lonlat_box(lon, lat, 1.0), http_get=http_get,
                                           cancel=cancel):
                    p = _props(f)
                    if _norm_id(p.get("nidid") or p.get("nid_id")) == _norm_id(rec.get("nid_id")):
                        xy = G.point_on_surface(f.get("geometry"))
                        hit = (xy[0], xy[1], p, s["name"]) if xy else None
                        break
            except Exception as e:                               # noqa
                say("NID record not read from {}: {}".format(s["name"], e))
    if hit is not None:
        vals = dam_values(hit[2])
        if rec.get("lon") is None and rec.get("lat") is None:
            vals.update({"lon": hit[0], "lat": hit[1]})
        out["values"] = vals
        out["refused"] = rec.set_many(vals, TOOL, version, note="from {}".format(hit[3])) or []
        say("dam values from {}: {}{}".format(hit[3], ", ".join(
            "{}={}".format(k, v) for k, v in sorted(vals.items()) if k not in ("lon", "lat")),
            "; kept the values already in dam.json for {}".format(", ".join(out["refused"]))
            if out["refused"] else ""))

    steps = len(roles) + 2
    n = 0

    def step(msg):
        nonlocal n
        n += 1
        prog(100.0 * n / steps, msg)
        if cancel and cancel():
            raise DataError("cancelled")

    def get_role(role):
        step(LABELS.get(role, role))
        if exists(role):
            out["skipped"][role] = "already there (overwrite to replace)"
            return
        try:
            if role == "dem":
                out["done"][role] = _get_dem(rec, ddir, lon, lat, data_folder, catalog, http_get,
                                             dem_half_mi, dem_cell_m, cancel, say)
            elif role == "nlcd":
                out["done"][role] = _get_nlcd(rec, ddir, lon, lat, data_folder, catalog, http_get,
                                              dem_half_mi, say)
            else:
                e = _get_vector(rec, role, ddir, vbox, reader, data_folder, catalog, http_get,
                                cancel, say)
                if e is None:
                    out["skipped"][role] = "no source (no pack data set, drop-in file or service)"
                else:
                    out["done"][role] = e
        except Exception as e:                                   # noqa
            out["failed"][role] = str(e)
            say("{}: FAILED - {}".format(LABELS.get(role, role), e))

    # the roles the suite package does not hold first, so the package (ND Dam Hazard's export,
    # which adds the dam's waterbodies / DWR layers from this data section) sees them
    pkg_roles = [r for r in roles if r in PACKAGE_LAYERS and not exists(r)]
    use_pkg = bool(pkg_roles) and make_package is not None and (reader is not None or bool(pack))
    for role in roles:
        if not (use_pkg and role in pkg_roles):
            get_role(role)
    # suite package (standard schema) for its roles
    if use_pkg:
        step("suite package")
        rec.save()                                   # the package reads the data section
        pkg = os.path.join(ddir, PACKAGE)
        try:
            rep = make_package(pkg, vbox) or {}
            counts = rep.get("counts") or {}
            for r in pkg_roles:
                lay = PACKAGE_LAYERS[r]
                if lay in counts:
                    out["done"][r] = set_entry(rec, r, "{}|layername={}".format(pkg, lay),
                                               "suite package (ND Dam Hazard rules) from the "
                                               "offline pack", counts.get(lay))
            say("suite package: {}".format(", ".join("{} {}".format(k, v)
                                                     for k, v in sorted(counts.items()))))
        except Exception as e:                                   # noqa
            say("suite package failed ({}); the raw data sets are used".format(e))
        for role in pkg_roles:
            if role not in out["done"]:
                get_role(role)
    # the area each file covers (lon / lat box), so a tool can tell whether it holds its area
    dbox = _lonlat_box(lon, lat, dem_half_mi)
    for role, e in out["done"].items():
        if isinstance(e, dict):
            e["box"] = [round(v, 6) for v in (dbox if role in ("dem", "nlcd") else vbox)]
    rec.save()
    prog(100.0, "done")
    say("data: {} written, {} failed, {} skipped".format(len(out["done"]), len(out["failed"]),
                                                          len(out["skipped"])))
    return out


def _get_dem(rec, ddir, lon, lat, data_folder, catalog, http_get, half_mi, cell, cancel, say):
    choice = catalog.chosen("dem")
    local = SD.local_dem(data_folder) if data_folder else ""
    if choice is not None and choice.get("kind") == "file":
        local = choice["url"]
    out = os.path.join(ddir, "dem.vrt")
    if local and (choice is None or choice.get("kind") == "file" or
                  catalog.choice.get("dem") in (None, "")):
        vrt, cov = raster_window(local, _lonlat_box(lon, lat, half_mi), out)
        note = "" if cov > 0.99 else "covers {:.0%} of the area".format(cov)
        say("DEM: window of {}{}".format(local, " ({})".format(note) if note else ""))
        return set_entry(rec, "dem", vrt, "your DEM " + local, note=note)
    if choice is None:
        raise DataError("no terrain source: put a DEM in the data folder's dem/ or choose a "
                        "terrain service")
    if not data_folder:
        raise DataError("set the suite data folder first (the terrain tiles are cached there)")
    cdir = TC.cache_dir(os.path.join(data_folder, SD.TERRAIN), choice["id"], cell)
    res = TC.get(_box_5070(lon, lat, half_mi), cell, cdir, TC.fetcher_for(choice, http_get), out,
                 cancel=cancel, log=say)
    return set_entry(rec, "dem", res["vrt"], "{} {} m (terrain cache: {} downloaded, {} cached)"
                     .format(choice["name"], cell, res["fetched"], res["cached"]))


def _get_nlcd(rec, ddir, lon, lat, data_folder, catalog, http_get, half_mi, say):
    local = SD.nlcd_raster(data_folder) if data_folder else ""
    choice = catalog.chosen("nlcd")
    if choice is not None and choice.get("kind") == "file":
        local = choice["url"]
    if local:
        vrt, cov = raster_window(local, _lonlat_box(lon, lat, half_mi),
                                 os.path.join(ddir, "nlcd.vrt"))
        return set_entry(rec, "nlcd", vrt, "land cover " + local,
                         note="" if cov > 0.99 else "covers {:.0%} of the area".format(cov))
    if choice is None:
        raise DataError("no land-cover source")
    box = TC.snap(_box_5070(lon, lat, half_mi), NLCD_CELL_M)
    size = (int(round((box[2] - box[0]) / NLCD_CELL_M)), int(round((box[3] - box[1]) / NLCD_CELL_M)))
    if choice["kind"] == "arcgis_image":
        url = SV.export_image_url(choice, box, size, pixel_type="U8")
    elif choice["kind"] == "wcs":
        url = SV.wcs_coverage_url(choice, box, size)
    else:
        raise DataError("{} cannot give land-cover values".format(choice["name"]))
    data = http_get(url)
    if data[:4] not in (b"II*\x00", b"MM\x00*"):
        raise DataError("{} did not send a GeoTIFF: {!r}".format(choice["name"], bytes(data[:80])))
    p = os.path.join(ddir, "nlcd.tif")
    with open(p, "wb") as f:
        f.write(data)
    say("land cover: {} ({} x {} cells)".format(choice["name"], size[0], size[1]))
    return set_entry(rec, "nlcd", p, choice["name"])


def _hydro():
    try:
        from . import hydro3dhp
    except ImportError:                                  # outside the package
        import hydro3dhp                                 # noqa
    return hydro3dhp


def _get_vector(rec, role, ddir, box, reader, data_folder, catalog, http_get, cancel, say):
    """One vector role from the pack, a drop-in file or a live service; None when no source."""
    path = os.path.join(ddir, "{}.gpkg".format(role))
    # 1. the offline pack
    if reader is not None:
        names = list(PACK_SETS.get(role, ()))
        if role == "lifelines":
            names = sorted(n for n in reader.datasets if n.startswith("lifeline_"))
        names = [n for n in names if reader.has(n)]
        names = _hydro().dedupe(names)           # 1.19.0: 3DHP_ and its old NHD_ name: once
        real = getattr(reader, "_real", None)    # the data set the pack really holds (alias)
        if real is not None:
            names = [real(n) for n in names]
        if names and all(reader.covers(box, n) for n in names):
            feats = []
            for n in names:
                for f in reader.query(n, box):
                    f = dict(f)
                    f["properties"] = dict(f.get("properties") or {}, dataset=n)
                    feats.append(f)
            info = reader.info(names[0]) or {}
            src = "offline pack {} (fetched {})".format("+".join(names), info.get("fetched", "?"))
            cnt = write_features(path, role, feats, src)
            say("{}: {} from {}".format(LABELS.get(role, role), cnt, src))
            return set_entry(rec, role, "{}|layername={}".format(path, role), src, cnt)
    # 2. a drop-in file (DWR export)
    if role in ("dwr_dams", "dwr_dam_lines") and data_folder:
        f = SD.dwr_layers(data_folder).get(role)
        if f:
            feats = read_vector_file(f, box)
            cnt = write_features(path, role, feats, f)
            say("{}: {} from {}".format(LABELS.get(role, role), cnt, f))
            return set_entry(rec, role, "{}|layername={}".format(path, role), "your file " + f, cnt)
    # 3. the chosen service
    got = fetch_role(role, box, catalog, http_get, cancel)
    if got is None:
        return None
    feats, src, short = got
    cnt = write_features(path, role, feats, src)
    say("{}: {} from {}".format(LABELS.get(role, role), cnt, short))
    return set_entry(rec, role, "{}|layername={}".format(path, role), src, cnt)


def fetch_role(role, box, catalog=None, http_get=None, cancel=None):
    """1.10.0: the features of a vector role in a lon / lat box from the server chosen for it in
    Suite data and servers (or the file chosen there): (features, source text, short name), or
    None when no server gives that role. Used by "Get data for this dam" and by Data source =
    Download."""
    catalog = catalog or SV.load()
    http_get = http_get or SV.urllib_get
    srole = SERVICE_ROLE.get(role, role)
    s = catalog.chosen(srole) if srole else None
    if s is not None and s.get("kind") == "file":
        return read_vector_file(s["url"], box), "your file " + s["url"], s["url"]
    if s is not None and SV.data_capable(s, srole):
        feats = SV.fetch_features(s, box, http_get=http_get, cancel=cancel)
        return feats, "{} ({})".format(s["name"], SV.layer_url(s)), s["name"]
    return None


def dam_folders(dams_folder):
    """The dam folders of a Dams folder (sub-folders holding a dam.json or legacy location)."""
    out = []
    if not dams_folder or not os.path.isdir(dams_folder):
        return out
    for n in sorted(os.listdir(dams_folder)):
        p = os.path.join(dams_folder, n)
        if os.path.isdir(p) and not n.startswith((".", "_")) and (
                os.path.isfile(os.path.join(p, DR.FILE_NAME)) or
                os.path.isfile(os.path.join(p, DR.LEGACY_LOCATION))):
            out.append(p)
    return out
