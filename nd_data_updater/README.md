# ND Data Updater (QGIS 3.28 plugin)

Keeps an offline copy of North Dakota structure and building-footprint data on
your Mac and downloads a new copy only when a source actually changes.

## Install

1. In QGIS: **Plugins > Manage and Install Plugins… > Install from ZIP**.
2. Choose `nd_data_updater.zip` and click **Install Plugin**.
3. The plugin appears under **Plugins > ND Data Updater** and as a toolbar button.

## Use

- **Check for Data Updates Now** (toolbar button): checks every dataset that is
  turned on. The first run downloads everything; later runs download only what
  changed. Progress shows in the task bar at the bottom of the QGIS window and
  can be cancelled there.
- **Add North Dakota Layers to Project**: adds the downloaded layers to a
  "North Dakota data" group.
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
| nsi-generator source code (`USACE-NSI/nsi-generator`, main branch) | off | `nsi_generator_code/` | Latest commit |

The NSI counties are merged into one GeoPackage, `nsi/nsi_nd.gpkg`, using the
GDAL bundled with QGIS.

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
check for each dataset.

## Notes

- Network access uses QGIS's own settings (**Settings > Options > Network**),
  including any proxy.
- A full first download is roughly 0.5-1 GB.
- The nsi-generator code expects USA Structures as per-county shapefiles; FEMA
  publishes a statewide geodatabase, so convert it before feeding it to that tool.
