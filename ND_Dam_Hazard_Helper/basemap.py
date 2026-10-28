# -*- coding: utf-8 -*-
"""Aerial imagery base map: the Review tab and (v2.9) the report maps.

When the engineer zooms to a structure, an aerial photo under the map shows
what the structure is (house, shed, grain bin, ruin) - the question the
review is about. The imagery is an XYZ tile layer added once, at the bottom
of the layer tree (below the engineer's own layers and the plugin's groups),
so it never hides anything. It is a picture for looking at, not data the
classification uses; if it cannot be added (no network, unsupported QGIS
provider) the zoom goes on without it.

v2.9: the same layer goes under every map of the PDF report and of the EAP
map book (layout_imagery / with_imagery). Those map items keep an explicit
layer list, so the imagery is drawn there whether or not it is switched on
in the Layers panel: when a layout adds it, it is added switched OFF (the
canvas does not change and does not download tiles) and marked; the Review
tab's zoom switches such a layer on the first time it asks for it. A layer
already in the project is used as it is, visibility included. Offline (the
"offline only" setting, or the imagery service does not answer a one-tile
probe) the maps are made without imagery and the caller gets a note.

The service address is AERIAL_IMAGERY_URL in constants.py and can be changed
in the data-source file (Readme tab > Edit data sources)."""

import math
import time
from urllib.parse import quote, unquote, urlparse

from qgis.core import QgsProject, QgsRasterLayer

from . import constants as C
from .helpers import log_message

# a layer added switched off for the layouts carries this custom property
# (saved with the project) until something shows it on the canvas
HIDDEN_PROP = "nd_dam_hazard_helper/added_hidden"
PROBE_ZOOM = 12                # tile level of the one-tile service check
PROBE_TIMEOUT_MS = 8000        # transfer timeout of that check (Qt >= 5.15)
PROBE_TTL_S = 300.0            # a check is re-used this long (report + map book)
PROBE_TTL_FAIL_S = 60.0        # ... a failed one only this long (reconnected?)
_PROBES = {}                   # url -> (time, ok, text)
_IMAGE_MAGIC = (b"\x89PNG", b"\xff\xd8\xff", b"GIF8")


def valid_url(url):
    """True for an http(s) XYZ address with {x}, {y} and {z}."""
    url = str(url or "")
    return url.startswith("http") and all(k in url for k in ("{x}", "{y}", "{z}"))


def source_string(url=None, zmax=None):
    """The provider string of an XYZ layer."""
    url = url or C.AERIAL_IMAGERY_URL
    return "type=xyz&url={}&zmin=0&zmax={}".format(quote(url, safe=":/{}"),
                                                    int(zmax or C.AERIAL_MAX_ZOOM))


def url_of(source):
    """The tile address in an XYZ provider string (or `source` itself when
    it is already an address)."""
    s = str(source or "")
    if s.startswith("http"):
        return s
    for part in s.split("&"):
        if part.startswith("url="):
            return unquote(part[4:])
    return ""


def source_name(source=None):
    """Short name of the imagery service for the map credit: the name in
    AERIAL_IMAGERY_CREDIT for the built-in address, else the host of the
    address set in the data-source file (the plugin does not guess a
    provider's name)."""
    url = url_of(source) if source else C.AERIAL_IMAGERY_URL
    named = getattr(C, "AERIAL_IMAGERY_SOURCE_NAME", None)     # optional override
    if named:
        return str(named)
    builtin = url == C.AERIAL_IMAGERY_URL and "AERIAL_IMAGERY_URL" not in C.SOURCE_OVERRIDES
    if builtin:
        credit = str(C.AERIAL_IMAGERY_CREDIT or "")
        name = credit.split(":", 1)[-1].split("(")[0].strip()
        if name:
            return name
    try:
        host = urlparse(url).netloc
    except Exception:
        host = ""
    return host or "aerial imagery service"


def caption(source=None):
    """'Imagery: <service>' - the credit printed under a map with imagery."""
    return "Imagery: {}".format(source_name(source))


def find_layer():
    """The imagery layer already in the project, or None."""
    try:
        found = QgsProject.instance().mapLayersByName(C.AERIAL_IMAGERY_NAME)
    except Exception:
        return None
    return found[0] if found else None


def new_layer(url=None):
    """A new imagery layer (not added to the project), or None. Resampled
    bilinearly so printed maps are not blocky between tile zoom levels."""
    url = url or C.AERIAL_IMAGERY_URL
    layer = QgsRasterLayer(source_string(url), C.AERIAL_IMAGERY_NAME, "wms")
    if not layer.isValid():
        return None
    try:
        from qgis.core import QgsBilinearRasterResampler
        rf = layer.resampleFilter()
        rf.setZoomedInResampler(QgsBilinearRasterResampler())
        rf.setZoomedOutResampler(QgsBilinearRasterResampler())
    except Exception:
        pass
    return layer


