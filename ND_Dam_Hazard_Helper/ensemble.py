# -*- coding: utf-8 -*-
"""v2.10: hazard class probability from an ensemble of breach runs.

An ensemble is a folder of run folders, each with the rasters of one breach run: the
ND Dam Breach Runner's probabilistic study writes <dam>/Probabilistic/Runs/run_NNN/ with
"Depth (Max).tif" (ft), "Velocity (Max).tif" (ft/s), "Arrival Time.tif" (h after breach
start) and run.json (format "ndbreach/probabilistic_run": the sample, "weight", "status",
"baseline"); any folder of sub-folders whose rasters autodetect.find_rasters recognises
works too.

  which runs  "Runs" wins over the legacy "runs" when both exist. When Runs/index.json
             exists (the runner writes it when the study has finished) ONLY the run folders
             it lists are used; without it every run folder there now is used, with the
             runner's provisional weights, and the summary says "study not finished (no
             index.json)".
  baseline   run.json "baseline" {"depth", "velocity", "arrival"} (paths relative to the run
             folder) is the run's no-breach baseline, so the incremental (with minus without
             breach) classification applies per run; else Baseline rasters in the run
             folder; else the dam's own baseline scenario.
  no breach  a run with status "no breach" (the dam does not fail; all-NaN rasters) is NOT
             classified. It keeps its weight and is its own line "No breach" of the
             distribution: every probability is over ALL runs, so P(High) + P(Medium) +
             P(Low) + P(no breach) = 1 and nothing is silently conditioned on a failure;
             "given a breach" (P(class | the dam fails)) is shown beside it. The no-breach
             case alone is reported separately: the no-breach (baseline) flood classified on
             its own when a baseline exists, else "no breach - not classified".

run_ensemble() classifies EVERY run with this plugin's pipeline and the dam's own settings,
then counts how often each class comes out:

  settings   the dam's last run as recorded in <dam>/outputs/QA/Run_Settings.json (every
             setting, the layers it read, the data files it read), or a cfg handed in by
             the caller (base_cfg - e.g. the dialog's current settings); a CONFIG_JSON file
             (as ndhazard:classify) overrides settings. Without either: plugin defaults.
  data       downloads are NOT repeated per run: the data files the dam's own run read
             (Run_Settings.json "inputs": the NSI / address / FEMA / road / rail / NID /
             lifeline ... files in outputs/_downloads or the data folder) are handed to
             every run as its data folder (cfg["local_data"], as a reproduction does), and
             the dam's FEMA_Structures file is the outline layer. Downloads stay OFF unless
             allowed; when allowed (or when there is no record) they go through ONE shared
             tile cache (QA/Ensemble/_tile_cache) and the downloads of the first run are
             copied to the next ones (the runner's runs share the base run's grid, so the
             study areas - and the download file names - are the same).
  per run    outputs in <dam>/outputs/QA/Ensemble/<run id>/ (never in the dam folder or the
             runner's folder); PDF report, EAP, map book, zone rasters, comparison with the
             last run, sensitivity and other extras are off (quick_rerun on); the dam's
             engineer review decisions apply. The run's result is kept in
             Ensemble_Run_Result.json with a key of its rasters and settings: running the
             ensemble again (after a cancel, or with more runs) classifies only what is new.
  reference  the dam's OWN rasters are classified once the same way (QA/Ensemble/
             _reference) and compared with the class of the dam's last run: when they
             differ, the ensemble's settings / data do not reproduce the dam's own
             classification and the report says so first.
  gate       optional model gate (modelgate.py) per run; its status is in the run table.

Outputs in <dam>/outputs/QA: Class_Probability.csv (the class distribution - ND REG_05.2026
High / Medium / Low and ACER TM 11 High / Significant / Low with counts, weights,
probabilities and the run IDs of each class - and, per structure, the probability of the
probable loss of life zone), Class_Probability_Runs.csv (one row per run) and
Class_Probability.html. Weighted when EVERY classified run has a positive weight in its
run.json; otherwise every run counts once (and the report says why).

THE PROBABILITIES ARE ONLY AS GOOD AS THE ENSEMBLE'S PARAMETER DISTRIBUTIONS. They are the
share of runs (or of the weight) giving each class - nothing more. The ND Dam Breach
Runner's default distributions are adjustable placeholders, not published values. Nothing
here has been run in a real QGIS.
"""

import csv
import hashlib
import html
import json
import math
import os
import re
import shutil
import time

from .constants import (OUTPUT_FOLDER_NAME, SUBDIR_QA, PLUGIN_NAME, PLUGIN_VERSION,
                        DOWNLOAD_FOLDER_NAME)

FOLDER = "Ensemble"
CSV_FILE = "Class_Probability.csv"
RUNS_CSV = "Class_Probability_Runs.csv"
HTML_FILE = "Class_Probability.html"
RESULT_FILE = "Ensemble_Run_Result.json"
RESULT_VERSION = 1
RUN_JSON = "run.json"
REFERENCE_ID = "_reference"
TILE_CACHE = "_tile_cache"
DEFAULT_MAX_RUNS = 200
MAX_RUNS_LIMIT = 1000
ND_CLASSES = ("High", "Medium", "Low")
TM11_CLASSES = ("High", "Significant", "Low")
OUT_PROBABLE = "Probable loss of life"
OUT_JUDGMENT = "Judgment zone"
MATCH_FT = 30.0                               # same structure in two runs (history.MATCH_FT)
# the runner's file names (nd_breach_runner core/workflow.FINAL_NAMES / BASELINE_NAMES)
RUNNER_NAMES = {"depth": "Depth (Max).tif", "vel": "Velocity (Max).tif",
                "arr": "Arrival Time.tif"}
CAVEAT = ("These probabilities are only as good as the ensemble's parameter distributions: "
          "each is the share of the runs (or of their weights) that gave that class, "
          "nothing more. The ND Dam Breach Runner's default distributions are adjustable "
          "placeholders, not published values - with them the numbers describe the "
          "placeholders, not the dam. The classification rules, the data and the engineer "
          "review decisions are the dam's own; the classes are this plugin's screening "
          "results until the engineer adopts them.")
# what every ensemble run does differently from a normal run (speed, no side effects)
ENSEMBLE_OVERRIDES = {
    "report_pdf": False, "eap": False, "eap_list": False, "eap_mapbook": False,
    "eap_travel": False, "compare_last_run": False, "add_to_project": False,
    "quick_rerun": True, "reuse_evaluation": False, "save_evaluation": False,
    "zone_rasters": False, "sampled_cells": False, "spreadsheets": False,
    "input_overviews": False, "zone_overviews": False, "report_imagery": False,
    "sensitivity": False, "termination": False, "evacuation": False, "evac_routes": False,
    "web_export": False, "temporal_animation": False, "conseq_sensitivity": False,
    "parcel_hints": False, "pop_source": None, "hcom_files": "", "peak_raster": None,
    "settings_profile": None, "ensemble_member": True,
}
# a data-folder role and the download it replaces
ROLE_DOWNLOADS = {"nsi": "download_nsi", "address": "download_address",
                  "usa": "download_usa_structures", "rail": "download_rail",
                  "crossings": "download_crossings", "roads": "download_roads",
                  "bridges": "download_crossings_structures", "dams": "download_dams",
                  "lifelines": "download_lifelines", "camps": "download_camps",
                  "centerline": "download_flowlines"}
_PATH_SETTINGS = ("output_folder", "dam_folder", "review_file", "facility_review_file",
                  "damage_curves_file", "hcom_files", "peak_raster", "local_data",
                  "scenarios", "dam_name", "settings_profile")
RUN_FIELDS = ["Run", "Status", "ND_Class", "TM11_Class", "Lives_In_Jeopardy", "Weight",
              "Governing_Reason", "Judgment_Items", "Gate", "Parameters", "Baseline",
              "Reused", "Seconds", "Message", "Output_Folder"]
CSV_FIELDS = ["Section", "Item", "Runs", "Weight", "Probability", "Run_IDs", "X", "Y", "CRS",
              "Detail"]


def _num(v, default=None):
    try:
        f = float(v)
        return f if math.isfinite(f) else default
    except (TypeError, ValueError):
        return default


def _natural(name):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", name)]


