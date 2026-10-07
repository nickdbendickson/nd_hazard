# -*- coding: utf-8 -*-
"""Hazard-creep watch: re-classify every dam of a folder against fresh data
(NSI, address points, building outlines, roads ...) and report what changed
since each dam's previous run.

A hazard classification is only as current as the data behind it: houses
built below a dam, a new subdivision, a re-routed road or a changed NSI
population can move a dam into a higher class years after the last study
(hazard creep). The watch re-runs each dam with the SAME flood rasters but
new downloads, compares the result with the dam's previous run (the same
comparison as QA/Changes_Since_Last_Run.csv) and lists, per dam, whether the
class changed and what caused it. Dams with an alert come first.

It can run from the Processing toolbox / the Hazard Classification tab, or on
a schedule with qgis_process (see the README). It never changes a dam's
classification decision - it produces the list of dams to look at.

Two run modes (v2.8; v2.9: shared with the batch classification):
  sequential  every dam is classified one after another in this QGIS session
              (processing_provider.batch_classify) - the default, and what runs
              when nothing else is possible;
  parallel    `workers` > 1: every dam is a separate `qgis_process run
              ndhazard:classify` child process, at most `workers` at a time. A
              child works on its own dam folder only and writes the same files
              as a sequential run (outputs/QA/Last_Run_Snapshot.json,
              Changes_Since_Last_Run.csv ...); the watch reads its rows from
              those files, never from the child's text output. The fresh-data
              settings travel in a small JSON file (CONFIG_JSON parameter of
              the classify algorithm, see processing_provider.py) and the
              offline data pack choice in environment variables. A per-dam
              timeout ends a stuck child; a failed or timed-out dam is a FAILED
              row with the tail of the child's error output; Cancel terminates
              all children. When qgis_process cannot be found (or this is no
              real QGIS) the watch falls back to the sequential mode and says
              so in the log and in the report.

v2.9: the watch IS a batch classification (processing_provider.batch_classify,
in either mode) followed by the comparison of each dam's snapshots, so both
modes also write Batch_Hazard_Classification.csv. The worker machinery below
(plan_mode, run_workers, find_qgis_process ...) is used by both. A dam whose
run finished without writing a NEW Last_Run_Snapshot.json is a FAILED row
("not compared"), never "no change" read from the old snapshot.
"""

import csv
import html
import os
import shutil
import sys
import tempfile
import time

from . import history as HIS
from .constants import OUTPUT_FOLDER_NAME, SUBDIR_QA, PLUGIN_NAME, PLUGIN_VERSION

WATCH_FIELDS = ["Alert", "Dam", "Status", "ND_Before", "ND_Now", "TM11_Before", "TM11_Now",
                "LIJ_Before", "LIJ_Now", "Flooded_Habitable_Before", "Flooded_Habitable_Now",
                "Newly_Flooded", "No_Longer_Flooded", "Category_Changes", "Roads_Newly_Overtopped",
                "Lifelines_Newly_Inundated", "Compared_With", "Message", "Folder"]
CSV_LATEST = "Hazard_Creep_Watch.csv"
_ND_RANK = {"Low": 1, "Medium": 2, "High": 3}
_DRY = ("", None, "Not inundated")

ALERT_CLASS_UP = "CLASS UP"
ALERT_CLASS_CHANGE = "CLASS CHANGE"
ALERT_CREEP = "CREEP"
ALERT_REVIEW = "REVIEW"
ALERT_NEW = "BASELINE"
ALERT_NONE = "-"
ALERT_FAILED = "FAILED"
DEFAULT_TIMEOUT_S = 3600            # per dam, parallel mode (0 = no limit)
MAX_WORKERS = 16
ENV_QGIS_PROCESS = "ND_QGIS_PROCESS"    # full path of qgis_process (or its .bat wrapper)
WATCH_WORKER_LOG = "Watch_Worker_Log.txt"   # <dam>/outputs: a failed worker's full output
BATCH_WORKER_LOG = "Batch_Worker_Log.txt"
_POLL_S = 0.25
_TAIL_LINES = 6
_TAIL_CHARS = 600
_KILL_GRACE_S = 5.0
_sleep = time.sleep                 # module level so tests can replace them
_clock = time.monotonic
_ALERT_ORDER = [ALERT_FAILED, ALERT_CLASS_UP, ALERT_CLASS_CHANGE, ALERT_CREEP, ALERT_REVIEW,
                ALERT_NEW, ALERT_NONE]

