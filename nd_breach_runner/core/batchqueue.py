"""A queue of dams to run, two at a time, that survives being cancelled (3.20.0).

Feature 16 of the review: run the dams of a list - the Screening priority list, a county's
worth of dam IDs, a spreadsheet column - instead of a point layer, and let the person stop it
and come back to it.

TWO AT A TIME, ONE THREAD EACH
That is the default here, and it is a measurement, not a preference. On this suite's own runs,
two dams side by side at one thread each finished 1.76x faster than one dam at two threads, and
asking for more threads than the machine has cores was far worse than either - the solvers
spend their time fighting over cache instead of solving. So `DEFAULT_WORKERS` is 2 and
`THREADS_PER_DAM` is 1, and `worker_note` says why on every run. A person who wants more can
have it; the note then says what the measurement was.

RESUMING
The queue is a file in the Dams folder (`_queue/breach_queue.json`): the dams in order, each
with its state ("waiting", "running", "done", "failed", "skipped", "cancelled") and what
happened. `resume` reads it back and re-queues only what is not finished, and a dam whose
folder already holds the final depth raster is marked "skipped (already run)" without being
started again. A dam left "running" by a crash or a cancel goes back to "waiting": the run it
was in the middle of wrote nothing final, and re-running it is cheaper than deciding from the
outside whether its half-written model folder is usable (the run's own checkpoint inside the
dam folder still picks up where it left off, which is where resuming a single dam belongs).

Cancelling is the caller's business - the Processing feedback's own Cancel - but the queue is
written after every dam, so whatever was finished stays finished.

    q = new_queue(dams_folder, dam_ids, source="Screening priority list")
    for dam_id in pending(q):
        ...
        mark(q, dam_id, "done", minutes=12.4); save(q)

Pure Python standard library; no QGIS, no numpy. Nothing here runs a model: it holds the list.
"""
import csv
import datetime
import json
import os

QUEUE_FOLDER = "_queue"
QUEUE_NAME = "breach_queue.json"
FORMAT = "nd_dam_suite/breach_queue"
FORMAT_VERSION = 1
# the measured values this release defaults to (see the module docstring)
DEFAULT_WORKERS = 2
THREADS_PER_DAM = 1
SCALING_NOTE = ("Two dams side by side at one thread each measured 1.76x faster than one dam "
                "at two threads, and more threads than cores was much slower than either; "
                "that is why this starts at {} dams x {} thread.".format(DEFAULT_WORKERS,
                                                                         THREADS_PER_DAM))
WAITING, RUNNING, DONE, FAILED, SKIPPED, CANCELLED = ("waiting", "running", "done", "failed",
                                                      "skipped", "cancelled")
FINISHED = (DONE, SKIPPED)
# the dam ID column of a list, in the order they are tried (the priority list's own first)
ID_COLUMNS = ("Dam ID", "dam_id", "DAM_ID", "Dam", "dam", "id", "ID", "NIDID", "nid_id",
              "dwr_structure_id")


def _now():
    return datetime.datetime.now().isoformat(timespec="seconds")


def queue_path(dams_folder):
    return os.path.join(dams_folder or "", QUEUE_FOLDER, QUEUE_NAME)


# ------------------------------------------------------------------ the list --
def read_list(path):
    """(dam ids, note) from a list file.

    A CSV with a dam ID column (the Screening priority list's "Dam ID", or any of ID_COLUMNS)
    or a plain text file with one ID per line. Blank lines, a leading '#' comment and repeats
    are dropped, and the order of the file is kept - a priority list is in priority order and
    re-sorting it would throw away the one thing it is for."""
    if not path or not os.path.isfile(path):
        raise OSError("no dam list at {}".format(path or "(none)"))
    with open(path, encoding="utf-8-sig") as fh:
        text = fh.read()
    ids, note = [], ""
    first = text.splitlines()[0] if text.strip() else ""
    if "," in first or "\t" in first:
        rows = list(csv.DictReader(text.splitlines(),
                                   dialect=csv.Sniffer().sniff(first) if "\t" in first
                                   else "excel"))
        col = next((c for c in ID_COLUMNS for f in (rows[0].keys() if rows else [])
                    if f == c), "")
        if not col and rows:
            col = list(rows[0].keys())[0]
            note = "no dam ID column was found, so the first column ({}) was used".format(col)
        else:
            note = "dam IDs from the column \"{}\"".format(col)
        for r in rows:
            v = str(r.get(col) or "").strip()
            if v and not v.startswith("#"):
                ids.append(v)
    else:
        for line in text.splitlines():
            v = line.strip()
            if v and not v.startswith("#"):
                ids.append(v)
        note = "one dam ID per line"
    out = []
    for v in ids:
        if v not in out:
            out.append(v)
    if len(out) != len(ids):
        note += "; {} repeated ID(s) dropped".format(len(ids) - len(out))
    return out, note


