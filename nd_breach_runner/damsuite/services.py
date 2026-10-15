"""The suite's web map servers (1.5.0): one list of WMS / WMTS / WCS / ArcGIS REST services for the
three plugins, the user's own servers, and which service each data role uses.

A *role* is a kind of data the suite needs (ROLES): "dem", "structures", "waterbodies" ... A
*service* is one entry of the catalog:

    {"id", "role", "name", "kind", "url", "layer", "fields", "source", "confidence", "notes",
     "builtin"}

    kind   arcgis_feature  an ArcGIS FeatureServer / MapServer LAYER (url ends in /<layer id>);
                           features are downloaded with its /query endpoint
           arcgis_map      an ArcGIS MapServer (display; with a layer id in `layer` it is also a
                           feature source)
           arcgis_image    an ArcGIS ImageServer (rasters with /exportImage; display)
           wms / wmts      OGC map images (display only; `layer` = the layer name)
           wcs             OGC coverage (raster data; `layer` = the coverage id)
           xyz             a tile template with {z} {x} {y} (display only)
           file           a file or folder on this computer (`url` = its path)
    confidence  "documented" (the address was seen in the provider's own documentation or
                service directory) or "inferred" (pieced together; check it with Test)

Built-in services (BUILTIN) are the addresses the plugins already used plus the ones found for
1.5.0. NONE of them could be tried from where this module was written: the plugins' "Test"
button asks the server itself (capabilities_url / parse_*), and a layer id or name is looked up
in the server's own listing rather than trusted.

The user's list lives in `services.json` (SERVICES_FILE) in the suite data folder (suitedata),
or in ~/.ndsuite/ when no data folder is set:

    {"format": "ndsuite/services", "format_version": 1,
     "servers": [service, ...],          user-added services (builtin False)
     "choice":  {role: service id},      the data source picked for a role
     "hidden":  [service id, ...]}       built-in services the user hid

Decisions:
  * `chosen(role)` is the user's pick when it still exists, else the first built-in data-capable
    service of the role (BUILTIN order = preference), else None.
  * An added service needs an http(s) URL (or an existing path for kind "file"); `detect_kind`
    guesses the kind from the address and the caller may override it.
  * A user service may not reuse a built-in id; ids are "user_<n>".
  * Nothing here opens a network connection by itself: the parse_* helpers take the server's
    answer as text, and `fetch_features` / `test_service` take an `http_get(url) -> bytes`
    callable (the plugins pass one that goes through QGIS's network settings; urllib is the
    fallback outside QGIS).
Pure Python 3.9+ standard library.
"""
import json
import os
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from . import netproxy

FORMAT = "ndsuite/services"
FORMAT_VERSION = 1
SERVICES_FILE = "services.json"
USER_AGENT = "ND-Dam-Suite/1.5 (QGIS plugins; dam safety)"

KINDS = ("arcgis_feature", "arcgis_map", "arcgis_image", "wms", "wmts", "wcs", "xyz", "file")
VECTOR_KINDS = ("arcgis_feature", "arcgis_map", "file")
RASTER_KINDS = ("arcgis_image", "wcs", "file")
DISPLAY_KINDS = ("arcgis_map", "arcgis_image", "arcgis_feature", "wms", "wmts", "xyz", "wcs")

# role -> (label, data type, used by)   data type: vector / raster / display
ROLES = {
    "dem": ("Terrain (DEM)", "raster", "Screening, Breach Runner"),
    "nlcd": ("Land cover (NLCD, Manning's n)", "raster", "Screening, Breach Runner"),
    "structures": ("Building outlines", "vector", "all three"),
    "address_points": ("Address points", "vector", "Hazard"),
    "roads": ("Roads", "vector", "all three"),
    "rail": ("Railroads", "vector", "all three"),
    "crossings": ("Rail grade crossings", "vector", "Hazard"),
    "bridges": ("Bridges and culverts", "vector", "Hazard, Breach Runner"),
    "nid_dams": ("National Inventory of Dams", "vector", "all three"),
    "dwr_dams": ("ND DWR dams inventory", "vector", "Screening"),
    "dwr_dam_lines": ("ND DWR dam lines", "vector", "Screening"),
    "flowlines": ("Flowlines (streams; 3DHP, was NHD)", "vector", "all three"),
    "waterbodies": ("Waterbodies (3DHP, was NHD)", "vector", "Screening, Breach Runner"),
    "barrier_dams": ("Aquatic barrier dams (SARP)", "vector", "offline pack"),
    "barrier_crossings": ("Aquatic barrier road-stream crossings (SARP, surveyed)", "vector",
                          "offline pack"),
    "flood_zones": ("FEMA flood zones", "vector", "reference"),
    "counties": ("County boundaries", "vector", "reference"),
    "imagery": ("Aerial imagery", "display", "map display"),
    "topo": ("Topographic base map", "display", "map display"),
    "reference": ("Other reference map", "display", "map display"),
}

_NDHUB = "https://ndgishub.nd.gov/arcgis/rest/services/"
_TNM = "https://hydro.nationalmap.gov/arcgis/"
_BASE = "https://basemap.nationalmap.gov/arcgis/"
_SARP = "https://services.arcgis.com/QVENGdaPbd4LUkLV/ArcGIS/rest/services/"


def _s(id, role, name, kind, url, layer="", fields=(), source="", confidence="documented",
       notes="", scale_axes=None):
    d = {"id": id, "role": role, "name": name, "kind": kind, "url": url, "layer": layer,
         "fields": list(fields), "source": source, "confidence": confidence, "notes": notes,
         "builtin": True}
    if scale_axes:                       # WCS grid axis names for scaleSize (1.19.0)
        d["scale_axes"] = list(scale_axes)
    return d