# ============================================================== runs ==
RUNNER_RUN_FORMAT = "ndbreach/probabilistic_run"      # nd_breach_runner probabilistic.RUN_FORMAT
RUNNER_INDEX_FORMAT = "ndbreach/probabilistic_runs"   # ... RUNS_INDEX_FORMAT
RUNS_INDEX = "index.json"
STATUS_DONE = "done"
STATUS_NO_BREACH = "no breach"
UNFINISHED = "study not finished (no index.json)"


def _child(folder, name):
    """The sub-folder `name` of `folder`: the exact spelling first, then any case."""
    if not folder or not os.path.isdir(folder):
        return None
    exact = os.path.join(folder, name)
    if os.path.isdir(exact) and name in os.listdir(folder):
        return exact
    hit = sorted(d for d in os.listdir(folder) if d.lower() == name.lower()
                 and os.path.isdir(os.path.join(folder, d)))
    return os.path.join(folder, hit[0]) if hit else None


def resolve_folder(path):
    """The folder holding the run folders: `path` itself, or its Probabilistic/Runs when
    `path` is a dam folder or a Probabilistic folder. "Runs" (the ND Dam Breach Runner from
    3.2.0) wins over the legacy "runs" when both exist."""
    cur = path
    for name in ("Probabilistic", "Runs"):
        hit = _child(cur, name)
        if hit:
            cur = hit
    return cur


def _runner_rasters(folder):
    out = {}
    for key, name in RUNNER_NAMES.items():
        p = os.path.join(folder, name)
        out[key] = p if os.path.isfile(p) else None
        b = os.path.join(folder, "Baseline " + name)
        out["base_" + key] = b if os.path.isfile(b) else None
    out["dv"] = None
    return out


def _runs_table(folder):
    """{run number: row} of Probabilistic_Runs.csv next to (or above) the run folders."""
    for f in (folder, os.path.dirname(folder)):
        p = os.path.join(f, "Probabilistic_Runs.csv")
        if os.path.isfile(p):
            try:
                with open(p, newline="", encoding="utf-8-sig") as fh:
                    return {str(int(float(r.get("run")))): r for r in csv.DictReader(fh)
                            if _num(r.get("run")) is not None}
            except (OSError, ValueError):
                return {}
    return {}


_PARAM_SKIP = ("weight", "run", "status", "resumed", "u", "key", "seed", "n_runs")


def _read_json(path):
    try:
        with open(path, encoding="utf-8-sig") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as e:
        return None, str(e)
    return (data, "") if isinstance(data, dict) else (None, "not a JSON object")


def read_run_meta(folder):
    """<run>/run.json as a dict ({} when there is none; {"_error": text} when unreadable)."""
    p = os.path.join(folder, RUN_JSON)
    if not os.path.isfile(p):
        return {}
    data, err = _read_json(p)
    return data if data is not None else {"_error": err}


def read_run_json(folder, meta=None):
    """(weight or None, params dict, note) of <run>/run.json. Parameters: the runner's
    "sample" and "parameters", else "params", else the scalar keys."""
    data = read_run_meta(folder) if meta is None else meta
    if not data:
        return None, {}, ""
    if data.get("_error"):
        return None, {}, "run.json not readable ({})".format(data["_error"])
    note = ""
    w = data.get("weight")
    weight = _num(w)
    if w is not None and (weight is None or weight <= 0):
        note = "weight {!r} is not a positive number - ignored".format(w)
        weight = None
    params = {}
    for key in ("params", "sample", "parameters"):
        if isinstance(data.get(key), dict):
            params.update(data[key])
    if not params:
        params = {k: v for k, v in data.items() if isinstance(v, (str, int, float, bool))}
    params = {k: v for k, v in params.items() if k not in _PARAM_SKIP and
              k not in ("format", "format_version", "tool", "created", "dam_id",
                        "weight_note", "baseline_note", "scenario", "arrival_depth_ft") and
              isinstance(v, (str, int, float, bool))}
    return weight, params, note


def _run_baseline(folder, meta):
    """({"base_depth", "base_vel", "base_arr"}, note) from run.json "baseline" (paths
    relative to the run folder); ({}, "") when it names none."""
    refs = meta.get("baseline") if isinstance(meta, dict) else None
    if not isinstance(refs, dict) or not refs:
        return {}, ""
    out, missing = {}, []
    for kind, key in (("depth", "base_depth"), ("velocity", "base_vel"),
                      ("arrival", "base_arr")):
        rel = refs.get(kind)
        if not rel:
            continue
        p = os.path.normpath(os.path.join(folder, str(rel)))
        if os.path.isfile(p):
            out[key] = p
        else:
            missing.append("{} ({})".format(kind, rel))
    if missing:
        note = "run.json baseline not found: {}".format(", ".join(missing))
        if not out.get("base_depth"):
            return {}, note + " - the dam's own baseline is used if it has one"
        return out, note
    return out, ""


def read_index(root):
    """(index dict or None, note) of <Runs>/index.json (the runner writes it when the study
    is finished)."""
    p = os.path.join(root, RUNS_INDEX)
    if not os.path.isfile(p):
        return None, ""
    data, err = _read_json(p)
    if data is None:
        return None, "index.json not readable ({}) - every run folder is used".format(err)
    if data.get("format") not in (None, RUNNER_INDEX_FORMAT) or \
            not isinstance(data.get("runs"), list):
        return None, "index.json is not an ND Dam Breach Runner run index - every run " \
                     "folder is used"
    return data, ""


def find_study(folder, max_runs=DEFAULT_MAX_RUNS):
    """The runs of an ensemble: {"root", "runs", "notes", "index" (path or None),
    "unfinished" (bool: a runner study without index.json)}.

    With Runs/index.json only the run folders it lists are used (in its order); without it
    every sub-folder with a depth raster (name order, run_2 before run_10). A run: {"id",
    "folder", "status" ("done" / "no breach"), "depth", "vel", "arr", "dv", "base_depth",
    "base_vel", "base_arr", "weight", "params", "note"}. A "no breach" run (all-NaN rasters)
    needs no raster. The baseline named in run.json (relative to the run folder) is the
    run's baseline; else Baseline rasters in the run folder."""
    from .autodetect import find_rasters
    notes = []
    root = resolve_folder(folder)
    if not root or not os.path.isdir(root):
        raise ValueError("Ensemble folder not found: {}".format(folder))
    index, inote = read_index(root)
    if inote:
        notes.append(inote)
    table = _runs_table(root)
    all_subs = sorted((d for d in os.listdir(root) if os.path.isdir(os.path.join(root, d))
                       and not d.startswith(("_", ".")) and d.lower() != OUTPUT_FOLDER_NAME),
                      key=_natural)
    if index is not None:
        listed = []
        for item in index["runs"]:
            name = item.get("folder") if isinstance(item, dict) else None
            if not name and isinstance(item, dict) and _num(item.get("run")) is not None:
                name = "run_{:03d}".format(int(item["run"]))
            if name and name not in listed:
                listed.append(str(name))
        gone = [d for d in listed if not os.path.isdir(os.path.join(root, d))]
        extra = [d for d in all_subs if d not in listed]
        subs = [d for d in listed if d not in gone]
        if gone:
            notes.append("{} run folder(s) listed in index.json are missing: {}".format(
                len(gone), ", ".join(gone[:8])))
        if extra:
            notes.append("{} folder(s) not listed in index.json left out (not part of the "
                         "finished study): {}".format(len(extra), ", ".join(extra[:8]) +
                                                      (" ..." if len(extra) > 8 else "")))
        notes.append("Runs: the {} run(s) listed in {}".format(len(listed), RUNS_INDEX))
    else:
        subs = all_subs
    runs, empty = [], []
    runner = False
    for d in subs:
        f = os.path.join(root, d)
        meta = read_run_meta(f)
        if meta.get("format") == RUNNER_RUN_FORMAT:
            runner = True
        status = str(meta.get("status") or STATUS_DONE).strip().lower()
        if status not in (STATUS_DONE, STATUS_NO_BREACH):
            status = STATUS_DONE
        r = _runner_rasters(f)
        if not r.get("depth") and status != STATUS_NO_BREACH:
            try:
                found = find_rasters(f)
            except Exception:               # noqa: BLE001
                found = {}
            r = {k: found.get(k) for k in ("depth", "vel", "arr", "dv", "base_depth",
                                           "base_vel", "base_arr")}
        if not r.get("depth") and status != STATUS_NO_BREACH:
            empty.append(d)
            continue
        weight, params, note = read_run_json(f, meta)
        base, bnote = _run_baseline(f, meta)
        if base:
            for k in ("base_depth", "base_vel", "base_arr"):
                r[k] = base.get(k)
        if bnote:
            note = (note + "; " if note else "") + bnote
        if not params:
            m = re.search(r"(\d+)$", d)
            row = table.get(str(int(m.group(1)))) if m else None
            if row:
                params = {k: row[k] for k in ("width_factor", "time_factor", "n_factor",
                                              "method") if row.get(k) not in (None, "")}
                note = (note + "; " if note else "") + "parameters from Probabilistic_Runs.csv"
        runs.append(dict(r, id=d, folder=f, status=status, weight=weight, params=params,
                         note=note, baseline_from_run=bool(base)))
    if empty:
        notes.append("{} sub-folder(s) without a depth raster left out: {}".format(
            len(empty), ", ".join(empty[:8]) + (" ..." if len(empty) > 8 else "")))
    unfinished = index is None and (runner or os.path.basename(
        os.path.dirname(os.path.abspath(root))).lower() == "probabilistic")
    if unfinished:
        notes.append("{}: the runner writes Runs/index.json when the study has finished; "
                     "these are the run folders there now, with provisional weights".format(
                         UNFINISHED[0].upper() + UNFINISHED[1:]))
    if max_runs and len(runs) > int(max_runs):
        notes.append("{} runs found; the first {} ({} order) are used (MAX_RUNS)".format(
            len(runs), int(max_runs), "index" if index is not None else "name"))
        runs = runs[:int(max_runs)]
    return {"root": root, "runs": runs, "notes": notes,
            "index": os.path.join(root, RUNS_INDEX) if index is not None else None,
            "unfinished": unfinished}


