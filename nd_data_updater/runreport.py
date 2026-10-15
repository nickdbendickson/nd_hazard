"""What a run is doing, what it did, and how old the data is (ND Data Updater 1.4.0).

Four things a user asked for, all of them readings of the pack manifest, so they live together
and none of them needs QGIS:

  * `StepTracker` turns the pack builder's own log lines ("[12/29] USACE National Structure
    Inventory") into the one-line message a long run shows beside the progress bar: "layer 12 of
    29: USACE National Structure Inventory - downloading". The builder's progress callback gets
    only "<label>: <what>", with no number in it, and the number is what tells a user that a
    90-minute run is moving; parsing the log line is how we get it without changing the builder
    (it is a byte-identical copy of the Hazard Helper's, so it cannot be edited here).
  * `run_summary` is the end-of-run block in plain words. The builder already logs every failure,
    but the failures are scattered through 2,000 lines of log and a dam safety engineer should
    not have to read it: what matters is the count of layers built and, for what was not fetched,
    its everyday name and the consequence ("the Hazard report will say 'no traffic counts'").
    The consequence comes from damsuite.packdiff's roles, so this module and the change report
    agree about what a layer is for.
  * `change_report` is the pack change report: the manifest is copied to manifest.previous.json
    before a refresh overwrites it, and damsuite.packdiff diffs the two afterwards. The valuable
    half is `stale`: the already-classified dams in the Dams folder that one of the changed
    layers can move. The copy is kept in the pack, so the report can be produced again later
    (a user who closes the window has not lost it).
  * `vintage_rows` is the (layer, source, downloaded, features) table the other plugins' reports
    stamp their data vintage from, and the per-layer rows the pack window draws.
  * A layer a user replaced with their own file carries `replaced` in its manifest entry, and
    `replaced_layers` / `clear_replaced` are the two halves of leaving it alone: the refresh skips
    it (packsource.plan), the end-of-run block says so, and an explicit "Fetch again" on that one
    layer clears the flag so the layer goes back to the normal cycle. Fetching over somebody's
    own county roads file because it is 31 days old would throw away better data than the
    service has, without telling them.

Decisions:
  * Nothing here opens a layer or touches the network: everything is read from manifest.json
    (and, for the stale dams, from the Dams folder's dam.json files through suite_home.scan).
  * Every function tolerates a missing, half-written or format-1 manifest and returns empty
    rather than raising: these are called from UI callbacks and from the end of a long run that
    must not fail at the last line.
  * The plain words are a small table (PLAIN) keyed on layer name prefixes, not a rename of the
    manifest labels: the labels are the agency's own names ("FHWA HPMS North Dakota") and the
    summary wants the words a user would say ("traffic counts").
Pure Python standard library; damsuite is reached through the vendored ndpack copy.
"""

import datetime
import json
import os
import re

