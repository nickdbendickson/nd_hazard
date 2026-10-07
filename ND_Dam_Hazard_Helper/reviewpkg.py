# -*- coding: utf-8 -*-
"""v2.10: the reviewer's view of a submittal package.

review_package(package, work_folder, reproduce=False, reviewer="") takes a
submittal package (the folder submittal.build made, or its .zip) and, without
writing anything into it, produces in the WORK folder:

  Review_Report.html      plain, printable page: the checklist with the automatic
                          findings, the package file check, the model chain, the
                          reproduction (every text HTML-escaped)
  Review_Checklist.csv    the same checklist for the reviewer to fill in: an EMPTY
                          Reviewer_Decision column (Accept / Question / Not
                          applicable) and Reviewer_Comment. An existing checklist
                          with decisions in it is never overwritten (the new one
                          gets a time stamp).
  Review_Files_Check.csv  every file of the package against MANIFEST.csv (SHA-256):
                          OK / MISSING / CHANGED / EXTRA (not listed) / INDEX (the
                          README, cover letter and the manifest itself are written
                          after the manifest and not hashed - by design)
  Model_Chain_Check.csv / .txt   modelchain.chain_check of the packaged record
                          against the packaged Runner manifest
  reproduction/           (reproduce=True) reproduce.reproduce into this sub-
                          folder: Reproduction_Check.csv / .txt
  _package/               (a .zip) the package unzipped (reproduce.extract_package)

The steps:
  1. every file against its MANIFEST.csv SHA-256 (missing / changed / extra);
  2. the model chain (item 13) from the packaged files only (the Runner manifest
     copied into 02_Method_and_record, the record's fingerprints) - the
     reviewer's machine need not have the dam folder;
  3. optionally the reproduction (reproduce.reproduce; needs the QGIS pipeline,
     `run` injectable as in reproduce.reproduce);
  4. the checklist, pre-filled from the package: result, plugin version,
     settings profile, data packaged for reproduction, open review items, data
     vintage (oldest data), model boundary check, model gate (Model_Gate.csv
     when the package holds one), raster units, dam.json values and conflicts,
     chain status, reproduction status.

THE CHECKLIST ITEMS ARE THIS TOOL'S OWN CHECKS OF THE PACKAGE. They are not the
State's review criteria and not a list of regulatory requirements; each row says
so (Check_Type). The one number used as a flag threshold here is
REFRESH_DAYS (365 days of data age, the ND dam suite dashboard's "refresh"
screening parameter chosen by the tool - not a regulatory limit).

read_checklist() / summarize_checklist() re-read a filled checklist and count
the decisions (Accept / Question / Not applicable / no decision / not
recognised).

Nothing here needs QGIS except the optional reproduction. Nothing has been run
in a real QGIS.
"""

import csv
import datetime
import html
import os
import re
import time
import zipfile

from .constants import PLUGIN_NAME, PLUGIN_VERSION
from . import modelchain as MC

REPORT_HTML = "Review_Report.html"
CHECKLIST_CSV = "Review_Checklist.csv"
FILES_CSV = "Review_Files_Check.csv"
REPRO_DIR = "reproduction"
RECORD_DIR = "02_Method_and_record"
MANIFEST_NAME = "MANIFEST.csv"                     # submittal.MANIFEST_NAME
INDEX_FILES = ("MANIFEST.csv", "README.txt", "Cover_Letter_TEMPLATE.txt")
DECISIONS = ("Accept", "Question", "Not applicable")
NO_DECISION = "No decision"
NOT_RECOGNISED = "Not recognised"
CHECK_TYPE = "Tool check (not a regulatory requirement)"
CHECKLIST_FIELDS = ["Item_ID", "Item", "Check_Type", "Automatic_Finding", "Flag", "Source",
                    "Reviewer_Decision", "Reviewer_Comment", "Reviewer"]
FILES_FIELDS = ["Path", "Status", "Manifest_SHA256", "Actual_SHA256", "Size_bytes",
                "Description"]
FLAGS = ("OK", "CHECK", "MISSING", "INFO", "NOT RUN")
# data older than this at the review gets the flag CHECK: the ND dam suite dashboard's
# "refresh" screening parameter (damsuite.dam_record.next_action) - chosen by the tool,
# NOT a regulatory limit
REFRESH_DAYS = 365
_DECISION_WORDS = {"accept": "Accept", "accepted": "Accept", "ok": "Accept",
                   "question": "Question", "questioned": "Question", "q": "Question",
                   "not applicable": "Not applicable", "n/a": "Not applicable",
                   "na": "Not applicable", "not-applicable": "Not applicable"}
_UNIT_RE = re.compile(r"check the units|look like water-surface|look like minutes|"
                      r"expects hours|expects feet|expects ft/s|elevations \(median", re.I)
_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")


# ----------------------------------------------------------------- helpers --
def _say(log, msg):
    if log:
        try:
            log(msg)
        except Exception:
            pass


def _tick(progress, pct, msg=""):
    if progress:
        progress(pct, msg)


def _norm(p):
    return os.path.normcase(os.path.abspath(p))


def _inside(child, parent):
    c, p = _norm(child), _norm(parent)
    return c == p or c.startswith(p.rstrip(os.sep) + os.sep)


def _read_text(path):
    for enc in ("utf-8-sig", "cp1252"):
        try:
            with open(path, encoding=enc) as fh:
                return fh.read()
        except UnicodeDecodeError:
            continue
        except OSError:
            return ""
    return ""


