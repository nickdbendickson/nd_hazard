# -*- coding: utf-8 -*-
"""Dam hazard classification from the structure, road and rail results.

ND  - North Dakota Dam Safety Standards Policy REG_05.2026 (effective
      04/01/2026), Section 5.2: High if loss of human life is probable;
      otherwise Medium if economic losses / lifeline impacts (habitable
      structures inundated to any degree, roads with AADT >= 500 or US/State
      highways, any railroad, sole-access roads, lifeline facilities);
      otherwise Low. Borderline -> higher class (5.3(f)).
TM 11 - USBR ACER TM 11 (1988) Table 1 by lives in jeopardy (LIJ):
      Low 0, Significant 1-6, High > 6.

The result is a screening classification with the reasons behind it; it
does not replace the engineer's review (ND 5.2.1.1 judgment zone, sole
access roads, lifeline facilities, imminent future development).
"""

import math
import os
import time

from qgis.core import QgsField, QgsFeature, QgsCoordinateReferenceSystem

from .compat import WKB_NOGEOMETRY, FT_STRING
from .constants import (
    ND_POLICY_REF, TM11_REF, ND_HIGH, ND_MEDIUM, ND_LOW, ND_RANK,
    TM11_SIGNIFICANT_MAX_LIJ, PLUGIN_NAME, PLUGIN_VERSION, CLASS_HIGH, ND_AADT_LOW_BELOW,
    CLASS_JUDGMENT, vehicle_curve_available,
)
from .helpers import create_memory_layer, export_layer_to_formats, prepare_output_path
from .structures import OUT_PROBABLE, OUT_JUDGMENT, OUT_ECONOMIC, TYPE_MOBILE


def _raise(level, new):
    return new if ND_RANK[new] > ND_RANK[level] else level


def _drop_low_reason(nd):
    """A criterion added after classify_nd raised the class: the "LOW - no
    ..." reason no longer applies."""
    nd["reasons"] = [r for r in nd["reasons"] if not r.startswith("LOW -")]


JUDGMENT_RULE_PREFIX = "MEDIUM - "     # 2.35.0: judgment-zone rule reason starts so


