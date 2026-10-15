# -*- coding: utf-8 -*-
"""1.10.0: the offline pack as a folder with one GeoPackage per layer (pack format 2).

    ND_Dam_Suite_Pack/
      manifest.json            what is in it: every data set, its file, source, date, count
      LAYERS.txt               the same as plain text: each layer, its file and its source
      structures/NSI_structures.gpkg       one layer per file, named like the data set,
      roads/ND_roads_NG911.gpkg            EPSG:4326 (lon / lat), with a spatial index
      ...                                  (sub-folders by theme, see GROUPS)
      landcover/NLCD_2021_landcover.gpkg   rasters too: a GeoPackage raster (manifest "rasters")

Every file opens in QGIS by itself (drag it in), and the plugins read the same files: a query
(GpkgPackReader.query) returns the features of a data set that touch a box as GeoJSON-like dicts,
exactly what the format-1 pack (one SQLite file of zlib-compressed GeoJSON, offline_pack) returns,
so the code that reads the pack does not change. offline_pack.open_pack() picks the reader from
the manifest.

Lossless round trip of the properties: a field's column type comes from all its values
(int / real / text; bool and mixed or nested values are stored as text and turned back on
reading - manifest "fields" = [[column, property name, kind]]); a property whose name a
GeoPackage cannot take as it is (fid, geom, a second "Name" next to "NAME") gets another column
name and keeps its own name on reading. Z values are not kept (2D, like every plugin reads them).

Writing is pure Python (suitegpkg.GpkgWriter with the R*Tree index); adding a file that is not
a pack data set (a DWR export, a HIFLD shapefile) needs GDAL / OGR (osgeo, inside QGIS).
"""
import datetime
import json
import os
import shutil
import sqlite3
import time

from . import suitegpkg as G

FORMAT_VERSION = 2
LAYOUT = "gpkg"
LAYERS_TXT = "LAYERS.txt"
MANIFEST_NAME = "manifest.json"
PACK_FOLDER = "ND_Dam_Suite_Pack"

# sub-folder of a data set: first prefix that matches (anything else: other/)
GROUPS = (
    ("structures", ("NSI_structures", "USA_Structures", "NDGISHUB_address_points",
                    "OSM_places", "OSM_campgrounds", "OSM_trailer_parks")),
    ("roads", ("ND_roads_NG911", "ND_county_roads", "HPMS_", "NDDOT_traffic_counts",
               "NBI_bridges", "OSM_crossings")),
    ("railroads", ("NARN_rail_lines", "FRA_grade_crossings")),
    ("lifelines", ("lifeline_", "OSM_lifelines")),
    ("hydro", ("3DHP_", "NHD_", "NID_dams")),
    ("barriers", ("SARP_",)),
    ("dwr", ("DWR_",)),
    ("hifld", ("HIFLD_",)),
    ("boundaries", ("ND_counties", "FEMA_flood_zones")),
)
RASTER_GROUP = "landcover"


class GpkgPackError(Exception):
    pass


def group_of(name):
    for group, prefixes in GROUPS:
        if any(name.startswith(p) for p in prefixes):
            return group
    return "other"


def _op():
    from . import offline_pack as OP          # late: offline_pack imports this module late too
    return OP


def is_gpkg_manifest(m):
    """True for the manifest of a format-2 pack."""
    return isinstance(m, dict) and m.get("format") == _op().PACK_FORMAT and (
        m.get("layout") == LAYOUT or int(m.get("format_version") or 0) >= FORMAT_VERSION)


def read_manifest(folder):
    try:
        with open(os.path.join(folder, MANIFEST_NAME), encoding="utf-8") as fh:
            m = json.load(fh)
        return m if isinstance(m, dict) else None
    except (OSError, ValueError):
        return None


def is_gpkg_pack(path):
    """True when `path` is a format-2 pack folder (or a file in one)."""
    if not path:
        return False
    folder = path if os.path.isdir(path) else os.path.dirname(path)
    return is_gpkg_manifest(read_manifest(folder))


