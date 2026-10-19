# -*- coding: utf-8 -*-
"""v2.9.0: KMZ (Google Earth) and GeoJSON (web maps) copies of the EAP layers.

export_layers(layers_or_paths, out_folder) writes, per vector layer:

* <name>.geojson - RFC 7946: WGS 84 longitude / latitude (EPSG:4326, the
  layer is reprojected with the project's transform context), coordinates
  rounded to GEOJSON_PRECISION decimals (about 1 cm), written by
  QgsVectorFileWriter with the OGR "GeoJSON" driver.
* <name>.kmz - a zip holding doc.kml, written by QgsVectorFileWriter with
  the OGR "KML" driver (LIBKML when KML fails). Each placemark gets a
  readable name (field KML_Name, see feature_label(): the notification list
  shows "12. 123 Main St (1:30)", contours "Arrival 2 h", map sheets "Sheet 3",
  other layers their Name / Address / Label field) and each layer a simple
  colour (arrival-band polygons: the colours of the arrival-band raster).

export_eap_web(eap_folder, dam_name) finds the EAP layers the run wrote
(outputs/EAP/EAP_*.gpkg - notification list, travel-time table, channel
centerline, arrival contours, map sheets -, EAP_Termination_Point.gpkg in
outputs/QA, and EAP_Arrival_Bands*.tif turned into polygons - the flooded
area by arrival time, i.e. the inundation boundary - where GDAL can) and
writes them to outputs/EAP/Web/, plus one KMZ with every layer as a folder.

One bad layer never stops the others: it is logged and its entry says why.
Nothing here changes the layers or the EAP files.
"""

import os
import re
import shutil
import tempfile
import zipfile
import xml.etree.ElementTree as ET

from qgis.core import (
    QgsVectorLayer, QgsVectorFileWriter, QgsCoordinateReferenceSystem, QgsCoordinateTransform,
    QgsCoordinateTransformContext, QgsFeatureRequest, QgsField,
)

from .compat import FT_STRING, WRITER_NO_ERROR, NULL, enum_value
from .helpers import log_message, prepare_output_path

WEB_SUBDIR = "Web"
GEOJSON_PRECISION = 7                    # decimals of a degree (RFC 7946 default)
KML_DRIVERS = ("KML", "LIBKML")
LABEL_FIELD = "KML_Name"
ALL_LAYERS_NAME = "EAP_All_Layers"
MAX_POLYGONIZE_CELLS = 60e6              # larger arrival-band rasters are not converted

KML_NS = "http://www.opengis.net/kml/2.2"
_NS = {"": KML_NS, "gx": "http://www.google.com/kml/ext/2.2",
       "atom": "http://www.w3.org/2005/Atom",
       "xal": "urn:oasis:names:tc:ciq:xsdschema:xAL:2.0"}

# the layers export_eap_web looks for, in this order (base-name prefixes)
EAP_LAYER_ORDER = ("EAP_Notification_List", "EAP_Travel_Time", "EAP_Termination_Point",
                   "EAP_Channel_Centerline", "EAP_Arrival_Contours", "EAP_Arrival_Bands",
                   "EAP_Map_Sheets")

# simple per-layer colours (#rrggbb); None = per feature (arrival bands)
LAYER_COLORS = (("EAP_Notification_List", "#d7301f"), ("EAP_Travel_Time", "#fd8d3c"),
                ("EAP_Termination_Point", "#6a3d9a"), ("EAP_Channel_Centerline", "#1f78b4"),
                ("EAP_Arrival_Contours", "#08519c"), ("EAP_Arrival_Bands", None),
                ("EAP_Map_Sheets", "#525252"))
DEFAULT_COLOR = "#e31a1c"
_OUTLINE_ONLY = ("EAP_Map_Sheets",)

_GENERIC_NAME_FIELDS = ("name", "label", "address", "prop_addr", "full_addr", "site_name",
                        "facility", "title", "item", "status", "id")

for _p, _u in _NS.items():
    try:
        ET.register_namespace(_p, _u)
    except ValueError:
        pass


def _log(log):
    return log or (lambda m: log_message(m))


def safe_name(name):
    """A file-name-safe version of a layer / dam name."""
    s = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(name or "")).strip("._")
    return s[:80] or "layer"


def _family(base):
    for pre in EAP_LAYER_ORDER:
        if base.startswith(pre):
            return pre
    return ""


