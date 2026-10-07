# -*- coding: utf-8 -*-
"""Submittal package: the files of the last classification run that go to
the reviewer, gathered into one dated folder (optionally also one zip) with
a manifest and a cover-letter template.

    outputs/Submittal/<Dam>_<date>/
        README.txt                     what is in the package
        MANIFEST.csv                   every file, what it is, size, SHA-256
        Cover_Letter_TEMPLATE.txt      filled with the result; the engineer edits it
        01_Report/                     PDF report, summary
        02_Method_and_record/          run log, review decisions, change log, QA
        03_Results_tables/             structures, roads, rail ... (CSV / XLSX)
        04_EAP/                        map book, notification list, travel-time
        05_GIS/                        (optional) GeoPackages and zone rasters
        data/                          (v2.9, optional "include data") the files the
                                       run read: inputs/<role>/, layers/<cfg key>/,
                                       files/<key>/ (review decisions, curves ...),
                                       rasters/<scenario>/ (optional, they are big)
        Run_Settings_package.json      (with data) QA/Run_Settings.json with every
                                       packaged path relative to the package - what
                                       reproduce.py re-runs the classification from

Nothing is recalculated: the package holds the files the last run wrote, so
what is submitted is what was run. It works from the files on disk (and the
run's Last_Run_Snapshot.json for the result), so it can be made from QGIS,
a script or the hazard-creep watch alike.
"""

import csv
import glob
import hashlib
import os
import re
import shutil
import time
import zipfile

from .constants import PLUGIN_NAME, PLUGIN_VERSION, ND_POLICY_REF, SUBDIR_QA, \
    SUBDIR_CLASSIFICATION, SUBDIR_EAP

SUBMITTAL_DIR = "Submittal"
COVER_NAME = "Cover_Letter_TEMPLATE.txt"
MANIFEST_NAME = "MANIFEST.csv"
README_NAME = "README.txt"

# (folder, [(pattern relative to outputs, description, newest only?)])
_TABLE_BASES = ("Habitable_Structures", "Roads_Inundated", "Rail_Inundated", "Access_Cut_Off",
                "Dams_In_Series", "Lifeline_Facilities", "Bridges_Culverts_Crossings",
                "Future_Development_Parcels")
GROUPS = [
    ("01_Report", [
        ("Hazard_Classification_Report*.pdf", "Hazard classification report (PDF)", True),
        ("Hazard_Classification_Summary.txt", "Classification summary (text)", False),
        ("Hazard_Classification_Summary.xlsx", "Classification summary (table)", False),
    ]),
    ("02_Method_and_record", [
        ("Hazard_Classification_Run_Log.txt",
         "Run log: settings, data sources, warnings, timings", False),
        ("Structure_Review_Decisions.csv", "Engineer's structure review decisions", False),
        ("Facility_Review_Decisions.csv",
         "Engineer's road / rail / crossing / lifeline review decisions (v2.9)", False),
        (SUBDIR_QA + "/Changes_Since_Last_Run.csv", "What changed since the previous run", False),
        (SUBDIR_QA + "/Last_Run_Snapshot.json", "Snapshot of this run's result", False),
        (SUBDIR_QA + "/Inventory_QA.csv", "Structure inventory completeness check", False),
        (SUBDIR_QA + "/DSSWISE_HCOM_CrossCheck.csv",
         "Population at risk against the DSS-WISE HCOM table", False),
        (SUBDIR_QA + "/Model_Boundary_Check.csv", "Model extent check (flood at the raster edge)",
         False),
        (SUBDIR_QA + "/Model_Gate.csv", "Model gate: checks of the flood model before the "
         "classification (v2.10)", False),
        (SUBDIR_QA + "/Class_Probability.csv", "Class probability over a model ensemble "
         "(v2.10)", False),
        (SUBDIR_QA + "/Class_Probability.html", "Class probability over a model ensemble "
         "(v2.10, page)", False),
        (SUBDIR_QA + "/Class_Probability_Runs.csv", "Class of every ensemble run (v2.10)",
         False),
        (SUBDIR_QA + "/Rules_Comparison.csv", "This plugin's rules against the ND Dam Breach "
         "Runner / Screening rules, per structure (v2.10)", False),
        (SUBDIR_QA + "/Run_Settings.json",
         "Run settings: every setting, the rasters and the data sets the run read (v2.9)", False),
        (SUBDIR_QA + "/Data_Vintage.csv", "Data vintage: what the results rest on, with dates",
         False),
    ]),
    ("03_Results_tables", [
        ("{}/{}.csv".format(SUBDIR_CLASSIFICATION, b), "{} (CSV)".format(b.replace("_", " ")),
         False) for b in _TABLE_BASES] + [
        ("{}/{}.xlsx".format(SUBDIR_CLASSIFICATION, b), "{} (Excel)".format(b.replace("_", " ")),
         False) for b in _TABLE_BASES]),
    ("04_EAP", [
        (SUBDIR_EAP + "/EAP_Map_Book*.pdf", "Inundation map book (PDF)", True),
        (SUBDIR_EAP + "/EAP_Notification_List.csv", "Notification list (CSV)", False),
        (SUBDIR_EAP + "/EAP_Notification_List.xlsx", "Notification list (Excel)", False),
        (SUBDIR_EAP + "/EAP_Travel_Time.csv", "Travel-time table (CSV)", False),
        (SUBDIR_EAP + "/EAP_Travel_Time.xlsx", "Travel-time table (Excel)", False),
    ]),
]
GIS_GROUP = ("05_GIS", [
    (SUBDIR_CLASSIFICATION + "/*.gpkg", "Result layer (GeoPackage)", False),
    (SUBDIR_CLASSIFICATION + "/Hazard_*.tif", "ACER zone raster", False),
    (SUBDIR_EAP + "/*.gpkg", "EAP layer (GeoPackage)", False),
    (SUBDIR_QA + "/Structure_Evaluation_Areas.gpkg", "Area evaluated around each structure",
     False),
])


