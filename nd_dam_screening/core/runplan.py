"""What the next run will actually do (0.28.0): the cell size it will use, and how long it takes.

Two things the window said and the engine did not do.

THE CELL SIZE. The Settings tab has a "Grid size (ft)" box, starting at 33 ft (10 m), and a user
reasonably believes it decides the flood grid. It does not: tool 7 also has a RESOLUTION PRESET
whose first entry is "Quick screen (50 m grid)", the window never set it, so QGIS used its
default - the preset - and every flood ran at 50 m (164 ft) whatever the box said. Two honest
ways out: show the size the engine will use next to the box, or relabel the box. This module does
the first, and the window now passes the preset explicitly, so the box means something when the
preset is Custom. Nothing here changes a cell size; it only says which one wins:

    eff = effective_cell(settings)       # {"cell_m", "cell_ft", "how", "text"}

THE TIME. The elapsed timer only starts once a run does, and "how long will a county take" was
answerable only by running tool 12 (which calibrates the engine with a real run). For a label
next to that timer an order of magnitude is enough, so the numbers come from the plugin's own
published preset figures (~12 s/dam at 50 m, ~2 min at 20 m, ~15 min at 10 m), interpolated in
log-log between them, times what doubles a run (a no-breach baseline, both scenarios, clip
pre-growth) and divided by the dams run at the same time:

    text = run_time_text(n_dams, settings, workers=4, pending=25)

The published figures are for the built-in engine on one modern core; LISFLOOD-FP is faster by a
factor nobody here has measured (core/estimate.LISFLOOD_FACTOR, a guess), so the label says
"about" and tool 12 stays the way to get a real number. Deliberately NOT a promise: the clip
growth, cascades and terrain downloads tool 7 decides at run time are outside it.

Pure Python standard library.
"""
import math
import os
import sqlite3

FT = 0.3048
# (cell size in m, seconds per dam) - the figures algorithms/run_breach_sim.PRESETS shows the user
ANCHORS = ((50.0, 12.0), (20.0, 120.0), (10.0, 900.0))
PRESET_CELL_M = (50.0, 20.0, 10.0)        # PRESET_VALS: Quick, Standard, Detailed; 3 = Custom
PRESET_NAMES = ("Quick screen", "Standard", "Detailed", "Custom")
CUSTOM = 3
LISFLOOD_FACTOR = 0.1                     # as core/estimate.py: A GUESS until timed here
BASELINE_FACTOR = 2.0                     # tool 7's own words: "roughly doubles the run time"
BOTH_SCENARIOS_FACTOR = 2.0               # two floods per dam instead of one
PREGROW_FACTOR = 1.6                      # a pre-grown clip is larger before the first run
AUTO_TEXT = ("chosen per dam from the reservoir size (3 m under 100 ac-ft, 5 m under 1,000, "
             "10 m under 10,000, else 20 m, never finer than the DEM)")


# ------------------------------------------------------------------ the cell size --
def preset_index(settings):
    """The resolution preset a run will use (0..3); the window's "preset" setting, default Quick
    screen - which is what QGIS's own default has silently been since 0.23.0."""
    try:
        i = int((settings or {}).get("preset") or 0)
    except (TypeError, ValueError):
        i = 0
    return i if 0 <= i <= CUSTOM else 0


def effective_cell(settings):
    """Which cell size tool 7 will use, and why.

    {"cell_m": float or None, "cell_ft": float or None, "how": "preset"|"grid box"|"automatic",
     "preset": index, "text": one line for the window}. cell_m is None only for "automatic",
     where the size is a per-dam decision (core/domain.auto_cell) and no single number is true."""
    S = settings or {}
    i = preset_index(S)
    if i < CUSTOM:
        cell = PRESET_CELL_M[i]
        return {"cell_m": cell, "cell_ft": cell / FT, "how": "preset", "preset": i,
                "text": "Step 7 will use {:.0f} ft ({:.0f} m): the {} preset, not this box.".format(
                    cell / FT, cell, PRESET_NAMES[i])}
    try:
        ft = float(S.get("grid_ft") if S.get("grid_ft") is not None else 33.0)
    except (TypeError, ValueError):
        ft = 33.0
    if ft <= 0:
        return {"cell_m": None, "cell_ft": None, "how": "automatic", "preset": i,
                "text": "Step 7 will use a cell size " + AUTO_TEXT + "."}
    return {"cell_m": ft * FT, "cell_ft": ft, "how": "grid box", "preset": i,
            "text": "Step 7 will use {:.0f} ft ({:.1f} m): this box, with the Custom preset.".format(
                ft, ft * FT)}