# ------------------------------------------------------------------ labels --
def _txt(v):
    if v is None or v is NULL:
        return ""
    try:
        if v.isNull():                       # QVariant (Qt5 NULL)
            return ""
    except AttributeError:
        pass
    s = str(v).strip()
    return "" if s.upper() in ("NULL", "NONE") else s


def _num(v):
    s = _txt(v)
    try:
        f = float(s)
        return "{:g}".format(f)
    except ValueError:
        return s


def feature_label(base, attrs, fallback=""):
    """Placemark name of a feature of layer `base` (attrs: {field: value},
    any case)."""
    a = dict((str(k).lower(), v) for k, v in (attrs or {}).items())

    def g(k):
        return _txt(a.get(k.lower()))
    fam = _family(base)
    if fam == "EAP_Notification_List":
        what = g("Address") or g("Name") or g("Category") or g("Type")
        t = g("Arrival_hhmm")
        return "{}{}{}".format("{}. ".format(_num(a.get("order"))) if g("Order") else "",
                               what or fallback, " ({})".format(t) if t else "")
    if fam == "EAP_Travel_Time":
        what = g("Name") or g("Item")
        rm = _num(a.get("river_mile"))
        t = g("Arrival_hhmm")
        return "{}{}{}".format("RM {}: ".format(rm) if rm else "", what or fallback,
                               " ({})".format(t) if t else "")
    if fam == "EAP_Arrival_Contours":
        v = _num(a.get("arrival_hr"))
        return "Arrival {} h".format(v) if v else (fallback or "Arrival contour")
    if fam == "EAP_Arrival_Bands":
        lbl = g("Arrival")
        if not lbl:
            try:
                from .eap import band_labels
                lbl = dict(band_labels()).get(int(float(g("Class"))), "")
            except (ValueError, ImportError):
                lbl = ""
        return "Arrival {}".format(lbl) if lbl else (fallback or "Flooded area")
    if fam == "EAP_Map_Sheets":
        s = _num(a.get("sheet"))
        return "Sheet {}".format(s) if s else fallback
    if fam == "EAP_Termination_Point":
        st, rm = g("Status"), _num(a.get("river_mile"))
        return "Termination point{}{}".format(" - {}".format(st) if st else "",
                                               " (RM {})".format(rm) if rm else "")
    if fam == "EAP_Channel_Centerline":
        return "Channel centerline" + (" ({})".format(g("Source")) if g("Source") else "")
    for k in _GENERIC_NAME_FIELDS:
        v = g(k)
        if v:
            return v
    return fallback


def _labelled_copy(layer, base):
    """Memory copy of `layer` with the placemark name in LABEL_FIELD (the
    layer itself is not changed)."""
    mem = layer.materialize(QgsFeatureRequest())
    idx = mem.fields().indexOf(LABEL_FIELD)
    if idx < 0:
        mem.dataProvider().addAttributes([QgsField(LABEL_FIELD, FT_STRING)])
        mem.updateFields()
        idx = mem.fields().indexOf(LABEL_FIELD)
    names = [f.name() for f in mem.fields()]
    changes = {}
    title = base.replace("_", " ")
    for i, f in enumerate(mem.getFeatures()):
        attrs = dict(zip(names, f.attributes()))
        changes[f.id()] = {idx: feature_label(base, attrs, "{} {}".format(title, i + 1))[:250]}
    if changes:
        mem.dataProvider().changeAttributeValues(changes)
    return mem


# ----------------------------------------------------------------- writing --
def _ctx():
    try:
        from qgis.core import QgsProject
        return QgsProject.instance().transformContext()
    except Exception:
        return QgsCoordinateTransformContext()


def _to_wgs84(layer, ctx):
    """QgsCoordinateTransform layer CRS -> EPSG:4326 (None: already 4326)."""
    dst = QgsCoordinateReferenceSystem("EPSG:4326")
    src = layer.crs()
    if src is None or not src.isValid():
        raise ValueError("the layer has no CRS - it cannot be placed on a web map")
    if src.authid() == "EPSG:4326":
        return None
    return QgsCoordinateTransform(src, dst, ctx)


