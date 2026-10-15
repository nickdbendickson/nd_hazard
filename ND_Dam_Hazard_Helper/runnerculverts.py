# -*- coding: utf-8 -*-
"""v2.10 (dam suite item 8), extended in 2.14: culvert / bridge lines for ND Dam Breach Runner.

Bare-earth LiDAR shows a road or rail embankment as a wall across the stream;
the Runner's "Culverts, bridges and notches" layer (lines, UPSTREAM END FIRST,
fields matched by name) opens it. This module writes that layer from the data
ND Dam Hazard already downloads and from NDDOT's own inventories:

  structures  National Bridge Inventory records (NBI_bridges: bridges and
              culverts with a span of 20 ft or more), OpenStreetMap crossings
              (OSM_crossings: culverts, fords / low-water crossings, railroad
              bridges) and, when given, NDDOT layers: the bridge inventory
              (Bridge_Inventory_External: bridges and the large culverts, with
              a culvert description "DOUBLE, 10X7X88' RCB" that is parsed for the
              barrels, span, rise, barrel length and shape) and the centerline
              pipes (Center_Line_Pipes: every pipe under a state highway, with
              its diameter or span x rise, length and the elevation of each end);
              other NDDOT-like layers are read by field names that give the unit;
  streams     USGS 3DHP flowlines (digitised in the flow direction - the same
              assumption traveltime.trace makes) or the engineer's centerline;
  embankments road lines (ND GIS Hub NG9-1-1 centerlines ...) and rail lines.

Placement, per structure point P (all in EPSG:5070 metres):
 1. every place within SEARCH_M of P where a stream line crosses a road / rail
    line (segment intersection); the nearest wins, a line of the kind the
    record belongs to first (railroad bridge -> rail; NBI item 42A = 2 -> rail;
    other NBI / NDDOT records and culverts -> road, then the other kind);
 2. without one, and only when ALLOW_NO_EMBANKMENT is set: the stream point
    nearest to P (the embankment direction is then unknown: the line is not
    lengthened for skew);
 3. otherwise the structure is listed as not placed, with the reason.
The line follows the stream through that point, upstream end first: the
stream's digitised direction is taken as the flow direction (NHD), or the
reverse when STREAM_REVERSED is set. Its length along the stream is the
inventory's barrel length + 2 x MARGIN_M when the record has one (NDDOT; the
barrel already runs toe to toe), else (embankment top width + 2 x MARGIN_M) /
sin(crossing angle), the angle limited to 30 degrees (at most twice the square
length); where the stream line ends first, the line goes on straight along its
end segment. The embankment top width is the NBI deck width (item 52, or the
roadway width, item 51; NDDOT ROADWIDTH_ENG) when the record has one and
USE_NBI_WIDTH is set, else the engineer's ROAD_WIDTH_M / RAIL_WIDTH_M. MARGIN_M
stands for the side slopes. All of these are SCREENING PARAMETERS chosen by the
engineer; the defaults are placeholders, not standards.

Fields (the names ND Dam Breach Runner matches: core/structures.py and
algorithms.STRUCT_FIELD_SYNONYMS - kind, span_ft, rise_ft, barrels, shape,
invert_up, invert_dn, name; units feet):
  kind       culvert / bridge / notch (NBI item 43B = 19 and NDDOT design "Culvert"
             are culverts - the same rule as facilities.read_crossings; a ford or
             low-water crossing is written as a notch: the road dips to the stream,
             there is no opening to model)
  span_ft    NBI item 48 (metres, converted); NDDOT: the description's span or the
             pipe diameter / arch span; an NDDOT bridge: structure length / spans
             (average, noted); empty when unknown (the Runner then uses its own default)
  rise_ft    NDDOT only (the description's rise, the pipe diameter, the arch rise);
             EMPTY from the NBI and OSM
  barrels    culvert: the NDDOT description's count ("DOUBLE"), NBI item 45 (main-unit
             spans; the NBI coding guide counts a culvert's cells there - verify), the
             pipe description ("DOUBLE ROW OF ..."); bridge: items 45 + 46 / NDDOT
             TOTAL_SPANS, all spans, so the Runner's cut (span x barrels x 1.1) is
             about the bridge opening; empty when unknown
  shape      box / pipe / arch from NDDOT materials (RCB = box, SPP / CMP = pipe,
             SPPA / "ARCH" = arch; the Runner models an arch as a box of that span and
             rise); EMPTY from the NBI and OSM (the Runner then assumes box)
  invert_up, invert_dn  NDDOT centerline pipes only: the two end elevations (ft) as the
             inventory states them. NDDOT's left / right are as seen looking from the lower
             station number to the higher; that direction at the crossing comes from the
             neighbouring pipes' route measures (else HWY_DIR), and the side on which the
             line's upstream end lies is the inlet (an inlet below the outlet is noted).
             Their datum is not stated, so the Runner checks them against the LiDAR bed
             before use (its S_INVERT_TOL_FT: within tolerance = used, both ends off alike
             = a datum shift corrected, otherwise the LiDAR inverts). PASS_ELEVATIONS off
             leaves them EMPTY (the Runner reads the DEM); NBI and OSM have none.
  name       source, number and "carried over crosses"
plus information fields the Runner ignores: source, src_id, carried, crosses,
nbi_kind (43A), nbi_design (43B / NDDOT design or pipe description), n_spans,
struct_len_ft (49 / LENGTH_ENG, along the road), deck_w_ft (52 / 51 / ROADWIDTH_ENG),
barrel_len_ft (NDDOT, along the stream), material, inv_text (the inventory text as
written), elev_left_ft and elev_right_ft (NDDOT, as written), inlet_side (left / right /
equal: which NDDOT side is the inlet; "(by elevation)" when no direction was known), end_section, emb_width_ft and emb_width_src,
line_len_ft and line_len_src, match_m (distance from the structure's point to the placed
crossing), angle_deg, embankment (road / rail / none), emb_name, stream, stream_dir,
also_in (other records merged into this line), notes.
No field name is one the Runner would misread ("width", "height", "type", "id" ...
are not used). Values that are not known are left EMPTY - never guessed; sizes read
from free text are checked for plausibility and dropped, with a note, when absurd.

Output: a GeoPackage (EPSG:5070) with layer "runner_culverts" (lines, give it
to the Runner as "Culverts, bridges and notches"), layer
"culverts_not_placed" (the structure points that could not be placed, with
the reason) and table "runner_culverts_info"; plus a CSV of the structures
not placed. Pure Python (suitegpkg); nothing has run in a real QGIS, and the
NDDOT readers were written against the two inventories' field names and
values as exported in 2026 - not against NDDOT's documentation.
"""

import csv
import datetime
import json
import math
import os
import re

from . import suitegpkg as G
from . import constants as C

M_FT = 1.0 / 0.3048
LAYER = "runner_culverts"
NOT_PLACED = "culverts_not_placed"
INFO_TABLE = "runner_culverts_info"

# engineer-chosen screening parameters (defaults are placeholders, not standards)
DEFAULTS = {"search_m": 150.0,          # structure point to the stream / road crossing
            "road_width_m": 10.0,       # embankment top width of a road (about 33 ft)
            "rail_width_m": 6.0,        # embankment top width of a railroad (about 20 ft)
            "margin_m": 10.0,           # added on EACH side for the side slopes
            "use_nbi_width": True,      # NBI deck / roadway width instead of road_width_m
            "allow_no_embankment": False,
            "stream_reversed": False,   # streams digitised upstream (not NHD)
            "dedupe_m": 30.0,           # records placed this close together = one crossing
            "min_angle_deg": 30.0,
            "pass_elevations": True}    # 2.14: NDDOT end elevations written as the inverts

