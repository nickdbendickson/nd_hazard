"""Why is that button grey? (3.20.0)

A disabled Run button with no explanation is the complaint that came back from the
walk-through, and it is the one thing a new user cannot work around. This module answers it as
data - one list of reasons, each naming the missing input and what to do about it - so the
window can put the same words in the button's tooltip and in the status line, and the run tools
can say it in the log before they stop.

    st = state("rundam", folder, dem=..., dams_folder=...)
    st["ok"]          False
    st["why"]         ["needs a DEM: Download DEM for this dam"]
    st["tooltip"]     the button's tooltip
    st["status"]      the status line

Where the reasons come from:
  * damsuite.suitecheck.blocking() - the suite set-up errors (no suite folder, no offline pack
    in pack mode, a plugin too old). These stop every tool, so they come first.
  * this plugin's own preflight knowledge, cheaply: is there a Dams folder, a dam ID, a dam
    folder, a DEM (the window's choice or the one dam.json's data section records), a dam
    height and storage (the tool's parameters or dam.json), and - for the tools that read a
    finished run - the final rasters.

Deliberately NOT here: anything that reads a raster or starts a solver. The window calls this
on every activation and whenever a field changes, so it must stay a few file-existence checks.
The real pre-run check (damsuite.solvegate: the DEM covers the dam, the hydrograph has a peak,
LISFLOOD-FP starts) still runs inside the tool, where it is allowed to take a second; what this
module says is a subset of it, never a different answer. A reason that is only a warning - no
land cover, no DWR export - is NOT a reason to grey a button out: the suite runs without them,
and `blocking` already draws that line.

Pure Python + the vendored damsuite; no QGIS, no numpy.
"""
import os

from . import workflow

# tools whose inputs are one dam's values (a run of the breach model)
RUN_TOOLS = ("rundam", "wizard:", "estimate", "sensitivity", "nested", "probabilistic")
# tools that read a finished run out of the dam folder
RESULT_TOOLS = ("products", "consequences", "ndhazardclass", "classify", "animation",
                "submittal", "benchmark", "hecrasexport", "hecrasmodel", "crossings",
                "cascadecheck", "validationrecord")
# tools that need nothing of a dam at all
FREE_TOOLS = ("check", "verification", "verifymanifest", "selftest", "setupanuga", "buildvrt",
              "fillgaps", "nlcdmanning", "getdamdata", "signrates", "compareruns", "queue",
              "batch", "cascade", "hydrograph", "rainfallrunoff", "hecrasimport",
              "conditiondem")

NEED_DAMS_FOLDER = "needs a Dams folder: choose it at the top of the window"
NEED_DAM = "needs a dam ID: choose or type one at the top of the window"
NEED_DEM = "needs a DEM: Download DEM for this dam"
NEED_RUN = "needs a finished run: Run breach model"
NEED_VALUES = ("needs the dam's height and storage: give them in the tool, or run "
               "'Get data for this dam'")


def _file(folder, *parts):
    return bool(folder) and os.path.isfile(os.path.join(folder, *parts))


def has_run(folder):
    """True when the dam folder holds a FINISHED run a results tool can read (3.21.0: the three
    final rasters AND the run report, so a cancelled run's pass rasters do not light up the
    results buttons; see workflow.run_finished)."""
    return workflow.run_finished(folder)


def dam_values(folder):
    """(height_ft, storage_acft) from the dam folder's dam.json; (None, None) without one.

    Never raises: a dam.json that cannot be read is the same as none for this purpose (the tool
    itself reports the file)."""
    if not folder or not os.path.isdir(folder):
        return None, None
    try:
        from ..damsuite import dam_record as DR
        rec = DR.load(folder)
    except Exception:                                            # noqa - an unreadable record
        return None, None
    if rec is None:
        return None, None

    def pos(field):
        v = rec.get(field)
        return float(v) if isinstance(v, (int, float)) and v > 0 else None
    return pos("height_ft"), pos("storage_acft")


def suite_reasons(items=None):
    """The suite set-up errors as reasons (damsuite.suitecheck.blocking).

    `items` replaces the live check (tests, and a window that already has them)."""
    out = []
    try:
        from ..damsuite import suitecheck as SC
        if items is None:
            items = SC.check()
        for i in SC.blocking(items):
            fix = (i.get("fix") or "").strip()
            out.append("{}: {}".format(i.get("label") or i.get("key") or "set-up",
                                       fix or i.get("text") or "not set up"))
    except Exception:                                            # noqa - never grey out on this
        return []
    return out


def state(tool, folder="", dem="", dams_folder="", dam_id="", items=None, values=None,
          models=None):
    """Whether `tool` can run for this dam, and why not.

    folder        the dam folder (<Dams folder>/<Dam ID>), "" when there is none yet
    dem           the DEM the window would fill in (the window's choice or the dam folder's)
    values        {"HEIGHT_FT", "STORAGE_ACFT"} the caller already has (the wizard's answers)
    models        {"lisflood": bool, "anuga": bool} when the caller knows; None = do not check
    items         damsuite.suitecheck items the caller already has

    Returns {"ok", "why", "tooltip", "status", "suite"}: `why` is one line per missing thing,
    the first of which is what a status line should say."""
    # a suite set-up error stops the tools that work on a dam; it does not stop the tools that
    # need nothing of the suite (checking the model installation, the self-test, the data
    # tools) - greying those out would leave a new user with nothing to press
    suite = [] if tool in FREE_TOOLS else list(suite_reasons(items))
    why = list(suite)
    if tool not in FREE_TOOLS:
        if not dams_folder:
            why.append(NEED_DAMS_FOLDER)
        elif not dam_id:
            why.append(NEED_DAM)
    if tool in RUN_TOOLS:
        if not str(dem or "").strip():
            why.append(NEED_DEM)
        h, s = (values or {}).get("HEIGHT_FT"), (values or {}).get("STORAGE_ACFT")
        if not h or not s:
            rh, rs = dam_values(folder)
            h, s = h or rh, s or rs
        if not h or not s:
            why.append(NEED_VALUES)
        if models is not None and not models.get("lisflood") and not models.get("builtin", True):
            why.append("needs a flood engine: Check the model installation")
    if tool in RESULT_TOOLS and not has_run(folder):
        why.append(NEED_RUN)
    ok = not why
    return {"ok": ok, "why": why, "suite": suite,
            "tooltip": tooltip(tool, why), "status": status_line(why)}


def tooltip(tool, why, base=""):
    """The button's tooltip: what the tool does, then why it is grey."""
    parts = [base.strip()] if base and base.strip() else []
    if why:
        parts.append("Not ready:\n" + "\n".join("- " + w for w in why))
    return "\n\n".join(parts)


def status_line(why):
    """The window's status line ("" when everything is there)."""
    if not why:
        return ""
    if len(why) == 1:
        return "Not ready: " + why[0]
    return "Not ready: {} (and {} more thing(s) below)".format(why[0], len(why) - 1)


def log_lines(st):
    """What a tool should log before it stops because of this."""
    if not st or st.get("ok"):
        return []
    return ["This run cannot start yet:"] + ["  - " + w for w in st.get("why") or []]
