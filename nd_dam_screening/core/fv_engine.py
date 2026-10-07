"""Built-in 2D shallow-water engine (numpy only; no external solver needed).

Same model class the DSS-WISE Lite Technical Report describes: an explicit,
first-order finite-volume solution of the conservative 2D shallow-water
equations on a regular square grid, with depth and unit discharges (h, qx, qy)
as unknowns, a shock-capturing upwind (HLL) flux, a CFL-limited time step and
wet-cell tracking.  Wet/dry fronts and steep beds use the hydrostatic
reconstruction of Audusse et al. (2004), which keeps a lake at rest exactly at
rest and depths non-negative.  Friction: Manning, semi-implicit.

Two ways to release water:
  * point sources (inject a breach hydrograph, like the LISFLOOD-FP set-up), or
  * reservoir in the grid: the reservoir starts full, and the cells of the
    breach are lowered from the crest to the breach invert over the formation
    time (as DSS-WISE does), so the outflow comes from the 2D reservoir itself,
    including tailwater effects.

Outputs: maximum depth, maximum velocity, exact maximum depth x velocity (|q|),
arrival time (first depth > threshold), final depth, and a mass balance.
Boundaries: no-data cells are walls; the outer grid edge lets water leave
(the runner flags any result where flood water reaches it).

ACTIVE WINDOW (v0.11): every step the fluxes are computed only inside the
bounding box of the wet cells (depth > H_DRY), source cells and breach cells,
grown by a halo of `halo` cells (default 2).  The ring of cells around the
window is read from the real grid (the grid's own edge is copied outward, the
open boundary), so the arithmetic inside the window is exactly the full-grid
arithmetic: a cell outside the window has only dry neighbours and cannot
change.  The window is rebuilt as soon as a wet cell reaches its outer ring
(the next step could then wet a cell outside it), and every `window_every`
steps so that it also shrinks.  `window=False` runs the whole grid every step;
tests/test_fv_engine.py checks that both give identical results.
Local time stepping (sub-cycling wet cells) is NOT done: it changes the
arithmetic and so the results.
"""
import time

import numpy as np

G = 9.81
H_DRY = 1e-4          # below this depth a cell carries no momentum
NODATA = -9999.0


def _safe_div(num, den, mask):
    """num / den where mask, else 0 - the same values as np.where(mask, num / np.where(mask, den,
    1.0), 0.0) without the two temporaries."""
    out = np.zeros(np.broadcast(num, den).shape)
    np.divide(num, den, out=out, where=mask)
    return out


def _hll(hL, huL, hvL, hR, huR, hvR):
    """HLL flux normal to a face.  hu = normal momentum, hv = tangential."""
    wetL, wetR = hL > H_DRY, hR > H_DRY
    uL, uR = _safe_div(huL, hL, wetL), _safe_div(huR, hR, wetR)
    vL, vR = _safe_div(hvL, hL, wetL), _safe_div(hvR, hR, wetR)
    cL, cR = np.sqrt(G * hL), np.sqrt(G * hR)
    sL = np.minimum(uL - cL, uR - cR)
    sR = np.maximum(uL + cL, uR + cR)
    # dry-bed wave speeds (Toro 2001): a front onto dry ground moves at u + 2c
    sL = np.where(~wetL, uR - 2.0 * cR, sL)
    sR = np.where(~wetR, uL + 2.0 * cL, sR)
    FL0, FR0 = hL * uL, hR * uR
    FL1, FR1 = FL0 * uL + 0.5 * G * hL * hL, FR0 * uR + 0.5 * G * hR * hR
    FL2, FR2 = FL0 * vL, FR0 * vR
    den = sR - sL
    den = np.where(np.abs(den) < 1e-12, 1e-12, den)
    pos, neg = sL >= 0, sR <= 0
    sLsR = sL * sR
    both_dry = ~(wetL | wetR)
    any_pos, any_neg, any_dry = pos.any(), neg.any(), both_dry.any()

    def mix(FLk, FRk, UL, UR):
        # (sR*FLk - sL*FRk + sLsR*(UR - UL)) / den, evaluated in place in the same order
        # (bit-for-bit the v0.11.0 result), then FRk where neg, FLk where pos (pos wins),
        # 0 where both sides are dry - the same choice as the nested np.where it replaces.
        mid = sR * FLk
        mid -= sL * FRk
        d = UR - UL
        d *= sLsR
        mid += d
        mid /= den
        if any_neg:
            np.copyto(mid, FRk, where=neg)
        if any_pos:
            np.copyto(mid, FLk, where=pos)
        if any_dry:
            mid[both_dry] = 0.0
        return mid

    f0 = mix(FL0, FR0, hL, hR)
    f1 = mix(FL1, FR1, FL0, FR0)
    f2 = mix(FL2, FR2, hL * vL, hR * vR)
    return f0, f1, f2


