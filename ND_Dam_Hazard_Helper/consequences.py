# -*- coding: utf-8 -*-
"""Screening-level consequence estimates per structure: economic loss
(value exposed and residential depth-damage) and loss of life (Graham
1999 fatality rates by flood severity and warning time).

Sources (tables transcribed from the published documents):
- USACE EGM 01-03 (2000), "Generic Depth-Damage Relationships", Tables 1-6:
  residential structure and content damage (% of the STRUCTURE value, the
  content curves included) for one-story,
  two-or-more-story and split-level houses without basement, depth above
  the first floor -2 ... 16 ft.
- Graham, W.J. (1999), USBR DSO-99-06, "A Procedure for Estimating Loss of
  Life Caused by Dam Failure", Table 7 (recommended fatality rates) and the
  severity definitions on pp. 35-36 (DV < 50 ft2/s low, otherwise medium;
  high only for near-instantaneous failures; without velocities: < 10 ft
  deep low, otherwise medium).
- USBR (2015) RCEM interim guidelines, p. 26: severity bands DV < 50 low,
  50 - 160 medium, > 160 high (optional "RCEM" severity setting).

These are screening numbers for the engineer, not a risk analysis.
"""

import re

import numpy as np

EGM_DEPTHS = np.arange(-2, 17, dtype=np.float64)          # ft above first floor
EGM_0103 = {
    # (structure %, content %) Tables 1-6
    "one": ([0, 2.5, 13.4, 23.3, 32.1, 40.1, 47.1, 53.2, 58.6, 63.2, 67.2, 70.5, 73.2, 75.4,
             77.2, 78.5, 79.5, 80.2, 80.7],
            [0, 2.4, 8.1, 13.3, 17.9, 22.0, 25.7, 28.8, 31.5, 33.8, 35.7, 37.2, 38.4, 39.2,
             39.7, 40.0, 40.0, 40.0, 40.0]),
    "two": ([0, 3.0, 9.3, 15.2, 20.9, 26.3, 31.4, 36.2, 40.7, 44.9, 48.8, 52.4, 55.7, 58.7,
             61.4, 63.8, 65.9, 67.7, 69.2],
            [0, 1.0, 5.0, 8.7, 12.2, 15.5, 18.5, 21.3, 23.9, 26.3, 28.4, 30.3, 32.0, 33.4,
             34.7, 35.6, 36.4, 36.9, 37.2]),
    "split": ([0, 6.4, 7.2, 9.4, 12.9, 17.4, 22.8, 28.9, 35.5, 42.3, 49.2, 56.1, 62.6, 68.6,
               73.9, 78.4, 81.7, 83.8, 84.4],
              [0, 2.2, 2.9, 4.7, 7.5, 11.1, 15.3, 20.1, 25.2, 30.5, 35.7, 40.9, 45.8, 50.2,
               54.1, 57.2, 59.4, 60.5, 60.5]),
}
EGM_LABELS = {"one": "EGM 01-03 one-story", "two": "EGM 01-03 two-or-more-story",
              "split": "EGM 01-03 split-level"}

# Graham (1999) Table 7: (rate, low, high)
SEV_LOW, SEV_MEDIUM, SEV_HIGH = "Low", "Medium", "High"
WARN_NONE, WARN_15_60, WARN_60 = "none (<15 min)", "15-60 min", ">60 min"
GRAHAM_RATES = {
    (SEV_HIGH, WARN_NONE, None): (0.75, 0.30, 1.00),
    (SEV_HIGH, WARN_15_60, None): (0.75, 0.30, 1.00),     # applied to people remaining
    (SEV_HIGH, WARN_60, None): (0.75, 0.30, 1.00),
    (SEV_MEDIUM, WARN_NONE, None): (0.15, 0.03, 0.35),
    (SEV_MEDIUM, WARN_15_60, "vague"): (0.04, 0.01, 0.08),
    (SEV_MEDIUM, WARN_15_60, "precise"): (0.02, 0.005, 0.04),
    (SEV_MEDIUM, WARN_60, "vague"): (0.03, 0.005, 0.06),
    (SEV_MEDIUM, WARN_60, "precise"): (0.01, 0.002, 0.02),
    (SEV_LOW, WARN_NONE, None): (0.01, 0.0, 0.02),
    (SEV_LOW, WARN_15_60, "vague"): (0.007, 0.0, 0.015),
    (SEV_LOW, WARN_15_60, "precise"): (0.002, 0.0, 0.004),
    (SEV_LOW, WARN_60, "vague"): (0.0003, 0.0, 0.0006),
    (SEV_LOW, WARN_60, "precise"): (0.0002, 0.0, 0.0004),
}
GRAHAM_REF = "Graham (1999) USBR DSO-99-06 Table 7"
EGM_REF = "USACE EGM 01-03 generic residential depth-damage (no basement)"
DV_MEDIUM = 50.0           # ft2/s
DV_HIGH_RCEM = 160.0       # ft2/s
DEPTH_MEDIUM = 10.0        # ft (Graham rule when DV is unknown)