BUILTIN = [
    # --- terrain
    _s("usgs_3dep_image", "dem", "USGS 3DEP elevation (ImageServer)", "arcgis_image",
       "https://elevation.nationalmap.gov/arcgis/rest/services/3DEPElevation/ImageServer",
       source="ND Dam Breach Screening terrain.py", notes="metres; exportImage in EPSG:5070"),
    _s("usgs_3dep_wcs", "dem", "USGS 3DEP elevation (WCS)", "wcs",
       "https://elevation.nationalmap.gov/arcgis/services/3DEPElevation/ImageServer/WCSServer",
       layer="DEP3Elevation", confidence="inferred",
       source="https://elevation.nationalmap.gov/arcgis/services/3DEPElevation/ImageServer/"
              "WCSServer?request=GetCapabilities&service=WCS"),
    _s("usgs_3dep_wms", "reference", "USGS 3DEP hillshade (WMS)", "wms",
       "https://elevation.nationalmap.gov/arcgis/services/3DEPElevation/ImageServer/WMSServer",
       layer="3DEPElevation:Hillshade Gray", confidence="inferred",
       source="https://elevation.nationalmap.gov/arcgis/services/3DEPElevation/ImageServer/"
              "WMSServer?request=GetCapabilities&service=WMS"),
    # --- land cover
    _s("mrlc_nlcd2021_wcs", "nlcd", "NLCD 2021 land cover (MRLC WCS)", "wcs",
       "https://www.mrlc.gov/geoserver/mrlc_download/wcs",
       layer="mrlc_download__NLCD_2021_Land_Cover_L48", confidence="documented",
       source="https://www.mrlc.gov/data-services-page", notes="WCS 2.0.1, EPSG:5070, 30 m; "
       "tried 2026-10-12: the grid axes are i / j", scale_axes=("i", "j")),
    _s("esri_nlcd_annual", "nlcd", "Annual NLCD land cover (Esri ImageServer)", "arcgis_image",
       "https://di-nlcd.img.arcgis.com/arcgis/rest/services/USA_NLCD_Annual_LandCover/ImageServer",
       source="https://www.arcgis.com/home/item.html?id=12bb5ad7b1d145648d1ec8a68ee5c8c4",
       notes="hosted by Esri; public access not confirmed"),
    _s("mrlc_nlcd2021_wms", "reference", "NLCD 2021 land cover (MRLC WMS)", "wms",
       "https://www.mrlc.gov/geoserver/mrlc_display/NLCD_2021_Land_Cover_L48/wms",
       layer="NLCD_2021_Land_Cover_L48", source="https://www.mrlc.gov/data-services-page"),
    # --- structures, roads, rail (the addresses ND Dam Hazard downloads from)
    _s("fema_usa_structures", "structures", "FEMA USA Structures", "arcgis_feature",
       "https://services2.arcgis.com/FiaPA4ga0iQKduv3/arcgis/rest/services/USA_Structures_View/"
       "FeatureServer/0", fields=("OCC_CLS", "PRIM_OCC", "HEIGHT", "SQMETERS"),
       source="ND Dam Hazard constants.USA_STRUCTURES_URL"),
    _s("ndhub_address_points", "address_points", "ND GIS Hub site / structure address points",
       "arcgis_feature",
       "https://services1.arcgis.com/GOcSXpzwBHyk2nog/arcgis/rest/services/"
       "NDGISHUBSiteStructureAddressPoints/FeatureServer/0",
       source="ND Dam Hazard constants.ND_ADDRESS_POINTS_URL"),
    _s("ndhub_roads_ng911", "roads", "ND GIS Hub roads (NG911)", "arcgis_feature",
       "https://services1.arcgis.com/GOcSXpzwBHyk2nog/arcgis/rest/services/NDGISHUBRoadsNG911/"
       "FeatureServer/0", source="ND Dam Hazard constants.ND_ROADS_URL"),
    _s("ndhub_county_roads", "roads", "ND GIS Hub county roads", "arcgis_feature",
       "https://services1.arcgis.com/GOcSXpzwBHyk2nog/arcgis/rest/services/"
       "NDGISHUB_County_Roads/FeatureServer/0", source="ND Dam Hazard constants.ND_COUNTY_ROADS_URL"),
    _s("tiger_primary_roads", "roads", "Census TIGERweb primary roads", "arcgis_feature",
       "https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/Transportation/MapServer/2",
       source="https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/Transportation/MapServer"),
    _s("tiger_secondary_roads", "roads", "Census TIGERweb secondary roads", "arcgis_feature",
       "https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/Transportation/MapServer/6",
       source="https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/Transportation/MapServer"),
    _s("tiger_local_roads", "roads", "Census TIGERweb local roads", "arcgis_feature",
       "https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/Transportation/MapServer/8",
       source="https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/Transportation/MapServer"),
    _s("ntad_rail_lines", "rail", "NTAD North American Rail Network", "arcgis_feature",
       "https://services.arcgis.com/xOi1kZaI0eWDREZv/arcgis/rest/services/"
       "NTAD_North_American_Rail_Network_Lines/FeatureServer/0",
       source="ND Dam Hazard constants.NARN_URL"),
    _s("ntad_crossings", "crossings", "NTAD railroad grade crossings", "arcgis_feature",
       "https://services.arcgis.com/xOi1kZaI0eWDREZv/arcgis/rest/services/"
       "NTAD_Railroad_Grade_Crossings/FeatureServer/0",
       source="ND Dam Hazard constants.CROSSINGS_URL"),
    _s("ntad_nbi", "bridges", "NTAD National Bridge Inventory", "arcgis_feature",
       "https://services.arcgis.com/xOi1kZaI0eWDREZv/arcgis/rest/services/"
       "NTAD_National_Bridge_Inventory/FeatureServer/0", source="ND Dam Hazard constants.NBI_URL"),
    # --- dams
    _s("usace_nid", "nid_dams", "USACE National Inventory of Dams", "arcgis_feature",
       "https://geospatial.sec.usace.army.mil/dls/rest/services/NID/"
       "National_Inventory_of_Dams_Public_Service/FeatureServer/0",
       fields=("NIDID", "NAME", "HAZARD_POTENTIAL", "NID_HEIGHT", "NID_STORAGE"),
       source="ND Dam Hazard constants.NID_URL"),
    _s("nid_mirror", "nid_dams", "National Inventory of Dams (ArcGIS Online copy)",
       "arcgis_feature",
       "https://services2.arcgis.com/FiaPA4ga0iQKduv3/arcgis/rest/services/NID_v1/FeatureServer/0",
       source="ND Dam Hazard constants.NID_MIRROR_URL"),
    _s("ndhub_hsip_dams", "dwr_dams", "ND GIS Hub HSIP dams", "arcgis_feature",
       _NDHUB + "Applications/DES_HSIP_Unclass/MapServer/7", confidence="inferred",
       source="https://ndgishub.nd.gov/arcgis/rest/services/Applications/DES_HSIP_Unclass/MapServer",
       notes="not the DWR dam safety inventory: use your DWR export (file) when you have one"),
    _s("dwr_mapservice_wms", "reference", "ND DWR MapService (WMS)", "wms",
       "https://mapservice.dwr.nd.gov/swcwms", confidence="inferred",
       source="https://www.swc.nd.gov/info_edu/map_data_resources/mapservices.html",
       notes="layer names: use Test / Browse layers"),
    # --- hydrography (1.19.0: the USGS 3D Hydrography Program is the default; NHD stays listed)
    _s("usgs_3dhp_flowline", "flowlines", "USGS 3DHP flowlines", "arcgis_feature",
       _TNM + "rest/services/3DHP_all/MapServer/50",
       fields=("gnisidlabel", "featuretypelabel", "lengthkm", "streamorder"),
       source="ND Dam Hazard constants.NHD_FLOWLINE_URL",
       notes="NHD-style gnis_name / ftype are added to each feature when it is downloaded"),
    _s("usgs_3dhp_waterbody", "waterbodies", "USGS 3DHP waterbodies", "arcgis_feature",
       _TNM + "rest/services/3DHP_all/MapServer/60",
       fields=("gnisidlabel", "featuretypelabel", "areasqkm"),
       source="ND Dam Hazard constants.NHD_WATERBODY_URL",
       notes="NHD-style gnis_name / ftype are added to each feature when it is downloaded"),
    _s("usgs_nhd_flowline", "flowlines", "USGS NHD flowlines (large scale, older)",
       "arcgis_feature",
       _TNM + "rest/services/nhd/MapServer/6", fields=("gnis_name", "ftype", "fcode"),
       source="https://hydro.nationalmap.gov/arcgis/rest/services/nhd/MapServer"),
    _s("usgs_nhdplushr_flowline", "flowlines", "USGS NHDPlus HR network flowlines",
       "arcgis_feature", _TNM + "rest/services/NHDPlus_HR/MapServer/3",
       source="https://hydro.nationalmap.gov/arcgis/rest/services/NHDPlus_HR/MapServer"),
    _s("usgs_nhd_waterbody", "waterbodies", "USGS NHD waterbodies (large scale, older)",
       "arcgis_feature", _TNM + "rest/services/nhd/MapServer/12",
       fields=("gnis_name", "ftype", "fcode", "areasqkm"),
       source="https://hydro.nationalmap.gov/arcgis/rest/services/nhd/MapServer"),
    _s("usgs_nhdplushr_waterbody", "waterbodies", "USGS NHDPlus HR waterbodies",
       "arcgis_feature", _TNM + "rest/services/NHDPlus_HR/MapServer/9",
       source="https://hydro.nationalmap.gov/arcgis/rest/services/NHDPlus_HR/MapServer"),
    # --- aquatic barriers (Southeast Aquatic Resources Partnership; the data behind
    # tool.aquaticbarriers.org, Great Plains / Intermountain West region)
    _s("sarp_barrier_dams", "barrier_dams", "SARP aquatic barrier prioritisation - dams",
       "arcgis_feature", _SARP + "Aquatic_Barrier_Prioritization_Dams_v360/FeatureServer/0",
       fields=("Name", "River", "YearCompleted", "Height", "Hazard", "Passability"),
       source="https://aquaticbarriers.org/", confidence="inferred",
       notes="~7,000 dams in North Dakota; includes the National Inventory of Dams and state "
             "records"),
    _s("sarp_barrier_crossings", "barrier_crossings",
       "SARP aquatic barrier prioritisation - surveyed road-stream crossings", "arcgis_feature",
       _SARP + "Aquatic_Barrier_Prioritization_RoadBarriers_v360/FeatureServer/0",
       fields=("Name", "River", "Road", "CrossingType", "BarrierSeverity"),
       source="https://aquaticbarriers.org/", confidence="inferred",
       notes="field-surveyed crossings only; unsurveyed (modelled) crossings are not in this "
             "service"),
    _s("ndhub_hydro_wms", "reference", "ND GIS Hub hydrography (WMS)", "wms",
       "https://ndgishub.nd.gov/arcgis/services/All_Hydrography/MapServer/WMSServer",
       source="https://ndgishub.nd.gov/arcgis/rest/services/All_Hydrography/MapServer",
       notes="layer names: use Test / Browse layers"),
    # --- reference
    _s("fema_nfhl_zones", "flood_zones", "FEMA NFHL flood hazard zones", "arcgis_feature",
       "https://hazards.fema.gov/arcgis/rest/services/public/NFHL/MapServer/28",
       fields=("FLD_ZONE", "ZONE_SUBTY", "SFHA_TF"),
       source="https://hazards.fema.gov/arcgis/rest/services/public/NFHL/MapServer"),
    _s("fema_nfhl_wms", "reference", "FEMA NFHL (WMS)", "wms",
       "https://hazards.fema.gov/arcgis/services/public/NFHLWMS/MapServer/WMSServer",
       layer="28", confidence="inferred",
       source="https://hazards.fema.gov/gis/nfhl/services/public/NFHLWMS/MapServer/WMSServer"),
    _s("ndhub_counties", "counties", "ND GIS Hub counties", "arcgis_feature",
       _NDHUB + "All_GovtBoundaries/MapServer/20", confidence="inferred",
       source="https://ndgishub.nd.gov/arcgis/rest/services/All_GovtBoundaries/MapServer"),
    _s("ndhub_elevation", "reference", "ND GIS Hub elevation (LiDAR index, hillshade)",
       "arcgis_map", _NDHUB + "All_Elevation/MapServer",
       source="https://ndgishub.nd.gov/arcgis/rest/services/All_Elevation/MapServer"),
    # --- map display
    _s("usgs_imagery_xyz", "imagery", "USGS imagery (tiles)", "xyz",
       _BASE + "rest/services/USGSImageryOnly/MapServer/tile/{z}/{y}/{x}",
       source="ND Dam Hazard constants.AERIAL_IMAGERY_URL"),
    _s("usgs_imagery_wms", "imagery", "USGS imagery (WMS)", "wms",
       _BASE + "services/USGSImageryOnly/MapServer/WMSServer", layer="0",
       source=_BASE + "services/USGSImageryOnly/MapServer/WMSServer?request=GetCapabilities"
                      "&service=WMS"),
    _s("usda_naip_wms", "imagery", "USDA NAIP imagery (WMS)", "wms",
       "https://gis.apfo.usda.gov/arcgis/services/NAIP/USDA_CONUS_PRIME/ImageServer/WMSServer",
       layer="0", source="https://gis.apfo.usda.gov/arcgis/rest/services/NAIP/USDA_CONUS_PRIME/"
                         "ImageServer"),
    _s("ndhub_aerial_2021", "imagery", "ND GIS Hub aerial imagery 2021", "arcgis_image",
       _NDHUB + "Imagery/AerialImage_ND_2021/ImageServer",
       source="https://ndgishub.nd.gov/arcgis/rest/services/Imagery"),
    _s("dwr_aerial_wms", "imagery", "ND DWR aerial photography (WMS)", "wms",
       "https://aerial.dwr.nd.gov/dwrwms",
       source="https://www.swc.nd.gov/info_edu/map_data_resources/mapservices.html",
       notes="layer names: use Test / Browse layers"),
    _s("usgs_topo_wms", "topo", "USGS topo (WMS)", "wms",
       _BASE + "services/USGSTopo/MapServer/WMSServer", layer="0",
       source=_BASE + "services/USGSTopo/MapServer/WMSServer?request=GetCapabilities&service=WMS"),
    _s("usgs_topo_xyz", "topo", "USGS topo (tiles)", "xyz",
       _BASE + "rest/services/USGSTopo/MapServer/tile/{z}/{y}/{x}",
       source=_BASE + "rest/services/USGSTopo/MapServer"),
]
_BUILTIN_IDS = {s["id"] for s in BUILTIN}