# ----------------------------------------------------------------- the queue --
def new_queue(dams_folder, dam_ids, source="", workers=DEFAULT_WORKERS,
              threads_per_dam=THREADS_PER_DAM, version=""):
    """A fresh queue (not written until `save`)."""
    return {"format": FORMAT, "format_version": FORMAT_VERSION, "created": _now(),
            "dams_folder": dams_folder or "", "source": source, "version": version,
            "workers": int(workers or DEFAULT_WORKERS),
            "threads_per_dam": int(threads_per_dam or THREADS_PER_DAM),
            "note": SCALING_NOTE,
            "items": [{"dam_id": str(d), "status": WAITING, "when": "", "message": "",
                       "minutes": ""} for d in dam_ids or []]}


def save(q, path=""):
    """Write the queue atomically. Returns the path."""
    p = path or queue_path(q.get("dams_folder"))
    os.makedirs(os.path.dirname(os.path.abspath(p)), exist_ok=True)
    q["updated"] = _now()
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(q, fh, indent=1)
    os.replace(tmp, p)
    return p


def load(dams_folder, path=""):
    """(queue, problem) - the queue file of a Dams folder, or (None, text)."""
    p = path or queue_path(dams_folder)
    if not os.path.isfile(p):
        return None, ""
    try:
        with open(p, encoding="utf-8") as fh:
            q = json.load(fh)
    except (OSError, ValueError) as e:
        return None, "the queue file {} could not be read ({})".format(p, e)
    if not isinstance(q, dict) or q.get("format") != FORMAT or \
            not isinstance(q.get("items"), list):
        return None, "{} is not a breach queue file".format(p)
    if int(q.get("format_version") or 0) > FORMAT_VERSION:
        return None, "{} was written by a newer version of the plugin".format(p)
    return q, ""


def item(q, dam_id):
    for it in (q or {}).get("items") or []:
        if str(it.get("dam_id")) == str(dam_id):
            return it
    return None


def mark(q, dam_id, status, message="", minutes=""):
    """Set one dam's state. Returns the item (a dam that is not in the queue is added, so a
    caller can log something about a dam the list did not have)."""
    it = item(q, dam_id)
    if it is None:
        it = {"dam_id": str(dam_id), "status": WAITING, "when": "", "message": "",
              "minutes": ""}
        q.setdefault("items", []).append(it)
    it.update({"status": status, "when": _now(), "message": str(message or "")})
    if minutes != "":
        it["minutes"] = minutes
    return it


def pending(q, done_check=None):
    """The dam IDs still to run, in the queue's order.

    `done_check(dam_id)` (usually `is_done`) marks a dam whose results are already on disk as
    skipped instead of returning it."""
    out = []
    for it in (q or {}).get("items") or []:
        if it.get("status") in FINISHED:
            continue
        if it.get("status") == RUNNING:
            # 3.21.0: a dam interrupted mid-run is run again and is NOT eligible for the "already
            # run" skip below: its folder may hold a cancelled pass's rasters, which the skip test
            # used to take for a finished run.
            it["status"] = WAITING
            it["message"] = "left running by an interrupted queue; queued again"
            out.append(it["dam_id"])
            continue
        if done_check is not None and done_check(it["dam_id"]):
            it.update({"status": SKIPPED, "when": _now(),
                       "message": "already run (the dam folder has a finished run)"})
            continue
        out.append(it["dam_id"])
    return out


