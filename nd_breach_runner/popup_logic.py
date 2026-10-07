"""The logic behind the ND Dam Breach Runner popup window - pure Python, no Qt, no QGIS.

The window is a front door to the tools, not a second copy of them: it knows the DAMS FOLDER
(one folder per dam, named after the dam ID - the Runner's own layout) and the dam, shows what
is in that dam's folder, and opens the Processing dialog of any tool with the dam's folder (and
the DEM chosen in the window) filled in. The tools, the wizard and the batch / qgis_process use
are unchanged; every filled-in value can still be changed in the tool's own dialog.

3.9.0: an empty DEM is the dam folder's (dam.json's data section, "Get data for this dam"), the
suite's Dams folder is read and offered, and the DEM download box's dam point, size and result
handling (data/dem_<cell>m.tif, recorded in dam.json) are here.
"""
import os

from .core import damjson, workflow

PROVIDER = "ndbreach"
WIZARD = "wizard:"

# tab -> tools: (algorithm, button label, tooltip). "wizard:" opens the guided wizard.
TABS = (
    ("Dam", (
        (WIZARD, "Guided setup for one dam (wizard)...",
         "Five pages that fill in 'Run breach model for one dam', with a live run-time estimate."),
        ("rundam", "Run breach model for one dam...",
         "The breach hydrograph, the LISFLOOD-FP flood (and ANUGA if chosen), rasters, products and "
         "the run report, written into <Dams folder>/<Dam ID>."),
        ("batch", "Run breach models for a layer of dams...",
         "One run per dam of a point layer; can hand each result to the hazard classification."),
        ("estimate", "Estimate the run time...",
         "The plan and the time of a run for this dam, without running it."),
    )),
    ("Model", (
        ("nested", "Nested fine grid near the dam...", "A fine grid near the dam, a coarse one downstream."),
        ("sensitivity", "Sensitivity of the breach model...",
         "Re-runs the dam with other breach widths, times, Manning n and equations."),
        ("probabilistic", "Probabilistic (Monte Carlo) run...",
         "Runs drawn from parameter distributions you set; 'Write each run's rasters' lets ND Dam "
         "Hazard give a class probability."),
        ("cascade", "Cascade failure of dams in a chain...", "Downstream dams that fail in turn."),
        ("hydrograph", "Breach hydrograph only...", "No flood model: the breach outflow hydrograph."),
        ("rainfallrunoff", "Rainfall to PMF inflow hydrograph...", "A PMF inflow from rainfall."),
    )),
    ("Data", (
        ("getdamdata", "Get data for this dam...",
         "3.9.0: the DEM, land cover, structures, roads, railroads, waterbodies ... of the dam into "
         "<dam folder>/data/ and dam.json (the suite data folder's offline pack, else the servers "
         "of Plugins > ND Dam Suite > Suite data and servers). The tools then fill their EMPTY "
         "inputs from there. Leave the Dam ID empty in the dialog for every dam of the folder."),
        ("buildvrt", "Build a statewide LiDAR mosaic (VRT)...", "One virtual raster over DEM tiles."),
        ("fillgaps", "Fill no-data gaps in a DEM...", "Small gaps in a DEM, before modelling."),
        ("nlcdmanning", "Manning's n from NLCD...", "A Manning's n raster from land cover."),
        ("conditiondem", "Preview the DEM conditioning...",
         "What the culvert / channel / pond conditioning does to the DEM around the dam."),
    )),
    ("Results", (
        ("products", "Flood polygons and EAP map from rasters...", "Polygons, arrival contours, map."),
        ("consequences", "Life safety and damage estimate...", "Incremental life safety and damage."),
        ("ndhazardclass", "ND hazard classification (Section 5.2)...",
         "The Runner's own preview of the ND 5.2 class with the ACER TM 11 charts. ND Dam Hazard "
         "is the classification authority."),
        ("classify", "Hand the results to the hazard classification...",
         "Runs the ND Dam Hazard classification on the dam's folder (DAM_FOLDER only)."),
        ("animation", "Animation of the flood...", "GIF / MP4 / time-enabled layers."),
        ("submittal", "Submittal report (Word)...", "The report with figures, checklist and files."),
        ("benchmark", "Compare with a reference model (HEC-RAS)...", "Rasters against a reference model."),
        ("hecrasexport", "Export to HEC-RAS...", "The breach hydrograph and breach table."),
        ("hecrasmodel", "Build a HEC-RAS 2D model...",
         "A complete HEC-RAS project: reservoir, dam with the breach, 2D area below."),
        ("hecrasimport", "Import HEC-RAS results...", "HEC-RAS 2D results as rasters."),
        ("validationrecord", "Validation record (Word)...", "From a benchmark comparison."),
    )),
    ("Checks", (
        ("verifymanifest", "Verify a run manifest...", "Are the inputs and results unchanged?"),
        ("verification", "Verification against analytical solutions...", "Analytic dam-break tests."),
        ("check", "Check the model installation...", "LISFLOOD-FP and ANUGA found and working?"),
        ("setupanuga", "Set up ANUGA (one-time download)...",
         "Installs ANUGA in its own private environment; no conda needed."),
        ("selftest", "Self-test in this QGIS...", "A short run of the whole chain in this QGIS."),
    )),
)
ALL_TOOLS = tuple(t for _tab, tools in TABS for t in tools)
TOOL_LABEL = {t[0]: t[1] for t in ALL_TOOLS}

