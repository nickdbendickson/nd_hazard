# -*- coding: utf-8 -*-
"""Other dams (dams in series, ND REG_05.2026 5.3(c)) and lifeline /
critical facilities (5.2.2) in the inundated area.

Facilities come from downloads (National Inventory of Dams; ND GIS Hub,
EIA and OpenStreetMap for lifelines) and / or a
layer the engineer gives. Each one is tested against every scenario's
depth raster:
  * dam: depth > 0 within DAM_SEARCH_FT of the NID point (the point is often
    on the embankment or at the outlet, not in the channel);
  * lifeline point: depth > 0 within LIFELINE_POINT_RADIUS_FT; polygon:
    depth > 0 in the polygon or its 5-ft edge ring.
With a no-breach baseline a facility is charged to the breach when it is
dry without the breach or the breach adds at least the incremental depth.

Dams in series are an engineering-judgment item (the plugin cannot tell
whether the flood overtops or fails the other dam); flooded lifelines make
the dam at least ND Medium unless switched off.
"""

import math
import re

import numpy as np

from qgis.core import (
    QgsCoordinateTransform, QgsFeature, QgsField, QgsGeometry, QgsPointXY,
)

from .compat import WKB_POINT, FT_DOUBLE, FT_INT, FT_STRING, NULL, GEOM_POLYGON
from .constants import (
    DAM_SEARCH_FT, LIFELINE_POINT_RADIUS_FT, LIFELINE_CATEGORIES, FACILITY_DEDUP_FT,
    OSM_EDGE_BUFFER_FT, CROSSING_RADIUS_FT,
)
from .helpers import (
    log_message, create_memory_layer, transform_geometry, buffer_distance_in_layer_units,
    is_null, transform_xy,
)

KIND_DAM, KIND_LIFELINE, KIND_CROSSING = "dam", "lifeline", "crossing"

_NAME_FIELDS = ("name", "dam_name", "facname", "facility", "plant_name", "cwp_name",
                "business", "fac_name", "label")
_TYPE_FIELDS = ("category", "type", "fac_type", "facility_type", "hosp_type", "naicsdescr",
                "primsource", "sic_codes")


def _ft(distance_ft, crs, lat=None):
    """Feet in the CRS units (degrees scaled at `lat` for geographic CRSs)."""
    d = buffer_distance_in_layer_units(distance_ft, crs)
    try:
        if crs is not None and crs.isGeographic() and lat is not None:
            d = distance_ft / (364000.0 * max(0.2, math.cos(math.radians(lat))))
    except Exception:
        pass
    return d


def _field_index(names_low, wanted):
    for w in wanted:
        if w in names_low:
            return names_low.index(w)
    return -1


def _attr(attrs, idx):
    if idx < 0:
        return ""
    v = attrs[idx]
    return "" if is_null(v) else str(v).strip()


def _num(attrs, idx):
    if idx < 0:
        return None
    try:
        v = float(attrs[idx])
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None


# free-text category / type -> canonical category (first match wins)
_CATEGORY_KEYWORDS = (
    ("nursing", "Nursing home"), ("skilled", "Nursing home"),
    ("assisted", "Basic care / assisted living"), ("basic care", "Basic care / assisted living"),
    ("hospital", "Hospital"), ("medical center", "Hospital"),
    ("emergency operation", "Emergency operations center"), ("eoc", "Emergency operations center"),
    ("fire", "Fire station"), ("ambulance", "EMS station"), ("ems", "EMS station"),
    ("police", "Law enforcement"), ("sheriff", "Law enforcement"),
    ("law enforcement", "Law enforcement"), ("substation", "Electric substation"),
    ("power plant", "Power plant"), ("generating", "Power plant"),
    ("wastewater", "Wastewater treatment"), ("sewage", "Wastewater treatment"),
    ("sewer", "Wastewater treatment"), ("lagoon", "Wastewater treatment"),
    ("water treatment", "Water treatment"), ("water works", "Water treatment"),
    ("water plant", "Water treatment"),
)
_CARE = {"Nursing home", "Basic care / assisted living"}
# v2.5: schools and child care (OSM) - vulnerable occupants, listed for review
# and as priority notifications; not lifelines (5.2.2) and never a class change
SCHOOLS = {"School", "Child care"}
VULNERABLE = _CARE | SCHOOLS | {"Hospital"}


