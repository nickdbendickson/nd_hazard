# -*- coding: utf-8 -*-
"""The Updater's offline pack builder. Since suite 1.18.6 (packbuild 1.0) this is the suite's
ONE pack builder, nd_data_updater/damsuite/packengine.py (the same engine as the ND Dam Hazard
Helper and the DamSafety plugins), bound to ndpack's constants and osm_parse (copies of the
Helper's); after the import this module IS damsuite.packengine (sys.modules alias).

    python ndpack/build_nd_pack.py --help

Never edit the builder here: edit shared/damsuite/packengine.py in DWR_Plugins and re-vendor.
"""
import os
import sys

if __package__:                      # imported by the Updater (QGIS) or a test
    from . import constants as _C
    from . import osm_parse as _O
    from ..damsuite import packengine as _E
    _E.bind(_C, _O)
    sys.modules[__name__] = _E
else:                                # run as a script: ndpack/ and the plugin folder on the path
    _HERE = os.path.dirname(os.path.abspath(__file__))
    for _p in (_HERE, os.path.dirname(_HERE)):     # the plugin's damsuite wins (inserted last)
        if _p in sys.path:
            sys.path.remove(_p)
        sys.path.insert(0, _p)
    import constants as _C           # noqa: E402  (ndpack/constants.py)
    import osm_parse as _O           # noqa: E402
    from damsuite import packengine as _E   # noqa: E402  (nd_data_updater/damsuite)
    _E.bind(_C, _O, script=True)
    if __name__ == "__main__":
        sys.exit(_E.main())
    sys.modules[__name__] = _E
