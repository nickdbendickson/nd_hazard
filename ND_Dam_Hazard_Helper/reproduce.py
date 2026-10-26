# -*- coding: utf-8 -*-
"""v2.9: reproduce a past classification run from its submittal package.

A reviewer (or the engineer, years later) re-runs the classification with
the SAME settings, rasters and data the run read, and checks that the same
result comes out. Three parts:

1. The record. At the end of every run the pipeline writes
   outputs/QA/Run_Settings.json = run_settings_dict(cfg, used_inputs):
     "settings"      every scalar cfg key (pipeline.DEFAULTS and any other
                     JSON-able key: local_data, settings_profile ...);
     "layers"        each cfg key holding a layer (road_layer, osm_layer,
                     pop_source ...): its source, file path, provider, name;
     "point_sources" "auto" or [{"family", "layer": {...}}];
     "scenarios"     the scenario rasters (paths as the engineer gave them);
     "inputs"        {role: path, or [path | [path, category], ...]}: the data
                     sets the run actually read (download cache files in
                     outputs/_downloads, project layers' files). Roles:
                     INPUT_ROLES;
     "inputs_format" 2 (INPUTS_FORMAT): "inputs" holds each role's files in the
                     order the run read them, and only those (both forks:
                     DamSafety Hazard 2.18.62, ND Hazard 2.34.5 and later); a
                     record without it lists every download in the order the
                     downloads finished (_old_inputs_as_run);
     "files"         other files the result depends on: the review decisions
                     (review_file; also when none existed: "exists" False),
                     the damage curves, the HCOM tables, the peak raster;
     "fingerprints"  {path: size, modified, sha256} of every file above, taken
                     when the run ended (files > HASH_MAX_BYTES: no sha256).
2. The package. submittal.build(..., include_data=True) copies the settings,
   every file of "inputs" / "layers" / "files" (and with include_rasters the
   scenario rasters) into <package>/data/..., and writes
   <package>/Run_Settings_package.json: the same record with each packaged
   path made relative to the package ("data/inputs/nsi/NSI_x.geojson") plus
   a "package" block (what was copied, what could not be, what changed on disk
   since the run). package_inputs() below does the copying.
3. The check. cfg_from_package() turns the package into the settings of a
   new run (downloads off, the package's copies as the data, output into a
   work folder, nothing added to the project, no PDF); reproduce() runs it and
   compares the new outputs/QA/Last_Run_Snapshot.json with the package's
   snapshot using history.diff - the comparison Changes_Since_Last_Run.csv
   uses - and writes Reproduction_Check.csv (+ .txt): "IDENTICAL" or the list
   of differences, the inputs missing from the package, files of the package
   changed since it was made (MANIFEST.csv hashes).

The data are handed to the new run as cfg["local_data"] = {role: path or
[[path, category], ...]} (read by the pipeline instead of downloading). With a
pipeline that does not know "local_data" yet (pipeline.DEFAULTS has no such
key) the packaged files are handed over as layers instead, where a cfg key
exists for the role (ROLE_CFG_KEYS); roles without one (campgrounds, bridges,
agency lifeline downloads) are then reported as not reproducible.

Nothing here needs QGIS except building the layers / scenarios of the new
run (imported when needed).
"""

import csv
import datetime
import hashlib
import json
import os
import re
import shutil
import time
import zipfile
import xml.etree.ElementTree as ET

from . import history as HIS
from .constants import PLUGIN_NAME, PLUGIN_VERSION, SUBDIR_QA, LIFELINE_SOURCES

RUN_SETTINGS_FILE = "Run_Settings.json"
PACKAGE_SETTINGS_FILE = "Run_Settings_package.json"
CHECK_FILE = "Reproduction_Check.csv"
CHECK_TEXT = "Reproduction_Check.txt"
RUN_SETTINGS_FORMAT = "ND_Dam_Hazard_Helper/run_settings"
FORMAT_VERSION = 1
# 2.34.5: "inputs_format" of a record whose "inputs" are as the run read them (both forks write
# it; a record without it - of either fork - is put in run order when it is read)
INPUTS_FORMAT = 2
HASH_MAX_BYTES = 1 << 30           # 1 GB: bigger files get size + date only
DATA_DIR = "data"
RECORD_DIR = "02_Method_and_record"
CHECK_FIELDS = ["Kind", "Change", "Item", "Package", "Reproduction", "X", "Y"]
NO_REVIEW_FILE = "_no_review_decisions_at_run_time.csv"
NO_FACILITY_REVIEW_FILE = "_no_facility_review_decisions_at_run_time.csv"

# roles of the data sets a run reads (pipeline._collect_inputs), with a label
INPUT_ROLES = (
    ("nsi", "USACE National Structure Inventory points"),
    ("address", "ND GIS Hub site-structure address points"),
    ("usa", "FEMA USA Structures outlines"),
    ("outlines", "Building outlines (FEMA_Structures file / project layer)"),
    ("camps", "OpenStreetMap campgrounds / RV parks"),
    ("roads", "Roads"),
    ("aadt", "Traffic counts (AADT)"),
    ("rail", "Rail lines"),
    ("crossings", "Highway-rail grade crossings"),
    ("bridges", "Bridges, culverts and crossings (NBI / OSM)"),
    ("dams", "Dam inventory (NID / project layer)"),
    ("lifelines", "Lifeline / critical facilities"),
    ("boundary", "Model boundary"),
    ("parcels", "Parcels (future development)"),
    ("centerline", "Channel centerline / flowlines"),
)
ROLE_LABELS = dict(INPUT_ROLES)
# a role handed to the pipeline as a layer when it has no cfg["local_data"] support
ROLE_CFG_KEYS = {"roads": "road_layer", "aadt": "aadt_layer", "rail": "rail_layer",
                 "crossings": "crossing_layer", "outlines": "osm_layer",
                 "usa": "extra_outline_layer", "dams": "dam_layer",
                 "boundary": "model_boundary_layer", "parcels": "parcel_layer",
                 "centerline": "centerline_layer"}
ROLE_POINT_FAMILIES = {"nsi": "NSI", "address": "NDGISHUB"}
FILE_KEYS = (("review_file", "Engineer review decisions"),
             ("facility_review_file", "Engineer review decisions - roads, rail, facilities"),
             ("damage_curves_file", "Non-residential depth-damage curves"),
             ("hcom_files", "DSS-WISE HCOM table"),
             ("peak_raster", "Time-of-peak raster"))
RASTER_KEYS = (("depth", "depth"), ("velocity", "velocity"), ("arrival", "arrival"),
               ("dv", "depth x velocity"))
# the reproduction never downloads, adds layers, makes the PDF or re-uses caches
_REPRO_OVERRIDES = {"add_to_project": False, "report_pdf": False, "eap_mapbook": False,
                    "quick_rerun": False, "reuse_evaluation": False, "save_evaluation": False,
                    "compare_last_run": True, "data_cache_folder": "", "refresh_data": False,
                    # v2.10: a reproduction never writes the dam's dam.json; the model gate
                    # does not stop it (its result is compared, not judged)
                    "write_dam_record": False, "gate_stop_on_fail": False}
# path-valued settings replaced by the reproduction (never read from the record)
_PATH_SETTINGS = ("output_folder", "dam_folder", "review_file", "facility_review_file",
                  "damage_curves_file",
                  "hcom_files", "peak_raster", "local_data")
