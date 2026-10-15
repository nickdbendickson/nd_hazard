# -*- coding: utf-8 -*-
"""Find the hydraulic rasters in a dam project folder.

HEC-RAS (RAS Mapper) exports are named "Depth (Max).vrt",
"Velocity (Max).Terrain.tif", "Arrival Time (...).tif", "D * V (Max).tif";
DSS-WISE Lite packages "MaxDepth_in_Ft.tif" and so on. The same file-name
rules as the DSS-WISE import (dsswise.detect_role) decide the role. A file
or folder whose name says "no breach" / "baseline" / "non-failure" is taken
as the no-breach baseline. Rasters of one output time ("Depth (<time>)")
are never taken for a maximum.

The plugin's own outputs folder is never searched. When several rasters fit
a role, the one named "(Max)" wins, then a .vrt (it covers every tile), then
the one nearest the dam folder; the others are reported so the user can
pick another one on the Setup tab.

2.17.0: the DSS-WISE Lite and HEC-RAS plan imports this plugin writes in
<dam>/outputs/DSSWISE_<name>/ and <dam>/outputs/HECRAS_<plan>/ (Depth_ft.tif,
Velocity_ftps.tif, Arrival_hr.tif, DxV_ft2s.tif: already in the plugin's
units) are used when the dam folder itself has no depth raster - all
rasters from ONE import (the newest), never mixed with rasters found
elsewhere. Nothing else in outputs/ is looked at (the plugin's own results,
the HEC-RAS plan cache). An import whose name says "no breach" / "baseline"
is the no-breach baseline.
"""

import os
import re

from .constants import OUTPUT_FOLDER_NAME
from .dsswise import (detect_role, R_DEPTH, R_VEL, R_DV, R_ARR, R_DEM, R_WSE, R_PEAK,
                      RASTER_EXT, OUT_NAMES)

# RAS Mapper time-step exports "Depth (01JAN2020 12:00:00).tif": recognised
# only to be left out (the maxima are wanted)
SERIES_RE = re.compile(r"^(?P<kind>depth|velocity|vel|speed)(?![a-z])[\s_\-]*(?P<rest>.*)$",
                       re.I)
PAREN_RE = re.compile(r"^\((?P<label>[^)]*)\)")
# v2.9.1: rasters that are the difference of two floods, not a flood
_DERIVED_RE = re.compile(r"(?<![a-z])(incremental|increment|difference|diff)(?![a-z])"
                         r"|(?<![a-z])rise(?![a-z])", re.I)
SKIP_LABELS = ("max", "min", "maximum", "minimum", "max value", "min value")

MAX_DEPTH_LEVELS = 5
_SKIP_DIRS = {OUTPUT_FOLDER_NAME.lower(), "_downloads", "_reprojected", ".git", "__pycache__",
              "backup", "backups", "old", "archive",
              # v2.10: ND Dam Breach Runner ensemble runs (<dam>/Probabilistic/Runs/run_NNN)
              # are classified by the class-probability tool, never taken for the dam's rasters
              "probabilistic"}
# Match only at the START of a path component so that compound names like
# "Breach_No_Breach_Compare" do not produce a false positive.
_BASELINE_PART_RE = re.compile(
    r"^(?:no[\s_\-.]*breach|non[\s_\-.]*breach|non[\s_\-.]*fail|"
    r"no[\s_\-.]*fail|without[\s_\-.]*(breach|fail)|baseline|"
    r"existing[\s_\-.]*cond)",
    re.I,
)
_ROLES = {R_DEPTH: "depth", R_VEL: "vel", R_ARR: "arr", R_DV: "dv"}
# 2.17.0: the import folders in <dam>/outputs (dsswise_tab._dsw_out, rasimport_tab._ras_out)
# and the files they hold (dsswise.OUT_NAMES; rasimport.OUT_NAMES uses the same names)
IMPORT_PREFIXES = ("dsswise_", "hecras_")
IMPORT_SKIP = ("hecras_plan_cache",)            # rasimport.CACHE_DIR_NAME, not an import
_IMPORT_FILES = {OUT_NAMES[R_DEPTH].lower(): "depth", OUT_NAMES[R_VEL].lower(): "vel",
                 OUT_NAMES[R_ARR].lower(): "arr", OUT_NAMES[R_DV].lower(): "dv"}


def _is_baseline(rel_path):
    """True if any component of rel_path starts with a baseline indicator.

    Splits on path separators so that a compound name like
    "Breach_No_Breach_Compare" does not accidentally match.
    """
    return any(_BASELINE_PART_RE.match(p) for p in re.split(r"[\\/]", rel_path))


