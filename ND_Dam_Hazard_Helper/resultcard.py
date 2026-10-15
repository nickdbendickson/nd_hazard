# -*- coding: utf-8 -*-
"""2.30.0: the plain-language result card - what a dam owner or a county emergency manager
reads first, at the top of the Review tab and at the top of the report.

A classification report answers "which class" in its first line and then gives twelve pages of
numbers. The two questions the people who receive it actually ask are not answered anywhere in
one place:

    Why is it this class?         ONE reason, named: "High: 2 BNSF railroad stretches,
                                  15 trains/day - no other criterion makes it High"
    What would change it?         the margin: "Medium if those train counts were under 10 a
                                  day" or "High if the depth at 1234 Main St rose 0.70 ft"

So this module reduces the result to a card: the class, the one reason it is that class, what
would change it, the flags that qualify it ("flood cut off: duration cap") and a handful of
numbers underneath. The numbers are the ones a county emergency manager uses (structures,
people, roads, lifelines), not the ones an engineer argues about.

Decisions:
  * The reason is built from the COUNTS the classification produced, not by parsing its reason
    sentences: the counts are what the criteria were decided on, and a parsed sentence would
    silently change whenever the wording does.
  * "What would change it" names the criterion that alone decides the class. When that is a
    structure depth, the sentence comes from robustness.analyse (the margin line the report has
    carried since v2.5); when it is a train count or an AADT, the depth margin is irrelevant and
    the sentence says what to confirm with the railroad or the DOT instead.
  * `would_be_without` is what the class would be if the governing criterion fell away. It is
    stated as a conditional for the engineer to check, never applied: lowering a class needs
    the engineer, and the suite only ever moves results in the conservative direction.
  * A flag is never silently dropped: everything that qualifies the class (an incomplete flood
    model, structures not evaluated, a provisional High, no signed-off fatality rates) is on
    the card even though it makes the card longer.
  * Pure Python: no QGIS, no file system, nothing that raises for a caller in a UI callback.
"""

import os

ND_RANKS = {"Low": 0, "Medium": 1, "High": 2}
# which criteria make which class, worst first - the keys of nd["counts"]
HIGH_KEYS = ("habitable_probable", "habitable_judgment", "rail_high", "roads_high")
MEDIUM_KEYS = ("habitable_inundated", "roads_medium", "rail", "lifeline", "access_cut")
TRAINS_HIGH_PER_DAY = 10            # ND 5.2.1.3
AADT_HIGH_ABOVE = 2000              # ND 5.2.1.2
AADT_MEDIUM_FROM = 500             # ND 5.2.1.2


def _n(v):
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _f(v):
    try:
        return None if v is None or v == "" else float(v)
    except (TypeError, ValueError):
        return None


def _plural(n, one, many=None):
    return one if _n(n) == 1 else (many or one + "s")


def _rail_detail(rails):
    """"2 BNSF railroad stretches, 15 trains/day" from the High rail stretches."""
    high = [r for r in rails or () if str(r.get("nd_class") or "") == "High"]
    if not high:
        return ""
    roads = sorted({str(r.get("railroad") or "").strip() for r in high if r.get("railroad")})
    trains = [_f(r.get("trains")) for r in high]
    trains = [t for t in trains if t is not None]
    who = " / ".join(roads[:2]) + (" and others" if len(roads) > 2 else "") if roads else ""
    what = "{} {} {}".format(len(high), (who + " railroad") if who else "railroad",
                             _plural(len(high), "stretch", "stretches"))
    if any(r.get("passenger") for r in high):
        return what + ", passenger service"
    if trains:
        return "{}, {:.0f} trains/day".format(what, max(trains))
    return what


def _road_detail(roads, high=True):
    want = "High" if high else "Medium"
    rs = [r for r in roads or () if str(r.get("nd_class") or "") == want]
    if not rs:
        return ""
    names = sorted({str(r.get("name") or "").strip() for r in rs if r.get("name")})
    aadt = [_f(r.get("aadt")) for r in rs]
    aadt = [a for a in aadt if a is not None]
    what = "{} overtopped road {}".format(len(rs), _plural(len(rs), "stretch", "stretches"))
    if names:
        what += " ({})".format(", ".join(names[:2]) + (" and others" if len(names) > 2 else ""))
    if aadt:
        what += ", AADT up to {:,.0f}".format(max(aadt))
    return what


