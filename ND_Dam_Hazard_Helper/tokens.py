# -*- coding: utf-8 -*-
"""Design tokens of the ND dam plugin windows: the one place a window colour is written.

Canonical copy: shared/ui/tokens.py. Each plugin ships a byte-identical copy (the plugins are
installed separately, so none imports another's); edit the canonical file and run
tools/sync_ui.py, never a copy. tools/ci_checks.py fails the build on a stale copy, and the GUI
lint fails on a #rrggbb colour in a window's style sheet that is not one of these.

Scope: window styling (style sheets, rich-text labels). Map symbology, PDF / HTML reports and
charts keep their own palettes (hazard-class colours are part of the product, not the chrome).
See docs/gui-grammar.md.

Only the standard library is used, so the file imports without QGIS.
"""

SHARED_VERSION = "1.0.0"

TOKENS = {
    "error": "#c62828",        # blocking errors, red text, missing required input
    "error_fill": "#b3261e",   # the CheckPanel's red banner background (white text)
    "error_edge": "#8c1d18",   # its border
    "warn": "#9a6700",         # amber warnings, partial state
    "warn_fill": "#f9a825",    # the WARN pill background
    "warn_ink": "#3e2723",     # text on the WARN pill
    "warn_bg": "#fff4e0",      # amber note box background
    "warn_edge": "#e0b060",    # its border
    "success": "#2e7d32",      # checks passed, done, found
    "off": "#888888",          # not run yet / missing / inactive
    "muted": "#666666",        # secondary text: hints, descriptions, paths
    "subtle": "#f6f6f6",       # banner and status-line background
    "card": "#fbfbfd",         # dam card background
    "frame": "#d0d0d0",        # hairline borders
    "link": "#1a5fb4",         # the active stage and text links (never a button background)
}

# Mark vocabulary, the same in every window.
MARKS = {"ok": "✓", "part": "◐", "empty": "○", "active": "▶",
         "flag": "⚠", "fail": "✕"}

SPACING = 6           # between rows of a form, px
GROUP_MARGIN = 12     # between sections, px
BANNER_ICON = "ℹ"
ELLIPSIS = "…"


def color(name):
    """TOKENS[name]; a KeyError names the token, so a typo fails loudly in tests."""
    return TOKENS[name]


def chip(label, on, extra_css=""):
    """Rich text for a ✓ / ○ status chip (word after the mark)."""
    return "<span style='color:{};{}'><b>{}</b> {}</span>".format(
        TOKENS["success"] if on else TOKENS["off"], extra_css,
        MARKS["ok"] if on else MARKS["empty"], label)


def err_style(on):
    """Style sheet fragment for an input whose content is in error."""
    return "border:1px solid {};".format(TOKENS["error"]) if on else ""
