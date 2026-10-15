# -*- coding: utf-8 -*-
"""Constants, ACER-11 coefficients, and README text for the ND Dam Hazard
Classification Helper. Pure data - no QGIS imports needed here."""

import os as _os

PLUGIN_NAME = "ND Dam Hazard Classification Helper"


def _version_from_metadata(fallback, path=None):
    """v2.28: the version= line of the plugin's metadata.txt (the one place the version is
    kept); `fallback` when the file cannot be read. Only a line that starts at column 0 counts,
    so the indented changelog text under it is never mistaken for it."""
    if path is None:
        path = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "metadata.txt")
    try:
        with open(path, encoding="utf-8-sig") as fh:
            for line in fh:
                if not line.startswith("version"):
                    continue
                key, sep, value = line.partition("=")
                if sep and key.strip() == "version" and value.strip():
                    return value.strip()
    except (OSError, UnicodeError):
        pass
    return fallback


PLUGIN_VERSION = _version_from_metadata("2.28.0")
OUTPUT_FOLDER_NAME = "outputs"

# ------------------------------------------------------------ layer tree ----
# Every layer the plugin adds goes into "ND Dam Hazard - <dam>" with one
# sub-group per category (layer-tree API only - works in QGIS 3.28 - 4.x)
GROUP_ROOT_FMT = "ND Dam Hazard - {}"
GRP_RESULTS = "Classification results"
GRP_SAMPLES = "Sample locations"
GRP_ZONES = "ACER hazard zones"
GRP_HYDRAULICS = "Hydraulic rasters"
GRP_DSSWISE = "DSS-WISE Lite"
GRP_EAP = "Emergency Action Plan"
GRP_DATA = "Downloaded data"            # v2.5.1: Data & Output tab downloads
GROUP_ORDER = (GRP_RESULTS, GRP_EAP, GRP_SAMPLES, GRP_ZONES, GRP_HYDRAULICS, GRP_DSSWISE,
               GRP_DATA)
# v2.3: sub-folders of <dam>/outputs. The files the engineer opens or edits
# stay in outputs itself (FEMA_Structures.gpkg, review decisions, summary,
# PDF report, run log). "Consolidation" held the Main Consolidation outputs
# of 1.x - 2.5; it is only searched so old copies of a result are retired.
SUBDIR_CLASSIFICATION = "Classification"
SUBDIR_EAP = "EAP"
SUBDIR_QA = "QA"
SUBDIR_CONSOLIDATION = "Consolidation"
OUTPUT_SUBDIRS = (SUBDIR_CLASSIFICATION, SUBDIR_EAP, SUBDIR_QA, SUBDIR_CONSOLIDATION)
# v2.5.1: downloaded data sets (Data & Output tab). Not in OUTPUT_SUBDIRS: a
# classification run never retires them.
SUBDIR_DATA = "Data"
OSM_EDGE_BUFFER_FT = 5.0
# Building-outline file (FEMA USA Structures for North Dakota) in the outputs
# folder: {dam}_FEMA_Structures.gpkg or FEMA_Structures.gpkg. Before v2.2
# it was called OSM_Outlines.gpkg; that name is still read.
STRUCTURES_TAG = "FEMA_Structures"
LEGACY_STRUCTURES_TAG = "OSM_Outlines"
STRUCTURES_FAMILY = "FEMA_STRUCTURES"  # source family of the outlines
# Study area: every structure layer (OSM outlines, NSI, ND GIS Hub, FEMA,
# POI, downloads) is limited to the depth raster's extent grown by this
# distance. Features inside it are kept even where the raster has no flood
# value (unless "keep dry features" is switched off).
DEFAULT_EXTENT_BUFFER_FT = 25

# Class numbering: 1 = HIGH (Red), 2 = JUDGMENT (Yellow), 3 = LOW (Green)
CLASS_HIGH = 1
CLASS_JUDGMENT = 2
CLASS_LOW = 3
CLASS_NODATA = -9999

BTN_ACTIVE_STYLE = (
    "background-color: #4CAF50; color: white; "
    "font-weight: bold; padding: 10px 20px; border-radius: 4px;"
)
BTN_INACTIVE_STYLE = (
    "background-color: #B0BEC5; color: #eceff1; "
    "font-weight: bold; padding: 10px 20px; border-radius: 4px;"
)

# How often (in features) the processing loops report progress. The dialog
# additionally throttles UI refreshes to PROGRESS_MIN_INTERVAL_S seconds.
PROGRESS_INTERVAL = 50
PROGRESS_MIN_INTERVAL_S = 0.1

