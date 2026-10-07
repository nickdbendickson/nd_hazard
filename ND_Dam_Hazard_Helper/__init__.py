# -*- coding: utf-8 -*-
def _install_transform_guard():
    """Re-create intermittently invalid coordinate transformations (see damsuite/transformguard.py).
    Loaded by file so it runs before any plugin module imports QgsCoordinateTransform."""
    try:
        import importlib.util
        import os
        spec = importlib.util.spec_from_file_location(
            "_nd_transformguard", os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                               "damsuite", "transformguard.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mod.install()
    except Exception:
        pass


_install_transform_guard()

def classFactory(iface):
    """QGIS plugin entry point."""
    from .plugin import NDdamHazardHelperPlugin
    return NDdamHazardHelperPlugin(iface)
