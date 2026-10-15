"""The logic of the guided wizard (wizard.py), with no Qt and no QGIS so it can be tested.

The wizard collects `answers` (a plain dict, see default_answers), checks each page
(validate_page), turns them into the parameters of the "Run breach model for one dam" tool
(build_rundam_params) and keeps a live run-time estimate (EstimateCache). Choices are stored
as the ids core uses ("piping", "lisflood", "sunny", "meters", "peak"); the index the
Processing enum parameters need is looked up in the same tuples the algorithms are built from,
and tests/test_wizard_logic.py checks that both stay in step.
"""
import copy
import json
import math
import os

from .core import breach as brmod
from .core import damjson
from .core import estimate as estmod
from .core import flood as floodmod
from .core import gdal_io
from .core import nd_data
from .core import workflow
from . import popup_logic as pl                 # 3.18.0: the Runner window's DEM download rules

ALGORITHM_ID = "ndbreach:rundam"

# enum parameters of the tool, in the order of their options
METHOD_IDS = tuple(brmod.METHODS)
TIER_IDS = tuple(workflow.TIERS)
SCENARIO_IDS = (workflow.SCENARIO_SUNNY, workflow.SCENARIO_FLOOD)
Z_IDS = tuple(gdal_io.Z_CHOICES)
TRIGGER_IDS = tuple(floodmod.TRIGGERS)

TIER_LABELS = {workflow.TIER_LISFLOOD: "LISFLOOD-FP only",
               workflow.TIER_BOTH: "LISFLOOD-FP for the extent, then ANUGA for the final results"}
SCENARIO_LABELS = {workflow.SCENARIO_SUNNY: "Sunny day: no inflow, breach at the top of dam",
                   workflow.SCENARIO_FLOOD: "Flood (PMF): inflow with and without the breach"}
Z_LABELS = {"auto": "Detect from the raster", "feet": "Feet", "us_feet": "US survey feet",
            "meters": "Meters"}
TRIGGER_LABELS = {floodmod.TRIGGER_PEAK: "at the highest pool level",
                  floodmod.TRIGGER_TIME: "at a set time (hours after the inflow starts)",
                  floodmod.TRIGGER_WSE: "when the pool reaches an elevation (ft)"}

# (nd_data field key, tool parameter, label, kind, required)
DAM_FIELDS = (
    ("F_ID", "DAM_ID", "Dam ID (NID ID)", "text", True),
    ("F_NAME", "DAM_NAME", "Dam name", "text", False),
    ("F_HEIGHT", "HEIGHT_FT", "Dam height (ft)", "number", True),
    ("F_STORAGE", "STORAGE_ACFT", "Storage at the top of dam (ac-ft)", "number", True),
    ("F_TOP", "TOP_FT", "Top of dam elevation (ft; empty = from the DEM)", "number", False),
    ("F_AREA", "AREA_AC", "Surface area at the top of dam (ac)", "number", False),
    ("F_PMF", "PMF_FT", "Maximum reservoir level in the PMF (ft)", "number", False),
    ("F_BOTTOM", "BOTTOM_FT", "Breach bottom elevation (ft)", "number", False),
    ("F_CREST", "CREST_WIDTH", "Crest width (ft)", "number", False),
    ("F_US", "US_SLOPE", "Upstream slope (H in H:1V)", "number", False),
    ("F_DS", "DS_SLOPE", "Downstream slope (H in H:1V)", "number", False),
    ("F_LENGTH", "CREST_LENGTH", "Crest length (ft)", "number", False),
)
FIELD_KEYS = tuple(f[0] for f in DAM_FIELDS)
PARAM_OF = dict((f[0], f[1]) for f in DAM_FIELDS)
NUMERIC_PARAMS = tuple(f[1] for f in DAM_FIELDS if f[3] == "number")
STRICTLY_POSITIVE = ("HEIGHT_FT", "STORAGE_ACFT")
NON_NEGATIVE = ("AREA_AC", "CREST_WIDTH", "US_SLOPE", "DS_SLOPE", "CREST_LENGTH")

PAGES = ("dam", "terrain", "scenario", "model")


# ------------------------------------------------------------------------------- reading a dam
def clean_number(v, default=None):
    """A layer attribute or typed text as a float; NULL, empty, text and NaN give `default`."""
    if v is None:
        return default
    if hasattr(v, "isNull"):
        try:
            if v.isNull():
                return default
        except Exception:
            pass
    if isinstance(v, str):
        t = v.strip().replace(",", "")
        if not t or t.lower() in ("null", "none", "nan", "na", "n/a"):
            return default
        v = t
    try:
        x = float(v)
    except (TypeError, ValueError):
        return default
    return default if (math.isnan(x) or math.isinf(x)) else x


