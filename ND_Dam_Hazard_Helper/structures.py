# -*- coding: utf-8 -*-
"""Habitable-structure inventory for the hazard classification.

1. Merge building outlines (OSM) with point inventories (NSI, FEMA, ND GIS
   Hub address points, NSI download) inside the study area (raster extent
   + 25 ft); points inside an outline describe that building; a point just
   outside an outline is moved onto it (address points placed at the
   driveway / parcel: up to ADDRESS_SNAP_FT; others: STRUCTURE_MERGE_DIST_FT)
   when that outline has no point of the same source yet; points from
   different sources within STRUCTURE_MERGE_DIST_FT are the same building.
2. Decide habitability (ND REG_05.2026 5.2.1.1 definition: dwellings,
   trailers and mobile homes, buildings regularly occupied by workers or the
   public, recreational facilities with overnight stays) and the ACER TM 11
   structure type (house on foundation vs mobile home).
3. For each structure, pair depth and velocity cell by cell over the
   outline plus the 5-ft edge buffer, classify with the curve for its type
   and keep the worst zone (not max depth combined with max velocity from
   different cells).

ACER type (v2.9 - deterministic, in this order; Type_Basis names the evidence):
 a. Source evidence ("strong"): NSI occtype RES2 or NSI bldgtype H (NSI
    technical documentation: H = Manufactured; where the NSI has no parcel
    data the construction type is drawn from Hazus distributions, so H alone
    is a modelled value), manufactured / mobile-home / trailer words in the
    attributes (KW_MOBILE), a NENA Place_Type of kind "mobile" (PLACE_TYPES:
    mobile home, trailer park / court, manufactured home community ...).
 b. Park / lot evidence: an ND GIS Hub (NENA) unit designator of a lot or
    space in a park ("Lot 12", "Trlr 4", "Space 7", "Spc 7" - words in
    MOBILE_LOT_UNIT_WORDS; switch: mobile_lot_units) and a position inside an
    OpenStreetMap mobile-home park polygon (mark_mobile_parks).
 c. Footprint shape (switch: mobile_shape, screening parameters chosen by the
    engineer, defaults MOBILE_*): only when a. and b. give nothing, for an
    outline whose oriented minimum bounding box is at most
    mobile_max_width_ft wide, mobile_min_length_ft..mobile_max_length_ft long
    and at least mobile_min_ratio long-to-wide (a single-wide manufactured
    home). Explicit house words (KW_HOUSE_EXPLICIT) win over the shape: the
    type is not switched and the structure is flagged (type_conflict).
 The evidence of a. and b. makes a Dwelling or Unverified structure a mobile
 home (explicit house words then only flag type_conflict, the stricter
 mobile-home curve is kept). A structure read as Occupied non-residential
 (e.g. an NSI COM occupancy) is a mobile home only on RESIDENTIAL mobile-
 home evidence of a. (NSI RES2, a mobile Place_Type, KW_MOBILE_HOME words) -
 then it is flagged (mobile_occupied); park / lot / shape evidence never
 types an occupied building (park office, club house). rec["mobile_rule"]
 is "park_lot" / "shape" when b. / c. alone made the type (listed for review).

Habitability evidence (v2.20.1):
 - FEMA USA Structures OCC_CLS / PRIM_OCC / PROP_ADDR describe the PARCEL and
   are copied to every building on it, so they are no evidence that a
   building is habitable (kept as FEMA_Category / Address and used to group
   the buildings of one parcel).
 - On a parcel whose ND GIS Hub (911) address point is on one building, the
   other buildings are its outbuildings: one that only NSI calls residential
   is Unverified (NSI 2026 gives the parcel's RES record to farm sheds), one
   that no source calls habitable is Not habitable (_mark_parcel_sites).
 - A 911 address point with no building outline within ADDRESS_SNAP_FT and no
   NSI structure is Unverified (field access, vacant lot, tower, utility).
"""

import math
import re

import numpy as np

from qgis.core import (
    QgsFeature, QgsField, QgsGeometry, QgsCoordinateTransform, QgsDistanceArea,
    QgsFeatureRequest, QgsPointXY,
)

from .compat import NULL, WKB_MULTIPOLYGON, WKB_POINT, FT_INT, FT_DOUBLE, FT_STRING
from .constants import (
    CLASS_HIGH, CLASS_JUDGMENT, ZONE_LABELS,
    OSM_EDGE_BUFFER_FT, STRUCTURES_FAMILY, TM11_PERSONS_PER_DWELLING, ND_1D_DEPTH_FT,
    DEFAULT_MIN_UNVERIFIED_AREA_FT2, DEFAULT_POINT_RADIUS_FT,
    STRUCTURE_MERGE_DIST_FT, PROGRESS_INTERVAL, FEATURE_BATCH_SIZE, ADDRESS_SNAP_FT,
    CLASS_LOW, DEFAULT_NEAR_THRESHOLD_FT,
)
from .helpers import (
    log_message, LimitedLogger, create_memory_layer, transform_geometry,
    buffer_distance_in_layer_units, classify_values, add_features_batched,
    is_null, transform_xy, curve_pair, CancelledByUser, strip_rows, finite_area_ft2,
)
from .processing import point_from_geometry
from .extent import distance_xy_in_crs, center_latitude, is_geographic, is_web_mercator
from .hazard import zone_margins, zone_label
from . import fastsample as FS

TYPE_HOUSE = "House on foundation"
TYPE_MOBILE = "Mobile home"
# v2.5.1: a structure with an ND GIS Hub site address. Habitable by rule and
# evaluated on the house-on-foundation curve (TM 11 Fig. 2) unless a source
# shows a manufactured home (then TYPE_MOBILE, the stricter Fig. 3 curve).
TYPE_HABITABLE = "Habitable"
ACER_TYPES = (TYPE_HABITABLE, TYPE_HOUSE, TYPE_MOBILE)


def curve_type(acer_type):
    """The TM 11 curve behind an ACER type: mobile-home curve for mobile
    homes, house-on-foundation curve for everything else."""
    return TYPE_MOBILE if acer_type == TYPE_MOBILE else TYPE_HOUSE


# FEMA Hazus occupancy classes (the NSI occtype codes), for the FEMA_Category
# reference column
HAZUS_OCCUPANCY = {
    "RES1": "Single family dwelling", "RES2": "Manufactured housing",
    "RES3": "Multi family dwelling", "RES4": "Temporary lodging",
    "RES5": "Institutional dormitory", "RES6": "Nursing home",
    "COM1": "Retail trade", "COM2": "Wholesale trade", "COM3": "Personal and repair services",
    "COM4": "Professional / technical services", "COM5": "Banks", "COM6": "Hospital",
    "COM7": "Medical office / clinic", "COM8": "Entertainment and recreation",
    "COM9": "Theaters", "COM10": "Parking", "IND1": "Heavy industry", "IND2": "Light industry",
    "IND3": "Food / drugs / chemicals", "IND4": "Metals / minerals processing",
    "IND5": "High technology", "IND6": "Construction", "AGR1": "Agriculture",
    "REL1": "Church / membership organization", "GOV1": "General government services",
    "GOV2": "Emergency response", "EDU1": "Grade school", "EDU2": "College / university",
}
_FEMA_FIELDS = ("occ_cls", "prim_occ")        # FEMA USA Structures occupancy
# v2.20.1: FEMA USA Structures copies the PARCEL's occupancy and address to
# every building on the parcel (a detached garage on a house lot reads
# "Residential / Single Family Dwelling", every barn and grain bin of a farm
# carries the farm's address). These fields are kept for reference
# (FEMA_Category, Address) and to group the buildings of one parcel; they are
# never read as evidence that a building is habitable.
_FEMA_PARCEL_TEXT = ("occ_cls", "prim_occ", "sec_occ")
_FEMA_PARCEL_ADDR = ("prop_addr", "prop_zip")


def hazus_label(occtype):
    """'RES1 Single family dwelling' for an NSI occtype such as 'RES1-1SNB'
    or 'RES3A'; the code alone when unknown."""
    code = str(occtype or "").upper().split("-")[0].strip()
    base = code if code in HAZUS_OCCUPANCY else code.rstrip("ABCDEF")
    label = HAZUS_OCCUPANCY.get(base)
    return "{} {}".format(base, label) if label else code

CAT_DWELLING = "Dwelling"
CAT_OCCUPIED = "Occupied non-residential"
CAT_UNVERIFIED = "Unverified"
CAT_NOT_HABITABLE = "Not habitable"

OUT_PROBABLE = "Probable loss of life"
OUT_JUDGMENT = "Judgment zone"
OUT_ECONOMIC = "Inundated (economic loss)"
OUT_DRY = "Not inundated"
# ND REG_05.2026 5.2.2: habitable structures inundated "to any degree" are a
# consequence. Only model noise below this depth is left out (the same 0.1 ft
# "any depth" tolerance as core/nd_hazard.py wet_ft in the Runner / Screening).
CONSEQUENCE_DEPTH_FT = 0.1
OUT_TRACE = "Below consequence depth"

def _kw(*words):
    return re.compile(r"\b(?:%s)\b" % "|".join(re.escape(w) for w in words))


KW_MOBILE = _kw("manufactured", "manufactured home", "mobile home", "mobile",
                "trailer", "static caravan", "caravan", "mobile home park",
                "trailer park")
KW_DWELLING = _kw("residential", "residence", "dwelling", "house", "home", "detached",
                  "semidetached house", "apartments", "apartment", "bungalow", "cabin",
                  "farmhouse", "farm", "duplex", "condominium", "condo", "townhouse",
                  "terrace", "dormitory", "single family", "multi family", "hotel",
                  "motel", "nursing home", "lodge", "houseboat")
KW_OCCUPIED = _kw("commercial", "industrial", "office", "retail", "school", "church",
                  "hospital", "government", "public", "civic", "warehouse", "education",
                  "assembly", "religious", "fire station", "police", "clinic", "college",
                  "university", "kindergarten", "restaurant", "store", "shop", "factory",
                  "manufacturing", "place of worship", "campground", "camp site",
                  "resort", "community centre", "community center", "library", "bank",
                  "supermarket", "service station", "fuel", "worship")
KW_NONHAB = _kw("non habitable", "garage", "garages", "shed", "barn", "silo", "grain bin", "bin",
                "greenhouse", "carport", "roof", "storage tank", "tank", "hut", "stable",
                "cowshed", "sty", "farm auxiliary", "outbuilding", "quonset",
                "utility and misc", "transformer tower", "ruins", "construction",
                "bunker silo", "kiosk", "toilets", "shelter")
KW_LIFELINE = _kw("fire station", "police", "hospital", "emergency", "water treatment",
                  "wastewater", "sewage", "substation", "power plant", "pumping station",
                  "dispatch")
KW_HOUSE_EXPLICIT = _kw("house", "detached", "single family", "farmhouse", "bungalow")
# v2.9: words that say a RESIDENTIAL manufactured / mobile home (not just
# "mobile" or "trailer", which also name phone shops and trailer dealers):
# they keep the mobile-home type on a building read as occupied non-residential
KW_MOBILE_HOME = _kw("manufactured home", "manufactured homes", "manufactured housing",
                     "mobile home", "mobile homes", "mobile residence", "static caravan",
                     "house trailer", "trailer home", "trailer house", "mobile home park",
                     "mobile home court", "trailer park", "trailer court")

# v2.9: manufactured-home screening. These are SCREENING PARAMETERS chosen by
# the engineer (defaults only, not regulatory numbers): the transportable
# width of a single-wide manufactured home and the length range / length-to-
# width ratio of its outline.
MOBILE_MAX_WIDTH_FT = 18.0
MOBILE_MIN_RATIO = 2.5
MOBILE_MIN_LENGTH_FT = 30.0
MOBILE_MAX_LENGTH_FT = 90.0
# NENA unit designators of a lot / space in a mobile-home park (screening
# choice): "Lot 12", "LOT 3", "Trlr 4", "Space 7", "Spc 7", "Lot #12"
MOBILE_LOT_UNIT_WORDS = ("lot", "trlr", "trailer", "space", "spc")


def _lot_unit_re(words):
    return re.compile(r"(?:^|[^a-z0-9])(?:%s)\.?\s*#?\s*-?\s*(?:\d+[a-z]?|[a-z]\d*)(?![a-z])"
                      % "|".join(re.escape(w) for w in words), re.I)


_LOT_UNIT = _lot_unit_re(MOBILE_LOT_UNIT_WORDS)


def is_lot_unit(unit, words=None):
    """True when a NENA unit text names a lot / space in a park ("Lot 12")."""
    if not unit:
        return False
    rx = _LOT_UNIT if words is None else _lot_unit_re(words)
    return bool(rx.search(str(unit)))


# keyword arguments of _interpret / reinterpret for the manufactured-home rules
MOBILE_RULE_DEFAULTS = {
    "mobile_shape": True, "mobile_max_width_ft": MOBILE_MAX_WIDTH_FT,
    "mobile_min_ratio": MOBILE_MIN_RATIO, "mobile_min_length_ft": MOBILE_MIN_LENGTH_FT,
    "mobile_max_length_ft": MOBILE_MAX_LENGTH_FT, "mobile_lot_units": True,
}


def mobile_rules_from_cfg(cfg):
    """The manufactured-home rule arguments from a settings dict (same key
    names as MOBILE_RULE_DEFAULTS; missing / unusable values -> defaults)."""
    out = dict(MOBILE_RULE_DEFAULTS)
    for k, dflt in MOBILE_RULE_DEFAULTS.items():
        v = (cfg or {}).get(k)
        if v is None:
            continue
        try:
            out[k] = bool(v) if isinstance(dflt, bool) else float(v)
        except (TypeError, ValueError):
            pass
    return out


def single_wide_shape(dims, max_width_ft=MOBILE_MAX_WIDTH_FT, min_ratio=MOBILE_MIN_RATIO,
                      min_length_ft=MOBILE_MIN_LENGTH_FT, max_length_ft=MOBILE_MAX_LENGTH_FT):
    """True when an outline of (width_ft, length_ft) has the proportions of a
    single-wide manufactured home under the screening parameters."""
    if not dims:
        return False
    try:
        w, ln = sorted(float(v) for v in dims)
    except (TypeError, ValueError):
        return False
    if not (math.isfinite(w) and math.isfinite(ln)) or w <= 0:
        return False
    return (w <= float(max_width_ft) and ln / w >= float(min_ratio)
            and float(min_length_ft) <= ln <= float(max_length_ft))

_TEXT_HINTS = ("occ", "use", "type", "class", "building", "amenity", "tourism",
               "landuse", "residential", "descr", "category", "struct", "bldg", "landmk")
_ADDRESS_HINTS = ("addr", "address", "add_num", "fulladd", "st_num", "house_num")
_NSI_FIELDS = ("occtype", "bldgtype", "fd_id", "sqft", "pop2amu65", "pop2amo65",
               "pop2pmu65", "pop2pmo65", "val_struct", "val_cont", "found_ht", "found_type",
               "num_story")
# NENA NG9-1-1 site / structure address points (ND GIS Hub). Street-name
# parts are never read as building descriptions ("LSt_Type=TER" is a street
# type, not a terrace house); "Building" is a sub-address there, not the
# OSM building tag.
_NENA_MARKERS = ("site_nguid", "add_number", "addnum_pre", "st_name", "placement",
                 "place_type", "msagcomm")
_STREET_FIELD = re.compile(r"^(l?st_|street|road|msag)")
_UNIT_FIELDS = ("unit", "building", "floor", "room", "seat")
# NENA address number + street name parts: the identity of an address
_NENA_ADDR_FIELDS = ("addnum_pre", "add_number", "addnum_suf", "st_premod", "st_predir",
                     "st_pretyp", "st_presep", "st_name", "st_postyp", "st_posdir",
                     "st_posmod")
# Community / ZIP fields (NENA, FEMA USA Structures, OSM) - EAP addresses
_CITY_FIELDS = ("post_comm", "inc_muni", "prop_city", "city", "addr:city", "msagcomm",
                "postal_community")
_ZIP_FIELDS = ("post_code", "prop_zip", "zip", "zipcode", "zip_code", "addr:postcode")
_ADDR_NOT_STREET = ("addr:state", "addr:country", "prop_st", "addr_id", "address_id",
                    "addnum_sep")
# NENA Placement values of points that are not on the building itself
_OFF_STRUCTURE_PLACEMENTS = ("property access", "parcel", "geocoding", "site", "unknown")

