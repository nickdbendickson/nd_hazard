"""OpenStreetMap layers for the ND dam suite's offline pack.

Reads the Geofabrik North Dakota extract (.osm.pbf, every tag) with the GDAL that comes with
QGIS and writes the five layers the dam suite reads - OSM_lifelines, OSM_crossings,
OSM_campgrounds, OSM_trailer_parks, OSM_places - each as its own GeoPackage (EPSG:4326).
The tag rules are those of the ND Dam Hazard Helper plugin (osm_parse.py); the same code is the
stand-alone script nd_osm_download.py (osm-download folder of the project). No QGIS imports.
"""

import json
import math
import os
import re
import time

ND_BBOX = (-104.1489, 45.8351, -96.4543, 49.1000)     # the offline pack's box (lon / lat)


def log(msg):
    pass


# --------------------------------------------------------------------------------------------
# Tag rules: copied from ND Dam Hazard Helper 2.29.0 (constants.py / osm_parse.py) so the
# layers hold what the plugins' Overpass download would give.
# --------------------------------------------------------------------------------------------
LIFELINE_TAGS = [
    ("power", "substation", "Electric substation"),
    ("power", "plant", "Power plant"),
    ("man_made", "water_works", "Water treatment"),
    ("man_made", "wastewater_plant", "Wastewater treatment"),
    ("amenity", "hospital", "Hospital"),
    ("amenity", "fire_station", "Fire station"),
    ("amenity", "police", "Law enforcement"),
    ("emergency", "ambulance_station", "EMS station"),
    ("social_facility", "nursing_home", "Nursing home"),
    ("amenity", "nursing_home", "Nursing home"),
    ("amenity", "school", "School"),
    ("amenity", "kindergarten", "Child care"),
    ("amenity", "childcare", "Child care"),
]
CAMP_TAGS = [("tourism", "camp_site", "Campground"), ("tourism", "caravan_site", "RV park")]
CAMP_POINT_RADIUS_FT = 150.0
PLACE_KINDS = ("city", "town", "village", "hamlet")
TRAILER_PARK_CATEGORY = "Mobile-home park"
TRAILER_PARK_NAME = re.compile(
    r"\b(?:mobile homes? (?:park|court|community)|trailer (?:park|court)"
    r"|manufactured (?:home|housing) (?:park|community)|mhp)\b", re.I)
DEAD_RAIL = ("abandoned", "disused", "razed", "dismantled", "proposed", "construction")

# (layer, pack label, geometry, [(field, type)])  type: "s" text, "i" integer
LAYERS = [
    ("OSM_lifelines", "OpenStreetMap lifeline facilities", "Point",
     [("name", "s"), ("category", "s"), ("osm_id", "s"), ("operator", "s")]),
    ("OSM_crossings", "OpenStreetMap culverts / fords / rail bridges", "Point",
     [("name", "s"), ("category", "s"), ("waterway", "s"), ("railway", "s"),
      ("operator", "s"), ("osm_id", "s")]),
    ("OSM_campgrounds", "OpenStreetMap campgrounds / RV parks", "Polygon",
     [("name", "s"), ("category", "s"), ("tourism", "s"), ("sites", "i"), ("seasonal", "s"),
      ("shape", "s"), ("osm_id", "s")]),
    ("OSM_trailer_parks", "OpenStreetMap mobile-home parks", "Polygon",
     [("name", "s"), ("category", "s"), ("match", "s"), ("landuse", "s"), ("shape", "s"),
      ("osm_id", "s")]),
    ("OSM_places", "OpenStreetMap cities / towns / villages", "Point",
     [("name", "s"), ("place", "s"), ("population", "i"), ("osm_id", "s")]),
]
GROUP = {"OSM_lifelines": "lifelines", "OSM_crossings": "roads", "OSM_campgrounds": "structures",
         "OSM_trailer_parks": "structures", "OSM_places": "structures"}
WHAT = {"OSM_lifelines": "power=substation/plant, man_made=water_works/wastewater_plant, "
                         "amenity=hospital/fire_station/police/nursing_home/school/kindergarten/"
                         "childcare, emergency=ambulance_station, social_facility=nursing_home",
        "OSM_crossings": "tunnel=culvert, ford=yes, bridge=low_water_crossing, railway bridges",
        "OSM_campgrounds": "tourism=camp_site / caravan_site",
        "OSM_trailer_parks": "residential=trailer_park, or landuse=residential named as a park",
        "OSM_places": "place=city/town/village/hamlet"}

