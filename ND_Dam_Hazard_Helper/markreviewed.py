# -*- coding: utf-8 -*-
"""v2.12.0: the reviewer's sign-off of a classification ("Mark the
classification reviewed", Processing ndhazard:markreviewed).

dam.json (damsuite.dam_record) has had a "reviewed" step since 2.10 that
nothing wrote. mark_reviewed() adds a status entry

    {"step": "reviewed", "tool": "ndhazard", "version": <PLUGIN_VERSION>,
     "when": ..., "result": {"decision", "comments", "package_sha256",
                             "open_items", "reviewer"}}

and appends one line to <dam>/outputs/QA/Review_Log.csv. Nothing else in
dam.json is touched: no field is set, so another tool's values are never
overwritten; the record's other status entries stay.

DECISIONS: "Accepted", "Accepted with comments", "Returned" (the reviewer's
words for this plugin's record - not a regulatory finding). "Returned" with
open_items 0 is recorded with open_items 1 (a returned classification has at
least one open item) and says so. The dashboard (portfolio.py) takes the
"review" next action from the open_items of the newest "reviewed" status
that is newer than the classification.

PACKAGE: the submittal package (folder, or the .zip) the reviewer looked at.
Its MANIFEST.csv (the file every package file is checked against, see
reviewpkg.verify_files) is hashed - SHA-256 of the manifest's bytes - so the
record says WHICH package was reviewed; the package itself is not read
further and never written to.

Nothing here was run in a real QGIS (simulated QGIS / Qt only). Pure Python.
"""

import csv
import datetime
import hashlib
import os
import zipfile

from .constants import PLUGIN_VERSION, OUTPUT_FOLDER_NAME, SUBDIR_QA
from .damsuite import dam_record as DR

TOOL = "ndhazard"
STEP = "reviewed"
DECISIONS = ("Accepted", "Accepted with comments", "Returned")
LOG_NAME = "Review_Log.csv"
LOG_FIELDS = ("When", "Reviewer", "Decision", "Open_Items", "Package", "Package_SHA256",
              "Classification_Date", "Classification_Class", "Plugin_Version", "Comments")
MANIFEST_NAME = "MANIFEST.csv"                     # reviewpkg.MANIFEST_NAME / submittal


def normalize_decision(value):
    """One of DECISIONS from the reviewer's text (case / spacing tolerant;
    "accept", "accepted w/ comments", "return" ...), or None."""
    t = " ".join(str(value or "").replace("_", " ").replace("/", " ").split()).lower()
    if not t:
        return None
    if t.startswith("return") or t in ("rejected", "reject", "not accepted"):
        return "Returned"
    if "comment" in t and t.startswith("accept"):
        return "Accepted with comments"
    if t.startswith("accept"):
        return "Accepted"
    for d in DECISIONS:
        if t == d.lower():
            return d
    return None


def manifest_sha256(package):
    """(sha256, manifest location) of the package's MANIFEST.csv: in the
    package folder, or inside the .zip (top level or under its one top
    folder - as submittal.build zips it). Raises ValueError when the package
    or its manifest cannot be found."""
    if not package:
        raise ValueError("no package given")
    package = os.path.abspath(package)
    if os.path.isdir(package):
        mpath = os.path.join(package, MANIFEST_NAME)
        if not os.path.isfile(mpath):
            raise ValueError("{} has no {}".format(package, MANIFEST_NAME))
        h = hashlib.sha256()
        with open(mpath, "rb") as fh:
            for block in iter(lambda: fh.read(1 << 20), b""):
                h.update(block)
        return h.hexdigest(), mpath
    if os.path.isfile(package) and zipfile.is_zipfile(package):
        with zipfile.ZipFile(package) as zf:
            names = [n for n in zf.namelist()
                     if n.replace("\\", "/").split("/")[-1] == MANIFEST_NAME]
            # the top-level manifest, else the shallowest one
            names.sort(key=lambda n: n.replace("\\", "/").count("/"))
            if not names:
                raise ValueError("{} holds no {}".format(package, MANIFEST_NAME))
            data = zf.read(names[0])
        return hashlib.sha256(data).hexdigest(), "{}!{}".format(package, names[0])
    raise ValueError("{} is neither a package folder nor a .zip".format(package))


def _latest_classification(rec):
    """The newest real (non-preview) "classified" status, or None."""
    try:
        return rec.latest("classified", include_previews=False)
    except Exception:                                        # noqa
        return None


