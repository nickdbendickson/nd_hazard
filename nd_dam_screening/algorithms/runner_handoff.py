"""Tool 7b: send chosen dams to ND Dam Breach Runner ('ndbreach:rundam' + 'ndbreach:ndhazardclass')
and bring its class back next to the screening class. The field mapping is core/handoff.py."""
import os

from qgis import processing
from qgis.core import (QgsProcessingAlgorithm, QgsProcessingParameterFeatureSource,
                       QgsProcessingParameterEnum, QgsProcessingParameterRasterLayer,
                       QgsProcessingParameterFolderDestination, QgsProcessingParameterFile,
                       QgsProcessingParameterNumber, QgsProcessingParameterField,
                       QgsProcessingParameterString, QgsProcessingParameterFeatureSink,
                       QgsFeature, QgsProcessingException, QgsApplication,
                       QgsProcessingMultiStepFeedback, QgsWkbTypes)

from ..core import handoff as ho, hazard as hz
from ..core import nd_hazard as nh
from ..core import damrecord_io as dri
from ..damsuite import versions as vs
from ..damsuite import damdata as dd
from . import _common as cm
from ..compat import (FILE_FOLDER, FT_STRING, GEOM_POINT, NUM_DOUBLE, SRC_LINE, SRC_POINT, SRC_POLYGON)

NEW = [("rn_status", FT_STRING), ("rn_folder", FT_STRING),
       ("rn_class", FT_STRING), ("rn_border", FT_STRING),
       ("rn_gaps", FT_STRING), ("scr_class", FT_STRING),
       ("rn_agree", FT_STRING), ("rn_inputs", FT_STRING)]


def _file_of(value, context):
    """0.16.0: the file behind a layer parameter value - a path, or the source of a project /
    context layer - or "" (a memory layer, nothing given)."""
    if not isinstance(value, str) or not value.strip():
        return ""
    if os.path.exists(value.split("|")[0]):
        return value
    try:
        from qgis.core import QgsProcessingUtils
        lyr = QgsProcessingUtils.mapLayerFromString(value, context)
        src = str(lyr.source()) if lyr is not None else ""
    except Exception:                                            # noqa
        return ""
    return src if src and os.path.exists(src.split("|")[0]) else ""


def _user_method_index(alg):
    """Index of 'My own hydrograph' in the installed runner's METHOD list."""
    try:
        for i, o in enumerate(alg.parameterDefinition("METHOD").options()):
            if "own hydrograph" in str(o).lower():
                return i
    except Exception:           # older/newer runner or a test double: use the v3.0 index
        pass
    return ho.USER_METHOD_INDEX


