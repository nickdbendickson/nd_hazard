# -*- coding: utf-8 -*-
"""2.30.0: why a Run button is grey, in words that name the missing input.

Both run buttons ("Run Hazard Classification" on the classification tab and "Run everything"
on the Data & Output tab) used to go grey with no reason: the tooltip said "Missing: Depth
raster" on one of them only, and the status line said "No classification run yet", which is
what it says after a finished run too. A novice had nothing to act on.

This module turns the state of the window into reasons, so both buttons, their tooltips and
the status line say the same thing and name what to do about it:

    rs = reasons(have, blocking=suitecheck.blocking(suitecheck.check()))
    button.setToolTip(tooltip(rs))
    status.setText(status_text(rs))

`have` is what the window knows: {"dam_folder": bool, "depth": bool, "vel": bool, "arr": bool,
"one_d": bool, "busy": bool}. Deliberately plain booleans, not widgets or paths, so the window
does the os.path work (it alone knows what the engineer typed) and this module can be tested.

Decisions:
  * The window's own inputs come FIRST and the suite's set-up items (damsuite.suitecheck,
    which knows about the folder, the offline pack and the ACER charts) after them: a missing
    depth raster is what the engineer can fix in the next second, a missing pack is a trip to
    another window.
  * The arrival raster is never a reason: the run works without it (no arrival fields, no
    warning times), as _required_rasters in the dialog has always had it.
  * Velocity is a reason for a 2D model only; the 1D criterion is depth > 2 ft.
  * One reason per missing thing, each with what to do - never "invalid input".
  * Nothing here reads the file system or QGIS, and nothing raises: a caller inside a UI
    callback can use it without a try / except.
"""

# the window's own inputs, in the order the Setup & Run tab asks for them
INPUT_ORDER = ("dam_folder", "depth", "vel")
LABELS = {"dam_folder": "Dam folder", "depth": "Depth raster", "vel": "Velocity raster",
          "arr": "Arrival raster"}
FIXES = {
    "dam_folder": "choose it on 'Setup & Run' (step 1) - the folder with the breach model's "
                  "rasters",
    "depth": "press 'Find rasters' on 'Setup & Run', or browse to the Depth (Max) raster; run "
             "the breach model first when there is none (step 3 of the suite)",
    "vel": "press 'Find rasters' on 'Setup & Run', or browse to the Velocity (Max) raster; "
           "choose the 1D model when the model has no velocity",
}
BUSY_TEXT = "A run is in progress - wait for it to finish or press Cancel."
READY_TEXT = "Every input is ready - press Run."


def required(have):
    """The input keys this run needs: depth always, velocity for a 2D model (ND Figs 5.1/5.2).
    The arrival raster is optional, so it is never required."""
    keys = ["dam_folder", "depth"]
    if not (have or {}).get("one_d"):
        keys.append("vel")
    return keys


def missing_inputs(have):
    """[(key, label, fix)] of the window's own inputs that are not ready, in INPUT_ORDER."""
    have = have or {}
    need = required(have)
    out = []
    for key in INPUT_ORDER:
        if key in need and not have.get(key):
            out.append((key, LABELS[key], FIXES[key]))
    return out


def _suite_reason(item):
    """One damsuite.suitecheck item as (key, label, fix)."""
    item = item or {}
    label = str(item.get("label") or item.get("key") or "Suite set-up")
    what = str(item.get("text") or "").strip()
    fix = str(item.get("fix") or "").strip()
    if what:
        label = "{} ({})".format(label, what)
    return (str(item.get("key") or "suite"), label,
            fix or "open 'Dam suite home...' and finish the set-up step it names")


def reasons(have, blocking=(), limit=6):
    """[(key, label, fix)] of everything that stops a run - the window's inputs first, then
    the suite's blocking set-up items (damsuite.suitecheck.blocking). Empty = ready to run.
    A busy window gives the single reason ("busy", ...)."""
    have = have or {}
    if have.get("busy"):
        return [("busy", "A run is in progress", BUSY_TEXT)]
    out = missing_inputs(have)
    seen = {k for k, _l, _f in out}
    for item in blocking or ():
        if not isinstance(item, dict):
            continue                      # suitecheck gave something else: it cannot be a reason
        try:
            key, label, fix = _suite_reason(item)
        except Exception:                                        # noqa - never stop a callback
            continue
        if key not in seen:
            seen.add(key)
            out.append((key, label, fix))
    return out[:max(1, int(limit))] if out else out


def names(rs):
    """"Depth raster, Velocity raster" - the short list for a one-line message."""
    return ", ".join(label.split(" (")[0] for _k, label, _f in rs or ())


def tooltip(rs, ready=READY_TEXT):
    """The tooltip of a run button: every reason with what to do, one per line."""
    if not rs:
        return ready
    if len(rs) == 1 and rs[0][0] == "busy":
        return BUSY_TEXT
    head = "Cannot run yet - {} thing(s) missing:".format(len(rs))
    return "\n".join([head] + ["- {}: {}".format(label, fix) for _k, label, fix in rs])


def status_text(rs, ready=READY_TEXT):
    """The window's status line: the same reasons as one sentence."""
    if not rs:
        return ready
    if len(rs) == 1 and rs[0][0] == "busy":
        return BUSY_TEXT
    first = rs[0]
    rest = " ({} more: {})".format(len(rs) - 1, names(rs[1:])) if len(rs) > 1 else ""
    return "Cannot run yet - {} is missing: {}.{}".format(first[1], first[2], rest)


def log_lines(rs):
    """The same reasons for the run log / a message box."""
    if not rs:
        return [READY_TEXT]
    return ["Cannot run yet:"] + ["  - {}: {}".format(label, fix) for _k, label, fix in rs]
