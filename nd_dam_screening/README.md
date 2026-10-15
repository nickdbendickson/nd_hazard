# ND Dam Breach Screening (v0.28.0)

Screening-level batch tool: for each North Dakota dam, estimate storage, place a breach,
compute a breach hydrograph, run a 2D flood simulation, and suggest a hazard class under
the ND Dam Safety Standards policy (Sec. 5). **Output is for prioritising reviews. The
official classification is the Department's.**

## New in 0.28.0

**A truncated flood is no longer a quiet answer.** "Step 7 terrain for larger clips" now follows the
terrain source (3DEP, or your LiDAR when a folder is given) instead of *cached clips only*. With
cached clips only, a flood that reached the edge of the step 2 clip was cut off there and the dam
screened on what fitted inside it: a 3,937 ac-ft dam screened **Medium with 0 buildings**, and with
the clip allowed to grow the same dam screened **High with 27 buildings and 86 roads**. **This moves
results, in the conservative direction only** - a dam whose flood stayed inside its clip is
unchanged; a dam whose flood was cut off can only gain consequences, so its class stays the same or
rises. A project that saved *cached clips only* on purpose keeps it, and the preflight now warns
about it with the reason. The flag itself is on the screen instead of only in `summary.csv`: the
Simple tab's step line and the Results tab's dam table say *their class is a FLOOR, not an answer*
in red, with the whole reason on the cell.

**The window tells the truth about the grid.** The resolution preset (Quick 50 m / Standard 20 m /
Detailed 10 m, or Custom) is passed to step 7 and shown live under the boxes. The "Grid size (ft)"
box did not decide the run - step 7 took its own default of 50 m whatever it said - and is now
enabled for the Custom preset only. The cached DEM cell follows the source cell (10 m, 32.8 ft)
instead of caching a 10 m source at 1 m (9 million cells for nothing).

**Both loading cases in one run.** Sunny-day and flood-day are modelled (step 7 `SCEN_BOTH`) and
classified, the **worse** class governs under the incremental rule, and `hz_scen` /
`hz_scen_class` / `hz_scen_why` say which case that was. **This moves results upward only**, and
only for a dam whose second loading case is worse. On by default for *Run one dam* (a minute of
model), off by default for a county.

**How much the breach parameters matter, per dam.** `bp_class_lo` / `bp_class_hi` / `bp_stable` /
`bp_detail`: the ND 5.3 equations' low / mid / high breach width and formation time, each routed,
with the class under each. A dam whose class depends on the breach parameters is **marked** instead
of being given one number, and its confidence drops to `review`. The band classes come from the
modelled flood scaled by the change in peak outflow (d ~ Q^0.6, v ~ Q^0.4), not from a second flood
model, so they can raise a structure already in the flood but cannot find one outside it: the
spread is a lower bound on the uncertainty. **The dam's own class is never changed by the band.**

**Warning time.** Arrival-time contours (30 min, 1 h, 2 h) as a layer in `7_flood_polygons.gpkg`,
and per impacted structure the arrival hour and the warning band (`warn_time`: under 15 min /
15-60 min / over 60 min, the loss-of-life bands of the suite), with `hz_warn` per dam.

**An interrupted county run picks up where it stopped.** Step 7 writes `run_dams.json` in
`7_simulations` as each dam finishes and skips the dams that already have a result (`SKIPDONE`, on
by default; switch it off to force a re-run). The manifest never decides a dam is done on its own:
its rasters have to still be there, and a FAILED dam is tried again.

**The window.** A time estimate next to the elapsed timer before a run (dams x seconds per dam at
the cell size that will really be used, the workers, and what an interrupted run has left). A grey
Run button says **why** it is grey, in its tooltip and the status line. *Run all* is now **Run
county**, beside *Run one dam...*. A new **Priority** tab ranks every dam of the Dams folder by
computed class against the class on record, worst disagreement first (ND *Medium* and DWR
*Significant* are the same tier and count as agreement), with an Excel export of the whole list and
of the review list. The preflight no longer warns *could not open* for the four `0_suite_data`
clips that the data step writes when the run starts.

**The hand-off to a detailed run.** The `screened` status this plugin writes to `dam.json` now
records what the run actually did: `cell_m` (the grid the flood really ran at, in metres),
`method` (the breach equation and mode as an ND Dam Breach Runner method key) and `scenario` /
`loading_case` (the case the reported class came from - the governing one when both were run).
ND Dam Breach Runner 3.20.0 reads them and fills only the inputs the engineer left empty, so a
20 m screening no longer becomes a 10 m detailed run with the Runner's own breach. Both of this
plugin's loading cases are handed over as the Runner's `sunny` scenario: neither routes a flood
inflow, which is what the Runner's `flood` means; `loading_case` keeps the distinction. Tool 5
records the breach mode in a field of its own (`brch_mode`).

No change to the hazard criterion (the ACER TM 11 charts, shipped signed off) or to the ND 5.2 /
5.3 rules.

## New in 0.27.0

**Streams and waterbodies are USGS 3DHP.** With ND Dam Hazard 2.29.0 (suite 1.15.0) the offline pack and the
servers list give 3DHP flowlines and waterbodies instead of NHD (pack layers 3DHP_flowlines and
3DHP_waterbodies; a pack from before 1.19 with NHD_ layers is still read, and a pack with both is read
once). Each 3DHP feature has the NHD name and type fields too (gnis_name, ftype), so tools 3, 6, 7 and 16 read
them as before. The screening method is unchanged.

## New in 0.26.0

- **Tool 3, a dam with no basin in the DEM.** When no mapped water body, no hydro-flattened water and
  no closed depression is found within the search distance, but the record has an inventory storage
  (maximum, else normal) and a height, the dam is no longer FAILED: a quadratic stage-storage curve
  from the ground at the dam point (median of the 5 x 5 cells) to ground + height, holding the
  inventory volume at the top, is assumed. `stor_method` = `inventory curve`, `stor_src` =
  `inventory (assumed curve)`, confidence low, `stor_notes` starts with `ASSUMPTION`, and the log
  carries a warning. Verify the location and the DWR values of such dams. Without a storage or a
  height the dam still fails as before.
- **Tool 4, breach direction.** When neither the water-body test nor the terrain side test gives a
  vote, the steepest drop of the DEM from the dam midpoint (8 compass directions at 200 m, or 40 m
  when 200 m has no data; at least 0.2 m of fall) is the downstream direction: `dir_method` =
  `terrain trace`, `dir_conf` = low, with a note to verify.
- **Tool 7, flood polygons.** One GeoPackage per run, `7_flood_polygons.gpkg` in the output folder,
  with three layers: `extent` (dam_id, scenario), `velocity_class` (vel_class: `0-2 ft/s`,
  `2-5 ft/s`, `over 5 ft/s`) and `arrival` (within_hr: 0.5, 1, 2, 4, 8). Wet means a depth of at
  least the suite's wet depth (0.1 ft), the same as the hazard step, not 1 ft. A dam run again
  replaces its own polygons. Option `WRITE_POLYGONS` (on by default).
