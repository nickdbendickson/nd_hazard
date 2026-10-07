# -*- coding: utf-8 -*-
"""v2.9: the engineer's review of roads, railroads, bridges / culverts /
low-water crossings and lifeline facilities, kept across re-runs.

Decisions are kept in outputs/Facility_Review_Decisions.csv (UTF-8 with
BOM, one row per reviewed item: kind, name, decision, value, note,
reviewer, date and the item's location in WGS 84 and in the analysis CRS).
The file is read tolerantly (Excel copies in the ANSI code page, ";" as
the list separator, decimal commas, thousands separators, headers in
another case) and never overwritten when it cannot be read.

Matching (every run, to the new road / rail / facility records):
  * a road or rail STRETCH is keyed by its middle point (half-way along
    the flooded stretch) and its name. A saved decision matches a stretch
    of the same kind when the saved point lies within LINE_MATCH_FT of the
    stretch (so a stretch that grows or shrinks a little with a changed
    raster still matches) and the names do not clearly describe different
    roads (linear.names_conflict);
  * a FACILITY (bridge / culvert / crossing, lifeline) is keyed by its
    point (a point on the surface of an outline) within POINT_MATCH_FT.
Every decision matches at most one item and every item at most one
decision: the pairs are taken best first (same name, same automatic
category, then the shorter distance, then the later row of the file). A
re-run with the same data gives the same points, so every decision
matches again. Decisions that match nothing are kept in the file and
reported.

Effects (apply_facility_decisions, before the classification):
  * Excluded (any kind) / "Overtopped: no" (crossing): the item is left out
    of the lists the classification, the output layers and the EAP list
    use; it is kept in res["facility_review"]["excluded"];
  * Set AADT (road) / Set trains per day (railroad): the value replaces
    the automatic one and the ND class, basis, judgment flag and (roads)
    the TM 11 motorists are worked out again the way linear.py does; the
    automatic values are kept in *_auto keys;
  * Set category (lifeline): category and lifeline flag replaced;
  * Overtopped: yes (crossing): recorded on the item (and in its Basis);
  * Confirmed / Note: recorded, nothing changes.
The automatic records are never changed: an overridden item is a copy,
and the automatic lists are kept in res["facility_review_auto"] so that
the next run (which may re-use the cached evaluation) starts from them
again. Without saved decisions nothing is touched.
"""

import csv
import getpass
import io
import math
import os
import re
import time

import numpy as np

from qgis.core import (
    QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsPointXY, QgsGeometry, QgsFeature,
    QgsField,
)

from .compat import MSG_WARNING, WKB_POINT, FT_STRING
from .constants import (
    CLASS_HIGH, CLASS_JUDGMENT, DEFAULT_VEHICLE_OCCUPANCY, DEFAULT_VEHICLE_SPEED_MPH,
    LIFELINE_CATEGORIES, ND_AADT_LOW_BELOW, ND_HIGH, ND_MEDIUM, ZONE_LABELS,
    vehicle_curve_available,
)
from .helpers import (log_message, create_memory_layer,
                      ground_distance_in_layer_units, points_rect)
from . import facilities as FAC
from . import linear as LN

REVIEW_FILE = "Facility_Review_Decisions.csv"
FIELDS = ["Kind", "Name", "Decision", "Value", "Auto_Value", "Note", "Reviewer", "Date",
          "Lon", "Lat", "X", "Y", "CRS", "Length_ft", "Item_ID", "Detail"]
# matching tolerances (software settings, not regulatory values)
POINT_MATCH_FT = 30.0
LINE_MATCH_FT = 50.0
_WGS = "EPSG:4326"

KIND_ROAD, KIND_RAIL, KIND_CROSSING, KIND_LIFELINE = "Road", "Railroad", "Crossing", "Lifeline"
KINDS = (KIND_ROAD, KIND_RAIL, KIND_CROSSING, KIND_LIFELINE)
LINE_KINDS = (KIND_ROAD, KIND_RAIL)
KIND_LABELS = {KIND_ROAD: "Roads", KIND_RAIL: "Railroads", KIND_CROSSING: "Bridges & crossings",
               KIND_LIFELINE: "Lifeline facilities"}
_ID_PREFIX = {KIND_ROAD: "R", KIND_RAIL: "RR", KIND_CROSSING: "C", KIND_LIFELINE: "L"}

DEC_CONFIRMED = "Confirmed"
DEC_EXCLUDED = "Excluded"
DEC_SET_AADT = "Set AADT"
DEC_SET_TRAINS = "Set trains per day"
DEC_OVERTOPPED_YES = "Overtopped: yes"
DEC_OVERTOPPED_NO = "Overtopped: no"
DEC_SET_CATEGORY = "Set category"
DEC_NOTE = "Note"
ALLOWED = {
    KIND_ROAD: (DEC_CONFIRMED, DEC_EXCLUDED, DEC_SET_AADT, DEC_NOTE),
    KIND_RAIL: (DEC_CONFIRMED, DEC_EXCLUDED, DEC_SET_TRAINS, DEC_NOTE),
    KIND_CROSSING: (DEC_CONFIRMED, DEC_EXCLUDED, DEC_OVERTOPPED_YES, DEC_OVERTOPPED_NO, DEC_NOTE),
    KIND_LIFELINE: (DEC_CONFIRMED, DEC_EXCLUDED, DEC_SET_CATEGORY, DEC_NOTE),
}
VALUE_DECISION = {KIND_ROAD: DEC_SET_AADT, KIND_RAIL: DEC_SET_TRAINS,
                  KIND_LIFELINE: DEC_SET_CATEGORY}

