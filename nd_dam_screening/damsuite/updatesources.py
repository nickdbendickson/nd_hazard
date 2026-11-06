"""The data sources both data updaters offer (damsuite.updatesources, suite 1.18.8).

ONE list for the ND Data Updater (nd_hazard) and DamSafety Data (DWR_Plugins): before 1.18.8 the
ND Updater had the OpenStreetMap pack layers (Geofabrik .osm.pbf) and checked the OSM GeoPackage
against Geofabrik's MD5, DamSafety Data had Overture buildings and the ND 911 address points.
Both now offer every one of them, with the same on / off defaults. Each plugin's sources.py adds
its own offline pack source (the `pack` argument) and is otherwise this module.
"""

from .updatecore import (ArcGISFeatureSource, GitHubRepoSource, HttpFileSource, NsiApiSource,
                         OsmLayersSource, OvertureSource)

MICROSOFT_URL = ("https://minedbuildings.z5.web.core.windows.net/legacy/"
                 "usbuildings-v2/NorthDakota.geojson.zip")
GEOFABRIK_URL = "https://download.geofabrik.de/north-america/us/north-dakota-latest-free.gpkg.zip"
ADDRESS_POINTS_URL = ("https://services1.arcgis.com/GOcSXpzwBHyk2nog/ArcGIS/rest/services/"
                      "NDGISHUBSiteStructureAddressPoints/FeatureServer/0")
ADDRESS_POINTS_FIELDS = ("OBJECTID,ADDRESS,Add_Number,St_Name,St_PosTyp,County,Lat,Long,"
                         "Site_NGUID,Placement,Post_Code,Post_Comm")
# NSI generator source code is pinned to one reviewed commit instead of following `main`
# (a branch can be force-pushed under us). To move to a newer commit, review it and update this.
# NSI_GENERATOR_ZIP_SHA256 is left None: GitHub may recompress archive zips, so the hash is logged
# on every fetch (visible if it changes) but not enforced; set it to enforce a byte-for-byte match.
NSI_GENERATOR_COMMIT = "87f768783302e51852e38bb622ebd35fe17eee59"   # USACE-NSI/nsi-generator main @ 2026-10-14
NSI_GENERATOR_ZIP_SHA256 = None

# id -> (display name, description, enabled by default); the order is the settings window's
SOURCE_INFO = {
    "nsi": ("National Structure Inventory (NSI)",
            "The USACE structure inventory for all 53 North Dakota counties, from the NSI "
            "API. By default NSI 2026 and NSI 2022 are merged into one layer: the 2026 point "
            "wins where both versions have a structure, and 2022-only structures are kept "
            "and flagged. Checked at most every N days (set below).", True),
    "microsoft": ("Microsoft building footprints",
                  "Statewide NorthDakota.geojson (about 568,000 footprints). "
                  "Microsoft no longer updates this legacy release.", True),
    "osm": ("OpenStreetMap (Geofabrik)",
            "Statewide OpenStreetMap GeoPackage, rebuilt daily by Geofabrik and "
            "verified against its published MD5 checksum (or the server's ETag / "
            "Last-Modified when no checksum is published).", True),
    "osm_layers": ("OpenStreetMap pack layers (Geofabrik)",
                   "Builds the dam suite's five OpenStreetMap layers (lifelines, culverts and "
                   "fords, campgrounds, mobile-home parks, towns) from Geofabrik's full "
                   "North Dakota extract (.osm.pbf, about 200 MB). Needs a few minutes of "
                   "processing after the download. The offline pack takes these layers from "
                   "the osm_layers folder.", True),
    "usa_structures": ("USA Structures (FEMA/ORNL)",
                       "Needs a download link: paste the North Dakota zip link from "
                       "FEMA's USA Structures page.", False),
    "overture": ("Overture Maps Buildings",
                 "Conflated building footprints (OSM + Microsoft + others) with height and type. "
                 "Downloaded for the North Dakota bounding box. Needs the overturemaps command "
                 "(pip install overturemaps). Layer name: overture_buildings.", False),
    "address_points": ("ND 911 Site/Structure Address Points",
                       "NENA SiteStructure Address Points from the ND GIS Hub "
                       "(438k+ points). Layer name: address_points. "
                       "Used by Hazard for household verification when no NSI/outline is present.",
                       True),
    "nsi_generator": ("nsi-generator source code",
                      "Latest code on the main branch of USACE-NSI/nsi-generator.", False),
}


