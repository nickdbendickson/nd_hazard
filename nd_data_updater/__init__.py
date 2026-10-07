"""ND Data Updater - QGIS plugin entry point."""


def classFactory(iface):  # noqa: N802 - name required by QGIS
    from .plugin import NdDataUpdaterPlugin
    return NdDataUpdaterPlugin(iface)
