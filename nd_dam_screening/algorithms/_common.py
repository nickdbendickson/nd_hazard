"""Shared QGIS/GDAL helpers for the processing tools."""
import os
import threading

import numpy as np
from osgeo import gdal, osr
from qgis.core import (QgsFeature, QgsFields, QgsField, QgsCoordinateReferenceSystem, QgsCoordinateTransform,
                       QgsProject, QgsFeatureRequest, QgsRectangle, QgsWkbTypes)

from ..core import terrain, hydrocond, gridutil as gu, us_units as uu
from ..compat import (GEOM_LINE, GEOM_POINT, GEOM_POLYGON, FT_DOUBLE)
from ..damsuite import gdalsafe as GS

ALBERS = "EPSG:5070"
FT = 0.3048                      # metres per foot (user-facing depths are in feet since 0.11)
# 0.11.1: no gdal.UseExceptions() - it is process-wide (QGIS and the other plugins); GDAL
# calls go through damsuite.gdalsafe instead, which raises the same errors in both modes.
_lock = threading.Lock()


def hide(param):
    """Mark a Processing parameter hidden (kept for old models / saved histories that still pass
    the metre value; the dialog shows the feet parameter). Works on QGIS 3.x (legacy
    FlagHidden), QGIS 3.36+ / 4 (Qgis.ProcessingParameterFlag.Hidden) and the test mock (no
    flags: ignored)."""
    try:
        from qgis.core import Qgis
        flag = Qgis.ProcessingParameterFlag.Hidden
    except Exception:
        try:
            from qgis.core import QgsProcessingParameterDefinition as D
            flag = D.FlagHidden
        except Exception:
            flag = None
    if flag is not None:
        try:
            param.setFlags(param.flags() | flag)
        except Exception:
            pass
    return param


def feet_or_legacy(alg, params, context, ft_key, m_key, default_ft):
    """The value of a depth threshold in METRES for the model: from the feet parameter
    `ft_key`, unless the caller passed only the old metre parameter `m_key` (a saved model /
    history from before 0.11), which then wins. Returns (metres, feet, source)."""
    legacy = m_key in params and params.get(m_key) not in (None, "") and ft_key not in params
    if legacy:
        m = float(alg.parameterAsDouble(params, m_key, context))
        return m, m / FT, m_key
    ft = alg.parameterAsDouble(params, ft_key, context)
    if ft is None or ft == 0 and ft_key not in params:
        ft = default_ft
    return float(ft) * FT, float(ft), ft_key


def distance_params(add, ft_key, m_key, label_ft, default_ft, min_ft=None, max_ft=None, legacy_label=None,
                    optional=False):
    """0.13: a user-facing DISTANCE in feet plus its hidden metre twin for saved models / histories
    from before 0.13 (`feet_or_legacy` reads them). `add` is the tool's addParameter; `label_ft`
    should name the unit, e.g. 'Toe search distance beyond the water edge (ft; YOUR value)'."""
    from qgis.core import QgsProcessingParameterNumber
    from ..compat import NUM_DOUBLE
    kw = {}
    if min_ft is not None:
        kw["minValue"] = min_ft
    if max_ft is not None:
        kw["maxValue"] = max_ft
    if optional:
        kw["optional"] = True
    add(QgsProcessingParameterNumber(ft_key, label_ft, NUM_DOUBLE, default_ft, **kw))
    add(hide(QgsProcessingParameterNumber(m_key, legacy_label or "(before 0.13) {} (m)".format(
        label_ft.split("(")[0].strip()), NUM_DOUBLE, None, optional=True)))


def distance_m(alg, params, context, ft_key, m_key, default_ft):
    """The metres of a distance parameter made by distance_params (feet wins; a saved metre value
    alone is honoured)."""
    m, _ft, _src = feet_or_legacy(alg, params, context, ft_key, m_key, default_ft)
    return m


def distance_m_opt(alg, params, context, ft_key, m_key):
    """Like distance_m for a distance parameter made with optional=True: the metres, or None
    when neither the feet nor the old metre parameter was given."""
    if params.get(ft_key) not in (None, ""):
        return float(alg.parameterAsDouble(params, ft_key, context)) * FT
    if params.get(m_key) not in (None, ""):
        return float(alg.parameterAsDouble(params, m_key, context))
    return None