# which parameters carry the dams folder / the dam
WITH_DAM = frozenset({"rundam", "estimate", "sensitivity", "nested", "probabilistic"})   # + DEM
WITH_DAMS_FOLDER = frozenset({"batch", "cascade"})                                       # + DEM
WITH_DAM_FOLDER = frozenset({"products", "consequences", "ndhazardclass", "animation", "benchmark",
                             "hecrasexport", "hecrasimport", "hecrasmodel",
                             "submittal"})              # DAM_FOLDER
WITH_DAM_ID = frozenset({"products", "submittal"})
WITH_DEM = frozenset({"conditiondem", "animation"})
# 3.9.0: tools whose empty DEM is the dam folder's ("Get data for this dam"; dam.json's data
# section). Not animation (empty = the model's own DEM) nor batch / cascade (many dams).
DEM_FROM_DAM_FOLDER = WITH_DAM | {"conditiondem"}
FROM_DAM_FOLDER = "(from dam folder)"

DEFAULT_SETTINGS = {"dams_folder": "", "dam_id": "", "dem": "", "settings_file": "",
                    "grid_m": 10.0, "simh": 0.0, "simmax": 96.0, "force_anuga": False}
PLUGIN_KEY = "ndbreach"                 # the suite settings file key (damsuite.versions)
TAB_ORDER = tuple(t for t, _tools in TABS)


def settings_group(alg):
    """(settings tab, order) of a tool: the popup tab it sits on; tools of no tab last."""
    for i, (tab, tools) in enumerate(TABS):
        for j, (a, _label, _tip) in enumerate(tools):
            if a == alg:
                return tab, j
    return (TAB_ORDER[-1], 99)


def dam_folder(dams_folder, dam_id):
    """<Dams folder>/<Dam ID> as the tools name it ('' when either is empty)."""
    if not dams_folder or not str(dam_id or "").strip():
        return ""
    return os.path.join(dams_folder, damjson.folder_name(str(dam_id).strip()))


def dam_data_dem(folder):
    """3.9.0: the DEM recorded in the dam folder's dam.json data section ("Get data for this
    dam", the DEM download box) when its file exists, else ""."""
    if not folder or not os.path.isdir(folder):
        return ""
    try:
        from .damsuite import damdata as DD
        return DD.dam_data(folder).get("dem", "")
    except Exception:                                            # noqa - an unreadable dam.json
        return ""


