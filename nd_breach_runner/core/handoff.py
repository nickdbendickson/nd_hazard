"""Hand the breach rasters to the ND hazard classification plugin.

That plugin ("ND Dam Hazard", Processing provider `ndhazard`) has its own
parameters that this tool cannot see in advance, so the hand-off reads the
algorithm's parameter list at run time and fills each one from the dam folder
by name:

* a folder parameter gets the dam folder (single dam) or the dams folder (batch);
* v3.1.1: when the algorithm takes the dam folder, its OPTIONAL raster
  parameters are left empty - the classifier finds the breach and baseline
  rasters in the folder by their names (the ND Dam Hazard plugin does; see its
  README "HANDING RESULTS TO THIS PLUGIN"). Otherwise a raster whose name or
  description mentions depth, velocity, arrival or WSE gets that raster;
* a raster parameter for a no-breach BASELINE gets the "Baseline ..." raster of
  that kind, never the breach raster; a depth x velocity, incremental or
  difference raster parameter is never filled by keyword (a required one is
  reported as missing, to be given in the mapping). Before 3.1.1 the words
  alone decided: "No-breach baseline: max depth" received the breach depth,
  so the breach was its own baseline and the classifier charged nothing to the
  dam;
* a text parameter that looks like a dam ID gets the dam ID; v3.2.0: one that names an NID
  gets the dam folder's dam.json nid_id, or the dam ID only when it looks like an NID ID
  (ND00123), else nothing (an optional one is left empty, a required one is reported);
* destination parameters get files in the dam folder's `_classification` folder.

v3.2.0: before calling an `ndhazard:` algorithm the installed ND Dam Hazard version is checked
(damsuite.versions.require); below the hard minimum (2.9.1) the hand-off stops with the text.

Anything it cannot fill is named in the error so the mapping can be given
explicitly as JSON: {"PARAM_NAME": "value with {depth} {velocity} {arrival} {wse}
{folder} {parent} {dam_id} {out} placeholders"}. Nothing here needs the
classification plugin to be installed until it is called.
"""
import csv
import json
import os
import re

DEFAULT_IDS = {"classify": ("ndhazard:classify", "ndhazard:classifydam", "ndhazard:rundam"),
               "batch": ("ndhazard:batch", "ndhazard:batchclassify", "ndhazard:batch_classify")}

RASTER_KEYS = (("depth", ("depth",)), ("velocity", ("veloc",)), ("arrival", ("arriv",)),
               ("wse", ("wse", "water surface", "watersurface", "elevation")))


# v3.1.1: raster parameters that must never get a breach raster by keyword
_BASELINE_WORDS = re.compile(r"baseline|no[\s_-]*breach|non[\s_-]*breach|without[\s_-]*"
                             r"(the[\s_-]*)?breach|no[\s_-]*fail|non[\s_-]*fail|"
                             r"(^|[^a-z])base[\s_]", re.I)
_PRODUCT_WORDS = re.compile(r"d\s*[x*]\s*v|dxv|depth[\s_-]*(x|times|\*)[\s_-]*vel|"
                            r"depth[\s_-]*velocity[\s_-]*product|(^|[^a-z])dv([^a-z]|$)|"
                            r"increment|difference|(^|[^a-z])diff([^a-z]|$)|(^|[^a-z])rise"
                            r"([^a-z]|$)", re.I)


class HandoffError(Exception):
    pass


# v3.2.0: NID parameters (SUBJECT_NID, "NID ID of the dam ...") get an NID ID, never any dam ID
_NID_WORDS = re.compile(r"(^|[^a-z])nid", re.I)
NID_PATTERN = re.compile(r"^[A-Z]{2}\d{5}$", re.I)


def nid_for(folder, dam_id):
    """The NID ID for a hand-off: the dam folder's dam.json nid_id, else the dam ID when it
    looks like an NID ID (two letters, five digits), else "". Never raises."""
    if folder:
        try:
            from ..damsuite import dam_record
            rec = dam_record.load(folder)
            v = rec.get("nid_id") if rec is not None else None
            if v not in (None, "") and str(v).strip():
                return str(v).strip()
        except Exception:
            pass
    d = str(dam_id or "").strip()
    return d if NID_PATTERN.match(d) else ""


