"""Quick screen (triage): a cheap, deliberately generous flood envelope.

Purpose: decide which dams obviously cannot reach any structure, road or
railroad, so the full 2D model is only run where it can change the answer.

Method (numpy only)
  1. Flow path: steepest descent from the breach toe.  Where the path gets
     stuck in a pit (a pothole or a road fill), a local priority search finds
     the lowest spill point and the path continues through it.  Very large
     closed basins end the path ("terminal basin").
  2. Stage along the path: Manning normal depth for the UNATTENUATED breach
     peak in a cross-section cut perpendicular to the path, using a local
     bed slope (never flatter than `min_slope`).  A safety margin is added:
     30% of the flow depth, at least 0.15 m (a fixed large margin floods
     half the prairie around a stock pond and clears nothing).
  3. Reach: the path is followed until the valley below that stage would hold
     `volume_factor` x the released volume (default 2x), or the terrain ends.
  4. Envelope: cells that DRAIN to the path (D8 on the pit-filled surface),
     below the stage of the path cell they drain to and not lower than its bed
     (height above nearest drainage).  Cells whose water goes elsewhere are
     never included, so the envelope does not spread along contour lines.

Every simplification errs towards a LARGER envelope (no attenuation, peak
stage everywhere at once, margin, volume factor), because a dam is only
cleared ("no 2D needed") when nothing lies inside it.  It is still a screening
shortcut: the triage tool can send a random audit sample of cleared dams to
the full model to check it.
"""
import heapq
import math
import numpy as np

from .gridutil import NODATA, inside

N8 = [(-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1)]
N4 = [(-1, 0), (1, 0), (0, -1), (0, 1)]


def trace_path(dem, start_rc, max_steps=200000, max_pit_cells=40000):
    """Steepest-descent path with pit escape.  Returns (cells, end_reason)."""
    shape = dem.shape
    valid = dem > NODATA + 1
    r, c = start_rc
    if not inside(shape, r, c) or not valid[r, c]:
        return [], "start outside terrain"
    path = [(r, c)]
    seen = {(r, c)}
    for _ in range(max_steps):
        z = dem[r, c]
        best, bz = None, z
        for dr, dc in N8:
            rr, cc = r + dr, c + dc
            if not inside(shape, rr, cc) or not valid[rr, cc]:
                return path, "edge"
            dz = (dem[rr, cc] - z) / (1.4142 if dr and dc else 1.0)
            if dem[rr, cc] < bz and (best is None or dz < best[2]):
                best = (rr, cc, dz)
        if best is not None:
            nxt = [(best[0], best[1])]
        else:                                    # pit or flat: priority search for a spill
            nxt = _escape(dem, valid, r, c, seen, max_pit_cells)
            if nxt is None:
                return path, "terminal basin"
            if nxt == "edge":
                return path, "edge"
        added = False
        for rc in nxt:
            if rc in seen:
                continue
            seen.add(rc); path.append(rc); added = True
        if not added:
            return path, "stuck"
        r, c = path[-1]
    return path, "max steps"


def _escape(dem, valid, r0, c0, seen, max_cells):
    z0 = dem[r0, c0]
    heap = [(z0, r0, c0)]
    parent = {(r0, c0): None}
    while heap:
        z, r, c = heapq.heappop(heap)
        if len(parent) > max_cells:
            return None
        for dr, dc in N4:
            rr, cc = r + dr, c + dc
            if (rr, cc) in parent:
                continue
            if not inside(dem.shape, rr, cc) or not valid[rr, cc]:
                return "edge"
            parent[(rr, cc)] = (r, c)
            if dem[rr, cc] < z0 - 1e-6:          # lower than the pit bottom: a true outlet
                chain, k = [], (rr, cc)
                while k is not None and k != (r0, c0):
                    chain.append(k); k = parent[k]
                return chain[::-1]
            heapq.heappush(heap, (dem[rr, cc], rr, cc))
    return None


def normal_depth_stage(xs_z, q, slope, n, cell, dmax=40.0):
    """Water-surface elevation carrying q in a cross-section (strip method).

    xs_z: elevations across the valley, centre index = channel point.
    Only strips connected to the centre (not past a higher bank) convey flow.
    Returns (wse, wet_width_m, area_m2).
    """
    xs = np.asarray(xs_z, float)
    ci = len(xs) // 2
    zmin = float(xs[ci])
    s = math.sqrt(max(slope, 1e-6))

    def conv(wse):
        lo = ci
        while lo > 0 and xs[lo - 1] < wse and xs[lo - 1] > NODATA + 1:
            lo -= 1
        hi = ci
        while hi < len(xs) - 1 and xs[hi + 1] < wse and xs[hi + 1] > NODATA + 1:
            hi += 1
        d = np.clip(wse - xs[lo:hi + 1], 0, None)
        return s / n * float(np.sum(d ** (5.0 / 3.0))) * cell, (hi - lo + 1) * cell, \
            float(d.sum()) * cell, lo == 0 or hi == len(xs) - 1

    a, b = zmin, zmin + dmax
    if conv(b)[0] < q:
        qq, w, area, spill = conv(b)
        return b, w, area
    for _ in range(50):
        m = 0.5 * (a + b)
        if conv(m)[0] < q:
            a = m
        else:
            b = m
    _, w, area, _ = conv(b)
    return b, w, area


