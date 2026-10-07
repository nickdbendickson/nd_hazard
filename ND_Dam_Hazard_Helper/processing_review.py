# -*- coding: utf-8 -*-
"""v2.10: Processing algorithms of the reviewer's view (reviewpkg.py).

  ndhazard:reviewpackage     review a submittal package (folder or .zip) in a work
                             folder: file check against MANIFEST.csv, model chain,
                             optional reproduction, checklist (Review_Report.html,
                             Review_Checklist.csv)
  ndhazard:reviewsummary     re-read a filled Review_Checklist.csv and report the
                             decisions (Accept / Question / Not applicable)

Registered by processing_provider.Provider.loadAlgorithms (integration note in
/home/claude/suite/integration/chain_submittal.patch.md). Enum spellings come from
processing_provider (compat-safe for QGIS 3.28 - 4.x). Nothing has been run in a real
QGIS.
"""

import os

from qgis.core import (
    QgsProcessingException, QgsProcessingParameterFile, QgsProcessingParameterFolderDestination,
    QgsProcessingParameterBoolean, QgsProcessingParameterString, QgsProcessingOutputFile,
    QgsProcessingOutputString,
)

from .helpers import CancelledByUser
from .processing_provider import _Base, _progress, FILE_FOLDER, FILE_FILE, NO_THREADING


class ReviewPackageAlgorithm(_Base):
    """Reviewer's view of a submittal package (reviewpkg.review_package)."""

    def name(self):
        return "reviewpackage"

    def displayName(self):
        return "Review a submittal package (checklist)"

    def shortHelpString(self):
        return ("For the reviewer. Reads a submittal package (its folder, or the .zip - "
                "unzipped into the work folder) and NEVER writes into it. In the work "
                "folder: Review_Files_Check.csv (every file against the package's "
                "MANIFEST.csv SHA-256: missing / changed / extra), Model_Chain_Check.csv "
                "(the rasters the classification read against the ND Dam Breach Runner's "
                "run manifest packaged with it), optionally the reproduction of the run "
                "(reproduction/Reproduction_Check.csv), and Review_Report.html + "
                "Review_Checklist.csv: each item with an automatic finding and EMPTY "
                "decision (Accept / Question / Not applicable) and comment columns. The "
                "items are this tool's own checks, not the State's review criteria or "
                "regulatory requirements. Re-read the filled checklist with 'Summarize a "
                "filled review checklist'.")

    def flags(self):
        return super().flags() | NO_THREADING       # the reproduction runs the pipeline

    def initAlgorithm(self, config=None):
        P = self.addParameter
        P(QgsProcessingParameterFile("PACKAGE_FOLDER", "Package folder", behavior=FILE_FOLDER,
                                     optional=True))
        P(QgsProcessingParameterFile("PACKAGE_ZIP", "... or the package .zip",
                                     behavior=FILE_FILE, extension="zip", optional=True))
        P(QgsProcessingParameterFolderDestination("WORK_FOLDER", "Work folder (the review "
                                                  "files go here, never into the package)"))
        P(QgsProcessingParameterBoolean("REPRODUCE", "Also reproduce the run (re-runs the "
                                        "classification; takes as long as the run)",
                                        defaultValue=False))
        P(QgsProcessingParameterFile("RASTERS_FOLDER", "Folder with the flood rasters (for "
                                     "the reproduction, if they are not in the package)",
                                     behavior=FILE_FOLDER, optional=True))
        P(QgsProcessingParameterString("REVIEWER", "Reviewer (name, written into the "
                                       "checklist)", defaultValue="", optional=True))
        self.addOutput(QgsProcessingOutputFile("REPORT", "Review_Report.html"))
        self.addOutput(QgsProcessingOutputFile("CHECKLIST", "Review_Checklist.csv"))
        self.addOutput(QgsProcessingOutputFile("FILES_CHECK", "Review_Files_Check.csv"))
        self.addOutput(QgsProcessingOutputFile("CHAIN_CSV", "Model_Chain_Check.csv"))
        self.addOutput(QgsProcessingOutputString("CHAIN_STATUS", "Model chain status"))
        self.addOutput(QgsProcessingOutputString("REPRODUCTION", "Reproduction status"))
        self.addOutput(QgsProcessingOutputString("SUMMARY", "Automatic flags"))

    def processAlgorithm(self, parameters, context, feedback):
        from . import reviewpkg as RV
        folder = self.parameterAsFile(parameters, "PACKAGE_FOLDER", context) or ""
        zpath = self.parameterAsFile(parameters, "PACKAGE_ZIP", context) or ""
        if folder and zpath:
            raise QgsProcessingException("Give the package folder OR its .zip, not both")
        pkg = folder or zpath
        if not pkg or not os.path.exists(pkg):
            raise QgsProcessingException("Package not found: {}".format(pkg or "(none given)"))
        work = self.parameterAsString(parameters, "WORK_FOLDER", context)
        if not work:
            raise QgsProcessingException("Choose a work folder")
        ras = self.parameterAsFile(parameters, "RASTERS_FOLDER", context) or None
        who = self.parameterAsString(parameters, "REVIEWER", context).strip()
        try:
            res = RV.review_package(pkg, work, reproduce=self.parameterAsBool(
                parameters, "REPRODUCE", context), reviewer=who, rasters_folder=ras,
                progress=_progress(feedback), log=lambda m: feedback.pushInfo(str(m)))
        except CancelledByUser as e:
            raise QgsProcessingException("Cancelled") from e
        except (ValueError, OSError) as e:
            raise QgsProcessingException(str(e)) from e
        for it in res["checklist"]:
            feedback.pushInfo("{} {} [{}]: {}".format(it["Item_ID"], it["Item"], it["Flag"],
                                                      it["Automatic_Finding"]))
        repro = res.get("reproduction")
        summary = ", ".join("{} {}".format(v, k) for k, v in sorted(res["flags"].items()))
        return {"REPORT": res["report"], "CHECKLIST": res["checklist_csv"],
                "FILES_CHECK": res["files_csv"], "CHAIN_CSV": res["chain"].get("csv") or "",
                "CHAIN_STATUS": res["chain"].get("status") or "",
                "REPRODUCTION": (repro or {}).get("status") or "NOT RUN",
                "SUMMARY": summary}