def _safe(name):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", str(name or "").strip()).strip("_")[:60] or "Dam"


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _find(out_folder, pattern, newest):
    hits = sorted(glob.glob(os.path.join(out_folder, pattern.replace("/", os.sep))))
    hits = [h for h in hits if os.path.isfile(h)]
    if newest and hits:
        hits = [max(hits, key=os.path.getmtime)]
    return hits


def collect(out_folder, include_gis=False):
    """[(group folder, source path, description)] of the files a package
    holds, in order; files that do not exist are left out."""
    groups = list(GROUPS) + ([GIS_GROUP] if include_gis else [])
    items, seen = [], set()
    for folder, specs in groups:
        for pattern, desc, newest in specs:
            for path in _find(out_folder, pattern, newest):
                key = os.path.normcase(os.path.abspath(path))
                if key in seen:
                    continue
                seen.add(key)
                items.append((folder, path, desc))
    # v2.10 (item 13): the ND Dam Breach Runner manifest the classification was checked
    # against (QA/run_manifest.json, else the dam folder's current one - described so),
    # QA/Model_Chain_Check.csv / .txt and the dam's dam.json -> 02_Method_and_record
    try:
        from . import modelchain as MC
        extra = MC.package_files(out_folder)
    except Exception:
        extra = []
    for rel, path, desc in extra:
        key = os.path.normcase(os.path.abspath(path))
        if key in seen:
            continue
        seen.add(key)
        items.append((rel.split("/")[0], path, desc))
    return items


def read_result(out_folder):
    """The last run's result (dam, ND class, TM 11 class, lives in
    jeopardy, governing scenario, time) from its snapshot, or {}."""
    from . import history as HIS
    snap = HIS.read_snapshot(os.path.join(out_folder, SUBDIR_QA)) or {}
    return {k: snap.get(k) for k in ("dam", "nd", "tm11", "lij", "governing", "time")
            if snap.get(k) is not None}


