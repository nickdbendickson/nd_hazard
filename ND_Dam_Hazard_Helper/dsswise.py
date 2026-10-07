# -*- coding: utf-8 -*-
"""DSS-WISE Lite (NCCHE, University of Mississippi / FEMA) results import.

A DSS-WISE Lite "Simulation Results Package" (zip) holds GeoTIFF rasters,
shapefiles, a KMZ, a PDF report, CSV discharge hydrographs at the
observation lines and - with HCOM - an Excel table of the human
consequence analysis. Published examples name the rasters like
    NAXXXXX_FloodDepth_in_Ft-05-03-2017_UTM.tif
    NAXXXXX_FloodArrivalTime_in_Hrs-05-03-2017_UTM.tif
    NAXXXXX_DEM_Elev_in_Ft-05-03-2017_UTM.tif
i.e. the quantity and its units are in the file name. Because the package
layout is not formally documented, every file is classified by name (role
and units) and the result is shown for confirmation before import; roles
and units can be changed by the user.

Import converts the rasters to the plugin's units (depth ft, velocity
ft/s, depth x velocity ft2/s, arrival hours) as GeoTIFFs, derives velocity
from DV / depth when only DV is supplied, summarises the HCOM table and the
discharge hydrographs, and returns a Scenario for the classification.
"""

import csv
import hashlib
import os
import re
import zipfile

import numpy as np
from osgeo import gdal

from .helpers import log_message, gdal_exceptions, prepare_output_path
from .scenarios import Scenario
from .compat import MSG_WARNING

R_DEPTH, R_VEL, R_DV, R_ARR, R_DEM, R_WSE, R_PEAK = (
    "Max depth", "Max velocity", "Max depth x velocity", "Arrival time", "Ground (DEM)",
    "Water surface", "Time of peak")
V_BOUNDARY, V_CONTOUR, T_HCOM, T_DISCHARGE, T_REPORT, T_KMZ, T_OTHER, T_IGNORE = (
    "Inundation boundary", "Contour polygons", "HCOM table", "Discharge CSV", "PDF report",
    "KMZ", "Other", "(ignore)")
V_HAZARD = "Hazard zone polygons"
RASTER_ROLES = [R_DEPTH, R_VEL, R_DV, R_ARR, R_DEM, R_WSE, R_PEAK]
ALL_ROLES = RASTER_ROLES + [V_BOUNDARY, V_CONTOUR, V_HAZARD, T_HCOM, T_DISCHARGE, T_REPORT,
                            T_KMZ, T_OTHER, T_IGNORE]
_HAZARD_WORDS = ("hazard", "danger", "plfz", "lethal", "severity", "risk", "hcom")
UNIT_CHOICES = {
    R_DEPTH: ["ft", "m"], R_DEM: ["ft", "m"], R_WSE: ["ft", "m"],
    R_VEL: ["ft/s", "m/s"], R_DV: ["ft2/s", "m2/s"],
    R_ARR: ["h", "min", "s"], R_PEAK: ["h", "min", "s"],
}
DEFAULT_UNITS = {R_DEPTH: "ft", R_DEM: "ft", R_WSE: "ft", R_VEL: "ft/s", R_DV: "ft2/s",
                 R_ARR: "h", R_PEAK: "h"}
TO_PLUGIN = {"ft": 1.0, "m": 3.28083989501312, "ft/s": 1.0, "m/s": 3.28083989501312,
             "ft2/s": 1.0, "m2/s": 10.763910416709722, "h": 1.0, "min": 1.0 / 60.0,
             "s": 1.0 / 3600.0}
OUT_NAMES = {R_DEPTH: "Depth_ft.tif", R_VEL: "Velocity_ftps.tif", R_DV: "DxV_ft2s.tif",
             R_ARR: "Arrival_hr.tif", R_DEM: "DEM_ft.tif", R_WSE: "WSE_ft.tif",
             R_PEAK: "TimeOfPeak_hr.tif"}
RASTER_EXT = (".tif", ".tiff", ".img", ".asc", ".flt", ".bil", ".vrt", ".grd")
VECTOR_EXT = (".shp", ".gpkg", ".geojson", ".json")