class ServiceError(Exception):
    """A service that cannot be added, read or used; the message says what to do."""


# ------------------------------------------------------------------ addresses --
_ARC = re.compile(r"/rest/services/(?P<path>.+?)/(?P<type>MapServer|FeatureServer|ImageServer)"
                  r"(?:/(?P<layer>\d+))?/?(?:\?.*)?$", re.I)


def detect_kind(url):
    """Best guess of a service kind from its address ("" when unknown)."""
    u = (url or "").strip()
    if not u:
        return ""
    if not re.match(r"^https?://", u, re.I):
        return "file" if os.path.exists(u) else ""
    low = u.lower()
    if "{z}" in low and ("{x}" in low or "{y}" in low):
        return "xyz"
    m = _ARC.search(u.split("?")[0])
    if m:
        t = m.group("type").lower()
        if t == "imageserver":
            return "arcgis_image"
        if m.group("layer") is not None or t == "featureserver":
            return "arcgis_feature"
        return "arcgis_map"
    q = urllib.parse.parse_qs(urllib.parse.urlparse(u).query)
    svc = (q.get("service") or q.get("SERVICE") or [""])[0].lower()
    if svc in ("wms", "wmts", "wcs"):
        return svc
    if "wmts" in low:
        return "wmts"
    if "wcs" in low:
        return "wcs"
    if "wms" in low:
        return "wms"
    return ""


