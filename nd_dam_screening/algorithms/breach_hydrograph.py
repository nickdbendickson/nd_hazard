"""Breach parameters + outflow hydrograph per dam (ND standard: top-of-dam, piping)."""
import csv
import os

import numpy as np
from qgis.core import (QgsProcessingAlgorithm, QgsProcessingParameterFeatureSource,
                       QgsProcessingParameterFile, QgsProcessingParameterEnum,
                       QgsProcessingParameterBoolean, QgsProcessingParameterNumber,
                       QgsProcessingParameterField,
                       QgsProcessingParameterFeatureSink, QgsFields,
                       QgsField, QgsFeature)

from ..core import breach_hydro as bh
from ..core.proposed import PHRASE as PROPOSED_PHRASE
from . import _common as cm
from ..core import errors as er
from ..compat import (FIELD_NUMERIC, FILE_FOLDER, FT_DOUBLE, FT_STRING, NUM_DOUBLE, SRC_POINT)

AF_M3 = 1233.4818375
SCEN_FIELDS = [("start_m", FT_DOUBLE), ("vw_m3", FT_DOUBLE),
               ("hb_m", FT_DOUBLE), ("bavg_m", FT_DOUBLE),
               ("bb_m", FT_DOUBLE), ("z", FT_DOUBLE),
               ("tf_min", FT_DOUBLE), ("qp_m3s", FT_DOUBLE),
               ("tp_min", FT_DOUBLE), ("qp_f95", FT_DOUBLE),
               ("qp_tr60", FT_DOUBLE), ("rel_m3", FT_DOUBLE),
               ("csv", FT_STRING)]
BASE_FIELDS = [("base_m", FT_DOUBLE), ("store_af", FT_DOUBLE),
               ("brch_eq", FT_STRING), ("brch_mode", FT_STRING),   # 0.28.0: piping / overtopping
               ("brch_src", FT_STRING),
               ("simplified", FT_STRING), ("flow_az", FT_DOUBLE),
               ("dir_conf", FT_STRING), ("brch_conf", FT_STRING),
               ("brch_notes", FT_STRING)]
FT_M = 0.3048
# where the breach parameters come from (PARAM_SOURCE)
PARAM_SOURCES = ["Equation chosen below",
                 "Fields of the dams layer (where filled; equation elsewhere)",
                 "These values for every dam (fields of the dams layer still win where filled)"]
# (parameter key, field parameter, value parameter, label, unit text, to-SI factor)
USER_PARAMS = [("bavg_m", "USER_B_FIELD", "USER_B_FT", "average breach width", "ft", FT_M),
               ("tf_s", "USER_TF_FIELD", "USER_TF_HR", "breach formation time", "hr", 3600.0),
               ("z", "USER_Z_FIELD", "USER_Z", "breach side slope", "H:1V", 1.0),
               ("invert_m", "USER_BOT_FIELD", "USER_BOT_FT", "breach bottom elevation",
                "ft, same datum as the DEM", FT_M)]


class _NoFlood(Exception):
    """A pond marked no_flood = YES by tool 16: nothing stands above the natural ground."""


