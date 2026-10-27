"""A complete HEC-RAS 2D dam-breach model from a finished breach run.

The model is laid out the way HEC's tutorial "Dam Breach - Simple 2D Geometry" and HEC's own
example project (BaldEagleCrkMulti2D, plan "SA to 2D Dam Break Run") lay it out:

* the reservoir is a STORAGE AREA with an elevation-volume curve (the plugin's own storage
  curve, `breach.Reservoir`: the same storage at the top of dam, the same surface area);
* the dam is an SA/2D AREA CONNECTION between the storage area and the 2D area: a weir
  along the dam line at the top of dam (or the ground where that is higher), with the breach
  in the plan (centre station, final bottom width and elevation, side slopes, formation time,
  failure mode, water-surface trigger);
* downstream is a 2D FLOW AREA whose perimeter is the run's flooded extent, widened by a side
  buffer, with an extra buffer at the downstream end of the inundation, and cut off straight
  along the dam line;
* a NORMAL DEPTH boundary where the valley leaves the 2D area, with the slope of the
  valley there;
* v3.12.0: the run's road culverts, bridges and notches that lie in the 2D area are SA/2D
  AREA CONNECTIONS INSIDE the 2D area (both sides the same 2D area, the way HEC-RAS models a
  road embankment with culverts in a 2D area): a weir along the road from the terrain, a
  culvert group for a culvert, a gap in the weir for a bridge or notch (see
  `structure_connection`). NOT opened in HEC-RAS; the README lists what to check;
* the plan starts the reservoir at the run's starting water surface and breaches at once
  (sunny day), or routes the run's inflow hydrograph and breaches at the run's trigger level
  (flood scenario).

Written (text files in the HEC-RAS 6.x format, CRLF line ends):

    <name>.prj         project (units, files)
    <name>.g01         geometry: storage area, 2D area perimeter and its computation points,
                       SA/2D connection (weir profile), normal-depth BC line; v3.12.0: a
                       connection inside the 2D area for each culvert, bridge and notch
    <name>.u01         unsteady flow: initial reservoir level, inflow, normal depth
    <name>.p01         plan: simulation window, time step, 2D equation set, breach
    <name>.rasmap      RAS Mapper file pointing at the projection
    GIS/Projection.prj the model's coordinate system (ESRI WKT)
    Terrain/<name> Terrain.tif   the DEM in the model's coordinate system and vertical units
    GIS/HECRAS Layout.gpkg       the same geometry as GIS layers, to check in QGIS
    README_HECRAS.md   what was built, the values and where they came from, and the steps
                       left to do in HEC-RAS

What only HEC-RAS can make (it is a proprietary, versioned binary format): the RAS terrain
(Terrain/<name>.hdf, made in RAS Mapper from the GeoTIFF), and the preprocessed geometry
(<name>.g01.hdf: the mesh faces and the hydraulic property tables, computed from the
computation points and the terrain). The README lists those steps.

Units follow the model's coordinate system: a CRS in feet (US survey or international) gives
an English-units model (ft, acre-ft, cfs), a CRS in metres an SI model (m, 1000 m3, m3/s).
By default the model uses the DEM's CRS when that is in feet, else North Dakota State Plane
(NAD83, international feet, North or South zone by latitude) for a dam in North Dakota, else
the run's metric CRS. Everything (dam point, flood extent, DEM) is transformed into it.

The pure-text writers at the top use the standard library and numpy only; `build_model`
needs GDAL/OGR (part of QGIS).
"""
import datetime
import json
import math
import os
import re

import numpy as np

from . import breach as brmod
from . import conditioning as cond             # v3.12.0: polygon and polyline helpers
from . import gaps as gapsmod                  # 3.26.8: the crossings' toe search
from . import hecras
from . import structures as structmod          # v3.12.0: the run's culverts and bridges
from .units import ACFT, CFS, FT, US_FT, MILE

PROGRAM_VERSION = "6.10"
SA_NAME = "Reservoir"
AREA_NAME = "Downstream"
CONN_NAME = "Dam"
BC_NAME = "DS Normal Depth"
START_DATE = "01JAN2000"
MAX_WEIR_POINTS = 450          # HEC-RAS allows 500 station-elevation points on a weir
MAX_ARC_POINTS = 400
# v3.26.6: HEC-RAS rejects an external BC line that touches the 2D perimeter ("BC Line ...
# intersects the perimeter ... it must be entirely outside"), so it is drawn this many cells
# outside the perimeter, along it (found running the export in HEC-RAS 6.6 itself)
BC_OFFSET_CELLS = 0.25
CR = "\r\n"

# v3.12.0: culverts, bridges and notches as connections inside the 2D area. The keyword lines
# are copied from HEC's own example geometries (HEC-RAS 6.6 example projects):
#   internal connection (both sides the 2D area, weir, routing type, outlet rating curve line):
#     2D Unsteady Flow Hydraulics/BaldEagleCrkMulti2D/BaldEagleDamBrk.g09 'Sayers Dam', and
#     BaldEagleDamBrk.g03 'Lower Levee' (weir equation for the overflow: 'Conn OverFlow
#     Method 2D=False', 'Conn Weir WD=10', 'Conn Weir Coef=2.6', 'Conn Weir Is Ogee= 0 ')
#   culvert group 'Connection Culv=', barrel stations, 'Conn Culv Bottom n=', routing type 3:
#     Applications Guide/Example 17 - Unsteady Flow Application/Diamond.g01 'Pyramid to Eagle'
#     1D Unsteady Flow Hydraulics/Storage Area Hydraulic Connection/Beaver_LS_SAConn.g01
# The fields of 'Connection Culv=' were read against the geometry HDF HEC-RAS 6.3 wrote for
# Diamond and Beaver_LS_SAConn: shape, RISE, SPAN (blank for a circular pipe), length,
# Manning's n, entrance loss, exit loss, FHWA chart, FHWA scale, upstream invert, downstream
# invert, number of barrels, group name (12 characters), solution flag (0), upstream distance
# (blank on a connection); then one upstream / downstream station pair per barrel in
# 8-character fields. In those storage-area files a culvert counts only with 'Conn Routing
# Type= 3' (OrleansEx.g04 has a culvert with type 1 and HEC-RAS 6.3 left it out of the HDF);
# inside a 2D area type 3 stops HEC-RAS 6.6, so a connection there is type 1 (v3.26.6).
CULVERT_GROUP = "Culvert #1"
# Runner shape -> (HEC-RAS shape code, FHWA chart, FHWA scale, entrance loss coefficient):
# a concrete pipe with a square edge in a headwall (chart 1 scale 1, Ke 0.5) and a box with
# wingwalls flared 30 to 75 degrees (chart 8 scale 1, Ke 0.4), HEC-RAS's typical values
CULVERT_TYPES = {"pipe": (1, 1, 1, 0.5), "box": (2, 8, 1, 0.4)}
CULVERT_EXIT_LOSS = 1.0
STRUCT_WEIR_COEF = 2.6         # road overflow, US units (HEC-RAS's default weir coefficient)
STRUCT_RISE_FT = 10.0          # the connection line runs along the road until the ground is
STRUCT_MAX_HALF_FT = 1000.0    # this far above the road at the structure, or this far each side
# 3.26.8: a culvert the run cut as a gap from the crossings (gaps.barrel_toes) runs from where
# the channel leaves the road embankment on one side to the other, at least BARREL_MIN_CELLS
# 2D cells long (and longer where needed) so its two ends fall in different cells, each invert
# at least BARREL_FIT_MARGIN_FT above the lowest ground of the cell at that end (as DamSafety
# Runner 3.10.56 fit_to_cells: HEC-RAS refuses a culvert invert below its cell)
BARREL_MIN_CELLS = 1.0
BARREL_FIT_MARGIN_FT = 0.05
BARREL_REACH_FT = 1.0          # the cell at a barrel end reaches within this of its invert:
BARREL_REACH_GAIN_FT = 0.25    # the barrel is lengthened for that only when it gains this,
BARREL_MAX_EXTRA_CELLS = 1.0   # and by at most this many cells in all (or this many terrain
BARREL_MAX_EXTRA_PIXELS = 4.0  # cells, where they are the larger: the toe smeared across them)
BARREL_SPLIT_EXTRA_CELLS = 2.0  # lengthening so that its two ends fall in two cells
BARREL_MAX_SKEW_DEG = 60.0     # a channel crossing the road at a sharper angle is not laid
BARREL_SNAP_M = 5.0            # each end onto the lowest terrain pixel this close to it, a
BARREL_SNAP_SLOPE = 0.05       # pixel this much per unit further away counting higher
CREST_PICK_FT = 1.0            # carriageway crests within this of the highest can take the line
FLOW_PATH_NEAR_FT = 1000.0     # the run's flow path tells the direction this close to it,
FLOW_PATH_MIN_DEG = 14.5       # crossing the road at this angle or more
PERCH_FT = 0.5                 # an invert more than this above the channel bed found: WARNING
BED_LABEL = "channel bed beside the road"
PRE_3268 = ("the run recorded no toes of the road embankment for it (a run made before "
            "3.26.8)")

# computation intervals HEC-RAS offers (seconds), and their labels
COMP_INTERVALS = ((0.1, "0.1SEC"), (0.2, "0.2SEC"), (0.25, "0.25SEC"), (0.3, "0.3SEC"),
                  (0.4, "0.4SEC"), (0.5, "0.5SEC"), (1, "1SEC"), (2, "2SEC"), (3, "3SEC"),
                  (4, "4SEC"), (5, "5SEC"), (6, "6SEC"), (10, "10SEC"), (12, "12SEC"),
                  (15, "15SEC"), (20, "20SEC"), (30, "30SEC"), (60, "1MIN"), (120, "2MIN"),
                  (180, "3MIN"), (240, "4MIN"), (300, "5MIN"), (600, "10MIN"))
NICE_CELLS = (5, 6, 8, 10, 12, 15, 20, 25, 30, 40, 50, 60, 75, 100, 125, 150, 200, 250, 300,
              400, 500)

EQ_SWE, EQ_DWE = "swe", "dwe"
EQUATIONS = {EQ_SWE: (1, "Shallow water equations (SWE-ELM)"),
             EQ_DWE: (0, "Diffusion wave")}


class HecRasModelError(ValueError):
    pass


# ============================================================================
# Units
# ============================================================================
class Units:
    """Project units of the HEC-RAS model, from the linear unit of its CRS."""

    def __init__(self, si):
        self.si = bool(si)
        self.L = 1.0 if self.si else FT            # metres per project length unit

    @property
    def label(self):
        return "SI Units" if self.si else "English Units"

    @property
    def len_name(self):
        return "m" if self.si else "ft"

    @property
    def vol_name(self):
        return "1000 m3" if self.si else "acre-ft"

    @property
    def q_name(self):
        return "m3/s" if self.si else "cfs"

    def length(self, m):
        return float(m) / self.L

    def from_ft(self, ft):
        return None if ft is None else float(ft) * FT / self.L

    def volume(self, m3):
        return float(m3) / (1000.0 if self.si else ACFT)

    def flow(self, cms):
        return float(cms) if self.si else float(cms) / CFS

    def weir_coef(self, c_us):
        """A US-units weir coefficient (ft^0.5/s) in the project units."""
        return float(c_us) * math.sqrt(FT) if self.si else float(c_us)


# ============================================================================
# Text formatting
# ============================================================================
def f8(v):
    """A number in an 8-character field."""
    if v is None or (isinstance(v, float) and not math.isfinite(v)):
        return " " * 8
    return hecras._field8(v, 8)


def f16(v):
    """A coordinate in a 16-character field, with as many decimals as fit in 15 so that
    neighbouring numbers never run together."""
    v = float(v)
    for d in (8, 7, 6, 5, 4, 3, 2, 1, 0):
        s = "{:.{}f}".format(v, d)
        if len(s) <= 15:
            return s.rjust(16)
    raise HecRasModelError("coordinate {} is too large for HEC-RAS".format(v))


def num(v, nd=3):
    """Short number text for 'key=value' lines."""
    if v is None:
        return ""
    s = "{:.{}f}".format(float(v), nd).rstrip("0").rstrip(".")
    return s if s not in ("-0", "") else "0"


def name16(s):
    s = str(s)
    if len(s) > 16 or not s.strip() or any(c in s for c in ",=\r\n"):
        raise HecRasModelError("HEC-RAS names are 1 to 16 characters without , or =: "
                               "{!r}".format(s))
    return s.ljust(16)


def name32(s):
    s = str(s)
    if len(s) > 32 or any(c in s for c in ",=\r\n"):
        raise HecRasModelError("bad boundary-line name {!r}".format(s))
    return s.ljust(32)


def written_increasing(pairs):
    """v3.26.6: the (elevation, volume) rows whose WRITTEN values (8-character fields) both
    rise. HEC-RAS stops on 'Volume Elevation relationship does not have increasing volume'
    when two rows round to the same volume, which the shallow bottom of a pool does at two
    decimals (0.00, 0.00, 0.00 acre-ft on the smoke-test dam). The first row is kept."""
    out, last = [], None
    for e, v in pairs or []:
        we, wv = float(f8(e)), float(f8(v))
        if last is None or (we > last[0] and wv > last[1]):
            out.append((e, v))
            last = (we, wv)
    return out


def lines8(values, per_line=10):
    vals = list(values)
    return ["".join(f8(x) for x in vals[i:i + per_line]) for i in range(0, len(vals), per_line)]


def lines_xy(points, per_line=2):
    """Coordinate pairs, `per_line` points to a line, 16-character fields."""
    pts = [(float(x), float(y)) for x, y in points]
    out = []
    for i in range(0, len(pts), per_line):
        out.append("".join(f16(x) + f16(y) for x, y in pts[i:i + per_line]))
    return out


def comp_interval(dt_s):
    """The largest HEC-RAS computation interval not above dt_s: (seconds, label)."""
    best = COMP_INTERVALS[0]
    for s, lab in COMP_INTERVALS:
        if s <= dt_s + 1e-9:
            best = (s, lab)
    return best


def nice_cell(c):
    for v in NICE_CELLS:
        if v >= c - 1e-9:
            return float(v)
    return float(NICE_CELLS[-1])


def ras_date(hours, start=START_DATE):
    """('01JAN2000', '0000') for a time `hours` after the start date at 00:00."""
    t0 = datetime.datetime.strptime(start, "%d%b%Y")
    t = t0 + datetime.timedelta(hours=float(hours))
    return t.strftime("%d%b%Y").upper(), t.strftime("%H%M")


def safe_name(s, n=24):
    s = re.sub(r"[^A-Za-z0-9_]+", "_", str(s)).strip("_") or "Dam"
    return s[:n]


# ============================================================================
# Model description (filled by build_model, or by hand in the tests)
# ============================================================================
class Model:
    """Everything the writers need, in project units and the model CRS."""

    def __init__(self):
        self.name = "Dam"
        self.title = "Dam breach"
        self.units = Units(False)
        self.perimeter = []          # 2D area perimeter, not closed, counter-clockwise
        self.points = np.zeros((0, 2))   # 2D computation points
        self.cell = 100.0
        self.manning_n = 0.06
        self.reservoir = []          # storage-area outline (display only)
        self.elev_volume = []        # [(elevation, volume)]
        self.conn_line = []          # connection line, left to right looking downstream
        self.weir = []               # [(station, elevation)] along conn_line
        self.weir_width = 20.0
        self.weir_coef = 2.6
        self.bc_line = []            # normal-depth BC line along the perimeter
        self.bc_slope = 0.001
        self.initial_wse = None
        self.inflow = None           # (interval label, [values]) or None for base flow
        self.base_flow = 0.0
        self.sim_hours = 24.0
        self.comp_interval = "5SEC"
        self.courant_max = 1.0           # v3.26.6: adaptive time step
        self.output_interval = "1MIN"
        self.mapping_interval = "5MIN"
        self.detail_interval = "1HOUR"
        self.equation = EQ_SWE
        self.breach = {}             # see breach_block()
        self.projection_name = "Projection.prj"
        self.terrain_name = ""
        self.description = []
        self.structures = []         # v3.12.0: structure_connection() dicts
        self.breaklines = []         # v3.23.0: [{name, polyline, cell_min, cell_max}]

    def bounds(self):
        pts = list(self.perimeter) + list(self.reservoir)
        a = np.asarray(pts, dtype=float)
        return float(a[:, 0].min()), float(a[:, 1].min()), float(a[:, 0].max()), \
            float(a[:, 1].max())


# ============================================================================
# Writers
# ============================================================================
def _centroid(pts):
    a = np.asarray(pts, dtype=float)
    return float(a[:, 0].mean()), float(a[:, 1].mean())


def project_text(m):
    L = ["Proj Title=" + m.title, "Current Plan=p01", "Default Exp/Contr=0.3,0.1", m.units.label,
         "Geom File=g01", "Unsteady File=u01", "Plan File=p01", "Y Axis Title=Elevation",
         "X Axis Title(PF)=Main Channel Distance", "X Axis Title(XS)=Station",
         "BEGIN DESCRIPTION:"] + list(m.description) + [
        "END DESCRIPTION:", "DSS Start Date=", "DSS Start Time=", "DSS End Date=",
        "DSS End Time=", "DSS File=dss", "DSS Export Filename=", "DSS Export Rating Curves= 0 ",
        "DSS Export Rating Curve Sorted= 0 ", "DSS Export Volume Flow Curves= 0 ",
        "DXF Filename=", "DXF OffsetX= 0 ", "DXF OffsetY= 0 ", "DXF ScaleX= 1 ",
        "DXF ScaleY= 10 ", "GIS Export Profiles= 0 "]
    return CR.join(L) + CR


