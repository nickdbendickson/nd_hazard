"""Carrying the offline pack on a USB stick (ND Data Updater 1.4.0).

The pack is 10-40 GB and takes about 90 minutes of downloading to build. A field laptop with no
internet cannot build one, and it does not have to: the office machine's pack is a folder of
GeoPackages and a manifest, and copying it is enough. The only thing a copy needs that a plain
drag-and-drop does not give is proof that it arrived whole - a USB stick pulled out mid-write
leaves a GeoPackage that opens and returns half a county, which is worse than no pack at all.
So a copy carries CHECKSUMS.sha256 (the same format as sha256sum: "<hex>  <relative path>"),
written after the copy, and an import verifies every file against it before the pack is put in
place.

    copy_pack(pack, "/Volumes/USB")          -> /Volumes/USB/ND_Dam_Suite_Pack (+ checksums)
    import_pack("/Volumes/USB/ND_Dam_Suite_Pack", data_folder)

Decisions:
  * The copy is written to "<name>.incoming" and renamed when it is complete, and an existing
    pack at the destination is moved aside to "<name>.previous" and deleted only after the
    rename succeeded. A cancelled or failed copy therefore never destroys the pack that was
    already there - the same rule the updater's staging swap follows.
  * Verifying is a choice at import time, not at copy time: on a slow stick, hashing 30 GB again
    doubles the wait, and the caller (the pack window) says whether to do it. A pack with no
    checksum file is refused by default, because "it looks like a pack" is not proof.
  * Nothing here adopts the pack into the plugins' settings: the caller does that with
    damsuite.suitedata.adopt_folder, so that one code path sets the folder, the pack location and
    the data source (the drift between those settings was the commonest first-run problem).
  * Progress is reported in bytes, not files: the pack is a few big files and many small ones, so
    counting files makes the bar jump.
Pure Python standard library; no QGIS.
"""

import hashlib
import os
import shutil

MANIFEST_NAME = "manifest.json"
SUMS_NAME = "CHECKSUMS.sha256"
PACK_FOLDER = "ND_Dam_Suite_Pack"
SKIP_NAMES = (SUMS_NAME,)
SKIP_SUFFIXES = (".part", ".tmp", ".incoming")
CHUNK = 1 << 20


class PackMoveError(Exception):
    """The copy or the import could not be done (and nothing was changed)."""


def is_pack(folder):
    """True when `folder` looks like a pack folder (it holds a manifest)."""
    return bool(folder) and os.path.isfile(os.path.join(folder, MANIFEST_NAME))


def pack_files(folder):
    """[(relative path, size)] of the files that belong to the pack, sorted.

    Working files (.part, .tmp) and the checksum file itself are left out: they are not data,
    and a half-written download must not make a copy fail its own checksum."""
    out = []
    for root, _dirs, files in os.walk(folder):
        for name in sorted(files):
            if name in SKIP_NAMES or name.endswith(SKIP_SUFFIXES):
                continue
            path = os.path.join(root, name)
            rel = os.path.relpath(path, folder).replace(os.sep, "/")
            try:
                out.append((rel, os.path.getsize(path)))
            except OSError:
                continue
    return sorted(out)


def total_size(folder):
    return sum(size for _rel, size in pack_files(folder))


def sha256_file(path, chunk=CHUNK, tick=None):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            block = f.read(chunk)
            if not block:
                break
            h.update(block)
            if tick:
                tick(len(block))
    return h.hexdigest()


def checksums(folder, progress=None, cancelled=None):
    """{relative path: sha256} for the pack's files. `progress(done_bytes, total_bytes)`."""
    rows = pack_files(folder)
    total = sum(s for _r, s in rows) or 1
    done = [0]

    def tick(n):
        done[0] += n
        if progress:
            progress(done[0], total)

    out = {}
    for rel, _size in rows:
        if cancelled and cancelled():
            raise PackMoveError("cancelled")
        out[rel] = sha256_file(os.path.join(folder, *rel.split("/")), tick=tick)
    return out


def sums_text(sums):
    """The checksum file's contents (sha256sum's format, so any machine can check it)."""
    return "".join(f"{sums[rel]}  {rel}\n" for rel in sorted(sums))


def parse_sums(text):
    """{relative path: sha256} from a checksum file (lines it cannot read are ignored)."""
    out = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 1)
        if len(parts) != 2 or len(parts[0]) != 64:
            continue
        out[parts[1].strip().lstrip("*").replace(os.sep, "/")] = parts[0].lower()
    return out


def write_sums(folder, sums=None, progress=None):
    """Write CHECKSUMS.sha256 in the pack folder. Returns its path."""
    sums = checksums(folder, progress) if sums is None else sums
    path = os.path.join(folder, SUMS_NAME)
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        f.write(sums_text(sums))
    os.replace(path + ".tmp", path)
    return path


def read_sums(folder):
    """{relative path: sha256} from the pack's checksum file ({} when there is none)."""
    try:
        with open(os.path.join(folder, SUMS_NAME), encoding="utf-8") as f:
            return parse_sums(f.read())
    except OSError:
        return {}


def compare_sums(want, have):
    """[(relative path, what is wrong)] between the recorded and the computed checksums."""
    bad = []
    for rel in sorted(want):
        if rel not in have:
            bad.append((rel, "missing from the copy"))
        elif have[rel] != want[rel]:
            bad.append((rel, "different contents (the copy is damaged)"))
    for rel in sorted(have):
        if rel not in want:
            bad.append((rel, "not listed in CHECKSUMS.sha256 (added after the copy)"))
    return bad