def pack_folder_of(path):
    """The pack folder of a path that is the folder or a file in it ("" otherwise)."""
    if not path:
        return ""
    folder = path if os.path.isdir(path) else os.path.dirname(path)
    return folder if is_gpkg_manifest(read_manifest(folder)) else ""


# ---------------------------------------------------------------- fields --
def _kind(v):
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, int):
        return "int" if -2 ** 63 <= v < 2 ** 63 else "json"
    if isinstance(v, float):
        return "real"
    if isinstance(v, str):
        return "text"
    return "json"


def _merge(a, b):
    if a is None or a == b:
        return b
    if {a, b} == {"int", "real"}:
        return "real"
    return "json"


_COLTYPE = {"int": "int", "bool": "int", "real": "real", "text": "text", "json": "text",
            None: "text"}


def plan_fields(features):
    """[[column, property name, kind]] for the properties of the features (first-seen order)."""
    order, kinds = [], {}
    for f in features:
        for k, v in ((f or {}).get("properties") or {}).items():
            if k not in kinds:
                order.append(k)
                kinds[k] = None
            if v is not None:
                kinds[k] = _merge(kinds[k], _kind(v))
    used = {"fid", "geom"}
    out = []
    for k in order:
        col = str(k) if str(k).strip() else "field"
        base, n = col, 1
        while col.lower() in used:
            n += 1
            col = "{}_{}".format(base, n)
        used.add(col.lower())
        out.append([col, k, kinds[k] or "text"])
    return out


def plan_geometry(types):
    """GeoPackage geometry type for a set of GeoJSON geometry types."""
    fam = {"Point": "POINT", "MultiPoint": "POINT", "LineString": "LINESTRING",
           "MultiLineString": "LINESTRING", "Polygon": "POLYGON", "MultiPolygon": "POLYGON"}
    types = {t for t in types if t}
    fams = {fam.get(t) for t in types}
    if not types:
        return "GEOMETRY"
    if None in fams or len(fams) != 1:
        return "GEOMETRY"
    f = fams.pop()
    return "MULTI" + f if any(t.startswith("Multi") for t in types) else f


def _store(v, kind):
    if v is None:
        return None
    if kind == "json":
        return json.dumps(v, separators=(",", ":"), ensure_ascii=False)
    if kind == "bool":
        return 1 if v else 0
    return v


def _load(v, kind):
    if v is None:
        return None
    if kind == "json":
        try:
            return json.loads(v)
        except (TypeError, ValueError):
            return v
    if kind == "bool":
        return bool(v)
    if kind == "real" and isinstance(v, int):
        return float(v)
    return v


def _has_points(geom):
    return bool(G.points_of(geom)) if geom else False


# --------------------------------------------------------------- writing --
def write_layer(path, name, features, description="", fields=None, geometry=None):
    """One data set to its own GeoPackage (layer `name`, EPSG:4326, R*Tree). `features` is a
    function returning a fresh iterable of GeoJSON-like features (it is read twice: once to
    plan the fields, once to write) unless fields and geometry are given. Returns
    {"count", "fields", "geometry", "bbox", "file_size"}."""
    if fields is None or geometry is None:
        types = set()

        def seen():
            for f in features():
                g = (f or {}).get("geometry")
                if g:
                    types.add(g.get("type"))
                yield f
        fields = plan_fields(seen()) if fields is None else fields
        if geometry is None:
            if not types:                          # fields were given: still read the types
                for f in features():
                    g = (f or {}).get("geometry")
                    if g:
                        types.add(g.get("type"))
            geometry = plan_geometry(types)
    w = G.GpkgWriter(path)
    try:
        lw = w.layer(name, geometry, [(c, _COLTYPE[k]) for c, _n, k in fields], G.EPSG_4326,
                     description=description, spatial_index=True, raw=True)
        names = [(c, n, k) for c, n, k in fields]
        for f in features():
            props = (f or {}).get("properties") or {}
            g = (f or {}).get("geometry")
            if g is not None and not _has_points(g):
                g = None
            lw.add(g, {c: _store(props.get(n), k) for c, n, k in names})
        count, box = lw.count, lw.box
        w.close()
    except Exception:
        w.abort()
        raise
    return {"count": count, "fields": fields, "geometry": geometry,
            "bbox": [round(v, 7) for v in box] if box else None,
            "file_size": os.path.getsize(path)}


