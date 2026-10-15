"""Does this dam's breach flood reach another dam? (1.16.0)

A breach flood that arrives at a second reservoir is the one thing a single-dam study cannot
answer for itself, and it is the question a reviewer asks first when two dams sit on the same
creek. The flood polygons the Runner and Screening already write are enough to answer it: any
dam of the inventory whose point falls inside the flood is downstream of this one (a flood only
goes downstream), minus the dam being run and anything within a short radius of it.

    hits = downstream(dams, rings, source=(lon, lat), source_id="DWR-2801")
    text = summary_text(hits)
    job  = next_job(hit, dam_folder, hydrograph="...\\outflow.csv")

`rings` is the flood footprint as [[(lon, lat), ...], ...] - one ring per polygon part, outer
rings only; a caller with QGIS can pass the polygon's exterior rings straight in. Point in
polygon is ray casting, with a point exactly on an edge counted as inside.

Distances are great-circle miles, which is close enough at ND latitudes for "how far
downstream" and needs no projection.

`next_job` builds the hand-off for running that dam next: its folder, and the upstream run's
outflow hydrograph as the inflow, so the second dam is modelled with water arriving rather than
from its own reservoir alone. It only describes the job - nothing is run or written here.

Pure Python standard library.
"""
import math
import os

EARTH_MI = 3958.7613
# a dam this close to the source is the source itself (a second inventory record for the same
# structure, or the dam line's own point)
SAME_DAM_MI = 0.25
ROLE_INFLOW = "upstream_breach"


def _pt(d):
    """(lon, lat) from a dam dict, or None."""
    for lon_key, lat_key in (("lon", "lat"), ("longitude", "latitude"), ("x", "y")):
        try:
            lon, lat = d.get(lon_key), d.get(lat_key)
        except AttributeError:
            return None
        if lon is not None and lat is not None:
            try:
                return float(lon), float(lat)
            except (TypeError, ValueError):
                continue
    return None


def miles(a, b):
    """Great-circle miles between two (lon, lat) points."""
    lon1, lat1 = math.radians(a[0]), math.radians(a[1])
    lon2, lat2 = math.radians(b[0]), math.radians(b[1])
    dlon, dlat = lon2 - lon1, lat2 - lat1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_MI * math.asin(min(1.0, math.sqrt(h)))


def _on_segment(p, a, b, tol=1e-12):
    cross = (b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0])
    if abs(cross) > tol:
        return False
    return min(a[0], b[0]) - tol <= p[0] <= max(a[0], b[0]) + tol and \
        min(a[1], b[1]) - tol <= p[1] <= max(a[1], b[1]) + tol


def in_ring(point, ring):
    """Ray casting; a point on an edge counts as inside."""
    if not ring or len(ring) < 3:
        return False
    x, y = point
    n = len(ring)
    inside = False
    for i in range(n):
        a, b = ring[i], ring[(i + 1) % n]
        if _on_segment(point, a, b):
            return True
        if (a[1] > y) != (b[1] > y):
            try:
                xx = a[0] + (y - a[1]) * (b[0] - a[0]) / (b[1] - a[1])
            except ZeroDivisionError:               # pragma: no cover - guarded by the test above
                continue
            if x < xx:
                inside = not inside
    return inside


def in_flood(point, rings):
    return any(in_ring(point, r) for r in rings or [])


def downstream(dams, rings, source=None, source_id="", same_dam_mi=SAME_DAM_MI):
    """[{"dam_id", "name", "lon", "lat", "distance_mi", "existing_class"}] for every inventory
    dam inside the flood, nearest first. The source dam and anything within `same_dam_mi` of it
    are left out."""
    out = []
    sid = str(source_id or "").strip().lower()
    for d in dams or []:
        p = _pt(d)
        if p is None:
            continue
        did = str(d.get("dam_id") or d.get("id") or d.get("DAM_ID") or "").strip()
        if sid and did.lower() == sid:
            continue
        dist = miles(source, p) if source else None
        if source and dist is not None and dist <= same_dam_mi:
            continue
        if not in_flood(p, rings):
            continue
        out.append({"dam_id": did, "name": str(d.get("name") or d.get("DAM_NAME") or ""),
                    "lon": p[0], "lat": p[1], "distance_mi": dist,
                    "existing_class": str(d.get("existing_class") or d.get("hazard_class")
                                          or "")})
    out.sort(key=lambda h: (h["distance_mi"] if h["distance_mi"] is not None else 9e9,
                            h["dam_id"]))
    return out