MANIFEST_NAME = "manifest.json"
PREVIOUS_NAME = "manifest.previous.json"
REPORT_NAME = "pack_change_report.txt"
# the builder's own "[12/29] <label>" log line
STEP_RE = re.compile(r"^\s*\[(\d+)\s*/\s*(\d+)\]\s*(.*)$")
GOOD = ("complete",)
# layer name prefix -> the words a user would use for it
PLAIN = (
    ("NSI_structures", "NSI structures"),
    ("NDGISHUB_address", "ND address points"),
    ("ND_address", "ND address points"),
    ("USA_Structures", "USA Structures building outlines"),
    ("MS_buildings", "Microsoft building footprints"),
    ("NID_dams", "the National Inventory of Dams"),
    ("DWR_dam_lines", "the DWR dam lines"),
    ("DWR_dams", "the DWR dams inventory"),
    ("DWR_unknown", "the DWR unknown structures"),
    ("DWR_dugout", "the DWR dugouts"),
    ("SARP_dams", "SARP aquatic barrier dams"),
    ("SARP_crossings", "surveyed road-stream crossings"),
    ("FRA_grade_crossings", "railroad grade crossings"),
    ("NARN_rail", "rail lines"),
    ("ND_roads", "ND road centerlines"),
    ("ND_county_roads", "county roads"),
    ("HPMS", "traffic counts (HPMS)"),
    ("NDDOT_traffic", "traffic counts"),
    ("OSM_lifelines", "OSM lifelines"),
    ("OSM_crossings", "unsurveyed crossings (OSM culverts and fords)"),
    ("OSM_campgrounds", "OSM campgrounds"),
    ("OSM_trailer_parks", "OSM mobile-home parks"),
    ("OSM_places", "OSM towns and places"),
    ("OSM_parks", "OSM parks and places"),
    ("HIFLD", "the HIFLD lifelines"),
    ("3DHP_flowlines", "the 3DHP streams"),
    ("3DHP_waterbodies", "the 3DHP lakes"),
    ("NLCD", "the land cover"),
    ("counties", "the county boundaries"),
    ("ND_counties", "the county boundaries"),
)
# packdiff role -> what a user loses when a layer of that role is missing
ROLE_LOSS = {
    "consequence": "fewer structures are counted, so a hazard class can come out lower than "
                   "it should",
    "access": "roads, railroads and access are incomplete, so the ND railroad rule and the "
              "access checks have less to go on",
    "lifeline": "the lifelines are not counted",
    "dams": "a dam can be missing from the inventory, so a cascade check may miss it",
    "model": "the breach model has to download terrain or falls back to the default "
             "Manning's n",
    "other": "nothing the classification counts",
}


def _packdiff():
    from .ndpack.damsuite import packdiff as PD
    return PD


