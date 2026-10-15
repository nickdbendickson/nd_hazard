"""Merge ND DWR dams (primary) with the NID (secondary) into one metric layer."""
from qgis.core import (QgsProcessingAlgorithm, QgsProcessingParameterFeatureSource,
                       QgsProcessingParameterField, QgsProcessingParameterNumber,
                       QgsProcessingParameterEnum, QgsProcessingParameterString,
                       QgsProcessingParameterFeatureSink,
                       QgsProcessingException, QgsFields, QgsField,
                       QgsFeature, QgsCoordinateTransform,
                       QgsCoordinateReferenceSystem, QgsProject, QgsSpatialIndex,
                       QgsPointXY, QgsGeometry)

from ..core import inventory as inv
from ..core import nd_counties as cty
from ..compat import (FIELD_ANY, FIELD_NUMERIC, FT_DOUBLE, FT_STRING, NUM_DOUBLE, SRC_POINT, WKB_POINT)
from . import _common as cm

OUT_FIELDS = [("dam_id", FT_STRING), ("name", FT_STRING),
              ("source", FT_STRING), ("nid_id", FT_STRING),
              ("height_m", FT_DOUBLE), ("inv_norm_m3", FT_DOUBLE),
              ("inv_max_m3", FT_DOUBLE), ("inv_area_m2", FT_DOUBLE),
              ("haz_existing", FT_STRING), ("status", FT_STRING),
              ("crest_len_m", FT_DOUBLE), ("shared_vol", FT_STRING),
              # 0.19.0: which DWR inventory, where the height / storage come from, dugout values
              ("kind", FT_STRING), ("h_src", FT_STRING), ("stor_src", FT_STRING),
              ("inv_total_m3", FT_DOUBLE), ("inv_above_m3", FT_DOUBLE),
              ("dug_depth_m", FT_DOUBLE), ("dug_h_m", FT_DOUBLE), ("inv_notes", FT_STRING)]
KIND_CHOICES = ["Auto-detect from the layer", "DWR dams", "DWR unknown structures", "DWR dugouts"]

# field defaults for the ND DWR "Dams by Hazard Class" export
FIELD_DEFAULTS = {f"DWR_{k}": v for k, v in inv.DWR_DEFAULTS.items()}

FIELD_KEYS = [("ID", "ID field", FIELD_ANY),
              ("NAME", "name field", FIELD_ANY),
              ("H", "dam height", FIELD_NUMERIC),
              ("NS", "normal storage", FIELD_NUMERIC),
              ("MS", "max storage", FIELD_NUMERIC),
              ("SA", "surface area", FIELD_NUMERIC),
              ("HZ", "hazard class", FIELD_ANY),
              ("STATUS", "status (e.g. Intact / Breached)", FIELD_ANY),
              ("NIDREF", "NID ID (links a DWR dam to the NID layer)", FIELD_ANY),
              ("LEN", "dam length", FIELD_NUMERIC)]


