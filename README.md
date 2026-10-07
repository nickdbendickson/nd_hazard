# ND Dam Suite (QGIS plugins)

QGIS plugins for North Dakota dam hazard classification and breach analysis.

| Folder | Plugin | Version |
|---|---|---|
| `ND_Dam_Hazard_Helper/` | ND Dam Hazard Classification Helper | 2.22.0 |
| `nd_breach_runner/` | ND Breach Runner (LISFLOOD-FP / ANUGA / HEC-RAS export) | 3.12.0 |
| `nd_dam_screening/` | ND Dam Screening | 0.20.0 |
| `nd_data_updater/` | ND Data Updater (NSI 2026 + 2022 merge) | 1.1.0 |

The three suite plugins share a vendored `damsuite/` package (1.8.0) that is byte-identical in each.

## Install

Download the plugin zips from [`releases/1.11/`](releases/1.11) (click a zip, then the download button) and use
QGIS > Plugins > Manage and Install Plugins > Install from ZIP.
Each folder here is the unzipped plugin, so you can also copy a folder straight into your QGIS profile's `python/plugins/` directory.

`ND_Dam_Hazard_Helper_2.10.0_source.md` is the older 2.10.0 source dump, kept for reference.