# Hazard raster generation works on strips of rows so memory stays bounded
# no matter how large the depth grid is (~2 M cells per strip is roughly
# 100 MB of working memory).
HAZARD_BLOCK_TARGET_CELLS = 2000000
# v2.12.0: every other raster pass (structure sampling windows and strips,
# the coarse wet mask, the flood mask, the inundated extent, the first
# arrival cell, the rules comparison) reads at most this many cells per
# window - about 32 MB of float64 working memory - so a county-scale raster
# (20,000 x 20,000 cells) is never held whole in memory. The results do not
# depend on the strip size (the tests compare against the whole read).
READ_BLOCK_TARGET_CELLS = 4000000
# Build internal overviews on hazard GeoTIFFs larger than this (pixels) so
# QGIS can draw them quickly when zoomed out.
HAZARD_OVERVIEW_MIN_SIZE = 2048

# Features written to a memory layer per addFeatures() call.
FEATURE_BATCH_SIZE = 5000

# Stop logging the same kind of per-feature warning after this many, so a
# systematic problem cannot flood (and slow down) the QGIS message log.
MAX_REPEATED_WARNINGS = 20

# Excel refuses (asks to "repair") workbooks whose sheet names exceed 31 chars.
XLSX_MAX_SHEET_NAME = 31

# ==============================================================================
# ACER TM 11 / ND REG_05.2026 FLOOD DANGER CURVES
# ==============================================================================
# v2.11.0: houses on foundations (ND Fig. 5.1 = TM 11 Fig. 2) and mobile homes
# (ND Fig. 5.2 = TM 11 Fig. 3) come from the TABLE digitized by the engineer
# from the REG_05.2026 figures (damsuite.tm11_curves - the same table in ND Dam
# Breach Runner and ND Dam Breach Screening): linear between the tabulated
# velocities, stated accuracy +/-0.06 ft. Checked against screenshots of the
# two REG_05.2026 figures (pixel trace): houses median 0.03 ft, 95 % within
# 0.05 ft (high line) / 0.09 ft (low line); mobile homes median 0.008 / 0.004
# ft, 95 % within 0.02 ft. Until v2.10.0 these four curves were the quartic
# fits kept below as QUARTIC_COEFFICIENTS_V2_10 (for the record and for
# comparisons); they differ from the table by up to 0.10 ft (house high line
# at 0-1 ft/s: 5.86 ft vs 5.97 ft) and 0.18 ft (house low line at 18-19 ft/s).
# Passenger vehicles (TM 11 Fig. 4, not in REG_05.2026) stay a quartic.
# Quartic ORDER: DESCENDING POWERS [x^4, x^3, x^2, x^1, constant] - required
# by evaluate_polynomial()'s Horner loop. Do NOT reorder.

try:                                     # inside the plugin package
    from .damsuite import tm11_curves as _TM11
except ImportError:                      # build_nd_pack.py run as a script (2.17.0: was broken)
    from damsuite import tm11_curves as _TM11   # noqa


class TableCurve(object):
    """One boundary of the REG_05.2026 table: depth (ft) as a straight line
    between tabulated velocities (ft/s); evaluate_polynomial() and
    chartsign.curve_depth() read it like a coefficient list."""
    __slots__ = ("kind", "level", "v", "d")

    def __init__(self, kind, level):
        self.kind, self.level = kind, level
        self.v = tuple(_TM11.CURVES[kind]["v"])
        self.d = tuple(_TM11.CURVES[kind][level])

    def at(self, velocity_fts):
        return _TM11.depth_at(self.kind, self.level, velocity_fts)

    def __bool__(self):
        return True

    def __repr__(self):            # part of the evaluation key: changes with the table
        return "TableCurve({!r}, {!r}, sha256 {})".format(self.kind, self.level,
                                                         _TM11.table_sha256()[:16])


CURVE_TABLE_SOURCE = _TM11.SOURCE
CURVE_TABLE_SHA256 = _TM11.table_sha256()

QUARTIC_COEFFICIENTS_V2_10 = {
    # House on Foundation - Judgment/High boundary
    "found_high": [
        -1.41114244584413e-05, 0.001413860022071, -0.0377816543149834,
        0.0739429363600124, 5.83698548778349
    ],
    # House on Foundation - Low/Judgment boundary
    "found_low": [
        -2.0774376228281e-05, 0.00138141479635702, -0.0257374008010214,
        -0.0166664953919504, 3.01517816478201
    ],
    # Mobile Home - Judgment/High boundary
    # v1.3.1: x^4 term corrected from 9.65180652684472e-05 (transcription
    # error). With e-05 the threshold rose to 3.9 ft at 12 ft/s and 6.3 ft at
    # 15 ft/s; Figure 3 shows ~2.1 and ~1.9 ft. With e-06 the quartic matches
    # the traced Figure 3 line within 0.02 ft RMS (max 0.04 ft) over 0-16 ft/s.
    "mobile_high": [
        9.65180652684472e-06, -0.000116307303808405, -0.00255026223774735,
        -0.0058867521367748, 2.50067599067344
    ],
    # Mobile Home - Low/Judgment boundary
    "mobile_low": [
        1.05623543123767e-05, -7.91569541576109e-05, -0.00346445221443759,
        -0.00313616938623795, 1.9001165501147
    ],
}