# the plugin's facility categories: lifelines (5.2.2) first, then the
# vulnerable-occupant categories that are listed for review only
_NON_LIFELINE = set(getattr(FAC, "_CARE", set())) | set(FAC.SCHOOLS)
CATEGORY_CHOICES = sorted(LIFELINE_CATEGORIES) + sorted(_NON_LIFELINE)

AUTO_KEY = "facility_review_auto"       # the automatic lists (restored on every run)
RESULT_KEY = "facility_review"          # what the decisions did in this run
_LIST_KEYS = ("roads", "rails", "facility_records")


def review_path(output_folder):
    return os.path.join(output_folder, REVIEW_FILE)


def reviewer_name():
    try:
        return getpass.getuser()
    except Exception:
        return ""


# ------------------------------------------------------------ parsing ----
def _num(v):
    """Number from a coordinate cell; a decimal comma is read as a point."""
    t = str(v).strip()
    if "," in t and "." not in t:
        t = t.replace(",", ".")
    return float(t)


_THOUSANDS = re.compile(r"^[+-]?\d{1,3}(?:[, '  ]\d{3})+(?:\.\d+)?$")


def parse_value(text):
    """AADT / trains per day from a CSV cell (None when empty or not a
    number). '2,500' and '2 500' are thousands (Excel formatting); '12,5'
    is a decimal comma."""
    t = str(text if text is not None else "").strip().replace(" ", " ")
    if not t:
        return None
    try:
        if "," in t and "." in t:              # 1,234.5 (US) or 1.234,5 (European)
            t = t.replace(".", "").replace(",", ".") if t.rfind(",") > t.rfind(".") \
                else t.replace(",", "")
            v = float(t.replace(" ", ""))
        elif _THOUSANDS.match(t):
            v = float(re.sub(r"[, '  ]", "", t))
        else:
            v = _num(t.replace(" ", ""))
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) and v >= 0 else None


_KIND_WORDS = {
    KIND_ROAD: ("road", "roads", "roadstretch", "street", "highway"),
    KIND_RAIL: ("railroad", "railroads", "rail", "rails", "railway", "railroadstretch"),
    KIND_CROSSING: ("crossing", "crossings", "bridge", "bridges", "culvert", "culverts",
                    "bridgecrossing", "bridgesandcrossings", "bridgesculvertscrossings",
                    "lowwatercrossing"),
    KIND_LIFELINE: ("lifeline", "lifelines", "lifelinefacility", "lifelinefacilities",
                    "facility", "facilities"),
}


def kind_of(text):
    t = re.sub(r"[^a-z]", "", str(text or "").lower())
    for k, words in _KIND_WORDS.items():
        if t in words:
            return k
    return None


def decision_code(text):
    """Canonical decision of a CSV cell ('' for an empty cell, None when
    it is not understood - such a decision is never applied)."""
    t = re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).strip()
    if not t:
        return ""
    # (the tab's longer labels, typed into the file by hand, are read too)
    if t.startswith("not overtopped") or t.startswith("overtopped no") or \
            t in ("overtopped n", "no overtopping"):
        return DEC_OVERTOPPED_NO
    if t.startswith("overtopped yes") or t in ("overtopped y", "overtopped"):
        return DEC_OVERTOPPED_YES
    if t.startswith("confirm") or t.startswith("automatic result is correct") or \
            t in ("correct", "ok"):
        return DEC_CONFIRMED
    if t.startswith("exclude") or t.startswith("not a lifeline"):
        return DEC_EXCLUDED
    if t.startswith("set aadt") or t == "aadt":
        return DEC_SET_AADT
    if t.startswith("set trains") or t in ("trains per day", "trains", "trains day"):
        return DEC_SET_TRAINS
    if t.startswith("set category") or t == "category":
        return DEC_SET_CATEGORY
    if t in ("note", "note only"):
        return DEC_NOTE
    return None


def canonical_category(text):
    """One of CATEGORY_CHOICES for a text (case-insensitive, or through the
    plugin's keyword rules), else None."""
    t = str(text or "").strip()
    for c in CATEGORY_CHOICES:
        if t.lower() == c.lower():
            return c
    c = FAC.canonical_category(t) if t else None
    return c if c in CATEGORY_CHOICES else None


def decision_of(row):
    """The decision of a file row as a dict (kind, decision, value, note,
    reviewer, date, name). decision None = not understood."""
    kind = kind_of(row.get("Kind"))
    code = decision_code(row.get("Decision"))
    note = (row.get("Note") or "").strip()
    raw_value = (row.get("Value") or "").strip()
    if code == "":
        code = DEC_NOTE if note else None
    value = None
    if code in (DEC_SET_AADT, DEC_SET_TRAINS):
        value = parse_value(raw_value)
    elif code == DEC_SET_CATEGORY:
        value = canonical_category(raw_value)
    return {"kind": kind, "decision": code, "value": value, "value_text": raw_value,
            "note": note, "reviewer": (row.get("Reviewer") or "").strip(),
            "date": (row.get("Date") or "").strip(), "name": (row.get("Name") or "").strip(),
            "raw_decision": (row.get("Decision") or "").strip()}


