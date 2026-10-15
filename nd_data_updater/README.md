# ND Data Updater (QGIS 3.28 plugin)

Keeps an offline copy of North Dakota structure and building-footprint data on
your computer and downloads a new copy only when a source actually changes. Since 1.3.0 it
also builds and keeps up to date the statewide **offline data pack** of the ND dam plugins
(Hazard Helper, Breach Runner, Breach Screening).

## Install

1. In QGIS: **Plugins > Manage and Install Plugins… > Install from ZIP**.
2. Choose `nd_data_updater.zip` and click **Install Plugin**.
3. The plugin appears under **Plugins > ND Data Updater** and as a toolbar button.

## Use

- **Check for Data Updates Now** (toolbar button): checks every dataset that is
  turned on. The first run downloads everything; later runs download only what
  changed. It runs in the background: the layer it is on is shown in the status bar
  ("layer 12 of 29: USACE National Structure Inventory") and in the plugin's windows, and it can
  be stopped from the task bar, from either window, or from **Stop the Running Update**. Every
  run ends with a short block in plain words: how many layers were built, and what was not
  fetched with what that costs.
- **Add North Dakota Layers to Project**: adds the downloaded layers (and the pack's 3DHP and SARP
  layers) to a "North Dakota data" group.
- **Offline Pack and Data Folder…** (1.4.0): the pack layer by layer (vintage, feature count,
  **Fetch again**, **Replace with my file…** - a layer holding your own file is left alone by
  every refresh until you press **Fetch again** on it), **Rebuild only missing layers**, **Copy pack to
  USB…** / **Import pack…**, the **Pack change report**, the **Data vintage** list, the suite
  readiness lights, one row per drop-in folder with what goes in it, and a **service check**.
- **Open Data Folder**: opens the folder in Finder.
- **Settings and Status…**: data folder, schedule, which datasets to keep, and
  when each was last updated and checked.

Data is stored in `~/Documents/ND Structure Data` unless you choose another folder.

## Datasets

| Dataset | Default | Folder | How changes are detected |
|---|---|---|---|
| National Structure Inventory (all 53 counties, NSI 2026 and 2022 merged, from the NSI API) | on | `nsi/` | Re-downloaded every 30 days (adjustable); replaced only if the structures changed |
| Microsoft building footprints (`NorthDakota.geojson`) | on | `microsoft_footprints/` | Server ETag / Last-Modified. Microsoft no longer updates this legacy release |
| OpenStreetMap from Geofabrik (GeoPackage; only the buildings layer is added to the project) | on | `osm/` | Geofabrik's published MD5; the download is verified against it |
| USA Structures (FEMA/ORNL) | off | `usa_structures/` | Needs a download link pasted into Settings. ETag / Last-Modified |
| OpenStreetMap pack layers (Geofabrik `north-dakota-latest.osm.pbf`, every tag; five GeoPackages: `OSM_lifelines`, `OSM_crossings`, `OSM_campgrounds`, `OSM_trailer_parks`, `OSM_places`) | on | `osm_layers/` | Geofabrik's published MD5; the .pbf is deleted after processing |
| **Offline pack for the ND dam plugins** (every layer the three dam plugins read) | on | `ND_Dam_Suite_Pack/` | The pack's own manifest: a layer that is missing or incomplete is built, a layer older than the refresh interval (default 30 days) is fetched again |
| nsi-generator source code (`USACE-NSI/nsi-generator`, main branch) | off | `nsi_generator_code/` | Latest commit |

The NSI counties are merged into one GeoPackage, `nsi/nsi_nd.gpkg`, using the
GDAL bundled with QGIS.

### OpenStreetMap pack layers

The plain OpenStreetMap download above is Geofabrik's ready-made GeoPackage, which keeps only
a fixed set of themes with a few columns. The dam suite needs features that file leaves out
(culverts, fords, low-water crossings, mobile-home parks, substations), so this dataset
downloads the full `.osm.pbf` instead and picks out the features with the same tag rules the
ND Dam Hazard Helper uses. Processing takes several minutes after the download. The result is
five GeoPackages in `osm_layers/` (EPSG:4326, layer named like the file) that go straight into
the offline pack's `lifelines/`, `roads/` and `structures/` folders. `osm_layers/osm_layers.json`
lists the counts and the tag rules.

