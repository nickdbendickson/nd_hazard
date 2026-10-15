# -*- coding: utf-8 -*-
"""Layers of the offline pack that come from FILES, not from a web service (release 1.19).

The ND DWR inventory exports (dam lines, dugouts, dugout polygons, unknown structures) and the HIFLD
layers (fire / EMS stations, emergency operations centers, law enforcement, wastewater treatment
plants) have no public service the builder can read, so they come from files the user drops in:

    <suite data folder>/dwr/     the DWR exports (also dwr/_other/)
    <suite data folder>/hifld/   the HIFLD downloads

(or any folder given to build_nd_pack.build(files=...) / --files). A file is recognised by its name
(KNOWN); the first match is written to the pack as that data set - reprojected to EPSG:4326 and
clipped to the pack's box (damsuite.gpkgpack.add_vector_file, which needs GDAL / OGR: QGIS has it).

Pure Python apart from that call. No network access.
"""
import datetime
import os
import re

try:
    from .damsuite import gpkgpack as GP
    from .damsuite import hydro3dhp as H3
except ImportError:                       # run as a script next to the plugin
    from damsuite import gpkgpack as GP   # noqa
    from damsuite import hydro3dhp as H3  # noqa

EXTS = (".gpkg", ".shp", ".geojson", ".json", ".kml", ".gdb")
MAX_DEPTH = 3

# (pack data set, label, source note, keyword groups, words that rule a file out). A file matches
# when ALL keywords of ANY group are in its name (lower case, every non-letter / digit run turned
# into "_" and the name wrapped in "_": a keyword starting with "_" must begin a word).
KNOWN = (
    ("DWR_dams", "ND DWR dams inventory", "ND State Water Commission / DWR dam inventory export",
     (("_dam_",), ("_dams_",)), ("line", "linear", "embank", "unknown", "dugout")),
    ("DWR_dam_lines", "ND DWR dam lines", "ND DWR dam lines export",
     (("_dam_", "line"), ("_dams_", "line"), ("damline",), ("embank",)), ("unknown", "dugout")),
    ("DWR_dugouts", "ND DWR dugouts", "ND DWR dugout points export",
     (("dugout",),), ("poly",)),
    ("DWR_dugout_polygons", "ND DWR dugout polygons", "ND DWR dugout polygons export",
     (("dugout", "poly"),), ()),
    ("DWR_unknown_structures", "ND DWR unknown structures",
     "ND DWR structures with no dam record (export)", (("unknown",),), ()),
    ("HIFLD_fire_ems_stations", "HIFLD fire and EMS stations",
     "HIFLD (DHS / CISA) Fire and Emergency Medical Service stations",
     (("fire", "station"), ("fire", "_ems")), ()),
    ("HIFLD_local_eoc", "HIFLD local emergency operations centers",
     "HIFLD (DHS / CISA) Local Emergency Operations Centers",
     (("_eoc_",), ("emergency", "operations")), ()),
    ("HIFLD_local_law_enforcement", "HIFLD local law enforcement locations",
     "HIFLD (DHS / CISA) Local Law Enforcement Locations", (("law", "enforcement"),), ()),
    ("HIFLD_wastewater_treatment_plants", "HIFLD wastewater treatment plants",
     "HIFLD (DHS / CISA) Wastewater Treatment Plants", (("wastewater",),), ()),
    # OpenStreetMap layers built from a Geofabrik extract (ND Data Updater 1.2.0's osm_layers folder,
    # nd_osm_download.py). Last, so a name that also fits a HIFLD rule stays with HIFLD; an exact
    # data set name always wins. They are in the builder's download list too (Overpass): a file found
    # here is used instead.
    ("OSM_lifelines", "OpenStreetMap lifeline facilities (Geofabrik extract)",
     "OpenStreetMap via a Geofabrik extract", (("osm", "lifeline"),), ()),
    ("OSM_crossings", "OpenStreetMap culverts / fords / rail bridges (Geofabrik extract)",
     "OpenStreetMap via a Geofabrik extract", (("osm", "crossing"),), ()),
    ("OSM_campgrounds", "OpenStreetMap campgrounds / RV parks (Geofabrik extract)",
     "OpenStreetMap via a Geofabrik extract", (("osm", "campground"),), ()),
    ("OSM_trailer_parks", "OpenStreetMap mobile-home parks (Geofabrik extract)",
     "OpenStreetMap via a Geofabrik extract", (("osm", "trailer", "park"),), ()),
    ("OSM_places", "OpenStreetMap cities / towns / villages (Geofabrik extract)",
     "OpenStreetMap via a Geofabrik extract", (("osm", "place"),), ()),
)
OSM_NAMES = tuple(k[0] for k in KNOWN if k[0].startswith("OSM_"))
NAMES = tuple(k[0] for k in KNOWN if k[0] not in OSM_NAMES)    # the builder's "file" data sets


