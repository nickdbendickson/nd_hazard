"""packcheck - check an offline data pack against its manifest.json (1.17.5).

A format-2 pack is a folder: manifest.json, LAYERS.txt and one GeoPackage per layer
(gpkgpack). The reader (gpkgpack.GpkgPackReader) quietly skips a layer whose file is gone,
so a pack that lost a file (hydro/NHD_waterbodies.gpkg, October 2026) looked fine and the
plugins fell back to "no waterbody layer" without saying why. This module says why.

    detect_format(path)  -> 1 (a .sqlite / .zip / folder of nd_offline_pack.sqlite),
                            2 (a folder, or a file in a folder, with a format-2 manifest.json,
                            or a .zip that holds one), or 0 (not a pack)
    check_pack(path, required=(), deep=True) -> PackCheck

For every data set in the manifest, check_pack looks at:
  * the file (`file`, relative to the folder) is there;
  * the GeoPackage opens and has the table (`layer`, default the data set name);
  * the feature count = `count` (deep only: a COUNT(*); gpkg_ogr_contents when GDAL wrote it);
  * the columns = the first item of each `fields` entry ([column, property, kind]);
  * the geometry type = `geometry` (gpkg_geometry_columns; GEOMETRY in the file accepts any);
  * the CRS = the data set's `crs`, else the manifest's `crs`, else EPSG:4326 (what
    LAYERS.txt promises for every vector layer). Rasters are checked only for their file and
    table, and for the CRS when the manifest gives one.
Data sets the builder could not finish (status other than complete / partial) and `left_out`
entries are listed as notes: the plugins already download those.

Nothing is written: every GeoPackage is opened read-only (mode=ro, query_only). Pure standard
library (sqlite3, json). Results are cached in memory by the manifest's and the files' size and
time, so a plugin may call check_pack at every pack open.

`required` names layers the caller cannot work without. A required layer that the manifest
lists but that is missing or unreadable, or that the manifest does not list at all, is an
ERROR (PackCheck.missing_required); everything else is a WARNING or a NOTE. check_pack never
raises; PackCheck.raise_if_required_missing() raises PackCheckError with a clear message.

1.17.6 (#171 follow-up): a manifest.json that cannot be read (not JSON, not an object, a
UTF-8 byte-order mark - the pack readers decode plain UTF-8 -, or a section - datasets /
rasters / left_out - of the wrong type) and any unexpected error of the check itself give a
FAILED result (PackCheck.failed, an ERROR of kind "manifest" / "check", every required layer
in missing_required) instead of an exception or a silent "not an offline data pack". One
entry of the wrong type (not an object, a `file` that is not text) is not FAILED: it is a
WARNING of kind "manifest" for that layer (an ERROR when the layer is required). A
GeoPackage whose SQLite header is bad or that is shorter than its header says (a
half-written copy) is "unreadable"; integrity=True also runs SQLite's quick_check (reads
the whole file). A zipped format-2 pack is checked against
its zip listing (every listed file present, not empty; the full check runs on the unpacked
folder). pack_signature(path) gives every listed file's size and time for run signatures.
A format-1 pack (one nd_offline_pack.sqlite) is checked lightly: each usable data set of its
datasets table has its ds_ table and (deep) that many rows, and manifest.json lists nothing
the .sqlite lacks. It has no per-layer fields, geometry type or CRS to compare.
"""
import json
import os
import pathlib
import sqlite3
import threading
import zipfile

ERROR, WARNING, NOTE = "error", "warning", "note"
DEFAULT_CRS = "EPSG:4326"
MANIFEST = "manifest.json"
DB_NAME = "nd_offline_pack.sqlite"
PACK_FORMAT = "nd-dam-hazard-offline-pack"
USABLE = ("complete", "partial")
# geometry types that mean the same for the check (a pack builder may write either)
_GEOM_SAME = {"MULTIPOINT": "POINT"}
_KIND_TYPES = {"int": ("INT",), "bool": ("INT", "BOOL"), "real": ("REAL", "FLOA", "DOUB", "NUM"),
               "text": ("TEXT", "CHAR", "CLOB", "VARCHAR"), "json": ("TEXT", "CHAR", "CLOB")}


class PackCheckError(Exception):
    """A layer the plugin needs is missing from the offline data pack."""


class Issue(object):
    __slots__ = ("level", "layer", "kind", "text")

    def __init__(self, level, layer, kind, text):
        self.level, self.layer, self.kind, self.text = level, layer, kind, text

    def line(self):
        tag = {ERROR: "ERROR", WARNING: "WARNING", NOTE: "note"}[self.level]
        return "{}: {}: {}".format(tag, self.layer or "pack", self.text)

    def __repr__(self):
        return "Issue({!r}, {!r}, {!r}, {!r})".format(self.level, self.layer, self.kind, self.text)


