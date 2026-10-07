"""Guard against intermittently INVALID coordinate transformations (QGIS 3.34, found on live data).

QgsCoordinateTransform can come back invalid for two CRSs that are fine (seen in QGIS 3.34.4 / PROJ 9.4
after several threads created transformations at once, and again later in the same process, with no
error message). An invalid transform reports isShortCircuited() == True, which the plugins read as "same
CRS": coordinates are then passed through unchanged, a layer's filter rectangle is left in the wrong
CRS, and every structure, road or receptor falls "outside the study area" or is not read at all. The
dam was then classified Low from nothing.

`install()` replaces qgis.core.QgsCoordinateTransform (before any plugin module imports it) with a
factory that re-creates a transform that is invalid although both CRSs are valid and differ: it clears
QGIS's transform cache, rebuilds both CRSs from their identifiers in the calling thread and tries again
(up to RETRIES times). Valid transforms, same-CRS transforms and
calls with other arguments are returned untouched; the static methods (invalidateCache, ...) are kept.
Idempotent. Never raises. `repairs()` is the number of transforms repaired so far (for the run log).
"""
import threading
import time

RETRIES = 6
_state = {"installed": False, "repaired": 0, "failed": 0}
_lock = threading.Lock()


def repairs():
    return _state["repaired"], _state["failed"]


def _rebuild(Crs, crs):
    try:
        auth = crs.authid()
        new = Crs(auth) if auth else Crs.fromWkt(crs.toWkt())
        return new if new.isValid() else crs
    except Exception:
        return crs


def install():
    if _state["installed"]:
        return True
    try:
        import qgis.core as core
        orig = core.QgsCoordinateTransform
        if getattr(orig, "_nd_guarded", False):
            _state["installed"] = True
            return True
        Crs = core.QgsCoordinateReferenceSystem

        def guarded(*args, **kwargs):
            t = orig(*args, **kwargs)
            try:
                if (len(args) >= 2 and isinstance(args[0], Crs) and isinstance(args[1], Crs)
                        and not t.isValid() and args[0].isValid() and args[1].isValid()
                        and args[0] != args[1]):
                    ok = False
                    for i in range(RETRIES):
                        with _lock:
                            orig.invalidateCache()
                        time.sleep(0.02 * (i + 1))
                        # a CRS object made in a thread that has since ended (a layer from a
                        # download worker) cannot make a valid transform: rebuild both from their
                        # identifiers in this thread
                        t = orig(_rebuild(Crs, args[0]), _rebuild(Crs, args[1]), *args[2:], **kwargs)
                        if t.isValid():
                            ok = True
                            break
                    with _lock:
                        _state["repaired" if ok else "failed"] += 1
            except Exception:
                pass
            return t

        for name in dir(orig):
            if not name.startswith("__") and not hasattr(guarded, name):
                try:
                    setattr(guarded, name, getattr(orig, name))
                except Exception:
                    pass
        guarded._nd_guarded = True
        guarded._nd_original = orig
        core.QgsCoordinateTransform = guarded
        _state["installed"] = True
        return True
    except Exception:
        return False
