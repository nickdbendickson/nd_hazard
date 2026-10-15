"""Why a screened class is a floor and not an answer (0.28.0) - the run flags, in plain words.

Step 7 records edge_flag and sim_notes, step 8 lowers hz_conf to "review" and adds a note, and
all of it ends up in 10_reports/summary.csv. Nothing else showed it. A real run said what that
costs: on a 3,937 ac-ft dam the flood ran off a 3 km clip with "Step 7 terrain for larger clips =
cached clips only", the dam screened Medium with 0 buildings, and the only sign was one line in
the CSV. The same dam with clip growth allowed screened High with 27 buildings and 86 roads. The
number on the screen is the one a novice believes, so the reason has to be on the screen too -
the Simple tab's step line and the Results tab's dam table, in red.

    rows   = reasons(row)            # ["flood truncated at the terrain edge", ...]
    line   = text(row)               # the same thing as one red line ("" when the run looks whole)
    bool   = truncated(row)          # the terrain-edge case alone (the one that hides consequences)

`row` is any mapping of field name to value: a step 7 or step 8 row read with sqlite3, a
QgsFeature wrapped in a dict, or a dam.json record. Unknown keys are ignored, so the same call
works on all three.

Decisions:
  * The wording contains the words damsuite.priority.INCOMPLETE_WORDS looks for ("truncat",
    "terrain edge", "cut off", "duration cap"). The statewide priority list groups a dam as
    "incomplete" from those words; a reason this module invented in other words would show red
    in the window and rank as "agree" in the review list, which is the worse of the two bugs.
  * Only the reasons that can HIDE a consequence are listed. A dam cleared by triage, excluded by
    the reviewer or with no flood at all is a result, not a truncated one: it says so in its own
    column and is not flagged here.
  * "cached clips only" is a setting, not a row, so settings_reason() takes the window's settings
    and names it before a run - it is the reason the 3,937 ac-ft dam was wrong, and it is worth
    saying while it can still be changed.
Pure Python standard library.
"""

from ..damsuite import priority as PR

EDGE = "flood truncated at the terrain edge: the consequences beyond it were not counted"
NO_TERRAIN = "the clip could not be enlarged (no terrain beyond it): flood cut off"
SPREADING = "still spreading when the clock ran out (duration cap): flood cut off"
FAILED = "the flood did not run for this dam: no class from a model"
NO_RESULT = "no simulation result: the class is not from a flood model"
CACHED_ONLY = ("Step 7 terrain for larger clips = cached clips only: a flood that reaches the "
               "edge of the step 2 clip is cut off there and the class can be too low")
# text in sim_notes -> the reason to show (first match wins per sentence)
NOTE_WORDS = (("no terrain beyond the clip", NO_TERRAIN),
              ("could not enlarge", NO_TERRAIN),
              ("still spreading", SPREADING),
              ("duration cap", SPREADING),
              ("longest allowed", SPREADING))
# the statuses that are a result of their own, not a truncated run
WHOLE_STATUSES = ("triage", "noflood", "excluded")


def _text(row, *keys):
    out = []
    for k in keys:
        v = row.get(k) if hasattr(row, "get") else None
        if v is None:
            continue
        s = str(v).strip()
        if s and s.lower() not in ("null", "none"):
            out.append(s)
    return " ".join(out)


def _yes(v):
    return str(v or "").strip().lower() in ("yes", "y", "true", "1")


def reasons(row):
    """The reasons this dam's class is a floor rather than an answer, worst first ([] when the
    run looks whole, and for a dam that was never meant to be modelled)."""
    if not hasattr(row, "get"):
        return []
    status = str(row.get("sim_status") or row.get("status") or "").strip()
    if status in WHOLE_STATUSES:
        return []
    out = []
    if _yes(row.get("edge_flag")):
        out.append(EDGE)
    notes = _text(row, "sim_notes", "notes", "hz_notes", "note", "problem").lower()
    for word, why in NOTE_WORDS:
        if word in notes and why not in out:
            out.append(why)
    if status == "FAILED":
        out.append(FAILED)
    elif str(row.get("hz_class") or "").strip() == "NoData" and NO_RESULT not in out:
        out.append(NO_RESULT)
    return out


def truncated(row):
    """True when the flood was cut off at the edge of the terrain (the reason a class is too low
    without anything else looking wrong)."""
    return any(r in (EDGE, NO_TERRAIN, SPREADING) for r in reasons(row))


def text(row, joiner="; "):
    """One line for the window ("" when there is nothing to say)."""
    return joiner.join(reasons(row))


def short(row):
    """A few words for a narrow table column ("" when the run looks whole)."""
    r = reasons(row)
    if not r:
        return ""
    first = r[0].split(":")[0]
    return first + (" (+{})".format(len(r) - 1) if len(r) > 1 else "")


