# ND Dam Suite — release 1.27 (2026-11-03)

Built from current `main` of `nickdbendickson/nd_hazard` at `685179c`. CI was green on that head
(fast-checks, plugin-suites and model-tests). Each zip's archive comment carries the source
commit. This release packages the work that landed on `main` after release 1.26 but had not yet
been cut into a release folder, plus the three code-review fix PRs merged on 2026-11-03.

## Versions

**ND set** (the zips in this folder, `releases/1.27/`)

| Plugin | 1.26 | 1.27 |
|---|---|---|
| ND Dam Hazard Classification Helper | 2.34.4 | **2.35.2** |
| ND Breach Runner | 3.26.2 | **3.27.1** |
| ND Dam Screening | 0.32.4 | **0.33.2** |
| ND Data Updater | 1.7.0 | 1.7.0 (**rebuilt**) |
| shared `damsuite` | 1.18.0 | 1.18.0 |

The Data Updater keeps its version number; its 1.27 zip is rebuilt from current `main` so the
`damsuite` release table it vendors matches the other three.

Install: QGIS → Plugins → Manage and Install Plugins → Install from ZIP. Keep the ND set and the
DamSafety set in **separate QGIS profiles**; they register the same Processing ids.

## What's new since 1.26

### ACER zone from the peak depth with the peak velocity (option C)
Hazard 2.35.2 / Runner 3.27.1 / Screening 0.33.2. A structure's ACER zone is now classified on
its peak depth together with the peak velocity over its wet cells, even when the two come from
different cells (TM 11 practice), rather than cell by cell. The pair can only keep or raise the
zone. Margins come from the same pair.

### Judgment-zone houses are ND Medium by rule
Hazard 2.35.0 / Runner 3.27.0 / Screening 0.33.0 (Nick, 2026-10-30). A habitable structure in the
ACER judgment zone makes the dam **ND Medium by rule** — not probable loss of life, no provisional
High. High-danger-zone houses are still High. The old behaviour (judgment zone = probable loss of
life, High) stays as an option, off by default (`ND_JUDGMENT_HIGH` / the Classify checkbox). The
TM 11 "lives in jeopardy" setting is unchanged.

### Hazard test and legend fixes (2.35.1)
Self-test expectations corrected for the post-audit habitability rules and the 25-algorithm
count; report/map-book legends use the QGIS 4 `setSyncMode(Manual)` path.

### Reproduce / data-folder completeness (Hazard 2.34.5)
Every file of every data role is reproduced (a single-file role was silently skipped, a
multi-file role opened only its first file).

### Runner HEC-RAS 2D and flood-extent work (3.26.3 – 3.26.9)
Arrival depth defaults to 0.1 ft (the ND wet depth, so every wet structure gets an arrival time);
the model domain also extends where water leaves through the edges (not only where an edge is >1 ft
deep); and a series of HEC-RAS 2D export fixes from running the Absaraka (DWR-2801) model in real
HEC-RAS 6.6 — BC line placement, finer cells and adaptive time step below the breach, breach/weir
elevations above the adjacent cells, dam line onto the embankment crest, monotonic elevation-volume
rows, and culverts cut from the found channel with inventory inverts. LISFLOOD-FP and ANUGA runs
are unchanged by the HEC-RAS work.

### Screening one-dam chain (0.33.1)
The Crest one-dam hand-off: the preflight disk estimate counts only the requested dam(s), and
tool 15 writes its log line by line so a stopped run still leaves its log.

### Code-review fixes (2026-11-03)
Three PRs from the Helper code-review batch, merged into this release:

- **nd_hazard #41** — the dialog's "disable while running" and settings lists are checked for
  missing widget names at start-up (one QGIS-log warning instead of a silent no-op), and the
  swallowed Qt signal-connect guards now log what failed.
- **nd_hazard #42** — a wet cell missing its velocity is filled with the structure's peak
  velocity instead of 0 (0 read optimistically on the ACER curves); the long-disabled
  `collapse_dwellings` is removed so its address-key merge can't be reinstated; a genuine second
  dwelling unit (different NSI id or address just outside the outline) is no longer dropped; the
  `Notes` attribute is length-capped; and `finish_run` resumes cleanly after a mid-finish failure.
  All in the conservative (dam-safety) direction.
- **DWR_Plugins #252** — the same widget-name audit ported to the DamSafety Hazard dialog, which
  found and removed a dead `run_all_btn` from its busy list.

None of the review PRs bumped a plugin version (no-behaviour-change hardening, apart from the
velocity fix which leans conservative), so they ride the 2.35.2 Hazard zip here.

## DamSafety (DWR) set

The DamSafety zips are built separately from `nickdbendickson/DWR_Plugins` (without `tests/`) and
are not committed to this repository. Since the 1.26 bundle, DWR_Plugins #252 (the widget-name
audit) merged with no version bump. Build the DWR bundle from current `DWR_Plugins` `main` when a
combined bundle is needed.

## Notes
- No plugin version was bumped for this release beyond what already sat on `main`; the `damsuite`
  release table and every `metadata.txt` already agree (fast-checks pass).
- A session cannot create a GitHub Release or tag; this is the `releases/1.27/` folder only. Tag
  and publish on GitHub from a workstation if a published release is wanted.