def cover_letter(result, dam_name, enclosures, when=None):
    """Text of the cover-letter template, filled with the run's result. The
    engineer completes and signs it; the classification stated is the
    plugin's screening result until the engineer confirms it."""
    when = when or time.strftime("%B %d, %Y")
    nd = str(result.get("nd") or "[ND hazard class]").upper()
    tm = str(result.get("tm11") or "[TM 11 class]").upper()
    lij = result.get("lij")
    lines = [
        "[Engineering firm letterhead]",
        "",
        when,
        "",
        "North Dakota Department of Water Resources - Dam Safety Program",
        "[address]",
        "",
        "RE: Hazard potential classification - {}".format(dam_name or "[dam name]"),
        "    NID ID: [NID ID]      State file no.: [file number]",
        "",
        "Dear [reviewer]:",
        "",
        "Enclosed is the hazard potential classification submittal for {}, prepared under "
        "{}.".format(dam_name or "[dam name]", ND_POLICY_REF),
        "",
        "Result of the analysis run on {}:".format(result.get("time") or "[date]"),
        "  ND hazard potential classification (screening): {}".format(nd),
        "  USBR ACER TM 11 classification (screening):     {}{}".format(
            tm, "  (lives in jeopardy {:.1f})".format(lij) if lij is not None else ""),
        "  Governing breach scenario: {}".format(result.get("governing") or "[scenario]"),
        "",
        "[State the classification proposed, the engineering judgment applied to the items "
        "listed in the report, and any structures reviewed in the field.]",
        "",
        "Enclosures:",
    ] + ["  - {}".format(e) for e in enclosures] + [
        "",
        "Please contact me with any questions.",
        "",
        "Sincerely,",
        "",
        "[Name, P.E.]",
        "[License no. / state]",
        "[Telephone / e-mail]",
        "",
        "-- The screening results above were produced by {} v{}. They are not the "
        "classification until the engineer of record adopts them. --".format(
            PLUGIN_NAME, PLUGIN_VERSION),
    ]
    return "\n".join(lines) + "\n"


def readme(dam_name, result, rows, when, profile=None, data=None, chain=""):
    lines = ["Hazard classification submittal package - {}".format(dam_name or "dam"),
             "Made {} by {} v{} from the files of the last classification run".format(
                 when, PLUGIN_NAME, PLUGIN_VERSION), ""]
    if result:
        lines += ["Run of {}: ND {} / TM 11 {} (governing scenario {})".format(
            result.get("time") or "?", result.get("nd") or "?", result.get("tm11") or "?",
            result.get("governing") or "?"), ""]
    if profile:
        lines += ["Settings profile: '{}' (profile hash sha256 {}){}".format(
            profile.get("name") or "?", profile.get("sha256") or "?",
            " - the settings were changed after the profile was loaded: {}".format(
                ", ".join(profile.get("changed_keys") or [])) if profile.get("modified")
            else ""), ""]
    if chain:
        lines += [chain, ""]
    if data is not None:
        lines += ["Data for reproducing the run: {} file(s) in data/, settings in {}{}.".format(
            data.get("files", 0), data.get("settings_file") or "-",
            "" if data.get("rasters") else " (flood rasters NOT included - the reviewer needs "
            "them separately)"),
            "  To re-run the classification and check that the same result comes out: "
            "reproduce.reproduce(<this folder>, <work folder>[, rasters_folder=...]) - it "
            "writes Reproduction_Check.csv."]
        for n in data.get("not_packaged") or []:
            lines.append("  NOT packaged: {} ({}) - {}".format(n.get("what"), n.get("path"),
                                                             n.get("why")))
        for c in data.get("changed") or []:
            lines.append("  CHANGED since the run: {} ({}) - the package holds the file as it "
                         "is now, not as the run read it".format(c.get("what"), c.get("path")))
        lines.append("")
    lines += ["Files (also in {}, with SHA-256):".format(MANIFEST_NAME)]
    lines += ["  {}  -  {}".format(r["Path"], r["Description"]) for r in rows]
    lines += ["", "Cover_Letter_TEMPLATE.txt is a template: complete and sign it. Nothing here "
              "was recalculated for the package - it is what the last run wrote."]
    return "\n".join(lines) + "\n"


def _chain_line(out_folder):
    """v2.10: the README line of the model chain check the run recorded, or ""."""
    try:
        from .modelchain import readme_line
        return readme_line(out_folder)
    except Exception:
        return ""


