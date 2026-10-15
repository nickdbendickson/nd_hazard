# -*- coding: utf-8 -*-
"""Headless hazard-classification run, shared by the dialog's Hazard
Classification tab and the Processing algorithm.

run_classification(cfg) does, for every scenario:
  study area -> downloads -> structure inventory (+ engineer review
  decisions) -> ACER zones (same-time method when data allows) ->
  structures, roads, rail -> consequences (economic, loss of life, sole
  access) -> incremental comparison with the no-breach baseline ->
  ND REG_05.2026 and ACER TM 11 classification
and writes the output layers, the summary (TXT / XLSX / CSV), the
completeness QA table and the PDF report.
"""

import json
import os
import re
import time

from qgis.core import QgsProject, QgsVectorLayer, QgsRasterLayer

from .constants import (
    NARN_URL, CROSSINGS_URL, TM11_PERSONS_PER_DWELLING, DEFAULT_MIN_UNVERIFIED_AREA_FT2,
    DEFAULT_VEHICLE_OCCUPANCY, DEFAULT_VEHICLE_SPEED_MPH, RAIL_CROSSING_SEARCH_MI,
    DEFAULT_EXTENT_BUFFER_FT, DEFAULT_NEAR_THRESHOLD_FT, DEFAULT_INCREMENTAL_DEPTH_FT,
    DEFAULT_ACCESS_SEARCH_FT, DEFAULT_WARNING_ISSUED_HR, ND_RANK, lifeline_downloads, DAM_SEARCH_FT,
    ND_ROADS_URL, HPMS_ND_URLS, ND_COUNTY_ROADS_URL,
    GRP_RESULTS, GRP_ZONES, GRP_SAMPLES, GRP_HYDRAULICS, GRP_EAP, STRUCTURES_FAMILY, SUBDIR_CLASSIFICATION, SUBDIR_EAP, SUBDIR_QA, OUTPUT_SUBDIRS, MAPBOOK_DEFAULT_SCALE, TRAVEL_STATION_MI, DEFAULT_PARCEL_MAX_ACRES,
    NHD_FLOWLINE_URL, NDDOT_AADT_URL, NDDOT_AADT_LAYERS, ZONE_LABELS,
)
from .compat import MSG_WARNING, environment_summary
from .helpers import (
    log_message, export_layer_to_formats, add_layer_to_project, dam_display_name,
    RasterSampler, apply_acer11_style, reproject_raster, plugin_group, prune_empty_groups,
    redirected_outputs, prepare_output_path, remove_project_layers_for_path,
    get_layer_path,
)
from .processing import (
    discover_project_layers, make_samplers, close_samplers, generate_hazard_rasters,
)
from .osm_processing import (
    get_osm_candidates, is_fema_structures_layer, download_structures_file,
)
from .download import (
    fetch_nsi, fetch_arcgis_layer, fetch_nd_address_points, fetch_usa_structures, try_download,
    fetch_dams, fetch_osm_lifelines, fetch_nbi, fetch_osm_crossings, STALE_USED, PACK_USED,
    fetch_osm_camps, fetch_osm_places, parallel_map, fetch_osm_trailer_parks,
)
from .helpers import CancelledByUser
from . import facilities as FAC
from . import boundary as BND
from . import eap as EAP
from .extent import ExtentClip
from .structures import (
    build_inventory, reinterpret, evaluate_structures, structures_to_layer, inventory_qa,
    write_qa_csv, structure_sample_points_layer, campground_records, evaluation_areas_layer,
    sampled_cells_layer, apply_type,
)
from .linear import analyze_roads, analyze_rail, roads_to_layer, rail_to_layer, FeatureIndex
from .classification import (
    classify_nd, classify_tm11, summary_lines, write_summary, extra_criteria, flood_cut_off_note,
)
from .styles import style_by_field, set_map_tip, style_outline
from .hazard import METHOD_MAXMAX, METHOD_LABEL
from .scenarios import (
    ROLE_BREACH, order_for_run, restore, mark_structures_incremental, raster_reuse_problems,
    line_baseline_depth, mark_lines_incremental, incremental_view, EVAL_KEYS,
)
from .consequences import economic_estimate, life_loss_estimate, totals
from .access import access_analysis
from .review import ReviewStore, review_path
from . import structures as ST
from . import sensitivity as SE
from . import parcelhints as PH
from . import facility_review as FR

DEFAULTS = {
    "output_folder": None, "dam_folder": None, "dam_name": None, "scenarios": [],
    "extent_buffer_ft": DEFAULT_EXTENT_BUFFER_FT, "keep_dry": True,
    "download_nsi": True, "download_address": True, "download_usa_structures": True,
    "download_rail": True, "download_crossings": True,
    "road_layer": None, "aadt_layer": None, "rail_layer": None, "crossing_layer": None,
    "extra_outline_layer": None, "osm_layer": "auto", "point_sources": "auto",
    "one_d": False, "judgment_as_probable": True,
    "persons_per_dwelling": TM11_PERSONS_PER_DWELLING,
    "min_area_ft2": DEFAULT_MIN_UNVERIFIED_AREA_FT2,
    "occupancy": DEFAULT_VEHICLE_OCCUPANCY, "speed_mph": DEFAULT_VEHICLE_SPEED_MPH,
    "near_ft": DEFAULT_NEAR_THRESHOLD_FT, "borderline_higher": False,
    "incremental_ft": DEFAULT_INCREMENTAL_DEPTH_FT,
    "lol": True, "warning_issued_hr": DEFAULT_WARNING_ISSUED_HR, "understanding": "vague",
    "severity_method": "graham", "population": "night", "high_severity": False,
    "access": True, "access_ft": DEFAULT_ACCESS_SEARCH_FT, "access_as_medium": True,
    "report_pdf": True, "report_pages": 8,
    "review_file": None, "add_to_project": True,
    "facility_review_file": None,       # v2.9: roads / facilities decisions (default: outputs)
    # v2.2
    "download_dams": True, "dam_layer": None, "subject_dam_nid": "",
    "download_lifelines": True, "lifeline_layer": None, "lifeline_as_medium": True,
    # 2.30.0: the alternative route and the second facility of the same kind, for every
    # lifeline facility the flood reaches (only computed when one is actually hit)
    "lifeline_detail": True,
    "model_boundary_layer": None, "eap": True,
    # v2.3
    "download_roads": True, "download_crossings_structures": True, "spreadsheets": True,
    "subfolders": True,
    # v2.5 - every EAP output can be switched off on its own ("eap" = all of them)
    "download_camps": True, "eap_list": True, "eap_mapbook": True,
    "mapbook_scale": MAPBOOK_DEFAULT_SCALE, "eap_travel": True, "centerline_layer": None,
    "download_flowlines": True, "download_places": True, "peak_raster": None,
    "travel_station_mi": TRAVEL_STATION_MI, "parcel_layer": None,
    "parcel_max_acres": DEFAULT_PARCEL_MAX_ACRES, "compare_last_run": True,
    # v2.6 - the ACER zone rasters (map product) and the cells-read layer;
    # re-use of the last run's evaluation when its inputs are unchanged
    "zone_rasters": True, "sampled_cells": True, "reuse_evaluation": True,
    "sample_area": "area",
    # v2.8 - analyses after the classification (all optional); screening values are the
    # engineer's choice, not requirements
    "sensitivity": False, "sensitivity_factors": "0.8, 0.9, 1.0, 1.1, 1.2",
    "sensitivity_split": False,
    "termination": False, "term_rise_ft": 2.0, "term_depth_ft": 2.0, "term_confirm": 3,
    "pop_source": None, "pop_field": "", "pop_day_field": "", "pop_housing_field": "",
    "pop_id_field": "", "pop_period": "night", "pop_raster_units": "per_cell",
    "pop_method": "auto", "pop_min_depth_ft": 0.0,
    "evacuation": False, "evac_prep_min": 15.0, "evac_passable_ft": 1.0,
    "evac_access_leg_mph": 10.0, "evac_include": "flooded", "evac_dry_safe_ft": 0.0,
    "evac_tight_min": 30.0,
    # v2.8 - downloads are cached per tile ("auto" = in the QGIS profile; "" = off)
    "data_cache_folder": "", "data_max_age_days": 7, "refresh_data": False,
    "data_cache_max_mb": 2000,
    # v2.8 - "Save and re-run": the PDF report and map book are not rebuilt
    "quick_rerun": False,
    # v2.7 - the evaluation is also kept on disk (outputs/QA) for the next QGIS
    # session; older than evaluation_max_age_days (default 7) it is not used
    "save_evaluation": True, "evaluation_max_age_days": None,
    # v2.7 - the engineer's own depth-damage curves for non-residential structures (CSV)
    "damage_curves_file": "",
    # v2.7 - DSS-WISE HCOM table(s) (';' separated) to cross-check the population at risk
    "hcom_files": "",
    # v2.9 - data sets found in a folder (datafolder.to_config): {role: uri, "lifelines":
    # [[uri, category], ...]}. Used before the QGIS layer tree and before any download.
    "local_data": {},
}

# v2.9 - the manufactured-home rules, the consequence sensitivity, parcel hints ...
DEFAULTS.update(ST.MOBILE_RULE_DEFAULTS)
DEFAULTS.update(SE.CONSEQ_DEFAULTS)
DEFAULTS.update(PH.DEFAULTS)
DEFAULTS.update({
    "download_trailer_parks": True,     # OpenStreetMap mobile-home parks (residential=trailer_park)
    "fast_sampling": False,             # vectorised ring / band cell selection (fastsample.py)
    "zone_overviews": True, "input_overviews": False,       # raster pyramids
    "report_imagery": True,             # aerial imagery under the maps of report / map book
    # v2.20.0: the structures on a copy of the depth-velocity spreadsheet and in the report
    "dv_spreadsheet": True, "dv_template": "",   # "" = templates/Hazard_Spreadsheet.xlsx
    "evac_routes": True, "web_export": False,
    "temporal_animation": False, "temporal_breach_start": "", "temporal_step_min": 15,
    "settings_profile": None,           # status of the settings profile in use (settings.py)
})
DEFAULTS.update({
    # v2.10: the dam suite
    "write_dam_record": True,           # dam.json: values and the "classified" status
    # v2.23.0: one switch for a quick run - no EAP map book, no aerial imagery under the maps
    # (the two slowest outputs). The two settings themselves are left as they are.
    "fast_run": False,
    "ensemble_member": False,           # set by ensemble.py on every ensemble run
})
from . import modelgate as _MG          # noqa: E402  (v2.10 model gate settings)
DEFAULTS.update(_MG.CFG_DEFAULTS)

CONSEQ_KEYS = ("val_struct", "val_cont", "ff_depth", "damage_usd", "damage_basis",
               "damage_nonres_usd", "par_night", "par_day", "lol_night", "lol_day", "severity",
               "warning_hr", "warning_cat", "fatality_rate", "par", "lol", "lol_low",
               "lol_high", "cut_off", "access_note", "access_unknown", "access_cut",
               "access_raw_note", "incremental", "incr_note", "base_depth", "base_zone",
               "cut_incremental")
SNAP_KEYS = EVAL_KEYS + CONSEQ_KEYS


def _eap_on(cfg, part):
    """An EAP output is made when EAP outputs are on AND that output is on.
    The map book also needs the notification list and the arrival maps."""
    if not cfg.get("eap"):
        return False
    if part == "list":
        return bool(cfg.get("eap_list", True) or cfg.get("eap_mapbook"))
    return bool(cfg.get("eap_" + part))


FAST_RUN_OFF = ("eap_mapbook", "report_imagery")


def fast_run_config(cfg):
    """cfg with the slow map outputs off when cfg["fast_run"] is set (a copy; cfg unchanged)."""
    if not cfg.get("fast_run"):
        return cfg
    out = dict(cfg)
    for k in FAST_RUN_OFF:
        out[k] = False
    return out


def config(**kw):
    """DEFAULTS updated with the given settings."""
    cfg = dict(DEFAULTS)
    cfg.update(kw)
    return cfg


def _snap(structs):
    return {rec["id"]: {k: rec.get(k) for k in SNAP_KEYS} for rec in structs}


def _crs_key(crs):
    try:
        return crs.authid() or crs.toWkt()
    except Exception:
        return ""


def _same_crs(a, b):
    """QGIS compares CRSs by definition (two custom WKTs of the same CRS are
    equal); the authority id / WKT text is the fallback."""
    try:
        if a == b:
            return True
    except Exception:
        pass
    return bool(_crs_key(a)) and _crs_key(a) == _crs_key(b)


def _same_crs_rasters(run, scenarios, layers, acrs):
    """Scenario rasters in another CRS than the first breach are copied into
    that CRS (nearest cell), so every sampler works with one set of
    coordinates."""
    if acrs is None or not acrs.isValid():
        return
    for sc in scenarios:
        lyr = layers[sc.name]["depth"]
        crs = lyr.crs() if lyr is not None else None
        if crs is None or not crs.isValid() or _same_crs(crs, acrs):
            continue
        folder = os.path.join(run.out, "_reprojected", sc.slug)
        os.makedirs(folder, exist_ok=True)
        if not getattr(sc, "orig_depth", None):
            sc.orig_depth = sc.depth          # v2.5: rasters beside it (time of peak)
        for key, attr in (("depth", "depth"), ("vel", "velocity"), ("arr", "arrival"),
                          ("dv", "dv")):
            path = getattr(sc, attr)
            src = layers[sc.name].get(key)
            if not path or src is None:
                continue
            setattr(sc, attr, reproject_raster(path, os.path.join(folder, key + ".tif"),
                                               src.crs() if src.crs().isValid() else crs,
                                               acrs, run.ctx))
        layers[sc.name] = sc.load_layers()
        run.log(">>> {}: rasters reprojected from {} to {} (nearest cell) - {}".format(
            sc.name, crs.authid() or "custom CRS", acrs.authid() or "custom CRS", folder))
        run.sources.append("{}: rasters reprojected to the first breach scenario's CRS "
                           "({})".format(sc.name, acrs.authid() or "custom"))


def _wet_extent(depth_layer):
    probe = RasterSampler(depth_layer)
    try:
        return probe.valid_extent(positive_only=True)
    finally:
        probe.close()


def _union_rect(clip_crs, scenarios, layers, ctx):
    """Depth extents of all scenarios in the clip CRS."""
    from qgis.core import QgsCoordinateTransform, QgsRectangle
    rect = None
    for sc in scenarios:
        lyr = layers[sc.name]["depth"]
        try:
            r = lyr.extent()
            xf = QgsCoordinateTransform(lyr.crs(), clip_crs, ctx)
            if not xf.isShortCircuited():
                r = xf.transformBoundingBox(r)
        except Exception:
            continue
        if rect is None:
            rect = QgsRectangle(r)
        else:
            rect = QgsRectangle(min(rect.xMinimum(), r.xMinimum()),
                                min(rect.yMinimum(), r.yMinimum()),
                                max(rect.xMaximum(), r.xMaximum()),
                                max(rect.yMaximum(), r.yMaximum()))
    return rect


class Run(object):
    """State of one classification run.

    v2.6: the run may execute in a background thread (dialog: QgsTask).
    Everything that touches the QGIS project - adding layers, layouts, the
    PDF report, the map book, layer-tree groups - is then recorded in
    `post` and done on the main thread by finish_run(); with deferred=False
    (Processing algorithms, scripts) it happens at once."""

    def __init__(self, cfg, progress=None, log=None, deferred=False):
        self.cfg = cfg
        self._progress = progress
        self._log = log
        self.ctx = QgsProject.instance().transformContext()
        self.out = cfg["output_folder"]
        self.deferred = bool(deferred)
        self.post = []                  # callables for the main thread (in order)
        self.added = {}                 # path -> the layer added to the project
        self.sources = []
        self.outputs = []
        self.layers_out = {}
        self.results = {}
        self.messages = []
        self.subject_dam = None
        self.forced_1d = False          # v2.5.1: no velocity raster -> 1D criterion
        self.crossings_evaluated = False
        self.clip_crs = None
        self.clip_lat = None
        self.reused = None              # v2.6: time of the evaluation re-used, or None
        self.eval_key = None            # v2.12: pipeline.evaluation_key of this run
        self.input_digests = {}         # v2.12: {raster path: content digest}
        self.local = {}                 # v2.9: layers of the data folder (roles without a cfg key)
        self.local_lines = []           # v2.9: what the data folder supplied (run log / sources)
        self.local_notes = []
        self.local_roles = set()        # v2.9: data-folder roles that opened
        self.structs = None             # v2.9: the structure records (stage B)
        self.facility_review = None
        self.used_inputs = {}
        self.marks = []                 # v2.6.2: (monotonic time, stage) for the run log
        self.damage_curves = None       # v2.7: the engineer's non-residential curves
        self.flood_cut_off = None       # v2.28: the Runner's run_report.json "cut_off", when any

    def tick(self, pct, msg):
        """A main stage of the run (timed in the run log) and progress."""
        if not self.marks or self.marks[-1][1] != msg:
            self.marks.append((time.monotonic(), msg))
        self._report(pct, msg)

    def mark(self, msg):
        """A timed step without a progress change (run log TIMINGS)."""
        self.marks.append((time.monotonic(), msg))

    def _report(self, pct, msg):
        if self._progress:
            self._progress(pct, msg)

    def timings(self):
        """[(stage, seconds)] from the stage marks (the last runs until now)."""
        out = []
        now = time.monotonic()
        for i, (t, msg) in enumerate(self.marks):
            end = self.marks[i + 1][0] if i + 1 < len(self.marks) else now
            out.append((msg, max(0.0, end - t)))
        return out

    def log(self, msg):
        log_message(msg)
        self.messages.append("{}  {}".format(time.strftime("%H:%M:%S"), msg))
        if self._log:
            self._log(msg)

    def stage(self, lo, hi):
        # sub-steps report progress only (the main stage keeps the time)
        return lambda p, m: self._report(lo + (hi - lo) * max(0.0, min(100.0, p)) / 100.0, m)

    # ---- project work: now or later ----------------------------------------
    def later(self, fn):
        """Run fn() on the main thread at the end (deferred) or now."""
        if self.deferred:
            self.post.append(fn)
        else:
            fn()

    def show_layer(self, layer, base, category, visibility=True, style=None, maptip=None,
                   path=None, raster=False):
        """Add a result layer to the project in the dam's group. `layer` is
        the layer to show now; deferred runs re-open `path` on the main
        thread instead (a layer made in a worker thread is not handed to the
        project). style: callable(layer); maptip: MAP_TIPS key."""
        if not self.cfg.get("add_to_project"):
            return
        dam_name = self.dam_name
        src = path or (get_layer_path(layer) if layer is not None and not raster else None)
        if self.deferred and not src:
            try:
                src = layer.source().split("|")[0] if layer is not None else None
            except Exception:
                src = None
            if not src or not os.path.isfile(src):
                self.log(">>> NOTE: {} was not saved to a file and is not shown on the map"
                         .format(base))
                return

        def add(lyr=layer, base=base, category=category, visibility=visibility, style=style,
                maptip=maptip, src=src, raster=raster):
            if self.deferred or lyr is None:
                lyr = QgsRasterLayer(src, base) if raster else QgsVectorLayer(src, base, "ogr")
                if not lyr.isValid():
                    log_message("{} could not be loaded from {}".format(base, src), MSG_WARNING)
                    return
            if style is not None:
                try:
                    style(lyr)
                except Exception as e:
                    log_message("Could not style {}: {}".format(base, e))
            if maptip:
                set_map_tip(lyr, maptip)
            if add_layer_to_project(lyr, base, visibility=visibility,
                                    group=plugin_group(dam_name, category)) and src:
                self.added[os.path.normcase(os.path.abspath(src))] = lyr
        self.later(add)