def dem_for(settings, folder):
    """3.9.0: (DEM, from_dam_folder) for the tools: the window's own choice, else the dam
    folder's DEM. The window's choice is never replaced."""
    own = str((settings or {}).get("dem") or "").strip()
    if own:
        return own, False
    got = dam_data_dem(folder)
    return got, bool(got)


def list_dams(dams_folder):
    """Names of the folders in the dams folder that hold a dam: a dam.json, a model folder or
    final rasters."""
    names = []
    try:
        entries = sorted(os.listdir(dams_folder))
    except OSError:
        return names
    for n in entries:
        p = os.path.join(dams_folder, n)
        if not os.path.isdir(p):
            continue
        if (os.path.isfile(os.path.join(p, "dam.json")) or os.path.isdir(os.path.join(p, workflow.MODEL_FOLDER))
                or os.path.isfile(os.path.join(p, workflow.FINAL_NAMES["depth"]))):
            names.append(n)
    return names


def _file(folder, *parts):
    return os.path.isfile(os.path.join(folder, *parts))


def _dir(folder, *parts):
    p = os.path.join(folder, *parts)
    try:
        return os.path.isdir(p) and any(True for _ in os.scandir(p))
    except OSError:
        return False


def dam_status(folder):
    """[(key, label, state, detail)] for a dam folder. state: 'yes', 'part', 'no'."""
    rows = []
    if not folder or not os.path.isdir(folder):
        return rows
    fin = workflow.FINAL_NAMES
    have = [k for k in ("depth", "velocity", "arrival") if _file(folder, fin[k])]
    rows.append(("run", "Flood rasters (depth, velocity, arrival)",
                 "yes" if len(have) == 3 else "part" if have else "no",
                 "" if len(have) == 3 else ("only " + ", ".join(have)) if have else "not run yet"))
    base = [k for k in ("depth", "velocity", "arrival") if _file(folder, workflow.BASELINE_NAMES[k])]
    rows.append(("baseline", "Baseline (no-breach) rasters", "yes" if len(base) == 3 else "part" if base else "no",
                 "" if len(base) == 3 else "none (a sunny-day run has none)" if not base else
                 "only " + ", ".join(base)))
    rows.append(("incremental", "Incremental depth",
                 "yes" if _file(folder, workflow.INCREMENT_NAMES["depth"]) else "no", ""))
    manifest = _file(folder, *workflow.MODEL_FOLDER.split("/"), "run_manifest.json")
    rows.append(("manifest", "Run manifest (SHA-256 of inputs and results)", "yes" if manifest else "no",
                 "" if manifest else "written by a run of this version"))
    rows.append(("report", "Run report", "yes" if _file(folder, workflow.MODEL_FOLDER, "run_report.json")
                 else "no", ""))
    prod = any(_file(folder, n) for n in ("Breach Products.gpkg", "Flood Extent.geojson",
                                          "Flood Polygons (Max Depth).geojson"))
    rows.append(("products", "Flood polygons / products", "yes" if prod else "no", ""))
    ens = _file(folder, "Probabilistic", "Runs", "index.json") or _file(folder, "Probabilistic",
                                                                         "Probabilistic_Summary.csv")
    rows.append(("ensemble", "Probabilistic runs", "yes" if ens else "no",
                 "" if _file(folder, "Probabilistic", "Runs", "index.json") or not ens else
                 "no per-run rasters: ND Dam Hazard cannot give a class probability"))
    rows.append(("sensitivity", "Sensitivity runs", "yes" if _dir(folder, "Sensitivity") else "no", ""))
    rows.append(("nested", "Nested-grid run", "yes" if _dir(folder, "Nested") else "no", ""))
    rows.append(("preview", "ND 5.2 preview class (Runner's own)",
                 "yes" if _file(folder, workflow.STALE_ON_RERUN[1]) else "no", ""))
    rows.append(("handoff", "Hand-off to the hazard classification",
                 "yes" if _dir(folder, "_classification") else "no", ""))
    rows.append(("animation", "Animation", "yes" if _dir(folder, "Animation") else "no", ""))
    rows.append(("hecras", "HEC-RAS files", "yes" if _dir(folder, "_hecras") else "no", ""))
    return rows


