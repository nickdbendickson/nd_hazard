# -*- coding: utf-8 -*-
"""v2.5: future-development screening (ND REG_05.2026 5.3(b)).

Vacant parcels (no habitable or unverified structure on them) up to a size
limit that lie in the ACER danger zone that would make the dam HIGH if a
house were built there: the high-danger zone, or the judgment zone when
judgment-zone structures are counted as probable loss of life (depth > 2 ft
for a 1D model). Each parcel is evaluated like a building outline (cell by
cell over the parcel and its 5-ft edge ring) with the house-on-foundation
curve. The list is a judgment aid - zoning, platting and access decide
whether development is imminent; it never changes the class.
"""

from qgis.core import QgsCoordinateTransform, QgsDistanceArea, QgsFeature, QgsField

from .compat import WKB_MULTIPOLYGON, FT_DOUBLE, FT_STRING, FT_INT, NULL
from .constants import ZONE_LABELS
from .helpers import (
    create_memory_layer, transform_geometry, log_message, is_null, add_features_batched,
    finite_area_ft2,
)

SQFT_PER_ACRE = 43560.0
_REF_FIELDS = ("parcel_id", "parcelid", "parcel", "parcelnum", "parcel_num", "pin", "apn",
               "pid", "tax_id", "taxid", "parcel_no", "parcelno")
_USE_HINTS = ("landuse", "land_use", "zoning", "zone", "use_desc", "usedesc", "propclass",
              "prop_class", "class_desc", "lu_desc")
FIELDS = [("Parcel_Ref", FT_STRING), ("Land_Use", FT_STRING), ("Acres", FT_DOUBLE),
          ("Max_Depth_ft", FT_DOUBLE), ("Max_Vel_fps", FT_DOUBLE), ("ACER_Zone", FT_INT),
          ("ACER_Zone_Label", FT_STRING), ("If_Built", FT_STRING)]


def _field(names_low, wanted, contains=False):
    for w in wanted:
        if w in names_low:
            return names_low.index(w)
    if contains:
        for i, n in enumerate(names_low):
            if any(w in n for w in wanted):
                return i
    return -1


