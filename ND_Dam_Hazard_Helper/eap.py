# -*- coding: utf-8 -*-
"""Emergency Action Plan (EAP) outputs for a breach scenario.

* EAP_Notification_List: every place to warn, in the order the flood gets
  there - flooded structures that are (or may be) occupied, dry ones whose
  road access is cut, flooded lifeline facilities, other dams (their owner /
  operator) and overtopped roads (closures). Address (ND GIS Hub NENA
  address first, then the FEMA_Structures PROP_ADDR ...), persons, arrival
  time, depth, ACER zone, latitude / longitude. GPKG + CSV + XLSX.
* EAP_Arrival_Bands.tif: the flooded area classed by arrival time
  (EAP_ARRIVAL_BREAKS_HR) for the inundation maps.
* EAP_Arrival_Contours.gpkg: arrival-time lines at the same breaks (GDAL
  contouring; skipped where the GDAL build has none).

Arrival is the scenario's arrival-time raster (hours after breach start).
Items without an arrival time are listed last.
"""

import os
import re

import numpy as np
from osgeo import gdal, ogr, osr
from qgis.core import (
    QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsFeature, QgsField, QgsGeometry,
    QgsPointXY,
)

from .compat import WKB_POINT, FT_DOUBLE, FT_INT, FT_STRING, NULL, GEOM_POINT
from .constants import (
    EAP_ARRIVAL_BREAKS_HR, ZONE_LABELS, CLASS_NODATA,
)
from .helpers import create_memory_layer, log_message, prepare_output_path, gdal_exceptions
from . import facilities as FAC

FIELDS = [("Order", FT_INT), ("Arrival_hr", FT_DOUBLE), ("Arrival_hhmm", FT_STRING),
          ("Type", FT_STRING), ("Priority", FT_STRING), ("Category", FT_STRING),
          ("Name", FT_STRING),
          ("Address", FT_STRING), ("City", FT_STRING), ("ZIP", FT_STRING),
          ("Persons", FT_INT), ("Max_Depth_ft", FT_DOUBLE), ("ACER_Zone", FT_STRING),
          ("ND_Outcome", FT_STRING), ("Charged", FT_STRING), ("Ref_ID", FT_STRING),
          ("Lat", FT_DOUBLE), ("Lon", FT_DOUBLE)]


