"""Mock of qgis.utils for damsuite.versions: PLUGIN_VERSIONS {folder: version} set by a test."""
PLUGIN_VERSIONS = {}
plugins = {}


def pluginMetadata(folder, key):
    if key == "version" and folder in PLUGIN_VERSIONS:
        return PLUGIN_VERSIONS[folder]
    return "__error__"