SIDECAR_EXTS = (".shx", ".dbf", ".prj", ".cpg", ".qix", ".sbn", ".sbx", ".shp.xml", ".qmd",
                ".tfw", ".tifw", ".wld", ".hdr", ".aux", ".rrd")
SIDECAR_SUFFIXES = (".aux.xml", ".ovr", ".xml")


# ------------------------------------------------------------------ helpers --
def _now_iso():
    return datetime.datetime.now().replace(microsecond=0).isoformat()


def sha256_file(path, limit=HASH_MAX_BYTES):
    """sha256 of a file, or None (a folder, missing, or bigger than limit)."""
    try:
        if not os.path.isfile(path) or (limit and os.path.getsize(path) > limit):
            return None
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def fingerprint(path, hash_files=True):
    """{path, exists, kind, size, modified, sha256} of a file or folder."""
    rec = {"path": path, "exists": False, "kind": "", "size": None, "modified": "",
           "sha256": None}
    try:
        if os.path.isdir(path):
            size = 0
            for dp, _d, files in os.walk(path):
                for f in files:
                    try:
                        size += os.path.getsize(os.path.join(dp, f))
                    except OSError:
                        pass
            rec.update({"exists": True, "kind": "folder", "size": size})
        elif os.path.isfile(path):
            rec.update({"exists": True, "kind": "file", "size": os.path.getsize(path),
                        "sha256": sha256_file(path) if hash_files else None})
        if rec["exists"]:
            rec["modified"] = time.strftime("%Y-%m-%d %H:%M:%S",
                                            time.localtime(os.path.getmtime(path)))
    except OSError:
        pass
    return rec


def split_source(source):
    """(file path or "", suffix) of a layer source / input spec: "a.gpkg|layername=x"
    -> ("a.gpkg", "|layername=x"); "GPKG:a.gpkg:table" -> ("a.gpkg", "GPKG:table"); a
    delimited-text URI "file:///C:/a.csv?delimiter=," -> ("C:/a.csv", "?delimiter=,")."""
    s = str(source or "").strip()
    if not s:
        return "", ""
    if s.lower().startswith("file:"):
        from urllib.parse import urlsplit, unquote
        u = urlsplit(s)
        p = unquote(u.path)
        if u.netloc and u.netloc.lower() != "localhost":
            p = "//" + u.netloc + p                  # UNC: file://server/share/...
        elif re.match(r"^/[A-Za-z]:[/\\]", p):
            p = p[1:]
        return p, ("?" + u.query if u.query else "?")
    m = re.match(r"^/vsizip/(.+?\.zip)((?:/[^|]*)?)(\|.*)?$", s, re.IGNORECASE)
    if m:                                            # a layer inside a zip archive
        return m.group(1), "VSIZIP:" + (m.group(2) or "") + (m.group(3) or "")
    m = re.match(r"^GPKG:(.+):([^:\\/]+)$", s)
    if m and os.path.splitext(m.group(1))[1].lower() == ".gpkg":
        return m.group(1), "GPKG:" + m.group(2)
    path, bar, rest = s.partition("|")
    if "?" in path and not bar:                  # a packaged delimited-text source
        path, q = path.split("?", 1)
        return path, "?" + q
    return path, (bar + rest if bar else "")


def join_source(path, suffix, uri=True):
    """Inverse of split_source for another file path (uri False: a package-
    relative path, a "?query" suffix is appended as it is)."""
    suffix = suffix or ""
    if suffix.startswith("GPKG:"):
        return "GPKG:{}:{}".format(path, suffix[5:])
    if suffix.startswith("VSIZIP:"):
        return "/vsizip/" + str(path).replace("\\", "/") + suffix[7:]
    if suffix.startswith("?") and not uri:
        return path + suffix
    if suffix.startswith("?"):
        import pathlib
        return pathlib.Path(os.path.abspath(path)).as_uri() + suffix
    return path + suffix


_NOT_FILE_PROVIDERS = ("memory", "virtual", "postgres", "wfs", "oapif", "arcgisfeatureserver",
                       "arcgismapserver", "xyz", "wms", "wcs", "oracle", "mssql", "hana")


def _layer_record(lyr):
    try:
        src = lyr.source()
    except Exception:
        src = ""
    prov = ""
    for fn in (lambda: lyr.providerType(), lambda: lyr.dataProvider().name()):
        try:
            prov = str(fn() or "")
            if prov:
                break
        except Exception:
            continue
    kind = "raster" if (hasattr(lyr, "bandCount") or "Raster" in type(lyr).__name__) \
        else "vector"
    try:
        name = lyr.name()
    except Exception:
        name = ""
    path, suffix = split_source(src)
    if prov.lower() in _NOT_FILE_PROVIDERS:
        path = ""
    return {"name": name, "source": src, "provider": prov, "kind": kind,
            "path": path if path and os.path.exists(path) else "", "suffix": suffix,
            "file_note": "" if path and os.path.exists(path) else
            ("not a file ({} layer)".format(prov or "unknown") if not path
             else "file not found")}


def _is_layer(v):
    return v is not None and callable(getattr(v, "source", None)) and \
        callable(getattr(v, "name", None))


def _json_safe(v):
    try:
        json.dumps(v)
        return True
    except (TypeError, ValueError):
        return False


def _scenario_dicts(cfg, scenarios):
    out = []
    for sc in (scenarios if scenarios is not None else cfg.get("scenarios") or []):
        d = dict(sc) if isinstance(sc, dict) else (sc.to_dict() if hasattr(sc, "to_dict")
                                                   else {})
        if not isinstance(sc, dict) and getattr(sc, "orig_depth", None):
            d["depth"] = sc.orig_depth           # stage A may have reprojected it
        out.append({k: d.get(k) for k in ("name", "role", "baseline", "depth", "velocity",
                                          "arrival", "dv", "source")})
    return out


def _input_paths(spec):
    """[(path, category or None)] of one role's input spec: a path, or a list of
    paths and / or (path, category) pairs."""
    if spec is None or spec == "":
        return []
    if isinstance(spec, (list, tuple)):
        out = []
        for item in spec:
            if isinstance(item, (list, tuple)):
                if item and item[0]:
                    out.append((str(item[0]), item[1] if len(item) > 1 else None))
            elif item:
                out.append((str(item), None))
        return out
    return [(str(spec), None)]