HAZARD_COEFFICIENTS = {
    # Houses on foundations (ND Fig. 5.1) and mobile homes (ND Fig. 5.2):
    # the REG_05.2026 table (v2.11.0)
    "found_high": TableCurve("house", "high"),
    "found_low": TableCurve("house", "low"),
    "mobile_high": TableCurve("mobile", "high"),
    "mobile_low": TableCurve("mobile", "low"),
    # Passenger Vehicles (TM 11 Figure 4) - v1.3.1. Traced from a 400-dpi
    # render of the TM 11 scan (line centres of the drawn boundaries, frame
    # calibrated on the ft / ft/s axes and checked against the m/s ticks,
    # +/-0.06 ft/s), then least-squares quartic over 0.25-15.75 ft/s.
    # Fit residual: RMS 0.008 ft / max 0.023 ft (low), RMS 0.012 ft / max
    # 0.029 ft (high). Method check: the same procedure applied to Figure 2
    # reproduces the verified house curves within 0.045 ft (low) and 0.065 ft
    # (high) RMS; exactly 5.208 ft at V = 6 ft/s for the house high curve.
    # Passenger Vehicles - Judgment/High boundary (3.00 ft at 0 -> 1.58 ft at 16 ft/s)
    "vehicle_high": [
        -0.0001038517130001145, 0.0037321173178590287, -0.03976081868384347,
        0.017266319739380753, 3.000214298404674
    ],
    # Passenger Vehicles - Low/Judgment boundary (1.97 ft at 0 -> 0.98 ft at 16 ft/s)
    "vehicle_low": [
        -6.211008775385708e-05, 0.0022273997745073724, -0.02296755584903149,
        -0.010291770870531423, 1.9681625256188149
    ],
}

# Velocity range of each TM 11 figure (ft/s). Velocities above the range are
# evaluated at the range limit instead of extrapolating the quartic (beyond
# 16 ft/s the mobile-home quartic turns upward and the vehicle quartic
# plunges below zero).
CURVE_V_MAX = {"found": 25.0, "mobile": 16.0, "vehicle": 16.0}
CURVE_FIGURE = {"found": "ND Figure 5.1 / TM 11 Figure 2 (houses built on foundations)",
                "mobile": "ND Figure 5.2 / TM 11 Figure 3 (mobile homes)",
                "vehicle": "Figure 4 (passenger vehicles)"}

ZONE_LABELS = {1: "HIGH", 2: "JUDGMENT", 3: "LOW", -9999: "NO DATA"}

# ACER TM 11 Figure 4 - passenger vehicles (digitized in v1.3.1). If the
# keys are removed, road vehicle zones and motorists in the TM 11 count are
# skipped and the summary says so.
VEHICLE_CURVE_KEYS = ("vehicle_low", "vehicle_high")


def vehicle_curve_available():
    return all(HAZARD_COEFFICIENTS.get(k) for k in VEHICLE_CURVE_KEYS)


# ------------------------------------------------------------------------------
# USBR ACER TM 11 (1988) - lives in jeopardy (LIJ) and Table 1
# ------------------------------------------------------------------------------
TM11_REF = "USBR ACER Technical Memorandum No. 11 (1988)"
TM11_PERSONS_PER_DWELLING = 3      # "three residents per dwelling"
TM11_SIGNIFICANT_MAX_LIJ = 6       # Table 1: Low 0, Significant 1-6, High > 6

# ------------------------------------------------------------------------------
# North Dakota Dam Safety Standards Policy REG_05.2026 (effective 2026-04-01)
# Section 5.2 Failure Consequences
# ------------------------------------------------------------------------------
ND_POLICY_REF = ("North Dakota Dam Safety Standards Policy REG_05.2026 "
                 "(effective 04/01/2026), Section 5.2")
