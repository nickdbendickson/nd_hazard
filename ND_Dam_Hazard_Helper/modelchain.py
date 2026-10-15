# -*- coding: utf-8 -*-
"""v2.10: the traceable chain model -> classification.

Question answered: are the flood rasters this classification READ the rasters
the ND Dam Breach Runner WROTE?

  * What the classification read comes from its own record, QA/Run_Settings.json
    (reproduce.run_settings_dict): the "scenarios" (the depth / velocity /
    arrival / D x V rasters of every scenario) and their "fingerprints"
    (SHA-256, size and date taken when the run ended).
  * What the model wrote comes from the Runner's <dam folder>/_breach_model/
    run_manifest.json ("outputs", then "inputs", with SHA-256), found and read
    with damsuite.chain (the Runner's own digest algorithm, reimplemented).
    The manifest's own digest is checked first (damsuite.chain.manifest_ok).

Per raster (one row of QA/Model_Chain_Check.csv):
  MATCH            the SHA-256 recorded at the classification (or, without a
                   recorded one, the file as it is now) is the digest of one of
                   the run's rasters.
  DIFFERENT        the run has a raster of the same file name or role with
                   another digest: the raster was changed or replaced after the
                   model run (or, without a recorded digest, the file changed
                   after the classification - the chain cannot be proven).
  NOT IN MANIFEST  no raster of the run has this digest, file name or role.
  MISSING          no digest was recorded and the file is not there any more.
  RUN INPUT        the digest is one of the run's INPUTS (a raster the Runner read,
                   e.g. a baseline given to it), not one of its outputs.
  NOT CHECKED      no Runner manifest (e.g. HEC-RAS results, DSS-WISE, rasters
                   from elsewhere): only the fingerprint is recorded; or a HEC-RAS
                   result next to a Runner manifest that does not list it.

Overall status (chain_check()["status"]):
  MATCH            every classified raster is one of the model's own outputs.
  PARTIAL          the Runner's rasters match; the others are HEC-RAS results
                   fingerprinted only.
  DIFFERENT        at least one raster changed after the model run.
  INCOMPLETE       rasters not in the run, run inputs, or missing; none DIFFERENT.
  MANIFEST INVALID the manifest's own digest does not match its content (edited
                   after the run) or it cannot be read: nothing can be proven from
                   it (rows are still listed).
  NO MANIFEST      not produced by the ND Dam Breach Runner - chain not checked.
  NO RECORD        no QA/Run_Settings.json (a run of a version before 2.9, or it
                   was not written): nothing to check.

A SHA-256 is recorded at the classification for files up to
reproduce.HASH_MAX_BYTES (1 GB); a bigger raster is compared as it is now (the
Runner's digest samples files above 2 GB: start, middle, end - noted), and its
recorded size / date must still be those of the classification.

The check never changes a raster, the record or the manifest. It writes
Model_Chain_Check.csv and Model_Chain_Check.txt in the folder given (default:
the run's QA folder; the reviewer view writes them in its work folder, never
in a package). pipeline_step() is the end-of-run hook (it never fails the
run); package_files() lists what a submittal package takes along
(integration notes: /home/claude/suite/integration/chain_submittal.patch.md).

Nothing here needs QGIS. Nothing has been run in a real QGIS.
"""

import csv
import json
import os
import re
import shutil

from .damsuite import chain as CH
from .constants import SUBDIR_QA, OUTPUT_FOLDER_NAME

CHECK_CSV = "Model_Chain_Check.csv"
CHECK_TXT = "Model_Chain_Check.txt"
RUN_REPORT_NAME = "run_report.json"          # v2.28: the Runner's report next to its manifest
QA_MANIFEST_COPY = "run_manifest.json"       # the manifest the classification was checked with
PACKAGE_RECORD_DIR = "02_Method_and_record"
FIELDS = ["Scenario", "Raster", "Classified_Path", "Recorded_SHA256", "Current_SHA256",
          "Manifest_Label", "Manifest_Path", "Manifest_SHA256", "Status", "Note"]
RASTER_KEYS = (("depth", "depth"), ("velocity", "velocity"), ("arrival", "arrival"),
               ("dv", "dv"))
OVERALL = ("MATCH", "PARTIAL", "DIFFERENT", "INCOMPLETE", "MANIFEST INVALID", "NO MANIFEST",
           "NO RECORD")
