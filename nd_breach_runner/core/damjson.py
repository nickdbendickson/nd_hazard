"""v3.2.0: the shared dam record `<Dams folder>/<dam id>/dam.json` (damsuite.dam_record) for
the breach runner.

Before a run (rundam, the batch and the wizard): a dam value left empty - height, storage,
surface area, top of dam elevation, dam point, name, NID ID - is taken from the record and
logged as "Dam height (ft) from dam.json (set by <tool> <version> on <when>): 25". A top of
dam elevation whose datum the record gives as local is not used as an elevation (the spec's
rule); the run then takes it from the DEM as before.

After a run: the values the run used are written with the tool name "ndbreach" and this
plugin's version (metadata.txt). Another tool's value is NEVER overwritten: a different
value is refused by dam_record, kept in the record's "conflicts" and listed as a warning in
the run report with both values. A "modelled" status is added with the result
    {"final_rasters": [file names in the dam folder], "baseline_rasters": [...],
     "manifest": "_breach_model/run_manifest.json", "run_report": "_breach_model/...json",
     "extent_ft": farthest flooded cell from the dam point (straight line, ft),
     "domain_mi": [width, height], "scenario", "final_model", "breach_occurs"}
(no "class_preview": the run itself does not classify; the ND 5.2 screening classification
tool adds its own "classified" status with {"preview": True, "tool": "nd_hazard.py"} - a
preview, never the authority).
top_of_dam_datum: "DEM" when the run took the top of dam from the DEM or from the tool's
parameter (whose datum is the DEM's by definition); nothing is written when the value came
from the record itself.

Pure Python + the vendored damsuite; no QGIS.
"""
import configparser
import math
import os

from ..damsuite import dam_record as dr

TOOL = "ndbreach"
LABEL = "ND Dam Breach Runner"
# (tool parameter, dam.json field, label for the log)
PARAM_FIELDS = (("HEIGHT_FT", "height_ft", "Dam height (ft)"),
                ("STORAGE_ACFT", "storage_acft", "Storage at the top of dam (ac-ft)"),
                ("AREA_AC", "surface_area_ac", "Surface area at the top of dam (ac)"),
                ("TOP_FT", "top_of_dam_ft", "Top of dam elevation (ft)"),
                ("DAM_NAME", "name", "Dam name"),
                ("NID_ID", "nid_id", "NID ID"))
LOCAL_DATUMS = ("local",)
MANIFEST_REL = "_breach_model/run_manifest.json"
REPORT_REL = "_breach_model/run_report.json"


def folder_name(dam_id):
    """The dam's folder name under the Dams folder (the same rule as algorithms._safe_name)."""
    import re
    s = re.sub(r"[^\w\- ]+", "_", str(dam_id)).strip()
    return s or "dam"


def plugin_version():
    """version= of the plugin's metadata.txt (the workflow's VERSION when unreadable)."""
    md = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "metadata.txt")
    try:
        cp = configparser.ConfigParser(interpolation=None, strict=False)
        with open(md, encoding="utf-8-sig") as f:
            cp.read_file(f)
        v = cp.get("general", "version", fallback="").strip()
        if v:
            return v
    except (OSError, configparser.Error):
        pass
    from .workflow import VERSION
    return VERSION


def load(folder):
    """(DamRecord or None, problem text or ""). A broken / foreign dam.json is reported,
    never replaced."""
    if not folder:
        return None, ""
    try:
        return dr.load(folder), ""
    except ValueError as e:
        return None, "dam.json not read: {}".format(e)
    except OSError as e:
        return None, "dam.json not read: {}".format(e)


def source_text(rec, field):
    """'set by ndhazard 2.10.0 on 2026-09-30T14:05:00' (what the record says of a field)."""
    s = (rec.sources.get(field) if rec is not None else None) or {}
    tool = s.get("tool") or "an unrecorded tool"
    ver = s.get("version") or ""
    when = s.get("when") or "an unrecorded date"
    return "set by {}{} on {}".format(tool, " " + ver if ver else "", when)


def local_datum(rec):
    d = str((rec.get("top_of_dam_datum") if rec is not None else "") or "").strip().lower()
    return d in LOCAL_DATUMS