def _is_time_step(name):
    """"Depth (01JAN2020 12:00:00)" / "Velocity_0100": one output time, not
    the maximum."""
    base = os.path.splitext(name)[0]
    m = SERIES_RE.match(base.strip())
    if not m:
        return False
    rest = m.group("rest").strip()
    pm = PAREN_RE.match(rest)
    label = (pm.group("label") if pm else rest).strip(" _-.").lower()
    if re.match(r"^[hdv][\s_\-]?max(?![a-z])", label):        # v2.9: "Depth_Hmax", "Velocity_Vmax"
        return False
    if re.search(r"(?<![a-z])[hdv]?max(?![a-z])", label):     # "Depth_x_Velocity_Hmax_Vmax"
        return False
    if re.match(r"^(x|times|by)[\s_\-]*(vel|velocity|speed)(?![a-z])", label):   # D x V product
        return False
    return bool(label) and label not in SKIP_LABELS and not label.startswith("max")


def _rank(path, root):
    name = os.path.basename(path).lower()
    depth = os.path.relpath(path, root).count(os.sep)
    return (0 if "max" in name else 1, 0 if name.endswith(".vrt") else 1, depth, len(name))


def find_imports(folder):
    """2.17.0: [(time, import folder, {"depth", "vel", "arr", "dv": path}, baseline)] of
    the DSS-WISE Lite / HEC-RAS plan imports in <dam>/outputs, newest first; only imports
    with a depth raster."""
    out = []
    outputs = os.path.join(folder or "", OUTPUT_FOLDER_NAME)
    try:
        entries = sorted(os.listdir(outputs))
    except OSError:
        return out
    for d in entries:
        low = d.lower()
        sub = os.path.join(outputs, d)
        if not low.startswith(IMPORT_PREFIXES) or low in IMPORT_SKIP or not os.path.isdir(sub):
            continue
        try:
            files = os.listdir(sub)
        except OSError:
            continue
        roles = {}
        for f in files:
            key = _IMPORT_FILES.get(f.lower())
            if key:
                roles[key] = os.path.join(sub, f)
        if not roles.get("depth"):
            continue
        try:
            when = max(os.path.getmtime(p) for p in roles.values())
        except OSError:
            when = 0.0
        slug = d.split("_", 1)[1] if "_" in d else d
        out.append((when, sub, roles, _is_baseline(slug)))
    out.sort(key=lambda t: (-t[0], t[1]))
    return out


def _use_imports(out, root):
    """2.17.0: no depth raster in the dam folder itself - take every raster from the newest
    import in outputs/ (and the newest no-breach import as the baseline)."""
    imports = find_imports(root)
    if not imports:
        return
    breach = [t for t in imports if not t[3]]
    base = [t for t in imports if t[3]]
    if not out["depth"] and breach:
        _when, sub, roles, _b = breach[0]
        for key in ("depth", "vel", "arr", "dv"):
            out[key] = roles.get(key)
        out["notes"].append("taken from the import {} (the dam folder has no depth raster of "
                            "its own){}".format(
                                os.path.relpath(sub, root),
                                "; {} older import(s) there".format(len(breach) - 1)
                                if len(breach) > 1 else ""))
        if not out["base_depth"] and base:
            _when, sub, roles, _b = base[0]
            out["base_depth"], out["base_vel"], out["base_arr"] = (
                roles.get("depth"), roles.get("vel"), roles.get("arr"))
    elif out["depth"] and breach:
        out["notes"].append("{} DSS-WISE / HEC-RAS import(s) in {} not used (the dam folder has "
                            "its own rasters), e.g. {}".format(
                                len(breach), OUTPUT_FOLDER_NAME,
                                os.path.relpath(breach[0][1], root)))