def geometry_text(m, now=None):
    now = now or datetime.datetime.now()
    xmin, ymin, xmax, ymax = m.bounds()
    pad = 0.05 * max(xmax - xmin, ymax - ymin)
    L = ["Geom Title=" + m.title, "Program Version=" + PROGRAM_VERSION,
         "Viewing Rectangle= {} , {} , {} , {} ".format(num(xmin - pad, 2), num(xmax + pad, 2),
                                                       num(ymax + pad, 2), num(ymin - pad, 2)),
         ""]
    # --- 2D flow area ------------------------------------------------------------------
    cx, cy = _centroid(m.perimeter)
    L.append("Storage Area={},{},{}".format(name16(AREA_NAME), num(cx, 4), num(cy, 4)))
    L.append("Storage Area Surface Line= {} ".format(len(m.perimeter)))
    L += [s.ljust(48) for s in lines_xy(m.perimeter, 1)]
    L += ["Storage Area Type= 0 ", "Storage Area Area=", "Storage Area Min Elev=",
          "Storage Area Is2D=-1",
          "Storage Area Point Generation Data=,,{},{}".format(num(m.cell), num(m.cell)),
          "Storage Area 2D Points= {} ".format(len(m.points))]
    L += lines_xy(m.points, 2)
    L += ["Storage Area 2D PointsPerimeterTime=" + now.strftime("%d%b%Y %H:%M:%S"),
          "Storage Area Mannings={}".format(num(m.manning_n, 4)),
          "2D Cell Volume Filter Tolerance=0.01", "2D Cell Minimum Area Fraction=0.01",
          "2D Face Profile Filter Tolerance=0.01",
          "2D Face Area Elevation Profile Filter Tolerance=0.01",
          "2D Face Area Elevation Conveyance Ratio=0.02", "2D Face Area Laminar Depth=0.2", ""]
    # --- reservoir storage area ----------------------------------------------------------
    cx, cy = _centroid(m.reservoir)
    L.append("Storage Area={},{},{}".format(name16(SA_NAME), num(cx, 4), num(cy, 4)))
    L.append("Storage Area Surface Line= {} ".format(len(m.reservoir)))
    L += [s.ljust(48) for s in lines_xy(m.reservoir, 1)]
    L += ["Storage Area Type= 1 ", "Storage Area Area=", "Storage Area Min Elev=",
          "Storage Area Vol Elev= {} ".format(len(written_increasing(m.elev_volume)))]
    flat = []
    for e, v in written_increasing(m.elev_volume):
        flat += [e, v]
    L += lines8(flat)
    L += ["Storage Area Is2D=0", ""]
    # --- v3.23.0: breaklines (enforce cell faces along the dam and road embankments) -----
    for bl in m.breaklines:
        L += breakline_block(bl)
    # --- SA/2D connection: the dam ------------------------------------------------------
    cx, cy = _centroid(m.conn_line)
    L.append("Connection={},{},{}".format(name16(CONN_NAME), num(cx, 4), num(cy, 4)))
    L.append("Connection Desc=Dam crest from the ND Dam Breach Runner")
    L.append("Connection Line={}".format(len(m.conn_line)))
    L += lines_xy(m.conn_line, 2)
    L += ["Connection Last Edited Time=" + now.strftime("%b/%d/%Y %H:%M:%S"),
          "Conn Near Repeats=1",
          "Connection Up SA=" + name16(SA_NAME), "Connection Dn SA=" + name16(AREA_NAME),
          "Conn Routing Type= 1 ", "Conn Use RC Family=False", "Conn OverFlow Method 2D=True",
          "Conn Weir WD={}".format(num(m.weir_width, 2)),
          "Conn Weir Coef={}".format(num(m.weir_coef, 3)),
          "Conn Simple Spill Pos Coef=0.05", "Conn Simple Spill Neg Coef=0.05",
          "Conn Weir SE= {} ".format(len(m.weir))]
    flat = []
    for s, e in m.weir:
        flat += [s, e]
    L += lines8(flat)
    L += ["Conn Outlet Rating Curve= 0 ,False,,"]
    # --- v3.12.0: culverts, bridges and notches inside the 2D area ----------------------
    for c in m.structures:
        L += structure_block(c, now)
    # --- normal-depth boundary line -----------------------------------------------------
    bc = m.bc_line
    mid = bc[len(bc) // 2]
    L += ["BC Line Name=" + name32(BC_NAME), "BC Line Storage Area=" + name16(AREA_NAME),
          "BC Line Start Position= {} , {} ".format(num(bc[0][0], 4), num(bc[0][1], 4)),
          "BC Line Middle Position= {} , {} ".format(num(mid[0], 4), num(mid[1], 4)),
          "BC Line End Position= {} , {} ".format(num(bc[-1][0], 4), num(bc[-1][1], 4)),
          "BC Line Arc= {} ".format(len(bc))]
    L += lines_xy(bc, 2)
    L += ["BC Line Text Position= {} , {} ".format(num(mid[0], 4), num(mid[1], 4)),
          "LCMann Time=Dec/30/1899 00:00:00", "LCMann Region Time=Dec/30/1899 00:00:00",
          "LCMann Table=0", "Chan Stop Cuts=-1 ", "", "", "",
          "Use User Specified Reach Order=0", "GIS Ratio Cuts To Invert=-1",
          "GIS Limit At Bridges=0", "Composite Channel Slope=5"]
    return CR.join(L) + CR


def breakline_block(bl):
    """v3.23.0: the geometry lines of one breakline, as HEC writes them (BaldEagleDamBrk's
    'Upper/Middle/Lower Levee' breaklines): a named polyline with optional near/far cell
    sizes. An empty cell size keeps the 2D area's cell size, so the breakline still forces
    cell faces to line up along it without refining. HEC-RAS honours a breakline when it
    (re)generates the 2D area's computation points; the README says so."""
    poly = bl["polyline"]
    L = ["BreakLine Name=" + str(bl["name"]),
         "BreakLine CellSize Min=" + ("" if bl.get("cell_min") is None
                                      else num(bl["cell_min"], 2)),
         "BreakLine CellSize Max=" + ("" if bl.get("cell_max") is None
                                      else num(bl["cell_max"], 2)),
         "BreakLine Near Repeats=0", "BreakLine Protection Radius=-1",
         "BreakLine Polyline= {} ".format(len(poly))]
    L += lines_xy(poly, 2)
    return L


def structure_block(c, now):
    """v3.12.0: the geometry lines of one structure inside the 2D area (a dict from
    `structure_connection`): an SA/2D area connection from the 2D area to itself, laid out as
    HEC's internal structure 'Sayers Dam' (BaldEagleDamBrk.g09) - connection line, weir
    station-elevation, outlet rating curve line - with, for a culvert, the culvert group as
    HEC's Diamond.g01 and Beaver_LS_SAConn.g01 write it (see CULVERT_GROUP above for the
    fields). NOT opened in HEC-RAS: the README lists what to check there."""
    cx, cy = _centroid(c["line"])
    culv = c.get("culvert")
    L = ["Connection={},{},{}".format(name16(c["name"]), num(cx, 4), num(cy, 4)),
         "Connection Desc=" + c["desc"],
         "Connection Line={}".format(len(c["line"]))]
    L += lines_xy(c["line"], 2)
    L += ["Connection Last Edited Time=" + now.strftime("%b/%d/%Y %H:%M:%S"),
          "Conn Near Repeats=1",
          "Connection Up SA=" + name16(AREA_NAME), "Connection Dn SA=" + name16(AREA_NAME),
          # v3.26.6 (as DamSafety Runner 3.10.56): 1 (weir, gates, culverts) with or without a
          # culvert, as BaldEagleDamBrk's internal levees are. Type 3, copied from the
          # storage-area examples (Diamond.g01, Beaver_LS_SAConn.g01), made HEC-RAS 6.6 read a
          # culvert inside a 2D area as a rating-curve family with no curves, and stop. The
          # weir equation is for flow over the road (BaldEagleDamBrk.g03 'Lower Levee').
          "Conn Routing Type= 1 ",
          "Conn Use RC Family=False", "Conn OverFlow Method 2D=False",
          "Conn Weir WD={}".format(num(c["weir_width"], 2)),
          "Conn Weir Coef={}".format(num(c["weir_coef"], 3)),
          "Conn Weir Is Ogee= 0 ",
          "Conn Simple Spill Pos Coef=0.05", "Conn Simple Spill Neg Coef=0.05",
          "Conn Weir SE= {} ".format(len(c["weir"]))]
    flat = []
    for s, e in c["weir"]:
        flat += [s, e]
    L += lines8(flat)
    if culv:
        pipe = culv["shape"] == CULVERT_TYPES["pipe"][0]
        L.append("Connection Culv={},{},{},{},{},{},{},{},{},{},{}, {} ,{}, 0 ,".format(
            culv["shape"], num(culv["rise"], 3), "" if pipe else num(culv["span"], 3),
            num(culv["length"], 2), num(culv["n"], 4), num(culv["entrance"], 3),
            num(culv["exit"], 3), culv["chart"], culv["scale"], num(culv["inv_up"], 2),
            num(culv["inv_dn"], 2), int(culv["barrels"]), CULVERT_GROUP.ljust(12)))
        st = []
        for s_up, s_dn in barrel_stations(culv):
            st += [s_up, s_dn]                   # upstream and downstream station of a barrel
        L += lines8(st)
        L.append("Conn Culv Bottom n={}".format(num(culv["n"], 4)))
    L.append("Conn Outlet Rating Curve= 0 ,False,,")
    return L


_MET = """Met Point Raster Parameters=,,,,
Precipitation Mode=Disable
Wind Mode=No Wind Forces
Air Density Mode=
Met BC=Precipitation|Expanded View=0
Met BC=Precipitation|Point Interpolation=Nearest
Met BC=Precipitation|Gridded Source=DSS
Met BC=Evapotranspiration|Expanded View=0
Met BC=Evapotranspiration|Point Interpolation=Nearest
Met BC=Evapotranspiration|Gridded Source=DSS
Met BC=Wind Speed|Expanded View=0
Met BC=Wind Speed|Point Interpolation=Nearest
Met BC=Wind Speed|Gridded Source=DSS
Met BC=Wind Direction|Expanded View=0
Met BC=Wind Direction|Point Interpolation=Nearest
Met BC=Wind Direction|Gridded Source=DSS
Met BC=Wind Velocity X|Expanded View=0
Met BC=Wind Velocity X|Point Interpolation=Nearest
Met BC=Wind Velocity X|Gridded Source=DSS
Met BC=Wind Velocity Y|Expanded View=0
Met BC=Wind Velocity Y|Point Interpolation=Nearest
Met BC=Wind Velocity Y|Gridded Source=DSS
Met BC=Air Density|Mode=Constant
Met BC=Air Density|Expanded View=0
Met BC=Air Density|Constant Value=1.225
Met BC=Air Density|Constant Units=kg/m3
Met BC=Air Density|Point Interpolation=Nearest
Met BC=Air Density|Gridded Source=DSS
Met BC=Air Temperature|Expanded View=0
Met BC=Air Temperature|Point Interpolation=Nearest
Met BC=Air Temperature|Gridded Source=DSS
Met BC=Humidity|Expanded View=0
Met BC=Humidity|Point Interpolation=Nearest
Met BC=Humidity|Gridded Source=DSS
Met BC=Air Pressure|Expanded View=0
Met BC=Air Pressure|Point Interpolation=Nearest
Met BC=Air Pressure|Gridded Source=DSS
Non-Newtonian Method= 0 ,
Non-Newtonian Constant Vol Conc=0
Non-Newtonian Yield Method= 0 ,
Non-Newtonian Yield Coef=0, 0
User Yeild=   0
Non-Newtonian Sed Visc= 0 ,
Non-Newtonian Obrian B=0
User Viscosity=0
User Viscosity Ratio=0
Herschel-Bulkley Coef=0, 0
Clastic Method= 0 ,
Coulomb Phi=0
Voellmy X=0
Non-Newtonian Hindered FV= 0
Non-Newtonian FV K=0
Non-Newtonian ds=0
Non-Newtonian Max Cv=0
Non-Newtonian Bulking Method= 0 ,
Non-Newtonian High C Transport= 0 , """


def _bc_tail():
    return ["DSS Path=", "Use DSS=False", "Use Fixed Start Time=False",
            "Fixed Start Date/Time=,", "Is Critical Boundary=False", "Critical Boundary Flow="]


def unsteady_text(m):
    blank16, blank8, blank32 = " " * 16, " " * 8, " " * 32
    L = ["Flow Title=" + m.title, "Program Version=" + PROGRAM_VERSION, "Use Restart= 0 ",
         "Initial Storage Elev={},{}".format(name16(SA_NAME), num(m.initial_wse, 3))]
    # inflow to the reservoir
    L.append("Boundary Location={},{},{},{},{},{},{},{}".format(
        blank16, blank16, blank8, blank8, blank16, name16(SA_NAME), blank16, blank32))
    if m.inflow:
        interval, vals = m.inflow
    else:
        interval = "1HOUR"
        vals = [m.base_flow] * (int(math.ceil(m.sim_hours)) + 2)
    L.append("Interval=" + interval)
    L.append("Lateral Inflow Hydrograph= {} ".format(len(vals)))
    L += lines8(vals)
    L += _bc_tail()
    # normal depth where the valley leaves the 2D area
    L.append("Boundary Location={},{},{},{},{},{},{},{}".format(
        blank16, blank16, blank8, blank8, blank16, name16(AREA_NAME), blank16, name32(BC_NAME)))
    L.append("Friction Slope={},0".format(num(m.bc_slope, 6)))
    L += _MET.split("\n")
    return CR.join(L) + CR


def breach_block(m):
    b = m.breach
    piping = bool(b.get("piping"))
    geom = [num(b["center_station"], 2), num(b["bottom_width"], 2), num(b["bottom_elev"], 2),
            num(b["left_slope"], 3), num(b["right_slope"], 3), "True" if piping else "False",
            num(b.get("piping_coef", 0.5), 3),
            num(b["piping_elev"], 2) if piping and b.get("piping_elev") is not None else "",
            num(b["formation_hr"], 3), num(b["weir_coef"], 3)]
    blank16, blank8 = " " * 16, " " * 8
    return ["Breach Loc={},{},{},True,{}".format(blank16, blank16, blank8, name16(CONN_NAME)),
            "Breach Method= 0 ",
            "Breach Geom=" + ",".join(geom),
            "Breach Start=True,{},,,False,,,0".format(num(b["trigger_wse"], 3)),
            "Breach Progression= 2 ", f8(0) + f8(0) + f8(1) + f8(1),
            "Simplified Physical Breach Downcutting= 2 ", f8(0) + f8(0) + f8(1) + f8(1),
            "Simplified Physical Breach Widening= 2 ", f8(0) + f8(0) + f8(1) + f8(1),
            "Starting Notch Depth= 3.402823E+38 ", "Initial Piping Diameter= 3.402823E+38 ",
            "Mass Wasting Options= 0 ", "Mass Wasting Width= 3.402823E+38 ",
            "Mass Wasting Duration= 3.402823E+38 ",
            "Mass Wasting Final Bottom Elevation= 3.402823E+38 ",
            "Breach Use User Defined Growth Ratio=0", "Breach User Defined Growth Ratio=1"]


def _d2_block(m, eq):
    return ["UNET D2 Name=" + name16(AREA_NAME), "UNET D2 Theta= 1 ", "UNET D2 Theta Warmup= 1 ",
            "UNET D2 Z Tol= 0.01 ", "UNET D2 Volume Tol= 0.01 ", "UNET D2 Max Iterations= 20 ",
            "UNET D2 Equation= {} ".format(eq), "UNET D2 TotalICTime=",
            "UNET D2 RampUpFraction=0.1", "UNET D2 TimeSlices= 1 ",
            "UNET D2 Turbulence Formulation=Non-Conservative (original)",
            "UNET D2 Eddy Viscosity=", "UNET D2 Transverse Eddy Viscosity=",
            "UNET D2 Smagorinsky Mixing=0", "UNET D2 BCVolumeCheck=0", "UNET D2 Latitude=",
            "UNET D2 Cores=0", "UNET D2 SolverType=PARDISO (Direct)"]


def plan_text(m):
    eq = EQUATIONS[m.equation][0]
    d1, t1 = ras_date(0.0)
    d2, t2 = ras_date(m.sim_hours)
    L = ["Plan Title=" + m.title, "Program Version=" + PROGRAM_VERSION,
         "Short Identifier=" + "Breach".ljust(64),
         "Simulation Date={},{},{},{}".format(d1, t1, d2, t2),
         "Geom File=g01", "Flow File=u01", "Subcritical Flow", "K Sum by GR= 0 ",
         "Std Step Tol= 0.01 ", "Critical Tol= 0.01 ", "Num of Std Step Trials= 20 ",
         "Max Error Tol= 0.3 ", "Flow Tol Ratio= 0.001 ", "Split Flow NTrial= 30 ",
         "Split Flow Tol= 0.02 ", "Split Flow Ratio= 0.02 ", "Log Output Level= 0 ",
         "Friction Slope Method= 1 ", "Unsteady Friction Slope Method= 2 ",
         "Unsteady Bridges Friction Slope Method= 1 ", "Parabolic Critical Depth",
         "Global Vel Dist= 0 , 0 , 0 ", "Global Log Level= 0 ", "CheckData=True",
         "Encroach Param=-1 ,0,0, 0 ", "BEGIN DESCRIPTION:"] + list(m.description) + [
        "END DESCRIPTION:",
        "Computation Interval=" + m.comp_interval, "Output Interval=" + m.output_interval,
        "Instantaneous Interval=" + m.detail_interval, "Mapping Interval=" + m.mapping_interval,
        # v3.26.6: HEC-RAS varies the step on the Courant number (halves it near the peak,
        # doubles it back as the flood slows), from the step set for the finest cells
        "Computation Time Step Use Courant=        1",
        "Computation Time Step Use Time Series=    0",
        "Computation Time Step Residence Courant=0",
        "Computation Time Step Max Courant= {}".format(num(m.courant_max, 2)),
        "Computation Time Step Min Courant= {}".format(num(m.courant_max * 0.45, 2)),
        "Computation Time Step Count To Double= 4",
        "Computation Time Step Max Doubling= 3",
        "Computation Time Step Max Halving= 4",
        "Run HTab=-1 ", "Run UNet=-1 ", "Run Sediment= 0 ", "Run PostProcess=-1 ",
        "Run WQNet= 0 ", "Run RASMapper= 0 ",
        "UNET Theta= 1 ", "UNET Theta Warmup= 1 ", "UNET ZTol= 0.02 ", "UNET ZSATol= 0.02 ",
        "UNET QTol=", "UNET MxIter= 20 ", "UNET Max Iter WO Improvement= 0 ",
        "UNET MaxInSteps= 0 ", "UNET DtIC= 0 ", "UNET DtMin= 0 ", "UNET MaxCRTS= 20 ",
        "UNET WFStab= 2 ", "UNET SFStab= 1 ", "UNET WFX= 1 ", "UNET SFX= 1 ",
        "UNET Gravity={}".format("9.80665" if m.units.si else "32.174"),
        "UNET 1D Methodology=Finite Difference", "UNET DSS MLevel= 4 ", "UNET Pardiso=0",
        "UNET DZMax Abort= 100 ", "UNET Use Existing IB Tables=-1 ",
        "UNET Froude Reduction=False", "UNET Froude Limit= 0.8 ", "UNET Froude Power= 4 ",
        "UNET D1 Cores= 0 ", "UNET D2 Coriolis=0", "UNET D2 Cores= 0 "]
    L += _d2_block(m, eq)[1:]
    L += _d2_block(m, eq)
    L += ["UNET D1D2 MaxIter= 0 ", "UNET D1D2 ZTol=0.01", "UNET D1D2 QTol=0.1",
          "UNET D1D2 MinQTol=1", "DSS File=dss", "Write IC File= 0 ",
          "Write IC File at Fixed DateTime=0", "IC Time=,,", "Write IC File Reoccurance=",
          "Write IC File at Sim End=0", "Echo Input=False", "Echo Parameters=False",
          "Echo Output=False", "Write Detailed= 0 ", "HDF Write Warmup=0",
          "HDF Write Time Slices=0", "HDF Flush=0", "HDF Compression= 1 ", "HDF Chunk Size= 1 ",
          "HDF Spatial Parts= 1 ", "HDF Use Max Rows=0", "HDF Fixed Rows= 1 "]
    L += breach_block(m)
    return CR.join(L) + CR


def rasmap_text(m):
    """RAS Mapper file: the projection and the geometry. The terrain is added in RAS Mapper
    (its terrain HDF is made there from the GeoTIFF in Terrain/)."""
    return ("<RASMapper>\n  <Version>2.0.0</Version>\n"
            '  <RASProjectionFilename Filename=".\\GIS\\{proj}" />\n'
            '  <Geometries Checked="True" Expanded="True">\n'
            '    <Layer Name="{title}" Type="RASGeometry" Checked="True" Expanded="True" '
            'Filename=".\\{geo}">\n'
            '      <Layer Type="RASStorageArea" Checked="True" />\n'
            '      <Layer Type="RASD2FlowArea" Checked="True" />\n'
            '      <Layer Type="StructureLayer" Checked="True" />\n'
            '      <Layer Type="RASBoundaryConditionLines" Checked="True" />\n'
            "    </Layer>\n  </Geometries>\n  <Terrains />\n"
            "  <Units>{units}</Units>\n</RASMapper>\n").format(
        proj=m.projection_name, title=xml_escape(m.title), geo="{}.g01.hdf".format(m.name),
        units=m.units.label).replace("\n", CR)


def xml_escape(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def write_text_files(m, folder):
    """Writes the project, geometry, unsteady, plan and RAS Mapper files. Returns paths."""
    os.makedirs(folder, exist_ok=True)
    out = {}
    for ext, fn in (("prj", project_text), ("g01", geometry_text), ("u01", unsteady_text),
                    ("p01", plan_text), ("rasmap", rasmap_text)):
        p = os.path.join(folder, "{}.{}".format(m.name, ext))
        with open(p, "w", newline="", encoding="ascii", errors="replace") as f:
            f.write(fn(m))
        out[ext] = p
    return out


# ============================================================================
# Pure geometry helpers (numpy)
# ============================================================================
def ring_area(pts):
    a = np.asarray(pts, dtype=float)
    x, y = a[:, 0], a[:, 1]
    return 0.5 * float(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1)))


def _grid_points(perim, cell, srs):
    """The 2D computation points on a square grid of `cell` inside the perimeter, without the
    ones closer than 0.3 cell to it (they would make sliver cells)."""
    gdal, ogr, _ = _ogr()
    pxmin, pymin = perim.min(axis=0)
    pxmax, pymax = perim.max(axis=0)
    gx0 = math.floor(pxmin / cell) * cell + cell / 2.0
    gy1 = math.ceil(pymax / cell) * cell - cell / 2.0
    gnx = int(math.ceil((pxmax - gx0) / cell)) + 1
    gny = int(math.ceil((gy1 - pymin) / cell)) + 1
    mds = gdal.GetDriverByName("MEM").Create("", gnx, gny, 1, gdal.GDT_Byte)
    mds.SetGeoTransform((gx0 - cell / 2.0, cell, 0.0, gy1 + cell / 2.0, 0.0, -cell))
    vds = ogr.GetDriverByName("Memory").CreateDataSource("p")
    lyr = vds.CreateLayer("p", srs, ogr.wkbPolygon)
    f_ = ogr.Feature(lyr.GetLayerDefn())
    f_.SetGeometry(_polygon(perim))
    lyr.CreateFeature(f_)
    gdal.RasterizeLayer(mds, [1], lyr, burn_values=[1])
    inside = mds.GetRasterBand(1).ReadAsArray().astype(bool)
    pr, pc = np.nonzero(inside)
    px = gx0 + pc * cell
    py = gy1 - pr * cell
    # drop points too close to the perimeter (they would make sliver cells)
    edge = np.zeros_like(inside)
    edge[1:-1, 1:-1] = ~(inside[:-2, 1:-1] & inside[2:, 1:-1] & inside[1:-1, :-2] &
                         inside[1:-1, 2:])
    edge |= inside & False
    edge[[0, -1], :] = True
    edge[:, [0, -1]] = True
    near_edge = edge[pr, pc]
    keep = np.ones(len(px), bool)
    if near_edge.any():
        dd = point_segment_distance(px[near_edge], py[near_edge],
                                    list(map(tuple, perim)) + [tuple(perim[0])])
        keep[np.nonzero(near_edge)[0][dd < 0.3 * cell]] = False
    return np.column_stack([px[keep], py[keep]])


def _piping_in_breach(bvals, top_u, notes, units):
    """v3.26.6: keeps the initial piping elevation inside a raised breach (half way up)."""
    pe, z = bvals.get("piping_elev"), bvals["bottom_elev"]
    if bvals.get("piping") and pe is not None and pe <= z:
        bvals["piping_elev"] = round(z + 0.5 * (top_u - z), 2)
        notes.append("Initial piping elevation moved to half way up the raised breach "
                     "({:.1f} {}).".format(bvals["piping_elev"], units.len_name))


def cut_breach_channel(tif_path, A, ab, d, s_lo, s_hi, t_max, z_cut, side_slope=None,
                       z_top=None, t_min=0.0):
    """v3.26.6 (as DamSafety Runner hecras_model.cut_breach_channel): lowers the terrain
    GeoTIFF (never raises it) to z_cut in the rectangle from the dam line A + ab*s
    (s_lo..s_hi) downstream (d, across the line) for t_min..t_max: the breach opening through
    the downstream half of the embankment. With side_slope (H:V) and z_top the sides rise
    from z_cut at that slope beyond s_lo / s_hi up to z_top. Returns {'cells', 'area',
    'max_cut'} (area in the GeoTIFF's units squared)."""
    gdal, _, _ = _ogr()
    ds = gdal.Open(tif_path, gdal.GA_Update)
    gt = ds.GetGeoTransform()
    b = ds.GetRasterBand(1)
    nd = b.GetNoDataValue()
    A = np.asarray(A, float)
    ab = np.asarray(ab, float)
    d = np.asarray(d, float)
    wside = 0.0
    if side_slope and z_top is not None and z_top > z_cut:
        wside = float(side_slope) * (float(z_top) - float(z_cut))
    lo_, hi_ = s_lo - wside, s_hi + wside
    corners = np.array([A + ab * lo_ + d * t_min, A + ab * hi_ + d * t_min,
                        A + ab * hi_ + d * t_max, A + ab * lo_ + d * t_max])
    c0 = max(0, int(math.floor((corners[:, 0].min() - gt[0]) / gt[1])) - 1)
    c1 = min(ds.RasterXSize, int(math.ceil((corners[:, 0].max() - gt[0]) / gt[1])) + 1)
    r0 = max(0, int(math.floor((corners[:, 1].max() - gt[3]) / gt[5])) - 1)
    r1 = min(ds.RasterYSize, int(math.ceil((corners[:, 1].min() - gt[3]) / gt[5])) + 1)
    out = {"cells": 0, "area": 0.0, "max_cut": 0.0}
    if c1 <= c0 or r1 <= r0:
        ds = None
        return out
    a = b.ReadAsArray(c0, r0, c1 - c0, r1 - r0).astype(np.float64)
    xs = gt[0] + (np.arange(c0, c1) + 0.5) * gt[1]
    ys = gt[3] + (np.arange(r0, r1) + 0.5) * gt[5]
    X, Y = np.meshgrid(xs, ys)
    s = (X - A[0]) * ab[0] + (Y - A[1]) * ab[1]
    t = (X - A[0]) * d[0] + (Y - A[1]) * d[1]
    valid = np.isfinite(a)
    if nd is not None:
        valid &= a != nd
    zt = np.full(a.shape, float(z_cut))
    if wside > 0:
        off = np.maximum(s_lo - s, s - s_hi)
        zt = np.where(off > 0, z_cut + off / float(side_slope), z_cut)
    m = (s >= lo_) & (s <= hi_) & (t >= t_min) & (t <= t_max) & valid & (a > zt + 1e-9)
    if m.any():
        out = {"cells": int(m.sum()), "area": float(m.sum() * abs(gt[1] * gt[5])),
               "max_cut": float((a[m] - zt[m]).max())}
        a[m] = zt[m]
        b.WriteArray(a.astype(np.float32), c0, r0)
        b.FlushCache()
    ds = None
    return out


def _crest_snap(z, x0, y0, step, P, top, tol, search, half):
    """v3.26.6: when the ground at the dam point P is more than `tol` below the top of dam
    (a dam.json point on the pool or the toe), the nearest ground within `search` that is
    within `tol` of the top, and the direction of the embankment there (the long axis of that
    high ground within `half` of it). Returns (point, unit axis) or None when P is on the
    crest, nothing is high enough or the high ground has no clear long axis (a hill, not an
    embankment). z: north-up array in model units, x0/y0 its top-left corner."""
    P = np.asarray(P, float)
    zp = _sample(z, x0, y0, step, [P])[0]
    if np.isfinite(zp) and zp >= top - tol:
        return None
    ny, nx = z.shape
    X = x0 + (np.arange(nx) + 0.5) * step
    Y = y0 - (np.arange(ny) + 0.5) * step
    GX, GY = np.meshgrid(X, Y)
    with np.errstate(invalid="ignore"):
        high = np.isfinite(z) & (z >= top - tol)
    dist = np.hypot(GX - P[0], GY - P[1])
    cand = high & (dist <= search)
    if not cand.any():
        return None
    i = np.argmin(np.where(cand, dist, np.inf))
    Q = np.array([GX.flat[i], GY.flat[i]])
    loc = high & (np.hypot(GX - Q[0], GY - Q[1]) <= half)
    if loc.sum() < 3:
        return None
    pts = np.column_stack([GX[loc], GY[loc]])
    w, v = np.linalg.eigh(np.cov((pts - pts.mean(axis=0)).T))
    if w[0] <= 0 or w[1] / w[0] >= 2.0:
        t = v[:, 1]
        return Q, t / np.hypot(*t)
    return None


def _connection_cell_mins(pts, A, B, d, z, x0, y0, step, cell):
    """v3.26.6: a function giving, for points on the dam line A-B, the lowest ground of the 2D
    cell just downstream of each (the Voronoi cell of the nearest computation point, the
    ground sampled on the terrain grid in a strip below the line). None when nothing is
    found. The strip only sees part of a cell, so its lowest ground is at or above the
    cell's: raising a weir to it always satisfies HEC-RAS."""
    A = np.asarray(A, float)
    B = np.asarray(B, float)
    d = np.asarray(d, float)
    L = float(np.hypot(*(B - A)))
    if L <= 0:
        return None
    ab = (B - A) / L
    depth = 1.5 * cell
    ns = max(2, int(L / step) + 1)
    nd = max(2, int(depth / step) + 1)
    s_ = np.linspace(0.0, L, ns)
    t_ = np.linspace(0.25 * step, depth, nd)
    S, T = np.meshgrid(s_, t_)
    xy = A[None, :] + S.reshape(-1, 1) * ab[None, :] + T.reshape(-1, 1) * d[None, :]
    zz = _sample(z, x0, y0, step, xy)
    near = np.abs((pts - A) @ np.array([-ab[1], ab[0]])) <= 3.0 * cell
    along = (pts - A) @ ab
    near &= (along > -3.0 * cell) & (along < L + 3.0 * cell)
    P = pts[near]
    if not len(P):
        return None
    owner = np.empty(len(xy), int)
    for i in range(0, len(xy), 4096):
        q = xy[i:i + 4096]
        owner[i:i + 4096] = np.argmin(((q[:, None, :] - P[None, :, :]) ** 2).sum(-1), axis=1)
    mins = np.full(len(P), np.inf)
    ok = np.isfinite(zz)
    np.minimum.at(mins, owner[ok], zz[ok])
    mins[~np.isfinite(mins)] = np.nan

    row = owner[:ns]                 # the strip's first row, right below the line
    row_m = mins[row]

    def at(line_pts):
        # a weir point spills into every cell whose face along the line is within about
        # half a cell of it: take the highest of those cells' lowest ground
        q = np.asarray(line_pts, float)
        qs = (q - A) @ ab
        r = np.sqrt(((q[:, None, :] - P[None, :, :]) ** 2).sum(-1).min(axis=1))
        out = np.full(len(q), np.nan)
        for i in range(len(q)):
            w = np.abs(s_ - qs[i]) <= max(r[i], step)
            if w.any() and np.isfinite(row_m[w]).any():
                out[i] = np.nanmax(row_m[w])
        return out

    def span_max(s0, s1):
        # the cells right below the line between stations s0 and s1 (the breach bottom)
        w = (s_ >= s0) & (s_ <= s1)
        return float(np.nanmax(row_m[w])) if w.any() and np.isfinite(row_m[w]).any() \
            else float("nan")
    at.span_max = span_max
    return at


