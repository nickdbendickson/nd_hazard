"""Dataset definitions for North Dakota."""

from .core import GitHubRepoSource, HttpFileSource, NsiApiSource

MICROSOFT_URL = ("https://minedbuildings.z5.web.core.windows.net/legacy/"
                 "usbuildings-v2/NorthDakota.geojson.zip")
GEOFABRIK_URL = "https://download.geofabrik.de/north-america/us/north-dakota-latest-free.gpkg.zip"

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
    "usa_structures": ("USA Structures (FEMA/ORNL)",
                       "Needs a download link: paste the North Dakota zip link from "
                       "FEMA's USA Structures page.", False),
    "nsi_generator": ("nsi-generator source code",
                      "Latest code on the main branch of USACE-NSI/nsi-generator.", False),
}


def build_sources(config, merger=None):
    """config keys: enabled (dict id->bool), usa_structures_url, nsi_interval_days,
    nsi_mode ("merged", "nsi2026", "nsi2022"), nsi_match_m."""
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
    usa_url = (config.get("usa_structures_url") or "").strip()
    if on("usa_structures") and usa_url:
        filename = usa_url.split("?")[0].rstrip("/").rsplit("/", 1)[-1] or "usa_structures.zip"
        sources.append(HttpFileSource(
            "usa_structures", SOURCE_INFO["usa_structures"][0], usa_url, filename,
            "usa_structures", version_mode="headers",
            extract=filename.lower().endswith(".zip"),
            layer_globs=("**/*.gdb", "**/*.shp", "**/*.gpkg"),
            description=SOURCE_INFO["usa_structures"][1]))
    if on("nsi_generator"):
        sources.append(GitHubRepoSource(
            "nsi_generator", SOURCE_INFO["nsi_generator"][0], "USACE-NSI", "nsi-generator",
            "main", "nsi_generator_code", description=SOURCE_INFO["nsi_generator"][1]))
    return sources
