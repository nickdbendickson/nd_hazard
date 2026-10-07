"""Run manifests of the screening tools (0.11): what each tool read and wrote, by content.

Every tool that writes a file or folder in the work folder leaves a manifest next to it:
    <output file>.manifest.json          (1_dams.gpkg.manifest.json, 8_hazard.gpkg.manifest.json ...)
    <output folder>/run_manifest.json    (2_terrain/, 7_simulations/, 10_reports/ ...)
in the ND Dam Breach Runner's manifest format (damsuite.chain reads both): manifest_version 1,
tool "ND Dam Breach Screening <version>", created, settings (the parameters as given, layers by
their source), inputs {label: {path, sha256, size, mtime, sampled}}, outputs {...}, and the
manifest's own digest. Digests are damsuite.chain.file_digest (SHA-256; sampled above 2 GB), the
same the Runner writes, so ND Dam Hazard's model-chain check can match the rasters it classified.

Tool 8 with a Dams folder also writes, per dam that has a folder there,
    <Dams folder>/<dam ID>/_screening_model/run_manifest.json
listing that dam's tool-7 rasters (depth, velocity, D x V, arrival, increment) and its class, so the
chain check finds a screening result the way it finds a Runner result (damsuite.chain looks in
_screening_model too).

The window's "out of date" test uses the manifests: a step is out of date when any INPUT FILE its
manifest recorded has another digest now (content changed), not when a date changed. Steps run
before 0.11 have no manifest and fall back to dates.

Pure Python; GDAL/QGIS objects are only looked at through duck typing (``.source()``).
"""
import datetime
import json
import os

from ..damsuite import chain

MANIFEST_SUFFIX = ".manifest.json"
FOLDER_MANIFEST = "run_manifest.json"
SCREENING_FOLDER = "_screening_model"
TOOL_NAME = "ND Dam Breach Screening"


def _version():
    try:
        from . import damrecord_io
        return damrecord_io.plugin_version()
    except Exception:                                            # noqa
        return ""


def tool_label(version=None):
    return "{} {}".format(TOOL_NAME, version or _version()).strip()


def source_of(value):
    """A file path from a parameter value: a path, 'path|layername=x', or a layer object with
    source(). None for anything else (memory layers, numbers, booleans ...)."""
    if value is None or isinstance(value, (bool, int, float)):
        return None
    if hasattr(value, "source") and callable(value.source):
        try:
            value = value.source()
        except Exception:                                        # noqa
            return None
    if not isinstance(value, str):
        return None
    p = value.split("|")[0]
    return p if p and os.path.exists(p) else None


def settings_of(params):
    """The parameters as JSON-able values (layers by their source, else repr)."""
    out = {}
    for k, v in (params or {}).items():
        if v is None or isinstance(v, (bool, int, float, str)):
            out[k] = v
        elif isinstance(v, (list, tuple)):
            out[k] = [x if isinstance(x, (bool, int, float, str)) else str(x) for x in v]
        else:
            src = source_of(v)
            out[k] = src if src else repr(v)[:200]
    return out


def output_paths(result):
    """{label: path} of the result values that are files or folders on disk."""
    out = {}
    for k, v in (result or {}).items():
        if isinstance(v, str) and v and v.upper() != "TEMPORARY_OUTPUT":
            p = v.split("|")[0]
            if os.path.exists(p):
                out[k] = p
    return out


def input_paths(params, result=None):
    """{label: path} of parameter values that are files / folders on disk, outputs excluded."""
    outs = set(os.path.abspath(p) for p in output_paths(result).values())
    out = {}
    for k, v in (params or {}).items():
        p = source_of(v)
        if p and os.path.abspath(p) not in outs:
            out[k] = p
    return out


def _digest(path):
    if os.path.isdir(path):
        # a folder: digest of the sorted (name, size, digest-of-small-files) listing
        import hashlib
        h = hashlib.sha256()
        n = 0
        for root, dirs, files in os.walk(path):
            dirs.sort()
            for f in sorted(files):
                if f.endswith(MANIFEST_SUFFIX) or f == FOLDER_MANIFEST:
                    continue
                fp = os.path.join(root, f)
                d = chain.file_digest(fp)
                h.update(os.path.relpath(fp, path).encode("utf-8", "replace"))
                h.update(str(d.get("sha256") or d.get("size") or "").encode())
                n += 1
        return {"sha256": h.hexdigest(), "files": n, "folder": True}
    return chain.file_digest(path)


