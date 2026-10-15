"""Two loading cases in one run (0.28.0): top of dam and sunny day, and which class governs.

A screening run did one scenario, the one the Settings tab was set to - normally "td", the top of
dam (ND 5.1.1: start at the top of dam, no added inflow). The other one, "sd" (sunny day: the
dam fails with the pool at normal level, fair weather, nobody watching the river), is a DIFFERENT
loading case, not a cheaper version of the first, and it is the one that often governs the class:
the top-of-dam flood arrives on top of a river that is already in flood and already dangerous,
while the sunny-day flood arrives on a dry day on a road people are driving on. The consequences
that count are the INCREMENTAL ones - what the breach adds to what would have happened anyway -
so the class a dam deserves is the WORSE of the two, and running only one of them can only ever
miss consequences.

    gov = governing({"td": "Medium", "sd": "High"})
    gov["hazard_class"] == "High"      # and gov["why"] says which scenario and why

Decisions:
  * The governing class is the higher of the two, never an average and never the primary one by
    preference: the suite's rule is that nothing may move a result in the less-conservative
    direction, and ND 5.3 already says a borderline case takes the higher class.
  * A tie goes to the primary scenario (td) so the reason text stays the familiar one.
  * A missing or unrecognised class is NOT treated as Low. A scenario that did not run leaves the
    other one governing on its own, and the fact is said in the text - "sunny day did not run" is
    a gap to fix, not a class.
  * Both scenarios double the flood model, so `both_default` is on for a one-dam run (a minute
    becomes two) and off for a county (hours become days). The engineer can set either.
Pure Python standard library.
"""

PRIMARY = "td"
SECOND = "sd"
ORDER = (PRIMARY, SECOND)
LABEL = {"td": "top of dam (ND standard)", "sd": "sunny day (normal pool, fair weather)"}
SHORT = {"td": "top of dam", "sd": "sunny day"}
# the hazard classes, worst last (core/nd_hazard.RANK, copied so this module stays standard library)
RANK = {"Low": 0, "Medium": 1, "Significant": 1, "High": 2}
INCREMENTAL_RULE = ("ND: the class follows the INCREMENTAL consequences of a breach, so the "
                    "worse of the loading cases governs (the sunny-day flood arrives on a dry "
                    "day; the top-of-dam flood arrives on a river already in flood).")


def both_default(one_dam):
    """Whether "run both scenarios" starts on: yes for one dam, no for a county."""
    return bool(one_dam)


def rank(cls):
    """0 / 1 / 2 for a class, or None when it is empty or not a class."""
    t = str(cls or "").strip()
    if not t:
        return None
    if t in RANK:
        return RANK[t]
    for name, r in RANK.items():
        if t.lower().startswith(name.lower()):
            return r
    return None


def governing(classes):
    """Which scenario's class the dam gets.

    `classes`: {"td": class, "sd": class} - a missing, empty or unrecognised value means that
    scenario has no class. Returns {"hazard_class", "scenario", "classes", "ran", "raised",
    "why"}; hazard_class is "" when neither scenario gave one."""
    got = {k: str((classes or {}).get(k) or "").strip() for k in ORDER}
    ranked = [(k, got[k], rank(got[k])) for k in ORDER if rank(got[k]) is not None]
    out = {"hazard_class": "", "scenario": "", "classes": got,
           "ran": [k for k in ORDER if got[k]], "raised": False, "why": ""}
    if not ranked:
        out["why"] = "no scenario produced a class"
        return out
    # the worse class wins; a tie keeps the primary scenario (ranked is in ORDER)
    top = max(r[2] for r in ranked)
    best = next(r for r in ranked if r[2] == top)
    out["hazard_class"], out["scenario"] = best[1], best[0]
    if len(ranked) == 1:
        other = SECOND if best[0] == PRIMARY else PRIMARY
        out["why"] = "{}: {} ({} gave no class)".format(SHORT[best[0]], best[1], SHORT[other])
        return out
    lo = min(ranked, key=lambda r: r[2])
    out["raised"] = lo[2] < top
    if out["raised"]:
        out["why"] = ("{} governs: {} ({} would be {}). ".format(
            SHORT[best[0]], best[1], SHORT[lo[0]], lo[1]) + INCREMENTAL_RULE)
    else:
        out["why"] = "both scenarios give {}".format(best[1])
    return out


def note(gov):
    """The sentence for the dam's hz_notes / report ("" when there is nothing to add)."""
    if not gov or not gov.get("hazard_class"):
        return ""
    if gov.get("raised"):
        return "two loading cases run; " + gov["why"]
    if len(gov.get("ran") or []) > 1:
        return "two loading cases run (top of dam and sunny day); " + gov["why"]
    return ""


def pairs(settings):
    """The scenarios one run should model, in order: the chosen one first, then the other when
    "both scenarios" is on. The window's settings: "scenario" (0 td, 1 sd), "both_scenarios"."""
    S = settings or {}
    try:
        first = ORDER[int(S.get("scenario") or 0)]
    except (TypeError, ValueError, IndexError):
        first = PRIMARY
    if not S.get("both_scenarios"):
        return (first,)
    return (first,) + tuple(k for k in ORDER if k != first)
