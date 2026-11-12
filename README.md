# ND Dam Suite (nd_hazard) — archived fork

**This repository is frozen.** Development of the North Dakota dam-safety QGIS plugin suite
continues in one place:

> **https://github.com/nickdbendickson/dAmIt_Plugins** (the `DamSafety_*` plugins, formerly `DWR_Plugins`)

The two repositories were forks of the same suite (same `damsuite/` shared package, same tools, the
same fixes applied twice). Keeping both meant every change had to land in both and, as the Oct‑15
audit (`docs/Audit_DWR_vs_nd_hazard.md` over there) showed, fixes were lost in one fork or the
other. As of 2026‑11‑07 the DamSafety line is the product; this `nd_*`-branded line is kept only
for reference and for anyone who still has the ND set installed.

## What is here

| Plugin (last version) | Successor in dAmIt_Plugins |
|---|---|
| ND Dam Hazard Classification Helper 2.35.18 | DamSafety Hazard |
| ND Dam Breach Runner 3.29.6 | DamSafety Runner |
| ND Dam Breach Screening 0.33.15 | DamSafety Screening |
| ND Data Updater 1.8.1 | DamSafety Data (+ DamSafety Setup) |

The final state of `main` is tagged **`archive/nd-plugins-2026-11-07`**. The last built zips are in
`releases/1.30/` (see `releases/1.30/RELEASE_NOTES.md`).

## If you still run the ND set

- It keeps working as installed; nothing here is removed.
- The ND and DamSafety sets register the same Processing ids, so keep them in **separate QGIS
  profiles** (as the release notes say) while you move over.
- To move over: install the DamSafety plugins from the dAmIt_Plugins releases (or its QGIS plugin
  repository once published), point DamSafety Setup at the same suite data folder, and your dam
  folders, offline packs and `dam.json` files are read as before (the pack format and `dam.json`
  schema are shared).
- Bugs and requests go to the dAmIt_Plugins issue tracker, not here.

## Developers

Anything still unique to this fork that the DamSafety line lacks was listed in the Oct‑15 audit
("DWR is genuinely ahead on…" and the reverse list). Port from the tag, not from `main`, so the
source is immutable. The CI here (`.github/workflows/ci.yml`) is left in place so the archived tree
still proves its own gate, but no new work should be merged.
