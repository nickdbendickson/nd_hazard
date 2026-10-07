# -*- coding: utf-8 -*-
"""v2.9: parcel-based habitability HINTS for the engineer's review list.

With the engineer's parcel layer (the one used for the future-development
screening, cfg "parcel_layer") and the fields the ENGINEER chooses - a land-use
/ property-class text field and / or an improvement (building) value field -
each habitable or unverified structure gets rec["parcel_hint"] (short text)
when the parcel under its representative point suggests there may be no
dwelling there:

  * improvement value 0 (or empty, when empty_means_no_building) - the tax
    roll carries no building on the parcel;
  * a land use matching NO_BUILDING_KEYWORDS ("vacant", "unimproved" ...) or
    NON_RESIDENTIAL_KEYWORDS ("agricultur", "commercial", "exempt" ...) and
    none of RESIDENTIAL_KEYWORDS ("resid", "dwelling", "farmstead" ...).

The keyword lists are SCREENING CHOICES (module constants, replaceable per
call / cfg), not rules: an agricultural parcel very often carries a farm
house, and a tax roll lags new construction. A hint is a reason to LOOK - it
never changes the category, the habitability, the ACER type or the persons of
a structure (only rec["parcel_hint"] is written) and never the classification.

Field names are never assumed: suggest_fields() only SUGGESTS candidates by
name for the dialog; the engineer's choice is what is used.

Join. The representative point of each structure (review.rep_point, the
point on its surface) is transformed into the parcel layer's CRS; the points
go on a uniform grid, the parcels are read once (only those touching the
points' extent - a request filter rectangle) and each parcel is tested only
against the points in the grid cells under its bounding box (exact
point-in-polygon, QgsGeometry.intersects - a point on the boundary counts).
Where parcels overlap (stacked condominium parcels, slivers) a hint is given
only when EVERY parcel under the point suggests it.
"""

import math
import re

from qgis.core import QgsCoordinateTransform, QgsFeatureRequest, QgsGeometry, QgsPointXY, \
    QgsRectangle

from .helpers import is_null, log_message

# SCREENING CHOICES (keyword lists) - not rules; matched at the start of a word,
# case-insensitive ("agricultur" matches "Agricultural")
NO_BUILDING_KEYWORDS = ("vacant", "unimproved", "undeveloped", "bare land", "empty lot",
                        "no improvement")
NON_RESIDENTIAL_KEYWORDS = ("agricultur", "farm", "pasture", "crop", "tillable", "grazing",
                            "exempt", "commercial", "industrial", "utilit", "railroad",
                            "right of way", "right-of-way", "church", "cemetery", "park",
                            "recreation", "government", "public", "school", "institutional",
                            "warehouse", "storage", "garage", "outbuilding", "shed")
RESIDENTIAL_KEYWORDS = ("resid", "dwelling", "house", "housing", "home", "farmstead",
                        "apartment", "condo", "townhouse", "duplex", "mobile", "manufactured",
                        "single family", "single-family", "multi family", "multi-family")
SCOPE = ("Yes", "Unverified")         # structures that get a hint (habitable / unverified)
HINT_KEY = "parcel_hint"

# cfg keys the pipeline would add (empty keyword text = the lists above)
DEFAULTS = {"parcel_hints": False, "parcel_landuse_field": "", "parcel_improvement_field": "",
            "parcel_ref_field": "", "parcel_hint_vacant_keywords": "",
            "parcel_hint_nonres_keywords": "", "parcel_hint_res_keywords": "",
            "parcel_hint_empty_is_zero": True}

# name patterns for suggest_fields (suggestions only)
_LANDUSE_EXACT = ("landuse", "land_use", "lu_desc", "ludesc", "use_desc", "usedesc",
                  "propclass", "prop_class", "property_class", "propertyclass", "class_desc",
                  "classdesc", "use_code", "usecode", "luc", "prop_type", "proptype",
                  "property_type", "zoning", "zone_desc")
