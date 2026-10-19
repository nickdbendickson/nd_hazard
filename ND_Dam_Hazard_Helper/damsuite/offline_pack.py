# -*- coding: utf-8 -*-
"""v2.6.1: offline data pack - every downloadable data set for a whole
area (North Dakota) in one SQLite file, so the plugin runs without
internet access. Pure Python (sqlite3, zlib, json): the builder
(build_nd_pack.py) runs outside QGIS, the plugin reads it in download.py.

Pack = a folder or a .zip holding
    manifest.json            what is in it, when each data set was fetched
    nd_offline_pack.sqlite   one table per data set

1.10.0 (suite): pack format 2 is a folder with one GeoPackage per data set
(gpkgpack: ND_Dam_Suite_Pack/<theme>/<name>.gpkg, manifest.json, LAYERS.txt).
open_pack() reads both formats; the format-2 reader answers the same queries.

Each data set is stored under the plugin's own download name
(NSI_structures, USA_Structures, NID_dams, lifeline_nd_hospitals ...), one
row per feature: its bounding box and the GeoJSON feature exactly as the
service returned it (zlib-compressed). A study-area query returns the
features whose geometry intersects the rectangle - what the services'
envelope / bbox queries return.

v2.9.0: spatial index. Besides the longitude B-tree (index ds_<name>_x on
minx, kept: older plugins and packs use it), every data set gets an SQLite
R*Tree when it is finished (rt_<name>: id = fid, minx, maxx, miny, maxy).
A query then asks the R*Tree for the candidates and applies the same test
on the stored box as before, so it returns exactly the same features in the
same order (fid) - only faster. The R*Tree is used only when it is known to
belong to the data set's current table: a guard trigger rtok_<name> on the
table (it disappears when an older builder drops and replaces the table)
and meta "rtree:<name>" = the table's row count. Without the rtree module
in Python's sqlite3, or for a pack built before 2.9, the B-tree query of
2.8 is used. PACK_FORMAT_VERSION stays 1: 2.8 plugins read these packs (the
extra tables are ignored). An existing pack is not changed when it is
opened (it is read-only by design); build_spatial_index() adds the R*Trees
to a pack's work file, and the builder does so when it writes the zip.
"""

import json
import os
import pathlib
import shutil
import sqlite3
import threading
import time
import zipfile
import zlib

PACK_FORMAT = "nd-dam-hazard-offline-pack"
PACK_FORMAT_VERSION = 1      # v2.8.0: unchanged - the resume tables below are work-file
                             # only (a finished pack has none of their rows)
DB_NAME = "nd_offline_pack.sqlite"
MANIFEST_NAME = "manifest.json"

# North Dakota (lon / lat, WGS 84) - the pack covers this box plus a margin
ND_BBOX = (-104.0489, 45.9351, -96.5543, 49.0000)
DEFAULT_PAD_DEG = 0.1


class PackError(Exception):
    pass


def _table(name):
    safe = "".join(ch if (ch.isalnum() or ch == "_") else "_" for ch in name)
    return "ds_" + safe


# ------------------------------------------------- v2.9.0: R*Tree spatial index --
RTREE_META = "rtree:"        # meta key prefix: rows of the table when it was indexed
_RTREE_OK = [None]           # does this Python's sqlite3 have the rtree module?


def _rtree(name):
    """R*Tree virtual table of data set `name` (never ends in __new, never ds_...)."""
    return "rt_" + _table(name)[3:]


def _guard(name):
    """Trigger on the data set's table that says "the R*Tree is this table's"."""
    return "rtok_" + _table(name)[3:]


def rtree_available():
    """True when Python's sqlite3 can create / read R*Tree tables (probed once)."""
    if _RTREE_OK[0] is None:
        con = None
        try:
            con = sqlite3.connect(":memory:")
            con.execute("CREATE VIRTUAL TABLE temp.probe USING rtree(id, a, b)")
            _RTREE_OK[0] = True
        except sqlite3.Error:
            _RTREE_OK[0] = False
        finally:
            if con is not None:
                con.close()
    return _RTREE_OK[0]


def _drop_index(con, name):
    """Forget the R*Tree of data set `name` (guard, meta record, table). The
    table itself cannot be dropped without the rtree module: it then stays,
    unused (no guard)."""
    con.execute('DROP TRIGGER IF EXISTS "{}"'.format(_guard(name)))
    con.execute("DELETE FROM meta WHERE key=?", (RTREE_META + name,))
    try:
        con.execute('DROP TABLE IF EXISTS "{}"'.format(_rtree(name)))
    except sqlite3.Error:
        pass


def _index_valid(con, name, count=None):
    """True when data set `name` has an R*Tree that belongs to its current
    table (guard trigger present, row count as recorded)."""
    t = _table(name)
    rows = dict((r[0], (r[1], r[2])) for r in con.execute(
        "SELECT name, type, tbl_name FROM sqlite_master WHERE name IN (?, ?)",
        (_rtree(name), _guard(name))))
    if rows.get(_rtree(name), ("",))[0] != "table" or rows.get(_guard(name)) != ("trigger", t):
        return False
    row = con.execute("SELECT value FROM meta WHERE key=?", (RTREE_META + name,)).fetchone()
    if row is None:
        return False
    if count is None:
        count = con.execute('SELECT COUNT(*) FROM "{}"'.format(t)).fetchone()[0]
    return str(row[0]) == str(count)