def no_flood(f, names):
    """(True, reason) when tool 16 marked this dam 'no breach flood' (no_flood = YES: a dugout or
    pond with no water above the natural ground); (False, "") otherwise."""
    if fval(f, names, "no_flood") is None or str(fval(f, names, "no_flood")).strip().upper() != "YES":
        return False, ""
    return True, str(fval(f, names, "no_flood_why") or "no water above the natural ground")


def excluded(f, names):
    """(True, reason) when the Review tab excluded this dam (rev_decision = exclude); (False, "")
    otherwise. Tools 5, 6, 7, 8 and 12 skip such a dam ("excluded by review")."""
    v = fval(f, names, "rev_decision")
    if v is None or str(v).strip().lower() != "exclude":
        return False, ""
    who, when = fval(f, names, "rev_by"), fval(f, names, "rev_date")
    return True, "excluded by review ({}{})".format(who or "reviewer", ", " + str(when)[:10] if when else "")


def albers():
    return QgsCoordinateReferenceSystem(ALBERS)


def to_albers(src):
    return QgsCoordinateTransform(src.sourceCrs(), albers(), QgsProject.instance())


def from_albers(src):
    return QgsCoordinateTransform(albers(), src.sourceCrs(), QgsProject.instance())


def extend_fields(src_fields, new):
    """Copy src fields and add `new` [(name, QVariant type)], reusing names that already exist.

    Returns (fields, setter) where setter(attrs_list, name, value) writes by name.
    """
    fields = QgsFields(src_fields)
    for n, t in new:
        if fields.indexOf(n) < 0:
            fields.append(QgsField(n, t))

    def setter(attrs, name, value):
        attrs[fields.indexOf(name)] = value
    return fields, setter


def with_us(fields):
    """0.18.0: the fields plus a US-units twin (feet, cfs, acres, acre-feet, ft/s) of every
    metric field (core.us_units.twin_pairs). Names that exist are reused."""
    out = QgsFields(fields)
    for _m, us, _k in uu.twin_pairs(fields.names()):
        if out.indexOf(us) < 0:
            out.append(QgsField(us, FT_DOUBLE))
    return out


class UsSink(object):
    """A feature sink that fills the US-units twin of each metric attribute before it writes
    (0.18.0). Everything else is passed on to the real sink."""

    def __init__(self, sink, fields):
        self._sink = sink
        self._fields = fields
        self._n = fields.count()
        names = fields.names()
        self._pairs = [(names.index(m), names.index(us), k) for m, us, k in uu.twin_pairs(names)
                       if us in names]

    def addFeature(self, feature, *args):
        attrs = list(feature.attributes())
        if len(attrs) < self._n:
            attrs += [None] * (self._n - len(attrs))
        for mi, ui, k in self._pairs:
            v = uu.convert(attrs[mi], k)
            if v is not None or attrs[ui] is None:
                attrs[ui] = v
        nf = QgsFeature(self._fields)                            # carries the twin fields too
        if feature.hasGeometry():
            nf.setGeometry(feature.geometry())
        nf.setAttributes(attrs)
        return self._sink.addFeature(nf, *args)

    def addFeatures(self, features, *args):
        ok = True
        for f in features:
            ok = self.addFeature(f, *args) and ok
        return ok

    def __getattr__(self, name):
        return getattr(self._sink, name)


def us_sink(alg, params, key, context, fields, wkb, crs, *rest):
    """alg.parameterAsSink with US twins: returns (sink wrapper, destination id)."""
    f2 = with_us(fields)
    sink, dest = alg.parameterAsSink(params, key, context, f2, wkb, crs, *rest)
    if sink is None:
        return sink, dest
    return UsSink(sink, f2), dest


def blank_attrs(fields, feature):
    a = list(feature.attributes()) + [None] * (fields.count() - len(feature.attributes()))
    return a[:fields.count()]


def fval(f, names, k, cast=None):
    """Feature value by name, None if missing/NULL/blank; optional cast."""
    if k is None or k not in names:
        return None
    v = f[k]
    if v is None or v == "" or (hasattr(v, "isNull") and v.isNull()):
        return None
    if cast is not None:
        try:
            return cast(v)
        except (TypeError, ValueError):
            return None
    return v


