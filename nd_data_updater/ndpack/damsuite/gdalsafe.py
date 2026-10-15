"""GDAL calls that behave the same whether GDAL's Python exceptions are switched on or off.

`gdal.UseExceptions()` is process-wide in the GDAL bindings QGIS ships: a plugin that calls it
at import time changes GDAL for QGIS and every other plugin (code written for the default mode
checks `if ds is None` and then meets an exception instead). The three suite plugins therefore
never change the mode (damsuite 1.3.0); they call GDAL through these helpers, which raise
RuntimeError - the exception type GDAL's exception mode raises - with GDAL's own message when
GDAL returns nothing, so `except RuntimeError` handlers work in both modes.

osgeo is imported only when a helper is called (damsuite itself stays importable without GDAL).
"""


def last_error(gdal=None):
    """GDAL's last error message, '' when there is none (never raises)."""
    try:
        if gdal is None:
            from osgeo import gdal
        return (gdal.GetLastErrorMsg() or "").strip()
    except Exception:                                            # noqa
        return ""


def checked(result, what, gdal=None):
    """`result` of a GDAL call (Open, Warp, Translate, BuildVRT, Create, ...); RuntimeError
    "<what> failed: <GDAL message>" when it is None."""
    if result is None:
        msg = last_error(gdal)
        raise RuntimeError("{} failed{}".format(what, ": " + msg if msg else ""))
    return result


def open_raster(path, update=False):
    """gdal.Open(path) (read-only, or update), or RuntimeError naming the file."""
    from osgeo import gdal
    mode = gdal.GA_Update if update else gdal.GA_ReadOnly
    try:
        ds = gdal.Open(path, mode)
    except RuntimeError as e:                    # exception mode: add the file name
        raise RuntimeError("GDAL cannot open {}: {}".format(path, e)) from None
    return checked(ds, "GDAL cannot open {} - opening".format(path), gdal)


def open_vector(path, update=False):
    """ogr.Open(path), or RuntimeError naming the file."""
    from osgeo import ogr
    try:
        ds = ogr.Open(path, 1 if update else 0)
    except RuntimeError as e:
        raise RuntimeError("OGR cannot open {}: {}".format(path, e)) from None
    if ds is None:
        raise RuntimeError("OGR cannot open {}{}".format(path, ": " + last_error() if last_error() else ""))
    return ds
