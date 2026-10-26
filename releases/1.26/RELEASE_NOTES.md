# ND Dam Suite — release 1.26 (2026-10-25)

Built from current `main` of both repositories on 2026-10-25:
`nickdbendickson/nd_hazard` at `2183465` and `nickdbendickson/DWR_Plugins` at `418cc6f`.
CI was green on both heads (fast-checks, plugin-suites and the new model-tests job).

## Versions

**ND set** (the zips in this folder, `releases/1.26/`; `nd/` in the release bundle)

| Plugin | 1.25 | 1.26 |
|---|---|---|
| ND Dam Hazard Classification Helper | 2.34.0 | **2.34.4** |
| ND Breach Runner | 3.25.0 | **3.26.2** |
| ND Dam Screening | 0.32.0 | **0.32.4** |
| ND Data Updater | 1.7.0 | 1.7.0 (**new zip**) |
| shared `damsuite` | 1.18.0 | 1.18.0 |

The Data Updater keeps its version number, but its 1.26 zip is not the 1.25 one: the release
table in both of its `damsuite` copies (`versions.py`, and the `FILES.json` hash of it) changed,
nothing else. Install it with the other three (see also "Known differences and issues").

**DamSafety (DWR) set** (zips in `dwr/` of the release bundle, not in this repository; built from
the DWR_Plugins folders without `tests/`)

| Plugin | last bundle (2026-10-21, 1c2758f) | now |
|---|---|---|
| DamSafety Hazard | 2.18.56 | **2.18.61** |
| DamSafety Runner | 3.10.61 | **3.10.80** |
| DamSafety Screening | 0.17.40 | **0.17.45** |
| DamSafety Data | 1.4.4 | 1.4.4 |
| DamSafety Setup | 1.0.15 | 1.0.15 |
| DamSafety Hazard Lite | 0.1.0 | 0.1.0 |
| ND PMP Tool (prototype) | — | **0.3.0** (new) |

Install: QGIS > Plugins > Manage and Install Plugins > Install from ZIP. Keep the ND set and the
DamSafety set in **separate QGIS profiles**; they register the same Processing ids.

## Today's fixes (2026-10-25)

1. **Screening normal-pool area (ND Screening 0.32.3 / 0.32.4).** Tool 8 no longer writes the
   mapped normal-pool area to `dam.json` as the area at the top of dam (0.32.3, 2026-10-23).
   0.32.4 also removes a pool area an older screening left (a dam screened again on 0.32.3 kept
   it) and writes the stage-storage curve's top-of-dam area instead. On a dry flood-control dam
   the pool area forced the Runner's storage-area exponent to 1 and the peak breach flow came
   out about a quarter too low. Absaraka (DWR-2801): 19,939 → 26,379 cfs.
   The DWR Screening never wrote the pool area, so it needed no fix.
2. **LISFLOOD-FP gauge sections clipped to the grid (ND Runner 3.26.2, DWR Runner 3.10.80).**
   The virtual gauge line ran off the model grid and LISFLOOD-FP's own clip checks the wrong
   axis, so the reported gauge discharge could be garbage (1e297, or a negative shown as 0),
   depending on the build and the grid. Gauges come from three places:
   - **GAUGE_POINTS** (Run a dam, and the Monte Carlo runs that pass them on): only the gauge
     records (`gauge_*.csv`, the gauge plot and report figure) were affected.
   - **Cascade** ("Cascade failure of dams in a chain"): the `to_<next dam>` gauge's discharge
     is the wave the next dam is modelled under and decides whether it fails, so every dam
     after the first, and its flood, depends on it.
   - **Nested grid** ("Nested fine grid near the dam"): it always adds an `interface` gauge, and
     that gauge's discharge is the inflow of the coarse run downstream, so the flood below the
     interface (depth, velocity, arrival, hazard class) depends on it.

   The sensitivity cases and the near-dam refine run write no gauges. A run that used none of
   these (an ordinary Run a dam without gauge points) was never affected.
