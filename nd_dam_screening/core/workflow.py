"""The logic behind the ND Dam Breach Screening popup window - pure Python, no Qt, no QGIS.

The window is a guide through the tools, not a second implementation of them. One WORK FOLDER
holds every tool's output under a fixed naming (see LAYOUT); the window reads what is there to
show which steps are done, and opens the Processing dialog of the next tool with its
parameters filled in: the output of the step before, the layers of the data package, and the
settings chosen in the window. Nothing is run by the window itself - every tool runs from its
own Processing dialog (background task, Cancel, log, history), where every filled-in value can be
changed.

The file names are a convention of this plugin, not a requirement of anything else: a step can
be run from the Processing toolbox with any other file names, and the window then simply shows
that step as not done.
"""
import csv
import json
import os
import time as _time

PROVIDER = "nddam"

# every tool's output in the work folder (relative paths). Keys: step -> {parameter: path}
LAYOUT = {
    "0": {"OUT": "0_data_package.gpkg"},
    "1": {"OUT": "1_dams.gpkg"},
    "2": {"OUT": "2_terrain"},
    "3": {"CURVES": "3_stage_storage", "OUT": "3_dams_storage.gpkg"},
    "4": {"OUTP": "4_breach_points.gpkg", "OUTL": "4_flow_arrows.gpkg"},
    "5": {"OUTDIR": "5_hydrographs", "OUT": "5_dams_breach.gpkg"},
    "6": {"OUTDIR": "6_triage_envelopes", "OUT": "6_triage.gpkg"},
    "7": {"OUTDIR": "7_simulations", "OUT": "7_simulations.gpkg"},
    "7b": {"DAMS_FOLDER": "Dams", "OUT": "7b_runner_results.gpkg"},
    "8": {"OUT": "8_hazard.gpkg", "IMP": "8_impacted.gpkg"},
    "9": {"OUTDIR": "9_sensitivity", "OUT": "9_sensitivity.gpkg"},
    "10": {"OUTDIR": "10_reports"},
    # 0.11
    "11": {"OUTDIR": "11_benchmark"},
    "12": {"OUT_CSV": "12_runtime_estimate.csv"},
    "13": {"OUT_CSV": "13_inventory_changes.csv", "OUT": "13_changed_dams.gpkg"},
    "14": {"REPORT": "14_selftest_report.txt"},
    "15": {"LOG": "15_chain_log.txt"},
    # 0.12: ponds and dugouts (does the work of 1-4 for water bodies; use a work folder of its own)
    "16": {"OUT": "16_ponds.gpkg", "OUTP": "16_pond_breach_points.gpkg", "OUTL": "16_pond_flow_arrows.gpkg",
           "OUTX": "16_pond_excluded.gpkg", "TERRAIN": "16_terrain", "CURVES": "16_stage_storage"},
    # 0.13: the preflight check; the merge of dams (1-4) and ponds (16) for the Simple tab's "Both"
    "17": {"REPORT": "17_preflight.txt"},
    "18": {"OUT": "18_dams_all.gpkg", "OUTP": "18_breach_points_all.gpkg", "OUTL": "18_flow_arrows_all.gpkg",
           "CURVES": "18_stage_storage", "TERRAIN": "18_terrain"},
    # 0.14: a proposed embankment drawn as a crest line (stands in for 1-4; its own work folder)
    "19": {"OUT": "19_proposed.gpkg", "OUTP": "19_breach_points.gpkg", "OUTL": "19_flow_arrows.gpkg",
           "CREST": "19_crest_lines.gpkg", "TERRAIN": "19_terrain", "CURVES": "19_stage_storage",
           "SPEC": "19_proposed.json"},
}
FIXED_OUTPUTS = frozenset({"18"})          # steps whose LAYOUT files are written under fixed names (no parameters)
RUN_SETTINGS = "settings_used.xlsx"       # 0.13: the settings a run used (the chain writes it)
REVIEW_LOG = "review_decisions.csv"       # 0.13: the Review tab's decisions
# the file (or folder) whose presence means "this step has been run"
DONE_MARK = {"0": "OUT", "1": "OUT", "2": "OUT", "3": "OUT", "4": "OUTP", "5": "OUT", "6": "OUT",
             "7": "OUT", "7b": "OUT", "8": "OUT", "9": "OUT", "11": "OUTDIR", "12": "OUT_CSV",
             "13": "OUT_CSV", "14": "REPORT", "15": "LOG", "16": "OUT", "17": "REPORT", "18": "OUT",
             "19": "OUT"}
# tool 16 writes what tools 1-4 write (for ponds): with it done, these count as done
POND_COVERS = ("1", "2", "3", "4")
REPORT_INDEX = os.path.join(LAYOUT["10"]["OUTDIR"], "index.html")
SUMMARY_CSV = os.path.join(LAYOUT["10"]["OUTDIR"], "summary.csv")

# step -> (algorithm name, title, optional?, one line)
STEPS = (
    ("0", "datapackage", "Build offline data package", True,
     "Structures, roads, railroads, flowlines, dam lines, lifelines and NID dams in one "
     "GeoPackage with standard fields; or repackage the package ND Dam Hazard exports."),
    ("16", "findponds", "Find ponds and dugouts", True,
     "Alternative to 1-4 for water bodies: the LiDAR tells embankment ponds from dugouts and holes; "
     "a dugout releases only water above the natural ground ('no breach flood' when none)."),
    ("19", "proposedam", "Propose a dam", True,
     "A crest line you draw: the embankment is inserted on a copy of the DEM (the source is not "
     "changed), the height is the crest minus the lowest ground on the line, and the storage is "
     "measured on the pre-dam bed. Takes the place of 1-4. Not part of Run all unless you tick it "
     "— the line cannot be invented."),
    ("1", "preparedams", "Prepare dam inventory", False,
     "ND DWR dams (+ NID) into one standardized dam layer."),
    ("2", "terraincache", "Cache terrain per dam", False,
     "One DEM clip per dam (USGS 3DEP or your local LiDAR tiles)."),
    ("3", "estimatestorage", "Estimate reservoir storage", False,
     "NHD waterbody + DEM: stage-storage curves and storage per dam."),
    ("4", "breachdirection", "Breach point and flow direction", False,
     "Breach point on the DWR dam line and the direction the water leaves."),
    ("18", "mergeponds", "Merge dams and ponds", True,
     "One work folder, both kinds: the DWR dams (1-4) and the ponds (16) as one set of layers for 5-10."),
    ("5", "breachhydrograph", "Breach parameters and hydrograph", False,
     "Breach width / time by the chosen equation and the outflow hydrograph."),
    ("6", "triage", "Quick screen (triage)", True,
     "About a second per dam: clears dams whose generous flood envelope reaches nothing."),
    ("7", "runbreachsim", "Run breach flood simulations", False,
     "2D flood per dam: LISFLOOD-FP or the built-in engine; parallel, resumable."),
    ("7b", "runnerhandoff", "Detailed runs with ND Breach Runner", True,
     "Send the dams that matter to the ND Dam Breach Runner plugin."),
    ("8", "hazardclassification", "Hazard classification", False,
     "ND 5.2 / 5.3 with the ACER TM 11 charts: class, confidence, impacted features."),
    ("9", "sensitivity", "Sensitivity of the class", True,
     "Re-runs borderline dams with other breach equations and Manning n."),
    ("10", "reports", "Reports and statewide summary", False,
     "index.html, summary.csv and a page per dam."),
    ("11", "comparereference", "Compare with a reference model", True,
     "Screening rasters against HEC-RAS / DSS-WISE rasters: fit index, depth and velocity errors."),
    ("12", "estimateruntime", "Estimate the run time", True,
     "Order-of-magnitude time of step 7 for these dams, engine and settings."),
    ("13", "inventorydiff", "Inventory diff", True,
     "What changed since the last inventory: new, removed and changed dams; dam.json 'refresh'."),
    ("14", "selftest", "Self-test in this QGIS", True,
     "A synthetic valley through tools 1-8 and 10: does every tool run here?"),
    ("15", "runchain", "Run the steps in order (headless)", True,
     "The window's chain as a tool for qgis_process / scripts."),
    ("17", "preflight", "Preflight check", True,
     "A few seconds: work folder, disk, terrain service, engine, layers and fields, charts, settings - "
     "what would otherwise fail a run halfway."),
)
KEYS = tuple(s[0] for s in STEPS)
ALG = {s[0]: s[1] for s in STEPS}
TITLE = {s[0]: s[2] for s in STEPS}
OPTIONAL = frozenset(s[0] for s in STEPS if s[3])
HINT = {s[0]: s[4] for s in STEPS}

# steps that must be done before this one makes sense; a tuple inside means "any of these"
REQUIRES = {
    "1": (), "0": (),
    "2": ("1",), "3": ("1", "2"), "4": ("1",),
    "5": ("3",), "6": ("5", "4", "2"),
    "7": (("5", "6"), "4", "2"),
    "7b": (("7", "8"), "4"),
    "8": ("7",), "9": (("3", "5"), "4", "2"), "10": ("8",),
    "11": ("7",), "12": (("5", "6"),), "13": ("1",), "14": (), "15": (), "16": (), "17": (),
    "18": ("3", "4", "16"), "19": (),
}

# settings slots that hold one of the data layers (a layer id / path chosen in the Inputs tab,
# else the package layer of the same meaning)
LAYER_SLOTS = (
    # key, label, package layer
    ("dwr", "ND DWR dams (tool 1)", None),
    ("nid", "NID dams (tool 1)", None),
    ("structures", "Structures", "structures"),
    ("roads", "Roads", "roads"),
    ("rail", "Railroads", "railroads"),
    ("lifelines", "Lifeline facilities", "lifelines"),
    ("streams", "NHD flowlines (stream lines)", "flowlines"),
    ("waterbodies", "NHD waterbodies", "waterbodies"),
    ("dam_lines", "DWR dam lines", "dam_lines"),
    ("nlcd", "NLCD land cover (raster, optional)", None),
)
PKG_LAYER = {k: p for k, _l, p in LAYER_SLOTS if p}
# tool 0's own inputs for the same slots
TOOL0_SLOT = {"streams": "FLOW", "waterbodies": "WB", "dam_lines": "DAML", "structures": "BLD",
              "roads": "ROADS", "rail": "RAIL", "lifelines": "LIFE", "nid": "NIDD"}
# fields of a tool-0 package layer (algorithms/data_package.PKG_FIELDS plus flags)
PKG_FIELDS = {"structures": {"occ_cls": "occ_cls", "prim_occ": "prim_occ", "mobile": "mobile",
                             "habitable": "habitable"},
              "roads": {"aadt": "aadt", "road_class": "road_class"},
              "railroads": {"passenger": "passenger", "trains_day": "trains_day"}}

# what the steps cannot work without, besides earlier steps: slot names
NEEDS_LAYER = {"3": ("waterbodies",), "4": ("dam_lines",), "8": ("structures",), "16": ("waterbodies",)}