FIELDS = [("name", "text"), ("kind", "text"), ("span_ft", "real"), ("rise_ft", "real"),
          ("barrels", "int"), ("shape", "text"), ("invert_up", "real"), ("invert_dn", "real"),
          ("source", "text"), ("src_id", "text"), ("carried", "text"), ("crosses", "text"),
          ("nbi_kind", "text"), ("nbi_design", "text"), ("n_spans", "int"),
          ("struct_len_ft", "real"), ("deck_w_ft", "real"), ("barrel_len_ft", "real"),
          ("material", "text"), ("inv_text", "text"), ("elev_left_ft", "real"),
          ("elev_right_ft", "real"), ("inlet_side", "text"), ("end_section", "text"),
          ("emb_width_ft", "real"), ("emb_width_src", "text"), ("line_len_ft", "real"),
          ("line_len_src", "text"), ("match_m", "real"), ("angle_deg", "real"),
          ("embankment", "text"), ("emb_name", "text"), ("stream", "text"),
          ("stream_dir", "text"), ("also_in", "text"), ("notes", "text")]
NOT_PLACED_FIELDS = [("name", "text"), ("kind", "text"), ("source", "text"), ("src_id", "text"),
                     ("reason", "text")]
# the Runner's own field synonyms (nd_breach_runner/algorithms.py, STRUCT_FIELD_SYNONYMS);
# kept here so a test can check that no information field is taken for one of them
RUNNER_FIELDS = {"S_KIND": "kind", "S_WIDTH": "span_ft", "S_HEIGHT": "rise_ft",
                 "S_BARRELS": "barrels", "S_SHAPE": "shape", "S_INV_UP": "invert_up",
                 "S_INV_DN": "invert_dn", "S_NAME": "name"}

KIND_CULVERT, KIND_BRIDGE, KIND_NOTCH = "culvert", "bridge", "notch"
SOURCE_RANK = {"NBI": 0, "NDDOT": 1, "OSM": 2}
# NBI item 42B (service under): waterway codes
NBI_WATERWAY = ("5", "6", "7", "8")


# ------------------------------------------------------------------ records --
def _low(props):
    return {str(k).lower(): v for k, v in (props or {}).items()}


def _txt(v):
    if v is None:
        return ""
    s = str(v).strip().strip("'").strip()
    return "" if s.upper() == "NULL" else s


def _num(v):
    try:
        x = float(str(v).strip()) if isinstance(v, str) else float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _pos(v):
    x = _num(v)
    return x if x is not None and x > 0 else None


def nbi_record(props, xy, source="NBI"):
    """A structure record from National Bridge Inventory fields (NTAD names such as
    STRUCTURE_TYPE_043B, MAX_SPAN_LEN_MT_048 - metres), or (None, reason)."""
    p = _low(props)
    no = _txt(p.get("structure_number_008"))
    design = _txt(p.get("structure_type_043b")).lstrip("0")
    kind43a = _txt(p.get("structure_kind_043a"))
    under = _txt(p.get("service_und_042b"))
    on = _txt(p.get("service_on_042a"))
    carried = _txt(p.get("facility_carried_007"))
    crosses = _txt(p.get("features_desc_006a"))
    kind = KIND_CULVERT if design == "19" else KIND_BRIDGE
    rec = {"x": xy[0], "y": xy[1], "source": source, "src_id": no, "kind": kind,
           "carried": carried, "crosses": crosses, "nbi_kind": kind43a, "nbi_design": design,
           "prefer": "rail" if on == "2" else "road", "notes": []}
    rec["name"] = "{} {}{}".format(source, no or "?", " ({} over {})".format(
        carried or "?", crosses or "?") if (carried or crosses) else "")
    if under and under not in NBI_WATERWAY:
        return None, "{}: not over a waterway (NBI item 42B = {})".format(rec["name"], under)
    span = _pos(p.get("max_span_len_mt_048"))
    main, appr = _pos(p.get("main_unit_spans_045")), _pos(p.get("appr_spans_046"))
    n_spans = int(round((main or 0) + (appr or 0))) or None
    rec["span_ft"] = round(span * M_FT, 2) if span else None
    rec["n_spans"] = n_spans
    if kind == KIND_CULVERT:
        rec["barrels"] = int(round(main)) if main else None
    else:
        rec["barrels"] = n_spans
    ln = _pos(p.get("structure_len_mt_049"))
    rec["struct_len_ft"] = round(ln * M_FT, 2) if ln else None
    deck = _pos(p.get("deck_width_mt_052"))
    road = _pos(p.get("roadway_width_mt_051"))
    rec["deck_w_ft"] = round((deck or road) * M_FT, 2) if (deck or road) else None
    rec["deck_w_src"] = "NBI item 52 (deck width)" if deck else \
        "NBI item 51 (roadway width)" if road else ""
    return rec, ""


def osm_record(props, xy):
    """A structure record from an OpenStreetMap crossing (osm_parse.crossing_features)."""
    p = _low(props)
    cat = _txt(p.get("category"))
    c = cat.lower()
    if c.startswith("culvert"):
        kind, prefer = KIND_CULVERT, "road"
    elif "ford" in c or "low-water" in c or "low water" in c:
        kind, prefer = KIND_NOTCH, "road"
    elif "bridge" in c:
        kind, prefer = KIND_BRIDGE, "rail" if "rail" in c else "road"
    else:
        return None, "OSM {}: category '{}' is not a culvert, ford or bridge".format(
            _txt(p.get("osm_id")) or "?", cat)
    oid = _txt(p.get("osm_id"))
    nm = _txt(p.get("name"))
    rec = {"x": xy[0], "y": xy[1], "source": "OSM", "src_id": oid, "kind": kind,
           "name": "OSM {} {}{}".format(oid or "?", cat.lower(), " ({})".format(nm) if nm else ""),
           "carried": _txt(p.get("railway")), "crosses": _txt(p.get("waterway")),
           "nbi_kind": "", "nbi_design": "", "prefer": prefer, "span_ft": None,
           "n_spans": None, "barrels": None, "struct_len_ft": None, "deck_w_ft": None,
           "deck_w_src": "", "notes": []}
    if kind == KIND_NOTCH:
        rec["notes"].append("ford / low-water crossing: written as a notch (no opening "
                            "through an embankment is expected)")
    return rec, ""


_UNIT_SUFFIX = (("_ft", 1.0), ("_feet", 1.0), ("_mt", M_FT), ("_m", M_FT))


def _dimension(p, stems):
    """A dimension in feet from a field whose name states its unit (span_ft, span_m ...)."""
    for stem in stems:
        for suf, k in _UNIT_SUFFIX:
            v = _pos(p.get(stem + suf))
            if v is not None:
                return round(v * k, 2), stem + suf
    return None, ""


def nddot_record(props, xy):
    """An NDDOT record, by the layer's field names: the NDDOT bridge inventory
    (BRIDGE_ID, DESIGNMAINDESC, CULVERT ...), the NDDOT centerline pipes (DIAMETER_E,
    LEFT_ELEV_E ...), NBI item fields, or any other layer with a kind text and dimension
    fields whose names give the unit (span_ft, span_m ...)."""
    p = _low(props)
    if "structure_number_008" in p or "structure_type_043b" in p:
        return nbi_record(props, xy, source="NDDOT")
    if _is_nddot_bridge(p):
        return nddot_bridge_record(props, xy)
    if _is_nddot_pipe(p):
        return nddot_pipe_record(props, xy)
    return nddot_generic_record(props, xy)