def hhmm(hours):
    if hours is None:
        return ""
    m = int(round(float(hours) * 60.0))
    return "{}:{:02d}".format(m // 60, m % 60)


def _point(geom):
    try:
        if geom.type() == GEOM_POINT:
            return geom.asPoint()
        return geom.pointOnSurface().asPoint()
    except Exception:
        try:
            return geom.centroid().asPoint()
        except Exception:
            return None


# v2.5: notification priority. 1 = people who cannot evacuate on their own
# (schools, child care, care homes, hospitals), 2 = emergency response and
# utility lifelines; the list itself stays sorted by arrival time.
PRIORITY_VULNERABLE = "1 - vulnerable occupants"
PRIORITY_LIFELINE = "2 - emergency response / lifeline"
_VULNERABLE_RE = re.compile(
    r"\b(school|schools|education|kindergarten|preschool|pre school|daycare|day care|child ?care|"
    r"nursery school|nursing|assisted living|basic care|care home|hospital|edu1|edu2|"
    r"res6|com6)\b", re.I)


def structure_priority(rec):
    """Priority of a structure from its sources' descriptions / NSI codes."""
    texts = " ; ".join(t for s in rec.get("sources", ()) for t in s.get("texts", ()))
    texts = re.sub(r"[_\-/]+", " ", "{} ; {}".format(texts, rec.get("occupancy") or ""))
    if _VULNERABLE_RE.search(texts):
        return PRIORITY_VULNERABLE
    if rec.get("lifeline"):
        return PRIORITY_LIFELINE
    return ""


def facility_priority(r):
    if r.get("kind") == FAC.KIND_LIFELINE:
        if r.get("category") in FAC.VULNERABLE:
            return PRIORITY_VULNERABLE
        if r.get("lifeline"):
            return PRIORITY_LIFELINE
    return ""


def notification_rows(structs, res):
    """Rows (dicts with a "pt" in the analysis CRS) for one breach scenario;
    `structs` must hold this scenario's results (restored snapshot)."""
    from .structures import address_of, OUT_DRY, CAT_NOT_HABITABLE
    rows = []
    for rec in structs:
        if rec.get("category") == CAT_NOT_HABITABLE or rec.get("habitable") == "No":
            continue
        depth = rec.get("max_depth")
        wet = depth is not None and depth > 0 and rec.get("outcome", OUT_DRY) != OUT_DRY
        if not wet and not rec.get("cut_off"):
            continue
        addr, city, zp = address_of(rec)
        inc = rec.get("incremental")
        rows.append({
            "type": "Structure" if wet else "Structure - road access cut",
            "priority": structure_priority(rec),
            # v2.5.1: FEMA occupancy for reference (not the habitability rule)
            "category": (rec.get("fema_category") or "")[:120],
            "name": (rec.get("occupancy") or "")[:120],
            "address": addr, "city": city, "zip": zp, "persons": int(rec.get("persons") or 0),
            "arrival": rec.get("arrival"), "depth": depth if wet else None,
            "zone": ZONE_LABELS.get(rec.get("zone"), "") if wet else "",
            "outcome": rec.get("outcome") or "",
            "charged": "" if inc is None else ("Yes" if inc else "No"),
            "ref": "Struct {}".format(rec["id"]), "pt": _point(rec["geom"])})
    recs = res.get("facility_records") or []
    ev = res.get("fac_eval") or {}
    charged = res.get("fac_charged") or {}
    for r in FAC.flooded(recs, ev, None):
        e = ev.get(r["id"]) or {}
        ch = charged.get(r["id"])
        dam = r["kind"] == FAC.KIND_DAM
        if r["kind"] == FAC.KIND_CROSSING:
            rows.append({
                "type": "Bridge / culvert / crossing - check, close if overtopped",
                "priority": "",
                "category": r["category"], "name": r["name"],
                "address": r.get("carried") or "", "city": "", "zip": "",
                "persons": 0, "arrival": e.get("arrival"), "depth": e.get("depth"),
                "zone": "", "outcome": ("ADT {:,.0f}".format(r["adt"]) if r.get("adt") else ""),
                "charged": "" if ch is None else ("Yes" if ch[0] else "No"),
                "ref": r.get("struct_no") or r["source"], "pt": _point(r["geom"])})
            continue
        rows.append({
            "type": "Dam - notify owner / operator" if dam else "Lifeline / critical facility",
            "priority": facility_priority(r),
            "category": ("{} hazard dam".format(r["hazard"]) if dam and r.get("hazard")
                         else r["category"]),
            "name": r["name"], "address": "", "city": "", "zip": "", "persons": 0,
            "arrival": e.get("arrival"), "depth": e.get("depth"), "zone": "", "outcome": "",
            "charged": "" if ch is None else ("Yes" if ch[0] else "No"),
            "ref": (r.get("nid") or "NID ?") if dam else r["source"],
            "pt": _point(r["geom"])})
    for rd in res.get("roads") or []:
        g = rd.get("geom")
        inc = rd.get("incremental")
        rows.append({
            "type": "Road - closure", "priority": "", "category": rd.get("road_class") or "",
            "name": rd.get("name") or "unnamed road", "address": "", "city": "", "zip": "",
            "persons": 0, "arrival": rd.get("arrival"), "depth": rd.get("max_depth"),
            "zone": ZONE_LABELS.get(rd.get("vehicle_zone"), "") if rd.get("vehicle_zone")
            else "", "outcome": rd.get("nd_class") or "",
            "charged": "" if inc is None else ("Yes" if inc else "No"),
            "ref": "{:.0f} ft overtopped".format(rd.get("length_ft") or 0),
            "pt": _point(g) if g is not None else None})
    rows = [r for r in rows if r["pt"] is not None]
    rows.sort(key=lambda r: (r["arrival"] is None, r["arrival"] if r["arrival"] is not None
                             else 0.0, -(r["depth"] or 0.0)))
    return rows


def notification_layer(structs, res, crs, transform_ctx, name="EAP_Notification_List"):
    rows = notification_rows(structs, res)
    if not rows:
        return None
    lyr = create_memory_layer(name, WKB_POINT, crs, [QgsField(n, t) for n, t in FIELDS])
    to_ll = QgsCoordinateTransform(crs, QgsCoordinateReferenceSystem("EPSG:4326"),
                                   transform_ctx)

    def rnd(v, n=2):
        return NULL if v is None else round(float(v), n)
    feats = []
    for i, r in enumerate(rows):
        pt = r["pt"]
        try:
            ll = to_ll.transform(QgsPointXY(pt))
            lat, lon = round(ll.y(), 6), round(ll.x(), 6)
        except Exception:
            lat = lon = NULL
        f = QgsFeature(lyr.fields())
        f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(pt)))
        f.setAttributes([i + 1, rnd(r["arrival"]), hhmm(r["arrival"]) or NULL, r["type"],
                         r.get("priority") or NULL, r["category"] or NULL, r["name"] or NULL,
                         r["address"] or NULL,
                         r["city"] or NULL, r["zip"] or NULL, int(r["persons"]),
                         rnd(r["depth"]), r["zone"] or NULL, r["outcome"] or NULL,
                         r["charged"] or NULL, r["ref"] or NULL, lat, lon])
        feats.append(f)
    lyr.dataProvider().addFeatures(feats)
    lyr.updateExtents()
    return lyr


