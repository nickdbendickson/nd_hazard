"""Model -> classification fingerprint chain.

The ND Dam Breach Runner writes `<dam folder>/_breach_model/run_manifest.json` after each run:
software versions, input files and output rasters with their digests, and a digest of the
manifest itself. This module checks that the rasters a hazard classification read are the
rasters that run wrote.

`file_digest`, `_canonical` and `digest_of` reproduce nd_breach_runner/core/provenance.py
exactly (same keys, same 8 MB start / middle / end sampling above `FULL_HASH_LIMIT`, same
canonical JSON), so digests compare directly with the runner's manifest. The suite's tests
prove the equality against the runner's module; if the runner's algorithm changes, change
it here too.

compare_rasters decisions:
  * `classified` is {role: path}; a relative path is taken relative to `dam_folder`.
  * The manifest's "outputs" are searched first, then its "inputs" (the note says so).
  * Match by digest first (sha256, and size when both are recorded) -> "MATCH"; then by file
    name (case-insensitive) -> "DIFFERENT"; then by role vs label ("base_depth" ~ "baseline
    depth", "incr_depth" ~ "incremental depth") -> "DIFFERENT"; else "NOT IN MANIFEST".
  * A classified raster that no longer exists gets status "MISSING" (an addition to the
    spec's three statuses: nothing can be compared).
  * A recorded manifest path that does not exist is looked up as <dam_folder>/<file name>
    (as the runner's verify_manifest does when the dam folder moved).
Nothing here needs QGIS.
"""
import datetime
import glob
import hashlib
import json
import os

FULL_HASH_LIMIT = 2 * 1024 ** 3        # as nd_breach_runner/core/provenance.py
PIECE = 8 * 1024 ** 2                  # as nd_breach_runner/core/provenance.py
MANIFEST_NAME = "run_manifest.json"
MODEL_FOLDER = "_breach_model"
# ND Dam Breach Screening 0.11: its tools 7 / 8 write the same manifest format into
# <dam>/_screening_model/run_manifest.json (tool "ND Dam Breach Screening <version>")
SCREENING_FOLDER = "_screening_model"
STATUSES = ("MATCH", "DIFFERENT", "NOT IN MANIFEST", "MISSING")
_cache = {}


def sha256_file(path):
    """SHA-256 (hex) of the whole file."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def file_digest(path, full_limit=FULL_HASH_LIMIT):
    """{"sha256", "size", "mtime", "sampled"} of a file, or {"missing": True}.

    Identical to the ND Dam Breach Runner's core/provenance.file_digest."""
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


def _canonical(obj):
    return json.dumps(obj, sort_keys=True, default=str, separators=(",", ":"))


def digest_of(manifest):
    """The runner's manifest digest: SHA-256 of the canonical JSON without "manifest_sha256"."""
    body = {k: v for k, v in manifest.items() if k != "manifest_sha256"}
    return hashlib.sha256(_canonical(body).encode()).hexdigest()


def find_runner_manifest(dam_folder):
    """Newest run_manifest.json of a dam folder: <dam>/_breach_model/ (the Runner),
    <dam>/_screening_model/ (Screening 0.11+), one folder level further down
    (<dam>/<run>/_breach_model/), or the folder itself when it is the model folder. A manifest
    file path is returned as given. None when there is none. The newest file wins, so a
    Screening manifest written after a Runner run is the one found - the manifest's "tool" says
    which tool wrote it."""
    if not dam_folder:
        return None
    if os.path.isfile(dam_folder):
        return dam_folder if os.path.basename(dam_folder).lower() == MANIFEST_NAME else None
    cands = [os.path.join(dam_folder, MODEL_FOLDER, MANIFEST_NAME),
             os.path.join(dam_folder, SCREENING_FOLDER, MANIFEST_NAME),
             os.path.join(dam_folder, MANIFEST_NAME)]
    cands += glob.glob(os.path.join(glob.escape(dam_folder), "*", MODEL_FOLDER, MANIFEST_NAME))
    found = []
    for p in cands:
        try:
            if os.path.isfile(p):
                found.append((os.path.getmtime(p), p))
        except OSError:
            pass
    if not found:
        return None
    return max(found)[1]


def read_runner_manifest(path):
    """The manifest dict. Raises OSError, or ValueError when it is not a JSON object."""
    with open(path, encoding="utf-8") as f:
        m = json.load(f)
    if not isinstance(m, dict):
        raise ValueError("{} is not a JSON object".format(path))
    return m


def manifest_ok(manifest):
    """(ok, text): does the manifest's own digest match its content (the runner's check)?"""
    if not isinstance(manifest, dict):
        return False, "not a manifest (not a JSON object)"
    rec = manifest.get("manifest_sha256")
    if not rec:
        return False, "the manifest carries no digest (manifest_sha256)"
    if digest_of(manifest) != rec:
        return False, "the manifest itself was edited after it was written"
    return True, "the manifest digest matches its content"


