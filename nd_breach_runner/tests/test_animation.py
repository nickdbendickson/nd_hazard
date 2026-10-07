"""Tests of core/animation.py (PNG, GIF/LZW, font, hillshade, frames) and the 'animation'
Processing tool."""
import datetime
import json
import os
import shutil
import struct
import subprocess
import tempfile
import zlib

import numpy as np

from nd_breach_runner.tests.registry import model_test, test


def _pil():
    try:
        from PIL import Image
        return Image
    except Exception:
        return None


# ---------------------------------------------------------------------------------------
# independent readers (struct + zlib, and our own LZW decoder)
# ---------------------------------------------------------------------------------------
def _read_png(path):
    """8-bit RGB PNG -> (h, w, 3) uint8, every chunk's CRC checked, all five filters."""
    b = open(path, "rb").read()
    assert b[:8] == b"\x89PNG\r\n\x1a\n"
    pos, idat, ihdr, kinds = 8, b"", None, []
    while pos < len(b):
        n, = struct.unpack(">I", b[pos:pos + 4])
        kind, data = b[pos + 4:pos + 8], b[pos + 8:pos + 8 + n]
        crc, = struct.unpack(">I", b[pos + 8 + n:pos + 12 + n])
        assert crc == zlib.crc32(kind + data) & 0xFFFFFFFF, kind
        kinds.append(kind)
        if kind == b"IHDR":
            ihdr = struct.unpack(">IIBBBBB", data)
        elif kind == b"IDAT":
            idat += data
        pos += 12 + n
    assert kinds[0] == b"IHDR" and kinds[-1] == b"IEND", kinds
    w, h, depth, ctype, comp, filt, inter = ihdr
    assert (depth, ctype, comp, filt, inter) == (8, 2, 0, 0, 0), ihdr
    raw = zlib.decompress(idat)
    stride, bpp = w * 3, 3
    assert len(raw) == h * (stride + 1)
    out = np.zeros((h, stride), np.uint8)
    prev = np.zeros(stride, np.int64)
    for r in range(h):
        ft = raw[r * (stride + 1)]
        line = np.frombuffer(raw, np.uint8, stride, r * (stride + 1) + 1).astype(np.int64)
        cur = line.copy()
        if ft == 1:
            for i in range(bpp, stride):
                cur[i] = (cur[i] + cur[i - bpp]) % 256
        elif ft == 2:
            cur = (line + prev) % 256
        elif ft == 3:
            for i in range(stride):
                a = cur[i - bpp] if i >= bpp else 0
                cur[i] = (cur[i] + (a + prev[i]) // 2) % 256
        elif ft == 4:
            for i in range(stride):
                a = cur[i - bpp] if i >= bpp else 0
                c = prev[i - bpp] if i >= bpp else 0
                bb = prev[i]
                p = a + bb - c
                pa, pb, pc = abs(p - a), abs(p - bb), abs(p - c)
                pred = a if pa <= pb and pa <= pc else (bb if pb <= pc else c)
                cur[i] = (cur[i] + pred) % 256
        else:
            assert ft == 0, ft
        out[r] = cur
        prev = cur
    return out.reshape(h, w, 3)


def _read_gif(path):
    """GIF89a with a global palette -> (palette, composited index frames, delays, loop)."""
    from nd_breach_runner.core import animation as an
    b = open(path, "rb").read()
    assert b[:6] in (b"GIF89a", b"GIF87a")
    w, h, packed, _bg, _asp = struct.unpack("<HHBBB", b[6:13])
    pos, pal = 13, None
    if packed & 0x80:
        n = 2 << (packed & 7)
        pal = np.frombuffer(b[pos:pos + 3 * n], np.uint8).reshape(n, 3)
        pos += 3 * n
    canvas = np.zeros((h, w), np.uint8)
    frames, delays, gce, loop = [], [], None, None

    def blocks():
        nonlocal pos
        out = []
        while True:
            n = b[pos]
            pos += 1
            if n == 0:
                return out
            out.append(b[pos:pos + n])
            pos += n
    while True:
        blk = b[pos]
        pos += 1
        if blk == 0x3B:
            break
        if blk == 0x21:
            label = b[pos]
            pos += 1
            subs = blocks()
            if label == 0xF9:
                s = subs[0]
                gce = {"disposal": (s[0] >> 2) & 7, "transparent": s[0] & 1,
                       "delay": s[1] | (s[2] << 8), "index": s[3]}
            elif label == 0xFF and subs[0][:11] == b"NETSCAPE2.0":
                loop = subs[1][1] | (subs[1][2] << 8)
            continue
        assert blk == 0x2C, blk
        left, top, fw, fh, fp = struct.unpack("<HHHHB", b[pos:pos + 9])
        pos += 9
        assert not fp & 0x40                          # not interlaced
        if fp & 0x80:                                 # a local palette (PIL writes one)
            pos += 3 * (2 << (fp & 7))
        mcs = b[pos]
        pos += 1
        px = np.frombuffer(an.lzw_decode(b"".join(blocks()), mcs), np.uint8)
        assert px.size == fw * fh, (px.size, fw, fh)
        sub = px.reshape(fh, fw)
        region = canvas[top:top + fh, left:left + fw]
        if gce and gce["transparent"]:
            m = sub != gce["index"]
            region[m] = sub[m]
        else:
            region[:] = sub
        assert gce is None or gce["disposal"] in (0, 1)
        frames.append(canvas.copy())
        delays.append(gce["delay"] if gce else 0)
        gce = None
    return pal, frames, delays, loop


def _lzw_codes(data, mcs):
    """[(code, width in bits)] of a GIF LZW stream, following the decoder's width rule."""
    clear, eoi = 1 << mcs, (1 << mcs) + 1
    size, nxt, prev, out = mcs + 1, eoi + 1, None, []
    bits = nbits = pos = 0
    while True:
        while nbits < size and pos < len(data):
            bits |= data[pos] << nbits
            nbits += 8
            pos += 1
        if nbits < size:
            return out
        code = bits & ((1 << size) - 1)
        bits >>= size
        nbits -= size
        out.append((code, size))
        if code == clear:
            size, nxt, prev = mcs + 1, eoi + 1, None
            continue
        if code == eoi:
            return out
        if prev is not None and nxt < 4096:
            nxt += 1
            if nxt == (1 << size) and size < 12:
                size += 1
        prev = code


def _inverse_palette(rgb):
    """RGB image -> palette indices (the palette's colours are all different)."""
    from nd_breach_runner.core import animation as an
    key = lambda a: (a[..., 0].astype(np.int64) << 16) | (a[..., 1].astype(np.int64) << 8) | a[..., 2]
    lut = {int(k): i for i, k in enumerate(key(an.PALETTE))}
    flat = key(np.asarray(rgb))
    out = np.vectorize(lambda v: lut.get(int(v), -1))(flat)
    assert (out >= 0).all(), "a colour that is not in the palette"
    return out.astype(np.uint8)


def _synthetic(d, n=7, ny=40, nx=60, dx=10.0, dry_first=True):
    """A valley running east with a flood spreading down it; (npz, dem Grid)."""
    from nd_breach_runner.core.grid import Grid
    x = (np.arange(nx) + 0.5) * dx
    y = (ny - np.arange(ny) - 0.5) * dx
    X, Y = np.meshgrid(x, y)
    z = 300.0 - 0.01 * X + 0.03 * np.abs(Y - 200.0) + 0.8 * np.sin(X / 60.0) * np.cos(Y / 45.0)
    dem = Grid(z, 1000.0, 5000.0 + ny * dx, dx, crs_wkt="EPSG:26914")
    depth = np.full((n, ny, nx), np.nan, np.float32)
    for i in range(n):
        if dry_first and i == 0:
            continue
        reach = 5 + (nx - 5) * i / (n - 1)
        h = (4.0 - 0.04 * np.abs(Y - 200.0)) * np.clip(1 - X / (reach * dx), 0, 1) * 1.1
        h[X > reach * dx] = 0
        h[h <= 0.003] = np.nan
        depth[i] = h.astype(np.float32)
    times = np.arange(n) * 600.0
    p = os.path.join(d, "depth_snapshots.npz")
    np.savez_compressed(p, times_s=times, depth=depth)
    return p, dem, times, depth


# ---------------------------------------------------------------------------------------
@test
def animation_png_writer_decodes():
    from nd_breach_runner.core import animation as an
    d = tempfile.mkdtemp()
    try:
        rng = np.random.default_rng(3)
        img = rng.integers(0, 256, (17, 23, 3)).astype(np.uint8)
        img[5:9] = np.arange(23 * 3, dtype=np.uint8).reshape(23, 3)     # smooth rows too
        p = an.write_png(os.path.join(d, "a.png"), img)
        assert np.array_equal(_read_png(p), img)
        one = an.write_png(os.path.join(d, "b.png"), img[:1, :1])
        assert np.array_equal(_read_png(one), img[:1, :1])
        Image = _pil()
        if Image is not None:
            assert np.array_equal(np.asarray(Image.open(p).convert("RGB")), img)
            # and our reader against PIL's own (adaptive filters: Sub, Average, Paeth)
            big = rng.integers(0, 256, (31, 29, 3)).astype(np.uint8)
            big[:, :10] = 7
            q = os.path.join(d, "pil.png")
            Image.fromarray(big).save(q, optimize=True)
            assert np.array_equal(_read_png(q), big)
    finally:
        shutil.rmtree(d)


@test
def animation_lzw_round_trips_with_code_size_growth_and_clear_codes():
    from nd_breach_runner.core import animation as an
    rng = np.random.default_rng(1)
    cases = [b"", b"\x05", b"\x01\x02", b"\x07" * 5, bytes(range(256)) * 3,
             bytes(20000),                                            # long runs: 12-bit codes
             rng.integers(0, 256, 60000).astype(np.uint8).tobytes(),  # table fills: clear codes
             np.repeat(rng.integers(0, 6, 30000), 3).astype(np.uint8).tobytes()]
    for data in cases:
        enc = an.lzw_encode(data, 8)
        assert an.lzw_decode(enc, 8) == data, len(data)
    # the code stream itself: widths grow 9 -> 12 bits, and random data refills the table,
    # so clear codes appear mid-stream (widths start over at 9)
    codes = _lzw_codes(an.lzw_encode(cases[6], 8), 8)
    assert codes[0] == (256, 9) and codes[-1][0] == 257
    clears = [i for i, (c, _w) in enumerate(codes) if c == 256]
    assert len(clears) >= 3, len(clears)
    assert {w for _c, w in codes[:clears[1]]} == {9, 10, 11, 12}
    assert codes[clears[1] + 1][1] == 9
    runs = _lzw_codes(an.lzw_encode(cases[5], 8), 8)          # 20000 zeros: few long codes
    assert len(runs) < 250 and max(w for _c, w in runs) == 9
    # smaller alphabets (min code size 2) round trip too
    small = rng.integers(0, 4, 5000).astype(np.uint8).tobytes()
    assert an.lzw_decode(an.lzw_encode(small, 2), 2) == small
    # a stream written by PIL decodes with our decoder
    Image = _pil()
    if Image is not None:
        d = tempfile.mkdtemp()
        try:
            a = rng.integers(0, 256, (90, 70)).astype(np.uint8)
            im = Image.fromarray(a, "P")
            im.putpalette(an.PALETTE.tobytes())
            p = os.path.join(d, "pil.gif")
            im.save(p, interlace=False)
            _pal, frames, _d, _l = _read_gif(p)
            assert np.array_equal(frames[0], a)
        finally:
            shutil.rmtree(d)


@test
def animation_gif_decodes_to_the_same_palette_indices():
    from nd_breach_runner.core import animation as an
    d = tempfile.mkdtemp()
    rng = np.random.default_rng(7)
    h, w = 64, 80
    frames = []
    base = rng.integers(16, 256, (h, w)).astype(np.uint8)
    base[base == an.C_TRANSPARENT] = 16
    frames.append(base)
    for k in range(1, 6):
        f = frames[-1].copy()
        f[10 + k:20 + 2 * k, 5 * k:5 * k + 12] = 176 + 16 * (k % 5)     # a moving patch
        f[0, 0] = k                                                    # a corner pixel
        frames.append(f)
    frames.append(frames[-1].copy())                                   # an unchanged frame
    frames.append(rng.integers(16, 256, (h, w)).astype(np.uint8))      # everything changes
    try:
        for optimize in (True, False):
            p = os.path.join(d, "o{}.gif".format(int(optimize)))
            an.write_gif(p, frames, delay_cs=12, optimize=optimize)
            pal, got, delays, loop = _read_gif(p)
            assert np.array_equal(pal, an.PALETTE) and loop == 0
            assert len(got) == len(frames) and delays == [12] * len(frames)
            for a, b in zip(got, frames):
                assert np.array_equal(a, b)
            Image = _pil()
            if Image is None:
                continue
            from PIL import GifImagePlugin
            im = Image.open(p)
            assert im.n_frames == len(frames)
            for i, f in enumerate(frames):
                im.seek(i)
                if im.mode == "P":
                    assert np.array_equal(np.asarray(im), f), i
                rgb = np.asarray(im.convert("RGB"))
                assert np.array_equal(rgb, an.PALETTE[f]), i
                assert np.array_equal(_inverse_palette(rgb), f), i
            im.close()
            # full frames with one palette stay palette images: exact indices from PIL
            strat = getattr(GifImagePlugin, "LoadingStrategy", None)
            if not optimize and strat is not None and hasattr(
                    strat, "RGB_AFTER_DIFFERENT_PALETTE_ONLY"):
                old = GifImagePlugin.LOADING_STRATEGY
                GifImagePlugin.LOADING_STRATEGY = strat.RGB_AFTER_DIFFERENT_PALETTE_ONLY
                try:
                    im = Image.open(p)
                    for i, f in enumerate(frames):
                        im.seek(i)
                        assert im.mode == "P" and np.array_equal(np.asarray(im), f), i
                    im.close()
                finally:
                    GifImagePlugin.LOADING_STRATEGY = old
    finally:
        shutil.rmtree(d)


@test
def animation_palette_and_font():
    from nd_breach_runner.core import animation as an
    assert an.PALETTE.shape == (256, 3)
    assert len({tuple(c) for c in an.PALETTE.tolist()}) == 256
    for ch in "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ:+-. ":
        assert ch in an.FONT and len(an.FONT[ch]) == 7 and max(an.FONT[ch]) < 32, ch
    img = np.zeros((40, 200), np.uint8)
    s = "T+01:30 after"
    n = an.draw_text(img, 3, 4, s, 9, scale=2)
    bits = sum(bin(r).count("1") for ch in s.upper() for r in an.FONT[ch])
    assert n == bits * 4 and (img == 9).sum() == n
    ys, xs = np.nonzero(img)
    assert ys.min() >= 4 and ys.max() < 4 + 14 and xs.min() >= 3
    assert xs.max() < 3 + an.text_width(s, 2)
    assert an.text_mask("a", 1).tolist() == an.text_mask("A", 1).tolist()      # upper case
    assert np.array_equal(an.text_mask("~"), an.text_mask("?"))                 # unknown: '?'
    # the 'T' glyph: full top row, then the centre column
    t = an.text_mask("T")
    assert t[0].all() and t[1:, 2].all() and not t[1:, [0, 1, 3, 4]].any()
    # clipped at the image edge, no error
    assert an.draw_text(img, 195, 35, "88", 1, 3) < bits * 9
    txt, sc = an.fit_text("x" * 100, 120, 3)
    assert sc == 1 and txt.endswith("..") and an.text_width(txt) <= 120
    assert an.fit_text("AB", 400, 3) == ("AB", 3)


@test
def animation_hillshade_range():
    from nd_breach_runner.core import animation as an
    flat = an.hillshade(np.full((10, 12), 5.0), 10.0)
    assert np.allclose(flat, np.sin(np.radians(45.0)))
    x = np.arange(20) * 10.0
    east_up = np.tile(x * 0.5, (20, 1))                  # rises to the east: faces west
    west_up = east_up[:, ::-1]                           # faces east
    y = np.arange(20)[:, None] * 10.0 * np.ones((1, 20))
    north_up = y[::-1] * 0.5                             # rises to the north: faces south
    for a in (east_up, west_up, north_up):
        h = an.hillshade(a, 10.0)
        assert h.min() >= 0.0 and h.max() <= 1.0
    # lit from the north-west: west- and north-facing slopes brighter than flat, east darker
    assert an.hillshade(east_up, 10.0).mean() > flat.mean() > an.hillshade(west_up, 10.0).mean()
    assert an.hillshade(north_up[::-1], 10.0).mean() > an.hillshade(north_up, 10.0).mean()
    steep = an.hillshade(np.tile(x * 100.0, (5, 1))[:, ::-1], 10.0)     # a cliff facing away
    assert steep.min() >= 0.0 and steep.max() < 0.05
    z = np.random.default_rng(0).normal(0, 30, (30, 30))
    z[3, 4] = np.nan
    h = an.hillshade(z, 5.0)
    assert np.isnan(h[3, 4]) and np.nanmin(h) >= 0 and np.nanmax(h) <= 1
    tone, nod = an.terrain_tone(z, 5.0)
    assert nod[3, 4] and tone.min() >= 0.0 and tone.max() <= 1.0


@test
def animation_labels_names_and_blocks():
    from nd_breach_runner.core import animation as an
    assert an.frame_times_label(5400.0, 0.0) == "T+01:30 after breach initiation"
    assert an.frame_times_label(0.0, 0.5) == "T-00:30 before breach initiation"
    assert an.frame_times_label(1800.0, 0.5) == "T+00:00 after breach initiation"
    assert an.frame_times_label(3600.0 * 30.25, 0.0) == "T+30:15 after breach initiation"
    assert an.frame_times_label(750.0, 0.0) == "T+00:13 after breach initiation"
    assert an.geotiff_names([1.5, -0.25, 0.0]) == ["Depth T+001.50h.tif", "Depth T-000.25h.tif",
                                                   "Depth T+000.00h.tif"]
    names = an.geotiff_names([0.0, 10 / 3600.0, 20 / 3600.0])          # 10 s apart
    assert len(set(names)) == 3 and names[1] == "Depth T+000.003h.tif", names
    assert an.iso_hours(1.5) == "PT1H30M0S" and an.iso_hours(-0.25) == "-PT0H15M0S"
    a = np.array([[1, np.nan, 3, 4, 5], [np.nan, np.nan, 8, 9, 10], [11, 12, 13, np.nan, 15]])
    m = an.block_reduce(a, 2, "max")
    assert m.shape == (2, 3)
    assert m[0, 0] == 1 and m[0, 1] == 9 and m[0, 2] == 10 and m[1, 0] == 12 and m[1, 2] == 15
    allnan = an.block_reduce(np.full((4, 4), np.nan), 2, "max")
    assert np.isnan(allnan).all()
    mean = an.block_reduce(a, 2, "mean")
    assert mean[0, 0] == 1 and mean[0, 1] == 6.0
    cls, wet = an.depth_class(np.array([np.nan, 0.005, 0.5, 1.0, 2.9, 3.0, 6.5, 10.0, 40.0]))
    assert wet.tolist() == [False, False] + [True] * 7
    assert cls[wet].tolist() == [0, 1, 1, 2, 3, 4, 4]


@test
def animation_render_run_on_synthetic_snapshots():
    from nd_breach_runner.core import animation as an
    from nd_breach_runner.core.units import FT
    from nd_breach_runner.tests.synthetic import npy_writer
    d = tempfile.mkdtemp()
    try:
        npz, dem, times, depth = _synthetic(d)
        info = an.npz_info(npz)
        assert (info["n"], info["ny"], info["nx"]) == (7, 40, 60) and info["meta"] == {}
        got = list(an.iter_depth_frames(npz, 2))
        assert [k for k, _ in got] == [0, 2, 4, 6]
        assert np.array_equal(got[1][1], depth[2], equal_nan=True)
        out = os.path.join(d, "anim")
        dam_xy = (1000.0 + 25.0, 5000.0 + 200.0)
        prog = []
        r = an.render_run(npz, dem, out, t_ref_hr=0.25, title="Test Creek Dam (ND00001)",
                          dam_xy=dam_xy, fps=5, geotiff_writer=npy_writer,
                          crs_note="LISFLOOD-FP depth snapshots, EPSG:26914",
                          progress=lambda p, m="": prog.append(p))
        n = 7
        assert len(r.frames) == n and len(r.times_s) == n and prog[-1] == 100.0
        assert r.labels[0] == "T-00:15 before breach initiation"
        assert r.labels[-1] == "T+00:45 after breach initiation"
        W, H = r.size
        assert W % 2 == 0 and H % 2 == 0
        # the flood covers only part of the grid: the frames are cropped to it
        r0, r1, c0, c1 = r.window
        assert (r1 - r0) < 40 and c1 == 60
        assert abs(r.max_depth_ft - float(np.nanmax(depth)) / FT) < 0.01
        # PNG frames decode; the GIF has the same frames and a 2 s hold at the end
        pngs = [_read_png(p) for p in r.frames]
        assert all(p.shape == (H, W, 3) for p in pngs)
        pal, gframes, delays, loop = _read_gif(r.gif)
        assert len(gframes) == n and delays[:-1] == [20] * (n - 1) and delays[-1] == 200
        for png, g in zip(pngs, gframes):
            assert np.array_equal(png, an.PALETTE[g])
        mx, my, mw, mh = r.map_rect
        idx = [_inverse_palette(p) for p in pngs]
        maps = [i[my:my + mh, mx:mx + mw] for i in idx]
        water = lambda im: (im >= an.WATER0).sum()
        assert water(maps[0]) == 0 and water(maps[-1]) > water(maps[3]) > 0
        classes = {int((v - an.WATER0) // an.N_SHADE) for v in np.unique(maps[-2])
                   if v >= an.WATER0}
        assert classes == {0, 1, 2, 3, 4}, classes
        assert (maps[-1] == an.C_FRONT).any() and not (maps[0] == an.C_FRONT).any()
        assert (idx[-1] == an.C_RED).any()                                  # breach tick
        assert (idx[0] == an.C_BLACK).sum() > 500                          # text and dam marker
        Image = _pil()
        if Image is not None:
            im = Image.open(r.gif)
            assert im.n_frames == n and im.size == (W, H)
            for i in (0, 3, n - 1):
                im.seek(i)
                assert np.array_equal(np.asarray(im.convert("RGB")), pngs[i])
            im.close()
        # GeoTIFF frames (the test writer writes .npy) in feet, with their time index
        assert len(r.geotiffs) == n
        assert os.path.basename(r.geotiffs[0]) == "Depth T-000.25h.tif"
        a = np.load(r.geotiffs[-1][:-4] + ".npy")
        exp = depth[-1][r0:r1, c0:c1] / FT
        exp = np.where(np.isfinite(exp) & (exp > an.WET_FT), exp, np.nan)
        assert np.allclose(a, exp, equal_nan=True, atol=1e-4)
        geo = json.load(open(r.geotiffs[-1][:-4] + ".json"))
        assert geo["x0"] == dem.x0 + c0 * dem.dx and geo["y0"] == dem.y0 - r0 * dem.dx
        ix = json.load(open(r.index_json))
        fr = ix["frames"]
        assert [f["path"] for f in fr] == [os.path.basename(p) for p in r.geotiffs]
        assert fr[0]["start_hr"] == -0.25 and fr[0]["end_hr"] == fr[1]["start_hr"]
        assert abs(fr[-1]["end_hr"] - fr[-1]["start_hr"] - 1 / 6.0) < 1e-6
        rng = an.temporal_ranges(r.index_json, datetime.datetime(2000, 1, 1))
        close = lambda a, b: abs((a - b).total_seconds()) < 1e-3
        assert close(rng[1][1], datetime.datetime(2000, 1, 1) - datetime.timedelta(minutes=5))
        assert close(rng[-1][2], datetime.datetime(2000, 1, 1, 0, 55))
        assert rng[0][0] == os.path.join(out, "Depth T-000.25h.tif")
        # the grid is recorded next to the snapshots for a later re-render
        side = an.npz_info(npz)["meta"]
        assert side["x0"] == dem.x0 and side["dx"] == dem.dx and side["t_ref_hr"] == 0.25
        assert side["dam_xy"] == list(dam_xy)
        # MP4 when ffmpeg is there
        if shutil.which("ffmpeg"):
            assert r.mp4 and os.path.getsize(r.mp4) > 0, r.notes
            if shutil.which("ffprobe"):
                c = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                                    "-count_frames", "-show_entries", "stream=nb_read_frames",
                                    "-of", "csv=p=0", r.mp4], stdout=subprocess.PIPE)
                assert int(c.stdout.decode().strip()) == n
        else:
            assert not r.mp4 and any("ffmpeg" in x for x in r.notes)

        # every other snapshot, a small size (block maximum), no GIF/MP4, frames kept
        r2 = an.render_run(npz, dem, out, 0.25, "", None, max_px=24, step=2, make_gif=False,
                           make_mp4=False, crop=False, front_ft=None)
        assert len(r2.frames) == 4 and not r2.gif and r2.cells_per_px == 3.0
        assert not os.path.exists(r.frames[-1])            # the old frames were replaced
        assert r2.window == (0, 40, 0, 60)
        # a single 1-cell-deep spot survives the block maximum
        spot = np.full((2, 40, 60), np.nan, np.float32)
        spot[1, 20, 31] = 5.0
        sp = os.path.join(d, "spot.npz")
        np.savez_compressed(sp, times_s=np.array([0.0, 60.0]), depth=spot)
        r3 = an.render_run(sp, dem, os.path.join(d, "spot"), max_px=20, crop=False,
                           make_gif=False, make_mp4=False, front_ft=None)
        mx, my, mw, mh = r3.map_rect
        last = _inverse_palette(_read_png(r3.frames[-1]))[my:my + mh, mx:mx + mw]
        deep = last >= an.WATER0 + 4 * an.N_SHADE                    # 16 ft: the deepest class
        assert deep.sum() == 1 and (last >= an.WATER0).sum() == 1
        # no water at all: terrain only, and said so
        dry = os.path.join(d, "dry.npz")
        np.savez_compressed(dry, times_s=np.array([0.0, 60.0]),
                            depth=np.full((2, 40, 60), np.nan, np.float32))
        r4 = an.render_run(dry, dem, os.path.join(d, "dry"), make_mp4=False)
        assert any("No water" in x for x in r4.notes) and len(r4.frames) == 2
        # keep_frames=False removes the PNGs once the GIF is written
        r5 = an.render_run(dry, dem, os.path.join(d, "dry2"), make_mp4=False, keep_frames=False)
        assert r5.frames == [] and os.path.exists(r5.gif)
        # the DEM must be on the snapshots' grid
        try:
            an.render_run(npz, dem.window(0, 10, 0, 10), os.path.join(d, "bad"))
            raise AssertionError("expected ValueError")
        except ValueError as e:
            assert "snapshots" in str(e)
    finally:
        shutil.rmtree(d)


