# -*- coding: utf-8 -*-
"""v2.9: portfolio dashboard - one row per dam of a folder of dams.

For an owner, a consultant or the state with many dams: where each dam
stands after its last classification run, read from the files the runs left
(no QGIS, nothing is re-run, a missing or unreadable file is "-", never an
error). The dam folders are the ones the batch classification, the hazard-
creep watch and the dams-in-series screen use (watch.dam_folders: the
folders directly inside the parent, not "_..." / "." / "outputs").

Per dam (<dam>/outputs/...):
  Dam, NID_ID          folder name; NID ID from dam_location.json / .csv, else
                       the run log (subject_dam_nid), else the summary's NID line
  ND / TM 11 class, lives in jeopardy, flooded habitable structures
                       QA/Last_Run_Snapshot.json (else the summary text)
  Last_Run, Run_Status, Plugin_Version
                       Hazard_Classification_Run_Log.txt ("Finished", "Result",
                       first line), else the snapshot time
  Oldest_Data_Date     QA/Data_Vintage.csv (oldest dated row except the plugin)
  Open_Review_Items    snapshot["review"]["open"] when the run records it
                       (review_counts(), see INTEGRATION in the lead's notes);
                       else ESTIMATED from Classification/Habitable_Structures.csv
                       (review.review_reasons as far as the table carries them,
                       Reviewed empty) and said so in Notes
  Creep_Alert          <parent>/Hazard_Creep_Watch.csv (the latest watch)
  Series_Flags         <parent>/Dams_In_Series_Screen.csv, else the dam's own
                       QA/Dams_In_Series_Screen.csv
  Sensitivity          the verdict of the summary's SENSITIVITY section
  Termination          QA/Termination_Point.csv (status, river mile)

v2.10 - one dashboard for the three plugins of the ND dam suite (item 10):
  Dam_ID, Height_ft, Storage_acft, Record_Conflicts, Suite_Status
                       <dam>/dam.json (damsuite.dam_record.load; the legacy
                       dam_location.json is read the same way). NID_ID prefers
                       dam.json's nid_id. Record_Conflicts = values another tool
                       offered that were NOT applied (conflicts with applied False).
                       Height / storage fall back to the Screening row (said so
                       in Notes). Suite_Status = the latest status per step
                       (screened / modelled / classified / reviewed) with date,
                       tool and version, "(dam.json)" or "(files)" when it was
                       derived from the files below. A status whose result
                       says {"preview": true} (another tool's own rules, e.g.
                       the runner's ND 5.2 check) is labelled "(preview)", is
                       listed only when newer than the real one and never
                       counts as the classification.
  Screen_*             ND Dam Breach Screening results: its report folder's
                       summary.csv (tool 10: dam_id, hz_class, hz_conf ...
                       review) and, in the same folder, any .csv / .gpkg with
                       dam_id + hz_class or tri_result columns (tool 6 / tool 8
                       output layers saved as GeoPackage or CSV: tri_result,
                       hz_border, hz_gaps). Found automatically in the parent
                       folder or a folder directly inside it holding a Screening
                       summary.csv (a "_screening" folder is not listed as a
                       dam), or given as `screening=` (folder or file(s)).
                       Matched to a dam folder by dam.json dam_id / nid_id /
                       dwr_structure_id, the folder name (also the runner's
                       folder-name rule) or, when unique, the dam name.
  Model_*, Runner_Class_Preview
                       ND Dam Breach Runner: <dam>/_breach_model/run_manifest.json
                       (damsuite.chain.find_runner_manifest: created, tool, final
                       model, digest check), run_report.json (run_finished; a
                       class preview if it carries one) and "Hazard Classification
                       (ND 5.2).json" (the runner's ndhazardclass result). The
                       runner's class is a SCREENING PREVIEW, labelled so; the
                       ND class column stays this plugin's result.
  Next_Action, Next_Action_Text
                       damsuite.dam_record.next_action(record, facts) on the
                       dam.json record with the status the files show added in
                       memory (never saved): screened <- the Screening row
                       when dam.json has no screened status (result {"triage",
                       "hz_class"}; triage "clear" from tri_result, or hz_conf
                       "triage"; time = the report file's time, never after the
                       dam's breach model), modelled <- the runner
                       manifest / run report / depth raster time, classified <-
                       this plugin's last successful run. facts:
                       open_review_items (Open_Review_Items - v2.12: replaced by
                       the open_items of the newest "reviewed" status in dam.json
                       when that review is newer than the classification; the
                       reviewer's sign-off, ndhazard:markreviewed), data_age_days
                       (today - Oldest_Data_Date), rasters_newer_than_class (a
                       depth raster of the last run's snapshot modified after
                       the run).
Rows: dams needing an action first, then by class (ND, TM 11, the runner's
preview, the screening class), then by name.

build() writes Dam_Portfolio.html (one self-contained page: a table sorted by
clicking a column header, class colour chips, the date and the plugin version)
and Dam_Portfolio.csv next to it. The classes are the plugin's SCREENING
results as last run - not classifications until the engineer adopts them.
"""

import csv
import datetime
import glob
import html
import json
import os
import re
import sys
import time

from . import history as HIS
from .constants import PLUGIN_NAME, PLUGIN_VERSION, OUTPUT_FOLDER_NAME, SUBDIR_QA, \
    SUBDIR_CLASSIFICATION
from .damsuite import dam_record as DR, chain as CH

HTML_FILE = "Dam_Portfolio.html"
CSV_FILE = "Dam_Portfolio.csv"
MISSING = "-"
RUN_LOG = "Hazard_Classification_Run_Log.txt"           # pipeline.RUN_LOG
SUMMARY = "Hazard_Classification_Summary.txt"
VINTAGE_CSV = "Data_Vintage.csv"                        # pipeline_extras.VINTAGE_CSV
WATCH_CSV = "Hazard_Creep_Watch.csv"                    # watch.CSV_LATEST
SERIES_CSV = "Dams_In_Series_Screen.csv"                # series.CSV_FILE
TERMINATION_CSV = "Termination_Point.csv"               # termination.CSV_NAME
LOCATION_JSON, LOCATION_CSV = "dam_location.json", "dam_location.csv"   # series.py
STRUCTURES_BASE = "Habitable_Structures"

PORTFOLIO_FIELDS = ["Dam", "NID_ID", "ND_Class", "TM11_Class", "Lives_In_Jeopardy",
                    "Flooded_Habitable", "Last_Run", "Run_Status", "Plugin_Version",
                    "Oldest_Data_Date", "Oldest_Data_Source", "Open_Review_Items",
                    "Creep_Alert", "Creep_Message", "Series_Flags", "Sensitivity",
                    "Termination",
                    # v2.10: the suite (dam.json, Screening, Runner, next action)
                    "Next_Action", "Next_Action_Text", "Dam_ID", "Height_ft", "Storage_acft",
                    "Suite_Status", "Record_Conflicts",
                    "Screen_Class", "Screen_Triage", "Screen_Border", "Screen_Gaps",
                    "Screen_Review", "Screen_Date", "Screen_Source",
                    "Model_Date", "Model_Tool", "Model_Manifest", "Runner_Class_Preview",
                    "Runner_Preview_Date",
                    "Notes", "Folder"]
CLASS_RANK = {"high": 3, "significant": 2, "medium": 2, "low": 1}
# v2.10 suite files
SCREEN_SUMMARY = "summary.csv"                  # nd_dam_screening core/report.summary()
SCREEN_INDEX = "index.html"
SCREEN_FIELDS = ("dam_id", "name", "hz_class", "hz_conf", "hz_border", "hz_gaps",
                 "hz_vs_existing", "haz_existing", "tri_result", "review", "height_m",
                 "height_ft", "store_af")
