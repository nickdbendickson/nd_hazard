"""Standard-library HTTP client. Used by the tests, and by the plugin only if the
QGIS network client is unavailable."""

import socket
import urllib.error
import urllib.request

from .core import NetworkError, UpdateError

USER_AGENT = "ND-Data-Updater-QGIS/1.0"
TIMEOUT = 60
CHUNK = 1024 * 1024


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
            return urllib.request.urlopen(req, timeout=TIMEOUT)
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

    def download(self, url, dest, feedback):
        resp = self._open(url, "GET")
        try:
            with resp, open(dest, "wb") as out:
                total = int(resp.headers.get("Content-Length") or 0)
                done = 0
                while True:
                    feedback.check_cancelled()
                    chunk = resp.read(CHUNK)
                    if not chunk:
                        break
                    out.write(chunk)
                    done += len(chunk)
                    if total:
                        feedback.fraction(done / total)
            if total and done != total:
                raise NetworkError(f"download interrupted ({done} of {total} bytes)")
        except (socket.timeout, ConnectionError) as e:
            _raise_for(e, url)