# ---- the NDDOT bridge inventory (Bridge_Inventory_External) and centerline pipes (2.14) --
#
# Bridge_Inventory_External (points, one per structure): BRIDGE_ID, FACILITY (carried),
# FEATURE_INTERSECTED (crossed), SERVTYPUNDDESC ("5 Waterway" ...), SERVTYPONDESC
# ("2 Railroad" ...), TOTAL_SPANS, DESIGNMAINDESC ("Culvert" or a bridge design), LENGTH_ENG
# (ft, along the road: NBI item 49), ROADWIDTH_ENG (ft; 0 for culverts), CULVERT (free text
# such as "DOUBLE, 10X7X88' RCB": count, span x rise x barrel length, material). No elevations.
#
# Center_Line_Pipes (points on the route centerline): ROUTE_ID, ROUTE_MEASURE, HIGHWAY,
# HWY_SUFFIX, HWY_DIR, REFERENCE_PT, OFFSET_E, SEQUENCE, PIPE_DESC ("REINFORCED CONCRETE
# PIPE", "DOUBLE ROW OF CONCRETE PIPE", "... PIPE ARCH", "CATTLE PASS" ...), DIAMETER_E (in;
# arch: span), ARCH_E (in; arch / cattle pass: rise), LENGTH_E (ft, the pipe), LEFT_ELEV_E and
# RIGHT_ELEV_E (ft, the two ends; 0 or 99999.9 = not recorded), END_SECTION_DESC, LOC_ERROR.
# Units are taken as the NDDOT field names say; a "diameter" below 12 is read as feet
# (cattle passes are recorded that way). The datum of the elevations is not stated: ND Dam
# Breach Runner compares them with the LiDAR bed before using them (its S_INVERT_TOL_FT).
NDDOT_WATERWAY = ("5", "6", "7", "8", "9")       # SERVTYPUNDDESC codes: waterway under
ELEV_MIN_FT, ELEV_MAX_FT = 500.0, 4000.0        # North Dakota lies between about 750 and 3,510 ft
_COUNT_WORDS = {"SINGLE": 1, "ONE": 1, "DOUBLE": 2, "DOUBLES": 2, "DBL": 2, "TWO": 2, "TWIN": 2,
                "TRIPLE": 3, "TRIPPLE": 3, "TRPL": 3, "TPL": 3, "THREE": 3, "QUAD": 4,
                "QUADRUPLE": 4, "QUARD": 4, "FOUR": 4, "QUINT": 5, "QUINTUPLE": 5, "PENTA": 5,
                "FIVE": 5, "SIX": 6, "SEXTUPLE": 6, "SEPTUPLE": 7, "SEVEN": 7, "EIGHT": 8,
                "NINE": 9, "TEN": 10}
_ARCH_WORDS = ("SPPA", "CMPA", "CSPA", "CMPAS", "RCPA", "RCAP", "ESPP", "ARCH", "ELLIP",
               "CON-SPAN", "CONSPAN", "CON/SPAN")
_BOX_WORDS = ("RCB", "RCBC", "PCRCB", "PRCB", "PRBC", "PCBC", "PCRBC", "RBC", "PRCBC", "RPCB",
              "BOX", "PCB", "PRECAST", "PRECASE", "PRECASR", "PREACST", "PREC", "CIP", "RCBS")
_PIPE_WORDS = ("SPP", "CSP", "CMP", "SMP", "SSP", "RCP", "PIPE", "MULTIPLATE", "MULTI-PLATE",
               "STEEL CULVERT", "CMP'S", "PRCSP")
# a size: 10  10'  10FT.  12.5  66"  71IN.  10-3 (ft-in)  9'6"  8'-7"  9FT.6IN.  97-7'
_MEASURE = (r"(?P<a>\d+(?:\.\d+)?)"
            r"(?:\s*(?P<au>'|FT\.?|\"|IN\.?)(?:\s*-?\s*(?P<b>\d{1,2})\s*(?P<bu>\"|IN\.?)(?!\d))?"
            r"|-(?P<b2>\d{1,2})(?!\d)(?P<bu2>'|\")?)?")
_SEP = r"\s*X\s*(?:'\s*)?"
_SIZE_FT = (1.0, 60.0)                          # plausible span / rise
_LEN_FT = (5.0, 600.0)                          # plausible barrel length


def _norm_text(s):
    s = _txt(s).upper().replace("&#39;", "'").replace("’", "'").replace("‘", "'")
    s = s.replace("“", '"').replace("”", '"').replace("''", "'")
    return " ".join(s.split())


def _measure_ft(m):
    """Feet from a _MEASURE match: the unit on the first number (' or FT = feet, " or IN =
    inches, none = feet), a second number = inches."""
    a = float(m.group("a"))
    au = (m.group("au") or "").replace(".", "")
    if au in ('"', "IN"):
        return None if m.group("b") else a / 12.0
    b = m.group("b") or m.group("b2")
    return a + (float(b) / 12.0 if b else 0.0)


def _measures(text, start=0):
    """The first run of two or three sizes joined by X in text[start:]:
    (values in ft, the unit written on each first number, end index) or ([], [], start)."""
    mre, sep = re.compile(_MEASURE), re.compile(_SEP)
    pos = start
    while pos < len(text):
        m = mre.search(text, pos)
        if not m:
            break
        if m.start() > start and (text[m.start() - 1].isdigit() or text[m.start() - 1] in ".-"):
            pos = m.end() if m.end() > pos else pos + 1
            continue
        vals, units, end = [_measure_ft(m)], [m.group("au") or ""], m.end()
        while len(vals) < 3:
            sm = sep.match(text, end)
            m2 = mre.match(text, sm.end()) if sm else None
            if not m2:
                break
            vals.append(_measure_ft(m2))
            units.append(m2.group("au") or "")
            end = m2.end()
        if len(vals) >= 2:
            return vals, units, end
        pos = end if end > pos else pos + 1
    return [], [], start


def _count_prefix(s):
    """(barrels or None, index where the sizes may start) from the start of a description:
    "DOUBLE, ", "DBL", "(2)-", "3-81\"", "2-10'x", "5 73\"", "3 DOUBLES" / "3-DOUBLE" (= 6)."""
    n, at = None, 0
    m = re.match(r"^\((\d{1,2})\)\s*-?\s*", s) or \
        re.match(r"^(\d{1,2})\s*-\s*(?=[A-Z]|\d+(?:\.\d+)?\s*(?:'|\"|FT|IN))", s) or \
        re.match(r"^(\d{1,2})\s+(?=\d)", s)
    if m is None:
        m = re.match(r"^(\d{1,2})\s+(?=[A-Z]+)", s)
        if m and re.match(r"[A-Z]+", s[m.end():]).group(0) not in _COUNT_WORDS:
            m = None                                         # "12 X 14 X 56": a size, not a count
    if m:
        n, at = int(m.group(1)), m.end()
    words = list(re.finditer(r"[A-Z]+", s[at:at + 24]))
    for w in words[:2]:
        if w.group(0) in _COUNT_WORDS:
            k = _COUNT_WORDS[w.group(0)]
            n = n * k if n else k
            at = at + w.end()
            break
    bm = re.search(r"([A-Z]+)-BARREL", s)
    if bm and bm.group(1) in _COUNT_WORDS:
        n = _COUNT_WORDS[bm.group(1)]
    return n, at


def _shape_words(s):
    if any(w in s for w in _ARCH_WORDS):
        return "arch"
    if any(re.search(r"\b{}\b".format(re.escape(w)), s) for w in _BOX_WORDS) or "CATTLE" in s:
        return "box"
    if any(re.search(r"\b{}\b".format(re.escape(w)), s) for w in _PIPE_WORDS):
        return "pipe"
    return None


def _material(s):
    known = set(_ARCH_WORDS) | set(_BOX_WORDS) | set(_PIPE_WORDS) | {
        "ALUMINUM", "ALUM", "STEEL", "TIMBER", "MC", "CONCRETE"}
    for w in re.findall(r"[A-Z][A-Z/'.-]*[A-Z]", s):
        ww = w.strip(".'")
        if ww in ("PRECAST", "PRECASE", "PRECASR", "PREACST", "CIP", "PC", "PREC"):
            continue
        if ww in known:
            return ww
    return ""