def _words(s):
    return "_" + re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_") + "_"


def classify(path):
    """The pack data set a file's name says it is, or None. An exact data set name
    (DWR_dams.gpkg) always wins."""
    stem = os.path.splitext(os.path.basename(path.rstrip("/\\")))[0]
    for name, *_r in KNOWN:
        if stem.lower() == name.lower():
            return name
    w = _words(stem)
    for name, _label, _src, groups, bad in KNOWN:
        if any(b in w for b in bad):
            continue
        if any(all(k in w for k in g) for g in groups):
            return name
    return None


def _is_pack(folder):
    return os.path.isfile(os.path.join(folder, "manifest.json")) and \
        os.path.isfile(os.path.join(folder, "LAYERS.txt"))


def scan(folders, depth=MAX_DEPTH):
    """{data set: path} of the layer files in `folders` (and below): the newest file of each data
    set wins. A pack folder (manifest.json + LAYERS.txt) is not entered."""
    found = {}
    for top in folders or ():
        if not top or not os.path.isdir(top):
            continue
        base = os.path.abspath(top).rstrip(os.sep).count(os.sep)
        for root, dirs, files in os.walk(top):
            if _is_pack(root) or os.path.abspath(root).count(os.sep) - base > depth:
                dirs[:] = []
                continue
            names = [(n, os.path.join(root, n)) for n in files + [d for d in dirs if
                                                                   d.lower().endswith(".gdb")]]
            dirs[:] = [d for d in dirs if not d.lower().endswith(".gdb")]
            for n, p in names:
                if os.path.splitext(n)[1].lower() not in EXTS:
                    continue
                name = classify(p)
                if name is None:
                    continue
                try:
                    t = os.path.getmtime(p)
                except OSError:
                    continue
                if name not in found or t > found[name][0]:
                    found[name] = (t, p)
    return {k: v[1] for k, v in found.items()}


def folders_for(data_folder, extra=()):
    """The folders searched: dwr/, hifld/ and osm_layers/ of the suite data folder, plus `extra`."""
    out = []
    for f in list(extra or ()) + [os.path.join(data_folder, "dwr") if data_folder else "",
                                  os.path.join(data_folder, "hifld") if data_folder else "",
                                  os.path.join(data_folder, "osm_layers") if data_folder else ""]:
        if f and f not in out:
            out.append(f)
    return out


def add_files(pack_folder, found, bbox=None, only=None, replace=False, log=None):
    """Write the found layer files into the pack folder. Returns (added {name: count}, problems
    {name: text}). A data set the pack already holds is kept unless the file is newer than the
    pack's copy (or replace). The manifest is saved when anything was written."""
    log = log or (lambda m: None)
    added, problems = {}, {}
    todo = {k: v for k, v in found.items() if not only or k in only}
    if not todo:
        return added, problems
    man = GP.load_or_new(pack_folder, bbox or GP._op().ND_BBOX)
    meta = {k[0]: k for k in KNOWN}
    for name, path in sorted(todo.items()):
        have = (man.get("datasets") or {}).get(name)
        if have and have.get("status") == "complete" and not replace:
            try:
                newer = datetime.date.fromtimestamp(os.path.getmtime(path)).isoformat() > \
                    str(have.get("fetched") or "")[:10]
            except OSError:
                newer = False
            if not newer:
                log("  {}: already in the pack - kept (the file {} is not newer)".format(
                    name, os.path.basename(path)))
                continue
        _n, label, src, _g, _b = meta[name]
        info = {"label": label, "source": "{}; file {}".format(src, os.path.basename(path)),
                "url": os.path.basename(path)}
        try:
            e = GP.add_vector_file(pack_folder, man, name, path, info, clip_bbox=man.get("bbox"),
                                   log=log)
            added[name] = e["count"]
        except ImportError:
            problems[name] = "needs GDAL / OGR (run it inside QGIS)"
        except Exception as ex:                                  # noqa - a bad file: say which
            problems[name] = "{}: {}".format(os.path.basename(path), ex)
    if added:
        GP.write_manifest(pack_folder, man)
    return added, problems


def missing_text(name):
    for n, label, src, *_r in KNOWN:
        if n == name:
            return "no {} file found - put the export in the suite data folder's {} folder " \
                   "({})".format(label, "dwr" if n.startswith("DWR") else
                                 "osm_layers" if n.startswith("OSM_") else "hifld", src)
    return "no file found"


