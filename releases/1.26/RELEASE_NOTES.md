# ND Dam Suite — release 1.26 (2026-10-25)

Built from current `main` of both repositories on 2026-10-25:
`nickdbendickson/nd_hazard` at `2183465` and `nickdbendickson/DWR_Plugins` at `418cc6f`.
CI was green on both heads (fast-checks, plugin-suites and the new model-tests job).

## Versions

**ND set** (zips in `nd/`, also in the GitHub repo under `releases/1.26/`)

| Plugin | 1.25 | 1.26 |
|---|---|---|
| ND Dam Hazard Classification Helper | 2.34.0 | **2.34.4** |
| ND Breach Runner | 3.25.0 | **3.26.2** |
| ND Dam Screening | 0.32.0 | **0.32.4** |
| ND Data Updater | 1.7.0 | 1.7.0 |
| shared `damsuite` | 1.18.0 | 1.18.0 |

**DamSafety (DWR) set** (zips in `dwr/`, built from the repo folders without `tests/`)

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
   mapped normal-pool area to `dam.json` as the area at the top of dam, removes a stale one an
   older screening left, and writes the stage-storage curve's top-of-dam area instead. On a dry
   flood-control dam the pool area forced the Runner's storage-area exponent to 1 and the peak
   breach flow came out about a third too low. Absaraka (DWR-2801): 19,939 → 26,379 cfs.
   The DWR Screening never wrote the pool area, so it needed no fix.
2. **LISFLOOD-FP gauge sections clipped to the grid (ND Runner 3.26.2, DWR Runner 3.10.80).**
   The virtual gauge line ran off the model grid and LISFLOOD-FP's own clip checks the wrong
   axis, so the reported gauge discharge could be garbage (1e297, or a negative shown as 0).
   Gauges are written only when GAUGE_POINTS is given or by the cascade tool, so ordinary runs
   (depth, velocity, arrival, peak flow, hazard class) were never affected.
3. **Stale model tests fixed and run in CI (nd_hazard #16, DWR_Plugins #180).** Tests and CI
   only. A new `model-tests` CI job now runs the real LISFLOOD-FP and ANUGA model tests on every
   PR; that job is what found fix 2.

## Also in this release since 1.25 (merged earlier this week)

- **HEC-RAS import depth (ND Runner 3.26.1, Hazard 2.34.4; DWR Runner 3.10.79, Hazard 2.18.61).**
  Imported HEC-RAS results were too deep and too wide (each pixel got the depth of its HEC-RAS
  cell's lowest point). With the terrain given, depth, wet area and arrival are now per pixel.
  DWR-2801: area at 1 ft or more went from 5,060 to 1,923 ac (ANUGA 1,960 ac). Re-export older
  HEC-RAS results (`ndbreach_cli.py hecras-reexport`) and reclassify.
- **Model grid stays in the DEM's CRS (ND Runner 3.26.0)**, so the hazard model-extent check sees
  wet edges; the run report gives the share of breach volume that left through the model edges.
- **Model-extent check sees the Runner's own grid (Hazard 2.34.3).**
- **Suite chain step 2, cut-off history, shorter field-check sheet (Hazard 2.34.2).**
- **Tool dialogs stay in front (2.34.1 / 3.25.1 / 0.32.1)** and **DEM downstream bearing
  (3.25.2 / 0.32.2)**.
- DWR set: format-2 offline pack checked against its `manifest.json` (Hazard 2.18.60, Screening
  0.17.45, Runner 3.10.77), Windows HEC-RAS launcher fixes (3.10.78, **not tested on Windows**),
  Screening tool 1 field map and `dam.json` dam length (0.17.43 / 0.17.44).

## What to re-run

- Dry flood-control dams screened on ND Screening 0.32.2 or older and then modelled: re-screen
  (tool 8) and re-run the Runner. Peak flows go up.
- Any HEC-RAS results imported before today: re-export and reclassify.
- Only runs with gauge points, or cascade runs, need a re-run for the gauge fix.

## Offline pack

**No new data pack is needed.** None of these fixes touch pack data or the pack format (the
pack code is unchanged since 1.25; only the version table moved). The current pack is
`pack-current/` (three zips, 670 MB, built 2026-10-12, format 2, 33 layers). It was checked
against these plugins today:

- The ND reader (Hazard 2.34.4's `damsuite.gpkgpack`) opens all 33 layers in QGIS 3.34.4.
- The DamSafety pack check (`damsuite.packcheck`, Hazard 2.18.61): "OK - every layer matches
  manifest.json". Known gaps unchanged: OSM_lifelines partial; no substations, OSM crossings,
  trailer parks or older HPMS years (blocked hosts).

If you want the data itself refreshed (it is 13 days old), the Data Updater's pack source can
rebuild it; that is a separate, long download job.

## Testing

- CI on both `main` heads: fast-checks, plugin-suites and model-tests all green. Model tests:
  nd_hazard 531/0, DWR_Plugins 837/0.
- Installed from these zips into real QGIS 3.34.4 (one profile per set):
  - ND: all four load; Runner self-test with a model run **26 PASS / 0 FAIL / 1 SKIP** (skip =
    keep-awake, no system bus in the container); Screening self-test OK.
  - DWR: all load; Runner self-test **24 PASS / 1 FAIL / 1 SKIP** (LISFLOOD-FP model run passes;
    the ANUGA check fails only because this container's ANUGA env is a venv whose python is a
    symlink, and the DWR Runner resolves the symlink to the system python; a conda/micromamba
    env on Windows is not affected); Screening self-test OK.
