# -*- coding: utf-8 -*-
"""v2.9.0: find the data sets of the Data & Output tab groups 3 - 6
(structures; roads, rail and crossings; dams, lifelines and other layers;
channel) in a folder on the computer.

scan_folder(folder) walks the folder - bounded by max_depth, max_files and a
cancel callback - and opens every candidate file ONCE with OGR, read-only,
to list its layers: name, geometry kind, field names, the feature count only
when the driver has it cheaply, CRS and the file time. At most one feature
is read, and only for a layer whose declared geometry type is "unknown"
(KML, mixed GeoJSON). NOTHING is written into the scanned folder: the scan
results are cached in memory only, per file (path + size + modification
time), so a second scan re-opens only new or changed files.

Formats:
  GeoPackage (every layer), Shapefile, GeoJSON (.geojson; a .json only when
  its start looks like GeoJSON / Esri JSON), KML / KMZ, File geodatabase
  folders (*.gdb, when this GDAL's OpenFileGDB / FileGDB driver opens them),
  GeoParquet (.parquet, only when this GDAL has the Parquet driver - skipped
  silently otherwise), ZIP archives (their vector files through /vsizip/ -
  HIFLD layers are often downloaded zipped) and CSV tables with longitude /
  latitude (or x / y) columns.
  A CSV is NOT opened with OGR - its CSV driver makes no points without open
  options. The header and first row are read with the csv module (the same
  column aliases and delimiter rule as the review exchange) and the data set
  gets a QGIS "delimitedtext" URI (DataSet.provider == "delimitedtext");
  open_uri() / open_layer() pick the provider from the URI. x / y columns
  whose first row is not longitude / latitude have no known CRS: the data set
  says so and ranks lower.

Skipped: hidden folders, __pycache__, _downloads, _reprojected, tile_cache,
backup / old / archive folders (autodetect's list and any folder name with
one of those words), and inside an 'outputs' folder the plugin's own result
folders (Classification, EAP, QA, Consolidation, Submittal) - NOT
outputs/Data: the data sets the Data & Output tab saved there are valid
sources, recognised by their own names (datafetch.DATASETS, plus
Downloaded_Data.csv, which also tells whether a set was limited to the
flooded area - such a set ranks a little lower and says so). The plugin's
own result layers (processing.is_plugin_output_name, <dam>_POI, "inundated")
are never candidates. One unreadable file is skipped with a note; the scan
never raises for it.

Geometry (ROLE_GEOMETRY): NSI / address points; FEMA and other outlines,
campgrounds (structures.campground_records takes footprints), model boundary
and parcels polygons; roads, rail and centerline lines; AADT lines or points;
grade crossings and bridges points; dams and lifelines points or polygons.
Tables and empty layers are never candidates.

match_roles(datasets) gives every role its candidates, best first, each
with a score and a human reason, and assigns a layer to at most one role
(best score first); "lifelines" takes many layers, each with the plugin's
category. The rules are the plugin's own wherever it has one:
  the saved / downloaded data-set names (datafetch.DATASETS,
  build_nd_pack.datasets(), download._CACHE_RE, constants.LIFELINE_SOURCES,
  STRUCTURES_TAG), structures._NSI_FIELDS / _NENA_MARKERS / _FEMA_FIELDS,
  processing._GROUP_PATTERNS, osm_processing's FEMA / OSM names,
  classify_tab's _ROAD_RE / _RAIL_RE / _CROSS_RE / _PARCEL_RE /
  _CENTERLINE_RE and their exclusions, linear's AADT and rail fields and
  trains_per_day(), facilities.canonical_category() (lifeline categories,
  e.g. "Fire_Stations.shp" -> "Fire station"), OSM_CAMP_TAGS /
  OSM_LIFELINE_TAGS. Rules the plugin did not have (model boundary, other
  building outlines, bridges by name, NID / NBI field markers that are
  inline in facilities.py) are marked as such below.

Scores rank candidates for picking a layer; they are not engineering
parameters, and nothing here changes a hazard classification: the picks are
inputs the engineer sees (describe()) and can change.
"""

import copy
import csv
import functools
import json
import os
import re
import threading
import time
import zipfile
from collections import deque
from urllib.parse import quote

from osgeo import gdal, ogr
from qgis.core import QgsVectorLayer

from . import constants as C
from .constants import (
    OUTPUT_FOLDER_NAME, OUTPUT_SUBDIRS, DOWNLOAD_FOLDER_NAME, STRUCTURES_TAG,
    LEGACY_STRUCTURES_TAG, OSM_CAMP_TAGS, OSM_LIFELINE_TAGS,
)
from .helpers import gdal_exceptions, CancelledByUser
from .processing import (
    _GROUP_PATTERNS, _layer_name_key, is_plugin_output_name, is_downloaded_data_layer,
    DATA_INDEX_FILE,
)
from .structures import _NSI_FIELDS, _NENA_MARKERS, _FEMA_FIELDS
from .osm_processing import _is_fema_structures_name, _is_osm_name
from .facilities import canonical_category, SCHOOLS
from .linear import (
    _AADT_NAMES, _AADT_YEAR_NAMES, _RE_YEAR_COL, _NAME_NAMES, _OWNER_NAMES, _PASS_NAMES,
    trains_per_day,
)
from .autodetect import _SKIP_DIRS as _AUTODETECT_SKIP_DIRS
from .review_exchange import _ALIASES as _XY_ALIASES, _delimiter
from .datafetch import DATASETS
from .download import _CACHE_RE

# ------------------------------------------------------------------ roles --
ROLES = ("nsi", "address", "usa", "outlines", "camps", "roads", "aadt", "rail", "crossings",
         "bridges", "dams", "lifelines", "boundary", "parcels", "centerline", "parks", "places")
MULTI_ROLES = ("lifelines",)
ROLE_LABELS = {
    "nsi": "NSI structure points", "address": "ND GIS Hub site / structure address points",
    "usa": "FEMA USA Structures outlines", "outlines": "other building outlines",
    "camps": "campgrounds / RV parks", "roads": "road lines", "aadt": "AADT (traffic) layer",
    "rail": "rail lines", "crossings": "rail grade crossings",
    "bridges": "bridges / culverts / low-water crossings", "dams": "dams (NID)",
    "lifelines": "lifeline facilities", "boundary": "model boundary polygon",
    "parcels": "parcels", "centerline": "channel centerline / 3DHP flowlines",
    "parks": "mobile-home / trailer parks", "places": "cities / towns / villages (travel-time table)"}
ROLE_GROUP = {"nsi": 3, "address": 3, "usa": 3, "outlines": 3, "camps": 3, "roads": 4,
              "aadt": 4, "rail": 4, "crossings": 4, "bridges": 4, "dams": 5, "lifelines": 5,
              "boundary": 5, "parcels": 5, "centerline": 6, "parks": 3, "places": 6}

POINT, LINE, POLYGON, NONE, UNKNOWN = "point", "line", "polygon", "none", "unknown"
ROLE_GEOMETRY = {
    "nsi": (POINT,), "address": (POINT,), "usa": (POLYGON,), "outlines": (POLYGON,),
    "camps": (POLYGON,), "roads": (LINE,), "aadt": (LINE, POINT), "rail": (LINE,),
    "crossings": (POINT,), "bridges": (POINT,), "dams": (POINT, POLYGON),
    "lifelines": (POINT, POLYGON), "boundary": (POLYGON,), "parcels": (POLYGON,),
    "centerline": (LINE,), "parks": (POLYGON,), "places": (POINT,)}
# camps: polygons only - structures.campground_records() takes every feature as a
# footprint (the OSM download already turns campground points into circles)

# ranking of the candidates (0 - 100); not engineering parameters
S_PLUGIN = 95            # the plugin's own saved / downloaded data-set name
S_FIELDS = 80            # the data set's own field signature (NSI, NENA, FEMA, NBI, NID)
S_NAME_STRONG = 70       # an unambiguous name (lifeline category, parcels)
S_NAME = 55              # the name rule the plugin uses for project layers
S_WEAK = 35              # a generic word only
S_SUPPORT = 8            # each further piece of evidence
MIN_SCORE = 30           # below this a layer is not a candidate
FLOOD_ONLY_PENALTY = 10  # saved by the Data & Output tab for the flooded area only
NO_CRS_PENALTY = 15      # CSV x / y columns without a known coordinate system

# ------------------------------------------------------------------- scan --
DEFAULT_MAX_DEPTH = 4
DEFAULT_MAX_FILES = 1000
MAX_WALK_ENTRIES = 200000        # directory entries looked at (bounds a scan of a whole drive)
ZIP_MAX_MEMBERS = 100            # vector files opened per ZIP archive
CSV_COUNT_MAX_BYTES = 5 * 1024 * 1024   # rows are counted only in smaller CSV files
JSON_SNIFF_BYTES = 65536