def _write(layer, path, ctx, driver, ct, layer_name, layer_opts=(), ds_opts=()):
    opts = QgsVectorFileWriter.SaveVectorOptions()
    opts.driverName = driver
    opts.fileEncoding = "UTF-8"
    opts.layerName = layer_name
    opts.layerOptions = list(layer_opts)
    opts.datasourceOptions = list(ds_opts)
    if ct is not None:
        opts.ct = ct
    res = QgsVectorFileWriter.writeAsVectorFormatV3(layer, path, ctx, opts)
    code = res[0] if isinstance(res, tuple) else res
    if enum_value(code) != enum_value(WRITER_NO_ERROR):
        msg = res[1] if isinstance(res, tuple) and len(res) > 1 else ""
        raise RuntimeError("{} writer: {} ({})".format(driver, msg or "failed",
                                                       enum_value(code)))
    return path


def write_geojson(layer, out_path, ctx=None):
    """RFC 7946 GeoJSON (EPSG:4326, GEOJSON_PRECISION decimals). Returns the
    path written (another name when the file was locked)."""
    ctx = ctx or _ctx()
    ct = _to_wgs84(layer, ctx)
    path = prepare_output_path(out_path)
    return _write(layer, path, ctx, "GeoJSON", ct,
                  os.path.splitext(os.path.basename(out_path))[0],
                  layer_opts=("RFC7946=YES", "COORDINATE_PRECISION={}".format(GEOJSON_PRECISION),
                              "WRITE_BBOX=NO"))


def write_kmz(layer, out_path, base=None, ctx=None):
    """KMZ (zip with doc.kml) of the layer, placemarks named by
    feature_label(), coloured per layer. Returns the path written."""
    ctx = ctx or _ctx()
    base = base or os.path.splitext(os.path.basename(out_path))[0]
    ct = _to_wgs84(layer, ctx)
    folder = os.path.dirname(os.path.abspath(out_path))
    os.makedirs(folder, exist_ok=True)
    work = tempfile.mkdtemp(prefix="_kml_", dir=folder)
    try:
        src = _labelled_copy(layer, base)
        kml = os.path.join(work, "doc.kml")
        errors = []
        title = _family(base).replace("_", " ") or base.replace("_", " ")
        for driver in KML_DRIVERS:
            try:
                _write_kml(src, kml, ctx, driver, ct, title)
                break
            except Exception as e:                  # noqa - next driver
                errors.append("{}: {}".format(driver, e))
                if os.path.exists(kml):
                    os.remove(kml)
        else:
            raise RuntimeError("; ".join(errors))
        style_kml(kml, base)
        path = prepare_output_path(out_path)
        tmp = path + ".part"
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
            z.write(kml, "doc.kml")
        os.replace(tmp, path)
        return path
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _write_kml(layer, path, ctx, driver, ct, title):
    if driver == "LIBKML":
        try:
            from osgeo import gdal
            old = gdal.GetConfigOption("LIBKML_NAME_FIELD")
            gdal.SetConfigOption("LIBKML_NAME_FIELD", LABEL_FIELD)
        except Exception:
            gdal = old = None
        try:
            return _write(layer, path, ctx, driver, ct, title)
        finally:
            if gdal is not None:
                gdal.SetConfigOption("LIBKML_NAME_FIELD", old)
    return _write(layer, path, ctx, driver, ct, title,
                  ds_opts=("NameField={}".format(LABEL_FIELD),))


# ------------------------------------------------------------------ styling --
def _kml_color(rgb, alpha=255):
    """#rrggbb -> KML aabbggrr."""
    h = rgb.lstrip("#")
    return "{:02x}{}{}{}".format(int(alpha), h[4:6], h[2:4], h[0:2])


def _style_el(sid, rgb, outline_only=False):
    st = ET.Element("{%s}Style" % KML_NS, {"id": sid})
    for tag, kids in (("IconStyle", (("color", _kml_color(rgb)), ("scale", "1.1"))),
                      ("LabelStyle", (("scale", "0.8"),)),
                      ("LineStyle", (("color", _kml_color(rgb)), ("width", "2.5"))),
                      ("PolyStyle", (("color", _kml_color(rgb, 0 if outline_only else 102)),
                                     ("fill", "0" if outline_only else "1"),
                                     ("outline", "1")))):
        e = ET.SubElement(st, "{%s}%s" % (KML_NS, tag))
        for k, v in kids:
            ET.SubElement(e, "{%s}%s" % (KML_NS, k)).text = v
    return st


_LEAD = ("name", "visibility", "open", "address", "phoneNumber", "Snippet", "description")


def _insert_after_lead(parent, el):
    i = 0
    for i, child in enumerate(list(parent)):
        if child.tag.split("}")[-1] not in _LEAD:
            break
    else:
        i = len(parent)
    parent.insert(i, el)


