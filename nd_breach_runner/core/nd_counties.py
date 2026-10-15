# -*- coding: utf-8 -*-
"""North Dakota county outlines for picking a county (the same file in ND Dam Breach Screening and
ND Dam Breach Runner).

nd_counties.geojson beside this file: the 53 counties of the suite offline pack's ND_counties
(NDGISHub county boundaries), EPSG:4326, simplified to about 10 m, NAME only. Pure Python (no QGIS,
no GDAL): a dam is in a county when its point is inside the county's outline. A point on a shared
line goes to the first county by name.
"""
import json
import os

DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "nd_counties.geojson")
ALL = "(all of North Dakota)"           # the choice that keeps every county
_cache = {}


def _load():
    if "c" not in _cache:
        with open(DATA, encoding="utf-8") as fh:
            gj = json.load(fh)
        out = {}
        for f in gj.get("features", []):
            name = str((f.get("properties") or {}).get("NAME") or "").strip()
            g = f.get("geometry") or {}
            polys = g.get("coordinates") or []
            if g.get("type") == "Polygon":
                polys = [polys]
            elif g.get("type") != "MultiPolygon":
                continue
            rings = [[[(float(x), float(y)) for x, y, *_z in ring] for ring in poly] for poly in polys]
            xs = [p[0] for poly in rings for p in poly[0]]
            ys = [p[1] for poly in rings for p in poly[0]]
            out.setdefault(name, {"polys": [], "box": None})
            out[name]["polys"] += rings
            b = out[name]["box"]
            nb = (min(xs), min(ys), max(xs), max(ys))
            out[name]["box"] = nb if b is None else (min(b[0], nb[0]), min(b[1], nb[1]),
                                                    max(b[2], nb[2]), max(b[3], nb[3]))
        _cache["c"] = out
    return _cache["c"]


def names():
    """The 53 county names, sorted."""
    return sorted(_load())


def normalize(name):
    """A county name as written in names() ('cass', 'Cass County', ' CASS ' -> 'Cass'), or ""."""
    n = str(name or "").strip().lower()
    if n.endswith(" county"):
        n = n[:-7].strip()
    for c in _load():
        if c.lower() == n:
            return c
    return ""


def parse(text):
    """Comma-separated county names -> [names()] ([] = all of North Dakota). Raises ValueError
    naming a county that is not in North Dakota."""
    out, bad = [], []
    for part in str(text or "").split(","):
        p = part.strip()
        if not p or p == ALL:
            continue
        c = normalize(p)
        if c:
            if c not in out:
                out.append(c)
        else:
            bad.append(p)
    if bad:
        raise ValueError("not a North Dakota county: {}".format(", ".join(bad)))
    return out


def _in_ring(x, y, ring):
    inside = False
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i]
        xj, yj = ring[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-300) + xi:
            inside = not inside
        j = i
    return inside


def contains(name, lon, lat):
    """True when the lon / lat point is inside county `name`."""
    c = _load().get(name)
    if c is None or lon is None or lat is None:
        return False
    w, s, e, n = c["box"]
    if not (w <= lon <= e and s <= lat <= n):
        return False
    for poly in c["polys"]:
        if _in_ring(lon, lat, poly[0]) and not any(_in_ring(lon, lat, h) for h in poly[1:]):
            return True
    return False


def county_of(lon, lat):
    """The county a lon / lat point is in, or "" (outside North Dakota)."""
    for name in names():
        if contains(name, lon, lat):
            return name
    return ""


def box(counties, pad_deg=0.0):
    """(west, south, east, north) around the counties (lon / lat), padded; None for none."""
    bs = [_load()[c]["box"] for c in counties if c in _load()]
    if not bs:
        return None
    return (min(b[0] for b in bs) - pad_deg, min(b[1] for b in bs) - pad_deg,
            max(b[2] for b in bs) + pad_deg, max(b[3] for b in bs) + pad_deg)


def features(counties=None):
    """GeoJSON features (EPSG:4326) of the counties (all when None) - for a clip layer."""
    with open(DATA, encoding="utf-8") as fh:
        gj = json.load(fh)
    want = set(counties) if counties else None
    return [f for f in gj.get("features", [])
            if want is None or (f.get("properties") or {}).get("NAME") in want]