def _faces(z, h, qn, qt, wall, axis, has_wall=True):
    """Fluxes on all faces along `axis` (0 = rows, 1 = cols) of a padded window.

    Returns flux arrays F0, F1, F2 on faces between consecutive cells, plus the
    hydrostatic-reconstruction corrections for the left and right cell.
    """
    if axis == 0:
        lo, hi = (slice(None, -1), slice(None)), (slice(1, None), slice(None))
    else:
        lo, hi = (slice(None), slice(None, -1)), (slice(None), slice(1, None))
    zL, zR = z[lo], z[hi]
    hL, hR = h[lo], h[hi]
    nL, nR = qn[lo], qn[hi]
    tL, tR = qt[lo], qt[hi]
    wL, wR = wall[lo], wall[hi]
    # walls: mirror the wet side (normal momentum reversed). Skipped when the window has no
    # wall cell (np.where would return the inputs unchanged).
    if has_wall:
        zL = np.where(wL, zR, zL); hL = np.where(wL, hR, hL)
        nL = np.where(wL, -nR, nL); tL = np.where(wL, tR, tL)
        zR = np.where(wR, zL, zR); hR = np.where(wR, hL, hR)
        nR = np.where(wR, -nL, nR); tR = np.where(wR, tL, tR)
    zf = np.maximum(zL, zR)
    hLs = np.maximum(hL + zL - zf, 0.0)
    hRs = np.maximum(hR + zR - zf, 0.0)
    wetL, wetR = hL > H_DRY, hR > H_DRY
    rL, rR = _safe_div(hLs, hL, wetL), _safe_div(hRs, hR, wetR)
    f0, f1, f2 = _hll(hLs, nL * rL, tL * rL, hRs, nR * rR, tR * rR)
    cL = 0.5 * G * (hL * hL - hLs * hLs)
    cR = 0.5 * G * (hR * hR - hRs * hRs)
    return f0, f1, f2, cL, cR


def _padded(a, r0, r1, c0, c1):
    """Window [r0:r1, c0:c1] of `a` with a one-cell ring of its real neighbours;
    at the grid edge the edge cell is copied outward (transmissive boundary)."""
    ny, nx = a.shape
    p = np.empty((r1 - r0 + 2, c1 - c0 + 2), a.dtype)
    R0, R1, C0, C1 = max(r0 - 1, 0), min(r1 + 1, ny), max(c0 - 1, 0), min(c1 + 1, nx)
    p[R0 - r0 + 1:R1 - r0 + 1, C0 - c0 + 1:C1 - c0 + 1] = a[R0:R1, C0:C1]
    if r0 == 0: p[0, :] = p[1, :]
    if r1 == ny: p[-1, :] = p[-2, :]
    if c0 == 0: p[:, 0] = p[:, 1]
    if c1 == nx: p[:, -1] = p[:, -2]
    return p


