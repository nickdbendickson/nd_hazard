"""NGVD29 -> NAVD88 for the dam elevations the plugins compare with the LiDAR DEM (1.18.0).

The ND LiDAR DEMs are NAVD88. DWR and NID inventory elevations (top of dam, normal pool, max
pool, spillway crest) and values an engineer types are often NGVD29 (the "1929 datum" of the
original plans), about 0.6 - 2.1 ft lower in North Dakota. A value whose datum is not stated
as NAVD88 is taken as NGVD29 and shifted with NOAA's VERTCON at the dam (Nick, 2026-10-28:
"convert the 1071.15 NGVD29 to NAVD88 ... for all dams that do not state the elevations as
NAVD88"); one stated NAVD88 (or "DEM", read from the DEM itself) is used as it is.

Offline: the shift comes from the bundled VERTCON grid clipped to North Dakota (vertcon_nd.py,
public-domain NOAA data, bilinear between the 0.05 deg nodes). Outside the clip, pyproj with
PROJ's us_noaa_vertconc.tif is tried when both are installed (no network request: PROJ's
network access is left as it is set); else the values are used unconverted, with a warning.
No web service (NCAT) is called.

Local datums (1.18.1, Nick 2026-10-28: "if elevation is 100 or similar, they would be local
and would have to take actual elevations from dem"): plans often use an assumed datum (top of
dam 100.0 ft). local_check() flags a top of dam (or, with no top, a pool) as local when
  * it lies outside ND_MIN_FT .. ND_MAX_FT (500 - 3,600 ft; ND ground is about 750 ft at the
    Red River to 3,506 ft at White Butte, so no ND elevation on NGVD29 / NAVD88 is outside it),
  * it is more than DEM_TOL_FT (50 ft) from the DEM crest near the dam after the NGVD29 shift
    (the NGVD29 / NAVD88 difference in ND is under 2.1 ft; the inventory heights are rarely
    off by more than a few feet), or
  * it is a round value (a multiple of 100 ft: 100, 1000, 1100 ...) more than ROUND_TOL_FT
    (10 ft) from the DEM crest.
A local value is not datum-converted: the top of dam becomes the DEM crest and every other
dam elevation keeps its height relative to the local top (DEM top - (local top - value)).
The record then has source_datum "local (assumed)" (or "local" when stated / chosen), method
"DEM", the original values in fields and a warning. MODE "local" (the window's "Local (use
DEM)") forces it; an explicit NAVD88 / NGVD29 choice turns the detection off.

0 - 500 ft is always local (1.18.2, Nick 2026-10-28): a dam or structure elevation (crest / top,
streambed, spillway, normal / max pool, outlet, culvert inverts ...) between LOCAL_MIN_FT and
LOCAL_MAX_FT (0 - 500 ft, inclusive) is on a local datum - no North Dakota ground is that low -
and is NEVER VERTCON-shifted, whatever the stated or chosen datum (1.18.2 as revised, Nick 2026-10-29:
"Forget overrid datum. Convert local to navd 88." - no override wording: a band value is simply
local and converted to NAVD88 by the DEM tie). It is local_check()'s first,
unconditional test (is_local_elev); plan(values=...) makes the whole record local when the top
of dam (or, with no top, a pool) is in that band, and convert_ft() never shifts such a value
even in a record that is not local (it is flagged "local_elev" for the caller to place or
drop). The elevations are then taken from the DEM (NAVD88): the top of dam from the DEM at
mid-crest (midcrest.py), the others tied by the offset DEM crest - local crest ("DEM-derived").
No split (Nick 2026-10-29): every elevation of a local record, in ND's range or not, gets
the same DEM offset. In a record whose top is not local, a 0 - 500 ft value is tied by
the DEM crest when the top is local by another rule, else flagged for review; the
other values follow the normal datum path. With no tie (no DEM crest, a DEM in the wrong units,
no mid-crest, no local crest to tie by) the Runner flags the record for review (rec["review"])
and leaves the local values unconverted (fields marked "unconverted").
Plausibility guard (Nick 2026-10-29, "change them"): a value tied by the DEM offset that lands
more than TIE_MAX_FROM_CREST_FT (100 ft) above or below the DEM crest is not used - it is
flagged REVIEW, left UNCONVERTED and named in a warning with how far off it is (e.g. a
streambed of 845 ft in a record whose top is 100 ft would land 745 ft above the crest).

    rec = plan(lon, lat, stated="", override="auto")
    rec["shift_ft"], rec["applied"], rec["warnings"]
    new = convert_ft(1071.15, rec, "top_of_dam_ft")    # 1072.239 at DWR-2801 (NCAT: 1072.24)

plan() records what was done: {"target": "NAVD88", "source_datum", "how" ("stated" /
"assumed" / "override"), "stated", "override", "lon", "lat", "shift_m", "shift_ft", "method",
"applied", "fields": {name: {"source": ft, "navd88": ft}}, "warnings": [...]}. That dict is
what the Runner writes into run_report.json and dam.json (extra "vertical_datum") and what
ND Dam Hazard prints in its summary (summary_lines).

Pure Python standard library."""
import base64
import math
import struct
import zlib

