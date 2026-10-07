"""18 - Merge dams and ponds: the DWR dams (steps 1-4) and the ponds and dugouts (step 16) as one
set of layers for steps 5-10 (core/merge.py). The Simple tab's "Both" runs it after 4 and 16."""
import os

from qgis.core import (QgsProcessingAlgorithm, QgsProcessingParameterFile, QgsProcessingException)

from ..core import merge as mg
from ..compat import FILE_FOLDER


class MergePonds(QgsProcessingAlgorithm):
    def name(self): return "mergeponds"
    def displayName(self): return "18 - Merge dams and ponds"
    def group(self): return "Dam screening"
    def groupId(self): return "dams"
    def createInstance(self): return MergePonds()

    def shortHelpString(self):
        return ("One work folder, both kinds: appends the ponds and dugouts of step 16 to copies of the dam "
                "layers of steps 3 and 4 (18_dams_all.gpkg, 18_breach_points_all.gpkg, 18_flow_arrows_all.gpkg) "
                "and gathers the stage-storage curves and terrain clips of both into 18_stage_storage and "
                "18_terrain (hard links where the file system allows, copies otherwise). Steps 5-10 then "
                "take the 18_* files when they exist. Columns one side lacks are added (empty for the "
                "other side); nothing in the 1-4 or 16 files is changed. Run it again after re-running "
                "either side. Not run in a real QGIS.")

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterFile("WORK", "Work folder (with the 3_, 4_ and 16_ files)",
                                                     behavior=FILE_FOLDER))

    def processAlgorithm(self, params, context, feedback):
        work = self.parameterAsString(params, "WORK", context)
        if not work or not os.path.isdir(work):
            raise QgsProcessingException("Work folder not found: {}".format(work))
        try:
            r = mg.merge(work)
        except (FileNotFoundError, ValueError) as e:
            raise QgsProcessingException(str(e))
        except Exception as e:                                   # noqa - sqlite3 errors
            raise QgsProcessingException("merge failed: {}".format(e))
        feedback.pushInfo("{} dam(s) + {} pond(s) -> {}".format(r["n_dams"], r["n_ponds"], r["dams"]))
        feedback.pushInfo("{} stage-storage curve(s), {} terrain clip(s)".format(r["n_curves"], r["n_terrain"]))
        for n in r["notes"]:
            feedback.pushInfo(n)
        return {"OUT": r["dams"], "OUTP": r["bpts"], "OUTL": r["arrows"], "CURVES": r["curves"],
                "TERRAIN": r["terrain"], "N_DAMS": r["n_dams"], "N_PONDS": r["n_ponds"]}
