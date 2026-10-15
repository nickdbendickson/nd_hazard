"""The 3DHP wait cap (1.4.0): mode "auto" must not spend 25 minutes on HTTP 504.

The service answers the small probe and then returns 504 to the state-sized query, so the small
probe alone was not a usable test. route_3dhp asks both questions inside one time budget; the
clock, the sleep and both probes are arguments so the decision is tested in no time at all."""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if os.path.dirname(os.path.dirname(HERE)) not in sys.path:
    sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
from nd_data_updater import packsource as PS  # noqa: E402
from nd_data_updater.tests.registry import test  # noqa: E402


class Clock:
    """A clock the probes move: every answer costs `cost` seconds."""

    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds

    def probe(self, answer, cost):
        def go():
            self.t += cost
            if isinstance(answer, Exception):
                raise answer
            return answer
        return go


@test
def test_ask_service_accepts_a_count():
    clock = Clock()
    ok, secs, detail = PS.ask_service(clock.probe({"count": 12345}, 1.0), 60.0, clock=clock,
                                      sleep=clock.sleep)
    assert ok is True
    assert secs == 1.0
    assert "12,345" in detail


@test
def test_ask_service_gives_up_when_the_budget_is_spent():
    clock = Clock()
    gateway = Exception("HTTP Error 504: Gateway Timeout")
    ok, secs, detail = PS.ask_service(clock.probe(gateway, 45.0), 60.0, tries=5, gap=3.0,
                                      clock=clock, sleep=clock.sleep)
    assert ok is False
    assert "gave up after" in detail
    # two attempts (45 s each) and then the budget stops it, instead of five
    assert 90.0 <= secs <= 100.0


@test
def test_ask_service_without_a_budget_uses_all_its_tries():
    clock = Clock()
    ok, secs, _detail = PS.ask_service(clock.probe(Exception("504"), 10.0), None, tries=3,
                                       gap=1.0, clock=clock, sleep=clock.sleep)
    assert ok is False
    assert secs == 32.0                            # 3 x 10 s of request + 2 x 1 s of waiting


@test
def test_route_3dhp_uses_the_service_when_it_answers_both_questions():
    clock = Clock()
    route, why = PS.route_3dhp(180.0, clock.probe({"count": 9}, 1.0),
                               clock.probe({"count": 500000}, 5.0), clock=clock,
                               sleep=clock.sleep)
    assert route == "service"
    assert "statewide" in why


@test
def test_route_3dhp_takes_the_file_when_the_small_question_fails():
    clock = Clock()
    route, why = PS.route_3dhp(180.0, clock.probe(Exception("HTTP 504"), 20.0),
                               clock.probe({"count": 1}, 1.0), tries=2, clock=clock,
                               sleep=clock.sleep)
    assert route == "file"
    assert "small question" in why and "National Map file" in why


@test
def test_route_3dhp_takes_the_file_when_only_the_statewide_question_fails():
    """The case that cost 25 minutes: a healthy small probe and a 504 on the real request."""
    clock = Clock()
    route, why = PS.route_3dhp(180.0, clock.probe({"count": 7}, 2.0),
                               clock.probe(Exception("HTTP Error 504: Gateway Timeout"), 60.0),
                               tries=2, clock=clock, sleep=clock.sleep)
    assert route == "file"
    assert "not the statewide one" in why
    assert clock.t < 180.0 + 60.0                  # bounded: the cap plus at most one request


@test
def test_route_3dhp_stops_when_the_small_question_ate_the_whole_allowance():
    clock = Clock()
    asked = []

    def state():
        asked.append(1)
        return {"count": 1}

    route, why = PS.route_3dhp(60.0, clock.probe({"count": 1}, 70.0), state, tries=1,
                               clock=clock, sleep=clock.sleep)
    assert route == "file"
    assert "allowance for deciding is spent" in why
    assert asked == []                             # the statewide question was never asked


@test
def test_service_answers_still_works_for_callers_that_only_want_yes_or_no():
    assert PS.service_answers(lambda: {"count": 3}, tries=1) is True
    assert PS.service_answers(lambda: {"error": "no"}, tries=1) is False
