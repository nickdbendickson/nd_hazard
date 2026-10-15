"""Is the suite ready to run? One list of checks, used by every window (1.16.0).

Three things a new user asks - which window comes first, which folder goes where, why is a
button grey - all come down to "what is set up and what is not". This module answers that once,
as plain data, so the suite home can draw it as lights, the settings windows can draw the same
rows with "Open folder" buttons, and "Check my setup" can print it as text to paste into a
message.

    items = check()                     # [item, ...], one per thing that can be set up
    steps = step_rows(items)            # the four numbered steps with their readiness
    text  = text_report(items)          # the same thing as pasteable text

An item is a dict:
    key      stable name ("folder", "pack", "dwr_dams", "charts", "versions", ...)
    group    "folder" | "data" | "files" | "plugins"  (which panel it belongs in)
    label    short human name for the row ("DWR dams inventory")
    level    "ok" | "warn" | "error" | "info"
    text     what is true now, one line ("in the offline pack")
    fix      what to do when it is not ok, one line ("" when nothing to do)
    folder   a folder to offer an "Open folder" button for ("" when none)
    source   where the file comes from, for the row's tooltip ("" when not applicable)

Levels: "error" stops a run (no folder, no pack in pack mode, a plugin too old); "warn" means
the suite runs but a result will be poorer or a step will have to download (no land cover, no
DWR export, a stale pack); "ok" and "info" need nothing. `blocking(items)` is the errors alone,
which is what a disabled Run button should name.

Decisions:
  * Nothing here downloads, creates or changes a file: it only looks. The one exception is that
    `check` never raises - a folder it cannot read becomes an "error" item.
  * The DWR and HIFLD rows are "warn", not "error": the plugins run without them (the DWR dams
    inventory falls back to the DWR service, the lifelines are simply not counted), and saying
    "error" would stop a novice who only wants one dam.
  * Land cover is "warn" for the same reason: Manning's n then comes from the default table.
  * The pack row is "info" when the data source is Download: then no pack is wanted.
  * The plugin rows reuse damsuite.versions (the hard / soft minimums live there, once).
Pure Python standard library; QGIS is only reached through suitedata and versions.
"""
import json
import os

from . import suitedata as SD
from . import versions as VS

LEVELS = ("ok", "info", "warn", "error")
_RANK = {"ok": 0, "info": 1, "warn": 2, "error": 3}
GROUPS = ("folder", "data", "files", "plugins")
# the four numbered steps of the suite home, in the order a user does them
STEPS = (
    # step 1 builds the pack, so it must not be blocked by the pack being missing; step 3 can
    # download its own terrain, so it needs nothing but the folder
    ("data", "1. Data", "ndupdater",
     "Build or refresh the offline pack: everything the other three windows read.",
     ("folder",)),
    ("screen", "2. Screen", "ndscreen",
     "Screen a county, or one dam, for a first hazard class and the dams worth modelling.",
     ("folder", "pack", "charts")),
    ("model", "3. Model", "ndbreach",
     "Run the breach model for one dam: depth, velocity and arrival time rasters.",
     ("folder",)),
    ("classify", "4. Classify", "ndhazard",
     "Classify the dam from those rasters and write the report.",
     ("folder", "pack", "charts")),
)
# role -> (label, what it is for, where to get it); the drop-in folders a user fills by hand
FILE_ROLES = (
    ("dwr_dams", SD.DWR, "DWR dams inventory",
     "the dam list, its classes and crest elevations",
     "ND DWR: export the dams inventory to GeoPackage or shapefile"),
    ("dwr_dam_lines", SD.DWR, "DWR dam lines",
     "the embankment line, for the breach location and crest length",
     "ND DWR: export the dam lines (the file name must contain \"line\")"),
    ("dwr_unknown", SD.DWR, "DWR unknown structures",
     "structures DWR has not classified yet (optional)",
     "ND DWR: export the unknown structures (file name contains \"unknown\")"),
    ("dwr_dugouts", SD.DWR, "DWR dugouts",
     "dugouts, so they are not screened as dams (optional)",
     "ND DWR: export the dugouts (file name contains \"dugout\")"),
)


def _item(key, group, label, level, text, fix="", folder="", source=""):
    return {"key": key, "group": group, "label": label, "level": level, "text": text,
            "fix": fix, "folder": folder, "source": source}


def _short(path, keep=60):
    """A path short enough for a row ("...\\dwr\\dams.gpkg")."""
    p = str(path or "")
    return p if len(p) <= keep else "..." + p[-(keep - 3):]