def _offset_outward(ring, idx, dist):
    """v3.26.6: the points ring[idx] of a closed ring moved `dist` outward, along the ring's
    normal there, so a line drawn along the perimeter lies just outside it."""
    ring = np.asarray(ring, float)
    n = len(ring)
    x, y = ring[:, 0], ring[:, 1]
    ccw = (np.dot(x, np.roll(y, -1)) - np.dot(np.roll(x, -1), y)) > 0
    out = []
    for i in idx:
        t = ring[(i + 1) % n] - ring[(i - 1) % n]
        L = math.hypot(t[0], t[1]) or 1.0
        # outward normal: right of the direction of travel on a counter-clockwise ring
        nx, ny = (t[1] / L, -t[0] / L) if ccw else (-t[1] / L, t[0] / L)
        out.append((ring[i][0] + nx * dist, ring[i][1] + ny * dist))
    return np.asarray(out)


def densify(pts, step, closed=False):
    a = [tuple(map(float, p)) for p in pts]
    if closed:
        a = a + [a[0]]
    out = []
    for p, q in zip(a[:-1], a[1:]):
        d = math.hypot(q[0] - p[0], q[1] - p[1])
        n = max(1, int(math.ceil(d / step)))
        for k in range(n):
            out.append((p[0] + (q[0] - p[0]) * k / n, p[1] + (q[1] - p[1]) * k / n))
    if not closed:
        out.append(a[-1])
    return out


def thin_ring(ring, min_step, keep=()):
    """Drops ring vertices closer than `min_step` to the last kept one (HEC-RAS cells along
    the perimeter get a face per perimeter segment, and at most 8 faces are allowed).
    Vertices in `keep` stay."""
    ring = np.asarray(ring, float)
    keep = set(int(k) for k in keep)
    out = [0]
    for i in range(1, len(ring)):
        if i in keep or np.hypot(*(ring[i] - ring[out[-1]])) >= min_step:
            out.append(i)
    # the closing segment
    while len(out) > 3 and out[-1] not in keep and \
            np.hypot(*(ring[out[-1]] - ring[out[0]])) < min_step:
        out.pop()
    return ring[out]


def point_segment_distance(px, py, seg):
    """Distance from points (arrays) to a polyline (list of (x, y)), vectorised."""
    s = np.asarray(seg, dtype=float)
    best = np.full(np.shape(px), np.inf)
    for (x1, y1), (x2, y2) in zip(s[:-1], s[1:]):
        dx, dy = x2 - x1, y2 - y1
        L2 = dx * dx + dy * dy
        t = np.zeros_like(px) if L2 == 0 else np.clip(((px - x1) * dx + (py - y1) * dy) / L2,
                                                       0.0, 1.0)
        d = np.hypot(px - (x1 + t * dx), py - (y1 + t * dy))
        best = np.minimum(best, d)
    return best


def simplify_profile(st, el, tol, max_points):
    """Douglas-Peucker on a station-elevation profile (keeps the end points)."""
    st = np.asarray(st, float)
    el = np.asarray(el, float)
    while True:
        keep = np.zeros(len(st), bool)
        keep[0] = keep[-1] = True
        stack = [(0, len(st) - 1)]
        while stack:
            i, j = stack.pop()
            if j <= i + 1:
                continue
            x0, y0, x1, y1 = st[i], el[i], st[j], el[j]
            seg = st[i + 1:j]
            yi = y0 + (y1 - y0) * (seg - x0) / ((x1 - x0) or 1.0)
            err = np.abs(el[i + 1:j] - yi)
            k = int(np.argmax(err))
            if err[k] > tol:
                keep[i + 1 + k] = True
                stack += [(i, i + 1 + k), (i + 1 + k, j)]
        if keep.sum() <= max_points:
            return st[keep], el[keep]
        tol *= 1.5


def storage_curve(dam, units, n=40, above_ft=None):
    """[(elevation, volume)] of the plugin's storage law (breach.Reservoir), from the
    reservoir bottom to above the top of dam, in project units."""
    top_m = dam.top_of_dam_ft * FT
    bot_m = top_m - dam.dam_height_ft * FT
    res = brmod.Reservoir(bot_m, top_m, dam.storage_top_acft * ACFT,
                          dam.surface_area_top_ac * 4046.8564224
                          if dam.surface_area_top_ac else None)
    extra_ft = above_ft if above_ft is not None else max(5.0, 0.25 * dam.dam_height_ft)
    hi_m = top_m + extra_ft * FT
    if dam.pmf_max_wse_ft is not None:
        hi_m = max(hi_m, dam.pmf_max_wse_ft * FT + 2 * FT)
    # finer near the bottom, where the power law curves most
    f = np.linspace(0.0, 1.0, n) ** 1.5
    z = bot_m + f * (hi_m - bot_m)
    z = np.unique(np.concatenate([z, [top_m]]))
    out = []
    for zz in z:
        out.append((round(units.length(zz), 2), round(units.volume(float(res.volume(zz))), 3)))
    return out, res


def breach_values(dam, params, units, crest_length_u):
    """Breach editor values (project units) from the run's parameters; same rules as the
    breach table of `hecras.breach_parameter_rows`. Returns (dict, notes)."""
    rows = {r["item"]: r for r in hecras.breach_parameter_rows(dam, params)}
    p = dict(params or {})
    notes = []
    bottom_ft = rows["Final bottom elevation"]["value"]
    width_ft = rows["Final bottom width"]["value"]
    z = rows["Left side slope"]["value"]
    tf = rows["Full formation time"]["value"]
    if z is None:
        z = 0.5
        notes.append("The breach method gives no side slope; 0.5H:1V was used (HEC-RAS "
                     "practice for earthen dams is 0.5 to 1).")
    if width_ft is None:
        width_ft = 3.0 * dam.dam_height_ft
        notes.append("The breach method gives no width; 3 x dam height ({:.0f} {}) was used."
                     .format(units.from_ft(width_ft), units.len_name))
    if tf is None:
        tf = p.get("rise_time_hr") or 1.0
        notes.append("The breach method gives no formation time; {:.2f} h (the plugin's "
                     "hydrograph rise) was used.".format(tf))
    mode = str(p.get("failure_mode") or "").lower()
    piping = mode == "piping"
    trig = p.get("breach_trigger")
    if trig in ("peak", "wse", "time"):
        trig_ft = p.get("pool_at_breach_ft")
        if trig == "time":
            notes.append("The run breached at a set time; HEC-RAS here breaches when the pool "
                         "reaches the level it had at that time ({} ft).".format(trig_ft))
    else:
        trig_ft = p.get("starting_wse_ft") or dam.top_of_dam_ft
    if trig_ft is None:
        trig_ft = dam.top_of_dam_ft
    wse0_ft = p.get("starting_wse_ft") or brmod.starting_wse_ft(dam)[0]
    b = {"bottom_elev": units.from_ft(bottom_ft), "bottom_width": units.from_ft(width_ft),
         "left_slope": float(z), "right_slope": float(z), "formation_hr": float(tf),
         "piping": piping, "piping_coef": float(p.get("pipe_cd") or 0.5),
         "piping_elev": None, "weir_coef": units.weir_coef(hecras.BREACH_WEIR_COEF),
         "trigger_wse": None}
    if piping:
        pe = rows.get("Initial piping elevation", {}).get("value")
        if pe is None:
            hb = dam.top_of_dam_ft - bottom_ft
            pe = bottom_ft + 0.5 * hb
            notes.append("Initial piping elevation: half way up the breach ({:.1f} {}).".format(
                units.from_ft(pe), units.len_name))
        b["piping_elev"] = units.from_ft(pe)
    top_u = units.from_ft(dam.top_of_dam_ft)
    hb_u = top_u - b["bottom_elev"]
    max_bottom = crest_length_u - 2.0 * z * hb_u
    if crest_length_u > 0 and b["bottom_width"] > max_bottom:
        notes.append("The breach bottom width ({:.0f}) does not fit in the dam line ({:.0f}) "
                     "with its side slopes; it was limited to {:.0f}.".format(
                         b["bottom_width"], crest_length_u, max(max_bottom, 0.0)))
        b["bottom_width"] = max(max_bottom, 0.0)
    return b, wse0_ft, trig_ft, notes


# ============================================================================
# v3.12.0: culverts, bridges and notches inside the 2D area (numpy only)
# ============================================================================
STRUCT_FIELDS = ("width_m", "height_m", "barrels", "shape", "invert_up_m", "invert_dn_m",
                 "manning", "losses", "name")


def run_structures(report):
    """The run's culverts, bridges and notches as structures.Structure (line in the run's
    model CRS, upstream end first; sizes and inverts in metres) and where they come from:
    as the run used them ('structures_used', written by 3.12.0 runs after the inventory
    inverts were checked against the LiDAR), else as they were given to the run
    ('settings' > 'structures', in every run report since structures came in)."""
    raw = report.get("structures_used")
    src = "the run's culvert / bridge layer, as the run used it (inverts after its LiDAR check)"
    if not raw:
        raw = (report.get("settings") or {}).get("structures")
        src = ("the run's culvert / bridge layer as it was given to the run (a run before "
               "3.12.0 does not record the inverts its LiDAR check settled on)")
    out = []
    for d in raw or []:
        d = d if isinstance(d, dict) else {}
        try:
            line = np.asarray(d.get("line"), dtype=float).reshape(-1, 2)
        except (TypeError, ValueError):
            line = np.zeros((0, 2))
        out.append(structmod.Structure(str(d.get("kind") or ""), line,
                                       **{k: d[k] for k in STRUCT_FIELDS if k in d}))
    return out, (src if out else "")


def _fill_nan(v):
    v = np.asarray(v, dtype=float)
    ok = np.isfinite(v)
    if ok.all() or not ok.any():
        return v
    i = np.arange(len(v))
    return np.interp(i, i[ok], v[ok])


def _profile(st, el, tol, max_points):
    s2, e2 = simplify_profile(st, el, tol, max_points)
    return [(round(float(a), 2), round(float(b), 2)) for a, b in zip(s2, e2)]


def structure_crest(line, ground, step, flat=0.0):
    """Where a structure line crosses its embankment: the highest ground along it (sampled
    every `step`; the middle of the top where it is level within `flat`). Returns (point,
    index, along, z, dense points, notes), or None without terrain under the line."""
    dense = cond.densify(np.asarray(line, dtype=float).reshape(-1, 2), step)
    along = np.concatenate([[0.0], np.cumsum(np.hypot(*np.diff(dense, axis=0).T))])
    zl = ground(dense)
    if not np.isfinite(zl).any():
        return None
    notes = []
    k = int(np.nanargmax(zl))
    top = np.isfinite(zl) & (zl >= zl[k] - flat)
    i0 = i1 = k
    while i0 > 0 and top[i0 - 1]:
        i0 -= 1
    while i1 < len(top) - 1 and top[i1 + 1]:
        i1 += 1
    k = int(np.argmin(np.abs(along - 0.5 * (along[i0] + along[i1]))))
    if along[k] < step or along[k] > along[-1] - step:
        k = int(np.argmin(np.abs(along - 0.5 * along[-1])))
        notes.append("its line crosses no high ground (embankment) in the terrain; the "
                     "connection is at the middle of the line")
    return dense[k], k, along, zl, dense, notes


def structure_connection(s, line, ground, perimeter, cell, step, units, name, avoid=(),
                         neighbours=(), barrel=None):
    """One Runner structure (structures.Structure) as a connection inside the 2D area.

    `line` is the structure's line in the model CRS (upstream end first), `ground(xy)` the
    terrain the model uses (project units, NaN where there is none), `perimeter` the 2D area,
    `cell` its cell size, `step` the terrain cell (sampling step), `avoid` [(name, line)] the
    connection lines already placed, `neighbours` [(name, crest point)] of the other
    structures. Returns (dict, None), or (None, why it is left out).

    * The embankment crest is the highest ground along the structure line; the connection
      line crosses it there, perpendicular to the structure line, drawn left to right looking
      downstream, and runs along the road each way until the ground is STRUCT_RISE_FT above
      the crest at the structure, or STRUCT_MAX_HALF_FT, keeping one cell inside the 2D area
      and half way (less half a cell) from the crest of any other structure, so that two
      structures on one road get two connections that do not touch.
    * The weir is the terrain along that line (the road crest; HEC-RAS's weir equation, not
      the 2D equations, takes the flow over it).
    * A culvert is one culvert group of the Runner's size, in project units: a box (shape 2,
      chart 8 scale 1) or a concrete pipe (shape 1, chart 1 scale 1), its line's length,
      Manning's n, the inverts the run used or else the terrain at the line's ends, and
      barrels side by side about the crest station. 3.26.8: with `barrel` (a culvert the run
      cut as a gap from the crossings, see crossing_barrel), `line` runs from the channel at
      the toe of the embankment on the upstream side to the downstream one (skewed to the road
      where the channel is) and the inverts are barrel["inverts"] (labelled barrel["from"];
      None = the terrain at that end). The connection line runs along the road
      (perpendicular to barrel["across"], the run's crossing line) over the carriageway crest
      _barrel_crest picks; HEC-RAS lays a barrel centred on the connection line, each end
      half its length across it at the end's own centreline station (barrel_stations), so
      the barrel is long enough to reach both toes from the crest, at least
      BARREL_MIN_CELLS cells, and a skewed barrel's two stations differ.
    * A bridge or a notch is a gap in the weir as wide as its span (times barrels) down to the
      invert (the structure's, else the lowest ground near each end of its line, as the
      Runner's notch cut): an open cut, as LISFLOOD-FP had it - no deck, piers or pressure
      flow."""
    line = np.asarray(line, dtype=float).reshape(-1, 2)
    kind = str(s.kind)
    culvert = kind == structmod.KIND_CULVERT
    if kind not in structmod.KINDS:
        return None, "unknown kind '{}'".format(kind)
    if len(line) < 2 or not np.isfinite(line).all() or cond.polyline_length(line) <= 0:
        return None, "needs a line with two ends"
    pipe = str(s.shape or "").lower() == "pipe"
    sizes = [s.width_m] + ([s.height_m] if culvert and not pipe else [])
    if any(v is None or not math.isfinite(float(v)) or float(v) <= 0 for v in sizes):
        return None, "no size (span{})".format(" / rise" if culvert and not pipe else "")
    nb = max(1, int(s.barrels or 1))
    per = np.asarray(perimeter, dtype=float)
    closed = list(map(tuple, per)) + [tuple(per[0])]
    if not cond.points_in_polygon(line[:, 0], line[:, 1], per).all():
        return None, "outside the downstream 2D area"
    step = max(float(step), 1e-6)
    span = units.length(s.width_m)
    group = nb * span * (1.1 if culvert else 1.0)      # width of the barrels or of the opening
    # --- the embankment crest along the structure line -----------------------------
    tol = units.from_ft(0.1)
    # 3.26.8: a crossing barrel's line is sampled every quarter terrain cell, so that the crest
    # of each carriageway it passes under shows (a 10 m top on a 10 m terrain cell)
    crest = structure_crest(line, ground, step / 4.0 if barrel else step, tol)
    if crest is None:
        return None, "no terrain under its line"
    C, k, along, zl, dense, notes = crest
    total = float(along[-1])
    if barrel:
        # 3.26.8: a crossing culvert's barrel runs from the channel on one side of the road to
        # the other (skewed to the road where the channel is); its connection line runs along
        # the road (across the run's crossing line), over a carriageway's crest
        d = np.asarray(barrel.get("across") if barrel.get("across") is not None else
                       line[-1] - line[0], dtype=float).reshape(2)
        d = d / (float(np.hypot(*d)) or 1.0)
        k, cnotes = _barrel_crest(dense, zl, k, d, tol, units)
        C = dense[k]
        notes = notes + cnotes
    else:
        t = dense[min(k + 1, len(dense) - 1)] - dense[max(k - 1, 0)]
        d = t / (np.hypot(*t) or 1.0)
    zc = float(zl[k]) if np.isfinite(zl[k]) else float(np.nanmax(zl))
    n_left = np.array([-d[1], d[0]])
    # --- connection line: along the road each way ------------------------------------
    rise = units.from_ft(STRUCT_RISE_FT)
    min_half = 0.5 * group + step
    ts = np.arange(step, max(units.from_ft(STRUCT_MAX_HALF_FT), min_half) + 0.5 * step, step)
    halves = []
    for side in (1.0, -1.0):
        pts = C[None, :] + side * n_left[None, :] * ts[:, None]
        z = ground(pts)
        ok = cond.points_in_polygon(pts[:, 0], pts[:, 1], per) & np.isfinite(z)
        ok &= point_segment_distance(pts[:, 0], pts[:, 1], closed) >= cell
        bad = np.nonzero(~ok)[0]
        end = int(bad[0]) if len(bad) else len(ts)            # points before this are usable
        why = "too close to the edge of the 2D area for a connection line across it"
        for other, q in neighbours:                         # stay on this structure's side
            near = np.nonzero(np.hypot(pts[:, 0] - q[0], pts[:, 1] - q[1]) <= ts + cell)[0]
            if len(near) and near[0] < end:
                end = int(near[0])
                why = ("too close to {} for a connection line of its own; add it to that "
                       "connection in HEC-RAS".format(other))
        if end == 0 or ts[end - 1] < min_half - 1e-9:
            return None, why
        high = np.nonzero(z[:end] >= zc + rise)[0]
        half = float(ts[high[0]]) if len(high) else float(ts[end - 1])
        halves.append(max(half, min_half))
    A = C + n_left * halves[0]                 # left end looking downstream
    B = C - n_left * halves[1]
    length = halves[0] + halves[1]
    sc = halves[0]                             # station of the crest point
    probe = A[None, :] + (B - A)[None, :] * np.linspace(0.0, 1.0, 64)[:, None]
    for other, oline in avoid:
        if point_segment_distance(probe[:, 0], probe[:, 1], oline).min() < cell:
            return None, ("its connection line would cross or run along the one of {} (the same "
                          "road?); add it to that connection in HEC-RAS".format(other))
    # --- weir: the terrain along the connection line --------------------------------
    n = int(min(MAX_WEIR_POINTS * 8, max(10, math.ceil(length / step) + 1)))
    st = np.linspace(0.0, length, n)
    el = _fill_nan(ground(A[None, :] + (B - A)[None, :] * (st / length)[:, None]))
    # top width of the road: the crest along the structure line, within 1 ft of its top
    top = np.isfinite(zl) & (zl >= zc - units.from_ft(1.0))
    i0 = i1 = k
    while i0 > 0 and top[i0 - 1]:
        i0 -= 1
    while i1 < len(top) - 1 and top[i1 + 1]:
        i1 += 1
    weir_width = min(max(float(along[i1] - along[i0]), units.from_ft(10.0)), total)
    out = {"name": name, "kind": kind, "source_name": s.name or kind, "line": [tuple(A), tuple(B)],
           "structure_line": [tuple(p) for p in line], "crest": tuple(C), "crest_elev": zc,
           "weir_width": weir_width, "weir_coef": units.weir_coef(STRUCT_WEIR_COEF),
           "culvert": None, "gap": None, "notes": notes}
    label = re.sub(r"[\s,=]+", " ", str(s.name or kind)).strip()
    if culvert:
        out["weir"] = _profile(st, el, tol, MAX_WEIR_POINTS)
        zends = ground(line[[0, -1]])
        if barrel:
            vals = list(barrel["inverts"])
            froms = [f if v is not None else "terrain" for v, f in zip(vals, barrel["from"])]
            inv = [float(v) if v is not None else float(z) for v, z in zip(vals, zends)]
        else:
            inv = [units.length(v) if v is not None else float(z)
                   for v, z in zip((s.invert_up_m, s.invert_dn_m), zends)]
            froms = ["run" if v is not None else "terrain"
                     for v in (s.invert_up_m, s.invert_dn_m)]
        if not all(math.isfinite(v) for v in inv):
            return None, "no terrain at an end of its line and no invert given"
        length = cond.polyline_length(line)
        shift = 0.0
        if barrel:
            # 3.26.8: HEC-RAS lays each barrel centred on the connection line, each end half
            # the barrel length across it at that end's centreline station: long enough from
            # the crest to reach the channel on both sides, its stations apart along the road
            # by as much as the channel crosses it at an angle
            tl = (B - A) / (float(np.hypot(*(B - A))) or 1.0)
            ax = line[-1] - line[0]
            ax = ax / (float(np.hypot(*ax)) or 1.0)
            cosk = float(ax @ d)
            skew = math.degrees(math.acos(max(-1.0, min(1.0, cosk))))
            if skew > BARREL_MAX_SKEW_DEG:
                return None, ("the channel found either side of the road would make its barrel "
                              "cross the road at {:.0f} degrees (more than {:g})".format(
                                  skew, BARREL_MAX_SKEW_DEG))
            shift = float(ax @ tl) / cosk
            p_up = float((C - line[0]) @ d)
            p_dn = float((line[-1] - C) @ d)
            reach = 2.0 * max(p_up, p_dn, 0.0)
            need = BARREL_MIN_CELLS * float(cell)
            u_ = units.len_name
            if reach > p_up + p_dn + max(step, units.from_ft(1.0)):
                notes.append("HEC-RAS centres the barrel on the connection line, on the road "
                             "crest {:,.0f} {u} from the channel upstream and {:,.0f} {u} from it "
                             "downstream: the barrel is {:,.0f} {u} long so that it reaches "
                             "both, and its {} end lies {:,.0f} {u} beyond the toe of the "
                             "embankment".format(max(p_up, 0.0), max(p_dn, 0.0), reach,
                                                 "upstream" if p_dn > p_up else "downstream",
                                                 abs(p_dn - p_up), u=u_))
            if need > reach + 1e-6:
                notes.append("its barrel is {:,.0f} {u} long, {:g} x the 2D cell, so that its two "
                             "ends can fall in different cells".format(need, BARREL_MIN_CELLS,
                                                                        u=u_))
            length = max(reach, need)
        shape, chart, scale, ke = CULVERT_TYPES["pipe" if pipe else "box"]
        rise_u = span if pipe else units.length(s.height_m)
        spacing = 1.1 * span
        stations = [round(sc + (i - 0.5 * (nb - 1)) * spacing, 2) for i in range(nb)]
        out["culvert"] = {"shape": shape, "chart": chart, "scale": scale, "entrance": ke,
                          "exit": CULVERT_EXIT_LOSS, "rise": rise_u, "span": span,
                          "length": round(length, 2),
                          "n": float(s.manning or 0.013), "inv_up": round(inv[0], 2),
                          "inv_dn": round(inv[1], 2), "barrels": nb, "stations": stations,
                          "inverts_from": " / ".join(froms)}
        if barrel:
            out["culvert"]["shift"] = round(shift, 5)
        road = el[np.abs(st - sc) <= 0.5 * group + step]
        if len(road) and float(road.min()) < max(inv) + rise_u - tol:
            notes.append("the road in the terrain at the culvert ({:.1f} {u}) is lower than the "
                         "top of the barrel ({:.1f} {u}): the DEM may be cut through the road "
                         "here, or the size or inverts are off; check the weir and the culvert"
                         .format(float(road.min()), max(inv) + rise_u, u=units.len_name))
        out["desc"] = "Culvert {} from the ND Dam Breach Runner".format(label)
    else:
        # the gap floor: the invert at the crest, from the structure's inverts or the lowest
        # ground near each end of its line (the Runner's notch rule)
        near = max(span, 2.0 * step)
        lo, hi = zl[along <= near], zl[along >= total - near]
        inv = [units.length(v) if v is not None else (float(np.nanmin(z)) if np.isfinite(z).any()
                                                       else float("nan"))
               for v, z in zip((s.invert_up_m, s.invert_dn_m), (lo, hi))]
        if not all(math.isfinite(v) for v in inv):
            return None, "no terrain near an end of its line and no invert given"
        floor = inv[0] + (inv[1] - inv[0]) * float(along[k]) / (total or 1.0)
        a, b = sc - 0.5 * group, sc + 0.5 * group
        za, zb = float(np.interp(a, st, el)), float(np.interp(b, st, el))
        mid = (st > a) & (st < b)
        per_part = MAX_WEIR_POINTS // 3
        left = _profile(np.r_[st[st < a], a], np.r_[el[st < a], za], tol, per_part)
        gap = _profile(np.r_[a, st[mid], b], np.minimum(np.r_[za, el[mid], zb], floor), tol,
                       per_part)
        right = _profile(np.r_[b, st[st > b]], np.r_[zb, el[st > b]], tol, per_part)
        weir = []
        for p in left + gap + right:                # vertical walls: two points at a station
            if not weir or p != weir[-1]:
                weir.append(p)
        out["weir"] = weir
        out["gap"] = {"width": group, "floor": round(floor, 2),
                      "inverts_from": " / ".join("run" if v is not None else "terrain"
                                                 for v in (s.invert_up_m, s.invert_dn_m))}
        out["desc"] = "{} {} from the ND Dam Breach Runner (open gap)".format(
            kind.capitalize(), label)
    return out, None


