#!/usr/bin/env python3
"""Generate the app icons in app/static/icons/.

Run from the repository root:

    python3 ops/make_icons.py

The icons are committed, so this only needs running when the design changes. It
exists so that no binary file in the repository is unexplainable — the icons can
always be rebuilt from this script, and the colours are a two-line edit.

Stdlib only: no Pillow, no ImageMagick, no build step. PNG is a simple enough
format to write directly, and the icon is made of axis-aligned rectangles, so
there are no jagged edges to smooth away.

The design is a placeholder: a calendar page on a dark green field. Replacing it
with real artwork means dropping in three PNG files at these sizes and deleting
this script.
"""

from __future__ import annotations

import struct
import sys
import zlib
from pathlib import Path

# Matches --accent and --bg in app/static/app.css.
FIELD = (0x2F, 0x5D, 0x50)
MARK = (0xFF, 0xFF, 0xFF)

# 180 is what iOS uses for a home screen icon; 192 and 512 are what the web app
# manifest asks for.
SIZES = (180, 192, 512)

OUTPUT_DIR = Path(__file__).resolve().parent.parent / "app" / "static" / "icons"


class Icon:
    """A square RGB image, drawn with rectangles in fractional coordinates."""

    def __init__(self, size: int, background: tuple[int, int, int]) -> None:
        self.size = size
        self.pixels = bytearray(bytes(background) * size * size)

    def rect(
        self,
        x0: float,
        y0: float,
        x1: float,
        y1: float,
        colour: tuple[int, int, int],
    ) -> None:
        """Fill a rectangle given as fractions of the icon's width and height."""
        px0, py0 = round(x0 * self.size), round(y0 * self.size)
        px1, py1 = round(x1 * self.size), round(y1 * self.size)
        row = bytes(colour) * max(0, px1 - px0)
        for y in range(max(0, py0), min(self.size, py1)):
            start = (y * self.size + px0) * 3
            self.pixels[start : start + len(row)] = row

    def to_png(self) -> bytes:
        # One filter byte (0 = no filtering) in front of each row of RGB triples.
        stride = self.size * 3
        raw = b"".join(
            b"\x00" + bytes(self.pixels[y * stride : (y + 1) * stride])
            for y in range(self.size)
        )

        def chunk(kind: bytes, payload: bytes) -> bytes:
            body = kind + payload
            return (
                struct.pack(">I", len(payload))
                + body
                + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
            )

        header = struct.pack(
            ">IIBBBBB",
            self.size,  # width
            self.size,  # height
            8,  # bits per channel
            2,  # colour type 2 = RGB, no alpha
            0,  # deflate
            0,  # adaptive filtering
            0,  # no interlacing
        )
        return (
            b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(raw, 9))
            + chunk(b"IEND", b"")
        )


def draw(size: int) -> Icon:
    icon = Icon(size, FIELD)

    # A calendar page: solid white block, hollowed out below its header band.
    # Everything stays inside the middle 80% of the square, which is the safe zone
    # a "maskable" icon must respect — Android and some launchers crop icons to a
    # circle, and anything outside that zone can be cut off.
    icon.rect(0.20, 0.24, 0.80, 0.82, MARK)
    icon.rect(0.26, 0.38, 0.74, 0.76, FIELD)

    # The two binder rings along the top edge.
    icon.rect(0.33, 0.16, 0.39, 0.26, MARK)
    icon.rect(0.61, 0.16, 0.67, 0.26, MARK)

    # Two entries written on the page.
    icon.rect(0.33, 0.47, 0.56, 0.525, MARK)
    icon.rect(0.33, 0.60, 0.67, 0.655, MARK)

    return icon


def main() -> int:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    for size in SIZES:
        path = OUTPUT_DIR / f"icon-{size}.png"
        path.write_bytes(draw(size).to_png())
        print(f"wrote {path.relative_to(OUTPUT_DIR.parents[3])} ({size}x{size})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