def status_lines(rows):
    mark = {"yes": "✓", "part": "◐", "no": "·"}
    return ["{} {}{}".format(mark[st], label, " - " + detail if detail else "")
            for _k, label, st, detail in rows]


def dam_record_text(folder):
    """(one line, next-action code) of the dam folder's dam.json."""
    if not folder or not os.path.isdir(folder):
        return "dam.json: no dam folder", ""
    try:
        from .damsuite import dam_record as DR
        rec = DR.load(folder)
    except ValueError as e:
        return "dam.json: cannot be read ({})".format(e), ""
    except Exception as e:                                       # noqa
        return "dam.json: not available ({})".format(e), ""
    if rec is None:
        return ("dam.json: none yet (written by the next run, ND Dam Breach Screening tool 7b or "
                "ND Dam Hazard)"), ""
    code, why = DR.next_action(rec)
    steps = []
    for step in DR.STEPS:
        e = rec.latest(step)
        if e:
            steps.append("{} {} ({})".format(step, (e.get("when") or "")[:10], e.get("tool") or "?"))
    disputed = len([c for c in rec.conflicts if not c.get("applied")])
    return ("dam.json: {}; next: {}{}".format(
        ", ".join(steps) or "no status", why or code.upper(),
        " - {} value(s) disputed".format(disputed) if disputed else "")), code


def suggest(rows, code=""):
    """(tool, text): the tool the dam most likely needs next, from the folder and dam.json's
    next-action code. Only a suggestion."""
    st = {k: s for k, _l, s, _d in rows}
    if not rows or st.get("run") != "yes" or code == "model":
        return "rundam", "Run the breach model."
    if code == "classify" or (st.get("handoff") != "yes" and code in ("", "none")):
        return "classify", "Hand the results to the ND Dam Hazard classification."
    if code == "review":
        return "classify", "Open ND Dam Hazard's reviewer's view; its classification has open review items."
    return None, "Nothing is waiting for this dam. The submittal report is on the Results tab."


def defaults(alg, dams_folder, dam_id, settings=None):
    """The parameters to fill into tool `alg` for the dam."""
    S = dict(DEFAULT_SETTINGS)
    S.update(settings or {})
    dams = (dams_folder or "").strip()
    did = str(dam_id or "").strip()
    folder = dam_folder(dams, did)
    p = {}

    def put(k, v):
        if v not in (None, ""):
            p[k] = v

    if alg in WITH_DAM:
        put("DAMS_FOLDER", dams)
        put("DAM_ID", did)
    if alg in WITH_DAMS_FOLDER:
        put("DAMS_FOLDER", dams)
    if alg in WITH_DAM or alg in WITH_DAMS_FOLDER or alg in WITH_DEM or alg == "conditiondem":
        put("DEM", (S.get("dem") or "").strip())
    if alg in DEM_FROM_DAM_FOLDER and "DEM" not in p:            # 3.9.0
        put("DEM", dem_for(S, folder)[0])
    if alg == "getdamdata":                                      # 3.9.0
        put("DAMS_FOLDER", dams)
        put("DAM_ID", did)
    if alg in WITH_DAM_FOLDER and folder and os.path.isdir(folder):
        put("DAM_FOLDER", folder)
    if alg in WITH_DAM_ID:
        put("DAM_ID", did)
    if alg == "classify" and folder and os.path.isdir(folder):
        p.update({"MODE": 0, "FOLDER": folder})
        put("DAM_ID", did)
    if alg == "verifymanifest" and folder and os.path.isdir(folder):
        put("TARGET", folder)
    if alg == "validationrecord" and folder and _file(folder, "Benchmark_Summary.csv"):
        put("BENCH_FOLDER", folder)
        put("DAM_ID", did)
    if alg == "hecrasimport" and folder and os.path.isdir(folder):
        put("DAM_FOLDER", folder)
    if alg == "rundam":
        put("CELL", float(S["grid_m"] if S.get("grid_m") is not None else 10.0))
        put("DURATION_HR", float(S["simh"] if S.get("simh") is not None else 0.0))
        put("DURATION_MAX_HR", float(S["simmax"] if S.get("simmax") is not None else 96.0))
        if S.get("force_anuga"):
            p["FORCE_ANUGA"] = True
            p["TIER"] = 1
    return p