class PackCheck(object):
    """The result of check_pack. issues: [Issue]; checked: data set names looked at;
    missing_required: required names the pack cannot give."""

    def __init__(self, path, fmt, folder=""):
        self.path = path
        self.format = fmt
        self.folder = folder
        self.failed = False          # 1.17.6: the manifest or the check itself failed
        self.issues = []
        self.checked = []
        self.missing_required = []
        self.deep = True
        self.created = ""

    def add(self, level, layer, kind, text):
        self.issues.append(Issue(level, layer, kind, text))

    def of(self, *levels):
        return [i for i in self.issues if i.level in levels]

    @property
    def errors(self):
        return self.of(ERROR)

    @property
    def warnings(self):
        return self.of(WARNING)

    @property
    def ok(self):
        """True when nothing is missing or different (notes do not count)."""
        return not self.of(ERROR, WARNING)

    def bad_layers(self):
        """Names of the layers with an error or a warning, in manifest order."""
        out = []
        for i in self.of(ERROR, WARNING):
            if i.layer and i.layer not in out:
                out.append(i.layer)
        return out

    def summary(self):
        name = os.path.basename(str(self.folder or self.path or "")) or str(self.path)
        if self.failed:
            why = "; ".join(i.text for i in self.issues if i.kind in ("manifest", "check"))
            return "Offline pack check ({}): FAILED - {}{}".format(
                name, why or "the check could not run",
                "; REQUIRED layer(s) not usable: " + ", ".join(self.missing_required)
                if self.missing_required else "")
        if self.format not in (1, 2):
            return "Offline pack check ({}): not an offline data pack".format(name)
        head = "Offline pack check ({}, format {}, {} layer(s){}{})".format(
            name, self.format, len(self.checked), ", built " + self.created if self.created else "",
            "" if self.deep else ", quick check: no counts")
        if self.ok:
            return head + ": OK - every layer matches manifest.json"
        miss = sorted({i.layer for i in self.of(ERROR, WARNING) if i.layer and i.kind in (
            "missing", "table", "unreadable", "manifest")})
        diff = [n for n in self.bad_layers() if n not in miss]
        parts = []
        if self.missing_required:
            parts.append("REQUIRED layer(s) missing: " + ", ".join(self.missing_required))
        rest = [n for n in miss if n not in self.missing_required]
        if rest:
            parts.append("missing: " + ", ".join(rest))
        if diff:
            parts.append("not as in manifest.json: " + ", ".join(diff))
        # 1.17.6: a problem of the pack itself (no layer name: a format-1 pack's broken
        # manifest.json, an unreadable .sqlite) is named by its text, not as "missing: "
        parts += [i.text for i in self.of(ERROR, WARNING) if not i.layer]
        return head + ": " + "; ".join(parts)

    def lines(self, notes=True):
        """The summary, then one line per issue (notes too unless notes=False)."""
        out = [self.summary()]
        for i in self.issues:
            if notes or i.level != NOTE:
                out.append("  " + i.line())
        return out

    def raise_if_required_missing(self, who="This plugin"):
        if self.missing_required:
            raise PackCheckError(self.required_message(who))

    def required_message(self, who="This plugin"):
        why = []
        for name in self.missing_required:
            hit = [i.text for i in self.issues if i.layer == name and i.level == ERROR]
            why.append("{} ({})".format(name, hit[0]) if hit else name)
        return ("{} needs layer(s) missing from the offline data pack {}: {}. Rebuild or repair "
                "the pack (Update offline pack), or choose another pack.".format(
                    who, self.folder or self.path, "; ".join(why)))


# ----------------------------------------------------------- detection --
def _read_json(p):
    try:
        with open(p, encoding="utf-8") as fh:
            m = json.load(fh)
        return m if isinstance(m, dict) else None
    except (OSError, ValueError):
        return None


def _manifest_error(p):
    """Why manifest.json at `p` cannot be used as a JSON object, or "" (also "" when absent)."""
    if not os.path.isfile(p):
        return ""
    try:
        with open(p, "rb") as fh:
            raw = fh.read()
    except OSError as ex:
        return "manifest.json cannot be read: {}".format(ex)
    return _manifest_bytes_error(raw)


def _manifest_bytes_error(raw):
    # 1.17.6: a UTF-8 byte-order mark is a problem - the pack readers (gpkgpack,
    # offline_pack, detect_format) decode plain UTF-8 and refuse it
    if raw.startswith(b"\xef\xbb\xbf"):
        return ("manifest.json starts with a UTF-8 byte-order mark - the pack readers "
                "cannot read it; save it as UTF-8 without BOM")
    try:
        m = json.loads(raw.decode("utf-8"))
    except UnicodeDecodeError as ex:
        return "manifest.json is not UTF-8 text: {}".format(ex)
    except ValueError as ex:
        return "manifest.json is not valid JSON ({}; {:,} bytes - half written?)".format(ex, len(raw))
    if not isinstance(m, dict):
        return "manifest.json is a JSON {}, not an object".format(type(m).__name__)
    return ""


