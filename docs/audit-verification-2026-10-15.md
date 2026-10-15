# ND Dam Hazard Helper: checking the earlier audit against the code

**Checked against:** `ND_Dam_Hazard_Helper/`, plugin version 2.32.0 per the top-level README (`constants.py` falls back to `"2.31.0"` when `metadata.txt` can't be read).
**Date:** 2026-10-15

## Why this exists

An earlier audit of this plugin was made in a chat with another AI assistant. It was based on pasted files, some of them older versions. It admitted part-way through to inventing whole modules. This document checks that audit's headline findings against the code in this repository. It also finishes the review of `settings.py` and `estimate.py`, which that audit broke off partway through.

Every verdict below cites a file and line in this repository. Nothing has been fixed yet; this is a report only.

## Findings that hold up

| ID | Severity | File | Finding |
|---|---|---|---|
| NEW-1 | Medium | `pipeline.py:608` (`evaluation_key`) | **The offline settings are not part of the evaluation cache key.** The pack path and the "offline only" switch come from `download.offline_config()`, which is read during the evaluation at `pipeline.py:2252` but never added to the key. Turning "offline only" off, switching to another pack, or rebuilding the pack all produce the same key. The next run can then reuse an evaluation that skipped downloads or used old pack data. Fix: add `offline_config()`, plus the pack file's signature, to `parts` in `evaluation_key`. |
| NEW-2 | Low–Medium | `settings.py:322` and `settings.py:424`, with the caller at `extras290_tab.py:858-906` | **A profile from a newer plugin version is always recorded as "MODIFIED".** `load_profile` keeps settings this version has no widget for in the returned `state` (this is documented). `profile_status` then compares that state with the dialog's current settings. Those unknown settings are never in the dialog, so they always count as changed. The run log and the submittal then say "MODIFIED after loading: <key>" even though the engineer changed nothing. Reproduced outside QGIS with stubbed imports: a profile carrying one unknown key gives `modified: True, changed_keys: ['future_spin']` straight after loading. A second, related problem: a list choice that doesn't exist in this project is skipped without a message (`set_widget_value`, `settings.py:89-93`), yet the load dialog still reports "N setting(s) applied". Here N also counts the unknown keys. Fix: in `profile_status`, compare only the keys in `known_keys`, and report how many settings were actually applied. |
| NEW-3 | Low | `estimate.py:217` vs `pipeline.py:1971-2030` | **The pre-run estimate no longer matches what the run downloads.** `planned_downloads` copies the pipeline's list of downloads by hand, and the two now differ. The estimate always counts FEMA USA Structures, but the run skips it when the outline layer is already FEMA structures (`is_fema_structures_layer`). The estimate counts mobile-home parks and places even when the data folder supplies them (the run checks `"parks"` / `"places"` against the data folder; the estimate does not). The result is an over-count. Fix: have the pipeline and the estimate share one function that builds the list, or add a test that compares the two on a fixed cfg. |
| D.2 | Low | `dialog.py:855-874` | `_set_busy` leaves `review_webmap_btn`, `field_sheet_btn` and `field_import_btn` clickable while a run is going. The web-map handler checks for a run itself (`review_tab.py:959`); the field-sheet handlers were not checked. |
| P.8 | Low | `pipeline.py:2296-2301` | `_read_places` reads `f["name"]` and `f["place"]` outside its `try`. A data-folder "places" layer without those fields raises `KeyError` and stops the run. |
| RS.1 | Low–Medium | `rasimport.py:730`, `766-790` | The HEC-RAS plan cache key uses size, modification time and an MD5 of the first and last MB, not `fingerprint.content_digest`. Saving a file without changing it still forces a new conversion. An edit in the middle of a large file that keeps its size and date is missed. |
| LN.1 | Low | `linear.py:599-600`, `sensitivity.py:408-410` | The formula for motorists on a road is written out twice. In `sensitivity.py` it is only a fallback (the run's own `persons` value is used first), so the two can drift apart without anyone noticing. |
| P.7 | Info | `pipeline.py:463` | `_vrt_sources_sig` is never called. VRT sources already reach the key through `fingerprint.file_sig`, so this is dead code. |
| ST.6 | Info | `structures.py:234` | `mobile_rules_from_cfg` uses `bool(v)`, so the string `"false"` reads as True. The dialog passes real booleans, so this only matters if a value arrives as text. |
| CL.1 / R.1 / EX.1 | Info | `pipeline.py:2618`, `review_tab.py:818-820`, `plugin.py:174` | Settings and messages are matched by their display text. **Every string matches today**: all 17 zoom keys, both merge-refusal prefixes and the "Suite settings" caption. These are fragile but not broken. |

## Findings that are wrong (refuted)

| ID | The earlier audit's claim | What the code does |
|---|---|---|
| SC.1 (its top priority) | Baseline `cut_off` is lost because it is missing from `EVAL_KEYS`. | The pipeline snapshots with `SNAP_KEYS = EVAL_KEYS + CONSEQ_KEYS` (`pipeline.py:170-176`, `209`), and `CONSEQ_KEYS` includes `cut_off`. The baseline is passed as `res["snap"]` (`pipeline.py:1480-1481`), so the baseline's cut-off state reaches `mark_structures_incremental`. |
| D.1 | The "Data & Output" vs "Data and Output" caption mismatch stops the Data tab refreshing its layer lists. | The tab is added as `"Data & Output"` (`dialog.py:231`) and the check uses the same text (`dialog.py:302`). |
| P.1 | Environment variables `ND_DAM_SUITE` / `ND_DAM_OFFLINE` change cfg after the cache key is computed. | Neither variable exists in the code. The real gap is NEW-1 above. |
| ST.1, ST.3, CN.3, SX.1 (`dwelling_extra`) | Re-interpreting structures brings back households that `collapse_dwellings` removed; campgrounds lose their persons; values are double-counted. | `collapse_dwellings` is never called. It was switched off in 2.16.0 (comment at `structures.py:2203-2207`), so no record has `dwelling_extra` set. The flaws ST.3 describes are real in that function's code, but they matter only if the function is switched back on. |
| ST.2 | `reinterpret` leaves the ACER zone and outcome out of date. | Stage B runs `apply_type` on every record after `restore` (`pipeline.py:1455-1460`), and `restore` does not touch `acer_type`. |
| FA.1 | NaN cells can win `argmax` in `facilities.evaluate`. | `cells_in_geometry` removes cells that are not finite or are no-data before the comparison (`helpers.py:1117`, `1210-1257`). |
| ST.4 | `mark_mobile_parks` leaves the records it touches on default rules. | Partly true: stage A re-types those records with the default area and persons per dwelling. Stage B then re-interprets every record with the configured values (`pipeline.py:1429-1430`), so the run's result is correct. |

## `settings.py`: other notes

* Profiles are protected well. Folders, rasters and layer ids are removed again when a profile is loaded. Profiles from a newer format are refused, and the hash ignores key order and differences such as 2 vs 2.0.
* `DIALOG_KEYS` (`settings.py:135-174`) is a hand-kept copy of the dialog's settings list. Only `submittal.py:279` relies on it, and only for meta; the dialog passes its own list. Low risk.
* `dv_template_edit` is a file path, but it is not in `DAM_SPECIFIC_KEYS`. It is left out of profiles only when it is an absolute path (`looks_like_path`), so a relative path would be shared. Info.

## `estimate.py`: other notes

* It keeps its promise not to change anything: it doesn't download, unpack, or modify the tile cache. Every step is in its own `try` and adds a note when it fails. Errors are reported clearly.
* The time and size figures are labelled ROUGH and say how to calibrate them. Apart from NEW-3, the counts follow the plugin's own rules.
* `DEFAULT_MAX_AGE_DAYS = 7.0` repeats `pipeline.DEFAULTS["data_max_age_days"]`, and is kept in step only by a comment. Info.

## Suggested order of fixes

1. NEW-1: add the offline pack and "offline only" setting to `evaluation_key`.
2. NEW-2: compare profiles on known keys only, and report how many settings were really applied.
3. P.8: move the field reads in `_read_places` inside the `try`.
4. NEW-3: share one download list between the estimate and the run, or test that they agree.
5. RS.1: build the plan cache key from `fingerprint.content_digest`.
6. D.2: add the three missing buttons to `_set_busy`.

Of the earlier audit's priority list, items 1 (SC.1), 2 (SX.1) and 3 (ST.1/ST.2 via `rederive`) are not needed for the code as it stands.
