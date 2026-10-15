# -*- coding: utf-8 -*-
"""2.30.0: lifeline detail - the alternative route and the second facility.

ND REG_05.2026 5.2.2 asks, in words, whether a lifeline facility is lost or isolated. Until
now the plugin answered "inundated" (facilities.py) and "sole access lost" (access.py) and
left the two questions a reviewer asks next unanswered:

    How far is the way round?   the shortest route that avoids the flooded roads, and how much
                                longer it is than the normal one
    Is there a second one?      another facility of the SAME KIND (fire, EMS, EOC, water,
                                wastewater) reachable within 15 minutes on the roads that stay
                                open

Both are answered on the road network evacuation.py already builds (its edges carry length,
travel time and the closure time that says which are flooded), so nothing new is read from a
raster. The core below works on plain lists, so it can be tested without QGIS:

    graph = {"n_nodes": N, "edges": [(u, v, length_ft, minutes, flooded), ...]}
    rows = detail(graph, facilities)      # facilities: {"name", "category", "node", ...}

Decisions:
  * "Within 15 minutes" is SECOND_FACILITY_MIN, a screening value chosen by the engineer (the
    15-minute response time the ND rule's wording implies), not a number from the standards -
    and it is measured on the DRY network at the model's own road speed, with no traffic, no
    turn delays and no dispatch time. The rows say so.
  * The alternative route is the shortest DRY route to the same destinations the normal route
    serves (the exit nodes - the places where the road leaves the model area, as access.py and
    evacuation.py define "out"). Its length minus the normal route's length is the detour. A
    facility with no dry route at all is ISOLATED - the answer the rule's "isolated" asks for.
  * A facility is reported when it is flooded OR isolated OR its detour exceeds
    DETOUR_REPORT_FT: a hundred feet of detour is not a consequence, and a reviewer should not
    have to read rows that say nothing.
  * Nothing is ever charged to a class here. These rows are detail for the engineer's review
    of 5.2.2; the class comes from facilities.py / access.py as before, so no result moves.
  * Pure Python (heapq, no numpy) and nothing raises.
"""

import csv
import heapq

SECOND_FACILITY_MIN = 15.0           # "a second facility of the same kind within 15 minutes"
DETOUR_REPORT_FT = 500.0             # a detour below this is not worth a row
INF = float("inf")
HEAD = ["Facility", "Category", "Status", "Normal_Route_ft", "Alternative_Route_ft",
        "Detour_ft", "Alternative_Route_min", "Second_Facility", "Second_Facility_min",
        "Second_Within_15min"]
# the lifeline kinds this detail is asked for (facilities.canonical_category spellings)
KINDS = ("Fire station", "EMS station", "Emergency operations center", "Law enforcement",
         "Hospital", "Water treatment", "Wastewater treatment")


def _edges(graph):
    return (graph or {}).get("edges") or ()


def _adjacency(graph, dry_only=False):
    """{node: [(other, length_ft, minutes)]} - the flooded edges left out when dry_only."""
    adj = {}
    for e in _edges(graph):
        u, v, length, minutes, flooded = (list(e) + [False])[:5]
        if dry_only and flooded:
            continue
        adj.setdefault(int(u), []).append((int(v), float(length), float(minutes)))
        adj.setdefault(int(v), []).append((int(u), float(length), float(minutes)))
    return adj


def shortest(graph, sources, dry_only=False, by="minutes"):
    """{node: (minutes, length_ft)} from the nearest of `sources` (a multi-source Dijkstra on
    travel time, or on length with by="length"). Unreachable nodes are left out."""
    adj = _adjacency(graph, dry_only)
    best = {}
    heap = []
    for s in sources or ():
        s = int(s)
        heap.append((0.0, 0.0, s))
    heapq.heapify(heap)
    while heap:
        cost, other, node = heapq.heappop(heap)
        if node in best:
            continue
        mins, length = (cost, other) if by == "minutes" else (other, cost)
        best[node] = (mins, length)
        for nxt, elen, emin in adj.get(node, ()):
            if nxt in best:
                continue
            step = emin if by == "minutes" else elen
            side = elen if by == "minutes" else emin
            heapq.heappush(heap, (cost + step, other + side, nxt))
    return best


def _pick(best, node):
    got = best.get(int(node)) if node is not None else None
    return got if got is not None else (INF, INF)


def _status(rec, isolated, detour_ft):
    if rec.get("flooded"):
        return "inundated"
    if isolated:
        return "isolated - no dry route out"
    if detour_ft and detour_ft > DETOUR_REPORT_FT:
        return "reached only by a longer route"
    if rec.get("cut_off"):
        return "access cut"
    return "reached normally"


