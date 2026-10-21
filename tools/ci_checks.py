#!/usr/bin/env python3
"""Fast, QGIS-free merge gate for the nd_* plugin suite. Runs the deterministic checks that need no
QGIS/GDAL install, so they can gate every PR cheaply:

  1. damsuite pin    — every vendored damsuite copy matches the shared pin (tools/damsuite_pin.json),
                       the single source both forks vendor from, with no checkout of the other repo
  2. version sanity  — each plugin metadata.txt has a well-formed version=, and the vendored
                       damsuite SUITE_VERSION is identical across every copy
  3. compile         — py_compile every .py in the suite (catches a syntax error from a bad merge
                       across all plugins in seconds, without importing anything)
  4. shared/ui       — every plugin copy of the shared window files matches shared/ui/, and no
                       window code writes a #rrggbb colour outside tokens.py (docs/gui-grammar.md)

Exit non-zero on any failure. The fuller per-plugin test suites (real/mock QGIS) run separately;
this job is the always-on hard gate.

    python3 tools/ci_checks.py
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGINS = ("ND_Dam_Hazard_Helper", "nd_breach_runner", "nd_dam_screening", "nd_data_updater")


def _fail(problems, msg):
    problems.append(msg)


def check_damsuite_pin(problems):
    """Every vendored damsuite copy matches the shared pin (and its FILES.json is current)."""
    sys.path.insert(0, os.path.join(ROOT, "tools"))
    import sync_damsuite
    for p in sync_damsuite.check(ROOT):
        _fail(problems, "damsuite pin: " + p)


def check_ui_drift(problems):
    """Every plugin copy of the shared window files (shared/ui/) matches its canonical file."""
    sys.path.insert(0, os.path.join(ROOT, "tools"))
    import sync_ui
    for p in sync_ui.check(ROOT):
        _fail(problems, "shared/ui drift: " + p)


# Window code: the files that build windows. A colour in them comes from tokens.py
# (docs/gui-grammar.md); maps, reports and charts keep their own palettes and are not listed.
WINDOW_FILES = ("dialog.py", "wizard.py", "popup_logic.py", "settings_dialog.py", "pack_dialog.py",
                "status_dock.py", "plugin.py", "suite_settings_tab.py", "classify_tab.py",
                "review_tab.py", "facility_review_tab.py", "data_tab.py", "datafolder_tab.py",
                "dsswise_tab.py", "extras_tab.py", "extras290_tab.py", "rasimport_tab.py",
                "suite_tab.py", "ui_check_panel.py", "constants.py")
HEX_RE = re.compile(r"#(?:[0-9a-fA-F]{6}|[0-9a-fA-F]{3})(?![0-9a-zA-Z])")


def check_window_colours(problems):
    """No #rrggbb in window code outside tokens.py (the shared/ui files are checked at their
    canonical copy; tokens.py is the one place a window colour is written)."""
    files = [os.path.join(ROOT, "shared", "ui", f) for f in os.listdir(os.path.join(ROOT, "shared", "ui"))
             if f.endswith(".py") and f not in ("tokens.py", "suite_styles.py")]
    for plugin in PLUGINS:
        for f in WINDOW_FILES:
            files.append(os.path.join(ROOT, plugin, f))
    for path in files:
        if not os.path.isfile(path):
            continue
        with open(path, encoding="utf-8") as f:
            for n, line in enumerate(f, 1):
                code = line.split("  #", 1)[0] if not line.lstrip().startswith("#") else ""
                if HEX_RE.search(code):
                    _fail(problems, "{}:{}: colour literal in window code - use tokens.py".format(
                        os.path.relpath(path, ROOT), n))


def check_versions(problems):
    ver_re = re.compile(r"^\d+\.\d+\.\d+")
    for plugin in PLUGINS:
        meta = os.path.join(ROOT, plugin, "metadata.txt")
        if not os.path.isfile(meta):
            _fail(problems, "{}: metadata.txt missing".format(plugin))
            continue
        with open(meta, encoding="utf-8") as f:
            m = re.search(r"^version=(.+)$", f.read(), re.M)
        if not m or not ver_re.match(m.group(1).strip()):
            _fail(problems, "{}: metadata.txt version not x.y.z".format(plugin))
    # the vendored damsuite SUITE_VERSION must be identical across every copy
    seen = {}
    for plugin in PLUGINS:
        init = os.path.join(ROOT, plugin, "damsuite", "__init__.py")
        if not os.path.isfile(init):
            continue
        with open(init, encoding="utf-8") as f:
            m = re.search(r'SUITE_VERSION\s*=\s*["\']([^"\']+)["\']', f.read())
        if m:
            seen.setdefault(m.group(1), []).append(plugin)
    if len(seen) > 1:
        _fail(problems, "damsuite SUITE_VERSION disagrees across copies: " + repr(seen))


def check_compile(problems):
    import py_compile
    n = 0
    for base, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in ("__pycache__", ".git")]
        for fn in files:
            if fn.endswith(".py"):
                try:
                    py_compile.compile(os.path.join(base, fn), doraise=True)
                    n += 1
                except py_compile.PyCompileError as e:
                    _fail(problems, "syntax error: " + str(e).splitlines()[-1])
    print("  compiled {} .py files".format(n))


# ---- Qt6 enum hygiene -------------------------------------------------------
# PyQt6 / QGIS 4.x removed unscoped enum members from the class namespace, and QgsField no
# longer accepts a bare int / QVariant type. These exact unscoped spellings raise
# AttributeError / TypeError on Qt6 (the sibling DamSafety suite hit this in #131 and #134).
# The scoped form (Class.EnumType.Member) and compat constants (FT_*, WKB_*, ...) do NOT match,
# so ported code passes. compat modules, the qgis_mock test doubles and this gate's own files
# are skipped; an intentional Qt5-fallback line can carry "# qt6-ok".
_QT6_FORBIDDEN = [
    ("QImage unscoped format (use QImage.Format.Format_*)",
     re.compile(r"\bQImage\.Format_[A-Za-z0-9_]+")),
    ("QIODevice unscoped open mode (use QIODevice.OpenModeFlag.*)",
     re.compile(r"\bQIODevice\.(?:ReadOnly|WriteOnly|ReadWrite|Append|Truncate|Text|Unbuffered"
                r"|NewOnly|ExistingOnly)\b")),
    ("QPainter unscoped render hint (use QPainter.RenderHint.*)",
     re.compile(r"\bQPainter\.(?:Antialiasing|TextAntialiasing|SmoothPixmapTransform"
                r"|HighQualityAntialiasing|LosslessImageRendering|NonCosmeticDefaultPen"
                r"|VerticalSubpixelPositioning)\b")),
    ("QPainter unscoped composition mode (use QPainter.CompositionMode.*)",
     re.compile(r"\bQPainter\.CompositionMode_[A-Za-z0-9_]+")),
    ("QNetworkRequest unscoped attribute (use QNetworkRequest.Attribute.*)",
     re.compile(r"\bQNetworkRequest\.(?!Attribute\b)[A-Z][A-Za-z0-9_]*Attribute\b")),
    ("QNetworkReply unscoped error (use QNetworkReply.NetworkError.*)",
     re.compile(r"\bQNetworkReply\.(?!NetworkError\b)[A-Za-z]+Error\b")),
    ("Qt unscoped item-data role (use Qt.ItemDataRole.*)",
     re.compile(r"\bQt\.(?:UserRole|DisplayRole|EditRole|DecorationRole|ToolTipRole"
                r"|CheckStateRole|BackgroundRole|ForegroundRole|FontRole|TextAlignmentRole"
                r"|SizeHintRole|WhatsThisRole|StatusTipRole|InitialSortOrderRole)\b")),
    ("QgsField with a bare-int type (use a QMetaType.Type or compat FT_*)",
     re.compile(r"\bQgsField\(\s*[^,()]+,\s*\d+\s*[),]")),
    ("QgsField with a QVariant type (use a QMetaType.Type or compat FT_*)",
     re.compile(r"\bQgsField\([^,()]+,\s*QVariant\.")),
]
_QT6_SKIP_FILES = {"compat.py", "qtcompat.py", "ci_checks.py", "test_ci_checks.py"}


def _mask_strings_comments(src):
    """src with every string/comment token blanked to spaces (line structure preserved) so the
    regexes see only code, not prose in a docstring or comment. On any tokenize error the source
    is returned unchanged."""
    import io
    import tokenize
    lines = src.split("\n")
    fstring = tuple(getattr(tokenize, n) for n in ("FSTRING_START", "FSTRING_MIDDLE", "FSTRING_END")
                    if hasattr(tokenize, n))
    blank_types = (tokenize.STRING, tokenize.COMMENT) + fstring
    try:
        toks = list(tokenize.generate_tokens(io.StringIO(src).readline))
    except Exception:
        return src
    for t in toks:
        if t.type not in blank_types:
            continue
        (sr, sc), (er, ec) = t.start, t.end
        sr, er = sr - 1, er - 1
        if sr == er:
            lines[sr] = lines[sr][:sc] + " " * (ec - sc) + lines[sr][ec:]
        else:
            lines[sr] = lines[sr][:sc] + " " * (len(lines[sr]) - sc)
            for r in range(sr + 1, er):
                lines[r] = " " * len(lines[r])
            lines[er] = " " * ec + lines[er][ec:]
    return "\n".join(lines)


def scan_qt6_enums(root):
    """[(rel_path, line_no, label, text)] of unscoped Qt6-broken enum spellings under root.
    Strings/comments are masked and a line guarded by hasattr(...) or '# qt6-ok' is skipped."""
    hits = []
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in ("__pycache__", ".git", "qgis_mock")]
        for fn in files:
            if not fn.endswith(".py") or fn in _QT6_SKIP_FILES:
                continue
            path = os.path.join(base, fn)
            try:
                with open(path, encoding="utf-8") as f:
                    src = f.read()
            except Exception:
                continue
            raw = src.split("\n")
            code = _mask_strings_comments(src).split("\n")
            for i, line in enumerate(code, 1):
                if "hasattr(" in raw[i - 1] or "# qt6-ok" in raw[i - 1]:
                    continue
                for label, rx in _QT6_FORBIDDEN:
                    if rx.search(line):
                        hits.append((os.path.relpath(path, root), i, label, raw[i - 1].strip()))
    return hits


def check_qt6_enums(problems):
    for rel, i, label, text in scan_qt6_enums(ROOT):
        _fail(problems, "qt6 enums: {}:{}: {} -> {}".format(rel, i, label, text))


def main():
    problems = []
    for name, fn in (("damsuite pin", check_damsuite_pin), ("versions", check_versions),
                     ("shared/ui drift", check_ui_drift), ("window colours", check_window_colours),
                     ("qt6 enums", check_qt6_enums),
                     ("compile", check_compile)):
        print("[ci] " + name)
        try:
            fn(problems)
        except Exception as e:      # a check itself blew up
            _fail(problems, "{} check errored: {}: {}".format(name, type(e).__name__, e))
    if problems:
        print("\nFAILED ({} problem(s)):".format(len(problems)))
        for p in problems:
            print("  - " + p)
        return 1
    print("\nAll fast checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