def classify_nd(structs, roads, rails, judgment_as_probable=False, one_d=False,
                roads_evaluated=True, rail_evaluated=True, structs_evaluated=True):
    """Returns dict(cls, reasons[], judgment[], counts{}). v2.19.0: `structs_evaluated` False
    (no structure layer was given or downloaded) shows the structure rows as "not evaluated"
    instead of "not met" and marks the class incomplete.

    2.35.0 (Nick, 10/30/2026): a habitable structure in the ACER judgment zone makes the dam
    MEDIUM by rule - not probable loss of life, not a provisional High. `judgment_as_probable`
    True is the old conservative option (judgment zone counted as probable loss of life,
    HIGH); it is kept as a non-default option."""
    cls = ND_LOW
    reasons, judgment = [], []
    hab = [s for s in structs if s["habitable"] == "Yes" and not s.get("dwelling_extra")]
    probable = [s for s in hab if s.get("outcome") == OUT_PROBABLE]
    judg = [s for s in hab if s.get("outcome") == OUT_JUDGMENT]
    econ = [s for s in hab if s.get("outcome") in (OUT_PROBABLE, OUT_JUDGMENT, OUT_ECONOMIC)]
    lifeline = [s for s in econ if s.get("lifeline")]
    unverified = [s for s in structs if s["habitable"] == "Unverified"
                  and not s.get("dwelling_extra")
                  and s.get("outcome") in (OUT_PROBABLE, OUT_JUDGMENT)]

    if probable:
        cls = ND_HIGH
        how = ("depth > 2 ft (1D model rule)" if one_d
               else "high danger zone, Figs 5.1/5.2 (Reclamation 1988)")
        reasons.append("HIGH - {} habitable structure(s) with lethal flood conditions: {} "
                       "[5.2.1.1]".format(len(probable), how))
    if judg:
        msg = ("{} habitable structure(s) in the ACER judgment zone - engineering judgment "
               "required [5.2.1.1]".format(len(judg)))
        if judgment_as_probable:
            cls = _raise(cls, ND_HIGH)
            reasons.append("HIGH - " + msg + "; counted as probable loss of life "
                           "(conservative, borderline rule 5.3(f))")
        else:
            # 2.35.0: the ND judgment-zone rule - MEDIUM, automatically
            cls = _raise(cls, ND_MEDIUM)
            reasons.append(JUDGMENT_RULE_PREFIX + "{} habitable structure(s) in the ACER "
                           "judgment zone - classed MEDIUM by rule (judgment-zone houses "
                           "are Medium, not probable loss of life) [5.2.1.1]".format(
                               len(judg)))
    for r in roads:
        if r["nd_class"] == ND_HIGH:
            cls = ND_HIGH
    road_high = [r for r in roads if r["nd_class"] == ND_HIGH]
    road_med = [r for r in roads if r["nd_class"] == ND_MEDIUM]
    if road_high:
        reasons.append("HIGH - {} overtopped road stretch(es) on Interstates or with "
                       "AADT > 2,000 [5.2.1.2]: {}".format(len(road_high), _road_list(road_high)))
    rail_high = [r for r in rails if r["nd_class"] == ND_HIGH]
    if rail_high:
        cls = ND_HIGH
        reasons.append("HIGH - {} inundated rail stretch(es) on passenger lines or freight "
                       "lines with >= 10 trains/day [5.2.1.3]: {}".format(
                           len(rail_high), _rail_list(rail_high)))

    if econ and not probable:
        cls = _raise(cls, ND_MEDIUM)
        reasons.append("MEDIUM - {} habitable structure(s) inundated (economic loss) "
                       "[5.2.2]".format(len(econ)))
    if road_med:
        cls = _raise(cls, ND_MEDIUM)
        reasons.append("MEDIUM - {} overtopped stretch(es) of US/State highways or roads "
                       "with AADT 500-2,000 [5.2.1.2]: {}".format(len(road_med),
                                                                 _road_list(road_med)))
    if rails and not rail_high:
        cls = _raise(cls, ND_MEDIUM)
        reasons.append("MEDIUM - railroad impacted ({} stretch(es)) [5.2.1.3]: {}".format(
            len(rails), _rail_list(rails)))
    if lifeline:
        cls = _raise(cls, ND_MEDIUM)
        reasons.append("MEDIUM (at least) - {} possible lifeline facilities inundated "
                       "(hospital / emergency response) [5.2.2] - verify".format(len(lifeline)))

    # structure items first (they matter most); roads / rail as one line each
    # so a large network cannot push them off the report page
    if unverified:
        judgment.append("{} unverified structure(s) (no habitability data) in high/judgment "
                        "zones - confirm whether any are habitable".format(len(unverified)))
    n_struct_items = len(judgment)

    def examples(items, fmt, n=4):
        names = [fmt(r) for r in items[:n]]
        return "; ".join(names) + ("" if len(items) <= n else " and {} more".format(
            len(items) - n))
    no_aadt_roads = [r for r in roads if r["nd_judgment"]]
    if no_aadt_roads:
        judgment.append("{} overtopped road stretch(es) without AADT - verify traffic: "
                        "{}".format(len(no_aadt_roads), examples(
                            no_aadt_roads, lambda r: "'{}' ({:.0f} ft)".format(
                                r["name"] or "unnamed", r["length_ft"]))))
    bridges = [r for r in roads if r["bridge"]]
    if bridges:
        judgment.append("{} bridge stretch(es) - confirm the deck is overtopped: {}".format(
            len(bridges), examples(bridges, lambda r: "'{}'".format(r["name"] or "unnamed"))))
    rail_unknown = [r for r in rails if r["nd_judgment"]]
    if rail_unknown:
        judgment.append("{} rail stretch(es) with trains/day unknown or unconfirmed (or "
                        "out of service) - high hazard if >= 10/day or passenger service: "
                        "{}".format(
                            len(rail_unknown), examples(rail_unknown, lambda r: "{} {} ({})".format(
                                r["railroad"], r["subdivision"], r.get("nd_basis", "")).strip())))
    if not structs_evaluated:
        judgment.append("No structure layer was evaluated (none given, none downloaded) - the "
                        "habitable-structure criteria (5.2.1.1, 5.2.2) were not applied, so "
                        "the class is a minimum and may be higher")
    if not roads_evaluated:
        judgment.append("No road layer was evaluated - ND road criteria (5.2.1.2) not applied")
    if not rail_evaluated:
        judgment.append("No rail layer was evaluated - ND railroad criteria (5.2.1.3) not "
                        "applied")
    judgment.append("Sole-access roads to occupied locations (5.2.1.2; network screening "
                    "below when a road layer was given), lifeline facilities (5.2.2; screened "
                    "from agency / OSM data when downloaded), imminent future development "
                    "(5.3(b)) and dams in series (5.3(c); screened from the National "
                    "Inventory of Dams when downloaded) must be reviewed by the engineer")
    if not reasons:
        reasons.append("LOW - no habitable structures, roads (AADT >= 500 / highways) or "
                       "railroads affected" if structs_evaluated else
                       "LOW SO FAR - no roads (AADT >= 500 / highways) or railroads affected "
                       "in the layers given; habitable structures were NOT evaluated")

    # ---- criteria table (report / summary): one row per ND 5.2 criterion ----
    def persons(items):
        return sum(int(s.get("persons") or 0) for s in items)

    def met(n, result):
        return result if n else "not met"
    rail_other = [r for r in rails if r["nd_class"] != ND_HIGH]
    no_aadt = [r for r in roads if r.get("nd_judgment")]
    se = structs_evaluated
    crit = [
        ("5.2.1.1", "Probable loss of life - habitable structures {}".format(
            "with depth > 2 ft (1D model)" if one_d else "in the ACER high danger zone"),
         "{} ({} persons)".format(len(probable), persons(probable)) if se else "not evaluated",
         met(probable, ND_HIGH) if se else "?"),
        ("5.2.1.1", "Habitable structures in the ACER judgment zone",
         "not evaluated" if not se else
         "n/a (1D model)" if one_d else "{} ({} persons)".format(len(judg), persons(judg)),
         "?" if not se else
         "not met" if not judg else (ND_HIGH + " (judgment counted)" if judgment_as_probable
                                     else ND_MEDIUM + " (by rule)")),
        ("5.2.1.2", "Overtopped roads - Interstate or AADT > 2,000",
         "{} stretch(es)".format(len(road_high)) if roads_evaluated else "not evaluated",
         met(road_high, ND_HIGH) if roads_evaluated else "?"),
        ("5.2.1.2", "Overtopped roads - US / State highway or AADT 500 - 2,000",
         "{} stretch(es)".format(len(road_med)) if roads_evaluated else "not evaluated",
         met(road_med, ND_MEDIUM) if roads_evaluated else "?"),
        ("5.2.1.2", "Overtopped roads without AADT data",
         "{} stretch(es)".format(len(no_aadt)) if roads_evaluated else "not evaluated",
         met(no_aadt, "Judgment") if roads_evaluated else "?"),
        ("5.2.1.3", "Railroad - passenger line or >= 10 trains/day",
         "{} stretch(es)".format(len(rail_high)) if rail_evaluated else "not evaluated",
         met(rail_high, ND_HIGH) if rail_evaluated else "?"),
        ("5.2.1.3", "Railroad - other lines",
         "{} stretch(es)".format(len(rail_other)) if rail_evaluated else "not evaluated",
         met(rail_other, ND_MEDIUM) if rail_evaluated else "?"),
        ("5.2.2", "Habitable structures inundated (economic loss)",
         "{} ({} persons)".format(len(econ), persons(econ)) if se else "not evaluated",
         met(econ, ND_MEDIUM) if se else "?"),
        ("5.2.2", "Possible lifeline facilities inundated",
         "{}".format(len(lifeline)) if se else "not evaluated",
         met(lifeline, ND_MEDIUM + " (verify)") if se else "?"),
        ("5.2.1.1", "Unverified structures (habitability unknown) in high / judgment zones",
         "{}".format(len(unverified)) if se else "not evaluated",
         met(unverified, "Judgment") if se else "?"),
    ]
    criteria = [{"section": a, "criterion": b, "found": c, "result": d} for a, b, c, d in crit]
    incomplete = [n for n, ok in (("structures", structs_evaluated), ("roads", roads_evaluated),
                                  ("railroads", rail_evaluated)) if not ok]
    return {"cls": cls, "reasons": reasons, "judgment": judgment, "criteria": criteria,
            "struct_items": n_struct_items, "incomplete": incomplete,
            "counts": {
                "habitable_probable": len(probable), "habitable_judgment": len(judg),
                "habitable_inundated": len(econ), "lifeline": len(lifeline),
                "roads_high": len(road_high), "roads_medium": len(road_med),
                "rail": len(rails), "rail_high": len(rail_high),
                "unverified": len(unverified), "persons_probable": persons(probable),
                "persons_judgment": persons(judg)},
            # 2.35.0: which judgment-zone rule classified this ("medium" = ND rule)
            "judgment_rule": "probable" if judgment_as_probable else "medium"}