def _csv_rows(path):
    """[dict] of a CSV (',' or ';', UTF-8 with / without BOM, else cp1252); [] when
    missing, empty or unreadable."""
    text = _read_text(path) if path and os.path.isfile(path) else ""
    if not text.strip():
        return []
    first = text.splitlines()[0]
    delim = ";" if first.count(";") > first.count(",") else ","
    try:
        return [{(k or "").strip(): (v if v is not None else "") for k, v in r.items()}
                for r in csv.DictReader(text.splitlines(), delimiter=delim)]
    except csv.Error:
        return []


def _pkg(folder, *parts):
    return os.path.join(folder, *parts)


def _newest(folder, prefix, ext):
    try:
        hits = sorted(f for f in os.listdir(folder)
                      if f.startswith(prefix) and f.lower().endswith(ext))
    except OSError:
        return None
    return os.path.join(folder, hits[-1]) if hits else None


def _free_path(path):
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    return "{}_{}{}".format(base, time.strftime("%Y%m%d_%H%M%S"), ext)


# ----------------------------------------------------------------- package --
def open_package(package, work_folder):
    """(package folder, from_zip) - a .zip is unzipped into <work>/_package
    (reproduce.extract_package: unsafe member paths refused). Raises ValueError for a
    missing package, a work folder inside the package, or a folder that is no package."""
    from . import reproduce as RP
    if not package or not os.path.exists(package):
        raise ValueError("Package not found: {}".format(package))
    if not work_folder:
        raise ValueError("Choose a work folder for the review")
    package = os.path.abspath(package)
    work_folder = os.path.abspath(work_folder)
    if os.path.isfile(package):
        if not zipfile.is_zipfile(package):
            raise ValueError("{} is neither a package folder nor a .zip".format(package))
        os.makedirs(work_folder, exist_ok=True)
        folder, from_zip = RP.extract_package(package, work_folder), True
    else:
        if _inside(work_folder, package):
            raise ValueError("Choose a work folder outside the package - the package must "
                             "stay as it was made")
        folder, from_zip = package, False
    if not any(os.path.exists(_pkg(folder, n)) for n in (MANIFEST_NAME, "README.txt",
                                                         RECORD_DIR,
                                                         RP.PACKAGE_SETTINGS_FILE)):
        raise ValueError("{} is not a submittal package (no MANIFEST.csv, README.txt or "
                         "{}/)".format(package, RECORD_DIR))
    return folder, from_zip


def verify_files(package_folder):
    """Every file against MANIFEST.csv: {"rows" (FILES_FIELDS), "counts", "problems"
    [(path, status)], "has_manifest"}. Read only."""
    from .reproduce import sha256_file
    mpath = _pkg(package_folder, MANIFEST_NAME)
    rows, listed = [], set()
    has = os.path.isfile(mpath)
    manifest = []
    if has:
        try:
            with open(mpath, newline="", encoding="utf-8-sig") as fh:
                manifest = list(csv.DictReader(fh))
        except (OSError, csv.Error):
            manifest = []
            has = False
    for r in manifest:
        rel = (r.get("Path") or "").replace("\\", "/").strip()
        if not rel:
            continue
        parts = rel.split("/")
        if rel.startswith("/") or ".." in parts or re.match(r"^[A-Za-z]:", rel):
            rows.append({"Path": rel, "Status": "UNSAFE PATH (not followed)",
                         "Manifest_SHA256": r.get("SHA256") or "", "Actual_SHA256": "",
                         "Size_bytes": "", "Description": r.get("Description") or ""})
            continue
        listed.add(rel)
        full = _pkg(package_folder, *parts)
        want = (r.get("SHA256") or "").strip().lower()
        if not os.path.isfile(full):
            st, have, size = "MISSING", "", ""
        else:
            have = sha256_file(full, limit=0) or ""
            size = os.path.getsize(full)
            st = "OK" if not want or have == want else "CHANGED"
            if not want:
                st = "OK (no SHA-256 in the manifest)"
        rows.append({"Path": rel, "Status": st, "Manifest_SHA256": want, "Actual_SHA256": have,
                     "Size_bytes": size, "Description": r.get("Description") or ""})
    for dp, dirs, files in os.walk(package_folder):
        dirs.sort()
        for f in sorted(files):
            rel = os.path.relpath(os.path.join(dp, f), package_folder).replace(os.sep, "/")
            if rel in listed:
                continue
            if rel in INDEX_FILES:
                st = "INDEX (written with the manifest, not hashed by design)"
            else:
                st = "EXTRA (not in MANIFEST.csv)"
            full = os.path.join(dp, f)
            rows.append({"Path": rel, "Status": st, "Manifest_SHA256": "",
                         "Actual_SHA256": sha256_file(full, limit=0) or "",
                         "Size_bytes": os.path.getsize(full), "Description": ""})
    counts = {}
    for r in rows:
        key = r["Status"].split(" ")[0]
        counts[key] = counts.get(key, 0) + 1
    problems = [(r["Path"], r["Status"]) for r in rows
                if r["Status"].split(" ")[0] in ("MISSING", "CHANGED", "EXTRA", "UNSAFE")]
    if not has:
        problems.insert(0, (MANIFEST_NAME, "missing or unreadable - the files cannot be "
                                           "verified"))
    return {"rows": rows, "counts": counts, "problems": problems, "has_manifest": has}


# --------------------------------------------------------------- checklist --
def _item(iid, item, finding, flag, source):
    return {"Item_ID": iid, "Item": item, "Check_Type": CHECK_TYPE,
            "Automatic_Finding": finding, "Flag": flag, "Source": source,
            "Reviewer_Decision": "", "Reviewer_Comment": "", "Reviewer": ""}