ND_1D_DEPTH_FT = 2.0               # 5.2.1.1: 1D model, depth > 2 ft -> high
ND_AADT_LOW_BELOW = 500            # 5.2.1.2: AADT < 500 -> no probable LOL
ND_AADT_HIGH_ABOVE = 2000          # 5.2.1.2: AADT > 2,000 -> probable LOL
ND_RAIL_TRAINS_HIGH = 10           # 5.2.1.3: freight >= 10 trains/day -> high

ND_HIGH, ND_MEDIUM, ND_LOW = "High", "Medium", "Low"
ND_RANK = {ND_LOW: 0, ND_MEDIUM: 1, ND_HIGH: 2}

# ------------------------------------------------------------------------------
# Structure / road / rail analysis defaults (all adjustable in the dialog)
# ------------------------------------------------------------------------------
DEFAULT_MIN_UNVERIFIED_AREA_FT2 = 400   # untagged outlines smaller = sheds/bins
DEFAULT_POINT_RADIUS_FT = 20.0          # footprint radius for point-only structures
STRUCTURE_MERGE_DIST_FT = 30.0          # same building reported by 2 sources
LINE_SAMPLE_STEP_FT = 10.0              # road/rail sampling interval
AADT_SEARCH_FT = 300.0                  # AADT station / segment match distance
CROSSING_AADT_SEARCH_FT = 100.0         # FRA crossing AADT match distance
RAIL_CROSSING_SEARCH_MI = 3.0           # crossings used for trains/day
DEFAULT_VEHICLE_OCCUPANCY = 1.5         # persons per vehicle (TM 11 motorists)
DEFAULT_VEHICLE_SPEED_MPH = 45.0
DEFAULT_NEAR_THRESHOLD_FT = 0.5         # "borderline": this close below a zone boundary
DEFAULT_INCREMENTAL_DEPTH_FT = 1.0      # breach adds at least this much depth
DEFAULT_ACCESS_SEARCH_FT = 500.0        # structure to its access road
DEFAULT_WARNING_ISSUED_HR = 0.0         # warning issued this long after breach start

# ------------------------------------------------------------------------------
# Online data sources (queried only for the inundated extent)
# ------------------------------------------------------------------------------
NSI_API_URL = "https://nsi.sec.usace.army.mil/nsiapi/structures"
NARN_URL = ("https://services.arcgis.com/xOi1kZaI0eWDREZv/arcgis/rest/services/"
            "NTAD_North_American_Rail_Network_Lines/FeatureServer/0")
CROSSINGS_URL = ("https://services.arcgis.com/xOi1kZaI0eWDREZv/arcgis/rest/services/"
                 "NTAD_Railroad_Grade_Crossings/FeatureServer/0")
# FEMA USA Structures (building outlines with occupancy class, nationwide)
USA_STRUCTURES_URL = ("https://services2.arcgis.com/FiaPA4ga0iQKduv3/arcgis/rest/services/"
                      "USA_Structures_View/FeatureServer/0")
DOWNLOAD_TILE_DEG = 0.2
ARCGIS_PAGE_SIZE = 2000             # = maxRecordCount of the NTAD / ND GIS Hub services
ARCGIS_MAX_PAGES = 200
# v2.6: downloads run side by side - services, ArcGIS pages and NSI tiles.
# At most this many HTTP requests are in flight at once (Overpass: 2).
DOWNLOAD_WORKERS = 6
OVERPASS_WORKERS = 2
DOWNLOAD_FOLDER_NAME = "_downloads"
# ND GIS Hub "NDGISHUB SiteStructure Address Points" (NENA NG9-1-1 site /
# structure address points, statewide, public).
ND_ADDRESS_POINTS_URL = ("https://services1.arcgis.com/GOcSXpzwBHyk2nog/arcgis/rest/services/"
                         "NDGISHUBSiteStructureAddressPoints/FeatureServer/0")
ND_ADDRESS_POINTS_ITEM = ("https://gishubdata-ndgov.hub.arcgis.com/datasets/"
                          "NDGOV::ndgishub-sitestructure-address-points")
# ------------------------------------------------------------------------------
# v2.2: other dams (dams in series, ND 5.3(c)) and lifeline facilities (5.2.2)
# ------------------------------------------------------------------------------
# USACE National Inventory of Dams (public FeatureServer; NIDID, NAME,
# HAZARD_POTENTIAL, NID_HEIGHT, NID_STORAGE, RIVER_OR_STREAM ...) and the
# Esri Federal User Community mirror with the same schema (fallback).
NID_URL = ("https://geospatial.sec.usace.army.mil/dls/rest/services/NID/"
           "National_Inventory_of_Dams_Public_Service/FeatureServer/0")
