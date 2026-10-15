# -*- coding: utf-8 -*-
"""v2.5: what changed since the previous classification run.

Every run writes outputs/QA/Last_Run_Snapshot.json (the governing
scenario's result: classes, flooded / cut-off structures with their
location, zone and outcome, overtopped roads, inundated lifelines, the main
settings). The next run reads it first, compares, and writes
QA/Changes_Since_Last_Run.csv plus a summary section.

Structures are matched by location (their IDs are renumbered on every run),
within MATCH_FT; roads by name and ND class. Everything below works on plain
dicts / JSON so it can be tested without QGIS.
"""

import csv
import json
import math
import os
import time

SNAPSHOT_FILE = "Last_Run_Snapshot.json"
CHANGES_FILE = "Changes_Since_Last_Run.csv"
SNAPSHOT_VERSION = 1
MATCH_FT = 30.0
DEPTH_CHANGE_FT = 0.5
SETTING_KEYS = ("one_d", "judgment_as_probable", "persons_per_dwelling",
                "min_area_ft2", "near_ft", "borderline_higher", "incremental_ft",
                "extent_buffer_ft", "access", "lifeline_as_medium", "access_as_medium")
CHANGE_FIELDS = ["Change", "Item", "Before", "Now", "X", "Y"]


def _point(rec):
    g = rec.get("geom")
    if g is None:
        return None
    try:
        p = g.pointOnSurface().asPoint()
        return float(p.x()), float(p.y())
    except Exception:
        try:
            p = g.centroid().asPoint()
            return float(p.x()), float(p.y())
        except Exception:
            return None


def make_snapshot(nd, tm11, structs, roads, lifelines, crs_id, dam_name, cfg, scenario_paths,
                  governing="", zone_labels=None, address_fn=None):
    """JSON-ready dict of the governing result. `lifelines`: [(name,
    category)] of inundated facilities; `scenario_paths`: {name: depth path}."""
    zl = zone_labels or {}
    st = []
    for s in structs:
        wet = s.get("outcome") not in (None, "Not inundated")
        if s.get("habitable") == "No" or not (wet or s.get("cut_off")):
            continue
        pt = _point(s)
        if pt is None:
            continue
        addr = ""
        if address_fn is not None:
            try:
                addr = address_fn(s)[0]
            except Exception:
                addr = ""
        st.append({"x": round(pt[0], 3), "y": round(pt[1], 3), "id": s.get("id"),
                   "category": s.get("category") or "", "habitable": s.get("habitable") or "",
                   "fema": s.get("fema_category") or "",
                   "zone": zl.get(s.get("zone"), "") if wet else "",
                   "outcome": s.get("outcome") or "", "persons": int(s.get("persons") or 0),
                   "depth": None if s.get("max_depth") is None else round(float(
                       s["max_depth"]), 2),
                   "cut_off": bool(s.get("cut_off")), "address": addr})
    rd = [{"name": r.get("name") or "unnamed road", "cls": r.get("nd_class") or "",
           "len": round(float(r.get("length_ft") or 0.0))} for r in roads or []]
    settings = {}
    for k in SETTING_KEYS:
        v = cfg.get(k)
        if isinstance(v, (str, int, float, bool)) or v is None:
            settings[k] = v
    return {"version": SNAPSHOT_VERSION, "time": time.strftime("%Y-%m-%d %H:%M"),
            "dam": dam_name, "crs": crs_id or "", "governing": governing,
            "nd": nd.get("cls"), "tm11": tm11.get("cls"), "lij": round(float(
                tm11.get("lij") or 0.0), 1),
            "structures": st, "roads": rd,
            "lifelines": sorted({"{} ({})".format(n or "(unnamed)", c or "")
                                 for n, c in lifelines or []}),
            "settings": settings, "rasters": dict(scenario_paths or {})}


def read_snapshot(folder):
    path = os.path.join(folder, SNAPSHOT_FILE)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) and data.get("version") == SNAPSHOT_VERSION \
            else None
    except (OSError, ValueError):
        return None


def write_snapshot(folder, snap):
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, SNAPSHOT_FILE)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(snap, fh, indent=1)
    os.replace(tmp, path)
    return path