def find_runs(folder, max_runs=DEFAULT_MAX_RUNS):
    """(runs, notes) of find_study."""
    st = find_study(folder, max_runs)
    return st["runs"], st["notes"]


# ========================================================== settings ==
def _qa(dam_folder):
    return os.path.join(dam_folder, OUTPUT_FOLDER_NAME, SUBDIR_QA)


def _reopen(rec):
    """A layer of a Run_Settings.json layer record, or None (not a file / gone)."""
    from . import reproduce as REP
    if not isinstance(rec, dict) or not rec.get("path") or not os.path.exists(rec["path"]):
        return None
    try:
        return REP._make_layer(rec.get("kind") or "vector", rec.get("source") or rec["path"],
                               rec.get("name"), rec.get("provider"))
    except Exception:                       # noqa: BLE001
        return None


def dam_settings(dam_folder, base_cfg=None, config_json=None, downloads=False):
    """(kw, info): the pipeline settings every ensemble run starts from, and
    {"record", "notes", "baseline" (dict or None), "dam_scenario" (dict or None),
    "local" (bool: the dam's data files are used), "downloads" (bool)}."""
    from . import pipeline as PL
    from . import reproduce as REP
    notes = []
    info = {"record": None, "notes": notes, "baseline": None, "dam_scenario": None,
            "local": False, "downloads": bool(downloads), "created": ""}
    rec = None
    rpath = os.path.join(_qa(dam_folder), REP.RUN_SETTINGS_FILE)
    if os.path.isfile(rpath):
        try:
            rec = REP.read_run_settings(rpath)
            info["record"] = rpath
            info["created"] = str(rec.get("created") or "")
        except ValueError as e:
            notes.append("QA/Run_Settings.json not used: {}".format(e))
    kw = {}
    if base_cfg is not None:
        kw = {k: v for k, v in dict(base_cfg).items() if k not in _PATH_SETTINGS}
        kw["local_data"] = dict((base_cfg or {}).get("local_data") or {})
        notes.append("Settings: handed in by the caller (the current settings)")
    elif rec is not None:
        for k, v in (rec.get("settings") or {}).items():
            if k in PL.DEFAULTS and k not in _PATH_SETTINGS:
                kw[k] = v
        for k, lrec in (rec.get("layers") or {}).items():
            if k not in PL.DEFAULTS:
                continue
            lyr = _reopen(lrec)
            kw[k] = lyr
            if lyr is None:
                notes.append("Layer {} ('{}') of the dam's run is not a file that still "
                             "exists - left out of the ensemble runs".format(
                                 k, lrec.get("name") if isinstance(lrec, dict) else ""))
        ps = rec.get("point_sources")
        if isinstance(ps, list):
            kw["point_sources"] = []
            for item in ps:
                lyr = _reopen((item or {}).get("layer"))
                if lyr is None:
                    notes.append("Point layer {} of the dam's run is not available - left "
                                 "out".format((item or {}).get("family")))
                else:
                    kw["point_sources"].append(((item or {}).get("family"), lyr))
        notes.append("Settings: the dam's last run (QA/Run_Settings.json of {})".format(
            rec.get("created") or "?"))
    else:
        notes.append("Settings: plugin defaults - the dam has no QA/Run_Settings.json (no run "
                     "with plugin 2.9 or later); classify the dam first for its own settings")
    if rec is not None:
        scen = rec.get("scenarios") or []
        br = next((s for s in scen if (s.get("role") or "breach") == "breach"), None)
        info["dam_scenario"] = br
        if br and br.get("baseline"):
            info["baseline"] = next((s for s in scen if s.get("name") == br.get("baseline")),
                                    None)
        # the data files the dam's run read, as a data folder for every run
        local = dict(kw.get("local_data") or {})
        multi = []
        for role, spec in (rec.get("inputs") or {}).items():
            if role in local:
                continue
            items = spec if isinstance(spec, list) else [spec]
            keep = []
            for it in items:
                pth = it[0] if isinstance(it, list) else it
                if pth and os.path.exists(REP.split_source(pth)[0]):
                    keep.append(it)
            if not keep:
                notes.append("Data '{}' the dam's run read is gone - not used".format(role))
                continue
            if role == "lifelines":
                local[role] = [it if isinstance(it, list) else [it, None] for it in keep]
            else:
                one = keep[0][0] if isinstance(keep[0], list) else keep[0]
                local[role] = one
                if len(keep) > 1:
                    multi.append("{} ({} files, the first used)".format(role, len(keep)))
        if multi:
            notes.append("Data read from several files by the dam's run and handed over as "
                         "one: {} - e.g. road traffic counts can differ from the dam's own "
                         "run (see the reference run)".format(", ".join(multi)))
        kw["local_data"] = local
        info["local"] = bool(local)
        if local:
            notes.append("Data: the {} data set(s) the dam's run read (run of {}) - not "
                         "downloaded again: {}".format(len(local), info["created"] or "?",
                                                      ", ".join(sorted(local))))
    # the dam's building-outline file
    osm = kw.get("osm_layer", "auto")
    if isinstance(osm, str) and osm == "auto":
        try:
            from .osm_processing import get_osm_candidates
            lyr = get_osm_candidates(os.path.join(dam_folder, OUTPUT_FOLDER_NAME), dam_folder)
        except Exception:                   # noqa: BLE001
            lyr = None
        kw["osm_layer"] = lyr
        if lyr is not None:
            src = lyr.source().split("|")[0]
            notes.append("Building outlines: {}".format(os.path.basename(src) or lyr.name()))
            loc = kw.get("local_data") or {}
            if isinstance(loc.get("usa"), str) and os.path.normcase(os.path.abspath(
                    REP.split_source(loc["usa"])[0])) == os.path.normcase(os.path.abspath(src)):
                loc.pop("usa")                # the same file: not twice
    if config_json:
        from .processing_provider import _read_worker_file
        extra, ignored, _res = _read_worker_file(config_json)
        kw.update(extra)
        notes.append("Settings file {}: {} setting(s){}".format(
            os.path.basename(config_json), len(extra),
            "; ignored: " + ", ".join(ignored) if ignored else ""))
    # downloads
    local = kw.get("local_data") or {}
    if not downloads:
        for k in [k for k in PL.DEFAULTS if k.startswith("download_")]:
            kw[k] = False
        if not local and rec is None:
            notes.append("Downloads are OFF and the dam has no record of its data: only "
                         "project layers and the FEMA_Structures file are used - allow "
                         "downloads, or classify the dam first")
        else:
            notes.append("Downloads: off (every run reads the same data)")
    else:
        for role, key in ROLE_DOWNLOADS.items():
            if role in local:
                kw[key] = False
        notes.append("Downloads: allowed for what the dam's data files do not cover - one "
                     "shared tile cache, the first run's downloads copied to the others")
    kw.update(ENSEMBLE_OVERRIDES)
    return kw, info


