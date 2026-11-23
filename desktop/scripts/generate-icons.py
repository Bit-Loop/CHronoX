#!/usr/bin/env python3
"""ChronoX Finance — Deterministic Zero-Dependency Cross-Platform Icon Pipeline.

Generates first-party Apple/Raycast-grade ChronoX Finance brand assets in
`desktop/build/icons/` using ONLY the Python 3 standard library (`math`, `struct`,
`zlib`, `pathlib`) so builds are 100% reproducible on Linux, Windows, and macOS
with zero native graphics library requirements.

Outputs:
  - icon.svg                Vector master (512x512 continuous superellipse)
  - icon.png                512x512 RGBA master PNG
  - 16x16.png .. 512x512.png Multi-size Linux/hicolor RGBA PNG set
  - icon.ico                Multi-resolution Windows icon (16/32/48/64/128/256)
  - icon.icns               Multi-resolution macOS icon (16/32/64/128/256/512)
  - tray-dark.png           32x32 monochrome light glyph (#F1F5F9) for dark panels
  - tray-light.png          32x32 monochrome dark glyph (#0F172A) for light panels
  - trayTemplate.png        22x22 pure black (#000000) macOS template glyph
  - trayTemplate@2x.png     44x44 pure black (#000000) macOS Retina template glyph
"""

from __future__ import annotations

import math
import struct
import sys
import zlib
from pathlib import Path
from typing import Callable, List, Sequence, Tuple

# ---------------------------------------------------------------------------
# Shared ChronoX Finance Geometric Constants (normalized [-1, 1] space)
# ---------------------------------------------------------------------------
SQUIRCLE_HALF = 0.88       # ~6% optical margin around Apple continuous squircle
SQUIRCLE_EXP = 4.5         # Smooth G2 continuous superellipse exponent

ARC_RADIUS = 0.48
ARC_GAP_ANGLE = -math.pi / 4.0      # -45 deg (1:30 upper-right momentum opening)
ARC_GAP_HALF = math.radians(33.0)   # 66 deg opening -> 294 deg orbital sweep
ARC_HALF_THICK = 0.060

BEACON_RADIUS = 0.068
BEACON_X = ARC_RADIUS * math.cos(ARC_GAP_ANGLE)
BEACON_Y = ARC_RADIUS * math.sin(ARC_GAP_ANGLE)

HAND_TOP = (0.0, -0.255)            # 12 o'clock upright chronograph hand
HAND_END = (0.205, 0.132)           # ~4:10 forward momentum hand
HAND_HALF_THICK = 0.046

PIVOT_OUTER_R = 0.064
PIVOT_INNER_R = 0.030

DIAL_RING_R = 0.655
DIAL_RING_HALF_THICK = 0.0065

# Precomputed trig for rotated arc symmetry (gap centered along +x)
_COS_GAP = math.cos(ARC_GAP_ANGLE)
_SIN_GAP = math.sin(ARC_GAP_ANGLE)
_ARC_EX = ARC_RADIUS * math.cos(ARC_GAP_HALF)
_ARC_EY = ARC_RADIUS * math.sin(ARC_GAP_HALF)

RGBA = Tuple[float, float, float, float]


def clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return lo if v < lo else (hi if v > hi else v)


def smoothstep(edge0: float, edge1: float, x: float) -> float:
    t = clamp((x - edge0) / (edge1 - edge0))
    return t * t * (3.0 - 2.0 * t)


def lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def lerp_rgb(
    c0: Tuple[float, float, float],
    c1: Tuple[float, float, float],
    t: float,
) -> Tuple[float, float, float]:
    t = clamp(t)
    return (
        c0[0] + (c1[0] - c0[0]) * t,
        c0[1] + (c1[1] - c0[1]) * t,
        c0[2] + (c1[2] - c0[2]) * t,
    )


def over_premul(
    dst_r: float, dst_g: float, dst_b: float, dst_a: float,
    src_r: float, src_g: float, src_b: float, src_a: float,
) -> RGBA:
    """Porter-Duff source-over compositing in premultiplied RGB + alpha."""
    if src_a <= 0.0:
        return dst_r, dst_g, dst_b, dst_a
    inv = 1.0 - src_a
    return (
        src_r * src_a + dst_r * inv,
        src_g * src_a + dst_g * inv,
        src_b * src_a + dst_b * inv,
        src_a + dst_a * inv,
    )


def sd_squircle(x: float, y: float, radius: float = SQUIRCLE_HALF, n: float = SQUIRCLE_EXP) -> float:
    """Gradient-normalized Euclidean signed distance to a continuous superellipse."""
    ax = abs(x)
    ay = abs(y)
    if ax < 1e-9 and ay < 1e-9:
        return -radius
    ax_n1 = ax ** (n - 1.0)
    ay_n1 = ay ** (n - 1.0)
    an = ax_n1 * ax + ay_n1 * ay
    val = an ** (1.0 / n)
    scale = val / an
    grad = math.hypot(scale * ax_n1, scale * ay_n1)
    return (val - radius) / max(grad, 1e-6)