def source_info(pack_info=None):
    """SOURCE_INFO, with the plugin's offline pack source (name, description, default) placed
    before nsi-generator when given."""
    if not pack_info:
        return dict(SOURCE_INFO)
    out = {}
    for sid, info in SOURCE_INFO.items():
        if sid == "nsi_generator":
            out["pack"] = tuple(pack_info)
        out[sid] = info
    return out


def build_sources(config, merger=None, pack=None, info=None):
    """config keys: enabled (dict id->bool), usa_structures_url, nsi_interval_days,
    nsi_mode ("merged", "nsi2026", "nsi2022"), nsi_match_m. pack: the plugin's
    pack(config, info) -> Source (or None), added in SOURCE_INFO order. info: the plugin's
    SOURCE_INFO (source_info(...); default this module's)."""
    info = info or SOURCE_INFO
    enabled = config.get("enabled", {})

    def on(source_id):
        return source_id in info and enabled.get(source_id, info[source_id][2])

    sources = []
    if on("nsi"):
        sources.append(NsiApiSource(
            "nsi", info["nsi"][0], "nsi", merger=merger,
            check_interval_hours=24 * int(config.get("nsi_interval_days", 30)),
            description=info["nsi"][1], mode=config.get("nsi_mode") or "merged",
            match_m=config.get("nsi_match_m")))
    if on("microsoft"):
        sources.append(HttpFileSource(
            "microsoft", info["microsoft"][0], MICROSOFT_URL,
            "NorthDakota.geojson.zip", "microsoft_footprints", version_mode="headers",
            layer_globs=("*.geojson",), description=info["microsoft"][1]))
    if on("osm"):
        sources.append(HttpFileSource(
            "osm", info["osm"][0], GEOFABRIK_URL,
            "north-dakota-latest-free.gpkg.zip", "osm", version_mode="md5",
            layer_globs=("**/*.gpkg",), layer_filter=("buildings",),
            description=info["osm"][1]))
    if on("osm_layers"):
        sources.append(OsmLayersSource(
            "osm_layers", info["osm_layers"][0], "osm_layers",
            description=info["osm_layers"][1]))
    usa_url = (config.get("usa_structures_url") or "").strip()
    if on("usa_structures") and usa_url:
        filename = usa_url.split("?")[0].rstrip("/").rsplit("/", 1)[-1] or "usa_structures.zip"
        sources.append(HttpFileSource(
            "usa_structures", info["usa_structures"][0], usa_url, filename,
            "usa_structures", version_mode="headers",
            extract=filename.lower().endswith(".zip"),
            layer_globs=("**/*.gdb", "**/*.shp", "**/*.gpkg"),
            description=info["usa_structures"][1]))
    if on("overture"):
        sources.append(OvertureSource(
            "overture", info["overture"][0], "overture_buildings",
            description=info["overture"][1]))
    if on("address_points"):
        sources.append(ArcGISFeatureSource(
            "address_points", info["address_points"][0], ADDRESS_POINTS_URL, "address_points",
            where="1=1", out_fields=ADDRESS_POINTS_FIELDS,
            description=info["address_points"][1], layer_name="address_points"))
    if pack is not None and on("pack"):
        src = pack(config, info)
        if src is not None:
            sources.append(src)
    if on("nsi_generator"):
        sources.append(GitHubRepoSource(
            "nsi_generator", info["nsi_generator"][0], "USACE-NSI", "nsi-generator",
            "main", "nsi_generator_code", description=info["nsi_generator"][1],
            commit=NSI_GENERATOR_COMMIT, zip_sha256=NSI_GENERATOR_ZIP_SHA256))
    return sources
