#!/usr/bin/env python3
"""ND Dam Breach Runner from a terminal: no QGIS needed.

    python3 ndbreach_cli.py template > config.json     a starting configuration
    python3 ndbreach_cli.py check                      find and test LISFLOOD-FP and ANUGA
    python3 ndbreach_cli.py setup-anuga                one-time download of a private ANUGA environment
    python3 ndbreach_cli.py hydrograph config.json     breach hydrograph only
    python3 ndbreach_cli.py estimate config.json       time a test flood, estimate the run
    python3 ndbreach_cli.py run config.json            one dam, the same outputs as in QGIS
    python3 ndbreach_cli.py batch dams.csv config.json a table of dams, one at a time
    python3 ndbreach_cli.py selftest                   what works on this computer
    python3 ndbreach_cli.py hecras-reexport PLAN.hdf   re-make HEC-RAS rasters on the terrain

Run it with any Python that has numpy. GeoTIFF and other GDAL-readable DEMs need GDAL's Python
bindings (`osgeo`), which QGIS's own Python has:

    /Applications/QGIS-LTR.app/Contents/MacOS/bin/python3 ndbreach_cli.py run config.json

Without GDAL the DEM can be an ESRI ASCII grid (.asc), read by the plugin's own reader, and the
rasters are written as ESRI ASCII grids (`Depth (Max).asc`, ...) instead of GeoTIFFs.

The configuration is one JSON file (`template` writes all keys). Relative paths in it are
relative to the file. Keys that start with an underscore are ignored, null means "not set".

  dem        path, z_units (auto|feet|us_feet|meters), crs (ASCII DEMs), work_crs, reader
  dam        dam_id, name, point [x, y] and inflow_point in the DEM's CRS, and the fields of
             breach.DamData (dam_height_ft, storage_top_acft, top_of_dam_ft, ...); the short
             names id, height_ft, storage_acft, top_ft, area_ac, pmf_ft, bottom_ft also work
  breach     method, user_csv, overrides (BreachOverrides fields: avg_width_ft, ...)
  scenario   type (sunny|flood|per_dam), inflow_csv, spillway_csv, normal_pool_ft, trigger,
             trigger_value, baseline_csv, baseline, incr_thresholds_ft
  layers     GeoJSON files: receptor_points/roads/rail, structures (culverts, bridges, notches),
             channels, ponds, refine_lines, gauges, and their options; vector_crs
  manning    n, raster, nlcd, nlcd_n
  model      tier, cell_size_m, threads, lisflood_exe, anuga_python and the other run settings
  output     folder, keep_model_files, products, resume
  batch      x_field, y_field, crs, fields (F_* -> column), skip_done

For the flood scenario a dam's folder `<output.folder>/<dam id>/inputs/` may hold pmf_inflow.csv,
spillway_rating.csv, baseline_outflow.csv and breach_hydrograph.csv, exactly as for the QGIS
tools; those files win over the paths in the configuration.

Exit status: 0 done, 1 a run or model failed, 2 bad input or usage, 3 a batch finished but some
dams failed, 130 cancelled (Ctrl-C once stops the model within a second or so).
"""
import argparse
import csv
import dataclasses
import difflib
import importlib
import importlib.util
import json
import math
import os
import re
import signal
import sys
import tempfile
import time
import traceback

PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class CliError(Exception):
    """Bad input: reported as `ndbreach: error: ...`, exit status 2."""


def _fail_import(e):
    sys.stderr.write("ndbreach: error: cannot load the plugin's core modules from {}: {}: {}\n"
                     "(numpy is required)\n".format(PLUGIN_DIR, type(e).__name__, e))
    sys.exit(2)


def _bootstrap():
    """Import the plugin folder as a package whatever the folder is called; the package's
    __init__ only defines classFactory, so QGIS is not needed."""
    name = "nd_breach_runner"
    mod = sys.modules.get(name)
    here = os.path.realpath(PLUGIN_DIR)
    if mod is not None and os.path.realpath(os.path.dirname(
            getattr(mod, "__file__", "") or "")) == here:
        return name
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(PLUGIN_DIR, "__init__.py"), submodule_search_locations=[PLUGIN_DIR])
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return name


try:
    import numpy as np
    PKG = _bootstrap()
    brmod = importlib.import_module(PKG + ".core.breach")
    floodmod = importlib.import_module(PKG + ".core.flood")
    impmod = importlib.import_module(PKG + ".core.impacts")
    structmod = importlib.import_module(PKG + ".core.structures")
    condmod = importlib.import_module(PKG + ".core.conditioning")
    nd_data = importlib.import_module(PKG + ".core.nd_data")
    estmod = importlib.import_module(PKG + ".core.estimate")
    workflow = importlib.import_module(PKG + ".core.workflow")
    lfmod = importlib.import_module(PKG + ".core.lisflood")
    anmod = importlib.import_module(PKG + ".core.anuga_model")
    anuga_setup = importlib.import_module(PKG + ".core.anuga_setup")
    gdal_io = importlib.import_module(PKG + ".core.gdal_io")
    grid_mod = importlib.import_module(PKG + ".core.grid")
    units = importlib.import_module(PKG + ".core.units")
except Exception as _e:              # pragma: no cover
    _fail_import(_e)

Grid = grid_mod.Grid
FT, MILE = units.FT, units.MILE

# what the QGIS batch tool writes (algorithms.BATCH_FIELDS); keep in step
BATCH_FIELDS = ("Dam", "Folder", "Status", "Scenario", "Final_Model", "Peak_Breach_cfs",
                "Storage_At_Failure_acft", "Flooded_ac", "Max_Depth_ft", "Max_Velocity_fps",
                "Latest_Arrival_hr", "Receptors_Flooded", "Runs", "Domain_mi", "Minutes",
                "Warnings", "Message")
# columns of the dams table -> what the batch tool calls them (algorithms.BATCH_FIELD_KEYS)
BATCH_FIELD_KEYS = ("F_ID", "F_NAME", "F_HEIGHT", "F_STORAGE", "F_TOP", "F_AREA", "F_PMF",
                    "F_BOTTOM", "F_CREST", "F_US", "F_DS", "F_LENGTH", "F_POOL")
BATCH_TO_DAM = {"F_ID": "dam_id", "F_NAME": "name", "F_HEIGHT": "dam_height_ft",
                "F_STORAGE": "storage_top_acft", "F_TOP": "top_of_dam_ft",
                "F_AREA": "surface_area_top_ac", "F_PMF": "pmf_max_wse_ft",
                "F_BOTTOM": "breach_bottom_ft", "F_CREST": "crest_width_ft",
                "F_US": "us_slope_h", "F_DS": "ds_slope_h", "F_LENGTH": "crest_length_ft"}
X_NAMES = ("x", "xcoord", "x_coord", "pointx", "point_x", "easting", "east", "longitude",
           "long", "lon", "lng")
Y_NAMES = ("y", "ycoord", "y_coord", "pointy", "point_y", "northing", "north", "latitude",
           "lat")

# structure attribute names (algorithms.STRUCT_FIELD_SYNONYMS); keep in step
STRUCT_FIELD_SYNONYMS = {
    "S_KIND": ("type", "kind", "structure_type", "structtype"),
    "S_WIDTH": ("width", "span", "width_ft", "diameter", "dia", "span_ft"),
    "S_HEIGHT": ("height", "rise", "height_ft", "rise_ft"),
    "S_BARRELS": ("barrels", "n_barrels", "num_barrels", "cells"),
    "S_SHAPE": ("shape", "culvert_shape"),
    "S_INV_UP": ("invert_up", "inv_up", "upstream_invert", "invert_us"),
    "S_INV_DN": ("invert_dn", "inv_dn", "downstream_invert", "invert_ds"),
    "S_NAME": ("name", "id", "structure_id", "label"),
}
STRUCT_KEYS = tuple(STRUCT_FIELD_SYNONYMS)
STRUCT_KINDS = (structmod.KIND_CULVERT, structmod.KIND_BRIDGE, structmod.KIND_NOTCH)

DAM_ALIASES = {"id": "dam_id", "height_ft": "dam_height_ft", "storage_acft": "storage_top_acft",
               "top_ft": "top_of_dam_ft", "area_ac": "surface_area_top_ac",
               "pmf_ft": "pmf_max_wse_ft", "bottom_ft": "breach_bottom_ft"}
TIER_ALIASES = {"both": workflow.TIER_BOTH, "anuga": workflow.TIER_BOTH,
                "lisflood-only": workflow.TIER_LISFLOOD, "lisflood_only": workflow.TIER_LISFLOOD}
SCENARIO_TYPES = (workflow.SCENARIO_SUNNY, workflow.SCENARIO_FLOOD, "per_dam")
READERS = ("auto", "gdal", "asc")

