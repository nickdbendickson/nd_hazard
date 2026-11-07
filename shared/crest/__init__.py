"""Consolidated crest logic for the ND dam-safety suite.

All crest-related algorithms live here so that an auditor can read one package
instead of 28 scattered files.  The originals become thin re-exports.

Modules
-------
elevation   resolve the top-of-dam number + locate the crest on the DEM
trace       trace algorithms (simple DEM ridge + HEC-RAS embankment) + polyline utilities
snap        pick a crest line from a GIS layer by structure ID or proximity
raise_dem   burn the crest into the model DEM (vectorised numpy)

Deeply-integrated call sites that remain in their host files (they depend on
QGIS, the record object, or the full HEC-RAS model builder):

  DamSafety_Runner/core/hecras_model.py
    crest_segment, add_spillway_notch, crest_dam_line
  DamSafety_Runner/algorithms.py
    _crest_from_dam_line  (QGIS Processing source wrapper)
  */core/terrain.py
    place_from_crest, streambed_inflow  (use grid + find_inflow_point)
  */core/proposed.py
    insert_embankment, crest_check  (depend on breach + flood + record)
"""
from .elevation import (resolve_top_of_dam, crest_band_m, embankment_crest,
                        crest_near, CREST_BAND_FT)
from .trace import (CrestTraceError, trace_crest_dem, trace_crest_hecras, tidy,
                    pick_breach, extend, cumlen, point_at, tangent_at,
                    left_normal, station_of, sub_line, frame_at, simplify,
                    smooth, straighten, monotonic, bearing_deg, angle_deg)
from .snap import (pick_crest_line, crest_line_near, nearest_on_line, dwr_ids,
                   CREST_SNAP_M)
from .raise_dem import raise_crest