def read_manifest(pack):
    """The pack manifest ({} when there is none or it cannot be read)."""
    try:
        with open(os.path.join(pack, MANIFEST_NAME), encoding="utf-8") as f:
            man = json.load(f)
        return man if isinstance(man, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def plain_name(name, label=""):
    """The everyday words for a pack layer ("traffic counts"), its label, or its name."""
    for prefix, words in PLAIN:
        if str(name or "").lower().startswith(prefix.lower()):
            return words
    return label or str(name or "")


# ------------------------------------------------------------- the live progress line --
def parse_step(line):
    """(index, total, label) from the builder's "[12/29] <label>" log line, else None."""
    m = STEP_RE.match(str(line or ""))
    if not m:
        return None
    try:
        i, n = int(m.group(1)), int(m.group(2))
    except ValueError:
        return None
    if n <= 0 or i <= 0:
        return None
    return i, n, m.group(3).strip()


class StepTracker:
    """The layer a build is on, taken from its log lines, and the one line to show for it.

    `note(line)` is given every log line (it ignores the ones that are not a step); `line(msg)`
    makes the message for the progress bar out of the last step and the builder's own sub-message
    ("downloading county 12 of 53")."""

    def __init__(self):
        self.index = 0
        self.total = 0
        self.label = ""

    def note(self, text):
        step = parse_step(text)
        if step:
            self.index, self.total, self.label = step
        return step

    def sub(self, msg):
        """The builder's sub-message without the label it repeats in front of it."""
        msg = str(msg or "").strip()
        if self.label and msg.lower().startswith(self.label.lower()):
            msg = msg[len(self.label):].lstrip(" :-")
        return msg

    def line(self, msg=""):
        sub = self.sub(msg)
        if not self.total:
            return sub
        head = f"layer {self.index} of {self.total}: {self.label or 'working'}"
        return f"{head} - {sub}" if sub else head


# --------------------------------------------------------------- the end-of-run block --
def layer_rows(pack, man=None):
    """[row] for every layer the pack holds or failed to hold, for the pack window's table.

    A row is {"name", "label", "group", "kind", "count", "fetched", "source", "status", "note",
    "plain", "role", "replaced", "replaced_at", "replaced_by"}: kind is "vector" | "raster" |
    "missing" (in left_out: it could not be put in at all), and "replaced" marks a layer the
    user put their own file in (which the refresh then leaves alone)."""
    man = read_manifest(pack) if man is None else (man or {})
    PD = None
    try:
        PD = _packdiff()
    except Exception:  # noqa: BLE001 - the roles are a nicety, not the row
        pass
    rows = []
    for kind, key in (("vector", "datasets"), ("raster", "rasters")):
        for name, e in sorted((man.get(key) or {}).items()):
            e = e if isinstance(e, dict) else {}
            rows.append({
                "name": name, "label": e.get("label") or name,
                "group": e.get("group") or "", "kind": kind,
                "count": e.get("count"), "fetched": str(e.get("fetched") or "")[:19],
                "source": e.get("source") or e.get("url") or "",
                "status": e.get("status") or "complete", "note": e.get("note") or "",
                "plain": plain_name(name, e.get("label") or ""),
                "role": PD.role(name)[0] if PD else "",
                "replaced": bool(e.get("replaced")),
                "replaced_at": str(e.get("replaced_at") or "")[:19],
                "replaced_by": e.get("replaced_by") or "",
            })
    for name, why in sorted((man.get("left_out") or {}).items()):
        rows.append({"name": name, "label": name, "group": "", "kind": "missing",
                     "count": None, "fetched": "", "source": "", "status": "not in the pack",
                     "note": str(why), "plain": plain_name(name),
                     "role": PD.role(name)[0] if PD else "", "replaced": False,
                     "replaced_at": "", "replaced_by": ""})
    return rows


def replaced_layers(pack, man=None, rows=None):
    """[(plain words, name, when, who)] for the layers holding a file the user put in.

    These are the layers a refresh must leave alone, and the ones the end-of-run block names so
    nobody wonders why they were not fetched."""
    rows = layer_rows(pack, man) if rows is None else rows
    return [(r["plain"], r["name"], r["replaced_at"][:10], r["replaced_by"])
            for r in rows if r.get("replaced")]


def clear_replaced(pack, names, man=None):
    """Forget that these layers hold the user's own file, so they refresh normally again.

    Called when the user presses "Fetch again" on such a layer: that is them asking for the
    downloaded version back. Returns the names that were flagged. The manifest is rewritten in
    place (nothing else in it is touched) and never through a half-written file."""
    man = read_manifest(pack) if man is None else (man or {})
    wanted = set(names or ())
    cleared = []
    for key in ("datasets", "rasters"):
        for name, e in (man.get(key) or {}).items():
            if name in wanted and isinstance(e, dict) and e.get("replaced"):
                for field in ("replaced", "replaced_at", "replaced_by"):
                    e.pop(field, None)
                cleared.append(name)
    if not cleared:
        return []
    path = os.path.join(pack, MANIFEST_NAME)
    try:
        with open(path + ".tmp", "w", encoding="utf-8") as f:
            json.dump(man, f, indent=1, sort_keys=True)
        os.replace(path + ".tmp", path)
    except (OSError, TypeError, ValueError):
        return []
    return sorted(cleared)


def not_fetched(pack, man=None, rows=None):
    """[(plain words, role, why)] for the layers a user does not have after a run."""
    rows = layer_rows(pack, man) if rows is None else rows
    out = []
    for r in rows:
        if r["status"] in GOOD:
            continue
        if r["status"] == "not available" and not r["note"]:
            r = dict(r, note="the service said it has nothing here")
        out.append((r["plain"], r["role"] or "other", r["note"] or r["status"]))
    seen, unique = set(), []
    for plain, role, why in out:
        if plain in seen:
            continue
        seen.add(plain)
        unique.append((plain, role, why))
    return unique


def pack_summary_lines(pack, man=None):
    """The pack's share of the end-of-run block: how many layers, and what is not there."""
    man = read_manifest(pack) if man is None else (man or {})
    rows = layer_rows(pack, man)
    if not rows:
        return ["Offline pack: not built yet."]
    built = [r for r in rows if r["status"] in GOOD]
    lines = [f"Offline pack: {len(built)} of {len(rows)} layers built."]
    kept = replaced_layers(pack, man, rows)
    missing = not_fetched(pack, man, rows)
    if not missing:
        lines.append("Every layer the plugins read is in the pack.")
        return lines + _kept_lines(kept)
    lines.append("Not fetched (blocked or down): "
                 + ", ".join(p for p, _r, _w in missing) + ".")
    losses, seen = [], set()
    for _plain, role, _why in missing:
        text = ROLE_LOSS.get(role) or ROLE_LOSS["other"]
        if text in seen or role == "other":
            continue
        seen.add(text)
        losses.append(text)
    tail = "The plugins run without these"
    if losses:
        tail += "; " + "; ".join(losses)
    lines.append(tail + ".")
    if any(p for p, _r, _w in missing if "traffic" in p):
        lines.append('The Hazard report will say "no traffic counts".')
    return lines + _kept_lines(kept)


def _kept_lines(kept):
    """The "your own file was left alone" half of the end-of-run block."""
    if not kept:
        return []
    return ["Left alone (your own file): " + ", ".join(p for p, _n, _w, _who in kept) + ". "
            "Press \"Fetch again\" on that layer to go back to the downloaded version."]


def run_summary(results=None, pack=None, man=None):
    """The whole end-of-run block in plain words: the datasets, then the pack.

    `results` are core.Result objects (anything with name / status / message)."""
    lines = []
    by = {}
    for r in results or []:
        by.setdefault(getattr(r, "status", "?"), []).append(getattr(r, "name", "?"))
    order = (("updated", "downloaded"), ("current", "already up to date"),
             ("skipped", "not due yet"), ("offline", "could not be reached"),
             ("error", "failed"), ("cancelled", "cancelled"))
    parts = [f"{len(by[key])} {word}" for key, word in order if by.get(key)]
    if parts:
        lines.append("Datasets: " + ", ".join(parts) + ".")
    for key, word in (("error", "failed"), ("offline", "could not be reached")):
        for name in by.get(key) or []:
            lines.append(f"  {name}: {word}; the copy already on this computer is still used.")
    if pack:
        lines += pack_summary_lines(pack, man)
    return lines or ["Nothing to do."]


# ------------------------------------------------------------- the pack change report --
def previous_path(pack):
    return os.path.join(pack, PREVIOUS_NAME)


def save_previous(pack, man=None):
    """Keep a copy of the manifest before a refresh overwrites it. Returns the path or ""."""
    man = read_manifest(pack) if man is None else man
    if not man:
        return ""
    path = previous_path(pack)
    try:
        with open(path + ".tmp", "w", encoding="utf-8") as f:
            json.dump(man, f, indent=1, sort_keys=True)
        os.replace(path + ".tmp", path)
        return path
    except (OSError, TypeError, ValueError):
        return ""


def read_previous(pack):
    """The manifest copy kept before the last refresh ({} when there is none)."""
    try:
        with open(previous_path(pack), encoding="utf-8") as f:
            man = json.load(f)
        return man if isinstance(man, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _scan_dams(dams):
    if not dams or not os.path.isdir(dams):
        return []
    try:
        from .ndpack.damsuite import suite_home as SH
        return SH.scan(dams) or []
    except Exception:  # noqa: BLE001 - no Dams folder is not an error for the report
        return []


def change_report(pack, old=None, new=None, dams=None, scan=None, title=None):
    """What changed in the pack, and which classified dams that makes out of date.

    Returns {"changes", "summary", "stale", "stale_text", "text", "have_old"}. `old` / `new`
    default to manifest.previous.json and manifest.json in the pack; `scan` replaces
    suite_home.scan (tests, and a caller that has the rows already)."""
    old = read_previous(pack) if old is None else (old or {})
    new = read_manifest(pack) if new is None else (new or {})
    out = {"changes": [], "summary": "", "stale": [], "stale_text": "", "text": "",
           "have_old": bool(old)}
    try:
        PD = _packdiff()
    except Exception as e:  # noqa: BLE001 - without packdiff there is no report, not a failure
        out["summary"] = f"The change report is not available ({e})."
        out["text"] = out["summary"] + "\n"
        return out
    if not new:
        out["summary"] = "There is no pack to compare."
        out["text"] = out["summary"] + "\n"
        return out
    if not old:
        out["summary"] = ("This is the first refresh since the change report was added, so "
                          "there is nothing to compare with yet. The next refresh will report "
                          "what changed.")
        out["text"] = out["summary"] + "\n"
        return out
    changes = PD.diff(old, new) + PD.diff(old, new, kind="rasters")
    rows = _scan_dams(dams) if scan is None else (scan() or [])
    stale = PD.stale_dams(rows, changes)
    out["changes"] = changes
    out["summary"] = PD.summary_text(changes)
    out["stale"] = stale
    out["stale_text"] = PD.stale_text(stale, None)
    head = title or ("Pack change report - " + datetime.date.today().isoformat())
    text = PD.report_text(changes, title=head)
    text += "\nAlready-classified dams\n-----------------------\n" + out["stale_text"] + "\n"
    for row, why in stale:
        dam = row.get("dam_id") or os.path.basename(row.get("folder") or "")
        text += f"  {dam}: {why}\n"
    if not rows:
        text += ("  (no Dams folder was read, so no dam could be checked; set the Dams folder "
                 "in any of the three dam plugins)\n")
    out["text"] = text
    return out


def save_report(pack, text, path=None):
    """Write a change report next to the pack (or at `path`). Returns the path or ""."""
    path = path or os.path.join(pack, REPORT_NAME)
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text or "")
        return path
    except OSError:
        return ""


# ------------------------------------------------------------------- the data vintage --
def vintage_rows(pack, man=None):
    """[(layer, source, downloaded date, feature count)] from the pack manifest.

    This is what the other plugins' reports stamp their data vintage with ("NSI structures,
    USACE NSI API, 2026-02-11, 412,913 structures"), so a report says how old the data behind
    it is. Layers that are not in the pack are left out: a report should stamp what it used."""
    man = read_manifest(pack) if man is None else (man or {})
    rows = []
    for r in layer_rows(pack, man):
        if r["kind"] == "missing":
            continue
        rows.append((r["name"], r["source"], (r["fetched"] or "")[:10], r["count"]))
    return rows


def vintage_text(rows=None, pack=None):
    """The vintage rows as lines to paste into a report."""
    rows = vintage_rows(pack) if rows is None else rows
    if not rows:
        return "No data vintage is recorded (the pack has not been built).\n"
    width = max(len(r[0]) for r in rows)
    lines = []
    for name, source, when, count in rows:
        n = "" if count is None else f"{int(count):,} features"
        lines.append("{}  {}  {}  {}".format(name.ljust(width), (when or "?").ljust(10), n,
                                             source).rstrip())
    return "\n".join(lines) + "\n"


def pack_age_days(pack, man=None, now=None):
    """How old the oldest layer in the pack is, in days (None when unknown)."""
    man = read_manifest(pack) if man is None else (man or {})
    now = now or datetime.datetime.now()
    ages = []
    for r in layer_rows(pack, man):
        when = r["fetched"]
        if not when:
            continue
        try:
            t = datetime.datetime.fromisoformat(when[:19])
        except ValueError:
            continue
        ages.append(max(0.0, (now - t).total_seconds() / 86400.0))
    return max(ages) if ages else None