RUN_LOG = "Hazard_Classification_Run_Log.txt"

# v2.6: the evaluated state of the last run per outputs folder (downloads,
# inventory, raster sampling, roads / rail / facilities), re-used by the
# next run when the rasters, layers and the settings that shape it are
# unchanged. In memory only - it lives as long as QGIS does.
_EVAL_CACHE = {}
_EVAL_KEYS = ("extent_buffer_ft", "keep_dry", "download_nsi", "download_address",
              "download_usa_structures", "download_rail", "download_crossings",
              "download_roads", "download_crossings_structures", "download_dams",
              "download_lifelines", "download_camps", "download_flowlines", "download_places",
              "osm_layer", "point_sources", "subject_dam_nid", "occupancy", "speed_mph",
              "access", "access_ft", "incremental_ft", "eap", "eap_travel", "sampled_cells",
              "sample_area", "sensitivity", "termination", "refresh_data", "local_data",
              "mobile_shape", "mobile_max_width_ft", "mobile_min_ratio", "mobile_min_length_ft",
              "mobile_max_length_ft", "mobile_lot_units", "download_trailer_parks",
              "fast_sampling")
_EVAL_LAYERS = ("road_layer", "aadt_layer", "rail_layer", "crossing_layer",
                "extra_outline_layer", "dam_layer", "lifeline_layer", "centerline_layer",
                "model_boundary_layer")


def _layer_sig(lyr):
    """What identifies an input layer: a file's path, size, time and digest;
    for a layer without a file (memory, service) a digest of every
    feature's attributes and bounding box."""
    if lyr is None:
        return None
    try:
        src = lyr.source().split("|")[0]
        sig = [lyr.name(), lyr.source(), lyr.featureCount()]
        if src and os.path.isfile(src):
            return sig + _file_sig(src)
        import hashlib
        h = hashlib.md5()
        for f in lyr.getFeatures():
            h.update(repr(f.attributes()).encode("utf-8", "replace"))
            g = f.geometry()
            if g is not None and not g.isNull():
                bb = g.boundingBox()
                h.update("{:.4f},{:.4f},{:.4f},{:.4f}".format(
                    bb.xMinimum(), bb.yMinimum(), bb.xMaximum(), bb.yMaximum()).encode())
        return sig + [h.hexdigest()]
    except Exception:
        return [repr(lyr)]


def _vrt_sources_sig(path, limit=2000):
    """Size and time of the files a VRT reads (a tile rewritten behind an
    unchanged VRT is a changed raster)."""
    import re
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return None
    out = []
    base = os.path.dirname(path)
    for rel, name in re.findall(
            r'<SourceFilename[^>]*?relativeToVRT="(\d)"[^>]*>([^<]+)</SourceFilename>',
            text)[:limit]:
        src = os.path.join(base, name.strip()) if rel == "1" else name.strip()
        try:
            st = os.stat(src)
            out.append([name.strip(), st.st_size, getattr(st, "st_mtime_ns",
                                                          int(st.st_mtime))])
        except OSError:
            out.append([name.strip(), None])
    return out


def _file_sig(path):
    """v2.12: path, size and CONTENT digest (damsuite.chain.file_digest -
    SHA-256, sampled above 2 GB; fingerprint.file_sig) - no modification
    time: a file touched without a change is the same input, a raster
    rewritten with other values is another one even when its size and time
    stamp did not change. Until 2.11: path, size, mtime and an MD5 of the
    first and last 64 KB."""
    from . import fingerprint as FP
    return FP.file_sig(path)


# v2.6.2: raster products (ACER zone rasters, EAP arrival bands / contours)
# are re-used when they were made by this plugin version from the same input
# rasters and are unchanged since - a review / settings re-run then skips
# the whole-raster passes that made them
PRODUCTS_FILE = "_products.json"


def _product_sig(inputs, extra=None):
    from .constants import PLUGIN_VERSION
    return {"version": PLUGIN_VERSION, "extra": extra,
            "inputs": [_file_sig(p) if p else None for p in inputs]}


def _stamp(path):
    """v2.12: [size, SHA-256] of a product file (content, not date)."""
    from . import fingerprint as FP
    return FP.stamp(path)


def _products_load(folder):
    import json
    try:
        with open(os.path.join(folder, PRODUCTS_FILE), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _same_file(a, b):
    return bool(a) and bool(b) and \
        os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def reusable_products(folder, key, sig, expected=None):
    """{name: path} of product `key` an earlier run made from the same
    inputs (sig), when every file is still there unchanged - and, with
    `expected` ({name: normal path}), under its normal name; else None."""
    ent = _products_load(folder).get(key)
    if not isinstance(ent, dict) or ent.get("sig") != sig:
        return None
    paths = ent.get("paths") or {}
    files = ent.get("files") or {}
    if not paths:
        return None
    for name, p in paths.items():
        if not p or _stamp(p) is None or _stamp(p) != files.get(p):
            return None
        if expected is not None and not _same_file(p, expected.get(name)):
            return None
    return dict(paths)


def remember_products(folder, key, sig, paths, expected=None):
    """Record what product `key` was made from (never fails the run). A
    product written under another name (the normal file was locked) is not
    recorded: the next run writes the normal file again."""
    import json
    if expected is not None and any(not _same_file(p, expected.get(n))
                                    for n, p in paths.items()):
        forget_products(folder, key)
        return
    try:
        data = _products_load(folder)
        data[key] = {"sig": sig, "paths": dict(paths),
                     "files": {p: _stamp(p) for p in paths.values() if p}}
        target = os.path.join(folder, PRODUCTS_FILE)
        tmp = target + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=1)
        os.replace(tmp, target)
    except (OSError, TypeError, ValueError) as e:
        log_message("Product record not written: {}".format(e), MSG_WARNING)


def forget_products(folder, key):
    if key in _products_load(folder):
        remember_products(folder, key, None, {})


def _local_data_sig(local):
    """The data-folder picks with the size / date of each file, so a replaced file
    is a changed input."""
    out = []
    try:
        from . import datafolder as DF
        for role in sorted(local or {}):
            v = local[role]
            items = [v] if isinstance(v, str) else list(v or [])
            for it in items:
                uri, cat = (it[0], it[1]) if isinstance(it, (list, tuple)) else (it, None)
                pth = DF._file_of_uri(uri) if isinstance(uri, str) else ""
                out.append([role, uri, cat, _file_sig(pth) if pth and os.path.isfile(pth)
                            else None])
    except Exception:
        return repr(local)
    return out


def _plan_sig(sc):
    """v2.9: a HEC-RAS plan used directly - its conversion options and terrain
    raster shape the rasters, so they are part of the key."""
    plan = getattr(sc, "plan", None)
    if not plan:
        return []
    ps = dict(getattr(sc, "plan_settings", None) or {})
    return [_file_sig(plan), sorted((str(k), repr(v)) for k, v in ps.items()),
            _file_sig(ps.get("dem")) if ps.get("dem") else None]


def evaluation_key(cfg):
    """Hash of everything that shapes the evaluation stage (rasters, input
    layers, download choices, the settings the sampling and the road / rail
    / facility analyses use). Settings applied afterwards (habitability
    rules, review decisions, 1D / 2D, borderline, consequences, judgment,
    report and EAP options) are not part of it."""
    import hashlib
    import json
    parts = {"version": 2}
    parts["scenarios"] = [[sc.name, sc.role, sc.baseline] + [
        _file_sig(pth) for pth in (sc.depth, sc.velocity, sc.arrival, sc.dv) if pth]
        + _plan_sig(sc) for sc in cfg.get("scenarios") or []]
    for k in _EVAL_KEYS:
        v = cfg.get(k)
        if k == "point_sources" and isinstance(v, (list, tuple)):
            v = [[fam, _layer_sig(lyr)] for fam, lyr in v]
        elif k == "osm_layer" and v not in (None, "auto"):
            v = _layer_sig(v)
        elif k == "local_data":
            v = _local_data_sig(v)
        parts[k] = v
    for k in _EVAL_LAYERS:
        parts[k] = _layer_sig(cfg.get(k))
    parts["out"] = os.path.normcase(os.path.abspath(cfg["output_folder"] or ""))
    parts["dam_folder"] = os.path.normcase(os.path.abspath(cfg.get("dam_folder") or ""))
    from .constants import SOURCE_OVERRIDES
    parts["sources"] = sorted((k, str(v)) for k, v in SOURCE_OVERRIDES.items())
    try:
        blob = json.dumps(parts, sort_keys=True, default=str)
    except Exception:
        blob = repr(parts)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def forget_evaluation(output_folder=None):
    """Drop the cached evaluation (of one outputs folder, or all)."""
    if output_folder is None:
        _EVAL_CACHE.clear()
    else:
        _EVAL_CACHE.pop(os.path.normcase(os.path.abspath(output_folder)), None)


def run_classification(cfg, progress=None, log=None, deferred=False):
    """Returns a dict: governing, per-scenario results, lines, outputs,
    structs, layers. Every run (also a failed one) leaves
    outputs/Hazard_Classification_Run_Log.txt: settings, scenarios,
    warnings and the result - a record for the submittal file.

    deferred=True (background thread): the project work is left in
    res["post"]; call finish_run(res) on the main thread to add the layers,
    make the map book and the PDF report and write the summary and the run
    log. deferred=False: everything is done here."""
    cfg = fast_run_config(cfg)
    run = Run(cfg, progress, log, deferred=deferred)
    if cfg.get("fast_run"):
        run.messages.append("Fast run: the EAP map book and the aerial imagery under the "
                            "maps are skipped. Every result is the same.")
    os.makedirs(run.out, exist_ok=True)
    try:
        res = _run_classification(run, cfg)
    except Exception as e:
        run.messages.append("ERROR: {!r}".format(e))
        _write_run_log(run, cfg, "FAILED")
        raise
    res["run"] = run
    res["post"] = run.post
    if not deferred:
        finish_run(res, progress=progress, log=log)
    return res


def finish_run(res, progress=None, log=None):
    """Main-thread part of a run started with deferred=True: layers into the
    project, EAP map book, summary, PDF report, run log. Updates and returns
    `res` (lines, outputs, report, notes, result_items). Safe to call for a
    run made with deferred=False (nothing left to do)."""
    run = res["run"]
    if progress is not None:
        run._progress = progress
    if log is not None:
        run._log = log
    status = "FAILED"
    try:
        for fn in list(run.post):
            fn()
        del run.post[:]                       # res["post"] is this list
        status = "ND {} / TM 11 {} (governing scenario {})".format(
            res["nd"]["cls"], res["tm11"]["cls"], res["governing"])
        return res
    except Exception as e:
        run.messages.append("ERROR: {!r}".format(e))
        raise
    finally:
        _write_run_log(run, run.cfg, status)
        # the cached state keeps no layer objects (they keep GeoPackages open
        # and belong to this thread); res["results"] keeps them for the dialog
        folder_key = os.path.normcase(os.path.abspath(run.out))
        cached = _EVAL_CACHE.get(folder_key)
        if cached is not None and cached["state"]["results"] is run.results:
            cached["state"]["results"] = {
                k: {kk: vv for kk, vv in r.items() if kk != "layers"}
                for k, r in run.results.items()}
        run.added = {}


def _sub(out, name):
    """outputs/<name>, created when missing."""
    path = os.path.join(out, name)
    os.makedirs(path, exist_ok=True)
    return path


def _retire_everywhere(out, base, exts=("gpkg", "csv", "xlsx"), keep=None):
    """_retire_output in outputs and each of its sub-folders (files written
    before v2.3 sat in outputs itself), except in `keep`."""
    for folder in [out] + [os.path.join(out, d) for d in OUTPUT_SUBDIRS]:
        if keep is not None and os.path.normcase(os.path.abspath(folder)) == \
                os.path.normcase(os.path.abspath(keep)):
            continue
        if os.path.isdir(folder):
            _retire_output(folder, base, exts)


def _retire_output(out, base, exts=("gpkg", "csv", "xlsx")):
    """An output that is empty this run: remove the previous run's layer and
    files so the map and the folder never show stale results."""
    for ext in exts:
        path = os.path.join(out, "{}.{}".format(base, ext))
        remove_project_layers_for_path(path)
        if os.path.exists(path):
            try:
                os.remove(path)
                log_message("Removed {} (nothing to report this run)".format(
                    os.path.basename(path)))
            except OSError as e:
                log_message("Old {} could not be removed ({}) - it is not part of this "
                            "run".format(os.path.basename(path), e), MSG_WARNING)


def _remove_layers_under(folder):
    """Remove project layers reading files inside `folder`."""
    from .helpers import layer_file_path, on_main_thread
    if not on_main_thread():
        return
    root = os.path.normcase(os.path.realpath(folder)) + os.sep
    ids = []
    for lid, lyr in QgsProject.instance().mapLayers().items():
        p = layer_file_path(lyr)
        if p and os.path.normcase(os.path.realpath(p)).startswith(root):
            ids.append(lid)
    if ids:
        QgsProject.instance().removeMapLayers(ids)


def _write_run_log(run, cfg, status):
    try:
        from .constants import PLUGIN_NAME, PLUGIN_VERSION
        lines = ["{} v{} - hazard classification run".format(PLUGIN_NAME, PLUGIN_VERSION),
                 "Finished: {}".format(time.strftime("%Y-%m-%d %H:%M:%S")),
                 "Result: {}".format(status), "", environment_summary(), "", "SETTINGS"]
        for k in sorted(cfg):
            v = cfg[k]
            if k == "scenarios":
                for sc in v or []:
                    lines.append("  scenario: {}".format(sc.to_dict()))
                continue
            if callable(getattr(v, "name", None)):
                v = "layer '{}'".format(v.name())
            elif isinstance(v, (list, tuple)) and v and not isinstance(v[0], str):
                v = "{} item(s)".format(len(v))
            lines.append("  {} = {}".format(k, v))
        # v2.6.2: where the time went (stages of this run, main-thread part included)
        tim = run.timings() if hasattr(run, "timings") else []
        if tim:
            lines += ["", "TIMINGS (s)"] + ["  {:8.1f}  {}".format(sec, msg)
                                           for msg, sec in tim if sec >= 0.05]
            lines.append("  {:8.1f}  total".format(sum(sec for _m, sec in tim)))
        lines += ["", "MESSAGES"] + ["  " + m for m in run.messages]
        lines += ["", "DATA SOURCES"] + ["  - " + s for s in run.sources]
        path = prepare_output_path(os.path.join(run.out, RUN_LOG))
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(str(x) for x in lines) + "\n")
    except Exception as e:                   # the log must never fail the run
        log_message("Run log not written: {}".format(e), MSG_WARNING)


def _run_classification(run, cfg):
    """Stage A (evaluate: rasters, downloads, inventory, sampling, roads /
    rail / facilities), re-used from the cache when its inputs are unchanged;
    stage B (classify and write) always."""
    out = run.out
    redirected_outputs(clear=True)
    _read_dam_record(run, cfg)                # v2.10: dam.json (name, NID) when not given
    _flood_cut_off_step(run, cfg)             # v2.28: the Runner's run_report.json "cut_off"
    run.dam_name = cfg.get("dam_name") or dam_display_name(cfg.get("dam_folder") or out)
    log_message(environment_summary())
    scenarios = order_for_run([s for s in cfg["scenarios"]])
    breaches = [s for s in scenarios if s.role == ROLE_BREACH]
    if not breaches:
        raise RuntimeError("No breach scenario defined.")
    problems = [p for s in scenarios for p in s.missing()]
    reuse_warnings = []
    problems += raster_reuse_problems(scenarios, reuse_warnings)          # v2.9.1
    if problems:
        raise RuntimeError("; ".join(problems))
    for w in reuse_warnings:
        run.log(">>> WARNING: " + w)
    gres = _MG.pipeline_step(cfg, log=run.log)        # v2.10: model gate (opt-in)
    if gres is not None and gres.get("csv"):
        run.sources.append("Model gate: {} (QA/{})".format(gres.get("status"),
                                                          os.path.basename(gres["csv"])))
    _apply_local_data(run, cfg)              # v2.9: data-folder layers into the cfg
    key = evaluation_key(cfg)
    run.eval_key = key
    _log_input_digests(run, scenarios)       # v2.12: content digests of the rasters
    folder_key = os.path.normcase(os.path.abspath(out))
    reuse = bool(cfg.get("reuse_evaluation", True))
    cached = _EVAL_CACHE.get(folder_key) if reuse else None
    from_disk = False
    if reuse and (cached is None or cached["key"] != key):
        cached = _load_saved_evaluation(run, cfg, key)      # v2.7: an earlier QGIS session
        if cached is not None:
            from_disk = True
            _EVAL_CACHE[folder_key] = cached
    if cached is not None and cached["key"] == key:
        state = cached["state"]
        run.reused = cached["time"]
        run.log(">>> Re-using the evaluation of {}{} (rasters, data and settings unchanged - "
                "the rasters and files by their content digests, not their dates): downloads, "
                "inventory and raster sampling skipped - untick 'Re-use the last "
                "evaluation' for a full run".format(
                    cached["time"], " (saved by an earlier QGIS session)" if from_disk else ""))
        run.sources.extend(state["sources"])
        run.sources.append("Evaluation re-used from the run of {}".format(cached["time"]))
        run.forced_1d = state["forced_1d"]
        run.subject_dam = state["subject_dam"]
        run.crossings_evaluated = state["crossings_evaluated"]
        run.clip_crs = state["acrs"]
        run.clip_lat = state["clip_lat"]
        run.tick(20, "Evaluation re-used")
    else:
        state = _evaluate(run, cfg, scenarios, breaches)
        if reuse:
            when = time.strftime("%Y-%m-%d %H:%M")
            _EVAL_CACHE[folder_key] = {"key": key, "state": state, "time": when}
            _save_evaluation(run, cfg, key, state, when)
    return _classify_and_write(run, cfg, state)


# data-folder role -> the pipeline setting that takes a layer (the other roles - nsi,
# address, usa, camps, bridges, lifelines - are read where the download would be)
LOCAL_CFG_KEYS = {"roads": "road_layer", "aadt": "aadt_layer", "rail": "rail_layer",
                  "crossings": "crossing_layer", "outlines": "extra_outline_layer",
                  "dams": "dam_layer", "boundary": "model_boundary_layer",
                  "parcels": "parcel_layer", "centerline": "centerline_layer"}


def _apply_local_data(run, cfg):
    """v2.9: open the layers of cfg["local_data"] (the Data & Output tab's folder search)
    and let them take the place of the QGIS layer-tree layer and of the download: order
    of use is data folder, QGIS layer tree, internet. A layer the engineer picked by hand
    in a list is not in local_data (the dialog leaves that role out)."""
    run.local, run.local_lines, run.local_notes, run.local_roles = {}, [], [], set()
    local = cfg.get("local_data") or {}
    if not local:
        return
    from . import datafolder as DF
    dam = cfg.get("dam_folder") or ""
    # 2.17.0: a layer of the dam folder's data/ (Get data for this dam) only when its area
    # holds the study area; otherwise that data set is read / downloaded as before
    if dam and any(DF.is_dam_data(u, dam) for v in local.values()
                   for u in ([x[0] for x in v if isinstance(x, (list, tuple)) and x]
                             if isinstance(v, (list, tuple)) else [v])):
        box, xbox = _study_boxes(run, cfg)
        local, dropped, _kept = DF.drop_uncovered(local, dam, box, xbox)
        cfg["local_data"] = local
        for n in dropped:
            run.log(">>> " + n)
        if not local:
            return
    layers, notes = DF.open_config(local)
    run.local_roles = set(layers)
    run.local_notes = ["Data folder: " + n for n in notes]

    def where(lyr):
        return "dam-folder layer" if DF.is_dam_data(lyr.source(), dam) else "data-folder layer"

    def mark(lyr):
        return " (from dam folder)" if DF.is_dam_data(lyr.source(), dam) else ""
    for role, lyr in layers.items():
        if role == "lifelines":
            run.local[role] = list(lyr)
            for one, cat in lyr:
                run.local_lines.append("Lifeline facilities: {} '{}'{}{}".format(
                    where(one), one.name(), " ({})".format(cat) if cat else "", mark(one)))
            continue
        label = DF.ROLE_LABELS.get(role, role)
        run.local_lines.append("{}: {} '{}'{}".format(label, where(lyr), lyr.name(), mark(lyr)))
        key = LOCAL_CFG_KEYS.get(role)
        if key:
            cfg[key] = lyr
        else:
            run.local[role] = lyr


