# -*- coding: utf-8 -*-
"""v2.6.1: OpenStreetMap (Overpass API) queries and answer parsing - pure
Python, no QGIS. Shared by download.py (the plugin) and build_nd_pack.py
(the offline data pack builder, which runs outside QGIS), so a pack holds
exactly the features a live download would give."""

import math
import re

try:                                     # inside the plugin package
    from .constants import (OSM_LIFELINE_TAGS, OSM_CROSSING_QUERIES, OSM_CAMP_TAGS,
                            CAMP_POINT_RADIUS_FT, OSM_PLACE_RADIUS_FT)
except ImportError:                      # build_nd_pack.py: plugin folder on sys.path
    from constants import (OSM_LIFELINE_TAGS, OSM_CROSSING_QUERIES, OSM_CAMP_TAGS,  # noqa
                           CAMP_POINT_RADIUS_FT, OSM_PLACE_RADIUS_FT)


def _bbox(extent_4326):
    xmin, ymin, xmax, ymax = extent_4326
    return "{:.6f},{:.6f},{:.6f},{:.6f}".format(ymin, xmin, ymax, xmax)   # S,W,N,E


def overpass_query(extent_4326, tags=OSM_LIFELINE_TAGS):
    xmin, ymin, xmax, ymax = extent_4326
    bbox = "{:.6f},{:.6f},{:.6f},{:.6f}".format(ymin, xmin, ymax, xmax)   # S,W,N,E
    parts = "".join('nwr["{}"="{}"]({});'.format(k, v, bbox) for k, v, _c in tags)
    return "[out:json][timeout:50];({});out center tags;".format(parts)


def overpass_to_features(data, tags=OSM_LIFELINE_TAGS):
    """GeoJSON point features (node position or way / relation centre) with
    name, category and the OSM id; elements without a position are skipped."""
    out = []
    for el in (data or {}).get("elements", []) if isinstance(data, dict) else []:
        t = el.get("tags") or {}
        cat = next((c for k, v, c in tags if t.get(k) == v), None)
        if cat is None:
            continue
        if "lat" in el and "lon" in el:
            lat, lon = el["lat"], el["lon"]
        else:
            c = el.get("center") or {}
            lat, lon = c.get("lat"), c.get("lon")
        if lat is None or lon is None:
            continue
        out.append({"type": "Feature",
                    "geometry": {"type": "Point", "coordinates": [lon, lat]},
                    "properties": {"name": t.get("name") or t.get("operator") or "",
                                   "category": cat,
                                   "osm_id": "{}/{}".format(el.get("type", ""), el.get("id", "")),
                                   "operator": t.get("operator") or ""}})
    return out


_DEAD_RAIL = ("abandoned", "disused", "razed", "dismantled", "proposed", "construction")


def crossing_category(tags):
    """Category of an OSM stream crossing from its tags (None: not one)."""
    t = tags or {}
    if t.get("tunnel") == "culvert":
        return "Culvert"
    if t.get("ford") == "yes" or t.get("bridge") == "low_water_crossing":
        return "Low-water crossing / ford"
    rw = t.get("railway")
    if rw and rw not in _DEAD_RAIL and t.get("bridge") not in (None, "no"):
        return "Railroad bridge"
    return None


def _circle_ll(lon, lat, radius_ft, n=16):
    """Closed lon/lat ring approximating a circle (small radii only)."""
    r_m = radius_ft * 0.3048
    dlat = r_m / 111320.0
    dlon = r_m / (111320.0 * max(0.05, math.cos(math.radians(lat))))
    ring = [[lon + dlon * math.cos(2 * math.pi * k / n), lat + dlat * math.sin(2 * math.pi * k / n)]
            for k in range(n)]
    return ring + [ring[0]]


def camp_features(data, tags=OSM_CAMP_TAGS, point_radius_ft=CAMP_POINT_RADIUS_FT):
    """GeoJSON POLYGON features of campgrounds / RV parks from an Overpass
    answer: closed ways as mapped, nodes / relations / open ways as a circle
    of `point_radius_ft` around their position."""
    out = []
    for el in (data or {}).get("elements", []) if isinstance(data, dict) else []:
        t = el.get("tags") or {}
        cat = next((c for k, v, c in tags if t.get(k) == v), None)
        if cat is None:
            continue
        ring = None
        geom = el.get("geometry")
        if isinstance(geom, list) and len(geom) >= 4:
            pts = [[g.get("lon"), g.get("lat")] for g in geom if isinstance(g, dict)]
            pts = [p for p in pts if p[0] is not None and p[1] is not None]
            if len(pts) >= 4 and pts[0] == pts[-1]:
                ring = pts
        if ring is None:
            if "lat" in el and "lon" in el:
                lat, lon = el["lat"], el["lon"]
            elif el.get("center"):
                lat, lon = el["center"].get("lat"), el["center"].get("lon")
            elif isinstance(geom, list) and geom:
                ok = [g for g in geom if isinstance(g, dict) and g.get("lat") is not None]
                if not ok:
                    continue
                lat = sum(g["lat"] for g in ok) / len(ok)
                lon = sum(g["lon"] for g in ok) / len(ok)
            else:
                continue
            if lat is None or lon is None:
                continue
            ring = _circle_ll(lon, lat, point_radius_ft)
            shape = "point (circle of {:g} ft)".format(point_radius_ft)
        else:
            shape = "mapped area"
        cap = None
        for k in ("capacity:pitches", "capacity:caravans", "capacity:tents", "capacity"):
            try:
                cap = int(float(str(t.get(k)).split(";")[0]))
                if cap > 0:
                    break
            except (TypeError, ValueError):
                cap = None
        out.append({"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [ring]},
                    "properties": {"name": t.get("name") or t.get("operator") or "",
                                   "category": cat, "tourism": t.get("tourism") or "",
                                   "sites": cap, "seasonal": t.get("seasonal") or "",
                                   "shape": shape,
                                   "osm_id": "{}/{}".format(el.get("type", ""),
                                                            el.get("id", ""))}})
    return out