def tm11_rows(tm11):
    """ACER TM 11 Table 1 rows for the report / summary:
    [(item, value)]."""
    g = tm11.get
    inc = "included" if g("judgment_included", True) else "not included"
    return [
        ("Persons in habitable structures - high danger zone",
         "{:,.0f}".format(g("lij_structures_high") or 0)),
        ("Persons in habitable structures - judgment zone ({})".format(inc),
         "{:,.0f}".format(g("lij_structures_judgment") or 0)),
        ("Motorists - high danger zone (estimate)",
         "{:.1f}".format(g("lij_vehicles_high") or 0)),
        ("Motorists - judgment zone ({})".format(inc),
         "{:.1f}".format(g("lij_vehicles_judgment") or 0)),
        ("Lives in jeopardy (LIJ)", "{:.1f}".format(g("lij") or 0)),
        ("Table 1: Low 0 / Significant 1 - 6 / High > 6", str(g("cls", "")).upper()),
        ("Without judgment-zone items", str(g("cls_without_judgment", ""))),
    ]


def classify_tm11(structs, roads, judgment_as_probable=True):
    """ACER TM 11 Table 1 by lives in jeopardy."""
    hab = [s for s in structs if s["habitable"] == "Yes" and not s.get("dwelling_extra")]
    lij_high = sum(s["persons"] for s in hab if s.get("zone") == CLASS_HIGH)
    lij_judg = sum(s["persons"] for s in hab if s.get("zone") == CLASS_JUDGMENT)
    veh = sum(r["persons"] or 0 for r in roads if r.get("vehicle_zone") == CLASS_HIGH)
    veh_j = sum(r["persons"] or 0 for r in roads if r.get("vehicle_zone") == CLASS_JUDGMENT)
    lij = lij_high + veh + (lij_judg + veh_j if judgment_as_probable else 0)
    lij_round = int(math.ceil(lij - 1e-9))

    def table1(n):
        return "Low" if n <= 0 else ("Significant" if n <= TM11_SIGNIFICANT_MAX_LIJ else "High")
    notes = []
    if not vehicle_curve_available():
        notes.append("Motorists not counted - ACER Figure 4 (passenger vehicle) curve not "
                     "configured")
    mobiles = sum(1 for s in hab if s["acer_type"] == TYPE_MOBILE and s.get("zone"))
    return {"cls": table1(lij_round), "lij": lij, "lij_structures_high": lij_high,
            "lij_structures_judgment": lij_judg, "lij_vehicles_high": veh,
            "lij_vehicles_judgment": veh_j, "judgment_included": judgment_as_probable,
            "cls_without_judgment": table1(int(math.ceil(lij_high + veh - 1e-9))),
            "mobile_homes": mobiles, "notes": notes}