# ---- 3.26.8: culverts the run cut as gaps from the crossings (gaps.barrel_toes) ----------
def crossing_barrels(report):
    """3.26.8: [(gap line, barrel)] of the crossings a run cut as gaps (CROSSING_GAPS), from
    run_report.json 'crossing_gaps' rows (gaps.barrel_toes), in the run's CRS; a row without
    a barrel (a run before 3.26.8, or barrel_toes failed on it) as {"problem": why}, so that
    the export can say why that culvert keeps the run's line. [] for a run whose crossings
    were only reported."""
    cg = report.get("crossing_gaps") or {}
    out = []
    if not isinstance(cg, dict) or not cg.get("cut"):
        return out
    for r in cg.get("rows") or []:
        if not isinstance(r, dict) or not r.get("line"):
            continue
        b = r.get("barrel")
        if not isinstance(b, dict):
            b = {"problem": PRE_3268}
        else:
            # the inventory's own inverts (ft), by the end they belong to (upstream, downstream)
            b = dict(b, given_ft=[r.get("invert_up_ft"), r.get("invert_dn_ft")])
        try:
            out.append((np.asarray(r["line"], dtype=float).reshape(-1, 2), b))
        except (TypeError, ValueError):
            continue
    return out


def barrel_of(s, barrels, tol=0.01):
    """The crossing barrel of a culvert the run cut as a gap (gaps.structures_from keeps the
    row's gap line as the structure's line), else None."""
    if s.kind != structmod.KIND_CULVERT or not barrels:
        return None
    try:
        line = np.asarray(s.line, dtype=float).reshape(-1, 2)
    except (TypeError, ValueError):
        return None
    for ln, b in barrels:
        if ln.shape == line.shape and np.allclose(ln, line, atol=tol, rtol=0.0):
            return b
    return None


def flow_direction(path, at, across, units):
    """3.26.8: which way the run's flow path (model CRS, downstream) runs across a road at
    `at`: (True when it runs against `across`, False with it, or None, why not). The path
    tells only where it passes within FLOW_PATH_NEAR_FT of `at` and its segment nearest `at`
    crosses the road at FLOW_PATH_MIN_DEG (14.5 degrees) or more: at least that far from
    the road's direction (perpendicular to `across`)."""
    if path is None or len(path) < 2:
        return None, "the run recorded no flow path"
    P = np.asarray(path, dtype=float).reshape(-1, 2)
    seg = np.diff(P, axis=0)
    L2 = (seg ** 2).sum(axis=1)
    if not (L2 > 0).any():
        return None, "the run's flow path has no length"
    m = np.asarray(at, dtype=float).reshape(2)
    t = np.clip(((m - P[:-1]) * seg).sum(axis=1) / np.where(L2 > 0, L2, 1.0), 0.0, 1.0)
    dist = np.where(L2 > 0, np.hypot(*(P[:-1] + seg * t[:, None] - m).T), np.inf)
    j = int(np.argmin(dist))
    if dist[j] > units.from_ft(FLOW_PATH_NEAR_FT):
        return None, "the run's flow path passes {:,.0f} {} from it".format(
            float(dist[j]), units.len_name)
    v = seg[j]
    c = float(v @ np.asarray(across, dtype=float)) / max(
        float(np.hypot(*v)) * float(np.hypot(*across)), 1e-12)
    # c is the cosine of the angle to the crossing line: the sine of the angle to the road
    if math.degrees(math.asin(min(1.0, abs(c)))) < FLOW_PATH_MIN_DEG:
        return None, "the run's flow path runs along the road there (less than {:g} degrees " \
            "to it)".format(FLOW_PATH_MIN_DEG)
    return c < 0, ""


def oriented_line(line, path, units):
    """3.26.8: a crossing culvert's line (the run's gap line, whose first end is arbitrary)
    turned so that it starts upstream when the run's flow path tells (flow_direction).
    Returns (line, how)."""
    line = np.asarray(line, dtype=float).reshape(-1, 2)
    if len(line) < 2:
        return line, ""
    d = line[-1] - line[0]
    flip, why = flow_direction(path, 0.5 * (line[0] + line[-1]), d, units)
    if flip is None:
        return line, ("upstream is the end the run's line starts from, which is arbitrary ({}); "
                      "check which way it runs".format(why))
    return (line[::-1].copy() if flip else line), "upstream is the side the run's flow path " \
                                                  "comes from"


def _no_barrel_why(b, units):
    """Why a crossing barrel has no channel to run to on a side (gaps.barrel_toes 'status')."""
    st = list(b.get("status") or [])
    walk = b.get("walk_m")

    def sides(word):
        return "each" if st.count(word) == 2 else "one"
    if "nodata" in st:
        return "the terrain has no data before the toe of the road embankment on {} side" \
            .format(sides("nodata"))
    if "road" in st:
        return ("another road (or a bend of this one) meets or crosses it on {} side, within "
                "{:g} m of where the toe of the embankment was looked for, so the toe found "
                "there can be on that road's fill".format(sides("road"), gapsmod.CLEAR_M))
    if "beyond" in st:
        return ("beyond the road embankment the ground keeps falling more than {:g} ft in {:g} m "
                "out to {} from the road on {} side (a steep channel or valley side), so the "
                "toe of the embankment was not found".format(
                    gapsmod.TOE_TOL_FT, gapsmod.CLEAR_M,
                    "{:,.0f} m".format(float(walk)) if walk else "the end of the search",
                    sides("beyond")))
    if "ground" in st:
        sm_ = [float(v) for v in (b.get("search_m") or []) if v is not None]
        return ("no channel (a valley at least {:g} ft deep in the toes of the embankment) was "
                "found beside the road on {} side within {} of the inventory's point".format(
                    gapsmod.CHANNEL_DEPTH_FT, sides("ground"),
                    "{:,.0f} m".format(max(sm_)) if sm_ else "the search"))
    return "the toes of the road embankment were not found ({})".format(" / ".join(
        map(str, st)) or "no status")


def crossing_barrel(s, b, to_model, units, cell, path=None):
    """3.26.8: the HEC-RAS barrel of a culvert the run cut as a gap from the crossings, from
    where gaps.barrel_toes found the channel leaving the road embankment on each side.

    Returns (line, barrel, notes): `line` from the upstream end to the downstream one in the
    model CRS (skewed to the road where the channel is; structure_connection makes it long
    enough); `barrel` for structure_connection ({'inverts': [up, dn] in project units,
    'from': [label, label], 'beds': [up, dn] the channel bed found at each end, 'across':
    the unit vector across the road from upstream to downstream (the run's crossing line),
    'status': [up, dn]}); (None, None, [why]) unless the channel was found on both sides
    (status 'channel': a valley of the toes; not 'ground', 'road', 'nodata' or 'beyond', nor
    a run before 3.26.8): the export then keeps the run's line.

    Upstream is the side the run's flow path comes from (`path`, model CRS, flow_direction)
    when it tells; else the side with the higher channel bed (a WARNING with both beds); else
    the end the run's crossing line starts from (arbitrary: a WARNING). A note says when the
    beds disagree with the path. The inverts are the run's (after its LiDAR check) where the
    inventory gave them (b['given_ft'], from the row), each at its own physical end (the
    inventory's upstream invert at the upstream end; an adverse slope is noted), else the
    channel bed at each end (BED_LABEL)."""
    if not isinstance(b, dict):
        return None, None, [PRE_3268]
    if b.get("problem"):
        return None, None, [str(b["problem"])]
    ends = list(b.get("ends") or [])
    status = list(b.get("status") or [])
    if len(ends) != 2 or len(status) != 2:
        return None, None, ["the run recorded the toes of the road embankment in an older "
                            "form"]
    if any(e is None for e in ends) or any(st != "channel" for st in status):
        return None, None, [_no_barrel_why(b, units)]
    u_ = units.len_name
    e = np.asarray(to_model([tuple(map(float, q)) for q in ends]), dtype=float)
    beds = [None if v is None else units.length(float(v) * FT)
            for v in (list(b.get("beds_ft") or []) + [None, None])[:2]]
    if None in beds:
        return None, None, ["the run recorded no channel bed at the toe of the embankment"]
    run = np.asarray(to_model(np.asarray(s.line, dtype=float).reshape(-1, 2)), dtype=float)
    d = run[-1] - run[0]
    across = d / max(float(np.hypot(*d)), 1e-12)
    by_beds = None
    if abs(beds[0] - beds[1]) >= units.from_ft(0.05):
        by_beds = beds[1] > beds[0]
    by_path, why = flow_direction(path, 0.5 * (e[0] + e[1]), across, units)
    notes, warn = [], []
    if by_path is not None:
        flip = by_path
        how = "upstream is the side the run's flow path comes from"
    elif by_beds is not None:
        flip = by_beds
        how = "upstream is the side with the higher channel bed at the toe"
        hi_, lo_ = max(beds), min(beds)
        warn.append("WARNING: which way it runs is from the ground, not the run's flow path "
                    "({}): upstream is the side whose channel bed at the toe is higher, {:.2f} "
                    "{u} against {:.2f} {u} downstream; a ditch, pond or scour hole at the toe "
                    "can make that the wrong way round; check it".format(why, hi_, lo_, u=u_))
    else:
        flip = False
        how = "upstream is the end the run's crossing line starts from"
        warn.append("WARNING: which way it runs is a guess: neither the run's flow path ({}) "
                    "nor the channel beds either side of the road ({:.2f} and {:.2f} {u}) "
                    "tell, so upstream is the end the run's crossing line starts from, which "
                    "is arbitrary; check it".format(why, beds[0], beds[1], u=u_))
    order = [1, 0] if flip else [0, 1]
    if by_path is not None and by_beds is not None and by_beds != by_path:
        notes.append("the ground at the toe is lower on its upstream side ({:.2f} {u}) than on "
                     "its downstream side ({:.2f} {u}): a ditch, pond or scour hole at the toe, "
                     "or a bend in the channel; the run's flow path decides which way it "
                     "runs".format(beds[order[0]], beds[order[1]], u=u_))
    # the inventory's upstream / downstream inverts as the run used them (after its LiDAR
    # check), each at its own physical end; an end the inventory does not give (one the run's
    # LiDAR check filled in at an end of its arbitrary line) takes the channel bed there
    run_inv = (s.invert_up_m, s.invert_dn_m)
    given = (list(b.get("given_ft")) + [None, None])[:2] if b.get("given_ft") is not None \
        else [True, True]
    inv, frm, gv = [], [], []
    for k, i in enumerate(order):
        if run_inv[k] is not None and given[k] is not None:
            inv.append(units.length(run_inv[k]))
            frm.append("run")
            gv.append(units.from_ft(given[k]) if isinstance(given[k], (int, float)) and
                      not isinstance(given[k], bool) else None)
        else:
            inv.append(beds[i])
            frm.append(BED_LABEL)
            gv.append(None)
    line = e[order]
    toe = float(np.hypot(*(line[1] - line[0])))
    if toe <= 0:
        return None, None, ["the ends found either side of the road are one point"]
    dn = across * (-1.0 if flip else 1.0)
    n_roads = int(b.get("roads") or 1)
    offs = [b.get("offsets_m", [None, None])[i] for i in order]
    where = ""
    if any(o is not None and abs(float(o)) >= 3.0 for o in offs):
        where = ", {} along the road from the inventory's point".format(" and ".join(
            "{:.0f} m {}".format(abs(float(o)), side) for o, side in zip(offs, ("upstream",
                                                                               "downstream"))
            if o is not None))
    notes.insert(0, "a culvert the run cut as a gap through the road: in HEC-RAS its barrel "
                    "runs from where the channel leaves the road embankment{} on one side to "
                    "the other ({:,.0f} {u} apart, found in the terrain beside the road{}), "
                    "{}{}".format(
                        "s (beyond the outer of {} carriageways)".format(n_roads)
                        if n_roads > 1 else "", toe, where, how,
                        "; its inverts with no value from the run are the {} at each end (the "
                        "ground there, not an inventory elevation)".format(BED_LABEL)
                        if BED_LABEL in frm else "", u=u_))
    if inv[0] < inv[1] - units.from_ft(0.01):
        notes.append("its upstream invert ({:.2f} {u}) is below its downstream one ({:.2f} {u}): "
                     "the barrel rises downstream (an adverse slope){}".format(
                         inv[0], inv[1], " - the inventory's inverts, kept at their own ends"
                         if frm == ["run", "run"] else "", u=u_))
    bar = {"inverts": inv, "from": frm, "beds": [beds[i] for i in order], "given": gv,
           "across": [float(dn[0]), float(dn[1])], "status": [status[i] for i in order]}
    return line, bar, notes + warn


def _barrel_crest(dense, zl, k0, d, tol, units):
    """3.26.8: where a crossing culvert's connection line crosses its barrel line (`dense`,
    from the upstream end; `zl` the ground along it; `d` the direction across the road,
    downstream): on the crest of a carriageway within CREST_PICK_FT of the highest ground
    (structure_crest's point k0), anywhere on its flat top (within `tol`). HEC-RAS centres
    the barrel on the connection line, so the point taken is the one that puts the upstream
    end at the upstream toe rather than beyond it (the downstream carriageway of a divided
    highway, the downstream edge of the top where the downstream toe is further out: the
    extra length then lies downstream, below the downstream toe, where the channel is lower),
    then the shortest barrel, when that gains at least 3 ft. Returns (index, notes)."""
    ok = np.isfinite(zl)
    if not ok.any():
        return k0, []
    zmax = float(np.nanmax(zl))
    hi = ok & (zl >= zmax - units.from_ft(CREST_PICK_FT))
    cands, i, n = [], 0, len(zl)
    while i < n:
        if not hi[i]:
            i += 1
            continue
        j = i
        while j + 1 < n and hi[j + 1]:
            j += 1
        km = i + int(np.nanargmax(zl[i:j + 1]))
        a = c = km
        while a > i and zl[a - 1] >= zl[km] - tol:
            a -= 1
        while c < j and zl[c + 1] >= zl[km] - tol:
            c += 1
        cands += list(range(a, c + 1))              # anywhere on its flat top
        if i <= k0 <= j and int(k0) not in cands:
            cands.append(int(k0))
        i = j + 1
    if len(cands) < 2:
        return k0, []

    def key(kk):
        p_up = float((dense[kk] - dense[0]) @ d)
        p_dn = float((dense[-1] - dense[kk]) @ d)
        return (max(0.0, p_dn - p_up), max(p_up, p_dn))
    best = min(cands, key=key)
    # only for a real gain (the upstream end at least 3 ft nearer its toe, or the barrel at
    # least 3 ft shorter with it no further)
    gain = units.from_ft(3.0)
    kb, k0_ = key(best), key(k0)
    if not (kb[0] < k0_[0] - gain or (kb[0] <= k0_[0] + 1e-9 and kb[1] < k0_[1] - gain)):
        return k0, []
    # the same carriageway: its top's edge on the downstream side; another: its crest
    same = hi[min(best, k0):max(best, k0) + 1].all()
    return best, [("its connection line is on the {} edge of the road's top ({:.2f} {u}, the "
                   "crest {:.2f} {u})" if same else "its connection line is on the crest of the {} "
                   "carriageway ({:.2f} {u}; the highest ground on its line is {:.2f} {u})")
                  .format("downstream" if best > k0 else "upstream", float(zl[best]), zmax
                          if not same else float(zl[k0]), u=units.len_name) +
                  ": HEC-RAS centres the barrel on the connection line, and from there its "
                  "upstream end reaches nearer the upstream toe"]


def snap_barrel_ends(line, raster, units, radius=None):
    """3.26.8: a crossing barrel's two ends (`line`, from gaps.barrel_toes in the run's DEM)
    moved each onto the lowest pixel of the model's terrain (`raster`: array, x0, ytop, pixel)
    whose centre is within `radius` of it (BARREL_SNAP_M, at least 0.75 pixel), a pixel
    BARREL_SNAP_SLOPE per unit further away counting higher (an end on a steep channel does not
    slide down it): where the terrain is coarse, the channel can be one row of pixels, and a 2D
    cell whose pixels miss it does not reach the channel bed (HEC-RAS takes a cell's lowest
    ground from the pixels in it). Returns (line, distance moved at each end, the pixel's
    elevation at each end (None where it did not move))."""
    line = np.asarray(line, dtype=float).reshape(-1, 2).copy()
    if raster is None:
        return line, [0.0, 0.0], [None, None]
    a, x0, y0, pc = raster
    a = np.asarray(a, dtype=float)
    pc = float(pc)
    r = max(units.length(BARREL_SNAP_M) if radius is None else float(radius), 0.75 * pc)
    moved, zs = [], []
    for i in (0, len(line) - 1):
        e = line[i]
        c0 = max(0, int(math.floor((e[0] - r - x0) / pc)))
        c1 = min(a.shape[1], int(math.ceil((e[0] + r - x0) / pc)) + 1)
        r0 = max(0, int(math.floor((y0 - e[1] - r) / pc)))
        r1 = min(a.shape[0], int(math.ceil((y0 - e[1] + r) / pc)) + 1)
        if c1 <= c0 or r1 <= r0:
            moved.append(0.0)
            zs.append(None)
            continue
        X, Y = np.meshgrid(x0 + (np.arange(c0, c1) + 0.5) * pc, y0 - (np.arange(r0, r1) + 0.5) * pc)
        z = a[r0:r1, c0:c1]
        dd = np.hypot(X - e[0], Y - e[1])
        ok = np.isfinite(z) & (dd <= r)
        if not ok.any():
            moved.append(0.0)
            zs.append(None)
            continue
        cost = np.where(ok, z + BARREL_SNAP_SLOPE * dd, np.inf)
        k = np.unravel_index(int(np.argmin(cost)), cost.shape)
        line[i] = (X[k], Y[k])
        moved.append(float(dd[k]))
        zs.append(float(z[k]))
    return line, moved, zs


def place_barrel(line, bar, raster, units):
    """3.26.8 (build_model): a crossing barrel's ends onto the model's terrain
    (snap_barrel_ends), and an invert that is the ground the run found there (not the run's
    own) lowered to the pixel the end moved onto where that is lower: the run reads its terrain
    at 3 m, which fills a narrow ditch or channel the model's terrain has. The channel bed
    found at an end (bar['beds'], what perch_notes measures the inverts from) goes down to that
    pixel too. Returns (line, bar, notes)."""
    line, moved, zs = snap_barrel_ends(line, raster, units)
    bar = dict(bar, inverts=list(bar["inverts"]))
    if bar.get("beds") is not None:
        bar["beds"] = [v if v is None or z is None else min(float(v), z)
                       for v, z in zip(bar["beds"], zs)]
    notes = []
    for j, word in ((0, "upstream"), (1, "downstream")):
        z = zs[j]
        if z is None or bar["from"][j] == "run" or bar["inverts"][j] is None:
            continue
        if z < bar["inverts"][j] - units.from_ft(0.01):
            notes.append("its {} invert is the lowest ground of the model's terrain at that end, "
                         "{:.2f} {u} (the run read the terrain there in cells of up to 3 m: "
                         "{:.2f} {u})".format(word, z, bar["inverts"][j], u=units.len_name))
            bar["inverts"][j] = z
    return line, bar, notes


def barrel_stations(cv):
    """3.26.8: the (upstream, downstream) centreline station of each barrel of a culvert
    connection (structure_connection 'culvert'): its station on the connection line, less /
    plus the skew `shift` times half the barrel length (a barrel at an angle to the road)."""
    sh = float(cv.get("shift") or 0.0)
    h = 0.5 * float(cv.get("length") or 0.0)
    return [(round(float(sb) - sh * h, 2), round(float(sb) + sh * h, 2))
            for sb in cv.get("stations", [])]


def barrel_ends(c):
    """3.26.8 (as DamSafety Runner 3.10.56): the (headwater end, tailwater end) of each
    culvert barrel of connection `c`, as HEC-RAS lays a barrel with no centreline of its
    own: half the barrel length across the connection line on each side, the headwater end
    at its upstream centreline station and the tailwater end at its downstream one
    (barrel_stations; the same station unless the barrel is skewed). The line is drawn left
    to right looking downstream, so the headwater side is on its right."""
    cv = c.get("culvert") or {}
    A = np.asarray(c["line"][0], float)
    B = np.asarray(c["line"][-1], float)
    t = (B - A) / (float(np.hypot(*(B - A))) or 1.0)
    down = np.array([-t[1], t[0]])                 # left of the line = downstream
    h = 0.5 * float(cv.get("length") or 0.0)
    sh = float(cv.get("shift") or 0.0)
    out = []
    for sb in cv.get("stations", []):
        sb = float(sb)
        out.append((tuple(A + t * (sb - sh * h) - down * h),
                    tuple(A + t * (sb + sh * h) + down * h)))
    return out


def cell_min_ground(points, k, ground, perimeter, step, max_samples=200000, raster=None):
    """3.26.8 (as DamSafety Runner's hecras_breaklines.cell_min_elevations): the lowest ground
    of the 2D cell of computation point k (its Voronoi cell inside the perimeter), as the
    higher of two readings, so that HEC-RAS takes an invert raised to it whichever it uses:
    `ground(xy)` (bilinear) sampled every min(step / 8, a 40th of the distance to the
    nearest point), and the terrain pixels whose centres lie in the cell - from `raster`
    (array, x0, ytop, pixel), else the bilinear samples at least 1.5 pixels (`step`) inside
    the cell, which a pixel outside it does not reach (an upper bound of the pixel minimum).
    On Absaraka the DamSafety Runner found the bilinear reading at or above the cell minimum
    HEC-RAS 6.6 reports; at a sharp step on a cell edge the pixel reading is higher."""
    P = np.asarray(points, float).reshape(-1, 2)
    p = P[int(k)]
    dd = np.hypot(P[:, 0] - p[0], P[:, 1] - p[1])
    nn = np.sort(dd)[1:9]
    if not len(nn) or nn[0] <= 0:
        z = ground(p[None, :])
        return float(z[0]) if np.isfinite(z[0]) else float("nan")
    R = float(nn.max())
    idx = np.nonzero(dd <= 2.0 * R + 1e-9)[0]        # every point that can own a sample
    me = int(np.nonzero(idx == int(k))[0][0])
    Q = P[idx]
    per = None if perimeter is None else np.asarray(perimeter, float)

    def owned(xy, inset=0.0):
        """the points of xy in the cell (and the perimeter), `inset` inside its edges"""
        own = np.zeros(len(xy), bool)
        others = np.delete(Q, me, axis=0)
        gap = np.maximum(np.hypot(*(others - p).T), 1e-12)
        for i in range(0, len(xy), 20000):
            q = xy[i:i + 20000]
            d2 = ((q[:, None, :] - Q[None, :, :]) ** 2).sum(-1)
            o = np.argmin(d2, axis=1) == me
            if inset > 0 and len(others):
                # distance to the edge with each neighbour: (|q-pj|^2 - |q-pk|^2) / 2|pj-pk|
                dj = ((q[:, None, :] - others[None, :, :]) ** 2).sum(-1)
                dk = ((q - p) ** 2).sum(-1)
                o &= ((dj - dk[:, None]) / (2.0 * gap[None, :])).min(axis=1) >= inset
            own[i:i + 20000] = o
        if per is not None:
            own &= cond.points_in_polygon(xy[:, 0], xy[:, 1], per)
            if inset > 0 and own.any():
                ring = list(map(tuple, per)) + [tuple(per[0])]
                w = np.nonzero(own)[0]
                own[w] = point_segment_distance(xy[w, 0], xy[w, 1], ring) >= inset
        return own

    h = max(min(float(step) / 8.0, float(nn[0]) / 40.0), 2.0 * R / math.sqrt(max_samples),
            1e-6)
    g = np.arange(-R, R + 0.5 * h, h)
    X, Y = np.meshgrid(p[0] + g, p[1] + g)
    xy = np.column_stack([X.ravel(), Y.ravel()])
    own = owned(xy)
    z = ground(np.vstack([xy[own], p[None, :]]))
    bil = float(np.nanmin(z)) if np.isfinite(z).any() else float("nan")
    pix = float("nan")
    if raster is not None:
        a, x0, y0, pc = raster
        a = np.asarray(a, float)
        c0 = max(0, int(math.floor((p[0] - R - x0) / pc)))
        c1 = min(a.shape[1], int(math.ceil((p[0] + R - x0) / pc)) + 1)
        r0 = max(0, int(math.floor((y0 - p[1] - R) / pc)))
        r1 = min(a.shape[0], int(math.ceil((y0 - p[1] + R) / pc)) + 1)
        if c1 > c0 and r1 > r0:
            PX, PY = np.meshgrid(x0 + (np.arange(c0, c1) + 0.5) * pc,
                                 y0 - (np.arange(r0, r1) + 0.5) * pc)
            cen = np.column_stack([PX.ravel(), PY.ravel()])
            zp = a[r0:r1, c0:c1].ravel()[owned(cen)]
            if np.isfinite(zp).any():
                pix = float(np.nanmin(zp))
    else:
        inner = owned(xy, 1.5 * float(step))
        if inner.any():
            zi = ground(xy[inner])
            if np.isfinite(zi).any():
                pix = float(np.nanmin(zi))
    vals = [v for v in (bil, pix) if math.isfinite(v)]
    return max(vals) if vals else float("nan")