3. **Stale model tests fixed and run in CI (nd_hazard #16, DWR_Plugins #180).** Tests and CI
   only. A new `model-tests` CI job now runs the real LISFLOOD-FP and ANUGA model tests on every
   PR; that job is what found fix 2.

## Also in this release since 1.25 (merged earlier this week)

- **HEC-RAS import depth (ND Runner 3.26.1; DWR Runner 3.10.79).** HEC-RAS results imported by
  the Runner were too deep and too wide (each pixel got the depth of its HEC-RAS cell's lowest
  point). With the terrain given, depth, wet area and arrival are now per pixel.
  DWR-2801: area at 1 ft or more went from 5,060 to 1,923 ac (ANUGA 1,960 ac). Re-export older
  HEC-RAS results (`ndbreach_cli.py hecras-reexport`) and reclassify.
- **HEC-RAS plan import arrival (ND Hazard 2.34.4; DWR Hazard 2.18.61).** The ND Hazard
  Helper's own plan import already took depth from the terrain; 2.34.4 changes only its
  arrival: now per pixel (the cell's water-surface series minus the pixel's terrain,
  interpolated between output steps, else the time of the maximum) and only where the depth
  reaches the arrival depth (0.1 ft). A plan used directly as a scenario is converted again on
  its own (2.34.4 changed the plan-cache key). Rasters already written by the HEC-RAS tab's
  Import button or the "Import HEC-RAS plan results" tool keep the old arrival: import the plan
  again. DWR Hazard 2.18.61 puts its plan-import arrival on the same depth basis as the depth.
- **Lone 911 address points count as habitable again (ND Hazard, audit H1, no version bump).**
  An ND 911 address point with no building outline within the snap distance and no NSI record
  had been made Unverified since 2.20.1, and Unverified never raises the class, so a new rural
  house could be dropped and a dam that should be High read "LOW - no habitable structures".
  It now counts as habitable by the ND site address rule and is listed for review (as
  DamSafety Hazard does).
- **Optional "accept unpatched LISFLOOD-FP" setting (ND and DWR Runner, no version bump; off by
  default).** Processing > Options > Providers > ND Dam Breach Runner (DamSafety Runner in the
  DWR set). When on, a LISFLOOD-FP build without the arrival-depth patch runs, with a loud
  warning, instead of being refused; arrival times then mark the first wetting (~1 mm), not the
  arrival depth, so warning times are unreliable.
- **Model grid stays in the DEM's CRS (ND Runner 3.26.0)**, so the hazard model-extent check sees
  wet edges; the run report gives the share of breach volume that left through the model edges.
- **Model-extent check sees the Runner's own grid (Hazard 2.34.3).**
- **Suite chain step 2, cut-off history, shorter field-check sheet (Hazard 2.34.2).**
- **Tool dialogs stay in front (2.34.1 / 3.25.1 / 0.32.1)** and **DEM downstream bearing
  (3.25.2 / 0.32.2)**.
- ND Screening window: refreshing the dam table no longer fails under Qt6 / QGIS 4 (no version
  bump).
- DWR set: format-2 offline pack checked against its `manifest.json` (Hazard 2.18.60, Screening
  0.17.45, Runner 3.10.77), Windows HEC-RAS launcher fixes (3.10.78, **not tested on Windows**),
  Screening tool 1 field map and `dam.json` dam length (0.17.43 / 0.17.44).

## What to re-run

- Dry flood-control dams screened on ND Screening 0.32.2 or older (even if screened again on
  0.32.3) and then modelled from `dam.json`: re-screen (tool 8, 0.32.4) and re-run the Runner.
  Peak flows go up.
- HEC-RAS results imported by the Runner before ND 3.26.1 / DWR 3.10.79: re-export and
  reclassify. ND Hazard Helper: a plan used directly as a scenario is converted again on its
  own on 2.34.4, so just reclassify; rasters written before 2.34.4 by the HEC-RAS tab's Import
  button or the "Import HEC-RAS plan results" tool: import the plan again, then reclassify.
- Gauge fix: re-run on ND Runner 3.26.2 / DWR Runner 3.10.80 every **nested-grid** run,
  **cascade** run and run **with gauge points** made on ND 3.26.1 / DWR 3.10.79 or older. For
  nested and cascade runs the flood itself can change (below the interface, and at every dam
  after the first); for gauge points only the gauge records. Whether an old record is wrong
  depends on the LISFLOOD-FP build and the grid (on the nested model-test case the bundled Linux
  build and the built-in engine happened to give the right interface flow), so re-run them
  whatever build or engine was used. No other run needs a re-run for this fix.
- Dams classified on ND Hazard 2.34.0 or older (back to 2.20.1) with ND 911 address points that
  had no building and no NSI record (listed as Unverified, "911 address point with no building
  outline"): reclassify on 2.34.4. The class can go up.

## Known differences and issues

- **Arrival depth: ND Runner 1 ft, DWR Runner 0.1 ft.** ND Runner 3.26.2 still defaults the
  arrival depth (ARRIVAL_FT of Run a dam and of the HEC-RAS import tool) to 1 ft. DWR Runner
  3.10.80 and the ND Hazard Helper's own HEC-RAS plan import use 0.1 ft, the ND hazard wet
  depth. So for the same dam the ND Runner gives no arrival time to structures flooded 0.1 to
  1 ft deep (REG_05.2026 5.4(c)), and the area its run summary gives as "deeper than the
  arrival depth" is the area at 1 ft or more. Set ARRIVAL_FT to 0.1 to match the DWR Runner.
  ND Runner 3.26.3 (nd_hazard PR #19, not in this release) changes the default to 0.1 ft.
- **Data Updater readiness lights.** The Data Updater window's suite readiness lights and its
  set-up report read the release table of its `ndpack/damsuite` copy, which in the 1.26 zip
  still names Hazard 2.34.1, Runner 3.25.2 and Screening 0.32.2. There, Hazard 2.34.1-2.34.3,
  Runner 3.25.2-3.26.1 and Screening 0.32.2-0.32.3 show as OK instead of "... installed, ...
  is this release". The Hazard, Runner and Screening windows check against the 1.26 table and
  do flag them.

## Offline pack

**No new data pack is needed.** None of these fixes touch pack data or the pack format (the
pack code is unchanged since 1.25; only the version table moved). The current pack is
`pack-current/` in the release bundle, not in this repository (three zips, 670 MB, built
2026-10-12, format 2, 33 layers). It was checked against these plugins today:

- The ND reader (Hazard 2.34.4's `damsuite.gpkgpack`) opens all 33 layers in QGIS 3.34.4.
- The DamSafety pack check (`damsuite.packcheck`, Hazard 2.18.61): "OK - every layer matches
  manifest.json". Known gaps unchanged: OSM_lifelines partial; no substations, OSM crossings,
  trailer parks or older HPMS years (blocked hosts).

If you want the data itself refreshed (it is 13 days old), the Data Updater's pack source can
rebuild it; that is a separate, long download job.

## Testing

- CI on both `main` heads: fast-checks, plugin-suites and model-tests all green. Model tests:
  nd_hazard 532/0, DWR_Plugins 838/0.
- Installed from these zips into real QGIS 3.34.4 (one profile per set):
  - ND: all four load; Runner self-test with a model run **26 PASS / 0 FAIL / 1 SKIP** (skip =
    keep-awake, no system bus in the container); Screening self-test OK.
  - DWR: all load; Runner self-test **24 PASS / 1 FAIL / 1 SKIP** (LISFLOOD-FP model run passes;
    the ANUGA check fails only because this container's ANUGA env is a venv whose python is a
    symlink, and the DWR Runner resolves the symlink to the system python; a conda/micromamba
    env on Windows is not affected); Screening self-test OK.
