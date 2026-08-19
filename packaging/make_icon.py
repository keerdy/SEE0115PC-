#!/usr/bin/env python3
"""Generate PocketTestAgent.ico from source/title_photo.svg using PySide6.

The SVG is a single-path icon with viewBox 0 0 1024 1024. QSvgRenderer renders
it, then the script packs multiple PNG resolutions into one ICO file so the
icon looks sharp at every size (16..256).
"""

from __future__ import annotations

import argparse
import struct
from pathlib import Path

from PySide6.QtCore import QBuffer, QByteArray, Qt
from PySide6.QtGui import QImage, QImageWriter, QPainter
from PySide6.QtSvg import QSvgRenderer

SIZES = (256, 128, 64, 48, 32, 24, 16)


def render_png(svg_path: Path, size: int) -> bytes:
    renderer = QSvgRenderer(str(svg_path))
    image = QImage(size, size, QImage.Format_ARGB32)
    image.fill(Qt.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.Antialiasing)
    renderer.render(painter)
    painter.end()
    buffer = QBuffer()
    buffer.open(QBuffer.WriteOnly)
    writer = QImageWriter(buffer, b"PNG")
    writer.write(image)
    return bytes(buffer.data())


def write_ico(output_path: Path, pngs: list[bytes]) -> None:
    if not pngs:
        raise ValueError("no PNG images to pack")
    images = [(png, _pixel_size(png)) for png in pngs]
    count = len(images)
    header = struct.pack("<HHH", 0, 1, count)
    offset = 6 + 16 * count
    body = bytearray()
    for png, size in images:
        body.extend(
            struct.pack(
                "<BBBBHHII",
                0 if size >= 256 else size,
                0 if size >= 256 else size,
                0,
                0,
                1,
                32,
                len(png),
                offset,
            )
        )
        offset += len(png)
    payload = header + bytes(body) + b"".join(pngs)
    output_path.write_bytes(payload)


def _pixel_size(png: bytes) -> int:
    width = struct.unpack(">I", png[16:20])[0]
    return width


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--svg", default="source/title_photo.svg")
    parser.add_argument("--output", default="source/PocketTestAgent.ico")
    args = parser.parse_args()

    svg_path = Path(args.svg).resolve()
    output_path = Path(args.output).resolve()
    if not svg_path.exists():
        print(f"ERROR: SVG not found: {svg_path}")
        return 1

    pngs = [render_png(svg_path, size) for size in SIZES]
    write_ico(output_path, pngs)
    print(f"Wrote {output_path} ({len(pngs)} sizes, {output_path.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
