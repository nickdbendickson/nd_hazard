"""Population at risk, flood severity, warning time - and loss of life only when an engineer
has signed a rate table (3.20.0).

This is feature 12 of the review: report PAR day and night, the flood-severity split and the
warning-time split for every dam, and keep loss of life out of the numbers until a named
engineer has signed off the fatality rates that produce it. The arithmetic is
damsuite.loss (shared with the other two plugins, and tested there); this module is the part
that belongs to the Runner:

  * the rate table as a FILE the plugin reads, signed off the way the ACER TM 11 chart file is
    (core/fatality_rates.json; the Hazard Helper's chartsign.py is the pattern, and
    `write_rate_file` / `signoff_status` here follow its rules - one engineer is enough, a
    second checker is optional, and the file never says a check happened that nobody recorded);
  * the shipped file is deliberately EMPTY and unsigned: no rates, no signature. A plugin that
    shipped numbers would be making the engineering judgement for the engineer, which is the
    whole reason loss.py refuses to do it;
  * the rows a consequences run already has, turned into the rows loss.py wants;
  * the report section, which says PAR and the two splits always, and says loss of life is not
    estimated - and why - until the table is signed.

One guard of our own: damsuite.loss.estimate adds up only the cells that have a rate, so a
table that is signed but has an empty cell would come back as a total that is missing those
people - a number too low, which is the one direction this suite does not allow. So
`complete_cells` checks that every (severity, warning band) cell the flood actually fills has a
rate, and `assess` reports loss of life only when it does; otherwise it names the empty cells.

The thresholds that split severity (depth x velocity 160 / 40, depth 13 ft) and warning time
(15 and 60 minutes) are damsuite.loss's, not this tool's: they are the same for the three
plugins so one dam does not get two answers. They are NOT the consequences tool's own
`sev_*` / `warning_edges_min` parameters, and the report says which is which - the two numbers
are both reported rather than one quietly replacing the other.

Pure Python + the vendored damsuite; no QGIS, no numpy (the caller passes plain rows).
"""
import datetime
import hashlib
import json
import os
import re

from ..damsuite import loss as LOSS

FILE_NAME = "fatality_rates.json"
FORMAT_NOTE = "nd_breach_runner fatality rate table, 3.20.0 format"
# One engineer's sign-off is enough, as for the ACER TM 11 charts (chartsign.py 2.28.1); a
# second checker may be recorded. True would demand a different person as checker.
REQUIRE_DIFFERENT_CHECKER = False
STATUS_SIGNED = "signed off"
STATUS_UNSIGNED = "not signed off"
OUTPUT_NAMES = {"csv": "Population at Risk (PAR).csv",
                "json": "Population at Risk (PAR).json",
                "markdown": "Population at Risk (PAR).md"}
NOT_ESTIMATED = ("Loss of life is NOT estimated. Population at risk, flood severity and "
                 "warning time are reported instead.")


def plugin_file():
    """core/fatality_rates.json of this plugin (shipped unsigned)."""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), FILE_NAME)


def _now():
    return datetime.datetime.now().isoformat(timespec="seconds")


def _norm_name(s):
    return re.sub(r"\s+", " ", str(s or "").strip()).casefold()


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and abs(f) != float("inf") else None


def empty_rates():
    """The rate table's shape with no numbers in it: what the engineer fills in."""
    return {sev: {band: None for band in LOSS.WARNING_BANDS} for sev in LOSS.SEVERITIES}


# ------------------------------------------------------------------ the file --
def load(path=""):
    """(table, problem): the rate table file, or (None, text).

    `path` empty = the plugin's own core/fatality_rates.json. A file that is not JSON, not an
    object, or has no "rates" object is refused with a sentence, never half-read: a rate table
    read wrongly would put a wrong number of deaths in a report."""
    p = path or plugin_file()
    if not os.path.isfile(p):
        return None, "no fatality rate table at {}".format(p)
    try:
        with open(p, encoding="utf-8-sig") as fh:
            d = json.load(fh)
    except (OSError, ValueError) as e:
        return None, "the fatality rate table {} could not be read ({})".format(p, e)
    if not isinstance(d, dict) or not isinstance(d.get("rates"), dict):
        return None, ("{} is not a fatality rate table (it needs a \"rates\" object of "
                      "severity -> warning band -> fraction)".format(p))
    d["path"] = p
    return d, ""