# what the change rows (history.diff) are called
_NEW, _GONE = "Newly flooded / cut off", "No longer flooded / cut off"
_CAT = ("Structure category", "Habitable")


def dam_folders(parent):
    """The dam folders directly inside `parent` (as the batch classification
    lists them)."""
    return sorted(os.path.join(parent, d) for d in os.listdir(parent)
                  if os.path.isdir(os.path.join(parent, d)) and not d.startswith(("_", "."))
                  and d.lower() != OUTPUT_FOLDER_NAME)


def qa_folder(dam_folder):
    return os.path.join(dam_folder, OUTPUT_FOLDER_NAME, SUBDIR_QA)


def snapshot_path(dam_folder):
    return os.path.join(qa_folder(dam_folder), HIS.SNAPSHOT_FILE)


def file_mark(path):
    """What identifies this version of a file (None: no file). A run that
    rewrites the file changes it: history.write_snapshot replaces the file
    (new inode / file id and time) - no clock comparison, so a network
    drive with another clock cannot make an old file look new."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size, st.st_ino, getattr(st, "st_ctime_ns", 0))


def read_before(dam_folder):
    """The dam's last-run snapshot (before the watch re-runs it), or None."""
    return HIS.read_snapshot(qa_folder(dam_folder))


def read_changes(dam_folder):
    """Change rows [(Change, Item, Before, Now)] of the run just made."""
    path = os.path.join(qa_folder(dam_folder), HIS.CHANGES_FILE)
    try:
        with open(path, newline="", encoding="utf-8-sig") as fh:
            return [(r.get("Change", ""), r.get("Item", ""), r.get("Before", ""),
                     r.get("Now", "")) for r in csv.DictReader(fh)]
    except OSError:
        return []


def flooded_habitable(snap):
    return sum(1 for s in (snap or {}).get("structures", [])
               if s.get("habitable") == "Yes" and s.get("outcome") not in _DRY)


def summarise(name, folder, before, after, changes=()):
    """One watch row from the dam's snapshots before / after the re-run and
    the change rows of that run. Pure data: testable without QGIS."""
    row = dict.fromkeys(WATCH_FIELDS, "")
    row.update({"Dam": name, "Folder": folder, "Alert": ALERT_NONE, "Status": "done",
                "ND_Now": (after or {}).get("nd") or "", "TM11_Now": (after or {}).get("tm11")
                or "", "LIJ_Now": (after or {}).get("lij", ""),
                "Flooded_Habitable_Now": flooded_habitable(after)})
    if before is None:
        row.update({"Alert": ALERT_NEW, "Message": "no earlier run to compare with - this run "
                    "is the baseline for the next watch"})
        return row
    row.update({"ND_Before": before.get("nd") or "", "TM11_Before": before.get("tm11") or "",
                "LIJ_Before": before.get("lij", ""), "Compared_With": before.get("time") or "",
                "Flooded_Habitable_Before": flooded_habitable(before)})
    n_new = sum(1 for c in changes if c[0] == _NEW)
    n_gone = sum(1 for c in changes if c[0] == _GONE)
    n_cat = sum(1 for c in changes if c[0] in _CAT)
    n_road = sum(1 for c in changes if c[0] == "Road newly overtopped")
    n_life = sum(1 for c in changes if c[0] == "Lifeline newly inundated")
    row.update({"Newly_Flooded": n_new, "No_Longer_Flooded": n_gone, "Category_Changes": n_cat,
                "Roads_Newly_Overtopped": n_road, "Lifelines_Newly_Inundated": n_life})
    nd_b, nd_a = before.get("nd"), (after or {}).get("nd")
    tm_b, tm_a = before.get("tm11"), (after or {}).get("tm11")
    why = []
    if nd_b != nd_a or tm_b != tm_a:
        up = _ND_RANK.get(nd_a, 0) > _ND_RANK.get(nd_b, 0) or \
            _ND_RANK.get(tm_a, 0) > _ND_RANK.get(tm_b, 0)
        if nd_b != nd_a:
            why.append("ND {} -> {}".format(nd_b, nd_a))
        if tm_b != tm_a:
            why.append("TM 11 {} -> {}".format(tm_b, tm_a))
        row["Alert"] = ALERT_CLASS_UP if up else ALERT_CLASS_CHANGE
    elif n_new or n_life or n_road or _lij_up(before, after):
        row["Alert"] = ALERT_CREEP
    elif n_gone or n_cat:
        row["Alert"] = ALERT_REVIEW
    if n_new:
        why.append("{} structure(s) newly flooded or cut off".format(n_new))
    if n_life:
        why.append("{} lifeline / critical facilit(ies) newly inundated".format(n_life))
    if n_road:
        why.append("{} road stretch(es) newly overtopped".format(n_road))
    if _lij_up(before, after):
        why.append("lives in jeopardy {} -> {}".format(before.get("lij"), after.get("lij")))
    if n_gone:
        why.append("{} structure(s) no longer flooded".format(n_gone))
    if n_cat:
        why.append("{} structure(s) with another habitability / category".format(n_cat))
    row["Message"] = "; ".join(why) or "no change"
    return row