VECTOR_EXT = (".gpkg", ".shp", ".geojson", ".json", ".kml", ".kmz", ".csv", ".parquet")
ZIP_MEMBER_EXT = (".gpkg", ".shp", ".geojson", ".kml", ".parquet")
_CONTAINER_DRIVERS = ("GPKG", "OpenFileGDB", "FileGDB", "KML", "LIBKML", "SQLite")
# autodetect's list (the hydraulic-raster search) without 'outputs': outputs/Data
# and the FEMA_Structures file in outputs are data sets
_SKIP_DIR_NAMES = (({d.lower() for d in _AUTODETECT_SKIP_DIRS} - {OUTPUT_FOLDER_NAME.lower()})
                   | {DOWNLOAD_FOLDER_NAME.lower(), "_reprojected", "__pycache__", "tile_cache",
                      "__macosx", "system volume information"})
_SKIP_DIR_WORDS = ("backup", "backups", "old", "archive", "archives", "archived")
_SKIP_DIR_PREFIXES = ("ACER_", "EAP_")          # autodetect: old result folders
_JSON_SNIFF = re.compile(r'"type"\s*:\s*"(?:FeatureCollection|Feature)"|"geometryType"\s*:')
_FILE_ATTRIBUTE_HIDDEN = 0x2
_FILE_ATTRIBUTE_SYSTEM = 0x4
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400    # Windows junctions: never followed (loops)

_CACHE = {}                      # (source, signature) -> (tuple of DataSet, tuple of notes)
_CACHE_LOCK = threading.Lock()
_CACHE_MAX = 5000


class DataSet(object):
    """One vector layer found by scan_folder().

    uri       what QgsVectorLayer(uri, name, provider) opens ("path|layername=x"
              for GeoPackage / FileGDB / KML layers, "/vsizip/..." for a ZIP
              member, a "file:///...?type=csv&xField=..." URI for a CSV)
    provider  "ogr" or "delimitedtext"
    path      the file (or .gdb folder) on disk; for a ZIP member the .zip
    source    what OGR opened (path, or /vsizip/<zip>/<member>)
    member    the file inside the ZIP ("" otherwise)
    layer     layer name; format: OGR driver ("GPKG", "ESRI Shapefile" ...)
    geometry  "point" / "line" / "polygon" / "none" / "unknown"
    fields, fields_lower   field names; count: features (None when not cheap)
    crs       "EPSG:xxxx" (or a CRS name, "" when unknown); mtime, size: file
    layer_count            layers in the same source
    plugin_data            True in the plugin's own outputs/Data folder
    data_label, data_area  its Downloaded_Data.csv row ("Data set", "Area")
    hints     names used for matching (layer name, file / archive stem)
    notes     remarks (CSV without CRS, geometry from the first feature ...)
    """

    def __init__(self, **kw):
        self.uri = kw.get("uri", "")
        self.provider = kw.get("provider", "ogr")
        self.path = kw.get("path", "")
        self.source = kw.get("source", self.path)
        self.member = kw.get("member", "")
        self.layer = kw.get("layer", "")
        self.format = kw.get("format", "")
        self.geometry = kw.get("geometry", UNKNOWN)
        self.fields = list(kw.get("fields") or [])
        self.fields_lower = [f.lower() for f in self.fields]
        self.count = kw.get("count")
        self.crs = kw.get("crs", "")
        self.mtime = kw.get("mtime", 0.0)
        self.size = kw.get("size", 0)
        self.layer_count = kw.get("layer_count", 1)
        self.plugin_data = False
        self.data_label = ""
        self.data_area = ""
        self.hints = list(kw.get("hints") or [self.layer])
        self.notes = list(kw.get("notes") or [])

    @property
    def name(self):
        return self.layer or os.path.splitext(os.path.basename(self.source))[0]

    def as_dict(self):
        return {k: getattr(self, k) for k in (
            "uri", "provider", "path", "source", "member", "layer", "format", "geometry",
            "fields", "fields_lower", "count", "crs", "mtime", "size", "layer_count",
            "plugin_data", "data_label", "data_area", "hints", "notes")}

    def __repr__(self):
        return "DataSet({!r}, {}, {} field(s))".format(self.uri, self.geometry, len(self.fields))


class ScanResult(list):
    """The DataSets of a scan (a list) plus what the scan met: notes (files
    skipped and why), files_seen / files_read, truncated (max_files or the
    walk limit reached), cancelled, skipped_parquet (no Parquet driver)."""

    def __init__(self, folder=""):
        list.__init__(self)
        self.folder = folder
        self.notes = []
        self.files_seen = 0
        self.files_read = 0
        self.truncated = False
        self.cancelled = False
        self.skipped_parquet = 0
        self.elapsed_s = 0.0


def clear_cache():
    """Forget the in-memory scan cache (e.g. after the user asks to re-scan)."""
    with _CACHE_LOCK:
        _CACHE.clear()


# ----------------------------------------------------------- small helpers --
def _stem(path):
    return os.path.splitext(os.path.basename(path.rstrip("/\\")))[0]


def _short(e):
    t = str(e).strip().splitlines()[0] if str(e).strip() else type(e).__name__
    return t[:200]


def _last_gdal_error():
    for mod in (gdal, ogr):
        try:
            msg = mod.GetLastErrorMsg()
            if msg:
                return msg
        except Exception:
            continue
    return ""


def _has_driver(name):
    try:
        return ogr.GetDriverByName(name) is not None
    except Exception:
        return False


def geometry_kind(code):
    """"point" / "line" / "polygon" / "none" / "unknown" of an OGR geometry
    type code (2.5D, Z / M / ZM and curve types included)."""
    try:
        t = int(code)
    except (TypeError, ValueError):
        return UNKNOWN
    if t == 100:                         # wkbNone
        return NONE
    t = (t & 0x7fffffff) % 1000          # drop the 2.5D flag and ISO Z / M / ZM
    return {1: POINT, 4: POINT, 2: LINE, 5: LINE, 8: LINE, 9: LINE, 11: LINE, 13: LINE,
            3: POLYGON, 6: POLYGON, 10: POLYGON, 12: POLYGON, 14: POLYGON, 15: POLYGON,
            16: POLYGON, 17: POLYGON}.get(t, UNKNOWN)


def _crs_of(srs):
    if srs is None:
        return ""
    try:
        srs.AutoIdentifyEPSG()
    except Exception:
        pass
    try:
        auth, code = srs.GetAuthorityName(None), srs.GetAuthorityCode(None)
        if auth and code:
            return "{}:{}".format(auth, code)
    except Exception:
        pass
    try:
        return srs.GetName() or ""
    except Exception:
        return ""


def _file_url(path):
    """file:/// URL of a local path (Windows drive letters and UNC paths)."""
    return _url_of_abs(os.path.abspath(path))


def _url_of_abs(path):
    p = path.replace("\\", "/")
    if p.startswith("//"):
        return "file:" + quote(p, safe="/:")
    if not p.startswith("/"):
        p = "/" + p
    return "file://" + quote(p, safe="/:")


def _uri_path(path):
    return path.replace("\\", "/")


def _skip_dir(entry, in_outputs):
    name = entry.name
    low = name.lower()
    if low.startswith((".", "$")) or low in _SKIP_DIR_NAMES:
        return True
    if name.upper().startswith(_SKIP_DIR_PREFIXES):
        return True
    if any(w in _SKIP_DIR_WORDS for w in re.split(r"[^a-z0-9]+", low)):
        return True
    if in_outputs and low in _result_dirs():
        return True
    try:
        if getattr(entry, "is_junction", lambda: False)():
            return True
        attrs = getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0) or 0
        if attrs & (_FILE_ATTRIBUTE_HIDDEN | _FILE_ATTRIBUTE_SYSTEM
                    | _FILE_ATTRIBUTE_REPARSE_POINT):
            return True
    except OSError:
        return True
    return False


def _result_dirs():
    """The plugin's result folders inside outputs (never data sets)."""
    names = {d.lower() for d in OUTPUT_SUBDIRS}
    try:
        from .submittal import SUBMITTAL_DIR
        names.add(SUBMITTAL_DIR.lower())
    except Exception:                  # pragma: no cover - submittal.py always ships
        names.add("submittal")
    return names


