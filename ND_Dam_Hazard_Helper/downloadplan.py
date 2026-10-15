# -*- coding: utf-8 -*-
"""2.32.1: the list of data sets a classification run downloads - one rule for
the run (pipeline._collect_inputs) and the pre-run estimate (estimate.py).

Until 2.32.0 the estimate kept its own copy of the pipeline's "what to
download" rules and the two had drifted apart (the estimate counted FEMA USA
Structures when the outline layer already is that file, and mobile-home parks
and places that the data folder supplies). Pure Python (no QGIS): the caller
works out the two facts that need QGIS - whether ND GIS Hub address points
are already at hand and whether the building-outline layer already is the
FEMA USA Structures file.
"""

from . import constants as C

# where a data set is fetched (the caller maps these to its lon / lat boxes)
AREA_STUDY = "study"          # the study area (raster extent + buffer)
AREA_CROSSINGS = "crossings"  # the grade-crossing search area (study area + RAIL_CROSSING_SEARCH_MI)
AREA_PLACES = "places"        # the study area + 2 miles (towns for the travel-time table)

KIND_NSI = "nsi"
KIND_ARCGIS = "arcgis"
KIND_OVERPASS = "overpass"


def _on(cfg, key, default=True):
    v = cfg.get(key, default)
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)


def _layer_given(cfg, key):
    return cfg.get(key) is not None


def download_plan(cfg, local_roles=(), have_hub=False, outlines_are_usa=False):
    """[(key, name, label, kind, url, area)] in the order the run fetches them.

    key: the run's job key (pipeline._collect_inputs reads its results by it);
    name: the download / offline-pack / tile-cache name of the data set;
    label: the text of the run log and of the estimate; kind: KIND_*; url: the
    service (None for the dedicated fetchers whose service is their own);
    area: AREA_*.
    cfg: the pipeline cfg (download_* switches, *_layer choices, eap,
    eap_travel, termination); local_roles: the data-folder roles the run
    opens (datafolder.existing_roles / run.local_roles); have_hub: ND GIS Hub
    address points are already among the point sources; outlines_are_usa:
    the building-outline layer is the FEMA USA Structures file."""
    roles = set(local_roles or ())
    jobs = []

    def add(key, name, label, kind, url, area=AREA_STUDY):
        jobs.append((key, name, label, kind, url, area))
    if _on(cfg, "download_nsi") and "nsi" not in roles:
        add("nsi", "NSI_structures", "NSI", KIND_NSI, C.NSI_API_URL)
    if not have_hub and "address" not in roles and _on(cfg, "download_address"):
        add("address", "NDGISHUB_address_points", "ND GIS Hub address points", KIND_ARCGIS,
            C.ND_ADDRESS_POINTS_URL)
    if _on(cfg, "download_usa_structures") and "usa" not in roles and not outlines_are_usa:
        add("usa", "USA_Structures", "FEMA USA Structures", KIND_ARCGIS, C.USA_STRUCTURES_URL)
    if not _layer_given(cfg, "rail_layer") and "rail" not in roles and \
            _on(cfg, "download_rail"):
        add("rail", "NARN_rail_lines", "Rail lines", KIND_ARCGIS, C.NARN_URL)
    if not _layer_given(cfg, "crossing_layer") and "crossings" not in roles and \
            _on(cfg, "download_crossings"):
        add("cross", "FRA_grade_crossings", "Grade crossings", KIND_ARCGIS, C.CROSSINGS_URL,
            AREA_CROSSINGS)
    if not _layer_given(cfg, "road_layer") and "roads" not in roles and \
            _on(cfg, "download_roads"):
        add("road", "ND_roads_NG911", "ND GIS Hub roads", KIND_ARCGIS, C.ND_ROADS_URL)
        if not _layer_given(cfg, "aadt_layer") and "aadt" not in roles:
            for url in C.HPMS_ND_URLS:
                year = "".join(ch for ch in url.split("HPMS_FULL_ND_")[-1][:4] if ch.isdigit())
                add("hpms_" + year, "HPMS_ND_{}".format(year), "HPMS {}".format(year),
                    KIND_ARCGIS, url)
            if C.NDDOT_AADT_URL:
                add("nddot", "NDDOT_traffic_counts", "NDDOT traffic counts", KIND_ARCGIS,
                    "{}/{}".format(C.NDDOT_AADT_URL.rstrip("/"),
                                   C.NDDOT_AADT_LAYERS.get("aadt", 0)))
            add("county", "ND_county_roads", "ND county roads (AADT)", KIND_ARCGIS,
                C.ND_COUNTY_ROADS_URL)
    if _on(cfg, "download_dams") and "dams" not in roles:
        add("nid", "NID_dams", "National Inventory of Dams", KIND_ARCGIS, C.NID_URL)
    if _on(cfg, "download_lifelines"):
        for key, label, url, _cat in C.lifeline_downloads():
            add("life_" + key, "lifeline_" + key, label, KIND_ARCGIS, url)
        add("life_osm", "OSM_lifelines", "OpenStreetMap lifelines", KIND_OVERPASS,
            C.OVERPASS_URL)
    if _on(cfg, "download_crossings_structures") and "bridges" not in roles:
        add("nbi", "NBI_bridges", "National Bridge Inventory", KIND_ARCGIS, C.NBI_URL)
        add("xosm", "OSM_crossings", "OpenStreetMap crossings", KIND_OVERPASS, C.OVERPASS_URL)
    if _on(cfg, "download_camps") and "camps" not in roles:
        add("camps", "OSM_campgrounds", "OpenStreetMap campgrounds", KIND_OVERPASS,
            C.OVERPASS_URL)
    if _on(cfg, "download_trailer_parks") and "parks" not in roles:
        add("parks", "OSM_trailer_parks", "OpenStreetMap mobile-home parks", KIND_OVERPASS,
            C.OVERPASS_URL)
    # pipeline._eap_on(cfg, "travel"): EAP outputs on AND the travel-time table on
    travel = _on(cfg, "eap") and _on(cfg, "eap_travel")
    if travel and _on(cfg, "download_places") and "places" not in roles:
        add("places", "OSM_places", "OpenStreetMap places", KIND_OVERPASS, C.OVERPASS_URL,
            AREA_PLACES)
    # v2.8: the termination point needs the channel too, with or without the travel table
    if (travel or _on(cfg, "termination", False)) and \
            not _layer_given(cfg, "centerline_layer") and "centerline" not in roles and \
            _on(cfg, "download_flowlines"):
        add("flow", "3DHP_flowlines", "USGS 3DHP flowlines", KIND_ARCGIS, C.NHD_FLOWLINE_URL)
    return jobs
