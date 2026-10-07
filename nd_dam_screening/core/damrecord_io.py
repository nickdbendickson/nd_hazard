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
     "triage_reason": tri_reason, "by": "tool 7b" / "tool 8"}
so damsuite.dam_record.next_action says "model" unless the triage cleared the dam. A status
whose substance (triage, hz_class, border, gaps) equals the newest "screened" entry of this
tool is not added again - whichever tool (7b or 8) wrote it. Numbers are rounded to 3
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


def tool8_values(attrs, class_field=None):
    v = identity(attrs, class_field)
    h = _num(attrs.get("height_m"))
    if h is not None and h > 0:
        v["height_ft"] = round3(h / FT)          # the same rounding as tool 7b's HEIGHT_FT
    return v


def screening_result(attrs, by):
    tri = _txt(attrs.get("tri_result"))
    res = {"triage": tri or "not run", "hz_class": _txt(attrs.get("hz_class")),
           "border": _txt(attrs.get("hz_border")), "gaps": _txt(attrs.get("hz_gaps")),
           "by": by}
    if _txt(attrs.get("tri_reason")):
        res["triage_reason"] = _txt(attrs.get("tri_reason"))
    if _txt(attrs.get("tri_audit")) == "yes":
        res["triage_audit"] = "yes (sent to the 2D model anyway)"
    return res


SUBSTANCE = ("triage", "hz_class", "border", "gaps")   # what next_action / the dashboards read


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
