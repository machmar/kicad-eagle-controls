"""Draw the plugin icons (no dependencies): a PCB-green tile with four pan arrows.

    python make_icons.py   -> resources/icon.png (64 px), eagle_controls/icon.png (24 px)
"""

import os
import struct
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
GREEN, GOLD, WHITE = (32, 110, 60), (214, 168, 64), (250, 250, 245)


def draw(n):
    s = n / 64.0  # design on a 64 grid
    px = [[(0, 0, 0, 0)] * n for _ in range(n)]
    for y in range(n):
        for x in range(n):
            u, v = (x + 0.5) / s, (y + 0.5) / s
            # rounded tile
            cx, cy = min(max(u, 10), 54), min(max(v, 10), 54)
            if (u - cx) ** 2 + (v - cy) ** 2 > 10 ** 2 or not (2 <= u <= 62 and 2 <= v <= 62):
                continue
            col = GREEN
            dx, dy = u - 32, v - 32
            if dx * dx + dy * dy <= 6.5 ** 2:
                col = GOLD                                        # centre pad
            for ax, ay in ((dx, dy), (-dx, dy), (dy, dx), (-dy, dx)):  # four arrows, pointing +x
                if 10 <= ax <= 20 and abs(ay) <= 2.6:
                    col = WHITE                                   # shaft
                if 20 <= ax <= 28 and abs(ay) <= (28 - ax):
                    col = WHITE                                   # head
            px[y][x] = col + (255,)
    return px


def png(px):
    n = len(px)
    raw = b"".join(b"\0" + b"".join(bytes(p) for p in row) for row in px)

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", n, n, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


for size, path in ((64, "resources/icon.png"), (24, "eagle_controls/icon.png")):
    full = os.path.join(HERE, path)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "wb") as f:
        f.write(png(draw(size)))
    print(full)