def envelope(dem, gt, start_rc, q_peak, volume_m3, n=0.05, min_slope=5e-4,
             margin_m=0.15, margin_frac=0.3, volume_factor=2.0, half_width_m=1500.0, slope_len_m=150.0,
             min_reach_m=500.0, max_lateral_m=None, hand_tol_m=0.5):
    """Triage envelope.  Returns dict(mask, depth, path, stage, reach_m, end, edge)."""
    cell = abs(gt[1])
    path, end = trace_path(dem, start_rc)
    res = {"mask": np.zeros(dem.shape, bool), "depth": np.zeros(dem.shape),
           "stage_grid": np.full(dem.shape, np.nan), "untraced": False,
           "path": path, "stage": [], "reach_m": 0.0, "end": end, "edge": end == "edge"}
    if len(path) < 2:
        # v0.11.1: no flow path (the breach toe is on the terrain edge, in a pit, outside the
        # terrain, or on flat ground). The envelope is empty, which must NOT read as "nothing
        # inside the envelope": the caller sees untraced=True and keeps the dam for 2D.
        res["untraced"] = True
        return res
    pr = np.array([p[0] for p in path]); pc = np.array([p[1] for p in path])
    steps = np.hypot(np.diff(pr), np.diff(pc)) * cell
    dist = np.concatenate(([0.0], np.cumsum(steps)))
    zp = dem[pr, pc]
    k = max(int(slope_len_m / cell), 2)
    hw = int(half_width_m / cell)
    stage = np.full(len(path), np.nan)
    stored = 0.0
    last = len(path) - 1
    for i in range(len(path)):
        i0, i1 = max(i - k, 0), min(i + k, len(path) - 1)
        ddist = max(dist[i1] - dist[i0], cell)
        slope = max((zp[i0] - zp[i1]) / ddist, min_slope)
        dy, dx = pr[i1] - pr[i0], pc[i1] - pc[i0]
        L = math.hypot(dx, dy) or 1.0
        py, px = dx / L, -dy / L                      # perpendicular (row, col)
        offs = np.arange(-hw, hw + 1)
        rr = np.clip(np.round(pr[i] + py * offs).astype(int), 0, dem.shape[0] - 1)
        cc = np.clip(np.round(pc[i] + px * offs).astype(int), 0, dem.shape[1] - 1)
        xs = dem[rr, cc].copy()
        xs[hw] = zp[i]
        wse, width, area = normal_depth_stage(xs, q_peak, slope, n, cell)
        depth_i = wse - float(xs[hw])
        stage[i] = wse + max(margin_m, margin_frac * depth_i)
        seg = steps[i - 1] if i > 0 else 0.0
        stored += area * seg
        if dist[i] >= min_reach_m and stored >= volume_factor * volume_m3:
            last = i
            break
    res["reach_m"] = float(dist[last])
    res["stage"] = stage[:last + 1].tolist()
    if last == len(path) - 1 and end == "edge":
        res["edge"] = True
    elif last < len(path) - 1:
        res["end"] = "volume"
        res["edge"] = False

    # ---- lateral extent by drainage (height above nearest drainage, HAND):
    # each cell is compared with the stage of the path cell its own flow reaches
    lab = drainage_labels(dem, path[:last + 1])
    lim_c = int((max_lateral_m or half_width_m) / cell)
    valid = dem > NODATA + 1
    li = np.where(lab >= 0, lab, 0)
    st_c = np.where(lab >= 0, stage[li], np.nan)
    zp_c = zp[li]
    pr_c, pc_c = pr[li], pc[li]
    rr, cc = np.mgrid[0:dem.shape[0], 0:dem.shape[1]]
    near = (np.abs(rr - pr_c) <= lim_c) & (np.abs(cc - pc_c) <= lim_c)
    wet = (lab >= 0) & valid & near & (dem < st_c) & (dem >= zp_c - hand_tol_m)
    for i in range(last + 1):                       # path cells themselves
        wet[pr[i], pc[i]] = True
    if wet[0, :].any() or wet[-1, :].any() or wet[:, 0].any() or wet[:, -1].any():
        res["edge"] = True
    ring = np.zeros(dem.shape, bool)                # next to missing terrain
    ring[1:, :] |= ~valid[:-1, :]; ring[:-1, :] |= ~valid[1:, :]
    ring[:, 1:] |= ~valid[:, :-1]; ring[:, :-1] |= ~valid[:, 1:]
    if (wet & ring).any():
        res["edge"] = True
    res["mask"] = wet
    res["stage_grid"] = np.where(wet, st_c, np.nan)
    res["depth"] = np.where(wet, np.nan_to_num(st_c) - dem, 0.0).clip(min=0)
    return res


