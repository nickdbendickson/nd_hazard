# -*- coding: utf-8 -*-
"""Dialog settings saved in the QGIS project (Project > Save keeps them):
dam folder, rasters, study-area and classification options, scenarios.
Re-opening the project and the plugin restores everything.

v2.9 - settings profiles: the classification options of the dialog saved
as a small JSON file that engineers share (a firm's standard settings, a
review agency's settings) and load into any project:

    {"format": "ND_Dam_Hazard_Helper/settings_profile", "format_version": 1,
     "plugin": ..., "plugin_version": "2.9.0", "name": "...", "note": "...",
     "created": "2026-09-29T14:03:11", "excluded_keys": [...],
     "sha256": "<profile_hash(state)>", "state": {widget name: value}}

`state` is the dict of dialog._current_state() (widget name -> value) with
everything that belongs to ONE dam / ONE computer / ONE QGIS project left
out (shareable_state):
  * the dam's folders, rasters, model imports, scenario table and NID ID
    (DAM_SPECIFIC_KEYS);
  * a layer choice (LAYER_COMBO_KEYS) that is a project layer id - only the
    choices "(auto-detect)", "(none)" and "(download ...)" are shared (they
    mean the same in every project); the population field pickers go with
    the population layer (DEPENDENT_KEYS);
  * as a safety net for widgets added later: any *_combo value that looks
    like a QGIS layer id (or is one of the ids given in `layer_ids`) and any
    text value that looks like an absolute file / folder path.
The same rule is applied again when a profile is loaded, so a hand-edited
file can never set a folder or a layer. The profile hash (sha256 of the
canonical JSON of `state`: keys sorted, 2 == 2.0, floats to 12 significant
digits) identifies the settings whatever the name, the key order or the
float formatting; the run log and the submittal record it.
"""

import datetime
import json
import os
import re
import hashlib
import math

from qgis.PyQt.QtWidgets import QCheckBox, QComboBox, QSpinBox, QDoubleSpinBox, QLineEdit
from qgis.core import QgsProject

from .helpers import log_message

SCOPE = "ND_Dam_Hazard_Helper"
KEY = "dialog_state_v2"


def save_state(state):
    try:
        QgsProject.instance().writeEntry(SCOPE, KEY, json.dumps(state))
        return True
    except Exception as e:
        log_message("Settings not saved in the project: {}".format(e))
        return False


def load_state():
    try:
        v = QgsProject.instance().readEntry(SCOPE, KEY, "")
    except Exception:
        return {}
    if isinstance(v, (tuple, list)):
        v = v[0] if v else ""
    try:
        state = json.loads(v or "{}")
        return state if isinstance(state, dict) else {}
    except (TypeError, ValueError):
        return {}


def widget_value(w):
    if isinstance(w, QCheckBox):
        return bool(w.isChecked())
    if isinstance(w, QComboBox):
        return w.currentData()
    if isinstance(w, (QSpinBox, QDoubleSpinBox)):
        return w.value()
    if isinstance(w, QLineEdit):
        return w.text()
    return None


def set_widget_value(w, v):
    try:
        if isinstance(w, QCheckBox):
            w.setChecked(bool(v))
        elif isinstance(w, QComboBox):
            for i in range(w.count()):
                if w.itemData(i) == v:
                    w.setCurrentIndex(i)
                    return
        elif isinstance(w, QSpinBox):
            w.setValue(int(v))
        elif isinstance(w, QDoubleSpinBox):
            w.setValue(float(v))
        elif isinstance(w, QLineEdit):
            w.setText("" if v is None else str(v))
    except (TypeError, ValueError):
        pass


# ================================================================ profiles ==
PROFILE_FORMAT = "ND_Dam_Hazard_Helper/settings_profile"
PROFILE_FORMAT_VERSION = 1
PROFILE_EXT = ".ndprofile.json"