def verify(folder, progress=None, cancelled=None):
    """(ok, [(file, what is wrong)]) for a pack against its own checksum file."""
    want = read_sums(folder)
    if not want:
        return False, [(SUMS_NAME, "there is no checksum file in this pack")]
    bad = compare_sums(want, checksums(folder, progress, cancelled))
    return (not bad), bad


def _copy_tree(src, dest, progress=None, cancelled=None, log=None):
    """Copy the pack's files one by one, reporting bytes, so a 30 GB copy shows progress."""
    rows = pack_files(src)
    total = sum(s for _r, s in rows) or 1
    done = 0
    for rel, size in rows:
        if cancelled and cancelled():
            raise PackMoveError("cancelled")
        s = os.path.join(src, *rel.split("/"))
        d = os.path.join(dest, *rel.split("/"))
        os.makedirs(os.path.dirname(d), exist_ok=True)
        try:
            shutil.copy2(s, d)
        except OSError as e:
            raise PackMoveError(f"could not copy {rel}: {e}") from e
        done += size
        if progress:
            progress(done, total)
        if log and size > 256 * 1024 * 1024:
            log(f"copied {rel} ({size / 1073741824.0:.1f} GB)")
    return done


def _install(incoming, dest):
    """Put a finished copy in place, keeping the old one until the rename worked."""
    previous = dest + ".previous"
    shutil.rmtree(previous, ignore_errors=True)
    if os.path.exists(dest):
        os.rename(dest, previous)
    try:
        os.rename(incoming, dest)
    except OSError as e:
        if os.path.exists(previous) and not os.path.exists(dest):
            os.rename(previous, dest)
        raise PackMoveError(f"could not put the pack in place: {e}") from e
    shutil.rmtree(previous, ignore_errors=True)
    return dest


def copy_pack(pack, dest_root, progress=None, cancelled=None, log=None, name=None):
    """Copy the pack folder to `dest_root` and write its checksum file beside the data.

    Returns {"folder", "bytes", "files", "sums"}: where it went and what it carries."""
    log = log or (lambda m: None)
    if not is_pack(pack):
        raise PackMoveError(f"{pack} is not a pack folder (no {MANIFEST_NAME} in it)")
    if not dest_root:
        raise PackMoveError("no destination was chosen")
    dest = os.path.join(dest_root, name or os.path.basename(pack.rstrip("\\/")) or PACK_FOLDER)
    if os.path.abspath(dest) == os.path.abspath(pack):
        raise PackMoveError("the destination is the pack itself")
    incoming = dest + ".incoming"
    shutil.rmtree(incoming, ignore_errors=True)
    os.makedirs(incoming, exist_ok=True)
    try:
        log(f"copying {total_size(pack) / 1073741824.0:.1f} GB to {dest}")
        wrote = _copy_tree(pack, incoming, progress, cancelled, log)
        sums = checksums(incoming)
        write_sums(incoming, sums)
        _install(incoming, dest)
    except BaseException:
        shutil.rmtree(incoming, ignore_errors=True)
        raise
    log(f"copied {len(sums)} file(s) to {dest} with {SUMS_NAME}")
    return {"folder": dest, "bytes": wrote, "files": len(sums), "sums": sums}


def import_pack(pack, dest_root, progress=None, cancelled=None, log=None, check=True,
                name=None):
    """Copy a pack from a USB stick (or any folder) into `dest_root`, verified.

    The checksums are compared against the source before anything is put in place, so a damaged
    stick is caught before it replaces a good pack. Returns {"folder", "files", "bytes",
    "checked"}."""
    log = log or (lambda m: None)
    if not is_pack(pack):
        raise PackMoveError(f"{pack} is not a pack folder (no {MANIFEST_NAME} in it). Choose the "
                            f"folder that holds {MANIFEST_NAME} (usually {PACK_FOLDER}).")
    want = read_sums(pack)
    if check:
        if not want:
            raise PackMoveError(
                f"this pack has no {SUMS_NAME}, so there is no way to tell whether it arrived "
                "whole. Copy it again with \"Copy pack to USB\", or import it without checking.")
        log(f"checking {len(want)} file(s) against {SUMS_NAME}...")
        bad = compare_sums(want, checksums(pack, progress, cancelled))
        if bad:
            first = "; ".join(f"{rel}: {why}" for rel, why in bad[:5])
            raise PackMoveError(f"the pack on this drive is damaged ({len(bad)} file(s)): "
                                f"{first}. Nothing was changed; copy it again.")
        log("every file matches its checksum")
    dest = os.path.join(dest_root, name or os.path.basename(pack.rstrip("\\/")) or PACK_FOLDER)
    if os.path.abspath(dest) == os.path.abspath(pack):
        raise PackMoveError("the pack is already in this folder")
    incoming = dest + ".incoming"
    shutil.rmtree(incoming, ignore_errors=True)
    os.makedirs(incoming, exist_ok=True)
    try:
        wrote = _copy_tree(pack, incoming, progress, cancelled, log)
        if want:
            write_sums(incoming, want)
        _install(incoming, dest)
    except BaseException:
        shutil.rmtree(incoming, ignore_errors=True)
        raise
    log(f"imported {dest}")
    return {"folder": dest, "files": len(pack_files(dest)), "bytes": wrote, "checked": bool(check)}


def adopt(folder):
    """Hand the imported pack's folder to the three plugins (one folder choice sets up all).

    Returns suitedata.adopt_folder's dict, or {} when the suite settings cannot be reached."""
    try:
        from .ndpack.damsuite import suitedata as SD
        return SD.adopt_folder(folder) or {}
    except Exception:  # noqa: BLE001 - a settings problem must not undo a good import
        return {}
