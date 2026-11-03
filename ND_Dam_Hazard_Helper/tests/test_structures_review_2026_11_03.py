# -*- coding: utf-8 -*-
"""Review of 2026-11-03 (Lumo), structures.py punch items 26-28.

  26 velocity NaN on a wet cell is filled with the structure's peak velocity, not 0 (the
     optimistic direction on the ACER curves)
  27 collapse_dwellings is removed - calling it raises, so the address-key collapse that zeroed
     real second households cannot be reinstated by accident
  28 the same-family snap drop keeps a genuine second dwelling unit (different NSI fd_id or a
     different address), matching _is_duplicate

    python3 -m ND_Dam_Hazard_Helper.tests.run_tests structures_review
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_DIR = os.path.dirname(HERE)
if os.path.dirname(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, os.path.dirname(PLUGIN_DIR))
from ND_Dam_Hazard_Helper.tests import fake_qgis  # noqa: E402
from ND_Dam_Hazard_Helper.tests.registry import test  # noqa: E402

fake_qgis.install()


@test
def velocity_nan_is_filled_with_the_peak_not_zero():
    import numpy as np
    from ND_Dam_Hazard_Helper import structures as ST
    v = np.array([np.nan, 2.0, 7.0, np.nan])
    out = ST._fill_missing_velocity(v)
    assert list(out) == [7.0, 2.0, 7.0, 7.0], out        # gaps take the structure's peak (7)
    assert not np.isnan(out).any()


@test
def velocity_all_nan_falls_back_to_zero_depth_only():
    import numpy as np
    from ND_Dam_Hazard_Helper import structures as ST
    out = ST._fill_missing_velocity(np.array([np.nan, np.nan]))
    assert list(out) == [0.0, 0.0], out


@test
def peak_pair_now_sees_the_filled_deep_cell():
    # the deepest cell lost its velocity; after the fill it carries the peak, so the
    # depth x velocity pair is no longer read on the optimistic side
    import numpy as np
    from ND_Dam_Hazard_Helper import structures as ST
    d = np.array([9.0, 1.0])          # deep cell first
    v = np.array([np.nan, 6.0])       # deep cell has no velocity
    v = ST._fill_missing_velocity(v)
    dpk, vpk = ST.peak_pair(d, v, True)
    assert (float(dpk[0]), float(vpk[0])) == (9.0, 6.0), (dpk, vpk)


@test
def collapse_dwellings_is_removed_and_raises():
    from ND_Dam_Hazard_Helper import structures as ST
    raised = False
    try:
        ST.collapse_dwellings([])
    except NotImplementedError:
        raised = True
    assert raised, "collapse_dwellings must raise - it was removed in 2.16.0"


@test
def distinct_unit_keeps_a_real_second_unit_but_not_a_phantom():
    from ND_Dam_Hazard_Helper import structures as ST
    # a first NSI point already on the outline
    host = [{"family": "NSI", "addr_key": "100 MAIN", "nsi": {"fd_id": "A1"}}]
    # same NSI structure, same address -> phantom duplicate, drop
    same = {"family": "NSI", "addr_key": "100 MAIN", "nsi": {"fd_id": "A1"}}
    assert ST._distinct_unit(host, same) is False
    # a different NSI structure (fd_id) -> a real second unit, keep
    other_fd = {"family": "NSI", "addr_key": "100 MAIN", "nsi": {"fd_id": "A2"}}
    assert ST._distinct_unit(host, other_fd) is True
    # a different address -> keep
    other_addr = {"family": "NSI", "addr_key": "100 MAIN APT 2", "nsi": {"fd_id": "A1"}}
    assert ST._distinct_unit(host, other_addr) is True
    # another family's point is not judged here
    assert ST._distinct_unit(host, {"family": "FEMA", "addr_key": "X"}) is False