NID_MIRROR_URL = ("https://services2.arcgis.com/FiaPA4ga0iQKduv3/arcgis/rest/services/"
                  "NID_v1/FeatureServer/0")
DAM_SEARCH_FT = 500.0         # a dam counts as flooded when depth > 0 within this of its point
LIFELINE_POINT_RADIUS_FT = 50.0   # facility point: depth > 0 within this distance
# Lifeline / critical facilities. HIFLD Open closed on 26 Aug 2025 and the NASA NCCS
# copy used by 2.8 no longer answers, so the HIFLD entries below have NO download
# address (v2.9). Download the HIFLD layers from the Homeland Security Digital Library
# (https://www.hsdl.org/hifld/), put the files in a folder and pick that folder on the
# Data & Output tab ("Search a folder for data sets"): the plugin recognises them by
# name and by their fields. An address written into the data-source file for one of
# these keys (LIFELINE_SOURCES.hifld_fire ...) is downloaded again as before.
# The ND GIS Hub serves hospitals and long-term care facilities.
# (key, label, url, category)
HIFLD_PORTAL_URL = "https://www.hsdl.org/hifld/"
_NDHUB = "https://ndgishub.nd.gov/arcgis/rest/services/"
LIFELINE_SOURCES = [
    ("nd_hospitals", "ND GIS Hub hospitals", _NDHUB + "All_EmergencyServices/MapServer/2",
     "Hospital"),
    ("nd_skilled_nursing", "ND GIS Hub skilled nursing facilities",
     _NDHUB + "All_PublicHealth/MapServer/2", "Nursing home"),
    ("nd_basic_care", "ND GIS Hub basic care facilities",
     _NDHUB + "All_PublicHealth/MapServer/1", "Basic care / assisted living"),
    ("nd_assisted_living", "ND GIS Hub assisted living facilities",
     _NDHUB + "All_PublicHealth/MapServer/0", "Basic care / assisted living"),
    ("hifld_fire", "HIFLD fire stations (from hsdl.org)", "", "Fire station"),
    ("hifld_ems", "HIFLD EMS stations (from hsdl.org)", "", "EMS station"),
    ("hifld_eoc", "HIFLD emergency operations centers (from hsdl.org)", "",
     "Emergency operations center"),
    ("hifld_police", "HIFLD law enforcement (from hsdl.org)", "", "Law enforcement"),
    ("hifld_wastewater", "HIFLD wastewater treatment plants (from hsdl.org)", "",
     "Wastewater treatment"),
    ("eia_power_plants", "EIA power plants",
     "https://services2.arcgis.com/FiaPA4ga0iQKduv3/arcgis/rest/services/"
     "Power_Plants_in_the_US/FeatureServer/0", "Power plant"),
]


def lifeline_downloads():
    """LIFELINE_SOURCES entries that have a download address (the HIFLD entries
    have none unless the data-source file gives one)."""
    return [t for t in LIFELINE_SOURCES if t[2]]


# OpenStreetMap (Overpass API) fills the gaps: substations and drinking-water
# plants have no agency source; the rest complements the agency layers.
OVERPASS_URL = "https://overpass-api.de/api/interpreter"
OSM_LIFELINE_TAGS = [
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
    # v2.5: vulnerable occupants - listed for review and flagged as priority
    # notifications in the EAP list; they do not change the class on their own
    ("amenity", "school", "School"),
    ("amenity", "kindergarten", "Child care"),
    ("amenity", "childcare", "Child care"),
]
# Categories that are lifelines under ND 5.2.2 (hospital / emergency response
# / utilities). Care facilities are listed for review (vulnerable occupants)
# but do not change the class on their own.
LIFELINE_CATEGORIES = {"Hospital", "Fire station", "EMS station", "Emergency operations center",
                       "Law enforcement", "Power plant", "Electric substation",
                       "Water treatment", "Wastewater treatment", "Lifeline (user layer)"}
FACILITY_DEDUP_FT = 300.0     # same category this close = the same facility
HTTP_USER_AGENT = "ND-Dam-Hazard-Helper-QGIS-plugin/" + PLUGIN_VERSION

# ------------------------------------------------------------------------------
# v2.2: model-extent check and EAP outputs
# ------------------------------------------------------------------------------
BOUNDARY_MAX_POINTS = 5000    # wet boundary cells written to Model_Boundary_Check
EAP_ARRIVAL_BREAKS_HR = (0.5, 1.0, 2.0, 4.0, 8.0, 24.0)   # arrival bands / contours