def valid(kind, dec):
    """(True, '') or (False, why) for a decision of `kind`."""
    if dec.get("decision") is None:
        return False, "decision '{}' not understood".format(dec.get("raw_decision", ""))
    if dec["decision"] not in ALLOWED.get(kind, ()):
        return False, "'{}' does not apply to a {}".format(dec["decision"], kind.lower())
    if dec["decision"] in (DEC_SET_AADT, DEC_SET_TRAINS) and dec.get("value") is None:
        return False, "'{}' without a number ('{}')".format(dec["decision"],
                                                           dec.get("value_text", ""))
    if dec["decision"] == DEC_SET_CATEGORY and dec.get("value") is None:
        return False, "category '{}' is not one of the plugin's categories".format(
            dec.get("value_text", ""))
    return True, ""


def _norm_name(text):
    return " ".join(re.findall(r"[a-z0-9]+", str(text or "").lower()))


# ------------------------------------------------------------ geometry ---
def item_name(kind, rec):
    if kind == KIND_RAIL:
        return " ".join(x for x in (rec.get("railroad") or "", rec.get("subdivision") or "")
                        if x).strip()
    return str(rec.get("name") or "")


def _polyline(rec):
    """(xs, ys) numpy arrays of a stretch (its wet samples; the geometry's
    vertices when the samples are not kept)."""
    xs, ys = rec.get("xs"), rec.get("ys")
    if xs is not None and ys is not None and len(xs) >= 1:
        return np.asarray(xs, dtype=np.float64), np.asarray(ys, dtype=np.float64)
    g = rec.get("geom")
    if g is None:
        return None
    try:
        pts = [p for part in LN.line_parts(g) for p in part]
    except Exception:
        pts = []
    if not pts:
        try:
            p = g.centroid().asPoint()
            pts = [(p.x(), p.y())]
        except Exception:
            return None
    a = np.asarray(pts, dtype=np.float64)
    return a[:, 0], a[:, 1]


def _half_way(xs, ys):
    if len(xs) == 1:
        return float(xs[0]), float(ys[0])
    seg = np.hypot(np.diff(xs), np.diff(ys))
    total = float(seg.sum())
    if total <= 0:
        return float(xs[0]), float(ys[0])
    cum = np.concatenate([[0.0], np.cumsum(seg)])
    half = total / 2.0
    k = int(min(max(np.searchsorted(cum, half) - 1, 0), len(seg) - 1))
    t = (half - cum[k]) / seg[k] if seg[k] > 0 else 0.0
    return float(xs[k] + t * (xs[k + 1] - xs[k])), float(ys[k] + t * (ys[k + 1] - ys[k]))


def _dist_to_line(px, py, xs, ys):
    if len(xs) == 1:
        return float(math.hypot(xs[0] - px, ys[0] - py))
    x0, y0, x1, y1 = xs[:-1], ys[:-1], xs[1:], ys[1:]
    dx, dy = x1 - x0, y1 - y0
    l2 = dx * dx + dy * dy
    t = np.where(l2 > 0, ((px - x0) * dx + (py - y0) * dy) / np.where(l2 > 0, l2, 1.0), 0.0)
    t = np.clip(t, 0.0, 1.0)
    return float(np.min(np.hypot(x0 + t * dx - px, y0 + t * dy - py)))


def item_point(kind, rec):
    """Matching point of an item in the analysis CRS: the middle of a
    stretch (half-way along it), the point of a facility. None if unknown."""
    if kind in LINE_KINDS:
        line = _polyline(rec)
        return None if line is None else _half_way(*line)
    g = rec.get("geom")
    if g is None:
        return None
    for fn in ("asPoint", "pointOnSurface", "centroid"):
        try:
            if fn == "asPoint":
                p = g.asPoint()
            else:
                p = getattr(g, fn)().asPoint()
            return p.x(), p.y()
        except Exception:
            continue
    return None


def auto_value(kind, rec):
    """The automatic value the decision may replace (text, for the file)."""
    if kind == KIND_ROAD:
        return "" if rec.get("aadt") is None else "{:.0f}".format(rec["aadt"])
    if kind == KIND_RAIL:
        return "" if rec.get("trains") is None else "{:g}".format(rec["trains"])
    return str(rec.get("category") or "")


def _detail(kind, rec):
    if kind == KIND_ROAD:
        return "{}; {:.0f} ft; AADT source: {}".format(rec.get("road_class") or "",
                                                       rec.get("length_ft") or 0,
                                                       rec.get("aadt_source") or "none")
    if kind == KIND_RAIL:
        return "{:.0f} ft; {}".format(rec.get("length_ft") or 0, rec.get("trains_source") or "")
    if kind == KIND_CROSSING:
        return " ".join(x for x in (rec.get("category") or "", rec.get("struct_no") or "",
                                    "over " + rec["crosses"] if rec.get("crosses") else "",
                                    "({})".format(rec.get("source") or "")) if x)
    return "{} ({})".format(rec.get("category") or "", rec.get("source") or "")


def item_key(kind, index, rec):
    """ID of an item in the tab and the report: R1, RR1 (position in the
    run's list), C<id>, L<id> (the facility id)."""
    if kind in LINE_KINDS:
        return "{}{}".format(_ID_PREFIX[kind], index + 1)
    return "{}{}".format(_ID_PREFIX[kind], rec.get("id", index + 1))


# --------------------------------------------------------------- store ---
_CANON = {re.sub(r"[^a-z]", "", f.lower()): f for f in FIELDS}