def _profile_info(out_folder, profile):
    """(record, source file or None) of the settings profile to name in the
    package: `profile` = a profile file path or its meta / status dict (with
    "name", "sha256", optional "path"); None: the one the run recorded in
    QA/Run_Settings.json ("settings_profile"), if any."""
    if profile is None:
        try:
            from .reproduce import read_run_settings, RUN_SETTINGS_FILE
            rs = read_run_settings(os.path.join(out_folder, SUBDIR_QA, RUN_SETTINGS_FILE))
            profile = rs.get("settings_profile") or None
        except (ValueError, OSError):
            profile = None
        if not isinstance(profile, dict):
            return None, None
    if isinstance(profile, str):
        from .settings import load_profile
        _state, meta = load_profile(profile)
        return ({"name": meta["name"], "sha256": meta["sha256"], "note": meta.get("note", ""),
                 "created": meta.get("created", ""), "path": meta["path"]}, meta["path"])
    rec = {k: profile.get(k) for k in ("name", "sha256", "note", "created", "modified",
                                       "changed_keys", "path") if profile.get(k) is not None}
    src = rec.get("path") if rec.get("path") and os.path.isfile(rec["path"]) else None
    return rec, src


def build(out_folder, dam_name=None, zip_it=True, include_gis=False, dest_root=None,
          when_ts=None, profile=None, include_data=False, include_rasters=False):
    """Make the package. Returns {"folder", "zip", "files", "missing", "result",
    "profile", "data", "warnings"} where "missing" lists what a submittal
    normally holds and this run did not write (e.g. no report because
    report_pdf was off). Raises ValueError when the outputs folder holds no
    classification run at all.

    v2.9: `profile` (a settings-profile path, or its meta / status dict; None =
    the one QA/Run_Settings.json records) is named in the README and the
    manifest (its file is copied to 02_Method_and_record when it exists).
    `include_data`: the files the run read (QA/Run_Settings.json "inputs",
    project layers, review decisions ...) are copied to data/ with
    Run_Settings_package.json (reproduce.py); `include_rasters` adds the flood
    rasters (they are big). Without QA/Run_Settings.json (a run of an older
    version) nothing can be packaged: listed in "missing"."""
    if not out_folder or not os.path.isdir(out_folder):
        raise ValueError("Outputs folder not found: {}".format(out_folder))
    items = collect(out_folder, include_gis=include_gis)
    if not any(os.path.basename(p).startswith("Hazard_Classification_") for _g, p, _d in items):
        raise ValueError("No classification run in {} - run the Hazard Classification "
                         "first".format(out_folder))
    result = read_result(out_folder)
    dam = dam_name or result.get("dam") or "Dam"
    prof, prof_src = _profile_info(out_folder, profile)
    stamp = time.strftime("%Y-%m-%d", time.localtime(when_ts)) if when_ts else \
        time.strftime("%Y-%m-%d")
    root = dest_root or os.path.join(out_folder, SUBMITTAL_DIR)
    base = "{}_{}".format(_safe(dam), stamp)
    folder = os.path.join(root, base)
    n = 1
    while os.path.exists(folder) or os.path.exists(folder + ".zip"):
        n += 1
        folder = os.path.join(root, "{}_{}".format(base, n))
    os.makedirs(folder)
    rows = []
    data_missing, warnings = [], []
    data = None

    def add_row(rel, src, desc):
        dest = os.path.join(folder, *rel.split("/"))
        rows.append({"Path": rel, "Description": desc, "Size_bytes": os.path.getsize(dest),
                     "SHA256": _sha256(dest),
                     "Source_modified": time.strftime(
                         "%Y-%m-%d %H:%M", time.localtime(os.path.getmtime(src)))
                     if src and os.path.exists(src) else ""})
    try:
        for group, src, desc in items:
            dest_dir = os.path.join(folder, group)
            os.makedirs(dest_dir, exist_ok=True)
            dest = os.path.join(dest_dir, os.path.basename(src))
            shutil.copy2(src, dest)
            add_row("{}/{}".format(group, os.path.basename(src)), src, desc)
        if prof:
            if prof_src:
                name = "Settings_Profile_{}.json".format(_safe(prof.get("name")))
                dest_dir = os.path.join(folder, "02_Method_and_record")
                os.makedirs(dest_dir, exist_ok=True)
                shutil.copy2(prof_src, os.path.join(dest_dir, name))
                add_row("02_Method_and_record/" + name, prof_src,
                        "Settings profile '{}' used for the run (profile hash {})".format(
                            prof.get("name"), prof.get("sha256")))
            else:
                warnings.append("settings profile '{}' (hash {}) recorded, its file is not "
                                "at hand".format(prof.get("name"), prof.get("sha256")))
        if include_data:
            data = _package_data(out_folder, folder, include_rasters, data_missing, warnings,
                                 add_row)
        enclosures = [r["Description"] for r in rows if r["Path"].startswith(
            ("01_Report", "04_EAP")) or "Review_Decisions" in r["Path"]
            or "Run_Log" in r["Path"]]
        with open(os.path.join(folder, COVER_NAME), "w", encoding="utf-8") as fh:
            fh.write(cover_letter(result, dam, enclosures))
        with open(os.path.join(folder, MANIFEST_NAME), "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=["Path", "Description", "Size_bytes", "SHA256",
                                               "Source_modified"])
            w.writeheader()
            w.writerows(rows)
        with open(os.path.join(folder, README_NAME), "w", encoding="utf-8") as fh:
            fh.write(readme(dam, result, rows, time.strftime("%Y-%m-%d %H:%M"), prof, data,
                            chain=_chain_line(out_folder)))
        zpath = None
        if zip_it:
            zpath = folder + ".zip"
            with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
                for dirpath, _d, files in os.walk(folder):
                    for f in sorted(files):
                        full = os.path.join(dirpath, f)
                        zf.write(full, os.path.join(os.path.basename(folder),
                                                    os.path.relpath(full, folder)))
    except Exception:
        shutil.rmtree(folder, ignore_errors=True)         # never leave half a package
        if zip_it and os.path.exists(folder + ".zip"):
            try:
                os.remove(folder + ".zip")
            except OSError:
                pass
        raise
    have = {r["Path"].split("/")[0] + "/" + os.path.basename(r["Path"]) for r in rows}
    missing = []
    if not any(p.endswith(".pdf") and p.startswith("01_Report") for p in have):
        missing.append("PDF report (the run had 'PDF report' off, or it failed - see the run "
                       "log)")
    if not any(p.startswith("02_Method_and_record/Structure_Review") for p in have):
        missing.append("engineer review decisions (none saved yet)")
    if not any(p.startswith("04_EAP/EAP_Map_Book") for p in have):
        missing.append("EAP inundation map book (off, or no arrival raster)")
    missing.extend(data_missing)
    return {"folder": folder, "zip": zpath, "files": rows, "missing": missing,
            "result": result, "profile": prof, "data": data, "warnings": warnings}