_PLAN_RE = re.compile(r"\.p\d{2}\.hdf$", re.I)             # rasimport.PLAN_RE
_HECRAS_CACHE = "hecras_plan_cache"                        # rasimport.CACHE_DIR_NAME (lower)


# ------------------------------------------------------------------ inputs --
def _is_package(folder):
    return os.path.isfile(os.path.join(folder, "MANIFEST.csv")) or \
        os.path.isfile(os.path.join(folder, "Run_Settings_package.json"))


def _record_of(source):
    """(record dict or None, kind, base folder, outputs folder or None, dam folder or None).
    kind: "outputs" / "package" / "record" / "cfg"."""
    from . import reproduce as RP
    if isinstance(source, dict):
        if source.get("format") == RP.RUN_SETTINGS_FORMAT:
            s = source.get("settings") or {}
            return source, "record", None, s.get("output_folder"), s.get("dam_folder")
        out = source.get("output_folder") or ""
        dam = source.get("dam_folder") or ""
        rs = os.path.join(out, SUBDIR_QA, RP.RUN_SETTINGS_FILE) if out else ""
        if rs and os.path.isfile(rs):
            try:
                return RP.read_run_settings(rs), "outputs", None, out, dam
            except ValueError:
                pass
        rec = {"scenarios": RP._scenario_dicts(source, None), "fingerprints": {}}
        for sc in rec["scenarios"]:
            for key, _l in RASTER_KEYS:
                p = sc.get(key)
                if p and p not in rec["fingerprints"]:
                    rec["fingerprints"][p] = RP.fingerprint(p)
        return rec, "cfg", None, out or None, dam or None
    path = str(source or "")
    if os.path.isfile(path):
        try:
            rec = RP.read_run_settings(path)
        except ValueError:
            return None, "record", None, None, None
        base = os.path.dirname(os.path.abspath(path))
        if os.path.basename(path) == RP.PACKAGE_SETTINGS_FILE or \
                os.path.basename(base) == PACKAGE_RECORD_DIR:
            pkg = base if os.path.basename(path) == RP.PACKAGE_SETTINGS_FILE else \
                os.path.dirname(base)
            return rec, "package", pkg, None, None
        s = rec.get("settings") or {}
        return rec, "record", None, s.get("output_folder"), s.get("dam_folder")
    if not os.path.isdir(path):
        return None, "outputs", None, None, None
    if _is_package(path):
        try:
            rec, _p, _packaged = RP._find_settings(path)
        except ValueError:
            rec = None
        return rec, "package", path, None, None
    out = path
    if not os.path.isfile(os.path.join(out, SUBDIR_QA, RP.RUN_SETTINGS_FILE)) and \
            os.path.isdir(os.path.join(path, OUTPUT_FOLDER_NAME)):
        out = os.path.join(path, OUTPUT_FOLDER_NAME)          # a dam folder was given
    try:
        rec = RP.read_run_settings(os.path.join(out, SUBDIR_QA, RP.RUN_SETTINGS_FILE))
    except ValueError:
        rec = None
    dam = ((rec or {}).get("settings") or {}).get("dam_folder") or os.path.dirname(
        os.path.abspath(out))
    return rec, "outputs", None, out, dam


def _raster_role(scenario_role, key):
    """The damsuite.chain role of a raster: "depth", "baseline depth" ... (the Runner's
    manifest labels: "depth", "velocity", "arrival", "baseline depth" ...)."""
    return ("baseline " if str(scenario_role or "").lower() == "baseline" else "") + key


def classified_rasters(record, package_folder=None):
    """[{"scenario", "key", "role", "recorded_path", "path", "fingerprint"}] of the rasters
    the classification read. `path`: the file at hand now (in a package: its copy, else
    the recorded path when it exists), or ""."""
    from . import reproduce as RP
    fps = (record or {}).get("fingerprints") or {}
    orig_of = {rel: src for src, rel in (((record or {}).get("package") or {})
                                          .get("files") or {}).items()}
    out = []
    for sc in (record or {}).get("scenarios") or []:
        for key, _label in RASTER_KEYS:
            p = sc.get(key)
            if not p:
                continue
            p = str(p)
            orig = orig_of.get(p, p)
            here = ""
            if package_folder and RP._is_package_relative(p):
                cand = os.path.join(package_folder, *p.replace("\\", "/").split("/"))
                here = cand if os.path.isfile(cand) else ""
            elif os.path.isfile(p):
                here = p
            out.append({"scenario": sc.get("name") or "Scenario", "key": key,
                        "role": _raster_role(sc.get("role"), key), "recorded_path": orig,
                        "path": here, "fingerprint": fps.get(orig) or {}})
    return out