class FacilityReviewStore(object):
    """The decisions file. Rows are dicts keyed by FIELDS (plus any columns
    the user added, which are kept)."""

    def __init__(self, path):
        self.path = path
        self.rows = []
        self.error = None
        if path and os.path.isfile(path):
            for enc in ("utf-8-sig", "cp1252"):
                try:
                    with open(path, newline="", encoding=enc) as fh:
                        text = fh.read()
                    self.rows = self._parse(text)
                    self.error = None
                    break
                except (UnicodeDecodeError, csv.Error, ValueError) as e:
                    self.rows, self.error = [], e
                except OSError as e:
                    self.rows, self.error = [], e
                    break
            if self.error is not None:
                log_message("Road / facility review decisions could not be read ({}): {}".format(
                    path, self.error), MSG_WARNING)

    @staticmethod
    def _parse(text):
        """Rows of the file. Raises ValueError when the columns are not
        recognised (the file is then never overwritten)."""
        text = text.lstrip("﻿")
        if not text.strip():
            return []
        first = text.splitlines()[0]
        delim = max((",", ";", "\t"), key=lambda d: first.count(d))
        if first.count(delim) == 0:
            delim = ","
        reader = csv.reader(io.StringIO(text), delimiter=delim)
        header = next(reader, [])
        names = []
        for h in header:
            h = (h or "").strip().lstrip("﻿").strip()
            names.append(_CANON.get(re.sub(r"[^a-z]", "", h.lower()), h))
        cols = {n for n in names if n}
        if not ("Kind" in cols and "Decision" in cols and
                ({"Lon", "Lat"} <= cols or {"X", "Y", "CRS"} <= cols)):
            raise ValueError("unrecognised columns {} (expected {})".format(
                sorted(cols), ", ".join(FIELDS)))
        rows = []
        for values in reader:
            if not any((v or "").strip() for v in values):
                continue                        # empty lines Excel leaves (";;;;")
            row = {}
            for name, v in zip(names, values):
                if name:
                    row[name] = (v or "").strip()
            rows.append(row)
        return rows

    def __len__(self):
        return len(self.rows)

    def save(self):
        """Write the decisions (UTF-8 with BOM). Refuses to overwrite a file
        that could not be read and reports a file open in another program."""
        if self.error is not None:
            raise RuntimeError("{} could not be read ({}), so it was not overwritten. Fix or "
                               "rename it and save again.".format(self.path, self.error))
        folder = os.path.dirname(self.path)
        if folder:
            os.makedirs(folder, exist_ok=True)
        tmp = self.path + ".tmp"
        try:
            extra = []
            for row in self.rows:
                for k in row:
                    if k and k not in FIELDS and k not in extra:
                        extra.append(k)
            with open(tmp, "w", newline="", encoding="utf-8-sig") as fh:
                w = csv.DictWriter(fh, fieldnames=FIELDS + extra, extrasaction="ignore")
                w.writeheader()
                for row in self.rows:
                    w.writerow(row)
            os.replace(tmp, self.path)
        except PermissionError as e:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise RuntimeError("{} is open in another program (Excel?) - close it and save "
                               "again ({})".format(self.path, e)) from e
        return self.path

    # -- matching ------------------------------------------------------------
    def _located(self, crs, ctx):
        """[(row index, row, x, y)] of the rows that can be placed in `crs`."""
        out = []
        if crs is None:
            return out
        try:
            to_crs = QgsCoordinateTransform(QgsCoordinateReferenceSystem(_WGS), crs, ctx)
        except Exception:
            to_crs = None
        for i, row in enumerate(self.rows):
            x = y = None
            try:
                if to_crs is not None and row.get("Lon") not in (None, ""):
                    p = QgsPointXY(_num(row["Lon"]), _num(row["Lat"]))
                    p = p if to_crs.isShortCircuited() else to_crs.transform(p)
                    x, y = p.x(), p.y()
            except Exception:
                x = y = None
            if x is None and row.get("CRS") == (crs.authid() or "custom"):
                try:
                    x, y = _num(row["X"]), _num(row["Y"])
                except (TypeError, ValueError, KeyError):
                    x = y = None
            if x is not None and math.isfinite(x) and math.isfinite(y):
                out.append((i, row, x, y))
        return out

    def match(self, kind, items, crs, ctx):
        """{index in items: row} - the saved decisions of `kind` matched one
        to one to the items (road / rail stretches or facility records)."""
        rows = [(i, r, x, y) for i, r, x, y in self._located(crs, ctx)
                if kind_of(r.get("Kind")) == kind]
        if not rows or not items:
            return {}
        line = kind in LINE_KINDS
        # 2.18.0: latitude-aware (centre of the saved decisions) - Web Mercator
        # map units are stretched by 1 / cos(lat), longitude degrees shrink
        tol = ground_distance_in_layer_units(
            LINE_MATCH_FT if line else POINT_MATCH_FT, crs,
            rect=points_rect([(x, y) for i, r, x, y in rows]), transform_ctx=ctx)
        geo = []
        for k, rec in enumerate(items):
            pt = item_point(kind, rec)
            if pt is None:
                continue
            poly = _polyline(rec) if line else None
            if poly is not None:
                bb = (float(poly[0].min()), float(poly[1].min()),
                      float(poly[0].max()), float(poly[1].max()))
            else:
                bb = (pt[0], pt[1], pt[0], pt[1])
            geo.append((k, rec, pt, poly, bb, item_name(kind, rec)))
        pairs = []
        for ri, row, x, y in rows:
            rname = row.get("Name") or ""
            nrow = _norm_name(rname)
            rcat = (row.get("Auto_Value") or "").strip().lower()
            for k, rec, pt, poly, bb, name in geo:
                if x < bb[0] - tol or x > bb[2] + tol or y < bb[1] - tol or y > bb[3] + tol:
                    continue
                d = _dist_to_line(x, y, *poly) if poly is not None else \
                    math.hypot(pt[0] - x, pt[1] - y)
                if d > tol:
                    continue
                if line and LN.names_conflict(rname, name):
                    continue                     # clearly another road / railroad
                name_rank = 0 if nrow == _norm_name(name) else 1
                cat_rank = 0
                if kind == KIND_LIFELINE:
                    cat_rank = 0 if rcat == str(rec.get("category") or "").lower() else 1
                d_mid = math.hypot(pt[0] - x, pt[1] - y)
                pairs.append(((name_rank, cat_rank, round(d, 6), round(d_mid, 6), -ri),
                              k, ri, row))
        pairs.sort(key=lambda p: p[0])
        out, used = {}, set()
        for _key, k, ri, row in pairs:
            if k in out or ri in used:
                continue
            out[k] = row
            used.add(ri)
        return out

    def remove(self, row):
        """Drop this row (by identity). True if it was there."""
        n = len(self.rows)
        self.rows = [r for r in self.rows if r is not row]
        return len(self.rows) != n

    def set_decision(self, kind, rec, crs, ctx, decision, value=None, note="", reviewer=None,
                     replace=None, item_id=""):
        """Add a decision for an item (replacing the row `replace`, the one
        match() found for it). Returns the new row, or None when the item
        has no location."""
        pt = item_point(kind, rec)
        if pt is None or crs is None:
            return None
        lon = lat = ""
        try:
            to_wgs = QgsCoordinateTransform(crs, QgsCoordinateReferenceSystem(_WGS), ctx)
            q = QgsPointXY(pt[0], pt[1])
            q = q if to_wgs.isShortCircuited() else to_wgs.transform(q)
            lon, lat = "{:.7f}".format(q.x()), "{:.7f}".format(q.y())
        except Exception:
            pass
        if decision in (DEC_SET_AADT, DEC_SET_TRAINS) and value is not None:
            vtext = "{:g}".format(float(value)) if float(value) != int(float(value)) else \
                "{:d}".format(int(float(value)))
        else:
            vtext = "" if value is None else str(value)
        row = {"Kind": kind, "Name": item_name(kind, rec), "Decision": decision or "",
               "Value": vtext, "Auto_Value": auto_value(kind, rec), "Note": note or "",
               "Reviewer": reviewer if reviewer is not None else reviewer_name(),
               "Date": time.strftime("%Y-%m-%d"), "Lon": lon, "Lat": lat,
               "X": "{:.3f}".format(pt[0]), "Y": "{:.3f}".format(pt[1]),
               "CRS": crs.authid() or "custom",
               "Length_ft": "{:.0f}".format(rec.get("length_ft") or 0)
               if kind in LINE_KINDS else "",
               "Item_ID": item_id or "", "Detail": _detail(kind, rec)}
        if replace is not None:
            self.remove(replace)
        self.rows.append(row)
        return row