def canonical_category(text):
    """Canonical facility category of a free-text value (case-insensitive),
    or None when nothing matches."""
    t = " {} ".format(re.sub(r"[^a-z0-9]+", " ", (text or "").lower()))
    for canon in LIFELINE_CATEGORIES | _CARE | SCHOOLS:
        if t.strip() == canon.lower():
            return canon
    for kw, canon in _CATEGORY_KEYWORDS:
        if re.search(r"(?<![a-z]){}(?![a-z])".format(re.escape(kw)), t):
            return canon
    return None


def _norm_name(name):
    n = re.sub(r"[^a-z0-9 ]+", " ", (name or "").lower())
    n = re.sub(r"\b(dam|dams|the|no|number)\b", " ", n)
    return re.sub(r"\s+", " ", n).strip()


# ------------------------------------------------------------------- read --
def read_dams(layer, source_label, analysis_crs, transform_ctx, clip=None):
    """Dam records (points in `analysis_crs`) from an NID-like layer."""
    if layer is None or not layer.isValid():
        return []
    names = [f.name() for f in layer.fields()]
    low = [n.lower() for n in names]
    i_id = _field_index(low, ("nidid", "nid_id", "nid", "damid", "dam_id"))
    i_name = _field_index(low, ("name", "dam_name") + _NAME_FIELDS)
    i_haz = _field_index(low, ("hazard_potential", "hazard", "hazard_class", "haz_class",
                               "hazardclass"))
    i_h = _field_index(low, ("nid_height", "dam_height", "height", "structural_height"))
    i_s = _field_index(low, ("nid_storage", "max_storage", "storage", "normal_storage"))
    i_riv = _field_index(low, ("river_or_stream", "river", "stream"))
    i_eap = _field_index(low, ("eap_prepared", "eap"))
    xf = QgsCoordinateTransform(layer.crs(), analysis_crs, transform_ctx)
    a_to_clip = clip.transform_from(analysis_crs) if clip is not None else None
    out = []
    for feat in layer.getFeatures():
        g = transform_geometry(feat.geometry(), xf)
        if g is None or g.isNull() or g.isEmpty():
            continue
        try:
            pt = g.asPoint() if g.type() != GEOM_POLYGON else g.pointOnSurface().asPoint()
        except Exception:
            try:
                pt = g.centroid().asPoint()
            except Exception:
                continue
        if clip is not None and not clip.contains_point(pt, xform=a_to_clip):
            continue
        a = feat.attributes()
        out.append({"kind": KIND_DAM, "source": source_label,
                    "geom": QgsGeometry.fromPointXY(QgsPointXY(pt)),
                    "nid": _attr(a, i_id), "name": _attr(a, i_name) or "(unnamed dam)",
                    "hazard": _attr(a, i_haz), "height_ft": _num(a, i_h),
                    "storage_acft": _num(a, i_s), "river": _attr(a, i_riv),
                    "eap": _attr(a, i_eap), "category": "Dam"})
    return out