# ------------------------------------------------------------------- the data --
def _folder_items(st, folder):
    out = []
    if not folder:
        out.append(_item("folder", "folder", "Suite folder", "error", "not set",
                         "Choose one folder for the whole suite; the plugins make their "
                         "sub-folders in it.", ""))
        return out
    if not st.get("exists"):
        out.append(_item("folder", "folder", "Suite folder", "error",
                         "does not exist: " + _short(folder),
                         "Choose it again, or create it.", folder))
        return out
    missing = [s for s in SD.SUBFOLDERS if not os.path.isdir(SD.sub(folder, s))]
    if missing:
        out.append(_item("folder", "folder", "Suite folder", "warn",
                         _short(folder) + " (sub-folders missing: " + ", ".join(missing) + ")",
                         "Press Set up this folder: it makes the sub-folders.", folder))
    else:
        out.append(_item("folder", "folder", "Suite folder", "ok", _short(folder), "", folder))
    return out


def _pack_items(st, folder):
    out = []
    source = st.get("source") or SD.SOURCE_PACK
    if source == SD.SOURCE_DOWNLOAD:
        out.append(_item("pack", "data", "Offline pack", "info",
                         "not used (data source is Download from the servers)",
                         "Switch the data source to Offline pack to work without internet."))
        return out
    pack = st.get("pack") or ""
    if not pack:
        out.append(_item("pack", "data", "Offline pack", "error", "none found",
                         "Run the ND Data Updater (step 1): Build / refresh the offline pack.",
                         folder))
        return out
    sets = st.get("pack_sets") or []
    bad = st.get("pack_stale") or []
    if bad:
        out.append(_item("pack", "data", "Offline pack", "warn",
                         "{} layers, {} to refresh".format(len(sets), len(bad)),
                         "Run the ND Data Updater: it fetches only what is missing or stale "
                         "({}).".format(", ".join(sorted(bad)[:4]) +
                                        (", ..." if len(bad) > 4 else "")),
                         pack))
    else:
        out.append(_item("pack", "data", "Offline pack", "ok",
                         "{} layers, all current".format(len(sets)), "", pack))
    return out


def _file_items(st, folder):
    """The drop-in files: in the pack, in their folder, or missing."""
    out = []
    dwr = st.get("dwr") or {}
    pack = st.get("pack") or ""
    for key, sub, label, why, where in FILE_ROLES:
        path = dwr.get(key) or ""
        sub_dir = SD.sub(folder, sub)
        in_pack = bool(path and pack and os.path.abspath(path).startswith(os.path.abspath(pack)))
        optional = "optional" in why
        if path:
            out.append(_item(key, "files", label, "ok",
                             "in the offline pack" if in_pack else _short(path),
                             "", sub_dir, where))
        else:
            out.append(_item(key, "files", label, "info" if optional else "warn", "none",
                             "Put the file in the {}/ folder. {}.".format(sub, where),
                             sub_dir, where))
    hifld = st.get("hifld") or []
    out.append(_item("hifld", "files", "HIFLD lifelines", "ok" if hifld else "warn",
                     "{} file(s)".format(len(hifld)) if hifld else "none",
                     "" if hifld else "Put the HIFLD layers (fire and EMS, EOC, law enforcement, "
                     "wastewater) in the hifld/ folder, or let the pack hold them. Without them "
                     "no lifeline is counted.",
                     SD.sub(folder, SD.HIFLD), "hsdl.org (HIFLD open data)"))
    out.append(_item("nlcd", "data", "Land cover", "ok" if st.get("nlcd") else "warn",
                     _short(st.get("nlcd")) if st.get("nlcd") else "none",
                     "" if st.get("nlcd") else "The updater fetches NLCD into the pack, or put a "
                     "raster in nlcd/. Without it Manning's n comes from the default table.",
                     SD.sub(folder, SD.NLCD), "MRLC (NLCD land cover)"))
    out.append(_item("dem", "data", "Your own DEM", "ok" if st.get("dem") else "info",
                     _short(st.get("dem")) if st.get("dem") else
                     "none (each run downloads a DEM instead)",
                     "" if st.get("dem") else "Put a statewide or county DEM (.tif or .vrt) in "
                     "dem/ to run without downloading terrain.",
                     SD.sub(folder, SD.DEM), "USGS 3DEP, or ND LiDAR"))
    tiles = st.get("terrain_tiles") or 0
    out.append(_item("terrain", "data", "Terrain cache", "ok" if tiles else "info",
                     "{} tiles, {} MB".format(tiles, st.get("terrain_mb") or 0.0) if tiles
                     else "empty (it fills itself as you run dams)", "",
                     SD.sub(folder, SD.TERRAIN)))
    return out