def sd_arc(
    x: float,
    y: float,
    radius: float,
    ex: float,
    ey: float,
    half_thick: float,
) -> float:
    """Exact SDF for a round-capped circular arc symmetric across ARC_GAP_ANGLE."""
    rx = x * _COS_GAP + y * _SIN_GAP
    ry = abs(-x * _SIN_GAP + y * _COS_GAP)
    if rx * ey - ry * ex <= 0.0:
        dist = abs(math.hypot(rx, ry) - radius)
    else:
        dist = math.hypot(rx - ex, ry - ey)
    return dist - half_thick


def sd_segment(
    x: float,
    y: float,
    ax: float,
    ay: float,
    bx: float,
    by: float,
    half_thick: float,
) -> float:
    """Exact SDF for a round-capped line segment."""
    pax = x - ax
    pay = y - ay
    bax = bx - ax
    bay = by - ay
    denom = bax * bax + bay * bay
    h = clamp((pax * bax + pay * bay) / denom) if denom > 0.0 else 0.0
    return math.hypot(pax - bax * h, pay - bay * h) - half_thick


def brand_gradient(x: float, y: float) -> Tuple[float, float, float]:
    """ChronoX emerald -> teal -> luminous sky-cyan diagonal brand gradient."""
    # Diagonal from bottom-left (-0.6, +0.6) to top-right (+0.6, -0.6)
    t = clamp((x - y) * 0.72 + 0.50)
    if t < 0.45:
        return lerp_rgb((16.0, 185.0, 129.0), (52.0, 211.0, 153.0), t / 0.45)
    if t < 0.76:
        return lerp_rgb((52.0, 211.0, 153.0), (45.0, 212.0, 191.0), (t - 0.45) / 0.31)
    return lerp_rgb((45.0, 212.0, 191.0), (56.0, 189.0, 248.0), (t - 0.76) / 0.24)


def hand_gradient(x: float, y: float) -> Tuple[float, float, float]:
    """Bright mint-white to calm teal gradient for high-legibility inner clock hands."""
    t = clamp((y + 0.28) / 0.46)
    return lerp_rgb((240.0, 253.0, 250.0), (110.0, 231.0, 183.0), t)