# ------------------------------------------------------ 3DHP national file --
# The 3DHP web service (hydro.nationalmap.gov) often answers HTTP 504 / 502 to a state-sized request.
# The National Map also publishes the whole product as ONE file (CONUS, about 12.5 GB zipped
# FileGDB or 23 GB GeoPackage; https://apps.nationalmap.gov/downloader/ > Hydrography > 3DHP).
# Put that file in the suite data folder's 3dhp/ folder (or give it to --files); the builder
# then cuts the pack's box out of it instead of asking the service. The zip is read in place.
BULK_3DHP = (
    ("3DHP_flowlines", "flowline", "USGS 3DHP flowlines (3D Hydrography Program)"),
    ("3DHP_waterbodies", "waterbody", "USGS 3DHP waterbodies (3D Hydrography Program)"),
)
BULK_EXTS = (".zip", ".gdb", ".gpkg")


def _normaliser(name):
    return H3.normalise_flowlines if name == "3DHP_flowlines" else H3.normalise_waterbodies


def scan_3dhp(folders, depth=MAX_DEPTH):
    """The 3DHP national file in `folders`: the newest .zip / .gdb / .gpkg whose name has
    "3dhp" in it; None when there is none."""
    best = None
    for top in folders or ():
        if not top or not os.path.isdir(top):
            continue
        base = os.path.abspath(top).rstrip(os.sep).count(os.sep)
        for root, dirs, files in os.walk(top):
            if _is_pack(root) or os.path.abspath(root).count(os.sep) - base > depth:
                dirs[:] = []
                continue
            names = list(files) + [d for d in dirs if d.lower().endswith(".gdb")]
            dirs[:] = [d for d in dirs if not d.lower().endswith(".gdb")]
            for n in names:
                if os.path.splitext(n)[1].lower() not in BULK_EXTS or "3dhp" not in n.lower():
                    continue
                p = os.path.join(root, n)
                try:
                    t = os.path.getmtime(p)
                except OSError:
                    continue
                if best is None or t > best[0]:
                    best = (t, p)
    return best[1] if best else None


def folders_3dhp(data_folder, extra=()):
    out = []
    for f in list(extra or ()) + [os.path.join(data_folder, "3dhp") if data_folder else ""]:
        if f and f not in out:
            out.append(f)
    return out


def open_path_3dhp(path):
    """The path GDAL opens: a zip is read in place (/vsizip/)."""
    path = os.path.abspath(path)
    return "/vsizip/" + path if path.lower().endswith(".zip") else path


def _layer_name(path, word):
    from osgeo import ogr                                         # noqa (QGIS / GDAL)
    ds = ogr.Open(open_path_3dhp(path))
    if ds is None:
        raise GP.GpkgPackError("{} could not be opened".format(path))
    for i in range(ds.GetLayerCount()):
        n = ds.GetLayer(i).GetName()
        if word in n.lower():
            return n
    raise GP.GpkgPackError("{}: no {} layer".format(os.path.basename(path), word))


def add_3dhp(pack_folder, path, bbox=None, only=None, replace=False, log=None):
    """The 3DHP flowlines and waterbodies of the pack's box cut out of the national file `path`.
    Returns (added {name: count}, problems {name: text}); a complete data set the pack already has
    is kept unless `replace`."""
    log = log or (lambda m: None)
    added, problems = {}, {}
    man = GP.load_or_new(pack_folder, bbox or GP._op().ND_BBOX)
    for name, word, label in BULK_3DHP:
        if only and name not in only:
            continue
        have = (man.get("datasets") or {}).get(name)
        if have and have.get("status") == "complete" and not replace:
            log("  {}: already in the pack - kept".format(name))
            continue
        info = {"label": label, "url": os.path.basename(path),
                "source": "USGS 3D Hydrography Program national file {}; NHD-style gnis_name / "
                          "gnis_id / ftype / fcode added to the 3DHP properties".format(
                              os.path.basename(path)),
                "used_by": "Hazard (travel time), Breach Runner (culverts), Screening (pool area)"}
        try:
            e = GP.add_vector_file(pack_folder, man, name, open_path_3dhp(path), info,
                                   layer=_layer_name(path, word), clip_bbox=man.get("bbox"),
                                   log=log, transform=_normaliser(name))
            added[name] = e["count"]
        except ImportError:
            problems[name] = "needs GDAL / OGR (run it inside QGIS)"
        except Exception as ex:                                  # noqa - a bad file: say which
            problems[name] = "{}: {}".format(os.path.basename(path), ex)
        else:
            GP.write_manifest(pack_folder, man)
    return added, problems
