# -*- coding: utf-8 -*-
"""v2.6: runs in the background.

The classification (and Download data now) run in a QgsTask so QGIS stays
usable while data are downloaded and rasters are sampled. The pipeline is
started with deferred=True: it writes its files in the worker thread and
leaves the project work (layers, map book, report, summary) to
pipeline.finish_run(), which the dialog calls on the main thread when the
task has finished. Progress and log lines cross to the main thread through
Qt signals.

Without a task manager (tests, headless scripts) start_task() runs the work
at once on the calling thread and calls the same callbacks - the dialog
code does not know the difference.
"""

import os
import traceback

from qgis.PyQt.QtCore import QObject, pyqtSignal

from .helpers import CancelledByUser, log_message, layer_file_path
from .compat import MSG_WARNING


class Bridge(QObject):
    """Signals a worker thread emits; the slots run on the main thread."""
    progressed = pyqtSignal(float, str)
    logged = pyqtSignal(str)
    done = pyqtSignal(object)


class Outcome(object):
    """What a task ended with."""

    def __init__(self):
        self.result = None
        self.error = None
        self.traceback = ""
        self.cancelled = False

    @property
    def ok(self):
        return self.error is None and not self.cancelled


def _task_class():
    try:
        from qgis.core import QgsTask
    except ImportError:
        return None

    try:
        from .compat import _first
        can_cancel = _first(lambda: QgsTask.Flag.CanCancel, lambda: QgsTask.CanCancel)
    except Exception:
        can_cancel = getattr(QgsTask, "CanCancel", 1)

    class PipelineTask(QgsTask):
        def __init__(self, description, work, bridge):
            super().__init__(description, can_cancel)
            self.work = work
            self.bridge = bridge
            self.outcome = Outcome()

        def _report_progress(self, pct, msg):
            if self.isCanceled():
                raise CancelledByUser()
            try:
                self.setProgress(max(0.0, min(100.0, float(pct))))
            except Exception:
                pass
            self.bridge.progressed.emit(float(pct), str(msg))

        def _report_log(self, msg):
            self.bridge.logged.emit(str(msg))

        def run(self):
            try:
                self.outcome.result = self.work(self._report_progress, self._report_log)
                return True
            except CancelledByUser:
                self.outcome.cancelled = True
                return False
            except Exception as e:                         # reported by the dialog
                self.outcome.error = e
                self.outcome.traceback = traceback.format_exc()
                return False

        def finished(self, result):
            # a Cancel that came after the last progress step changes nothing:
            # the work is complete and its files are written
            self.bridge.done.emit(self.outcome)

    return PipelineTask


def task_manager():
    try:
        from qgis.core import QgsApplication
        return QgsApplication.taskManager()
    except Exception:
        return None


def start_task(description, work, on_progress, on_log, on_done):
    """Run work(progress, log) in a QgsTask; on_progress(pct, msg),
    on_log(msg) and on_done(Outcome) are called on the main thread. Returns
    the task (cancel() stops it at the next progress call), or None when the
    work ran at once because no task manager is available (on_done was then
    already called)."""
    cls = _task_class()
    mgr = task_manager()
    if cls is None or mgr is None:
        out = Outcome()

        def progress(pct, msg):
            on_progress(float(pct), str(msg))

        try:
            out.result = work(progress, on_log)
        except CancelledByUser:
            out.cancelled = True
        except Exception as e:
            out.error = e
            out.traceback = traceback.format_exc()
        on_done(out)
        return None
    bridge = Bridge()
    bridge.progressed.connect(on_progress)
    bridge.logged.connect(on_log)
    bridge.done.connect(on_done)
    task = cls(description, work, bridge)
    task._bridge_ref = bridge                 # keep the QObject alive with the task
    mgr.addTask(task)
    return task


