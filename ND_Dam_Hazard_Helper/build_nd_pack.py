# -*- coding: utf-8 -*-
"""Build the offline data pack for North Dakota.

Since suite 1.18.6 (packbuild 1.0) this file is only the plugin's door to the suite's ONE pack
builder, damsuite/packengine.py - the same engine in the ND Dam Suite and the DamSafety suite.
It binds this plugin's constants (addresses, workers, the data-source file) and osm_parse, then
this module IS damsuite.packengine (sys.modules alias): build(), datasets(), main() and the rest
are the engine's. Run it as before - QGIS is NOT needed:

    python build_nd_pack.py --help
    python build_nd_pack.py --list

Never edit the builder here: edit shared/damsuite/packengine.py in DWR_Plugins and run
tools/sync_damsuite.py.
"""
import os
import sys

if __package__:                      # imported by the plugin (QGIS) or a test
    from . import constants as _C
    from . import osm_parse as _O
    from .damsuite import packengine as _E
    _E.bind(_C, _O)
    sys.modules[__name__] = _E
else:                                # run as a script: the plugin folder is beside it
    _HERE = os.path.dirname(os.path.abspath(__file__))
    if _HERE not in sys.path:
        sys.path.insert(0, _HERE)
    import constants as _C           # noqa: E402  (no QGIS imports needed)
    import osm_parse as _O           # noqa: E402
    from damsuite import packengine as _E   # noqa: E402
    _E.bind(_C, _O, script=True)
    if __name__ == "__main__":
        sys.exit(_E.main())
    sys.modules[__name__] = _E