def _looks_like_geojson(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            head = fh.read(JSON_SNIFF_BYTES)
    except OSError:
        return False
    return bool(_JSON_SNIFF.search(head))


# ------------------------------------------------------------------- walk --
def _walk(root, max_depth, max_files, include_zip, stopped, report):
    """Candidate files [(kind, path, stat)] breadth first (shallow files
    first), plus (truncated, cancelled, notes)."""
    out, notes = [], []
    todo = deque([(root, 0)])
    seen = 0
    truncated = cancelled = False
    while todo:
        folder, depth = todo.popleft()
        try:
            with os.scandir(folder) as it:
                entries = sorted(it, key=lambda e: e.name.lower())
        except OSError as e:
            notes.append("folder not readable: {} ({})".format(folder, e.strerror or _short(e)))
            continue
        in_outputs = os.path.basename(folder.rstrip("/\\")).lower() == OUTPUT_FOLDER_NAME.lower()
        for e in entries:
            seen += 1
            if seen % 500 == 0:
                if stopped():
                    return out, truncated, True, notes
                report(min(9.0, 1.0 + seen / 20000.0),
                       "Looking for data sets: {} file(s) found".format(len(out)))
            if seen > MAX_WALK_ENTRIES:
                truncated = True
                notes.append("stopped looking after {:,} folder entries - choose a smaller "
                              "folder".format(MAX_WALK_ENTRIES))
                return out, truncated, cancelled, notes
            try:
                is_dir = e.is_dir(follow_symlinks=False)
            except OSError:
                continue
            kind = None
            if is_dir:
                if e.name.lower().endswith(".gdb"):
                    kind = "gdb"               # a File geodatabase: one data source
                elif not _skip_dir(e, in_outputs) and depth < max_depth:
                    todo.append((e.path, depth + 1))
                    continue
                else:
                    continue
            else:
                ext = os.path.splitext(e.name)[1].lower()
                if ext in VECTOR_EXT:
                    kind = ext[1:]
                elif ext == ".zip" and include_zip:
                    kind = "zip"
                else:
                    continue
            if len(out) >= max_files:
                notes.append("stopped after {} data file(s) (the limit) - choose a smaller "
                             "folder or raise the limit".format(max_files))
                return out, True, cancelled, notes
            try:
                st = e.stat(follow_symlinks=False)
            except OSError as err:
                notes.append("skipped {}: {}".format(e.path, err.strerror or _short(err)))
                continue
            out.append((kind, e.path, st))
    return out, truncated, cancelled, notes


def _signature(kind, path, st):
    if kind != "gdb":
        return (st.st_size, getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9)))
    n = size = newest = 0
    try:
        with os.scandir(path) as it:
            for e in it:
                try:
                    s = e.stat()
                except OSError:
                    continue
                n += 1
                size += s.st_size
                newest = max(newest, getattr(s, "st_mtime_ns", int(s.st_mtime * 1e9)))
    except OSError:
        pass
    return (n, size, newest)


# ------------------------------------------------------------------- read --
def _dataset_from_layer(lyr, source, path, driver, n_layers, st, member, hints):
    name = lyr.GetName()
    notes = []
    geom = geometry_kind(lyr.GetGeomType())
    if geom == UNKNOWN:
        # KML / mixed GeoJSON declare no type: the first feature tells
        try:
            lyr.ResetReading()
            f = lyr.GetNextFeature()
            g = f.GetGeometryRef() if f is not None else None
            if g is not None:
                geom = geometry_kind(g.GetGeometryType())
                if geom != UNKNOWN:
                    notes.append("geometry type from the first feature")
            lyr.ResetReading()
        except Exception:
            pass
    defn = lyr.GetLayerDefn()
    fields = [defn.GetFieldDefn(i).GetName() for i in range(defn.GetFieldCount())]
    try:
        count = lyr.GetFeatureCount(0)        # force=0: only when the driver knows it
        count = int(count) if count is not None and int(count) >= 0 else None
    except Exception:
        count = None
    try:
        crs = _crs_of(lyr.GetSpatialRef())
    except Exception:
        crs = ""
    uri = _uri_path(source)
    if driver in _CONTAINER_DRIVERS or n_layers > 1:
        uri = "{}|layername={}".format(uri, name)
    h = [name] + ([x for x in hints if x] if n_layers == 1 else [])
    return DataSet(uri=uri, provider="ogr", path=path, source=source, member=member,
                   layer=name, format=driver, geometry=geom, fields=fields, count=count,
                   crs=crs, mtime=st.st_mtime, size=st.st_size, layer_count=n_layers,
                   hints=list(dict.fromkeys(h)), notes=notes)


def _read_ogr(source, path, st, member="", hints=()):
    """DataSets of every layer OGR finds in `source` (opened once), notes."""
    out, notes = [], []
    shown = member and "{} ({})".format(path, member) or path
    try:
        with gdal_exceptions():
            ds = ogr.Open(source, 0)
            if ds is None:
                raise RuntimeError(_last_gdal_error() or "not recognised by OGR")
            try:
                driver = ds.GetDriver().GetName()
            except Exception:
                driver = ""
            n = ds.GetLayerCount()
            if not n:
                notes.append("skipped {}: no layers".format(shown))
            for i in range(n):
                lyr = ds.GetLayer(i)
                if lyr is None:
                    continue
                try:
                    out.append(_dataset_from_layer(lyr, source, path, driver, n, st, member,
                                                   [_stem(source)] + list(hints)))
                except Exception as e:
                    notes.append("skipped {} layer {}: {}".format(shown, i, _short(e)))
            ds = None
    except CancelledByUser:
        raise
    except Exception as e:          # one bad file never stops the scan
        notes.append("skipped {}: could not be opened ({})".format(shown, _short(e)))
    return out, notes


def _read_csv(path, st):
    """A point DataSet for a CSV with lon / lat (or x / y) columns; a CSV
    without them is a table, not a data set (skipped silently)."""
    try:
        with open(path, "r", encoding="utf-8-sig", errors="replace", newline="") as fh:
            first = fh.readline()
            second = fh.readline()
    except OSError as e:
        return [], ["skipped {}: {}".format(path, e.strerror or _short(e))]
    if not first.strip():
        return [], []
    delim = _delimiter(first)
    try:
        header = next(csv.reader([first], delimiter=delim))
        row = next(csv.reader([second], delimiter=delim), []) if second.strip() else []
    except (csv.Error, StopIteration):
        return [], []
    tags = [_XY_ALIASES.get(re.sub(r"[^a-z0-9]", "", h.lower()), "") for h in header]

    def column(tag):
        return tags.index(tag) if tag in tags else None
    notes = []
    ix, iy, crs = column("Lon"), column("Lat"), "EPSG:4326"
    if ix is None or iy is None:
        ix, iy, crs = column("X"), column("Y"), ""
        if ix is None or iy is None:
            return [], []
        try:
            x, y = float(row[ix]), float(row[iy])
            if -180.0 <= x <= 180.0 and -90.0 <= y <= 90.0:
                crs = "EPSG:4326"
                notes.append("x / y read as longitude / latitude (first row)")
        except (IndexError, ValueError):
            pass
        if not crs:
            notes.append("x / y columns without a known coordinate system - set the CRS")
    count = None
    if st.st_size <= CSV_COUNT_MAX_BYTES:
        try:
            with open(path, "rb") as fh:
                count = max(0, sum(1 for line in fh if line.strip()) - 1)
        except OSError:
            count = None
    xname, yname = header[ix].strip(), header[iy].strip()
    query = [("type", "csv"), ("delimiter", delim), ("xField", xname), ("yField", yname),
             ("detectTypes", "yes"), ("geomType", "point"), ("spatialIndex", "no"),
             ("subsetIndex", "no"), ("watchFile", "no")]
    if crs:
        query.append(("crs", crs))
    uri = _file_url(path) + "?" + "&".join("{}={}".format(k, quote(v, safe=":"))
                                           for k, v in query)
    ds = DataSet(uri=uri, provider="delimitedtext", path=path, source=path, layer=_stem(path),
                 format="CSV", geometry=POINT, fields=[h.strip() for h in header], count=count,
                 crs=crs, mtime=st.st_mtime, size=st.st_size, layer_count=1,
                 hints=[_stem(path)], notes=notes)
    return [ds], []