def _added_hidden(layer):
    try:
        v = layer.customProperty(HIDDEN_PROP, False)
    except Exception:
        return False
    return str(v).lower() in ("1", "true", "yes")


def ensure_aerial(visible=True):
    """Add the imagery layer at the bottom of the layer tree when the project
    has none. Returns (state, text): 'present' (already there, left as the
    user has it), 'added', or 'failed' (text says why).

    visible=False (the layouts): a new layer is added switched off and
    marked; a later ensure_aerial() (the Review tab's zoom) switches such a
    layer on once - it was never the user's choice to hide it."""
    layer = find_layer()
    if layer is not None:
        if visible and _added_hidden(layer):
            set_visible(True)                  # also drops the mark
        return "present", "aerial imagery already in the project"
    url = C.AERIAL_IMAGERY_URL
    if not valid_url(url):
        return "failed", "the aerial imagery address needs {x}, {y} and {z}: " + str(url)
    try:
        layer = new_layer(url)
        if layer is None:
            return "failed", "QGIS could not open the aerial imagery service"
        project = QgsProject.instance()
        project.addMapLayer(layer, False)
        node = project.layerTreeRoot().addLayer(layer)   # last child = bottom of the tree
        if not visible and node is not None:
            node.setItemVisibilityChecked(False)
    except Exception as e:
        return "failed", "aerial imagery not added ({})".format(e)
    if not visible:
        try:
            layer.setCustomProperty(HIDDEN_PROP, True)
        except Exception:
            pass
    log_message("Added aerial imagery base map{}. {}".format(
        "" if visible else " (switched off; used by the report maps)", C.AERIAL_IMAGERY_CREDIT))
    return "added", C.AERIAL_IMAGERY_CREDIT


def set_visible(on):
    """Show or hide the imagery layer if it is in the project; returns True
    when there was one."""
    layer = find_layer()
    if layer is None:
        return False
    try:
        node = QgsProject.instance().layerTreeRoot().findLayer(layer.id())
        if node is not None:
            node.setItemVisibilityChecked(bool(on))
    except Exception as e:
        log_message("Aerial imagery visibility not changed: {}".format(e))
        return False
    try:
        layer.removeCustomProperty(HIDDEN_PROP)          # now the user's choice
    except Exception:
        pass
    return True


# ------------------------------------------------------------ v2.9: layouts --
def offline_only():
    """The 'offline only' setting (no internet requests at all)."""
    try:
        from .download import offline_config
        return bool(offline_config()[1])
    except Exception:
        return False


def suite_offline():
    """True only when ND_DAM_OFFLINE is set to 1 / true / yes / on."""
    import os
    v = os.environ.get("ND_DAM_OFFLINE", "").strip().lower()
    return v in ("1", "true", "yes", "on")


def probe_error_text(message, elapsed_s, timeout_ms):
    """A short reason for a failed probe, naming a timeout when one happened."""
    msg = str(message or "")
    if elapsed_s is not None and timeout_ms and elapsed_s * 1000 >= 0.9 * timeout_ms:
        return "did not answer within {:.0f} s (timed out)".format(timeout_ms / 1000.0)
    if "timed out" in msg.lower() or "canceled" in msg.lower():
        return "did not answer within the timeout"
    return msg[:120] or "no answer"


def probe_key(url, request_url=None):
    """Cache key for a probe: the URL plus its query, without BBOX / WIDTH / HEIGHT."""
    from urllib.parse import urlparse, parse_qsl, urlencode
    u = request_url or url or ""
    p = urlparse(u)
    q = [(k, v) for k, v in parse_qsl(p.query) if k.upper() not in ("BBOX", "WIDTH", "HEIGHT")]
    return p.scheme + "://" + p.netloc + p.path + "?" + urlencode(q)


def label_checks(lonlat=None, timeout_ms=3000):
    """Probe one tile per label service. Returns {service: True/False}."""
    # Placeholder: the full probe loop lives in DamSafety_Hazard/basemap.py.
    # This port keeps the signature so callers can be wired later.
    return {}


def forget_probe():
    """Forget the cached service checks (tests; a changed connection)."""
    _PROBES.clear()


def center_lonlat(rect, crs):
    """(lon, lat) of the centre of `rect` (in `crs`), or None."""
    try:
        from qgis.core import QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsPointXY
        cx = (rect.xMinimum() + rect.xMaximum()) / 2.0
        cy = (rect.yMinimum() + rect.yMaximum()) / 2.0
        xf = QgsCoordinateTransform(crs, QgsCoordinateReferenceSystem("EPSG:4326"),
                                    QgsProject.instance().transformContext())
        p = QgsPointXY(cx, cy) if xf.isShortCircuited() else xf.transform(QgsPointXY(cx, cy))
        lon, lat = float(p.x()), float(p.y())
    except Exception:
        return None
    if not (math.isfinite(lon) and math.isfinite(lat) and -180 <= lon <= 180
            and -85 <= lat <= 85):
        return None
    return lon, lat