def _review_files(dam_folder, kw):
    """The dam's engineer review decisions (structures, roads / facilities) for every run."""
    from . import review as RV
    from . import facility_review as FR
    out = os.path.join(dam_folder, OUTPUT_FOLDER_NAME)
    got = {}
    for key, fn in (("review_file", RV.review_path), ("facility_review_file", FR.review_path)):
        p = fn(out)
        if os.path.isfile(p):
            got[key] = p
    return got


def run_config(base_kw, run, out_folder, dam_name, baseline=None, reviews=None):
    """Pipeline config keywords of one run (Scenario objects; the run's own baseline
    rasters, else the dam's baseline scenario)."""
    from .scenarios import Scenario, ROLE_BASELINE
    kw = dict(base_kw)
    kw["local_data"] = dict(base_kw.get("local_data") or {})
    kw["point_sources"] = list(base_kw["point_sources"]) \
        if isinstance(base_kw.get("point_sources"), list) else base_kw.get("point_sources",
                                                                            "auto")
    base_name = None
    scen = []
    if run.get("base_depth"):
        base_name = "No breach"
        scen.append(Scenario(base_name, run["base_depth"], run.get("base_vel"),
                             run.get("base_arr"), role=ROLE_BASELINE))
        base_note = "the run's own"
    elif baseline and baseline.get("depth") and os.path.isfile(baseline["depth"]):
        base_name = baseline.get("name") or "No breach"
        scen.append(Scenario(base_name, baseline["depth"], baseline.get("velocity"),
                             baseline.get("arrival"), dv=baseline.get("dv"),
                             role=ROLE_BASELINE))
        base_note = "the dam's ({})".format(os.path.basename(baseline["depth"]))
    else:
        base_note = ""
    scen.insert(0, Scenario("Breach", run["depth"], run.get("vel"), run.get("arr"),
                            dv=run.get("dv"), baseline=base_name))
    kw.update({"scenarios": scen, "output_folder": out_folder, "dam_folder": out_folder,
               "dam_name": "{} - {}".format(dam_name, run["id"])})
    if not run.get("vel") and not kw.get("one_d"):
        kw["one_d"] = True                      # as the batch: no velocity -> 1D criterion
    kw.update(reviews or {})
    return kw, base_note


# =========================================================== one run ==
def run_key(kw):
    """Key of a run's inputs: its rasters (path, size, time, content digest), the settings
    and the data, the plugin version."""
    from . import pipeline as PL
    from .processing_provider import worker_settings
    parts = {"plugin": PLUGIN_VERSION, "v": RESULT_VERSION,
             "scenarios": [[s.name, s.role, s.baseline] + [
                 PL._file_sig(p) for p in (s.depth, s.velocity, s.arrival, s.dv) if p]
                 for s in kw.get("scenarios") or []],
             "settings": sorted((k, repr(v)) for k, v in worker_settings(kw).items()
                                if k not in ("dam_name",)),
             "local": PL._local_data_sig(kw.get("local_data")),
             "layers": [[k, PL._layer_sig(kw.get(k))] for k in sorted(kw)
                        if k.endswith("_layer") and kw.get(k) is not None],
             "files": [[k, PL._file_sig(kw[k])] for k in ("review_file",
                                                        "facility_review_file")
                       if kw.get(k) and os.path.isfile(kw[k])]}
    try:
        blob = json.dumps(parts, sort_keys=True, default=str)
    except (TypeError, ValueError):
        blob = repr(parts)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _rec_point(rec):
    from . import history as HIS
    return HIS._point(rec)


def extract_result(res):
    """JSON-ready result of a pipeline run: classes, lives in jeopardy, and the wet
    structures (not 'not habitable') of the governing scenario as classified (with a
    baseline: the incremental view)."""
    from .constants import ZONE_LABELS
    nd, tm = res.get("nd") or {}, res.get("tm11") or {}
    gov = (res.get("results") or {}).get(res.get("governing")) or {}
    views = gov.get("views")
    structs = views[0] if views else (res.get("structs") or [])
    crs = res.get("crs")
    out = []
    for s in structs:
        if s.get("habitable") == "No" or s.get("outcome") in (None, "Not inundated"):
            continue
        pt = _rec_point(s)
        if pt is None:
            continue
        out.append({"x": round(pt[0], 3), "y": round(pt[1], 3), "id": s.get("id"),
                    "outcome": s.get("outcome") or "", "habitable": s.get("habitable") or "",
                    "zone": ZONE_LABELS.get(s.get("zone"), "") if s.get("zone") else "",
                    "persons": _num(s.get("persons"), 0.0),
                    "depth": None if s.get("max_depth") is None else round(float(
                        s["max_depth"]), 2),
                    "category": s.get("fema_category") or s.get("category") or ""})
    try:
        lij = float(tm.get("lij") or 0.0)
    except (TypeError, ValueError):
        lij = 0.0
    return {"nd": nd.get("cls") or "", "tm11": tm.get("cls") or "", "lij": round(lij, 2),
            "reason": (list(nd.get("reasons") or []) or [""])[0][:250],
            "judgment_items": len(nd.get("judgment") or []),
            "crs": (crs.authid() or crs.toWkt()) if crs is not None else "",
            "structures": out}


def _result_path(out):
    return os.path.join(out, RESULT_FILE)


def load_result(out, key):
    try:
        with open(_result_path(out), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("version") != RESULT_VERSION or \
            data.get("key") != key:
        return None
    return data


def save_result(out, key, data):
    data = dict(data, version=RESULT_VERSION, key=key,
                time=time.strftime("%Y-%m-%d %H:%M:%S"))
    os.makedirs(out, exist_ok=True)
    tmp = _result_path(out) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1)
    os.replace(tmp, _result_path(out))
    return data


def _seed_downloads(src_outs, dst_out):
    """Copy the downloaded data files of earlier runs (and of the dam's own run) into a
    run's outputs/_downloads: a file of the same study area and day is read, not fetched."""
    dst = os.path.join(dst_out, DOWNLOAD_FOLDER_NAME)
    today = time.strftime("%Y%m%d")
    n = 0
    for src_out in src_outs:
        src = os.path.join(src_out, DOWNLOAD_FOLDER_NAME)
        if not os.path.isdir(src):
            continue
        for f in os.listdir(src):
            if today not in f or not f.lower().endswith(".geojson"):
                continue
            target = os.path.join(dst, f)
            if os.path.exists(target):
                continue
            try:
                os.makedirs(dst, exist_ok=True)
                shutil.copy2(os.path.join(src, f), target)
                n += 1
            except OSError:
                continue
    return n


def classify_run(run, kw, resume=True, gate=False, dam_folder=None, progress=None,
                 log=None):
    """Classify one run (or re-use its saved result). Returns the saved result dict with
    "status" ("done" / "failed"), "reused", "seconds", "message", "gate"."""
    from . import pipeline as PL
    from .helpers import CancelledByUser
    out = kw["output_folder"]
    key = run_key(kw)
    if resume:
        old = load_result(out, key)
        if old is not None and old.get("status") == "done":
            old["reused"] = True
            return old
    t0 = time.monotonic()
    data = {"status": "failed", "reused": False, "message": "", "gate": ""}
    try:
        os.makedirs(out, exist_ok=True)
        if gate:
            from . import modelgate as MG
            try:
                g = MG.gate({"depth": run["depth"], "vel": run.get("vel"),
                             "arr": run.get("arr"), "dv": run.get("dv"),
                             "base_depth": run.get("base_depth"), "dam_folder": dam_folder,
                             "output_folder": out}, write=True)
                data["gate"] = g["status"]
            except CancelledByUser:
                raise
            except Exception as e:          # noqa: BLE001 - the gate is an extra
                data["gate"] = "not run ({})".format(e)
        res = PL.run_classification(PL.config(**kw), progress=progress, log=log)
        data.update(extract_result(res))
        data["status"] = "done"
    except CancelledByUser:
        raise
    except Exception as e:                  # noqa: BLE001 - one run never stops the study
        data["message"] = str(e)[:300]
    finally:
        PL.forget_evaluation(out)           # the cache would keep every run's inventory
    data["seconds"] = round(time.monotonic() - t0, 1)
    saved = save_result(out, key, data)
    saved["reused"] = False
    return saved


