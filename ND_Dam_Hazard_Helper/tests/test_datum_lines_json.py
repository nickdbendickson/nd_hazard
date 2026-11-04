"""datum_lines reads the "vertical_datum" record from run_report.json. It used json without
importing it, so the NameError was swallowed and the VERTICAL DATUM section was always empty.

    python3 -m ND_Dam_Hazard_Helper.tests.run_tests datum_lines_json
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_DIR = os.path.dirname(HERE)
if os.path.dirname(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, os.path.dirname(PLUGIN_DIR))
from ND_Dam_Hazard_Helper.tests.registry import test  # noqa: E402


@test
def datum_lines_json_reads_run_report():
    from ND_Dam_Hazard_Helper import pipeline as P
    rec = {"crest_datum": "NAVD88", "streambed_datum": "NAVD88"}
    with tempfile.TemporaryDirectory(dir=HERE) as out:
        with open(os.path.join(out, "run_report.json"), "w", encoding="utf-8") as f:
            json.dump({"vertical_datum": rec}, f)
        lines = P.datum_lines(out)
    assert lines, "no VERTICAL DATUM lines from run_report.json"
    assert lines[-1] == "(from run_report.json)", lines