def _road_list(rs, n=4):
    names = []
    for r in rs[:n]:
        a = "AADT {:,.0f}{}".format(r["aadt"], " ({})".format(r["aadt_year"])
                                   if r["aadt_year"] else "") if r["aadt"] else "no AADT"
        names.append("{} [{}, {}]".format(r["name"] or "unnamed", r["road_class"], a))
    more = "" if len(rs) <= n else " and {} more".format(len(rs) - n)
    return "; ".join(names) + more


def _rail_list(rs, n=3):
    out = []
    for r in rs[:n]:
        t = "{:.0f} trains/day".format(r["trains"]) if r["trains"] is not None else "trains ?"
        out.append("{} {} [{}{}]".format(r["railroad"], r["subdivision"], t,
                                         ", passenger" if r["passenger"] else "").strip())
    return "; ".join(out) + ("" if len(rs) <= n else " and {} more".format(len(rs) - n))


def zone_table(structs):
    """Counts of habitable structures by ACER type and zone."""
    rows = {}
    for s in structs:
        if s["habitable"] != "Yes" or not s.get("zone"):
            continue
        key = (s["acer_type"], s["zone"])
        rows[key] = rows.get(key, 0) + 1
    return rows


def facility_criteria(nd, res, cfg, total=False):
    """v2.2 (in place): other dams in the inundated area (dams in series,
    5.3(c) - judgment) and lifeline / critical facilities from agency / OSM
    data (5.2.2 - at least Medium unless switched off). `total`: the result
    without the baseline (every flooded facility counts)."""
    from . import facilities as FAC
    crit = nd.setdefault("criteria", [])
    recs = res.get("facility_records")
    ev = res.get("fac_eval") or {}
    charged = None if total else res.get("fac_charged")
    dams_on = res.get("dams_evaluated", False)
    life_on = res.get("lifelines_evaluated", False)
    at = nd.get("struct_items", 0)
    # ---- dams in series ----
    dams = FAC.flooded(recs or [], ev, charged, kind=FAC.KIND_DAM, charged_only=False)
    high = [d for d in dams if FAC.is_high_hazard(d)]
    crit.append({"section": "5.3(c)", "criterion": "Dams in series - other dams in the "
                 "inundated area (National Inventory of Dams)",
                 "found": ("{} (High hazard: {})".format(len(dams), len(high))
                           if dams_on else "not evaluated"),
                 "result": ("Judgment" if dams else "not met") if dams_on else "?"})
    if dams:
        nd["judgment"].insert(at, "{} other dam(s) in the inundated area - dams in series "
                              "(5.3(c)): check whether the breach flood overtops or fails "
                              "them and what their failure adds: {}".format(
                                  len(dams), FAC.describe(dams, ev, dam=True)))
        at += 1
    # ---- lifelines ----
    wet = FAC.flooded(recs or [], ev, charged, kind=FAC.KIND_LIFELINE)
    life = [r for r in wet if r.get("lifeline")]
    schools = [r for r in wet if not r.get("lifeline") and r.get("category") in FAC.SCHOOLS]
    care = [r for r in wet if not r.get("lifeline") and r.get("category") not in FAC.SCHOOLS]
    as_medium = cfg.get("lifeline_as_medium", True)
    crit.append({"section": "5.2.2", "criterion": "Lifeline facilities inundated - hospital, "
                 "emergency response, utilities (agency / OSM data)",
                 "found": "{}".format(len(life)) if life_on else "not evaluated",
                 "result": ((ND_MEDIUM + " (verify)" if as_medium else "Judgment")
                            if life else "not met") if life_on else "?"})
    if life:
        msg = "{} lifeline facilit(ies) inundated [5.2.2]: {}".format(
            len(life), FAC.describe(life, ev))
        if as_medium:
            nd["cls"] = _raise(nd["cls"], ND_MEDIUM)
            _drop_low_reason(nd)
            nd["reasons"].append("MEDIUM (at least) - " + msg + " - verify")
        else:
            nd["judgment"].insert(at, msg + " - verify")
            at += 1
    # ---- bridges / culverts / crossings ----
    cr_on = res.get("crossings_evaluated", False)
    cross = sorted(FAC.flooded(recs or [], ev, charged, kind=FAC.KIND_CROSSING),
                   key=FAC.crossing_priority)
    busy = [r for r in cross if (r.get("adt") or 0) >= ND_AADT_LOW_BELOW]
    crit.append({"section": "5.2.1.2", "criterion": "Bridges, culverts, low-water crossings "
                 "and rail bridges in the inundated area (NBI / OSM)",
                 "found": ("{} (ADT >= {:,}: {})".format(len(cross), ND_AADT_LOW_BELOW,
                                                         len(busy))
                           if cr_on else "not evaluated"),
                 "result": ("Judgment" if cross else "not met") if cr_on else "?"})
    if cross:
        nd["judgment"].append("{} bridge(s) / culvert(s) / crossing(s) in the inundated area "
                              "- confirm whether the road or track is overtopped or washed "
                              "out (ADT >= {:,}: {}): {}".format(
                                  len(cross), ND_AADT_LOW_BELOW, len(busy),
                                  FAC.describe(cross, ev)))
    if care:
        nd["judgment"].insert(at, "{} care facilit(ies) (nursing home / assisted living) "
                              "inundated - vulnerable occupants; confirm they are in the "
                              "structure inventory: {}".format(len(care),
                                                               FAC.describe(care, ev)))
        at += 1
    if schools:
        nd["judgment"].insert(at, "{} school(s) / child-care facilit(ies) inundated - "
                              "vulnerable occupants (daytime); confirm they are in the "
                              "structure inventory and first on the EAP notification list: "
                              "{}".format(len(schools), FAC.describe(schools, ev)))
        at += 1
    nd["struct_items"] = at
    return nd