def _band_class(pm):
    for sd in pm.iter("{%s}SimpleData" % KML_NS):
        if sd.get("name", "").lower() == "class":
            try:
                return int(float(sd.text or ""))
            except ValueError:
                return None
    return None


def style_kml(kml_path, base):
    """Give the placemarks of a KML file written by OGR a colour (Style +
    styleUrl). Best effort: the file stays as it is when it cannot be parsed."""
    try:
        tree = ET.parse(kml_path)
        root = tree.getroot()
        doc = root.find("{%s}Document" % KML_NS)
        if doc is None:
            return False
        fam = _family(base)
        color = dict(LAYER_COLORS).get(fam, DEFAULT_COLOR) if fam else DEFAULT_COLOR
        sid = "s_" + safe_name(base)
        styles = []
        if fam == "EAP_Arrival_Bands":
            from .eap import BAND_COLORS
            per = {}
            for i, rgb in enumerate(BAND_COLORS):
                per[i + 1] = "{}_{}".format(sid, i + 1)
                styles.append(_style_el(per[i + 1], rgb))
            styles.append(_style_el(sid, DEFAULT_COLOR))
        else:
            per = None
            styles.append(_style_el(sid, color, outline_only=fam in _OUTLINE_ONLY))
        for st in reversed(styles):
            _insert_after_lead(doc, st)
        for pm in root.iter("{%s}Placemark" % KML_NS):
            if pm.find("{%s}styleUrl" % KML_NS) is not None:
                continue
            use = sid
            if per is not None:
                use = per.get(_band_class(pm), sid)
            su = ET.Element("{%s}styleUrl" % KML_NS)
            su.text = "#" + use
            _insert_after_lead(pm, su)
        tree.write(kml_path, encoding="utf-8", xml_declaration=True)
        return True
    except Exception:                                   # noqa - colours are optional
        return False


def merge_kmz(kmz_paths, out_path, title):
    """One KMZ with the documents of several KMZ files as folders (styles and
    schemas kept). Returns the path, or None when nothing could be merged."""
    doc = ET.Element("{%s}Document" % KML_NS)
    ET.SubElement(doc, "{%s}name" % KML_NS).text = title
    heads, folders = [], []
    for p in kmz_paths:
        try:
            with zipfile.ZipFile(p) as z:
                root = ET.fromstring(z.read("doc.kml"))
            d = root.find("{%s}Document" % KML_NS)
            if d is None:
                continue
            kids = [c for c in d if c.tag.split("}")[-1] not in ("name", "open")]
            heads += [c for c in kids if c.tag.split("}")[-1] in ("Style", "StyleMap", "Schema")]
            body = [c for c in kids if c.tag.split("}")[-1] not in ("Style", "StyleMap",
                                                                    "Schema")]
            if len(body) == 1 and body[0].tag.split("}")[-1] == "Folder":
                folders.append(body[0])
            elif body:
                f = ET.Element("{%s}Folder" % KML_NS)
                ET.SubElement(f, "{%s}name" % KML_NS).text = \
                    os.path.splitext(os.path.basename(p))[0]
                f.extend(body)
                folders.append(f)
        except Exception:                               # noqa - skip that one
            continue
    if not folders:
        return None
    doc.extend(heads + folders)
    root = ET.Element("{%s}kml" % KML_NS)
    root.append(doc)
    path = prepare_output_path(out_path)
    tmp = path + ".part"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("doc.kml", b'<?xml version="1.0" encoding="utf-8"?>\n' +
                   ET.tostring(root, encoding="utf-8"))
    os.replace(tmp, path)
    return path


# ------------------------------------------------------------------ public --
def _items(layers_or_paths):
    """[(name, layer or path)] from layers, paths, (name, x) pairs or a dict."""
    if isinstance(layers_or_paths, dict):
        return list(layers_or_paths.items())
    if isinstance(layers_or_paths, (str, QgsVectorLayer)):
        layers_or_paths = [layers_or_paths]
    out = []
    for it in layers_or_paths or []:
        if isinstance(it, (tuple, list)) and len(it) == 2:
            out.append((str(it[0]), it[1]))
        elif isinstance(it, str):
            out.append((os.path.splitext(os.path.basename(it.split("|")[0]))[0], it))
        else:
            try:
                out.append((it.name(), it))
            except Exception:
                out.append(("layer", it))
    return out