class RunnerHandoff(QgsProcessingAlgorithm):
    def name(self): return "runnerhandoff"
    def displayName(self): return "7b - Detailed runs with ND Breach Runner"
    def group(self): return "Dam screening"
    def groupId(self): return "dams"
    def createInstance(self): return RunnerHandoff()

    def shortHelpString(self):
        return ("Sends the dams that matter to the ND Dam Breach Runner plugin (v3.1 or later, provider "
                "'ndbreach'; install it first) for a defensible breach flood and its ND 5.2 "
                "classification, one dam after the other, and puts the runner's class next to the "
                "screening class.\n"
                "For each chosen dam: 'ndbreach:rundam' with the dam ID, the tool-4 breach point as the "
                "dam point, height (ft, from height_m), storage at the top of dam (ac-ft, from tool 5 "
                "td_vw_m3, else tool 3, else the inventory maximum), the surface area near the top of "
                "dam when a tool-3 curve folder is given, your DEM and Dams folder. No DWR elevations "
                "are passed (local datum): the runner takes the top of dam from the DEM. Breach "
                "hydrograph: the runner's own ND method (default), or the screening's tool-5 "
                "hydrograph written to <Dams folder>/<dam>/inputs/breach_hydrograph.csv (hr, cfs) and "
                "passed as the runner's own-hydrograph CSV. Then 'ndbreach:ndhazardclass' on the dam "
                "folder with your receptor layers, the dams layer as downstream dams (5.3 cascading: D_ID = dam_id, "
                "D_CLASS = the existing class field), the uninhabited-building words, 'any depth' (WET_FT) "
                "and the unknown AADT / trains classes, and its JSON is read back. In 'High and Medium' mode "
                "dams without a screening class (NoData or empty: failed simulation) are sent too.\n"
                "Both plugins use the same rules module (core/nd_hazard.py, ND 5.2 / 5.3 with the ACER "
                "TM 11 charts); the chart method needs digitized and checked charts.\n"
                "OUTPUT: the input dams plus rn_status (done / failed: message / skipped: reason), "
                "rn_folder, rn_class, rn_border, rn_gaps, scr_class (the screening class, hz_class), "
                "rn_agree (agree / runner higher / runner lower) and rn_inputs (what was sent). A dam "
                "that fails is logged and the others continue. Runner runs take minutes to hours per "
                "dam; the runner needs LISFLOOD-FP set up in its own provider settings.\n"
                "DAM RECORD (v0.9): before each run, <Dams folder>/<dam ID>/dam.json (the suite's "
                "shared dam record) gets the values sent to the runner - dam_id, name, nid_id, DWR "
                "structure number, height, storage, surface area, the breach point (lon / lat and "
                "EPSG:5070 x / y), existing class - and a 'screened' status with the triage result "
                "(clear / needs 2D), tool 8's class, borderline flag and data gaps. A value another "
                "tool or the engineer put there is never overwritten (the conflict is logged and "
                "kept in the file). The tool stops when an installed ND Dam Breach Runner is older "
                "than this plugin needs (suite version check).\n"
                "SUITE DATA (0.16.0): the DEM and the receptor layers sent (files, not memory layers) "
                "are also recorded in dam.json's data section where it has no entry for them yet, so "
                "ND Dam Breach Runner and ND Dam Hazard fill their empty inputs with the same data. "
                "Without a DEM here, each dam's DEM comes from its dam folder (Plugins > ND Dam Suite > "
                "Suite data and servers > Get data for this dam).")

    def initAlgorithm(self, config=None):
        P = self.addParameter
        pt, ln, pg = SRC_POINT, SRC_LINE, SRC_POLYGON
        P(QgsProcessingParameterFeatureSource("DAMS", "Dams (tool 8 output, or tool 5/6/7 output; also the "
                                              "downstream dams for 5.3 cascading)", [pt]))
        P(QgsProcessingParameterField("DAM_CLASS", "Dams: existing hazard class field (5.3 cascading; DWR "
                                      "hazard_cla also found)", defaultValue="haz_existing",
                                      parentLayerParameterName="DAMS", optional=True))
        P(QgsProcessingParameterEnum("WHICH", "Dams to send", ho.SELECT_LABELS, defaultValue=0))
        P(QgsProcessingParameterFeatureSource("BPTS", "Breach points (tool 4; joined by dam_id)", [pt]))
        # 0.16.0: optional - empty takes each dam folder's DEM (dam.json data section)
        P(QgsProcessingParameterRasterLayer("DEM", "DEM for the runner (statewide DEM or VRT; empty = each "
                                            "dam folder's DEM from Get data for this dam)", optional=True))
        P(QgsProcessingParameterFolderDestination("DAMS_FOLDER", "Dams folder (the runner writes "
                                                  "<folder>/<dam ID>/)"))
        P(QgsProcessingParameterEnum("HYDRO", "Breach hydrograph", ho.HYDRO_LABELS, defaultValue=0))
        cm.distance_params(P, "CELL_FT", "CELL", "Runner cell size (ft; empty = runner default)",
                           None, min_ft=3.0, optional=True)
        P(QgsProcessingParameterFile("CURVES", "Stage-storage curve folder (tool 3; optional, gives the "
                                     "surface area near the top of dam)",
                                     behavior=FILE_FOLDER, optional=True))
        P(QgsProcessingParameterFeatureSource("BLD", "Structures (points or polygons; points are also "
                                              "given to rundam as receptors)", [pt, pg], optional=True))
        for k, desc in (("S_OCC", "occupancy class field (empty = runner matches OCC_CLS, OCCTYPE ...)"),
                        ("S_MOBILE", "mobile home field (empty = runner matches PRIM_OCC, mobile ...)"),
                        ("S_HABITABLE", "habitable yes/no field (empty = runner matches habitable ...)")):
            P(QgsProcessingParameterField(k, "Structures: " + desc, parentLayerParameterName="BLD",
                                          optional=True))
        P(QgsProcessingParameterString("UNINHABITED", "Structures: occupancy words that mark an UNINHABITED "
                                       "building (as tool 8)", defaultValue=", ".join(hz.UNINHABITED),
                                       optional=True))
        P(QgsProcessingParameterFeatureSource("ROADS", "Roads (lines)", [ln], optional=True))
        P(QgsProcessingParameterField("R_AADT", "Roads: AADT field (empty = runner matches aadt ...)",
                                      parentLayerParameterName="ROADS", optional=True))
        P(QgsProcessingParameterField("R_CLASS", "Roads: class / route field (empty = runner matches "
                                      "road_class ...)", parentLayerParameterName="ROADS", optional=True))
        P(QgsProcessingParameterField("R_SOLE", "Roads: sole access to an occupied location field (empty = "
                                      "runner matches sole_access ...)", parentLayerParameterName="ROADS",
                                      optional=True))
        P(QgsProcessingParameterFeatureSource("RAIL", "Railroads (lines)", [ln], optional=True))
        P(QgsProcessingParameterField("T_TRAINS", "Railroads: trains a day field (empty = runner matches "
                                      "trains_day ...)", parentLayerParameterName="RAIL", optional=True))
        P(QgsProcessingParameterField("T_PASSENGER", "Railroads: passenger field (empty = runner matches "
                                      "PASSNGR ...)", parentLayerParameterName="RAIL", optional=True))
        P(QgsProcessingParameterString("PASSENGER_CODES", "Railroads: passenger codes",
                                       defaultValue=", ".join(hz.PAX_CODES), optional=True))
        P(QgsProcessingParameterFeatureSource("LIFELINES", "Lifeline facilities (points or polygons)",
                                              [pt, pg], optional=True))
        P(QgsProcessingParameterField("L_KIND", "Lifelines: kind field", parentLayerParameterName="LIFELINES",
                                      optional=True))
        P(QgsProcessingParameterEnum("LETHALITY", "Runner classification: lethality (ND 5.2.1.1)",
                                     [nh.LETHALITY_LABELS[m] for m in nh.LETHALITY_METHODS],
                                     defaultValue=nh.LETHALITY_METHODS.index(nh.LETHALITY_CHARTS)))
        P(QgsProcessingParameterEnum("UNKNOWN_AADT", "Runner classification: class of a road with no AADT "
                                     "(a data gap)", list(nh.CLASSES), defaultValue=nh.CLASSES.index(nh.MEDIUM)))
        P(QgsProcessingParameterEnum("UNKNOWN_TRAINS", "Runner classification: class of a railroad with no "
                                     "trains a day (a data gap)", list(nh.CLASSES),
                                     defaultValue=nh.CLASSES.index(nh.MEDIUM)))
        P(QgsProcessingParameterNumber("WET_FT", "Runner classification: 'any depth' (ft; as tool 8's thresholds; "
                                       "the starting value 0.1 ft is YOUR allowance for model noise)",
                                       NUM_DOUBLE, hz.DEFAULT_WET_FT,
                                       minValue=hz.MIN_WET_FT))
        P(cm.hide(QgsProcessingParameterNumber("WET_M", "(before 0.11) 'any depth' (m)",
                                               NUM_DOUBLE, hz.DEFAULT_WET_M,
                                               minValue=hz.MIN_WET_M, optional=True)))
        P(QgsProcessingParameterFile("CHART_FILE", "Digitized ACER TM 11 chart file (.json; empty = the "
                                     "runner's own copy)", extension="json", optional=True))
        P(QgsProcessingParameterFeatureSink("OUT", "Dams with runner results", SRC_POINT))

    # ------------------------------------------------------------------
    def processAlgorithm(self, params, context, feedback):
        try:                                    # suite version check (damsuite.versions)
            vs.require("ndscreen", "ndbreach")
        except RuntimeError as e:
            raise QgsProcessingException(str(e))
        for line in vs.report_lines("ndscreen"):
            feedback.pushInfo("Suite: " + line)
        version = dri.plugin_version()
        reg = QgsApplication.processingRegistry()
        run_alg = reg.algorithmById(ho.RUNNER_ALG)
        if run_alg is None or reg.algorithmById(ho.HAZARD_ALG) is None:
            raise QgsProcessingException(
                "ND Dam Breach Runner v{}+ is not installed or not enabled: the Processing algorithms "
                "'{}' and '{}' were not found. Install ND Dam Breach Runner v{} or later (provider "
                "'ndbreach') and enable it in Plugins > Manage and Install Plugins, then run this tool "
                "again.".format(ho.RUNNER_MIN_VERSION, ho.RUNNER_ALG, ho.HAZARD_ALG,
                                ho.RUNNER_MIN_VERSION))
        dams = self.parameterAsSource(params, "DAMS", context)
        bpts = self.parameterAsSource(params, "BPTS", context)
        mode = self.parameterAsEnum(params, "WHICH", context)
        hydro = self.parameterAsEnum(params, "HYDRO", context)
        folder = self.parameterAsString(params, "DAMS_FOLDER", context)
        curves = self.parameterAsString(params, "CURVES", context) or None
        cell = cm.distance_m_opt(self, params, context, "CELL_FT", "CELL") or None
        lethality = self.parameterAsEnum(params, "LETHALITY", context)
        unknown_aadt = self.parameterAsEnum(params, "UNKNOWN_AADT", context)
        unknown_trains = self.parameterAsEnum(params, "UNKNOWN_TRAINS", context)
        wet_m, wet_ft, _src = cm.feet_or_legacy(self, params, context, "WET_FT", "WET_M", hz.DEFAULT_WET_FT)
        if wet_m < hz.MIN_WET_M:
            raise QgsProcessingException("'Any depth' must be at least {} ft ({} m)".format(hz.MIN_WET_FT,
                                                                                            hz.MIN_WET_M))
        chart_file = self.parameterAsString(params, "CHART_FILE", context) or ""
        S = lambda k: self.parameterAsString(params, k, context) or ""
        if dams is None or bpts is None:
            raise QgsProcessingException("Dams and breach points are required")
        dem_given = params.get("DEM") not in (None, "")
        if not dem_given:                       # 0.16.0: the dam folders' own DEMs, else stop
            if not any(dd.dam_data(os.path.join(folder, n)).get("dem")
                       for n in (os.listdir(folder) if folder and os.path.isdir(folder) else [])
                       if os.path.isdir(os.path.join(folder, n))):
                raise QgsProcessingException(
                    "A DEM is required (statewide DEM or VRT), or run Plugins > ND Dam Suite > Suite "
                    "data and servers > Get data for this dam for the dams first (their dam folders "
                    "have no DEM)")
            feedback.pushInfo("No DEM given: each dam's DEM comes from its dam folder (dam.json data section)")
        if nh.LETHALITY_METHODS[lethality] == nh.LETHALITY_CHARTS:
            # the runner's copy is byte-identical to core/acer_tm11_charts.json (same module
            # and data file in both plugins), so check here before hours of runs
            try:
                hz.load_ready_charts(chart_file or None,
                                     conservative_label=nh.LETHALITY_LABELS[nh.LETHALITY_ALL_WET])
            except nh.ChartsNotDigitized as e:
                raise QgsProcessingException(str(e))
            except (OSError, ValueError) as e:
                raise QgsProcessingException("Could not read the chart file {}: {}".format(
                    chart_file or nh.CHART_FILE, e))
        names = dams.fields().names()
        if mode in ho.NEEDS_TOOL8 and "hz_class" not in names:
            raise QgsProcessingException("'{}' needs the tool-8 fields (hz_class); these dams have none. "
                                         "Choose 'All dams' or 'Selected features only'.".format(
                                             ho.SELECT_LABELS[mode]))
        sel_ids = set()
        if mode == ho.SELECT_SELECTED:
            lyr = self.parameterAsVectorLayer(params, "DAMS", context)
            if lyr is None:
                raise QgsProcessingException("'Selected features only' needs the dams as a map layer")
            sel_ids = set(lyr.selectedFeatureIds())
            if not sel_ids:
                raise QgsProcessingException("No dams are selected in the dams layer")
        os.makedirs(folder, exist_ok=True)

        b2a = cm.to_albers(bpts)
        bnames = bpts.fields().names()
        if "dam_id" not in bnames:
            raise QgsProcessingException("The breach points have no dam_id field (tool 4 output expected)")
        bmap = {}
        for b in bpts.getFeatures():
            g = b.geometry()
            if g.isEmpty():
                continue
            g.transform(b2a); p = cm.rep_point(g)
            if p is not None:
                bmap[str(b["dam_id"])] = p

        bld = self.parameterAsSource(params, "BLD", context)
        bld_points = bld is not None and QgsWkbTypes.geometryType(bld.wkbType()) == GEOM_POINT
        if bld is not None and not bld_points:
            feedback.pushInfo("Structures are polygons: used for the classification only (rundam takes "
                              "receptor points).")
        receptors = {"RECEPTOR_POINTS": params.get("BLD") if bld_points else None,
                     "RECEPTOR_ROADS": params.get("ROADS"), "RECEPTOR_RAIL": params.get("RAIL")}
        d_class = S("DAM_CLASS") if S("DAM_CLASS") in names else ("hazard_cla" if "hazard_cla" in names else "")
        if not d_class:
            feedback.pushInfo("Dams: no existing hazard class field - the runner takes the downstream dams "
                              "it reaches as Medium (a data gap, 5.3).")
        layers = {"STRUCTURES": params.get("BLD"), "ROADS": params.get("ROADS"),
                  "RAIL": params.get("RAIL"), "LIFELINES": params.get("LIFELINES"),
                  "DOWNSTREAM_DAMS": params.get("DAMS")}
        fields_ = {k: S(k) for k in ("S_OCC", "S_MOBILE", "S_HABITABLE", "R_AADT", "R_CLASS", "R_SOLE",
                                     "T_TRAINS", "T_PASSENGER", "L_KIND")}
        fields_.update({"D_ID": "dam_id" if "dam_id" in names else "", "D_CLASS": d_class})
        words = ", ".join(hz.words(S("UNINHABITED"))) or ", ".join(hz.UNINHABITED)
        user_idx = _user_method_index(run_alg)
        # 0.16.0: the data files sent, for dam.json's data section (roles with no entry yet)
        used = {"dem": _file_of(params.get("DEM"), context),
                "structure_points" if bld_points else "structures": _file_of(params.get("BLD"), context),
                "roads": _file_of(params.get("ROADS"), context), "rail": _file_of(params.get("RAIL"), context),
                "lifelines": _file_of(params.get("LIFELINES"), context)}
        used = {k: v for k, v in used.items() if v}

        fields, put = cm.extend_fields(dams.fields(), NEW)
        sink, dest = cm.us_sink(self, params, "OUT", context, fields, dams.wkbType(), dams.sourceCrs())
        get = lambda f, k: cm.fval(f, names, k)
        feats = list(dams.getFeatures())
        chosen = []
        for f in feats:
            attrs = {n: get(f, n) for n in names}
            ok, why = ho.selected(attrs, mode, f.id() in sel_ids)
            chosen.append((f, attrs, ok, why))
        n_send = sum(1 for c in chosen if c[2]) or 1
        steps = QgsProcessingMultiStepFeedback(2 * n_send, feedback)
        feedback.pushInfo("{} of {} dams to send to ND Dam Breach Runner ({}); hydrograph: {}".format(
            sum(1 for c in chosen if c[2]), len(feats), ho.SELECT_LABELS[mode], ho.HYDRO_LABELS[hydro]))
        counts, agree = {"done": 0, "failed": 0, "skipped": 0}, {}
        k = 0
        for f, attrs, ok, why in chosen:
            dam_id = str(attrs.get("dam_id") or f.id())
            scr = attrs.get("hz_class")
            res = {"rn_status": None, "rn_folder": None, "rn_class": None, "rn_border": None,
                   "rn_gaps": None, "scr_class": scr, "rn_agree": None, "rn_inputs": None}
            if not ok:
                res["rn_status"] = "skipped: " + why
            elif feedback.isCanceled():
                res["rn_status"] = "skipped: cancelled"
            else:
                steps.setCurrentStep(2 * k)
                k += 1
                feedback.pushInfo("--- dam {} ({} of {}; sent: {})".format(dam_id, k, n_send, why))
                try:
                    if dam_id not in bmap:
                        raise ho.HandoffError("no breach point (tool 4) for this dam_id")
                    dfold = os.path.join(folder, ho.safe_name(dam_id))
                    user_csv = None
                    if hydro == ho.HYDRO_SCREENING:
                        src = attrs.get("td_csv")
                        if not src or not os.path.exists(str(src)):
                            raise ho.HandoffError("no tool-5 hydrograph (td_csv)")
                        user_csv, peak = ho.write_runner_hydrograph(str(src),
                                                                    os.path.join(dfold, ho.RUNNER_HYDRO_CSV))
                        feedback.pushInfo("tool-5 hydrograph -> {} (peak {:,.0f} cfs)".format(user_csv, peak))
                    area = None
                    if curves:
                        area = ho.area_at_top_m2(os.path.join(curves, f"curve_{dam_id}.csv"),
                                                 cm.fval(f, names, "est_crest_m", float))
                    dem = params.get("DEM")
                    if not dem_given:           # 0.16.0: the dam folder's DEM
                        dem = dd.dam_data(dfold).get("dem")
                        if not dem:
                            raise ho.HandoffError("no DEM: none given and the dam folder has none (Get "
                                                  "data for this dam)")
                        feedback.pushInfo("DEM from the dam folder: {}".format(dem))
                    p, notes = ho.rundam_params(attrs, bmap[dam_id], cm.ALBERS, dem, folder,
                                                cell, user_csv, area, user_idx)
                    for key, v in receptors.items():
                        if v not in (None, ""):
                            p[key] = v
                    res["rn_inputs"] = hz.limit_text(["sent: " + why] + notes)
                    for n in notes:
                        feedback.pushInfo(n)
                    try:                        # the suite's dam record, before the run
                        jpath, jlog = dri.write(
                            dfold, dri.handoff_values(attrs, p, bmap[dam_id], cm.ALBERS, d_class or None),
                            dri.screening_result(attrs, "tool 7b"), version, data=used,
                            field_notes=dri.estimate_notes(attrs))
                        for line in jlog:
                            feedback.pushInfo("dam {}: {}".format(dam_id, line))
                        if jpath:
                            feedback.pushInfo("dam record: {}".format(jpath))
                    except Exception as e:      # the record must never stop the run
                        feedback.reportError("dam {}: dam.json not written: {}".format(dam_id, e))
                    r1 = processing.run(ho.RUNNER_ALG, p, context=context, feedback=steps,
                                        is_child_algorithm=True)
                    dfold = (r1 or {}).get("FOLDER") or dfold
                    res["rn_folder"] = dfold
                    if feedback.isCanceled():
                        raise QgsProcessingException("cancelled after the flood run")
                    steps.setCurrentStep(2 * k - 1)
                    hp = ho.ndhazard_params(dfold, layers, fields_, lethality, unknown_aadt, chart_file,
                                            S("PASSENGER_CODES"), unknown_trains, wet_m, words)
                    r2 = processing.run(ho.HAZARD_ALG, hp, context=context, feedback=steps,
                                        is_child_algorithm=True)
                    info = ho.read_runner_json((r2 or {}).get("JSON") or os.path.join(dfold, ho.RUNNER_JSON))
                    res.update({"rn_status": "done", "rn_class": info["class"], "rn_border": info["border"],
                                "rn_gaps": info["gaps"], "rn_agree": ho.agreement(info["class"], scr)})
                    agree[res["rn_agree"]] = agree.get(res["rn_agree"], 0) + 1
                    feedback.pushInfo("dam {}: runner {} / screening {} ({})".format(
                        dam_id, info["class"], scr or "-", res["rn_agree"]))
                except Exception as e:          # one dam must not stop the batch
                    msg = str(e).strip() or type(e).__name__
                    res["rn_status"] = hz.limit_text(["failed: " + msg.replace("\n", " ")])
                    feedback.reportError("dam {}: {}".format(dam_id, msg))
            counts[res["rn_status"].split(":")[0]] = counts.get(res["rn_status"].split(":")[0], 0) + 1
            a = cm.blank_attrs(fields, f)
            for key, _ in NEW:
                put(a, key, res[key])
            out = QgsFeature(fields); out.setGeometry(f.geometry()); out.setAttributes(a)
            sink.addFeature(out)
        feedback.setProgress(100)
        feedback.pushInfo("runner hand-off: " + ", ".join(f"{k_} {v}" for k_, v in counts.items() if v))
        if agree:
            feedback.pushInfo("runner vs screening: " + ", ".join(f"{k_} {v}" for k_, v in sorted(agree.items())))
        return {"OUT": dest}