def fill(values, rec):
    """Fill empty values ({tool parameter: value or None}) from the record.

    Returns (values, notes, used) - `used` = {param: dam.json field} of what came from the
    record, notes = log lines."""
    values = dict(values or {})
    notes, used = [], {}
    if rec is None:
        return values, notes, used
    for param, field, label in PARAM_FIELDS:
        if param not in values:
            continue
        if values.get(param) not in (None, ""):
            continue
        v = rec.get(field)
        if v in (None, ""):
            continue
        if field == "top_of_dam_ft" and local_datum(rec):
            notes.append("{} in dam.json ({}) is on a local datum ({}); not used as an "
                         "elevation.".format(label, v, rec.get("top_of_dam_datum")))
            continue
        if field in ("height_ft", "storage_acft") and not (isinstance(v, (int, float))
                                                           and v > 0):
            notes.append("{} in dam.json ({!r}) is not a positive number; not used.".format(
                label, v))
            continue
        values[param] = v
        used[param] = field
        extra = ""
        if field == "top_of_dam_ft" and rec.get("top_of_dam_datum"):
            extra = " (datum {})".format(rec.get("top_of_dam_datum"))
        notes.append("{} from dam.json ({}): {}{}".format(label, source_text(rec, field), v,
                                                          extra))
    return values, notes, used


def point_from_record(rec):
    """The dam point of the record: ("xy", x, y, crs) when x, y and crs are there, else
    ("lonlat", lon, lat, "EPSG:4326"), else None. Second item: the log line."""
    if rec is None:
        return None, ""
    x, y, crs = rec.get("x"), rec.get("y"), rec.get("crs")
    if isinstance(x, (int, float)) and isinstance(y, (int, float)) and crs:
        return ("xy", float(x), float(y), str(crs)), \
            "Dam point from dam.json ({}): {:.2f}, {:.2f} {}".format(
                source_text(rec, "x"), x, y, crs)
    lon, lat = rec.get("lon"), rec.get("lat")
    if isinstance(lon, (int, float)) and isinstance(lat, (int, float)) and \
            -180.0 <= lon <= 180.0 and -90.0 <= lat <= 90.0:
        return ("lonlat", float(lon), float(lat), "EPSG:4326"), \
            "Dam point from dam.json ({}): lon {:.6f}, lat {:.6f}".format(
                source_text(rec, "lon"), lon, lat)
    return None, ""


# ------------------------------------------------------------------------------ after a run
def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def values_used(dam, st=None, top_source="parameter", point=None, nid_id=None):
    """{dam.json field: value} of what a run used. top_source: "parameter", "record" or
    "dem". point: {"lon", "lat", "x", "y", "crs"} (any may be missing)."""
    v = {"dam_id": getattr(dam, "dam_id", None), "name": getattr(dam, "name", None) or None,
         "height_ft": _num(getattr(dam, "dam_height_ft", None)),
         "storage_acft": _num(getattr(dam, "storage_top_acft", None)),
         "surface_area_ac": _num(getattr(dam, "surface_area_top_ac", None)),
         "dam_length_ft": _num(getattr(dam, "crest_length_ft", None)),
         "nid_id": nid_id or None}
    if top_source != "record":
        v["top_of_dam_ft"] = _num(getattr(dam, "top_of_dam_ft", None))
        if v["top_of_dam_ft"] is not None:
            v["top_of_dam_datum"] = "DEM"
    fl = getattr(st, "flood", None) if st is not None else None
    if fl is not None and _num(getattr(fl, "normal_pool_ft", None)) is not None:
        v["normal_pool_ft"] = _num(fl.normal_pool_ft)
    for k in ("lon", "lat", "x", "y", "crs"):
        if point and point.get(k) not in (None, ""):
            v[k] = point[k] if k == "crs" else _num(point[k])
    return {k: x for k, x in v.items() if x not in (None, "")}


