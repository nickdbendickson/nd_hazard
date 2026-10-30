# ND Dam Breach Runner

A QGIS plugin (3.28 LTR and later, macOS/Windows/Linux) that runs dam breach floods
with **LISFLOOD-FP** and, optionally, **ANUGA**, following the North Dakota Dam Safety
Standards Policy (REG_05.2026). It writes the rasters the ND hazard classification plugin
reads:

## New in 3.26.1: HEC-RAS results imported before 3.26.1 are too deep and too wide, so re-export them

**Who needs this.** Anyone who turned a HEC-RAS 2D plan into rasters with *Import HEC-RAS 2D
results as rasters* (`ndbreach:hecrasimport`) before 3.26.1, or who runs it in 3.26.1 without
the new Terrain input. That covers `<dam folder>/_hecras/HECRAS Depth (Max).tif` and the other
`HECRAS *.tif` rasters, plus any depth, flooded area, structure count or hazard class worked out
from them, including the benchmark against the Runner's own run. LISFLOOD-FP and ANUGA results
aren't affected.

**What was wrong.** HEC-RAS works on large cells (150 ft on DWR-2801), and each cell has one
water surface. The import tool turns those cells into pixels on the dam folder's grid. Before
3.26.1, each pixel's depth was the cell's highest water surface minus the *lowest* ground point
anywhere in that cell (HEC-RAS's "Cells Minimum Elevation"). So when a cell held a ditch, a
stream channel or a culvert outlet, every pixel in it got the ditch's depth, even where the
ground was a few feet higher. The wet area and the arrival time used that same depth. As a result:
- **Depths were too high** across most cells that hold a low spot.
- **The flooded area was too large**, because pixels on higher ground counted as wet or at 1 ft
  or more.
- **Structures, roads and rail looked deeper than they were**, so they could land in a worse
  danger zone or count as flooded when they weren't.
- **Arrival came too early** on higher ground inside such a cell.

The HEC-RAS model itself was fine. The error was only in how its results were read.

**An example: DWR-2801** (from the DWR Runner, which had the same error and got the same fix in
DWR_Plugins #173), over the area both models cover (51,387 ac):

| | Before the fix | With the fix | ANUGA |
|---|---|---|---|
| Area at ≥ 1 ft | 5,060 ac | **1,923 ac** | 1,960 ac |
| Area at ≥ 0.1 ft | 17,420 ac | 8,843 ac | 6,718 ac |
| Wet area | 18,727 ac | 10,657 ac | 8,290 ac |

The 2 lethal and 16 judgment structures behind HEC-RAS's ACER TM 11 High class there came from
this error. The fix doesn't remove real differences between the models. On DWR-2801, HEC-RAS
still arrives earlier at Casselton and loses more volume.

**What 3.26.1 does.**
- **Depth from the terrain.** Give the import tool the terrain the HEC-RAS model used. Each
  pixel then gets the highest water surface of its nearest HEC-RAS cell, minus that terrain
  averaged onto the pixel. Depth never goes below 0. A pixel inside the mesh with no terrain
  falls back to the old lowest-point depth, and a note gives the number of pixels.
- **Wet area on the same depth.** A pixel is wet at 0.01 ft or more, and the depth, water
  surface, velocity and time-of-max rasters are blank where it is dry.
- **Arrival per pixel, on the same depth.** Arrival is the first time the cell's water surface
  over time, minus that pixel's terrain, reaches the arrival depth. It's interpolated between
  output times and counted from the time of breach initiation. Only pixels whose maximum depth
  reaches the arrival depth get an arrival time. A pixel that reaches it only between two output
  times gets the cell's time of the maximum instead, and a note gives the number of pixels. The
  arrival depth is the tool's *Arrival depth* (`ARRIVAL_FT`) or the re-export's
  `--arrival-depth-ft`, 0.1 ft by default in both (1 ft before 3.26.3).
- **The method is recorded** as `depth_basis`: `terrain` (from the terrain) or `cell_min` (the old
  lowest-point depth).

**The import tool's new Terrain input.** *Import HEC-RAS 2D results as rasters* has a new
optional **Terrain** input ("Terrain the HEC-RAS model used", `TERRAIN`). Give it the model's
terrain GeoTIFF, normally `<HEC-RAS project>/Terrain/<name> Terrain.tif`. Don't use HEC-RAS's
processed `Terrain.<name>.tif` copies. Leave it empty and the results are the same as before
3.26.1, and the log adds the warning "Check: No terrain given: depth is the water surface minus
each cell's minimum elevation, so a ditch deepens its whole cell."

**Check which method a result used.**
- **A re-export** (below): open `reexport.json` in its folder and look at `depth_basis`. It's
  `"terrain"` when the depth came from the terrain. The command line also prints
  `Depth basis: terrain`. From a terminal:
  `python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['depth_basis'])" "DAM_FOLDER/_hecras/reexport_<UTC>/reexport.json"`
- **An import tool run**: the import tool writes no file with `depth_basis`, so check its log. With
  the Terrain input given there is no "No terrain given" warning. With that warning, the result is
  `cell_min`.
- **`_hecras/HECRAS *.tif` made before 3.26.1**, or any you can't trace to a terrain import or a
  re-export, use the old lowest-point depth. Re-import them with Terrain, or re-export them.

**Re-export an old result (HEC-RAS doesn't run again).** The re-export reads the finished plan HDF
(`.p##.hdf`) and the model terrain again and writes new rasters.
- **Command line:**
  `python3 nd_breach_runner/scripts/ndbreach_cli.py hecras-reexport PLAN.hdf --dam-folder DAM_FOLDER`
- **Python** (QGIS Python console, or any Python with numpy, h5py and GDAL):
  `from nd_breach_runner.core import hecras; hecras.reexport_results("PLAN.p01.hdf", dam_folder="DAM_FOLDER")`
- **The grid.** It needs a grid to write on: the dam folder's `Depth (Max).tif`, else its
  `_hecras/HECRAS Depth (Max).tif`, else a raster given with `--grid` (`like=` in Python). With
  just `PLAN.hdf` and no dam folder or grid, it stops and asks for one. The HDF, the terrain and
  the grid must use the same coordinate system.
- **The terrain.** By default it takes the first `Terrain/*.tif` in the HDF's folder or the folder
  above it, skipping HEC-RAS's `Terrain.*.tif` copies. Give `--terrain` (`terrain=`) to choose one.
- **What it writes.** It writes `HECRAS Depth (Max).tif`, `HECRAS Velocity (Max).tif`,
  `HECRAS Arrival Time.tif`, `HECRAS WSE (Max).tif`, `HECRAS Time of Max WSE.tif` (each where the
  HDF has the data) and `reexport.json` to a new folder,
  `DAM_FOLDER/_hecras/reexport_<UTC time>`. With no dam folder, that folder goes next to the HDF.
  `reexport.json` records the inputs, `depth_basis`, the pixels without terrain
  (`no_terrain_cells`) and the areas both ways (`areas_old_cell_min` and `areas_new`: wet,
  ≥ 0.1 ft and ≥ 1 ft, in acres). The command also prints the wet area both ways.
- **It doesn't touch your existing results.** The rasters already in the dam folder and in
  `_hecras` stay as they are. If the output folder already holds any of these files, it stops,
  unless you add `--overwrite` (`overwrite=True`).
- **Options:** `--out DIR` (the output folder), `--terrain`, `--grid`, `--area` (the 2D flow
  area, default the first), `--units us|si`, `--terrain-z` (the terrain's elevation unit: `auto`,
  `feet`, `us_feet` or `meters`, default read from the raster), `--t0-hr` (the plan hour that
  arrival counts from, as the import tool's time of breach initiation) and `--arrival-depth-ft`
  (default 1 ft).

**Rerun Hazard afterwards.** Run the ND hazard classification (this plugin's *ND hazard
classification (Section 5.2)* or the ND Dam Hazard Classification Helper) again on the new
rasters. Point its depth, velocity and arrival inputs at the files in the `reexport_<UTC time>`
folder, or at the re-import. Treat any classification, structure count, benchmark or map made
from the old HEC-RAS rasters as out of date.

**Hazard Helper's own HEC-RAS plan import** (`ND_Dam_Hazard_Helper/rasimport.py`, the HEC-RAS
plan tab) is a separate fix, Hazard 2.34.4 (nd_hazard #14). With a terrain it already took depth
from the terrain, but its arrival came from each cell's lowest point. In 2.34.4, arrival is per
pixel from the terrain, interpolated, with the time-of-max fallback, and plans converted before
are converted again. Without a terrain raster, its depth still comes from each cell's lowest
point, so give it the terrain.

## New in 3.20.0

Everything the 2026-10 walk-through and review asked for. **No number in an existing run
changes**, with one exception that is off by default and named below.

**The window fits a laptop.** The DEM download box is a collapsible group that folds itself
away once the dam has a DEM; the four engineering settings that sat in the header (grid size,
simulated time, longest simulated time, force ANUGA) moved to a new **Advanced** tab; the tool
tabs and the log may shrink. The minimum height is 560 px instead of 902, so *Run breach model*
is above the fold on a 768 px screen.

**A grey button says why.** Every disabled Run button carries the reason in its tooltip and in
a status line under the tabs, naming the missing input and the button that fixes it - *"needs a
DEM: Download DEM for this dam"*. The reasons are the suite's own blocking set-up errors
(`damsuite.suitecheck`) plus what this plugin can see without reading a raster: a Dams folder, a
dam ID, a DEM, the dam's height and storage, and - for a results tool - a finished run. Tools
that need nothing of a dam (check the installation, the self-test, the data tools) are never
greyed out, so a new user always has something to press.

**Hand-off fidelity.** What ND Dam Breach Screening ran for a dam - grid size, breach method,
scenario - is read back from the dam folder and used to fill the *empty* CELL, METHOD and
SCENARIO of the run tools and the wizard. The page says where it came from: *"from dam.json (ND
Dam Breach Screening 0.28.0, 2026-10-10)"*. It is read from dam.json's newest `screened` status
result (keys `cell_m`, `method`, `scenario`) and, failing that, from
`<dam folder>/_screening_model/run_manifest.json`. Nothing is guessed from a file name or a
raster's pixel size: a value the screening has not recorded stays missing, and the page and the
run report say which of the three it is and that the Screening plugin must record it. A grid
size you typed is never replaced.

**Walk-through fixes.** The grey DEM-source note has a row of its own (it used to be drawn over
*Grid size*) and shows the file name, with the full path in its tooltip. The *ND LiDAR path* row
is hidden unless Source = ND LiDAR. A DEM whose `UNITS` metadata item says metres or feet is
read that way instead of logging *"no units recorded"* - which is what a mosaic from the
screening's step 2 does - and the Runner's own VRT builder writes that tag. After each finished
pass the log names the rasters that can already be opened in the dam folder.

**Population at risk and loss of life** (`damsuite.loss`). The consequences step and its report
now carry PAR by day and by night, the flood-severity split (high / medium / low) and the
warning-time split (under 15 min / 15 to 60 / over 60), written as *Population at Risk
(PAR).csv*. Loss of life is estimated **only** from a fatality rate table an engineer has
entered and signed off: `core/fatality_rates.json` ships empty and unsigned, and until it is
signed every report says loss of life is not estimated and why. Sign it in the window's Advanced
tab or with *Sign off the fatality rate table* (`ndbreach:signrates`); a signed table with an
empty cell still produces no number, because a total that left those people out would be too
low. These numbers are reported **beside** the consequences tool's own estimate, never instead
of it.

**Road and railroad crossings** (`ndbreach:crossings`). One row per unbroken stretch of road or
railroad the flood wets: when it first gets wet, the maximum depth and velocity there, how long
it stays wet and whether it counts as closed to traffic, as *Road and Rail Crossings.csv*,
*.geojson* and *.md* in the dam folder. The duration is bounded by the end of the simulated run
and the column says so rather than passing an upper bound off as a measurement. The closure
rule is yours (0.5 ft deep or 7 ft²/s), not a published standard.

**Cascade check** (`ndbreach:cascadecheck`). After a run, the flood footprint is tested against
the dam inventory (every dam folder's dam.json plus a dam layer you give) with
`damsuite.cascade`, and the dams the flood reaches are listed nearest first. For each one the
next job is written to `_cascade/next_jobs.json` and this run's breach outflow is copied into
that dam's own `inputs/upstream_breach_inflow.csv`; where it already has a `pmf_inflow.csv`, the
**sum** of the two is written beside it. The inflow is **recorded, not consumed**: the tool does
not start the next run and never replaces an existing PMF inflow, because a PMF inflow may be
the larger flow and overwriting it would quietly make the next run milder.

**Crossings as embankment gaps.** Inventory crossings (NBI bridges and culverts from the offline
pack, rail grade crossings, your own surveyed layer; parameter `CROSSINGS`, filled from the pack)
are measured against the embankment the bare-earth DEM leaves across them, and every blocking
one is reported in the log and the run report whether or not anything is done about it. With
`CROSSING_GAPS` on, the ones whose **span and rise are surveyed** are cut as gaps using the
existing culvert / notch machinery; a crossing with no surveyed opening is never cut. **This is
the one option that changes a result**, and it is off by default: a gap lowers the water behind
the road, which is the less conservative direction for anyone upstream of it.

**Compare two runs** (`ndbreach:compareruns`). A depth difference raster (B minus A, dry in both
= no data) with its statistics, the two runs described from their run reports, and - with a
structures layer - the structures whose ACER TM 11 danger zone is not the same in the two runs
and which way it moved.

**A queue of dams** (`ndbreach:queue`). One run per dam ID of a list (the Screening priority
list's *Dam ID* column, or one ID per line), with each dam's values from its own dam.json.
`_queue/breach_queue.json` records each dam as waiting, running, done, skipped or failed, so the
work can be cancelled and picked up where it stopped. **Two dams run side by side at one thread
each** by default: that measured 1.76x faster than one dam on two threads, and oversubscribing
the solvers was much slower than either. The batch tool's default is two dams as well. A speed
setting only - it changes nothing about any result.

## New in 3.19.0

**Streams and waterbodies are USGS 3DHP.** With ND Dam Hazard 2.29.0 (suite 1.15.0) the stream lines and
waterbodies the culvert finder and the pond finder read come from the USGS 3D Hydrography Program instead
of NHD: the offline pack layers are 3DHP_flowlines and 3DHP_waterbodies, the servers list has 3DHP as the
default, and each 3DHP feature carries the NHD name and type fields too (gnis_name, ftype). A pack from
before 1.19 (NHD_ layers) is still read. The model is unchanged.

## New in 3.18.0

**Download the DEM from the wizard.** The terrain page has a button *Download DEM for this dam*
with the resolution (10 m, or 1 m) next to it. It runs the same download as the Runner window's
DEM box (ND Dam Breach Screening's DEM tool: USGS 3DEP around the dam point, the buffer half the
run tool's largest domain side) for the wizard's dam, writes the GeoTIFF into the dam folder as
`data/dem_<cell>m.tif` (EPSG:5070, metres), records it in dam.json and chooses it as the DEM when
it finishes. The tool's own window shows the progress; the page's note says what happened. Needs
ND Dam Breach Screening, as the window's box does.

**The last finished pass is kept.** After every finished LISFLOOD-FP run (pass) the depth,
velocity and arrival rasters are written to the dam folder with the final names and units
(`Depth (Max).tif`, `Velocity (Max).tif`, `Arrival Time.tif`), so a run that is cancelled or dies
later still leaves the last finished pass. The final results replace them; `products_pass` in
`run_report.json` says which pass the rasters on disk come from (the final pass of a finished
run). One write per pass, nothing else.

**Results cut off.** When a run ends with the flood still moving - the duration cap, the number
of domain runs (`MAX_ITERATIONS`) or the largest domain side (`MAX_EXTENT_MI`) - the run report
(`run_report.md`) has a section *Results cut off* before *Check these* with the reason and the
warning text, `run_report.json` has `cut_off = {"reason", "detail"}`, and the dam.json run entry
records it too. The reasons are `duration cap`, `domain-run limit` and `largest domain side`.

Smaller: the tool version in the reports is read from `metadata.txt` (it was a copy in the code
that went stale); the ANUGA worker no longer inherits `PYTHONNOUSERSITE` from QGIS, so an ANUGA
installed with `pip --user` is found.

## New in 3.14.0 (ND dam suite release set: ND Dam Hazard 2.24.0, ND Dam Breach Screening 0.22.0)

The offline pack is now a folder, ND_Dam_Suite_Pack, with one GeoPackage per layer (structures/, roads/, railroads/, lifelines/, hydro/, dwr/, hifld/, boundaries/, landcover/). Each file opens in QGIS by itself. LAYERS.txt in the folder lists every layer, its file, feature count and source, and what was left out. The DWR inventory files, the HIFLD lifeline layers and NLCD land cover are inside the pack, so the pack alone is enough. An older nd_offline_pack.zip still works, and the dwr/, hifld/ and nlcd/ drop-in folders are still read when the pack lacks a layer.

Data source, shared by the three plugins: Offline pack (choose its folder or zip) or Download from the servers (the pack is not read). Set it in Suite data and servers in any of the plugins; the setting is the same in all three. The wizard's DWR dam IDs and the suite layer list take the DWR layers from the pack.

## New in 3.13.1 (ND dam suite release set: ND Dam Hazard 2.23.1, ND Dam Breach Screening 0.21.1)

**Pick a county in the wizard.** The dam page has a County list under the dam layer. With a county
chosen, the dam list shows only the dams whose point is inside that county (a dam point just over
the state line goes by the layer's county field), and the note says how many there are. The
statewide DWR inventory has over 10,000 dams, more than the list's 5,000, so this is also how to
reach every dam. The county outlines ship with the plugin (`core/nd_counties.geojson`: the 53
counties of the suite offline pack, simplified to about 10 m); the same file is in ND Dam Breach
Screening.

## New in 3.13.0 (ND dam suite release set: ND Dam Hazard 2.23.0, ND Dam Breach Screening 0.21.0)

Ideas taken from the DamSafety plugins audit (2026-10-08) and rebuilt on this plugin's own rules.

**Pre-run check.** Just before the flood solve, `damsuite/solvegate.py` checks that the DEM
covers the dam point (and that most of the ground around it has data), that the breach
hydrograph has a peak and a volume (a 2-point straight line is a warning), and that the
LISFLOOD-FP executable runs 1 simulated second on a 5 x 5 grid. The smoke run happens once
per executable and modification time, never for the built-in engine, and
`ND_DAM_PRE_RUN_SMOKE=0` in the environment skips it. A failure stops the run in seconds
with the reason; the run report lists every check (`pre_run_check`).

**Layers that miss the study area.** A structures layer (`RECEPTOR_POINTS`) or culvert
layer (`STRUCTURES`) whose whole extent misses the study area stops the run, and the
message says which of two things is wrong: the layer has no CRS, or it has one and simply
does not overlap. A statewide layer with nothing near this dam passes.

**The DWR dam line is the crest.** `CREST_LINE` now fills from the dam folder's DWR dam
lines (`dwr_dam_lines`, written by Get data for this dam). The line whose `structure_id`
matches the dam (dam.json `dwr_structure_id`, or a dam ID `DWR-nnnn`) wins; otherwise the
nearest line within 500 m. When you gave no `BREACH_POSITION`, a dam point off the line is
moved to the nearest point on it, and the log says by how much.

**Breach inflow fallback.** The first search is unchanged (lowest cell at least half the
dam height down near the dam point), so earlier runs give the same results. When it finds
nothing, the nearest such cell within 400 m is used. When that also fails and the top of
dam is known, the dam point moves to the highest ground within 400 m that is within
max(3 ft, a quarter of the dam height) of the top of dam, and the search runs from there.
Each fallback is a warning in the report, with the point you gave (`dam_xy_given`).

**Culvert candidates.** Every place a road (the `RECEPTOR_ROADS` layer) crosses the
downstream flow path with no inventory culvert, bridge or notch within 30 m is listed in
the report (`culvert_candidates`), in `culvert_candidates.csv` and in the HEC-RAS model
folder (`GIS/culvert_candidates.csv`, a section in `README_HECRAS.md`). They are a list to
check in the field. They are never put in the model.

**Terrain downloads.** Through the suite terrain cache, a tile whose request fails (a big
3DEP export can time out) is asked for again as four quarters and joined (needs GDAL). The
cache grid and tile size are unchanged, so cached tiles stay valid.

**Load the dam's data layers** (Plugins > ND Dam Breach Runner): the layers a run of this
dam would take from the dam folder, plus the suite data folder's DWR dams and dam lines, in
one layer group, so you can see them before the run. A layer already in the project is not
added again.

**Wizard.** With no layer chosen, the dam page starts on the suite data folder's DWR dam
inventory (added to the project when needed). Each dam is listed as ID, name, height and
storage; a DWR dam without an NID ID is `DWR-<structure id>`. DWR's `max_pool_vol` and
`max_pool_area` fill the storage and area.

## New in 3.12.0 (ND dam suite release set: ND Dam Hazard 2.22.0, ND Dam Breach Screening 0.20.0)

**The computer is kept awake while a model runs.** LISFLOOD-FP, ANUGA and the built-in
engine now ask the operating system not to sleep for as long as the run lasts (shared
`damsuite/keepawake.py`, damsuite 1.8.0). Windows: `SetThreadExecutionState`; macOS:
`caffeinate`; Linux: `systemd-inhibit`, used only after a test call works. It runs beside
the solver, not around it: LISFLOOD-FP is still the Runner's own process, so Cancel still
stops it, and a missing or broken sleep blocker never stops a run. The log says "Keeping the
computer awake while LISFLOOD-FP runs (...)" or, once, why it could not. Set the environment
variable `ND_DAM_KEEP_AWAKE=0` to switch it off. Closing a laptop's lid still puts it to
sleep. The self-test (`ndbreach:selftest`) has a new line, "Keep the computer awake during
runs": PASS on a computer where it works, SKIP with the reason where it does not. Tested on
Linux only; on Windows and macOS run the self-test once to see. (Grok's version wrapped the solver in `systemd-inhibit`, so on
a Linux machine without a systemd bus every flood run failed with "Failed to connect to
bus".)

**Near-dam refine is off by default again** (`REFINE_NEAR`, run one dam, batch and
Screening's hand-off). LISFLOOD-FP 8.1 writes no maximum-velocity file (`.maxVc`) for the
FV1 solver the refine uses, and 3.8.0 - 3.11.x merged that as 0 ft/s in every wet cell of
the fine box (Eddy County: DWR-4664 5.54 ft/s with the refine against 24.74 without). When
the refine is turned on and FV1 wrote no velocity, depth, water surface and arrival come
from the fine run and the ACC velocities are kept; the report's `refine_near_dam` section
says so (`velocity_kept_acc`) and the log gives a warning.

**HEC-RAS model export: the run's culverts, bridges and notches.** The HEC-RAS 2D export
(`ndbreach:hecrasmodel`) now writes the culverts, bridges and notches from the run's
structure layer that lie in the downstream 2D area as connections inside the 2D area. Each
one gets a connection line across the road where the structure crosses it, a weir from the
terrain along the road crest, and either a culvert group of the run's size (box or pipe, in
feet or metres, with the inverts the run used or else the ground at the ends) or, for a
bridge or notch, an open gap in the weir (no deck, piers or pressure flow). Runs now record
their structures with the inverts the run actually used (`structures_used` in the run
report); runs before 3.12.0 export the structures as they were given. Structures outside
the 2D area, without a size, or too close to the edge or to another structure are left out
and listed with the reason. README_HECRAS.md lists every connection and what to check. The
layout GeoPackage gains "Structure connections" and "Structures from the run". A new option
("Put the run's culverts, bridges and notches in the 2D area", on by default) switches this
off. The keyword lines follow HEC's own example geometries (HEC-RAS 6.6 examples), but the
result has **not been opened in HEC-RAS**: check the connections in the Geometric Data
editor, add breaklines along the connection lines, and compute the property tables before
running. Without structures the export is unchanged.

## New: Set up ANUGA without installing conda (on top of 3.11.0)

*Plugins > ND Dam Breach Runner > Set up ANUGA (one-time download)* (Checks tab of the Runner window, Processing tool `ndbreach:setupanuga`, command line `ndbreach_cli.py setup-anuga`) installs ANUGA for you:

* it downloads **micromamba** (one small program; no conda, no administrator rights) from its official GitHub release and checks it against a SHA-256 pinned in `core/anuga_setup.py`;
* micromamba creates a **private environment** (Python 3.12, ANUGA from conda-forge, numpy, scipy, matplotlib) in the plugin's data folder (Windows `%LOCALAPPDATA%\ndbreach\anuga`, macOS `~/Library/Application Support/ndbreach/anuga`, Linux `~/.local/share/ndbreach/anuga`; `NDBREACH_ANUGA_HOME` moves it) and sets *ANUGA Python* in the provider options to it. The Runner also finds that environment on its own, so the command line needs no setting;
* it needs the **internet once** (github.com and conda.anaconda.org; behind a proxy set `HTTPS_PROXY`): about 360 MB to download and 1.6 GB on disk, a few minutes. Running it again does nothing when ANUGA already works; *Create it again* rebuilds it and *Remove* deletes it;
* Windows 64-bit, Linux x86-64 and macOS (Intel and Apple silicon) have ANUGA on conda-forge. **Windows on ARM and Linux on ARM do not**: the tool says so and the Runner keeps working with LISFLOOD-FP or the built-in engine. ANUGA stays optional.

The conda route below (`scripts/create_anuga_env.sh`) still works and is used first when *ANUGA Python* points at it.

## New in 3.11.0 (ND dam suite release set: ND Dam Hazard 2.19.0, ND Dam Breach Screening 0.18.0)

Fixes from the Elm River Detention Dam 1 run (`RUNNER_FV1_INSTABILITY.md` has the cause). Nothing
here has been run in a real QGIS yet.

- **The near-dam fine run is checked before it is merged.** It is rejected, and the ACC results are
  kept everywhere, when the water in its domain is more than 10 % above what was injected plus what
  was already ponded (`max_domain_volume_ratio`), when water still enters across the boundary after
  the flood (more than 2 % of the injected volume), or when its depth is over twice the dam height.
  The report says which check failed. At Elm River Dam 1 the unchecked fine run put 39,137 ft of
  water on the map.
- **The fine run closes the upstream and side edges of its box.** In LISFLOOD-FP an edge that the
  `.bci` does not list is a wall; the FV1 solver imports water through a FREE edge that sits against
  ponded water, and that growth is exponential. Only the downstream edge stays free.
- **The fine run's clock follows the flood.** It is the travel time to the end of the box (twice the
  front's travel time, at least 2 h, never more than the main run), not the main run's 70 h.
- **The dam line is raised to the top of dam in the model DEM** (`RAISE_CREST`, on by default; the
  source DEM is not changed). A DEM smooths the crest (4 ft under the top of dam at Elm River), so
  the breach water spilled back over it and filled the empty reservoir basin. The line is the
  `CREST_LINE` layer when you give one; otherwise it is traced from the DEM along the ridge by the
  dam point. A dam whose top comes from the DEM, or with a proposed embankment, is left as it is.
- **`DURATION_HR` 0 means automatic** (it did say so, but defaulted to 12), and the clock is
  lengthened while the flood is still moving, up to `DURATION_MAX_HR`.
- **Resume reuses finished runs when only the refine setting changed.** The resume key no longer
  includes the refine settings.
- The run report adds the late boundary inflow, the water in the domain against the injected volume
  and a section for a rejected fine run.

**Without installing LISFLOOD-FP** (from the Plugin run guide work, `standalone-1.7`; the engine itself ran in a real QGIS 3.34 on Linux, the Windows file only under Wine):

* **LISFLOOD-FP bundled for Windows 64-bit and Linux 64-bit.** The patched program (LISFLOOD-FP 8.2, GPL-2.0, with the `inittm_depth` patch) is in `bin/windows-x64/lisflood.exe` and `bin/linux-x64/lisflood`, with its complete source in `bin/source/` and the licence in `bin/COPYING-GPL-2.txt`; see `bin/README.txt`. An empty LISFLOOD-FP path now finds the installed program first, then the bundled one. The Linux build needs glibc 2.38 or newer. macOS has no bundled build.
* **Built-in flood engine** (`core/builtin_lisflood.py`, numpy only): the same local-inertial scheme, boundary conditions and input files as LISFLOOD-FP, for any computer including a Mac. Choose it with **Use the built-in flood engine** (Processing > Options > Providers > ND Dam Breach Runner), by typing `builtin` as the LISFLOOD-FP path (also in the command line's `--lisflood` and `model.lisflood_exe`), or it is used when nothing else is found. It is slower and a little more diffusive than LISFLOOD-FP; check results against LISFLOOD-FP before a submittal. The log and the Check tool say which engine ran.
* `scripts/patch_lisflood.py` also patches LISFLOOD-FP so it compiles with MinGW (Windows).
* The Windows search for an installed LISFLOOD-FP now includes `~/lisflood-fp` and `C:\lisflood-fp`.

## New in 3.10.0 (ND dam suite release set: ND Dam Hazard 2.18.0, ND Dam Breach Screening 0.17.0)

**A HEC-RAS 2D dam-breach model from a finished run.** *Build a HEC-RAS 2D model...* (menu, the
Tools tab of the Runner window, Processing tool `ndbreach:hecrasmodel`) writes a HEC-RAS 6.x
project laid out like HEC's tutorial *Dam Breach - Simple 2D Geometry*:

- the reservoir is a **storage area** with the plugin's elevation-volume curve, starting at the
  run's water surface;
- the dam is an **SA/2D connection** along the dam line (a weir at the top of dam, or the ground
  where higher) carrying the **breach** of the run (width, bottom, side slopes, formation time,
  piping or overtopping, trigger);
- the valley is a **2D flow area**: the run's flooded extent widened by a side buffer (1000 ft)
  plus an end buffer (2640 ft) where the inundation stops, cut straight along the dam line, with
  its computation points, and a **normal depth** boundary where the valley leaves it;
- for a flood (PMF) run, the inflow hydrograph goes into the storage area; sunny day takes an
  optional base inflow.

Files: `.prj`, `.g01`, `.u01`, `.p01`, `.rasmap`, `GIS/Projection.prj`, the DEM as
`Terrain/<name> Terrain.tif` in the model's coordinate system and vertical units, a GeoPackage of
the layout to check in QGIS and `README_HECRAS.md` with every value and its source. Units follow
the coordinate system: feet gives an English-units model, metres an SI model. Empty coordinate
system: the DEM's when it is in feet, else North Dakota State Plane (NAD83, international feet,
North or South zone by latitude) for a dam in North Dakota, else the run's.

**Left to do in HEC-RAS** (it writes these in its own binary format): make the RAS terrain from
the GeoTIFF, associate it with the geometry, compute the geometry (or let the plan's
preprocessor do it), run the plan. The text files follow HEC's example projects and were read
back with the ras-commander library; they have not been opened in HEC-RAS itself.

**Coordinate systems:**

- A DEM without an elevation unit in a feet CRS (ND State Plane) is no longer taken as feet when
  its elevations only fit North Dakota as metres (a 3DEP DEM warped to State Plane keeps metre
  values); the run report says how the units were decided.
- The standalone consequences tool measured flooded road and rail lengths in the rasters' own
  units, so a DEM in State Plane feet gave lengths 3.28 times too long (Web Mercator: about 1.46
  times); lengths are now ground metres.
- The command line's transformation cache is keyed by the coordinate system, not the Python
  object, so two layers in different CRSs can no longer share a transformation.

**Linux and macOS:** the LISFLOOD-FP executable and ANUGA Python parameters accept a file name
without an extension (QGIS 3.34 refused `/opt/lisflood/lisflood` with "Incorrect parameter value").

Checked in a real QGIS 3.34 (Linux) with the other two plugins: the self-test (no LISFLOOD-FP or
ANUGA there, so no flood run) and the HEC-RAS model tool on synthetic runs.

## New in 3.9.0 (ND dam suite release set: ND Dam Hazard 2.17.0, ND Dam Breach Screening 0.16.0)

**One download, everything fills in.** *Plugins > ND Dam Suite > Suite data and servers...* (also
in this plugin's menu) sets the suite data folder and the servers once per computer, and **Get
data for this dam** (that window, the Data tab of the Runner window, or the Processing tool
`ndbreach:getdamdata`) writes the dam's DEM, land cover, structures, roads, railroads,
waterbodies ... into `<dam folder>/data/` and records them in `dam.json`. Then:

- The tools fill their **empty** inputs from the dam folder and say so in the log; what you
  choose is never replaced. Run one dam, sensitivity, estimate, nested and probabilistic: `DEM`,
  structures / roads / railways (`RECEPTOR_*`), `NLCD_RASTER`, `PONDS` (NHD waterbodies) and the
  flood scenario's `NORMAL_POOL_FT` (from dam.json; refused when dam.json gives its datum as
  local). The DEM may now be left empty in their dialogs when the dam folder has one; without
  either the run stops at once with a clear message. Consequences: `STRUCTURES` (NSI-style
  structure points), `ROADS`, `RAIL`. ND hazard classification: `STRUCTURES`, `ROADS`, `RAIL`,
  `LIFELINES`, `DOWNSTREAM_DAMS` (NID dams). The culvert / bridge lines (`STRUCTURES` of the run
  tools), the batch and cascade tools and the animation's DEM are not filled.
- **Runner window:** an empty DEM choice means the dam folder's DEM, shown as "(from dam folder)".
  An empty Dams folder starts as the suite's, and the first one you choose becomes the suite's.
- **Wizard:** opened from the window it starts with the window's Dams folder, dam and DEM. The
  Dams folder moved to page 1, so dam.json fills the height and storage before page 1's check.
- **DEM download box:** the dam point comes from dam.json, the buffer starts at half the run's
  largest domain (30 mi; the domain can grow to 60 mi) and the cell at 10 m. The DEM is saved as
  `<dam folder>/data/dem_<cell>m.tif`, recorded in dam.json, loaded and put into the DEM choice.
- Results that the Processing tools load (rasters and flood polygons) get the suite's styles, as
  the window's "Add the dam's results" does.
- The ANUGA Python parameter is a file picker again (same name, so saved values still work).
- The Word report no longer counts a LibreOffice without its Draw module as an SVG converter.

None of this has been run in a real QGIS (simulated QGIS and Qt only).

## New in 3.8.0 (ND dam suite release set: ND Dam Hazard 2.16.0, ND Dam Breach Screening 0.15.0)

The near-dam FV1 refine (`REFINE_NEAR`) is on by default in the run tools (off again from
3.12.0, see New in 3.12.0) (run one dam,
batch and the tools built on them, and Screening's hand-off) when a structure, road, railroad
or crossing is within 1 mile (`REFINE_DIST_MI`) of the dam point, at half the model cell
(`REFINE_CELL` = 0; 5 m on the 10 m default grid). A fine cell that is not smaller than the
model cell is skipped and the report says so. The command-line script and `RunSettings` keep
it off. The class-stop rule stays off by default (an engineer's choice per run: without
receptor layers it can stop the domain where up to its 2 ft threshold still leaves the edge,
with nothing beyond the edge checked). ACC remains the floodplain solver. ANUGA is unchanged
and still off unless the tier asks for it.

## New in 3.7.0 (ND dam suite release set: ND Dam Hazard 2.15.0, ND Dam Breach Screening 0.14.0)

A **proposed dam** drawn in Screening (tool 19) is modelled like an existing one. When
`dam.json` carries `proposed_embankment`, the embankment is inserted on whatever DEM the
run reads — the source file is not modified — so the reservoir is in the grid and the
top-of-dam check looks for the design crest rather than the highest hillside within 30 m.
An empty crest length is taken from `dam_length_ft`, which gives the PMF weir a length.
Reports carry "proposed — embankment inserted from the drawn line". Re-inserting on a clip
that already has the crest changes nothing. Nothing here has been run in a real QGIS.

| File in `<Dams folder>/<Dam ID>/` | Units |
|---|---|
| `Depth (Max).tif` | ft |
| `Velocity (Max).tif` | ft/s |
| `Arrival Time.tif` | hours after breach initiation, at the arrival depth (default 0.1 ft, the ND hazard wet depth; 1 ft before 3.26.3) |
| `WSE (Max).tif` | ft (DEM datum) |
| `Baseline Depth (Max).tif` etc. | flood scenario: the same flood without the breach |
| `Incremental Depth (Max).tif`, `Incremental WSE Rise (Max).tif` | flood scenario: with minus without (ND 6.4) |
| `Flood Polygons (Max Depth).geojson`, `Flood Extent.geojson`, `Arrival Zones.geojson`, `Breach Products.gpkg` | vectors (the GeoPackage needs GDAL's OGR, which QGIS has) |
| `EAP Inundation Map (preview).svg` | a preview only: depth classes, arrival lines, dam, flooded structures. The EAP map (ND section 15) is ND Dam Hazard's map book; see "New in 3.2.0" |

Everything runs offline on local files: a statewide DEM (or a VRT mosaic of LiDAR tiles),
optional roughness, structure and road layers, and a dam point layer.

**Suite data and servers / Get data for this dam (3.9.0).** Instead of choosing the DEM and the
layers in every tool, run *Get data for this dam* once (Plugins > ND Dam Suite > Suite data and
servers, or `ndbreach:getdamdata`): it fills `<Dams folder>/<Dam ID>/data/` from the suite data
folder's offline pack and terrain cache, else from the chosen servers, and records each file in
the dam's `dam.json`. Every tool that names the dam (Dams folder + Dam ID, or the dam folder)
then takes its empty DEM, structures, roads, railways, land cover and ponds from there. Run it
with an empty Dam ID for every dam of the Dams folder.

*ND hazard classification (Section 5.2)* gives a screening class (Low / Medium / High) from
what the flood reaches; *Hand the results to the hazard classification* still calls the ND
Dam Hazard plugin for you (see below). The official classification is ND DWR's.

## What is in it

Algorithms are under *Processing Toolbox > ND Dam Breach Runner* and the *ND Dam Breach
Runner* menu.

**Breach modelling**
- *Guided setup for one dam (wizard)*: five pages (dam, terrain, scenario, models with a live
  run-time estimate, review) that open the tool below with everything filled in.
- *Run breach model for one dam* and *...for a layer of dams (batch)*.
- *Nested fine grid near the dam*: a fine grid for the first stretch, the coarse grid
  downstream, results merged on the coarse grid.
- *Sensitivity of the breach model*: low/high breach width, formation time and roughness,
  and the other parameter methods, on one shared domain, with low/median/high depth
  envelope rasters and a "does the spread change the answer" flag.
- *Probabilistic (Monte Carlo) run*: Latin-hypercube draws of width, time, Manning's n and
  method; percentile and probability-of-flooding rasters; resumable; optionally each run's
  rasters for ND Dam Hazard's class probability.
- *Cascade failure of dams in a chain*.
- *Estimate the run time*, *Breach hydrograph only*.
- *Rainfall to PMF inflow hydrograph*: your storm depth and distribution, SCS curve number or
  constant losses, SCS/Clark unit hydrograph, base flow; writes the inflow CSV.

**Data preparation**: *Build a statewide LiDAR mosaic (VRT)*, *Fill no-data gaps in a
DEM*, *Manning's n from NLCD*, *Preview the DEM conditioning*.

**Results and submittal**: *Life safety and damage estimate* (with, without and incremental),
*ND hazard classification (Section 5.2, ACER TM 11 charts)*,
*Animation of the flood* (GIF, MP4, time-enabled layers), *Submittal report (Word)*,
*Flood polygons and EAP map from rasters*, *Hand the results to the hazard classification*.

**Exchange with HEC-RAS**: *Compare with a reference model*, *Export to HEC-RAS*
(hydrograph as a `.u##` block plus the breach table), *Import HEC-RAS results* (2D plan HDF
to rasters; give it the model's terrain, see "New in 3.26.1").

**Trust**: *Verify a run manifest*, *Verification against analytical solutions* (Ritter, Stoker,
Manning normal depth, mass balance, radial symmetry), *Validation record (Word)*, *Check the
model installation*, *Self-test in this QGIS*.

**Without QGIS**: `scripts/ndbreach_cli.py` (`run`, `batch`, `hydrograph`, `estimate`, `check`,
`selftest`, `template`, `hecras-reexport`) runs the same workflow from a JSON configuration.
`scripts/install_macos.sh` checks for and installs the pieces on a Mac.

## How a run works

1. **Breach hydrograph (ND 5.1.1).**
   - Starting water surface: top of dam, or the PMF maximum reservoir level if lower.
   - Methods, all routed through the reservoir with the broad-crested trapezoidal weir
     equation and a power-law storage curve fitted to storage and surface area:
     Froehlich (2008) piping or overtopping (default: piping); Froehlich (2008) piping
     with a **pipe (orifice) phase** first; Froehlich (1995); MacDonald &
     Langridge-Monopolis (1984); Von Thun & Gillette (1990). *NRCS TR-60* gives a peak,
     drawn as a triangle or a curvilinear hydrograph of the same volume and peak. Or give
     your own CSV.
   - The report lists the Froehlich (1995) peak and the TR-60 peak as checks. The routed
     peak can be well above the regression peaks; review the breach width and time.
2. **Flood scenario (optional).** Give the PMF inflow hydrograph (CSV: time hr, discharge
   cfs), the spillway rating (elevation ft, discharge cfs) and the normal pool level. The
   inflow is routed through the reservoir, over the spillway and, if the pool overtops the
   crest, over the crest. The dam fails at the highest pool level, at a set time or at a
   pool elevation. Both floods are then modelled (with the breach; without it) and the
   incremental depth and WSE rise are written, with the acres and receptors where the
   breach adds 1, 2 and 3 ft. Arrival times count from breach initiation. If the trigger is
   never reached, the report says the dam does not fail.
3. **Where the water goes in.** The lowest DEM cell just below the dam (or your point).
4. **First domain.** A steepest-descent trace from there (default 5 mi), buffered 1 mi.
5. **LISFLOOD-FP**, inertial solver, 10 m cells by default. **How far downstream
   (ND 5.1.1(f)):** if the flood leaves an edge deeper than the tolerance (default 1 ft),
   that edge moves out (default 3 mi) and the model runs again. Since 3.26.5 the water that
   leaves counts too: once 5% of the breach volume has left the model (`OUTFLOW_EXTEND_PCT`,
   0 = the depth rule alone), every edge carrying at least 10% of that water and wet past the
   arrival depth moves out as well, however shallow, so a wide sheet under 1 ft no longer
   leaves unmodelled. **How long:** while the latest
   arrival time comes after 90% of the simulated time, the flood is still advancing and the next
   run is 1.5 times longer (an automatic duration, or a chosen one up to `DURATION_MAX_HR`); a run
   that cannot lengthen ends with a *Results cut off* note. The arrival time is at the arrival
   depth (0.1 ft by default since 3.26.3, 1 ft before), so the thin front keeps runs going
   longer, and ends more of them with a cut-off note, than 1 ft did. With structure, road or rail
   layers, an edge is left alone when none of them lies within 5 mi of the flood's path
   beyond it, and the report says so for you to confirm. Optionally (3.2.0) an edge is also
   left alone where the class stops changing (see "New in 3.2.0"). Results are read every
   run; each finished run is checkpointed.
6. **ANUGA (optional)**: a mesh over the LISFLOOD-FP extent, refined near the dam, along
   embankment lines you give it and around culverts. ANUGA's results become the final
   rasters; the report compares the two models and a difference raster is written.
7. **Outputs**: rasters, vectors, the EAP map, `_breach_model/` with the hydrograph CSV and
   plot, the run report (`.md`, `.json`, `.html`), gauge hydrographs, a QA layer with the
   dam, inflow point and flow path, and both models' rasters.

## Things in the flow path

All optional layers, read only within about 45 mi of the dam:

- **Structures, roads, rail** (points/lines, e.g. NSI and a road layer): decide how far
  downstream to model, and the report and `receptors_flooded.csv` list what is flooded, with
  depth, velocity and arrival, and how many have depth x velocity above 7 ft²/s.
- **Culverts, bridges, notches** (lines, upstream end first; fields for kind, span, rise,
  barrels, shape, inverts are matched by name or chosen). Bare-earth LiDAR leaves road
  embankments as walls. LISFLOOD-FP's solver has no culvert or weir links, so every
  structure is an **open cut** through the embankment (no roof, no inlet control: a
  culvert passes more than the real one). ANUGA uses its Boyd culvert operators (inlet and
  outlet control). The report says which each structure got. Inverts given in the layer
  (an inventory's inlet / outlet elevations) are checked against the LiDAR bed first (3.6.0,
  `S_INVERT_TOL_FT`); without them the DEM at each end is the invert. ND Dam Hazard's
  "Culvert and bridge lines for ND Dam Breach Runner" writes this layer from the NBI,
  OpenStreetMap and NDDOT's bridge inventory and centerline pipes.
- **Channels to burn** (lines): cut into the DEM, with the bed forced to fall downstream so
  a LiDAR pit does not dam the channel.
- **Ponds and reservoirs** (polygons): lowered to their bed (mean depth below the LiDAR
  water surface) and started full, so a downstream pond does not absorb the flood
  and the depth reported is the water the flood adds. This is also the tailwater for cascades.
- **Gauges** (points): discharge hydrograph past each point (CSV and plot).
- **Mesh lines** for ANUGA: levees, road embankments, channel banks.

The source DEM is never changed; edits are applied to whatever grid the models read.

## New in 3.6.0 (ND dam suite release set: ND Dam Hazard 2.14.0, ND Dam Breach Screening 0.13.1)

Culvert inventories carry inlet / outlet elevations whose datum is not the LiDAR's (NDDOT's centerline
pipes, written to the structures layer by ND Dam Hazard 2.14's culvert tool as `invert_up` /
`invert_dn`). The models only ever LOWER ground to an invert, so a wrong elevation trenches the stream
(too low) or leaves the embankment uncut (too high). Before any model runs, each structure's given
inverts are now compared with the LiDAR bed - the lowest ground within the structure's corridor width of
each end of its line:

- both ends within **`Structures: inventory inlet / outlet elevations are used when within this of the
  LiDAR bed`** (`S_INVERT_TOL_FT`, starting value 3 ft - YOUR value) -> used as given, slope and any
  perch kept;
- both ends off by about the same amount (within 1 ft of each other) -> taken as a datum shift and moved
  by the mean offset (slope kept);
- otherwise -> the LiDAR bed at both ends, the inventory values kept for the report.

A culvert whose rise does not fit under the road crown in the LiDAR is noted (size or elevations wrong).
The run log and the report (`structure_inverts`, and each structure's line) say what happened to every
structure. Layers without inverts behave exactly as before (DEM inverts). Nothing here was run in a real
QGIS; the check is tested on synthetic terrain.

## New in 3.5.0 (ND dam suite release set: ND Dam Hazard 2.13.0, ND Dam Breach Screening 0.13.0)

Nothing here was run in a real QGIS (the simulated QGIS in three flavours); the model results are
unchanged.

- **Settings tab in the popup**: the suite settings file (`damsuite_settings.xlsx` / `.csv`, shared with
  ND Dam Hazard and ND Dam Breach Screening) as a grid - a tab per popup tab, the Runner's tool
  parameters with `Value / Unit / Starting value`, Find, `show only YOUR values`, `show only changed`,
  Reset / Load / Save. The Processing tools open with the file's values (every one can still be changed
  in the dialog); edits are saved to the file at once. No extra Python package: the workbook is written
  and read by the suite's own small .xlsx code (CSV works everywhere).
- `Plugins > ND Dam Suite`: one menu for the three plugins (each window, Dam suite home, Suite settings),
  next to this plugin's own menu.
- Units: the Runner was already in feet / acres / acre-feet / cfs; the grid cell size and the ANUGA mesh
  sizes stay in metres (grid properties) with the feet in their labels, as before.

## New in 3.4.4

From the real-QGIS run on macOS (QGIS 3.28.6 LTR, Python 3.9, GDAL 3.3): that QGIS's Python has
no `contourpy`, which the ANUGA tier used to trace the flood-domain outline for its mesh. A
built-in tracer (numpy + scipy) is used when contourpy is missing; it gives a simple polygon
within about one cell of contourpy's outline. The self-test check is now "Flood-domain outline
(contourpy or built-in)" and passes with either. Nothing else changed.

## New in 3.4.3

From the second real-QGIS run (QGIS 3.44.14 LTR, GDAL 3.13, Windows 11): the self-test's
"GDAL: warp, resample, re-project on write" check failed because its test CRS was not shifted
(`+proj=utm` ignores `+x_0` in PROJ); it is now a transverse Mercator definition. A fix to the
self-test only - the Runner's own warping was not at fault.

## New in 3.4.2 (ND dam suite release set: ND Dam Hazard 2.12.2, ND Dam Breach Screening 0.11.2)

From the first real-QGIS run (smoke kit, QGIS 3.44.14 LTR on Windows 11): the shared styles
declared their geometry type as a word, which QGIS reads as "point", so the flood-extent
(polygon) and flow-arrow (line) styles were refused; they are numeric now and all 15 styles
load. No other change in this plugin.

## New in 3.4.1 (ND dam suite release set: ND Dam Hazard 2.12.1, ND Dam Breach Screening 0.11.1)

Stability release; nothing here was run in a real QGIS or with a real LISFLOOD-FP / ANUGA.
Results for valid inputs are unchanged.

- No more process-wide `gdal.UseExceptions()`: it changed GDAL for QGIS and the other plugins
  (ND Dam Hazard expects the default). GDAL calls go through `damsuite.gdalsafe`, which raises
  the same RuntimeError, with GDAL's message and the file name, in both modes.
- The breach equations (Froehlich 2008 / 1995, Von Thun & Gillette, MacDonald &
  Langridge-Monopolis, TR-60, the curvilinear shape) stop with a plain message for a zero,
  negative, missing or non-finite input; they used to return NaN / complex numbers or divide
  by zero.
- `initProcessing` registers the provider once. QGIS / qgis_process and initGui both call it; a
  second call used to replace the registered provider object, so unloading the plugin left the
  first one registered (a crash risk on plugin reload).
- ANUGA result archives (.npz) and solver logs are closed at once (Windows kept them locked
  until garbage collection); hazard sampling treats a NaN coordinate and an empty footprint as
  "no sample" (the copy in ND Dam Breach Screening is the same file).

## New in 3.4.0 (ND dam suite release set: ND Dam Hazard 2.12.0, ND Dam Breach Screening 0.11.0)
None of this has been run in a real QGIS or against a real LISFLOOD-FP: the model runs in the
tests go through the stubbed LISFLOOD-FP call on the synthetic valley (tests/test_v340.py). The
model, the rasters, the manifest and the classification of a run with every new option OFF are
unchanged.

- **Cell-size convergence check** (sensitivity tool, `SENS_CELL`, on by default): the base case
  runs again at half the cell size on the same domain (or at the next coarser factor that fits
  the cell budget `SENS_MAX_CELLS`, and says so); the report gets the fit index F between the two
  depth rasters, depths and velocities at the receptors, and `class_stable_grid: yes/no` from the
  Runner's own ND 5.2 preview. Grid resolution is often the larger error; a class that changes
  with the grid is a dam to look at.
- **Near-dam refine** (`REFINE_NEAR`, off by default in 3.4.0-3.7.0, on in the run tools in 3.8.0-3.11.x, off again from 3.12.0, with `REFINE_DIST_MI` = 1 mi and
  `REFINE_CELL` = half the model cell, the engineer's screening values): LISFLOOD-FP's ACC (local
  inertial) velocities are not reliable in the fast, often supercritical flow just below a breach.
  When a structure, road or rail receptor lies within the flagged distance of the breach point and
  the final rasters come from the ACC run, the first stretch is run again on a fine grid with the
  FV1 (shock-capturing) solver and merged into the final rasters the way the nested tool does; the
  run log, the run report and the manifest say what was replaced and why. Also: the floodplain
  solver keyword (`acceleration` / `fv1`) is now written to the .par file and recorded in the
  settings.
- **Buildings as obstructions** (`BUILDINGS`, off by default): polygon footprints in the structures
  layer (points are ignored and say so) are raised by `BUILDING_RAISE_FT` (default 10 ft, YOUR
  value) or get Manning's n `BUILDING_N` (default 0.15, YOUR value) before the run; the
  conditioning report and the manifest record it; the conditioning preview tool shows it and writes
  the Manning grid. Neither treatment is right everywhere - bare terrain stays the default.
- **Feet audit:** every depth-like parameter says feet and that its starting value is the
  engineer's; cell sizes stay metres with the feet equivalent in the label (3 m = 9.8 ft ...).
- **Dam suite home** (Plugins > ND Dam Breach Runner > Dam suite home...): one window, the same in
  all three plugins, listing every dam of a Dams folder with its dam.json state and next action and
  opening the plugin that does it (damsuite.suite_home).
- Suite: damsuite 1.2.0 (the Screening tools 7 / 8 write `_screening_model/run_manifest.json` in
  this plugin's manifest format and the chain check finds it; "refresh" next action from an
  inventory change recorded by Screening tool 13).

## New in 3.3.1: the REG_05.2026 curve table
- The chart file shipped with the plugin (`core/acer_tm11_charts.json`) now holds the zone polygons
  of ND REG_05.2026 Figures 5.1 (houses on foundations) and 5.2 (mobile homes) from the table the
  engineer digitized from those figures (stated accuracy +/-0.06 ft; `damsuite/tm11_curves.py`,
  the same table ND Dam Hazard 2.11.0 uses). A screenshot trace of the two figures agrees with the
  table within 0.05 ft (house high line), 0.09 ft (house low line) and 0.02 ft (mobile homes) for
  95 % of its points.
- Since suite 1.14.1 the file ships signed off by Nicholas Bendickson (2026-10-12): one
  engineer's sign-off is enough (`signed_off_by`, status "digitized"; a second engineer in
  `checked_by` is optional). ND Dam Hazard > Chart sign-off writes a file for another engineer
  and installs it in both plugins. A chart file without a signing engineer still refuses the chart
  method and says what is missing.
- Suite version check: ND Dam Hazard below 2.11.0 is reported (it uses older quartic curves, up to
  0.18 ft from the table).
- Not run in a real QGIS.

## New in 3.3.0: a popup window (Plugins > ND Dam Breach Runner > Open the Runner window...)
The plugin had a wizard and about thirty separate menu entries. It now also has one window, like the
ND Dam Hazard window, for working on one dam:
- **Dams folder and dam ID** at the top (the folder is `<Dams folder>/<Dam ID>`, the Runner's own
  layout; a list offers the dams that already have a folder) and the **DEM**: a raster layer of the
  project or a file. Which of LISFLOOD-FP and ANUGA were found is shown under them.
- **Dam tab:** what is in the dam's folder (final rasters, baseline and incremental rasters, run
  manifest and report, products, probabilistic / sensitivity / nested runs, the Runner's own ND 5.2
  preview, the hand-off to the classification, animation, HEC-RAS files), the dam.json line and its
  next action, and a suggestion of the tool the dam probably needs next. Buttons: the guided wizard,
  Run breach model, Run for a layer of dams, Estimate the run time.
- **Model, Data, Results and Checks tabs:** every other tool of the plugin (nested, sensitivity,
  probabilistic, cascade, hydrograph only, rainfall to PMF; LiDAR mosaic, gap fill, NLCD Manning n,
  conditioning preview; products, consequences, the ND 5.2 preview, the hand-off, animation, the Word
  submittal report, benchmark, HEC-RAS export / import, validation record; manifest check,
  verification, installation check, self-test) plus buttons to ND Dam Hazard and ND Dam Breach
  Screening, the model paths (Processing options) and this README.
- Each button opens the tool's own Processing dialog with the dam's folder (DAM_FOLDER / DAMS_FOLDER /
  DAM_ID, the DEM) filled in, so every value can be seen and changed before the run; the window runs
  nothing itself. The wizard, the menu entries, the Processing tools, qgis_process and the
  command-line script are unchanged. The choices are saved with the QGIS project.
- None of this has been run in a real QGIS: tests/test_popup.py drives the window through test doubles
  of Qt (tests/qt_fakes). How it looks and lays out, and `processing.execAlgorithmDialog` with filled-in
  values, have not been seen in QGIS.
- Version 3.3.0 changes nothing in the model, the rasters, the manifest or the classification.

## New in 3.2.0

Part of the ND dam suite release set (ND Dam Hazard 2.10.0, ND Dam Breach Runner 3.2.0, ND
Dam Breach Screening 0.9.0). **None of this has been run in a real QGIS, and no model test ran
here: LISFLOOD-FP and ANUGA are not installed where it was written.** The new code is covered
by unit tests on synthetic grids and by runs of the workflow with the LISFLOOD-FP call stubbed
(`tests/test_v320.py`). Try each piece on one dam before relying on it.

- **dam.json, the shared dam record.** *Run breach model for one dam*, the batch tool and the
  wizard read `<Dams folder>/<Dam ID>/dam.json` (written by any of the three plugins). A dam
  height, storage, surface area, top of dam elevation, dam point, name or NID ID left empty is
  taken from it, and the log says so: "Dam height (ft) from dam.json (set by ndhazard 2.10.0
  on 2026-09-30T14:05:00): 25". A top of dam recorded on a local datum is not used as an
  elevation. After the run the values it used are written back (tool `ndbreach`, version
  3.2.0) with a `modelled` status (final rasters, run manifest, flood extent). **Another
  tool's value is never overwritten**: when the run used a different value, dam.json keeps
  the other tool's, and the run report lists both under "Check these" and in a "dam.json"
  section. The top of dam datum is recorded as `DEM` when the run took the top from the DEM
  (or from the tool's parameter, which is in the DEM's datum). The ND 5.2 classification tool
  adds a `classified` status marked `{"preview": true, "tool": "nd_hazard.py"}` to an existing
  dam.json: a preview, never the classification (that is ND Dam Hazard's). The height,
  storage and dam point parameters are now optional for this reason; a run without them and
  without a dam.json that has them stops with a message. Sensitivity cases and Monte Carlo
  runs do not write dam.json.
- **Per-run rasters for the class probability** (*Probabilistic (Monte Carlo) run*, option
  *Write each run's rasters for the class probability*, off by default). Every finished run
  also gets `Probabilistic/Runs/run_NNN/` with `Depth (Max).tif` (ft), `Velocity (Max).tif`
  (ft/s) and `Arrival Time.tif` (hours after breach initiation), written from the run's
  arrays in the dam's grid and CRS like the percentile rasters, and `run.json` (the sample,
  its weight - equal, 1 / the number of finished runs -, the run key, units, and a reference
  to the base run's `Baseline ...` rasters in the dam folder when the scenario has a baseline;
  they are not copied). `Probabilistic/Runs/index.json` lists the runs. **ND Dam Hazard's
  class probability tool classifies these folders.** The log estimates the disk space (three
  float32 rasters per run before compression). An interrupted study continues: rasters that
  exist for the same run are not written again; a run that failed or is no longer in the study
  loses its run.json and rasters. The per-run model folders moved from `runs` to `Runs` (the
  same folder on Windows and macOS).
- **Stop extending the domain when the class stops changing** (advanced, *Class-stop rule*,
  off by default). An edge the flood leaves deeper than the edge tolerance is accepted when
  (a) along the flow path near it the breach flood's depth - or, with a no-breach baseline
  (flood scenario, ambient flow), its rise over the baseline - stays below the threshold:
  ND Dam Hazard's termination point (a station below the threshold confirmed by the next
  stations) lies inside the domain, every station from it to the edge is below, and so is
  the deepest water leaving through that edge; and (b) no structure, road or rail from the
  layers given lies within the receptor swath beyond the edge. The starting values are ND Dam
  Hazard's termination-point defaults: 2 ft (depth or rise), 3 confirming stations, a station
  every 0.5 mi, the deepest wet cell within 150 ft - screening values the engineer chooses,
  not requirements of REG_05.2026, FEMA P-333 or TM 11. Only the edge the flow path from the
  breach leaves through is judged. With a baseline the no-breach flood is run on the current
  domain to get the rise (reused as the final baseline when the domain stops there). Each
  decision is in the run report ("edge accepted: rise over the baseline below 2 ft for 5
  stations from station 3.5 mi ...", with the station table) for the engineer to confirm.
  With receptor layers, the receptor stop rule already stops an edge with nothing ahead, so
  (b) keeps every edge it would not have stopped; without receptor layers nothing beyond the
  edge is checked and the report says so.
- **One EAP map.** ND Dam Hazard's map book (`outputs/EAP/EAP_Map_Book.pdf`, on aerial
  imagery) is the EAP map. The run tools have an *EAP map* setting: *Automatic* (default:
  this tool's SVG preview only when ND Dam Hazard is not installed), *Leave to ND Dam
  Hazard's map book (recommended)* or *Also draw this tool's SVG preview*. The preview is now
  called `EAP Inundation Map (preview).svg`. The Word submittal report reads either name
  (older folders keep `EAP Inundation Map.svg`) and, when the map book PDF is in the dam
  folder's `outputs/EAP`, lists it as the EAP map instead of the preview.
- **Version checks.** At start-up (after QGIS has loaded its plugins) the plugin checks the
  versions of ND Dam Hazard and ND Dam Breach Screening against what it needs and shows
  problems in the message bar and the log (*ND Dam Breach Runner*). Before handing results to
  ND Dam Hazard it stops when that plugin is older than 2.9.1 (the version that refuses a
  breach raster as its own baseline); 2.10.0 is recommended. Without the other plugins
  installed nothing is reported and everything else works as before.

## New in 3.1.1

- **Hand-off to the ND hazard plugin passes the dam folder only** (see "Hand-off to the ND
  hazard plugin" below). Before, the breach depth could be handed over as the no-breach
  baseline, and a High dam classified Low. Use ND Dam Hazard 2.9.1 or later.

## New in 3.0

- **Ambient flow and tailwater.** *Ambient stream flow* is added at the breach inflow through
  the whole event and gives a sunny-day baseline (ambient only) for incremental results, so
  the increment is the breach on top of the flow the stream already carries. The hydrograph
  starts after a spin-up so the channel is at steady flow when the event begins. *Tailwater*
  polygons are river or lake reaches standing at a stage; every cell inside a polygon that is
  lower than the stage starts wet, so draw the polygons around the water only (low ground
  behind a levee inside the polygon would be filled too). Depths include the ambient water; it is not subtracted.
- **Failure geometry.** *Partial breach* raises the breach bottom (a share of the dam height);
  *crest line and position* starts the breach anywhere along the crest, not only at the dam point.
- **Outlet works and gates.** *Outlet works* (pipes or openings with invert, size, coefficient
  and a gate schedule) and a *spillway gate* schedule change the routed pool. Weir flow below
  the crown, orifice flow above, no conduit friction or tailwater: replace with a rating curve
  when the dam has one. LISFLOOD-FP has no weir links, so structures in the flood path stay
  open cuts.
- **Run manifest.** Every run writes `_breach_model/run_manifest.json`: software versions, the
  LISFLOOD-FP executable and every input with SHA-256, the settings, and the SHA-256 of the
  result rasters. *Verify a run manifest* lists what changed since. It shows the files are the
  ones the run used; it is not a signature.
- **Nested grid.** One-way: the fine run's discharge through a section across the flow starts
  the coarse run. The coarse model does not affect the fine one. LISFLOOD-FP only.
- **Probabilistic.** The default distributions are adjustable placeholders, not published
  values. Set them to what you can defend.
- **Rainfall-to-PMF.** The storm depth is yours (no PMP is generated). The SCS Type II table
  is transcribed and must be checked against the source before use.
- **Consequences.** The built-in depth-damage curves and fatality rates are generic
  placeholders; give your own curves as CSV. No criterion of ND section 6.4 is checked.
- **Animation.** Shows the LISFLOOD-FP snapshots (saved at intervals), not every time step.
- **Submittal report.** A Word file from what the dam folder holds, with a reviewer checklist
  pre-filled from it. It is a draft for the engineer, not a certification.

## Installing

### 1. The plugin

Zip the `nd_breach_runner` folder (or use the zip you were given) and install it with
*Plugins > Manage and Install Plugins > Install from ZIP*.

### 2. LISFLOOD-FP (required)

The plugin needs a LISFLOOD-FP 8.x build with a small patch that adds the
`inittm_depth` keyword (arrival time at a depth threshold) and removes the Linux-only NUMA
requirement so it builds on macOS.

1. Download the LISFLOOD-FP source from the official release (University of Bristol;
   v8.2 is on Zenodo).
2. Install the Xcode command line tools (`xcode-select --install`) and Homebrew.
3. Run:

   ```bash
   cd nd_breach_runner/scripts
   ./build_lisflood_macos.sh ~/Downloads/LISFLOOD-FP-8.2 ~/lisflood-fp
   ```

   This installs `cmake` and `libomp` with Homebrew, applies the patch with
   `patch_lisflood.py` (safe to run twice), builds without NetCDF and copies the
   executable to `~/lisflood-fp/lisflood`.

On Linux, run `python3 scripts/patch_lisflood.py <source>` and build with CMake as the
LISFLOOD-FP instructions describe; on Windows, apply the same patch and build with Visual
Studio.

### 3. ANUGA (only for the ANUGA tier)

ANUGA runs in its own Python environment, not inside QGIS. The easy way is *Set up ANUGA
(one-time download)* (see the top of this file). By hand with conda:

```bash
./scripts/create_anuga_env.sh anuga
```

### 4. Find them

The plugin looks for both in the usual places (`~/lisflood-fp*/build/lisflood`,
`~/miniforge3/envs/anuga/bin/python`, and so on) and fills the provider settings on first
load. *Plugins > ND Dam Breach Runner > Check the model installation* reports what it found,
runs LISFLOOD-FP on a tiny channel, confirms the patch is active and imports ANUGA. Set the
paths by hand in *Processing > Options > Providers > ND Dam Breach Runner* if it finds
nothing.

## Using it

**One dam:** *Run breach model for one dam*. Give the Dam ID, click the dam crest, enter the
height, storage and (ideally) the top of dam elevation and surface area, pick the DEM and a
*Dams folder*. Pick *Flood* as the scenario for the PMF case.

**Many dams:** *Run breach models for a layer of dams (batch)*. Leave the field choices empty
and the fields are matched by name (NIDID, DAM_HEIGHT, MAX_STORAGE, SURFACE_AREA, ...); the
log lists the matches, so check them. For the flood scenario put each dam's inputs in
`<Dams folder>/<Dam ID>/inputs/`:

| File | What |
|---|---|
| `pmf_inflow.csv` | PMF inflow hydrograph (time hr, discharge cfs) |
| `spillway_rating.csv` | spillway rating (elevation ft, discharge cfs), optional |
| `baseline_outflow.csv` | your no-breach outflow (own-hydrograph method only) |
| `breach_hydrograph.csv` | your breach hydrograph (own-hydrograph method only) |

*Per dam* scenario runs the flood case where `pmf_inflow.csv` exists and the sunny-day case
elsewhere. Finished dams are skipped on a re-run; tick *Resume* to continue a dam that was
interrupted (LISFLOOD-FP runs already finished for identical settings are reused). Cancel
stops the model within a second or so.

**Before a long run:** *Estimate the run time* times a small test flood on this computer.
**Uncertainty:** *Sensitivity of the breach model*. **Chains of dams:** *Cascade failure*.

### Statewide LiDAR

*Build a statewide LiDAR mosaic* makes a VRT of a folder of tiles (no copying). Tiles in another
CRS are left out and listed. *Fill no-data gaps* fixes holes in the flood path (give an extent;
it reads the area into memory). Roughness: *Manning's n from NLCD*, or give the NLCD raster
straight to the run tools. The values are typical floodplain values from the usual references
(cultivated crops 0.04, grassland 0.035, developed medium 0.10, forest 0.10, and so on); they
are starting points, so calibrate where you can, and override any class with JSON such as
`{"82": 0.05}`.

### Cascade failure

Dams are ordered upstream first. The upstream dam's run measures the flood's discharge just
upstream of the next dam. If the wave overtops the next dam (or you choose "always"), that dam
is modelled failing: with its pool data (`F_POOL` normal pool field, and `pmf_inflow.csv` /
`spillway_rating.csv` in its `inputs` folder) the wave is added to its own inflow and routed
through its reservoir; without them it breaches as a sunny-day case when the wave arrives.
The combined rasters take the deepest and fastest water and the earliest arrival over all
the runs (conservative: downstream of a failed dam its own run already carries the upstream water).

### Sensitivity

Runs the base case and low/high variations (breach width x0.7 and x1.4, formation time x0.5
and x2, Manning's n x0.75 and x1.3; change them in the parameters) and, optionally, the other
parameter methods, all LISFLOOD-FP on the base run's domain. Writes
`Sensitivity/Sensitivity Depth (Low|Median|High).tif`, the fastest water, the earliest arrival,
a table per case and a report. With receptors it says when some cases flood a structure and
others none; with the classification plugin it says when the hazard class changes.

### Benchmark against HEC-RAS

Give the dam folder and the reference model's depth (and optionally velocity, arrival, WSE)
rasters. You get fit index F (above 0.8 is a close match), hit rate, false-alarm ratio, depth
bias/RMSE/90th percentile, depth-class agreement, an agreement raster and a difference raster.

### Hazard classification (ND 5.2, ACER TM 11)

*ND hazard classification (Section 5.2)* (`ndbreach:ndhazardclass`) reads `Depth (Max).tif` and
`Velocity (Max).tif` from the dam folder and your structure, road, railroad, lifeline and
downstream-dam layers, and applies REG_05.2026 Sections 5.2 and 5.3 as `core/nd_hazard.py`
does (the same module, byte for byte, as the ND dam screening plugin):

- **Habitable structures** (5.2.1.1: dwellings, trailers and mobile homes, regularly occupied
  commercial / industrial / agricultural buildings, overnight recreation). Every structure is
  habitable unless its occupancy names an uninhabited building (barn, shed, garage, silo,
  grain bin, well pad ...; the list is editable), so an unfamiliar or blank class counts as
  habitable (5.3). A velocity that is missing at a structure is counted potentially lethal and
  listed as a data gap. Potentially lethal
  -> High: with a 2D model by the ACER TM 11 depth-velocity charts (ND Fig. 5.1 houses, Fig. 5.2
  mobile homes), with a 1D model when deeper than 2 ft. A habitable structure in the chart's
  **judgment zone is Medium by rule** (Nick, 10/30/2026; the advanced option JUDGMENT_HIGH keeps
  the old rule, judgment zone -> High under 5.3). A point within the digitizing tolerance of a
  higher zone or beyond the chart's axes takes the higher class (5.3). Inundated at any depth -> at least
  Medium (economic loss, 5.2.2; the policy leaves low vs medium to the Engineer, the tool says
  Medium and notes it). Uninhabited buildings only -> Low.
- **Roads** (5.2.1.2), overtopped at any depth: AADT > 2,000 or interstate -> High, 500-2,000 ->
  Medium, private or < 500 -> Low, sole access to an occupied location -> Medium.
  **Railroads** (5.2.1.3): Medium; 10 or more trains a day or an active passenger line -> High.
  **Lifeline facilities** (5.2.2) inundated -> Medium (their access roads are not traced).
  **Downstream dams** (5.3 cascading): a dam whose point the flood reaches passes on its class
  (NID H / S / L codes are read); the dam itself is left out when it is in the layer.
- **"Any depth"** is the wet depth, 0.1 ft by default and at least 0.01 ft (a dry cell reads 0).
- **Private roads** are only those an explicit field marks private; ownership or access codes
  are not read as private. Interstates are recognised from "Interstate", "I-94", "I 94", "I94"
  or TIGER route type "I". FRA passenger codes default to A, B, C (T tourist is not counted).
- **Data gaps**: a road without AADT or a railroad without a train count is classified by the
  assumption you choose (Medium by default) and listed as a data gap; when every governing
  finding is borderline or assumed, the log says the higher class was assigned.
- No allowance is made for evacuation or warning (5.3).

Sampling: a point takes its raster cell; a polygon the deepest cell whose centre is inside it
(the centroid's cell for a footprint smaller than a cell), with the **maximum velocity over
the footprint's wet cells** (conservative; the velocity at the deepest cell is in the GeoJSON);
a line is sampled every half cell and takes its deepest sample. Depth and velocity maxima may
not occur at the same time, so the chart point is an upper bound. Fields are matched by name
when not chosen (NSI `OCCTYPE`, USA Structures `OCC_CLS` / `PRIM_OCC`, `AADT`, TIGER `RTTYP`,
FRA `PASSNGR` with codes A, B, C by default, NID `NIDID` / `HAZARD`); the log lists the fields
used.

**The charts are signed off.** `core/acer_tm11_charts.json` ships signed off by Nicholas
Bendickson (2026-10-12), so the chart method runs by default. A chart file of your own must
record the engineer who signed it off (one engineer is enough; ND Dam Hazard > Chart sign-off
writes one and installs it here and in the Screening); without that the tool stops with a
message and offers the conservative method (every inundated habitable structure counted as
potentially lethal, which over-classifies). The 1D 2 ft rule is offered only for results
from a 1D model; this plugin's results are 2D, and the tool warns if it is chosen. Re-running
a dam removes its old classification files, so the report never shows a stale class. The tool writes
`Hazard Classification (ND 5.2).md` / `.json`, `Hazard Classification Findings.csv` /
`.geojson` and, with the chart method, `ND Fig 5.1 Houses.svg` and `ND Fig 5.2 Mobile Homes.svg`
into the dam folder; the Word submittal report includes the class, the governing findings,
the data gaps and the two charts. **This is a screening classification for the engineer; the
official classification is made by the ND Department of Water Resources.**

### Hand-off to the ND hazard plugin

The older *Hand the results to the hazard classification* tool (`ndbreach:classify`) is still
there for the separate `ndhazard` plugin: it calls `ndhazard:classify` (one dam) or
`ndhazard:batch` (a dams folder).

**Since 3.1.1 it passes the dam folder only** when the classifier takes one (the ND Dam
Hazard plugin's `DAM_FOLDER`): the optional raster parameters are left empty and that plugin
finds `Depth (Max).tif`, `Velocity (Max).tif`, `Arrival Time.tif` and the `Baseline ...`
rasters itself (its README, "HANDING RESULTS TO THIS PLUGIN"). Up to 3.1.0 the rasters were
filled by matching words, and "No-breach baseline: max depth" and "max depth x velocity" both
got the breach depth: the breach became its own baseline and the classifier charged nothing
to the dam (a High dam came out Low in a test). Now a baseline parameter only ever gets a
`Baseline ...` raster, and a depth x velocity, incremental or difference parameter is never
filled by keyword. The ND class (`ND_CLASS`) is read before the TM 11 class. Use ND Dam
Hazard 2.9.1 or later: it also refuses a baseline that is the breach raster.

For a classifier without a folder parameter the parameters are read at run time and filled by
name from the dam folder; if any cannot be filled the error names them, and you give them in
the mapping JSON, for example
`{"DEPTH_RASTER": "{depth}", "OUTPUT": "{out}/result.csv"}` (placeholders `{depth}`
`{velocity}` `{arrival}` `{wse}` `{folder}` `{parent}` `{dam_id}` `{out}`). For a batch the
breach summary and the classification CSV are merged into `Batch_Breach_Runs_Classified.csv`.
The same hook classifies each sensitivity case.

### Inputs worth getting right

- **Top of dam elevation**, in the DEM's vertical datum. Without it the plugin takes the
  highest DEM point within 30 m of the dam point and says so in the report.
- **Surface area at the top of dam.** Without it the storage curve assumes volume grows
  with depth squared.
- **DEM elevation units.** Detected from the raster when recorded; otherwise it assumes the
  CRS's linear unit and warns. Set it explicitly for State Plane (US survey feet) LiDAR.
- **Cell size.** 10 m is a sensible default. 20-30 m is much faster for screening.

## Limits to keep in mind

- **Nested grids** are one-way and the fine and coarse results differ at the seam (the coarse
  model gets the fine run's discharge spread over the flow-carrying width of a straight section),
  so expect a step in depth and velocity there; check it on the merged rasters. The interface
  hydrograph is cut off if the fine run ends before the flow has receded; the run says so.
  Only the plain rundam report, manifest and rasters are written for the merged result.
- **Ambient flow** shifts the breach in the model timeline by the spin-up; the CSV and report
  give the breach hydrograph unshifted, and `model_inflow_hydrograph.csv` is what the model saw.

- **Culverts in LISFLOOD-FP are open cuts** (see above); ANUGA models them properly.
- **The pipe phase and all breach parameters are documented assumptions**, not measured
  behaviour; the pipe phase has flow jump at roof collapse. Override the width, formation time
  or side slope when you have better values, and use the sensitivity tool to see how much it
  matters.
- **The flood scenario routes the pool with level-pool routing and a weir crest**, with a
  spillway rating you supply. Outlet works and gates use textbook weir/orifice equations
  (see above); other pool operations are not modelled unless they are in the rating.
- **Incremental results** are with-breach minus without-breach depth (and WSE) on the same
  domain. The file names ("Baseline ...", "Incremental ...") are this tool's own; check what
  the classification plugin expects.
- **The receptor stop rule** looks along the flood's steepest-descent path for receptors
  within a swath (at least 400 m, more for a wide flood). A flood spreading across a plain can
  reach farther than that swath; the report lists each decision so it can be reviewed.
- **Cascades** combine runs conservatively (see above), and the wave gauge only exists if
  the flood reaches the next dam inside the domain.
- **Classification hand-off from a long run.** "Hand the results to the ND hazard
  classification" runs on QGIS's main thread (it calls another plugin). The batch and
  sensitivity tools call that plugin from their background task when you ask for
  classification; if QGIS misbehaves there, run the classification tool afterwards instead.
- **Untested against the real thing:** the GDAL-side tools (VRT, gap fill, NLCD roughness,
  benchmark reading), the call into the `ndhazard` plugin, the wizard's Qt pages, the
  macOS installer script and the HEC-RAS HDF import (its dataset layout is assumed from the
  HEC-RAS 6 format) were written from the APIs and documentation and not run against the real
  software here. Run *Self-test in this QGIS* first, then try each on a small area.
- **The breach inflow point** is found from the DEM; give it explicitly if the dam is dry or
  the pool is low. The run warns when the flow path does not lead away from the dam.
- **DEM gaps.** LISFLOOD-FP treats no-data as very high ground; the run warns when the flood
  reaches them. Fill them first.
- **Units check.** When the top of dam is given, the run stops if the DEM crest differs from it
  by more than 60% of the dam height (at least 8 ft).
- **The DEM is the channel.** Burn channels and lower ponds for the reaches where LiDAR is
  the water surface.
- **The run-time estimate** is good to about a factor of two.
- **LISFLOOD-FP near the dam.** The inertial solver is not meant for supercritical flow;
  velocities within a few cells of the breach are the least reliable part. That is where
  ANUGA matters most.
- **Review by ND DWR.** Confirm that LISFLOOD-FP / ANUGA results are accepted for your
  submittal before relying on them for classification or EAP maps. This tool's EAP map is a
  preview; the EAP map is ND Dam Hazard's map book on aerial imagery.

## Tests

Without QGIS:

```bash
python3 -m nd_breach_runner.tests.run_tests
NDBREACH_LISFLOOD=~/lisflood-fp/lisflood NDBREACH_ANUGA_PYTHON=~/miniforge3/envs/anuga/bin/python \
  python3 -m nd_breach_runner.tests.run_tests --models
```

The model tests run both models on synthetic valleys (reservoir, prairie valley, road
embankment with a culvert, a second dam) and check mass balance, domain extension, the
flood scenario with incremental results, structures, ponds, gauges, the receptor stop rule,
sensitivity, cascade, resume and cancel, LISFLOOD-FP / ANUGA agreement, ambient flow, tailwater,
outlets and gates, partial breach, nested grids, Monte Carlo, animation and the run manifest. The QGIS
algorithms are exercised against a stand-in for `qgis.core`. Not testable without QGIS
and GDAL (the *Self-test* tool covers these inside your QGIS): the GDAL-side tools (VRT, gap filling, NLCD, benchmark and product reading, GeoTIFF
writing) and the real ND hazard plugin's interface; try those once on real data.

## References

- North Dakota Dam Safety Standards Policy REG_05.2026, ND Department of Water Resources.
- Froehlich, D.C. (2008). Embankment dam breach parameters and their uncertainties.
  *J. Hydraulic Engineering* 134(12).
- Froehlich, D.C. (1995). Peak outflow from breached embankment dam. *J. Water Resources
  Planning and Management* 121(1).
- USDA NRCS Technical Release 60, *Earth Dams and Reservoirs*.
- Bates, P.D., Horritt, M.S., Fewtrell, T.J. (2010). A simple inertial formulation of the
  shallow water equations for efficient two-dimensional flood inundation modelling.
  *J. Hydrology* 387.
- Roberts, S. et al. ANUGA: open-source hydrodynamic modelling, Geoscience Australia /
  Australian National University.

## Ported from DamSafety (DWR) 2026-10-28

- **Outflow_boundary** (anuga_worker.py): outer mesh edges no longer gain water.
  Transmissive for outflow, mirrored normal momentum for inflow. Matches the
  DamSafety Runner 3.10.55 fix.
- **channel_bed / thalweg_profile / channel_slope** (structures.py): slope-aware
  channel bed at culvert ends. Matches the DamSafety Runner 3.11.1 fix.

Still to review against DamSafety:
- volume_balance operator split and notes (anuga_model.py)
- basemap label probe / timeout handling (Hazard)

## Ported from DamSafety (2026-10-28)
- volume_note / structures_net / operator_share (anuga_model.py): volume gain notes
- suite_offline / probe_error_text / probe_key / label_checks (basemap.py): probe helpers