def _study_boxes(run, cfg):
    """2.17.0: (study area, grade-crossing search area) as lon / lat boxes - the same
    union of the scenarios' depth rasters and buffer stage A clips to - or (None, None)."""
    try:
        scenarios = order_for_run(list(cfg.get("scenarios") or []))
        breaches = [s for s in scenarios if s.role == ROLE_BREACH]
        layers = {s.name: s.load_layers() for s in scenarios}
        dlyr = layers[breaches[0].name]["depth"]
        rect = _union_rect(dlyr.crs(), scenarios, layers, run.ctx)
        clip = ExtentClip(dlyr, cfg["extent_buffer_ft"], run.ctx, rect=rect)
        return clip.bbox_4326(), clip.bbox_4326(extra_ft=RAIL_CROSSING_SEARCH_MI * 5280.0)
    except Exception:                                        # noqa - unknown: not used
        return None, None


def _log_input_digests(run, scenarios):
    """v2.12: the content digests of the scenario rasters (what the
    evaluation key and the re-use decision rest on) into the run and its
    log; kept on `run.input_digests` for QA/Run_Settings.json."""
    from . import fingerprint as FP
    paths = [pth for sc in scenarios for pth in (sc.depth, sc.velocity, sc.arrival, sc.dv)
             if pth and isinstance(pth, str)]
    try:
        run.input_digests = FP.digest_records(paths)
    except Exception as e:                                   # noqa - never fails the run
        run.input_digests = {}
        run.log(">>> NOTE: input digests not taken ({})".format(e))
        return
    for pth, d in run.input_digests.items():
        run.log(">>> Input raster {}: {}".format(os.path.basename(pth), FP.short(d)))
    if any(d.get("sampled") for d in run.input_digests.values()):
        run.log(">>> NOTE: a raster over 2 GB is " + FP.SAMPLED_NOTE)


def _save_evaluation(run, cfg, key, state, when):
    """v2.7: keep the evaluation on disk for the next QGIS session (never
    fails the run; taken before stage B changes the records)."""
    if not cfg.get("save_evaluation", True):
        return
    from . import evalcache
    from .constants import PLUGIN_VERSION
    run.mark("Saving the evaluation for the next session")
    path, info = evalcache.save(run.out, key, state, when, PLUGIN_VERSION)
    if path:
        run.log(">>> Evaluation saved for the next QGIS session ({:.1f} MB): {}".format(
            info / 1048576.0, os.path.relpath(path, run.out)))
    else:
        run.log(">>> NOTE: the evaluation could not be kept on disk ({}) - it is re-used "
                "only in this QGIS session".format(info))


def _load_saved_evaluation(run, cfg, key):
    """v2.7: the evaluation an earlier QGIS session saved, when it is for
    exactly these inputs, settings and this plugin version, is not too old
    and the files it points at are still there; else None."""
    if not cfg.get("save_evaluation", True):
        return None
    from . import evalcache
    from .constants import PLUGIN_VERSION
    days = cfg.get("evaluation_max_age_days")
    state, when, why = evalcache.load(
        run.out, key, PLUGIN_VERSION,
        max_age_days=evalcache.MAX_AGE_DAYS if days is None else float(days))
    if state is None:
        if why:
            run.log(">>> The evaluation saved by an earlier session is not used: {}".format(why))
        return None
    missing = [p for sc in state.get("scenarios") or [] for p in sc.missing()]
    if missing or not evalcache.files_exist(state):
        run.log(">>> The evaluation saved by an earlier session is not used: files it needs "
                "are gone ({})".format("; ".join(missing[:2]) or "channel flowlines"))
        return None
    return {"key": key, "state": state, "time": when}


def _evaluate(run, cfg, scenarios, breaches):
    """Stage A. Returns the state stage B needs (nothing project-bound)."""
    out = run.out
    layers = {s.name: s.load_layers() for s in scenarios}
    for s in scenarios:
        bad = [k for k, lyr in layers[s.name].items() if lyr is not None and not lyr.isValid()]
        if bad:
            raise RuntimeError("{}: raster(s) could not be loaded: {}".format(s.name, ", ".join(bad)))
    if cfg.get("input_overviews"):
        _input_overviews(run, scenarios)
    primary = breaches[0]
    if layers[primary.name].get("vel") is None and not cfg.get("one_d"):
        # v2.5.1: ACER zones come from Dmax / Vmax only - without a velocity
        # raster the depth-only (1D) criterion is used rather than velocity 0
        run.forced_1d = True
        run.log(">>> WARNING: no velocity raster - ACER zones need max depth with max "
                "velocity (Dmax / Vmax); the depth-only (1D) criterion (depth > 2 ft) is used")
        run.sources.append("No velocity raster: depth-only (1D) criterion used (ACER zones "
                           "need Dmax / Vmax)")
    one_d = bool(cfg.get("one_d")) or run.forced_1d
    dlyr = layers[primary.name]["depth"]
    acrs = dlyr.crs()
    run.clip_crs = acrs
    run.clip_lat = None
    _same_crs_rasters(run, scenarios, layers, acrs)
    rect = _union_rect(acrs, scenarios, layers, run.ctx)
    clip = ExtentClip(dlyr, cfg["extent_buffer_ft"], run.ctx, rect=rect)
    run.clip_lat = clip.lat
    run.sources.append("Rasters ({}): {}".format(primary.name, ", ".join(
        lyr.source() for lyr in (layers[primary.name]["depth"], layers[primary.name]["vel"],
                                 layers[primary.name]["arr"]) if lyr is not None)))
    for s in scenarios:
        if s is primary:
            continue
        run.sources.append("Scenario '{}' ({}{}): {}".format(
            s.name, s.role, ", baseline '{}'".format(s.baseline) if s.baseline else "",
            s.depth))
    run.sources.append("Study area: {} - every structure layer and download is limited "
                       "to it".format(clip.describe()))
    from .constants import SOURCE_OVERRIDES, sources_file_path
    if SOURCE_OVERRIDES:
        run.sources.append("Data-source addresses changed in {}: {}".format(
            sources_file_path(), ", ".join(sorted(SOURCE_OVERRIDES))))
    run.tick(1, "Finding the inundated area...")
    probe = RasterSampler(dlyr)
    try:
        ext = probe.valid_extent(positive_only=True)
    finally:
        probe.close()
    if ext is None:
        raise RuntimeError("The depth raster has no cells with depth > 0.")

    # ---- downloads and project layers -------------------------------------
    from .helpers import prime_ellipsoids
    prime_ellipsoids(acrs.ellipsoidAcronym())   # v2.20.1: before the download threads
    inputs = _collect_inputs(run, clip)

    # ---- structure inventory -----------------------------------------------
    run.tick(20, "Building the structure inventory...")
    rules = ST.mobile_rules_from_cfg(cfg)          # v2.9: manufactured-home rules
    structs = build_inventory(inputs["outlines"], inputs["points"], acrs, run.ctx,
                              progress_cb=run.stage(20, 28), clip=clip, mobile_rules=rules)
    if inputs.get("camps") is not None:
        camps = campground_records(inputs["camps"], acrs, run.ctx, clip,
                                   start_id=len(structs) + 1)
        structs.extend(camps)
        if camps:
            run.log(">>> {} campground(s) / RV park(s) in the study area added as overnight-"
                    "recreation structures (5.2.1.1)".format(len(camps)))
    # the automatic reading with the default rules; stage B applies the
    # engineer's decisions and the settings (habitability, persons)
    reinterpret(structs, **rules)
    if inputs.get("parks") is not None:
        try:
            n_park = ST.mark_mobile_parks(structs, inputs["parks"], acrs, run.ctx, clip=clip,
                                          **rules)
            run.log(">>> {} structure(s) inside an OpenStreetMap mobile-home park".format(n_park))
        except CancelledByUser:
            raise
        except Exception as e:
            run.log(">>> WARNING: mobile-home parks not applied ({})".format(e))

    # ---- scenarios: sampling, roads, rail, facilities, access ----------------
    cross_idx = FeatureIndex(inputs["crossings"], acrs, run.ctx, 5280.0) \
        if inputs["crossings"] is not None else None
    n = len(scenarios)
    for i, sc in enumerate(scenarios):
        lo, hi = 30 + 50.0 * i / n, 30 + 50.0 * (i + 1) / n
        res = _run_scenario(run, sc, layers[sc.name], structs, acrs, clip, inputs, cross_idx,
                            run.stage(lo, hi), one_d)
        run.results[sc.name] = res

    return {"structs": structs, "results": run.results, "acrs": acrs, "clip_rect": rect,
            "scenarios": scenarios, "clip_lat": clip.lat, "sources": list(run.sources),
            "forced_1d": run.forced_1d, "subject_dam": run.subject_dam,
            "crossings_evaluated": run.crossings_evaluated,
            "vintage": _download_vintage(),       # v2.8: what the downloads were
            "inputs": {"used": inputs.get("used") or {},
                       "roads_on": inputs["roads"] is not None,
                       "rail_on": inputs["rail"] is not None,
                       # 2.19.0: an empty list is "nothing was given or downloaded"
                       "structs_on": any(x is not None and (len(x) > 0 if isinstance(x, (list, tuple)) else True)
                                         for x in (inputs["outlines"], inputs["points"])),
                       "places": inputs.get("places") or [],
                       "flowlines": get_layer_path(inputs["flowlines"])
                       if inputs.get("flowlines") is not None else None,
                       "roads_path": inputs.get("roads_path"),
                       "roads_source": inputs.get("roads_source"),
                       "access_wanted": inputs["roads"] is not None and bool(cfg["access"])}}


def _write_run_settings(run, cfg, d_qa, written, state):
    """v2.9: QA/Run_Settings.json - every setting, the layers and files the run read (with
    SHA-256) and the scenarios as the run started: what 'Reproduce a package' needs. Never
    fails the run."""
    try:
        from .constants import CURVE_TABLE_SOURCE, CURVE_TABLE_SHA256
        line = ("Flood danger curves (houses on foundations, mobile homes): {} - table "
                "SHA-256 {}".format(CURVE_TABLE_SOURCE, CURVE_TABLE_SHA256[:16]))
        if line not in run.sources:
            run.sources.append(line)
            run.log(">>> " + line)
    except Exception:                                            # noqa
        pass
    try:
        from . import reproduce as REP
        from . import settings as SET
        prof = cfg.get("settings_profile")
        if prof:
            line = SET.profile_log_line(prof)
            run.sources.append(line)
            run.log(">>> " + line)
        path = REP.write_run_settings(d_qa, cfg, (state.get("inputs") or {}).get("used"),
                                      profile=prof, scenarios=cfg.get("scenarios"),
                                      hash_files=not cfg.get("ensemble_member"),
                                      evaluation=_evaluation_record(run))
        if path:
            written.append(os.path.relpath(path, run.out))
    except CancelledByUser:
        raise
    except Exception as e:
        run.log(">>> NOTE: QA/Run_Settings.json not written ({})".format(e))


def _evaluation_record(run):
    """v2.12: the "evaluation" block of QA/Run_Settings.json - the evaluation
    key, whether the evaluation was re-used, and the content digests of the
    input rasters it rests on."""
    from . import fingerprint as FP
    return {"key": getattr(run, "eval_key", None), "reused_from": run.reused or None,
            "rule": "re-used while the rasters' and files' content digests (SHA-256, "
                    "damsuite.chain.file_digest; sampled above 2 GB), the chosen layers and "
                    "the sampling settings are unchanged - file dates are not compared",
            "input_digests": dict(getattr(run, "input_digests", None) or {}),
            "sampled_note": FP.SAMPLED_NOTE}


def _model_chain_step(run, cfg, written):
    """v2.10: QA/Model_Chain_Check.csv / .txt - the rasters this run read against the ND Dam
    Breach Runner's <dam>/_breach_model/run_manifest.json (SHA-256; modelchain.py) - and
    QA/run_manifest.json, the manifest it was checked against (for the submittal package).
    No manifest (HEC-RAS, DSS-WISE ...): the fingerprints are recorded, "chain not checked".
    Never fails the run."""
    try:
        from . import modelchain as MC
        line, paths = MC.pipeline_step(run.out, cfg.get("dam_folder") or None, log=run.log)
        if line:
            run.sources.append(line)
        written.extend(os.path.relpath(p, run.out) for p in paths)
    except CancelledByUser:
        raise
    except Exception as e:
        run.log(">>> NOTE: model chain check not done ({})".format(e))


DAM_RECORD_TOOL = "ndhazard"
_NID_RE = re.compile(r"^[A-Za-z]{2}\d{5}$")


def _read_dam_record(run, cfg):
    """v2.10: <dam folder>/dam.json (the record the three dam-suite plugins share): the dam
    name and the NID ID of the dam being classified are taken from it when the run was not
    given them. Never fails the run; a broken dam.json is reported and left alone."""
    folder = cfg.get("dam_folder")
    if not folder or not os.path.isdir(folder):
        return None
    try:
        from .damsuite import dam_record as DR
        rec = DR.load(folder)
    except ValueError as e:
        run.log(">>> WARNING: dam.json not read ({}) - it is left as it is".format(e))
        return None
    except Exception as e:                                   # noqa
        run.log(">>> NOTE: dam.json not read ({})".format(e))
        return None
    if rec is None:
        return None
    used = []
    if not (cfg.get("subject_dam_nid") or "").strip() and rec.get("nid_id"):
        cfg["subject_dam_nid"] = str(rec.get("nid_id"))
        used.append("NID ID {}".format(cfg["subject_dam_nid"]))
    if not cfg.get("dam_name") and rec.get("name"):
        cfg["dam_name"] = str(rec.get("name"))
        used.append("name '{}'".format(cfg["dam_name"]))
    if used:
        run.log(">>> From dam.json: " + ", ".join(used))
    # v2.21: a height or storage the screening ESTIMATED (DWR unknown structures, dugouts and dams
    # with no inventory value) is named in the log and the summary's data sources
    est = []
    for name, label, unit in (("height_ft", "dam height", "ft"), ("storage_acft", "storage", "ac-ft")):
        note = str(((rec.sources or {}).get(name) or {}).get("note") or "")
        if "estimat" in note.lower() and rec.get(name) is not None:
            est.append("{} {:,.1f} {}".format(label, float(rec.get(name)), unit))
    if est:
        msg = ("Breach inputs ESTIMATED by the screening from the DEM (no DWR value): {}. The flood "
               "rasters rest on these estimates; confirm them before relying on the class.".format(
                   ", ".join(est)))
        run.log(">>> WARNING: " + msg)
        run.sources.append(msg)
    if rec.conflicts and any(not c.get("applied") for c in rec.conflicts):
        run.log(">>> NOTE: dam.json lists {} value(s) other tools could not agree on - see "
                "the dam's dam.json ('conflicts')".format(
                    sum(1 for c in rec.conflicts if not c.get("applied"))))
    return rec


def _flood_cut_off_step(run, cfg):
    """v2.28: when the ND Dam Breach Runner's run_report.json of the dam folder says the flood
    model was cut off (duration cap, domain-run limit, largest domain side), the run log and the
    summary's data sources say so and the summary's classification line is marked incomplete
    (modelchain.read_flood_cut_off, classification.flood_cut_off_note). The class is not
    changed. No Runner report: nothing changes. Never fails the run."""
    run.flood_cut_off = None
    try:
        from . import modelchain as MC
        info = MC.read_flood_cut_off(cfg.get("dam_folder") or None)
    except Exception as e:                                   # noqa
        run.log(">>> NOTE: the Runner's run report was not read ({})".format(e))
        return None
    if not info:
        return None
    run.flood_cut_off = info
    note = flood_cut_off_note(info)
    run.log(">>> WARNING: {} ({})".format(note, info.get("path")))
    if note not in run.sources:
        run.sources.append(note)
    return info


def _write_dam_record(run, cfg, res):
    """v2.10: record this classification in <dam folder>/dam.json: the NID ID / name / dam
    point this run used (never replacing another tool's value - a disagreement is listed in
    the file's conflicts and in the log) and the status "classified" with the class. Not
    for reproductions or ensemble runs (cfg write_dam_record / ensemble_member). Never
    fails the run."""
    folder = cfg.get("dam_folder")
    if not cfg.get("write_dam_record", True) or not folder or not os.path.isdir(folder):
        return
    try:
        from .damsuite import dam_record as DR
        rec = DR.load(folder, create=True)
        vals = {}
        nid = (cfg.get("subject_dam_nid") or "").strip()
        if _NID_RE.match(nid):
            # only a value that IS an NID ID (another tool's dam ID such as "DWR-5289" may
            # have been handed over as the subject NID - it is not recorded as the NID)
            vals["nid_id"] = nid.upper()
        elif nid:
            run.log(">>> NOTE: '{}' does not look like an NID ID (two letters, five digits) - "
                    "not recorded as the NID in dam.json".format(nid))
        if cfg.get("dam_name"):
            vals["name"] = cfg["dam_name"]
        # the dam POINT is not written: this plugin knows the NID point of the dam
        # (dam_location.json), which is not the crest / breach point the Runner and
        # Screening record there
        from .constants import PLUGIN_VERSION
        refused = rec.set_many(vals, DAM_RECORD_TOOL, PLUGIN_VERSION)
        for name in refused:
            src = rec.sources.get(name) or {}
            run.log(">>> NOTE: dam.json keeps {} = {} (set by {}); this run used {}".format(
                name, rec.get(name), src.get("tool", "?"), vals.get(name)))
        chain = ""
        txt = os.path.join(run.out, SUBDIR_QA, "Model_Chain_Check.txt")
        if os.path.isfile(txt):
            try:
                with open(txt, encoding="utf-8") as fh:
                    chain = fh.readline().strip()
            except OSError:
                pass
        nd = res.get("nd") or {}
        tm = res.get("tm11") or {}
        rec.add_status("classified", DAM_RECORD_TOOL, PLUGIN_VERSION, result={
            "nd_class": nd.get("cls"), "tm11_class": tm.get("cls"),
            "lives_in_jeopardy": tm.get("lij"), "governing": res.get("governing"),
            "output_folder": os.path.relpath(run.out, folder)
            if os.path.normcase(os.path.abspath(run.out)).startswith(
                os.path.normcase(os.path.abspath(folder))) else run.out,
            "chain": chain, "quick_rerun": bool(cfg.get("quick_rerun"))})
        rec.save()
        run.sources.append("dam.json: status 'classified' recorded ({} / {})".format(
            nd.get("cls"), tm.get("cls")))
    except ValueError as e:
        run.log(">>> WARNING: dam.json not updated ({}) - it is left as it is".format(e))
    except CancelledByUser:
        raise
    except Exception as e:                                   # noqa
        run.log(">>> NOTE: dam.json not updated ({})".format(e))