def _load_context(folder):
    """What the checklist reads from the package (every part may be missing)."""
    from . import history as HIS
    from . import reproduce as RP
    rd = _pkg(folder, RECORD_DIR)
    ctx = {"folder": folder, "record_dir": rd, "settings": None, "settings_file": "",
           "settings_error": "", "snapshot": HIS.read_snapshot(rd), "readme": "",
           "run_log": "", "run_log_file": ""}
    try:
        rec, spath, _packaged = RP._find_settings(folder)
        ctx["settings"], ctx["settings_file"] = rec, os.path.relpath(spath, folder).replace(
            os.sep, "/")
    except ValueError as e:
        ctx["settings_error"] = str(e)
    ctx["readme"] = _read_text(_pkg(folder, "README.txt"))
    log = _newest(rd, "Hazard_Classification_Run_Log", ".txt")
    if log:
        ctx["run_log"] = _read_text(log)
        ctx["run_log_file"] = RECORD_DIR + "/" + os.path.basename(log)
    return ctx


def _c_files(files):
    c = files["counts"]
    if not files["has_manifest"]:
        return _item("C01", "Package files against MANIFEST.csv (SHA-256)",
                     "MANIFEST.csv is missing or unreadable - the files cannot be verified",
                     "CHECK", MANIFEST_NAME)
    n_ok = c.get("OK", 0)
    bad = files["problems"]
    finding = "{} file(s) listed, {} as made".format(
        sum(v for k, v in c.items() if k not in ("EXTRA", "INDEX")), n_ok)
    if bad:
        finding += "; {} problem(s): {}".format(len(bad), "; ".join(
            "{} {}".format(p, s.split(" (")[0]) for p, s in bad[:8]) + (" ..." if len(bad) > 8
                                                                     else ""))
    else:
        finding += "; no file missing, changed or added"
    return _item("C01", "Package files against MANIFEST.csv (SHA-256)", finding,
                 "CHECK" if bad else "OK", MANIFEST_NAME + " / " + FILES_CSV)


def _c_result(ctx):
    s = ctx["snapshot"]
    if not s:
        return _item("C02", "Result as packaged", "no Last_Run_Snapshot.json in the package "
                     "(the run had 'compare with the last run' off) - see the summary in "
                     "01_Report", "MISSING", RECORD_DIR + "/Last_Run_Snapshot.json")
    return _item("C02", "Result as packaged",
                 "run of {}: ND {} / TM 11 {} (lives in jeopardy {}), governing scenario {}"
                 "; {} flooded / cut-off habitable structure(s) in the snapshot".format(
                     s.get("time") or "?", s.get("nd") or "?", s.get("tm11") or "?",
                     s.get("lij") if s.get("lij") is not None else "?",
                     s.get("governing") or "?",
                     sum(1 for x in s.get("structures") or [] if x.get("habitable") == "Yes")),
                 "INFO", RECORD_DIR + "/Last_Run_Snapshot.json")


def _c_version(ctx):
    from .portfolio import parse_run_log
    rec = ctx["settings"] or {}
    run_v = rec.get("plugin_version") or parse_run_log(ctx["run_log"]).get("version") or ""
    m = re.search(r"by .*? v(\d+(?:\.\d+)+)", ctx["readme"].splitlines()[1]
                  if len(ctx["readme"].splitlines()) > 1 else "")
    pkg_v = m.group(1) if m else ""
    parts = ["run made with {} v{}".format(PLUGIN_NAME, run_v) if run_v else
             "the plugin version of the run is not recorded"]
    if pkg_v:
        parts.append("package made with v{}".format(pkg_v))
    parts.append("this review made with v{}".format(PLUGIN_VERSION))
    flag = "INFO"
    if run_v and run_v != PLUGIN_VERSION:
        parts.append("a reproduction with this version may differ where the method changed "
                     "(see the CHANGES files)")
    if not run_v:
        flag = "MISSING"
    return _item("C03", "Plugin version", "; ".join(parts), flag,
                 ctx["settings_file"] or ctx["run_log_file"] or "README.txt")


def _c_profile(ctx):
    rec = ctx["settings"] or {}
    prof = rec.get("settings_profile")
    files = [f for f in os.listdir(ctx["record_dir"]) if f.startswith("Settings_Profile_")] \
        if os.path.isdir(ctx["record_dir"]) else []
    if not isinstance(prof, dict):
        if ctx["settings"] is None:
            return _item("C04", "Settings profile", "not recorded (no run settings in the "
                         "package)", "MISSING", "README.txt")
        return _item("C04", "Settings profile", "no settings profile recorded: the run used "
                     "the settings of the dialog / script (all of them are in {})".format(
                         ctx["settings_file"]), "INFO", ctx["settings_file"])
    finding = "profile '{}' (hash {})".format(prof.get("name") or "?",
                                              (prof.get("sha256") or "?")[:16])
    flag = "INFO"
    if prof.get("modified"):
        finding += "; the settings were CHANGED after the profile was loaded: {}".format(
            ", ".join(prof.get("changed_keys") or []) or "?")
        flag = "CHECK"
    finding += "; profile file {}in the package".format(
        "" if files else "NOT ")
    return _item("C04", "Settings profile", finding, flag,
                 ctx["settings_file"] + ("; " + RECORD_DIR + "/" + files[0] if files else ""))


