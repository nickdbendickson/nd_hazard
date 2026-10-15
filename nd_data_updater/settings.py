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

    @staticmethod
    def _suite_folder():
        """1.5.0: the suite data folder (ndsuite/data_folder) the three dam plugins read, or ""."""
        try:
            from .ndpack.damsuite import suitedata as SD
            return (SD.data_folder() or "").strip()
        except Exception:                  # the suite package is optional; never raise here
            return ""

    @property
    def data_dir(self):
        # 1.5.0: one data folder for the whole suite. The suite folder the dam plugins read wins,
        # so a folder chosen in the suite home reaches the updater; the updater's own old key is
        # used only for a profile that never set the suite folder. Before, these were two
        # settings, so a folder set in the home left the updater on its default and its first
        # quiet run built a second pack there and then moved the suite onto it.
        return (self._suite_folder()
                or self._get("data_dir", "", str)
                or DEFAULT_DATA_DIR)

    @data_dir.setter
    def data_dir(self, v):
        v = (v or "").strip()
        self._set("data_dir", v)           # keep the old key for an older suite that lacks the new
        try:
            from .ndpack.damsuite import suitedata as SD
            if v:
                SD.set_data_folder(v)      # the dam plugins read this one
        except Exception:
            pass

    @property
    def data_dir_is_set(self):
        """1.5.0: True once a data folder has been chosen (here or in the suite home). The quiet
        auto-check must not start a build before this, or it downloads gigabytes unasked into a
        default folder on a fresh machine."""
        return bool(self._suite_folder() or self._get("data_dir", "", str))

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
    def pack_days(self):
        return self._get("pack_days", 30, int)

    @pack_days.setter
    def pack_days(self, v):
        self._set("pack_days", int(v))

    @property
    def pack_3dhp(self):
        value = self._get("pack_3dhp", "auto", str)
        return value if value in ("auto", "file", "service") else "auto"

    @pack_3dhp.setter
    def pack_3dhp(self, v):
        self._set("pack_3dhp", v)

    @property
    def pack_3dhp_minutes(self):
        """How long mode "auto" may spend finding out whether the 3DHP service is usable."""
        return self._get("pack_3dhp_minutes", 3, int)

    @pack_3dhp_minutes.setter
    def pack_3dhp_minutes(self, v):
        self._set("pack_3dhp_minutes", max(1, int(v)))

    @property
    def service_check_on_open(self):
        """Probe the services when the pack window opens (off by default: it costs a few
        seconds of network, and a user who is only changing a setting did not ask for it)."""
        return self._get("service_check_on_open", False, bool)

    @service_check_on_open.setter
    def service_check_on_open(self, v):
        self._set("service_check_on_open", bool(v))

    @property
    def pack_share(self):
        return self._get("pack_share", True, bool)

    @pack_share.setter
    def pack_share(self, v):
        self._set("pack_share", bool(v))

    @property
    def pack_full(self):
        """One-shot: fetch every pack layer again at the next check (cleared when it starts)."""
        return self._get("pack_full", False, bool)

    @pack_full.setter
    def pack_full(self, v):
        self._set("pack_full", bool(v))

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
            "data_dir": self.data_dir,
            "pack_days": self.pack_days,
            "pack_3dhp": self.pack_3dhp,
            "pack_3dhp_minutes": self.pack_3dhp_minutes,
            "pack_share": self.pack_share,
            "pack_full": self.pack_full,
        }
