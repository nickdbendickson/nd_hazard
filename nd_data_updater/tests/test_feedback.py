"""The one-line status a long run shows (1.4.0): the channel itself, and the in-place source
(the offline pack) getting it. Without this a 90-minute build left the user watching a bar that
did not move and no name of what it was on."""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
if os.path.dirname(os.path.dirname(HERE)) not in sys.path:
    sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
from nd_data_updater import core, runreport  # noqa: E402
from nd_data_updater.tests.registry import test  # noqa: E402


class Pretend(core.Source):
    """A source that builds in place and talks the way the pack builder does."""

    in_place = True

    def __init__(self, id="pack", name="Offline pack"):
        self.id = id
        self.name = name
        self.folder = id
        self.seen = []

    def update(self, http, target, feedback, force, entry):
        steps = runreport.StepTracker()
        for line in ("[1/2] USACE National Structure Inventory", "  done: 5 features in 1 s",
                     "[2/2] ND roads"):
            steps.note(line)
            feedback.log(line)
            feedback.status(steps.line("downloading"))
        feedback.fraction(1.0)
        return "updated", "2 layers", "built:2026-10-13:2"


@test
def test_feedback_status_goes_where_it_is_asked_and_nowhere_else():
    said = []
    fb = core.Feedback(status=said.append)
    fb.status("layer 1 of 2: x")
    fb.status("")                                  # nothing to say: nothing is said
    assert said == ["layer 1 of 2: x"]
    core.Feedback().status("no callback is not a failure")


@test
def test_check_and_update_names_the_dataset_it_is_on():
    with tempfile.TemporaryDirectory() as d:
        said = []
        fb = core.Feedback(status=said.append, log=lambda m: None)
        updater = core.Updater(d, [Pretend("a", "NSI"), Pretend("b", "Offline pack")], None)
        results = updater.check_and_update(fb, force=True)
        assert [r.status for r in results] == ["updated", "updated"]
        assert said[0] == "NSI (1 of 2)"
        assert "Offline pack (2 of 2)" in said


@test
def test_the_in_place_source_reports_the_layer_it_is_on():
    with tempfile.TemporaryDirectory() as d:
        said = []
        fb = core.Feedback(status=said.append, log=lambda m: None)
        core.Updater(d, [Pretend()], None).check_and_update(fb, force=True)
        assert "layer 1 of 2: USACE National Structure Inventory - downloading" in said
        assert "layer 2 of 2: ND roads - downloading" in said
