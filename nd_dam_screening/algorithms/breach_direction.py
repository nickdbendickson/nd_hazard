"""Match each dam to its dam line; derive breach point and flow direction."""
import os

import numpy as np
from qgis.core import (QgsProcessingAlgorithm, QgsProcessingParameterFeatureSource,
                       QgsProcessingParameterFile, QgsProcessingParameterNumber,
                       QgsProcessingParameterField,
                       QgsProcessingParameterFeatureSink, QgsFields,
                       QgsField, QgsFeature, QgsGeometry, QgsPointXY,
                       QgsCoordinateTransform, QgsCoordinateReferenceSystem, QgsProject,
                       QgsFeatureRequest, QgsRectangle)
import math

from ..core import breach_dir as bd
from ..compat import (FILE_FOLDER, FT_DOUBLE, FT_INT, FT_LONGLONG, FT_STRING, NUM_DOUBLE, SRC_LINE, SRC_POINT, SRC_POLYGON, WKB_LINESTRING, WKB_POINT)
from . import _common as cm

RESERVOIR_TOUCH_M = 150.0      # a water body this close to the dam line counts as the reservoir

PT_FIELDS = [("dam_id", FT_STRING), ("name", FT_STRING),
             ("line_fid", FT_LONGLONG), ("n_lines", FT_INT),
             ("crest_len_m", FT_DOUBLE), ("dam_az", FT_DOUBLE),
             ("flow_az", FT_DOUBLE), ("dir_method", FT_STRING),
             ("dir_conf", FT_STRING), ("brch_method", FT_STRING),
             ("height_m", FT_DOUBLE), ("notes", FT_STRING)]


