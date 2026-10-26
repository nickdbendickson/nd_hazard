#!/usr/bin/env python3
"""tools/ci_checks.py fast-gate tests. Covers the 'qt6 enums' lint: it must fire on the
unscoped PyQt6/QGIS 4.x enum spellings (and bare-int / QVariant QgsField types) that broke the
sibling DamSafety suite in #131 / #134, and must allow the scoped forms, compat constants,
hasattr-guarded and '# qt6-ok' lines, prose in docstrings/comments, compat modules and qgis_mock
doubles. And the 'release table' check: the damsuite RELEASE of every vendored copy must equal
each plugin's metadata.txt version, so a merge that keeps one side of a version bump fails here.
Pure stdlib; builds a throw-away tree.

    python3 tools/test_ci_checks.py
"""
import os
import re
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ci_checks as C  # noqa: E402

QT6_BAD = {
    "pkg/a_image.py": "from qgis.PyQt.QtGui import QImage\nimg = QImage(1, 2, QImage.Format_RGB32)\n",
    "pkg/a_io.py": "buf.open(QIODevice.WriteOnly)\n",
    "pkg/a_paint.py": "p.setRenderHint(QPainter.Antialiasing, True)\n",
    "pkg/a_net.py": ("s = reply.attribute(QNetworkRequest.HttpStatusCodeAttribute)\n"
                     "ok = (reply.error() == QNetworkReply.NoError)\n"),
    "pkg/a_role.py": "item.setData(Qt.UserRole, r[6])\n",
    "pkg/a_field.py": 'f = QgsField("x", 10)\ng = QgsField("y", QVariant.String)\n',
}
QT6_GOOD = {
    "pkg/b_scoped.py": (
        "a = QImage.Format.Format_RGB32\n"
        "b = QIODevice.OpenModeFlag.WriteOnly\n"
        "c = QPainter.RenderHint.Antialiasing\n"
        "d = QNetworkRequest.Attribute.HttpStatusCodeAttribute\n"
        "e = QNetworkReply.NetworkError.NoError\n"
        "g = Qt.ItemDataRole.UserRole\n"
        'h = QgsField("x", FT_STRING)\n'
        'i = QgsField("y", QMetaType.Type.QString)\n'),
    "pkg/b_guarded.py": (
        'fmt = QImage.Format_RGB32 if hasattr(QImage, "Format_RGB32") else None\n'
        "hint = QPainter.Antialiasing  # qt6-ok\n"),
    "pkg/b_prose.py": (
        '"""Docstring naming the Qt5 spellings QImage.Format_RGB32 and Qt.UserRole."""\n'
        "x = 1  # comment: QIODevice.WriteOnly and QNetworkReply.NoError\n"),
    "pkg/compat.py": "s = QVariant.String\nw = QIODevice.WriteOnly\n",
    "pkg/qgis_mock/qgis.py": "UserRole = 256\nVAL = QImage.Format_RGB32\n",
}


def _w(root, rel, text):
    p = os.path.join(root, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write(text)


def test_qt6_enum_lint_fires_and_allows():
    root = tempfile.mkdtemp(prefix="qt6_lint_")
    try:
        for rel, text in dict(QT6_BAD, **QT6_GOOD).items():
            _w(root, rel, text)
        hits = C.scan_qt6_enums(root)
        hit_files = {h[0].replace(os.sep, "/") for h in hits}
        for rel in QT6_BAD:                       # every bad spelling is caught
            assert rel in hit_files, (rel, sorted(hit_files))
        for rel in QT6_GOOD:                      # scoped / compat / guarded / prose / mock: allowed
            assert rel not in hit_files, (rel, sorted(hit_files))
        labels = " ".join(h[2] for h in hits)
        assert "bare-int" in labels and "QVariant type" in labels, labels
        assert "QNetworkRequest" in labels and "QNetworkReply" in labels, labels
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_qt6_enum_lint_clean_on_the_real_tree():
    hits = C.scan_qt6_enums(C.ROOT)
    assert hits == [], ["{}:{} {}".format(h[0], h[1], h[2]) for h in hits]


SET = {"ndhazard": "2.34.5", "ndbreach": "3.26.2", "ndscreen": "0.32.4", "ndupdater": "1.7.0"}


def _release_tree(root, meta, release, odd=None):
    """Every plugin gets the real damsuite/versions.py with its RELEASE replaced by `release` (or
    by odd[plugin] for that one copy) and a metadata.txt at version=meta[key]."""
    folders = C._load_versions(os.path.join(C.ROOT, "nd_breach_runner", "damsuite",
                                            "versions.py")).ALL
    for plugin in C.PLUGINS:
        with open(os.path.join(C.ROOT, plugin, "damsuite", "versions.py"), encoding="utf-8") as f:
            src = f.read()
        table = "RELEASE = " + repr((odd or {}).get(plugin, release))
        src, n = re.subn(r"^RELEASE\s*=\s*\{[^}]*\}", lambda _m: table, src, flags=re.M)
        assert n == 1, plugin
        _w(root, plugin + "/damsuite/versions.py", src)
    for key, folder in folders.items():
        _w(root, folder + "/metadata.txt", "[general]\nname={}\nversion={}\n".format(key, meta[key]))


def _release_problems(meta, release, odd=None):
    root = tempfile.mkdtemp(prefix="release_table_")
    try:
        _release_tree(root, meta, release, odd)
        problems = []
        C.check_release_table(problems, root)
        return problems
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_release_table_matching_metadata_passes():
    assert _release_problems(SET, SET) == []


def test_release_table_one_sided_bump_fails():
    # RELEASE took the Runner bump (3.26.3), metadata.txt kept the other side (3.26.2)
    p = _release_problems(SET, dict(SET, ndbreach="3.26.3"))
    assert any("RELEASE['ndbreach'] = 3.26.3 but nd_breach_runner/metadata.txt version=3.26.2"
               in x for x in p), p
    assert len(p) == 1, p
    # and the other way round: metadata.txt is 2.34.5, every RELEASE copy still says 2.34.4
    p = _release_problems(SET, dict(SET, ndhazard="2.34.4"))
    assert any("RELEASE['ndhazard'] = 2.34.4 but ND_Dam_Hazard_Helper/metadata.txt version="
               "2.34.5" in x for x in p), p


def test_release_table_one_copy_disagreeing_fails():
    p = _release_problems(SET, SET, odd={"nd_dam_screening": dict(SET, ndbreach="3.26.3")})
    assert any("RELEASE disagrees across copies" in x and "nd_dam_screening" in x for x in p), p
    assert any("RELEASE['ndbreach'] = 3.26.3" in x and "nd_dam_screening" in x for x in p), p


def test_release_table_clean_on_the_real_tree():
    problems = []
    C.check_release_table(problems)
    assert problems == [], problems


def main():
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("PASS", name)
            except Exception:
                fails += 1
                import traceback
                traceback.print_exc()
                print("FAIL", name)
    print("{} failed".format(fails))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