# ------------------------------------------------------------- the record ---
def run_settings_dict(cfg, used_inputs=None, profile=None, scenarios=None, hash_files=True,
                      evaluation=None):
    """JSON-safe record of a run (see the module docstring). `used_inputs`:
    {role: path | [path | (path, category), ...]} of the data sets the run read;
    `profile`: the settings profile record (settings.profile_status) if any -
    else cfg["settings_profile"]; `scenarios`: the scenario list as the run
    STARTED (stage A may reproject rasters and change the Scenario objects);
    `evaluation` (v2.12): the "evaluation" block - the evaluation key and the
    content digests the re-use decision rests on (pipeline._evaluation_record)."""
    cfg = dict(cfg or {})
    out = {"format": RUN_SETTINGS_FORMAT, "format_version": FORMAT_VERSION,
           "plugin": PLUGIN_NAME, "plugin_version": PLUGIN_VERSION, "created": _now_iso(),
           "inputs_format": INPUTS_FORMAT,
           "dam_name": cfg.get("dam_name") or os.path.basename(os.path.normpath(
               cfg.get("dam_folder") or cfg.get("output_folder") or "")) or "Dam",
           "settings": {}, "layers": {}, "point_sources": "auto", "scenarios": [],
           "inputs": {}, "files": {}, "fingerprints": {}, "skipped": {}, "warnings": []}
    paths = []
    for k in sorted(cfg):
        v = cfg[k]
        if k in ("scenarios", "settings_profile"):
            continue
        if k == "point_sources":
            if isinstance(v, (list, tuple)):
                ps = []
                for item in v:
                    try:
                        fam, lyr = item
                    except (TypeError, ValueError):
                        continue
                    rec = _layer_record(lyr) if _is_layer(lyr) else {}
                    ps.append({"family": str(fam), "layer": rec})
                    if rec.get("path"):
                        paths.append(rec["path"])
                out["point_sources"] = ps
            else:
                out["point_sources"] = v if isinstance(v, str) else "auto"
            continue
        if _is_layer(v):
            rec = _layer_record(v)
            out["layers"][k] = rec
            if rec["path"]:
                paths.append(rec["path"])
            continue
        if isinstance(v, float) and v != v:
            out["settings"][k] = None
        elif v is None or isinstance(v, (str, int, float, bool)):
            out["settings"][k] = v
        elif isinstance(v, (list, tuple, dict)) and _json_safe(v):
            out["settings"][k] = v if not isinstance(v, tuple) else list(v)
        else:
            out["skipped"][k] = type(v).__name__
    sc = _scenario_dicts(cfg, scenarios)
    out["scenarios"] = sc
    for s in sc:
        for key, _label in RASTER_KEYS:
            if s.get(key):
                paths.append(s[key])
                if "_reprojected" in str(s[key]).replace("\\", "/").split("/"):
                    out["warnings"].append("scenario '{}' {} raster is a reprojected copy - "
                                           "record the scenarios before the run starts".format(
                                               s.get("name"), key))
    for role in sorted(used_inputs or {}):
        spec = (used_inputs or {})[role]
        items = _input_paths(spec)
        if not items:
            continue
        if isinstance(spec, (list, tuple)):
            out["inputs"][role] = [[p, c] if c is not None else p for p, c in items]
        else:
            out["inputs"][role] = items[0][0]
        paths.extend(split_source(p)[0] for p, _c in items)
    # other files the result rests on
    outf = cfg.get("output_folder") or ""
    review = cfg.get("review_file") or (os.path.join(outf, "Structure_Review_Decisions.csv")
                                        if outf else "")
    files = {}
    if review:
        files["review_file"] = [review]
    freview = cfg.get("facility_review_file") or (
        os.path.join(outf, "Facility_Review_Decisions.csv") if outf else "")
    if freview:
        files["facility_review_file"] = [freview]
    if cfg.get("damage_curves_file"):
        files["damage_curves_file"] = [str(cfg["damage_curves_file"]).strip()]
    if cfg.get("hcom_files"):
        files["hcom_files"] = [p.strip() for p in str(cfg["hcom_files"]).split(";")
                               if p.strip()]
    if cfg.get("peak_raster") and isinstance(cfg["peak_raster"], str):
        files["peak_raster"] = [cfg["peak_raster"]]
    out["files"] = files
    for lst in files.values():
        paths.extend(lst)
    prof = profile if profile is not None else cfg.get("settings_profile")
    if prof:
        out["settings_profile"] = prof if _json_safe(prof) else str(prof)
    if evaluation and _json_safe(evaluation):
        out["evaluation"] = evaluation
    seen = set()
    for p in paths:
        p = split_source(p)[0]
        if not p or p in seen:
            continue
        seen.add(p)
        out["fingerprints"][p] = fingerprint(p, hash_files)
    return out


def write_run_settings(folder, cfg, used_inputs=None, profile=None, scenarios=None,
                       hash_files=True, evaluation=None):
    """Write <folder>/Run_Settings.json (folder: outputs/QA). Returns its path."""
    data = run_settings_dict(cfg, used_inputs, profile, scenarios, hash_files,
                             evaluation=evaluation)
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, RUN_SETTINGS_FILE)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1, ensure_ascii=False)
    os.replace(tmp, path)
    return path


def read_run_settings(path):
    """The record of a run (dict). Raises ValueError when it is not one."""
    try:
        with open(path, encoding="utf-8-sig") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as e:
        raise ValueError("run settings {} could not be read: {}".format(path, e))
    if not isinstance(data, dict) or data.get("format") != RUN_SETTINGS_FORMAT:
        raise ValueError("{} is not a run-settings record of this plugin".format(path))
    try:
        fv = int(data.get("format_version") or 0)
    except (TypeError, ValueError):
        fv = 0
    if fv > FORMAT_VERSION:
        raise ValueError("{} was written by a newer plugin version ({}) - update the "
                         "plugin".format(path, data.get("plugin_version")))
    return data


# ------------------------------------------------------------ the package ---
def _safe(name):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", str(name or "").strip()).strip("_")[:60] or "x"


def _sidecars(path):
    stem, ext = os.path.splitext(path)
    out = []
    for e in SIDECAR_EXTS:
        for cand in (stem + e, stem + e.upper()):
            if cand != path and os.path.isfile(cand):
                out.append(cand)
                break
    for s in SIDECAR_SUFFIXES:
        if os.path.isfile(path + s):
            out.append(path + s)
    if ext.lower() == ".vrt":
        out.extend(_vrt_relative_sources(path))
    return sorted(set(out))


def _vrt_relative_sources(path):
    """Files a VRT names relative to itself (relativeToVRT="1")."""
    out = []
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError):
        return out
    base = os.path.dirname(path)
    for el in root.iter("SourceFilename"):
        if str(el.get("relativeToVRT", "0")) == "1" and el.text:
            p = os.path.normpath(os.path.join(base, el.text.strip()))
            if os.path.isfile(p):
                out.append(p)
    return out


def _vrt_absolute_sources(path):
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError):
        return []
    return [el.text.strip() for el in root.iter("SourceFilename")
            if str(el.get("relativeToVRT", "0")) != "1" and el.text]


class _Packer(object):
    """Copies files into <package>/data/<sub>/..., once per source file."""

    def __init__(self, package_folder):
        self.root = package_folder
        self.done = {}              # normcase(abs source) -> relative main path
        self.rows = []              # (relative path, source path, description)

    def rel(self, *parts):
        return "/".join(p.strip("/\\") for p in parts if p)

    def _free(self, rel):
        base, ext = os.path.splitext(rel)
        n, cand = 1, rel
        while os.path.exists(os.path.join(self.root, *cand.split("/"))):
            n += 1
            cand = "{}_{}{}".format(base, n, ext)
        return cand

    def copy(self, src, sub, desc):
        """Relative path of the packaged copy of `src` (a file or a folder such
        as a .gdb), or None when it does not exist."""
        key = os.path.normcase(os.path.abspath(src))
        if key in self.done:
            return self.done[key]
        if not src or not os.path.exists(src):
            return None
        rel = self._free(self.rel(DATA_DIR, sub, os.path.basename(os.path.normpath(src))))
        dest = os.path.join(self.root, *rel.split("/"))
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        if os.path.isdir(src):
            shutil.copytree(src, dest)
            for dp, _d, files in os.walk(dest):
                for f in sorted(files):
                    full = os.path.join(dp, f)
                    self.rows.append((os.path.relpath(full, self.root).replace(os.sep, "/"),
                                      os.path.join(src, os.path.relpath(full, dest)), desc))
        else:
            shutil.copy2(src, dest)
            self.rows.append((rel, src, desc))
            dest_dir = os.path.dirname(dest)
            stem_src = os.path.splitext(os.path.basename(src))[0]
            stem_dst = os.path.splitext(os.path.basename(dest))[0]
            for side in _sidecars(src):
                srel = os.path.relpath(side, os.path.dirname(src))
                if srel.startswith(".."):
                    continue
                name = srel
                if os.path.basename(srel).startswith(stem_src) and os.sep not in srel:
                    name = stem_dst + srel[len(stem_src):]
                sd = os.path.join(dest_dir, name)
                os.makedirs(os.path.dirname(sd), exist_ok=True)
                shutil.copy2(side, sd)
                self.rows.append((os.path.relpath(sd, self.root).replace(os.sep, "/"), side,
                                  desc + " (companion file)"))
        self.done[key] = rel
        return rel