class Item(object):
    def __init__(self, path, role, units=None, basis=""):
        self.path = path
        self.role = role
        self.units = units
        self.basis = basis

    @property
    def name(self):
        return os.path.basename(self.path.rstrip("/\\"))

    def __repr__(self):
        return "<Item {} {} {}>".format(self.name, self.role, self.units)


# ------------------------------------------------------------------ package --
def _zip_key(path):
    """Short fingerprint of a zip (size, time, first MB): a revised package
    under the same name gets its own folder."""
    h = hashlib.sha1()
    try:
        st = os.stat(path)
        h.update("{}|{}".format(st.st_size, int(st.st_mtime)).encode("ascii"))
        with open(path, "rb") as fh:
            h.update(fh.read(1 << 20))
    except OSError:
        h.update(str(path).encode("utf-8", "replace"))
    return h.hexdigest()[:10]


def open_package(path, work_folder):
    """Folder holding the package contents. A zip is extracted safely into
    work_folder/package_<fingerprint>: a fresh folder for every different
    zip, so files of an earlier package imported under the same name can
    never be mixed in."""
    if os.path.isdir(path):
        return path
    if not zipfile.is_zipfile(path):
        raise ValueError("{} is neither a folder nor a zip file".format(path))
    dest = os.path.join(work_folder, "package_" + _zip_key(path))
    os.makedirs(dest, exist_ok=True)
    root = os.path.realpath(dest)
    with zipfile.ZipFile(path) as zf:
        for member in zf.infolist():
            if is_junk_path(member.filename):
                continue        # v2.4.1: __MACOSX / ._* / .DS_Store not extracted
            target = os.path.realpath(os.path.join(dest, member.filename))
            if target != root and not target.startswith(root + os.sep):
                log_message("DSS-WISE: skipped unsafe path in zip: {}".format(member.filename),
                            MSG_WARNING)
                continue
            zf.extract(member, dest)
    # a zip inside the zip (package of packages)
    for dirpath, _dirs, files in os.walk(dest):
        for f in files:
            if f.lower().endswith(".zip") and not is_junk_path(f):
                inner = os.path.join(dirpath, f)
                sub = os.path.join(dirpath, os.path.splitext(f)[0])
                try:
                    open_package(inner, sub)
                except Exception as e:
                    log_message("DSS-WISE: inner zip {} not extracted: {}".format(f, e),
                                MSG_WARNING)
    return dest


def _tokens(name):
    return [t for t in re.split(r"[^a-z0-9]+", name.lower()) if t]


def detect_role(name):
    low = name.lower()
    flat = re.sub(r"[^a-z0-9]", "", low)
    ext = os.path.splitext(low)[1]
    if ext in (".pdf",):
        return T_REPORT
    if ext in (".kmz", ".kml"):
        return T_KMZ
    if ext in (".xlsx", ".xls"):
        return T_HCOM
    if ext == ".csv":
        if any(k in flat for k in ("hcom", "consequence", "population", "censusblock")):
            return T_HCOM
        return T_DISCHARGE if any(k in flat for k in ("discharge", "flow", "hydrograph",
                                                      "observation", "obs")) else T_OTHER
    if ext in VECTOR_EXT:
        if any(k in flat for k in _HAZARD_WORDS):
            return V_HAZARD                  # e.g. ..._FloodHazardZones.shp
        if "contour" in flat:
            return V_CONTOUR
        if any(k in flat for k in ("boundary", "extent", "inundation", "wetdry", "floodline",
                                   "flood")):
            return V_BOUNDARY
        return T_OTHER
    if ext in RASTER_EXT or ext == "" or ext == ".adf":
        if "arrival" in flat:
            return R_ARR
        # time of peak / time of max depth ("times" as in DepthTimesVelocity
        # is a product, not a time)
        if re.search(r"time(?!s)", flat) and any(k in flat for k in ("peak", "max")):
            return R_PEAK
        if "duration" in flat:
            return T_OTHER
        if re.search(r"depth.?x.?vel|depthvel|dxv|dtimesv|depth.{0,8}times.{0,8}vel|"
                     r"specificdischarge|unitdischarge", flat) or "dv" in _tokens(low) or \
                re.search(r"(^|[^a-z])dv(max)?([^a-z]|$)", low):
            return R_DV
        # v2.9: the short names modellers use for the maxima: Hmax / H_max / MaxH / Dmax
        # (depth) and Vmax / V_max / MaxV (velocity); HV / HVmax is depth x velocity
        if re.search(r"(^|[^a-z])h[\s_\-]?v(max)?(?![a-z])", low):
            return R_DV
        if any(k in flat for k in ("velocity", "speed")) or "vel" in _tokens(low):
            return R_VEL
        if re.search(r"(^|[^a-z])v[\s_\-]?max(?![a-z])|(^|[^a-z])max[\s_\-]?v(?![a-z])", low):
            return R_VEL
        if "depth" in flat:
            return R_DEPTH
        if re.search(r"(^|[^a-z])[hd][\s_\-]?max(?![a-z])|(^|[^a-z])max[\s_\-]?[hd](?![a-z])",
                     low):
            return R_DEPTH
        if any(k in flat for k in ("wse", "watersurface", "stage")):
            return R_WSE
        if any(k in flat for k in ("dem", "elev", "terrain", "ground")):
            return R_DEM
        return T_OTHER
    return T_IGNORE