NAVD88 = "NAVD88"
NGVD29 = "NGVD29"
LOCAL = "local"
TARGET = NAVD88
AUTO = "auto"
MODES = (AUTO, NAVD88, NGVD29, LOCAL)
LOCAL_ASSUMED = "local (assumed)"   # source_datum of a value detected as local
METHOD_DEM = "DEM"
# local_check thresholds (see the module text)
ND_MIN_FT = 500.0
ND_MAX_FT = 3600.0
DEM_TOL_FT = 50.0
ROUND_TOL_FT = 10.0
# 1.18.4: widened from 0-300 to 0-500. 301-499 is below ND_MIN_FT, so no North Dakota
# ground elevation, but 1.18.2 still VERTCON-shifted it unless another local rule ran.
# 500 is included: the range test is v < 500, so 500.00 was the round value that slipped through.
LOCAL_MIN_FT = 0.0
LOCAL_MAX_FT = 500.0
LOCAL_BAND_WHY = ("between {:.0f} and {:.0f} ft: a local datum (no North Dakota ground is that "
                  "low), never datum-converted")
ROUND_STEP_FT = 100.0
# 1.18.2 plausibility guard (Nick 2026-10-29): a DEM-tied value farther than this from the
# DEM crest (above or below, ft) is not used - REVIEW, left unconverted
TIE_MAX_FROM_CREST_FT = 100.0
# rec["tie"] of a local record: "pending" until the Runner's DEM tie (apply_local) has run,
# then "done" / "review" / "not applied (...)"
TIE_PENDING = "pending"
NAVD88_TAG = "NAVD88 ft"          # dam.json top_of_dam_datum of a converted (or NAVD88) value
FT = 0.3048
METHOD_GRID = "VERTCON grid (NOAA vertconc.gtx 2018, bundled ND clip, bilinear)"
METHOD_PYPROJ = "VERTCON grid via pyproj vgridshift (PROJ us_noaa_vertconc.tif)"
METHOD_NONE = "none"

_GRID = None


def classify(text):
    """NAVD88 / NGVD29 / "local" / "" (not stated or not recognised) for a datum text:
    'NAVD88 ft', 'NAVD 88', 'DEM', 'DEM estimate', 'LiDAR' -> NAVD88; 'NGVD29', 'NGVD 1929',
    'MSL', '1929' -> NGVD29; 'local', 'assumed', 'arbitrary' -> local."""
    t = str(text or "").strip().lower()
    if not t:
        return ""
    navd = "navd" in t or "1988" in t or t == "dem" or t.startswith(("dem ", "dem(", "lidar"))
    ngvd = "ngvd" in t or "1929" in t or "msl" in t or "mean sea level" in t
    if navd and ngvd:
        return ""                 # 'NGVD29 / NAVD88': ambiguous - treated as not stated
    if navd:
        return NAVD88
    if ngvd:
        return NGVD29
    if "local" in t or "assumed" in t or "arbitrary" in t:
        return LOCAL
    return ""