def parse_culvert_text(text):
    """The NDDOT CULVERT description ("DOUBLE, 10X7X88' RCB", "SINGLE, 10-6X39' SPP",
    "TRIPLE 66\" X 40' CMPA" ...) as a dict: barrels, span_ft, rise_ft, barrel_len_ft, shape
    (box / pipe / arch), material (the code as written), notes (list), raw. What the text does
    not state stays None; when several openings or extensions are described the first size is
    read and a note says so. Sizes are checked for plausibility (span and rise 1-60 ft, barrel
    length 5-600 ft) and left empty otherwise, with a note."""
    raw = _txt(text)
    out = {"barrels": None, "span_ft": None, "rise_ft": None, "barrel_len_ft": None,
           "shape": None, "material": "", "notes": [], "raw": raw}
    s = _norm_text(raw)
    if not s or s == "-":
        return out
    out["barrels"], at = _count_prefix(s)
    vals, units, _end = _measures(s, at)
    shape = _shape_words(s)
    out["material"] = _material(s)
    span = rise = length = None
    if len(vals) == 3 and None not in vals:
        span, rise, length = vals
    elif len(vals) == 2 and None not in vals:
        a, b = vals
        both_in = all(u.replace(".", "") in ('"', "IN") for u in units)
        if shape == "box" or both_in or (shape == "arch" and b < 3.0 * a and b <= _SIZE_FT[1]):
            span, rise = a, b                                # span x rise, no length
        else:
            span, length = a, b                              # diameter x length
            if shape == "arch":
                out["notes"].append("one size given for an arch / ellipse: written as a round "
                                    "pipe of that diameter")
            shape = "pipe" if shape in (None, "arch") else shape
    elif vals:
        out["notes"].append("size not read from '{}'".format(raw))
    if shape in ("arch", "pipe") and span is not None and rise is not None and \
            span > _SIZE_FT[1] and rise > 24.0 and not units[0] and not units[1]:
        span, rise = span / 12.0, rise / 12.0               # 117X79 CSPA: inches without the mark
        out["notes"].append("sizes above 60 read as inches")
    if span is not None and not (_SIZE_FT[0] <= span <= _SIZE_FT[1]):
        out["notes"].append("span {:g} ft in '{}' not plausible: left empty".format(span, raw))
        span = None
    if rise is not None and not (_SIZE_FT[0] <= rise <= _SIZE_FT[1]):
        out["notes"].append("rise {:g} ft in '{}' not plausible: left empty".format(rise, raw))
        rise = None
    if length is not None and not (_LEN_FT[0] <= length <= _LEN_FT[1]):
        out["notes"].append("length {:g} ft in '{}' not plausible: left empty".format(length, raw))
        length = None
    if shape == "pipe" and span is not None and rise is not None and abs(rise - span) > 0.01:
        shape = "arch"                                       # two sizes: not a round pipe
        out["notes"].append("two sizes for a pipe: written as an arch (span x rise)")
    if shape == "arch" and span is not None and rise is not None and rise > span:
        span, rise = rise, span                              # an arch is wider than high
        out["notes"].append("arch sizes given rise-first: swapped")
    if shape == "pipe" and span is not None and rise is None:
        rise = span
    out["shape"] = shape
    out["span_ft"] = round(span, 2) if span is not None else None
    out["rise_ft"] = round(rise, 2) if rise is not None else None
    out["barrel_len_ft"] = round(length, 1) if length is not None else None
    if re.search(r"\s(&|AND|\+|TO)\s", " " + s + " ") or re.search(r"\bEXT", s) or \
            re.search(r"WITH\s+(?:DOUBLE|SINGLE|TRIPLE|\d)", s):
        out["notes"].append("several openings or extensions described in '{}': the first size "
                            "is used - verify".format(raw))
    return out


def _is_nddot_bridge(p):
    return "bridge_id" in p and ("designmaindesc" in p or "culvert" in p or "servtypunddesc" in p)


def _is_nddot_pipe(p):
    return "diameter_e" in p and ("left_elev_e" in p or "pipe_desc" in p or "route_measure" in p)


def nddot_bridge_record(props, xy):
    """A record from NDDOT's Bridge_Inventory_External (field names above)."""
    p = _low(props)
    bid = _txt(p.get("bridge_id"))
    carried = _txt(p.get("facility"))
    crosses = _txt(p.get("feature_intersected"))
    under = _txt(p.get("servtypunddesc"))
    on = _txt(p.get("servtypondesc"))
    design = _txt(p.get("designmaindesc"))
    ctext = _txt(p.get("culvert"))
    kind = KIND_CULVERT if ("culvert" in design.lower() or (not design and ctext)) else KIND_BRIDGE
    name = "NDDOT {}{}".format(bid or "?", " ({} over {})".format(carried or "?", crosses or "?")
                                if (carried or crosses) else "")
    rec = {"x": xy[0], "y": xy[1], "source": "NDDOT", "src_id": bid, "kind": kind, "name": name,
           "carried": carried, "crosses": crosses, "nbi_kind": "", "nbi_design": design,
           "prefer": "rail" if on.split(" ")[0] == "2" else "road", "span_ft": None,
           "rise_ft": None, "shape": None, "barrels": None, "n_spans": None,
           "struct_len_ft": None, "barrel_len_ft": None, "deck_w_ft": None, "deck_w_src": "",
           "material": "", "inv_text": ctext, "notes": []}
    code = under.split(" ")[0]
    if code and code not in NDDOT_WATERWAY:
        return None, "{}: not over a waterway (NDDOT service under = {})".format(name, under)
    spans = _pos(p.get("total_spans"))
    n_spans = int(round(spans)) if spans else None
    rec["n_spans"] = n_spans
    ln = _pos(p.get("length_eng"))
    rec["struct_len_ft"] = round(ln, 1) if ln else None
    road = _pos(p.get("roadwidth_eng"))
    if road:
        rec["deck_w_ft"], rec["deck_w_src"] = round(road, 1), "NDDOT ROADWIDTH_ENG (roadway width)"
    if kind == KIND_CULVERT:
        c = parse_culvert_text(ctext)
        rec["span_ft"], rec["rise_ft"] = c["span_ft"], c["rise_ft"]
        rec["barrel_len_ft"], rec["shape"], rec["material"] = c["barrel_len_ft"], c["shape"], c["material"]
        rec["notes"] += c["notes"]
        if c["barrels"]:
            rec["barrels"] = c["barrels"]
            if n_spans and n_spans != c["barrels"]:
                rec["notes"].append("NDDOT total spans {} differs from the description ({} barrels): "
                                    "the description is used".format(n_spans, c["barrels"]))
        elif n_spans:
            rec["barrels"] = n_spans
            rec["notes"].append("barrels = NDDOT total spans (the description gives no count)")
        if not ctext:
            rec["notes"].append("no culvert description in the inventory: size unknown")
        elif c["span_ft"] is None and not any("not read" in n or "not plausible" in n for n in c["notes"]):
            rec["notes"].append("size not read from '{}'".format(ctext))
    else:
        rec["barrels"] = n_spans
        if ln and n_spans:
            rec["span_ft"] = round(ln / n_spans, 1)
            rec["notes"].append("span = structure length {:g} ft / {} spans (average)".format(ln, n_spans))
    return rec, ""


def _elev_ft(v):
    """An NDDOT end elevation (ft): 0, 99999.9 and anything outside North Dakota's range are
    'not recorded'."""
    x = _num(v)
    if x is None or not (ELEV_MIN_FT <= x <= ELEV_MAX_FT):
        return None
    return round(x, 2)