# NENA Place_Type (free text in the ND data; values seen statewide in 2026)
# -> (phrase for the keyword rules, kind). kind "unverified": an address
# alone is not evidence of an occupied building (vacant lot, "LOT").
_PT_DWELLING, _PT_MOBILE, _PT_OCCUPIED, _PT_NONHAB, _PT_UNVERIFIED = (
    "dwelling", "mobile", "occupied", "nonhab", "unverified")
PLACE_TYPES = {
    "residence": ("residence", _PT_DWELLING), "residential": ("residential", _PT_DWELLING),
    "home": ("home", _PT_DWELLING), "single family home": ("single family", _PT_DWELLING),
    "single family residence": ("single family", _PT_DWELLING),
    "single residential": ("residential", _PT_DWELLING),
    "apartment": ("apartment", _PT_DWELLING), "apartment complex": ("apartments", _PT_DWELLING),
    "fema residence": ("residence", _PT_DWELLING), "hotel": ("hotel", _PT_DWELLING),
    "motel": ("motel", _PT_DWELLING),
    "mobile home": ("mobile home", _PT_MOBILE), "mobile residence": ("mobile home", _PT_MOBILE),
    "manufactured home": ("manufactured home", _PT_MOBILE), "trailer": ("trailer", _PT_MOBILE),
    # v2.9: mobile-home / trailer parks and close spellings of them
    "mobile homes": ("mobile home", _PT_MOBILE), "mobile home park": ("mobile home park", _PT_MOBILE),
    "mobile home court": ("mobile home court", _PT_MOBILE),
    "mobile home community": ("mobile home", _PT_MOBILE),
    "mobile home parks": ("mobile home park", _PT_MOBILE),
    "trailer park": ("trailer park", _PT_MOBILE), "trailer court": ("trailer court", _PT_MOBILE),
    "trailer home": ("trailer home", _PT_MOBILE), "trailer house": ("trailer house", _PT_MOBILE),
    "manufactured homes": ("manufactured home", _PT_MOBILE),
    "manufactured housing": ("manufactured housing", _PT_MOBILE),
    "manufactured home park": ("manufactured home", _PT_MOBILE),
    "manufactured home community": ("manufactured home", _PT_MOBILE),
    "manufactured housing community": ("manufactured housing", _PT_MOBILE),
    "mhp": ("mobile home park", _PT_MOBILE),
    "business": ("commercial", _PT_OCCUPIED), "office": ("office", _PT_OCCUPIED),
    "offices": ("office", _PT_OCCUPIED), "office lab testing": ("office", _PT_OCCUPIED),
    "medical": ("clinic", _PT_OCCUPIED), "medical office": ("clinic", _PT_OCCUPIED),
    "hospital": ("hospital", _PT_OCCUPIED), "fire": ("fire station", _PT_OCCUPIED),
    "police": ("police", _PT_OCCUPIED), "retail": ("retail", _PT_OCCUPIED),
    "store": ("store", _PT_OCCUPIED), "shop": ("shop", _PT_OCCUPIED),
    "shopping area": ("retail", _PT_OCCUPIED), "bank": ("bank", _PT_OCCUPIED),
    "bar": ("restaurant", _PT_OCCUPIED), "cafe": ("restaurant", _PT_OCCUPIED),
    "restaurant": ("restaurant", _PT_OCCUPIED), "club": ("assembly", _PT_OCCUPIED),
    "community": ("community center", _PT_OCCUPIED), "recreation": ("assembly", _PT_OCCUPIED),
    "arena": ("assembly", _PT_OCCUPIED), "stadium": ("assembly", _PT_OCCUPIED),
    "theater": ("assembly", _PT_OCCUPIED), "convention center": ("assembly", _PT_OCCUPIED),
    "church": ("church", _PT_OCCUPIED), "place of worship": ("place of worship", _PT_OCCUPIED),
    "school": ("school", _PT_OCCUPIED), "education": ("education", _PT_OCCUPIED),
    "educationl": ("education", _PT_OCCUPIED), "classroom": ("school", _PT_OCCUPIED),
    "childcare": ("school", _PT_OCCUPIED), "daycare": ("school", _PT_OCCUPIED),
    "library": ("library", _PT_OCCUPIED), "government": ("government", _PT_OCCUPIED),
    "public": ("public", _PT_OCCUPIED), "prison": ("government", _PT_OCCUPIED),
    "airport": ("commercial", _PT_OCCUPIED), "bus station": ("commercial", _PT_OCCUPIED),
    "train station": ("commercial", _PT_OCCUPIED), "industrial": ("industrial", _PT_OCCUPIED),
    "warehouse": ("warehouse", _PT_OCCUPIED), "loading facility": ("industrial", _PT_OCCUPIED),
    "city garage shop": ("industrial", _PT_OCCUPIED),
    "campground": ("campground", _PT_OCCUPIED),
    "parking": ("non habitable", _PT_NONHAB), "park": ("non habitable", _PT_NONHAB),
    "outdoors": ("non habitable", _PT_NONHAB), "street": ("non habitable", _PT_NONHAB),
    "water": ("non habitable", _PT_NONHAB), "landfill": ("non habitable", _PT_NONHAB),
    "lay down yard": ("non habitable", _PT_NONHAB),
    "lift station": ("non habitable", _PT_NONHAB), "utility": ("non habitable", _PT_NONHAB),
    "truck barn": ("barn", _PT_NONHAB), "construction": ("construction", _PT_NONHAB),
    "vacant": ("", _PT_UNVERIFIED), "lot": ("", _PT_UNVERIFIED),
    "structure": ("", None), "other": ("", None), "unknown": ("", None),
}


def _norm_text(v):
    return re.sub(r"[_\-/]+", " ", str(v).lower())


def place_type_info(value):
    """(phrase, kind) for a NENA Place_Type value; unknown values are passed
    to the keyword rules as they are."""
    key = re.sub(r"\s+", " ", _norm_text(value)).strip()
    if not key:
        return "", None
    return PLACE_TYPES.get(key, (key, None))


def _text(v):
    return "" if is_null(v) else str(v).strip()


class _SourceReader(object):
    """Field lookup for one source layer (done once, not per feature)."""

    def __init__(self, family, layer):
        self.family = family
        self.layer = layer
        fields = layer.fields()
        names = [fields.at(i).name() for i in range(fields.count())]
        low = [n.lower() for n in names]
        self.nena = any(m in low for m in _NENA_MARKERS)
        self.nsi = {k: low.index(k) for k in _NSI_FIELDS if k in low}
        self.place_idx = low.index("place_type") if "place_type" in low else None
        self.fema_idx = [low.index(n) for n in _FEMA_FIELDS if n in low]
        # v2.20.1: a FEMA USA Structures layer - occupancy and address are the parcel's
        self.fema_parcel = bool(self.fema_idx)
        self.parcel_idx = [low.index(n) for n in _FEMA_PARCEL_ADDR if n in low] \
            if self.fema_parcel else []
        self.placement_idx = low.index("placement") if "placement" in low else None
        # v2.9: the NENA Site_NGUID of an ND GIS Hub address point
        self.nguid_idx = low.index("site_nguid") if "site_nguid" in low else None
        self.unit_idx = [i for i, n in enumerate(low) if n in _UNIT_FIELDS] if self.nena else []
        self.text_idx = [i for i, n in enumerate(low)
                         if any(h in n for h in _TEXT_HINTS) and n not in self.nsi
                         and i != self.place_idx and i not in self.unit_idx
                         and not (self.nena and _STREET_FIELD.match(n))
                         and not (self.fema_parcel and n in _FEMA_PARCEL_TEXT)]
        self.text_names = [names[i] for i in self.text_idx]
        self.addr_idx = [i for i, n in enumerate(low) if any(h in n for h in _ADDRESS_HINTS)]
        self.key_idx = [low.index(n) for n in _NENA_ADDR_FIELDS if n in low] if self.nena \
            else []
        # v2.2: readable address for the EAP notification list
        self.city_idx = next((low.index(n) for n in _CITY_FIELDS if n in low), None)
        self.zip_idx = next((low.index(n) for n in _ZIP_FIELDS if n in low), None)
        skip = {self.city_idx, self.zip_idx} | {i for i, n in enumerate(low)
                                                if n in _CITY_FIELDS or n in _ZIP_FIELDS
                                                or n in _ADDR_NOT_STREET}
        self.street_idx = self.key_idx or [i for i in self.addr_idx if i not in skip]

    def describe(self, feat):
        attrs = feat.attributes()
        rec = {"family": self.family, "texts": [], "address": False, "nsi": None,
               "addr_key": None, "unit": "", "placement": None, "place_kind": None,
               "nena": self.nena, "place_type": "", "fema": ""}
        if self.nguid_idx is not None:
            rec["nguid"] = _text(attrs[self.nguid_idx])
        if self.fema_idx:
            parts = []
            for i in self.fema_idx:
                v = _text(attrs[i])
                if v and v.lower() not in (p.lower() for p in parts):
                    parts.append(v)
            rec["fema"] = " - ".join(parts)
        for i, name in zip(self.text_idx, self.text_names):
            v = attrs[i]
            if not is_null(v) and str(v).strip() and str(v).strip().lower() not in ("yes", "no"):
                rec["texts"].append("{}={}".format(name, v))
            elif name.lower() == "building" and not is_null(v):
                rec["texts"].append("building={}".format(v))
        if self.place_idx is not None:
            v = _text(attrs[self.place_idx])
            if v:
                phrase, kind = place_type_info(v)
                rec["place_kind"] = kind
                rec["place_type"] = v
                rec["texts"].append("Place_Type={}{}".format(
                    v, " [{}]".format(phrase) if phrase and phrase != v.lower() else ""))
        if self.placement_idx is not None:
            rec["placement"] = _text(attrs[self.placement_idx]).lower() or None
        parts = [p for p in (_text(attrs[i]) for i in self.addr_idx) if p]
        if self.parcel_idx and _text(attrs[self.parcel_idx[0]]):
            # v2.20.1: the parcel's address (groups the buildings of one parcel)
            rec["parcel"] = re.sub(r"\s+", " ", " | ".join(
                _text(attrs[i]) for i in self.parcel_idx).upper()).strip(" |")
        # v2.20.1: FEMA PROP_ADDR is the parcel's address, not this building's -
        # it is shown (addr_text) but is no evidence of a dwelling
        if parts and not self.fema_parcel:
            rec["address"] = True
            # NENA: number + street (+ unit) identify the address; other
            # sources (OSM addr:*) only say "has an address"
            key_parts = [p for p in (_text(attrs[i]) for i in self.key_idx) if p] or parts
            rec["unit"] = " ".join(p for p in (_text(attrs[i]) for i in self.unit_idx) if p)
            rec["addr_key"] = re.sub(r"\s+", " ", " | ".join(key_parts + [rec["unit"]]).lower())
        street = " ".join(p for p in (_text(attrs[i]) for i in self.street_idx) if p)
        if street:
            if rec["unit"]:
                street += " " + rec["unit"]
            rec["addr_text"] = re.sub(r"\s+", " ", street).strip()
            rec["city"] = _text(attrs[self.city_idx]) if self.city_idx is not None else ""
            rec["zip"] = _text(attrs[self.zip_idx]) if self.zip_idx is not None else ""
        if self.nsi:
            nsi = {}
            for k, i in self.nsi.items():
                v = attrs[i]
                nsi[k] = None if is_null(v) else v
            rec["nsi"] = nsi
        return rec


class _Grid(object):
    """Tiny uniform-grid spatial index for points / bounding boxes."""

    def __init__(self, cell):
        self.cell = max(float(cell), 1e-9)
        self.cells = {}

    def _key(self, x, y):
        return int(math.floor(x / self.cell)), int(math.floor(y / self.cell))

    def insert_rect(self, item, rect):
        k0 = self._key(rect.xMinimum(), rect.yMinimum())
        k1 = self._key(rect.xMaximum(), rect.yMaximum())
        for i in range(k0[0], k1[0] + 1):
            for j in range(k0[1], k1[1] + 1):
                self.cells.setdefault((i, j), []).append(item)

    def insert_point(self, item, x, y):
        self.cells.setdefault(self._key(x, y), []).append(item)

    def near(self, x, y, rings=1):
        ki, kj = self._key(x, y)
        out = []
        for i in range(ki - rings, ki + rings + 1):
            for j in range(kj - rings, kj + rings + 1):
                out.extend(self.cells.get((i, j), ()))
        return out


def _ft_units(ft, crs, lat=None):
    """Feet in CRS units; with `lat`, corrected for Web Mercator / degrees."""
    if lat is None:
        return buffer_distance_in_layer_units(ft, crs)
    return max(distance_xy_in_crs(ft, crs, lat))


# ------------------------------------------------ v2.9: footprint shape --
def _wkb_xy(b):
    """Every vertex (x, y) of a WKB geometry (ISO or EWKB; 2D, Z, M, ZM; either
    byte order; curves by their control points). Raises ValueError on a type
    it does not know."""
    import struct
    out = []

    def read(o):
        end = "<" if b[o] == 1 else ">"
        (code,) = struct.unpack_from(end + "I", b, o + 1)
        o += 5
        has_z, has_m = bool(code & 0x80000000), bool(code & 0x40000000)
        if code & 0x20000000:                          # EWKB SRID
            o += 4
        code &= 0x0FFFFFFF
        base, dim = code % 1000, code // 1000
        has_z = has_z or dim in (1, 3)
        has_m = has_m or dim in (2, 3)
        step = 8 * (2 + has_z + has_m)

        def pts(o, n):
            for i in range(n):
                out.append(struct.unpack_from(end + "dd", b, o + step * i))
            return o + step * n
        if base == 1:
            return pts(o, 1)
        if base not in (2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 15, 16, 17):
            raise ValueError("WKB type {}".format(code))
        (n,) = struct.unpack_from(end + "I", b, o)
        o += 4
        if base in (2, 8):                             # line string, circular string
            return pts(o, n)
        if base in (3, 17):                            # polygon, triangle
            for _r in range(n):
                (k,) = struct.unpack_from(end + "I", b, o)
                o = pts(o + 4, k)
            return o
        if base in (4, 5, 6, 7, 9, 10, 11, 12, 15, 16):  # made of whole geometries
            for _g in range(n):
                o = read(o)
            return o
        raise ValueError("WKB type {}".format(code))
    read(0)
    return [(x, y) for x, y in out if math.isfinite(x) and math.isfinite(y)]


def _geom_xy(geom):
    from .helpers import _wkb_bytes
    return _wkb_xy(_wkb_bytes(geom))


def _convex_hull(pts):
    """Convex hull (counter-clockwise, not closed) of (x, y) tuples."""
    pts = sorted(set(pts))
    if len(pts) < 3:
        return pts

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])
    lower, upper = [], []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]


def _obb_wh(geom):
    """(side_a, side_b) of QgsGeometry.orientedMinimumBoundingBox() in CRS
    units. PyQGIS 3.x / 4.x returns (geometry, area, angle, width, height);
    a binding that returns the rectangle alone is measured along its edges.
    None when it fails."""
    res = geom.orientedMinimumBoundingBox()
    box, w, h = res, None, None
    if isinstance(res, (tuple, list)):
        box = res[0] if res else None
        if len(res) >= 5:
            try:
                w, h = float(res[3]), float(res[4])
            except (TypeError, ValueError):
                w = h = None
    if w is None or h is None or not (math.isfinite(w) and math.isfinite(h)):
        if box is None or box.isNull() or box.isEmpty():
            return None
        ring = _geom_xy(box)
        if len(ring) < 3:
            return None
        w = math.hypot(ring[1][0] - ring[0][0], ring[1][1] - ring[0][1])
        h = math.hypot(ring[2][0] - ring[1][0], ring[2][1] - ring[1][1])
    return w, h