def mode(text):
    """The override setting from a text or an index (0 auto, 1 NAVD88, 2 NGVD29, 3 local)."""
    if isinstance(text, int) and not isinstance(text, bool):
        return MODES[text] if 0 <= text < len(MODES) else AUTO
    c = classify(text)
    return c if c in (NAVD88, NGVD29, LOCAL) else AUTO


def _grid():
    global _GRID
    if _GRID is None:
        from . import vertcon_nd as V
        raw = zlib.decompress(base64.b64decode("".join(V.DATA)))
        n = V.NCOLS * V.NROWS
        _GRID = (V, struct.unpack("<{}h".format(n), raw))
    return _GRID


def grid_shift_m(lon, lat):
    """NAVD88 - NGVD29 (m) from the bundled ND grid, or None outside it."""
    try:
        lon, lat = float(lon), float(lat)
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(lon) and math.isfinite(lat)):
        return None
    V, vals = _grid()
    fi = (lon - V.LON0) / V.STEP
    fj = (V.LAT_TOP - lat) / V.STEP
    eps = 1e-9
    if fi < -eps or fj < -eps or fi > V.NCOLS - 1 + eps or fj > V.NROWS - 1 + eps:
        return None
    fi = min(max(fi, 0.0), V.NCOLS - 1.0)
    fj = min(max(fj, 0.0), V.NROWS - 1.0)
    i0, j0 = min(int(fi), V.NCOLS - 2), min(int(fj), V.NROWS - 2)
    tx, ty = fi - i0, fj - j0

    def at(j, i):
        return vals[j * V.NCOLS + i] / float(V.SCALE)
    top = at(j0, i0) * (1 - tx) + at(j0, i0 + 1) * tx
    bot = at(j0 + 1, i0) * (1 - tx) + at(j0 + 1, i0 + 1) * tx
    return top * (1 - ty) + bot * ty


def pyproj_shift_m(lon, lat):
    """The same offset through pyproj + PROJ's us_noaa_vertconc.tif, or None (no pyproj, no
    grid file, outside the grid)."""
    try:
        from pyproj import Transformer
        t = Transformer.from_pipeline(
            "+proj=pipeline +step +proj=unitconvert +xy_in=deg +xy_out=rad "
            "+step +proj=vgridshift +grids=us_noaa_vertconc.tif +multiplier=1 "
            "+step +proj=unitconvert +xy_in=rad +xy_out=deg")
        _x, _y, z = t.transform(float(lon), float(lat), 0.0)
        z = float(z)
        return z if math.isfinite(z) and abs(z) < 5.0 else None
    except Exception:                                    # noqa - optional path
        return None


def shift_m(lon, lat, use_pyproj=True):
    """(NAVD88 - NGVD29 in m, method) at lon / lat (WGS 84 / NAD 83 degrees), or (None, why)."""
    s = grid_shift_m(lon, lat)
    if s is not None:
        return s, METHOD_GRID
    if use_pyproj:
        s = pyproj_shift_m(lon, lat)
        if s is not None:
            return s, METHOD_PYPROJ
    return None, ("outside the bundled North Dakota VERTCON grid and no pyproj / "
                  "us_noaa_vertconc.tif to fall back on")


def resolve(stated="", override=AUTO):
    """(source datum, how): the override when it is NAVD88 / NGVD29 / local; else the stated datum
    when recognised; else (NGVD29, "assumed")."""
    o = mode(override)
    if o != AUTO:
        return o, "override"
    c = classify(stated)
    if c:
        return c, "stated"
    return NGVD29, "assumed"