def record_run(folder, values, result, notes=None, dem_top_note="", version=None):
    """Write the values a run used and a "modelled" status into <folder>/dam.json.

    values: {field: value} (values_used); result: the status result dict. Returns a dict
    for the run report: {"path", "written", "kept_from_record", "conflicts" (list of
    {"field", "dam_json", "this_run", "dam_json_source"}), "warnings" (sentences), "read"
    (the notes from before the run), "error"}."""
    version = version or plugin_version()
    info = {"path": os.path.join(folder, dr.FILE_NAME), "written": [], "kept_from_record": [],
            "conflicts": [], "warnings": [], "read": list(notes or []), "error": ""}
    rec, problem = load(folder)
    if problem:
        info["error"] = problem
        info["warnings"].append(problem + " The run did not update dam.json; fix or move "
                                          "the file.")
        return info
    if rec is None:
        rec = dr.DamRecord(folder)
    values = {k: v for k, v in (values or {}).items() if k in dr.FIELDS}
    before = {k: rec.get(k) for k in values}
    top_keys = ("top_of_dam_ft", "top_of_dam_datum")
    main = {k: v for k, v in values.items() if not (dem_top_note and k in top_keys)}
    refused = list(rec.set_many(main, TOOL, version))
    if dem_top_note:
        refused += rec.set_many({k: v for k, v in values.items() if k in top_keys}, TOOL,
                                version, note=dem_top_note)
    for k, v in values.items():
        if k in refused:
            continue
        if before.get(k) is None:
            info["written"].append(k)
        elif _same(before.get(k), v):
            info["kept_from_record"].append(k)
        else:
            info.setdefault("updated", []).append(k)     # this tool's own earlier value
    for k in refused:
        src = source_text(rec, k)
        c = {"field": k, "dam_json": rec.get(k), "this_run": values[k],
             "dam_json_source": src}
        info["conflicts"].append(c)
        info["warnings"].append(
            "dam.json keeps {} = {} ({}); this run used {}. The other tool's value is not "
            "overwritten: check which is right and correct dam.json or the run's input."
            .format(k, _fmt(rec.get(k)), src, _fmt(values[k])))
    rec.add_status("modelled", TOOL, version, result=result)
    try:
        rec.save()
    except OSError as e:
        info["error"] = "dam.json could not be written: {}".format(e)
        info["warnings"].append(info["error"])
    return info


def _same(a, b):
    na, nb = _num(a), _num(b)
    if na is not None and nb is not None and not isinstance(a, str) and \
            not isinstance(b, str):
        return math.isclose(na, nb, rel_tol=dr.REL_TOL, abs_tol=0.0)
    return str(a).strip().casefold() == str(b).strip().casefold()


def _fmt(v):
    if isinstance(v, float):
        return "{:g}".format(v)
    return repr(v) if isinstance(v, str) else str(v)


def extent_ft(grid, depth, dam_xy, wet_m=0.003):
    """Straight-line distance from the dam point to the farthest flooded cell (ft)."""
    import numpy as np
    wet = np.isfinite(depth) & (depth > wet_m)
    if not wet.any():
        return 0.0
    r, c = np.nonzero(wet)
    xs = grid.x0 + (c + 0.5) * grid.dx
    ys = grid.y0 - (r + 0.5) * grid.dx
    return round(float(np.hypot(xs - dam_xy[0], ys - dam_xy[1]).max()) / 0.3048, 0)


PREVIEW_RESULT = {"preview": True, "tool": "nd_hazard.py"}


def classified_preview(folder, version=None):
    """The ND 5.2 screening classification ran on this dam folder: add a "classified"
    status to its existing dam.json, marked as a preview (result exactly
    {"preview": True, "tool": "nd_hazard.py"}) - never the authority, which is ND Dam Hazard.
    A folder without dam.json is left alone (no record is created for a preview).
    Returns (state, text): state "added", "skipped" or "error"."""
    rec, problem = load(folder)
    if problem:
        return "error", problem
    if rec is None or not os.path.exists(os.path.join(folder, dr.FILE_NAME)):
        return "skipped", "no dam.json in the dam folder: the preview class is not recorded"
    rec.add_status("classified", TOOL, version or plugin_version(),
                   result=dict(PREVIEW_RESULT))
    try:
        rec.save()
    except OSError as e:
        return "error", "dam.json could not be written: {}".format(e)
    return "added", "dam.json: status 'classified' added as a PREVIEW (tool nd_hazard.py); " \
                    "the classification authority is ND Dam Hazard"


def report_lines(info):
    """Markdown lines for the run report."""
    if not info:
        return []
    lines = ["## dam.json (shared dam record)", ""]
    for n in info.get("read") or []:
        lines.append("- Read: " + n)
    if info.get("written"):
        lines.append("- Written by this run (tool {}): {}".format(TOOL, ", ".join(
            info["written"])))
    if info.get("kept_from_record"):
        lines.append("- Same value already in dam.json (its first source kept): {}".format(
            ", ".join(info["kept_from_record"])))
    if info.get("updated"):
        lines.append("- Updated (this tool's own earlier value): {}".format(
            ", ".join(info["updated"])))
    for c in info.get("conflicts") or []:
        lines.append("- CONFLICT {}: dam.json {} ({}), this run {}".format(
            c["field"], _fmt(c["dam_json"]), c["dam_json_source"], _fmt(c["this_run"])))
    if info.get("error"):
        lines.append("- " + info["error"])
    lines.append("- File: {}".format(info.get("path", "")))
    lines.append("")
    return lines