def footprint_dims_ft(geom, crs, lat=None):
    """(width_ft, length_ft) of an outline's oriented minimum bounding box,
    width <= length; None when it cannot be measured.

    Projected CRS in metres / feet: the QGIS box, converted to feet. Web
    Mercator: the same with the 1 / cos(latitude) stretch removed (`lat`
    needed - None without it). Geographic CRS: a degree of longitude is
    shorter than a degree of latitude, so the vertices are first scaled to
    feet on each axis (latitude from `lat` or the outline itself) and the
    box is taken around their hull."""
    try:
        if geom is None or geom.isNull() or geom.isEmpty():
            return None
        geo = is_geographic(crs)
        if geo and lat is None:
            bb = geom.boundingBox()
            lat = (bb.yMinimum() + bb.yMaximum()) / 2.0
        if lat is None and is_web_mercator(crs):
            return None
        ux, uy = distance_xy_in_crs(1.0, crs, lat)      # CRS units per foot
        if not (ux > 0 and uy > 0 and math.isfinite(ux) and math.isfinite(uy)):
            return None
        if abs(ux - uy) <= 1e-9 * max(ux, uy):
            wh = _obb_wh(geom)
            if wh is None:
                return None
            dims = (wh[0] / ux, wh[1] / ux)
        else:
            xy = _geom_xy(geom)
            if len(xy) < 3:
                return None
            x0, y0 = xy[0]
            hull = _convex_hull([((x - x0) / ux, (y - y0) / uy) for x, y in xy])
            if len(hull) < 3:
                return None
            ring = [QgsPointXY(x, y) for x, y in hull + [hull[0]]]
            wh = _obb_wh(QgsGeometry.fromPolygonXY([ring]))
            if wh is None:
                return None
            dims = wh
        a, b = sorted(float(v) for v in dims)
        if not (math.isfinite(a) and math.isfinite(b)) or b <= 0:
            return None
        return (a, b)
    except Exception:
        return None


OUTLINE_OVERLAP = 0.5


def _overlapping_outline(grid, g):
    """Existing outline covering >= OUTLINE_OVERLAP of the smaller of the
    two areas, or None. Of several, the one sharing the LARGEST area wins
    (a roof outline over a house and its separately mapped garage joins the
    house, not the garage - each covers the garage completely)."""
    bb = g.boundingBox()
    try:
        ga = g.area()
    except Exception:
        return None
    seen = set()
    best, best_ia = None, 0.0
    k0 = grid._key(bb.xMinimum(), bb.yMinimum())
    k1 = grid._key(bb.xMaximum(), bb.yMaximum())
    for i in range(k0[0], k1[0] + 1):
        for j in range(k0[1], k1[1] + 1):
            for cand in grid.cells.get((i, j), ()):
                if id(cand) in seen or not cand.get("footprint"):
                    continue
                seen.add(id(cand))
                if not cand["geom"].boundingBox().intersects(bb):
                    continue
                try:
                    inter = g.intersection(cand["geom"])
                    ia = inter.area() if inter is not None and not inter.isNull() else 0.0
                    frac = ia / max(min(ga, cand["geom"].area()), 1e-12)
                except Exception:
                    continue
                if frac >= OUTLINE_OVERLAP and ia > best_ia:
                    best, best_ia = cand, ia
    return best


def _request_for(layer, clip):
    req = QgsFeatureRequest()
    if clip is not None:
        rect = clip.request_rect(layer.crs())
        if rect is not None:
            req.setFilterRect(rect)
    return req


def _is_duplicate(sources, src, pt, dup_dist):
    """Same source family, same spot and the same address (or both without
    one): the same structure listed twice. Stacked address points of the
    units of one building differ in their address and are kept."""
    fd = (src.get("nsi") or {}).get("fd_id")
    for s in sources:
        if s.get("family") != src["family"] or s.get("addr_key") != src.get("addr_key"):
            continue
        sfd = (s.get("nsi") or {}).get("fd_id")
        if fd is not None and sfd is not None and sfd != fd:
            continue                    # two NSI records (e.g. two units) at one spot
        sp = s.get("pt")
        if sp is not None and math.hypot(sp[0] - pt.x(), sp[1] - pt.y()) <= dup_dist:
            return True
    return False


def _snap_distance_ft(src, snap_ft):
    """How far a point outside every outline may be moved onto one (None:
    never). NENA address points not placed on the structure itself
    (Placement "Property Access", "Parcel", "Geocoding", "Site", "Unknown")
    may sit at the driveway or parcel centre; addresses of parking lots,
    vacant lots etc. are not moved at all."""
    if src.get("nena") and src.get("place_kind") in (_PT_NONHAB, _PT_UNVERIFIED):
        return None
    if src.get("nena") and src.get("placement") in _OFF_STRUCTURE_PLACEMENTS:
        return max(snap_ft, STRUCTURE_MERGE_DIST_FT)
    return STRUCTURE_MERGE_DIST_FT


def _outline_takes_points(rec, min_area_ft2=DEFAULT_MIN_UNVERIFIED_AREA_FT2):
    """False for outlines a nearby point must not be moved onto: tagged
    outbuildings (garage, shed, barn, ...) and small untagged outlines."""
    norm = _norm_text(" ; ".join(t for s in rec["sources"] for t in s["texts"]))
    hab = KW_NONHAB.sub(" ", norm)
    if KW_DWELLING.search(hab) or KW_MOBILE.search(norm) or KW_OCCUPIED.search(hab):
        return True
    if KW_NONHAB.search(norm):
        return False
    area = rec.get("area_ft2")
    return area is None or area >= min_area_ft2


# v2.20.1: a structure without an outline (NSI / address point) belongs to the
# parcel of the nearest outline within this distance
SITE_JOIN_FT = 150.0


def _has_own_911(rec, merge_dist):
    """An ND GIS Hub address point sits on this structure: inside its
    outline, moved onto it from at most STRUCTURE_MERGE_DIST_FT, or (no
    outline) merged with its point."""
    for s in rec["sources"]:
        if s.get("family") == "NDGISHUB" and s.get("nena") and s.get("address"):
            d = s.get("snapped")
            if d is None or d <= merge_dist:
                return True
    return False


def _mark_parcel_sites(structs, grid, merge_dist, join_dist, lone_dist):
    """v2.20.1: parcel / site evidence for _interpret.

    FEMA USA Structures gives every building of a parcel the parcel's address
    (PROP_ADDR), so the outlines that share it are one site (a farmstead, a
    house lot). When an ND GIS Hub (911) address point sits on one of them,
    that building is the site's addressed structure and the others are its
    outbuildings: rec["site_anchor"] = {"parcel", "ids"} on the others.
    Structures without an outline join the parcel of the nearest outline
    within join_dist. rec["lone_address"]: a structure made only of 911
    address points with no outline within lone_dist (field access, vacant
    lot, tower or utility site) - only when outlines were loaded at all."""
    members = {}
    for r in structs:
        r.pop("site_anchor", None)
        r.pop("lone_address", None)
        if r["footprint"]:
            for s in r["sources"]:
                if s.get("parcel"):
                    members.setdefault(s["parcel"], {})[r["id"]] = r
    if not any(r["footprint"] for r in structs):
        return
    rings = max(1, int(math.ceil(max(join_dist, lone_dist) / grid.cell)))
    for r in structs:
        if r["footprint"]:
            continue
        p = r["point"]
        pgeom = QgsGeometry.fromPointXY(p)
        best, best_d = None, None
        for cand in grid.near(p.x(), p.y(), rings=rings):
            if not cand.get("footprint"):
                continue
            try:
                d = cand["geom"].distance(pgeom)
            except Exception:
                continue
            if d is not None and d >= 0 and (best_d is None or d < best_d):
                best, best_d = cand, d
        if r["families"] == {"NDGISHUB"} and (best_d is None or best_d > lone_dist):
            r["lone_address"] = True
        if best is not None and best_d <= join_dist:
            for s in best["sources"]:
                if s.get("parcel"):
                    members.setdefault(s["parcel"], {})[r["id"]] = r
    for k, recs in members.items():
        anchors = sorted(i for i, r in recs.items() if _has_own_911(r, merge_dist))
        if not anchors:
            continue
        for i, r in recs.items():
            if i not in anchors and "site_anchor" not in r:
                r["site_anchor"] = {"parcel": k.split(" | ")[0], "ids": anchors}


class InventoryRejected(Exception):
    """Structure layers were supplied and none of them fell in the study area.

    An empty inventory must not be classified. Low is what that used to return.
    """


def build_inventory(footprint_layer, point_sources, analysis_crs, transform_ctx,
                    progress_cb=None, clip=None, snap_ft=ADDRESS_SNAP_FT, mobile_rules=None):
    """Structure records in `analysis_crs`.

    footprint_layer: polygon layer (FEMA_Structures outlines), a list of (family, layer)
        outline sources (first = primary), or None
    point_sources: list of (family, layer) e.g. ("NSI", lyr), ("NDGISHUB", lyr)
    clip: extent.ExtentClip - only outlines touching / points inside it
    mobile_rules: v2.9 - keyword arguments of _interpret for the manufactured-
        home rules (MOBILE_RULE_DEFAULTS; None = the defaults)
    Every outline record keeps rec["dims_ft"] = (width_ft, length_ft) of its
    oriented minimum bounding box (None if not measurable) for the shape rule.
    """
    problems = LimitedLogger("Structure inventory")
    da = QgsDistanceArea()
    da.setSourceCrs(analysis_crs, transform_ctx)
    try:
        da.setEllipsoid(analysis_crs.ellipsoidAcronym() or "EPSG:7019")
    except Exception:
        pass
    structs = []
    lat = clip.lat if clip is not None else None
    grid = _Grid(_ft_units(200.0, analysis_crs, lat))
    a_to_clip = clip.transform_from(analysis_crs) if clip is not None else None
    outside = 0

    # --- footprints ---------------------------------------------------------
    # The first outline source defines the buildings; outlines of further
    # sources (FEMA USA Structures, Microsoft / Overture footprints, ...)
    # that overlap one of them by >= OUTLINE_OVERLAP of the smaller area are
    # the same building (their attributes are added); the others are new.
    if isinstance(footprint_layer, (list, tuple)):
        outline_sources = [(fam, lyr) for fam, lyr in footprint_layer if lyr is not None]
    else:
        outline_sources = [(STRUCTURES_FAMILY, footprint_layer)] \
            if footprint_layer is not None else []
    merged_outlines = 0
    # v2.9: latitude for measuring outlines in Web Mercator (geographic CRS:
    # each outline's own latitude is used)
    shape_lat = [lat]

    def dims_of(g):
        if shape_lat[0] is None and is_web_mercator(analysis_crs):
            shape_lat[0] = center_latitude(g.boundingBox(), analysis_crs, transform_ctx)
        return footprint_dims_ft(g, analysis_crs, shape_lat[0])

    for n_src, (ofam, olyr) in enumerate(outline_sources):
        if not olyr.isValid():
            continue
        reader = _SourceReader(ofam, olyr)
        xf = QgsCoordinateTransform(olyr.crs(), analysis_crs, transform_ctx)
        if not xf.isValid() and olyr.crs() != analysis_crs:
            raise RuntimeError("The coordinate transformation {} -> {} for the structure layer '{}' is invalid "
                               "(QGIS could not create it); the structures were NOT read. Run again.".format(
                                   olyr.crs().authid() or "layer CRS", analysis_crs.authid() or "analysis CRS",
                                   olyr.name()))
        for feat in olyr.getFeatures(_request_for(olyr, clip)):
            g = transform_geometry(feat.geometry(), xf)
            if g is None or g.isNull() or g.isEmpty():
                continue
            if clip is not None and not clip.intersects_geometry(g, xform=a_to_clip):
                outside += 1
                continue
            try:
                if not g.isGeosValid():
                    g = g.makeValid()
            except Exception:
                pass
            src = reader.describe(feat)
            src["ref"] = "{}:{}".format(ofam, feat.id())
            if n_src > 0:
                host = _overlapping_outline(grid, g)
                if host is not None:
                    host["sources"].append(src)
                    host["families"].add(ofam)
                    merged_outlines += 1
                    continue
            area_ft2 = finite_area_ft2(da, g, analysis_crs, transform_ctx)   # v2.20.1
            rec = {"id": len(structs) + 1, "geom": g, "footprint": True,
                   "area_ft2": area_ft2, "sources": [src],
                   "families": {ofam}, "notes": [], "dims_ft": dims_of(g)}
            structs.append(rec)
            grid.insert_rect(rec, g.boundingBox())
    if merged_outlines:
        log_message("Structure inventory: {} outline(s) of additional sources matched "
                    "existing buildings".format(merged_outlines))

    # --- points: read -------------------------------------------------------
    merge_dist = _ft_units(STRUCTURE_MERGE_DIST_FT, analysis_crs, lat)
    dup_dist = _ft_units(3.0, analysis_crs, lat)  # same source, same spot = duplicate
    seen_fd = set()
    total = sum(max(0, lyr.featureCount()) for _f, lyr in point_sources) or 1
    done = 0
    pending = []
    for family, layer in point_sources:
        if layer is None or not layer.isValid():
            continue
        reader = _SourceReader(family, layer)
        xf = QgsCoordinateTransform(layer.crs(), analysis_crs, transform_ctx)
        if not xf.isValid() and layer.crs() != analysis_crs:
            raise RuntimeError("The coordinate transformation {} -> {} for the structure layer '{}' is invalid "
                               "(QGIS could not create it); the structures were NOT read. Run again.".format(
                                   layer.crs().authid() or "layer CRS", analysis_crs.authid() or "analysis CRS",
                                   layer.name()))
        for feat in layer.getFeatures(_request_for(layer, clip)):
            done += 1
            if progress_cb and done % PROGRESS_INTERVAL == 0:
                progress_cb(100.0 * done / total, "Structure inventory: {} of {} points".format(
                    done, total))
            g = transform_geometry(feat.geometry(), xf)
            pt = point_from_geometry(g) if g is not None else None
            if pt is None:
                continue
            if clip is not None and not clip.contains_point(pt, xform=a_to_clip):
                outside += 1
                continue
            src = reader.describe(feat)
            src["ref"] = "{}:{}".format(family, feat.id())
            src["xy"] = (round(pt.x() / dup_dist), round(pt.y() / dup_dist))
            src["pt"] = (pt.x(), pt.y())
            fd = (src.get("nsi") or {}).get("fd_id")
            if fd is not None:
                if fd in seen_fd:
                    continue            # same NSI structure from another layer
                seen_fd.add(fd)
            pending.append((src, pt, family))

    def attach(host, src, family):
        host["sources"].append(src)
        host["families"].add(family)

    # --- points inside an outline ---------------------------------------------
    unplaced = []
    for src, pt, family in pending:
        pgeom = QgsGeometry.fromPointXY(pt)
        host = None
        for cand in grid.near(pt.x(), pt.y(), rings=0):
            bb = cand["geom"].boundingBox()
            if bb.contains(pt) and cand["geom"].intersects(pgeom):
                host = cand
                break
        if host is None:
            unplaced.append((src, pt, family))
        elif not _is_duplicate(host["sources"], src, pt, dup_dist):
            attach(host, src, family)

    # --- points just outside an outline: onto the nearest building ------------
    # Each point may only go to ITS nearest suitable outline (never on to a
    # second-nearest one). Several NENA addresses may share one outline
    # (duplex, apartments: each is a dwelling unit); for the other sources
    # an outline takes at most one point of each source.
    snapped = 0
    if unplaced and structs:
        by_id = {r["id"]: r for r in structs}
        takes = {r["id"]: _outline_takes_points(r) for r in structs if r["footprint"]}
        nearest = {}
        for k, (src, pt, _family) in enumerate(unplaced):
            reach_ft = _snap_distance_ft(src, snap_ft)
            if reach_ft is None:
                continue
            reach = _ft_units(reach_ft, analysis_crs, lat)
            pgeom = QgsGeometry.fromPointXY(pt)
            seen = set()
            rings = max(1, int(math.ceil(reach / grid.cell)))
            for cand in grid.near(pt.x(), pt.y(), rings=rings):
                if cand["id"] in seen or not takes.get(cand["id"]):
                    continue
                seen.add(cand["id"])
                bb = cand["geom"].boundingBox()
                if (pt.x() < bb.xMinimum() - reach or pt.x() > bb.xMaximum() + reach
                        or pt.y() < bb.yMinimum() - reach or pt.y() > bb.yMaximum() + reach):
                    continue
                try:
                    dd = cand["geom"].distance(pgeom)
                except Exception:
                    continue
                if dd is None or dd < 0 or dd > reach:
                    continue            # QgsGeometry.distance() is -1 on error
                if k not in nearest or dd < nearest[k][0]:
                    nearest[k] = (dd, cand["id"])
        placed = set()
        same_family_dropped = []
        for k, (dd, sid) in sorted(nearest.items(), key=lambda kv: kv[1]):
            src, pt, family = unplaced[k]
            host = by_id[sid]
            if _is_duplicate(host["sources"], src, pt, dup_dist):
                placed.add(k)
                continue
            if family in host["families"] and not src.get("nena"):
                # v2.3.1: prevent phantom duplicate structures (double LIJ counts)
                placed.add(k)
                same_family_dropped.append((host["id"], src))
                continue                # one NSI / FEMA point per outline
            src["snapped"] = dd
            attach(host, src, family)
            placed.add(k)
            snapped += 1
        unplaced = [u for k, u in enumerate(unplaced) if k not in placed]
        if same_family_dropped:
            log_message("Structure inventory: {} point(s) of a source whose nearest "
                        "outline already has one were not attached - review (outline id, "
                        "point): {}".format(len(same_family_dropped),
                            "; ".join("{} / {}".format(sid, s.get("ref"))
                                      for sid, s in same_family_dropped)))

    # --- free-standing points: merge sources that describe one building --------
    pgrid = _Grid(merge_dist)
    for src, pt, family in unplaced:
        host = None
        for cand in pgrid.near(pt.x(), pt.y()):
            c = cand["point"]
            dd = math.hypot(c.x() - pt.x(), c.y() - pt.y())
            if family in cand["families"]:
                if dd <= dup_dist:  # the same structure listed twice, or another unit
                    host = cand
                    break
                continue
            if dd <= merge_dist:
                host = cand
                break
        if host is not None:
            if not _is_duplicate(host["sources"], src, pt, dup_dist):
                attach(host, src, family)
            continue
        rec = {"id": len(structs) + 1, "geom": QgsGeometry.fromPointXY(pt), "point": pt,
               "footprint": False, "area_ft2": None, "sources": [src],
               "families": {family}, "notes": []}
        structs.append(rec)
        pgrid.insert_point(rec, pt.x(), pt.y())
    problems.summary()
    _mark_parcel_sites(structs, grid, merge_dist, _ft_units(SITE_JOIN_FT, analysis_crs, lat),
                       _ft_units(snap_ft, analysis_crs, lat))
    rules = dict(mobile_rules or {})
    for rec in structs:
        _interpret(rec, **rules)
    if not structs and outside > 0:
        raise InventoryRejected(
            "All {} structure feature(s) fell outside the study area, so the inventory "
            "is empty. This is usually a layer with no CRS (longitude/latitude read as "
            "projected coordinates). The dam was not classified; an empty inventory is "
            "not Low.".format(outside))
    log_message("Structure inventory: {} buildings ({} with outlines) - {} habitable, "
                "{} unverified, {} not habitable; {} points moved onto nearby outlines; "
                "{} features outside the study area{}".format(
                    len(structs), sum(1 for r in structs if r["footprint"]),
                    sum(1 for r in structs if r["habitable"] == "Yes"),
                    sum(1 for r in structs if r["habitable"] == "Unverified"),
                    sum(1 for r in structs if r["habitable"] == "No"), snapped, outside,
                    " ({})".format(clip.describe()) if clip is not None else ""))
    _log_mobile_rules(structs)
    _log_site_rules(structs)
    return structs