# ---------------------------------------------------------------- items ---
def automatic_items(res, kind):
    """[(key, record)] of the automatic records of `kind` in a scenario
    result (before any decision): the kept automatic lists when decisions
    were applied, else the result's own lists."""
    src = res.get(AUTO_KEY) or res
    if kind == KIND_ROAD:
        recs = src.get("roads") or []
    elif kind == KIND_RAIL:
        recs = src.get("rails") or []
    else:
        want = FAC.KIND_CROSSING if kind == KIND_CROSSING else FAC.KIND_LIFELINE
        recs = [r for r in (src.get("facility_records") or []) if r.get("kind") == want]
    return [(item_key(kind, i, r), r) for i, r in enumerate(recs)]


def flooded_depth(res, kind, rec):
    """Max depth (ft) of an item in a scenario (None = dry)."""
    if kind in LINE_KINDS:
        return rec.get("max_depth")
    return ((res.get("fac_eval") or {}).get(rec.get("id")) or {}).get("depth")


def review_flags(res, kind, rec):
    """Why an item deserves a look (short texts; empty = nothing special)."""
    out = []
    if kind == KIND_ROAD:
        if rec.get("nd_judgment"):
            out.append("no AADT")
        if rec.get("bridge"):
            out.append("bridge - deck overtopped?")
        if rec.get("beyond_chart"):
            out.append("velocity beyond chart")
        if rec.get("road_class") == LN.ROAD_PRIVATE:
            out.append("private road")
    elif kind == KIND_RAIL:
        if rec.get("nd_judgment"):
            out.append("trains/day unknown or unconfirmed")
        if rec.get("passenger"):
            out.append("passenger")
    elif kind == KIND_CROSSING:
        if flooded_depth(res, kind, rec) is not None:
            out.append("overtopped?")
        if (rec.get("adt") or 0) >= ND_AADT_LOW_BELOW:
            out.append("ADT >= {:,}".format(ND_AADT_LOW_BELOW))
    else:
        if rec.get("near_subject"):
            out.append("next to the dam")
        if not rec.get("lifeline"):
            out.append("not a lifeline category")
    return out