# NSI residential codes: RES1-1SNB, RES1-2SWB, RES1-3SNB, RES1-SLNB, RES1-SLWB
_RES1 = re.compile(r"^RES1-(?:(?P<st>\d)S|(?P<sl>SL))(?P<b>NB|WB)", re.I)


def curve_for_occtype(occ):
    """('one'|'two'|'split', basement?) for an NSI RES1 occupancy code;
    (None, False) for anything without a generic curve."""
    m = _RES1.match(str(occ or "").strip())
    if not m:
        return None, False
    key = "split" if m.group("sl") else ("one" if m.group("st") == "1" else "two")
    return key, m.group("b").upper() == "WB"


def damage_percent(key, depth_ft):
    """(structure %, content %) at `depth_ft` above the first floor."""
    s, c = EGM_0103[key]
    d = float(np.clip(depth_ft, EGM_DEPTHS[0], EGM_DEPTHS[-1]))
    if depth_ft < EGM_DEPTHS[0]:
        return 0.0, 0.0
    return float(np.interp(d, EGM_DEPTHS, s)), float(np.interp(d, EGM_DEPTHS, c))


def _num(v):
    try:
        f = float(v)
        return f if np.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def economic_estimate(rec, curves=None):
    """Value exposed and depth-damage for one structure (in place). Uses the
    NSI units merged into the record; nothing for dry records. Residential
    (RES1) units use the built-in EGM 01-03 curves (damage_usd). Other
    occupancies use the engineer's own curves (`curves`, damage_curves.py) when
    given (damage_nonres_usd); without them only their value exposed is
    reported."""
    units = rec.get("nsi_units") or []
    rec.update({"val_struct": None, "val_cont": None, "ff_depth": None,
                "damage_usd": None, "damage_basis": "", "damage_nonres_usd": None})
    if not units:
        return rec
    vs = sum(_num(u.get("val_struct")) or 0.0 for u in units)
    vc = sum(_num(u.get("val_cont")) or 0.0 for u in units)
    rec["val_struct"], rec["val_cont"] = (vs or None), (vc or None)
    depth = rec.get("max_depth")
    if depth is None or depth <= 0:
        return rec
    total, bases, ffs, missing = 0.0, set(), [], 0
    nonres, nonres_used = 0.0, set()
    for u in units:
        key, basement = curve_for_occtype(u.get("occtype"))
        fh = _num(u.get("found_ht"))
        ff = depth - (fh if fh is not None else 0.0)
        ffs.append(ff)
        if key is None:
            if curves is not None:
                usd, prefix = curves.damage(u.get("occtype"), ff, _num(u.get("val_struct")),
                                            _num(u.get("val_cont")))
                if prefix is not None:
                    nonres += usd
                    nonres_used.add(prefix)
                    if fh is None:
                        bases.add("foundation height unknown (0 ft used)")
                    continue
            missing += 1
            continue
        ps, pc = damage_percent(key, ff)
        vstruct = _num(u.get("val_struct")) or 0.0
        vcont = _num(u.get("val_cont"))
        # EGM 01-03 content damage is a percent of the STRUCTURE value;
        # it cannot exceed the content value when NSI gives one
        cdmg = vstruct * pc / 100.0
        if vcont is not None:
            cdmg = min(cdmg, vcont)
        total += vstruct * ps / 100.0 + cdmg
        bases.add(EGM_LABELS[key] + (" (basement: no-basement curve used)" if basement else ""))
        if fh is None:
            bases.add("foundation height unknown (0 ft used)")
    rec["ff_depth"] = max(ffs) if ffs else None
    if any(b.startswith("EGM") for b in bases):
        rec["damage_usd"] = total
    if nonres_used:
        rec["damage_nonres_usd"] = nonres
        bases.add("non-residential: {} curve(s) {} (supplied by the engineer)".format(
            curves.source or "file", "/".join(sorted(nonres_used))))
    if missing:
        bases.add("{} unit(s) without a depth-damage curve: value exposed only".format(missing))
    rec["damage_basis"] = "; ".join(sorted(bases))
    return rec


def flood_severity(dv, depth, method="graham"):
    """Low / Medium / High flood severity for Graham (1999) rates."""
    if dv is not None and np.isfinite(dv):
        if method == "rcem" and dv > DV_HIGH_RCEM:
            return SEV_HIGH, "DV {:.0f} ft2/s > {:.0f} (RCEM)".format(dv, DV_HIGH_RCEM)
        if dv >= DV_MEDIUM:
            return SEV_MEDIUM, "DV {:.0f} ft2/s >= {:.0f}".format(dv, DV_MEDIUM)
        return SEV_LOW, "DV {:.0f} ft2/s < {:.0f}".format(dv, DV_MEDIUM)
    if depth is not None and depth >= DEPTH_MEDIUM:
        return SEV_MEDIUM, "depth {:.1f} ft >= 10 (no velocity)".format(depth)
    return SEV_LOW, "depth {:.1f} ft < 10 (no velocity)".format(depth or 0.0)


