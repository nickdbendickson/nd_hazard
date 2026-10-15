# ND Dam Suite — release 1.24 (2026-10-15)

| Plugin | Version |
|---|---|
| ND Dam Hazard Classification Helper | 2.32.0 |
| ND Breach Runner | 3.22.0 |
| ND Dam Screening | 0.30.0 |
| ND Data Updater | 1.6.0 |
| shared `damsuite` package | 1.18.0 |

Install the zips in this folder with QGIS > Plugins > Manage and Install Plugins > Install from ZIP,
or copy the matching source folder at the repo root into your QGIS profile's `python/plugins/`.

## What's new

**New tool — Spillway adequacy (Breach Runner).** Answers "does the dam pass its Inflow Design
Flood?" Each inflow hydrograph is routed through the reservoir with **no breach** — starting pool,
spillway rating, optional outlets / gate, and flow over the embankment crest — and the peak pool is
checked against the top of dam minus the required residual freeboard (ND REG_05.2026 §8.1.1).
Bisection finds the largest inflow multiple the dam passes (up to 2×); the highest-pool storm
governs. Writes `adequacy.csv`, `adequacy.md`, and a pool-vs-time plot. Screening aid only — an
engineer reviews the inputs and results.

## Fixes

- **Breach inflow point (correctness).** The breach inflow could be placed on the upstream
  reservoir floor instead of the downstream channel — most significant for dry flood-control dams.
  The inflow is now placed downstream of the dam.
- **Shared settings written safely.** The suite settings file was rewritten in place with no lock,
  so two plugins saving at once could lose each other's keys. Saves are now atomic, keep a backup,
  merge concurrent writers, and take an exclusive lock.
- **Updater hygiene.** The OSM / Geofabrik download no longer fails the whole update when the
  `.md5` checksum is missing (falls back to ETag / Last-Modified), and the NSI generator source is
  pinned to a fixed commit.

## Testing

Unit tests show no new failures versus a clean 1.23 baseline across all four plugins. All four
plugins load at the correct versions in QGIS 3.34.4 and their selftests pass. The spillway-adequacy
tool was run end-to-end in QGIS on a synthetic PMF hydrograph and spillway rating — both the pass
and overtopping branches behave correctly.
