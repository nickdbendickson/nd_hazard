# CI

`.github/workflows/ci.yml` runs on every pull request and every push to `main`. The repository
is public, so GitHub-hosted runner minutes are free.

| Job | What it runs | Time |
|---|---|---|
| `fast-checks` | `tools/ci_checks.py` (damsuite pin parity, versions, qt6 enums, compile), `tools/test_ci_checks.py`, the fingerprint nested-VRT test. No QGIS. | < 1 min |
| `plugin-suites` | `run_tests` for ND_Dam_Hazard_Helper, nd_breach_runner and nd_data_updater, every `nd_dam_screening/tests/test_*.py`, `tools/gui_lint.py`, on QGIS from conda-forge. | ~5 min |
| `model-tests` | `python -m nd_breach_runner.tests.run_tests --models` on the shipped `bin/linux-x64/lisflood` and ANUGA 4.0.1 in its own conda env. | ~7 min |

Every job fails the run when it fails. A newer push to a PR cancels that PR's older run.

## Making the checks required (repository owner)

Settings > Rules > Rulesets > New branch ruleset: target the default branch, turn on
"Require status checks to pass", and add `fast-checks`, `plugin-suites` and `model-tests`.
Without a ruleset, GitHub lets a PR merge while its checks are red or have not run.

## When no job starts

A run whose jobs fail in a few seconds with no steps, annotated "The job was not started because
recent account payments have failed or your spending limit needs to be increased", is an account
billing lock, not a test failure. Fix the payment method under the account's
Settings > Billing and licensing, then re-run the jobs.

## Running CI locally

The cloud sessions have QGIS 3.34 with numpy 1.26 and scipy at `/usr/bin/python3.12`, which is
enough for all three jobs. From the repository root:

```bash
PY=/usr/bin/python3.12
export QT_QPA_PLATFORM=offscreen

# fast-checks
$PY tools/ci_checks.py
$PY tools/test_ci_checks.py
$PY ND_Dam_Hazard_Helper/tests/test_fingerprint_vrt.py

# plugin-suites
for p in ND_Dam_Hazard_Helper nd_breach_runner nd_data_updater; do $PY -m $p.tests.run_tests; done
for t in nd_dam_screening/tests/test_*.py; do $PY "$t"; done
$PY -X faulthandler tools/gui_lint.py

# model-tests (needs an ANUGA python; without it the ANUGA tests are skipped)
chmod +x nd_breach_runner/bin/linux-x64/lisflood
NDBREACH_LISFLOOD="$PWD/nd_breach_runner/bin/linux-x64/lisflood" \
NDBREACH_ANUGA_PYTHON=/path/to/anuga/env/bin/python \
  $PY -m nd_breach_runner.tests.run_tests --models
```

An ANUGA env like CI's: `micromamba create -y -n anuga -c conda-forge python=3.12 anuga=4.0.1 scipy`.