### The offline pack (1.3.0)

**Check for Data Updates Now** builds `<data folder>/ND_Dam_Suite_Pack/`: one GeoPackage per
layer, `manifest.json` and `LAYERS.txt` (what each layer is and where it came from). The first
run takes an hour or more; a stopped run goes on where it stopped (finished layers are kept).
Layers:

- structures: NSI (2026), NDGISHUB address points, USA Structures; roads and traffic: NG911
  roads, county roads, HPMS, NDDOT traffic counts, NBI bridges; rail: NARN lines, FRA crossings
- lifelines: hospitals, nursing and care homes, EIA power plants, OpenStreetMap lifelines,
  crossings, campgrounds, trailer parks, places
- hydrography: NID dams, **USGS 3DHP flowlines and waterbodies** (they replace the NHD layers)
- **SARP aquatic barriers**: the barrier dams and the surveyed road-stream crossings from the
  Southeast Aquatic Resources Partnership's public services (the data behind
  tool.aquaticbarriers.org). The USGS database of unsurveyed crossings is not on a public
  service and is not in the pack.
- DWR layers (dams, dam lines, dugouts, dugout polygons, unknown structures) and HIFLD layers
  (fire / EMS stations, emergency operations centers, law enforcement, wastewater plants) come
  from files you put in `<data folder>/dwr/` and `<data folder>/hifld/` (the DWR dams come from
  DWR's public service when there is no file). NLCD 2021 land cover (a GeoPackage raster) and
  the counties.

**Settings > Offline pack**: refresh interval; where 3DHP comes from and how long the service
may take to answer; make the ND dam plugins read this pack; fetch every layer again (once).
Choosing the data folder (in either window) now sets it for the three dam plugins as well - the
suite data folder, the pack location, the data source and the Dams folder together - so a folder
chosen here is the folder they read. Before 1.4.0 those were separate settings and a user who
set only this one got plugins looking in an empty folder.

**3DHP.** The USGS 3DHP web service often answers HTTP 504 or 502 to a state-sized request. In
the default mode the updater asks it a small question *and* the statewide question a build really
makes (count only), with a time limit of **Give the 3DHP service at most** (three minutes by
default). If it has not answered both in that time, the National
Map's 3DHP national file (FileGDB zip, about 12 GB; the newest CONUS file is found through The
National Map's product list) is downloaded once into `<data folder>/3dhp/` (it continues if the
connection drops; it uses Python's own network access, not QGIS's proxy settings) and the flowlines and waterbodies are cut out of it, which takes
a few minutes. You can also put that file in `3dhp/` yourself. The NHD-style fields
(`gnis_name`, `gnis_id`, `ftype`, `fcode`) are added to the 3DHP fields, so the dam plugins work
as before.

The pack's five OpenStreetMap layers come from the OpenStreetMap pack layers dataset above
(`osm_layers/`) when it has built them; without those files the pack builder asks Overpass,
which often times out. The pack builder is a copy of the ND Dam Hazard Helper's (`ndpack/`); the NSI layer in the pack is
the builder's own download (NSI 2026), separate from the merged `nsi/` copy above.

### NSI versions

The NSI API serves each release at its own address (`/nsiapi/nsi2026/...`,
`/nsiapi/nsi2022/...`); its unversioned address follows whatever USACE makes
the default, which became NSI 2026 in 2026. The plugin always asks for a named
version. **Settings > NSI version** chooses:

- **2026 and 2022 merged** (default). NSI point ids are not carried between
  versions, so structures are matched by location:
  - Every 2026 point is kept with its 2026 data.
  - A 2022 point with a 2026 point within the match distance (default 30 m) is
    the same structure, and the 2026 data wins. It is folded into the 2026
    point that matches best (nearest, preferring the same occupancy category:
    a 2022 house prefers the 2026 house over a slightly closer barn).
  - A 2022 point with no 2026 point nearby is kept with its 2022 data. Some are
    demolished or misplaced; keeping them is the conservative choice.
- **2026 only** or **2022 only**.

Extra fields on every point:

