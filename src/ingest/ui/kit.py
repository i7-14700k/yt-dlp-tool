"""Hand-painted controls. No borders, no outlines; state is shown with fill and inversion."""

from __future__ import annotations

from PySide6.QtCore import (QEasingCurve, QEvent, QObject, QPoint, QPointF, QRect, QRectF, QSize, Qt, QTimer,
                            QVariantAnimation, Signal)
from PySide6.QtGui import (QColor, QCursor, QFontMetricsF, QGuiApplication, QKeyEvent, QPainter, QPainterPath,
                           QPalette)
from PySide6.QtWidgets import (QAbstractButton, QApplication, QHBoxLayout, QLabel, QLineEdit, QMenu, QSizePolicy,
                               QVBoxLayout, QWidget)

from . import icons, theme as T


def mix(a: QColor, b: QColor, t: float) -> QColor:
    t = max(0.0, min(1.0, t))
    return QColor(round(a.red() + (b.red() - a.red()) * t), round(a.green() + (b.green() - a.green()) * t),
                  round(a.blue() + (b.blue() - a.blue()) * t), round(a.alpha() + (b.alpha() - a.alpha()) * t))


class _Hover:
    """0..1 hover value that eases in 110 ms and repaints its owner."""

    def __init__(self, owner: QWidget, ms: int = 110) -> None:
        self.owner = owner
        self.value = 0.0
        self.anim = QVariantAnimation(owner)
        self.anim.setDuration(ms)
        self.anim.setEasingCurve(QEasingCurve.OutCubic)
        self.anim.valueChanged.connect(self._tick)

    def _tick(self, v) -> None:
        self.value = float(v)
        self.owner.update()

    def to(self, target: float) -> None:
        self.anim.stop()
        self.anim.setStartValue(self.value)
        self.anim.setEndValue(float(target))
        self.anim.start()


def rounded(p: QPainter, rect: QRectF, r: float, color: QColor) -> None:
    p.setPen(Qt.NoPen)
    p.setBrush(color)
    p.drawRoundedRect(rect, r, r)


# ======================================================================= labels

class Label(QLabel):
    def __init__(self, text: str = "", px: float = 13, weight: int = 400, color: QColor | None = None,
                 mono: bool = False, wrap: bool = False, parent: QWidget | None = None, tabular: bool = False) -> None:
        super().__init__(text, parent)
        self.setFont(T.font(px, weight, mono, tabular))
        self.set_color(color or T.FG)
        self.setWordWrap(wrap)
        self.setTextInteractionFlags(Qt.NoTextInteraction)

    def set_color(self, color: QColor) -> None:
        pal = self.palette()
        pal.setColor(QPalette.WindowText, color)
        pal.setColor(QPalette.Text, color)
        self.setPalette(pal)


# ======================================================================= buttons

