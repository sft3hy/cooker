#!/usr/bin/env python3
"""Make the pancake. The favicon is the dish Cooker exists to serve, drawn
the way the kitchen draws everything: chunky pixels, no anti-aliasing, a dark
outline so it survives being 16 pixels wide in a browser tab.

Written as a generator rather than a binary because art you cannot re-render
is art you cannot fix. Run from the repo root:

    .venv/bin/python scripts/make_favicon.py

Emits web/public/favicon.png (64x64, a 32-grid pancake at x2) and
web/public/apple-touch-icon.png (160x160, x5 — chunky on purpose; iOS will
resize, let it resize whole pixels' worth, not subpixels).

Inspired by the pancake Sam drew on 2026-10-07: stacked rounds, a knob of
butter, a run of syrup, warm highlight up the left like light from an oven.
"""

from __future__ import annotations

import binascii
import struct
import zlib
from pathlib import Path

GRID = 32

# the palette: pressed, not pastel
OUTLINE = (88, 38, 14, 255)      # dark rim, survives 16px
DARK = (201, 106, 27, 255)       # cooked edge
MID = (232, 145, 47, 255)        # pancake body
LIGHT = (247, 192, 75, 255)      # top face
GLOW = (255, 233, 168, 255)      # oven-light highlight
SYRUP = (217, 83, 30, 255)       # the run
SYRUP_D = (178, 60, 18, 255)     # where it thickens
BUTTER = (255, 224, 102, 255)    # the knob
BUTTER_D = (230, 184, 48, 255)    # its shaded facet
TRANSPARENT = (0, 0, 0, 0)


def ellipse(cx: float, cy: float, rx: float, ry: float) -> list[tuple[int, int]]:
    pts = []
    for y in range(GRID):
        for x in range(GRID):
            d = ((x + 0.5 - cx) / rx) ** 2 + ((y + 0.5 - cy) / ry) ** 2
            if d <= 1.0:
                pts.append((x, y))
    return pts


def draw() -> list[list[tuple[int, int, int, int]]]:
    g = [[TRANSPARENT] * GRID for _ in range(GRID)]
    layers = [  # bottom up: each pancake is a squat ellipse, offset up
        (16.0, 23.0, 13.0, 4.6),
        (16.0, 17.5, 12.0, 4.6),
        (16.0, 12.0, 11.0, 4.4),
    ]
    for cx, cy, rx, ry in layers:
        for (x, y) in ellipse(cx, cy, rx, ry):
            dy = y - cy
            if dy > ry * 0.45:
                g[y][x] = DARK          # the cooked skirt
            elif dy > -ry * 0.25:
                g[y][x] = MID
            else:
                g[y][x] = LIGHT        # top face catches the light
            # oven light: the left third of the top faces glows
            if dy < -ry * 0.1 and x < cx - rx * 0.15 and (x + y) % 5 != 0:
                g[y][x] = GLOW
    # syrup: runs from under the butter down the right side
    for x0, run in ((18, 6), (21, 5), (14, 4), (11, 3)):
        top = 11 if x0 % 2 else 12
        for i in range(run):
            y = top + i
            if 0 <= y < GRID:
                g[y][x0] = SYRUP if i < run - 1 else SYRUP_D
    # the knob of butter: a chunky cube with a shaded facet
    for y in range(5, 11):
        for x in range(13, 19):
            g[y][x] = BUTTER_D if x >= 17 or y >= 9 else BUTTER
    for x in range(13, 17):           # knife-cut shine
        g[5][x] = GLOW
    # dark outline: any body pixel touching empty space, and its neighbours
    body = {(x, y) for y in range(GRID) for x in range(GRID)
            if g[y][x] is not TRANSPARENT}
    for (x, y) in sorted(body):
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            nx, ny = x + dx, y + dy
            if 0 <= nx < GRID and 0 <= ny < GRID and (nx, ny) not in body:
                g[y][x] = OUTLINE
    return g


def png_bytes(grid, scale: int) -> bytes:
    w = h = GRID * scale
    raw = b"".join(
        b"\x00" + b"".join(struct.pack("4B", *row[x])
                           for x in range(GRID) for _ in range(scale))
        for row in grid for _ in range(scale))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", binascii.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def main() -> None:
    grid = draw()
    pub = Path("web/static")
    pub.mkdir(parents=True, exist_ok=True)
    out = [
        (pub / "favicon.png", 2),
        (pub / "apple-touch-icon.png", 5),
    ]
    for path, scale in out:
        path.write_bytes(png_bytes(grid, scale))
        print(f"wrote {path} ({GRID * scale}x{GRID * scale})")
    # also drop a big preview next to the generator for human eyes
    Path("scripts/pancake-preview.png").write_bytes(png_bytes(grid, 8))
    print("wrote scripts/pancake-preview.png (256x256)")


if __name__ == "__main__":
    main()