def summary_lines(rows_layer, bands_path, contours_path):
    out = []
    if rows_layer is not None:
        n = rows_layer.featureCount()
        first = next(iter(rows_layer.getFeatures()), None)
        t = ""
        if first is not None and first["Arrival_hhmm"] not in (None, NULL, ""):
            t = "; first arrival {} h:min after breach start ({})".format(
                first["Arrival_hhmm"], first["Type"].lower())
        out.append("Notification list: {} item(s) sorted by arrival time{} - "
                   "EAP/{}.csv / .xlsx".format(n, t, rows_layer.name()))
        try:
            pri = [f for f in rows_layer.getFeatures()
                   if str(f["Priority"] or "").startswith("1")]
        except Exception:
            pri = []
        if pri:
            out.append("{} priority notification(s) - vulnerable occupants (school, child "
                       "care, care home, hospital): {}".format(len(pri), "; ".join(
                           "#{} {}{}".format(f["Order"], f["Name"] or f["Category"] or "",
                                             " ({} h:min)".format(f["Arrival_hhmm"])
                                             if f["Arrival_hhmm"] not in (None, NULL, "")
                                             else "") for f in pri[:6]) +
                       ("" if len(pri) <= 6 else " and {} more".format(len(pri) - 6))))
    if bands_path:
        out.append("Arrival-time bands (h): {} - EAP/{}".format(
            band_labels_text(), os.path.basename(bands_path)))
    if contours_path:
        out.append("Arrival-time contours: EAP/{}".format(os.path.basename(contours_path)))
    return out


# ----------------------------------------------------------- arrival maps --
def band_labels():
    """[(class value, label)]: 1 = up to the first break ... last = beyond."""
    b = EAP_ARRIVAL_BREAKS_HR
    out = [(1, "0 - {:g} h".format(b[0]))]
    for i in range(1, len(b)):
        out.append((i + 1, "{:g} - {:g} h".format(b[i - 1], b[i])))
    out.append((len(b) + 1, "> {:g} h".format(b[-1])))
    return out


BAND_COLORS = ("#b2182b", "#e34a33", "#fc8d59", "#fdbb84", "#91bfdb", "#4575b4", "#313695")


def style_arrival_bands(lyr):
    from .helpers import apply_class_style
    items = [(v, BAND_COLORS[min(v - 1, len(BAND_COLORS) - 1)], "Arrival " + lbl)
             for v, lbl in band_labels()]
    return apply_class_style(lyr, items)


def band_labels_text():
    return ", ".join(lbl for _v, lbl in band_labels())