def screen(parcel_layer, structs, samplers, analysis_crs, transform_ctx, clip=None, lat=None,
           max_acres=5.0, judgment_as_probable=True, one_d=False, progress_cb=None):
    """(layer or None, summary dict). `samplers`: the governing scenario's
    open samplers (as for evaluate_structures)."""
    from .structures import (
        evaluate_structures, TYPE_HOUSE, OUT_PROBABLE, OUT_JUDGMENT, _Grid, _ft_units,
        _request_for,
    )
    summary = {"checked": 0, "too_large": 0, "built": 0, "hits": 0, "acres": 0.0}
    if parcel_layer is None or not parcel_layer.isValid():
        return None, summary
    da = QgsDistanceArea()
    da.setSourceCrs(analysis_crs, transform_ctx)
    try:
        da.setEllipsoid(analysis_crs.ellipsoidAcronym() or "EPSG:7019")
    except Exception:
        pass
    names = [f.name() for f in parcel_layer.fields()]
    low = [n.lower() for n in names]
    i_ref = _field(low, _REF_FIELDS)
    i_use = _field(low, _USE_HINTS, contains=True)
    # structures that make a parcel "built": habitable or unverified ones
    grid = _Grid(_ft_units(200.0, analysis_crs, lat))
    for s in structs:
        # a campground area is not a building on the parcel
        if s.get("habitable") in ("Yes", "Unverified") and s.get("geom") is not None \
                and not s.get("camp"):
            grid.insert_rect(s, s["geom"].boundingBox())
    xf = QgsCoordinateTransform(parcel_layer.crs(), analysis_crs, transform_ctx)
    a_to_clip = clip.transform_from(analysis_crs) if clip is not None else None
    cands = []
    for feat in parcel_layer.getFeatures(_request_for(parcel_layer, clip)):
        g = transform_geometry(feat.geometry(), xf)
        if g is None or g.isNull() or g.isEmpty():
            continue
        if clip is not None and not clip.intersects_geometry(g, xform=a_to_clip):
            continue
        summary["checked"] += 1
        try:
            if not g.isGeosValid():
                g = g.makeValid()
            acres = finite_area_ft2(da, g, analysis_crs, transform_ctx) / SQFT_PER_ACRE
        except Exception:
            continue
        if acres > max_acres:
            summary["too_large"] += 1
            continue
        bb = g.boundingBox()
        built = False
        k0 = grid._key(bb.xMinimum(), bb.yMinimum())
        k1 = grid._key(bb.xMaximum(), bb.yMaximum())
        seen = set()
        for i in range(k0[0], k1[0] + 1):
            for j in range(k0[1], k1[1] + 1):
                for s in grid.cells.get((i, j), ()):
                    if id(s) in seen:
                        continue
                    seen.add(id(s))
                    try:
                        if s["geom"].boundingBox().intersects(bb) and s["geom"].intersects(g):
                            built = True
                            break
                    except Exception:
                        continue
                if built:
                    break
            if built:
                break
        if built:
            summary["built"] += 1
            continue
        a = feat.attributes()
        ref = "" if i_ref < 0 or is_null(a[i_ref]) else str(a[i_ref])
        use = "" if i_use < 0 or is_null(a[i_use]) else str(a[i_use])
        cands.append({"id": len(cands) + 1, "geom": g, "footprint": True, "sqft": None,
                      "acer_type": TYPE_HOUSE, "ref": ref or "fid {}".format(feat.id()),
                      "use": use, "acres": acres})
    if not cands:
        return None, summary
    evaluate_structures(cands, samplers, analysis_crs, transform_ctx, one_d=one_d,
                        progress_cb=progress_cb, lat=lat, near_ft=0.0)
    trigger = {OUT_PROBABLE} | ({OUT_JUDGMENT} if judgment_as_probable and not one_d else set())
    hits = [c for c in cands if c.get("outcome") in trigger]
    summary["hits"] = len(hits)
    summary["acres"] = sum(c["acres"] for c in hits)
    summary["examples"] = [c["ref"] for c in hits[:6]]
    if not hits:
        return None, summary
    lyr = create_memory_layer("Future_Development_Parcels", WKB_MULTIPOLYGON, analysis_crs,
                              [QgsField(n, t) for n, t in FIELDS])
    feats = []
    for c in hits:
        g = c["geom"]
        try:
            if not g.isMultipart():
                from qgis.core import QgsGeometry
                g = QgsGeometry(g)
                g.convertToMultiType()
        except Exception:
            pass
        f = QgsFeature(lyr.fields())
        f.setGeometry(g)
        z = c.get("zone")
        f.setAttributes([c["ref"], c["use"] or NULL, round(c["acres"], 2),
                         NULL if c.get("max_depth") is None else round(c["max_depth"], 2),
                         NULL if c.get("max_vel") is None else round(c["max_vel"], 2),
                         NULL if z is None else int(z), ZONE_LABELS.get(z, "") or NULL,
                         "probable loss of life (dam HIGH)" if c.get("outcome") ==
                         OUT_PROBABLE else "judgment zone (HIGH when counted)"])
        feats.append(f)
    add_features_batched(lyr.dataProvider(), feats, 5000)
    lyr.updateExtents()
    log_message("Future development: {} vacant parcel(s) <= {:g} ac in the danger zone "
                "({:.1f} ac)".format(len(hits), max_acres, summary["acres"]))
    return lyr, summary


def summary_lines(summary, max_acres, layer_name="Future_Development_Parcels"):
    if not summary.get("checked"):
        return ["No parcels in the study area"]
    out = ["{} parcel(s) checked: {} larger than {:g} ac, {} with a habitable / unverified "
           "structure; {} vacant parcel(s) ({:.1f} ac) where a new house would be in the "
           "danger zone{}".format(summary["checked"], summary["too_large"], max_acres,
                                   summary["built"], summary["hits"], summary.get("acres", 0),
                                   ": " + ", ".join(summary.get("examples", [])) +
                                   (" ..." if summary["hits"] > 6 else "")
                                   if summary["hits"] else "")]
    if summary["hits"]:
        out.append("Layer {} - check zoning, platting and access: imminent development "
                   "there would make the dam HIGH (5.3(b))".format(layer_name))
    return out
