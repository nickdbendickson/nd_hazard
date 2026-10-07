"""Quick screen (triage): clear dams whose generous flood envelope reaches nothing."""
import os
import random

import numpy as np
from qgis.core import (QgsProcessingAlgorithm, QgsProcessingParameterFeatureSource,
                       QgsProcessingParameterFile, QgsProcessingParameterNumber,
                       QgsProcessingParameterField, QgsProcessingParameterString,
                       QgsProcessingParameterFeatureSink, QgsProcessingParameterFolderDestination,
                       QgsFeature, QgsFeatureRequest, QgsRectangle)

from ..core import triage as tg, hazard as hz, gridutil as gu, runner as rn
from . import _common as cm
from ..core import errors as er
from ..compat import (FILE_FOLDER, FT_DOUBLE, FT_INT, FT_STRING, NUM_DOUBLE, NUM_INT, SRC_LINE, SRC_POINT, SRC_POLYGON)

AF_M3 = 1233.4818375
NEW = [("tri_result", FT_STRING), ("tri_reason", FT_STRING),
       ("tri_reach_m", FT_DOUBLE), ("tri_hits", FT_INT),
       ("tri_audit", FT_STRING), ("tri_tif", FT_STRING)]


class _NoFlood(Exception):
    """A pond marked no_flood = YES by tool 16."""