def write_arrival_bands(path, depth_path, arrival_path):
    """Int16 GeoTIFF on the depth grid: arrival class where depth > 0 and the
    arrival is known (NoData elsewhere). Returns the path or None."""
    from .hazard import _Grid
    from .processing import _create_class_raster, _finalize_class_raster, _rows_per_block
    if not depth_path or not arrival_path:
        return None
    ds_d = gdal.Open(depth_path, gdal.GA_ReadOnly)
    if ds_d is None:
        return None
    breaks = np.asarray(EAP_ARRIVAL_BREAKS_HR, dtype=np.float64)
    gd = ga = out = None
    path = prepare_output_path(path)
    try:
        gd, ga = _Grid(ds_d, depth_path), _Grid(ds_d, arrival_path)
        w, h = ds_d.RasterXSize, ds_d.RasterYSize
        out = _create_class_raster(gdal.GetDriverByName("GTiff"), path, w, h,
                                   ds_d.GetGeoTransform(), ds_d.GetProjection())
        band = out.GetRasterBand(1)
        rows = _rows_per_block(w, h)
        n = 0
        for y0 in range(0, h, rows):
            hh = min(rows, h - y0)
            d, dv = gd.read(y0, hh, w)
            a, av = ga.read(y0, hh, w)
            ok = dv & av & (d > 0) & (a >= 0)
            cls = np.full((hh, w), CLASS_NODATA, np.int16)
            cls[ok] = (np.searchsorted(breaks, a[ok], side="left") + 1).astype(np.int16)
            n += int(ok.sum())
            band.WriteArray(cls, 0, y0)
        _finalize_class_raster(out, w, h)
        band = None
        out = None
        if not n:
            log_message("EAP arrival bands: no flooded cell has an arrival time")
        return path
    except Exception as e:
        log_message("EAP arrival bands not written: {}".format(e))
        band = None                     # the band keeps the dataset open (GDAL >= 3.8)
        out = None
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError:
            pass
        return None
    finally:
        for g in (gd, ga):
            if g is not None:
                g.close()
        ds_d = None


def write_arrival_contours(path, arrival_path):
    """Arrival-time lines (GDAL ContourGenerateEx, fixed levels) to a
    GeoPackage. None when GDAL cannot contour here."""
    if not arrival_path or not hasattr(gdal, "ContourGenerateEx"):
        return None
    drv = ogr.GetDriverByName("GPKG")
    if drv is None:
        return None
    path = prepare_output_path(path)
    try:
        with gdal_exceptions():
            src = gdal.Open(arrival_path, gdal.GA_ReadOnly)
            band = src.GetRasterBand(1)
            if os.path.exists(path):
                drv.DeleteDataSource(path)
            ds = drv.CreateDataSource(path)
            srs = osr.SpatialReference()
            wkt = src.GetProjection()
            if wkt:
                srs.ImportFromWkt(wkt)
            lyr = ds.CreateLayer("arrival_contours", srs if wkt else None, ogr.wkbLineString)
            lyr.CreateField(ogr.FieldDefn("ID", ogr.OFTInteger))
            lyr.CreateField(ogr.FieldDefn("Arrival_hr", ogr.OFTReal))
            opts = ["FIXED_LEVELS=" + ",".join("{:g}".format(b) for b in EAP_ARRIVAL_BREAKS_HR),
                    "ID_FIELD=0", "ELEV_FIELD=1"]
            nd = band.GetNoDataValue()
            if nd is not None:
                opts.append("NODATA={}".format(nd))
            lyr.StartTransaction()
            gdal.ContourGenerateEx(band, lyr, options=opts)
            lyr.CommitTransaction()
            n = lyr.GetFeatureCount()
            ds = lyr = band = src = None
        if not n:
            os.remove(path)
            return None
        return path
    except Exception as e:
        log_message("EAP arrival contours not written: {}".format(e))
        return None


# ------------------------------------------- 2.30.0: the one-page EAP map layout --
# The map book (mapbook.py) is the set of sheets an EAP needs for field use; this is the ONE
# page the dam owner and the county emergency manager are handed: title block, the dam, the
# inundation shaded by arrival time, the structures and roads in it, the legend and the data
# vintages. It is filled from the dam folder in one click, kept in the project's Layout Manager
# so it can be edited, and exported to PDF and GeoPDF (the GeoPDF carries the coordinates, so
# the sheet can be used on a tablet in the field).
LAYOUT_PDF = "EAP_Map.pdf"
LAYOUT_GEOPDF = "EAP_Map_GeoPDF.pdf"
LAYOUT_TITLE = "ND Dam EAP map"
# role -> (file name under the outputs folder, raster?) - the layers the sheet draws, in
# drawing order (first = on top). The names are the pipeline's output contracts.
LAYOUT_LAYERS = (
    ("notifications", os.path.join("EAP", "EAP_Notification_List.gpkg"), False),
    ("structures", os.path.join("Classification", "Habitable_Structures.gpkg"), False),
    ("access", os.path.join("Classification", "Access_Cut_Off.gpkg"), False),
    ("lifelines", os.path.join("Classification", "Lifeline_Facilities.gpkg"), False),
    ("roads", os.path.join("Classification", "Roads_Inundated.gpkg"), False),
    ("rail", os.path.join("Classification", "Rail_Inundated.gpkg"), False),
    ("contours", os.path.join("EAP", "EAP_Arrival_Contours.gpkg"), False),
    ("bands", os.path.join("EAP", "EAP_Arrival_Bands.tif"), True),
)
VINTAGE_FILE = os.path.join("QA", "Data_Vintage.csv")
SUMMARY_FILE = "Hazard_Classification_Summary.txt"