def criteria_met(nd, roads=(), rails=(), access=None):
    """[(class, key, text)] of every ND criterion the result met, worst first."""
    counts = (nd or {}).get("counts") or {}
    acc = access or {}
    out = []
    probable, judg = _n(counts.get("habitable_probable")), _n(counts.get("habitable_judgment"))
    if probable:
        out.append(("High", "habitable_probable", "{} habitable {} in lethal flood conditions "
                    "({} {})".format(probable, _plural(probable, "structure"),
                                     _n(counts.get("persons_probable")),
                                     _plural(_n(counts.get("persons_probable")), "person",
                                             "people"))))
    if judg:
        out.append(("High", "habitable_judgment", "{} habitable {} in the ACER judgment zone, "
                    "counted as probable loss of life".format(judg,
                                                              _plural(judg, "structure"))))
    if _n(counts.get("rail_high")):
        out.append(("High", "rail_high", _rail_detail(rails) or "{} railroad {} with 10 or more "
                    "trains a day".format(_n(counts.get("rail_high")),
                                          _plural(_n(counts.get("rail_high")), "stretch",
                                                  "stretches"))))
    if _n(counts.get("roads_high")):
        out.append(("High", "roads_high", _road_detail(roads, True) or
                    "{} overtopped Interstate / high-traffic road {}".format(
                        _n(counts.get("roads_high")),
                        _plural(_n(counts.get("roads_high")), "stretch", "stretches"))))
    inund = _n(counts.get("habitable_inundated"))
    if inund and not probable:
        out.append(("Medium", "habitable_inundated", "{} habitable {} inundated (economic "
                    "loss)".format(inund, _plural(inund, "structure"))))
    if _n(counts.get("roads_medium")):
        out.append(("Medium", "roads_medium", _road_detail(roads, False) or
                    "{} overtopped US / State highway or AADT 500 - 2,000 {}".format(
                        _n(counts.get("roads_medium")),
                        _plural(_n(counts.get("roads_medium")), "stretch", "stretches"))))
    if _n(counts.get("rail")) and not _n(counts.get("rail_high")):
        out.append(("Medium", "rail", "{} inundated railroad {}".format(
            _n(counts.get("rail")), _plural(_n(counts.get("rail")), "stretch", "stretches"))))
    if _n(counts.get("lifeline")):
        out.append(("Medium", "lifeline", "{} lifeline / critical {} inundated".format(
            _n(counts.get("lifeline")), _plural(_n(counts.get("lifeline")), "facility",
                                                "facilities"))))
    if _n(acc.get("cut")):
        out.append(("Medium", "access_cut", "{} habitable {} lose their only road access"
                    .format(_n(acc.get("cut")), _plural(_n(acc.get("cut")), "structure"))))
    return out


def governing(met, cls):
    """(the criteria that carry `cls`, the class it would be without them)."""
    rank = ND_RANKS.get(str(cls or "").title(), 0)
    carry = [m for m in met if ND_RANKS.get(m[0], 0) >= rank]
    rest = [m for m in met if ND_RANKS.get(m[0], 0) < rank]
    without = "Low"
    for m in rest:
        if ND_RANKS.get(m[0], 0) > ND_RANKS.get(without, 0):
            without = m[0]
    return carry, without


def reason_text(nd, met, carry):
    """The ONE reason sentence ("High: 2 BNSF railroad stretches, 15 trains/day; nothing else
    makes it High")."""
    cls = str((nd or {}).get("cls") or "").title() or "Low"
    if not met:
        return "{}: no habitable structure, road (AADT 500 or more / highway) or railroad is " \
               "affected.".format(cls)
    if not carry:
        carry = met[:1]
    head = "{}: {}".format(cls, carry[0][2])
    if len(carry) == 1:
        head += " - the only criterion that makes it {}".format(cls)
    else:
        head += "; and {} other {} ({})".format(
            len(carry) - 1, _plural(len(carry) - 1, "reason"),
            "; ".join(c[2] for c in carry[1:3]) +
            (" ..." if len(carry) > 3 else ""))
    return head + "."


