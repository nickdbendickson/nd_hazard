"""Mock of `from qgis import processing`: dispatches to registered mock algorithms.

REGISTRY keys are either an algorithm name ('hazardclassification', the screening's own tools)
or a full id ('ndbreach:rundam', test doubles of another provider); the full id wins.
Every call is appended to CALLS as (alg_id, params)."""
REGISTRY = {}
CALLS = []


def run(alg_id, params, context=None, feedback=None, is_child_algorithm=False):
    CALLS.append((alg_id, dict(params)))
    cls = REGISTRY.get(alg_id) or REGISTRY[alg_id.split(":")[-1]]
    alg = cls.createInstance()
    p = {k: (None if v == "TEMPORARY_OUTPUT" else v) for k, v in params.items()}
    return alg.run_mock(p, context, feedback)


# the popup window opens a tool's dialog: recorded as (alg_id, params); FAIL makes it raise
DIALOGS = []
FAIL = []


def execAlgorithmDialog(alg_id, params=None):
    if FAIL:
        raise RuntimeError(FAIL[0])
    DIALOGS.append((alg_id, dict(params or {})))
    return {}