def field_mapping(field_names):
    """Match a layer's fields to the dam data by name (core.nd_data.suggest_fields).

    Returns {"mapping": {F_KEY: field}, "how": [text], "missing": [labels of required data
    with no field]}."""
    found, how = nd_data.suggest_fields(list(field_names))
    mapping = dict((k, v) for k, v in found.items() if k in PARAM_OF)
    missing = [f[2] for f in DAM_FIELDS if f[4] and f[0] not in mapping]
    return {"mapping": mapping, "how": list(how), "missing": missing}


def _get(attrs, name):
    try:
        return attrs[name]
    except (KeyError, IndexError, TypeError):
        pass
    getter = getattr(attrs, "get", None)
    return getter(name) if getter else None


def dam_values_from_attrs(attrs, mapping):
    """{tool parameter: value} for the mapped fields of one feature's attributes. Numbers
    that are missing, NULL or text are left out; the ID and name are trimmed text."""
    out = {}
    for key, param, _label, kind, _req in DAM_FIELDS:
        field = mapping.get(key)
        if not field:
            continue
        raw = _get(attrs, field)
        if kind == "text":
            if raw is None or (hasattr(raw, "isNull") and raw.isNull()):
                continue
            t = str(raw).strip()
            if t and t.lower() != "null":
                out[param] = t
        else:
            x = clean_number(raw)
            if x is not None:
                out[param] = x
    if not out.get("DAM_ID"):
        did = dwr_dam_id(attrs)
        if did:
            out["DAM_ID"] = did
    return out


def dwr_dam_id(attrs):
    """3.13.0: 'DWR-<structure_id>' for a row of the ND DWR dam inventory (many DWR dams have no
    NID ID; the suite names their folders this way), else ''."""
    raw = _get(attrs, "structure_id")
    if raw is None or (hasattr(raw, "isNull") and raw.isNull()):
        return ""
    x = clean_number(raw)
    t = "{:d}".format(int(x)) if x is not None and x == int(x) else str(raw).strip()
    return "DWR-" + t if t and t.lower() != "null" else ""


def point_text(x, y, authid=""):
    """A point as a Processing point parameter's text: 'x,y [EPSG:26914]'."""
    geographic = abs(x) <= 180.0 and abs(y) <= 90.0
    fmt = "{:.8f},{:.8f}" if geographic else "{:.3f},{:.3f}"
    text = fmt.format(x, y)
    return "{} [{}]".format(text, authid) if authid else text


def parse_point(text):
    """(x, y) from 'x, y' or 'x,y [EPSG:xxxx]' (as typed in the wizard), or None."""
    t = str(text or "").split("[")[0].replace(";", ",").strip()
    parts = [p for p in t.replace(",", " ").split() if p]
    if len(parts) != 2:
        return None
    x, y = clean_number(parts[0]), clean_number(parts[1])
    return None if x is None or y is None else (x, y)


# ------------------------------------------------------------------------------- dam.json
def dam_record_values(dams_folder, dam_id):
    """v3.2.0: what <Dams folder>/<Dam ID>/dam.json offers the wizard: ({tool parameter:
    value} for height, storage, surface area, top of dam and name; the point as (x, y, crs)
    or None; log lines). Nothing when the folder or ID is not known yet."""
    dam_id = str(dam_id or "").strip()
    if not dams_folder or not dam_id:
        return {}, None, []
    rec, problem = damjson.load(os.path.join(dams_folder, damjson.folder_name(dam_id)))
    empty = dict((param, None) for param, _f, _l in damjson.PARAM_FIELDS
                 if param in PARAM_OF.values())
    vals, notes, used = damjson.fill(empty, rec)
    got = dict((k, v) for k, v in vals.items() if k in used)
    pt, note = damjson.point_from_record(rec)
    point = (pt[1], pt[2], pt[3]) if pt else None
    if note:
        notes.append(note)
    if problem:
        notes.insert(0, problem)
    return got, point, notes


def apply_dam_record(a):
    """Fill the wizard's EMPTY dam values and location from dam.json once the Dams folder and
    the dam ID are known (the terrain page calls it). Values given in the wizard win. The
    lines of what was taken are kept in a["record_notes"] and returned."""
    dam = a.setdefault("dam", {})
    got, point, notes = dam_record_values(a.get("dams_folder"), dam.get("DAM_ID"))
    labels = dict((param, label) for param, _f, label in damjson.PARAM_FIELDS)
    by_label = dict((n.split(" from dam.json")[0], n) for n in notes if " from dam.json" in n)
    lines = [n for n in notes if " from dam.json" not in n]       # problems, local datum
    for param, v in sorted(got.items()):
        if dam.get(param) in (None, ""):
            dam[param] = v
            lines.append(by_label.get(labels.get(param), "{} from dam.json: {}".format(
                labels.get(param, param), v)))
    if point and not a.get("point"):
        a["point"] = point
        lines.append(by_label.get("Dam point", "Dam point from dam.json"))
    a["record_notes"] = lines
    return lines