def extra_criteria(nd, structs, res, cfg, total=False):
    """v2.0 additions to the ND result (in place): sole-access roads
    (5.2.1.2), borderline structures (5.3(f)), economic-loss figures; v2.2:
    dams in series (5.3(c)) and lifeline facilities (5.2.2)."""
    from .structures import OUT_DRY, OUT_TRACE
    # (the incremental view has already cleared cut-offs the breach does
    # not cause; the "without baseline" class must count them all)
    cut = [s for s in structs if s.get("cut_off") and s.get("habitable") == "Yes"
           and s.get("outcome") in (None, OUT_DRY)]
    crit = nd.setdefault("criteria", [])
    access_on = res.get("access") is not None if isinstance(res, dict) else True
    persons = sum(s.get("persons") or 0 for s in cut)
    if cut:
        msg = ("{} dry habitable structure(s) ({} persons) lose all mapped road access - "
               "sole-access roads overtopped [5.2.1.2]".format(len(cut), persons))
        if cfg.get("access_as_medium", True):
            nd["cls"] = _raise(nd["cls"], ND_MEDIUM)
            _drop_low_reason(nd)
            nd["reasons"].append("MEDIUM (at least) - " + msg + " (network screening)")
        else:
            at = nd.get("struct_items", 0)
            nd["struct_items"] = at + 1
            nd["judgment"].insert(at, msg + " - verify")
    crit.insert(5 if len(crit) >= 5 else len(crit), {
        "section": "5.2.1.2", "criterion": "Sole-access roads - dry habitable structures "
        "cut off (network screening)",
        "found": "{} ({} persons)".format(len(cut), persons) if access_on else "not evaluated",
        "result": ("not met" if not cut else ND_MEDIUM if cfg.get("access_as_medium", True)
                   else "Judgment") if access_on else "?"})
    # v2.5: campgrounds / RV parks (overnight recreation) - counted like any
    # habitable structure above; their occupancy is seasonal and uncertain
    camps = [s for s in structs if s.get("camp") and s.get("habitable") == "Yes"
             and s.get("outcome") not in (None, OUT_DRY)]
    if camps or any(s.get("camp") for s in structs):
        crit.append({"section": "5.2.1.1", "criterion": "Campgrounds / RV parks inundated "
                     "(overnight recreation, OpenStreetMap)",
                     "found": "{} ({} persons)".format(len(camps), sum(
                         int(s.get("persons") or 0) for s in camps)),
                     "result": "not met" if not camps else "counted (verify)"})
    if camps:
        at = nd.get("struct_items", 0)
        nd["struct_items"] = at + 1
        nd["judgment"].insert(at, "{} campground(s) / RV park(s) inundated - overnight "
                              "recreation (5.2.1.1); occupancy is seasonal: verify the "
                              "number of sites, the season and the ACER type: {}".format(
                                  len(camps), "; ".join(
                                      "{} ({}, {})".format(
                                          (s["camp"].get("name") or "unnamed"),
                                          s.get("outcome"), "{:.1f} ft".format(
                                              s["max_depth"]) if s.get("max_depth")
                                          else "-") for s in camps[:4]) +
                                  ("" if len(camps) <= 4 else " and {} more".format(
                                      len(camps) - 4))))
    near = [s for s in structs if s.get("near") and s.get("habitable") == "Yes"]
    crit.append({"section": "5.3(f)", "criterion": "Borderline structures within {:g} ft of "
                 "the next boundary ({})".format(cfg.get("near_ft", 0.5),
                                                 "1D 2-ft rule" if cfg.get("one_d")
                                                 else "ACER curve"),
                 "found": "{}".format(len(near)),
                 "result": "not met" if not near else "counted higher"
                 if cfg.get("borderline_higher") else "Judgment"})
    if near:
        hi = sum(1 for s in near if s["near"] == "High")
        at = nd.get("struct_items", 0)
        nd["struct_items"] = at + 1
        nd["judgment"].insert(
            at,
            "{} habitable structure(s) within {:g} ft below the next boundary ({} below "
            "High) - borderline, ND 5.3(f){}".format(
                len(near), cfg.get("near_ft", 0.5), hi,
                ": already counted in the higher zone" if cfg.get("borderline_higher")
                else ""))
    wet = [s for s in structs if s.get("outcome") not in (None, OUT_DRY, OUT_TRACE)
           and s.get("habitable") == "Yes"]
    dmg = sum(s.get("damage_usd") or 0 for s in wet)
    if dmg:
        for i, r in enumerate(nd["reasons"]):
            if "economic loss" in r:
                nd["reasons"][i] = r + "; estimated residential damage ${:,.0f}".format(dmg)
                break
        for c in crit:
            if c["criterion"].startswith("Habitable structures inundated"):
                c["found"] += "; damage ${:,.0f}".format(dmg)
    if isinstance(res, dict):
        b = res.get("boundary")
        if b and b.get("flagged"):
            from .boundary import judgment_text
            at = nd.get("struct_items", 0)
            nd["judgment"].insert(at, judgment_text(b))
            nd["struct_items"] = at + 1
        facility_criteria(nd, res, cfg, total=total)
    # A High that exists only because judgment-zone dwellings clear the ACER
    # line by less than a foot is inside lidar and cell-size error.
    counts = nd.get("counts") or {}
    # 2.35.0: only when the old option counted them (by rule they are MEDIUM, never High)
    judgment_only = (nd["cls"] == ND_HIGH and counts.get("habitable_judgment")
                     and nd.get("judgment_rule", "probable") == "probable"
                     and not counts.get("habitable_probable")
                     and not counts.get("roads_high") and not counts.get("rail_high"))
    if judgment_only:
        margins = [abs(float(s["margin_judg"])) for s in structs
                   if s.get("habitable") == "Yes" and not s.get("dwelling_extra")
                   and s.get("outcome") == OUT_JUDGMENT and s.get("margin_judg") is not None]
        # the High rests on the MOST exposed judgment-zone dwelling: provisional
        # only when every one of them is within 1 ft of the line
        if margins and max(margins) < 1.0:
            nd["provisional"] = True
            nd["provisional_margin_ft"] = max(margins)
            msg = ("HIGH is provisional: the most-exposed judgment-zone dwelling is only "
                   "{:.2f} ft past the ACER judgment line, inside the vertical error of a "
                   "lidar crest and a coarse grid. The class stays High (borderline goes "
                   "to the higher class, 5.3(f)); confirm the buildings are habitable and "
                   "the depths before the class is final.".format(max(margins)))
            nd["judgment"].insert(0, msg)
            nd["reasons"].insert(0, "PROVISIONAL - " + msg)
    result = "HIGH (provisional)" if nd.get("provisional") else nd["cls"].upper()
    crit.append({"section": "Result", "criterion": "ND hazard classification (screening)",
                 "found": "", "result": result})
    return nd


