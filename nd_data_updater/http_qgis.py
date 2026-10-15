"""HTTP client that goes through QGIS's own network stack.

Using QgsBlockingNetworkRequest means downloads honour the proxy, SSL and
authentication settings in QGIS > Settings > Options > Network, and use the
macOS certificate store, which avoids the certificate errors plain Python
downloads often hit in the QGIS macOS bundle. It is safe to call from a
QgsTask worker thread.
"""

from qgis.core import QgsBlockingNetworkRequest
from qgis.PyQt.QtCore import QUrl
from qgis.PyQt.QtNetwork import QNetworkRequest

from .qtcompat import ALWAYS_NETWORK, BLOCKING_NO_ERROR, CACHE_LOAD_CONTROL, HTTP_STATUS

from .core import Cancelled, NetworkError, UpdateError

USER_AGENT = b"ND-Data-Updater-QGIS/1.0"
# HTTP statuses that mean "the link is wrong", as opposed to "can't connect right now".
PERMANENT_STATUSES = {400, 401, 403, 404, 405, 410}


class QgisHttp:
    def __init__(self, qgs_feedback=None):
        # QgsFeedback shared with the task, so Cancel aborts an in-progress download.
        self.qgs_feedback = qgs_feedback

    def _request(self, url, headers=None):
        req = QNetworkRequest(QUrl(url))
        req.setRawHeader(b"User-Agent", USER_AGENT)
        for k, v in (headers or {}).items():
            req.setRawHeader(k.encode(), v.encode())
        req.setAttribute(CACHE_LOAD_CONTROL, ALWAYS_NETWORK)
        return req

    def _check(self, blocking, err, url):
        if self.qgs_feedback is not None and self.qgs_feedback.isCanceled():
            raise Cancelled()
        if err == BLOCKING_NO_ERROR:
            return blocking.reply()
        status = blocking.reply().attribute(HTTP_STATUS)
        try:
            status = int(status)
        except (TypeError, ValueError):
            status = None
        message = blocking.errorMessage() or "network error"
        if status in PERMANENT_STATUSES:
            raise UpdateError(f"HTTP {status} for {url} (the link may have changed)")
        raise NetworkError(f"{message} ({url})")

    @staticmethod
    def _headers(reply):
        return {bytes(name).decode("latin-1").lower(): bytes(reply.rawHeader(name)).decode("latin-1")
                for name in reply.rawHeaderList()}

    def head(self, url):
        blocking = QgsBlockingNetworkRequest()
        try:
            if not hasattr(blocking, "head"):
                raise UpdateError("HTTP 405")
            err = blocking.head(self._request(url), True, self.qgs_feedback)
            return self._headers(self._check(blocking, err, url))
        except UpdateError as e:
            if "HTTP 405" not in str(e) and "HTTP 403" not in str(e):
                raise
        # Server refuses HEAD: ask for a single byte instead.
        blocking = QgsBlockingNetworkRequest()
        err = blocking.get(self._request(url, {"Range": "bytes=0-0"}), True, self.qgs_feedback)
        return self._headers(self._check(blocking, err, url))

    def get_bytes(self, url, headers=None):
        blocking = QgsBlockingNetworkRequest()
        err = blocking.get(self._request(url, headers), True, self.qgs_feedback)
        return bytes(self._check(blocking, err, url).content())

    def download(self, url, dest, feedback):
        blocking = QgsBlockingNetworkRequest()

        def progress(received, total):
            if total and total > 0:
                feedback.fraction(received / total)

        blocking.downloadProgress.connect(progress)
        err = blocking.get(self._request(url), True, self.qgs_feedback)
        reply = self._check(blocking, err, url)
        content = reply.content()
        expected = self._headers(reply).get("content-length")
        if expected and expected.isdigit() and int(expected) != content.size():
            raise NetworkError(f"download interrupted ({content.size()} of {expected} bytes)")
        with open(dest, "wb") as out:
            out.write(content.data())


def gdal_merger():
    """Return a function that merges GeoJSON files into one GeoPackage layer, or
    None if GDAL's Python bindings are not available."""
    try:
        from osgeo import gdal
    except ImportError:
        return None

    def merge(paths, dest):
        gdal.UseExceptions()
        for i, path in enumerate(paths):
            kwargs = dict(format="GPKG", layerName="nsi_structures", dstSRS="EPSG:4326",
                          accessMode=None if i == 0 else "append")
            try:
                options = gdal.VectorTranslateOptions(addFields=i > 0, **kwargs)
            except TypeError:  # very old GDAL without addFields
                options = gdal.VectorTranslateOptions(**kwargs)
            ds = gdal.VectorTranslate(dest, path, options=options)
            if ds is None:
                raise UpdateError(f"Could not add {path} to {dest}")
            ds = None  # close and flush

    return merge
