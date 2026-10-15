# -*- coding: utf-8 -*-
"""v2.5.1: 'Download data' - every data set the plugin can fetch from the
internet (or from a work server whose address is set in the data-source
file) saved as a GeoPackage in <outputs>/Data and added to the project.

Area of each data set:
  "study" - the study area (depth raster extent + the study-area buffer)
  "flood" - structures, facilities and other point / area data only where
            the flood is (wet depth cells + the flood buffer); lines (roads,
            rail, 3DHP flowlines) keep the whole study area because the road
            network beyond the flood decides sole access and the travel-time
            trace

The downloads go through download.py, so the dated caches in
outputs/_downloads are shared with Main Consolidation and the Hazard
Classification of the same day (nothing is fetched twice), and the
stale-cache fallback applies.
"""

import math
import os

import numpy as np
from osgeo import gdal

from qgis.core import (
    QgsCoordinateTransform, QgsFeatureRequest, QgsProject, QgsVectorLayer,
)

from .constants import (
    NARN_URL, CROSSINGS_URL, ND_ROADS_URL, HPMS_ND_URLS, ND_COUNTY_ROADS_URL, NDDOT_AADT_URL,
    NDDOT_AADT_LAYERS, NHD_FLOWLINE_URL, lifeline_downloads, RAIL_CROSSING_SEARCH_MI,
    GRP_DATA, SUBDIR_DATA,
)
from .compat import MSG_WARNING
from .download import (
    fetch_nsi, fetch_nd_address_points, fetch_usa_structures, fetch_osm_camps,
    fetch_arcgis_layer, fetch_dams, fetch_osm_lifelines, fetch_nbi, fetch_osm_crossings,
    fetch_osm_places, try_download, STALE_USED, PACK_USED,
    configure_tile_cache_from_cfg, tile_cache_report_lines,
)
from .extent import distance_xy_in_crs
from .helpers import (
    log_message, export_layer_to_formats, add_layer_to_project, plugin_group,
    dam_display_name, get_layer_path, gdal_exceptions, CancelledByUser, is_null, strip_rows,
)

AREA_STUDY, AREA_FLOOD = "study", "flood"
AREA_CHOICES = [("Study area - depth raster extent + study-area buffer", AREA_STUDY),
                ("Flooded area - wet depth cells + flood buffer (lines keep the study "
                 "area)", AREA_FLOOD)]
DEFAULT_FLOOD_BUFFER_FT = 300.0
MASK_MAX_CELLS = 4000            # flood mask side, cells (coarser grid for huge rasters)

# (key, file name, label, config flag, may be limited to the flood)
# the flag names are the pipeline's cfg keys, so one set of choices drives
# the downloads here, in Main Consolidation and in the classification
DATASETS = [
    ("nsi", "NSI_Structures", "USACE National Structure Inventory", "download_nsi", True),
    ("address", "ND_Site_Addresses", "ND GIS Hub site / structure address points",
     "download_address", True),
    ("usa", "FEMA_USA_Structures", "FEMA USA Structures outlines", "download_usa_structures",
     True),
    ("camps", "Campgrounds", "OpenStreetMap campgrounds / RV parks", "download_camps", True),
    ("roads", "ND_Roads", "ND GIS Hub NG9-1-1 road centerlines", "download_roads", False),
    ("traffic", "Traffic_Counts", "Traffic counts (HPMS / NDDOT / county AADT)",
     "download_roads", False),
    ("rail", "Rail_Lines", "FRA/BTS rail network", "download_rail", False),
    ("xing", "Rail_Grade_Crossings", "FRA grade crossings", "download_crossings", True),
    ("bridges", "Bridges_Culverts", "National Bridge Inventory + OpenStreetMap crossings",
     "download_crossings_structures", True),
    ("dams", "NID_Dams", "National Inventory of Dams", "download_dams", True),
    ("lifelines", "Lifeline_Facilities", "Lifeline facilities (ND GIS Hub, EIA, OSM)",
     "download_lifelines", True),
    ("flowlines", "3DHP_Flowlines", "USGS 3DHP flowlines (streams)", "download_flowlines", False),
    ("places", "Towns", "OpenStreetMap cities / towns / villages", "download_places", True),
]