# ---------------------------------------------------------------------------------------
# the Processing tool, through the fake QGIS
# ---------------------------------------------------------------------------------------
def _load_tool():
    import importlib
    from nd_breach_runner.tests import fake_qgis as fq
    fq.install()
    import nd_breach_runner.algorithms as algs
    importlib.reload(algs)
    import nd_breach_runner.algorithms_tools as tools
    importlib.reload(tools)
    import nd_breach_runner.algorithms_animation as aa
    importlib.reload(aa)
    return aa, fq


class _Raster:
    def __init__(self, src):
        self.src = src

    def source(self):
        return self.src


@test
def animation_algorithm_is_defined():
    aa, fq = _load_tool()
    assert aa.ANIMATION_ALGORITHMS == (aa.AnimationAlgorithm,)
    a = aa.AnimationAlgorithm()
    a.initAlgorithm()
    assert a.name() == "animation"
    assert a.displayName() == "Flood animation (GIF / MP4 / time-enabled layers)"
    for k in ("DAM_FOLDER", "DEM", "DEM_Z", "TITLE", "FPS", "MAX_PX", "STEP", "FRONT_FT", "GIF",
              "MP4", "GEOTIFF", "LOAD_TEMPORAL", "BASE_TIME", "KEEP_FRAMES", "OUT_FOLDER"):
        assert a.parameterDefinition(k) is not None, k
    for k in ("FOLDER", "GIF", "MP4", "INDEX", "RESULT"):
        assert k in a.outputs
    assert a.parameterDefinition("BASE_TIME").kwargs["defaultValue"] == "2000-01-01 00:00"
    assert "not been tested in QGIS" in a.shortHelpString()
    assert a.createInstance().name() == "animation"
    assert aa.parse_base_time("") == datetime.datetime(2000, 1, 1)
    assert aa.parse_base_time("2026-04-01T06:30") == datetime.datetime(2026, 4, 1, 6, 30)
    assert aa.parse_base_time("2026-04-01") == datetime.datetime(2026, 4, 1)
    try:
        aa.parse_base_time("next tuesday")
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