def _lij_up(before, after):
    try:
        return float(after.get("lij")) > float(before.get("lij")) + 0.05
    except (TypeError, ValueError, AttributeError):
        return False


def failed_row(name, folder, status, message):
    row = dict.fromkeys(WATCH_FIELDS, "")
    row.update({"Dam": name, "Folder": folder, "Status": status, "Message": message,
                "Alert": ALERT_FAILED if status == "failed" else ALERT_NONE})
    return row


def sort_rows(rows):
    return sorted(rows, key=lambda r: (_ALERT_ORDER.index(r["Alert"]) if r["Alert"]
                                       in _ALERT_ORDER else 99, str(r["Dam"]).lower()))


def counts(rows):
    return {a: sum(1 for r in rows if r["Alert"] == a) for a in _ALERT_ORDER}


def alerts_text(rows):
    """One line: how many dams need a look."""
    c = counts(rows)
    hot = c[ALERT_CLASS_UP] + c[ALERT_CLASS_CHANGE] + c[ALERT_CREEP] + c[ALERT_REVIEW]
    return ("{} dam(s) watched: {} class change(s) ({} up), {} with creep, {} to review, {} "
            "unchanged or baseline, {} failed".format(
                len(rows), c[ALERT_CLASS_UP] + c[ALERT_CLASS_CHANGE], c[ALERT_CLASS_UP],
                c[ALERT_CREEP], c[ALERT_REVIEW], len(rows) - hot - c[ALERT_FAILED],
                c[ALERT_FAILED]))