# Quick pre-check, so the millions of roads and buildings are skipped unparsed: a feature is
# looked at only when one of these columns holds one of the values, or its other_tags text
# holds one of the key / value snippets.
_VALUES = set(v for _k, v, _c in LIFELINE_TAGS) | set(v for _k, v, _c in CAMP_TAGS) | \
    set(PLACE_KINDS)
_COLUMNS = {"amenity": _VALUES, "man_made": _VALUES, "tourism": _VALUES, "place": _VALUES,
            "landuse": {"residential"}}
_SNIPPETS = tuple('"{}"=>"{}"'.format(k, v) for k, v, _c in LIFELINE_TAGS + CAMP_TAGS) + (
    '"place"=>"', '"landuse"=>"residential"', '"residential"=>"trailer_park"',
    '"tunnel"=>"culvert"', '"ford"=>"yes"', '"bridge"=>"low_water_crossing"')


# ---------------------------------------------------------------------------- tag parsing --
_HSTORE = re.compile(r'"((?:[^"\\]|\\.)*)"=>"((?:[^"\\]|\\.)*)"')
_SKIP_FIELDS = {"osm_id", "osm_way_id", "other_tags", "z_order", "osm_type", "address",
                "is_in", "land_area"}


def _unescape(s):
    return s.replace('\\"', '"').replace("\\\\", "\\")


def feature_tags(feat, field_names):
    """All OSM tags of a GDAL OSM-driver feature: its named columns plus other_tags."""
    tags = {}
    for i, n in field_names:
        if n in _SKIP_FIELDS:
            continue
        v = feat.GetField(i)
        if v not in (None, ""):
            tags[n] = str(v)
    other = feat.GetField("other_tags") if "other_tags" in (n for _i, n in field_names) else None
    if other:
        for k, v in _HSTORE.findall(other):
            tags[_unescape(k)] = _unescape(v)
    return tags


def crossing_category(tags, etype):
    """osm_parse.crossing_category, limited to the element types the plugin's Overpass query
    asks for (culverts, low-water crossings and rail bridges: ways; fords: any element)."""
    if tags.get("tunnel") == "culvert":
        return "Culvert" if etype == "way" else None
    if tags.get("ford") == "yes":
        return "Low-water crossing / ford"
    if tags.get("bridge") == "low_water_crossing":
        return "Low-water crossing / ford" if etype == "way" else None
    rw = tags.get("railway")
    if etype == "way" and rw and rw not in DEAD_RAIL and tags.get("bridge") not in (None, "no"):
        return "Railroad bridge"
    return None


def is_trailer_park(tags):
    if tags.get("residential") == "trailer_park":
        return "residential=trailer_park"
    name = re.sub(r"\s+", " ", re.sub(r"[_\-/.,]+", " ", str(tags.get("name") or ""))).strip()
    if tags.get("landuse") == "residential" and TRAILER_PARK_NAME.search(name):
        return "name"
    return None


def _circle(lon, lat, radius_ft, n=16):
    r_m = radius_ft * 0.3048
    dlat = r_m / 111320.0
    dlon = r_m / (111320.0 * max(0.05, math.cos(math.radians(lat))))
    ring = [[lon + dlon * math.cos(2 * math.pi * k / n), lat + dlat * math.sin(2 * math.pi * k / n)]
            for k in range(n)]
    return ring + [ring[0]]


def _sites(tags):
    for k in ("capacity:pitches", "capacity:caravans", "capacity:tents", "capacity"):
        try:
            cap = int(float(str(tags.get(k)).split(";")[0]))
            if cap > 0:
                return cap
        except (TypeError, ValueError):
            pass
    return None


# ------------------------------------------------------------------------------ geometry --
def _r(v):
    return round(v, 7)


def _rings_of(geom):
    """[[outer, inner...], ...] lon / lat polygons of an OGR polygon / multipolygon, or of a
    closed line (a closed way GDAL did not make an area)."""
    gj = json.loads(geom.ExportToJson())
    t, c = gj.get("type"), gj.get("coordinates")
    if t == "Polygon":
        return [c]
    if t == "MultiPolygon":
        return list(c)
    if t == "LineString" and len(c) >= 4 and c[0] == c[-1]:
        return [[c]]
    return []


def _clean_rings(rings):
    return [[[_r(x), _r(y)] for x, y, *_z in ring] for ring in rings]