@test
def animation_algorithm_end_to_end_through_the_fake_harness():
    aa, fq = _load_tool()
    from nd_breach_runner.core import animation as an, gdal_io, workflow
    from nd_breach_runner.core.grid import Grid
    from nd_breach_runner.tests.synthetic import npy_writer
    d = tempfile.mkdtemp()
    folder = os.path.join(d, "ND00001")
    md = os.path.join(folder, workflow.MODEL_FOLDER)
    os.makedirs(os.path.join(md, "lisflood"))
    npz, dem, times, depth = _synthetic(md)
    assert npz == os.path.join(md, an.SNAPSHOT_FILE)
    # the model's own DEM, as LISFLOOD-FP read it (local coordinates)
    Grid(dem.data, 0.0, dem.ny * dem.dx, dem.dx).write_asc(os.path.join(md, "lisflood",
                                                                        "run.dem.asc"))
    report = {"dam": {"dam_id": "ND00001", "name": "Test Creek Dam"}, "dam_xy": [1025.0, 5200.0],
              "hydrograph": {"breach_initiation_hr": 0.5},
              "settings": {"cell_size_m": dem.dx, "tier": "lisflood"},
              "lisflood_runs": [{"run": 1, "bounds": [0, 0, 1, 1]},
                                {"run": 2, "bounds": list(dem.bounds)}]}
    json.dump(report, open(os.path.join(md, "run_report.json"), "w"))
    json.dump({"type": "FeatureCollection", "features": [],
               "crs": {"type": "name", "properties": {"name": "EPSG:26914"}}},
              open(os.path.join(folder, "Flood Extent.geojson"), "w"))
    real = (gdal_io.GeoTiffWriter, gdal_io.read_like, gdal_io.z_to_m_factor, gdal_io.read_grid)
    writers = []

    def fake_writer(crs):
        writers.append(crs)
        return npy_writer
    gdal_io.GeoTiffWriter = fake_writer
    try:
        ctx = fq.QgsProcessingContext()
        a = aa.AnimationAlgorithm()
        a.initAlgorithm()
        # 1) no grid recorded yet: from the last LISFLOOD-FP domain in the report; the
        #    time-enabled layers are queued, their ranges cannot attach outside QGIS
        fb = fq.QgsProcessingFeedback()
        r = a.processAlgorithm({"DAM_FOLDER": folder, "MP4": False, "LOAD_TEMPORAL": True},
                               ctx, fb)
        out = os.path.join(folder, "Animation")
        assert r["FOLDER"] == out and os.path.exists(r["GIF"]) and r["MP4"] == ""
        assert any("last LISFLOOD-FP domain" in m for m in fb.info), fb.info
        assert any("model's DEM" in m for m in fb.info), fb.info
        assert writers == ["EPSG:26914"]
        tifs = sorted(p for p in ctx.loaded if p.endswith(".tif"))
        assert len(tifs) == 7 and os.path.basename(tifs[0]) == "Depth T+000.00h.tif", tifs
        assert os.path.exists(r["INDEX"]) and "7 time-enabled" not in r["RESULT"]
        assert any("could not be attached" in e for e in fb.errors), fb.errors
        _pal, gframes, _dl, _lp = _read_gif(r["GIF"])
        assert len(gframes) == 7
        ix = json.load(open(r["INDEX"]))
        assert ix["t_ref_hr"] == 0.5 and ix["frames"][0]["start_hr"] == -0.5
        side = an.read_grid_sidecar(npz)
        assert side["x0"] == dem.x0 and side["crs_wkt"] == "EPSG:26914"
        assert side["title"] == "Test Creek Dam (ND00001)"

        # 2) the grid is now recorded with the snapshots; a DEM layer gives the hillshade
        gdal_io.read_like = lambda p, g, alg="bilinear": dem.data / 0.3048
        gdal_io.z_to_m_factor = lambda p, choice="auto": (0.3048, "feet (test)")
        fb = fq.QgsProcessingFeedback()
        own = os.path.join(d, "own")
        r = a.processAlgorithm({"DAM_FOLDER": folder, "DEM": _Raster("dem.tif"), "MP4": False,
                                "GIF": True, "STEP": 3, "TITLE": "My title", "MAX_PX": 300,
                                "OUT_FOLDER": own}, ctx, fb)
        assert r["FOLDER"] == own and r["INDEX"] == "" and r["RESULT"].startswith("3 frames")
        assert any("recorded with the snapshots" in m for m in fb.info), fb.info
        assert any("feet (test)" in m for m in fb.info), fb.info

        # 3) no sidecar and no report domain, but 'Depth (Max).tif' on the same grid
        os.remove(an.sidecar_path(npz))
        report["lisflood_runs"] = []
        json.dump(report, open(os.path.join(md, "run_report.json"), "w"))
        open(os.path.join(folder, workflow.FINAL_NAMES["depth"]), "w").write("x")
        gdal_io.read_grid = lambda p: Grid(np.zeros((40, 60)), dem.x0, dem.y0, dem.dx,
                                           crs_wkt="EPSG:26914")
        fb = fq.QgsProcessingFeedback()
        r = a.processAlgorithm({"DAM_FOLDER": folder, "MP4": False, "GIF": False,
                                "GEOTIFF": True, "OUT_FOLDER": own}, ctx, fb)
        assert any("Depth (Max).tif" in m for m in fb.info), fb.info
        assert os.path.exists(r["INDEX"]) and r["GIF"] == ""

        # 4) errors are Processing exceptions
        os.remove(an.sidecar_path(npz))
        gdal_io.read_grid = lambda p: Grid(np.zeros((5, 5)), 0, 0, 1.0)   # re-projected
        for params in ({"DAM_FOLDER": folder},                                # grid unknown
                       {"DAM_FOLDER": os.path.join(d, "nowhere")},
                       {"DAM_FOLDER": d}):                                   # no snapshots
            try:
                a.processAlgorithm(params, ctx, fq.QgsProcessingFeedback())
                raise AssertionError(params)
            except fq.QgsProcessingException as e:
                assert "snapshots" in str(e) or "dam folder" in str(e), str(e)
        an.write_grid_sidecar(npz, Grid(np.zeros((40, 60)), dem.x0, dem.y0, dem.dx))  # no CRS
        try:
            a.processAlgorithm({"DAM_FOLDER": folder, "GEOTIFF": True, "MP4": False}, ctx,
                               fq.QgsProcessingFeedback())
            raise AssertionError("GeoTIFFs need the CRS")
        except fq.QgsProcessingException as e:
            assert "CRS" in str(e)
        try:
            a.processAlgorithm({"DAM_FOLDER": folder, "LOAD_TEMPORAL": True, "MP4": False,
                                "BASE_TIME": "soon"}, ctx, fq.QgsProcessingFeedback())
            raise AssertionError("bad base time")
        except fq.QgsProcessingException as e:
            assert "date-time" in str(e), str(e)
    finally:
        (gdal_io.GeoTiffWriter, gdal_io.read_like, gdal_io.z_to_m_factor,
         gdal_io.read_grid) = real
        shutil.rmtree(d)


