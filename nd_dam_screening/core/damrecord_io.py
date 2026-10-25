"""The screening's entries in the suite's dam record, `<Dams folder>/<dam ID>/dam.json`
(damsuite.dam_record; tool name "ndscreen", version = metadata.txt). Pure Python, no QGIS.

Written by
  * tool 7b for every dam it sends to ND Dam Breach Runner, BEFORE the Runner runs: the values
    it sends (dam_id, name, nid_id, dwr_structure_id, height_ft, storage_acft, surface_area_ac,
    the breach point as lon / lat and x / y / crs, existing_hazard_class) and a "screened"
    status;
  * tool 8, when given the Dams folder, for every dam that ALREADY has a folder there (no folder
    is created): the dam's identity, height and existing class and a "screened" status.

Rules (damsuite.dam_record): values are set with set_many - a value another tool (or the
engineer) put there is never overwritten; the refusal is recorded in the file's "conflicts"
and reported back here for the log. The "screened" status result is
    {"triage": tri_result of tool 6 ("clear" / "needs 2D") or "not run",
     "hz_class": tool 8's class or None, "border": hz_border, "gaps": hz_gaps,
     "triage_reason": tri_reason, "by": "tool 7b" / "tool 8",
     "cell_m": the grid the flood really ran at, in metres (0.28.0),
     "method": the breach method as an ND Dam Breach Runner key, "scenario": "sunny" / "flood",
     "loading_case": "top of dam" / "sunny day", the case the class came from}
so damsuite.dam_record.next_action says "model" unless the triage cleared the dam, and a
detailed run of the same dam can start from the same grid, breach and loading case instead of
the Runner's own defaults (see RUNNER_METHOD below). A status whose substance (triage, hz_class,
border, gaps, and those three) equals the newest "screened" entry of this tool is not added
again - whichever tool (7b or 8) wrote it. Numbers are rounded to 3
decimals, as tool 7b sends them to the Runner (round3). A dam.json that
cannot be read (broken, foreign, newer format) is never replaced: the dam is reported and
skipped.

lon / lat of the breach point are computed from EPSG:5070 with the Albers equal-area inverse
(Snyder 1987) on GRS 1980 - NAD83 degrees, used as WGS 84 (they differ by about 1 m here).
"""
import configparser
import math
import os
import re

try:
    from ..damsuite import dam_record as dr
except ImportError:                                   # core/ itself on sys.path (tests)
    import importlib
    dr = importlib.import_module("damsuite.dam_record")

TOOL = "ndscreen"
FT = 0.3048
AF_M3 = 1233.4818375
ACRE_M2 = 4046.8564224
_PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def plugin_version(folder=_PLUGIN_DIR):
    """version= of the plugin's metadata.txt ("" when it cannot be read)."""
    cp = configparser.ConfigParser(interpolation=None, strict=False)
    try:
        with open(os.path.join(folder, "metadata.txt"), encoding="utf-8-sig") as fh:
            cp.read_file(fh)
        return cp.get("general", "version", fallback="").strip()
    except (OSError, configparser.Error):
        return ""


# ------------------------------------------------------------------ EPSG:5070 inverse
_A, _INVF = 6378137.0, 298.257222101
_E2 = 2.0 / _INVF - 1.0 / _INVF ** 2
_E = math.sqrt(_E2)


def _q(phi):
    s = math.sin(phi)
    return (1 - _E2) * (s / (1 - _E2 * s * s) - (1 / (2 * _E)) * math.log((1 - _E * s) / (1 + _E * s)))


def _m(phi):
    s = math.sin(phi)
    return math.cos(phi) / math.sqrt(1 - _E2 * s * s)


_P1, _P2, _P0, _L0 = (math.radians(v) for v in (29.5, 45.5, 23.0, -96.0))
_N = (_m(_P1) ** 2 - _m(_P2) ** 2) / (_q(_P2) - _q(_P1))
_C = _m(_P1) ** 2 + _N * _q(_P1)
_RHO0 = _A * math.sqrt(_C - _N * _q(_P0)) / _N