def _center(geom):
    """Point of an element as Overpass's "out center" gives it: a node's position, else the
    middle of the bounding box."""
    if geom.GetGeometryName().upper() == "POINT":
        return geom.GetX(), geom.GetY()
    xmin, xmax, ymin, ymax = geom.GetEnvelope()
    return (xmin + xmax) / 2.0, (ymin + ymax) / 2.0


def _in_box(lon, lat, box):
    return box[0] <= lon <= box[2] and box[1] <= lat <= box[3]


def _point(lon, lat):
    return {"type": "Point", "coordinates": [_r(lon), _r(lat)]}


# -------------------------------------------------------------------------- extraction --
def _element(layer_name, feat):
    """(OSM element type, id) of a feature of the GDAL OSM driver's layers."""
    if layer_name == "points":
        return "node", feat.GetField("osm_id")
    if layer_name == "lines":
        return "way", feat.GetField("osm_id")
    if layer_name == "multipolygons":
        rid = feat.GetField("osm_id")
        return ("relation", rid) if rid else ("way", feat.GetField("osm_way_id"))
    return "relation", feat.GetField("osm_id")             # multilinestrings, other_relations


def extract(pbf_path, out, box=ND_BBOX, tick=None, say=None):
    """Read one .osm.pbf (or .osm) and add its features to out {layer: {osm_id: feature}}.
    tick(features_read), when given, is called every few seconds (it may raise to cancel);
    say(text) reports progress (default: print)."""
    say = say or log
    from osgeo import gdal
    gdal.UseExceptions()
    gdal.SetConfigOption("OSM_MAX_TMPFILE_SIZE", "1000")       # MB of node index in memory
    gdal.SetConfigOption("OGR_INTERLEAVED_READING", "YES")
    ds = gdal.OpenEx(pbf_path, gdal.OF_VECTOR)
    fields = {}
    for i in range(ds.GetLayerCount()):
        lyr = ds.GetLayer(i)
        d = lyr.GetLayerDefn()
        fields[lyr.GetName()] = [(j, d.GetFieldDefn(j).GetName()) for j in range(d.GetFieldCount())]
    checks = {}
    for lname, fn in fields.items():
        idx = {n: i for i, n in fn}
        checks[lname] = ([(idx[c], v) for c, v in _COLUMNS.items() if c in idx],
                         idx.get("other_tags"), idx.get("railway"))
    t0, seen, shown = time.time(), 0, time.time()
    while True:
        feat, lyr = ds.GetNextFeature()
        if feat is None:
            break
        seen += 1
        if not seen % 20000 and time.time() - shown > 5:
            if tick:
                tick(seen)
            if time.time() - shown > 15:
                shown = time.time()
                say("  {:,} OSM features read ({:.0f} s)".format(seen, shown - t0))
        lname = lyr.GetName()
        if lname == "multilinestrings":
            continue
        fn = fields[lname]
        cols, oti, rwi = checks[lname]
        other = feat.GetField(oti) if oti is not None else None
        if not (any(feat.GetField(i) in vals for i, vals in cols)
                or (other and (any(w in other for w in _SNIPPETS)
                               or (rwi is not None and '"bridge"=>' in other
                                   and feat.GetField(rwi))))):
            continue
        geom = feat.GetGeometryRef()
        if geom is None or geom.IsEmpty():
            continue
        tags = feature_tags(feat, fn)
        etype, eid = _element(lname, feat)
        oid = "{}/{}".format(etype, eid)
        _classify(tags, etype, oid, geom, out, box)
    say("  {:,} OSM features read in {:.0f} s".format(seen, time.time() - t0))