_LANDUSE_PART = ("landuse", "land_use", "propclass", "prop_class", "use_desc", "usedesc",
                 "class", "use", "zoning", "zone")
_IMPR_EXACT = ("impr_val", "imprval", "imp_val", "impval", "improvement", "improvements",
               "impr_value", "improv_val", "improvval", "bldg_val", "bldgval", "bldg_value",
               "building_val", "building_value", "buildingvalue", "struct_val", "totimpr",
               "tot_impr", "imprv_val", "imprvval")
_IMPR_PART = ("impr", "improv", "bldg", "building", "struct")
_REF_EXACT = ("parcel_id", "parcelid", "parcel", "parcelnum", "parcel_num", "pin", "apn",
              "pid", "tax_id", "taxid", "parcel_no", "parcelno")


# ------------------------------------------------------------------ fields --
def _rank(names, exact, parts):
    low = [n.lower() for n in names]
    out = []
    for w in exact:
        for n, lo in zip(names, low):
            if lo == w and n not in out:
                out.append(n)
    for w in parts:
        for n, lo in zip(names, low):
            if w in lo and n not in out:
                out.append(n)
    return out


def suggest_fields(layer):
    """{"landuse": [...], "improvement": [...], "ref": [...]} field names of
    `layer` that LOOK like a land-use / property-class field, an improvement
    (building) value field and a parcel number, best first. Suggestions for
    the dialog only - nothing is used without the engineer's choice."""
    try:
        names = [f.name() for f in layer.fields()]
    except Exception:
        return {"landuse": [], "improvement": [], "ref": []}
    return {"landuse": _rank(names, _LANDUSE_EXACT, _LANDUSE_PART),
            "improvement": _rank(names, _IMPR_EXACT, _IMPR_PART),
            "ref": _rank(names, _REF_EXACT, ())}


def _field_index(layer, name):
    if not name:
        return -1
    names = [f.name() for f in layer.fields()]
    if name in names:
        return names.index(name)
    low = [n.lower() for n in names]
    return low.index(str(name).lower()) if str(name).lower() in low else -2


def keywords(text, default):
    """Keyword tuple from comma / semicolon separated text (empty = default)."""
    if text is None or (isinstance(text, str) and not text.strip()):
        return tuple(default)
    items = text if isinstance(text, (list, tuple)) else re.split(r"[;,\n]", str(text))
    out = tuple(k.strip().lower() for k in items if str(k).strip())
    return out or tuple(default)


def options_from_cfg(cfg):
    """Keyword arguments for mark_parcel_hints from the pipeline cfg."""
    cfg = cfg or {}
    return {"landuse_field": (cfg.get("parcel_landuse_field") or "").strip(),
            "improvement_field": (cfg.get("parcel_improvement_field") or "").strip(),
            "ref_field": (cfg.get("parcel_ref_field") or "").strip(),
            "no_building_keywords": keywords(cfg.get("parcel_hint_vacant_keywords"),
                                             NO_BUILDING_KEYWORDS),
            "nonres_keywords": keywords(cfg.get("parcel_hint_nonres_keywords"),
                                        NON_RESIDENTIAL_KEYWORDS),
            "residential_keywords": keywords(cfg.get("parcel_hint_res_keywords"),
                                             RESIDENTIAL_KEYWORDS),
            "empty_means_no_building": bool(cfg.get("parcel_hint_empty_is_zero", True))}


# ------------------------------------------------------------------ verdict --
def _matcher(words):
    words = [w for w in (str(x).strip().lower() for x in words or ()) if w]
    if not words:
        return None
    return re.compile(r"(?<![a-z0-9])(?:{})".format("|".join(re.escape(w) for w in words)))


def _number(v):
    """float of an improvement value (text like "$12,300" too); None = empty;
    NaN = text that is not a number."""
    if is_null(v):
        return None
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    s = str(v).strip().replace(",", "").replace("$", "")
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return float("nan")


