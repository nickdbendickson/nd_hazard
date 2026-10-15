# -*- coding: utf-8 -*-
"""Reviewer export / import / merge of the engineer review decisions (v2.8).

Two engineers can review the same dam separately and merge their work:

  export(store, dest, reviewer)   a portable CSV of the decisions plus a small
                                  JSON header next to it (plugin version, dam,
                                  reviewer, date, CRS, row count, SHA-256 of
                                  the rows);
  import_plan(mine, theirs)       compares the other engineer's file with the
                                  decisions on this side WITHOUT writing
                                  anything: decisions to add, identical ones,
                                  CONFLICTS (different decisions for the same
                                  structure - both shown with reviewer and
                                  date) and removed-vs-kept differences; the
                                  header hash, dam and CRS are checked and a
                                  file for another dam is refused;
  apply_plan(store, plan, policy) carries out the plan: per conflict keep
                                  mine / take theirs / newest wins (or an
                                  explicit choice per conflict), after a backup
                                  copy of the decisions file, and writes
                                  Review_Merge_Report.csv (every row, action,
                                  both sides).

Decisions are matched by LOCATION (structure ids change between runs), the
same way review.ReviewStore matches them to structures: the nearest one
within MATCH_FT, one-to-one. Locations are compared as WGS 84 longitude /
latitude (converted from X / Y / CRS when a row has no Lon / Lat), so two
files made in different project CRSs still line up.

Two decisions are "identical" when category, ACER type, persons and note
agree (case / spacing ignored); reviewer and date do not count, and the
row on this side keeps its own reviewer and date. Everything that is NOT
touched by the plan (identical rows, rows only on this side, extra columns
added in Excel) is left exactly as it is.

Removals: a plain two-file comparison cannot tell "they deleted it" from
"I added it". Give `base=` (the decisions file / export both engineers
started from) and the plan can: a decision that is in the base and in one
file but not the other is a REMOVAL that needs a choice, and a change made
on one side only is applied without being a conflict (three-way merge).

The CSV is read tolerantly (UTF-8 with / without BOM, Windows ANSI,
UTF-16 "Unicode text", "," ";" or tab separators, a "sep=;" first line,
quoting, decimal commas, Excel's re-formatted dates, blank trailing rows).
Everything here is plain files and dicts (QGIS is only used to convert X / Y
to lon / lat when a row has no Lon / Lat), so it can be tested without QGIS.

Nothing here decides a hazard class: it only combines the engineers'
saved decisions.
"""

import csv
import datetime
import hashlib
import io
import json
import math
import os
import re
import time
from collections import Counter

from . import review as RV
from .constants import PLUGIN_NAME, PLUGIN_VERSION, OUTPUT_FOLDER_NAME
from .helpers import log_message, dam_display_name, timestamped_path, prepare_output_path
from .compat import MSG_WARNING

EXCHANGE_FORMAT = "ND_Review_Exchange"
EXCHANGE_VERSION = 1
BACKUP_PREFIX = "Structure_Review_Decisions_backup_"
REPORT_FILE = "Review_Merge_Report.csv"
# Struct_ID is left out: it is renumbered on every run and means nothing to the other engineer
EXCHANGE_FIELDS = ["Location_Key", "Lon", "Lat", "X", "Y", "CRS", "Category", "ACER_Type",
                   "Persons", "Note", "Reviewer", "Date", "Sources"]
_ALL_FIELDS = ["Location_Key", "Lon", "Lat", "X", "Y", "CRS", "Struct_ID", "Category",
               "ACER_Type", "Persons", "Note", "Reviewer", "Date", "Sources"]
HASH_FIELDS = ["Lon", "Lat", "X", "Y", "CRS", "Category", "ACER_Type", "Persons", "Note",
               "Reviewer", "Date"]
DECISION_FIELDS = ("Category", "ACER_Type", "Persons", "Note")

# a file whose decisions are all farther than this from every decision here is
# probably another dam (only checked when the header cannot say)
# SCREENING VALUE CHOSEN BY THE ENGINEER (default), not a regulatory number
MAX_CENTROID_KM = 20.0
_FT_PER_DEG_LAT = 364567.2

# plan item kinds
KIND_ADD = "add"                              # only in theirs
KIND_IDENTICAL = "identical"
KIND_CONFLICT = "conflict"                    # different decisions, same structure
KIND_ONLY_MINE = "only_mine"
KIND_THEIRS_REMOVED = "removed_by_theirs"     # (needs base) in base, kept here, gone there
KIND_MINE_REMOVED = "removed_by_mine"         # (needs base) in base, gone here, kept there
KIND_UPDATE = "update_from_theirs"            # (needs base) only they changed it
KIND_MINE_CHANGED = "changed_by_mine"         # (needs base) only I changed it
KIND_UNLOCATED = "unlocated"                  # a row that has no usable location
CHOICE_KINDS = (KIND_CONFLICT, KIND_THEIRS_REMOVED, KIND_MINE_REMOVED)

POLICY_MINE, POLICY_THEIRS, POLICY_NEWEST = "mine", "theirs", "newest"
POLICIES = (POLICY_MINE, POLICY_THEIRS, POLICY_NEWEST)

ACT_ADDED = "ADDED"
ACT_SKIPPED = "SKIPPED (identical)"
ACT_KEPT_MINE = "KEPT MINE"
ACT_TOOK_THEIRS = "TOOK THEIRS"
ACT_REMOVED = "REMOVED"
ACT_ONLY_MINE = "UNCHANGED (only mine)"
ACT_NOT_LOCATED = "NOT APPLIED (no location)"

REPORT_FIELDS = (["Action", "Kind", "Choice", "Location_Key", "Distance_ft", "Differing_Fields"]
                 + ["Mine_" + f for f in ("Category", "ACER_Type", "Persons", "Note",
                                          "Reviewer", "Date")]
                 + ["Theirs_" + f for f in ("Category", "ACER_Type", "Persons", "Note",
                                            "Reviewer", "Date")]
                 + ["Result_" + f for f in ("Category", "ACER_Type", "Persons", "Note",
                                            "Reviewer", "Date")]
                 + ["Reason"])