# ------------------------------------------------------------ flood mask --
def _dilate(mask, r):
    """Square dilation of a boolean grid by r cells (two 1D passes with
    cumulative sums - no scipy)."""
    if r <= 0:
        return mask
    out = mask
    for axis in (0, 1):
        a = np.moveaxis(out, axis, 0).astype(np.int32)
        n = a.shape[0]
        c = np.zeros((n + 1,) + a.shape[1:], dtype=np.int32)
        np.cumsum(a, axis=0, out=c[1:])
        lo = np.clip(np.arange(n) - r, 0, n)
        hi = np.clip(np.arange(n) + r + 1, 0, n)
        out = np.moveaxis((c[hi] - c[lo]) > 0, 0, axis)
    return out


class FloodMask(object):
    """Where the depth raster is wet (> 0), grown by `buffer_ft`, on a grid
    of at most MASK_MAX_CELLS per side (a coarse cell is wet when any of its
    cells is - never smaller than the flood)."""

    def __init__(self, depth_layer, buffer_ft, transform_ctx, lat=None):
        self.ok = False
        self.crs = depth_layer.crs() if depth_layer is not None else None
        self.ctx = transform_ctx
        self._xf = {}
        path = get_layer_path(depth_layer)
        if not path:
            return
        with gdal_exceptions():
            try:
                self._read(path, buffer_ft, lat)
                self.ok = True
            except Exception as e:
                log_message("Flood mask not made ({}) - the study area is used".format(e),
                            MSG_WARNING)

    def _read(self, path, buffer_ft, lat):
        ds = gdal.Open(path)
        if ds is None:
            raise RuntimeError("cannot open " + path)
        w, h = ds.RasterXSize, ds.RasterYSize
        scale = max(1.0, float(max(w, h)) / MASK_MAX_CELLS)
        if scale > 1.0:
            # coarse grid over the same extent; "max" keeps a coarse cell wet
            # when any of its cells is wet (never smaller than the flood)
            cw, ch = max(1, int(math.ceil(w / scale))), max(1, int(math.ceil(h / scale)))
            g0 = ds.GetGeoTransform()
            bounds = (g0[0], g0[3] + h * g0[5], g0[0] + w * g0[1], g0[3])
            ds = gdal.Warp("", ds, format="MEM", width=cw, height=ch, outputBounds=bounds,
                           resampleAlg="max")
        band = ds.GetRasterBand(1)
        nod = band.GetNoDataValue()
        s, o = band.GetScale(), band.GetOffset()
        s = s if s not in (None, 0) else 1.0
        o = o or 0.0
        # v2.12: strips of at most READ_BLOCK_TARGET_CELLS cells - only the
        # boolean wet mask is kept whole (1 byte per cell)
        nx, ny = ds.RasterXSize, ds.RasterYSize
        wet = np.zeros((ny, nx), dtype=bool)
        rows = strip_rows(nx, ny)
        for y0 in range(0, ny, rows):
            hh = min(rows, ny - y0)
            a = np.asarray(band.ReadAsArray(0, y0, nx, hh), dtype=np.float64)
            if nod is not None:
                a[a == nod] = np.nan
            a = a * s + o
            with np.errstate(invalid="ignore"):
                wet[y0:y0 + hh, :] = np.isfinite(a) & (a > 0)
            del a
        gt = ds.GetGeoTransform()
        self.gt = gt
        self.shape = wet.shape
        dx, dy = distance_xy_in_crs(float(buffer_ft), self.crs, lat) if self.crs is not None \
            else (0.0, 0.0)
        r = int(math.ceil(max(dx / abs(gt[1]), dy / abs(gt[5])))) if buffer_ft > 0 else 0
        r = min(r, max(self.shape))
        if r:
            # pad the grid by the buffer so a feature just outside the raster
            # edge but within the buffer of a wet edge cell is kept
            wet = np.pad(wet, r, mode="constant", constant_values=False)
            gt = (gt[0] - r * gt[1], gt[1], gt[2], gt[3] - r * gt[5], gt[4], gt[5])
            self.gt = gt
            self.shape = wet.shape
        self.mask = _dilate(wet, r)
        self.n_wet = int(wet.sum())

    def _to_mask(self, src_crs):
        key = src_crs.authid() or src_crs.toWkt() if src_crs is not None else ""
        if key not in self._xf:
            xf = None
            try:
                if src_crs is not None and src_crs.isValid() and self.crs.isValid():
                    t = QgsCoordinateTransform(src_crs, self.crs, self.ctx)
                    xf = None if t.isShortCircuited() else t
            except Exception:
                xf = None
            self._xf[key] = xf
        return self._xf[key]

    def touches(self, geom, src_crs):
        """True when the feature's box reaches a wet (buffered) cell. A
        feature that cannot be located is kept."""
        if not self.ok:
            return True
        try:
            bb = geom.boundingBox()
            xf = self._to_mask(src_crs)
            if xf is not None:
                bb = xf.transformBoundingBox(bb)
            gt = self.gt
            c0 = (bb.xMinimum() - gt[0]) / gt[1]
            c1 = (bb.xMaximum() - gt[0]) / gt[1]
            r0 = (bb.yMaximum() - gt[3]) / gt[5]
            r1 = (bb.yMinimum() - gt[3]) / gt[5]
            c0, c1 = sorted((c0, c1))
            r0, r1 = sorted((r0, r1))
            h, w = self.shape
            c0, c1 = max(0, int(math.floor(c0))), min(w - 1, int(math.floor(c1)))
            r0, r1 = max(0, int(math.floor(r0))), min(h - 1, int(math.floor(r1)))
            if c0 > c1 or r0 > r1:
                return False                  # outside the raster
            return bool(self.mask[r0:r1 + 1, c0:c1 + 1].any())
        except Exception:
            return True