def is_local_elev(value_ft):
    """1.18.2: True for an elevation (ft) between LOCAL_MIN_FT and LOCAL_MAX_FT (0 - 500 ft,
    inclusive): a local datum, never VERTCON-shifted. False for None / text / NaN."""
    try:
        v = float(value_ft)
    except (TypeError, ValueError):
        return False
    return math.isfinite(v) and LOCAL_MIN_FT <= v <= LOCAL_MAX_FT


def local_band_hit(values):
    """(key, value) of the value that makes a dam's elevations local under the 0 - 500 ft rule:
    the top of dam ("top_of_dam_ft") when given, else the first pool in values (normal / PMF /
    trigger pool). None when none is in the band. values: {key: ft}."""
    values = values or {}
    if values.get("top_of_dam_ft") is not None:
        v = values["top_of_dam_ft"]
        return ("top_of_dam_ft", float(v)) if is_local_elev(v) else None
    for k in ("normal_pool_ft", "pmf_max_wse_ft", "trigger_wse_ft"):
        if values.get(k) is not None and is_local_elev(values[k]):
            return k, float(values[k])
    return None


def plan(lon, lat, stated="", override=AUTO, use_pyproj=True, what="the dam elevations",
         values=None):
    """The conversion record for one dam (see the module text). Nothing is converted here;
    convert_ft() applies it to each value and lists it in rec["fields"].
    values (1.18.2): {key: ft} of the run's elevations; a top of dam (or, with no top, a pool)
    between 0 and 500 ft makes the record local whatever the stated / chosen datum."""
    src, how = resolve(stated, override)
    rec = {"target": TARGET, "source_datum": src, "how": how, "stated": str(stated or ""),
           "override": mode(override), "lon": _r(lon, 6), "lat": _r(lat, 6),
           "shift_m": 0.0, "shift_ft": 0.0, "method": METHOD_NONE, "applied": False,
           "fields": {}, "warnings": []}
    hit = local_band_hit(values)
    if hit is not None:
        # 1.18.2 (Nick 2026-10-29: "Forget overrid datum. Convert local to navd 88."): the band
        # makes the record local whatever the stated / chosen datum - no "override" wording;
        # the local values are converted to NAVD88 by the DEM tie (Runner vdatum_run.apply_local).
        # No split (Nick 2026-10-29): the datum choice is not applied to any value of a local
        # record; every elevation is tied by the DEM offset.
        k, v = hit
        why = "the {} {:.2f} ft is {}".format(k.replace("_ft", "").replace("_", " "), v,
                                              LOCAL_BAND_WHY.format(LOCAL_MIN_FT, LOCAL_MAX_FT))
        # a datum stated as local stays "local (stated)" whatever ELEV_DATUM says (the choice
        # is not applied to a local record), not "local (assumed) (DETECTED ...)"
        if src != LOCAL and classify(stated) == LOCAL:
            src_l, how_l = LOCAL, "stated"
        elif src == LOCAL:
            src_l, how_l = LOCAL, how
        else:
            src_l, how_l = LOCAL_ASSUMED, "detected"
        rec.update(source_datum=src_l, how=how_l, method=METHOD_DEM, local=True,
                   local_rule="0-500 ft", local_reason=why, chosen_datum=src,
                   chosen_how=how)
        msg = ("The datum of {} is local ({}): they are converted to NAVD88 by the DEM - the "
               "top of dam is taken from the DEM at mid-crest and the other local elevations "
               "are DEM-derived (they keep their height below it); never VERTCON-shifted."
               .format(what, why))
        rec["warnings"].append(msg)
        rec["_pre_tie_warning"] = msg   # replaced by the tie's own warning (no near-duplicate)
        rec["tie"] = TIE_PENDING
        return rec
    if src == NAVD88:
        rec["method"] = "none (NAVD88 {}: used as given)".format(
            "set by the engineer" if how == "override" else "as stated")
        return rec
    if src == LOCAL:
        rec["method"] = METHOD_DEM
        rec["local"] = True
        msg = ("The datum of {} is local ({}): they are not datum-converted; the top of dam is "
               "taken from the DEM and the other elevations keep their height below it.".format(
                   what, "set by the engineer" if how == "override" else repr(stated)))
        rec["warnings"].append(msg)
        rec["_pre_tie_warning"] = msg
        rec["tie"] = TIE_PENDING
        return rec
    s, method = shift_m(lon, lat, use_pyproj)
    if s is None:
        rec["method"] = "none ({})".format(method)
        rec["warnings"].append(
            "{} {} NGVD29 but could not be converted to NAVD88 ({}); they are used as given, "
            "which may put them about 1-2 ft low against the DEM.".format(
                what[0].upper() + what[1:], "were taken as" if how == "assumed" else "are",
                method))
        return rec
    rec.update(shift_m=round(s, 4), shift_ft=round(s / FT, 3), method=method, applied=True)
    rec["_shift_m"] = s
    if how == "assumed":
        rec["warnings"].append(
            "The datum of {} is not stated{}: NGVD29 was assumed and they were converted to "
            "NAVD88 ({:+.2f} ft, VERTCON at {:.5f}, {:.5f}). Set the source datum to NAVD88 if "
            "they already are.".format(what, " ({!r})".format(stated) if stated else "",
                                       s / FT, float(lat), float(lon)))
    return rec


