"""Tests of ND Dam Hazard Classification Helper that run without QGIS (v2.28):

    cd <folder that holds ND_Dam_Hazard_Helper>
    python3 -m ND_Dam_Hazard_Helper.tests.run_tests

With a real PyQGIS on the interpreter the real qgis modules are used; without one,
tests/fake_qgis.py stands in. Every tests/test_*.py registers its tests through
tests/registry.py (the same layout as the ND Dam Breach Runner's tests/)."""