# ======================================================= aggregation ==
def _weights(runs_ok):
    """(weights list, weighted?, note)."""
    ws = [_num(r.get("weight")) for r in runs_ok]
    if runs_ok and all(w is not None and w > 0 for w in ws):
        return ws, True, "weighted by the runs' weights (run.json)"
    have = sum(1 for w in ws if w is not None and w > 0)
    note = "unweighted (every run counts once)"
    if have:
        note += " - only {} of {} runs have a weight; weights are used only when every " \
                "run has one".format(have, len(runs_ok))
    return [1.0] * len(runs_ok), False, note


NO_BREACH_ITEM = "No breach"
NOT_CLASSIFIED = "no breach - not classified"


def aggregate(items, match_units=MATCH_FT, no_breach_class=None):
    """Class distribution of an ensemble. items: [{"id", "weight", "status", "nd", "tm11",
    "lij", "structures": [...], ...}] - the classified runs (status "done") and the runs in
    which the dam did not fail (status "no breach"). Pure data.

    Presentation: every probability is over ALL these runs (their weight). The breach runs
    give P(High) / P(Medium) / P(Low); the no-breach runs are their own line "No breach"
    (P = the share of runs without a failure), so the class probabilities add up to 1 -
    P(no breach) and are never silently conditioned on a breach. Each class also carries
    "p_breach" = P(class | the dam fails). The class of the no-breach case (the no-breach
    flood classified on its own, when the dam has one: `no_breach_class` {"nd", "tm11",
    "basis"}) is reported beside that line, not added to a class.

    Returns {"n", "n_breach", "weighted", "weight_note", "total_weight", "breach_weight",
    "nd": {cls: {"runs", "weight", "p", "p_breach", "ids"}}, "tm11": ..., "no_breach":
    {"runs", "weight", "p", "ids", "nd", "tm11", "basis"}, "structures": [...],
    "lij": {"min", "mean", "max"}} (lives in jeopardy over the breach runs)."""
    ws, weighted, note = _weights(items)
    tot = sum(ws) or 1.0
    nb = [(it, w) for it, w in zip(items, ws) if it.get("status") == STATUS_NO_BREACH]
    br = [(it, w) for it, w in zip(items, ws) if it.get("status") != STATUS_NO_BREACH]
    btot = sum(w for _it, w in br)

    def dist(key, classes):
        out = {c: {"runs": 0, "weight": 0.0, "p": 0.0, "p_breach": 0.0, "ids": []}
               for c in classes}
        for it, w in br:
            c = it.get(key) or "?"
            d = out.setdefault(c, {"runs": 0, "weight": 0.0, "p": 0.0, "p_breach": 0.0,
                                   "ids": []})
            d["runs"] += 1
            d["weight"] += w
            d["ids"].append(it["id"])
        for d in out.values():
            d["p"] = d["weight"] / tot
            d["p_breach"] = d["weight"] / btot if btot else 0.0
        return out
    nbc = dict(no_breach_class or {})
    no_breach = {"runs": len(nb), "weight": sum(w for _it, w in nb),
                 "p": sum(w for _it, w in nb) / tot, "ids": [it["id"] for it, _w in nb],
                 "nd": nbc.get("nd") or NOT_CLASSIFIED, "tm11": nbc.get("tm11") or
                 NOT_CLASSIFIED, "basis": nbc.get("basis") or ""}
    from . import history as HIS
    clusters = []
    for it, w in br:
        structs = [dict(s) for s in it.get("structures") or []]
        pairs, _gone, new = HIS._match(clusters, structs, match_units)
        for c, s in pairs:
            c["_hits"].append((it["id"], w, s))
        for s in new:
            c = {"x": s["x"], "y": s["y"], "_hits": [(it["id"], w, s)]}
            clusters.append(c)
    srows = []
    for c in clusters:
        hits = c["_hits"]
        prob = [(i, w) for i, w, s in hits if s.get("outcome") == OUT_PROBABLE]
        judg = [(i, w) for i, w, s in hits if s.get("outcome") == OUT_JUDGMENT]
        if not prob and not judg:
            continue
        first = hits[0][2]
        depths = [s.get("depth") for _i, _w, s in hits if s.get("depth") is not None]
        srows.append({"x": c["x"], "y": c["y"], "id": first.get("id"),
                      "category": first.get("category") or "",
                      "habitable": first.get("habitable") or "",
                      "p_probable": sum(w for _i, w in prob) / tot,
                      "p_judgment": sum(w for _i, w in judg) / tot,
                      "p_wet": sum(w for _i, w, _s in hits) / tot,
                      "runs_probable": [i for i, _w in prob], "runs_judgment": [i for i, _w
                                                                                in judg],
                      "max_depth": max(depths) if depths else None,
                      "persons": max((_num(s.get("persons"), 0.0) for _i, _w, s in hits),
                                     default=0.0)})
    srows.sort(key=lambda r: (-r["p_probable"], -r["p_judgment"], r["x"], r["y"]))
    lij = [(float(it.get("lij") or 0.0), w) for it, w in br]
    return {"n": len(items), "n_breach": len(br), "weighted": weighted, "weight_note": note,
            "total_weight": tot, "breach_weight": btot,
            "nd": dist("nd", ND_CLASSES), "tm11": dist("tm11", TM11_CLASSES),
            "no_breach": no_breach, "structures": srows,
            "lij": {"min": min(v for v, _w in lij) if lij else 0.0,
                    "max": max(v for v, _w in lij) if lij else 0.0,
                    "mean": (sum(w * v for v, w in lij) / btot) if lij and btot else 0.0}}


def _dist_text(d, classes, no_breach=None):
    parts = []
    for c in list(classes) + [k for k in d if k not in classes]:
        v = d.get(c)
        if v is None or (c not in classes and not v["runs"]):
            continue
        parts.append("{} {:.2f} ({})".format(c, v["p"], v["runs"]))
    if no_breach and no_breach["runs"]:
        parts.append("no breach {:.2f} ({})".format(no_breach["p"], no_breach["runs"]))
    return ", ".join(parts)


def summary_line(agg, n_failed=0, reference=None, unfinished=False):
    tail = "; " + UNFINISHED if unfinished else ""
    if not agg or not agg["n"]:
        return "Class probability: no run was classified{}{}.".format(
            " ({} failed)".format(n_failed) if n_failed else "", tail)
    nb = agg.get("no_breach") or {"runs": 0}
    text = ("Class probability over {} run(s), {}: ND {}; TM 11 {}".format(
        agg["n"], "weighted" if agg["weighted"] else "unweighted",
        _dist_text(agg["nd"], ND_CLASSES, nb), _dist_text(agg["tm11"], TM11_CLASSES, nb)))
    if nb["runs"]:
        text += ("; the dam did not fail in {} of {} run(s) (P {:.2f}) - the class "
                 "probabilities are over all runs; the no-breach case alone: ND {} / TM 11 "
                 "{}".format(nb["runs"], agg["n"], nb["p"], nb["nd"], nb["tm11"]))
    if n_failed:
        text += "; {} failed run(s) left out".format(n_failed)
    if reference and reference.get("mismatch"):
        text += "; WARNING: the reference run does not reproduce the dam's own class"
    return text + tail + ". Only as good as the ensemble's parameter distributions."


