# -*- coding: utf-8 -*-
"""USGS 3D Hydrography Program (3DHP) flowlines and waterbodies in the suite (release 1.19).

The suite's streams and waterbodies come from the 3DHP service instead of the National Hydrography
Dataset (NHD) service:

    https://hydro.nationalmap.gov/arcgis/rest/services/3DHP_all/MapServer    layer 50 Flowline,
                                                                             layer 60 Waterbody

3DHP names its fields differently (gnisidlabel, featuretypelabel, ...). Everything in the suite that
reads streams or ponds (the travel-time channel trace, the Runner's culvert finder, the pond finder,
the Screening's pool area) knows the NHD field names gnis_name, gnis_id, ftype, fcode, areasqkm and
lengthkm, so every 3DHP feature fetched is given those properties as well: normalise_flowline() and
normalise_waterbody() add them and keep every 3DHP property as it came (id3dhp, mainstemid,
featuretype, streamorder ...). A layer written by the older releases (NHD names) and one written
now (3DHP names) therefore read the same way.

Pack data set names: "3DHP_flowlines" and "3DHP_waterbodies". An offline pack built before 1.19 holds
"NHD_flowlines" and "NHD_waterbodies"; ALIASES lets a reader answer to either name, so old packs keep
working with the new plugins and new packs with older code that still asks for the NHD names.
"""
import datetime

BASE = "https://hydro.nationalmap.gov/arcgis/rest/services/3DHP_all/MapServer"
FLOWLINE_LAYER = 50
WATERBODY_LAYER = 60
FLOWLINE_URL = "{}/{}".format(BASE, FLOWLINE_LAYER)
WATERBODY_URL = "{}/{}".format(BASE, WATERBODY_LAYER)

FLOWLINES = "3DHP_flowlines"
WATERBODIES = "3DHP_waterbodies"
OLD_FLOWLINES = "NHD_flowlines"
OLD_WATERBODIES = "NHD_waterbodies"
# pack data set names that mean the same thing, newest first
FLOWLINE_NAMES = (FLOWLINES, OLD_FLOWLINES)
WATERBODY_NAMES = (WATERBODIES, OLD_WATERBODIES)
ALIASES = {FLOWLINES: OLD_FLOWLINES, OLD_FLOWLINES: FLOWLINES,
           WATERBODIES: OLD_WATERBODIES, OLD_WATERBODIES: WATERBODIES}

# fields the pack keeps (a 3DHP flowline has about 40; these are the ones a user or a later
# release can use: identity, name, type, network order). Names are lower case as the service has them.
FLOW_FIELDS = ("id3dhp,featuredate,mainstemid,gnisid,gnisidlabel,featuretype,featuretypelabel,"
               "lengthkm,waterbodyid3dhp,flowdirection,streamorder,streamlevel,hydrosequence,"
               "dnhydrosequence,uphydrosequence,levelpath,arbolatesum,workunitid")
WB_FIELDS = ("id3dhp,featuredate,mainstemid,gnisid,gnisidlabel,featuretype,featuretypelabel,"
             "areasqkm,workunitid")

# NHD feature type codes (the codes traveltime._FTYPE_RANK knows: 460 stream / river, 558 artificial
# path, 334 connector, 336 canal / ditch)
FT_STREAM, FT_ARTIFICIAL, FT_CONNECTOR, FT_CANAL, FT_PIPELINE, FT_UNDERGROUND = \
    460, 558, 334, 336, 428, 420
FT_LAKE, FT_RESERVOIR, FT_SWAMP, FT_ICE, FT_PLAYA, FT_INUNDATION = 390, 436, 466, 378, 361, 445

# (text in the 3DHP feature type label, NHD code); first match wins. The label is what the service
# prints (Channel Line, Canal, Surface Connector, Waterbody Connector ...); a code for a label not
# listed here is 0 (unknown), which the travel-time trace ranks after the known kinds.
_FLOW_LABELS = (("waterbody connector", FT_ARTIFICIAL), ("artificial", FT_ARTIFICIAL),
                ("surface connector", FT_CONNECTOR), ("connector", FT_CONNECTOR),
                ("channel", FT_STREAM), ("stream", FT_STREAM), ("river", FT_STREAM),
                ("canal", FT_CANAL), ("ditch", FT_CANAL), ("pipeline", FT_PIPELINE),
                ("underground", FT_UNDERGROUND), ("subsurface", FT_UNDERGROUND))
_WB_LABELS = (("reservoir", FT_RESERVOIR), ("lake", FT_LAKE), ("pond", FT_LAKE),
              ("river", FT_STREAM), ("stream", FT_STREAM), ("canal", FT_CANAL),
              ("ditch", FT_CANAL), ("swamp", FT_SWAMP), ("marsh", FT_SWAMP), ("ice mass", FT_ICE),
              ("playa", FT_PLAYA), ("inundation", FT_INUNDATION), ("ocean", FT_LAKE),
              ("great lake", FT_LAKE))
