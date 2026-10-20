# ND dam plugins GUI grammar

One page on what an ND dam plugin window looks like, so the next window does not have to work it out
from the Hazard code. Hazard's main window is the reference implementation. Where this page and
the code disagree, the code is wrong. If a case isn't covered here, ask before inventing a new
style.

## How it is kept

- **Shared files.** The window widgets (`ui_widgets.py`), the colours (`tokens.py`), window
  sizing (`ui_fit.py`), the settings grid (`suite_settings*.py`), the map styles
  (`suite_styles.py`) and the Dam suite home (`suite_home.py`) each have one canonical copy in
  `shared/ui/`. The plugins install separately, so each ships its own copy. **Edit
  `shared/ui/` only**, then run `python3 tools/sync_ui.py`. `tools/sync_ui.py` lists which
  plugin carries which file.
- **Fast CI check** (`tools/ci_checks.py`, no QGIS needed): every plugin copy must match
  `shared/ui/`, and window code must not contain a `#rrggbb` colour outside `tokens.py`.
- **GUI lint** (`tools/gui_lint.py`, real QGIS, offscreen): opens every window and checks the
  rules below. Run it as `QT_QPA_PLATFORM=offscreen <QGIS python> tools/gui_lint.py [shots_dir]`.
  Add a screenshots folder to see every window. One rule, no page scrolls sideways, needs a
  1080p screen (Qt5's offscreen screen is 800x600), so run it under a virtual display as well:
  `xvfb-run -s "-screen 0 1920x1080x24" env QT_QPA_PLATFORM=xcb <QGIS python> tools/gui_lint.py`.

## The rules

| Element | Rule |
|---|---|
| Window title | `<plugin name> <version>`, the name as metadata.txt gives it and the version without a `v`, for example `ND Dam Breach Runner 3.23.0`. A secondary window is `<plugin name> — <Window>`. |
| In-window title | None. The window title and the banner carry the context. |
| Banner | The shared grey `InfoBanner` (ℹ) wherever a caveat exists. No plugin-specific boxes. |
| Navigation | Main tabs, plus the ⚙ gear for settings, documentation and tools. |
| Progress | `StageStrip` pills, mark before the word: `✓ Folder → ▶ Dam → ○ DEM`. Every mark in the suite (chips, row marks, check summaries) leads, so the marks line up in a column. |
| Primary action | Qt's own button look with **bold** text, in the action bar at the bottom right, next to its check button (Classify beside Check inputs). No ▶ prefix and no painted background. |
| Wizards | One wizard per job. A page that exists in one wizard is written there; widgets (`DamCard`, `Collapsible`, the path row) come from `ui_widgets.py`. Nobody copies a page. A wizard that hands over to another says **Continue…** on its button; the line beside it says where the work goes next. |
| Checks | `CheckPanel`, never a message box. Red blocks the primary button; amber warnings are collapsible rows; passes fold into one line. Each problem is reported once, next to what it blocks. |
| Disabled controls | Always say why, in the text beside the control or in its tooltip. |
| Log | `▸ Log` fold-out in the footer, above Close. |
| Buttons | One `…` character (U+2026, not three dots) on anything that opens another window or a picker (`Browse…`, `Load…`, `Save as…`). Close sits at the bottom right. No icons on action buttons. |
| Units | Brackets in form labels: `Dam height (ft)`. Dense table headers may use bare abbreviations (`Peak cfs`, `Flooded ac`). This exception is deliberate. |
| Ampersands | Write `&&` in any tab or button text that contains `&`. A single `&` marks a keyboard shortcut, so "Review & QA" shows as "Review _QA". `suite_settings.tab_title()` does this for the settings tabs. |
| Size | Every window must shrink to fit a 1080p screen. Long pages scroll (`ui_fit.wrap_tabs`), and a settings table shows its Value column without scrolling sideways. |
| Colours | Only from `tokens.TOKENS`: red means blocking, amber a warning, green passed, grey inactive. `link` blue is for the active stage and text links only, never a button background. |
| Plugin icon | Every plugin has an `icon.svg` (dam wedge plus water, with a mark for the tool) named by `icon=` in metadata.txt, and its menu entry uses it. The same icons as the DamSafety plugins. |

Map symbology, PDF and HTML reports, and charts keep their own palettes. Hazard-class colours
belong to the product, not the window, so the colour rules don't apply to them.

## Decided 2026-10-20 (Lumo)

- Pills put the mark before the word (the implementation was right; this page had it backwards).
- (DamSafety set) Setup's fallback wizard becomes one page, the suite data folder scan, and then
  continues into the Runner wizard with the scan carried forward. The Runner wizard is the suite's only per-dam
  wizard, and it takes over B's ideas: the DamCard summary, Required / Improves-the-estimate
  groups with ✓/⚠/○ row marks, the live DEM coverage line, source warnings as amber rows, and a
  `Collapsible` Advanced header that shows the values.
- Built in DWR_Plugins part 3 (the DamSafety set); this repo has no Setup plugin, so here it means
  the Runner's dam wizard: DamCard, Required / Improves groups, coverage line, Advanced header,
  problems in red on the page (part 3 of the same work on this repo).