# ============================================================ the study ==
def _reference(dam_folder, info, base_kw, ens_root, reviews, resume, progress, log):
    """Classify the dam's own rasters with the ensemble settings; compare with the dam's
    last-run class. None when there is nothing to compare."""
    from . import history as HIS
    from .autodetect import find_rasters
    sc = info.get("dam_scenario") or {}
    if sc.get("depth") and os.path.isfile(sc["depth"]):
        run = {"id": REFERENCE_ID, "depth": sc["depth"], "vel": sc.get("velocity"),
               "arr": sc.get("arrival"), "dv": sc.get("dv")}
    else:
        f = find_rasters(dam_folder)
        if not f.get("depth"):
            return None
        run = {"id": REFERENCE_ID, "depth": f["depth"], "vel": f.get("vel"),
               "arr": f.get("arr"), "dv": f.get("dv"), "base_depth": f.get("base_depth"),
               "base_vel": f.get("base_vel"), "base_arr": f.get("base_arr")}
    snap = HIS.read_snapshot(_qa(dam_folder))
    kw, _b = run_config(base_kw, run, os.path.join(ens_root, REFERENCE_ID),
                        _dam_name(dam_folder), info.get("baseline"), reviews)
    r = classify_run(run, kw, resume=resume, progress=progress, log=log)
    ref = {"status": r.get("status"), "nd": r.get("nd"), "tm11": r.get("tm11"),
           "lij": r.get("lij"), "dam_nd": (snap or {}).get("nd"),
           "dam_tm11": (snap or {}).get("tm11"), "dam_time": (snap or {}).get("time"),
           "message": r.get("message") or "", "mismatch": False}
    if r.get("status") == "done" and snap:
        ref["mismatch"] = (snap.get("nd") != r.get("nd") or snap.get("tm11") != r.get("tm11"))
    return ref


def _dam_name(dam_folder):
    from .helpers import dam_display_name
    return dam_display_name(dam_folder)


def run_ensemble(ensemble_folder, dam_folder, base_cfg=None, config_json=None,
                 max_runs=DEFAULT_MAX_RUNS, downloads=False, resume=True, gate=True,
                 reference=True, progress=None, log=None):
    """Classify every run of the ensemble and write the class probability. Returns
    {"agg", "runs", "summary", "csv", "runs_csv", "html", "folder", "notes", "reference"}.
    progress(pct, text) may raise CancelledByUser (Cancel): the runs finished so far are
    kept and re-used by the next call."""
    from .helpers import CancelledByUser, buffer_distance_in_layer_units
    if not dam_folder or not os.path.isdir(dam_folder):
        raise ValueError("Dam folder not found: {}".format(dam_folder))
    study = find_study(ensemble_folder, max_runs)
    runs, notes = study["runs"], study["notes"]
    if not runs:
        raise ValueError("No run folder with a depth raster in {}".format(ensemble_folder))
    base_kw, info = dam_settings(dam_folder, base_cfg, config_json, downloads)
    notes = info["notes"] + notes
    qa = _qa(dam_folder)
    ens_root = os.path.join(qa, FOLDER)
    os.makedirs(ens_root, exist_ok=True)
    if downloads or not info["local"]:
        base_kw["data_cache_folder"] = os.path.join(ens_root, TILE_CACHE)
        base_kw["data_max_age_days"] = max(_num(base_kw.get("data_max_age_days"), 7.0) or 7.0,
                                           30.0)
        base_kw["refresh_data"] = False
    reviews = _review_files(dam_folder, base_kw) if base_cfg is None or not (
        base_cfg.get("review_file")) else {"review_file": base_cfg.get("review_file"),
                                           "facility_review_file":
                                               base_cfg.get("facility_review_file")}
    if reviews.get("review_file"):
        notes.append("Engineer review decisions of the dam applied to every run: {}".format(
            os.path.basename(reviews["review_file"])))
    name = _dam_name(dam_folder)
    n = len(runs) + (1 if reference else 0)

    def sub(i, label):
        def cb(p, m=""):
            if progress:
                progress(100.0 * (i + max(0.0, min(100.0, float(p or 0.0))) / 100.0) / n,
                         "{} ({}/{}): {}".format(label, i + 1, n, m))
        return cb

    def say(text):
        if log:
            log(text)

    ref = None
    step = 0
    nb_class = None
    try:
        if reference:
            say("Ensemble: reference run - the dam's own rasters with the ensemble settings")
            ref = _reference(dam_folder, info, base_kw, ens_root, reviews, resume,
                             sub(0, "Reference run"), None)
            step = 1
            if ref and ref.get("mismatch"):
                say("WARNING: the reference run gives ND {} / TM 11 {}, the dam's last run ND {} "
                    "/ TM 11 {} - the ensemble's settings or data do not reproduce the dam's "
                    "classification".format(ref["nd"], ref["tm11"], ref["dam_nd"],
                                            ref["dam_tm11"]))
        results = []
        seeds = [os.path.join(dam_folder, OUTPUT_FOLDER_NAME)]
        osm_from_run = None
        for i, run in enumerate(runs):
            out = os.path.join(ens_root, run["id"])
            if run.get("status") == STATUS_NO_BREACH:
                if nb_class is None:
                    say("Ensemble: the no-breach case - the no-breach flood classified on its "
                        "own")
                    nb_class = _no_breach_class(runs, info, base_kw, ens_root, name, reviews,
                                                resume, sub(step + i, "No-breach case"))
                r = {"status": STATUS_NO_BREACH, "id": run["id"], "weight": run.get("weight"),
                     "params": run.get("params"), "baseline": "", "folder": "",
                     "note": run.get("note") or "", "nd": "", "tm11": "", "lij": 0.0,
                     "structures": [], "gate": "", "reused": False,
                     "message": "the dam does not fail in this run (runner status 'no "
                                "breach') - not classified, counted as 'No breach'; the "
                                "no-breach case alone: ND {} / TM 11 {}".format(
                                    nb_class.get("nd") or NOT_CLASSIFIED,
                                    nb_class.get("tm11") or NOT_CLASSIFIED)}
                results.append(r)
                say("  {}: no breach (counted as 'No breach')".format(run["id"]))
                continue
            kw, base_note = run_config(base_kw, run, out, name, info.get("baseline"), reviews)
            if downloads or not info["local"]:
                if kw.get("osm_layer") in (None, "auto") and osm_from_run is not None:
                    kw["osm_layer"] = osm_from_run
                _seed_downloads(seeds, out)
            say("Ensemble run {} ({}/{})".format(run["id"], i + 1, len(runs)))
            r = classify_run(run, kw, resume=resume, gate=gate, dam_folder=dam_folder,
                             progress=sub(step + i, "Run " + run["id"]), log=None)
            r = dict(r, id=run["id"], weight=run.get("weight"), params=run.get("params"),
                     baseline=base_note, folder=out, note=run.get("note") or "")
            results.append(r)
            if r["status"] == "done":
                if out not in seeds:
                    seeds.append(out)
                if (downloads or not info["local"]) and osm_from_run is None and \
                        base_kw.get("osm_layer") in (None, "auto"):
                    osm_from_run = _outline_file_of(out)
            say("  {}: {}".format(run["id"], "ND {} / TM 11 {}{}".format(
                r.get("nd"), r.get("tm11"), " (kept from an earlier study)" if r.get("reused")
                else "") if r["status"] == "done" else "FAILED - " + r.get("message", "")))
    except CancelledByUser:
        say("Cancelled - the runs finished so far are kept; run the ensemble again to "
            "continue")
        raise
    ok = [r for r in results if r["status"] == STATUS_DONE]
    nb_runs = [r for r in results if r["status"] == STATUS_NO_BREACH]
    failed = [r for r in results if r["status"] not in (STATUS_DONE, STATUS_NO_BREACH)]
    if nb_runs:
        notes.append("{} run(s) in which the dam does not fail (runner status 'no breach'): "
                     "not classified; they are the 'No breach' line of the probabilities, "
                     "which are over all runs. The no-breach case alone: {}".format(
                         len(nb_runs), (nb_class or {}).get("basis") or NOT_CLASSIFIED))
    if failed:
        notes.append("{} of {} runs failed and are left out of the probabilities ({}) - "
                     "leaving runs out can bias the result".format(
                         len(failed), len(results), ", ".join(r["id"] for r in failed[:10])))
    ok = _same_crs(ok, notes)
    units = MATCH_FT
    try:
        from qgis.core import QgsCoordinateReferenceSystem
        if ok and ok[0].get("crs"):
            units = buffer_distance_in_layer_units(MATCH_FT, QgsCoordinateReferenceSystem(
                ok[0]["crs"]))
    except Exception:                       # noqa: BLE001
        units = MATCH_FT
    agg = aggregate(ok + nb_runs, units, nb_class)
    text = summary_line(agg, len(failed), ref, study["unfinished"])
    paths = write_outputs(qa, dam_folder, agg, results, notes, ref, text, info)
    say(text)
    if progress:
        progress(100.0, "Class probability written")
    return {"agg": agg, "runs": results, "summary": text, "csv": paths["csv"],
            "runs_csv": paths["runs_csv"], "html": paths["html"], "folder": ens_root,
            "notes": notes, "reference": ref, "no_breach": nb_class,
            "unfinished": study["unfinished"], "index": study["index"]}