# ------------------------------------------------------ the suite's Dams folder (3.9.0) --
def suite_dams_folder():
    """The suite's Dams folder (Plugins > ND Dam Suite > Suite data and servers; shared by the
    three plugins), "" when none is set or it cannot be read."""
    try:
        from .damsuite import suitedata as SD
        return SD.dams_folder() or ""
    except Exception:                                            # noqa
        return ""


def remember_dams_folder(dams):
    """The window's Dams folder becomes the suite's when the suite has none yet (a Dams folder
    the suite already has is never replaced). True when it was set."""
    dams = str(dams or "").strip()
    if not dams or not os.path.isdir(dams) or suite_dams_folder():
        return False
    try:
        from .damsuite import suitedata as SD
        SD.set_dams_folder(dams)
        return True
    except Exception:                                            # noqa
        return False


# ---------------------------------------------------------- the DEM download box (3.9.0) --
DEM_CELL_M = 10.0          # the box's starting cell (Screening tool 20's own default)
DEM_HALF_MI = 30.0         # damdata.DEFAULT_DEM_HALF_MI: half of the run tool's 60 mi largest domain
DEM_MAX_HALF_MI = 50.0     # the box's largest buffer


def dem_box_half_mi(max_extent_mi=None):
    """Buffer (mi) of the DEM downloaded around the dam: half the run tool's largest domain side
    (MAX_EXTENT_MI, from the suite settings file; 60 mi gives 30 mi), within 1 - 50 mi."""
    try:
        v = float(max_extent_mi) / 2.0
    except (TypeError, ValueError):
        return DEM_HALF_MI
    if not v > 0.0:
        return DEM_HALF_MI
    return max(1.0, min(DEM_MAX_HALF_MI, v))


def settings_max_extent_mi():
    """MAX_EXTENT_MI of 'Run breach model for one dam' in the current suite settings file, or
    None (no file, or the starting value is used)."""
    try:
        from .damsuite import settings as DS
        st = DS.load_current()
        return None if st is None else st.get(PLUGIN_KEY, "rundam", "MAX_EXTENT_MI")
    except Exception:                                            # noqa
        return None