def tile_xy(lon, lat, z):
    """Web Mercator XYZ tile holding lon / lat at level z."""
    n = 2 ** int(z)
    x = int((lon + 180.0) / 360.0 * n)
    r = math.radians(lat)
    y = int((1.0 - math.log(math.tan(r) + 1.0 / math.cos(r)) / math.pi) / 2.0 * n)
    return min(max(x, 0), n - 1), min(max(y, 0), n - 1)


def tile_url(url, x, y, z):
    return str(url).replace("{x}", str(x)).replace("{y}", str(y)).replace("{z}", str(z))


def probe(url=None, lonlat=None, timeout_ms=PROBE_TIMEOUT_MS):
    """(ok, text): does the imagery service answer with a tile image? One
    small request (the tile at the map centre, level PROBE_ZOOM; level 0
    without a centre) through QGIS's network settings (proxy, auth). The
    answer is kept PROBE_TTL_S seconds so the report and the map book of a
    run ask once. Never raises."""
    url = url or C.AERIAL_IMAGERY_URL
    if offline_only():
        return False, "offline mode (no internet requests)"
    if not valid_url(url):
        return False, "the imagery address needs {x}, {y} and {z}"
    now = time.time()
    hit = _PROBES.get(url)
    if hit is not None and now - hit[0] < (PROBE_TTL_S if hit[1] else PROBE_TTL_FAIL_S):
        return hit[1], hit[2]
    z = min(PROBE_ZOOM, int(C.AERIAL_MAX_ZOOM or PROBE_ZOOM)) if lonlat else 0
    x, y = tile_xy(lonlat[0], lonlat[1], z) if lonlat else (0, 0)
    ok, text = False, ""
    try:
        from qgis.core import QgsBlockingNetworkRequest
        from qgis.PyQt.QtCore import QUrl
        from qgis.PyQt.QtNetwork import QNetworkRequest
        from .compat import NET_NO_ERROR, enum_value
        req = QNetworkRequest(QUrl(tile_url(url, x, y, z)))
        try:
            req.setTransferTimeout(int(timeout_ms))          # Qt >= 5.15
        except Exception:
            pass
        blocking = QgsBlockingNetworkRequest()
        # forceRefresh: a tile in QGIS's network cache must not pass for a connection
        err = blocking.get(req, True)
        if enum_value(err) != enum_value(NET_NO_ERROR):
            text = "the imagery service did not answer ({})".format(
                blocking.errorMessage() or "network error")
        else:
            content = blocking.reply().content()
            data = bytes(content) if isinstance(content, (bytes, bytearray)) else \
                bytes(content.data())
            if data.startswith(_IMAGE_MAGIC) or (data[:4] == b"RIFF" and data[8:12] == b"WEBP"):
                ok, text = True, "imagery service answered"
            else:
                text = "the imagery service did not send an image (a login page or an " \
                       "error message?)"
    except Exception as e:
        text = "the imagery service could not be checked ({})".format(e)
    _PROBES[url] = (now, ok, text)
    if not ok:
        log_message("Aerial imagery for the maps: " + text)
    return ok, text


def layout_imagery(add_to_project=True, rect=None, crs=None):
    """The imagery layer for the maps of a print layout: (layer or None,
    caption, note). The note says why there is none (offline mode, service
    not reachable, ...). add_to_project: the layout is kept in the project,
    so its map layers must be project layers - the project's imagery layer
    is used (added switched off when missing); otherwise a private layer
    (keep a reference while the layout exists). Never raises."""
    try:
        if offline_only():
            return None, None, "offline mode (no internet requests)"
        from .helpers import on_main_thread
        in_project = bool(add_to_project) and on_main_thread()
        existing = find_layer() if in_project else None
        url = url_of(existing.source()) if existing is not None else C.AERIAL_IMAGERY_URL
        lonlat = center_lonlat(rect, crs) if rect is not None and crs is not None else None
        ok, text = probe(url, lonlat)
        if not ok:
            return None, None, text
        if in_project:
            state, text = ensure_aerial(visible=False)
            layer = find_layer() if state != "failed" else None
            if layer is None:
                return None, None, text
        else:
            layer = new_layer(url)
            if layer is None:
                return None, None, "QGIS could not open the aerial imagery service"
        if not layer.isValid():
            return None, None, "the aerial imagery layer is not valid"
        return layer, caption(layer.source()), ""
    except Exception as e:
        log_message("Aerial imagery for the maps skipped: {}".format(e))
        return None, None, "aerial imagery not available ({})".format(e)


def with_imagery(layers, imagery):
    """Layer list of a map item (top first) with the imagery LAST, i.e. under
    everything. An empty list is returned unchanged: such a map item follows
    the project's visible layers (the imagery then shows only if it is
    switched on in the project, which the plugin never does itself)."""
    layers = list(layers or [])
    if imagery is None or not layers:
        return layers
    return [lyr for lyr in layers if lyr is not imagery] + [imagery]
