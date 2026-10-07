"""One record per dam, shared by the three plugins: `<dam folder>/dam.json`.

File layout (UTF-8, indent=1):
    {"format": "nd_dam_suite/dam_record", "format_version": 1,
     "fields":    {name: value}            (keys sorted),
     "sources":   {name: {"tool", "version", "when", "note"}},
     "status":    [{"step", "tool", "version", "when", "result": {}}],
     "conflicts": [{"field", "old", "new", "old_source", "new_source", "when", "applied"}],
     ... any other top-level key is kept as it was (DamRecord.extra) }

Rules of `set` (the spec's conflict rules):
  * None, "" (after stripping) and NaN never overwrite anything (set returns False).
  * An empty field takes the value; its source is recorded.
  * The same value (numbers: relative tolerance 1e-6; text: case-insensitive, stripped)
    keeps the FIRST source and returns True.
  * A different value from the tool that set the field replaces it (the tool may update its
    own value; the source is refreshed).
  * A different value from ANOTHER tool (or over a value without a recorded source, e.g. typed
    by the engineer) is not applied: it is recorded in `conflicts` with "applied": False and
    set returns False - unless overwrite=True, then it is applied and the conflict entry is
    recorded with "applied": True (an audit trail; "applied" is an addition to the spec).
    Repeating the same refused conflict refreshes the "when" of the existing entry instead of
    adding a new one.
  * Unknown field names raise KeyError; a value that cannot be a number for a number field
    (or +/-inf) raises ValueError.
  * set_many skips None / "" values silently; it returns the fields refused by a conflict.

Other decisions:
  * Timestamps are local time ISO 8601 to the second ("2026-09-30T14:05:00").
  * Unknown names found inside "fields" of a file (a newer tool) are kept and saved again;
    get() returns them; set() refuses them (KeyError).
  * A dam.json that is not valid JSON, not an object, of another "format", or of a newer
    "format_version" raises ValueError from load(); the file is never touched.
  * Legacy import: when dam.json is missing and ND Dam Hazard's `dam_location.json` exists
    ({"lon","lat"} or {"x","y","crs"}, optional "nid"/"nidid", "name", "source"; keys
    case-insensitive as series.py reads them), lon, lat, x, y, crs, nid_id and name are
    filled with source tool "dam_location.json" (the file's own "source" text becomes the
    note). An unreadable dam_location.json, or one without a usable value, is ignored (ND Dam
    Hazard reports it itself).
    Nothing is written until save(). dam_id is never guessed from the folder name.
  * save() is atomic (<path>.tmp then os.replace) and creates the folder when needed. It is
    last-writer-wins: load, change and save promptly (do not hold a record over a long run).
  * next_action: "screen" only when there is no screened AND no modelled / classified status
    (a dam modelled or classified without the screening tool does not need screening). A
    screened result needs a model unless it says so otherwise: result["needs_model"] (bool)
    when present, else result["triage"] == "clear" (the Screening plugin's word) means no
    model. Anything else, including an empty result, needs a model (the conservative side).
"""
import collections
import datetime
import json
import math
import os

FILE_NAME = "dam.json"
LEGACY_LOCATION = "dam_location.json"
LEGACY_TOOL = "dam_location.json"
FORMAT = "nd_dam_suite/dam_record"
FORMAT_VERSION = 1
REL_TOL = 1e-6

FIELDS = collections.OrderedDict([
    ("dam_id", (str, "the folder's dam ID (DWR-5289, ND00123 ...)")),
    ("name", (str, "dam name")),
    ("nid_id", (str, "National Inventory of Dams ID (ND00123)")),
    ("dwr_structure_id", (str, "ND DWR structure number")),
    ("county", (str, "county")),
    ("lon", (float, "breach / dam point longitude, WGS 84 degrees")),
    ("lat", (float, "breach / dam point latitude, WGS 84 degrees")),
    ("x", (float, "the same point, projected x (see crs)")),
    ("y", (float, "the same point, projected y (see crs)")),
    ("crs", (str, "CRS of x / y, e.g. EPSG:26914")),
    ("height_ft", (float, "dam height, ft")),
    ("storage_acft", (float, "storage, acre-ft")),
    ("surface_area_ac", (float, "reservoir surface area, acres")),
    ("top_of_dam_ft", (float, "top of dam elevation, ft")),
    ("normal_pool_ft", (float, "normal pool elevation, ft")),
    ("dam_length_ft", (float, "dam crest length, ft")),
    ("top_of_dam_datum", (str, "datum of top_of_dam_ft / normal_pool_ft: 'NAVD88 ft', 'DEM' "
                               "or 'local' (local datum values are not used as elevations)")),
    ("existing_hazard_class", (str, "High / Medium / Low / Significant ... as given")),
])
STEPS = ("screened", "modelled", "classified", "reviewed")
_KNOWN = ("format", "format_version", "fields", "sources", "status", "conflicts")


