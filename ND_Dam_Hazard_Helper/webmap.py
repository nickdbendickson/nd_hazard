# -*- coding: utf-8 -*-
"""Web-map link for the Review tab ("Open in web map").

The engineer opens the selected structure's location in the web browser for
a street-level look (street view, recent imagery, business names). The
default is the Google Maps URLs "search" action, which needs no API key:

    https://www.google.com/maps/search/?api=1&query={lat}%2C{lon}

(documented at https://developers.google.com/maps/documentation/urls/get-started:
latitude first, the comma encoded as %2C). Another address can be set with
WEBMAP_URL_TEMPLATE in constants.py / the data-source file: an http(s)
address with {lat} and {lon} in it, e.g. the Google Street View panorama
https://www.google.com/maps/@?api=1&map_action=pano&viewpoint={lat}%2C{lon}
(rural North Dakota often has no panorama, so the search pin is the default).
An address that is not usable is ignored (one line in the log) and the
default is used.

Opening the link sends the structure's coordinates to that web site; nothing
else about the study leaves the computer."""

import math
from urllib.parse import urlparse

from qgis.core import (
    QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsPointXY, QgsProject,
)

from . import constants as C
from .helpers import log_message
from .compat import MSG_WARNING

DEFAULT_TEMPLATE = "https://www.google.com/maps/search/?api=1&query={lat}%2C{lon}"
WGS84 = "EPSG:4326"
DECIMALS = 6                     # ~0.1 m - plenty for "which building is it"
_warned = set()                  # bad templates already reported (one log line each)


def template_problem(template):
    """Why a web-map address cannot be used (text), or None when it can."""
    if not isinstance(template, str):
        return "the address is not text"
    t = template.strip()
    if not t:
        return "the address is empty"
    try:
        parts = urlparse(t)
    except ValueError as e:
        return "not a web address ({})".format(e)
    if parts.scheme.lower() not in ("http", "https") or not parts.netloc:
        return "only http:// or https:// addresses are opened"
    if "{lat}" not in t or "{lon}" not in t:
        return "the address needs {lat} and {lon}"
    return None


def configured_template():
    """WEBMAP_URL_TEMPLATE from constants / the data-source file, or None."""
    return getattr(C, "WEBMAP_URL_TEMPLATE", None) or None


def usable_template(template=None):
    """The template to use: `template` (or the configured one) when it is
    valid, otherwise DEFAULT_TEMPLATE (a bad one is logged once)."""
    if template is None:
        template = configured_template()
    if template is None:
        return DEFAULT_TEMPLATE
    problem = template_problem(template)
    if problem is None:
        return template.strip()
    key = str(template)
    if key not in _warned:
        _warned.add(key)
        log_message("Web map address {!r} not used ({}) - using {}".format(
            template, problem, DEFAULT_TEMPLATE), MSG_WARNING)
    return DEFAULT_TEMPLATE


def valid_lonlat(lon, lat):
    try:
        lon, lat = float(lon), float(lat)
    except (TypeError, ValueError):
        return False
    return (math.isfinite(lon) and math.isfinite(lat) and -180.0 <= lon <= 180.0 and
            -90.0 <= lat <= 90.0)


def url_for(lat, lon, template=None):
    """The web-map address of a WGS 84 location (latitude first, as in the
    template's {lat} / {lon}). Raises ValueError for an impossible location."""
    if not valid_lonlat(lon, lat):
        raise ValueError("not a longitude / latitude: {}, {}".format(lon, lat))
    fmt = "{:.%df}" % DECIMALS
    # str.replace, not str.format: a template may hold other braces
    return (usable_template(template).replace("{lat}", fmt.format(float(lat)))
            .replace("{lon}", fmt.format(float(lon))))


def lonlat_of(rec, crs, ctx=None):
    """(lon, lat) in WGS 84 of a structure record (its representative point
    in the analysis CRS `crs`), or None when it cannot be located."""
    from .review import rep_point
    p = rep_point(rec)
    if p is None or crs is None:
        return None
    try:
        if not crs.isValid():
            return None               # projected numbers must never be read as degrees
        if ctx is None:
            ctx = QgsProject.instance().transformContext()
        xf = QgsCoordinateTransform(crs, QgsCoordinateReferenceSystem(WGS84), ctx)
        q = QgsPointXY(p.x(), p.y())
        if not xf.isShortCircuited():
            q = xf.transform(q)
        lon, lat = q.x(), q.y()
    except Exception as e:
        log_message("Web map: structure {} could not be located in WGS 84 ({})".format(
            rec.get("id", "?"), e), MSG_WARNING)
        return None
    return (lon, lat) if valid_lonlat(lon, lat) else None


def url_of_structure(rec, crs, ctx=None, template=None):
    """The web-map address of a structure, or None when it cannot be located."""
    ll = lonlat_of(rec, crs, ctx)
    if ll is None:
        return None
    return url_for(ll[1], ll[0], template)