# --------------------------------------------------------------------------------------
# the configuration file: sections, keys, and what each value must be
# --------------------------------------------------------------------------------------
# spec: ("num", min, max) ("int", min, max) ("bool",) ("str",) ("path",) ("nums",)
#       ("enum", choices) ("point",) ("dict",) ("any",)
MODEL_KEYS = {
    "tier": ("tier", ("enum", workflow.TIERS)),
    "cell_size_m": ("cell_size_m", ("num", 1.0, None)),
    "arrival_depth_ft": ("arrival_depth_ft", ("num", 0.01, None)),
    "edge_tolerance_ft": ("edge_tolerance_ft", ("num", 0.01, None)),
    "initial_length_mi": ("initial_length_mi", ("num", 0.25, None)),
    "half_width_mi": ("half_width_mi", ("num", 0.1, None)),
    "expand_step_mi": ("expand_step_mi", ("num", 0.1, None)),
    "max_extent_mi": ("max_extent_mi", ("num", 1.0, None)),
    "max_iterations": ("max_iterations", ("int", 1, None)),
    "sim_time_hr": ("sim_time_hr", ("num", 0.0, None)),
    "threads": ("threads", ("int", 0, None)),
    "lisflood_exe": ("lisflood_exe", ("path",)),
    "anuga_python": ("anuga_python", ("path",)),
    "anuga_fine_area_m2": ("anuga_fine_area_m2", ("num", 1.0, None)),
    "anuga_coarse_area_m2": ("anuga_coarse_area_m2", ("num", 1.0, None)),
    "anuga_near_field_mi": ("anuga_near_field_mi", ("num", 0.0, None)),
    "anuga_buffer_m": ("anuga_buffer_m", ("num", 0.0, None)),
    "arrival_contours_hr": ("arrival_contours_hr", ("nums",)),
}
SECTIONS = {
    "dem": {"path": ("path",), "z_units": ("enum", gdal_io.Z_CHOICES), "crs": ("str",),
            "work_crs": ("str",), "reader": ("enum", READERS),
            "resample": ("enum", gdal_io.RESAMPLE_METHODS)},
    "breach": {"method": ("enum", brmod.METHODS), "user_csv": ("path",), "overrides": ("dict",)},
    "scenario": {"type": ("enum", SCENARIO_TYPES), "inflow_csv": ("path",),
                 "spillway_csv": ("path",), "normal_pool_ft": ("num", None, None),
                 "trigger": ("enum", floodmod.TRIGGERS), "trigger_value": ("num", None, None),
                 "baseline_csv": ("path",), "baseline": ("bool",),
                 "incr_thresholds_ft": ("nums",)},
    "layers": {"receptor_points": ("path",), "receptor_roads": ("path",),
               "receptor_rail": ("path",), "lookahead_mi": ("num", 0.1, None),
               "structures": ("path",), "structure_fields": ("dict",),
               "structure_defaults": ("dict",), "channels": ("path",),
               "channel_width_ft": ("num", 1.0, None), "channel_depth_ft": ("num", 0.0, None),
               "ponds": ("path",), "pond_depth_ft": ("num", 0.1, None),
               "refine_lines": ("path",), "gauges": ("path",), "gauge_name_field": ("str",),
               "vector_crs": ("str",)},
    "manning": {"n": ("num", 0.005, 1.0), "raster": ("path",), "nlcd": ("path",),
                "nlcd_n": ("any",)},
    "model": dict((k, v[1]) for k, v in MODEL_KEYS.items()),
    "output": {"folder": ("path",), "keep_model_files": ("bool",), "products": ("bool",),
               "resume": ("bool",)},
    "batch": {"x_field": ("str",), "y_field": ("str",), "crs": ("str",), "fields": ("dict",),
              "skip_done": ("bool",)},
}
DAM_KEYS = tuple(f.name for f in dataclasses.fields(brmod.DamData))
OVERRIDE_KEYS = tuple(f.name for f in dataclasses.fields(brmod.BreachOverrides))
TOP_KEYS = ("dem", "dam", "breach", "scenario", "layers", "manning", "model", "output", "batch")


def _suggest(word, choices):
    m = difflib.get_close_matches(str(word), list(choices), n=1, cutoff=0.6)
    return " (did you mean '{}'?)".format(m[0]) if m else ""


