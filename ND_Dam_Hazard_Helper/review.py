# -*- coding: utf-8 -*-
"""Engineer review decisions that survive re-runs.

Decisions are kept in outputs/Structure_Review_Decisions.csv (one row per
reviewed structure: location, decided category / ACER type / persons,
note, reviewer, date). On every classification run they are matched to the
new structure inventory by location (within MATCH_FT) and override the
automatic interpretation (structures._apply_review). The CSV is plain text
so it can be checked into a project record or sent with a submittal.
"""

import csv
import getpass
import io
import math
import os
import time

from qgis.core import QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsPointXY

from .helpers import log_message, ground_distance_in_layer_units, points_rect
from .compat import MSG_WARNING

REVIEW_FILE = "Structure_Review_Decisions.csv"
FIELDS = ["Lon", "Lat", "X", "Y", "CRS", "Struct_ID", "Category", "ACER_Type", "Persons",
          "Note", "Reviewer", "Date", "Sources"]
MATCH_FT = 10.0
CONFIRMED = "Confirmed"          # category saved when the automatic result is confirmed
_WGS = "EPSG:4326"


class _Near(object):
    """v2.6.2: the saved decisions on a grid of `tol` cells, so matching a
    structure looks at the few decisions near it instead of all of them.
    Same answer as the full scan: the nearest decision within tol, a tie
    going to the one later in the file."""

    def __init__(self, pts, tol):
        self.tol = max(float(tol), 1e-9)
        self.grid = {}
        for idx, (row, x, y) in enumerate(pts):
            if not (math.isfinite(x) and math.isfinite(y)):
                continue                 # a hand-edited cell: never matched (as before)
            self.grid.setdefault(self._key(x, y), []).append((idx, row, x, y))

    def _key(self, x, y):
        return int(math.floor(x / self.tol)), int(math.floor(y / self.tol))

    def around(self, px, py):
        kx, ky = self._key(px, py)
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for item in self.grid.get((kx + dx, ky + dy), ()):
                    yield item

    def nearest(self, px, py):
        best, key = None, None
        for idx, row, x, y in self.around(px, py):
            d = math.hypot(x - px, y - py)
            if d <= self.tol and (key is None or (d, -idx) < key):
                best, key = row, (d, -idx)
        return best

    def within(self, px, py):
        return [row for _i, row, x, y in self.around(px, py)
                if math.hypot(x - px, y - py) <= self.tol]


def _match_tol(crs, ctx, pts=None, p=None):
    """2.18.0: MATCH_FT in `crs` units, latitude-aware (Web Mercator /
    geographic) at the structure point `p` or else the saved decisions'
    centre; projected feet / metre CRSs are the plain conversion."""
    rect = None
    if p is not None:
        rect = points_rect([(p.x(), p.y())])
    elif pts:
        rect = points_rect([(x, y) for r, x, y in pts])
    return ground_distance_in_layer_units(MATCH_FT, crs, rect=rect, transform_ctx=ctx)


def review_path(output_folder):
    return os.path.join(output_folder, REVIEW_FILE)


def rep_point(rec):
    """Representative point of a structure (analysis CRS)."""
    try:
        return rec["geom"].pointOnSurface().asPoint()
    except Exception:
        try:
            return rec["geom"].centroid().asPoint()
        except Exception:
            return None


def reviewer_name():
    try:
        return getpass.getuser()
    except Exception:
        return ""