def _now():
    return datetime.datetime.now().isoformat(timespec="seconds")


def _when(when):
    if when is None:
        return _now()
    if isinstance(when, datetime.datetime):
        return when.isoformat(timespec="seconds")
    return str(when)


def _parse_when(text):
    try:
        t = datetime.datetime.fromisoformat(str(text).strip().replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if t.tzinfo is not None:
        t = t.astimezone().replace(tzinfo=None)
    return t


def _empty(value):
    if value is None:
        return True
    if isinstance(value, str) and not value.strip():
        return True
    return isinstance(value, float) and math.isnan(value)


def _coerce(name, value):
    """The value in the field's type, or None when empty. ValueError when impossible."""
    kind = FIELDS[name][0]
    if _empty(value):
        return None
    if kind is float:
        if isinstance(value, bool):
            raise ValueError("{}: {!r} is not a number".format(name, value))
        try:
            v = float(str(value).strip()) if isinstance(value, str) else float(value)
        except (TypeError, ValueError):
            raise ValueError("{}: {!r} is not a number".format(name, value))
        if math.isnan(v):
            return None
        if math.isinf(v):
            raise ValueError("{}: {!r} is not a finite number".format(name, value))
        return v
    return str(value).strip()


def _same(a, b):
    if isinstance(a, (int, float)) and isinstance(b, (int, float)) and \
            not isinstance(a, bool) and not isinstance(b, bool):
        return math.isclose(float(a), float(b), rel_tol=REL_TOL, abs_tol=0.0)
    if isinstance(a, str) and isinstance(b, str):
        return a.strip().casefold() == b.strip().casefold()
    return a == b


class DamRecord:
    """The record of one dam folder (see the module docstring for the rules)."""

    def __init__(self, folder):
        self.folder = folder
        self.path = os.path.join(folder, FILE_NAME)
        self.fields = {}
        self.sources = {}
        self.status = []
        self.conflicts = []
        self.extra = {}

    def __repr__(self):
        return "DamRecord({!r}, {} fields)".format(self.folder, len(self.fields))

    # --- fields -----------------------------------------------------------------------------
    def get(self, name, default=None):
        v = self.fields.get(name)
        return default if v is None else v

    def set(self, name, value, tool, version, note="", overwrite=False):
        """Apply one value under the conflict rules. True when the field now holds it."""
        if name not in FIELDS:
            raise KeyError("unknown dam record field: {}".format(name))
        value = _coerce(name, value)
        if value is None:
            return False
        new_source = {"tool": str(tool or ""), "version": str(version or ""), "when": _now(),
                      "note": str(note or "")}
        old = self.fields.get(name)
        if old is None:
            self.fields[name] = value
            self.sources[name] = new_source
            return True
        if _same(old, value):
            return True
        old_source = self.sources.get(name) or {}
        if old_source.get("tool") and old_source.get("tool") == new_source["tool"]:
            self.fields[name] = value
            self.sources[name] = new_source
            return True
        self._conflict(name, old, value, old_source, new_source, applied=bool(overwrite))
        if overwrite:
            self.fields[name] = value
            self.sources[name] = new_source
            return True
        return False

    def _conflict(self, name, old, new, old_source, new_source, applied):
        if not applied:
            for c in self.conflicts:
                if c.get("field") == name and not c.get("applied") and _same(c.get("old"), old) \
                        and _same(c.get("new"), new) and \
                        (c.get("new_source") or {}).get("tool") == new_source["tool"]:
                    c["when"] = new_source["when"]
                    c["new_source"] = dict(new_source)
                    return
        self.conflicts.append({"field": name, "old": old, "new": new,
                               "old_source": dict(old_source), "new_source": dict(new_source),
                               "when": new_source["when"], "applied": applied})

    def set_many(self, values, tool, version, note="", overwrite=False):
        """set() for each {name: value}; returns the names NOT applied because of a conflict.
        All names are checked first (KeyError before anything changes)."""
        values = dict(values or {})
        for name in values:
            if name not in FIELDS:
                raise KeyError("unknown dam record field: {}".format(name))
        refused = []
        for name, value in values.items():
            if _coerce(name, value) is None:
                continue
            if not self.set(name, value, tool, version, note=note, overwrite=overwrite):
                refused.append(name)
        return refused

    # --- status -----------------------------------------------------------------------------
    def add_status(self, step, tool, version, result=None, when=None):
        if step not in STEPS:
            raise ValueError("unknown step {!r}; one of {}".format(step, ", ".join(STEPS)))
        entry = {"step": step, "tool": str(tool or ""), "version": str(version or ""),
                 "when": _when(when), "result": dict(result or {})}
        self.status.append(entry)
        return entry

    def latest(self, step=None, include_previews=True):
        """The newest status entry (of `step`), or None. Ordered by "when"; entries whose
        time cannot be read count as oldest; ties go to the later entry in the list.
        include_previews=False skips entries whose result says {"preview": true} (e.g. the
        ND Dam Breach Runner's own nd_hazard.py class, which is not the authority)."""
        best, best_key = None, None
        for i, s in enumerate(self.status):
            if step is not None and s.get("step") != step:
                continue
            if not include_previews and isinstance(s.get("result"), dict) and \
                    s["result"].get("preview") is True:
                continue
            t = _parse_when(s.get("when"))
            key = (t is not None, t or datetime.datetime.min, i)
            if best_key is None or key > best_key:
                best, best_key = s, key
        return best

    # --- file -------------------------------------------------------------------------------
    def to_dict(self):
        d = {"format": FORMAT, "format_version": FORMAT_VERSION,
             "fields": {k: self.fields[k] for k in sorted(self.fields)},
             "sources": {k: self.sources[k] for k in sorted(self.sources)},
             "status": list(self.status), "conflicts": list(self.conflicts)}
        for k, v in self.extra.items():
            if k not in d:
                d[k] = v
        return d

    def save(self):
        """Write dam.json atomically; returns its path."""
        os.makedirs(self.folder or ".", exist_ok=True)
        text = json.dumps(self.to_dict(), indent=1, ensure_ascii=False, default=str)
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8", newline="\n") as f:
                f.write(text + "\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise
        return self.path


def _from_dict(folder, data, path):
    if not isinstance(data, dict):
        raise ValueError("{} is not a JSON object".format(path))
    if data.get("format") != FORMAT:
        raise ValueError("{} is not a dam record (format {!r}, expected {!r})".format(
            path, data.get("format"), FORMAT))
    fv = data.get("format_version")
    if not isinstance(fv, int) or isinstance(fv, bool) or fv < 1:
        raise ValueError("{}: bad format_version {!r}".format(path, fv))
    if fv > FORMAT_VERSION:
        raise ValueError("{} was written in dam record format {} by a newer tool; this one "
                         "reads format {}. Update the plugin.".format(path, fv, FORMAT_VERSION))
    rec = DamRecord(folder)
    for key, kind in (("fields", dict), ("sources", dict), ("status", list),
                      ("conflicts", list)):
        v = data.get(key, kind())
        if not isinstance(v, kind):
            raise ValueError("{}: '{}' is not a JSON {}".format(
                path, key, "object" if kind is dict else "list"))
        setattr(rec, key, v)
    # damsuite 1.3.0: the entries too - a record whose status / sources / conflicts hold
    # non-objects used to load and then fail in next_action / latest / the home window
    for i, e in enumerate(rec.status):
        if not isinstance(e, dict) or not isinstance(e.get("step"), str):
            raise ValueError("{}: status entry {} is not a status object with a 'step'".format(path, i + 1))
        if e.get("result") is not None and not isinstance(e.get("result"), dict):
            raise ValueError("{}: the result of status entry {} ({}) is not a JSON object".format(
                path, i + 1, e.get("step")))
        if e.get("when") is not None and not isinstance(e.get("when"), str):
            raise ValueError("{}: status entry {} has a non-text 'when'".format(path, i + 1))
    for name, src in rec.sources.items():
        if not isinstance(src, dict):
            raise ValueError("{}: the source of field '{}' is not a JSON object".format(path, name))
    for i, c in enumerate(rec.conflicts):
        if not isinstance(c, dict):
            raise ValueError("{}: conflict entry {} is not a JSON object".format(path, i + 1))
    rec.extra = {k: v for k, v in data.items() if k not in _KNOWN}
    return rec


def _legacy(folder):
    """A record filled from ND Dam Hazard's dam_location.json, or None."""
    p = os.path.join(folder, LEGACY_LOCATION)
    if not os.path.isfile(p):
        return None
    try:
        with open(p, encoding="utf-8-sig") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    low = {str(k).strip().lower(): v for k, v in data.items()}
    rec = DamRecord(folder)
    note = str(low.get("source") or "").strip()
    values = {"lon": low.get("lon"), "lat": low.get("lat"), "x": low.get("x"),
              "y": low.get("y"), "crs": low.get("crs"),
              "nid_id": low.get("nid") or low.get("nidid"), "name": low.get("name")}
    for name, value in values.items():
        try:
            rec.set(name, value, LEGACY_TOOL, "", note=note)
        except ValueError:
            pass
    return rec if rec.fields else None


def load(folder, create=False):
    """The DamRecord of a dam folder, or None. See the module docstring."""
    path = os.path.join(folder, FILE_NAME)
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8-sig") as f:
                data = json.load(f)
        except UnicodeDecodeError as e:
            raise ValueError("{} could not be read: {}".format(path, e))
        except ValueError as e:
            raise ValueError("{} is not valid JSON: {}".format(path, e))
        except RecursionError:
            raise ValueError("{} is not a dam record (nested too deeply to read)".format(path)) from None
        return _from_dict(folder, data, path)
    rec = _legacy(folder)
    if rec is not None:
        return rec
    return DamRecord(folder) if create else None


def _needs_model(result):
    result = result or {}
    if isinstance(result.get("needs_model"), bool):
        return result["needs_model"]
    return str(result.get("triage") or "").strip().lower() != "clear"


def _key(record, entry):
    t = _parse_when(entry.get("when"))
    idx = next((i for i, s in enumerate(record.status) if s is entry), -1)
    return (t is not None, t or datetime.datetime.min, idx)


def _newer(record, a, b):
    """Is status entry a newer than b (b None -> True)? Same ordering as DamRecord.latest."""
    if a is None:
        return False
    if b is None:
        return True
    return _key(record, a) > _key(record, b)


def next_action(record, facts=None):
    """(code, text) of the next thing to do for a dam: screen / model / classify / review /
    refresh / none, in that order of precedence."""
    facts = facts or {}
    scr = record.latest("screened") if record is not None else None
    mod = record.latest("modelled") if record is not None else None
    # a preview class (another tool's own rules) does not count as the classification
    cla = record.latest("classified", include_previews=False) if record is not None else None
    if scr is None and mod is None and cla is None:
        return "screen", "Not screened yet: run the breach screening (triage)."
    if scr is not None and _needs_model(scr.get("result")) and not _newer(record, mod, scr):
        return "model", "Screening says a breach model is needed and none is newer than the " \
                        "screening: run the breach model."
    if mod is not None and _newer(record, mod, cla):
        res = mod.get("result") if isinstance(mod.get("result"), dict) else {}
        if res.get("breach_occurs") is False:
            return "classify", "The breach model says the dam does not fail in that " \
                               "scenario: confirm it, then classify with the scenario that " \
                               "governs."
        return "classify", "The breach model is newer than the hazard classification: " \
                           "classify the dam (again)."
    if facts.get("rasters_newer_than_class"):
        return "classify", "The flood rasters changed after the hazard classification: " \
                           "classify the dam again."
    try:
        n = int(facts.get("open_review_items") or 0)
    except (TypeError, ValueError):
        n = 0
    if n > 0:
        return "review", "{} open review item{}.".format(n, "" if n == 1 else "s")
    try:
        age = float(facts.get("data_age_days") or 0)
    except (TypeError, ValueError):
        age = 0.0
    if age > 365:
        return "refresh", "The data used are {:.0f} days old (over a year): refresh them.".format(
            age)
    change = inventory_change(record, facts)
    if change:
        return "refresh", "The dam inventory changed after the last work ({}): check the dam's "                           "values and screen / model it again.".format(change)
    return "none", "Nothing to do."


def inventory_change(record, facts=None):
    """Text of an inventory change that is newer than the dam's last status, else "".

    ND Dam Breach Screening's inventory diff writes extra["inventory_change"] =
    {"when": ISO time, "fields": {name: {"old", "new"}}, "note"} into dam.json; a
    facts["inventory_changed"] string / True says the same for a dashboard. The change counts
    until a status (screened / modelled / classified / reviewed) is added after it."""
    facts = facts or {}
    f = facts.get("inventory_changed")
    if f:
        return f if isinstance(f, str) else "inventory changed"
    if record is None:
        return ""
    ch = (record.extra or {}).get("inventory_change")
    if not isinstance(ch, dict):
        return ""
    when = _parse_when(ch.get("when"))
    last = record.latest()
    if last is not None and when is not None:
        t = _parse_when(last.get("when"))
        if t is not None and t >= when:
            return ""                                        # worked on since the change
    fields = ch.get("fields") if isinstance(ch.get("fields"), dict) else {}
    names = ", ".join(sorted(fields)) or str(ch.get("note") or "inventory changed")
    return names