def _input_overviews(run, scenarios):
    """v2.9 (opt-in): .ovr pyramid sidecars for large input rasters, so QGIS draws them
    faster. The rasters themselves are never changed. Never fails the run."""
    try:
        from . import overviews as OV
        paths = []
        for s in scenarios:
            for p in (s.depth, s.velocity, s.arrival, s.dv):
                if p and p not in paths:
                    paths.append(p)
        run.tick(1, "Raster overviews...")
        for line in OV.summary_lines(OV.ensure_input_overviews(paths)):
            run.log(">>> " + line)
    except CancelledByUser:
        raise
    except Exception as e:
        run.log(">>> WARNING: raster overviews not built ({})".format(e))


def _facility_review_step(run, cfg, acrs):
    """v2.9: apply Facility_Review_Decisions.csv to every breach scenario's roads, rail,
    crossings and lifelines. Never fails the run (the automatic lists are then kept)."""
    try:
        store = FR.FacilityReviewStore(cfg.get("facility_review_file")
                                       or FR.review_path(run.out))
        out = FR.apply_to_run(run.results, store, acrs, run.ctx,
                              occupancy=float(cfg["occupancy"]),
                              speed_mph=float(cfg["speed_mph"]))
    except CancelledByUser:
        raise
    except Exception as e:
        run.log(">>> WARNING: roads / facilities review decisions not applied ({})".format(e))
        return None
    for w in out.get("warnings") or []:
        run.log(">>> WARNING: " + w)
    for line in out.get("lines") or []:
        run.sources.append(line)
    if out.get("applied"):
        run.log(">>> Roads / facilities review: {} decision(s) applied".format(out["applied"]))
    return out


def _parcel_hints_step(run, cfg, structs, acrs):
    """v2.9: a parcel's land use / improvement value as a hint in the Review tab (never a
    class change). Old hints are always cleared."""
    PH.clear_parcel_hints(structs)
    if not cfg.get("parcel_hints"):
        return
    lyr = cfg.get("parcel_layer")
    if lyr is None:
        run.log(">>> WARNING: parcel hints ticked but no parcel layer - skipped")
        return
    try:
        counts = PH.mark_parcel_hints(structs, lyr, acrs, run.ctx, **PH.options_from_cfg(cfg))
    except CancelledByUser:
        raise
    except Exception as e:
        run.log(">>> WARNING: parcel hints not made ({})".format(e))
        return
    for line in PH.summary_lines(counts, lyr.name()):
        run.sources.append(line)
        run.log(">>> " + line)


def _layer_source(lyr):
    """[full source, provider key] of a layer (layer name / subset / GeoPackage table kept),
    or None - for a step of stage B that has to open the layer again."""
    try:
        return [lyr.source(), lyr.providerType()] if lyr is not None else None
    except Exception:
        return None


def _download_vintage():
    from .pipeline_extras import download_vintage_snapshot
    return download_vintage_snapshot()


def _classify_and_write(run, cfg, state):
    """Stage B: engineer review, habitability rules, consequences,
    incremental analysis, ND / TM 11 classification and every output."""
    out = run.out
    dam_name = run.dam_name
    # the scenarios as stage A left them (rasters reprojected to one CRS)
    scenarios = state["scenarios"]
    breaches = [s for s in scenarios if s.role == ROLE_BREACH]
    layers = {s.name: s.load_layers() for s in scenarios}
    for s in scenarios:
        bad = [k for k, lyr in layers[s.name].items() if lyr is not None and not lyr.isValid()]
        if bad:
            raise RuntimeError("{}: raster(s) could not be loaded: {}".format(s.name, ", ".join(bad)))
    structs = state["structs"]
    run.structs = structs
    acrs = state["acrs"]
    run.results = state["results"]
    inputs = state["inputs"]
    # what an earlier stage B left in the results is redone here
    for r in run.results.values():
        for k in ("layers", "snap", "access", "incremental", "nd", "tm11", "conseq", "views",
                  "nd_total", "tm11_total", "fac_charged", "fac_base_eval", "margin",
                  "future_geoms", "mapbook", "zone_rasters", "eap_bands", "eap_contours"):
            r.pop(k, None)
    # v2.9: the engineer's decisions on roads, rail, bridges / culverts and lifelines
    # (Review: roads & facilities tab). Run every time - it also puts the automatic lists
    # back, so a re-used evaluation starts from what the rasters gave
    run.facility_review = _facility_review_step(run, cfg, acrs)
    dlyr = layers[breaches[0].name]["depth"]
    clip = ExtentClip(dlyr, cfg["extent_buffer_ft"], run.ctx, rect=state["clip_rect"])
    # no velocity raster: the depth-only criterion, whatever the setting says
    run.forced_1d = layers[breaches[0].name].get("vel") is None and not cfg.get("one_d")
    forced_line = ("No velocity raster: depth-only (1D) criterion used (ACER zones need "
                   "Dmax / Vmax)")
    run.sources = [x for x in run.sources if x != forced_line]
    if run.forced_1d:
        run.sources.append(forced_line)
    one_d = bool(cfg.get("one_d")) or run.forced_1d
    cfg["one_d"] = one_d                    # what the classification used (run log)
    judge = cfg["judgment_as_probable"]

    # ---- engineer review and the habitability rules ---------------------------
    run.tick(82, "Applying the review decisions and the rules...")
    store = ReviewStore(cfg.get("review_file") or review_path(out))
    if store.error is not None:
        run.log(">>> WARNING: engineer review decisions in {} could not be read ({}) - "
                "none applied".format(store.path, store.error))
    n_rev = store.attach(structs, acrs, run.ctx)
    if store.unmatched:
        run.log(">>> WARNING: {} saved review decision(s) no longer match a structure - "
                "see the QGIS log".format(store.unmatched))
    if n_rev:
        run.sources.append("Engineer review decisions: {} applied from {}".format(
            n_rev, os.path.basename(store.path)))
    reinterpret(structs, float(cfg["min_area_ft2"]), int(cfg["persons_per_dwelling"]),
                **ST.mobile_rules_from_cfg(cfg))
    _parcel_hints_step(run, cfg, structs, acrs)

    # ---- v2.7: the engineer's depth-damage curves for non-residential structures ----
    curves = None
    curve_file = (cfg.get("damage_curves_file") or "").strip()
    if curve_file:
        from . import damage_curves as DC
        curves, curve_problems = DC.load(curve_file)
        for prob in curve_problems:
            run.log(">>> WARNING: non-residential damage curves: {}".format(prob))
        if curves is None:
            run.log(">>> WARNING: no usable non-residential damage curves - only the value "
                    "exposed is reported for those structures")
        else:
            run.sources.append("Non-residential depth-damage curves (supplied by the engineer): "
                               "{} - occupancy classes {}".format(
                                   curves.source, ", ".join(curves.prefixes())))
    run.damage_curves = curves

    # ---- per scenario: zones for the type, consequences, increment --------------
    from .access import access_stats
    base_snaps = {}
    for sc in scenarios:
        res = run.results[sc.name]
        restore(structs, res["eval_snap"])
        for rec in structs:
            for k in CONSEQ_KEYS:
                if k not in ("access_unknown", "access_cut", "access_raw_note"):
                    rec[k] = None
            apply_type(rec, one_d, cfg["near_ft"], cfg["borderline_higher"])
        if res.get("access_edges") is not None:
            res["access"] = access_stats(structs, *res["access_edges"])
        else:
            for rec in structs:
                rec["cut_off"] = None
            res["access"] = None
        if sc.role == ROLE_BREACH:
            for rec in structs:
                economic_estimate(rec, curves)
                if cfg["lol"]:
                    life_loss_estimate(rec, cfg["warning_issued_hr"], cfg["understanding"],
                                       cfg["severity_method"], cfg["population"],
                                       cfg["high_severity"])
            res["fac_charged"] = FAC.mark_incremental(res["facility_records"],
                                                      res.get("fac_eval") or {}, None,
                                                      float(cfg["incremental_ft"]))
            if sc.baseline and sc.baseline in base_snaps:
                _incremental(run, sc, res, structs, base_snaps[sc.baseline], layers, acrs)
        res["snap"] = _snap(structs)
        if sc.role != ROLE_BREACH:
            base_snaps[sc.name] = (res["snap"], sc, res["method"])

    # ---- model extent check (with the baseline's depth after the increment) ---
    run.tick(83, "Checking the model extent...")
    for sc in breaches:
        run.results[sc.name]["boundary"] = _boundary_check(run, sc, layers)

    # ---- classification ------------------------------------------------------
    run.tick(83, "Classifying...")
    for sc in breaches:
        res = run.results[sc.name]
        restore(structs, res["snap"])
        s_view, r_view, l_view = (incremental_view(structs, res["roads"], res["rails"])
                                  if res.get("incremental") else
                                  (structs, res["roads"], res["rails"]))
        nd = classify_nd(s_view, r_view, l_view, judgment_as_probable=judge, one_d=one_d,
                         roads_evaluated=inputs["roads_on"], rail_evaluated=inputs["rail_on"],
                         structs_evaluated=inputs.get("structs_on", True))
        tm11 = classify_tm11(s_view, r_view, judgment_as_probable=judge)
        extra_criteria(nd, s_view, res, cfg)
        res.update({"nd": nd, "tm11": tm11, "conseq": totals(s_view),
                    "views": (s_view, r_view, l_view)})
        res.pop("nd_total", None)
        res.pop("tm11_total", None)
        if res.get("incremental"):
            nd_t = classify_nd(structs, res["roads"], res["rails"], judgment_as_probable=judge,
                               one_d=one_d, roads_evaluated=inputs["roads_on"],
                               rail_evaluated=inputs["rail_on"],
                               structs_evaluated=inputs.get("structs_on", True))
            extra_criteria(nd_t, structs, res, cfg, total=True)
            res["nd_total"] = nd_t
            res["tm11_total"] = classify_tm11(structs, res["roads"], judgment_as_probable=judge)
    gov = max(breaches, key=lambda s: (ND_RANK[run.results[s.name]["nd"]["cls"]],
                                       run.results[s.name]["tm11"]["lij"]))
    governing = run.results[gov.name]

    # ---- outputs --------------------------------------------------------------
    run.tick(84, "Writing outputs...")
    single = len(breaches) == 1
    written = []
    d_class, d_eap, d_qa = (_sub(out, SUBDIR_CLASSIFICATION), _sub(out, SUBDIR_EAP),
                            _sub(out, SUBDIR_QA))
    layer_formats = ("gpkg", "csv", "xlsx") if cfg.get("spreadsheets", True) else ("gpkg",)
    for sc in breaches:
        res = run.results[sc.name]
        res["layers"] = {}
        restore(structs, res["snap"])
        suffix = "" if single else "_" + sc.slug
        for lyr, base, field in (
                (structures_to_layer(structs, acrs, include_dry=cfg["keep_dry"],
                                     name="Habitable_Structures" + suffix),
                 "Habitable_Structures" + suffix, "ACER_Zone_Label"),
                (roads_to_layer(res["roads"], acrs) if res["roads"] else None,
                 "Roads_Inundated" + suffix, "ND_Class"),
                (rail_to_layer(res["rails"], acrs) if res["rails"] else None,
                 "Rail_Inundated" + suffix, "ND_Class"),
                (_cutoff_layer(structs, acrs, "Access_Cut_Off" + suffix)
                 if res.get("access") else None, "Access_Cut_Off" + suffix, "Access_Note"),
                (FAC.facilities_layer(res["facility_records"], FAC.KIND_DAM,
                                      res.get("fac_eval") or {}, res.get("fac_base_eval"),
                                      res.get("fac_charged"), acrs, "Dams_In_Series" + suffix),
                 "Dams_In_Series" + suffix, "Flooded"),
                (FAC.facilities_layer(res["facility_records"], FAC.KIND_LIFELINE,
                                      res.get("fac_eval") or {}, res.get("fac_base_eval"),
                                      res.get("fac_charged"), acrs,
                                      "Lifeline_Facilities" + suffix),
                 "Lifeline_Facilities" + suffix, "Flooded"),
                (FAC.facilities_layer(res["facility_records"], FAC.KIND_CROSSING,
                                      res.get("fac_eval") or {}, res.get("fac_base_eval"),
                                      res.get("fac_charged"), acrs,
                                      "Bridges_Culverts_Crossings" + suffix),
                 "Bridges_Culverts_Crossings" + suffix, "Flooded"),
                (BND.to_layer(res.get("boundary"), sc.name, acrs,
                              "Model_Boundary_Check" + suffix),
                 "Model_Boundary_Check" + suffix, "Flag"),
                (EAP.notification_layer(structs, res, acrs, run.ctx,
                                        "EAP_Notification_List" + suffix)
                 if _eap_on(cfg, "list") else None, "EAP_Notification_List" + suffix,
                 "Type")):
            is_eap = base.startswith("EAP_")
            dest = d_eap if is_eap else d_qa if base.startswith("Model_Boundary") else d_class
            if lyr is None or lyr.featureCount() == 0:
                _retire_everywhere(out, base)     # never leave a previous run's result
                continue
            fmts = ("gpkg", "csv", "xlsx") if is_eap else layer_formats
            _retire_everywhere(out, base, keep=dest)          # old flat-folder copies
            if "csv" not in fmts:
                _retire_output(dest, base, ("csv", "xlsx"))
            _ok, _r, paths = export_layer_to_formats(lyr, dest, base, run.ctx, formats=fmts)
            show = lyr
            if paths.get("gpkg"):
                saved = QgsVectorLayer(paths["gpkg"], base, "ogr")
                if saved.isValid():
                    show = saved
                written.append(os.path.relpath(paths["gpkg"], out))
            key = base[:-len(suffix)] if suffix else base
            run.show_layer(show, base, GRP_EAP if is_eap else GRP_RESULTS,
                           style=lambda l, f=field: style_by_field(l, f), maptip=key,
                           path=paths.get("gpkg"))
            res["layers"][key] = show
        if _eap_on(cfg, "list"):
            _eap_maps(run, sc, suffix, dam_name, written, visible=sc is gov)
        else:
            _retire_everywhere(out, "EAP_Arrival_Contours" + suffix, ("gpkg",))
            _retire_everywhere(out, "EAP_Arrival_Bands" + suffix, ("tif", "tif.aux.xml"))
        # where each structure's depth / velocity / arrival / zone was read,
        # v2.6: the area searched around it and the raster cells that were read
        cell = res.get("cell_size")
        for lyr, base, style, visible in (
                (structure_sample_points_layer(structs, acrs,
                                               name="Structure_Sample_Points" + suffix),
                 "Structure_Sample_Points" + suffix,
                 lambda l: style_by_field(l, "Value_Type"), False),
                (evaluation_areas_layer(structs, acrs, name="Structure_Evaluation_Areas" + suffix),
                 "Structure_Evaluation_Areas" + suffix,
                 lambda l: style_outline(l, "#E7298A", 0.6), True),
                (sampled_cells_layer(structs, acrs, cell[0], cell[1],
                                     name="Structure_Sampled_Cells" + suffix)
                 if cell and cfg.get("sampled_cells", True) else None,
                 "Structure_Sampled_Cells" + suffix,
                 lambda l: style_outline(l, "#08519C", 0.25, fill_alpha=40), False)):
            if lyr is None:
                _retire_everywhere(out, base, ("gpkg",))
                continue
            _retire_everywhere(out, base, ("gpkg",), keep=d_qa)
            _ok, _r, paths = export_layer_to_formats(lyr, d_qa, base, run.ctx, formats=("gpkg",))
            show = lyr
            if paths.get("gpkg"):
                saved = QgsVectorLayer(paths["gpkg"], base, "ogr")
                if saved.isValid():
                    show = saved
                written.append(os.path.relpath(paths["gpkg"], out))
            key = base[:-len(suffix)] if suffix else base
            run.show_layer(show, base, GRP_SAMPLES, visibility=visible, style=style, maptip=key,
                           path=paths.get("gpkg"))
            res["layers"][key] = show
    # v2.6: ACER zone rasters of the governing scenario (map product; the
    # Main Consolidation of 1.x - 2.5 wrote them to outputs/Consolidation)
    zone_paths = {}
    if cfg.get("zone_rasters", True) and layers[gov.name].get("vel") is not None:
        run.tick(88, "ACER zone rasters...")
        try:
            from .constants import HAZARD_COEFFICIENTS
            zsig = _product_sig([get_layer_path(layers[gov.name]["depth"]),
                                 get_layer_path(layers[gov.name]["vel"])],
                                extra=repr(sorted(HAZARD_COEFFICIENTS.items())))
            from .hazard import ZONE_FILES
            znormal = {k: os.path.join(d_class, f) for k, f in ZONE_FILES.items()}
            zone_paths = reusable_products(d_class, "zone_rasters", zsig, znormal)
            if zone_paths:
                run.log(">>> ACER zone rasters re-used - made from the same depth / velocity "
                        "rasters by an earlier run")
            else:
                forget_products(d_class, "zone_rasters")   # being rewritten
                zone_paths = generate_hazard_rasters(
                    d_class, layers[gov.name]["depth"], layers[gov.name]["vel"],
                    add_to_project=False, overviews=bool(cfg.get("zone_overviews", True)),
                    progress_cb=lambda p, m: run._report(88 + 0.01 * p, m))
                if zone_paths:
                    remember_products(d_class, "zone_rasters", zsig, zone_paths, znormal)
            for k in zone_paths:
                _retire_everywhere(out, os.path.splitext(os.path.basename(zone_paths[k]))[0],
                                   ("tif", "tif.aux.xml"), keep=d_class)
                written.append(os.path.relpath(zone_paths[k], out))
            run.sources.append("ACER zone rasters ({}): {}".format(gov.name, ", ".join(
                os.path.basename(p) for p in zone_paths.values())))
        except CancelledByUser:
            raise
        except Exception as e:
            run.log(">>> WARNING: ACER zone rasters not written: {}".format(e))
            zone_paths = {}
    governing["zone_rasters"] = zone_paths
    if cfg["add_to_project"]:
        run.later(lambda: [_remove_layers_under(os.path.join(out, "ACER_" + sc.slug))
                           for sc in breaches])      # zone layers of the 2.0 - 2.5 methods
        for k, label in (("found", "houses on foundations"), ("mobile", "mobile homes"),
                         ("vehicle", "passenger vehicles")):
            pth = zone_paths.get(k)
            if pth:
                run.show_layer(None, "ACER zones - {} ({})".format(label, gov.name), GRP_ZONES,
                               visibility=k == "found", style=apply_acer11_style, path=pth,
                               raster=True)
    restore(structs, governing["snap"])
    # ---- v2.8: optional analyses on the governing scenario -----------------------
    from . import pipeline_extras as PX
    more = {}
    more["sensitivity"] = PX.sensitivity_step(run, gov, governing, structs, base_snaps, layers,
                                              inputs, one_d, judge, d_qa, written)
    more["conseq_sensitivity"] = PX.conseq_sensitivity_step(run, gov, governing, structs, d_qa,
                                                            written)
    more["evacuation"] = PX.evacuation_step(run, gov, governing, structs, layers, inputs, d_qa,
                                            written, d_eap=d_eap, export_show=_export_show,
                                            dam_name=dam_name)
    more["popsource"] = PX.popsource_step(run, gov, governing, layers, d_qa, written)
    more["termination"] = PX.termination_step(run, gov, governing, layers, inputs, clip,
                                              dam_name, d_qa, written, _export_show)
    more["vintage"] = PX.vintage_step(run, layers, d_qa, written, state.get("vintage"))
    PX.location_file_step(run, acrs)
    restore(structs, governing["snap"])
    # ---- v2.5: future development and the travel-time table --------------------
    more["future"] = _future_development(run, gov, governing, structs, layers, clip, dam_name,
                                         written, d_class)
    eap_more = []
    if _eap_on(cfg, "travel"):
        run.tick(90, "EAP travel-time table...")
        eap_more += _travel_time(run, gov, governing, structs, layers, inputs, clip, dam_name,
                                 written, d_eap)
    else:
        for base in ("EAP_Travel_Time", "EAP_Channel_Centerline"):
            _retire_everywhere(out, base)
    s_view, r_view, l_view = governing["views"]
    from . import robustness as ROB
    rob = ROB.analyse(s_view, governing["nd"], judgment_as_probable=judge, one_d=one_d,
                      roads=r_view, rails=l_view)
    governing["margin"] = rob
    more["margin"] = rob["lines"]
    more["nid"] = _nid_lines(run, governing)
    # ---- 2.30.0: what would change the class, the lifeline detail, the history, the card ----
    more["whatif"] = _whatif_step(run, governing, s_view, r_view, l_view, judge, one_d, d_qa,
                                  written)
    more["lifeline_detail"] = PX.lifeline_detail_step(run, gov, governing, layers, inputs,
                                                      d_qa, written)
    more["history"] = _history_step(run, gov, governing, dam_name, more.get("vintage"))
    _result_card(run, gov, governing, s_view, r_view, l_view, dam_name)
    qa_rows = inventory_qa(structs)
    _retire_everywhere(out, "Inventory_QA", ("csv",), keep=d_qa)
    qa_path = write_qa_csv(qa_rows, os.path.join(d_qa, "Inventory_QA.csv"), acrs)
    written.append(os.path.relpath(qa_path, out))
    extra = _extra_sections(run, breaches, gov, qa_rows, inputs)
    if getattr(run, "flood_cut_off", None):
        extra["flood_cut_off"] = run.flood_cut_off           # v2.28: marks the class line
    if governing.get("card"):
        extra["card"] = governing["card"]                   # 2.30.0: the top of the summary
    hcom = _hcom_check(run, governing, d_qa, written)
    if hcom:
        extra["hcom"] = hcom
    if eap_more:
        extra["eap"] = (extra.get("eap") or []) + eap_more
    extra.update({k: v for k, v in more.items() if v})
    if cfg.get("compare_last_run", True):
        extra["changes"] = _compare_last_run(run, gov, governing, scenarios, dam_name,
                                             written, d_qa)
    if run.reused:
        extra.setdefault("method", []).append(
            "Evaluation (downloads, inventory, raster sampling) re-used from the run of {} - "
            "rasters, data and their settings were unchanged".format(run.reused))
    governing["dv_charts"] = _dv_spreadsheet(run, cfg, gov, structs, dam_name, written, d_class)
    run.tick(92, "Writing the summary...")
    res = {"governing": gov.name, "nd": governing["nd"], "tm11": governing["tm11"],
           "card": governing.get("card"), "whatif": governing.get("whatif") or [],
           "access": governing.get("access"), "margin": governing.get("margin"),
           "output_folder": out,
           "result_items": _result_items(run, gov, governing),
           "results": run.results, "lines": [], "outputs": written, "structs": structs,
           "crs": acrs, "report": None, "review_file": store.path, "clip": clip,
           "notes": [], "dam_name": dam_name}

    # ---- main-thread part: map book, summary, report, layers, groups -----------
    def post_outputs():
        run.tick(93, "EAP map book..." if _eap_on(cfg, "mapbook") else "Summary...")
        if run.deferred:
            # layers made in the worker thread are not handed to layouts or
            # the project: the layouts use the layers just added to the
            # project (run.added), else the main thread opens its own
            for sc_name, r in run.results.items():
                for key, lyr in list((r.get("layers") or {}).items()):
                    src = get_layer_path(lyr)
                    if not src:
                        continue
                    have = run.added.get(os.path.normcase(os.path.abspath(src)))
                    if have is None:
                        have = QgsVectorLayer(src, lyr.name(), "ogr")
                        if not have.isValid():
                            continue
                    r["layers"][key] = have
            for name in list(layers):
                layers[name] = next(sc for sc in scenarios if sc.name == name).load_layers()
        eap_lines = []
        quick = bool(cfg.get("quick_rerun"))
        if quick:
            pass                          # v2.8: the map book of the earlier run stays as it is
        elif _eap_on(cfg, "mapbook"):
            eap_lines = _map_book(run, gov, governing, layers, clip, dam_name, written, d_eap)
        else:
            _retire_everywhere(out, "EAP_Map_Sheets", ("gpkg",))
            _retire_output(d_eap, "EAP_Map_Book", ("pdf",))
        if eap_lines:
            extra["eap"] = (extra.get("eap") or []) + eap_lines
        web_lines = PX.webexport_step(run, dam_name, d_eap, written)      # v2.9
        if web_lines:
            extra["eap"] = (extra.get("eap") or []) + web_lines
        try:
            from .proposed_note import PHRASE as _PROPOSED, find_record, lines_for
            _plines = lines_for(find_record(out))
            if _plines:
                extra["proposed"] = _plines
                if _PROPOSED not in " ".join(run.sources):
                    run.sources.append(_PROPOSED)
        except Exception:
            pass
        # the tables of the summary follow the classification: with a baseline
        # they only count what the breach causes (incremental view)
        lines = summary_lines(governing["nd"], governing["tm11"], s_view, r_view, l_view,
                              dam_name, run.sources, one_d, extra=extra)
        paths = write_summary(out, lines, governing["nd"], governing["tm11"], run.ctx)
        written.extend(os.path.basename(p) for p in paths.values() if p)
        report = None
        if quick:
            old = os.path.join(out, "Hazard_Classification_Report.pdf")
            report = old if os.path.isfile(old) else None
        elif cfg["report_pdf"]:
            run.tick(95, "Building the PDF report...")
            rep_info = {}
            try:
                from .report import build_report
                report, _note = build_report(
                    out, dam_name, governing, gov, lines, structs, clip, layers[gov.name],
                    max_pages=int(cfg["report_pages"]), add_to_project=cfg["add_to_project"],
                    all_results=run.results,
                    hydraulics_group=plugin_group(dam_name, GRP_HYDRAULICS)
                    if cfg["add_to_project"] else None,
                    imagery=bool(cfg.get("report_imagery", True)), info=rep_info)
                if rep_info.get("imagery_note"):
                    run.log(">>> " + str(rep_info["imagery_note"]))
                if report:
                    written.append(os.path.basename(report))
            except Exception as e:
                run.log(">>> WARNING: PDF report not produced: {}".format(e))
                log_message("Report failed: {}".format(e), MSG_WARNING)
        notes = []
        # outputs that could not be overwritten (open in Excel / a PDF viewer)
        for wanted, used in redirected_outputs():
            notes.append("{} is open in another program - this run's copy is {}".format(
                os.path.basename(wanted), os.path.basename(used)))
        if run.forced_1d:
            notes.append("No velocity raster: the depth-only (1D) criterion was used - give the "
                         "Vmax raster for ACER depth-velocity zones")
        if quick:
            notes.append("Quick re-run: the PDF report and the EAP map book were NOT rebuilt - "
                         "the ones in the folder are from an earlier run; run the classification "
                         "again without 'quick' to refresh them")
        for n in notes:
            run.log(">>> WARNING: " + n)
        _write_run_settings(run, cfg, d_qa, written, state)
        if not cfg.get("ensemble_member"):
            _model_chain_step(run, cfg, written)               # v2.10 (item 13)
            _write_dam_record(run, cfg, res)                   # v2.10 (item 2)
        if cfg["add_to_project"]:
            prune_empty_groups(plugin_group(dam_name))
        written.append(RUN_LOG)               # written when the run returns
        res.update({"lines": lines, "report": report, "notes": notes})
        run.tick(100, "Done")
    # the layers go in first (recorded while the outputs were written), then
    # the map book (it uses them), the summary and the report
    run.later(post_outputs)
    if cfg.get("temporal_animation"):
        def temporal():
            lines_t = PX.temporal_step(run, gov, "" if single else "_" + gov.slug, d_eap)
            for ln in lines_t:
                run.sources.append(ln)
        run.later(temporal)
    return res