_ALIASES = {"lon": "Lon", "long": "Lon", "longitude": "Lon", "lat": "Lat", "latitude": "Lat",
            "x": "X", "y": "Y", "crs": "CRS", "structid": "Struct_ID",
            "category": "Category", "acertype": "ACER_Type", "persons": "Persons",
            "note": "Note", "notes": "Note", "reviewer": "Reviewer", "date": "Date",
            "sources": "Sources", "locationkey": "Location_Key"}


# ------------------------------------------------------------- normalising --
def _fold(text):
    return " ".join(str(text or "").split()).casefold()


def _fmt_num(value, decimals):
    """Number cell -> fixed-decimal text ("" when empty / not a number). A
    decimal comma (Excel with ';' as list separator) is read as a point."""
    t = "" if value is None else str(value).strip()
    if not t:
        return ""
    try:
        f = RV._num(t)
    except (TypeError, ValueError):
        return ""
    return "{:.{}f}".format(f, decimals) if math.isfinite(f) else ""


def _persons(value):
    t = "" if value is None else str(value).strip()
    if not t:
        return ""
    try:
        f = RV._num(t)
        return str(int(f)) if math.isfinite(f) else ""
    except (TypeError, ValueError):
        return ""


def norm_date(value):
    """ISO date text of a date cell. Excel turns 2026-01-02 into 1/2/2026
    (US month / day order is assumed for "/") or 02.01.2026 (day first for
    "."); anything else is returned as typed."""
    t = "" if value is None else str(value).strip()
    if not t:
        return ""
    m = re.match(r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})(?:[ T].*)?$", t)
    parts = None
    if m:
        parts = (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    else:
        m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{2,4})(?:\s.*)?$", t)
        if m:
            parts = (int(m.group(3)), int(m.group(1)), int(m.group(2)))
        else:
            m = re.match(r"^(\d{1,2})\.(\d{1,2})\.(\d{2,4})(?:\s.*)?$", t)
            if m:
                parts = (int(m.group(3)), int(m.group(2)), int(m.group(1)))
    if parts is None:
        return t
    y, mo, d = parts
    if y < 100:
        y += 2000
    try:
        return datetime.date(y, mo, d).isoformat()
    except ValueError:
        return t


def _norm_note(value):
    t = "" if value is None else str(value)
    return t.replace("\r\n", "\n").replace("\r", "\n").strip()


def norm_row(raw):
    """A decision row (from the store, an Excel copy ...) with every exchange
    column present and normalised: numbers in fixed decimals, ISO date,
    stripped text. Unknown columns are dropped."""
    g = raw.get if hasattr(raw, "get") else dict(raw).get
    out = {
        "Location_Key": str(g("Location_Key") or "").strip(),
        "Lon": _fmt_num(g("Lon"), 7), "Lat": _fmt_num(g("Lat"), 7),
        "X": _fmt_num(g("X"), 3), "Y": _fmt_num(g("Y"), 3),
        "CRS": str(g("CRS") or "").strip(),
        "Struct_ID": str(g("Struct_ID") or "").strip(),
        "Category": " ".join(str(g("Category") or "").split()),
        "ACER_Type": " ".join(str(g("ACER_Type") or "").split()),
        "Persons": _persons(g("Persons")),
        "Note": _norm_note(g("Note")),
        "Reviewer": " ".join(str(g("Reviewer") or "").split()),
        "Date": norm_date(g("Date")),
        "Sources": str(g("Sources") or "").strip()}
    return out


def _decision_sig(row):
    r = norm_row(row)
    return (_fold(r["Category"]), _fold(r["ACER_Type"]), r["Persons"],
            " ".join(r["Note"].split()))


def same_decision(a, b):
    """True when two rows carry the same decision (reviewer / date ignored)."""
    return _decision_sig(a) == _decision_sig(b)


def differing_fields(a, b):
    sa, sb = _decision_sig(a), _decision_sig(b)
    return [name for name, x, y in zip(DECISION_FIELDS, sa, sb) if x != y]


def rows_sha256(rows):
    """SHA-256 (hex) of the decision rows: order of the rows does not matter,
    spelling of numbers / dates / line ends does not either (a file re-saved
    in Excel keeps its hash)."""
    lines = []
    for row in rows:
        r = norm_row(row)
        lines.append("\x1f".join(r[f] for f in HASH_FIELDS))
    lines.sort()
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def _date_tuple(text):
    t = norm_date(text)
    try:
        return datetime.date.fromisoformat(t)
    except ValueError:
        return None


# ---------------------------------------------------------------- locations --
def location_key(lon, lat):
    return "{:.6f}_{:.6f}".format(lon, lat)


def _xy_to_lonlat(x, y, crs_id, ctx=None):
    try:
        from qgis.core import (QgsCoordinateReferenceSystem, QgsCoordinateTransform,
                               QgsPointXY, QgsProject)
        src = QgsCoordinateReferenceSystem(crs_id)
        if not src.isValid():
            return None
        xf = QgsCoordinateTransform(src, QgsCoordinateReferenceSystem("EPSG:4326"),
                                    ctx or QgsProject.instance().transformContext())
        p = QgsPointXY(x, y)
        p = p if xf.isShortCircuited() else xf.transform(p)
        return float(p.x()), float(p.y())
    except Exception:
        return None


def row_lonlat(row, ctx=None):
    """(lon, lat) of a decision row, from Lon / Lat or else converted from
    X / Y / CRS; None when the row cannot be located."""
    r = norm_row(row)
    try:
        if r["Lon"] and r["Lat"]:
            lon, lat = float(r["Lon"]), float(r["Lat"])
            if -180.0 <= lon <= 180.0 and -90.0 <= lat <= 90.0:
                return lon, lat
        if r["X"] and r["Y"] and r["CRS"]:
            return _xy_to_lonlat(float(r["X"]), float(r["Y"]), r["CRS"], ctx)
    except (TypeError, ValueError):
        pass
    return None