def write_reports(parent, rows, when=None, mode=None):
    """Hazard_Creep_Watch.csv (latest), Hazard_Creep_Watch_<date>.csv and
    .html (a table, alerts first) in `parent`. Returns {"csv", "dated", "html"}.
    `mode` (text, see mode_text) is named in the HTML header; the CSV columns
    are the same in every mode."""
    from .helpers import prepare_output_path
    when = when or time.localtime()
    day = time.strftime("%Y-%m-%d", when)
    rows = sort_rows(rows)
    paths = {}
    for key, name in (("csv", CSV_LATEST),
                      ("dated", "Hazard_Creep_Watch_{}.csv".format(day))):
        path = prepare_output_path(os.path.join(parent, name))
        with open(path, "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.DictWriter(fh, fieldnames=WATCH_FIELDS)
            w.writeheader()
            w.writerows(rows)
        paths[key] = path
    hpath = prepare_output_path(os.path.join(parent, "Hazard_Creep_Watch_{}.html".format(day)))
    with open(hpath, "w", encoding="utf-8") as fh:
        fh.write(_html(rows, day, mode))
    paths["html"] = hpath
    return paths


_COLOR = {ALERT_FAILED: "#b71c1c", ALERT_CLASS_UP: "#b71c1c", ALERT_CLASS_CHANGE: "#e65100",
          ALERT_CREEP: "#f9a825", ALERT_REVIEW: "#fdd835", ALERT_NEW: "#90caf9"}


def _html(rows, day, mode=None):
    cols = ["Alert", "Dam", "ND_Before", "ND_Now", "TM11_Before", "TM11_Now", "Newly_Flooded",
            "Compared_With", "Message"]
    out = ["<!DOCTYPE html><html><head><meta charset='utf-8'><title>Hazard creep watch {}"
           "</title><style>body{{font-family:sans-serif;margin:16px}}table{{border-collapse:"
           "collapse}}td,th{{border:1px solid #bbb;padding:3px 8px;font-size:13px}}th{{"
           "background:#eee}}</style></head><body>".format(html.escape(day)),
           "<h2>Hazard creep watch - {}</h2>".format(html.escape(day)),
           "<p>{}</p>".format(html.escape(alerts_text(rows)))]
    if mode:
        out.append("<p>Run mode: {}</p>".format(html.escape(mode)))
    out += [
           "<p>Each dam was re-classified with its own flood rasters and fresh data and "
           "compared with its previous run. This is a list of dams to look at, not a "
           "classification decision. {} v{}.</p>".format(html.escape(PLUGIN_NAME),
                                                        html.escape(PLUGIN_VERSION)),
           "<table><tr>" + "".join("<th>{}</th>".format(html.escape(c)) for c in cols)
           + "</tr>"]
    for r in rows:
        style = " style='background:{}'".format(_COLOR[r["Alert"]]) if r["Alert"] in _COLOR \
            else ""
        out.append("<tr>" + "".join(
            "<td{}>{}</td>".format(style if c == "Alert" else "", html.escape(str(r.get(c, ""))))
            for c in cols) + "</tr>")
    out.append("</table></body></html>")
    return "\n".join(out)


def fresh_data_config(cfg_over=None):
    """Settings for a watch run: the flood rasters are the dam's own but
    nothing of the downloads / inventory is re-used, the comparison with the
    previous run is on, nothing is added to a project."""
    # save_evaluation off: a folder of dams would fill the disk with caches
    # v2.8: downloads come from the per-tile cache when a tile is younger than
    # data_max_age_days (7): only tiles older than that are fetched again
    over = {"reuse_evaluation": False, "save_evaluation": False, "compare_last_run": True,
            "add_to_project": False, "report_pdf": False, "eap": False,
            "data_cache_folder": "auto", "data_max_age_days": 7}
    over.update(cfg_over or {})
    over.update({"reuse_evaluation": False, "compare_last_run": True,
                 "add_to_project": False})
    return over


# ------------------------------------------------------- worker processes --
class WorkerUnavailable(Exception):
    """The first worker process could not be started (no permission, broken
    install ...): the watch / batch falls back to the sequential mode."""


def default_workers():
    """Suggested number of parallel workers: min(3, CPU count)."""
    return max(1, min(3, os.cpu_count() or 1))


def _real_qgis():
    """True in a real QGIS (the test harness' fake QGIS has no prefixPath)."""
    try:
        from qgis.core import QgsApplication
        return callable(getattr(QgsApplication, "prefixPath", None))
    except Exception:
        return False


def _qgis_process_names():
    if os.name == "nt":
        return ["qgis_process-qgis-ltr.bat", "qgis_process-qgis.bat", "qgis_process.bat",
                "qgis_process.exe"]
    return ["qgis_process"]


def _is_exe(path):
    return bool(path) and os.path.isfile(path) and os.access(path, os.X_OK)


def find_qgis_process():
    """(path, note): the qgis_process executable of this QGIS installation, or
    (None, why not). Looked for, in this order: the environment variable
    ND_QGIS_PROCESS (a full path); the QGIS install directory (QgsApplication.
    prefixPath() / pkgDataPath(): <prefix>/bin, <prefix>, and on Windows the
    OSGeo4W wrapper qgis_process-qgis[-ltr].bat two levels above apps/qgis);
    the folder of the running QGIS program; PATH. Never raises."""
    if not _real_qgis():
        return None, "this is not a real QGIS session"
    note = ""
    env = os.environ.get(ENV_QGIS_PROCESS, "").strip().strip('"')
    if env:
        if _is_exe(env):
            return env, ""
        note = "{} points to {} which is not an executable file; ".format(ENV_QGIS_PROCESS, env)
    dirs = []
    try:
        from qgis.core import QgsApplication
        for base in (QgsApplication.prefixPath(), os.path.join(
                QgsApplication.pkgDataPath(), os.pardir, os.pardir)):
            if not base:
                continue
            base = os.path.normpath(str(base))
            dirs += [os.path.join(base, "bin"), base,
                     os.path.join(base, os.pardir, os.pardir, "bin")]
    except Exception:
        pass
    exe_dir = os.path.dirname(sys.executable or "")
    if exe_dir and "qgis" in os.path.basename(sys.executable or "").lower():
        dirs.append(exe_dir)
    for d in dirs:
        for name in _qgis_process_names():
            path = os.path.normpath(os.path.join(d, name))
            if _is_exe(path):
                return path, ""
    for name in _qgis_process_names():
        path = shutil.which(name)
        if path:
            return path, ""
    return None, note + "qgis_process was not found (set {} to its full path)".format(
        ENV_QGIS_PROCESS)


def worker_command(exe, dam_folder, settings_path):
    """The command line of one dam's worker: the Processing algorithm
    ndhazard:classify with the dam folder and the JSON settings file."""
    from .processing_provider import PROVIDER_ID
    return [exe, "run", PROVIDER_ID + ":classify", "--DAM_FOLDER=" + dam_folder,
            "--CONFIG_JSON=" + settings_path]


def worker_env(base=None):
    """Environment of the workers: the offline data pack choice of this
    session (a worker may use another QGIS profile), unbuffered output."""
    env = dict(os.environ if base is None else base)
    try:
        from .download import offline_config, ENV_PACK, ENV_ONLY
        path, only = offline_config()
        env[ENV_PACK] = path or ""
        env[ENV_ONLY] = "1" if only else "0"
    except Exception:
        pass
    env.setdefault("PYTHONUNBUFFERED", "1")
    return env


def _tail(path, lines=_TAIL_LINES, chars=_TAIL_CHARS):
    """Last lines of a text file on one line ('' when unreadable)."""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - 8192))
            text = fh.read().decode("utf-8", "replace")
    except OSError:
        return ""
    got = [ln.strip() for ln in text.splitlines() if ln.strip()][-lines:]
    out = " | ".join(got)
    return out[-chars:] if len(out) > chars else out