def check_version(algorithm_id="", versions=None):
    """v3.2.0: before calling ND Dam Hazard, check its version against this plugin's needs
    (damsuite.versions.require("ndbreach", "ndhazard")): below the hard minimum ->
    HandoffError with the text. Another provider's algorithm (a custom ID) is not checked.
    `versions` ({key: version}) replaces the QGIS lookup (tests)."""
    if algorithm_id and not str(algorithm_id).lower().startswith("ndhazard:"):
        return
    from ..damsuite import versions as dv
    try:
        dv.require("ndbreach", "ndhazard", versions)
    except RuntimeError as e:
        raise HandoffError(str(e))


def _reg():
    from qgis.core import QgsApplication
    return QgsApplication.processingRegistry()


def find_algorithm(kind, algorithm_id="", registry=None):
    """The classification algorithm (kind "classify" or "batch"), or None."""
    reg = registry or _reg()
    ids = (algorithm_id,) if algorithm_id else DEFAULT_IDS[kind]
    for i in ids:
        a = reg.algorithmById(i)
        if a is not None:
            return a
    if not algorithm_id:      # any ndhazard algorithm whose id or name mentions it
        for a in reg.algorithms():
            aid = a.id().lower()
            if aid.startswith("ndhazard:") and (kind in aid or (
                    kind == "classify" and "class" in aid)):
                return a
    return None


def describe(alg):
    out = []
    for p in alg.parameterDefinitions():
        out.append({"name": p.name(), "description": p.description(), "type": p.type(),
                    "optional": _optional(p),
                    "destination": bool(getattr(p, "isDestination", lambda: False)())})
    return out


def _optional(p):
    try:
        from qgis.core import QgsProcessingParameterDefinition as D
        flag = getattr(D, "FlagOptional", None) or D.Flag.FlagOptional
        return bool(p.flags() & flag)
    except Exception:
        return bool(getattr(p, "optional", False))


def _raster_kind(p):
    text = (p.name() + " " + (p.description() or "")).lower()
    for kind, keys in RASTER_KEYS:
        if any(k in text for k in keys):
            # 'elevation' alone would also match a DEM; WSE needs 'wse' or 'water surface'
            if kind == "wse" and not any(k in text for k in ("wse", "water surface",
                                                              "watersurface")):
                continue
            return kind
    return None


def _param_text(p):
    return p.name() + " " + (p.description() or "")


def _is_folder_param(p):
    typ = p.type()
    return typ == "folder" or (typ == "file" and any(
        s in _param_text(p).lower() for s in ("folder", "directory")))


def baseline_rasters_in(folder):
    """{kind: path} of the dam folder's no-breach baseline rasters."""
    from .workflow import BASELINE_NAMES
    if not folder:
        return {}
    return {k: os.path.join(folder, v) for k, v in BASELINE_NAMES.items()
            if os.path.exists(os.path.join(folder, v))}


