"""1.7.2 (GUI check 2026-11-04): the automatic update check is off until the engineer turns it on,
as in DamSafety Data. With it on by default, every QGIS start began a network update about 20 s
later once a data folder was set. A value the engineer saved is still honoured."""
import os
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
if os.path.dirname(os.path.dirname(HERE)) not in sys.path:
    sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
from nd_data_updater.tests.registry import test  # noqa: E402


class _FakeQgsSettings:
    def __init__(self, store=None):
        self.store = dict(store or {})

    def value(self, key, default=None, type=None):                 # noqa: A002 - QgsSettings' name
        return self.store.get(key, default)

    def setValue(self, key, value):
        self.store[key] = value


def _settings_class():
    """nd_data_updater.settings.Settings, with a stand-in qgis.core when QGIS is not installed."""
    try:
        import qgis.core  # noqa: F401
    except ImportError:
        qgis = types.ModuleType("qgis")
        core = types.ModuleType("qgis.core")
        core.QgsSettings = _FakeQgsSettings
        qgis.core = core
        sys.modules.setdefault("qgis", qgis)
        sys.modules.setdefault("qgis.core", core)
    from nd_data_updater import settings
    return settings


def _make(store=None):
    settings = _settings_class()
    s = settings.Settings.__new__(settings.Settings)
    s.s = _FakeQgsSettings(store)
    return settings, s


@test
def test_auto_check_is_off_by_default():
    _settings, s = _make()
    assert s.auto_check is False


@test
def test_auto_check_keeps_a_saved_choice():
    settings = _settings_class()
    settings, s = _make({settings.PREFIX + "auto_check": True})
    assert s.auto_check is True
    s.auto_check = False
    assert s.auto_check is False
    assert s.s.store[settings.PREFIX + "auto_check"] is False