def _build_index(con, name):
    """(Re)build the R*Tree of data set `name` from its table and commit.
    True when built; False (nothing left behind that a reader would use)
    without the rtree module or on any SQLite error."""
    if not rtree_available():
        return False
    t, rt = _table(name), _rtree(name)
    try:
        _drop_index(con, name)
        con.execute('CREATE VIRTUAL TABLE "{}" USING rtree(id, minx, maxx, miny, maxy)'.format(rt))
        # the R*Tree keeps 32-bit floats rounded outwards: its boxes are never
        # smaller than the stored ones, so it only pre-selects - the query tests
        # the stored (double) box afterwards. MIN / MAX: a box written the wrong
        # way round would otherwise fail the insert
        con.execute('INSERT INTO "{}" (id, minx, maxx, miny, maxy) SELECT fid, MIN(minx, maxx), '
                    'MAX(minx, maxx), MIN(miny, maxy), MAX(miny, maxy) FROM "{}" WHERE minx IS NOT '
                    'NULL AND maxx IS NOT NULL AND miny IS NOT NULL AND maxy IS NOT NULL'.format(
                        rt, t))
        n = con.execute('SELECT COUNT(*) FROM "{}"'.format(t)).fetchone()[0]
        con.execute('CREATE TRIGGER "{}" AFTER DELETE ON "{}" WHEN 0 BEGIN SELECT 1; END'.format(
            _guard(name), t))
        con.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (RTREE_META + name, str(n)))
        con.commit()
        return True
    except sqlite3.Error:
        try:
            con.rollback()
            _drop_index(con, name)
            con.commit()
        except sqlite3.Error:
            pass
        return False


def _index_all(con, log=None):
    """R*Tree for every usable data set that has none (or a stale one); R*Trees
    of data sets no longer in the file are dropped. Returns {name: "kept" |
    "built" | "not available"}."""
    log = log or (lambda m: None)
    out = {}
    names = [r[0] for r in con.execute("SELECT name FROM datasets WHERE status IN (?, ?)",
                                       USABLE_STATUS)]
    keep = set()
    for name in names:
        if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                           (_table(name),)).fetchone():
            continue
        keep.add(_rtree(name))
        if rtree_available() and _index_valid(con, name):
            out[name] = "kept"
            continue
        t0 = time.time()
        ok = _build_index(con, name)
        out[name] = "built" if ok else "not available"
        if ok:
            log("  spatial index (R*Tree) of {}: {:.1f} s".format(name, time.time() - t0))
    if rtree_available():
        for (t,) in con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE "
                                "'rt\\_%' ESCAPE '\\' AND sql LIKE 'CREATE VIRTUAL TABLE%'"
                                ).fetchall():
            if t not in keep:
                try:
                    con.execute('DROP TABLE IF EXISTS "{}"'.format(t))
                    con.commit()
                except sqlite3.Error:
                    pass
    return out


def build_spatial_index(db_path, log=None):
    """Add the R*Tree index to every data set of a pack database that has
    none (a pack or work file of a plugin before 2.9, or one built where
    sqlite3 had no rtree module). Changes the file in place: use it on a
    work file or a pack folder you own, not on a pack other computers read
    at the same time. Returns {name: "kept" | "built" | "not available"}."""
    if not os.path.isfile(db_path):
        raise PackError("offline data pack not found: {}".format(db_path))
    con = sqlite3.connect(db_path)
    try:
        meta = dict(con.execute("SELECT key, value FROM meta").fetchall())
        if meta.get("format") != PACK_FORMAT:
            raise PackError("{} is not an offline data pack".format(db_path))
        return _index_all(con, log)
    except sqlite3.DatabaseError as e:
        raise PackError("the offline data pack could not be indexed: {}".format(e))
    finally:
        con.close()


# ---------------------------------------------------------------- geometry --
def _coords_iter(geom):
    """Every [x, y] of a GeoJSON geometry."""
    if not isinstance(geom, dict):
        return
    t = geom.get("type")
    c = geom.get("coordinates")
    if t == "GeometryCollection":
        for g in geom.get("geometries") or []:
            for p in _coords_iter(g):
                yield p
        return
    if c is None:
        return
    depth = {"Point": 0, "MultiPoint": 1, "LineString": 1, "MultiLineString": 2,
             "Polygon": 2, "MultiPolygon": 3}.get(t)
    if depth is None:
        return

    def walk(a, d):
        if d == 0:
            if isinstance(a, (list, tuple)) and len(a) >= 2 and a[0] is not None \
                    and a[1] is not None:
                yield a
            return
        for b in a or []:
            for p in walk(b, d - 1):
                yield p
    for p in walk(c, depth):
        yield p


def geom_bbox(geom):
    """(xmin, ymin, xmax, ymax) of a GeoJSON geometry, or None when empty."""
    xs, ys = [], []
    for p in _coords_iter(geom):
        try:
            xs.append(float(p[0]))
            ys.append(float(p[1]))
        except (TypeError, ValueError):
            continue
    if not xs:
        return None
    return min(xs), min(ys), max(xs), max(ys)


def _in_rect(x, y, r):
    return r[0] <= x <= r[2] and r[1] <= y <= r[3]


def _seg_hits_rect(x0, y0, x1, y1, r):
    """Liang-Barsky: does the segment touch the rectangle?"""
    dx, dy = x1 - x0, y1 - y0
    t0, t1 = 0.0, 1.0
    for p, q in ((-dx, x0 - r[0]), (dx, r[2] - x0), (-dy, y0 - r[1]), (dy, r[3] - y0)):
        if p == 0:
            if q < 0:
                return False
            continue
        t = q / p
        if p < 0:
            if t > t1:
                return False
            if t > t0:
                t0 = t
        else:
            if t < t0:
                return False
            if t < t1:
                t1 = t
    return True


def _point_in_ring(x, y, ring):
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-300) + xi:
            inside = not inside
        j = i
    return inside