def detail(graph, facilities, exits=None, second_minutes=SECOND_FACILITY_MIN,
           detour_report_ft=DETOUR_REPORT_FT):
    """One row per lifeline facility worth reporting. `facilities`: dicts with "name",
    "category", "node" (its nearest road node), optional "flooded" / "cut_off". `exits`: the
    nodes where the road leaves the model area (graph["exits"] when not given)."""
    graph = graph or {}
    exits = list(exits if exits is not None else (graph.get("exits") or ()))
    facs = [f for f in facilities or () if f.get("node") is not None]
    # both searches minimise LENGTH (the question is "how far is the way round"); the
    # minutes reported are the minutes of that same route, not of a faster one
    normal = shortest(graph, exits, dry_only=False, by="length") if exits else {}
    dry = shortest(graph, exits, dry_only=True, by="length") if exits else {}
    rows = []
    for rec in facs:
        node = int(rec["node"])
        _n_min, n_len = _pick(normal, node)
        d_min, d_len = _pick(dry, node)
        isolated = d_len == INF
        detour = None if isolated or n_len == INF else max(0.0, d_len - n_len)
        same = [f for f in facs if f is not rec
                and str(f.get("category") or "") == str(rec.get("category") or "")
                and not f.get("flooded")]
        second_name, second_min = "", INF
        if same:
            reach = shortest(graph, [node], dry_only=True, by="minutes")
            for f in same:
                mins = _pick(reach, int(f["node"]))[0]
                if mins < second_min:
                    second_min, second_name = mins, str(f.get("name") or "(unnamed)")
        rows.append({
            "name": str(rec.get("name") or "(unnamed)"), "category": str(rec.get("category")
                                                                        or ""),
            "flooded": bool(rec.get("flooded")), "cut_off": bool(rec.get("cut_off")),
            "isolated": isolated,
            "normal_ft": None if n_len == INF else n_len,
            "alt_ft": None if isolated else d_len,
            "alt_min": None if d_min == INF else d_min,
            "detour_ft": detour,
            "second": second_name, "second_min": None if second_min == INF else second_min,
            "second_within": bool(second_min <= float(second_minutes)),
            "status": _status(rec, isolated, detour),
        })
    keep = [r for r in rows if r["flooded"] or r["isolated"] or r["cut_off"]
            or (r["detour_ft"] or 0.0) > float(detour_report_ft)]
    keep.sort(key=lambda r: (not r["flooded"], not r["isolated"],
                             -(r["detour_ft"] or 0.0), r["name"]))
    return keep


def _ft(v):
    return "-" if v is None else "{:,.0f} ft".format(v)


def row_line(r):
    """One row as the report's sentence."""
    bits = ["{}{} - {}".format(r["name"], " ({})".format(r["category"]) if r["category"]
                               else "", r["status"])]
    if r["isolated"]:
        bits.append("no route out avoids the flooded roads")
    elif r["detour_ft"] is not None:
        bits.append("alternative route {} ({} longer than the normal route{})".format(
            _ft(r["alt_ft"]), _ft(r["detour_ft"]),
            ", {:.0f} min".format(r["alt_min"]) if r["alt_min"] is not None else ""))
    if r["second"]:
        bits.append("nearest other {}: {} at {:.0f} min - {}".format(
            (r["category"] or "facility").lower(), r["second"], r["second_min"] or 0.0,
            "within {:g} min".format(SECOND_FACILITY_MIN) if r["second_within"]
            else "NOT within {:g} min".format(SECOND_FACILITY_MIN)))
    else:
        bits.append("no second facility of the same kind in the data")
    return "; ".join(bits)


def lines(rows, note=""):
    """The summary / report section for the lifeline detail."""
    if not rows:
        return ["No lifeline facility is inundated, isolated or reached only by a longer "
                "route{}.".format(" (" + note + ")" if note else "")]
    out = [row_line(r) for r in rows]
    out.append("Routes are measured on the mapped road network at the model's road speed, "
               "avoiding every road the flood makes impassable: no traffic, no turn or "
               "dispatch delay. '{:g} minutes' is a screening value chosen by the engineer, "
               "not a value of the ND standards.".format(SECOND_FACILITY_MIN))
    if note:
        out.append(note)
    return out


def table_rows(rows):
    out = []
    for r in rows or ():
        out.append([r["name"], r["category"], r["status"],
                    None if r["normal_ft"] is None else round(r["normal_ft"]),
                    None if r["alt_ft"] is None else round(r["alt_ft"]),
                    None if r["detour_ft"] is None else round(r["detour_ft"]),
                    None if r["alt_min"] is None else round(r["alt_min"], 1),
                    r["second"], None if r["second_min"] is None else round(r["second_min"], 1),
                    "Yes" if r["second_within"] else "No"])
    return out


def write_csv(path, rows):
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(HEAD)
        for row in table_rows(rows):
            w.writerow(["" if v is None else v for v in row])
    return path


# ------------------------------------------------- the suite's own road graph --
def graph_from_road_graph(rg, facility_points=(), snap_ft=2000.0):
    """(graph, facility nodes) from an evacuation.RoadGraph: its edges with their length and
    travel time, flooded where the edge has a closure time, and its exit (safe) nodes. Needs
    numpy only through the RoadGraph it is given; `facility_points` are (x, y) in the graph's
    CRS, snapped to the nearest node within snap_ft."""
    import math

    import numpy as np
    edges = []
    closing = np.isfinite(np.asarray(rg.eclose, dtype=np.float64))
    for i in range(int(rg.n_edges)):
        edges.append((int(rg.eu[i]), int(rg.ev[i]), float(rg.elen_ft[i]), float(rg.etau[i]),
                      bool(closing[i]) or bool(rg.eunk[i])))
    exits = [int(n) for n in np.nonzero(np.asarray(rg.safe))[0].tolist()]
    graph = {"n_nodes": int(rg.n_nodes), "edges": edges, "exits": exits}
    nodes = []
    kx = float(getattr(rg, "kx", 1.0) or 1.0)
    nx_, ny_ = np.asarray(rg.node_x, dtype=np.float64), np.asarray(rg.node_y, dtype=np.float64)
    limit = float(snap_ft) * kx
    for pt in facility_points or ():
        if pt is None:
            nodes.append(None)
            continue
        try:
            d = np.hypot(nx_ - float(pt[0]), ny_ - float(pt[1]))
            i = int(np.argmin(d))
            nodes.append(i if math.isfinite(d[i]) and d[i] <= limit else None)
        except Exception:                                        # noqa - a bad point
            nodes.append(None)
    return graph, nodes
