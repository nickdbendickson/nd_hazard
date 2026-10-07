"""19 - Propose a dam: a crest line drawn on the pre-dam ground.

The embankment is built into a conditioned copy of the DEM (crest at the design elevation,
faces sloping to natural ground). The source DEM is not modified. Dam height is the design
crest minus the lowest natural ground on the line, and the breach point is there. Storage
is measured by filling that ground stage by stage. The outputs are the same files tools
5-10 and 7b already read for an existing dam, plus dam.json for the Runner and ND Dam Hazard.
"""
import json
import math
import os

import numpy as np
from osgeo import gdal
from qgis.core import (QgsProcessingAlgorithm, QgsProcessingParameterFeatureSource,
                       QgsProcessingParameterField, QgsProcessingParameterFile,
                       QgsProcessingParameterEnum, QgsProcessingParameterNumber,
                       QgsProcessingParameterString, QgsProcessingParameterFolderDestination,
                       QgsProcessingParameterFeatureSink, QgsProcessingParameterFileDestination,
                       QgsProcessingException, QgsFields, QgsField, QgsFeature, QgsGeometry,
                       QgsPointXY, QgsCoordinateTransform, QgsCoordinateReferenceSystem,
                       QgsProject)

from ..core import proposed as pr
from ..core import terrain
from ..core import damrecord_io as dio
from ..core import workflow as wf
from ..compat import (FIELD_ANY, FILE_FOLDER, FT_DOUBLE, FT_STRING, NUM_DOUBLE, SRC_LINE,
                      SRC_POINT, WKB_LINESTRING, WKB_POINT)
from ..damsuite import dam_record as dr
from ..damsuite import gdalsafe as GS
from . import _common as cm
from .breach_direction import PT_FIELDS
from .estimate_storage import NEW_FIELDS as STORAGE_FIELDS
from .prepare_dams import OUT_FIELDS as INVENTORY_FIELDS

MAX_CELLS = 8_000_000
FT = pr.FT


def proposed_id(raw, n):
    """A dam id that will not collide with a DWR structure number. Bare digits become PROP-<n>."""
    s = str(raw or "").strip()
    if not s:
        s = "PROP-{}".format(n)
    elif s.isdigit():
        s = "PROP-{}".format(int(s))
    return wf.safe_name(s)


def _snap(xmin, ymin, xmax, ymax, cell):
    xmin = math.floor(xmin / cell) * cell
    ymin = math.floor(ymin / cell) * cell
    xmax = math.ceil(xmax / cell) * cell
    ymax = math.ceil(ymax / cell) * cell
    if xmax <= xmin:
        xmax = xmin + cell
    if ymax <= ymin:
        ymax = ymin + cell
    return xmin, ymin, xmax, ymax


def _save_tif(path, arr, gt):
    a = np.array(arr, dtype="float64")
    a[~np.isfinite(arr)] = -9999.0
    cm.write_tif(path, a, gt, nodata=-9999.0)