# ---------------------------------------------------------------------------------------
@model_test
def animation_from_a_lisflood_run():
    """LISFLOOD-FP writes depth snapshots of a small channel flood; they are packed and
    animated."""
    exe = os.environ.get("NDBREACH_LISFLOOD", "")
    if not exe or not os.path.isfile(exe):
        print("   (skipped: NDBREACH_LISFLOOD not set)")
        return
    from nd_breach_runner.core import animation as an, lisflood as lf
    from nd_breach_runner.core.breach import Hydrograph
    from nd_breach_runner.core.grid import Grid
    d = tempfile.mkdtemp()
    try:
        nr, nc, dx = 24, 90, 20.0
        X, Y = np.meshgrid((np.arange(nc) + 0.5) * dx, (np.arange(nr) + 0.5) * dx)
        z = 100.0 - 0.002 * X + 0.02 * np.abs(Y - nr * dx / 2)
        dem = Grid(z[::-1], 500000.0, 5200000.0 + nr * dx, dx, crs_wkt="EPSG:26914")
        hy = Hydrograph(np.array([0.0, 600.0, 1200.0]), np.array([30.0, 30.0, 0.0]), "test")
        st = lf.LisfloodSettings(exe, arrival_depth_m=0.3, sim_time_s=1500.0, massint_s=60.0,
                                 initial_tstep_s=1.0, snapshot_s=150.0)
        wd = os.path.join(d, "lisflood")
        inflow = (dem.x0 + 30.0, dem.y0 - nr * dx / 2 + 5)
        par = lf.write_inputs(wd, dem, inflow, hy, st)
        lf.run(wd, par, st)
        files = lf.snapshot_files(wd)
        assert len(files) >= 8, files
        npz = os.path.join(d, an.SNAPSHOT_FILE)
        t, p = lf.collect_snapshots(wd, st, npz)
        assert p == npz and not lf.snapshot_files(wd)          # text files removed
        info = an.npz_info(npz)
        assert info["n"] == len(t) and (info["ny"], info["nx"]) == dem.data.shape
        wet = [int(np.isfinite(a).sum()) for _k, a in an.iter_depth_frames(npz)]
        assert wet[-1] > wet[1] > 0, wet                       # the flood spreads
        r = an.render_run(npz, dem, os.path.join(d, "anim"), t_ref_hr=0.0,
                          title="LISFLOOD-FP snapshot test", dam_xy=(dem.x0 + 20.0, inflow[1]),
                          make_mp4=bool(shutil.which("ffmpeg")))
        pal, frames, delays, _loop = _read_gif(r.gif)
        assert len(frames) == info["n"] == len(r.frames)
        mx, my, mw, mh = r.map_rect
        wet_px = [(f[my:my + mh, mx:mx + mw] >= an.WATER0).sum() for f in frames]
        assert wet_px[-1] > wet_px[1] > 0, wet_px
        Image = _pil()
        if Image is not None:
            im = Image.open(r.gif)
            assert im.n_frames == info["n"]
            im.seek(im.n_frames - 1)
            assert np.array_equal(np.asarray(im.convert("RGB")), an.PALETTE[frames[-1]])
            im.close()
        assert r.labels[-1] == an.frame_times_label(t[-1], 0.0)
        if shutil.which("ffmpeg"):
            assert r.mp4 and os.path.getsize(r.mp4) > 0, r.notes
    finally:
        shutil.rmtree(d)
