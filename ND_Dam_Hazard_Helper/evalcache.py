# -*- coding: utf-8 -*-
"""The evaluated state of a run (stage A: downloads, inventory, raster
sampling, roads / rail / facilities) kept on disk, so that the next QGIS
session can re-use it as the same session does (pipeline._EVAL_CACHE).

The file is outputs/QA/_evaluation_cache.bin. It holds a JSON header line
(what the evaluation was made from, plugin version, time) and the state,
compressed. The state is written with pickle, but read back with a
restricted reader that accepts only plain data (numbers, text, lists,
dicts, sets, numpy arrays) and the few QGIS objects the state contains
(geometries, rectangles, coordinate systems), which are stored as WKB / text.
Nothing else can be loaded from it. Anything the writer does not know
makes it refuse to write - the run is never affected, the session cache
simply is not kept on disk.

An evaluation is used again only when its key (pipeline.evaluation_key -
the raster contents, the chosen layers, the download choices and the
settings that shape the sampling), the plugin version and the age fit. Old
data is not trusted forever: downloads (NSI, roads ...) change, so the
default age limit is MAX_AGE_DAYS.
"""

import io
import json
import os
import pickle
import time
import zlib

import numpy as np

MAGIC = b"NDEVAL1\n"
FILE_NAME = "_evaluation_cache.bin"
FORMAT = 1
MAX_AGE_DAYS = 7.0

_PLAIN = (type(None), bool, int, float, str, bytes, list, tuple, dict, set, frozenset,
          complex, bytearray)
_NUMPY_NAMES = {"_reconstruct", "scalar", "ndarray", "dtype", "_frombuffer"}
_NUMPY_MODULES = {"numpy", "numpy.core.multiarray", "numpy._core.multiarray",
                  "numpy.core.numeric", "numpy._core.numeric"}


class NotSaved(Exception):
    """The state holds something this module does not write."""


def cache_path(out_folder):
    from .constants import SUBDIR_QA
    return os.path.join(out_folder, SUBDIR_QA, FILE_NAME)


# ----------------------------------------------------------- QGIS objects --
def _crs_token(crs):
    auth = crs.authid() if crs.isValid() else ""
    if auth:
        return ("crs", "auth", auth)
    return ("crs", "wkt", crs.toWkt() if crs.isValid() else "")


def _crs_from(token):
    from qgis.core import QgsCoordinateReferenceSystem
    if token[1] == "auth" or not token[2]:
        return QgsCoordinateReferenceSystem(token[2])
    crs = QgsCoordinateReferenceSystem()
    for name in ("createFromWkt", "createFromString"):
        fn = getattr(crs, name, None)
        if fn is None:
            continue
        try:
            fn(token[2]) if name == "createFromWkt" else fn("WKT:" + token[2])
        except Exception:
            continue
        if crs.isValid():
            return crs
    return QgsCoordinateReferenceSystem("WKT:" + token[2])


def _geom_from(data):
    from .helpers import geometry_from_wkb
    return geometry_from_wkb(data)


class _Pickler(pickle.Pickler):
    def __init__(self, fh):
        pickle.Pickler.__init__(self, fh, protocol=4)

    def persistent_id(self, obj):
        t = type(obj)
        if t in _PLAIN or isinstance(obj, (np.generic, np.dtype)) or t is np.ndarray:
            if t is np.ndarray and obj.dtype.hasobject:
                raise NotSaved("array of objects")
            return None
        # what pickle itself refers to when it writes a numpy array / scalar
        if getattr(obj, "__module__", None) in _NUMPY_MODULES and \
                getattr(obj, "__name__", None) in _NUMPY_NAMES:
            return None
        from qgis.core import (QgsGeometry, QgsRectangle, QgsPointXY,
                               QgsCoordinateReferenceSystem)
        from .scenarios import Scenario
        if isinstance(obj, QgsGeometry):
            return ("geom", bytes(obj.asWkb()) if not obj.isNull() else b"")
        if isinstance(obj, QgsRectangle):
            return ("rect", obj.xMinimum(), obj.yMinimum(), obj.xMaximum(), obj.yMaximum())
        if isinstance(obj, QgsPointXY):
            return ("pt", obj.x(), obj.y())
        if isinstance(obj, QgsCoordinateReferenceSystem):
            return _crs_token(obj)
        if isinstance(obj, Scenario):
            return ("scn", json.dumps(vars(obj), sort_keys=True))
        raise NotSaved("cannot keep a {}".format(t.__name__))