def _gdal_msg():
    try:
        return gdal.GetLastErrorMsg()
    except Exception:                                            # noqa
        return ""


def gdal_open(path):
    """gdal.Open, or an error that names the file (GDAL returns None for an unreadable or
    damaged file; v0.11.1 - it used to surface as "'NoneType' object has no attribute ...")."""
    ds = gdal.Open(path)
    if ds is None:
        raise RuntimeError("GDAL cannot open {} ({}) - delete it to have it made again".format(
            path, _gdal_msg() or "missing or damaged file"))
    return ds


def write_tif(path, arr, gt, nodata=None):
    ds = GS.checked(gdal.GetDriverByName("GTiff").Create(path, arr.shape[1], arr.shape[0], 1,
                                               gdal.GDT_Float32, ["COMPRESS=DEFLATE"]), "creating " + path)
    ds.SetGeoTransform(gt)
    srs = osr.SpatialReference(); srs.ImportFromEPSG(5070)
    ds.SetProjection(srs.ExportToWkt())
    b = ds.GetRasterBand(1)
    if nodata is not None:
        b.SetNoDataValue(nodata)
    b.WriteArray(np.asarray(arr, "float32")); ds.FlushCache(); ds = None


def read_tif(path):
    ds = gdal_open(path)
    b = ds.GetRasterBand(1)
    a = b.ReadAsArray().astype("float64")
    nd = b.GetNoDataValue()
    if nd is not None:
        a[a == nd] = gu.NODATA
    return a, ds.GetGeoTransform()


def lines_near(source, xy, radius, key_field=None):
    """Line geometries (EPSG:5070 numpy arrays) of `source` within radius of xy.

    Returns list of (key, [array, ...]) - one entry per feature, parts separately.
    """
    if source is None:
        return []
    ta, fa = to_albers(source), from_albers(source)
    box = fa.transformBoundingBox(QgsRectangle(xy[0] - radius, xy[1] - radius,
                                               xy[0] + radius, xy[1] + radius))
    out = []
    names = source.fields().names()
    for f in source.getFeatures(QgsFeatureRequest().setFilterRect(box)):
        g = f.geometry()
        if g.isEmpty():
            continue
        g.transform(ta)
        if g.type() != GEOM_LINE:          # compat spelling (QGIS 3 and 4), as geom_parts
            continue
        parts = g.asMultiPolyline() if g.isMultipart() else [g.asPolyline()]
        arrs = [np.array([(p.x(), p.y()) for p in pl]) for pl in parts if len(pl) >= 2]
        key = f[key_field] if key_field and key_field in names else f.id()
        if arrs:
            out.append((key, arrs))
    return out


# ------------------------------------------------- 0.17.0: elevation units and local mosaics --
US_FT = 1200.0 / 3937.0          # metres per US survey foot
_FT_WORDS = ("ft", "foot", "feet", "intl foot", "international foot", "ft_intl", "foot_intl")
_M_WORDS = ("m", "metre", "meter", "metres", "meters", "metre (m)")


def z_unit_scale(unit):
    """(metres per elevation unit, kind) for a band's GetUnitType(): kind is "m", "ft" (0.3048),
    "us-ft" (1200/3937), "" (no tag: metres assumed) or "other" (a tag we do not know: metres
    assumed). Read the tag, never the CRS: a State Plane feet CRS often holds metre elevations."""
    u = " ".join(str(unit or "").strip().lower().replace("-", " ").replace(".", " ").split())
    if not u:
        return 1.0, ""
    if u in _M_WORDS:
        return 1.0, "m"
    squashed = u.replace(" ", "").replace("_", "")
    if ("us" in squashed or "survey" in squashed) and any(w in squashed for w in ("ft", "foot", "feet")):
        return US_FT, "us-ft"                                    # us-ft, ftUS, US survey foot, foot_us
    if u in _FT_WORDS or squashed in ("ft", "foot", "feet", "intlfoot", "ftintl", "footintl"):
        return FT, "ft"
    return 1.0, "other"