def hecras_hint(path):
    """True when a raster looks like a HEC-RAS result: a plan file (.p##.hdf), the
    plugin's HEC-RAS plan cache, or a RAS Mapper project (.rasmap / .prj with .p##.hdf)
    in its folder or the folder above."""
    p = str(path or "")
    if not p:
        return False
    if _PLAN_RE.search(os.path.basename(p)):
        return True
    if _HECRAS_CACHE in p.replace("\\", "/").lower().split("/"):
        return True
    d = os.path.dirname(os.path.abspath(p))
    for folder in (d, os.path.dirname(d)):
        try:
            names = os.listdir(folder)
        except OSError:
            continue
        if any(n.lower().endswith(".rasmap") or _PLAN_RE.search(n) for n in names):
            return True
    return False


# -------------------------------------------------------------- comparison --
def _entries(manifest):
    out = []
    for group in ("outputs", "inputs"):
        for label, rec in ((manifest or {}).get(group) or {}).items():
            if isinstance(rec, dict):
                out.append((group, label, rec))
    return out


def _by_digest(entries, sha, size=None):
    for e in entries:
        rec = e[2]
        if rec.get("sha256") and rec.get("sha256") == sha and \
                (size is None or rec.get("size") is None or rec.get("size") == size):
            return e
    return None


def _by_name_or_role(entries, path, role):
    name = os.path.basename(str(path or "").replace("\\", "/")).lower()
    if name:
        for e in entries:
            if os.path.basename(str(e[2].get("path", "")).replace("\\", "/")).lower() == name:
                return e, "file name"
    nr = CH._norm_role(role)
    for e in entries:
        if nr and CH._norm_role(e[1]) == nr:
            return e, "role"
    return None, ""


CHANGED_NOTE = ("the run wrote '{}' at this path with another SHA-256: the raster was changed "
                "after the model run")
OTHER_NOTE = ("same {} as the run's '{}' but another file and SHA-256: not the raster the "
              "model wrote")


def _same_place(a, b, dam_folder):
    """Is the classified file the file the manifest recorded (same path, or the same
    name in the dam folder when the recorded path moved)?"""
    if not a or not b:
        return False
    n = lambda p: os.path.normcase(os.path.abspath(str(p)))       # noqa: E731
    if n(a) == n(b):
        return True
    return bool(dam_folder) and n(CH._where(str(b), dam_folder)) == n(a)


def _row(r, **kw):
    row = {"Scenario": r["scenario"], "Raster": r["role"],
           "Classified_Path": r["recorded_path"],
           "Recorded_SHA256": (r["fingerprint"] or {}).get("sha256") or "",
           "Current_SHA256": "", "Manifest_Label": "", "Manifest_Path": "",
           "Manifest_SHA256": "", "Status": "", "Note": ""}
    row.update(kw)
    return row


def _set_hit(row, hit, dam_folder):
    if hit is not None:
        row["Manifest_Label"] = hit[1]
        row["Manifest_Path"] = CH._where(str(hit[2].get("path", "")), dam_folder)
        row["Manifest_SHA256"] = hit[2].get("sha256") or ""


