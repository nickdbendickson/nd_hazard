"""What the screening actually ran, so the Runner opens with those choices (3.20.0).

The walk-through found this: a dam handed over by ND Dam Breach Screening's tool 7b opened in
the Runner with piping and a 10 m grid even though the screen had run 20 m, and the screen's
scenario (top of dam) was nowhere in sight. Tool 7b passes CELL and METHOD straight into
`ndbreach:rundam` when it starts the run itself, but nothing of that survives for the next
person who opens the window or the wizard on that dam - they get the tool's own starting
values, which is how a 20 m screen turns into a 10 m model run nobody asked for.

So the choices are read back from the dam folder, and the page says where they came from:

    info = read(folder)
    text = source_text(info)     # "from dam.json (ND Dam Breach Screening 0.28.0, 2026-10-13)"
    values, notes = apply_to({"CELL": None, "METHOD": None}, info)

Where they are looked for, in this order (the first that has a value wins, per value):
  1. dam.json's newest "screened" status result (damsuite.dam_record): keys cell_m, method and
     scenario. This is the right home for them - one record per dam, already shared by the
     three plugins - and it is where the Screening plugin is asked to write them.
  2. <dam folder>/_screening_model/run_manifest.json, which the screening's tool 8 writes: its
     "settings" block and its "extra" / "screening_result" row.
Nothing is guessed from a file name or a raster: a value that is not recorded stays missing and
`missing_text` says so, naming the keys the screening has to record. Guessing the cell size
from, say, the screening raster's pixel size would look like provenance without being it.

Decisions:
  * The values are only ever used to fill an EMPTY input. An engineer who typed 10 m keeps 10 m.
  * A cell size is accepted between 0.5 and 500 m (the Runner's own CELL range) and a method
    only when it is one of the Runner's methods; anything else is reported as unreadable rather
    than quietly ignored, because a silently dropped hand-off is the bug this module exists for.
  * The scenario is carried as the Runner's own word ("sunny" / "flood"). The screening's
    "top of dam" is the sunny-day scenario of ND 5.1.1: the breach starts with the pool at the
    top of dam and no inflow. "pmf" / "flood" / "inflow" map to the flood scenario.
  * Reading never raises: a broken dam.json or manifest becomes a note.

Pure Python + the vendored damsuite; no QGIS, no numpy.
"""
import json
import os

from . import breach as brmod
from . import workflow

SCREEN_TOOL = "ndscreen"
SCREEN_LABEL = "ND Dam Breach Screening"
SCREENING_FOLDER = "_screening_model"
MANIFEST_NAME = "run_manifest.json"
# dam.json "screened" result keys this reads, in the order they are tried
CELL_KEYS = ("cell_m", "cell", "grid_m", "cell_size_m")
METHOD_KEYS = ("method", "breach_method", "breach")
SCENARIO_KEYS = ("scenario", "case", "failure_scenario")
# the Runner's CELL range (algorithms.add_model_params)
CELL_MIN_M, CELL_MAX_M = 0.5, 500.0
# what the screening has to record for a complete hand-off
WANTED = ("cell_m", "method", "scenario")

# screening words -> the Runner's scenario
_SCENARIO_WORDS = ((("sunny", "top of dam", "top_of_dam", "topofdam", "fair weather",
                     "fair-weather", "normal"), workflow.SCENARIO_SUNNY),
                   (("flood", "pmf", "inflow", "hydrologic"), workflow.SCENARIO_FLOOD))
# screening words -> the Runner's breach method (brmod.METHODS)
_METHOD_WORDS = ((("froehlich 2008 piping", "froehlich2008 piping", "piping"),
                  brmod.METHOD_PIPING),
                 (("froehlich 2008 overtopping", "overtopping", "overtop"),
                  brmod.METHOD_OVERTOPPING),
                 (("froehlich 1995 piping", "froehlich1995 piping", "f95 piping"),
                  brmod.METHOD_F95_PIPING),
                 (("froehlich 1995 overtopping", "froehlich1995 overtopping",
                   "f95 overtopping"), brmod.METHOD_F95_OVERTOPPING),
                 (("macdonald", "mlm"), brmod.METHOD_MLM),
                 (("von thun", "vonthun", "vtg"), brmod.METHOD_VTG),
                 (("tr-60 curv", "tr60 curv", "curvilinear"), brmod.METHOD_TR60_CURVE),
                 (("tr-60", "tr60"), brmod.METHOD_TR60),
                 (("own", "user", "csv", "given hydrograph"), brmod.METHOD_USER))


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and abs(f) != float("inf") else None


def _txt(v):
    return "" if v is None else str(v).strip()


def cell_from(value):
    """A recorded cell size in metres, or None (out of the Runner's range counts as None)."""
    f = _num(value)
    if f is None or not CELL_MIN_M <= f <= CELL_MAX_M:
        return None
    return f