def dem_z_scale(path):
    """(metres per elevation unit, kind, unit tag) of band 1 of a DEM file (z_unit_scale)."""
    ds = gdal_open(path)
    try:
        unit = ds.GetRasterBand(1).GetUnitType()
    except AttributeError:                                       # a stand-in without unit tags
        unit = ""
    scale, kind = z_unit_scale(unit)
    return scale, kind, unit or ""


def _crs_name(wkt):
    try:
        srs = osr.SpatialReference()
        srs.ImportFromWkt(wkt)
        code = srs.GetAuthorityCode(None)
        name = srs.GetName() or "unnamed CRS"
        return "EPSG:{} {}".format(code, name) if code else name
    except Exception:                                            # noqa
        return str(wkt)[:60] or "no CRS"


def _same_crs(a, b):
    if a == b:
        return True
    if not a or not b:
        return False
    try:
        sa, sb = osr.SpatialReference(), osr.SpatialReference()
        sa.ImportFromWkt(a)
        sb.ImportFromWkt(b)
        return bool(sa.IsSame(sb))
    except Exception:                                            # noqa
        return False


def _cell_m(wkt, gt):
    """A tile's cell size in metres (feet CRSs scaled; degrees taken as 111 km), or None."""
    try:
        srs = osr.SpatialReference()
        srs.ImportFromWkt(wkt)
        f = srs.GetLinearUnits() if srs.IsProjected() else 111320.0
        return abs(gt[1]) * f or None
    except Exception:                                            # noqa
        return None


def mosaic_key(tiles):
    """A short hash of the sorted tile list (path, size, modified time): the name of a mosaic
    that is reused only while the local DEM folder holds exactly these files."""
    import hashlib
    h = hashlib.sha1()
    for t in sorted(tiles):
        try:
            st = os.stat(t)
            h.update("{}|{}|{}\n".format(os.path.abspath(t), st.st_size, int(st.st_mtime)).encode("utf-8"))
        except OSError:
            h.update("{}|-\n".format(os.path.abspath(t)).encode("utf-8"))
    return h.hexdigest()[:12]