def _lines_of(geom):
    """Point lists of every line / ring, and the polygons (list of rings)."""
    t = geom.get("type")
    c = geom.get("coordinates") or []
    if t == "LineString":
        return [c], []
    if t == "MultiLineString":
        return list(c), []
    if t == "Polygon":
        return list(c), [c]
    if t == "MultiPolygon":
        rings = [r for poly in c for r in poly]
        return rings, list(c)
    if t == "GeometryCollection":
        lines, polys = [], []
        for g in geom.get("geometries") or []:
            a, b = _lines_of(g)
            lines += a
            polys += b
        return lines, polys
    return [], []


def intersects_rect(geom, rect):
    """True when the GeoJSON geometry touches the rectangle (xmin, ymin,
    xmax, ymax) - the esriSpatialRelIntersects / bbox test of the services."""
    if not isinstance(geom, dict):
        return False
    bb = geom_bbox(geom)
    if bb is None:
        return False
    if bb[2] < rect[0] or bb[0] > rect[2] or bb[3] < rect[1] or bb[1] > rect[3]:
        return False
    for p in _coords_iter(geom):
        if _in_rect(p[0], p[1], rect):
            return True
    lines, polys = _lines_of(geom)
    for ln in lines:
        for a, b in zip(ln[:-1], ln[1:]):
            if _seg_hits_rect(a[0], a[1], b[0], b[1], rect):
                return True
    # no vertex inside and no edge through it: the rectangle is either wholly
    # inside a polygon or outside all of them
    cx, cy = (rect[0] + rect[2]) / 2.0, (rect[1] + rect[3]) / 2.0
    for rings in polys:
        if rings and _point_in_ring(cx, cy, rings[0]) and \
                not any(_point_in_ring(cx, cy, h) for h in rings[1:]):
            return True
    return False


def covers(outer, inner):
    return (outer[0] <= inner[0] and outer[1] <= inner[1] and
            outer[2] >= inner[2] and outer[3] >= inner[3])


def boxes_touch(a, b):
    return not (a[2] < b[0] or a[0] > b[2] or a[3] < b[1] or a[1] > b[3])


# v2.6.2: status of a data set in the pack
#   complete  every feature of the pack area
#   partial   some features could not be read and it is not known where they
#             are: used only when the service cannot be (pack as fallback,
#             offline only), always with a warning
#   gaps: areas (boxes) that could not be read - a study area touching one
#   is not covered by the pack
STATUS_COMPLETE, STATUS_PARTIAL = "complete", "partial"
USABLE_STATUS = (STATUS_COMPLETE, STATUS_PARTIAL)