def _match(before, now, tol):
    """Pairs (b, n) matched by nearest location within tol (greedy, closest
    first); unmatched lists."""
    cell = max(tol, 1e-9)
    grid = {}
    for j, n in enumerate(now):
        grid.setdefault((int(math.floor(n["x"] / cell)), int(math.floor(n["y"] / cell))),
                        []).append(j)
    cands = []
    for i, b in enumerate(before):
        kx, ky = int(math.floor(b["x"] / cell)), int(math.floor(b["y"] / cell))
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for j in grid.get((kx + dx, ky + dy), ()):
                    d = math.hypot(now[j]["x"] - b["x"], now[j]["y"] - b["y"])
                    if d <= tol:
                        cands.append((d, i, j))
    cands.sort()
    used_b, used_n, pairs = set(), set(), []
    for _d, i, j in cands:
        if i in used_b or j in used_n:
            continue
        used_b.add(i)
        used_n.add(j)
        pairs.append((before[i], now[j]))
    return (pairs, [b for i, b in enumerate(before) if i not in used_b],
            [n for j, n in enumerate(now) if j not in used_n])


def _desc(s):
    return "structure {}{} ({})".format(s.get("id"), " " + s["address"] if s.get("address")
                                        else "", s.get("fema") or s.get("category") or "")


def diff(before, now, match_units):
    """(summary lines, change rows) of `now` against `before`. match_units:
    MATCH_FT in the analysis CRS units."""
    lines, rows = [], []
    if before is None:
        return ["No earlier run to compare with (first run with v2.5, or the snapshot was "
                "removed)."], rows
    lines.append("Compared with the run of {}: ND {} -> {}, TM 11 {} -> {} (LIJ {} -> "
                 "{}).".format(before.get("time", "?"), before.get("nd"), now.get("nd"),
                               before.get("tm11"), now.get("tm11"), before.get("lij"),
                               now.get("lij")))
    if before.get("nd") != now.get("nd"):
        rows.append(["ND class", "dam", before.get("nd"), now.get("nd"), "", ""])
    if before.get("tm11") != now.get("tm11"):
        rows.append(["TM 11 class", "dam", before.get("tm11"), now.get("tm11"), "", ""])
    if (before.get("crs") or "") != (now.get("crs") or ""):
        lines.append("The analysis CRS changed ({} -> {}) - structures cannot be matched by "
                     "location.".format(before.get("crs") or "?", now.get("crs") or "?"))
        return lines, rows
    changed_set = [k for k in sorted(set(before.get("settings", {})) | set(now.get("settings",
                                                                                    {})))
                   if before.get("settings", {}).get(k) != now.get("settings", {}).get(k)]
    for k in changed_set:
        rows.append(["Setting", k, before["settings"].get(k), now["settings"].get(k), "", ""])
    rchanged = [k for k in sorted(set(before.get("rasters", {})) | set(now.get("rasters", {})))
                if before.get("rasters", {}).get(k) != now.get("rasters", {}).get(k)]
    for k in rchanged:
        rows.append(["Raster", k, before.get("rasters", {}).get(k),
                     now.get("rasters", {}).get(k), "", ""])
    if changed_set or rchanged:
        lines.append("Inputs changed: {}".format(", ".join(
            changed_set + ["raster '{}'".format(k) for k in rchanged])))
    pairs, gone, new = _match(before.get("structures", []), now.get("structures", []),
                              match_units)
    zone_ch, out_ch, dep_ch, pers_ch = [], [], [], []
    for b, n in pairs:
        if b.get("zone") != n.get("zone"):
            zone_ch.append((b, n))
            rows.append(["ACER zone", _desc(n), b.get("zone") or "-", n.get("zone") or "-",
                         n["x"], n["y"]])
        if b.get("outcome") != n.get("outcome"):
            out_ch.append((b, n))
            rows.append(["ND outcome", _desc(n), b.get("outcome"), n.get("outcome"), n["x"],
                         n["y"]])
        if b.get("habitable") != n.get("habitable"):
            rows.append(["Habitable", _desc(n), b.get("habitable") or "-",
                         n.get("habitable") or "-", n["x"], n["y"]])
        elif (b.get("category") or "") != (n.get("category") or ""):
            rows.append(["Structure category", _desc(n), b.get("category") or "-",
                         n.get("category") or "-", n["x"], n["y"]])
        bd, nd_ = b.get("depth"), n.get("depth")
        if bd is not None and nd_ is not None and abs(nd_ - bd) >= DEPTH_CHANGE_FT:
            dep_ch.append((b, n))
            rows.append(["Depth ft", _desc(n), bd, nd_, n["x"], n["y"]])
        if b.get("persons") != n.get("persons"):
            pers_ch.append((b, n))
            rows.append(["Persons", _desc(n), b.get("persons"), n.get("persons"), n["x"],
                         n["y"]])
    for s in new:
        rows.append(["Newly flooded / cut off", _desc(s), "-", "{} {}".format(
            s.get("zone") or "", s.get("outcome") or "").strip(), s["x"], s["y"]])
    for s in gone:
        rows.append(["No longer flooded / cut off", _desc(s), "{} {}".format(
            s.get("zone") or "", s.get("outcome") or "").strip(), "-", s["x"], s["y"]])
    lines.append("Structures: {} newly flooded or cut off, {} no longer, {} with another ACER "
                 "zone, {} with another ND outcome, {} with depth changed by >= {:g} ft, {} "
                 "with other persons".format(len(new), len(gone), len(zone_ch), len(out_ch),
                                             len(dep_ch), DEPTH_CHANGE_FT, len(pers_ch)))

    def keyed(items):
        out = {}
        for r in items:
            k = (r["name"], r["cls"])
            out[k] = out.get(k, 0) + 1
        return out
    rb, rn = keyed(before.get("roads", [])), keyed(now.get("roads", []))
    r_new = sorted(k for k in rn if rn[k] > rb.get(k, 0))
    r_gone = sorted(k for k in rb if rb[k] > rn.get(k, 0))
    for name, cls in r_new:
        rows.append(["Road newly overtopped", name, "-", cls, "", ""])
    for name, cls in r_gone:
        rows.append(["Road no longer overtopped", name, cls, "-", "", ""])
    if r_new or r_gone:
        lines.append("Roads: {} stretch(es) newly overtopped or in another class, {} no "
                     "longer".format(len(r_new), len(r_gone)))
    lb, ln = set(before.get("lifelines", [])), set(now.get("lifelines", []))
    for x in sorted(ln - lb):
        rows.append(["Lifeline newly inundated", x, "-", "inundated", "", ""])
    for x in sorted(lb - ln):
        rows.append(["Lifeline no longer inundated", x, "inundated", "-", "", ""])
    if ln != lb:
        lines.append("Lifeline / critical facilities: {} newly inundated, {} no longer".format(
            len(ln - lb), len(lb - ln)))
    if len(rows) == 0:
        lines.append("No differences.")
    return lines, rows