def _num_ok(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _check_value(where, value, spec):
    """Validated value (None for null)."""
    if value is None:
        return None
    kind = spec[0]
    if kind in ("num", "int"):
        if not _num_ok(value):
            raise CliError("{} must be a number, not {!r}".format(where, value))
        if kind == "int" and int(value) != value:
            raise CliError("{} must be a whole number, not {!r}".format(where, value))
        lo, hi = spec[1], spec[2]
        if lo is not None and value < lo:
            raise CliError("{} must be at least {:g}, not {:g}".format(where, lo, value))
        if hi is not None and value > hi:
            raise CliError("{} must be at most {:g}, not {:g}".format(where, hi, value))
        return int(value) if kind == "int" else float(value)
    if kind == "bool":
        if not isinstance(value, bool):
            raise CliError("{} must be true or false, not {!r}".format(where, value))
        return value
    if kind in ("str", "path"):
        if not isinstance(value, str):
            raise CliError("{} must be text, not {!r}".format(where, value))
        return value
    if kind == "nums":
        if isinstance(value, (int, float)) and _num_ok(value):
            value = [value]
        if not isinstance(value, list) or not all(_num_ok(x) for x in value):
            raise CliError("{} must be a list of numbers, not {!r}".format(where, value))
        return [float(x) for x in value]
    if kind == "enum":
        v = TIER_ALIASES.get(value, value) if where.endswith("model.tier") else value
        if v not in spec[1]:
            raise CliError("{} is '{}', which is not one of: {}{}".format(
                where, value, ", ".join(spec[1]), _suggest(value, spec[1])))
        return v
    if kind == "point":
        if not (isinstance(value, list) and len(value) == 2 and all(_num_ok(x) for x in value)):
            raise CliError("{} must be [x, y], not {!r}".format(where, value))
        return [float(value[0]), float(value[1])]
    if kind == "dict":
        if not isinstance(value, dict):
            raise CliError("{} must be an object {{...}}, not {!r}".format(where, value))
        return value
    return value


class Config(object):
    """A validated configuration. `section(name)` gives a dict of the keys that are set."""

    def __init__(self, data, path=""):
        self.path = path
        self.base = os.path.dirname(os.path.abspath(path)) if path else os.getcwd()
        self.data = {}
        if not isinstance(data, dict):
            raise CliError("{}: the configuration must be a JSON object".format(path or "config"))
        for k in data:
            if k.startswith("_"):
                continue
            if k not in TOP_KEYS:
                raise CliError("{}: unknown section '{}'{}; sections are: {}".format(
                    path or "config", k, _suggest(k, TOP_KEYS), ", ".join(TOP_KEYS)))
        for name in TOP_KEYS:
            raw = data.get(name)
            if raw is None:
                self.data[name] = {}
                continue
            if not isinstance(raw, dict):
                raise CliError("'{}' must be an object {{...}}".format(name))
            self.data[name] = self._section(name, raw)

    def _section(self, name, raw):
        out = {}
        if name == "dam":
            allowed = set(DAM_KEYS) | set(DAM_ALIASES) | {"point", "inflow_point"}
        else:
            allowed = set(SECTIONS[name])
        for k, v in raw.items():
            if k.startswith("_"):
                continue
            where = "{}.{}".format(name, k)
            if k not in allowed:
                raise CliError("unknown key '{}'{}; the keys of '{}' are: {}".format(
                    where, _suggest(k, sorted(allowed)), name, ", ".join(sorted(allowed))))
            if name == "dam":
                key = DAM_ALIASES.get(k, k)
                if key in out and v is not None and out[key] is not None:
                    raise CliError("dam.{} and dam.{} are the same value; give one".format(
                        k, key))
                if k in ("point", "inflow_point"):
                    spec = ("point",)
                elif key in ("dam_id", "name"):
                    spec = ("str",)
                else:
                    spec = ("num", None, None)
                out[key] = _check_value(where, v, spec)
                continue
            out[k] = _check_value(where, v, SECTIONS[name][k])
        if name == "breach" and out.get("overrides"):
            for k, v in out["overrides"].items():
                if k.startswith("_"):
                    continue
                if k not in OVERRIDE_KEYS:
                    raise CliError("unknown key 'breach.overrides.{}'{}; the keys are: {}".format(
                        k, _suggest(k, OVERRIDE_KEYS), ", ".join(OVERRIDE_KEYS)))
                if k == "vtg_erodible":
                    _check_value("breach.overrides.vtg_erodible", v, ("bool",))
                else:
                    _check_value("breach.overrides." + k, v, ("num", None, None))
            out["overrides"] = {k: v for k, v in out["overrides"].items()
                                if not k.startswith("_") and v is not None}
        return {k: v for k, v in out.items() if v is not None}

    # -- access ---------------------------------------------------------------------------
    def section(self, name):
        return self.data.get(name, {})

    def get(self, section, key, default=None):
        return self.data.get(section, {}).get(key, default)

    def path_of(self, value):
        """A path from the configuration, ~ expanded and relative to the file."""
        if value in (None, ""):
            return ""
        p = os.path.expanduser(str(value))
        return p if os.path.isabs(p) else os.path.normpath(os.path.join(self.base, p))

    def get_path(self, section, key):
        return self.path_of(self.get(section, key))


def load_config(path):
    try:
        with open(path, encoding="utf-8-sig") as f:
            text = f.read()
    except OSError as e:
        raise CliError("cannot read the configuration {}: {}".format(path, e.strerror or e))
    try:
        data = json.loads(text)
    except ValueError as e:
        raise CliError("{} is not valid JSON: {}".format(path, e))
    return Config(data, path)


TEMPLATE = {
    "_help": "ND Dam Breach Runner command-line configuration. Keys starting with _ are ignored; "
             "null means not set; relative paths are relative to this file; all coordinates are "
             "in the DEM's CRS. `ndbreach_cli.py --help` describes every section.",
    "dem": {
        "_help": "GeoTIFF/VRT (needs GDAL) or ESRI ASCII .asc. z_units: auto|feet|us_feet|meters "
                 "(set it: auto only trusts what the raster records). crs is for .asc DEMs "
                 "(metric CRS). resample: how GeoTIFF/VRT terrain is resampled onto the model "
                 "grid (average|bilinear|near|cubic|...); null = the NDBREACH_DEM_RESAMPLE "
                 "environment variable, else average.",
        "path": "dem.tif", "z_units": "meters", "crs": None, "work_crs": None,
        "reader": "auto", "resample": None},
    "dam": {
        "_help": "point is the dam crest in the DEM's CRS; the rest are the fields of "
                 "breach.DamData (ft, ac-ft, acres, H in H:1V).",
        "dam_id": "ND00001", "name": "Example Dam", "point": [350000.0, 5230000.0],
        "inflow_point": None, "dam_height_ft": 25.0, "storage_top_acft": 500.0,
        "top_of_dam_ft": None, "surface_area_top_ac": 60.0, "pmf_max_wse_ft": None,
        "breach_bottom_ft": None, "crest_width_ft": None, "us_slope_h": None,
        "ds_slope_h": None, "valley_width_ft": None, "crest_length_ft": None},
    "breach": {
        "_help": "method: " + ", ".join(brmod.METHODS) + ". overrides: any of "
                 + ", ".join(OVERRIDE_KEYS) + ".",
        "method": brmod.METHOD_PIPING, "user_csv": None, "overrides": {}},
    "scenario": {
        "_help": "sunny: no inflow. flood: PMF inflow with and without the breach. per_dam "
                 "(batch): flood where the dam's folder has inputs/pmf_inflow.csv. "
                 "trigger: peak|time|wse.",
        "type": "sunny", "inflow_csv": None, "spillway_csv": None, "normal_pool_ft": None,
        "trigger": "peak", "trigger_value": None, "baseline_csv": None, "baseline": True,
        "incr_thresholds_ft": [1.0, 2.0, 3.0]},
    "layers": {
        "_help": "GeoJSON files (points, lines or polygons) in the DEM's CRS, or with a crs "
                 "member / vector_crs 'EPSG:xxxx' (needs GDAL). structure_fields maps S_KIND, "
                 "S_WIDTH, S_HEIGHT, S_BARRELS, S_SHAPE, S_INV_UP, S_INV_DN, S_NAME to "
                 "property names; structure_defaults: kind, width_ft, height_ft.",
        "receptor_points": None, "receptor_roads": None, "receptor_rail": None,
        "lookahead_mi": 5.0, "structures": None, "structure_fields": {},
        "structure_defaults": {"kind": "culvert", "width_ft": 4.0, "height_ft": 4.0},
        "channels": None, "channel_width_ft": 15.0, "channel_depth_ft": 1.0, "ponds": None,
        "pond_depth_ft": 5.0, "refine_lines": None, "gauges": None, "gauge_name_field": None,
        "vector_crs": None},
    "manning": {
        "_help": "n where there is no raster; raster of n values, or an NLCD class raster "
                 "(nlcd, with nlcd_n overrides like {\"82\": 0.05}).",
        "n": 0.06, "raster": None, "nlcd": None, "nlcd_n": None},
    "model": {
        "_help": "tier: lisflood or lisflood+anuga. Paths left empty are auto-detected "
                 "(NDBREACH_LISFLOOD, NDBREACH_ANUGA_PYTHON, the usual install places).",
        "tier": "lisflood", "cell_size_m": 10.0, "arrival_depth_ft": 1.0,
        "edge_tolerance_ft": 1.0, "initial_length_mi": 5.0, "half_width_mi": 1.0,
        "expand_step_mi": 3.0, "max_extent_mi": 60.0, "max_iterations": 6, "sim_time_hr": None,
        "threads": 0, "lisflood_exe": None, "anuga_python": None,
        "anuga_fine_area_m2": 100.0, "anuga_coarse_area_m2": 1000.0,
        "anuga_near_field_mi": 1.0, "anuga_buffer_m": 500.0,
        "arrival_contours_hr": [0.25, 0.5, 1.0, 2.0, 4.0, 8.0]},
    "output": {
        "_help": "Each dam goes to <folder>/<Dam ID>/, as in the QGIS tools.",
        "folder": "dams", "keep_model_files": True, "products": True, "resume": False},
    "batch": {
        "_help": "For `batch DAMS.csv`: columns are matched by name (NIDID, DAM_HEIGHT, "
                 "MAX_STORAGE, ...); fields maps F_ID, F_NAME, F_HEIGHT, F_STORAGE, F_TOP, "
                 "F_AREA, F_PMF, F_BOTTOM, F_CREST, F_US, F_DS, F_LENGTH, F_POOL to columns. "
                 "Point columns x_field/y_field (auto: x/y, easting/northing, lon/lat) are in "
                 "the DEM's CRS unless crs says otherwise (needs GDAL). Values in the table "
                 "win over the 'dam' section.",
        "x_field": None, "y_field": None, "crs": None, "fields": {}, "skip_done": True},
}


def template_text():
    return json.dumps(TEMPLATE, indent=2) + "\n"


# --------------------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------------------
def safe_name(s):
    s = re.sub(r"[^\w\- ]+", "_", str(s)).strip()
    return s or "dam"


def num(v, default=None):
    """A table value as a float, or default for empty / text / NaN."""
    if v is None:
        return default
    if isinstance(v, str):
        v = v.strip().replace(",", "")
        if v == "" or v.lower() in ("null", "none", "nan", "na", "n/a"):
            return default
    try:
        x = float(v)
    except (TypeError, ValueError):
        return default
    return default if x != x else x


def have_gdal():
    try:
        from osgeo import gdal  # noqa: F401
        return True
    except Exception:
        return False


def say(msg=""):
    sys.stdout.write(str(msg) + "\n")
    sys.stdout.flush()


CANCEL = {"flag": False}


class Progress(object):
    """The workflow's progress / log / cancel callbacks, printed to stdout."""

    def __init__(self, lo=0.0, hi=100.0, quiet=False, prefix=""):
        self.lo, self.hi, self.quiet, self.prefix = lo, hi, quiet, prefix
        self._last = ""

    def progress(self, p, msg=""):
        if msg and msg != self._last and not self.quiet:
            pct = self.lo + (self.hi - self.lo) * max(0.0, min(100.0, p)) / 100.0
            say("{}[{:3.0f}%] {}".format(self.prefix, pct, msg))
            self._last = msg

    def log(self, msg):
        say(self.prefix + str(msg))

    def cancel(self):
        return CANCEL["flag"]


# --------------------------------------------------------------------------------------
# GeoJSON (vector inputs)
# --------------------------------------------------------------------------------------
class GeoLayer(object):
    """Features of one GeoJSON file as plain arrays: (kind, [ (n, 2) arrays ], properties) with
    kind point / line / polygon (outer ring only; a hole in a pond is an island)."""

    def __init__(self, path, features, epsg=None):
        self.path, self.features, self.epsg = path, features, epsg

    def field_names(self):
        names = []
        for _k, _p, props in self.features:
            for k in props:
                if k not in names:
                    names.append(k)
        return names

    def of_kind(self, kind):
        return [(parts, props) for k, parts, props in self.features if k == kind]

    def all_xy(self):
        arrs = [a for _k, parts, _p in self.features for a in parts]
        return np.vstack(arrs) if arrs else np.zeros((0, 2))


def _xy(coords):
    a = np.asarray(coords, dtype=float)
    if a.ndim == 1:
        a = a.reshape(1, -1)
    return a[:, :2]


def _geom_parts(g):
    """[(kind, [arrays])] of one GeoJSON geometry."""
    t = g.get("type")
    c = g.get("coordinates")
    if t == "Point":
        return [("point", [_xy(c)])]
    if t == "MultiPoint":
        return [("point", [_xy(p) for p in c])]
    if t == "LineString":
        return [("line", [_xy(c)])]
    if t == "MultiLineString":
        return [("line", [_xy(p) for p in c])]
    if t == "Polygon":
        return [("polygon", [_xy(c[0])])] if c else []
    if t == "MultiPolygon":
        return [("polygon", [_xy(p[0]) for p in c if p])]
    if t == "GeometryCollection":
        out = []
        for gg in g.get("geometries") or []:
            out += _geom_parts(gg)
        return out
    raise ValueError("unsupported geometry type {!r}".format(t))


def parse_geojson(data, path="<geojson>"):
    if not isinstance(data, dict):
        raise CliError("{}: not a GeoJSON object".format(path))
    t = data.get("type")
    if t == "FeatureCollection":
        feats = data.get("features") or []
    elif t == "Feature":
        feats = [data]
    elif t in ("Point", "MultiPoint", "LineString", "MultiLineString", "Polygon",
               "MultiPolygon", "GeometryCollection"):
        feats = [{"type": "Feature", "geometry": data, "properties": {}}]
    else:
        raise CliError("{}: not GeoJSON (type is {!r})".format(path, t))
    epsg = None
    crs = data.get("crs")
    if isinstance(crs, dict):
        name = str((crs.get("properties") or {}).get("name", ""))
        m = re.search(r"EPSG:+(\d+)", name, re.I)
        if m:
            epsg = "EPSG:" + m.group(1)
        elif "CRS84" in name.upper():
            epsg = "EPSG:4326"
    out = []
    for f in feats:
        g = f.get("geometry")
        if not g:
            continue
        props = f.get("properties") or {}
        try:
            for kind, parts in _geom_parts(g):
                parts = [p for p in parts if len(p)]
                if parts:
                    out.append((kind, parts, props))
        except (ValueError, TypeError, IndexError) as e:
            raise CliError("{}: bad geometry: {}".format(path, e))
    return GeoLayer(path, out, epsg)


def read_geojson(path):
    try:
        with open(path, encoding="utf-8-sig") as f:
            data = json.load(f)
    except OSError as e:
        raise CliError("cannot read {}: {}".format(path, e.strerror or e))
    except ValueError as e:
        raise CliError("{} is not valid GeoJSON: {}".format(path, e))
    return parse_geojson(data, path)


# --------------------------------------------------------------------------------------
# coordinate frames
# --------------------------------------------------------------------------------------
class Frame(object):
    """From the DEM's CRS to the model CRS. Without GDAL both are the same CRS."""

    def __init__(self, dem_srs=None, work_srs=None, osr_module=None, dem_epsg=None):
        self.osr = osr_module
        self.dem_srs, self.work_srs = dem_srs, work_srs
        self.dem_epsg = dem_epsg
        self._cts = {}
        self.identity = dem_srs is None or work_srs is None or bool(dem_srs.IsSame(work_srs))

    def srs(self, text):
        if self.osr is None:
            raise CliError("changing coordinate systems ('{}') needs GDAL's Python bindings, "
                           "which this Python does not have".format(text))
        s = self.osr.SpatialReference()
        try:
            s.SetFromUserInput(text)
        except Exception as e:
            raise CliError("cannot understand the CRS '{}': {}".format(text, e))
        if hasattr(self.osr, "OAMS_TRADITIONAL_GIS_ORDER") and hasattr(
                s, "SetAxisMappingStrategy"):
            s.SetAxisMappingStrategy(self.osr.OAMS_TRADITIONAL_GIS_ORDER)
        return s

    def _ct(self, src, dst):
        # 3.10.0: the entry keeps both SRS objects alive, so no later SRS can be given the same
        # id() and pick up this transformation (a layer's SRS used to be freed after use)
        key = (id(src), id(dst))
        if key not in self._cts:
            self._cts[key] = (self.osr.CoordinateTransformation(src, dst), src, dst)
        return self._cts[key][0]

    def xy(self, x, y):
        """A point in the DEM's CRS -> the model CRS."""
        if self.identity:
            return (float(x), float(y))
        p = self._ct(self.dem_srs, self.work_srs).TransformPoint(float(x), float(y))
        return (float(p[0]), float(p[1]))

    def array(self, a, src=None):
        """(n, 2) coordinates in `src` (an SRS from .srs(); None = the DEM's CRS) -> the model
        CRS."""
        a = np.asarray(a, dtype=float).reshape(-1, 2)
        if src is None:
            if self.identity or not len(a):
                return a
            src = self.dem_srs
        if self.work_srs is None:
            return a
        if src.IsSame(self.work_srs) or not len(a):
            return a
        pts = self._ct(src, self.work_srs).TransformPoints([tuple(p) for p in a])
        return np.asarray([[p[0], p[1]] for p in pts], dtype=float)

    def to_dem(self, x, y, src):
        """A point in `src` -> the DEM's CRS."""
        if self.dem_srs is None or src.IsSame(self.dem_srs):
            return (float(x), float(y))
        p = self._ct(src, self.dem_srs).TransformPoint(float(x), float(y))
        return (float(p[0]), float(p[1]))


def layer_to_work(layer, frame, vector_crs=None, dem_is_projected=None):
    """The layer's coordinates in the model CRS, per feature. Vectors are taken to be in the
    DEM's CRS unless the file declares another (a `crs` member) or `vector_crs` is given."""
    declared = vector_crs or layer.epsg
    src = None
    if declared and frame.dem_epsg and declared.upper() == frame.dem_epsg.upper():
        declared = None
    if declared:
        src = frame.srs(declared)
    elif dem_is_projected:
        xy = layer.all_xy()
        if len(xy) and np.abs(xy[:, 0]).max() <= 180.0 and np.abs(xy[:, 1]).max() <= 90.0:
            raise CliError(
                "{}: the coordinates look like longitude/latitude but the DEM is in projected "
                "coordinates. Reproject the layer to the DEM's CRS, or set layers.vector_crs "
                "to 'EPSG:4326' (needs GDAL)".format(layer.path))
    out = []
    for kind, parts, props in layer.features:
        out.append((kind, [frame.array(p, src) for p in parts], props))
    return out


def in_box(arr, center, radius):
    """Does a coordinate array touch the square of half-side `radius` around `center`?"""
    if not len(arr):
        return False
    return not (arr[:, 0].max() < center[0] - radius or arr[:, 0].min() > center[0] + radius or
                arr[:, 1].max() < center[1] - radius or arr[:, 1].min() > center[1] + radius)


# --------------------------------------------------------------------------------------
# terrain without GDAL: ESRI ASCII grids
# --------------------------------------------------------------------------------------
class AscTerrain(object):
    """terrain_src for workflow.run_dam from an ESRI ASCII grid, in a metric CRS. Bilinear
    sampling onto the requested grid (nearest for Manning's n); the whole file is in memory,
    so this is for small DEMs and tests; use GDAL for anything big."""

    def __init__(self, dem_path, z_choice=gdal_io.Z_AUTO, manning_path=None, default_n=0.06,
                 manning_lut=None):
        try:
            self.grid = Grid.read_asc(dem_path)
        except (OSError, ValueError, KeyError) as e:
            raise CliError("cannot read the ESRI ASCII grid {}: {}: {}".format(
                dem_path, type(e).__name__, e))
        self.dem_path = dem_path
        self.default_n = default_n
        self.manning_lut = manning_lut
        self.manning_grid = None
        if manning_path:
            try:
                self.manning_grid = Grid.read_asc(manning_path)
            except (OSError, ValueError, KeyError) as e:
                raise CliError("cannot read the ESRI ASCII grid {}: {}: {}".format(
                    manning_path, type(e).__name__, e))
        if z_choice == gdal_io.Z_FEET:
            self.z_to_m, self.z_note = FT, "feet (chosen)"
        elif z_choice == gdal_io.Z_US_FEET:
            self.z_to_m, self.z_note = units.US_FT, "US survey feet (chosen)"
        elif z_choice == gdal_io.Z_METERS:
            self.z_to_m, self.z_note = 1.0, "metres (chosen)"
        else:
            self.z_to_m, self.z_note = 1.0, "metres (an ESRI ASCII grid records no units; check)"

    @property
    def bounds(self):
        return self.grid.bounds

    def _sample(self, src, bounds, cell, method):
        xmin, ymin, xmax, ymax = bounds
        nx = int(round((xmax - xmin) / cell))
        ny = int(round((ymax - ymin) / cell))
        xs = xmin + (np.arange(nx) + 0.5) * cell
        ys = ymax - (np.arange(ny) + 0.5) * cell
        X, Y = np.meshgrid(xs, ys)
        if method == "near":
            r = np.floor((src.y0 - Y) / src.dx).astype(int)
            c = np.floor((X - src.x0) / src.dx).astype(int)
            ok = (r >= 0) & (r < src.ny) & (c >= 0) & (c < src.nx)
            a = src.masked()
            vals = np.where(ok, a[np.clip(r, 0, src.ny - 1), np.clip(c, 0, src.nx - 1)], np.nan)
        else:
            vals = src.sample_bilinear(X.ravel(), Y.ravel()).reshape(X.shape)
        return Grid(vals, xmin, ymax, cell)

    def read(self, bounds, cell):
        g = self._sample(self.grid, bounds, cell, "bilinear")
        g.data = g.data * self.z_to_m
        return g

    def manning(self, bounds, cell):
        if self.manning_grid is None:
            return None
        g = self._sample(self.manning_grid, bounds, cell, "near")
        if self.manning_lut:
            lut = dict(self.manning_lut)
            lut["default"] = self.default_n
            g.data = nd_data.nlcd_to_manning(g.data, lut)
            return g
        bad = ~np.isfinite(g.data) | (g.data <= 0) | (g.data > 1.0)
        g.data[bad] = self.default_n
        return g


class AscWriter(object):
    """writer for workflow.run_dam without GDAL: ESRI ASCII grids, `X.tif` -> `X.asc`."""

    def __init__(self, work_srs=None):
        self.work_srs = work_srs
        self.written = []

    def __call__(self, grid, path):
        out = os.path.splitext(path)[0] + ".asc"
        os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
        g = Grid(np.where(np.isfinite(grid.data), grid.data, grid_mod.NODATA), grid.x0, grid.y0,
                 grid.dx)
        g.write_asc(out, fmt="%.4f")
        self.written.append(out)
        return out

    @staticmethod
    def actual(path):
        """The file that exists for a path the workflow reports."""
        if os.path.exists(path):
            return path
        a = os.path.splitext(path)[0] + ".asc"
        return a if os.path.exists(a) else path


class Setup(object):
    """Terrain, writer and coordinate frame for one dam."""

    def __init__(self, terrain, writer, frame, work_srs, dem_bounds, notes=()):
        self.terrain, self.writer, self.frame = terrain, writer, frame
        self.work_srs, self.dem_bounds, self.notes = work_srs, dem_bounds, list(notes)


def manning_options(cfg):
    m = cfg.section("manning")
    n = m.get("n", 0.06)
    raster, nlcd = cfg.path_of(m.get("raster")), cfg.path_of(m.get("nlcd"))
    kw = {}
    if raster and nlcd:
        raise CliError("give manning.raster or manning.nlcd, not both")
    if nlcd:
        try:
            kw["manning_lut"] = nd_data.manning_lut(m.get("nlcd_n") if m.get("nlcd_n") not in (
                None, "") else None, n)
        except (ValueError, TypeError, AttributeError) as e:
            raise CliError("manning.nlcd_n: {}".format(e))
    for what, p in (("manning.raster", raster), ("manning.nlcd", nlcd)):
        if p and not os.path.isfile(p):
            raise CliError("{} does not exist: {}".format(what, p))
    return n, raster or nlcd or None, kw


def check_in_dem(pt, bounds, what="the dam point"):
    xmin, ymin, xmax, ymax = bounds
    if not (xmin <= pt[0] <= xmax and ymin <= pt[1] <= ymax):
        raise CliError(
            "{} ({:.2f}, {:.2f}) is outside the DEM, which covers x {:.2f} to {:.2f}, "
            "y {:.2f} to {:.2f}. Coordinates must be in the DEM's CRS.".format(
                what, pt[0], pt[1], xmin, xmax, ymin, ymax))


def open_dem(cfg, dam_pt, cache=None):
    """Setup for a dam at `dam_pt` (in the DEM's CRS). Cached per model CRS."""
    d = cfg.section("dem")
    path = cfg.path_of(d.get("path"))
    if not path:
        raise CliError("dem.path is required")
    if not os.path.exists(path):
        raise CliError("the DEM does not exist: {}".format(path))
    ext = os.path.splitext(path)[1].lower()
    reader = d.get("reader", "auto")
    use_asc = reader == "asc" or (reader == "auto" and ext == ".asc")
    if use_asc:
        return _open_asc(cfg, d, path, dam_pt, cache)
    return _open_gdal(cfg, d, path, dam_pt, cache)


def _open_asc(cfg, d, path, dam_pt, cache):
    if d.get("work_crs"):
        raise CliError("dem.work_crs cannot be used with an ESRI ASCII DEM (it is read as it "
                       "is); use a GeoTIFF with GDAL to re-project")
    key = ("asc", path)
    if cache is not None and key in cache:
        s = cache[key]
    else:
        n, mpath, kw = manning_options(cfg)
        if mpath and os.path.splitext(mpath)[1].lower() != ".asc":
            raise CliError("with an ESRI ASCII DEM the Manning / NLCD raster must be .asc too "
                           "(GDAL reads other formats)")
        terrain = AscTerrain(path, d.get("z_units", gdal_io.Z_AUTO), mpath, n, **kw)
        crs = d.get("crs")
        writer, notes = None, []
        if crs and have_gdal():
            try:
                unit = gdal_io.linear_unit_m(crs)
            except Exception as e:
                raise CliError("cannot understand dem.crs '{}': {}".format(crs, e))
            if unit != 1.0:
                raise CliError("dem.crs '{}' is not projected in metres; the models need "
                               "metres".format(crs))
            writer = gdal_io.GeoTiffWriter(crs)
        else:
            writer = AscWriter(crs)
            notes.append("Writing ESRI ASCII rasters (.asc) instead of GeoTIFFs: {}.".format(
                "GDAL is not available" if not have_gdal() else "dem.crs is not given"))
        s = Setup(terrain, writer, Frame(dem_epsg=crs), crs or "(DEM CRS)", terrain.bounds, notes)
        if cache is not None:
            cache[key] = s
    check_in_dem(dam_pt, s.dem_bounds)
    return s


def _open_gdal(cfg, d, path, dam_pt, cache):
    if not have_gdal():
        raise CliError(
            "reading {} needs GDAL's Python bindings (osgeo), which this Python does not "
            "have. Run with QGIS's Python (macOS: /Applications/QGIS-LTR.app/Contents/MacOS/"
            "bin/python3), or convert the DEM to an ESRI ASCII grid (.asc).".format(path))
    gdal, osr = gdal_io._gdal()
    ds = gdal.Open(path)
    if ds is None:
        raise CliError("GDAL cannot open the DEM {}".format(path))
    wkt = ds.GetProjection()
    if not wkt:
        raise CliError("the DEM {} has no coordinate system; assign one first, e.g. "
                       "gdal_edit.py -a_srs EPSG:26914 {}".format(path, path))
    gt = ds.GetGeoTransform()
    bounds = (gt[0], gt[3] + gt[5] * ds.RasterYSize, gt[0] + gt[1] * ds.RasterXSize, gt[3])
    ds = None
    check_in_dem(dam_pt, bounds)
    frame0 = Frame(osr_module=osr)
    dem_srs = frame0.srs(wkt)
    work_text = d.get("work_crs")
    if not work_text:
        geo = frame0.srs("EPSG:4269")
        ll = frame0._ct(dem_srs, geo).TransformPoint(float(dam_pt[0]), float(dam_pt[1]))
        work_text = "EPSG:{}".format(gdal_io.utm_epsg(ll[0], ll[1]))
    key = ("gdal", path, work_text, d.get("resample"))
    if cache is not None and key in cache:
        return cache[key]
    work_srs = frame0.srs(work_text)
    if gdal_io.linear_unit_m(work_text) != 1.0:
        raise CliError("The model CRS must be projected in metres ({} is not)".format(work_text))
    n, mpath, kw = manning_options(cfg)
    kw["resample"] = d.get("resample")
    try:
        terrain = gdal_io.GdalTerrain(path, work_text, d.get("z_units", gdal_io.Z_AUTO), mpath, n,
                                      **kw)
    except ValueError as e:                            # a bad NDBREACH_DEM_RESAMPLE value
        raise CliError(str(e))
    unit = gdal_io.linear_unit_m(wkt)
    target, res = None, None
    if unit and not gdal_io.same_crs(wkt, work_text):
        target = gdal_io.epsg_of(wkt) or wkt
        res = cfg.get("model", "cell_size_m", 10.0) / unit
    writer = gdal_io.GeoTiffWriter(work_text, target, res)
    frame = Frame(dem_srs, work_srs, osr, gdal_io.epsg_of(wkt))
    s = Setup(terrain, writer, frame, work_text, bounds)
    if cache is not None:
        cache[key] = s
    return s


# --------------------------------------------------------------------------------------
# building a run from the configuration
# --------------------------------------------------------------------------------------
def dam_dict(cfg, row=None):
    """The dam section, overridden by the values of a table row."""
    d = dict(cfg.section("dam"))
    for k, v in (row or {}).items():
        if v is not None:
            d[k] = v
    return d


def build_dam(d, need_top=False):
    kw = {}
    for f in dataclasses.fields(brmod.DamData):
        kw[f.name] = d.get(f.name)
    if not kw.get("dam_id"):
        raise CliError("dam.dam_id is required")
    kw["dam_id"] = str(kw["dam_id"]).strip()
    kw["name"] = kw.get("name") or ""
    for key, what in (("dam_height_ft", "the dam height (ft)"),
                      ("storage_top_acft", "the storage at the top of dam (ac-ft)")):
        if not kw.get(key) or kw[key] <= 0:
            raise CliError("dam.{} is required and must be greater than 0: {}".format(key, what))
    if need_top and kw.get("top_of_dam_ft") is None:
        kw["top_of_dam_ft"] = kw["dam_height_ft"]
    return brmod.DamData(**kw)


def breach_overrides(cfg):
    ov = dict(cfg.get("breach", "overrides", {}) or {})
    return brmod.BreachOverrides(**ov)


def _builtin_or(value, other):
    """The built-in engine when the value is 'builtin' (3.11.0), else other()."""
    if str(value or "").strip().lower() in lfmod.BUILTIN_WORDS:
        return lfmod.BUILTIN_EXE
    return other()


def find_models(cfg, opts, need_lisflood=True, need_anuga=None):
    """(lisflood exe, anuga python): command line, then the configuration, then auto-detect."""
    exe = _builtin_or(getattr(opts, "lisflood", "") or cfg.get("model", "lisflood_exe"),
                      lambda: (getattr(opts, "lisflood", "")
                               or cfg.get_path("model", "lisflood_exe")
                               or nd_data.detect_lisflood()))
    py = (getattr(opts, "anuga_python", "") or cfg.get_path("model", "anuga_python")
          or nd_data.detect_anuga_python())
    tier = cfg.get("model", "tier", workflow.TIER_LISFLOOD)
    if need_anuga is None:
        need_anuga = tier == workflow.TIER_BOTH
    if need_lisflood and (not exe or not os.path.isfile(exe)):
        raise CliError(
            "no LISFLOOD-FP executable: set model.lisflood_exe, pass --lisflood, set "
            "NDBREACH_LISFLOOD, or build it with scripts/install_macos.sh. Found: '{}'".format(
                exe))
    if need_anuga and (not py or not os.path.isfile(py)):
        raise CliError("no ANUGA Python interpreter (needed for model.tier '{}'): set "
                       "model.anuga_python, pass --anuga-python or set NDBREACH_ANUGA_PYTHON. "
                       "Found: '{}'".format(tier, py))
    return exe, py


def build_settings(cfg, opts, need_models=True):
    st = workflow.RunSettings()
    for key, (attr, _spec) in MODEL_KEYS.items():
        if key in cfg.section("model"):
            v = cfg.get("model", key)
            if key in ("lisflood_exe", "anuga_python"):
                continue                                   # resolved by find_models
            if key == "sim_time_hr" and not v:
                v = None                                   # 0 = automatic, as in QGIS
            if key == "arrival_contours_hr":
                v = tuple(v)
            if not hasattr(st, attr):
                raise CliError("this version of the plugin has no run setting '{}'".format(attr))
            setattr(st, attr, v)
    st.manning_n = cfg.get("manning", "n", st.manning_n)
    if getattr(opts, "threads", None) is not None:
        st.threads = opts.threads
    st.lisflood_exe, st.anuga_python = find_models(cfg, opts, need_lisflood=need_models,
                                                   need_anuga=None if need_models else False)
    st.planning_cell_m = max(30.0, st.cell_size_m)
    st.overrides = breach_overrides(cfg)
    st.breach_method = cfg.get("breach", "method", st.breach_method)
    st.user_hydrograph = cfg.get_path("breach", "user_csv") or None
    out = cfg.section("output")
    st.keep_model_files = out.get("keep_model_files", st.keep_model_files)
    st.products = out.get("products", st.products)
    st.resume = bool(getattr(opts, "resume", False) or out.get("resume", False))
    st.impact_look_ahead_mi = cfg.get("layers", "lookahead_mi", st.impact_look_ahead_mi)
    sc = cfg.section("scenario")
    st.baseline = sc.get("baseline", st.baseline)
    if "incr_thresholds_ft" in sc:
        st.incr_thresholds_ft = tuple(sc["incr_thresholds_ft"])
    return st


def build_flood(cfg, dam, files, pool_ft=None):
    """FloodInputs for the flood scenario; a dam folder's files win over the configuration
    (same rules as the QGIS tools' flood_inputs)."""
    sc = cfg.section("scenario")
    inflow = files.get("pmf_inflow") or cfg.get_path("scenario", "inflow_csv")
    if not inflow:
        raise brmod.BreachInputError("the flood scenario needs the PMF inflow hydrograph "
                                     "(a CSV: time hr, discharge cfs)")
    spill = files.get("spillway") or cfg.get_path("scenario", "spillway_csv")
    pool = pool_ft if pool_ft is not None else sc.get("normal_pool_ft")
    trig = sc.get("trigger", floodmod.TRIGGER_PEAK)
    val = sc.get("trigger_value")
    if trig != floodmod.TRIGGER_PEAK and val is None:
        raise brmod.BreachInputError("give scenario.trigger_value (the failure time or pool "
                                     "elevation) for the chosen trigger")
    base = files.get("baseline") or cfg.get_path("scenario", "baseline_csv")
    for what, p in (("inflow", inflow), ("spillway rating", spill), ("baseline outflow", base)):
        if p and not os.path.isfile(p):
            raise CliError("the {} CSV does not exist: {}".format(what, p))
    return floodmod.FloodInputs.from_files(
        inflow, spill or None, normal_pool_ft=pool, crest_length_ft=dam.crest_length_ft,
        trigger=trig, trigger_value=val, baseline_csv=base or None)


class LayerCache(object):
    """GeoJSON files read once and converted per model CRS."""

    def __init__(self):
        self.raw = {}
        self.work = {}

    def read(self, path):
        if path not in self.raw:
            if not os.path.isfile(path):
                raise CliError("the layer does not exist: {}".format(path))
            self.raw[path] = read_geojson(path)
        return self.raw[path]

    def in_work(self, path, setup, vector_crs, dem_projected):
        key = (path, setup.work_srs)
        if key not in self.work:
            self.work[key] = layer_to_work(self.read(path), setup.frame, vector_crs,
                                           dem_projected)
        return self.work[key]


def _point_ids(feats, id_field):
    xy, ids = [], []
    for i, (parts, props) in enumerate(feats):
        for p in parts:
            for q in p:
                xy.append((float(q[0]), float(q[1])))
                ids.append(str(props.get(id_field)) if id_field and props.get(id_field)
                           is not None else str(i))
    return np.asarray(xy, dtype=float).reshape(-1, 2), ids


def apply_layers(st, cfg, setup, center_xy, cache, dem_projected=True):
    """Read the flow-path layers around the dam into `st` (the counterpart of the QGIS
    tools' apply_features). Returns notes."""
    ly = cfg.section("layers")
    notes = []
    radius = 0.75 * st.max_extent_mi * MILE
    vcrs = ly.get("vector_crs")

    def layer(key):
        p = cfg.path_of(ly.get(key))
        if not p:
            return None
        return cache.in_work(p, setup, vcrs, dem_projected)

    def near(feats, kind):
        out = [(parts, props) for k, parts, props in feats if k == kind
               and any(in_box(a, center_xy, radius) for a in parts)]
        return out

    def dropped(key, feats, kept):
        if feats and not kept:
            notes.append("layers.{}: none of its {} feature(s) lies within {:.0f} mi of the "
                         "dam; check that it is in the DEM's CRS".format(
                             key, len(feats), radius / MILE))

    rec = impmod.Receptors()
    for key, kind in (("receptor_points", impmod.KIND_STRUCTURE),
                      ("receptor_roads", impmod.KIND_ROAD),
                      ("receptor_rail", impmod.KIND_RAIL)):
        feats = layer(key)
        if feats is None:
            continue
        if kind == impmod.KIND_STRUCTURE:
            kept = near(feats, "point")
            xy, ids = _point_ids(kept, None)
            rec.add_points(xy, kind, ids)
        else:
            kept = near(feats, "line")
            rec.add_lines([p for parts, _pr in kept for p in parts if len(p) >= 2], kind)
        dropped(key, feats, kept)
    st.receptors = rec if len(rec) else None
    if st.receptors is not None:
        notes.append("Receptors within {:.0f} mi of the dam: {}".format(
            radius / MILE, impmod.count_text(rec, np.ones(len(rec), bool))))
    # culverts, bridges, notches
    structs = []
    feats = layer("structures")
    if feats is not None:
        names = []
        for _k, _p, props in feats:
            for k in props:
                if k not in names:
                    names.append(k)
        chosen = dict(ly.get("structure_fields") or {})
        for k in chosen:
            if k not in STRUCT_KEYS:
                raise CliError("layers.structure_fields: unknown key '{}'{}; keys are: {}".format(
                    k, _suggest(k, STRUCT_KEYS), ", ".join(STRUCT_KEYS)))
        low = {n.lower(): n for n in names}
        fld = {}
        for key, syns in STRUCT_FIELD_SYNONYMS.items():
            c = chosen.get(key) or next((low[s] for s in syns if s in low), "")
            if c and c not in names:
                raise CliError("layers.structure_fields.{}: the layer has no property '{}' "
                               "(it has: {})".format(key, c, ", ".join(names)))
            fld[key] = c
        dflt = ly.get("structure_defaults") or {}
        dk = dflt.get("kind", structmod.KIND_CULVERT)
        if dk not in STRUCT_KINDS:
            raise CliError("layers.structure_defaults.kind is '{}', not one of: {}".format(
                dk, ", ".join(STRUCT_KINDS)))
        dw = num(dflt.get("width_ft"), 4.0)
        dh = num(dflt.get("height_ft"), 4.0)
        kept = near(feats, "line")
        dropped("structures", feats, kept)
        i = 0
        for parts, at in kept:
            for line in parts:
                if len(line) < 2:
                    continue
                g = lambda k: at.get(fld[k]) if fld[k] else None
                ktxt = str(g("S_KIND") or "").lower()
                kind = (structmod.KIND_BRIDGE if "bridge" in ktxt else
                        structmod.KIND_NOTCH if any(w in ktxt for w in ("notch", "breach", "open"))
                        else structmod.KIND_CULVERT if ktxt else dk)
                shape_txt = str(g("S_SHAPE") or "").lower()
                shape = "pipe" if any(w in shape_txt for w in ("pipe", "round", "circ", "cmp")) \
                    else "box"
                w_ft = num(g("S_WIDTH"), dw)
                h_ft = w_ft if shape == "pipe" else num(g("S_HEIGHT"), dh)
                inv_u, inv_d = num(g("S_INV_UP")), num(g("S_INV_DN"))
                structs.append(structmod.Structure(
                    kind, line, w_ft * FT, h_ft * FT, int(num(g("S_BARRELS"), 1) or 1), shape,
                    inv_u * FT if inv_u is not None else None,
                    inv_d * FT if inv_d is not None else None,
                    name=str(g("S_NAME") or "{} {}".format(kind, i + 1))))
                i += 1
    st.structures = structs
    # DEM conditioning
    ed = condmod.Edits()
    feats = layer("channels")
    if feats is not None:
        w = ly.get("channel_width_ft", 15.0) * FT
        dpt = ly.get("channel_depth_ft", 1.0) * FT
        kept = near(feats, "line")
        dropped("channels", feats, kept)
        i = 0
        for parts, _pr in kept:
            for line in parts:
                if len(line) >= 2:
                    i += 1
                    ed.channels.append(condmod.ChannelBurn(line, w, dpt,
                                                           name="channel {}".format(i)))
    feats = layer("ponds")
    if feats is not None:
        dpt = ly.get("pond_depth_ft", 5.0) * FT
        kept = near(feats, "polygon")
        dropped("ponds", feats, kept)
        i = 0
        for parts, _pr in kept:
            for ring in parts:
                if len(ring) >= 3:
                    i += 1
                    ed.ponds.append(condmod.PondLower(ring, dpt, name="pond {}".format(i)))
    st.edits = None if ed.empty() else ed
    feats = layer("refine_lines")
    st.refine_lines = []
    if feats is not None:
        kept = near(feats, "line")
        dropped("refine_lines", feats, kept)
        st.refine_lines = [p for parts, _pr in kept for p in parts if len(p) >= 2]
    feats = layer("gauges")
    st.gauges = []
    if feats is not None:
        gname = ly.get("gauge_name_field") or ""
        kept = near(feats, "point")
        dropped("gauges", feats, kept)
        xy, ids = _point_ids(kept, gname or None)
        st.gauges = [{"name": ids[i] if gname else "gauge {}".format(i + 1),
                      "xy": (float(xy[i][0]), float(xy[i][1]))} for i in range(len(xy))]
    if st.structures:
        notes.append("{} culvert/bridge/notch structure(s) read".format(len(st.structures)))
    return notes


class Prepared(object):
    """Everything run_dam needs for one dam."""

    def __init__(self, dam, st, setup, dam_xy, inflow_xy, folder, notes):
        self.dam, self.st, self.setup = dam, st, setup
        self.dam_xy, self.inflow_xy, self.folder, self.notes = dam_xy, inflow_xy, folder, notes


def prepare(cfg, opts, row=None, cache=None, layers=None, need_models=True, scenario=None,
            pool_ft=None):
    """Dam, settings, terrain and layers for one dam from the configuration (and a table row).
    `scenario` overrides scenario.type ('sunny' or 'flood'); 'per_dam' looks in the dam's folder."""
    d = dam_dict(cfg, row)
    dam = build_dam(d)
    pt = d.get("point")
    if not pt:
        raise CliError("the dam's point (dam.point = [x, y] in the DEM's CRS) is required")
    out_root = cfg.get_path("output", "folder")
    if not out_root:
        raise CliError("output.folder is required")
    folder = os.path.join(out_root, safe_name(dam.dam_id))
    setup = open_dem(cfg, pt, cache)
    dam_xy = setup.frame.xy(pt[0], pt[1])
    inflow_xy = None
    if d.get("inflow_point"):
        ip = d["inflow_point"]
        check_in_dem(ip, setup.dem_bounds, "the breach inflow point")
        inflow_xy = setup.frame.xy(ip[0], ip[1])
    st = build_settings(cfg, opts, need_models)
    files = nd_data.dam_input_files(folder)
    scen = scenario or cfg.get("scenario", "type", workflow.SCENARIO_SUNNY)
    if scen == "per_dam":
        scen = workflow.SCENARIO_FLOOD if "pmf_inflow" in files or cfg.get_path(
            "scenario", "inflow_csv") else workflow.SCENARIO_SUNNY
    st.scenario = scen
    if files.get("breach"):
        st.user_hydrograph = files["breach"]
    if st.breach_method == brmod.METHOD_USER:
        if not st.user_hydrograph:
            raise brmod.BreachInputError(
                "own-hydrograph method: give breach.user_csv or put breach_hydrograph.csv in "
                "the dam's inputs folder")
        if not os.path.isfile(st.user_hydrograph):
            raise CliError("the hydrograph CSV does not exist: {}".format(st.user_hydrograph))
    notes = list(setup.notes)
    dem_projected = True
    notes += apply_layers(st, cfg, setup, dam_xy, layers or LayerCache(), dem_projected)
    if st.scenario == workflow.SCENARIO_FLOOD:
        st.flood = build_flood(cfg, dam, files, pool_ft)
    return Prepared(dam, st, setup, dam_xy, inflow_xy, folder, notes)


# --------------------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------------------
def _describe_result(res, writer):
    say()
    say("Results: " + res.folder)
    labels = {"depth": "Depth (Max)", "velocity": "Velocity (Max)", "arrival": "Arrival Time",
              "wse": "WSE (Max)"}
    actual = getattr(writer, "actual", lambda p: p)
    for k, p in res.final.items():
        say("  {:15s} {}".format(labels.get(k, k), actual(p)))
    if res.increment.get("depth"):
        say("  {:15s} {}".format("Incremental", actual(res.increment["depth"])))
    for k in ("report_md", "report_html", "eap_map"):
        p = getattr(res, k, "")
        if p and os.path.exists(p):
            say("  {:15s} {}".format(k.replace("_", " "), p))


def _print_summary(res):
    for w in res.warnings:
        say("Check: " + str(w))
    for k, v in res.summary.items():
        say("{}: {}".format(k, v))


def cmd_run(args):
    cfg = load_config(args.config)
    if args.output:
        cfg.data["output"]["folder"] = os.path.abspath(args.output)
    try:
        p = prepare(cfg, args)
    except brmod.BreachInputError as e:
        raise CliError(str(e))
    say("Model CRS {}; DEM elevations in {}".format(p.setup.work_srs,
                                                    getattr(p.setup.terrain, "z_note", "?")))
    for n in p.notes:
        say(n)
    say("Dam {}: LISFLOOD-FP {}{}; scenario {}; cell {:g} m; output {}".format(
        p.dam.dam_id, p.st.lisflood_exe,
        " + ANUGA" if p.st.tier == workflow.TIER_BOTH else "", p.st.scenario,
        p.st.cell_size_m, p.folder))
    pr = Progress()
    t0 = time.time()
    try:
        res = workflow.run_dam(p.dam, p.dam_xy, p.st, p.setup.terrain, p.setup.writer, p.folder,
                               p.inflow_xy, pr.progress, pr.log, pr.cancel)
    except lfmod.Cancelled:
        say("Cancelled.")
        return 130
    except brmod.BreachInputError as e:
        raise CliError(str(e))
    except lfmod.ModelError as e:
        sys.stderr.write("ndbreach: model error: {}\n".format(e))
        return 1
    _print_summary(res)
    _describe_result(res, p.setup.writer)
    say("Finished in {:.1f} min.".format((time.time() - t0) / 60.0))
    return 0


def cmd_hydrograph(args):
    cfg = load_config(args.config)
    d = dam_dict(cfg)
    try:
        dam = build_dam(d, need_top=True)
        hy = brmod.make_hydrograph(dam, cfg.get("breach", "method", brmod.METHOD_PIPING),
                                   breach_overrides(cfg),
                                   cfg.get_path("breach", "user_csv") or None)
    except brmod.BreachInputError as e:
        raise CliError(str(e))
    except OSError as e:
        raise CliError("cannot read the hydrograph CSV: {}".format(e))
    out = args.output_file
    if not out:
        root = cfg.get_path("output", "folder") or cfg.base
        out = os.path.join(root, safe_name(dam.dam_id), "breach_hydrograph.csv")
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    hy.to_csv(out)
    say("Hydrograph: " + out)
    try:
        products = importlib.import_module(PKG + ".core.products")
        svg = os.path.splitext(out)[0] + ".svg"
        with open(svg, "w", encoding="utf-8") as f:
            f.write(products.hydrograph_plot(hy))
        say("Plot: " + svg)
    except Exception as e:
        say("No plot: {}".format(e))
    for k, v in hy.summary().items():
        say("{}: {}".format(k, v))
    for n in hy.notes:
        say("Check: " + n)
    return 0


def cmd_estimate(args):
    cfg = load_config(args.config)
    try:
        p = prepare(cfg, args)
        plan = workflow.preview(p.dam, p.dam_xy, p.st, p.setup.terrain, p.inflow_xy, p.folder)
    except brmod.BreachInputError as e:
        raise CliError(str(e))
    for n in p.notes:
        say(n)
    if not plan.get("breach_occurs"):
        say("The dam does not fail in this flood: there is nothing to model.")
        return 0
    say("Timing a test flood...")
    with tempfile.TemporaryDirectory(prefix="ndbreach_cal_") as tmp:
        rate = lfmod.calibrate(p.st.lisflood_exe, tmp, p.st.threads)
    if not rate:
        sys.stderr.write("ndbreach: the LISFLOOD-FP test flood did not run; use `check`\n")
        return 1
    est = estmod.estimate(plan, rate, p.st.tier == workflow.TIER_BOTH)
    for ln in estmod.describe(plan, est):
        say(ln)
    return 0


def cmd_check(args):
    exe = _builtin_or(args.lisflood, lambda: args.lisflood or nd_data.detect_lisflood())
    py = args.anuga_python or nd_data.detect_anuga_python()
    lines, bad = [], False
    lines.append("OK Python {} at {}".format(sys.version.split()[0], sys.executable))
    lines.append("OK numpy {}".format(np.__version__))
    if have_gdal():
        from osgeo import gdal
        lines.append("OK GDAL {} (GeoTIFF, VRT and other formats readable)".format(
            gdal.VersionInfo("RELEASE_NAME")))
    else:
        lines.append("NOTE GDAL's Python bindings (osgeo) are not available here: only ESRI "
                     "ASCII DEMs work, and rasters are written as .asc. Use QGIS's Python "
                     "for GeoTIFFs.")
    with tempfile.TemporaryDirectory(prefix="ndbreach_check_") as tmp:
        if not exe:
            lines.append("PROBLEM LISFLOOD-FP not found. Build it with scripts/install_macos.sh "
                         "(or build_lisflood_macos.sh), or pass --lisflood / set "
                         "NDBREACH_LISFLOOD.")
            bad = True
        else:
            r = lfmod.check_lisflood(exe, os.path.join(tmp, "lisflood"))
            ok = r["ok"] and r.get("patched")
            bad = bad or not ok
            lines.append(("OK " if ok else "PROBLEM ") + r["message"])
        if py:
            a = anmod.check_anuga(py, tmp)
            bad = bad or not a["ok"]
            lines.append(("OK " if a["ok"] else "PROBLEM ") + "ANUGA ({}): {}".format(
                py, a["message"]))
        else:
            lines.append("NOTE ANUGA: no ANUGA environment found (only needed for the ANUGA "
                         "tier). `ndbreach_cli.py setup-anuga` makes one, no conda needed.")
    for ln in lines:
        (sys.stderr if ln.startswith("PROBLEM") else sys.stdout).write(ln + "\n")
    sys.stdout.flush()
    return 1 if bad else 0


def cmd_setup_anuga(args):
    """Download micromamba and create the private ANUGA environment (core/anuga_setup.py)."""
    AS = anuga_setup
    try:
        if args.remove:
            say("Removed {}".format(AS.data_dir()) if AS.remove() else "Nothing to remove")
            return 0
        py = AS.setup(reinstall=args.reinstall, log=say)
    except AS.SetupError as e:
        sys.stderr.write("PROBLEM {}\n".format(e))
        return 1
    sys.stdout.write("OK ANUGA Python: {}\n(found automatically; or set NDBREACH_ANUGA_PYTHON / "
                     "--anuga-python to it)\n".format(py))
    return 0


def cmd_template(args):
    text = template_text()
    if args.output_file:
        with open(args.output_file, "w", encoding="utf-8") as f:
            f.write(text)
        say("Wrote " + args.output_file)
    else:
        sys.stdout.write(text)
    return 0


def cmd_selftest(args):
    st = importlib.import_module(PKG + ".core.selftest")
    return st.main(args.rest)


def cmd_hecras_reexport(args):
    """Re-make the HEC-RAS rasters of a finished plan HDF with depth from the terrain under
    each grid cell (no HEC-RAS run)."""
    hec = importlib.import_module(PKG + ".core.hecras")
    try:
        rep = hec.reexport_results(
            args.hdf, terrain=args.terrain or None, out_dir=args.out or None,
            dam_folder=args.dam_folder or None, like=args.grid or None,
            area=args.area or None, units=args.units or None,
            arrival_depth_ft=args.arrival_depth_ft, t0_hr=args.t0_hr,
            terrain_z=args.terrain_z or None, overwrite=args.overwrite, log=say)
    except hec.HecRasError as e:
        raise CliError(str(e))
    a, b = rep["areas_old_cell_min"], rep["areas_new"]
    say("Depth basis: {}".format(rep["depth_basis"]))
    say("Wet area: {:.1f} ac (was {:.1f} ac with the cell minimum)".format(
        b["wet_ac"], a["wet_ac"]))
    for n in rep.get("notes") or []:
        say("Note: " + n)
    return 0


# -- batch -----------------------------------------------------------------------------
def read_dams_table(path):
    """(headers, rows) of the dams CSV."""
    try:
        with open(path, newline="", encoding="utf-8-sig") as f:
            text = f.read()
    except OSError as e:
        raise CliError("cannot read the dams table {}: {}".format(path, e.strerror or e))
    if not text.strip():
        raise CliError("the dams table {} is empty".format(path))
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    rd = csv.DictReader(text.splitlines(), dialect=dialect)
    rows = list(rd)
    heads = [h for h in (rd.fieldnames or []) if h is not None]
    if not heads or not rows:
        raise CliError("the dams table {} has no rows".format(path))
    return heads, rows


def map_columns(headers, cfg):
    """({F_KEY: column}, [messages], (x column, y column))"""
    auto, _how = nd_data.suggest_fields(headers)
    given = cfg.get("batch", "fields", {}) or {}
    fld, used = {}, []
    lower = {h.lower(): h for h in headers}
    for key in given:
        if key not in BATCH_FIELD_KEYS:
            raise CliError("batch.fields: unknown key '{}'{}; keys are: {}".format(
                key, _suggest(key, BATCH_FIELD_KEYS), ", ".join(BATCH_FIELD_KEYS)))
    for key in BATCH_FIELD_KEYS:
        chosen = given.get(key)
        if chosen:
            if chosen not in headers:
                raise CliError("batch.fields.{}: the table has no column '{}' (it has: {})".format(
                    key, chosen, ", ".join(headers)))
            fld[key] = chosen
        elif key in auto:
            fld[key] = auto[key]
            used.append("{} -> {}".format(key, auto[key]))
        else:
            fld[key] = ""

    def pick(explicit, names, what):
        if explicit:
            if explicit not in headers:
                raise CliError("batch.{}: the table has no column '{}' (it has: {})".format(
                    what, explicit, ", ".join(headers)))
            return explicit
        for n in names:
            if n in lower:
                return lower[n]
        return ""
    xc = pick(cfg.get("batch", "x_field"), X_NAMES, "x_field")
    yc = pick(cfg.get("batch", "y_field"), Y_NAMES, "y_field")
    return fld, used, (xc, yc)


def _batch_scenario(cfg, folder):
    t = cfg.get("scenario", "type", workflow.SCENARIO_SUNNY)
    if t == "per_dam":
        files = nd_data.dam_input_files(folder)
        return workflow.SCENARIO_FLOOD if "pmf_inflow" in files else workflow.SCENARIO_SUNNY
    return t


def cmd_batch(args):
    cfg = load_config(args.config)
    if args.output:
        cfg.data["output"]["folder"] = os.path.abspath(args.output)
    headers, rows = read_dams_table(args.dams)
    fld, used, (xc, yc) = map_columns(headers, cfg)
    if used:
        say("Columns matched by name: " + "; ".join(used))
    dflt = cfg.section("dam")
    for key, dk, what in (("F_ID", "dam_id", "the dam ID"),
                          ("F_HEIGHT", "dam_height_ft", "the dam height (ft)"),
                          ("F_STORAGE", "storage_top_acft", "the storage at the top of dam")):
        if not fld.get(key) and dflt.get(dk) is None:
            raise CliError("no column was found for {} ({}); name it in batch.fields, e.g. "
                           "\"batch\": {{\"fields\": {{\"{}\": \"YOUR_COLUMN\"}}}}".format(
                               what, key, key))
    if not (xc and yc):
        raise CliError("the dams table needs point columns (x and y in the DEM's CRS, or "
                       "lon/lat with batch.crs): name them in batch.x_field / batch.y_field. "
                       "Columns: {}".format(", ".join(headers)))
    out_root = cfg.get_path("output", "folder")
    if not out_root:
        raise CliError("output.folder is required")
    os.makedirs(out_root, exist_ok=True)
    if args.limit:
        rows = rows[:args.limit]
    skip_done = cfg.get("batch", "skip_done", True)
    cache, layers = {}, LayerCache()
    batch_crs = cfg.get("batch", "crs")
    results = []
    n = max(1, len(rows))
    base_opts = args
    for i, row in enumerate(rows):
        if CANCEL["flag"]:
            break
        vals = {}
        for key, dk in BATCH_TO_DAM.items():
            col = fld.get(key)
            if not col:
                continue
            raw = row.get(col)
            vals[dk] = ((raw or "").strip() or None) if dk in ("dam_id", "name") else num(raw)
        dam_id = str(vals.get("dam_id") or dflt.get("dam_id") or "row{}".format(i + 1)).strip()
        vals["dam_id"] = dam_id
        folder = os.path.join(out_root, safe_name(dam_id))
        out = dict.fromkeys(BATCH_FIELDS, "")
        out.update({"Dam": dam_id, "Folder": folder})
        pr = Progress(100.0 * i / n, 100.0 * (i + 1) / n)
        depth_names = (workflow.FINAL_NAMES["depth"],
                       os.path.splitext(workflow.FINAL_NAMES["depth"])[0] + ".asc")
        if skip_done and any(os.path.exists(os.path.join(folder, nm)) for nm in depth_names):
            out.update({"Status": "skipped", "Message": "already has Depth (Max)"})
            results.append(out)
            continue
        say("=== {} ({}/{})".format(dam_id, i + 1, len(rows)))
        t0 = time.time()
        try:
            x, y = num(row.get(xc)), num(row.get(yc))
            if x is None or y is None:
                raise brmod.BreachInputError("no point coordinates ({}, {})".format(xc, yc))
            if batch_crs:
                x, y = _batch_point_to_dem(cfg, cache, batch_crs, x, y)
            vals["point"] = [x, y]
            scen = _batch_scenario(cfg, folder)
            pool = num(row.get(fld["F_POOL"])) if fld.get("F_POOL") else None
            p = prepare(cfg, base_opts, vals, cache, layers, True, scen, pool)
            out["Scenario"] = p.st.scenario
            for note in p.notes:
                say(note)
            res = workflow.run_dam(p.dam, p.dam_xy, p.st, p.setup.terrain, p.setup.writer,
                                   p.folder, None, pr.progress, pr.log, pr.cancel)
            s = res.summary
            if not s.get("breach_occurs", True):
                out.update({"Status": "no breach",
                            "Message": "the dam does not fail in this flood"})
            else:
                out.update({"Status": "done", "Final_Model": s["final_model"],
                            "Peak_Breach_cfs": s["peak_breach_cfs"],
                            "Storage_At_Failure_acft": s["storage_at_failure_acft"],
                            "Flooded_ac": s["flooded_acres"], "Max_Depth_ft": s["max_depth_ft"],
                            "Max_Velocity_fps": s["max_velocity_fps"],
                            "Latest_Arrival_hr": s["latest_arrival_hr"],
                            "Receptors_Flooded": s.get("receptors_flooded", ""),
                            "Runs": s["lisflood_runs"],
                            "Domain_mi": "{} x {}".format(*s["domain_mi"]),
                            "Warnings": " | ".join(res.warnings)})
            for w in res.warnings:
                say("Check ({}): {}".format(dam_id, w))
        except lfmod.Cancelled:
            out.update({"Status": "cancelled"})
            results.append(out)
            break
        except (lfmod.ModelError, brmod.BreachInputError, CliError) as e:
            out.update({"Status": "failed", "Message": str(e)[:500]})
            sys.stderr.write("ndbreach: {}: {}\n".format(dam_id, e))
        except Exception as e:
            out.update({"Status": "failed", "Message": "{}: {}".format(type(e).__name__, e)})
            sys.stderr.write("ndbreach: {}: {}\n".format(dam_id, traceback.format_exc()
                                                         if args.verbose else e))
        out["Minutes"] = round((time.time() - t0) / 60.0, 1)
        results.append(out)
    path = os.path.join(out_root, "Batch_Breach_Runs.csv")
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=BATCH_FIELDS)
        w.writeheader()
        w.writerows(results)
    counts = dict((k, sum(1 for r in results if r["Status"] == k))
                  for k in ("done", "failed", "skipped", "cancelled", "no breach"))
    say("{done} run, {failed} failed, {skipped} skipped, {no breach} with no breach".format(
        **counts))
    say("Summary: " + path)
    if counts["cancelled"] or CANCEL["flag"]:
        return 130
    return 3 if counts["failed"] else 0


