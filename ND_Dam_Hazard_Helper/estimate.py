# -*- coding: utf-8 -*-
"""v2.9.0: pre-run estimate - what a classification run will read, download
and write, before it starts.

estimate(depth_path, ...) looks at the depth raster (size, cell size, CRS
units), the study area (raster extent + the extent buffer, as the run
builds it with extent.ExtentClip), the downloads the run would make with
the given download choices (the pipeline cfg download_* keys and the layers
that replace downloads), how many of the NSI / ArcGIS tiles are already in
the per-tile cache and still fresh, and which data sets the offline data
pack answers. It returns a dict; estimate_lines(est) turns it into text for
the dialog or the run log.

Nothing is downloaded, nothing is written, no pack is unpacked (its
manifest.json is read, or the .sqlite read-only) and the tile cache is only
looked at (file names and the first bytes of each tile - never
TileCache.read(), which deletes a damaged tile). estimate() never raises: it
returns what it could compute plus a note.

The TIME and DISK figures are ROUGH. They come from the per-unit cost
constants below, which are ASSUMPTIONS, not measurements: calibrate them
from the TIMINGS section of a run log on your own computer and network
(e.g. "Downloading N data set(s)..." seconds / requests made). The counts
(cells, tiles, requests, data sets) follow the plugin's own rules.
"""

import gzip
import math
import os
import re
import time

# ---------------------------------------------------------------------------
# ROUGH ASSUMPTIONS (not measured) - (fast, slow) ranges. Calibrate them from
# the TIMINGS section of a run log: the stage named in brackets.
# ---------------------------------------------------------------------------
SEC_PER_HTTP_REQUEST = (1.0, 8.0)       # one NSI / ArcGIS request ("Downloading ...")
SEC_PER_OVERPASS_REQUEST = (5.0, 90.0)  # one OpenStreetMap Overpass query ("Downloading ...")
SEC_PER_PACK_QUERY = (0.2, 3.0)         # one data set read from the offline pack
SEC_PER_MCELL_READ = (0.5, 3.0)         # one raster, per million cells ("Finding the
                                        # inundated area...", "Classifying...")
SEC_PER_MCELL_PRODUCTS = (1.0, 6.0)     # per million depth cells ("ACER zone rasters...",
                                        # EAP arrival bands, "Writing outputs...")
SEC_FIXED = (20.0, 120.0)               # structure inventory, rules, report, map book
MB_PER_HTTP_REQUEST = (0.02, 1.0)       # disk per downloaded tile / page (tile cache,
                                        # outputs/_downloads)
MB_OUTPUT_PER_MCELL = (1.0, 8.0)        # rasters the run writes, per million depth cells
MB_OUTPUT_FIXED = (5.0, 60.0)           # GeoPackages, tables, PDF report, map book

# screening thresholds for the warnings (the engineer's call, not rules)
WARN_STUDY_AREA_SQ_MI = 500.0
WARN_RASTER_CELLS = 150e6
WARN_RASTER_MB = 2048.0
WARN_TILES = 150                         # NSI / ArcGIS tiles of one data set

DEFAULT_MAX_AGE_DAYS = 7.0               # pipeline DEFAULTS["data_max_age_days"]
_SQ_FT_PER_SQ_MI = 5280.0 ** 2

ROUGH_NOTE = ("Times and disk use are ROUGH: they come from assumed per-unit costs (constants "
              "at the top of estimate.py), not from measurements - calibrate them with the "
              "TIMINGS section of a run log. Counts of cells, tiles and requests follow the "
              "plugin's own rules.")


def _log_none(_m):
    pass


# ---------------------------------------------------------------- rasters --
def _gdal():
    from osgeo import gdal
    return gdal


def _bytes_per_cell(gdal, dtype):
    table = {}
    for name, size in (("GDT_Byte", 1), ("GDT_Int8", 1), ("GDT_UInt16", 2), ("GDT_Int16", 2),
                       ("GDT_UInt32", 4), ("GDT_Int32", 4), ("GDT_Float32", 4),
                       ("GDT_Float64", 8), ("GDT_UInt64", 8), ("GDT_Int64", 8)):
        v = getattr(gdal, name, None)
        if v is not None:
            table.setdefault(v, size)
    return table.get(dtype, 4)


def _file_mb(path):
    """Size on disk of a raster file (+ its .aux.xml / .ovr / world file)."""
    total = 0
    for p in (path, path + ".aux.xml", path + ".ovr"):
        try:
            if os.path.isfile(p):
                total += os.path.getsize(p)
        except OSError:
            pass
    return total / 1048576.0


def _units_name(crs):
    from .compat import DIST_FEET, DIST_METERS, DIST_DEGREES, enum_value
    try:
        u = enum_value(crs.mapUnits())
    except Exception:
        return "unknown"
    for val, name in ((DIST_FEET, "feet"), (DIST_METERS, "meters"), (DIST_DEGREES, "degrees")):
        try:
            if u == enum_value(val):
                return name
        except Exception:
            continue
    try:
        from qgis.core import QgsUnitTypes
        return str(QgsUnitTypes.toString(crs.mapUnits())) or "unknown"
    except Exception:
        return "unknown"


