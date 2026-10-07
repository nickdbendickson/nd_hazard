"""Outlet works and gate operations for the flood scenario.

The spillway rating (`flood.Spillway`) is a fixed elevation-discharge curve. Real
dams also have low-level outlets (a drawdown pipe, a gated conduit) and gates
that are open, closed, or stuck. This module adds:

* `Conduit`: a low-level outlet with an invert, a size, a discharge coefficient
  and a gate schedule (fraction open against time). It flows as a weir while the
  water is below the crown and as an orifice above it.
* `schedule_factor(schedule, t_s)`: the same kind of schedule multiplies the
  spillway rating (a gated spillway that opens at some time, or a clogged one).

Text format used by the Processing tools and the command line, one outlet per
line or separated by `|`:

    name; invert_ft; diameter_ft; cd; gate
    drawdown pipe; 1832.5; 1.5; 0.6; 0:1, 6:0

The gate is `hours:fraction` pairs (linear steps hold the last value: 0:1, 6:0
means fully open until hour 6, closed after); omit it for always open. A
rectangular opening is `name; invert_ft; width_ft x height_ft; cd; gate`.

Flow is a weir Q = 3.0 * width * h^1.5 (US units, h above the invert) while the water
is below the crown, an orifice Q = cd * area * sqrt(2 g (H - centre)) above it, and
the smaller of the two near the crown so the rating has no jump. These are textbook
approximations without conduit friction or tailwater: replace them with a rating
curve (the spillway CSV) when the dam has a better one.
"""
import math
from dataclasses import dataclass, field
from typing import List, Optional

import numpy as np

from .breach import BreachInputError
from .units import FT, G, CFS

WEIR_C_US = 3.0


class OutletError(BreachInputError):
    pass


def schedule_factor(schedule, t_s):
    """Gate fraction (0-1) at t_s for [(t_s, fraction)]; holds the first value before the first
    point and the last after the last. None means always fully open."""
    if not schedule:
        return 1.0
    t = [p[0] for p in schedule]
    f = [p[1] for p in schedule]
    if t_s <= t[0]:
        return float(f[0])
    for i in range(1, len(t)):
        if t_s < t[i]:
            return float(f[i - 1])           # steps: the value holds until the next point
    return float(f[-1])


def parse_schedule(text):
    """'0:1, 6:0' (hours:fraction) -> [(seconds, fraction)]"""
    if text is None or not str(text).strip():
        return None
    out = []
    for part in str(text).replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            h, f = part.split(":")
            out.append((float(h) * 3600.0, min(1.0, max(0.0, float(f)))))
        except ValueError:
            raise OutletError("gate schedule '{}': use hours:fraction pairs like 0:1, 6:0"
                              .format(part))
    out.sort()
    return out or None


@dataclass
class Conduit:
    name: str
    invert_m: float
    width_m: float               # diameter for a circular outlet
    height_m: float
    circular: bool = True
    cd: float = 0.6
    schedule: Optional[list] = None      # [(t_s, fraction open)]

    @property
    def area_m2(self):
        return math.pi * self.width_m ** 2 / 4.0 if self.circular else self.width_m * self.height_m

    @property
    def crown_m(self):
        return self.invert_m + (self.width_m if self.circular else self.height_m)

    def q(self, wse_m, t_s=0.0):
        """Discharge (m3/s) with the pool at wse_m at time t_s."""
        gate = schedule_factor(self.schedule, t_s)
        if gate <= 0 or wse_m <= self.invert_m:
            return 0.0
        h = wse_m - self.invert_m
        open_h = (self.crown_m - self.invert_m) * gate      # a partly open gate shortens the crown
        open_h = max(open_h, 1e-6)
        crown = self.invert_m + open_h
        width = self.width_m
        weir = WEIR_C_US * FT ** 0.5 * width * h ** 1.5
        if wse_m <= crown:
            return float(weir)
        area = (math.pi * width ** 2 / 4.0 * min(1.0, gate) if self.circular
                else width * open_h)
        centre = self.invert_m + 0.5 * open_h
        orifice = self.cd * area * math.sqrt(2.0 * G * (wse_m - centre))
        return float(min(weir, orifice)) if (wse_m - crown) < 0.05 * open_h else float(orifice)


def q_total(conduits, wse_m, t_s=0.0):
    return sum(c.q(wse_m, t_s) for c in conduits or [])


def parse_outlets(text):
    """Outlets from the text format above ('' -> [])."""
    if text is None or not str(text).strip():
        return []
    out = []
    for raw in str(text).replace("|", "\n").splitlines():
        raw = raw.strip()
        if not raw or raw.startswith("#"):
            continue
        p = [x.strip() for x in raw.split(";")]
        if len(p) < 3:
            raise OutletError("outlet '{}': need name; invert_ft; size_ft[; cd[; gate]]".format(raw))
        name = p[0] or "outlet"
        try:
            invert = float(p[1]) * FT
            size = p[2].lower().replace("×", "x")
            if "x" in size:
                w, h = size.split("x")
                w, h, circ = float(w) * FT, float(h) * FT, False
            else:
                w = float(size) * FT
                h, circ = w, True
            cd = float(p[3]) if len(p) > 3 and p[3] else 0.6
        except ValueError:
            raise OutletError("outlet '{}': invert, size and coefficient must be numbers".format(raw))
        if w <= 0 or h <= 0 or not (0.1 <= cd <= 1.0):
            raise OutletError("outlet '{}': size must be > 0 and cd between 0.1 and 1".format(raw))
        gate = ";".join(p[4:]) if len(p) > 4 else None
        out.append(Conduit(name, invert, w, h, circ, cd, parse_schedule(gate)))
    return out


def describe(conduits):
    return ["{}: invert {:.2f} ft, {}, cd {:.2f}, gate {}".format(
        c.name, c.invert_m / FT,
        "{:.2f} ft diameter".format(c.width_m / FT) if c.circular else
        "{:.2f} x {:.2f} ft".format(c.width_m / FT, c.height_m / FT), c.cd,
        "always open" if not c.schedule else ", ".join(
            "{:g} h: {:g}".format(t / 3600.0, f) for t, f in c.schedule)) for c in conduits]
