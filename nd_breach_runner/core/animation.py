"""Flood animations from LISFLOOD-FP depth snapshots.

`render_run()` turns `_breach_model/depth_snapshots.npz` (written by
`lisflood.collect_snapshots`: `times_s` (n,) and `depth` (n, ny, nx) float32 metres,
NaN where dry, row 0 north, on the run's DEM grid) into

- PNG frames (8-bit RGB): a hillshade of the DEM with the depth drawn in the EAP
  map's depth classes (products.DEPTH_CLASSES_FT / DEPTH_COLORS), the time after
  breach initiation ('T+hh:mm'), a timeline, a legend and a scale bar;
- an animated GIF (GIF89a, LZW, one fixed 256-colour palette);
- an MP4 when `ffmpeg` is on the PATH;
- optionally one GeoTIFF per frame (depth in ft) and a JSON index of the frames'
  time ranges, for QGIS's temporal controller.

Only numpy and the standard library (PIL, matplotlib and scipy are not always in
QGIS). The depth array is streamed one snapshot at a time from the .npz, so long
runs on large grids do not need the whole stack in memory.
"""
import datetime
import json
import math
import os
import re
import shutil
import struct
import subprocess
import sys
import zipfile
import zlib

import numpy as np

from .products import DEPTH_CLASSES_FT, DEPTH_COLORS
from .units import FT, MILE

SNAPSHOT_FILE = "depth_snapshots.npz"
WET_FT = 0.01                 # shallower than this is drawn dry (as the final rasters)
META_KEYS = ("x0", "y0", "dx", "crs_wkt", "t_ref_hr")


class Cancelled(RuntimeError):
    pass


# ======================================================================================
# 5 x 7 bitmap font: 7 rows per glyph, 5 bits per row (bit 4 = left column)
# ======================================================================================
FONT = {
    "0": (0x0E, 0x11, 0x13, 0x15, 0x19, 0x11, 0x0E),
    "1": (0x04, 0x0C, 0x04, 0x04, 0x04, 0x04, 0x0E),
    "2": (0x0E, 0x11, 0x01, 0x02, 0x04, 0x08, 0x1F),
    "3": (0x1F, 0x02, 0x04, 0x02, 0x01, 0x11, 0x0E),
    "4": (0x02, 0x06, 0x0A, 0x12, 0x1F, 0x02, 0x02),
    "5": (0x1F, 0x10, 0x1E, 0x01, 0x01, 0x11, 0x0E),
    "6": (0x06, 0x08, 0x10, 0x1E, 0x11, 0x11, 0x0E),
    "7": (0x1F, 0x01, 0x02, 0x04, 0x08, 0x08, 0x08),
    "8": (0x0E, 0x11, 0x11, 0x0E, 0x11, 0x11, 0x0E),
    "9": (0x0E, 0x11, 0x11, 0x0F, 0x01, 0x02, 0x0C),
    "A": (0x0E, 0x11, 0x11, 0x1F, 0x11, 0x11, 0x11),
    "B": (0x1E, 0x11, 0x11, 0x1E, 0x11, 0x11, 0x1E),
    "C": (0x0E, 0x11, 0x10, 0x10, 0x10, 0x11, 0x0E),
    "D": (0x1C, 0x12, 0x11, 0x11, 0x11, 0x12, 0x1C),
    "E": (0x1F, 0x10, 0x10, 0x1E, 0x10, 0x10, 0x1F),
    "F": (0x1F, 0x10, 0x10, 0x1E, 0x10, 0x10, 0x10),
    "G": (0x0E, 0x11, 0x10, 0x17, 0x11, 0x11, 0x0F),
    "H": (0x11, 0x11, 0x11, 0x1F, 0x11, 0x11, 0x11),
    "I": (0x0E, 0x04, 0x04, 0x04, 0x04, 0x04, 0x0E),
    "J": (0x07, 0x02, 0x02, 0x02, 0x02, 0x12, 0x0C),
    "K": (0x11, 0x12, 0x14, 0x18, 0x14, 0x12, 0x11),
    "L": (0x10, 0x10, 0x10, 0x10, 0x10, 0x10, 0x1F),
    "M": (0x11, 0x1B, 0x15, 0x15, 0x11, 0x11, 0x11),
    "N": (0x11, 0x11, 0x19, 0x15, 0x13, 0x11, 0x11),
    "O": (0x0E, 0x11, 0x11, 0x11, 0x11, 0x11, 0x0E),
    "P": (0x1E, 0x11, 0x11, 0x1E, 0x10, 0x10, 0x10),
    "Q": (0x0E, 0x11, 0x11, 0x11, 0x15, 0x12, 0x0D),
    "R": (0x1E, 0x11, 0x11, 0x1E, 0x14, 0x12, 0x11),
    "S": (0x0F, 0x10, 0x10, 0x0E, 0x01, 0x01, 0x1E),
    "T": (0x1F, 0x04, 0x04, 0x04, 0x04, 0x04, 0x04),
    "U": (0x11, 0x11, 0x11, 0x11, 0x11, 0x11, 0x0E),
    "V": (0x11, 0x11, 0x11, 0x11, 0x11, 0x0A, 0x04),
    "W": (0x11, 0x11, 0x11, 0x15, 0x15, 0x15, 0x0A),
    "X": (0x11, 0x11, 0x0A, 0x04, 0x0A, 0x11, 0x11),
    "Y": (0x11, 0x11, 0x11, 0x0A, 0x04, 0x04, 0x04),
    "Z": (0x1F, 0x01, 0x02, 0x04, 0x08, 0x10, 0x1F),
    " ": (0, 0, 0, 0, 0, 0, 0),
    ":": (0, 0x0C, 0x0C, 0, 0x0C, 0x0C, 0),
    "+": (0, 0x04, 0x04, 0x1F, 0x04, 0x04, 0),
    "-": (0, 0, 0, 0x1F, 0, 0, 0),
    ".": (0, 0, 0, 0, 0, 0x0C, 0x0C),
    ",": (0, 0, 0, 0, 0x0C, 0x04, 0x08),
    "(": (0x02, 0x04, 0x08, 0x08, 0x08, 0x04, 0x02),
    ")": (0x08, 0x04, 0x02, 0x02, 0x02, 0x04, 0x08),
    "/": (0x01, 0x01, 0x02, 0x04, 0x08, 0x10, 0x10),
    "<": (0x02, 0x04, 0x08, 0x10, 0x08, 0x04, 0x02),
    ">": (0x08, 0x04, 0x02, 0x01, 0x02, 0x04, 0x08),
    "=": (0, 0, 0x1F, 0, 0x1F, 0, 0),
    "_": (0, 0, 0, 0, 0, 0, 0x1F),
    "'": (0x0C, 0x04, 0x08, 0, 0, 0, 0),
    "!": (0x04, 0x04, 0x04, 0x04, 0x04, 0, 0x04),
    "%": (0x18, 0x19, 0x02, 0x04, 0x08, 0x13, 0x03),
    "?": (0x0E, 0x11, 0x01, 0x02, 0x04, 0, 0x04),
}
GLYPH_W, GLYPH_H, ADVANCE = 5, 7, 6
_GLYPHS = {}


