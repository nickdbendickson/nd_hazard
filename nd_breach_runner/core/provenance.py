"""Run manifest: what produced a set of results, so a submittal can be reproduced and checked.

`build_manifest` collects the software versions, the model executables (with SHA-256), every
input file (path, size, modification time, SHA-256), the settings, the dam data and the output
rasters (SHA-256). The manifest carries a digest of its own content. `verify_manifest`
recomputes the hashes and says which inputs or outputs changed since the run.

A file larger than `FULL_HASH_LIMIT` is hashed from its size and three 8 MB pieces (start,
middle, end) and marked "sampled": it catches edits and replacements, not every possible
change. Nothing here needs QGIS or GDAL; their versions are recorded when importable.
"""
import datetime
import hashlib
import json
import os
import platform
import sys

FULL_HASH_LIMIT = 2 * 1024 ** 3
PIECE = 8 * 1024 ** 2
MANIFEST_NAME = "run_manifest.json"
_cache = {}


def file_digest(path, full_limit=FULL_HASH_LIMIT):
    """{"sha256", "size", "mtime", "sampled"} of a file, or {"missing": True}."""
    try:
        st = os.stat(path)
    except OSError:
        return {"missing": True}
    key = (os.path.abspath(path), st.st_size, st.st_mtime_ns, full_limit)
    if key in _cache:
        return dict(_cache[key])
    h = hashlib.sha256()
    sampled = st.st_size > full_limit
    try:
        with open(path, "rb") as f:
            if not sampled:
                for block in iter(lambda: f.read(1 << 20), b""):
                    h.update(block)
            else:
                h.update(str(st.st_size).encode())
                for pos in (0, max(0, st.st_size // 2 - PIECE // 2), max(0, st.st_size - PIECE)):
                    f.seek(pos)
                    h.update(f.read(PIECE))
    except OSError as e:
        return {"missing": True, "error": str(e)}
    out = {"sha256": h.hexdigest(), "size": st.st_size,
           "mtime": datetime.datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
           "sampled": sampled}
    _cache[key] = dict(out)
    return out


def software_versions(exe="", anuga_python=""):
    import numpy as np
    v = {"python": sys.version.split()[0], "platform": platform.platform(),
         "numpy": np.__version__}
    try:
        from osgeo import gdal
        v["gdal"] = gdal.__version__
    except Exception:
        pass
    try:
        from qgis.core import Qgis
        v["qgis"] = Qgis.QGIS_VERSION
    except Exception:
        pass
    try:
        from scipy import __version__ as sv
        v["scipy"] = sv
    except Exception:
        pass
    if exe:
        v["lisflood_exe"] = dict(path=exe, **file_digest(exe))
    if anuga_python:
        v["anuga_python"] = anuga_python
    return v


def _canonical(obj):
    return json.dumps(obj, sort_keys=True, default=str, separators=(",", ":"))


def digest_of(manifest):
    body = {k: v for k, v in manifest.items() if k != "manifest_sha256"}
    return hashlib.sha256(_canonical(body).encode()).hexdigest()


def build_manifest(tool_version, settings, dam, dam_xy, inflow_point, hydrograph_summary,
                   input_files=None, outputs=None, exe="", anuga_python="", extra=None,
                   started=None):
    """A manifest dict. `input_files` {label: path} and `outputs` {label: path}."""
    m = {"manifest_version": 1,
         "tool": tool_version,
         "created": datetime.datetime.now().isoformat(timespec="seconds"),
         "run_started": started,
         "software": software_versions(exe, anuga_python),
         "settings": settings, "dam": dam, "dam_xy": list(dam_xy) if dam_xy else None,
         "inflow_point": inflow_point, "hydrograph": hydrograph_summary,
         "inputs": {k: dict(path=p, **file_digest(p)) for k, p in (input_files or {}).items()
                    if p},
         "outputs": {k: dict(path=p, **file_digest(p)) for k, p in (outputs or {}).items()
                     if p and os.path.exists(p)}}
    if extra:
        m["extra"] = extra
    m["manifest_sha256"] = digest_of(m)
    return m


def write_manifest(path, manifest):
    with open(path, "w") as f:
        json.dump(manifest, f, indent=2, default=str)
    return path


def flatten_outputs(*groups):
    """{label: path} from dicts of kind->path (final, baseline, increment ...)."""
    out = {}
    for prefix, g in groups:
        for k, p in (g or {}).items():
            out["{}{}".format(prefix, k)] = p
    return out


def verify_manifest(path, folder=None):
    """Check a manifest file. Returns {"ok", "problems", "checked", "manifest"}.

    The manifest's own digest, then each input and output against its recorded hash. Paths are
    tried as recorded and, when missing, relative to `folder` (the dam folder, if it moved)."""
    with open(path) as f:
        m = json.load(f)
    problems = []
    if digest_of(m) != m.get("manifest_sha256"):
        problems.append("the manifest itself was edited after it was written")
    checked = 0
    for group in ("inputs", "outputs"):
        for label, rec in (m.get(group) or {}).items():
            p = rec.get("path", "")
            if not os.path.exists(p) and folder:
                alt = os.path.join(folder, os.path.basename(p))
                if os.path.exists(alt):
                    p = alt
            if rec.get("missing"):
                continue
            d = file_digest(p)
            checked += 1
            if d.get("missing"):
                problems.append("{} '{}' is missing: {}".format(group[:-1], label, rec.get("path")))
            elif d.get("sha256") != rec.get("sha256") or d.get("size") != rec.get("size"):
                problems.append("{} '{}' changed since the run: {}".format(
                    group[:-1], label, p))
    exe = (m.get("software") or {}).get("lisflood_exe")
    if exe and exe.get("sha256"):
        d = file_digest(exe.get("path", ""))
        checked += 1
        if d.get("missing"):
            problems.append("the LISFLOOD-FP executable is not at {} any more".format(
                exe.get("path")))
        elif d.get("sha256") != exe["sha256"]:
            problems.append("the LISFLOOD-FP executable at {} is not the one used for the run "
                            "(different SHA-256)".format(exe.get("path")))
    return {"ok": not problems, "problems": problems, "checked": checked, "manifest": m}