def _suffix_of(rec):
    """The source suffix of a layer record ("|layername=..." ...)."""
    if "suffix" in rec:
        return rec.get("suffix") or ""
    return split_source(rec.get("source") or "")[1]


def _changed_since_run(src, fp):
    """True when `src` on disk is not the file the run read (by sha256, or size
    and date when no hash was taken)."""
    if not fp or not fp.get("exists"):
        return False
    now = fingerprint(src, hash_files=bool(fp.get("sha256")))
    if fp.get("sha256") and now.get("sha256"):
        return fp["sha256"] != now["sha256"]
    return (fp.get("size"), fp.get("modified")) != (now.get("size"), now.get("modified"))


def package_inputs(run_settings, package_folder, include_rasters=False):
    """Copy what the run read into <package>/data and write
    <package>/Run_Settings_package.json. Returns {"rows": [(relative path,
    source, description)], "settings_file": relative path, "not_packaged":
    [{"what", "path", "why"}], "changed": [{"what", "path"}]}."""
    rs = json.loads(json.dumps(run_settings))          # deep copy
    fps = rs.get("fingerprints") or {}
    pk = _Packer(package_folder)
    not_packaged, changed = [], []

    def put(path, sub, desc, what):
        src, suffix = split_source(path)
        if not src:
            not_packaged.append({"what": what, "path": path, "why": "not a file"})
            return path
        if not os.path.exists(src):
            not_packaged.append({"what": what, "path": src, "why": "file not found now"})
            return path
        if _changed_since_run(src, fps.get(src)):
            changed.append({"what": what, "path": src})
        rel = pk.copy(src, sub, desc)
        return join_source(rel, suffix, uri=False) if rel else path

    for role, spec in sorted((rs.get("inputs") or {}).items()):
        label = ROLE_LABELS.get(role, role)
        desc = "Input data read by the run: {}".format(label)
        if isinstance(spec, list):
            new = []
            for item in spec:
                if isinstance(item, list):
                    new.append([put(item[0], "inputs/" + _safe(role), desc,
                                    "input '{}'".format(role))] + item[1:])
                else:
                    new.append(put(item, "inputs/" + _safe(role), desc,
                                   "input '{}'".format(role)))
            rs["inputs"][role] = new
        else:
            rs["inputs"][role] = put(spec, "inputs/" + _safe(role), desc,
                                     "input '{}'".format(role))
    for key, rec in sorted((rs.get("layers") or {}).items()):
        what = "layer {} ('{}')".format(key, rec.get("name"))
        if not rec.get("path"):
            not_packaged.append({"what": what, "path": rec.get("source") or "",
                                 "why": rec.get("file_note") or "not a file"})
            continue
        rel = put(rec["path"], "layers/" + _safe(key),
                  "Project layer '{}' ({})".format(rec.get("name"), key), what)
        if rel != rec["path"]:
            rec["path"] = rel
            rec["source"] = join_source(rel, _suffix_of(rec), uri=False)
    for item in rs.get("point_sources") if isinstance(rs.get("point_sources"), list) else []:
        rec = item.get("layer") or {}
        what = "point layer {} ('{}')".format(item.get("family"), rec.get("name"))
        if not rec.get("path"):
            not_packaged.append({"what": what, "path": rec.get("source") or "",
                                 "why": rec.get("file_note") or "not a file"})
            continue
        rel = put(rec["path"], "layers/point_sources",
                  "Point layer '{}' ({})".format(rec.get("name"), item.get("family")), what)
        if rel != rec["path"]:
            rec["path"] = rel
            rec["source"] = join_source(rel, _suffix_of(rec), uri=False)
    labels = dict(FILE_KEYS)
    for key, lst in sorted((rs.get("files") or {}).items()):
        new = []
        for p in lst:
            fp = fps.get(p) or {}
            if key in ("review_file", "facility_review_file") and fp and \
                    not fp.get("exists"):
                new.append(p)                   # there were no decisions at run time
                continue
            new.append(put(p, "files/" + _safe(key), labels.get(key, key), key))
        rs["files"][key] = new
    for s in rs.get("scenarios") or []:
        for key, label in RASTER_KEYS:
            p = s.get(key)
            if not p:
                continue
            if not include_rasters:
                not_packaged.append({"what": "scenario '{}' {} raster".format(s.get("name"),
                                                                             label),
                                     "path": p, "why": "rasters not included (give the "
                                     "rasters folder when reproducing)"})
                continue
            s[key] = put(p, "rasters/" + _safe(s.get("name")),
                         "Flood raster: {} - {}".format(s.get("name"), label),
                         "scenario '{}' {} raster".format(s.get("name"), label))
            if p.lower().endswith(".vrt"):
                for a in _vrt_absolute_sources(p):
                    not_packaged.append({"what": "tile of VRT {}".format(os.path.basename(p)),
                                         "path": a, "why": "the VRT names it by an absolute "
                                         "path - give the rasters folder when reproducing"})
    rs["package"] = {"made": _now_iso(), "plugin_version": PLUGIN_VERSION,
                     "include_rasters": bool(include_rasters),
                     "files": {src: rel for src, rel in
                               ((r[1], r[0]) for r in pk.rows)},
                     "not_packaged": not_packaged, "changed_since_run": changed}
    spath = os.path.join(package_folder, PACKAGE_SETTINGS_FILE)
    with open(spath, "w", encoding="utf-8") as fh:
        json.dump(rs, fh, indent=1, ensure_ascii=False)
    return {"rows": pk.rows, "settings_file": PACKAGE_SETTINGS_FILE,
            "not_packaged": not_packaged, "changed": changed}


# ------------------------------------------------------- the reproduction ---
def _is_package_relative(p):
    return bool(p) and not os.path.isabs(p) and not re.match(r"^[A-Za-z]:", str(p)) \
        and str(p).replace("\\", "/").startswith(DATA_DIR + "/")