def _match_points(a, b, tol_ft):
    """One-to-one nearest matching of two lists of (idx, lon, lat) within
    tol_ft (closest pairs first). Returns {a idx: (b idx, distance ft)}."""
    if not a or not b:
        return {}
    lats = [p[2] for p in a] + [p[2] for p in b]
    lat0 = sum(lats) / len(lats)
    kx = math.cos(math.radians(lat0)) * _FT_PER_DEG_LAT

    def ft(p):
        return p[1] * kx, p[2] * _FT_PER_DEG_LAT
    cell = max(float(tol_ft), 1e-6)
    grid = {}
    for p in b:
        x, y = ft(p)
        grid.setdefault((int(math.floor(x / cell)), int(math.floor(y / cell))), []).append(
            (p[0], x, y))
    cands = []
    for p in a:
        x, y = ft(p)
        cx, cy = int(math.floor(x / cell)), int(math.floor(y / cell))
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for j, bx, by in grid.get((cx + dx, cy + dy), ()):
                    d = math.hypot(bx - x, by - y)
                    if d <= tol_ft:
                        cands.append((d, p[0], j))
    cands.sort()
    used_a, used_b, out = set(), set(), {}
    for d, i, j in cands:
        if i in used_a or j in used_b:
            continue
        used_a.add(i)
        used_b.add(j)
        out[i] = (j, d)
    return out


def _centroid_km(points_a, points_b):
    """Distance in km between the centroids of two lists of (idx, lon, lat)."""
    def cen(pts):
        return (sum(p[1] for p in pts) / len(pts), sum(p[2] for p in pts) / len(pts))
    (x1, y1), (x2, y2) = cen(points_a), cen(points_b)
    kx = math.cos(math.radians((y1 + y2) / 2.0))
    return math.hypot((x1 - x2) * kx, y1 - y2) * 111.12


# --------------------------------------------------------------- file I/O --
def _decode(data):
    """(text, encoding) of the bytes of a CSV as Excel may have saved it."""
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16"), "utf-16"
    try:
        return data.decode("utf-8-sig"), "utf-8"
    except UnicodeDecodeError:
        return data.decode("cp1252"), "cp1252"


def _delimiter(first_line):
    counts = {d: first_line.count(d) for d in (",", ";", "\t")}
    best = max(counts.values())
    if best == 0:
        return ","
    for d in (",", ";", "\t"):
        if counts[d] == best:
            return d
    return ","


def read_exchange(path):
    """(rows, info): the decision rows of an export / decisions file, each as
    norm_row() gives them, blank rows dropped. info = {"encoding",
    "delimiter", "blank_rows", "columns"}. Raises OSError (unreadable) or
    ValueError (columns not recognised)."""
    with open(path, "rb") as fh:
        data = fh.read()
    text, enc = _decode(data)
    body = text
    while body:                              # blank lines and Excel's "sep=;" hint first
        first, nl, rest = body.partition("\n")
        if first.strip(" \r;,\t") and not first.strip().lower().startswith("sep="):
            break
        body = rest
    if not body.strip():
        return [], {"encoding": enc, "delimiter": ",", "blank_rows": 0, "columns": []}
    delim = _delimiter(body.split("\n", 1)[0])
    reader = csv.reader(io.StringIO(body, newline=""), delimiter=delim)
    header = next(reader)
    names = [_ALIASES.get(re.sub(r"[^a-z0-9]", "", h.lower()), "") for h in header]
    have = set(n for n in names if n)
    if not ({"Lon", "Lat"} <= have or {"X", "Y", "CRS"} <= have):
        raise ValueError("unrecognised columns {} (expected {})".format(
            sorted(h.strip() for h in header if h.strip()), ", ".join(RV.FIELDS)))
    rows, blank = [], 0
    try:
        for rec in reader:
            raw = {}
            for n, v in zip(names, rec):
                if n and n not in raw:
                    raw[n] = v
            r = norm_row(raw)
            if not any(r[f] for f in ("Lon", "Lat", "X", "Y", "Category", "ACER_Type",
                                      "Persons", "Note", "Reviewer", "Date")):
                blank += 1
                continue
            rows.append(r)
    except csv.Error as e:
        raise ValueError("the file could not be read as CSV ({})".format(e))
    return rows, {"encoding": enc, "delimiter": delim, "blank_rows": blank,
                  "columns": sorted(have)}


def _write_atomic(path, data):
    """Write bytes to `path` through a temp file + os.replace. When the file
    is locked (open in Excel ...) the same content goes to a time-stamped
    name next to it. Returns the path written."""
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)

    def put(target):
        tmp = target + ".tmp"
        try:
            with open(tmp, "wb") as fh:
                fh.write(data)
        except OSError as e:
            raise RuntimeError("{} could not be written ({})".format(target, e)) from e
        try:
            os.replace(tmp, target)
        except PermissionError:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise
        return target
    try:
        return put(path)
    except PermissionError as e:
        alt = timestamped_path(path)
        log_message("Could not overwrite {} ({}); writing {} instead".format(
            os.path.basename(path), e, os.path.basename(alt)), MSG_WARNING)
        put(alt)
        try:                                   # same list prepare_output_path keeps
            from . import helpers as _H
            _H._REDIRECTED.append((path, alt))
        except Exception:
            pass
        return alt


def _csv_bytes(fields, rows):
    buf = io.StringIO(newline="")
    w = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow(r)
    return buf.getvalue().encode("utf-8-sig")            # BOM: Excel reads it as UTF-8


def header_path(csv_path):
    """The JSON header file that belongs to an exported CSV."""
    return os.path.splitext(csv_path)[0] + ".header.json"


def read_header(path):
    """The header dict of `path` (a header file, or the CSV it belongs to);
    None when there is none or it is unreadable."""
    for p in (path, header_path(path)):
        if p and os.path.isfile(p) and p.lower().endswith(".json"):
            try:
                with open(p, encoding="utf-8-sig") as fh:
                    data = json.load(fh)
                return data if isinstance(data, dict) else None
            except (OSError, ValueError):
                return None
    return None