def _read_zip(path, st, parquet_ok, stopped):
    try:
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()
    except (OSError, zipfile.BadZipFile, RuntimeError, ValueError) as e:
        return [], ["skipped {}: not a readable ZIP archive ({})".format(path, _short(e))], True
    gdbs, files = [], []
    for n in names:
        parts = [p for p in n.replace("\\", "/").split("/") if p]
        if not parts or parts[-1].startswith(".") or any(
                p.startswith(".") or p.lower() in _SKIP_DIR_NAMES for p in parts[:-1]):
            continue
        gdb = next((i for i, p in enumerate(parts[:-1]) if p.lower().endswith(".gdb")), None)
        if gdb is not None:
            key = "/".join(parts[:gdb + 1])
            if key not in gdbs:
                gdbs.append(key)
            continue
        ext = os.path.splitext(parts[-1])[1].lower()
        if ext in ZIP_MEMBER_EXT and (ext != ".parquet" or parquet_ok):
            files.append("/".join(parts))
    members = gdbs + sorted(files)
    notes = []
    if len(members) > ZIP_MAX_MEMBERS:
        notes.append("{}: only the first {} of {} vector files read".format(
            path, ZIP_MAX_MEMBERS, len(members)))
        members = members[:ZIP_MAX_MEMBERS]
    out = []
    zroot = "/vsizip/" + _uri_path(os.path.abspath(path))
    hints = [_stem(path)] if len(members) == 1 else []
    for m in members:
        if stopped():
            return out, notes, False          # partial: never cached
        sets, n2 = _read_ogr("{}/{}".format(zroot, m), path, st, member=m, hints=hints)
        out.extend(sets)
        notes.extend(n2)
    return out, notes, True


def _read_candidate(kind, path, st, parquet_ok, stopped):
    source = os.path.abspath(path)
    key = (os.path.normcase(source), kind, parquet_ok, _signature(kind, path, st))
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
    if hit is not None:
        return [_copy(ds) for ds in hit[0]], list(hit[1]), True       # (sets, notes, complete)
    complete = True
    if kind == "zip":
        sets, notes, complete = _read_zip(path, st, parquet_ok, stopped)
    elif kind == "csv":
        sets, notes = _read_csv(path, st)
    else:
        sets, notes = _read_ogr(path, path, st)
    if not complete:
        return sets, notes, False
    with _CACHE_LOCK:
        if len(_CACHE) >= _CACHE_MAX:
            _CACHE.clear()
        _CACHE[key] = (tuple(_copy(ds) for ds in sets), tuple(notes))
    return sets, notes, True


def _copy(ds):
    c = copy.copy(ds)
    for k in ("fields", "fields_lower", "hints", "notes"):
        setattr(c, k, list(getattr(ds, k)))
    return c


class _PathLayer(object):
    """Just enough of a layer for processing.is_downloaded_data_layer()."""

    def __init__(self, path):
        self._p = path

    def source(self):
        return self._p


def _annotate_plugin_data(sets):
    """Mark the layers of the plugin's own outputs/Data folder and attach
    their Downloaded_Data.csv row (data-set label and area)."""
    index = {}
    for ds in sets:
        ds.plugin_data, ds.data_label, ds.data_area = False, "", ""
        if ds.member or not is_downloaded_data_layer(_PathLayer(ds.path)):
            continue
        ds.plugin_data = True
        folder = os.path.dirname(ds.path)
        if folder not in index:
            index[folder] = {}
            p = os.path.join(folder, DATA_INDEX_FILE)
            try:
                with open(p, newline="", encoding="utf-8-sig") as fh:
                    for row in csv.DictReader(fh):
                        f = (row.get("File") or "").strip()
                        if f:
                            index[folder][f.lower()] = ((row.get("Data set") or "").strip(),
                                                        (row.get("Area") or "").strip())
            except (OSError, csv.Error):
                pass
        label, area = index[folder].get(os.path.basename(ds.path).lower(), ("", ""))
        ds.data_label, ds.data_area = label, area


def scan_folder(folder, max_depth=DEFAULT_MAX_DEPTH, progress=None, cancel=None,
                max_files=DEFAULT_MAX_FILES, include_zip=True):
    """Every vector layer in `folder` (and its sub-folders up to max_depth
    levels) as a ScanResult (a list of DataSet with .notes, .truncated,
    .cancelled ...). progress(pct, message) is called along the way; the
    scan stops (keeping what it found) when cancel() returns True or
    progress raises CancelledByUser. Never raises for a bad file."""
    t0 = time.time()
    res = ScanResult(folder or "")

    def stopped():
        try:
            return bool(cancel and cancel())
        except Exception:
            return False

    def report(pct, msg):
        if progress is None:
            return
        try:
            progress(pct, msg)
        except CancelledByUser:
            raise
        except Exception:
            pass

    if not folder or not os.path.isdir(folder):
        res.notes.append("not a folder: {}".format(folder))
        return res
    root = os.path.abspath(folder)
    res.folder = root
    try:
        report(0.0, "Looking for data sets in {}".format(root))
        cands, res.truncated, res.cancelled, notes = _walk(
            root, max(0, int(max_depth)), max(1, int(max_files)), include_zip, stopped, report)
        res.notes.extend(notes)
        res.files_seen = len(cands)
        parquet_ok = _has_driver("Parquet")
        n = max(1, len(cands))
        for i, (kind, path, st) in enumerate(cands):
            if res.cancelled or stopped():
                res.cancelled = True
                break
            if kind == "parquet" and not parquet_ok:
                res.skipped_parquet += 1        # no Parquet driver in this GDAL
                continue
            if kind == "json" and not _looks_like_geojson(path):
                continue                        # settings, manifests ...: not data sets
            report(10.0 + 90.0 * i / n, "Reading {} of {}: {}".format(
                i + 1, len(cands), os.path.basename(path)))
            sets, notes, complete = _read_candidate(kind, path, st, parquet_ok, stopped)
            res.extend(sets)
            res.notes.extend(notes)
            res.files_read += 1
            if not complete:                    # cancelled inside a ZIP archive
                res.cancelled = True
                break
    except CancelledByUser:
        res.cancelled = True
    if res.cancelled:
        res.notes.append("scan cancelled - {} layer(s) found so far".format(len(res)))
    _annotate_plugin_data(res)
    res.elapsed_s = time.time() - t0
    try:
        report(100.0, "{} layer(s) in {} file(s)".format(len(res), res.files_read))
    except CancelledByUser:
        pass
    return res


# --------------------------------------------------------------- matching --
_TRAFFIC_WORDS = ("aadt", "traffic", "hpms")     # the traffic words of classify_tab._ROAD_RE
# new rules (the plugin had none): bridges by name, dams by name, building
# outlines, model boundary, generic lifeline layers; field markers that are
# inline in facilities.read_dams / read_crossings
_BRIDGE_NAME_RE = re.compile(r"(?<![a-z])(?:bridges?|culverts?|fords?|nbi)(?![a-z])|low ?water")
_NBI_MARKER = "structure_number_008"             # facilities.read_crossings: NBI record
_OSM_CROSSING_FIELDS = ("waterway", "railway")   # facilities.read_crossings: OSM crossing
_NID_ID_FIELDS = ("nidid",)                      # facilities.read_dams (compact form)
_DAM_DETAIL_FIELDS = ("hazardpotential", "nidheight", "nidstorage")
_DWR_DAM_FIELDS = ("structureid", "damname", "maxdamheight")   # compact form
_DWR_NOT_DAMS_RE = re.compile(r"(?<![a-z])(?:unknown|dugouts?)(?![a-z])")
_DAM_NAME_RE = re.compile(r"(?<![a-z])(?:nid|dams|national inventory of dams|"
                          r"dam (?:points?|locations?|inventory))(?![a-z])")
_PARK_NAME_RE = re.compile(
    r"(?<![a-z])(?:mobile ?home ?(?:parks?|courts?|communit(?:y|ies))|trailer ?(?:parks?|courts?)|"
    r"manufactured ?(?:home|housing) ?(?:parks?|communit(?:y|ies))|mhps?)(?![a-z])")
_PLACES_NAME_RE = re.compile(r"(?<![a-z])(?:osm ?places?|populated ?places?|cities|towns|villages)(?![a-z])")
_OUTLINE_RE = re.compile(r"(?<![a-z])(?:buildings?|footprints?|outlines?|bldgs?)(?![a-z])")
_BOUNDARY_STRONG_RE = re.compile(
    r"2d ?flow ?areas?|(?<![a-z])flow ?areas?(?![a-z])|model ?(?:boundary|boundaries|extent|"
    r"domain|limits?|perimeter)|mesh ?(?:boundary|perimeter)|computational ?domain")
_BOUNDARY_WEAK_RE = re.compile(r"(?<![a-z])(?:boundary|boundaries|perimeter|domain)(?![a-z])")
_BOUNDARY_ADMIN_RE = re.compile(
    r"county|city|state|township|district|parcel|zip|census|tribal|reservation|municipal|"
    r"corporate|precinct|ward|school|watershed|huc|basin|fire")