def _c_data(ctx):
    rec = ctx["settings"]
    if rec is None:
        return _item("C05", "Data packaged for reproduction",
                     "no run settings in the package: {}".format(ctx["settings_error"]),
                     "MISSING", "Run_Settings_package.json")
    pk = rec.get("package")
    if not isinstance(pk, dict):
        return _item("C05", "Data packaged for reproduction", "the package holds the run "
                     "settings but no copies of the data (made without 'include data'): a "
                     "reproduction needs the original files", "INFO", ctx["settings_file"])
    np_ = [n for n in pk.get("not_packaged") or []]
    ch = pk.get("changed_since_run") or []
    parts = ["{} file(s) packaged{}".format(len(pk.get("files") or {}),
                                            ", flood rasters included" if
                                            pk.get("include_rasters") else
                                            ", flood rasters NOT included")]
    if np_:
        parts.append("not packaged: " + "; ".join("{} ({})".format(n.get("what"), n.get("why"))
                                                   for n in np_[:6]) +
                     (" ..." if len(np_) > 6 else ""))
    if ch:
        parts.append("changed on disk between the run and the package: " +
                     "; ".join(str(c.get("what")) for c in ch))
    real = [n for n in np_ if "rasters not included" not in str(n.get("why"))]
    return _item("C05", "Data packaged for reproduction", "; ".join(parts),
                 "CHECK" if real or ch else "OK", ctx["settings_file"])


def _c_review(ctx):
    s = ctx["snapshot"] or {}
    rv = s.get("review")
    src = RECORD_DIR + "/Last_Run_Snapshot.json"
    decisions = os.path.isfile(_pkg(ctx["record_dir"], "Structure_Review_Decisions.csv"))
    tail = "; engineer review decisions {}in the package".format("" if decisions else "NOT ")
    if isinstance(rv, dict) and rv.get("open") is not None:
        n = int(rv.get("open") or 0)
        return _item("C06", "Open review items",
                     "{} open, {} reviewed of {} listed by the run{}".format(
                         n, rv.get("reviewed", "?"), rv.get("listed", "?"), tail),
                     "CHECK" if n else "OK", src)
    tables = _pkg(ctx["folder"], "03_Results_tables")
    gov = re.sub(r"[^A-Za-z0-9]+", "_", str(s.get("governing") or "")).strip("_")[:40]
    cands = ["Habitable_Structures_{}.csv".format(gov)] if gov else []
    cands.append("Habitable_Structures.csv")
    for c in cands:
        p = _pkg(tables, c)
        if os.path.isfile(p):
            try:
                from .portfolio import open_review_from_csv
                got = open_review_from_csv(p)
            except Exception:
                got = None
            if got is not None:
                return _item("C06", "Open review items",
                             "{} open, {} reviewed - ESTIMATED from {} (the run did not "
                             "record them){}".format(got[0], got[1], c, tail),
                             "CHECK" if got[0] else "OK", "03_Results_tables/" + c)
    return _item("C06", "Open review items", "not recorded by the run and no readable "
                 "structures table in the package{}".format(tail), "MISSING", src)


def _c_vintage(ctx, today=None):
    path = _pkg(ctx["record_dir"], "Data_Vintage.csv")
    rows = _csv_rows(path)
    src = RECORD_DIR + "/Data_Vintage.csv"
    if not rows:
        return _item("C07", "Data vintage (oldest data)", "no Data_Vintage.csv in the package "
                     "(a run before v2.8, or it was not written)", "MISSING", src)
    best = None
    for r in rows:
        if (r.get("Source") or "").strip().lower() == "plugin":
            continue
        dates = _DATE_RE.findall(r.get("Date") or "")
        if dates and (best is None or min(dates) < best[0]):
            best = (min(dates), "{}: {}".format(r.get("Source") or "?", r.get("Detail") or ""))
    if best is None:
        return _item("C07", "Data vintage (oldest data)", "{} row(s), none dated besides the "
                     "plugin's own".format(len(rows)), "INFO", src)
    today = today or datetime.date.today()
    try:
        age = (today - datetime.date(*map(int, best[0].split("-")))).days
    except ValueError:
        age = None
    finding = "oldest: {} ({})".format(best[0], best[1])
    flag = "INFO"
    if age is not None:
        finding += "; {} day(s) old at this review".format(age)
        if age > REFRESH_DAYS:
            finding += (" - older than {} days (the ND dam suite dashboard's 'refresh' "
                        "screening parameter, not a regulatory limit)".format(REFRESH_DAYS))
            flag = "CHECK"
    return _item("C07", "Data vintage (oldest data)", finding, flag, src)


def _c_boundary(ctx):
    path = _pkg(ctx["record_dir"], "Model_Boundary_Check.csv")
    src = RECORD_DIR + "/Model_Boundary_Check.csv"
    log_lines = [ln.strip(" >") for ln in ctx["run_log"].splitlines()
                 if "reaches the edge" in ln]
    if not os.path.isfile(path):
        finding = ("no Model_Boundary_Check.csv in the package: the run found no flood at the "
                   "raster edge, or the check was not made")
        if log_lines:
            finding += "; the run log says: " + log_lines[0][:300]
            return _item("C08", "Model boundary check", finding, "CHECK", ctx["run_log_file"])
        return _item("C08", "Model boundary check", finding, "INFO", src)
    rows = _csv_rows(path)
    if not rows:
        finding = "Model_Boundary_Check.csv is empty or unreadable"
        if log_lines:
            finding += "; the run log says: " + log_lines[0][:300]
        return _item("C08", "Model boundary check", finding, "CHECK", src)
    by = {}
    for r in rows:
        by.setdefault(r.get("Scenario") or "?", []).append(r)
    parts = []
    for sc, rs in sorted(by.items()):
        n_inc = sum(1 for r in rs if (r.get("Flag") or "").startswith("incremental"))
        sides = sorted({r.get("Side") or "?" for r in rs})
        parts.append("{}: {} wet edge cell(s) ({}), {} at or above the incremental "
                     "threshold".format(sc, len(rs), ", ".join(sides), n_inc))
    return _item("C08", "Model boundary check", "flood at the raster edge - " +
                 "; ".join(parts) + ". Consequences beyond the model are not evaluated",
                 "CHECK", src)