def manifest_problem(path):
    """1.17.6: why the manifest.json of `path` (a folder, a file in it, or a .zip) cannot be
    read or used (not UTF-8 / JSON / an object; a format-2 section of the wrong type), or ""
    when it reads (or there is none). Never raises."""
    try:
        if not path:
            return ""
        path = os.path.abspath(os.path.expanduser(str(path)))
        if os.path.isfile(path) and path.lower().endswith(".zip"):
            try:
                with zipfile.ZipFile(path) as z:
                    name = min((n for n in z.namelist() if n.rsplit("/", 1)[-1] == MANIFEST),
                               key=lambda n: n.count("/"), default=None)
                    if not name:
                        return ""
                    raw = z.read(name)
                    return _manifest_bytes_error(raw) or _shape_text(json.loads(
                        raw.decode("utf-8")))
            except (OSError, KeyError, ValueError, zipfile.BadZipFile) as ex:
                return "the zip cannot be read: {}".format(ex)
        folder = path if os.path.isdir(path) else os.path.dirname(path)
        mp = os.path.join(folder, MANIFEST)
        err = _manifest_error(mp)
        if err or not os.path.isfile(mp):
            return err
        return _shape_text(_read_json(mp))
    except Exception as ex:                                      # noqa: BLE001
        return "manifest.json cannot be read: {}".format(ex)


def _shape_text(m):
    """"" or the shape problems of a parsed manifest, joined (manifest_problem)."""
    if not isinstance(m, dict):
        return ""
    return "; ".join(manifest_shape_problems(m))


def manifest_shape_problems(m):
    """1.17.6: [text] for the parts of a format-2 manifest of the wrong type: the sections
    datasets / rasters / left_out must be objects. [] when the shape is usable."""
    out = []
    for sect in ("datasets", "rasters", "left_out"):
        v = m.get(sect)
        if v is not None and not isinstance(v, dict):
            out.append("manifest.json '{}' is a JSON {}, not an object".format(
                sect, type(v).__name__))
    return out


def _entries(m, sect):
    v = m.get(sect)
    return v if isinstance(v, dict) else {}


def _rel_file(e):
    """The entry's `file` as a str, or None when missing or of the wrong type."""
    f = e.get("file") if isinstance(e, dict) else None
    return f if isinstance(f, str) and f else None


def is_format2_manifest(m):
    return isinstance(m, dict) and m.get("format") == PACK_FORMAT and (
        m.get("layout") == "gpkg" or _int(m.get("format_version")) >= 2)


def _int(v):
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _zip_manifest(path):
    try:
        with zipfile.ZipFile(path) as z:
            name = min((n for n in z.namelist() if n.rsplit("/", 1)[-1] == MANIFEST),
                       key=lambda n: n.count("/"), default=None)
            if not name:
                return None
            m = json.loads(z.read(name).decode("utf-8"))
            return m if isinstance(m, dict) else None
    except (OSError, ValueError, KeyError, zipfile.BadZipFile):
        return None


def pack_folder(path):
    """The format-2 pack folder of `path` (the folder, or a file in it such as manifest.json
    or LAYERS.txt), or ""."""
    if not path:
        return ""
    path = os.path.abspath(os.path.expanduser(str(path)))
    folder = path if os.path.isdir(path) else os.path.dirname(path)
    return folder if is_format2_manifest(_read_json(os.path.join(folder, MANIFEST))) else ""


def detect_format(path):
    """1, 2 or 0 (see the module text). Never raises; reads only manifest.json."""
    if not path:
        return 0
    path = os.path.abspath(os.path.expanduser(str(path)))
    if not os.path.exists(path):
        return 0
    if os.path.isfile(path) and path.lower().endswith(".zip"):
        m = _zip_manifest(path)
        if m is None or m.get("format") != PACK_FORMAT:
            return 0
        return 2 if is_format2_manifest(m) else 1
    if os.path.isfile(path) and path.lower().endswith((".sqlite", ".db")):
        return 1
    if pack_folder(path):
        return 2
    folder = path if os.path.isdir(path) else os.path.dirname(path)
    if os.path.isfile(os.path.join(folder, DB_NAME)):
        return 1
    return 0