# --------------------------------------------------------- main-thread prep --
def detach_layers(cfg):
    """A copy of the configuration whose project layers are replaced by
    clones, so the worker thread never reads a layer the user could edit or
    remove from the project meanwhile. Layers without clone() are kept."""
    from .pipeline import _EVAL_LAYERS

    done = {}                                 # one clone per layer (road = AADT layer)

    def clone(lyr):
        if lyr is None:
            return None
        key = id(lyr)
        if key in done:
            return done[key]
        c = None
        try:
            # unsaved edits live in the edit buffer, which a clone does not
            # share: such a layer is copied feature by feature instead
            edited = bool(getattr(lyr, "isEditable", lambda: False)() and
                          getattr(lyr, "isModified", lambda: False)())
        except Exception:
            edited = False
        order = ("materialize", "clone") if edited else ("clone", "materialize")
        for meth in order:
            fn = getattr(lyr, meth, None)
            if fn is None:
                continue
            try:
                c = fn(_all_request()) if meth == "materialize" else fn()
                if c is not None and c.isValid():
                    try:
                        c.setName(lyr.name())
                    except Exception:
                        pass
                    break
                c = None
            except Exception as e:
                log_message("Layer '{}' not copied for the background run ({}); read in "
                            "place".format(lyr.name(), e))
                c = None
        if edited and c is not None and order[0] == "materialize" and \
                getattr(c, "featureCount", lambda: -1)() != getattr(lyr, "featureCount",
                                                                    lambda: -2)():
            log_message("Layer '{}': unsaved edits may not be in the copy the run reads - "
                        "save the layer first".format(lyr.name()), MSG_WARNING)
        done[key] = c if c is not None else lyr
        return done[key]
    out = dict(cfg)
    for key in _EVAL_LAYERS + ("parcel_layer", "pop_source"):
        if out.get(key) is not None:
            out[key] = clone(out[key])
    if out.get("osm_layer") not in (None, "auto"):
        out["osm_layer"] = clone(out["osm_layer"])
    if isinstance(out.get("point_sources"), (list, tuple)):
        out["point_sources"] = [(fam, clone(lyr)) for fam, lyr in out["point_sources"]]
    return out


def _all_request():
    from qgis.core import QgsFeatureRequest
    return QgsFeatureRequest()


# result files the classification writes (also the flat layout before 2.3)
_RESULT_BASES = ("habitable_structures", "roads_inundated", "rail_inundated", "access_cut_off",
                 "dams_in_series", "lifeline_facilities", "bridges_culverts_crossings",
                 "model_boundary_check", "eap_", "structure_sample_points",
                 "structure_evaluation_areas", "structure_sampled_cells",
                 "future_development_parcels", "hazard_foundations", "hazard_mobilehomes",
                 "hazard_vehicles", "inventory_qa")


def release_outputs(output_folder, data_sets=False):
    """Remove from the project the plugin's result layers under
    output_folder (the run rewrites those files; a loaded layer would keep
    them locked on Windows): the files of the Classification / EAP / QA /
    Consolidation / ACER_* folders and result files left in outputs itself.
    With data_sets, the saved data sets in Data too (Download data now).
    Imports (HECRAS_* / DSSWISE_*), downloads, the outline file and the
    user's own layers stay. Main thread only. Returns the number removed."""
    from qgis.core import QgsProject
    from .constants import (SUBDIR_DATA, SUBDIR_CLASSIFICATION, SUBDIR_EAP, SUBDIR_QA,
                            SUBDIR_CONSOLIDATION)
    if not output_folder:
        return 0
    root = os.path.normcase(os.path.realpath(output_folder))
    result_dirs = {d.lower() for d in (SUBDIR_CLASSIFICATION, SUBDIR_EAP, SUBDIR_QA,
                                       SUBDIR_CONSOLIDATION)}
    ids = []
    for lid, lyr in QgsProject.instance().mapLayers().items():
        p = layer_file_path(lyr)
        if not p:
            continue
        rp = os.path.normcase(os.path.realpath(p))
        if not rp.startswith(root + os.sep):
            continue
        parts = rp[len(root) + 1:].split(os.sep)
        first = parts[0].lower()
        base = os.path.basename(rp).lower()
        if len(parts) > 1 and (first in result_dirs or first.startswith("acer_")):
            ids.append(lid)
        elif len(parts) > 1 and data_sets and first == SUBDIR_DATA.lower():
            ids.append(lid)
        elif len(parts) == 1 and base.startswith(_RESULT_BASES):
            ids.append(lid)
    if ids:
        try:
            QgsProject.instance().removeMapLayers(ids)
        except Exception as e:
            log_message("Result layers not released before the run: {}".format(e), MSG_WARNING)
            return 0
    return len(ids)
