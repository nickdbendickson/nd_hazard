"""The Review tab's logic (0.13): which dams need a look, and the decisions the reviewer records.

Flagged dams come from the work folder's own outputs:
  * breach points (4 / 16 / 18) with dir_conf = low      -> "breach direction uncertain"
  * ponds (16) with pond_conf = low, or a dugout / depression written to the dam layer
  * the hazard layer (8) with hz_border = yes or hz_conf = low  -> "borderline class"
  * the report's review queue (10, summary.csv review = yes)
Decisions are appended to <work>/review_decisions.csv (dam_id, action, value, reviewer, when, note)
and written into the layers: rev_decision / rev_by / rev_date fields on the dam layer (and the
pond layer); a moved breach point moves the point's geometry and marks dir_conf = reviewed. Tools
5, 6, 7, 8 and 12 skip a dam whose rev_decision is "exclude" ("excluded by review").

Pure Python. Layers are read with `read_rows(ref, fields)` (the window reads QGIS layers, the
tests files) and written with an Apply object: `SqliteApply` edits GeoPackage files directly
(sqlite3); the window can give one backed by QGIS instead.
"""
import csv
import datetime
import os
import sqlite3
import struct

from . import workflow as wf

LOG = wf.REVIEW_LOG
ACTIONS = ("accept", "exclude", "set_type", "move_breach", "note")
ACTION_TEXT = {"accept": "accepted as screened", "exclude": "excluded from the screening",
               "set_type": "pond type set by the reviewer", "move_breach": "breach point moved by the reviewer",
               "note": "note"}
FIELDS = ("dam_id", "action", "value", "reviewer", "when", "note")
REV_FIELDS = (("rev_decision", "TEXT"), ("rev_by", "TEXT"), ("rev_date", "TEXT"), ("rev_note", "TEXT"))
POND_TYPES = ("embankment pond", "dugout", "natural depression")
MAX_MOVE_M = 5000.0     # 0.17.0: a moved breach point farther than this from the old one is refused


# ------------------------------------------------------------------ queue --
def _rows(read_rows, ref, fields):
    if not ref:
        return []
    try:
        return read_rows(ref, fields) or []
    except Exception:                                            # noqa
        return []


def queue(work, read_rows, decided=None):
    """[{dam_id, name, why, source, kind, decision}] of the dams that need a look, one entry per
    dam (reasons joined), with any decision already recorded."""
    decided = decided if decided is not None else latest_decisions(work)
    items = {}

    def add(did, why, source, kind="", name=""):
        did = str(did or "").strip()
        if not did:
            return
        it = items.setdefault(did, {"dam_id": did, "name": "", "why": [], "source": set(), "kind": kind})
        if why not in it["why"]:
            it["why"].append(why)
        it["source"].add(source)
        if name and not it["name"]:
            it["name"] = str(name)
        if kind and not it["kind"]:
            it["kind"] = kind

    bpts = wf.out(work, "18", "OUTP") or wf.out(work, "4", "OUTP") or wf.out(work, "16", "OUTP")
    for did, conf, method in _rows(read_rows, bpts, ("dam_id", "dir_conf", "dir_method")):
        if str(conf or "").strip().lower() == "low":
            add(did, "breach direction uncertain ({})".format(method or "no dam line"), "breach points")
    ponds = wf.out(work, "16")
    for did, name, ptype, conf, nf in _rows(read_rows, ponds, ("dam_id", "name", "pond_type", "pond_conf", "no_flood")):
        kind = str(ptype or "")
        if str(conf or "").strip().lower() == "low":
            add(did, "pond type / crest uncertain ({})".format(kind), "ponds", kind, name)
        elif kind and kind != POND_TYPES[0]:
            add(did, "{}: check on an aerial image".format(kind), "ponds", kind, name)
        elif did in items:
            items[did]["kind"] = kind
    haz = wf.out(work, "8")
    for did, name, cls, border, conf in _rows(read_rows, haz, ("dam_id", "name", "hz_class", "hz_border", "hz_conf")):
        if str(border or "").strip().lower() == "yes":
            add(did, "borderline class ({})".format(cls or "?"), "hazard", "", name)
        elif str(conf or "").strip().lower() == "low":
            add(did, "class with low confidence ({})".format(cls or "?"), "hazard", "", name)
    summary = os.path.join(work, wf.SUMMARY_CSV) if work else ""
    if summary and os.path.isfile(summary):
        try:
            with open(summary, newline="", encoding="utf-8-sig") as fh:
                for row in csv.DictReader(fh):
                    if str(row.get("review") or "").strip().lower() in ("1", "true", "yes", "y"):
                        add(row.get("dam_id"), "in the report's review queue", "report", "", row.get("name"))
        except (OSError, csv.Error):
            pass
    out = []
    for did in sorted(items):
        it = items[did]
        d = decided.get(did)
        out.append({"dam_id": did, "name": it["name"], "why": "; ".join(it["why"]),
                    "source": ", ".join(sorted(it["source"])), "kind": it["kind"],
                    "decision": "{} ({}, {})".format(ACTION_TEXT.get(d["action"], d["action"]), d["reviewer"], d["when"][:10])
                    if d else ""})
    return out