def _v1_features(db, name):
    """A function giving the features of data set `name` of a format-1 pack database, in
    fid order (no box test: the whole data set)."""
    OP = _op()
    import zlib

    def it():
        con = sqlite3.connect(OP.sqlite_ro_uri(_abs(db)), uri=True)
        try:
            for (blob,) in con.execute("SELECT feat FROM {} ORDER BY fid".format(OP._table(name))):
                yield json.loads(zlib.decompress(blob).decode("utf-8"))
        finally:
            con.close()
    return it


def _abs(p):
    import pathlib
    return pathlib.Path(os.path.abspath(p))


def new_manifest(bbox, area="North Dakota", created_by="", started=None):
    OP = _op()
    return {"format": OP.PACK_FORMAT, "format_version": FORMAT_VERSION, "layout": LAYOUT,
            "area": area, "bbox": list(bbox), "started": started or _now(), "created": _now(),
            "created_by": created_by, "datasets": {}, "rasters": {}, "left_out": {}}


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def load_or_new(folder, bbox, **kw):
    m = read_manifest(folder) if os.path.isdir(folder) else None
    if is_gpkg_manifest(m):
        m.setdefault("datasets", {})
        m.setdefault("rasters", {})
        m.setdefault("left_out", {})
        return m
    return new_manifest(bbox, **kw)