def build(alg_id, params, result, version=None, extra=None):
    m = {"manifest_version": 1, "tool": tool_label(version), "algorithm": alg_id,
         "created": datetime.datetime.now().isoformat(timespec="seconds"),
         "settings": settings_of(params),
         "inputs": {k: dict(path=p, **_digest(p)) for k, p in input_paths(params, result).items()},
         "outputs": {k: dict(path=p, **_digest(p)) for k, p in output_paths(result).items()}}
    if extra:
        m["extra"] = extra
    m["manifest_sha256"] = chain.digest_of(m)
    return m


def manifest_path_for(output_path):
    """Where the manifest of an output lives."""
    if os.path.isdir(output_path):
        return os.path.join(output_path, FOLDER_MANIFEST)
    return output_path + MANIFEST_SUFFIX


def write(path, manifest):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=1, default=str)
    os.replace(tmp, path)
    return path


def record(alg_id, params, result, version=None, extra=None):
    """Write the manifest next to every file / folder output of a tool run. Returns the paths
    written ([] when the tool wrote nothing on disk). Never raises."""
    written = []
    try:
        outs = output_paths(result)
        if not outs:
            return written
        m = build(alg_id, params, result, version, extra)
        for p in outs.values():
            try:
                written.append(write(manifest_path_for(p), m))
            except OSError:
                pass
    except Exception:                                            # noqa
        return written
    return written


def read(path):
    try:
        with open(path, encoding="utf-8") as fh:
            m = json.load(fh)
        return m if isinstance(m, dict) else None
    except (OSError, ValueError):
        return None


def changed_inputs(manifest):
    """Labels of the manifest's inputs whose content differs now (or that are missing)."""
    out = []
    for k, rec in (manifest.get("inputs") or {}).items():
        p = rec.get("path")
        if not p:
            continue
        if not os.path.exists(p):
            out.append(k + " (missing)")
            continue
        now = _digest(p)
        if (now.get("sha256") or "") != (rec.get("sha256") or ""):
            out.append(k)
    return out


# ------------------------------------------------------------ per-dam manifests (tool 8) --
def dam_manifest(dam_folder, dam_id, rasters, result, params, version=None):
    """<dam folder>/_screening_model/run_manifest.json for one dam: its tool-7 rasters as
    outputs (the files ND Dam Hazard will classify) and the screening class. `rasters`:
    {kind: path} (depth / velocity / dv / arrival / increment), `result`: the tool-8 row."""
    outs = {k: p for k, p in (rasters or {}).items() if p and os.path.isfile(str(p))}
    if not outs:
        return None
    folder = os.path.join(dam_folder, SCREENING_FOLDER)
    os.makedirs(folder, exist_ok=True)
    m = {"manifest_version": 1, "tool": tool_label(version), "algorithm": "nddam:hazardclassification",
         "created": datetime.datetime.now().isoformat(timespec="seconds"),
         "dam": {"dam_id": dam_id},
         "settings": settings_of({k: v for k, v in (params or {}).items()
                                  if k in ("CRIT", "MIND_FT", "RDEP_FT", "RULE", "INCREMENTAL", "CHART_FILE")}),
         "inputs": {}, "outputs": {k: dict(path=str(p), **chain.file_digest(str(p))) for k, p in outs.items()},
         "extra": {"screening_result": {k: v for k, v in (result or {}).items()
                                        if isinstance(v, (str, int, float, bool)) or v is None},
                   "note": "Screening-level rasters (metres) from ND Dam Breach Screening tool 7; the class "
                           "is a screening preview, not ND Dam Hazard's classification."}}
    m["manifest_sha256"] = chain.digest_of(m)
    return write(os.path.join(folder, FOLDER_MANIFEST), m)