def export_layers(layers_or_paths, out_folder, formats=("kmz", "geojson"), name_prefix="",
                  log=None):
    """KMZ and / or GeoJSON copies of vector layers (QgsVectorLayer, file paths,
    (name, layer-or-path) pairs or {name: layer-or-path}) in out_folder.
    Returns {name: {"kmz": path or None, "geojson": path or None,
    "features": n, "error": "" or why}} - name without the prefix. Never
    raises for one layer."""
    log = _log(log)
    result = {}
    try:
        os.makedirs(out_folder, exist_ok=True)
    except OSError as e:
        log("Web export: folder {} cannot be made ({})".format(out_folder, e))
        return result
    ctx = _ctx()
    formats = tuple(f.lower() for f in (formats or ()))
    for name, src in _items(layers_or_paths):
        base = name
        k = 2
        while base in result:
            base = "{}_{}".format(name, k)
            k += 1
        entry = result[base] = {"kmz": None, "geojson": None, "features": 0, "error": ""}
        try:
            lyr = src
            if isinstance(src, str):
                lyr = QgsVectorLayer(src, base, "ogr")
            if lyr is None or not lyr.isValid():
                raise ValueError("cannot be opened")
            entry["features"] = n = lyr.featureCount()
            if not n:
                raise ValueError("no features")
            fname = safe_name(name_prefix + base)
            if "geojson" in formats:
                try:
                    entry["geojson"] = write_geojson(
                        lyr, os.path.join(out_folder, fname + ".geojson"), ctx)
                except Exception as e:              # noqa
                    entry["error"] += "GeoJSON: {}; ".format(e)
            if "kmz" in formats:
                try:
                    entry["kmz"] = write_kmz(lyr, os.path.join(out_folder, fname + ".kmz"),
                                             base=base, ctx=ctx)
                except Exception as e:              # noqa
                    entry["error"] += "KMZ: {}; ".format(e)
            entry["error"] = entry["error"].rstrip("; ")
        except Exception as e:                      # noqa - one bad layer
            entry["error"] = str(e) or e.__class__.__name__
        if entry["error"]:
            log("Web export of {}: {}".format(base, entry["error"]))
    return result


def find_eap_layers(eap_folder):
    """[(name, path)] of the EAP vector layers of a run: EAP_*.gpkg in the EAP
    folder, EAP_Termination_Point*.gpkg in the QA folder beside it (or the
    same folder - runs without sub-folders), in EAP_LAYER_ORDER."""
    found = {}
    folders = [eap_folder, os.path.join(os.path.dirname(os.path.abspath(eap_folder)), "QA")]
    for folder in folders:
        try:
            names = sorted(os.listdir(folder))
        except OSError:
            continue
        for f in names:
            stem, ext = os.path.splitext(f)
            if ext.lower() != ".gpkg" or not stem.startswith("EAP_") or stem in found:
                continue
            if folder != eap_folder and not stem.startswith("EAP_Termination_Point"):
                continue
            found[stem] = os.path.join(folder, f)

    def order(stem):
        fam = _family(stem)
        return (EAP_LAYER_ORDER.index(fam) if fam else len(EAP_LAYER_ORDER), stem)
    return [(s, found[s]) for s in sorted(found, key=order)]


def _band_rasters(eap_folder):
    try:
        return [os.path.join(eap_folder, f) for f in sorted(os.listdir(eap_folder))
                if f.startswith("EAP_Arrival_Bands") and f.lower().endswith(".tif")]
    except OSError:
        return []