def raster_info(path):
    """{path, cols, rows, cells, bands, bytes_per_cell, mb_on_disk, mb_in_memory,
    geotransform, cell_x, cell_y, projection} of a raster (GDAL, read-only), or
    {"path", "error"}."""
    info = {"path": path}
    if not path:
        info["error"] = "no raster given"
        return info
    if not os.path.exists(path):
        info["error"] = "not found"
        return info
    try:
        gdal = _gdal()
        prev = gdal.GetUseExceptions()
        try:
            gdal.DontUseExceptions()
            ds = gdal.Open(path, gdal.GA_ReadOnly)
        finally:
            if prev:
                gdal.UseExceptions()
        if ds is None:
            info["error"] = ("a HEC-RAS plan - converted to rasters when the run starts"
                             if path.lower().endswith(".hdf") else "GDAL cannot open it")
            return info
        cols, rows = int(ds.RasterXSize), int(ds.RasterYSize)
        gt = tuple(ds.GetGeoTransform() or (0.0, 1.0, 0.0, 0.0, 0.0, -1.0))
        band = ds.GetRasterBand(1)
        bpc = _bytes_per_cell(gdal, band.DataType if band is not None else None)
        info.update(cols=cols, rows=rows, cells=cols * rows, bands=int(ds.RasterCount or 1),
                    bytes_per_cell=bpc, mb_on_disk=_file_mb(path),
                    mb_in_memory=cols * rows * bpc / 1048576.0, geotransform=gt,
                    cell_x=abs(gt[1]), cell_y=abs(gt[5]), projection=ds.GetProjection() or "")
        band = ds = None
    except Exception as e:                      # noqa - never raises
        info["error"] = str(e) or e.__class__.__name__
    return info


def same_grid(a, b, tol=1e-6):
    """True when two raster_info dicts describe the same grid (size, cell,
    origin, CRS text)."""
    try:
        if a.get("error") or b.get("error"):
            return False
        if (a["cols"], a["rows"]) != (b["cols"], b["rows"]):
            return False
        cell = max(abs(a["geotransform"][1]), abs(a["geotransform"][5]), 1e-12)
        if any(abs(x - y) > tol * cell + 1e-9 for x, y in zip(a["geotransform"],
                                                               b["geotransform"])):
            return False
        return (a.get("projection") or "") == (b.get("projection") or "")
    except Exception:
        return False


def _study_area(depth_path, buffer_ft):
    """(dict, clip) of the study area as the run builds it (extent.ExtentClip)."""
    from qgis.core import QgsRasterLayer, QgsCoordinateTransformContext
    from .extent import ExtentClip, distance_xy_in_crs, is_geographic, is_web_mercator
    from .constants import RAIL_CROSSING_SEARCH_MI
    try:
        from qgis.core import QgsProject
        ctx = QgsProject.instance().transformContext()
    except Exception:
        ctx = QgsCoordinateTransformContext()
    lyr = QgsRasterLayer(depth_path, "estimate")
    if not lyr.isValid():
        raise ValueError("QGIS cannot open the depth raster")
    clip = ExtentClip(lyr, float(buffer_ft or 0.0), ctx)
    crs = clip.crs
    out = {"buffer_ft": float(buffer_ft or 0.0), "crs": crs.authid() or crs.description() or "",
           "crs_valid": bool(crs.isValid()), "units": _units_name(crs),
           "geographic": bool(is_geographic(crs)), "web_mercator": bool(is_web_mercator(crs)),
           "lat": clip.lat}
    ux, uy = distance_xy_in_crs(1.0, crs, clip.lat)          # CRS units per foot
    if crs.isValid() and ux > 0 and uy > 0:
        w_ft, h_ft = clip.rect.width() / ux, clip.rect.height() / uy
        out.update(width_mi=w_ft / 5280.0, height_mi=h_ft / 5280.0,
                   sq_mi=w_ft * h_ft / _SQ_FT_PER_SQ_MI)
    out["extent_4326"] = clip.bbox_4326()
    out["crossings_4326"] = clip.bbox_4326(extra_ft=RAIL_CROSSING_SEARCH_MI * 5280.0)
    out["places_4326"] = clip.bbox_4326(extra_ft=2.0 * 5280.0)
    return out


# --------------------------------------------------------------- downloads --
def _truthy(v):
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def _on(cfg, key, default=True):
    v = cfg.get(key, default)
    return _truthy(v) if isinstance(v, str) else bool(v)


def _eap_on(cfg, part):
    # pipeline._eap_on: "eap" switches every EAP output, "eap_<part>" one of them
    return _on(cfg, "eap") and _on(cfg, "eap_" + part)