def apply_screening_handoff(a):
    """3.20.0: pre-select the breach method, the scenario and the cell size ND Dam Breach
    Screening actually ran for this dam, and say where they came from.

    Only values the wizard still holds at its own starting value are replaced - a wizard the
    user has clicked through keeps what they chose - and only once per dam
    (a["handoff_applied"] remembers which dam it was done for). The note for the page is in
    a["handoff_note"]; a["handoff_missing"] is what the screening has not recorded yet.
    Returns the lines that were applied."""
    folder = dam_folder_of(a)
    did = str((a.get("dam") or {}).get("DAM_ID") or "").strip()
    if not folder or a.get("handoff_applied") == did:
        return []
    try:
        from .core import screenhandoff as SH
        info = SH.read(folder)
    except Exception:                                            # noqa - never stop the wizard
        return []
    a["handoff_applied"] = did
    a["handoff_note"] = ""
    a["handoff_missing"] = SH.missing_text(info)
    if not SH.from_screening(info):
        return []
    start = default_answers()
    lines = []
    where = SH.source_text(info)
    if info.get("method") and a.get("method") == start["method"]:
        a["method"] = info["method"]
        lines.append("Breach {} {}".format(brmod.METHOD_LABELS.get(info["method"],
                                                                   info["method"]), where))
    if info.get("scenario") and a.get("scenario") == start["scenario"]:
        a["scenario"] = info["scenario"]
        lines.append("Scenario {} {}".format(SCENARIO_LABELS.get(info["scenario"],
                                                                 info["scenario"]), where))
    if info.get("cell_m") and float(a.get("cell_m") or 0.0) == float(start["cell_m"]):
        a["cell_m"] = float(info["cell_m"])
        lines.append("Cell size {:g} m {}".format(float(info["cell_m"]), where))
    a["handoff_note"] = ("The screening ran {} - {}.".format(SH.values_text(info), where)
                         if lines else
                         "The screening recorded {} {}; the values here are unchanged because "
                         "they were chosen in this wizard.".format(SH.values_text(info), where))
    a["handoff_lines"] = lines
    return lines


def dam_folder_dem(dams_folder, dam_id):
    """3.9.0: the DEM recorded in <Dams folder>/<Dam ID>/dam.json's data section ("Get data for
    this dam", the window's DEM download) when its file exists, else ""."""
    dam_id = str(dam_id or "").strip()
    if not dams_folder or not dam_id:
        return ""
    folder = os.path.join(dams_folder, damjson.folder_name(dam_id))
    if not os.path.isdir(folder):
        return ""
    try:
        from .damsuite import damdata as DD
        return DD.dam_data(folder).get("dem", "")
    except Exception:                                            # noqa - an unreadable dam.json
        return ""


def apply_dam_folder_dem(a):
    """3.9.0: an EMPTY DEM of the wizard from the dam folder's data (the terrain page calls it);
    a DEM chosen in the wizard or given by the window is kept. A DEM taken from the dam folder
    before follows a change of the dam. Returns the DEM taken or ""."""
    if a.get("dem_id") and not a.get("dem_from_dam_folder"):
        return ""
    dem = dam_folder_dem(a.get("dams_folder"), (a.get("dam") or {}).get("DAM_ID"))
    if dem:
        a["dem_id"] = dem
        a["dem_name"] = "{} (from dam folder)".format(os.path.basename(dem.split("|")[0]))
        a["dem_from_dam_folder"] = True
    elif a.get("dem_from_dam_folder"):                           # another dam, without a DEM
        a["dem_id"], a["dem_name"], a["dem_from_dam_folder"] = "", "", False
    return dem


# ------------------------------------------------------------- the DEM download (3.18.0) --
# The terrain page's "Download DEM for this dam" runs ND Dam Breach Screening's DEM download
# (tool 20, nddam:downloaddem) as the Runner window's DEM box does (dialog.py): USGS 3DEP around
# the dam point, into <dam folder>/data/. The tool writes <stem>_10m_5070.tif or
# <stem>_1m_5070.tif (EPSG:5070, metres, float32 GeoTIFF); popup_logic.finish_dem_download then
# renames it to data/dem_<cell>m.tif and records it in dam.json's data section, and
# apply_dam_folder_dem picks it as the wizard's DEM. Everything here is plain Python; the page
# gives QGIS's coordinate transform and GDAL as callables.
DEM_DOWNLOAD_ALG = "nddam:downloaddem"
DEM_RESOLUTIONS = ((10.0, "10 m"), (1.0, "1 m"))       # the tool's RESOLUTION enum, in order
DEM_SOURCE_NOTE = "DEM download of the wizard (ND Dam Breach Screening tool 20)"
DEM_NOT_INSTALLED = ("ND Dam Breach Screening is not installed, so the DEM download is "
                     "unavailable. Choose a DEM loaded in QGIS instead.")