def nddot_pipe_record(props, xy):
    """A record from NDDOT's Center_Line_Pipes (field names above). The two end elevations
    are kept as elev_left_ft / elev_right_ft; place() turns them into inverts."""
    p = _low(props)
    desc = _txt(p.get("pipe_desc"))
    du = desc.upper()
    hwy = _txt(p.get("highway"))
    if hwy.endswith(".0"):
        hwy = hwy[:-2]
    suffix = _txt(p.get("hwy_suffix"))
    hdir = _txt(p.get("hwy_dir")).upper()[:1]
    rp = _num(p.get("reference_pt"))
    off = _num(p.get("offset_e"))
    rid = _num(p.get("route_id"))
    meas = _num(p.get("route_measure"))
    seq = _num(p.get("sequence"))
    sid = "{:g}@{:.3f}".format(rid, meas) if (rid is not None and meas is not None) else \
        _txt(p.get("objectid") or p.get("fid"))
    if seq and seq > 1:
        sid += "/{:g}".format(seq)
    where = "hwy {}{}{}".format(hwy or "?", suffix, " " + hdir if hdir else "")
    if rp is not None:
        where += " RP {:g}{}".format(rp, "+{:.2f}".format(off) if off is not None else "")
    size = _pos(p.get("diameter_e"))
    rise_in = _pos(p.get("arch_e"))
    notes = []
    if size is not None and size < 12.0:
        notes.append("size {:g} read as feet (below 12)".format(size))
        span = size
        rise = rise_in if (rise_in is not None and rise_in < 12.0) else (rise_in / 12.0 if rise_in else None)
    else:
        span = size / 12.0 if size else None
        rise = rise_in / 12.0 if rise_in else None
    if "ARCH" in du or "ELLIP" in du:
        shape = "arch"
        if span is not None and rise is not None and rise > span:
            span, rise = rise, span
            notes.append("arch span / rise given rise-first: swapped")
        if rise is None:
            notes.append("arch without a rise: written as a round pipe")
            shape, rise = "pipe", span
    elif "CATTLE" in du:
        shape = "box"
        notes.append("cattle pass: width x height from DIAMETER_E x ARCH_E")
    elif "PIPE" in du or "R.C.P" in du or "C.M.P" in du or "S.P.P" in du or not du:
        shape = "pipe"
        rise = span
    else:
        shape = None
    if rise is not None and span is not None and shape == "pipe" and abs(rise - span) > 1e-9:
        rise = span
    barrels = 1
    for w in du.replace(".", " ").split():
        if w in _COUNT_WORDS:
            barrels = _COUNT_WORDS[w]
            break
    m = re.search(r"(\w+)\s+ROW", du)
    if m and m.group(1) in _COUNT_WORDS:
        barrels = _COUNT_WORDS[m.group(1)]
    length = _pos(p.get("length_e"))
    if length is not None and length < 5.0:
        notes.append("pipe length {:g} ft not plausible: left empty".format(length))
        length = None
    left, right = _elev_ft(p.get("left_elev_e")), _elev_ft(p.get("right_elev_e"))
    if (p.get("left_elev_e") is not None and left is None) or (p.get("right_elev_e") is not None and right is None):
        notes.append("an end elevation is not recorded (0 / 99999.9 / outside ND)")
    if left is not None and right is not None and abs(left - right) > 20.0:
        notes.append("the two end elevations differ by {:.1f} ft: suspect".format(abs(left - right)))
    loc = _txt(p.get("loc_error")).upper()
    if loc and loc != "NO ERROR":
        notes.append("NDDOT location flag '{}': the point may be misplaced".format(loc.title()))
    size_txt = ""
    if span is not None:
        size_txt = "{:g} in".format(round(span * 12)) if shape == "pipe" else \
            "{:g} x {:g} in".format(round(span * 12), round((rise or 0) * 12))
    rec = {"x": xy[0], "y": xy[1], "source": "NDDOT", "src_id": sid, "kind": KIND_CULVERT,
           "name": "NDDOT pipe {} ({}{})".format(where, desc.title() if desc else "pipe",
                                                  " " + size_txt if size_txt else ""),
           "carried": where, "crosses": "", "nbi_kind": "", "nbi_design": desc, "prefer": "road",
           "span_ft": round(span, 2) if span is not None else None,
           "rise_ft": round(rise, 2) if rise is not None else None, "shape": shape,
           "barrels": barrels, "n_spans": None, "struct_len_ft": None,
           "barrel_len_ft": round(length, 1) if length else None, "deck_w_ft": None,
           "deck_w_src": "", "material": desc.title(), "inv_text": desc,
           "end_section": _txt(p.get("end_section_desc")).title(), "elev_left_ft": left,
           "elev_right_ft": right, "route_dir": hdir, "route_id": rid, "measure": meas,
           "notes": notes}
    return rec, ""


def nddot_generic_record(props, xy):
    """Any other NDDOT-like layer: a kind from a type / kind / description text and
    dimensions only from fields whose name gives the unit (span_ft, span_m ...)."""
    p = _low(props)
    text = " ".join(_txt(p.get(k)) for k in ("type", "kind", "structure_type", "struct_type",
                                             "category", "description", "descr", "str_type")
                    ).lower()
    sid = _txt(p.get("structure_number") or p.get("bridge_no") or p.get("bridge_number") or
               p.get("str_num") or p.get("id") or p.get("objectid"))
    if "culvert" in text or "pipe" in text or "box" in text:
        kind = KIND_CULVERT
    elif "bridge" in text:
        kind = KIND_BRIDGE
    else:
        return None, "NDDOT {}: kind (culvert / bridge) not found in the attributes".format(
            sid or "?")
    span, _s = _dimension(p, ("span", "max_span", "opening_width", "diameter"))
    rise, _r = _dimension(p, ("rise", "height"))
    length, _l = _dimension(p, ("length", "barrel_length", "pipe_length"))
    up, _u = _dimension(p, ("invert_up", "inv_up", "upstream_invert", "inlet_elev"))
    dn, _d = _dimension(p, ("invert_dn", "inv_dn", "downstream_invert", "outlet_elev"))
    barrels = _pos(p.get("barrels") or p.get("cells") or p.get("num_barrels"))
    deck, dsrc = _dimension(p, ("deck_width", "roadway_width", "road_width"))
    nm = _txt(p.get("name") or p.get("route") or p.get("facility_carried"))
    shape = "arch" if "arch" in text else "pipe" if ("pipe" in text or "cmp" in text) else \
        "box" if "box" in text else None
    return {"x": xy[0], "y": xy[1], "source": "NDDOT", "src_id": sid, "kind": kind,
            "name": "NDDOT {}{}".format(sid or "?", " ({})".format(nm) if nm else ""),
            "carried": nm, "crosses": _txt(p.get("crosses") or p.get("feature_crossed")),
            "nbi_kind": "", "nbi_design": "", "prefer": "road", "span_ft": span,
            "rise_ft": rise if rise is not None else (span if shape == "pipe" else None),
            "shape": shape, "barrel_len_ft": length, "invert_up_ft": up, "invert_dn_ft": dn,
            "n_spans": None, "barrels": int(round(barrels)) if barrels else None,
            "struct_len_ft": None, "deck_w_ft": deck, "deck_w_src": "NDDOT " + dsrc if deck else "",
            "material": "", "inv_text": text.strip(), "notes": []}, ""


# ------------------------------------------------------------------ placement --
class _Lines(object):
    """Polylines in metres with a segment grid: [{"parts", "name", "kind"}]."""

    def __init__(self, items, cell):
        self.items = [it for it in items if it.get("parts")]
        self.grid = G.SegmentGrid(cell)
        for k, it in enumerate(self.items):
            self.grid.add_line(k, it["parts"])

    def segments_near(self, x, y, radius):
        for k, pi, si in self.grid.near(x, y, radius):
            pts = self.items[k]["parts"][pi]
            a, b = pts[si], pts[si + 1]
            if G.seg_distance(x, y, a, b) <= radius:
                yield k, pi, si, a, b