def _classify(tags, etype, oid, geom, out, box):
    lon, lat = _center(geom)
    # lifelines: point at the node / centre of the way or relation
    cat = next((c for k, v, c in LIFELINE_TAGS if tags.get(k) == v), None)
    if cat and _in_box(lon, lat, box):
        out["OSM_lifelines"][oid] = {"geometry": _point(lon, lat), "properties": {
            "name": tags.get("name") or tags.get("operator") or "", "category": cat,
            "osm_id": oid, "operator": tags.get("operator") or ""}}
    # stream crossings
    cat = crossing_category(tags, etype)
    if cat and _in_box(lon, lat, box):
        out["OSM_crossings"][oid] = {"geometry": _point(lon, lat), "properties": {
            "name": tags.get("name") or tags.get("ref") or "", "category": cat,
            "waterway": tags.get("waterway") or "", "railway": tags.get("railway") or "",
            "operator": tags.get("operator") or "", "osm_id": oid}}
    # populated places (nodes)
    if etype == "node" and tags.get("place") in PLACE_KINDS and _in_box(lon, lat, box):
        try:
            pop = int(float(str(tags.get("population")).replace(",", "")))
        except (TypeError, ValueError):
            pop = None
        out["OSM_places"][oid] = {"geometry": _point(lon, lat), "properties": {
            "name": tags.get("name") or "", "place": tags["place"], "population": pop,
            "osm_id": oid}}
    # campgrounds / RV parks: mapped areas as mapped, anything else a 150 ft circle
    cat = next((c for k, v, c in CAMP_TAGS if tags.get(k) == v), None)
    if cat and _in_box(lon, lat, box):
        props = {"name": tags.get("name") or tags.get("operator") or "", "category": cat,
                 "tourism": tags.get("tourism") or "", "sites": _sites(tags),
                 "seasonal": tags.get("seasonal") or ""}
        polys = _rings_of(geom) if etype != "node" else []
        if not polys:
            if geom.GetGeometryName().upper() == "LINESTRING" and geom.GetPointCount():
                pts = [geom.GetPoint_2D(i) for i in range(geom.GetPointCount())]
                lon = sum(p[0] for p in pts) / len(pts)        # open way: mean of its points
                lat = sum(p[1] for p in pts) / len(pts)
            polys = [[_circle(lon, lat, CAMP_POINT_RADIUS_FT)]]
            shape = "point (circle of {:g} ft)".format(CAMP_POINT_RADIUS_FT)
        else:
            shape = "mapped area"
        for k, rings in enumerate(polys):
            p = dict(props, shape=shape, osm_id=oid if k == 0 else "{}#{}".format(oid, k + 1))
            out["OSM_campgrounds"][p["osm_id"]] = {
                "geometry": {"type": "Polygon", "coordinates": _clean_rings(rings)},
                "properties": p}
    # mobile-home / trailer parks: areas only
    match = is_trailer_park(tags)
    if match and etype in ("way", "relation") and _in_box(lon, lat, box):
        for k, rings in enumerate(_rings_of(geom)):
            o = oid if k == 0 else "{}#{}".format(oid, k + 1)
            out["OSM_trailer_parks"][o] = {
                "geometry": {"type": "Polygon", "coordinates": _clean_rings(rings)},
                "properties": {"name": tags.get("name") or "", "category": TRAILER_PARK_CATEGORY,
                               "match": match, "landuse": tags.get("landuse") or "",
                               "shape": "mapped area", "osm_id": o}}


# ------------------------------------------------------------------------------ writing --
def write_gpkg(path, name, label, gtype, schema, features):
    """One layer to its own GeoPackage (EPSG:4326, spatial index)."""
    from osgeo import ogr, osr
    if os.path.exists(path):
        os.remove(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    try:
        srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    except AttributeError:
        pass
    ds = ogr.GetDriverByName("GPKG").CreateDataSource(path)
    lyr = ds.CreateLayer(name, srs, ogr.wkbPoint if gtype == "Point" else ogr.wkbPolygon,
                         ["SPATIAL_INDEX=YES", "DESCRIPTION=" + label])
    for f, t in schema:
        lyr.CreateField(ogr.FieldDefn(f, ogr.OFTInteger64 if t == "i" else ogr.OFTString))
    lyr.StartTransaction()
    for feat in features:
        o = ogr.Feature(lyr.GetLayerDefn())
        for f, _t in schema:
            v = feat["properties"].get(f)
            if v is not None:
                o.SetField(f, v)
        o.SetGeometry(ogr.CreateGeometryFromJson(json.dumps(feat["geometry"])))
        lyr.CreateFeature(o)
    lyr.CommitTransaction()
    ds = None
    return path


def build_layers(pbf_path, out_dir, tick=None, say=None):
    """Write the five GeoPackages into out_dir; returns {layer: feature count}."""
    layers = {name: {} for name, *_r in LAYERS}
    extract(pbf_path, layers, tick=tick, say=say)
    counts = {}
    for name, label, gtype, schema in LAYERS:
        write_gpkg(os.path.join(out_dir, name + ".gpkg"), name, label, gtype, schema,
                   layers[name].values())
        counts[name] = len(layers[name])
    return counts


def write_summary(out_dir, counts, version, url):
    with open(os.path.join(out_dir, "osm_layers.json"), "w", encoding="utf-8") as f:
        json.dump({"source": url, "version": version, "box": ND_BBOX, "counts": counts,
                   "what": WHAT, "made": time.strftime("%Y-%m-%dT%H:%M:%S")}, f, indent=1)
