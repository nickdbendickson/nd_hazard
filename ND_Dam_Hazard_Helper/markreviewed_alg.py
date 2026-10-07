# -*- coding: utf-8 -*-
"""v2.12.0: Processing algorithm ndhazard:markreviewed - "Mark the
classification reviewed" (markreviewed.mark_reviewed). Registered by
processing_provider.Provider.loadAlgorithms; the "Mark reviewed..." button of
the Dam suite group (suite_tab.py) opens its dialog with the dam folder
filled in. Enum spellings come from processing_provider (compat-safe for
QGIS 3.28 - 4.x). Nothing has been run in a real QGIS."""

import os

from qgis.core import (
    QgsProcessingException, QgsProcessingParameterFile, QgsProcessingParameterEnum,
    QgsProcessingParameterNumber, QgsProcessingParameterString, QgsProcessingOutputFile,
    QgsProcessingOutputString,
)

from .processing_provider import _Base, FILE_FOLDER, NUM_INTEGER


class MarkReviewedAlgorithm(_Base):
    """Writes the reviewer's decision into <dam>/dam.json and QA/Review_Log.csv."""

    def name(self):
        return "markreviewed"

    def displayName(self):
        return "Mark the classification reviewed"

    def shortHelpString(self):
        from .markreviewed import DECISIONS
        return ("For the reviewer. Records the review of this dam's hazard classification "
                "as a 'reviewed' status in <dam folder>/dam.json (the record shared with ND "
                "Dam Breach Runner and ND Dam Breach Screening): the reviewer, the decision "
                "({}), the comments, the number of open items and the SHA-256 of the "
                "submittal package's MANIFEST.csv when the package (folder or .zip) is given, "
                "so the record says which package was reviewed. One line is appended to "
                "outputs/QA/Review_Log.csv. Nothing else in dam.json changes - no value of "
                "another tool is overwritten - and the package is never written to. The "
                "dashboard (ndhazard:portfolio) shows 'review' as the next action while open "
                "items remain. 'Returned' always counts at least one open item. The decision "
                "words are this plugin's record of the review, not a regulatory finding. Not "
                "run in a real QGIS (simulated QGIS only).".format(" / ".join(DECISIONS)))

    def initAlgorithm(self, config=None):
        from .markreviewed import DECISIONS
        P = self.addParameter
        P(QgsProcessingParameterFile("DAM_FOLDER", "Dam folder (holds dam.json and outputs/)",
                                     behavior=FILE_FOLDER))
        P(QgsProcessingParameterString("REVIEWER", "Reviewer (name) - required"))
        P(QgsProcessingParameterEnum("DECISION", "Decision", options=list(DECISIONS),
                                     defaultValue=0))
        P(QgsProcessingParameterString("COMMENTS", "Comments", defaultValue="", optional=True,
                                       multiLine=True))
        P(QgsProcessingParameterFile("PACKAGE", "Submittal package reviewed (folder or .zip; "
                                     "its MANIFEST.csv SHA-256 is recorded)", optional=True))
        P(QgsProcessingParameterNumber("OPEN_ITEMS", "Open items (0 = none)", type=NUM_INTEGER,
                                       defaultValue=0, minValue=0))
        self.addOutput(QgsProcessingOutputFile("DAM_JSON", "dam.json"))
        self.addOutput(QgsProcessingOutputFile("REVIEW_LOG", "Review_Log.csv"))
        self.addOutput(QgsProcessingOutputString("PACKAGE_SHA256", "Package MANIFEST.csv SHA-256"))
        self.addOutput(QgsProcessingOutputString("SUMMARY", "What was recorded"))

    def processAlgorithm(self, parameters, context, feedback):
        from . import markreviewed as MR
        folder = self.parameterAsFile(parameters, "DAM_FOLDER", context) or ""
        if not folder or not os.path.isdir(folder):
            raise QgsProcessingException("Dam folder not found: {}".format(folder or "(none)"))
        who = self.parameterAsString(parameters, "REVIEWER", context).strip()
        if not who:
            raise QgsProcessingException("The reviewer's name is required")
        try:
            idx = int(self.parameterAsEnum(parameters, "DECISION", context))
            decision = MR.DECISIONS[idx]
        except (TypeError, ValueError, IndexError):
            raise QgsProcessingException("Decision must be one of: {}".format(
                ", ".join(MR.DECISIONS)))
        comments = self.parameterAsString(parameters, "COMMENTS", context)
        package = self.parameterAsFile(parameters, "PACKAGE", context) or None
        n_open = self._int(parameters, "OPEN_ITEMS", context, 0)
        try:
            res = MR.mark_reviewed(folder, who, decision, comments=comments, package=package,
                                   open_items=n_open, log=lambda m: feedback.pushInfo(str(m)))
        except ValueError as e:
            raise QgsProcessingException(str(e)) from e
        except OSError as e:
            raise QgsProcessingException("Could not write the record: {}".format(e)) from e
        st = res["status"]["result"]
        summary = "{} by {} on {}: {} open item(s){}".format(
            st["decision"], st["reviewer"], res["status"]["when"], st["open_items"],
            " - package manifest sha256 {}".format(st["package_sha256"][:16])
            if st["package_sha256"] else "")
        return {"DAM_JSON": res["dam_json"], "REVIEW_LOG": res["log_csv"],
                "PACKAGE_SHA256": res["package_sha256"], "SUMMARY": summary}
