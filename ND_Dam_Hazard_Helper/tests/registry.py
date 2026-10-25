"""Where tests register themselves, so every tests/test_*.py adds to the one suite:
`from ND_Dam_Hazard_Helper.tests.registry import test`."""
TESTS = []


class Skip(Exception):
    """2.34.5: raised by a test that cannot run here (e.g. it needs a real PyQGIS) - the runner
    reports it as 'skip', never 'ok'."""


def test(fn):
    TESTS.append(fn)
    return fn
