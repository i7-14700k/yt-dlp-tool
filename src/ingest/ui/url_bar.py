"""The link field at the top of the Queue page."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QGuiApplication, QKeySequence, QPalette
from PySide6.QtWidgets import QLineEdit, QWidget

from ..core.util import domain_of, extract_urls
from . import theme as T
from .kit import Button, Label, hbox, make_menu, vbox


class UrlField(QLineEdit):
    submitted = Signal(bool)  # inspect?

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFixedHeight(52)
        self.setPlaceholderText("Paste a link from any site")
        self.setStyleSheet(f"QLineEdit {{ font-family: '{T.SANS}'; font-size: 16px; padding: 0 16px;"
                           f" border-radius: {T.R_LG}px; background: {T.hexa(T.S2)}; }}"
                           f"QLineEdit:focus {{ background: {T.hexa(T.S3)}; }}")
        pal = self.palette()
        pal.setColor(QPalette.PlaceholderText, T.FG3)
        self.setPalette(pal)
        self.setClearButtonEnabled(False)
        self.setDragEnabled(True)

    def keyPressEvent(self, e) -> None:  # noqa: N802
        if e.key() in (Qt.Key_Return, Qt.Key_Enter):
            self.submitted.emit(bool(e.modifiers() & Qt.ShiftModifier))
            return
        if e.key() == Qt.Key_Escape:
            self.clear()
            return
        if e.matches(QKeySequence.Paste):
            self.paste_links()
            return
        super().keyPressEvent(e)

    def paste_links(self, text: str | None = None) -> None:
        text = text if text is not None else QGuiApplication.clipboard().text()
        urls = extract_urls(text)
        if len(urls) > 1 or (urls and not self.text().strip()):
            existing = extract_urls(self.text())
            merged = existing + [u for u in urls if u not in existing]
            self.setText(" ".join(merged))
            self.setCursorPosition(len(self.text()))
        else:
            self.insert(text.replace("\r", " ").replace("\n", " ").strip())

    def contextMenuEvent(self, e) -> None:  # noqa: N802
        m = make_menu(self)
        m.addAction("Paste", self.paste_links)
        m.addAction("Paste and download", lambda: (self.paste_links(), self.submitted.emit(False)))
        m.addSeparator()
        m.addAction("Select all", self.selectAll)
        m.addAction("Clear", self.clear)
        m.exec(e.globalPos())

    def dragEnterEvent(self, e) -> None:  # noqa: N802
        if e.mimeData().hasText() or e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e) -> None:  # noqa: N802
        md = e.mimeData()
        text = md.text() if md.hasText() else " ".join(u.toString() for u in md.urls())
        self.paste_links(text)
        e.acceptProposedAction()


class UrlBar(QWidget):
    submit = Signal(list, bool)  # urls, inspect first

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.field = UrlField()
        self.inspect = Button("Inspect", "secondary", size="lg")
        self.inspect.setFixedHeight(52)
        self.inspect.setToolTip("Look at formats, trim, and choose before downloading  (Shift+Enter)")
        self.go = Button("Download", "primary", size="lg")
        self.go.setFixedHeight(52)
        self.go.setToolTip("Download with the Output settings  (Enter)")
        self.hint = Label("", 12, 400, T.FG3)
        self.hint.setTextFormat(Qt.RichText)
        self.hint.setTextInteractionFlags(Qt.LinksAccessibleByMouse)
        self.hint.linkActivated.connect(self._on_link)
        self._clip_url = ""
        lay = vbox(hbox(self.field, self.inspect, self.go, spacing=8), self.hint, spacing=10)
        self.setLayout(lay)
        self.field.submitted.connect(self._submit)
        self.field.textChanged.connect(self._update_hint)
        self.inspect.clicked.connect(lambda: self._submit(True))
        self.go.clicked.connect(lambda: self._submit(False))
        self._update_hint()

    def _submit(self, inspect: bool) -> None:
        urls = extract_urls(self.field.text())
        if not urls and self.field.text().strip():
            raw = self.field.text().strip()
            if "." in raw and " " not in raw:
                urls = ["https://" + raw]
        if not urls:
            self.field.setFocus()
            self._update_hint(error="That doesn't look like a link. Links start with https:// or www.")
            return
        self.submit.emit(urls, inspect)

    def clear(self) -> None:
        self.field.clear()
        self._update_hint()

    def suggest_clipboard(self, url: str) -> None:
        self._clip_url = url
        self._update_hint()

    def _on_link(self, href: str) -> None:
        if href == "paste" and self._clip_url:
            self.field.paste_links(self._clip_url)
            self._clip_url = ""
            self._submit(False)
        elif href == "paste-inspect" and self._clip_url:
            self.field.paste_links(self._clip_url)
            self._clip_url = ""
            self._submit(True)

    def _update_hint(self, *_a, error: str = "") -> None:
        link = f"color:{T.hexa(T.FG)};text-decoration:none;font-weight:600"
        if error:
            self.hint.setText(f"<span style='color:{T.hexa(T.FG)}'>{error}</span>")
            return
        urls = extract_urls(self.field.text())
        if len(urls) > 1:
            self.hint.setText(f"{len(urls)} links. Enter downloads all of them with the Output settings.")
        elif self._clip_url and not self.field.text().strip():
            dom = domain_of(self._clip_url)
            short = self._clip_url if len(self._clip_url) < 64 else self._clip_url[:61] + "…"
            self.hint.setText(f"Copied link from {dom}: <span style='color:{T.hexa(T.FG2)}'>{_esc(short)}</span>"
                              f"&nbsp;&nbsp;<a href='paste' style='{link}'>Download</a>"
                              f"&nbsp;&nbsp;<a href='paste-inspect' style='{link}'>Inspect</a>")
        else:
            self.hint.setText("Enter downloads with the Output settings. Shift+Enter inspects formats first. "
                              "Links and .txt files can be dropped anywhere.")


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