NO_BREACH_ID = "_no_breach"


def _no_breach_class(runs, info, base_kw, ens_root, name, reviews, resume, progress):
    """The class of the no-breach case: the no-breach (baseline) flood classified on its
    own with the ensemble's settings - the baseline named in a no-breach run's run.json,
    else another run's, else the dam's own baseline scenario. {"nd", "tm11", "basis"};
    without a baseline {"nd": None, "tm11": None, "basis": "no baseline ..."}."""
    src = None
    for r in sorted(runs, key=lambda r: r.get("status") != STATUS_NO_BREACH):
        if r.get("base_depth") and os.path.isfile(r["base_depth"]):
            src = {"depth": r["base_depth"], "vel": r.get("base_vel"),
                   "arr": r.get("base_arr"), "from": "the baseline named by run {}".format(
                       r["id"])}
            break
    b = info.get("baseline") or {}
    if src is None and b.get("depth") and os.path.isfile(b["depth"]):
        src = {"depth": b["depth"], "vel": b.get("velocity"), "arr": b.get("arrival"),
               "from": "the dam's own baseline scenario '{}'".format(b.get("name") or "")}
    if src is None:
        return {"nd": None, "tm11": None,
                "basis": "{} (neither the runs nor the dam's record have a no-breach "
                         "baseline)".format(NOT_CLASSIFIED)}
    run = {"id": NO_BREACH_ID, "depth": src["depth"], "vel": src.get("vel"),
           "arr": src.get("arr")}
    kw, _b = run_config(base_kw, run, os.path.join(ens_root, NO_BREACH_ID), name, None,
                        reviews)
    r = classify_run(run, kw, resume=resume, progress=progress)
    if r.get("status") != STATUS_DONE:
        return {"nd": None, "tm11": None, "basis": "{} (the no-breach flood could not be "
                "classified: {})".format(NOT_CLASSIFIED, r.get("message") or "?")}
    return {"nd": r.get("nd"), "tm11": r.get("tm11"),
            "basis": "ND {} / TM 11 {} - {} ({}) classified on its own as a flood, not "
                     "added to any class".format(r.get("nd"), r.get("tm11"), src["from"],
                                                 os.path.basename(src["depth"]))}


def _outline_file_of(out):
    """The FEMA_Structures outline file a run downloaded into its outputs (for the next
    runs), or None."""
    try:
        from qgis.core import QgsVectorLayer
        for f in sorted(os.listdir(out)):
            if f.endswith("_FEMA_Structures.gpkg") or f == "FEMA_Structures.gpkg":
                lyr = QgsVectorLayer(os.path.join(out, f), "FEMA_Structures", "ogr")
                if lyr.isValid():
                    return lyr
    except OSError:
        return None
    return None


def _same_crs(ok, notes):
    """The runs' structure points in the first run's CRS (a run in another CRS is
    transformed; one that cannot be is left out of the structure table)."""
    if not ok:
        return ok
    ref = ok[0].get("crs") or ""
    if all((r.get("crs") or "") == ref for r in ok):
        return ok
    try:
        from qgis.core import (QgsCoordinateReferenceSystem, QgsCoordinateTransform,
                               QgsPointXY, QgsProject)
        dst = QgsCoordinateReferenceSystem(ref)
        ctx = QgsProject.instance().transformContext()
    except Exception:                       # noqa: BLE001
        dst = None
    out = []
    for r in ok:
        if (r.get("crs") or "") == ref or dst is None:
            out.append(r)
            continue
        try:
            xf = QgsCoordinateTransform(QgsCoordinateReferenceSystem(r["crs"]), dst, ctx)
            moved = []
            for s in r.get("structures") or []:
                q = xf.transform(QgsPointXY(s["x"], s["y"]))
                moved.append(dict(s, x=q.x(), y=q.y()))
            out.append(dict(r, structures=moved, crs=ref))
            notes.append("Run {}: structures moved from {} to {}".format(r["id"], r["crs"], ref))
        except Exception:                   # noqa: BLE001
            out.append(dict(r, structures=[]))
            notes.append("Run {}: CRS {} could not be matched - its structures are left out "
                         "of the structure table (its class counts)".format(r["id"], r["crs"]))
    return out


# ============================================================== outputs ==
def _pct(p):
    return "{:.3f}".format(p)


def _params_text(params):
    return "; ".join("{}={}".format(k, v) for k, v in sorted((params or {}).items()))[:250]


def write_outputs(qa, dam_folder, agg, results, notes, ref, text, info):
    os.makedirs(qa, exist_ok=True)
    rows = []
    for section, key, classes in (("ND REG_05.2026 class", "nd", ND_CLASSES),
                                  ("ACER TM 11 class", "tm11", TM11_CLASSES)):
        d = agg[key] if agg and agg["n"] else {c: {"runs": 0, "weight": 0.0, "p": 0.0,
                                                   "ids": []} for c in classes}
        for c in list(classes) + [k for k in d if k not in classes]:
            v = d[c]
            det = agg["weight_note"] if agg and agg["n"] else "no run"
            if agg and agg["n"] and agg["no_breach"]["runs"]:
                det += "; P(class | the dam fails) = {}".format(_pct(v.get("p_breach", 0.0)))
            rows.append({"Section": section, "Item": c, "Runs": v["runs"],
                         "Weight": "{:g}".format(v["weight"]), "Probability": _pct(v["p"]),
                         "Run_IDs": " ".join(v["ids"]), "X": "", "Y": "", "CRS": "",
                         "Detail": det})
        nb = (agg or {}).get("no_breach") or {"runs": 0, "weight": 0.0, "p": 0.0, "ids": [],
                                             key: NOT_CLASSIFIED, "basis": ""}
        rows.append({"Section": section, "Item": NO_BREACH_ITEM, "Runs": nb["runs"],
                     "Weight": "{:g}".format(nb["weight"]), "Probability": _pct(nb["p"]),
                     "Run_IDs": " ".join(nb["ids"]), "X": "", "Y": "", "CRS": "",
                     "Detail": "runs in which the dam does not fail (not classified); the "
                               "no-breach case alone: {}".format(
                                   nb.get("basis") or nb.get(key) or NOT_CLASSIFIED)})
    crs = next((r.get("crs") for r in results if r.get("status") == "done"), "")
    for s in (agg or {}).get("structures") or []:
        rows.append({"Section": "Structure: probable loss of life zone",
                     "Item": "structure {} {}".format(s["id"], s["category"]).strip(),
                     "Runs": len(s["runs_probable"]), "Weight": "",
                     "Probability": _pct(s["p_probable"]),
                     "Run_IDs": " ".join(s["runs_probable"]), "X": "{:.2f}".format(s["x"]),
                     "Y": "{:.2f}".format(s["y"]), "CRS": crs,
                     "Detail": "judgment zone P={} ({} run(s)); flooded P={}; max depth {} "
                               "ft; habitable {}".format(
                                   _pct(s["p_judgment"]), len(s["runs_judgment"]),
                                   _pct(s["p_wet"]), "-" if s["max_depth"] is None else
                                   "{:.1f}".format(s["max_depth"]), s["habitable"] or "?")})
    path = os.path.join(qa, CSV_FILE)
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=CSV_FIELDS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)
    rpath = os.path.join(qa, RUNS_CSV)
    with open(rpath, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=RUN_FIELDS, extrasaction="ignore")
        w.writeheader()
        for r in results:
            w.writerow(run_row(r, dam_folder))
    hpath = os.path.join(qa, HTML_FILE)
    with open(hpath, "w", encoding="utf-8") as fh:
        fh.write(render_html(dam_folder, agg, results, notes, ref, text, info))
    return {"csv": path, "runs_csv": rpath, "html": hpath}