def local_check(value_ft, dem_crest_ft=None, shift_ft=0.0, what="top of dam"):
    """(is_local, reason) for one elevation (ft, as given) against ND's range and, when given,
    the DEM crest near the dam (ft NAVD88). shift_ft: the NGVD29 shift the value would get."""
    try:
        v = float(value_ft)
    except (TypeError, ValueError):
        return False, ""
    if not math.isfinite(v):
        return False, ""
    if is_local_elev(v):              # 1.18.2: first and unconditional
        return True, "the {} {:.2f} ft is {}".format(what, v, LOCAL_BAND_WHY.format(
            LOCAL_MIN_FT, LOCAL_MAX_FT))
    if v < ND_MIN_FT or v > ND_MAX_FT:
        return True, ("the {} {:.2f} ft is outside North Dakota's range of ground elevations "
                      "(about 750 - 3,510 ft; checked {:.0f} - {:.0f} ft)".format(
                          what, v, ND_MIN_FT, ND_MAX_FT))
    try:
        c = float(dem_crest_ft) if dem_crest_ft is not None else None
    except (TypeError, ValueError):
        c = None
    if c is None or not math.isfinite(c):
        return False, ""
    d = v + float(shift_ft or 0.0) - c
    if abs(d) > DEM_TOL_FT:
        return True, ("the {} {:.2f} ft is {:+.1f} ft from the DEM crest near the dam "
                      "({:.2f} ft NAVD88; more than {:.0f} ft)".format(what, v, d, c,
                                                                       DEM_TOL_FT))
    if abs(v / ROUND_STEP_FT - round(v / ROUND_STEP_FT)) < 1e-9 and abs(d) > ROUND_TOL_FT:
        return True, ("the {} {:.2f} ft is a round value {:+.1f} ft from the DEM crest near "
                      "the dam ({:.2f} ft NAVD88; more than {:.0f} ft)".format(
                          what, v, d, c, ROUND_TOL_FT))
    return False, ""