def method_from(value):
    """A recorded breach method as one of brmod.METHODS, or None."""
    t = _txt(value).lower()
    if not t:
        return None
    if t in brmod.METHODS:
        return t
    for words, key in _METHOD_WORDS:
        if any(w in t for w in words):
            return key
    return None


def scenario_from(value):
    """A recorded scenario as workflow.SCENARIO_SUNNY / SCENARIO_FLOOD, or None."""
    t = _txt(value).lower()
    if not t:
        return None
    if t in (workflow.SCENARIO_SUNNY, workflow.SCENARIO_FLOOD):
        return t
    for words, key in _SCENARIO_WORDS:
        if any(w in t for w in words):
            return key
    return None


def method_index(options, method):
    """Index of `method` in a METHOD enum list, or None.

    `options` is either the Runner's METHODS list of (key, label) pairs or the plain labels a
    Processing parameter reports, so the same call works on this plugin's own list and on an
    installed one whose order may differ."""
    if not method:
        return None
    for i, o in enumerate(options or []):
        key = o[0] if isinstance(o, (tuple, list)) and o else o
        label = o[1] if isinstance(o, (tuple, list)) and len(o) > 1 else o
        if _txt(key).lower() == method:
            return i
        if method_from(label) == method:
            return i
    return None


def _result_values(result):
    """(values, read) from a screening result dict: {key: value} and which keys were there."""
    values, read = {}, []
    if not isinstance(result, dict):
        return values, read
    for field, keys, parse in (("cell_m", CELL_KEYS, cell_from),
                               ("method", METHOD_KEYS, method_from),
                               ("scenario", SCENARIO_KEYS, scenario_from)):
        for k in keys:
            if k not in result:
                continue
            got = parse(result.get(k))
            if got is not None:
                values[field] = got
                read.append(k)
            break
    return values, read


def _from_dam_json(folder, info):
    """dam.json's newest "screened" status: the values and who wrote them."""
    try:
        from ..damsuite import dam_record as DR
        rec = DR.load(folder)
    except ValueError as e:
        info["notes"].append("dam.json could not be read ({}); the screening's choices were "
                             "not read from it.".format(e))
        return {}
    except Exception as e:                                       # noqa - never stop a window
        info["notes"].append("dam.json not available ({}).".format(e))
        return {}
    if rec is None:
        return {}
    entry = rec.latest("screened")
    if not entry:
        return {}
    values, _read = _result_values(entry.get("result"))
    if values:
        info.update({"source": "dam.json", "tool": entry.get("tool") or SCREEN_TOOL,
                     "version": entry.get("version") or "", "when": entry.get("when") or ""})
    info["screened"] = {"tool": entry.get("tool") or "", "version": entry.get("version") or "",
                        "when": entry.get("when") or "",
                        "triage": _txt((entry.get("result") or {}).get("triage")),
                        "hz_class": _txt((entry.get("result") or {}).get("hz_class"))}
    return values


def manifest_path(folder):
    return os.path.join(folder or "", SCREENING_FOLDER, MANIFEST_NAME)


def _from_manifest(folder, info):
    """The screening's per-dam manifest (<dam folder>/_screening_model/run_manifest.json)."""
    p = manifest_path(folder)
    if not os.path.isfile(p):
        return {}
    try:
        with open(p, encoding="utf-8") as fh:
            m = json.load(fh)
        if not isinstance(m, dict):
            raise ValueError("not a JSON object")
    except (OSError, ValueError) as e:
        info["notes"].append("{} could not be read ({}).".format(MANIFEST_NAME, e))
        return {}
    blocks = [m.get("settings"), (m.get("extra") or {}).get("screening_result"),
              m.get("extra"), m.get("dam")]
    values = {}
    for b in blocks:
        got, _read = _result_values(b)
        for k, v in got.items():
            values.setdefault(k, v)
    if values:
        # `info` starts with source "", so setdefault would never fire: only an empty source
        # (nothing found in dam.json) is replaced, and dam.json keeps precedence when it had one
        if not info.get("source"):
            info["source"] = MANIFEST_NAME
        if info.get("source") == MANIFEST_NAME:
            info.update({"tool": _txt(m.get("tool")) or SCREEN_LABEL,
                         "version": _manifest_version(m),
                         "when": _txt(m.get("created")) or _txt(m.get("when"))})
    return values


def _manifest_version(m):
    """The screening's version: its own "version" key, else the number at the end of the
    manifest's "tool" text ("ND Dam Breach Screening 0.28.0")."""
    own = _txt(m.get("version"))
    if own[:1].isdigit():
        return own
    parts = _txt(m.get("tool")).split()
    return parts[-1] if parts and parts[-1][:1].isdigit() else ""