def cell_label(settings):
    """The one line that sits next to the Grid size box."""
    return effective_cell(settings)["text"]


# -------------------------------------------------------------------- the time --
def seconds_per_dam(cell_m, engine=2, baseline=False, both_scenarios=False, pregrow=False):
    """Order-of-magnitude seconds for one dam at this cell size (log-log between ANCHORS, held
    at the ends), times what doubles a run. `engine`: the window's index (0 / 1 LISFLOOD-FP)."""
    cell = float(cell_m or PRESET_CELL_M[0])
    cell = min(max(cell, 1.0), 200.0)
    pts = sorted(ANCHORS)                                     # by cell size, finest first
    if cell <= pts[0][0]:
        s = pts[0][1]
    elif cell >= pts[-1][0]:
        s = pts[-1][1]
    else:
        s = None
        for (c0, s0), (c1, s1) in zip(pts[:-1], pts[1:]):
            if c0 <= cell <= c1:
                f = math.log(cell / c0) / math.log(c1 / c0)
                s = math.exp(math.log(s0) + f * (math.log(s1) - math.log(s0)))
                break
    if int(engine or 0) in (0, 1):
        s *= LISFLOOD_FACTOR
    if baseline:
        s *= BASELINE_FACTOR
    if both_scenarios:
        s *= BOTH_SCENARIOS_FACTOR
    if pregrow:
        s *= PREGROW_FACTOR
    return float(s)


def fmt_duration(s):
    """"45 s", "7 min", "2.5 h", "1.3 days" - the same shape as core/estimate.fmt_duration."""
    s = float(s or 0)
    if s < 90:
        return "{:.0f} s".format(s)
    if s < 5400:
        return "{:.0f} min".format(s / 60.0)
    if s < 48 * 3600:
        return "{:.1f} h".format(s / 3600.0)
    return "{:.1f} days".format(s / 86400.0)


def run_time_text(n_dams, settings, workers=None, pending=None):
    """"County: 37 dams x ~12 s/dam at 164 ft (50 m) = about 2 min (4 at a time)." - the label
    next to the elapsed timer. "" when there is no dam count to work with."""
    S = settings or {}
    try:
        n = int(n_dams or 0)
    except (TypeError, ValueError):
        n = 0
    if n <= 0:
        return ""
    eff = effective_cell(S)
    cell = eff["cell_m"]
    per = seconds_per_dam(cell, engine=int(S.get("engine") or 0), baseline=bool(S.get("baseline")),
                          both_scenarios=bool(S.get("both_scenarios")), pregrow=bool(S.get("pregrow")))
    try:
        w = int(workers if workers is not None else (S.get("workers") or 0))
    except (TypeError, ValueError):
        w = 0
    w = max(w, 1)
    todo = n if pending is None else max(int(pending), 0)
    total = per * todo / float(w)
    at = ("at {:.0f} ft ({:.0f} m)".format(eff["cell_ft"], eff["cell_m"]) if cell
          else "at the automatic cell size")
    text = "{} dam(s) x about {}/dam {} = about {}".format(n, fmt_duration(per), at,
                                                            fmt_duration(total))
    if w > 1:
        text += " ({} at a time)".format(w)
    if pending is not None and todo != n:
        text += "; {} already have a result and are not re-run".format(n - todo)
    extra = [t for t, on in (("no-breach baseline", S.get("baseline")),
                             ("both scenarios", S.get("both_scenarios")),
                             ("clip pre-growth", S.get("pregrow"))) if on]
    if extra:
        text += "; includes " + " and ".join(extra)
    return text + ". Order of magnitude only (tool 12 times your computer)."


# ------------------------------------------------------- how many dams are there --
def _first_table(con):
    row = con.execute("SELECT table_name FROM gpkg_contents WHERE data_type='features' "
                      "LIMIT 1").fetchone()
    return row[0] if row else None


def count_rows(gpkg):
    """Features in the first feature table of a GeoPackage, or None when it cannot be read.
    sqlite3 only - no QGIS, so the window can call it while a run is going on."""
    if not gpkg or not os.path.isfile(gpkg):
        return None
    con = None
    try:
        con = sqlite3.connect(gpkg)
        t = _first_table(con)
        if not t:
            return None
        return int(con.execute('SELECT COUNT(*) FROM "{}"'.format(t.replace('"', '""'))).fetchone()[0])
    except Exception:                                        # noqa - unreadable: no number
        return None
    finally:
        if con is not None:
            con.close()