def dem_cell_m(resolution_idx):
    """The cell (m) of a RESOLUTION index (0 = 10 m, 1 = 1 m; anything else = 10 m)."""
    try:
        return DEM_RESOLUTIONS[int(resolution_idx)][0]
    except (IndexError, TypeError, ValueError):
        return DEM_RESOLUTIONS[0][0]


def dem_stem(dam_id, lonlat=None):
    """The download tool's file stem for a dam (its local_tif_stem rule, area Dam): the dam ID
    with every character but letters, digits, - and _ replaced by _, without leading or
    trailing _; a dam without an ID is dam_<lon>_<lat> (- as m, . as p, 4 decimals); else dam."""
    name = "".join(c if c.isalnum() or c in "-_" else "_" for c in str(dam_id or "").strip())
    if not name.strip("_") and lonlat is not None:
        name = "dam_{:.4f}_{:.4f}".format(lonlat[0], lonlat[1]).replace("-", "m").replace(
            ".", "p")
    return name.strip("_") or "dam"


def dem_file_name(dam_id, cell_m, lonlat=None):
    """<stem>_10m_5070.tif or <stem>_1m_5070.tif: the file the download tool writes."""
    return "{}_{}m_5070.tif".format(dem_stem(dam_id, lonlat), int(round(float(cell_m))))


def dam_folder_of(a):
    """<Dams folder>/<dam ID folder> of the wizard's answers (the tools' folder rule), or ""."""
    dams = str(a.get("dams_folder") or "").strip()
    did = str((a.get("dam") or {}).get("DAM_ID") or "").strip()
    if not dams or not did:
        return ""
    return os.path.join(dams, damjson.folder_name(did))


def dam_lonlat(a, to_lonlat=None):
    """(lon, lat) of the wizard's dam: its point (x, y, authid) as it is when it is already
    lon / lat, else through `to_lonlat(x, y, crs)` (QGIS's transform; the page gives it), else
    the dam folder's dam.json (popup_logic.dam_lonlat). None when it is not known."""
    pt = a.get("point")
    if pt and len(pt) >= 2:
        x, y = clean_number(pt[0]), clean_number(pt[1])
        crs = str(pt[2] if len(pt) > 2 and pt[2] else "").strip()
        if x is not None and y is not None:
            if crs.upper() in ("", "EPSG:4326") and abs(x) <= 180.0 and abs(y) <= 90.0:
                return x, y
            if crs and to_lonlat is not None:
                try:
                    lon, lat = to_lonlat(x, y, crs)
                    lon, lat = clean_number(lon), clean_number(lat)
                except Exception:                                # noqa - an unknown CRS
                    lon = lat = None
                if lon is not None and lat is not None and abs(lon) <= 180.0 and \
                        abs(lat) <= 90.0:
                    return lon, lat
    return pl.dam_lonlat(dam_folder_of(a), to_lonlat)


def dem_download_params(a, resolution_idx=0, to_lonlat=None, buffer_mi=None):
    """The download tool's parameters for the wizard's dam -> (params, problems). The Runner
    window's choices: USGS 3DEP, area Dam, the buffer half the run tool's largest domain side
    (popup_logic.dem_box_half_mi, 30 mi for 60 mi), the resolution chosen next to the button,
    the output <dam folder>/data with the Dams folder and dam ID so the tool records the DEM
    in dam.json. `problems` are sentences for what is still missing (then params is {})."""
    problems = []
    dams = str(a.get("dams_folder") or "").strip()
    did = str((a.get("dam") or {}).get("DAM_ID") or "").strip()
    if not dams:
        problems.append("Choose the Dams folder on the dam page: the DEM goes into the dam's "
                        "folder there.")
    if not did:
        problems.append("Give the dam ID on the dam page: it names the DEM file and the dam "
                        "folder.")
    lonlat = dam_lonlat(a, to_lonlat)
    if lonlat is None:
        problems.append("The dam point is not known: pick the dam from the layer or type its "
                        "location on the dam page.")
    if problems:
        return {}, problems
    if buffer_mi is None:
        buffer_mi = pl.dem_box_half_mi(pl.settings_max_extent_mi())
    params = {"SOURCE": 0, "SCOPE": 0, "DAM_POINT": pl.lonlat_text(lonlat),
              "BUFFER_MI": float(buffer_mi), "COUNTY": "", "RESOLUTION": int(resolution_idx),
              "DWR_PATH": "", "ASCII_FT": False, "IMAGINE": True,
              "OUT": pl.dam_data_dir(dam_folder_of(a)), "DAMS_FOLDER": dams, "DAM_ID": did}
    return params, []