# ------------------------------------------------------------ downloads --
def data_folder(output_folder):
    from .processing import DATA_MARKER_FILE
    path = os.path.join(output_folder, SUBDIR_DATA)
    os.makedirs(path, exist_ok=True)
    marker = os.path.join(path, DATA_MARKER_FILE)
    if not os.path.isfile(marker):
        try:
            with open(marker, "w", encoding="utf-8") as fh:
                fh.write("Layers in this folder were saved by the ND Dam Hazard "
                         "Classification Helper (Data & Output tab).\n")
        except OSError:
            pass
    return path


def _kept_ids(layer, clip, mask, use_flood, progress=None):
    ids = []
    xf = clip.transform_from(layer.crs()) if clip is not None else None
    for f in layer.getFeatures(QgsFeatureRequest().setNoAttributes()):
        g = f.geometry()
        if g is None or g.isNull() or g.isEmpty():
            continue
        if clip is not None and not clip.intersects_geometry(g, xform=xf):
            continue
        if use_flood and mask is not None and not mask.touches(g, layer.crs()):
            continue
        ids.append(f.id())
    return ids


def _save(layer, name, dam, out, clip, mask, use_flood, ctx, add_layer):
    """Write the features of `layer` inside the area to Data/<dam>_<name>.gpkg
    and hand the file to add_layer(path, base) (None: not shown).
    Returns (path or None, kept, total)."""
    total = layer.featureCount()
    ids = _kept_ids(layer, clip, mask, use_flood)
    base = "{}_{}".format(dam, name)
    sub = layer.materialize(QgsFeatureRequest().setFilterFids(ids)) if ids else None
    if sub is None:
        return None, 0, total
    _ok, _r, paths = export_layer_to_formats(sub, data_folder(out), base, ctx,
                                             formats=("gpkg",))
    path = paths.get("gpkg")
    if path and add_layer is not None:
        add_layer(path, base)
    return path, len(ids), total