def compare(rasters, manifest, dam_folder=None):
    """Rows (FIELDS) comparing classified_rasters() with a Runner manifest (None: no
    manifest - every raster NOT CHECKED, fingerprint recorded)."""
    entries = _entries(manifest) if manifest is not None else []
    rows = []
    for r in rasters:
        fp = r["fingerprint"] or {}
        rec_sha = fp.get("sha256") or ""
        now = CH.file_digest(r["path"]) if r["path"] else {"missing": True}
        row = _row(r, Current_SHA256=now.get("sha256", ""))
        notes = []
        ras = hecras_hint(r["recorded_path"]) or hecras_hint(r["path"])
        changed_since = bool(rec_sha and now.get("sha256") and not now.get("sampled")
                             and now["sha256"] != rec_sha)
        if manifest is None:
            row["Status"] = "NOT CHECKED"
            notes.append("{}no model manifest to compare - fingerprint recorded{}".format(
                "HEC-RAS result: " if ras else "",
                "" if rec_sha or now.get("sha256") else " (no SHA-256: file too big or "
                "missing)"))
        elif rec_sha:
            hit = _by_digest(entries, rec_sha, fp.get("size"))
            if hit is not None:
                row["Status"] = "MATCH"
                notes.append("the classification read the run's '{}' (SHA-256 recorded when "
                             "the classification ran)".format(hit[1]))
            elif ras:
                row["Status"] = "NOT CHECKED"
                notes.append("HEC-RAS result, not a raster of this Runner run - fingerprint "
                             "recorded")
            else:
                hit, how = _by_name_or_role(entries, r["recorded_path"], r["role"])
                if hit is not None:
                    row["Status"] = "DIFFERENT"
                    if hit[2].get("missing") or not hit[2].get("sha256"):
                        notes.append("the manifest recorded '{}' without a digest".format(
                            hit[1]))
                    elif how == "file name" and _same_place(
                            r["recorded_path"], hit[2].get("path"), dam_folder):
                        notes.append(CHANGED_NOTE.format(hit[1]))
                    else:
                        notes.append(OTHER_NOTE.format(how, hit[1]))
                else:
                    row["Status"] = "NOT IN MANIFEST"
                    notes.append("no raster of the run has this SHA-256, file name or role")
            _set_hit(row, hit, dam_folder)
            if hit is not None and hit[0] == "inputs":
                notes.append("found among the run's inputs")
                if row["Status"] == "MATCH":
                    row["Status"] = "RUN INPUT"
                    notes.append("a raster the run READ, not one the model wrote")
        elif r["path"] and not now.get("missing"):
            c = CH.compare_rasters({r["role"]: r["path"]}, manifest, dam_folder)[0]
            row["Status"] = c["status"]
            if c["status"] == "NOT IN MANIFEST" and ras:
                row["Status"] = "NOT CHECKED"
                notes.append("HEC-RAS result, not a raster of this Runner run - fingerprint "
                             "recorded")
            else:
                notes.append(c["note"])
            row["Manifest_Label"] = c["manifest_label"]
            row["Manifest_Path"] = c["manifest_path"]
            if c["status"] == "MATCH" and "found among the run's inputs" in c["note"]:
                row["Status"] = "RUN INPUT"
                notes.append("a raster the run READ, not one the model wrote")
            hit = next((e for e in entries if e[1] == c["manifest_label"]), None)
            row["Manifest_SHA256"] = (hit[2].get("sha256") or "") if hit else ""
            notes.append("no SHA-256 recorded at the classification (file over the 1 GB "
                         "record limit, or an older record): the file as it is now was "
                         "compared")
            if fp.get("exists") and (fp.get("size") != now.get("size")):
                row["Status"] = "DIFFERENT"
                notes.append("its size is not the size the classification recorded: the "
                             "file changed after the classification - the chain cannot be "
                             "proven")
        else:
            row["Status"] = "MISSING"
            notes.append("no SHA-256 recorded and the file is not at hand - nothing to compare")
        if changed_since:
            notes.append("the file at hand ({}) is not the file the classification read "
                          "(SHA-256 differs from the record)".format(r["path"]))
        if now.get("sampled"):
            notes.append("large file: sampled digest (start, middle, end)")
        row["Note"] = "; ".join(n for n in notes if n)
        rows.append(row)
    return rows