def listed_by_default(res, kind, rec):
    """Shown without 'List every item': roads that count (ND High / Medium)
    or are flagged, every rail stretch, flooded facilities."""
    if kind == KIND_ROAD:
        return rec.get("nd_class") in (ND_HIGH, ND_MEDIUM) or bool(review_flags(res, kind, rec))
    if kind == KIND_RAIL:
        return True
    return flooded_depth(res, kind, rec) is not None


# ---------------------------------------------------------------- apply ---
def restore_automatic(res):
    """Put the automatic lists back (an earlier run applied decisions to
    this - possibly cached - result). True when something was restored."""
    auto = res.pop(AUTO_KEY, None)
    res.pop(RESULT_KEY, None)
    if auto:
        for k, v in auto.items():
            res[k] = v
    return auto is not None


def _who(dec):
    parts = [x for x in (dec.get("reviewer"), dec.get("date")) if x]
    return " ({})".format(", ".join(parts)) if parts else ""


def _motorists(rec, aadt, occupancy, speed_mph):
    """TM 11 motorists on a stretch, as linear.analyze_roads counts them."""
    if not vehicle_curve_available() or not aadt or rec.get("paved") == "No" or \
            rec.get("vehicle_zone") not in (CLASS_HIGH, CLASS_JUDGMENT):
        return None
    vehicles = aadt / 24.0 * (float(rec.get("length_ft") or 0.0) / 5280.0) / max(speed_mph, 1.0)
    return vehicles * occupancy


def _road_with_aadt(rec, aadt, dec, occupancy, speed_mph):
    new = dict(rec)
    for k in ("aadt", "aadt_year", "aadt_source", "nd_class", "nd_basis", "nd_judgment",
              "persons"):
        new[k + "_auto"] = rec.get(k)
    auto = rec.get("aadt")
    new["aadt"] = float(aadt)
    new["aadt_year"] = None
    new["aadt_source"] = "engineer review{}; automatic: {}".format(
        _who(dec), "none found" if auto is None else "{:,.0f}{} ({})".format(
            auto, " ({})".format(rec["aadt_year"]) if rec.get("aadt_year") else "",
            rec.get("aadt_source") or ""))
    cls, basis, judge = LN.nd_road_class(rec.get("road_class"), new["aadt"])
    new["nd_class"], new["nd_basis"], new["nd_judgment"] = \
        cls, basis + " [AADT set by engineer review]", judge
    new["persons"] = _motorists(new, new["aadt"], occupancy, speed_mph)
    return new


def _rail_with_trains(rec, trains, dec):
    new = dict(rec)
    for k in ("trains", "trains_source", "nd_class", "nd_basis", "nd_judgment"):
        new[k + "_auto"] = rec.get(k)
    auto = rec.get("trains")
    new["trains"] = float(trains)
    new["trains_source"] = "engineer review{}; automatic: {}".format(
        _who(dec), "unknown" if auto is None else "{:g} ({})".format(
            auto, rec.get("trains_source") or ""))
    cls, basis, judge = LN.nd_rail_class(rec.get("passenger"), new["trains"], confirmed=True)
    new["nd_class"], new["nd_basis"], new["nd_judgment"] = \
        cls, basis + " [trains/day set by engineer review]", judge
    return new


def _lifeline_with_category(rec, cat, dec):
    new = dict(rec)
    new["category_auto"], new["lifeline_auto"] = rec.get("category"), rec.get("lifeline")
    new["category"] = cat
    new["lifeline"] = cat not in _NON_LIFELINE
    new["basis"] = "category set by engineer review{} (automatic: {}); {}".format(
        _who(dec), rec.get("category") or "", rec.get("basis") or "")
    return new


def _crossing_overtopped(rec, dec):
    new = dict(rec)
    new["overtopped"] = "Yes"
    new["basis"] = "overtopped - confirmed by engineer review{}; {}".format(
        _who(dec), rec.get("basis") or "")
    return new


_COUNT_TEXT = (
    ("road_excluded", "{} road stretch(es) excluded"),
    ("road_aadt", "{} AADT set"),
    ("rail_excluded", "{} railroad stretch(es) excluded"),
    ("rail_trains", "{} trains per day set"),
    ("crossing_not_overtopped", "{} bridge(s) / crossing(s) not overtopped"),
    ("crossing_excluded", "{} bridge(s) / crossing(s) excluded"),
    ("crossing_overtopped", "{} bridge(s) / crossing(s) confirmed overtopped"),
    ("lifeline_excluded", "{} lifeline facilit(ies) excluded (not a lifeline)"),
    ("lifeline_category", "{} lifeline categor(ies) set"),
    ("confirmed", "{} automatic result(s) confirmed"),
    ("notes", "{} note(s)"),
)


def _empty_summary(store):
    return {"file": getattr(store, "path", None),
            "error": None if store is None or store.error is None else str(store.error),
            "decisions": len(store) if store is not None else 0, "matched": 0,
            "matched_ids": set(), "unmatched": [], "problems": [], "counts": {},
            "excluded": [], "changed": [], "applied": {}}