def layout_sources(dam_folder, output_folder=None, depth=None, find=None):
    """What the one-page layout can draw for this dam: {"layers": {role: path}, "depth": path,
    "vintage": [line], "class": "High", "missing": [role], "output_folder": path}.

    Only the file system is read (no QGIS), so a window, a Processing algorithm and a test can
    all ask the same question. `find`: autodetect.find_rasters, passed in by the caller that
    has it; `depth`: the depth raster when the caller already knows it."""
    dam_folder = dam_folder or ""
    out = output_folder or os.path.join(dam_folder, "outputs")
    got = {"layers": {}, "depth": depth or "", "vintage": [], "class": "", "missing": [],
           "output_folder": out, "summary": ""}
    for role, rel, _raster in LAYOUT_LAYERS:
        path = os.path.join(out, rel)
        if os.path.isfile(path):
            got["layers"][role] = path
        else:
            got["missing"].append(role)
    if not got["depth"] and find is not None:
        try:
            got["depth"] = (find(dam_folder) or {}).get("depth") or ""
        except Exception:                                        # noqa - never stop the sheet
            got["depth"] = ""
    vintage = os.path.join(out, VINTAGE_FILE)
    if os.path.isfile(vintage):
        try:
            import csv as _csv
            with open(vintage, encoding="utf-8-sig", newline="") as fh:
                for i, row in enumerate(_csv.reader(fh)):
                    if i == 0 or not row:
                        continue
                    got["vintage"].append(" - ".join(x for x in row[:3] if x))
        except (OSError, UnicodeDecodeError, ValueError):
            pass
    summary = os.path.join(out, SUMMARY_FILE)
    if os.path.isfile(summary):
        got["summary"] = summary
        try:
            with open(summary, encoding="utf-8") as fh:
                for line in fh:
                    if line.startswith("ND HAZARD CLASSIFICATION"):
                        got["class"] = line.split(":", 1)[-1].split("|")[0].strip()
                        break
        except (OSError, UnicodeDecodeError):
            pass
    return got


def layout_ready(sources):
    """(ok, why) - the sheet needs an inundation raster at least."""
    sources = sources or {}
    if sources.get("layers", {}).get("bands") or sources.get("depth"):
        return True, ""
    return False, ("No inundation raster for the map: run the breach model (step 3 of the "
                   "suite) and the classification first, or choose the Depth raster on "
                   "'Setup & Run'")


def layout_title_lines(dam_name, hazard_class="", scenario="", when=None, plugin="",
                       version="", vintage=(), max_vintage=6):
    """(title, subtitle, vintage block) of the sheet's title block."""
    import time as _time
    when = when or _time.strftime("%Y-%m-%d")
    title = "{} - dam-break inundation, Emergency Action Plan".format(dam_name or "dam")
    bits = []
    if hazard_class:
        bits.append("ND hazard class (screening): {}".format(str(hazard_class).upper()))
    if scenario:
        bits.append("breach scenario: {}".format(scenario))
    bits.append("prepared {}".format(when))
    sub = "   |   ".join(bits)
    lines = ["Data vintages"]
    rows = [v for v in (vintage or ()) if str(v).strip()]
    for v in rows[:max_vintage]:
        lines.append("  " + str(v))
    if len(rows) > max_vintage:
        lines.append("  ... {} more in QA/Data_Vintage.csv".format(len(rows) - max_vintage))
    if not rows:
        lines.append("  not recorded - run the classification to write QA/Data_Vintage.csv")
    lines.append("")
    lines.append("Screening result produced by {}{}. The engineer of record remains "
                 "responsible for the classification. Arrival times are hours after the "
                 "breach starts.".format(plugin or "the ND Dam Hazard Classification Helper",
                                         " v" + version if version else ""))
    return title, sub, "\n".join(lines)