def write_changes(path, rows):
    """CSV of the change rows at `path` (the caller picks a free name when
    the previous copy is open in Excel)."""
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(CHANGE_FIELDS)
        for r in rows:
            w.writerow(["" if v is None else v for v in r])
    return path


# ---------------------------------------------------------- 2.30.0: every run --
# The snapshot above is the PREVIOUS run (one file, overwritten); the log below is EVERY run,
# so "what was this dam classified as before, with which data and which plugin version?" can
# be answered years later. It is a small append-only JSON file in the dam folder.
HISTORY_FILE = "Classification_History.json"
HISTORY_VERSION = 1
MAX_ENTRIES = 500                # the oldest entries are dropped beyond this
HISTORY_HEAD = ["Date", "ND class", "TM 11 class", "LIJ", "Governing scenario",
                "Plugin version", "Data vintage", "Structures inundated", "Note"]
# the entry keys kept for the table, in order
_VINTAGE_MAX = 12                # vintage lines kept per entry (the table shows the first few)


def history_path(dam_folder):
    return os.path.join(dam_folder or "", HISTORY_FILE)


def make_entry(nd, tm11, dam_name="", governing="", plugin_version="", vintage=(), cfg=None,
               counts=None, outputs="", note="", when=None):
    """One run's summary, JSON-ready. `vintage`: the data-vintage lines of the run (the
    pipeline's QA table), kept as text because that is what a reader needs to judge the row."""
    nd, tm11 = nd or {}, tm11 or {}
    cnt = dict(counts or (nd.get("counts") or {}))
    keep = {}
    for k in SETTING_KEYS:
        v = (cfg or {}).get(k)
        if isinstance(v, (str, int, float, bool)) or v is None:
            keep[k] = v
    return {"version": HISTORY_VERSION, "time": when or time.strftime("%Y-%m-%d %H:%M"),
            "dam": str(dam_name or ""), "nd": nd.get("cls"), "tm11": tm11.get("cls"),
            "lij": round(float(tm11.get("lij") or 0.0), 1),
            "provisional": bool(nd.get("provisional")),
            "incomplete": list(nd.get("incomplete") or ()),
            "governing": str(governing or ""), "plugin_version": str(plugin_version or ""),
            "vintage": [str(v) for v in list(vintage or ())[:_VINTAGE_MAX]],
            "counts": {k: cnt.get(k) for k in sorted(cnt)}, "outputs": str(outputs or ""),
            "note": str(note or ""), "settings": keep}