# ---------------------------------------------------------------------------
# App Icon Analytic SDF Sampler
# ---------------------------------------------------------------------------
def sample_app_icon(
    x: float,
    y: float,
    px_width: float,
    small_optical_boost: float = 0.0,
    show_micro_ticks: bool = True,
) -> RGBA:
    """Evaluate premultiplied RGBA color (0..255 RGB, 0..1 A) at normalized (x, y)."""
    aa = max(px_width * 0.72, 1e-4)
    pr = pg = pb = pa = 0.0

    # 1. Soft grounded shadow under the squircle (fits cleanly within 6% margin)
    d_shadow = sd_squircle(x, y - 0.032, SQUIRCLE_HALF * 0.992)
    if d_shadow < 0.11:
        shadow_a = 0.34 * math.exp(-max(d_shadow, 0.0) * 26.0) * smoothstep(0.11, -0.01, d_shadow)
        if shadow_a > 0.002:
            pr, pg, pb, pa = over_premul(pr, pg, pb, pa, 2.0, 5.0, 12.0, shadow_a)

    # 2. Main obsidian midnight squircle body
    d_body = sd_squircle(x, y, SQUIRCLE_HALF)
    body_a = clamp(0.5 - d_body / aa)
    if body_a > 0.0:
        # Base vertical-diagonal midnight obsidian gradient
        bg_t = clamp((y + 0.88) / 1.76 * 0.72 + (x + 0.88) / 1.76 * 0.28)
        if bg_t < 0.55:
            br, bg_c, bb = lerp_rgb((21.0, 32.0, 52.0), (10.0, 16.0, 29.0), bg_t / 0.55)
        else:
            br, bg_c, bb = lerp_rgb((10.0, 16.0, 29.0), (5.0, 9.0, 17.0), (bg_t - 0.55) / 0.45)

        # Ambient radial emerald-teal core aura behind the orbital emblem
        r_center = math.hypot(x * 0.96, (y + 0.03) * 0.96)
        glow_em = math.exp(-r_center * r_center * 3.8) * 0.26
        br += 52.0 * glow_em
        bg_c += 211.0 * glow_em
        bb += 153.0 * glow_em

        # Upper-right luminous cyan atmospheric kiss near beacon
        r_sky = math.hypot(x - 0.44, y + 0.46)
        glow_sky = math.exp(-r_sky * r_sky * 3.6) * 0.19
        br += 56.0 * glow_sky
        bg_c += 189.0 * glow_sky
        bb += 248.0 * glow_sky

        # Subtle bottom-left deep indigo balance glow
        r_ind = math.hypot(x + 0.52, y - 0.50)
        glow_ind = math.exp(-r_ind * r_ind * 3.2) * 0.08
        br += 99.0 * glow_ind
        bg_c += 102.0 * glow_ind
        bb += 241.0 * glow_ind

        # Subtle recessed chronograph medallion dial
        r_dial = math.hypot(x, y)
        if r_dial < DIAL_RING_R + 0.03:
            med_a = clamp(0.5 - (r_dial - DIAL_RING_R) / aa) * 0.28
            br = lerp(br, 6.0, med_a)
            bg_c = lerp(bg_c, 11.0, med_a)
            bb = lerp(bb, 21.0, med_a)

        # Subtle top glass specular sweep
        spec = smoothstep(0.15, -0.88, y) * 0.065
        br += 255.0 * spec
        bg_c += 255.0 * spec
        bb += 255.0 * spec

        br = min(br, 255.0)
        bg_c = min(bg_c, 255.0)
        bb = min(bb, 255.0)
        pr, pg, pb, pa = over_premul(pr, pg, pb, pa, br, bg_c, bb, body_a)

        # 3. Inner precision glass hairline rim (top-lit specular -> emerald accent)
        rim_width = 0.022 + small_optical_boost * 0.012
        d_rim = abs(d_body + rim_width * 0.55) - rim_width * 0.5
        rim_cov = clamp(0.5 - d_rim / aa) * body_a
        if rim_cov > 0.002:
            top_factor = smoothstep(0.65, -0.85, y)
            rim_r, rim_g, rim_b = lerp_rgb((52.0, 211.0, 153.0), (226.0, 238.0, 255.0), top_factor * 0.72)
            rim_alpha = lerp(0.12, 0.28, top_factor) * rim_cov
            pr, pg, pb, pa = over_premul(pr, pg, pb, pa, rim_r, rim_g, rim_b, rim_alpha)

        # 4. Delicate chronograph bezel ring + 4 cardinal ticks (visible at >=48px)
        if show_micro_ticks:
            d_ring = abs(r_dial - DIAL_RING_R) - DIAL_RING_HALF_THICK
            ring_a = clamp(0.5 - d_ring / aa) * 0.16 * body_a
            if ring_a > 0.002:
                pr, pg, pb, pa = over_premul(pr, pg, pb, pa, 148.0, 163.0, 184.0, ring_a)

            # 4 crisp cardinal micro-ticks at 12, 3, 6, 9 o'clock
            d_ticks = min(
                sd_segment(x, y, 0.0, -0.685, 0.0, -0.625, 0.011),
                sd_segment(x, y, 0.685, 0.0, 0.625, 0.0, 0.011),
                sd_segment(x, y, 0.0, 0.685, 0.0, 0.625, 0.011),
                sd_segment(x, y, -0.685, 0.0, -0.625, 0.0, 0.011),
            )
            tick_a = clamp(0.5 - d_ticks / aa) * 0.34 * body_a
            if tick_a > 0.002:
                pr, pg, pb, pa = over_premul(pr, pg, pb, pa, 203.0, 213.0, 225.0, tick_a)

        # 5. Soft self-luminous halo beneath the orbital arc & beacon
        arc_ht = ARC_HALF_THICK + small_optical_boost * 0.020
        d_arc = sd_arc(x, y, ARC_RADIUS, _ARC_EX, _ARC_EY, arc_ht)
        beacon_r = BEACON_RADIUS + small_optical_boost * 0.016
        d_beacon = math.hypot(x - BEACON_X, y - BEACON_Y) - beacon_r

        d_emblem = min(d_arc, d_beacon)
        if d_emblem < 0.22:
            halo_a = 0.28 * math.exp(-max(d_emblem, 0.0) * 14.0) * body_a
            if halo_a > 0.002:
                gr, gg, gb = brand_gradient(x, y)
                pr, pg, pb, pa = over_premul(pr, pg, pb, pa, gr, gg, gb, halo_a)

        # 6. Primary ChronoX Luminous Orbital Clock-Arc
        arc_a = clamp(0.5 - d_arc / aa) * body_a
        if arc_a > 0.002:
            ar, ag, ab = brand_gradient(x, y)
            # Subtle inner specular lift near arc centerline for dimensional polish
            center_lift = clamp(1.0 + d_arc / max(arc_ht, 1e-4)) * 0.14
            ar = min(255.0, ar + (255.0 - ar) * center_lift)
            ag = min(255.0, ag + (255.0 - ag) * center_lift)
            ab = min(255.0, ab + (255.0 - ab) * center_lift)
            pr, pg, pb, pa = over_premul(pr, pg, pb, pa, ar, ag, ab, arc_a)

        # 7. Upper-Right Luminous Cyan Beacon Node
        beacon_a = clamp(0.5 - d_beacon / aa) * body_a
        if beacon_a > 0.002:
            core_t = clamp(-d_beacon / max(beacon_r, 1e-4))
            nr, ng, nb = lerp_rgb((56.0, 189.0, 248.0), (224.0, 242.0, 254.0), core_t * 0.65)
            pr, pg, pb, pa = over_premul(pr, pg, pb, pa, nr, ng, nb, beacon_a)

        # 8. Precision Inner Clock Hands (12 o'clock -> center -> 4:10)
        hand_ht = HAND_HALF_THICK + small_optical_boost * 0.018
        d_hand1 = sd_segment(x, y, 0.0, 0.0, HAND_TOP[0], HAND_TOP[1], hand_ht)
        d_hand2 = sd_segment(x, y, 0.0, 0.0, HAND_END[0], HAND_END[1], hand_ht)
        d_hands = min(d_hand1, d_hand2)
        hands_a = clamp(0.5 - d_hands / aa) * body_a
        if hands_a > 0.002:
            hr, hg, hb = hand_gradient(x, y)
            pr, pg, pb, pa = over_premul(pr, pg, pb, pa, hr, hg, hb, hands_a)

        # 9. Precision Center Pivot Collar & Bright Pin
        r_origin = math.hypot(x, y)
        d_pivot_outer = r_origin - (PIVOT_OUTER_R + small_optical_boost * 0.012)
        piv_a = clamp(0.5 - d_pivot_outer / aa) * body_a
        if piv_a > 0.002:
            pr, pg, pb, pa = over_premul(pr, pg, pb, pa, 45.0, 212.0, 191.0, piv_a)

        d_pivot_inner = r_origin - (PIVOT_INNER_R + small_optical_boost * 0.006)
        pin_a = clamp(0.5 - d_pivot_inner / aa) * body_a
        if pin_a > 0.002:
            pr, pg, pb, pa = over_premul(pr, pg, pb, pa, 240.0, 253.0, 250.0, pin_a)

    return pr, pg, pb, pa