def lonlat_from_5070(x, y):
    """(lon, lat) degrees of an EPSG:5070 point."""
    dy = _RHO0 - y
    rho = math.hypot(x, dy)
    theta = math.atan2(x, dy)
    q = (_C - (rho * _N / _A) ** 2) / _N
    phi = math.asin(max(-1.0, min(1.0, q / 2)))
    for _i in range(25):
        s, c = math.sin(phi), math.cos(phi)
        nxt = phi + (1 - _E2 * s * s) ** 2 / (2 * c) * (
            q / (1 - _E2) - s / (1 - _E2 * s * s) + (1 / (2 * _E)) * math.log((1 - _E * s) / (1 + _E * s)))
        if abs(nxt - phi) < 1e-13:
            phi = nxt
            break
        phi = nxt
    return math.degrees(_L0 + theta / _N), math.degrees(phi)


# ------------------------------------------------------------------ values
def _txt(v):
    if v is None:
        return None
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    s = str(v).strip()
    return s if s and s.upper() not in ("NULL", "NONE") else None


def _num(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def identity(attrs, class_field=None):
    """dam_id, name, nid_id, dwr_structure_id and existing_hazard_class of a screening dam
    record (tool 1 names: dam_id 'DWR-5289' -> DWR structure 5289; 'NID-ND00123' has no DWR
    number)."""
    dam_id = _txt(attrs.get("dam_id"))
    out = {"dam_id": dam_id, "name": _txt(attrs.get("name")),
           "nid_id": (_txt(attrs.get("nid_id")) or "").upper() or None}
    m = re.match(r"^DWR-(.+)$", dam_id or "", re.I)
    if m:
        out["dwr_structure_id"] = m.group(1).strip()
    elif _txt(attrs.get("structure_id")):
        out["dwr_structure_id"] = _txt(attrs.get("structure_id"))
    if out["nid_id"] is None:
        m = re.match(r"^NID-(.+)$", dam_id or "", re.I)
        if m:
            out["nid_id"] = m.group(1).strip().upper()
    for f in (class_field, "haz_existing", "hazard_cla"):
        if f and _txt(attrs.get(f)):
            out["existing_hazard_class"] = _txt(attrs.get(f))
            break
    return {k: v for k, v in out.items() if v not in (None, "")}


def round3(x):
    """Every number the screening writes to dam.json is rounded like the values it sends to the
    Runner (handoff.rundam_params: 3 decimals), so tool 7b and tool 8 never write two values of
    one quantity that differ in the last digits (a later write would record a conflict)."""
    return None if x is None else round(float(x), 3)


def handoff_values(attrs, rundam_params, point_xy, crs="EPSG:5070", class_field=None):
    """The values tool 7b sends to 'ndbreach:rundam', as dam record fields."""
    v = identity(attrs, class_field)
    p = rundam_params or {}
    for key, field in (("HEIGHT_FT", "height_ft"), ("STORAGE_ACFT", "storage_acft"),
                       ("AREA_AC", "surface_area_ac")):
        if _num(p.get(key)) is not None:
            v[field] = round3(_num(p.get(key)))
    if point_xy is not None:
        x, y = float(point_xy[0]), float(point_xy[1])
        v.update({"x": round3(x), "y": round3(y), "crs": crs})
        if crs.upper() == "EPSG:5070":
            lon, lat = lonlat_from_5070(x, y)
            v.update({"lon": round(lon, 7), "lat": round(lat, 7)})
    return v


AC_M2 = 4046.8564224
# storage at the top of dam, in tool 7b's order of preference (core/handoff.STORAGE_SOURCES)
STORAGE_KEYS = ("td_vw_m3", "cal_total_m3", "est_total_m3", "inv_max_m3")


def tool8_values(attrs, class_field=None, area_top_m2=None):
    """The dam record values of a screened dam. 0.26.0: besides the identity and the height, what
    ND Dam Breach Runner needs to start from the record alone (storage at the top of dam, surface
    area, crest length) and the dam point (attrs "_x", "_y", "_crs" when tool 8 adds them), so a
    dam screened alone can be modelled from the Runner window without typing anything.
    area_top_m2 (0.32.4): the surface area at the top of dam from the tool-3 curve, worked out
    exactly as tool 7b's hand-off does (core/handoff.area_for_runner); None writes no area."""
    v = identity(attrs, class_field)
    h = _num(attrs.get("height_m"))
    if h is not None and h > 0:
        v["height_ft"] = round3(h / FT)          # the same rounding as tool 7b's HEIGHT_FT
    for key in STORAGE_KEYS:
        vol = _num(attrs.get(key))
        if vol is not None and vol > 0:
            v["storage_acft"] = round3(vol / AF_M3)
            break
    # surface_area_ac is the area at the TOP OF DAM: the Runner turns it into its storage-area
    # exponent (area * height / storage). The screening's est_area_m2 / inv_area_m2 are the
    # normal-pool (mapped water) area, which on a dry flood-control dam is near zero and forces
    # the exponent to its floor of 1, about a third low on the peak breach flow (Absaraka
    # DWR-2801). The tool-7b / runchain handoff already leaves it out for this reason
    # (core/handoff.py area_for_runner, "normal-pool areas are not passed"); do the same here,
    # so a dam modelled from the Runner window off dam.json gets the depth-squared default
    # (exponent 2) instead of a pool area read as the top-of-dam area.
    # 0.32.4: the top-of-dam area from the tool-3 curve IS what the Runner wants, so it is written
    # when tool 8 has it - the same number tool 7b sends as AREA_AC.
    if area_top_m2 is not None and area_top_m2 > 0:
        v["surface_area_ac"] = round3(area_top_m2 / ACRE_M2)
    crest = _num(attrs.get("crest_len_m"))
    if crest is not None and crest > 0:
        v["dam_length_ft"] = round3(crest / FT)
    x, y, crs = _num(attrs.get("_x")), _num(attrs.get("_y")), _txt(attrs.get("_crs"))
    if x is not None and y is not None and crs:
        if crs.upper() in ("EPSG:4326", "OGC:CRS84"):
            v["lon"], v["lat"] = round(x, 6), round(y, 6)
        else:
            v["x"], v["y"], v["crs"] = round3(x), round3(y), crs
    return v


# ------------------------------------------------- 0.28.0: what the detailed run should reuse --
# ND Dam Breach Runner starts a detailed run from the newest "screened" result (its
# core/screenhandoff.py) and fills only the inputs the engineer left empty. Without these three
# keys a 20 m screening became a 10 m Runner run with the Runner's own default breach - the same
# dam modelled twice, differently, and nobody could see why the numbers moved. The keys and the
# spellings are the Runner's, not ours: "cell_m" in METRES, "method" one of its core/breach.py
# METHODS keys, "scenario" exactly "sunny" or "flood".
RUNNER_SUNNY, RUNNER_FLOOD = "sunny", "flood"
# This plugin's two loading cases, in the Runner's vocabulary. BOTH are "sunny" there, and that
# is not a mapping bug: the Runner's "flood" routes a flood INFLOW hydrograph (PMF) through the
# reservoir, which no screening run does - core/breach_hydro: "no added inflow, no coincident
# downstream flooding". The screening's "td" starts the breach with the pool already at the top
# of dam and no inflow (ND 5.1.1), which is the Runner's sunny-day case with a higher starting
# pool; its own core/screenhandoff.py reads the words "top of dam" as sunny for the same reason.
# Handing over "flood" would start a different model (and fail outright on a dam with no
# flood-frequency data), so the case itself is recorded beside it as loading_case.
SCENARIO_WORD = {"td": RUNNER_SUNNY, "sd": RUNNER_SUNNY}
LOADING_CASE = {"td": "top of dam", "sd": "sunny day"}
# (brch_eq of tool 5, breach mode) -> the Runner's method key. VTG and MLM have one key each
# there (the mode is in their own equations), and TR-60 is a peak, not a width.
RUNNER_METHOD = {
    ("F2008", "piping"): "piping", ("F2008", "overtopping"): "overtopping",
    ("F1995", "piping"): "froehlich1995_piping",
    ("F1995", "overtopping"): "froehlich1995_overtopping",
    ("MLM", ""): "mlm1984", ("VTG-E", ""): "vtg1990", ("VTG-R", ""): "vtg1990",
    ("TR60", ""): "tr60", ("TR60-C", ""): "tr60_curvilinear",
}
DEFAULT_MODE = "piping"                                # tool 5's own default (the ND standard)


def runner_method(attrs):
    """The Runner's breach-method key for a screened dam ("" when tool 5 has not run).

    tool 5 records the equation (brch_eq) and, since 0.28.0, the mode (brch_mode). An older row
    has no mode: piping is tool 5's default and the ND standard, so it is assumed rather than
    leaving the Runner with its own default, and the mode only matters for the two Froehlich
    equations anyway.
    """
    eq = _txt(attrs.get("brch_eq")) or ""
    if not eq:
        return ""
    mode = (_txt(attrs.get("brch_mode")) or "").lower()
    if eq in ("F2008", "F1995"):
        mode = "overtopping" if mode.startswith("overtop") else DEFAULT_MODE
    else:
        mode = ""
    return RUNNER_METHOD.get((eq, mode), "")


def governing_case(attrs):
    """This plugin's key ("td" / "sd") for the loading case the reported class came from ("" when
    there is none).

    With both loading cases in one run (0.28.0) tool 8 writes the governing one in hz_scen, and
    that is the case to hand on: the Runner fills ONE scenario, and it has to be the case the
    class was taken from, not the first one that happened to run.
    """
    key = (_txt(attrs.get("hz_scen")) or _txt(attrs.get("scenario")) or "").lower()
    return key if key in SCENARIO_WORD else ""


def runner_scenario(attrs):
    """The Runner's word for that case ("" when there is none). See SCENARIO_WORD."""
    return SCENARIO_WORD.get(governing_case(attrs), "")


def runner_cell_m(attrs):
    """The grid cell the flood really ran at, in metres (None when no flood ran).

    sim_cell_m is written by tool 7 from the grid the engine actually used - not the Settings
    tab's box and not the preset, which is the whole point (core/runplan.py). A dam cleared by
    triage has no flood and therefore no cell: better none than a number nothing ran at.
    """
    cell = _num(attrs.get("sim_cell_m"))
    if cell is None or cell <= 0:
        return None
    return round3(cell)


def screening_result(attrs, by):
    tri = _txt(attrs.get("tri_result"))
    res = {"triage": tri or "not run", "hz_class": _txt(attrs.get("hz_class")),
           "border": _txt(attrs.get("hz_border")), "gaps": _txt(attrs.get("hz_gaps")),
           "by": by}
    if _txt(attrs.get("tri_reason")):
        res["triage_reason"] = _txt(attrs.get("tri_reason"))
    if _txt(attrs.get("tri_audit")) == "yes":
        res["triage_audit"] = "yes (sent to the 2D model anyway)"
    # 0.28.0: what a detailed run of the same dam should start from (see RUNNER_METHOD above)
    cell = runner_cell_m(attrs)
    if cell is not None:
        res["cell_m"] = cell
    for key, value in (("method", runner_method(attrs)), ("scenario", runner_scenario(attrs)),
                       ("loading_case", LOADING_CASE.get(governing_case(attrs), ""))):
        if value:
            res[key] = value
    return res


# what next_action / the dashboards read - and, since 0.28.0, what a detailed run reuses: a
# screening re-run at another cell, breach or loading case is a new status entry, because the
# Runner reads the NEWEST one and a stale grid size there is worse than an extra entry
SUBSTANCE = ("triage", "hz_class", "border", "gaps", "cell_m", "method", "scenario",
             "loading_case")


def same_substance(a, b):
    """True when two "screened" results agree on triage, class, borderline flag and data gaps
    (who wrote them - "by" - and the free-text reasons do not count)."""
    a, b = a or {}, b or {}
    norm = lambda v: None if v in (None, "") else str(v).strip()
    return all(norm(a.get(k)) == norm(b.get(k)) for k in SUBSTANCE)


def data_entries(rec, data, source="ND Dam Breach Screening (tool 7b hand-off)"):
    """0.16.0: record the data files the screening used for the dam (DEM, structures, roads ...)
    in dam.json's data section, so ND Dam Breach Runner and ND Dam Hazard fill their empty inputs
    with them. `data`: {data role: file path ('|layername=...' kept)}. Only roles the data section
    has NO entry for are written (what "Get data for this dam" or another tool recorded stays);
    only paths of files that exist (not project layer IDs). Returns [log lines]."""
    try:
        from ..damsuite import damdata as dd
    except ImportError:                               # core/ itself on sys.path (tests)
        import importlib
        dd = importlib.import_module("damsuite.damdata")
    have = dd.data_section(rec)
    log = []
    for role, p in sorted((data or {}).items()):
        if role not in dd.DATA_ROLES or not p or have.get(role):
            continue
        p = str(p)
        if not os.path.exists(p.split("|")[0]):
            continue
        dd.set_entry(rec, role, p, source)
        log.append("dam.json: {} recorded in the data section ({})".format(dd.LABELS.get(role, role), p))
    return log


ESTIMATE_NOTE = "screening: ESTIMATED from the DEM (no DWR value)"


def estimate_notes(attrs):
    """0.19.0: {dam record field: note} for the values the screening ESTIMATED (tool 3's h_src /
    stor_src), so ND Dam Breach Runner and ND Dam Hazard can say so in their outputs."""
    out = {}
    if "estimated" in str(attrs.get("h_src") or "").lower():
        out["height_ft"] = ESTIMATE_NOTE
    if "estimated" in str(attrs.get("stor_src") or "").lower():
        out["storage_acft"] = ESTIMATE_NOTE
    return out


# 0.32.4: fields this tool may leave out on a later run because it no longer has a sound value.
# Screening 0.32.2 and older wrote the normal-pool area as surface_area_ac; without this, a dam
# screened again keeps that stale area and the Runner still reads it as the top-of-dam area.
OWN_OPTIONAL = ("surface_area_ac",)


def _drop_stale(rec, values):
    """Remove an OWN_OPTIONAL field this tool wrote before and does not write now. A value from
    another tool or the engineer is never touched. Returns log lines."""
    log = []
    for name in OWN_OPTIONAL:
        if values.get(name) not in (None, ""):
            continue
        src = rec.sources.get(name) or {}
        if name in rec.fields and src.get("tool") == TOOL:
            old = rec.fields.pop(name)
            rec.sources.pop(name, None)
            log.append("dam.json: {} {!r} from screening {} removed (no top-of-dam value from "
                       "this run; a normal-pool area is not the top-of-dam area)".format(
                           name, old, src.get("version") or "?"))
    return log


def write(folder, values, result, version=None, create=True, data=None, field_notes=None):
    """Update <folder>/dam.json. Returns (path or None, [log lines]). create=False: nothing
    happens when the folder does not exist. 0.16.0: `data` {data role: path} - the files used,
    recorded in the data section where it has no entry for the role yet (data_entries)."""
    version = plugin_version() if version is None else version
    if not os.path.isdir(folder) and not create:
        return None, []
    try:
        rec = dr.load(folder, create=True)
    except ValueError as e:
        return None, ["dam.json not updated (the file is kept as it is): {}".format(e)]
    log = []
    values = dict(values)
    refused = []
    for name, note in (field_notes or {}).items():      # 0.19.0: estimated values say so
        if name in values and values[name] not in (None, ""):
            if not rec.set(name, values.pop(name), TOOL, version, note=note):
                if any(c.get("field") == name for c in rec.conflicts):
                    refused.append(name)
    refused += rec.set_many(values, TOOL, version, note="screening")
    log += _drop_stale(rec, values)
    for name in refused:
        c = next((c for c in reversed(rec.conflicts) if c.get("field") == name), {})
        src = (c.get("old_source") or {}).get("tool") or "the engineer / unknown"
        log.append("dam.json: {} kept at {!r} (from {}); the screening's {!r} was not written "
                   "(recorded as a conflict)".format(name, c.get("old"), src, c.get("new")))
    last = rec.latest("screened")
    if not (last and last.get("tool") == TOOL and
            same_substance(last.get("result"), result)):
        rec.add_status("screened", TOOL, version, result)
    if data:
        try:
            log += data_entries(rec, data)
        except Exception as e:                        # noqa - the values are written anyway
            log.append("dam.json: data paths not recorded: {}".format(e))
    path = rec.save()
    return path, log