# ------------------------------------------------------------------------------
# v2.3: roads / traffic, bridges, culverts and crossings
# ------------------------------------------------------------------------------
# ND GIS Hub NG9-1-1 road centerlines (every public road statewide: FULL_NAME,
# RoadClass, FUNC_SYS, OWNER, SHIELD ...)
ND_ROADS_URL = ("https://services1.arcgis.com/GOcSXpzwBHyk2nog/arcgis/rest/services/"
                "NDGISHUBRoadsNG911/FeatureServer/0")
# Traffic (AADT): FHWA HPMS for North Dakota (aadt, data_year, routename ...),
# newest year first; ND GIS Hub county roads also carry AADT / AADT_YR.
HPMS_ND_URLS = ["https://geo.dot.gov/server/rest/services/Hosted/HPMS_FULL_ND_{}/"
                "FeatureServer/0".format(y) for y in (2024, 2023, 2022, 2020)]
ND_COUNTY_ROADS_URL = ("https://services1.arcgis.com/GOcSXpzwBHyk2nog/arcgis/rest/services/"
                       "NDGISHUB_County_Roads/FeatureServer/0")
# v2.4.0: NDDOT Traffic Counts MapServer (ArcGIS REST; layer 0 = AADT). v2.5:
# tried only when no HPMS year answers. The address could not be checked
# when this was written - override it in the data-source file if it moves.
NDDOT_AADT_URL = ("https://gis.dot.nd.gov/ArcGIS/rest/services/external/"
                  "Public_TrafficCounts/MapServer")
NDDOT_AADT_LAYERS = {"aadt": 0}
# National Bridge Inventory (NTAD): bridges and culverts (STRUCTURE_TYPE_043B
# = 19) with a span >= 20 ft; ADT_029, FACILITY_CARRIED_007, FEATURES_DESC_006A
NBI_URL = ("https://services.arcgis.com/xOi1kZaI0eWDREZv/arcgis/rest/services/"
           "NTAD_National_Bridge_Inventory/FeatureServer/0")
# OpenStreetMap stream crossings: culverts (smaller than NBI), fords / low-
# water crossings, railway bridges. (element filter, category)
OSM_CROSSING_QUERIES = [
    ('way["tunnel"="culvert"]', "Culvert"),
    ('nwr["ford"="yes"]', "Low-water crossing / ford"),
    ('way["bridge"="low_water_crossing"]', "Low-water crossing / ford"),
    ('way["railway"]["bridge"]', "Railroad bridge"),
]
CROSSING_RADIUS_FT = 50.0     # a crossing counts when depth > 0 within this distance

# ------------------------------------------------------------------------------
# v2.5: campgrounds, towns, channel centerline, map book, future development
# ------------------------------------------------------------------------------
# OpenStreetMap campgrounds and RV parks: overnight recreation is habitable
# under ND REG_05.2026 5.2.1.1. (tag, value, label)
OSM_CAMP_TAGS = [("tourism", "camp_site", "Campground"),
                 ("tourism", "caravan_site", "RV park")]
CAMP_POINT_RADIUS_FT = 150.0  # campground mapped as a point: this circle is evaluated
# OpenStreetMap populated places for the travel-time table; radius = the
# distance from the place point within which its first flooded structure counts
OSM_PLACE_RADIUS_FT = {"city": 10560.0, "town": 5280.0, "village": 2640.0,
                       "hamlet": 1320.0}
# USGS flowlines for the channel centerline of the travel-time table when none is given.
# 1.19.0: the USGS 3D Hydrography Program (3DHP) service replaces the National Hydrography
# Dataset service (the constant keeps its old name: a data-source file may change it, and the
# older NHD service stays in the Suite data and servers list). Each downloaded feature also gets
# the NHD properties gnis_name, ftype ... (damsuite.hydro3dhp).
NHD_FLOWLINE_URL = ("https://hydro.nationalmap.gov/arcgis/rest/services/3DHP_all/MapServer/50")
# 2.17.0: waterbodies - ponds and reservoirs for the suite's dam data and the offline pack (the
# Breach Runner's PONDS input, Screening's pool area); 1.19.0: 3DHP waterbodies
NHD_WATERBODY_URL = ("https://hydro.nationalmap.gov/arcgis/rest/services/3DHP_all/MapServer/60")
# 1.19.0: the ND DWR dam inventory as a public service (the pack's DWR_dams when no DWR export
# file is at hand; the export has a few more fields)
DWR_DAMS_URL = ("https://ndgishub.nd.gov/arcgis/rest/services/Applications/DWR_Dams/"
                "MapServer/0")