def _number(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if x == x and abs(x) != float("inf") else None


def dam_lonlat(folder, to_lonlat=None):
    """(lon, lat) of the dam from its dam.json: lon / lat, else x / y in crs through
    `to_lonlat(x, y, crs)` (QGIS's transform; the window gives it). None when there is none."""
    if not folder or not os.path.isdir(folder):
        return None
    try:
        from .damsuite import dam_record as DR
        rec = DR.load(folder)
    except Exception:                                            # noqa - an unreadable dam.json
        return None
    if rec is None:
        return None
    lon, lat = _number(rec.get("lon")), _number(rec.get("lat"))
    if lon is not None and lat is not None and abs(lon) <= 180.0 and abs(lat) <= 90.0:
        return lon, lat
    x, y, crs = _number(rec.get("x")), _number(rec.get("y")), str(rec.get("crs") or "").strip()
    if to_lonlat is None or x is None or y is None or not crs:
        return None
    try:
        got = to_lonlat(x, y, crs)
        lon, lat = _number(got[0]), _number(got[1])
    except Exception:                                            # noqa
        return None
    if lon is None or lat is None or abs(lon) > 180.0 or abs(lat) > 90.0:
        return None
    return lon, lat


def lonlat_text(lonlat):
    """'lon,lat' for the box's dam point ('' for None)."""
    return "" if not lonlat else "{:.6f},{:.6f}".format(lonlat[0], lonlat[1])


def dam_data_dir(folder):
    """<dam folder>/data (where "Get data for this dam" and the DEM download write)."""
    return os.path.join(folder, "data") if folder else ""


def dem_download_target(folder, cell_m):
    """<dam folder>/data/dem_<cell>m.tif, the downloaded DEM of the dam."""
    cell = _number(cell_m) or DEM_CELL_M
    return os.path.join(dam_data_dir(folder), "dem_{:g}m.tif".format(cell))


def finish_dem_download(result, folder, cell_m, translate=None, source=""):
    """After the DEM download into <dam folder>/data/: the DEM becomes data/dem_<cell>m.tif (a
    GeoTIFF there is renamed; a VRT is written to it by `translate(src, dst)` - GDAL in QGIS -
    and kept as it is when that is not possible) and is recorded as the dam's "dem" in dam.json's
    data section. A result outside the dam's data folder is used and recorded where it is.
    Returns (path of the DEM, notes); ("", notes) when the download gave no file."""
    notes = []
    src = str(result or "").split("|")[0]
    if not src or not os.path.isfile(src):
        return "", ["The download gave no DEM file."]
    path = src
    if not folder or not os.path.isdir(folder):
        return path, notes
    target = dem_download_target(folder, cell_m)
    inside = os.path.normcase(os.path.dirname(os.path.abspath(src))) == \
        os.path.normcase(os.path.abspath(dam_data_dir(folder)))
    if inside and os.path.normcase(os.path.abspath(src)) != os.path.normcase(os.path.abspath(target)):
        try:
            if os.path.splitext(src)[1].lower() in (".tif", ".tiff"):
                os.replace(src, target)
                path = target
            elif translate is not None:
                translate(src, target)
                if os.path.isfile(target):
                    path = target
                    notes.append("DEM written as {} (from {})".format(
                        os.path.basename(target), os.path.basename(src)))
        except Exception as e:                                   # noqa - the download is kept
            notes.append("The DEM stays {} ({} not written: {})".format(
                os.path.basename(src), os.path.basename(target), e))
            path = src
    try:
        from .damsuite import dam_record as DR
        from .damsuite import damdata as DD
        rec = DR.load(folder, create=True)
        DD.set_entry(rec, "dem", path, source or "DEM download of the ND Dam Breach Runner "
                                                 "window (ND Dam Breach Screening tool 20)")
        rec.save()
        notes.append("dam.json: DEM recorded ({})".format(DD.rel_path(folder, path)))
    except Exception as e:                                       # noqa - the DEM is there anyway
        notes.append("dam.json: the DEM was not recorded ({})".format(e))
    return path, notes


RESULT_RASTERS = tuple(workflow.FINAL_NAMES.values()) + tuple(workflow.BASELINE_NAMES.values()) + \
    tuple(workflow.INCREMENT_NAMES.values())
RESULT_VECTORS = ("Flood Extent.geojson", "Flood Polygons (Max Depth).geojson", "Arrival Zones.geojson",
                  "Incremental Depth Polygons.geojson")


def result_files(folder):
    """[(kind, path)] of the dam folder's result files that exist, rasters first, in the order the
    Runner writes them (for 'Add the dam's results to the project')."""
    out = []
    if not folder or not os.path.isdir(folder):
        return out
    for n in RESULT_RASTERS:
        p = os.path.join(folder, n)
        if os.path.isfile(p):
            out.append(("raster", p))
    for n in RESULT_VECTORS:
        p = os.path.join(folder, n)
        if os.path.isfile(p):
            out.append(("vector", p))
    return out
