"""2.30.2 (damsuite 1.16.2): the downloads that go through urllib use the proxy set in QGIS.

damsuite.netproxy reads QGIS's proxy/ settings; here they are passed in as a dict, so the test
never touches the QGIS profile of the machine it runs on. A small local server plays the proxy:
a request for an address that does not exist (example.invalid) can only arrive through it.
Run: python3 -m ND_Dam_Hazard_Helper.tests.run_tests (from the folder above the plugin)."""
import base64
import http.server
import os
import sys
import threading
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_DIR = os.path.dirname(HERE)
if os.path.dirname(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, os.path.dirname(PLUGIN_DIR))
from ND_Dam_Hazard_Helper.tests.registry import test  # noqa: E402

BASE = {"proxyEnabled": "true", "proxyType": "HttpProxy", "proxyHost": "proxy.nd.gov",
        "proxyPort": "8080", "proxyUser": "", "proxyPassword": "", "authcfg": None,
        "proxyExcludedUrls": None, "noProxyUrls": None}


def _s(**kw):
    s = dict(BASE)
    s.update(kw)
    return s


@test
def the_proxy_qgis_would_use_for_an_address():
    from ND_Dam_Hazard_Helper.damsuite import netproxy as NP
    assert NP.proxy_for("https://a.gov/x", {}) is None                     # outside QGIS
    assert NP.proxy_for("https://a.gov/x", _s(proxyEnabled="false")) is None
    assert NP.proxy_for("https://a.gov/x", _s(proxyEnabled=False)) is None
    assert NP.proxy_for("https://a.gov/x", _s()) == "http://proxy.nd.gov:8080"
    assert NP.proxy_for("https://a.gov/x", _s(proxyEnabled=True, proxyPort=3128)) == \
        "http://proxy.nd.gov:3128"
    assert NP.proxy_for("https://a.gov/x", _s(proxyType="HttpCachingProxy")) == \
        "http://proxy.nd.gov:8080"
    # "Default proxy" is the system's, which urllib already follows; SOCKS and FTP it cannot use
    for kind in ("DefaultProxy", "Socks5Proxy", "FtpCachingProxy"):
        assert NP.proxy_for("https://a.gov/x", _s(proxyType=kind)) is None, kind
    assert NP.proxy_for("https://a.gov/x", _s(proxyHost="")) is None
    assert NP.proxy_for("https://a.gov/x", _s(proxyHost="http://proxy.nd.gov/")) == \
        "http://proxy.nd.gov:8080"
    # a user name and password, quoted for the URL
    assert NP.proxy_for("https://a.gov/x", _s(proxyUser="DOM\\nick", proxyPassword="p@ss:1")) \
        == "http://DOM%5Cnick:p%40ss%3A1@proxy.nd.gov:8080"
    # QGIS's lists: the no-proxy list goes direct, the exclude list keeps the system's proxy;
    # a list from the options dialog, or one '|'-joined string from an older QGIS
    s = _s(noProxyUrls=["https://intranet.nd.gov"],
           proxyExcludedUrls="https://ndgishub.nd.gov|http://localhost")
    assert NP.proxy_for("https://intranet.nd.gov/map", s) == NP.NO_PROXY
    assert NP.proxy_for("https://ndgishub.nd.gov/arcgis", s) is None
    assert NP.proxy_for("http://localhost:8000/", s) is None
    assert NP.proxy_for("https://elevation.nationalmap.gov/", s) == "http://proxy.nd.gov:8080"
    # what a program started from the plugin, and a GDAL /vsicurl/ read, are given
    env = NP.env_for("https://conda.anaconda.org/", {"PATH": "x"}, _s(proxyUser="nick"))
    assert env["HTTPS_PROXY"] == env["http_proxy"] == "http://nick@proxy.nd.gov:8080"
    assert NP.env_for("https://conda.anaconda.org/", {}, {}) == {}
    assert NP.gdal_options("https://a.gov/x", _s(proxyUser="nick", proxyPassword="p@ss")) == \
        {"GDAL_HTTP_PROXY": "proxy.nd.gov:8080", "GDAL_HTTP_PROXYUSERPWD": "nick:p@ss"}
    assert NP.gdal_options("https://a.gov/x", {}) == {}


@test
def a_download_goes_through_the_proxy_set_in_qgis():
    from ND_Dam_Hazard_Helper.damsuite import netproxy as NP
    seen = []

    class Proxy(http.server.BaseHTTPRequestHandler):
        def do_GET(self):                                   # noqa: N802
            seen.append((self.requestline, self.headers.get("Proxy-Authorization")))
            body = b"via proxy"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), Proxy)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        s = _s(proxyHost="127.0.0.1", proxyPort=str(srv.server_port), proxyUser="nick",
               proxyPassword="s3cret")
        req = urllib.request.Request("http://example.invalid/pack.json")
        with NP.urlopen(req, timeout=10, settings=s) as r:
            assert r.read() == b"via proxy"
        line, auth = seen[-1]
        assert line == "GET http://example.invalid/pack.json HTTP/1.1", line
        assert auth == "Basic " + base64.b64encode(b"nick:s3cret").decode()
        with NP.urlopen("http://example.invalid/b", timeout=10, settings=s) as r:  # a bare URL
            assert r.read() == b"via proxy"
        # the services module's own reader goes the same way
        from ND_Dam_Hazard_Helper.damsuite import services as SV
        real = NP.qgis_settings
        NP.qgis_settings = lambda: s
        try:
            assert SV.urllib_get("http://example.invalid/caps") == b"via proxy"
        finally:
            NP.qgis_settings = real
        assert seen[-1][0] == "GET http://example.invalid/caps HTTP/1.1"
    finally:
        srv.shutdown()
        srv.server_close()


@test
def the_pack_builder_downloads_through_netproxy():
    from ND_Dam_Hazard_Helper import build_nd_pack as B
    from ND_Dam_Hazard_Helper.damsuite import netproxy as NP
    assert B.urlopen is NP.urlopen