def _batch_point_to_dem(cfg, cache, crs_text, x, y):
    """A table point in `crs_text` -> the DEM's CRS (needs GDAL)."""
    if not have_gdal():
        raise CliError("batch.crs '{}' needs GDAL's Python bindings, which this Python does "
                       "not have".format(crs_text))
    gdal, osr = gdal_io._gdal()
    path = cfg.get_path("dem", "path")
    ds = gdal.Open(path)
    if ds is None or not ds.GetProjection():
        raise CliError("the DEM has no coordinate system to convert the table's points to")
    fr = Frame(osr_module=osr)
    dem = fr.srs(ds.GetProjection())
    src = fr.srs(crs_text)
    fr.dem_srs = dem
    return fr.to_dem(x, y, src)


# --------------------------------------------------------------------------------------
def build_parser():
    ap = argparse.ArgumentParser(
        prog="ndbreach_cli.py", description="ND Dam Breach Runner without QGIS (see the top of "
        "this file, or `template`, for the configuration).",
        epilog="exit status: 0 done, 1 run/model failed, 2 bad input, 3 batch had failures, "
               "130 cancelled")
    sub = ap.add_subparsers(dest="command")
    sub.required = True

    def models(p):
        p.add_argument("--lisflood", default="", help="LISFLOOD-FP executable")
        p.add_argument("--anuga-python", default="", help="ANUGA environment's python")
        p.add_argument("--threads", type=int, default=None, help="model threads (0 = all)")

    p = sub.add_parser("run", help="run one dam (the same outputs as the QGIS tool)")
    p.add_argument("config")
    p.add_argument("--resume", action="store_true", help="reuse finished LISFLOOD-FP runs")
    p.add_argument("--output", default="", help="output folder (overrides the configuration)")
    models(p)
    p.set_defaults(func=cmd_run)
    p = sub.add_parser("hydrograph", help="breach hydrograph only (no DEM needed)")
    p.add_argument("config")
    p.add_argument("-o", "--output-file", default="", help="hydrograph CSV to write")
    p.set_defaults(func=cmd_hydrograph)
    p = sub.add_parser("estimate", help="time a test flood and estimate the run time")
    p.add_argument("config")
    models(p)
    p.set_defaults(func=cmd_estimate)
    p = sub.add_parser("check", help="find LISFLOOD-FP and ANUGA and test them")
    models(p)
    p.set_defaults(func=cmd_check)
    p = sub.add_parser("setup-anuga", help="one-time download of a private ANUGA environment "
                       "(micromamba + conda-forge; no conda needed)")
    p.add_argument("--reinstall", action="store_true", help="create it again even if it works")
    p.add_argument("--remove", action="store_true", help="delete the private environment")
    p.set_defaults(func=cmd_setup_anuga)
    p = sub.add_parser("template", help="print a starting configuration (redirect it to a file)")
    p.add_argument("-o", "--output-file", default="")
    p.set_defaults(func=cmd_template)
    p = sub.add_parser("batch", help="run a table of dams (CSV) with one configuration")
    p.add_argument("dams", metavar="DAMS.csv")
    p.add_argument("config", metavar="CONFIG.json")
    p.add_argument("--resume", action="store_true")
    p.add_argument("--output", default="")
    p.add_argument("--limit", type=int, default=0, help="only the first N dams")
    p.add_argument("--verbose", action="store_true", help="tracebacks for unexpected errors")
    models(p)
    p.set_defaults(func=cmd_batch)
    p = sub.add_parser("selftest", help="test GDAL, the models and a small run on this computer")
    p.add_argument("rest", nargs=argparse.REMAINDER, help="options of core/selftest.py")
    p.set_defaults(func=cmd_selftest)
    p = sub.add_parser("hecras-reexport", help="re-make the HEC-RAS rasters of a finished "
                       "plan HDF with depth from the terrain (no HEC-RAS run)")
    p.add_argument("hdf", metavar="PLAN.hdf")
    p.add_argument("--terrain", default="", help="the model's terrain GeoTIFF (default "
                   "<project>/Terrain/*.tif next to the HDF)")
    p.add_argument("--dam-folder", default="", help="its Depth (Max).tif sets the grid; "
                   "output goes to <dam folder>/_hecras/reexport_<UTC>")
    p.add_argument("--grid", default="", help="raster whose grid to write on")
    p.add_argument("--out", default="", help="output folder (default a new one)")
    p.add_argument("--area", default="", help="the 2D flow area (default the first)")
    p.add_argument("--units", default="", choices=["", "us", "si"])
    p.add_argument("--terrain-z", default="", help="terrain elevation unit (default from "
                   "the raster: auto, feet, us_feet, meters)")
    p.add_argument("--t0-hr", type=float, default=0.0, help="arrival counted from this hour")
    p.add_argument("--arrival-depth-ft", type=float, default=1.0)
    p.add_argument("--overwrite", action="store_true", help="replace existing rasters in --out")
    p.set_defaults(func=cmd_hecras_reexport)
    return ap


def _install_sigint():
    """First Ctrl-C asks the model to stop; a second one interrupts at once."""
    CANCEL["flag"] = False
    old = None

    def handler(signum, frame):
        if CANCEL["flag"]:
            raise KeyboardInterrupt
        CANCEL["flag"] = True
        sys.stderr.write("\nCancelling... (press Ctrl-C again to stop at once)\n")
    try:
        old = signal.signal(signal.SIGINT, handler)
    except (ValueError, OSError):        # not the main thread
        return None
    return old


def main(argv=None):
    ap = build_parser()
    args = ap.parse_args(argv)
    old = _install_sigint() if args.command in ("run", "batch", "estimate") else None
    try:
        return int(args.func(args) or 0)
    except CliError as e:
        sys.stderr.write("ndbreach: error: {}\n".format(e))
        return 2
    except brmod.BreachInputError as e:
        sys.stderr.write("ndbreach: error: {}\n".format(e))
        return 2
    except KeyboardInterrupt:
        sys.stderr.write("ndbreach: interrupted\n")
        return 130
    finally:
        if old is not None:
            try:
                signal.signal(signal.SIGINT, old)
            except (ValueError, OSError):
                pass


if __name__ == "__main__":
    sys.exit(main())