def _dv_spreadsheet(run, cfg, gov, structs, dam_name, written, folder):
    """v2.20.0: the flooded habitable structures of the governing scenario (the
    report's structures table) plotted on a copy of the depth-velocity
    spreadsheet's first tab and as two SVG charts for the report. The
    template is never written. Returns {"svg": {kind: path}, "xlsx": path,
    "result": dvsheet.write_workbook's dict, "notes": [...]}; {} when off."""
    if not cfg.get("dv_spreadsheet", True):
        return {}
    from . import dvsheet as DV
    from .report import report_structures
    from .helpers import prepare_output_path, same_path
    from .constants import PLUGIN_NAME, PLUGIN_VERSION
    run.tick(91, "Depth-velocity spreadsheet and charts...")
    out = {"svg": {}, "xlsx": None, "result": None, "notes": []}
    rows = DV.plot_rows(report_structures(structs))
    out["rows"] = rows
    for kind in (DV.HOUSE, DV.MOBILE):
        try:
            p = DV.write_svg(kind, rows, os.path.join(folder, DV.SVG_FILES[kind]))
            out["svg"][kind] = p
            written.append(os.path.relpath(p, run.out))
        except (OSError, ValueError) as e:
            run.log(">>> WARNING: depth-velocity chart ({}) not written: {}".format(kind, e))
        win = DV.close_up_window(kind, rows)
        close = os.path.join(folder, DV.CLOSE_UP_FILES[kind])
        try:
            if win:
                p = DV.write_svg(kind, rows, close, window=win)
                out.setdefault("close_up", {})[kind] = p
                written.append(os.path.relpath(p, run.out))
            elif os.path.isfile(close):
                os.remove(close)                  # an earlier run's close-up
        except (OSError, ValueError) as e:
            run.log(">>> WARNING: depth-velocity close-up ({}) not written: {}".format(kind, e))
    template = (cfg.get("dv_template") or "").strip() or DV.default_template()
    if not os.path.isfile(template):
        run.log(">>> WARNING: depth-velocity spreadsheet not written: template {} not "
                "found".format(template))
        return out
    wanted = os.path.join(folder, DV.output_name(template, dam_name))
    if same_path(wanted, template):
        run.log(">>> WARNING: depth-velocity spreadsheet not written: the copy would replace "
                "the template {} - keep the template outside the output folder".format(template))
        return out
    houses = sum(1 for r in rows if r["kind"] == DV.HOUSE)
    info = ("Dam: {} | Scenario: {} | Classified {} by {} {} | {} house(s) on foundation, {} "
            "mobile home(s): the flooded habitable structures of the report; Structure / POI = "
            "Struct_ID of Habitable_Structures; V ft/s, D ft, AT h = maxima and first arrival"
            .format(dam_name, gov.name, time.strftime("%Y-%m-%d %H:%M"), PLUGIN_NAME,
                    PLUGIN_VERSION, houses, len(rows) - houses))
    try:
        path = prepare_output_path(wanted)
        res = DV.write_workbook(template, path, rows, info_line=info)
    except Exception as e:                  # never fails the classification
        run.log(">>> WARNING: depth-velocity spreadsheet not written ({}): {}".format(
            os.path.basename(template), e))
        return out
    out.update({"xlsx": path, "result": res})
    written.append(os.path.relpath(path, run.out))
    notes = out["notes"]
    if res["no_velocity"]:
        notes.append("{} structure(s) have no velocity and are not plotted".format(
            len(res["no_velocity"])))
    if res["clipped"]:
        notes.append("{} structure(s) faster than the figure are drawn at its right edge "
                     "(as the classification reads the curves): {}".format(
                         len(res["clipped"]), ", ".join(str(i) for i in res["clipped"][:20])))
    if res["inset_removed"]:
        notes.append("the template's zoom inset of its sample data is left out of the copy")
    run.log(">>> Depth-velocity spreadsheet: {} ({} house(s) on foundation, {} mobile home(s)"
            "{})".format(os.path.relpath(path, run.out), res["houses"], res["mobile"],
                         "; " + "; ".join(notes) if notes else ""))
    run.sources.append("Depth-velocity spreadsheet: {} (copy of {})".format(
        os.path.basename(path), os.path.basename(template)))
    return out


# ------------------------------------------------------------------ inputs --
def _collect_inputs(run, clip):
    """Project layers and downloads for the run. v2.6: every download that the
    settings ask for is fetched side by side (download.parallel_map); the
    results are then read in the same order as before, so the run log and
    the data-source list read the same."""
    cfg, out = run.cfg, run.out
    tick = run.tick
    del STALE_USED[:]                  # v2.4.1: earlier-day copies used this run
    del PACK_USED[:]                   # v2.6.1: data sets read from the offline pack
    from .download import configure_tile_cache_from_cfg
    configure_tile_cache_from_cfg(cfg)     # v2.8: per-tile download cache (empties TILE_USED)
    _log_offline_mode(run)
    ext4326 = clip.bbox_4326()
    xing4326 = clip.bbox_4326(extra_ft=RAIL_CROSSING_SEARCH_MI * 5280.0)
    big4326 = clip.bbox_4326(extra_ft=2.0 * 5280.0)
    if ext4326 is None:
        run.log(">>> WARNING: the depth raster has no usable CRS - nothing can be "
                "downloaded; only project layers are used")
    loc = run.local
    for line in run.local_notes:
        run.log(">>> WARNING: " + line)
    for line in run.local_lines:
        run.sources.append(line)
        run.log(">>> " + line)
    if cfg["point_sources"] == "auto":
        layer_dict = discover_project_layers()
        points = []
        for fam in ("NSI", "NDGISHUB"):          # FEMA point layers: not used (v2.2)
            points.extend((fam, lyr) for lyr in layer_dict.get(fam, []))
    else:
        points = list(cfg["point_sources"] or [])
    for fam, lyr in points:
        run.sources.append("{} points: project layer '{}'".format(fam, lyr.name()))
    if loc.get("nsi") is not None:
        points.append(("NSI", loc["nsi"]))
    if loc.get("address") is not None:
        points.append(("NDGISHUB", loc["address"]))
    have_hub = any(f == "NDGISHUB" for f, _l in points)
    osm = cfg["osm_layer"]
    if osm == "auto":
        osm = get_osm_candidates(out, cfg.get("dam_folder") or "")
    dam_folder = cfg.get("dam_folder") or ""
    road, aadt = cfg["road_layer"], cfg["aadt_layer"]
    rail, cross = cfg["rail_layer"], cfg["crossing_layer"]
    travel = _eap_on(cfg, "travel")

    # ---- what to download ------------------------------------------------------
    jobs = []           # (key, label, fn, args)

    def want(key, label, fn, *args):
        jobs.append((key, label, fn, args))
    if ext4326 is not None:
        if cfg["download_nsi"] and "nsi" not in loc:
            want("nsi", "NSI", fetch_nsi, ext4326, out)
        if not have_hub and cfg["download_address"]:
            want("address", "ND GIS Hub address points", fetch_nd_address_points, ext4326, out)
        if cfg["download_usa_structures"] and "usa" not in loc \
                and not is_fema_structures_layer(osm, out, dam_folder):
            if osm is None:
                # no outline file yet: download it once and keep it as the file
                want("usa_file", "FEMA USA Structures", download_structures_file, out,
                     dam_folder, ext4326, run.ctx)
            else:
                want("usa", "FEMA USA Structures", fetch_usa_structures, ext4326, out)
        if rail is None and cfg["download_rail"]:
            want("rail", "Rail lines", fetch_arcgis_layer, NARN_URL, ext4326, out,
                 "NARN_rail_lines")
        if cross is None and cfg["download_crossings"]:
            want("cross", "Grade crossings", fetch_arcgis_layer, CROSSINGS_URL, xing4326, out,
                 "FRA_grade_crossings")
        if road is None and cfg.get("download_roads"):
            want("road", "ND GIS Hub roads", fetch_arcgis_layer, ND_ROADS_URL, ext4326, out,
                 "ND_roads_NG911")
            if aadt is None:
                for url in HPMS_ND_URLS:
                    year = "".join(ch for ch in url.split("HPMS_FULL_ND_")[-1][:4]
                                   if ch.isdigit())
                    want("hpms_" + year, "HPMS {}".format(year), fetch_arcgis_layer, url,
                         ext4326, out, "HPMS_ND_{}".format(year))
                if NDDOT_AADT_URL:
                    want("nddot", "NDDOT traffic counts", fetch_arcgis_layer,
                         "{}/{}".format(NDDOT_AADT_URL.rstrip("/"),
                                        NDDOT_AADT_LAYERS.get("aadt", 0)),
                         ext4326, out, "NDDOT_traffic_counts")
                want("county", "ND county roads (AADT)", fetch_arcgis_layer,
                     ND_COUNTY_ROADS_URL, ext4326, out, "ND_county_roads")
        if cfg.get("download_dams") and "dams" not in run.local_roles:
            want("nid", "National Inventory of Dams", fetch_dams, ext4326, out)
        if cfg.get("download_lifelines"):
            for key, label, url, _cat in lifeline_downloads():
                want("life_" + key, label, fetch_arcgis_layer, url, ext4326, out,
                     "lifeline_" + key)
            want("life_osm", "OpenStreetMap lifelines", fetch_osm_lifelines, ext4326, out)
        if cfg.get("download_crossings_structures") and "bridges" not in loc:
            want("nbi", "National Bridge Inventory", fetch_nbi, ext4326, out)
            want("xosm", "OpenStreetMap crossings", fetch_osm_crossings, ext4326, out)
        if cfg.get("download_camps") and "camps" not in loc:
            want("camps", "OpenStreetMap campgrounds", fetch_osm_camps, ext4326, out)
        if cfg.get("download_trailer_parks", True) and "parks" not in loc:
            want("parks", "OpenStreetMap mobile-home parks", fetch_osm_trailer_parks, ext4326,
                 out)
        if travel and cfg.get("download_places") and "places" not in loc:
            want("places", "OpenStreetMap places", fetch_osm_places, big4326, out)
        # v2.8: the termination point needs the channel too, with or without the travel table
        if (travel or cfg.get("termination")) and cfg.get("centerline_layer") is None \
                and cfg.get("download_flowlines"):
            want("flow", "USGS 3DHP flowlines", fetch_arcgis_layer, NHD_FLOWLINE_URL, ext4326,
                 out, "3DHP_flowlines")
    labels = {k: lbl for k, lbl, _f, _a in jobs}
    got = {}
    if jobs:
        tick(2, "Downloading {} data set(s)...".format(len(jobs)))
        res = parallel_map([(k, try_download, (lbl, fn) + args, {}) for k, lbl, fn, args in jobs],
                           progress_cb=run.stage(2, 18), label="Downloading")
        for k, (ok, val) in res.items():
            got[k] = val if ok else (None, 0, None)
            if not ok and not isinstance(val, CancelledByUser):
                log_message("{} download failed: {}".format(labels[k], val), MSG_WARNING)

    def result(key):
        return got.get(key) or (None, 0, None)

    # ---- structures ------------------------------------------------------------
    if cfg["download_nsi"] and "nsi" not in loc:
        lyr, n, path = result("nsi")
        if lyr is not None:
            points.append(("NSI", lyr))
            run.sources.append("USACE National Structure Inventory API: {} structures "
                               "({})".format(n, os.path.basename(path)))
        else:
            run.log(">>> WARNING: NSI download failed - project layers only")
    if not have_hub and cfg["download_address"]:
        lyr, n, path = result("address")
        if lyr is not None:
            points.append(("NDGISHUB", lyr))
            run.sources.append("ND GIS Hub SiteStructure Address Points: {} points "
                               "({})".format(n, os.path.basename(path)))
        else:
            run.log(">>> WARNING: ND GIS Hub address point download failed")

    outlines = []
    if osm is not None:
        outlines.append((STRUCTURES_FAMILY, osm))
        run.sources.append("Building outlines (FEMA_Structures): {}".format(
            osm.source().split("|")[0] or osm.name()))
    if "usa" in loc:
        outlines.append(("USA_STRUCTURES", loc["usa"]))
    if cfg["download_usa_structures"] and "usa" not in loc \
            and is_fema_structures_layer(osm, out, dam_folder):
        run.log(">>> FEMA USA Structures download skipped - the FEMA_Structures outline "
                "file already holds them")
    elif "usa_file" in got:
        lyr, n, path = result("usa_file")
        if lyr is not None:
            outlines.insert(0, (STRUCTURES_FAMILY, lyr))
            run.sources.append("FEMA USA Structures outlines: {} downloaded{}".format(
                n, " and saved as {}".format(os.path.basename(path)) if path else ""))
        else:
            run.log(">>> WARNING: FEMA USA Structures download failed or found nothing - "
                    "structures come from points only")
    elif "usa" in got:
        lyr, n, path = result("usa")
        if lyr is not None:
            outlines.append(("USA_STRUCTURES", lyr))
            run.sources.append("FEMA USA Structures outlines: {} ({})".format(
                n, os.path.basename(path)))
    extra = cfg["extra_outline_layer"]
    if extra is not None:
        outlines.append(("OUTLINES", extra))
        run.sources.append("Additional building outlines: project layer '{}'".format(
            extra.name()))

    # ---- roads, rail, crossings --------------------------------------------------
    if rail is None and "rail" in got:
        rail, n, path = result("rail")
        if rail is not None:
            run.sources.append("FRA/BTS North American Rail Network lines: {} segments "
                               "({})".format(n, os.path.basename(path)))
    elif rail is not None:
        run.sources.append("Rail lines: project layer '{}'".format(rail.name()))
    if cross is None and "cross" in got:
        cross, n, path = result("cross")
        if cross is not None:
            run.sources.append("FRA highway-rail grade crossing inventory (NTAD): {} "
                               "crossings ({})".format(n, os.path.basename(path)))
    elif cross is not None:
        run.sources.append("Rail crossings: project layer '{}'".format(cross.name()))
    aadt_layers = [aadt] if aadt is not None and aadt is not road else []
    if road is not None:
        run.sources.append("Roads: project layer '{}'".format(road.name()))
        if aadt is not None and aadt is not road:
            run.sources.append("AADT: project layer '{}'".format(aadt.name()))
    elif "road" in got:
        road, n, path = result("road")
        if road is not None and n:
            run.sources.append("Roads: ND GIS Hub NG9-1-1 road centerlines, {} segments "
                               "({})".format(n, os.path.basename(path)))
            if aadt is None:
                aadt_layers = _traffic_layers(run, got)
        else:
            road = None
            run.log(">>> WARNING: road download failed - ND road criteria not evaluated")
    if road is None:
        run.log(">>> WARNING: no road layer - ND road criteria not evaluated")
    if rail is None:
        run.log(">>> WARNING: no rail layer - ND railroad criteria not evaluated")
    facilities, dams_on, life_on = _collect_facilities(run, clip, ext4326, got)
    camps = loc.get("camps")
    if "camps" in got:
        camps, n, path = result("camps")
        if camps is not None:
            run.sources.append("OpenStreetMap campgrounds / RV parks (overnight recreation, "
                               "Overpass API): {} ({})".format(n, os.path.basename(path)))
        else:
            run.log(">>> WARNING: campground download failed - campgrounds / RV parks not "
                    "screened")
    parks = loc.get("parks")
    if "parks" in got:
        parks, n, path = result("parks")
        if parks is not None:
            run.sources.append("OpenStreetMap mobile-home parks (Overpass API): {} ({})".format(
                n, os.path.basename(path)))
        else:
            run.log(">>> WARNING: mobile-home park download failed - park evidence for the "
                    "manufactured-home typing not used")
    places, flowlines = [], None
    if loc.get("places") is not None:
        places = _read_places(loc["places"], clip.crs, run.ctx)
    if "places" in got:
        lyr, n, path = result("places")
        if lyr is not None:
            places = _read_places(lyr, clip.crs, run.ctx)
            run.sources.append("OpenStreetMap cities / towns / villages (travel-time "
                               "table): {} ({})".format(n, os.path.basename(path)))
    if "flow" in got:
        flowlines, n, path = result("flow")
        if flowlines is not None and n:
            run.sources.append("USGS National Hydrography Dataset flowlines (large "
                               "scale): {} ({})".format(n, os.path.basename(path)))
        else:
            flowlines = None
    for name, day, fname in STALE_USED:
        run.log(">>> WARNING: {}: the service did not answer - the download of {} was used "
                "({})".format(name, day, fname))
        run.sources.append("{}: service unavailable on this run - earlier download of {} "
                           "used ({})".format(name, day, fname))
    used = _used_inputs(cfg, got, loc)
    for line in pack_report_lines():
        run.log(line)
    if PACK_USED:
        run.sources.append(pack_source_line())
    from .download import tile_cache_report_lines, tile_cache_source_lines
    for line in tile_cache_report_lines():          # v2.8: tiles read from the cache
        run.log(line)
    run.sources.extend(tile_cache_source_lines())
    tick(19, "Inputs ready")
    return {"points": points, "outlines": outlines, "roads": road, "aadt": aadt,
            "aadt_layers": aadt_layers,
            "rail": rail, "crossings": cross, "facilities": facilities,
            "dams_evaluated": dams_on, "lifelines_evaluated": life_on,
            "camps": camps, "places": places, "flowlines": flowlines, "parks": parks,
            "used": used,
            "roads_path": get_layer_path(road) if road is not None else None,
            "roads_source": _layer_source(road)}