def read(folder):
    """What the screening ran for this dam.

    {"cell_m", "method", "scenario"} (a missing one is absent), plus "source" (where they were
    read), "tool", "version", "when", "screened" (the dam.json status, when there is one),
    "missing" (the WANTED keys with no value) and "notes" (what could not be read)."""
    info = {"source": "", "tool": "", "version": "", "when": "", "notes": [], "missing": [],
            "screened": {}}
    if not folder or not os.path.isdir(folder):
        info["missing"] = list(WANTED)
        return info
    values = _from_dam_json(folder, info)
    for k, v in _from_manifest(folder, info).items():
        values.setdefault(k, v)
    info.update(values)
    info["missing"] = [k for k in WANTED if k not in values]
    return info


def from_screening(info):
    """True when anything of the screening's own run was found for this dam."""
    return bool(info) and any(k in info for k in WANTED)


def source_text(info):
    """"from dam.json (ND Dam Breach Screening 0.28.0, 2026-10-13)" - the line the page shows.

    "" when nothing was read, so a caller can hide the row."""
    if not from_screening(info):
        return ""
    tool = info.get("tool") or SCREEN_TOOL
    label = SCREEN_LABEL if tool in (SCREEN_TOOL, SCREEN_LABEL) else tool
    ver = info.get("version") or ""
    if ver and label.endswith(ver):        # "ND Dam Breach Screening 0.28.0" + " 0.28.0"
        label = label[:-len(ver)].strip()
    who = label + ((" " + info["version"]) if info.get("version") else "")
    when = (info.get("when") or "")[:10]
    return "from {} ({}{})".format(info.get("source") or "the dam folder", who,
                                   ", " + when if when else "")


def values_text(info):
    """"20 m grid, Froehlich 2008 piping, sunny-day (top of dam)" - what was read."""
    bits = []
    if info.get("cell_m") is not None and "cell_m" in info:
        bits.append("{:g} m grid".format(info["cell_m"]))
    if info.get("method"):
        bits.append(brmod.METHOD_LABELS.get(info["method"], info["method"]))
    if info.get("scenario"):
        bits.append("sunny-day (top of dam)" if info["scenario"] == workflow.SCENARIO_SUNNY
                    else "flood (PMF inflow)")
    return ", ".join(bits)


MISSING_LABELS = {"cell_m": "the grid size it ran (cell_m)",
                  "method": "the breach method it used (method)",
                  "scenario": "the scenario it ran (scenario)"}


def missing_text(info):
    """The sentence for the page and the log when the screening recorded only part of it."""
    missing = [k for k in (info or {}).get("missing") or []]
    if not missing:
        return ""
    return ("{} has not recorded {} for this dam, so the tool's own starting value is used. The "
            "Screening plugin records these in dam.json's \"screened\" status.".format(
                SCREEN_LABEL, " or ".join(MISSING_LABELS[k] for k in missing)))


def apply_to(values, info, method_options=None):
    """Fill the EMPTY entries of `values` ({"CELL", "METHOD", "SCENARIO"}) from `info`.

    A key that is not in `values` is not added, and a value the caller already has is never
    replaced. `method_options` (a METHOD enum list) turns the method into its index; without it
    METHOD is set to the method's own key. Returns (values, notes)."""
    values = dict(values or {})
    notes = []
    if not from_screening(info):
        return values, notes
    where = source_text(info)
    if "CELL" in values and values.get("CELL") in (None, "") and info.get("cell_m"):
        values["CELL"] = float(info["cell_m"])
        notes.append("Grid size {:g} m {}".format(info["cell_m"], where))
    if "METHOD" in values and values.get("METHOD") in (None, "") and info.get("method"):
        idx = method_index(method_options, info["method"]) if method_options else None
        values["METHOD"] = info["method"] if idx is None else idx
        notes.append("Breach method {} {}".format(
            brmod.METHOD_LABELS.get(info["method"], info["method"]), where))
    if "SCENARIO" in values and values.get("SCENARIO") in (None, "") and info.get("scenario"):
        values["SCENARIO"] = info["scenario"]
        notes.append("Scenario {} {}".format(info["scenario"], where))
    return values, notes


def report_lines(info):
    """Markdown lines for the run report: what the screening chose and what it did not say."""
    if not info or (not from_screening(info) and not info.get("screened")):
        return []
    lines = ["## Hand-off from the screening", ""]
    if from_screening(info):
        lines.append("- Values {}: {}".format(source_text(info), values_text(info)))
    st = info.get("screened") or {}
    if st.get("when"):
        lines.append("- dam.json screened {} by {}{}{}".format(
            (st.get("when") or "")[:10], st.get("tool") or "?",
            " " + st["version"] if st.get("version") else "",
            "; triage {}".format(st["triage"]) if st.get("triage") else ""))
    if info.get("missing"):
        lines.append("- " + missing_text(info))
    for n in info.get("notes") or []:
        lines.append("- " + n)
    lines.append("")
    return lines