_LIFELINE_GENERIC_RE = re.compile(r"lifelines?|critical ?facilit|essential ?facilit|"
                                  r"critical ?infrastructure")
_LIFELINE_EXCLUDE_RE = re.compile(
    r"district|boundar|jurisdiction|coverage|territor|precinct|hydrant|manhole|valve|"
    r"risk|hazard|incident|danger|perimeter|response|history|"
    r"(?<![a-z])(?:zones?|areas?|pipes?|mains?|lines?|beats?|burn(?:ed)?)(?![a-z])")
# data-set keys of datafetch.DATASETS -> role (None: not a group 3-6 input)
_DATASET_ROLES = {"nsi": "nsi", "address": "address", "usa": "usa", "camps": "camps",
                  "roads": "roads", "traffic": "aadt", "rail": "rail", "xing": "crossings",
                  "bridges": "bridges", "dams": "dams", "lifelines": "lifelines",
                  "flowlines": "centerline", "places": None}
# groups of build_nd_pack.datasets() -> role ("rail" holds lines and crossings)
_PACK_GROUP_ROLES = {"nsi": ("nsi",), "address": ("address",), "usa": ("usa",),
                     "nid": ("dams",), "roads": ("roads",), "traffic": ("aadt",),
                     "rail": ("rail", "crossings"), "lifelines": ("lifelines",),
                     "bridges": ("bridges",), "nhd": ("centerline",),
                     "hydro": ("centerline",)}


def _tab_rules():
    """classify_tab's layer-name rules (imported late: a UI module)."""
    from . import classify_tab as T
    return T


def _singular(key):
    def one(w):
        if len(w) > 4 and w.endswith("ies"):
            return w[:-3] + "y"
        if len(w) > 3 and w.endswith("s") and not w.endswith(("ss", "us", "is")):
            return w[:-1]
        return w
    return " ".join(one(w) for w in key.split())


def _keyword_re(words):
    alts = sorted({re.escape(w).replace(r"\ ", " ").replace(" ", " ?") for w in words},
                  key=len, reverse=True)
    return re.compile(r"(?<![a-z])(?:{})(?![a-z])".format("|".join(alts)))


@functools.lru_cache(maxsize=1)
def _school_rules():
    """[(regex, category)] for the plugin's school / child-care categories
    (facilities.SCHOOLS, OSM_LIFELINE_TAGS)."""
    words = {}
    for tag in OSM_LIFELINE_TAGS:
        cat = tag[-1]
        if cat in SCHOOLS:
            words.setdefault(cat, set()).add(tag[1].replace("_", " "))
    for cat in SCHOOLS:
        words.setdefault(cat, set()).add(cat.lower())
    return tuple((_keyword_re(w), cat) for cat, w in sorted(words.items()))


@functools.lru_cache(maxsize=1)
def _camp_re():
    words = set()
    for tag in OSM_CAMP_TAGS:
        words.add(tag[1].replace("_", " "))
        words.add(tag[-1].lower())
    return _keyword_re(words)


def lifeline_category(text):
    """The plugin's facility category for a lifeline layer / file name
    ("Fire_Stations" -> "Fire station", "Emergency_Operations_Centers" ->
    "Emergency operations center", "Public_Schools" -> "School"), or None.
    facilities.canonical_category() decides, on the name and on its
    singular form; districts, zones, hydrants and the like are not
    facilities."""
    key = re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()
    if not key or _LIFELINE_EXCLUDE_RE.search(key):
        return None
    sing = _singular(key)
    for k in (key, sing):
        cat = canonical_category(k)
        if cat:
            return cat
    for rx, cat in _school_rules():
        if rx.search(sing):
            return cat
    return None


def _plugin_names():
    """[(name key, roles, category, reason)] of the plugin's own data-set
    names: the Data & Output tab's saved files, the download / offline-pack
    names and the building-outline file."""
    out = []
    for d in DATASETS:
        role = _DATASET_ROLES.get(d[0])
        if role:
            out.append((_layer_name_key(d[1]), (role,), None,
                        "plugin data set name '{}'".format(d[1])))
    for tag in (STRUCTURES_TAG, LEGACY_STRUCTURES_TAG):
        out.append((_layer_name_key(tag), ("usa",), None,
                    "plugin building-outline file name '{}'".format(tag)))
    lifeline_cats = {"lifeline_" + t[0]: t[-1] for t in C.LIFELINE_SOURCES}
    try:
        from . import build_nd_pack as B
        from . import osm_parse as O
        osm_roles = {O.overpass_to_features: ("lifelines",), O.crossing_features: ("bridges",),
                     O.camp_features: ("camps",), O.trailer_park_features: ("parks",),
                     O.place_features: ("places",)}
        for entry in B.datasets():
            name, group, spec = entry[0], entry[2], entry[-1] if isinstance(entry[-1], dict) \
                else {}
            roles = _PACK_GROUP_ROLES.get(group)
            if group == "osm":
                roles = osm_roles.get(spec.get("parse"))
            if not roles:
                continue
            names = [name] + [c[0] for c in spec.get("choices") or []]
            for n in names:
                out.append((_layer_name_key(n), roles, lifeline_cats.get(n),
                            "plugin download name '{}'".format(n)))
    except Exception:                    # the pack builder is optional here
        pass
    for key, cat in lifeline_cats.items():
        out.append((_layer_name_key(key), ("lifelines",), cat,
                    "plugin download name '{}'".format(key)))
    return out


def _nhd_fields():
    """Flowline fields (the pack builder's field list: 3DHP since 1.19.0, NHD before), lower case."""
    old = ("gnis_name", "gnis_id", "ftype", "fcode", "reachcode", "lengthkm")
    try:
        from . import build_nd_pack as B
        for entry in B.datasets():
            spec = entry[-1] if isinstance(entry[-1], dict) else {}
            if entry[0] == "3DHP_flowlines" and spec.get("fields"):
                new = tuple(f.strip().lower() for f in spec["fields"].split(",") if f.strip())
                return old + tuple(f for f in new if f not in old)
    except Exception:
        pass
    return old


class Candidate(object):
    """A data set proposed for a role: score (0-100), reasons (text) and,
    for lifelines, the plugin category (None: per feature from its category
    / type field, as facilities.read_lifelines does)."""

    def __init__(self, role, dataset, score, reasons, category=None):
        self.role, self.dataset, self.score = role, dataset, int(round(score))
        self.reasons = list(reasons)
        self.category = category

    @property
    def reason(self):
        return "; ".join(self.reasons)

    def __repr__(self):
        return "Candidate({}, {!r}, {}, {!r})".format(self.role, self.dataset.uri, self.score,
                                                      self.category)


class MatchResult(object):
    """candidates: role -> [Candidate] best first (every layer that fits);
    chosen: role -> Candidate or None (one layer per role); lifelines: the
    chosen lifeline layers; unmatched: DataSets given no role."""

    def __init__(self):
        self.candidates = {r: [] for r in ROLES}
        self.chosen = {r: None for r in ROLES if r not in MULTI_ROLES}
        self.lifelines = []
        self.datasets = []
        self.unmatched = []
        self.notes = []
        self.folder = ""

    def best(self, role):
        return list(self.lifelines) if role in MULTI_ROLES else self.chosen.get(role)

    @property
    def found(self):
        return [r for r in ROLES if self.best(r)]

    @property
    def missing(self):
        return [r for r in ROLES if not self.best(r)]


class _Evidence(object):
    def __init__(self):
        self.primary, self.support = [], []

    def add(self, score, reason):
        if score > 0:
            self.primary.append((score, reason))

    def also(self, reason):
        self.support.append(reason)

    def total(self):
        if not self.primary:
            return 0, []
        parts = sorted(self.primary, key=lambda p: -p[0])
        extra = len(parts) - 1 + len(self.support)
        cap = 99 if parts[0][0] >= S_PLUGIN else S_PLUGIN - 1
        return min(cap, parts[0][0] + S_SUPPORT * extra), [r for _s, r in parts] + self.support


def _is_own_output(names):
    T = _tab_rules()
    for n in names:
        if is_plugin_output_name(n) or T._PLUGIN_POI_RE.search(n) or "inundated" in n.lower():
            return True
    return False


def _pattern_hit(names, pattern, exclude=None):
    """First name (raw or as a key: '_' '-' '.' as spaces) the pattern finds,
    unless the exclusion finds it too."""
    for n in names:
        forms = (n, _layer_name_key(n))
        if any(pattern.search(f) for f in forms) and \
                not (exclude is not None and any(exclude.search(f) for f in forms)):
            return n
    return None