# ------------------------------------------------------------------ export --
def _as_store(store_or_path):
    if hasattr(store_or_path, "rows"):
        return store_or_path
    return RV.ReviewStore(store_or_path)


def _dam_of(store, dam_name=None):
    if dam_name:
        return str(dam_name)
    path = getattr(store, "path", "") or ""
    folder = os.path.dirname(os.path.abspath(path)) if path else ""
    if folder and os.path.basename(folder).lower() == OUTPUT_FOLDER_NAME:
        return dam_display_name(os.path.dirname(folder))
    return ""


def _common_crs(rows):
    c = Counter(str(r.get("CRS") or "").strip() for r in rows)
    c.pop("", None)
    return c.most_common(1)[0][0] if c else ""


def _select(rows, subset):
    if subset is None:
        return list(rows)
    if callable(subset):
        return [r for r in rows if subset(norm_row(r))]
    if isinstance(subset, dict):
        names = subset.get("reviewer")
        names = {_fold(n) for n in ([names] if isinstance(names, str) else names or [])}
        cats = {_fold(c) for c in (subset.get("category") or subset.get("categories") or [])}
        since, until = _date_tuple(subset.get("since")), _date_tuple(subset.get("until"))
        out = []
        for r in rows:
            n = norm_row(r)
            d = _date_tuple(n["Date"])
            if names and _fold(n["Reviewer"]) not in names:
                continue
            if cats and _fold(n["Category"]) not in cats:
                continue
            if subset.get("exclude_confirmed") and _fold(n["Category"]) == \
                    _fold(RV.CONFIRMED):
                continue
            if since and (d is None or d < since):
                continue
            if until and (d is None or d > until):
                continue
            out.append(r)
        return out
    idx = {int(i) for i in subset}
    return [r for i, r in enumerate(rows) if i in idx]


def export(store_or_path, dest, reviewer, subset=None, dam_name=None, crs=None, when=None,
           ctx=None):
    """Write the decisions as a portable CSV at `dest` plus `<dest>.header.json`.

    `subset`: None (all), a function(row) -> bool (row as norm_row), a dict
    of filters {"reviewer": name or names, "since": "YYYY-MM-DD", "until",
    "category": [...], "exclude_confirmed": True}, or the row numbers to
    export. A row without a reviewer gets `reviewer` (the exporter) so that
    a conflict can name who decided. Returns {"csv", "header_path", "header",
    "rows", "redirected"}; when a file is locked its content goes to a
    time-stamped name (see "redirected")."""
    store = _as_store(store_or_path)
    if getattr(store, "error", None) is not None:
        raise RuntimeError("{} could not be read ({}) - nothing exported".format(
            store.path, store.error))
    when = when or time.localtime()
    chosen = _select(store.rows, subset)
    out = []
    for r in chosen:
        n = norm_row(r)
        ll = row_lonlat(n, ctx)
        n["Location_Key"] = location_key(*ll) if ll else ""
        if not n["Reviewer"]:
            n["Reviewer"] = str(reviewer or "")
        out.append(n)
    out.sort(key=lambda n: (n["Location_Key"], n["Category"], n["Date"]))
    header = {
        "format": EXCHANGE_FORMAT, "format_version": EXCHANGE_VERSION,
        "plugin": PLUGIN_NAME, "plugin_version": PLUGIN_VERSION,
        "dam": _dam_of(store, dam_name), "reviewer": str(reviewer or ""),
        "exported": time.strftime("%Y-%m-%d %H:%M", when),
        "crs": crs if crs is not None else _common_crs(out),
        "rows": len(out), "sha256": rows_sha256(out),
        "csv": os.path.basename(dest),
        "subset": "all decisions" if subset is None else "a subset of the decisions",
        "note": "Merge with review_exchange.import_plan(); the SHA-256 covers the decision "
                "rows (spelling of numbers / dates and row order do not matter)."}
    csv_path = _write_atomic(dest, _csv_bytes(EXCHANGE_FIELDS, out))
    header["csv"] = os.path.basename(csv_path)
    hdr_target = header_path(dest)
    hdr_path = _write_atomic(hdr_target, (json.dumps(header, indent=1) + "\n").encode("utf-8"))
    if csv_path != dest and hdr_path == hdr_target:      # keep the pair together
        hdr_path = _write_atomic(header_path(csv_path),
                                 (json.dumps(header, indent=1) + "\n").encode("utf-8"))
    return {"csv": csv_path, "header_path": hdr_path, "header": header, "rows": len(out),
            "redirected": [p for p, w in ((dest, csv_path), (hdr_target, hdr_path)) if p != w]}


# -------------------------------------------------------------------- plan --
class MergePlan(object):
    """What a merge would do. Nothing has been written when this exists."""

    def __init__(self):
        self.items = []
        self.issues = []              # (level "error" | "warning" | "info", text)
        # 2.32.1: the refusals an engineer may override, by the import_plan keyword
        # that overrides them ("allow_other_dam", "accept_modified") - the review tab
        # used to find them by the start of their text
        self.overridable = set()
        self.header = None
        self.theirs_path = ""
        self.theirs_rows = []
        self.dam = ""
        self.their_dam = ""
        self.crs = ""
        self.their_crs = ""
        self.tol_ft = RV.MATCH_FT
        self.used_base = False
        self.hash_ok = None           # True / False / None (no header)
        self.file_info = {}

    @property
    def refused(self):
        return any(level == "error" for level, _t in self.issues)

    def add_issue(self, level, text):
        self.issues.append((level, text))

    def counts(self):
        c = Counter(i["kind"] for i in self.items)
        return {k: c.get(k, 0) for k in (KIND_ADD, KIND_IDENTICAL, KIND_CONFLICT, KIND_ONLY_MINE,
                                         KIND_THEIRS_REMOVED, KIND_MINE_REMOVED, KIND_UPDATE,
                                         KIND_MINE_CHANGED, KIND_UNLOCATED)}

    def conflicts(self):
        """The items that need a choice (conflicts and removals)."""
        return [i for i in self.items if i["kind"] in CHOICE_KINDS]

    def item(self, key):
        """An item by its location key or its number."""
        for i in self.items:
            if i["key"] == key or i["id"] == key:
                return i
        return None