def planned_downloads(cfg, area):
    """[(name, label, kind, url, extent)] the run would download - the job list
    of pipeline.py ("what to download"), from the cfg keys. kind: "nsi",
    "arcgis" or "overpass"; name: the download / pack / tile-cache name."""
    from . import constants as C
    from . import download as DL
    ext = area.get("extent_4326")
    if ext is None:
        return []
    xing = area.get("crossings_4326") or ext
    big = area.get("places_4326") or ext
    none = object()
    jobs = []

    def arc(name, label, url, e=ext):
        jobs.append((name, label, "arcgis", url, e))
    loc = _local_roles(cfg)          # v2.9: what the data folder supplies (as the run does)
    if _on(cfg, "download_nsi") and "nsi" not in loc:
        jobs.append(("NSI_structures", "NSI structures", "nsi", DL.NSI_API_URL, ext))
    if _on(cfg, "download_address") and "address" not in loc:
        arc("NDGISHUB_address_points", "ND GIS Hub address points", C.ND_ADDRESS_POINTS_URL)
    if _on(cfg, "download_usa_structures") and "usa" not in loc:
        arc("USA_Structures", "FEMA USA Structures", C.USA_STRUCTURES_URL)
    if cfg.get("rail_layer", none) in (None, none) and "rail" not in loc and \
            _on(cfg, "download_rail"):
        arc("NARN_rail_lines", "Rail lines", C.NARN_URL)
    if cfg.get("crossing_layer", none) in (None, none) and "crossings" not in loc and \
            _on(cfg, "download_crossings"):
        arc("FRA_grade_crossings", "Grade crossings", C.CROSSINGS_URL, xing)
    if cfg.get("road_layer", none) in (None, none) and "roads" not in loc and \
            _on(cfg, "download_roads"):
        arc("ND_roads_NG911", "ND GIS Hub roads", C.ND_ROADS_URL)
        if cfg.get("aadt_layer", none) in (None, none) and "aadt" not in loc:
            for url in C.HPMS_ND_URLS:
                year = "".join(ch for ch in url.split("HPMS_FULL_ND_")[-1][:4] if ch.isdigit())
                arc("HPMS_ND_{}".format(year), "HPMS {}".format(year), url)
            if C.NDDOT_AADT_URL:
                arc("NDDOT_traffic_counts", "NDDOT traffic counts", "{}/{}".format(
                    C.NDDOT_AADT_URL.rstrip("/"), C.NDDOT_AADT_LAYERS.get("aadt", 0)))
            arc("ND_county_roads", "ND county roads (AADT)", C.ND_COUNTY_ROADS_URL)
    if _on(cfg, "download_dams") and "dams" not in loc:
        arc("NID_dams", "National Inventory of Dams", C.NID_URL)
    if _on(cfg, "download_lifelines"):
        for key, label, url, _cat in C.lifeline_downloads():
            arc("lifeline_" + key, label, url)
        jobs.append(("OSM_lifelines", "OpenStreetMap lifelines", "overpass", C.OVERPASS_URL,
                     ext))
    if _on(cfg, "download_crossings_structures") and "bridges" not in loc:
        arc("NBI_bridges", "National Bridge Inventory", C.NBI_URL)
        jobs.append(("OSM_crossings", "OpenStreetMap crossings", "overpass", C.OVERPASS_URL,
                     ext))
    if _on(cfg, "download_camps") and "camps" not in loc:
        jobs.append(("OSM_campgrounds", "OpenStreetMap campgrounds", "overpass",
                     C.OVERPASS_URL, ext))
    if _on(cfg, "download_trailer_parks"):
        jobs.append(("OSM_trailer_parks", "OpenStreetMap mobile-home parks", "overpass",
                     C.OVERPASS_URL, ext))
    travel = _eap_on(cfg, "travel")
    if travel and _on(cfg, "download_places"):
        jobs.append(("OSM_places", "OpenStreetMap places", "overpass", C.OVERPASS_URL, big))
    if (travel or _on(cfg, "termination", False)) and \
            cfg.get("centerline_layer", none) in (None, none) and "centerline" not in loc \
            and _on(cfg, "download_flowlines"):
        arc("NHD_flowlines", "USGS NHD flowlines", C.NHD_FLOWLINE_URL)
    return jobs


def _local_roles(cfg):
    """The data-folder roles the run will open (datafolder.existing_roles)."""
    from . import datafolder as DF
    return DF.existing_roles(cfg.get("local_data"))


def _pack_info(pack_path):
    """{"bbox", "datasets": {name: {"status", "gaps"}}, "label"} of an offline
    pack without unpacking it, or {"error"}."""
    from . import offline_pack as OP
    if not pack_path:
        return None
    try:
        man = OP.read_manifest(pack_path)
        if man is None and not pack_path.lower().endswith(".zip"):
            db = os.path.join(pack_path, OP.DB_NAME) if os.path.isdir(pack_path) else pack_path
            r = OP.PackReader(db)                     # read-only connection
            return {"bbox": tuple(r.bbox), "label": OP.describe(pack_path),
                    "datasets": {k: {"status": v.get("status"), "gaps": v.get("gaps") or []}
                                 for k, v in r.datasets.items()}}
        if man is None or man.get("format") != OP.PACK_FORMAT:
            return {"error": "not an offline data pack (no manifest.json)"}
        ds = {}
        for k, v in (man.get("datasets") or {}).items():
            if v.get("status") in OP.USABLE_STATUS:
                ds[k] = {"status": v.get("status"), "gaps": [tuple(g) for g in
                                                             (v.get("gaps") or [])]}
        return {"bbox": tuple(man.get("bbox") or ()), "datasets": ds,
                "label": OP.describe(pack_path)}
    except Exception as e:                            # noqa
        return {"error": str(e) or e.__class__.__name__}