def _family(names):
    """processing's point-layer family of the first name one of its rules
    matches (NSI / NDGISHUB; None for FEMA points)."""
    for n in names:
        key = _layer_name_key(n)
        for fam, pattern in _GROUP_PATTERNS:
            if pattern.search(key):
                return fam, n
    return None, None


class _Probe(object):
    """Just enough of linear.AttrReader for trains_per_day(): does the layer
    have a train-count field?"""

    def __init__(self, low):
        self.low = {n: i for i, n in enumerate(low)}

    def get(self, attrs, name):
        return 1.0 if name in self.low else None


def _plugin_hit(ds, table):
    """(roles, category, reason) when the data set carries one of the
    plugin's own data-set names."""
    if ds.data_label:
        for d in DATASETS:
            if d[2] == ds.data_label and _DATASET_ROLES.get(d[0]):
                return ((_DATASET_ROLES[d[0]],), None,
                        "plugin data set '{}' (Downloaded_Data.csv)".format(d[1]))
    names = list(ds.hints)
    m = _CACHE_RE.match(os.path.basename(ds.source))
    if m:
        names.append(m.group(1))           # a download-cache file: <name>_<date>_<hash>
    for n in names:
        key = _layer_name_key(n).strip()
        for pk, roles, cat, reason in table:
            if key == pk:
                return roles, cat, reason
            # <dam>_<name> in the plugin's Data folder / outputs (outline file)
            if key.endswith(" " + pk) and (ds.plugin_data or "usa" in roles):
                return roles, cat, reason
    return None


def _score(ds, table, nhd_fields):
    """{role: (score, reasons, category)} for one data set."""
    T = _tab_rules()
    names = [n for n in ds.hints if n]
    low = ds.fields_lower
    compact = [re.sub(r"[^a-z0-9]", "", f) for f in low]
    ev = {r: _Evidence() for r in ROLES}
    cats = {}
    geom = ds.geometry

    hit = _plugin_hit(ds, table)
    if hit is not None:
        roles, cat, reason = hit
        role = next((r for r in roles if geom in ROLE_GEOMETRY[r]), None)
        if role is not None:
            ev[role].add(S_PLUGIN, reason)
            if role in MULTI_ROLES:
                cats[role] = cat

    # --- structures (group 3) ---------------------------------------------
    fam, fam_name = _family(names)
    nsi = [f for f in _NSI_FIELDS if f in low]
    if len(nsi) >= 3:
        ev["nsi"].add(S_FIELDS, "fields {} -> NSI".format(", ".join(nsi[:4])))
    elif len(nsi) == 2:
        ev["nsi"].add(60, "fields {} -> NSI".format(", ".join(nsi)))
    elif nsi and nsi[0] in ("fd_id", "occtype"):
        ev["nsi"].add(40, "field {} -> NSI".format(nsi[0]))
    if fam == "NSI":
        ev["nsi"].add(S_NAME, "name '{}' -> NSI".format(fam_name))
    nena = [f for f in _NENA_MARKERS if f in low]
    if len(nena) >= 3:
        ev["address"].add(S_FIELDS, "NENA fields {} -> site / structure address points".format(
            ", ".join(nena[:4])))
        ev["roads"].add(70, "NENA NG9-1-1 fields {} -> road centerlines".format(
            ", ".join(nena[:3])))
    elif len(nena) == 2:
        ev["address"].add(60, "NENA fields {} -> address points".format(", ".join(nena)))
        ev["roads"].add(50, "NENA street fields {}".format(", ".join(nena)))
    if fam == "NDGISHUB":
        ev["address"].add(S_NAME, "name '{}' -> ND GIS Hub address points".format(fam_name))
    fema = [f for f in _FEMA_FIELDS if f in low]
    if len(fema) == len(_FEMA_FIELDS):
        ev["usa"].add(S_FIELDS, "fields {} -> FEMA USA Structures".format(", ".join(fema)))
    elif fema:
        ev["usa"].add(50, "field {} -> FEMA USA Structures".format(fema[0]))
    n = next((x for x in names if _is_fema_structures_name(x)
              or "usa structures" in _layer_name_key(x)), None)
    if n:
        ev["usa"].add(60, "name '{}' -> FEMA USA Structures".format(n))
    n = _pattern_hit(names, _OUTLINE_RE)
    if n:
        ev["outlines"].add(50, "name '{}' -> building outlines".format(n))
    elif next((x for x in names if _is_osm_name(x)), None) and "building" in low:
        ev["outlines"].add(45, "OpenStreetMap buildings")
    if "building" in low and ev["outlines"].primary:
        ev["outlines"].also("field building")
    n = next((x for x in names if _camp_re().search(_singular(_layer_name_key(x)))), None)
    if n:
        ev["camps"].add(65, "name '{}' -> campgrounds / RV parks".format(n))
    # 2.29.0 + OSM layers: mobile-home parks (areas) and populated places (points) of the
    # OpenStreetMap layers (osm_parse.trailer_park_features / place_features)
    n = _pattern_hit(names, _PARK_NAME_RE)
    if n:
        ev["parks"].add(S_NAME_STRONG, "name '{}' -> mobile-home / trailer parks".format(n))
    if "match" in low and "landuse" in low and "osm_id" in low:
        ev["parks"].add(S_FIELDS, "fields match, landuse, osm_id -> OpenStreetMap mobile-home parks")
    if "place" in low and "name" in low and "osm_id" in low:
        ev["places"].add(S_FIELDS, "fields place, name, osm_id -> OpenStreetMap places")
    n = _pattern_hit(names, _PLACES_NAME_RE)
    if n and "place" in low:
        ev["places"].add(S_NAME, "name '{}' -> cities / towns / villages".format(n))

    # --- roads, rail and crossings (group 4) ------------------------------
    rail_names = [x for x in names
                  if T._RAIL_RE.search(re.sub(r"trails?", " ", _layer_name_key(x)))]
    road_words, road_name_hit = set(), None
    for x in names:
        if not T._RAIL_RE.search(x):
            words = {m.group(0).lower() for m in T._ROAD_RE.finditer(_layer_name_key(x))}
            if words and road_name_hit is None:
                road_name_hit = x
            road_words.update(words)
    if road_words - set(_TRAFFIC_WORDS):
        ev["roads"].add(S_NAME, "name '{}' -> roads".format(road_name_hit))
    elif road_words:
        ev["roads"].add(40, "name '{}' -> traffic".format(road_name_hit))
    road_name = next((f for f in _NAME_NAMES if f in low), None)
    aadt = [f for f in low if f in _AADT_NAMES or _RE_YEAR_COL.match(f)]
    if road_name and ev["roads"].primary:
        ev["roads"].also("road-name field {}".format(road_name))
    if aadt and ev["roads"].primary:
        ev["roads"].also("AADT field {}".format(aadt[0]))
    if not rail_names:
        if aadt:
            ev["aadt"].add(60, "fields {} -> AADT".format(", ".join(aadt[:3])))
        if road_words & set(_TRAFFIC_WORDS):
            ev["aadt"].add(S_NAME, "name '{}' -> traffic ({})".format(
                road_name_hit, ", ".join(sorted(road_words & set(_TRAFFIC_WORDS)))))
        year = next((f for f in _AADT_YEAR_NAMES if f in low), None)
        if year and ev["aadt"].primary:
            ev["aadt"].also("year field {}".format(year))
    if rail_names:
        ev["rail"].add(S_NAME, "name '{}' -> rail".format(rail_names[0]))
    narn = [f for f in _OWNER_NAMES + _PASS_NAMES if f in low and f not in ("owner", "operator")]
    if len(narn) >= 2:
        ev["rail"].add(75, "rail fields {} -> rail lines".format(", ".join(narn[:3])))
    elif narn and ev["rail"].primary:
        ev["rail"].also("rail field {}".format(narn[0]))
    bridge_name = _pattern_hit(names, _BRIDGE_NAME_RE)
    if trains_per_day(_Probe(low), None) is not None:
        ev["crossings"].add(75, "train-count fields -> FRA grade crossings")
    xname = _pattern_hit(names, T._CROSS_RE)
    if xname and not bridge_name:
        rail_like = rail_names or any(w in _layer_name_key(xname).split()
                                      for w in ("grade", "fra"))
        ev["crossings"].add(60 if rail_like else 40, "name '{}' -> {}crossings".format(
            xname, "rail grade " if rail_like else ""))
    if _NBI_MARKER in low:
        ev["bridges"].add(85, "field {} -> National Bridge Inventory".format(_NBI_MARKER))
    if "category" in low and any(f in low for f in _OSM_CROSSING_FIELDS):
        ev["bridges"].add(50, "fields category, {} -> OpenStreetMap crossings".format(
            next(f for f in _OSM_CROSSING_FIELDS if f in low)))
    if bridge_name:
        ev["bridges"].add(60, "name '{}' -> bridges / culverts".format(bridge_name))

    # --- dams, lifelines and other layers (group 5) -----------------------
    if any(f in compact for f in _NID_ID_FIELDS):
        ev["dams"].add(85, "NID ID field -> National Inventory of Dams")
    elif "damname" in compact and any(f in compact for f in _DAM_DETAIL_FIELDS):
        ev["dams"].add(60, "dam name / hazard fields -> dams")
    # v2.23.0: the ND DWR dam inventory (structure_id, dam_name, max_dam_height). The DWR
    # unknown-structures and dugouts layers carry the same fields but are not dams.
    not_dams = _pattern_hit(names, _DWR_NOT_DAMS_RE)
    if not not_dams and all(f in compact for f in _DWR_DAM_FIELDS):
        ev["dams"].add(85, "DWR fields structure_id / dam_name / max_dam_height -> dams")
    n = _pattern_hit(names, _DAM_NAME_RE)
    if n and not not_dams:
        ev["dams"].add(60, "name '{}' -> dams".format(n))
    for x in names:
        cat = lifeline_category(x)
        if cat:
            ev["lifelines"].add(S_NAME_STRONG, "name '{}' -> {}".format(x, cat))
            cats.setdefault("lifelines", cat)
            break
    else:
        n = _pattern_hit(names, _LIFELINE_GENERIC_RE)
        if n:
            ev["lifelines"].add(S_NAME, "name '{}' -> lifeline facilities (category per "
                                        "feature)".format(n))
            cats.setdefault("lifelines", None)
            typ = next((f for f in ("category", "type", "fac_type", "facility_type")
                        if f in low), None)
            if typ:
                ev["lifelines"].also("field {}".format(typ))
    n = _pattern_hit(names, _BOUNDARY_STRONG_RE)
    if n:
        ev["boundary"].add(65, "name '{}' -> model boundary / 2D flow area".format(n))
    else:
        n = _pattern_hit(names, _BOUNDARY_WEAK_RE, _BOUNDARY_ADMIN_RE)
        if n:
            ev["boundary"].add(S_WEAK, "name '{}' -> boundary".format(n))
    n = _pattern_hit(names, T._PARCEL_RE, T._PARCEL_EXCLUDE)
    if n:
        ev["parcels"].add(S_NAME_STRONG, "name '{}' -> parcels".format(n))

    # --- channel (group 6) ------------------------------------------------
    nhd = [f for f in nhd_fields if f in low]
    if len(nhd) >= 3:
        ev["centerline"].add(75, "3DHP / NHD fields {} -> flowlines".format(", ".join(nhd[:4])))
    n = _pattern_hit(names, T._CENTERLINE_RE, T._CENTER_EXCLUDE)
    if n:
        ev["centerline"].add(S_NAME, "name '{}' -> channel centerline".format(n))

    out = {}
    for role in ROLES:
        if geom not in ROLE_GEOMETRY[role]:
            continue
        score, reasons = ev[role].total()
        if not score:
            continue
        if ds.data_area and "flood" in ds.data_area.lower() and geom != LINE:
            score -= FLOOD_ONLY_PENALTY
            reasons.append("saved for the flooded area only")
        if ds.provider == "delimitedtext" and not ds.crs:
            score -= NO_CRS_PENALTY
            reasons.append("no coordinate system")
        if score >= MIN_SCORE:
            out[role] = (score, reasons, cats.get(role))
    return out