# ---------------------------------------------------------------------------
# Monochrome Tray Icon Analytic SDF Sampler
# ---------------------------------------------------------------------------
_TRAY_ARC_R = 0.72
_TRAY_ARC_EX = _TRAY_ARC_R * math.cos(ARC_GAP_HALF)
_TRAY_ARC_EY = _TRAY_ARC_R * math.sin(ARC_GAP_HALF)
_TRAY_BEACON_X = _TRAY_ARC_R * math.cos(ARC_GAP_ANGLE)
_TRAY_BEACON_Y = _TRAY_ARC_R * math.sin(ARC_GAP_ANGLE)


def make_tray_sampler(rgb: Tuple[float, float, float]) -> Callable[[float, float, float], RGBA]:
    """Return a monochrome ChronoX emblem sampler for crisp OS tray / menu bar icons."""
    cr, cg, cb = rgb

    def sample(x: float, y: float, px_width: float) -> RGBA:
        aa = max(px_width * 0.72, 1e-4)
        d_arc = sd_arc(x, y, _TRAY_ARC_R, _TRAY_ARC_EX, _TRAY_ARC_EY, 0.118)
        d_beacon = math.hypot(x - _TRAY_BEACON_X, y - _TRAY_BEACON_Y) - 0.128
        d_h1 = sd_segment(x, y, 0.0, 0.0, 0.0, -0.37, 0.102)
        d_h2 = sd_segment(x, y, 0.0, 0.0, 0.29, 0.19, 0.102)
        d_pivot = math.hypot(x, y) - 0.125
        d = min(d_arc, d_beacon, d_h1, d_h2, d_pivot)
        a = clamp(0.5 - d / aa)
        return cr * a, cg * a, cb * a, a

    return sample


# ---------------------------------------------------------------------------
# Rasterization, Box Downsampling, PNG / ICO / ICNS Encoders
# ---------------------------------------------------------------------------
def render_rgba_buffer(
    size: int,
    sampler: Callable[[float, float, float], RGBA],
    supersample: int = 1,
) -> bytearray:
    """Render an RGBA8888 buffer (unpremultiplied bytes) of `size x size`."""
    buf = bytearray(size * size * 4)
    inv_size = 2.0 / size
    px_width = inv_size
    inv_ss = 1.0 / supersample
    inv_ss2 = inv_ss * inv_ss

    offsets = [((s + 0.5) * inv_ss - 0.5) * inv_size for s in range(supersample)]

    idx = 0
    for py in range(size):
        cy = (py + 0.5) * inv_size - 1.0
        for px in range(size):
            cx = (px + 0.5) * inv_size - 1.0
            if supersample == 1:
                pr, pg, pb, pa = sampler(cx, cy, px_width)
            else:
                sr = sg = sb = sa = 0.0
                for oy in offsets:
                    yy = cy + oy
                    for ox in offsets:
                        r, g, b, a = sampler(cx + ox, yy, px_width * inv_ss)
                        sr += r
                        sg += g
                        sb += b
                        sa += a
                pr = sr * inv_ss2
                pg = sg * inv_ss2
                pb = sb * inv_ss2
                pa = sa * inv_ss2

            if pa > 1e-5:
                inv_a = 1.0 / pa
                buf[idx] = int(clamp(pr * inv_a, 0.0, 255.0) + 0.5)
                buf[idx + 1] = int(clamp(pg * inv_a, 0.0, 255.0) + 0.5)
                buf[idx + 2] = int(clamp(pb * inv_a, 0.0, 255.0) + 0.5)
                buf[idx + 3] = int(clamp(pa * 255.0, 0.0, 255.0) + 0.5)
            else:
                buf[idx] = buf[idx + 1] = buf[idx + 2] = buf[idx + 3] = 0
            idx += 4
    return buf