def _chainage(pts, si, t):
    ch = G.line_length(pts[:si + 1])
    return ch + t * math.hypot(pts[si + 1][0] - pts[si][0], pts[si + 1][1] - pts[si][1])


def _cut(pts, ch0, ch1):
    """The polyline between chainages ch0 < ch1, continued straight along the end segments
    where it is too short. Returns (points, extended?)."""
    total = G.line_length(pts)
    ext = ch0 < 0 or ch1 > total
    out = []
    if ch0 < 0:
        (xa, ya), (xb, yb) = pts[0], pts[1]
        n = math.hypot(xb - xa, yb - ya) or 1.0
        out.append((xa - (xb - xa) / n * -ch0, ya - (yb - ya) / n * -ch0))
    out.append(G.point_at(pts, max(ch0, 0.0)))
    run = 0.0
    for i in range(len(pts) - 1):
        run += math.hypot(pts[i + 1][0] - pts[i][0], pts[i + 1][1] - pts[i][1])
        if max(ch0, 0.0) < run < min(ch1, total):
            out.append(tuple(pts[i + 1]))
    out.append(G.point_at(pts, min(ch1, total)))
    if ch1 > total:
        (xa, ya), (xb, yb) = pts[-2], pts[-1]
        n = math.hypot(xb - xa, yb - ya) or 1.0
        out.append((xb + (xb - xa) / n * (ch1 - total), yb + (yb - ya) / n * (ch1 - total)))
    clean = [out[0]]
    for p in out[1:]:
        if math.hypot(p[0] - clean[-1][0], p[1] - clean[-1][1]) > 1e-6:
            clean.append(p)
    return clean, ext


def _angle(a, b, c, d):
    """Angle in degrees (0-90) between segments a-b and c-d."""
    ux, uy = b[0] - a[0], b[1] - a[1]
    vx, vy = d[0] - c[0], d[1] - c[1]
    n = math.hypot(ux, uy) * math.hypot(vx, vy)
    if n <= 0:
        return 90.0
    cs = abs(ux * vx + uy * vy) / n
    return math.degrees(math.acos(max(-1.0, min(1.0, cs))))


def place(records, streams, roads=(), rails=(), params=None):
    """Place structure records on the streams. streams / roads / rails:
    [{"parts": [[(x, y), ...]], "name": str}] in metres (EPSG:5070).
    Returns (placed rows [(line geometry, attrs)], not placed [(point geometry, attrs)])."""
    P = dict(DEFAULTS)
    P.update(params or {})
    search = float(P["search_m"])
    cell = max(search, 50.0)
    S = _Lines(streams, cell)
    E = {"road": _Lines(roads or (), cell), "rail": _Lines(rails or (), cell)}
    placed, not_placed = [], []

    def fail(rec, reason):
        not_placed.append(({"type": "Point", "coordinates": (rec["x"], rec["y"])},
                           {"name": rec["name"], "kind": rec["kind"], "source": rec["source"],
                            "src_id": rec["src_id"], "reason": reason}))
    for rec in records:
        x, y = rec["x"], rec["y"]
        segs = list(S.segments_near(x, y, search))
        if not segs:
            fail(rec, "no stream line within {:g} m".format(search))
            continue
        hits = []
        for ek, lines in E.items():
            esegs = list(lines.segments_near(x, y, search))
            if not esegs:
                continue
            for k, pi, si, a, b in segs:
                for ek2, epi, esi, c, d in esegs:
                    hit = G.segment_intersection(a, b, c, d)
                    if hit is None:
                        continue
                    t, _u, (ix, iy) = hit
                    dd = math.hypot(ix - x, iy - y)
                    if dd <= search:
                        hits.append((0 if ek == rec["prefer"] else 1, dd, ek, k, pi, si, t,
                                     (ix, iy), _angle(a, b, c, d), lines.items[ek2]["name"],
                                     (c, d)))
        rec_notes = list(rec.get("notes") or [])
        road_seg = None
        if hits:
            hits.sort(key=lambda h: (h[0], h[1]))
            pref, dd, ek, k, pi, si, t, xy, ang, ename, road_seg = hits[0]
            if pref:
                rec_notes.append("no {} line crosses a stream here: placed at a {} "
                                 "crossing".format(rec["prefer"], ek))
        elif P["allow_no_embankment"]:
            best = None
            for k, pi, si, a, b in segs:
                ch, d, pxy, _i = G.project_on_line([a, b], x, y)
                seglen = math.hypot(b[0] - a[0], b[1] - a[1]) or 1.0
                if best is None or d < best[1]:
                    best = (k, d, pi, si, ch / seglen, pxy)
            k, dd, pi, si, t, xy = best
            ek, ang, ename = "none", None, ""
            rec_notes.append("no road / rail line crosses a stream within {:g} m: placed on the "
                             "stream point nearest the structure (embankment not seen)".format(search))
        else:
            fail(rec, "no road / rail line crosses a stream within {:g} m".format(search))
            continue
        # embankment top width
        if P["use_nbi_width"] and rec.get("deck_w_ft"):
            width_m, wsrc = rec["deck_w_ft"] / M_FT, rec.get("deck_w_src") or "record"
        elif ek == "rail":
            width_m, wsrc = float(P["rail_width_m"]), "rail default (engineer parameter)"
        else:
            width_m, wsrc = float(P["road_width_m"]), "road default (engineer parameter)"
        length = width_m + 2.0 * float(P["margin_m"])
        if ang is not None:
            length /= math.sin(math.radians(max(float(P["min_angle_deg"]), ang)))
        len_src = "(top width + 2 x margin) / sin(crossing angle)" if ang is not None else \
            "top width + 2 x margin (no embankment line)"
        if rec.get("barrel_len_ft"):
            # 2.14: the inventory's barrel length already runs toe to toe along the stream
            from_barrel = rec["barrel_len_ft"] / M_FT + 2.0 * float(P["margin_m"])
            if from_barrel >= length:
                length = from_barrel
                len_src = "inventory barrel length {:g} ft + 2 x margin".format(rec["barrel_len_ft"])
            else:
                rec_notes.append("inventory barrel length {:g} ft is shorter than the embankment "
                                 "crossing ({:.0f} ft): the longer is used".format(
                                     rec["barrel_len_ft"], length * M_FT))
        stream = S.items[k]
        pts = [tuple(p) for p in stream["parts"][pi]]
        ch = _chainage(pts, si, t)
        direction = "as digitised (NHD: downstream)"
        if P["stream_reversed"]:
            total = G.line_length(pts)
            pts = pts[::-1]
            ch = total - ch
            direction = "reversed (streams digitised upstream)"
        line, ext = _cut(pts, ch - length / 2.0, ch + length / 2.0)
        if ext:
            rec_notes.append("the stream line ends within the embankment: extended straight")
        inv_up, inv_dn, side = orient_elevations(rec, line, xy, road_seg, rec_notes)
        if not P.get("pass_elevations", True):
            inv_up = inv_dn = None
        attrs = {"name": rec["name"], "kind": rec["kind"], "span_ft": rec.get("span_ft"),
                 "rise_ft": rec.get("rise_ft"), "barrels": rec.get("barrels"),
                 "shape": rec.get("shape"), "invert_up": inv_up, "invert_dn": inv_dn,
                 "source": rec["source"], "src_id": rec["src_id"], "carried": rec.get("carried"),
                 "crosses": rec.get("crosses"), "nbi_kind": rec.get("nbi_kind"),
                 "nbi_design": rec.get("nbi_design"), "n_spans": rec.get("n_spans"),
                 "struct_len_ft": rec.get("struct_len_ft"), "deck_w_ft": rec.get("deck_w_ft"),
                 "barrel_len_ft": rec.get("barrel_len_ft"), "material": rec.get("material") or "",
                 "inv_text": rec.get("inv_text") or "", "elev_left_ft": rec.get("elev_left_ft"),
                 "elev_right_ft": rec.get("elev_right_ft"), "inlet_side": side,
                 "end_section": rec.get("end_section") or "",
                 "emb_width_ft": round(width_m * M_FT, 1), "emb_width_src": wsrc,
                 "line_len_ft": round(G.line_length(line) * M_FT, 1), "line_len_src": len_src,
                 "match_m": round(dd, 1), "angle_deg": round(ang, 1) if ang is not None else None,
                 "embankment": ek, "emb_name": ename, "stream": stream.get("name") or "",
                 "stream_dir": direction, "also_in": "", "notes": "; ".join(rec_notes)}
        placed.append(({"type": "LineString", "coordinates": line}, attrs,
                       {"xy": xy, "rank": _rank(rec), "point": (x, y)}))
    placed, dups = _dedupe(placed, float(P["dedupe_m"]))
    for pt, a, why in dups:
        not_placed.append(({"type": "Point", "coordinates": pt},
                           {"name": a["name"], "kind": a["kind"], "source": a["source"],
                            "src_id": a["src_id"], "reason": why}))
    return placed, not_placed