def split_arcgis(url):
    """(service url, layer id or None) of an ArcGIS REST address."""
    base = (url or "").split("?")[0].rstrip("/")
    m = _ARC.search(base)
    if not m:
        return base, None
    lay = m.group("layer")
    if lay is not None:
        base = base[: base.rfind("/")]
    return base, (int(lay) if lay is not None else None)


def _with_query(url, params):
    base, _, q = (url or "").partition("?")
    have = urllib.parse.parse_qs(q)
    keep = {k: v[0] for k, v in have.items() if k.lower() not in {p.lower() for p in params}}
    keep.update(params)
    return base + "?" + urllib.parse.urlencode(keep)


def capabilities_url(service):
    """The address that lists what a service offers (its layers / coverages)."""
    kind, url = service.get("kind"), service.get("url", "")
    if kind in ("arcgis_feature", "arcgis_map", "arcgis_image"):
        base, lay = split_arcgis(url)
        if kind == "arcgis_feature" and lay is not None:
            return base + "/{}?f=json".format(lay)
        return base + "?f=json"
    if kind == "wms":
        return _with_query(url, {"service": "WMS", "request": "GetCapabilities"})
    if kind == "wcs":
        return _with_query(url, {"service": "WCS", "request": "GetCapabilities"})
    if kind == "wmts":
        if url.lower().endswith(".xml"):
            return url
        return _with_query(url, {"service": "WMTS", "request": "GetCapabilities"})
    return ""