def read_lifelines(layer, source_label, category, analysis_crs, transform_ctx, clip=None):
    """Lifeline records from a point or polygon layer. `category` is the
    category of every feature (agency layers) or None to read it from a
    'category' field, else a 'type' field (OSM download, engineer's layer):
    matched case-insensitively to the plugin's categories; an engineer's
    feature whose type matches none of them is still a lifeline."""
    if layer is None or not layer.isValid():
        return []
    names = [f.name() for f in layer.fields()]
    low = [n.lower() for n in names]
    i_name = _field_index(low, _NAME_FIELDS)
    i_cat = _field_index(low, ("category",)) if category is None else -1
    i_type = _field_index(low, _TYPE_FIELDS)
    xf = QgsCoordinateTransform(layer.crs(), analysis_crs, transform_ctx)
    a_to_clip = clip.transform_from(analysis_crs) if clip is not None else None
    out = []
    for feat in layer.getFeatures():
        g = transform_geometry(feat.geometry(), xf)
        if g is None or g.isNull() or g.isEmpty():
            continue
        if clip is not None and not clip.intersects_geometry(g, xform=a_to_clip):
            continue
        a = feat.attributes()
        detail = _attr(a, i_type) if i_type != i_cat else ""
        if category:
            cat = category
        else:
            raw = _attr(a, i_cat)
            cat = canonical_category(raw) or canonical_category(detail) or \
                canonical_category(_attr(a, i_name)) or "Lifeline (user layer)"
        out.append({"kind": KIND_LIFELINE, "source": source_label, "geom": g,
                    "name": _attr(a, i_name) or "(unnamed)", "category": cat,
                    "detail": detail, "lifeline": cat not in (_CARE | SCHOOLS),
                    "polygon": g.type() == GEOM_POLYGON})
    return out


_NBI_OWNERS = {"01": "State highway agency", "02": "County highway agency",
               "03": "Town or township", "04": "City or municipal", "11": "State park / forest",
               "21": "Other state agency", "25": "Other local agency", "26": "Private",
               "27": "Railroad", "31": "State toll authority", "60": "Other federal",
               "62": "Bureau of Indian Affairs", "64": "U.S. Forest Service",
               "66": "National Park Service", "70": "Corps of Engineers"}


def read_crossings(layer, source_label, analysis_crs, transform_ctx, clip=None):
    """Bridges, culverts and stream crossings: National Bridge Inventory
    records (STRUCTURE_TYPE_043B = 19 is a culvert) or OpenStreetMap points
    with a 'category' field (culvert, ford / low-water crossing, railroad
    bridge)."""
    if layer is None or not layer.isValid():
        return []
    low = [f.name().lower() for f in layer.fields()]
    nbi = "structure_number_008" in low
    idx = {k: _field_index(low, names) for k, names in (
        ("no", ("structure_number_008",)), ("carried", ("facility_carried_007",)),
        ("crosses", ("features_desc_006a",)), ("adt", ("adt_029",)),
        ("adt_year", ("year_adt_030",)), ("type", ("structure_type_043b",)),
        ("owner", ("owner_022",)), ("built", ("year_built_027",)),
        ("scour", ("scour_critical_113",)), ("name", ("name",)),
        ("category", ("category",)), ("waterway", ("waterway",)),
        ("railway", ("railway",)), ("osm_id", ("osm_id",)), ("operator", ("operator",)))}
    xf = QgsCoordinateTransform(layer.crs(), analysis_crs, transform_ctx)
    a_to_clip = clip.transform_from(analysis_crs) if clip is not None else None
    out = []
    for feat in layer.getFeatures():
        g = transform_geometry(feat.geometry(), xf)
        if g is None or g.isNull() or g.isEmpty():
            continue
        try:
            pt = g.asPoint()
        except Exception:
            try:
                pt = g.centroid().asPoint()
            except Exception:
                continue
        if clip is not None and not clip.contains_point(pt, xform=a_to_clip):
            continue
        a = feat.attributes()
        if nbi:
            typ = _attr(a, idx["type"]).lstrip("0")
            cat = "Culvert" if typ == "19" else "Road bridge"
            carried = _attr(a, idx["carried"]).strip("' ")
            crosses = _attr(a, idx["crosses"]).strip("' ")
            owner = _attr(a, idx["owner"])
            adt = _num(a, idx["adt"])
            yr = _num(a, idx["adt_year"])
            rec = {"name": carried or "(bridge)", "category": cat,
                   "struct_no": _attr(a, idx["no"]).strip(), "carried": carried,
                   "crosses": crosses, "adt": adt, "adt_year": int(yr) if yr else None,
                   "owner": _NBI_OWNERS.get(owner.zfill(2), owner) if owner else "",
                   "built": int(_num(a, idx["built"]) or 0) or None,
                   "scour": _attr(a, idx["scour"])}
        else:
            cat = _attr(a, idx["category"]) or "Crossing"
            rec = {"name": _attr(a, idx["name"]) or "(unnamed)", "category": cat,
                   "struct_no": _attr(a, idx["osm_id"]), "carried": _attr(a, idx["railway"]),
                   "crosses": _attr(a, idx["waterway"]), "adt": None, "adt_year": None,
                   "owner": _attr(a, idx["operator"]), "built": None, "scour": ""}
        rec.update({"kind": KIND_CROSSING, "source": source_label,
                    "geom": QgsGeometry.fromPointXY(QgsPointXY(pt))})
        out.append(rec)
    return out


