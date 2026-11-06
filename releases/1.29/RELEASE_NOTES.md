# ND Dam Suite — release 1.29 (2026-11-06)

Built from current `main` of `nickdbendickson/nd_hazard` at `65125d0`. Each zip's archive comment carries the
source commit (`unzip -z <zip>`). Tests are included, no caches. The DamSafety set is built from
`nickdbendickson/DWR_Plugins` `main` at `682a0f3` (see the bottom of this page).

## Versions

**ND set** (the zips in this folder, `releases/1.29/`)

| Plugin | 1.28 | 1.29 |
|---|---|---|
| ND Dam Hazard Classification Helper | 2.35.16 | 2.35.16 |
| ND Breach Runner | 3.28.1 | **3.29.0** |
| ND Dam Screening | 0.33.12 | 0.33.12 |
| ND Data Updater | 1.8.0 | 1.8.0 |

Only the Runner's code changed. The other three zips differ from 1.28 only in the shared version
table (`damsuite/versions.py`, `FILES.json`), which now lists Runner 3.29.0.

Install: QGIS → Plugins → Manage and Install Plugins → Install from ZIP. Keep the ND set and the
DamSafety set in **separate QGIS profiles**, because they register the same Processing ids.

## What's new since 1.28

### Runner: choose the LISFLOOD-FP solver (Runner 3.29.0, #69)
The main LISFLOOD-FP run can use **ACC** (the default, unchanged), **FV2** or **MWDG2**. Only
solvers that write the maximum velocity and honour the arrival-depth patch (`inittm_depth`) are
offered. Both FV2 and MWDG2 run with the slope limiter.
- Pick it with the advanced **LISFLOOD-FP solver** parameter (`SOLVER`: 0 Default, 1 ACC, 2 FV2,
  3 MWDG2) on the run, batch and cascade tools, or in the CLI with `model.lisflood_solver`.
- The default for every run comes from the `NDBREACH_LISFLOOD_SOLVER` environment variable, else
  the provider setting *Default LISFLOOD-FP solver*, else ACC.
- FV1 (LISFLOOD-FP 8.1 writes no maximum velocity or arrival time for it), DG2 (needs
  slope-coefficient DEMs) and unknown names are refused, and the run says why.
- Before the first FV2 or MWDG2 run, the Runner tests the LISFLOOD-FP build on a 20-minute
  synthetic channel. A build that writes no velocity or lacks the arrival-depth patch, or the
  built-in engine, stops the run. The Runner never falls back to another solver. The bundled 8.1
  build passes for both.
- The near-dam refine still uses FV1 internally and keeps the ACC velocities. The ACC stability
  re-run and the near-dam refine apply to ACC runs only.
- Caution: on DWR-2801 the FV2 and MWDG2 maximum velocities read well below DSS-WISE, while
  depth and extent are close to ACC. Treat their velocities as indicative until a velocity study
  is done.

### Runner: optional finer ANUGA terrain (Runner 3.28.2, #61)
New setting **ANUGA: terrain cell size** (`anuga_cell_m`), off by default. When it is finer than
the model cell, ANUGA samples mesh elevations from the DEM at that resolution, for example 1–2 m
from the statewide VRT. Embankment crests then survive under the mesh, while the model grid,
the outputs and LISFLOOD-FP stay at the model cell. The fine DEM is read only over the flooded
footprint plus the mesh margin, so a whole-domain fine array is never held in memory. On DWR-2801,
a 10 m grid with 1–2 m terrain along the embankments pulls the over-spread in toward a 5 m run.

## Checks
- GitHub CI (fast-checks, plugin-suites, model-tests) green on #61 and #69 before merge.
- Runner fast suite on merged main: 633 tests, 0 failed. Helper suite: 144 tests, 0 failed.
- New model test: the bundled LISFLOOD-FP passes the FV2 and MWDG2 check, and ACC, FV2 and MWDG2
  runs of the synthetic dam all write depth and velocity.
- `tools/ci_checks.py` and `sync_damsuite --check` pass on both repositories.
- No new real-QGIS self-test for this release. The 1.28 self-test (ndbreach 26 PASS / 0 FAIL)
  covers the unchanged plugins.

## DamSafety (DWR) set
Built from `DWR_Plugins` `main` at `682a0f3` without `tests/`. The zips are not committed here.

| Plugin | 1.28 | 1.29 |
|---|---|---|
| DamSafety Hazard | 2.23.8 | 2.23.8 |
| DamSafety Runner | 3.15.1 | **3.16.0** |
| DamSafety Screening | 0.18.11 | 0.18.11 |
| DamSafety Data | 1.7.0 | **1.9.0** |
| DamSafety Setup | 1.0.20 | 1.0.20 |
| DamSafety Hazard Lite | 0.1.4 | 0.1.4 |
| ND PMP Tool (prototype) | 0.4.1 | 0.4.1 |

- **Runner 3.16.0 (#280):** the same solver choice and rules as ND Runner 3.29.0.
- **Data 1.8.0 (#281):** the offline pack builder gets the USGS 3DHP flowlines and waterbodies
  from the National Map 3DHP national file when `hydro.nationalmap.gov` answers 502/504. It reads
  the file from a `3dhp/` folder (the suite data folder's or one beside the pack), or downloads it
  once (about 12.5 GB). `NDSUITE_3DHP_DOWNLOAD=0` turns the download off.
- **Data 1.9.0 (#282):** the same for the OpenStreetMap layers. When Overpass is down
  (429/502/504), they are built from the Geofabrik North Dakota extract (about 200 MB) into
  `osm_layers/`, the folder the ND Data Updater uses. `NDSUITE_OSM_GEOFABRIK=0` turns it off.

## Notes
- A session cannot create a GitHub Release or tag. Tag `v1.29` at this release's merge commit
  and publish from a workstation (steps in the project library, `release-1.29/TAG-STEPS.md`).