def _rank(rec):
    """Lower is better: more known dimensions, then NBI, NDDOT, OSM."""
    known = sum(1 for k in ("span_ft", "rise_ft", "barrels", "barrel_len_ft", "deck_w_ft",
                            "elev_left_ft", "elev_right_ft") if rec.get(k))
    return (-known, SOURCE_RANK.get(rec["source"], 9))


_CARDINAL = {"N": (0.0, 1.0), "E": (1.0, 0.0), "S": (0.0, -1.0), "W": (-1.0, 0.0)}
STATION_GAP_MAX = 5.0                         # route measure (miles) between neighbours to use


def station_directions(recs):
    """2.14: the direction of increasing station (route measure) at each NDDOT pipe, from its
    neighbours on the same route: the unit vector from the previous pipe (lower measure) to
    the next (higher measure), or from / to this one when only one neighbour exists; neighbours
    farther than STATION_GAP_MAX miles along the route are not used. Sets rec["station_dir"]
    (EPSG:5070) where it could be found."""
    routes = {}
    for r in recs:
        if r.get("route_id") is not None and r.get("measure") is not None:
            routes.setdefault(r["route_id"], []).append(r)
    for items in routes.values():
        items.sort(key=lambda r: r["measure"])
        for i, r in enumerate(items):
            prev = next((items[j] for j in range(i - 1, -1, -1)
                         if r["measure"] - items[j]["measure"] > 1e-6), None)
            nxt = next((items[j] for j in range(i + 1, len(items))
                        if items[j]["measure"] - r["measure"] > 1e-6), None)
            if prev is not None and r["measure"] - prev["measure"] > STATION_GAP_MAX:
                prev = None
            if nxt is not None and nxt["measure"] - r["measure"] > STATION_GAP_MAX:
                nxt = None
            a, b = prev or r, nxt or r
            if a is b:
                continue
            dx, dy = b["x"] - a["x"], b["y"] - a["y"]
            n = math.hypot(dx, dy)
            if n > 1.0:
                r["station_dir"] = (dx / n, dy / n)


def orient_elevations(rec, line, xy, road_seg, notes):
    """2.14: the record's two end elevations (elev_left_ft / elev_right_ft, or invert_up_ft /
    invert_dn_ft when a layer states them that way) as (invert_up, invert_dn, inlet_side) in
    feet for the placed line (upstream end first; xy = the crossing point on it).

    NDDOT's left and right are as seen looking from the lower station number to the higher.
    That direction at the crossing is the station direction read from the neighbouring pipes
    (station_directions), else HWY_DIR (W and S routes are the opposite carriageways of
    divided highways; their measures still increase east / north), turned onto the local road
    segment. The side on which the line's upstream end lies is the inlet: inlet_side = "left"
    / "right" ("equal" when both elevations are the same). An inlet below the outlet is noted
    (the stream line's flow direction or the elevations deserve a look). Without any direction
    the HIGHER elevation is taken as the inlet, inlet_side "... (by elevation)". Without
    elevations: (None, None, "")."""
    up, dn = rec.get("invert_up_ft"), rec.get("invert_dn_ft")
    if up is not None or dn is not None:
        return up, dn, "as given"
    left, right = rec.get("elev_left_ft"), rec.get("elev_right_ft")
    if left is None and right is None:
        return None, None, ""
    if left is None or right is None:
        v = left if left is not None else right
        notes.append("only one end elevation in the inventory: written for both ends")
        return v, v, "left" if left is not None else "right"
    if abs(left - right) < 1e-9:
        return left, right, "equal"
    geo_up = None
    ref = rec.get("station_dir")
    if ref is None:
        hdir = (rec.get("route_dir") or "").upper()[:1]
        if hdir in _CARDINAL:
            ref = _CARDINAL["N" if hdir == "S" else "E" if hdir == "W" else hdir]
    if ref is not None and len(line) >= 2:
        ax, ay = ref
        if road_seg is not None:
            (cx, cy), (dx_, dy_) = road_seg
            rx, ry = dx_ - cx, dy_ - cy
            n = math.hypot(rx, ry) or 1.0
            rx, ry = rx / n, ry / n
            dot = rx * ref[0] + ry * ref[1]
            # the matched road must run with the route; a route direction across it says the
            # pipe is not on this road (or the route turns here): no left / right then
            ax, ay = ((rx, ry) if dot > 0 else (-rx, -ry)) if abs(dot) >= 0.3 else (None, None)
        if ax is not None:
            ux, uy = line[0][0] - xy[0], line[0][1] - xy[1]
            cross = ax * uy - ay * ux
            if abs(cross) > 1e-6:
                geo_up = "left" if cross > 0 else "right"
    if geo_up is not None:
        up, dn = (left, right) if geo_up == "left" else (right, left)
        if up < dn - 1e-9:
            notes.append("the inventory's upstream end ({}) is {:.1f} ft lower than its downstream "
                         "end: check the stream line's flow direction and the elevations"
                         .format(geo_up, dn - up))
        return up, dn, geo_up
    side = "left" if left > right else "right"
    return max(left, right), min(left, right), side + " (by elevation)"


def _dedupe(placed, tol):
    """One line per crossing: records whose placed crossing points lie within `tol` of each
    other on the same embankment kind are one structure (the best-described record is kept;
    the others are named in also_in and listed as not placed)."""
    order = sorted(range(len(placed)), key=lambda i: (placed[i][2]["rank"], i))
    hosts, merged, dups = [], {}, []
    for i in order:
        g, a, meta = placed[i]
        host = next((h for h in hosts if placed[h][1]["embankment"] == a["embankment"] and
                     math.hypot(placed[h][2]["xy"][0] - meta["xy"][0],
                                placed[h][2]["xy"][1] - meta["xy"][1]) <= tol), None)
        if host is None:
            hosts.append(i)
            continue
        merged.setdefault(host, []).append(i)
        dups.append((meta["point"], a, "same crossing as {} (merged into its line)".format(
            placed[host][1]["name"])))
    for h, others in merged.items():
        ha = placed[h][1]
        ha["also_in"] = "; ".join(placed[i][1]["name"] for i in others)
        same = 1 + sum(1 for i in others if placed[i][1]["source"] == ha["source"])
        if same > 1:
            note = "{} {} records at this crossing (twin structures? verify the barrels)".format(
                same, ha["source"])
            ha["notes"] = "; ".join(x for x in (ha["notes"], note) if x)
    return [(placed[i][0], placed[i][1]) for i in sorted(hosts)], dups