def settings_reason(settings, resolved_clips=None):
    """The reason to show BEFORE a run: "cached clips only" is a setting, and it is the one that
    silently truncated a real screening. `resolved_clips` is the index step 7 will actually get
    (workflow.larger_clips_index); without it the raw setting is read."""
    v = resolved_clips
    if v is None:
        raw = (settings or {}).get("larger_clips")
        try:                     # 3 = follow the terrain source (the default): never cached-only
            v = int(raw if raw is not None else 3)
        except (TypeError, ValueError):
            v = 3
    return CACHED_ONLY if int(v) == 0 else ""


def known_to_priority(line):
    """True when damsuite.priority would group a dam carrying this text as "incomplete" (the
    test that keeps the window's wording and the review list's grouping together)."""
    low = str(line or "").lower()
    return any(w in low for w in PR.INCOMPLETE_WORDS)


# ------------------------------------------- a whole run's worth of flags (sqlite3, no QGIS) --
READ_FIELDS = ("dam_id", "sim_status", "sim_notes", "edge_flag", "scen2_edge_flag",
               "hz_class", "hz_conf", "hz_notes")


def from_gpkg(path, fields=READ_FIELDS):
    """[{field: value}] of the first feature table of a step 7 / step 8 GeoPackage; [] when it
    cannot be read. sqlite3 only, so the window can read it while a run is going on."""
    import os
    import sqlite3
    if not path or not os.path.isfile(path):
        return []
    con = None
    try:
        con = sqlite3.connect(path)
        row = con.execute("SELECT table_name FROM gpkg_contents WHERE data_type='features' "
                          "LIMIT 1").fetchone()
        if not row:
            return []
        t = row[0].replace('"', '""')
        have = {r[1] for r in con.execute('PRAGMA table_info("{}")'.format(t))}
        sel = ", ".join('"{}"'.format(f) if f in have else "NULL" for f in fields)
        return [dict(zip(fields, r)) for r in con.execute('SELECT {} FROM "{}"'.format(sel, t))]
    except Exception:                                            # noqa - unreadable: no flags
        return []
    finally:
        if con is not None:
            con.close()


# ------------------------------------- the same flags for the statewide list (dam.json folders) --
# the keys a plugin of the suite writes its run notes under, in the dam.json status results
RECORD_NOTE_KEYS = ("note", "notes", "hz_notes", "sim_notes", "flags", "problem")
RECORD_STEPS = ("classified", "modelled", "screened")


def record_note(folder):
    """The words in one dam folder's dam.json that say its class is a floor ("" when there are
    none, and for a folder without a record).

    damsuite.priority._flags looks for those words in a scan row's "note", and
    damsuite.suite_home.scan does not fill that key - it has no field for a run note. Without
    this the statewide list only flags a dam with NO model at all, and the truncated flood this
    release is about ranks as "agree": the one grouping that must not be wrong. Reading only, and
    it never raises - a folder that cannot be read contributes no words.
    """
    try:
        from ..damsuite import dam_record as DR
        rec = DR.load(folder) if folder else None
    except Exception:                                            # noqa - no record: no words
        return ""
    if rec is None:
        return ""
    out = []
    for step in RECORD_STEPS:
        try:
            e = rec.latest(step, include_previews=False) if step == "classified" else rec.latest(step)
        except Exception:                                        # noqa
            e = None
        res = e.get("result") if isinstance(e, dict) and isinstance(e.get("result"), dict) else {}
        for k in RECORD_NOTE_KEYS:
            v = str(res.get(k) or "").strip()
            if v and v not in out:
                out.append(v)
    return "; ".join(out)


def note_rows(rows):
    """damsuite.suite_home.scan rows with the "note" damsuite.priority reads filled in from each
    dam.json (the rows themselves, changed in place)."""
    for r in rows or []:
        try:
            if hasattr(r, "get") and not str(r.get("note") or "").strip():
                r["note"] = record_note(r.get("folder"))
        except Exception:                                        # noqa - one dam, not the list
            pass
    return rows


def summary(rows):
    """{"dams", "flagged", "reasons": {reason: count}} over a run's rows."""
    out = {"dams": 0, "flagged": 0, "reasons": {}}
    for r in rows or []:
        out["dams"] += 1
        rs = reasons(r)
        if rs:
            out["flagged"] += 1
        for why in rs:
            out["reasons"][why] = out["reasons"].get(why, 0) + 1
    return out


def summary_text(rows):
    """"3 of 37 dams: flood truncated at the terrain edge (3)" - the red line on the step line
    and above the dam table ("" when every run looks whole)."""
    s = summary(rows)
    if not s["flagged"]:
        return ""
    worst = sorted(s["reasons"].items(), key=lambda kv: -kv[1])
    return "{} of {} dam(s) incomplete - their class is a FLOOR, not an answer: {}".format(
        s["flagged"], s["dams"], "; ".join("{} ({})".format(w.split(":")[0], n) for w, n in worst))
