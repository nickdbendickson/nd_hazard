"""Tests that run without QGIS (v2.28):

    cd <folder that holds ND_Dam_Hazard_Helper>
    python3 -m ND_Dam_Hazard_Helper.tests.run_tests

or `python3 ND_Dam_Hazard_Helper/tests/run_tests.py` from anywhere. A real PyQGIS on the
interpreter is used when it imports (QT_QPA_PLATFORM=offscreen helps); otherwise
tests/fake_qgis.py stands in. Every tests/test_*.py registers its tests through
tests/registry.py; a test that cannot run here raises registry.Skip ('skip', not 'ok'); the
exit code is the number of failures (0 = all passed)."""
import importlib
import os
import pkgutil
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_DIR = os.path.dirname(HERE)
if os.path.dirname(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, os.path.dirname(PLUGIN_DIR))
PACKAGE = os.path.basename(PLUGIN_DIR)

from ND_Dam_Hazard_Helper.tests import fake_qgis  # noqa: E402
from ND_Dam_Hazard_Helper.tests.registry import TESTS, Skip  # noqa: E402


def _load_test_modules():
    """tests/test_*.py register their tests through tests.registry."""
    pkg = importlib.import_module(PACKAGE + ".tests")
    for m in sorted(pkgutil.iter_modules(pkg.__path__), key=lambda m: m.name):
        if m.name.startswith("test_"):
            importlib.import_module(PACKAGE + ".tests." + m.name)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    mode = fake_qgis.install()
    print("QGIS: {} ({})".format(mode, sys.version.split()[0]))
    _load_test_modules()
    only = [a for a in argv if not a.startswith("-")]
    todo = [fn for fn in TESTS if not only or any(o in fn.__name__ for o in only)]
    failed = skipped = 0
    for fn in todo:
        try:
            fn()
            print("ok    ", fn.__name__)
        except Skip as e:                       # 2.34.5: ran nothing here - not 'ok'
            skipped += 1
            print("skip  ", fn.__name__, "({})".format(e))
        except Exception:
            failed += 1
            print("FAIL  ", fn.__name__)
            traceback.print_exc()
    print("\n{} tests, {} failed{}".format(len(todo), failed, ", {} skipped".format(skipped)
                                            if skipped else ""))
    return failed


if __name__ == "__main__":
    code = main()
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)              # offscreen QGIS can crash on teardown; the result stands