def _gate_file(folder, files):
    for r in files["rows"]:
        b = os.path.basename(r["Path"])
        if b.lower().startswith("model_gate") and b.lower().endswith(".csv"):
            return r["Path"]
    return None


def _c_gate(ctx, files):
    rel = _gate_file(ctx["folder"], files)
    if not rel:
        return _item("C09", "Model gate", "no Model_Gate.csv in the package (the gate was not "
                     "run, or the package was made before v2.10)", "INFO",
                     RECORD_DIR + "/Model_Gate.csv")
    rows = _csv_rows(_pkg(ctx["folder"], *rel.split("/")))
    if not rows:
        return _item("C09", "Model gate", "{} is empty or unreadable".format(rel), "CHECK", rel)
    keys = {k.lower(): k for k in rows[0]}
    col = next((keys[k] for k in ("gate", "status", "result", "verdict", "decision",
                                  "outcome") if k in keys), None)
    if col is None:
        return _item("C09", "Model gate", "{}: {} row(s), no status column recognised "
                     "(columns: {})".format(rel, len(rows), ", ".join(rows[0])), "CHECK", rel)
    vals = {}
    for r in rows:
        v = (r.get(col) or "").strip() or "(empty)"
        vals[v] = vals.get(v, 0) + 1
    bad = [v for v in vals if re.search(r"fail|stop|block|reject|not ok|error", v, re.I)]
    good = all(re.match(r"^(pass|passed|ok|go|yes)\b", v, re.I) for v in vals)
    finding = "{}: {} row(s); {}: {}".format(rel, len(rows), col, ", ".join(
        "{} {}".format(v, n) for v, n in sorted(vals.items())))
    if bad:
        msg = next((keys[k] for k in ("message", "note", "reason", "text", "check")
                    if k in keys), None)
        if msg:
            finding += "; " + "; ".join(str(r.get(msg) or "") for r in rows
                                        if (r.get(col) or "").strip() in bad)[:400]
    return _item("C09", "Model gate", finding, "CHECK" if bad or not good else "OK", rel)


def _c_units(ctx):
    rec = ctx["settings"] or {}
    scen = rec.get("scenarios") or []
    given = []
    for s in scen:
        have = [k for k in ("depth", "velocity", "arrival", "dv") if s.get(k)]
        given.append("{} ({}{}): {}".format(s.get("name") or "?", s.get("role") or "breach",
                                            ", baseline " + str(s.get("baseline"))
                                            if s.get("baseline") else "",
                                            ", ".join(have) or "no raster"))
    warn = [ln.strip(" >") for ln in ctx["run_log"].splitlines() if _UNIT_RE.search(ln)]
    finding = ("the plugin reads depth in ft, velocity in ft/s and arrival time in hours "
               "(its own convention; the rasters carry no unit it can check)")
    if given:
        finding += "; rasters: " + "; ".join(given)
    if warn:
        finding += "; the run log warns: " + " | ".join(w[:200] for w in warn[:4])
        flag = "CHECK"
    else:
        finding += "; no unit warning in the run log" if ctx["run_log"] else \
            "; no run log in the package"
        flag = "INFO"
    if any(s.get("depth") and not s.get("velocity") for s in scen):
        finding += "; a scenario has no velocity raster (depth-only criterion)"
        flag = "CHECK"
    return _item("C10", "Raster units", finding, flag,
                 (ctx["settings_file"] + "; " if ctx["settings_file"] else "") +
                 (ctx["run_log_file"] or "run log"))


def _c_damjson(ctx):
    from .damsuite import dam_record as DR
    src = RECORD_DIR + "/dam.json"
    if not os.path.isfile(_pkg(ctx["record_dir"], DR.FILE_NAME)):
        return _item("C11", "dam.json values and conflicts", "no dam.json in the package (made "
                     "before v2.10, or the dam folder has no dam record)", "INFO", src)
    try:
        rec = DR.load(ctx["record_dir"])
    except ValueError as e:
        return _item("C11", "dam.json values and conflicts", "dam.json cannot be read: "
                     "{}".format(e), "CHECK", src)
    if rec is None:
        return _item("C11", "dam.json values and conflicts", "dam.json cannot be read",
                     "CHECK", src)
    vals = []
    for k in ("dam_id", "name", "nid_id", "height_ft", "storage_acft", "top_of_dam_ft",
              "top_of_dam_datum", "existing_hazard_class"):
        v = rec.get(k)
        if v not in (None, ""):
            srcs = (rec.sources.get(k) or {}).get("tool") or "?"
            vals.append("{} = {} ({})".format(k, v, srcs))
    finding = "; ".join(vals) if vals else "no values recorded"
    flag = "OK"
    open_c = [c for c in rec.conflicts if not c.get("applied")]
    if open_c:
        finding += "; {} conflict(s) not applied: {}".format(len(open_c), "; ".join(
            "{}: {} ({}) vs {} ({})".format(c.get("field"), c.get("old"),
                                            _src_tool(c.get("old_source")), c.get("new"),
                                            _src_tool(c.get("new_source")))
            for c in open_c[:6]))
        flag = "CHECK"
    applied = [c for c in rec.conflicts if c.get("applied")]
    if applied:
        finding += "; {} value(s) overwritten over another tool's value".format(len(applied))
    try:                                  # the authority's entries, not a tool's preview
        last = rec.latest("classified", include_previews=False)
    except TypeError:                     # a damsuite before include_previews
        last = next((s for s in sorted(
            (s for s in rec.status if s.get("step") == "classified" and not (
                isinstance(s.get("result"), dict) and s["result"].get("preview") is True)),
            key=lambda s: str(s.get("when") or ""), reverse=True)), None)
    newest = rec.latest("classified")
    snap = ctx["snapshot"] or {}
    if newest is not None and newest is not last:
        pres = newest.get("result") or {}
        pcls = pres.get("nd") or pres.get("nd_class") or pres.get("class")
        finding += "; a newer screening PREVIEW ({} by {}{}) - a preview, not the " \
            "classification".format(newest.get("when") or "?",
                                    pres.get("tool") or newest.get("tool") or "?",
                                    ", ND {}".format(pcls) if pcls else "")
    if not last:
        finding += "; no 'classified' status of the classification tool"
    if last:
        res = last.get("result") or {}
        cls = res.get("nd") or res.get("nd_class") or res.get("class")
        finding += "; last 'classified' status: {} by {} {}{}".format(
            last.get("when") or "?", last.get("tool") or "?", last.get("version") or "",
            ", ND {}".format(cls) if cls else "")
        if cls and snap.get("nd") and str(cls).strip().lower() != str(snap["nd"]).lower():
            finding += " - NOT the class of the packaged run ({})".format(snap["nd"])
            flag = "CHECK"
    return _item("C11", "dam.json values and conflicts", finding, flag, src)