# defaults of the settings the window keeps (saved with the QGIS project)
DEFAULT_SETTINGS = {
    "work": "", "package": "",
    "counties": "",           # 0.21.1: the county (comma-separated names; "" = all of North Dakota)
    "terrain_source": 0,      # tool 2: 0 USGS 3DEP, 1 local DEM folder
    # 0.28.0: tool 7: 0 cached clips only, 1 3DEP, 2 local, 3 follow the terrain source (the
    # default). "Cached clips only" was the default and it truncated floods in silence: a
    # 3,937 ac-ft dam ran its flood off a 3 km clip and screened Medium with 0 buildings, where
    # the same dam with clip growth allowed screened High with 27 buildings. A clip that cannot
    # grow is a flood that stops at its edge, so the default now follows the terrain the run
    # already has (larger_clips_index).
    "larger_clips": 3,
    "local_dem": "", "engine": 2, "exe": "", "scenario": 0, "workers": 0,   # engine 2 = built-in (0.13: no install)
    "crit": 0, "chart_file": "", "runner_dem": "", "dams_folder": "", "clip": "",
    # 0.11: tool 7 no-breach baseline / buildings, tool 8 incremental, tools 11 / 13 inputs
    "baseline": False, "baseline_cfs": 0.0,
    "buildings": 0,           # 0 bare terrain, 1 raise the DEM, 2 Manning's n
    "building_raise_ft": 10.0, "building_n": 0.15,
    "incremental": False,
    # step 2 writes the DEM; the flood grid is its own size (0 = automatic)
    # 0.28.0: dem_cell_ft follows the SOURCE cell (3DEP and the suite's DEM downloads are 10 m =
    # 32.8 ft). At 3.3 ft it resampled a 10 m source to 1 m - 9 million cells for a 3 km box that
    # step 7 then ran at 50 m. A local 1 m LiDAR folder is finer than this: set it to 3.3 there.
    "dem_cell_ft": 32.8, "grid_ft": 33.0, "simh": 12.0, "simmax": 96.0,      # 0.18.0: feet (was dem_cell / grid_m in m)
    # 0.28.0: the resolution preset of step 7 (0 Quick 50 m, 1 Standard 20 m, 2 Detailed 10 m,
    # 3 Custom = the Grid size box). The window never passed it, so every run took QGIS's own
    # default - Quick - and the Grid size box decided nothing (core/runplan.effective_cell).
    "preset": 0,
    # 0.28.0: step 7 runs top of dam AND sunny day and the worse class governs (core/scenarios).
    # Two settings, not one, because the honest default differs: a one-dam run turns a minute into
    # two, a county turns hours into days.
    "both_scenarios": False,  # a county run
    "both_one_dam": True,     # "Run one dam..." / "Run selected dams..."
    "skip_done": True,        # 0.28.0: step 7 skips dams that already have a result (core/resume)
    "ref_folder": "",         # tool 11: reference rasters (per-dam sub-folders / files by dam ID)
    "ref_units": 0,           # tool 11: 0 US (ft, ft/s, h), 1 metric
    "old_inventory": "",      # tool 13: the previous tool-1 layer / DWR export
    # 0.13: the suite settings file (damsuite.settings), what the Simple tab screens, the reviewer
    "settings_file": "",
    "screen_what": 0,         # 0 DWR dam inventory (1-4), 1 ponds and dugouts (16), 2 both
    "chain": [],              # optional steps the chain includes (CHAIN_OPTIONAL keys)
    "reviewer": "",
    # 0.19.0: tool 16 on DWR dugouts (DWR_Dugouts points joined to DWR_Dugouts_Polygons by number)
    "dugout_records": "", "wb_id": "", "pond_max_ac": "",
    "wb_search_ft": "",       # tool 3 waterbody search distance (empty = the tool's 500 ft)
    # 0.20.0: tool 7 enlarges the clip before the flood run when the downstream trace reaches its
    # edge only when asked (PREGROW); otherwise the clip grows after a run that reaches the edge.
    # 0.28.0: on by default, with the larger-clip source above - a flood cut off at the clip edge
    # is the one error that reads as a finished answer. It costs time (a pre-grown clip is larger
    # before the first run); switch it off for a big county and let the clip grow after a run.
    "pregrow": True,
    # 0.28.3: the suite data folder (the offline pack, the dwr/ drop-in folder) for a run that
    # has no QGIS setting to read - a script, qgis_process, another computer. Empty = the folder
    # set in Plugins > ND Dam Suite > Suite data and servers (or NDSUITE_DATA).
    "suite_folder": "",
}
LEGACY_METRIC = (("dem_cell", "dem_cell_ft"), ("grid_m", "grid_ft"))     # (old key in m, new key in ft)


def migrate_settings(s):
    """0.18.0: settings saved before the feet window (dem_cell / grid_m in metres) as the new
    keys (dem_cell_ft / grid_ft); the new key wins when both are there. A copy."""
    s = dict(s or {})
    for old, new in LEGACY_METRIC:
        if old in s:
            v = s.pop(old)
            if s.get(new) in (None, ""):
                try:
                    s[new] = round(float(v) / 0.3048, 1)
                except (TypeError, ValueError):
                    pass
    return s


# the window's choices that go to the settings file (not paths of this project / computer)
WINDOW_KEYS = ("terrain_source", "larger_clips", "engine", "scenario", "workers", "crit", "baseline",
               "baseline_cfs", "buildings", "building_raise_ft", "building_n", "incremental", "ref_units",
               "screen_what", "chain", "dem_cell_ft", "grid_ft", "simh", "simmax",
               "preset", "pregrow", "both_scenarios", "both_one_dam", "skip_done")   # 0.28.0

# 0.28.0: "Step 7 terrain for larger clips" = follow the terrain source. The tool's own enum is
# 0 cached clips only / 1 USGS 3DEP / 2 local DEM folder; this fourth value is the window's, and
# it resolves to whichever terrain the run is already using, so the clip can grow when the flood
# reaches its edge instead of the flood stopping there.
CLIPS_CACHE, CLIPS_3DEP, CLIPS_LOCAL, CLIPS_FOLLOW = 0, 1, 2, 3


def larger_clips_index(settings):
    """The TSRC value step 7 gets (0 cache, 1 3DEP, 2 local): the chosen one, or - with "follow
    the terrain source" - the source step 2 used. A local folder is only followed when one is
    given; without it 3DEP is the fallback, because a clip that cannot grow is a truncated
    flood and that is the worse of the two failures."""
    S = settings or {}
    try:
        v = int(S.get("larger_clips") if S.get("larger_clips") is not None else CLIPS_FOLLOW)
    except (TypeError, ValueError):
        v = CLIPS_FOLLOW
    if v != CLIPS_FOLLOW:
        return v if v in (CLIPS_CACHE, CLIPS_3DEP, CLIPS_LOCAL) else CLIPS_CACHE
    local = (S.get("local_dem") or "").strip()
    if int(S.get("terrain_source") or 0) == 1 and local:
        return CLIPS_LOCAL
    return CLIPS_3DEP


def larger_clips_text(settings):
    """One line for the log and the preflight: which terrain a larger clip comes from."""
    i = larger_clips_index(settings)
    v = (settings or {}).get("larger_clips")
    try:
        chosen = int(v if v is not None else CLIPS_FOLLOW)
    except (TypeError, ValueError):
        chosen = CLIPS_FOLLOW
    how = " (follows the terrain source)" if chosen == CLIPS_FOLLOW else ""
    return {CLIPS_CACHE: "step 7 larger clips: cached clips only - a flood that reaches the clip "
                         "edge is CUT OFF there and the class can be too low",
            CLIPS_3DEP: "step 7 larger clips: USGS 3DEP" + how,
            CLIPS_LOCAL: "step 7 larger clips: your local DEM folder" + how}[i]


def dam_count(work):
    """How many dams the run is about (the step 1 / 18 / 16 table), or None when none is
    written yet. Used for the time estimate before a run."""
    from . import runplan as rpl
    for step in ("18", "1", "16"):
        p = out(work, step) if work else None
        n = rpl.count_rows(p) if p else None
        if n:
            return n
    return None
for _k, _l, _p in LAYER_SLOTS:
    DEFAULT_SETTINGS[_k] = ""


# ------------------------------------------------------------------ paths --
def path(work, step, param):
    """Absolute path of a step's output in the work folder."""
    return os.path.join(work, LAYOUT[step][param])


def dams_folder(work, settings):
    """The folder of dam folders (tool 7b output, tool 8's dam.json status): the chosen folder,
    else <work>/Dams."""
    return (settings.get("dams_folder") or "").strip() or path(work, "7b", "DAMS_FOLDER")


def _nonempty_dir(p):
    try:
        if not os.path.isdir(p):
            return False
        with os.scandir(p) as it:                # closed, so no ResourceWarning per call
            return any(True for _ in it)
    except OSError:
        return False


def exists(work, step, param):
    p = path(work, step, param)
    return _nonempty_dir(p) if os.path.isdir(p) or not os.path.splitext(p)[1] else os.path.isfile(p)


def out(work, step, param="OUT"):
    """The output's path when it exists, else None."""
    return path(work, step, param) if work and exists(work, step, param) else None


def is_done(work, step):
    if not work or not os.path.isdir(work):
        return False
    if step == "10":
        return os.path.isfile(os.path.join(work, REPORT_INDEX))
    if exists(work, step, DONE_MARK[step]):
        return True
    # 0.12 ponds, 0.14 a proposed dam: either one writes what tools 1-4 write
    return (step in POND_COVERS and bool(work) and os.path.isdir(work) and (
        exists(work, "16", DONE_MARK["16"]) or exists(work, "19", DONE_MARK["19"])))


def covered_by_ponds(work, step):
    """True when `step` (1-4) counts as done only because tool 16 ran in this work folder."""
    return (step in POND_COVERS and bool(work) and os.path.isdir(work)
            and not exists(work, step, DONE_MARK[step]) and exists(work, "16", DONE_MARK["16"]))


def covered_by_proposed(work, step):
    """True when `step` (1-4) counts as done only because tool 19 ran in this work folder."""
    return (step in POND_COVERS and bool(work) and os.path.isdir(work)
            and not exists(work, step, DONE_MARK[step]) and exists(work, "19", DONE_MARK["19"]))


def _mtime(work, step):
    try:
        if step == "10":
            return os.path.getmtime(os.path.join(work, REPORT_INDEX))
        return os.path.getmtime(path(work, step, DONE_MARK[step]))
    except OSError:
        return None


# ---------------------------------------------------------------- package --
def _phrase(label):
    """A slot label in running text: no '(tool 1)' note, first letter lower case unless it
    starts an acronym ('NHD flowlines', 'Roads' -> 'roads')."""
    label = label.split(" (")[0]
    return label[0].lower() + label[1:] if len(label) > 1 and label[1].islower() else label


def package_layers(pkg):
    """Table names of a GeoPackage, {} when it cannot be read."""
    try:
        from . import package as pk
        return pk.gpkg_layers(pkg)
    except Exception:                                            # noqa
        return {}


def package_path(work):
    """The tool-0 output when it exists."""
    return out(work, "0")


def slot_source(slot, work, settings, log=None, prepare=False, suite=True):
    """The layer to give a tool for a data slot: the one chosen in the Inputs tab, else the
    tool-0 package's layer, else (0.16.0) the suite data - see slot_origin - else None. A package
    layer is '<file>|layername=<name>'. With settings['package_work'] (a one-dam run), the package
    is looked for in that folder. `log`: told where a slot's layer came from when it is not the
    user's choice or the package; `prepare`: write the offline pack's clip now (else its path is
    only named); suite=False: the user's choice or the package only (inputs that change the
    model rather than give it data - see MODEL_SLOTS)."""
    value, origin = slot_origin(slot, work, settings, prepare=prepare, log=log)
    if not suite and origin not in ("chosen", "package"):
        return None
    if log and value and origin in ORIGIN_TEXT and origin != "package":
        label = dict((k, l) for k, l, _p in LAYER_SLOTS).get(slot, slot)
        log("{}: {} ({})".format(_phrase(label), value, ORIGIN_TEXT[origin]))
    return value