RUNNER_REPORT_JSON = "run_report.json"          # nd_breach_runner core/workflow.py
RUNNER_HAZARD_JSON = "Hazard Classification (ND 5.2).json"   # ndbreach:ndhazardclass
RUNNER_DEPTH = "Depth (Max).tif"                # workflow.FINAL_NAMES["depth"]
FT_PER_M = 1.0 / 0.3048
ACTION_ORDER = ("screen", "model", "classify", "review", "refresh", "none")
RASTER_SLACK_S = 120.0          # the snapshot time has minute precision
_DRY = ("", None, "Not inundated")                      # structures.OUT_DRY
_NID_RE = re.compile(r"\b([A-Z]{2}\d{5})\b")
_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")


# ============================================================ file readers ==
# (also used by formfill.py)
def _set_csv_limit():
    try:
        csv.field_size_limit(min(sys.maxsize, 2 ** 31 - 1))   # WKT columns are long
    except (OverflowError, ValueError):
        pass


def read_text(path):
    for enc in ("utf-8-sig", "cp1252"):
        try:
            with open(path, encoding=enc) as fh:
                return fh.read()
        except UnicodeDecodeError:
            continue
        except OSError:
            return None
    return None


def read_csv_rows(path):
    """[dict] of a CSV (UTF-8 with or without BOM, else cp1252; ',' or ';'),
    [] when missing or unreadable."""
    text = read_text(path) if path and os.path.isfile(path) else None
    if not text:
        return []
    _set_csv_limit()
    first = text.splitlines()[0] if text.strip() else ""
    delim = ";" if first.count(";") > first.count(",") else ","
    try:
        return [{(k or "").strip(): (v if v is not None else "") for k, v in r.items()}
                for r in csv.DictReader(text.splitlines(), delimiter=delim)]
    except csv.Error:
        return []


def newest(folder, pattern):
    """The newest file matching `pattern` in folder (the plugin writes a time-
    stamped copy when the file is open in another program), or None."""
    hits = [p for p in glob.glob(os.path.join(folder, pattern)) if os.path.isfile(p)]
    return max(hits, key=os.path.getmtime) if hits else None


def output_folder_of(dam_folder):
    return os.path.join(dam_folder, OUTPUT_FOLDER_NAME)


def parse_summary(text):
    """{"header": {version, dam, generated, model}, "sections": {heading:
    [lines]}} of Hazard_Classification_Summary.txt: a heading is a line
    starting in column 0, its lines are the indented ones below it ("- "
    removed)."""
    out = {"header": {}, "sections": {}, "order": []}
    if not text:
        return out
    lines = text.splitlines()
    if lines:
        m = re.search(r"\bv(\d+(?:\.\d+)+)\b", lines[0])
        if m:
            out["header"]["version"] = m.group(1)
    cur = None
    for ln in lines[1:]:
        if not ln.strip():
            continue
        if ln.startswith("Dam: ") and "Generated:" in ln:
            dam, _s, gen = ln[5:].partition("Generated:")
            out["header"]["dam"] = dam.strip()
            out["header"]["generated"] = gen.strip()
            continue
        if ln.startswith("Hydraulic model:"):
            out["header"]["model"] = ln.split(":", 1)[1].strip()
            continue
        if not ln[0].isspace():
            cur = ln.strip()
            out["sections"][cur] = []
            out["order"].append(cur)
            continue
        if cur is not None:
            s = ln.strip()
            out["sections"][cur].append(s[2:] if s.startswith("- ") else s)
    return out


def section(parsed, prefix):
    """(heading, lines) of the first section whose heading starts with
    `prefix`, or (None, [])."""
    for h in parsed.get("order", []):
        if h.upper().startswith(prefix.upper()):
            return h, parsed["sections"][h]
    return None, []


def summary_facts(parsed):
    """Classes, lives in jeopardy, NID and the optional-analysis lines from a
    parsed summary."""
    f = {}
    h, lines = section(parsed, "ND HAZARD CLASSIFICATION")
    if h:
        # v2.28: "  |  incomplete: flood cut off (...)" after the class is a marker, not the class
        f["nd"] = h.split(":", 1)[1].split("|", 1)[0].strip().capitalize() if ":" in h else ""
        f["nd_reasons"] = [x for x in lines if not x.startswith("Basis:")]
    h, lines = section(parsed, "ACER TM 11 CLASSIFICATION")
    if h:
        m = re.match(r"ACER TM 11 CLASSIFICATION:\s*(\S+)\s*\(lives in jeopardy = ([\d.]+)\)",
                     h)
        if m:
            f["tm11"] = m.group(1).capitalize()
            f["lij"] = float(m.group(2))
        f["tm11_lines"] = lines
    _h, lines = section(parsed, "NATIONAL INVENTORY OF DAMS")
    for x in lines:
        m = _NID_RE.search(x.split(":")[0])
        if m:
            f["nid"] = m.group(1)
            break
    _h, lines = section(parsed, "SENSITIVITY")
    if lines:
        x = lines[0]
        f["sensitivity"] = x.split("not a requirement):", 1)[1].strip() \
            if "not a requirement):" in x else x
    _h, lines = section(parsed, "DOWNSTREAM TERMINATION")
    if lines:
        f["termination_line"] = lines[0]
    return f


def parse_run_log(text):
    """{"version", "finished", "result", "failed", "settings": {key: text}} of
    the run log."""
    out = {"settings": {}}
    if not text:
        return out
    lines = text.splitlines()
    m = re.search(r"\bv(\d+(?:\.\d+)+)\b", lines[0]) if lines else None
    if m:
        out["version"] = m.group(1)
    in_set = False
    for ln in lines[1:]:
        if ln.startswith("Finished:"):
            out["finished"] = ln.split(":", 1)[1].strip()
        elif ln.startswith("Result:"):
            out["result"] = ln.split(":", 1)[1].strip()
            out["failed"] = out["result"].upper().startswith("FAILED")
        elif ln.strip() == "SETTINGS":
            in_set = True
        elif in_set:
            if not ln.startswith("  "):
                if ln.strip():
                    in_set = False
                continue
            k, eq, v = ln.strip().partition(" = ")
            if eq:
                out["settings"][k] = v
    return out


def read_location_nid(dam_folder):
    """NID ID from dam_location.json / dam_location.csv of a dam folder, or ""."""
    js = os.path.join(dam_folder, LOCATION_JSON)
    if os.path.isfile(js):
        try:
            with open(js, encoding="utf-8-sig") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                low = {str(k).lower(): v for k, v in data.items()}
                return str(low.get("nid") or low.get("nidid") or "").strip()
        except (OSError, ValueError):
            return ""
    rows = read_csv_rows(os.path.join(dam_folder, LOCATION_CSV))
    if rows:
        name = os.path.basename(os.path.normpath(dam_folder)).lower()
        low = [{k.lower(): (v or "").strip() for k, v in r.items()} for r in rows]
        pick = next((r for r in low if (r.get("name") or r.get("dam") or "").lower() == name),
                    low[0])
        return pick.get("nid") or pick.get("nidid") or ""
    return ""


def vintage_oldest(qa_folder):
    """(date, "source: detail") of the oldest dated row of Data_Vintage.csv
    (the plugin's own row left out), or (None, None)."""
    best = None
    for r in read_csv_rows(os.path.join(qa_folder, VINTAGE_CSV)):
        if (r.get("Source") or "").strip().lower() == "plugin":
            continue
        dates = _DATE_RE.findall(r.get("Date") or "")
        if not dates:
            continue
        d = min(dates)
        if best is None or d < best[0]:
            best = (d, "{}: {}".format(r.get("Source") or "?", r.get("Detail") or ""))
    return best if best else (None, None)