def camps_query(extent_4326, tags=OSM_CAMP_TAGS):
    xmin, ymin, xmax, ymax = extent_4326
    bbox = "{:.6f},{:.6f},{:.6f},{:.6f}".format(ymin, xmin, ymax, xmax)
    ways = "".join('way["{}"="{}"]({});'.format(k, v, bbox) for k, v, _c in tags)
    other = "".join('node["{0}"="{1}"]({2});relation["{0}"="{1}"]({2});'.format(k, v, bbox)
                    for k, v, _c in tags)
    return ("[out:json][timeout:50];({})->.w;.w out body geom;({})->.o;.o out body center;"
            .format(ways, other))


# ------------------------------------------ v2.9: mobile-home / trailer parks --
# OSM wiki, Tag:residential=trailer_park (status "in use"): "Residential area
# consisting of prefabricated houses or static caravans (mobile homes) on
# small lots"; requires landuse=residential; areas and multipolygon relations
# only. Areas tagged landuse=residential whose NAME says a park are taken too.
TRAILER_PARK_CATEGORY = "Mobile-home park"
TRAILER_PARK_NAME = re.compile(
    r"\b(?:mobile homes? (?:park|court|community)|trailer (?:park|court)"
    r"|manufactured (?:home|housing) (?:park|community)|mhp)\b", re.I)
# the server-side pre-filter (POSIX regex, case-insensitive); the exact
# words are checked by is_trailer_park()
_TRAILER_NAME_PREFILTER = "mobile home|trailer|manufactured|mhp"


def _norm_name(name):
    return re.sub(r"\s+", " ", re.sub(r"[_\-/.,]+", " ", str(name or ""))).strip()


def is_trailer_park(tags):
    """How an OSM element is recognised as a mobile-home / trailer park -
    "residential=trailer_park" or "name" - or None."""
    t = tags or {}
    if t.get("residential") == "trailer_park":
        return "residential=trailer_park"
    if t.get("landuse") == "residential" and TRAILER_PARK_NAME.search(
            _norm_name(t.get("name"))):
        return "name"
    return None


def trailer_parks_query(extent_4326, timeout=50):
    """Overpass query for mobile-home / trailer parks (areas): ways and
    relations with residential=trailer_park, or landuse=residential with a
    name that may say a park. "out body geom" returns the way outlines and
    the relation members' outlines."""
    bbox = _bbox(extent_4326)
    parts = []
    for el in ("way", "relation"):
        parts.append('{}["residential"="trailer_park"]({});'.format(el, bbox))
        parts.append('{}["landuse"="residential"]["name"~"{}",i]({});'.format(
            el, _TRAILER_NAME_PREFILTER, bbox))
    return "[out:json][timeout:{}];({});out body geom;".format(int(timeout), "".join(parts))


def _pts(geom):
    pts = [[g.get("lon"), g.get("lat")] for g in (geom or []) if isinstance(g, dict)]
    return [p for p in pts if p[0] is not None and p[1] is not None]


def _join_rings(lines):
    """Closed rings from way outlines joined end to end (multipolygon
    members); pieces that never close are dropped."""
    todo = [list(ln) for ln in lines if len(ln) >= 2]
    rings = []
    while todo:
        cur = todo.pop(0)
        grown = True
        while cur[0] != cur[-1] and grown:
            grown = False
            for i, ln in enumerate(todo):
                if ln[0] == cur[-1]:
                    cur += ln[1:]
                elif ln[-1] == cur[-1]:
                    cur += list(reversed(ln))[1:]
                elif ln[-1] == cur[0]:
                    cur = ln[:-1] + cur
                elif ln[0] == cur[0]:
                    cur = list(reversed(ln))[:-1] + cur
                else:
                    continue
                todo.pop(i)
                grown = True
                break
        if len(cur) >= 4 and cur[0] == cur[-1]:
            rings.append(cur)
    return rings


