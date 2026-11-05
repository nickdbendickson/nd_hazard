# -*- coding: utf-8 -*-
"""Merge two NSI versions (2022 and 2026) into one point inventory.
Since suite 1.18.8 this module IS damsuite/nsi_merge.py (sys.modules alias),
the same in the ND Data Updater and DamSafety Data - edit shared/damsuite/nsi_merge.py in
DWR_Plugins."""
import sys

from .damsuite import nsi_merge as _M

sys.modules[__name__] = _M