def _log_site_rules(structs):
    """v2.20.1: what the parcel / site and lone-address rules changed."""
    unv = sum(1 for r in structs if r.get("site_note") and not r.get("lone_address")
              and r["category"] == CAT_UNVERIFIED)
    out = sum(1 for r in structs if r.get("site_note") and r["category"] == CAT_NOT_HABITABLE)
    lone = sum(1 for r in structs if r.get("site_note") and r.get("lone_address"))
    if unv or out or lone:
        log_message("Structure inventory: on parcels whose 911 address point is on another "
                    "building, {} building(s) only NSI calls residential are unverified and {} "
                    "that no source calls habitable are outbuildings (not habitable, listed in "
                    "QA/Inventory_QA.csv); {} 911 address point(s) with no building are "
                    "unverified".format(unv, out, lone))


def _log_mobile_rules(structs):
    """v2.9: how many mobile homes the park / lot and shape rules typed."""
    n_mob = sum(1 for r in structs if r.get("acer_type") == TYPE_MOBILE)
    n_pl = sum(1 for r in structs if r.get("mobile_rule") == "park_lot")
    n_sh = sum(1 for r in structs if r.get("mobile_rule") == "shape")
    n_occ = sum(1 for r in structs if r.get("mobile_occupied"))
    if n_pl or n_sh or n_occ:
        log_message("Structure inventory: {} mobile home(s), of which {} from a mobile-home "
                    "park / lot address and {} from the footprint shape alone; {} on a "
                    "building read as occupied non-residential - all listed for review".format(
                        n_mob, n_pl, n_sh, n_occ))


CAMP_FAMILY = "OSM_CAMPS"


def campground_records(layer, analysis_crs, transform_ctx, clip=None, start_id=1):
    """v2.5: structure records for OpenStreetMap campgrounds / RV parks
    (polygons from download.fetch_osm_camps, or any polygon layer with
    'category' / 'name' / 'sites' fields). They are not merged with the
    building outlines (a campground contains its own buildings)."""
    if layer is None or not layer.isValid():
        return []
    da = QgsDistanceArea()
    da.setSourceCrs(analysis_crs, transform_ctx)
    try:
        da.setEllipsoid(analysis_crs.ellipsoidAcronym() or "EPSG:7019")
    except Exception:
        pass
    names = [f.name().lower() for f in layer.fields()]

    def get(attrs, key):
        if key not in names:
            return None
        v = attrs[names.index(key)]
        return None if is_null(v) else v
    xf = QgsCoordinateTransform(layer.crs(), analysis_crs, transform_ctx)
    a_to_clip = clip.transform_from(analysis_crs) if clip is not None else None
    out = []
    for feat in layer.getFeatures(_request_for(layer, clip)):
        g = transform_geometry(feat.geometry(), xf)
        if g is None or g.isNull() or g.isEmpty():
            continue
        if clip is not None and not clip.intersects_geometry(g, xform=a_to_clip):
            continue
        try:
            if not g.isGeosValid():
                g = g.makeValid()
        except Exception:
            pass
        a = feat.attributes()
        cat = str(get(a, "category") or "Campground")
        name = str(get(a, "name") or "")
        sites = _num(get(a, "sites"))
        camp = {"category": cat, "name": name,
                "sites": int(sites) if sites and sites > 0 else None,
                "seasonal": str(get(a, "seasonal") or ""), "shape": str(get(a, "shape") or "")}
        texts = ["category={}".format(cat)] + (["name={}".format(name)] if name else [])
        src = {"family": CAMP_FAMILY, "texts": texts, "address": False, "nsi": None,
               "addr_key": None, "unit": "", "placement": None, "place_kind": None,
               "nena": False, "camp": camp,
               "ref": "{}:{}".format(CAMP_FAMILY, get(a, "osm_id") or feat.id())}
        area_ft2 = finite_area_ft2(da, g, analysis_crs, transform_ctx)       # v2.20.1
        rec = {"id": start_id + len(out), "geom": g, "footprint": True, "area_ft2": area_ft2,
               "sources": [src], "families": {CAMP_FAMILY}, "notes": []}
        _interpret(rec)
        out.append(rec)
    return out


def _num(v):
    try:
        f = float(v)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def _interpret(rec, min_area_ft2=DEFAULT_MIN_UNVERIFIED_AREA_FT2,
               persons_per_dwelling=TM11_PERSONS_PER_DWELLING, mobile_shape=True,
               mobile_max_width_ft=MOBILE_MAX_WIDTH_FT, mobile_min_ratio=MOBILE_MIN_RATIO,
               mobile_min_length_ft=MOBILE_MIN_LENGTH_FT,
               mobile_max_length_ft=MOBILE_MAX_LENGTH_FT, mobile_lot_units=True):
    """Category, habitability, ACER type and occupancy basis for a record.

    v2.9 manufactured-home rules (see the module docstring for the order):
    mobile_lot_units - a NENA unit "Lot 12" / "Space 7" ... is mobile-home
    evidence; mobile_shape (+ the four screening parameters) - the footprint
    shape of a single-wide manufactured home types an otherwise untyped
    dwelling / unverified outline. rec["mobile_park"] (mark_mobile_parks)
    is used whenever it is set."""
    nsi_recs = [s["nsi"] for s in rec["sources"] if s.get("nsi")]
    texts = " ; ".join(t for s in rec["sources"] for t in s["texts"])
    norm = _norm_text(texts)
    mobile_basis, category, lifeline, occ_codes = [], None, False, []
    units, night, day = 0, 0.0, 0.0
    has_pop = False
    sqft = None
    unit_keys = set()
    nsi_units = []

    for src in rec["sources"]:
        n = src.get("nsi")
        if not n:
            continue
        key = n.get("fd_id") if n.get("fd_id") is not None else src.get("xy")
        if key in unit_keys:
            continue                         # same NSI structure twice
        unit_keys.add(key)
        nsi_units.append(n)
        occ = str(n.get("occtype") or "").upper()
        bt = str(n.get("bldgtype") or "").upper()
        occ_codes.append(occ or "?")
        if _num(n.get("sqft")):
            sqft = max(sqft or 0, _num(n.get("sqft")))
        if any(_num(n.get(k)) is not None for k in ("pop2amu65", "pop2amo65",
                                                      "pop2pmu65", "pop2pmo65")):
            has_pop = True
        night += sum(_num(n.get(k)) or 0 for k in ("pop2amu65", "pop2amo65"))
        day += sum(_num(n.get(k)) or 0 for k in ("pop2pmu65", "pop2pmo65"))
        if occ.startswith("RES2") or bt == "H":
            mobile_basis.append("NSI {}".format("RES2" if occ.startswith("RES2") else "bldgtype H"))
        if occ.startswith("RES"):
            category = CAT_DWELLING
            units += 1
        elif occ.startswith(("COM", "IND", "EDU", "GOV", "REL")):
            category = category or CAT_OCCUPIED
            if occ.startswith(("COM6", "GOV2")):
                lifeline = True
        elif occ.startswith("AGR"):
            category = category or CAT_UNVERIFIED
    nsi_category = category

    if KW_MOBILE.search(norm):
        mobile_basis.append("attributes: {}".format(KW_MOBILE.search(norm).group(0)))
    if KW_LIFELINE.search(norm):
        lifeline = True
    # dwelling/occupied words are looked for after removing non-habitable
    # phrases, so "farm auxiliary" or "boat house shed" do not read as homes
    norm_hab = KW_NONHAB.sub(" ", norm)
    kw_dwelling = bool(KW_DWELLING.search(norm_hab)) or bool(KW_MOBILE.search(norm))
    kw_occupied = bool(KW_OCCUPIED.search(norm_hab))
    kw_nonhab = bool(KW_NONHAB.search(norm))
    has_address = any(s.get("address") for s in rec["sources"])
    place_kinds = {s.get("place_kind") for s in rec["sources"]}
    if _PT_MOBILE in place_kinds and not mobile_basis:
        mobile_basis.append("address Place_Type")

    if category is None:
        if kw_dwelling:
            category = CAT_DWELLING
        elif kw_occupied or lifeline:
            category = CAT_OCCUPIED
        elif kw_nonhab:
            category = CAT_NOT_HABITABLE
        elif _PT_UNVERIFIED in place_kinds:
            category = CAT_UNVERIFIED       # "Vacant" / "LOT": no building confirmed
        elif has_address:
            category = CAT_DWELLING
        elif rec["footprint"] and rec["area_ft2"] is not None and rec["area_ft2"] < min_area_ft2:
            category = CAT_NOT_HABITABLE
        else:
            category = CAT_UNVERIFIED
    elif category == CAT_UNVERIFIED and (kw_dwelling or kw_occupied):
        category = CAT_DWELLING if kw_dwelling else CAT_OCCUPIED

    # (only an ND GIS Hub address point that carries an address - not any
    # layer that happens to have a NENA-like field such as ST_NAME)
    nd_srcs = [s for s in rec["sources"] if s.get("nena") and s.get("address")
               and s.get("family") == "NDGISHUB"]
    nd_addr = bool(nd_srcs)

    # v2.20.1: site evidence (_mark_parcel_sites). On a parcel whose 911 address
    # point is on another building, a building that only NSI calls
    # residential is unverified (NSI 2026 gives the parcel's RES record to
    # machine sheds and barns of farmsteads, while the farmhouse is often
    # AGR1), and one that nothing calls habitable (only the FEMA parcel record
    # or NSI AGR) is not habitable. Manufactured-home evidence and FEMA multi-
    # family / manufactured-home parcels keep the earlier reading.
    site_note = ""
    site = rec.get("site_anchor")
    fema_txt = " ".join(s.get("fema") or "" for s in rec["sources"]).lower()
    if site and not nd_addr and not mobile_basis and rec.get("mobile_park") is None \
            and "multi" not in fema_txt and "manufactured" not in fema_txt:
        where = "the 911 address of its parcel ({}) is on structure {}".format(
            site["parcel"], ", ".join(str(i) for i in site["ids"][:3])
            + (" ..." if len(site["ids"]) > 3 else ""))
        if category == CAT_DWELLING and nsi_category == CAT_DWELLING and not kw_dwelling:
            category = CAT_UNVERIFIED
            site_note = "NSI lists it as residential ({}), but {} - probably an outbuilding; " \
                "verify".format(", ".join(sorted({c for c in occ_codes if c.startswith("RES")})),
                                where)
        elif category == CAT_UNVERIFIED and nsi_category in (None, CAT_UNVERIFIED) \
                and not (kw_dwelling or kw_occupied or has_address):
            category = CAT_NOT_HABITABLE
            site_note = "outbuilding: no source says it is habitable and {}".format(where)
    # v2.20.1: 911 address points with no building - no outline within the
    # address snap distance and no NSI structure merged with them (a field
    # access, vacant lot, tower or utility site) - are unverified, unless
    # their Place_Type says what is there
    lone = bool(rec.get("lone_address")) and not (
        place_kinds & {_PT_DWELLING, _PT_MOBILE, _PT_OCCUPIED})
    if lone:
        category = CAT_UNVERIFIED
        site_note = "911 address point with no building outline within {:.0f} ft and no NSI " \
            "structure - field access, vacant lot, tower or utility site? verify".format(
                ADDRESS_SNAP_FT)

    # v2.5.1: a structure with an ND GIS Hub site address is habitable - the
    # address is the evidence. Place_Type values that would otherwise read as
    # vacant / not habitable are kept in a note (and listed for review).
    addr_note = ""
    if nd_addr and not lone and category not in (CAT_DWELLING, CAT_OCCUPIED):
        odd = sorted({s.get("place_type") or "" for s in nd_srcs
                      if s.get("place_kind") in (_PT_NONHAB, _PT_UNVERIFIED)} - {""})
        was = category
        category = CAT_OCCUPIED if (kw_occupied or _PT_OCCUPIED in place_kinds) \
            else CAT_DWELLING
        addr_note = "ND site address{} - habitable by rule (automatic reading: {}); " \
            "verify".format(" (Place_Type {})".format(", ".join(odd)) if odd else "",
                            was.lower())

    # ---- ACER type: manufactured / mobile home? (order: module docstring) ----
    strong = list(mobile_basis)
    weak = []                             # v2.9: park / lot evidence
    if mobile_lot_units:
        lots = []
        for s in rec["sources"]:
            u = s.get("unit") or ""
            if s.get("nena") and u and is_lot_unit(u) and u not in lots:
                lots.append(u)
        if lots:
            weak.append("address unit {}{}".format(
                ", ".join("'{}'".format(u) for u in lots[:3]),
                " (+{} more)".format(len(lots) - 3) if len(lots) > 3 else ""))
    if rec.get("mobile_park") is not None:
        weak.append("inside mobile-home park '{}'".format(rec["mobile_park"] or "unnamed"))
    evidence = strong + weak
    dwelling_like = category in (CAT_DWELLING, CAT_UNVERIFIED)
    is_mobile = bool(evidence) and dwelling_like
    # v2.9: a residential manufactured home stays one when another source
    # (e.g. an NSI COM occupancy) reads the building as occupied non-residential
    mobile_occupied = (not is_mobile and category == CAT_OCCUPIED and bool(strong)
                       and (_PT_MOBILE in place_kinds or bool(KW_MOBILE_HOME.search(norm))))
    if mobile_occupied:
        is_mobile = True
    conflict = is_mobile and bool(KW_HOUSE_EXPLICIT.search(
        KW_MOBILE.sub(" ", norm)))
    mobile_rule = "park_lot" if is_mobile and weak and not strong else ""
    # v2.9: footprint shape of a single-wide, when nothing else says anything
    shape_txt, shape_conflict = "", False
    if mobile_shape and not evidence and dwelling_like and rec.get("footprint") \
            and rec.get("dims_ft") and single_wide_shape(
                rec["dims_ft"], mobile_max_width_ft, mobile_min_ratio, mobile_min_length_ft,
                mobile_max_length_ft):
        w_ft, l_ft = sorted(rec["dims_ft"])
        shape_txt = "footprint shape suggests a single-wide manufactured home " \
            "({:.0f} x {:.0f} ft)".format(w_ft, l_ft)
        house = KW_HOUSE_EXPLICIT.search(norm)
        if house:
            shape_conflict = conflict = True
            shape_txt += " but the attributes say '{}' - house curve kept, verify".format(
                house.group(0))
        else:
            is_mobile = True
            evidence = [shape_txt]
            mobile_rule = "shape"
    mobile_basis = evidence
    if mobile_occupied:
        mobile_basis = evidence + ["on a building read as occupied non-residential - verify"]
    # dwelling units from address points when no NSI record counted them:
    # one per distinct address; if some carry a unit (Apt 3, Lot 12) the
    # building-level address without a unit is not counted again
    addr_units = 0
    if not units:
        keys = {s["addr_key"] for s in rec["sources"]
                if s.get("nena") and s.get("addr_key")
                and s.get("place_kind") not in (_PT_OCCUPIED, _PT_NONHAB)}
        with_unit = {s["addr_key"] for s in rec["sources"]
                     if s.get("addr_key") in keys and s.get("unit")}
        addr_units = len(with_unit) if with_unit else len(keys)
    if category == CAT_DWELLING:
        per = persons_per_dwelling
        n_units = max(1, units or addr_units)
        persons = n_units * per
        if any(str(c).startswith(("RES3", "RES4", "RES5", "RES6")) for c in occ_codes):
            persons = max(persons, int(round(night)))
        if persons != n_units * per:
            basis = "NSI night population"
        elif not units and addr_units > 1:
            basis = "{} per dwelling (TM 11) x {} address units".format(per, addr_units)
        else:
            basis = "{} per dwelling (TM 11)".format(per)
    elif category == CAT_OCCUPIED:
        persons = int(round(max(day, night)))
        basis = "NSI day/night population" if persons else "no occupancy data"
        if not persons and nd_addr:
            # v2.5.1: an ND-addressed occupied building without NSI
            # population is not counted as empty - one dwelling's worth,
            # listed for review
            persons = persons_per_dwelling
            basis = "{} per dwelling (TM 11) - ND address, no occupancy data; " \
                "verify".format(persons_per_dwelling)
    else:
        persons, basis = 0, "-"

    # v2.5.1: FEMA occupancy for reference only (FEMA USA Structures OCC_CLS /
    # PRIM_OCC, else the NSI Hazus occupancy class) - it does not decide
    # habitability
    fema = []
    for s in rec["sources"]:
        if s.get("fema") and s["fema"] not in fema:
            fema.append(s["fema"])
    if not fema:
        for c in occ_codes:
            lab = hazus_label(c) if c and c != "?" else ""
            if lab and lab not in fema:
                fema.append(lab)
    fema_category = "; ".join(fema)

    # v2.5: campground / RV park (overnight recreation, ND 5.2.1.1): the
    # whole area is evaluated; campers, RVs and tents use the mobile-home curve
    camp = next((s["camp"] for s in rec["sources"] if s.get("camp")), None)
    if camp is not None:
        category, lifeline, conflict = CAT_OCCUPIED, False, False
        is_mobile = True
        mobile_rule, mobile_occupied, shape_conflict = "", False, False
        mobile_basis = ["campground / RV park (RVs, trailers, tents) - TM 11 Fig. 3 curve"]
        sites = camp.get("sites")
        if sites:
            persons = int(sites) * persons_per_dwelling
            basis = "{} sites x {} persons (OSM capacity) - seasonal, verify".format(
                sites, persons_per_dwelling)
        else:
            persons = persons_per_dwelling
            basis = "campground: number of sites unknown - counted as one party, verify"
    rec.update({
        "category": category,
        "habitable": ("Yes" if category in (CAT_DWELLING, CAT_OCCUPIED)
                      else "Unverified" if category == CAT_UNVERIFIED else "No"),
        "acer_type": TYPE_MOBILE if is_mobile else TYPE_HABITABLE if nd_addr and camp is None
        else TYPE_HOUSE,
        "type_basis": "; ".join(mobile_basis) if is_mobile else "; ".join(
            ["ND site address - habitable; house-on-foundation curve (TM 11 Fig. 2)"
             if nd_addr and camp is None else "no manufactured-home evidence"]
            + ([shape_txt] if shape_conflict else [])),
        "mobile_rule": mobile_rule,
        "mobile_occupied": bool(mobile_occupied),
        "nd_address": bool(nd_addr and camp is None),
        "address_note": addr_note if camp is None else "",
        "site_note": site_note if camp is None else "",
        "fema_category": fema_category,
        "type_conflict": conflict,
        "lifeline": lifeline,
        "persons": persons,
        "persons_basis": basis,
        "occupancy": ", ".join(sorted(set(occ_codes))) if occ_codes else (
            texts[:120] or fema_category[:120]),
        "sqft": sqft,
        "fd_id": ", ".join(sorted({str(n.get("fd_id")) for n in nsi_recs
                                   if n.get("fd_id") is not None})),
        "nsi_units": nsi_units,
        # None = no NSI population (not the same as an NSI population of 0)
        "pop_night": night if has_pop else None, "pop_day": day if has_pop else None,
        "reviewed": "",
        "camp": camp,
    })
    if camp is not None:
        rec["occupancy"] = "{} (overnight recreation){}".format(
            camp.get("category") or "Campground",
            ": " + camp["name"] if camp.get("name") else "")
    rv = rec.get("review")
    if rv:
        _apply_review(rec, rv, max(1, units or addr_units), persons_per_dwelling)


