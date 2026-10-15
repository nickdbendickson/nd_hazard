"""1.4.2: the updater's own downloads (the urllib client, the pack source, the service check)
use the proxy set in QGIS, through damsuite.netproxy. A local server plays the proxy; a request
for example.invalid can only arrive through it. QGIS's settings are stood in for, so the test
never reads or writes the QGIS profile of the machine it runs on."""
import http.server
import os
import sys
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(HERE)
if os.path.dirname(PLUGIN) not in sys.path:
    sys.path.insert(0, os.path.dirname(PLUGIN))
from nd_data_updater.tests.registry import test  # noqa: E402


@test
def test_updater_downloads_use_the_proxy_set_in_qgis():
    from nd_data_updater import http_urllib, packsource, servicecheck
    from nd_data_updater.ndpack.damsuite import netproxy as NP
    seen = []

    class Proxy(http.server.BaseHTTPRequestHandler):
        def do_GET(self):                                   # noqa: N802
            seen.append(self.requestline)
            body = b'{"ok": true}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), Proxy)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    real = NP.qgis_settings
    NP.qgis_settings = lambda: {"proxyEnabled": "true", "proxyType": "HttpProxy",
                                "proxyHost": "127.0.0.1", "proxyPort": str(srv.server_port)}
    try:
        with http_urllib.UrllibHttp()._open("http://example.invalid/a") as r:
            assert r.read() == b'{"ok": true}'
        assert packsource.get_json("http://example.invalid/b") == {"ok": True}
        with servicecheck._default_opener("http://example.invalid/c", 10) as r:
            r.read()
        assert [s.split()[1] for s in seen] == ["http://example.invalid/a",
                                                "http://example.invalid/b",
                                                "http://example.invalid/c"], seen
        assert packsource.load_builder().urlopen is NP.urlopen       # the vendored pack builder
    finally:
        NP.qgis_settings = real
        srv.shutdown()
        srv.server_close()