class Triage(QgsProcessingAlgorithm):
    def name(self): return "triage"
    def displayName(self): return "6 - Quick screen (triage)"
    def group(self): return "Dam screening"
    def groupId(self): return "dams"
    def createInstance(self): return Triage()

    def shortHelpString(self):
        return ("Takes about a second per dam. Builds a deliberately generous flood envelope from "
                "the breach peak: unattenuated peak flow, Manning normal depth in valley cross-"
                "sections along the steepest-descent path (through potholes and, with stream lines, "
                "through road fills), a safety margin, and a reach that stops only when the valley "
                "would hold 'volume factor' x the released volume.\n"
                "tri_result = clear: no habitable structure (tool 8's rule: habitable unless the "
                "occupancy names an uninhabited building), no road that could rate Medium or High "
                "(only a known AADT < 500, not interstate, not sole access is left out; an unknown "
                "AADT counts), no railroad, no lifeline facility and no other dam of the layer lies "
                "inside the envelope, so the dam screens LOW without the 2D model. needs 2D: something is inside, the envelope reached the terrain edge, or the "
                "dam is above the size limit. Tool 7 skips cleared dams.\n"
                "AUDIT: a random share of cleared dams is still sent to tool 7 (tri_audit = yes); "
                "tool 8 reports whether the 2D model agreed. Screening shortcut only.")

    def initAlgorithm(self, config=None):
        P = self.addParameter
        P(QgsProcessingParameterFeatureSource("DAMS", "Dams with breach hydrographs (tool 5)",
                                              [SRC_POINT]))
        P(QgsProcessingParameterFeatureSource("BPTS", "Breach points (tool 4)", [SRC_POINT]))
        P(QgsProcessingParameterFile("DEMDIR", "Folder of cached dam DEMs (tool 2)",
                                     behavior=FILE_FOLDER))
        P(QgsProcessingParameterFeatureSource("BLD", "Structures", [SRC_POINT,
                                              SRC_POLYGON], optional=True))
        P(QgsProcessingParameterField("OCC", "Structures: occupancy class field (as tool 8; a tool-0 "
                                      "'habitable' field wins)", defaultValue="OCC_CLS",
                                      parentLayerParameterName="BLD", optional=True))
        P(QgsProcessingParameterField("PRIM", "Structures: primary occupancy field (as tool 8)",
                                      defaultValue="PRIM_OCC", parentLayerParameterName="BLD", optional=True))
        P(QgsProcessingParameterString("UNINHABITED", "Structures: occupancy words that mark an UNINHABITED "
                                       "building (as tool 8)", defaultValue=", ".join(hz.UNINHABITED)))
        P(QgsProcessingParameterFeatureSource("ROADS", "Roads", [SRC_LINE], optional=True))
        P(QgsProcessingParameterField("ROAD_AADT", "Roads: AADT field", defaultValue="aadt",
                                      parentLayerParameterName="ROADS", optional=True))
        P(QgsProcessingParameterField("ROAD_CLASS", "Roads: class field", defaultValue="road_class",
                                      parentLayerParameterName="ROADS", optional=True))
        P(QgsProcessingParameterField("R_SOLE", "Roads: sole access to an occupied location field (yes/no)",
                                      defaultValue="sole_access", parentLayerParameterName="ROADS", optional=True))
        P(QgsProcessingParameterFeatureSource("RAIL", "Railroads", [SRC_LINE], optional=True))
        P(QgsProcessingParameterFeatureSource("LIFELINES", "Lifeline facilities (points or polygons)",
                                              [SRC_POINT, SRC_POLYGON],
                                              optional=True))
        P(QgsProcessingParameterFeatureSource("STREAMS", "Stream lines for road-crossing cuts (optional)",
                                              [SRC_LINE], optional=True))
        P(QgsProcessingParameterFeatureSource("DAMLINES", "DWR dam lines (protected from cuts, optional)",
                                              [SRC_LINE], optional=True))
        P(QgsProcessingParameterNumber("N", "Manning n for the envelope", NUM_DOUBLE,
                                       0.04, minValue=0.01, maxValue=0.3))
        P(QgsProcessingParameterNumber("MARGIN_FT", "Stage safety margin: at least (ft; the starting value 0.5 "
                                       "ft is a screening value YOU choose)",
                                       NUM_DOUBLE, 0.5, minValue=0.0))
        P(cm.hide(QgsProcessingParameterNumber("MARGIN", "(before 0.11) Stage safety margin: at least (m)",
                                               NUM_DOUBLE, 0.15, minValue=0.0,
                                               optional=True)))
        P(QgsProcessingParameterNumber("MFRAC", "Stage safety margin: share of flow depth",
                                       NUM_DOUBLE, 0.3, minValue=0.0, maxValue=2.0))
        P(QgsProcessingParameterNumber("VF", "Volume factor", NUM_DOUBLE, 2.0,
                                       minValue=1.0, maxValue=10.0))
        P(QgsProcessingParameterNumber("BIG", "Always send to 2D above this storage (ac-ft), 0 = never",
                                       NUM_DOUBLE, 1000.0, minValue=0.0))
        P(QgsProcessingParameterNumber("AUDIT", "Audit share of cleared dams sent to 2D anyway (%)",
                                       NUM_DOUBLE, 5.0, minValue=0.0, maxValue=100.0))
        P(QgsProcessingParameterNumber("SEED", "Random seed for the audit sample",
                                       NUM_INT, 1))
        P(QgsProcessingParameterFolderDestination("OUTDIR", "Folder for envelope rasters"))
        P(QgsProcessingParameterFeatureSink("OUT", "Dams with triage result", SRC_POINT))

    def processAlgorithm(self, params, context, feedback):
        dams = self.parameterAsSource(params, "DAMS", context)
        bpts = self.parameterAsSource(params, "BPTS", context)
        demdir = self.parameterAsString(params, "DEMDIR", context)
        bld = self.parameterAsSource(params, "BLD", context)
        occ = self.parameterAsString(params, "OCC", context) or None
        prim = self.parameterAsString(params, "PRIM", context) or None
        uninhabited = hz.words(self.parameterAsString(params, "UNINHABITED", context)) or hz.UNINHABITED
        f_sole = self.parameterAsString(params, "R_SOLE", context) or None
        life = self.parameterAsSource(params, "LIFELINES", context)
        roads = self.parameterAsSource(params, "ROADS", context)
        f_aadt = self.parameterAsString(params, "ROAD_AADT", context) or None
        f_rcls = self.parameterAsString(params, "ROAD_CLASS", context) or None
        rail = self.parameterAsSource(params, "RAIL", context)
        streams = self.parameterAsSource(params, "STREAMS", context)
        damlines = self.parameterAsSource(params, "DAMLINES", context)
        n = self.parameterAsDouble(params, "N", context)
        margin, margin_ft, _src = cm.feet_or_legacy(self, params, context, "MARGIN_FT", "MARGIN", 0.5)
        feedback.pushInfo("Stage safety margin at least {:.2f} ft ({:.3f} m)".format(margin_ft, margin))
        mfrac = self.parameterAsDouble(params, "MFRAC", context)
        vf = self.parameterAsDouble(params, "VF", context)
        big = self.parameterAsDouble(params, "BIG", context)
        audit = self.parameterAsDouble(params, "AUDIT", context) / 100.0
        rnd = random.Random(self.parameterAsInt(params, "SEED", context))
        outdir = self.parameterAsString(params, "OUTDIR", context)
        os.makedirs(outdir, exist_ok=True)
        loader = cm.TerrainLoader(demdir, "cache")

        bnames = bld.fields().names() if bld else []
        occ = occ if occ in bnames else None
        prim = prim if prim in bnames else None
        f_hab = "habitable" if "habitable" in bnames else None
        rnames = roads.fields().names() if roads else []
        f_aadt = f_aadt if f_aadt in rnames else None
        f_rcls = f_rcls if f_rcls in rnames else None
        f_sole = f_sole if f_sole in rnames else None

        b2a = cm.to_albers(bpts)
        bmap = {}
        for b in bpts.getFeatures():
            g = b.geometry(); g.transform(b2a); p = cm.rep_point(g)
            if p is not None and b["flow_az"] not in (None, ""):
                bmap[str(b["dam_id"])] = (p, float(b["flow_az"]))
        names = dams.fields().names()
        # every dam of the layer: another dam inside the envelope keeps the dam from 'clear' (5.3)
        d2a = cm.to_albers(dams)
        dam_pts = []
        for f in dams.getFeatures():
            g = f.geometry()
            if g is not None and not g.isEmpty():
                g.transform(d2a); p = cm.rep_point(g)
                if p is not None:
                    dam_pts.append((str(cm.fval(f, names, "dam_id") or f.id()), p))
        get = lambda f, k, c=None: cm.fval(f, names, k, c)
        fields, put = cm.extend_fields(dams.fields(), NEW)
        sink, dest = cm.us_sink(self, params, "OUT", context, fields, dams.wkbType(), dams.sourceCrs())
        total = dams.featureCount() or 1
        stats = {"clear": 0, "needs 2D": 0, "audit": 0}

        def habitable(feat):          # as tool 8: a tool-0 yes/no field wins, else the words rule
            return hz.is_habitable(feat[occ] if occ else None, feat[prim] if prim else None,
                                   flag=feat[f_hab] if f_hab else None, uninhabited=uninhabited)

        def points_of(kind, parts):
            """Points tested against the envelope: the points; a polygon's centroid and vertices;
            a line's vertices (lines are densified before)."""
            if kind == "point":
                return parts
            if kind == "polygon":
                return [(sum(x for x, _ in r) / len(r), sum(y for _, y in r) / len(r)) for r in parts] + \
                    [p for r in parts for p in r]
            return [p for ln in parts for p in ln]

        for i, f in enumerate(dams.getFeatures()):
            if feedback.isCanceled(): break
            feedback.setProgress(100.0 * i / total)
            dam_id = str(get(f, "dam_id") or f.id())
            a = cm.blank_attrs(fields, f)
            result, reason, reach, hits, tif = "needs 2D", "", None, 0, None
            nf, nf_why = cm.no_flood(f, dams.fields().names())
            ex, ex_why = cm.excluded(f, dams.fields().names())
            try:
                if ex:                      # 0.13: excluded by the reviewer -> cleared, with the reason
                    raise _NoFlood(ex_why)
                if nf:                      # tool 16: nothing above the natural ground -> nothing floods
                    raise _NoFlood(nf_why)
                vw = get(f, "td_vw_m3", float); qp = get(f, "td_qp_m3s", float)
                rel = get(f, "td_rel_m3", float) or vw
                if dam_id not in bmap or vw is None or qp is None:
                    raise LookupError("no breach point or hydrograph")
                bxy, az = bmap[dam_id]
                if big > 0 and vw / AF_M3 > big:
                    reason = f"storage {vw / AF_M3:,.0f} ac-ft above the triage limit"
                else:
                    st = [x for _, arrs in cm.lines_near(streams, bxy, 3000) for x in arrs] if streams else []
                    pr = [x for _, arrs in cm.lines_near(damlines, bxy, 3000) for x in arrs] if damlines else []
                    dem, gt = cm.load_cached_clip(loader, dam_id, st, pr)
                    half = 0.5 * min(dem.shape) * abs(gt[1])
                    inj = rn.injection_cells(gt, dem.shape, bxy, az, get(f, "td_bb_m", float) or 10.0,
                                             get(f, "td_hb_m", float) or 5.0, abs(gt[1]))
                    if not inj:
                        raise ValueError("breach toe outside the terrain clip")
                    r0 = int(np.mean([c[0] for c in inj])); c0 = int(np.mean([c[1] for c in inj]))
                    env = tg.screen(dem, gt, (r0, c0), qp, rel, n=n, margin_m=margin, margin_frac=mfrac, volume_factor=vf,
                                    half_width_m=min(1500.0, half))
                    reach = env["reach_m"]
                    tif = os.path.join(outdir, f"{dam_id}_triage_depth.tif")
                    cm.write_tif(tif, env["depth"], gt)
                    mask = env["mask"]
                    ext = QgsRectangle(gt[0], gt[3] + dem.shape[0] * gt[5], gt[0] + dem.shape[1] * gt[1], gt[3])

                    def inside(x, y):
                        r, c = gu.xy_to_rc(gt, x, y)
                        return gu.inside(mask.shape, r, c) and mask[r, c]
                    found = []
                    for src, kind in ((bld, "structure"), (life, "lifeline")):
                        if src is None:
                            continue
                        ta, fa = cm.to_albers(src), cm.from_albers(src)
                        for b in src.getFeatures(QgsFeatureRequest().setFilterRect(fa.transformBoundingBox(ext))):
                            if kind == "structure" and not habitable(b):
                                continue
                            if any(inside(x, y) for x, y in points_of(*cm.albers_parts(b, ta))):
                                found.append(kind)
                    for oid, (x, y) in dam_pts:
                        if oid != dam_id and inside(x, y):
                            found.append("dam")
                    for src, kind in ((roads, "road"), (rail, "railroad")):
                        if src is None:
                            continue
                        ta, fa = cm.to_albers(src), cm.from_albers(src)
                        for r in src.getFeatures(QgsFeatureRequest().setFilterRect(fa.transformBoundingBox(ext))):
                            if kind == "road":
                                aadt = cm.fval(r, rnames, f_aadt, float)
                                # same rule as tool 8 (nd_hazard): only a road KNOWN to be
                                # low (AADT < 500, not interstate) cannot raise the class; an
                                # unknown AADT is assumed Medium there, so it counts here too
                                rd = hz.nd_road("r", 1.0, aadt, str(r[f_rcls]) if f_rcls and r[f_rcls] else "",
                                                sole_access=hz.nh.yes_no(r[f_sole]) is True if f_sole else False)
                                lvl = hz.nh.road_class(rd, hz.nh.Settings())[0]
                                if lvl == "Low":
                                    continue            # cannot raise the class above Low
                            gl = r.geometry(); gl.transform(ta); gl = gl.densifyByDistance(abs(gt[1]))
                            if any(inside(v.x(), v.y()) for v in gl.vertices()):
                                found.append(kind)
                    hits = len(found)
                    if env.get("untraced"):
                        reason = (f"no flow path could be traced from the breach toe ({env['end']}); "
                                  f"check the dam point, breach direction and terrain clip")
                    elif hits:
                        reason = ", ".join(f"{found.count(k)} {k}(s)" for k in ("structure", "road", "railroad",
                                                                                 "lifeline", "dam")
                                           if found.count(k)) + " inside the envelope"
                    elif env["edge"]:
                        reason = "envelope reaches the terrain edge"
                    else:
                        result, reason = "clear", f"nothing inside the envelope ({env['end']} limit, {reach:,.0f} m)"
            except _NoFlood as e:
                result, reason = "clear", "no breach flood: {}".format(e)
            except Exception as e:  # keep the batch going
                reason = "not screened: " + er.plain(e, dam_id, "6")
            aud = "no"
            if result == "clear" and not nf and rnd.random() < audit:
                aud = "yes"; stats["audit"] += 1
            stats[result] += 1
            for k, v in (("tri_result", result), ("tri_reason", reason), ("tri_reach_m", reach),
                         ("tri_hits", hits), ("tri_audit", aud), ("tri_tif", tif)):
                put(a, k, v)
            o = QgsFeature(fields); o.setGeometry(f.geometry()); o.setAttributes(a)
            sink.addFeature(o)
        feedback.pushInfo(f"triage: {stats['clear']} cleared ({stats['audit']} of them audited in 2D), "
                          f"{stats['needs 2D']} need the 2D model")
        return {"OUT": dest}