# ------------------------------------------------------------------ inputs --
def structure_records(nbi=(), osm=(), nddot=()):
    """Records from [(feature, geometry in EPSG:5070)] lists; returns (records, rejected)
    where rejected = [(point geometry, attrs)] of the ones that are not structures over a
    stream."""
    recs, rejected = [], []
    for maker, feats, src in ((nbi_record, nbi, "NBI"), (osm_record, osm, "OSM"),
                              (nddot_record, nddot, "NDDOT")):
        for f, g in feats or ():
            xy = G.point_on_surface(g)
            if xy is None:
                continue
            rec, why = maker(f.get("properties") or {}, xy)
            if rec is None:
                rejected.append(({"type": "Point", "coordinates": xy},
                                 {"name": why.split(":")[0], "kind": "", "source": src,
                                  "src_id": "", "reason": why}))
            else:
                recs.append(rec)
    station_directions(recs)
    return recs, rejected


def line_items(feats, name_fields=("gnis_name", "name", "fullname", "full_name", "routename",
                                   "rrowner1", "subdiv"), skip=None):
    """[{"parts", "name"}] of line features [(feature, geometry)]; skip(props) -> True leaves
    a feature out."""
    out = []
    for f, g in feats or ():
        props = f.get("properties") or {}
        if skip is not None and skip(props):
            continue
        parts = [p for p in G.lines_of(g) if len(p) >= 2]
        if not parts:
            continue
        low = _low(props)
        name = next((_txt(low.get(n)) for n in name_fields if _txt(low.get(n))), "")
        out.append({"parts": parts, "name": name})
    return out


def abandoned_rail(props):
    """NARN lines that carry no trains (linear.NARN_NO_TRAFFIC) are no embankment to open."""
    from .linear import NARN_NO_TRAFFIC
    return _txt(_low(props).get("net")).upper() in NARN_NO_TRAFFIC


# ------------------------------------------------------------------ output --
def write(out_path, placed, not_placed, params=None, sources=None):
    """Write the GeoPackage (+ <name>_not_placed.csv). Returns (gpkg path, csv path)."""
    if not out_path.lower().endswith(".gpkg"):
        out_path += ".gpkg"
    w = G.GpkgWriter(out_path)
    try:
        lyr = w.layer(LAYER, "LINESTRING", FIELDS,
                      description="Culverts, bridges and notches for ND Dam Breach Runner "
                                  "(lines, upstream end first)")
        for g, a in placed:
            lyr.add(g, a)
        np_ = w.layer(NOT_PLACED, "POINT", NOT_PLACED_FIELDS,
                      description="structures that could not be placed, with the reason")
        for g, a in not_placed:
            np_.add(g, a)
        info = w.table(INFO_TABLE, [("key", "text"), ("value", "text")])
        P = dict(DEFAULTS)
        P.update(params or {})
        for k, v in (("created", datetime.datetime.now().isoformat(timespec="seconds")),
                     ("created_by", "ND Dam Hazard {} (runnerculverts.py)".format(
                         C.PLUGIN_VERSION)),
                     ("crs", "EPSG:5070"), ("placed", str(len(placed))),
                     ("not_placed", str(len(not_placed))),
                     ("parameters", json.dumps(P, sort_keys=True)),
                     ("sources", json.dumps(sources or {}, sort_keys=True)),
                     ("for", "ND Dam Breach Runner: 'Culverts, bridges and notches' (lines, "
                             "upstream end first); fields kind, span_ft, rise_ft, barrels, shape, "
                             "invert_up, invert_dn, name are matched by name"),
                     ("inverts", "invert_up / invert_dn (ft) are NDDOT end elevations as written "
                                 "(left / right looking from the lower station to the higher; the "
                                 "upstream side's is the inlet); datum unknown - the Runner checks "
                                 "them against the LiDAR bed (S_INVERT_TOL_FT) before using them")):
            info.add(None, {"key": k, "value": v})
        w.close()
    except BaseException:
        w.abort()
        raise
    csv_path = os.path.splitext(out_path)[0] + "_not_placed.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as fh:
        wr = csv.writer(fh)
        wr.writerow(["name", "kind", "source", "src_id", "reason", "x_5070", "y_5070", "lon",
                     "lat"])
        for g, a in not_placed:
            x, y = g["coordinates"][:2]
            lon, lat = G.from_5070(x, y)
            wr.writerow([a["name"], a["kind"], a["source"], a["src_id"], a["reason"],
                         "{:.1f}".format(x), "{:.1f}".format(y), "{:.6f}".format(lon),
                         "{:.6f}".format(lat)])
    return out_path, csv_path


def run(out_path, nbi=(), osm=(), nddot=(), streams=(), roads=(), rails=(), params=None,
        sources=None, log=None):
    """The whole job on [(feature, geometry in EPSG:5070)] lists. Returns a report dict."""
    log = log or (lambda m: None)
    recs, rejected = structure_records(nbi, osm, nddot)
    s_items = line_items(streams)
    r_items = line_items(roads)
    t_items = line_items(rails, skip=abandoned_rail)
    if not s_items:
        raise ValueError("no stream lines: give 3DHP flowlines or a centerline layer")
    if not r_items and not t_items and not (params or {}).get("allow_no_embankment"):
        raise ValueError("no road or rail lines: give them, or allow placement on the stream "
                         "alone")
    placed, not_placed = place(recs, s_items, r_items, t_items, params)
    not_placed = rejected + not_placed
    gpkg, csv_path = write(out_path, placed, not_placed, params, sources)
    by_kind = {}
    for _g, a in placed:
        by_kind[a["kind"]] = by_kind.get(a["kind"], 0) + 1
    lines = ["Runner culverts: {} line(s) placed ({}), {} structure(s) not placed (see {})".format(
        len(placed), ", ".join("{} {}".format(v, k) for k, v in sorted(by_kind.items())) or "-",
        len(not_placed), os.path.basename(csv_path)),
        "Structures read: {} NBI, {} OSM, {} NDDOT; streams {}, roads {}, rail lines {}".format(
            len(nbi or ()), len(osm or ()), len(nddot or ()), len(s_items), len(r_items),
            len(t_items))]
    reasons = {}
    for _g, a in not_placed:
        key = a["reason"].split(":")[-1].strip() if a["reason"].startswith(("NBI", "NDDOT", "OSM")) \
            else a["reason"]
        key = key if len(key) < 90 else key[:87] + "..."
        reasons[key] = reasons.get(key, 0) + 1
    lines += ["  not placed: {} x {}".format(v, k) for k, v in sorted(reasons.items(),
                                                                      key=lambda t: -t[1])]
    for ln in lines:
        log(ln)
    return {"path": gpkg, "csv": csv_path, "placed": len(placed), "not_placed": len(not_placed),
            "by_kind": by_kind, "lines": lines}


def from_source(out_path, source, extent=None, params=None, log=None, nddot=()):
    """The job on an offline data pack / downloads folder (suiteexport.open_source):
    NBI_bridges, OSM_crossings, 3DHP_flowlines, ND_roads_NG911 (else the newest HPMS year),
    NARN_rail_lines. nddot: [(feature, geometry)] of an NDDOT layer, when given."""
    from .suiteexport import projected, HPMS_SETS
    from .offline_pack import ND_BBOX
    extent = tuple(extent or ND_BBOX)
    got, sources = {}, {}
    for key, names in (("nbi", ("NBI_bridges",)), ("osm", ("OSM_crossings",)),
                       ("streams", ("3DHP_flowlines",)),
                       ("roads", ("ND_roads_NG911",) + HPMS_SETS),
                       ("rails", ("NARN_rail_lines",))):
        name = next((n for n in names if source.has(n)), None)
        if name is None:
            got[key] = []
            sources[key] = "not in the source"
            continue
        feats = source.features(name, extent) or []
        got[key] = projected(feats)
        sources[key] = "{} ({} features, {})".format(name, len(feats), source.note(name, extent))
    return run(out_path, got["nbi"], got["osm"], nddot, got["streams"], got["roads"],
               got["rails"], params, sources, log)