def dem_download_result(result):
    """The DEM file of the tool's result (its VRT output: a GeoTIFF since Screening 0.24.0), or
    "" (the dialog was cancelled, or an older QGIS returned nothing)."""
    if isinstance(result, dict):
        return str(result.get("VRT") or "").split("|")[0]
    if isinstance(result, str):
        return result.split("|")[0]
    return ""


def find_downloaded_dem(a, cell_m, since=None):
    """The DEM a download left for the wizard's dam when the tool's result does not say: the
    newest of data/dem_<cell>m.tif (after finish_dem_download) and data/<stem>_<cell>m_5070.tif
    (the tool's own name), changed at or after `since` (a time.time()). "" when there is none."""
    folder = dam_folder_of(a)
    if not folder:
        return ""
    did = (a.get("dam") or {}).get("DAM_ID")
    cands = (pl.dem_download_target(folder, cell_m),
             os.path.join(pl.dam_data_dir(folder), dem_file_name(did, cell_m)))
    best, best_t = "", None
    for p in cands:
        try:
            t = os.path.getmtime(p)
        except OSError:
            continue
        if since is not None and t < since - 1.0:
            continue
        if best_t is None or t > best_t:
            best, best_t = p, t
    return best


def finish_dem_download(a, result, cell_m, translate=None, since=None):
    """After the download tool's dialog closed: the DEM becomes data/dem_<cell>m.tif and is
    recorded in dam.json (popup_logic.finish_dem_download, as the Runner window does), then it
    is the wizard's DEM (a["dem_id"], "(from dam folder)"). Returns (path, notes); ("", notes)
    when there is no DEM (the dialog was cancelled): the wizard's choice is left as it was."""
    src = dem_download_result(result)
    if not src or not os.path.isfile(src):
        src = find_downloaded_dem(a, cell_m, since)
    if not src:
        return "", ["The download gave no DEM file."]
    path, notes = pl.finish_dem_download(src, dam_folder_of(a), cell_m, translate=translate,
                                         source=DEM_SOURCE_NOTE)
    if not path:
        return "", notes
    a["dem_id"], a["dem_name"], a["dem_from_dam_folder"] = "", "", False
    got = apply_dam_folder_dem(a)
    if not got or os.path.normcase(os.path.abspath(got.split("|")[0])) != \
            os.path.normcase(os.path.abspath(path)):
        a["dem_id"] = path                   # dam.json did not take it: the file itself
        a["dem_name"] = "{} (downloaded)".format(os.path.basename(path))
        a["dem_from_dam_folder"] = True
    return path, notes


# ------------------------------------------------------------------------------- the answers
def initial_answers(dams_folder="", dam_id="", dem=""):
    """3.9.0: the answers when the wizard opens from the Runner window: its Dams folder, dam ID
    and DEM (a layer id or a file). An empty DEM is the dam folder's ("Get data for this dam")."""
    a = default_answers(str(dams_folder or "").strip())
    did = str(dam_id or "").strip()
    if did:
        a["dam"]["DAM_ID"] = did
    dem = str(dem or "").strip()
    if dem:
        a["dem_id"] = dem
    else:
        apply_dam_folder_dem(a)
    return a


def default_answers(dams_folder=""):
    return {
        "layer_id": "", "feature_id": None, "mapping": {}, "dam": {},
        "point": None,                      # (x, y, authid) of the crest
        "dem_id": "", "z_units": "auto", "nlcd_id": "", "manning_id": "", "manning_n": 0.06,
        "dams_folder": dams_folder,
        "method": brmod.METHOD_PIPING, "scenario": workflow.SCENARIO_SUNNY,
        "inflow_csv": "", "spillway_csv": "", "normal_pool_ft": None,
        "trigger": floodmod.TRIGGER_PEAK, "trigger_value": None,
        "tier": workflow.TIER_LISFLOOD, "cell_m": 10.0,
    }


