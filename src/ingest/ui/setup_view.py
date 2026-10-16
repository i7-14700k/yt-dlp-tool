"""First run: install the engines. Also provides the engine rows used in Settings."""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import QFileDialog, QScrollArea, QWidget

from ..core import paths
from ..core.engines import SETUP_KEYS, SPECS, EngineState
from ..core.util import human_bytes
from . import theme as T
from .kit import Button, Label, Toggle, hbox, rounded, vbox

SOURCE_LABEL = {"managed": "Installed by Ingest", "custom": "Custom path", "bundled": "Bundled",
                "system": "From PATH", "": ""}


class MiniBar(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.value = 0.0
        self.indeterminate = False
        self.setFixedHeight(3)

    def set(self, v: float | None) -> None:
        self.indeterminate = v is None
        self.value = v or 0.0
        self.update()

    def paintEvent(self, _e) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect())
        rounded(p, r, 1.5, T.S3)
        if self.indeterminate:
            rounded(p, QRectF(0, 0, r.width() * 0.25, r.height()), 1.5, T.FG2)
        elif self.value > 0:
            rounded(p, QRectF(0, 0, max(3.0, r.width() * min(1.0, self.value)), r.height()), 1.5, T.FG)


class EngineRow(QWidget):
    """Name, purpose, version and actions for one engine."""
    install = Signal(str)
    locate = Signal(str)

    def __init__(self, key: str, compact: bool = False, parent=None) -> None:
        super().__init__(parent)
        spec = SPECS[key]
        self.key = key
        self.name = Label(spec.name, 14, 600)
        self.purpose = Label(spec.purpose, 12, 400, T.FG2)
        self.version = Label("", 12, 400, T.FG3, mono=True)
        self.status = Label("Checking…", 12, 400, T.FG3)
        self.bar = MiniBar()
        self.bar.hide()
        self.action = Button("Install", "secondary")
        self.action.clicked.connect(lambda: self.install.emit(self.key))
        self.locate_btn = Button("Locate", "ghost")
        self.locate_btn.clicked.connect(lambda: self.locate.emit(self.key))
        self.action.setEnabled(False)  # until the first state arrives
        left = vbox(hbox(self.name, self.version, None, spacing=10), self.purpose, self.status, self.bar,
                    spacing=4)
        lay = hbox(left, None, self.locate_btn, self.action, spacing=8, margins=(16, 14, 14, 14))
        self.setLayout(lay)

    def paintEvent(self, _e) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        rounded(p, QRectF(self.rect()), T.R_LG, T.S1)

    def set_state(self, st: EngineState) -> None:
        self.action.setEnabled(True)
        if st.path:
            self.version.setText(st.version or "")
            src = SOURCE_LABEL.get(st.source, st.source)
            if st.error:
                self.status.setText(f"{src}. {st.error}")
            elif st.update:
                self.status.setText(f"{src}. Update available: {st.latest}")
            else:
                self.status.setText(src)
            self.action.setText("Update" if st.update else ("Reinstall" if st.source == "managed" else "Install"))
            self.action.set_kind("primary" if st.update or st.extra.get("too_old") else "secondary")
        else:
            self.version.setText("")
            self.status.setText("Not installed" + ("" if SPECS[self.key].required else ". Optional."))
            self.action.setText("Install")
            self.action.set_kind("primary" if SPECS[self.key].required else "secondary")
        self.status.set_color(T.FG if (not st.path and SPECS[self.key].required) or st.update
                              or st.extra.get("too_old") else T.FG2)

    def set_progress(self, phase: str, done: float, total: float) -> None:
        self.bar.show()
        self.action.setEnabled(False)
        if total > 0:
            self.bar.set(done / total)
            self.status.setText(f"{phase} {human_bytes(done)} of {human_bytes(total)}")
        else:
            self.bar.set(None)
            self.status.setText(f"{phase}…")

    def set_done(self, ok: bool, message: str) -> None:
        self.bar.hide()
        self.action.setEnabled(True)
        self.status.setText(message if ok else f"Failed: {message}")
        self.status.set_color(T.FG2 if ok else T.FG)


class SetupPage(QWidget):
    """Shown on first launch, and whenever a required engine is missing."""
    install_requested = Signal(list)
    locate_requested = Signal(str)
    skip = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        title = Label("Ingest needs a few engines", 22, 600)
        body = Label(
            "yt-dlp does the downloading, FFmpeg converts for Premiere Pro and After Effects, and Deno lets "
            "yt-dlp read YouTube. They come from each project's official GitHub releases, are checked against "
            "the published SHA-256 sums, and live in " + str(paths.engines_dir()) + ".", 13, 400, T.FG2, wrap=True)
        body.setMaximumWidth(620)
        self.rows = {k: EngineRow(k) for k in SETUP_KEYS}
        for r in self.rows.values():
            r.install.connect(lambda k: self.install_requested.emit([k]))
            r.locate.connect(self.locate_requested.emit)
            r.setMaximumWidth(620)
        self.gallery = Toggle(True)
        gal = hbox(Label("Also install gallery-dl for image galleries", 13), None, self.gallery)
        gal_w = QWidget()
        gal_w.setLayout(gal)
        gal_w.setMaximumWidth(620)
        self.go = Button("Install engines", "primary", size="lg")
        self.go.clicked.connect(self._go)
        self.later = Button("Skip for now", "ghost")
        self.later.clicked.connect(self.skip.emit)
        self.message = Label("", 12, 400, T.FG2, wrap=True)
        self.message.setMaximumWidth(620)
        col = vbox(title, body, 12, *self.rows.values(), gal_w, 8, hbox(self.go, self.later, None), self.message,
                   None, spacing=10)
        col_w = QWidget()
        col_w.setLayout(col)
        col_w.setFixedWidth(620)
        inner = QWidget()
        inner.setLayout(hbox(56, col_w, None, margins=(0, 40, 24, 24)))
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setWidget(inner)
        self.setLayout(vbox(scroll))

    def _go(self) -> None:
        keys = [k for k, s in SPECS.items() if s.required]
        if self.gallery.isChecked():
            keys.append("gallerydl")
        self.install_requested.emit(keys)

    def set_states(self, states: dict) -> None:
        for k, st in states.items():
            if k in self.rows:
                self.rows[k].set_state(st)

    def set_busy(self, busy: bool) -> None:
        self.go.setEnabled(not busy)
        self.go.setText("Installing…" if busy else "Install engines")


def pick_engine_file(parent, key: str) -> str:
    spec = SPECS[key]
    filt = f"{spec.name} ({spec.files[0]})" if spec.files[0].endswith(".exe") else "All files (*)"
    path, _ = QFileDialog.getOpenFileName(parent, f"Locate {spec.name}", "", filt)
    return path
