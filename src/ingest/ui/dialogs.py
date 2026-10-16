"""Small modal dialogs drawn in the app's own style."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QRectF, Qt, QTimer
from PySide6.QtGui import QFont, QPainter, QTextCursor
from PySide6.QtWidgets import QDialog, QPlainTextEdit, QWidget

from . import theme as T
from .kit import Button, Label, autohide_scrollbars, hbox, rounded, vbox


class _Base(QDialog):
    def __init__(self, parent: QWidget | None, width: int = 440) -> None:
        super().__init__(parent, Qt.Dialog | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setModal(True)
        self.setFixedWidth(width)
        self._drag = None

    def paintEvent(self, _e) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        rounded(p, QRectF(self.rect()), T.R_LG + 2, T.S2)

    def mousePressEvent(self, e) -> None:  # noqa: N802
        if e.button() == Qt.LeftButton:
            self._drag = e.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, e) -> None:  # noqa: N802
        if self._drag is not None and e.buttons() & Qt.LeftButton:
            self.move(e.globalPosition().toPoint() - self._drag)

    def mouseReleaseEvent(self, e) -> None:  # noqa: N802
        self._drag = None

    def showEvent(self, e) -> None:  # noqa: N802
        if self.parentWidget() is not None:
            pw = self.parentWidget().window()
            g = pw.frameGeometry()
            self.adjustSize()
            self.move(g.center().x() - self.width() // 2, g.center().y() - self.height() // 2 - 40)
        super().showEvent(e)


class Confirm(_Base):
    def __init__(self, parent, title: str, body: str, ok: str = "OK", cancel: str = "Cancel",
                 destructive: bool = False) -> None:
        super().__init__(parent)
        t = Label(title, 16, 600)
        b = Label(body, 13, 400, T.FG2, wrap=True)
        self.ok = Button(ok, "primary")
        self.cancel = Button(cancel, "ghost")
        self.ok.clicked.connect(self.accept)
        self.cancel.clicked.connect(self.reject)
        self.setLayout(vbox(t, b, 8, hbox(None, self.cancel, self.ok), spacing=10, margins=(24, 22, 24, 20)))
        self.ok.setFocus()

    @classmethod
    def ask(cls, parent, title: str, body: str, ok: str = "OK", cancel: str = "Cancel") -> bool:
        return cls(parent, title, body, ok, cancel).exec() == QDialog.Accepted


class LogViewer(_Base):
    """Live view of a job's log file, following new lines."""

    def __init__(self, parent, title: str, path: str) -> None:
        super().__init__(parent, width=880)
        self.path = Path(path) if path else None
        self._pos = 0
        head = Label(title, 15, 600)
        self.file_label = Label(str(path or "No log yet"), 11.5, 400, T.FG3, mono=True)
        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        self.text.setLineWrapMode(QPlainTextEdit.WidgetWidth)
        f = T.font(12, 400, mono=True)
        f.setStyleHint(QFont.Monospace)
        self.text.setFont(f)
        self.text.setStyleSheet(f"QPlainTextEdit {{ font-family: '{T.MONO}'; font-size: 12px;"
                                f" background: {T.hexa(T.BG)}; border-radius: {T.R_MD}px; }}")
        self.text.setMinimumHeight(460)
        autohide_scrollbars(self.text)
        copy = Button("Copy all", "secondary")
        copy.clicked.connect(lambda: (self.text.selectAll(), self.text.copy(),
                                      self.text.moveCursor(QTextCursor.End)))
        close = Button("Close", "primary")
        close.clicked.connect(self.accept)
        self.setLayout(vbox(head, self.file_label, self.text, hbox(None, copy, close), spacing=10,
                            margins=(22, 20, 22, 18)))
        self._timer = QTimer(self)
        self._timer.setInterval(500)
        self._timer.timeout.connect(self._poll)
        self._poll()
        self._timer.start()

    def _poll(self) -> None:
        if self.path is None or not self.path.exists():
            return
        try:
            with open(self.path, "rb") as fh:
                fh.seek(self._pos)
                data = fh.read()
                self._pos = fh.tell()
        except OSError:
            return
        if data:
            at_end = self.text.verticalScrollBar().value() >= self.text.verticalScrollBar().maximum() - 4
            text = data.decode("utf-8", "replace")
            # Progress lines are noise in a log view.
            text = "\n".join(ln for ln in text.splitlines() if not ln.startswith("@@P "))
            self.text.appendPlainText(text)
            if at_end:
                self.text.moveCursor(QTextCursor.End)