def build_rundam_params(a):
    """The parameters of 'Run breach model for one dam' for execAlgorithmDialog. Only what the
    wizard asked is given; every other parameter keeps the tool's default."""
    p = {}
    dam = a.get("dam") or {}
    for _key, param, _label, _kind, _req in DAM_FIELDS:
        if param in dam and dam[param] not in (None, ""):
            p[param] = dam[param]
    pt = a.get("point")
    if pt:
        p["DAM_POINT"] = point_text(pt[0], pt[1], pt[2] if len(pt) > 2 else "")
    p["METHOD"] = METHOD_IDS.index(a.get("method", brmod.METHOD_PIPING))
    p["SCENARIO"] = SCENARIO_IDS.index(a.get("scenario", workflow.SCENARIO_SUNNY))
    if a.get("scenario") == workflow.SCENARIO_FLOOD:
        if a.get("inflow_csv"):
            p["INFLOW_CSV"] = a["inflow_csv"]
        if a.get("spillway_csv"):
            p["SPILLWAY_CSV"] = a["spillway_csv"]
        if a.get("normal_pool_ft") is not None:
            p["NORMAL_POOL_FT"] = float(a["normal_pool_ft"])
        p["TRIGGER"] = TRIGGER_IDS.index(a.get("trigger", floodmod.TRIGGER_PEAK))
        if a.get("trigger_value") is not None:
            p["TRIGGER_VALUE"] = float(a["trigger_value"])
    if a.get("dem_id"):
        p["DEM"] = a["dem_id"]
    p["DEM_Z"] = Z_IDS.index(a.get("z_units", "auto"))
    if a.get("manning_id"):
        p["MANNING_RASTER"] = a["manning_id"]
    if a.get("nlcd_id"):
        p["NLCD_RASTER"] = a["nlcd_id"]
    p["MANNING"] = float(a.get("manning_n", 0.06))
    p["TIER"] = TIER_IDS.index(a.get("tier", workflow.TIER_LISFLOOD))
    p["CELL"] = float(a.get("cell_m", 10.0))
    if a.get("dams_folder"):
        p["DAMS_FOLDER"] = a["dams_folder"]
    return p


# ------------------------------------------------------------------------------- checking pages
def validate_page(page, a, exists=os.path.isfile, isdir=os.path.isdir, models=None):
    """(errors, warnings) for one page of the wizard. Errors stop the wizard from going on.
    `models` = {"lisflood": bool, "anuga": bool} when the caller knows what is installed."""
    errors, warnings = [], []
    if page == "dam":
        dam = a.get("dam") or {}
        if not str(dam.get("DAM_ID", "")).strip():
            errors.append("Give the dam ID.")
        # v3.2.0: a value the dam's dam.json gives may be left empty (known once the Dams
        # folder is, e.g. when the wizard is gone through again)
        rec_vals, rec_point, _n = dam_record_values(a.get("dams_folder"), dam.get("DAM_ID"))
        for param in STRICTLY_POSITIVE:
            v = dam.get(param)
            label = [f[2] for f in DAM_FIELDS if f[1] == param][0]
            if v is None and rec_vals.get(param) is not None:
                warnings.append("{}: taken from dam.json ({:g}).".format(label,
                                                                      rec_vals[param]))
            elif v is None:
                errors.append("Give the value for: {}.".format(label))
            elif v <= 0:
                errors.append("{} must be greater than 0.".format(label))
        for param in NON_NEGATIVE:
            v = dam.get(param)
            if v is not None and v < 0:
                label = [f[2] for f in DAM_FIELDS if f[1] == param][0]
                errors.append("{} cannot be negative.".format(label))
        # 3.9.0: the Dams folder is on this page (dam.json fills the values before this check)
        folder = str(a.get("dams_folder") or "").strip()
        if not folder:
            errors.append("Choose the Dams folder: each dam gets a folder named after its ID.")
        elif os.path.exists(folder) and not isdir(folder):
            errors.append("The Dams folder is a file, not a folder: {}".format(folder))
        if not a.get("point") and rec_point is None:
            errors.append("The dam needs a location (x, y of a point on the crest).")
        elif not a.get("point"):
            warnings.append("Location: taken from dam.json.")
        h, top, bottom = dam.get("HEIGHT_FT"), dam.get("TOP_FT"), dam.get("BOTTOM_FT")
        if top is not None and bottom is not None and h and bottom >= top:
            errors.append("The breach bottom elevation must be below the top of dam.")
        if dam.get("TOP_FT") is None:
            warnings.append("No top of dam elevation: the crest read from the DEM near "
                            "the dam point will be used. Check it in the run report.")
        if dam.get("AREA_AC") is None:
            warnings.append("No surface area: the breach is taken from storage alone.")
    elif page == "terrain":
        if not a.get("dem_id"):
            errors.append("Choose the DEM (a GeoTIFF or a VRT mosaic loaded in QGIS).")
        n = a.get("manning_n")
        if n is None or not (0.005 <= n <= 1.0):
            errors.append("Manning's n must be between 0.005 and 1.")
        if a.get("manning_id") and a.get("nlcd_id"):
            warnings.append("Both a Manning's n raster and an NLCD raster are set: the n "
                            "raster is used and the NLCD raster ignored.")
        if a.get("z_units") == "auto":
            warnings.append("Elevation units 'detect': a unit recorded in the raster is "
                            "trusted; without one, a feet CRS means feet unless the "
                            "elevations only fit North Dakota as metres. Check the units "
                            "note in the run report.")
    elif page == "scenario":
        if a.get("scenario") == workflow.SCENARIO_FLOOD:
            inflow = str(a.get("inflow_csv") or "").strip()
            if not inflow:
                errors.append("The flood scenario needs the PMF inflow hydrograph "
                              "(a CSV: time in hours, discharge in cfs).")
            elif not exists(inflow):
                errors.append("The inflow CSV does not exist: {}".format(inflow))
            spill = str(a.get("spillway_csv") or "").strip()
            if spill and not exists(spill):
                errors.append("The spillway CSV does not exist: {}".format(spill))
            if a.get("normal_pool_ft") is None and not spill:
                errors.append("The flood scenario needs the normal pool elevation or a "
                              "spillway rating CSV.")
            if a.get("trigger") != floodmod.TRIGGER_PEAK and a.get("trigger_value") is None:
                errors.append("Give the failure time (hours) or pool elevation (ft) for the "
                              "chosen trigger.")
            dam = a.get("dam") or {}
            if not spill:
                warnings.append("No spillway rating: the only releases are the breach and, "
                                "above the crest, the overtopping flow.")
            if dam.get("CREST_LENGTH") is None:
                warnings.append("No crest length (Dam page): overtopping of the embankment "
                                "cannot be computed without it.")
    elif page == "model":
        cell = a.get("cell_m")
        if cell is None or cell < 1.0:
            errors.append("The cell size must be at least 1 m.")
        elif cell < 5.0:
            warnings.append("A cell size under 5 m makes very large models; use the estimate "
                            "below before running.")
        if models is not None:
            if not models.get("lisflood"):
                errors.append("LISFLOOD-FP was not found. Run the installer "
                              "(scripts/install_macos.sh) or set it in Processing > Options "
                              "> Providers > ND Dam Breach Runner.")
            if a.get("tier") == workflow.TIER_BOTH and not models.get("anuga"):
                errors.append("The ANUGA tier needs the ANUGA Python environment, which was "
                              "not found. Run Checks > Set up ANUGA (one-time download).")
        if a.get("tier") == workflow.TIER_BOTH:
            warnings.append("ANUGA adds from a few minutes to several hours to the run.")
    else:
        raise ValueError("unknown page {!r}".format(page))
    return errors, warnings