def data_capable(service, role=None):
    """Can the service give the role's DATA (features / raster values), not just a picture?"""
    role = role or service.get("role")
    rtype = ROLES.get(role, ("", "display", ""))[1]
    kind = service.get("kind")
    if rtype == "vector":
        if kind == "arcgis_feature":
            return True
        if kind == "arcgis_map":
            return split_arcgis(service.get("url"))[1] is not None or \
                str(service.get("layer", "")).strip().isdigit()
        return kind == "file"
    if rtype == "raster":
        return kind in RASTER_KINDS
    return False


def layer_url(service):
    """The ArcGIS layer address of a feature source (MapServer + layer id joined)."""
    base, lay = split_arcgis(service.get("url"))
    if lay is None and str(service.get("layer", "")).strip().isdigit():
        lay = int(service["layer"])
    if lay is None:
        raise ServiceError("{}: no layer id - pick a layer with Browse layers".format(
            service.get("name") or service.get("url")))
    return "{}/{}".format(base, lay)


def qgis_layer_source(service):
    """(QGIS provider key, data source uri, layer name) to add the service to a map."""
    kind, url, name = service.get("kind"), service.get("url", ""), service.get("name", "")
    layer = str(service.get("layer") or "")
    if kind == "xyz":
        return "wms", "type=xyz&zmin=0&zmax=19&url=" + urllib.parse.quote(url, safe=":/{}"), name
    if kind == "wms":
        if not layer:
            raise ServiceError("{}: choose a layer first (Browse layers)".format(name or url))
        uri = "contextualWMSLegend=0&crs=EPSG:3857&dpiMode=7&format=image/png&layers={}&styles=" \
              "&url={}".format(urllib.parse.quote(layer), url.split("?")[0])
        return "wms", uri, name
    if kind == "wmts":
        if not layer:
            raise ServiceError("{}: choose a layer first (Browse layers)".format(name or url))
        uri = "contextualWMSLegend=0&crs=EPSG:3857&dpiMode=7&format=image/png&layers={}" \
              "&styles=default&tileMatrixSet=default028mm&url={}".format(
                  urllib.parse.quote(layer), url)
        return "wms", uri, name
    if kind == "wcs":
        if not layer:
            raise ServiceError("{}: choose a coverage first (Browse layers)".format(name or url))
        return "wcs", "identifier={}&url={}".format(layer, url.split("?")[0]), name
    has_layer = split_arcgis(url)[1] is not None or layer.strip().isdigit()
    if kind == "arcgis_feature" or (kind == "arcgis_map" and has_layer):
        return "arcgisfeatureserver", "url=" + layer_url(service), name
    if kind in ("arcgis_map", "arcgis_image"):
        # an ImageServer is read by the map-server provider (QGIS >= 3.22); its WMS face
        # (.../ImageServer/WMSServer) works in any QGIS
        return "arcgismapserver", "url=" + split_arcgis(url)[0], name
    if kind == "file":
        return ("gdal" if os.path.splitext(url)[1].lower() in (".tif", ".tiff", ".vrt", ".img")
                else "ogr"), url, name or os.path.basename(url)
    raise ServiceError("unknown service kind {!r}".format(kind))


# -------------------------------------------------------------- server answers --
def parse_arcgis_service(text):
    """An ArcGIS REST service / layer ?f=json answer -> {"type", "name", "layers": [{id, name,
    geometry, kind}], "fields": [names], "geometry", "error"}."""
    try:
        d = json.loads(text if isinstance(text, str) else text.decode("utf-8", "replace"))
    except ValueError:
        raise ServiceError("the server did not answer with ArcGIS JSON (is it an ArcGIS REST "
                           "address?)")
    if not isinstance(d, dict):
        raise ServiceError("unexpected ArcGIS answer")
    if "error" in d:
        e = d.get("error") or {}
        raise ServiceError("the server answered: {} {}".format(
            e.get("code", ""), e.get("message", "")).strip())
    out = {"type": d.get("type") or ("ImageServer" if "pixelType" in d else ""),
           "name": d.get("name") or d.get("mapName") or d.get("serviceDescription", "")[:60],
           "layers": [], "fields": [f.get("name") for f in d.get("fields") or []
                                    if isinstance(f, dict) and f.get("name")],
           "geometry": d.get("geometryType", "")}
    for lay in (d.get("layers") or []) + (d.get("tables") or []):
        if isinstance(lay, dict) and "id" in lay:
            out["layers"].append({"id": lay["id"], "name": lay.get("name", ""),
                                  "geometry": lay.get("geometryType", ""),
                                  "kind": lay.get("type", "")})
    return out


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _child_text(el, name):
    for c in el:
        if _local(c.tag) == name:
            return (c.text or "").strip()
    return ""