# ----------------------------------------------------------------- the log --
def log_path(work):
    return os.path.join(work, LOG)


def read_log(work):
    p = log_path(work)
    if not os.path.isfile(p):
        return []
    out = []
    try:
        with open(p, newline="", encoding="utf-8-sig") as fh:
            for row in csv.DictReader(fh):
                out.append({k: (row.get(k) or "") for k in FIELDS})
    except (OSError, csv.Error):
        return []
    return out


def latest_decisions(work):
    """{dam_id: the last decision row that is not just a note}."""
    out = {}
    for row in read_log(work):
        if row["action"] != "note":
            out[row["dam_id"]] = row
    return out


def record(work, dam_id, action, value="", reviewer="", note=""):
    """Append a decision to the log. Returns the row."""
    if action not in ACTIONS:
        raise ValueError("unknown review action {}".format(action))
    row = {"dam_id": str(dam_id).strip(), "action": action, "value": str(value or ""),
           "reviewer": str(reviewer or "").strip() or "reviewer", "when": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
           "note": str(note or "")}
    p = log_path(work)
    new = not os.path.isfile(p)
    with open(p, "a", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(FIELDS))
        if new:
            w.writeheader()
        w.writerow(row)
    return row


# ---------------------------------------------------------------- applying --
def _gpkg_point_blob(x, y, srs_id):
    """A GeoPackage binary point (no envelope, little endian)."""
    return struct.pack("<2sBBi", b"GP", 0, 1, int(srs_id)) + struct.pack("<BIdd", 1, 1, float(x), float(y))


def _gpkg_point_xy(blob):
    """0.17.0: (x, y) of a GeoPackage binary point (any envelope, either byte order), else None."""
    try:
        blob = bytes(blob)
        if blob[:2] != b"GP":
            return None
        flags = blob[3]
        env = {0: 0, 1: 32, 2: 48, 3: 48, 4: 64}.get((flags >> 1) & 7, 0)
        wkb = blob[8 + env:]
        e = "<" if wkb[0] == 1 else ">"
        if struct.unpack(e + "I", wkb[1:5])[0] % 1000 != 1:     # Point, Point Z / M / ZM
            return None
        return struct.unpack(e + "dd", wkb[5:21])
    except (IndexError, TypeError, struct.error):
        return None


class SqliteApply(object):
    """Write decisions into GeoPackage files with sqlite3 (the work folder's own layers)."""

    def _table(self, con):
        row = con.execute("SELECT table_name FROM gpkg_contents WHERE data_type='features'").fetchone()
        if row is None:
            raise ValueError("no feature table")
        return row[0]

    def set_attributes(self, path, dam_id, values):
        """values = {field: value}; missing fields are added (TEXT). Returns rows changed."""
        con = sqlite3.connect(path)
        try:
            t = self._table(con)
            have = {r[1] for r in con.execute('PRAGMA table_info("{}")'.format(t))}
            for f in values:
                if f not in have:
                    con.execute('ALTER TABLE "{}" ADD COLUMN "{}" TEXT'.format(t, f))
            sets = ", ".join('"{}" = ?'.format(f) for f in values)
            cur = con.execute('UPDATE "{}" SET {} WHERE "dam_id" = ?'.format(t, sets), list(values.values()) + [dam_id])
            con.commit()
            return cur.rowcount
        finally:
            con.close()

    def point_of(self, path, dam_id):
        """0.17.0: (x, y) of the dam's point in the layer's own CRS, or None."""
        con = sqlite3.connect(path)
        try:
            t = self._table(con)
            gcol = con.execute("SELECT column_name FROM gpkg_geometry_columns WHERE table_name=?",
                               (t,)).fetchone()[0]
            row = con.execute('SELECT "{}" FROM "{}" WHERE "dam_id" = ?'.format(gcol, t), (dam_id,)).fetchone()
            return _gpkg_point_xy(row[0]) if row and row[0] is not None else None
        finally:
            con.close()

    def move_point(self, path, dam_id, x, y):
        con = sqlite3.connect(path)
        try:
            t = self._table(con)
            gcol, srs = con.execute("SELECT column_name, srs_id FROM gpkg_geometry_columns WHERE table_name=?",
                                    (t,)).fetchone()
            cur = con.execute('UPDATE "{}" SET "{}" = ? WHERE "dam_id" = ?'.format(t, gcol),
                              (_gpkg_point_blob(x, y, srs), dam_id))
            con.execute("UPDATE gpkg_contents SET min_x=NULL, min_y=NULL, max_x=NULL, max_y=NULL WHERE table_name=?", (t,))
            con.commit()
            return cur.rowcount
        finally:
            con.close()