# keys of dialog._current_state() that describe ONE dam (its folders, rasters,
# model imports, scenario table, NID ID) or a file on one computer: never in a profile
DAM_SPECIFIC_KEYS = (
    "dam_folder_edit", "output_folder_edit",
    "depth_edit", "vel_edit", "arr_edit", "dv_edit",
    "dsw_path_edit", "ras_plan_edit", "ras_dem_edit",
    "scenario_rows", "main_baseline", "main_name_edit",
    "subject_nid_edit",
    "damage_curves_edit", "hcom_edit",
    # v2.9: the data folder is a place on one computer, its picks are files there
    "datafolder_edit", "datafolder_picks_edit",
)
# layer pickers: their value is a project layer id or one of LAYER_CHOICE_TOKENS
LAYER_COMBO_KEYS = (
    "road_combo", "aadt_combo", "rail_combo", "crossing_combo", "outline_combo",
    "dam_layer_combo", "lifeline_combo", "boundary_combo", "parcel_combo",
    "centerline_combo", "pop_source_combo",
)
LAYER_CHOICE_TOKENS = ("__auto__", "__none__", "__download__")
# picked from the layer named by the key: dropped together with it
DEPENDENT_KEYS = {"pop_source_combo": ("pop_field_combo", "pop_day_combo",
                                       "pop_housing_combo"),
                  "parcel_combo": ("parcel_landuse_combo", "parcel_improvement_combo",
                                   "parcel_ref_combo")}
# the keys the dialog saves (dialog._SETTING_WIDGETS + the scenario table), for
# "unknown key" reports; the dialog passes its own list to load_profile
DIALOG_KEYS = (
    "dam_folder_edit", "output_folder_edit", "depth_edit", "vel_edit", "arr_edit", "dv_edit",
    "extent_spin", "keep_dry_check", "nsi_download_check", "addr_download_check",
    "usa_download_check", "model_combo", "judgment_check", "near_spin", "borderline_check",
    "ppd_spin", "min_area_spin", "occ_spin", "speed_spin", "lol_check", "warn_spin",
    "understanding_combo", "severity_combo", "population_combo", "access_check", "access_spin",
    "access_medium_check", "report_check", "pages_spin", "road_combo", "aadt_combo",
    "rail_combo", "crossing_combo", "outline_combo", "dsw_path_edit", "dams_download_check",
    "dam_layer_combo", "subject_nid_edit", "lifelines_download_check", "lifeline_combo",
    "lifeline_medium_check", "boundary_combo", "eap_check", "crossings_download_check",
    "advanced_check", "spreadsheets_check", "camps_download_check", "parcel_combo",
    "parcel_acres_spin", "compare_check", "eap_list_check", "eap_mapbook_check",
    "mapbook_scale_combo", "eap_travel_check", "centerline_combo", "flowlines_download_check",
    "station_spin", "ras_plan_edit", "ras_dem_edit", "ras_breach_spin", "ras_arrival_spin",
    "ras_cell_spin", "data_area_combo", "flood_buffer_spin", "data_save_check",
    "zone_rasters_check", "sampled_cells_check", "reuse_check", "main_name_edit",
    "damage_curves_edit", "hcom_edit", "review_aerial_check", "sample_area_combo",
    "sens_check", "sens_factors_edit", "sens_split_check", "term_check", "term_rise_spin",
    "term_depth_spin", "term_confirm_spin", "pop_source_combo", "pop_field_combo",
    "pop_day_combo", "pop_housing_combo", "pop_period_combo", "pop_units_combo",
    "pop_method_combo", "pop_mindepth_spin", "evac_check", "evac_prep_spin",
    "evac_passable_spin", "evac_include_combo", "evac_safe_spin", "cache_check",
    "cache_age_spin", "cache_refresh_check", "quick_check", "watch_workers_spin",
    "scenario_rows", "main_baseline",
    # v2.9
    "datafolder_edit", "datafolder_use_check", "datafolder_picks_edit",
    "mobile_shape_check", "mobile_max_width_spin", "mobile_min_ratio_spin",
    "mobile_min_length_spin", "mobile_max_length_spin", "mobile_lot_check",
    "trailer_parks_check", "fast_sampling_check", "input_overviews_check",
    "zone_overviews_check", "report_imagery_check", "conseq_sens_check", "conseq_warning_edit",
    "conseq_ppd_edit", "conseq_pop_check", "conseq_severity_check", "evac_routes_check",
    "temporal_check", "temporal_start_edit", "temporal_step_spin", "web_export_check",
    "parcel_hints_check", "parcel_vacant_kw_edit", "parcel_nonres_kw_edit",
    "parcel_res_kw_edit", "parcel_empty_zero_check", "parcel_landuse_combo",
    "parcel_improvement_combo", "parcel_ref_combo",
    # v2.10
    "suite_gate_check", "suite_gate_stop_check",
    # v2.20
    "dv_spreadsheet_check", "dv_template_edit",
)