# --------------------------------------------------------------- reading --
def _ro(path):
    uri = pathlib.Path(os.path.abspath(path)).as_uri()
    if uri.startswith("file://") and not uri.startswith("file:///"):
        uri = "file:////" + uri[len("file://"):]
    con = sqlite3.connect(uri + "?mode=ro", uri=True, timeout=5)
    con.execute("PRAGMA query_only = ON")
    return con


def _q(name):
    return '"{}"'.format(str(name).replace('"', '""'))


def _crs_of(con, srs_id):
    if srs_id is None:
        return ""
    try:
        row = con.execute("SELECT organization, organization_coordsys_id FROM "
                          "gpkg_spatial_ref_sys WHERE srs_id = ?", (srs_id,)).fetchone()
    except sqlite3.Error:
        row = None
    if row and row[0] and str(row[0]).upper() != "NONE":
        return "{}:{}".format(str(row[0]).upper(), row[1])
    if _int(srs_id) > 0:
        return "EPSG:{}".format(srs_id)
    return "undefined (srs_id {})".format(srs_id)


def _norm_crs(c):
    c = str(c or "").strip().upper().replace(" ", "")
    if c.isdigit():
        c = "EPSG:" + c
    return c


def _norm_geom(g):
    g = str(g or "").strip().upper().replace(" ", "")
    for z in ("ZM", "Z", "M"):
        if g.endswith(z) and g[:-len(z)] in ("POINT", "LINESTRING", "POLYGON", "MULTIPOINT",
                                             "MULTILINESTRING", "MULTIPOLYGON"):
            g = g[:-len(z)]
            break
    return _GEOM_SAME.get(g, g)


def _layer_facts(path, table, deep):
    """dict(kind, crs, geometry, geom_col, columns {name: type}, count) of one GeoPackage
    table, or raises sqlite3.Error / LookupError."""
    con = _ro(path)
    try:
        row = con.execute("SELECT data_type, srs_id FROM gpkg_contents WHERE table_name = ?",
                          (table,)).fetchone()
        if row is None:
            names = [r[0] for r in con.execute("SELECT table_name FROM gpkg_contents")]
            raise LookupError("no table {} in the file (it has: {})".format(
                table, ", ".join(names) or "none"))
        out = {"kind": row[0], "crs": _crs_of(con, row[1]), "geometry": "", "geom_col": "",
               "columns": {}, "count": None}
        if row[0] == "features":
            g = con.execute("SELECT column_name, geometry_type_name, srs_id FROM "
                            "gpkg_geometry_columns WHERE table_name = ?", (table,)).fetchone()
            if g:
                out["geom_col"], out["geometry"] = g[0], str(g[1] or "")
                if g[2] is not None:
                    out["crs"] = _crs_of(con, g[2])
            cols = con.execute("PRAGMA table_info({})".format(_q(table))).fetchall()
            out["columns"] = {c[1]: str(c[2] or "") for c in cols
                              if c[1] != out["geom_col"] and not c[5]}
            if deep:
                n = None
                try:
                    r = con.execute("SELECT feature_count FROM gpkg_ogr_contents WHERE "
                                    "table_name = ?", (table,)).fetchone()
                    n = r[0] if r else None
                except sqlite3.Error:
                    n = None
                if n is None:
                    n = con.execute("SELECT COUNT(*) FROM {}".format(_q(table))).fetchone()[0]
                out["count"] = int(n)
        return out
    finally:
        con.close()


def _type_ok(kind, decl):
    pats = _KIND_TYPES.get(str(kind or "").lower())
    if not pats or not decl:
        return True
    d = decl.upper()
    return any(p in d for p in pats)


# ------------------------------------------------------------------ check --
_CACHE = {}
_LOCK = threading.Lock()


def _stamp(folder, m):
    out = []
    for p in [os.path.join(folder, MANIFEST)] + [
            os.path.join(folder, *_rel_file(e).split("/"))
            for sect in ("datasets", "rasters") for e in _entries(m, sect).values()
            if _rel_file(e)]:
        try:
            st = os.stat(p)
            out.append((p, st.st_size, st.st_mtime))
        except OSError:
            out.append((p, None, None))
    return tuple(out)


def clear_cache():
    with _LOCK:
        _CACHE.clear()


def failed_result(path, required, kind, text, fmt=2, folder=""):
    """1.17.6: a FAILED PackCheck: one ERROR of `kind` ("manifest" or "check") and every
    required layer missing, so a caller that needs layers blocks on it."""
    res = PackCheck(path, fmt, folder)
    res.failed = True
    res.add(ERROR, "", kind, text)
    for name in tuple(required or ()):
        res.missing_required.append(name)
        res.add(ERROR, name, "missing", "required, and the pack cannot be checked")
    return res