def build_parameters(alg, dam_id, folder, parent, rasters, mapping=None, out_dir=None,
                     baselines=None, notes=None):
    """Parameter dict for `alg`. `rasters` maps depth/velocity/arrival/wse to
    paths; `baselines` the same for the no-breach baseline (default: the
    "Baseline ..." files of `folder`). Raises HandoffError naming what could
    not be filled. `notes` (a list) receives what was left to the classifier."""
    mapping = mapping or {}
    if baselines is None:
        baselines = baseline_rasters_in(folder)
    notes = notes if notes is not None else []
    # v3.1.1: a classifier that takes the dam folder finds its own rasters there
    folder_mode = bool(folder) and any(_is_folder_param(p) for p in alg.parameterDefinitions()
                                       if not getattr(p, "isDestination", lambda: False)())
    out_dir = out_dir or os.path.join(folder or parent, "_classification")
    os.makedirs(out_dir, exist_ok=True)
    fmt = dict(rasters)
    fmt.update({"folder": folder or "", "parent": parent or "", "dam_id": dam_id or "",
                "out": out_dir})
    params, missing = {}, []
    for p in alg.parameterDefinitions():
        n, typ = p.name(), p.type()
        if n in mapping:
            v = mapping[n]
            try:
                params[n] = v.format(**fmt) if isinstance(v, str) else v
            except (KeyError, IndexError, ValueError) as e:
                raise HandoffError("Mapping for {} uses {} which is not available for {} "
                                   "(rasters found: {}).".format(
                                       n, e, dam_id or folder or parent,
                                       ", ".join(sorted(rasters)) or "none"))
            continue
        dest = bool(getattr(p, "isDestination", lambda: False)())
        if dest:
            tl = typ.lower()
            if "folder" in tl:
                params[n] = out_dir
            else:
                ext = ".tif" if "raster" in tl else ".gpkg" if "vector" in tl else \
                    ".html" if "html" in (n + p.description()).lower() else ".csv"
                params[n] = os.path.join(out_dir, "{}{}".format(n.lower(), ext))
            continue
        if typ == "raster":
            text = _param_text(p)
            if folder_mode and _optional(p):
                notes.append("{}: left to the classifier (found in the dam folder)".format(n))
                continue
            if _PRODUCT_WORDS.search(text):
                if _optional(p):
                    notes.append("{}: not filled (depth x velocity / difference raster)"
                                 .format(n))
                    continue
                missing.append("{} (raster: {} - not filled by keyword)".format(
                    n, p.description()))
                continue
            k = _raster_kind(p)
            if _BASELINE_WORDS.search(text):
                if k and baselines.get(k):
                    params[n] = baselines[k]
                    continue
                if _optional(p):
                    notes.append("{}: no baseline raster of this kind".format(n))
                    continue
                missing.append("{} (raster: {} - no 'Baseline' raster of this kind in the "
                               "dam folder)".format(n, p.description()))
                continue
            if k and rasters.get(k):
                params[n] = rasters[k]
                continue
        elif typ == "folder" or (typ == "file" and any(
                s in (n + p.description()).lower() for s in ("folder", "directory"))):
            low = (n + " " + (p.description() or "")).lower()
            params[n] = parent if ("dams" in low or "parent" in low or "batch" in low) and parent \
                else (folder or parent)
            continue
        elif typ == "string" and _NID_WORDS.search(_param_text(p)):
            # v3.2.0: an NID parameter gets an NID - dam.json's nid_id, or the dam ID only
            # when it looks like one (ND Dam Hazard records it as the dam's NID)
            nid = nid_for(folder, dam_id)
            if nid:
                params[n] = nid
                continue
            if _optional(p):
                notes.append("{}: left empty (no NID ID: dam.json has none and the dam ID "
                             "'{}' is not an NID ID)".format(n, dam_id or ""))
                continue
            missing.append("{} ({}: {} - no NID ID in dam.json and the dam ID '{}' is not "
                           "one)".format(n, typ, p.description(), dam_id or ""))
            continue
        elif typ == "string" and any(s in (n + " " + (p.description() or "")).lower()
                                     for s in ("dam id", "damid", "dam_id")):
            params[n] = dam_id
            continue
        if _optional(p) or p.defaultValue() is not None:
            continue                # leave at its default
        missing.append("{} ({}: {})".format(n, typ, p.description()))
    if missing:
        raise HandoffError(
            "Could not fill these parameters of {}: {}. Give them in the mapping JSON, e.g. "
            "{{\"{}\": \"{{folder}}\"}}.".format(alg.id(), "; ".join(missing),
                                                 missing[0].split(" ")[0]))
    return params


def _run(alg, params, feedback=None):
    import processing
    return processing.run(alg.id(), params, feedback=feedback)


def find_class(outputs):
    """A hazard class string in an algorithm's outputs, or ''. The ND class
    (ND_CLASS) comes first when the classifier gives several classes."""
    for k in ("ND_CLASS", "ND_HAZARD_CLASS", "HAZARD_CLASS"):
        v = outputs.get(k)
        if isinstance(v, str) and v and os.path.sep not in v:
            return v
    for k, v in outputs.items():
        if isinstance(v, str) and any(s in k.lower() for s in ("class", "hazard")) and not (
                os.path.sep in v or v.lower().endswith((".csv", ".tif", ".gpkg"))):
            return v
    for k, v in outputs.items():
        if isinstance(v, str) and v.lower().endswith(".csv") and os.path.exists(v):
            row = _first_row(v)
            for kk, vv in row.items():
                if any(s in kk.lower() for s in ("class", "hazard")) and vv:
                    return str(vv)
    return ""