# ------------------------------------------------------------------ writer --
class PackWriter(object):
    """Builds the pack database. A data set is written with begin() -
    add() ... - finish(); an interrupted build keeps the finished ones
    (resume).

    v2.8.0: chunk-level resume. A data set is fetched in chunks (a batch of
    object ids, an NSI / Overpass tile, a page). add(..., chunk=key) stores
    the chunk's rows and the note "chunk key done" in ONE transaction, so an
    interrupted build (Cancel, crash, failed data set) leaves the staging
    table with exactly the chunks that were recorded. begin(..., resume=True)
    picks that staging table up again when it was started for the same
    address and settings; the caller asks chunks() which keys are done and
    fetches the rest. Tables `stages` and `chunks` are new; a work file or
    pack from an older version simply has none (created empty on open), and
    older versions ignore them."""

    def __init__(self, path, bbox, area="North Dakota", created_by=""):
        self.path = path
        self.con = sqlite3.connect(path)
        self.con.execute("PRAGMA journal_mode=WAL")
        self.con.execute("PRAGMA synchronous=NORMAL")
        self.con.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
        self.con.execute(
            "CREATE TABLE IF NOT EXISTS datasets (name TEXT PRIMARY KEY, label TEXT, "
            "url TEXT, fetched TEXT, count INTEGER, status TEXT, note TEXT, "
            "maxw REAL, maxh REAL, geometry TEXT, gaps TEXT)")
        # v2.8.0: resume bookkeeping of the data set being fetched (work file only)
        self.con.execute("CREATE TABLE IF NOT EXISTS stages (name TEXT PRIMARY KEY, url TEXT, "
                         "sig TEXT, started TEXT, types TEXT)")
        self.con.execute("CREATE TABLE IF NOT EXISTS chunks (name TEXT, ckey TEXT, n INTEGER, "
                         "fid_lo INTEGER, fid_hi INTEGER, payload BLOB, "
                         "PRIMARY KEY (name, ckey))")
        cols = [r[1] for r in self.con.execute("PRAGMA table_info(datasets)")]
        if "gaps" not in cols:               # a work file of a 2.6.1 build (resume)
            self.con.execute("ALTER TABLE datasets ADD COLUMN gaps TEXT")
        old = dict(self.con.execute("SELECT key, value FROM meta").fetchall())
        if old.get("bbox") and json.loads(old["bbox"]) != list(bbox):
            raise PackError("{} was started for another area {} - delete it or use another "
                            "output name".format(path, old["bbox"]))
        self._meta({"format": PACK_FORMAT, "format_version": str(PACK_FORMAT_VERSION),
                    "area": area, "bbox": json.dumps(list(bbox))})
        if created_by:
            self._meta({"created_by": created_by})
        if "started" not in old:
            self._meta({"started": time.strftime("%Y-%m-%dT%H:%M:%S")})
        self.con.commit()
        self._cur = None

    def _meta(self, d):
        for k, v in d.items():
            self.con.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (k, str(v)))

    def status(self, name):
        row = self.con.execute("SELECT status FROM datasets WHERE name=?", (name,)).fetchone()
        return row[0] if row else None

    def gaps(self, name):
        """The areas of data set `name` that could not be read ([] = none)."""
        row = self.con.execute("SELECT gaps FROM datasets WHERE name=?", (name,)).fetchone()
        try:
            return json.loads(row[0]) if row and row[0] else []
        except ValueError:
            return []

    def done(self, name):
        """Complete, nothing missing: a resumed build skips it."""
        return self.status(name) == STATUS_COMPLETE and not self.gaps(name)

    def begin(self, name, label, url, resume=False, signature=""):
        """Start data set `name`. resume=True: a staging table an earlier,
        interrupted run left with recorded chunks is kept and continued when it
        was started for the same address and `signature` (settings that change
        what the chunks hold); otherwise it is dropped and started empty (the
        v2.6.2 behaviour, and what --refresh does). Returns the number of
        chunks kept (0 = fresh start)."""
        # v2.6.2: fetched into a staging table and swapped in by finish(); a
        # failed or interrupted re-fetch keeps the data set there was
        t = _table(name)
        stage = t + "__new"
        kept = 0
        if resume and (self._table_exists(stage) or self._table_exists(t)):
            row = self.con.execute("SELECT url, sig FROM stages WHERE name=?",
                                   (name,)).fetchone()
            if row and row[0] == url and (row[1] or "") == (signature or ""):
                kept = self.con.execute("SELECT COUNT(*) FROM chunks WHERE name=?",
                                        (name,)).fetchone()[0]
            if kept and not self._table_exists(stage):
                # finished incomplete (partial / gaps) last time: its rows are
                # copied to a new staging table, the finished data set stays
                # untouched until finish() (or a failure keeps it)
                self._create_stage(stage)
                self.con.execute(
                    "INSERT INTO {} (fid, uid, minx, miny, maxx, maxy, feat) SELECT fid, uid, "
                    "minx, miny, maxx, maxy, feat FROM {}".format(stage, t))
                self.con.commit()
        n, maxw, maxh, types = 0, 0.0, 0.0, set()
        if kept:
            n, maxw, maxh = self.con.execute(
                "SELECT COUNT(*), MAX(maxx - minx), MAX(maxy - miny) FROM {}".format(stage)
            ).fetchone()
            maxw, maxh = float(maxw or 0.0), float(maxh or 0.0)
            trow = self.con.execute("SELECT types FROM stages WHERE name=?", (name,)).fetchone()
            types = set(x for x in ((trow[0] if trow else "") or "").split(",") if x)
        else:
            self._reset_stage(name, url, signature)
        self._cur = {"name": name, "label": label, "url": url, "table": stage, "final": t,
                     "n": n, "maxw": maxw, "maxh": maxh, "types": types, "resumed": kept}
        return kept

    def _table_exists(self, table):
        return self.con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                                (table,)).fetchone() is not None

    def _reset_stage(self, name, url, signature):
        stage = _table(name) + "__new"
        self._create_stage(stage)
        self.con.execute("DELETE FROM chunks WHERE name=?", (name,))
        self.con.execute("INSERT OR REPLACE INTO stages (name, url, sig, started, types) "
                         "VALUES (?, ?, ?, ?, '')", (name, url, signature or "",
                                                     time.strftime("%Y-%m-%dT%H:%M:%S")))
        self.con.commit()

    def _create_stage(self, stage):
        self.con.execute("DROP TABLE IF EXISTS {}".format(stage))
        self.con.execute("CREATE TABLE {} (fid INTEGER PRIMARY KEY, uid TEXT UNIQUE, "
                         "minx REAL, miny REAL, maxx REAL, maxy REAL, feat BLOB)".format(stage))

    def has_chunks(self):
        """True when the file holds resume records (of interrupted, failed or
        incomplete data sets)."""
        return self.con.execute("SELECT 1 FROM chunks LIMIT 1").fetchone() is not None

    def restart_stage(self):
        """Forget every stored chunk and row of the data set being fetched."""
        cur = self._cur
        self._reset_stage(cur["name"], cur["url"], self._stage_sig(cur["name"]))
        cur.update(n=0, maxw=0.0, maxh=0.0, types=set(), resumed=0)

    def _stage_sig(self, name):
        row = self.con.execute("SELECT sig FROM stages WHERE name=?", (name,)).fetchone()
        return row[0] if row else ""

    def chunks(self, name=None):
        """{key: payload} of the chunks stored for data set `name` (default:
        the one being fetched); payload: bytes given to add(), or None."""
        name = name or self._cur["name"]
        return dict((k, p) for k, p in self.con.execute(
            "SELECT ckey, payload FROM chunks WHERE name=?", (name,)))

    def drop_chunks(self, keys):
        """Remove chunks (their rows and their record) - they are fetched again.
        Returns the number of rows removed."""
        cur = self._cur
        removed = 0
        for k in keys:
            row = self.con.execute("SELECT fid_lo, fid_hi FROM chunks WHERE name=? AND ckey=?",
                                   (cur["name"], k)).fetchone()
            if row is None:
                continue
            c = self.con.execute("DELETE FROM {} WHERE fid > ? AND fid <= ?".format(
                cur["table"]), (row[0], row[1]))
            removed += max(0, c.rowcount)
            self.con.execute("DELETE FROM chunks WHERE name=? AND ckey=?", (cur["name"], k))
        cur["n"] = self.con.execute("SELECT COUNT(*) FROM {}".format(cur["table"])).fetchone()[0]
        self.con.commit()
        return removed

    def stage_leftovers(self):
        """Names of staging tables (data sets interrupted or failed) in this file."""
        return [r[0] for r in self.con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'ds\\_%\\_\\_new' "
            "ESCAPE '\\'")]

    def add(self, features, uid_of, chunk=None, payload=None):
        """Insert GeoJSON features; uid_of(feature) -> a unique key (a feature
        met twice - tile edges, pages - is kept once). Returns the number of
        new rows. chunk: the key of the chunk these features complete; it is
        recorded in the same transaction as the rows (with `payload`, bytes
        the caller wants back from chunks()), so after an interruption a
        chunk is either wholly stored and recorded, or neither."""
        cur = self._cur
        types_before = len(cur["types"])
        rows = []
        for f in features:
            g = f.get("geometry") if isinstance(f, dict) else None
            bb = geom_bbox(g) if g else None
            if bb is None:
                continue                  # no geometry: a query could never return it
            uid = uid_of(f)
            if uid is None:
                uid = json.dumps(g, sort_keys=True)[:400]
            cur["maxw"] = max(cur["maxw"], bb[2] - bb[0])
            cur["maxh"] = max(cur["maxh"], bb[3] - bb[1])
            cur["types"].add(g.get("type"))
            blob = zlib.compress(json.dumps(f, separators=(",", ":")).encode("utf-8"), 6)
            rows.append((str(uid), bb[0], bb[1], bb[2], bb[3], blob))
        lo = 0
        if chunk is not None:
            lo = self.con.execute("SELECT MAX(fid) FROM {}".format(cur["table"])).fetchone()[0] or 0
        before = self.con.total_changes
        self.con.executemany("INSERT OR IGNORE INTO {} (uid, minx, miny, maxx, maxy, feat) "
                             "VALUES (?, ?, ?, ?, ?, ?)".format(cur["table"]), rows)
        added = self.con.total_changes - before
        cur["n"] += added
        if chunk is not None:
            hi = lo
            if added:
                hi = self.con.execute("SELECT MAX(fid) FROM {}".format(
                    cur["table"])).fetchone()[0] or lo
            self.con.execute("INSERT OR REPLACE INTO chunks (name, ckey, n, fid_lo, fid_hi, "
                             "payload) VALUES (?, ?, ?, ?, ?, ?)",
                             (cur["name"], str(chunk), added, lo, hi, payload))
        if len(cur["types"]) != types_before:
            self.con.execute("UPDATE stages SET types=? WHERE name=?", (
                ",".join(sorted(t for t in cur["types"] if t)), cur["name"]))
        self.con.commit()
        return added

    def finish(self, note="", status=STATUS_COMPLETE, gaps=None):
        """gaps: [(xmin, ymin, xmax, ymax)] areas that could not be read."""
        cur = self._cur
        n = self.con.execute("SELECT COUNT(*) FROM {}".format(cur["table"])).fetchone()[0]
        _drop_index(self.con, cur["name"])        # v2.9.0: the old table's R*Tree
        self.con.commit()
        self.con.execute("DROP TABLE IF EXISTS {}".format(cur["final"]))
        self.con.execute("ALTER TABLE {} RENAME TO {}".format(cur["table"], cur["final"]))
        self.con.execute("CREATE INDEX IF NOT EXISTS {0}_x ON {0} (minx)".format(cur["final"]))
        self.con.execute("INSERT OR REPLACE INTO datasets (name, label, url, fetched, count, "
                         "status, note, maxw, maxh, geometry, gaps) VALUES "
                         "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                         (cur["name"], cur["label"], cur["url"], time.strftime("%Y-%m-%d"), n,
                          status, note, cur["maxw"], cur["maxh"],
                          ",".join(sorted(t for t in cur["types"] if t)),
                          json.dumps([list(g) for g in gaps]) if gaps else None))
        if status == STATUS_COMPLETE and not gaps:
            self.con.execute("DELETE FROM chunks WHERE name=?", (cur["name"],))
            self.con.execute("DELETE FROM stages WHERE name=?", (cur["name"],))
        # (incomplete - partial, or gaps: the chunk records stay, so the next run reads
        # only the chunks that are missing, see begin(resume=True))
        self.con.commit()
        # v2.9.0: the R*Tree of the new table (none without the rtree module:
        # the pack is still complete, queries use the longitude index)
        _build_index(self.con, cur["name"])
        self._cur = None
        return n

    def fail(self, name, note):
        """The fetch of `name` failed. A usable data set already in the pack
        (complete or partial, from an earlier run) is kept, with the failure
        noted; otherwise the data set is recorded as failed. v2.8.0: the
        chunks already stored stay in the staging table (begin(resume=True)
        continues with them); a staging table without recorded chunks is
        dropped as before."""
        if not self.con.execute("SELECT 1 FROM chunks WHERE name=? LIMIT 1",
                                (name,)).fetchone():
            self.con.execute("DROP TABLE IF EXISTS {}".format(_table(name) + "__new"))
            self.con.execute("DELETE FROM stages WHERE name=?", (name,))
        prev = self.status(name)
        if prev in USABLE_STATUS:
            old = self.con.execute("SELECT note FROM datasets WHERE name=?",
                                   (name,)).fetchone()[0] or ""
            self.con.execute("UPDATE datasets SET note=? WHERE name=?", (
                (old + "; " if old else "") + "update of {} failed ({}) - the earlier "
                "download is kept".format(time.strftime("%Y-%m-%d"), note), name))
        else:
            self.con.execute("INSERT OR REPLACE INTO datasets (name, status, note, count) "
                             "VALUES (?, 'failed', ?, 0)", (name, note))
            _drop_index(self.con, name)
            self.con.execute("DROP TABLE IF EXISTS {}".format(_table(name)))
        self.con.commit()
        self._cur = None

    def manifest(self):
        meta = dict(self.con.execute("SELECT key, value FROM meta").fetchall())
        ds = {}
        for row in self.con.execute("SELECT name, label, url, fetched, count, status, note, "
                                    "geometry, gaps FROM datasets ORDER BY name"):
            ds[row[0]] = {"label": row[1], "url": row[2], "fetched": row[3], "count": row[4],
                          "status": row[5], "note": row[6] or "", "geometry": row[7]}
            if row[8]:
                ds[row[0]]["gaps"] = json.loads(row[8])
        return {"format": PACK_FORMAT, "format_version": PACK_FORMAT_VERSION,
                "area": meta.get("area"), "bbox": json.loads(meta.get("bbox") or "null"),
                "started": meta.get("started"), "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "created_by": meta.get("created_by"), "datasets": ds}

    def index_all(self, log=None):
        """v2.9.0: R*Tree for every usable data set without a valid one (data
        sets finished by an older builder in a resumed work file). Returns
        {name: "kept" | "built" | "not available"}."""
        return _index_all(self.con, log)

    def close(self, finalize=False):
        if finalize:
            try:
                self.index_all()
            except sqlite3.Error:
                pass                     # the pack works without it (longitude index)
            self._meta({"created": time.strftime("%Y-%m-%dT%H:%M:%S")})
            self.con.commit()
            self.con.execute("PRAGMA journal_mode=DELETE")    # one file, no -wal beside it
            self.con.execute("VACUUM")
        self.con.close()


def write_clean_copy(db, dst):
    """Copy the work database to `dst` without the staging tables and resume
    records of data sets that were interrupted or failed (what a zip must not
    carry). Returns dst."""
    shutil.copyfile(db, dst)
    con = sqlite3.connect(dst)
    try:
        for (t,) in con.execute("SELECT name FROM sqlite_master WHERE type='table' AND "
                                "name LIKE '%\\_\\_new' ESCAPE '\\'").fetchall():
            con.execute("DROP TABLE IF EXISTS \"{}\"".format(t))
        for t in ("chunks", "stages"):
            con.execute("DELETE FROM {}".format(t))
        con.commit()
        con.execute("VACUUM")
    finally:
        con.close()
    return dst


def sqlite_ro_uri(path):
    """Read-only SQLite URI of an absolute pathlib path: every character
    encoded ("#", "%", "?", spaces), and a Windows share (\\\\server\\share)
    written with an empty URI host, which SQLite requires."""
    uri = path.as_uri()
    if uri.startswith("file://") and not uri.startswith("file:///"):
        uri = "file:////" + uri[len("file://"):]
    return uri + "?mode=ro"


# ------------------------------------------------------------------ reader --
class PackReader(object):
    """Read access to an extracted pack database (thread-safe: one SQLite
    connection per call)."""

    def __init__(self, db_path):
        if not os.path.isfile(db_path):
            raise PackError("offline data pack not found: {}".format(db_path))
        self.db_path = db_path
        con = self._connect()
        try:
            meta = dict(con.execute("SELECT key, value FROM meta").fetchall())
            if meta.get("format") != PACK_FORMAT:
                raise PackError("{} is not an offline data pack".format(db_path))
            if int(meta.get("format_version") or 0) > PACK_FORMAT_VERSION:
                raise PackError("the offline data pack is newer than this plugin (format {}) - "
                                "update the plugin".format(meta.get("format_version")))
            self.meta = meta
            self.bbox = tuple(json.loads(meta["bbox"]))
            self.datasets = {}
            cols = [r[1] for r in con.execute("PRAGMA table_info(datasets)")]
            gaps_col = ", gaps" if "gaps" in cols else ", NULL"      # 2.6.1 packs: none
            for row in con.execute("SELECT name, label, url, fetched, count, status, note, "
                                   "maxw, maxh, geometry{} FROM datasets WHERE status IN "
                                   "(?, ?)".format(gaps_col), USABLE_STATUS):
                gaps = []
                try:
                    gaps = [tuple(float(v) for v in g) for g in json.loads(row[10] or "[]")]
                except (TypeError, ValueError):
                    gaps = [self.bbox]           # unreadable: treat as not covered
                self.datasets[row[0]] = {"label": row[1], "url": row[2], "fetched": row[3],
                                         "count": row[4], "status": row[5],
                                         "note": row[6] or "",
                                         "maxw": row[7] or 0.0, "maxh": row[8] or 0.0,
                                         "geometry": row[9] or "", "gaps": gaps}
            self._rtrees = self._find_rtrees(con)
        except sqlite3.DatabaseError as e:
            raise PackError("the offline data pack could not be read: {}".format(e))
        finally:
            con.close()

    def _find_rtrees(self, con):
        """v2.9.0: {data set: R*Tree table} of the data sets whose R*Tree can be
        used here (valid for the current table, rtree module present)."""
        out = {}
        if not rtree_available():
            return out
        for name, info in self.datasets.items():
            try:
                if not _index_valid(con, name, info.get("count")):
                    continue
                con.execute('SELECT id FROM "{}" LIMIT 0'.format(_rtree(name))).fetchall()
            except sqlite3.Error:
                continue
            out[name] = _rtree(name)
        return out

    def index_kind(self, name):
        """"rtree" when queries of data set `name` use its R*Tree, "btree" (the
        longitude index of every pack) otherwise."""
        return "rtree" if self._real(name) in self._rtrees else "btree"

    def _connect(self):
        # read only: the pack may sit on a write-protected drive. v2.6.2: a
        # proper file URI (a "#" or "%" in the path used to cut it short and
        # create an empty file), and never a read-write connection that
        # would create an empty database when the file has gone
        if not os.path.isfile(self.db_path):
            raise PackError("offline data pack not found: {}".format(self.db_path))
        uri = sqlite_ro_uri(pathlib.Path(os.path.abspath(self.db_path)))
        try:
            con = sqlite3.connect(uri, uri=True)
            # 1.9.0: refuse writes on this connection too (belt and braces next to mode=ro)
            con.execute("PRAGMA query_only = ON")
            return con
        except sqlite3.Error as e:
            raise PackError("the offline data pack could not be opened: {}".format(e))

    @property
    def created(self):
        return (self.meta.get("created") or self.meta.get("started") or "")[:10]

    def _real(self, name):
        """1.19.0: a data set answers to its old or new name (NHD_flowlines = 3DHP_flowlines)."""
        from . import hydro3dhp as H
        return H.real_name(name, self.datasets)

    def has(self, name):
        return self._real(name) in self.datasets

    def info(self, name):
        return self.datasets.get(self._real(name))

    def covers(self, extent, name=None):
        """True when the pack holds every feature of the extent - for data
        set `name`: complete, and the extent touches none of its gaps."""
        if not covers(self.bbox, extent):
            return False
        if name is None:
            return True
        info = self.datasets.get(self._real(name))
        if info is None or info.get("status") != STATUS_COMPLETE:
            return False
        return not any(boxes_touch(g, extent) for g in info.get("gaps") or ())

    def query(self, name, extent):
        """GeoJSON features of data set `name` that intersect the extent."""
        name = self._real(name)
        info = self.datasets.get(name)
        if info is None:
            raise PackError("{} is not in the offline data pack".format(name))
        xmin, ymin, xmax, ymax = [float(v) for v in extent]
        # the box test of every version (longitude index): v2.9.0 keeps it as the
        # final test, so the R*Tree (a pre-selection) never changes the result
        box = ("minx BETWEEN ? AND ? AND maxx >= ? AND miny <= ? AND maxy >= ?")
        args = (xmin - info["maxw"] - 1e-9, xmax, xmin, ymax, ymin)
        rt = self._rtrees.get(name)
        con = self._connect()
        try:
            if rt is not None:
                try:
                    # fid IN (...): SQLite walks the sorted id list through the
                    # primary key - the rows come in fid order without a sort
                    return self._decode(con.execute(
                        "SELECT feat FROM {} WHERE fid IN (SELECT id FROM \"{}\" WHERE "
                        "maxx >= ? AND minx <= ? AND maxy >= ? AND miny <= ?) AND {} "
                        "ORDER BY fid".format(_table(name), rt, box),
                        (xmin, xmax, ymin, ymax) + args), (xmin, ymin, xmax, ymax))
                except sqlite3.Error:
                    self._rtrees.pop(name, None)     # damaged R*Tree: the B-tree from now on
            return self._decode(con.execute(
                "SELECT feat FROM {} WHERE {} ORDER BY fid".format(_table(name), box), args),
                (xmin, ymin, xmax, ymax))
        finally:
            con.close()

    @staticmethod
    def _decode(rows, rect):
        out = []
        for (blob,) in rows:
            f = json.loads(zlib.decompress(blob).decode("utf-8"))
            if intersects_rect(f.get("geometry"), rect):
                out.append(f)
        return out


# --------------------------------------------------------------- locating --
def read_manifest(path):
    """manifest.json of a pack folder, .zip or .sqlite (None when there is
    none) - without extracting anything. 1.10.0: also a file inside a
    format-2 pack folder (its LAYERS.txt, a layer's GeoPackage)."""
    try:
        if path.lower().endswith(".zip"):
            with zipfile.ZipFile(path) as z:
                # the top-most manifest.json (a format-2 zip holds only one)
                cands = sorted((n for n in z.namelist() if _safe_member(n) and
                                n.rsplit("/", 1)[-1] == MANIFEST_NAME),
                               key=lambda n: n.count("/"))
                name = cands[0] if cands else None
                return json.loads(z.read(name).decode("utf-8")) if name else None
        folder = os.path.dirname(path) if os.path.isfile(path) else path
        p = os.path.join(folder, MANIFEST_NAME)
        if os.path.isfile(p):
            with open(p, encoding="utf-8") as fh:
                return json.load(fh)
    except (OSError, ValueError, zipfile.BadZipFile, KeyError):
        return None
    return None


def _extract_target(zip_path, manifest):
    """Where a zipped pack is unpacked: next to the zip when that folder can
    be written, else under the user's home folder."""
    stem = os.path.splitext(os.path.basename(zip_path))[0]
    tag = (manifest or {}).get("created", "")[:10].replace("-", "") or "pack"
    cands = [os.path.join(os.path.dirname(os.path.abspath(zip_path)), stem),
             os.path.join(os.path.expanduser("~"), ".nd_dam_hazard_helper", "offline_packs",
                          "{}_{}".format(stem, tag))]
    return cands


_OPEN_LOCK = threading.Lock()
_READERS = {}


def open_pack(path, log=None):
    """PackReader for a pack folder, its .sqlite, or a .zip (unpacked on
    first use; unpacked again when the zip holds a newer pack)."""
    log = log or (lambda m: None)
    if not path:
        raise PackError("no offline data pack chosen")
    path = os.path.abspath(os.path.expanduser(path))
    with _OPEN_LOCK:
        try:
            stamp = os.path.getmtime(path)
        except OSError:
            raise PackError("offline data pack not found: {}".format(path))
        from . import gpkgpack as GP
        folder = GP.pack_folder_of(path) if not path.lower().endswith(".zip") else ""
        if folder:                    # 1.10.0: format 2 - the manifest's date is the stamp
            try:
                stamp = os.path.getmtime(os.path.join(folder, MANIFEST_NAME))
            except OSError:
                pass
        hit = _READERS.get(path)
        # v2.6.2: an unpacked copy deleted meanwhile is unpacked again
        if hit and hit[0] == stamp and os.path.isfile(hit[1].db_path):
            return hit[1]
        if folder:
            reader = GP.GpkgPackReader(folder)
            _READERS[path] = (stamp, reader)
            return reader
        if path.lower().endswith(".zip"):
            db = _unzip(path, log)
            if os.path.isdir(db):                              # a zipped format-2 folder
                reader = GP.GpkgPackReader(db)
                _READERS[path] = (stamp, reader)
                return reader
        elif os.path.isdir(path):
            db = os.path.join(path, DB_NAME)
        else:
            db = path
        reader = PackReader(db)
        _READERS[path] = (stamp, reader)
        return reader


def _safe_member(name):
    """False for a zip member path that could point outside the folder."""
    n = name.replace("\\", "/")
    return not (n.startswith("/") or ":" in n.split("/", 1)[0] or ".." in n.split("/"))


def _unzip_folder(zip_path, manifest, log):
    """1.10.0: a zipped format-2 pack folder, unpacked once (all its files) - the folder."""
    for target in _extract_target(zip_path, manifest):
        have = read_manifest(target)
        if have and have.get("created") == manifest.get("created"):
            return target
        try:
            os.makedirs(target, exist_ok=True)
            probe = os.path.join(target, ".write_test")
            with open(probe, "w", encoding="utf-8") as fh:
                fh.write("x")
            os.remove(probe)
        except OSError:
            continue
        log("Offline data pack: unpacking {} to {} (once)...".format(
            os.path.basename(zip_path), target))
        with zipfile.ZipFile(zip_path) as z:
            names = [n for n in z.namelist() if _safe_member(n) and not n.endswith("/")]
            top = min((n for n in names if n.rsplit("/", 1)[-1] == MANIFEST_NAME),
                      key=lambda n: n.count("/"))
            prefix = top[:-len(MANIFEST_NAME)]
            for n in names:
                if not n.startswith(prefix) or n == top:
                    continue
                dst = os.path.join(target, *n[len(prefix):].split("/"))
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                with z.open(n) as fin, open(dst + ".part", "wb") as fout:
                    shutil.copyfileobj(fin, fout, 4 * 1024 * 1024)
                os.replace(dst + ".part", dst)
            with open(os.path.join(target, MANIFEST_NAME), "wb") as fh:   # last: "done"
                fh.write(z.read(top))
        return target
    raise PackError("no writable folder to unpack {} into".format(zip_path))


def _unzip(zip_path, log):
    manifest = read_manifest(zip_path)
    if manifest is None or manifest.get("format") != PACK_FORMAT:
        raise PackError("{} is not an offline data pack (no manifest.json)".format(zip_path))
    from . import gpkgpack as GP
    if GP.is_gpkg_manifest(manifest):
        return _unzip_folder(zip_path, manifest, log)
    for target in _extract_target(zip_path, manifest):
        db = os.path.join(target, DB_NAME)
        have = read_manifest(target)
        if os.path.isfile(db) and have and have.get("created") == manifest.get("created"):
            return db
        try:
            os.makedirs(target, exist_ok=True)
            probe = os.path.join(target, ".write_test")
            with open(probe, "w", encoding="utf-8") as fh:
                fh.write("x")
            os.remove(probe)
        except OSError:
            continue
        log("Offline data pack: unpacking {} to {} (once)...".format(
            os.path.basename(zip_path), target))
        tmp = db + ".part"
        with zipfile.ZipFile(zip_path) as z:
            # 1.9.0: a member path with ".." or a drive / root is never used
            names = [n for n in z.namelist() if _safe_member(n)]
            src = next((n for n in names if n.rsplit("/", 1)[-1] == DB_NAME), None)
            if src is None:
                raise PackError("{} holds no {}".format(zip_path, DB_NAME))
            with z.open(src) as fin, open(tmp, "wb") as fout:
                shutil.copyfileobj(fin, fout, 4 * 1024 * 1024)
            os.replace(tmp, db)
            mf = next(n for n in names if n.rsplit("/", 1)[-1] == MANIFEST_NAME)
            with open(os.path.join(target, MANIFEST_NAME), "wb") as fh:
                fh.write(z.read(mf))
        return db
    raise PackError("no writable folder to unpack {} into".format(zip_path))


def describe(path):
    """One line about a pack for the dialog (never raises)."""
    if not path:
        return "No offline data pack - every download uses the internet."
    if not os.path.exists(path):
        return "Not found: {}".format(path)
    m = read_manifest(path)
    if m is None:
        try:
            r = open_pack(path)
            return "{} data sets, built {} ({} {})".format(
                len(r.datasets), r.created, r.meta.get("area", ""), _box(r.bbox))
        except Exception as e:
            return "Not usable: {}".format(e)
    ds = m.get("datasets") or {}
    from . import gpkgpack as GP
    gp = " (one GeoPackage per layer)" if GP.is_gpkg_manifest(m) else ""
    ok = [k for k, v in ds.items() if v.get("status") == STATUS_COMPLETE]
    part = sorted(k for k, v in ds.items() if v.get("status") == STATUS_PARTIAL or
                  (v.get("status") == STATUS_COMPLETE and v.get("gaps")))
    bad = [k for k, v in ds.items()
           if v.get("status") not in (STATUS_COMPLETE, STATUS_PARTIAL, "not available")]
    return "{} data sets{}, built {} for {}{}{}".format(
        len(ok) + len([k for k in part if k not in ok]), gp, (m.get("created") or "")[:10],
        m.get("area") or "the pack area",
        " - incomplete (used with a warning): {}".format(", ".join(part)) if part else "",
        " - missing: {}".format(", ".join(sorted(bad))) if bad else "")


def _box(b):
    return "({:.2f}, {:.2f} to {:.2f}, {:.2f})".format(*b) if b else ""