class Button(QAbstractButton):
    STYLES = {
        "primary": ((T.FG, QColor("#E3E3E3"), QColor("#C8C8C8")), T.BG),
        "secondary": ((T.S2, T.S3, T.S4), T.FG),
        "ghost": ((QColor(0, 0, 0, 0), T.S2, T.S3), T.FG2),
    }

    def __init__(self, text: str = "", kind: str = "secondary", icon: str | None = None, size: str = "md",
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setText(text)
        self.kind = kind
        self.icon_name = icon
        self.size_name = size
        self._hover = _Hover(self)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.TabFocus)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self._font = T.font(14 if size == "lg" else 12.5 if size == "sm" else 13, 600 if size == "lg" else 500)

    def set_kind(self, kind: str) -> None:
        self.kind = kind
        self.update()

    def set_icon(self, name: str | None) -> None:
        self.icon_name = name
        self.updateGeometry()
        self.update()

    def setText(self, text: str) -> None:  # noqa: N802 - Qt API
        super().setText(text)
        self.updateGeometry()

    def sizeHint(self) -> QSize:  # noqa: N802
        h = {"lg": 44, "sm": 26}.get(self.size_name, T.CONTROL_H)
        pad = {"lg": 22, "sm": 10}.get(self.size_name, 14)
        w = T.text_width(self._font, self.text()) if self.text() else 0
        if self.icon_name:
            w += 16 + (6 if self.text() else 0)
            if not self.text():
                return QSize(h, h)
        return QSize(int(w + pad * 2), h)

    def enterEvent(self, e) -> None:  # noqa: N802
        self._hover.to(1)
        super().enterEvent(e)

    def leaveEvent(self, e) -> None:  # noqa: N802
        self._hover.to(0)
        super().leaveEvent(e)

    def paintEvent(self, _e) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        (base, hover, pressed), fg = self.STYLES.get(self.kind, self.STYLES["secondary"])
        h = max(self._hover.value, 1.0 if self.hasFocus() else 0.0)
        bg = mix(base, hover, h)
        if self.isDown():
            bg = pressed
        if not self.isEnabled():
            bg = T.S2 if self.kind == "primary" else (base if self.kind != "ghost" else QColor(0, 0, 0, 0))
            fg = T.FG4
        elif self.kind == "ghost":
            fg = mix(T.FG2, T.FG, h)
        if self.kind == "secondary" and self.isChecked():
            bg, fg = T.FG, T.BG
        r = QRectF(self.rect())
        rounded(p, r, T.R_MD, bg)
        p.setFont(self._font)
        p.setPen(fg)
        if self.icon_name and not self.text():
            icons.draw(p, self.icon_name, QRectF(r.center().x() - 8, r.center().y() - 8, 16, 16), fg)
            return
        tw = T.text_width(self._font, self.text())
        total = tw + (22 if self.icon_name else 0)
        x = r.center().x() - total / 2
        if self.icon_name:
            icons.draw(p, self.icon_name, QRectF(x, r.center().y() - 8, 16, 16), fg)
            x += 22
        p.drawText(QRectF(x, r.top(), tw + 2, r.height()), Qt.AlignVCenter | Qt.AlignLeft, self.text())


class IconButton(QAbstractButton):
    def __init__(self, icon: str, tip: str = "", parent: QWidget | None = None, size: int = 28,
                 color: QColor | None = None) -> None:
        super().__init__(parent)
        self.icon_name = icon
        self._size = size
        self._hover = _Hover(self)
        self._color = color
        self.setToolTip(tip)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.TabFocus)
        self.setFixedSize(size, size)

    def set_icon(self, name: str) -> None:
        self.icon_name = name
        self.update()

    def enterEvent(self, e) -> None:  # noqa: N802
        self._hover.to(1)

    def leaveEvent(self, e) -> None:  # noqa: N802
        self._hover.to(0)

    def paintEvent(self, _e) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        h = max(self._hover.value, 1.0 if self.hasFocus() else 0.0)
        bg = T.S4 if self.isDown() else mix(QColor(0, 0, 0, 0), T.S3, h)
        if self.isChecked():
            bg = T.FG
        rounded(p, QRectF(self.rect()), T.R_MD, bg)
        fg = T.BG if self.isChecked() else mix(self._color or T.FG2, T.FG, h)
        if not self.isEnabled():
            fg = T.FG4
        c = QRectF(self.rect()).center()
        icons.draw(p, self.icon_name, QRectF(c.x() - 8, c.y() - 8, 16, 16), fg)


# ======================================================================= segmented