def match_roles(datasets):
    """MatchResult for the DataSets of scan_folder(): candidates per role,
    best first; every layer assigned to at most one role, the highest score
    first (ties: role order, then path); lifelines may take many layers."""
    res = MatchResult()
    res.datasets = list(datasets or [])
    res.folder = getattr(datasets, "folder", "")
    res.notes = list(getattr(datasets, "notes", []) or [])
    table = _plugin_names()
    nhd_fields = _nhd_fields()
    pairs = []
    for i, ds in enumerate(res.datasets):
        if ds.geometry in (NONE, UNKNOWN):
            continue
        if ds.count == 0:
            continue
        if _is_own_output(ds.hints):
            continue
        for role, (score, reasons, cat) in _score(ds, table, nhd_fields).items():
            c = Candidate(role, ds, score, reasons, cat)
            res.candidates[role].append(c)
            pairs.append((-c.score, ROLES.index(role), ds.uri.lower(), i, c))
    for role in ROLES:
        res.candidates[role].sort(key=lambda c: (-c.score, c.dataset.uri.lower()))
    taken = set()
    for _s, _r, _u, i, c in sorted(pairs, key=lambda p: p[:4]):
        if i in taken:
            continue
        if c.role in MULTI_ROLES:
            res.lifelines.append(c)
        elif res.chosen[c.role] is None:
            res.chosen[c.role] = c
        else:
            continue
        taken.add(i)
    res.unmatched = [ds for i, ds in enumerate(res.datasets) if i not in taken]
    return res


# ----------------------------------------------------------------- output --
def describe(result):
    """Text lines for the run log: the pick for every role with its reason,
    the other candidates, what was not found and what the scan skipped."""
    lines = []
    if result.folder:
        lines.append("Data folder {}: {} layer(s)".format(_uri_path(result.folder),
                                                           len(result.datasets)))
    taken = {id(c.dataset) for c in result.lifelines}
    taken.update(id(c.dataset) for c in result.chosen.values() if c is not None)
    for role in ROLES:
        if role in MULTI_ROLES:
            for c in result.lifelines:
                lines.append("{} [{}]: {} ({})".format(
                    role, c.category or "category per feature", c.dataset.uri, c.reason))
            continue
        c = result.chosen.get(role)
        if c is None:
            continue
        lines.append("{}: {} ({})".format(role, c.dataset.uri, c.reason))
        others = [o for o in result.candidates[role]
                  if o is not c and id(o.dataset) not in taken]
        if others:
            lines.append("    {} other candidate(s) for {}, e.g. {} (score {})".format(
                len(others), role, others[0].dataset.uri, others[0].score))
    for ds in result.datasets:
        for note in ds.notes:
            if id(ds) in taken:
                lines.append("note: {}: {}".format(ds.uri, note))
    if result.missing:
        lines.append("not found: {}".format(", ".join(result.missing)))
    for note in result.notes:
        lines.append("scan note: {}".format(note))
    return lines


def to_config(result):
    """{role: uri} of the picks; "lifelines": [(uri, category or None)] -
    what the lead stores in the settings and passes as cfg["local_data"]."""
    out = {}
    for role in ROLES:
        if role in MULTI_ROLES:
            if result.lifelines:
                out[role] = [(c.dataset.uri, c.category) for c in result.lifelines]
        elif result.chosen.get(role) is not None:
            out[role] = result.chosen[role].dataset.uri
    return out


def provider_for_uri(uri):
    """"delimitedtext" for a CSV URI made here, else "ogr"."""
    u = (uri or "").strip()
    return "delimitedtext" if u.lower().startswith("file:") and "?" in u else "ogr"


def _name_from_uri(uri):
    u = uri or ""
    if "|layername=" in u:
        return u.split("|layername=", 1)[1].split("|")[0]
    return _stem(u.split("?")[0].split("|")[0])


def open_uri(uri, name=None, provider=None):
    """QgsVectorLayer for a URI of to_config() / DataSet.uri, or None when it
    cannot be opened (missing file, invalid layer). Never raises."""
    if not uri:
        return None
    try:
        lyr = QgsVectorLayer(uri, name or _name_from_uri(uri), provider or provider_for_uri(uri))
        return lyr if lyr.isValid() else None
    except Exception:
        return None


def open_layer(dataset, name=None):
    """QgsVectorLayer of a DataSet (or Candidate), or None."""
    ds = getattr(dataset, "dataset", dataset)
    if ds is None:
        return None
    return open_uri(ds.uri, name or ds.name, ds.provider)


def _file_of_uri(uri):
    if provider_for_uri(uri) == "delimitedtext":
        try:
            from urllib.parse import unquote, urlparse
            u = urlparse(uri.split("?")[0])
            p = unquote(u.path)
            if u.netloc:                       # UNC: file://server/share/...
                return "//" + u.netloc + p
            if re.match(r"^/[A-Za-z]:", p):
                p = p[1:]
            return p
        except Exception:
            return ""
    p = uri.split("|")[0]
    if p.startswith("/vsizip/"):
        m = re.match(r"^/vsizip/(.+?\.zip)(?:/|$)", p, re.I)
        return m.group(1) if m else ""
    return p