def apply_facility_decisions(res, store, analysis_crs, transform_ctx, occupancy=None,
                             speed_mph=None):
    """Apply the saved decisions to one scenario result `res` in place
    (res["roads"], res["rails"], res["facility_records"] are replaced by
    reviewed lists; the automatic ones are kept in res[AUTO_KEY]; what was
    done in res[RESULT_KEY]). Call it on every run before the
    classification (also with no decisions: it undoes an earlier run's
    decisions on a re-used evaluation). occupancy / speed_mph: the run's
    TM 11 motorist settings (for a changed AADT).

    Returns (summary, lines): summary = {"counts", "matched", "unmatched"
    (rows), "problems" (texts), "excluded", "changed", "applied" {(kind,
    key): {"text", "rec"}}, ...}; lines = text for the run log / summary."""
    restore_automatic(res)
    summary = _empty_summary(store)
    if store is None or store.error is not None or not store.rows:
        return summary, summary_lines(summary)
    occupancy = DEFAULT_VEHICLE_OCCUPANCY if occupancy is None else float(occupancy)
    speed_mph = DEFAULT_VEHICLE_SPEED_MPH if speed_mph is None else float(speed_mph)
    res[AUTO_KEY] = {k: res[k] for k in _LIST_KEYS if k in res}
    counts = summary["counts"]

    def count(key):
        counts[key] = counts.get(key, 0) + 1

    decided = {}                       # id(automatic record) -> (kind, key, dec)
    for kind in KINDS:
        items = automatic_items(res, kind)
        matched = store.match(kind, [r for _k, r in items], analysis_crs, transform_ctx)
        for idx, row in matched.items():
            key, rec = items[idx]
            dec = decision_of(row)
            summary["matched_ids"].add(id(row))
            summary["matched"] += 1
            ok, why = valid(kind, dec)
            if not ok:
                summary["problems"].append("{} '{}' ({}): {} - not applied".format(
                    kind, item_name(kind, rec) or "unnamed", key, why))
                continue
            decided[id(rec)] = (kind, key, dec)

    def label(kind, key, rec):
        extra = ", {:.0f} ft".format(rec.get("length_ft") or 0) if kind in LINE_KINDS else \
            ", {}".format(rec.get("category")) if rec.get("category") else ""
        return "{} '{}' ({}{})".format(kind, item_name(kind, rec) or "unnamed", key, extra)

    def one(rec):
        """The record the classification uses (None = left out)."""
        got = decided.get(id(rec))
        if got is None:
            return rec
        kind, key, dec = got
        d = dec["decision"]
        note = " - {}".format(dec["note"]) if dec.get("note") else ""
        text, new = None, rec
        if d == DEC_CONFIRMED:
            count("confirmed")
            text = "Confirmed" + _who(dec)
        elif d == DEC_NOTE:
            count("notes")
            text = "Note" + _who(dec)
        elif d == DEC_EXCLUDED or d == DEC_OVERTOPPED_NO:
            if d == DEC_OVERTOPPED_NO:
                count("crossing_not_overtopped")
                text = "Not overtopped - left out" + _who(dec)
            else:
                count({KIND_ROAD: "road_excluded", KIND_RAIL: "rail_excluded",
                       KIND_CROSSING: "crossing_excluded",
                       KIND_LIFELINE: "lifeline_excluded"}[kind])
                text = "Excluded by review" + _who(dec)
            new = None
            summary["excluded"].append({"kind": kind, "key": key, "rec": rec, "decision": d,
                                        "note": dec.get("note") or "",
                                        "reviewer": dec.get("reviewer") or "",
                                        "date": dec.get("date") or "",
                                        "text": label(kind, key, rec) + ": " +
                                        ("not overtopped (deck above the water)"
                                         if d == DEC_OVERTOPPED_NO else "excluded") +
                                        note + _who(dec)})
        elif d == DEC_SET_AADT:
            count("road_aadt")
            new = _road_with_aadt(rec, dec["value"], dec, occupancy, speed_mph)
            auto = rec.get("aadt")
            text = "AADT {:,.0f} set (automatic {}){}".format(
                dec["value"], "none" if auto is None else "{:,.0f}".format(auto), _who(dec))
        elif d == DEC_SET_TRAINS:
            count("rail_trains")
            new = _rail_with_trains(rec, dec["value"], dec)
            auto = rec.get("trains")
            text = "{:g} trains/day set (automatic {}){}".format(
                dec["value"], "unknown" if auto is None else "{:g}".format(auto), _who(dec))
        elif d == DEC_SET_CATEGORY:
            count("lifeline_category")
            new = _lifeline_with_category(rec, dec["value"], dec)
            text = "Category '{}' set (automatic '{}'){}".format(
                dec["value"], rec.get("category") or "", _who(dec))
        elif d == DEC_OVERTOPPED_YES:
            count("crossing_overtopped")
            new = _crossing_overtopped(rec, dec)
            text = "Overtopped - confirmed" + _who(dec)
        if new is not None and new is not rec:
            summary["changed"].append({"kind": kind, "key": key, "rec": new, "auto": rec,
                                       "decision": d,
                                       "text": label(kind, key, rec) + ": " + text + note})
        summary["applied"][(kind, key)] = {"text": text, "rec": new, "decision": d,
                                           "value": dec.get("value"),
                                           "note": dec.get("note") or ""}
        return new

    for k in _LIST_KEYS:
        if k in res[AUTO_KEY]:
            src = res[AUTO_KEY][k]
            res[k] = [x for x in (one(r) for r in (src or [])) if x is not None]
    unmatched = [r for r in store.rows if id(r) not in summary["matched_ids"]]
    summary["unmatched"] = [r for r in unmatched if kind_of(r.get("Kind")) is not None]
    for r in unmatched:
        if kind_of(r.get("Kind")) is None:
            summary["problems"].append("row with kind '{}' ('{}') not understood - not "
                                       "applied".format(r.get("Kind", ""), r.get("Name", "")))
    res[RESULT_KEY] = summary
    return summary, summary_lines(summary)