# 1.19.0: Southeast Aquatic Resources Partnership (SARP) aquatic barrier services - the data
# behind tool.aquaticbarriers.org (Great Plains / Intermountain West region): dams and the
# surveyed road-stream crossings
SARP_DAMS_URL = ("https://services.arcgis.com/QVENGdaPbd4LUkLV/ArcGIS/rest/services/"
                 "Aquatic_Barrier_Prioritization_Dams_v360/FeatureServer/0")
SARP_CROSSINGS_URL = ("https://services.arcgis.com/QVENGdaPbd4LUkLV/ArcGIS/rest/services/"
                      "Aquatic_Barrier_Prioritization_RoadBarriers_v360/FeatureServer/0")
TRAVEL_STATION_MI = 0.5       # travel-time table: a station every ... miles
TRAVEL_SEARCH_FT = 150.0      # station value: deepest / earliest cell within this
TRAVEL_MAX_MI = 200.0         # traced channel is cut here
FLOWLINE_SNAP_FT = 3.0        # NHD line ends closer than this are connected
# EAP map book: (label, scale denominator) - 1 in = ... ft on a Letter sheet
MAPBOOK_SCALES = [("1 in = 500 ft (1:6,000)", 6000), ("1 in = 1,000 ft (1:12,000)", 12000),
                  ("1 in = 2,000 ft (1:24,000)", 24000), ("1 in = 1 mile (1:63,360)", 63360)]
MAPBOOK_DEFAULT_SCALE = 12000
MAPBOOK_MAX_SHEETS = 80       # more sheets than this: the next smaller scale is used
MAPBOOK_TABLE_ROWS = 18       # notification items listed on a sheet
# Future development (5.3(b)): vacant parcels up to this size in the danger zone
DEFAULT_PARCEL_MAX_ACRES = 5.0
# Aerial imagery base map (Review tab, when zooming to a structure): an XYZ tile
# service, {z}/{y}/{x} in the address. USGS National Map imagery is public domain;
# another service (Esri World Imagery, a state service) can be set in the data-source file.
AERIAL_IMAGERY_URL = ("https://basemap.nationalmap.gov/arcgis/rest/services/USGSImageryOnly/"
                      "MapServer/tile/{z}/{y}/{x}")
AERIAL_IMAGERY_NAME = "Aerial imagery (base map)"
AERIAL_IMAGERY_CREDIT = "Imagery: USGS National Map (or the service set in the data-source file)"
AERIAL_MAX_ZOOM = 19
# Review tab "Open web map" link: {lat} and {lon} are replaced by the structure's
# position. Change it in the data-source file to use another web map.
WEBMAP_URL_TEMPLATE = "https://www.google.com/maps/search/?api=1&query={lat}%2C{lon}"

# ------------------------------------------------------------------------------
# Data-source overrides: every service address above can be changed without a
# new plugin version, in <QGIS profile>/ND_Dam_Hazard_Helper_data_sources.json
# (Readme tab: "Edit data sources"). Read when the plugin loads.
# ------------------------------------------------------------------------------
SOURCE_KEYS = ("NSI_API_URL", "NARN_URL", "CROSSINGS_URL", "USA_STRUCTURES_URL",
               "ND_ADDRESS_POINTS_URL", "NID_URL", "NID_MIRROR_URL", "OVERPASS_URL",
               "ND_ROADS_URL", "HPMS_ND_URLS", "ND_COUNTY_ROADS_URL", "NBI_URL",
               "NDDOT_AADT_URL", "NHD_FLOWLINE_URL", "NHD_WATERBODY_URL",
               "SARP_DAMS_URL", "SARP_CROSSINGS_URL", "DWR_DAMS_URL",
               "AERIAL_IMAGERY_URL", "WEBMAP_URL_TEMPLATE")
SOURCES_FILE_NAME = "ND_Dam_Hazard_Helper_data_sources.json"
SOURCE_OVERRIDES = {}          # what the file changed (shown in the run log)


def sources_file_path():
    """The user's data-source file (it may not exist)."""
    import os
    env = os.environ.get("ND_DAM_HAZARD_SOURCES")
    if env:
        return env
    try:
        from qgis.core import QgsApplication
        base = QgsApplication.qgisSettingsDirPath()
    except Exception:
        base = ""
    if not base:
        base = os.path.join(os.path.expanduser("~"), ".nd_dam_hazard_helper")
    return os.path.join(base, SOURCES_FILE_NAME)


def current_sources():
    """{key: value} of every data-source address, lifelines by key."""
    g = globals()
    out = {k: g[k] for k in SOURCE_KEYS}
    out["LIFELINE_SOURCES"] = {k: u for k, _l, u, _c in LIFELINE_SOURCES}
    return out


_SOURCE_DEFAULTS = {}          # 2.17.0: the built-in addresses, before any override