def _pack_answer(pack, name, extent):
    """"covers" (the pack answers), "partial" (it holds the data set, not all of
    this area) or None."""
    from . import offline_pack as OP
    if not pack or pack.get("error") or name not in pack.get("datasets", {}):
        return None
    info = pack["datasets"][name]
    bbox = pack.get("bbox")
    if bbox and len(bbox) == 4 and OP.covers(bbox, extent) and \
            info.get("status") == OP.STATUS_COMPLETE and \
            not any(OP.boxes_touch(g, extent) for g in info.get("gaps") or ()):
        return "covers"
    return "partial"


_FETCHED_RE = re.compile(rb'"fetched"\s*:\s*(-?[0-9.]+(?:[eE][-+]?[0-9]+)?)')


def tile_age_days(path, now):
    """Age (days) of a tile-cache file from its "fetched" stamp (in the first
    bytes of the gzip); None when it cannot be read - the run would not use
    that tile (TileCache.read rejects it) but fetch it again. Read-only:
    nothing is removed or rewritten."""
    try:
        with gzip.open(path, "rb") as fh:
            head = fh.read(4096)
        m = _FETCHED_RE.search(head)
        return (now - float(m.group(1))) / 86400.0 if m else None
    except Exception:                                 # noqa - damaged tile
        return None


def _tile_state(cache_folder, source, url_key, tiles, max_age, refresh, now):
    """(fresh, stale, missing) tile counts of one source in the tile cache."""
    from .download import TileCache
    tc = TileCache(cache_folder, max_age_days=max_age)
    fresh = stale = missing = 0
    for t in tiles:
        p = tc.path(source, url_key, t)
        if not os.path.isfile(p):
            missing += 1
            continue
        age = tile_age_days(p, now)
        if refresh or age is None or age < -1.0 or max_age <= 0 or age >= max_age:
            stale += 1
        else:
            fresh += 1
    return fresh, stale, missing


def _todays_download(output_folder, name, extent):
    """Today's whole-area download file in outputs/_downloads (the run re-uses
    it) - path or None. Nothing is created."""
    if not output_folder:
        return None
    try:
        from .download import _cache_path
        from .constants import DOWNLOAD_FOLDER_NAME
        p = _cache_path(os.path.join(output_folder, DOWNLOAD_FOLDER_NAME), name, extent)
        return p if os.path.isfile(p) else None
    except Exception:
        return None


def _plan_one(job, cache_folder, pack, offline_only, max_age, refresh, now, output_folder):
    from . import download as DL
    name, label, kind, url, ext = job
    row = {"name": name, "label": label, "kind": kind, "url": url,
           "extent": tuple(ext), "source": "internet", "tiles": 0, "fresh": 0, "stale": 0,
           "missing": 0, "requests": 0, "overpass_requests": 0, "pack_queries": 0,
           "note": ""}
    ans = _pack_answer(pack, name, ext)
    today = _todays_download(output_folder, name, ext)
    if today and (kind == "overpass" or not refresh):
        row.update(source="today's download", note=os.path.basename(today))
        return row
    if ans == "covers":
        row.update(source="offline pack", pack_queries=1)
        return row
    if ans == "partial" and offline_only:
        row.update(source="offline pack", pack_queries=1,
                   note="INCOMPLETE for this area (beyond the pack's area, a gap, or a partial "
                        "data set)")
        return row
    if offline_only:
        row.update(source="skipped", note="offline only - not in the offline data pack")
        return row
    if ans == "partial":
        row["note"] = "the offline pack does not cover all of it - the service first"
    if kind == "overpass":
        row.update(overpass_requests=1)
        return row
    if kind == "nsi":
        row["nsi_tiles_02"] = len(DL.tile_extent(ext))
        if row["nsi_tiles_02"] > DL.NSI_MAX_TILES:
            row.update(source="refused", note="{} NSI tiles of {:g} deg - more than the "
                       "plugin's limit of {}: the NSI download will fail".format(
                           row["nsi_tiles_02"], DL.DOWNLOAD_TILE_DEG, DL.NSI_MAX_TILES))
            return row
    if cache_folder:
        tiles = DL.cache_tiles(ext)
        key = url if kind == "nsi" else "{}|{}".format(url.rstrip("/"), "1=1")
        fresh, stale, missing = _tile_state(cache_folder, name, key, tiles, max_age, refresh,
                                            now)
        row.update(tiles=len(tiles), fresh=fresh, stale=stale, missing=missing)
        todo = stale + missing
        if kind == "nsi":
            row["requests"] = todo
        else:
            # the layer description (edit date) is always asked, then per tile at
            # least a count and one page
            row["requests"] = 1 + 2 * todo
        if not todo:
            row["source"] = "tile cache"
    else:
        if kind == "nsi":
            row.update(tiles=row["nsi_tiles_02"], missing=row["nsi_tiles_02"],
                       requests=row["nsi_tiles_02"])
        else:
            row["requests"] = 3        # description, count, first page (more pages: more)
            row["note"] = (row["note"] + "; " if row["note"] else "") + \
                "at least 3 requests (more pages when the area holds many features)"
    return row