def detect_units(name, role):
    """(units, basis): from the 'in_<units>' part of the name, else the
    DSS-WISE Lite default (US units) marked as assumed."""
    if role not in UNIT_CHOICES:
        return None, ""
    toks = _tokens(os.path.splitext(name)[0])
    cand = []
    for i, t in enumerate(toks):
        if t == "in" and i + 1 < len(toks):
            # "in_Ft", "in_M_per_Sec", "in_Ft2_per_S"
            cand.append([x for x in toks[i + 1:i + 4] if x != "per"][:2])
        m = re.match(r"^in(ft2?|m2?|hrs?|hours?|min|mins|minutes|sec|secs|seconds|fps|mps)$", t)
        if m:
            cand.append([m.group(1)] + toks[i + 1:i + 2])
    for c in cand:
        u = _unit_from(c, role)
        if u:
            return u, "file name"
    for t in toks:
        u = _unit_from([t], role, strict=True)
        if u:
            return u, "file name"
    return DEFAULT_UNITS[role], "assumed (DSS-WISE Lite US units)"


def _unit_from(parts, role, strict=False):
    a = parts[0] if parts else ""
    b = parts[1] if len(parts) > 1 else ""
    if role in (R_DEPTH, R_DEM, R_WSE):
        if a in ("ft", "feet", "foot"):
            return "ft"
        if a in ("m", "meter", "meters", "metre", "metres") and not strict:
            return "m"
        if a in ("meter", "meters", "metre", "metres"):
            return "m"
    if role == R_VEL:
        if a in ("fps", "ftps", "ftpersec", "ftpersecond") or (a == "ft" and b in ("s", "sec")):
            return "ft/s"
        if a in ("mps", "mpersec") or (a == "m" and b in ("s", "sec") and not strict):
            return "m/s"
    if role == R_DV:
        if a in ("ft2ps", "ft2s", "sqftps", "ft2persec") or (a == "ft2" and b in ("s", "sec")):
            return "ft2/s"
        if a in ("m2ps", "m2s", "m2persec") or (a == "m2" and b in ("s", "sec")):
            return "m2/s"
    if role in (R_ARR, R_PEAK):
        if a in ("hrs", "hr", "hours", "hour") or (a == "h" and not strict):
            return "h"
        if a in ("min", "mins", "minutes"):
            return "min"
        if a in ("sec", "secs", "seconds") or (a == "s" and not strict):
            return "s"
    return None


def scan(folder):
    """[Item] for every recognisable file / ESRI grid in the package."""
    items = []
    for dirpath, dirs, files in os.walk(folder):
        # v2.4.1: never descend into __MACOSX / .Trashes (a folder package
        # copied from a Mac keeps them even when the zip step is skipped)
        dirs[:] = [d for d in dirs if not is_junk_path(d)]
        if "hdr.adf" in [f.lower() for f in files]:     # ESRI binary grid folder
            role = detect_role(os.path.basename(dirpath) + ".adf")
            units, basis = detect_units(os.path.basename(dirpath), role)
            items.append(Item(dirpath, role, units, basis))
            dirs[:] = []
            continue
        for f in sorted(files):
            # v2.4.0: skip macOS / Windows metadata files
            if is_junk_path(f):
                continue
            low = f.lower()
            if low.endswith((".aux.xml", ".ovr", ".prj", ".dbf", ".shx", ".cpg", ".sbn", ".sbx",
                             ".xml", ".tfw", ".zip", ".qpj")):
                continue
            role = detect_role(f)
            if role == T_IGNORE:
                continue
            units, basis = detect_units(f, role)
            items.append(Item(os.path.join(dirpath, f), role, units, basis))
    # one file per raster role: prefer names stating units, then UTM over geographic
    return items