def check_pack(path, required=(), deep=True, aliases=None, integrity=False):
    """PackCheck for the pack at `path` (folder, a file in it, .zip or .sqlite). Never raises:
    an unreadable manifest.json or an error of the check is a FAILED result (1.17.6).
    `required`: layer names the caller needs. `aliases`: {name: other name} a layer may be
    listed under (default: hydro3dhp's NHD_* <-> 3DHP_*). `integrity`: also SQLite's
    quick_check of every GeoPackage (reads every page; off by default)."""
    try:
        required = tuple(required or ())
    except TypeError:
        required = (str(required),)
    try:
        return _check_pack(path, required, deep, aliases, integrity)
    except Exception as ex:                                      # noqa: BLE001
        return failed_result(path, required, "check", "the pack check failed: {}: {}".format(
            type(ex).__name__, ex))


def _check_pack(path, required, deep, aliases, integrity):
    fmt = detect_format(path)
    if fmt == 0:
        bad = manifest_problem(path)
        if bad:                          # 1.17.6: a pack whose manifest.json is broken
            p = os.path.abspath(os.path.expanduser(str(path)))
            folder = "" if p.lower().endswith(".zip") else (p if os.path.isdir(p) else
                                                             os.path.dirname(p))
            return failed_result(path, required, "manifest", bad, 2, folder)
    folder = pack_folder(path) if fmt == 2 else ""
    if fmt == 2 and not folder:          # a zipped format-2 pack: its zip listing (1.17.6)
        return _check_zip(path, required, aliases)
    if fmt == 1:
        return _check_v1(path, required, deep, aliases)
    if fmt != 2:
        return PackCheck(path, fmt)
    m = _read_json(os.path.join(folder, MANIFEST)) or {}
    shape = manifest_shape_problems(m)
    if shape:
        return failed_result(folder, required, "manifest", "; ".join(shape), 2, folder)
    key = (folder, required, bool(deep), bool(integrity),
           tuple(sorted((aliases or {}).items())))
    stamp = _stamp(folder, m)
    with _LOCK:
        hit = _CACHE.get(key)
        if hit and hit[0] == stamp:
            return hit[1]
    res = _check(folder, m, required, deep, aliases, integrity)
    with _LOCK:
        _CACHE[key] = (stamp, res)
    return res


def _v1_table(name):
    return "ds_" + "".join(ch if (ch.isalnum() or ch == "_") else "_" for ch in name)


def _check_v1(path, required, deep, aliases):
    """Format 1: each usable data set of the .sqlite's own datasets table has its ds_ table
    and (deep) its count. A zipped format-1 pack is checked once it is unpacked."""
    path = os.path.abspath(os.path.expanduser(str(path)))
    db = os.path.join(path, DB_NAME) if os.path.isdir(path) else path
    if not db.lower().endswith(".sqlite") and os.path.isfile(
            os.path.join(os.path.dirname(db), DB_NAME)) and not db.lower().endswith(".zip"):
        db = os.path.join(os.path.dirname(db), DB_NAME)
    res = PackCheck(path, 1, os.path.dirname(db))
    res.deep = bool(deep)
    if db.lower().endswith(".zip"):
        res.add(NOTE, "", "zip", "a zipped format-1 pack: checked once it is unpacked")
        return res
    good = set()
    try:
        con = _ro(db)
    except sqlite3.Error as ex:
        res.add(ERROR if required else WARNING, "", "unreadable",
                "{} cannot be read: {}".format(db, ex))
        res.missing_required = list(required)
        return res
    try:
        meta = dict(con.execute("SELECT key, value FROM meta").fetchall())
        res.created = str(meta.get("created") or "")[:10]
        rows = con.execute("SELECT name, count, status FROM datasets").fetchall()
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for name, count, status in rows:
            if status not in USABLE:
                res.add(NOTE, name, "status", "status '{}' (not in the pack, downloaded instead)"
                        .format(status))
                continue
            res.checked.append(name)
            t = _v1_table(name)
            if t not in tables:
                res.add(WARNING, name, "table", "table {} not in {}".format(t, DB_NAME))
                continue
            good.add(name)
            if deep and count is not None:
                n = con.execute("SELECT COUNT(*) FROM {}".format(_q(t))).fetchone()[0]
                if int(n) != int(count):
                    res.add(WARNING, name, "count", "{:,} feature(s) in the file, {:,} listed"
                            .format(int(n), int(count)))
    except (sqlite3.Error, TypeError, ValueError) as ex:
        res.add(WARNING, "", "unreadable", "{} cannot be read: {}".format(db, ex))
    finally:
        con.close()
    bad = _manifest_error(os.path.join(os.path.dirname(db), MANIFEST))
    if bad:                              # 1.17.6: the .sqlite is checked, the manifest is broken
        res.add(WARNING, "", "manifest", bad)
    m = _read_json(os.path.join(os.path.dirname(db), MANIFEST)) or {}
    for name, e in _entries(m, "datasets").items():
        if isinstance(e, dict) and e.get("status") in USABLE and name not in res.checked:
            res.add(WARNING, name, "missing", "listed in manifest.json, not in {}".format(DB_NAME))
    al = _aliases(aliases)
    for name in required:
        if name in good or al.get(name) in good:
            continue
        res.missing_required.append(name)
        res.add(ERROR, name, "missing", "required, and not in the pack")
    return res