def fit_barrel(c, points, ground, perimeter, cell, step, units, raster=None, lengthen=True):
    """3.26.8: a crossing culvert's barrels (barrel_ends) with their two ends in two
    different 2D cells inside the 2D area, and each invert at least BARREL_FIT_MARGIN_FT
    above the lowest ground of the cell at its end (cell_min_ground, with the terrain's
    `raster` when given), as DamSafety Runner 3.10.56 cells_check / fit_to_cells: HEC-RAS
    refuses a barrel with both ends in one cell or an invert below its cell. In place, with
    a note for each change:

    * lengthened half a cell at a time (at most BARREL_SPLIT_EXTRA_CELLS cells, never out of
      the 2D area) until both ends of every barrel fall in different cells;
    * then lengthened further only where that brings the cells at its ends down towards the
      inverts laid there (the HEC-RAS terrain can still be the embankment's slope at the
      toe) by at least BARREL_REACH_GAIN_FT, to the shortest length that does, at most
      BARREL_MAX_EXTRA_CELLS cells (or BARREL_MAX_EXTRA_PIXELS terrain cells, `step`) more;
    * then each invert still below its cell + BARREL_FIT_MARGIN_FT is raised to that.

    At each length the two ends of a crossing barrel (one with a skew 'shift' from
    structure_connection) slide along the connection line, each up to BARREL_SNAP_M (at least
    0.75 terrain pixel), onto the 2D cell that reaches lowest: on coarse terrain the channel
    can be one row of pixels that a cell beside it misses; its layout note (barrel_layout_note)
    is the last note, with the final stations. With `lengthen` False (a culvert that keeps the
    run's line: export_crossing) it is not lengthened to reach the inverts and a raised invert
    is a WARNING. cv['raised'] keeps each invert's value before a raise (None: not raised).
    Returns (notes, None), or (None, why it cannot be exported)."""
    cv = c["culvert"]
    P = np.asarray(points, float).reshape(-1, 2)
    per = np.asarray(perimeter, float)
    notes = []
    u_ = units.len_name
    was = float(cv["length"])
    want = [float(cv["inv_up"]), float(cv["inv_dn"])]
    reach = units.from_ft(BARREL_REACH_FT)
    cache = {}

    def cmin(k):
        if k not in cache:
            cache[k] = cell_min_ground(P, k, ground, per, step, raster=raster)
        return cache[k]

    def nearest(q):
        return int(np.argmin((P[:, 0] - q[0]) ** 2 + (P[:, 1] - q[1]) ** 2))
    # a crossing barrel (structure_connection gave it a skew 'shift'): each end slides along
    # the connection line, up to BARREL_SNAP_M (at least 0.75 terrain pixel), onto the 2D cell
    # that reaches lowest, its upstream and downstream stations apart as needed
    base_st = [float(v) for v in cv.get("stations", [])]
    has_shift = "shift" in cv
    base_sh = float(cv.get("shift") or 0.0)
    A = np.asarray(c["line"][0], float)
    B = np.asarray(c["line"][-1], float)
    tl = (B - A) / (float(np.hypot(*(B - A))) or 1.0)
    pc = float(raster[3]) if raster is not None else float(step)
    r_sl = max(units.length(BARREL_SNAP_M), 0.75 * pc)
    n_sl = int(math.ceil(r_sl / max(min(0.25 * float(cell), 0.5 * pc), 1e-6)))
    offs = np.linspace(-r_sl, r_sl, 2 * n_sl + 1)
    offs = offs[np.argsort(np.abs(offs), kind="stable")]       # the nearest first
    slid = {"by": (0.0, 0.0)}

    def slide(ends, j):
        best = None
        for o in offs:
            qs = np.array([np.asarray(pair[j], float) + tl * o for pair in ends])
            if not cond.points_in_polygon(qs[:, 0], qs[:, 1], per).all():
                continue
            v = max(cmin(nearest(q)) for q in qs)
            if math.isfinite(v) and (best is None or v < best[0] - 1e-9):
                best = (v, float(o))
        return 0.0 if best is None else best[1]

    def state(k):
        cv["length"] = round(was + k * 0.5 * float(cell), 2)
        if has_shift and base_st:
            cv["stations"], cv["shift"] = list(base_st), base_sh
            h = 0.5 * float(cv["length"])
            ends = barrel_ends(c)
            du, dd = slide(ends, 0), slide(ends, 1)
            if h > 0 and (du or dd):
                cv["stations"] = [round(v + 0.5 * (du + dd), 3) for v in base_st]
                cv["shift"] = round(base_sh + (dd - du) / (2.0 * h), 6)
            slid["by"] = (du, dd)
        ends = barrel_ends(c)
        ks = [tuple(nearest(q) for q in pair) for pair in ends]
        e = np.array([q for pair in ends for q in pair], float)
        return ks, bool(cond.points_in_polygon(e[:, 0], e[:, 1], per).all())

    def ends_min(ks):
        return [max(cmin(pair[j]) for pair in ks) for j in (0, 1)]

    def excess(ks):
        # how far the cells at the ends stay above the inverts laid there (+ BARREL_REACH_FT)
        return sum(max(0.0, m - w - reach) for m, w in zip(ends_min(ks), want)
                   if math.isfinite(m))
    ks, inside = state(0)
    if not inside:
        cv["length"] = round(was, 2)
        return None, ("its barrel ({:,.0f} {} long, from the channel on one side of the road to "
                      "the other) would end outside the 2D area".format(was, u_))
    # 1. the two ends of every barrel in two cells
    k0 = None
    for k in range(int(round(2 * BARREL_SPLIT_EXTRA_CELLS)) + 1):
        ks, inside = state(k)
        if not inside:
            break
        if all(a != b for a, b in ks):
            k0 = k
            break
    if k0 is None:
        cv["length"] = round(was, 2)
        return None, "both ends of its barrel stay in one 2D cell"
    # 2. the cells at the ends down towards the inverts, where that gains something
    ks0, _ = state(k0)
    e0 = excess(ks0)
    tried = [(k0, e0, ks0)]
    if e0 > 0.0 and lengthen:
        steps = int(math.floor(2.0 * max(BARREL_MAX_EXTRA_CELLS * float(cell),
                                         BARREL_MAX_EXTRA_PIXELS * float(step)) / float(cell)
                               + 1e-9))
        for k in range(k0 + 1, k0 + max(steps, 1) + 1):
            ks, inside = state(k)
            if not inside or any(a == b for a, b in ks):
                break
            tried.append((k, excess(ks), ks))
    e_min = min(t[1] for t in tried)
    k, ks = k0, ks0
    if e0 - e_min >= units.from_ft(BARREL_REACH_GAIN_FT):
        k, _, ks = next(t for t in tried if t[1] <= e_min + units.from_ft(0.05))
    state(k)
    if k:
        parts = []
        if k0:
            parts.append("its two ends fall in different 2D cells (HEC-RAS refuses a barrel "
                         "with both ends in one cell)")
        if k > k0:
            before, after = ends_min(ks0), ends_min(ks)
            ch = ["{} end {:.2f} to {:.2f} {u}".format(w, b_, a_, u=u_)
                  for w, a_, b_ in zip(("upstream", "downstream"), after, before)
                  if b_ > a_ + 1e-6]
            parts.append("the 2D cells at its ends reach nearer the inverts laid there (their "
                         "lowest ground: {}; the HEC-RAS terrain at the toe is still the slope "
                         "of the embankment)".format("; ".join(ch) or "unchanged"))
        notes.append("its barrel was lengthened from {:,.0f} to {:,.0f} {u} so that {}".format(
            was, cv["length"], " and ".join(parts), u=u_))
    moved = [(w, o) for w, o in zip(("upstream", "downstream"), slid["by"]) if abs(o) > 0.005]
    if moved:
        notes.append("{} along the road onto the 2D cell near it that reaches lowest (the "
                     "barrel's upstream and downstream centreline stations are now {})".format(
                         " and ".join("its {} end was moved {:,.1f} {u}".format(w, abs(o), u=u_)
                                      for w, o in moved),
                         ", ".join("{:,.1f} / {:,.1f}".format(a_, b_)
                                   for a_, b_ in barrel_stations(cv))))
    m = units.from_ft(BARREL_FIT_MARGIN_FT)
    mins = ends_min(ks)
    cv["raised"] = [None, None]
    for key, j, word in (("inv_up", 0, "upstream"), ("inv_dn", 1, "downstream")):
        need = mins[j]
        if math.isfinite(need) and cv[key] < need + m - 1e-9:
            new = round(math.ceil((need + m) * 100.0) / 100.0, 2)
            notes.append("{}its {} invert was raised from {:.2f} to {:.2f} {u}, {:g} ft above "
                         "the lowest ground of the 2D cell at that end of the barrel ({:.2f} "
                         "{u}): HEC-RAS refuses a culvert invert below its cell{}".format(
                             "" if lengthen else "WARNING: ", word, cv[key], new,
                             BARREL_FIT_MARGIN_FT, need,
                             "; the cells there ({:,.0f} {u}) do not reach the ground the "
                             "invert was found on".format(float(cell), u=u_)
                             if new - cv[key] > units.from_ft(0.5) else "", u=u_))
            cv["raised"][j] = cv[key]
            cv[key] = new
    cv["cell_min"] = [round(v, 3) for v in mins]
    if has_shift:
        lay = barrel_layout_note(cv, units)
        if lay:
            notes.append(lay)
    return notes, None


def barrel_layout_note(cv, units):
    """3.26.8: the note on a skewed crossing barrel (cv: structure_connection 'culvert', after
    fit_barrel): its skew to the road, its final upstream and downstream centreline stations
    and its length along the channel; '' for a barrel square to the road (under 2 degrees)."""
    sh = float(cv.get("shift") or 0.0)
    skew = math.degrees(math.atan(abs(sh)))
    if skew < 2.0:
        return ""
    L = float(cv.get("length") or 0.0)
    u_ = units.len_name
    return ("the channel crosses the road at an angle: its barrel is skewed {:.0f} degrees to "
            "the road, its upstream and downstream centreline stations {} ({:,.1f} {u} apart "
            "along the connection line; HEC-RAS lays each end half the barrel length across "
            "the line at its own station; check the barrel in RAS Mapper). Its length, {:,.0f} "
            "{u}, is measured across the road; the barrel along the channel is {:,.0f} {u} "
            "long".format(skew, ", ".join("{:,.1f} / {:,.1f}".format(a, b)
                                         for a, b in barrel_stations(cv)),
                          abs(sh) * L, L, L * math.hypot(1.0, sh), u=u_))


def _ft(v, units):
    """A length in project units, in feet."""
    return float(v) * units.L / FT


def _old_line_note(why, line, how, cv, units):
    """3.26.8: the WARNING for a culvert cut from the crossings that keeps the run's line (cv:
    its structure_connection 'culvert', before fit_barrel)."""
    u_ = units.len_name
    return ("WARNING: a culvert the run cut as a gap through the road: its barrel could not be "
            "laid from the channel on one side of the embankment to the other ({}), so it is "
            "exported as before 3.26.8: the run's {:,.0f} {u} crossing line, {}; its inverts are "
            "{:.2f} / {:.2f} {u} ({}; 'terrain' is the ground at that end of the line, which can "
            "be a field or the embankment's slope, not the channel bed)".format(
                why, cond.polyline_length(np.asarray(line, dtype=float).reshape(-1, 2)), how,
                float(cv["inv_up"]), float(cv["inv_dn"]), cv["inverts_from"], u=u_))


def perch_notes(cv, beds, units, cell, given=None):
    """3.26.8: a WARNING for each invert of a culvert the run cut as a gap from the crossings
    (cv: its structure_connection 'culvert' after fit_barrel) that is more than PERCH_FT above
    the channel bed found beside the road at that end (`beds`: [upstream, downstream] in
    project units, None where no channel was found), with the numbers and why it is there:
    raised to the lowest ground of its 2D cell (cv['raised']), the inventory's invert as the
    run used it (`given`: the inventory's own, where known), or the ground at the end of the
    run's line. HEC-RAS holds the water back to that invert."""
    out = []
    if not beds:
        return out
    froms = str(cv.get("inverts_from") or "").split(" / ")
    raised = list(cv.get("raised") or [None, None]) + [None, None]
    cmin = list(cv.get("cell_min") or [None, None]) + [None, None]
    given = list(given or []) + [None, None]
    u_ = units.len_name
    for j, (key, word) in enumerate((("inv_up", "upstream"), ("inv_dn", "downstream"))):
        bed = beds[j] if j < len(beds) else None
        if bed is None or not math.isfinite(float(bed)):
            continue
        inv = float(cv[key])
        if inv <= float(bed) + units.from_ft(PERCH_FT):
            continue
        frm = froms[j] if j < len(froms) else ""
        low = "{:.2f} {}".format(float(cmin[j]), u_) if cmin[j] is not None else "not known"
        if raised[j] is not None and float(raised[j]) <= float(bed) + units.from_ft(PERCH_FT):
            why = ("it was raised from {:.2f} {u} to clear the lowest ground of the 2D cell at "
                   "that end ({}): the {:,.0f} {u} cells there do not reach the channel bed"
                   .format(float(raised[j]), low, float(cell), u=u_))
        elif frm == "run":
            why = ("it is the inventory's invert as the run used it{} (the run's LiDAR check can "
                   "move an inventory invert onto the ground at the ends of its crossing line, "
                   "and the inventory's datum can differ from the terrain's)".format(
                       "" if given[j] is None or abs(float(given[j]) - inv) < units.from_ft(0.01)
                       else " (the inventory gives {:.2f} {u})".format(float(given[j]), u=u_)))
        elif frm == "terrain":
            why = "it is the ground at that end of the run's crossing line, not the channel"
        else:
            why = "it is the {}".format(frm or "invert laid there")
        if raised[j] is not None and float(raised[j]) > float(bed) + units.from_ft(PERCH_FT):
            why += ", then raised from {:.2f} {} to clear the lowest ground of its 2D cell " \
                "({})".format(float(raised[j]), u_, low)
        out.append("WARNING: its {} invert, {:.2f} {u}, is {:.2f} ft above the channel bed found "
                   "beside the road at that end ({:.2f} {u}): {}; HEC-RAS holds the water back "
                   "to that invert; check it".format(word, inv, _ft(inv - float(bed), units),
                                                     float(bed), why, u=u_))
    return out


def crossing_plan(s, b, line_u, to_model, units, cell, path):
    """3.26.8 (build_model): how a culvert the run cut as a gap from the crossings goes to
    HEC-RAS (`b` its gaps.barrel_toes row, crossing_barrels; `line_u` the run's line in the
    model CRS; `path` the run's flow path in the model CRS): {"line", "bar", "notes": its
    barrel from crossing_barrel, or None (and "why" not); "run": the run's line, turned to
    start upstream where the flow path tells (oriented_line), "how": which way it runs, and
    "run_beds": the channel bed barrel_toes found on the side of each of its ends (project
    units; None where it found none) - what export_crossing falls back to}."""
    try:
        bl, bar, bnotes = crossing_barrel(s, b, to_model, units, cell, path)
    except Exception as e:                               # noqa - it keeps the run's line
        bl, bar, bnotes = None, None, ["{}: {}".format(type(e).__name__, e)]
    run, how = oriented_line(line_u, path, units)
    beds = [None, None]
    if isinstance(b, dict) and not b.get("problem"):
        st, bf = list(b.get("status") or []), list(b.get("beds_ft") or [])
        if len(st) == 2 and len(bf) == 2:
            beds = [units.length(float(v) * FT) if v is not None and x == "channel" else None
                    for v, x in zip(bf, st)]
    if len(run) >= 2 and len(line_u) >= 2 and not np.allclose(run[0], line_u[0]):
        beds = beds[::-1]                                # turned: its first end is the other
    return {"line": bl, "bar": bar if bl is not None else None,
            "notes": list(bnotes) if bl is not None else [],
            "why": "" if bl is not None else (bnotes[0] if bnotes else "no reason recorded"),
            "run": run, "how": how, "run_beds": beds}


def export_crossing(s, plan, connect, points, ground, perimeter, cell, step, units,
                    raster=None):
    """3.26.8 (build_model): the HEC-RAS connection of a culvert the run cut as a gap from the
    crossings (`plan`: crossing_plan). `connect(line, barrel)` is structure_connection for it
    (build_model's, with the other structures). Returns (connection, None, line) or (None, why
    it is left out, line).

    * With a barrel (the channel found on both sides): its ends onto the lowest pixel of the
      model's terrain near them (place_barrel), the connection, fit_barrel; then a WARNING if
      the export before 3.26.8 (the run's line) left this culvert out, and perch_notes.
    * Otherwise, or where that cannot be exported: exactly the export before 3.26.8 - the
      run's line (turned to start upstream where the run's flow path tells), its inverts the
      run's or the terrain at the line's ends, left out where that was - with a WARNING that
      says why; then fit_barrel without lengthening to reach the inverts (HEC-RAS refuses a
      barrel with both ends in one cell or an invert below its cell: it is left out, with a
      WARNING, where that cannot be met; a raised invert is a WARNING) and perch_notes against
      the channel bed barrel_toes found on each side."""
    run = plan["run"]
    try:
        c_old, why_old = connect(run, None)
    except Exception as e:                               # noqa - the barrel may still do
        if plan.get("bar") is None:
            raise
        c_old, why_old = None, "{}: {}".format(type(e).__name__, e)
    why_bar = plan.get("why") or ""
    if plan.get("bar") is not None:
        line, fnotes, pnotes = plan["line"], [], []
        try:
            line, bar, pnotes = place_barrel(plan["line"], plan["bar"], raster, units)
            c, why_bar = connect(line, bar)
            if not why_bar:
                fnotes, why_bar = fit_barrel(c, points, ground, perimeter, cell, step, units,
                                             raster=raster)
        except Exception as e:                           # noqa - the run's line instead
            c, why_bar = None, "{}: {}".format(type(e).__name__, e)
        if not why_bar:
            notes = list(plan["notes"]) + pnotes + c["notes"] + fnotes
            if why_old:
                notes.append("WARNING: the HEC-RAS export before 3.26.8 left this culvert out "
                             "({}, with the run's {:,.0f} {} crossing line); it is in now "
                             "because the channel was found on both sides of the road and its "
                             "barrel from one to the other lies in the model's terrain inside "
                             "the 2D area; check it".format(
                                 why_old, cond.polyline_length(run), units.len_name))
            notes += perch_notes(c["culvert"], bar.get("beds"), units, cell, bar.get("given"))
            c["notes"] = notes
            return c, None, line
    if why_old:
        return None, ("{} (its barrel from the channel on one side of the road to the other "
                      "could not be laid either: {})".format(why_old, why_bar)), run
    cv = c_old["culvert"]
    if cv is None:                                       # not a culvert: as before 3.26.8
        return c_old, None, run
    warn = _old_line_note(why_bar, run, plan["how"], cv, units)
    try:
        fnotes, why = fit_barrel(c_old, points, ground, perimeter, cell, step, units,
                                 raster=raster, lengthen=False)
    except Exception as e:                               # noqa - left out, with why
        fnotes, why = None, "{}: {}".format(type(e).__name__, e)
    if why:
        return None, ("WARNING: left out: as the run's {:,.0f} {} crossing line (its barrel from "
                      "the channel on one side of the road to the other could not be laid: {}), "
                      "{}, which HEC-RAS refuses".format(cond.polyline_length(run),
                                                         units.len_name, why_bar, why)), run
    c_old["notes"] = [warn] + c_old["notes"] + fnotes + perch_notes(cv, plan["run_beds"], units,
                                                                    cell)
    return c_old, None, run


def _breakline_name(text):
    """A breakline name for the .g file: no '=' or line breaks (the value is the rest of the
    line), whitespace collapsed, trimmed to 40 characters."""
    return (re.sub(r"[\s=]+", " ", str(text or "")).strip() or "Breakline")[:40]


def connection_name(text, used):
    """A unique HEC-RAS connection name (16 characters, no , or =) from a structure name."""
    base = re.sub(r"[\s,=]+", " ", str(text or "")).strip() or "Structure"
    nm, i = base[:16], 1
    while nm.strip() in used:
        i += 1
        suffix = " {}".format(i)
        nm = base[:16 - len(suffix)].rstrip() + suffix
    used.add(nm.strip())
    return nm.strip()


# ============================================================================
# Building the model from a run (GDAL / OGR)
# ============================================================================
ND_LON = (-104.06, -96.55)
ND_LAT = (45.93, 49.01)
# NAD83 / North Dakota North (ft) and South (ft): North Dakota's State Plane is in
# international feet
ND_NORTH_FT, ND_SOUTH_FT = "EPSG:2265", "EPSG:2266"
ND_ZONE_SPLIT_LAT = 47.33     # approximate; the zone line follows county lines

DEFAULTS = {"side_buffer_ft": 1000.0, "end_buffer_ft": 2640.0, "cell_ft": 0.0,
            "target_cells": 100000, "equation": EQ_SWE, "base_flow_cfs": 0.0,
            "terrain_cell": 0.0, "max_terrain_cells": 150e6, "extent_depth_ft": 0.0,
            "manning_n": None, "courant": None,
            "structures": True,                  # v3.12.0: the run's culverts and bridges
            "breaklines": True,                  # v3.23.0: enforce cell faces along the dam
                                                 #          crest and road embankments
            "breach_cut": True, "refine_breach": True}   # v3.26.6: see build_model


def _ogr():
    from osgeo import gdal, ogr, osr
    return gdal, ogr, osr


def srs_from(text):
    _, _, osr = _ogr()
    s = osr.SpatialReference()
    if s.SetFromUserInput(str(text)) != 0:
        raise HecRasModelError("not a coordinate system: {}".format(str(text)[:80]))
    try:
        s.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    except AttributeError:
        pass
    return s


def transformer(src, dst):
    """f(xy array (n, 2)) -> (n, 2) from srs `src` to srs `dst`."""
    _, _, osr = _ogr()
    if src.IsSame(dst):
        return lambda a: np.asarray(a, dtype=float).reshape(-1, 2)
    ct = osr.CoordinateTransformation(src, dst)

    def f(a):
        a = np.asarray(a, dtype=float).reshape(-1, 2)
        if not len(a):
            return a
        r = ct.TransformPoints([(float(x), float(y)) for x, y in a])
        return np.asarray([(p[0], p[1]) for p in r], dtype=float)
    return f


def crs_units(srs):
    """(Units, metres per CRS unit) of a projected CRS; error for a geographic one."""
    if not srs.IsProjected():
        raise HecRasModelError("the HEC-RAS model needs a projected coordinate system "
                               "(feet or metres), not a geographic one")
    u = srs.GetLinearUnits()
    if abs(u - FT) < 1e-6 or abs(u - US_FT) < 1e-6:
        return Units(False), u
    if abs(u - 1.0) < 1e-9:
        return Units(True), u
    raise HecRasModelError("the model coordinate system must be in feet or metres (its unit "
                           "is {} m)".format(u))


def choose_crs(requested, dem_wkt, lonlat, work_wkt):
    """(srs, why). requested > the DEM's CRS when it is in feet > ND State Plane (US ft) for
    a dam in North Dakota > the run's metric CRS."""
    if requested:
        return srs_from(requested), "chosen"
    if dem_wkt:
        s = srs_from(dem_wkt)
        if s.IsProjected() and abs(s.GetLinearUnits() - 1.0) > 1e-6:
            try:
                crs_units(s)
                return s, "the DEM's coordinate system (feet)"
            except HecRasModelError:
                pass
    if lonlat is not None:
        lon, lat = lonlat
        if ND_LON[0] <= lon <= ND_LON[1] and ND_LAT[0] <= lat <= ND_LAT[1]:
            code = ND_NORTH_FT if lat >= ND_ZONE_SPLIT_LAT else ND_SOUTH_FT
            return srs_from(code), ("North Dakota State Plane {} zone, NAD83, international feet "
                                    "({}); "
                                    "the zone is picked by latitude, choose the other one "
                                    "if your county is in it".format(
                                        "North" if code == ND_NORTH_FT else "South", code))
    if work_wkt:
        return srs_from(work_wkt), "the run's model coordinate system (metres)"
    raise HecRasModelError("no coordinate system for the HEC-RAS model: choose one")