_GEOGRAPHIC_WORDS = ("gcs", "wgs84", "wgs", "geographic", "latlon", "latlong", "geo")


def _rank(it):
    """Preference among several files of one role: units stated in the name,
    then UTM (projected) over geographic copies, then the shorter name."""
    toks = _tokens(it.name)
    geo = any(t in _GEOGRAPHIC_WORDS for t in toks)
    return (0 if it.basis == "file name" or it.basis == "set by user" else 1,
            0 if "utm" in toks else (2 if geo else 1), len(it.name), it.name)


def chosen(items, role):
    """The item used for a role: the preferred one when several files have
    the same role (the UI lets the user switch the others to (ignore))."""
    cands = [it for it in items if it.role == role]
    return min(cands, key=_rank) if cands else None


def duplicate_roles(items):
    """{role: [names]} of raster roles held by more than one file."""
    out = {}
    for it in items:
        if it.role in RASTER_ROLES:
            out.setdefault(it.role, []).append(it.name)
    return {r: n for r, n in out.items() if len(n) > 1}


def assumed_units(items):
    """Chosen raster items whose units were not stated anywhere."""
    out = []
    for role in RASTER_ROLES:
        it = chosen(items, role)
        if it is not None and it.basis.startswith("assumed"):
            out.append(it)
    return out