class _Resolver(object):
    def __init__(self, package_folder, rasters_folder=None, fingerprints=None, packaged=None):
        self.pkg = package_folder
        self.rasters = rasters_folder
        self.fps = fingerprints or {}
        self.orig_of = {rel: src for src, rel in (packaged or {}).items()}
        self.notes = []
        self._index = None

    def path(self, p, what):
        """Absolute path of a recorded path: the package copy, else the original
        file when it still exists (noted), else None."""
        if not p:
            return None
        if _is_package_relative(p):
            full = os.path.join(self.pkg, *str(p).replace("\\", "/").split("/"))
            return full if os.path.exists(full) else None
        if os.path.exists(p):
            self.notes.append("{} read from its original place, not from the package: "
                              "{}".format(what, p))
            return p
        return None

    def source(self, source, what):
        """A layer source with its file resolved (suffix kept), or None."""
        path, suffix = split_source(source)
        full = self.path(path, what)
        return join_source(full, suffix) if full else None

    def layer_source(self, rec, what):
        full = self.path(rec.get("path"), what) if rec.get("path") else None
        return join_source(full, _suffix_of(rec)) if full else None

    def _walk(self):
        if self._index is None:
            self._index = {}
            if self.rasters and os.path.isdir(self.rasters):
                for dp, _d, files in os.walk(self.rasters):
                    for f in files:
                        self._index.setdefault(f.lower(), []).append(os.path.join(dp, f))
        return self._index

    def raster(self, p, scenario, what):
        """A scenario raster: the package copy, else a file of the same name in
        the rasters folder (<folder>/<scenario>/<name>, <folder>/<name>, or a
        unique match below it - several: the one with the recorded sha256),
        else the original path. A raster from the rasters folder whose sha256
        differs from the one the run read is noted."""
        if not p:
            return None
        if _is_package_relative(p):
            got = self.path(p, what)
            if got:
                return got
        orig = self.orig_of.get(p, p)
        want = (self.fps.get(orig) or {}).get("sha256")
        name = os.path.basename(str(p).replace("\\", "/"))
        got = None
        if self.rasters:
            for cand in (os.path.join(self.rasters, _safe(scenario), name),
                         os.path.join(self.rasters, str(scenario or ""), name),
                         os.path.join(self.rasters, name)):
                if os.path.isfile(cand):
                    got = cand
                    break
            if got is None:
                hits = self._walk().get(name.lower(), [])
                if len(hits) > 1 and want:
                    got = next((h for h in hits if sha256_file(h) == want), None)
                if got is None and hits:
                    got = hits[0]
                    if len(hits) > 1:
                        self.notes.append("{}: several files named {} in the rasters folder "
                                          "- {} used".format(what, name, hits[0]))
            if got is not None and want:
                have = sha256_file(got)
                if have and have != want:
                    self.notes.append("{}: {} is not the file the run read (sha256 "
                                      "differs)".format(what, got))
        if got is None and not _is_package_relative(p) and os.path.isfile(p):
            self.notes.append("{} read from its original place: {}".format(what, p))
            got = p
        return got


def _find_settings(package_folder):
    """(record, path, packaged?) of a package."""
    p = os.path.join(package_folder, PACKAGE_SETTINGS_FILE)
    if os.path.isfile(p):
        return read_run_settings(p), p, True
    p = os.path.join(package_folder, RECORD_DIR, RUN_SETTINGS_FILE)
    if os.path.isfile(p):
        return read_run_settings(p), p, False
    raise ValueError("No run settings in {} - the package was made without them (a run of a "
                     "plugin version before 2.9, or the run did not finish)".format(
                         package_folder))


def _input_items(spec):
    """2.34.5: ([(path, category or None)], [problem]) of one role's recorded inputs in any
    shape (datafolder.local_items: a path, a list of paths, [path, category] pairs, one
    unwrapped pair, nested lists) - the one normaliser the run itself uses."""
    try:
        from .datafolder import local_items
    except Exception:                         # noqa: BLE001 - no QGIS: the record's own shape
        return _input_paths(spec), []
    return local_items(spec)


_HPMS_RE = re.compile(r"^HPMS_ND_(\d{4})(?:_|$)", re.I)


def _traffic_as_run(files, notes):
    """2.34.5: the 'aadt' files of a record without INPUTS_FORMAT hold EVERY traffic download
    (NDDOT counts, the county roads, each HPMS year that answered) in download order, but the
    run read only the newest HPMS year (else NDDOT) plus the county roads, in that order
    (pipeline._traffic_layers). Hand over what the run read, in its order; files with other
    names (a data folder's own) stay, after them. `files`: the RECORDED files - a file the run
    read that is gone now is then missing, never replaced by an older year."""
    def kind(p):
        b = os.path.basename(split_source(p)[0])
        m = _HPMS_RE.match(b)
        if m:
            return "hpms", m.group(1)
        if b.lower().startswith("nddot_traffic_counts"):
            return "nddot", ""
        if b.lower().startswith("nd_county_roads"):
            return "county", ""
        return "other", ""
    kinds = [(kind(p), p, c) for p, c in files]
    if not any(k[0] in ("hpms", "nddot") for k, _p, _c in kinds):
        return files
    hpms = sorted((k[1], p, c) for k, p, c in kinds if k[0] == "hpms")
    keep, drop = [], []
    if hpms:
        keep.append([hpms[-1][1], hpms[-1][2]])
        drop += [p for _y, p, _c in hpms[:-1]]
        drop += [p for k, p, _c in kinds if k[0] == "nddot"]
    else:
        keep += [[p, c] for k, p, c in kinds if k[0] == "nddot"]
    keep += [[p, c] for k, p, c in kinds if k[0] == "county"]
    keep += [[p, c] for k, p, c in kinds if k[0] == "other"]
    if drop or [k[0] for k in keep] != [p for p, _c in files]:
        notes.append("input 'aadt': handed over as the run read it (newest HPMS year, else "
                     "NDDOT counts, then the county roads){}".format(
                         "; recorded but not read by the run: " + ", ".join(
                             os.path.basename(split_source(p)[0]) for p in drop)
                         if drop else ""))
    return keep


def _base_lower(path):
    return os.path.basename(split_source(path)[0]).lower()


def _bridge_rank(path):
    """pipeline._collect_facilities reads the NBI download before the OSM crossings."""
    b = _base_lower(path)
    return 0 if b.startswith("nbi_bridges") else 1 if b.startswith("osm_crossings") else 2


def _lifeline_rank(path):
    """pipeline._collect_facilities reads the data-folder lifelines first, then the agency
    downloads (lifeline_<key>_..., in LIFELINE_SOURCES order), then OpenStreetMap."""
    b = _base_lower(path)
    if b.startswith("osm_lifelines"):
        return len(LIFELINE_SOURCES) + 1
    for i, src in enumerate(LIFELINE_SOURCES):
        if b.startswith("lifeline_{}_".format(src[0].lower())):
            return i + 1
    return 0


def _order_as_run(role, files, rank, order, notes):
    """[[path, category]] of `files` by rank(path) (equal ranks as recorded); a note when the
    order changed."""
    keep = [[p, c] for p, c in sorted(files, key=lambda pc: rank(pc[0]))]   # stable
    if [k[0] for k in keep] != [p for p, _c in files]:
        notes.append("input '{}': handed over in the order the run read it ({})".format(
            role, order))
    return keep


def inputs_as_run(rec):
    """2.34.5: True when the record's "inputs" are each role's files as the run read them, in
    its order (INPUTS_FORMAT, written by both forks); False for any record without it, of
    either fork (_old_inputs_as_run)."""
    try:
        return int((rec or {}).get("inputs_format") or 0) >= INPUTS_FORMAT
    except (TypeError, ValueError):
        return False