# QGIS 3 layer id: <name>_<uuid with "_">; QGIS 2 / early 3: <name><yyyyMMddhhmmsszzz>
_LAYER_ID_RE = re.compile(r"(_[0-9a-f]{8}_[0-9a-f]{4}_[0-9a-f]{4}_[0-9a-f]{4}_[0-9a-f]{12}"
                          r"|\d{17})$", re.IGNORECASE)
_PATH_RE = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\|/|~[\\/]|file:)")


class ProfileError(ValueError):
    """A file that is not a usable settings profile (the message says why)."""


def looks_like_layer_id(value, layer_ids=None):
    if not isinstance(value, str) or not value or value in LAYER_CHOICE_TOKENS:
        return False
    if layer_ids and value in layer_ids:
        return True
    return bool(_LAYER_ID_RE.search(value))


def looks_like_path(value):
    return isinstance(value, str) and bool(_PATH_RE.match(value.strip()))


def _drop_reason(key, value, layer_ids=None):
    """Why `key` = `value` must not be in a shared profile ("" = it may)."""
    if key in DAM_SPECIFIC_KEYS:
        return "dam-specific"
    if key in LAYER_COMBO_KEYS:
        return "" if value in LAYER_CHOICE_TOKENS or value in (None, "") else "project layer"
    if key.endswith("_combo") and looks_like_layer_id(value, layer_ids):
        return "project layer"
    if looks_like_path(value):
        return "file or folder path"
    if isinstance(value, (list, tuple)) and any(looks_like_path(v) for v in value):
        return "file or folder path"
    return ""


def shareable_state(state, layer_ids=None):
    """(profile state, {dropped key: reason}) of a dialog state: what may go
    into a profile shared between engineers (see the module docstring)."""
    out, dropped = {}, {}
    for k in sorted(state or {}):
        why = _drop_reason(k, state[k], layer_ids)
        if why:
            dropped[k] = why
        else:
            out[k] = state[k]
    for parent, kids in DEPENDENT_KEYS.items():
        if parent in dropped:
            for k in kids:
                if k in out:
                    del out[k]
                    dropped[k] = "field of the {} layer".format(parent)
    return out, dropped