class ReviewStore(object):
    def __init__(self, path):
        self.path = path
        self.rows = []
        self.error = None
        self.unmatched = 0
        if path and os.path.isfile(path):
            # written as UTF-8 (with BOM, for Excel); a copy saved by Excel on
            # Windows may be in the ANSI code page instead, and with ";" as
            # the list separator in many locales
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
                log_message("Review decisions could not be read ({}): {}".format(
                    path, self.error), MSG_WARNING)

    @staticmethod
    def _parse(text):
        """Rows of the decisions file. Raises ValueError when the columns
        are not recognised (then the file is never overwritten)."""
        if not text.strip():
            return []
        first = text.splitlines()[0]
        delim = ","
        if "," not in first:
            try:
                delim = csv.Sniffer().sniff(first, delimiters=",;\t").delimiter
            except csv.Error:
                delim = ","
        reader = csv.DictReader(io.StringIO(text), delimiter=delim)
        cols = {c.strip() for c in (reader.fieldnames or []) if c}
        if not ({"Lon", "Lat"} <= cols or {"X", "Y", "CRS"} <= cols):
            raise ValueError("unrecognised columns {} (expected {})".format(
                sorted(cols), ", ".join(FIELDS)))
        rows = []
        for row in reader:
            rows.append({(k or "").strip(): (v or "").strip() if isinstance(v, str) else v
                         for k, v in row.items() if k})
        return rows

    def __len__(self):
        return len(self.rows)

    def save(self):
        """Write the decisions. Refuses to overwrite a file that could not be
        read (that would lose its decisions) and reports a file that is open
        in another program."""
        if self.error is not None:
            raise RuntimeError("{} could not be read ({}), so it was not overwritten. Fix or "
                               "rename it and save again.".format(self.path, self.error))
        folder = os.path.dirname(self.path)
        if folder:
            os.makedirs(folder, exist_ok=True)
        tmp = self.path + ".tmp"
        try:
            # columns the user added (e.g. in Excel) are kept
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
    def _points(self, crs, ctx):
        """(row, x, y) of every decision in `crs`."""
        out = []
        to_crs = None
        try:
            to_crs = QgsCoordinateTransform(QgsCoordinateReferenceSystem(_WGS), crs, ctx)
        except Exception:
            to_crs = None
        for row in self.rows:
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
                except (TypeError, ValueError):
                    x = y = None
            if x is not None:
                out.append((row, x, y))
        return out

    def attach(self, structs, crs, ctx):
        """Sets rec["review"] for structures with a saved decision (and
        clears earlier ones - the structures may come from a re-used
        evaluation). Returns the number matched."""
        for rec in structs:
            rec.pop("review", None)
        if not self.rows:
            return 0
        pts = self._points(crs, ctx)
        tol = _match_tol(crs, ctx, pts)   # 2.18.0: latitude-aware
        near = _Near(pts, tol)
        matched = 0
        used = set()
        hits = {}  # v2.3.1: id(row) -> structures it matched
        for rec in structs:
            p = rep_point(rec)
            if p is None:
                continue
            best = near.nearest(p.x(), p.y())
            if best is not None:
                rec["review"] = _decision(best)
                used.add(id(best))
                matched += 1
                hits.setdefault(id(best), []).append(rec)
        # v2.3.1: demote decisions matching multiple structures
        ambiguous = [recs for recs in hits.values() if len(recs) > 1]
        self.ambiguous = len(ambiguous)
        for recs in ambiguous:
            for rec in recs:
                rec.pop("review", None)
            matched -= len(recs)
        if ambiguous:
            log_message("Engineer review: {} saved decision(s) match more than one "
                        "structure - not applied, review which structure each "
                        "belongs to".format(len(ambiguous)), MSG_WARNING)
        self.unmatched = len(self.rows) - len(used)
        if matched:
            log_message("Engineer review: {} saved decision(s) applied".format(matched))
        if self.unmatched:
            log_message("Engineer review: {} saved decision(s) match no structure any more "
                        "(outline or point changed / outside the study area) - they are kept "
                        "in {}".format(self.unmatched, os.path.basename(self.path)), MSG_WARNING)
        return matched

    def decision_at(self, rec, crs, ctx):
        """The saved decision (dict as in rec["review"]) nearest to this
        structure within the matching distance, or None."""
        p = rep_point(rec)
        if p is None:
            return None
        best = _Near(self._points(crs, ctx),
                     _match_tol(crs, ctx, p=p)).nearest(p.x(), p.y())  # 2.18.0
        return _decision(best) if best is not None else None

    def decisions_for(self, structs, crs, ctx):
        """{structure id: saved decision} for many structures at once (the
        file's points are located only once)."""
        out = {}
        if not self.rows:
            return out
        pts = self._points(crs, ctx)
        near = _Near(pts, _match_tol(crs, ctx, pts))   # 2.18.0: latitude-aware
        for rec in structs:
            p = rep_point(rec)
            if p is None:
                continue
            best = near.nearest(p.x(), p.y())
            if best is not None:
                out[rec["id"]] = _decision(best)
        return out

    def remove_decision(self, rec, crs, ctx):
        """Drop the saved decision(s) at this structure. Returns how many."""
        p = rep_point(rec)
        if p is None:
            return 0
        tol = _match_tol(crs, ctx, p=p)   # 2.18.0: latitude-aware
        drop = {id(r) for r, x, y in self._points(crs, ctx)
                if math.hypot(x - p.x(), y - p.y()) <= tol}
        self.rows = [r for r in self.rows if id(r) not in drop]
        return len(drop)

    def set_decision(self, rec, crs, ctx, category=None, acer_type=None, persons=None,
                     note="", reviewer=None):
        """Add / replace the decision for a structure."""
        p = rep_point(rec)
        if p is None:
            return None
        lon = lat = ""
        try:
            to_wgs = QgsCoordinateTransform(crs, QgsCoordinateReferenceSystem(_WGS), ctx)
            q = p if to_wgs.isShortCircuited() else to_wgs.transform(p)
            lon, lat = "{:.7f}".format(q.x()), "{:.7f}".format(q.y())
        except Exception:
            pass
        tol = _match_tol(crs, ctx, p=p)   # 2.18.0: latitude-aware
        located = self._points(crs, ctx)
        drop = {id(r) for r, x, y in located if math.hypot(x - p.x(), y - p.y()) <= tol}
        row = {"Lon": lon, "Lat": lat, "X": "{:.3f}".format(p.x()), "Y": "{:.3f}".format(p.y()),
               "CRS": crs.authid() or "custom", "Struct_ID": rec.get("id", ""),
               "Category": category or "", "ACER_Type": acer_type or "",
               "Persons": "" if persons is None else str(int(persons)), "Note": note or "",
               "Reviewer": reviewer if reviewer is not None else reviewer_name(),
               "Date": time.strftime("%Y-%m-%d"),
               "Sources": ";".join(sorted(rec.get("families", ())))}
        # rows that cannot be located in this CRS are kept untouched
        self.rows = [r for r in self.rows if id(r) not in drop] + [row]
        return row