def crossing_priority(rec):
    """Sort key: busy roads first (ADT), then railroad bridges, then the rest."""
    adt = rec.get("adt") or 0
    rail = 1 if rec["category"] == "Railroad bridge" else 0
    return (-adt, -rail, rec["name"])


def dedupe(records, crs, lat=None):
    """Drop a lifeline record when an earlier one of the same category lies
    within FACILITY_DEDUP_FT (agency layers are read first, so they win over
    OSM); a dam when an earlier one has the same NID ID."""
    tol = _ft(FACILITY_DEDUP_FT, crs, lat)
    crossing_tol = _ft(100.0, crs, lat)
    kept = []
    for r in records:
        c = r["geom"].centroid().asPoint()
        dup = None
        for k in kept:
            if k["category"] != r["category"]:
                continue
            if r["kind"] == KIND_CROSSING:
                # the same structure from NBI and OSM; two records of one
                # source (twin bridges, neighbouring culverts) stay apart
                if r["source"] == k["source"]:
                    continue
                kc = k["geom"].centroid().asPoint()
                if math.hypot(kc.x() - c.x(), kc.y() - c.y()) <= crossing_tol:
                    dup = k
                    break
                continue
            if r["kind"] == KIND_DAM:
                # dams: the same NID ID (a dam in the download and in the
                # engineer's layer), or no ID and the same name close by -
                # separate dams of one project (main dam, dikes) are kept
                if r.get("nid") and k.get("nid"):
                    if r["nid"].upper() == k["nid"].upper():
                        dup = k
                        break
                    continue
                if _norm_name(r.get("name")) != _norm_name(k.get("name")):
                    continue
            kc = k["geom"].centroid().asPoint()
            if math.hypot(kc.x() - c.x(), kc.y() - c.y()) <= tol:
                dup = k
                break
        if dup is None:
            kept.append(r)
        else:
            if r["source"] not in dup.setdefault("also_in", []):
                dup["also_in"].append(r["source"])
            if dup.get("name") in ("", "(unnamed)") and r.get("name"):
                dup["name"] = r["name"]
    for i, r in enumerate(kept):
        r["id"] = i + 1
    return kept


def mark_subject_dam(dams, subject_nid="", dam_name=""):
    """Flag the dam being classified (by NID ID, else by name) so it is not
    counted as a dam in series. Returns the flagged record or None."""
    sid = (subject_nid or "").strip().upper()
    target = _norm_name(dam_name)
    hit = None
    for d in dams:
        d["subject"] = False
        if sid and d.get("nid", "").upper() == sid:
            hit = d
        elif not sid and target and _norm_name(d.get("name")) == target:
            hit = hit or d
    if hit is not None:
        hit["subject"] = True
        # saddle dams / dikes listed under the same name are part of it
        hn = _norm_name(hit.get("name"))
        for d in dams:
            if d is not hit and hn and _norm_name(d.get("name")) == hn:
                d["subject"] = True
    return hit