_DEFAULT = object()


def parcel_verdict(landuse=None, improvement=None, use_landuse=False, use_improvement=False,
                   no_building=_DEFAULT, nonres=_DEFAULT, residential=_DEFAULT,
                   empty_means_no_building=True):
    """([kinds], hint text) of one parcel - kinds "no_building" / "non_residential";
    ([], "") when nothing suggests a missing dwelling. no_building / nonres /
    residential: compiled matchers (_matcher), keyword lists, None (no keyword)
    or left out (the module's lists)."""
    def m(x, default):
        if x is _DEFAULT:
            return _matcher(default)
        if x is None:
            return None
        return x if hasattr(x, "search") else _matcher(x)
    kinds, parts = [], []
    if use_improvement:
        v = _number(improvement)
        if v is None:
            if empty_means_no_building:
                kinds.append("no_building")
                parts.append("no improvement value")
        elif math.isfinite(v) and v <= 0:
            kinds.append("no_building")
            parts.append("improvement value 0")
    if use_landuse and not is_null(landuse):
        text = str(landuse).strip()
        low = text.lower()
        res_m = m(residential, RESIDENTIAL_KEYWORDS)
        if text and not (res_m is not None and res_m.search(low)):
            nb = m(no_building, NO_BUILDING_KEYWORDS)
            nr = m(nonres, NON_RESIDENTIAL_KEYWORDS)
            hit = nb.search(low) if nb is not None else None
            if hit:
                if "no_building" not in kinds:
                    kinds.append("no_building")
                parts.append('land use "{}"'.format(text[:40]))
            else:
                hit = nr.search(low) if nr is not None else None
                if hit:
                    kinds.append("non_residential")
                    parts.append('land use "{}"'.format(text[:40]))
    return kinds, "; ".join(parts)


# ------------------------------------------------------------------- the join --
def clear_parcel_hints(structs):
    """Remove every rec["parcel_hint"] (the records are re-used between runs)."""
    for rec in structs or ():
        rec.pop(HINT_KEY, None)
    return structs


def _rep_point(rec):
    from .review import rep_point
    return rep_point(rec)


class _PointGrid(object):
    def __init__(self, xs, ys):
        n = len(xs)
        w = (max(xs) - min(xs)) if n else 0.0
        h = (max(ys) - min(ys)) if n else 0.0
        # about 4 points per occupied cell on average; never 0
        self.cell = max(math.sqrt(max(w * h, 1e-12) / max(n, 1)) * 2.0,
                        max(w, h) / 4096.0, 1e-9)
        self.cells = {}
        for i, (x, y) in enumerate(zip(xs, ys)):
            self.cells.setdefault(self._key(x, y), []).append(i)

    def _key(self, x, y):
        return int(math.floor(x / self.cell)), int(math.floor(y / self.cell))

    def in_rect(self, r):
        k0 = self._key(r.xMinimum(), r.yMinimum())
        k1 = self._key(r.xMaximum(), r.yMaximum())
        span = (k1[0] - k0[0] + 1) * (k1[1] - k0[1] + 1)
        out = []
        if span > len(self.cells):                   # a huge parcel: walk the occupied cells
            for (i, j), idx in self.cells.items():
                if k0[0] <= i <= k1[0] and k0[1] <= j <= k1[1]:
                    out.extend(idx)
            return out
        for i in range(k0[0], k1[0] + 1):
            for j in range(k0[1], k1[1] + 1):
                out.extend(self.cells.get((i, j), ()))
        return out