def _old_inputs_as_run(role, files, notes):
    """2.34.5: the recorded files of one role of a record without INPUTS_FORMAT, put as the
    run read them. Such a record lists the downloads in the order they FINISHED
    (download.parallel_map), and the data-folder lifelines after them; the run reads the
    traffic as _traffic_as_run, the NBI bridges before the OSM crossings and the lifelines
    data folder - agency - OSM, and facilities.dedupe keeps the first of two duplicates.
    Other roles as recorded."""
    if len(files) < 2:
        return files
    if role == "aadt":
        return _traffic_as_run(files, notes)
    if role == "bridges":
        return _order_as_run(role, files, _bridge_rank,
                             "the NBI bridges, then the OpenStreetMap crossings", notes)
    if role == "lifelines":
        return _order_as_run(role, files, _lifeline_rank, "the data-folder layers, then the "
                             "agency downloads, then OpenStreetMap", notes)
    return files


def _local_data_supported():
    try:
        from .pipeline import DEFAULTS
        return "local_data" in DEFAULTS
    except Exception:
        return False


def _provider_for(source):
    """"delimitedtext" for a CSV URI (file:...?...), else "ogr" (as datafolder)."""
    u = str(source or "").strip().lower()
    return "delimitedtext" if u.startswith("file:") and "?" in u else "ogr"


def _make_layer(rec_kind, source, name, provider):
    if rec_kind == "raster":
        from qgis.core import QgsRasterLayer
        lyr = QgsRasterLayer(source, name or "raster", provider or "gdal")
    else:
        from qgis.core import QgsVectorLayer
        prov = provider if provider and provider not in ("memory",) else \
            _provider_for(source)
        lyr = QgsVectorLayer(source, name or "layer", prov)
    return lyr if lyr is not None and lyr.isValid() else None


def plan_from_package(package_folder, work_folder, rasters_folder=None, as_layers=None):
    """The reproduction plan: {"cfg", "notes", "missing", "settings_file",
    "packaged", "record"}. `as_layers` None: hand the data over as layers only
    when the pipeline has no cfg["local_data"] support (see the module
    docstring)."""
    rec, spath, packaged = _find_settings(package_folder)
    R = _Resolver(package_folder, rasters_folder, rec.get("fingerprints"),
                  (rec.get("package") or {}).get("files"))
    missing = []
    notes = []
    if not packaged:
        notes.append("The package holds no data copies (made without 'include data'): the "
                     "original files are used where they still exist")
    try:
        from .pipeline import DEFAULTS
        known = set(DEFAULTS)
    except Exception:
        known = None
    cfg = {}
    for k, v in (rec.get("settings") or {}).items():
        if k in _PATH_SETTINGS or (known is not None and k not in known
                                   and k != "settings_profile"):
            continue
        cfg[k] = v
    for k in list(cfg):
        if k.startswith("download_"):
            cfg[k] = False                       # never on the internet
    cfg.update(_REPRO_OVERRIDES)
    cfg.update({"output_folder": work_folder, "dam_folder": work_folder,
                "dam_name": rec.get("dam_name") or "Dam", "osm_layer": None,
                "point_sources": []})
    # ---- scenarios
    from .scenarios import Scenario
    scen = []
    for s in rec.get("scenarios") or []:
        d = dict(s)
        for key, label in RASTER_KEYS:
            if d.get(key):
                what = "scenario '{}' {} raster".format(d.get("name"), label)
                got = R.raster(d[key], d.get("name"), what)
                if got is None:
                    missing.append("{} ({})".format(what, d[key]))
                d[key] = got
        scen.append(Scenario.from_dict(d))
    cfg["scenarios"] = scen
    # ---- layers the engineer chose
    for key, lrec in sorted((rec.get("layers") or {}).items()):
        what = "layer {} ('{}')".format(key, lrec.get("name"))
        src = R.layer_source(lrec, what)
        lyr = _make_layer(lrec.get("kind"), src, lrec.get("name"), lrec.get("provider")) \
            if src else None
        if lyr is None:
            why = (lrec.get("file_note") or "not in the package") if not src else \
                "could not be opened"
            missing.append("{} - {}".format(what, why))
        cfg[key] = lyr
    for item in rec.get("point_sources") if isinstance(rec.get("point_sources"), list) else []:
        lrec = item.get("layer") or {}
        what = "point layer {} ('{}')".format(item.get("family"), lrec.get("name"))
        src = R.layer_source(lrec, what)
        lyr = _make_layer("vector", src, lrec.get("name"), lrec.get("provider")) if src \
            else None
        if lyr is None:
            missing.append("{} - {}".format(what, lrec.get("file_note") or "not in the package"))
        else:
            cfg["point_sources"].append((item.get("family"), lyr))
    # ---- the data sets the run read
    # 2.34.5: ONE shape for every role - [[path, category or None], ...] - from any recorded
    # shape (old records: a path, or a list of paths and pairs); every file of a role is
    # handed over (AADT, bridges, ... may come from several); an unreadable entry is missing.
    # A record without INPUTS_FORMAT (either fork) is put as the run read it first, from the
    # whole record: a file the run read that is gone is missing, nothing takes its place
    as_run = inputs_as_run(rec)
    local = {}
    for role, spec in sorted((rec.get("inputs") or {}).items()):
        what = "input '{}'".format(role)
        items, problems = _input_items(spec)
        for prob in problems:
            missing.append("{} - unreadable record entry: {}".format(what, prob))
        if not as_run:
            items = _old_inputs_as_run(role, items, notes)
        got = []
        for p, cat in items:
            full = R.source(p, what)
            if full is None:
                missing.append("{} ({})".format(what, p))
                continue
            got.append([full, cat])
        if got:
            local[role] = got
        elif items or problems:
            n = len(items) + len(problems)
            missing.append("{} - none of its {} entr{} is available: the role is NOT "
                           "reproduced".format(what, n, "y" if n == 1 else "ies"))
    cfg["local_data"] = local
    if as_layers is None:
        as_layers = not _local_data_supported()
    if as_layers:
        notes.append("This pipeline reads no cfg['local_data']: the packaged data are "
                     "handed over as layers")
        for role, spec in sorted(local.items()):
            one = spec[0][0]                     # 2.34.5: [[path, category], ...]
            key = ROLE_CFG_KEYS.get(role)
            fam = ROLE_POINT_FAMILIES.get(role)
            if key:
                if cfg.get(key) is None:
                    cfg[key] = _make_layer("vector", one, role, _provider_for(one))
                    if isinstance(spec, list) and len(spec) > 1:
                        notes.append("input '{}': only the first of {} files could be handed "
                                     "over as a layer".format(role, len(spec)))
            elif fam:
                for path, _cat in spec:          # 2.34.5: every file of the role
                    lyr = _make_layer("vector", path, role, _provider_for(path))
                    if lyr is not None:
                        cfg["point_sources"].append((fam, lyr))
                    else:
                        missing.append("input '{}' ({}) - could not be opened".format(role,
                                                                                      path))
            else:
                notes.append("input '{}' ({}) cannot be handed to this pipeline without "
                             "cfg['local_data'] - it is left out, the result may differ".format(
                                 role, ROLE_LABELS.get(role, role)))
    # ---- other files
    files = rec.get("files") or {}
    fps = rec.get("fingerprints") or {}
    for key, _label in FILE_KEYS:
        lst = files.get(key) or []
        if key == "review_file":
            p = lst[0] if lst else None
            fp = fps.get(p) or {}
            if not p or (fp and not fp.get("exists")):
                cfg["review_file"] = os.path.join(work_folder, NO_REVIEW_FILE)
                notes.append("No engineer review decisions existed when the run was made")
            else:
                full = R.path(p, "review decisions")
                if full is None:
                    missing.append("engineer review decisions ({}) - the reproduction runs "
                                   "without them".format(p))
                    cfg["review_file"] = os.path.join(work_folder, NO_REVIEW_FILE)
                else:
                    cfg["review_file"] = full
        elif key == "facility_review_file":
            p = lst[0] if lst else None
            fp = fps.get(p) or {}
            nofile = os.path.join(work_folder, NO_FACILITY_REVIEW_FILE)
            if not p or (fp and not fp.get("exists")):
                cfg["facility_review_file"] = nofile
            else:
                full = R.path(p, "roads / facilities review decisions")
                if full is None:
                    missing.append("roads / facilities review decisions ({}) - the "
                                   "reproduction runs without them".format(p))
                    cfg["facility_review_file"] = nofile
                else:
                    cfg["facility_review_file"] = full
        elif key == "hcom_files":
            got = [R.path(p, "HCOM table") for p in lst]
            for p, g in zip(lst, got):
                if g is None:
                    missing.append("DSS-WISE HCOM table ({})".format(p))
            cfg["hcom_files"] = ";".join(g for g in got if g)
        elif key == "damage_curves_file":
            full = R.path(lst[0], "damage curves") if lst else None
            if lst and full is None:
                missing.append("non-residential damage curves ({})".format(lst[0]))
            cfg["damage_curves_file"] = full or ""
        elif key == "peak_raster":
            full = R.path(lst[0], "time-of-peak raster") if lst else None
            if lst and full is None:
                missing.append("time-of-peak raster ({})".format(lst[0]))
            cfg["peak_raster"] = full
    pkg = rec.get("package") or {}
    for c in pkg.get("changed_since_run") or []:
        notes.append("{} had changed on disk between the run and the package ({}) - the "
                     "package holds the later file".format(c.get("what"), c.get("path")))
    notes.extend(R.notes)
    return {"cfg": cfg, "notes": notes, "missing": missing, "settings_file": spath,
            "packaged": packaged, "record": rec}


