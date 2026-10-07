"""Plain words for the usual per-dam failures (0.13). The tools keep the batch going when one dam
fails; the note they write should say what to change, not what the code saw. `plain(e)` maps the
common exception kinds to that sentence (the original text stays at the end for the log)."""
import re

_FIELD = re.compile(r"^'?([A-Za-z_][A-Za-z0-9_]*)'?$")


def plain(e, dam_id=None, step=None):
    """One sentence for an exception caught in a per-dam loop."""
    kind = type(e).__name__
    text = str(e).strip()
    who = "dam {}".format(dam_id) if dam_id not in (None, "") else "this dam"
    if isinstance(e, FileNotFoundError) or kind in ("FileNotFoundError",):
        name = getattr(e, "filename", None) or text
        if "curve" in str(name).lower() or "stage-storage" in text.lower():
            return "no stage-storage curve for {}: run step 3 (or 16) again, or check it did not skip this dam".format(who)
        if "dam_" in str(name) and str(name).lower().endswith((".tif", ".tiff")):
            return "no terrain clip for {}: run step 2 (or 16) again, or point the step at the terrain folder".format(who)
        if "hydrograph" in text.lower() or ".csv" in str(name).lower():
            return "no breach hydrograph for {}: run step 5 again".format(who)
        return "a file {} needs is missing ({}): run the earlier step again".format(who, name)
    if isinstance(e, KeyError):
        m = _FIELD.match(text)
        if m:
            return "the layer has no field '{}': pick the right field in the tool's dialog (Inputs tab)".format(m.group(1))
        return "a value {} needs is missing ({})".format(who, text)
    if isinstance(e, MemoryError):
        return "out of memory for {}: use a coarser cell size, a narrower corridor or fewer dams at a time".format(who)
    if isinstance(e, ZeroDivisionError):
        return "a zero in the dam record of {} (height, storage or width): check the inventory values".format(who)
    if kind in ("URLError", "HTTPError", "timeout", "TimeoutError", "ConnectionError", "RemoteDisconnected"):
        return "no answer from the USGS 3DEP service for {}: check the internet connection or switch to a local " \
               "DEM folder ({})".format(who, text)
    if isinstance(e, (ValueError,)) and ("could not convert" in text or "invalid literal" in text):
        return "a value of {} is not a number ({}): check the inventory fields and units".format(who, text)
    if isinstance(e, LookupError) and text:
        return "{}: {}".format(who, text)
    if isinstance(e, (RuntimeError, OSError)) and "gdal" in text.lower():
        return "GDAL could not read or write a raster for {}: {}".format(who, text)
    if not text:
        return "{} failed ({}) - see the Processing log".format(who, kind)
    return text if text[0].isupper() or ":" in text[:40] else "{}: {}".format(who, text)
