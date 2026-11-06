"""Standard-library HTTP client. Used by the tests, and by the plugin only if the
QGIS network client is unavailable. QGIS's proxy setting applies (suite 1.18.8, as in the ND
Data Updater 1.4.2)."""

import socket
import urllib.error
import urllib.request

from .core import NetworkError, UpdateError
from .ndpack.damsuite import netproxy

USER_AGENT = "ND-Data-Updater-QGIS/1.0"
TIMEOUT = 60
CHUNK = 1024 * 1024
MAX_BYTES = 2 * 1024 * 1024 * 1024   # refuse a download larger than 2 GB


def _raise_for(e, url):
    if isinstance(e, urllib.error.HTTPError):
        if e.code >= 500 or e.code == 429:
            raise NetworkError(f"server error {e.code} for {url}") from e
        raise UpdateError(f"HTTP {e.code} for {url} (the link may have changed)") from e
    raise NetworkError(f"{getattr(e, 'reason', e)} ({url})") from e


class UrllibHttp:
    def _open(self, url, method="GET", headers=None):
        req = urllib.request.Request(url, method=method,
                                     headers={"User-Agent": USER_AGENT, **(headers or {})})
        try:
            return netproxy.urlopen(req, timeout=TIMEOUT)
        except (urllib.error.URLError, socket.timeout, ConnectionError, OSError) as e:
            _raise_for(e, url)

    def head(self, url):
        try:
            resp = self._open(url, "HEAD")
        except UpdateError as e:
            # Some servers refuse HEAD; ask for one byte instead.
            if "HTTP 405" not in str(e) and "HTTP 403" not in str(e):
                raise
            resp = self._open(url, "GET", {"Range": "bytes=0-0"})
        with resp:
            return {k.lower(): v for k, v in resp.headers.items()}

    def get_bytes(self, url, headers=None):
        resp = self._open(url, "GET", headers)
        try:
            with resp:
                return resp.read()
        except (socket.timeout, ConnectionError, OSError) as e:
            _raise_for(e, url)

    def get_json(self, url, params=None):
        import json
        import urllib.parse
        if params:
            url = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        raw = self.get_bytes(url)
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        return json.loads(raw)

    def download(self, url, dest, feedback):
        resp = self._open(url, "GET")
        try:
            with resp, open(dest, "wb") as out:
                total = int(resp.headers.get("Content-Length") or 0)
                if total > MAX_BYTES:
                    raise UpdateError(f"download is {total} bytes, over the {MAX_BYTES} byte limit")
                done = 0
                while True:
                    feedback.check_cancelled()
                    chunk = resp.read(CHUNK)
                    if not chunk:
                        break
                    out.write(chunk)
                    done += len(chunk)
                    if done > MAX_BYTES:
                        raise UpdateError(f"download passed the {MAX_BYTES} byte limit")
                    if total:
                        feedback.fraction(done / total)
            if total and done != total:
                raise NetworkError(f"download interrupted ({done} of {total} bytes)")
        except (socket.timeout, ConnectionError) as e:
            _raise_for(e, url)