def local_mosaic(tiles, vrt, log=None):
    """0.17.0: one VRT in metres (EPSG:5070 when the tiles' CRSs differ) over a local DEM folder.

    gdal.BuildVRT alone drops every tile whose CRS differs from the first one's and mosaics feet
    with metres. Here a tile tagged in feet / US survey feet is scaled to metres (a VRT beside
    the mosaic, tag "m"; the ND LiDAR nd_lidar_ascii_m.vrt is already "m" and left alone); an
    untagged tile is taken as metres, with a warning; when the tiles are in more than one CRS
    each is warped to EPSG:5070 (a warped VRT, no pixels written) before the mosaic. The
    per-tile VRTs go in <mosaic>_tiles/. Returns vrt."""
    say = log or (lambda _m: None)
    groups, feet, untagged, other = [], [], [], []
    scales, cells = [], []
    for t in tiles:
        ds = gdal_open(t)
        wkt = ds.GetProjection() or ""
        cells.append(_cell_m(wkt, ds.GetGeoTransform()))
        ds = None
        scale, kind, unit = dem_z_scale(t)
        scales.append(scale)
        if kind in ("ft", "us-ft"):
            feet.append("{} ({})".format(os.path.basename(t), unit))
        elif kind == "":
            untagged.append(os.path.basename(t))
        elif kind == "other":
            other.append("{} ({!r})".format(os.path.basename(t), unit))
        for g in groups:
            if _same_crs(g[0], wkt):
                g[1].append(t)
                break
        else:
            groups.append((wkt, [t]))
    if len(groups) > 1 and any(not g[0] for g in groups):
        raise RuntimeError("The local DEM folder mixes tiles with and without a CRS; give every tile "
                           "a CRS (a .prj / .aux.xml) or remove these: {}".format(
                               ", ".join(os.path.basename(t) for g in groups if not g[0] for t in g[1][:5])))
    if feet:
        say("Local DEM: {} tile(s) tagged in feet, converted to metres: {}{}".format(
            len(feet), ", ".join(feet[:5]), " ..." if len(feet) > 5 else ""))
    if untagged:
        say("WARNING: local DEM: {} tile(s) have no elevation unit tag; their elevations are assumed to be "
            "METRES (a feet DEM would be read 3.28x too high): {}{}".format(
                len(untagged), ", ".join(untagged[:5]), " ..." if len(untagged) > 5 else ""))
    if other:
        say("WARNING: local DEM: unknown elevation unit, assumed metres: {}".format(", ".join(other[:5])))
    mixed = len(groups) > 1
    if mixed:
        say("Local DEM: tiles in {} CRSs ({}), each warped to EPSG:5070 for the mosaic".format(
            len(groups), "; ".join("{} - {} tile(s)".format(_crs_name(g[0]), len(g[1])) for g in groups)))
    if not mixed and all(sc == 1.0 for sc in scales):
        GS.checked(gdal.BuildVRT(vrt, tiles), "building " + vrt)
        return vrt
    work = os.path.splitext(vrt)[0] + "_tiles"
    os.makedirs(work, exist_ok=True)
    parts = []
    res = min([c for c in cells if c] or [1.0])     # the finest tile's cell, on one aligned grid
    for i, (t, sc) in enumerate(zip(tiles, scales)):
        p = t
        if sc != 1.0:
            p = os.path.join(work, "{:05d}_m.vrt".format(i))
            ds = GS.checked(gdal.Translate(p, t, format="VRT", outputType=gdal.GDT_Float32,
                                           scaleParams=[[0.0, 1.0, 0.0, sc]]), "converting {} to metres".format(t))
            ds = None
            ds = gdal.Open(p, gdal.GA_Update)
            ds.GetRasterBand(1).SetUnitType("m")
            ds = None
        if mixed:
            q = os.path.join(work, "{:05d}_5070.vrt".format(i))
            GS.checked(gdal.Warp(q, p, format="VRT", dstSRS=ALBERS, resampleAlg="bilinear",
                                 xRes=res, yRes=res, targetAlignedPixels=True,
                                 dstNodata=-9999, outputType=gdal.GDT_Float32),
                       "warping {} to EPSG:5070".format(t))
            p = q
        parts.append(p)
    GS.checked(gdal.BuildVRT(vrt, parts), "building " + vrt)
    return vrt


def _clip_meta_path(dst):
    return dst + ".json"


CELL_TOL = 0.01          # 0.18.0: 3.3 ft is 1.006 m; a 1 m cache is still the 3.3 ft one


def clip_reusable(dst, ext, cell, source):
    """0.17.0: (True, "") when an existing dam_<ID>.tif was cut for this extent, cell and source,
    else (False, why). The parameters are kept in <file>.json (write_clip_meta); a file from
    before 0.17 (no .json) is reused when its cell matches and it covers the extent."""
    import json
    if not os.path.exists(dst):
        return False, "none yet"
    meta = _clip_meta_path(dst)
    if os.path.exists(meta):
        try:
            with open(meta, encoding="utf-8") as fh:
                m = json.load(fh)
            if abs(float(m["cell"]) - float(cell)) > CELL_TOL * max(1.0, float(cell)):   # 0.18.0: cells are asked in ft
                return False, "cell {:.1f} ft, not {:.1f} ft".format(float(m["cell"]) / FT, float(cell) / FT)
            if any(abs(float(a) - float(b)) > 0.01 for a, b in zip(m["extent"], ext)):
                return False, "another extent (radius or dam point changed)"
            if str(m.get("source", "")) != str(source):
                return False, "another terrain source ({})".format(m.get("source") or "?")
            return True, ""
        except (OSError, ValueError, KeyError, TypeError) as e:
            return False, "unreadable {} ({})".format(os.path.basename(meta), e)
    try:
        ds = gdal_open(dst)
        gt, nx, ny = ds.GetGeoTransform(), ds.RasterXSize, ds.RasterYSize
        ds = None
    except RuntimeError as e:
        return False, str(e)
    if abs(abs(gt[1]) - float(cell)) > CELL_TOL * max(1.0, float(cell)):
        return False, "cell {:.1f} ft, not {:.1f} ft".format(abs(gt[1]) / FT, float(cell) / FT)
    x0, y1 = gt[0], gt[3]
    x1, y0 = x0 + nx * gt[1], y1 + ny * gt[5]
    if not (x0 <= ext[0] + 1 and y0 <= ext[1] + 1 and x1 >= ext[2] - 1 and y1 >= ext[3] - 1):
        return False, "does not cover the clip extent"
    return True, ""