def find_rasters(folder):
    """{"depth", "vel", "arr", "dv", "base_depth", "base_vel", "base_arr":
    path or None, "notes": [text]} for the dam folder."""
    out = {k: None for k in ("depth", "vel", "arr", "dv", "base_depth", "base_vel",
                             "base_arr")}
    out["notes"] = []
    if not folder or not os.path.isdir(folder):
        return out
    cands = {}
    unknown = []
    derived = []
    root = os.path.abspath(folder)
    for dirpath, dirs, files in os.walk(root):
        rel = os.path.relpath(dirpath, root)
        level = 0 if rel == "." else rel.count(os.sep) + 1
        dirs[:] = [d for d in dirs if d.lower() not in _SKIP_DIRS and not d.startswith(".")
                   and not d.upper().startswith(("ACER_", "EAP_"))
                   and level < MAX_DEPTH_LEVELS]
        rasters = [f for f in files if os.path.splitext(f)[1].lower() in RASTER_EXT
                   and not f.lower().endswith((".aux.xml", ".ovr"))]
        if not rasters:
            continue
        for f in rasters:
            if _is_time_step(f):
                continue
            if _DERIVED_RE.search(f):
                # v2.9.1: a difference of two floods (ND Dam Breach Runner "Incremental
                # Depth (Max)", "... WSE Rise", "Difference ...") is never a flood depth
                derived.append(os.path.relpath(os.path.join(dirpath, f), root))
                continue
            # RAS Mapper writes tiles "Depth (Max).Terrain.<name>.tif" next to
            # "Depth (Max).vrt": the tiles are parts of the VRT
            if f.lower().endswith((".tif", ".tiff")) and ".terrain." in f.lower():
                stem = f.split(".")[0]
                if any(x.lower() == (stem + ".vrt").lower() for x in files):
                    continue
            if re.match(r"^d\s*[*x]\s*v(?![a-z])", f, re.I):     # RAS Mapper "D * V (Max)"
                role = "dv"
            else:
                role = _ROLES.get(detect_role(f))
            if role is None:
                # v2.9: say which rasters were passed over (not DEM / WSE / duration ...)
                if detect_role(f) not in (R_DEM, R_WSE, R_PEAK):
                    unknown.append(os.path.relpath(os.path.join(dirpath, f), root))
                continue
            path = os.path.join(dirpath, f)
            base = _is_baseline(os.path.join(rel, f))
            cands.setdefault(("base_" if base else "") + role, []).append(path)
    if unknown and not (out["depth"] or cands.get("depth")):
        out["notes"].append(
            "{} raster file(s) not recognised as depth / velocity / arrival (the file name "
            "should say Depth or Hmax / Dmax, Velocity or Vmax, Arrival): {}".format(
                len(unknown), ", ".join(sorted(unknown)[:6]) + (" ..." if len(unknown) > 6
                                                                else "")))
    if derived:
        out["notes"].append("{} raster(s) of the DIFFERENCE between two floods left out "
                            "(incremental / rise / difference): {}".format(
                                len(derived), ", ".join(sorted(derived)[:4]) +
                                (" ..." if len(derived) > 4 else "")))
    for key, paths in cands.items():
        if key == "base_dv":
            continue
        paths.sort(key=lambda p: _rank(p, root))
        out[key] = paths[0]
        if len(paths) > 1:
            out["notes"].append("{}: {} other candidate(s), e.g. {}".format(
                key, len(paths) - 1, os.path.relpath(paths[1], root)))
    _use_imports(out, root)                       # 2.17.0: outputs/DSSWISE_* / HECRAS_*
    return out


def describe(found, root):
    parts = []
    for key, label in (("depth", "depth"), ("vel", "velocity"), ("arr", "arrival"),
                       ("dv", "D x V"), ("base_depth", "no-breach depth")):
        if found.get(key):
            parts.append("{} = {}".format(label, os.path.relpath(found[key], root)))
    return "; ".join(parts) if parts else NO_RASTERS


# 2.30.0: before a breach model has been run there is nothing to recognise, and "no rasters
# recognised" reads like a failure of the search. These two say what to do instead.
NO_RASTERS = ("no depth / velocity / arrival raster in the dam folder yet - run the breach "
              "model first (step 3 of the suite: ND Dam Breach Runner)")
MODEL_FOLDERS = ("_breach_model", "_screening_model")


def no_rasters_text(root=""):
    """Why there are no rasters and what to do, in one sentence. A dam folder that already
    holds a model folder gets the more exact version (the run was started but has no final
    rasters), because "run the breach model first" would be wrong there."""
    started = []
    for name in MODEL_FOLDERS:
        try:
            if root and os.path.isdir(os.path.join(root, name)):
                started.append(name)
        except Exception:                                        # noqa - an unreadable folder
            continue
    if started:
        return ("no final depth / velocity / arrival raster in the dam folder, although {} is "
                "there - finish or re-run the breach model (step 3 of the suite: ND Dam Breach "
                "Runner), then press 'Find rasters'".format(" and ".join(started)))
    return NO_RASTERS


def found_text(found, root=""):
    """The log sentence after a search of the dam folder."""
    text = describe(found, root)
    if text == NO_RASTERS:
        return no_rasters_text(root)
    return "Rasters found in the dam folder: {}".format(text)