def _in_ring(pt, ring):
    x, y = pt
    inside = False
    for (x0, y0), (x1, y1) in zip(ring[:-1], ring[1:]):
        if (y0 > y) != (y1 > y) and x < (x1 - x0) * (y - y0) / ((y1 - y0) or 1e-300) + x0:
            inside = not inside
    return inside


def trailer_park_features(data):
    """GeoJSON POLYGON features of the mobile-home / trailer parks in an
    Overpass answer (trailer_parks_query): closed ways as mapped, multipolygon
    relations from their outer / inner member outlines (one feature per outer
    ring). Nodes and open ways are not areas and are skipped. Properties:
    name, category, match ("residential=trailer_park" / "name"), osm_id."""
    out = []
    for el in (data or {}).get("elements", []) if isinstance(data, dict) else []:
        if not isinstance(el, dict):
            continue
        t = el.get("tags") or {}
        match = is_trailer_park(t)
        if match is None or el.get("type") not in ("way", "relation"):
            continue
        polys = []
        if el.get("type") == "way":
            ring = _pts(el.get("geometry"))
            if len(ring) >= 4 and ring[0] == ring[-1]:
                polys.append([ring])
        else:
            members = [m for m in el.get("members") or [] if isinstance(m, dict)
                       and m.get("type") == "way"]
            outers = _join_rings(_pts(m.get("geometry")) for m in members
                                 if m.get("role") in ("outer", ""))
            inners = _join_rings(_pts(m.get("geometry")) for m in members
                                 if m.get("role") == "inner")
            for o in outers:
                polys.append([o] + [r for r in inners if _in_ring(r[0], o)])
        oid = "{}/{}".format(el.get("type", ""), el.get("id", ""))
        for k, rings in enumerate(polys):
            out.append({"type": "Feature",
                        "geometry": {"type": "Polygon", "coordinates": rings},
                        "properties": {"name": t.get("name") or "",
                                       "category": TRAILER_PARK_CATEGORY, "match": match,
                                       "landuse": t.get("landuse") or "",
                                       "shape": "mapped area",
                                       "osm_id": oid if k == 0 else "{}#{}".format(oid, k + 1)}})
    return out


def place_features(data):
    """GeoJSON points of populated places (city / town / village / hamlet)."""
    out = []
    for el in (data or {}).get("elements", []) if isinstance(data, dict) else []:
        t = el.get("tags") or {}
        kind = t.get("place")
        if kind not in OSM_PLACE_RADIUS_FT or "lat" not in el or "lon" not in el:
            continue
        pop = None
        try:
            pop = int(float(str(t.get("population")).replace(",", "")))
        except (TypeError, ValueError):
            pop = None
        out.append({"type": "Feature",
                    "geometry": {"type": "Point", "coordinates": [el["lon"], el["lat"]]},
                    "properties": {"name": t.get("name") or "", "place": kind,
                                   "population": pop,
                                   "osm_id": "node/{}".format(el.get("id", ""))}})
    return out


def crossings_query(extent_4326, queries=OSM_CROSSING_QUERIES, timeout=50):
    """Overpass query for culverts, fords / low-water crossings and railway
    bridges (OSM_CROSSING_QUERIES)."""
    bbox = _bbox(extent_4326)
    return "[out:json][timeout:{}];({});out center tags;".format(
        int(timeout), "".join("{}({});".format(f, bbox) for f, _c in queries))


def crossing_features(data):
    """GeoJSON points of the stream crossings in an Overpass answer."""
    feats = []
    for el in data.get("elements", []) if isinstance(data, dict) else []:
        t = el.get("tags") or {}
        cat = crossing_category(t)
        if cat is None:
            continue
        if "lat" in el and "lon" in el:
            lat, lon = el["lat"], el["lon"]
        else:
            c = el.get("center") or {}
            lat, lon = c.get("lat"), c.get("lon")
        if lat is None or lon is None:
            continue
        feats.append({"type": "Feature",
                      "geometry": {"type": "Point", "coordinates": [lon, lat]},
                      "properties": {"name": t.get("name") or t.get("ref") or "",
                                     "category": cat,
                                     "waterway": t.get("waterway") or "",
                                     "railway": t.get("railway") or "",
                                     "operator": t.get("operator") or "",
                                     "osm_id": "{}/{}".format(el.get("type", ""),
                                                              el.get("id", ""))}})
    return feats


def places_query(extent_4326, timeout=50):
    """Overpass query for cities, towns, villages and hamlets. "out body"
    prints the node position (lat / lon); "out tags" would not."""
    return ('[out:json][timeout:{}];node["place"~"^(city|town|village|hamlet)$"]({});'
            'out body;'.format(int(timeout), _bbox(extent_4326)))


def with_timeout(query, timeout):
    """The same Overpass query with another [timeout:...] (the pack builder
    asks for much larger areas than a study area)."""
    import re
    return re.sub(r"\[timeout:\d+\]", "[timeout:{}]".format(int(timeout)), query, count=1)