def _apply_review(rec, rv, n_units, per):
    """Engineer decisions saved in the review file win over the automatic
    interpretation (review.py)."""
    done = []
    cat = rv.get("category")
    if cat in (CAT_DWELLING, CAT_OCCUPIED, CAT_UNVERIFIED, CAT_NOT_HABITABLE) \
            and cat != rec["category"]:
        rec["category"] = cat
        rec["habitable"] = ("Yes" if cat in (CAT_DWELLING, CAT_OCCUPIED)
                            else "Unverified" if cat == CAT_UNVERIFIED else "No")
        if cat == CAT_DWELLING and not rec["persons"]:
            rec["persons"], rec["persons_basis"] = n_units * per, \
                "{} per dwelling (TM 11)".format(per)
        elif cat in (CAT_UNVERIFIED, CAT_NOT_HABITABLE):
            rec["persons"], rec["persons_basis"] = 0, "-"
        done.append("category")
    typ = rv.get("acer_type")
    if typ in ACER_TYPES:
        # v2.9: the engineer has decided the type - the automatic
        # manufactured-home screening is no longer a reason to review it
        rec["mobile_rule"], rec["mobile_occupied"] = "", False
    if typ in ACER_TYPES and typ != rec["acer_type"]:
        rec["acer_type"] = typ
        rec["type_basis"] = "engineer review"
        rec["type_conflict"] = False
        done.append("type")
    if rv.get("persons") is not None:
        rec["persons"] = int(rv["persons"])
        rec["persons_basis"] = "engineer review"
        done.append("persons")
    rec["reviewed"] = "{} {}{}{}".format(
        rv.get("reviewer", ""), rv.get("date", ""),
        " ({})".format(", ".join(done)) if done else " (confirmed)",
        ": " + rv["note"] if rv.get("note") else "").strip()


def reinterpret(structs, min_area_ft2=DEFAULT_MIN_UNVERIFIED_AREA_FT2,
                persons_per_dwelling=TM11_PERSONS_PER_DWELLING, mobile_shape=True,
                mobile_max_width_ft=MOBILE_MAX_WIDTH_FT, mobile_min_ratio=MOBILE_MIN_RATIO,
                mobile_min_length_ft=MOBILE_MIN_LENGTH_FT,
                mobile_max_length_ft=MOBILE_MAX_LENGTH_FT, mobile_lot_units=True):
    """Re-apply the habitability / occupancy rules with other settings
    (v2.9: and the manufactured-home rules, see _interpret)."""
    for rec in structs:
        _interpret(rec, min_area_ft2, persons_per_dwelling, mobile_shape=mobile_shape,
                   mobile_max_width_ft=mobile_max_width_ft, mobile_min_ratio=mobile_min_ratio,
                   mobile_min_length_ft=mobile_min_length_ft,
                   mobile_max_length_ft=mobile_max_length_ft,
                   mobile_lot_units=mobile_lot_units)


def _park_polygons(parks, analysis_crs, transform_ctx, parks_crs=None, name_field="name",
                   clip=None):
    """[(geometry in analysis_crs, name)] of park polygons from a polygon
    layer or from (QgsGeometry, name) pairs in `parks_crs` (default: the
    analysis CRS)."""
    out = []
    if parks is None:
        return out
    if hasattr(parks, "getFeatures"):
        if not parks.isValid():
            return out
        names = [f.name().lower() for f in parks.fields()]
        ni = names.index(name_field.lower()) if name_field.lower() in names else None
        oi = names.index("osm_id") if "osm_id" in names else None
        xf = QgsCoordinateTransform(parks.crs(), analysis_crs, transform_ctx)
        for feat in parks.getFeatures(_request_for(parks, clip)):
            a = feat.attributes()
            name = _text(a[ni]) if ni is not None else ""
            if not name and oi is not None and _text(a[oi]):
                name = "unnamed, OSM {}".format(_text(a[oi]))
            out.append((transform_geometry(feat.geometry(), xf), name))
    else:
        xf = None
        if parks_crs is not None:
            xf = QgsCoordinateTransform(parks_crs, analysis_crs, transform_ctx)
        for item in parks:
            g, name = item[0], (item[1] if len(item) > 1 else "")
            out.append((transform_geometry(g, xf) if xf is not None else g,
                        _text(name) if name is not None else ""))
    return [(g, n) for g, n in out if g is not None and not g.isNull() and not g.isEmpty()]


def mark_mobile_parks(structs, parks, analysis_crs, transform_ctx, parks_crs=None,
                      name_field="name", clip=None, **interpret_kw):
    """v2.9: mobile-home evidence "inside mobile-home park '<name>'" for the
    structures inside a mobile-home / trailer park polygon (e.g. the
    OpenStreetMap parks of osm_parse.trailer_park_features, loaded as a layer),
    then the structures are typed again (_interpret with `interpret_kw`, e.g.
    min_area_ft2, persons_per_dwelling and the MOBILE_RULE_DEFAULTS keys).

    parks: a polygon layer (name from `name_field`, else its osm_id), or
        (QgsGeometry, name) pairs in `parks_crs` (None: the analysis CRS)
    A structure is inside when its point (outline: point on surface) lies in
    the polygon. The evidence only types Dwelling / Unverified structures
    (a park office or club house stays what it is) and is kept in
    rec["mobile_park"], so later reinterpret() calls keep it. Earlier marks
    are cleared first. Campground records are left alone. Returns the
    number of structures inside a park."""
    had = [r for r in structs if r.pop("mobile_park", None) is not None]
    polys = _park_polygons(parks, analysis_crs, transform_ctx, parks_crs, name_field, clip)
    marked = []
    if polys:
        widths = [max(g.boundingBox().width(), g.boundingBox().height(), 1e-9)
                  for g, _n in polys]
        grid = _Grid(max(widths))
        for k, (g, _n) in enumerate(polys):
            grid.insert_rect(k, g.boundingBox())
        for rec in structs:
            if rec.get("camp") or any(s.get("camp") for s in rec.get("sources", ())):
                continue
            try:
                p = rec["point"] if not rec.get("footprint") and rec.get("point") is not None \
                    else rec["geom"].pointOnSurface().asPoint()
            except Exception:
                continue
            pg = QgsGeometry.fromPointXY(QgsPointXY(p.x(), p.y()))
            for k in sorted(set(grid.near(p.x(), p.y(), rings=0))):
                g, name = polys[k]
                if g.boundingBox().contains(p) and g.intersects(pg):
                    rec["mobile_park"] = name
                    marked.append(rec)
                    break
    for rec in {id(r): r for r in had + marked}.values():
        _interpret(rec, **interpret_kw)
    if marked:
        log_message("Mobile-home parks: {} structure(s) inside {} park polygon(s) - "
                    "mobile-home evidence for the dwellings among them".format(
                        len(marked), len(polys)))
    return len(marked)


def _locations(xs, ys, d, v, a, dvv, vel_ok, zone_cls):
    """Where the governing values of a structure were read (analysis CRS):
    {"depth": (x, y, value), "vel", "arr", "dv", "zone": (x, y, zone)}."""
    loc = {}
    i = int(np.argmax(d))
    loc["depth"] = (float(xs[i]), float(ys[i]), float(d[i]))
    if vel_ok and v.size:
        j = int(np.argmax(v))
        loc["vel"] = (float(xs[j]), float(ys[j]), float(v[j]))
    if a is not None and np.isfinite(a).any():
        k = int(np.nanargmin(a))
        loc["arr"] = (float(xs[k]), float(ys[k]), float(a[k]))
    if dvv is not None and np.isfinite(dvv).any():
        m = int(np.nanargmax(dvv))
        loc["dv"] = (float(xs[m]), float(ys[m]), float(dvv[m]))
    if zone_cls is not None and zone_cls.size:
        worst = zone_cls.min()
        cand = np.nonzero(zone_cls == worst)[0]
        n = int(cand[np.argmax(d[cand])])              # deepest cell of the worst zone
        loc["zone"] = (float(xs[n]), float(ys[n]), float(worst))
    return loc


# --------------------------------------------------------------- sampling --
COARSE_TARGET = 2048          # coarse wet mask: at most this many cells per side
ONEPASS_MIN = 8               # fewer candidates: the per-structure reads are cheaper