def convert_ft(value_ft, rec, field=None):
    """value_ft on NAVD88 under `rec` (plan()); None stays None. With `field` the value is
    listed in rec["fields"] (a field listed already is NOT converted again: the value
    recorded then is returned)."""
    if value_ft is None or rec is None:
        return value_ft
    try:
        v = float(value_ft)
    except (TypeError, ValueError):
        return value_ft
    if field and field in rec.get("fields", {}):
        return rec["fields"][field]["_navd88"]
    band = is_local_elev(v)           # 1.18.2: 0 - 500 ft is local - never shifted
    out = v + float(rec.get("_shift_m", rec.get("shift_m", 0.0))) / FT \
        if rec.get("applied") and not band else v
    if field:
        e = {"source": round(v, 3), "navd88": round(out, 3), "_navd88": out}
        if band:
            e["local_elev"] = True
        rec.setdefault("fields", {})[field] = e
    return out


def convert_m(value_m, rec, field=None):
    """The same for a value in metres."""
    if value_m is None or rec is None:
        return value_m
    ft = convert_ft(float(value_m) / FT, rec, field)
    return ft * FT


def public(rec):
    """The record without its private keys (for JSON files)."""
    if not rec:
        return rec
    out = {k: v for k, v in rec.items() if not k.startswith("_")}
    out["fields"] = {k: {a: b for a, b in v.items() if not a.startswith("_")}
                     for k, v in (rec.get("fields") or {}).items()}
    return out


def summary_lines(rec):
    """Plain-text lines (no bullets) for a report or summary."""
    if not rec:
        return []
    lines = [_source_line(rec)]
    tied = rec.get("local_offset_ft") is not None
    # a local record before apply_local has run (the Runner logs it once before the tie):
    # its values are not converted yet - no "100.00 local -> 100.00 NAVD88 (DEM-derived)"
    pending = bool(rec.get("local")) and rec.get("tie") == TIE_PENDING and not rec.get("review")
    not_applied = bool(rec.get("local")) and str(rec.get("tie") or "").startswith("not applied")
    if rec.get("review") and tied:
        lines.append("REVIEW: {} - those values are UNCONVERTED and not used; check the datum "
                     "before using this run.".format(rec.get("review_reason")))
    elif rec.get("review"):
        lines.append("REVIEW: {} - the local values are UNCONVERTED (no DEM tie); check the "
                     "datum before using this run.".format(rec.get("review_reason") or
                                                           "no DEM tie"))
    if rec.get("local_rule"):
        lines.append("Local datum rule: {}.".format(rec.get("local_reason") or rec["local_rule"]))
    if pending:
        lines.append("Local datum: to be converted to NAVD88 by the DEM tie once the DEM crest "
                     "is read (top of dam = DEM crest, the others + the offset).")
    elif not_applied:
        lines.append("Local datum: NOT converted - the DEM tie was {}.".format(rec["tie"]))
    elif rec.get("local") and rec.get("review") and not tied:
        lines.append("Local datum: NOT converted (flagged for review).")
    elif rec.get("local"):
        lines.append("Local datum: not converted; elevations taken from the DEM ({}{}).".format(
            rec.get("dem_crest_from") or "DEM crest",
            "; DEM top - local top = {:+.2f} ft".format(rec["local_offset_ft"])
            if rec.get("local_offset_ft") is not None else ""))
    elif rec.get("applied"):
        lines.append("Shift NGVD29 -> NAVD88: {:+.3f} ft ({:+.4f} m) at lat {}, lon {}; method: "
                     "{}.".format(rec.get("shift_ft", 0.0), rec.get("shift_m", 0.0),
                                  rec.get("lat"), rec.get("lon"), rec.get("method")))
    else:
        lines.append("No shift applied; method: {}.".format(rec.get("method")))
    for k, v in sorted((rec.get("fields") or {}).items()):
        if v.get("unconverted"):
            lines.append("{}: {:.2f} local -> UNCONVERTED ({})".format(
                k, v["source"], v.get("note") or "flagged for review"))
        elif pending:
            lines.append("{}: {:.2f} local -> waiting for the DEM tie".format(k, v["source"]))
        elif not_applied:
            lines.append("{}: {:.2f} local (as given, not converted)".format(k, v["source"]))
        elif (rec.get("local") or v.get("local_elev")) and v.get("navd88") is None:
            lines.append("{}: {:.2f} local -> not used ({})".format(
                k, v["source"], v.get("note") or "no local top to place it"))
        elif rec.get("local"):
            lines.append("{}: {:.2f} local -> {:.2f} NAVD88 ({}{})".format(
                k, v["source"], v["navd88"], v.get("how") or "DEM-derived",
                "; offset {:+.2f} ft".format(rec["local_offset_ft"])
                if rec.get("local_offset_ft") is not None and k != "top_of_dam_ft" else ""))
        elif v.get("dem_derived") and v.get("navd88") is not None:
            # Runner 3.12.5 (Nick 2026-10-29): a 0-500 ft value with a real top of dam,
            # replaced by a DEM elevation (no local crest to tie it by)
            lines.append("{}: {:.2f} (0-500 ft, not used) -> {:.2f} NAVD88 ({})".format(
                k, v["source"], v["navd88"], v.get("how") or "DEM-derived"))
        elif v.get("local_elev"):
            lines.append("{}: {:.2f} local (0-500 ft) -> {} ({})".format(
                k, v["source"], "{:.2f}".format(v["navd88"]) if v.get("navd88") is not None
                else "not used", v.get("note") or "not datum-converted"))
        elif rec.get("applied"):
            lines.append("{}: {:.2f} {} -> {:.2f} NAVD88".format(k, v["source"],
                                                                 rec.get("source_datum"),
                                                                 v["navd88"]))
        else:
            lines.append("{}: {:.2f} (as given)".format(k, v["source"]))
    for e in rec.get("structures_local") or []:
        lines.append("{}: local inverts {} ft -> {} ft NAVD88 ({}; offset {}).".format(
            e.get("name"), " / ".join("n/a" if x is None else "{:.2f}".format(x)
                                      for x in e.get("source_ft") or []),
            " / ".join("n/a" if x is None else "{:.2f}".format(x) for x in e.get("navd88_ft") or []),
            e.get("method"), "n/a" if e.get("offset_ft") is None else
            "{:+.2f} ft".format(e["offset_ft"])))
    if rec.get("dam_point"):
        from . import midcrest          # 1.18.2: the mid-crest dam point (one wording)
        lines.append(midcrest.summary_line(rec["dam_point"]))
    for w in rec.get("warnings") or []:
        lines.append("WARNING: " + w)
    return lines