_ALIASES = {"base": "baseline", "incr": "incremental", "inc": "incremental",
            "increment": "incremental", "vel": "velocity", "arr": "arrival", "hmax": "depth",
            "max": ""}


def _norm_role(s):
    words = str(s or "").lower().replace("_", " ").replace("-", " ").replace("(", " ") \
        .replace(")", " ").split()
    words = [_ALIASES.get(w, w) for w in words]
    words = [w for w in words if w and w != "time"]
    return " ".join(words)


def _entries(manifest):
    out = []
    for group in ("outputs", "inputs"):
        for label, rec in (manifest.get(group) or {}).items():
            if isinstance(rec, dict):
                out.append((group, label, rec))
    return out


def _where(path, dam_folder):
    if path and not os.path.exists(path) and dam_folder:
        alt = os.path.join(dam_folder, os.path.basename(path))
        if os.path.exists(alt):
            return alt
    return path


def compare_rasters(classified, manifest, dam_folder=None):
    """Rows comparing each classified raster {role: path} with the runner's manifest."""
    manifest = manifest or {}
    entries = _entries(manifest)
    rows = []
    for role, path in (classified or {}).items():
        path = str(path or "")
        if path and not os.path.isabs(path) and dam_folder:
            path = os.path.join(dam_folder, path)
        d = file_digest(path) if path else {"missing": True}
        row = {"role": role, "classified_path": path,
               "classified_sha256": d.get("sha256", ""), "manifest_label": "",
               "manifest_path": "", "status": "", "note": ""}
        hit, how = None, ""
        if not d.get("missing"):
            for e in entries:
                rec = e[2]
                if rec.get("sha256") and rec.get("sha256") == d["sha256"] and \
                        (rec.get("size") is None or rec.get("size") == d["size"]):
                    hit, how = e, "digest"
                    break
        name = os.path.basename(path).lower()
        if hit is None and name:
            for e in entries:
                if os.path.basename(str(e[2].get("path", ""))).lower() == name:
                    hit, how = e, "name"
                    break
        if hit is None:
            nr = _norm_role(role)
            for e in entries:
                if nr and _norm_role(e[1]) == nr:
                    hit, how = e, "role"
                    break
        if hit is not None:
            group, label, rec = hit
            row["manifest_label"] = label
            row["manifest_path"] = _where(str(rec.get("path", "")), dam_folder)
        notes = []
        if d.get("missing"):
            row["status"] = "MISSING"
            notes.append("the classified raster is not there any more; nothing to compare")
        elif how == "digest":
            row["status"] = "MATCH"
            notes.append("same digest as the run's '{}'".format(hit[1]))
            if d.get("sampled"):
                notes.append("large file: sampled digest (start, middle, end)")
        elif how in ("name", "role"):
            row["status"] = "DIFFERENT"
            if hit[2].get("missing") or not hit[2].get("sha256"):
                notes.append("the manifest recorded '{}' without a digest".format(hit[1]))
            else:
                notes.append("same {} as the run's '{}' but another digest: the raster was "
                             "replaced or changed after the run".format(
                                 "file name" if how == "name" else "role", hit[1]))
        else:
            row["status"] = "NOT IN MANIFEST"
            notes.append("no raster of the run has this digest, file name or role")
        if hit is not None and hit[0] == "inputs":
            notes.append("found among the run's inputs")
        row["note"] = "; ".join(notes)
        rows.append(row)
    return rows


def chain_summary(rows):
    """Plain-text summary of compare_rasters rows (for a submittal / log)."""
    rows = list(rows or [])
    if not rows:
        return "No classified rasters to compare with the breach run manifest."
    counts = {s: 0 for s in STATUSES}
    for r in rows:
        counts[r.get("status")] = counts.get(r.get("status"), 0) + 1
    head = "Rasters of the classification vs the breach run manifest: {} of {} match".format(
        counts["MATCH"], len(rows))
    extra = ["{} {}".format(counts[s], s) for s in STATUSES[1:] if counts.get(s)]
    lines = [head + (" ({}).".format(", ".join(extra)) if extra else ".")]
    for r in rows:
        lines.append("  {}: {}{} - {}".format(
            r.get("role"), r.get("status"),
            " ('{}')".format(r["manifest_label"]) if r.get("manifest_label") else "",
            r.get("note", "")))
    if counts["MATCH"] == len(rows):
        lines.append("The classification used the rasters this run wrote.")
    else:
        lines.append("The chain is NOT complete: the classification did not (provably) use "
                     "only the rasters this run wrote.")
    return "\n".join(lines)