def _overall(rows, manifest, man_ok):
    counts = {}
    for r in rows:
        counts[r["Status"]] = counts.get(r["Status"], 0) + 1
    n = len(rows)
    label = lambda r: "{} {}".format(r["Scenario"], r["Raster"])     # noqa: E731
    if manifest is None:
        return "NO MANIFEST", ("not produced by the ND Dam Breach Runner (no manifest) - "
                               "chain not checked; fingerprints of {} classified raster(s) "
                               "recorded".format(n)), counts
    if not man_ok[0]:
        return "MANIFEST INVALID", ("the Runner's run manifest cannot be trusted ({}) - the "
                                    "chain cannot be proven".format(man_ok[1])), counts
    diff = [r for r in rows if r["Status"] == "DIFFERENT"]
    if diff:
        late = [label(r) for r in diff if "changed after the classification" in r["Note"]]
        changed = [label(r) for r in diff if "changed after the model run" in r["Note"]
                   and label(r) not in late]
        other = [label(r) for r in diff if label(r) not in late and label(r) not in changed]
        parts = []
        if changed:
            parts.append("{} changed after the model run".format(", ".join(changed)))
        if late:
            parts.append("{} changed after the classification (no SHA-256 recorded; the "
                         "chain cannot be proven)".format(", ".join(late)))
        if other:
            parts.append("{} not the raster(s) the model wrote (another file / SHA-256 for "
                         "the same name or role)".format(", ".join(other)))
        return "DIFFERENT", "DIFFERENT: " + "; ".join(parts), counts
    bad = [label(r) + (" (an input of the run, not an output)" if r["Status"] == "RUN INPUT"
                       else "")
           for r in rows if r["Status"] in ("NOT IN MANIFEST", "MISSING", "RUN INPUT")]
    m = counts.get("MATCH", 0)
    if bad:
        return "INCOMPLETE", ("{} of {} classified raster(s) are the model's own outputs "
                              "(SHA-256); not in the model run or missing: {}".format(
                                  m, n, ", ".join(bad))), counts
    other = counts.get("NOT CHECKED", 0)
    if other:
        return "PARTIAL", ("{} of {} classified raster(s) are the model's own outputs "
                           "(SHA-256); {} HEC-RAS result(s) fingerprinted only (no manifest "
                           "to compare)".format(m, n, other)), counts
    return "MATCH", "all {} classified rasters are the model's own outputs (SHA-256)".format(
        n), counts


# ------------------------------------------------------------------- check --
def chain_check(source, dam_folder=None, write=True, out_dir=None, manifest_path=None):
    """Compare the rasters a classification read with the Runner's run manifest.

    source: the run's outputs folder (or the dam folder), a submittal package folder,
    a Run_Settings(.json / _package.json) file or dict, or the run's cfg dict.
    dam_folder: where to look for <dam>/_breach_model/run_manifest.json (default: the
    record's dam_folder, else the folder above the outputs). A package is checked
    against ITS copy (02_Method_and_record/run_manifest.json), never the dam folder,
    unless manifest_path is given. write: Model_Chain_Check.csv / .txt in out_dir
    (default <outputs>/QA; nothing written for a package or a record without out_dir).

    Returns {"status" (OVERALL), "summary", "lines", "rows" (FIELDS dicts), "counts",
    "manifest_path", "manifest_ok" (bool, text), "manifest_tool", "manifest_created",
    "csv", "text", "kind"}."""
    rec, kind, pkg, out, dam = _record_of(source)
    dam = dam_folder or dam
    res = {"status": "", "summary": "", "lines": [], "rows": [], "counts": {},
           "manifest_path": None, "manifest_ok": (False, ""), "manifest_tool": "",
           "manifest_created": "", "csv": None, "text": None, "kind": kind}
    if out_dir is None and write and kind in ("outputs", "cfg") and out:
        out_dir = os.path.join(out, SUBDIR_QA)
    # the manifest
    mpath = manifest_path
    if mpath is None:
        if kind == "package":
            cand = os.path.join(pkg, PACKAGE_RECORD_DIR, CH.MANIFEST_NAME)
            mpath = cand if os.path.isfile(cand) else None
        elif dam and os.path.isdir(dam):
            mpath = CH.find_runner_manifest(dam)
    manifest = None
    if mpath:
        try:
            manifest = CH.read_runner_manifest(mpath)
        except (OSError, ValueError) as e:
            res["manifest_ok"] = (False, "the manifest could not be read: {}".format(e))
            manifest = {}
    res["manifest_path"] = mpath
    if manifest is not None:
        if manifest:
            res["manifest_ok"] = CH.manifest_ok(manifest)
        res["manifest_tool"] = str(manifest.get("tool") or "")
        res["manifest_created"] = str(manifest.get("created") or "")
    # the rasters
    if rec is None:
        res["status"] = "NO RECORD"
        res["summary"] = ("no record of the rasters the classification read (QA/Run_Settings."
                          "json missing or unreadable - a run of a version before 2.9?) - "
                          "chain not checked")
        res["lines"] = [res["summary"]]
    else:
        rows = compare(classified_rasters(rec, pkg), manifest, dam)
        res["rows"] = rows
        if not rows:
            res["status"] = "NO RECORD"
            res["summary"] = "the record names no scenario raster - chain not checked"
        else:
            res["status"], res["summary"], res["counts"] = _overall(
                rows, manifest, res["manifest_ok"])
        lines = ["Model chain check: " + res["summary"]]
        if mpath:
            lines.append("  Runner manifest: {} ({}{}) - {}".format(
                mpath, res["manifest_tool"] or "tool ?",
                ", made " + res["manifest_created"] if res["manifest_created"] else "",
                res["manifest_ok"][1]))
        for r in rows:
            lines.append("  {} {}: {}{} - {}".format(
                r["Scenario"], r["Raster"], r["Status"],
                " ('{}')".format(r["Manifest_Label"]) if r["Manifest_Label"] else "",
                r["Note"]))
        res["lines"] = lines
    if write and out_dir:
        os.makedirs(out_dir, exist_ok=True)
        res["csv"] = _write_csv(os.path.join(out_dir, CHECK_CSV), res["rows"])
        res["text"] = _write_text(os.path.join(out_dir, CHECK_TXT), res["lines"])
    return res


