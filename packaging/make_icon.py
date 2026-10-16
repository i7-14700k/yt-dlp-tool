"""Render assets/icon.ico (and icon.png) from the Ingest mark.

Run: python packaging/make_icon.py   (needs PySide6; works headless)
The mark: a white tile, corner crop marks like a viewfinder, and a stroke
dropping into the frame. Small sizes are drawn on the pixel grid by hand
so they stay sharp in the taskbar.
"""

from __future__ import annotations

import io
import os
import struct
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QPointF, QRectF, Qt  # noqa: E402
from PySide6.QtGui import QColor, QGuiApplication, QImage, QPainter, QPainterPath, QPen  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SIZES = [16, 20, 24, 32, 40, 48, 64, 128, 256]


def render(size: int) -> QImage:
    img = QImage(size, size, QImage.Format_ARGB32_Premultiplied)
    img.fill(Qt.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing, True)
    s = size / 16.0
    radius = 3.6 if size >= 32 else 3.0
    p.setPen(Qt.NoPen)
    p.setBrush(QColor("#FFFFFF"))
    p.drawRoundedRect(QRectF(0, 0, size, size), radius * s, radius * s)

    # Stroke weight grows a little on small sizes so it survives downsampling.
    w = (1.5 if size >= 48 else 1.75 if size >= 24 else 2.0) / 16 * size
    if size <= 20:
        w = round(w)
    pen = QPen(QColor("#000000"), w)
    pen.setCapStyle(Qt.FlatCap)
    pen.setJoinStyle(Qt.MiterJoin)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)

    def snap(v: float) -> float:
        # Centre odd-width strokes on half pixels.
        return (round(v - w / 2) + w / 2) if size <= 32 else v

    if size <= 20:
        # Too small for the frame: a heavier stroke and arrowhead alone.
        cx = round(size / 2 - w / 2) + w / 2
        p.drawLine(QPointF(cx, 3.0 * s), QPointF(cx, 11.6 * s))
        head = QPainterPath(QPointF(cx - 3.6 * s, 8.0 * s))
        head.lineTo(cx, 11.8 * s)
        head.lineTo(cx + 3.6 * s, 8.0 * s)
        p.drawPath(head)
        p.end()
        return img
    inset = 3.2 * s
    arm = 3.0 * s
    lo, hi = snap(inset), snap(size - inset)
    for (x, y, dx, dy) in ((lo, lo, 1, 1), (hi, lo, -1, 1), (lo, hi, 1, -1), (hi, hi, -1, -1)):
        path = QPainterPath(QPointF(x + arm * dx, y))
        path.lineTo(x, y)
        path.lineTo(x, y + arm * dy)
        p.drawPath(path)
    cx = snap(size / 2)
    p.drawLine(QPointF(cx, 5.0 * s), QPointF(cx, 10.6 * s))
    head = QPainterPath(QPointF(cx - 2.6 * s, 8.2 * s))
    head.lineTo(cx, 10.8 * s)
    head.lineTo(cx + 2.6 * s, 8.2 * s)
    p.drawPath(head)
    p.end()
    return img


def png_bytes(img: QImage) -> bytes:
    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QIODevice.WriteOnly)
    img.save(buf, "PNG")
    buf.close()
    return bytes(ba)


def write_ico(path: Path, images: list[tuple[int, bytes]]) -> None:
    out = io.BytesIO()
    out.write(struct.pack("<HHH", 0, 1, len(images)))
    offset = 6 + 16 * len(images)
    for size, data in images:
        dim = 0 if size >= 256 else size
        out.write(struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(data), offset))
        offset += len(data)
    for _size, data in images:
        out.write(data)
    path.write_bytes(out.getvalue())


def main() -> int:
    QGuiApplication(sys.argv[:1])
    assets = ROOT / "assets"
    assets.mkdir(exist_ok=True)
    images = [(s, png_bytes(render(s))) for s in SIZES]
    write_ico(assets / "icon.ico", images)
    render(512).save(str(assets / "icon.png"))
    sheet = QImage(len(SIZES) * 270, 280, QImage.Format_ARGB32)
    sheet.fill(QColor("#000000"))
    p = QPainter(sheet)
    x = 10
    for s in SIZES:
        p.drawImage(x, 10 + (256 - s) // 2, render(s))
        x += max(s, 24) + 20
    p.end()
    sheet.save(str(ROOT / "packaging" / "icon-sheet.png"))
    print(f"wrote {assets / 'icon.ico'} ({len(images)} sizes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