class ReviewSummaryAlgorithm(_Base):
    """Counts of the decisions of a filled review checklist (reviewpkg.summarize_checklist)."""

    def name(self):
        return "reviewsummary"

    def displayName(self):
        return "Summarize a filled review checklist"

    def shortHelpString(self):
        return ("Re-reads a Review_Checklist.csv the reviewer filled in and reports the "
                "decisions: how many items Accept / Question / Not applicable / no decision, "
                "the questions with their comments, decisions not recognised, and items "
                "accepted despite an automatic CHECK flag without a comment. Nothing is "
                "written.")

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterFile("CHECKLIST", "Filled Review_Checklist.csv",
                                                     behavior=FILE_FILE, extension="csv"))
        self.addOutput(QgsProcessingOutputString("SUMMARY", "Decisions"))
        self.addOutput(QgsProcessingOutputString("QUESTIONS", "Items questioned"))

    def processAlgorithm(self, parameters, context, feedback):
        from . import reviewpkg as RV
        path = self.parameterAsFile(parameters, "CHECKLIST", context)
        if not path or not os.path.isfile(path):
            raise QgsProcessingException("Checklist not found: {}".format(path))
        try:
            s = RV.summarize_checklist(path)
        except ValueError as e:
            raise QgsProcessingException(str(e)) from e
        for line in s["lines"]:
            feedback.pushInfo(line)
        return {"SUMMARY": s["lines"][0],
                "QUESTIONS": "; ".join("{} {}".format(i, c) for i, _it, c in s["questions"])}


def algorithms():
    """The algorithms of this module, for Provider.loadAlgorithms."""
    return [ReviewPackageAlgorithm(), ReviewSummaryAlgorithm()]