def write_clip_meta(dst, ext, cell, source):
    """0.17.0: the parameters a dam_<ID>.tif was cut with, in <file>.json (clip_reusable)."""
    import json
    try:
        with open(_clip_meta_path(dst), "w", encoding="utf-8") as fh:
            json.dump({"cell": float(cell), "extent": [float(v) for v in ext], "source": str(source)}, fh)
    except OSError:
        pass


def drop_clip(dst):
    """Remove a dam_<ID>.tif that is to be cut again, and its .json."""
    for p in (dst, _clip_meta_path(dst)):
        try:
            os.remove(p)
        except OSError:
            pass


def _from_5070(xy, wkt):
    """0.21.0: an EPSG:5070 point in the CRS `wkt` (the point as it is when the raster has no CRS,
    is EPSG:5070 itself, or the transform is not available)."""
    x, y = float(xy[0]), float(xy[1])
    if not wkt:
        return x, y
    try:
        from osgeo import osr
        srs = osr.SpatialReference()
        srs.ImportFromWkt(wkt)
        a = osr.SpatialReference()
        a.ImportFromEPSG(5070)
        if srs.IsSame(a):
            return x, y
        for r in (srs, a):
            try:
                r.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
            except AttributeError:
                pass
        return tuple(osr.CoordinateTransformation(a, srs).TransformPoint(x, y)[:2])
    except Exception:                                            # noqa - the loader warps to 5070
        return x, y


