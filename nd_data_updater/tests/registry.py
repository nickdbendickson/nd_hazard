"""Where tests register themselves, so every tests/test_*.py adds to the one suite:
`from nd_data_updater.tests.registry import test`. The same layout as the three dam plugins'
tests/, so a reader moving between them sees one convention."""
TESTS = []


def test(fn):
    TESTS.append(fn)
    return fn