_USED_ROLE = {"nsi": "nsi", "address": "address", "usa": "usa", "usa_file": "usa",
              "camps": "camps", "road": "roads", "county": "aadt", "nddot": "aadt",
              "rail": "rail", "cross": "crossings", "nbi": "bridges", "xosm": "bridges",
              "nid": "dams", "life_osm": "lifelines", "flow": "centerline"}


def _used_inputs(cfg, got, loc):
    """{role: path | [path or (path, category)]} of the files a run read: the downloaded data
    sets (their cache files) and the data-folder layers - for QA/Run_Settings.json.
    Project layers are recorded from the settings (reproduce.run_settings_dict)."""
    used = {}

    def add(role, path, cat=None):
        if path:
            used.setdefault(role, []).append((path, cat) if cat else path)
    try:
        for key, val in got.items():
            if not val or val[0] is None:
                continue
            path = val[2] if len(val) > 2 else None
            if key in _USED_ROLE:
                add(_USED_ROLE[key], path)
            elif key.startswith("hpms_"):
                add("aadt", path)
            elif key.startswith("life_"):
                cats = {k: c for k, _l, _u, c in lifeline_downloads()}
                add("lifelines", path, cats.get(key[5:]))
        from . import datafolder as DF
        for role, v in (cfg.get("local_data") or {}).items():
            items = [v] if isinstance(v, str) else list(v or [])
            for it in items:
                uri, cat = (it[0], it[1]) if isinstance(it, (list, tuple)) else (it, None)
                add(role, DF._file_of_uri(uri) if isinstance(uri, str) else "", cat)
    except Exception:                                    # noqa: BLE001 - a record only
        pass
    return used


def pack_hifld_layers():
    """2.24.0: [(GeoPackage, category or None)] of the HIFLD layers of the offline pack in use
    (a folder pack, Data source = Offline pack), else []."""
    from .download import offline_config
    try:
        from .damsuite import suitedata as _SD
        from .damsuite import gpkgpack as _GP
        path, _only = offline_config()
        folder = _GP.pack_folder_of(path) if path else ""
        if not folder:
            return []
        out = []
        for name, e in sorted(_SD.pack_entries(folder).items()):
            if e.get("group") == "hifld" or name.startswith("HIFLD_"):
                p = _SD.pack_file(name, folder)
                if p:
                    out.append((p, e.get("category")))
        return out
    except Exception:
        return []


def _log_offline_mode(run):
    """v2.6.1: say in the run log where the data come from."""
    from .download import offline_config, pack_reader
    path, only = offline_config()
    if not path:
        if only:
            run.log(">>> Offline only, and no offline data pack chosen - nothing is "
                    "downloaded; only project layers and earlier downloads are used")
        return
    r = pack_reader(log=run.log)
    if r is None:
        run.log(">>> WARNING: the offline data pack {} could not be read{}".format(
            path, " - offline only: nothing is downloaded" if only else
            " - the services are used"))
        return
    run.log(">>> Offline data pack: {} ({} data sets, built {}){}".format(
        os.path.basename(path), len(r.datasets), r.created,
        " - offline only, no internet requests" if only else ""))


def pack_report_lines():
    """Run-log lines for the data sets read from the offline pack."""
    from .download import PACK_USED
    out = []
    for label, key, fetched, n, partial in PACK_USED:
        out.append(">>> {}{}: {} feature(s) from the offline data pack (fetched {}){}".format(
            "WARNING: " if partial else "", label, n, fetched,
            " - the area reaches beyond the pack, features there are missing"
            if partial else ""))
    return out


def pack_source_line():
    from .download import PACK_USED, offline_config
    days = sorted(set(f for _l, _k, f, _n, _p in PACK_USED if f))
    return "Offline data pack {}: {} data set(s) fetched {}{}".format(
        os.path.basename(offline_config()[0]), len(PACK_USED),
        " to ".join([days[0], days[-1]] if len(days) > 1 else days) or "?",
        " - PARTIAL for the study area" if any(p for *_x, p in PACK_USED) else "")


def _read_places(layer, crs, ctx):
    """[{"x", "y", "name", "place"}] of OSM places in the analysis CRS."""
    from qgis.core import QgsCoordinateTransform
    xf = QgsCoordinateTransform(layer.crs(), crs, ctx)
    out = []
    for f in layer.getFeatures():
        try:
            p = xf.transform(f.geometry().asPoint())
        except Exception:
            continue
        out.append({"x": p.x(), "y": p.y(), "name": str(f["name"] or ""),
                    "place": str(f["place"] or "")})
    return out


def _traffic_layers(run, got):
    """AADT lines for downloaded roads from the downloads already made: the
    newest FHWA HPMS year that answered, else NDDOT traffic counts, plus the
    ND GIS Hub county roads (AADT / AADT_YR)."""
    out = []
    years = sorted((k[5:] for k in got if k.startswith("hpms_")), reverse=True)
    for year in years:
        lyr, n, path = got["hpms_" + year] or (None, 0, None)
        if lyr is not None:
            out.append(lyr)
            run.sources.append("Traffic: FHWA HPMS North Dakota {} - {} sections ({})".format(
                year, n, os.path.basename(path)))
            break
    if not out and "nddot" in got:
        # v2.5: NDDOT traffic counts only when no HPMS year answered
        lyr, n, path = got["nddot"] or (None, 0, None)
        if lyr is not None and n:
            out.append(lyr)
            run.sources.append("Traffic: NDDOT traffic counts - {} ({})".format(
                n, os.path.basename(path)))
    lyr, n, path = got.get("county") or (None, 0, None)
    if lyr is not None:
        out.append(lyr)
        run.sources.append("Traffic: ND GIS Hub county roads AADT - {} segments ({})".format(
            n, os.path.basename(path)))
    if not out:
        run.log(">>> WARNING: no traffic counts downloaded - overtopped roads without AADT "
                "become judgment items")
    return out


def _collect_facilities(run, clip, ext4326, got):
    """Other dams (NID download and / or the engineer's layer) and lifeline
    facilities (agency + OSM downloads and / or the engineer's layer) in the
    study area, in the analysis CRS; `got` holds the downloads already made
    by _collect_inputs. Returns (records, dams_evaluated, lifelines_evaluated)."""
    cfg, out = run.cfg, run.out
    acrs = clip.crs
    dams, lifelines = [], []
    dams_on = life_on = False
    user = cfg.get("dam_layer")
    if user is not None:
        dams += FAC.read_dams(user, "layer '{}'".format(user.name()), acrs, run.ctx, clip)
        dams_on = True
        run.sources.append("Dams: project layer '{}'".format(user.name()))
    if "nid" in got:
        nid = got["nid"] or (None, 0, None, None)
        if nid[0] is not None:
            lyr, n, path, url = nid
            dams += FAC.read_dams(lyr, "National Inventory of Dams", acrs, run.ctx, clip)
            dams_on = True
            run.sources.append("USACE National Inventory of Dams: {} dam(s) ({}; {})".format(
                n, os.path.basename(path), url.split("/rest/")[0]))
        else:
            run.log(">>> WARNING: National Inventory of Dams download failed - dams in series "
                    "(5.3(c)) not screened")
    if dams:
        subj = FAC.mark_subject_dam(dams, cfg.get("subject_dam_nid") or "",
                                    cfg.get("dam_name") or dam_display_name(
                                        cfg.get("dam_folder") or out))
        run.subject_dam = subj
        if subj is not None:
            run.log(">>> Dam being classified found in the dam inventory: {} {} - not "
                    "counted as a dam in series".format(subj["name"], subj.get("nid") or ""))
        elif not cfg.get("subject_dam_nid"):
            run.log(">>> The dam being classified was not found by name in the dam inventory "
                    "- enter its NID ID so it is not listed as a dam in series")
    user = cfg.get("lifeline_layer")
    if user is not None:
        lifelines += FAC.read_lifelines(user, "layer '{}'".format(user.name()), None, acrs,
                                        run.ctx, clip)
        life_on = True
        run.sources.append("Lifeline facilities: project layer '{}'".format(user.name()))
    for one, cat in run.local.get("lifelines") or []:
        lifelines += FAC.read_lifelines(one, "data folder '{}'".format(one.name()), cat, acrs,
                                        run.ctx, clip)
        life_on = True
    # 2.24.0: the HIFLD lifeline layers of the offline pack (Data source = Offline pack; agency
    # layers, read before the downloads and OpenStreetMap, so they win the dedupe)
    if cfg.get("download_lifelines", True):
        for path, cat in pack_hifld_layers():
            lyr = QgsVectorLayer(path, os.path.splitext(os.path.basename(path))[0], "ogr")
            if not lyr.isValid():
                run.log(">>> WARNING: offline pack layer {} could not be opened".format(path))
                continue
            got_n = FAC.read_lifelines(lyr, "offline pack {}".format(lyr.name()), cat, acrs,
                                       run.ctx, clip)
            lifelines += got_n
            life_on = True
            run.sources.append("HIFLD lifelines (offline pack): {} in the area ({})".format(
                len(got_n), os.path.basename(path)))
    if "life_osm" in got:
        failed = []
        for key, label, url, cat in lifeline_downloads():
            lyr, n, path = got.get("life_" + key) or (None, 0, None)
            if lyr is None:
                failed.append(label)
                continue
            life_on = True
            lifelines += FAC.read_lifelines(lyr, label, cat, acrs, run.ctx, clip)
            if n:
                run.sources.append("{}: {} ({})".format(label, n, os.path.basename(path)))
        lyr, n, path = got.get("life_osm") or (None, 0, None)
        if lyr is None:
            failed.append("OpenStreetMap (Overpass)")
        else:
            life_on = True
            lifelines += FAC.read_lifelines(lyr, "OpenStreetMap", None, acrs, run.ctx, clip)
            run.sources.append("OpenStreetMap lifelines (Overpass API): {} ({})".format(
                n, os.path.basename(path)))
        if failed:
            run.log(">>> WARNING: lifeline download(s) failed: {} - those facility types may "
                    "be missing".format(", ".join(failed)))
    crossings = []
    cross_on = False
    if run.local.get("bridges") is not None:
        one = run.local["bridges"]
        crossings += FAC.read_crossings(one, "data folder '{}'".format(one.name()), acrs,
                                        run.ctx, clip)
        cross_on = True
    if "nbi" in got:
        lyr, n, path = got.get("nbi") or (None, 0, None)
        if lyr is not None:
            cross_on = True
            crossings += FAC.read_crossings(lyr, "National Bridge Inventory", acrs, run.ctx,
                                            clip)
            run.sources.append("FHWA National Bridge Inventory (NTAD): {} bridge(s) / "
                               "culvert(s) ({})".format(n, os.path.basename(path)))
        lyr, n, path = got.get("xosm") or (None, 0, None)
        if lyr is not None:
            cross_on = True
            crossings += FAC.read_crossings(lyr, "OpenStreetMap", acrs, run.ctx, clip)
            run.sources.append("OpenStreetMap culverts, fords and railroad bridges "
                               "(Overpass API): {} ({})".format(n, os.path.basename(path)))
        if not cross_on:
            run.log(">>> WARNING: bridge / culvert downloads failed - crossings not screened")
    run.crossings_evaluated = cross_on
    records = FAC.dedupe(dams + lifelines + crossings, acrs, clip.lat)
    if FAC.mark_near_subject(records, acrs, clip.lat):
        run.log(">>> Lifeline facilities within {:g} ft of the dam being classified are "
                "flagged (they may be in the reservoir) - verify".format(DAM_SEARCH_FT))
    FAC.log_counts(records)
    return records, dams_on, life_on


# --------------------------------------------------------------- scenarios --
def _open_samplers(run, sc, lyrs):
    """Depth / velocity / arrival samplers of a scenario, plus "dv" when it
    has a D x V raster (flood severity for the loss-of-life estimate)."""
    samplers = make_samplers(lyrs["depth"], lyrs["vel"], lyrs["arr"], 0)
    if sc.dv:
        samplers["dv"] = RasterSampler(QgsRasterLayer(sc.dv, "DxV"))
    return samplers


def _run_scenario(run, sc, lyrs, structs, acrs, clip, inputs, cross_idx, tick, one_d):
    """Stage A of one scenario: raster sampling of the structures, roads,
    rail, facilities and the access network. Consequences, the increment
    and the type-dependent zone are applied in stage B."""
    cfg = run.cfg
    # roads / rail are searched in THIS scenario's flooded area
    ext = _wet_extent(lyrs["depth"])
    if lyrs["vel"] is None and not one_d:
        run.log(">>> WARNING: {}: no velocity raster - ACER zones use velocity 0 (not "
                "conservative); give the Vmax raster or use the depth-only (1D) "
                "criterion".format(sc.name))
    for rec in structs:
        for k in CONSEQ_KEYS:
            rec[k] = None
    samplers = _open_samplers(run, sc, lyrs)
    res = {"scenario": sc, "method": METHOD_MAXMAX, "roads": [], "access_edges": None,
           "rails": [], "access": None, "facility_records": inputs.get("facilities") or [],
           "dams_evaluated": inputs.get("dams_evaluated", False),
           "lifelines_evaluated": inputs.get("lifelines_evaluated", False),
           "crossings_evaluated": getattr(run, "crossings_evaluated", False)}
    try:
        run.mark("{}: facilities".format(sc.name))
        res["fac_eval"] = FAC.evaluate(res["facility_records"], samplers, acrs, run.ctx,
                                       clip.lat)
        run.mark("{}: structure sampling".format(sc.name))
        evaluate_structures(structs, samplers, acrs, run.ctx, one_d=one_d,
                            progress_cb=lambda p, m: tick(30 + 0.3 * p, "{}: {}".format(
                                sc.name, m)), lat=clip.lat, near_ft=cfg["near_ft"],
                            borderline_higher=cfg["borderline_higher"],
                            keep_cells=bool(cfg.get("sampled_cells", True)),
                            sample_area=cfg.get("sample_area") or "area",
                            keep_dv=bool(cfg.get("sensitivity")),
                            fast_sampling=bool(cfg.get("fast_sampling")))
        res["cell_size"] = (abs(samplers["depth"].gt[1]), abs(samplers["depth"].gt[5])) \
            if samplers["depth"].ok and samplers["depth"].gt else None
        if sc.role == ROLE_BREACH:
            if inputs["roads"] is not None and ext is not None:
                run.mark("{}: roads".format(sc.name))
                res["roads"] = analyze_roads(
                    inputs["roads"], samplers, acrs, run.ctx, ext,
                    aadt_layers=inputs.get("aadt_layers") or [],
                    crossing_index=cross_idx, occupancy=float(cfg["occupancy"]),
                    speed_mph=float(cfg["speed_mph"]),
                    progress_cb=lambda p, m: tick(60 + 0.2 * p, "{}: {}".format(sc.name, m)))
                if cfg["access"]:
                    run.mark("{}: sole-access analysis".format(sc.name))
                    acc = access_analysis(
                        inputs["roads"], structs, samplers["depth"], acrs, run.ctx, clip=clip,
                        access_ft=float(cfg["access_ft"]), lat=clip.lat,
                        progress_cb=lambda p, m: tick(80 + 0.1 * p, "{}: {}".format(
                            sc.name, m)))
                    res["access_edges"] = (acc["edges"], acc["edges_cut"]) if acc else None
            if inputs["rail"] is not None and ext is not None:
                run.mark("{}: railroads".format(sc.name))
                res["rails"] = analyze_rail(
                    inputs["rail"], samplers, acrs, run.ctx, ext, crossing_index=cross_idx,
                    progress_cb=lambda p, m: tick(90 + 0.1 * p, "{}: {}".format(sc.name, m)))
        elif inputs["roads"] is not None and cfg["access"]:
            # baseline: which structures are cut off even without the breach
            acc = access_analysis(
                inputs["roads"], structs, samplers["depth"], acrs, run.ctx, clip=clip,
                access_ft=float(cfg["access_ft"]), lat=clip.lat)
            res["access_edges"] = (acc["edges"], acc["edges_cut"]) if acc else None
        res["eval_snap"] = _snap(structs)      # stage B starts from this
    finally:
        close_samplers(samplers)
    return res