class Segmented(QWidget):
    changed = Signal(str)

    def __init__(self, options: list[tuple[str, str]], value: str | None = None, parent: QWidget | None = None,
                 equal: bool = True, height: int = 30, font_px: float = 12.5) -> None:
        super().__init__(parent)
        self.options = list(options)
        self.value = value if value is not None else (options[0][0] if options else "")
        self.equal = equal
        self._h = height
        self._font = T.font(font_px, 500)
        self._hover_index = -1
        self._ind = QRectF()
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(170)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)
        self._anim.valueChanged.connect(self._on_anim)
        self.setMouseTracking(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.TabFocus)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        self.setFixedHeight(height)

    def set_options(self, options: list[tuple[str, str]]) -> None:
        self.options = list(options)
        if self.value not in [k for k, _ in options] and options:
            self.value = options[0][0]
        self._ind = QRectF()
        self.updateGeometry()
        self.update()

    def set_value(self, value: str, animate: bool = True, emit: bool = False) -> None:
        if value == self.value and not self._ind.isNull():
            return
        old = self._segment_rect(self._index(self.value))
        self.value = value
        new = self._segment_rect(self._index(value))
        if animate and self.isVisible() and not old.isNull():
            self._anim.stop()
            self._anim.setStartValue(self._ind if not self._ind.isNull() else old)
            self._anim.setEndValue(new)
            self._anim.start()
        else:
            self._ind = new
            self.update()
        if emit:
            self.changed.emit(value)

    def _on_anim(self, v) -> None:
        self._ind = QRectF(v)
        self.update()

    def _index(self, value: str) -> int:
        for i, (k, _) in enumerate(self.options):
            if k == value:
                return i
        return 0

    def _widths(self) -> list[float]:
        n = max(1, len(self.options))
        if self.equal:
            return [(self.width() - 4) / n] * n
        natural = [T.text_width(self._font, label) + 20 for _, label in self.options]
        total = sum(natural)
        extra = max(0.0, (self.width() - 4) - total) / n
        return [w + extra for w in natural]

    def _segment_rect(self, i: int) -> QRectF:
        if not self.options:
            return QRectF()
        ws = self._widths()
        x = 2 + sum(ws[:i])
        return QRectF(x, 2, ws[i], self.height() - 4)

    def sizeHint(self) -> QSize:  # noqa: N802
        natural = sum(T.text_width(self._font, label) + 20 for _, label in self.options) + 4
        return QSize(int(natural), self._h)

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        return QSize(int(len(self.options) * 36), self._h)

    def resizeEvent(self, e) -> None:  # noqa: N802
        self._anim.stop()
        self._ind = self._segment_rect(self._index(self.value))
        super().resizeEvent(e)

    def mouseMoveEvent(self, e) -> None:  # noqa: N802
        i = self._hit(e.position())
        if i != self._hover_index:
            self._hover_index = i
            self.update()

    def leaveEvent(self, e) -> None:  # noqa: N802
        self._hover_index = -1
        self.update()

    def _hit(self, pos: QPointF) -> int:
        for i in range(len(self.options)):
            if self._segment_rect(i).contains(pos):
                return i
        return -1

    def mousePressEvent(self, e) -> None:  # noqa: N802
        if e.button() != Qt.LeftButton:
            return
        i = self._hit(e.position())
        if i >= 0 and self.options[i][0] != self.value:
            self.set_value(self.options[i][0], emit=True)

    def keyPressEvent(self, e: QKeyEvent) -> None:  # noqa: N802
        i = self._index(self.value)
        if e.key() in (Qt.Key_Left, Qt.Key_Up) and i > 0:
            self.set_value(self.options[i - 1][0], emit=True)
        elif e.key() in (Qt.Key_Right, Qt.Key_Down) and i < len(self.options) - 1:
            self.set_value(self.options[i + 1][0], emit=True)
        else:
            super().keyPressEvent(e)

    def paintEvent(self, _e) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        rounded(p, QRectF(self.rect()), T.R_MD, T.S2 if not self.hasFocus() else T.S3)
        if self._ind.isNull():
            self._ind = self._segment_rect(self._index(self.value))
        p.setFont(self._font)
        enabled = self.isEnabled()
        for i, (_k, label) in enumerate(self.options):
            r = self._segment_rect(i)
            if i == self._hover_index and self.options[i][0] != self.value and enabled:
                rounded(p, r, T.R_MD - 1, T.S3)
            p.setPen(T.FG2 if enabled else T.FG4)
            if i == self._hover_index and enabled:
                p.setPen(T.FG)
            p.drawText(r, Qt.AlignCenter, label)
        # Indicator, then the same labels clipped to it in black: text inverts as it slides.
        ind_color = T.FG if enabled else T.S4
        rounded(p, self._ind, T.R_MD - 1, ind_color)
        p.save()
        path = QPainterPath()
        path.addRoundedRect(self._ind, T.R_MD - 1, T.R_MD - 1)
        p.setClipPath(path)
        p.setPen(T.BG if enabled else T.FG3)
        for i, (_k, label) in enumerate(self.options):
            p.drawText(self._segment_rect(i), Qt.AlignCenter, label)
        p.restore()


