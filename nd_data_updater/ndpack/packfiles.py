# -*- coding: utf-8 -*-
"""Layers of the offline pack that come from files. Since suite 1.18.6 this module IS
nd_data_updater/damsuite/packfiles.py (sys.modules alias) - edit shared/damsuite/packfiles.py in
DWR_Plugins."""
import sys

if __package__:
    from ..damsuite import packfiles as _M
else:                                # build_nd_pack.py run as a script: the plugin folder
    from damsuite import packfiles as _M    # noqa: E402
sys.modules[__name__] = _M