def signoff_status(signed_off_by, checked_by="", require_different=None):
    """(status, reason) - the chart file's rule: an engineer named is a sign-off."""
    req = REQUIRE_DIFFERENT_CHECKER if require_different is None else bool(require_different)
    a, b = _norm_name(signed_off_by), _norm_name(checked_by)
    if not a and not b:
        return STATUS_UNSIGNED, ("no engineer named: nobody has signed the fatality rates off")
    if req and not a:
        return STATUS_UNSIGNED, ("'signed off by' is empty (a second checker alone is not a "
                                 "sign-off)")
    if req and not b:
        return STATUS_UNSIGNED, "'checked by' is empty: a second engineer must check"
    if req and a == b:
        return STATUS_UNSIGNED, ("the same person is named as signer and checker: a person may "
                                 "not check their own work")
    if a and b and a != b:
        return STATUS_SIGNED, "signed off by {}, checked by {} (as entered)".format(
            str(signed_off_by).strip(), str(checked_by).strip())
    return STATUS_SIGNED, "signed off by {} (one engineer; no second checker recorded)".format(
        str(signed_off_by or checked_by).strip())


def parse_rates(values):
    """{severity: {band: fraction or None}} from anything a form or a CSV gives.

    Accepts the nested form, "high/under15" keys and (severity, band) tuples; a fraction must
    be between 0 and 1 (a percentage like 75 is refused rather than read as 7500%)."""
    out = empty_rates()
    bad = []
    items = []
    if isinstance(values, dict):
        for k, v in values.items():
            if isinstance(v, dict):
                for b, x in v.items():
                    items.append((k, b, x))
            elif isinstance(k, (tuple, list)) and len(k) == 2:
                items.append((k[0], k[1], v))
            else:
                sev, _s, band = str(k).partition("/")
                items.append((sev, band, v))
    for sev, band, v in items:
        s, b = str(sev or "").strip().lower(), str(band or "").strip().lower()
        if s not in out or b not in LOSS.WARNING_BANDS:
            bad.append("{}/{}".format(sev, band))
            continue
        if v in (None, ""):
            continue
        f = _num(v)
        if f is None or not 0.0 <= f <= 1.0:
            bad.append("{}/{} = {!r} (a fraction of the population at risk, 0 to 1)".format(
                sev, band, v))
            continue
        out[s][b] = f
    return out, bad


def write_rate_file(path, rates=None, signed_off_by="", checked_by="", source="",
                    signed_off_on="", notes="", require_different_checker=None):
    """Write a fatality rate table file. `path`: a .json file or a folder (then
    fatality_rates.json in it).

    The numbers are always written (an unsigned file is a draft an engineer can review); only
    the status decides whether anything uses them. Returns
    {"path", "status", "reason", "sha256", "bad", "empty_cells"}."""
    if os.path.isdir(path) or not str(path).lower().endswith(".json"):
        os.makedirs(path, exist_ok=True)
        path = os.path.join(path, FILE_NAME)
    else:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    table, bad = parse_rates(rates if rates is not None else empty_rates())
    status, reason = signoff_status(signed_off_by, checked_by, require_different_checker)
    when = str(signed_off_on or "").strip() or datetime.date.today().isoformat()
    parts = [FORMAT_NOTE,
             "Written by ND Dam Breach Runner on {}. Sign-off: {}. The tool records the names "
             "entered; it cannot confirm that a check was made.".format(_now(), reason)]
    if str(notes or "").strip():
        parts.append("Engineer's notes: " + str(notes).strip())
    d = {"status": status,
         "source": str(source or "").strip(),
         "signed_off_by": str(signed_off_by or "").strip() if status == STATUS_SIGNED else "",
         "signed_off_on": when if status == STATUS_SIGNED else "",
         "checked_by": str(checked_by or "").strip(),
         "entered_by": str(signed_off_by or checked_by or "").strip(),
         "severities": list(LOSS.SEVERITIES), "warning_bands": list(LOSS.WARNING_BANDS),
         "warning_labels": dict(LOSS.WARNING_LABELS),
         "severity_rule": ("damsuite.loss: high = depth x velocity >= {:g} ft2/s or depth >= "
                           "{:g} ft; medium = depth x velocity >= {:g} ft2/s; low = anything "
                           "wet above {:g} ft".format(LOSS.DV_HIGH, LOSS.DEPTH_HIGH_FT,
                                                      LOSS.DV_MEDIUM, LOSS.COUNT_DEPTH_FT)),
         "notes": " ".join(parts),
         "rates": table}
    if bad:
        d["refused"] = bad
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(d, fh, indent=1, sort_keys=True)
    os.replace(tmp, path)
    return {"path": path, "status": status, "reason": reason, "sha256": _sha256(path),
            "bad": bad, "empty_cells": empty_cells(d)}