class PrepareDams(QgsProcessingAlgorithm):
    DWR, NID, OUT, DIST, KEEP, MINH, MINV = ("DWR", "NID", "OUT", "DIST", "KEEP", "MINH", "MINV")
    UNITS = ["Feet / acre-feet / acres", "Metres / m3 / m2"]

    def name(self): return "preparedams"
    def displayName(self): return "1 - Prepare dam inventory"
    def group(self): return "Dam screening"
    def groupId(self): return "dams"
    def createInstance(self): return PrepareDams()

    def shortHelpString(self):
        return ("Builds one metric dam layer. Defaults match the ND DWR 'Dams by Hazard Class' "
                "export (feet, acre-feet, acres; 0 = not recorded).\n"
                "0.19.0 - the DWR layer can also be DWR's UNKNOWN STRUCTURES or DUGOUTS export "
                "(DWR_Unknown_Structures / DWR_Dugouts): the kind is detected from the layer name "
                "and fields, or chosen. Their structure numbers are separate number series, so the "
                "dam IDs are UNK-<number> and DUG-<number> (never joined to a dam's line by number). "
                "A field that is not in the layer is replaced by the same field under its other "
                "export name (structure_ / structure_id, max_dam_he / max_dam_height ...). Status "
                "'auto' keeps Intact dams; Intact, Unknown and blank-status unknown structures; "
                "Intact and Pending dugouts. A dugout's breach storage is DWR's storage ABOVE "
                "GRADE when recorded (its total storage is mostly below the ground and cannot run "
                "out); without it the dugout has no inventory storage - run tool 16 on DWR's "
                "dugout polygons for the LiDAR above-grade storage. Missing heights and storages "
                "are estimated from the DEM by tool 3 and marked h_src / stor_src = estimated.\n"
                "Status filter: only dams whose status is in the list are kept (default 'Intact'); "
                "breached, removed, not-built, submerged and location-inconsistent dams are "
                "dropped. Leave empty to keep all.\n"
                "Size filter (optional): a dam is skipped only when BOTH its height and its storage "
                "are known and BOTH are below your limits, so dams with missing data are never "
                "skipped by accident. Use it to run the ~1000 larger dams first.\n"
                "NID (optional): matched to DWR dams first by NID ID, then by distance; matched "
                "values fill gaps in the DWR record (source = DWR+NID). NID dams with no DWR dam "
                "are added (source = NID).\n"
                "shared_vol = YES marks records that share an identical large volume (e.g. levee "
                "'reach' segments of one reservoir): they are not separate reservoirs and need "
                "manual handling. Existing hazard text is kept as-is; DWR says 'Significant' where "
                "the ND policy says 'Medium'.")

    def initAlgorithm(self, config=None):
        for src, label in ((self.DWR, "ND DWR dams"), (self.NID, "NID dams")):
            self.addParameter(QgsProcessingParameterFeatureSource(
                src, f"{label} (points)", [SRC_POINT], optional=True))
            self.addParameter(QgsProcessingParameterEnum(
                f"{src}_UNITS", f"{label}: units", self.UNITS, defaultValue=0))
            for key, lab, kind in FIELD_KEYS:
                # 0.21.0: an empty field box is found in the layer (the dialog fills it in when
                # the layer is picked; the run looks again) - the label says so
                self.addParameter(QgsProcessingParameterField(
                    f"{src}_{key}", f"{label}: {lab} (empty = found in the layer)",
                    defaultValue=FIELD_DEFAULTS.get(f"{src}_{key}"),
                    parentLayerParameterName=src, type=kind, optional=True))
        self.addParameter(QgsProcessingParameterEnum(
            "DWR_KIND", "ND DWR layer: which DWR inventory it is", KIND_CHOICES, defaultValue=0))
        self.addParameter(QgsProcessingParameterString(
            self.KEEP, "Keep only these DWR statuses (comma-separated; empty = all; auto = Intact "
            "dams / Intact, Unknown, (blank) unknown structures / Intact, Pending dugouts)",
            defaultValue="auto", optional=True))
        self.addParameter(QgsProcessingParameterString(
            "ONLY_IDS", "Only these dams (comma-separated DWR structure IDs, NID IDs or dam IDs such as "
            "DWR-5289; empty = all) - 'Run one dam' uses this", defaultValue="", optional=True))
        # 0.21.1: one or more counties (by where the dam point is)
        self.addParameter(QgsProcessingParameterEnum(
            "COUNTIES", "Only dams in these counties (none ticked = all of North Dakota; by where "
            "the dam point is)", cty.names(), allowMultiple=True, defaultValue=[], optional=True))
        self.addParameter(QgsProcessingParameterNumber(
            self.MINH, "Size filter: skip dams lower than (ft)...",
            NUM_DOUBLE, 0.0, minValue=0))
        self.addParameter(QgsProcessingParameterNumber(
            self.MINV, "...AND holding less than (acre-ft). 0 and 0 = no size filter",
            NUM_DOUBLE, 0.0, minValue=0))
        cm.distance_params(self.addParameter, "DIST_FT", self.DIST,
                           "Match distance (ft) for DWR/NID pairs without a shared NID ID", 1600.0, min_ft=0.0)
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUT, "Standardized dams", SRC_POINT))

    # ------------------------------------------------------------------
    def _read(self, params, context, key, tag, feedback=None):
        """Return [(wgs_geometry, record dict)] for one source layer."""
        src = self.parameterAsSource(params, key, context)
        if src is None:
            return []
        metric = self.parameterAsEnum(params, f"{key}_UNITS", context) == 1
        fld = {k: (self.parameterAsString(params, f"{key}_{k}", context) or None)
               for k, _l, _t in FIELD_KEYS}
        kind = inv.KIND_DAM
        if key == self.DWR:                  # 0.19.0: the three DWR inventories
            choice = self.parameterAsEnum(params, "DWR_KIND", context)
            names = src.fields().names()
            lname = ""
            try:
                lname = self.parameterAsVectorLayer(params, key, context).name()
            except Exception:                # noqa - a feature source without a layer
                lname = str(params.get(key) or "")
            kind = inv.detect_kind(names, lname) if choice == 0 else inv.KINDS[choice - 1]
            chosen = dict(fld)
            fld, notes = inv.resolve_fields(fld, names, kind)
            notes += _found_notes(chosen, fld)
            self.kind = kind
            if feedback is not None:
                feedback.pushInfo("DWR layer read as: {}{}".format(
                    kind.upper(), " (detected)" if choice == 0 else ""))
                for n in notes:
                    feedback.pushInfo("  " + n)
                if not fld.get("ID"):
                    raise QgsProcessingException("The DWR layer has no structure ID field "
                                                 "(structure_ / structure_id); choose it.")
        else:                                # 0.21.0: the NID's fields are found the same way
            chosen = dict(fld)
            fld, notes = inv.resolve_fields(fld, src.fields().names(), inv.KIND_DAM,
                                            inv.NID_FIELD_CANDIDATES)
            notes += _found_notes(chosen, fld)
            if feedback is not None:
                for n in notes:
                    feedback.pushInfo("  NID " + n)
        wgs = QgsCoordinateReferenceSystem("EPSG:4326")
        tr = QgsCoordinateTransform(src.sourceCrs(), wgs, QgsProject.instance())
        low = {n.lower(): n for n in src.fields().names()}
        cfield = low.get("county") or low.get("countystate") or low.get("county_name")
        out = []
        for i, f in enumerate(src.getFeatures()):
            g = f.geometry()
            if g.isNull() or g.isEmpty():
                continue
            g.transform(tr)
            if g.isMultipart():                  # DWR points are stored as multipoints
                g = g.centroid()
            rec = inv.standardize(lambda n, _f=f: _f[n], fld, metric, tag, i, kind)
            if cfield:                       # 0.21.1: the county the record names (a fallback)
                rec["county_attr"] = f[cfield]
            out.append((g, rec))
        return out

    def processAlgorithm(self, params, context, feedback):
        dist = cm.distance_m(self, params, context, "DIST_FT", self.DIST, 1600.0)
        min_h = self.parameterAsDouble(params, self.MINH, context)
        min_v = self.parameterAsDouble(params, self.MINV, context)
        self.kind = inv.KIND_DAM
        dwr = self._read(params, context, self.DWR, "DWR", feedback)
        kind = self.kind
        keep_text = inv.keep_for(self.parameterAsString(params, self.KEEP, context), kind)
        keep = inv.parse_status_list(keep_text)
        if dwr:
            feedback.pushInfo("status filter: " + (keep_text or "none (all kept)"))
        nid = self._read(params, context, self.NID, "NID", feedback)
        if not dwr and not nid:
            raise QgsProcessingException("Provide a DWR layer, an NID layer, or both.")

        fields = QgsFields()
        for n, t in OUT_FIELDS:
            fields.append(QgsField(n, t))
        wgs = QgsCoordinateReferenceSystem("EPSG:4326")
        sink, dest = cm.us_sink(self, params, self.OUT, context, fields,
                                          WKB_POINT, wgs)
        to_alb = QgsCoordinateTransform(wgs, QgsCoordinateReferenceSystem("EPSG:5070"),
                                        QgsProject.instance())

        def albers(g):
            g2 = QgsGeometry(g); g2.transform(to_alb)
            return g2.asPoint()

        # --- match NID dams to DWR dams (against ALL DWR records, so that a NID dam whose
        # --- DWR twin is breached is dropped with it instead of coming back as "NID only")
        fill, used = {}, set()
        nid_by_id = {a["id"].strip().upper(): j for j, (_g, a) in enumerate(nid)}
        for i, (_g, a) in enumerate(dwr):
            j = nid_by_id.get(a["nidref"]) if a["nidref"] else None
            if j is not None and j not in used:
                fill[i] = nid[j][1]; used.add(j)
        n_by_id = len(fill)
        if dwr and nid:
            index, dwr_pts = QgsSpatialIndex(), {}
            for i, (g, _a) in enumerate(dwr):
                if i in fill:
                    continue
                p = albers(g); dwr_pts[i] = p
                ft = QgsFeature(i)
                ft.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(p.x(), p.y())))
                index.addFeature(ft)
            for j, (g, _a) in enumerate(nid):
                if j in used:
                    continue
                p = albers(g)
                hit = index.nearestNeighbor(QgsPointXY(p.x(), p.y()), 1, dist)
                if hit and hit[0] in dwr_pts and dwr_pts[hit[0]].distance(p) <= dist \
                        and hit[0] not in fill:
                    fill[hit[0]] = nid[j][1]; used.add(j)

        # --- assemble the output records
        only = {x.strip().upper() for x in (self.parameterAsString(params, "ONLY_IDS", context) or "").split(",")
                if x.strip()}

        def wanted(*ids):
            return not only or any(str(i).strip().upper() in only for i in ids if i not in (None, ""))

        # 0.21.1: the county filter, by the dam point (a point outside every county outline -
        # a few sit just over the state line - goes by the county its record names)
        names = cty.names()
        counties = [names[i] for i in self.parameterAsEnums(params, "COUNTIES", context)
                    if 0 <= int(i) < len(names)]
        if counties:
            feedback.pushInfo("county filter: " + ", ".join(counties))

        def in_county(g, a):
            if not counties:
                return True
            p = g.asPoint()
            c = cty.county_of(p.x(), p.y())
            if not c:
                c = cty.normalize(str(a.get("county_attr") or "").split(",")[0])
            return c in counties

        recs, n_status, n_size, n_only, n_county = [], 0, 0, 0, 0
        for i, (g, a) in enumerate(dwr):
            if not inv.status_ok(a["status"], keep):
                n_status += 1
                continue
            if not in_county(g, a):
                n_county += 1
                continue
            a = dict(a); bare = a["id"]; a["id"] = inv.ID_PREFIX[kind] + bare
            if not wanted(a["id"], bare, a["nidref"]):
                n_only += 1
                continue
            src, nid_id = "DWR", a["nidref"]
            if i in fill:
                n = fill[i]; src = "DWR+NID"; nid_id = nid_id or n["id"]
                for k in ("h", "ns", "ms", "sa", "len"):
                    if a[k] is None: a[k] = n[k]
                if not a["name"]: a["name"] = n["name"]
                if not a["hz"]: a["hz"] = n["hz"]
            if inv.size_skip(a["h"], a["ns"] or a["ms"], min_h, min_v):
                n_size += 1
                continue
            recs.append((g, a, src, nid_id))
        for j, (g, a) in enumerate(nid):
            if j in used:
                continue
            if not in_county(g, a):
                n_county += 1
                continue
            if inv.size_skip(a["h"], a["ns"] or a["ms"], min_h, min_v):
                n_size += 1
                continue
            a = dict(a); a["id"] = f"NID-{a['id']}"
            if not wanted(a["id"], a["id"][4:]):
                n_only += 1
                continue
            recs.append((g, a, "NID", a["id"][4:]))

        shared = inv.shared_volume_flags([r[1]["ms"] or r[1]["ns"] for r in recs])
        n_noh = n_nov = 0
        for (g, a, src, nid_id), sh in zip(recs, shared):
            o = QgsFeature(fields); o.setGeometry(g)
            k = a.get("kind") or inv.KIND_DAM
            notes = []
            if k == inv.KIND_DUGOUT:
                notes.append("dugout: the breach can release only the water above the natural "
                             "ground" + (" (DWR above-grade storage used)" if a.get("above")
                                         else "; DWR has no above-grade storage - tool 3 estimates "
                                              "it, tool 16 on the dugout polygon measures it"))
            elif k == inv.KIND_UNKNOWN:
                notes.append("DWR unknown structure: not inspected; location and values unverified")
            h_src = "inventory" if a["h"] else ("none (estimate in tool 3)" if k != inv.KIND_DUGOUT
                                                else "none (dugout)")
            stor_src = "inventory" if (a["ms"] or a["ns"]) else "none (estimate in tool 3)"
            n_noh += not a["h"]
            n_nov += not (a["ms"] or a["ns"])
            o.setAttributes([a["id"], a["name"], src, nid_id, a["h"], a["ns"], a["ms"], a["sa"],
                             a["hz"], a["status"], a["len"], "YES" if sh else "",
                             k, h_src, stor_src, a.get("total"), a.get("above"), a.get("depth"),
                             a.get("dug_h"), "; ".join(notes)])
            sink.addFeature(o)
        if recs and kind != inv.KIND_DAM:
            feedback.pushInfo("{}: {} with no height, {} with no storage in the inventory (tool 3 "
                              "estimates them from the DEM)".format(kind, n_noh, n_nov))
        feedback.pushInfo(
            f"{len(recs)} dams written | dropped by status: {n_status}, by size: {n_size} | "
            f"NID matched: {len(used)} ({n_by_id} by ID) | flagged shared volume: {sum(shared)}" +
            (f" | not in the chosen IDs: {n_only}" if only else "") +
            (f" | outside the chosen counties: {n_county}" if counties else ""))
        if counties and not recs and not only:
            raise QgsProcessingException("No dam of the layers is in {}.".format(", ".join(counties)))
        if only and not recs:
            raise QgsProcessingException("None of the dams matches the chosen IDs ({}); check the ID "
                                         "(DWR structure ID such as 5289, or DWR-5289).".format(", ".join(sorted(only))))
        # 0.21.0: tools 2-4 opened from the Processing Toolbox start with this layer
        cm.remember_output("dams", dest)
        if cm.is_temporary(dest):
            feedback.pushInfo(
                "Standardized dams went to a temporary layer: the Screening window looks for "
                "1_dams.gpkg in its work folder. Run tool 1 from the window (All steps), or save "
                "this layer as <work folder>/1_dams.gpkg, so the next steps find it there too.")
        return {self.OUT: dest}

    # 0.21.0: the dialog fills the field boxes in from the layer when a DWR / NID layer is picked
    def createCustomParametersWidget(self, parent=None):
        try:
            from processing.gui.AlgorithmDialog import AlgorithmDialog
        except Exception:                    # noqa - no Processing GUI (qgis_process, tests)
            return None
        try:
            dlg = AlgorithmDialog(self.create(), parent=parent)
            attach_field_fill(dlg)
            return dlg
        except Exception:                    # noqa - QGIS then opens its own dialog
            return None


