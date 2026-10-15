"""netproxy - the proxy set in QGIS (Settings > Options > Network) for the suite's downloads
that go through Python's urllib instead of QGIS's own network client (1.16.2).

urllib on its own follows the computer's proxy (the Windows / macOS settings, http_proxy and
https_proxy) and checks certificates, but it knows nothing of a proxy set only in QGIS. On such
a network those downloads failed while QGIS's own ones worked. urlopen() here sends the request
through QGIS's proxy when one is set for that address, and is plain urllib.request.urlopen
otherwise: outside QGIS, with the proxy off, with "Default proxy" (= the system's), for a SOCKS
or FTP proxy urllib cannot use, and for an address on QGIS's exclude list. Certificates are
checked either way; nothing here turns that off.

QGIS keeps the setting under proxy/ (proxyEnabled, proxyType, proxyHost, proxyPort, proxyUser,
proxyPassword, authcfg, proxyExcludedUrls, noProxyUrls), read here with QgsSettings, which works
from any thread. Pure standard library apart from that lazy import.
"""
import urllib.parse
import urllib.request

HTTP_TYPES = ("HttpProxy", "HttpCachingProxy")      # the QNetworkProxy types urllib can use
NO_PROXY = ""                                        # proxy_for(): connect directly


def _truthy(v):
    if isinstance(v, str):
        return v.strip().lower() in ("true", "1", "yes")
    return bool(v)


def _url_list(v):
    """A QGIS URL list: a list (QGIS 3.x options dialog) or one '|'-joined string (older)."""
    if v is None:
        return []
    items = v if isinstance(v, (list, tuple)) else str(v).split("|")
    return [str(i).strip() for i in items if str(i).strip()]


def qgis_settings():
    """QGIS's proxy settings as a plain dict; {} outside QGIS."""
    try:
        from qgis.core import QgsSettings
        s = QgsSettings()
        keys = ("proxyEnabled", "proxyType", "proxyHost", "proxyPort", "proxyUser",
                "proxyPassword", "authcfg", "proxyExcludedUrls", "noProxyUrls")
        return {k: s.value("proxy/" + k) for k in keys}
    except Exception:                                    # noqa: BLE001 - no QGIS, no settings
        return {}


def _auth_login(authcfg):
    """(user, password) from a QGIS authentication configuration, or ("", "")."""
    try:
        from qgis.core import QgsApplication, QgsAuthMethodConfig
        cfg = QgsAuthMethodConfig()
        QgsApplication.authManager().loadAuthenticationConfig(authcfg, cfg, True)
        return cfg.config("username") or "", cfg.config("password") or ""
    except Exception:                                    # noqa: BLE001
        return "", ""


def proxy_for(url, settings=None):
    """The proxy URL ("http://[user:password@]host:port") QGIS would use for `url`, NO_PROXY
    ("") when QGIS connects to it directly, or None when urllib's own choice stands."""
    s = qgis_settings() if settings is None else settings
    if not s or not _truthy(s.get("proxyEnabled")):
        return None
    url = str(url or "")
    # QGIS's order: the no-proxy list goes direct, the exclude list takes the system's proxy
    if any(url.startswith(u) for u in _url_list(s.get("noProxyUrls"))):
        return NO_PROXY
    if any(url.startswith(u) for u in _url_list(s.get("proxyExcludedUrls"))):
        return None
    host = str(s.get("proxyHost") or "").strip()
    if str(s.get("proxyType") or "") not in HTTP_TYPES or not host:
        return None
    if "://" in host:
        host = host.split("://", 1)[1]
    host = host.rstrip("/")
    try:
        port = int(s.get("proxyPort") or 0)
    except (TypeError, ValueError):
        port = 0
    user, pw = str(s.get("proxyUser") or ""), str(s.get("proxyPassword") or "")
    if not user and s.get("authcfg"):
        user, pw = _auth_login(str(s.get("authcfg")))
    login = ""
    if user:
        login = urllib.parse.quote(user, safe="") + (
            ":" + urllib.parse.quote(pw, safe="") if pw else "") + "@"
    return "http://{}{}{}".format(login, host, ":{}".format(port) if port else "")


def _url_of(req):
    return req.full_url if isinstance(req, urllib.request.Request) else str(req)


def urlopen(req, timeout=60, settings=None):
    """urllib.request.urlopen(req, timeout=timeout) through QGIS's proxy (see above)."""
    proxy = proxy_for(_url_of(req), settings)
    if proxy is None:
        return urllib.request.urlopen(req, timeout=timeout)
    table = {} if proxy == NO_PROXY else {"http": proxy, "https": proxy}
    return urllib.request.build_opener(urllib.request.ProxyHandler(table)).open(
        req, timeout=timeout)


def env_for(url, env, settings=None):
    """`env` (a dict, changed in place and returned) with HTTP(S)_PROXY set to QGIS's proxy for
    `url`, for a program started from the plugin that downloads on its own (micromamba)."""
    proxy = proxy_for(url, settings)
    if proxy:
        for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            env[k] = proxy
    return env


def gdal_options(url, settings=None):
    """GDAL config options for a /vsicurl/ read of `url` through QGIS's proxy ({} for none);
    set them with gdal.SetThreadLocalConfigOption around the read."""
    proxy = proxy_for(url, settings)
    if not proxy:
        return {}
    rest = proxy.split("://", 1)[1]
    login, _, hostport = rest.rpartition("@")
    opts = {"GDAL_HTTP_PROXY": hostport}
    if login:
        opts["GDAL_HTTP_PROXYUSERPWD"] = urllib.parse.unquote(login)
    return opts