def cfg_from_package(package_folder, work_folder, rasters_folder=None):
    """cfg for pipeline.config(**cfg) that re-runs the packaged classification
    in `work_folder` (see plan_from_package for the notes and missing inputs)."""
    return plan_from_package(package_folder, work_folder, rasters_folder)["cfg"]


def verify_manifest(package_folder):
    """[(path, problem)] of the package files that are missing or differ from
    MANIFEST.csv (the package was changed after it was made)."""
    mpath = os.path.join(package_folder, "MANIFEST.csv")
    out = []
    try:
        with open(mpath, newline="", encoding="utf-8-sig") as fh:
            rows = list(csv.DictReader(fh))
    except OSError:
        return [("MANIFEST.csv", "missing - the package cannot be checked")]
    for r in rows:
        rel = r.get("Path") or ""
        full = os.path.join(package_folder, *rel.split("/"))
        if not os.path.isfile(full):
            out.append((rel, "missing"))
        elif r.get("SHA256") and sha256_file(full, limit=0) != r["SHA256"]:
            out.append((rel, "changed since the package was made"))
    return out


def _match_units(crs_id):
    try:
        from qgis.core import QgsCoordinateReferenceSystem
        from .helpers import buffer_distance_in_layer_units
        crs = QgsCoordinateReferenceSystem(crs_id)
        if crs.isValid():
            return float(buffer_distance_in_layer_units(HIS.MATCH_FT, crs))
    except Exception:
        pass
    return HIS.MATCH_FT


def compare_snapshots(before, now, match_units=None):
    """(identical, lines, rows) of a reproduction's snapshot against the
    package's: history.diff (the Changes_Since_Last_Run comparison) without the
    raster paths (they differ by design; the files are checked by hash), plus
    lives in jeopardy and the governing scenario. rows: history CHANGE_FIELDS."""
    if before is None:
        return False, ["Not compared: the package holds no Last_Run_Snapshot.json (the run "
                       "had 'compare with the last run' off)"], []
    if now is None:
        return False, ["Not compared: the reproduction wrote no snapshot"], []
    b, n = dict(before), dict(now)
    b.pop("rasters", None)
    n.pop("rasters", None)
    units = match_units if match_units is not None else _match_units(n.get("crs") or "")
    lines, rows = HIS.diff(b, n, units)
    try:
        lij_diff = abs(float(b.get("lij") or 0) - float(n.get("lij") or 0)) > 0.05
    except (TypeError, ValueError):
        lij_diff = b.get("lij") != n.get("lij")
    if lij_diff:
        rows.append(["Lives in jeopardy", "dam", b.get("lij"), n.get("lij"), "", ""])
    if (b.get("governing") or "") != (n.get("governing") or ""):
        rows.append(["Governing scenario", "dam", b.get("governing"), n.get("governing"), "",
                     ""])
    lines = [ln for ln in lines if ln != "No differences."]
    return not rows, lines, rows


def _free_path(path):
    if not os.path.exists(path):
        return path
    try:
        os.remove(path)
        return path
    except OSError:
        base, ext = os.path.splitext(path)
        return "{}_{}{}".format(base, time.strftime("%Y%m%d_%H%M%S"), ext)


def check_local_data(local_data):
    """2.34.5: ({role: number of files that open}, [problem]) of a reproduction's
    cfg["local_data"] - opened as the run opens them (datafolder.open_config_all), so a role
    or file the run would leave out is named in the log and the check, never skipped
    silently. ({}, []) without QGIS / datafolder."""
    if not local_data:
        return {}, []
    try:
        from .datafolder import open_config_all
    except Exception:                         # noqa: BLE001 - no QGIS: not checked here
        return {}, []
    layers, notes = open_config_all(local_data)
    loaded = {role: len(v) for role, v in layers.items()}
    return loaded, list(notes)


WORK_MARKER = "_Reproduction_Work_Folder.txt"


def _check_work_folder(work_folder):
    """A work folder is empty, new, or one a reproduction made (WORK_MARKER):
    never a dam's real outputs folder."""
    if not os.path.isdir(work_folder) or os.path.isfile(os.path.join(work_folder, WORK_MARKER)):
        return
    if os.path.exists(os.path.join(work_folder, "Hazard_Classification_Run_Log.txt")) or \
            os.path.isfile(os.path.join(work_folder, SUBDIR_QA, HIS.SNAPSHOT_FILE)):
        raise ValueError("{} holds the outputs of a classification run - choose an empty work "
                         "folder for the reproduction".format(work_folder))


def extract_package(zip_path, work_folder):
    """Unzip a package (submittal.build zip_it) into <work folder>/_package and
    return the package folder in it (the zip holds one top folder)."""
    dest = os.path.join(os.path.abspath(work_folder), "_package")
    if os.path.isdir(dest):
        shutil.rmtree(dest)
    os.makedirs(dest)
    with zipfile.ZipFile(zip_path) as zf:
        for n in zf.namelist():
            parts = n.replace("\\", "/").split("/")
            if n.startswith("/") or ".." in parts or re.match(r"^[A-Za-z]:", n):
                raise ValueError("{} holds an unsafe path: {}".format(zip_path, n))
        zf.extractall(dest)
    tops = [d for d in os.listdir(dest) if os.path.isdir(os.path.join(dest, d))]
    if len(tops) == 1 and not os.path.isfile(os.path.join(dest, "MANIFEST.csv")):
        return os.path.join(dest, tops[0])
    return dest


