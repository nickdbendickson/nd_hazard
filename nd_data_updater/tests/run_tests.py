"""Run every test of the ND Data Updater (1.4.0):

    cd <folder that holds nd_data_updater>
    /usr/bin/python3.12 -m nd_data_updater.tests.run_tests

or `/usr/bin/python3.12 nd_data_updater/tests/run_tests.py` from anywhere. Arguments narrow the
run to the tests whose name contains one of them. The exit code is the number of failures
(0 = all passed). No QGIS and no network: every test that would touch either passes its own
opener, clock or builder in."""
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

from nd_data_updater.tests.registry import TESTS  # noqa: E402


def _load_test_modules():
    pkg = importlib.import_module(PACKAGE + ".tests")
    for m in sorted(pkgutil.iter_modules(pkg.__path__), key=lambda m: m.name):
        if m.name.startswith("test_"):
            importlib.import_module(PACKAGE + ".tests." + m.name)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    print("python {}".format(sys.version.split()[0]))
    _load_test_modules()
    only = [a for a in argv if not a.startswith("-")]
    todo = [fn for fn in TESTS if not only or any(o in fn.__name__ for o in only)]
    failed = 0
    for fn in todo:
        try:
            fn()
            print("ok    ", fn.__name__)
        except Exception:
            failed += 1
            print("FAIL  ", fn.__name__)
            traceback.print_exc()
    print("\n{} tests, {} failed".format(len(todo), failed))
    return failed


if __name__ == "__main__":
    code = main()
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)              # offscreen QGIS can crash on teardown; the result stands