def _dam_norm(name):
    n = re.sub(r"[^a-z0-9 ]+", " ", (name or "").lower())
    n = re.sub(r"\b(dam|dams|the|no|number)\b", " ", n)
    return re.sub(r"\s+", " ", n).strip()


def describe_row(row):
    """One line for a dialog / log: category, type, persons, note, reviewer, date."""
    if row is None:
        return "(no decision)"
    r = norm_row(row)
    bits = [b for b in (r["Category"], r["ACER_Type"],
                        "{} persons".format(r["Persons"]) if r["Persons"] else "") if b]
    text = " / ".join(bits) or "(empty decision)"
    if r["Note"]:
        text += " - '{}'".format(r["Note"][:80])
    who = ", ".join(x for x in (r["Reviewer"], r["Date"]) if x)
    return text + (" ({})".format(who) if who else "")


def _newer(mine, theirs):
    dm, dt = _date_tuple(norm_row(mine)["Date"]), _date_tuple(norm_row(theirs)["Date"])
    if dm is None or dt is None or dm == dt:
        return "tie" if (dm is not None and dt is not None) else "unknown"
    return "theirs" if dt > dm else "mine"


def _located(rows, ctx):
    pts, none = [], []
    for i, r in enumerate(rows):
        ll = row_lonlat(r, ctx)
        if ll is None:
            none.append(i)
        else:
            pts.append((i, ll[0], ll[1]))
    return pts, none


def import_plan(mine, theirs_path, dam_name=None, crs=None, base=None, tol_ft=None,
                allow_other_dam=False, accept_modified=False, header=None, ctx=None):
    """Compare the other engineer's file with this side's decisions.

    mine          ReviewStore or path of Structure_Review_Decisions.csv;
    theirs_path   the exported CSV (its header is <name>.header.json next to
                  it; `header` overrides it: dict or path);
    dam_name      this dam (default: the name of the dam folder above
                  <dam>/outputs/ of the store's path); a header for another
                  dam refuses the plan unless allow_other_dam;
    crs           this side's project CRS (default: the CRS most rows use);
    base          the decisions both started from (store / path / export):
                  turns on removal detection and three-way merging;
    tol_ft        matching distance (default review.MATCH_FT);
    accept_modified   a file whose rows do not match the header hash (edited
                  after the export) is refused unless this is set.
    Returns a MergePlan (never raises for a bad file: see plan.refused and
    plan.issues)."""
    plan = MergePlan()
    plan.tol_ft = float(tol_ft) if tol_ft else RV.MATCH_FT
    plan.theirs_path = str(theirs_path)
    store = _as_store(mine)
    if getattr(store, "error", None) is not None:
        plan.add_issue("error", "your decisions file {} could not be read ({}) - fix or "
                       "rename it first".format(store.path, store.error))
        return plan
    plan.dam = _dam_of(store, dam_name)
    plan.crs = crs if crs is not None else _common_crs(store.rows)
    try:
        theirs_rows, info = read_exchange(theirs_path)
    except (OSError, ValueError) as e:
        plan.add_issue("error", "{} could not be read: {}".format(theirs_path, e))
        return plan
    plan.file_info = info
    plan.theirs_rows = theirs_rows
    if info.get("blank_rows"):
        plan.add_issue("info", "{} blank row(s) at the end / in the file ignored".format(
            info["blank_rows"]))

    # ---- header: hash, dam, CRS ------------------------------------------
    if isinstance(header, dict):
        hdr = header
    else:
        hdr = read_header(header) if header else read_header(str(theirs_path))
    plan.header = hdr
    if hdr is None:
        plan.add_issue("warning", "no header file ({}) - hash, dam and CRS could not be "
                       "verified".format(os.path.basename(header_path(str(theirs_path)))))
    else:
        if hdr.get("format") not in (None, EXCHANGE_FORMAT):
            plan.add_issue("error", "the header is not a review exchange header "
                           "(format '{}')".format(hdr.get("format")))
        want = str(hdr.get("sha256") or "")
        if want:
            plan.hash_ok = rows_sha256(theirs_rows) == want.lower()
            if not plan.hash_ok:
                why = "" if hdr.get("rows") in (None, len(theirs_rows)) else \
                    " ({} row(s) in the header, {} in the file)".format(hdr.get("rows"),
                                                                       len(theirs_rows))
                plan.add_issue("warning" if accept_modified else "error",
                               "the rows do not match the SHA-256 in the header - the file was "
                               "changed after it was exported{}".format(why))
                if not accept_modified:
                    plan.overridable.add("accept_modified")
        else:
            plan.add_issue("warning", "the header has no SHA-256 - rows could not be verified")
        plan.their_dam = str(hdr.get("dam") or "")
        if plan.dam and plan.their_dam and _dam_norm(plan.dam) != _dam_norm(plan.their_dam):
            plan.add_issue("warning" if allow_other_dam else "error",
                           "the file is for another dam ('{}', this is '{}')".format(
                               plan.their_dam, plan.dam))
            if not allow_other_dam:
                plan.overridable.add("allow_other_dam")
        elif not plan.dam or not plan.their_dam:
            plan.add_issue("info", "dam name not checked ({})".format(
                "unknown here" if not plan.dam else "not in the header"))
        plan.their_crs = str(hdr.get("crs") or "")
        if plan.crs and plan.their_crs and plan.crs != plan.their_crs:
            have_ll = all(r["Lon"] and r["Lat"] for r in theirs_rows) and \
                all(norm_row(r)["Lon"] and norm_row(r)["Lat"] for r in store.rows)
            if have_ll:
                plan.add_issue("warning", "CRS differs (theirs {}, yours {}); locations are "
                               "compared as longitude / latitude, X / Y of added rows are "
                               "kept in their CRS".format(plan.their_crs, plan.crs))
            else:
                plan.add_issue("error", "CRS differs (theirs {}, yours {}) and some rows have "
                               "no longitude / latitude - the locations cannot be compared"
                               .format(plan.their_crs, plan.crs))
    if plan.refused:
        return plan

    # ---- locations ---------------------------------------------------------
    mine_rows = list(store.rows)
    m_pts, m_none = _located(mine_rows, ctx)
    t_pts, t_none = _located(theirs_rows, ctx)
    if hdr is None and m_pts and t_pts:
        km = _centroid_km(m_pts, t_pts)
        near = _match_points(m_pts, t_pts, plan.tol_ft)
        if km > MAX_CENTROID_KM and not near:
            plan.add_issue("warning", "no decision of the file is anywhere near a decision "
                           "here ({:.0f} km apart) - is it for another dam?".format(km))
    base_rows, b_pts = [], []
    if base is not None:
        try:
            if hasattr(base, "rows"):
                base_rows = list(base.rows)
            elif os.path.isfile(str(base)):
                base_rows = read_exchange(str(base))[0]
            else:
                plan.add_issue("warning", "base file not found - two-way comparison only")
            b_pts, _bn = _located(base_rows, ctx)
        except (OSError, ValueError) as e:
            plan.add_issue("warning", "base file could not be read ({}) - two-way comparison "
                           "only".format(e))
            base_rows = []
        plan.used_base = bool(base_rows)
    mt = _match_points(m_pts, t_pts, plan.tol_ft)               # mine idx -> (theirs idx, d)
    mb = _match_points(m_pts, b_pts, plan.tol_ft) if plan.used_base else {}
    tb = _match_points(t_pts, b_pts, plan.tol_ft) if plan.used_base else {}
    pos_m = {i: (lo, la) for i, lo, la in m_pts}
    pos_t = {i: (lo, la) for i, lo, la in t_pts}
    matched_t = {v[0] for v in mt.values()}
    items = []

    def add(kind, mi=None, ti=None, dist=None, note=""):
        m = mine_rows[mi] if mi is not None else None
        t = theirs_rows[ti] if ti is not None else None
        pos = pos_m.get(mi) if mi is not None else pos_t.get(ti)
        key = location_key(*pos) if pos else "unlocated:{}:{}".format(
            "mine" if mi is not None else "theirs", mi if mi is not None else ti)
        items.append({"kind": kind, "key": key, "mine": m, "theirs": t, "distance_ft": dist,
                      "fields": differing_fields(m, t) if m is not None and t is not None
                      else [], "newer": _newer(m, t) if m is not None and t is not None else "",
                      "note": note, "pos": pos})

    for mi, _lo, _la in m_pts:
        m = mine_rows[mi]
        if mi in mt:
            ti, d = mt[mi]
            t = theirs_rows[ti]
            if same_decision(m, t):
                add(KIND_IDENTICAL, mi, ti, d)
                continue
            if mi in mb:
                b = base_rows[mb[mi][0]]
                m_same, t_same = same_decision(m, b), same_decision(t, b)
                if m_same and not t_same:
                    add(KIND_UPDATE, mi, ti, d, "only the other engineer changed it since the "
                        "base")
                    continue
                if t_same and not m_same:
                    add(KIND_MINE_CHANGED, mi, ti, d, "only you changed it since the base")
                    continue
            add(KIND_CONFLICT, mi, ti, d)
        else:
            if mi in mb:
                add(KIND_THEIRS_REMOVED, mi, None, None,
                    "in the base, not in the other engineer's file (removed there)")
            else:
                add(KIND_ONLY_MINE, mi)
    for ti, _lo, _la in t_pts:
        if ti in matched_t:
            continue
        if ti in tb:
            add(KIND_MINE_REMOVED, None, ti, None,
                "in the base and in the other engineer's file, not in yours (removed here)")
        else:
            add(KIND_ADD, None, ti)
    for mi in m_none:
        add(KIND_ONLY_MINE, mi, None, None, "no usable location - never compared")
    for ti in t_none:
        add(KIND_UNLOCATED, None, ti, None, "no usable location (Lon / Lat / X / Y / CRS) - "
            "not added")
    items.sort(key=lambda i: (i["key"], i["kind"]))
    for n, it in enumerate(items, 1):
        it["id"] = n
    plan.items = items
    return plan