def write_manifest(folder, manifest):
    os.makedirs(folder, exist_ok=True)
    manifest["created"] = _now()
    p = os.path.join(folder, MANIFEST_NAME)
    with open(p + ".tmp", "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=1, sort_keys=True)
    os.replace(p + ".tmp", p)
    write_layers_txt(folder, manifest)
    return p


def add_dataset(folder, manifest, name, features, info, log=None):
    """Write data set `name` (features: see write_layer) into the pack folder and record it in
    the manifest (info: label, url, fetched, status, note, gaps, source ... kept as given).
    The manifest is not saved here (write_manifest)."""
    log = log or (lambda m: None)
    rel = "{}/{}.gpkg".format(group_of(name), name)
    path = os.path.join(folder, *rel.split("/"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    t0 = time.time()
    res = write_layer(path, name, features, description=info.get("label") or "")
    e = dict(info)
    e.update({"file": rel, "layer": name, "count": res["count"], "geometry": res["geometry"],
              "fields": res["fields"], "bbox": res["bbox"], "file_size": res["file_size"],
              "group": group_of(name)})
    e.setdefault("status", "complete")
    manifest["datasets"][name] = e
    manifest.get("left_out", {}).pop(name, None)
    log("  {}: {:,} features -> {} ({:.1f} MB, {:.0f} s)".format(
        name, res["count"], rel, res["file_size"] / 1048576.0, time.time() - t0))
    return e


def convert_v1(src, folder, only=None, skip=(), extra_info=None, log=None, progress=None,
               cancel=None):
    """Write every usable data set of a format-1 pack (its .sqlite, folder or .zip) into the
    pack folder `folder` (format 2), keeping what the folder's manifest already lists (data
    sets added from files, rasters). Data sets that are not usable (failed / not available)
    are listed in the manifest with their status, so LAYERS.txt says what is missing.
    extra_info: {name: {key: value}} merged into a data set's manifest entry (a better source
    note). Returns the manifest (saved)."""
    OP = _op()
    log = log or (lambda m: None)
    progress = progress or (lambda p, m="": None)
    src = os.path.abspath(src)
    if src.lower().endswith(".zip"):
        db = OP._unzip(src, log)
    elif os.path.isdir(src):
        db = os.path.join(src, OP.DB_NAME)
    else:
        db = src
    reader = OP.PackReader(db)
    con = sqlite3.connect(OP.sqlite_ro_uri(_abs(db)), uri=True)
    try:
        rows = con.execute("SELECT name, label, url, fetched, count, status, note FROM "
                           "datasets ORDER BY name").fetchall()
        meta = dict(con.execute("SELECT key, value FROM meta").fetchall())
    finally:
        con.close()
    manifest = load_or_new(folder, reader.bbox, area=meta.get("area") or "North Dakota",
                           created_by=meta.get("created_by") or "",
                           started=meta.get("started"))
    manifest["source_pack_created"] = meta.get("created") or ""
    names = [r[0] for r in rows if (only is None or r[0] in only) and r[0] not in skip]
    for i, row in enumerate(r for r in rows if r[0] in names):
        name, label, url, fetched, count, status, note = row
        if cancel and cancel():
            break
        progress(100.0 * i / max(1, len(names)), name)
        info = {"label": label or name, "url": url or "", "fetched": fetched or "",
                "status": status, "note": note or ""}
        rinfo = reader.info(name)
        if rinfo and rinfo.get("gaps"):
            info["gaps"] = [list(g) for g in rinfo["gaps"]]
        info.update((extra_info or {}).get(name) or {})
        if status not in OP.USABLE_STATUS:
            manifest["datasets"].pop(name, None)
            manifest.setdefault("left_out", {})[name] = "{} in the source pack{}".format(
                status or "not built", ": " + note if note else "")
            continue
        add_dataset(folder, manifest, name, _v1_features(db, name), info, log)
        write_manifest(folder, manifest)          # a stopped conversion keeps what is done
    progress(100.0, "done")
    write_manifest(folder, manifest)
    return manifest


def add_vector_file(folder, manifest, name, src_path, info, layer=None, clip_bbox=None,
                    log=None, transform=None):
    """A vector file (any GDAL format; a DWR export, a HIFLD shapefile) as data set `name` of
    the pack: reprojected to EPSG:4326, only the features touching clip_bbox (lon / lat; the
    pack's box when None). Needs GDAL / OGR. 1.19.0: the layer is read through a spatial filter
    (a national file such as the 3DHP download has millions of features), and transform(features)
    -> features, when given, is applied to each feature batch (3DHP: the NHD-style properties)."""
    from osgeo import ogr, osr                                   # noqa: E402 (QGIS / GDAL)
    clip_bbox = clip_bbox or manifest.get("bbox")
    ds = ogr.Open(src_path)
    if ds is None:
        raise GpkgPackError("{} could not be opened".format(src_path))
    lyr = ds.GetLayerByName(layer) if layer else ds.GetLayer(0)
    if lyr is None:
        raise GpkgPackError("{}: no layer {}".format(src_path, layer or "0"))
    dst = osr.SpatialReference()
    dst.ImportFromEPSG(4326)
    try:
        dst.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    except AttributeError:
        pass
    srs = lyr.GetSpatialRef()
    tr = None
    if srs is not None:
        try:
            srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        except AttributeError:
            pass
        if not srs.IsSame(dst):
            tr = osr.CoordinateTransformation(srs, dst)
    defn = lyr.GetLayerDefn()
    fnames = [defn.GetFieldDefn(i).GetName() for i in range(defn.GetFieldCount())]
    OP = _op()
    if clip_bbox:
        _spatial_filter(lyr, clip_bbox, srs, dst, osr)
    one = (lambda f: transform([f])) if transform else (lambda f: [f])

    def it():
        lyr.ResetReading()
        for feat in lyr:
            g = feat.GetGeometryRef()
            geom = None
            if g is not None and not g.IsEmpty():
                g = g.Clone()
                g.FlattenTo2D()
                if tr is not None:
                    g.Transform(tr)
                txt = g.ExportToJson()
                geom = json.loads(txt) if txt else None
                if geom is None or (clip_bbox and not OP.intersects_rect(geom, clip_bbox)):
                    continue
            elif clip_bbox:
                continue                                         # no place: not in the area
            props = {}
            for i, n in enumerate(fnames):                       # by index: by name is slow
                v = feat.GetField(i)
                if isinstance(v, (datetime.date, datetime.datetime)):
                    v = v.isoformat()
                props[n] = v
            for out in one({"type": "Feature", "geometry": geom, "properties": props}):
                yield out
    e = dict(info)
    e.setdefault("url", src_path)
    real = src_path[len("/vsizip/"):] if src_path.startswith("/vsizip/") else src_path
    try:
        e.setdefault("fetched", datetime.date.fromtimestamp(os.path.getmtime(real)).isoformat())
    except OSError:
        e.setdefault("fetched", datetime.date.today().isoformat())
    return add_dataset(folder, manifest, name, it, e, log)


def _spatial_filter(lyr, bbox, srs, dst, osr):
    """Only the features near bbox (lon / lat) are read: the box's outline, densified, in the layer's
    own system (a projected national file), widened a little. Nothing is set when the layer has
    no system."""
    if srs is None:
        return
    x0, y0, x1, y1 = bbox
    n = 24
    pts = [(x0 + (x1 - x0) * i / n, y) for i in range(n + 1) for y in (y0, y1)] + \
          [(x, y0 + (y1 - y0) * j / n) for j in range(n + 1) for x in (x0, x1)]
    back = osr.CoordinateTransformation(dst, srs)
    xs, ys = [], []
    for x, y in pts:
        try:
            px, py = back.TransformPoint(x, y)[:2]
        except Exception:                                        # noqa - outside the system
            continue
        xs.append(px)
        ys.append(py)
    if xs:
        pad = 0.001 * max(max(xs) - min(xs), max(ys) - min(ys)) if srs.IsProjected() else 0.001
        lyr.SetSpatialFilterRect(min(xs) - pad, min(ys) - pad, max(xs) + pad, max(ys) + pad)


def add_raster_file(folder, manifest, name, src_path, info, log=None):
    """A raster written into the pack's landcover/ (or info["group"]) and recorded in
    manifest["rasters"]. 1.10.0: a GeoTIFF becomes a GeoPackage raster (lossless PNG tiles, same
    grid and values) when GDAL is at hand, like the vector layers; else the file is copied."""
    log = log or (lambda m: None)
    group = info.get("group") or RASTER_GROUP
    ext = os.path.splitext(src_path)[1].lower() or ".tif"
    as_gpkg = ext in (".tif", ".tiff") and _gdal() is not None
    rel = "{}/{}{}".format(group, name, ".gpkg" if as_gpkg else ext)
    dst = os.path.join(folder, *rel.split("/"))
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if as_gpkg:
        _raster_to_gpkg(src_path, dst, name)
    else:
        shutil.copyfile(src_path, dst)
    e = dict(info)
    e.update({"file": rel, "file_size": os.path.getsize(dst), "group": group})
    e.setdefault("status", "complete")
    manifest.setdefault("rasters", {})[name] = e
    log("  {}: raster -> {} ({:.1f} MB)".format(name, rel, e["file_size"] / 1048576.0))
    return e


def _gdal():
    try:
        from osgeo import gdal
    except Exception:                                            # noqa - no GDAL here
        return None
    return gdal if hasattr(gdal, "Translate") and hasattr(gdal, "Open") else None


def _raster_to_gpkg(src_path, dst, name):
    """GeoTIFF -> one-raster GeoPackage (table `name`, PNG tiles: lossless), then a check that
    the grid and every value are the same."""
    gdal = _gdal()
    for p in (dst, dst + "-journal"):
        if os.path.exists(p):
            os.remove(p)
    tmp = dst + ".part.gpkg"
    if os.path.exists(tmp):
        os.remove(tmp)
    out = gdal.Translate(tmp, src_path, format="GPKG",
                         creationOptions=["TILE_FORMAT=PNG", "RASTER_TABLE=" + name,
                                          "RASTER_IDENTIFIER=" + name])
    if out is None:
        raise RuntimeError("could not write {} as a GeoPackage raster".format(src_path))
    out = None
    a, b = gdal.Open(src_path), gdal.Open(tmp)
    try:
        same = (a.RasterXSize, a.RasterYSize, a.GetGeoTransform()) == \
            (b.RasterXSize, b.RasterYSize, b.GetGeoTransform())
        same = same and all(ba.Checksum() == bb.Checksum() for ba, bb in
                            ((a.GetRasterBand(i), b.GetRasterBand(i))
                             for i in range(1, a.RasterCount + 1)))
    finally:
        a = b = None
    if not same:
        os.remove(tmp)
        raise RuntimeError("the GeoPackage copy of {} differs from it".format(src_path))
    os.replace(tmp, dst)


def remove_dataset(folder, manifest, name, reason=""):
    """Take a data set out of the pack (file deleted; listed under left_out with the reason)."""
    e = manifest.get("datasets", {}).pop(name, None) or manifest.get("rasters", {}).pop(name, None)
    if e and e.get("file"):
        p = os.path.join(folder, *e["file"].split("/"))
        if os.path.isfile(p):
            os.remove(p)
    if reason:
        manifest.setdefault("left_out", {})[name] = reason
    return e


# ------------------------------------------------------------ LAYERS.txt --
def _mb(n):
    return "{:.1f} MB".format((n or 0) / 1048576.0)


def layers_text(manifest):
    """LAYERS.txt: each layer, its file, count, date and source."""
    ds = manifest.get("datasets") or {}
    rs = manifest.get("rasters") or {}
    lines = [
        "ND Dam Suite - offline data pack: layers and sources",
        "",
        "Pack built {} for {} (lon / lat box {}).".format(
            (manifest.get("created") or "")[:19].replace("T", " "), manifest.get("area") or "?",
            ", ".join("{:.4f}".format(v) for v in manifest.get("bbox") or [])),
        "Format 2: one GeoPackage per layer (each file holds one layer named like the file),",
        "EPSG:4326 longitude / latitude, with a spatial index. Open any file in QGIS directly.",
        "The three plugins read the pack when Data source = Offline pack (Plugins > ND Dam Suite >",
        "Suite data and servers, or ND Dam Hazard's Data & Output tab).",
        "",
    ]
    total = sum(int(e.get("file_size") or 0) for e in list(ds.values()) + list(rs.values()))
    lines.append("{} vector layers, {} raster(s), {} in all.".format(len(ds), len(rs), _mb(total)))
    lines.append("")
    by_group = {}
    for n, e in ds.items():
        by_group.setdefault(e.get("group") or group_of(n), []).append((n, e, "vector"))
    for n, e in rs.items():
        by_group.setdefault(e.get("group") or RASTER_GROUP, []).append((n, e, "raster"))
    order = [g for g, _p in GROUPS] + [RASTER_GROUP, "other"]
    for g in sorted(by_group, key=lambda x: (order.index(x) if x in order else 99, x)):
        lines.append("=" * 78)
        lines.append(g.upper())
        lines.append("=" * 78)
        for n, e, kind in sorted(by_group[g]):
            lines.append("")
            if kind == "vector":
                lines.append("{}  ({:,} features, {}, {})".format(
                    e.get("file"), int(e.get("count") or 0), (e.get("geometry") or "").lower(),
                    _mb(e.get("file_size"))))
            else:
                lines.append("{}  (raster, {})".format(e.get("file"), _mb(e.get("file_size"))))
            lines.append("  Layer:   {}".format(n))
            lines.append("  What:    {}".format(e.get("label") or n))
            src = e.get("source") or ""
            if src:
                lines.append("  Source:  {}".format(src))
            if e.get("url"):
                lines.append("  Address: {}".format(e["url"]))
            if e.get("fetched"):
                lines.append("  Fetched: {}".format(str(e["fetched"])[:10]))
            if e.get("status") and e.get("status") != "complete":
                lines.append("  Status:  {} (used with a warning)".format(e["status"]))
            if e.get("note"):
                lines.append("  Note:    {}".format(e["note"]))
            if e.get("used_by"):
                lines.append("  Used by: {}".format(e["used_by"]))
    lo = manifest.get("left_out") or {}
    if lo:
        lines += ["", "=" * 78, "NOT IN THE PACK", "=" * 78, ""]
        for n in sorted(lo):
            lines.append("{}: {}".format(n, lo[n]))
    lines.append("")
    return "\n".join(lines)


def write_layers_txt(folder, manifest):
    p = os.path.join(folder, LAYERS_TXT)
    with open(p, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(layers_text(manifest))
    return p


# ---------------------------------------------------------------- reading --
class GpkgPackReader(object):
    """Read access to a format-2 pack folder, with the methods of offline_pack.PackReader
    (has, info, covers, query, created, datasets, bbox, meta). Thread-safe: one read-only
    SQLite connection per query."""
    layout = LAYOUT

    def __init__(self, folder):
        OP = _op()
        m = read_manifest(folder)
        if not is_gpkg_manifest(m):
            raise OP.PackError("{} is not an offline data pack folder (no manifest.json of "
                               "format 2)".format(folder))
        if int(m.get("format_version") or 0) > FORMAT_VERSION:
            raise OP.PackError("the offline data pack is newer than this plugin (format {}) - "
                               "update the plugin".format(m.get("format_version")))
        self.folder = os.path.abspath(folder)
        self.db_path = os.path.join(self.folder, MANIFEST_NAME)     # (open_pack's cache check)
        self.manifest = m
        self.meta = {k: v for k, v in m.items() if k not in ("datasets", "rasters", "left_out")}
        self.bbox = tuple(m.get("bbox") or OP.ND_BBOX)
        self.datasets = {}
        for name, e in (m.get("datasets") or {}).items():
            if e.get("status") not in OP.USABLE_STATUS or not e.get("file"):
                continue
            p = os.path.join(self.folder, *e["file"].split("/"))
            if not os.path.isfile(p):
                continue
            d = dict(e)
            d["path"] = p
            d["gaps"] = [tuple(float(v) for v in g) for g in (e.get("gaps") or [])]
            self.datasets[name] = d
        self.rasters = {}
        for name, e in (m.get("rasters") or {}).items():
            p = os.path.join(self.folder, *(e.get("file") or "").split("/"))
            if e.get("file") and os.path.isfile(p):
                d = dict(e)
                d["path"] = p
                self.rasters[name] = d
        self._shape = {}

    @property
    def created(self):
        return (self.meta.get("created") or self.meta.get("started") or "")[:10]

    def _real(self, name):
        """1.19.0: a data set answers to its old or new name (NHD_flowlines = 3DHP_flowlines)."""
        from . import hydro3dhp as H
        return H.real_name(name, self.datasets)

    def has(self, name):
        return self._real(name) in self.datasets

    def info(self, name):
        return self.datasets.get(self._real(name))

    def path(self, name):
        """The GeoPackage file of data set `name` ("" when the pack has none)."""
        e = self.datasets.get(self._real(name)) or self.rasters.get(name)
        return e["path"] if e else ""

    def uri(self, name):
        """QGIS / OGR source of the data set: "<file>|layername=<layer>"."""
        name = self._real(name)
        e = self.datasets.get(name)
        return "{}|layername={}".format(e["path"], e.get("layer") or name) if e else ""

    def covers(self, extent, name=None):
        OP = _op()
        if not OP.covers(self.bbox, extent):
            return False
        if name is None:
            return True
        info = self.datasets.get(self._real(name))
        if info is None or info.get("status") != OP.STATUS_COMPLETE:
            return False
        return not any(OP.boxes_touch(g, extent) for g in info.get("gaps") or ())

    def index_kind(self, name):
        return "rtree" if (self._layout(self._real(name)) or {}).get("rtree") else "scan"

    def _connect(self, path):
        OP = _op()
        if not os.path.isfile(path):
            raise OP.PackError("offline data pack file not found: {}".format(path))
        con = sqlite3.connect(OP.sqlite_ro_uri(_abs(path)), uri=True)
        con.execute("PRAGMA query_only = ON")
        return con

    def _layout(self, name):
        """{"table", "geom", "cols", "props", "kinds", "rtree"} of a data set's file (cached)."""
        hit = self._shape.get(name)
        if hit is not None:
            return hit
        e = self.datasets.get(name)
        if e is None:
            return None
        table = e.get("layer") or name
        con = self._connect(e["path"])
        try:
            row = con.execute("SELECT column_name FROM gpkg_geometry_columns WHERE "
                              "table_name = ?", (table,)).fetchone()
            gcol = row[0] if row else "geom"
            cols = [r[1] for r in con.execute('PRAGMA table_info("{}")'.format(
                table.replace('"', '""')))]
            rt = "rtree_{}_{}".format(table, gcol)
            has_rt = con.execute("SELECT 1 FROM sqlite_master WHERE name = ?",
                                 (rt,)).fetchone() is not None
        finally:
            con.close()
        spec = {c: (n, k) for c, n, k in (e.get("fields") or [])}
        pcols = [c for c in cols if c not in ("fid", gcol)]
        out = {"table": table, "geom": gcol, "cols": pcols,
               "props": [spec.get(c, (c, None))[0] for c in pcols],
               "kinds": [spec.get(c, (c, None))[1] for c in pcols],
               "rtree": rt if has_rt else None}
        self._shape[name] = out
        return out

    def query(self, name, extent):
        """GeoJSON features of data set `name` that intersect the extent (lon / lat), in the
        order they were written - what offline_pack.PackReader.query returns."""
        OP = _op()
        name = self._real(name)
        e = self.datasets.get(name)
        if e is None:
            raise OP.PackError("{} is not in the offline data pack".format(name))
        lay = self._layout(name)
        rect = tuple(float(v) for v in extent)
        q = lambda s: '"{}"'.format(s.replace('"', '""'))         # noqa: E731
        sel = ", ".join([q(lay["geom"])] + [q(c) for c in lay["cols"]])
        con = self._connect(e["path"])
        try:
            if lay["rtree"]:
                rows = con.execute(
                    "SELECT {} FROM {} WHERE fid IN (SELECT id FROM {} WHERE maxx >= ? AND "
                    "minx <= ? AND maxy >= ? AND miny <= ?) ORDER BY fid".format(
                        sel, q(lay["table"]), q(lay["rtree"])),
                    (rect[0], rect[2], rect[1], rect[3]))
            else:
                rows = con.execute("SELECT {} FROM {} ORDER BY fid".format(sel, q(lay["table"])))
            out = []
            props, kinds = lay["props"], lay["kinds"]
            for row in rows:
                if row[0] is None:
                    continue
                geom = G.parse_blob(row[0], lists=True)[0]
                if not OP.intersects_rect(geom, rect):
                    continue
                out.append({"type": "Feature", "geometry": geom,
                            "properties": {p: _load(v, k) for p, k, v in
                                           zip(props, kinds, row[1:])}})
            return out
        except sqlite3.Error as ex:
            raise OP.PackError("{}: {}".format(e["path"], ex))
        finally:
            con.close()