def _glyph(ch):
    ch = ch.upper()
    g = _GLYPHS.get(ch)
    if g is None:
        rows = FONT.get(ch, FONT["?"])
        g = np.array([[(r >> (4 - i)) & 1 for i in range(GLYPH_W)] for r in rows], bool)
        _GLYPHS[ch] = g
    return g


def text_width(s, scale=1):
    return max(0, len(str(s)) * ADVANCE - 1) * int(scale)


def text_mask(s, scale=1):
    """Boolean mask (7*scale, text_width) of `s` in the built-in font (upper case;
    characters it lacks are drawn as '?')."""
    s = str(s)
    m = np.zeros((GLYPH_H, max(0, len(s) * ADVANCE - 1)), bool)
    for i, ch in enumerate(s):
        m[:, i * ADVANCE:i * ADVANCE + GLYPH_W] = _glyph(ch)
    if scale > 1:
        m = np.repeat(np.repeat(m, scale, axis=0), scale, axis=1)
    return m


def draw_text(img, x, y, s, color, scale=1):
    """Draw `s` into the 2-D index image with its top-left corner at (x, y),
    clipped to the image. Returns the number of pixels set."""
    m = text_mask(s, scale)
    return _paste_mask(img, m, int(x), int(y), color)


def _paste_mask(img, m, x, y, color):
    h, w = m.shape
    H, W = img.shape[:2]
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(W, x + w), min(H, y + h)
    if x1 <= x0 or y1 <= y0:
        return 0
    sub = m[y0 - y:y1 - y, x0 - x:x1 - x]
    img[y0:y1, x0:x1][sub] = color
    return int(sub.sum())