# ======================================================================= select

class _SelectPopup(QWidget):
    chosen = Signal(str)

    ROW = 32
    MAX_ROWS = 11

    def __init__(self, owner: "Select") -> None:
        super().__init__(None, Qt.Popup | Qt.FramelessWindowHint | Qt.NoDropShadowWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.owner = owner
        self.options = owner.options
        self.index = max(0, owner._index(owner.value))
        self.hover = self.index
        self.offset = 0
        self._font = T.font(13, 400)
        self._detail_font = T.font(11.5, 400, mono=True)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)

    def _row_h(self) -> int:
        return self.ROW + (8 if any(len(o) > 2 and o[2] for o in self.options) else 0)

    def popup_at(self, rect: QRect) -> None:
        rows = min(len(self.options), self.MAX_ROWS)
        rh = self._row_h()
        detail_w = max([T.text_width(self._detail_font, o[2]) for o in self.options if len(o) > 2 and o[2]] or [0])
        label_w = max([T.text_width(self._font, o[1]) for o in self.options] or [0])
        w = max(rect.width(), int(label_w + 56), int(detail_w + 56))
        h = rows * rh + 12
        pos = rect.bottomLeft() + QPoint(0, 4)
        screen = QGuiApplication.screenAt(pos) or QGuiApplication.primaryScreen()
        avail = screen.availableGeometry()
        if pos.y() + h > avail.bottom():
            pos.setY(rect.top() - h - 4)
        if pos.x() + w > avail.right():
            pos.setX(avail.right() - w)
        self.offset = max(0, min(self.index - rows // 2, len(self.options) - rows))
        self.setGeometry(pos.x(), pos.y(), w, h)
        self.show()
        self.setFocus()

    def _row_at(self, y: float) -> int:
        i = int((y - 6) // self._row_h()) + self.offset
        return i if 0 <= i < len(self.options) else -1

    def mouseMoveEvent(self, e) -> None:  # noqa: N802
        i = self._row_at(e.position().y())
        if i != self.hover:
            self.hover = i
            self.update()

    def mouseReleaseEvent(self, e) -> None:  # noqa: N802
        if not self.rect().contains(e.position().toPoint()):
            self.close()
            return
        i = self._row_at(e.position().y())
        if i >= 0:
            self.chosen.emit(self.options[i][0])
            self.close()

    def wheelEvent(self, e) -> None:  # noqa: N802
        rows = min(len(self.options), self.MAX_ROWS)
        step = -1 if e.angleDelta().y() > 0 else 1
        self.offset = max(0, min(self.offset + step, len(self.options) - rows))
        self.update()

    def keyPressEvent(self, e: QKeyEvent) -> None:  # noqa: N802
        rows = min(len(self.options), self.MAX_ROWS)
        if e.key() in (Qt.Key_Down, Qt.Key_Tab):
            self.hover = min(len(self.options) - 1, max(0, self.hover) + 1)
        elif e.key() in (Qt.Key_Up, Qt.Key_Backtab):
            self.hover = max(0, self.hover - 1)
        elif e.key() in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space):
            if self.hover >= 0:
                self.chosen.emit(self.options[self.hover][0])
            self.close()
            return
        elif e.key() == Qt.Key_Escape:
            self.close()
            return
        else:
            return super().keyPressEvent(e)
        if self.hover < self.offset:
            self.offset = self.hover
        elif self.hover >= self.offset + rows:
            self.offset = self.hover - rows + 1
        self.update()

    def paintEvent(self, _e) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        rounded(p, QRectF(self.rect()), T.R_LG, T.S2)
        rh = self._row_h()
        rows = min(len(self.options), self.MAX_ROWS)
        for vis in range(rows):
            i = vis + self.offset
            opt = self.options[i]
            r = QRectF(6, 6 + vis * rh, self.width() - 12, rh)
            if i == self.hover:
                rounded(p, r, T.R_SM, T.S3)
            selected = opt[0] == self.owner.value
            if selected:
                icons.draw(p, "check", QRectF(r.right() - 24, r.center().y() - 8, 16, 16), T.FG)
            p.setFont(self._font)
            p.setPen(T.FG)
            has_detail = len(opt) > 2 and opt[2]
            if has_detail:
                p.drawText(QRectF(r.left() + 10, r.top() + 5, r.width() - 40, 18), Qt.AlignLeft | Qt.AlignVCenter,
                           opt[1])
                p.setFont(self._detail_font)
                p.setPen(T.FG3)
                p.drawText(QRectF(r.left() + 10, r.top() + 22, r.width() - 40, 14), Qt.AlignLeft | Qt.AlignVCenter,
                           opt[2])
            else:
                p.drawText(r.adjusted(10, 0, -34, 0), Qt.AlignLeft | Qt.AlignVCenter, opt[1])
        if len(self.options) > rows:
            # Scroll position as a short bar on the right edge, inside the radius.
            track_h = self.height() - 24
            bar_h = max(18, track_h * rows / len(self.options))
            y = 12 + (track_h - bar_h) * self.offset / max(1, len(self.options) - rows)
            rounded(p, QRectF(self.width() - 5, y, 2.5, bar_h), 1.2, T.S4)


class Select(QAbstractButton):
    changed = Signal(str)

    def __init__(self, options: list[tuple], value: str | None = None, parent: QWidget | None = None,
                 min_width: int = 120, placeholder: str = "") -> None:
        super().__init__(parent)
        self.options = [tuple(o) for o in options]
        self.value = value if value is not None else (self.options[0][0] if self.options else "")
        self.placeholder = placeholder
        self._hover = _Hover(self)
        self._font = T.font(13, 400)
        self._min_w = min_width
        self._popup: _SelectPopup | None = None
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.TabFocus)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        self.setFixedHeight(T.CONTROL_H)
        self.clicked.connect(self.open_popup)

    def _index(self, value: str) -> int:
        for i, o in enumerate(self.options):
            if o[0] == value:
                return i
        return -1

    def set_options(self, options: list[tuple], value: str | None = None) -> None:
        self.options = [tuple(o) for o in options]
        if value is not None:
            self.value = value
        elif self._index(self.value) < 0 and self.options:
            self.value = self.options[0][0]
        self.updateGeometry()
        self.update()

    def set_value(self, value: str, emit: bool = False) -> None:
        self.value = value
        self.update()
        if emit:
            self.changed.emit(value)

    def current_label(self) -> str:
        i = self._index(self.value)
        return self.options[i][1] if i >= 0 else self.placeholder

    def sizeHint(self) -> QSize:  # noqa: N802
        w = max([T.text_width(self._font, o[1]) for o in self.options] or [40])
        return QSize(max(self._min_w, int(w + 46)), T.CONTROL_H)

    def enterEvent(self, e) -> None:  # noqa: N802
        self._hover.to(1)

    def leaveEvent(self, e) -> None:  # noqa: N802
        self._hover.to(0)

    def keyPressEvent(self, e: QKeyEvent) -> None:  # noqa: N802
        if e.key() in (Qt.Key_Down, Qt.Key_Up, Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space):
            self.open_popup()
        else:
            super().keyPressEvent(e)

    def open_popup(self) -> None:
        if not self.options:
            return
        self._popup = _SelectPopup(self)
        self._popup.chosen.connect(self._choose)
        top_left = self.mapToGlobal(QPoint(0, 0))
        self._popup.popup_at(QRect(top_left, self.size()))

    def _choose(self, key: str) -> None:
        if key != self.value:
            self.value = key
            self.update()
            self.changed.emit(key)

    def paintEvent(self, _e) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        h = max(self._hover.value, 1.0 if self.hasFocus() else 0.0)
        rounded(p, QRectF(self.rect()), T.R_MD, mix(T.S2, T.S3, h))
        p.setFont(self._font)
        p.setPen(T.FG if self.isEnabled() else T.FG4)
        text = self.current_label()
        fm = QFontMetricsF(self._font)
        avail = self.width() - 40
        p.drawText(QRectF(10, 0, avail, self.height()), Qt.AlignVCenter | Qt.AlignLeft,
                   fm.elidedText(text, Qt.ElideRight, avail))
        icons.draw(p, "chevron-down", QRectF(self.width() - 26, self.height() / 2 - 8, 16, 16), T.FG2)


# ======================================================================= toggle

class Toggle(QAbstractButton):
    def __init__(self, checked: bool = False, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setCheckable(True)
        self.setChecked(checked)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.TabFocus)
        self.setFixedSize(36, 20)
        self._pos = 1.0 if checked else 0.0
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(150)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)
        self._anim.valueChanged.connect(self._tick)
        self.toggled.connect(self._animate)
        self._hover = _Hover(self)

    def _tick(self, v) -> None:
        self._pos = float(v)
        self.update()

    def _animate(self, on: bool) -> None:
        self._anim.stop()
        self._anim.setStartValue(self._pos)
        self._anim.setEndValue(1.0 if on else 0.0)
        self._anim.start()

    def set_checked_silent(self, on: bool) -> None:
        self.blockSignals(True)
        self.setChecked(on)
        self.blockSignals(False)
        self._anim.stop()
        self._pos = 1.0 if on else 0.0
        self.update()

    def enterEvent(self, e) -> None:  # noqa: N802
        self._hover.to(1)

    def leaveEvent(self, e) -> None:  # noqa: N802
        self._hover.to(0)

    def hitButton(self, pos) -> bool:  # noqa: N802
        return self.rect().contains(pos)

    def paintEvent(self, _e) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        t = self._pos
        off_track = mix(T.S4, QColor("#353535"), max(self._hover.value, 1.0 if self.hasFocus() else 0))
        track = mix(off_track, T.FG, t)
        if not self.isEnabled():
            track = T.S3
        r = QRectF(self.rect())
        rounded(p, r, r.height() / 2, track)
        knob_d = r.height() - 6
        x = 3 + (r.width() - 6 - knob_d) * t
        knob = mix(T.FG2, T.BG, t)
        if not self.isEnabled():
            knob = T.FG4
        p.setBrush(knob)
        p.drawEllipse(QRectF(x, 3, knob_d, knob_d))