def change_text(nd, carry, without, margin=None, rails=(), roads=()):
    """The "what would change it" sentence: the depth margin when a structure decides the
    class, the count to confirm when a train count or an AADT does."""
    lines = [str(x) for x in ((margin or {}).get("lines") or []) if str(x).strip()]
    keys = [k for _c, k, _t in carry]
    if "habitable_probable" in keys or "habitable_judgment" in keys:
        if lines:
            return lines[0]
    if "rail_high" in keys:
        high = [r for r in rails or () if str(r.get("nd_class") or "") == "High"]
        who = sorted({str(r.get("railroad") or "").strip() for r in high if r.get("railroad")})
        return ("{} if the train count on {} were under {} a day and no passenger service used "
                "it (ND 5.2.1.3) - confirm the count with the railroad; the class stays High "
                "until it is confirmed.".format(
                    without, " / ".join(who) or "those stretches", TRAINS_HIGH_PER_DAY))
    if "roads_high" in keys:
        return ("{} if the AADT of those stretches were {:,} or less and none is an Interstate "
                "(ND 5.2.1.2) - confirm the traffic count with the DOT; the class stays High "
                "until it is confirmed.".format(without, AADT_HIGH_ABOVE))
    if "habitable_inundated" in keys or "lifeline" in keys or "access_cut" in keys:
        if lines:
            return lines[0]
        return ("{} if no habitable structure, lifeline facility or sole-access road were "
                "affected (ND 5.2.2).".format(without))
    if lines:
        return lines[0]
    return "No margin could be computed for this result."


def flag_lines(nd, flood_cut_off=None, loss=None, access=None, extra=()):
    """The short flags that qualify the class."""
    out = []
    nd = nd or {}
    cut = flood_cut_off or {}
    if cut:
        out.append("flood cut off: {}".format(str(cut.get("reason") or "reason not given")
                                              .strip()))
    if nd.get("provisional"):
        m = _f(nd.get("provisional_margin_ft"))
        out.append("High is provisional{}".format(
            ": the most-exposed dwelling is {:.2f} ft past the ACER judgment line".format(m)
            if m is not None else ""))
    for what in (nd.get("incomplete") or ()):
        out.append("{} not evaluated - the class is a minimum".format(what))
    if _n((access or {}).get("unknown")):
        out.append("{} structure(s) with no mapped road: access not screened".format(
            _n((access or {}).get("unknown"))))
    if loss is not None and not (loss or {}).get("rates_ok", True):
        out.append("loss of life not estimated: no signed-off fatality rate table")
    for x in extra or ():
        if str(x).strip():
            out.append(str(x).strip())
    return out


def number_rows(nd, tm11=None, access=None, loss=None):
    """[(label, value)] - the numbers under the card."""
    counts = (nd or {}).get("counts") or {}
    tm11 = tm11 or {}
    rows = [("Habitable structures inundated", "{}".format(_n(counts.get(
        "habitable_inundated")))),
        ("... in lethal flood conditions (ACER high danger zone)",
         "{} ({} people)".format(_n(counts.get("habitable_probable")),
                                 _n(counts.get("persons_probable")))),
        ("... in the ACER judgment zone", "{}".format(_n(counts.get("habitable_judgment")))),
        ("Overtopped road stretches", "{} (High {}, Medium {})".format(
            _n(counts.get("roads_high")) + _n(counts.get("roads_medium")),
            _n(counts.get("roads_high")), _n(counts.get("roads_medium")))),
        ("Inundated railroad stretches", "{} ({} with 10+ trains/day or passenger)".format(
            _n(counts.get("rail")), _n(counts.get("rail_high")))),
        ("Lifeline / critical facilities inundated", "{}".format(_n(counts.get("lifeline"))))]
    acc = access or {}
    if acc:
        rows.append(("Habitable structures losing their only road access",
                     "{} of {}".format(_n(acc.get("cut")), _n(acc.get("structures")))))
    if tm11.get("cls"):
        rows.append(("ACER TM 11 class (lives in jeopardy)", "{} ({:.1f})".format(
            tm11.get("cls"), _f(tm11.get("lij")) or 0.0)))
    if loss:
        rows.append(("Population at risk (night / day)", "{:.0f} / {:.0f}".format(
            _f((loss.get("night") or {}).get("par")) or 0.0,
            _f((loss.get("day") or {}).get("par")) or 0.0)))
    return rows