class TerrainLoader:
    """Loads terrain for any extent at any cell size and conditions it.

    Source order: the cached per-dam clip from tool 2 when it covers the extent,
    otherwise the configured source (USGS 3DEP or a local DEM folder); with
    neither, the cached clip is used as far as it reaches (outside = wall).
    Road-fill barriers on stream lines are cut at the model resolution.
    """

    def __init__(self, demdir, source="cache", local_folder=None, tmpdir=None,
                 max_rise_m=8.0, max_len_m=150.0, log=None):
        self.demdir, self.source, self.tmpdir = demdir, source, tmpdir
        self.max_rise, self.max_len = max_rise_m, max_len_m
        self.vrt = None
        if source == "local" and local_folder:
            tiles = terrain.list_local_tiles(local_folder)
            if tiles:
                # 0.17.0: named by the tile list, so a changed local DEM folder gets a new mosaic
                # (the old fixed name was reused whatever the folder then held); local_mosaic
                # converts feet tiles and warps mixed CRSs
                self.vrt = os.path.join(tmpdir or demdir, "_terrain_mosaic_{}.vrt".format(mosaic_key(tiles)))
                with _lock:
                    if not os.path.exists(self.vrt):
                        local_mosaic(tiles, self.vrt, log)

    def _cached(self, dam_id):
        p = os.path.join(self.demdir, f"dam_{dam_id}.tif") if self.demdir else None
        return p if p and os.path.exists(p) else None

    def covers_point(self, dam_id, xy):
        """0.21.0 (pre-run check): (True / False / None, detail) - is the EPSG:5070 point `xy` on
        the terrain this loader would read for the dam? None when it can't be known before the
        run (USGS 3DEP is fetched for the extent then)."""
        srcs = [p for p in (self._cached(dam_id), self.vrt) if p]
        if not srcs:
            if self.source == "3dep":
                return None, "USGS 3DEP is fetched for the run"
            return False, "no cached clip (tool 2) and no terrain source"
        for path in srcs:
            try:
                ds = gdal_open(path)
                gt = ds.GetGeoTransform()
                x, y = _from_5070(xy, ds.GetProjection())
                x0, y1 = gt[0], gt[3]
                x1, y0 = x0 + ds.RasterXSize * gt[1], y1 + ds.RasterYSize * gt[5]
                if min(x0, x1) <= x <= max(x0, x1) and min(y0, y1) <= y <= max(y0, y1):
                    return True, "the breach point is on {}".format(os.path.basename(path))
            except Exception:                                    # noqa - the next source
                continue
        if self.source == "3dep":
            return None, "USGS 3DEP is fetched for the run"
        return False, "the breach point is outside the terrain ({})".format(
            ", ".join(os.path.basename(p) for p in srcs))

    def _covers(self, path, ext):
        ds = gdal_open(path); gt = ds.GetGeoTransform()
        x0, y1 = gt[0], gt[3]
        x1, y0 = x0 + ds.RasterXSize * gt[1], y1 + ds.RasterYSize * gt[5]
        return x0 <= ext[0] + 1 and y0 <= ext[1] + 1 and x1 >= ext[2] - 1 and y1 >= ext[3] - 1

    def load(self, dam_id, center, half, cell, streams=(), protect_lines=(), roads=None,
             condition=True):
        x0 = np.floor((center[0] - half) / cell) * cell
        y0 = np.floor((center[1] - half) / cell) * cell
        n = int(np.ceil(2 * half / cell))
        ext = (x0, y0, x0 + n * cell, y0 + n * cell)
        src = self._cached(dam_id)
        if src is None or not self._covers(src, ext):
            if self.source == "3dep":
                tmp = os.path.join(self.tmpdir or self.demdir, "_3dep")
                src_list = terrain.fetch_3dep(ext, max(cell, 1.0), tmp, f"x{dam_id}_{int(half)}")
                src = GS.checked(gdal.BuildVRT("", src_list), "mosaicking the 3DEP tiles")
            elif self.vrt:
                src = self.vrt
            elif src is None:
                raise FileNotFoundError("no cached DEM (tool 2) and no terrain source for a larger clip")
        ds = gdal.Warp("", src, format="MEM", dstSRS=ALBERS, outputBounds=ext, width=n, height=n,
                       resampleAlg="average", srcNodata=-9999, dstNodata=-9999,
                       outputType=gdal.GDT_Float32)
        if ds is None:
            raise RuntimeError("GDAL could not warp the terrain for dam {} ({})".format(
                dam_id, _gdal_msg() or "no message"))
        dem = ds.GetRasterBand(1).ReadAsArray().astype("float64")
        dem[~np.isfinite(dem)] = gu.NODATA
        dem[dem < -1000] = gu.NODATA
        gt = ds.GetGeoTransform()
        if condition and streams:
            prot = gu.dilate(gu.rasterize_lines(protect_lines, gt, dem.shape), 2) if protect_lines else None
            rmask = gu.dilate(gu.rasterize_lines(roads, gt, dem.shape), 2) if roads else None
            dem, _info = hydrocond.breach_barriers(dem, streams, gt, protect=prot, roads=rmask,
                                                   max_rise_m=self.max_rise, max_len_m=self.max_len)
        return dem, gt


def load_cached_clip(loader, dam_id, streams=(), protect_lines=()):
    """The tool-2 clip on its own grid (no re-centring), conditioned."""
    path = loader._cached(dam_id)
    if not path:
        raise FileNotFoundError("no cached DEM (tool 2)")
    dem, gt = read_tif(path)
    dem[~np.isfinite(dem)] = gu.NODATA
    if streams:
        prot = gu.dilate(gu.rasterize_lines(protect_lines, gt, dem.shape), 2) if protect_lines else None
        dem, _ = hydrocond.breach_barriers(dem, streams, gt, protect=prot,
                                           max_rise_m=loader.max_rise, max_len_m=loader.max_len)
    return dem, gt


def warp_to_grid(path_or_layer_source, gt, shape, resample="near"):
    ds = gdal.Warp("", path_or_layer_source, format="MEM", dstSRS=ALBERS,
                   outputBounds=(gt[0], gt[3] + shape[0] * gt[5], gt[0] + shape[1] * gt[1], gt[3]),
                   width=shape[1], height=shape[0], resampleAlg=resample,
                   outputType=gdal.GDT_Float32)
    if ds is None:
        raise RuntimeError("GDAL could not warp {} to the model grid ({})".format(
            path_or_layer_source, _gdal_msg() or "no message"))
    return ds.GetRasterBand(1).ReadAsArray().astype("float64")