# ------------------------------------------------------------------- apply --
def _canon_choice(c):
    t = str(c).strip().lower()
    if t in ("keep", "keep_mine", "mine", "yours", "me"):
        return POLICY_MINE
    if t in ("take", "take_theirs", "theirs", "other"):
        return POLICY_THEIRS
    if t in ("newest", "newest_wins", "newer"):
        return POLICY_NEWEST
    raise ValueError("unknown merge choice '{}' (use mine / theirs / newest)".format(c))


def _choice_for(item, policy, default):
    """(choice, why) for an item that needs one."""
    if isinstance(policy, dict):
        c = policy.get(item["key"], policy.get(item["id"]))
        why = "explicit choice" if c is not None else "default for unlisted conflicts"
        c = c if c is not None else default
    else:
        c, why = policy, "policy"
    c = _canon_choice(c)
    if c == POLICY_NEWEST:
        if item["kind"] == KIND_CONFLICT and item["newer"] in ("mine", "theirs"):
            return item["newer"], "newest wins: {} is newer".format(
                "the other engineer's decision" if item["newer"] == "theirs" else "yours")
        if item["kind"] == KIND_CONFLICT:
            return POLICY_MINE, "newest wins: same or unknown date - kept yours"
        return POLICY_MINE, "newest wins: a removal has no date - kept yours"
    return c, why


def _new_row(theirs, header=None, ctx=None):
    """A store row (review.FIELDS) from the other engineer's row."""
    r = norm_row(theirs)
    if not (r["Lon"] and r["Lat"]):
        ll = row_lonlat(r, ctx)
        if ll:
            r["Lon"], r["Lat"] = "{:.7f}".format(ll[0]), "{:.7f}".format(ll[1])
    if not r["Reviewer"] and header and header.get("reviewer"):
        r["Reviewer"] = str(header["reviewer"])
    row = {f: r.get(f, "") for f in RV.FIELDS}
    row["Struct_ID"] = ""                          # an id of the other engineer's run
    return row