def _epsg_text(srs):
    code = srs.GetAuthorityCode(None)
    name = srs.GetAuthorityName(None)
    return "{}:{}".format(name, code) if code and name else srs.GetName()


def _poly_from_mask(mask, x0, y0, cell, srs):
    """OGR (multi)polygon of the True cells of a north-up mask."""
    gdal, ogr, _ = _ogr()
    ny, nx = mask.shape
    ds = gdal.GetDriverByName("MEM").Create("", nx, ny, 1, gdal.GDT_Byte)
    ds.SetGeoTransform((x0, cell, 0.0, y0, 0.0, -cell))
    ds.SetProjection(srs.ExportToWkt())
    ds.GetRasterBand(1).WriteArray(mask.astype(np.uint8))
    vds = ogr.GetDriverByName("Memory").CreateDataSource("m")
    lyr = vds.CreateLayer("p", srs, ogr.wkbPolygon)
    lyr.CreateField(ogr.FieldDefn("v", ogr.OFTInteger))
    gdal.Polygonize(ds.GetRasterBand(1), ds.GetRasterBand(1), lyr, 0, [], callback=None)
    out = ogr.Geometry(ogr.wkbMultiPolygon)
    for feat in lyr:
        if feat.GetField(0) == 1:
            out.AddGeometry(feat.GetGeometryRef().Clone())
    return out.UnionCascaded() if out.GetGeometryCount() else None


def _rings(geom):
    """Exterior rings of a (multi)polygon as numpy arrays (not closed), largest first."""
    from osgeo import ogr
    polys = []
    t = ogr.GT_Flatten(geom.GetGeometryType())
    if t == ogr.wkbPolygon:
        polys = [geom]
    elif t in (ogr.wkbMultiPolygon, ogr.wkbGeometryCollection):
        polys = [geom.GetGeometryRef(i) for i in range(geom.GetGeometryCount())
                 if ogr.GT_Flatten(geom.GetGeometryRef(i).GetGeometryType()) == ogr.wkbPolygon]
    out = []
    for p in polys:
        r = p.GetGeometryRef(0)
        a = np.asarray(r.GetPoints(), dtype=float)[:, :2]
        if len(a) > 1 and np.allclose(a[0], a[-1]):
            a = a[:-1]
        out.append((abs(p.GetArea()), a, p))
    out.sort(key=lambda t: -t[0])
    return out


def _polygon(pts):
    from osgeo import ogr
    ring = ogr.Geometry(ogr.wkbLinearRing)
    for x, y in pts:
        ring.AddPoint_2D(float(x), float(y))
    ring.AddPoint_2D(float(pts[0][0]), float(pts[0][1]))
    p = ogr.Geometry(ogr.wkbPolygon)
    p.AddGeometry(ring)
    return p


def _line(pts):
    from osgeo import ogr
    g = ogr.Geometry(ogr.wkbLineString)
    for x, y in pts:
        g.AddPoint_2D(float(x), float(y))
    return g


def _point(x, y):
    from osgeo import ogr
    g = ogr.Geometry(ogr.wkbPoint)
    g.AddPoint_2D(float(x), float(y))
    return g


def _warp(path, srs, cell, bounds=None, alg="bilinear", src_nodata=None):
    """A raster in the model CRS on a north-up grid: (array with NaN, x0, ytop)."""
    gdal, _, _ = _ogr()
    from ..damsuite import gdalsafe as GS
    kw = dict(format="MEM", dstSRS=srs.ExportToWkt(), xRes=cell, yRes=cell,
              resampleAlg=alg, dstNodata=-9999.0, outputType=gdal.GDT_Float32,
              multithread=True, targetAlignedPixels=True)
    if bounds is not None:
        kw["outputBounds"] = bounds
    if src_nodata is not None:
        kw["srcNodata"] = src_nodata
    out = GS.checked(gdal.Warp("", path, options=gdal.WarpOptions(**kw)),
                     "reprojecting {}".format(path))
    a = out.GetRasterBand(1).ReadAsArray().astype(np.float64)
    a[a == -9999.0] = np.nan
    gt = out.GetGeoTransform()
    return a, gt[0], gt[3]


def _tif_window(path, bounds, srs, max_cells=2000):
    """v3.12.0: the model's terrain GeoTIFF over `bounds` as (array with NaN, x0, ytop, cell),
    read as it is (the terrain HEC-RAS will have, not resampled again) when the window is at
    most `max_cells` across, else warped to a coarser cell."""
    gdal, _, _ = _ogr()
    ds = gdal.Open(path)
    gt = ds.GetGeoTransform()
    cell = abs(gt[1])
    if max(bounds[2] - bounds[0], bounds[3] - bounds[1]) / cell > max_cells:
        ds = None
        scs = max(bounds[2] - bounds[0], bounds[3] - bounds[1]) / max_cells
        a, x0, y0 = _warp(path, srs, scs, bounds=bounds, alg="bilinear")
        return a, x0, y0, scs
    c0 = max(0, int(math.floor((bounds[0] - gt[0]) / cell)))
    c1 = min(ds.RasterXSize, int(math.ceil((bounds[2] - gt[0]) / cell)))
    r0 = max(0, int(math.floor((gt[3] - bounds[3]) / cell)))
    r1 = min(ds.RasterYSize, int(math.ceil((gt[3] - bounds[1]) / cell)))
    if c1 - c0 < 2 or r1 - r0 < 2:
        ds = None
        return np.full((2, 2), np.nan), bounds[0], bounds[3], cell
    b = ds.GetRasterBand(1)
    a = b.ReadAsArray(c0, r0, c1 - c0, r1 - r0).astype(np.float64)
    nd = b.GetNoDataValue()
    if nd is not None:
        a[a == nd] = np.nan
    ds = None
    return a, gt[0] + c0 * cell, gt[3] - r0 * cell, cell


def _sample(a, x0, y0, cell, xy):
    """Bilinear sample of a north-up array at points (NaN outside)."""
    xy = np.asarray(xy, float).reshape(-1, 2)
    fx = (xy[:, 0] - x0) / cell - 0.5
    fy = (y0 - xy[:, 1]) / cell - 0.5
    ny, nx = a.shape
    c0 = np.clip(np.floor(fx).astype(int), 0, nx - 2) if nx > 1 else np.zeros(len(xy), int)
    r0 = np.clip(np.floor(fy).astype(int), 0, ny - 2) if ny > 1 else np.zeros(len(xy), int)
    tx = np.clip(fx - c0, 0, 1)
    ty = np.clip(fy - r0, 0, 1)
    c1 = np.minimum(c0 + 1, nx - 1)
    r1 = np.minimum(r0 + 1, ny - 1)
    v = (a[r0, c0] * (1 - tx) * (1 - ty) + a[r0, c1] * tx * (1 - ty) +
         a[r1, c0] * (1 - tx) * ty + a[r1, c1] * tx * ty)
    # where a corner is NaN, fall back to the nearest cell
    bad = ~np.isfinite(v)
    if bad.any():
        rr = np.clip(np.round(fy).astype(int), 0, ny - 1)
        cc = np.clip(np.round(fx).astype(int), 0, nx - 1)
        v[bad] = a[rr[bad], cc[bad]]
    out = (fx < -0.5) | (fy < -0.5) | (fx > nx - 0.5) | (fy > ny - 0.5)
    v[out] = np.nan
    return v


def _flood_fill(mask, seed_rc):
    """Cells of `mask` 4-connected to the seed (numpy only)."""
    r, c = seed_rc
    reach = np.zeros_like(mask, dtype=bool)
    if not mask[r, c]:
        return reach
    reach[r, c] = True
    while True:
        grown = reach.copy()
        grown[1:, :] |= reach[:-1, :]
        grown[:-1, :] |= reach[1:, :]
        grown[:, 1:] |= reach[:, :-1]
        grown[:, :-1] |= reach[:, 1:]
        grown &= mask
        if (grown == reach).all():
            return reach
        reach = grown


def _z_factor(report, dem_path):
    """Metres per DEM elevation unit: the run's record, else GDAL's guess."""
    note = str(report.get("dem_elevation_units") or "").lower()
    if report.get("dem_z_to_m"):
        return float(report["dem_z_to_m"]), report.get("dem_elevation_units") or ""
    if note.startswith("us survey feet"):
        return US_FT, note
    if note.startswith("feet"):
        return FT, note
    if note.startswith("metres"):
        return 1.0, note
    from . import gdal_io
    return gdal_io.z_to_m_factor(dem_path)


def _work_crs(report, model_dir):
    if report.get("work_crs"):
        return report["work_crs"]
    qa = os.path.join(model_dir, "qa_dam_inflow_and_flow_path.geojson")
    if os.path.exists(qa):
        try:
            with open(qa) as f:
                return json.load(f)["crs"]["properties"]["name"]
        except (KeyError, ValueError, TypeError, OSError):
            pass
    return None


def _flow_path(model_dir):
    qa = os.path.join(model_dir, "qa_dam_inflow_and_flow_path.geojson")
    try:
        with open(qa) as f:
            for ft in json.load(f)["features"]:
                if ft["geometry"]["type"] == "LineString":
                    return np.asarray(ft["geometry"]["coordinates"], dtype=float)
    except (KeyError, ValueError, TypeError, OSError):
        pass
    return None


def _dam_lonlat(dam_folder):
    p = os.path.join(dam_folder, "dam.json")
    try:
        with open(p) as f:
            d = json.load(f)
        pt = d.get("point") or d.get("location") or {}
        if pt.get("lon") is not None and pt.get("lat") is not None:
            return float(pt["lon"]), float(pt["lat"])
    except (ValueError, OSError, AttributeError, TypeError):
        pass
    return None