def fill_eps(dem, eps=1e-3):
    """Priority-flood pit filling with a tiny gradient so every cell drains out."""
    valid = dem > NODATA + 1
    z = np.where(valid, dem, np.inf).astype(float)
    done = ~valid.copy()
    heap = []
    ny, nx = dem.shape
    edge = np.zeros(dem.shape, bool)
    edge[0, :] = edge[-1, :] = edge[:, 0] = edge[:, -1] = True
    nb = np.zeros(dem.shape, bool)
    nb[1:, :] |= ~valid[:-1, :]; nb[:-1, :] |= ~valid[1:, :]
    nb[:, 1:] |= ~valid[:, :-1]; nb[:, :-1] |= ~valid[:, 1:]
    for r, c in zip(*np.where(valid & (edge | nb))):
        heapq.heappush(heap, (z[r, c], r, c)); done[r, c] = True
    while heap:
        zz, r, c = heapq.heappop(heap)
        for dr, dc in N8:
            rr, cc = r + dr, c + dc
            if 0 <= rr < ny and 0 <= cc < nx and not done[rr, cc]:
                done[rr, cc] = True
                if z[rr, cc] <= zz:
                    z[rr, cc] = zz + eps
                heapq.heappush(heap, (z[rr, cc], rr, cc))
    return np.where(valid, z, NODATA)


def drainage_labels(dem, path):
    """Index of the path cell each grid cell drains to (D8 on the pit-filled
    surface), -1 if it drains elsewhere."""
    zf = fill_eps(dem)
    ny, nx = dem.shape
    big = np.where(zf > NODATA + 1, zf, np.inf)
    pad = np.pad(big, 1, constant_values=np.inf)
    best = np.full(dem.shape, 0.0)
    recv = np.arange(ny * nx).reshape(ny, nx)
    idx = recv.copy()
    for dr, dc in N8:
        nbz = pad[1 + dr:1 + dr + ny, 1 + dc:1 + dc + nx]
        drop = (big - nbz) / (1.4142 if dr and dc else 1.0)
        take = drop > best
        best = np.where(take, drop, best)
        rr = np.clip(np.arange(ny)[:, None] + dr, 0, ny - 1)
        cc = np.clip(np.arange(nx)[None, :] + dc, 0, nx - 1)
        recv = np.where(take, rr * nx + cc, recv)
    recv = recv.ravel()
    lab = np.full(ny * nx, -1)
    for i, (r, c) in enumerate(path):
        if lab[r * nx + c] < 0:
            lab[r * nx + c] = i
    # pointer jumping: follow receivers until a labelled cell (or a sink) is met
    for _ in range(int(np.ceil(np.log2(ny * nx))) + 2):
        need = lab < 0
        if not need.any():
            break
        lab = np.where(need, lab[recv], lab)
        recv = recv[recv]
    del idx
    return lab.reshape(ny, nx)


def coarsen(dem, factor):
    """Block mean; a block with any no-data cell becomes no-data."""
    if factor <= 1:
        return dem
    ny, nx = (dem.shape[0] // factor) * factor, (dem.shape[1] // factor) * factor
    b = dem[:ny, :nx].reshape(ny // factor, factor, nx // factor, factor)
    bad = (b <= NODATA + 1).any(axis=(1, 3))
    m = b.mean(axis=(1, 3))
    return np.where(bad, NODATA, m)


def screen(dem, gt, start_rc, q_peak, volume_m3, target_cell=10.0, **kw):
    """Envelope computed on a coarsened grid (default ~10 m), returned at full resolution.

    Screening does not need LiDAR resolution, and the coarse grid makes it
    ~100x faster.  Stage is carried back to the fine grid, so fine-grid depth
    = coarse stage - fine ground.
    """
    cell = abs(gt[1])
    k = max(int(round(target_cell / cell)), 1)
    dc = coarsen(dem, k)
    gtc = (gt[0], cell * k, 0.0, gt[3], 0.0, -cell * k)
    res = envelope(dc, gtc, (start_rc[0] // k, start_rc[1] // k), q_peak, volume_m3, **kw)
    st = np.repeat(np.repeat(res["stage_grid"], k, 0), k, 1)
    full = np.full(dem.shape, np.nan)
    full[:st.shape[0], :st.shape[1]] = st
    wet = np.isfinite(full) & (dem < full) & (dem > NODATA + 1)
    res["mask"] = wet
    res["depth"] = np.where(wet, full - dem, 0.0)
    res["path"] = [(r * k + k // 2, c * k + k // 2) for r, c in res["path"]]
    res["factor"] = k
    return res
