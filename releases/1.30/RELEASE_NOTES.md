# ND Dam Suite — release 1.30 (2026-11-06)

This release is built from the current `main` of `nickdbendickson/nd_hazard` at `dcd6795`. Each zip's archive comment records the
source commit (`unzip -z <zip>`). The zips include the tests and no caches. The DamSafety set is built from
`nickdbendickson/DWR_Plugins` `main` at `1cd757b` (see the bottom of this page).

## Versions

**ND set** (the zips in this folder, `releases/1.30/`)

| Plugin | 1.29 | 1.30 |
|---|---|---|
| ND Dam Hazard Classification Helper | 2.35.16 | **2.35.17** |
| ND Breach Runner | 3.29.0 | **3.29.3** |
| ND Dam Screening | 0.33.12 | **0.33.13** |
| ND Data Updater | 1.8.0 | **1.8.1** |

All four plugins carry the bounded settings-workbook reader (shared `damsuite`, pin
`damsuite-pin-v7`). The Runner also gets the HEC-RAS breach and breach-volume changes and the
plain errors for bad input files.

To install, go to QGIS → Plugins → Manage and Install Plugins → Install from ZIP. Keep the ND set and the
DamSafety set in **separate QGIS profiles**, because they register the same Processing ids.

## What's new since 1.29

### Security audit pass 3: bad input gives a plain message (all plugins, #73)
- **Settings workbook (medium).** The shared `.xlsx` reader is now bounded. Before, a 704-byte
  `damsuite_settings.xlsx` that named a cell at row 50,000,000 made the reader build a
  50-million-row table, which froze QGIS. A sheet past Excel's limits (1,048,576 rows, 16,384 columns), a
  table over 2,000,000 cells or a part that unpacks to more than 64 MB is now refused
  before anything large is built. The error reads "the settings file … cannot be read".
- **Runner, fatality / damage-curve CSV.** A row with more values than the header has columns
  used to stop the consequences tool with a raw `AttributeError`. The message now names the file, the
  line and the column counts.
- **Runner, animation.** A damaged or cut-short `depth_snapshots.npz` gives "damaged or
  incomplete, run the breach model again" instead of a numpy or zip error. The file is checked
  before any frame is read.
- **Runner, built-in engine.** A truncated DEM, Manning's n, start, `.bci`, `.bdy` or gauge file
  stops the run with "built-in engine: the … file is truncated or damaged" and names the file.

### Runner: breach volume and the HEC-RAS breach (3.29.1 #71, 3.29.2 #72)
- The breach equations (Froehlich 2008 / 1995, MLM, VTG, TR-60 and the Froehlich 1995 peak check) now use
  **Vw = the dam's maximum storage** (DWR `MAX_STORAGE`), not the storage at the starting pool.
  The elevation-volume curve is only a check, with a warning when it is more than 5 % off, and it still sets the
  routed reservoir. **Breach peaks and widths change for runs that start below the crest.** Runs that start at the
  crest are unchanged. The report lists `vw_acft`.
- The HEC-RAS plan now defaults to **piping** and always uses Froehlich (2008) piping values
  (K0 1.0, side slope 0.7) for the bottom width, side slopes and formation time.
  The opt-in `breach_f2008 = 'fill_missing'` option keeps the run's own values. The piping coefficient (0.5) and the
  initial piping elevation are always written. Only an explicit overtopping failure mode writes
  an overtopping breach.

## Checks
- GitHub CI (fast-checks, plugin-suites with real QGIS, model-tests) was green on #71, #72 and #73
  before merge.
- The 7 new input-hardening tests (`tests/test_input_hardening.py`) fail on 1.29 code and pass here.
- `tools/ci_checks.py` and `sync_damsuite --check` pass on both repositories.
- No new real-QGIS self-test was run for this release.

## DamSafety (DWR) set
Built from `DWR_Plugins` `main` at `1cd757b`, without `tests/`. These zips are not committed to this repository.

| Plugin | 1.29 | 1.30 |
|---|---|---|
| DamSafety Hazard | 2.23.8 | **2.23.9** |
| DamSafety Runner | 3.16.0 | **3.16.3** |
| DamSafety Screening | 0.18.11 | **0.18.12** |
| DamSafety Data | 1.9.0 | **1.9.1** |
| DamSafety Setup | 1.0.20 | 1.0.20 |
| DamSafety Hazard Lite | 0.1.4 | 0.1.4 |
| ND PMP Tool (prototype) | 0.4.1 | 0.4.1 |

The DamSafety set has the same changes as the ND set: Runner 3.16.1 (#283), 3.16.2 (#284) and 3.16.3
(#285, the security fixes and the bounded reader in Hazard, Screening and Data).

## Notes
- A session cannot create a GitHub Release or tag. Tag `v1.30` at this release's merge commit,
  and tag `damsuite-pin-v7` on DWR_Plugins at `1cd757b`, from a workstation.
