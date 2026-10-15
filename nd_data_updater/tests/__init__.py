"""Tests of the ND Data Updater that run without QGIS:

    cd <folder that holds nd_data_updater>
    /usr/bin/python3.12 -m nd_data_updater.tests.run_tests

Nothing here imports qgis: the logic the tests cover (the lock file, the 3DHP decision, the
plain-words run summary, the pack change report, the USB copy's checksums, the data vintage and
the pack's build plan) lives in pure-Python modules for exactly that reason. The Qt windows are
layout and wiring over those modules and are not tested here."""