def count_text(counts):
    return ", ".join(fmt.format(counts[k]) for k, fmt in _COUNT_TEXT if counts.get(k))


def _row_text(row):
    where = ""
    if row.get("Lon") and row.get("Lat"):
        where = " near {}, {}".format(row["Lat"], row["Lon"])
    return "{} '{}'{} ({})".format(kind_of(row.get("Kind")) or row.get("Kind", ""),
                                   row.get("Name") or "unnamed", where,
                                   row.get("Decision") or "note")


def summary_lines(summary, scenario=None, details=True):
    """Text lines of what the decisions did (empty without decisions)."""
    if not summary.get("decisions") and not summary.get("error"):
        return []
    fname = os.path.basename(summary.get("file") or REVIEW_FILE)
    if summary.get("error"):
        return ["Road / facility review decisions in {} could not be read ({}) - none "
                "applied".format(fname, summary["error"])]
    head = "Engineer review of roads, railroads, crossings and lifelines{}: ".format(
        " ({})".format(scenario) if scenario else "")
    body = count_text(summary["counts"]) or "no saved decision matched this run"
    lines = [head + body + " - from {}".format(fname)]
    if details:
        for item in summary.get("excluded", []) + summary.get("changed", []):
            lines.append("  " + item["text"])
    lines.extend("  " + p for p in summary.get("problems", []))
    if summary.get("unmatched"):
        lines.append(unmatched_line(summary["unmatched"], fname))
    return lines


def unmatched_line(rows, fname=REVIEW_FILE):
    ex = "; ".join(_row_text(r) for r in rows[:4])
    return ("  {} saved road / facility decision(s) match nothing in this run (data or flood "
            "extent changed?) - kept in {}: {}{}".format(
                len(rows), os.path.basename(fname), ex,
                "" if len(rows) <= 4 else " and {} more".format(len(rows) - 4)))


def apply_to_run(results, store, analysis_crs, transform_ctx, occupancy=None, speed_mph=None,
                 names=None):
    """apply_facility_decisions on every breach scenario of a run (or the
    scenario names given). Returns {"scenarios": {name: summary}, "lines",
    "warnings", "unmatched" (rows matched in no scenario), "applied"}."""
    from .scenarios import ROLE_BREACH
    per = {}
    for name, res in (results or {}).items():
        sc = res.get("scenario")
        wanted = name in names if names is not None else \
            getattr(sc, "role", ROLE_BREACH) == ROLE_BREACH
        if not wanted:
            restore_automatic(res)
            continue
        per[name] = apply_facility_decisions(res, store, analysis_crs, transform_ctx,
                                             occupancy=occupancy, speed_mph=speed_mph)[0]
    matched = set()
    for s in per.values():
        matched |= s["matched_ids"]
    rows = store.rows if store is not None and store.error is None else []
    unmatched = [r for r in rows if id(r) not in matched and kind_of(r.get("Kind"))]
    lines, warnings = [], []
    fname = getattr(store, "path", None) or REVIEW_FILE
    if store is not None and store.error is not None:
        warnings.append("road / facility review decisions in {} could not be read ({}) - none "
                        "applied".format(store.path, store.error))
    elif rows:
        for name, s in per.items():
            ls = summary_lines(dict(s, unmatched=[]), scenario=name if len(per) > 1 else None)
            lines.extend(ls)
            warnings.extend(s["problems"])
        if unmatched:
            lines.append(unmatched_line(unmatched, fname))
            warnings.append("{} saved road / facility decision(s) match nothing in this run - "
                            "kept in {}".format(len(unmatched), os.path.basename(fname)))
    for w in warnings:
        log_message("Road / facility review: " + w, MSG_WARNING)
    return {"scenarios": per, "lines": lines, "warnings": warnings, "unmatched": unmatched,
            "applied": sum(s["matched"] for s in per.values())}


# -------------------------------------------------------------- output ---
EXCLUDED_FIELDS = [("Item", FT_STRING), ("Kind", FT_STRING), ("Name", FT_STRING),
                   ("Decision", FT_STRING), ("Note", FT_STRING), ("Reviewer", FT_STRING),
                   ("Date", FT_STRING), ("Detail", FT_STRING)]


def excluded_layer(res, crs, name="Facility_Review_Excluded"):
    """Point layer of the items the review left out of a scenario (marked
    'Excluded by review' / 'Not overtopped'), or None when there are none."""
    items = ((res or {}).get(RESULT_KEY) or {}).get("excluded") or []
    if not items:
        return None
    lyr = create_memory_layer(name, WKB_POINT, crs, [QgsField(n, t) for n, t in EXCLUDED_FIELDS])
    feats = []
    for it in items:
        pt = item_point(it["kind"], it["rec"])
        if pt is None:
            continue
        f = QgsFeature(lyr.fields())
        f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(pt[0], pt[1])))
        f.setAttributes([it["key"], it["kind"], item_name(it["kind"], it["rec"]) or "unnamed",
                         "Not overtopped (deck above the water)"
                         if it["decision"] == DEC_OVERTOPPED_NO else "Excluded by review",
                         it.get("note") or "", it.get("reviewer") or "", it.get("date") or "",
                         _detail(it["kind"], it["rec"])])
        feats.append(f)
    lyr.dataProvider().addFeatures(feats)
    lyr.updateExtents()
    return lyr


def zone_label(v):
    return ZONE_LABELS.get(v, "") if v is not None else ""