def _slug(name):
    return re.sub(r"[^A-Za-z0-9]+", "_", str(name or "")).strip("_")[:40] or "Scenario"


def structures_csv(out_folder, governing=None):
    """Classification/Habitable_Structures[_<governing slug>].csv, or None."""
    folder = os.path.join(out_folder, SUBDIR_CLASSIFICATION)
    names = [STRUCTURES_BASE + ".csv"]
    if governing:
        names.insert(0, "{}_{}.csv".format(STRUCTURES_BASE, _slug(governing)))
    for n in names:
        p = os.path.join(folder, n)
        if os.path.isfile(p):
            return p
    return None


def open_review_from_csv(path):
    """(open, reviewed) review items ESTIMATED from Habitable_Structures.csv:
    the reasons of review.review_reasons the table carries (judgment zone,
    near a zone boundary, habitability unverified, sources disagree, possible
    lifeline, occupied without occupancy data, point moved onto an outline,
    dry but cut off) with an empty Reviewed column. Not habitable structures
    are not in the table, so a judgment-zone one of them is not counted."""
    rows = read_csv_rows(path)
    if not rows:
        return None
    n_open = n_done = 0
    for r in rows:
        wet = (r.get("ND_Outcome") or "") not in _DRY
        notes = (r.get("Notes") or "").lower()
        why = ((wet and r.get("Habitable") == "Unverified")
               or (wet and r.get("Type_Conflict") == "Yes")
               or r.get("ND_Outcome") == "Judgment zone"
               or bool((r.get("Near_Threshold") or "").strip())
               or (wet and r.get("Lifeline") == "Yes")
               or (wet and "occupied building without occupancy data" in notes)
               or (wet and "moved onto this outline" in notes)
               or (wet and "nd site address" in notes)
               or (not wet and r.get("Access_Cut_Off") == "Yes"
                   and r.get("Habitable") == "Yes"))
        if not why:
            continue
        if (r.get("Reviewed") or "").strip():
            n_done += 1
        else:
            n_open += 1
    return n_open, n_done


def review_counts(structs):
    """For the pipeline to record in the snapshot (snap["review"]): the
    structures of the review list (review.needs_review) not reviewed yet /
    reviewed. Needs the run's structure records (QGIS)."""
    from .review import needs_review
    rows = needs_review(structs)
    done = sum(1 for rec, _w in rows if rec.get("review"))
    return {"open": len(rows) - done, "reviewed": done, "listed": len(rows)}


# ================================================= the suite (v2.10) ==
def _iso(text):
    """ISO 8601 (to the second) of a date / time text the tools write, or None."""
    s = str(text or "").strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            return datetime.datetime.strptime(s, fmt).isoformat(timespec="seconds")
        except ValueError:
            continue
    try:
        t = datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if t.tzinfo is not None:
        t = t.astimezone().replace(tzinfo=None)
    return t.isoformat(timespec="seconds")


def _mtime_iso(path):
    try:
        return datetime.datetime.fromtimestamp(os.path.getmtime(path)).isoformat(
            timespec="seconds")
    except (OSError, ValueError, OverflowError):
        return None


def _day(iso):
    return str(iso)[:10] if iso else MISSING


def _read_json(path):
    """A JSON object from a file, or None (missing, unreadable, not an object)."""
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8-sig") as fh:
            d = json.load(fh)
    except (OSError, ValueError):
        return None
    return d if isinstance(d, dict) else None


def _screen_cols_ok(cols):
    low = {str(c).strip().lower() for c in cols}
    return "dam_id" in low and ("hz_class" in low or "tri_result" in low)


def _csv_header(path):
    for enc in ("utf-8-sig", "cp1252"):
        try:
            with open(path, encoding=enc, newline="") as fh:
                first = fh.readline()
            break
        except UnicodeDecodeError:
            continue
        except OSError:
            return []
    else:
        return []
    delim = ";" if first.count(";") > first.count(",") else ","
    try:
        return next(csv.reader([first], delimiter=delim), [])
    except csv.Error:
        return []


def _gpkg_rows(path):
    """Attribute rows (lower-case keys, text values) of the tables of a GeoPackage
    that carry Screening fields (dam_id + hz_class or tri_result); [] otherwise.
    Plain sqlite3 (a GeoPackage is a SQLite file); opened read-only."""
    import sqlite3
    from urllib.request import pathname2url
    try:
        con = sqlite3.connect("file:{}?mode=ro".format(pathname2url(os.path.abspath(path))),
                              uri=True)
    except (sqlite3.Error, ValueError):
        return []
    out = []
    try:
        tables = [r[0] for r in con.execute("SELECT table_name FROM gpkg_contents")]
        for t in tables:
            qt = '"{}"'.format(str(t).replace('"', '""'))
            cols = [r[1] for r in con.execute("PRAGMA table_info({})".format(qt))]
            if not _screen_cols_ok(cols):
                continue
            low = {str(c).lower(): c for c in cols}
            want = [low[k] for k in SCREEN_FIELDS if k in low]
            sql = "SELECT {} FROM {}".format(
                ", ".join('"{}"'.format(str(c).replace('"', '""')) for c in want), qt)
            for row in con.execute(sql):
                out.append({str(c).lower(): ("" if v is None else str(v))
                            for c, v in zip(want, row)})
    except sqlite3.Error:
        return out
    finally:
        con.close()
    return out


def screening_rows(path):
    """Rows (lower-case keys) of one Screening result file (.csv or .gpkg)."""
    if path.lower().endswith(".gpkg"):
        return _gpkg_rows(path)
    rows = read_csv_rows(path)
    return [{k.lower(): v for k, v in r.items()} for r in rows] \
        if rows and _screen_cols_ok(rows[0].keys()) else []


def screening_files(path):
    """The Screening result files of a folder (summary.csv first, then the other
    .csv / .gpkg files directly in it with Screening columns), or [path] for a
    file."""
    if not path:
        return []
    if os.path.isfile(path):
        return [path] if path.lower().endswith((".csv", ".gpkg")) else []
    if not os.path.isdir(path):
        return []
    out = []
    summ = os.path.join(path, SCREEN_SUMMARY)
    if os.path.isfile(summ) and _screen_cols_ok(_csv_header(summ)):
        out.append(summ)
    try:
        names = sorted(os.listdir(path))
    except OSError:
        names = []
    for n in names:
        p = os.path.join(path, n)
        if n.lower() == SCREEN_SUMMARY or not os.path.isfile(p):
            continue
        if n.lower().endswith(".csv") and _screen_cols_ok(_csv_header(p)):
            out.append(p)
        elif n.lower().endswith(".gpkg"):
            out.append(p)            # checked when read (no Screening table -> no rows)
    return out


def find_screening(parent):
    """Screening report folders: the parent itself or a folder directly inside it
    (also "_..." ones) holding a Screening summary.csv."""
    cands = [parent]
    try:
        cands += sorted(os.path.join(parent, d) for d in os.listdir(parent)
                        if not d.startswith(".") and os.path.isdir(os.path.join(parent, d)))
    except OSError:
        pass
    out = []
    for c in cands:
        s = os.path.join(c, SCREEN_SUMMARY)
        if os.path.isfile(s) and _screen_cols_ok(_csv_header(s)):
            out.append(c)
    return out


def _runner_safe(s):
    """The runner's folder name for a dam ID (nd_breach_runner algorithms._safe_name)."""
    return re.sub(r"[^\w\- ]+", "_", str(s)).strip() or "dam"