def _src_tool(s):
    if isinstance(s, dict):
        return s.get("tool") or "?"
    return str(s or "?")


def _c_chain(chain):
    st = chain.get("status") or ""
    flag = {"MATCH": "OK", "PARTIAL": "INFO", "NO MANIFEST": "INFO", "NO RECORD": "MISSING"} \
        .get(st, "CHECK")
    finding = "{}: {}".format(st, chain.get("summary") or "")
    if chain.get("manifest_path"):
        finding += " (manifest: {}; {})".format(chain.get("manifest_tool") or "?",
                                               (chain.get("manifest_ok") or (0, ""))[1])
    return _item("C12", "Model chain (ND Dam Breach Runner -> classification)", finding, flag,
                 RECORD_DIR + "/run_manifest.json; " + MC.CHECK_CSV)


def _c_repro(repro, asked):
    if not asked:
        return _item("C13", "Reproduction", "not run (reproduce was off) - run the review "
                     "again with reproduce on, in QGIS", "NOT RUN", "")
    if repro.get("status") == "NOT POSSIBLE":
        return _item("C13", "Reproduction", "not possible: {}".format(repro.get("error")),
                     "MISSING", "")
    lines = repro.get("lines") or []
    flag = "OK" if repro.get("status") == "IDENTICAL" else "CHECK"
    finding = lines[0] if lines else str(repro.get("status"))
    if repro.get("missing"):
        finding += "; {} input(s) missing from the package".format(len(repro["missing"]))
    return _item("C13", "Reproduction", finding, flag,
                 REPRO_DIR + "/Reproduction_Check.csv")


def build_checklist(ctx, files, chain, repro, reproduce_asked, today=None):
    """The checklist rows (CHECKLIST_FIELDS), in order."""
    makers = [lambda: _c_files(files), lambda: _c_result(ctx), lambda: _c_version(ctx),
              lambda: _c_profile(ctx), lambda: _c_data(ctx), lambda: _c_review(ctx),
              lambda: _c_vintage(ctx, today), lambda: _c_boundary(ctx),
              lambda: _c_gate(ctx, files), lambda: _c_units(ctx), lambda: _c_damjson(ctx),
              lambda: _c_chain(chain), lambda: _c_repro(repro or {}, reproduce_asked)]
    names = ["Package files", "Result", "Plugin version", "Settings profile", "Data packaged",
             "Open review items", "Data vintage", "Model boundary check", "Model gate",
             "Raster units", "dam.json", "Model chain", "Reproduction"]
    out = []
    for i, (mk, name) in enumerate(zip(makers, names)):
        try:
            out.append(mk())
        except Exception as e:                     # one unreadable part never stops the view
            out.append(_item("C{:02d}".format(i + 1), name, "could not be checked ({}: {})"
                             .format(type(e).__name__, e), "CHECK", ""))
    return out


# ------------------------------------------------------------------ review --
def review_package(package, work_folder, reproduce=False, reviewer="", run=None,
                   rasters_folder=None, progress=None, log=None, today=None):
    """The reviewer's view of a submittal package (folder or .zip) - see the module
    docstring. Nothing is written into the package. Returns {"package_folder",
    "from_zip", "work_folder", "files", "chain", "reproduction", "checklist" (rows),
    "report", "checklist_csv", "files_csv", "notes", "flags" (counts)}. Raises
    ValueError for no package / a work folder inside the package."""
    work_folder = os.path.abspath(work_folder or "")
    folder, from_zip = open_package(package, work_folder)
    os.makedirs(work_folder, exist_ok=True)
    notes = []
    _tick(progress, 5, "Checking the package files")
    files = verify_files(folder)
    _say(log, "Package files: {}".format(", ".join("{} {}".format(v, k) for k, v in
                                                   sorted(files["counts"].items()))))
    _tick(progress, 20, "Model chain")
    chain = MC.chain_check(folder, write=True, out_dir=work_folder)
    _say(log, "Model chain: {}".format(chain.get("summary")))
    repro = None
    if reproduce:
        _tick(progress, 30, "Reproducing the run")
        rwork = os.path.join(work_folder, REPRO_DIR)
        try:
            repro = _reproduce(folder, rwork, run, rasters_folder, progress, log)
        except ValueError as e:
            repro = {"status": "NOT POSSIBLE", "error": str(e), "lines": []}
        except Exception as e:                      # e.g. no QGIS pipeline here
            repro = {"status": "NOT POSSIBLE", "error": "{}: {}".format(type(e).__name__, e),
                     "lines": []}
        _say(log, "Reproduction: {}".format(repro.get("status")))
    _tick(progress, 85, "Checklist")
    ctx = _load_context(folder)
    items = build_checklist(ctx, files, chain, repro, reproduce, today)
    for it in items:
        it["Reviewer"] = reviewer or ""
    flags = {}
    for it in items:
        flags[it["Flag"]] = flags.get(it["Flag"], 0) + 1
    # outputs (the work folder only)
    fpath = _write_csv(os.path.join(work_folder, FILES_CSV), FILES_FIELDS, files["rows"])
    cpath = os.path.join(work_folder, CHECKLIST_CSV)
    if os.path.isfile(cpath) and any((r.get("Reviewer_Decision") or "").strip()
                                     for r in read_checklist(cpath)):
        new = _free_path(cpath)
        notes.append("{} holds the reviewer's decisions - kept; the new checklist is {}".format(
            CHECKLIST_CSV, os.path.basename(new)))
        cpath = new
    cpath = _write_csv(cpath, CHECKLIST_FIELDS, items)
    info = {"package": package, "package_folder": folder, "from_zip": from_zip,
            "reviewer": reviewer, "when": time.strftime("%Y-%m-%d %H:%M"), "notes": notes,
            "flags": flags}
    rpath = os.path.join(work_folder, REPORT_HTML)
    with open(rpath, "w", encoding="utf-8") as fh:
        fh.write(render_html(info, items, files, chain, repro, ctx))
    for n in notes:
        _say(log, n)
    _tick(progress, 100, "Done")
    return {"package_folder": folder, "from_zip": from_zip, "work_folder": work_folder,
            "files": files, "chain": chain, "reproduction": repro, "checklist": items,
            "report": rpath, "checklist_csv": cpath, "files_csv": fpath, "notes": notes,
            "flags": flags}


