# -*- coding: utf-8 -*-
"""Update engine of the data updater.
Since suite 1.18.8 this module IS damsuite/updatecore.py (sys.modules alias),
the same in the ND Data Updater and DamSafety Data - edit shared/damsuite/updatecore.py in
DWR_Plugins."""
import sys

from .damsuite import updatecore as _M

sys.modules[__name__] = _M