# ======================================================================= stepper

class Stepper(QWidget):
    valueChanged = Signal(int)

    def __init__(self, value: int, lo: int, hi: int, suffix: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.lo, self.hi, self.suffix = lo, hi, suffix
        self.value = max(lo, min(hi, value))
        lay = QHBoxLayout(self)
        lay.setContentsMargins(2, 2, 2, 2)
        lay.setSpacing(0)
        self.minus = IconButton("minus", "Less", size=28)
        self.plus = IconButton("plus", "More", size=28)
        self.label = Label("", 13, 500, tabular=True)
        self.label.setAlignment(Qt.AlignCenter)
        self.label.setMinimumWidth(44)
        lay.addWidget(self.minus)
        lay.addWidget(self.label, 1)
        lay.addWidget(self.plus)
        self.minus.clicked.connect(lambda: self.set_value(self.value - 1, emit=True))
        self.plus.clicked.connect(lambda: self.set_value(self.value + 1, emit=True))
        self.setFixedHeight(T.CONTROL_H)
        self._sync()

    def set_value(self, v: int, emit: bool = False) -> None:
        v = max(self.lo, min(self.hi, int(v)))
        changed = v != self.value
        self.value = v
        self._sync()
        if emit and changed:
            self.valueChanged.emit(v)

    def _sync(self) -> None:
        self.label.setText(f"{self.value}{self.suffix}")
        self.minus.setEnabled(self.value > self.lo)
        self.plus.setEnabled(self.value < self.hi)

    def paintEvent(self, _e) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        rounded(p, QRectF(self.rect()), T.R_MD, T.S2)


# ======================================================================= text field

class TextField(QLineEdit):
    def __init__(self, text: str = "", placeholder: str = "", parent: QWidget | None = None, mono: bool = False,
                 height: int = T.CONTROL_H, px: float = 13) -> None:
        super().__init__(text, parent)
        self.setPlaceholderText(placeholder)
        self.setFixedHeight(height)
        self.setFont(T.font(px, 400, mono))
        pal = self.palette()
        pal.setColor(QPalette.PlaceholderText, T.FG3)
        self.setPalette(pal)
        self.setStyleSheet(f"font-family: '{T.MONO if mono else T.SANS}'; font-size: {int(px)}px;")


# ======================================================================= panels and misc

class Surface(QWidget):
    """A filled, rounded region. The only way Ingest groups things besides spacing."""

    def __init__(self, color: QColor = T.S1, radius: int = T.R_LG, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.color = color
        self.radius = radius

    def paintEvent(self, _e) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        rounded(p, QRectF(self.rect()), self.radius, self.color)


class Spinner(QWidget):
    def __init__(self, size: int = 16, parent: QWidget | None = None, color: QColor | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(size, size)
        self.angle = 0.0
        self.color = color or T.FG
        self._timer = QTimer(self)
        self._timer.setInterval(16)
        self._timer.timeout.connect(self._step)

    def _step(self) -> None:
        self.angle = (self.angle + 7) % 360
        self.update()

    def showEvent(self, e) -> None:  # noqa: N802
        self._timer.start()
        super().showEvent(e)

    def hideEvent(self, e) -> None:  # noqa: N802
        self._timer.stop()
        super().hideEvent(e)

    def paintEvent(self, _e) -> None:  # noqa: N802
        p = QPainter(self)
        icons.draw_spinner(p, QRectF(self.rect()), self.angle, self.color)


def make_menu(parent: QWidget | None = None) -> QMenu:
    m = QMenu(parent)
    m.setWindowFlags(m.windowFlags() | Qt.FramelessWindowHint | Qt.NoDropShadowWindowHint)
    m.setAttribute(Qt.WA_TranslucentBackground)
    return m


class Toast(QWidget):
    """Inverted message pill at the bottom of the window. Optional single action."""

    def __init__(self, host: QWidget) -> None:
        super().__init__(host)
        self.host = host
        self.text = ""
        self.action_text = ""
        self._action = None
        self._font = T.font(13, 500)
        self._afont = T.font(13, 600)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._hide)
        self._opacity = 0.0
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(160)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)
        self._anim.valueChanged.connect(self._tick)
        self._anim.finished.connect(self._done)
        self.setCursor(Qt.ArrowCursor)
        self.hide()
        host.installEventFilter(self)

    def eventFilter(self, obj, ev) -> bool:  # noqa: N802
        if obj is self.host and ev.type() == QEvent.Resize and self.isVisible():
            self._place()
        return False

    def show_message(self, text: str, action: str = "", callback=None, ms: int = 3200) -> None:
        self.text, self.action_text, self._action = text, action, callback
        self._place()
        self.show()
        self.raise_()
        self._anim.stop()
        self._anim.setStartValue(self._opacity)
        self._anim.setEndValue(1.0)
        self._anim.start()
        self._timer.start(ms + (2500 if action else 0))

    def _place(self) -> None:
        w = T.text_width(self._font, self.text) + 36
        if self.action_text:
            w += T.text_width(self._afont, self.action_text) + 28
        w = min(int(w), self.host.width() - 48)
        h = 40
        self.setGeometry((self.host.width() - w) // 2, self.host.height() - h - 28, w, h)

    def _hide(self) -> None:
        self._anim.stop()
        self._anim.setStartValue(self._opacity)
        self._anim.setEndValue(0.0)
        self._anim.start()

    def _tick(self, v) -> None:
        self._opacity = float(v)
        self.update()

    def _done(self) -> None:
        if self._opacity <= 0.01:
            self.hide()

    def mousePressEvent(self, e) -> None:  # noqa: N802
        if self._action and e.position().x() > self.width() - T.text_width(self._afont, self.action_text) - 40:
            cb = self._action
            self._action = None
            self._hide()
            cb()

    def paintEvent(self, _e) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setOpacity(self._opacity)
        rect = QRectF(self.rect())
        # 8 px of travel as it appears, nothing more.
        p.translate(0, (1 - self._opacity) * 8)
        rounded(p, rect, T.R_LG, T.FG)
        p.setPen(T.BG)
        p.setFont(self._font)
        fm = QFontMetricsF(self._font)
        right = 18
        if self.action_text:
            aw = T.text_width(self._afont, self.action_text)
            p.setFont(self._afont)
            p.drawText(QRectF(rect.right() - aw - 18, 0, aw + 2, rect.height()), Qt.AlignVCenter, self.action_text)
            right = aw + 40
            p.setFont(self._font)
        avail = rect.width() - 18 - right
        p.drawText(QRectF(18, 0, avail, rect.height()), Qt.AlignVCenter | Qt.AlignLeft,
                   fm.elidedText(self.text, Qt.ElideRight, avail))


def hbox(*items, spacing: int = 8, margins=(0, 0, 0, 0)) -> QHBoxLayout:
    lay = QHBoxLayout()
    lay.setSpacing(spacing)
    lay.setContentsMargins(*margins)
    for it in items:
        if it is None:
            lay.addStretch(1)
        elif isinstance(it, int):
            lay.addSpacing(it)
        elif isinstance(it, QWidget):
            lay.addWidget(it)
        else:
            lay.addLayout(it)
    return lay


def vbox(*items, spacing: int = 8, margins=(0, 0, 0, 0)) -> QVBoxLayout:
    lay = QVBoxLayout()
    lay.setSpacing(spacing)
    lay.setContentsMargins(*margins)
    for it in items:
        if it is None:
            lay.addStretch(1)
        elif isinstance(it, int):
            lay.addSpacing(it)
        elif isinstance(it, QWidget):
            lay.addWidget(it)
        else:
            lay.addLayout(it)
    return lay


def clear_layout(lay) -> None:
    while lay.count():
        item = lay.takeAt(0)
        w = item.widget()
        if w is not None:
            w.deleteLater()
        elif item.layout() is not None:
            clear_layout(item.layout())


def cursor_pos() -> QPoint:
    return QCursor.pos()


def app() -> QApplication:
    return QApplication.instance()


class _AutoHide(QObject):
    """Scrollbar handles stay invisible until the area is scrolled or hovered."""

    def __init__(self, area) -> None:
        super().__init__(area)
        self.area = area
        self.bars = [area.verticalScrollBar(), area.horizontalScrollBar()]
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(1100)
        self.timer.timeout.connect(lambda: self._set(False))
        self._on = False
        area.viewport().installEventFilter(self)
        area.installEventFilter(self)
        for b in self.bars:
            b.installEventFilter(self)
            b.valueChanged.connect(lambda _v: self._pulse())

    def _pulse(self) -> None:
        self._set(True)
        if not any(b.underMouse() for b in self.bars):
            self.timer.start()

    def _set(self, on: bool) -> None:
        if on == self._on:
            return
        self._on = on
        for b in self.bars:
            b.setProperty("active", "true" if on else "false")
            b.style().unpolish(b)
            b.style().polish(b)
            b.update()

    def eventFilter(self, obj, ev) -> bool:  # noqa: N802
        t = ev.type()
        if t in (QEvent.Wheel, QEvent.Enter, QEvent.MouseMove) and obj is not None:
            if t != QEvent.MouseMove or obj in self.bars:
                self._pulse()
        elif t == QEvent.Leave and obj in self.bars:
            self.timer.start()
        return False


def autohide_scrollbars(area) -> None:
    if not hasattr(area, "_autohide"):
        area._autohide = _AutoHide(area)