class Engine:
    def __init__(self, dem, cell, manning=0.05, wall=None, arrive_thresh=0.03,
                 window=True, halo=2, window_every=200):
        self.z0 = np.asarray(dem, float)
        if self.z0.ndim != 2 or min(self.z0.shape) < 1:
            raise ValueError("the DEM must be a non-empty 2D grid (got shape {})".format(self.z0.shape))
        self.cell = float(cell)
        if not (self.cell > 0 and np.isfinite(self.cell)):
            raise ValueError("cell size must be a positive number (got {})".format(cell))
        # no-data: -9999 cells AND NaN / inf cells are walls (v0.11.1: a NaN DEM cell used to
        # spread NaN through the whole run)
        nod = ~np.isfinite(self.z0) | (self.z0 <= NODATA + 1)
        self.wall = nod if wall is None else (np.asarray(wall, bool) | nod)
        if self.wall.all():
            raise ValueError("the DEM has no valid cell")
        self.z = np.where(self.wall, 0.0, self.z0)
        n = np.broadcast_to(np.asarray(manning, float), self.z.shape)
        if not np.all(np.isfinite(n[~self.wall])) or np.any(n[~self.wall] < 0):
            raise ValueError("Manning's n must be finite and >= 0 on every valid cell")
        self.n2 = np.where(np.isfinite(n), n, 0.0) ** 2
        self.h = np.zeros(self.z.shape)
        self.qx = np.zeros(self.z.shape)      # along columns (east)
        self.qy = np.zeros(self.z.shape)      # along rows (south)
        self.thr = arrive_thresh
        self.sources = []                     # (rows, cols, t, q)
        self.breach = None
        self.t = 0.0
        self.steps = 0
        self.injected = 0.0
        self.lost = 0.0
        self.window = bool(window)            # False: whole grid every step (reference mode)
        self.halo = max(int(halo), 1)
        self.window_every = max(int(window_every), 1)
        self.window_rebuilds = 0
        self.window_cells = 0                 # sum over steps of window cells (work measure)

    # ---------------------------------------------------------------- set-up
    def add_source(self, cells_rc, t_s, q_m3s, start_s=0.0):
        rr = np.array([c[0] for c in cells_rc]); cc = np.array([c[1] for c in cells_rc])
        self.sources.append((rr, cc, np.asarray(t_s, float) + start_s, np.asarray(q_m3s, float)))

    def set_reservoir(self, mask, wse, bed=None):
        """Fill `mask` cells to water surface `wse`; `bed` (optional) replaces the ground there."""
        if bed is not None:
            self.z = np.where(mask & ~self.wall, bed, self.z)
            self.z0 = np.where(mask & ~self.wall, bed, self.z0)
        self.h = np.where(mask & ~self.wall, np.maximum(wse - self.z, 0.0), self.h)

    def set_breach(self, mask, invert, t_start, tf_s):
        """Lower ground in `mask` linearly from its own elevation to `invert` over tf_s."""
        self.breach = (mask & ~self.wall, float(invert), float(t_start), max(float(tf_s), 1.0),
                       self.z.copy())
        self._breach_done = False
        rows = np.flatnonzero(self.breach[0].any(axis=1)); cols = np.flatnonzero(self.breach[0].any(axis=0))
        self._breach_box = (slice(rows[0], rows[-1] + 1), slice(cols[0], cols[-1] + 1)) if len(rows) \
            else (slice(0, 0), slice(0, 0))

    # ------------------------------------------------------------------ run
    def _source_rate(self, t):
        out = []
        for rr, cc, ts, qs in self.sources:
            q = float(np.interp(t, ts, qs, left=0.0, right=0.0))
            out.append((rr, cc, q))
        return out

    def _update_breach(self):
        if self.breach is None or getattr(self, "_breach_done", False):
            return
        m, inv, t0, tf, zorig = self.breach
        f = min(max((self.t - t0) / tf, 0.0), 1.0)
        b = self._breach_box                  # only the breach's bounding box changes
        mb, zb = m[b], zorig[b]
        target = np.maximum(inv, zb - f * (zb - inv))
        self.z[b] = np.where(mb, np.minimum(zb, target), self.z[b])
        if f >= 1.0:                          # fully formed: z no longer changes
            self._breach_done = True

    def _window(self, act_static):
        """(r0, r1, c0, c1) of the active cells + halo; None when nothing is active."""
        act = self.h > H_DRY
        act |= act_static
        rows = np.flatnonzero(act.any(axis=1))
        if len(rows) == 0:
            return None
        ny, nx = self.h.shape
        if not self.window:
            return 0, ny, 0, nx
        cols = np.flatnonzero(act.any(axis=0))
        k = self.halo
        return (max(rows[0] - k, 0), min(rows[-1] + k + 1, ny),
                max(cols[0] - k, 0), min(cols[-1] + k + 1, nx))

    def run(self, t_end, cfl=0.45, max_wall_s=None, progress=None, cancel=None,
            snapshot_every_s=None):
        """Advance to t_end.  Can be called again with a later t_end to continue."""
        ny, nx = self.z.shape
        if not hasattr(self, "hmax"):            # first call: initialise maxima and mass
            self.hmax = self.h.copy()
            self.vmax = np.zeros(self.z.shape)
            self.dvmax = np.zeros(self.z.shape)
            self.arrive = np.where(self.h > self.thr, 0.0, NODATA)
            self.initial_wet = self.h > self.thr
            self.vol0 = float(self.h.sum()) * self.cell ** 2
            self.snapshots = []
            self._next_snap = snapshot_every_s
        vol0 = self.vol0
        start_clock = time.time()
        win = None
        next_win = 0
        dx = self.cell
        act_static = np.zeros(self.z.shape, bool)       # sources + breach: always active
        for rr, cc, _, _ in self.sources:
            act_static[rr, cc] = True
        if self.breach is not None:
            act_static |= self.breach[0]
        next_snap = self._next_snap
        while self.t < t_end - 1e-9:
            if cancel is not None and cancel():
                raise RuntimeError("cancelled")
            if max_wall_s and time.time() - start_clock > max_wall_s:
                raise TimeoutError(f"built-in engine exceeded {max_wall_s / 60:.0f} min")
            # ---- active window (wet cells + sources + breach, plus the halo)
            if win is None or self.steps >= next_win:
                win = self._window(act_static)
                if win is None:
                    self.t = t_end; break
                next_win = self.steps + self.window_every
                self.window_rebuilds += 1
            r0, r1, c0, c1 = win
            self._update_breach()
            hs = self.h[r0:r1, c0:c1]
            qxs = self.qx[r0:r1, c0:c1]; qys = self.qy[r0:r1, c0:c1]
            ws = self.wall[r0:r1, c0:c1]
            self.window_cells += hs.size
            wet = hs > H_DRY
            if wet.any():
                u, v = _safe_div(qxs, hs, wet), _safe_div(qys, hs, wet)
                c = np.sqrt(G * hs)
                lam = float(np.max(np.maximum(np.abs(u), np.abs(v)) + c))
                if not np.isfinite(lam):
                    raise FloatingPointError(
                        "built-in engine became unstable at t = {:.0f} s (step {}): a depth or "
                        "discharge is not finite. Check the DEM (spikes, no-data inside the valley), "
                        "the cell size and the breach inputs.".format(self.t, self.steps))
            else:
                lam = 0.0
            dt = cfl * dx / lam if lam > 1e-9 else 1.0
            dt = min(dt, 5.0, t_end - self.t)
            # sources: never step past a change in inflow by more than 1 s
            rates = self._source_rate(self.t + 0.5 * dt)

            # ---- window + its ring of real neighbours (grid edge = open outflow)
            zp, hp = _padded(self.z, r0, r1, c0, c1), _padded(self.h, r0, r1, c0, c1)
            qxp, qyp = _padded(self.qx, r0, r1, c0, c1), _padded(self.qy, r0, r1, c0, c1)
            wp = _padded(self.wall, r0, r1, c0, c1)
            has_wall = bool(wp.any())
            # east-west faces (axis=1): normal = qx, tangential = qy
            fx0, fx1, fx2, cxL, cxR = _faces(zp[1:-1, :], hp[1:-1, :], qxp[1:-1, :],
                                             qyp[1:-1, :], wp[1:-1, :], axis=1, has_wall=has_wall)
            # north-south faces (axis=0): normal = qy, tangential = qx
            fy0, fy1, fy2, cyL, cyR = _faces(zp[:, 1:-1], hp[:, 1:-1], qyp[:, 1:-1],
                                             qxp[:, 1:-1], wp[:, 1:-1], axis=0, has_wall=has_wall)
            k = dt / dx
            dh = -k * ((fx0[:, 1:] - fx0[:, :-1]) + (fy0[1:, :] - fy0[:-1, :]))
            dqx = -k * ((fx1[:, 1:] + cxL[:, 1:]) - (fx1[:, :-1] + cxR[:, :-1])
                        + (fy2[1:, :] - fy2[:-1, :]))
            dqy = -k * ((fy1[1:, :] + cyL[1:, :]) - (fy1[:-1, :] + cyR[:-1, :])
                        + (fx2[:, 1:] - fx2[:, :-1]))
            # outflow through the outer grid edge (mass that leaves the model)
            out = 0.0
            if c0 == 0: out -= fx0[:, 0].sum()
            if c1 == nx: out += fx0[:, -1].sum()
            if r0 == 0: out -= fy0[0, :].sum()
            if r1 == ny: out += fy0[-1, :].sum()
            self.lost += out * dt * dx
            if has_wall:
                hn = np.where(ws, 0.0, hs + dh)
                qxn = np.where(ws, 0.0, qxs + dqx)
                qyn = np.where(ws, 0.0, qys + dqy)
            else:
                hn, qxn, qyn = hs + dh, qxs + dqx, qys + dqy
            # sources
            for rr, cc, q in rates:
                if q <= 0:
                    continue
                inw = (rr >= r0) & (rr < r1) & (cc >= c0) & (cc < c1)
                if inw.any():
                    add = q * dt / (inw.sum() * dx * dx)
                    np.add.at(hn, (rr[inw] - r0, cc[inw] - c0), add)
                    self.injected += q * dt
            neg = hn < 0
            if neg.any():                        # tiny negatives from round-off
                self.lost += float(hn[neg].sum()) * dx * dx
                hn[neg] = 0.0
            dry = hn <= H_DRY
            qxn[dry] = 0.0; qyn[dry] = 0.0
            # semi-implicit Manning friction
            wetn = ~dry
            hs43 = np.ones(hn.shape)                 # h^(4/3) on wet cells (1 elsewhere, unused)
            np.power(hn, 4.0 / 3.0, out=hs43, where=wetn)
            speed = _safe_div(np.hypot(qxn, qyn), hn, wetn)
            fac = 1.0 + dt * G * self.n2[r0:r1, c0:c1] * speed / hs43
            qxn = np.where(wetn, qxn / fac, 0.0); qyn = np.where(wetn, qyn / fac, 0.0)
            self.h[r0:r1, c0:c1] = hn
            self.qx[r0:r1, c0:c1] = qxn
            self.qy[r0:r1, c0:c1] = qyn
            self.t += dt
            self.steps += 1
            # ---- running maxima
            wetw = hn > self.thr
            qn = np.hypot(qxn, qyn)
            spd = np.where(wetw, qn / np.where(wetw, hn, 1.0), 0.0)
            np.maximum(self.hmax[r0:r1, c0:c1], hn, out=self.hmax[r0:r1, c0:c1])
            np.maximum(self.vmax[r0:r1, c0:c1], spd, out=self.vmax[r0:r1, c0:c1])
            np.maximum(self.dvmax[r0:r1, c0:c1], np.where(wetw, qn, 0.0),
                       out=self.dvmax[r0:r1, c0:c1])
            aw = self.arrive[r0:r1, c0:c1]
            self.arrive[r0:r1, c0:c1] = np.where(wetw & (aw == NODATA), self.t / 3600.0, aw)
            # ---- the wet front reached the window's outer ring: rebuild before the next step
            if self.window and (
                    (r0 > 0 and (hn[0, :] > H_DRY).any()) or (r1 < ny and (hn[-1, :] > H_DRY).any())
                    or (c0 > 0 and (hn[:, 0] > H_DRY).any()) or (c1 < nx and (hn[:, -1] > H_DRY).any())):
                win = None
            if progress and self.steps % 200 == 0:
                progress(self.t / t_end)
            if next_snap is not None and self.t >= next_snap:
                self.snapshots.append((self.t, self.h.copy()))
                next_snap += snapshot_every_s
        self._next_snap = next_snap
        vol1 = float(self.h.sum()) * dx * dx
        self.mass = {"initial_m3": vol0, "injected_m3": self.injected, "final_m3": vol1,
                     "left_domain_m3": self.lost,
                     "error_pct": 100.0 * (vol0 + self.injected - vol1 - self.lost)
                     / max(vol0 + self.injected, 1e-9)}
        return self.results()

    def results(self):
        return {"depth": self.hmax, "vel": self.vmax, "dv": self.dvmax,
                "arrive_h": self.arrive, "final": self.h.copy(), "mass": self.mass,
                "initial_wet": self.initial_wet, "steps": self.steps, "t": self.t}