def _incremental(run, sc, res, structs, base, layers, acrs):
    base_snap, base_sc, _base_method = base
    thr = float(run.cfg["incremental_ft"])
    own_vel = layers[sc.name].get("vel") is not None
    base_vel = layers[base_sc.name].get("vel") is not None
    # zones are compared as they are only when both sides have a velocity
    # raster (or neither has one)
    same = own_vel == base_vel
    one_d = bool(run.cfg.get("one_d"))
    if not same and not one_d:
        run.log(">>> {}: '{}' has no velocity raster - breach and baseline zones are "
                "not comparable; the increment is judged by depth (+{:g} ft) and "
                "flooding of dry places".format(
                    sc.name, base_sc.name if not base_vel else sc.name, thr))
    n_s = mark_structures_incremental(structs, base_snap, thr, same_method=same, one_d=one_d)
    bdepth = RasterSampler(layers[base_sc.name]["depth"])
    bvel = RasterSampler(layers[base_sc.name]["vel"]) \
        if layers[base_sc.name].get("vel") is not None else None
    try:
        line_baseline_depth(res["roads"], bdepth, acrs, run.ctx, bvel)
        line_baseline_depth(res["rails"], bdepth, acrs, run.ctx, bvel)
    finally:
        bdepth.close()
        if bvel is not None:
            bvel.close()
    base_res = run.results.get(base_sc.name) or {}
    res["fac_charged"] = FAC.mark_incremental(res.get("facility_records") or [],
                                              res.get("fac_eval") or {},
                                              base_res.get("fac_eval") or {}, thr)
    res["fac_base_eval"] = base_res.get("fac_eval") or {}
    n_r = mark_lines_incremental(res["roads"], thr, own_velocity=own_vel)
    n_l = mark_lines_incremental(res["rails"], thr, own_velocity=False)
    res["incremental"] = {"baseline": base_sc.name, "structures": n_s, "roads": n_r,
                          "rails": n_l, "threshold_ft": thr}
    run.sources.append("Incremental analysis: '{}' against no-breach baseline '{}' "
                        "(breach adds >= {:g} ft, a worse ACER zone, or floods a dry "
                        "place)".format(sc.name, base_sc.name, thr))


def _result_items(run, gov, governing):
    """Rows of the result panel: the two classes, every ND criterion (with the
    geometries behind it, for zooming) and every judgment item."""
    from .structures import OUT_PROBABLE, OUT_JUDGMENT, OUT_ECONOMIC
    from .constants import ND_HIGH as _H, ND_MEDIUM as _M
    nd, tm11 = governing["nd"], governing["tm11"]
    s_view, r_view, l_view = governing["views"]
    hab = [s for s in s_view if s.get("habitable") == "Yes"]
    recs = governing.get("facility_records") or []
    ev = governing.get("fac_eval") or {}
    charged = governing.get("fac_charged")

    def st(pred):
        return [s["geom"] for s in s_view if pred(s)]
    geo = {
        "Probable loss of life": st(lambda s: s.get("habitable") == "Yes"
                                    and s.get("outcome") == OUT_PROBABLE),
        "Habitable structures in the ACER judgment zone": st(
            lambda s: s.get("habitable") == "Yes" and s.get("outcome") == OUT_JUDGMENT),
        "Overtopped roads - Interstate": [r["geom"] for r in r_view if r["nd_class"] == _H],
        "Overtopped roads - US": [r["geom"] for r in r_view if r["nd_class"] == _M],
        "Overtopped roads without AADT": [r["geom"] for r in r_view if r.get("nd_judgment")],
        "Railroad - passenger": [r["geom"] for r in l_view if r["nd_class"] == _H],
        "Railroad - other": [r["geom"] for r in l_view if r["nd_class"] != _H],
        "Habitable structures inundated": [s["geom"] for s in hab if s.get("outcome") in (
            OUT_PROBABLE, OUT_JUDGMENT, OUT_ECONOMIC)],
        "Possible lifeline facilities": [s["geom"] for s in hab if s.get("lifeline") and
                                         s.get("outcome") in (OUT_PROBABLE, OUT_JUDGMENT,
                                                              OUT_ECONOMIC)],
        "Unverified structures": st(lambda s: s.get("habitable") == "Unverified"
                                    and s.get("outcome") in (OUT_PROBABLE, OUT_JUDGMENT)),
        "Sole-access roads": st(lambda s: s.get("cut_off") and s.get("habitable") == "Yes"),
        "Borderline structures": st(lambda s: s.get("near") and s.get("habitable") == "Yes"),
        "Dams in series": [r["geom"] for r in FAC.flooded(recs, ev, None, kind=FAC.KIND_DAM)],
        "Lifeline facilities inundated": [r["geom"] for r in FAC.flooded(
            recs, ev, charged, kind=FAC.KIND_LIFELINE) if r.get("lifeline")],
        "Bridges, culverts": [r["geom"] for r in FAC.flooded(recs, ev, charged,
                                                             kind=FAC.KIND_CROSSING)],
        "Campgrounds / RV parks": st(lambda s: s.get("camp") and s.get("habitable") == "Yes"
                                     and s.get("outcome") in (OUT_PROBABLE, OUT_JUDGMENT,
                                                              OUT_ECONOMIC)),
        "Future development": governing.get("future_geoms") or [],
    }
    items = [{"section": "Result", "item": "ND hazard classification (screening)",
              "found": "", "result": nd["cls"].upper(), "geoms": []},
             {"section": "Result", "item": "ACER TM 11 (lives in jeopardy {:.1f})".format(
                 tm11["lij"]), "found": "", "result": tm11["cls"].upper(), "geoms": []}]
    for c in nd.get("criteria", []):
        if c["section"] == "Result":
            continue
        g = next((v for k, v in geo.items() if c["criterion"].startswith(k)), [])
        items.append({"section": c["section"], "item": c["criterion"], "found": c["found"],
                      "result": c["result"], "geoms": g if c["result"] not in (
                          "not met", "?") else []})
    rob = governing.get("margin") or {}
    for i, line in enumerate(rob.get("lines") or []):
        items.append({"section": "Margin", "item": line, "found": "", "result": "",
                      "geoms": (rob.get("geoms") or []) if i == 0 else []})
    for line in _nid_lines(run, governing) if run is not None else []:
        items.append({"section": "NID", "item": line, "found": "", "result": "",
                      "geoms": [run.subject_dam["geom"]] if run.subject_dam else []})
    b = governing.get("boundary")
    if b and b.get("flagged") and b.get("points"):
        from qgis.core import QgsGeometry as _G, QgsPointXY as _P
        items.append({"section": "Model", "item": "Flood reaches the model edge",
                      "found": "{} cell(s)".format(b.get("wet", 0)), "result": "Judgment",
                      "geoms": [_G.fromPointXY(_P(p[0], p[1])) for p in b["points"][:200]]})
    for j in nd.get("judgment", []):
        items.append({"section": "Judgment", "item": j, "found": "", "result": "",
                      "geoms": []})
    return items


def _boundary_check(run, sc, layers):
    """Model-extent check of one breach scenario (with its baseline's depth
    when it has one)."""
    cfg = run.cfg
    lyrs = layers[sc.name]
    depth = RasterSampler(lyrs["depth"])
    arr = RasterSampler(lyrs["arr"]) if lyrs.get("arr") is not None else None
    base = None
    if sc.baseline and sc.baseline in layers and \
            (run.results.get(sc.name) or {}).get("incremental"):
        base = RasterSampler(layers[sc.baseline]["depth"])
    try:
        res = BND.check(depth, run.clip_crs, run.ctx,
                        boundary_layer=cfg.get("model_boundary_layer"), base_sampler=base,
                        arr_sampler=arr, min_incr_ft=float(cfg["incremental_ft"]))
    except Exception as e:
        run.log(">>> WARNING: {}: model extent check failed: {}".format(sc.name, e))
        return {"error": str(e), "flagged": True, "method": None, "wet": 0, "points": []}
    finally:
        for smp in (depth, arr, base):
            if smp is not None:
                smp.close()
    if res.get("flagged"):
        run.log(">>> WARNING: {}: {}".format(sc.name, BND.describe(res)))
    return res


def _eap_maps(run, sc, suffix, dam_name, written, visible=False):
    """Arrival-time bands (GeoTIFF) and contours (GPKG) of one breach
    scenario for the EAP inundation maps."""
    out, res = run.out, run.results[sc.name]
    d_eap = _sub(out, SUBDIR_EAP)
    _retire_everywhere(out, "EAP_Arrival_Bands" + suffix, ("tif", "tif.aux.xml"), keep=d_eap)
    _retire_everywhere(out, "EAP_Arrival_Contours" + suffix, ("gpkg",), keep=d_eap)
    out = d_eap
    bands = contours = None
    bpath = os.path.join(out, "EAP_Arrival_Bands{}.tif".format(suffix))
    cpath = os.path.join(out, "EAP_Arrival_Contours{}.gpkg".format(suffix))
    # a loaded layer keeps the file open (Windows): release it before writing
    remove_project_layers_for_path(bpath)
    remove_project_layers_for_path(cpath)
    if sc.arrival:
        # v2.6.2: re-used when made from the same depth / arrival rasters
        from .constants import EAP_ARRIVAL_BREAKS_HR
        esig = _product_sig([sc.depth, sc.arrival], extra=list(EAP_ARRIVAL_BREAKS_HR))
        key = "eap_maps" + suffix
        enormal = {"bands": bpath, "contours": cpath}
        prev = reusable_products(out, key, esig, enormal)
        if prev and prev.get("bands"):
            bands, contours = prev["bands"], prev.get("contours")
            run.log(">>> EAP arrival bands / contours re-used ({}) - same rasters as an "
                    "earlier run".format(sc.name))
        else:
            forget_products(out, key)
            bands = EAP.write_arrival_bands(bpath, sc.depth, sc.arrival)
            contours = EAP.write_arrival_contours(cpath, sc.arrival)
            if bands:          # (contours: none where the GDAL build cannot contour)
                remember_products(out, key, esig, {k: v for k, v in (
                    ("bands", bands), ("contours", contours)) if v}, enormal)
    if not bands:
        _retire_output(out, "EAP_Arrival_Bands" + suffix, ("tif", "tif.aux.xml"))
    if not contours:
        _retire_output(out, "EAP_Arrival_Contours" + suffix, ("gpkg",))
    res["eap_bands"], res["eap_contours"] = bands, contours
    for p in (bands, contours):
        if p:
            written.append(os.path.relpath(p, run.out))
    if bands:
        run.show_layer(None, "EAP arrival bands ({})".format(sc.name), GRP_EAP,
                       visibility=visible, style=EAP.style_arrival_bands, path=bands,
                       raster=True)
    if contours:
        run.show_layer(None, "EAP arrival contours ({})".format(sc.name), GRP_EAP,
                       visibility=visible, path=contours)


def _cutoff_layer(structs, crs, name):
    from qgis.core import QgsField, QgsFeature, QgsGeometry
    from .compat import WKB_POINT, FT_INT, FT_STRING, FT_DOUBLE
    from .helpers import create_memory_layer
    lyr = create_memory_layer(name, WKB_POINT, crs, [
        QgsField("Struct_ID", FT_INT), QgsField("FEMA_Category", FT_STRING),
        QgsField("Persons", FT_INT), QgsField("Max_Depth_ft", FT_DOUBLE),
        QgsField("Access_Note", FT_STRING)])
    feats = []
    for rec in structs:
        if not rec.get("cut_off"):
            continue
        try:
            p = rec["geom"].pointOnSurface().asPoint()
        except Exception:
            continue
        f = QgsFeature(lyr.fields())
        f.setGeometry(QgsGeometry.fromPointXY(p))
        f.setAttributes([rec["id"], rec.get("fema_category") or "",
                         int(rec.get("persons") or 0),
                         rec.get("max_depth") if rec.get("max_depth") else 0.0,
                         rec.get("access_note") or ""])
        feats.append(f)
    lyr.dataProvider().addFeatures(feats)
    lyr.updateExtents()
    return lyr


def _hcom_check(run, governing, d_qa, written):
    """v2.7: the plugin's population at risk against a DSS-WISE HCOM table
    (crosscheck.py); summary lines, QA/DSSWISE_HCOM_CrossCheck.csv."""
    from . import crosscheck as XC
    files = XC.split_files(run.cfg.get("hcom_files"))
    if not files:
        return []
    try:
        from . import dsswise as DW
        tables = [DW.hcom_totals(p) if os.path.isfile(p) else
                  {"file": os.path.basename(p), "error": "file not found"} for p in files]
        lines, rows = XC.compare(tables, governing.get("conseq") or {},
                                 run.cfg.get("population", "night"))
        if rows:
            written.append(os.path.relpath(XC.write_csv(d_qa, rows), run.out))
            run.sources.append("DSS-WISE HCOM cross-check: {}".format(
                ", ".join(os.path.basename(p) for p in files)))
            bad = [r for r in rows if r["Ratio_plugin_to_HCOM"] is None
                   and r["Verdict"] != "agree (both zero)" and r["Verdict"] != "not compared"
                   or (r["Ratio_plugin_to_HCOM"] is not None
                       and not XC.RATIO_LOW <= r["Ratio_plugin_to_HCOM"] <= XC.RATIO_HIGH)]
            if bad:
                run.log(">>> NOTE: the population at risk differs from the DSS-WISE HCOM table "
                        "in {} of {} comparison(s) - see the summary / QA/{}".format(
                            len(bad), len(rows), XC.CSV_NAME))
        return lines
    except CancelledByUser:
        raise
    except Exception as e:
        run.log(">>> WARNING: DSS-WISE HCOM cross-check failed: {}".format(e))
        return ["Not compared: {}".format(e)]


def _population_lines(c, cfg):
    """v2.7: the population at risk for both times of day, and the seasonal
    share (campgrounds / RV parks). The classification uses the time of day
    chosen in the settings; campgrounds are counted (overnight recreation,
    ND 5.2.1.1) - this only shows what changes without them."""
    if not (c.get("par_night") or c.get("par_day")):
        return []
    lines = ["Population at risk by time of day (NSI 2 am / 2 pm where it has a population, "
             "else the TM 11 persons): night {:.0f} (estimated fatalities {:.1f}), day {:.0f} "
             "({:.1f}) - the {} figures above are the ones used".format(
                 c["par_night"], c["lol_night"], c["par_day"], c["lol_day"],
                 "day" if cfg.get("population") == "day" else "night")]
    if c.get("camps"):
        lines.append(
            "Seasonal population: {} campground(s) / RV park(s) in the inundated area hold "
            "{:.0f} (night) / {:.0f} (day) of those persons and are occupied only part of the "
            "year; without them the population at risk is {:.0f} (night) / {:.0f} (day), "
            "estimated fatalities {:.1f} / {:.1f}. The classification counts them (ND "
            "5.2.1.1)".format(c["camps"], c["par_camps_night"], c["par_camps_day"],
                              c["par_night"] - c["par_camps_night"],
                              c["par_day"] - c["par_camps_day"],
                              c["lol_night"] - c["lol_camps_night"],
                              c["lol_day"] - c["lol_camps_day"]))
    return lines