- **Window.** *Run selected dams...* next to *Run one dam...*: a list of the dams of step 1 with
  check boxes; the ticked dams run through the whole chain together into one folder under
  `one_dam/` (the step 1 filter set to their IDs; the window's own settings are not changed). On
  the Results tab, with a row of the dam table selected, *Open Runner...* and *Open Hazard
  Helper...* open the other plugin on that dam.
- **Preflight.** An NSI-only structures layer (`occtype` / `bldgtype`) no longer warns about the
  missing `occ_cls` / `prim_occ` fields.
- **Local DEM.** A DEM GeoTIFF written by the download tool carries the tags SOURCE, CRS
  (EPSG:5070), UNITS (metres) and CELL_M beside its `_dem.txt` sidecar.

## New in 0.22.1

Tool 15 (Run the steps in order) from the command line (`qgis_process run nddam:runchain` with no `--INCLUDE`) no longer runs step 0 (Build offline data package) first. QGIS passed option 0 for an empty optional-steps list, so the chain stopped at step 0 when no data package inputs were given.

When you give tool 1 (or the window) your own DWR layer and the NID comes from the suite data (the offline pack, the nid/ folder or a download), only the NID dams within 0.5 mi of your DWR layer's dams are added, and only inside the chosen counties. Before, every NID dam of the work area was added, which is all of North Dakota when the work folder has no dams yet, so a one-dam run became a run of about 980 dams. Without your own DWR layer the NID still covers the work area.

## New in 0.22.0 (with ND Dam Hazard 2.24.0 and ND Dam Breach Runner 3.14.0; damsuite 1.10.0)

The offline pack is now a folder, ND_Dam_Suite_Pack, with one GeoPackage per layer (structures/, roads/, railroads/, lifelines/, hydro/, dwr/, hifld/, boundaries/, landcover/). Each file opens in QGIS by itself. LAYERS.txt in the folder lists every layer, its file, feature count and source, and what was left out. The DWR inventory files, the HIFLD lifeline layers and NLCD land cover are inside the pack, so the pack alone is enough. An older nd_offline_pack.zip still works, and the dwr/, hifld/ and nlcd/ drop-in folders are still read when the pack lacks a layer.

Data source, shared by the three plugins: Offline pack (choose its folder or zip) or Download from the servers (the pack is not read). Set it in Suite data and servers in any of the plugins; the setting is the same in all three. With Download chosen, the window's suite-data fallback downloads a layer for the work area into `<work>/0_suite_data/` (reused for 7 days) instead of clipping the pack; if a download fails it falls back to the dwr/ and nid/ folders.

## New in 0.21.1 (with ND Dam Hazard 2.23.0 and ND Dam Breach Runner 3.13.0; damsuite unchanged, 1.9.0)

**Tool 1 fills its field boxes in.** When a DWR or NID layer is picked, or the window fills it
in, each field box is set to the layer's field: the DWR GeoPackage export's `structure_id`,
`max_dam_height`, `max_pool_vol`, `normal_area`, `hazard_class`, `national_id` ... (the boxes'
defaults are the shapefile's 10-character names, which the GeoPackage does not have, so they
stayed blank) and the NID's `NIDID`, `NAME`, `NID_HEIGHT`, `NORMAL_STORAGE`, `MAX_STORAGE`,
`SURFACE_AREA`, `HAZARD_POTENTIAL`, `DAM_LENGTH`. A field you choose is kept. A box left empty is
also found at run time, and the log names each field. Before 0.21.1 this was done for DWR layers
only, so an NID layer with empty boxes added dams with no ID, height or storage.

**NID without the offline pack.** The NID layer comes from the offline pack. Without it, put the
NID dams file (for example `NID_dams.gpkg`) in a folder `nid/` in the suite data folder; "Fill
empty layers from the suite data" and tool 1 then use it.

**Tool 1 uses the suite data in the window.** An empty DWR or NID box on the Inputs tab now falls
back when tool 1 opens, like every other input (the dam folder's data, the suite offline pack,
`dwr/`, `nid/`), whether or not "Fill empty layers" was pressed. Clear the NID box for a DWR-only
run.

**Tools 2, 3 and 4 find tool 1's output.** The window fills them from `<work folder>/1_dams.gpkg`
as before and now says in its log when that file is not there. Opened from the Processing
Toolbox (which knows no work folder), their dialog starts with the last Standardized dams written
(tool 4: tool 3's, else tool 1's); a run given no layer still stops. Tool 1 says in its log when
its output went to a temporary layer.

## New in 0.21.0 (ND dam suite release set: ND Dam Hazard 2.23.0, ND Dam Breach Runner 3.13.0)

Ideas taken from the DamSafety plugins audit (2026-10-08) and rebuilt on this plugin's own rules.

**Tool 7 pre-run check** (`damsuite/solvegate.py`). Before the first dam, LISFLOOD-FP runs
1 simulated second on a 5 x 5 grid (once per executable; never for the built-in engine;
`ND_DAM_PRE_RUN_SMOKE=0` skips it). Each dam's hydrograph must have a peak and a volume and
its terrain must cover the dam point; a dam that fails is FAILED with the reason and the
other dams still run. A buildings layer whose whole extent misses the dams stops the run and
says whether it has no CRS or does not overlap.

**Smaller 3DEP tiles.** Without the suite terrain cache, 3DEP terrain is downloaded in
tiles of at most 1,200 pixels (was 4,000); a 10-mile box at 10 m used to be one tile and
could time out. Through the suite terrain cache (2,000-pixel tiles), a tile whose request
fails is asked for again as four quarters and joined (needs GDAL). The cache grid is
unchanged, so cached tiles stay valid.

**Fill empty layers from the suite data** (a button on the Inputs tab). It shows and sets
the layers a run would use for the empty slots (DWR dams, NID dams, structures, roads,
railroads, lifelines, water bodies, dam lines), taken from the dam folder, the offline pack
or the suite `dwr/` folder by the same rules the run uses. A slot you set is left alone.
Streams and land cover are not filled because they change the model.

**Ponds: inventory storage check** (tool 16, new optional field `WB_STOR`, ac-ft). When
the inventory storage and the DEM curve differ by more than 50%, the pond gets a
`CHECK: inventory storage ...` note and low storage confidence (`stor_conf`), and
`inv_vs_est` holds the ratio. The DEM curve is still used.

**Fixed: tool 16 gave a different storage on a second run.** The rerun read each pond from
its dam clip (`dam_<ID>.tif`, written at the coarser clip cell) instead of the terrain at
the analysis cell; the test pond went from 2,800 m3 to 1,600 m3. The clip is now used only
when it is as fine as the analysis cell.

## New in 0.20.0 (ND dam suite release set: ND Dam Hazard 2.22.0, ND Dam Breach Runner 3.12.0)

**The computer is kept awake while tool 7 runs a flood model** (built-in engine or
LISFLOOD-FP; shared `damsuite/keepawake.py`, damsuite 1.8.0). It runs beside the model, and
when it cannot work (for example Linux without a systemd bus) the log says so once and the
run goes on. `ND_DAM_KEEP_AWAKE=0` switches it off. Tested on Linux only.

**Tool 7: enlarging the clip before the first flood run is now an option, off by default**
(`PREGROW`, "Enlarge the clip before the flood run when the downstream trace reaches its
edge"). From 0.15.0 the clip was doubled before the first run whenever the downstream trace
reached its edge; on Absaraka that grew it to the 40,000 ft maximum and the flood was still
cut off, and on a drained landscape most dams started at the largest clip. By default the
clip again grows only after a run whose flood reaches the terrain edge (the re-run budget
applies). Turn `PREGROW` on (in the chain settings: `"pregrow": true`) for the 0.15.0 - 0.19.x
behaviour.

**Window:** the DEM cell size and Grid size boxes are labelled (ft). They have held feet since
0.18.0 but still said (m), so a value typed in metres gave a grid 3.28 times finer.

## New in 0.19.0 (ND dam suite release set: ND Dam Hazard 2.21.0, ND Dam Breach Runner 3.11.1)

**DWR unknown structures and dugouts.**

- Tool 1 reads DWR's *Unknown Structures* and *Dugouts* exports as well as the dams. The kind is
  detected from the fields (or chosen), and IDs get a prefix because each export numbers its
  structures separately: `DWR-`, `UNK-`, `DUG-`. Status *auto* keeps Intact dams; Intact, Unknown
  or blank unknown structures; Intact or Pending dugouts. GeoPackage field names are found as well
  as the shapefile names.
- Unknown structures: a missing height is estimated from the DEM (crest minus the lowest ground
  near the point); with no mapped water body, the hydro-flattened water in the DEM is used, else
  the nearest dry basin. The waterbody search distance can be set in the chain settings
  (`wb_search_ft`; DWR's unknown-structure points can sit hundreds of feet off their pond).
- Dugouts (tool 16, with *DWR dugout records* set): DWR's records are joined to DWR's dugout
  polygons. A dugout releases only what stands above the natural ground. A LiDAR release above
  DWR's total storage is held to the DWR total and flagged CHECK. Waste lagoons get a note.
- Every estimated height or storage is marked (`h_src`, `stor_src` = *estimated (DEM)*), lowers
  the class confidence, is named in the dam page, summary.csv and step 8's notes (triage clears
  too), and is written to dam.json as estimated, so ND Dam Hazard names it in its run log.

**Storage leak fixed (tool 3).** The DEM fill used to run out over the spillway or a low saddle
into the next valley (Absaraka 28,701 ac-ft against DWR's 1,616). Now:

- the fill stops at the spill level (border of the DEM window reached, or the flooded area jumps);
  a dam of known height goes on to its crest with the reservoir area at the spill level, a
  structure without one is held at the spill level;
- a pond whose pool already leaks at its own level (lower ground past the dam at DEM resolution)
  gets a prism above the pool (pond area times the rise);
- DWR's maximum storage, when known, is the volume at the top of dam: the curve is stretched to it
  when the DEM falls short or runs past 1.5 times it. DEM and DWR more than 3 times apart is a
  CHECK with low confidence.

## New in 0.18.1

**ND LiDAR over HTTPS.** Tool 20 (and the DEM box in this window and in the Runner) with Source
*ND DWR LiDAR* and the FTP link left empty finds every LiDAR_NorthDakota_QL2 tile that touches the
area in the statewide tile index and downloads it over HTTPS from `lidar.dwr.nd.gov`:

- *Dam*: the dam point (or the dam in dam.json) and the buffer in miles (new; it needed an FTP
  package before);
- *County*: one North Dakota county clip;
- *State*: all of North Dakota (about 47,600 tiles; long).

DEM_Imagine (1 m, metres) and/or ASCII_Grids_ft as before. The index is read in place (only the
records needed). FTP (port 21) is blocked on cloud machines and some networks; HTTPS is not. A
pasted FTP link still pulls that package over FTP.

## New in 0.18.0 (ND dam suite release set: ND Dam Hazard 2.19.0, ND Dam Breach Runner 3.11.0)

**US units.** What you read is in feet, ft/s, ft²/s, cfs, acres and acre-feet:

- the HTML reports (height, breach peak in cfs, cell, depth, flooded area in acres; the impacts
  table in ft, ft/s and ft²/s; the hydrograph in cfs; the depth map legend in feet) and
  `summary.csv` (`height_ft`, `qp_cfs`);
- the Processing inputs: cell sizes (tools 2, 7, 7b, 12, 14, 16 and the propose-dam tool; 13's move distance),
  the corridor distances, and the settings window's *DEM cell size (ft)* and *Grid size (ft)*.
  A saved model or history that passes the old metre parameters still runs, and window settings
  saved in metres are converted when they load. Tool 20's 3DEP cell stays in metres: it picks a
  dataset (1 m, 3 m, 10 m);
- every result table keeps its metric working fields, because the steps read each other's tables
  by field name, and gains a US twin of each (`height_ft`, `td_qp_cfs`, `td_vw_acft`,
  `inv_area_ac`, `vel_fps`, `dv_ft2s`);
- step 7 also writes feet companion rasters (`_depth_ft.tif`, `_vel_fps.tif`, `_dv_ft2s.tif`) next to
  the metric ones the later steps read; the hydrograph and stage-storage CSVs have feet, cfs and
  acre-foot columns after the metric ones.

Fixes from the Elm River Detention Dam 1 run:

- **Breach direction** takes the reservoir polygon that touches the dam line (else the largest
  nearby), samples along the whole line, and the terrain wins when it disagrees with the water
  bodies. The breach was aimed into the reservoir on a 4,000 ft dam with a small pond below it.
- **Tool 8** finds the AADT, occupancy, class, trains and other fields whatever their case.
- **The chain stops and says so** when step 7 failed for every dam, and warns when it failed for some.
- **Preflight:** an empty structures layer is a warning ("empty on purpose?"), not a refusal; the
  "dams without a dam line" count honours the dams you picked.
- **Tool 20** names the blocked host when the 3DEP download fails; unclosed directory scans no longer
  fill the log with ResourceWarnings.

## New in 0.17.0 (ND dam suite release set: ND Dam Hazard 2.18.0, ND Dam Breach Runner 3.10.0)

Coordinate systems and units, checked in a real QGIS 3.34 (Linux):

- **DEM elevation units.** Local tiles whose band says feet or US survey feet are converted to
  metres; untagged tiles are taken as metres and the log names them. Every DEM this plugin
  writes (downloads, cuts, dam_<ID>.tif) is tagged "m", so the Runner no longer reads a 3DEP DEM
  warped to State Plane feet as feet.
- **Mixed coordinate systems.** Local tiles in more than one CRS (UTM 13 and 14, State Plane
  North and South) are warped onto one EPSG:5070 grid; before, tiles unlike the first were
  dropped and left no-data holes.
- **Move breach point** reads the map centre in the map's CRS and refuses a move of more than
  5 km; **zoom to selected** shows 400 m whatever the map's CRS.
- **QL2 tiles:** the state box follows the curved southern edge in State Plane (a 6 km strip was
  missed).
- **Caches:** the terrain mosaic and `dam_<ID>.tif` are rebuilt when the tiles, cell size or
  extent change.
- **Linux / macOS:** the LISFLOOD-FP executable accepts a name without an extension, and the
  self-test has an `EXE` parameter so its LISFLOOD-FP engines can run.

## New in 0.16.0 (ND dam suite release set: ND Dam Hazard 2.17.0, ND Dam Breach Runner 3.9.0)

**Suite data and servers / Get data for this dam.** Plugins > ND Dam Suite > *Suite data and
servers...* sets one suite data folder per computer (shared by the three plugins): the statewide
offline pack, a shared terrain tile cache, your own DEM (`dem/`), land cover (`nlcd/`) and the ND DWR
dams / dam lines you drop into `dwr/`. *Get data for this dam* (also the Processing tool
`nddam:getdamdata`, "Get data for this dam (or all dams)") clips what a dam needs into
`<dam folder>/data/` and records the files in dam.json's data section. Then:

- **Empty data layers fill in by themselves.** A layer you choose on the Inputs tab always wins,
  then the step-0 package as before. With neither, the window and Run all take (a) the dam
  folder's data when the run is about one dam (Run one dam, when the dam has a folder in the
  Dams folder), else (b) the offline pack, clipped to the work area into `<work>/0_suite_data/`,
  else (c) your files in `dwr/`. The log says which one was used. Stream lines, dam lines and land
  cover for the flood steps (6, 7, 9) change the model, not only its data: they are still taken
  only from your choice or the package.
- **Tools 1 and 16** fill an empty DWR / NID / water-body layer from the dam folder when the run
  is about one dam (Only these IDs = one dam whose folder exists).
- **Terrain is downloaded once.** With a suite data folder, every 3DEP request (tools 2, 7, 16, 19
  and 20) reads the shared tile cache (the terrain server chosen in Suite data and servers): a
  tile is fetched once for every dam, tool and work folder. Without one, nothing changes.
- **Tool 20 / the DEM box:** the file is named after the dam ID or county
  (`<dam ID>_dem_10m.vrt`, `Cass_dem_10m.vrt`), so a second dam no longer overwrites the first. The
  box sends its own cell size (10 m to start; it sent step 2's 1 m before) and remembers its values.
  With a Dam ID that has a dam folder, the DEM goes into `<dam folder>/data/` and dam.json, and an
  empty dam point is taken from dam.json.
- **Tool 7b:** the Runner's DEM falls back to the dam folder's DEM (empty DEM = each dam's own),
  and the DEM and receptor files sent are recorded in dam.json's data section where it has no
  entry yet, so the Runner and ND Dam Hazard fill their inputs with the same data.

Nothing here has been run in a real QGIS or against the real services (simulated QGIS only).

## New in 0.15.0 (ND dam suite release set: ND Dam Hazard 2.16.0, ND Dam Breach Runner 3.8.0)

(Opt-in from 0.20.0: `PREGROW`.) Tool 7: when the downstream flow path (steepest-descent trace) reaches the edge of the
terrain clip, the clip is doubled (up to the largest clip, `RMAX_FT`) before the first flood
run instead of after it. This only happens when tool 7 may fetch larger clips (3DEP or a
local DEM folder); with cached clips only nothing changes. The enlargement is written to
`sim_notes` and `radius_m`. It does not use up the re-run budget, and a no-data hole inside
the clip does not trigger it. A trace that reaches the edge does not show that the flood
would, so most dams on a drained landscape now start at the largest clip. Expect larger
grids and longer run times than 0.14 with 3DEP / local terrain. Tool 12's estimate does
not include this.

`core/breach_hydro.analyze_dam(..., hydrograph=(t_seconds, q_m3s))` replaces the top-of-dam
outflow with a supplied series. The series is checked: time strictly increasing in seconds,
discharge >= 0 m3/s, finite values, a zero appended at the end if needed. A note is added
when its volume exceeds the storage. This is a Python entry point only: no Processing tool,
chain step or hand-off passes a hydrograph to it yet. The breach-geometry equations are
unchanged. The Von Thun & Gillette Cb storage limits are now exactly the Runner's
(1,000 / 5,000 / 10,000 acre-ft).

**Download a DEM** (tool 20, also a box on the Simple tab and in the Runner's window): USGS 3DEP
(metres, EPSG:5070) for a dam buffer, a county or the state, or ND DWR QL2 LiDAR from a package
staged on lidar.dwr.nd.gov or by county / state from the statewide tile index (public guest login).
County boxes are padded download clips, not legal boundaries. DEM_Imagine (metres) is the default
product; ASCII_Grids_ft is off by default and, when chosen, goes to its own VRT converted to metres.
Feet and metre grids are never mosaicked together. A tile that fails to download stops the tool and
is listed in failed_tiles.txt; running it again resumes. Optional warp to a work CRS (EPSG:5070,
26914, 2265, 2266). Nothing here has been run against the real services.

## New in 0.14.0 (ND dam suite release set: ND Dam Hazard 2.15.0, ND Dam Breach Runner 3.7.0)

**Propose a dam** (tool 19). Draw the crest line. The plugin builds the embankment into a
conditioned copy of the DEM — crest at the design elevation, faces down to natural ground —
and does not modify the source DEM. The dam height is the design crest minus the lowest
natural ground on the line (the breach point is there). Storage is measured on the pre-dam
LiDAR, stage by stage, and a rim below the crest, where the pool would leave the basin, is
reported. The inventory, curve, breach point, terrain clip and dam.json are what steps 5–10
and 7b, the Runner and ND Dam Hazard already read, and every report carries
"proposed — embankment inserted from the drawn line". Use a work folder of its own. Tick
step 19 on a chain; Run all does not invent a crest line. Nothing here has been run in a
real QGIS.

## New in 0.13.1 (ND dam suite release set: ND Dam Hazard 2.14.0, ND Dam Breach Runner 3.6.0)

Only the shared `damsuite` package changed (1.4.1: the release table and the version check's note
that a Runner before 3.6.0 uses an inventory's culvert elevations unchecked). No tool, result or
setting of this plugin changed.

## New in 0.13.0 (ND dam suite release set: ND Dam Hazard 2.13.0, ND Dam Breach Runner 3.5.0)

A release about using the plugin, not about the numbers: the results of the tools for the same
inputs are unchanged except where a unit changed (below). Nothing here has been run in a real QGIS;
every item was exercised against the simulated QGIS (three flavours: 3.28, 3.40, 4.0 / Qt6).

- **Simple tab.** Work folder, what to screen (DWR dam inventory / ponds and dugouts / both), data
  package, terrain, flood engine, hazard criterion, settings file, preset; the steps in plain words
  with their state; `Check before running`, `Run all`, `Run one dam...`. The 0.12 window is the
  `All steps` tab (a numbered button per tool). The built-in engine is the default now (no install).
- **One settings file for the suite.** `damsuite_settings.xlsx` (or `.csv`): every engineer-chosen
  value of the tools of the three plugins in one table (plugin, tool, key, parameter, unit, value,
  starting value, YOUR value, help), plus this window's choices. The tools open with its values
  (Processing dialogs, the chain, qgis_process); every value can still be changed in a dialog. Edited
  on the Settings tab - a HEC-RAS-style grid: a tab per family of parameters, `Value / Unit / Starting
  value`, a Find box, `show only YOUR values`, `show only changed`, the help of the selected row, Reset /
  Load / Save - or in Excel. Edits are saved to the file at once. The file is written without any
  extra Python package (a small .xlsx writer / reader of its own; CSV works everywhere). A copy of
  the settings a run used goes to `<work>/settings_used.xlsx`. Three presets ship with the plugin
  (`presets/`): County screening, Pond survey, Single dam detailed.
- **Units.** Every user-facing distance is in feet now (search distances, corridor and clip widths,
  arrow lengths, match distances); areas in acres, volumes in acre-feet, flows in cfs. The one
  exception is a grid's cell size, which is a property of the DEM and stays in metres with the feet
  in its label. New `_FT` parameters; the old metre parameters stay hidden and are still read, so a
  saved model or history from before 0.13 gives the same result. Starting values are round numbers
  in feet (e.g. toe search 200 ft instead of 60 m), so a run with defaults can differ slightly.
- **Preflight check (tool 17, `Check before running`).** A few seconds: work folder and disk space,
  the USGS 3DEP service or the local DEM folder, the LISFLOOD-FP executable when that engine is
  chosen, the dam / water-body / dam-line / structures layers with their fields and CRS, dams without
  a dam line, the ACER TM 11 chart file, the Runner plugin for 7b, the settings file. OK / WARN /
  FAIL with what to do. `Run all` and the headless chain (15) refuse to start on a FAIL.
- **Run one dam.** Pick a dam or pond (the list, or type an ID) and the whole chain runs for it
  alone into `<work>/one_dam/<ID>` (tools 1 and 16 have `ONLY_IDS`); the report opens at the end
  and the layers are added as a group. The quick screen is skipped so the flood is simulated.
- **Review tab.** The dams that need a look - uncertain breach direction (no dam line), uncertain or
  unusual pond type, borderline class, the report's review queue - with Zoom to, Accept, Exclude,
  Set pond type, Move breach point, Add note. Decisions go to `review_decisions.csv` and into the
  layers (`rev_decision`, `rev_by`, `rev_date`, `rev_note`; a moved point gets `dir_conf = reviewed`).
  Tools 5, 6, 7, 8 and 12 skip an excluded dam ("excluded by review"); tool 8 leaves its class empty.
- **Both kinds in one folder (tool 18, Merge dams and ponds).** `Both` on the Simple tab runs 1-4 and
  16, then appends the ponds to copies of the dam layers (`18_dams_all.gpkg`, `18_breach_points_all.gpkg`,
  `18_flow_arrows_all.gpkg`, `18_stage_storage`, `18_terrain`; hard links where possible) and tools
  5-10 take the 18 files. Nothing in the 1-4 or 16 files is changed.
- **How it decides...** - an illustrated help page (`help/how_it_decides.html`): the rim / toe / head
  of a pond with its parameters, the 16 sectors and the breach point, the triage envelope, the
  ND 5.2 / 5.3 decision tree as the shared rules module applies it, the units.
- Results are added to the project as one layer group; the Processing toolbox shows `Screening steps`
  (0-8, 10, 16-18) and `Advanced` (9, 11-15); the three plugins share a `Plugins > ND Dam Suite`
  menu (window, Dam suite home, Suite settings). Per-dam failures say what to change (a missing
  curve -> "run step 3 again", a missing field -> "pick the field in the dialog").

Not done / not run: nothing here has been run in a real QGIS. The settings grid, the Review tab's
layer edits (sqlite3 on the GeoPackages) and the 3DEP reachability check were tried only against test
doubles and files. The `Both` merge copies geometries as they are; both sides are written in the same
CRS by the tools, so this holds for the work folder's own files only. The ACER TM 11 chart file
ships signed off (0.26.1), so the chart criterion passes the preflight; a chart file of your own must
name the engineer who signed it off - the check says so.

## New in 0.12.0 (ND dam suite release set: ND Dam Hazard 2.12.2, ND Dam Breach Runner 3.4.4)

**Tool 16 - Find ponds and dugouts.** Takes a layer of water-body polygons (NHD waterbodies, NWI
wetlands or your own) plus the LiDAR terrain (3DEP or local DEM tiles), decides for each water
body whether it is held by an embankment, sits in a dugout, or is a natural depression, and
writes the embankment ponds and dugouts as dams. It takes the place of tools 1-4 for ponds: its
outputs have the formats of tools 1-4, so tools 5 (breach hydrograph), 6 (triage), 7 (flood
simulation), 8 (hazard classification), 9 and 10 run on them unchanged, and the work-folder logic
and the chain (tool 15, "ponds and dugouts instead of 1-4") know it.

- How it decides (`core/ponds.py`): pool level = the flat water surface of the DEM, else the
  shoreline; the ground around the water is cut into 16 compass sectors; in each, rim = top of the
  embankment face, toe = its foot. A sector holds water above the ground when pool - toe >= your
  head value. An arc of at least your minimum arc = embankment pond (crest = lowest rim of those
  sectors, breach at the sector with the lowest toe, flow away from the water). Otherwise a dugout
  (steep banks, a raised spoil rim, or the layer's own "excavated" words) or a natural depression
  (left out unless you ask for it; listed with the reason in the excluded layer).
- Dugouts release only what is above the natural ground. When nothing is above grade (or the rim
  is lower than the 0.3 m breach height tool 5 can route) the pond is marked `no_flood = YES`: no
  flood is simulated and tool 8 reports "Low - no breach flood".
- Storage: above the pool from the DEM, below the pool by tool 3's shoreline-slope proxy; the
  greatest depth is your assumption (the LiDAR cannot see under the water).
- **Every threshold is yours** (head, minimum arc, face angle, dugout bank slope, assumed depth,
  smallest breach flood). The starting values are not standards.
- Use a separate work folder from the DWR dam run (where both exist, tools 5-10 use the DWR files).
- Tools 5, 6, 7 and 8 skip a `no_flood` dam; tool 12 leaves it out of the run-time estimate.
- Self-test (tool 14) has a step 16.

Not done / not run: nothing here has been run in a real QGIS (simulated QGIS and synthetic
terrain only). Water bodies are not downloaded (the NHD / NWI services were not reachable when this
was written): load the layer and pick it. Small ponds are rarely hydro-flattened, so the shoreline
pool level can be off by roughly 0.3 m. Check the classes on a few ponds against an aerial image
before a county run.

## New in 0.11.2 (ND dam suite release set: ND Dam Hazard 2.12.2, ND Dam Breach Runner 3.4.2)

From the first real-QGIS run (smoke kit, QGIS 3.44.14 LTR on Windows 11): the shared styles
declared their geometry type as a word, which QGIS reads as "point", so the flood-extent
(polygon) and flow-arrow (line) styles were refused; they are numeric now and all 15 styles
load. No other change in this plugin.

## New in 0.11.1 (ND dam suite release set: ND Dam Hazard 2.12.1, ND Dam Breach Runner 3.4.1)

Stability release; nothing here was run in a real QGIS or with a real LISFLOOD-FP. Results for
valid inputs are unchanged (the built-in engine bit for bit).

- QGIS 4 / qgis_process: metadata.txt declares `supportsQt6=True`, `qgisMaximumVersion=4.99`
  and `hasProcessingProvider=yes`, and the plugin has `initProcessing()` - so
  `qgis_process run nddam:runchain` (tool 15) can load the tools. Every QGIS 3-only enum
  spelling (319 of them) goes through the new `compat.py`; the tests run in a strict QGIS 4
  flavour of the mock too.
- LISFLOOD-FP's output goes to `lisflood.log` in the run folder; it went to a pipe that was
  read only at the end, and a long run filled it and then waited until the timeout. Cancel and
  timeout wait for the solver to end (Windows file locks).
- One dam's unexpected error is that dam's FAILED result; in parallel mode it ended the batch.
- Triage (tool 4): a breach toe with no traceable flow path (DEM edge, a pit, flat ground) is
  reported as such and stays "needs 2D"; it used to fail with KeyError 'stage_grid'.
- Built-in engine: NaN DEM cells are walls (they spread NaN through the run); an unstable run
  stops with a clear error instead of writing NaN rasters; about 15-20% faster (28 s instead
  of 33-36 s on a 160 x 220-cell test valley) with bit-for-bit the same results.
- Breach equations: a zero / negative / missing / non-finite input stops with a plain message.
- Storage (tool 3): an undeclared -9999 fill is no-data. A damaged raster names the file.
- No process-wide `gdal.UseExceptions()` (it changed GDAL for QGIS and the other plugins).
- The OGR in-memory driver is looked up as "MEM" first (GDAL 3.11).

## New in 0.11.0 (ND dam suite release set: ND Dam Hazard 2.12.0, ND Dam Breach Runner 3.4.0)
Nothing in this release has been run in a real QGIS either; every item below was exercised with
the test doubles in tests/ (tests/test_popup.py, test_selftest.py, test_runchain.py,
test_engine_baseline.py, test_tools_*.py, test_fv_engine.py). The first thing to run in a real
QGIS is tool 14.

- **Feet everywhere the engineer types a depth.** Tool 6 `MARGIN_FT` (was `MARGIN` in m), tool 8
  and 9 `MIND_FT` / `RDEP_FT` (were `MIND` / `RDEP` in m), tool 7b `WET_FT` (was `WET_M`). The old
  metre parameters are still accepted (hidden) so saved models and histories keep running: when a
  model passes only the old name, that value is used. Internal computation stays metric; the log
  prints both. Cell sizes and distances stay metres with the feet equivalent in the label. Tool 7's
  rasters stay metres (the file contract since 0.1); tool 11 converts them.
- **New tools.** `11 - Compare with a reference model` (the Runner's benchmark metrics on tool-7
  rasters against HEC-RAS / DSS-WISE / Runner rasters, per dam or a folder of dams; Benchmark_Summary.csv,
  Benchmark_Report.html, difference and agreement rasters in ft); `12 - Estimate the run time`
  (order-of-magnitude time of tool 7 from cell counts, steps and a calibration run of the built-in
  engine); `13 - Inventory diff` (new / removed / changed dams between two inventories; writes
  `inventory_change` into dam.json so the suite's next action says "refresh"); `14 - Self-test in
  this QGIS` (a synthetic valley through tools 1-8 and 10 with PASS / FAIL per step); `15 - Run the
  steps in order (headless)` (the window's chain for qgis_process / scripts, from the settings JSON
  the window exports).
- **Tool 5: breach equations and your own parameters.** Same equation list as the Runner:
  Froehlich 2008 / 1995, Von Thun & Gillette (erodible / resistant), MacDonald & Langridge-Monopolis
  (1984), NRCS TR-60 peak with a triangular or curvilinear hydrograph (the same formulas, checked
  against the Runner's functions). "Where the breach parameters come from": the equation, fields of
  the dams layer (average width ft, formation time hr, side slope, breach bottom ft) where filled,
  or one set of typed values for every dam; `brch_src` records the source per dam. Old indices and
  defaults unchanged.
- **Tool 7: no-breach baseline and increment** (off by default). A second run of each dam with only a
  steady ambient flow (`BASELINE_CFS`, YOUR value; 0 = dry) and the dam intact writes
  `<dam>_<scenario>_base_depth.tif`, `_base_vel.tif` and `_incr_depth.tif` = breach minus baseline.
  **Tool 8 `INCREMENTAL`** then counts impacts only where the increment meets the threshold (velocity
  from the breach run). A screening approximation of ND's flood-scenario comparison; the real PMF
  routing is the Runner's / the engineer's.
- **Tool 7: buildings as obstructions** (off by default): cells inside building footprint polygons
  (`BLD`; points are ignored) are raised by `BUILDING_RAISE_FT` (default 10 ft, YOUR value) or get
  Manning's n `BUILDING_N` (default 0.15, YOUR value). Bare terrain remains the default; neither
  treatment is right everywhere.
- **Faster built-in engine.** The finite-volume engine computes only inside the bounding box of the
  wet cells plus a halo, rebuilt when the front reaches its edge; results are bitwise identical to
  the full-grid engine (tests). About 1.7x on the test valley; more on big clips with a small flood.
- **Run manifests and "out of date" by content.** Every tool leaves `<output>.manifest.json` (or
  `<folder>/run_manifest.json`) in the Runner's manifest format: parameters, inputs and outputs with
  SHA-256 digests (damsuite.chain). The window marks a step out of date when an input FILE's content
  changed since the step ran - not when a date changed; steps without a manifest (run before 0.11)
  fall back to dates. Tool 8 with a Dams folder also writes `<dam>/_screening_model/run_manifest.json`
  per dam, which ND Dam Hazard's model-chain check reads like a Runner manifest.
- **Window.** Settings for the baseline, buildings and increment, the reference folder (tool 11)
  and the previous inventory (tool 13); buttons for tools 11-15; "Export settings (JSON)" for the
  headless chain. Suite home: Plugins > ND Dam Breach Screening > Dam suite home (the same window
  in all three plugins: every dam of a Dams folder with its next action, opens the plugin that does it).
- summary.csv: `height_ft` next to `height_m`.

## New in 0.10.1: the REG_05.2026 curve table
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

## New in 0.10: a popup window (Plugins > ND Dam Breach Screening)
The tools were only in the Processing Toolbox. The plugin now also has a window, opened from the
Plugins menu like the ND Dam Hazard window. It is a guide through the tools, not a second copy of
them:
- **Work folder.** One folder for everything the steps write, with fixed file names
  (`0_data_package.gpkg`, `1_dams.gpkg`, `2_terrain/`, `3_dams_storage.gpkg` + `3_stage_storage/`,
  `4_breach_points.gpkg`, `5_dams_breach.gpkg`, `6_triage.gpkg`, `7_simulations.gpkg`, `Dams/`,
  `8_hazard.gpkg`, `9_sensitivity.gpkg`, `10_reports/`). The window reads the folder and shows each
  step as done, ready, waiting for an earlier step, optional (0, 6, 7b, 9), or *out of date* (an
  earlier step was run again after it). The names are this window's convention only; a tool run
  from the toolbox with other names simply shows as not done.
- **Workflow tab.** One button per step (0-10 and 7b) opens that tool's Processing dialog with its
  parameters filled in: the output of the step before, the layers of the data package, the
  settings of the window, and the standard field names of the package's layers. "Open the next
  step" goes to the first required step that can start. The tools run from their own dialogs
  (background task, Cancel, log, history), so every filled-in value can be seen and changed first.
- **Run the steps in order** (Workflow tab). One button runs every step that is not done or is out
  of date, one after the other, as background tasks with the values the window would fill in and
  no dialogs: 1, 2, 3, 4, 5, 7, 8, 10, plus the optional 0, 6 and 9 if ticked (7b - the Runner - is
  never part of it). Before it starts it lists the steps and the data each will use, and says which
  data it will run WITHOUT (for example "no roads layer - it cannot affect the result" for the
  classification). It stops at the first step that fails, is cancelled, lacks a required input
  (a dam layer for step 1, waterbodies for 3, dam lines for 4, structures for 8, or a required tool
  parameter that is still empty), or does not write its output file; nothing is asked in between.
  Steps whose earlier step was run again are re-run (shown as "out of date").
- **Inputs tab.** The package exported by ND Dam Hazard (for step 0) and the data layers: a project
  layer or a file per data set. A layer chosen here wins; left empty, the tools use the layer of the
  same meaning in the package step 0 wrote. A layer of your own has unknown field names, so none
  are filled in for it. 0.16.0: with neither, the dam folder's data (Get data for this dam), the
  suite offline pack or your files in the suite data folder's `dwr/` are used (Plugins > ND Dam
  Suite > Suite data and servers).
- **Settings tab.** Terrain source, LISFLOOD-FP executable, flood engine, scenario, dams at once,
  lethality, chart file, the Runner's DEM and the dams folder. All are only starting values of the
  tools' own dialogs.
- **Results tab.** The class counts of summary.csv, the dam.json state of the dams folder, the report,
  the work folder, all result layers into the project, and a button to ND Dam Hazard.
- The choices are saved with the QGIS project. The Processing tools are unchanged and still listed
  in the Toolbox.
- Not run in a real QGIS: tests/test_popup.py drives the window through test doubles of Qt and
  QGIS. How the window looks and lays out, `processing.execAlgorithmDialog` with filled-in values
  (for example a `file.gpkg|layername=x` source) and the background task behind "Run the steps in
  order" (`QgsProcessingAlgRunnerTask`, tools called from a worker thread) have not been seen in
  QGIS; the order, stopping and output checks are tested with a synchronous stand-in.

## New in 0.9 (ND dam suite release set: ND Dam Hazard 2.10.0, ND Dam Breach Runner 3.2.0)
- **Tool 0 reads a package exported by ND Dam Hazard.** New input "or: a data package exported
  by ND Dam Hazard (GeoPackage)" - the file ND Dam Hazard 2.10's "Export data package for ND Dam
  Breach Screening" writes from its offline data pack or downloads. Every input you leave empty
  is taken from the package's layer of the same name (structures, roads, railroads, flowlines,
  lifelines, dams; waterbodies / dam_lines only if the package has them - ND Dam Hazard's pack
  has neither, nor any DEM). Layers you give explicitly always win. The package's habitable /
  mobile flags (ND Dam Hazard's own reading of each structure) and its aadt, passenger and
  trains_day are kept; `level` is rated again with this plugin's rule (a road without AADT is
  Medium by assumption here, where ND Dam Hazard writes Low "verify"). The log lists which
  layers came from the package, which were given, and what the package says is missing.
  Design: only the exported GeoPackage is read, not ND Dam Hazard's offline pack zip itself -
  the AADT join, habitability and lifeline rules stay in one place (ND Dam Hazard, the
  classification authority), the package is an ordinary GeoPackage you can open and check in
  QGIS, and tool 0 keeps working without ND Dam Hazard installed.
- **Tool 0 writes more layers:** `structure_points` (the structures as points - ND Dam Breach
  Runner's receptor layers are points), `lifelines` and `dams` (new optional inputs, copied as
  they are). A structures layer with its own `habitable` / `mobile` yes/no fields keeps them.
- **dam.json (the suite's shared dam record).** Tool 7b writes `<Dams folder>/<dam ID>/dam.json`
  before each Runner run: the values it sends (dam_id, name, NID ID, DWR structure number,
  height, storage, surface area when known, the breach point as lon / lat and EPSG:5070 x / y,
  existing class) and a "screened" status with the triage result (clear / needs 2D / not run),
  tool 8's class, borderline flag and data gaps. Tool 8 has a new optional "Dams folder" input:
  every dam that already has a folder there gets the same status (no folder is created). A
  value another tool or the engineer wrote is never overwritten - the conflict is logged and
  kept in the file; a dam.json that cannot be read is left untouched. lon / lat come from the
  EPSG:5070 point by the Albers inverse (NAD83 used as WGS 84, about 1 m apart).
- **Suite version checks.** When QGIS has loaded its plugins, an installed ND Dam Breach Runner
  or ND Dam Hazard that is older than this release needs is reported in the message log (tag
  "ND dam suite") and the message bar. Tool 7b stops when the Runner is below 3.1.0 and logs
  the versions of all three plugins.
- As before: nothing here has been run inside a real QGIS; the new code is exercised with the
  test doubles in tests/ (tests/test_suite09.py, including a real export made by ND Dam Hazard's
  exporter under its own test harness).

## Workflow (Processing toolbox > ND Dam Breach Screening, or the popup window)
0. Build offline data package - one GeoPackage (EPSG:5070) with standard fields: USA Structures
   (habitable, mobile) and structure_points, roads (aadt, road_class, level), FRA rail (passenger,
   trains_day, level), NHD flowlines/waterbodies, DWR dam lines, lifelines, dams. Later tools
   pick these fields up automatically. Inputs can come from a package exported by ND Dam Hazard.
1. Prepare dam inventory - ND DWR dams (primary) + NID; metric standard fields.
2. Cache terrain per dam - USGS 3DEP (once, needs internet) or local ND LiDAR DEM tiles.
3. Estimate reservoir storage - NHD HR waterbody + DEM; calibrated to inventory storage.
4. Breach point and flow direction - DWR dam line, joined by structure ID.
5. Breach parameters and hydrograph - Froehlich 2008 (default), Froehlich 1995, Von Thun &
   Gillette (erodible / resistant); top-of-dam piping per ND 5.1.1.
6. Quick screen (triage) - about a second per dam; clears dams whose generous flood envelope
   reaches no habitable structure, road that could be Medium/High, railroad, lifeline or other
   dam. A random audit share still
   goes to the 2D model.
7. Run breach flood simulations - several dams at once; engines: LISFLOOD-FP ACC or FV1, or
   the built-in engine (no install), optionally with the reservoir in the grid and a gradual
   breach. Automatic cell size, downstream corridor, run length, clip growth, road-crossing
   cuts and cascade check. Resumable.
7b. Detailed runs with ND Breach Runner - sends the dams that matter (default: High and Medium
   from tool 8) to the ND Dam Breach Runner plugin and brings its class back (see below).
8. Hazard classification - ND Dam Safety Standards Sec. 5.2 / 5.3 through core/nd_hazard.py
   (the runner's rules module), ACER TM 11 charts by default (see below); USA Structures and
   FRA fields understood directly.
9. Sensitivity - re-runs chosen dams with the four breach equations and Manning n x0.75/x1.25;
   class_stable = yes/no.
10. Reports and statewide summary - index.html (class counts, review queue, sortable table),
    summary.csv, and a page per dam (flood map, hydrograph, impacted features, notes).
11. Compare with a reference model - tool-7 rasters against HEC-RAS / DSS-WISE / Runner rasters
    (fit index, depth / velocity / arrival errors), per dam or a folder of dams.
12. Estimate the run time - order-of-magnitude time of tool 7 before starting it.
13. Inventory diff - what changed since the last inventory; dam.json "refresh".
14. Self-test in this QGIS - a synthetic valley through tools 1-8, 10 and 16.
16. Find ponds and dugouts - instead of 1-4 for ponds and dugouts: finds them in a water-body
    layer + LiDAR, builds the dam, storage, curve, breach-point and terrain files, and marks
    dugouts with no above-grade water as no breach flood (see 0.12.0 above).
17. Preflight check - what would fail a run halfway, in a few seconds (0.13).
18. Merge dams and ponds - the DWR dams (1-4) and the ponds (16) as one set of layers for 5-10 (0.13).
15. Run the steps in order (headless) - the window's chain for qgis_process / scripts.

Tool 1 defaults match the ND DWR 'Dams by Hazard Class' export (structure_, dam_name,
max_dam_he, normal_vol, max_pool_v, normal_are, hazard_cla, status, national_i, dam_length;
feet / acre-feet / acres; 0 = not recorded). Default status filter: Intact.
DWR dam lines (`dams_linear_features`, EPSG:2266) join to the dams by structure ID
(`structure_id` = DWR `structure_`; dam_id `DWR-5289` uses structure_id 5289).

## Hazard classification: ND 5.2 / 5.3 with the ACER TM 11 charts (tool 8)
The authority is the ND Dam Safety Standards (REG_05.2026) Sections 5.2 and 5.3 with the
Reclamation (1988) / ACER TM 11 depth-velocity danger charts (ND Fig. 5.1 houses, Fig. 5.2
mobile homes). Tool 8 classifies through `core/nd_hazard.py`, a byte-identical copy of the
ND Dam Breach Runner's module (with `core/acer_tm11_charts.json`), so both plugins give a dam
the same class from the same flood. Never edit the copies; re-copy them from the runner
(tests/test_nd_hazard_adapter.py fails when they differ). The adapter in `core/hazard.py`
converts the screening's metres and m/s to feet and ft/s.

- What is checked (5.2 / 5.3): habitable structures, roads, railroads, lifeline facilities
  (optional layer, points or polygons) and the other dams of the input layer that the flood
  reaches (5.3 cascading: the dam gets at least their existing class - `haz_existing`, or DWR
  `hazard_cla`; an unknown class is taken as Medium and listed as a data gap).
- Habitable structures: potentially lethal on the charts -> High; inundated at any depth ->
  at least Medium (economic loss). The charts' judgment zone is borderline and takes the
  higher class (5.3), as does a point within the digitizing tolerance of a higher zone or
  beyond the chart axes, and a structure whose velocity is unknown (no-data velocity; listed
  as a data gap). `hz_border = yes` when every governing finding is borderline or rests on an
  assumption.
- Habitable means: a tool-0 `habitable` yes/no field wins; otherwise every structure is
  habitable unless one of its occupancy texts (OCC, PRIM) names an uninhabited building from
  the editable word list (default `nd_hazard.UNINHABITED_KEYWORDS`: uninhabited, unoccupied,
  shed, garage, barn, silo, grain bin, outbuilding, well pad, oil well, pump house). 'Heavy',
  'Agriculture', 'Temporary Lodging', 'Duplex', blank or unknown are habitable. The runner
  reads one occupancy field (S_OCC); tool 8 reads OCC and PRIM, so a structure named a barn
  only in PRIM_OCC is uninhabited here and habitable in the runner (the runner is the more
  conservative). Mobile homes (Fig. 5.2): mobile / manufactured / trailer / NSI RES2.
- Sampling is nd_hazard's, as in the runner: points - the cell under the point; polygons - the
  deepest cell whose centre is inside each part's exterior ring (the centroid cell when none
  is), with the largest velocity over the wet cells; lines - every half cell, the deepest.
  Multi-part features (MultiPoint, MultiLineString, MultiPolygon) use their deepest part.
  Square north-up cells are required.
- "Any depth" is 0.1 ft = 0.03048 m (MIND and RDEP defaults; at least 0.003 m), the runner's
  WET_FT default. Before v0.8 roads needed 0.15 m and structures 0.05 m.
- Roads: AADT > 2,000 or interstate (I-94, I 94, I94, IH-29, 'Interstate', TIGER 'I') -> High;
  500-2,000 or sole access to an occupied location (optional yes/no field) -> Medium; < 500 ->
  Low. A missing, zero, negative or NaN AADT is unknown, never Low, and a road is not
  promoted by its name (the old 'state/US highway = Medium' heuristic is not in the policy):
  it gets the 'unknown AADT' class you choose (default Medium) and is listed in `hz_gaps`.
  Railroads: Medium; >= 10 trains a day or passenger (FRA codes A/B/C or yes; never T or X) ->
  High; a missing train count is a data gap. Tools 0 and 6 use the same rules.
- Other lethality options: the conservative 'every inundated habitable structure', and the ND
  1D rule (deeper than 2 ft), which 5.2.1.1 allows only for 1D models - these floods are 2D, so
  tool 8 warns when it is chosen. FEMA/Feinberg and your own curve remain, labelled
  "comparison only, not the ND authority"; everything but the structures follows nd_hazard.
- New fields: `hz_basis` (method), `hz_border`, `hz_gaps` (truncated to 254 characters),
  `hz_chart` (chart status and the first 12 characters of the chart file's SHA-256).
- **The charts are signed off.** The shipped `acer_tm11_charts.json` is signed off by Nicholas
  Bendickson (2026-10-12), so the default method runs. A chart file of your own (CHART_FILE) must
  record the engineer who signed it off (`signed_off_by`, status "digitized"; ND Dam Hazard >
  Chart sign-off writes one and installs it in both plugins), else the tool stops with
  nd_hazard's message pointing to the conservative method. The SHA-256 in `hz_chart` shows which
  file a result used.
- Chart points use the maximum depth and maximum velocity at a structure, which may not occur
  together: an upper bound. Tool 8 does not write the chart SVG figures (it has no report
  folder); the runner's classification writes them per dam folder.
- Cascading uses the dam points of the input layer: with the reservoir-in-grid engine a dam
  whose point lies in the modelled reservoir (a saddle dam) can count as reached. Check the
  governing reason (`hz_reason`) of such dams.
- Tool 6 (triage) uses the same rules: only a road known to be Low (AADT < 500, not
  interstate, not sole access) is ignored, and a lifeline or another dam inside the envelope
  also prevents 'clear'. Tool 9 passes all of tool 8's inputs through.

## Detailed runs with ND Breach Runner (tool 7b)
Needs the ND Dam Breach Runner plugin v3.1+ (provider `ndbreach`, with LISFLOOD-FP set up in
its provider settings); without it the tool stops with a message. For each chosen dam (High
and Medium from tool 8 by default; or High only, borderline or changed vs the existing class,
all, or the selected features) it runs `ndbreach:rundam` and then `ndbreach:ndhazardclass` on
the dam folder and reads 'Hazard Classification (ND 5.2).json'. What is sent:

| rundam parameter | from |
|---|---|
| DAM_ID, DAM_NAME | dam_id, name |
| DAM_POINT | the tool-4 breach point (on the DWR dam line), joined by dam_id, in EPSG:5070 |
| HEIGHT_FT | height_m / 0.3048 (else tool-5 td_hb_m, noted) |
| STORAGE_ACFT | td_vw_m3, else cal_total_m3, else est_total_m3, else inv_max_m3; / 1233.48 |
| AREA_AC | only with a tool-3 curve folder: mean area over the top 0.3 m of the curve below the crest (normal-pool areas are not passed) |
| TOP_FT | never passed: DWR elevations are a local datum; the runner takes the highest DEM point within 30 m |
| DEM, DAMS_FOLDER, CELL_FT | the tool's inputs (CELL_FT only when given; the old CELL in metres still works) |
| METHOD, USER_CSV | only for the screening hydrograph: tool-5 td_csv converted to hr / cfs in `<Dams folder>/<dam>/inputs/breach_hydrograph.csv`; otherwise the runner's default ND method |
| RECEPTOR_POINTS / _ROADS / _RAIL | structures (point layers only), roads, railroads |

The classification gets the structures, roads (with the sole-access field), railroads and
lifelines with the field names you choose (empty = the runner's own name matching), the dams
layer as DOWNSTREAM_DAMS (D_ID = dam_id, D_CLASS = the existing class field), the uninhabited-
building words (UNINHABITED_WORDS), 'any depth' as WET_FT (0.1 ft by default), the lethality
method, the unknown-AADT and unknown-trains classes and the chart file. In the default mode
dams whose tool-8 class is empty or NoData (failed simulation: class unknown) are sent too. Output: the input dams plus `rn_status` (done / failed: message /
skipped: reason), `rn_folder`, `rn_class`, `rn_border`, `rn_gaps`, `scr_class` (hz_class),
`rn_agree` (agree / runner higher / runner lower) and `rn_inputs` (what was sent). One failing
dam is logged and the rest continue; cancelling stops after the current dam. With the chart
method the tool checks the chart file before any run (the runner's copy is byte-identical).
Parameter names are checked in tests/test_handoff.py by importing the runner's algorithms
under its own fake QGIS; the runs themselves have only been exercised with test doubles.

## Engines
| Engine | Needs | Notes |
|---|---|---|
| LISFLOOD-FP ACC | LISFLOOD-FP 8.x executable | fastest; DV is an upper bound (max depth x max velocity) |
| LISFLOOD-FP FV1 | same | full shallow-water; max velocity / DV from snapshots |
| Built-in | nothing | explicit finite-volume shallow water (HLL, hydrostatic reconstruction), exact DV; ~10-50x slower than LISFLOOD-FP |
| Built-in, reservoir in grid | nothing | reservoir starts at the top of dam, bed under the LiDAR water surface estimated and calibrated to tool-3 storage, breach lowered over tf |

Checks run in development: Ritter dam break (rarefaction within 1.3% of h0), lake at rest
(|q| < 1e-13), mass conserved to round-off; built-in vs LISFLOOD-FP FV1 on the same valley
flood: extent agreement F = 1.00, section depths within 1%. A reservoir-in-grid breach gave
an outflow peak about 70% of the level-pool hydrograph (drawdown and tailwater), so the
hydrograph-injection engines are the more conservative.

LISFLOOD-FP is not bundled: build v8.x from source (CMake, C++14, OpenMP).

## Road crossings
LiDAR shows road fills but not culverts. With stream lines (NHD flowlines) given, tools 6 and 7
cut a one-cell, 4-connected gap wherever a short (<= 150 m), low (<= 8 m) barrier blocks a
stream, never through a DWR dam line; optionally only where a road crosses.

## Known limits (read before trusting numbers)
- The QGIS tool code has been exercised against test doubles of QGIS/GDAL, not in QGIS itself.
  Run a few dams you know before a statewide batch ('Run one dam' on the Simple tab is for that).
- Units: feet / acres / acre-feet / cfs for everything the engineer types, except a grid's cell
  size (metres, the DEM's own unit, shown with the feet). The layers carry both (`_m` and `_ft`).
- Tool 16 (ponds and dugouts) is screening on LiDAR: the pool of a small pond is usually the shoreline,
  not a flat water surface (about +-0.3 m), and the depth below the water is an assumption.
- DWR elevations are mostly 'Local' datum, so they are NOT used; only heights, volumes, lengths.
- Below-normal-pool storage is estimated from the shoreline slope unless inventory storage
  calibrates it. LiDAR is hydro-flattened; no bathymetry is read from it.
- Piping is modelled as a top-down weir breach (no orifice phase); injection engines have no
  tailwater effect on the outflow.
- Triage uses the unattenuated peak, normal depth, a margin (30% of depth, >= 0.15 m) and a
  reach of 2x the released volume. It is a screening shortcut; keep the audit share.
- The policy gives no numeric depth-velocity lethality; it points to the Reclamation 1988
  (ACER TM 11) charts, digitized by the engineer and shipped signed off since 0.26.1 (the
  default method); the ND 1D rule and the conservative method remain as options. The FEMA/Feinberg
  thresholds are practice values from the DSS-WISE Lite Technical Report (comparison only).
- Tool 7b has been exercised only against test doubles of the runner and of QGIS; run a few
  dams you know through it before a batch.
- Manning n from NLCD uses a typical table in core/solver_io.py (NLCD_N); edit for your standard.
- Automatic cell sizes are in core/domain.py (CELL_TABLE); edit to taste.