def _found_notes(chosen, fld):
    """Run-log lines for the fields found in the layer because the box was left empty."""
    return ["{} field: '{}' (found in the layer)".format(role, f)
            for role, f in fld.items() if f and not chosen.get(role) and role in dict(
                (k, 1) for k, _l, _t in FIELD_KEYS)]


def fill_fields(src, names, layer_name="", kind_choice=0, current=None):
    """{parameter name: field} for the field boxes of layer parameter `src` ("DWR" / "NID"):
    the field found in the layer for each box that is empty or names a field the layer does
    not have. Pure Python (the dialog hook below and the tests)."""
    current = current or {}
    names = list(names or [])
    if not names:
        return {}
    if src == PrepareDams.DWR:
        kind = inv.detect_kind(names, layer_name) if not kind_choice else inv.KINDS[kind_choice - 1]
        found, _n = inv.resolve_fields({}, names, kind)
    else:
        found, _n = inv.resolve_fields({}, names, inv.KIND_DAM, inv.NID_FIELD_CANDIDATES)
    out = {}
    for key, _lab, _kind in FIELD_KEYS:
        cur = current.get(key)
        if (not cur or cur not in names) and found.get(key):
            out["{}_{}".format(src, key)] = found[key]
    return out


def attach_field_fill(dlg):
    """Hook the field boxes of a tool-1 dialog to its DWR / NID layer boxes."""
    from qgis.core import QgsProcessingContext, QgsProcessingUtils, QgsProject
    panel = dlg.mainWidget()
    wrappers = getattr(panel, "wrappers", {}) or {}
    context = QgsProcessingContext()
    context.setProject(QgsProject.instance())        # a layer picked from the project, by its id

    def value_of(name):
        w = wrappers.get(name)
        try:
            return w.parameterValue() if w is not None else None
        except Exception:                    # noqa
            return None

    def refill(src):
        id_box = wrappers.get("{}_ID".format(src))
        if id_box is None:
            return
        combo = id_box.wrappedWidget()
        names, lname = [], ""
        try:
            names = [f.name() for f in combo.fields()]
        except Exception:                    # noqa
            pass
        try:
            lay = combo.layer()
            lname = lay.name() if lay is not None else ""
        except Exception:                    # noqa
            lay = None
        if not lname:
            v = value_of(src)
            try:
                v = v.source.staticValue() if hasattr(v, "source") else v
                lay = QgsProcessingUtils.mapLayerFromString(str(v or ""), context)
                lname = lay.name() if lay is not None else str(v or "")
                if not names and lay is not None:
                    names = lay.fields().names()
            except Exception:                # noqa
                lname = str(v or "")
        choice = 0
        if src == PrepareDams.DWR:
            try:
                choice = int(value_of("DWR_KIND") or 0)
            except (TypeError, ValueError):
                choice = 0
        current = {k: value_of("{}_{}".format(src, k)) for k, _l, _t in FIELD_KEYS}
        for name, field in fill_fields(src, names, lname, choice, current).items():
            w = wrappers.get(name)
            if w is not None:
                try:
                    w.setParameterValue(field, context)
                except Exception:            # noqa
                    pass

    for src in (PrepareDams.DWR, PrepareDams.NID):
        w = wrappers.get(src)
        if w is None:
            continue
        w.widgetValueHasChanged.connect(lambda _w=None, _s=src: refill(_s))
        refill(src)
    kind = wrappers.get("DWR_KIND")
    if kind is not None:
        kind.widgetValueHasChanged.connect(lambda _w=None: refill(PrepareDams.DWR))
    dlg._nd_refill = refill                  # the tests call it