def validate_all(a, **kw):
    """(errors, warnings) of every page, each message prefixed by its page."""
    errs, warns = [], []
    for page in PAGES:
        e, w = validate_page(page, a, **kw)
        errs += ["{}: {}".format(page, m) for m in e]
        warns += ["{}: {}".format(page, m) for m in w]
    return errs, warns


# ------------------------------------------------------------------------------- the estimate
def rescale_plan(plan, cell_m):
    """The plan of workflow.preview() for another cell size: same domain, cells scale with
    the inverse square (an estimate; the domain snaps to whole cells in a real run)."""
    p = copy.deepcopy(plan)
    old = float(plan["cell_m"])
    p["cells"] = int(round(plan["cells"] * (old / float(cell_m)) ** 2))
    p["cell_m"] = float(cell_m)
    return p


def estimate_lines(plan, rate, tier):
    """The text lines of core.estimate for a plan, the measured rate and the tier id."""
    if not plan.get("breach_occurs", True):
        return ["The dam does not fail in this flood: there is nothing to model."]
    est = estmod.estimate(plan, rate, tier == workflow.TIER_BOTH)
    return estmod.describe(plan, est)


class EstimateError(Exception):
    """The estimate could not be made; the message is for the user."""


class EstimateCache(object):
    """Live run-time estimate for the wizard.

    `make_plan(params)` -> (plan, context) does the expensive setup (workflow.preview through
    the tool's own parameter handling) and `measure_rate(context)` times a test flood; both are
    given by the caller so this class runs without QGIS. The plan is kept while the answers
    that shape it stay the same, and a change of the cell size or the tier only rescales it."""
    IGNORED = ("CELL", "TIER", "MANNING", "MANNING_RASTER", "NLCD_RASTER", "DAMS_FOLDER")

    def __init__(self, make_plan, measure_rate):
        self.make_plan, self.measure_rate = make_plan, measure_rate
        self.key = None
        self.plan = None
        self.context = None
        self.rate = None

    @classmethod
    def key_of(cls, answers):
        p = build_rundam_params(answers)
        for k in cls.IGNORED:
            p.pop(k, None)
        return json.dumps(p, sort_keys=True, default=str)

    def has_plan(self, answers):
        return self.plan is not None and self.key == self.key_of(answers)

    def compute(self, answers, force=False):
        """Set up the dam at the answers' cell size and time the test flood if needed.
        Returns the text lines."""
        key = self.key_of(answers)
        if force or self.plan is None or key != self.key:
            params = build_rundam_params(answers)
            try:
                plan, context = self.make_plan(params)
            except EstimateError:
                raise
            except Exception as e:
                raise EstimateError(str(e) or type(e).__name__)
            self.key, self.plan, self.context = key, plan, context
        if self.plan.get("breach_occurs", True) and self.rate is None:
            try:
                rate = self.measure_rate(self.context)
            except Exception as e:
                raise EstimateError(str(e) or type(e).__name__)
            if not rate:
                raise EstimateError("The LISFLOOD-FP test flood did not run: use 'Check the "
                                    "model installation' from the plugin menu.")
            self.rate = rate
        return self.lines(answers)

    def lines(self, answers):
        """The estimate for the answers' cell size and tier from the cached plan and rate, or
        None when there is no plan for these answers yet."""
        if not self.has_plan(answers):
            return None
        if not self.plan.get("breach_occurs", True):        # nothing to rescale or time
            return estimate_lines(self.plan, None, answers.get("tier"))
        if self.rate is None:
            return None
        plan = rescale_plan(self.plan, answers.get("cell_m", self.plan["cell_m"]))
        return estimate_lines(plan, self.rate, answers.get("tier", workflow.TIER_LISFLOOD))

    def reset(self):
        self.key = self.plan = self.context = self.rate = None