def screening_index(sources):
    """{"by_id", "by_name", "files", "folders"} of Screening rows merged per dam
    from `sources` (folders or files); a newer file's non-empty values win."""
    files, folders = [], []
    for s in sources or []:
        for f in screening_files(s):
            if f not in files:
                files.append(f)
        if s and os.path.isdir(s):
            folders.append(s)
    files.sort(key=lambda p: os.path.getmtime(p) if os.path.exists(p) else 0)
    by_id = {}
    for f in files:
        when = _mtime_iso(f)
        for r in screening_rows(f):
            key = str(r.get("dam_id") or "").strip()
            if not key:
                continue
            m = by_id.setdefault(key.lower(), {"_files": []})
            for k, v in r.items():
                if v not in (None, "") and str(v).strip().upper() != "NULL":
                    m[k] = str(v).strip()
            if f not in m["_files"]:
                m["_files"].append(f)
            if when and (m.get("_when") is None or when > m["_when"]):
                m["_when"] = when
    for key, m in list(by_id.items()):
        alt = _runner_safe(m.get("dam_id") or key).lower()
        by_id.setdefault(alt, m)
    by_name = {}
    for m in by_id.values():
        nm = str(m.get("name") or "").strip().lower()
        if nm:
            by_name.setdefault(nm, [])
            if m not in by_name[nm]:
                by_name[nm].append(m)
    return {"by_id": by_id, "by_name": by_name, "files": files, "folders": folders}


def screening_for(index, folder, rec=None):
    """The merged Screening row of a dam folder, or None."""
    if not index or not index.get("by_id"):
        return None
    keys = []
    if rec is not None:
        keys += [rec.get("dam_id"), rec.get("nid_id"), rec.get("dwr_structure_id")]
    name = os.path.basename(os.path.normpath(folder))
    keys.append(name)
    for k in keys:
        k = str(k or "").strip().lower()
        if k and k in index["by_id"]:
            return index["by_id"][k]
    for nm in (rec.get("name") if rec is not None else None, name):
        hits = index["by_name"].get(str(nm or "").strip().lower()) or []
        if len(hits) == 1:
            return hits[0]
    return None


def _screen_triage(s):
    """The triage word of a Screening row: tri_result, else "clear" when tool 8
    marked the dam as cleared by triage (hz_conf "triage"), else "needs 2D" when
    a 2D class exists (the dam was not cleared), else ""."""
    t = str(s.get("tri_result") or "").strip()
    if t:
        return t
    if str(s.get("hz_conf") or "").strip().lower() == "triage":
        return "clear"
    if str(s.get("hz_class") or "").strip():
        return "needs 2D"
    return ""


def _preview_class(d):
    """A class preview carried by a runner JSON (ndhazardclass result or a run
    report), or ""."""
    if not isinstance(d, dict):
        return ""
    for k in ("hazard_class", "nd_class", "class_preview"):
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    for sub in ("summary", "classification", "nd_hazard", "hazard"):
        v = d.get(sub)
        if isinstance(v, dict):
            got = _preview_class(v)
            if got:
                return got
    return ""


def runner_facts(folder):
    """What the ND Dam Breach Runner left in a dam folder: {"when", "tool", "final_model",
    "manifest", "manifest_note", "preview", "preview_when", "preview_tool", "depth"}
    (missing keys = not found)."""
    out = {}
    mp = None
    try:
        mp = CH.find_runner_manifest(folder)
    except OSError:
        mp = None
    model_dir = os.path.dirname(mp) if mp else os.path.join(folder, CH.MODEL_FOLDER)
    run_folder = os.path.dirname(model_dir)
    if mp:
        try:
            m = CH.read_runner_manifest(mp)
        except (OSError, ValueError):
            m = None
        if m is None:
            out["manifest"] = "unreadable"
        else:
            ok, _txt = CH.manifest_ok(m)
            out["manifest"] = "digest OK" if ok else "EDITED after the run"
            out["when"] = _iso(m.get("created")) or _iso(m.get("run_started"))
            out["tool"] = str(m.get("tool") or "")
            out["final_model"] = str((m.get("extra") or {}).get("final_model") or "")
    rep = _read_json(os.path.join(model_dir, RUNNER_REPORT_JSON))
    if rep is not None:
        out.setdefault("when", None)
        out["when"] = out["when"] or _iso(rep.get("run_finished")) or _iso(rep.get("run_started"))
        out["tool"] = out.get("tool") or str(rep.get("tool") or "")
        out["final_model"] = out.get("final_model") or str(
            (rep.get("summary") or {}).get("final_model") or "")
        pv = _preview_class(rep)
        if pv:
            out["preview"], out["preview_when"] = pv, out["when"]
            out["preview_tool"] = "run report"
    depth = next((p for p in (os.path.join(run_folder, RUNNER_DEPTH),
                              os.path.join(folder, RUNNER_DEPTH)) if os.path.isfile(p)), None)
    if depth:
        out["depth"] = depth
        if not out.get("when"):
            out["when"] = _mtime_iso(depth)
    for p in (os.path.join(run_folder, RUNNER_HAZARD_JSON), os.path.join(folder,
                                                                         RUNNER_HAZARD_JSON)):
        d = _read_json(p)
        if d is None:
            continue
        pv = _preview_class(d)
        if pv:
            out["preview"] = pv
            out["preview_when"] = _iso(d.get("created")) or _mtime_iso(p)
            out["preview_tool"] = str(d.get("tool") or "ndbreach:ndhazardclass")
        break
    return {k: v for k, v in out.items() if v not in (None, "")}


def _tool_version(text):
    m = re.search(r"\b(\d+(?:\.\d+)+)\b", str(text or ""))
    return m.group(1) if m else ""