def is_line_source(src):
    return src is not None and QgsWkbTypes.geometryType(src.wkbType()) == GEOM_LINE


def geom_parts(g):
    """(kind, parts) of a geometry in its current CRS, multi-part aware:
    'point' -> [(x, y), ...]; 'line' -> [[(x, y), ...], ...] (parts with >= 2 vertices);
    'polygon' -> [exterior ring [(x, y), ...], ...] (interior rings ignored). (None, []) when
    empty or of another type."""
    if g is None or g.isEmpty():
        return None, []
    t = g.type()
    multi = g.isMultipart()
    if t == GEOM_POINT:
        pts = g.asMultiPoint() if multi else [g.asPoint()]
        return "point", [(p.x(), p.y()) for p in pts]
    if t == GEOM_LINE:
        lines = g.asMultiPolyline() if multi else [g.asPolyline()]
        return "line", [[(p.x(), p.y()) for p in ln] for ln in lines if len(ln) >= 2]
    if t == GEOM_POLYGON:
        polys = g.asMultiPolygon() if multi else [g.asPolygon()]
        return "polygon", [[(p.x(), p.y()) for p in rings[0]] for rings in polys
                           if rings and len(rings[0]) >= 3]
    return None, []


def rep_point(g):
    """(x, y) representing a feature: the point itself, the mean of a MultiPoint's points,
    else the centroid. None when empty."""
    kind, parts = geom_parts(g)
    if kind == "point" and parts:
        return (sum(p[0] for p in parts) / len(parts), sum(p[1] for p in parts) / len(parts))
    if g is None or g.isEmpty():
        return None
    c = g.centroid().asPoint()
    return (c.x(), c.y())


def albers_parts(feature, transform):
    """geom_parts of a feature's geometry transformed to EPSG:5070."""
    g = feature.geometry()
    if g is None or g.isEmpty():
        return None, []
    g.transform(transform)
    return geom_parts(g)


# ------------------------------------------------- last outputs (0.21.0) --
# The last layer a tool wrote, so that the next tools opened from the Processing Toolbox (which
# knows no work folder) start with it: "dams" = tool 1's Standardized dams, "dams3" = tool 3's.
# A file path, or the id of a layer of the project (a temporary layer); forgotten when gone.
_LAST_KEY = "nd_dam_screening/last_output/{}"


def remember_output(kind, dest):
    try:
        from qgis.core import QgsSettings
        QgsSettings().setValue(_LAST_KEY.format(kind), str(dest or ""))
    except Exception:                                            # noqa - never fail a tool here
        pass


def last_output(kind):
    """The remembered output of `kind` when it still exists (a file, or a layer of the
    project), else None."""
    try:
        from qgis.core import QgsSettings
        v = str(QgsSettings().value(_LAST_KEY.format(kind), "") or "").strip()
    except Exception:                                            # noqa
        return None
    if not v:
        return None
    if os.path.isfile(v.split("|")[0]):
        return v
    try:
        if QgsProject.instance().mapLayer(v) is not None:
            return v
    except Exception:                                            # noqa
        pass
    return None


def dialog_with_last(alg, parent, name, *kinds):
    """The standard dialog of `alg` whose layer box `name` starts with the first remembered
    output of `kinds` that still exists. A value set afterwards (the Screening window's) wins.
    None (QGIS then opens its own dialog) without the Processing GUI or nothing remembered."""
    v = next((x for x in (last_output(k) for k in kinds) if x), None)
    if not v:
        return None
    try:
        from processing.gui.AlgorithmDialog import AlgorithmDialog
        from qgis.core import QgsProcessingContext
        dlg = AlgorithmDialog(alg.create(), parent=parent)
        w = (getattr(dlg.mainWidget(), "wrappers", {}) or {}).get(name)
        if w is not None:
            ctx = QgsProcessingContext()
            ctx.setProject(QgsProject.instance())            # a temporary layer, by its id
            w.setParameterValue(v, ctx)
        return dlg
    except Exception:                                            # noqa
        return None


def is_temporary(dest):
    """True when a tool's destination is not a file (a temporary / memory layer)."""
    p = str(dest or "").split("|")[0]
    return not p or not os.path.splitext(p)[1] or p.startswith("memory:")