def mark_parcel_hints(structs, parcel_layer, analysis_crs, transform_ctx, landuse_field="",
                      improvement_field="", ref_field="", no_building_keywords=None,
                      nonres_keywords=None, residential_keywords=None,
                      empty_means_no_building=True, scope=SCOPE, progress_cb=None):
    """Set rec["parcel_hint"] on the structures (habitability in `scope`)
    whose parcel suggests no dwelling (see the module text); every other
    record's hint is removed. Only rec["parcel_hint"] is ever written.

    parcel_layer: the engineer's parcel polygons (any CRS); analysis_crs: the
    CRS of rec["geom"]; landuse_field / improvement_field: the ENGINEER'S field
    choices (at least one; "" = not used); ref_field: optional parcel-number
    field for the hint text (else the feature id). Keyword lists: None = the
    module defaults (screening choices).

    Returns counts {"ok", "message", "structures", "on_parcel", "no_parcel",
    "hinted", "no_building", "non_residential", "parcels_read",
    "improvement_empty", "fields"}; never raises for bad input."""
    counts = {"ok": False, "message": "", "structures": 0, "on_parcel": 0, "no_parcel": 0,
              "hinted": 0, "no_building": 0, "non_residential": 0, "parcels_read": 0,
              "parcels_matched": 0, "improvement_empty": 0, "no_point": 0,
              "fields": {"landuse": landuse_field or "", "improvement": improvement_field or "",
                         "ref": ref_field or ""}}
    clear_parcel_hints(structs)
    if parcel_layer is None or not parcel_layer.isValid():
        counts["message"] = "no parcel layer - no parcel hints"
        return counts
    if not landuse_field and not improvement_field:
        counts["message"] = ("no land-use or improvement-value field chosen for the parcel "
                             "layer - no parcel hints")
        return counts
    i_use = _field_index(parcel_layer, landuse_field)
    i_imp = _field_index(parcel_layer, improvement_field)
    i_ref = _field_index(parcel_layer, ref_field)
    missing = [f for f, i in ((landuse_field, i_use), (improvement_field, i_imp),
                              (ref_field, i_ref)) if i == -2]
    if missing:
        counts["message"] = "the parcel layer has no field {} - no parcel hints".format(
            ", ".join(missing))
        return counts
    try:
        nb = _matcher(NO_BUILDING_KEYWORDS if no_building_keywords is None
                      else no_building_keywords)
        nr = _matcher(NON_RESIDENTIAL_KEYWORDS if nonres_keywords is None else nonres_keywords)
        rs = _matcher(RESIDENTIAL_KEYWORDS if residential_keywords is None
                      else residential_keywords)

        # ---- representative points in the parcel layer's CRS ----------------
        to_p = QgsCoordinateTransform(analysis_crs, parcel_layer.crs(), transform_ctx)
        recs, xs, ys = [], [], []
        for rec in structs:
            if rec.get("habitable") not in scope:
                continue
            counts["structures"] += 1
            p = _rep_point(rec)
            if p is None:
                counts["no_point"] += 1
                continue
            try:
                q = to_p.transform(QgsPointXY(p.x(), p.y()))
            except Exception:
                counts["no_point"] += 1
                continue
            if not (math.isfinite(q.x()) and math.isfinite(q.y())):
                counts["no_point"] += 1
                continue
            recs.append(rec)
            xs.append(q.x())
            ys.append(q.y())
        if not recs:
            counts["ok"] = True
            counts["message"] = "no habitable / unverified structure to check"
            return counts
        grid = _PointGrid(xs, ys)
        pad = grid.cell * 1e-6
        req = QgsFeatureRequest()
        req.setFilterRect(QgsRectangle(min(xs) - pad, min(ys) - pad, max(xs) + pad,
                                       max(ys) + pad))
        verdicts = [[] for _ in recs]        # per structure: (kinds, text, ref) per parcel
        total = max(1, parcel_layer.featureCount())
        for k, feat in enumerate(parcel_layer.getFeatures(req)):
            if progress_cb and k % 500 == 0:
                progress_cb(min(99.0, 100.0 * k / total), "Parcel hints")
            g = feat.geometry()
            if g is None or g.isNull() or g.isEmpty():
                continue
            counts["parcels_read"] += 1
            bb = g.boundingBox()
            x0, y0, x1, y1 = bb.xMinimum(), bb.yMinimum(), bb.xMaximum(), bb.yMaximum()
            cand = [i for i in grid.in_rect(bb) if x0 <= xs[i] <= x1 and y0 <= ys[i] <= y1]
            if not cand:
                continue
            inside = []
            for i in cand:
                try:
                    if g.intersects(QgsGeometry.fromPointXY(QgsPointXY(xs[i], ys[i]))):
                        inside.append(i)
                except Exception:
                    continue
            if not inside:
                continue
            counts["parcels_matched"] += 1
            a = feat.attributes()
            use = a[i_use] if i_use >= 0 else None
            imp = a[i_imp] if i_imp >= 0 else None
            if i_imp >= 0 and _number(imp) is None:
                counts["improvement_empty"] += 1
            kinds, text = parcel_verdict(use, imp, i_use >= 0, i_imp >= 0, nb, nr, rs,
                                         empty_means_no_building)
            ref = a[i_ref] if i_ref >= 0 else None
            ref = "fid {}".format(feat.id()) if is_null(ref) or not str(ref).strip() \
                else str(ref).strip()
            for i in inside:
                verdicts[i].append((kinds, text, ref))

        for rec, vs in zip(recs, verdicts):
            if not vs:
                counts["no_parcel"] += 1
                continue
            counts["on_parcel"] += 1
            if any(not kinds for kinds, _t, _r in vs):
                continue                   # a parcel under the point does not suggest it
            kinds, text, ref = vs[0]
            more = " (+{} overlapping parcel(s))".format(len(vs) - 1) if len(vs) > 1 else ""
            rec[HINT_KEY] = "parcel {}: {}{} - check habitability".format(ref, text, more)
            counts["hinted"] += 1
            all_kinds = {x for kk, _t, _r in vs for x in kk}
            if "no_building" in all_kinds:
                counts["no_building"] += 1
            else:
                counts["non_residential"] += 1
        counts["ok"] = True
        counts["message"] = "{} of {} structure(s) on a parcel have a parcel hint".format(
            counts["hinted"], counts["on_parcel"])
        if progress_cb:
            progress_cb(100.0, "Parcel hints done")
        return counts
    except Exception as e:                        # noqa: BLE001 - never fails the run
        clear_parcel_hints(structs)
        counts["ok"] = False
        counts["message"] = "parcel hints not made: {}".format(e)
        log_message(counts["message"])
        return counts


