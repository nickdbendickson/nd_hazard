"""Tests of 3.26.0: the Runner can accept a LISFLOOD-FP build without the arrival-depth patch
when the user opts in (NDBREACH_ALLOW_UNPATCHED), instead of always refusing it. Off by default,
and the run then carries a loud warning because arrival times mark the first wetting, not the
arrival depth. For users who cannot build a patched LISFLOOD-FP (e.g. a locked-down Mac)."""
from nd_breach_runner.tests.registry import test


@test
def v3260_patch_missing_detects_the_marker():
    from nd_breach_runner.core import lisflood as lf
    assert lf.patch_missing("... Unknown parameter ignored: inittm_depth ...")
    assert not lf.patch_missing("LISFLOOD-FP version 8.3\nSimulation finished")
    assert not lf.patch_missing("")
    assert not lf.patch_missing(None)


@test
def v3260_settings_carry_the_opt_in_flag():
    from nd_breach_runner.core import lisflood as lf
    st = lf.LisfloodSettings("lisflood")
    assert st.allow_unpatched is False          # off by default: an unpatched build is refused
    assert st.unpatched_used is False
    st2 = lf.LisfloodSettings("lisflood", allow_unpatched=True)
    assert st2.allow_unpatched is True


@test
def v3260_warning_names_the_arrival_time_problem():
    from nd_breach_runner.core import lisflood as lf
    for text in (lf.PATCH_MISSING_WARNING, lf.PATCH_MISSING_MSG):
        low = text.lower()
        assert "arrival" in low
    assert "first wetting" in lf.PATCH_MISSING_WARNING.lower()


@test
def v3260_run_settings_default_off():
    from nd_breach_runner.core import workflow as wf
    assert wf.RunSettings().allow_unpatched is False


@test
def v3260_setting_is_wired_through_provider():
    # the provider setting and the algorithms key exist and agree
    from nd_breach_runner import algorithms, provider
    assert algorithms.SET_ALLOW_UNPATCHED == "NDBREACH_ALLOW_UNPATCHED"
    assert provider.SET_ALLOW_UNPATCHED == algorithms.SET_ALLOW_UNPATCHED