def build(nd, tm11=None, roads=(), rails=(), access=None, margin=None, flood_cut_off=None,
          loss=None, history_line="", dam_name="", governing_scenario="", extra_flags=()):
    """The card as a dict: cls, reason, change, would_be_without, flags, numbers, history."""
    nd = nd or {}
    met = criteria_met(nd, roads, rails, access)
    carry, without = governing(met, nd.get("cls"))
    return {
        "dam": str(dam_name or ""),
        "scenario": str(governing_scenario or ""),
        "cls": str(nd.get("cls") or "").title(),
        "provisional": bool(nd.get("provisional")),
        "reason": reason_text(nd, met, carry),
        "change": change_text(nd, carry, without, margin, rails, roads),
        "would_be_without": without,
        "criteria": [(c, k, t) for c, k, t in met],
        "governing_keys": [k for _c, k, _t in carry],
        "flags": flag_lines(nd, flood_cut_off, loss, access, extra_flags),
        "numbers": number_rows(nd, tm11, access, loss),
        "history": str(history_line or ""),
    }


TITLE = "WHAT THIS MEANS"
CAVEAT = ("This card is a screening result for the people who receive the report (dam owner, "
          "county emergency manager). The engineer of record remains responsible for the "
          "classification.")


def lines(card, title=TITLE):
    """The card as report / summary text lines."""
    c = card or {}
    out = [title,
           "  ND hazard class (screening): {}{}".format(
               str(c.get("cls") or "?").upper(),
               " (provisional)" if c.get("provisional") else ""),
           "  Why: {}".format(c.get("reason") or "-"),
           "  What would change it: {}".format(c.get("change") or "-")]
    if c.get("flags"):
        out.append("  Flags: " + "; ".join(c["flags"]))
    if c.get("history"):
        out.append("  History: " + c["history"])
    for label, value in c.get("numbers") or ():
        out.append("  {:<52} {}".format(label, value))
    out.append("  " + CAVEAT)
    return out


def _esc(text):
    return (str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


COLORS = {"High": "#b2182b", "Medium": "#e08214", "Low": "#1a7a3c"}


def html(card, title=TITLE):
    """The card for a Qt label (the Review tab). Qt's rich text, no CSS files."""
    c = card or {}
    cls = str(c.get("cls") or "?")
    colour = COLORS.get(cls.title(), "#333333")
    rows = "".join(
        "<tr><td style='padding-right:14px'>{}</td><td><b>{}</b></td></tr>".format(
            _esc(label), _esc(value)) for label, value in (c.get("numbers") or ()))
    parts = ["<div><span style='font-size:11pt'>{}</span><br>".format(_esc(title)),
             "<span style='font-size:16pt; font-weight:bold; color:{}'>ND {}{}</span>".format(
                 colour, _esc(cls.upper()), " (provisional)" if c.get("provisional") else ""),
             "<br><b>Why:</b> {}".format(_esc(c.get("reason") or "-")),
             "<br><b>What would change it:</b> {}".format(_esc(c.get("change") or "-"))]
    if c.get("flags"):
        parts.append("<br><b>Flags:</b> {}".format(_esc("; ".join(c["flags"]))))
    if c.get("history"):
        parts.append("<br><b>History:</b> {}".format(_esc(c["history"])))
    if rows:
        parts.append("<table style='margin-top:6px'>{}</table>".format(rows))
    parts.append("<span style='font-size:8pt; color:#555555'>{}</span></div>".format(
        _esc(CAVEAT)))
    return "".join(parts)


def report_text(card):
    """The card as the two blocks the report's first page prints (title block, numbers)."""
    c = card or {}
    head = ["ND hazard class (screening): {}{}".format(
        str(c.get("cls") or "?").upper(), " (provisional)" if c.get("provisional") else ""),
        "", "Why: {}".format(c.get("reason") or "-"), "",
        "What would change it: {}".format(c.get("change") or "-")]
    if c.get("flags"):
        head += ["", "Flags: " + "; ".join(c["flags"])]
    if c.get("history"):
        head += ["", "History: " + c["history"]]
    nums = ["{}: {}".format(label, value) for label, value in c.get("numbers") or ()]
    return "\n".join(head), "\n".join(nums)


def write_card_file(folder, card, name="Result_Card.txt"):
    """The card next to the report, for whoever does not open the PDF. Returns the path."""
    path = os.path.join(folder, name)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines(card)) + "\n")
    return path