def _write_csv(path, rows):
    tmp = path + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)
    try:
        os.replace(tmp, path)
    except OSError:                       # open in Excel: keep this run's copy beside it
        base, ext = os.path.splitext(path)
        path = base + "_new" + ext
        os.replace(tmp, path)
    return path


def _write_text(path, lines):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(str(x) for x in lines) + "\n")
    return path


def read_check(path):
    """Rows of a Model_Chain_Check.csv ([] when missing / unreadable)."""
    try:
        with open(path, newline="", encoding="utf-8-sig") as fh:
            return list(csv.DictReader(fh))
    except (OSError, csv.Error):
        return []


# ------------------------------------------------------------ integration --
def pipeline_step(out_folder, dam_folder=None, log=None):
    """End-of-run hook: QA/Model_Chain_Check.csv / .txt and QA/run_manifest.json (a copy
    of the Runner manifest the classification was checked against, for the submittal
    package). Returns (summary line or "", [written paths]). NEVER raises."""
    say = log or (lambda m: None)
    try:
        res = chain_check(out_folder, dam_folder, write=True)
        written = [p for p in (res.get("csv"), res.get("text")) if p]
        qa = os.path.join(out_folder, SUBDIR_QA)
        copy = os.path.join(qa, QA_MANIFEST_COPY)
        if res.get("manifest_path") and os.path.isfile(res["manifest_path"]) and \
                os.path.normcase(os.path.abspath(res["manifest_path"])) != \
                os.path.normcase(os.path.abspath(copy)):
            shutil.copy2(res["manifest_path"], copy)
            written.append(copy)
        elif os.path.isfile(copy) and not res.get("manifest_path"):
            os.remove(copy)                  # never leave an earlier run's manifest
        say("Model chain: " + res["summary"])
        return "Model chain (ND Dam Breach Runner -> classification): " + res["summary"], \
            written
    except Exception as e:                   # an extra: never fail the run for it
        try:
            say(">>> NOTE: model chain check not done ({}: {})".format(type(e).__name__, e))
        except Exception:
            pass
        return "", []