# ------------------------------------------------------------------ public --
def estimate(depth_path, velocity_path=None, extent_buffer_ft=None, downloads=None,
             pack_path=None, tile_cache_folder=None, extra_rasters=(), arrival_path=None,
             output_folder=None, offline_only=None, clock=None):
    """Pre-run estimate. Never raises; downloads nothing.

    depth_path / velocity_path / arrival_path: rasters of the (governing)
    breach scenario; extra_rasters: further rasters the run reads (other
    scenarios, peak-time raster) - paths or (label, path). extent_buffer_ft:
    None = the plugin default. downloads: the pipeline cfg (download_* keys,
    rail_layer / crossing_layer / road_layer / aadt_layer / centerline_layer
    - a layer there replaces a download -, eap / eap_travel / termination,
    data_max_age_days, refresh_data). pack_path: the offline data pack (or
    None); offline_only: None = download.offline_config(). tile_cache_folder:
    cfg "data_cache_folder" ("" / None: off, "auto": the default folder).
    output_folder: to see today's downloads there (optional).

    Returns a dict: "depth" / "velocity" / "arrival" / "extra" (raster_info),
    "study_area", "downloads" (one row per data set), "counts", "time_s"
    (lo, hi), "time_parts", "disk_mb" (lo, hi), "warnings", "notes",
    "assumptions", "ok"."""
    now = float((clock or time.time)())
    cfg = dict(downloads or {})
    est = {"ok": False, "warnings": [], "notes": [], "downloads": [], "extra": [],
           "assumptions": assumptions(), "rough_note": ROUGH_NOTE}

    def note(what, e):
        est["notes"].append("{} could not be estimated: {}".format(
            what, str(e) or e.__class__.__name__))
    try:
        if extent_buffer_ft is None:
            from .constants import DEFAULT_EXTENT_BUFFER_FT
            extent_buffer_ft = cfg.get("extent_buffer_ft", DEFAULT_EXTENT_BUFFER_FT)
    except Exception:
        extent_buffer_ft = 25.0
    # ---- rasters -----------------------------------------------------------
    try:
        dep = est["depth"] = raster_info(depth_path)
        if dep.get("error"):
            est["notes"].append("Depth raster {}: {}".format(
                os.path.basename(depth_path or "") or "(none)", dep["error"]))
        est["velocity"] = raster_info(velocity_path) if velocity_path else None
        est["arrival"] = raster_info(arrival_path) if arrival_path else None
        for item in extra_rasters or ():
            label, path = item if isinstance(item, (tuple, list)) else (None, item)
            ri = raster_info(path)
            ri["label"] = label or os.path.basename(str(path or ""))
            est["extra"].append(ri)
    except Exception as e:                                        # noqa
        note("The rasters", e)
        dep = est.setdefault("depth", {"path": depth_path, "error": str(e)})
    # ---- study area --------------------------------------------------------
    area = {}
    if not dep.get("error"):
        try:
            area = _study_area(depth_path, extent_buffer_ft)
        except Exception as e:                                    # noqa
            note("The study area", e)
    est["study_area"] = area
    # ---- downloads ---------------------------------------------------------
    counts = {"datasets": 0, "http_requests": 0, "overpass_requests": 0, "pack_queries": 0,
              "tiles": 0, "tiles_fresh": 0, "tiles_to_download": 0}
    try:
        if area.get("extent_4326") is not None:
            cache = tile_cache_folder
            if isinstance(cache, str) and cache.strip().lower() == "auto" or cache is True:
                from .download import default_cache_folder
                cache = default_cache_folder()
            cache = cache or None
            if offline_only is None:
                try:
                    from .download import offline_config
                    offline_only = offline_config()[1] if pack_path else False
                except Exception:
                    offline_only = False
            try:
                max_age = float(cfg.get("data_max_age_days", DEFAULT_MAX_AGE_DAYS))
            except (TypeError, ValueError):
                max_age = DEFAULT_MAX_AGE_DAYS
            refresh = _on(cfg, "refresh_data", False)
            pack = _pack_info(pack_path)
            if pack and pack.get("error"):
                est["warnings"].append("Offline data pack not usable ({}) - every data set "
                                       "from the internet".format(pack["error"]))
            est["pack"] = pack
            est["tile_cache"] = {"folder": cache, "max_age_days": max_age, "refresh": refresh,
                                 "exists": bool(cache and os.path.isdir(cache))}
            for job in planned_downloads(cfg, area):
                try:
                    row = _plan_one(job, cache, pack, bool(offline_only), max_age, refresh,
                                    now, output_folder)
                except Exception as e:                            # noqa
                    row = {"name": job[0], "label": job[1], "kind": job[2], "source": "?",
                           "requests": 0, "overpass_requests": 0, "pack_queries": 0,
                           "tiles": 0, "fresh": 0, "stale": 0, "missing": 0,
                           "note": "not estimated: {}".format(e)}
                est["downloads"].append(row)
            for r in est["downloads"]:
                counts["datasets"] += 1
                counts["http_requests"] += r.get("requests", 0)
                counts["overpass_requests"] += r.get("overpass_requests", 0)
                counts["pack_queries"] += r.get("pack_queries", 0)
                counts["tiles"] += r.get("tiles", 0)
                counts["tiles_fresh"] += r.get("fresh", 0)
                counts["tiles_to_download"] += r.get("stale", 0) + r.get("missing", 0) \
                    if r.get("source") in ("internet", "tile cache") and cache else 0
        elif not dep.get("error"):
            est["warnings"].append("The depth raster has no usable CRS - nothing can be "
                                   "downloaded (only project layers are used)")
    except Exception as e:                                        # noqa
        note("The downloads", e)
    est["counts"] = counts
    # ---- time / disk ---------------------------------------------------------
    try:
        _time_and_disk(est, cfg)
    except Exception as e:                                        # noqa
        note("Time and disk use", e)
    try:
        _warnings(est, cfg, arrival_path)
    except Exception as e:                                        # noqa
        note("The warnings", e)
    est["ok"] = not dep.get("error")
    return est


