# -*- coding: utf-8 -*-
"""v2.5: how close the ND classification is to changing (support for the
ND 5.3(f) borderline statement).

Only the structure criteria depend on flood depth in a way that can be
stated as a margin:
  * 2D model: a habitable structure makes the dam HIGH when it reaches the
    ACER high-danger boundary, or the judgment boundary when judgment-zone
    structures are counted as probable loss of life (old option; ND default since 2.35.0 is MEDIUM by rule);
  * 1D model: when its depth exceeds 2 ft.
The margins are the depth still missing to reach that boundary (or the
depth by which it is exceeded), per structure, from max depth with max
velocity - the same pairing the borderline check uses. Road / rail
criteria depend on traffic and train counts, not on a depth margin; the
result says when they also decide the class.

Everything here works on the plugin's structure records (plain dicts), so
it can be tested without QGIS.
"""

from .constants import ND_HIGH, ND_MEDIUM, ND_LOW, ND_1D_DEPTH_FT

_DRY = "Not inundated"
_PROBABLE = "Probable loss of life"
_JUDGMENT = "Judgment zone"


def _trigger_margin(rec, judgment_as_probable, one_d):
    """Depth (ft) still missing before this structure makes the dam HIGH
    (> 0), or the depth by which it already does (<= 0). None when unknown
    (dry, no velocity for the ACER margins)."""
    d = rec.get("max_depth")
    if d is None or d <= 0:
        return None
    if one_d:
        return ND_1D_DEPTH_FT - float(d)
    key = "margin_judg" if judgment_as_probable else "margin_high"
    m = rec.get(key)
    return None if m is None else float(m)


def _triggers_high(rec, judgment_as_probable, one_d):
    out = rec.get("outcome")
    if out == _PROBABLE:
        return True
    return bool(judgment_as_probable and not one_d and out == _JUDGMENT)


def _label(rec):
    from .structures import address_of
    try:
        addr = address_of(rec)[0]
    except Exception:
        addr = ""
    what = rec.get("occupancy") or rec.get("fema_category") or ""
    return "structure {}{}{}".format(rec.get("id"), " ({})".format(addr) if addr else "",
                                     " - {}".format(what[:40]) if what and not addr else "")


def analyse(structs, nd, judgment_as_probable=False, one_d=False, roads=None, rails=None):
    """{"lines": [...], "up": {...} or None, "down": {...} or None,
    "geoms": [QgsGeometry of the controlling structures]}.

    up   - class below HIGH: the wet habitable structure closest to the HIGH
           trigger and the depth increase that would reach it
    down - class HIGH through structures: how many structures carry it and
           the depth decrease that would take ALL of them below the trigger
    """
    cls = nd.get("cls")
    hab = [s for s in structs if s.get("habitable") == "Yes"
           and s.get("outcome") not in (None, _DRY)]
    boundary = ("the 2-ft depth rule (1D model)" if one_d else
                "the ACER judgment boundary (judgment zone counted as probable loss of life)"
                if judgment_as_probable else "the ACER high-danger boundary")
    lines = []
    res = {"lines": lines, "up": None, "down": None, "geoms": []}
    roads = roads or []
    rails = rails or []
    other_high = [r for r in list(roads) + list(rails) if r.get("nd_class") == ND_HIGH]

    if cls == ND_HIGH:
        carriers = [s for s in hab if _triggers_high(s, judgment_as_probable, one_d)]
        if carriers:
            excess = [(-(_trigger_margin(s, judgment_as_probable, one_d) or 0.0), s)
                      for s in carriers]
            excess.sort(key=lambda t: t[0])
            lo, lo_rec = excess[0]
            hi, hi_rec = excess[-1]
            res["down"] = {"structures": len(carriers), "min_excess_ft": lo,
                           "max_excess_ft": hi, "controlling": hi_rec["id"]}
            res["geoms"] = [s["geom"] for s in carriers if s.get("geom") is not None][:50]
            lines.append(
                "HIGH rests on {} habitable structure(s) at or beyond {}; the least exposed "
                "is {:.2f} ft beyond it ({}), the most exposed {:.2f} ft ({}). All of them "
                "would have to be {:.2f} ft shallower for the structures alone to stop "
                "making the dam HIGH.".format(len(carriers), boundary, max(lo, 0.0),
                                             _label(lo_rec), max(hi, 0.0), _label(hi_rec),
                                             max(hi, 0.0)))
            if len(carriers) == 1:
                lines.append("A single structure decides the HIGH class - confirm its "
                             "habitability, type and location first.")
        if other_high:
            lines.append("{} road / rail stretch(es) also make the dam HIGH (traffic / train "
                         "counts, not a depth margin).".format(len(other_high)))
        elif not carriers:
            lines.append("HIGH is not decided by a structure depth margin.")
        return res

    # below HIGH: nearest structure to the trigger
    cand = []
    unknown = 0
    for s in hab:
        m = _trigger_margin(s, judgment_as_probable, one_d)
        if m is None:
            unknown += 1
            continue
        cand.append((m, s))
    reaches = [(m, s) for m, s in cand if m <= 0]
    below = sorted([(m, s) for m, s in cand if m > 0], key=lambda t: t[0])
    if below:
        m, s = below[0]
        res["up"] = {"margin_ft": m, "controlling": s["id"], "count_within_1ft":
                     sum(1 for mm, _s in below if mm <= 1.0)}
        res["geoms"] = [s["geom"]] if s.get("geom") is not None else []
        lines.append("The dam would become HIGH if the depth at {} rose {:.2f} ft (to {}).".format(
            _label(s), m, boundary))
        n1 = res["up"]["count_within_1ft"]
        if n1 > 1:
            lines.append("{} habitable structures are within 1 ft of that boundary.".format(n1))
    if reaches:
        lines.append("{} structure(s) reach the boundary with max depth paired with max "
                     "velocity but not with the same-time ACER method used for the zones - "
                     "the method decides their zone.".format(len(reaches)))
    if unknown and not one_d:
        lines.append("{} flooded habitable structure(s) have no depth margin (no velocity "
                     "raster).".format(unknown))
    if cls == ND_LOW:
        lines.append("No habitable structure is flooded; any flooding of one would make the "
                     "dam at least MEDIUM (5.2.2).")
    elif cls == ND_MEDIUM and not hab:
        lines.append("MEDIUM comes from roads, rail, lifelines or access, not from flooded "
                     "structures.")
    if not lines:
        lines.append("No depth margin could be computed.")
    return res