def mark_near_subject(records, crs, lat=None):
    """Lifelines within DAM_SEARCH_FT of the dam being classified (they may
    sit in the reservoir / at the dam itself): flagged for review."""
    subj = [r for r in records if r["kind"] == KIND_DAM and r.get("subject")]
    if not subj:
        return 0
    tol = _ft(DAM_SEARCH_FT, crs, lat)
    n = 0
    for r in records:
        if r["kind"] != KIND_LIFELINE:
            continue
        c = r["geom"].centroid().asPoint()
        for d in subj:
            p = d["geom"].asPoint()
            if math.hypot(p.x() - c.x(), p.y() - c.y()) <= tol:
                r["near_subject"] = True
                n += 1
                break
    return n


# --------------------------------------------------------------- evaluate --
def _search_geom(rec, crs, lat):
    if rec["kind"] == KIND_DAM:
        return rec["geom"].buffer(_ft(DAM_SEARCH_FT, crs, lat), 16), \
            "depth > 0 within {:g} ft of the NID point".format(DAM_SEARCH_FT)
    if rec.get("polygon"):
        return rec["geom"].buffer(_ft(OSM_EDGE_BUFFER_FT, crs, lat), 8), \
            "depth > 0 in the outline or its {:g}-ft edge ring".format(OSM_EDGE_BUFFER_FT)
    if rec["kind"] == KIND_CROSSING:
        return rec["geom"].buffer(_ft(CROSSING_RADIUS_FT, crs, lat), 16), \
            "depth > 0 within {:g} ft of the structure point (the channel under a " \
            "bridge is wet too - the deck / road may not be overtopped)".format(
                CROSSING_RADIUS_FT)
    return rec["geom"].buffer(_ft(LIFELINE_POINT_RADIUS_FT, crs, lat), 16), \
        "depth > 0 within {:g} ft of the point".format(LIFELINE_POINT_RADIUS_FT)


def evaluate(records, samplers, analysis_crs, transform_ctx, lat=None):
    """{id: {"depth": max depth ft or None, "arrival": earliest arrival h or
    None, "x", "y" (analysis CRS, where the max depth is)}} for one scenario.
    A facility with no wet cell gets depth None."""
    depth = samplers.get("depth")
    arr = samplers.get("arr")
    if depth is None or not depth.ok:
        return {}
    to_d = QgsCoordinateTransform(analysis_crs, depth.crs, transform_ctx) \
        if depth.crs is not None else None
    back = QgsCoordinateTransform(depth.crs, analysis_crs, transform_ctx) \
        if depth.crs is not None else None
    to_a = QgsCoordinateTransform(analysis_crs, arr.crs, transform_ctx) \
        if arr is not None and arr.ok and arr.crs is not None else None
    out = {}
    for rec in records:
        area, basis = _search_geom(rec, analysis_crs, lat)
        rec["basis"] = basis
        g = transform_geometry(area, to_d) if to_d is not None else area
        res = {"depth": None, "arrival": None, "x": None, "y": None}
        if g is not None:
            xs, ys, vals = depth.cells_in_geometry(g)
            if vals.size:
                wet = vals > 0
                if wet.any():
                    i = int(vals.argmax())
                    res["depth"] = float(vals[i])
                    p = QgsPointXY(float(xs[i]), float(ys[i]))
                    if back is not None:
                        try:
                            p = back.transform(p)
                        except Exception:
                            pass
                    res["x"], res["y"] = p.x(), p.y()
                    if to_a is not None:
                        # v2.3.1: arrival at wet cells at facility, not buffer minimum
                        # xs/ys in depth.crs; back: depth.crs→analysis_crs; to_a: analysis_crs→arr.crs
                        awx, awy = transform_xy(xs[wet], ys[wet], back)
                        awx, awy = transform_xy(awx, awy, to_a)
                        av = arr.values_at(awx, awy)
                        av = av[np.isfinite(av) & (av >= 0)]
                        if av.size:
                            res["arrival"] = float(av.min())
        out[rec["id"]] = res
    return out