def summary_text(hits, source_name=""):
    who = " of {}".format(source_name) if source_name else ""
    if not hits:
        return "The breach flood{} does not reach another dam in the inventory.".format(who)
    first = hits[0]
    text = "The breach flood{} reaches {} other dam(s): ".format(who, len(hits)) + ", ".join(
        "{}{}".format(h["dam_id"] or h["name"] or "?",
                      " ({:.1f} mi)".format(h["distance_mi"]) if h["distance_mi"] is not None
                      else "") for h in hits[:6]) + (", ..." if len(hits) > 6 else "")
    text += ". The nearest is {}{}: model it next with this run's outflow as its inflow, " \
            "because its own reservoir is not the only water arriving.".format(
                first["dam_id"] or first["name"] or "?",
                " " + first["name"] if first["dam_id"] and first["name"] else "")
    return text


def next_job(hit, dams_folder, hydrograph="", source_id="", note=""):
    """The hand-off for running one downstream dam next (a plain dict; nothing is written).

    {"dam_id", "folder", "inflow", "inflow_role", "from_dam", "note"} - `folder` is where the
    dam's folder would be under the Dams folder, `inflow` the upstream run's outflow hydrograph
    to use as this dam's inflow."""
    did = (hit or {}).get("dam_id") or (hit or {}).get("name") or ""
    folder = os.path.join(dams_folder, did) if dams_folder and did else ""
    return {"dam_id": did, "folder": folder, "inflow": hydrograph or "",
            "inflow_role": ROLE_INFLOW, "from_dam": source_id or "",
            "note": note or ("Inflow is the breach outflow of {}; the reservoir's own inflow is "
                             "added to it.".format(source_id) if source_id else "")}


def jobs(hits, dams_folder, hydrograph="", source_id=""):
    return [next_job(h, dams_folder, hydrograph, source_id) for h in hits or []]


# ----------------------------------------------------------------- footprint --
def rings_from_geojson(obj):
    """Outer rings of a GeoJSON geometry / feature / collection, as [[(lon, lat), ...], ...].

    Takes a Polygon, a MultiPolygon, a Feature or a FeatureCollection; holes are ignored (a dam
    inside a hole of a flood polygon is not a real case, and ignoring them is the conservative
    direction: it keeps the dam in the list)."""
    out = []

    def geom(g):
        if not isinstance(g, dict):
            return
        t = str(g.get("type") or "")
        if t == "Polygon":
            rings = g.get("coordinates") or []
            if rings:
                out.append([(float(c[0]), float(c[1])) for c in rings[0]])
        elif t == "MultiPolygon":
            for poly in g.get("coordinates") or []:
                if poly:
                    out.append([(float(c[0]), float(c[1])) for c in poly[0]])
        elif t == "GeometryCollection":
            for g2 in g.get("geometries") or []:
                geom(g2)

    if isinstance(obj, dict):
        t = str(obj.get("type") or "")
        if t == "FeatureCollection":
            for f in obj.get("features") or []:
                geom((f or {}).get("geometry"))
        elif t == "Feature":
            geom(obj.get("geometry"))
        else:
            geom(obj)
    return out


def bbox(rings):
    """(min lon, min lat, max lon, max lat) of the footprint, or None."""
    xs = [p[0] for r in rings or [] for p in r]
    ys = [p[1] for r in rings or [] for p in r]
    if not xs or not ys:
        return None
    return min(xs), min(ys), max(xs), max(ys)
