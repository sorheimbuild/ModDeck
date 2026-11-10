#!/usr/bin/env python3
"""
Generate web/icon.ico for the Windows build.

Draws the ModDeck cube mark directly (no image libraries, no browser) at every
standard Windows icon size, then packs the bitmaps into a single ICO.

    python tools/make_icon.py
"""

from __future__ import annotations

import math
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "web" / "icon.ico"
SIZES = (16, 32, 48, 64, 128, 256)

BG_TOP = (13, 21, 32)
BG_BOTTOM = (8, 16, 22)
LINE_TOP = (90, 232, 168)
LINE_BOTTOM = (47, 191, 128)

# Cube geometry in a 64x64 viewBox (matches web/favicon.svg).
MAIN_LINES = [
    ((32, 9), (53, 20.5)),
    ((53, 20.5), (53, 43.5)),
    ((53, 43.5), (32, 55)),
    ((32, 55), (11, 43.5)),
    ((11, 43.5), (11, 20.5)),
    ((11, 20.5), (32, 9)),
    ((11, 20.5), (32, 32)),
    ((32, 32), (53, 20.5)),
    ((32, 32), (32, 55)),
]
FAINT_LINES = [
    ((21.5, 15.5), (42.5, 27)),
    ((42.5, 27), (42.5, 38)),
]


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return low if value < low else high if value > high else value


def lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def seg_distance(px: float, py: float, a: tuple[float, float], b: tuple[float, float]) -> float:
    ax, ay = a
    bx, by = b
    vx, vy = bx - ax, by - ay
    denom = vx * vx + vy * vy
    t = 0.0 if denom == 0 else ((px - ax) * vx + (py - ay) * vy) / denom
    t = clamp(t)
    dx = px - (ax + t * vx)
    dy = py - (ay + t * vy)
    return math.hypot(dx, dy)


def rounded_rect_sdf(px: float, py: float, size: float, radius: float) -> float:
    qx = abs(px - size / 2) - (size / 2 - radius)
    qy = abs(py - size / 2) - (size / 2 - radius)
    outside = math.hypot(max(qx, 0.0), max(qy, 0.0))
    inside = min(max(qx, qy), 0.0)
    return outside + inside - radius


def render(size: int) -> bytearray:
    """Return top-down RGBA bytes for one square icon."""
    scale = size / 64.0
    px_size = 1.0 / scale
    radius = 14.0
    main_half = (2.6 / 2.0)
    faint_half = (2.2 / 2.0)

    data = bytearray(size * size * 4)
    for y in range(size):
        for x in range(size):
            px = (x + 0.5) * px_size
            py = (y + 0.5) * px_size

            sdf = rounded_rect_sdf(px, py, 64.0, radius)
            bg_cov = clamp(0.5 - sdf / px_size)
            t = clamp((px + py) / 128.0)
            bg = (
                lerp(BG_TOP[0], BG_BOTTOM[0], t),
                lerp(BG_TOP[1], BG_BOTTOM[1], t),
                lerp(BG_TOP[2], BG_BOTTOM[2], t),
            )

            line_hit = 0.0
            for a, b in MAIN_LINES:
                d = seg_distance(px, py, a, b)
                line_hit = max(line_hit, clamp(0.5 + (main_half - d) / px_size))
            faint = 0.0
            for a, b in FAINT_LINES:
                d = seg_distance(px, py, a, b)
                faint = max(faint, clamp(0.5 + (faint_half - d) / px_size))
            line_hit = max(line_hit, faint * 0.35)

            lt = clamp(py / 64.0)
            line = (
                lerp(LINE_TOP[0], LINE_BOTTOM[0], lt),
                lerp(LINE_TOP[1], LINE_BOTTOM[1], lt),
                lerp(LINE_TOP[2], LINE_BOTTOM[2], lt),
            )

            r = bg[0] * (1.0 - line_hit) + line[0] * line_hit
            g = bg[1] * (1.0 - line_hit) + line[1] * line_hit
            b = bg[2] * (1.0 - line_hit) + line[2] * line_hit
            alpha = max(bg_cov, line_hit)

            i = (y * size + x) * 4
            data[i] = int(clamp(r, 0, 255))
            data[i + 1] = int(clamp(g, 0, 255))
            data[i + 2] = int(clamp(b, 0, 255))
            data[i + 3] = int(alpha * 255)

    return data


def dib(size: int, rgba: bytearray) -> bytes:
    """32-bit bottom-up BMP with an all-opaque AND mask, as stored in an ICO."""
    pixels = bytearray()
    for y in range(size - 1, -1, -1):
        row = y * size * 4
        for x in range(size):
            i = row + x * 4
            pixels += bytes((rgba[i + 2], rgba[i + 1], rgba[i], rgba[i + 3]))
    mask_stride = ((size + 31) // 32) * 4
    mask = bytes(mask_stride * size)
    header = struct.pack(
        "<IiiHHIIiiII",
        40, size, size * 2, 1, 32, 0,
        len(pixels) + len(mask), 0, 0, 0, 0,
    )
    return header + bytes(pixels) + mask


def build_ico(images: list[tuple[int, bytes]], dest: Path) -> None:
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    entries = bytearray()
    payload = bytearray()
    for size, image in images:
        dim = 0 if size >= 256 else size
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(image), offset)
        payload += image
        offset += len(image)
    dest.write_bytes(header + bytes(entries) + bytes(payload))


def main() -> int:
    images = []
    for size in SIZES:
        images.append((size, dib(size, render(size))))
        print(f"  drew {size}x{size}")
    build_ico(images, OUT)
    print(f"wrote {OUT.relative_to(ROOT)} ({OUT.stat().st_size:,} bytes, {len(SIZES)} sizes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
