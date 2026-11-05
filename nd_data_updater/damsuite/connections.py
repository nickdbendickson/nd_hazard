"""Browser connections for the suite's map services (suite 1.18.5).

The services the suite knows (services.BUILTIN plus the ones the user added in Suite data and
servers) are written into the QGIS profile as Browser connections, so they are listed under
**WMS/WMTS**, **XYZ Tiles** and **ArcGIS REST Servers** as soon as QGIS starts:

    wms / wmts             -> WMS/WMTS connection (the service address)
    xyz                    -> XYZ Tiles connection (the tile template)
    arcgis_feature / _map  -> ArcGIS REST Servers connection (the FeatureServer / MapServer)
    arcgis_image           -> ArcGIS REST Servers connection (the services folder that holds the
                              ImageServer: QGIS lists image services inside a folder)

Rules:
  * A connection is named "<prefix>: <service name>" (prefix "ND Dam Suite" or "DamSafety").
  * Each name is added ONCE. The names already added are recorded in the setting
    ndsuite/connections_added, so a connection the user deleted or edited is never put back or
    overwritten. A newer suite that knows more services adds only the new ones.
  * ndsuite/register_connections (default on) turns it off; nothing is removed then.
  * QGIS 3.30 and newer keep connections under connections/...; 3.28 under qgis/connections-*.
    The right keys are written for the running QGIS.

register(settings) works on any object with value() / setValue() (a QgsSettings, or a dict
wrapper in the tests); it never raises - a failure is returned as a note.
"""
import re

from . import services as SV

ENABLED_KEY = "ndsuite/register_connections"
ADDED_KEY = "ndsuite/connections_added"
DEFAULT_PREFIX = "Dam Suite"

_ARC_SERVICE = re.compile(r"^(?P<base>.+?/(?:MapServer|FeatureServer))(?:/\d+)?/?$", re.I)
_ARC_IMAGE = re.compile(r"^(?P<root>.+?/rest/services)(?:/.*)?/[^/]+/ImageServer/?$", re.I)


def connection_for(service):
    """(kind, url) of the Browser connection for one catalog service, or None. kind: "wms",
    "xyz" or "arcgis"."""
    kind = service.get("kind")
    url = (service.get("url") or "").strip()
    if not url.lower().startswith(("http://", "https://")):
        return None
    if kind in ("wms", "wmts"):
        return "wms", url.split("?")[0]
    if kind == "xyz":
        return "xyz", url
    if kind in ("arcgis_feature", "arcgis_map"):
        m = _ARC_SERVICE.match(url.split("?")[0])
        return ("arcgis", m.group("base")) if m else None
    if kind == "arcgis_image":
        m = _ARC_IMAGE.match(url.split("?")[0])
        return ("arcgis", m.group("root")) if m else None
    return None


def planned(catalog_services=None, prefix=DEFAULT_PREFIX):
    """[(name, kind, url)] one per distinct (kind, url), in catalog order. The first service
    that uses an address names the connection."""
    services = SV.BUILTIN if catalog_services is None else catalog_services
    seen, out = set(), []
    for s in services:
        c = connection_for(s)
        if not c or c in seen:
            continue
        seen.add(c)
        name = "{}: {}".format(prefix, s.get("name") or s.get("id"))
        out.append((name, c[0], c[1]))
    return out


def _new_tree(qgis_version_int):
    return int(qgis_version_int or 0) >= 33000


def keys_for(kind, name, qgis_version_int):
    """{setting key: value template} for one connection ("{url}" is filled in)."""
    if _new_tree(qgis_version_int):
        if kind == "wms":
            return {"connections/ows/items/wms/connections/items/{}/url".format(name): "{url}"}
        if kind == "xyz":
            base = "connections/xyz/items/{}/".format(name)
            return {base + "url": "{url}", base + "zmin": 0, base + "zmax": 19}
        return {"connections/arcgisfeatureserver/items/{}/url".format(name): "{url}"}
    if kind == "wms":
        return {"qgis/connections-wms/{}/url".format(name): "{url}"}
    if kind == "xyz":
        base = "qgis/connections-xyz/{}/".format(name)
        return {base + "url": "{url}", base + "zmin": 0, base + "zmax": 19}
    return {"qgis/connections-arcgisfeatureserver/{}/url".format(name): "{url}"}


def _as_list(v):
    if v is None or v == "":
        return []
    if isinstance(v, (list, tuple)):
        return [str(x) for x in v]
    return [str(v)]


def _on(v):
    if v is None or v == "":
        return True                                  # default: on
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() not in ("false", "0", "no", "off")


def register(settings, catalog_services=None, prefix=DEFAULT_PREFIX, qgis_version_int=None):
    """Add the suite's services as Browser connections (see the module text). Returns
    {"added": [names], "kept": n (already added before), "off": bool, "note": text}."""
    out = {"added": [], "kept": 0, "off": False, "note": ""}
    try:
        if not _on(settings.value(ENABLED_KEY, True)):
            out["off"] = True
            return out
        if qgis_version_int is None:
            try:
                from qgis.core import Qgis
                qgis_version_int = Qgis.QGIS_VERSION_INT
            except Exception:                                    # noqa
                qgis_version_int = 33400
        done = set(_as_list(settings.value(ADDED_KEY, [])))
        for name, kind, url in planned(catalog_services, prefix):
            if name in done:
                out["kept"] += 1
                continue
            for key, val in keys_for(kind, name, qgis_version_int).items():
                settings.setValue(key, url if val == "{url}" else val)
            done.add(name)
            out["added"].append(name)
        if out["added"]:
            settings.setValue(ADDED_KEY, sorted(done))
    except Exception as e:                                       # noqa - never break start-up
        out["note"] = "map service connections not added: {}".format(e)
    return out


def catalog_services():
    """The built-in services plus the ones the user added (services.json), or the built-ins
    when the user's file cannot be read."""
    try:
        cat = SV.load()
        hidden = set(getattr(cat, "hidden", []) or [])
        extra = [s for s in getattr(cat, "user", []) or [] if isinstance(s, dict)]
        return [s for s in list(SV.BUILTIN) + extra if s.get("id") not in hidden]
    except Exception:                                            # noqa
        return list(SV.BUILTIN)


def register_in_qgis(prefix=DEFAULT_PREFIX, iface=None):
    """Called by each plugin when QGIS starts: register with QgsSettings, then ask the Browser
    to reload its connections when something was added. Never raises."""
    try:
        from qgis.core import QgsSettings
        res = register(QgsSettings(), catalog_services(), prefix)
    except Exception as e:                                       # noqa
        return {"added": [], "kept": 0, "off": False, "note": str(e)}
    if res["added"] and iface is not None:
        try:
            iface.reloadConnections()
        except Exception:                                        # noqa
            pass
    return res