def fit_text(s, width, max_scale=3):
    """(text, scale): the largest scale up to max_scale at which `s` fits in
    `width` pixels, shortened with '..' if it does not fit even at scale 1."""
    s = str(s)
    for sc in range(int(max_scale), 0, -1):
        if text_width(s, sc) <= width:
            return s, sc
    n = max(0, (width + 1) // ADVANCE - 2)
    return (s[:n] + ".." if n > 0 else ""), 1


# ======================================================================================
# palette: UI colours, hillshade greys, depth classes shaded by the terrain
# ======================================================================================
N_UI, N_GREY, N_SHADE = 16, 160, 16
GREY0 = N_UI                        # 16..175 terrain greys (dark to light)
WATER0 = N_UI + N_GREY              # 176..255 depth class c, shade l: WATER0 + 16 c + l
(C_WHITE, C_BLACK, C_FRAME, C_SUBTEXT, C_NODATA, C_FRONT, C_BAR_BG, C_BAR_FG, C_RED,
 C_PANEL) = range(10)
C_TRANSPARENT = 15                  # GIF: pixel unchanged from the previous frame
_UI = [(255, 255, 255), (0, 0, 0), (80, 80, 80), (105, 105, 105), (226, 226, 230),
       (25, 25, 25), (212, 212, 212), (42, 85, 181), (214, 39, 40), (244, 241, 234),
       (10, 0, 0), (0, 10, 0), (0, 0, 10), (10, 10, 0), (0, 10, 10), (255, 0, 255)]


def _hex(c):
    return tuple(int(c[i:i + 2], 16) for i in (1, 3, 5))


def build_palette():
    """The fixed 256-colour palette (256, 3) uint8. Every entry is a different
    colour, so an RGB image maps back to its indices exactly."""
    pal = np.zeros((256, 3), np.int64)
    pal[:N_UI] = _UI
    lum = 92 + np.arange(N_GREY)                       # 92..251, one step per entry
    pal[GREY0:GREY0 + N_GREY] = np.stack([lum, lum - 2, lum - 7], axis=1)
    f = 0.80 + 0.28 * np.arange(N_SHADE) / (N_SHADE - 1)
    for c, col in enumerate(DEPTH_COLORS):
        base = np.array(_hex(col), float)
        pal[WATER0 + c * N_SHADE:WATER0 + (c + 1) * N_SHADE] = np.clip(
            np.round(base[None, :] * f[:, None]), 0, 255)
    pal = pal.astype(np.uint8)
    if len({tuple(p) for p in pal.tolist()}) != 256:
        raise AssertionError("the animation palette has repeated colours")
    return pal


PALETTE = build_palette()


# ======================================================================================
# terrain
# ======================================================================================
def hillshade(z, dx, azimuth=315.0, altitude=45.0):
    """Hillshade of elevations `z` (rows north to south, cell size `dx` in the
    units of z) lit from `azimuth` (degrees clockwise from north) and `altitude`
    (degrees above the horizon): 0 (dark) to 1 (lit); flat ground is
    sin(altitude). NaN where z is NaN."""
    z = np.asarray(z, dtype=np.float64)
    nan = ~np.isfinite(z)
    if nan.all():
        return np.full(z.shape, np.nan)
    zz = np.where(nan, np.nanmean(z), z) if nan.any() else z
    if zz.shape[0] < 2 or zz.shape[1] < 2:
        out = np.full(z.shape, math.sin(math.radians(altitude)))
        out[nan] = np.nan
        return out
    dzdr, dzdc = np.gradient(zz, float(dx))
    dzdx, dzdy = dzdc, -dzdr                             # east, north (row 0 is north)
    az, alt = math.radians(azimuth), math.radians(altitude)
    lx, ly, lz = math.sin(az) * math.cos(alt), math.cos(az) * math.cos(alt), math.sin(alt)
    shade = (-dzdx * lx - dzdy * ly + lz) / np.sqrt(1.0 + dzdx ** 2 + dzdy ** 2)
    shade = np.clip(shade, 0.0, 1.0)
    shade[nan] = np.nan
    return shade


def terrain_tone(z, dx):
    """0..1 tone for the map background: the hillshade, stretched to the
    window's own range (prairie is nearly flat), with a little of the relative
    elevation so valleys read darker than uplands. Returns (tone, nodata mask)."""
    z = np.asarray(z, dtype=np.float64)
    valid = np.isfinite(z)
    if not valid.any():
        return np.full(z.shape, 0.8), np.zeros(z.shape, bool)
    hs = hillshade(z, dx)
    lo, hi = np.percentile(hs[valid], [2.0, 98.0])
    span = max(hi - lo, 0.08)
    s = np.clip(0.5 + (hs - 0.5 * (lo + hi)) / span, 0.0, 1.0)
    e_lo, e_hi = np.percentile(z[valid], [2.0, 98.0])
    e = np.clip((z - e_lo) / max(e_hi - e_lo, 1e-6), 0.0, 1.0)
    tone = 0.25 + 0.75 * (0.78 * s + 0.22 * e)           # keep the map light
    tone[~valid] = 0.8
    return tone, ~valid


def block_reduce(a, k, how="max"):
    """k x k blocks of `a` -> one value: 'max' or 'mean', ignoring NaN (NaN
    where a block is all NaN). Edge blocks may be partial."""
    a = np.asarray(a, dtype=np.float64)
    if k <= 1:
        return a.copy()
    ny, nx = a.shape
    py, px = (-ny) % k, (-nx) % k
    if py or px:
        a = np.pad(a, ((0, py), (0, px)), constant_values=np.nan)
    b = a.reshape(a.shape[0] // k, k, a.shape[1] // k, k)
    if how == "max":
        m = np.where(np.isnan(b), -np.inf, b).max(axis=(1, 3))
        m[np.isneginf(m)] = np.nan
        return m
    ok = ~np.isnan(b)
    s = np.where(ok, b, 0.0).sum(axis=(1, 3))
    n = ok.sum(axis=(1, 3))
    return np.where(n > 0, s / np.maximum(n, 1), np.nan)


def depth_class(depth_ft):
    """Depth class index 0..4 (products.DEPTH_CLASSES_FT) and the wet mask."""
    d = np.asarray(depth_ft, dtype=np.float64)
    wet = np.isfinite(d) & (d > WET_FT)
    cls = np.digitize(np.where(wet, d, 0.0), DEPTH_CLASSES_FT[1:])
    return cls.astype(np.int64), wet


# ======================================================================================
# PNG and GIF writers
# ======================================================================================
def _png_chunk(kind, data):
    return (struct.pack(">I", len(data)) + kind + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))


def png_bytes(rgb, level=6):
    """An 8-bit RGB PNG of the (h, w, 3) uint8 array, each row with the 'Up'
    filter."""
    rgb = np.ascontiguousarray(rgb, dtype=np.uint8)
    h, w = rgb.shape[:2]
    rows = rgb.reshape(h, w * 3)
    up = rows.copy()
    up[1:] = rows[1:] - rows[:-1]                        # uint8 arithmetic wraps mod 256
    raw = np.empty((h, w * 3 + 1), np.uint8)
    raw[:, 0] = 2
    raw[:, 1:] = up
    return (b"\x89PNG\r\n\x1a\n"
            + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + _png_chunk(b"IDAT", zlib.compress(raw.tobytes(), level))
            + _png_chunk(b"IEND", b""))


def write_png(path, rgb, level=6):
    with open(path, "wb") as f:
        f.write(png_bytes(rgb, level))
    return path


def lzw_encode(data, min_code_size=8):
    """GIF LZW: variable-length codes (min_code_size + 1 up to 12 bits, packed
    least significant bit first), a clear code first and whenever the 4096-entry
    table is full, and the end code last. `data` is bytes of palette indices."""
    clear = 1 << min_code_size
    eoi = clear + 1
    out = bytearray()
    bits, nbits = clear, min_code_size + 1               # the clear code starts the stream
    size = min_code_size + 1
    nxt = eoi + 1
    table = {}
    it = iter(bytes(data))
    w = next(it, None)
    if w is None:
        bits |= eoi << nbits
        nbits += size
        while nbits > 0:
            out.append(bits & 0xFF)
            bits >>= 8
            nbits -= 8
        return bytes(out)
    get = table.get
    for k in it:
        key = (w << 8) | k
        c = get(key)
        if c is not None:
            w = c
            continue
        bits |= w << nbits
        nbits += size
        while nbits >= 8:
            out.append(bits & 0xFF)
            bits >>= 8
            nbits -= 8
        if nxt < 4096:
            table[key] = nxt
            nxt += 1
            if nxt > (1 << size) and size < 12:
                size += 1
        else:                                            # table full: start over
            bits |= clear << nbits
            nbits += size
            while nbits >= 8:
                out.append(bits & 0xFF)
                bits >>= 8
                nbits -= 8
            table = {}
            get = table.get
            size = min_code_size + 1
            nxt = eoi + 1
        w = k
    for code in (w, eoi):
        bits |= code << nbits
        nbits += size
        while nbits >= 8:
            out.append(bits & 0xFF)
            bits >>= 8
            nbits -= 8
        if code == w and nxt < 4096:                     # the decoder adds one more entry
            nxt += 1
            if nxt > (1 << size) and size < 12:
                size += 1
    if nbits > 0:
        out.append(bits & 0xFF)
    return bytes(out)


def lzw_decode(data, min_code_size=8):
    """Decode GIF LZW data (the inverse of lzw_encode, and of any conforming GIF
    encoder). Returns bytes."""
    clear = 1 << min_code_size
    eoi = clear + 1
    size = min_code_size + 1
    nxt = eoi + 1
    dic = [bytes([i]) for i in range(clear)] + [b"", b""]
    out = bytearray()
    prev = None
    bits = nbits = 0
    pos = 0
    n = len(data)
    while True:
        while nbits < size and pos < n:
            bits |= data[pos] << nbits
            nbits += 8
            pos += 1
        if nbits < size:
            break
        code = bits & ((1 << size) - 1)
        bits >>= size
        nbits -= size
        if code == clear:
            size = min_code_size + 1
            nxt = eoi + 1
            del dic[eoi + 1:]
            prev = None
            continue
        if code == eoi:
            break
        if code < len(dic):
            entry = dic[code]
        elif code == nxt and prev is not None:
            entry = prev + prev[:1]
        else:
            raise ValueError("bad LZW code {}".format(code))
        out += entry
        if prev is not None and nxt < 4096:
            dic.append(prev + entry[:1])
            nxt += 1
            if nxt == (1 << size) and size < 12:
                size += 1
        prev = entry
    return bytes(out)


def _sub_blocks(data):
    out = bytearray()
    for i in range(0, len(data), 255):
        part = data[i:i + 255]
        out.append(len(part))
        out += part
    out.append(0)
    return bytes(out)


class GifWriter:
    """Streams an animated GIF89a with one global 256-colour palette. With
    `optimize`, each frame after the first stores only the rectangle that
    changed, with unchanged pixels transparent (disposal 'leave in place')."""

    def __init__(self, path, width, height, palette=PALETTE, loop=0, optimize=True):
        self.path = path
        self.w, self.h = int(width), int(height)
        self.optimize = optimize
        self.prev = None
        self.count = 0
        pal = np.zeros((256, 3), np.uint8)
        pal[:len(palette)] = palette
        self.f = open(path, "wb")
        self.f.write(b"GIF89a" + struct.pack("<HHBBB", self.w, self.h, 0xF7, 0, 0)
                     + pal.tobytes())
        self.f.write(b"\x21\xFF\x0BNETSCAPE2.0\x03\x01" + struct.pack("<H", loop) + b"\x00")

    def add(self, img, delay_cs):
        img = np.ascontiguousarray(img, dtype=np.uint8)
        if img.shape != (self.h, self.w):
            raise ValueError("frame is {} but the GIF is {}".format(img.shape, (self.h, self.w)))
        left, top, sub, transparent = 0, 0, img, False
        if self.optimize and self.prev is not None:
            changed = img != self.prev
            if not changed.any():
                left, top, sub = 0, 0, np.full((1, 1), C_TRANSPARENT, np.uint8)
                transparent = True
            else:
                rows = np.nonzero(changed.any(axis=1))[0]
                cols = np.nonzero(changed.any(axis=0))[0]
                r0, r1, c0, c1 = rows[0], rows[-1] + 1, cols[0], cols[-1] + 1
                sub = img[r0:r1, c0:c1].copy()
                sub[~changed[r0:r1, c0:c1]] = C_TRANSPARENT
                left, top, transparent = int(c0), int(r0), True
        packed = (1 << 2) | (1 if transparent else 0)            # disposal 1: leave in place
        self.f.write(b"\x21\xF9\x04" + bytes([packed]) + struct.pack("<H", int(delay_cs))
                     + bytes([C_TRANSPARENT if transparent else 0]) + b"\x00")
        h, w = sub.shape
        self.f.write(b"\x2C" + struct.pack("<HHHHB", left, top, w, h, 0) + b"\x08")
        self.f.write(_sub_blocks(lzw_encode(sub.tobytes(), 8)))
        self.prev = img
        self.count += 1

    def close(self):
        if self.f is not None:
            self.f.write(b"\x3B")
            self.f.close()
            self.f = None


def write_gif(path, frames, delay_cs=25, optimize=True, palette=PALETTE):
    """All frames (2-D uint8 index images of one size) into one GIF."""
    frames = list(frames)
    h, w = frames[0].shape
    g = GifWriter(path, w, h, palette, optimize=optimize)
    try:
        for fr in frames:
            g.add(fr, delay_cs)
    finally:
        g.close()
    return path


# ======================================================================================
# the snapshot file
# ======================================================================================
def _read_exact(f, n):
    buf = bytearray()
    while len(buf) < n:
        part = f.read(n - len(buf))
        if not part:
            raise ValueError("the snapshot file is truncated")
        buf += part
    return bytes(buf)


def _npy_header(f):
    fmt = np.lib.format
    version = fmt.read_magic(f)
    if version == (1, 0):
        return fmt.read_array_header_1_0(f)
    return fmt.read_array_header_2_0(f)


def npz_info(npz_path):
    """{'times_s', 'n', 'ny', 'nx', 'meta'} of a snapshot file without reading
    the depth stack. `meta` holds any of x0, y0, dx, crs_wkt, t_ref_hr stored in
    the .npz or in its '.grid.json' sidecar (see write_grid_sidecar)."""
    with np.load(npz_path, allow_pickle=False) as z:
        if "times_s" not in z.files or "depth" not in z.files:
            raise ValueError("{} has no 'times_s' and 'depth' arrays".format(npz_path))
        times = np.asarray(z["times_s"], dtype=np.float64).ravel()
        meta = {}
        for k in META_KEYS:
            if k in z.files:
                v = z[k]
                meta[k] = str(v) if k == "crs_wkt" else float(v)
    with zipfile.ZipFile(npz_path) as zf:
        with zf.open("depth.npy") as f:
            shape, _fortran, _dtype = _npy_header(f)
    if len(shape) != 3:
        raise ValueError("the depth array is not (frames, rows, columns): {}".format(shape))
    if shape[0] != len(times):
        raise ValueError("{} depth frames but {} times".format(shape[0], len(times)))
    side = read_grid_sidecar(npz_path)
    for k in META_KEYS:
        if k not in meta and side.get(k) is not None:
            meta[k] = side[k]
    for k in ("dam_xy", "title"):
        if side.get(k):
            meta[k] = side[k]
    return {"times_s": times, "n": int(shape[0]), "ny": int(shape[1]), "nx": int(shape[2]),
            "meta": meta}


def iter_depth_frames(npz_path, step=1):
    """(index, depth) for every `step`-th snapshot, depth a float32 (ny, nx)
    array in metres, read one at a time from the compressed file."""
    step = max(1, int(step))
    with zipfile.ZipFile(npz_path) as zf:
        with zf.open("depth.npy") as f:
            shape, fortran, dtype = _npy_header(f)
            if fortran or dtype.hasobject:
                arr = None
            else:
                n, ny, nx = shape
                nb = ny * nx * dtype.itemsize
                for k in range(n):
                    buf = _read_exact(f, nb)
                    if k % step == 0:
                        yield k, np.frombuffer(buf, dtype=dtype).reshape(ny, nx).astype(
                            np.float32)
                return
    with np.load(npz_path, allow_pickle=False) as z:      # unusual layout: read it whole
        arr = z["depth"]
        for k in range(0, arr.shape[0], step):
            yield k, np.asarray(arr[k], dtype=np.float32)


def sidecar_path(npz_path):
    return os.path.splitext(npz_path)[0] + ".grid.json"


def write_grid_sidecar(npz_path, grid, t_ref_hr=0.0, dam_xy=None, title=""):
    """Record the snapshots' grid (and the breach time, dam and title) next to
    the .npz, so the animation can be made again later without the model run.
    Returns the path."""
    p = sidecar_path(npz_path)
    d = {"x0": grid.x0, "y0": grid.y0, "dx": grid.dx, "ny": grid.ny, "nx": grid.nx,
         "crs_wkt": grid.crs_wkt, "t_ref_hr": float(t_ref_hr or 0.0),
         "dam_xy": [float(dam_xy[0]), float(dam_xy[1])] if dam_xy is not None else None,
         "title": title or ""}
    with open(p, "w") as f:
        json.dump(d, f, indent=1)
    return p


def read_grid_sidecar(npz_path):
    p = sidecar_path(npz_path)
    if not os.path.exists(p):
        return {}
    try:
        with open(p) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


# ======================================================================================
# labels
# ======================================================================================
def frame_times_label(t_s, t_ref_hr=0.0):
    """'T+01:30 after breach initiation' (model time t_s seconds, breach at
    t_ref_hr hours of the record); 'T-00:45 before breach initiation' earlier."""
    dt_h = float(t_s) / 3600.0 - float(t_ref_hr or 0.0)
    mins = int(math.floor(abs(dt_h) * 60.0 + 0.5))          # half a minute rounds up
    before = dt_h < 0 and mins > 0
    return "T{}{:02d}:{:02d} {} breach initiation".format(
        "-" if before else "+", mins // 60, mins % 60, "before" if before else "after")


def _hours_name(h, decimals=2):
    return "T{}{:0{w}.{d}f}h".format("-" if h < 0 and round(abs(h), decimals) > 0 else "+",
                                     abs(h), w=4 + decimals, d=decimals)


def geotiff_names(hours):
    """'Depth T+001.50h.tif' names, with more decimals if two would clash."""
    for dec in (2, 3, 4, 6):
        names = ["Depth {}.tif".format(_hours_name(h, dec)) for h in hours]
        if len(set(names)) == len(names):
            return names
    return ["Depth {} ({}).tif".format(_hours_name(h, 6), i + 1) for i, h in enumerate(hours)]


def iso_hours(h):
    """ISO 8601 duration of `h` hours ('PT1H30M0S', '-PT0H15M0S')."""
    s = int(round(abs(float(h)) * 3600.0))
    return "{}PT{}H{}M{}S".format("-" if h < 0 and s else "", s // 3600, (s % 3600) // 60, s % 60)


def _nice(v):
    if v <= 0:
        return 1.0
    p = 10 ** math.floor(math.log10(v))
    for m in (5, 2, 1):
        if m * p <= v:
            return m * p
    return p


# ======================================================================================
# layout
# ======================================================================================
class _Layout:
    """Positions of the parts of a frame and the static image (title, legend,
    scale bar, map frame)."""

    def __init__(self, map_w, map_h, title, psize_m, crs_note, front_label, margin=10):
        m = self.m = margin
        W = max(map_w + 2 * m + 2, 560)                  # room for the legend at scale 2
        W += W % 2                                        # even sizes for the MP4 encoder
        inner = W - 2 * m
        self.W, self.inner = W, inner
        self.map_w, self.map_h = map_w, map_h
        self.psize_m = psize_m
        self.crs_note = crs_note
        self.front_label = front_label
        self.title, self.ts = fit_text(title or "Dam breach flood", inner, 2)
        longest = frame_times_label(-99 * 3600.0)
        self.tl = 3 if text_width(longest, 3) <= inner else (
            2 if text_width(longest, 2) <= inner else 1)
        self.scalebar = self._scalebar()
        self.ls = 2 if max(self._legend1(None, 2), self._legend2(None, 2)) <= inner else 1
        y = m
        self.title_y = y
        y += GLYPH_H * self.ts + 6
        self.label_y = y
        y += GLYPH_H * self.tl + 7
        self.bar_y, self.bar_h = y, 5
        y += self.bar_h + 9
        self.map_x = (W - map_w) // 2
        self.map_y = y
        y += map_h + 1 + 10
        self.leg1_y = y
        y += GLYPH_H * self.ls + 2 + 9
        self.leg2_y = y
        y += GLYPH_H * self.ls + 2 + 7
        self.note_y = y
        if crs_note:
            y += GLYPH_H + 4
        y += m - 3
        self.H = y + y % 2

    def _scalebar(self):
        real_w = self.map_w * self.psize_m
        if real_w * 0.25 >= 0.5 * MILE:
            L = _nice(real_w * 0.25 / MILE) * MILE
            lab = "{:g} MI".format(L / MILE)
        else:
            L = _nice(real_w * 0.25 / FT) * FT
            lab = "{:,.0f} FT".format(L / FT)
        return max(8, int(round(L / self.psize_m))), lab

    @staticmethod
    def _class_labels():
        b = list(DEPTH_CLASSES_FT) + [None]
        return ["{:g}-{:g}".format(b[i], b[i + 1]) if b[i + 1] is not None
                else "{:g}+".format(b[i]) for i in range(len(DEPTH_COLORS))]

    def _legend1(self, img, s):
        """Depth classes; draws when img is given. Returns the width."""
        x, y = self.m, getattr(self, "leg1_y", 0)
        if img is not None:
            draw_text(img, x, y + 1, "DEPTH (FT)", C_BLACK, s)
        x += text_width("DEPTH (FT)", s) + 5 * s
        for i, lab in enumerate(self._class_labels()):
            if img is not None:
                img[y:y + GLYPH_H * s + 2, x:x + 12 * s] = WATER0 + i * N_SHADE + 11
            x += 15 * s
            if img is not None:
                draw_text(img, x, y + 1, lab, C_BLACK, s)
            x += text_width(lab, s) + 6 * s
        return x - self.m - 6 * s

    def _legend2(self, img, s):
        """Scale bar, the arrival-front line and a north arrow."""
        x, y = self.m, getattr(self, "leg2_y", 0)
        px, lab = self.scalebar
        if img is not None:
            by = y + (GLYPH_H * s + 2 - 6) // 2
            img[by:by + 6, x:x + px] = C_BLACK
            q = px // 4
            for j in (1, 3):                                  # alternating segments
                img[by + 1:by + 5, x + j * q:x + min(px - 1, (j + 1) * q)] = C_WHITE
        x += px + 6
        if img is not None:
            draw_text(img, x, y + 1, lab, C_BLACK, s)
        x += text_width(lab, s) + 14 * s
        if self.front_label:
            if img is not None:
                mid = y + (GLYPH_H * s + 2) // 2
                img[mid - 1:mid + 1, x:x + 12 * s] = C_FRONT
            x += 15 * s
            if img is not None:
                draw_text(img, x, y + 1, self.front_label, C_BLACK, s)
            x += text_width(self.front_label, s) + 14 * s
        # north arrow, right-aligned
        aw = 4 * s + 1
        width = text_width("N", s) + 3 * s + aw
        if img is not None:
            nx0 = self.W - self.m - width
            draw_text(img, nx0, y + 1, "N", C_BLACK, s)
            cx = nx0 + text_width("N", s) + 3 * s + aw // 2
            h = GLYPH_H * s + 2
            for r in range(h):
                half = int(r * (aw // 2) / max(1, h - 1))
                img[y + r, cx - half:cx + half + 1] = C_BLACK
        return x - self.m + width

    def base(self):
        img = np.full((self.H, self.W), C_WHITE, np.uint8)
        draw_text(img, self.m, self.title_y, self.title, C_BLACK, self.ts)
        x0, y0 = self.map_x - 1, self.map_y - 1                # map frame
        x1, y1 = self.map_x + self.map_w, self.map_y + self.map_h
        img[y0, x0:x1 + 1] = C_FRAME
        img[y1, x0:x1 + 1] = C_FRAME
        img[y0:y1 + 1, x0] = C_FRAME
        img[y0:y1 + 1, x1] = C_FRAME
        self._legend1(img, self.ls)
        self._legend2(img, self.ls)
        if self.crs_note:
            t, sc = fit_text(self.crs_note, self.inner, 1)
            draw_text(img, self.m, self.note_y, t, C_SUBTEXT, sc)
        return img


def _triangle(img, cx, cy, size, fill, outline):
    """An upward triangle centred on (cx, cy)."""
    H, W = img.shape
    for col, grow in ((outline, 2), (fill, 0)):
        h = size + grow
        top = int(cy - 0.6 * size) - grow
        for r in range(h + 1):
            half = int(round(r * 0.6 * h / max(1, h)))
            yy = top + r
            if 0 <= yy < H:
                a, b = max(0, int(cx) - half), min(W, int(cx) + half + 1)
                if a < b:
                    img[yy, a:b] = col


# ======================================================================================
# the frames
# ======================================================================================
class AnimationResult:
    def __init__(self):
        self.out_dir = ""
        self.frames = []            # PNG paths
        self.gif = ""
        self.mp4 = ""
        self.geotiffs = []          # GeoTIFF paths (depth ft), one per frame
        self.index_json = ""        # time ranges of the GeoTIFFs
        self.times_s = []           # model time of each frame
        self.hours = []             # hours after breach initiation
        self.labels = []
        self.size = (0, 0)          # image (width, height) in pixels
        self.cells_per_px = 1.0
        self.window = None          # (r0, r1, c0, c1) of the model grid shown
        self.map_rect = None        # (x, y, width, height) of the map in the frames
        self.max_depth_ft = 0.0
        self.notes = []

    def as_dict(self):
        d = dict(self.__dict__)
        d["times_s"] = [float(t) for t in self.times_s]
        d["hours"] = [float(h) for h in self.hours]
        return d


def _crop_window(union, dam_rc=None, pad_frac=0.08, min_pad=3, min_ratio=0.35):
    ny, nx = union.shape
    if not union.any():
        return 0, ny, 0, nx
    rows = np.nonzero(union.any(axis=1))[0]
    cols = np.nonzero(union.any(axis=0))[0]
    r0, r1, c0, c1 = int(rows[0]), int(rows[-1]) + 1, int(cols[0]), int(cols[-1]) + 1
    if dam_rc is not None and 0 <= dam_rc[0] < ny and 0 <= dam_rc[1] < nx:
        r0, r1 = min(r0, dam_rc[0]), max(r1, dam_rc[0] + 1)
        c0, c1 = min(c0, dam_rc[1]), max(c1, dam_rc[1] + 1)
    pad = max(min_pad, int(round(pad_frac * max(r1 - r0, c1 - c0))))
    r0, r1, c0, c1 = r0 - pad, r1 + pad, c0 - pad, c1 + pad
    # not a sliver: the short side at least min_ratio of the long one
    h, w = r1 - r0, c1 - c0
    if h < min_ratio * w:
        g = int(math.ceil((min_ratio * w - h) / 2.0))
        r0, r1 = r0 - g, r1 + g
    elif w < min_ratio * h:
        g = int(math.ceil((min_ratio * h - w) / 2.0))
        c0, c1 = c0 - g, c1 + g
    return max(0, r0), min(ny, r1), max(0, c0), min(nx, c1)


def _scales(h, w, max_px, min_px=480):
    """(block size k, upsampling factor up): the map's longer side at most max_px."""
    longest = max(h, w)
    if longest > max_px:
        return int(math.ceil(longest / float(max_px))), 1
    up = max(1, min(max_px // longest, int(math.ceil(min(min_px, max_px) / float(longest)))))
    return 1, up


def _front(reached):
    """Edge pixels of the reached area (not drawn along the map edge)."""
    p = np.pad(reached, 1, mode="edge")
    inner = p[:-2, 1:-1] & p[2:, 1:-1] & p[1:-1, :-2] & p[1:-1, 2:]
    return reached & ~inner


def _clean_old(out_dir):
    for fn in os.listdir(out_dir):
        if re.match(r"^frame_\d+\.png$", fn) or re.match(r"^Depth T[+-][\d.]+h( \(\d+\))?\.tif$", fn):
            try:
                os.remove(os.path.join(out_dir, fn))
            except OSError:
                pass


def _no_window():
    if sys.platform.startswith("win"):
        return {"creationflags": 0x08000000}           # CREATE_NO_WINDOW
    return {}


def encode_mp4(frame_pattern, fps, out_path, start_number=1, ffmpeg=None):
    """Encode numbered PNG frames with ffmpeg. Returns (path or '', note)."""
    exe = ffmpeg or shutil.which("ffmpeg")
    if not exe:
        return "", "MP4 skipped: ffmpeg is not on the PATH (install it, or use the GIF)."
    base = [exe, "-y", "-loglevel", "error", "-framerate", "{:g}".format(fps),
            "-start_number", str(start_number), "-i", frame_pattern]
    err = ""
    for codec in (["-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "20",
                   "-movflags", "+faststart"],
                  ["-c:v", "mpeg4", "-q:v", "3", "-pix_fmt", "yuv420p"]):
        try:
            r = subprocess.run(base + codec + [out_path], stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, timeout=3600, **_no_window())
        except (OSError, subprocess.SubprocessError) as e:
            return "", "MP4 not written: {}".format(e)
        if r.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 0:
            return out_path, ""
        err = r.stderr.decode("utf-8", "replace").strip()[-300:]
    return "", "MP4 not written: ffmpeg failed: {}".format(err)


def render_run(npz_path, dem_grid, out_dir, t_ref_hr=0.0, title="", dam_xy=None, max_px=900,
               fps=4, make_gif=True, make_mp4=True, geotiff_writer=None, crs_note="",
               step=1, crop=True, front_ft=1.0, unit_m=1.0, keep_frames=True,
               progress=None, cancel=None, hold_last_s=2.0, write_sidecar=True):
    """Render the depth snapshots in `npz_path` over the DEM `dem_grid` (a Grid
    of elevations in metres on the snapshots' grid) into `out_dir`.

    t_ref_hr: breach initiation, hours into the model record (labels count from it).
    dam_xy: the dam in the grid's CRS (a marker). max_px: the map's longer side
    at most this many pixels (block maximum of the depth when the grid is larger).
    geotiff_writer(grid, path): also write one depth GeoTIFF (ft) per frame and
    'Depth frames.json' with their time ranges. step: use every step-th snapshot.
    crop: show only the part of the grid the flood reaches. front_ft: draw the
    edge of the area reached by this depth so far (None: no line). unit_m: metres
    per grid unit (the scale bar). Also records the grid next to the .npz
    ('.grid.json') unless write_sidecar is False. Returns an AnimationResult."""
    progress = progress or (lambda p, m="": None)
    cancel = cancel or (lambda: False)
    res = AnimationResult()
    res.out_dir = out_dir
    os.makedirs(out_dir, exist_ok=True)
    info = npz_info(npz_path)
    ny, nx = info["ny"], info["nx"]
    if dem_grid.data.shape != (ny, nx):
        raise ValueError("the DEM grid is {} but the snapshots are {}".format(
            dem_grid.data.shape, (ny, nx)))
    if info["n"] == 0:
        raise ValueError("the snapshot file holds no frames")
    if write_sidecar:
        try:
            write_grid_sidecar(npz_path, dem_grid, t_ref_hr, dam_xy, title)
        except OSError:
            pass
    step = max(1, int(step))
    times_all = info["times_s"]
    use = list(range(0, info["n"], step))
    times = times_all[use]
    n = len(use)
    _clean_old(out_dir)

    # pass 1: where the water ever goes, and how deep
    wet_m = WET_FT * FT
    union = np.zeros((ny, nx), bool)
    dmax = 0.0
    for j, (k, d) in enumerate(iter_depth_frames(npz_path, step)):
        if cancel():
            raise Cancelled("cancelled")
        w = np.isfinite(d) & (d > wet_m)
        union |= w
        if w.any():
            dmax = max(dmax, float(d[w].max()))
        progress(10.0 * (j + 1) / n, "reading the snapshots")
    res.max_depth_ft = round(dmax / FT, 2)
    if not union.any():
        res.notes.append("No water in any snapshot: the frames show the terrain only.")
    dam_rc = dem_grid.rc(*dam_xy) if dam_xy is not None else None
    r0, r1, c0, c1 = _crop_window(union, dam_rc) if crop else (0, ny, 0, nx)
    res.window = (r0, r1, c0, c1)
    k, up = _scales(r1 - r0, c1 - c0, int(max_px))
    res.cells_per_px = k / float(up)
    psize = dem_grid.dx * k / float(up)                    # grid units per pixel

    # static background
    z = dem_grid.masked()[r0:r1, c0:c1]
    zk = block_reduce(z, k, "mean")
    tone, nod = terrain_tone(zk, dem_grid.dx * k * unit_m)
    if up > 1:
        tone = np.repeat(np.repeat(tone, up, 0), up, 1)
        nod = np.repeat(np.repeat(nod, up, 0), up, 1)
    mh, mw = tone.shape
    terrain_idx = (GREY0 + np.round(tone * (N_GREY - 1))).astype(np.uint8)
    terrain_idx[nod] = C_NODATA
    shade_lvl = np.round(tone * (N_SHADE - 1)).astype(np.int64)
    front_label = "REACHED {:g} FT".format(front_ft) if front_ft else ""
    lay = _Layout(mw, mh, title, psize * unit_m, crs_note, front_label)
    base = lay.base()
    res.size = (lay.W, lay.H)
    res.map_rect = (lay.map_x, lay.map_y, mw, mh)
    dam_px = None
    if dam_xy is not None:
        px = (dam_xy[0] - (dem_grid.x0 + c0 * dem_grid.dx)) / psize
        py = ((dem_grid.y0 - r0 * dem_grid.dx) - dam_xy[1]) / psize
        if 0 <= px < mw and 0 <= py < mh:
            dam_px = (lay.map_x + px, lay.map_y + py)
        else:
            res.notes.append("The dam is outside the area shown.")

    # outputs that stream frame by frame
    digits = max(4, len(str(n)))
    delay = max(2, int(round(100.0 / max(0.1, float(fps)))))
    gif = None
    if make_gif:
        res.gif = os.path.join(out_dir, "Flood animation.gif")
        gif = GifWriter(res.gif, lay.W, lay.H)
    hours = [float(t) / 3600.0 - float(t_ref_hr or 0.0) for t in times]
    tif_names = geotiff_names(hours) if geotiff_writer is not None else []
    win = dem_grid.window(r0, r1, c0, c1)
    reached = np.zeros((mh, mw), bool)
    t_first, t_last = float(times[0]), float(times[-1])
    span = max(t_last - t_first, 1e-9)
    ref_s = float(t_ref_hr or 0.0) * 3600.0
    try:
        for j, (kk, d) in enumerate(iter_depth_frames(npz_path, step)):
            if cancel():
                raise Cancelled("cancelled")
            t = float(times[j])
            dw = d[r0:r1, c0:c1]
            d_ft = block_reduce(dw, k, "max") / FT
            if up > 1:
                d_ft = np.repeat(np.repeat(d_ft, up, 0), up, 1)
            cls, wet = depth_class(d_ft)
            mp = np.where(wet, WATER0 + cls * N_SHADE + shade_lvl, terrain_idx).astype(np.uint8)
            if front_ft:
                reached |= wet & (d_ft >= front_ft)
                mp[_front(reached)] = C_FRONT
            img = base.copy()
            img[lay.map_y:lay.map_y + mh, lay.map_x:lay.map_x + mw] = mp
            if dam_px is not None:
                _triangle(img, dam_px[0], dam_px[1], 5 + 3 * lay.ts, C_BLACK, C_WHITE)
            label = frame_times_label(t, t_ref_hr)
            draw_text(img, lay.m, lay.label_y, label, C_BLACK, lay.tl)
            bx0, bx1 = lay.m, lay.W - lay.m
            by = lay.bar_y
            img[by:by + lay.bar_h, bx0:bx1] = C_BAR_BG
            fill = int(round((bx1 - bx0) * (t - t_first) / span)) if n > 1 else bx1 - bx0
            img[by:by + lay.bar_h, bx0:bx0 + fill] = C_BAR_FG
            if t_first <= ref_s <= t_last and n > 1:
                tx = bx0 + int(round((bx1 - bx0 - 1) * (ref_s - t_first) / span))
                img[by - 2:by + lay.bar_h + 2, max(bx0, tx - 1):tx + 1] = C_RED
            rgb = PALETTE[img]
            p = os.path.join(out_dir, "frame_{:0{w}d}.png".format(j + 1, w=digits))
            write_png(p, rgb)
            res.frames.append(p)
            if gif is not None:
                last = j == n - 1
                gif.add(img, max(delay, int(round(hold_last_s * 100))) if last else delay)
            if geotiff_writer is not None:
                g = win.like(np.where(np.isfinite(dw) & (dw > wet_m), dw / FT, np.nan),
                             nodata=np.nan)
                tp = os.path.join(out_dir, tif_names[j])
                geotiff_writer(g, tp)
                res.geotiffs.append(tp)
            res.times_s.append(t)
            res.hours.append(hours[j])
            res.labels.append(label)
            progress(10.0 + 80.0 * (j + 1) / n, "frame {} of {}".format(j + 1, n))
    finally:
        if gif is not None:
            gif.close()
    if len(res.frames) != n:
        raise ValueError("read {} frames, expected {}".format(len(res.frames), n))

    if geotiff_writer is not None:
        res.index_json = write_frame_index(out_dir, [os.path.basename(p) for p in res.geotiffs],
                                           hours, times, t_ref_hr)
    if make_mp4:
        progress(92.0, "MP4 (ffmpeg)")
        mp4, note = encode_mp4(os.path.join(out_dir, "frame_%0{}d.png".format(digits)), fps,
                             os.path.join(out_dir, "Flood animation.mp4"))
        res.mp4 = mp4
        if note:
            res.notes.append(note)
    if not keep_frames and (res.gif or res.mp4):
        for p in res.frames:
            try:
                os.remove(p)
            except OSError:
                pass
        res.frames = []
    progress(100.0, "animation done")
    return res


def write_frame_index(out_dir, names, hours, times_s, t_ref_hr=0.0):
    """'Depth frames.json': each GeoTIFF's time range in hours after breach
    initiation (start inclusive, end exclusive; the last frame lasts one
    snapshot interval), for fixed temporal ranges in QGIS."""
    hours = [float(h) for h in hours]
    if len(hours) > 1:
        dt = float(np.median(np.diff(hours)))
    else:
        dt = 0.25
    frames = []
    for i, (nm, h) in enumerate(zip(names, hours)):
        end = hours[i + 1] if i + 1 < len(hours) else h + dt
        frames.append({"path": nm, "start_hr": h, "end_hr": end,
                       "start_iso": iso_hours(h), "end_iso": iso_hours(end),
                       "model_time_s": float(times_s[i])})
    idx = {"what": "LISFLOOD-FP depth snapshots (ft), one GeoTIFF per frame",
           "time_reference": "hours after breach initiation",
           "t_ref_hr": float(t_ref_hr or 0.0), "units": "ft",
           "interval_hr": dt, "frames": frames}
    p = os.path.join(out_dir, "Depth frames.json")
    with open(p, "w") as f:
        json.dump(idx, f, indent=1)
    return p


def temporal_ranges(index_path, base):
    """[(GeoTIFF path, start datetime, end datetime)] from a frame index and the
    date-time that stands for breach initiation."""
    with open(index_path) as f:
        idx = json.load(f)
    folder = os.path.dirname(index_path)
    out = []
    for fr in idx["frames"]:
        p = fr["path"] if os.path.isabs(fr["path"]) else os.path.join(folder, fr["path"])
        out.append((p, base + datetime.timedelta(hours=fr["start_hr"]),
                    base + datetime.timedelta(hours=fr["end_hr"])))
    return out
