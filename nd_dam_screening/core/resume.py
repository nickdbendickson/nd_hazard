"""Picking a county run back up where it stopped (0.28.0): a per-dam manifest.

A county of 200 dams is hours of flood model. The run already survived an interruption in one
way - tool 7 reuses a dam whose result rasters are all there ("cached") - but nothing WROTE DOWN
what had got through, so after a crash the only way to know was to start the whole chain again
and watch it. This module is that record:

    <7_simulations>/run_dams.json      {"dams": {"DWR-5289": {"state": "ok", ...}}, ...}

written one dam at a time as tool 7 finishes it, so a kill -9 between two dams loses at most the
dam that was running. It is read by:
  * tool 7, to say "resuming: 124 of 200 dams already have a result" before the first dam, and -
    with the "skip dams that already have a result" switch off - to re-run them anyway;
  * the window, for the step line and the time estimate (only the dams left are estimated).

Decisions:
  * The manifest never decides that a dam is DONE on its own. The rasters do: `pending` only
    skips a dam whose recorded state is "ok" / "cached" AND whose rasters are still on disk. A
    manifest copied from another machine, or rasters deleted by hand, therefore cost a re-run and
    never a missing result - the safe way round.
  * A FAILED dam is pending again. A failure is usually the terrain service or a time limit, and
    re-trying it is exactly what the engineer wants; "do not retry" would quietly drop dams.
  * Reading and writing never raise. A manifest that cannot be written is a lost convenience,
    not a lost run, so the caller gets False and carries on.
  * No locking: tool 7 runs its dams in worker threads but writes the manifest from the main
    thread only (the on_done callback), the same rule as the flood polygons.
Pure Python standard library.
"""
import json
import os
import time

NAME = "run_dams.json"
VERSION = 1
# a recorded state that counts as a result (when the rasters are there too)
DONE_STATES = ("ok", "cached")
# states that are a result of their own: the dam was not meant to be modelled
SKIPPED_STATES = ("triage", "noflood", "excluded")


def path_for(outdir):
    return os.path.join(outdir or "", NAME)


def load(outdir):
    """The manifest as {"version", "dams", "updated"} ({} dams when there is none / it is
    unreadable)."""
    p = path_for(outdir)
    try:
        with open(p) as fh:
            d = json.load(fh)
        if isinstance(d, dict) and isinstance(d.get("dams"), dict):
            return {"version": d.get("version", VERSION), "dams": d["dams"],
                    "updated": d.get("updated", "")}
    except (OSError, ValueError):
        pass
    return {"version": VERSION, "dams": {}, "updated": ""}


def save(outdir, man):
    """Write the manifest. True when it was written."""
    try:
        os.makedirs(outdir, exist_ok=True)
        man = dict(man or {})
        man.setdefault("version", VERSION)
        man.setdefault("dams", {})
        man["updated"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        tmp = path_for(outdir) + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(man, fh, indent=1, sort_keys=True)
        os.replace(tmp, path_for(outdir))
        return True
    except (OSError, ValueError, TypeError):
        return False


def record(outdir, dam_id, state, scenario="", rasters=(), note="", extra=None):
    """Write down what one dam ended as. Called from the main thread after each dam."""
    man = load(outdir)
    row = {"state": str(state or ""), "scenario": str(scenario or ""),
           "rasters": [str(r) for r in (rasters or []) if r],
           "note": str(note or "")[:400], "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    if extra:
        row.update(extra)
    man["dams"][str(dam_id)] = row
    return save(outdir, man)


def has_result(row, check_rasters=True):
    """True when a manifest row means "this dam has a result". `check_rasters`: its rasters must
    still be on disk (the manifest alone never makes a dam done)."""
    if not isinstance(row, dict):
        return False
    if str(row.get("state") or "") not in DONE_STATES:
        return False
    rs = [r for r in (row.get("rasters") or []) if r]
    if not check_rasters:
        return True
    return bool(rs) and all(os.path.isfile(r) for r in rs)


def done_ids(outdir, check_rasters=True, man=None):
    """The dam IDs the manifest says have a result."""
    dams = (man or load(outdir))["dams"]
    return sorted(k for k, v in dams.items() if has_result(v, check_rasters))


def pending(dam_ids, outdir, skip_done=True, man=None):
    """(to_run, skipped) of `dam_ids`, in the order given. With skip_done off every dam is
    to_run, which is how an engineer forces a re-run of a county."""
    man = man or load(outdir)
    todo, skip = [], []
    for d in dam_ids or []:
        did = str(d)
        if skip_done and has_result(man["dams"].get(did)):
            skip.append(did)
        else:
            todo.append(did)
    return todo, skip


def counts(outdir, man=None):
    """{"dams", "done", "failed", "skipped", "other"} of the recorded states."""
    dams = (man or load(outdir))["dams"]
    out = {"dams": len(dams), "done": 0, "failed": 0, "skipped": 0, "other": 0}
    for v in dams.values():
        st = str((v or {}).get("state") or "")
        if st in DONE_STATES:
            out["done"] += 1
        elif st == "FAILED":
            out["failed"] += 1
        elif st in SKIPPED_STATES:
            out["skipped"] += 1
        else:
            out["other"] += 1
    return out


def summary_text(outdir, total=None, man=None):
    """"124 of 200 dam(s) already have a result; 3 failed and will be tried again." ("" when
    there is no manifest yet.)"""
    c = counts(outdir, man)
    if not c["dams"]:
        return ""
    parts = ["{}{} dam(s) already have a result".format(
        c["done"], " of {}".format(total) if total else "")]
    if c["failed"]:
        parts.append("{} failed and will be tried again".format(c["failed"]))
    if c["skipped"]:
        parts.append("{} were not modelled (triage / no flood / excluded)".format(c["skipped"]))
    return "; ".join(parts) + "."


def pending_count(outdir, total, man=None):
    """How many of `total` dams are left to run (total when there is no manifest)."""
    try:
        total = int(total or 0)
    except (TypeError, ValueError):
        return None
    if total <= 0:
        return None
    man = man or load(outdir)
    # the same test `pending` uses: a recorded result whose rasters are gone is work still to do
    done = len(done_ids(outdir, True, man))
    return max(total - done - counts(outdir, man)["skipped"], 0)