def _aliases(aliases):
    if aliases is not None:
        return dict(aliases)
    try:
        from . import hydro3dhp as H
        return dict(H.ALIASES)
    except Exception:                                            # noqa: BLE001
        return {"NHD_flowlines": "3DHP_flowlines", "3DHP_flowlines": "NHD_flowlines",
                "NHD_waterbodies": "3DHP_waterbodies", "3DHP_waterbodies": "NHD_waterbodies"}


def _check(folder, m, required, deep, aliases, integrity=False):
    res = PackCheck(folder, 2, folder)
    res.deep = bool(deep)
    res.created = str(m.get("created") or m.get("started") or "")[:10]
    pack_crs = m.get("crs") or DEFAULT_CRS
    good = set()
    for name, e in _entries(m, "datasets").items():
        if not isinstance(e, dict):
            res.checked.append(name)
            res.add(WARNING, name, "manifest", "manifest.json entry is a JSON {}, not an object"
                    .format(type(e).__name__))
            continue
        status = e.get("status") or ""
        if not isinstance(status, str):
            status = str(status)
        if status not in USABLE:
            res.add(NOTE, name, "status", "status '{}' in manifest.json (not in the pack, "
                    "downloaded instead){}".format(status, ": " + e["note"] if e.get("note") else ""))
            continue
        res.checked.append(name)
        if status == "partial":
            res.add(NOTE, name, "status", "partial in manifest.json (part of the area is missing)")
        if _check_one(res, folder, name, e, pack_crs, deep, raster=False, integrity=integrity):
            good.add(name)
    for name, e in _entries(m, "rasters").items():
        res.checked.append(name)
        if not isinstance(e, dict):
            res.add(WARNING, name, "manifest", "manifest.json entry is a JSON {}, not an object"
                    .format(type(e).__name__))
            continue
        if _check_one(res, folder, name, e, e.get("crs") or "", deep, raster=True,
                      integrity=integrity):
            good.add(name)
    for name, why in _entries(m, "left_out").items():
        res.add(NOTE, name, "left_out", "left out of the pack: {}".format(why))
    # files on disk that the manifest does not list
    listed = {os.path.normcase(os.path.normpath(os.path.join(folder, *_rel_file(e).split("/"))))
              for sect in ("datasets", "rasters") for e in _entries(m, sect).values()
              if _rel_file(e)}
    try:
        for sub in sorted(os.listdir(folder)):
            d = os.path.join(folder, sub)
            if not os.path.isdir(d):
                continue
            for f in sorted(os.listdir(d)):
                p = os.path.join(d, f)
                if f.lower().endswith(".gpkg") and os.path.normcase(os.path.normpath(p)) not in listed:
                    res.add(NOTE, "", "extra", "{}/{} is not in manifest.json (not used)".format(sub, f))
    except OSError:
        pass
    al = _aliases(aliases)
    listed_names = set(_entries(m, "datasets")) | set(_entries(m, "rasters"))
    for name in required:
        real = name if name in listed_names else al.get(name, name)
        if real in good:
            continue
        res.missing_required.append(name)
        if real not in listed_names:
            res.add(ERROR, name, "missing", "required, and manifest.json does not list it")
        else:
            for i in res.issues:          # the layer's own problem becomes an error
                if i.layer == real and i.level == WARNING and i.kind in (
                        "missing", "table", "unreadable", "manifest"):
                    i.level = ERROR
            if not any(i.layer == real and i.level == ERROR for i in res.issues):
                res.add(ERROR, real, "missing", "required, and not usable (status {})".format(
                    (_entries(m, "datasets").get(real) or {}).get("status") or "?"
                    if isinstance(_entries(m, "datasets").get(real), dict) else "?"))
    return res