def _first_row(path):
    try:
        with open(path, newline="") as f:
            return next(csv.DictReader(f), {})
    except (OSError, StopIteration, csv.Error):
        return {}


def rasters_in(folder):
    from .workflow import FINAL_NAMES
    return {k: os.path.join(folder, v) for k, v in FINAL_NAMES.items()
            if os.path.exists(os.path.join(folder, v))}


def make_classifier(algorithm_id="", mapping=None, feedback=None, registry=None,
                    dam_id_of=None, versions=None):
    """classify(folder) -> hazard class string, for sensitivity runs."""
    alg = find_algorithm("classify", algorithm_id, registry)
    if alg is None:
        raise HandoffError("The classification algorithm was not found. Install the ND "
                           "hazard plugin or give its algorithm ID.")
    check_version(alg.id(), versions)

    def classify(folder):
        r = rasters_in(folder)
        params = build_parameters(alg, (dam_id_of or os.path.basename)(folder), folder,
                                  os.path.dirname(folder), r, mapping)
        return find_class(_run(alg, params, feedback)) or "(class not in the outputs)"
    return classify


def classify_folder(folder, dam_id="", algorithm_id="", mapping=None, feedback=None,
                    registry=None, versions=None):
    alg = find_algorithm("classify", algorithm_id, registry)
    if alg is None:
        raise HandoffError("The classification algorithm was not found (looked for {}).".format(
            ", ".join(DEFAULT_IDS["classify"])))
    check_version(alg.id(), versions)
    notes = []
    params = build_parameters(alg, dam_id or os.path.basename(folder), folder,
                              os.path.dirname(folder), rasters_in(folder), mapping,
                              notes=notes)
    if feedback is not None:
        for n in notes:
            try:
                feedback.pushInfo("Hand-off: " + n)
            except Exception:
                pass
    out = _run(alg, params, feedback)
    return {"outputs": out, "class": find_class(out), "algorithm": alg.id(), "notes": notes}


def classify_batch(parent, algorithm_id="", mapping=None, feedback=None, registry=None,
                   versions=None):
    alg = find_algorithm("batch", algorithm_id, registry)
    if alg is None:
        raise HandoffError("The classification batch algorithm was not found (looked for {}).".format(
            ", ".join(DEFAULT_IDS["batch"])))
    check_version(alg.id(), versions)
    params = build_parameters(alg, "", "", parent, {}, mapping,
                              out_dir=os.path.join(parent, "_classification"))
    out = _run(alg, params, feedback)
    return {"outputs": out, "algorithm": alg.id()}


def merge_csv(breach_csv, class_csv, out_csv, key_breach="Dam", key_class=None):
    """One CSV with the breach summary and the classification of each dam.
    The class file's key column is the first of its columns named like a dam ID."""
    with open(breach_csv, newline="") as f:
        a = list(csv.DictReader(f))
    with open(class_csv, newline="") as f:
        rd = csv.DictReader(f)
        b = list(rd)
        cols = rd.fieldnames or []
    if key_class is None:
        norm = {c.lower().replace("_", "").replace(" ", ""): c for c in cols}
        for k in ("damid", "nidid", "nid", "dam", "id", "damname", "name"):   # by priority
            if k in norm:
                key_class = norm[k]
                break
        else:
            key_class = cols[0] if cols else None
    if key_class is None:
        raise HandoffError("the classification CSV has no columns")
    idx = {str(r.get(key_class, "")).strip().lower(): r for r in b}
    extra = [c for c in cols if c != key_class]
    rows = []
    for r in a:
        m = idx.get(str(r.get(key_breach, "")).strip().lower(), {})
        row = dict(r)
        for c in extra:
            row["Class_" + c if c in row else c] = m.get(c, "")
        rows.append(row)
    fields = list(a[0].keys()) if a else []
    for r in rows:
        for k in r:
            if k not in fields:
                fields.append(k)
    with open(out_csv, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    return out_csv


def load_mapping(text):
    """Mapping JSON from a parameter (empty = none)."""
    if not text or not str(text).strip():
        return {}
    try:
        m = json.loads(text)
    except ValueError as e:
        raise HandoffError("The parameter mapping is not valid JSON: {}".format(e))
    if not isinstance(m, dict):
        raise HandoffError("The parameter mapping must be a JSON object")
    return m