def _backup_path(decisions_path, stamp):
    folder = os.path.dirname(decisions_path)
    base = os.path.join(folder, "{}{}".format(BACKUP_PREFIX, stamp))
    path, n = base + ".csv", 1
    while os.path.exists(path):
        n += 1
        path = "{}_{}.csv".format(base, n)
    return path


def _report_row(action, item, choice, result, reason):
    def side(row, p):
        n = norm_row(row) if row is not None else dict.fromkeys(_ALL_FIELDS, "")
        return {p + f: n[f] for f in ("Category", "ACER_Type", "Persons", "Note", "Reviewer",
                                      "Date")}
    out = {"Action": action, "Kind": item["kind"], "Choice": choice or "",
           "Location_Key": item["key"],
           "Distance_ft": "" if item["distance_ft"] is None else "{:.1f}".format(
               item["distance_ft"]),
           "Differing_Fields": ";".join(item["fields"]), "Reason": reason}
    out.update(side(item["mine"], "Mine_"))
    out.update(side(item["theirs"], "Theirs_"))
    out.update(side(result, "Result_"))
    return out


def apply_plan(store, plan, policy=POLICY_MINE, default=POLICY_MINE, report_folder=None,
               backup=True, now=None, ctx=None):
    """Carry out a MergePlan on `store` (a ReviewStore) and save it.

    policy   "mine" | "theirs" | "newest" for every conflict / removal, or a
             dict {location key (or item number): choice} for explicit
             choices (conflicts left out take `default`).
    Order of work: the plan is checked (refused plans and plans made for
    other decisions raise), a backup copy Structure_Review_Decisions_backup_
    <timestamp>.csv of the file is written next to it, the decisions are
    saved atomically, Review_Merge_Report.csv (every row: action, both
    sides, result) is written in `report_folder` (default: the decisions
    file's folder). Rows the plan does not change stay exactly as they were
    (reviewer, date, extra columns). If the decisions file is locked the
    merged decisions are written to a time-stamped name instead (result
    "locked": True) and stay in `store` in memory.

    Returns {"counts", "actions", "changed", "backup", "saved_to", "locked",
    "report", "unused_choices"}."""
    if getattr(store, "error", None) is not None:
        raise RuntimeError("{} could not be read ({}), so nothing was merged".format(
            store.path, store.error))
    if plan.refused:
        raise ValueError("the merge plan is refused: " + "; ".join(
            t for lv, t in plan.issues if lv == "error"))
    live = {id(r) for r in store.rows}
    if any(it["mine"] is not None and id(it["mine"]) not in live for it in plan.items):
        raise ValueError("the decisions changed after the plan was made - make a new plan")
    for c in ([policy] if not isinstance(policy, dict) else list(policy.values())) + [default]:
        _canon_choice(c)                            # unknown names fail before any change
    used = set()
    remove, replace, add = set(), {}, []
    actions, counts = [], Counter()

    def done(action, item, choice, result, reason=""):
        actions.append(_report_row(action, item, choice, result, reason))
        counts[action] += 1
    for it in plan.items:
        kind = it["kind"]
        m, t = it["mine"], it["theirs"]
        if kind == KIND_IDENTICAL:
            done(ACT_SKIPPED, it, "", m, "same decision - your reviewer and date kept")
        elif kind == KIND_ONLY_MINE:
            done(ACT_ONLY_MINE, it, "", m, it["note"] or "only on your side - kept")
        elif kind == KIND_MINE_CHANGED:
            done(ACT_KEPT_MINE, it, "", m, it["note"])
        elif kind == KIND_UNLOCATED:
            done(ACT_NOT_LOCATED, it, "", None, it["note"])
        elif kind == KIND_ADD:
            row = _new_row(t, plan.header, ctx)
            add.append(row)
            done(ACT_ADDED, it, "", row, "only in the other engineer's file")
        elif kind == KIND_UPDATE:
            new = dict(m)
            _take(new, t)
            replace[id(m)] = new
            done(ACT_TOOK_THEIRS, it, "", new, it["note"])
        else:                                       # conflict / removals: a choice
            if isinstance(policy, dict):
                for k in (it["key"], it["id"]):
                    if k in policy:
                        used.add(k)
            choice, why = _choice_for(it, policy, default)
            if kind == KIND_CONFLICT:
                if choice == POLICY_THEIRS:
                    new = dict(m)
                    _take(new, t)
                    replace[id(m)] = new
                    done(ACT_TOOK_THEIRS, it, choice, new, why)
                else:
                    done(ACT_KEPT_MINE, it, choice, m, why)
            elif kind == KIND_THEIRS_REMOVED:
                if choice == POLICY_THEIRS:
                    remove.add(id(m))
                    done(ACT_REMOVED, it, choice, None, why + " (removed there, so removed "
                         "here)")
                else:
                    done(ACT_KEPT_MINE, it, choice, m, why + " (kept although removed there)")
            else:                                   # KIND_MINE_REMOVED
                if choice == POLICY_THEIRS:
                    row = _new_row(t, plan.header, ctx)
                    add.append(row)
                    done(ACT_ADDED, it, choice, row, why + " (put back)")
                else:
                    done(ACT_KEPT_MINE, it, choice, None, why + " (stays removed)")
    unused = sorted((k for k in (policy if isinstance(policy, dict) else {})
                     if k not in used), key=str)

    old_rows = list(store.rows)
    new_rows = []
    for r in old_rows:
        if id(r) in remove:
            continue
        new_rows.append(replace.get(id(r), r))
    new_rows += add
    changed = bool(remove or replace or add)
    result = {"counts": dict(counts), "actions": actions, "changed": changed, "backup": None,
              "saved_to": None, "locked": False, "report": None, "unused_choices": unused}
    stamp = time.strftime("%Y%m%d_%H%M%S", now or time.localtime())
    if changed:
        if backup and store.path:
            result["backup"] = _write_backup(store, old_rows, stamp)
        store.rows = new_rows
        try:
            result["saved_to"] = store.save()
        except RuntimeError as e:
            alt = RV.ReviewStore(timestamped_path(store.path))
            alt.rows = new_rows
            try:
                result["saved_to"] = alt.save()
            except Exception as e2:
                store.rows = old_rows
                raise RuntimeError("the merged decisions could not be saved ({}; {})".format(
                    e, e2)) from e2
            result["locked"] = True
            log_message("Review merge: {} could not be written ({}); the merged decisions are "
                        "in {}".format(os.path.basename(store.path), e,
                                       os.path.basename(result["saved_to"])), MSG_WARNING)
    folder = report_folder or (os.path.dirname(os.path.abspath(store.path))
                               if store.path else None)
    if folder:
        result["report"] = write_report(folder, actions)
    return result