def reproduce(package_folder, work_folder, run=None, progress=None, log=None,
              rasters_folder=None, as_layers=None):
    """Re-run the packaged classification in `work_folder` and compare its
    result with the package's (`package_folder`: the package folder or its
    .zip - unzipped into <work folder>/_package). `run` (cfg, progress=, log=) -> result:
    pipeline.run_classification by default (injectable). Returns {"identical",
    "status" ("IDENTICAL" / "DIFFERENT" / "NOT COMPARED" / "FAILED"), "lines",
    "rows" (CHECK_FIELDS dicts), "csv", "text", "missing", "notes", "result",
    "error"}. Never raises for a failed run (status FAILED); raises ValueError
    for a folder that is no package or a work folder inside the package."""
    package_folder = os.path.abspath(package_folder)
    work_folder = os.path.abspath(work_folder)
    _check_work_folder(work_folder)
    if os.path.isfile(package_folder) and zipfile.is_zipfile(package_folder):
        package_folder = extract_package(package_folder, work_folder)
    if not os.path.isdir(package_folder):
        raise ValueError("Package folder not found: {}".format(package_folder))
    if os.path.normcase(work_folder + os.sep).startswith(os.path.normcase(package_folder
                                                                          + os.sep)) \
            or os.path.normcase(work_folder) == os.path.normcase(package_folder):
        raise ValueError("Choose a work folder outside the package - the package must stay "
                         "as it was made")
    plan = plan_from_package(package_folder, work_folder, rasters_folder, as_layers)
    os.makedirs(work_folder, exist_ok=True)
    with open(os.path.join(work_folder, WORK_MARKER), "w", encoding="utf-8") as fh:
        fh.write("Work folder of a reproduction of {} ({}). Its outputs may be replaced by the "
                 "next reproduction.\n".format(package_folder, time.strftime("%Y-%m-%d %H:%M")))
    old_snap = os.path.join(work_folder, SUBDIR_QA, HIS.SNAPSHOT_FILE)
    if os.path.isfile(old_snap):
        os.remove(old_snap)                     # never compare with an earlier reproduction
    say = log or (lambda m: None)
    before = HIS.read_snapshot(os.path.join(package_folder, RECORD_DIR))
    tampered = verify_manifest(package_folder)
    result, error = None, None
    try:
        from . import pipeline as PL
        cfg = PL.config(**plan["cfg"])
        run = run or PL.run_classification
    except ImportError:
        cfg = dict(plan["cfg"])
        if run is None:
            raise
    for m in plan["missing"]:
        say(">>> WARNING: not in the package: {}".format(m))
    loaded, not_loaded = check_local_data(plan["cfg"].get("local_data"))
    for role in sorted(loaded):
        say(">>> Package data '{}': {} file(s) open".format(role, loaded[role]))
    for m in not_loaded:
        say(">>> WARNING: package data NOT loaded: {}".format(m))
    try:
        result = run(cfg, progress=progress, log=log)
    except Exception as e:                      # the check reports it
        error = "{}: {}".format(type(e).__name__, e)
        say(">>> Reproduction run FAILED: {}".format(error))
    now = HIS.read_snapshot(os.path.join(work_folder, SUBDIR_QA))
    rows = []
    if error:
        identical, lines = False, ["The reproduction run failed: {}".format(error)]
        status = "FAILED"
    else:
        identical, lines, drows = compare_snapshots(before, now)
        for r in drows:
            rows.append(dict(zip(CHECK_FIELDS, ["Result"] + ["" if v is None else v
                                                             for v in r])))
        if before is None or now is None:
            status = "NOT COMPARED"
        else:
            status = "IDENTICAL" if identical else "DIFFERENT"
    for m in plan["missing"]:
        rows.append(dict(zip(CHECK_FIELDS, ["Input", "Missing from the package", m, "", "",
                                            "", ""])))
    for m in not_loaded:
        rows.append(dict(zip(CHECK_FIELDS, ["Input", "Not loaded", m, "", "", "", ""])))
    for role in sorted(loaded):
        rows.append(dict(zip(CHECK_FIELDS, ["Input", "Loaded", role, "",
                                            "{} file(s)".format(loaded[role]), "", ""])))
    for rel, prob in tampered:
        rows.append(dict(zip(CHECK_FIELDS, ["Package", "Package file " + prob, rel, "", "",
                                            "", ""])))
    for n in plan["notes"]:
        rows.append(dict(zip(CHECK_FIELDS, ["Note", "", n, "", "", "", ""])))
    head = {"IDENTICAL": "IDENTICAL - the reproduction gives the same result as the package "
                         "(ND {} / TM 11 {}, lives in jeopardy {})",
            "DIFFERENT": "DIFFERENT - the reproduction does not give the package's result "
                         "({} difference(s), see below)",
            "NOT COMPARED": "NOT COMPARED - see below",
            "FAILED": "FAILED - the reproduction run did not finish"}[status]
    if status == "IDENTICAL":
        head = head.format((now or {}).get("nd"), (now or {}).get("tm11"),
                           (now or {}).get("lij"))
    elif status == "DIFFERENT":
        head = head.format(sum(1 for r in rows if r["Kind"] == "Result"))
    summary = [head] + lines
    if status == "DIFFERENT":
        for r in rows:
            if r["Kind"] == "Result":
                summary.append("  {}: {} ({} -> {})".format(r["Change"], r["Item"],
                                                          r["Package"], r["Reproduction"]))
    if plan["missing"]:
        summary.append("{} input(s) missing from the package - a difference may come from "
                       "them: {}".format(len(plan["missing"]), "; ".join(plan["missing"])))
    if not_loaded:
        summary.append("{} package data file(s) / role(s) NOT loaded - a difference may come "
                       "from them: {}".format(len(not_loaded), "; ".join(not_loaded)))
    if loaded:
        summary.append("Package data loaded: {}".format(", ".join(
            "{} ({})".format(r, loaded[r]) for r in sorted(loaded))))
    if tampered:
        summary.append("{} package file(s) missing or changed since the package was made "
                       "(MANIFEST.csv): {}".format(len(tampered), ", ".join(
                           r for r, _p in tampered)))
    summary.extend("Note: " + n for n in plan["notes"])
    summary.append("Reproduced {} with {} v{} from {} (run of {} by v{})".format(
        time.strftime("%Y-%m-%d %H:%M"), PLUGIN_NAME, PLUGIN_VERSION, package_folder,
        (before or {}).get("time") or "?", plan["record"].get("plugin_version") or "?"))
    if (plan["record"].get("plugin_version") or PLUGIN_VERSION) != PLUGIN_VERSION:
        summary.append("The package was made by another plugin version - a difference may "
                       "come from a changed method (see the CHANGES files)")
    cpath = _free_path(os.path.join(work_folder, CHECK_FILE))
    with open(cpath, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=CHECK_FIELDS)
        w.writeheader()
        w.writerows(rows)
    tpath = _free_path(os.path.join(work_folder, CHECK_TEXT))
    with open(tpath, "w", encoding="utf-8") as fh:
        fh.write("\n".join(str(x) for x in summary) + "\n")
    for line in summary:
        say(line)
    return {"identical": status == "IDENTICAL", "status": status, "lines": summary,
            "rows": rows, "csv": cpath, "text": tpath, "missing": plan["missing"],
            "notes": plan["notes"], "result": result, "error": error,
            "loaded": loaded, "not_loaded": not_loaded,
            "package_snapshot": before, "new_snapshot": now}
