# -*- coding: utf-8 -*-
"""OpenStreetMap layers for the offline pack (Geofabrik .osm.pbf).
Since suite 1.18.8 this module IS damsuite/osm_layers.py (sys.modules alias),
the same in the ND Data Updater and DamSafety Data - edit shared/damsuite/osm_layers.py in
DWR_Plugins."""
import sys

from .damsuite import osm_layers as _M

sys.modules[__name__] = _M