def assumptions():
    return {"SEC_PER_HTTP_REQUEST": SEC_PER_HTTP_REQUEST,
            "SEC_PER_OVERPASS_REQUEST": SEC_PER_OVERPASS_REQUEST,
            "SEC_PER_PACK_QUERY": SEC_PER_PACK_QUERY,
            "SEC_PER_MCELL_READ": SEC_PER_MCELL_READ,
            "SEC_PER_MCELL_PRODUCTS": SEC_PER_MCELL_PRODUCTS,
            "SEC_FIXED": SEC_FIXED, "MB_PER_HTTP_REQUEST": MB_PER_HTTP_REQUEST,
            "MB_OUTPUT_PER_MCELL": MB_OUTPUT_PER_MCELL, "MB_OUTPUT_FIXED": MB_OUTPUT_FIXED}


def _time_and_disk(est, cfg):
    from .constants import DOWNLOAD_WORKERS, OVERPASS_WORKERS
    c = est.get("counts") or {}
    workers, ovp_workers = max(1, int(DOWNLOAD_WORKERS)), max(1, int(OVERPASS_WORKERS))
    http, ovp, pq = c.get("http_requests", 0), c.get("overpass_requests", 0), \
        c.get("pack_queries", 0)
    # requests run side by side (DOWNLOAD_WORKERS; Overpass OVERPASS_WORKERS)
    dl_lo = max(math.ceil(http / float(workers)) * SEC_PER_HTTP_REQUEST[0],
                math.ceil(ovp / float(ovp_workers)) * SEC_PER_OVERPASS_REQUEST[0])
    dl_hi = math.ceil(http / float(workers)) * SEC_PER_HTTP_REQUEST[1] + \
        math.ceil(ovp / float(ovp_workers)) * SEC_PER_OVERPASS_REQUEST[1]
    pk = (pq * SEC_PER_PACK_QUERY[0], pq * SEC_PER_PACK_QUERY[1])
    dep = est.get("depth") or {}
    mcells = (dep.get("cells") or 0) / 1e6
    reads = [r for r in [dep, est.get("velocity"), est.get("arrival")] + list(est.get("extra")
                                                                             or [])
             if r and not r.get("error")]
    read_mcells = sum((r.get("cells") or 0) for r in reads) / 1e6
    rd = (read_mcells * SEC_PER_MCELL_READ[0], read_mcells * SEC_PER_MCELL_READ[1])
    products = _on(cfg, "zone_rasters") or _eap_on(cfg, "list")
    pr = (mcells * SEC_PER_MCELL_PRODUCTS[0], mcells * SEC_PER_MCELL_PRODUCTS[1]) \
        if products else (0.0, 0.0)
    parts = {"downloads": (dl_lo, dl_hi), "offline pack": pk, "rasters read": rd,
             "raster products": pr, "everything else": SEC_FIXED}
    est["time_parts"] = parts
    est["time_s"] = (sum(v[0] for v in parts.values()), sum(v[1] for v in parts.values()))
    dmb = (http * MB_PER_HTTP_REQUEST[0], http * MB_PER_HTTP_REQUEST[1])
    omb = (mcells * MB_OUTPUT_PER_MCELL[0] + MB_OUTPUT_FIXED[0],
           mcells * MB_OUTPUT_PER_MCELL[1] + MB_OUTPUT_FIXED[1])
    est["disk_parts"] = {"downloads": dmb, "outputs": omb}
    est["disk_mb"] = (dmb[0] + omb[0], dmb[1] + omb[1])