# ------------------------------------------------------------------------------- the review
def review_text(a, estimate=None, warnings=()):
    """The last page: what will be put into the tool's dialog."""
    dam = a.get("dam") or {}
    lines = ["Dam"]
    for _key, param, label, _kind, _req in DAM_FIELDS:
        if param in dam:
            lines.append("  {}: {}".format(label.split(" (")[0], dam[param]))
    pt = a.get("point")
    if pt:
        lines.append("  Location: {}".format(point_text(pt[0], pt[1], pt[2] if len(pt) > 2 else "")))
    lines += ["", "Terrain",
              "  DEM: {}".format(a.get("dem_name") or a.get("dem_id") or "(none)"),
              "  Elevation units: {}".format(Z_LABELS.get(a.get("z_units"), a.get("z_units")))]
    if a.get("manning_id"):
        lines.append("  Manning's n raster: {}".format(a.get("manning_name") or a["manning_id"]))
    elif a.get("nlcd_id"):
        lines.append("  NLCD land cover: {}".format(a.get("nlcd_name") or a["nlcd_id"]))
    lines.append("  Manning's n elsewhere: {:g}".format(a.get("manning_n", 0.06)))
    lines.append("  Dams folder: {}".format(a.get("dams_folder") or "(none)"))
    lines += ["", "Scenario",
              "  Breach: {}".format(brmod.METHOD_LABELS.get(a.get("method"), a.get("method"))),
              "  {}".format(SCENARIO_LABELS.get(a.get("scenario"), a.get("scenario")))]
    if a.get("scenario") == workflow.SCENARIO_FLOOD:
        lines.append("  Inflow: {}".format(a.get("inflow_csv") or "(none)"))
        if a.get("spillway_csv"):
            lines.append("  Spillway rating: {}".format(a["spillway_csv"]))
        if a.get("normal_pool_ft") is not None:
            lines.append("  Normal pool: {:g} ft".format(a["normal_pool_ft"]))
        lines.append("  The dam fails {}{}".format(
            TRIGGER_LABELS.get(a.get("trigger"), a.get("trigger")),
            "" if a.get("trigger_value") is None else " ({:g})".format(a["trigger_value"])))
    lines += ["", "Models",
              "  {}".format(TIER_LABELS.get(a.get("tier"), a.get("tier"))),
              "  Cell size: {:g} m".format(a.get("cell_m", 10.0))]
    if estimate:
        lines += ["", "Run time"] + ["  " + ln for ln in estimate]
    if a.get("record_notes"):
        lines += ["", "From the dam's dam.json"] + ["  " + n for n in a["record_notes"]]
    if warnings:
        lines += ["", "Check before running"] + ["  - " + w for w in warnings]
    lines += ["", "'Open the tool' opens 'Run breach model for one dam' with these values filled "
              "in. Everything else (structures, roads, DEM conditioning, ANUGA settings) is in "
              "its dialog, in the advanced section."]
    return "\n".join(lines)