def _package_source(slot, work, settings):
    name = PKG_LAYER.get(slot)
    pkg_work = (settings.get("package_work") or "").strip() or work
    pkg = package_path(pkg_work) if pkg_work else None
    if name and pkg and name in package_layers(pkg):
        return "{}|layername={}".format(pkg, name)
    return None


def _from_package(slot, work, settings):
    """True when the slot's value comes from the tool-0 package (so its standard field names
    are known)."""
    return slot_origin(slot, work, settings)[1] == "package"


# ------------------------------------------------------------- suite data --
# 0.16.0: a data slot with no layer chosen and no tool-0 package falls back to, in this order:
#   (a) the dam folder's data (dam.json's data section, written by "Get data for this dam") when
#       the run is about one dam (a one-dam run whose dam has a folder, or a work folder that is
#       a dam folder);
#   (b) the suite offline pack in the suite data folder, clipped to the work area into
#       <work>/0_suite_data/<slot>.gpkg (once; written again when the pack is newer);
#   (c) the files the user dropped into <suite data folder>/dwr/ (DWR dams, DWR dam lines).
# A layer the user chose is never replaced; the run log says which source was used.
# 0.22.0: (b) is the suite's offline pack wherever it is (suitedata.active_pack: the pack
# location of Suite data and servers, else the data folder's pack; a pack folder with one
# GeoPackage per layer also holds the DWR files). With Data source = Download the pack is not
# read: (b) becomes the server chosen for that kind of data in Suite data and servers, fetched
# for the work area into the same <work>/0_suite_data/<slot>.gpkg.
SLOT_ROLE = {"dwr": "dwr_dams", "nid": "nid_dams", "structures": "structures", "roads": "roads",
             "rail": "rail", "lifelines": "lifelines", "streams": "flowlines",
             "waterbodies": "waterbodies", "dam_lines": "dwr_dam_lines", "nlcd": "nlcd"}
SUITE_DIR = "0_suite_data"
# inputs that change the model, not only give it data: road-crossing cuts along the stream lines,
# the dam lines protected from them (and the cascade), Manning's n from land cover, buildings in
# the terrain (steps 6, 7, 9). They are never filled from the suite data - only the user's choice
# or the step-0 package, as before 0.16.0.
MODEL_SLOTS = ("streams", "dam_lines", "nlcd")
ORIGIN_TEXT = {"package": "the step-0 data package",
               "dam folder": "the dam folder's data (Get data for this dam)",
               "suite pack": "the suite offline pack, clipped to the work area",
               "download": "downloaded for the work area from the server chosen in Suite data "
                           "and servers (Data source = Download)",
               "dwr folder": "your file in the suite data folder's dwr/",
               "nid folder": "your file in the suite data folder's nid/"}


def _ds(name):
    """A module of the shared damsuite package."""
    try:
        import importlib
        return importlib.import_module("..damsuite." + name, __package__)
    except (ImportError, TypeError, ValueError):                 # core/ itself on sys.path (tests)
        import importlib
        return importlib.import_module("damsuite." + name)


def suite_data_folder():
    """The suite data folder of THIS COMPUTER ("" when none is set). A hook: the tests replace
    it. Prefer settings_data_folder(settings), which lets a run carry its own folder."""
    try:
        return _ds("suitedata").data_folder()
    except Exception:                                            # noqa
        return ""


def settings_data_folder(settings):
    """0.28.3: the suite data folder a run should read: the settings' own "suite_folder" when it
    has one, else this computer's. A headless run (qgis_process, a script, a server with no QGIS
    profile) can then name the folder in the settings JSON it already passes."""
    v = ((settings or {}).get("suite_folder") or "").strip()
    return os.path.expanduser(v) if v else suite_data_folder()


def _runner_name(dam_id):
    """The dam folder name of a dam ID (handoff.safe_name, the Runner's rule)."""
    import re
    return re.sub(r"[^\w\- ]+", "_", str(dam_id)).strip() or "dam"


def dam_folder_in(roots, dam_id):
    """The dam folder (holding dam.json) of a dam ID in one of the Dams folders `roots`, or "".
    A bare DWR structure number (5289) also finds DWR-5289."""
    dam_id = str(dam_id or "").strip()
    if not dam_id:
        return ""
    names = [dam_id] + (["DWR-" + dam_id] if dam_id.isdigit() else [])
    for root in roots:
        if not root or not os.path.isdir(root):
            continue
        for n in names:
            f = os.path.join(root, _runner_name(n))
            if os.path.isfile(os.path.join(f, "dam.json")):
                return f
    return ""


def run_dam_folder(work, settings):
    """The dam folder a run is about, or "": settings['dam_folder']; the work folder when it holds
    dam.json; a one-dam run (only_ids = one ID) whose dam has a folder in the window's Dams folder
    (else <main work folder>/Dams) or the suite's Dams folder."""
    S = settings or {}
    f = (S.get("dam_folder") or "").strip()
    if f and os.path.isdir(f):
        return f
    if work and os.path.isfile(os.path.join(work, "dam.json")):
        return work
    ids = [x.strip() for x in str(S.get("only_ids") or "").split(",") if x.strip()]
    if len(ids) != 1:
        return ""
    base = (S.get("package_work") or "").strip() or work
    roots = [(S.get("dams_folder") or "").strip() or (path(base, "7b", "DAMS_FOLDER") if base else "")]
    try:
        roots.append(_ds("suitedata").dams_folder())
    except Exception:                                            # noqa
        pass
    return dam_folder_in(roots, ids[0])


def dam_data_paths(folder):
    """{role: absolute path} of a dam folder's data section ({} when none / unreadable)."""
    if not folder:
        return {}
    try:
        return _ds("damdata").dam_data(folder)
    except Exception:                                            # noqa - a broken dam.json
        return {}


_MANIFESTS = {}


def _pack_sets(pack):
    """{data set: info} of the usable (complete / partial) data sets of a pack's manifest, read
    without unpacking it (cached by file time)."""
    if not pack:
        return {}
    try:
        stamp = os.path.getmtime(pack if os.path.isfile(pack) else os.path.join(pack, "manifest.json"))
    except OSError:
        return {}
    hit = _MANIFESTS.get(pack)
    if hit and hit[0] == stamp:
        return hit[1]
    try:
        OP = _ds("offline_pack")
        man = OP.read_manifest(pack) or {}
        ok = (OP.STATUS_COMPLETE, OP.STATUS_PARTIAL)
    except Exception:                                            # noqa
        man, ok = {}, ()
    sets = {k: v for k, v in (man.get("datasets") or {}).items()
            if isinstance(v, dict) and v.get("status") in ok}
    _MANIFESTS[pack] = (stamp, sets)
    return sets


def pack_names(slot, sets):
    """The pack data sets that fill a slot (damdata.PACK_SETS; every lifeline_* set)."""
    role = SLOT_ROLE.get(slot)
    if not role or role == "nlcd":
        return []
    if role == "lifelines":
        return sorted(n for n in sets if n.startswith("lifeline_"))
    try:
        cands = _ds("damdata").PACK_SETS.get(role, ())
    except Exception:                                            # noqa
        cands = ()
    try:
        dedupe = _ds("hydro3dhp").dedupe          # 1.19.0: 3DHP_ and its old NHD_ name: once
    except Exception:                                            # noqa
        dedupe = list
    return dedupe([n for n in cands if n in sets])


def _pad_box(box, half_mi):
    import math
    lat = 0.5 * (box[1] + box[3])
    dlat = half_mi / 69.0
    dlon = half_mi / (69.17 * max(0.1, math.cos(math.radians(lat))))
    return (box[0] - dlon, box[1] - dlat, box[2] + dlon, box[3] + dlat)


def _gpkg_extent_lonlat(gpkg):
    """lon / lat box of a GeoPackage's feature layers from gpkg_contents (EPSG:4326 / 5070), or
    None (other CRS, no extent recorded, unreadable)."""
    if not gpkg or not os.path.isfile(gpkg):
        return None
    import sqlite3
    try:
        con = sqlite3.connect("file:{}?mode=ro".format(
            os.path.abspath(gpkg).replace("?", "%3f").replace("#", "%23")), uri=True)
        try:
            rows = con.execute("SELECT min_x, min_y, max_x, max_y, srs_id FROM gpkg_contents "
                               "WHERE data_type = 'features'").fetchall()
        finally:
            con.close()
    except Exception:                                            # noqa
        return None
    boxes = []
    for x0, y0, x1, y1, srs in rows:
        if None in (x0, y0, x1, y1):
            continue
        if int(srs or 0) == 4326:
            boxes.append((x0, y0, x1, y1))
        elif int(srs or 0) == 5070:
            G = _ds("suitegpkg")
            pts = [G.from_5070(x, y) for x, y in ((x0, y0), (x0, y1), (x1, y0), (x1, y1))]
            boxes.append((min(p[0] for p in pts), min(p[1] for p in pts),
                          max(p[0] for p in pts), max(p[1] for p in pts)))
    if not boxes:
        return None
    return (min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes),
            max(b[3] for b in boxes))


def chosen_counties(settings):
    """0.21.1: the window's counties ([] = all of North Dakota; a name that is not a county is
    left out)."""
    from . import nd_counties as cty
    out = []
    for part in str((settings or {}).get("counties") or "").split(","):
        c = cty.normalize(part)
        if c and c not in out:
            out.append(c)
    return out


COUNTY_CLIP = "0_county.gpkg"


def county_clip(work, settings, prepare=False):
    """0.21.1: '<work>/0_county.gpkg|layername=county', the chosen counties' outlines (step 0's
    clip), or None with no county chosen. prepare=True writes it (again when the counties
    changed)."""
    counties = chosen_counties(settings)
    if not counties or not work:
        return None
    target = os.path.join(work, COUNTY_CLIP)
    stamp = target + ".json"
    if prepare:
        try:
            with open(stamp) as fh:
                same = json.load(fh).get("counties") == counties and os.path.isfile(target)
        except (OSError, ValueError):
            same = False
        if not same:
            from . import nd_counties as cty
            if os.path.isfile(target):
                os.remove(target)
            _ds("damdata").write_features(target, "county", cty.features(counties),
                                          "ND county outlines (nd_counties.geojson)")
            with open(stamp, "w") as fh:
                json.dump({"counties": counties}, fh)
    return "{}|layername=county".format(target)


def work_area(work, settings):
    """lon / lat box the offline pack is clipped to: the dam (dam.json's location) when the run is
    about one dam folder, else the dams of the work folder (1_dams / 18 / 16 / 19), padded by the
    suite's data distance (damdata.DEFAULT_VECTOR_HALF_MI), else (0.21.1) the chosen counties,
    else North Dakota."""
    DD = _ds("damdata")
    half = DD.DEFAULT_VECTOR_HALF_MI
    folder = run_dam_folder(work, settings)
    if folder:
        try:
            rec = _ds("dam_record").load(folder)
            lon, lat, _how = DD.dam_location(rec)
            if lon is not None:
                return _pad_box((lon, lat, lon, lat), half)
        except Exception:                                        # noqa
            pass
    for st in ("1", "18", "16", "19"):
        b = _gpkg_extent_lonlat(out(work, st)) if work else None
        if b:
            return _pad_box(b, half)
    counties = chosen_counties(settings)
    if counties:
        from . import nd_counties as cty
        b, nd = _pad_box(cty.box(counties), half), tuple(_ds("offline_pack").ND_BBOX)
        return (max(b[0], nd[0]), max(b[1], nd[1]), min(b[2], nd[2]), min(b[3], nd[3]))
    return tuple(_ds("offline_pack").ND_BBOX)


