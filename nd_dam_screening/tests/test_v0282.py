"""0.28.2: the downloads that go through urllib use the proxy set in QGIS (damsuite.netproxy):
the 3DEP terrain tile, the LiDAR HTTPS tile, the preflight's server check, and the GDAL options
for the LiDAR tile index. A local server plays the proxy; a request for example.invalid can only
arrive through it. QGIS's settings are stood in for; the machine's QGIS profile is not touched.

Run: FAKE_QGIS_FLAVOR=3.40 PYTHONPATH=tests/qgis_mock:<parent of the plugin> python3 tests/test_v0282.py
"""
import http.server
import os
import sys
import tempfile
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(HERE, "qgis_mock"))
sys.path.insert(0, os.path.dirname(PLUGIN))

from nd_dam_screening.core import dem_download as dd, preflight as pf, terrain  # noqa: E402
from nd_dam_screening.damsuite import netproxy as NP  # noqa: E402

TIF = b"II*\x00" + b"\x00" * 60
seen = []


class Proxy(http.server.BaseHTTPRequestHandler):
    def do_GET(self):                                       # noqa: N802
        seen.append(self.requestline.split()[1])
        body = TIF
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


srv = http.server.HTTPServer(("127.0.0.1", 0), Proxy)
threading.Thread(target=srv.serve_forever, daemon=True).start()
S = {"proxyEnabled": "true", "proxyType": "HttpProxy", "proxyHost": "127.0.0.1",
     "proxyPort": str(srv.server_port), "proxyUser": "nick", "proxyPassword": "pw"}
NP.qgis_settings = lambda: S
ROOT = tempfile.mkdtemp(prefix="v0282_")

terrain.EXPORT_URL = "http://example.invalid/3dep/exportImage"
terrain.fetch_3dep_tile((0, 0, 100, 100), 10, os.path.join(ROOT, "t.tif"), retries=1, timeout=10)
assert seen[-1].startswith("http://example.invalid/3dep/exportImage?"), seen
with open(os.path.join(ROOT, "t.tif"), "rb") as f:
    assert f.read() == TIF
print("OK (the 3DEP terrain tile)")

dd._https_get("http://example.invalid/lidar/tile.zip", os.path.join(ROOT, "tile.zip"), retries=1,
              timeout=10)
assert seen[-1] == "http://example.invalid/lidar/tile.zip"
print("OK (the LiDAR HTTPS tile)")

ok, why = pf.Env.url_ok(type("E", (), {"timeout": 10})(), "http://example.invalid/caps")
assert ok and seen[-1] == "http://example.invalid/caps", (ok, why)
print("OK (the preflight's server check)")

assert NP.gdal_options(dd.INDEX_URL) == {"GDAL_HTTP_PROXY": "127.0.0.1:{}".format(srv.server_port),
                                         "GDAL_HTTP_PROXYUSERPWD": "nick:pw"}
print("OK (the GDAL options for the tile index)")
srv.shutdown()
print("ALL OK")