def _reproduce(folder, rwork, run, rasters_folder, progress, log):
    from . import reproduce as RP
    prog = None
    if progress:
        prog = lambda p, m="": progress(30 + 0.5 * float(p), m)       # noqa: E731
    return RP.reproduce(folder, rwork, run=run, progress=prog, log=log,
                        rasters_folder=rasters_folder)


def _write_csv(path, fields, rows):
    tmp = path + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    try:
        os.replace(tmp, path)
    except OSError:                      # open in Excel
        path = _free_path(path)
        os.replace(tmp, path)
    return path


# ------------------------------------------------------------------- HTML --
_CSS = """
body{font-family:Arial,Helvetica,sans-serif;font-size:10.5pt;color:#111;background:#fff;
 margin:18px;max-width:1100px}
h1{font-size:16pt;margin:0 0 4px}h2{font-size:12.5pt;margin:22px 0 6px;
 border-bottom:1px solid #999}
.note{border:1px solid #999;padding:6px 8px;background:#f4f4f4;margin:8px 0}
table{border-collapse:collapse;width:100%;margin:4px 0}
th,td{border:1px solid #888;padding:3px 5px;vertical-align:top;text-align:left}
th{background:#e6e6e6}td.flag{font-weight:bold;white-space:nowrap}
td.box{min-width:90px}td.comment{min-width:160px}
.CHECK{color:#a00}.MISSING{color:#a60}.OK{color:#070}
code{font-size:9pt;word-break:break-all}
@media print{body{margin:0;max-width:none}h2{page-break-after:avoid}
 tr{page-break-inside:avoid}.noprint{display:none}}
"""


def _e(v):
    return html.escape("" if v is None else str(v), quote=True)