def criteria_text(nd):
    """The criteria table as fixed-width text lines."""
    out = ["  {:<8} {:<74} {:<30} {}".format("Section", "Criterion", "Found", "Result")]
    for c in nd.get("criteria", []):
        out.append("  {:<8} {:<74} {:<30} {}".format(c["section"], c["criterion"][:74],
                                                    c["found"][:30], c["result"]))
    return out


def flood_cut_off_note(info):
    """v2.28: the data-sources / warning sentence for a flood model the ND Dam Breach Runner cut
    off (modelchain.read_flood_cut_off), or "" when `info` is empty. The class is not changed by
    this; the sentence says why it may be low."""
    if not info:
        return ""
    reason = str(info.get("reason") or "reason not given").strip()
    detail = str(info.get("detail") or "").strip()
    return ("Flood model INCOMPLETE: the ND Dam Breach Runner stopped the run while water was "
            "still moving ({}{}). The inundated extent, and so the structures and roads found "
            "in it and the class, may be low; run the model longer before relying on the "
            "class.".format(reason, "; " + detail if detail else ""))


def summary_lines(nd, tm11, structs, roads, rails, dam_name, sources, one_d, extra=None):
    zt = zone_table(structs)
    L = []
    L.append("{} v{} - Hazard Classification Summary".format(PLUGIN_NAME, PLUGIN_VERSION))
    L.append("Dam: {}    Generated: {}".format(dam_name, time.strftime("%Y-%m-%d %H:%M")))
    L.append("Hydraulic model: {}".format("1D (depth > 2 ft rule)" if one_d
                                          else "2D (depth-velocity curves)"))
    L.append("")
    # 2.30.0: the plain-language result card first - the class, the one reason for it, what
    # would change it and the flags, for the dam owner and the county emergency manager who
    # read this file before the criteria tables (resultcard.py builds it)
    card = (extra or {}).get("card")
    if card:
        from . import resultcard as RC
        L.extend(RC.lines(card))
        L.append("")
    # v2.28: a flood model the Runner cut off marks the class line incomplete (the class itself
    # is not changed) and its sentence joins the data sources when the run did not add it
    cut = (extra or {}).get("flood_cut_off")
    suffix = ""
    if cut:
        suffix = "  |  incomplete: flood cut off ({})".format(
            str(cut.get("reason") or "reason not given").strip())
        note = flood_cut_off_note(cut)
        if note and note not in sources:
            sources = list(sources) + [note]
    L.append("ND HAZARD CLASSIFICATION (screening): {}{}".format(nd["cls"].upper(), suffix))
    L.append("  Basis: " + ND_POLICY_REF)
    for r in nd["reasons"]:
        L.append("  - " + r)
    if nd.get("criteria"):
        L.append("")
        L.append("ND STANDARDS SECTION 5.2 CRITERIA")
        L.extend(criteria_text(nd))
    L.append("")
    L.append("ACER TM 11 CLASSIFICATION: {}  (lives in jeopardy = {:.1f})".format(
        tm11["cls"].upper(), tm11["lij"]))
    L.append("  Basis: {} Table 1 (Low 0, Significant 1-6, High > 6)".format(TM11_REF))
    L.append("  Structures, high danger zone: {} persons; judgment zone: {} persons ({})".format(
        tm11["lij_structures_high"], tm11["lij_structures_judgment"],
        "included" if tm11["judgment_included"] else "not included"))
    L.append("  Motorists (estimate): high {:.1f}, judgment {:.1f}".format(
        tm11["lij_vehicles_high"], tm11["lij_vehicles_judgment"]))
    L.append("  Without judgment-zone items: {}".format(tm11["cls_without_judgment"]))
    for n in tm11["notes"]:
        L.append("  Note: " + n)
    L.append("")
    L.append("HABITABLE STRUCTURES BY ACER ZONE")
    for t in ("Habitable", "House on foundation", "Mobile home"):
        L.append("  {:<20} High {:>4}   Judgment {:>4}   Low (wet) {:>4}".format(
            t, zt.get((t, 1), 0), zt.get((t, 2), 0), zt.get((t, 3), 0)))
    unv = sum(1 for s in structs if s["habitable"] == "Unverified" and s.get("zone"))
    L.append("  Unverified structures in flooded area: {}".format(unv))
    L.append("")
    L.append("ROADS: {} overtopped stretch(es) - High {}, Medium {}, Low {}".format(
        len(roads), sum(r["nd_class"] == ND_HIGH for r in roads),
        sum(r["nd_class"] == ND_MEDIUM for r in roads),
        sum(r["nd_class"] == ND_LOW for r in roads)))
    L.append("RAIL: {} inundated stretch(es)".format(len(rails)))
    L.append("")
    extra = extra or {}
    for key, title in (("proposed", "PROPOSED DAM"),
                       ("scenarios", "SCENARIOS"), ("margin", "CLASSIFICATION MARGIN (5.3(f))"),
                       ("whatif", "WHAT WOULD CHANGE THE CLASS - THE ITEMS NEAREST A "
                                  "BOUNDARY (5.3(f))"),
                       ("nid", "NATIONAL INVENTORY OF DAMS - THIS DAM"),
                       ("changes", "CHANGES SINCE THE LAST RUN"),
                       ("method", "ACER ZONE METHOD"),
                       ("model_extent", "MODEL EXTENT CHECK"),
                       ("consequences", "CONSEQUENCES (screening)"),
                       ("hcom", "DSS-WISE HCOM CROSS-CHECK (population at risk)"),
                       ("popsource", "SECOND POPULATION SOURCE (census / gridded)"),
                       ("evacuation", "EVACUATION TIMING (screening)"),
                       ("sensitivity", "SENSITIVITY (depth / velocity scaling)"),
                       ("conseq_sensitivity", "CONSEQUENCE SENSITIVITY (screening range)"),
                       ("termination", "DOWNSTREAM TERMINATION OF THE INCREMENTAL HAZARD"),
                       ("access", "SOLE-ACCESS ROADS"),
                       ("dams", "DAMS IN SERIES (5.3(c))"),
                       ("lifelines", "LIFELINE / CRITICAL FACILITIES (5.2.2)"),
                       ("lifeline_detail", "LIFELINE DETAIL - ALTERNATIVE ROUTE AND A SECOND "
                                           "FACILITY (5.2.2)"),
                       ("history", "CLASSIFICATION HISTORY OF THIS DAM"),
                       ("crossings", "BRIDGES, CULVERTS AND CROSSINGS"),
                       ("future", "FUTURE DEVELOPMENT (5.3(b))"),
                       ("eap", "EMERGENCY ACTION PLAN OUTPUTS"),
                       ("vintage", "DATA VINTAGE"),
                       ("qa", "INVENTORY COMPLETENESS")):
        if extra.get(key):
            L.append(title)
            for x in extra[key]:
                L.append("  - " + x)
            L.append("")
    L.append("ITEMS REQUIRING ENGINEERING JUDGMENT")
    for j in nd["judgment"]:
        L.append("  - " + j)
    L.append("")
    L.append("DATA SOURCES")
    for s in sources:
        L.append("  - " + s)
    L.append("")
    L.append("This is a screening result produced from the listed data. The engineer "
             "remains responsible for the hazard classification.")
    return L