class ProposeDam(QgsProcessingAlgorithm):
    def name(self):
        return "proposedam"

    def displayName(self):
        return "19 - Propose a dam"

    def group(self):
        return "Dam screening"

    def groupId(self):
        return "dams"

    def createInstance(self):
        return ProposeDam()

    def shortHelpString(self):
        return (
            "Builds ONE proposed embankment (or several, one per line) from a crest line you draw, "
            "and writes the inventory, storage curve, breach point, terrain clip and dam.json that "
            "steps 5-10 and 7b, the ND Dam Breach Runner and ND Dam Hazard already use for an "
            "existing dam. Use a work folder of its own when this is the only dam: steps 1-4 then "
            "count as done. An existing step-3 layer in the same folder is not replaced.\n"
            "THE SOURCE DEM IS NOT MODIFIED. The crest is held at your design elevation and the "
            "faces fall at your slopes until they meet the natural ground. Nothing is excavated. "
            "Height is the design crest minus the lowest natural ground on the line (the streambed); "
            "the breach point is that place. Storage is measured on the pre-dam LiDAR, stage by "
            "stage — there is no water surface to see through, so nothing is estimated under a pool. "
            "A rim low point below the crest, where the pool would leave the basin, is reported; the "
            "curve still goes to the design crest and the confidence is low.\n"
            "Draw the crest line from abutment to abutment. The reservoir is the LEFT of the drawn "
            "direction unless you give an upstream point. Starting values (crest width 12 ft, slopes "
            "3H:1V) are engineer choices, not standards. Every report carries the sentence "
            "\"proposed — embankment inserted from the drawn line\". Screening only; the official "
            "classification is the Department's. Not run in a real QGIS.")

    def initAlgorithm(self, config=None):
        P = self.addParameter
        P(QgsProcessingParameterFeatureSource(
            "LINE", "Crest line (draw it from abutment to abutment, on the ground)", [SRC_LINE]))
        P(QgsProcessingParameterFeatureSource(
            "UP", "Upstream point (optional: which side of the line the reservoir is on)",
            [SRC_POINT], optional=True))
        P(QgsProcessingParameterField(
            "ID", "Dam ID field (optional; empty = PROP-1, PROP-2, …; a bare number becomes PROP-<n>)",
            parentLayerParameterName="LINE", type=FIELD_ANY, optional=True))
        P(QgsProcessingParameterField(
            "NAME", "Name field (optional)", parentLayerParameterName="LINE", type=FIELD_ANY,
            optional=True))
        P(QgsProcessingParameterField(
            "CREST_FIELD", "Design crest elevation field (ft, optional; else the number below)",
            parentLayerParameterName="LINE", type=FIELD_ANY, optional=True))
        P(QgsProcessingParameterNumber(
            "CREST_FT", "Design crest elevation (ft, DEM datum) when the field is empty",
            NUM_DOUBLE, 0.0))
        P(QgsProcessingParameterNumber(
            "WIDTH_FT", "Crest width (ft; YOUR choice, not a standard)", NUM_DOUBLE, 12.0,
            minValue=0.0))
        P(QgsProcessingParameterNumber(
            "US_H", "Upstream slope, horizontal per 1 vertical (YOUR choice)", NUM_DOUBLE, 3.0,
            minValue=0.2))
        P(QgsProcessingParameterNumber(
            "DS_H", "Downstream slope, horizontal per 1 vertical (YOUR choice)", NUM_DOUBLE, 3.0,
            minValue=0.2))
        P(QgsProcessingParameterEnum(
            "SRC", "Terrain source",
            ["USGS 3DEP service", "Local DEM folder", "A single raster"], defaultValue=0))
        P(QgsProcessingParameterFile(
            "LOCAL", "Local DEM folder (if that source)", behavior=FILE_FOLDER, optional=True))
        P(QgsProcessingParameterFile(
            "RASTER", "Single DEM raster (if that source)", optional=True))
        cm.distance_params(P, "CELL_FT", "CELL", "Cell size of the clip (ft)", 9.8, min_ft=1.6)
        cm.distance_params(
            P, "RADIUS_FT", "RADIUS",
            "Clip radius beyond the crest line (ft)", 5000.0, min_ft=200.0)
        P(QgsProcessingParameterNumber(
            "STEPS", "Stages in the storage curve", NUM_DOUBLE, 24, minValue=2))
        P(QgsProcessingParameterString(
            "ONLY_IDS", "Only these lines (comma-separated IDs or feature ids; empty = all)",
            defaultValue="", optional=True))
        cm.distance_params(P, "ARROW_FT", "ARROW_M", "Direction arrow length (ft)", 800.0,
                           min_ft=30.0)
        P(QgsProcessingParameterFolderDestination("TERRAIN", "Terrain clips (dam_<ID>.tif)"))
        P(QgsProcessingParameterFolderDestination("CURVES", "Stage-storage curves (CSV)"))
        P(QgsProcessingParameterFolderDestination(
            "DAMS_FOLDER", "Dams folder for dam.json (the Runner and ND Dam Hazard read it)"))
        P(QgsProcessingParameterFeatureSink("OUT", "Proposed dams (inventory with storage)", SRC_POINT))
        P(QgsProcessingParameterFeatureSink("OUTP", "Breach points", SRC_POINT))
        P(QgsProcessingParameterFeatureSink("OUTL", "Flow direction arrows", SRC_LINE))
        P(QgsProcessingParameterFeatureSink("CREST", "Crest lines (the embankment)", SRC_LINE))
        P(QgsProcessingParameterFileDestination("SPEC", "Proposed-dam specification (JSON)"))

    def _source(self, params, context, ext, cell, stem, tmp):
        kind = self.parameterAsEnum(params, "SRC", context)
        if kind == 1:
            folder = self.parameterAsString(params, "LOCAL", context)
            tiles = terrain.list_local_tiles(folder) if folder else []
            if not tiles:
                raise QgsProcessingException("No DEM tiles in the local DEM folder.")
            # 0.17.0: feet tiles converted, mixed CRSs warped to EPSG:5070 (no tile dropped);
            # built once per run and per tile list, not once per dam
            vrt = os.path.join(tmp, "_local_mosaic_{}.vrt".format(cm.mosaic_key(tiles)))
            if vrt not in self._mosaics:
                self._mosaics[vrt] = cm.local_mosaic(tiles, vrt, self._log)
            return vrt
        if kind == 2:
            path = self.parameterAsString(params, "RASTER", context)
            if not path or not os.path.exists(path):
                raise QgsProcessingException("Give the single DEM raster.")
            # 0.17.0: read through a VRT in metres (a raster tagged in feet is converted)
            vrt = os.path.join(tmp, "_single_dem_{}.vrt".format(cm.mosaic_key([path])))
            if vrt not in self._mosaics:
                self._mosaics[vrt] = cm.local_mosaic([path], vrt, self._log)
            return vrt
        return terrain.fetch_3dep(ext, cell, tmp, stem)

    def processAlgorithm(self, params, context, feedback):
        lines = self.parameterAsSource(params, "LINE", context)
        if lines is None:
            raise QgsProcessingException("Draw the crest line first.")
        ups = self.parameterAsSource(params, "UP", context)
        S = lambda k: self.parameterAsString(params, k, context) or ""
        names = lines.fields().names()
        f_id = S("ID") if S("ID") in names else ""
        f_name = S("NAME") if S("NAME") in names else ""
        f_crest = S("CREST_FIELD") if S("CREST_FIELD") in names else ""
        crest_default = self.parameterAsDouble(params, "CREST_FT", context)
        width_m = self.parameterAsDouble(params, "WIDTH_FT", context) * FT
        us_h = self.parameterAsDouble(params, "US_H", context)
        ds_h = self.parameterAsDouble(params, "DS_H", context)
        cell = cm.distance_m(self, params, context, "CELL_FT", "CELL", 9.8)
        steps = max(2, int(round(self.parameterAsDouble(params, "STEPS", context))))
        radius = cm.distance_m(self, params, context, "RADIUS_FT", "RADIUS", 5000.0)
        arrow = cm.distance_m(self, params, context, "ARROW_FT", "ARROW_M", 800.0)
        only = {x.strip().upper() for x in S("ONLY_IDS").split(",") if x.strip()}
        terrain_dir = self.parameterAsString(params, "TERRAIN", context)
        curves = self.parameterAsString(params, "CURVES", context)
        dams_folder = self.parameterAsString(params, "DAMS_FOLDER", context)
        spec_path = self.parameterAsFileOutput(params, "SPEC", context)
        os.makedirs(terrain_dir, exist_ok=True)
        os.makedirs(curves, exist_ok=True)
        tmp = os.path.join(terrain_dir, "_tmp")
        os.makedirs(tmp, exist_ok=True)
        self._mosaics, self._log = {}, feedback.pushInfo         # 0.17.0: _source's local mosaics

        alb = cm.albers()
        wgs = QgsCoordinateReferenceSystem("EPSG:4326")
        prj = QgsProject.instance()
        to_a = QgsCoordinateTransform(lines.sourceCrs(), alb, prj)
        up_to_a = QgsCoordinateTransform(ups.sourceCrs(), alb, prj) if ups is not None else None

        fields = QgsFields()
        for n, t in INVENTORY_FIELDS + STORAGE_FIELDS:
            fields.append(QgsField(n, t))
        pf = QgsFields()
        for n, t in PT_FIELDS:
            pf.append(QgsField(n, t))
        lf = QgsFields()
        for n, t in (("dam_id", FT_STRING), ("flow_az", FT_DOUBLE), ("dir_conf", FT_STRING)):
            lf.append(QgsField(n, t))
        cf = QgsFields()
        for n, t in (("dam_id", FT_STRING), ("name", FT_STRING), ("crest_m", FT_DOUBLE)):
            cf.append(QgsField(n, t))
        sink, dest = cm.us_sink(self, params, "OUT", context, fields, WKB_POINT, wgs)
        sp, dp = cm.us_sink(self, params, "OUTP", context, pf, WKB_POINT, alb)
        sl, dl = self.parameterAsSink(params, "OUTL", context, lf, WKB_LINESTRING, alb)
        sc, dc = self.parameterAsSink(params, "CREST", context, cf, WKB_LINESTRING, alb)

        up_xy = []
        if ups is not None:
            for f in ups.getFeatures():
                g = f.geometry()
                if g is None or g.isEmpty():
                    continue
                g.transform(up_to_a)
                p = g.asPoint()
                up_xy.append((p.x(), p.y()))

        used = set()
        specs = []
        n_ok = n_skip = n_fail = 0
        feats = [f for f in lines.getFeatures()]
        total = len(feats) or 1
        for i, f in enumerate(feats):
            if feedback.isCanceled():
                break
            feedback.setProgress(100.0 * i / total)
            raw_id = str(f[f_id]) if f_id and f[f_id] not in (None, "") else ""
            dam_id = proposed_id(raw_id, i + 1)
            if only and not any(v in only for v in (raw_id.upper(), dam_id.upper(), str(f.id()))):
                n_skip += 1
                continue
            base, k = dam_id, 2
            while dam_id in used:
                dam_id = "{}-{}".format(base, k)
                k += 1
            g = f.geometry()
            if g is None or g.isEmpty():
                n_fail += 1
                feedback.reportError("{}: the feature has no line.".format(dam_id))
                continue
            g.transform(to_a)
            parts = g.asMultiPolyline() if g.isMultipart() else [g.asPolyline()]
            parts = [pl for pl in parts if len(pl) >= 2]
            if not parts:
                n_fail += 1
                feedback.reportError("{}: the crest line needs at least two points.".format(dam_id))
                continue
            pl = max(parts, key=lambda q: sum(
                math.hypot(q[j + 1].x() - q[j].x(), q[j + 1].y() - q[j].y()) for j in range(len(q) - 1)))
            line = np.array([(p.x(), p.y()) for p in pl], dtype=float)
            crest_ft = crest_default
            if f_crest and f[f_crest] not in (None, ""):
                try:
                    crest_ft = float(f[f_crest])
                except (TypeError, ValueError):
                    pass
            if not crest_ft:
                n_fail += 1
                feedback.reportError("{}: give the design crest elevation (ft).".format(dam_id))
                continue
            name = str(f[f_name]) if f_name and f[f_name] not in (None, "") else "Proposed {}".format(dam_id)
            side, given = "left", False
            if up_xy:
                dists = []
                for p in up_xy:
                    d, _s, _a = pr.polyline_geom(np.array([p[0]]), np.array([p[1]]), line)
                    dists.append((float(d[0]), p))
                dists.sort()
                if dists[0][0] <= max(radius, 1000.0):
                    side = pr.side_of_point(line, dists[0][1])
                    given = True
            xmin, ymin, xmax, ymax = _snap(
                float(line[:, 0].min()) - radius, float(line[:, 1].min()) - radius,
                float(line[:, 0].max()) + radius, float(line[:, 1].max()) + radius, cell)
            ncell = int(round((xmax - xmin) / cell)) * int(round((ymax - ymin) / cell))
            if ncell > MAX_CELLS:
                n_fail += 1
                feedback.reportError(
                    "{}: the clip is {:,} cells. Raise the cell size or shorten the clip radius "
                    "(limit {:,}).".format(dam_id, ncell, MAX_CELLS))
                continue
            ext = (xmin, ymin, xmax, ymax)
            try:
                source = self._source(params, context, ext, cell, wf.safe_name(dam_id), tmp)
                ds = GS.checked(gdal.Warp(
                    "", source, format="MEM", dstSRS=cm.ALBERS, outputBounds=ext,
                    width=int(round((xmax - xmin) / cell)), height=int(round((ymax - ymin) / cell)),
                    resampleAlg="bilinear", srcNodata=-9999, dstNodata=-9999,
                    outputType=gdal.GDT_Float32), "warping the terrain for " + dam_id)
                dem = ds.GetRasterBand(1).ReadAsArray().astype("float64")
                dem[(dem <= -9998.0) | ~np.isfinite(dem)] = np.nan
                gt = ds.GetGeoTransform()
                if np.isnan(dem).mean() > 0.5:
                    raise pr.ProposedError(
                        "no terrain over most of this clip (check the DEM covers the line)")
                emb, stor, text, conf = pr.build(
                    dem, gt, line, crest_ft * FT, width_m, us_h, ds_h, side, given, steps)
            except pr.ProposedError as e:
                n_fail += 1
                feedback.reportError("{}: {}".format(dam_id, e))
                continue
            except Exception as e:
                n_fail += 1
                feedback.reportError("{}: {}".format(dam_id, e))
                continue
            used.add(dam_id)
            stem = wf.safe_name(dam_id)
            _save_tif(os.path.join(terrain_dir, "dam_{}.tif".format(stem)), emb["dem"], gt)
            _save_tif(os.path.join(terrain_dir, "dam_{}_natural.tif".format(stem)), emb["natural"], gt)
            with open(os.path.join(curves, "curve_{}.csv".format(stem)), "w", encoding="utf-8") as fh:
                fh.write("elev_m,storage_m3,elev_ft,storage_acft\n")
                for elev, vol, _area, _hold in stor["curve"]:
                    fh.write("{:.4f},{:.2f},{:.2f},{:.3f}\n".format(elev, vol, elev / 0.3048, vol / 1233.48183754752))
            with open(os.path.join(curves, "curve_{}_detail.csv".format(stem)), "w",
                      encoding="utf-8") as fh:
                fh.write("elev_m,storage_m3,area_m2,holdable,elev_ft,storage_acft,area_ac\n")
                for elev, vol, area, hold in stor["curve"]:
                    fh.write("{:.4f},{:.2f},{:.2f},{},{:.2f},{:.3f},{:.4f}\n".format(
                        elev, vol, area, int(bool(hold)), elev / 0.3048, vol / 1233.48183754752,
                        area / 4046.8564224))
            bx, by = emb["breach_xy"]
            lon, lat = dio.lonlat_from_5070(bx, by)
            spec = pr.json_safe_spec(emb, stor, line, _line_lonlat(line), dam_id)
            specs.append(spec)
            self._write_record(dams_folder, dam_id, name, emb, stor, spec, lon, lat, feedback)
            row = [dam_id, name, "proposed", "", emb["height_m"], stor["holdable_m3"],
                   stor["storage_crest_m3"], stor["area_crest_m2"], "", pr.PHRASE,
                   emb["crest_len_m"], "",
                   # 0.19.0 inventory fields: kind, h_src, stor_src, inv_total, inv_above, dugout x2, notes
                   "proposed dam", "measured (drawn crest line)", "measured (DEM)", None, None, None, None, "",
                   stor["pool_m"], emb["crest_m"], stor["area_crest_m2"], 0.0,
                   stor["storage_crest_m3"], stor["storage_crest_m3"],
                   stor["pool_m"] - emb["bed_m"], None, stor["storage_crest_m3"], "measured",
                   conf, None, text, 1.0, stor["storage_crest_m3"], None, None]
            feat = QgsFeature(fields)
            feat.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(lon, lat)))
            feat.setAttributes(row)
            sink.addFeature(feat)
            dir_conf = "high" if given else "medium"
            bp = QgsFeature(pf)
            bp.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(bx, by)))
            bp.setAttributes([
                dam_id, name, f.id(), 1, emb["crest_len_m"], emb["dam_az"], emb["flow_az"],
                "upstream point" if given else "left of the drawn line", dir_conf,
                "lowest ground on the crest line", emb["height_m"], text])
            sp.addFeature(bp)
            az = math.radians(emb["flow_az"])
            ln = QgsFeature(lf)
            ln.setGeometry(QgsGeometry.fromPolylineXY([
                QgsPointXY(bx, by),
                QgsPointXY(bx + arrow * math.sin(az), by + arrow * math.cos(az))]))
            ln.setAttributes([dam_id, emb["flow_az"], dir_conf])
            sl.addFeature(ln)
            cl = QgsFeature(cf)
            cl.setGeometry(QgsGeometry.fromPolylineXY([QgsPointXY(x, y) for x, y in line]))
            cl.setAttributes([dam_id, name, emb["crest_m"]])
            sc.addFeature(cl)
            n_ok += 1
            feedback.pushInfo("{}: height {:.1f} ft, storage at the crest {:.1f} ac-ft, {}".format(
                dam_id, emb["height_m"] / FT, stor["storage_crest_m3"] / pr.AF_M3, stor["rim_kind"]))

        if spec_path:
            os.makedirs(os.path.dirname(spec_path) or ".", exist_ok=True)
            with open(spec_path, "w", encoding="utf-8") as fh:
                json.dump({"phrase": pr.PHRASE, "dams": specs}, fh, indent=1)
                fh.write("\n")
        if n_ok == 0:
            raise QgsProcessingException(
                "No proposed dam was written ({} skipped, {} failed). {}".format(
                    n_skip, n_fail, pr.PHRASE))
        feedback.pushInfo("{} proposed dam(s) written, {} skipped, {} failed. {}".format(
            n_ok, n_skip, n_fail, pr.PHRASE))
        return {"OUT": dest, "OUTP": dp, "OUTL": dl, "CREST": dc,
                "TERRAIN": terrain_dir, "CURVES": curves, "DAMS_FOLDER": dams_folder,
                "SPEC": spec_path}

    def _write_record(self, folder, dam_id, name, emb, stor, spec, lon, lat, feedback):
        if not folder:
            return
        sub = os.path.join(folder, wf.safe_name(dam_id))
        try:
            rec = dr.load(sub) or dr.DamRecord(sub)
            vals = {
                "dam_id": dam_id, "name": name,
                "height_ft": dio.round3(emb["height_m"] / FT),
                "storage_acft": dio.round3(stor["storage_crest_m3"] / pr.AF_M3),
                "surface_area_ac": dio.round3(stor["area_crest_m2"] / pr.AC_M2),
                "top_of_dam_ft": dio.round3(emb["crest_m"] / FT),
                "top_of_dam_datum": "DEM",
                "dam_length_ft": dio.round3(emb["crest_len_m"] / FT),
                "x": round(emb["breach_xy"][0], 2), "y": round(emb["breach_xy"][1], 2),
                "crs": "EPSG:5070", "lon": round(lon, 7), "lat": round(lat, 7),
            }
            refused = rec.set_many(vals, dio.TOOL, dio.plugin_version(), note=pr.PHRASE)
            rec.extra["proposed_embankment"] = spec
            rec.save()
            if refused:
                feedback.pushInfo("{}: dam.json kept another tool's value for {}.".format(
                    dam_id, ", ".join(refused)))
        except Exception as e:
            feedback.reportError("{}: dam.json not written ({})".format(dam_id, e))


def _line_lonlat(line):
    return [list(dio.lonlat_from_5070(float(x), float(y))) for x, y in np.asarray(line, float)]