NID_PAD_MI = 0.5      # 0.22.1: the suite NID is clipped to the user's DWR layer, padded by this


def _vector_box_lonlat(layer):
    """lon / lat box of a vector layer (file path, path|layername=..., or a project layer id), or
    None when it cannot be read."""
    try:
        from qgis.core import (QgsProject, QgsVectorLayer, QgsCoordinateReferenceSystem,
                               QgsCoordinateTransform)
        lyr = QgsProject.instance().mapLayer(layer)
        if lyr is None:
            lyr = QgsVectorLayer(layer, "dwr", "ogr")
        if lyr is not None and lyr.isValid() and lyr.featureCount() != 0:
            ext = lyr.extent()
            wgs = QgsCoordinateReferenceSystem("EPSG:4326")
            if lyr.crs().isValid() and lyr.crs() != wgs:
                ext = QgsCoordinateTransform(lyr.crs(), wgs, QgsProject.instance()) \
                    .transformBoundingBox(ext)
            if not ext.isNull() and ext.width() >= 0:
                return (ext.xMinimum(), ext.yMinimum(), ext.xMaximum(), ext.yMaximum())
    except Exception:                                            # noqa - no QGIS here: read it
        pass
    try:
        DD = _ds("damdata")
        feats = DD.read_vector_file(layer, tuple(_ds("offline_pack").ND_BBOX))
        G = _ds("suitegpkg")
        pts = [G.point_on_surface(f.get("geometry")) for f in feats]
        pts = [p for p in pts if p]
        if pts:
            return (min(p[0] for p in pts), min(p[1] for p in pts),
                    max(p[0] for p in pts), max(p[1] for p in pts))
    except Exception:                                            # noqa
        pass
    return None


def nid_clip_box(settings):
    """0.22.1: the lon / lat box the suite's NID dams are clipped to when the DWR slot holds the
    user's own layer: that layer's dams padded by NID_PAD_MI (and inside the chosen counties).
    None when the DWR slot is empty or cannot be read (then the NID covers the work area)."""
    S = settings or {}
    v = (S.get("dwr") or "").strip()
    if not v:
        return None
    b = _vector_box_lonlat(v)
    if b is None:
        return None
    b = _pad_box(b, NID_PAD_MI)
    counties = chosen_counties(S)
    if counties:
        from . import nd_counties as cty
        c = _pad_box(cty.box(counties), NID_PAD_MI)
        b = (max(b[0], c[0]), max(b[1], c[1]), min(b[2], c[2]), min(b[3], c[3]))
        if b[0] > b[2] or b[1] > b[3]:
            return None
    return tuple(round(x, 9) for x in b)


def prepare_nid_file_slot(f, target, box, log=None):
    """0.22.1: the NID dams of the suite data folder's nid/ file inside `box`, written to `target`
    (layer nid). Kept when written after the file for the same box. Returns the count or None."""
    log = log or (lambda m: None)
    if _keep_clip(target, box, "nid folder", os.path.getmtime(f), exact=True):
        return None
    DD = _ds("damdata")
    feats = DD.read_vector_file(f, box)
    cnt = DD.write_features(target, "nid", feats, "nid folder " + os.path.basename(f))
    _write_box(target, box, "nid folder")
    log("nid: {} feature(s) of {} near your DWR dams written to {}".format(
        cnt, os.path.basename(f), target))
    return cnt


def prepare_pack_slot(slot, work, settings, pack, names, target, log=None, box=None):
    """Write the pack's data sets of a slot, clipped to the work area, to `target` (a GeoPackage
    with layer <slot>, EPSG:5070). An existing file newer than the pack is kept when it covers the
    work area (0.21.1: its box is in <target>.box.json; a file without one is kept as before).
    Returns the number of features written (None when kept)."""
    log = log or (lambda m: None)
    OP, DD = _ds("offline_pack"), _ds("damdata")
    exact = box is not None                  # 0.22.1: an explicit box (the NID clip) is kept only as is
    box = box or work_area(work, settings)
    # 0.22.0: a pack folder's date is its manifest's
    stamp_of = os.path.join(pack, "manifest.json") if os.path.isdir(pack) else pack
    if _keep_clip(target, box, "pack", os.path.getmtime(stamp_of) if os.path.exists(stamp_of)
                  else 0.0, exact=exact):
        return None
    reader = OP.open_pack(pack, log=log)
    feats = []
    for n in names:
        if not reader.covers(box, n):
            log("    {}: the offline pack does not hold all of the work area (incomplete data set)".format(n))
        for f in reader.query(n, box):
            if len(names) > 1:
                f = dict(f)
                f["properties"] = dict(f.get("properties") or {}, dataset=n)
            feats.append(f)
    info = reader.info(names[0]) or {}
    src = "offline pack {} (fetched {})".format("+".join(names), info.get("fetched", "?"))
    cnt = DD.write_features(target, slot, feats, src)
    _write_box(target, box, "pack")
    log("{}: {} feature(s) from the {} written to {}".format(slot, cnt, src, target))
    return cnt


def _keep_clip(target, box, kind, newer_than, exact=False):
    """True when <work>/0_suite_data/<slot>.gpkg can stay: written after `newer_than` (a file
    time) from the same kind of source (0.22.0: "pack" or "download"), and its box (0.21.1,
    <target>.box.json) covers `box` (0.22.1, exact: is `box`). Otherwise the file is removed
    (written again)."""
    try:
        if not os.path.isfile(target):
            return False
        try:
            with open(target + ".box.json") as fh:
                d = json.load(fh)
            had, was = d.get("box"), d.get("from", "pack")
        except (OSError, ValueError):
            had, was = None, "pack"
        if exact:
            fits = had is not None and all(abs(a - b) < 1e-9 for a, b in zip(had, box))
        else:
            fits = had is None or (had[0] <= box[0] + 1e-9 and had[1] <= box[1] + 1e-9 and
                                   had[2] >= box[2] - 1e-9 and had[3] >= box[3] - 1e-9)
        if was == kind and os.path.getmtime(target) >= newer_than and fits:
            return True
        os.remove(target)                # another county / area / source: written again
    except OSError:
        pass
    return False


def _write_box(target, box, kind):
    try:
        with open(target + ".box.json", "w") as fh:
            json.dump({"box": list(box), "from": kind}, fh)
    except OSError:
        pass


DOWNLOAD_MAX_AGE_S = 7 * 86400          # a download of the same area is fetched again after


def prepare_download_slot(slot, work, settings, target, log=None, box=None):
    """0.22.0 (Data source = Download): the slot's data for the work area from the server chosen
    for it in Suite data and servers, written to `target` (layer <slot>, EPSG:5070). A download
    of the same area younger than DOWNLOAD_MAX_AGE_S is kept. Returns the number of features
    written (None when kept); raises when no server gives that kind of data."""
    import time
    log = log or (lambda m: None)
    DD = _ds("damdata")
    role = SLOT_ROLE.get(slot)
    exact = box is not None
    box = box or work_area(work, settings)
    if _keep_clip(target, box, "download", time.time() - DOWNLOAD_MAX_AGE_S, exact=exact):
        return None
    log("{}: downloading for the work area {:.3f}, {:.3f} to {:.3f}, {:.3f}...".format(
        slot, *box))
    got = DD.fetch_role(role, box)
    if got is None:
        raise LookupError("no server gives {} (Suite data and servers)".format(role))
    feats, src, _short = got
    cnt = DD.write_features(target, slot, feats, src)
    _write_box(target, box, "download")
    log("{}: {} feature(s) downloaded from {} written to {}".format(slot, cnt, src, target))
    return cnt


def slot_origin(slot, work, settings, prepare=False, log=None):
    """(layer, origin) of a data slot; origin: "chosen", "package", "dam folder", "suite pack",
    "dwr folder" or None (nothing). See the comment above SLOT_ROLE for the order."""
    S = settings or {}
    v = (S.get(slot) or "").strip()
    if v:
        return v, "chosen"
    v = _package_source(slot, work, S)
    if v:
        return v, "package"
    role = SLOT_ROLE.get(slot)
    if not role:
        return None, None
    v = dam_data_paths(run_dam_folder(work, S)).get(role)
    if v:
        return v, "dam folder"
    data = settings_data_folder(S)
    try:
        SD = _ds("suitedata")
        download = SD.data_source() == SD.SOURCE_DOWNLOAD
        pack = SD.active_pack(data)              # 0.22.0: "" with Data source = Download
    except Exception:                                            # noqa
        SD, pack, download = None, "", False
    nbox = nid_clip_box(S) if slot == "nid" and work else None     # 0.22.1: near the DWR dams
    if nbox and prepare and log:
        log("nid: only the NID dams within {} mi of your DWR layer's dams ({:.4f}, {:.4f} to "
            "{:.4f}, {:.4f})".format(NID_PAD_MI, *nbox))
    if download and work and role not in ("nlcd",):
        target = os.path.join(work, SUITE_DIR, slot + ".gpkg")
        ok = True
        if prepare:
            try:
                prepare_download_slot(slot, work, S, target, log=log, box=nbox)
            except Exception as e:                               # noqa - fall back to dwr/ below
                ok = False
                if log:
                    log("{}: not downloaded ({})".format(slot, e))
        elif not os.path.isfile(target):
            ok = _download_possible(role)
        if ok:
            return "{}|layername={}".format(target, slot), "download"
    if not data and not pack:
        return None, None
    names = pack_names(slot, _pack_sets(pack)) if pack and work else []
    if names:
        target = os.path.join(work, SUITE_DIR, slot + ".gpkg")
        ok = True
        if prepare:
            try:
                prepare_pack_slot(slot, work, S, pack, names, target, log=log, box=nbox)
            except Exception as e:                               # noqa - fall back to dwr/ below
                ok = False
                if log:
                    log("{}: the offline pack could not be read ({})".format(slot, e))
        if ok:
            return "{}|layername={}".format(target, slot), "suite pack"
    if SD is not None and role in ("dwr_dams", "dwr_dam_lines"):
        try:
            f = SD.dwr_layers(data).get(role)
        except Exception:                                        # noqa
            f = ""
        if f:
            return f, "dwr folder"
    if role == "nid_dams":                                       # 0.21.1: no pack - an NID file
        f = nid_dropin(data)
        if f and nbox:                                           # 0.22.1: near the DWR dams only
            target = os.path.join(work, SUITE_DIR, slot + ".gpkg")
            if prepare:
                try:
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    prepare_nid_file_slot(f, target, nbox, log=log)
                except Exception as e:                           # noqa - the whole file then
                    if log:
                        log("nid: {} could not be clipped ({}); all of it is used".format(f, e))
                    return f, "nid folder"
            return "{}|layername={}".format(target, slot), "nid folder"
        if f:
            return f, "nid folder"
    return None, None


def _download_possible(role):
    """True when a server is chosen for the role in Suite data and servers (0.22.0)."""
    try:
        SV, DD = _ds("services"), _ds("damdata")
        srole = DD.SERVICE_ROLE.get(role, role)
        s = SV.load().chosen(srole) if srole else None
        return s is not None and (s.get("kind") == "file" or SV.data_capable(s, srole))
    except Exception:                                            # noqa
        return False