class _Unpickler(pickle.Unpickler):
    def __init__(self, fh):
        pickle.Unpickler.__init__(self, fh)
        self._scn = {}

    def persistent_load(self, pid):
        kind = pid[0]
        if kind == "geom":
            return _geom_from(pid[1])
        if kind == "rect":
            from qgis.core import QgsRectangle
            return QgsRectangle(*pid[1:5])
        if kind == "pt":
            from qgis.core import QgsPointXY
            return QgsPointXY(pid[1], pid[2])
        if kind == "crs":
            return _crs_from(pid)
        if kind == "scn":
            # one object per scenario, as in the run that made it (results
            # and the scenario list share it)
            if pid[1] not in self._scn:
                from .scenarios import Scenario
                sc = Scenario.__new__(Scenario)
                sc.__dict__.update(json.loads(pid[1]))
                self._scn[pid[1]] = sc
            return self._scn[pid[1]]
        raise pickle.UnpicklingError("unknown object kind")

    def find_class(self, module, name):
        if module in _NUMPY_MODULES and name in _NUMPY_NAMES:
            import importlib
            return getattr(importlib.import_module(module), name)
        if module == "builtins" and name in ("set", "frozenset", "complex", "bytearray"):
            import builtins
            return getattr(builtins, name)
        raise pickle.UnpicklingError("{}.{} is not allowed in an evaluation cache".format(
            module, name))


# ------------------------------------------------------------------ files --
def save(out_folder, key, state, when, version, note=None):
    """Write the evaluation to the outputs folder. Returns (path, size) or
    (None, reason): never raises."""
    path = cache_path(out_folder)
    try:
        buf = io.BytesIO()
        _Pickler(buf).dump({"format": FORMAT, "state": state})
        blob = zlib.compress(buf.getvalue(), 1)
        head = json.dumps({"format": FORMAT, "key": key, "version": version, "when": when,
                           "saved_at": time.time(), "size": len(blob),
                           "note": note or ""}, sort_keys=True).encode("utf-8")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "wb") as fh:
            fh.write(MAGIC + head + b"\n" + blob)
        os.replace(tmp, path)
        return path, len(blob)
    except NotSaved as e:
        return None, str(e)
    except (OSError, pickle.PicklingError, TypeError, ValueError, RecursionError,
            MemoryError) as e:
        return None, "{}: {}".format(type(e).__name__, e)


def read_header(out_folder):
    """The JSON header of the saved evaluation, or None."""
    path = cache_path(out_folder)
    try:
        with open(path, "rb") as fh:
            if fh.read(len(MAGIC)) != MAGIC:
                return None
            head = json.loads(fh.readline().decode("utf-8"))
        return head if isinstance(head, dict) else None
    except (OSError, ValueError):
        return None


def load(out_folder, key, version, max_age_days=MAX_AGE_DAYS, now=None):
    """(state, when, None) of the saved evaluation when it is for `key` and
    this plugin version and not older than max_age_days; else
    (None, None, reason) - reason is "" when there is simply nothing saved."""
    path = cache_path(out_folder)
    if not os.path.isfile(path):
        return None, None, ""
    try:
        with open(path, "rb") as fh:
            if fh.read(len(MAGIC)) != MAGIC:
                return None, None, "not an evaluation cache"
            head = json.loads(fh.readline().decode("utf-8"))
            if head.get("format") != FORMAT:
                return None, None, "another file format"
            if head.get("version") != version:
                return None, None, "made by plugin version {}".format(head.get("version"))
            if head.get("key") != key:
                return None, None, "the rasters, layers or settings changed since"
            age_days = ((now if now is not None else time.time())
                        - float(head.get("saved_at") or 0)) / 86400.0
            if max_age_days is not None and age_days > max_age_days:
                return None, None, "{:.0f} days old (limit {:g})".format(age_days,
                                                                         max_age_days)
            blob = fh.read()
            if len(blob) != head.get("size"):
                return None, None, "incomplete file"
        data = _Unpickler(io.BytesIO(zlib.decompress(blob))).load()
        state = data["state"] if isinstance(data, dict) else None
        if not isinstance(state, dict):
            return None, None, "unreadable"
        return state, head.get("when") or "?", None
    except (OSError, ValueError, KeyError, zlib.error, pickle.UnpicklingError, EOFError,
            AttributeError, ImportError, IndexError, TypeError, MemoryError) as e:
        return None, None, "unreadable ({}: {})".format(type(e).__name__, e)


def discard(out_folder):
    try:
        os.remove(cache_path(out_folder))
        return True
    except OSError:
        return False


def files_exist(state):
    """Files the cached state points at that must still be there for the
    re-used evaluation to work in stage B."""
    inp = state.get("inputs") or {}
    fl = inp.get("flowlines")
    return not fl or os.path.isfile(fl)
