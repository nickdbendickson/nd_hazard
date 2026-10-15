"""Population at risk, flood severity and warning time (1.16.0).

What this gives you, per structure and for the dam:
  * PAR - the people in the flood, day and night, from the structure layer's own population
    fields (NSI carries pop2amu65 / pop2amo65 / pop2pmu65 / pop2pmo65).
  * Flood severity - "high", "medium" or "low" from depth and velocity at the structure, the
    usual DV product bands.
  * Warning time - from the flood's arrival time at the structure, in the bands the loss-of-life
    literature uses (under 15 minutes, 15 to 60, over 60).

What this deliberately does NOT do by itself: turn that into a number of deaths. A fatality rate
table (Graham / RCEM style) is an engineering judgement that belongs to a named engineer, not to
a plugin's source code, and this suite already has the pattern for that - the ACER TM 11 charts
are a signed-off data file, and nothing uses them until an engineer signs them. So:

    est = estimate(rows)                      # PAR, severity and warning bands: always
    est["fatalities"] is None                 # until a rate table is signed off
    est = estimate(rows, rates=signed_table)  # then the bands carry rates and it is filled in

`rate_table_status()` says whether a table has been signed off, and the windows show PAR with
"no fatality rate table has been signed off; loss of life is not estimated" until it is. A rate
table is {("high"|"medium"|"low", "under15"|"15to60"|"over60"): fraction} plus "signed_off_by"
and "source", written by the same sign-off tool as the charts.

Severity bands (depth in feet, velocity in feet per second, DV = depth x velocity):
    high    DV >= 160, or depth >= 13 ft     - a house is destroyed or swept
    medium  DV >= 40                         - a house is damaged, people cannot stand
    low     anything wet above the counting depth
These are the same thresholds the ND rule's judgement zone uses at its upper edge; they are
constants here so a reviewer can see and change them in one place.

Pure Python standard library.
"""
SEVERITIES = ("high", "medium", "low")
WARNING_BANDS = ("under15", "15to60", "over60")
WARNING_LABELS = {"under15": "under 15 minutes", "15to60": "15 to 60 minutes",
                  "over60": "over 60 minutes"}
WARNING_MINUTES = {"under15": (0.0, 15.0), "15to60": (15.0, 60.0), "over60": (60.0, None)}
# severity thresholds (feet, feet per second)
DV_HIGH, DV_MEDIUM = 160.0, 40.0
DEPTH_HIGH_FT = 13.0
COUNT_DEPTH_FT = 0.1              # the suite's consequence depth; below it a structure is dry
# NSI population fields: (day fields, night fields)
NSI_DAY = ("pop2pmu65", "pop2pmo65")
NSI_NIGHT = ("pop2amu65", "pop2amo65")
# fallback people per habitable structure when the layer carries no population at all
DEFAULT_PEOPLE = 2.5


def _f(v, default=None):
    try:
        if v is None or v == "":
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def severity(depth_ft, velocity_fts):
    """"high" | "medium" | "low" | "" (dry) for one structure."""
    d = _f(depth_ft, 0.0) or 0.0
    v = _f(velocity_fts, 0.0) or 0.0
    if d < COUNT_DEPTH_FT:
        return ""
    if d * v >= DV_HIGH or d >= DEPTH_HIGH_FT:
        return "high"
    if d * v >= DV_MEDIUM:
        return "medium"
    return "low"


def warning_band(arrival_minutes):
    """Which warning band an arrival time falls in ("" when there is no arrival time).

    The warning time is taken as the flood's arrival time at the structure: the dam fails at
    time zero and nobody is warned before it. A real EAP with gauges and a siren gives more; a
    caller that knows the warning time can pass it instead of the arrival time."""
    m = _f(arrival_minutes)
    if m is None:
        return ""
    if m < 15.0:
        return "under15"
    if m < 60.0:
        return "15to60"
    return "over60"


def people(row, when="night", default=DEFAULT_PEOPLE):
    """People in one structure, day or night, from its population fields.

    Falls back to `default` for a habitable structure with no population field, and to 0 for a
    structure that is not habitable."""
    fields = NSI_DAY if when == "day" else NSI_NIGHT
    total, found = 0.0, False
    for f in fields:
        v = _f(row.get(f))
        if v is not None:
            total += v
            found = True
    if found:
        return total
    for f in ("pop", "population", "POP"):
        v = _f(row.get(f))
        if v is not None:
            return v
    habitable = row.get("habitable")
    if habitable is None:
        habitable = row.get("is_habitable", True)
    return float(default) if habitable else 0.0