def _sha256(p):
    try:
        with open(p, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    except OSError:                                              # pragma: no cover
        return ""


def empty_cells(table):
    """["high/under15", ...] - the cells of a rate table with no number in them."""
    rates = (table or {}).get("rates") or {}
    out = []
    for sev in LOSS.SEVERITIES:
        cell = rates.get(sev) if isinstance(rates.get(sev), dict) else {}
        for band in LOSS.WARNING_BANDS:
            if _num(cell.get(band)) is None:
                out.append("{}/{}".format(sev, band))
    return out


def status_text(table):
    """(usable, text): is this table a signed-off table that can produce a number?"""
    ok, text = LOSS.rate_table_status(table)
    if not ok:
        return False, text
    blank = empty_cells(table)
    if blank:
        return False, ("The fatality rate table is signed off but {} cell(s) are empty ({}), so "
                       "loss of life is not estimated: a total that leaves those people out "
                       "would be too low. Fill them in or remove the sign-off.".format(
                           len(blank), ", ".join(blank)))
    return True, text


# --------------------------------------------------------------- the estimate --
def rows_from_evaluate(structs, ev):
    """The rows damsuite.loss wants, from a consequences `evaluate` result.

    One row per structure with the depth, velocity and arrival the flood gave it and both
    occupant counts, written into the NSI field names loss.people reads (pop2amu65 = the night
    count, pop2pmu65 = the day count), so the same rows answer for day and for night."""
    rows = []
    for i, s in enumerate(structs or []):
        rows.append({"id": getattr(s, "ident", ""),
                     "depth_ft": _val(ev, "depth_ft", i),
                     "velocity_fts": _val(ev, "velocity_fps", i),
                     "arrival_hr": _val(ev, "arrival_hr", i),
                     "pop2amu65": float(getattr(s, "occupants_night", 0.0) or 0.0),
                     "pop2pmu65": float(getattr(s, "occupants_day", 0.0) or 0.0),
                     "habitable": bool((getattr(s, "occupants_night", 0.0) or 0.0) or
                                       (getattr(s, "occupants_day", 0.0) or 0.0))})
    return rows


def _val(ev, key, i):
    try:
        v = (ev or {}).get(key)
        return None if v is None else _num(v[i])
    except (IndexError, KeyError, TypeError):
        return None


def rows_from_csv_rows(rows):
    """The same rows from the consequences CSV rows (which keep one occupant count:
    `occupants_used`, the basis the run chose). Day and night are then the same number and the
    report says so."""
    out = []
    for r in rows or []:
        occ = _num(r.get("occupants_used")) or 0.0
        out.append({"id": r.get("id", ""), "depth_ft": _num(r.get("depth_ft")),
                    "velocity_fts": _num(r.get("velocity_fps")),
                    "arrival_hr": _num(r.get("arrival_hr")),
                    "pop": occ, "habitable": occ > 0})
    return out


def assess(rows, table=None, one_count=False):
    """PAR, severity and warning splits for day and night, and loss of life only when a
    complete signed-off table is given.

    Returns {"day", "night", "rates_ok", "rates_text", "fatalities" ({"day", "night"} or None),
    "one_count", "empty_cells", "table"} - "day" / "night" are damsuite.loss estimates."""
    table = table or {}
    usable, text = status_text(table)
    pair = LOSS.both_times(rows, table if usable else None)
    fatal = None
    if usable:
        fatal = {k: pair[k].get("fatalities") for k in ("day", "night")}
        if any(v is None for v in fatal.values()):
            fatal = None
    return {"day": pair["day"], "night": pair["night"], "rates_ok": usable, "rates_text": text,
            "fatalities": fatal, "one_count": bool(one_count),
            "empty_cells": empty_cells(table), "table": {
                k: table.get(k) for k in ("status", "signed_off_by", "signed_off_on",
                                          "checked_by", "source", "path") if table.get(k)}}


def cell_rows(est):
    """[{"severity", "warning", "structures", "par", "rate", "fatalities"}] of one estimate's
    severity x warning cells, worst first - the matrix a report prints."""
    out = []
    cells = (est or {}).get("cells") or {}
    for (sev, band), cell in cells.items():
        out.append({"severity": sev, "warning": band,
                    "warning_label": LOSS.WARNING_LABELS.get(band, band),
                    "structures": cell.get("structures") or 0,
                    "par": round(float(cell.get("par") or 0.0), 1),
                    "rate": cell.get("rate"), "fatalities": cell.get("fatalities")})
    out.sort(key=lambda r: (LOSS.SEVERITIES.index(r["severity"]) if r["severity"] in
                            LOSS.SEVERITIES else 9,
                            LOSS.WARNING_BANDS.index(r["warning"]) if r["warning"] in
                            LOSS.WARNING_BANDS else 9))
    return out


CSV_COLUMNS = ("basis", "severity", "warning_band", "warning_time", "structures", "par",
               "fatality_rate", "loss_of_life")


def csv_rows(assessment):
    """The rows of Population at Risk (PAR).csv: the cells of the day and the night estimate."""
    rows = []
    for basis in ("day", "night"):
        for r in cell_rows((assessment or {}).get(basis)):
            rows.append({"basis": basis, "severity": r["severity"],
                         "warning_band": r["warning"], "warning_time": r["warning_label"],
                         "structures": r["structures"], "par": r["par"],
                         "fatality_rate": "" if r["rate"] is None else r["rate"],
                         "loss_of_life": "" if r["fatalities"] is None
                         else round(r["fatalities"], 3)})
    return rows


def markdown_lines(assessment, title="Population at risk (PAR), severity and warning time"):
    """The report section. PAR and the splits always; loss of life only when it is allowed."""
    if not assessment:
        return []
    lines = ["## " + title, ""]
    for basis in ("night", "day"):
        est = assessment.get(basis) or {}
        lines.append("- {}: PAR {:.0f} in {} flooded structure(s)".format(
            basis.capitalize(), est.get("par") or 0.0, est.get("wet") or 0))
        sev = est.get("by_severity") or {}
        lines.append("  - by flood severity: " + ", ".join(
            "{} {:.0f}".format(s, sev.get(s) or 0.0) for s in LOSS.SEVERITIES))
        band = est.get("by_band") or {}
        lines.append("  - by warning time: " + ", ".join(
            "{} {:.0f}".format(LOSS.WARNING_LABELS[b], band.get(b) or 0.0)
            for b in LOSS.WARNING_BANDS))
    if assessment.get("one_count"):
        lines.append("- The structure layer carried one occupant count, so the day and the "
                     "night figures are the same number; give day and night population fields "
                     "(NSI pop2pm* / pop2am*) to tell them apart.")
    lines.append("")
    if assessment.get("fatalities") is None:
        lines.append("- " + NOT_ESTIMATED + " " + (assessment.get("rates_text") or ""))
    else:
        f = assessment["fatalities"]
        lines.append("- Loss of life with the signed-off rate table: night {:.1f}, day "
                     "{:.1f}.".format(f.get("night") or 0.0, f.get("day") or 0.0))
        lines.append("- " + (assessment.get("rates_text") or ""))
    lines.append("- Severity and warning bands: {}".format(_bands_note()))
    lines.append("")
    return lines


def _bands_note():
    return ("flood severity high = depth x velocity >= {:g} ft2/s or depth >= {:g} ft, medium "
            "= depth x velocity >= {:g} ft2/s, low = wet above {:g} ft; warning time from the "
            "flood's arrival at the structure (under 15 min / 15 to 60 / over 60). These are "
            "the suite's shared bands (damsuite.loss), not the consequences tool's own "
            "severity and warning parameters.".format(
                LOSS.DV_HIGH, LOSS.DEPTH_HIGH_FT, LOSS.DV_MEDIUM, LOSS.COUNT_DEPTH_FT))


def markdown_from_summary(d, title="Population at risk (PAR), severity and warning time"):
    """The same section rendered from the JSON-friendly `summary` dict, for a report that keeps
    the summary rather than the estimate objects (the consequences tool does)."""
    if not d:
        return []
    if d.get("problem"):
        return ["## " + title, "",
                "- Not worked out: {}".format(d["problem"]), ""]
    lines = ["## " + title, ""]
    for basis in ("night", "day"):
        est = d.get(basis) or {}
        lines.append("- {}: PAR {:g} in {} flooded structure(s)".format(
            basis.capitalize(), est.get("par") or 0.0, est.get("structures_flooded") or 0))
        sev = est.get("by_severity") or {}
        lines.append("  - by flood severity: " + ", ".join(
            "{} {:g}".format(s, sev.get(s) or 0.0) for s in LOSS.SEVERITIES))
        band = est.get("by_warning") or {}
        lines.append("  - by warning time: " + ", ".join(
            "{} {:g}".format(LOSS.WARNING_LABELS[b], band.get(b) or 0.0)
            for b in LOSS.WARNING_BANDS))
    if d.get("one_count"):
        lines.append("- The structure layer carried one occupant count, so day and night are "
                     "the same number here.")
    lol = d.get("loss_of_life")
    if lol is None:
        lines.append("- " + NOT_ESTIMATED + " " + (d.get("rates_text") or ""))
    else:
        lines.append("- Loss of life with the signed-off rate table: night {:.1f}, day "
                     "{:.1f}. {}".format(lol.get("night") or 0.0, lol.get("day") or 0.0,
                                         d.get("rates_text") or ""))
    lines.append("- Bands: " + (d.get("bands") or _bands_note()))
    lines.append("")
    return lines


def write_outputs(folder, assessment, title=""):
    """Population at Risk (PAR).csv / .json / .md in the dam folder. Returns {kind: path}."""
    out = {}
    os.makedirs(folder, exist_ok=True)
    rows = csv_rows(assessment)
    p = os.path.join(folder, OUTPUT_NAMES["csv"])
    with open(p, "w", newline="", encoding="utf-8") as fh:
        fh.write(",".join(CSV_COLUMNS) + "\n")
        for r in rows:
            fh.write(",".join(_cell(r.get(c, "")) for c in CSV_COLUMNS) + "\n")
    out["csv"] = p
    p = os.path.join(folder, OUTPUT_NAMES["json"])
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(summary(assessment), fh, indent=1, default=str)
    out["json"] = p
    p = os.path.join(folder, OUTPUT_NAMES["markdown"])
    with open(p, "w", encoding="utf-8") as fh:
        fh.write("\n".join(markdown_lines(assessment, title or
                                          "Population at risk (PAR), severity and warning "
                                          "time")))
    out["markdown"] = p
    return out


def _cell(v):
    s = "" if v is None else str(v)
    return '"{}"'.format(s.replace('"', '""')) if ("," in s or '"' in s) else s


def summary(assessment):
    """The JSON summary: the two estimates without their per-cell dict keys (tuples)."""
    out = {"rates_ok": bool((assessment or {}).get("rates_ok")),
           "one_count": bool((assessment or {}).get("one_count")),
           "rates_text": (assessment or {}).get("rates_text") or "",
           "loss_of_life": (assessment or {}).get("fatalities"),
           "empty_rate_cells": (assessment or {}).get("empty_cells") or [],
           "table": (assessment or {}).get("table") or {},
           "bands": _bands_note(), "cells": csv_rows(assessment)}
    for basis in ("day", "night"):
        est = (assessment or {}).get(basis) or {}
        out[basis] = {"par": round(float(est.get("par") or 0.0), 1),
                      "structures_flooded": est.get("wet") or 0,
                      "by_severity": {k: round(float(v or 0.0), 1)
                                      for k, v in (est.get("by_severity") or {}).items()},
                      "by_warning": {k: round(float(v or 0.0), 1)
                                     for k, v in (est.get("by_band") or {}).items()},
                      "worst_severity": est.get("worst") or ""}
    return out