def read_entries(dam_folder):
    """The runs recorded for this dam, oldest first ([] when there is no log / it is
    unreadable - a classification must never fail over its own history)."""
    path = history_path(dam_folder)
    if not path or not os.path.isfile(path):
        return []
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return []
    runs = data.get("runs") if isinstance(data, dict) else data
    return [r for r in (runs or []) if isinstance(r, dict)]


def append_entry(dam_folder, entry, keep=MAX_ENTRIES):
    """Add one run to <dam folder>/Classification_History.json and return the path ("" when
    there is no dam folder). The log lives in the DAM folder, not in outputs/, so that
    clearing the outputs folder - which every re-run does - cannot throw the history away."""
    if not dam_folder or not os.path.isdir(dam_folder):
        return ""
    runs = read_entries(dam_folder)
    runs.append(entry)
    if keep and len(runs) > int(keep):
        runs = runs[-int(keep):]
    path = history_path(dam_folder)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"version": HISTORY_VERSION, "file": HISTORY_FILE, "runs": runs}, fh, indent=1)
    os.replace(tmp, path)
    return path


def class_changes(entries):
    """[(when, from class, to class)] - every run whose ND class differs from the run before."""
    out = []
    prev = None
    for e in entries or []:
        cls = e.get("nd")
        if prev is not None and cls != prev:
            out.append((e.get("time") or "?", prev, cls))
        prev = cls
    return out


def history_rows(entries):
    """[[date, ND, TM 11, LIJ, scenario, plugin, vintage, structures, note]] for a table."""
    rows = []
    for e in entries or []:
        cnt = e.get("counts") or {}
        rows.append([e.get("time") or "", "{}{}".format(e.get("nd") or "?",
                                                        " (provisional)"
                                                        if e.get("provisional") else ""),
                     e.get("tm11") or "?", e.get("lij"), e.get("governing") or "",
                     e.get("plugin_version") or "",
                     "; ".join(e.get("vintage") or [])[:120],
                     cnt.get("habitable_inundated"), e.get("note") or ""])
    return rows


def history_one_line(entries, current=None):
    """The card's one-line history ("" when this is the first run of this dam)."""
    runs = list(entries or [])
    if current is not None:
        runs = runs + [current]
    if len(runs) < 2:
        return ""
    first, last = runs[0], runs[-1]
    changes = class_changes(runs)
    head = "{} classification run(s) since {}; ND {} then, {} now".format(
        len(runs), str(first.get("time") or "?").split(" ")[0], first.get("nd") or "?",
        last.get("nd") or "?")
    if changes:
        when, was, now = changes[-1]
        head += " (last change {} -> {} on {})".format(was, now, str(when).split(" ")[0])
    else:
        head += " (unchanged)"
    return head


def history_lines(entries, current=None, limit=10):
    """The summary / report section: class over time with the vintage and version behind it."""
    runs = list(entries or [])
    if current is not None:
        runs = runs + [current]
    if not runs:
        return ["No earlier classification run is recorded for this dam ({} is written from "
                "this run on).".format(HISTORY_FILE)]
    out = []
    shown = runs[-int(max(1, limit)):]
    if len(runs) > len(shown):
        out.append("{} earlier run(s) not shown - the full log is {}".format(
            len(runs) - len(shown), HISTORY_FILE))
    for e in shown:
        bits = ["{}: ND {}{}".format(e.get("time") or "?", e.get("nd") or "?",
                                     " (provisional)" if e.get("provisional") else "")]
        bits.append("TM 11 {} (LIJ {})".format(e.get("tm11") or "?", e.get("lij")))
        if e.get("plugin_version"):
            bits.append("plugin {}".format(e["plugin_version"]))
        if e.get("governing"):
            bits.append("scenario '{}'".format(e["governing"]))
        if e.get("vintage"):
            bits.append("data: " + "; ".join(e["vintage"][:3]))
        if e.get("incomplete"):
            bits.append("INCOMPLETE ({})".format(", ".join(e["incomplete"])))
        out.append(" | ".join(bits))
    for when, was, now in class_changes(runs):
        out.append("CLASS CHANGED on {}: {} -> {} - say in the submittal why (data, model or "
                   "development)".format(when, was, now))
    out.append("Every run is recorded in <dam folder>/{} with its data vintage and the plugin "
               "version that produced it.".format(HISTORY_FILE))
    return out


def write_history_csv(path, entries):
    """The log as a CSV for a submittal. Returns the path."""
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(HISTORY_HEAD)
        for row in history_rows(entries):
            w.writerow(["" if v is None else v for v in row])
    return path