def row_risk(row, when="night"):
    """{"severity", "warning", "par", "depth_ft", "velocity_fts", "arrival_min"} for one
    flooded structure row (a dict of the structure layer's fields)."""
    d = _f(row.get("depth_ft"), _f(row.get("depth")))
    v = _f(row.get("velocity_fts"), _f(row.get("velocity")))
    a = _f(row.get("arrival_min"), _f(row.get("arrival_minutes")))
    if a is None:
        hours = _f(row.get("arrival_hr"), _f(row.get("arrival_hours")))
        a = hours * 60.0 if hours is not None else None
    sev = severity(d, v)
    return {"severity": sev, "warning": warning_band(a),
            "par": people(row, when) if sev else 0.0,
            "depth_ft": d, "velocity_fts": v, "arrival_min": a}


# ------------------------------------------------------------- the rate table --
def rate_table_status(rates=None):
    """(ok, text) - whether a fatality rate table has been signed off."""
    if not rates:
        return False, ("No fatality rate table has been signed off, so loss of life is not "
                       "estimated. Population at risk, flood severity and warning time are "
                       "reported instead. An engineer enters and signs a rate table in "
                       "Suite settings > Loss of life.")
    by = (rates.get("signed_off_by") or "").strip() if isinstance(rates, dict) else ""
    if not by:
        return False, ("A fatality rate table is present but nobody has signed it off; it is "
                       "not used. Sign it off in Suite settings > Loss of life.")
    src = (rates.get("source") or "").strip()
    return True, "Fatality rates signed off by {}{}.".format(by, " (" + src + ")" if src else "")


def _rate(rates, sev, band):
    """The signed-off fatality rate for a (severity, warning band) cell, or None."""
    if not isinstance(rates, dict):
        return None
    table = rates.get("rates") if isinstance(rates.get("rates"), dict) else rates
    for key in ((sev, band), "{}/{}".format(sev, band), "{}|{}".format(sev, band)):
        if key in table:
            return _f(table[key])
    cell = table.get(sev)
    if isinstance(cell, dict):
        return _f(cell.get(band))
    return None


# -------------------------------------------------------------- the estimate --
def estimate(rows, when="night", rates=None):
    """Roll `rows` (flooded structure dicts) up into:

        {"par", "wet", "by_severity", "by_band", "cells", "fatalities", "rates_ok",
         "rates_text", "when", "worst"}

    `cells` is {(severity, band): {"par", "structures", "rate", "fatalities"}}. "fatalities" is
    None for the whole estimate until a rate table is signed off."""
    cells, by_sev, by_band = {}, {sev: 0.0 for sev in SEVERITIES}, \
        {b: 0.0 for b in WARNING_BANDS}
    par, wet, worst = 0.0, 0, ""
    for r in rows or []:
        got = row_risk(r, when)
        if not got["severity"]:
            continue
        wet += 1
        par += got["par"]
        by_sev[got["severity"]] = by_sev.get(got["severity"], 0.0) + got["par"]
        band = got["warning"] or "over60"
        by_band[band] = by_band.get(band, 0.0) + got["par"]
        cell = cells.setdefault((got["severity"], band),
                                {"par": 0.0, "structures": 0, "rate": None, "fatalities": None})
        cell["par"] += got["par"]
        cell["structures"] += 1
        if SEVERITIES.index(got["severity"]) < (SEVERITIES.index(worst) if worst else 9):
            worst = got["severity"]
    ok, text = rate_table_status(rates)
    total = None
    if ok:
        total = 0.0
        for (sev, band), cell in cells.items():
            rate = _rate(rates, sev, band)
            cell["rate"] = rate
            if rate is None:
                cell["fatalities"] = None
                total = None if total is None else total
                continue
            cell["fatalities"] = cell["par"] * rate
            if total is not None:
                total += cell["fatalities"]
    return {"par": par, "wet": wet, "by_severity": by_sev, "by_band": by_band, "cells": cells,
            "fatalities": total, "rates_ok": ok, "rates_text": text, "when": when,
            "worst": worst}


def estimate_text(est):
    """The lines for a report section."""
    lines = ["Population at risk ({}): {:.0f} in {} flooded structure(s)".format(
        est.get("when") or "night", est.get("par") or 0.0, est.get("wet") or 0)]
    sev = est.get("by_severity") or {}
    lines.append("  by flood severity: " + ", ".join(
        "{} {:.0f}".format(s, sev.get(s) or 0.0) for s in SEVERITIES))
    band = est.get("by_band") or {}
    lines.append("  by warning time: " + ", ".join(
        "{} {:.0f}".format(WARNING_LABELS[b], band.get(b) or 0.0) for b in WARNING_BANDS))
    if est.get("fatalities") is None:
        lines.append("  loss of life: not estimated. " + (est.get("rates_text") or ""))
    else:
        lines.append("  loss of life (signed-off rates): {:.1f}".format(est["fatalities"]))
        lines.append("  " + (est.get("rates_text") or ""))
    return lines


def both_times(rows, rates=None):
    """The estimate for day and for night - the pair a report should carry, since the day /
    night difference is often what decides whether anybody is home."""
    return {"day": estimate(rows, "day", rates), "night": estimate(rows, "night", rates)}
