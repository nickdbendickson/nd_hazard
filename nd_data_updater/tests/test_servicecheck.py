"""The service health check (1.4.0): reachable / slow / down, decided from one small answer.

The opener and the clock are arguments, so every case - a good answer, a slow one, an ArcGIS
error object served with HTTP 200, a gateway timeout, a dead host - is tested without a
network."""
import io
import os
import sys
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
if os.path.dirname(os.path.dirname(HERE)) not in sys.path:
    sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
from nd_data_updater import servicecheck as SC  # noqa: E402
from nd_data_updater.tests.registry import test  # noqa: E402


class Clock:
    def __init__(self, costs=None):
        self.t = 0.0
        self.costs = dict(costs or {})

    def __call__(self):
        return self.t

    def opener(self, answers):
        def go(url, timeout):
            self.t += self.costs.get(url, 0.2)
            answer = answers[url] if isinstance(answers, dict) else answers
            if isinstance(answer, Exception):
                raise answer
            return io.BytesIO(answer)
        return go


@test
def test_classify_has_three_answers():
    assert SC.classify(True, 0.4)[0] == "up"
    assert SC.classify(True, 9.0)[0] == "slow"
    assert SC.classify(False, 6.0, "HTTP 504") == ("down", "HTTP 504")
    assert "answered in 0.4 s" in SC.classify(True, 0.4)[1]


@test
def test_probe_reads_a_small_json_answer():
    clock = Clock({"u": 0.5})
    out = SC.probe("u", "json", opener=clock.opener(b'{"count": 12}'), clock=clock)
    assert out["state"] == "up" and out["seconds"] == 0.5


@test
def test_probe_calls_a_slow_answer_slow():
    clock = Clock({"u": 4.0})
    out = SC.probe("u", "json", opener=clock.opener(b'{"currentVersion": 11.2}'), clock=clock)
    assert out["state"] == "slow"


@test
def test_probe_treats_an_arcgis_error_object_as_down():
    """The services answer HTTP 200 with {"error": ...}; that is not an answer."""
    clock = Clock()
    body = b'{"error": {"code": 500, "message": "Unable to complete operation"}}'
    out = SC.probe("u", "json", opener=clock.opener(body), clock=clock)
    assert out["state"] == "down"
    assert "Unable to complete operation" in out["detail"]


@test
def test_probe_names_a_gateway_timeout():
    clock = Clock()
    err = urllib.error.HTTPError("u", 504, "Gateway Timeout", {}, None)
    out = SC.probe("u", "json", opener=clock.opener(err), clock=clock)
    assert out["state"] == "down"
    assert out["detail"] == "HTTP 504 (gateway timeout)"


@test
def test_probe_survives_a_dead_host_and_rubbish_json():
    clock = Clock()
    out = SC.probe("u", "json", opener=clock.opener(OSError("Name or service not known")),
                   clock=clock)
    assert out["state"] == "down" and "Name or service" in out["detail"]
    out = SC.probe("u", "json", opener=clock.opener(b"<html>a proxy page</html>"), clock=clock)
    assert out["state"] == "down" and "not usable" in out["detail"]
    out = SC.probe("u", "head", opener=clock.opener(b"<html>a proxy page</html>"), clock=clock)
    assert out["state"] == "up"              # "head": any answer at all will do


@test
def test_check_returns_one_row_per_service_in_order():
    clock = Clock()
    services = (("a", "Service A", "ua", "json", "what a is for"),
                ("b", "Service B", "ub", "head", "what b is for"))
    rows = SC.check(services, opener=clock.opener({"ua": b'{"count": 1}', "ub": b"ok"}),
                    clock=clock, workers=0)
    assert [r["key"] for r in rows] == ["a", "b"]
    assert rows[0]["what"] == "what a is for"
    assert SC.worst(rows) == "up"
    assert SC.warning_text(rows) == ""


@test
def test_check_runs_the_probes_in_parallel_without_losing_a_row():
    clock = Clock()
    services = tuple((str(i), f"S{i}", f"u{i}", "head", "x") for i in range(6))
    rows = SC.check(services, opener=clock.opener(b"ok"), clock=clock)
    assert len(rows) == 6 and {r["state"] for r in rows} == {"up"}


@test
def test_warning_text_tells_a_user_what_to_do_about_3dhp():
    rows = [{"key": "3dhp_state", "label": "USGS 3DHP (statewide)", "state": "down",
             "seconds": 6.0, "detail": "HTTP 504"},
            {"key": "nsi", "label": "NSI", "state": "up", "seconds": 0.4, "detail": ""}]
    text = SC.warning_text(rows)
    assert "down: USGS 3DHP (statewide)" in text
    assert "National Map file" in text
    assert SC.blocking(rows) and SC.blocking(rows)[0]["key"] == "3dhp_state"
    assert SC.worst(rows) == "down"


@test
def test_warning_text_mentions_the_layers_that_would_be_left_out():
    rows = [{"key": "mrlc", "label": "MRLC land cover", "state": "down", "seconds": None,
             "detail": "timed out"}]
    text = SC.warning_text(rows)
    assert "left out of the pack" in text
    assert SC.blocking(rows) == []
    assert "MRLC land cover" in SC.text_report(rows)


@test
def test_the_shipped_service_list_is_sane():
    keys = [s[0] for s in SC.SERVICES]
    assert set(("nsi", "3dhp", "3dhp_state", "nid", "fema", "dwr", "mrlc")) <= set(keys)
    for _key, label, url, kind, what in SC.SERVICES:
        assert url.startswith("https://") and label and what
        assert kind in ("json", "head")