def _canon(v):
    """JSON-ready value with one spelling per value: 2 == 2.0, floats to 12
    significant digits, tuples as lists, dict keys as text."""
    if isinstance(v, bool) or v is None or isinstance(v, str):
        return v
    if isinstance(v, int):
        return int(v)
    if isinstance(v, float):
        if not math.isfinite(v):
            return repr(v)
        r = float(format(v, ".12g"))
        if r == int(r) and abs(r) < 2 ** 53:
            return int(r)
        return r
    if isinstance(v, dict):
        return {str(k): _canon(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_canon(x) for x in v]
    return str(v)


def canonical_json(state):
    return json.dumps(_canon(state or {}), sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True)


def profile_hash(state):
    """sha256 (hex) of the canonical JSON of a profile state: the same for the
    same settings whatever the key order or the float formatting."""
    return hashlib.sha256(canonical_json(state).encode("utf-8")).hexdigest()


def _now_iso():
    return datetime.datetime.now().replace(microsecond=0).isoformat()


def _plugin_version():
    try:
        from .constants import PLUGIN_NAME, PLUGIN_VERSION
        return PLUGIN_NAME, PLUGIN_VERSION
    except Exception:
        return "ND Dam Hazard Classification Helper", ""


def save_profile(path, state, name, note="", layer_ids=None):
    """Write a settings profile. `state`: dialog._current_state(); what is
    dam- or project-specific is left out (shareable_state; `layer_ids`: the
    project's layer ids, QgsProject.instance().mapLayers().keys()). Returns
    the profile's meta dict (name, note, created, plugin_version, sha256, path,
    excluded_keys, keys). Raises ProfileError for an empty name, OSError when
    the file cannot be written."""
    name = str(name or "").strip()
    if not name:
        raise ProfileError("give the profile a name")
    shared, dropped = shareable_state(state, layer_ids)
    plugin, version = _plugin_version()
    data = {"format": PROFILE_FORMAT, "format_version": PROFILE_FORMAT_VERSION,
            "plugin": plugin, "plugin_version": version, "name": name,
            "note": str(note or ""), "created": _now_iso(),
            "excluded_keys": sorted(dropped), "sha256": profile_hash(shared),
            "state": {k: shared[k] for k in sorted(shared)}}
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, path)
    return _meta(data, path, data["sha256"], [], {}, [])


def _meta(data, path, sha, unknown, dropped, warnings):
    return {"name": data.get("name") or "", "note": data.get("note") or "",
            "created": data.get("created") or "", "plugin_version":
            data.get("plugin_version") or "", "format_version": data.get("format_version"),
            "sha256": sha, "sha256_saved": data.get("sha256") or "",
            "hash_ok": (data.get("sha256") or "") == sha, "path": os.path.abspath(path),
            "excluded_keys": list(data.get("excluded_keys") or []),
            "keys": len(data.get("state") or {}), "unknown_keys": list(unknown),
            "dropped_keys": dict(dropped), "warnings": list(warnings)}


def _version_tuple(v):
    out = []
    for part in str(v or "").split("."):
        m = re.match(r"\d+", part)
        out.append(int(m.group(0)) if m else 0)
    return tuple(out)


def load_profile(path, known_keys=None, layer_ids=None):
    """(state, meta) of a settings profile file. Refused with ProfileError
    (a clear message) when the file is missing, not JSON, not a profile of
    this plugin, of a newer profile format, or has no settings object.
    Accepted with warnings in meta["warnings"]:
      * keys this dialog does not know (meta["unknown_keys"]; they are kept
        in `state` - the dialog ignores what it has no widget for);
      * dam-specific keys, layer ids or paths found in the file (removed from
        `state`, listed in meta["dropped_keys"]);
      * a hash that does not match the settings (edited by hand after it was
        saved: meta["hash_ok"] False; meta["sha256"] is the hash of what is
        loaded);
      * a profile saved by a newer plugin version.
    `known_keys`: the dialog's widget names (default DIALOG_KEYS)."""
    if not path or not os.path.isfile(path):
        raise ProfileError("Settings profile not found: {}".format(path))
    try:
        with open(path, encoding="utf-8-sig") as fh:
            data = json.load(fh)
    except UnicodeDecodeError:
        raise ProfileError("{} is not a text (JSON) file - not a settings profile".format(
            os.path.basename(path)))
    except ValueError as e:
        raise ProfileError("{} is not valid JSON ({}) - not a settings profile".format(
            os.path.basename(path), e))
    except OSError as e:
        raise ProfileError("{} could not be read: {}".format(os.path.basename(path), e))
    if not isinstance(data, dict) or data.get("format") != PROFILE_FORMAT:
        what = data.get("format") if isinstance(data, dict) else None
        raise ProfileError("{} is not a settings profile of this plugin (format {}; expected "
                           "'{}'). Profiles are made with 'Save profile...'.".format(
                               os.path.basename(path), repr(what) if what else "missing",
                               PROFILE_FORMAT))
    fv = data.get("format_version")
    if not isinstance(fv, int) or isinstance(fv, bool) or fv < 1:
        raise ProfileError("{}: the profile format version is missing or not a number".format(
            os.path.basename(path)))
    if fv > PROFILE_FORMAT_VERSION:
        raise ProfileError("{} was saved in profile format {} by plugin version {} - this "
                           "version reads format {} only; update the plugin".format(
                               os.path.basename(path), fv, data.get("plugin_version") or "?",
                               PROFILE_FORMAT_VERSION))
    raw = data.get("state")
    if not isinstance(raw, dict):
        raise ProfileError("{}: the profile holds no settings object ('state')".format(
            os.path.basename(path)))
    warnings = []
    saved_sha = str(data.get("sha256") or "")
    raw_sha = profile_hash(raw)
    if not saved_sha:
        warnings.append("the profile has no hash - it was not made by 'Save profile...'")
    elif saved_sha != raw_sha:
        warnings.append("the settings in the file do not match its hash - the file was edited "
                        "after it was saved")
    state, dropped = shareable_state(raw, layer_ids)
    if dropped:
        warnings.append("left out (they belong to one dam or project): {}".format(
            ", ".join(sorted(dropped))))
    known = set(known_keys) if known_keys else set(DIALOG_KEYS)
    unknown = sorted(k for k in state if k not in known)
    if unknown:
        warnings.append("settings this version does not know (ignored by the dialog): "
                        "{}".format(", ".join(unknown)))
    if not state:
        warnings.append("the profile holds no settings")
    _plugin, mine = _plugin_version()
    if mine and data.get("plugin_version") and \
            _version_tuple(data["plugin_version"]) > _version_tuple(mine):
        warnings.append("saved by a newer plugin version ({}; this is {})".format(
            data["plugin_version"], mine))
    sha = profile_hash(state)
    meta = _meta(data, path, sha, unknown, dropped, warnings)
    meta["hash_ok"] = bool(saved_sha) and saved_sha == raw_sha
    return state, meta