NID_DIR = "nid"


def nid_dropin(data):
    """0.21.1: the NID dams file the user put in <suite data folder>/nid/ (the first vector file
    there, by name), or "". Without the offline pack this is where tool 1's NID layer comes from
    (the folders download has no NID; layers/hydro-dams/NID_dams.gpkg is the statewide one)."""
    d = os.path.join(data or "", NID_DIR)
    if not data or not os.path.isdir(d):
        return ""
    for n in sorted(os.listdir(d)):
        if os.path.splitext(n)[1].lower() in (".gpkg", ".shp", ".geojson", ".json", ".fgb") \
                and not n.startswith((".", "_")):
            return os.path.join(d, n)
    return ""


# 0.21.0: "Fill empty layers from the suite data" (Inputs tab). The same sources a run falls back
# to (slot_origin), written into the empty slots so the user sees them before the run. Streams and
# land cover are left alone: they change the model (road-crossing cuts, Manning's n), not only
# give it data (MODEL_SLOTS); dam lines are filled (they only protect the dam from those cuts).
FILL_SLOTS = ("dwr", "nid", "structures", "roads", "rail", "lifelines", "waterbodies", "dam_lines")


def fill_empty_slots(work, settings, log=None, prepare=True, slots=FILL_SLOTS):
    """{slot: (layer, origin)} for the EMPTY slots that the dam folder, the suite offline pack or
    the suite data folder's dwr/ can fill. A chosen layer and the step-0 package are left as they
    are. prepare=True writes the pack clips now (needs the work folder)."""
    S = dict(settings or {})
    out = {}
    for slot in slots:
        if (S.get(slot) or "").strip():
            continue
        try:
            value, origin = slot_origin(slot, work, S, prepare=prepare, log=log)
        except Exception as e:                                   # noqa - one slot never stops the rest
            if log:
                log("{}: not filled ({})".format(slot, e))
            continue
        if value and origin in ("dam folder", "suite pack", "dwr folder", "nid folder",
                                "download"):
            out[slot] = (value, origin)
    return out


# 0.28.1: the Simple tab's one line of what the run will read, without clipping anything (the
# short names a user knows; the origins in the words of the Inputs tab)
FOUND_NAMES = {"dwr": "DWR dams", "nid": "NID dams", "structures": "structures", "roads": "roads",
               "rail": "railroads", "lifelines": "lifelines", "waterbodies": "waterbodies",
               "dam_lines": "dam lines"}
FOUND_ORIGIN = {"chosen": "chosen on the Inputs tab", "package": "data package",
                "dam folder": "dam folder", "suite pack": "offline pack",
                "download": "download", "dwr folder": "dwr/ folder", "nid folder": "nid/ folder"}


def found_layers(work, settings):
    """{slot: origin or None} for the layers a run needs (FILL_SLOTS), read without writing a
    clip (slot_origin with prepare=False). One slot that cannot be read is None, never an error."""
    out = {}
    for slot in FILL_SLOTS:
        try:
            _value, origin = slot_origin(slot, work, settings or {}, prepare=False)
        except Exception:                                        # noqa
            origin = None
        out[slot] = origin
    return out


def found_layers_text(work, settings, found=None):
    """0.28.1: one line for the Simple tab: what the run will read and from where, and what it
    will not find - "Found: DWR dams, structures, roads (offline pack); dam lines (dwr/ folder).
    Not found: railroads." Empty without a work folder (the hint line asks for one then)."""
    if not work:
        return ""
    found = found_layers(work, settings) if found is None else found
    by_origin = {}
    for slot in FILL_SLOTS:
        o = found.get(slot)
        if o:
            by_origin.setdefault(o, []).append(FOUND_NAMES[slot])
    missing = [FOUND_NAMES[s] for s in FILL_SLOTS if not found.get(s)]
    if not by_origin:
        return ("No input layers found: set the Suite folder on the Dam suite home's Start tab (it "
                "finds the offline pack), or choose layers on the Inputs tab.")
    parts = ["{} ({})".format(", ".join(names), FOUND_ORIGIN.get(o, o))
             for o, names in by_origin.items()]
    text = "Found: " + "; ".join(parts) + "."
    if missing:
        text += " Not found: " + ", ".join(missing) + "."
    return text


# 0.28.1: one Continue button that opens the plugin the selected dam needs next, read from its
# dam.json (dam_record.next_action) - not from file names, which a baseline or an interim raster
# would also match
def continue_target(work, settings, dam_id):
    """(plugin, label, why) for the Continue button: plugin "ndbreach", "ndhazard" or "" (the
    button stays grey and `why` says why)."""
    dam_id = str(dam_id or "").strip()
    if not work or not dam_id:
        return "", "Continue", ("Select a dam in the Results tab's table, or run one dam, then "
                                "Continue opens the plugin it needs next.")
    _root, row = dam_row_for(work, settings, dam_id)
    folder = (row or {}).get("folder") or ""
    try:
        rec = _ds("dam_record").load(folder) if folder and os.path.isdir(folder) else None
    except Exception as e:                                       # noqa - a broken dam.json
        return "", "Continue", "{}: dam.json could not be read ({}).".format(dam_id, e)
    if rec is None:
        return "", "Continue", ("{} has no dam record yet: screen it (Run one dam) first.".format(dam_id))
    code, text = _ds("dam_record").next_action(rec)
    if code == "model":
        return "ndbreach", "Continue: model {} in the Runner".format(dam_id), text
    if code in ("classify", "review", "none"):
        verb = {"classify": "classify", "review": "review"}.get(code, "open")
        return "ndhazard", "Continue: {} {} in the Hazard Helper".format(verb, dam_id), text
    return "", "Continue", text        # screen / refresh: this window's own job first


# --------------------------------------------------------------- statuses --
def statuses(work, settings):
    """[(step, title, state, note)] for the window. state: 'done', 'stale' (an earlier step was
    run again after this one), 'ready', 'waiting', 'optional'. note: what is missing or why."""
    rows = []
    for key in KEYS:
        done = is_done(work, key)
        missing = _missing_requirements(work, key)
        notes = []
        for slot in NEEDS_LAYER.get(key, ()):
            if slot_source(slot, work, settings) is None:
                label = dict((k, l) for k, l, _p in LAYER_SLOTS)[slot]
                notes.append("no {} layer (Inputs tab, the data package, or Suite data and servers)".format(
                    _phrase(label)))
        if done and covered_by_proposed(work, key):
            rows.append((key, TITLE[key], "done", "made by step 19 (proposed dam)"))
            continue
        if done and covered_by_ponds(work, key):
            rows.append((key, TITLE[key], "done", "made by step 16 (ponds and dugouts)"))
            continue
        if done:
            changed = changed_inputs(work, key)
            if changed is not None:                              # 0.11: by content (manifest)
                if changed:
                    state = "stale"
                    notes.insert(0, "input(s) changed since this step ran: {}".format(
                        ", ".join(changed)))
                else:
                    state = "done"
            else:                                                # no manifest: by dates
                mt = _mtime(work, key)
                newer = [r for r in _flat_requirements(key) if is_done(work, r)
                         and mt is not None and (_mtime(work, r) or 0) > mt + 1]
                if newer:
                    state = "stale"
                    notes.insert(0, "step {} was run again after this one".format(", ".join(newer)))
                else:
                    state = "done"
        elif missing:
            state = "waiting"
            notes.insert(0, "needs step {}".format(" and ".join(missing)))
        else:
            state = "optional" if key in OPTIONAL else "ready"
        rows.append((key, TITLE[key], state, "; ".join(notes)))
    return rows


def manifest_of(work, step):
    """The manifest dict of a step's marker output (core/manifest.py), or None."""
    from . import manifest as mf
    if step == "10":
        p = os.path.join(work, LAYOUT["10"]["OUTDIR"])
    else:
        p = path(work, step, DONE_MARK[step])
    mp = mf.manifest_path_for(p) if os.path.exists(p) else None
    return mf.read(mp) if mp and os.path.isfile(mp) else None


def changed_inputs(work, step):
    """Labels of the step's recorded input files whose content changed (by SHA-256) since it
    ran; [] when none did; None when the step has no manifest (run before 0.11, or the tool
    wrote nothing on disk)."""
    m = manifest_of(work, step)
    if m is None:
        return None
    from . import manifest as mf
    try:
        return mf.changed_inputs(m)
    except Exception:                                            # noqa
        return None


def _flat_requirements(key):
    flat = []
    for r in REQUIRES.get(key, ()):
        flat.extend(r if isinstance(r, tuple) else (r,))
    return flat


def _done_for_chain(work, key, both=False):
    """is_done, except that with 'both' (step 18 wanted) steps 1-4 do not count as done just
    because tool 16 ran: the DWR dams must be prepared too."""
    if both and key in POND_COVERS and (covered_by_ponds(work, key) or covered_by_proposed(work, key)):
        return False
    return is_done(work, key)


def _missing_requirements(work, key, both=False):
    missing = []
    for r in REQUIRES.get(key, ()):
        alts = r if isinstance(r, tuple) else (r,)
        if not any(_done_for_chain(work, a, both) for a in alts):
            missing.append(" or ".join(alts))
    return missing


def next_step(work, settings=None):
    """The first step, in order, that is not done and can be started; optional steps are
    skipped. None when everything required is done."""
    for key in KEYS:
        if key in OPTIONAL or is_done(work, key):
            continue
        if not _missing_requirements(work, key):
            return key
        return None
    return None


def _one_proposed_dem(work):
    """The conditioned clip when step 19 wrote exactly one dam (not the natural copy)."""
    d = out(work, "19", "TERRAIN") if work else None
    if not d:
        return None
    try:
        names = [n for n in os.listdir(d) if n.startswith("dam_") and n.lower().endswith(".tif")
                 and "_natural" not in n.lower()]
    except OSError:
        return None
    return os.path.join(d, names[0]) if len(names) == 1 else None


def missing_dams_note(work):
    """0.21.0: the window's log line when tools 2-4 open with no tool-1 output in the work
    folder (the dialog then starts with the last Standardized dams written, if any)."""
    return ("Standardized dams: no {} in the work folder {}. Run tool 1 from this window (All "
            "steps), or save its layer there as {}; the dialog starts with the last Standardized "
            "dams written, when there is one - check it is the right layer.").format(
                LAYOUT["1"]["OUT"], work, LAYOUT["1"]["OUT"])


