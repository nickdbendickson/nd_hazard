# ND Dam Suite — release 1.25 (2026-10-20)

| Plugin | Version |
|---|---|
| ND Dam Hazard Classification Helper | 2.34.0 |
| ND Breach Runner | 3.25.0 |
| ND Dam Screening | 0.32.0 |
| ND Data Updater | 1.7.0 |
| shared `damsuite` package | 1.18.0 |

Install the zips in this folder with QGIS > Plugins > Manage and Install Plugins > Install from ZIP,
or copy the matching source folder at the repo root into your QGIS profile's `python/plugins/`.

## What's new

A GUI pass across the whole suite, so every window follows one visual grammar (`docs/gui-grammar.md`).
No tool, parameter, default or result changed — this release is the interface only.

**One grammar, shared.** The window building blocks now live in a single `shared/ui/` package that
is copied byte-identical into each plugin, with a CI check (`tools/gui_lint.py`) that opens every
window in QGIS and holds it to the grammar: titles, colours from one token file, one primary button,
tabs and tables that fit a 1080p screen.

**The main windows.** Each window carries a grey information banner at the top, a stage strip that
shows where the window sits in the Data → Screen → Model → Classify sequence, a gear on the tab bar
for settings and help, and an action bar at the bottom with the bold primary button always in view
(Classify, Run this dam, Run county). The log is folded under a one-line status, and Close sits at
the bottom right.

**The "Model one dam" wizard (Breach Runner).** The dam page opens on a summary card and lists the
values in two groups, Required and Improves the estimate, each row led by a ✓ / ○ / ✕ mark that
follows every edit. A problem shows as a red line on the page rather than a pop-up. The terrain page
says whether the chosen DEM covers the dam point and folds the roughness settings under an Advanced
header that shows their values.

## Notes

- Hazard 2.33.0 → 2.34.0, Breach Runner 3.23.0 → 3.25.0 and Screening 0.31.0 → 0.32.0 cover the
  three-part GUI work; the Data Updater moved to 1.7.0 for the shared-window and settings changes.
- The shared `damsuite` data package is unchanged at 1.18.0, so an offline pack built for release
  1.24 is still current.