def _check_one(res, folder, name, e, want_crs, deep, raster, integrity=False):
    """Adds the issues of one data set; True when its file and table are usable."""
    if e.get("file") not in (None, "") and not _rel_file(e):
        res.add(WARNING, name, "manifest", "manifest.json 'file' is a JSON {}, not a path"
                .format(type(e.get("file")).__name__))
        return False
    rel = _rel_file(e) or ""
    if not rel:
        res.add(WARNING, name, "missing", "manifest.json gives no file")
        return False
    path = os.path.join(folder, *rel.split("/"))
    if not os.path.isfile(path):
        res.add(WARNING, name, "missing", "file {} not found".format(rel))
        return False
    bad = sqlite_file_problem(path, integrity=integrity)
    if bad:
        res.add(WARNING, name, "unreadable", "{} cannot be read: {}".format(rel, bad))
        return False
    table = e.get("layer") or name
    if not isinstance(table, str):
        table = str(table)
    try:
        facts = _layer_facts(path, table, deep and not raster)
    except LookupError as ex:
        res.add(WARNING, name, "table", "{}: {}".format(rel, ex))
        return False
    except sqlite3.Error as ex:
        res.add(WARNING, name, "unreadable", "{} cannot be read: {}".format(rel, ex))
        return False
    if raster:
        if facts["kind"] not in ("tiles", "2d-gridded-coverage"):
            res.add(WARNING, name, "geometry", "{} is '{}', not a raster".format(rel, facts["kind"]))
    else:
        if facts["kind"] != "features":
            res.add(WARNING, name, "geometry", "{} is '{}', not a vector layer".format(
                rel, facts["kind"]))
            return True
        want = _norm_geom(e.get("geometry"))
        have = _norm_geom(facts["geometry"])
        if want and have and have != "GEOMETRY" and want != have:
            res.add(WARNING, name, "geometry", "geometry {} in the file, {} in manifest.json"
                    .format(facts["geometry"], e.get("geometry")))
        cols = facts["columns"]
        fields = [f for f in (e.get("fields") or []) if isinstance(f, (list, tuple)) and f]
        if fields:
            want_cols = [str(f[0]) for f in fields]
            low = {c.lower(): c for c in cols}
            miss = [c for c in want_cols if c.lower() not in low]
            extra = [c for c in cols if c.lower() not in {w.lower() for w in want_cols}
                     and c.lower() != "fid"]
            badtype = []
            for f in fields:
                c = low.get(str(f[0]).lower())
                if c and len(f) > 2 and not _type_ok(f[2], cols[c]):
                    badtype.append("{} is {} (manifest: {})".format(c, cols[c] or "untyped", f[2]))
            if miss or extra or badtype:
                bits = []
                if miss:
                    bits.append("missing field(s) " + ", ".join(miss))
                if extra:
                    bits.append("field(s) not in manifest.json " + ", ".join(extra))
                if badtype:
                    bits.append("type(s) differ: " + "; ".join(badtype))
                res.add(WARNING, name, "fields", "; ".join(bits))
        if deep and e.get("count") is not None and facts["count"] is not None:
            try:
                want_n = int(e.get("count"))
            except (TypeError, ValueError):
                want_n = None
            if want_n is not None and want_n != facts["count"]:
                res.add(WARNING, name, "count", "{:,} feature(s) in the file, {:,} in manifest.json"
                        .format(facts["count"], want_n))
    if want_crs and facts["crs"] and _norm_crs(facts["crs"]) != _norm_crs(want_crs):
        res.add(WARNING, name, "crs", "CRS {} in the file, {} expected".format(facts["crs"],
                                                                           want_crs))
    return True


_SQLITE_MAGIC = b"SQLite format 3\x00"


