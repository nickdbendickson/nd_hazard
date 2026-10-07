"""The suite home: one view of a Dams folder for the three plugins (pure Python).

Each plugin has a "Dam suite home..." window (suite_home.py, byte-identical in the three) that
shows this module's rows and opens the plugin whose window the next action needs. This module
knows the folder layout the plugins share (one folder per dam, named after the dam ID, with
dam.json and the Runner's / Screening's model files) and nothing about Qt or QGIS.

Which plugin the next action belongs to (NEXT_PLUGIN):
  screen   -> ND Dam Breach Screening (triage / 2D screening of the dam)
  model    -> ND Dam Breach Runner (the breach model)
  classify -> ND Dam Hazard Classification Helper (the authority)
  review   -> ND Dam Hazard (reviewer's view / mark reviewed)
  refresh  -> ND Dam Breach Screening (new data: screen again) - the dam's values first
  none     -> nothing; ND Dam Hazard's submittal / portfolio
"""
import os

from . import dam_record as DR
from . import versions as VS

NEXT_PLUGIN = {"screen": "ndscreen", "model": "ndbreach", "classify": "ndhazard",
               "review": "ndhazard", "refresh": "ndscreen", "none": "ndhazard"}
# the Runner's / Screening's model folders and the final rasters (names are the contracts)
RUNNER_FOLDER, SCREENING_FOLDER = "_breach_model", "_screening_model"
FINAL_DEPTH = "Depth (Max).tif"
CLASS_FILES = ("Hazard Classification (ND 5.2).json",)      # the Runner's own preview


def plugin_label(key):
    return VS.LABELS.get(key, key)


def _mtime(p):
    try:
        return os.path.getmtime(p)
    except OSError:
        return None


def dam_row(folder, name=None):
    """One dam folder -> dict for the home table (never raises; problems in "problem")."""
    name = name or os.path.basename(os.path.normpath(folder))
    row = {"dam_id": name, "folder": folder, "name": "", "class": "", "existing_class": "",
           "screened": "", "modelled": "", "classified": "", "reviewed": "", "next": "",
           "next_text": "", "plugin": "", "disputed": 0, "problem": "", "has_rasters": False,
           "has_manifest": False, "has_record": False}
    rec = None
    try:
        rec = DR.load(folder)
    except ValueError as e:
        row["problem"] = "dam.json cannot be read: {}".format(e)
    except Exception as e:                                       # noqa
        row["problem"] = "dam.json: {}".format(e)
    depth = os.path.join(folder, FINAL_DEPTH)
    row["has_rasters"] = os.path.isfile(depth)
    row["has_manifest"] = any(os.path.isfile(os.path.join(folder, f, "run_manifest.json"))
                              for f in (RUNNER_FOLDER, SCREENING_FOLDER))
    facts = {}
    try:
        _fill_from_record(row, rec, facts, depth)
    except Exception as e:                                       # noqa  (1.3.0: never raises)
        row["problem"] = (row["problem"] + "; " if row["problem"] else "") + \
            "dam.json could not be summarised: {}".format(e)
        rec = None
    try:
        code, text = DR.next_action(rec, facts)
    except Exception as e:                                       # noqa
        code, text = "none", "next action unknown ({})".format(e)
    if rec is None and row["has_rasters"]:
        code, text = "classify", "Flood rasters without a dam record: classify the dam (ND Dam " \
                                 "Hazard writes dam.json)."
    row["next"], row["next_text"], row["plugin"] = code, text, NEXT_PLUGIN.get(code, "ndhazard")
    return row


def _fill_from_record(row, rec, facts, depth):
    if rec is not None:
        row["has_record"] = True
        row["name"] = str(rec.get("name") or "")
        row["existing_class"] = str(rec.get("existing_hazard_class") or "")
        row["dam_id"] = str(rec.get("dam_id") or row["dam_id"])
        for step in DR.STEPS:
            e = rec.latest(step, include_previews=False) if step == "classified" else rec.latest(step)
            if e:
                res = e.get("result") if isinstance(e.get("result"), dict) else {}
                when = (e.get("when") or "")[:10]
                row[step] = "{} ({})".format(when, e.get("tool") or "?")
                if step == "classified":
                    row["class"] = str(res.get("hazard_class") or res.get("nd_class") or
                                       res.get("class") or "")
                if step == "reviewed":
                    try:
                        facts["open_review_items"] = int(res.get("open_items") or 0)
                    except (TypeError, ValueError):
                        pass
        row["disputed"] = len([c for c in rec.conflicts if not c.get("applied")])
        cla = rec.latest("classified", include_previews=False)
        if row["has_rasters"] and cla is not None:
            t = DR._parse_when(cla.get("when"))
            mt = _mtime(depth)
            if t is not None and mt is not None:
                import datetime
                if datetime.datetime.fromtimestamp(mt) > t:
                    facts["rasters_newer_than_class"] = True


def scan(dams_folder):
    """Rows of every dam folder in a Dams folder (a dam folder has dam.json, a model folder or
    the final depth raster), sorted by dam ID. [] when the folder cannot be read."""
    rows = []
    try:
        names = sorted(os.listdir(dams_folder))
    except OSError:
        return rows
    for n in names:
        p = os.path.join(dams_folder, n)
        if not os.path.isdir(p):
            continue
        if not (os.path.isfile(os.path.join(p, DR.FILE_NAME)) or
                os.path.isfile(os.path.join(p, FINAL_DEPTH)) or
                os.path.isdir(os.path.join(p, RUNNER_FOLDER)) or
                os.path.isdir(os.path.join(p, SCREENING_FOLDER))):
            continue
        try:
            rows.append(dam_row(p, n))
        except Exception as e:                                   # noqa  (one dam never hides the rest)
            rows.append({"dam_id": n, "folder": p, "name": "", "class": "", "existing_class": "",
                         "screened": "", "modelled": "", "classified": "", "reviewed": "", "next": "none",
                         "next_text": "", "plugin": "", "disputed": 0, "problem": str(e),
                         "has_rasters": False, "has_manifest": False, "has_record": False})
    return rows


def summary(rows):
    """Counts for the home window's header line."""
    out = {"dams": len(rows), "by_next": {}, "by_class": {}, "disputed": 0, "problems": 0}
    for r in rows:
        out["by_next"][r["next"]] = out["by_next"].get(r["next"], 0) + 1
        c = r["class"] or "(none)"
        out["by_class"][c] = out["by_class"].get(c, 0) + 1
        out["disputed"] += 1 if r["disputed"] else 0
        out["problems"] += 1 if r["problem"] else 0
    return out


def summary_text(s):
    if not s["dams"]:
        return "No dam folders found."
    order = ("screen", "model", "classify", "review", "refresh", "none")
    nxt = ", ".join("{} {}".format(s["by_next"][k], k) for k in order if s["by_next"].get(k))
    cls = ", ".join("{} {}".format(v, k) for k, v in sorted(s["by_class"].items(),
                                                            key=lambda kv: (kv[0] == "(none)", kv[0])))
    text = "{} dam(s); next: {}; class: {}".format(s["dams"], nxt or "-", cls or "-")
    if s["disputed"]:
        text += "; {} with disputed values".format(s["disputed"])
    if s["problems"]:
        text += "; {} with a dam.json problem".format(s["problems"])
    return text + ". Classes from dam.json (ND Dam Hazard's, previews excluded)."


def installed_plugins():
    """{key: version or None} of the three plugins in this QGIS (None outside QGIS)."""
    return {k: VS.installed(k) for k in VS.PLUGINS}