def mark_reviewed(dam_folder, reviewer, decision, comments="", package=None, open_items=0,
                  when=None, version=PLUGIN_VERSION, log=None):
    """Record the review in <dam>/dam.json and outputs/QA/Review_Log.csv.
    Returns {"status" (the entry written), "dam_json", "log_csv", "notes",
    "package_sha256"}. Raises ValueError for a missing dam folder, an empty
    reviewer name, an unknown decision or an unusable package."""
    notes = []
    if not dam_folder or not os.path.isdir(dam_folder):
        raise ValueError("Dam folder not found: {}".format(dam_folder or "(none)"))
    who = " ".join(str(reviewer or "").split())
    if not who:
        raise ValueError("The reviewer's name is required")
    dec = normalize_decision(decision)
    if dec is None:
        raise ValueError("Decision must be one of: {}".format(", ".join(DECISIONS)))
    try:
        n_open = int(open_items or 0)
    except (TypeError, ValueError):
        raise ValueError("Open items must be a whole number")
    if n_open < 0:
        raise ValueError("Open items cannot be negative")
    if dec == "Returned" and n_open == 0:
        n_open = 1
        notes.append("Returned with 0 open items: recorded as 1 open item (a returned "
                     "classification has at least one)")
    pkg_sha, pkg_where = "", ""
    if package:
        pkg_sha, pkg_where = manifest_sha256(package)
    comments = str(comments or "").strip()
    rec = DR.load(dam_folder, create=True)                  # ValueError for a broken file
    cla = _latest_classification(rec)
    if cla is None:
        notes.append("dam.json has no 'classified' status: the review is recorded, but the "
                     "dashboard cannot tell which classification it belongs to")
    result = {"decision": dec, "comments": comments, "package_sha256": pkg_sha,
              "open_items": n_open, "reviewer": who}
    if pkg_where:
        result["package"] = os.path.abspath(package)
    if cla is not None:
        result["classification_when"] = cla.get("when") or ""
    # only a status entry is added: the fields, their sources and the conflicts (other
    # tools' values) are not touched
    entry = rec.add_status(STEP, TOOL, version, result=result, when=when)
    dam_json = rec.save()
    log_csv = _append_log(dam_folder, entry, pkg_where, cla, version)
    for n in notes:
        if log:
            log(">>> NOTE: " + n)
    if log:
        log(">>> Review recorded in {}: {} by {}, {} open item(s){}".format(
            os.path.basename(dam_json), dec, who, n_open,
            ", package manifest sha256 {}".format(pkg_sha[:16]) if pkg_sha else ""))
    return {"status": entry, "dam_json": dam_json, "log_csv": log_csv, "notes": notes,
            "package_sha256": pkg_sha}


def _append_log(dam_folder, entry, pkg_where, cla, version):
    qa = os.path.join(dam_folder, OUTPUT_FOLDER_NAME, SUBDIR_QA)
    os.makedirs(qa, exist_ok=True)
    path = os.path.join(qa, LOG_NAME)
    res = entry.get("result") or {}
    cres = (cla or {}).get("result") or {}
    row = {"When": entry.get("when") or "", "Reviewer": res.get("reviewer", ""),
           "Decision": res.get("decision", ""), "Open_Items": res.get("open_items", 0),
           "Package": pkg_where, "Package_SHA256": res.get("package_sha256", ""),
           "Classification_Date": (cla or {}).get("when") or "",
           "Classification_Class": cres.get("nd") or cres.get("class") or "",
           "Plugin_Version": version, "Comments": res.get("comments", "")}
    new = not os.path.isfile(path) or os.path.getsize(path) == 0
    with open(path, "a", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(LOG_FIELDS))
        if new:
            w.writeheader()
        w.writerow(row)
    return path


def read_log(dam_folder):
    """[dict] rows of outputs/QA/Review_Log.csv ([] when none)."""
    path = os.path.join(dam_folder, OUTPUT_FOLDER_NAME, SUBDIR_QA, LOG_NAME)
    if not os.path.isfile(path):
        return []
    with open(path, newline="", encoding="utf-8-sig") as fh:
        return list(csv.DictReader(fh))


def review_facts(rec):
    """{"open_review_items": n, "review_decision", "reviewer", "review_when"}
    from the newest "reviewed" status that is newer than the (non-preview)
    classification - the review of the CURRENT classification; {} when
    there is none (or the review is older than a re-classification). For
    portfolio.py's next action."""
    if rec is None:
        return {}
    try:
        rev = rec.latest(STEP)
    except Exception:                                        # noqa
        return {}
    if rev is None:
        return {}
    cla = _latest_classification(rec)
    if cla is not None and not DR._newer(rec, rev, cla):
        return {}
    res = rev.get("result") if isinstance(rev.get("result"), dict) else {}
    try:
        n = int(res.get("open_items") or 0)
    except (TypeError, ValueError):
        n = 0
    return {"open_review_items": n, "review_decision": str(res.get("decision") or ""),
            "reviewer": str(res.get("reviewer") or ""), "review_when": rev.get("when") or ""}


def _day(iso):
    return str(iso or "")[:10]


def summary_line(rec):
    """One line for the UI / log, or ''."""
    f = review_facts(rec)
    if not f:
        return ""
    return "Reviewed {} by {}: {}, {} open item(s)".format(
        _day(f["review_when"]), f["reviewer"] or "?", f["review_decision"] or "?",
        f["open_review_items"])


def now_iso():
    return datetime.datetime.now().replace(microsecond=0).isoformat()
