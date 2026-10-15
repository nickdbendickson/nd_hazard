"""Where tests register themselves, so every tests/test_*.py adds to the one suite:
`from ND_Dam_Hazard_Helper.tests.registry import test`."""
TESTS = []


def test(fn):
    TESTS.append(fn)
    return fn