def parse_wms_capabilities(text):
    """WMS GetCapabilities XML -> [{"name", "title", "crs": [...]}] of the named layers."""
    try:
        root = ET.fromstring(text if isinstance(text, (bytes, str)) else str(text))
    except ET.ParseError as e:
        raise ServiceError("the server did not answer with WMS capabilities ({})".format(e))
    if _local(root.tag) in ("ServiceExceptionReport", "ExceptionReport"):
        raise ServiceError("the server answered: " + " ".join(
            (t.text or "").strip() for t in root.iter()).strip()[:300])
    out = []

    def walk(el, crs):
        here = list(crs) + [(c.text or "").strip() for c in el
                            if _local(c.tag) in ("CRS", "SRS") and c.text]
        name = _child_text(el, "Name")
        if name:
            out.append({"name": name, "title": _child_text(el, "Title") or name,
                        "crs": sorted(set(here))})
        for c in el:
            if _local(c.tag) == "Layer":
                walk(c, here)

    for el in root.iter():
        if _local(el.tag) == "Capability":
            for c in el:
                if _local(c.tag) == "Layer":
                    walk(c, [])
            break
    return out


def parse_wcs_capabilities(text):
    """WCS (1.x / 2.0) GetCapabilities XML -> [{"name", "title"}] of the coverages."""
    try:
        root = ET.fromstring(text)
    except ET.ParseError as e:
        raise ServiceError("the server did not answer with WCS capabilities ({})".format(e))
    out = []
    for el in root.iter():
        t = _local(el.tag)
        if t in ("CoverageSummary", "CoverageOfferingBrief"):
            name = _child_text(el, "CoverageId") or _child_text(el, "Identifier") or \
                _child_text(el, "name")
            if name:
                out.append({"name": name, "title": _child_text(el, "Title") or
                            _child_text(el, "label") or name})
    return out


def parse_wmts_capabilities(text):
    """WMTS capabilities XML -> [{"name", "title"}] of the layers."""
    try:
        root = ET.fromstring(text)
    except ET.ParseError as e:
        raise ServiceError("the server did not answer with WMTS capabilities ({})".format(e))
    out = []
    for el in root.iter():
        if _local(el.tag) == "Layer":
            name = _child_text(el, "Identifier")
            if name:
                out.append({"name": name, "title": _child_text(el, "Title") or name})
    return out


def list_layers(service, text):
    """The layers a server offers, from its capabilities answer: [{"name", "title", ...}]
    where "name" is what goes into the service's `layer` (an ArcGIS layer id as text)."""
    kind = service.get("kind")
    if kind in ("arcgis_feature", "arcgis_map", "arcgis_image"):
        info = parse_arcgis_service(text)
        if info["layers"]:
            return [{"name": str(l["id"]), "title": "{} - {}".format(l["id"], l["name"]),
                     "geometry": l["geometry"]} for l in info["layers"]]
        return [{"name": "", "title": info["name"] or "(the service itself)",
                 "geometry": info["geometry"], "fields": info["fields"]}]
    if kind == "wms":
        return parse_wms_capabilities(text)
    if kind == "wcs":
        return parse_wcs_capabilities(text)
    if kind == "wmts":
        return parse_wmts_capabilities(text)
    return []