_TAKE_FIELDS = ("Category", "ACER_Type", "Persons", "Note", "Reviewer", "Date")


def _take(row, theirs):
    """Their decision (and its reviewer / date) into my row; my location,
    Struct_ID and Sources stay."""
    n = norm_row(theirs)
    for f in _TAKE_FIELDS:
        row[f] = n[f]


def _write_backup(store, old_rows, stamp):
    """Copy of the decisions file as it was. When the file does not exist yet
    (unsaved decisions in memory) the in-memory rows are written instead."""
    if not store.path:
        return None
    dest = _backup_path(store.path, stamp)
    try:
        if os.path.isfile(store.path):
            with open(store.path, "rb") as fh:
                data = fh.read()
        elif old_rows:
            extra = []
            for row in old_rows:
                for k in row:
                    if k and k not in RV.FIELDS and k not in extra:
                        extra.append(k)
            data = _csv_bytes(RV.FIELDS + extra, old_rows)
        else:
            return None
        tmp = dest + ".tmp"
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, dest)
    except OSError as e:
        raise RuntimeError("the backup copy of the decisions could not be written ({}) - "
                           "nothing was merged".format(e)) from e
    return dest


def write_report(folder, actions):
    """Review_Merge_Report.csv: every row of the merge with its action and
    both sides. Returns the path written."""
    os.makedirs(folder, exist_ok=True)
    path = prepare_output_path(os.path.join(folder, REPORT_FILE))
    tmp = path + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(_csv_bytes(REPORT_FIELDS, actions))
    os.replace(tmp, path)
    return path


# -------------------------------------------------------------------- text --
_KIND_TEXT = [
    (KIND_ADD, "{} decision(s) only in the other file (to add)"),
    (KIND_IDENTICAL, "{} identical (skipped)"),
    (KIND_CONFLICT, "{} CONFLICT(S) - different decisions for the same structure"),
    (KIND_THEIRS_REMOVED, "{} decision(s) removed by the other engineer but kept here"),
    (KIND_MINE_REMOVED, "{} decision(s) removed here but kept by the other engineer"),
    (KIND_UPDATE, "{} changed only by the other engineer (taken)"),
    (KIND_MINE_CHANGED, "{} changed only by you (kept)"),
    (KIND_ONLY_MINE, "{} only on your side (kept)"),
    (KIND_UNLOCATED, "{} without a usable location (not added)")]


def plan_lines(plan):
    """Text lines for the dialog / log: the plan and its problems."""
    h = plan.header or {}
    who = ", ".join(x for x in (h.get("reviewer"), h.get("exported")) if x)
    lines = ["Review merge plan for {} ({}{}):".format(
        os.path.basename(plan.theirs_path) or "?", who or "no header",
        "; dam " + plan.their_dam if plan.their_dam else "")]
    for lv, text in plan.issues:
        lines.append("{}: {}".format({"error": "REFUSED", "warning": "Warning",
                                      "info": "Note"}[lv], text))
    if plan.refused:
        return lines
    c = plan.counts()
    for kind, fmt in _KIND_TEXT:
        if c[kind]:
            lines.append("  " + fmt.format(c[kind]))
    if plan.hash_ok:
        lines.append("  SHA-256 in the header matches the rows")
    if plan.conflicts():
        lines.append("  Each conflict needs a choice: keep yours, take theirs, or the newest "
                     "date wins")
    elif not any(c[k] for k in (KIND_ADD, KIND_UPDATE)):
        lines.append("  Nothing to merge")
    return lines


def conflict_lines(plan):
    """One block of text per item that needs a choice: both sides with
    reviewer and date."""
    out = []
    for it in plan.conflicts():
        out.append("[{}] {} ({}){}".format(
            it["id"], it["key"], it["kind"].replace("_", " "),
            "; differs in " + ", ".join(it["fields"]) if it["fields"] else ""))
        out.append("     yours : " + describe_row(it["mine"]))
        out.append("     theirs: " + describe_row(it["theirs"]))
    return out


def result_lines(result):
    """Text lines for the dialog / log after apply_plan()."""
    c = result["counts"]
    lines = ["Review merge: {} added, {} took theirs, {} kept yours, {} removed, {} identical "
             "(skipped), {} only yours".format(
                 c.get(ACT_ADDED, 0), c.get(ACT_TOOK_THEIRS, 0), c.get(ACT_KEPT_MINE, 0),
                 c.get(ACT_REMOVED, 0), c.get(ACT_SKIPPED, 0), c.get(ACT_ONLY_MINE, 0))]
    if not result["changed"]:
        lines.append("  the decisions file was not changed")
    if result["backup"]:
        lines.append("  backup of the decisions: {}".format(os.path.basename(result["backup"])))
    if result["locked"]:
        lines.append("  THE DECISIONS FILE IS OPEN IN ANOTHER PROGRAM: the merged decisions "
                     "were written to {} - close the other program and copy it over the "
                     "decisions file".format(os.path.basename(result["saved_to"])))
    if result["report"]:
        lines.append("  report: {}".format(os.path.basename(result["report"])))
    if result["unused_choices"]:
        lines.append("  choices that matched no conflict: {}".format(
            ", ".join(str(k) for k in result["unused_choices"])))
    return lines