def _warnings(est, cfg, arrival_path):
    w = est["warnings"]
    dep = est.get("depth") or {}
    area = est.get("study_area") or {}
    if area.get("sq_mi") and area["sq_mi"] > WARN_STUDY_AREA_SQ_MI:
        w.append("Study area very large: {:,.0f} sq mi (more than {:,.0f}) - many downloads; "
                 "check the depth raster's extent (a model domain much larger than the "
                 "flooded area?)".format(area["sq_mi"], WARN_STUDY_AREA_SQ_MI))
    if dep.get("cells") and (dep["cells"] > WARN_RASTER_CELLS or
                             dep.get("mb_on_disk", 0) > WARN_RASTER_MB):
        w.append("Depth raster very large: {:,} x {:,} cells ({:,.0f} million, {:,.0f} MB on "
                 "disk) - reading it takes long; a coarser export or a clip to the flooded "
                 "area is faster".format(dep["cols"], dep["rows"], dep["cells"] / 1e6,
                                         dep.get("mb_on_disk", 0)))
    if area.get("geographic") or area.get("web_mercator"):
        w.append("The depth raster CRS ({}) is {}: distances in feet are corrected for the "
                 "latitude; a projected CRS (UTM / State Plane) is more accurate".format(
                     area.get("crs") or "?",
                     "geographic" if area.get("geographic") else "Web Mercator"))
    vel = est.get("velocity")
    if vel is not None:
        if vel.get("error"):
            w.append("Velocity raster {}: {}".format(os.path.basename(vel.get("path") or ""),
                                                      vel["error"]))
        elif not dep.get("error") and not same_grid(dep, vel):
            w.append("The velocity grid differs from the depth grid ({}) - it is resampled "
                     "to the depth grid (nearest cell) while the run reads it: slower, and "
                     "check that the two rasters belong together".format(_grid_diff(dep, vel)))
    elif not dep.get("error"):
        est["notes"].append("No velocity raster: the depth-only (1D) criterion is used")
    arr = est.get("arrival")
    if arr is None and (_eap_on(cfg, "list") or _eap_on(cfg, "mapbook")):
        w.append("No arrival-time raster: the EAP arrival bands / contours, the map book and "
                 "the arrival times of the notification list need one")
    elif arr is not None and arr.get("error"):
        w.append("Arrival-time raster {}: {}".format(os.path.basename(arr.get("path") or ""),
                                                     arr["error"]))
    for r in est.get("extra") or []:
        if r.get("error"):
            w.append("{}: {}".format(r.get("label") or r.get("path"), r["error"]))
    for row in est.get("downloads") or []:
        if row.get("source") == "refused":
            w.append("{}: {}".format(row["label"], row["note"]))
        elif row.get("source") == "skipped":
            w.append("{}: skipped - {}".format(row["label"], row["note"]))
        elif row.get("source") == "offline pack" and row.get("note"):
            w.append("{}: offline pack {}".format(row["label"], row["note"]))
        if row.get("tiles", 0) > WARN_TILES:
            w.append("{}: {} tiles - a long download".format(row["label"], row["tiles"]))
    tc = est.get("tile_cache") or {}
    if tc.get("folder") and not tc.get("exists"):
        est["notes"].append("The tile cache folder does not exist yet - every tile is "
                            "downloaded (and kept for the next run)")


def _grid_diff(a, b):
    parts = []
    try:
        if (a["cols"], a["rows"]) != (b["cols"], b["rows"]):
            parts.append("{} x {} vs {} x {} cells".format(a["cols"], a["rows"], b["cols"],
                                                           b["rows"]))
        if abs(a["cell_x"] - b["cell_x"]) > 1e-9 or abs(a["cell_y"] - b["cell_y"]) > 1e-9:
            parts.append("cell {:g} vs {:g}".format(a["cell_x"], b["cell_x"]))
        if (a.get("projection") or "") != (b.get("projection") or ""):
            parts.append("another CRS")
        if not parts:
            parts.append("origin shifted")
    except Exception:
        parts.append("grid")
    return ", ".join(parts)


# ------------------------------------------------------------------ text --
def _dur(s):
    s = float(s)
    if s < 90:
        return "{:.0f} s".format(s)
    if s < 5400:
        return "{:.0f} min".format(s / 60.0)
    return "{:.1f} h".format(s / 3600.0)


def _range(pair, fmt):
    return "{} - {}".format(fmt(pair[0]), fmt(pair[1]))


def _mb(v):
    return "{:,.0f} MB".format(v) if v >= 10 else "{:.1f} MB".format(v)