# --------------------------------------------------------------- defaults --
def defaults(step, work, settings, log=None, prepare=False):
    """The parameters to fill in for tool `step` (only values that are known; a parameter
    left out keeps its own default). 0.16.0: `log` is told which data a slot falls back to (the
    dam folder, the suite offline pack, dwr/); `prepare` writes the pack's clips (the window and
    the chain, just before a tool runs)."""
    S = dict(DEFAULT_SETTINGS)
    S.update(migrate_settings(settings))
    p = {}

    def put(key, value):
        if value not in (None, ""):
            p[key] = value

    def src(slot):
        return slot_source(slot, work, S, log=log, prepare=prepare)

    def src_model(slot):
        """a slot that changes the model (MODEL_SLOTS, buildings): no suite data fallback"""
        return slot_source(slot, work, S, suite=False)

    def o(st, param="OUT"):
        return out(work, st, param)

    def folder(st, param):
        return path(work, st, param)

    # 0.12: the outputs of tool 16 stand in for those of tools 2, 3 and 4 (ponds and dugouts)
    # 0.13: the merged layers (18) win when both kinds were screened in one folder
    def dams3():
        return o("18") or o("3") or o("19") or o("16")

    def curves():
        return o("18", "CURVES") or o("3", "CURVES") or o("19", "CURVES") or o("16", "CURVES")

    def bpts():
        return o("18", "OUTP") or o("4", "OUTP") or o("19", "OUTP") or o("16", "OUTP")

    def demdir():
        return o("18", "TERRAIN") or o("2") or o("19", "TERRAIN") or o("16", "TERRAIN")

    def damlines():
        # a proposed dam has no DWR line: the crest line written by step 19 stands in
        return src_model("dam_lines") or o("19", "CREST")

    def fields(slot, names):
        """standard field names of the package layer, when the layer comes from the package"""
        if _from_package(slot, work, S):
            table = PKG_LAYER[slot]
            for key, fld in names.items():
                put(key, PKG_FIELDS[table].get(fld))

    for param, rel in LAYOUT[step].items():               # the outputs
        if param != "DAMS_FOLDER" and step not in FIXED_OUTPUTS:
            put(param, os.path.join(work, rel))

    if step == "0":
        pkg = (S.get("package") or "").strip()
        if pkg and os.path.isfile(pkg):
            put("PACKAGE", pkg)
        put("CLIP", S.get("clip") or county_clip(work, S, prepare=prepare))   # 0.21.1: the county
        for slot, param in TOOL0_SLOT.items():
            put(param, (S.get(slot) or "").strip())
    elif step == "1":
        # 0.21.0: like every other data slot, an empty DWR / NID slot falls back to the dam
        # folder, the suite offline pack or the suite data folder's dwr/ (logged); before, tool 1
        # took only the Inputs tab's choice
        put("DWR", src("dwr"))
        put("NID", src("nid"))
        counties = chosen_counties(S)                        # 0.21.1: the window's county
        if counties:
            from . import nd_counties as cty
            p["COUNTIES"] = [cty.names().index(c) for c in counties]
        put("ONLY_IDS", S.get("only_ids"))
    elif step == "2":
        put("DAMS", o("1"))
        put("SRC", S["terrain_source"])
        put("LOCAL", S.get("local_dem"))
        cell_ft = float(S.get("dem_cell_ft") if S.get("dem_cell_ft") is not None else 32.8)
        put("CELL_FT", cell_ft)
        # 0.28.0: the cached DEM's cell follows the source (3DEP and the suite's downloads are
        # 10 m). A local LiDAR folder can be finer than this, and resampling it coarser throws
        # terrain away, so say so rather than decide it here.
        if log and int(S.get("terrain_source") or 0) == 1 and cell_ft > 10.0:
            log("step 2: DEM cell size {:.1f} ft ({:.1f} m). Your local tiles may be finer: set "
                "'DEM cell size (ft)' to your tiles' cell (3.3 ft for 1 m LiDAR) to keep their "
                "detail.".format(cell_ft, cell_ft * 0.3048))
    elif step == "3":
        put("DAMS", o("1"))
        put("WB", src("waterbodies"))
        put("DEMDIR", o("2"))
        if S.get("wb_search_ft") not in (None, ""):        # 0.19.0: unknown structures sit off their water
            put("SEARCH_FT", float(S["wb_search_ft"]))
    elif step == "4":
        put("DAMS", o("3") or o("1"))
        put("LINES", src("dam_lines"))
        put("WB", src("waterbodies"))
        put("DEMDIR", o("2"))
    elif step == "5":
        put("DAMS", dams3())
        put("CURVES", curves())
        put("BPTS", bpts())
    elif step == "6":
        put("DAMS", o("5"))
        put("BPTS", bpts())
        put("DEMDIR", demdir())
        put("BLD", src("structures"))
        fields("structures", {"OCC": "occ_cls", "PRIM": "prim_occ"})
        put("ROADS", src("roads"))
        put("RAIL", src("rail"))
        put("LIFELINES", src("lifelines"))
        put("STREAMS", src_model("streams"))
        put("DAMLINES", damlines())
    elif step == "7":
        put("DAMS", o("6") or o("5"))
        put("BPTS", bpts())
        put("DEMDIR", demdir())
        tsrc = larger_clips_index(S)                     # 0.28.0: follows the terrain source
        put("TSRC", tsrc)
        if log:
            log("    " + larger_clips_text(S))
        put("LOCAL", S.get("local_dem"))
        put("ENGINE", S["engine"])
        put("EXE", S.get("exe"))
        put("SCEN", S["scenario"])
        put("CURVES", curves())
        # 0.28.0: the preset is passed, so the Grid size box means what it says (with Custom) and
        # the window can show the cell the engine will use (core/runplan.effective_cell)
        from . import runplan as rpl
        put("PRESET", rpl.preset_index(S))
        put("CELL_FT", float(S["grid_ft"] if S.get("grid_ft") is not None else 33.0))
        put("SIMH", float(S.get("simh") if S.get("simh") is not None else 12.0))
        put("SIMMAX", float(S.get("simmax") if S.get("simmax") is not None else 96.0))
        put("STREAMS", src_model("streams"))
        put("DAMLINES", damlines())
        put("NLCD", src_model("nlcd"))
        if int(S.get("workers") or 0) > 0:
            put("WORKERS", int(S["workers"]))
        if S.get("pregrow"):                                # 0.20.0: opt-in clip growth from the trace
            p["PREGROW"] = True
        if S.get("both_scenarios"):                         # 0.28.0: top of dam AND sunny day
            p["SCEN_BOTH"] = True
        if not S.get("skip_done", True):                    # 0.28.0: force a re-run of every dam
            p["SKIPDONE"] = False
        # 0.11: no-breach baseline and buildings as obstructions
        if S.get("baseline"):
            p["BASELINE"] = True
            put("BASELINE_CFS", float(S.get("baseline_cfs") or 0.0))
        bmode = int(S.get("buildings") or 0)
        if bmode:
            p["BUILDINGS"] = bmode
            put("BLD", src_model("structures"))
            put("BUILDING_RAISE_FT", float(S.get("building_raise_ft") or 10.0))
            put("BUILDING_N", float(S.get("building_n") or 0.15))
    elif step == "7b":
        put("DAMS", o("8") or o("7"))
        put("BPTS", bpts())
        # 0.16.0: else the DEM of the dam folder (Get data for this dam / tool 20)
        dem = (S.get("runner_dem") or "").strip() or _one_proposed_dem(work)
        if not dem:
            dem = dam_data_paths(run_dam_folder(work, S)).get("dem")
            if dem and log:
                log("DEM for the Runner: {} ({})".format(dem, ORIGIN_TEXT["dam folder"]))
        put("DEM", dem)
        p["DAMS_FOLDER"] = dams_folder(work, S)
        put("CURVES", curves())
        pts = src("structures")
        pkg = package_path(work)
        if not (S.get("structures") or "").strip() and pkg and "structure_points" in package_layers(pkg):
            pts = "{}|layername=structure_points".format(pkg)     # the Runner's receptors are points
        elif not (S.get("structures") or "").strip() and slot_origin("structures", work, S)[1] == "dam folder":
            pts = dam_data_paths(run_dam_folder(work, S)).get("structure_points") or pts
        put("BLD", pts)
        if _from_package("structures", work, S):
            put("S_OCC", "occ_cls")
            put("S_MOBILE", "mobile")
            put("S_HABITABLE", "habitable")
        put("ROADS", src("roads"))
        fields("roads", {"R_AADT": "aadt", "R_CLASS": "road_class"})
        put("RAIL", src("rail"))
        fields("rail", {"T_TRAINS": "trains_day", "T_PASSENGER": "passenger"})
        put("LIFELINES", src("lifelines"))
        put("CHART_FILE", S.get("chart_file"))
    elif step == "8":
        put("SIM", o("7"))
        put("CURVES", curves())              # 0.28.0: the breach-parameter sensitivity row
        put("BLD", src("structures"))
        fields("structures", {"OCC": "occ_cls", "PRIM": "prim_occ"})
        put("ROADS", src("roads"))
        put("RAIL", src("rail"))
        fields("rail", {"RAIL_PAX": "passenger", "RAIL_TRAINS": "trains_day"})
        put("LIFELINES", src("lifelines"))
        df = dams_folder(work, S)
        mosaic = os.path.join(out(work, "2") or "", "_local_mosaic.vrt")   # step 2's local DEM mosaic
        if S.get("terrain_source") == 1 and os.path.isfile(mosaic):
            put("DEM_FOR_RECORD", mosaic)                   # 0.26.0: the Runner starts with this DEM
        if S.get("only_ids"):                               # 0.26.0: chosen dams get a dam folder
            p["CREATE_DAM_FOLDERS"] = True
            try:
                os.makedirs(df, exist_ok=True)
            except OSError:
                pass
        if os.path.isdir(df):                               # otherwise tool 8 creates no folder
            put("DAMS_FOLDER", df)
        put("CRIT", S["crit"])
        put("CHART_FILE", S.get("chart_file"))
        if S.get("incremental"):
            p["INCREMENTAL"] = True
    elif step == "9":
        put("DAMS", dams3() or o("5"))
        put("CURVES", curves())
        put("BPTS", bpts())
        put("DEMDIR", demdir())
        put("ENGINE", S["engine"])
        put("EXE", S.get("exe"))
        if int(S.get("workers") or 0) > 0:
            put("WORKERS", int(S["workers"]))
        put("STREAMS", src_model("streams"))
        put("DAMLINES", damlines())
        put("NLCD", src_model("nlcd"))
        put("BLD", src("structures"))
        put("ROADS", src("roads"))
        put("RAIL", src("rail"))
        put("LIFELINES", src("lifelines"))
        put("CRIT", S["crit"])
        put("CHART_FILE", S.get("chart_file"))
    elif step == "10":
        put("HAZ", o("8"))
        put("IMP", o("8", "IMP"))
        put("SENS", o("9"))
        put("BPTS", bpts())
        put("DEMDIR", demdir())
    elif step == "11":
        put("SIM", o("7"))
        put("SIMDIR", o("7", "OUTDIR"))
        put("SCEN", S["scenario"])
        put("REF_FOLDER", (S.get("ref_folder") or "").strip())
        put("REF_UNITS", int(S.get("ref_units") or 0))
    elif step == "12":
        put("DAMS", o("6") or o("5"))
        put("SCEN", S["scenario"])
        put("ENGINE", S["engine"])
        put("DEMDIR", demdir())
        # 0.28.0: tool 12 estimates the cell size step 7 will really use, not the box's
        from . import runplan as rpl
        eff = rpl.effective_cell(S)
        put("CELL_FT", float(eff["cell_ft"] if eff["cell_ft"] else 0.0))
        put("SIMH", float(S.get("simh") if S.get("simh") is not None else 12.0))
        put("SIMMAX", float(S.get("simmax") if S.get("simmax") is not None else 96.0))
        if int(S.get("workers") or 0) > 0:
            put("WORKERS", int(S["workers"]))
    elif step == "13":
        put("NEW", o("1"))
        put("OLD", (S.get("old_inventory") or "").strip())
        df = dams_folder(work, S)
        if os.path.isdir(df):
            put("DAMS_FOLDER", df)
    elif step == "14":
        put("ENGINE", 2)
    elif step == "16":
        put("WB", src("waterbodies"))
        put("SRC", S["terrain_source"])
        put("LOCAL", S.get("local_dem"))
        put("ONLY_IDS", S.get("only_ids"))
        # 0.19.0: DWR's dugouts - the polygons as the water bodies, the records joined by number
        put("INV", S.get("dugout_records"))
        put("WB_ID", S.get("wb_id") or ("structure_id" if S.get("dugout_records") else None))
        if S.get("pond_max_ac") not in (None, ""):
            p["MAX_AC"] = float(S["pond_max_ac"])
    elif step == "19":
        put("SRC", S["terrain_source"])
        put("LOCAL", S.get("local_dem"))
        put("ONLY_IDS", S.get("only_ids"))
        p["DAMS_FOLDER"] = dams_folder(work, S)
    elif step == "18":
        put("WORK", work)
    elif step == "15":
        put("WORK", work)
        sf = (S.get("settings_file") or "").strip()
        put("SETTINGS", sf if sf and os.path.isfile(sf) else json.dumps({k: v for k, v in S.items() if k != "work"}))
    elif step == "17":
        put("WORK", work)
        sf = (S.get("settings_file") or "").strip()
        put("SETTINGS", sf if sf and os.path.isfile(sf) else json.dumps({k: v for k, v in S.items() if k != "work"}))
        inc = [i for i, k in enumerate(CHAIN_OPTIONAL) if k in (S.get("chain") or [])]
        if inc:
            put("INCLUDE", inc)
    return p