class BreachDirection(QgsProcessingAlgorithm):
    DAMS, LINES, WB, DEMDIR, MATCH, OUTP, OUTL, ARROW = (
        "DAMS", "LINES", "WB", "DEMDIR", "MATCH", "OUTP", "OUTL", "ARROW")

    def name(self): return "breachdirection"
    def displayName(self): return "4 - Breach point and flow direction"
    def group(self): return "Dam screening"
    def groupId(self): return "dams"
    def createInstance(self): return BreachDirection()

    # 0.21.0: opened from the Processing Toolbox (no work folder), the dialog starts with the
    # last Standardized dams written (only the dialog: a run given no layer still stops)
    def createCustomParametersWidget(self, parent=None):
        return cm.dialog_with_last(self, parent, self.DAMS, "dams3", "dams")

    def shortHelpString(self):
        return ("Matches each dam to its DWR dam line (by DWR structure ID first, then by "
                "distance and name), works out which side is downstream "
                "(away from the reservoir; terrain is the cross-check), and places the breach "
                "where the ground at the downstream toe is lowest, i.e. where the embankment "
                "is tallest. Outputs a breach point layer and a direction-arrow line layer so "
                "you can check every dam visually. dir_conf = low means the reservoir and the "
                "terrain disagreed; those dams need a manual look. Dams with no dam line fall "
                "back to 'flow away from the reservoir centroid' (dir_method = centroid, "
                "conf = low). Without cached terrain the breach goes to the line point nearest "
                "the inventory location.")

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.DAMS, "Standardized dams", [SRC_POINT]))
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.LINES, "DWR dam lines", [SRC_LINE],
            defaultValue="dams_linear_features"))
        self.addParameter(QgsProcessingParameterField(
            "LID", "Dam lines: structure ID field (joins to the DWR structure ID; primary match)",
            defaultValue="structure_id", parentLayerParameterName=self.LINES, optional=True))
        self.addParameter(QgsProcessingParameterField(
            "LNAME", "Dam lines: dam name field (fallback only; the DWR export has none)",
            parentLayerParameterName=self.LINES, optional=True))
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.WB, "NHD HR waterbodies (polygons)", [SRC_POLYGON],
            optional=True))
        self.addParameter(QgsProcessingParameterFile(
            self.DEMDIR, "Folder of cached dam DEMs (optional)",
            behavior=FILE_FOLDER, optional=True))
        cm.distance_params(self.addParameter, "MATCH_FT", self.MATCH,
                           "Max distance from dam point to its dam line (ft)", 1000.0, min_ft=3.0)
        cm.distance_params(self.addParameter, "ARROW_FT", self.ARROW,
                           "Direction arrow length (ft)", 800.0, min_ft=30.0)
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUTP, "Breach points", SRC_POINT))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUTL, "Flow direction arrows", SRC_LINE))

    @staticmethod
    def _dem_fn(path):
        if not path or not os.path.exists(path):
            return None
        ds = cm.gdal_open(path)
        arr = ds.GetRasterBand(1).ReadAsArray().astype("float64")
        nd = ds.GetRasterBand(1).GetNoDataValue()
        if nd is not None:
            arr[arr == nd] = np.nan
        gt = ds.GetGeoTransform()

        def fn(x, y):
            c = int((x - gt[0]) / gt[1]); r = int((y - gt[3]) / gt[5])
            if 0 <= r < arr.shape[0] and 0 <= c < arr.shape[1]:
                return float(arr[r, c])
            return None
        return fn

    def processAlgorithm(self, params, context, feedback):
        dams = self.parameterAsSource(params, self.DAMS, context)
        lines = self.parameterAsSource(params, self.LINES, context)
        wbs = self.parameterAsSource(params, self.WB, context)
        demdir = self.parameterAsString(params, self.DEMDIR, context)
        lname = self.parameterAsString(params, "LNAME", context) or None
        lid = self.parameterAsString(params, "LID", context) or None
        match = cm.distance_m(self, params, context, "MATCH_FT", self.MATCH, 1000.0)
        arrow = cm.distance_m(self, params, context, "ARROW_FT", self.ARROW, 800.0)

        alb = QgsCoordinateReferenceSystem("EPSG:5070")
        pf = QgsFields()
        for n, t in PT_FIELDS: pf.append(QgsField(n, t))
        lf = QgsFields()
        for n, t in (("dam_id", FT_STRING), ("flow_az", FT_DOUBLE),
                     ("dir_conf", FT_STRING)): lf.append(QgsField(n, t))
        sp, dp = cm.us_sink(self, params, self.OUTP, context, pf, WKB_POINT, alb)
        sl, dl = self.parameterAsSink(params, self.OUTL, context, lf, WKB_LINESTRING, alb)

        prj = QgsProject.instance()
        d2a = QgsCoordinateTransform(dams.sourceCrs(), alb, prj)
        l2a = QgsCoordinateTransform(lines.sourceCrs(), alb, prj)
        a2l = QgsCoordinateTransform(alb, lines.sourceCrs(), prj)
        if wbs is not None:
            w2a = QgsCoordinateTransform(wbs.sourceCrs(), alb, prj)
            a2w = QgsCoordinateTransform(alb, wbs.sourceCrs(), prj)
        names = dams.fields().names()
        total = dams.featureCount() or 1
        stats = {"line": 0, "fallback": 0, "none": 0, "low": 0, "by_id": 0}

        # index dam lines by structure ID once (about 10k lines; cheap)
        by_id = {}
        if lid and lid in lines.fields().names():
            for lf_ in lines.getFeatures():
                try:
                    k = int(lf_[lid])
                except (TypeError, ValueError):
                    continue
                by_id.setdefault(k, []).append(lf_)
            feedback.pushInfo(f"indexed {sum(len(v) for v in by_id.values())} dam lines "
                              f"by {lid} ({len(by_id)} structures)")

        for i, f in enumerate(dams.getFeatures()):
            if feedback.isCanceled(): break
            feedback.setProgress(100 * i / total)
            dam_id = str(f["dam_id"]) if "dam_id" in names else str(f.id())
            name = str(f["name"]) if "name" in names and f["name"] else ""
            h = f["height_m"] if "height_m" in names else None
            h = float(h) if h not in (None, "") else None
            g = f.geometry(); g.transform(d2a); pt = g.asPoint(); ref = (pt.x(), pt.y())

            # --- matching dam line(s) ------------------------------------
            cands, named, by_id_used = [], [], False
            sid = None
            if dam_id.startswith("DWR-"):
                try:
                    sid = int(dam_id[4:])
                except ValueError:
                    sid = None
            if sid is not None and sid in by_id:
                for lf_ in by_id[sid]:
                    lg = lf_.geometry(); lg.transform(l2a)
                    cands.append((lg.distance(g), lf_.id(), lg, None))
                cands.sort(key=lambda c: c[0])
                by_id_used = True
            else:
                box = a2l.transformBoundingBox(
                    QgsRectangle(pt.x() - match, pt.y() - match, pt.x() + match, pt.y() + match))
                for lf_ in lines.getFeatures(QgsFeatureRequest().setFilterRect(box)):
                    lg = lf_.geometry(); lg.transform(l2a)
                    d = lg.distance(g)
                    if d <= match:
                        cands.append((d, lf_.id(), lg, lf_[lname] if lname else None))
                cands.sort(key=lambda c: c[0])
                named = [c for c in cands if bd.names_match(c[3], name)] if (name and lname) else []
                if named:
                    cands = named        # same-name lines first: dike/spillway lines drop out

            # --- reservoir + terrain helpers -----------------------------
            wgeoms = []
            if wbs is not None:
                rb = a2w.transformBoundingBox(QgsRectangle(
                    pt.x() - 1000, pt.y() - 1000, pt.x() + 1000, pt.y() + 1000))
                for w in wbs.getFeatures(QgsFeatureRequest().setFilterRect(rb)):
                    wg = w.geometry(); wg.transform(w2a); wgeoms.append(wg)
            # 0.18.0: the reservoir is ONE water body, not "any water within 1 km": the
            # largest one that touches the dam line (a pond below a long dam is closer to the
            # dam's midpoint than the reservoir is)
            reservoir = None
            if wgeoms:
                ref_g = cands[0][2] if cands else g
                near_w = [w for w in wgeoms if w.distance(ref_g) <= RESERVOIR_TOUCH_M]
                if near_w:
                    reservoir = max(near_w, key=lambda w: w.area())
                else:
                    reservoir = min(wgeoms, key=lambda w: w.distance(g))
                if len(wgeoms) > 1:
                    feedback.pushInfo("{}: {} water bodies within 3,300 ft; the reservoir is "
                                      "the {} one at the dam line ({:,.1f} acres)".format(
                                          dam_id, len(wgeoms), "largest" if near_w else
                                          "nearest", reservoir.area() / 4046.8564224))
            def _water(x, y, _r=reservoir):
                return _r.distance(QgsGeometry.fromPointXY(QgsPointXY(x, y)))
            water_fn = _water if reservoir is not None else None
            dem_fn = self._dem_fn(os.path.join(demdir, f"dam_{dam_id}.tif")) if demdir else None

            notes, res = [], None
            if cands:
                lg = cands[0][2]
                parts = lg.asMultiPolyline() if lg.isMultipart() else [lg.asPolyline()]
                part = max(parts, key=lambda pl: len(pl))
                verts = [(p.x(), p.y()) for p in part]
                if by_id_used:
                    stats["by_id"] += 1
                    if cands[0][0] > 300:
                        notes.append(f"ID-matched dam line is {cands[0][0] / cm.FT:,.0f} ft from the "
                                     "inventory point; check the location")
                    if len(cands) > 1:
                        notes.append(f"{len(cands)} lines share this structure ID; nearest used")
                elif named:
                    notes.append("dam line chosen by name match")
                elif len(cands) > 1:
                    notes.append(f"{len(cands)} dam lines within {match / cm.FT:,.0f} ft; nearest used")
                res = bd.resolve_breach(verts, ref, water_fn, dem_fn, h)
                if res["ok"]:
                    stats["line"] += 1
                else:
                    notes.append(res["reason"])
                    res = None

            if res is None:                      # no usable dam line
                cen = None
                if wgeoms:
                    near = min(wgeoms, key=lambda w: w.distance(g))
                    c = near.centroid().asPoint(); cen = (c.x(), c.y())
                az = bd.fallback_direction(ref, cen) if cen else None
                if az is None:
                    stats["none"] += 1
                    o = QgsFeature(pf); o.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(*ref)))
                    o.setAttributes([dam_id, name, None, len(cands), None, None, None, "none",
                                     "none", "none", h, "; ".join(notes + ["no direction"])])
                    sp.addFeature(o); continue
                stats["fallback"] += 1
                b = math.radians(az)
                res = {"ok": True, "breach": ref, "flow_az": az, "axis_az": None,
                       "length_m": None, "dir_method": "centroid", "dir_conf": "low",
                       "breach_method": "dam point", "notes": ["no dam line; centroid method"],
                       "down": (math.sin(b), math.cos(b))}
            if res["dir_conf"] == "low": stats["low"] += 1

            bx, by = res["breach"]; dx, dy = res["down"]
            o = QgsFeature(pf)
            o.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(bx, by)))
            o.setAttributes([dam_id, name, cands[0][1] if cands else None, len(cands),
                             res["length_m"], res["axis_az"], res["flow_az"], res["dir_method"],
                             res["dir_conf"], res["breach_method"], h,
                             "; ".join(notes + res["notes"])])
            sp.addFeature(o)
            a = QgsFeature(lf)
            a.setGeometry(QgsGeometry.fromPolylineXY(
                [QgsPointXY(bx, by), QgsPointXY(bx + dx * arrow, by + dy * arrow)]))
            a.setAttributes([dam_id, res["flow_az"], res["dir_conf"]])
            sl.addFeature(a)

        feedback.pushInfo(f"dam line used: {stats['line']} ({stats['by_id']} by structure ID), centroid fallback: "
                          f"{stats['fallback']}, no direction: {stats['none']}, "
                          f"low confidence (review): {stats['low']}")
        return {self.OUTP: dp, self.OUTL: dl}