def _as_state(x):
    if isinstance(x, str):
        return load_profile(x)[0]
    if isinstance(x, tuple) and len(x) == 2 and isinstance(x[0], dict):
        return x[0]
    return dict(x or {})


def diff_profiles(a, b):
    """Sorted keys whose values differ between two profile states (a key in
    one only counts). a / b: a state dict, a (state, meta) pair or a profile
    path. Values are compared canonically (2 == 2.0)."""
    sa, sb = _as_state(a), _as_state(b)
    return sorted(k for k in set(sa) | set(sb)
                  if k not in sa or k not in sb or _canon(sa[k]) != _canon(sb[k]))


def diff_lines(a, b):
    """'key: a -> b' lines for diff_profiles (for a message box / the log)."""
    sa, sb = _as_state(a), _as_state(b)
    miss = "(not set)"
    return ["{}: {} -> {}".format(k, sa.get(k, miss) if k in sa else miss,
                                  sb.get(k, miss) if k in sb else miss)
            for k in diff_profiles(sa, sb)]


def profile_status(current_state, profile_state, meta, layer_ids=None):
    """What the run record keeps about the profile a run was made with
    (cfg["settings_profile"]): {"name", "sha256", "modified", "changed_keys"}.
    `modified`: the dialog's settings no longer equal the profile (the engineer
    changed something after loading it)."""
    shared, _d = shareable_state(current_state, layer_ids)
    changed = [k for k in diff_profiles(profile_state, shared) if k in profile_state]
    return {"name": (meta or {}).get("name") or "", "sha256": (meta or {}).get("sha256") or
            profile_hash(profile_state), "modified": bool(changed), "changed_keys": changed,
            "path": (meta or {}).get("path") or ""}


def profile_log_line(meta):
    """One line for the run log / the summary."""
    if not meta:
        return "Settings profile: none"
    return "Settings profile: '{}' (sha256 {}){}".format(
        meta.get("name") or "?", (meta.get("sha256") or "?")[:16],
        " - MODIFIED after loading: {}".format(", ".join(meta.get("changed_keys") or []))
        if meta.get("modified") else "")
