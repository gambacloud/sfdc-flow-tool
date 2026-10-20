"""Generate placeholder PNG icons (16/32/48/128) with only the stdlib.

Blue rounded square with the three-node mark used by the web UI favicon.
Replace extension/icons/*.png with final artwork before submission.
"""

import struct
import zlib
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "icons"
BLUE = (1, 118, 211)
WHITE = (255, 255, 255)


def render(size: int):
    s = size / 32.0
    nodes = [(8, 9), (24, 9), (16, 23)]
    rows = []
    for y in range(size):
        row = bytearray()
        for x in range(size):
            px, py = (x + 0.5) / s, (y + 0.5) / s
            # rounded square, radius 7 (in 32 space)
            dx = max(abs(px - 16) - 9, 0)
            dy = max(abs(py - 16) - 9, 0)
            inside = dx * dx + dy * dy <= 7 * 7
            if not inside:
                row += bytes((0, 0, 0, 0))
                continue
            colour = BLUE
            for nx, ny in nodes:
                if (px - nx) ** 2 + (py - ny) ** 2 <= 3.4 ** 2:
                    colour = WHITE
            # the two connectors, as thin segments toward the bottom node
            for ax, ay in nodes[:2]:
                bx, by = nodes[2]
                vx, vy = bx - ax, by - ay
                t = max(0.0, min(1.0, ((px - ax) * vx + (py - ay) * vy) / (vx * vx + vy * vy)))
                if (px - (ax + t * vx)) ** 2 + (py - (ay + t * vy)) ** 2 <= 1.1 ** 2:
                    colour = WHITE
            row += bytes((*colour, 255))
        rows.append(bytes(row))
    return rows


def png(size: int) -> bytes:
    raw = b"".join(b"\x00" + r for r in render(size))

    def chunk(tag: bytes, data: bytes) -> bytes:
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    for n in (16, 32, 48, 128):
        (OUT / f"icon{n}.png").write_bytes(png(n))
        print("wrote", OUT / f"icon{n}.png")
