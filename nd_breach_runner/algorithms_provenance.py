"""Processing algorithm "Verify a run manifest" (ndbreach:verifymanifest)."""
import os

from qgis.core import (QgsProcessingException, QgsProcessingOutputFile, QgsProcessingOutputString)

from .algorithms_tools import _ToolBase
from .core import provenance


class VerifyManifestAlgorithm(_ToolBase):
    def name(self):
        return "verifymanifest"

    def displayName(self):
        return "Verify a run manifest (inputs and results unchanged?)"

    def shortHelpString(self):
        return ("Every model run writes run_manifest.json in its _breach_model folder: the "
                "software versions, the LISFLOOD-FP executable and every input file with their "
                "SHA-256, the settings, and the SHA-256 of the result rasters. This tool "
                "checks a dam folder (or a manifest file) against that record and lists what "
                "changed since the run: an input file replaced, a raster edited, a different "
                "LISFLOOD-FP executable, or the manifest itself modified. Files larger than "
                "2 GB are compared by size and three 8 MB pieces.")

    def initAlgorithm(self, config=None):
        self._file("TARGET", "Dam folder or run_manifest.json", folder=False, optional=False)
        self.addOutput(QgsProcessingOutputString("RESULT", "Result"))
        self.addOutput(QgsProcessingOutputFile("MANIFEST", "Manifest"))

    def processAlgorithm(self, parameters, context, feedback):
        target = self._fileparam(parameters, "TARGET", context)
        folder = None
        if os.path.isdir(target):
            folder = target
            target = os.path.join(target, "_breach_model", provenance.MANIFEST_NAME)
        else:
            folder = os.path.dirname(os.path.dirname(target))
        if not os.path.isfile(target):
            raise QgsProcessingException("No {} at {}".format(provenance.MANIFEST_NAME, target))
        try:
            r = provenance.verify_manifest(target, folder)
        except (OSError, ValueError) as e:
            raise QgsProcessingException("Could not read the manifest: {}".format(e))
        if r["ok"]:
            msg = "OK: {} files match the manifest.".format(r["checked"])
            feedback.pushInfo(msg)
        else:
            msg = "CHANGED: " + "; ".join(r["problems"])
            for p in r["problems"]:
                feedback.reportError(p, fatalError=False)
        return {"RESULT": msg, "MANIFEST": target}


PROVENANCE_ALGORITHMS = (VerifyManifestAlgorithm,)