# ------------------------------------------------------------ one dam --
ONE_DAM_DIR = "one_dam"


def safe_name(text):
    """A folder name from a dam ID ('DWR-5289' stays; odd characters become '_')."""
    out = "".join(c if c.isalnum() or c in "-_." else "_" for c in str(text or "").strip())
    return out.strip("._") or "dam"


def one_dam_work(work, dam_id):
    """<work>/one_dam/<dam id>: the work folder of a single-dam run (0.13 'Run one dam')."""
    return os.path.join(work, ONE_DAM_DIR, safe_name(dam_id))


def one_dam_settings(settings, work, dam_id):
    """The window's settings for a single-dam run: the dam filter for tools 1 / 16, and the
    package of the main work folder."""
    S = dict(settings or {})
    S["only_ids"] = str(dam_id).strip()
    S["package_work"] = work
    # 0.26.0: its dam.json goes to the main work folder's Dams folder (the Runner's and the
    # Hazard Helper's folder of dam folders), not to one under one_dam/<ID>/
    S["dams_folder"] = (settings or {}).get("dams_folder") or dams_folder(work, settings or {})
    # 0.28.0: one dam is a minute of flood model, so it gets BOTH loading cases unless the
    # engineer has already switched them on or off by hand for the county (core/scenarios.py:
    # both_default). A county keeps one scenario - two would double hours into days.
    S["both_scenarios"] = bool(S.get("both_one_dam", True))
    return S


def only_ids_text(ids):
    """0.26.0: the ONLY_IDS value of a run of chosen dams: the IDs joined by ', ' (tools 1 and 16
    split the value on commas); blanks and repeats dropped, order kept."""
    seen, out = set(), []
    for x in ids or []:
        s = str(x or "").strip()
        if s and s.upper() not in seen:
            seen.add(s.upper())
            out.append(s)
    return ", ".join(out)


def selected_dams_name(ids):
    """0.26.0: the folder name (under one_dam/) of a run of chosen dams: the IDs joined by '_'
    up to three of them, else the first and a count ("DWR-1_and_4_more"); "" without IDs."""
    ids = [x.strip() for x in only_ids_text(ids).split(",") if x.strip()]
    if not ids:
        return ""
    if len(ids) <= 3:
        return safe_name("_".join(ids))
    return safe_name("{}_and_{}_more".format(ids[0], len(ids) - 1))


def dam_row_for(work, settings, dam_id):
    """0.26.0: the dam of this work folder as the row suite_home.point_window_at takes
    ({"dam_id", "folder"}), for pointing the Runner's and the Hazard Helper's windows at it: the
    dam folder under the Dams folder the chain hands dams over to (an existing folder of that ID,
    a bare DWR number finding DWR-<number>, else <Dams folder>/<ID>). Returns (Dams folder, row),
    or ("", None) without an ID."""
    dam_id = str(dam_id or "").strip()
    if not dam_id:
        return "", None
    root = dams_folder(work, settings or {})
    folder = dam_folder_in([root], dam_id) or os.path.join(root, _runner_name(dam_id))
    return root, {"dam_id": dam_id, "folder": folder}


def one_dam_runs(work):
    """[(dam id, folder)] of the single-dam runs under a work folder."""
    root = os.path.join(work, ONE_DAM_DIR) if work else ""
    if not root or not os.path.isdir(root):
        return []
    return [(n, os.path.join(root, n)) for n in sorted(os.listdir(root)) if os.path.isdir(os.path.join(root, n))]


def dam_choices(work, settings, read_rows):
    """[(id, label)] of the dams the engineer can pick for a one-dam run. `read_rows(ref,
    fields)` returns [(v1, v2, ...)] of a layer (the window reads QGIS layers, the tests files).
    From 1_dams.gpkg when it exists, else the DWR layer (structure_ / dam_name), else the pond
    layer; [] when nothing can be read."""
    S = dict(DEFAULT_SETTINGS)
    S.update(migrate_settings(settings))
    found = []
    dams = out(work, "1") if work else None
    try:
        if dams:
            for did, name in read_rows(dams, ("dam_id", "name")) or []:
                if did not in (None, ""):
                    found.append((str(did), "{} {}".format(did, name or "").strip()))
        dwr = slot_source("dwr", work, S, prepare=True) if not found else None
        if dwr:
            for sid, name in read_rows(dwr, ("structure_", "dam_name")) or []:
                if sid not in (None, ""):
                    found.append(("DWR-{}".format(sid), "DWR-{} {}".format(sid, name or "").strip()))
        ponds = out(work, "16") if work else None
        if ponds:
            for did, name in read_rows(ponds, ("dam_id", "name")) or []:
                if did not in (None, ""):
                    found.append((str(did), "{} {}".format(did, name or "").strip()))
    except Exception:                                            # noqa - a layer that cannot be read
        pass
    seen, uniq = set(), []
    for did, label in found:
        if did not in seen:
            seen.add(did)
            uniq.append((did, label))
    return uniq


# ------------------------------------------------------- the settings file --
PLUGIN_KEY = "ndscreen"


def window_settings_from(sf):
    """The window's choices stored in a suite settings file (its 'window' rows) as the keys of
    DEFAULT_SETTINGS, typed like the defaults. Unknown keys are ignored."""
    out = {}
    try:
        rows = sf.window(PLUGIN_KEY)
    except Exception:                                            # noqa
        return out
    for k, v in migrate_settings(rows).items():
        if k not in DEFAULT_SETTINGS or k in ("work", "package"):
            continue
        d = DEFAULT_SETTINGS[k]
        try:
            if k == "chain":
                out[k] = [x.strip() for x in str(v).split(",") if x.strip()] if not isinstance(v, list) else v
            elif isinstance(d, bool):
                out[k] = str(v).strip().lower() in ("true", "yes", "1", "y") if not isinstance(v, bool) else v
            elif isinstance(d, int):
                out[k] = int(float(v))
            elif isinstance(d, float):
                out[k] = float(v)
            else:
                out[k] = str(v)
        except (TypeError, ValueError):
            continue
    return out


def window_settings_to(sf, settings):
    """Put the window's shareable choices into a suite settings file object."""
    S = dict(DEFAULT_SETTINGS)
    S.update(migrate_settings(settings))
    for k in WINDOW_KEYS:
        v = S.get(k)
        if k == "chain":
            v = ",".join(v or [])
        if v in (None, ""):
            sf.set(PLUGIN_KEY, "window", k, None)
        else:
            sf.set(PLUGIN_KEY, "window", k, v)
    return sf