def urllib_get(url, timeout=60):
    """http_get without QGIS's network client (QGIS's proxy setting still applies, 1.16.2)."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with netproxy.urlopen(req, timeout=timeout) as r:
        return r.read()


def test_service(service, http_get=None):
    """(ok, message, layers) - asks the server for its capabilities and checks that the chosen
    layer / coverage exists and that the expected fields are there. Never raises."""
    http_get = http_get or urllib_get
    kind = service.get("kind")
    if kind == "file":
        p = service.get("url", "")
        return (os.path.exists(p), "found" if os.path.exists(p) else "not found: " + p, [])
    if kind == "xyz":
        url = service.get("url", "").replace("{z}", "5").replace("{x}", "7").replace("{y}", "11")
        try:
            data = http_get(url)
        except Exception as e:                                   # noqa
            return False, "no tile: {}".format(e), []
        ok = data[:4] in (b"\x89PNG", b"\xff\xd8\xff\xe0", b"\xff\xd8\xff\xe1") or \
            data[:3] == b"\xff\xd8\xff"
        return ok, ("a tile came back" if ok else "the answer is not an image"), []
    cap = capabilities_url(service)
    if not cap:
        return False, "no capabilities address for kind {!r}".format(kind), []
    try:
        text = http_get(cap)
    except Exception as e:                                       # noqa
        return False, "the server did not answer: {}".format(e), []
    try:
        layers = list_layers(service, text)
    except ServiceError as e:
        return False, str(e), []
    except Exception as e:                                       # noqa
        return False, "could not read the answer: {}".format(e), []
    want = str(service.get("layer") or "")
    if kind == "arcgis_map":
        _b, lid = split_arcgis(service.get("url"))
        if lid is not None:
            want = str(lid)
    if want and kind in ("wms", "wcs", "wmts", "arcgis_map") and \
            want not in [l["name"] for l in layers]:
        return False, "layer {!r} is not offered (pick one with Browse layers)".format(want), layers
    if kind == "arcgis_feature" and service.get("fields"):
        have = {f.lower() for l in layers for f in l.get("fields", [])}
        miss = [f for f in service["fields"] if f.lower() not in have] if have else []
        if miss:
            return False, "the layer answers but lacks the fields {}".format(
                ", ".join(miss)), layers
    return True, "the server answers ({} layer(s))".format(len(layers)) if layers else \
        "the server answers", layers


# ------------------------------------------------------------- feature download --
def query_url(layer, box_lonlat, offset=0, count=1000, where="1=1", fields="*"):
    """An ArcGIS /query address for the features touching a lon / lat box (GeoJSON, WGS 84)."""
    xmin, ymin, xmax, ymax = box_lonlat
    p = {"where": where, "outFields": fields, "returnGeometry": "true", "f": "geojson",
         "geometry": "{},{},{},{}".format(xmin, ymin, xmax, ymax),
         "geometryType": "esriGeometryEnvelope", "inSR": "4326", "outSR": "4326",
         "spatialRel": "esriSpatialRelIntersects", "resultOffset": str(int(offset)),
         "resultRecordCount": str(int(count))}
    return layer.rstrip("/") + "/query?" + urllib.parse.urlencode(p)


def fetch_features(service, box_lonlat, http_get=None, page=1000, max_pages=200, cancel=None,
                   progress=None):
    """Every feature of an ArcGIS feature source touching the box, as GeoJSON features.
    Raises ServiceError on an error answer, and when the page limit is reached (so a run never
    goes on with half a layer)."""
    http_get = http_get or urllib_get
    lay = layer_url(service)
    feats = []
    try:                                  # 1.19.0: 3DHP features also get the NHD properties
        from . import hydro3dhp as _H
    except ImportError:                   # outside the package
        import hydro3dhp as _H            # noqa
    norm = _H.normaliser_for_url(lay) or (lambda f: f)
    for n in range(max_pages):
        if cancel and cancel():
            raise ServiceError("cancelled")
        raw = http_get(query_url(lay, box_lonlat, offset=n * page, count=page))
        try:
            d = json.loads(raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw)
        except ValueError:
            raise ServiceError("{}: the answer is not GeoJSON".format(service.get("name")))
        if isinstance(d, dict) and "error" in d:
            e = d["error"] or {}
            raise ServiceError("{}: {} {}".format(service.get("name"), e.get("code", ""),
                                                  e.get("message", "")))
        got = d.get("features") or [] if isinstance(d, dict) else []
        feats.extend(got)
        if progress:
            progress(len(feats))
        more = bool(d.get("exceededTransferLimit")) or \
            bool((d.get("properties") or {}).get("exceededTransferLimit"))
        if not got or (len(got) < page and not more):
            return norm(feats)
    raise ServiceError("{}: more than {} features in the area - use a smaller area or the "
                       "offline pack".format(service.get("name"), page * max_pages))


def export_image_url(service, box, size, sr=5070, pixel_type="F32", fmt="tiff"):
    """ArcGIS ImageServer /exportImage address for a box (in `sr`) and a pixel size."""
    base, _lay = split_arcgis(service.get("url"))
    p = {"f": "image", "format": fmt, "pixelType": pixel_type,
         "bbox": "{},{},{},{}".format(*box), "bboxSR": sr, "imageSR": sr,
         "size": "{},{}".format(int(size[0]), int(size[1])),
         "interpolation": "RSP_BilinearInterpolation" if pixel_type == "F32" else
         "RSP_NearestNeighbor"}
    return base + "/exportImage?" + urllib.parse.urlencode(p)


def wcs_coverage_url(service, box, size, crs="EPSG:5070"):
    """WCS 2.0.1 GetCoverage address (GeoTIFF) for a box in `crs` axes x / y."""
    url = service.get("url", "").split("?")[0]
    cov = service.get("layer") or ""
    if not cov:
        raise ServiceError("{}: choose a coverage first".format(service.get("name")))
    # 1.19.0: the scaling extension names the coverage's GRID axes: MRLC's GeoServer calls them i / j
    # (X / Y answered "ScaleAxisUndefined" - the land cover could not be fetched); `scale_axes`
    ax = tuple(service.get("scale_axes") or ("X", "Y"))
    q = [("service", "WCS"), ("version", "2.0.1"), ("request", "GetCoverage"),
         ("coverageId", cov), ("format", "image/tiff"),
         ("subset", "X({},{})".format(box[0], box[2])), ("subset", "Y({},{})".format(box[1], box[3])),
         ("subsettingCrs", "http://www.opengis.net/def/crs/EPSG/0/" + crs.split(":")[-1]),
         ("outputCrs", "http://www.opengis.net/def/crs/EPSG/0/" + crs.split(":")[-1]),
         ("scaleSize", "{}({}),{}({})".format(ax[0], int(size[0]), ax[1], int(size[1])))]
    return url + "?" + urllib.parse.urlencode(q)


# -------------------------------------------------------------------- catalog --
def default_path():
    try:
        from . import suitedata
        folder = suitedata.data_folder()
    except Exception:                                            # noqa
        folder = ""
    if folder:
        return os.path.join(folder, SERVICES_FILE)
    return os.path.join(os.path.expanduser("~"), ".ndsuite", SERVICES_FILE)


class Catalog(object):
    """The built-in services plus the user's, and the user's choices."""

    def __init__(self, path=None, data=None):
        self.path = path
        data = data or {}
        self.user = [dict(s, builtin=False) for s in data.get("servers") or []
                     if isinstance(s, dict) and s.get("id") and s.get("url")]
        self.choice = {k: v for k, v in (data.get("choice") or {}).items()
                       if isinstance(k, str) and isinstance(v, str)}
        self.hidden = [h for h in data.get("hidden") or [] if isinstance(h, str)]
        self.problem = data.get("_problem", "")

    # -- reading
    def all(self, include_hidden=False):
        out = [dict(s) for s in BUILTIN if include_hidden or s["id"] not in self.hidden]
        return out + [dict(s) for s in self.user]

    def get(self, sid):
        for s in self.all(include_hidden=True):
            if s["id"] == sid:
                return s
        return None

    def services(self, role, data_only=False):
        out = [s for s in self.all() if s.get("role") == role]
        if data_only:
            out = [s for s in out if data_capable(s, role)]
        return out

    def chosen(self, role):
        """The data (or display) service for a role: the user's pick, else the first built-in."""
        sid = self.choice.get(role)
        s = self.get(sid) if sid else None
        if s is not None and s["id"] not in self.hidden:
            return s
        rtype = ROLES.get(role, ("", "display", ""))[1]
        cands = self.services(role, data_only=(rtype != "display"))
        return cands[0] if cands else None

    def is_default(self, role):
        c = self.chosen(role)
        cands = self.services(role, data_only=ROLES.get(role, ("", "display"))[1] != "display")
        return c is None or not cands or c["id"] == cands[0]["id"] and c.get("builtin")

    # -- changing
    def add(self, url, role, name="", kind="", layer="", fields=(), notes=""):
        url = (url or "").strip()
        if role not in ROLES:
            raise ServiceError("unknown data role {!r}".format(role))
        kind = kind or detect_kind(url)
        if kind not in KINDS:
            raise ServiceError("cannot tell what kind of server this is - choose the kind")
        if kind == "file":
            if not os.path.exists(url):
                raise ServiceError("no such file or folder: " + url)
        elif not re.match(r"^https?://", url, re.I):
            raise ServiceError("give an http(s) address")
        n = 1
        ids = {s["id"] for s in self.all(include_hidden=True)}
        while "user_{}".format(n) in ids:
            n += 1
        s = {"id": "user_{}".format(n), "role": role, "name": name.strip() or url, "kind": kind,
             "url": url, "layer": str(layer or ""), "fields": list(fields), "source": "added by you",
             "confidence": "yours", "notes": notes, "builtin": False}
        self.user.append(s)
        return s

    def update(self, sid, **changes):
        for s in self.user:
            if s["id"] == sid:
                for k in ("name", "layer", "role", "kind", "url", "notes"):
                    if k in changes and changes[k] is not None:
                        s[k] = str(changes[k])
                return s
        raise ServiceError("only your own servers can be changed")

    def remove(self, sid):
        if sid in _BUILTIN_IDS:
            if sid not in self.hidden:
                self.hidden.append(sid)
        else:
            self.user = [s for s in self.user if s["id"] != sid]
        for role, c in list(self.choice.items()):
            if c == sid:
                del self.choice[role]

    def unhide_all(self):
        self.hidden = []

    def choose(self, role, sid):
        if role not in ROLES:
            raise ServiceError("unknown data role {!r}".format(role))
        if sid in (None, ""):
            self.choice.pop(role, None)
            return
        s = self.get(sid)
        if s is None:
            raise ServiceError("no service {!r}".format(sid))
        self.choice[role] = sid

    def to_dict(self):
        return {"format": FORMAT, "format_version": FORMAT_VERSION,
                "servers": [{k: v for k, v in s.items() if k != "builtin"} for s in self.user],
                "choice": dict(sorted(self.choice.items())), "hidden": list(self.hidden)}

    def save(self, path=None):
        path = path or self.path or default_path()
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, indent=1)
        os.replace(tmp, path)
        self.path = path
        return path