def mark_incremental(records, breach_eval, base_eval, min_depth_ft):
    """{id: (charged?, note)} of the flooded facilities against the
    baseline evaluation (None = no baseline: every flooded one is charged)."""
    out = {}
    for rec in records:
        d = (breach_eval.get(rec["id"]) or {}).get("depth")
        if d is None:
            continue
        if base_eval is None:
            out[rec["id"]] = (True, "")
            continue
        bd = (base_eval.get(rec["id"]) or {}).get("depth")
        if bd is None or bd <= 0:
            out[rec["id"]] = (True, "dry without breach")
        elif d - bd >= min_depth_ft:
            out[rec["id"]] = (True, "+{:.1f} ft with breach".format(d - bd))
        else:
            out[rec["id"]] = (False, "flooded without breach ({:.1f} ft)".format(bd))
    return out


def flooded(records, ev, charged=None, kind=None, charged_only=True):
    """Records of `kind` flooded in evaluation `ev` (and charged to the breach
    when `charged` is given and charged_only)."""
    out = []
    for rec in records:
        if kind is not None and rec["kind"] != kind:
            continue
        if rec.get("subject"):
            continue
        if (ev.get(rec["id"]) or {}).get("depth") is None:
            continue
        if charged is not None and charged_only and not charged.get(rec["id"], (True,))[0]:
            continue
        out.append(rec)
    return out


# ----------------------------------------------------------------- layers --
DAM_FIELDS = [("Dam_Name", FT_STRING), ("NID_ID", FT_STRING), ("Hazard_Potential", FT_STRING),
              ("Height_ft", FT_DOUBLE), ("Storage_acft", FT_DOUBLE), ("River", FT_STRING),
              ("EAP", FT_STRING), ("Flooded", FT_STRING), ("Max_Depth_ft", FT_DOUBLE),
              ("Arrival_hr", FT_DOUBLE), ("Base_Depth_ft", FT_DOUBLE), ("Charged", FT_STRING),
              ("Note", FT_STRING), ("Basis", FT_STRING), ("Source", FT_STRING)]
LIFELINE_FIELDS = [("Facility", FT_STRING), ("Category", FT_STRING), ("Lifeline", FT_STRING),
                   ("Detail", FT_STRING), ("Flooded", FT_STRING), ("Max_Depth_ft", FT_DOUBLE),
                   ("Arrival_hr", FT_DOUBLE), ("Base_Depth_ft", FT_DOUBLE),
                   ("Charged", FT_STRING), ("Note", FT_STRING), ("Basis", FT_STRING),
                   ("Source", FT_STRING)]


CROSSING_FIELDS = [("Structure", FT_STRING), ("Category", FT_STRING),
                   ("Structure_No", FT_STRING), ("Carries", FT_STRING), ("Crosses", FT_STRING),
                   ("ADT", FT_INT), ("ADT_Year", FT_INT), ("Owner", FT_STRING),
                   ("Year_Built", FT_INT), ("Scour_Critical", FT_STRING),
                   ("Flooded", FT_STRING), ("Max_Depth_ft", FT_DOUBLE),
                   ("Arrival_hr", FT_DOUBLE), ("Base_Depth_ft", FT_DOUBLE),
                   ("Charged", FT_STRING), ("Note", FT_STRING), ("Basis", FT_STRING),
                   ("Source", FT_STRING)]


def _round(v, n=2):
    return NULL if v is None else round(float(v), n)