def _package_data(out_folder, folder, include_rasters, missing, warnings, add_row):
    """Copy the run's inputs into the package (reproduce.package_inputs) and add
    their manifest rows. Returns the summary for the README / the caller."""
    from . import reproduce as RP
    spath = os.path.join(out_folder, SUBDIR_QA, RP.RUN_SETTINGS_FILE)
    try:
        rs = RP.read_run_settings(spath)
    except ValueError as e:
        missing.append("data for reproducing the run: no usable QA/{} ({}) - run the "
                       "classification again with this version".format(
                           RP.RUN_SETTINGS_FILE, e))
        return None
    got = RP.package_inputs(rs, folder, include_rasters=include_rasters)
    for rel, src, desc in got["rows"]:
        add_row(rel, src, desc)
    add_row(got["settings_file"], None,
            "Run settings with the packaged paths (for reproduce.py)")
    for n in got["not_packaged"]:
        if "rasters not included" not in n.get("why", ""):
            warnings.append("not packaged: {} ({}) - {}".format(n["what"], n["path"],
                                                                 n["why"]))
    for c in got["changed"]:
        warnings.append("changed since the run: {} ({}) - the package holds the file as it "
                        "is now".format(c["what"], c["path"]))
    return {"files": len(got["rows"]), "settings_file": got["settings_file"],
            "rasters": bool(include_rasters), "not_packaged": got["not_packaged"],
            "changed": got["changed"]}
