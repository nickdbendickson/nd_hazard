"""H06 residual: a truncated run report is not a finished run.

run_finished() used to return True on any parse error once the three final rasters
existed, so a kill during json.dump (the report was written in place) still made the
batch skip the dam. A readable report with a truthy run_finished stamp is finished;
a truncated file, a report with no stamp, or rasters alone are not.
"""
import json
import os
import tempfile

from nd_breach_runner.core import workflow
from nd_breach_runner.tests.registry import test


def _rasters(folder):
    for key in ("depth", "velocity", "arrival"):
        with open(os.path.join(folder, workflow.FINAL_NAMES[key]), "w", encoding="utf-8") as fh:
            fh.write("x")


@test
def run_finished_rejects_a_truncated_report():
    folder = tempfile.mkdtemp()
    _rasters(folder)
    assert workflow.run_finished(folder) is False
    model = os.path.join(folder, workflow.MODEL_FOLDER)
    os.makedirs(model)
    rep = os.path.join(model, "run_report.json")
    with open(rep, "w", encoding="utf-8") as fh:
        fh.write("{")
    assert workflow.run_finished(folder) is False
    with open(rep, "w", encoding="utf-8") as fh:
        json.dump({"summary": "no stamp"}, fh)
    assert workflow.run_finished(folder) is False
    with open(rep, "w", encoding="utf-8") as fh:
        json.dump({"run_finished": ""}, fh)
    assert workflow.run_finished(folder) is False
    with open(rep, "w", encoding="utf-8") as fh:
        json.dump({"run_finished": "2026-11-08T00:00:00"}, fh)
    assert workflow.run_finished(folder) is True


@test
def write_json_replaces_the_report_only_when_complete():
    folder = tempfile.mkdtemp()
    path = os.path.join(folder, "run_report.json")
    workflow._write_json(path, {"run_finished": "2026-11-08T00:00:00"})
    with open(path, encoding="utf-8") as fh:
        assert json.load(fh)["run_finished"] == "2026-11-08T00:00:00"
    assert not os.path.exists(path + ".tmp")
    workflow._write_json(path, {"run_finished": "2026-11-08T01:00:00", "n": 1})
    with open(path, encoding="utf-8") as fh:
        got = json.load(fh)
    assert got["run_finished"] == "2026-11-08T01:00:00" and got["n"] == 1
    assert not os.path.exists(path + ".tmp")