def facilities_layer(records, kind, ev, base_ev, charged, crs, name):
    """Point layer of the facilities of `kind` (all of them in the study area,
    flooded or not). None when there are none."""
    recs = [r for r in records if r["kind"] == kind]
    if not recs:
        return None
    spec = {KIND_DAM: DAM_FIELDS, KIND_CROSSING: CROSSING_FIELDS}.get(kind, LIFELINE_FIELDS)
    if kind == KIND_CROSSING:
        recs = sorted(recs, key=crossing_priority)
    lyr = create_memory_layer(name, WKB_POINT, crs, [QgsField(n, t) for n, t in spec])
    feats = []
    for r in recs:
        e = ev.get(r["id"]) or {}
        b = (base_ev or {}).get(r["id"]) or {}
        wet = e.get("depth") is not None
        ch = (charged or {}).get(r["id"])
        note = ""
        if r.get("subject"):
            note = "the dam being classified - not counted"
        elif ch is not None:
            note = ch[1]
        if r.get("near_subject"):
            note = (note + "; " if note else "") + \
                "next to the dam being classified - may be in the reservoir; verify"
        if r.get("also_in"):
            note = (note + "; " if note else "") + "also in " + ", ".join(r["also_in"])
        charged_txt = NULL if not wet or r.get("subject") else (
            "Yes" if ch is None or ch[0] else "No")
        f = QgsFeature(lyr.fields())
        try:
            f.setGeometry(QgsGeometry.fromPointXY(r["geom"].centroid().asPoint()
                                                  if r.get("polygon") else r["geom"].asPoint()))
        except Exception:
            f.setGeometry(r["geom"].centroid())
        if kind == KIND_DAM:
            f.setAttributes([r["name"], r.get("nid") or NULL, r.get("hazard") or NULL,
                             _round(r.get("height_ft"), 1), _round(r.get("storage_acft"), 0),
                             r.get("river") or NULL, r.get("eap") or NULL,
                             "Yes" if wet else "No", _round(e.get("depth")),
                             _round(e.get("arrival")), _round(b.get("depth")), charged_txt,
                             note or NULL, r.get("basis") or NULL, r["source"]])
        elif kind == KIND_CROSSING:
            def _int(v):
                return NULL if v in (None, "") else int(v)
            f.setAttributes([r["name"], r["category"], r.get("struct_no") or NULL,
                             r.get("carried") or NULL, r.get("crosses") or NULL,
                             _int(r.get("adt")), _int(r.get("adt_year")),
                             r.get("owner") or NULL, _int(r.get("built")),
                             r.get("scour") or NULL, "Yes" if wet else "No",
                             _round(e.get("depth")), _round(e.get("arrival")),
                             _round(b.get("depth")), charged_txt, note or NULL,
                             r.get("basis") or NULL, r["source"]])
        else:
            f.setAttributes([r["name"], r["category"], "Yes" if r.get("lifeline") else "No",
                             r.get("detail") or NULL, "Yes" if wet else "No",
                             _round(e.get("depth")), _round(e.get("arrival")),
                             _round(b.get("depth")), charged_txt, note or NULL,
                             r.get("basis") or NULL, r["source"]])
        feats.append(f)
    lyr.dataProvider().addFeatures(feats)
    lyr.updateExtents()
    return lyr


def describe(recs, ev, n=4, dam=False):
    """Short list for the summary: 'Name (NID, High, 3.2 ft)'."""
    items = []
    for r in recs[:n]:
        d = (ev.get(r["id"]) or {}).get("depth")
        extra = [r.get("nid")] if dam and r.get("nid") else []
        if dam and r.get("hazard"):
            extra.append("{} hazard".format(r["hazard"]))
        if not dam:
            extra.append(r["category"])
            if r.get("near_subject"):
                extra.append("at the dam - may be in the reservoir")
            if r.get("kind") == KIND_CROSSING:
                if r.get("crosses"):
                    extra.append("over " + r["crosses"])
                if r.get("adt"):
                    extra.append("ADT {:,.0f}".format(r["adt"]))
        if d is not None:
            extra.append("{:.1f} ft".format(d))
        items.append("{} ({})".format(r["name"], ", ".join(extra)))
    more = "" if len(recs) <= n else " and {} more".format(len(recs) - n)
    return "; ".join(items) + more


def is_high_hazard(rec):
    return (rec.get("hazard") or "").strip().lower().startswith("h")


def log_counts(records):
    dams = sum(1 for r in records if r["kind"] == KIND_DAM)
    lif = sum(1 for r in records if r["kind"] == KIND_LIFELINE)
    cr = sum(1 for r in records if r["kind"] == KIND_CROSSING)
    log_message("Facilities in the study area: {} dam(s), {} lifeline / critical "
                "facilit(ies), {} bridge(s) / culvert(s) / crossing(s)".format(dams, lif, cr))
