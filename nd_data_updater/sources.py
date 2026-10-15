"""Dataset definitions for North Dakota."""

from .core import GitHubRepoSource, HttpFileSource, NsiApiSource, OsmLayersSource
from .packsource import PACK_FOLDER, PackSource

MICROSOFT_URL = ("https://minedbuildings.z5.web.core.windows.net/legacy/"
                 "usbuildings-v2/NorthDakota.geojson.zip")
GEOFABRIK_URL = "https://download.geofabrik.de/north-america/us/north-dakota-latest-free.gpkg.zip"
# NSI generator source code is pinned to one reviewed commit instead of following `main`
# (a branch can be force-pushed under us). To move to a newer commit, review it and update this.
# NSI_GENERATOR_ZIP_SHA256 is left None: GitHub may recompress archive zips, so the hash is logged
# on every fetch (visible if it changes) but not enforced; set it to enforce a byte-for-byte match.
NSI_GENERATOR_COMMIT = "87f768783302e51852e38bb622ebd35fe17eee59"   # USACE-NSI/nsi-generator main @ 2026-10-14
NSI_GENERATOR_ZIP_SHA256 = None

# id -> (display name, description, enabled by default)
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
            "verified against its published MD5 checksum.", True),
    "osm_layers": ("OpenStreetMap pack layers (Geofabrik)",
                   "Builds the dam suite's five OpenStreetMap layers (lifelines, culverts and "
                   "fords, campgrounds, mobile-home parks, towns) from Geofabrik's full "
                   "North Dakota extract (.osm.pbf, about 200 MB). Needs a few minutes of "
                   "processing after the download.", True),
    "usa_structures": ("USA Structures (FEMA/ORNL)",
                       "Needs a download link: paste the North Dakota zip link from "
                       "FEMA's USA Structures page.", False),
    "pack": ("Offline pack for the ND dam plugins (all layers)",
             "Builds and keeps up to date the statewide offline data pack the ND Dam Hazard Helper, "
             "Breach Runner and Breach Screening read: structures, roads, rail, lifelines, dams, "
             "USGS 3DHP flowlines and waterbodies, SARP aquatic barrier dams and surveyed "
             "crossings, DWR and HIFLD layers (from files in the data folder's dwr/ and hifld/), "
             "NLCD land cover and counties. A layer that is missing or older than the refresh "
             "interval is fetched; the rest is left alone.", True),
    "nsi_generator": ("nsi-generator source code",
                      "Latest code on the main branch of USACE-NSI/nsi-generator.", False),
}


def build_sources(config, merger=None):
    """config keys: enabled (dict id->bool), usa_structures_url, nsi_interval_days,
    nsi_mode ("merged", "nsi2026", "nsi2022"), nsi_match_m, and for the offline pack: data_dir,
    pack_days, pack_3dhp ("auto" | "file" | "service"), pack_3dhp_minutes (the cap on deciding
    that in mode "auto"), pack_share, pack_full, pack_box, and the one-shot run choices
    pack_only_layers (fetch these layers again) and pack_missing_only (build only what is
    missing)."""
    enabled = config.get("enabled", {})

    def on(source_id):
        return enabled.get(source_id, SOURCE_INFO[source_id][2])

    sources = []
    if on("nsi"):
        sources.append(NsiApiSource(
            "nsi", SOURCE_INFO["nsi"][0], "nsi", merger=merger,
            check_interval_hours=24 * int(config.get("nsi_interval_days", 30)),
            description=SOURCE_INFO["nsi"][1], mode=config.get("nsi_mode") or "merged",
            match_m=config.get("nsi_match_m")))
    if on("microsoft"):
        sources.append(HttpFileSource(
            "microsoft", SOURCE_INFO["microsoft"][0], MICROSOFT_URL,
            "NorthDakota.geojson.zip", "microsoft_footprints", version_mode="headers",
            layer_globs=("*.geojson",), description=SOURCE_INFO["microsoft"][1]))
    if on("osm"):
        sources.append(HttpFileSource(
            "osm", SOURCE_INFO["osm"][0], GEOFABRIK_URL,
            "north-dakota-latest-free.gpkg.zip", "osm", version_mode="md5",
            layer_globs=("**/*.gpkg",), layer_filter=("buildings",),
            description=SOURCE_INFO["osm"][1]))
    if on("osm_layers"):
        sources.append(OsmLayersSource(
            "osm_layers", SOURCE_INFO["osm_layers"][0], "osm_layers",
            description=SOURCE_INFO["osm_layers"][1]))
    usa_url = (config.get("usa_structures_url") or "").strip()
    if on("usa_structures") and usa_url:
        filename = usa_url.split("?")[0].rstrip("/").rsplit("/", 1)[-1] or "usa_structures.zip"
        sources.append(HttpFileSource(
            "usa_structures", SOURCE_INFO["usa_structures"][0], usa_url, filename,
            "usa_structures", version_mode="headers",
            extract=filename.lower().endswith(".zip"),
            layer_globs=("**/*.gdb", "**/*.shp", "**/*.gpkg"),
            description=SOURCE_INFO["usa_structures"][1]))
    if on("pack"):
        sources.append(PackSource(
            "pack", SOURCE_INFO["pack"][0], PACK_FOLDER, config.get("data_dir") or "",
            refresh_days=config.get("pack_days", 30), mode_3dhp=config.get("pack_3dhp", "auto"),
            share=config.get("pack_share", True), full=config.get("pack_full", False),
            description=SOURCE_INFO["pack"][1], box=config.get("pack_box"),
            minutes_3dhp=config.get("pack_3dhp_minutes"),
            only_layers=config.get("pack_only_layers"),
            missing_only=config.get("pack_missing_only", False)))
    if on("nsi_generator"):
        sources.append(GitHubRepoSource(
            "nsi_generator", SOURCE_INFO["nsi_generator"][0], "USACE-NSI", "nsi-generator",
            "main", "nsi_generator_code", description=SOURCE_INFO["nsi_generator"][1],
            commit=NSI_GENERATOR_COMMIT, zip_sha256=NSI_GENERATOR_ZIP_SHA256))
    return sources
