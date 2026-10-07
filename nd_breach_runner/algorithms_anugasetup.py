"""Processing algorithm "Set up ANUGA (one-time download)" (ndbreach:setupanuga).

Creates the private ANUGA environment of core/anuga_setup.py (micromamba from GitHub, ANUGA
from conda-forge, in the plugin's data folder) and points the provider setting "ANUGA Python"
at it. No conda install, no administrator rights; needs the internet once.
"""
from qgis.core import QgsProcessingException, QgsProcessingOutputString

from .algorithms import SET_ANUGA
from .algorithms_tools import _ToolBase
from .core import anuga_setup


def save_setting(python):
    """Put `python` into the provider setting (Processing > Options > ND Dam Breach Runner)."""
    try:
        from processing.core.ProcessingConfig import ProcessingConfig
        if SET_ANUGA in ProcessingConfig.settings:           # saves it to the profile as well
            ProcessingConfig.setSettingValue(SET_ANUGA, python)
            return True
        from qgis.core import QgsSettings                    # provider not loaded in this process
        QgsSettings().setValue("Processing/Configuration/" + SET_ANUGA, python)
        return True
    except Exception:                                            # noqa
        return False


class SetupAnugaAlgorithm(_ToolBase):
    def group(self):
        return "Dam breach modelling"

    def groupId(self):
        return "breach"

    def name(self):
        return "setupanuga"

    def displayName(self):
        return "Set up ANUGA (one-time download)"

    def shortHelpString(self):
        return ("Installs ANUGA for the Runner's ANUGA tier without installing conda yourself: "
                "downloads micromamba (one small program, no administrator rights), checks it "
                "against a pinned SHA-256, and creates a private environment with ANUGA from "
                "conda-forge in the plugin's own folder ({}). Then sets 'ANUGA Python' in the "
                "provider options to that environment.\n\n"
                "Needs the internet once (a few hundred MB to download, about 1.6 GB on disk, a "
                "few minutes) and works on Windows 64-bit, Linux x86-64 and macOS. ANUGA is "
                "optional: LISFLOOD-FP or the built-in engine need none of this. Running it "
                "again does nothing when ANUGA already works; tick 'Create it again' to rebuild, "
                "or 'Remove' to delete the environment.".format(anuga_setup.data_dir()))

    def initAlgorithm(self, config=None):
        self._bool("REINSTALL", "Create the environment again even if ANUGA already works", False)
        self._bool("REMOVE", "Remove the private ANUGA environment instead (frees the disk "
                   "space; clears 'ANUGA Python' when it points there)", False, advanced=True)
        self.addOutput(QgsProcessingOutputString("PYTHON", "ANUGA Python interpreter"))

    def processAlgorithm(self, parameters, context, feedback):
        if self.parameterAsBool(parameters, "REMOVE", context):
            py = anuga_setup.env_python()
            gone = anuga_setup.remove()
            from .algorithms import _setting
            cur = _setting(SET_ANUGA)
            if cur and cur == py:
                save_setting("")
            feedback.pushInfo("Removed {}".format(anuga_setup.data_dir()) if gone else
                              "Nothing to remove: {} does not exist".format(anuga_setup.data_dir()))
            return {"PYTHON": ""}
        feedback.setProgress(2)
        try:
            py = anuga_setup.setup(reinstall=self.parameterAsBool(parameters, "REINSTALL", context),
                                   log=feedback.pushInfo, cancel=feedback.isCanceled)
        except anuga_setup.Cancelled:
            raise QgsProcessingException("Cancelled")
        except anuga_setup.SetupError as e:
            raise QgsProcessingException(str(e))
        saved = save_setting(py)
        feedback.pushInfo("'ANUGA Python' is now {}{}".format(
            py, "" if saved else " (could not be written to the options; the Runner finds this "
            "environment on its own anyway)"))
        feedback.setProgress(100)
        return {"PYTHON": py}


SETUPANUGA_ALGORITHMS = (SetupAnugaAlgorithm,)