def _merge(layers):
    """One memory layer of several layers of the same kind (lifelines from
    several agencies): geometry, source and name / category."""
    from qgis.core import QgsFeature, QgsField
    from .compat import FT_STRING, WKB_POINT, GEOM_POINT
    from .helpers import create_memory_layer
    layers = [(label, lyr) for label, lyr in layers if lyr is not None and lyr.featureCount()]
    if not layers:
        return None
    from qgis.core import QgsCoordinateReferenceSystem
    crs = QgsCoordinateReferenceSystem("EPSG:4326")
    out = create_memory_layer("merged", WKB_POINT, crs,
                              [QgsField("Source", FT_STRING), QgsField("Name", FT_STRING),
                               QgsField("Type", FT_STRING)])
    feats = []
    for label, lyr in layers:
        names = [f.name() for f in lyr.fields()]
        low = [n.lower() for n in names]
        pick = lambda cands: next((names[low.index(c)] for c in cands if c in low), None)
        nm = pick(("name", "facility", "fac_name", "plant_name", "station_name", "label"))
        tp = pick(("category", "type", "amenity", "facility_type", "naics_desc"))
        xf = QgsCoordinateTransform(lyr.crs(), crs, QgsProject.instance().transformContext())
        for f in lyr.getFeatures():
            g = f.geometry()
            if g is None or g.isNull():
                continue
            try:
                p = g if g.type() == GEOM_POINT else g.pointOnSurface()
                p.transform(xf)
            except Exception:
                continue
            q = QgsFeature(out.fields())
            q.setGeometry(p)
            q.setAttributes([label, "" if not nm or is_null(f[nm]) else str(f[nm]),
                             "" if not tp or is_null(f[tp]) else str(f[tp])])
            feats.append(q)
    out.dataProvider().addFeatures(feats)
    out.updateExtents()
    return out


def _fetch(key, ext, xing, big, out, progress):
    """(layer, label) for one data set - several services are merged."""
    if key == "nsi":
        return try_download("NSI", fetch_nsi, ext, out, progress_cb=progress)[0]
    if key == "address":
        return try_download("ND GIS Hub address points", fetch_nd_address_points, ext, out,
                            progress_cb=progress)[0]
    if key == "usa":
        return try_download("FEMA USA Structures", fetch_usa_structures, ext, out,
                            progress_cb=progress)[0]
    if key == "camps":
        return try_download("OpenStreetMap campgrounds", fetch_osm_camps, ext, out)[0]
    if key == "roads":
        return try_download("ND GIS Hub roads", fetch_arcgis_layer, ND_ROADS_URL, ext, out,
                            "ND_roads_NG911", progress_cb=progress)[0]
    if key == "traffic":
        for url in HPMS_ND_URLS:
            year = "".join(ch for ch in url.split("HPMS_FULL_ND_")[-1][:4] if ch.isdigit())
            lyr = try_download("HPMS {}".format(year), fetch_arcgis_layer, url, ext, out,
                               "HPMS_ND_{}".format(year))[0]
            if lyr is not None:
                return lyr
        if NDDOT_AADT_URL:
            url = "{}/{}".format(NDDOT_AADT_URL.rstrip("/"), NDDOT_AADT_LAYERS.get("aadt", 0))
            lyr = try_download("NDDOT traffic counts", fetch_arcgis_layer, url, ext, out,
                               "NDDOT_traffic_counts")[0]
            if lyr is not None and lyr.featureCount():
                return lyr
        return try_download("ND county roads (AADT)", fetch_arcgis_layer, ND_COUNTY_ROADS_URL,
                            ext, out, "ND_county_roads")[0]
    if key == "rail":
        return try_download("Rail lines", fetch_arcgis_layer, NARN_URL, ext, out,
                            "NARN_rail_lines", progress_cb=progress)[0]
    if key == "xing":
        return try_download("Grade crossings", fetch_arcgis_layer, CROSSINGS_URL, xing, out,
                            "FRA_grade_crossings", progress_cb=progress)[0]
    if key == "bridges":
        return _merge([("National Bridge Inventory",
                        try_download("National Bridge Inventory", fetch_nbi, ext, out)[0]),
                       ("OpenStreetMap", try_download("OpenStreetMap crossings",
                                                      fetch_osm_crossings, ext, out)[0])])
    if key == "dams":
        got = try_download("National Inventory of Dams", fetch_dams, ext, out)
        return got[0]
    if key == "lifelines":
        parts = []
        for k, label, url, _cat in lifeline_downloads():
            parts.append((label, try_download(label, fetch_arcgis_layer, url, ext, out,
                                              "lifeline_" + k)[0]))
        parts.append(("OpenStreetMap", try_download("OpenStreetMap lifelines",
                                                    fetch_osm_lifelines, ext, out)[0]))
        return _merge(parts)
    if key == "flowlines":
        return try_download("USGS 3DHP flowlines", fetch_arcgis_layer, NHD_FLOWLINE_URL, ext,
                            out, "3DHP_flowlines")[0]
    if key == "places":
        return try_download("OpenStreetMap places", fetch_osm_places, big, out)[0]
    return None