def estimate_lines(est):
    """Text lines of an estimate() result (never raises)."""
    lines = []
    try:
        lines.append("PRE-RUN ESTIMATE (rough)")
        dep = est.get("depth") or {}
        area = est.get("study_area") or {}
        if dep.get("error"):
            lines.append("Depth raster: {} - {}".format(
                os.path.basename(dep.get("path") or "") or "(none)", dep["error"]))
        elif dep:
            lines.append("Depth raster: {:,} x {:,} cells ({:,.1f} million), cell {:g} x {:g} "
                         "{}, {}, {} on disk ({} in memory)".format(
                             dep["cols"], dep["rows"], dep["cells"] / 1e6, dep["cell_x"],
                             dep["cell_y"], area.get("units") or "CRS units",
                             area.get("crs") or "CRS unknown", _mb(dep.get("mb_on_disk", 0)),
                             _mb(dep.get("mb_in_memory", 0))))
        for key, label in (("velocity", "Velocity raster"), ("arrival", "Arrival raster")):
            r = est.get(key)
            if r and not r.get("error"):
                lines.append("{}: {:,} x {:,} cells, {} on disk{}".format(
                    label, r["cols"], r["rows"], _mb(r.get("mb_on_disk", 0)),
                    "" if not dep or dep.get("error") or same_grid(dep, r)
                    else " - NOT the depth grid"))
        for r in est.get("extra") or []:
            if not r.get("error"):
                lines.append("Also read: {} ({:,.1f} million cells)".format(
                    r.get("label"), r["cells"] / 1e6))
        if area.get("sq_mi") is not None:
            e = area.get("extent_4326")
            lines.append("Study area: raster extent + {:g} ft = {:,.1f} sq mi ({:.1f} x {:.1f} "
                         "mi){}".format(area.get("buffer_ft", 0), area["sq_mi"],
                                        area.get("width_mi", 0), area.get("height_mi", 0),
                                        "; lon/lat {:.4f}, {:.4f} to {:.4f}, {:.4f}".format(*e)
                                        if e else ""))
        c = est.get("counts") or {}
        rows = est.get("downloads") or []
        if rows:
            by = {}
            for r in rows:
                by[r.get("source")] = by.get(r.get("source"), 0) + 1
            lines.append("Downloads: {} data set(s) - {}; about {} request(s) to the "
                         "services{}".format(
                             c.get("datasets", 0), ", ".join("{} {}".format(v, k) for k, v in
                                                             sorted(by.items())),
                             c.get("http_requests", 0),
                             ", {} OpenStreetMap Overpass quer(ies)".format(
                                 c["overpass_requests"]) if c.get("overpass_requests") else ""))
            tc = est.get("tile_cache") or {}
            if tc.get("folder"):
                lines.append("Tile cache: {} of {} tile(s) there and fresh (younger than {:g} "
                             "day(s){}), {} to download".format(
                                 c.get("tiles_fresh", 0), c.get("tiles", 0),
                                 tc.get("max_age_days", 0),
                                 "; REFRESH requested" if tc.get("refresh") else "",
                                 c.get("tiles_to_download", 0)))
            for r in rows:
                extra = ""
                if r.get("tiles"):
                    extra = " - {} tile(s)".format(r["tiles"])
                    if (est.get("tile_cache") or {}).get("folder"):
                        extra += ", {} fresh in the cache, {} to download".format(
                            r.get("fresh", 0), r.get("stale", 0) + r.get("missing", 0))
                lines.append("  {}: {}{}{}".format(r.get("label"), r.get("source"), extra,
                                                   " ({})".format(r["note"])
                                                   if r.get("note") else ""))
        if est.get("time_s"):
            parts = est.get("time_parts") or {}
            lines.append("Rough time: {} ({})".format(
                _range(est["time_s"], _dur), ", ".join(
                    "{} {}".format(k, _range(v, _dur)) for k, v in parts.items() if v[1] > 0)))
        if est.get("disk_mb"):
            lines.append("Rough disk use: {} (downloads {}, outputs {})".format(
                _range(est["disk_mb"], _mb),
                _range((est.get("disk_parts") or {}).get("downloads", (0, 0)), _mb),
                _range((est.get("disk_parts") or {}).get("outputs", (0, 0)), _mb)))
        for m in est.get("warnings") or []:
            lines.append("WARNING: " + m)
        for m in est.get("notes") or []:
            lines.append("Note: " + m)
        lines.append(est.get("rough_note") or ROUGH_NOTE)
    except Exception as e:                                        # noqa
        lines.append("(estimate text incomplete: {})".format(e))
    return lines


def estimate_for_cfg(cfg, pack_path=None, offline_only=None):
    """estimate() for a pipeline cfg: the first breach scenario's rasters
    (cfg["scenarios"]), the other scenarios' rasters as extra reads, the cfg's
    buffer, downloads, tile cache and output folder. pack_path None: the
    plugin's offline setting (download.offline_config()). Never raises."""
    try:
        cfg = cfg or {}
        scs = [s for s in (cfg.get("scenarios") or []) if getattr(s, "depth", None)]
        if not scs:
            return {"ok": False, "warnings": [], "notes": ["No scenario with a depth raster"],
                    "downloads": [], "extra": [], "rough_note": ROUGH_NOTE,
                    "assumptions": assumptions()}
        try:
            from .scenarios import ROLE_BREACH
            main = next((s for s in scs if getattr(s, "role", ROLE_BREACH) == ROLE_BREACH),
                        scs[0])
        except Exception:
            main = scs[0]
        extra = []
        for s in scs:
            for attr in ("depth", "velocity", "arrival"):
                p = getattr(s, attr, None)
                if p and not (s is main):
                    extra.append(("{} {}".format(s.name, attr), p))
        if pack_path is None:
            try:
                from .download import offline_config
                pack_path, only = offline_config()
                offline_only = only if offline_only is None else offline_only
            except Exception:
                pack_path = None
        return estimate(main.depth, velocity_path=getattr(main, "velocity", None),
                        extent_buffer_ft=cfg.get("extent_buffer_ft"), downloads=cfg,
                        pack_path=pack_path or None,
                        tile_cache_folder=cfg.get("data_cache_folder"), extra_rasters=extra,
                        arrival_path=getattr(main, "arrival", None),
                        output_folder=cfg.get("output_folder"), offline_only=offline_only)
    except Exception as e:                                        # noqa
        return {"ok": False, "warnings": [], "downloads": [], "extra": [],
                "notes": ["The estimate could not be made: {}".format(e)],
                "rough_note": ROUGH_NOTE, "assumptions": assumptions()}