def open_config(local_data):
    """Open the layers of a to_config() dict (also after a JSON round trip):
    ({role: layer, "lifelines": [(layer, category)]}, notes). A file that no
    longer exists or does not open is left out with a note."""
    layers, notes = {}, []
    for role, value in (local_data or {}).items():
        if role not in ROLES:
            continue
        if role in MULTI_ROLES and not isinstance(value, str):
            items = value
        else:
            items = [(value, None)] if isinstance(value, str) else [value]
        opened = []
        for item in items or []:
            try:
                uri, cat = (item[0], item[1]) if isinstance(item, (list, tuple)) else (item, None)
            except (IndexError, TypeError):
                continue
            path = _file_of_uri(uri)
            if path and not os.path.exists(path):
                notes.append("{}: {} no longer exists".format(role, path))
                continue
            lyr = open_uri(uri)
            if lyr is None:
                notes.append("{}: {} could not be opened".format(role, uri))
                continue
            opened.append((lyr, cat))
        if role in MULTI_ROLES:
            if opened:
                layers[role] = opened
        elif opened:
            layers[role] = opened[0][0]
    return layers, notes


def find_data(folder, max_depth=DEFAULT_MAX_DEPTH, progress=None, cancel=None,
              max_files=DEFAULT_MAX_FILES):
    """scan_folder() + match_roles() in one call."""
    return match_roles(scan_folder(folder, max_depth=max_depth, progress=progress,
                                   cancel=cancel, max_files=max_files))


# ------------------------------------------------- dam folder data (2.17.0) --
# "Get data for this dam" (damsuite.damdata) writes <dam folder>/data/; the Data & Output
# tab fills the data sets the engineer left empty from there (datafolder_tab.dam_data_picks).
# Those files hold the dam's surroundings only (a box around the dam, "Data within ... mi"),
# so a run uses one only when that box holds the whole study area; else the role is read
# as before (QGIS layer, offline pack, internet) and the log says why.
DAM_DATA_DIR = "data"                   # damdata.DATA_DIR
DAM_PACKAGE = "suite_package.gpkg"      # damdata.PACKAGE (this plugin's suite export)
_DAM_COVER_ROLES = ("nsi", "address", "usa", "roads", "rail", "crossings", "bridges",
                    "dams", "lifelines")
_STRUCTURE_LAYERS = ("structures", "structure_points", "address_points")


def dam_data_dir(dam_folder):
    return os.path.join(dam_folder, DAM_DATA_DIR) if dam_folder else ""


def is_dam_data(uri, dam_folder):
    """True for a URI of a file in <dam folder>/data/."""
    d = dam_data_dir(dam_folder)
    if not d or not isinstance(uri, str):
        return False
    try:
        f = os.path.normcase(os.path.abspath(_file_of_uri(uri)))
        return os.path.dirname(f) == os.path.normcase(os.path.abspath(d))
    except Exception:                                            # noqa
        return False


def _inner_lonlat(box5070):
    """The lon / lat box inside an EPSG:5070 rectangle (conservative: its corners)."""
    from .damsuite import suitegpkg as G
    x0, y0, x1, y1 = box5070
    ll, lr = G.from_5070(x0, y0), G.from_5070(x1, y0)
    ul, ur = G.from_5070(x0, y1), G.from_5070(x1, y1)
    return (max(ll[0], ul[0]), max(ll[1], lr[1]), min(lr[0], ur[0]), min(ul[1], ur[1]))


def dam_data_area(dam_folder):
    """(lon / lat box the dam folder's data are known to cover or None, package usable,
    note). The suite package records its area (suite_export_info area_lonlat; a data set
    it says is INCOMPLETE for the area makes the package unusable); else the extent of the
    structure layers damdata wrote (EPSG:5070) stands for the box. Never raises."""
    import sqlite3
    d = dam_data_dir(dam_folder)
    if not d or not os.path.isdir(d):
        return None, False, "no data folder"
    pkg = os.path.join(d, DAM_PACKAGE)
    box, pkg_ok, note = None, False, ""
    if os.path.isfile(pkg):
        try:
            con = sqlite3.connect("file:{}?mode=ro".format(
                pkg.replace("?", "%3f").replace("#", "%23")), uri=True)
            try:
                rows = dict(con.execute("SELECT key, value FROM suite_export_info").fetchall())
            finally:
                con.close()
            area = json.loads(rows.get("area_lonlat") or "null")
            if isinstance(area, list) and len(area) == 4:
                box = tuple(float(v) for v in area)
            bad = sorted(k[8:] for k, v in rows.items()
                         if k.startswith("dataset:") and "INCOMPLETE" in str(v))
            pkg_ok = box is not None and not bad
            if bad:
                note = "the suite package says {} is incomplete for its area".format(
                    ", ".join(bad))
        except Exception as e:                                   # noqa
            note = "suite package not read ({})".format(_short(e))
    if box is None:
        boxes = []
        for name in _STRUCTURE_LAYERS:
            p = os.path.join(d, name + ".gpkg")
            if not os.path.isfile(p):
                continue
            try:
                con = sqlite3.connect("file:{}?mode=ro".format(
                    p.replace("?", "%3f").replace("#", "%23")), uri=True)
                try:
                    r = con.execute("SELECT min_x, min_y, max_x, max_y, srs_id FROM "
                                    "gpkg_contents WHERE table_name = ?", (name,)).fetchone()
                finally:
                    con.close()
            except Exception:                                    # noqa
                continue
            if r and None not in r[:4] and int(r[4] or 0) == 5070:
                boxes.append(_inner_lonlat(r[:4]))
        if boxes:
            box = (max(b[0] for b in boxes), max(b[1] for b in boxes),
                   min(b[2] for b in boxes), min(b[3] for b in boxes))
    if box is not None and not (box[0] < box[2] and box[1] < box[3]):
        box = None
    return box, pkg_ok, note


def _holds(outer, inner):
    return outer is not None and inner is not None and outer[0] <= inner[0] and \
        outer[1] <= inner[1] and outer[2] >= inner[2] and outer[3] >= inner[3]


def drop_uncovered(local_data, dam_folder, box, crossing_box=None):
    """local_data without the dam-folder layers whose data do not hold the study area
    (`box`, lon / lat; `crossing_box` for the grade crossings): (local_data, notes, roles
    kept from the dam folder). Layers from anywhere else are left as they are."""
    if not isinstance(local_data, dict) or not local_data or not dam_folder:
        return local_data, [], []
    mine = {}
    for role, v in local_data.items():
        uris = [x[0] for x in v if isinstance(x, (list, tuple)) and x] \
            if isinstance(v, (list, tuple)) else [v]
        if uris and all(is_dam_data(u, dam_folder) for u in uris):
            mine[role] = uris
    if not mine:
        return local_data, [], []
    area, pkg_ok, note = dam_data_area(dam_folder)
    out, notes, kept = dict(local_data), [], []
    for role, uris in mine.items():
        want = crossing_box if role == "crossings" and crossing_box is not None else box
        pkg = any(os.path.basename(_file_of_uri(u)).lower() == DAM_PACKAGE for u in uris)
        why = ""
        if role not in _DAM_COVER_ROLES:
            why = "not a data set the dam folder fills"
        elif want is None:
            why = "the study area is not known"
        elif area is None:
            why = note or "the area of the dam folder's data is not known"
        elif pkg and not pkg_ok:
            why = note or "the suite package's area is not known"
        elif not _holds(area, want):
            why = ("the study area reaches beyond the dam folder's data (Get data for this "
                   "dam again with a larger 'Data within' distance)")
        if why:
            out.pop(role, None)
            notes.append("{}: the dam folder's layer is not used - {}".format(
                ROLE_LABELS.get(role, role), why))
        else:
            kept.append(role)
    return out, notes, kept


def existing_roles(local_data):
    """The roles of a to_config() dict whose file (at least one, for the
    lifelines) still exists - what a run will take from the folder instead of
    downloading. A missing file is left to the download."""
    if not isinstance(local_data, dict) or not local_data:
        return set()
    out = set()
    for role, v in local_data.items():
        uris = [x[0] for x in v if isinstance(x, (list, tuple)) and x] \
            if isinstance(v, (list, tuple)) else [v]
        for uri in uris:
            try:
                f = _file_of_uri(uri) if isinstance(uri, str) else ""
            except Exception:                        # noqa
                f = ""
            if f and os.path.exists(f):
                out.add(role)
                break
    return out
