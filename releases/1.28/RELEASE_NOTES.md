# ND Dam Suite — release 1.28 (2026-11-06)

Built from current `main` of `nickdbendickson/nd_hazard` at `a8f53ed` (CI green on that head:
fast-checks, plugin-suites, model-tests). Each zip's archive comment carries the source commit
(`unzip -z <zip>`). Tests are included, no caches. The DamSafety set is built from
`nickdbendickson/DWR_Plugins` `main` at `792b793` (see the bottom of this page).

## Versions

**ND set** (the zips in this folder, `releases/1.28/`)

| Plugin | 1.27 | 1.28 |
|---|---|---|
| ND Dam Hazard Classification Helper | 2.35.2 | **2.35.16** |
| ND Breach Runner | 3.27.1 | **3.28.0** |
| ND Dam Screening | 0.33.2 | **0.33.12** |
| ND Data Updater | 1.7.0 | **1.8.0** |

Install: QGIS → Plugins → Manage and Install Plugins → Install from ZIP. Keep the ND set and the
DamSafety set in **separate QGIS profiles**; they register the same Processing ids.

## What's new since 1.27

### Runner: crest burn and an automatic finer grid, on by default (Runner 3.28.0, #59)
Before the model runs, every grid cell crossed by a road or railway is raised to the highest
1 m ground in that cell, so embankments that hold water back survive on a 10–20 m grid. The 1 m
source is the statewide VRT in the suite data folder when it covers the dam, else USGS 3DEP 1 m
(tiles retried on failure). Guards skip the traced main channel, bridges and implausible raises.
When a run still shows coarse-grid over-spread (cell > 10 m, flood ≥ 1,000 ac, median wet depth
≤ 1 ft) the Runner reruns the same plan at 10 m and keeps that as the record; the coarse report
is kept beside it. The 5 m check run is **off by default** (set the check cell to 5 to opt in).
Absaraka (DWR-2801), area > 0.5 ft vs DSS-WISE 2,141 ac: 10 m + burn 2,151 ac, CSI 0.52 (was
3,218 ac / 0.42 without the burn). Toggles: `CREST_BURN`, `CREST_BURN_DEM`, `GRID_REFINE`,
`GRID_CHECK_CELL` (window check boxes and CLI keys).

### Runner: grid default and advisory on flat plains (3.27.7, 3.27.8, #55, #56)
On flat section-road plains the Runner starts at 10 m and offers 5 m; a pre-run warning fires
when the cell is coarser than 10 m there. After the run a `grid_spread_check` advisory flags
likely coarse-grid over-spread.

### One offline pack builder and one data updater for both suites (#60, #62–#65)
The ND Data Updater and DamSafety Data now share one engine (`damsuite/packengine.py`,
`updatecore.py`, `updatesources.py`) and one pack layer list. Results do not change.
- Pack gains USGS stream crossings, the ABI barrier layers (`ABI_dams`,
  `ABI_small_barriers_surveyed`, `ABI_road_crossings_unsurveyed`; the old `SARP_*` names are still
  read) and the ND DEQ layers.
- Suite map services are added as QGIS Browser connections at start (34 services).
- Updater 1.8.0: ND 911 address points (on) and Overture buildings (off) join the source list;
  a second concurrent update is refused; the structures record is now `structures_manifest.json`
  (the old `manifest.json` is read once).

### Hazard and rules
- US / State highways rate at least Medium (REG_05.2026 5.2.1.2(b)) in the Runner, Screening and
  the Helper criteria row (Helper 2.35.4, #45).
- DV = peak depth × peak wet-cell velocity, None when velocity is unknown (Helper 2.35.5, #46).
- DSS-WISE rasters named `Depth_ft` / `Velocity_ftps` are recognised automatically (2.35.3, #44).
- REG_05.2026 Fig 5.1/5.2 chart colours match the ND standard (2.35.11, #58).

### GUI, wording and housekeeping
Windows and Processing panels fit 800 × 600 and suite home pages scroll at 640 × 480 (#50, #52,
#53); updater auto-check is off; wording synced with DamSafety (#48); lint cleanup and a simpler
Screening storage confidence (#49); QGIS plugin-upload security-scan blockers cleared (#47);
Helper `datum_lines` import fix (#51); tests kept out of the real QGIS profile (#54).

### Not in this release
nd_hazard #61 (optional finer ANUGA terrain cell, `anuga_cell_m`) is still open and is not
included.

## Checks
- `tools/ci_checks.py` and `sync_damsuite --check` pass on both repositories.
- Real QGIS 3.34 self-test of these zips: ND `ndbreach:selftest` 26 PASS / 0 FAIL / 1 SKIP
  (keep-awake, container only), `nddam:selftest` OK.
- DamSafety set (own profile): `ndbreach:selftest` 24 PASS / 1 FAIL / 1 SKIP, the FAIL being the
  container's ANUGA venv (known, not a plugin fault, same as 1.26); `nddam:selftest` (LISFLOOD) OK.

## DamSafety (DWR) set
Built from `DWR_Plugins` `main` at `792b793` without `tests/`; not committed here. Last bundled in
1.26.

| Plugin | 1.26 | 1.28 |
|---|---|---|
| DamSafety Hazard | 2.18.61 | **2.23.8** |
| DamSafety Runner | 3.10.80 | **3.15.0** |
| DamSafety Screening | 0.17.45 | **0.18.11** |
| DamSafety Data | 1.4.4 | **1.7.0** |
| DamSafety Setup | 1.0.15 | **1.0.20** |
| DamSafety Hazard Lite | 0.1.0 | **0.1.4** |
| ND PMP Tool (prototype) | 0.3.0 | **0.4.1** |

Recent DamSafety work: the same crest burn + finer grid defaults (Runner 3.15.0, #270, also ports
the grid-spread advisory), the shared pack builder / layer list / updater (#271–#275), and
**DamSafety Data 1.7.0 (#276): the offline pack it built is kept up to date** (on by default, at
most once a day: missing layers are built and layers older than 30 days fetched again, both pack
formats; it never starts a new pack). Also: OpenStreetMap culverts and guessed culvert boxes
(Runner 3.14.0/3.14.1), judgment zone ND Medium by rule in Hazard Lite, Screening DEM download
when the PROJ grid server is unreachable (0.18.7), GUI fit at 800 × 600.

## Notes
- No version bump in this release; every `metadata.txt` and the `damsuite` release table already
  agree on `main`.
- A session cannot create a GitHub Release or tag. Tag `v1.28` and publish from a workstation if a
  published release is wanted.