def resume(dams_folder, dam_ids=None, source="", workers=DEFAULT_WORKERS,
           threads_per_dam=THREADS_PER_DAM, version="", path=""):
    """(queue, note): the queue file if there is one - with any new IDs appended - else a new
    queue. The note is the line to log."""
    q, problem = load(dams_folder, path)
    if q is None:
        if problem:
            return (new_queue(dams_folder, dam_ids, source, workers, threads_per_dam, version),
                    problem + "; a new queue was started")
        return (new_queue(dams_folder, dam_ids, source, workers, threads_per_dam, version),
                "new queue of {} dam(s)".format(len(dam_ids or [])))
    have = {str(it.get("dam_id")) for it in q.get("items") or []}
    added = [d for d in dam_ids or [] if str(d) not in have]
    for d in added:
        q["items"].append({"dam_id": str(d), "status": WAITING, "when": "", "message": "",
                           "minutes": ""})
    q["workers"] = int(workers or DEFAULT_WORKERS)
    q["threads_per_dam"] = int(threads_per_dam or THREADS_PER_DAM)
    counts = summary(q)
    return q, ("resumed the queue in {}: {} done, {} failed, {} still to run{}".format(
        queue_path(dams_folder), counts[DONE] + counts[SKIPPED], counts[FAILED],
        counts[WAITING] + counts[RUNNING],
        "; {} new dam(s) added".format(len(added)) if added else ""))


def summary(q):
    """{status: count} plus "total"."""
    out = {s: 0 for s in (WAITING, RUNNING, DONE, FAILED, SKIPPED, CANCELLED)}
    for it in (q or {}).get("items") or []:
        s = it.get("status")
        if s in out:
            out[s] += 1
    out["total"] = len((q or {}).get("items") or [])
    return out


def summary_text(q):
    c = summary(q)
    return ("{} dam(s): {} done, {} already run, {} failed, {} cancelled, {} still to "
            "run.".format(c["total"], c[DONE], c[SKIPPED], c[FAILED], c[CANCELLED],
                          c[WAITING] + c[RUNNING]))


def worker_note(workers, threads_per_dam=THREADS_PER_DAM, cores=None):
    """The line a run logs about how it is sharing the machine - including the warning when the
    choice oversubscribes it, which is the one setting that was measured as catastrophic."""
    workers = int(workers or DEFAULT_WORKERS)
    threads = int(threads_per_dam or THREADS_PER_DAM)
    cores = cores or (os.cpu_count() or 2)
    text = "Running {} dam(s) at a time, {} thread(s) each ({} core(s) available). {}".format(
        workers, threads, cores, SCALING_NOTE)
    if workers * threads > cores:
        text += (" WARNING: {} x {} is more than the {} core(s) of this computer; "
                 "oversubscribing the solvers was measured as much slower than either "
                 "setting.".format(workers, threads, cores))
    return text


def rows(q):
    """The queue as table rows (for a CSV next to the batch summary)."""
    return [{"Dam": it.get("dam_id"), "Status": it.get("status"), "When": it.get("when"),
             "Minutes": it.get("minutes"), "Message": it.get("message")}
            for it in (q or {}).get("items") or []]


ROW_COLUMNS = ("Dam", "Status", "When", "Minutes", "Message")


def write_csv(path, q):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=ROW_COLUMNS)
        w.writeheader()
        w.writerows(rows(q))
    return path


def is_done(dams_folder, dam_id):
    """True when that dam's folder holds a FINISHED run (not just a cancelled run's pass rasters;
    3.21.0, see workflow.run_finished)."""
    from . import damjson, workflow
    folder = os.path.join(dams_folder or "", damjson.folder_name(dam_id))
    return workflow.run_finished(folder)


def done_checker(dams_folder):
    """`is_done` bound to one Dams folder, for `pending`."""
    return lambda dam_id: is_done(dams_folder, dam_id)
