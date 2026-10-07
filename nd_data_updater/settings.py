"""Plugin settings, stored with QgsSettings under nd_data_updater/."""

import os

from qgis.core import QgsSettings

from .sources import SOURCE_INFO

PREFIX = "nd_data_updater/"
DEFAULT_DATA_DIR = os.path.join(os.path.expanduser("~"), "Documents", "ND Structure Data")


class Settings:
    def __init__(self):
        self.s = QgsSettings()

    def _get(self, key, default, type_):
        value = self.s.value(PREFIX + key, default, type=type_)
        return default if value is None else value

    def _set(self, key, value):
        self.s.setValue(PREFIX + key, value)

    @property
    def data_dir(self):
        return self._get("data_dir", DEFAULT_DATA_DIR, str) or DEFAULT_DATA_DIR

    @data_dir.setter
    def data_dir(self, v):
        self._set("data_dir", v)

    @property
    def auto_check(self):
        return self._get("auto_check", True, bool)

    @auto_check.setter
    def auto_check(self, v):
        self._set("auto_check", bool(v))

    @property
    def check_hours(self):
        return self._get("check_hours", 24, int)

    @check_hours.setter
    def check_hours(self, v):
        self._set("check_hours", int(v))

    @property
    def nsi_interval_days(self):
        return self._get("nsi_interval_days", 30, int)

    @nsi_interval_days.setter
    def nsi_interval_days(self, v):
        self._set("nsi_interval_days", int(v))

    @property
    def nsi_mode(self):
        value = self._get("nsi_mode", "merged", str)
        return value if value in ("merged", "nsi2026", "nsi2022") else "merged"

    @nsi_mode.setter
    def nsi_mode(self, v):
        self._set("nsi_mode", v)

    @property
    def nsi_match_m(self):
        return self._get("nsi_match_m", 30, int)

    @nsi_match_m.setter
    def nsi_match_m(self, v):
        self._set("nsi_match_m", int(v))

    @property
    def usa_structures_url(self):
        return self._get("usa_structures_url", "", str)

    @usa_structures_url.setter
    def usa_structures_url(self, v):
        self._set("usa_structures_url", v.strip())

    @property
    def last_auto_check(self):
        return self._get("last_auto_check", 0.0, float)

    @last_auto_check.setter
    def last_auto_check(self, v):
        self._set("last_auto_check", float(v))

    def enabled(self, source_id):
        return self._get("enabled/" + source_id, SOURCE_INFO[source_id][2], bool)

    def set_enabled(self, source_id, value):
        self._set("enabled/" + source_id, bool(value))

    def source_config(self):
        return {
            "enabled": {sid: self.enabled(sid) for sid in SOURCE_INFO},
            "usa_structures_url": self.usa_structures_url,
            "nsi_interval_days": self.nsi_interval_days,
            "nsi_mode": self.nsi_mode,
            "nsi_match_m": self.nsi_match_m,
        }