def build_model(dam_folder, out_folder=None, dem_path=None, crs=None, options=None,
                log=None, progress=None):
    """Builds the HEC-RAS model of a finished run in `out_folder` (default
    <dam folder>/_hecras/model). Returns a dict with the paths, the Model and notes."""
    log = log or (lambda m: None)
    progress = progress or (lambda p, m="": None)
    o = dict(DEFAULTS)
    o.update({k: v for k, v in (options or {}).items() if v is not None})
    notes = []
    progress(2, "reading the run")
    hy, dam, params = hecras.load_run(dam_folder)
    model_dir = os.path.join(dam_folder, hecras.MODEL_FOLDER)
    with open(os.path.join(model_dir, hecras.RUN_REPORT_JSON)) as f:
        report = json.load(f)
    settings = report.get("settings") or {}
    depth_path = os.path.join(dam_folder, "Depth (Max).tif")
    if not os.path.exists(depth_path):
        raise HecRasModelError("the dam folder has no 'Depth (Max).tif' (run with the final "
                               "rasters written)")
    dem_path = dem_path or (settings.get("input_files") or {}).get("DEM")
    if not dem_path or not os.path.exists(dem_path):
        raise HecRasModelError("the DEM of the run was not found ({}); choose it".format(
            dem_path or "not recorded"))

    # --- coordinate systems --------------------------------------------------------
    gdal, ogr, osr = _ogr()
    from ..damsuite import gdalsafe as GS
    dem_ds = GS.open_raster(dem_path)
    dem_wkt = dem_ds.GetProjection()
    dem_gt = dem_ds.GetGeoTransform()
    dem_nodata = dem_ds.GetRasterBand(1).GetNoDataValue()
    dem_ds = None
    work_text = _work_crs(report, model_dir)
    lonlat = _dam_lonlat(dam_folder)
    dam_xy_work = report.get("dam_xy")
    geo = srs_from("EPSG:4269")
    if work_text and dam_xy_work:
        work = srs_from(work_text)
        lonlat = tuple(transformer(work, geo)([dam_xy_work])[0])
    else:
        work = None
        notes.append("The run did not record its model coordinate system; the dam point "
                     "comes from dam.json and the flow direction from the flooded area.")
    if lonlat is None and work is None:
        raise HecRasModelError("the run records no coordinate system for the dam point and "
                               "there is no dam.json point")
    srs, why = choose_crs(crs, dem_wkt, lonlat, work_text)
    units, u_m = crs_units(srs)
    log("HEC-RAS model coordinate system: {} ({}); {}".format(_epsg_text(srs), why,
                                                              units.label))
    to_model = transformer(work, srs) if work is not None else None
    P = (to_model([dam_xy_work])[0] if to_model is not None else
         transformer(geo, srs)([lonlat])[0])
    if settings.get("breach_xy") and to_model is not None:
        P = to_model([settings["breach_xy"]])[0]
    inflow = (report.get("inflow_point") or {}).get("xy")
    inflow_m = to_model([inflow])[0] if (inflow and to_model is not None) else None
    path_m = None
    fp = _flow_path(model_dir)
    if fp is not None and to_model is not None:
        path_m = to_model(fp)

    # --- flooded extent in the model CRS -----------------------------------------
    progress(10, "flooded extent")
    side = units.from_ft(o["side_buffer_ft"])
    end = units.from_ft(o["end_buffer_ft"])
    run_cell_u = units.length(float(settings.get("cell_size_m") or 10.0))
    acell = max(run_cell_u, side / 8.0, units.from_ft(20.0))
    dep, dx0, dy0 = _warp(depth_path, srs, acell, alg="max")
    thr = o["extent_depth_ft"]
    wet = np.isfinite(dep) & (dep > thr)
    if not wet.any():
        raise HecRasModelError("the run's Depth (Max).tif has no flooded cells")
    ny, nx = wet.shape
    X = dx0 + (np.arange(nx) + 0.5) * acell
    Y = dy0 - (np.arange(ny) + 0.5) * acell
    # flow direction at the dam: towards the inflow point, else the path, else the flood
    d = None
    for q in ([inflow_m] if inflow_m is not None else []) + (
            [path_m[min(len(path_m) - 1, 5)]] if path_m is not None and len(path_m) > 1
            else []):
        v = np.asarray(q, float) - P
        if np.hypot(*v) > 1e-6:
            d = v / np.hypot(*v)
            break
    rr, cc = np.nonzero(wet)
    wx, wy = X[cc], Y[rr]
    dist = np.hypot(wx - P[0], wy - P[1])
    if d is None:
        near = dist <= np.percentile(dist, 10)
        v = np.array([wx[near].mean() - P[0], wy[near].mean() - P[1]])
        d = v / (np.hypot(*v) or 1.0)
        notes.append("Flow direction at the dam taken from the flooded area near it.")
    # v3.26.6: a dam point off the embankment (on the pool or the toe) put the dam line through
    # the reservoir and across the flow direction to the toe, not across the embankment;
    # move it to the crest found nearby and across the embankment's own direction
    res_near = None
    if o.get("snap_crest", True):
        toe_s = units.from_ft((dam.ds_slope_h or 3.0) * dam.dam_height_ft +
                              0.5 * (dam.crest_width_ft or 20.0))
        search = max(3.0 * toe_s, units.from_ft(300.0))
        half = max(2.0 * toe_s, units.from_ft(150.0))
        tol_s = units.from_ft(max(3.0, 0.1 * dam.dam_height_ft))
        top_s = units.from_ft(dam.top_of_dam_ft)
        dsrs_s = srs_from(dem_wkt) if dem_wkt else srs
        step_s = max(units.from_ft(5.0), abs(dem_gt[1]) * (
            dsrs_s.GetLinearUnits() if dsrs_s.IsProjected() else 111320.0) / units.L)
        r_s = search + half + 2 * step_s
        zs, sx0, sy0 = _warp(dem_path, srs, step_s, alg="bilinear",
                             bounds=(P[0] - r_s, P[1] - r_s, P[0] + r_s, P[1] + r_s))
        zs = zs * _z_factor(report, dem_path)[0] / units.L
        snap = _crest_snap(zs, sx0, sy0, step_s, P, top_s, tol_s, search, half)
        if snap is not None:
            Q, t = snap
            nq = np.array([-t[1], t[0]])
            near_w = np.hypot(wx - Q[0], wy - Q[1]) <= max(units.from_ft(1000.0), 3 * half)
            sgn = (wx[near_w] - Q[0]) * nq[0] + (wy[near_w] - Q[1]) * nq[1]
            npos, nneg = int((sgn > 0).sum()), int((sgn < 0).sum())
            if npos != nneg:
                nq = nq if npos > nneg else -nq
            elif float(np.dot(d, nq)) < 0:
                nq = -nq
            turn = math.degrees(math.acos(max(-1.0, min(1.0, float(np.dot(d, nq))))))
            notes.append("The dam point is {:,.0f} {u} from the embankment crest (its ground is "
                         "more than {:.1f} {u} below the top of dam): the dam line goes through "
                         "the crest found there, across the embankment ({:.0f} degrees from the "
                         "direction to the run's inflow point). Check it in RAS Mapper.".format(
                             float(np.hypot(*(Q - P))), tol_s, turn, u=units.len_name))
            log(notes[-1])
            P, d = Q, nq
            res_near = search    # the pool is just behind the crest, not the lowest ground
    n_left = np.array([-d[1], d[0]])
    wet_poly = _poly_from_mask(wet, dx0, dy0, acell, srs)
    # the end of the inundation: the flooded cells farthest from the dam
    far = dist >= dist.max() - max(end, 3 * acell)
    endmask = np.zeros_like(wet)
    endmask[rr[far], cc[far]] = True
    end_poly = _poly_from_mask(endmask, dx0, dy0, acell, srs)
    progress(20, "2D area perimeter")
    dom = wet_poly.Buffer(side, 4)
    if end_poly is not None and end > side:
        dom = dom.Union(end_poly.Buffer(end, 4))
    # cell size of the 2D area
    area_u2 = dom.GetArea()
    cell = units.from_ft(float(o["cell_ft"] or 0.0))
    if not cell:
        lo, hi = (units.from_ft(25.0), units.from_ft(500.0))
        cell = nice_cell(min(hi, max(lo, math.sqrt(area_u2 / float(o["target_cells"])))))
    dom = dom.Simplify(cell / 4.0)
    # cut straight along the dam line, keep the downstream part
    span = 4.0 * math.sqrt(max(area_u2, 1.0)) + 10 * side
    cut = _line([P - n_left * span, P + n_left * span])
    inter = dom.Intersection(cut)
    segs = []
    for g in ([inter] if ogr.GT_Flatten(inter.GetGeometryType()) == ogr.wkbLineString
              else [inter.GetGeometryRef(i) for i in range(inter.GetGeometryCount())]):
        pts = np.asarray(g.GetPoints(), float)[:, :2] if g.GetPointCount() else None
        if pts is not None and len(pts) >= 2:
            segs.append(pts)
    if not segs:
        raise HecRasModelError("the dam line does not cross the flooded area; check the dam "
                               "point")
    t_of = lambda p: float(np.dot(np.asarray(p) - P, n_left))
    seg = min(segs, key=lambda s: 0.0 if min(t_of(s[0]), t_of(s[-1])) <= 0 <= max(
        t_of(s[0]), t_of(s[-1])) else min(abs(t_of(s[0])), abs(t_of(s[-1]))))
    ta, tb = sorted((t_of(seg[0]), t_of(seg[-1])))
    A = P + n_left * tb           # left end looking downstream
    B = P + n_left * ta
    eps = cell * 0.01
    up_len = span
    cutter = _polygon([A + n_left * eps, B - n_left * eps, B - n_left * eps - d * up_len,
                       A + n_left * eps - d * up_len])
    down = dom.Difference(cutter)
    probe = P + d * max(2 * acell, cell)
    parts = _rings(down)
    if not parts:
        raise HecRasModelError("nothing of the 2D area is left below the dam")
    chosen = None
    for _, ring, poly in parts:
        if poly.Contains(_point(*probe)) or poly.Distance(_point(*probe)) < cell:
            chosen = ring
            break
    chosen = chosen if chosen is not None else parts[0][1]
    perim = chosen
    if ring_area(perim) < 0:
        perim = perim[::-1]
    # make A and B perimeter vertices (they are, from the cut) and find the dam edge
    dA = np.hypot(*(perim - A).T)
    dB = np.hypot(*(perim - B).T)
    if dA.min() > cell or dB.min() > cell:
        notes.append("The 2D area's edge at the dam is not straight; check the connection "
                     "line in RAS Mapper.")
    iA, iB = int(np.argmin(dA)), int(np.argmin(dB))
    perim = thin_ring(perim, 0.9 * cell, keep=(iA, iB))
    A = perim[int(np.argmin(np.hypot(*(perim - A).T)))]
    B = perim[int(np.argmin(np.hypot(*(perim - B).T)))]
    dam_len = float(np.hypot(*(A - B)))
    log("2D area: {:,.0f} {}2, dam line {:,.0f} {}".format(abs(ring_area(perim)),
                                                           units.len_name, dam_len,
                                                           units.len_name))

    # --- terrain in the model CRS --------------------------------------------------
    progress(35, "terrain")
    zf, znote = _z_factor(report, dem_path)
    dem_srs = srs_from(dem_wkt) if dem_wkt else srs
    dem_cell_u = abs(dem_gt[1]) * (dem_srs.GetLinearUnits() if dem_srs.IsProjected() else
                                   111320.0) / units.L
    # reservoir search window, upstream of the dam
    if dam.surface_area_top_ac:
        res_r = 3.0 * math.sqrt(dam.surface_area_top_ac * 4046.8564224) / units.L
    else:
        res_r = 3.0 * math.sqrt(dam.storage_top_acft * ACFT / max(dam.dam_height_ft * FT * 0.4,
                                                                 0.1)) / units.L
    res_r = min(max(res_r, units.from_ft(2000.0)), units.length(8 * MILE))
    xs = list(perim[:, 0]) + [P[0] - res_r, P[0] + res_r]
    ys = list(perim[:, 1]) + [P[1] - res_r, P[1] + res_r]
    margin = 2 * cell
    tb_ = (min(xs) - margin, min(ys) - margin, max(xs) + margin, max(ys) + margin)
    tcell = float(o["terrain_cell"] or 0.0) or dem_cell_u
    ncell = (tb_[2] - tb_[0]) * (tb_[3] - tb_[1]) / tcell ** 2
    if ncell > o["max_terrain_cells"]:
        tcell = math.sqrt((tb_[2] - tb_[0]) * (tb_[3] - tb_[1]) / o["max_terrain_cells"])
        tcell = math.ceil(tcell * 10) / 10.0
        notes.append("Terrain written at {:g} {} cells (the DEM's would make more than "
                     "{:,.0f} cells).".format(tcell, units.len_name, o["max_terrain_cells"]))
    out = out_folder or os.path.join(dam_folder, "_hecras", "model")
    os.makedirs(os.path.join(out, "GIS"), exist_ok=True)
    os.makedirs(os.path.join(out, "Terrain"), exist_ok=True)
    terrain_name = "{} Terrain".format(safe_name(dam.dam_id))
    terrain_tif = os.path.join(out, "Terrain", terrain_name + ".tif")
    write_terrain(dem_path, terrain_tif, srs, tcell, tb_, zf / units.L, dem_nodata,
                  "ft" if not units.si else "metre")
    # the terrain at a coarser cell for the layout (at most about 20 million cells), and at
    # its own cell around the dam for the weir profile
    scell = max(tcell, math.sqrt((tb_[2] - tb_[0]) * (tb_[3] - tb_[1]) / 20e6))
    z, zx0, zy0 = _warp(terrain_tif, srs, scell, bounds=tb_, alg="average")
    top_u = units.from_ft(dam.top_of_dam_ft)

    # --- reservoir outline: ground below the top of dam reached from just upstream ---
    progress(50, "reservoir")
    rcell = max(scell, res_r / 300.0)
    k = max(1, int(round(rcell / scell)))
    zc = z[:z.shape[0] // k * k, :z.shape[1] // k * k]
    zc = np.nanmin(zc.reshape(zc.shape[0] // k, k, zc.shape[1] // k, k), axis=(1, 3)) \
        if k > 1 else zc
    rc_cell = scell * k
    RX = zx0 + (np.arange(zc.shape[1]) + 0.5) * rc_cell
    RY = zy0 - (np.arange(zc.shape[0]) + 0.5) * rc_cell
    GX, GY = np.meshgrid(RX, RY)
    upstream = ((GX - P[0]) * d[0] + (GY - P[1]) * d[1]) < -rc_cell
    lat = np.abs((GX - P[0]) * n_left[0] + (GY - P[1]) * n_left[1])
    within = np.hypot(GX - P[0], GY - P[1]) <= res_r
    can = upstream & within & np.isfinite(zc) & (zc <= top_u)
    near = can & (lat <= max(dam_len, 4 * rc_cell)) & (np.hypot(GX - P[0], GY - P[1]) <=
                                                        max(dam_len, 10 * rc_cell))
    if res_near is not None:
        close = near & (np.hypot(GX - P[0], GY - P[1]) <= max(res_near, 3 * rc_cell))
        near = close if close.any() else near
    reservoir = None
    if near.any():
        cand = np.argwhere(near)
        zz = zc[near]
        seed = tuple(cand[int(np.argmin(zz))])
        reach = _flood_fill(can, seed)
        if reach.sum() >= 4:
            rp = _poly_from_mask(reach, zx0, zy0, rc_cell, srs)
            if rp is not None:
                rp = rp.Simplify(rc_cell)
                rings = _rings(rp)
                if rings:
                    reservoir = rings[0][1]
    if reservoir is None or len(reservoir) < 3:
        sa = (dam.surface_area_top_ac * 4046.8564224 / units.L ** 2 if dam.surface_area_top_ac
              else dam_len ** 2)
        Lr = max(sa / max(dam_len, 1.0), cell)
        reservoir = np.array([A - d * cell, A - d * (cell + Lr), B - d * (cell + Lr),
                              B - d * cell])
        notes.append("The reservoir outline is a rectangle with the surface area at the top "
                     "of dam (the DEM did not show it); it is for display only.")
    if ring_area(reservoir) < 0:
        reservoir = reservoir[::-1]
    if len(reservoir) > 500:
        reservoir = reservoir[np.linspace(0, len(reservoir) - 1, 500).astype(int)]

    # --- dam: connection line and weir profile ------------------------------------
    progress(60, "dam crest")
    toe_off = units.from_ft((dam.ds_slope_h or 3.0) * dam.dam_height_ft +
                            0.5 * (dam.crest_width_ft or 20.0))
    wcell = tcell if dam_len / tcell <= 20000 else dam_len / 20000.0
    pad = toe_off + 4 * wcell
    wb = (min(A[0], B[0]) - pad, min(A[1], B[1]) - pad, max(A[0], B[0]) + pad,
          max(A[1], B[1]) + pad)
    zw, wx0, wy0 = _warp(terrain_tif, srs, wcell, bounds=wb, alg="bilinear")
    npts = int(min(MAX_WEIR_POINTS * 8, max(10, dam_len / max(wcell, 1e-9))))
    line = np.linspace(0.0, 1.0, npts)[:, None] * (B - A)[None, :] + A[None, :]
    st = np.hypot(*(line - A).T)
    zl = _sample(zw, wx0, wy0, wcell, line)
    weir_z = np.where(np.isfinite(zl), np.maximum(zl, top_u), top_u)
    s2, e2 = simplify_profile(st, weir_z, units.from_ft(0.1), MAX_WEIR_POINTS)
    weir = [(round(float(a), 2), round(float(b), 2)) for a, b in zip(s2, e2)]
    conn_line = [tuple(A)] + [tuple(p) for p in densify([A, B], max(dam_len / 16.0, cell))[1:-1]] \
        + [tuple(B)]
    # breach centre: where the run's breach inflow point (the low ground at the toe that
    # the runner found) projects onto the dam line; without it, the lowest ground along the
    # toe within the crest around the dam point
    ab = (B - A) / (dam_len or 1.0)
    sP = float(np.dot(P - A, ab))
    if inflow_m is not None:
        sc = float(np.dot(inflow_m - A, ab))
        toe_xy = inflow_m
        centre_how = "the run's breach inflow point projected onto the dam line"
    else:
        toe = line + d[None, :] * toe_off
        zt = _sample(zw, wx0, wy0, wcell, toe)
        crest_len_u = units.from_ft(dam.crest_length_ft) if dam.crest_length_ft else dam_len
        win = np.abs(st - sP) <= max(0.5 * crest_len_u, 3 * tcell)
        if np.isfinite(zt[win]).any():
            k_ = int(np.nanargmin(np.where(np.isfinite(zt[win]), zt[win], np.inf)))
            sc = float(st[win][k_])
            toe_xy = toe[win][k_]
        else:
            sc, toe_xy = sP, P + d * toe_off
        centre_how = "lowest ground at the toe near the dam point"
    bvals, wse0_ft, trig_ft, bnotes = breach_values(dam, params, units, dam_len)
    notes += bnotes
    half = 0.5 * bvals["bottom_width"] + bvals["left_slope"] * (top_u - bvals["bottom_elev"])
    sc = min(max(sc, half), max(dam_len - half, half))
    bvals["center_station"] = round(sc, 1)
    bvals["trigger_wse"] = units.from_ft(trig_ft)
    if bvals["trigger_wse"] is not None and abs(trig_ft - wse0_ft) < 1e-6:
        # sunny day: breach as soon as the run starts (pool starts at the trigger)
        bvals["trigger_wse"] = units.from_ft(wse0_ft - 0.01)
    gb = float(_sample(zw, wx0, wy0, wcell, [toe_xy])[0])
    # v3.26.6 (the DamSafety Runner's breach cut): HEC-RAS refuses a breach bottom below the 2D
    # cells it opens into ('weir breach lower than the cells they are connected to'), and with
    # the dam line on the crest those cells sit on the embankment. The breach bottom is no
    # lower than the ground at the toe (HEC-RAS cannot drain the reservoir lower anyway), and
    # the breach opening through the downstream half of the embankment is cut into the
    # terrain GeoTIFF down to it, so the cells along the breach reach its bottom.
    dn = np.array([-ab[1], ab[0]])
    if float(np.dot(dn, d)) < 0:
        dn = -dn
    if np.isfinite(gb) and bvals["bottom_elev"] < gb - units.from_ft(0.01):
        notes.append("The breach bottom was raised from {:.1f} to {:.1f} {u}, the ground at the "
                     "toe the breach flows onto: HEC-RAS cannot drain the reservoir lower than "
                     "the 2D cells there. The plugin's run breached to {:.1f} {u}.".format(
                         bvals["bottom_elev"], gb, bvals["bottom_elev"], u=units.len_name))
        bvals["bottom_elev"] = round(gb, 2)
        _piping_in_breach(bvals, top_u, notes, units)
    if o.get("breach_cut", True):
        # past each bottom corner and a little upstream of the dam line by about 1.5 terrain
        # cells, so the terrain HEC-RAS interpolates is the breach bottom right at the line
        # and at the corners (DamSafety Runner: breach_corner_margin, t_min < 0)
        cw = 0.5 * max(bvals["bottom_width"], 2.0 * tcell) + 1.5 * tcell
        cut = cut_breach_channel(terrain_tif, A, ab, dn, sc - cw, sc + cw, toe_off + cell,
                                 bvals["bottom_elev"], side_slope=bvals["left_slope"],
                                 z_top=top_u, t_min=-1.5 * tcell)
        if cut["cells"]:
            notes.append("Terrain: the breach opening ({:,.0f} {u} wide, to "
                         "{:,.0f} {u} below the dam line) is cut to the breach bottom {:.2f} {u} "
                         "in the terrain GeoTIFF ({:,} cells, lowered by up to {:.1f} {u}), so "
                         "the 2D cells at the breach reach its bottom.".format(
                             2 * cw, toe_off + cell, bvals["bottom_elev"], cut["cells"],
                             cut["max_cut"], u=units.len_name))
            zw, wx0, wy0 = _warp(terrain_tif, srs, wcell, bounds=wb, alg="bilinear")

    # --- boundary: normal depth where the valley leaves the 2D area ---------------
    progress(70, "downstream boundary")
    dens = np.asarray(densify(perim, cell / 2.0, closed=True))
    zp = _sample(z, zx0, zy0, scell, dens)
    dam_seg_d = point_segment_distance(dens[:, 0], dens[:, 1], [A, B])
    ok = np.isfinite(zp) & (dam_seg_d > side + cell)
    if not ok.any():
        raise HecRasModelError("no ground along the 2D perimeter away from the dam")
    zq = np.where(ok, zp, np.inf)
    i0 = int(np.argmin(zq))
    zmin = float(zq[i0])
    dmax = float(np.nanmax(dep[np.isfinite(dep)])) if np.isfinite(dep).any() else 1.0
    end_depth = dep[rr[far], cc[far]]
    h_bc = units.from_ft(max(10.0, 1.5 * float(np.nanmax(end_depth)) if len(end_depth) else 10.0))
    n = len(dens)
    lo = hi = i0
    lim = n // 3
    while hi - i0 < lim and ok[(hi + 1) % n] and zp[(hi + 1) % n] <= zmin + h_bc:
        hi += 1
    while i0 - lo < lim and ok[(lo - 1) % n] and zp[(lo - 1) % n] <= zmin + h_bc:
        lo -= 1
    idx = [(i % n) for i in range(lo, hi + 1)]
    if len(idx) < 2:
        idx = [(i0 - 1) % n, i0, (i0 + 1) % n]
    bc = _offset_outward(dens, idx, BC_OFFSET_CELLS * cell)
    if len(bc) > MAX_ARC_POINTS:
        bc = bc[np.linspace(0, len(bc) - 1, MAX_ARC_POINTS).astype(int)]
    exit_xy = dens[i0]
    # valley slope at the exit: along the run's flow path when known, else dam toe to exit
    slope, how = None, ""
    if path_m is not None and len(path_m) > 2:
        j = int(np.argmin(np.hypot(*(path_m - exit_xy).T)))
        if np.hypot(*(path_m[j] - exit_xy)) < 3 * side + end:
            along = np.concatenate([[0.0], np.cumsum(np.hypot(*np.diff(path_m, axis=0).T))])
            back = units.from_ft(2640.0)
            k2 = int(np.searchsorted(along, along[j] - back))
            k2 = max(0, min(k2, j - 1))
            zz2 = _sample(z, zx0, zy0, scell, path_m[[k2, j]])
            Lp = along[j] - along[k2]
            if np.isfinite(zz2).all() and Lp > 0:
                slope, how = (zz2[0] - zz2[1]) / Lp, "along the run's flow path, the last " \
                    "{:,.0f} {} before the boundary".format(Lp, units.len_name)
    if slope is None or not slope > 0:
        ref = inflow_m if inflow_m is not None else P + d * toe_off
        zr = _sample(z, zx0, zy0, scell, [ref])[0]
        Lr2 = float(np.hypot(*(exit_xy - ref)))
        slope = (zr - zmin) / Lr2 if (np.isfinite(zr) and Lr2 > 0) else 0.001
        how = "average fall from the dam toe to the boundary (straight line)"
    slope = float(min(max(slope, 1e-4), 0.05))

    # --- 2D computation points -----------------------------------------------------
    progress(80, "2D computation points")
    pts = _grid_points(perim, cell, srs)
    # v3.26.6: finer cells below the breach. With the 2D area's cell (about 100 ft on a dam
    # like Absaraka) the breach opens into one or two cells and HEC-RAS 6.6 does not converge
    # there (20 iterations every step, the breach flow flipping sign step to step); HEC's dam
    # breach guidance is cells no wider than the breach near it. Rings of half and a quarter
    # of the cell (no finer than a third of the breach bottom width) around the breach centre.
    refine = []
    if o.get("refine_breach", True):
        Cxy = A + ab * sc
        fine = nice_cell(max(bvals["bottom_width"] / 3.0, cell / 4.0, units.from_ft(10.0)))
        r1 = max(units.from_ft(500.0), 6.0 * half, 8.0 * fine)
        rings_ = []
        if fine < cell / 2.0 - 1e-9:
            rings_ = [(fine, r1), (nice_cell(cell / 2.0), 2.5 * r1)]
        elif fine < cell - 1e-9:
            rings_ = [(fine, 2.5 * r1)]
        if rings_:
            dist = np.hypot(pts[:, 0] - Cxy[0], pts[:, 1] - Cxy[1])
            keep_pts = [pts[dist > rings_[-1][1]]]
            inner = 0.0
            for fc, rr_ in rings_:
                q = _grid_points(perim, fc, srs)
                dq = np.hypot(q[:, 0] - Cxy[0], q[:, 1] - Cxy[1])
                keep_pts.append(q[(dq > inner) & (dq <= rr_)])
                refine.append({"cell": fc, "radius": rr_})
                inner = rr_
            pts = np.vstack(keep_pts)
    # v3.26.8: HEC-RAS stops with '1 face errors detected on the perimeter of the mesh' on a
    # cell the perimeter cuts in two (a grid point beside a narrow notch of the perimeter;
    # Absaraka with roads, 2026-10-26). Drop those points, as the DamSafety Runner's
    # perimeter pre-check does (3.10.22); their neighbours take the area.
    split = perimeter_split_cells(perim, pts, cell)
    if split is not None and len(split):
        pts = np.delete(pts, split, axis=0)
        notes.append("Perimeter pre-check: {} computation point(s) whose cell the perimeter "
                     "cut in two were removed (HEC-RAS stops with 'face errors detected on "
                     "the perimeter of the mesh').".format(len(split)))
    # v3.26.6: HEC-RAS stops ("has a weir elevation lower than the cells they are connected
    # to ... has a weir breach lower than the cells") when the dam weir or the breach bottom is
    # below the lowest ground of a 2D cell along the connection. Raise them to it.
    cmin = _connection_cell_mins(pts, A, B, dn, zw, wx0, wy0, wcell, cell)
    if cmin is not None:
        cell_at = cmin(line)
        low = np.isfinite(cell_at) & (weir_z < cell_at + units.from_ft(0.01))
        if low.any():
            weir_z = np.where(low, cell_at + units.from_ft(0.01), weir_z)
            s2, e2 = simplify_profile(st, weir_z, units.from_ft(0.01), MAX_WEIR_POINTS)
            # simplifying must not cut a kept profile below the cells again
            weir_z2 = np.interp(st, s2, e2)
            if (weir_z2 < weir_z - 1e-6).any():
                s2, e2 = st[::max(1, len(st) // MAX_WEIR_POINTS + 1)], weir_z[
                    ::max(1, len(st) // MAX_WEIR_POINTS + 1)]
            weir = [(round(float(a), 2), round(float(b) + 0.005, 2)) for a, b in zip(s2, e2)]
            notes.append("The dam weir was raised to the lowest ground of the 2D cells next to "
                         "it at {} place(s) (by up to {:.2f} {}); HEC-RAS does not allow a weir "
                         "below the cells it spills into.".format(
                             int(low.sum()), float(np.nanmax((cell_at - zl)[low])
                                                   if np.isfinite(zl[low]).any() else 0.0),
                             units.len_name))
        floor_ = cmin.span_max(sc - 0.5 * bvals["bottom_width"],
                               sc + 0.5 * bvals["bottom_width"]) + units.from_ft(0.01)
        if np.isfinite(floor_):
            if bvals["bottom_elev"] < floor_ - units.from_ft(0.02):
                notes.append("The breach bottom was raised from {:.1f} to {:.1f} {u}, the lowest "
                             "ground of the 2D cells below the breach: HEC-RAS does not allow a "
                             "breach below the cells it opens into.".format(
                                 bvals["bottom_elev"], floor_, u=units.len_name))
                bvals["bottom_elev"] = round(floor_, 2)
                _piping_in_breach(bvals, top_u, notes, units)
    if len(pts) < 10:
        raise HecRasModelError("the 2D area has fewer than 10 cells at {} {}; choose a "
                               "smaller cell size".format(cell, units.len_name))

    mc = mesh_check(perim, pts, cell)
    if mc and mc["over_8_faces"]:
        notes.append("{} cells along the perimeter have more than 8 faces; HEC-RAS will "
                     "report them. Fix them in RAS Mapper (move or delete the points).".format(
                         mc["over_8_faces"]))

    # --- v3.12.0: the run's culverts, bridges and notches inside the 2D area -------
    structs, ssrc = run_structures(report) if o["structures"] else ([], "")
    sinfo = {"source": ssrc, "exported": [], "skipped": []}
    used = {CONN_NAME, AREA_NAME, SA_NAME}
    if structs:
        progress(84, "culverts and bridges")
    todo = []
    # 3.26.8: a culvert the run cut as a gap from the crossings gets a barrel from the channel
    # on one side of the embankment to the other where the channel was found on both sides
    # (crossing_plan, export_crossing; the run's own models cut its whole gap line); else, or
    # where that cannot be exported, it is exported as before 3.26.8, with a WARNING
    barrels = crossing_barrels(report) if structs else []
    plans = []
    for s in structs:
        label = s.name or s.kind or "?"
        line_u = to_model(np.asarray(s.line, dtype=float)) if (
            to_model is not None and len(s.line)) else np.zeros((0, 2))
        b = barrel_of(s, barrels) if to_model is not None else None
        plan = None
        if b is not None and len(line_u) >= 2:
            plan = crossing_plan(s, b, line_u, to_model, units, cell, path_m)
            line_u = plan["line"] if plan["line"] is not None else plan["run"]
        plans.append(plan)
        if to_model is None:
            todo.append((s, label, line_u, None, "the run did not record its model coordinate "
                                                 "system"))
            continue
        if len(line_u) < 2:
            todo.append((s, label, line_u, None, "needs a line with two ends"))
            continue
        # the terrain around it: the model's own terrain GeoTIFF, as HEC-RAS will have it
        pad = units.from_ft(STRUCT_MAX_HALF_FT) + 4 * cell
        both = line_u if plan is None else np.vstack([line_u, plan["run"]])
        sb = (both[:, 0].min() - pad, both[:, 1].min() - pad,
              both[:, 0].max() + pad, both[:, 1].max() + pad)
        todo.append((s, label, line_u, _tif_window(terrain_tif, sb, srs), None))
    # the crest of each structure in the 2D area, so that connections on one road stay apart
    crests = []
    for i, (s, label, line_u, g, whynot) in enumerate(todo):
        if g is not None and cond.points_in_polygon(line_u[:, 0], line_u[:, 1], perim).all():
            cr = structure_crest(line_u, lambda xy, g=g: _sample(g[0], g[1], g[2], g[3], xy),
                                 g[3], units.from_ft(0.1))
            if cr is not None:
                crests.append((i, label, cr[0]))
    done = set()
    for i, (s, label, line_u, g, whynot) in enumerate(todo):
        if whynot is None:
            ground = (lambda xy, g=g: _sample(g[0], g[1], g[2], g[3], xy))
            avoid = [(e["name"], e["line"]) for e in sinfo["exported"]]

            def connect(ln, br, i=i, s=s, ground=ground, avoid=avoid, g=g):
                c, why = structure_connection(s, ln, ground, perim, cell, g[3], units, "",
                                              avoid, [(lab, q) for j, lab, q in crests if j != i],
                                              barrel=br)
                if why and any(j > i for j, _, _ in crests):
                    # two structures too close for a connection each: the first one takes it
                    c, why = structure_connection(s, ln, ground, perim, cell, g[3], units, "",
                                                  avoid, [(lab, q) for j, lab, q in crests
                                                          if j in done], barrel=br)
                return c, why
            if plans[i] is None:
                c, whynot = connect(line_u, None)
            else:
                c, whynot, line_u = export_crossing(s, plans[i], connect, pts, ground, perim,
                                                    cell, g[3], units, raster=g)
        if whynot:
            sinfo["skipped"].append({"name": label, "kind": s.kind, "why": whynot,
                                     "line": [tuple(p) for p in line_u]})
            log("Structure {} ({}) not exported: {}".format(label, s.kind, whynot))
            continue
        c["name"] = connection_name(label, used)
        sinfo["exported"].append(c)
        done.add(i)
        log("Structure {} ({}): connection '{}' in the 2D area".format(label, s.kind, c["name"]))

    # --- simulation timing ---------------------------------------------------------
    summ = report.get("summary") or {}
    arr = summ.get("latest_arrival_hr") or 0.0
    dur_hr = float(hy.t_s[-1]) / 3600.0 if len(hy.t_s) else 6.0
    sim = max(6.0, 1.25 * max(dur_hr, float(arr) + bvals["formation_hr"] + 2.0))
    sim = float(min(math.ceil(sim), 336.0))
    vmax_fps = float(summ.get("max_velocity_fps") or 10.0)
    hmax_ft = float(summ.get("max_depth_ft") or dmax or 10.0)
    cour = o["courant"] or (1.0 if o["equation"] == EQ_SWE else 2.0)
    # HEC-RAS's Courant number is V dt / dx (velocity, not wave celerity)
    # v3.26.6: the finest cells (below the breach) set the step
    cell_ft = (refine[0]["cell"] if refine else cell) * units.L / FT
    dt = cour * cell_ft / max(vmax_fps, 5.0)
    dt_s, dt_lab = comp_interval(dt)

    # --- inflow (flood scenario) ---------------------------------------------------
    inflow_series = None
    flood = settings.get("flood")
    if report.get("scenario") == "flood":
        src = (settings.get("input_files") or {}).get("PMF inflow hydrograph")
        if src and os.path.exists(src):
            t_h, q_cfs = brmod.read_csv_series(src, "inflow hydrograph")
            step_min = 15 if (t_h[-1] - t_h[0]) <= 120 else 60
            tt = np.arange(0.0, sim + step_min / 60.0, step_min / 60.0)
            qq = np.interp(tt, t_h - t_h[0], q_cfs, right=float(q_cfs[-1]))
            inflow_series = (hecras.ras_interval_label(step_min),
                             [round(units.flow(v * CFS), 2) for v in qq])
        else:
            notes.append("Flood scenario: the inflow hydrograph file was not found; the "
                         "reservoir gets the base flow only. Enter the inflow in HEC-RAS.")
        notes.append("Flood scenario: the spillway and outlets are not in the HEC-RAS dam "
                     "(the weir is the top of dam); add them to the connection.")
        if flood and flood.get("normal_pool_ft") is not None and inflow_series:
            wse0_ft = float(flood["normal_pool_ft"])

    # --- the model -----------------------------------------------------------------
    m = Model()
    m.name = safe_name(dam.dam_id)
    m.title = "{} breach".format(dam.dam_id)[:60]
    m.units = units
    m.perimeter = [tuple(p) for p in perim]
    m.points = pts
    m.cell = cell
    m.manning_n = float(o["manning_n"] or settings.get("manning_n") or 0.06)
    m.reservoir = [tuple(p) for p in reservoir]
    ev, res = storage_curve(dam, units)
    m.elev_volume = ev
    m.conn_line = conn_line
    m.weir = weir
    m.weir_width = units.from_ft(dam.crest_width_ft or 20.0)
    m.weir_coef = units.weir_coef(2.6)
    m.bc_line = [tuple(p) for p in bc]
    m.bc_slope = slope
    m.initial_wse = units.from_ft(wse0_ft)
    m.inflow = inflow_series
    m.base_flow = units.flow(o["base_flow_cfs"] * CFS)
    m.sim_hours = sim
    m.comp_interval = dt_lab
    m.courant_max = float(cour)
    m.equation = o["equation"]
    m.breach = bvals
    m.terrain_name = terrain_name
    m.description = ["Built by the ND Dam Breach Runner from the run in {}.".format(
        os.path.basename(os.path.normpath(dam_folder)))]
    m.structures = sinfo["exported"]            # v3.12.0
    # v3.23.0: breaklines along the hydraulic controls, so cell faces line up on them when
    # the 2D area's computation points are (re)generated in HEC-RAS. The dam crest, and each
    # exported structure's road line (culvert / bridge / notch embankment). Cell sizes left
    # empty = the 2D area's cell size (align faces, no refinement).
    breaklines = []
    if o["breaklines"]:
        if len(conn_line) >= 2:
            breaklines.append({"name": "Dam crest", "polyline": [tuple(p) for p in conn_line],
                               "cell_min": None, "cell_max": None})
        for c in sinfo["exported"]:
            line = c.get("structure_line") or c.get("line")
            if line and len(line) >= 2:
                breaklines.append({"name": _breakline_name(c["name"]),
                                   "polyline": [tuple(p) for p in line],
                                   "cell_min": None, "cell_max": None})
    m.breaklines = breaklines

    # --- write -------------------------------------------------------------------
    progress(88, "writing files")
    paths = write_text_files(m, out)
    esri = srs.Clone()
    esri.MorphToESRI()
    paths["projection"] = os.path.join(out, "GIS", m.projection_name)
    with open(paths["projection"], "w") as f:
        f.write(esri.ExportToWkt())
    # terrain GeoTIFF: only what the model needs (2D area and reservoir) plus a margin
    paths["terrain_tif"] = terrain_tif
    paths["layout"] = write_layout(os.path.join(out, "GIS", "HECRAS Layout.gpkg"), m, srs,
                                   bc_slope_how=how, P=P, skipped=sinfo["skipped"])
    info = {"crs": _epsg_text(srs), "crs_why": why, "units": units.label,
            "len_name": units.len_name, "cell": cell, "refine": refine, "cell_chosen": bool(o["cell_ft"]),
            "cells": int(len(pts)), "area": abs(ring_area(perim)),
            "dam_line": dam_len, "terrain_cell": tcell, "dem_z": znote,
            "comp_interval": dt_lab, "sim_hours": sim, "bc_slope": slope,
            "bc_slope_how": how, "centre_how": centre_how, "initial_wse": m.initial_wse,
            "breach": bvals,
            "side_buffer": side, "end_buffer": end, "weir_points": len(weir),
            "storage_exponent": res.m, "manning_n": m.manning_n,
            "equation": EQUATIONS[m.equation][1], "scenario": report.get("scenario"),
            "edits": settings.get("edits"), "dem": dem_path, "mesh_check": mc,
            "structures": sinfo,                # v3.12.0
            "breaklines": [bl["name"] for bl in m.breaklines]}    # v3.23.0
    paths["readme"] = write_readme(os.path.join(out, "README_HECRAS.md"), m, info, notes, dam,
                                   params)
    # 3.13.0: roads crossing the flow path with no culvert or bridge in the run (candidates)
    cands = report.get("culvert_candidates") or []
    if cands and to_model is not None:
        xy = to_model([(c["x"], c["y"]) for c in cands])
        paths["culvert_candidates"] = os.path.join(out, "GIS", "culvert_candidates.csv")
        with open(paths["culvert_candidates"], "w") as f:
            f.write("name,x,y,along_mi,angle_deg,surveyed,inventory,inventory_kind,"
                    "inventory_span_ft,inventory_barrels\n")
            for c, (x, y) in zip(cands, xy):
                inv = c.get("inventory") or {}           # 3.26.7: NBI crossing near it
                f.write('"{}",{:.2f},{:.2f},{},{},no,"{}",{},{},{}\n'.format(
                    c["name"], x, y, c["along_mi"], c["angle_deg"], inv.get("name", ""),
                    inv.get("kind", ""), "" if inv.get("span_ft") is None else inv["span_ft"],
                    inv.get("barrels", "")))
        with open(paths["readme"], "a") as f:
            f.write("\n## Culvert candidates (not in the model)\n\n"
                    "{} road crossing(s) of the run's flow path have no culvert or bridge in the "
                    "run: `GIS/culvert_candidates.csv` ({}). They are candidates for you to "
                    "check, not surveyed structures, and no connection was written for them. Add "
                    "the real ones in the Geometric Data editor, or give them to the Runner as "
                    "STRUCTURES and export again.\n".format(len(cands), info["crs"]))
        info["culvert_candidates"] = len(cands)
    progress(100, "done")
    return {"folder": out, "paths": paths, "model": m, "info": info, "notes": notes}


def perimeter_split_cells(perimeter, points, cell, reach=3.0):
    """v3.26.8: indices of the points whose Voronoi cell, clipped to the perimeter, falls
    in two or more pieces (checked within `reach` cells of the perimeter), or None without
    scipy. HEC-RAS counts such a cell as a face error on the perimeter and stops."""
    try:
        from scipy.spatial import Voronoi
    except ImportError:
        return None
    pts = np.asarray(points, float)
    per = np.asarray(perimeter, float)
    if len(pts) < 4:
        return np.zeros(0, np.int64)
    poly = _polygon(per)
    xmin, ymin = per.min(axis=0) - 10 * cell
    xmax, ymax = per.max(axis=0) + 10 * cell
    vor = Voronoi(np.vstack([pts, [(xmin, ymin), (xmax, ymin), (xmax, ymax), (xmin, ymax)]]))
    d = point_segment_distance(pts[:, 0], pts[:, 1], list(map(tuple, per)) + [tuple(per[0])])
    out = []
    for i in np.nonzero(d < reach * cell)[0]:
        reg = vor.regions[vor.point_region[i]]
        if not reg or -1 in reg:
            continue
        piece = _polygon(vor.vertices[reg]).Intersection(poly)
        if piece is not None and not piece.IsEmpty() and len(_rings(piece)) > 1:
            out.append(int(i))
    return np.asarray(out, np.int64)


def mesh_check(perimeter, points, cell):
    """A stand-in for HEC-RAS's mesh checks on the cells along the perimeter: the Voronoi
    cell of each point clipped to the perimeter. Returns {'cells_checked', 'max_faces',
    'over_8_faces', 'smallest_fraction'} or None without scipy."""
    try:
        from scipy.spatial import Voronoi
    except ImportError:
        return None
    pts = np.asarray(points, float)
    per = np.asarray(perimeter, float)
    poly = _polygon(per)
    # far-away mirror points close every region
    xmin, ymin = per.min(axis=0) - 10 * cell
    xmax, ymax = per.max(axis=0) + 10 * cell
    frame = [(xmin, ymin), (xmax, ymin), (xmax, ymax), (xmin, ymax)]
    vor = Voronoi(np.vstack([pts, frame]))
    d = point_segment_distance(pts[:, 0], pts[:, 1], list(map(tuple, per)) + [tuple(per[0])])
    edge = np.nonzero(d < 1.5 * cell)[0]
    worst, over, small = 0, 0, 1.0
    for i in edge:
        reg = vor.regions[vor.point_region[i]]
        if not reg or -1 in reg:
            continue
        cellpoly = _polygon(vor.vertices[reg]).Intersection(poly)
        if cellpoly is None or cellpoly.IsEmpty():
            continue
        rings = _rings(cellpoly)
        if not rings:
            continue
        nf = len(rings[0][1])
        worst = max(worst, nf)
        over += nf > 8
        small = min(small, cellpoly.GetArea() / cell ** 2)
    return {"cells_checked": int(len(edge)), "max_faces": int(worst), "over_8_faces": int(over),
            "smallest_fraction": round(float(small), 3)}


def write_terrain(dem_path, path, srs, cell, bounds, factor, src_nodata, zunit, rows=512):
    """The DEM warped into the model CRS on a `cell` grid over `bounds`, elevations times
    `factor` (to project units), as a tiled Float32 GeoTIFF. The warp is a virtual raster
    read in blocks of rows, so a large terrain never has to fit in memory."""
    gdal, _, _ = _ogr()
    from ..damsuite import gdalsafe as GS
    drv = gdal.GetDriverByName("GTiff")
    if os.path.exists(path):
        drv.Delete(path)
    kw = dict(format="VRT", dstSRS=srs.ExportToWkt(), xRes=cell, yRes=cell,
              outputBounds=bounds, resampleAlg="bilinear", dstNodata=-9999.0,
              outputType=gdal.GDT_Float32)
    if src_nodata is not None:
        kw["srcNodata"] = src_nodata
    vrt = GS.checked(gdal.Warp("", dem_path, options=gdal.WarpOptions(**kw)),
                     "reprojecting the DEM {}".format(dem_path))
    nx, ny = vrt.RasterXSize, vrt.RasterYSize
    ds = GS.checked(drv.Create(path, nx, ny, 1, gdal.GDT_Float32,
                               options=["COMPRESS=DEFLATE", "TILED=YES", "PREDICTOR=3",
                                        "BIGTIFF=IF_SAFER"]), "creating {}".format(path))
    ds.SetGeoTransform(vrt.GetGeoTransform())
    ds.SetProjection(srs.ExportToWkt())
    b = ds.GetRasterBand(1)
    b.SetNoDataValue(-9999.0)
    src = vrt.GetRasterBand(1)
    for r0 in range(0, ny, rows):
        n = min(rows, ny - r0)
        a = src.ReadAsArray(0, r0, nx, n).astype(np.float32)
        ok = a != -9999.0
        if abs(factor - 1.0) > 1e-12:
            a[ok] = a[ok] * np.float32(factor)
        b.WriteArray(a, 0, r0)
    b.SetUnitType(zunit)
    b.FlushCache()
    ds = vrt = None
    return path


def _write_tif(path, a, x0, y0, cell, srs, zunit):
    gdal, _, _ = _ogr()
    from ..damsuite import gdalsafe as GS
    drv = gdal.GetDriverByName("GTiff")
    if os.path.exists(path):
        drv.Delete(path)
    ds = GS.checked(drv.Create(path, a.shape[1], a.shape[0], 1, gdal.GDT_Float32,
                               options=["COMPRESS=DEFLATE", "TILED=YES", "PREDICTOR=3",
                                        "BIGTIFF=IF_SAFER"]), "creating {}".format(path))
    ds.SetGeoTransform((x0, cell, 0.0, y0, 0.0, -cell))
    ds.SetProjection(srs.ExportToWkt())
    b = ds.GetRasterBand(1)
    b.SetNoDataValue(-9999.0)
    b.SetUnitType(zunit)
    b.WriteArray(np.where(np.isfinite(a), a, -9999.0).astype(np.float32))
    b.FlushCache()
    ds = None
    return path


def write_layout(path, m, srs, bc_slope_how="", P=None, skipped=()):
    """The model's geometry as GeoPackage layers in the model CRS, to check in QGIS.
    v3.12.0: with structures, 'Structure connections' (the connection lines in the 2D area)
    and 'Structures from the run' (each run structure's line, exported or why not)."""
    _, ogr, _ = _ogr()
    drv = ogr.GetDriverByName("GPKG")
    if os.path.exists(path):
        drv.DeleteDataSource(path)
    ds = drv.CreateDataSource(path)

    def layer(name, gtype, rows):
        lyr = ds.CreateLayer(name, srs, gtype)
        keys = []
        for _, props in rows:
            for k, v in props.items():
                if k not in keys:
                    keys.append(k)
                    lyr.CreateField(ogr.FieldDefn(k, ogr.OFTReal if isinstance(v, float)
                                                  else ogr.OFTString))
        for geom, props in rows:
            f = ogr.Feature(lyr.GetLayerDefn())
            for k, v in props.items():
                f.SetField(k, v)
            f.SetGeometry(geom)
            lyr.CreateFeature(f)
    u = m.units.len_name
    layer("2D flow area", ogr.wkbPolygon, [(_polygon(m.perimeter), {
        "name": AREA_NAME, "cell_size": float(m.cell), "cells": str(len(m.points)),
        "manning_n": float(m.manning_n), "units": u})])
    layer("Reservoir storage area", ogr.wkbPolygon, [(_polygon(m.reservoir), {
        "name": SA_NAME, "note": "display only; HEC-RAS uses the elevation-volume curve"})])
    b = m.breach
    layer("Dam connection", ogr.wkbLineString, [(_line(m.conn_line), {
        "name": CONN_NAME, "breach_center_sta": float(b["center_station"]),
        "breach_bottom_width": float(b["bottom_width"]),
        "breach_bottom_elev": float(b["bottom_elev"]), "units": u})])
    layer("Normal depth boundary", ogr.wkbLineString, [(_line(m.bc_line), {
        "name": BC_NAME, "friction_slope": float(m.bc_slope), "slope_from": bc_slope_how})])
    # breach centre on the line
    A = np.asarray(m.conn_line[0])
    B = np.asarray(m.conn_line[-1])
    L = float(np.hypot(*(B - A))) or 1.0
    c = A + (B - A) * float(b["center_station"]) / L
    rows = [(_point(*c), {"what": "breach centre"})]
    if P is not None:
        rows.append((_point(*P), {"what": "dam point of the run"}))
    layer("Breach centre", ogr.wkbPoint, rows)
    # v3.12.0: culverts, bridges and notches
    if m.structures:
        layer("Structure connections", ogr.wkbLineString, [(_line(c["line"]), {
            "name": c["name"], "kind": c["kind"], "structure": c["source_name"],
            "hecras": "culvert group" if c["culvert"] else "weir with an open gap",
            "units": u}) for c in m.structures])
    rows = [(_line(c["structure_line"]), {"name": c["source_name"], "kind": c["kind"],
                                          "exported": "yes", "connection": c["name"]})
            for c in m.structures]
    rows += [(_line(sk["line"]), {"name": sk["name"], "kind": sk["kind"], "exported": "no",
                                  "connection": sk["why"]})
             for sk in skipped if len(sk["line"]) >= 2]
    if rows:
        layer("Structures from the run", ogr.wkbLineString, rows)
    # v3.23.0: breaklines (the dam crest and road embankments the mesh should follow)
    if m.breaklines:
        layer("Breaklines", ogr.wkbLineString, [(_line(bl["polyline"]), {
            "name": bl["name"],
            "cell_size": "2D area" if bl.get("cell_min") is None else num(bl["cell_min"], 2)})
            for bl in m.breaklines if len(bl["polyline"]) >= 2])
    ds = None
    return path


def write_readme(path, m, info, notes, dam, params):
    u = m.units
    L = u.len_name
    b = info["breach"]
    bl_g01 = ", breaklines" if m.breaklines else ""
    lines = [
        "# HEC-RAS model: {}".format(m.title), "",
        "Built by the ND Dam Breach Runner the way HEC's tutorial *Dam Breach - Simple 2D "
        "Geometry* builds a model: the reservoir is a storage area, the dam is an SA/2D "
        "connection with the breach, and the valley below is a 2D flow area. **The values "
        "are the plugin's screening estimates; review them before a submittal.**", "",
        "## Files", "",
        "| File | What |", "|---|---|",
        "| `{0}.prj` | HEC-RAS project ({1}) |".format(m.name, u.label),
        "| `{0}.g01` | Geometry: storage area, 2D area ({1:,} cells of {2:g} {3}), dam "
        "connection, boundary line{4} |".format(m.name, info["cells"], info["cell"], L, bl_g01),
        "| `{0}.u01` | Unsteady flow: initial pool, inflow, normal depth |".format(m.name),
        "| `{0}.p01` | Plan: {1:g} h, {2} time step, {3}, the breach |".format(
            m.name, info["sim_hours"], info["comp_interval"], info["equation"]),
        "| `{0}.rasmap` | RAS Mapper: projection and geometry |".format(m.name),
        "| `GIS/Projection.prj` | Coordinate system {} |".format(info["crs"]),
        "| `Terrain/{0}.tif` | DEM in that system, elevations in {1}, {2:g} {1} cells |".format(
            m.terrain_name, L, info["terrain_cell"]),
        "| `GIS/HECRAS Layout.gpkg` | The same geometry as layers, to check in QGIS |", "",
        "## Steps left to do in HEC-RAS (6.x)", "",
        "HEC-RAS makes two files itself, in its own binary format: the terrain and the "
        "preprocessed geometry.", "",
        "1. Open `{}.prj` in HEC-RAS. Open RAS Mapper; if it asks for the projection, choose "
        "`GIS/Projection.prj`.".format(m.name),
        "2. RAS Mapper: *Project > Create a New RAS Terrain*, add `Terrain/{}.tif`, save it in "
        "`Terrain/` (vertical units {}).".format(m.terrain_name, L),
        "3. Right-click the geometry > *Manage Geometry Associations* (6.x: Geometry > "
        "Terrain) and pick that terrain.",
        "4. Still in RAS Mapper, right-click the 2D flow area > *Edit 2D Area Properties* and "
        "press *Generate Computation Points* only if you want a different cell size; the "
        "points are already there.{0} Then *Geometry > Compute Property Tables* (or let the "
        "plan's geometry preprocessor do it). Check the mesh messages.".format(
            " The breaklines (the dam crest and the roads) are written into the geometry but "
            "only line the cells up on themselves once you regenerate the computation points."
            if m.breaklines else ""),
        "5. (Optional) Land cover: the 2D area uses one Manning's n ({:g}). If the run used "
        "an NLCD or Manning raster, make a RAS land cover layer and associate it.".format(
            m.manning_n),
        "6. Open the plan `{}.p01` and *Compute*. Then compare the HEC-RAS results with the "
        "plugin's: *Import HEC-RAS 2D results as rasters* reads the `.p01.hdf`.".format(m.name),
        "",
        "## Values and where they come from", "",
        "| Item | Value | From |", "|---|---|---|",
        "| Coordinate system | {} | {} |".format(info["crs"], info["crs_why"]),
        "| DEM elevations | {} | {} |".format(L, info["dem_z"]),
        "| 2D area | {:,.0f} {}2 | flooded extent of the run + {:,.0f} {} each side + {:,.0f} {} "
        "at the end of the inundation, cut along the dam line |".format(
            info["area"], L, info["side_buffer"], L, info["end_buffer"], L),
        "| Cell size | {:g} {} | {} |".format(info["cell"], L,
                                             "chosen" if info.get("cell_chosen") else
                                             "about {:,} cells".format(DEFAULTS["target_cells"])),
    ] + ["| Finer cells below the breach | {:g} {} within {:,.0f} {} of the breach centre | "
         "HEC-RAS does not converge where the breach opens into one or two cells |".format(
             r["cell"], L, r["radius"], L) for r in info.get("refine") or []] + [
        "| Manning's n | {:g} | the run's default n |".format(m.manning_n),
        "| Reservoir curve | {} points | the plugin's storage law: storage at the top of dam, "
        "surface area, dam height (exponent {:.2f}) |".format(len(m.elev_volume),
                                                             info["storage_exponent"]),
        "| Initial pool | {:.2f} {} | the run's starting water surface |".format(
            info["initial_wse"], L),
        "| Dam weir | {} points along {:,.0f} {} | top of dam {:.2f}, or the ground where "
        "higher; coefficient {:g} |".format(info["weir_points"], info["dam_line"], L,
                                            u.from_ft(dam.top_of_dam_ft), m.weir_coef),
        "| Breach centre station | {:.1f} {} | {} |".format(b["center_station"], L,
                                                          info["centre_how"]),
        "| Breach bottom width / elevation | {:.1f} / {:.2f} {} | the run's breach "
        "parameters |".format(b["bottom_width"], b["bottom_elev"], L),
        "| Side slopes | {:g} H:1V | the run |".format(b["left_slope"]),
        "| Formation time | {:g} h, linear | the run |".format(b["formation_hr"]),
        "| Failure mode | {} | the run |".format("Piping" if b["piping"] else "Overtopping"),
        "| Breach weir coefficient | {:g} | the plugin's breach weir (HEC-RAS default 2.6) |"
        .format(b["weir_coef"]),
        "| Breach trigger | water surface {:.2f} {} | {} |".format(
            b["trigger_wse"], L, "the run's trigger" if info["scenario"] == "flood" else
            "sunny day: the breach starts with the run"),
        "| Downstream boundary | normal depth, slope {:.5f} | {} |".format(
            info["bc_slope"], info["bc_slope_how"]),
        "| Time step | {} | Courant number (V dt / dx) about {} at the run's peak velocity |"
        .format(info["comp_interval"], 1 if m.equation == EQ_SWE else 2),
        "| Mesh check (cells along the perimeter) | {} |  Voronoi cells clipped to the "
        "perimeter, as HEC-RAS builds them |".format(
            "not run (no scipy)" if not info.get("mesh_check") else
            "{cells_checked} checked, most faces {max_faces}, over 8 faces {over_8_faces}, "
            "smallest {smallest_fraction:g} of a full cell".format(**info["mesh_check"])),
        "| Simulation | {:g} h from {} 00:00 | 1.25 x the longer of the hydrograph and the "
        "latest arrival |".format(info["sim_hours"], START_DATE), "",
        "## Not carried over from the run", "",
        "- DEM edits made for the run (channels, ponds, notches, buildings, a proposed "
        "embankment) are not in the terrain: add them in RAS Mapper as terrain "
        "modifications if they matter.",
        "- Spillways and outlets: the dam is a weir at the top of dam. For a flood scenario "
        "add the spillway and outlets to the connection.",
        "- HEC-RAS routes the breach itself (weir flow through the opening); its peak will "
        "differ from the plugin's {:,.0f} cfs.".format(float(params.get("peak_cfs") or 0)),
        ""]
    # v3.12.0: culverts, bridges and notches (nothing is added when the run had none)
    k = lines.index("## Not carried over from the run")
    lines[k:k] = structures_readme(m, info.get("structures"))
    if notes:
        lines += ["## Notes", ""] + ["- " + n for n in notes] + [""]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return path


def structures_readme(m, sinfo):
    """v3.12.0: the README section on the run's culverts, bridges and notches: what became of
    each one and what to check in HEC-RAS. [] when the run had none."""
    if not sinfo or not (sinfo.get("exported") or sinfo.get("skipped")):
        return []
    u = m.units
    L = u.len_name
    out = [
        "## Culverts, bridges and notches", "",
        "From {}. Each one that lies in the 2D area is an SA/2D area connection *inside* the "
        "2D area (upstream and downstream both `{}`), the way HEC-RAS models a road embankment "
        "with culverts in a 2D area. Its connection line crosses the embankment where the "
        "structure line is highest, perpendicular to that line, and runs along the road each "
        "way until the ground is {:g} ft above the road at the structure (at most {:,.0f} ft "
        "each side, and one cell inside the 2D area). The weir is the terrain along that line "
        "(the road crest), with the weir equation (coefficient {:g}) for flow over the road."
        .format(sinfo["source"], AREA_NAME, STRUCT_RISE_FT, STRUCT_MAX_HALF_FT,
                u.weir_coef(STRUCT_WEIR_COEF)), "",
        "**These connections follow the keyword lines of HEC's own example geometries but have "
        "NOT been opened in HEC-RAS. Check every one (below) before you run the plan.**", ""]
    ex = sinfo.get("exported") or []
    if ex:
        out += ["| Connection | Structure | In HEC-RAS | Opening | Inverts US / DS ({0}) | "
                "Length ({0}) | Weir |".format(L), "|---|---|---|---|---|---|---|"]
        for c in ex:
            weir = "{} points along {:,.0f} {}, road top {:,.0f} {} wide".format(
                len(c["weir"]), float(np.hypot(*np.subtract(c["line"][1], c["line"][0]))), L,
                c["weir_width"], L)
            cv, gp = c["culvert"], c["gap"]
            if cv:
                pipe = cv["shape"] == CULVERT_TYPES["pipe"][0]
                how = ("culvert group: {} (shape {}), FHWA chart {} scale {}, n {:g}, entrance "
                       "loss {:g}, exit loss {:g}".format(
                           "concrete pipe" if pipe else "box", cv["shape"], cv["chart"],
                           cv["scale"], cv["n"], cv["entrance"], cv["exit"]))
                size = ("{} x {} {} diameter".format(cv["barrels"], num(cv["rise"], 2), L) if pipe
                        else "{} x {} x {} {} (span x rise)".format(
                            cv["barrels"], num(cv["span"], 2), num(cv["rise"], 2), L))
                inv = "{:.2f} / {:.2f} ({})".format(cv["inv_up"], cv["inv_dn"], cv["inverts_from"])
                length = "{:,.0f}".format(cv["length"])
            else:
                how = "weir with an open gap (no deck, piers or pressure flow)"
                size = "gap {} {} wide".format(num(gp["width"], 1), L)
                inv = "gap floor {:.2f} ({})".format(gp["floor"], gp["inverts_from"])
                length = ""
            out.append("| {} | {} `{}` | {} | {} | {} | {} | {} |".format(
                c["name"], c["kind"], c["source_name"], how, size, inv, length, weir))
        out.append("")
        for c in ex:
            out += ["- {}: {}.".format(c["name"], n) for n in c["notes"]]
        froms = " ".join((c.get("culvert") or {}).get("inverts_from") or "" for c in ex)
        crossing = any((c.get("culvert") or {}).get("shift") is not None for c in ex)
        out += ["- Inverts: 'run' is the structure's invert as the run used it, 'terrain' the "
                "ground at that end of its line (a culvert) or the lowest ground near that end "
                "(a gap){}.".format(
                    ", '{}' the channel bed where the channel leaves the road embankment on "
                    "that side, found in the terrain for a culvert the run cut as a gap from "
                    "the crossings (not an inventory elevation)".format(BED_LABEL)
                    if BED_LABEL in froms else ""),
                "- The barrels sit side by side about the crossing of the two "
                "lines, {:g} x the span apart, on HEC-RAS's default barrel centrelines (across "
                "the connection line){}. The Runner's own lines are in the layout GeoPackage "
                "(layer 'Structures from the run').".format(
                    1.1, "; a culvert the run cut as a gap from the crossings runs from the "
                    "channel on one side of the road to the other, its connection line along "
                    "the road over a carriageway's crest, and where the channel crosses the "
                    "road at an angle its upstream and downstream centreline stations differ "
                    "(check the barrel in RAS Mapper)" if crossing else ""), ""]
    sk = sinfo.get("skipped") or []
    if sk:
        out += ["Not exported (add them in HEC-RAS if they matter):", ""]
        out += ["- {} ({}): {}.".format(x["name"], x["kind"], x["why"]) for x in sk] + [""]
    if ex:
        out += [
            "What to check in HEC-RAS:", "",
            "1. Geometric Data: open each connection above in the SA/2D Area Connection editor. "
            "Check the connection line (it should cross the road at the structure), the weir "
            "station-elevation (the road crest, with no cut at a culvert: see the notes), the "
            "weir width and coefficient, and the culvert data: shape, span, rise, length, "
            "Manning's n (top and bottom), entrance and exit loss, FHWA chart and scale (set "
            "for a concrete pipe or a box with flared wingwalls; change them for corrugated "
            "metal or another inlet), upstream and downstream inverts, number of barrels and "
            "their centreline stations. Make sure the culvert group is listed for the "
            "connection. Replace an open gap with a bridge or culverts if you have the data.",
            "2. RAS Mapper: the 2D cells need faces along every connection line. The "
            "computation points here were laid out without the structures: put breaklines "
            "on the connection lines (or regenerate the points) so the mesh follows them.",
            "3. Compute the property tables (Geometry > Compute Property Tables, or the plan's "
            "geometry preprocessor) and read the messages for each connection.",
            "4. After the run, compare the water upstream of each road with the run's maximum "
            "depth there.", ""]
    return out