def _terminate(proc, grace=None):
    """End a worker and whatever it started (the OSGeo4W .bat wrapper starts
    the real program as a child): terminate, then kill after `grace` s."""
    import subprocess
    grace = _KILL_GRACE_S if grace is None else grace
    real = type(proc).__module__ == "subprocess"        # never signal a test double's pid
    try:
        if real and os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
        elif real:
            import signal
            os.killpg(proc.pid, signal.SIGTERM)     # started in its own session
    except Exception:
        pass
    try:
        proc.terminate()
    except Exception:
        pass
    t0 = _clock()
    while proc.poll() is None and _clock() - t0 < grace:
        _sleep(0.05)
    if proc.poll() is None:
        try:
            if real and os.name != "nt":
                import signal
                os.killpg(proc.pid, signal.SIGKILL)
        except Exception:
            pass
        try:
            proc.kill()
        except Exception:
            pass


def _write_worker_log(folder, out_path, err_path, why, name=WATCH_WORKER_LOG,
                      what="hazard creep watch"):
    """Keep a failed worker's full output next to the dam's results."""
    try:
        target = os.path.join(folder, OUTPUT_FOLDER_NAME, name)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8") as fh:
            fh.write("{} - {} worker: {}\n\n".format(PLUGIN_NAME, what, why))
            for label, path in (("standard output", out_path), ("error output", err_path)):
                fh.write("=== {} ===\n".format(label))
                try:
                    with open(path, encoding="utf-8", errors="replace") as src:
                        fh.write(src.read())
                except OSError:
                    pass
                fh.write("\n")
        return target
    except OSError:
        return ""