def polygonize_bands(tif, name=None):
    """The arrival-band raster as polygons - the flooded area by arrival time
    (the inundation boundary) - in a QGIS memory layer (fields Class, Arrival;
    the raster's CRS). GDAL Polygonize into an OGR memory layer: no file is
    written. Returns (layer, "") or (None, why)."""
    from osgeo import gdal, ogr
    from qgis.core import QgsRasterLayer, QgsFeature
    from .compat import WKB_POLYGON, FT_INT
    from .helpers import create_memory_layer, gdal_exceptions, geometry_from_wkb
    from .eap import band_labels
    if not hasattr(gdal, "Polygonize"):
        return None, "GDAL Polygonize is not available"
    drv = ogr.GetDriverByName("MEM") or ogr.GetDriverByName("Memory")
    if drv is None or not hasattr(drv, "CreateDataSource"):
        return None, "the OGR memory driver is not available"
    name = name or os.path.splitext(os.path.basename(tif))[0]
    rl = QgsRasterLayer(tif, name)
    if not rl.isValid():
        return None, "the raster cannot be opened"
    labels = dict(band_labels())
    with gdal_exceptions():
        src = gdal.Open(tif, gdal.GA_ReadOnly)
        if src.RasterXSize * float(src.RasterYSize) > MAX_POLYGONIZE_CELLS:
            return None, "{:,} x {:,} cells - too large to convert here (open the .tif in " \
                         "QGIS instead)".format(src.RasterXSize, src.RasterYSize)
        band = src.GetRasterBand(1)
        ds = drv.CreateDataSource("bands")
        olyr = ds.CreateLayer("bands", None, ogr.wkbPolygon)
        olyr.CreateField(ogr.FieldDefn("Class", ogr.OFTInteger))
        gdal.Polygonize(band, band.GetMaskBand(), olyr, 0, [])
        mem = create_memory_layer(name, WKB_POLYGON, rl.crs(),
                                  [QgsField("Class", FT_INT), QgsField("Arrival", FT_STRING)])
        feats = []
        olyr.ResetReading()
        of = olyr.GetNextFeature()
        while of is not None:
            g = geometry_from_wkb(bytes(of.GetGeometryRef().ExportToWkb()))
            cls = of.GetField("Class")
            f = QgsFeature(mem.fields())
            f.setGeometry(g)
            f.setAttributes([cls, labels.get(cls, "")])
            feats.append(f)
            of = olyr.GetNextFeature()
        olyr = ds = band = src = None
    if not feats:
        return None, "no flooded cell with an arrival time"
    mem.dataProvider().addFeatures(feats)
    mem.updateExtents()
    return mem, ""


def export_eap_web(eap_folder, dam_name, log=None, formats=("kmz", "geojson"),
                   bands=True):
    """KMZ / GeoJSON of the run's EAP layers in <eap_folder>/Web (see the
    module text), file names prefixed with the dam name, plus
    <dam>_EAP_All_Layers.kmz with every layer. Returns export_layers()'s dict
    (with an ALL_LAYERS_NAME entry for the combined KMZ). Never raises."""
    log = _log(log)
    result = {}
    try:
        out = os.path.join(eap_folder, WEB_SUBDIR)
        items = find_eap_layers(eap_folder)
        if bands:
            for tif in _band_rasters(eap_folder):
                stem = os.path.splitext(os.path.basename(tif))[0]
                try:
                    lyr, why = polygonize_bands(tif, stem)
                    if lyr is not None:
                        items.append((stem, lyr))
                    else:
                        log("Web export: {} not converted to polygons ({})".format(stem, why))
                except Exception as e:                  # noqa
                    log("Web export: {} not converted to polygons ({})".format(stem, e))
        if not items:
            log("Web export: no EAP layers in {}".format(eap_folder))
            return result
        prefix = safe_name(dam_name) + "_" if dam_name else ""
        result = export_layers(items, out, formats=formats, name_prefix=prefix, log=log)
        kmzs = [v["kmz"] for v in result.values() if v.get("kmz")]
        if len(kmzs) > 1 and "kmz" in formats:
            try:
                p = merge_kmz(kmzs, os.path.join(out, safe_name(prefix + ALL_LAYERS_NAME) +
                                                 ".kmz"),
                              "{} - EAP layers".format(dam_name) if dam_name else "EAP layers")
                if p:
                    result[ALL_LAYERS_NAME] = {"kmz": p, "geojson": None,
                                               "features": sum(v.get("features", 0)
                                                               for v in result.values()),
                                               "error": ""}
            except Exception as e:                      # noqa
                log("Web export: the combined KMZ was not written ({})".format(e))
        ok = [k for k, v in result.items() if v.get("kmz") or v.get("geojson")]
        log("Web export: {} of {} EAP layer(s) written to {}".format(
            len([k for k in ok if k != ALL_LAYERS_NAME]), len(items), out))
    except Exception as e:                              # noqa
        log("Web export failed: {}".format(e))
    return result


def summary_lines(result, folder_label="EAP/Web"):
    """Run-log / summary lines of an export_eap_web() result."""
    lines = []
    for name, v in (result or {}).items():
        files = [os.path.basename(v[k]) for k in ("kmz", "geojson") if v.get(k)]
        if files:
            lines.append("{}: {} ({})".format(folder_label, ", ".join(files),
                                               "{} feature(s)".format(v.get("features", 0))))
        elif v.get("error"):
            lines.append("{}: {} not exported - {}".format(folder_label, name, v["error"]))
    return lines