class BreachHydrograph(QgsProcessingAlgorithm):
    DAMS, CURVES, BPTS, MODE, SUNNY, EXP, OUTDIR, OUT, METHOD = (
        "DAMS", "CURVES", "BPTS", "MODE", "SUNNY", "EXP", "OUTDIR", "OUT", "METHOD")
    PARAM_SOURCE = "PARAM_SOURCE"
    METHODS = list(bh.BREACH_METHODS)

    def name(self): return "breachhydrograph"
    def displayName(self): return "5 - Breach parameters and hydrograph"
    def group(self): return "Dam screening"
    def groupId(self): return "dams"
    def createInstance(self): return BreachHydrograph()

    def shortHelpString(self):
        return ("ND Dam Safety Standards 5.1.1 scenario: water at the top of dam, no added "
                "inflow, no downstream flooding, piping breach (td_ fields). Optional sunny-day "
                "run at normal pool (sd_ fields). Breach width and formation time: Froehlich "
                "(2008) by default; Froehlich (1995), Von Thun & Gillette (1990, easily erodible "
                "or erosion resistant) and MacDonald & Langridge-Monopolis (1984; the embankment "
                "section is assumed 12 ft crest, 3H:1V faces because the inventory has neither) "
                "can be chosen, with the same formulas as the ND Dam Breach Runner, and tool 9 "
                "runs them as a sensitivity. NRCS TR-60 gives a peak discharge instead (its "
                "lower / upper limits applied, the same assumed section) with a triangular or "
                "curvilinear hydrograph whose volume is the releasable storage and whose rise "
                "time is the Froehlich (2008) formation time; it is not routed.\n"
                "YOUR OWN BREACH PARAMETERS: 'Where the breach parameters come from' lets fields "
                "of the dams layer (average width ft, formation time hr, side slope H:1V, breach "
                "bottom elevation ft in the DEM datum) or one set of typed values replace the "
                "equation. A field wins where it is filled (> 0; the bottom elevation where not "
                "empty), then a typed value (> 0), then the equation; brch_src records which "
                "source gave each dam's width / time / slope / bottom. A breach bottom above the "
                "toe leaves the storage below it behind the dam.\n"
                "The hydrograph is level-pool routed through a growing trapezoidal "
                "breach over the stage-storage curve from tool 3 (calibrated to inventory "
                "normal storage by tool 3 when available). Froehlich (1995) and NRCS TR-60 peaks are "
                "given as cross-checks (TR-60 only above 10.5 ft of water). Simplification: "
                "piping is modelled as a top-down weir breach with no orifice phase and no "
                "tailwater. simplified = YES marks dams under 200 ac-ft, which the ND policy "
                "may allow to skip breach modelling when the classification is obvious. "
                "Screening-level only.")

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.DAMS, "Dams with storage (output of tool 3)", [SRC_POINT]))
        self.addParameter(QgsProcessingParameterFile(
            self.CURVES, "Stage-storage curve folder (from tool 3)",
            behavior=FILE_FOLDER))
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.BPTS, "Breach points (output of tool 4, optional)",
            [SRC_POINT], optional=True))
        self.addParameter(QgsProcessingParameterEnum(
            self.MODE, "Breach mode", ["Piping (ND standard)", "Overtopping"], defaultValue=0))
        self.addParameter(QgsProcessingParameterEnum(
            self.METHOD, "Breach width / formation time equation",
            [bh.BREACH_METHODS[k][0] for k in self.METHODS], defaultValue=0))
        self.addParameter(QgsProcessingParameterEnum(
            self.PARAM_SOURCE, "Where the breach parameters come from", PARAM_SOURCES,
            defaultValue=0))
        for key, fld, val, label, unit, _f in USER_PARAMS:
            self.addParameter(QgsProcessingParameterField(
                fld, f"Dams layer field: {label} ({unit}; optional)",
                parentLayerParameterName=self.DAMS, type=FIELD_NUMERIC,
                optional=True))
            self.addParameter(QgsProcessingParameterNumber(
                val, f"Value for every dam: {label} ({unit}; 0 = not given)",
                NUM_DOUBLE, 0.0, optional=True))
        self.addParameter(QgsProcessingParameterBoolean(
            self.SUNNY, "Also run sunny-day scenario at normal pool", defaultValue=False))
        self.addParameter(QgsProcessingParameterNumber(
            self.EXP, "Below-pool storage exponent (2 = bowl, 3 = cone)",
            NUM_DOUBLE, 2.0, minValue=1.0, maxValue=4.0))
        self.addParameter(QgsProcessingParameterFile(
            self.OUTDIR, "Hydrograph CSV folder", behavior=FILE_FOLDER))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUT, "Dams with breach parameters", SRC_POINT))

    @staticmethod
    def _read_curve(path):
        with open(path) as fh:
            rows = list(csv.DictReader(fh))
        return [float(r["elev_m"]) for r in rows], [float(r["storage_m3"]) for r in rows]

    @staticmethod
    def _write_csv(path, t, q):
        idx = np.unique(np.concatenate((np.linspace(0, len(t) - 1, min(len(t), 600)).astype(int),
                                        [int(np.argmax(q))])))
        with open(path, "w", newline="") as fh:
            # 0.18.0: minutes and cfs appended after the metric pair (readers take columns 0 and 1)
            w = csv.writer(fh); w.writerow(["time_s", "discharge_m3s", "time_min", "discharge_cfs"])
            for i in idx: w.writerow([f"{t[i]:.1f}", f"{q[i]:.3f}", f"{t[i] / 60.0:.2f}",
                                      f"{q[i] / 0.028316846592:.1f}"])

    def processAlgorithm(self, params, context, feedback):
        dams = self.parameterAsSource(params, self.DAMS, context)
        bpts = self.parameterAsSource(params, self.BPTS, context)
        curves = self.parameterAsString(params, self.CURVES, context)
        outdir = self.parameterAsString(params, self.OUTDIR, context)
        mode = ["piping", "overtopping"][self.parameterAsEnum(params, self.MODE, context)]
        sunny = self.parameterAsBool(params, self.SUNNY, context)
        exp = self.parameterAsDouble(params, self.EXP, context)
        method = self.METHODS[self.parameterAsEnum(params, self.METHOD, context)]
        psrc = self.parameterAsEnum(params, self.PARAM_SOURCE, context)
        user_fields, user_vals = {}, {}
        for key, fld, val, label, unit, fac in USER_PARAMS:
            fname = self.parameterAsString(params, fld, context) if psrc else ""
            if fname and fname in dams.fields().names():
                user_fields[key] = (fname, fac)
            elif fname:
                feedback.reportError(f"{label}: field '{fname}' is not in the dams layer; ignored")
            v = self.parameterAsDouble(params, val, context) if psrc == 2 else 0.0
            if v and (v > 0 or key == "invert_m"):
                user_vals[key] = (v * fac, f"value {v:g} {unit.split(',')[0]}")
        if psrc:
            feedback.pushInfo("breach parameters: " + PARAM_SOURCES[psrc] + "; fields: " +
                              (", ".join(f"{k} <- {f[0]}" for k, f in user_fields.items()) or "none") +
                              "; values: " + (", ".join(v[1] for v in user_vals.values()) or "none"))
        os.makedirs(outdir, exist_ok=True)

        bmap = {}
        if bpts is not None:
            for b in bpts.getFeatures():
                bmap[str(b["dam_id"])] = b

        prefixes = ["td"] + (["sd"] if sunny else [])
        fields = QgsFields(dams.fields())
        for n, t in BASE_FIELDS: fields.append(QgsField(n, t))
        for pf in prefixes:
            for n, t in SCEN_FIELDS: fields.append(QgsField(f"{pf}_{n}", t))
        sink, dest = cm.us_sink(self, params, self.OUT, context, fields,
                                          dams.wkbType(), dams.sourceCrs())
        names = dams.fields().names()
        total = dams.featureCount() or 1
        done = fail = skipped = 0

        def get(f, k):
            if k not in names: return None
            v = f[k]
            return None if v in (None, "") or (hasattr(v, "isNull") and v.isNull()) else v

        for i, f in enumerate(dams.getFeatures()):
            if feedback.isCanceled(): break
            feedback.setProgress(100 * i / total)
            dam_id = str(get(f, "dam_id") or f.id())
            out = QgsFeature(fields); out.setGeometry(f.geometry())
            vals = list(f.attributes())
            extra = {k: None for k, _ in BASE_FIELDS}
            scen_vals = {pf: {k: None for k, _ in SCEN_FIELDS} for pf in prefixes}
            notes = []
            try:
                nf, nf_why = cm.no_flood(f, names)
                ex, ex_why = cm.excluded(f, names)
                if ex:                        # 0.13: the Review tab excluded this dam
                    notes.append(ex_why)
                    extra["brch_conf"] = "excluded"
                    skipped += 1
                    raise _NoFlood()
                if nf:                        # tool 16: a pond / dugout with nothing above the ground
                    notes.append("no breach flood: " + nf_why)
                    extra["brch_conf"] = "none"
                    skipped += 1
                    raise _NoFlood()
                cpath = os.path.join(curves, f"curve_{dam_id}.csv")
                if not os.path.exists(cpath):
                    raise FileNotFoundError("no stage-storage curve (tool 3 failed for this dam)")
                ce, cv = self._read_curve(cpath)
                pool, crest = float(get(f, "est_pool_m")), float(get(f, "est_crest_m"))
                h = get(f, "height_m"); h = float(h) if h is not None else None
                maxdep = get(f, "est_maxdep_m"); maxdep = float(maxdep) if maxdep is not None else 3.0
                scale = 1.0          # tool 3 already calibrated the curve to inventory storage
                if h:
                    base = crest - h
                else:
                    base = pool - maxdep
                    notes.append("no dam height: breach depth taken from estimated max depth")
                bed = pool - maxdep

                b = bmap.get(dam_id)
                crest_len = float(b["crest_len_m"]) if b is not None and b["crest_len_m"] not in (None, "") else None
                if crest_len is None and get(f, "crest_len_m"):
                    crest_len = float(get(f, "crest_len_m"))      # inventory dam length
                if b is not None:
                    extra["flow_az"] = b["flow_az"]; extra["dir_conf"] = b["dir_conf"]

                user = {}
                for key, (fname, fac) in user_fields.items():
                    uv = get(f, fname)
                    try:
                        uv = float(uv) if uv is not None else None
                    except (TypeError, ValueError):
                        uv = None
                    if uv is not None and (uv > 0 or key == "invert_m"):
                        user[key] = (uv * fac, "fields")
                for key, uv in user_vals.items():
                    user.setdefault(key, uv)
                res = bh.analyze_dam(ce, cv, pool, crest, base, mode, crest_len, scale,
                                     bed, exp, sunny, method=method, user=user or None)
                notes += res["notes"]
                base = res["invert_m"]
                for pf in prefixes:
                    r = res[pf]
                    if "skipped" in r:
                        notes.append(f"{pf}: {r['skipped']}"); continue
                    csvp = os.path.join(outdir, f"hydro_{dam_id}_{pf}.csv")
                    self._write_csv(csvp, r["t"], r["q"])
                    scen_vals[pf] = {"start_m": r["start_elev"], "vw_m3": r["vw_m3"],
                        "hb_m": r["hb_m"], "bavg_m": r["bavg_m"], "bb_m": r["bb_m"], "z": r["z"],
                        "tf_min": r["tf_s"] / 60.0, "qp_m3s": r["qp_route"],
                        "tp_min": r["tp_s"] / 60.0, "qp_f95": r["qp_f95"],
                        "qp_tr60": r["qp_tr60"], "rel_m3": r["released_m3"], "csv": csvp}
                    notes += [f"{pf}: {n}" for n in r["notes"]]
                td = res["td"]
                vw = td.get("vw_m3")
                # 0.28.0: the mode as a field of its own. It only ever appeared inside the
                # source text, and ND Dam Breach Runner needs it (with brch_eq) to start a
                # detailed run from the same breach the screening used (core/damrecord_io).
                extra.update({"base_m": base, "brch_eq": method, "brch_mode": mode,
                              "brch_src": td.get("src_text") or f"equation {method}",
                              "store_af": (vw / AF_M3) if vw else None,
                              "simplified": ("YES" if vw and vw / AF_M3 < 200 else "no") if vw else None})
                conf = "medium"
                if str(get(f, "stor_conf")) in ("low", "none") or not h:
                    conf = "low"
                if str(get(f, "stor_method")) == "inventory" and h:
                    conf = "medium"
                extra["brch_conf"] = conf
                done += 1
            except _NoFlood:
                pass
            except Exception as e:  # keep the batch going
                fail += 1
                notes.append("FAILED: " + er.plain(e, dam_id, "5"))
                extra["brch_conf"] = "none"
                feedback.reportError(f"dam {dam_id}: {e}")

            extra["brch_notes"] = "; ".join(notes)
            carried = " ".join(str(get(f, k) or "") for k in ("stor_notes", "status", "source"))
            if PROPOSED_PHRASE in carried and PROPOSED_PHRASE not in extra["brch_notes"]:
                extra["brch_notes"] = PROPOSED_PHRASE + ("; " + extra["brch_notes"] if extra["brch_notes"] else "")
            vals += [extra[k] for k, _ in BASE_FIELDS]
            for pf in prefixes:
                vals += [scen_vals[pf][k] for k, _ in SCEN_FIELDS]
            out.setAttributes(vals)
            sink.addFeature(out)

        feedback.pushInfo(f"breach hydrographs: {done} ok, {fail} failed" +
                          (f", {skipped} with no breach flood (tool 16: nothing above the natural ground)"
                           if skipped else ""))
        return {self.OUT: dest}