def run_workers(jobs, exe, workers, timeout_s=DEFAULT_TIMEOUT_S, work_dir=None, progress=None,
                log=None, popen=None, env=None, log_name=WATCH_WORKER_LOG,
                what="hazard creep watch"):
    """Run `jobs` [{"name", "folder", "settings"[, "result_path"]}] as
    qgis_process children, at most `workers` at a time, and wait for all of
    them. Returns {folder: (ok, message)}: ok when the child exited with 0;
    otherwise the message says why (exit code, timeout, could not start) with
    the tail of its error output (the full output goes to <dam>/outputs/
    `log_name`). A job's "result_path" is handed to the child in its settings
    file (processing_provider.RESULT_KEY): the classify algorithm writes its
    result there. Cancel (CancelledByUser from `progress`) or any other
    exception terminates every child still running before it propagates.
    Raises WorkerUnavailable when not a single worker could be started.
    `popen` replaces subprocess.Popen (tests)."""
    import subprocess
    from .processing_provider import write_worker_settings, RESULT_KEY
    popen = popen or subprocess.Popen
    work_dir = work_dir or tempfile.mkdtemp(prefix="nd_watch_")
    env = env if env is not None else worker_env()
    workers = max(1, int(workers))
    pending, running, results = list(jobs), [], {}
    total, finished, started_any = len(jobs), 0, False
    kw = {"start_new_session": True} if os.name != "nt" else {
        "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)}

    def say(msg=None):
        if progress is None:
            return
        names = ", ".join(j["name"] for j in running)
        progress(100.0 * finished / max(1, total),
                 msg or "{}/{} dam(s) finished{}".format(
                     finished, total, "; running: " + names if names else ""))

    def close(job):
        for fh in (job.get("out"), job.get("err")):
            try:
                fh.close()
            except Exception:
                pass

    def end(job, ok, message):
        nonlocal finished
        close(job)
        running.remove(job)
        finished += 1
        results[job["folder"]] = (ok, message)
        secs = _clock() - job["t0"]
        if ok:
            note = "done in {:.0f} s".format(secs)
        else:
            note = "FAILED - " + message
            logfile = _write_worker_log(job["folder"], job["out_path"], job["err_path"],
                                        message, log_name, what)
            if logfile:
                note += " (full worker output: {})".format(logfile)
        if log:
            log("{} ({}/{}): {}".format(job["name"], finished, total, note))

    try:
        while pending or running:
            while pending and len(running) < workers:
                job = dict(pending.pop(0))
                idx = len(results) + len(running) + 1
                stem = os.path.join(work_dir, "dam{:03d}".format(idx))
                job.update({"settings_path": stem + ".json", "out_path": stem + ".out.txt",
                            "err_path": stem + ".err.txt"})
                job["t0"] = _clock()
                settings = job["settings"]
                if job.get("result_path"):
                    settings = dict(settings)
                    settings[RESULT_KEY] = job["result_path"]
                try:
                    write_worker_settings(job["settings_path"], settings)
                    job["out"] = open(job["out_path"], "wb")
                    job["err"] = open(job["err_path"], "wb")
                    job["proc"] = popen(worker_command(exe, job["folder"], job["settings_path"]),
                                        stdin=subprocess.DEVNULL, stdout=job["out"],
                                        stderr=job["err"], env=env, **kw)
                except OSError as e:
                    close(job)
                    if not started_any:
                        raise WorkerUnavailable("{}: {}".format(exe, e)) from e
                    running.append(job)
                    end(job, False, "the worker process could not be started: {}".format(e))
                    continue
                started_any = True
                running.append(job)
                if log:
                    log("{}: worker started ({} running)".format(job["name"], len(running)))
                say("{}: worker started".format(job["name"]))
            _sleep(_POLL_S)
            for job in list(running):
                rc = job["proc"].poll()
                if rc is None:
                    if timeout_s and _clock() - job["t0"] > timeout_s:
                        _terminate(job["proc"])
                        close(job)
                        tail = _tail(job["err_path"]) or _tail(job["out_path"])
                        end(job, False, "timed out after {:.0f} min - worker ended{}".format(
                            timeout_s / 60.0, ": " + tail if tail else ""))
                    continue
                if rc == 0:
                    end(job, True, "")
                else:
                    close(job)
                    tail = _tail(job["err_path"]) or _tail(job["out_path"])
                    end(job, False, "qgis_process ended with exit code {}: {}".format(
                        rc, tail or "no error text captured"))
            say()
    finally:
        for job in list(running):           # cancel or an error: leave nothing behind
            _terminate(job["proc"])
            close(job)
    return results


def mode_text(info):
    """One line naming how the watch / batch ran (log and report header)."""
    info = info or {}
    if info.get("mode") == "parallel":
        return "parallel - {} qgis_process worker(s) at a time ({}), {}".format(
            info.get("workers"), info.get("executable"),
            "no per-dam time limit" if not info.get("timeout_s")
            else "per-dam limit {:.0f} min".format(info["timeout_s"] / 60.0))
    if info.get("fallback_reason"):
        return "sequential in this QGIS session - {} workers were requested but {}".format(
            info.get("requested_workers"), info["fallback_reason"])
    return "sequential in this QGIS session (one dam after another)"


def clamp_workers(workers):
    """1 .. MAX_WORKERS (anything unreadable is 1)."""
    try:
        return max(1, min(MAX_WORKERS, int(workers)))
    except (TypeError, ValueError, OverflowError):
        return 1


def clean_timeout(timeout_s):
    """A per-dam limit in seconds; 0 = none (also for None, negative or
    unreadable values)."""
    try:
        t = float(timeout_s)
    except (TypeError, ValueError, OverflowError):
        return 0
    if not 0 < t < float("inf"):         # also NaN
        return 0
    return int(t) if t == int(t) else t


def _unsupported_settings(cfg_over):
    """Settings a worker process cannot be given - a parallel run would differ
    from a sequential one, so it is not used: layers and other objects; since
    v2.9 also pipeline settings a CONFIG_JSON file cannot carry (the dam's
    folder / scenarios / output folder, a wrongly typed value) when they differ
    from what the worker would use (the default)."""
    cfg_over = cfg_over or {}
    bad = {k for k, v in cfg_over.items()
           if v is not None and not isinstance(v, (bool, int, float, str))}
    try:
        from .pipeline import DEFAULTS
        from .processing_provider import worker_settings
        passed = worker_settings(cfg_over)
        for k, v in cfg_over.items():
            if k in DEFAULTS and k not in passed and v is not None and k not in bad:
                try:
                    same = v == DEFAULTS[k]
                except Exception:
                    same = False
                if not same:
                    bad.add(k)
    except Exception:           # the Processing module / pipeline not importable
        pass
    return sorted(bad)


def plan_mode(workers, cfg, timeout_s, qgis_process=None, info=None, blocked=None):
    """How a folder of dams is classified: fills (and returns) `info` with
    mode ("sequential" / "parallel"), requested_workers, workers, executable,
    fallback_reason, timeout_s and text (mode_text). Parallel needs
    `workers` > 1, the qgis_process executable (`qgis_process`, else
    find_qgis_process) and settings `cfg` a worker can receive; otherwise the
    mode is sequential and fallback_reason says why. `blocked` (text) forces
    the sequential mode for that reason."""
    workers = clamp_workers(workers)
    timeout_s = clean_timeout(timeout_s)
    state = info if info is not None else {}
    state.clear()
    state.update({"mode": "sequential", "requested_workers": workers, "workers": 1,
                  "executable": "", "fallback_reason": "", "timeout_s": timeout_s})
    if workers > 1:
        if blocked:
            exe, why = None, blocked
        else:
            exe, why = (qgis_process, "") if qgis_process else find_qgis_process()
        bad = _unsupported_settings(cfg)
        if exe and bad:
            exe, why = None, "the settings {} cannot be passed to worker processes".format(
                ", ".join(bad))
        if exe:
            state.update({"mode": "parallel", "workers": workers, "executable": exe})
        else:
            state["fallback_reason"] = why
    state["text"] = mode_text(state)
    return state


def fall_back(state, why):
    """Switch a planned parallel run to the sequential mode (reason `why`)."""
    state.update({"mode": "sequential", "workers": 1, "executable": "",
                  "fallback_reason": why})
    state["text"] = mode_text(state)
    return state


def watch_row(folder, batch_row, before, mark=None):
    """The watch row of one dam from its batch row (processing_provider.
    BATCH_FIELDS), its snapshot before the run and the file_mark of that
    snapshot before the run (None: there was none)."""
    from .helpers import dam_display_name
    name = dam_display_name(folder)
    b = batch_row
    if b is None or b.get("Status") in ("skipped", "failed"):
        return failed_row(name, folder, (b or {}).get("Status") or "skipped",
                          (b or {}).get("Message") or "not classified")
    after = HIS.read_snapshot(qa_folder(folder))
    if after is None:
        return failed_row(name, folder, "failed", "the classification finished but wrote "
                          "no Last_Run_Snapshot.json")
    if mark is not None and file_mark(snapshot_path(folder)) == mark:
        return failed_row(name, folder, "failed", "the classification finished but did not "
                          "write a new QA/Last_Run_Snapshot.json - not compared (see the "
                          "dam's run log)")
    return summarise(name, folder, before, after, read_changes(folder))


def run_watch(parent, cfg_over=None, progress=None, log=None, classify=None, workers=1,
              timeout_s=DEFAULT_TIMEOUT_S, info=None, qgis_process=None, popen=None):
    """Watch every dam folder under `parent`. Returns (rows, paths).
    `classify(parent, over, progress, log)` is processing_provider.batch_classify
    (a parameter so the watch can be tested without the Processing module; when
    given, the watch always runs sequentially).
    `workers` > 1 runs the dams as that many parallel qgis_process worker
    processes (see the module docstring) and falls back to the sequential mode
    when qgis_process is not found, this is no real QGIS, or `cfg_over` holds
    settings a child process cannot receive (layers). `timeout_s` is the limit
    per dam in that mode (0 = none). `info`, when a dict, is filled with the mode
    used: mode ("sequential" / "parallel"), requested_workers, workers, executable,
    fallback_reason, timeout_s, text. `qgis_process` (a path) skips the search
    for the executable; `popen` replaces subprocess.Popen (tests).
    Both modes also write Batch_Hazard_Classification.csv (v2.9; its path is
    paths["batch"])."""
    if not parent or not os.path.isdir(parent):
        raise ValueError("Folder not found: {}".format(parent))
    folders = dam_folders(parent)
    befores = {f: read_before(f) for f in folders}
    marks = {f: file_mark(snapshot_path(f)) for f in folders}
    state = info if info is not None else {}
    cfg = fresh_data_config(cfg_over)
    if classify is None:
        from .processing_provider import batch_classify
        batch_rows, _csv = batch_classify(
            parent, cfg, progress=progress, log=log, workers=workers, timeout_s=timeout_s,
            info=state, qgis_process=qgis_process, popen=popen, label="Creep watch",
            worker_log=WATCH_WORKER_LOG)
    else:
        plan_mode(workers, cfg, timeout_s, info=state,
                  blocked="a custom classify function is used")
        if log:
            log(">>> Creep watch mode: " + state["text"])
        batch_rows, _csv = classify(parent, cfg, progress=progress, log=log)
    by_folder = {r.get("Folder"): r for r in batch_rows or []}
    rows = [watch_row(f, by_folder.get(f), befores.get(f), marks.get(f)) for f in folders]
    paths = write_reports(parent, rows, mode=state["text"])
    if _csv:
        paths["batch"] = _csv                   # Batch_Hazard_Classification.csv
    return rows, paths