def downsample_2x_premul(buf: bytearray, size: int) -> bytearray:
    """Exact 2x2 area-averaging box downsample in premultiplied RGBA space."""
    half = size // 2
    out = bytearray(half * half * 4)
    row_stride = size * 4
    dst = 0
    for y in range(half):
        r0 = (y * 2) * row_stride
        r1 = r0 + row_stride
        for x in range(half):
            c0 = r0 + x * 8
            c1 = c0 + 4
            c2 = r1 + x * 8
            c3 = c2 + 4

            a0, a1, a2, a3 = buf[c0 + 3], buf[c1 + 3], buf[c2 + 3], buf[c3 + 3]
            a_sum = a0 + a1 + a2 + a3
            if a_sum > 0:
                pr = buf[c0] * a0 + buf[c1] * a1 + buf[c2] * a2 + buf[c3] * a3
                pg = buf[c0 + 1] * a0 + buf[c1 + 1] * a1 + buf[c2 + 1] * a2 + buf[c3 + 1] * a3
                pb = buf[c0 + 2] * a0 + buf[c1 + 2] * a1 + buf[c2 + 2] * a2 + buf[c3 + 2] * a3
                out[dst] = min(255, (pr + a_sum // 2) // a_sum)
                out[dst + 1] = min(255, (pg + a_sum // 2) // a_sum)
                out[dst + 2] = min(255, (pb + a_sum // 2) // a_sum)
                out[dst + 3] = min(255, (a_sum + 2) // 4)
            dst += 4
    return out


def encode_png(width: int, height: int, rgba: bytes | bytearray) -> bytes:
    """Encode an RGBA8888 pixel buffer into a standards-compliant PNG byte stream."""
    def chunk(tag: bytes, payload: bytes) -> bytes:
        crc = zlib.crc32(tag)
        crc = zlib.crc32(payload, crc) & 0xFFFFFFFF
        return struct.pack(">I", len(payload)) + tag + payload + struct.pack(">I", crc)

    raw = bytearray()
    stride = width * 4
    for y in range(height):
        raw.append(0)  # Filter type 0 (None)
        start = y * stride
        raw.extend(rgba[start : start + stride])

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    idat = zlib.compress(bytes(raw), level=9)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", idat)
        + chunk(b"IEND", b"")
    )


def encode_ico_dib(size: int, rgba: bytes | bytearray) -> bytes:
    """Encode an RGBA image as a 32-bit BGRA ICO DIB (BITMAPINFOHEADER + pixels + AND mask)."""
    mask_row_bytes = ((size + 31) // 32) * 4
    pixel_bytes_len = size * size * 4
    mask_bytes_len = mask_row_bytes * size

    header = struct.pack(
        "<IIIHHIIIIII",
        40,                         # biSize
        size,                       # biWidth
        size * 2,                   # biHeight (XOR + AND masks combined)
        1,                          # biPlanes
        32,                         # biBitCount (BGRA)
        0,                          # biCompression (BI_RGB)
        pixel_bytes_len + mask_bytes_len,
        0,
        0,
        0,
        0,
    )

    pixels = bytearray(pixel_bytes_len)
    mask = bytearray(mask_bytes_len)
    stride = size * 4

    p_idx = 0
    for row_out in range(size):
        src_y = size - 1 - row_out  # Bottom-up DIB scanline order
        src_row = src_y * stride
        mask_row = row_out * mask_row_bytes
        for x in range(size):
            i = src_row + x * 4
            r, g, b, a = rgba[i], rgba[i + 1], rgba[i + 2], rgba[i + 3]
            pixels[p_idx] = b
            pixels[p_idx + 1] = g
            pixels[p_idx + 2] = r
            pixels[p_idx + 3] = a
            p_idx += 4
            if a < 16:
                mask[mask_row + (x >> 3)] |= 1 << (7 - (x & 7))

    return header + bytes(pixels) + bytes(mask)


def encode_ico(entries: Sequence[Tuple[int, bytes, bytearray]]) -> bytes:
    """Build a multi-resolution Windows .ico (DIB for <=128px, PNG for 256px)."""
    count = len(entries)
    dir_entries: List[bytes] = []
    payloads: List[bytes] = []
    offset = 6 + count * 16

    for size, png_bytes, rgba in entries:
        data = png_bytes if size >= 256 else encode_ico_dib(size, rgba)
        dim_byte = 0 if size >= 256 else size
        dir_entries.append(
            struct.pack("<BBBBHHII", dim_byte, dim_byte, 0, 0, 1, 32, len(data), offset)
        )
        payloads.append(data)
        offset += len(data)

    return struct.pack("<HHH", 0, 1, count) + b"".join(dir_entries) + b"".join(payloads)


def encode_icns(png_by_size: dict[int, bytes]) -> bytes:
    """Build a modern macOS .icns container wrapping PNG chunks (16..512px)."""
    os_types = [
        (16, b"icp4"),
        (32, b"icp5"),
        (64, b"icp6"),
        (128, b"ic07"),
        (256, b"ic08"),
        (512, b"ic09"),
    ]
    chunks: List[bytes] = []
    for size, ostype in os_types:
        png = png_by_size[size]
        chunks.append(ostype + struct.pack(">I", 8 + len(png)) + png)
    body = b"".join(chunks)
    return b"icns" + struct.pack(">I", 8 + len(body)) + body


# ---------------------------------------------------------------------------
# Vector SVG Master Generator (Matches Analytic SDF Geometry 1:1)
# ---------------------------------------------------------------------------
def build_squircle_svg_path(half_px: float, center: float = 256.0, n: float = SQUIRCLE_EXP, segs: int = 24) -> str:
    """Generate a C1-smooth cubic Bezier SVG path matching the exact superellipse."""
    def pt_and_deriv(theta: float) -> Tuple[float, float, float, float]:
        c = math.cos(theta)
        s = math.sin(theta)
        ac = abs(c)
        as_ = abs(s)
        f = ac**n + as_**n
        r = f ** (-1.0 / n)
        # df/dtheta = n * (|c|^(n-1)*(-s)*sgn(c) + |s|^(n-1)*(c)*sgn(s))
        sc = 1.0 if c >= 0.0 else -1.0
        ss = 1.0 if s >= 0.0 else -1.0
        df = n * (-(ac ** (n - 1.0)) * s * sc + (as_ ** (n - 1.0)) * c * ss)
        dr = (-1.0 / n) * (f ** (-1.0 / n - 1.0)) * df
        x = center + half_px * r * c
        y = center + half_px * r * s
        dx = half_px * (dr * c - r * s)
        dy = half_px * (dr * s + r * c)
        return x, y, dx, dy

    dt = (2.0 * math.pi) / segs
    pts = [pt_and_deriv(k * dt) for k in range(segs)]
    cmds = [f"M {pts[0][0]:.2f} {pts[0][1]:.2f}"]
    scale = dt / 3.0
    for k in range(segs):
        x0, y0, dx0, dy0 = pts[k]
        x1, y1, dx1, dy1 = pts[(k + 1) % segs]
        c1x, c1y = x0 + dx0 * scale, y0 + dy0 * scale
        c2x, c2y = x1 - dx1 * scale, y1 - dy1 * scale
        cmds.append(f"C {c1x:.2f} {c1y:.2f} {c2x:.2f} {c2y:.2f} {x1:.2f} {y1:.2f}")
    cmds.append("Z")
    return " ".join(cmds)


def build_icon_svg() -> str:
    """Create the first-party 512x512 ChronoX Finance vector squircle icon SVG."""
    outer_path = build_squircle_svg_path(SQUIRCLE_HALF * 256.0)
    rim_path = build_squircle_svg_path((SQUIRCLE_HALF - 0.011) * 256.0)

    arc_r = ARC_RADIUS * 256.0
    start_a = ARC_GAP_ANGLE + ARC_GAP_HALF
    end_a = ARC_GAP_ANGLE - ARC_GAP_HALF + 2.0 * math.pi
    ax0 = 256.0 + arc_r * math.cos(start_a)
    ay0 = 256.0 + arc_r * math.sin(start_a)
    ax1 = 256.0 + arc_r * math.cos(end_a)
    ay1 = 256.0 + arc_r * math.sin(end_a)

    bx = 256.0 + BEACON_X * 256.0
    by = 256.0 + BEACON_Y * 256.0
    br = BEACON_RADIUS * 256.0

    hx0 = 256.0 + HAND_TOP[0] * 256.0
    hy0 = 256.0 + HAND_TOP[1] * 256.0
    hx1 = 256.0 + HAND_END[0] * 256.0
    hy1 = 256.0 + HAND_END[1] * 256.0

    dial_r = DIAL_RING_R * 256.0

    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="512" height="512" viewBox="0 0 512 512" fill="none">
  <defs>
    <linearGradient id="cxBg" x1="76" y1="36" x2="436" y2="476" gradientUnits="userSpaceOnUse">
      <stop offset="0%" stop-color="#152034" />
      <stop offset="54%" stop-color="#0a101d" />
      <stop offset="100%" stop-color="#050911" />
    </linearGradient>
    <radialGradient id="cxAuraEmerald" cx="256" cy="248" r="196" gradientUnits="userSpaceOnUse">
      <stop offset="0%" stop-color="#34d399" stop-opacity="0.28" />
      <stop offset="55%" stop-color="#2dd4bf" stop-opacity="0.09" />
      <stop offset="100%" stop-color="#0a101d" stop-opacity="0" />
    </radialGradient>
    <radialGradient id="cxAuraSky" cx="368" cy="138" r="156" gradientUnits="userSpaceOnUse">
      <stop offset="0%" stop-color="#38bdf8" stop-opacity="0.20" />
      <stop offset="100%" stop-color="#0a101d" stop-opacity="0" />
    </radialGradient>
    <linearGradient id="cxRim" x1="256" y1="32" x2="256" y2="480" gradientUnits="userSpaceOnUse">
      <stop offset="0%" stop-color="#e2eeff" stop-opacity="0.28" />
      <stop offset="48%" stop-color="#34d399" stop-opacity="0.18" />
      <stop offset="100%" stop-color="#94a3b8" stop-opacity="0.10" />
    </linearGradient>
    <linearGradient id="cxBrandArc" x1="120" y1="392" x2="392" y2="120" gradientUnits="userSpaceOnUse">
      <stop offset="0%" stop-color="#10b981" />
      <stop offset="45%" stop-color="#34d399" />
      <stop offset="76%" stop-color="#2dd4bf" />
      <stop offset="100%" stop-color="#38bdf8" />
    </linearGradient>
    <linearGradient id="cxHands" x1="256" y1="184" x2="310" y2="296" gradientUnits="userSpaceOnUse">
      <stop offset="0%" stop-color="#f0fdfa" />
      <stop offset="100%" stop-color="#6ee7b7" />
    </linearGradient>
    <radialGradient id="cxBeacon" cx="{bx - 4:.1f}" cy="{by - 4:.1f}" r="{br * 1.4:.1f}" gradientUnits="userSpaceOnUse">
      <stop offset="0%" stop-color="#e0f2fe" />
      <stop offset="62%" stop-color="#7dd3fc" />
      <stop offset="100%" stop-color="#38bdf8" />
    </radialGradient>
    <filter id="cxGlow" x="72" y="72" width="368" height="368" filterUnits="userSpaceOnUse" color-interpolation-filters="sRGB">
      <feGaussianBlur stdDeviation="11" result="blur" />
      <feMerge>
        <feMergeNode in="blur" />
        <feMergeNode in="SourceGraphic" />
      </feMerge>
    </filter>
    <clipPath id="cxClip">
      <path d="{outer_path}" />
    </clipPath>
  </defs>

  <g clip-path="url(#cxClip)">
    <path d="{outer_path}" fill="url(#cxBg)" />
    <circle cx="256" cy="248" r="204" fill="url(#cxAuraEmerald)" />
    <circle cx="368" cy="138" r="160" fill="url(#cxAuraSky)" />
    <circle cx="256" cy="256" r="{dial_r:.1f}" fill="#060b15" fill-opacity="0.28" stroke="#94a3b8" stroke-opacity="0.16" stroke-width="3.2" />

    <!-- Precision chronograph cardinal ticks -->
    <g stroke="#cbd5e1" stroke-opacity="0.34" stroke-width="5.6" stroke-linecap="round">
      <line x1="256" y1="80.6" x2="256" y2="96.0" />
      <line x1="431.4" y1="256" x2="416.0" y2="256" />
      <line x1="256" y1="431.4" x2="256" y2="416.0" />
      <line x1="80.6" y1="256" x2="96.0" y2="256" />
    </g>

    <!-- Primary ChronoX luminous orbital arc + beacon -->
    <g filter="url(#cxGlow)">
      <path
        d="M {ax0:.2f} {ay0:.2f} A {arc_r:.2f} {arc_r:.2f} 0 1 1 {ax1:.2f} {ay1:.2f}"
        stroke="url(#cxBrandArc)"
        stroke-width="{ARC_HALF_THICK * 512.0:.2f}"
        stroke-linecap="round"
      />
      <circle cx="{bx:.2f}" cy="{by:.2f}" r="{br:.2f}" fill="url(#cxBeacon)" />
    </g>

    <!-- Inner precision chronograph hands -->
    <path
      d="M {hx0:.2f} {hy0:.2f} L 256 256 L {hx1:.2f} {hy1:.2f}"
      stroke="url(#cxHands)"
      stroke-width="{HAND_HALF_THICK * 512.0:.2f}"
      stroke-linecap="round"
      stroke-linejoin="round"
    />
    <circle cx="256" cy="256" r="{PIVOT_OUTER_R * 256.0:.2f}" fill="#2dd4bf" />
    <circle cx="256" cy="256" r="{PIVOT_INNER_R * 256.0:.2f}" fill="#f0fdfa" />
  </g>

  <!-- Subtle glass hairline rim -->
  <path d="{rim_path}" stroke="url(#cxRim)" stroke-width="5.5" />
</svg>
"""


def main() -> int:
    repo_desktop = Path(__file__).resolve().parent.parent
    out_dir = repo_desktop / "build" / "icons"
    out_dir.mkdir(parents=True, exist_ok=True)

    # 1. Write SVG master
    svg_path = out_dir / "icon.svg"
    svg_path.write_text(build_icon_svg(), encoding="utf-8")
    print(f"Generated {svg_path.relative_to(repo_desktop)} ({svg_path.stat().st_size} bytes)")

    # 2. Render 512x512 master and exact 2x premultiplied box downscales (256, 128, 64)
    rgba_by_size: dict[int, bytearray] = {}
    png_by_size: dict[int, bytes] = {}

    master_512 = render_rgba_buffer(
        512,
        lambda x, y, pw: sample_app_icon(x, y, pw, small_optical_boost=0.0, show_micro_ticks=True),
        supersample=1,
    )
    rgba_by_size[512] = master_512
    rgba_by_size[256] = downsample_2x_premul(master_512, 512)
    rgba_by_size[128] = downsample_2x_premul(rgba_by_size[256], 256)
    rgba_by_size[64] = downsample_2x_premul(rgba_by_size[128], 128)

    # 3. Render tiny optical-boosted sizes (48, 32, 16) with 4x4 supersampling for crisp edges
    small_configs = [
        (48, 0.15, True),
        (32, 0.35, False),
        (16, 0.72, False),
    ]
    for size, boost, show_ticks in small_configs:
        rgba_by_size[size] = render_rgba_buffer(
            size,
            lambda x, y, pw, b=boost, t=show_ticks: sample_app_icon(
                x, y, pw, small_optical_boost=b, show_micro_ticks=t
            ),
            supersample=4,
        )

    for size in (16, 32, 48, 64, 128, 256, 512):
        png_bytes = encode_png(size, size, rgba_by_size[size])
        png_by_size[size] = png_bytes
        p = out_dir / f"{size}x{size}.png"
        p.write_bytes(png_bytes)
        print(f"Generated {p.relative_to(repo_desktop)} ({len(png_bytes)} bytes)")

    # Master icon.png (512x512)
    icon_png_path = out_dir / "icon.png"
    icon_png_path.write_bytes(png_by_size[512])
    print(f"Generated {icon_png_path.relative_to(repo_desktop)} ({len(png_by_size[512])} bytes)")

    # 4. Multi-resolution Windows .ico (16, 32, 48, 64, 128, 256)
    ico_entries = [
        (sz, png_by_size[sz], rgba_by_size[sz]) for sz in (16, 32, 48, 64, 128, 256)
    ]
    ico_bytes = encode_ico(ico_entries)
    ico_path = out_dir / "icon.ico"
    ico_path.write_bytes(ico_bytes)
    print(f"Generated {ico_path.relative_to(repo_desktop)} ({len(ico_bytes)} bytes, 6 sizes)")

    # 5. Multi-resolution macOS .icns (16, 32, 64, 128, 256, 512)
    icns_bytes = encode_icns(png_by_size)
    icns_path = out_dir / "icon.icns"
    icns_path.write_bytes(icns_bytes)
    print(f"Generated {icns_path.relative_to(repo_desktop)} ({len(icns_bytes)} bytes, 6 sizes)")

    # 6. Monochrome System Tray Icons (4x4 supersampled)
    #   - tray-dark.png       (32x32): light glyph (#F1F5F9) designed for dark OS taskbars/panels
    #   - tray-light.png      (32x32): dark obsidian glyph (#0F172A) designed for light OS taskbars/panels
    #   - trayTemplate.png    (22x22): pure black (#000000) macOS template image
    #   - trayTemplate@2x.png (44x44): pure black (#000000) macOS Retina template image
    tray_specs = [
        ("tray-dark.png", 32, (241.0, 245.0, 249.0)),
        ("tray-light.png", 32, (15.0, 23.0, 42.0)),
        ("trayTemplate.png", 22, (0.0, 0.0, 0.0)),
        ("trayTemplate@2x.png", 44, (0.0, 0.0, 0.0)),
    ]
    for filename, sz, rgb in tray_specs:
        tray_rgba = render_rgba_buffer(sz, make_tray_sampler(rgb), supersample=4)
        tray_png = encode_png(sz, sz, tray_rgba)
        tp = out_dir / filename
        tp.write_bytes(tray_png)
        print(f"Generated {tp.relative_to(repo_desktop)} ({len(tray_png)} bytes, {sz}x{sz})")

    return 0


if __name__ == "__main__":
    sys.exit(main())