def write_run_settings(work, settings, log=None):
    """<work>/settings_used.xlsx: the settings file this run used (its values plus the window's
    choices), as the record a reviewer can open in Excel. Returns the path or ''."""
    from ..damsuite import settings as DS
    sf_path = (settings or {}).get("settings_file") or DS.current_path()
    sf = None
    if sf_path and os.path.isfile(sf_path):
        try:
            sf = DS.Settings.load(sf_path)
        except DS.SettingsError:
            sf = None
    if sf is None:
        sf = DS.Settings()
    window_settings_to(sf, settings)
    sf.meta["note"] = "written by the run that started {}; source file: {}".format(
        __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M"), sf_path or "(none: the tools' defaults)")
    out = os.path.join(work, RUN_SETTINGS)
    try:
        from ..damsuite import versions as vs
        from . import damrecord_io as _dio                     # 0.21.0: this plugin's own version
        sf.save(out, name=sf.meta.get("name") or "settings used",
                versions={PLUGIN_KEY: _dio.plugin_version() or vs.RELEASE.get(PLUGIN_KEY, "")})
    except Exception as e:                                       # noqa
        if log:
            log("settings record not written: {}".format(e))
        return ""
    if log:
        log("settings used: {}".format(out))
    return out


def algorithm_id(step):
    return "{}:{}".format(PROVIDER, ALG[step])


# ---------------------------------------------------------------- results --
PARAM_LABEL = {"OUTP": "breach points", "OUTL": "flow arrows", "IMP": "impacted features",
               "OUTX": "water bodies not written", "CREST": "crest line"}


def result_layers(work):
    """[(title, path)] of the results that exist, in workflow order (for 'Add to the project').
    Not step 0 (its package holds many layers and is an input) nor the folders of steps 2 and 10."""
    rows = []
    for key in KEYS:
        if key in ("0", "2", "10", "11", "12", "14", "15", "17"):
            continue
        for param in LAYOUT[key]:
            if param in ("OUT", "OUTP", "OUTL", "OUTX", "IMP", "CREST") and exists(work, key, param):
                rows.append(("{} - {}".format(key, TITLE[key]) + (
                    "" if param == "OUT" else " ({})".format(PARAM_LABEL.get(param, param))),
                    path(work, key, param)))
    return rows


def summary_counts(work):
    """{'dams': n, 'High': n, ..., 'review': n} from <work>/10_reports/summary.csv; None when
    the report has not been written. Class names as the file has them."""
    p = os.path.join(work, SUMMARY_CSV) if work else ""
    if not os.path.isfile(p):
        return None
    counts = {"dams": 0, "review": 0}
    try:
        with open(p, newline="", encoding="utf-8-sig") as fh:
            for row in csv.DictReader(fh):
                counts["dams"] += 1
                c = (row.get("hz_class") or "").strip() or "(none)"
                counts[c] = counts.get(c, 0) + 1
                if str(row.get("review") or "").strip().lower() in ("1", "true", "yes", "y"):
                    counts["review"] += 1
    except (OSError, csv.Error):
        return None
    return counts


def counts_text(counts):
    if not counts:
        return "No report yet (tool 10)."
    classes = [(k, v) for k, v in counts.items() if k not in ("dams", "review")]
    order = {"High": 0, "Medium": 1, "Low": 2}
    classes.sort(key=lambda kv: (order.get(kv[0], 9), kv[0]))
    text = "{} dam(s): {}".format(counts["dams"], ", ".join("{} {}".format(v, k) for k, v in classes))
    if counts.get("review"):
        text += "; {} flagged for review".format(counts["review"])
    return text + ". Screening classes only - the Department decides."


def dam_record_text(folder):
    """One line about the dam.json records in a folder of dam folders (tool 7b / 8)."""
    if not folder or not os.path.isdir(folder):
        return "dam.json: no dams folder yet (written by tool 7b; tool 8 adds to folders that exist)"
    try:
        from ..damsuite import dam_record as DR
    except Exception:                                            # noqa
        return "dam.json: not available"
    n = 0
    steps = {}
    bad = 0
    conflicts = 0
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return "dam.json: the dams folder cannot be read"
    for name in names:
        sub = os.path.join(folder, name)
        if not os.path.isdir(sub):
            continue
        try:
            rec = DR.load(sub)
        except ValueError:
            bad += 1
            continue
        except Exception:                                        # noqa
            continue
        if rec is None:
            continue
        n += 1
        conflicts += len([c for c in rec.conflicts if not c.get("applied")])
        for step in DR.STEPS:
            if rec.latest(step):
                steps[step] = steps.get(step, 0) + 1
    if not n and not bad:
        return "dam.json: no dam folder with a record in {}".format(folder)
    text = "dam.json: {} dam(s)".format(n)
    if steps:
        text += "; " + ", ".join("{} {}".format(v, k) for k, v in steps.items())
    if conflicts:
        text += "; {} value(s) the tools did not agree on".format(conflicts)
    if bad:
        text += "; {} record(s) could not be read".format(bad)
    return text



# ------------------------------------------------------------ run in order --
CHAIN_SKIP = frozenset({"7b", "11", "12", "13", "14", "15", "17"})   # never chained (Runner / checks / diffs)
CHAIN_OPTIONAL = ("0", "16", "18", "6", "9", "19")  # optional steps the engineer can add to a chain
# inputs a step cannot give a meaningful result without, besides its required parameters:
# any one of the slots
NEEDS_ANY = {"1": ("dwr", "nid")}


def chain_next(work, settings, include=(), ran=()):
    """(step, problem): the next step of a run in order. Every non-optional step (and the
    optional ones in `include`) that is not done - or is out of date - in workflow order, 7b
    never, a step at most once per chain (`ran`). (None, "") when nothing is left; (None,
    text) when the next step cannot start."""
    rows = {k: state for k, _t, state, _n in statuses(work, settings)}
    both = "18" in include
    for key in KEYS:
        if key in CHAIN_SKIP or key in ran:
            continue
        if key in OPTIONAL and key not in include:
            continue
        if rows[key] == "done" and _done_for_chain(work, key, both):
            continue
        missing = _missing_requirements(work, key, both)
        if missing:
            return None, "step {} cannot start: it needs step {}".format(key, " and ".join(missing))
        return key, ""
    return None, ""


def chain_plan(work, settings, include=()):
    """The steps a run in order is expected to run: those not done or out of date, and every step
    that follows one of them (it is out of date once the earlier one has run again)."""
    rows = {k: state for k, _t, state, _n in statuses(work, settings)}
    plan = []
    both = "18" in include                               # 0.13: dams AND ponds in one folder
    for key in KEYS:
        if key in CHAIN_SKIP or (key in OPTIONAL and key not in include):
            continue
        if key in POND_COVERS and not both and (("16" in plan) or ("19" in plan)):
            continue
        if rows[key] != "done" or not _done_for_chain(work, key, both) or \
                any(r in plan for r in _flat_requirements(key)):
            plan.append(key)
    return plan


def chain_problems(step, work, settings):
    """What step `step` lacks that a chain cannot ask the engineer for: data layers
    (NEEDS_LAYER / NEEDS_ANY) that are neither chosen nor in the package."""
    labels = dict((k, l) for k, l, _p in LAYER_SLOTS)
    out = []
    for slot in NEEDS_LAYER.get(step, ()):
        if slot_source(slot, work, settings) is None:
            out.append("no {} layer".format(_phrase(labels[slot])))
    anyof = NEEDS_ANY.get(step)
    if anyof and all(slot_source(s, work, settings) is None for s in anyof):
        out.append("no {} layer".format(" layer and no ".join(_phrase(labels[s]) for s in anyof)))
    return out


def required_missing(definitions, params):
    """Labels of the required parameters a tool has no value for. `definitions`:
    [(name, description, optional, has_default, is_destination)] of the tool's parameters."""
    out = []
    for name, desc, optional, has_default, destination in definitions:
        if optional or has_default or destination:
            continue
        v = params.get(name)
        if v is None or v == "":
            out.append(desc or name)
    return out


def step_status_counts(work, step):
    """{status: count} and the first failure note of the step's table, or ({}, "") when it has
    no status column or cannot be read. Step 7 writes sim_status for every dam: 'FAILED' means
    the flood did not run, although the tool itself finished and wrote its table. Reads the
    GeoPackage with sqlite3 (no QGIS needed)."""
    import sqlite3
    if step != "7":
        return {}, ""
    p = path(work, step, "OUT")
    if not os.path.isfile(p):
        return {}, ""
    counts, first = {}, ""
    con = None
    try:
        con = sqlite3.connect(p)
        tbl = con.execute("SELECT table_name FROM gpkg_contents WHERE data_type='features' "
                          "LIMIT 1").fetchone()
        if not tbl:
            return {}, ""
        t = tbl[0].replace('"', '""')
        cols = [r[1] for r in con.execute('PRAGMA table_info("{}")'.format(t))]
        if "sim_status" not in cols:
            return {}, ""
        note = ", COALESCE(sim_notes, '')" if "sim_notes" in cols else ", ''"
        for st, n in con.execute('SELECT COALESCE(sim_status, "")' + note + ' FROM "{}"'.format(t)):
            counts[st] = counts.get(st, 0) + 1
            if st == "FAILED" and not first:
                first = n
    except Exception:                                    # noqa - unreadable: no verdict
        return {}, ""
    finally:
        if con is not None:
            con.close()
    return counts, first


def step_problem(work, step):
    """None when the step's results are usable, else (all_failed, text). A step can finish
    (the tool ran, its table exists) with every dam FAILED; the chain must not carry that on
    to the classification (it would classify NoData and report OK)."""
    counts, first = step_status_counts(work, step)
    bad = counts.get("FAILED", 0)
    if not bad:
        return None
    total = sum(counts.values())
    why = " ({})".format(first) if first else ""
    return (bad == total, "the flood did not run for {} of {} dam(s){}".format(bad, total, why))


def chain_summary(step, work, settings):
    """Lines saying which data the step will use and which it will run without, for the log
    and the confirmation before a chain starts."""
    p = defaults(step, work, settings)
    lines = []
    if step == "7":
        if p.get("BASELINE"):
            lines.append("step 7: no-breach baseline run too (ambient {:g} cfs); increment rasters".format(
                p.get("BASELINE_CFS", 0.0)))
        if p.get("BUILDINGS"):
            bld = str(p.get("BLD") or "")
            shown = os.path.basename(bld.split("|")[0]) + ("|" + bld.split("|", 1)[1] if "|" in bld else "")
            lines.append("step 7: buildings {} ({})".format(
                "raise the DEM" if p["BUILDINGS"] == 1 else "Manning's n",
                "footprints = " + shown if bld else "NO footprint layer - nothing will be done"))
    if step == "8" and p.get("INCREMENTAL"):
        lines.append("step 8: impacts counted from the incremental depth (needs step 7's baseline)")
    for slot, label, _pkg in LAYER_SLOTS:
        param = {"structures": "BLD", "roads": "ROADS", "rail": "RAIL", "lifelines": "LIFELINES"}.get(slot)
        if step in ("6", "8") and param:
            if param in p:
                lines.append("step {}: {} = {}".format(step, _phrase(label), os.path.basename(
                    str(p[param]).split("|")[0]) + (("|" + str(p[param]).split("|", 1)[1])
                                                     if "|" in str(p[param]) else "")))
            else:
                lines.append("step {}: no {} layer - it cannot affect the result".format(
                    step, _phrase(label)))
    return lines


class Chain(object):
    """Runs steps one after the other. The window gives `start(step, algorithm id, params,
    on_done)` which starts the tool (in the background) and calls on_done(ok, message) when it
    ends; this class decides the order, stops at the first problem and checks each step wrote its
    output. Pure Python (the tests drive it with a synchronous `start`)."""

    def __init__(self, work, get_settings, include, start, log=None, problems=None, on_end=None):
        self.work, self.get_settings, self.include = work, get_settings, tuple(include)
        self._start, self.log, self.problems, self.on_end = start, log or (lambda m: None), problems, on_end
        self.ran, self.active, self.current = [], False, None
        self.result = ""
        self.ok = False
        self._cancel = False
        self._step_t0 = None

    def begin(self):
        self.ran, self.active, self.current, self.result, self.ok = [], True, None, "", False
        self._cancel = False
        self._step_t0 = None
        self._chain_t0 = _time.time()
        self._advance()

    def cancel(self):
        self._cancel = True

    def _end(self, ok, text):
        self.active, self.ok, self.result, self.current = False, ok, text, None
        total = _time.time() - self._chain_t0 if getattr(self, "_chain_t0", None) else 0
        self.log(("=== " if ok else "!!! ") + text + (" ({:.0f}s total)".format(total) if total > 0 else ""))
        if self.on_end:
            self.on_end(ok, text)

    def _advance(self):
        if self._cancel:
            return self._end(False, "Stopped by you after step(s) {}.".format(", ".join(self.ran) or "-"))
        S = self.get_settings()
        step, problem = chain_next(self.work, S, self.include, self.ran)
        if problem:
            return self._end(False, problem[0].upper() + problem[1:] + ".")
        if step is None:
            return self._end(True, "Nothing left to run; ran step(s) {}.".format(
                ", ".join(self.ran) or "none (all were done)"))
        aid, params = algorithm_id(step), defaults(step, self.work, S, log=self.log, prepare=True)
        lacks = chain_problems(step, self.work, S)
        if self.problems:
            lacks += list(self.problems(step, aid, params) or [])
        if lacks:
            return self._end(False, "Step {} ({}) cannot run: {}. Choose it in the Inputs / Settings "
                                    "tab, or open the step and fill it in.".format(
                                        step, TITLE[step], "; ".join(lacks)))
        self.current = step
        self._step_t0 = _time.time()
        for line in chain_summary(step, self.work, S):
            self.log("    " + line)
        self.log(">>> Step {} - {}".format(step, TITLE[step]))
        self._start(step, aid, params, lambda ok, msg="", _s=step: self._finished(_s, ok, msg))

    def _finished(self, step, ok, message=""):
        if not self.active or step != self.current:
            return                                              # a late call after a stop
        if not ok:
            return self._end(False, "Step {} ({}) {}{}".format(
                step, TITLE[step], "did not finish" if not self._cancel else "was cancelled",
                ": " + message if message else "."))
        if not is_done(self.work, step):
            return self._end(False, "Step {} ({}) finished but its output is not in the work folder "
                                    "({}).".format(step, TITLE[step], LAYOUT[step].get(
                                        DONE_MARK.get(step, "OUTDIR"), "")))
        prob = step_problem(self.work, step)
        if prob and prob[0]:
            return self._end(False, "Step {} ({}) finished but {}. Nothing after it was run; "
                                    "look at the tool's log and the sim_notes column.".format(
                                        step, TITLE[step], prob[1]))
        if prob:
            self.log("    warning: step {}: {}; those dams get no class in the next step".format(
                step, prob[1]))
        self.ran.append(step)
        elapsed = _time.time() - self._step_t0 if self._step_t0 is not None else 0
        self.log("    step {} done ({:.0f}s)".format(step, elapsed))
        self._advance()