def _coarse_wet_mask(sampler, rows_per_block=1024):
    """(mask, factor) - a coarse boolean grid of the depth raster, True where
    any cell of a factor x factor block has depth > 0. One pass over the
    raster in strips; None when the raster cannot be read."""
    if not sampler.ok:
        return None
    w, h = sampler.xsize, sampler.ysize
    f = max(1, int(math.ceil(max(w, h) / float(COARSE_TARGET))))
    cw, ch = int(math.ceil(w / float(f))), int(math.ceil(h / float(f)))
    mask = np.zeros((ch, cw), dtype=bool)
    # v2.12: whole coarse rows per strip, at most READ_BLOCK_TARGET_CELLS cells
    rows_per_block = strip_rows(w, h, rows_per_block)
    rows = max(f, (rows_per_block // f) * f)
    for y0 in range(0, h, rows):
        hh = min(rows, h - y0)
        arr = sampler.window_values(0, y0, w, hh)
        if arr is None:
            return None
        with np.errstate(invalid="ignore"):
            wet = np.nan_to_num(arr, nan=0.0) > 0
        if not wet.any():
            continue
        ph, pw = int(math.ceil(hh / float(f))) * f, cw * f
        pad = np.zeros((ph, pw), dtype=bool)
        pad[:hh, :w] = wet
        coarse = pad.reshape(ph // f, f, pw // f, f).max(axis=(1, 3))
        r0 = y0 // f
        mask[r0:r0 + coarse.shape[0], :] |= coarse
    return mask, f


def _near_flood(mask_f, sampler, xmin, ymin, xmax, ymax):
    """True when the bbox (raster CRS) touches a wet coarse cell."""
    if mask_f is None:
        return True
    mask, f = mask_f
    inv = sampler.inv_gt
    cs, rs = [], []
    for x, y in ((xmin, ymin), (xmin, ymax), (xmax, ymin), (xmax, ymax)):
        cs.append(inv[0] + x * inv[1] + y * inv[2])
        rs.append(inv[3] + x * inv[4] + y * inv[5])
    c0, c1 = int(math.floor(min(cs))) // f, int(math.floor(max(cs))) // f
    r0, r1 = int(math.floor(min(rs))) // f, int(math.floor(max(rs))) // f
    ch, cw = mask.shape
    c0, c1 = max(0, c0), min(cw - 1, c1)
    r0, r1 = max(0, r0), min(ch - 1, r1)
    if c0 > c1 or r0 > r1:
        return False
    return bool(mask[r0:r1 + 1, c0:c1 + 1].any())


def _colour_classes(items):
    """Split (sid, bbox) items into lists whose bboxes do not overlap within
    a list, so one rasterisation per list gives every region its own cells
    (a cell holds one id). Greedy, in bbox order."""
    if not items:
        return []
    widths = [max(bb.xMaximum() - bb.xMinimum(), bb.yMaximum() - bb.yMinimum(), 1e-9)
              for _s, bb in items]
    cell = max(1e-6, float(np.median(widths)) * 4.0)
    classes, grids = [], []
    for sid, bb in sorted(items, key=lambda it: (it[1].yMinimum(), it[1].xMinimum())):
        placed = False
        for k, grid in enumerate(grids):
            clash = False
            k0, k1 = grid._key(bb.xMinimum(), bb.yMinimum()), grid._key(bb.xMaximum(),
                                                                          bb.yMaximum())
            for i in range(k0[0], k1[0] + 1):
                for j in range(k0[1], k1[1] + 1):
                    for other in grid.cells.get((i, j), ()):
                        if other.intersects(bb):
                            clash = True
                            break
                    if clash:
                        break
                if clash:
                    break
            if not clash:
                classes[k].append(sid)
                grid.insert_rect(bb, bb)
                placed = True
                break
        if not placed:
            g = _Grid(cell)
            g.insert_rect(bb, bb)
            grids.append(g)
            classes.append([sid])
    return classes


def _onepass_cells(regions, sampler, progress=None):
    """{sid: (xs, ys, values)} of the valid depth cells whose centre lies in
    each region (regions: {sid: QgsGeometry in the raster CRS}), from one
    rasterisation per colour class over the rows the regions cover, instead
    of one window read and one rasterisation per structure. Raises when the
    GDAL / OGR path is unavailable (the caller falls back)."""
    from osgeo import gdal, ogr
    from .helpers import _wkb_bytes
    if sampler.backend != "gdal" or not regions:
        raise RuntimeError("one-pass sampling needs the GDAL reader")
    items = [(sid, g.boundingBox()) for sid, g in regions.items()]
    classes = _colour_classes(items)
    # pixel window covering every region
    xs = [v for _s, bb in items for v in (bb.xMinimum(), bb.xMaximum())]
    ys = [v for _s, bb in items for v in (bb.yMinimum(), bb.yMaximum())]
    win = sampler._window_for_bbox(min(xs), min(ys), max(xs), max(ys))
    out = {}
    if win is None:
        return out
    c0, r0, w, h = win
    rows = strip_rows(w, h)                       # <= READ_BLOCK_TARGET_CELLS cells
    drv_mem = gdal.GetDriverByName("MEM")
    drv_ogr = ogr.GetDriverByName("MEM") or ogr.GetDriverByName("Memory")
    if drv_mem is None or drv_ogr is None:
        raise RuntimeError("GDAL MEM / OGR Memory drivers unavailable")
    ds_v = drv_ogr.CreateDataSource("regions")
    layers = []
    for k, sids in enumerate(classes):
        lyr = ds_v.CreateLayer("c{}".format(k), None, ogr.wkbPolygon)
        lyr.CreateField(ogr.FieldDefn("sid", ogr.OFTInteger))
        defn = lyr.GetLayerDefn()
        for sid in sids:
            og = ogr.CreateGeometryFromWkb(_wkb_bytes(regions[sid]))
            if og is None:
                continue
            if og.HasCurveGeometry():
                og = og.GetLinearGeometry()
            og.FlattenTo2D()
            f = ogr.Feature(defn)
            f.SetGeometry(og)
            f.SetField("sid", int(sid))
            lyr.CreateFeature(f)
        layers.append(lyr)
    gt = sampler.gt
    n_strips = int(math.ceil(h / float(rows)))
    for si, y0 in enumerate(range(r0, r0 + h, rows)):
        if progress and si % 4 == 0:
            progress(100.0 * si / max(1, n_strips), "Sampling structures: strip {} of {}".format(
                si + 1, n_strips))
        hh = min(rows, r0 + h - y0)
        depth = sampler.window_values(c0, y0, w, hh)
        if depth is None:
            continue
        wet_any = np.isfinite(depth)
        if not wet_any.any():
            continue
        wgt = sampler._window_gt(c0, y0)
        # strip bbox in raster CRS for the spatial filter
        bx = [wgt[0] + cc * wgt[1] + rr * wgt[2] for cc in (0, w) for rr in (0, hh)]
        by = [wgt[3] + cc * wgt[4] + rr * wgt[5] for cc in (0, w) for rr in (0, hh)]
        for lyr in layers:
            try:
                lyr.SetSpatialFilterRect(min(bx), min(by), max(bx), max(by))
                if lyr.GetFeatureCount() == 0:
                    continue
            except Exception:
                pass
            mem = drv_mem.Create("", int(w), int(hh), 1, gdal.GDT_Int32)
            mem.SetGeoTransform(wgt)
            band = mem.GetRasterBand(1)
            band.Fill(0)
            err = gdal.RasterizeLayer(mem, [1], lyr, options=["ATTRIBUTE=sid"])
            if err not in (0, None):
                raise RuntimeError("GDAL RasterizeLayer returned error {}".format(err))
            ids = np.asarray(band.ReadAsArray())
            mem = band = None
            sel = (ids > 0) & wet_any
            if not sel.any():
                continue
            rr, cc = np.nonzero(sel)
            sids = ids[rr, cc]
            vals = depth[rr, cc]
            colc = cc + (c0 + 0.5)
            rowc = rr + (y0 + 0.5)
            px = gt[0] + colc * gt[1] + rowc * gt[2]
            py = gt[3] + colc * gt[4] + rowc * gt[5]
            order = np.argsort(sids, kind="stable")
            sids, px, py, vals = sids[order], px[order], py[order], vals[order]
            uniq, starts = np.unique(sids, return_index=True)
            ends = list(starts[1:]) + [sids.size]
            for sid, a, b in zip(uniq.tolist(), starts.tolist(), ends):
                prev = out.get(sid)
                part = (px[a:b], py[a:b], vals[a:b])
                out[sid] = part if prev is None else tuple(
                    np.concatenate((p, q)) for p, q in zip(prev, part))
        for lyr in layers:
            try:
                lyr.SetSpatialFilter(None)
            except Exception:
                pass
    return out


def apply_type(rec, one_d=False, near_ft=DEFAULT_NEAR_THRESHOLD_FT, borderline_higher=False):
    """Set the structure's zone, margins, borderline flag, sample locations
    and ND outcome from its ACER type (house-on-foundation or mobile-home
    curve) and the per-type results kept by evaluate_structures - so a
    changed type (engineer review), 1D / 2D choice or borderline setting is
    applied without reading the rasters again."""
    types = rec.get("_types")
    rec.update({"near": "", "zone_note": "", "near_raised": False})
    if not types or rec.get("max_depth") is None:
        rec["zone"] = rec["zone_mm"] = rec["margin_high"] = rec["margin_judg"] = None
        rec["loc"] = None
        rec["outcome"] = structure_outcome(rec, one_d)
        return
    typ = curve_type(rec["acer_type"])
    t = types.get(typ) or types[TYPE_HOUSE]
    rec["zone"], rec["loc"] = t["zone"], t["loc"]
    rec["zone_mm"] = t["zone"] if t.get("has_vel") else None
    rec["margin_high"], rec["margin_judg"] = t.get("margin_high"), t.get("margin_judg")
    if one_d:
        # 1D model (ND 5.2.1.1): probable loss of life above 2 ft of depth;
        # borderline = within near_ft below that (ND 5.3(f))
        m1 = ND_1D_DEPTH_FT - rec["max_depth"]
        rec["margin_high"] = rec["margin_judg"] = None   # ACER margins do not decide 1D
        if near_ft and near_ft > 0 and 0 <= m1 <= near_ft:
            rec["near"], rec["margin_high"] = "High", float(m1)
            if borderline_higher:
                rec["near_raised"] = True
                rec["zone_note"] = ("counted as probable loss of life ({:.2f} ft below "
                                    "the 1D 2-ft rule, ND 5.3(f))".format(m1))
    elif t.get("has_vel") and near_ft and near_ft > 0:
        if rec["zone"] == CLASS_JUDGMENT and 0 < rec["margin_high"] <= near_ft:
            rec["near"] = "High"
        elif rec["zone"] == CLASS_LOW and 0 < rec["margin_judg"] <= near_ft:
            rec["near"] = "Judgment"
        if rec["near"] and borderline_higher:
            rec["zone_note"] = "moved from {} ({:.2f} ft below the boundary, " \
                "ND 5.3(f))".format(zone_label(rec["zone"]), rec["margin_high"]
                                    if rec["near"] == "High" else rec["margin_judg"])
            rec["zone"] = CLASS_HIGH if rec["near"] == "High" else CLASS_JUDGMENT
    rec["outcome"] = structure_outcome(rec, one_d)


_RESET = {"max_depth": None, "max_vel": None, "arrival": None, "zone_found": None,
          "zone_mobile": None, "zone": None, "zone_mm": None, "vel_missing": False,
          "dv_max": None, "margin_high": None, "margin_judg": None, "near": "",
          "zone_note": "", "loc": None, "near_raised": False, "region": None,
          "reach_ft": None, "region_kind": "", "n_cells": 0, "sampling": "",
          "cells_xy": None, "_types": None}


# v2.7.1: which cells around a structure are read for its maximum depth / velocity
SAMPLE_AREA, SAMPLE_RING, SAMPLE_BAND = "area", "ring", "band"
SAMPLE_AREAS = ((SAMPLE_AREA, "Whole area: outline + 5-ft buffer (default)"),
                (SAMPLE_RING, "Buffer ring only: outside the wall, no cells inside"),
                (SAMPLE_BAND, "Band along the outline: 5 ft either side of the wall"))


def _sample_region(rec, full, r_disc, edge, mode):
    """The area the cells are read from for `mode` (SAMPLE_RING / SAMPLE_BAND),
    cut from `full` (the outline or point disc grown by the edge ring), or
    None when nothing sensible is left (the whole area is then used).
    r_disc: the assumed radius of a point structure (None with an outline)."""
    geom = rec["geom"]
    if r_disc is None:                                  # building outline
        cut = geom if mode == SAMPLE_RING else geom.buffer(-edge, 8)
    else:                                               # a point: its assumed disc
        inner = r_disc if mode == SAMPLE_RING else r_disc - edge
        if inner <= 0:
            return None
        cut = geom.buffer(inner, 8)
    if cut is None or cut.isNull() or cut.isEmpty():
        return None                                     # a wall thinner than the band
    out = full.difference(cut)
    if out is None or out.isNull() or out.isEmpty():
        return None
    return out


def evaluate_structures(structs, samplers, analysis_crs, transform_ctx, one_d=False,
                        edge_ft=OSM_EDGE_BUFFER_FT, point_radius_ft=DEFAULT_POINT_RADIUS_FT,
                        progress_cb=None, lat=None, near_ft=DEFAULT_NEAR_THRESHOLD_FT,
                        borderline_higher=False, keep_cells=False, sample_area=SAMPLE_AREA,
                        keep_dv=False, fast_sampling=False):
    """Depth/velocity/arrival and ACER zone for each structure (in place).

    samplers: {"depth", "vel", "arr"} RasterSamplers (depth in analysis_crs)
    and optionally "dv" (depth x velocity max raster, flood severity). Also
    records the depth margin to the next more severe boundary (from
    Dmax/Vmax) and flags structures within `near_ft` of it; with
    borderline_higher they are moved to that zone (ND 5.3(f)).

    v2.6: one pass instead of one read per structure - a coarse wet mask
    finds the structures near the flood, every evaluation area is rasterised
    at once (per colour class of non-overlapping areas) and the velocity /
    arrival / D x V rasters are read for all cells together. Results are
    identical to the per-structure reads (same cells: centres inside the
    area). Each evaluated structure records the evaluation area
    (rec["region"]: the outline grown by the edge ring, or the point's
    disc), its reach in feet, the number of wet cells read and, with
    keep_cells, the centres of those cells (Structure_Evaluation_Areas /
    Structure_Sampled_Cells layers). Per-type results (both curves) are kept
    in rec["_types"] so apply_type() can re-apply a changed type or setting
    without the rasters.

    v2.7.1 sample_area: SAMPLE_AREA (default, as before) reads every cell
    whose centre is in the outline grown by the edge ring; SAMPLE_RING only
    the ring outside the wall; SAMPLE_BAND a band of the edge width on both
    sides of the wall - so the maximum points lie along the buffer / outline.
    A structure with no cell centre in the ring / band (grid coarser than the
    ring) is read over the whole area instead, and says so in "sampling".

    v2.8 keep_dv (default False - nothing changes): keep the wet cells the
    maximum came from, compactly, as rec["_dv"] = (depth, velocity) float32
    arrays (velocity None without a velocity raster; NaN velocities as used,
    i.e. 0). Only wet structures get them. sensitivity.py re-classifies from
    these arrays with scaled depth / velocity, without reading the rasters.

    v2.9 fast_sampling (default False - nothing changes): select the cells
    with fastsample.py (numpy, exact distances) instead of GEOS regions +
    GDAL rasterisation; GEOS regions are then built only for the wet
    structures (rec["region"], display) and for the coarse-grid fallback.
    Same cells except in the arc/chord gap of GEOS buffers (see fastsample);
    any error, a rotated raster or an unsupported geometry uses the GEOS path."""
    if sample_area not in (SAMPLE_AREA, SAMPLE_RING, SAMPLE_BAND):
        sample_area = SAMPLE_AREA
    if lat is None and samplers["depth"].layer is not None:
        try:
            lat = center_latitude(samplers["depth"].layer.extent(), analysis_crs, transform_ctx)
        except Exception:
            lat = None
    edge = _ft_units(edge_ft, analysis_crs, lat)
    dep = samplers["depth"]
    to_vel = QgsCoordinateTransform(analysis_crs, samplers["vel"].crs, transform_ctx) \
        if samplers["vel"].crs is not None else None
    to_arr = QgsCoordinateTransform(analysis_crs, samplers["arr"].crs, transform_ctx) \
        if samplers["arr"].crs is not None else None
    dvs = samplers.get("dv")
    to_dv = QgsCoordinateTransform(analysis_crs, dvs.crs, transform_ctx) \
        if dvs is not None and dvs.crs is not None else None
    curves = {TYPE_HOUSE: curve_pair("found"), TYPE_MOBILE: curve_pair("mobile")}
    problems = LimitedLogger("Structure evaluation")
    total = len(structs) or 1

    def tick(p, m):
        if progress_cb:
            progress_cb(p, m)

    # ---- 1. reset, evaluation areas of the structures near the flood --------
    tick(0, "Finding structures near the flood...")
    # (the provider reader has no fast whole-raster pass: every structure is
    # then a candidate, as before)
    mask_f = _coarse_wet_mask(dep) if dep.ok and dep.backend == "gdal" else None
    regions = {}
    fulls = {}                       # ring / band mode: the whole area, as a fallback
    fast = {}                        # v2.9 fast_sampling: id -> (Shape, reach, r_disc)

    def _geos_region(rec, reach, r_disc):
        """(region, full): the GEOS evaluation region of `rec` for sample_area
        and, for ring / band, the whole area (else None); labels region_kind."""
        region = rec["geom"].buffer(reach, 8)
        full = None
        if sample_area != SAMPLE_AREA:
            part = _sample_region(rec, region, r_disc, edge, sample_area)
            if part is not None:
                full, region = region, part
                rec["region_kind"] += ("; buffer ring only (outside the wall)"
                                       if sample_area == SAMPLE_RING else
                                       "; band {:g} ft either side of the wall".format(
                                           edge_ft))
        return region, full

    use_fast = bool(fast_sampling) and dep.ok and FS.grid_ok(dep.gt)
    for i, rec in enumerate(structs):
        rec.update(_RESET)
        rec.pop("_dv", None)                 # v2.8: never stale from an earlier evaluation
        try:
            r_disc = None
            if rec["footprint"]:
                reach = edge
                rec["reach_ft"] = float(edge_ft)
                rec["region_kind"] = "footprint + {:g} ft edge ring".format(edge_ft)
            else:
                r_ft = point_radius_ft
                if rec.get("sqft"):
                    r_ft = max(5.0, math.sqrt(float(rec["sqft"]) / math.pi))
                r_disc = _ft_units(r_ft, analysis_crs, lat)
                reach = r_disc + edge
                rec["reach_ft"] = float(r_ft + edge_ft)
                rec["region_kind"] = "point: {:g} ft radius{} + {:g} ft edge ring".format(
                    r_ft, " (from the floor area)" if rec.get("sqft") else "", edge_ft)
            bb = rec["geom"].boundingBox()
            # dry structures (most of them in a large study area) are dropped
            # by the coarse wet mask before any GEOS work
            if not _near_flood(mask_f, dep, bb.xMinimum() - reach, bb.yMinimum() - reach,
                               bb.xMaximum() + reach, bb.yMaximum() + reach):
                continue
            if use_fast:
                shp = FS.shape_from_geometry(rec["geom"], r_disc)
                if shp is not None:
                    fast[rec["id"]] = (shp, reach, r_disc)
                    continue
            region, full = _geos_region(rec, reach, r_disc)
            if full is not None:
                fulls[rec["id"]] = full
            rec["region"] = region
            regions[rec["id"]] = region
        except Exception as e:
            problems.log("structure {}: {}".format(rec["id"], e))
    by_id = {rec["id"]: rec for rec in structs}

    # ---- 1b. v2.9 fast sampling: the cells of the fast structures -------------
    fast_cells, fast_fb, fast_wet = {}, set(), []
    if fast:
        got = None
        try:
            got = FS.sample_cells({s: (v[0], v[1]) for s, v in fast.items()}, dep,
                                  sample_area, edge, progress=lambda p, m: tick(5 + 0.45 * p, m))
        except CancelledByUser:
            raise
        except Exception as e:
            log_message("Fast sampling not used ({}); using GEOS regions".format(e))
        if got is None:                              # rotated raster / error: GEOS path
            for sid, (_shp, reach, r_disc) in fast.items():
                rec = by_id[sid]
                try:
                    region, full = _geos_region(rec, reach, r_disc)
                    if full is not None:
                        fulls[sid] = full
                    rec["region"] = regions[sid] = region
                except Exception as e:
                    problems.log("structure {}: {}".format(sid, e))
            fast = {}
        else:
            fast_cells, fast_fb, _whole = got
            for sid, (_shp, reach, r_disc) in fast.items():
                if sid in fast_cells:
                    fast_wet.append(sid)
                    continue
                # no valid cell in the (whole) area: the GEOS region only serves the
                # "cell under the centre" fallback below
                rec = by_id[sid]
                try:
                    region, full = _geos_region(rec, reach, r_disc)
                    if full is not None:
                        rec["region_kind"] += " (no cell centre in it - whole area used)"
                        region = full
                    regions[sid] = region
                except Exception as e:
                    problems.log("structure {}: {}".format(sid, e))

    # ---- 2. depth cells of every area: one pass, else per structure ----------
    cells = None
    if len(regions) >= ONEPASS_MIN:
        try:
            cells = _onepass_cells(regions, dep, progress=lambda p, m: tick(5 + 0.45 * p, m))
        except CancelledByUser:
            raise
        except Exception as e:
            log_message("One-pass sampling not used ({}); reading structure by structure"
                        .format(e))
            cells = None
    if cells is None:
        cells = {}
        n = len(regions) or 1
        for i, (sid, region) in enumerate(regions.items()):
            if i % PROGRESS_INTERVAL == 0:
                tick(5 + 45.0 * i / n, "Evaluating structures: {} of {}".format(i, n))
            xs, ys, d = dep.cells_in_geometry(region)
            if d.size:
                cells[sid] = (xs, ys, d)
    cells.update(fast_cells)
    # ring / band without a cell centre inside (grid coarser than the ring):
    # the whole area is read instead
    for sid in [s_ for s_ in fulls if s_ not in cells]:
        by_id[sid]["region"] = regions[sid] = fulls[sid]
        by_id[sid]["region_kind"] += " (no cell centre in it - whole area used)"
        xs, ys, d = dep.cells_in_geometry(regions[sid])
        if d.size:
            cells[sid] = (xs, ys, d)
    # areas without a cell centre inside (grid coarser than the building):
    # the cell under the area's centre
    coarse_ids = [sid for sid in regions if sid not in cells]
    if coarse_ids:
        cx = np.array([regions[s].centroid().asPoint().x() for s in coarse_ids])
        cy = np.array([regions[s].centroid().asPoint().y() for s in coarse_ids])
        cd = dep.values_at(cx, cy)
        for sid, x, y, d in zip(coarse_ids, cx, cy, cd):
            by_id[sid]["sampling"] = "grid coarser than the area: the cell under its centre"
            if np.isfinite(d):
                cells[sid] = (np.array([x]), np.array([y]), np.array([d]))
    # wet cells only; a structure without one is dry and, as before 2.6,
    # keeps no evaluation area
    wet_ids = []
    for sid in list(cells):
        xs, ys, d = cells[sid]
        wet = np.isfinite(d) & (d > 0)
        if not wet.any():
            del cells[sid]
            continue
        cells[sid] = (xs[wet], ys[wet], d[wet])
        wet_ids.append(sid)
        rec = by_id[sid]
        if not rec["sampling"]:
            rec["sampling"] = "cells whose centre lies in the area"
        rec["n_cells"] = int(cells[sid][2].size)

    wet_set = set(wet_ids)
    for sid in regions:
        if sid not in wet_set:
            by_id[sid]["region"] = None
    for sid in fast_wet:                 # v2.9: the evaluation area shown for a wet structure
        if sid not in wet_set:
            continue
        rec = by_id[sid]
        try:
            region, full = _geos_region(rec, fast[sid][1], fast[sid][2])
            if full is not None and sid in fast_fb:
                rec["region_kind"] += " (no cell centre in it - whole area used)"
                region = full
            rec["region"] = region
        except Exception as e:
            problems.log("structure {} evaluation area: {}".format(sid, e))
    # ---- 3. velocity, arrival, D x V for all wet cells at once ---------------
    tick(55, "Reading velocity and arrival at {} structure(s)...".format(len(wet_ids)))
    if wet_ids:
        counts = [cells[s][2].size for s in wet_ids]
        offs = np.concatenate(([0], np.cumsum(counts)))
        ax_all = np.concatenate([cells[s][0] for s in wet_ids])
        ay_all = np.concatenate([cells[s][1] for s in wet_ids])
        vx, vy = transform_xy(ax_all, ay_all, to_vel)
        v_all = samplers["vel"].values_at(vx, vy)
        rx, ry = transform_xy(ax_all, ay_all, to_arr)
        a_all = samplers["arr"].values_at(rx, ry)
        dv_all = None
        if dvs is not None and dvs.ok:
            px, py = transform_xy(ax_all, ay_all, to_dv)
            dv_all = dvs.values_at(px, py)
    vel_ok = samplers["vel"].ok

    # ---- 4. per structure: maxima, zones for both curves, margins -----------
    n = len(wet_ids) or 1
    for i, sid in enumerate(wet_ids):
        if i % PROGRESS_INTERVAL == 0:
            tick(60 + 35.0 * i / n, "Evaluating structures: {} of {}".format(i, n))
        rec = by_id[sid]
        try:
            xs, ys, d = cells[sid]
            a0, a1 = int(offs[i]), int(offs[i + 1])
            v = v_all[a0:a1]
            a = a_all[a0:a1]
            if np.isnan(v).any() and vel_ok:
                rec["vel_missing"] = True
                v = np.where(np.isnan(v), 0.0, v)
            if keep_cells:
                rec["cells_xy"] = (np.array(xs, dtype=np.float64), np.array(ys, dtype=np.float64))
            if keep_dv:
                rec["_dv"] = (np.asarray(d, dtype=np.float32).copy(),
                              np.asarray(v, dtype=np.float32).copy() if vel_ok else None)
            rec["max_depth"] = float(d.max())
            rec["max_vel"] = float(v.max()) if vel_ok else None
            rec["arrival"] = float(np.nanmin(a)) if np.isfinite(a).any() else None
            dvv = None
            if dv_all is not None:
                dvv = dv_all[a0:a1]
                rec["dv_max"] = float(np.nanmax(dvv)) if np.isfinite(dvv).any() else None
            elif vel_ok and (not rec["vel_missing"] or v.any()):
                dvv = d * v
                rec["dv_max"] = float(dvv.max())
            types = {}
            for typ in (TYPE_HOUSE, TYPE_MOBILE):
                zcls = classify_values(d, v, *curves[typ])
                t = {"zone": int(zcls.min()), "has_vel": vel_ok, "margin_high": None,
                     "margin_judg": None, "loc": None}
                try:
                    t["loc"] = _locations(xs, ys, d, v, a, dvv, vel_ok, zcls)
                except Exception as e:                       # never fails the structure
                    problems.log("structure {} sample locations: {}".format(rec["id"], e))
                if vel_ok:
                    mh, mj = zone_margins(d, v, *curves[typ])
                    t["margin_high"], t["margin_judg"] = float(mh.min()), float(mj.min())
                types[typ] = t
            rec["zone_found"], rec["zone_mobile"] = types[TYPE_HOUSE]["zone"], \
                types[TYPE_MOBILE]["zone"]
            rec["_types"] = types
        except Exception as e:
            problems.log("structure {}: {}".format(rec["id"], e))
    for rec in structs:
        apply_type(rec, one_d, near_ft, borderline_higher)
    # 2.16.0 collapse_dwellings(structs) is not applied (audit): it zeroed the
    # persons of every further habitable building at one street address
    # (apartment buildings, mobile-home parks and farmsteads on one address)
    # and was not kept per scenario. Same-building duplicates are already
    # merged by build_inventory.
    tick(100, "Structures evaluated")
    problems.summary()
    return structs


def structure_outcome(rec, one_d=False):
    """ND 5.2.1.1 outcome for one structure."""
    if rec.get("max_depth") is None or rec["max_depth"] <= 0:
        return OUT_DRY
    if one_d:
        if rec["max_depth"] < CONSEQUENCE_DEPTH_FT and rec["max_depth"] <= ND_1D_DEPTH_FT \
                and not rec.get("near_raised"):
            return OUT_TRACE
        return OUT_PROBABLE if rec["max_depth"] > ND_1D_DEPTH_FT or rec.get("near_raised") \
            else OUT_ECONOMIC
    if rec.get("zone") == CLASS_HIGH:
        return OUT_PROBABLE
    if rec.get("zone") == CLASS_JUDGMENT:
        return OUT_JUDGMENT
    # Below the model-noise tolerance only; any real depth is economic loss (5.2.2).
    if rec["max_depth"] < CONSEQUENCE_DEPTH_FT:
        return OUT_TRACE
    return OUT_ECONOMIC


_STREET_ABBREV = {
    "avenue": "ave", "ave": "ave", "av": "ave", "street": "st", "str": "st",
    "road": "rd", "drive": "dr", "boulevard": "blvd", "blvd": "blvd",
    "lane": "ln", "court": "ct", "place": "pl", "trail": "trl", "circle": "cir",
    "highway": "hwy", "parkway": "pkwy", "southeast": "se", "southwest": "sw",
    "northeast": "ne", "northwest": "nw", "north": "n", "south": "s",
    "east": "e", "west": "w",
}


def normalize_street(text):
    """One key for '146th Avenue Southeast' and '146th Ave SE'."""
    raw = re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).strip()
    if not raw:
        return ""
    return " ".join(_STREET_ABBREV.get(p, p) for p in raw.split())


def _dwelling_rank(rec):
    rank = {CLASS_HIGH: 3, CLASS_JUDGMENT: 2, CLASS_LOW: 1}.get(rec.get("zone"), 0)
    return (rank, rec.get("max_depth") or 0.0, rec.get("persons") or 0,
            rec.get("area_ft2") or 0.0)


def _mark_dwelling_extra(rec, keep, why):
    rec["dwelling_extra"] = True
    rec["persons"] = 0
    rec["persons_basis"] = "same dwelling as structure {} ({})".format(keep.get("id"), why)
    rec.setdefault("notes", []).append("same dwelling as structure {}".format(keep.get("id")))


def _footprints_overlap(a, b):
    ga, gb = a.get("geom"), b.get("geom")
    if ga is None or gb is None:
        return False
    try:
        return bool(ga.intersects(gb) and not ga.touches(gb))
    except Exception:
        return False


def collapse_dwellings(structs):
    """One household per street address, and per overlapping unaddressed footprint.

    A second polygon at the same address is still sampled. It does not add
    another 3-person household or another judgment-zone structure. A unit
    number stays in the address, so apartments are not merged.
    """
    for rec in structs:
        rec["dwelling_extra"] = False
    groups = {}
    for rec in structs:
        if rec.get("habitable") != "Yes":
            continue
        key = normalize_street(address_of(rec)[0])
        if key:
            groups.setdefault(key, []).append(rec)
    n_extra = 0
    for key, group in groups.items():
        if len(group) < 2:
            continue
        group.sort(key=_dwelling_rank, reverse=True)
        keep = group[0]
        for rec in group[1:]:
            _mark_dwelling_extra(rec, keep, key)
            n_extra += 1
    kept = [r for r in structs if r.get("habitable") == "Yes" and not r.get("dwelling_extra")
            and r.get("geom") is not None]
    for rec in structs:
        if rec.get("habitable") != "Yes" or rec.get("dwelling_extra"):
            continue
        if normalize_street(address_of(rec)[0]):
            continue
        for host in kept:
            if host is rec or not _footprints_overlap(rec, host):
                continue
            _mark_dwelling_extra(rec, host, "overlapping footprint")
            n_extra += 1
            break
    if n_extra:
        log_message("Structure inventory: {} extra footprint(s) of the same dwelling "
                    "are not a second household".format(n_extra))
    return n_extra


# v2.9: the address columns (Address / City / ZIP from address_of - the ND GIS
# Hub address first) and the ND GIS Hub (NENA NG9-1-1) site / structure
# address points joined to the structure. Every reader of this layer and of
# its CSV / XLSX copies uses field NAMES, so they sit after NSI_fd_id.
ND_ADDRESS_FIELDS = [
    ("Address", FT_STRING), ("City", FT_STRING), ("ZIP", FT_STRING),
    ("ND_Site_NGUID", FT_STRING), ("ND_Place_Type", FT_STRING),
    ("ND_Address_Points", FT_INT),
]
ND_FAMILY = "NDGISHUB"
TEXT_LIMIT = 250              # longest text written to a list column (Shapefile-safe)

STRUCTURE_FIELDS = [
    ("Struct_ID", FT_INT), ("Sources", FT_STRING), ("NSI_fd_id", FT_STRING),
] + ND_ADDRESS_FIELDS + [
    ("Occupancy", FT_STRING), ("FEMA_Category", FT_STRING),
    ("Habitable", FT_STRING), ("ACER_Type", FT_STRING),
    ("Type_Basis", FT_STRING), ("Type_Conflict", FT_STRING),
    ("Lifeline", FT_STRING), ("Persons", FT_INT),
    ("Persons_Basis", FT_STRING), ("Area_ft2", FT_DOUBLE),
    ("Max_Depth_ft", FT_DOUBLE), ("Max_Vel_fps", FT_DOUBLE),
    ("First_Arrival_hr", FT_DOUBLE), ("Zone_Found", FT_INT),
    ("Zone_Mobile", FT_INT), ("ACER_Zone", FT_INT),
    ("ACER_Zone_Label", FT_STRING), ("ND_Outcome", FT_STRING),
    ("DV_Max_ft2s", FT_DOUBLE), ("Margin_High_ft", FT_DOUBLE),
    ("Margin_Judg_ft", FT_DOUBLE), ("Near_Threshold", FT_STRING),
    ("Pop_Night", FT_DOUBLE), ("Pop_Day", FT_DOUBLE),
    ("Val_Struct_USD", FT_DOUBLE), ("Val_Cont_USD", FT_DOUBLE),
    ("FirstFloor_Depth_ft", FT_DOUBLE), ("Damage_USD", FT_DOUBLE),
    ("Damage_NonRes_USD", FT_DOUBLE),
    ("Damage_Basis", FT_STRING), ("Warning_hr", FT_DOUBLE),
    ("Flood_Severity", FT_STRING), ("Fatality_Rate", FT_DOUBLE),
    ("Est_Life_Loss", FT_DOUBLE), ("PAR_Night", FT_DOUBLE), ("PAR_Day", FT_DOUBLE),
    ("Access_Cut_Off", FT_STRING),
    ("Incremental", FT_STRING), ("Base_Depth_ft", FT_DOUBLE),
    ("Base_Zone", FT_INT), ("Reviewed", FT_STRING), ("Notes", FT_STRING),
]


def _nz(v):
    return NULL if v is None else v


def _yes_no(v):
    return NULL if v is None else ("Yes" if v else "No")


def join_capped(values, limit=TEXT_LIMIT, sep="; "):
    """Distinct non-empty values joined with `sep`, at most `limit`
    characters: whole values only, then "(+N more)"."""
    vals = []
    for v in values:
        t = _text(v)
        if t and t not in vals:
            vals.append(t)
    out = sep.join(vals)
    if len(out) <= limit:
        return out
    for k in range(len(vals) - 1, 0, -1):
        s = "{} (+{} more)".format(sep.join(vals[:k]), len(vals) - k)
        if len(s) <= limit:
            return s
    more = " (+{} more)".format(len(vals) - 1) if len(vals) > 1 else ""
    return vals[0][:max(0, limit - len(more) - 3)] + "..." + more


def nd_address_points(rec):
    """The ND GIS Hub (NENA) address point sources joined to a structure,
    one per point (by Site_NGUID, else by source reference)."""
    out, seen = [], set()
    for s in rec.get("sources", ()):
        if s.get("family") != ND_FAMILY:
            continue
        key = s.get("nguid") or s.get("ref") or id(s)
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
    return out


def address_fields(rec):
    """v2.9: {field: value} of the ND_ADDRESS_FIELDS for one structure -
    Address / City / ZIP from address_of (ND GIS Hub first), the Site_NGUID(s)
    and Place_Type(s) of the joined ND GIS Hub address points ("; "-joined,
    at most TEXT_LIMIT characters) and how many there are. Empty text is
    NULL; the count is 0 without an ND address point."""
    addr, city, zp = address_of(rec)
    pts = nd_address_points(rec)

    def txt(v):
        v = _text(v)[:TEXT_LIMIT]
        return v if v else NULL
    return {"Address": txt(addr), "City": txt(city), "ZIP": txt(zp),
            "ND_Site_NGUID": join_capped(s.get("nguid") for s in pts) or NULL,
            "ND_Place_Type": join_capped(s.get("place_type") for s in pts) or NULL,
            "ND_Address_Points": len(pts)}


def structure_notes(rec):
    notes = []
    if rec.get("type_conflict"):
        notes.append("sources disagree on structure type - verify")
    if rec.get("mobile_rule") == "shape":
        notes.append("ACER type from the footprint shape (possible single-wide manufactured "
                     "home) - verify")
    elif rec.get("mobile_rule") == "park_lot":
        notes.append("ACER type from a mobile-home park / lot address - verify")
    if rec.get("mobile_occupied"):
        notes.append("mobile-home evidence on a building read as occupied non-residential "
                     "- verify")
    if rec.get("vel_missing"):
        notes.append("velocity missing on some wet cells (used 0)")
    if rec.get("site_note"):
        notes.append(rec["site_note"])
    elif rec["category"] == CAT_UNVERIFIED:
        notes.append("habitability not confirmed by any source - verify")
    if rec.get("address_note"):
        notes.append(rec["address_note"])
    if rec["category"] == CAT_OCCUPIED and not rec["persons"]:
        notes.append("occupied building without occupancy data")
    moved = [x["snapped"] for x in rec["sources"] if x.get("snapped") is not None]
    if moved:
        notes.append("{} point(s) moved onto this outline".format(len(moved)))
    if rec.get("near"):
        notes.append("within the near-threshold distance of the {} boundary".format(
            rec["near"]))
    if rec.get("zone_note"):
        notes.append("zone " + rec["zone_note"])
    if rec.get("camp"):
        notes.append("campground / RV park ({}) - seasonal overnight occupancy; verify the "
                     "number of sites and when it is open".format(rec["camp"].get("shape")
                                                                  or "OSM"))
    return notes


def structures_to_layer(structs, crs, include_dry=False, name="Habitable_Structures"):
    fields = [QgsField(n, t) for n, t in STRUCTURE_FIELDS]
    lyr = create_memory_layer(name, WKB_MULTIPOLYGON, crs, fields)
    out_fields = lyr.fields()
    feats = []
    for rec in structs:
        if rec["category"] == CAT_NOT_HABITABLE:
            continue
        if not include_dry and rec.get("outcome", OUT_DRY) == OUT_DRY:
            continue
        geom = rec["geom"] if rec["footprint"] else rec.get("region") or rec["geom"].buffer(
            _ft_units(DEFAULT_POINT_RADIUS_FT, crs), 8)
        try:
            if not geom.isMultipart():
                geom = QgsGeometry(geom)
                geom.convertToMultiType()
        except Exception:
            pass
        vals = {
            "Struct_ID": rec["id"], "Sources": ";".join(sorted(rec["families"])),
            "NSI_fd_id": rec.get("fd_id") or NULL,
            "Occupancy": (rec.get("occupancy") or "")[:250],
            "FEMA_Category": (rec.get("fema_category") or "")[:250] or NULL,
            "Habitable": rec["habitable"], "ACER_Type": rec["acer_type"],
            "Type_Basis": rec["type_basis"], "Type_Conflict": _yes_no(rec["type_conflict"]),
            "Lifeline": _yes_no(rec["lifeline"]), "Persons": int(rec["persons"]),
            "Persons_Basis": rec["persons_basis"], "Area_ft2": _nz(rec.get("area_ft2")),
            "Max_Depth_ft": _nz(rec.get("max_depth")), "Max_Vel_fps": _nz(rec.get("max_vel")),
            "First_Arrival_hr": _nz(rec.get("arrival")),
            "Zone_Found": _nz(rec.get("zone_found")), "Zone_Mobile": _nz(rec.get("zone_mobile")),
            "ACER_Zone": _nz(rec.get("zone")),
            "ACER_Zone_Label": ZONE_LABELS.get(rec.get("zone"), "DRY"),
            "ND_Outcome": rec.get("outcome", OUT_DRY),
            "DV_Max_ft2s": _nz(rec.get("dv_max")), "Margin_High_ft": _nz(rec.get("margin_high")),
            "Margin_Judg_ft": _nz(rec.get("margin_judg")),
            "Near_Threshold": rec.get("near") or NULL,
            "Pop_Night": _nz(rec.get("pop_night")),
            "Pop_Day": _nz(rec.get("pop_day")),
            "Val_Struct_USD": _nz(rec.get("val_struct")), "Val_Cont_USD": _nz(rec.get("val_cont")),
            "FirstFloor_Depth_ft": _nz(rec.get("ff_depth")),
            "Damage_USD": _nz(rec.get("damage_usd")),
            "Damage_NonRes_USD": _nz(rec.get("damage_nonres_usd")),
            "Damage_Basis": rec.get("damage_basis") or NULL,
            "Warning_hr": _nz(rec.get("warning_hr")),
            "Flood_Severity": rec.get("severity") or NULL,
            "Fatality_Rate": _nz(rec.get("fatality_rate")), "Est_Life_Loss": _nz(rec.get("lol")),
            "PAR_Night": _nz(rec.get("par_night")), "PAR_Day": _nz(rec.get("par_day")),
            "Access_Cut_Off": _yes_no(rec.get("cut_off")),
            "Incremental": _yes_no(rec.get("incremental")),
            "Base_Depth_ft": _nz(rec.get("base_depth")), "Base_Zone": _nz(rec.get("base_zone")),
            "Reviewed": rec.get("reviewed") or NULL,
            "Notes": "; ".join(structure_notes(rec)),
        }
        vals.update(address_fields(rec))
        f = QgsFeature(out_fields)
        f.setGeometry(geom)
        f.setAttributes([vals[n] for n, _t in STRUCTURE_FIELDS])
        feats.append(f)
    add_features_batched(lyr.dataProvider(), feats, FEATURE_BATCH_SIZE)
    lyr.updateExtents()
    return lyr


SAMPLE_TYPES = (("depth", "Max depth", "ft"), ("vel", "Max velocity", "ft/s"),
                ("dv", "Max depth x velocity", "ft2/s"), ("arr", "Earliest arrival", "h"),
                ("zone", "Controlling ACER zone", "zone"))


def structure_sample_points_layer(structs, crs, name="Structure_Sample_Points"):
    """Where each flooded structure's governing values were read (the cells
    behind Max_Depth_ft, Max_Vel_fps, DV_Max_ft2s, First_Arrival_hr and the
    ACER zone), for checking the automatic result on the map. None if
    nothing to show."""
    fields = [QgsField("Struct_ID", FT_INT), QgsField("FEMA_Category", FT_STRING),
              QgsField("ACER_Type", FT_STRING), QgsField("Value_Type", FT_STRING),
              QgsField("Value", FT_DOUBLE), QgsField("Units", FT_STRING),
              QgsField("Label", FT_STRING)]
    lyr = create_memory_layer(name, WKB_POINT, crs, fields)
    feats = []
    for rec in structs:
        loc = rec.get("loc")
        if not loc or rec.get("outcome", OUT_DRY) == OUT_DRY:
            continue
        for key, vtype, units in SAMPLE_TYPES:
            if key not in loc:
                continue
            x, y, val = loc[key]
            label = ZONE_LABELS.get(int(val), "") if key == "zone" else \
                "{:.2f} {}".format(val, units)
            f = QgsFeature(lyr.fields())
            f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(x, y)))
            f.setAttributes([int(rec["id"]), rec.get("fema_category") or NULL,
                             rec["acer_type"], vtype,
                             float(val), units, label])
            feats.append(f)
    if not feats:
        return None
    add_features_batched(lyr.dataProvider(), feats, FEATURE_BATCH_SIZE)
    lyr.updateExtents()
    return lyr