def _num(v):
    """Number from a CSV cell; a decimal comma (Excel with ";" as the list
    separator writes 46,9) is read as a decimal point."""
    t = str(v).strip()
    if "," in t and "." not in t:
        t = t.replace(",", ".")
    return float(t)


def _decision(row):
    persons = None
    try:
        if row.get("Persons") not in (None, ""):
            persons = int(_num(row["Persons"]))
    except (TypeError, ValueError):
        persons = None
    return {"category": row.get("Category") or None, "acer_type": row.get("ACER_Type") or None,
            "persons": persons, "note": row.get("Note") or "",
            "reviewer": row.get("Reviewer") or "", "date": row.get("Date") or ""}


REASON_SHAPE = "mobile home by shape - verify"
REASON_PARK_LOT = "mobile home (park / lot address) - verify"
REASON_MOBILE_OCCUPIED = "mobile-home evidence on an occupied non-residential building - verify"
PARCEL_PREFIX = "parcel: "
# v2.20.1 (structures._mark_parcel_sites)
REASON_OUTBUILDING = "NSI residential, but the parcel's 911 address is on another building"
REASON_LONE_ADDRESS = "911 address point with no building"


def review_reasons(rec):
    """Why the engineer should look at this structure (empty = no need).

    v2.9: the manufactured-home screening rules that alone made the ACER
    type (structures: rec["mobile_rule"] "shape" / "park_lot", and
    rec["mobile_occupied"]) are listed for flooded structures; a parcel hint
    (rec["parcel_hint"], set by the parcel check) is listed whenever present
    as "parcel: <hint>"."""
    from .structures import CAT_UNVERIFIED, CAT_OCCUPIED, OUT_JUDGMENT, OUT_DRY
    wet = rec.get("outcome") not in (None, OUT_DRY)
    out = []
    if wet and rec.get("category") == CAT_UNVERIFIED:
        out.append("habitability unverified")
    if wet and rec.get("address_note"):
        out.append("ND site address, habitable by rule - Place_Type / sources say otherwise")
    if wet and rec.get("site_note") and rec.get("category") == CAT_UNVERIFIED:
        out.append(REASON_LONE_ADDRESS if rec.get("lone_address") else REASON_OUTBUILDING)
    if wet and rec.get("type_conflict"):
        out.append("sources disagree on house / mobile home")
    if wet and rec.get("mobile_rule") == "shape":
        out.append(REASON_SHAPE)
    if wet and rec.get("mobile_rule") == "park_lot":
        out.append(REASON_PARK_LOT)
    if wet and rec.get("mobile_occupied"):
        out.append(REASON_MOBILE_OCCUPIED)
    if rec.get("outcome") == OUT_JUDGMENT:
        out.append("ACER judgment zone")
    if rec.get("near"):
        out.append("within {} of the {} boundary".format("near-threshold distance",
                                                         rec["near"]))
    if wet and any(s.get("snapped") is not None for s in rec.get("sources", ())):
        out.append("point moved onto outline")
    if wet and rec.get("lifeline"):
        out.append("possible lifeline facility")
    if wet and rec.get("category") == CAT_OCCUPIED and not rec.get("persons"):
        out.append("occupied, no occupancy data")
    if rec.get("cut_off") and not wet and rec.get("habitable") == "Yes":
        out.append("access cut off")
    hint = rec.get("parcel_hint")
    if hint is not None and str(hint).strip():
        out.append(PARCEL_PREFIX + str(hint).strip())
    return out


def needs_review(structs, include_all_wet=False):
    """[(rec, reasons)] most important first (judgment / near-threshold,
    then unverified, then the rest, then structures listed only as
    "flooded" or - dry - only for a parcel hint); reviewed ones last. With
    include_all_wet every flooded structure is listed (reason "flooded")."""
    from .structures import OUT_DRY
    rows = []
    for r in structs:
        why = review_reasons(r)
        if not why and include_all_wet and r.get("outcome") not in (None, OUT_DRY):
            why = ["flooded"]
        if why:
            rows.append((r, why))

    def key(item):
        r, why = item
        wet = r.get("outcome") not in (None, OUT_DRY)
        minor = why == ["flooded"] or (not wet and all(w.startswith(PARCEL_PREFIX)
                                                       for w in why))
        pri = 0 if ("ACER judgment zone" in why or r.get("near")) else \
            1 if "habitability unverified" in why else 3 if minor else 2
        return (1 if r.get("review") else 0, pri, r.get("id", 0))
    return sorted(rows, key=key)