def _extra_sections(run, breaches, gov, qa_rows, inputs):
    cfg = run.cfg
    res = run.results[gov.name]
    sec = {}
    sec["method"] = ["ACER zones: {}".format(METHOD_LABEL)]
    c = res["conseq"]
    cons = []
    if c["value_exposed"]:
        cons.append("Value of flooded structures and contents (NSI): ${:,.0f} in {} "
                    "structure(s)".format(c["value_exposed"], c["wet_with_values"]))
    if c["damage_structures"]:
        cons.append("Estimated residential damage (EGM 01-03, depth above first floor): "
                    "${:,.0f} for {} structure(s)".format(c["damage"], c["damage_structures"]))
    if c.get("damage_nonres_structures"):
        cons.append("Estimated non-residential damage (depth-damage curves supplied by the "
                    "engineer: {}): ${:,.0f} for {} structure(s)".format(
                        getattr(run, "damage_curves", None) and run.damage_curves.source or "file",
                        c["damage_nonres"], c["damage_nonres_structures"]))
    if cfg["lol"]:
        cons.append("Loss-of-life screening (Graham 1999 DSO-99-06, {} population, warning "
                    "issued {:g} h after breach, {} understanding, severity: {}): population "
                    "at risk {:.0f}, estimated fatalities {:.1f} (range {:.1f} - {:.1f})".format(
                        cfg["population"], float(cfg["warning_issued_hr"]),
                        cfg["understanding"], "RCEM DV bands" if cfg["severity_method"] ==
                        "rcem" else "Graham DV / depth rule", c["par"], c["lol"],
                        c["lol_low"], c["lol_high"]))
    if cfg["lol"]:
        cons.extend(_population_lines(c, cfg))
    sec["consequences"] = cons
    acc = res.get("access")
    if acc:
        sec["access"] = ["Sole-access screening: {} of {} habitable structures lose road "
                         "access ({} of them dry, {} persons); {} of {} road segments "
                         "overtopped".format(acc["cut"], acc["structures"], acc["cut_dry"],
                                             acc["persons_cut_dry"], acc["edges_cut"],
                                             acc["edges"])]
    elif inputs.get("access_wanted"):
        sec["access"] = ["Sole-access screening not evaluated (no road leaves the model "
                         "area, or no road near the structures)"]
    scen = []
    for sc in breaches:
        r = run.results[sc.name]
        line = "{}{}: ND {} / TM 11 {} (lives in jeopardy {:.1f})".format(
            sc.name, " [GOVERNS]" if sc is gov and len(breaches) > 1 else "",
            r["nd"]["cls"], r["tm11"]["cls"], r["tm11"]["lij"])
        inc = r.get("incremental")
        if inc:
            line += "; incremental vs '{}': {} structure(s), {} road and {} rail stretch(es) " \
                "charged to the breach (without baseline: ND {} / TM 11 {})".format(
                    inc["baseline"], inc["structures"], inc["roads"], inc["rails"],
                    r["nd_total"]["cls"], r["tm11_total"]["cls"])
        scen.append(line)
    sec["scenarios"] = scen
    ext = []
    for sc in breaches:
        b = run.results[sc.name].get("boundary")
        if b is not None:
            ext.append("{}: {}".format(sc.name, BND.describe(b)))
    if ext:
        ext.append("Method: {}; layer Model_Boundary_Check lists the wet edge cells".format(
            (res.get("boundary") or {}).get("method") or "-"))
    sec["model_extent"] = ext
    if _eap_on(cfg, "list"):
        lyr = (res.get("layers") or {}).get("EAP_Notification_List")
        lines = EAP.summary_lines(lyr, res.get("eap_bands"), res.get("eap_contours"))
        if lines:
            if len(breaches) > 1:
                lines.insert(0, "Governing scenario '{}' (other breach scenarios: files with "
                                "their name)".format(gov.name))
        elif not gov.arrival:
            lines = ["No arrival raster - notification list not sorted by time and no "
                     "arrival maps"]
        sec["eap"] = lines
    recs = res.get("facility_records") or []
    ev = res.get("fac_eval") or {}
    if res.get("dams_evaluated"):
        dams = FAC.flooded(recs, ev, None, kind=FAC.KIND_DAM)
        n_all = sum(1 for r in recs if r["kind"] == FAC.KIND_DAM and not r.get("subject"))
        line = "{} other dam(s) in the study area, {} in the inundated area".format(
            n_all, len(dams))
        if dams:
            line += ": " + FAC.describe(dams, ev, n=10, dam=True)
        sec["dams"] = [line, "Dams within {:g} ft of a flooded cell count (layer "
                       "Dams_In_Series); whether the flood overtops or fails them is an "
                       "engineering judgment".format(DAM_SEARCH_FT)]
    else:
        sec["dams"] = ["Not screened - no dam inventory downloaded or given"]
    if res.get("lifelines_evaluated"):
        charged = res.get("fac_charged")
        wet = FAC.flooded(recs, ev, None, kind=FAC.KIND_LIFELINE)
        ch = FAC.flooded(recs, ev, charged, kind=FAC.KIND_LIFELINE)
        n_all = sum(1 for r in recs if r["kind"] == FAC.KIND_LIFELINE)
        line = "{} lifeline / critical facilit(ies) in the study area, {} inundated".format(
            n_all, len(wet))
        if res.get("incremental"):
            line += " ({} charged to the breach)".format(len(ch))
        if wet:
            line += ": " + FAC.describe(wet, ev, n=10)
        sec["lifelines"] = [line, "Sources: ND GIS Hub, EIA, OpenStreetMap and the HIFLD files of the data folder (hsdl.org), "
                            "verify each facility and its use (layer "
                            "Lifeline_Facilities)"]
    else:
        sec["lifelines"] = ["Not screened - no lifeline data downloaded or given"]
    if res.get("crossings_evaluated"):
        wet = sorted(FAC.flooded(recs, ev, None, kind=FAC.KIND_CROSSING),
                     key=FAC.crossing_priority)
        n_all = sum(1 for r in recs if r["kind"] == FAC.KIND_CROSSING)
        cats = {}
        for r in wet:
            cats[r["category"]] = cats.get(r["category"], 0) + 1
        line = "{} bridge(s) / culvert(s) / crossing(s) in the study area, {} in the " \
            "inundated area{}".format(n_all, len(wet), " ({})".format(", ".join(
                "{} {}".format(v, k.lower()) for k, v in sorted(cats.items()))) if cats else "")
        if wet:
            line += ": " + FAC.describe(wet, ev, n=8)
        sec["crossings"] = [line, "Depth at a bridge includes the channel under it - confirm "
                            "deck / approach overtopping and culvert washout (layer "
                            "Bridges_Culverts_Crossings, sorted by ADT)"]
    else:
        sec["crossings"] = ["Not screened - no bridge / culvert data downloaded"]
    n_out = sum(1 for _r, i in qa_rows if i.startswith("outline"))
    n_ob = sum(1 for _r, i in qa_rows if i.startswith("outbuilding"))     # v2.20.1
    n_pt = len(qa_rows) - n_out - n_ob
    sec["qa"] = ["{} building outline(s) without any NSI / address point{} and {} "
                 "point(s) without an outline - see QA/Inventory_QA.csv".format(
                     n_out, ", {} outbuilding(s) not counted as habitable (the 911 address of "
                     "their parcel is on another building)".format(n_ob) if n_ob else "", n_pt)]
    return sec


# ------------------------------------------------------------------ v2.5 --
def _export_show(run, lyr, folder, base, formats, dam_name, grp, written, field=None,
                 visible=True):
    """Write a result layer (the previous run's copies elsewhere removed),
    load the saved GeoPackage and add it to the project. Returns the shown
    layer or None."""
    out = run.out
    _retire_everywhere(out, base, keep=folder)
    if "csv" not in formats:
        _retire_output(folder, base, ("csv", "xlsx"))
    _ok, _r, paths = export_layer_to_formats(lyr, folder, base, run.ctx, formats=formats)
    show = lyr
    if paths.get("gpkg"):
        saved = QgsVectorLayer(paths["gpkg"], base, "ogr")
        if saved.isValid():
            show = saved
        written.append(os.path.relpath(paths["gpkg"], out))
    if field:
        style_by_field(show, field)
    run.show_layer(show, base, grp, visibility=visible,
                   style=(lambda l, f=field: style_by_field(l, f)) if field else None,
                   path=paths.get("gpkg"))
    return show


def _future_development(run, gov, governing, structs, layers, clip, dam_name, written, folder):
    """Vacant parcels in the danger zone (5.3(b)) - judgment item, no class
    change. Returns summary lines."""
    from . import devscreen as DEV
    cfg = run.cfg
    base = "Future_Development_Parcels"
    parcels = cfg.get("parcel_layer")
    if parcels is None:
        _retire_everywhere(run.out, base)
        return []
    run.tick(89, "Screening vacant parcels (future development)...")
    samplers = _open_samplers(run, gov, layers[gov.name])
    try:
        lyr, summ = DEV.screen(parcels, structs, samplers, clip.crs, run.ctx, clip=clip,
                               lat=clip.lat, max_acres=float(cfg.get("parcel_max_acres") or 5.0),
                               judgment_as_probable=cfg["judgment_as_probable"],
                               one_d=cfg["one_d"])
    except CancelledByUser:
        raise
    except Exception as e:
        run.log(">>> WARNING: future-development screening failed: {}".format(e))
        return ["Not screened: {}".format(e)]
    finally:
        close_samplers(samplers)
    run.sources.append("Parcels (future development screening): project layer '{}'".format(
        parcels.name()))
    nd = governing["nd"]
    crit = nd.setdefault("criteria", [])
    at = len(crit) - 1 if crit and crit[-1].get("section") == "Result" else len(crit)
    crit.insert(at, {"section": "5.3(b)", "criterion": "Future development - vacant parcels "
                     "<= {:g} ac where a new house would be in the danger zone".format(
                         float(cfg.get("parcel_max_acres") or 5.0)),
                     "found": "{} ({:.1f} ac)".format(summ.get("hits", 0), summ.get("acres", 0)),
                     "result": "Judgment" if summ.get("hits") else "not met"})
    if lyr is None:
        _retire_everywhere(run.out, base)
    else:
        show = _export_show(run, lyr, folder, base, ("gpkg", "csv", "xlsx") if cfg.get(
            "spreadsheets", True) else ("gpkg",), dam_name, GRP_RESULTS, written,
            "ACER_Zone_Label")
        governing.setdefault("layers", {})[base] = show
        nd["judgment"].append("{} vacant parcel(s) ({:.1f} ac) where a new house would be in "
                              "the danger zone - imminent development there would make the "
                              "dam HIGH (5.3(b)); check zoning, platting and access (layer "
                              "{})".format(summ["hits"], summ["acres"], base))
        governing["future_geoms"] = [f.geometry() for f in lyr.getFeatures()][:200]
    return DEV.summary_lines(summ, float(cfg.get("parcel_max_acres") or 5.0), base)


def _peak_raster(cfg, sc):
    """Time-of-peak raster: the one given, else TimeOfPeak_hr.tif next to the
    depth raster (written by the HEC-RAS plan and DSS-WISE imports)."""
    p = cfg.get("peak_raster")
    if p and os.path.isfile(p):
        return p
    depth = getattr(sc, "orig_depth", None) or sc.depth
    if depth:
        cand = os.path.join(os.path.dirname(depth), "TimeOfPeak_hr.tif")
        if os.path.isfile(cand):
            return cand
    return None


def _travel_time(run, gov, governing, structs, layers, inputs, clip, dam_name, written,
                 folder):
    from . import traveltime as TT
    cfg = run.cfg
    lyrs = layers[gov.name]
    depth = RasterSampler(lyrs["depth"])
    arr = RasterSampler(lyrs["arr"]) if lyrs.get("arr") is not None else None
    dam_pt = None
    subj = run.subject_dam
    if subj is not None:
        try:
            p = subj["geom"].asPoint()
            dam_pt = (p.x(), p.y())
        except Exception:
            dam_pt = None
    peak = _peak_raster(cfg, gov)
    flow = None
    if inputs.get("flowlines"):
        flow = QgsVectorLayer(inputs["flowlines"], "3DHP flowlines", "ogr")
        if not flow.isValid():
            flow = None
    try:
        result = TT.build(depth, arr, peak, clip.crs, run.ctx, clip.lat,
                          centerline_layer=cfg.get("centerline_layer"),
                          flowline_layer=flow, dam_point=dam_pt,
                          places=inputs.get("places"),
                          facilities=governing.get("facility_records"),
                          fac_eval=governing.get("fac_eval"), roads=governing.get("roads"),
                          structs=structs,
                          spacing_mi=float(cfg.get("travel_station_mi") or TRAVEL_STATION_MI),
                          clip=clip)
    except CancelledByUser:
        raise
    except Exception as e:
        run.log(">>> WARNING: travel-time table not made: {}".format(e))
        return ["Travel-time table not made: {}".format(e)]
    finally:
        depth.close()
        if arr is not None:
            arr.close()
    if result is None or not result.get("rows"):
        for base in ("EAP_Travel_Time", "EAP_Channel_Centerline"):
            _retire_everywhere(run.out, base)
        msg = "; ".join((result or {}).get("notes") or []) or "no centerline"
        if cfg.get("centerline_layer") is None and not inputs.get("flowlines"):
            msg = "no centerline layer given and no 3DHP flowlines downloaded"
        run.log(">>> Travel-time table not made: {}".format(msg))
        return ["Travel-time table not made: {}".format(msg)]
    if peak:
        run.sources.append("Time of peak: {}".format(peak))
    pts, line = TT.to_layers(result, clip.crs)
    fmts = ("gpkg", "csv", "xlsx")
    show = _export_show(run, pts, folder, "EAP_Travel_Time", fmts, dam_name, GRP_EAP, written,
                        "Item")
    governing.setdefault("layers", {})["EAP_Travel_Time"] = show
    _export_show(run, line, folder, "EAP_Channel_Centerline", ("gpkg",), dam_name, GRP_EAP,
                 written)
    run.sources.append("Channel centerline (travel-time table): {}".format(result["source"]))
    return TT.summary_lines(result)


def _map_book(run, gov, governing, layers, clip, dam_name, written, folder):
    from . import mapbook as MB
    cfg = run.cfg
    lyrs = layers[gov.name]
    try:
        path, lines = MB.build(
            folder, dam_name, gov.name, clip.crs, clip.lat, run.ctx, lyrs["depth"],
            lyrs.get("arr"), bands_path=governing.get("eap_bands"),
            contours_path=governing.get("eap_contours"),
            notif_layer=(governing.get("layers") or {}).get("EAP_Notification_List"),
            roads_layer=(governing.get("layers") or {}).get("Roads_Inundated"),
            scale=int(cfg.get("mapbook_scale") or MAPBOOK_DEFAULT_SCALE),
            add_to_project=cfg["add_to_project"], group=plugin_group(dam_name, GRP_EAP),
            imagery=bool(cfg.get("report_imagery", True)))
    except CancelledByUser:
        raise
    except Exception as e:
        run.log(">>> WARNING: EAP map book not produced: {}".format(e))
        log_message("Map book failed: {}".format(e), MSG_WARNING)
        return ["Map book not produced: {}".format(e)]
    if path:
        written.append(os.path.relpath(path, run.out))
        governing["mapbook"] = path
    return lines


# --------------------------------------------------------- 2.30.0: the result card --
def _whatif_step(run, governing, s_view, r_view, l_view, judge, one_d, d_qa, written):
    """The items nearest a class boundary (whatif.py) with the change that would flip each
    one, as the summary section and QA/What_Would_Change_It.csv. Never fails the run."""
    try:
        from . import whatif as WI
        rows = WI.candidates(s_view, r_view, l_view, governing["nd"],
                             judgment_as_probable=judge, one_d=one_d)
        governing["whatif"] = rows
        lines = WI.lines(rows, governing["nd"]["cls"])
        if rows:
            _retire_everywhere(run.out, "What_Would_Change_It", ("csv",), keep=d_qa)
            path = WI.write_csv(prepare_output_path(
                os.path.join(d_qa, "What_Would_Change_It.csv")), rows)
            written.append(os.path.relpath(path, run.out))
            lines.append("Table: QA/{}".format(os.path.basename(path)))
        return lines
    except CancelledByUser:
        raise
    except Exception as e:                                       # noqa - a report section only
        run.log(">>> WARNING: 'what would change it' not computed: {}".format(e))
        return []


def _history_step(run, gov, governing, dam_name, vintage=None):
    """Record this run in <dam folder>/Classification_History.json and return the "class over
    time" section. The log is the dam's, so it survives a cleared outputs folder."""
    try:
        from . import history as HIS
        folder = run.cfg.get("dam_folder") or ""
        entries = HIS.read_entries(folder)
        from .constants import PLUGIN_VERSION
        entry = HIS.make_entry(governing["nd"], governing["tm11"], dam_name=dam_name,
                               governing=gov.name, plugin_version=PLUGIN_VERSION,
                               vintage=vintage or (), cfg=run.cfg,
                               outputs=os.path.basename(run.out),
                               note="quick re-run" if run.cfg.get("quick_rerun") else "")
        governing["history_entries"] = entries
        governing["history_entry"] = entry
        lines = HIS.history_lines(entries, entry)
        if not run.cfg.get("ensemble_member") and not run.cfg.get("reproduction"):
            path = HIS.append_entry(folder, entry)
            if path:
                run.sources.append("Classification history: this run added to {}".format(
                    os.path.basename(path)))
        return lines
    except CancelledByUser:
        raise
    except Exception as e:                                       # noqa - a report section only
        run.log(">>> WARNING: classification history not written: {}".format(e))
        return []


def _result_card(run, gov, governing, s_view, r_view, l_view, dam_name):
    """The plain-language card (resultcard.py) for the top of the Review tab, the top of the
    report and the top of the summary. Never fails the run."""
    try:
        from . import resultcard as RC
        from . import history as HIS
        from .damsuite import loss as LOSS
        rates = None
        est = None
        try:
            est = LOSS.both_times([_loss_row(rec) for rec in s_view
                                   if rec.get("habitable") == "Yes"], rates)
        except Exception as e:                                   # noqa - PAR is optional here
            run.log(">>> NOTE: population-at-risk summary not added to the card: {}".format(e))
        history_line = HIS.history_one_line(governing.get("history_entries") or [],
                                            governing.get("history_entry"))
        governing["card"] = RC.build(
            governing["nd"], governing["tm11"], roads=r_view, rails=l_view,
            access=governing.get("access"), margin=governing.get("margin"),
            flood_cut_off=getattr(run, "flood_cut_off", None), loss=est,
            history_line=history_line, dam_name=dam_name, governing_scenario=gov.name)
    except CancelledByUser:
        raise
    except Exception as e:                                       # noqa - the card is a view
        run.log(">>> WARNING: result card not built: {}".format(e))
        governing["card"] = None


def _loss_row(rec):
    """One structure as damsuite.loss reads it (plain numbers, no QGIS)."""
    return {"depth_ft": rec.get("max_depth"), "velocity_fts": rec.get("max_vel"),
            "arrival_hr": rec.get("arrival"), "habitable": rec.get("habitable") == "Yes",
            "pop": rec.get("persons")}


_NID_TO_ND = {"high": "High", "significant": "Medium", "low": "Low"}


def _nid_lines(run, governing):
    """The dam's own NID hazard potential next to this screening result."""
    cfg = run.cfg
    if not cfg.get("download_dams") and cfg.get("dam_layer") is None:
        return []
    subj = run.subject_dam
    nd = governing["nd"]["cls"]
    if subj is None:
        return ["This dam was not found in the dam inventory{} - enter its NID ID to compare "
                "with its NID hazard potential".format(
                    " (NID ID '{}')".format(cfg.get("subject_dam_nid"))
                    if cfg.get("subject_dam_nid") else "")]
    hz = (subj.get("hazard") or "").strip()
    eq = _NID_TO_ND.get(hz.lower().split()[0]) if hz else None
    line = "{} ({}): NID hazard potential '{}'; this screening: ND {}".format(
        subj.get("name") or "this dam", subj.get("nid") or "no NID ID", hz or "not given", nd)
    if eq is None:
        line += " - the NID value cannot be compared"
    elif eq == nd:
        line += " - consistent (NID {} = ND {})".format(hz, eq)
    else:
        line += " - DIFFERENT: NID {} corresponds to ND {}; review the classification and " \
            "update the inventory if this result is confirmed".format(hz, eq)
    return [line]


def _compare_last_run(run, gov, governing, scenarios, dam_name, written, folder):
    from . import history as HIS
    from .structures import address_of
    from .helpers import prepare_output_path
    try:
        s_view, r_view, _l = governing["views"]
        recs = governing.get("facility_records") or []
        ev = governing.get("fac_eval") or {}
        life = [(r.get("name"), r.get("category")) for r in FAC.flooded(
            recs, ev, governing.get("fac_charged"), kind=FAC.KIND_LIFELINE)]
        crs = run.clip_crs
        snap = HIS.make_snapshot(governing["nd"], governing["tm11"], s_view, r_view, life,
                                 crs.authid() or crs.toWkt()[:200], dam_name, run.cfg,
                                 {sc.name: sc.depth for sc in scenarios}, governing=gov.name,
                                 zone_labels=ZONE_LABELS, address_fn=address_of)
        try:                                   # v2.9: open review items for the portfolio table
            from . import portfolio as PF
            if run.structs is not None:
                snap["review"] = PF.review_counts(run.structs)
        except Exception:                      # noqa: BLE001 - a record only
            pass
        before = HIS.read_snapshot(folder)
        from .structures import _ft_units
        lines, rows = HIS.diff(before, snap, _ft_units(HIS.MATCH_FT, crs, run.clip_lat))
        if before is not None:
            path = prepare_output_path(os.path.join(folder, HIS.CHANGES_FILE))
            HIS.write_changes(path, rows)
            written.append(os.path.relpath(path, run.out))
            if rows:
                lines.append("Details: QA/{}".format(os.path.basename(path)))
        HIS.write_snapshot(folder, snap)
        return lines
    except CancelledByUser:
        raise
    except Exception as e:
        run.log(">>> WARNING: comparison with the last run failed: {}".format(e))
        return ["Not compared: {}".format(e)]
