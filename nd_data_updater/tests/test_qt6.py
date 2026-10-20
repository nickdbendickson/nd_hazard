"""QGIS 4 (Qt6) readiness (1.4.1): the plugin says it supports Qt6, and no file outside
qtcompat.py spells a Qt or QGIS enum, QAction or exec_() the Qt5-only way.

The three dam plugins have read their enums through a compat module since 2.x; the updater did
not, and its metadata did not declare Qt6, so QGIS 4 would not load it - and step 1 of the suite
with it. This test reads the source, so it runs without QGIS; qtcompat.py itself is resolved
against a real QGIS by the release check."""
import configparser
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(HERE)
if os.path.dirname(PLUGIN) not in sys.path:
    sys.path.insert(0, os.path.dirname(PLUGIN))
from nd_data_updater.tests.registry import test  # noqa: E402

# Qt5-only spellings: an enum member read straight off its class (PyQt6 wants the enum's own
# name in between: QMessageBox.StandardButton.Yes), QAction from QtWidgets, exec_()
UNSCOPED = re.compile(
    r"\b(?:QMessageBox\.(?:Yes|No|Cancel|Ok|Close|Save)"
    r"|QDialogButtonBox\.(?:Ok|Cancel|Close|Save|Yes|No)"
    r"|Qt\.(?:WindowModal|ApplicationModal|WaitCursor|ItemIsEditable|ItemIsEnabled|UserRole"
    r"|AlignLeft|AlignRight|AlignCenter|Checked|Unchecked|Horizontal|Vertical)"
    r"|QHeaderView\.(?:Stretch|ResizeToContents|Interactive)"
    r"|QAbstractItemView\.(?:NoEditTriggers|SelectRows|NoSelection|SingleSelection)"
    r"|QPlainTextEdit\.NoWrap"
    r"|QNetworkRequest\.(?:HttpStatusCodeAttribute|CacheLoadControlAttribute|AlwaysNetwork)"
    r"|QgsBlockingNetworkRequest\.NoError"
    r"|Qgis\.(?:Info|Warning|Critical|Success)"
    r"|QgsTask\.CanCancel|QgsMapLayerType\.VectorLayer)\b"
    r"|from qgis\.PyQt\.QtWidgets import[^\n]*\bQAction\b"
    r"|\.exec_\(")


def _sources():
    for root, dirs, files in os.walk(PLUGIN):
        dirs[:] = [d for d in dirs if d not in ("tests", "__pycache__", "damsuite")]
        for n in files:
            if n.endswith(".py") and n != "qtcompat.py":
                yield os.path.join(root, n)


@test
def test_metadata_declares_qt6():
    cp = configparser.ConfigParser(interpolation=None)
    cp.read(os.path.join(PLUGIN, "metadata.txt"), encoding="utf-8")
    g = cp["general"]
    assert g.get("supportsQt6", "").strip() == "True"
    assert g.get("qgisMaximumVersion", "").strip() == "4.99"
    assert g["version"].strip() == "1.7.0"


@test
def test_no_qt5_only_spelling_outside_qtcompat():
    bad = []
    for path in _sources():
        with open(path, encoding="utf-8") as f:
            for i, line in enumerate(f, 1):
                code = line.split("#", 1)[0]
                if UNSCOPED.search(code):
                    bad.append("{}:{}: {}".format(os.path.relpath(path, PLUGIN), i, line.strip()))
    assert not bad, "\n".join(bad)


@test
def test_qtcompat_tries_the_qt6_spelling_first():
    with open(os.path.join(PLUGIN, "qtcompat.py"), encoding="utf-8") as f:
        text = f.read()
    for name in ("MB_YES", "BB_CLOSE", "WINDOW_MODAL", "NO_EDIT_TRIGGERS", "HTTP_STATUS",
                 "MSG_WARNING", "TASK_CAN_CANCEL", "VECTOR_LAYER"):
        line = next(ln for ln in text.splitlines() if ln.startswith(name + " = "))
        first = line.split("lambda:", 2)[1]
        assert first.count(".") >= 2, line                   # Class.Enum.Member, not Class.Member