EVAL_AREA_FIELDS = [
    ("Struct_ID", FT_INT), ("Sources", FT_STRING), ("FEMA_Category", FT_STRING),
    ("Habitable", FT_STRING), ("ACER_Type", FT_STRING), ("Area_Type", FT_STRING),
    ("Reach_ft", FT_DOUBLE), ("Sampling", FT_STRING), ("Wet_Cells", FT_INT),
    ("Max_Depth_ft", FT_DOUBLE), ("Max_Vel_fps", FT_DOUBLE), ("First_Arrival_hr", FT_DOUBLE),
    ("ACER_Zone", FT_INT), ("ACER_Zone_Label", FT_STRING), ("ND_Outcome", FT_STRING),
    ("Persons", FT_INT),
]


def _multi(geom):
    """A copy of `geom` as a multi-polygon (GeoPackage layers hold one type)."""
    try:
        g = QgsGeometry(geom)
        if not g.isMultipart():
            g.convertToMultiType()
        return g
    except Exception:
        return geom


def evaluation_areas_layer(structs, crs, name="Structure_Evaluation_Areas", include_dry=False):
    """v2.6: the exact area each structure's maximum depth and velocity were
    searched in - the building outline grown by the 5-ft edge ring (ACER TM
    11 practice: water against the wall counts), or the disc around a point
    without an outline - with the values found there. Structures whose
    surroundings were dry have no area (their region was never built) and
    are left out unless include_dry. None when there is nothing to show."""
    lyr = create_memory_layer(name, WKB_MULTIPOLYGON, crs,
                              [QgsField(n, t) for n, t in EVAL_AREA_FIELDS])
    feats = []
    for rec in structs:
        region = rec.get("region")
        if region is None or region.isNull() or region.isEmpty():
            continue
        if not include_dry and rec.get("outcome", OUT_DRY) == OUT_DRY:
            continue
        z = rec.get("zone")
        f = QgsFeature(lyr.fields())
        f.setGeometry(_multi(region))
        f.setAttributes([int(rec["id"]), ", ".join(sorted(rec.get("families") or [])),
                         rec.get("fema_category") or NULL, rec.get("habitable") or NULL,
                         rec.get("acer_type") or NULL, rec.get("region_kind") or NULL,
                         _nz(rec.get("reach_ft")), rec.get("sampling") or NULL,
                         int(rec.get("n_cells") or 0), _nz(rec.get("max_depth")),
                         _nz(rec.get("max_vel")), _nz(rec.get("arrival")),
                         NULL if z is None else int(z), ZONE_LABELS.get(z, "") or NULL,
                         rec.get("outcome") or NULL, int(rec.get("persons") or 0)])
        feats.append(f)
    if not feats:
        return None
    add_features_batched(lyr.dataProvider(), feats, FEATURE_BATCH_SIZE)
    lyr.updateExtents()
    return lyr