def summary_lines(counts, layer_name=""):
    """Summary text lines for the run (one line when nothing was done)."""
    if not counts:
        return []
    if not counts.get("ok"):
        return ["Parcel hints: {}".format(counts.get("message") or "not made")]
    f = counts.get("fields") or {}
    used = ", ".join("{} field {}".format(k, v) for k, v in (
        ("land-use", f.get("landuse")), ("improvement-value", f.get("improvement"))) if v)
    out = ["Parcel hints (review aid, screening keyword lists - they never change a "
           "structure's category, habitability or type){}: {} of {} habitable / unverified "
           "structure(s) on a parcel flagged ({} no building on the parcel, {} non-residential "
           "land use); {} not on any parcel{}".format(
               " - {}{}".format(layer_name + ", " if layer_name else "", used) if used else "",
               counts["hinted"], counts["on_parcel"], counts["no_building"],
               counts["non_residential"], counts["no_parcel"],
               "; {} without a representative point".format(counts["no_point"])
               if counts.get("no_point") else "")]
    n = counts.get("parcels_matched") or 0
    if f.get("improvement") and n and counts.get("improvement_empty", 0) >= 0.9 * n:
        out.append("  Note: the improvement-value field is empty on {} of the {} parcel(s) "
                   "under the structures - check that it is the right field".format(
                       counts["improvement_empty"], n))
    return out