def run_row(r, dam_folder):
    return {"Run": r.get("id"), "Status": r.get("status"), "ND_Class": r.get("nd", ""),
            "TM11_Class": r.get("tm11", ""), "Lives_In_Jeopardy": r.get("lij", ""),
            "Weight": "" if r.get("weight") is None else "{:g}".format(r["weight"]),
            "Governing_Reason": r.get("reason", ""),
            "Judgment_Items": r.get("judgment_items", ""), "Gate": r.get("gate", ""),
            "Parameters": _params_text(r.get("params")), "Baseline": r.get("baseline", ""),
            "Reused": "yes" if r.get("reused") else "",
            "Seconds": r.get("seconds", ""),
            "Message": "; ".join(x for x in (r.get("message"), r.get("note")) if x),
            "Output_Folder": os.path.relpath(r["folder"], dam_folder) if r.get("folder")
            else ""}


_CSS = ("body{font-family:Segoe UI,Arial,sans-serif;margin:16px;color:#222;background:#fff}"
        "table{border-collapse:collapse;font-size:13px;margin-bottom:14px}td,th{border:1px "
        "solid #ccc;padding:3px 6px;vertical-align:top}th{background:#eee}"
        ".bar{display:inline-block;height:10px;background:#555}.caveat{border:2px solid "
        "#b00;padding:8px;max-width:62em;background:#fff6f6}.warn{color:#b00;font-weight:bold}"
        ".note{max-width:62em;font-size:13px}")


def render_html(dam_folder, agg, results, notes, ref, text, info):
    e = html.escape
    out = ["<!DOCTYPE html><html><head><meta charset='utf-8'><title>Class probability - {}"
           "</title><style>{}</style></head><body>".format(e(_dam_name(dam_folder)), _CSS),
           "<h1>Hazard class probability - {}</h1>".format(e(_dam_name(dam_folder))),
           "<p class='note'>{} - {} v{}</p>".format(e(time.strftime("%Y-%m-%d %H:%M")),
                                                   e(PLUGIN_NAME), e(PLUGIN_VERSION)),
           "<p class='caveat'><b>Read this first.</b> {}</p>".format(e(CAVEAT))]
    if ref and ref.get("mismatch"):
        out.append("<p class='warn'>The reference run (the dam's own rasters with the "
                   "ensemble's settings and data) gives ND {} / TM 11 {}, the dam's last run "
                   "({}) ND {} / TM 11 {}: the ensemble does not reproduce the dam's "
                   "classification - find out why before using these numbers.</p>".format(
                       e(str(ref.get("nd"))), e(str(ref.get("tm11"))),
                       e(str(ref.get("dam_time") or "")), e(str(ref.get("dam_nd"))),
                       e(str(ref.get("dam_tm11")))))
    elif ref:
        out.append("<p class='note'>Reference run (the dam's own rasters with the ensemble's "
                   "settings and data): ND {} / TM 11 {}{}.</p>".format(
                       e(str(ref.get("nd"))), e(str(ref.get("tm11"))),
                       " - the same as the dam's last run" if ref.get("dam_nd") else
                       " (the dam has no last-run snapshot to compare with)"))
    out.append("<p class='note'><b>{}</b></p>".format(e(text)))
    if UNFINISHED in text:
        out.append("<p class='warn'>The study is not finished: the ND Dam Breach Runner writes "
                   "Runs/index.json when it is. These are the runs there now, with "
                   "provisional weights - run the class probability again when the study "
                   "has finished.</p>")
    if agg and agg["n"]:
        out.append("<p class='note'>{}; total weight {:g}. Lives in jeopardy over the runs: "
                   "{:.1f} to {:.1f} (mean {:.1f}).</p>".format(
                       e(agg["weight_note"]), agg["total_weight"], agg["lij"]["min"],
                       agg["lij"]["max"], agg["lij"]["mean"]))
        nb = agg["no_breach"]
        if nb["runs"]:
            out.append("<p class='warn'>The dam does not fail in {} of {} run(s): P(no breach) "
                       "= {:.3f}. Every probability below is over ALL runs, so the classes add "
                       "up to 1 - P(no breach); the column 'given a breach' is the class "
                       "probability if the dam fails. The no-breach case alone: {}.</p>".format(
                           nb["runs"], agg["n"], nb["p"], e(nb.get("basis") or NOT_CLASSIFIED)))
        for title, key, classes in (("ND REG_05.2026 class", "nd", ND_CLASSES),
                                    ("ACER TM 11 class", "tm11", TM11_CLASSES)):
            d = agg[key]
            out.append("<h2>{}</h2><table><tr><th>Class</th><th>Probability</th><th>Given a "
                       "breach</th><th>Runs</th><th>Weight</th><th>Run IDs</th></tr>".format(
                           e(title)))
            for c in list(classes) + [k for k in d if k not in classes]:
                v = d[c]
                out.append("<tr><td>{}</td><td><span class='bar' style='width:{}px'></span> "
                           "{:.3f}</td><td>{:.3f}</td><td>{}</td><td>{:g}</td><td>{}</td></tr>"
                           .format(e(c), int(round(120 * v["p"])), v["p"],
                                   v.get("p_breach", 0.0), v["runs"], v["weight"],
                                   e(" ".join(v["ids"]))))
            out.append("<tr><td><i>{}</i></td><td><span class='bar' style='width:{}px'></span> "
                       "{:.3f}</td><td>-</td><td>{}</td><td>{:g}</td><td>{}</td></tr>".format(
                           e(NO_BREACH_ITEM), int(round(120 * nb["p"])), nb["p"], nb["runs"],
                           nb["weight"], e(" ".join(nb["ids"]))))
            out.append("</table>")
        st = agg["structures"]
        out.append("<h2>Structures in the probable loss of life zone</h2><p class='note'>"
                   "Structures matched between runs by location (within {:g} ft). P = share "
                   "of the runs (weight) in which the structure's outcome is 'Probable loss "
                   "of life' (ACER high danger zone, or depth &gt; 2 ft with the 1D rule); "
                   "the judgment zone is listed beside it.{}</p>".format(
                       MATCH_FT, " First 300 of {}.".format(len(st)) if len(st) > 300 else ""))
        out.append("<table><tr><th>Structure</th><th>P probable</th><th>P judgment</th>"
                   "<th>P flooded</th><th>Max depth ft</th><th>X</th><th>Y</th><th>Runs "
                   "(probable)</th></tr>")
        for s in st[:300]:
            out.append("<tr><td>{} {}</td><td>{:.3f}</td><td>{:.3f}</td><td>{:.3f}</td>"
                       "<td>{}</td><td>{:.1f}</td><td>{:.1f}</td><td>{}</td></tr>".format(
                           e(str(s["id"])), e(s["category"]), s["p_probable"], s["p_judgment"],
                           s["p_wet"], "-" if s["max_depth"] is None else
                           "{:.1f}".format(s["max_depth"]), s["x"], s["y"],
                           e(" ".join(s["runs_probable"][:30]))))
        out.append("</table>")
    out.append("<h2>Runs</h2><table><tr>{}</tr>".format("".join(
        "<th>{}</th>".format(e(c.replace("_", " "))) for c in RUN_FIELDS)))
    for r in results:
        row = run_row(r, dam_folder)
        out.append("<tr>{}</tr>".format("".join("<td>{}</td>".format(e(str(row[c])))
                                                for c in RUN_FIELDS)))
    out.append("</table><h2>How the runs were classified</h2><ul class='note'>")
    for n in notes:
        out.append("<li>{}</li>".format(e(n)))
    out.append("<li>Every run: PDF report, EAP outputs, zone rasters and the extra analyses "
               "off; outputs in outputs/QA/Ensemble/&lt;run&gt;. Nothing of the dam's own "
               "outputs is changed.</li></ul></body></html>")
    return "\n".join(out)