def _source_line(rec):
    """'Source datum: ...' - one line, no repeat of the local rule's reason (its own line)."""
    h = rec.get("how")
    stated = rec.get("stated")
    if h == "detected":
        ch = rec.get("chosen_datum")
        extra = []
        if stated:
            extra.append("stated {!r}".format(stated))
        if rec.get("chosen_how") == "override" and ch:
            extra.append("ELEV_DATUM {} not applied to a local record".format(ch))
        how = "detected from the values{}{}".format(
            " by the 0 - 500 ft rule" if rec.get("local_rule") else "",
            "; " + "; ".join(extra) if extra else "")
        if not rec.get("local_rule") and rec.get("local_reason"):
            how += ": " + str(rec["local_reason"])
        return "Source datum: {} ({}); target NAVD88 (the DEM's).".format(
            rec.get("source_datum"), how)
    how = {"stated": "stated", "assumed": "NOT STATED - assumed",
           "override": "set by the engineer"}.get(h, h or "")
    if h == "stated" and rec.get("chosen_how") == "override" and \
            rec.get("chosen_datum") not in (None, LOCAL):
        how += "; ELEV_DATUM {} not applied to a local record".format(rec["chosen_datum"])
    return "Source datum: {} ({}{}); target NAVD88 (the DEM's).".format(
        rec.get("source_datum"), how,
        ", given as {!r}".format(stated) if stated else "")


def _r(v, n):
    try:
        return round(float(v), n)
    except (TypeError, ValueError):
        return None