def _breach_layers(work):
    return (wf.out(work, "4", "OUTP"), wf.out(work, "16", "OUTP"), wf.out(work, "18", "OUTP"))


def move_xy(work, dam_id, value, applier=None):
    """(x, y) of a move_breach value 'x, y' in EPSG:5070 metres. 0.17.0: refused (ValueError)
    when it is more than MAX_MOVE_M from the dam's current breach point - a point typed in
    another CRS (degrees, State Plane feet) or a wrong dam. No current point: not checked."""
    try:
        x, y = (float(v) for v in str(value).replace(";", ",").split(",")[:2])
    except (TypeError, ValueError):
        raise ValueError("move_breach needs 'x, y' in EPSG:5070 metres, got {!r}".format(value))
    applier = applier or SqliteApply()
    point_of = getattr(applier, "point_of", None)
    for p in _breach_layers(work):
        if not p or point_of is None:
            continue
        try:
            old = point_of(p, dam_id)
        except Exception:                                        # noqa - an unreadable layer: next one
            old = None
        if old is None:
            continue
        d = ((x - old[0]) ** 2 + (y - old[1]) ** 2) ** 0.5
        if d > MAX_MOVE_M:
            raise ValueError("The new breach point ({:.1f}, {:.1f}) is {:,.0f} m from the current one "
                             "({:.1f}, {:.1f}); a move of more than {:,.0f} m is refused. Give 'x, y' in "
                             "EPSG:5070 metres (not degrees or State Plane feet).".format(
                                 x, y, d, old[0], old[1], MAX_MOVE_M))
        break
    return x, y


def apply(work, row, applier=None):
    """Write one decision row into the work folder's layers. Returns [(file, rows changed)];
    files that do not exist are skipped. The 18_* copies are updated too when present."""
    applier = applier or SqliteApply()
    did = row["dam_id"]
    stamp = {"rev_decision": row["action"], "rev_by": row["reviewer"], "rev_date": row["when"],
             "rev_note": row.get("note", "")}
    done = []
    dam_layers = [wf.out(work, "3"), wf.out(work, "1"), wf.out(work, "16"), wf.out(work, "18"), wf.out(work, "5"),
                  wf.out(work, "6"), wf.out(work, "8")]
    if row["action"] in ("accept", "exclude", "note"):
        for p in dam_layers:
            if p:
                try:
                    done.append((p, applier.set_attributes(p, did, stamp)))
                except Exception as e:                           # noqa
                    done.append((p, "not written: {}".format(e)))
    elif row["action"] == "set_type":
        vals = dict(stamp)
        vals["pond_type"] = row["value"]
        for p in (wf.out(work, "16"), wf.out(work, "18")):
            if p:
                try:
                    done.append((p, applier.set_attributes(p, did, vals)))
                except Exception as e:                           # noqa
                    done.append((p, "not written: {}".format(e)))
    elif row["action"] == "move_breach":
        x, y = move_xy(work, did, row["value"], applier)
        for p in _breach_layers(work):
            if p:
                try:
                    n = applier.move_point(p, did, x, y)
                    if n:
                        applier.set_attributes(p, did, {"dir_conf": "reviewed", "dir_method": "reviewer"})
                    done.append((p, n))
                except Exception as e:                           # noqa
                    done.append((p, "not written: {}".format(e)))
        for p in dam_layers:
            if p:
                try:
                    applier.set_attributes(p, did, stamp)
                except Exception:                                # noqa
                    pass
    return done


def decide(work, dam_id, action, value="", reviewer="", note="", applier=None):
    """Record and apply a decision in one go. Returns (row, [(file, changed)])."""
    if action == "move_breach":
        move_xy(work, str(dam_id).strip(), value, applier)  # 0.17.0: a refused move is not logged
    row = record(work, dam_id, action, value, reviewer, note)
    return row, apply(work, row, applier)


def excluded_ids(work):
    """The dam IDs whose latest decision is 'exclude'."""
    return sorted(d for d, row in latest_decisions(work).items() if row["action"] == "exclude")
