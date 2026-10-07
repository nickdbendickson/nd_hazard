# -*- coding: utf-8 -*-
"""Moved to the shared package in 2.17.0 (damsuite 1.5.0): damsuite/offline_pack.py, so the three suite
plugins read the same offline data pack. This module keeps the old import path working."""
try:
    from .damsuite import offline_pack as _m
except ImportError:                  # run as a script (build_nd_pack.py): the plugin folder is on sys.path
    from damsuite import offline_pack as _m

globals().update({_k: _v for _k, _v in vars(_m).items()
                  if not (_k.startswith("__") and _k.endswith("__"))})
del _m