| Field | Meaning |
|---|---|
| `nsi_version` | `2026`, or `2022` for a structure only NSI 2022 has |
| `nsi22_fd_id`, `nsi22_occtype`, `nsi22_bldgtype` | The best-matching 2022 point (for a 2022-only point, its own values) |
| `nsi22_match_m` | Distance to that 2022 point, metres |
| `nsi22_count` | How many 2022 points were folded into this point (2022 often had several unit points on one apartment building) |
| `nsi22_mobile` | 1 if any folded 2022 point was a mobile home (occtype RES2 or bldgtype H). NSI 2026 dropped many of these. |

The one renamed field, 2022 `ground_elv_meters`, is written as the 2026
`grnd_elv_m`. `nsi/nsi_summary.json` lists point counts per county and in total.
Changing the version or the match distance rebuilds the NSI copy at the next check.

## When it checks

- About 20 seconds after QGIS starts, then hourly while QGIS is open, but never
  more often than **Check at most every** (default 24 hours).
- Automatic checks stay quiet unless something was downloaded or went wrong.
- Nothing runs while QGIS is closed.

## Offline behaviour

The plugin always works from the local copies. If there is no connection, the
check reports "offline" and nothing is changed. Updates are downloaded to a
hidden `.staging` folder, verified (checksum, complete size, valid zip), and only
then swapped into place, so an interrupted download never damages the data you
have. Layers already in your project are reloaded after an update.

`manifest.json` in the data folder records the version, last update and last
check for each dataset. A run that is killed (QGIS force-quit, a restart) leaves
`.update.lock` behind; since 1.4.0 the lock records the process that made it, and a lock whose
process is gone is cleared automatically with one line in the log, instead of refusing every
later run.

## Carrying the pack to a field laptop (1.4.0)

**Copy pack to USB…** copies `ND_Dam_Suite_Pack/` to a drive and writes `CHECKSUMS.sha256`
beside the data (the `sha256sum` format). **Import pack…** on the laptop checks every file
against that list *before* anything is replaced - a stick pulled out mid-write leaves a
GeoPackage that opens and returns half a county, which is worse than no pack - and then adopts
the folder, so the three dam plugins read it. A pack with no checksum file can still be
imported, with a warning.

## Your own file instead of a downloaded layer (1.4.0)

**Replace with my file…** writes your file into the pack as that layer and records it in
`manifest.json` as yours, with who put it in and when. From then on no refresh touches that
layer - not the ordinary one, not "fetch every layer again", not "rebuild only missing layers" -
however old it gets, because your county roads file is presumably better than the service's and
fetching over it silently would throw that away. Every run's summary says so ("Left alone (your
own file): county roads"). **Fetch again** on that layer is the one thing that overwrites it:
that run downloads the agency's version and puts the layer back in the normal refresh cycle.

## The pack change report (1.4.0)

Before a refresh overwrites `manifest.json`, a copy is kept as `manifest.previous.json` in the
pack. After the refresh the two are compared: new structures, new addresses, new roads, and -
the part that matters - which dams in the Dams folder already have a hazard class that one of
the changed layers could move ("re-classify" or, for terrain and land cover, "re-run the
model"). The report is in the log and in the pack window, where it can be saved next to the
pack. **Data vintage** gives layer, source, download date and feature count for stamping a
report with how old its data was.

## Are the services up? (1.4.0)

The **Services** tab asks NSI, 3DHP (the small and the statewide question), NID, FEMA, the DWR
service and MRLC one small question each, in parallel, with a six-second timeout, and reports
reachable / slow / down. The point is to find out before a 90-minute run that would hang. It can
also run when the window opens.

## Tests

    cd <folder that holds nd_data_updater>
    /usr/bin/python3.12 -m nd_data_updater.tests.run_tests

64 tests, no QGIS and no network needed: the lock decision, the 3DHP cap, the progress line, the
plain-words summary, the change report, the USB checksums, the data vintage and the build plan.

## Notes

- Network access uses QGIS's own settings (**Settings > Options > Network**),
  including any proxy.
- A full first download is roughly 0.5-1 GB without the offline pack; the pack is about 1.6 GB,
  plus the 3DHP national file (about 12 GB) when the 3DHP web service is down.
- The nsi-generator code expects USA Structures as per-county shapefiles; FEMA
  publishes a statewide geodatabase, so convert it before feeding it to that tool.