def package_files(out_folder, dam_folder=None):
    """[(package relative path, source path, description)] the submittal package takes
    along for the chain: the Runner manifest (QA/run_manifest.json as checked at the
    classification, else the dam folder's current one - said so), QA/Model_Chain_Check
    .csv / .txt and the dam's dam.json. Missing files are left out. Never raises."""
    out = []
    try:
        qa = os.path.join(out_folder, SUBDIR_QA)
        dam = dam_folder
        if not dam:
            try:
                from . import reproduce as RP
                rec = RP.read_run_settings(os.path.join(qa, RP.RUN_SETTINGS_FILE))
                dam = (rec.get("settings") or {}).get("dam_folder")
            except (ValueError, OSError):
                dam = None
            dam = dam or os.path.dirname(os.path.abspath(out_folder))
        man = os.path.join(qa, QA_MANIFEST_COPY)
        if os.path.isfile(man):
            out.append((PACKAGE_RECORD_DIR + "/" + CH.MANIFEST_NAME, man,
                        "ND Dam Breach Runner run manifest (the one the classification was "
                        "checked against, v2.10)"))
        else:
            found = CH.find_runner_manifest(dam) if dam and os.path.isdir(dam) else None
            if found:
                out.append((PACKAGE_RECORD_DIR + "/" + CH.MANIFEST_NAME, found,
                            "ND Dam Breach Runner run manifest (found in the dam folder when "
                            "the package was made - not checked at the classification)"))
        for name, desc in ((CHECK_CSV, "Model chain check: classified rasters vs the Runner "
                                       "run manifest (SHA-256, v2.10)"),
                           (CHECK_TXT, "Model chain check summary (v2.10)")):
            p = os.path.join(qa, name)
            if os.path.isfile(p):
                out.append((PACKAGE_RECORD_DIR + "/" + name, p, desc))
        dj = os.path.join(dam, "dam.json") if dam else ""
        if dj and os.path.isfile(dj):
            out.append((PACKAGE_RECORD_DIR + "/dam.json", dj,
                        "Dam record shared by the ND dam suite (dam.json) as it was when the "
                        "package was made"))
    except Exception:
        return out
    return out


# -------------------------------------------------------- the Runner's report --
def find_run_report(dam_folder):
    """v2.28: the ND Dam Breach Runner's run_report.json for a dam folder, or None.

    Looked for in this order: the file the newest "modelled" status of <dam>/dam.json points
    at (its result's "run_report", relative to the dam folder), run_report.json next to the
    newest Runner manifest (damsuite.chain.find_runner_manifest), and
    <dam>/_breach_model/run_report.json. A report file path is returned as given. Never
    raises."""
    if not dam_folder:
        return None
    try:
        if os.path.isfile(dam_folder):
            return dam_folder if os.path.basename(dam_folder).lower() == RUN_REPORT_NAME \
                else None
        if not os.path.isdir(dam_folder):
            return None
        cands = []
        try:
            from .damsuite import dam_record as DR
            rec = DR.load(dam_folder)
            mod = rec.latest("modelled") if rec is not None else None
            res = mod.get("result") if mod is not None and isinstance(mod.get("result"), dict) \
                else {}
            rel = str(res.get("run_report") or "")
            if rel:
                cands.append(rel if os.path.isabs(rel) else os.path.join(dam_folder, rel))
        except Exception:            # a broken dam.json: the other places are still looked at
            pass
        man = CH.find_runner_manifest(dam_folder)
        if man:
            cands.append(os.path.join(os.path.dirname(man), RUN_REPORT_NAME))
        cands.append(os.path.join(dam_folder, CH.MODEL_FOLDER, RUN_REPORT_NAME))
        for p in cands:
            if os.path.isfile(p):
                return p
    except Exception:
        return None
    return None


def read_flood_cut_off(dam_folder):
    """v2.28: {"reason", "detail", "path"} when the Runner's run_report.json says the flood
    model stopped while water was still moving - report["cut_off"] = {"reason": "duration cap"
    | "domain-run limit" | "largest domain side", "detail": "..."} - and None when there is no
    report or it has no such key. Never raises."""
    path = find_run_report(dam_folder)
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            report = json.load(fh)
    except (OSError, ValueError):
        return None
    cut = report.get("cut_off") if isinstance(report, dict) else None
    if not cut:
        return None
    if not isinstance(cut, dict):                # a bare true / text: still a cut-off
        cut = {"reason": "" if isinstance(cut, bool) else str(cut), "detail": ""}
    return {"reason": str(cut.get("reason") or "").strip() or "reason not given",
            "detail": str(cut.get("detail") or "").strip(), "path": path}


def readme_line(out_folder):
    """One README line: the chain status the run recorded (QA/Model_Chain_Check.txt),
    or "" when the run wrote none."""
    p = os.path.join(out_folder, SUBDIR_QA, CHECK_TXT)
    try:
        with open(p, encoding="utf-8") as fh:
            first = fh.readline().strip()
    except OSError:
        return ""
    if not first:
        return ""
    return ("{} (02_Method_and_record/{}; the reviewer view re-checks it from the packaged "
            "files)".format(first, CHECK_CSV))