def write_summary(output_folder, lines, nd, tm11, transform_ctx):
    """Write Hazard_Classification_Summary.txt and a one-table XLSX/CSV."""
    txt = prepare_output_path(os.path.join(output_folder, "Hazard_Classification_Summary.txt"))
    with open(txt, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    lyr = create_memory_layer("Summary", WKB_NOGEOMETRY, QgsCoordinateReferenceSystem(),
                              [QgsField("Item", FT_STRING), QgsField("Value", FT_STRING)])
    rows = [("ND classification (screening)", nd["cls"]), ("ND basis", ND_POLICY_REF)]
    rows += [("ND reason", r) for r in nd["reasons"]]
    rows += [("ND {} {}".format(c["section"], c["criterion"]),
              "{} -> {}".format(c["found"], c["result"]) if c["found"] else c["result"])
             for c in nd.get("criteria", [])]
    rows += [("TM 11 classification", tm11["cls"]),
             ("TM 11 lives in jeopardy", "{:.1f}".format(tm11["lij"])),
             ("TM 11 without judgment-zone items", tm11["cls_without_judgment"])]
    rows += [("Judgment item", j) for j in nd["judgment"]]
    feats = []
    for k, v in rows:
        f = QgsFeature(lyr.fields())
        f.setAttributes([k, v])
        feats.append(f)
    lyr.dataProvider().addFeatures(feats)
    # v2.3.1: only csv/xlsx - no orphan GPKG left in outputs
    _ok, _res, paths = export_layer_to_formats(lyr, output_folder,
                                               "Hazard_Classification_Summary", transform_ctx,
                                               formats=("csv", "xlsx"))
    paths["txt"] = txt
    return paths