def sqlite_file_problem(path, integrity=False):
    """1.17.6: why a GeoPackage file is not a whole SQLite file, or "". Cheap by default:
    the 100-byte header (magic, page size) and the file size against the page count the
    header gives (a copy cut short - a half-written GPKG - is shorter). A file with a -wal
    beside it is not size-checked (its pages may still be in the WAL). integrity=True also
    runs PRAGMA quick_check (reads every page). Never raises."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            head = fh.read(100)
    except OSError as ex:
        return str(ex)
    if size == 0:
        return "the file is empty"
    if len(head) < 100 or not head.startswith(_SQLITE_MAGIC):
        return "not an SQLite / GeoPackage file (bad header)"
    page = int.from_bytes(head[16:18], "big")
    page = 65536 if page == 1 else page
    if page < 512 or page & (page - 1):
        return "bad SQLite page size {} in the header".format(page)
    change = int.from_bytes(head[24:28], "big")
    pages = int.from_bytes(head[28:32], "big")
    valid_for = int.from_bytes(head[92:96], "big")
    if (pages and valid_for == change and not os.path.exists(path + "-wal")
            and size < pages * page):
        return ("the file is cut short: {:,} bytes, the header says {:,} pages of {:,} "
                "({:,} bytes) - a half-written copy?".format(size, pages, page, pages * page))
    if integrity:
        try:
            con = _ro(path)
            try:
                row = con.execute("PRAGMA quick_check(1)").fetchone()
            finally:
                con.close()
            if not row or str(row[0]).lower() != "ok":
                return "SQLite quick_check: {}".format(row[0] if row else "no answer")
        except sqlite3.Error as ex:
            return "SQLite quick_check failed: {}".format(ex)
    return ""


def _check_zip(path, required, aliases):
    """1.17.6: a zipped format-2 pack against its zip listing: every listed file is in the
    zip and not empty, every required layer listed. The layers themselves (tables, counts,
    fields, CRS) are checked on the unpacked folder (Hazard checks the folder it reads)."""
    res = PackCheck(path, 2)
    try:
        with zipfile.ZipFile(path) as z:
            infos = {i.filename: i for i in z.infolist()}
            name = min((n for n in infos if n.rsplit("/", 1)[-1] == MANIFEST),
                       key=lambda n: n.count("/"))
            raw = z.read(name)
    except (OSError, KeyError, ValueError, zipfile.BadZipFile) as ex:
        return failed_result(path, required, "manifest", "the zip cannot be read: {}".format(ex))
    # plain UTF-8, as the readers decode it: a BOM is named as for a folder
    bad = _manifest_bytes_error(raw)
    if bad:
        return failed_result(path, required, "manifest", bad)
    m = json.loads(raw.decode("utf-8"))
    shape = manifest_shape_problems(m)
    if shape:
        return failed_result(path, required, "manifest", "; ".join(shape))
    base = name[:-len(MANIFEST)]
    res.created = str(m.get("created") or m.get("started") or "")[:10]
    res.deep = False
    good = set()
    for sect in ("datasets", "rasters"):
        for lname, e in _entries(m, sect).items():
            if not isinstance(e, dict):
                res.checked.append(lname)
                res.add(WARNING, lname, "manifest", "manifest.json entry is not an object")
                continue
            if sect == "datasets" and (e.get("status") or "") not in USABLE:
                res.add(NOTE, lname, "status", "status '{}' in manifest.json".format(
                    e.get("status")))
                continue
            res.checked.append(lname)
            rel = _rel_file(e)
            if not rel:
                res.add(WARNING, lname, "manifest" if e.get("file") else "missing",
                        "manifest.json gives no usable file")
                continue
            info = infos.get(base + rel)
            if info is None:
                res.add(WARNING, lname, "missing", "file {} not in the zip".format(rel))
            elif info.file_size == 0:
                res.add(WARNING, lname, "unreadable", "file {} is empty in the zip".format(rel))
            else:
                good.add(lname)
    res.add(NOTE, "", "zip", "a zipped format-2 pack: files checked against the zip listing; "
            "tables, counts, fields and CRS are checked once it is unpacked")
    al = _aliases(aliases)
    listed = set(_entries(m, "datasets")) | set(_entries(m, "rasters"))
    for rname in required:
        real = rname if rname in listed else al.get(rname, rname)
        if real in good:
            continue
        res.missing_required.append(rname)
        hit = [i for i in res.issues if i.layer == real and i.level == WARNING]
        for i in hit:
            i.level = ERROR
        if not hit:
            res.add(ERROR, rname, "missing", "required, and not usable in the zip")
    return res


def pack_signature(path):
    """1.17.6: [[relative file, size, mtime_ns], ...] of the pack's manifest.json and every
    file it lists (format 2), or of the file / zip itself; for run signatures, so a layer
    file replaced in place is a new run. Sizes and times, not hashes (a hash would read
    gigabytes at every signature). Never raises."""
    out = []
    try:
        p = os.path.abspath(os.path.expanduser(str(path)))
        folder = pack_folder(p) if not p.lower().endswith(".zip") else ""
        files = [p]
        if folder:
            m = _read_json(os.path.join(folder, MANIFEST)) or {}
            files = [os.path.join(folder, MANIFEST)] + [
                os.path.join(folder, *_rel_file(e).split("/"))
                for sect in ("datasets", "rasters") for e in _entries(m, sect).values()
                if _rel_file(e)]
        for f in files:
            rel = os.path.relpath(f, folder) if folder else os.path.basename(f)
            try:
                st = os.stat(f)
                out.append([rel.replace(os.sep, "/"), int(st.st_size),
                            int(getattr(st, "st_mtime_ns", 0))])
            except OSError:
                out.append([rel.replace(os.sep, "/"), None, None])
    except Exception as ex:                                      # noqa: BLE001
        out.append(["error", repr(ex)])
    return out


def log_lines(res, log, warn=None, notes=False):
    """Write res.lines() to `log` (and the error / warning lines to `warn` too when given).
    Never raises."""
    try:
        for line in res.lines(notes=notes):
            log(line)
        if warn is not None and not res.ok:
            warn(res.summary())
    except Exception:                                            # noqa: BLE001
        pass