def load(path=None):
    """The catalog (built-in + the user's file). A missing file is an empty user list; an
    unreadable one is reported in Catalog.problem and left untouched (never overwritten until
    the user saves)."""
    path = path or default_path()
    data = {}
    if path and os.path.isfile(path):
        try:
            with open(path, encoding="utf-8-sig") as f:
                data = json.load(f)
            if not isinstance(data, dict) or data.get("format") not in (None, FORMAT):
                data = {"_problem": "{} is not a suite services file".format(path)}
            elif int(data.get("format_version") or 1) > FORMAT_VERSION:
                data = {"_problem": "{} was written by a newer plugin".format(path)}
        except (OSError, ValueError) as e:
            data = {"_problem": "{} cannot be read: {}".format(path, e)}
    return Catalog(path, data)


# role -> ND Dam Hazard constants key (its downloads take these addresses)
HAZARD_KEYS = {"structures": "USA_STRUCTURES_URL", "address_points": "ND_ADDRESS_POINTS_URL",
               "roads": "ND_ROADS_URL", "rail": "NARN_URL", "crossings": "CROSSINGS_URL",
               "bridges": "NBI_URL", "nid_dams": "NID_URL", "flowlines": "NHD_FLOWLINE_URL",
               "waterbodies": "NHD_WATERBODY_URL", "imagery": "AERIAL_IMAGERY_URL"}


def hazard_overrides(catalog):
    """{ND Dam Hazard constant: address} for the roles where the user picked a service that is not
    the built-in default and that Hazard can use (an ArcGIS layer; tiles for imagery)."""
    out = {}
    for role, key in HAZARD_KEYS.items():
        sid = catalog.choice.get(role)
        if not sid:
            continue
        s = catalog.get(sid)
        if s is None:
            continue
        cands = catalog.services(role, data_only=(role != "imagery"))
        if cands and cands[0]["id"] == sid and s.get("builtin"):
            continue                                            # the default anyway
        if role == "imagery":
            if s["kind"] == "xyz":
                out[key] = s["url"]
            continue
        if s["kind"] in ("arcgis_feature", "arcgis_map") and data_capable(s, role):
            try:
                out[key] = layer_url(s)
            except ServiceError:
                pass
    return out