# ---------------------------------------------------------------- converting --
def _convert_raster(src, dst, factor, progress=None):
    """Copy `src` to a float32 GeoTIFF multiplied by `factor` (NoData -9999)."""
    with gdal_exceptions():
        ds = gdal.Open(src, gdal.GA_ReadOnly)
        if ds is None:
            raise RuntimeError("cannot open {}".format(src))
        band = ds.GetRasterBand(1)
        nd = band.GetNoDataValue()
        scale, offset = band.GetScale(), band.GetOffset()
        scale = 1.0 if scale in (None, 0) else float(scale)
        offset = 0.0 if offset is None else float(offset)
        w, h = ds.RasterXSize, ds.RasterYSize
        dst = prepare_output_path(dst)
        out = gdal.GetDriverByName("GTiff").Create(
            dst, w, h, 1, gdal.GDT_Float32, options=["COMPRESS=LZW", "TILED=YES",
                                                    "BIGTIFF=IF_SAFER"])
        out.SetGeoTransform(ds.GetGeoTransform())
        proj = ds.GetProjection()
        if proj:
            out.SetProjection(proj)
        ob = out.GetRasterBand(1)
        ob.SetNoDataValue(-9999.0)
        rows = max(1, min(h, 2000000 // max(1, w)))
        for y0 in range(0, h, rows):
            n = min(rows, h - y0)
            a = np.asarray(band.ReadAsArray(0, y0, w, n), dtype=np.float64)
            valid = np.isfinite(a) & (a > -9999)
            if nd is not None:
                valid &= a != nd
            a = (a * scale + offset) * factor
            ob.WriteArray(np.where(valid, a, -9999.0).astype(np.float32), 0, y0)
            if progress:
                progress(100.0 * (y0 + n) / h)
        ob.FlushCache()
        ob = None
        out = None
        ds = None
    return dst


def convert(items, out_folder, name="DSS-WISE Lite", progress=None):
    """Convert the chosen rasters, summarise tables. Returns a dict with the
    converted paths, a Scenario and summary lines."""
    os.makedirs(out_folder, exist_ok=True)
    res = {"paths": {}, "notes": [], "lines": [], "boundary": None, "report": None,
           "hazard_zones": [it.path for it in items if it.role == V_HAZARD],
           "hcom": [it.path for it in items if it.role == T_HCOM]}
    for role, names in duplicate_roles(items).items():
        res["notes"].append("{} files for '{}': {} - used {} (set the others to (ignore) to "
                            "choose another)".format(len(names), role, ", ".join(names),
                                                     chosen(items, role).name))
    for it in assumed_units(items):
        res["notes"].append("Units of {} were not stated in the file name - {} assumed; "
                            "confirm against the DSS-WISE report".format(it.name, it.units))
    roles = [r for r in RASTER_ROLES if chosen(items, r) is not None]
    for i, role in enumerate(roles):
        it = chosen(items, role)
        units = it.units or DEFAULT_UNITS[role]
        factor = TO_PLUGIN[units]
        dst = os.path.join(out_folder, OUT_NAMES[role])

        def sub(p, i=i, role=role):
            if progress:
                progress(100.0 * (i + p / 100.0) / max(1, len(roles) + 1),
                         "Converting {}".format(role))
        res["paths"][role] = _convert_raster(it.path, dst, factor, sub)
        res["lines"].append("{}: {} ({} -> plugin units, x{:g}; units {})".format(
            role, it.name, units, factor, it.basis or "set by user"))
    if R_DEPTH not in res["paths"]:
        raise RuntimeError("The package has no maximum-depth raster (or it was not "
                           "recognised - set its role in the table).")
    if R_VEL not in res["paths"] and R_DV in res["paths"]:
        # v2.5.1: ACER zones come from Dmax / Vmax only. DVmax / Dmax is NOT a
        # velocity (the maxima occur at different times), so without a
        # velocity raster the depth-only (1D) criterion is used; D x V is
        # kept for flood severity in the loss-of-life estimate
        res["notes"].append("No velocity raster: ACER zones need Dmax / Vmax - the depth-only "
                            "(1D) criterion is used; D x V is kept for flood severity")
    if R_VEL not in res["paths"] and R_DV not in res["paths"]:
        res["notes"].append("No velocity or DV raster: ACER depth-velocity zones cannot be "
                            "evaluated - use the depth-only (1D) criterion")
    if R_ARR not in res["paths"]:
        res["notes"].append("No arrival-time raster: warning times and arrival fields are "
                            "left empty")
    b = chosen(items, V_BOUNDARY)
    if b is not None:
        res["boundary"] = b.path
    rep = chosen(items, T_REPORT)
    if rep is not None:
        res["report"] = rep.path
    for it in items:
        if it.role == T_HCOM:
            res["lines"].extend(hcom_summary(it.path))
        elif it.role == T_DISCHARGE:
            res["lines"].extend(discharge_summary(it.path))
    p = res["paths"]
    res["scenario"] = Scenario(name, p[R_DEPTH], p.get(R_VEL), p.get(R_ARR), dv=p.get(R_DV),
                               source="DSS-WISE Lite")
    res["depth_only"] = R_VEL not in p          # v2.5.1: no Vmax -> 1D criterion
    with open(prepare_output_path(os.path.join(out_folder, "DSSWISE_Import_Summary.txt")),
              "w", encoding="utf-8") as fh:
        fh.write("\n".join(["DSS-WISE Lite import: " + name, ""] + res["lines"] + [""] +
                           ["NOTE: " + n for n in res["notes"]]) + "\n")
    if progress:
        progress(100.0, "DSS-WISE import done")
    return res


# -------------------------------------------------------------------- tables --
def _read_table(path):
    """(header, rows) of the first sheet / CSV table."""
    if path.lower().endswith(".csv"):
        with open(path, newline="", encoding="utf-8-sig", errors="replace") as fh:
            rows = list(csv.reader(fh))
        return (rows[0], rows[1:]) if rows else ([], [])
    from qgis.core import QgsVectorLayer
    lyr = QgsVectorLayer(path, "hcom", "ogr")
    if not lyr.isValid():
        return [], []
    header = [f.name() for f in lyr.fields()]
    rows = [[f[n] for n in header] for f in lyr.getFeatures()]
    return header, rows


def _col(header, *keys, exclude=()):
    for i, h in enumerate(header):
        k = re.sub(r"[^a-z0-9]", "", str(h).lower())
        if all(x in k for x in keys) and not any(x in k for x in exclude):
            return i
    return None


def _fnum(v):
    try:
        f = float(str(v).replace(",", ""))
        return f if np.isfinite(f) else None
    except (TypeError, ValueError):
        return None


_HCOM_FIELDS = (("pop_day", "population (day)", ("pop", "day"), ()),
                ("pop_night", "population (night)", ("pop", "night"), ()),
                ("pop", "population", ("pop",), ("day", "night")),
                ("par", "population at risk", ("par",), ("parcel",)),
                ("housing", "housing units", ("hous",), ()))


def hcom_totals(path):
    """Totals of a DSS-WISE HCOM (census block) table as a dict: file, rows,
    header, and - for each column found - pop_day, pop_night, pop (census
    population), par (population at risk), housing (housing units) and
    max_dv, with the column it came from in "columns". Column names are
    matched loosely (the table layout is not formally documented). {"error":
    text, "file": name} when the table cannot be read."""
    name = os.path.basename(path)
    try:
        header, rows = _read_table(path)
    except Exception as e:
        return {"file": name, "error": "not readable ({})".format(e)}
    if not header:
        return {"file": name, "error": "empty or not readable"}
    out = {"file": name, "rows": len(rows), "header": [str(h) for h in header],
           "columns": {}}
    for key, _label, keys, excl in _HCOM_FIELDS:
        i = _col(header, *keys, exclude=excl)
        if i is not None:
            out[key] = sum(_fnum(r[i]) or 0 for r in rows if i < len(r))
            out["columns"][key] = str(header[i])
    i = _col(header, "maxdv")
    if i is None:
        i = _col(header, "dv", exclude=("time", "arrival"))
    if i is not None:
        vals = [v for v in (_fnum(r[i]) for r in rows if i < len(r)) if v is not None]
        if vals:
            out["max_dv"] = max(vals)
            out["columns"]["max_dv"] = str(header[i])
    return out


def hcom_summary(path):
    """Totals of a DSS-WISE HCOM (census block) table: population at risk
    day / night, housing units, max DV. Column names are matched loosely."""
    t = hcom_totals(path)
    if t.get("error"):
        return ["HCOM table {}: {}".format(t["file"], t["error"])]
    out = ["HCOM table {} ({} rows):".format(t["file"], t["rows"])]
    for key, label, _k, _e in _HCOM_FIELDS:
        if key in t:
            out.append("  {}: {:,.0f} (column '{}')".format(label, t[key],
                                                           t["columns"][key]))
    if "max_dv" in t:
        out.append("  max DV: {:.1f} (column '{}', units as in the table)".format(
            t["max_dv"], t["columns"]["max_dv"]))
    if len(out) == 1:
        out.append("  columns: " + ", ".join(t["header"][:20]))
    return out


def discharge_summary(path):
    """Peak of each discharge column of a DSS-WISE observation-line CSV."""
    try:
        header, rows = _read_table(path)
    except Exception as e:
        return ["Discharge CSV {}: not readable ({})".format(os.path.basename(path), e)]
    if not header or not rows:
        return ["Discharge CSV {}: empty".format(os.path.basename(path))]
    tcol = _col(header, "time")
    out = ["Discharge hydrographs {} ({} rows):".format(os.path.basename(path), len(rows))]
    for i, h in enumerate(header):
        if i == tcol:
            continue
        vals = [(_fnum(r[i]), r[tcol] if tcol is not None and tcol < len(r) else "")
                for r in rows if i < len(r)]
        vals = [v for v in vals if v[0] is not None]
        if not vals:
            continue
        peak, when = max(vals, key=lambda t: t[0])
        out.append("  {}: peak {:,.0f}{}".format(h, peak, " at {} {}".format(
            header[tcol], when) if tcol is not None else ""))
    return out


# v2.4.1: operating-system metadata that is never part of a DSS-WISE package
# (macOS resource forks / Finder files, Windows thumbnails). Only these exact
# names are skipped - an ordinary file starting with "__" or "." is kept.
_JUNK_DIRS = {"__macosx", ".trashes", ".spotlight-v100", ".fseventsd"}
_JUNK_FILES = {".ds_store", "thumbs.db", "desktop.ini", "ehthumbs.db"}


def is_junk_path(name):
    """True for macOS / Windows metadata: '._*' resource forks, .DS_Store,
    Thumbs.db, desktop.ini, or anything inside __MACOSX / .Trashes."""
    if not name:
        return False
    parts = [p for p in name.replace("\\", "/").split("/") if p and p not in (".", "..")]
    if not parts:
        return False
    if any(p.lower() in _JUNK_DIRS for p in parts[:-1]):
        return True
    last = parts[-1]
    return last.startswith("._") or last.lower() in _JUNK_FILES or last.lower() in _JUNK_DIRS
