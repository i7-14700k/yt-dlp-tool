"""A small glyph set drawn with QPainter.

Geometric, 1.5 px strokes on a 16 px grid, square caps. Used only where a
symbol is quicker to read than a word.
"""

from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap

from . import theme


def _pen(color: QColor, w: float) -> QPen:
    pen = QPen(color, w)
    pen.setCapStyle(Qt.SquareCap)
    pen.setJoinStyle(Qt.MiterJoin)
    return pen


def draw(p: QPainter, name: str, rect: QRectF, color: QColor | None = None, stroke: float = 1.5) -> None:
    color = color or theme.FG
    p.save()
    p.setRenderHint(QPainter.Antialiasing, True)
    s = min(rect.width(), rect.height()) / 16.0
    p.translate(rect.center().x() - 8 * s, rect.center().y() - 8 * s)
    p.scale(s, s)
    w = stroke / s
    pen = _pen(color, w)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)

    if name == "pause":
        p.setPen(Qt.NoPen)
        p.setBrush(color)
        p.drawRect(QRectF(4.5, 3.5, 2.4, 9))
        p.drawRect(QRectF(9.1, 3.5, 2.4, 9))
    elif name == "play":
        p.setPen(Qt.NoPen)
        p.setBrush(color)
        path = QPainterPath(QPointF(5, 3.2))
        path.lineTo(12.8, 8)
        path.lineTo(5, 12.8)
        path.closeSubpath()
        p.drawPath(path)
    elif name == "stop":
        p.setPen(Qt.NoPen)
        p.setBrush(color)
        p.drawRoundedRect(QRectF(4, 4, 8, 8), 1, 1)
    elif name == "close":
        p.drawLine(QPointF(4.5, 4.5), QPointF(11.5, 11.5))
        p.drawLine(QPointF(11.5, 4.5), QPointF(4.5, 11.5))
    elif name == "plus":
        p.drawLine(QPointF(8, 3.5), QPointF(8, 12.5))
        p.drawLine(QPointF(3.5, 8), QPointF(12.5, 8))
    elif name == "minus":
        p.drawLine(QPointF(3.5, 8), QPointF(12.5, 8))
    elif name == "check":
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        p.setPen(pen)
        path = QPainterPath(QPointF(3.5, 8.4))
        path.lineTo(6.6, 11.4)
        path.lineTo(12.5, 4.8)
        p.drawPath(path)
    elif name == "retry":
        pen.setCapStyle(Qt.FlatCap)
        p.setPen(pen)
        r = QRectF(3.5, 3.5, 9, 9)
        p.drawArc(r, 100 * 16, 290 * 16)
        p.setPen(Qt.NoPen)
        p.setBrush(color)
        tip = QPainterPath(QPointF(7.2, 1.6))
        tip.lineTo(10.2, 3.6)
        tip.lineTo(7.2, 5.6)
        tip.closeSubpath()
        p.drawPath(tip)
    elif name == "folder":
        path = QPainterPath(QPointF(2.5, 4))
        path.lineTo(6.2, 4)
        path.lineTo(7.6, 5.6)
        path.lineTo(13.5, 5.6)
        path.lineTo(13.5, 12.5)
        path.lineTo(2.5, 12.5)
        path.closeSubpath()
        p.drawPath(path)
    elif name == "more":
        p.setPen(Qt.NoPen)
        p.setBrush(color)
        for x in (3.5, 8, 12.5):
            p.drawEllipse(QPointF(x, 8), 1.25, 1.25)
    elif name == "chevron-down":
        path = QPainterPath(QPointF(4.5, 6.2))
        path.lineTo(8, 9.7)
        path.lineTo(11.5, 6.2)
        p.drawPath(path)
    elif name == "chevron-right":
        path = QPainterPath(QPointF(6.2, 4.5))
        path.lineTo(9.7, 8)
        path.lineTo(6.2, 11.5)
        p.drawPath(path)
    elif name == "chevron-left":
        path = QPainterPath(QPointF(9.8, 4.5))
        path.lineTo(6.3, 8)
        path.lineTo(9.8, 11.5)
        p.drawPath(path)
    elif name == "arrow-down":
        p.drawLine(QPointF(8, 2.8), QPointF(8, 11))
        path = QPainterPath(QPointF(4.4, 7.6))
        path.lineTo(8, 11.2)
        path.lineTo(11.6, 7.6)
        p.drawPath(path)
        p.drawLine(QPointF(3.5, 13.5), QPointF(12.5, 13.5))
    elif name == "search":
        p.drawEllipse(QRectF(3, 3, 7.6, 7.6))
        p.drawLine(QPointF(9.8, 9.8), QPointF(13, 13))
    elif name == "trash":
        p.drawLine(QPointF(3, 4.5), QPointF(13, 4.5))
        p.drawLine(QPointF(6.5, 2.8), QPointF(9.5, 2.8))
        path = QPainterPath(QPointF(4.4, 4.5))
        path.lineTo(5.2, 13.2)
        path.lineTo(10.8, 13.2)
        path.lineTo(11.6, 4.5)
        p.drawPath(path)
    elif name == "external":
        path = QPainterPath(QPointF(7, 3.5))
        path.lineTo(3.5, 3.5)
        path.lineTo(3.5, 12.5)
        path.lineTo(12.5, 12.5)
        path.lineTo(12.5, 9)
        p.drawPath(path)
        p.drawLine(QPointF(8.5, 7.5), QPointF(12.8, 3.2))
        p.drawLine(QPointF(9.5, 3.2), QPointF(12.8, 3.2))
        p.drawLine(QPointF(12.8, 3.2), QPointF(12.8, 6.5))
    elif name == "log":
        for y in (4.5, 8, 11.5):
            p.drawLine(QPointF(3.5, y), QPointF(12.5 if y != 11.5 else 9, y))
    elif name == "link":
        pen.setCapStyle(Qt.RoundCap)
        p.setPen(pen)
        p.drawRoundedRect(QRectF(2.5, 6, 6.5, 4), 2, 2)
        p.drawRoundedRect(QRectF(7, 6, 6.5, 4), 2, 2)
    elif name == "drag":
        p.setPen(Qt.NoPen)
        p.setBrush(color)
        for x in (6, 10):
            for y in (4, 8, 12):
                p.drawEllipse(QPointF(x, y), 1.1, 1.1)
    elif name == "warn":
        path = QPainterPath(QPointF(8, 2.5))
        path.lineTo(14, 13)
        path.lineTo(2, 13)
        path.closeSubpath()
        p.drawPath(path)
        p.drawLine(QPointF(8, 6.5), QPointF(8, 9.2))
        p.setPen(Qt.NoPen)
        p.setBrush(color)
        p.drawEllipse(QPointF(8, 11.2), 0.9, 0.9)
    elif name == "spinner":
        pen.setCapStyle(Qt.RoundCap)
        p.setPen(pen)
        p.drawArc(QRectF(3, 3, 10, 10), 90 * 16, -270 * 16)
    elif name == "dot":
        p.setPen(Qt.NoPen)
        p.setBrush(color)
        p.drawEllipse(QPointF(8, 8), 3, 3)
    elif name == "ring":
        p.drawEllipse(QPointF(8, 8), 4, 4)
    p.restore()


def draw_spinner(p: QPainter, rect: QRectF, angle: float, color: QColor | None = None, stroke: float = 1.6) -> None:
    color = color or theme.FG
    p.save()
    p.setRenderHint(QPainter.Antialiasing, True)
    pen = QPen(color, stroke)
    pen.setCapStyle(Qt.RoundCap)
    p.setPen(pen)
    r = rect.adjusted(stroke, stroke, -stroke, -stroke)
    p.drawArc(r, int(-angle * 16), int(-100 * 16))
    faint = QColor(color)
    faint.setAlphaF(0.18)
    pen.setColor(faint)
    p.setPen(pen)
    p.drawEllipse(r)
    p.restore()


def icon(name: str, size: int = 16, color: QColor | None = None) -> QIcon:
    ic = QIcon()
    for dpr in (1.0, 1.5, 2.0):
        pm = QPixmap(QSize(int(math.ceil(size * dpr)), int(math.ceil(size * dpr))))
        pm.setDevicePixelRatio(dpr)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        draw(p, name, QRectF(0, 0, size, size), color or theme.FG)
        p.end()
        ic.addPixmap(pm)
    return ic
