# -*- coding: utf-8 -*-
"""v2.12.0: content fingerprints of the files a run reads.

Until 2.11 the evaluation key (pipeline.evaluation_key) and the product
records (pipeline.reusable_products) told a changed input by its path, size,
modification time and an MD5 of its first and last 64 KB. A file touched
without a change (copied, re-saved by a sync tool, time stamps lost in a
zip) was "out of date" and forced a full run; a raster rewritten with other
values in its middle but the same size and date passed as unchanged.

Now a file is known by its CONTENT: damsuite.chain.file_digest - the SHA-256
of the whole file, or, above chain.FULL_HASH_LIMIT (2 GB), the SHA-256 of
its size and three 8 MB pieces (start, middle, end), the same digest the ND
Dam Breach Runner writes into its run manifest - and its size. Dates are not
part of it. The digest is taken again every time it is asked for (chain's
per-process cache, keyed by size and date, is bypassed) so a raster
rewritten with the same size and date is still seen; the Runner's full
digest of a 1 GB raster takes a few seconds.

QGIS project layers without a file (memory layers) keep the 2.11 rule
(name, source, feature count, MD5 of the features) in pipeline._layer_sig.

Also: `digest_records` for QA/Run_Settings.json ("evaluation" block) and the
run log. Pure Python - no QGIS.
"""

import os
import re

from .damsuite import chain

SAMPLED_NOTE = "sampled: SHA-256 of the size and three 8 MB pieces (start, middle, end)"


def _forget_cached(path):
    """Drop chain.file_digest's per-process entries for `path` (they are keyed
    by size and date, which is what this module must not trust)."""
    try:
        ap = os.path.abspath(path)
        for key in [k for k in chain._cache if isinstance(k, tuple) and k and k[0] == ap]:
            chain._cache.pop(key, None)
    except Exception:                                        # noqa - a cache, nothing more
        pass


def content_digest(path, full_limit=chain.FULL_HASH_LIMIT):
    """{"sha256", "size", "sampled"} of a file's content (no date), or
    {"missing": True} when it cannot be read."""
    if not path:
        return {"missing": True}
    _forget_cached(path)
    # FP.1: never trust the persistent size+mtime digest cache for evaluation keys
    d = chain.file_digest(path, full_limit=full_limit, use_disk_cache=False)
    if d.get("missing"):
        return {"missing": True}
    return {"sha256": d.get("sha256"), "size": d.get("size"), "sampled": bool(d.get("sampled"))}


_VRT_SRC_RE = re.compile(
    r"<SourceFilename\b([^>]*)>([^<]+)</SourceFilename>", re.I)
_VRT_REL_RE = re.compile(r'relativeToVRT\s*=\s*"?([01])"?', re.I)


def _vrt_source_paths(path, limit=2000, _seen=None):
    """Source file paths a VRT reads (nested VRTs followed; cycles skipped).
    Returns (paths, incomplete): incomplete when the tile cap truncated the list."""
    _seen = set() if _seen is None else _seen
    ap = os.path.abspath(path)
    if ap in _seen:
        return [], False
    _seen.add(ap)
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return None, False
    base = os.path.dirname(path)
    found, incomplete = [], False
    for attrs, name in _VRT_SRC_RE.findall(text):
        name = html_unescape(name.strip())
        m = _VRT_REL_RE.search(attrs or "")
        rel = m.group(1) if m else "1"          # GDAL default: relative to the VRT
        src = os.path.join(base, name) if rel == "1" else name
        if len(found) >= limit:
            incomplete = True
            break
        if src.lower().endswith(".vrt"):
            nested, nest_inc = _vrt_source_paths(src, limit - len(found), _seen)
            if nested is None:
                found.append(src)
            else:
                found.extend(nested)
                incomplete = incomplete or nest_inc
        else:
            found.append(src)
    return found, incomplete


def html_unescape(text):
    return (text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
            .replace("&quot;", '"').replace("&apos;", "'"))


def _vrt_sources(path, limit=2000):
    """[(name, content digest)] of the files a VRT reads (a tile rewritten
    behind an unchanged VRT is a changed raster). Last item may be
    ["incomplete", True] when the source list was truncated (FP.2)."""
    paths, incomplete = _vrt_source_paths(path, limit)
    if paths is None:
        return None
    out = [[os.path.basename(src), content_digest(src)] for src in paths]
    if incomplete:
        out.append(["incomplete", True])
    return out


def file_sig(path):
    """The evaluation-key signature of a file: path, size, content digest
    (and, for a VRT, its sources'; for a GeoPackage, its -wal journal's).
    No modification time. [path, None] when the file cannot be read.
    A truncated VRT source list is marked incomplete so the signature changes
    rather than silently ignoring tiles (FP.2)."""
    d = content_digest(path)
    if d.get("missing"):
        return [path, None]
    sig = [path, d["size"], d["sha256"], "sampled" if d["sampled"] else "full"]
    if path.lower().endswith(".vrt"):
        sig.append(_vrt_sources(path))
    wal = path + "-wal"                     # GeoPackage edits not yet in the main file
    if os.path.isfile(wal):
        sig.append(["wal", content_digest(wal)])
    return sig


def stamp(path):
    """[size, sha256] of a file (product records), or None when missing."""
    d = content_digest(path)
    if d.get("missing"):
        return None
    return [d["size"], d["sha256"]]


def digest_records(paths):
    """{path: content digest} for Run_Settings.json / the run log, in the
    order given, each path once."""
    out = {}
    for p in paths:
        if not p or p in out:
            continue
        out[p] = content_digest(p)
    return out


def short(d):
    """'sha256 0123abcd... (sampled)' for log lines."""
    if not d or d.get("missing"):
        return "missing"
    return "sha256 {}{}".format((d.get("sha256") or "")[:16],
                                " (sampled)" if d.get("sampled") else "")