# the service's numeric feature type codes, used only when the label is missing
_FLOW_CODES = {1: FT_STREAM, 2: FT_CANAL, 4: FT_CONNECTOR, 5: FT_ARTIFICIAL}
_WB_CODES = {3: FT_LAKE}


def ftype_of(label, code=None, flowline=True):
    """NHD feature type code for a 3DHP feature type label (code: the service's number)."""
    s = str(label or "").strip().lower()
    for key, val in (_FLOW_LABELS if flowline else _WB_LABELS):
        if key in s:
            return val
    try:
        return (_FLOW_CODES if flowline else _WB_CODES).get(int(code), 0)
    except (TypeError, ValueError):
        return 0


def iso_date(v):
    """A 3DHP date (epoch milliseconds, as ArcGIS sends dates) as YYYY-MM-DD; text stays as it is."""
    if isinstance(v, bool) or v is None or v == "":
        return v
    if isinstance(v, (int, float)):
        try:
            return datetime.datetime.fromtimestamp(v / 1000.0, datetime.timezone.utc).strftime(
                "%Y-%m-%d")
        except (OverflowError, OSError, ValueError):
            return v
    return v


def _lower(props):
    return {str(k).lower(): v for k, v in (props or {}).items()}


def _name(p):
    n = p.get("gnisidlabel")
    return n if n not in ("", None) else None


def normalise_flowline(feature):
    """The feature with the NHD properties (gnis_name, gnis_id, ftype, fcode, lengthkm) added to its
    3DHP properties (kept; featuredate as a YYYY-MM-DD date). A feature that already has NHD
    properties (an old pack, an NHD layer) is returned as it is."""
    props = feature.get("properties") if isinstance(feature, dict) else None
    if props is None:
        return feature
    low = _lower(props)
    if "featuretype" not in low and "featuretypelabel" not in low and "id3dhp" not in low:
        return feature                                         # not a 3DHP feature
    out = dict(props)
    ft = ftype_of(low.get("featuretypelabel"), low.get("featuretype"), True)
    out["gnis_name"] = _name(low)
    out["gnis_id"] = low.get("gnisid")
    out["ftype"] = ft
    out["fcode"] = ft * 100 if ft else None          # NHD: type code x 100 = "no further attribute"
    if "featuredate" in low:
        for k in list(out):
            if k.lower() == "featuredate":
                out[k] = iso_date(out[k])
    f = dict(feature)
    f["properties"] = out
    return f


def normalise_waterbody(feature):
    """Like normalise_flowline for a waterbody: gnis_name, gnis_id, ftype, fcode (areasqkm is the
    service's own field)."""
    props = feature.get("properties") if isinstance(feature, dict) else None
    if props is None:
        return feature
    low = _lower(props)
    if "featuretype" not in low and "featuretypelabel" not in low and "id3dhp" not in low:
        return feature
    out = dict(props)
    ft = ftype_of(low.get("featuretypelabel"), low.get("featuretype"), False)
    out["gnis_name"] = _name(low)
    out["gnis_id"] = low.get("gnisid")
    out["ftype"] = ft
    out["fcode"] = ft * 100 if ft else None
    for k in list(out):
        if k.lower() == "featuredate":
            out[k] = iso_date(out[k])
    f = dict(feature)
    f["properties"] = out
    return f


def normalise_flowlines(features):
    return [normalise_flowline(f) for f in features]


def normalise_waterbodies(features):
    return [normalise_waterbody(f) for f in features]


def is_3dhp_url(url):
    return "3dhp_all" in str(url or "").lower()


def layer_of_url(url):
    """50 / 60 for the 3DHP flowline / waterbody layer address, else 0."""
    u = str(url or "").rstrip("/").lower()
    if not is_3dhp_url(u):
        return 0
    tail = u.rsplit("/", 1)[-1]
    return int(tail) if tail in (str(FLOWLINE_LAYER), str(WATERBODY_LAYER)) else 0


def normaliser_for_url(url):
    """The function that gives features of this layer address the NHD properties (None when the
    address is not a 3DHP flowline / waterbody layer)."""
    n = layer_of_url(url)
    return normalise_flowlines if n == FLOWLINE_LAYER else \
        normalise_waterbodies if n == WATERBODY_LAYER else None


def normaliser_for_name(name):
    """Same by data set name ("3DHP_flowlines" / "NHD_flowlines" ...), for a layer fetched from
    a 3DHP address."""
    return normalise_flowlines if name in FLOWLINE_NAMES else \
        normalise_waterbodies if name in WATERBODY_NAMES else None


def real_name(name, available):
    """`name`, or its alias when only the alias is in `available` (a collection of names)."""
    if name in available:
        return name
    other = ALIASES.get(name)
    return other if other is not None and other in available else name


def first_available(names, available):
    """The first of `names` that is in `available`, trying each one's alias too; None when none."""
    for n in names:
        r = real_name(n, available)
        if r in available:
            return r
    return None


def dedupe(names):
    """`names` without a name whose alias came earlier in the list (a pack that holds both the old
    NHD_ and the new 3DHP_ data set is read once)."""
    out = []
    for n in names:
        if ALIASES.get(n) in out:
            continue
        out.append(n)
    return out