def render_html(info, items, files, chain, repro, ctx):
    rec = ctx.get("settings") or {}
    snap = ctx.get("snapshot") or {}
    out = ["<!doctype html>", "<html lang=\"en\"><head><meta charset=\"utf-8\">",
           "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">",
           "<title>Package review - {}</title>".format(_e(snap.get("dam") or
                                                         rec.get("dam_name") or "dam")),
           "<style>{}</style></head><body>".format(_CSS)]
    out.append("<h1>Submittal package review - {}</h1>".format(
        _e(snap.get("dam") or rec.get("dam_name") or "dam")))
    out.append("<p>Package: <code>{}</code>{}<br>Reviewed {} with {} v{}<br>Reviewer: {}"
               "</p>".format(_e(info["package"]),
                             " (unzipped to <code>{}</code>)".format(_e(info["package_folder"]))
                             if info["from_zip"] else "", _e(info["when"]), _e(PLUGIN_NAME),
                             _e(PLUGIN_VERSION),
                             _e(info["reviewer"]) if info["reviewer"] else
                             "______________________"))
    out.append("<div class=\"note\"><b>About this checklist.</b> The items below are this "
               "tool's own checks of the package, each with an automatic finding. They are "
               "NOT the State's review criteria and not a list of regulatory requirements. "
               "The decision and comment columns are for the reviewer (Accept / Question / "
               "Not applicable); fill them in {} and re-read it with "
               "reviewpkg.summarize_checklist. Nothing was written into the package."
               "</div>".format(_e(CHECKLIST_CSV)))
    for n in info.get("notes") or []:
        out.append("<p class=\"note\">{}</p>".format(_e(n)))
    out.append("<p>Automatic flags: {}</p>".format(_e(", ".join(
        "{} {}".format(v, k) for k, v in sorted(info["flags"].items())))))
    out.append("<h2>Checklist</h2><table><tr><th>ID</th><th>Item</th><th>Automatic finding"
               "</th><th>Flag</th><th>Decision</th><th>Comment</th></tr>")
    for it in items:
        out.append("<tr><td>{}</td><td>{}<br><small>{}</small></td><td>{}</td>"
                   "<td class=\"flag {}\">{}</td><td class=\"box\"></td>"
                   "<td class=\"comment\"></td></tr>".format(
                       _e(it["Item_ID"]), _e(it["Item"]), _e(it["Source"]),
                       _e(it["Automatic_Finding"]), _e(it["Flag"].replace(" ", "")),
                       _e(it["Flag"])))
    out.append("</table>")
    # files
    c = files["counts"]
    out.append("<h2>Package files against MANIFEST.csv</h2><p>{}</p>".format(_e(", ".join(
        "{} {}".format(v, k) for k, v in sorted(c.items())) or "no files")))
    if files["problems"]:
        out.append("<table><tr><th>File</th><th>Problem</th></tr>")
        for p, s in files["problems"]:
            out.append("<tr><td><code>{}</code></td><td>{}</td></tr>".format(_e(p), _e(s)))
        out.append("</table>")
    else:
        out.append("<p>No file missing, changed or added. Every file: {}.</p>".format(
            _e(FILES_CSV)))
    # chain
    out.append("<h2>Model chain (ND Dam Breach Runner -&gt; classification)</h2>")
    out.append("<p><b>{}</b>: {}</p>".format(_e(chain.get("status")), _e(chain.get("summary"))))
    if chain.get("manifest_path"):
        out.append("<p>Runner manifest: <code>{}</code> - {}{} - {}</p>".format(
            _e(chain["manifest_path"]), _e(chain.get("manifest_tool") or "?"),
            _e(", made " + chain["manifest_created"]) if chain.get("manifest_created") else "",
            _e((chain.get("manifest_ok") or (0, ""))[1])))
    if chain.get("rows"):
        out.append("<table><tr><th>Scenario</th><th>Raster</th><th>Status</th>"
                   "<th>Run's raster</th><th>Note</th></tr>")
        for r in chain["rows"]:
            out.append("<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>".format(
                _e(r["Scenario"]), _e(r["Raster"]), _e(r["Status"]), _e(r["Manifest_Label"]),
                _e(r["Note"])))
        out.append("</table>")
    # reproduction
    out.append("<h2>Reproduction</h2>")
    if repro is None:
        out.append("<p>Not run.</p>")
    elif repro.get("status") == "NOT POSSIBLE":
        out.append("<p>Not possible: {}</p>".format(_e(repro.get("error"))))
    else:
        out.append("<pre style=\"white-space:pre-wrap\">{}</pre>".format(
            _e("\n".join(str(x) for x in repro.get("lines") or []))))
    out.append("<p><small>{} v{} - review view of {}. The findings are automatic; the "
               "decisions are the reviewer's.</small></p>".format(
                   _e(PLUGIN_NAME), _e(PLUGIN_VERSION), _e(info["when"])))
    out.append("</body></html>")
    return "\n".join(out) + "\n"


# --------------------------------------------------- filled-in checklist --
def normalize_decision(value):
    """"Accept" / "Question" / "Not applicable", NO_DECISION for an empty cell, or
    NOT_RECOGNISED."""
    v = str(value or "").strip()
    if not v:
        return NO_DECISION
    for d in DECISIONS:
        if v.lower() == d.lower():
            return d
    return _DECISION_WORDS.get(v.lower(), NOT_RECOGNISED)


def read_checklist(path):
    """The rows of a (filled) Review_Checklist.csv: [dict] (',' or ';' - Excel in some
    locales saves with ';'); [] when missing or unreadable."""
    return _csv_rows(path)


def summarize_checklist(path):
    """{"total", "counts" {decision: n} (DECISIONS + NO_DECISION + NOT_RECOGNISED),
    "questions" [(id, item, comment)], "undecided" [id], "unrecognised" [(id, value)],
    "check_accepted_without_comment" [id], "lines"}. Raises ValueError when the file is no
    review checklist."""
    rows = read_checklist(path)
    if not rows or "Reviewer_Decision" not in rows[0] or "Item_ID" not in rows[0]:
        raise ValueError("{} is not a review checklist (no Item_ID / Reviewer_Decision "
                         "columns)".format(path))
    counts = {d: 0 for d in DECISIONS + (NO_DECISION, NOT_RECOGNISED)}
    questions, undecided, unrec, bare = [], [], [], []
    reviewers = set()
    for r in rows:
        d = normalize_decision(r.get("Reviewer_Decision"))
        counts[d] += 1
        iid = r.get("Item_ID") or "?"
        if (r.get("Reviewer") or "").strip():
            reviewers.add(r["Reviewer"].strip())
        if d == "Question":
            questions.append((iid, r.get("Item") or "", (r.get("Reviewer_Comment") or "")
                              .strip()))
        elif d == NO_DECISION:
            undecided.append(iid)
        elif d == NOT_RECOGNISED:
            unrec.append((iid, r.get("Reviewer_Decision")))
        if d == "Accept" and (r.get("Flag") or "") == "CHECK" and \
                not (r.get("Reviewer_Comment") or "").strip():
            bare.append(iid)
    lines = ["Review checklist {}: {} item(s) - {}".format(
        os.path.basename(path), len(rows), ", ".join("{} {}".format(counts[k], k)
                                                     for k in counts if counts[k]))]
    if reviewers:
        lines.append("Reviewer(s): " + ", ".join(sorted(reviewers)))
    for iid, item, com in questions:
        lines.append("  QUESTION {} {}: {}".format(iid, item, com or "(no comment)"))
    if undecided:
        lines.append("  No decision yet: " + ", ".join(undecided))
    for iid, v in unrec:
        lines.append("  Not recognised ({}): '{}' - use Accept, Question or Not "
                     "applicable".format(iid, v))
    if bare:
        lines.append("  Accepted with an automatic CHECK flag and no comment: " +
                     ", ".join(bare))
    return {"total": len(rows), "counts": counts, "questions": questions,
            "undecided": undecided, "unrecognised": unrec,
            "check_accepted_without_comment": bare, "lines": lines}