def _restore_source_defaults():
    """Put the built-in addresses back (captured on the first call), so applying the data-source
    file and the suite's server choices again starts from the built-ins."""
    g = globals()
    if not _SOURCE_DEFAULTS:
        for k in SOURCE_KEYS:
            _SOURCE_DEFAULTS[k] = list(g[k]) if isinstance(g[k], list) else g[k]
        _SOURCE_DEFAULTS["LIFELINE_SOURCES"] = list(LIFELINE_SOURCES)
        return
    for k in SOURCE_KEYS:
        v = _SOURCE_DEFAULTS[k]
        g[k] = list(v) if isinstance(v, list) else v
    LIFELINE_SOURCES[:] = _SOURCE_DEFAULTS["LIFELINE_SOURCES"]


def apply_suite_services(catalog=None):
    """2.17.0: the servers chosen in Plugins > ND Dam Suite > Suite data and servers (damsuite
    services.json) replace the matching addresses - only where a server other than the built-in
    default was chosen. Applied after the data-source file, so the newer choice wins. Returns
    {key: address} it changed (also in SOURCE_OVERRIDES as "suite:" + key)."""
    try:
        try:
            from .damsuite import services as _SV
        except ImportError:                  # build_nd_pack.py run as a script
            from damsuite import services as _SV    # noqa
        cat = catalog if catalog is not None else _SV.load()
        changes = _SV.hazard_overrides(cat)
    except Exception as e:                   # noqa - a broken services.json never stops the plugin
        SOURCE_OVERRIDES["_suite_error"] = str(e)
        return {}
    g = globals()
    done = {}
    for k, v in changes.items():
        if k in SOURCE_KEYS and isinstance(g[k], str) and isinstance(v, str) and v != g[k]:
            g[k] = v
            done[k] = v
            SOURCE_OVERRIDES["suite:" + k] = v
    return done


def apply_source_overrides(path=None):
    """Read the data-source file and replace the addresses it names. Unknown
    keys and values of the wrong type are ignored (and reported)."""
    import json
    import os
    g = globals()
    path = path or sources_file_path()
    _restore_source_defaults()        # 2.17.0: an address removed from the file goes back
    SOURCE_OVERRIDES.clear()
    if not path or not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8-sig") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as e:
        SOURCE_OVERRIDES["_error"] = "{}: {}".format(path, e)
        return SOURCE_OVERRIDES
    if not isinstance(data, dict):
        SOURCE_OVERRIDES["_error"] = "{}: not a JSON object".format(path)
        return SOURCE_OVERRIDES
    for k, v in data.items():
        if k in SOURCE_KEYS:
            if isinstance(g[k], list) and isinstance(v, list) and all(
                    isinstance(x, str) for x in v):
                if v != g[k]:
                    g[k] = list(v)
                    SOURCE_OVERRIDES[k] = v
            elif isinstance(g[k], str) and isinstance(v, str) and v.startswith("http"):
                if v != g[k]:
                    g[k] = v
                    SOURCE_OVERRIDES[k] = v
        elif k == "LIFELINE_SOURCES" and isinstance(v, dict):
            for i, (key, label, url, cat) in enumerate(LIFELINE_SOURCES):
                nv = v.get(key)
                if isinstance(nv, str) and nv.startswith("http") and nv != url:
                    LIFELINE_SOURCES[i] = (key, label, nv, cat)
                    SOURCE_OVERRIDES["LIFELINE_SOURCES." + key] = nv
    return SOURCE_OVERRIDES


def write_sources_file(path=None):
    """Write the current addresses to the data-source file (created with the
    folder when missing; an existing file is left as it is). Returns path."""
    import json
    import os
    path = path or sources_file_path()
    if not os.path.isfile(path):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        data = {"_about": "ND Dam Hazard Classification Helper - data-source addresses. "
                          "Change a URL and restart QGIS (or reload the plugin). Delete "
                          "a line to use the plugin's built-in address again."}
        data.update(current_sources())
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
    return path


apply_source_overrides()
apply_suite_services()

# An address point that is not placed on a structure (NENA Placement =
# "Property Access", "Parcel", "Geocoding", ...) is moved onto the nearest
# building outline within this distance that has no address point yet.
ADDRESS_SNAP_FT = 100.0


_FT_PER_METER = 3.28083989501312
_METERS_PER_DEGREE_EQUATOR = 111320.0


def _read_readme():
    """README.txt next to this file (shown on the Readme tab)."""
    import os
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "README.txt")
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return "README.txt is missing from the plugin folder."


README_TEXT = _read_readme()