CHART_FILE_NAME = "acer_tm11_charts.json"
SIGN_OFF_FIX = ("The chart criterion cannot run until an engineer signs the charts off: Hazard "
                "Helper > Suite settings > Dam suite > Chart sign-off. Until then the "
                "conservative rule is used, which reads High more often.")


def _chart_paths():
    """Where a signed-off chart file could be: the plugin this damsuite copy lives in (the
    Runner and the Screening carry core/acer_tm11_charts.json next to their nd_hazard.py), then
    the sibling plugins' copies when the Hazard Helper's chartsign can name them. The Hazard
    Helper keeps no copy of its own - it is the plugin that signs the file, not one that reads
    it - so there it is the siblings' files, or the table this release ships, that answer."""
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out = [os.path.join(here, "core", CHART_FILE_NAME)]
    try:
        from .. import chartsign as CS
        for path in (CS.plugin_chart_paths() or {}).values():
            if path:
                out.append(path)
    except Exception:                                            # noqa - not the Hazard Helper
        pass
    seen, keep = set(), []
    for path in out:
        key = os.path.normcase(path)
        if key not in seen:
            seen.add(key)
            keep.append(path)
    return keep


def _chart_items():
    """The ACER TM 11 chart file - the default hazard criterion - and its sign-off."""
    path, data, where = "", None, ""
    for candidate in _chart_paths():
        if not os.path.isfile(candidate):
            continue
        try:
            with open(candidate, encoding="utf-8") as f:
                got = json.load(f)
        except (OSError, ValueError):
            continue
        if isinstance(got, dict):
            path, data = candidate, got
            break
    if data is None:
        try:
            from . import tm11_curves as TC
            data = TC.default_chart_file()
            where = " (the file this release ships)"
        except Exception as e:                                   # noqa
            return [_item("charts", "data", "ACER TM 11 charts", "warn",
                          "could not be read ({})".format(e), SIGN_OFF_FIX)]
    folder = os.path.dirname(path) if path else ""
    source = path or "shipped with the plugins"
    status = str((data or {}).get("status") or "")
    by = str((data or {}).get("signed_off_by") or "")
    on = str((data or {}).get("signed_off_on") or "")
    if status == "digitized":
        return [_item("charts", "data", "ACER TM 11 charts", "ok",
                      "signed off by {}{}{}".format(by or "an engineer",
                                                    " on " + on if on else "", where),
                      "", folder, source)]
    return [_item("charts", "data", "ACER TM 11 charts", "warn", "not signed off" + where,
                  SIGN_OFF_FIX, folder, source)]


def _plugin_items(versions=None):
    """One row per suite plugin: its version, and whether it is new enough for the others."""
    out = []
    everything = getattr(VS, "ALL", VS.PLUGINS)
    vers = {k: VS.installed(k) for k in everything} if versions is None else dict(versions)
    release = dict(getattr(VS, "RELEASE", {}) or {})
    for key in everything:
        label = VS.LABELS.get(key, key)
        v = vers.get(key)
        if not v:
            out.append(_item("plugin_" + key, "plugins", label, "warn", "not installed",
                             "Install it to use step {}.".format(
                                 next((i + 1 for i, s in enumerate(STEPS) if s[2] == key), "?"))))
            continue
        want = release.get(key)
        if want and VS._cmp(v, want) < 0:
            out.append(_item("plugin_" + key, "plugins", label, "warn",
                             "{} installed, {} is this release".format(v, want),
                             "Install the {} zip of this release: a mixed set is the commonest "
                             "cause of a hand-off that does nothing.".format(want)))
        else:
            out.append(_item("plugin_" + key, "plugins", label, "ok", v, ""))
    # the pairwise hard / soft minimums, for whichever plugin asks
    for me in VS.PLUGINS:
        if not vers.get(me):
            continue
        for level, text in VS.check(me, versions=vers):
            out.append(_item("versions", "plugins", "Plugin versions",
                             "error" if level == "error" else "warn", text,
                             "Install the matching zips of one release."))
    return out