def sampled_cells_layer(structs, crs, cell_w, cell_h, name="Structure_Sampled_Cells",
                        max_cells=400000):
    """v2.6: the raster cells that were read for each evaluated structure
    (one multi-polygon of cell squares per structure), from the cell centres
    kept by evaluate_structures(keep_cells=True). None when the centres were
    not kept, nothing is wet, or there are more than `max_cells` cells."""
    if not cell_w or not cell_h:
        return None
    total = sum(int(r.get("n_cells") or 0) for r in structs if r.get("cells_xy") is not None)
    if total == 0 or total > max_cells:
        return None
    lyr = create_memory_layer(name, WKB_MULTIPOLYGON, crs, [
        QgsField("Struct_ID", FT_INT), QgsField("Wet_Cells", FT_INT),
        QgsField("Max_Depth_ft", FT_DOUBLE), QgsField("Max_Vel_fps", FT_DOUBLE),
        QgsField("ACER_Zone_Label", FT_STRING)])
    hw, hh = abs(float(cell_w)) / 2.0, abs(float(cell_h)) / 2.0
    feats = []
    for rec in structs:
        cells = rec.get("cells_xy")
        if cells is None or rec.get("outcome", OUT_DRY) == OUT_DRY:
            continue
        xs, ys = cells
        polys = [[[QgsPointXY(x - hw, y - hh), QgsPointXY(x + hw, y - hh),
                   QgsPointXY(x + hw, y + hh), QgsPointXY(x - hw, y + hh),
                   QgsPointXY(x - hw, y - hh)]] for x, y in zip(xs.tolist(), ys.tolist())]
        if not polys:
            continue
        f = QgsFeature(lyr.fields())
        f.setGeometry(QgsGeometry.fromMultiPolygonXY(polys))
        z = rec.get("zone")
        f.setAttributes([int(rec["id"]), len(polys), _nz(rec.get("max_depth")),
                         _nz(rec.get("max_vel")), ZONE_LABELS.get(z, "") or NULL])
        feats.append(f)
    if not feats:
        return None
    add_features_batched(lyr.dataProvider(), feats, FEATURE_BATCH_SIZE)
    lyr.updateExtents()
    return lyr


# ------------------------------------------------------------------- QA --
QA_FIELDS = ["Struct_ID", "Issue", "FEMA_Category", "Habitable", "Sources", "X", "Y", "CRS"]


def address_of(rec):
    """(address, city, zip) of a structure for notification lists: the ND GIS
    Hub (NENA) address first, then any other source with a street address."""
    best = None
    for src in rec.get("sources", ()):
        if not src.get("addr_text"):
            continue
        rank = 0 if src.get("nena") else 1
        if best is None or rank < best[0]:
            best = (rank, src)
    if best is None:
        return "", "", ""
    s = best[1]
    return s.get("addr_text", ""), s.get("city", ""), s.get("zip", "")


def inventory_qa(structs, outline_families=(STRUCTURES_FAMILY, "OSM", "USA_STRUCTURES",
                                            "OUTLINES")):
    """(record, issue) pairs for the completeness check: outlines that no
    point source describes (possible unmapped dwelling or outbuilding) and
    points without an outline (possible missing footprint)."""
    fams = set(outline_families)
    rows = []
    for rec in structs:
        if rec["footprint"]:
            if rec.get("site_note") and rec["category"] == CAT_NOT_HABITABLE:
                # v2.20.1: left out of the habitable layer - listed here
                rows.append((rec, "outbuilding: the 911 address of its parcel is on another "
                                  "building (not habitable)"))
            elif not (rec["families"] - fams) and rec["category"] != CAT_NOT_HABITABLE:
                rows.append((rec, "outline without NSI / address point"))
        else:
            rows.append((rec, "point without building outline"))
    return rows


def write_qa_csv(rows, path, crs):
    """Inventory_QA.csv (UTF-8 with BOM so Excel shows accents). A copy
    open in Excel is not overwritten: this run's table gets a new name
    (returned)."""
    import csv
    from .helpers import prepare_output_path
    path = prepare_output_path(path)
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(QA_FIELDS)
        for rec, issue in rows:
            try:
                p = rec["geom"].centroid().asPoint()
                x, y = round(p.x(), 3), round(p.y(), 3)
            except Exception:
                x = y = ""
            w.writerow([rec["id"], issue, rec.get("fema_category") or "", rec["habitable"],
                        ";".join(sorted(rec["families"])), x, y, crs.authid() or "custom"])
    return path