# the Data & Output data sets that the data-folder search can supply (datafolder role)
LOCAL_ROLE_OF_DATASET = {"nsi": "nsi", "address": "address", "usa": "usa", "camps": "camps",
                         "roads": "roads", "traffic": "aadt", "rail": "rail",
                         "xing": "crossings", "bridges": "bridges", "dams": "dams",
                         "flowlines": "centerline", "places": "places"}


def download_data(cfg, clip, depth_layer, output_folder, dam_folder="", progress=None,
                  log=None, add_layer=None):
    """Fetch the data sets switched on in `cfg` (pipeline cfg keys) for the
    study area and save them to <outputs>/Data. cfg also holds "data_area"
    (study / flood), "flood_buffer_ft" and "add_to_project". add_layer(path,
    base): how a saved file reaches the project (v2.6: a background run
    collects them for the main thread); None with add_to_project adds them
    here. Returns [(label, path or None, kept, total, note)]."""
    log = log or (lambda m: None)
    ext = clip.bbox_4326() if clip is not None else None
    if ext is None:
        raise RuntimeError("The depth raster has no usable coordinate system / extent - the "
                           "study area for the downloads is unknown.")
    xing = clip.bbox_4326(extra_ft=RAIL_CROSSING_SEARCH_MI * 5280.0)
    big = clip.bbox_4326(extra_ft=2.0 * 5280.0)
    ctx = QgsProject.instance().transformContext()
    use_flood = cfg.get("data_area") == AREA_FLOOD
    mask = None
    if use_flood:
        mask = FloodMask(depth_layer, float(cfg.get("flood_buffer_ft") or 0.0), ctx,
                         lat=clip.lat)
        if mask.ok:
            log(">>> Download area: flooded cells + {:g} ft (lines: study area)".format(
                float(cfg.get("flood_buffer_ft") or 0.0)))
        else:
            use_flood = False
    if not use_flood:
        log(">>> Download area: {}".format(clip.describe()))
    dam = dam_display_name(dam_folder or output_folder).replace(" ", "_")
    if add_layer is None and cfg.get("add_to_project", True):
        group = plugin_group(dam_display_name(dam_folder or output_folder), GRP_DATA)

        def add_layer(path, base):
            saved = QgsVectorLayer(path, base, "ogr")
            if saved.isValid():
                add_layer_to_project(saved, base, visibility=False, group=group)
    del STALE_USED[:]
    del PACK_USED[:]
    # v2.8.0: per-tile download cache from cfg (data_cache_folder, data_max_age_days,
    # refresh_data, data_cache_max_mb); off when data_cache_folder is absent / empty
    configure_tile_cache_from_cfg(cfg)
    todo = [d for d in DATASETS if cfg.get(d[3], True)]
    # v2.9: a data set the data folder supplies is not downloaded again
    local = cfg.get("local_data") or {}
    if local and dam_folder:
        # 2.17.0: the dam folder's data/ only where it holds the study area
        from .datafolder import drop_uncovered
        local, dropped, _kept = drop_uncovered(local, dam_folder, ext, xing)
        for n in dropped:
            log(">>> " + n)
    if local:
        from .datafolder import existing_roles
        usable = existing_roles(local)          # a pick whose file is gone is downloaded
        have = [d for d in todo if LOCAL_ROLE_OF_DATASET.get(d[0]) in usable]
        if have:
            log(">>> Found in the data folder, not downloaded: {}".format(
                ", ".join(d[2] for d in have)))
        todo = [d for d in todo if d not in have]
    rows = []
    n = max(1, len(todo))
    # v2.6: every data set is fetched side by side, then saved one by one
    from .download import parallel_map

    def fetch(key, label, progress_cb=None):
        return _fetch(key, ext, xing, big, output_folder, progress_cb or (lambda p, m="": None))
    got = parallel_map([(key, fetch, (key, label), {}) for key, _n, label, _f, _fl in todo],
                       progress_cb=lambda p, m: progress(0.7 * p, m) if progress else None,
                       label="Downloading data sets")
    for i, (key, name, label, _flag, floodable) in enumerate(todo):
        lo, hi = 70.0 + 30.0 * i / n, 70.0 + 30.0 * (i + 1) / n

        def sub(p, m="", lo=lo, hi=hi, label=label):
            if progress:
                progress(lo + (hi - lo) * max(0.0, min(100.0, p)) / 100.0,
                         "{}: {}".format(label, m) if m else label)
        ok, lyr = got.get(key, (False, None))
        if not ok:
            if lyr is not None:
                log_message("{}: {}".format(label, lyr), MSG_WARNING)
            lyr = None
        if lyr is None:
            rows.append((label, None, 0, 0, "download failed / service unavailable"))
            log(">>> {}: download failed (service unavailable or offline)".format(label))
            continue
        sub(90, "saving")
        try:
            path, kept, total = _save(lyr, name, dam, output_folder, clip, mask,
                                      use_flood and floodable, ctx, add_layer)
        except CancelledByUser:
            raise
        except Exception as e:
            log_message("{}: not saved: {}".format(label, e), MSG_WARNING)
            rows.append((label, None, 0, 0, "save failed: {}".format(e)))
            log(">>> {}: could not be saved ({})".format(label, e))
            continue
        note = "flooded area" if (use_flood and floodable) else "study area"
        rows.append((label, path, kept, total, note))
        log(">>> {}: {} of {} feature(s) in the {}{}".format(
            label, kept, total, note, " -> {}".format(os.path.relpath(
                path, output_folder)) if path else " - nothing saved"))
    for name, day, fname in STALE_USED:
        log(">>> WARNING: {}: the service did not answer - the download of {} was used "
            "({})".format(name, day, fname))
    for line in tile_cache_report_lines():
        log(line)
    from .pipeline import pack_report_lines
    for line in pack_report_lines():
        log(line)
    _write_index(rows, output_folder, use_flood, cfg)
    if progress:
        progress(100.0, "Downloads saved")
    return rows


def _write_index(rows, output_folder, use_flood, cfg):
    """Data/Downloaded_Data.csv: what was downloaded, when and for which area."""
    import csv
    import time
    from .helpers import prepare_output_path
    from .processing import DATA_INDEX_FILE
    path = prepare_output_path(os.path.join(data_folder(output_folder), DATA_INDEX_FILE))
    try:
        with open(path, "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.writer(fh)
            w.writerow(["Data set", "File", "Features kept", "Features downloaded", "Area",
                        "Date"])
            day = time.strftime("%Y-%m-%d")
            for label, p, kept, total, note in rows:
                w.writerow([label, os.path.basename(p) if p else "", kept, total, note, day])
            if use_flood:
                w.writerow(["(flood buffer {:g} ft)".format(
                    float(cfg.get("flood_buffer_ft") or 0.0)), "", "", "", "", ""])
    except OSError as e:
        log_message("Downloaded_Data.csv not written: {}".format(e), MSG_WARNING)