# ------------------------------------------------------------------ the check --
def check(folder=None, versions=None, st=None, charts=True, plugins=True):
    """Every set-up item, worst first within each group. Never raises."""
    folder = SD.data_folder() if folder is None else folder
    if st is None:
        try:
            st = SD.status(folder)
        except Exception as e:                                   # noqa
            return [_item("folder", "folder", "Suite folder", "error",
                          "could not be read ({})".format(e),
                          "Choose a folder you can write to.", folder or "")]
    items = _folder_items(st, folder)
    if folder and st.get("exists"):
        items += _pack_items(st, folder) + _file_items(st, folder)
    else:
        items += _pack_items(st, folder)
    if charts:
        try:
            items += _chart_items()
        except Exception:                                        # noqa
            pass
    if plugins:
        try:
            items += _plugin_items(versions)
        except Exception:                                        # noqa
            pass
    return items


def by_key(items):
    return {i["key"]: i for i in items or []}


def worst(items):
    """The worst level in `items` ("ok" when empty)."""
    lv = "ok"
    for i in items or []:
        if _RANK.get(i.get("level"), 0) > _RANK.get(lv, 0):
            lv = i["level"]
    return lv


def blocking(items):
    """The items that stop a run - what a disabled Run button should name."""
    return [i for i in items or [] if i.get("level") == "error"]


def summary(items):
    """{"ok", "info", "warn", "error", "level"} counts."""
    out = {lv: 0 for lv in LEVELS}
    for i in items or []:
        if i.get("level") in out:
            out[i["level"]] += 1
    out["level"] = worst(items)
    return out


def summary_text(items):
    s = summary(items)
    if s["error"]:
        return "{} thing(s) stop a run, {} worth fixing.".format(s["error"], s["warn"])
    if s["warn"]:
        return "Ready to run. {} thing(s) would make the results better.".format(s["warn"])
    return "Ready to run; everything is set up."


def group(items, name):
    return [i for i in items or [] if i.get("group") == name]


# -------------------------------------------------------------------- steps --
def step_rows(items, versions=None):
    """[{"key", "label", "plugin", "plugin_label", "why", "level", "blocked_by", "ready"}] for
    the four numbered steps of the suite home. A step is "ready" when nothing it needs is an
    error; `blocked_by` names what is (the text a grey button should show)."""
    found = by_key(items)
    everything = getattr(VS, "ALL", VS.PLUGINS)
    vers = {k: VS.installed(k) for k in everything} if versions is None else dict(versions)
    out = []
    for key, label, plugin, why, needs in STEPS:
        bad, lv = [], "ok"
        for need in needs:
            it = found.get(need)
            if it is None:
                continue
            if _RANK.get(it.get("level"), 0) > _RANK.get(lv, 0):
                lv = it["level"]
            if it.get("level") == "error":
                bad.append("{}: {}".format(it["label"], it["text"]))
        known = plugin in everything
        installed = vers.get(plugin) if known else "n/a"
        if known and not installed:
            bad.append("{} is not installed".format(VS.LABELS.get(plugin, plugin)))
            lv = "error"
        out.append({"key": key, "label": label, "plugin": plugin,
                    "plugin_label": VS.LABELS.get(plugin, "ND Data Updater"),
                    "why": why, "level": lv, "blocked_by": "; ".join(bad),
                    "ready": not bad})
    return out


def next_step(steps):
    """The first step that is ready and not obviously done - what to point the user at."""
    for s in steps or []:
        if not s["ready"]:
            return s
    return (steps or [None])[0]


# ------------------------------------------------------------------- report --
MARK = {"ok": "OK  ", "info": "--  ", "warn": "WARN", "error": "STOP"}


def text_report(items, folder=None, steps=None, title="ND dam suite set-up"):
    """The whole check as text to paste into a message ("Check my setup")."""
    lines = [title, "=" * len(title), ""]
    folder = SD.data_folder() if folder is None else folder
    lines.append("Suite folder: " + (folder or "not set"))
    lines.append(summary_text(items))
    lines.append("")
    names = {"folder": "Folder", "data": "Data", "files": "Files you supply",
             "plugins": "Plugins"}
    for g in GROUPS:
        rows = group(items, g)
        if not rows:
            continue
        lines.append(names.get(g, g) + ":")
        for i in rows:
            lines.append("  {} {:<26} {}".format(MARK.get(i["level"], "?   "),
                                                 i["label"][:26], i["text"]))
            if i.get("fix") and i["level"] in ("warn", "error"):
                lines.append("       -> " + i["fix"])
        lines.append("")
    if steps:
        lines.append("Steps:")
        for s in steps:
            lines.append("  {} {:<12} {}".format(MARK.get(s["level"], "?   "), s["label"],
                                                 s["blocked_by"] or "ready"))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