def warning_category(arrival_hr, warning_issued_hr):
    if arrival_hr is None or not np.isfinite(arrival_hr):
        return WARN_NONE, None
    wt = float(arrival_hr) - float(warning_issued_hr or 0.0)
    if wt < 0.25:
        return WARN_NONE, wt
    if wt <= 1.0:
        return WARN_15_60, wt
    return WARN_60, wt


def graham_rate(severity, warning, understanding="vague"):
    und = None if severity == SEV_HIGH or warning == WARN_NONE else \
        ("precise" if understanding == "precise" else "vague")
    return GRAHAM_RATES[(severity, warning, und)]


def population_at_risk(rec, period="night"):
    """Occupants for the loss-of-life estimate: NSI population of the time
    of day when available, otherwise the TM 11 persons."""
    if rec.get("persons_basis") == "engineer review":
        return float(rec.get("persons") or 0), "engineer review"
    pop = rec.get("pop_night") if period == "night" else rec.get("pop_day")
    if pop is not None:                     # an NSI population of 0 is a real 0
        return float(pop), "NSI {} population".format(period)
    return float(rec.get("persons") or 0), rec.get("persons_basis") or ""


def life_loss_estimate(rec, warning_issued_hr=0.0, understanding="vague",
                       severity_method="graham", period="night", high_severity=False):
    """Fatality rate and expected fatalities for one wet, habitable structure
    (in place). high_severity: treat the location as Graham's HIGH severity
    (near-instantaneous failure) regardless of DV."""
    rec.update({"severity": None, "warning_hr": None, "fatality_rate": None,
                "lol": None, "lol_low": None, "lol_high": None, "par": None,
                "par_night": None, "par_day": None, "lol_night": None, "lol_day": None})
    if rec.get("habitable") != "Yes" or not rec.get("max_depth") or rec["max_depth"] <= 0:
        return rec
    sev, why = flood_severity(rec.get("dv_max"), rec.get("max_depth"), severity_method)
    if high_severity:
        sev, why = SEV_HIGH, "near-instantaneous failure (user setting)"
    warn, wt = warning_category(rec.get("arrival"), warning_issued_hr)
    rate, lo, hi = graham_rate(sev, warn, understanding)
    par, _basis = population_at_risk(rec, period)
    # v2.7: both times of day, whatever the setting (same severity and rate)
    p_night, _b = population_at_risk(rec, "night")
    p_day, _b = population_at_risk(rec, "day")
    rec.update({"severity": "{} ({})".format(sev, why), "warning_hr": wt, "warning_cat": warn,
                "fatality_rate": rate, "par": par, "lol": par * rate, "lol_low": par * lo,
                "lol_high": par * hi, "par_night": p_night, "par_day": p_day,
                "lol_night": p_night * rate, "lol_day": p_day * rate})
    return rec


def totals(structs):
    """Sums for the summary."""
    wet = [s for s in structs if s.get("max_depth") and s["max_depth"] > 0]
    camps = [s for s in wet if s.get("camp") is not None]
    t = {"par": sum(s.get("par") or 0 for s in wet),
         "lol": sum(s.get("lol") or 0 for s in wet),
         "lol_low": sum(s.get("lol_low") or 0 for s in wet),
         "lol_high": sum(s.get("lol_high") or 0 for s in wet),
         # v2.7: both times of day, and the seasonal (campground / RV park) share
         "par_night": sum(s.get("par_night") or 0 for s in wet),
         "par_day": sum(s.get("par_day") or 0 for s in wet),
         "lol_night": sum(s.get("lol_night") or 0 for s in wet),
         "lol_day": sum(s.get("lol_day") or 0 for s in wet),
         "camps": sum(1 for s in camps if s.get("habitable") == "Yes"),
         "par_camps_night": sum(s.get("par_night") or 0 for s in camps),
         "par_camps_day": sum(s.get("par_day") or 0 for s in camps),
         "lol_camps_night": sum(s.get("lol_night") or 0 for s in camps),
         "lol_camps_day": sum(s.get("lol_day") or 0 for s in camps),
         "value_exposed": sum((s.get("val_struct") or 0) + (s.get("val_cont") or 0)
                              for s in wet),
         "damage": sum(s.get("damage_usd") or 0 for s in wet),
         "damage_structures": sum(1 for s in wet if s.get("damage_usd") is not None),
         "damage_nonres": sum(s.get("damage_nonres_usd") or 0 for s in wet),
         "damage_nonres_structures": sum(1 for s in wet
                                         if s.get("damage_nonres_usd") is not None),
         "wet_with_values": sum(1 for s in wet if s.get("val_struct"))}
    return t