def _num_or_none(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if x == x and abs(x) != float("inf") else None


def _rasters_newer(snap, cls_iso):
    """The first depth raster of the last run's snapshot modified after the run,
    or None."""
    if not snap or not cls_iso:
        return None
    try:
        t = datetime.datetime.fromisoformat(cls_iso).timestamp() + RASTER_SLACK_S
    except (ValueError, OverflowError, OSError):
        return None
    for p in (snap.get("rasters") or {}).values():
        p = str(p or "").split("|")[0]
        try:
            if p and os.path.isfile(p) and os.path.getmtime(p) > t:
                return p
        except OSError:
            continue
    return None


def _is_preview(entry):
    res = entry.get("result") if isinstance(entry, dict) else None
    return isinstance(res, dict) and res.get("preview") is True


def _latest_preview(shadow, step):
    """The newest PREVIEW status entry of a step ({"preview": true} in its result,
    e.g. the runner's own ND 5.2 check), or None."""
    tmp = DR.DamRecord(shadow.folder)
    tmp.status = [x for x in shadow.status if isinstance(x, dict) and x.get("step") == step
                  and _is_preview(x)]
    return tmp.latest(step)


def _entry_text(step, e, derived_ids):
    res = e.get("result") if isinstance(e.get("result"), dict) else {}
    bits = [str(res[k]) for k in ("triage", "hz_class", "final_model", "nd", "class",
                                  "hazard_class")
            if res.get(k) not in (None, "")]
    return "{} {} {}{}{}{}{}".format(
        step, _day(e.get("when")), e.get("tool") or "?",
        " " + str(e["version"]) if e.get("version") else "",
        " [{}]".format(", ".join(bits)) if bits else "",
        " (preview)" if _is_preview(e) else "",
        " (files)" if id(e) in derived_ids else " (dam.json)")


def _status_text(shadow, derived_ids):
    """"screened 2026-05-01 ndscreen 0.9.0 [needs 2D, Medium] (files); ..." A step's
    entry is the newest one that is not a preview ({"preview": true}: another tool's
    own rules - never the classification); a newer preview is listed after it,
    labelled "(preview)"."""
    parts = []
    for step in DR.STEPS:
        e = shadow.latest(step, include_previews=False)
        p = _latest_preview(shadow, step)
        if e is not None:
            parts.append(_entry_text(step, e, derived_ids))
        if p is not None and (e is None or DR._newer(shadow, p, e)):
            parts.append(_entry_text(step, p, derived_ids))
    return "; ".join(parts)


def _before_model(shadow, when):
    """The time of a screening known only from its report file: the file time,
    but never after the dam's breach model (a report re-written after the model
    does not make the screening newer than it; in the suite the runner follows
    the screening)."""
    mod = shadow.latest("modelled")
    if not when or mod is None:
        return when
    try:
        t = datetime.datetime.fromisoformat(when)
        m = datetime.datetime.fromisoformat(str(mod.get("when")))
    except (TypeError, ValueError):
        return when
    if t >= m:
        return (m - datetime.timedelta(seconds=1)).isoformat(timespec="seconds")
    return when


def suite_columns(row, folder, snap, log, parsed, screen_index=None, today=None):
    """Fill the v2.10 columns of `row` (dam.json, Screening, Runner, next action);
    returns the notes to add."""
    notes = []
    rec = None
    try:
        rec = DR.load(folder)
    except ValueError as e:
        notes.append("dam.json could not be read: {}".format(e))
    except OSError as e:
        notes.append("dam.json could not be read: {}".format(e))
    if rec is not None:
        if rec.get("dam_id"):
            row["Dam_ID"] = str(rec.get("dam_id"))
        if rec.get("nid_id"):
            row["NID_ID"] = str(rec.get("nid_id"))
        for key, field in (("Height_ft", "height_ft"), ("Storage_acft", "storage_acft")):
            v = _num_or_none(rec.get(field))
            if v is not None:
                row[key] = _fmt_num(v, 1)
        refused = [c for c in rec.conflicts if isinstance(c, dict) and not c.get("applied")]
        row["Record_Conflicts"] = str(len(refused))
        if refused:
            notes.append("dam.json: {} value(s) offered by another tool not applied ({})".format(
                len(refused), ", ".join(sorted({str(c.get("field")) for c in refused}))))
    shadow = DR.DamRecord(folder)
    if rec is not None:
        shadow.status = [dict(s) for s in rec.status if isinstance(s, dict)]
    derived = set()

    def derive(step, tool, version, result, when):
        if not when:
            return
        e = shadow.add_status(step, tool, version, result, when=when)
        derived.add(id(e))

    # Runner
    rf = runner_facts(folder)
    if rf.get("when"):
        row["Model_Date"] = _day(rf["when"])
        row["Model_Tool"] = " / ".join(x for x in (rf.get("tool"), rf.get("final_model")) if x) \
            or MISSING
        derive("modelled", "ndbreach", _tool_version(rf.get("tool")),
               {"final_model": rf["final_model"]} if rf.get("final_model") else {}, rf["when"])
    if rf.get("manifest"):
        row["Model_Manifest"] = rf["manifest"]
        if rf["manifest"] != "digest OK":
            notes.append("runner manifest {}".format(rf["manifest"]))
    if rf.get("preview"):
        row["Runner_Class_Preview"] = rf["preview"].strip().capitalize()
        row["Runner_Preview_Date"] = _day(rf.get("preview_when"))
    # Screening
    s = screening_for(screen_index, folder, rec)
    if s is not None:
        row["Screen_Class"] = s.get("hz_class") or MISSING
        tri = _screen_triage(s)
        row["Screen_Triage"] = tri or MISSING
        row["Screen_Border"] = s.get("hz_border") or MISSING
        row["Screen_Gaps"] = s.get("hz_gaps") or MISSING
        row["Screen_Review"] = s.get("review") or MISSING
        row["Screen_Date"] = _day(s.get("_when"))
        row["Screen_Source"] = "; ".join(s.get("_files") or []) or MISSING
        res = {"triage": tri}
        if s.get("hz_class"):
            res["hz_class"] = s["hz_class"]
        if not any(isinstance(x, dict) and x.get("step") == "screened"
                   for x in shadow.status):
            derive("screened", "ndscreen", "", res, _before_model(shadow, s.get("_when")))
        if row["Height_ft"] == MISSING and _num_or_none(s.get("height_ft")) is not None:
            row["Height_ft"] = _fmt_num(_num_or_none(s["height_ft"]), 1)     # Screening 0.18.0
            notes.append("height from the Screening inventory (height_ft), not dam.json")
        elif row["Height_ft"] == MISSING and _num_or_none(s.get("height_m")) is not None:
            row["Height_ft"] = _fmt_num(_num_or_none(s["height_m"]) * FT_PER_M, 1)
            notes.append("height from the Screening inventory (height_m), not dam.json")
        if row["Storage_acft"] == MISSING and _num_or_none(s.get("store_af")) is not None:
            row["Storage_acft"] = _fmt_num(s["store_af"], 1)
            notes.append("storage from the Screening run (store_af), not dam.json")
    # this plugin's classification
    cls_iso = None
    if log.get("finished") and not log.get("failed"):
        cls_iso = _iso(log["finished"])
    if cls_iso is None and snap is not None:
        cls_iso = _iso(snap.get("time"))
    if cls_iso is None and parsed["order"]:
        cls_iso = _iso(parsed["header"].get("generated"))
    if cls_iso and row["ND_Class"] != MISSING:
        derive("classified", "ndhazard", row["Plugin_Version"] if row["Plugin_Version"] != MISSING
               else "", {"nd": row["ND_Class"]}, cls_iso)
    # next action
    facts = {}
    try:
        facts["open_review_items"] = int(row["Open_Review_Items"])
    except (TypeError, ValueError):
        pass
    # v2.12: the reviewer's sign-off (ndhazard:markreviewed) - its open items count when
    # the review is newer than the classification; "Accepted" with 0 closes the review
    try:
        from . import markreviewed as MR
        rf = MR.review_facts(shadow)
    except Exception as e:                                   # noqa
        rf = {}
        notes.append("review status not read: {}".format(e))
    if rf:
        facts["open_review_items"] = rf["open_review_items"]
        row["Open_Review_Items"] = str(rf["open_review_items"])
        notes.append("reviewed {} by {}: {} ({} open item(s), dam.json)".format(
            _day(rf["review_when"]), rf["reviewer"] or "?", rf["review_decision"] or "?",
            rf["open_review_items"]))
    if row["Oldest_Data_Date"] != MISSING:
        try:
            d = datetime.date.fromisoformat(row["Oldest_Data_Date"])
            facts["data_age_days"] = float(((today or datetime.date.today()) - d).days)
        except ValueError:
            pass
    newer = _rasters_newer(snap, cls_iso)
    if newer:
        facts["rasters_newer_than_class"] = True
        notes.append("{} changed after the last classification run".format(
            os.path.basename(newer)))
    code, text = DR.next_action(shadow, facts)
    row["Next_Action"], row["Next_Action_Text"] = code, text
    row["Suite_Status"] = _status_text(shadow, derived) or MISSING
    return notes


# ================================================================== rows ==
def _dam_folders(parent):
    from .watch import dam_folders
    return dam_folders(parent)


def _norm(p):
    return os.path.normcase(os.path.abspath(p or ""))


def _watch_rows(parent):
    path = os.path.join(parent, WATCH_CSV)
    rows = read_csv_rows(path)
    day = time.strftime("%Y-%m-%d", time.localtime(os.path.getmtime(path))) if rows else ""
    by_folder, by_name = {}, {}
    for r in rows:
        if r.get("Folder"):
            by_folder[_norm(r["Folder"])] = r
        if r.get("Dam"):
            by_name[r["Dam"].strip().lower()] = r
    return by_folder, by_name, day


def _series_text(rows, name, folder):
    """Short text of the dams-in-series rows that concern this dam."""
    me_f, me_n = _norm(folder), name.strip().lower()
    out = []
    for r in rows:
        up = _norm(r.get("Upstream_Folder")) == me_f if r.get("Upstream_Folder") else \
            (r.get("Upstream") or "").strip().lower() == me_n
        down = _norm(r.get("Downstream_Folder")) == me_f if r.get("Downstream_Folder") else \
            (r.get("Downstream") or "").strip().lower() == me_n
        flags = (r.get("Flags") or "").strip()
        depth = r.get("Depth_ft") or r.get("Max_Depth_Within_Tol_ft") or ""
        tail = "{}{}".format(" " + depth + " ft" if depth else "",
                             " [" + flags + "]" if flags else "")
        if up:
            out.append((0 if flags else 1, "floods {}{}".format(r.get("Downstream"), tail)))
        elif down:
            out.append((0 if flags else 1, "flooded by {}{}".format(r.get("Upstream"), tail)))
    return "; ".join(t for _k, t in sorted(out)) if out else ""


def _fmt_num(v, nd=1):
    try:
        return "{:.{}f}".format(float(v), nd)
    except (TypeError, ValueError):
        return MISSING


def dam_row(folder, parent_watch=None, parent_series=None, screen_index=None, today=None):
    """One portfolio row (PORTFOLIO_FIELDS) from a dam folder's files.
    `screen_index`: screening_index() of the Screening results (None = none);
    `today`: a datetime.date for the data age (default today)."""
    name = os.path.basename(os.path.normpath(folder))[:80] or "Dam"
    row = dict.fromkeys(PORTFOLIO_FIELDS, MISSING)
    row.update({"Dam": name, "Folder": folder, "Notes": ""})
    notes = []
    out = output_folder_of(folder)
    qa = os.path.join(out, SUBDIR_QA)
    snap = HIS.read_snapshot(qa)
    parsed = parse_summary(read_text(newest(out, "Hazard_Classification_Summary*.txt") or ""))
    sf = summary_facts(parsed)
    log = parse_run_log(read_text(newest(out, "Hazard_Classification_Run_Log*.txt") or ""))
    if snap is None and not parsed["order"] and not log.get("finished"):
        row["Run_Status"] = "no run"
        notes.append("no classification run found in outputs/")
    # classes
    if snap is not None:
        row["ND_Class"] = snap.get("nd") or MISSING
        row["TM11_Class"] = snap.get("tm11") or MISSING
        row["Lives_In_Jeopardy"] = _fmt_num(snap.get("lij"))
        row["Flooded_Habitable"] = str(sum(
            1 for s in snap.get("structures", []) if s.get("habitable") == "Yes"
            and s.get("outcome") not in _DRY))
    else:
        if sf.get("nd"):
            row["ND_Class"] = sf["nd"]
        if sf.get("tm11"):
            row["TM11_Class"] = sf["tm11"]
            row["Lives_In_Jeopardy"] = _fmt_num(sf.get("lij"))
        if parsed["order"]:
            notes.append("no Last_Run_Snapshot.json (comparison with the last run off) - "
                         "classes from the summary")
    # NID
    nid = read_location_nid(folder) or (log["settings"].get("subject_dam_nid") or "").strip() \
        or sf.get("nid") or ""
    if nid and nid.lower() not in ("none", "''", '""'):
        row["NID_ID"] = nid
    # last run
    row["Last_Run"] = log.get("finished") or (snap or {}).get("time") or \
        parsed["header"].get("generated") or MISSING
    row["Plugin_Version"] = log.get("version") or parsed["header"].get("version") or MISSING
    if log.get("failed"):
        row["Run_Status"] = "FAILED"
        notes.append("the last run FAILED ({}) - the results shown are from the run of "
                     "{}".format(log.get("finished") or "?", (snap or {}).get("time") or "?"))
    elif row["Run_Status"] == MISSING:
        row["Run_Status"] = "done"
    # data vintage
    d, src = vintage_oldest(qa)
    if d:
        row["Oldest_Data_Date"], row["Oldest_Data_Source"] = d, src
    # review
    rv = (snap or {}).get("review")
    if isinstance(rv, dict) and rv.get("open") is not None:
        row["Open_Review_Items"] = str(rv.get("open"))
    else:
        path = structures_csv(out, (snap or {}).get("governing"))
        got = open_review_from_csv(path) if path else None
        if got is not None:
            row["Open_Review_Items"] = str(got[0])
            notes.append("open review items estimated from {} (the run does not record "
                         "them)".format(os.path.basename(path)))
    # hazard-creep watch
    if parent_watch is not None:
        by_folder, by_name, day = parent_watch
        w = by_folder.get(_norm(folder)) or by_name.get(name.lower())
        if w:
            row["Creep_Alert"] = w.get("Alert") or MISSING
            row["Creep_Message"] = "{}{}".format(w.get("Message") or "",
                                                 " (watch of {})".format(day) if day else "")
    # dams in series
    txt = _series_text(parent_series, name, folder) if parent_series else ""
    if not txt:
        own = read_csv_rows(os.path.join(qa, SERIES_CSV))
        if own:
            txt = _series_text(own, name, folder)
    if txt:
        row["Series_Flags"] = txt
    # optional analyses
    if sf.get("sensitivity"):
        row["Sensitivity"] = sf["sensitivity"]
    term = read_csv_rows(os.path.join(qa, TERMINATION_CSV))
    if term:
        t = term[0]
        rm = (t.get("River_Mile") or "").strip()
        row["Termination"] = "{}{}".format(t.get("Status") or "?",
                                           " - river mile {}".format(rm) if rm else "")
    elif sf.get("termination_line"):
        row["Termination"] = sf["termination_line"].split(": ", 1)[-1][:120]
    # v2.10: dam.json, Screening, Runner, next action
    notes += suite_columns(row, folder, snap, log, parsed, screen_index, today)
    row["Notes"] = "; ".join(notes) or ""
    return row


def _rank(v):
    return CLASS_RANK.get(str(v or "").strip().lower(), 0)


def _sort_key(r):
    """Dams needing an action first, then by class (ND, TM 11, the runner's
    preview, the screening class), then by name."""
    return (0 if r.get("Next_Action") != "none" else 1,
            -_rank(r.get("ND_Class")), -_rank(r.get("TM11_Class")),
            -_rank(r.get("Runner_Class_Preview")), -_rank(r.get("Screen_Class")),
            str(r.get("Dam")).lower())


def screening_sources(parent_folder, screening=None):
    """The Screening sources to read: `screening` (a folder or file, or a list of
    them) as given; None = found in / directly inside the parent (find_screening);
    False or "" = none."""
    if screening is None:
        return find_screening(parent_folder)
    if not screening:
        return []
    if isinstance(screening, (list, tuple)):
        return [s for s in screening if s]
    return [screening]


def collect(parent_folder, screening=None, today=None):
    """[row] for the dam folders of `parent_folder`: dams needing an action first,
    then highest class first. `screening`: see screening_sources(). A folder
    directly inside the parent that holds the Screening results is not a dam."""
    if not parent_folder or not os.path.isdir(parent_folder):
        raise ValueError("Folder not found: {}".format(parent_folder))
    try:
        watch = _watch_rows(parent_folder)
    except Exception:
        watch = None
    series = read_csv_rows(os.path.join(parent_folder, SERIES_CSV))
    try:
        sidx = screening_index(screening_sources(parent_folder, screening))
    except Exception:                             # the Screening part never stops the table
        sidx = None
    skip = {_norm(f) for f in (sidx or {}).get("folders", [])}
    rows = []
    for folder in _dam_folders(parent_folder):
        if _norm(folder) in skip:
            continue
        try:
            rows.append(dam_row(folder, watch, series, sidx, today))
        except Exception as e:                    # one odd folder never stops the table
            row = dict.fromkeys(PORTFOLIO_FIELDS, MISSING)
            row.update({"Dam": os.path.basename(folder), "Folder": folder,
                        "Notes": "could not be read: {}".format(e)})
            rows.append(row)
    rows.sort(key=_sort_key)
    return rows


# ================================================================ writing ==
def _free_path(path):
    """`path`, or a time-stamped name when the old file cannot be replaced
    (open in Excel / a browser on Windows)."""
    if not os.path.exists(path):
        return path
    try:
        os.remove(path)
        return path
    except OSError:
        base, ext = os.path.splitext(path)
        return "{}_{}{}".format(base, time.strftime("%Y%m%d_%H%M%S"), ext)


_CHIP = {3: ("#c62828", "#fff"), 2: ("#ef6c00", "#fff"), 1: ("#2e7d32", "#fff")}
_ALERT = {"FAILED": "#b71c1c", "CLASS UP": "#b71c1c", "CLASS CHANGE": "#e65100",
          "CREEP": "#f9a825", "REVIEW": "#fdd835", "BASELINE": "#90caf9"}
_ACTION = {"screen": "#6a1b9a", "model": "#1565c0", "classify": "#00838f",
           "review": "#f9a825", "refresh": "#8d6e63", "none": "#9e9e9e"}
_COLS = [("Dam", "Dam", "text"), ("Next_Action", "Next action", "action"),
         ("NID_ID", "NID ID", "text"), ("Dam_ID", "Dam ID", "text"),
         ("ND_Class", "ND class", "rank"),
         ("TM11_Class", "TM 11 class", "rank"),
         ("Screen_Class", "Screening class", "rank"), ("Screen_Triage", "Triage", "text"),
         ("Runner_Class_Preview", "Runner class (preview)", "rank"),
         ("Model_Date", "Breach model", "text"), ("Suite_Status", "Suite status", "text"),
         ("Height_ft", "Height ft", "num"), ("Storage_acft", "Storage ac-ft", "num"),
         ("Lives_In_Jeopardy", "Lives in jeopardy", "num"),
         ("Flooded_Habitable", "Flooded habitable", "num"), ("Last_Run", "Last run", "text"),
         ("Run_Status", "Status", "text"), ("Plugin_Version", "Plugin", "text"),
         ("Oldest_Data_Date", "Oldest data", "text"),
         ("Open_Review_Items", "Open review items", "num"),
         ("Record_Conflicts", "dam.json conflicts", "num"),
         ("Creep_Alert", "Creep alert", "text"), ("Series_Flags", "Dams in series", "text"),
         ("Sensitivity", "Sensitivity", "text"), ("Termination", "Termination", "text"),
         ("Notes", "Notes", "text")]
_SMALL = ("Notes", "Series_Flags", "Sensitivity", "Termination", "Creep_Message",
          "Suite_Status")

_JS = """
(function(){
  var table = document.getElementById('pf');
  var heads = table.tHead.rows[0].cells;
  function key(td){ var s = td.getAttribute('data-sort');
    return (s === null ? td.textContent : s).trim(); }
  function missing(s){ return s === '' || s === '-'; }
  function num(s){ return /^-?\\d+(\\.\\d+)?$/.test(s) ? parseFloat(s) : NaN; }
  function sortBy(i){
    var th = heads[i], asc = th.getAttribute('data-dir') !== 'asc';
    for (var h = 0; h < heads.length; h++) { heads[h].removeAttribute('data-dir'); }
    th.setAttribute('data-dir', asc ? 'asc' : 'desc');
    var body = table.tBodies[0], rows = Array.prototype.slice.call(body.rows);
    rows.sort(function(a, b){
      var x = key(a.cells[i]), y = key(b.cells[i]);
      if (missing(x) !== missing(y)) { return missing(x) ? 1 : -1; }
      var nx = num(x), ny = num(y), c;
      if (!isNaN(nx) && !isNaN(ny)) { c = nx - ny; }
      else { c = x.localeCompare(y, undefined, {numeric: true, sensitivity: 'base'}); }
      return asc ? c : -c;
    });
    for (var r = 0; r < rows.length; r++) { body.appendChild(rows[r]); }
  }
  for (var i = 0; i < heads.length; i++) {
    (function(k){ heads[k].addEventListener('click', function(){ sortBy(k); }); })(i);
  }
})();
"""

_CSS = """
body{font-family:Segoe UI,Arial,sans-serif;margin:16px;color:#222;background:#fff}
h1{font-size:20px;margin:0 0 4px}
p.meta{color:#555;margin:2px 0 10px;font-size:13px}
table{border-collapse:collapse;font-size:13px}
th,td{border:1px solid #ccc;padding:4px 8px;vertical-align:top;text-align:left}
th{background:#eceff1;cursor:pointer;user-select:none;white-space:nowrap}
th[data-dir=asc]:after{content:' \\25B2'} th[data-dir=desc]:after{content:' \\25BC'}
tbody tr:nth-child(even){background:#fafafa}
.chip{display:inline-block;padding:1px 8px;border-radius:9px;font-weight:600;font-size:12px}
.small{font-size:12px;color:#444;max-width:360px}
.legend span{margin-right:10px}
"""


def _cell(key, value, kind):
    v = "" if value is None else str(value)
    esc = html.escape(v, quote=True)
    if kind == "rank":
        rk = _rank(v)
        sort = ' data-sort="{}"'.format(rk if rk else "-")
        if rk:
            bg, fg = _CHIP[rk]
            return '<td{}><span class="chip" style="background:{};color:{}">{}</span></td>'.format(
                sort, bg, fg, esc.upper())
        return "<td{}>{}</td>".format(sort, esc)
    if kind == "action":
        if v in _ACTION:
            return ('<td data-sort="{}"><span class="chip" style="background:{};color:#fff">'
                    '{}</span></td>').format(ACTION_ORDER.index(v) + 1, _ACTION[v],
                                             esc.upper())
        return '<td data-sort="-">{}</td>'.format(esc)
    if key == "Creep_Alert" and v in _ALERT:
        return '<td><span class="chip" style="background:{}">{}</span></td>'.format(
            _ALERT[v], esc)
    cls = ' class="small"' if key in _SMALL else ""
    return "<td{}>{}</td>".format(cls, esc)


def _link(target, page_folder):
    """A link to a local file from a page in `page_folder` (relative when possible)."""
    try:
        rel = os.path.relpath(target, page_folder)
        return rel.replace(os.sep, "/")
    except ValueError:                           # another drive on Windows
        import pathlib
        return pathlib.Path(os.path.abspath(target)).as_uri()


def _with_small(cell, lines):
    lines = [str(x) for x in lines if x not in (None, "", MISSING)]
    if not lines:
        return cell
    return cell[:-5] + "".join('<div class="small">{}</div>'.format(html.escape(x, quote=True))
                               for x in lines) + "</td>"


def _screening_meta(sources, page_folder):
    """The page's line about the Screening results read (with links to its index.html)."""
    if not sources:
        return ("<p class='meta'>No ND Dam Breach Screening results were found (a folder in or "
                "directly inside this folder holding its summary.csv, e.g. '_screening'): "
                "the screening columns are '-'.</p>")
    bits = []
    for src in sources:
        folder = src if os.path.isdir(src) else os.path.dirname(src)
        idx = os.path.join(folder, SCREEN_INDEX)
        if os.path.isfile(idx):
            bits.append("<a href=\"{}\">{}</a>".format(
                html.escape(_link(idx, page_folder), quote=True), html.escape(idx)))
        else:
            bits.append(html.escape(src))
    return ("<p class='meta'>ND Dam Breach Screening results read from: {}. The screening "
            "and runner classes are the other plugins' screening previews; the ND class is "
            "this plugin's result.</p>").format("; ".join(bits))


def render_html(rows, parent_folder, when=None, screening=None, page_folder=None):
    """The dashboard page (text). `screening`: the Screening sources read (for the
    links on the page); `page_folder`: where the page is written (relative links)."""
    when = when or time.strftime("%Y-%m-%d %H:%M")
    page_folder = page_folder or parent_folder
    counts = {}
    for r in rows:
        k = str(r.get("ND_Class") or MISSING)
        counts[k] = counts.get(k, 0) + 1
    acts = {}
    for r in rows:
        k = str(r.get("Next_Action") or MISSING)
        acts[k] = acts.get(k, 0) + 1
    act_text = ", ".join("{} {}".format(acts[k], k) for k in ACTION_ORDER + (MISSING,)
                         if k in acts) or "none"
    title = "Dam portfolio - {}".format(os.path.basename(os.path.normpath(parent_folder)))
    out = ["<!DOCTYPE html>", "<html lang='en'><head><meta charset='utf-8'>",
           "<meta name='viewport' content='width=device-width, initial-scale=1'>",
           "<title>{}</title>".format(html.escape(title)),
           "<style>{}</style></head><body>".format(_CSS),
           "<h1>{}</h1>".format(html.escape(title)),
           "<p class='meta'>Generated {} by {} v{} from the files of each dam's last run in "
           "{}. {} dam(s): {}.</p>".format(
               html.escape(when), html.escape(PLUGIN_NAME), html.escape(PLUGIN_VERSION),
               html.escape(parent_folder), len(rows),
               html.escape(", ".join("{} {}".format(v, k) for k, v in sorted(
                   counts.items(), key=lambda kv: -_rank(kv[0]))) or "none")),
           "<p class='meta'>The classes are the plugin's SCREENING results as last run - "
           "not hazard classifications until the engineer of record adopts them. '-' = not "
           "recorded or not run. Click a column heading to sort.</p>",
           "<p class='meta'>Next action (dams needing one are listed first): {}. It is "
           "worked out from dam.json and the files of the three plugins (screen, model, "
           "classify, review, refresh data older than a year, none) - a reminder, not a "
           "requirement.</p>".format(html.escape(act_text)),
           _screening_meta(screening, page_folder),
           "<table id='pf'><thead><tr>" + "".join(
               "<th>{}</th>".format(html.escape(label)) for _k, label, _t in _COLS)
           + "</tr></thead><tbody>"]
    for r in rows:
        cells = []
        for key, _label, kind in _COLS:
            val = r.get(key)
            if key == "Creep_Alert" and r.get("Creep_Message") not in (None, "", MISSING):
                cells.append(_cell(key, val, kind)[:-5] + '<div class="small">{}</div></td>'.format(
                    html.escape(str(r["Creep_Message"]), quote=True)))
                continue
            if key == "Oldest_Data_Date" and r.get("Oldest_Data_Source") not in (None, "",
                                                                                 MISSING):
                cells.append('<td title="{}">{}</td>'.format(
                    html.escape(str(r["Oldest_Data_Source"]), quote=True),
                    html.escape(str(val), quote=True)))
                continue
            if key == "Next_Action":
                cells.append(_with_small(_cell(key, val, kind), [r.get("Next_Action_Text")]))
                continue
            if key == "Screen_Class":
                extra = []
                if r.get("Screen_Border") not in (None, "", MISSING, "no"):
                    extra.append("borderline: {}".format(r["Screen_Border"]))
                if r.get("Screen_Gaps") not in (None, "", MISSING):
                    extra.append("data gaps: {}".format(r["Screen_Gaps"]))
                if r.get("Screen_Review") not in (None, "", MISSING):
                    extra.append("review: {}".format(r["Screen_Review"]))
                if r.get("Screen_Date") not in (None, "", MISSING):
                    extra.append("results of {}".format(r["Screen_Date"]))
                cells.append(_with_small(_cell(key, val, kind), extra))
                continue
            if key == "Runner_Class_Preview":
                extra = ["preview of {}".format(r["Runner_Preview_Date"])] if r.get(
                    "Runner_Preview_Date") not in (None, "", MISSING) else []
                cells.append(_with_small(_cell(key, val, kind), extra))
                continue
            if key == "Model_Date":
                cells.append(_with_small(_cell(key, val, kind), [
                    r.get("Model_Tool"), "manifest: {}".format(r["Model_Manifest"])
                    if r.get("Model_Manifest") not in (None, "", MISSING) else ""]))
                continue
            if key == "Dam":
                cells.append('<td title="{}"><b>{}</b></td>'.format(
                    html.escape(str(r.get("Folder") or ""), quote=True),
                    html.escape(str(val), quote=True)))
                continue
            cells.append(_cell(key, val, kind))
        out.append("<tr>" + "".join(cells) + "</tr>")
    out += ["</tbody></table>",
            "<p class='meta legend'><span class='chip' style='background:#c62828;color:#fff'>"
            "HIGH</span><span class='chip' style='background:#ef6c00;color:#fff'>SIGNIFICANT / "
            "MEDIUM</span><span class='chip' style='background:#2e7d32;color:#fff'>LOW</span>"
            "</p>",
            "<p class='meta legend'>" + "".join(
                "<span class='chip' style='background:{};color:#fff'>{}</span>".format(
                    _ACTION[k], k.upper()) for k in ACTION_ORDER) + "</p>",
            "<script>{}</script>".format(_JS), "</body></html>"]
    return "\n".join(out) + "\n"


def build(parent_folder, out_path=None, screening=None, today=None):
    """Write the dashboard. Returns (html_path, csv_path, rows). `out_path`:
    the HTML file (default <parent>/Dam_Portfolio.html); the CSV is written
    next to it with the same name and .csv. `screening`: the ND Dam Breach
    Screening results (see screening_sources(); None = found automatically).
    Raises ValueError when `parent_folder` does not exist; a dam whose files
    are missing or unreadable is a row of "-"."""
    if not parent_folder or not os.path.isdir(parent_folder):
        raise ValueError("Folder not found: {}".format(parent_folder))
    sources = screening_sources(parent_folder, screening)
    rows = collect(parent_folder, sources if sources else False, today)
    hpath = out_path or os.path.join(parent_folder, HTML_FILE)
    if not hpath.lower().endswith((".html", ".htm")):
        hpath += ".html"
    folder = os.path.dirname(os.path.abspath(hpath))
    os.makedirs(folder, exist_ok=True)
    cpath = _free_path(os.path.splitext(hpath)[0] + ".csv")
    hpath = _free_path(hpath)
    with open(cpath, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=PORTFOLIO_FIELDS)
        w.writeheader()
        w.writerows(rows)
    with open(hpath, "w", encoding="utf-8") as fh:
        fh.write(render_html(rows, parent_folder, screening=sources, page_folder=folder))
    return hpath, cpath, rows
