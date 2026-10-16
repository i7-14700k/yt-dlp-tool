"""Inspect: read a link first, then choose formats, trim, subtitles or playlist entries."""

from __future__ import annotations

import threading

from PySide6.QtCore import QObject, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QFontMetricsF, QPainter
from PySide6.QtWidgets import QScrollArea, QSizePolicy, QStackedWidget, QWidget

from ..core import conform
from ..core.errors import Failure, classify
from ..core.procs import Proc
from ..core.settings import OutputOptions, copy_options
from ..core.util import (domain_of, fps_label, human_bytes, human_duration, parse_timecode, short_res, timecode)
from ..core.ytdlp import Attempt, Network, Tools, codec_family, load_info, resolve_args
from . import icons, theme as T
from .kit import Button, Label, Spinner, TextField, focus_mark, hbox, kbd_focus, rounded, vbox
from .output_panel import OutputPanel
from .thumbs import ThumbCache, draw_thumb


def _format_bytes(f: dict | None, duration: float) -> float:
    if not f:
        return 0.0
    size = f.get("filesize") or f.get("filesize_approx")
    if size:
        return float(size)
    return float(f.get("tbr") or 0) * 125 * duration


class Resolver(QObject):
    done = Signal(object, object)  # info dict | None, Failure | None
    status = Signal(str)

    def __init__(self, parent=None, get_nightly=None, allow_nightly=None) -> None:
        super().__init__(parent)
        self._proc: Proc | None = None
        self._gen = 0
        self.get_nightly = get_nightly or (lambda: "")
        self.allow_nightly = allow_nightly or (lambda: False)

    def start(self, url: str, playlist: bool, tools: Tools, net: Network, opts: OutputOptions) -> None:
        self.cancel()
        self._gen += 1
        gen = self._gen
        threading.Thread(target=self._run, args=(gen, url, playlist, tools, net, opts), daemon=True).start()

    def cancel(self) -> None:
        self._gen += 1
        if self._proc is not None:
            self._proc.kill()

    def _run(self, gen, url, playlist, tools, net, opts) -> None:
        if not tools.ytdlp:
            self.done.emit(None, Failure("engine", "yt-dlp isn't installed", "Install it in Settings > Engines."))
            return
        attempt = Attempt()
        failure = None
        for _ in range(4):
            proc = Proc(resolve_args(tools, net, attempt, url, opts, playlist), merge_stderr=False)
            self._proc = proc
            raw = proc.read_all()
            code = proc.wait()
            self._proc = None
            if gen != self._gen:
                return
            if code == 0:
                try:
                    self.done.emit(load_info(raw.decode("utf-8", "replace")), None)
                    return
                except ValueError:
                    pass
            failure = classify(proc.stderr_lines, code)
            if failure.retry_with == "cookies" and net.has_cookies and not attempt.cookies:
                attempt.cookies = True
                continue
            if failure.kind in ("forbidden", "impersonate") and net.impersonate != "off" and not attempt.impersonate:
                attempt.impersonate = True
                continue
            if failure.retry_with == "update" and not attempt.nightly and self.allow_nightly():
                self.status.emit("Site changed, trying yt-dlp nightly")
                path = self.get_nightly()
                if gen != self._gen:
                    return
                if path:
                    attempt.nightly = True
                    tools = Tools(ytdlp=path, ffmpeg=tools.ffmpeg, ffprobe=tools.ffprobe, deno=tools.deno,
                                  gallerydl=tools.gallerydl)
                    continue
            break
        if gen == self._gen:
            self.done.emit(None, failure)


class Table(QWidget):
    """Selectable rows with columns. First row may be 'Automatic'."""
    changed = Signal(str)
    ROW = 32

    MIN_STRETCH = 150  # room for "1080p 24 H.264, copy"

    def __init__(self, columns: list[tuple[str, float, int]], drop: tuple[int, ...] = (), parent=None) -> None:
        super().__init__(parent)
        self.columns = columns  # (title, width or 0 for stretch, alignment)
        self.drop = drop  # column indexes to hide first when the table gets narrow
        self.rows: list[tuple[str, list[str], bool]] = []
        self.value = ""
        self.hover = -1
        self._f = T.font(12.5, 400, tabular=True)
        self._fh = T.font(11.5, 500)
        self._fm = T.font(12, 400, mono=True)
        self.setMouseTracking(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.TabFocus)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def keyPressEvent(self, e) -> None:  # noqa: N802
        keys = [k for k, _c, _d in self.rows]
        if not keys:
            return super().keyPressEvent(e)
        i = keys.index(self.value) if self.value in keys else 0
        target = {Qt.Key_Up: i - 1, Qt.Key_Down: i + 1, Qt.Key_Home: 0, Qt.Key_End: len(keys) - 1}.get(e.key())
        if target is None:
            return super().keyPressEvent(e)
        target = max(0, min(len(keys) - 1, target))
        if keys[target] != self.value:
            self.value = keys[target]
            self.update()
            self.changed.emit(self.value)

    def focusInEvent(self, e) -> None:  # noqa: N802
        self.update()
        super().focusInEvent(e)

    def focusOutEvent(self, e) -> None:  # noqa: N802
        self.update()
        super().focusOutEvent(e)

    def set_rows(self, rows: list[tuple[str, list[str], bool]], value: str) -> None:
        self.rows = rows
        self.value = value
        self.setFixedHeight(24 + self.ROW * len(rows) + 4)
        self.updateGeometry()
        self.update()

    def _row_at(self, y: float) -> int:
        i = int((y - 24) // self.ROW)
        return i if 0 <= i < len(self.rows) else -1

    def mouseMoveEvent(self, e) -> None:  # noqa: N802
        i = self._row_at(e.position().y())
        if i != self.hover:
            self.hover = i
            self.update()

    def leaveEvent(self, e) -> None:  # noqa: N802
        self.hover = -1
        self.update()

    def mousePressEvent(self, e) -> None:  # noqa: N802
        i = self._row_at(e.position().y())
        if i >= 0 and self.rows[i][0] != self.value:
            self.value = self.rows[i][0]
            self.update()
            self.changed.emit(self.value)

    def _col_rects(self) -> list[QRectF | None]:
        """One rect per column, None for columns hidden to make room."""
        hidden: set[int] = set()
        stretch_n = sum(1 for _, w, _ in self.columns if not w) or 1

        def room() -> float:
            fixed = sum(w for i, (_, w, _) in enumerate(self.columns) if w and i not in hidden)
            return (self.width() - 24 - fixed) / stretch_n
        for i in self.drop:
            if room() >= self.MIN_STRETCH:
                break
            hidden.add(i)
        sw = max(60.0, room())
        x = 12.0
        out: list[QRectF | None] = []
        for i, (_, w, _) in enumerate(self.columns):
            if i in hidden:
                out.append(None)
                continue
            cw = w or sw
            out.append(QRectF(x, 0, cw, 0))
            x += cw
        return out

    def paintEvent(self, _e) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        cols = self._col_rects()
        p.setFont(self._fh)
        p.setPen(T.FG3)
        for (title, _w, align), c in zip(self.columns, cols):
            if c is not None:
                p.drawText(QRectF(c.left(), 0, c.width() - 8, 20), align | Qt.AlignVCenter, title)
        for i, (key, cells, dim) in enumerate(self.rows):
            r = QRectF(0, 24 + i * self.ROW, self.width(), self.ROW - 2)
            sel = key == self.value
            if sel:
                rounded(p, r, T.R_MD, T.FG)
                if kbd_focus(self):
                    focus_mark(p, r, T.BG, inset=12)
            elif i == self.hover:
                rounded(p, r, T.R_MD, T.S2)
            for j, ((_t, _w, align), c) in enumerate(zip(self.columns, cols)):
                if c is None:
                    continue
                text = cells[j] if j < len(cells) else ""
                p.setFont(self._fm if j == len(cols) - 1 else self._f)
                if sel:
                    p.setPen(T.BG if j < len(cols) - 1 else T.INV_FG2)
                else:
                    p.setPen((T.FG3 if dim else T.FG) if j < len(cols) - 1 else T.FG3)
                cell = QRectF(c.left(), r.top(), c.width() - 8, r.height())
                p.drawText(cell, align | Qt.AlignVCenter,
                           QFontMetricsF(p.font()).elidedText(text, Qt.ElideRight, cell.width()))


class Chips(QWidget):
    """Wrapping row of toggleable chips (subtitle languages)."""
    changed = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.items: list[tuple[str, str]] = []
        self.selected: set[str] = set()
        self._rects: list[QRectF] = []
        self._f = T.font(12, 500)
        self.hover = -1
        self.setMouseTracking(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def set_items(self, items: list[tuple[str, str]], selected: set[str]) -> None:
        self.items = items
        self.selected = set(selected)
        self._layout()
        self.update()

    def _layout(self) -> None:
        x = y = 0.0
        self._rects = []
        for _k, label in self.items:
            w = T.text_width(self._f, label) + 20
            if x + w > self.width() and x > 0:
                x, y = 0.0, y + 32
            self._rects.append(QRectF(x, y, w, 26))
            x += w + 6
        self.setFixedHeight(int(y + 26) if self.items else 0)

    def resizeEvent(self, e) -> None:  # noqa: N802
        self._layout()
        super().resizeEvent(e)

    def mouseMoveEvent(self, e) -> None:  # noqa: N802
        i = next((i for i, r in enumerate(self._rects) if r.contains(e.position())), -1)
        if i != self.hover:
            self.hover = i
            self.update()

    def leaveEvent(self, e) -> None:  # noqa: N802
        self.hover = -1
        self.update()

    def mousePressEvent(self, e) -> None:  # noqa: N802
        for (k, _l), r in zip(self.items, self._rects):
            if r.contains(e.position()):
                self.selected.symmetric_difference_update({k})
                self.update()
                self.changed.emit()

    def paintEvent(self, _e) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setFont(self._f)
        for i, ((k, label), r) in enumerate(zip(self.items, self._rects)):
            on = k in self.selected
            rounded(p, r, T.R_SM + 1, T.FG if on else (T.S3 if i == self.hover else T.S2))
            p.setPen(T.BG if on else T.FG2)
            p.drawText(r, Qt.AlignCenter, label)


class Thumb(QWidget):
    def __init__(self, thumbs: ThumbCache, w: int = 320, h: int = 180, parent=None) -> None:
        super().__init__(parent)
        self.thumbs = thumbs
        self.url = ""
        self.setFixedSize(w, h)
        thumbs.loaded.connect(lambda u: self.update() if u == self.url else None)

    def set_url(self, url: str) -> None:
        self.url = url
        self.update()

    def paintEvent(self, _e) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        draw_thumb(p, QRectF(self.rect()), self.thumbs.get(self.url) if self.url else None, T.R_MD, T.S2)


class EntryList(QWidget):
    """Playlist entries with checkboxes, painted."""
    changed = Signal()
    ROW = 52

    def __init__(self, thumbs: ThumbCache, parent=None) -> None:
        super().__init__(parent)
        self.thumbs = thumbs
        self.entries: list[dict] = []
        self.checked: list[bool] = []
        self.hover = -1
        self._anchor = -1
        self._f = T.font(13, 500)
        self._f2 = T.font(12, 400, tabular=True)
        self._fi = T.font(12, 400, mono=True)
        self.setMouseTracking(True)
        self.setCursor(Qt.PointingHandCursor)
        thumbs.loaded.connect(lambda _u: self.update())

    def set_entries(self, entries: list[dict]) -> None:
        self.entries = entries
        self.checked = [True] * len(entries)
        self.setFixedHeight(self.ROW * len(entries) + 4)
        self.update()

    def set_all(self, on: bool) -> None:
        self.checked = [on] * len(self.entries)
        self.update()
        self.changed.emit()

    def set_range(self, indexes: set[int]) -> None:
        self.checked = [(i + 1) in indexes for i in range(len(self.entries))]
        self.update()
        self.changed.emit()

    def mouseMoveEvent(self, e) -> None:  # noqa: N802
        i = int(e.position().y() // self.ROW)
        i = i if 0 <= i < len(self.entries) else -1
        if i != self.hover:
            self.hover = i
            self.update()

    def leaveEvent(self, e) -> None:  # noqa: N802
        self.hover = -1
        self.update()

    def mousePressEvent(self, e) -> None:  # noqa: N802
        i = int(e.position().y() // self.ROW)
        if not 0 <= i < len(self.entries):
            return
        if e.modifiers() & Qt.ShiftModifier and self._anchor >= 0:
            a, b = sorted((self._anchor, i))
            state = self.checked[self._anchor]
            for k in range(a, b + 1):
                self.checked[k] = state
        else:
            self.checked[i] = not self.checked[i]
            self._anchor = i
        self.update()
        self.changed.emit()

    def paintEvent(self, e) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        first = max(0, int(e.rect().top() // self.ROW))
        last = min(len(self.entries), int(e.rect().bottom() // self.ROW) + 1)
        for i in range(first, last):
            ent = self.entries[i]
            r = QRectF(0, i * self.ROW, self.width(), self.ROW - 4)
            if i == self.hover:
                rounded(p, r, T.R_MD, T.S1)
            box = QRectF(12, r.center().y() - 8, 16, 16)
            on = self.checked[i]
            rounded(p, box, 4, T.FG if on else T.S3)
            if on:
                icons.draw(p, "check", box, T.BG, 1.8)
            p.setFont(self._fi)
            p.setPen(T.FG3)
            p.drawText(QRectF(36, r.top(), 40, r.height()), Qt.AlignRight | Qt.AlignVCenter, str(i + 1))
            tr = QRectF(88, r.top() + 4, 72, r.height() - 8)
            draw_thumb(p, tr, self.thumbs.get(ent.get("thumbnail") or "") if ent.get("thumbnail") else None,
                       T.R_SM, T.S2)
            p.setFont(self._f)
            p.setPen(T.FG if on else T.FG3)
            avail = self.width() - 172 - 90
            p.drawText(QRectF(172, r.top(), avail, r.height()), Qt.AlignLeft | Qt.AlignVCenter,
                       QFontMetricsF(self._f).elidedText(ent.get("title") or ent.get("url", ""), Qt.ElideRight,
                                                         avail))
            p.setFont(self._f2)
            p.setPen(T.FG3)
            p.drawText(QRectF(self.width() - 90, r.top(), 76, r.height()), Qt.AlignRight | Qt.AlignVCenter,
                       human_duration(ent.get("duration")))


def parse_ranges(text: str, n: int) -> set[int]:
    out: set[int] = set()
    for part in text.replace(" ", "").split(","):
        if not part:
            continue
        if "-" in part:
            a, _, b = part.partition("-")
            try:
                lo = int(a) if a else 1
                hi = int(b) if b else n
            except ValueError:
                continue
            out.update(range(max(1, lo), min(n, hi) + 1))
        elif part.isdigit():
            if 1 <= int(part) <= n:
                out.add(int(part))
    return out


class InspectPage(QWidget):
    back = Signal()
    add = Signal(list, object, bool, list, bool)  # urls, options, as playlist, metadata, even if in library

    def __init__(self, thumbs: ThumbCache, get_tools, get_net, defaults: OutputOptions, parent=None,
                 get_nightly=None, allow_nightly=None) -> None:
        super().__init__(parent)
        self.thumbs = thumbs
        self.get_tools = get_tools
        self.get_net = get_net
        self.defaults = defaults
        self.opts = copy_options(defaults)
        self.url = ""
        self.info: dict | None = None
        self.resolver = Resolver(self, get_nightly, allow_nightly)
        self.resolver.done.connect(self._on_done)
        self.resolver.status.connect(self._on_status)  # bound method: queued onto the GUI thread

        back = Button("Queue", "ghost", icon="chevron-left")
        back.clicked.connect(self._back)
        self.url_label = Label("", 12, 400, T.FG3, mono=True)
        top = hbox(back, self.url_label, None, spacing=12)

        # states: loading / error / video / playlist
        self.stack = QStackedWidget()
        self.loading = self._build_loading()
        self.error = self._build_error()
        self.video = self._build_video()
        self.playlist = self._build_playlist()
        for w in (self.loading, self.error, self.video, self.playlist):
            self.stack.addWidget(w)

        self.panel = OutputPanel(self.opts)
        self.panel.setFixedWidth(T.PANEL_W)
        self.panel.changed.connect(self._options_changed)

        body = hbox(self.stack, self.panel, spacing=16)
        self.setLayout(vbox(top, body, spacing=12, margins=(24, 8, 24, 20)))

    # ------------------------------------------------------------------ build
    def _build_loading(self) -> QWidget:
        w = QWidget()
        self.spinner = Spinner(18)
        self.loading_text = Label("Reading the page", 14, 500)
        self.loading_sub = Label("yt-dlp is extracting formats. Big playlists take a little longer.", 12, 400,
                                 T.FG3)
        cancel = Button("Cancel", "ghost")
        cancel.clicked.connect(self._back)
        w.setLayout(vbox(24, hbox(self.spinner, self.loading_text, None, spacing=10), self.loading_sub, 8,
                         hbox(cancel, None), None, spacing=6, margins=(12, 0, 0, 0)))
        return w

    def _build_error(self) -> QWidget:
        w = QWidget()
        self.err_title = Label("", 18, 600, wrap=True)
        self.err_hint = Label("", 13, 400, T.FG2, wrap=True)
        self.err_raw = Label("", 12, 400, T.FG3, mono=True, wrap=True)
        self.err_raw.setTextInteractionFlags(Qt.TextSelectableByMouse)
        retry = Button("Try again", "primary")
        retry.clicked.connect(lambda: self.open(self.url, self._playlist_mode))
        anyway = Button("Queue it anyway", "secondary")
        anyway.setToolTip("Add the link to the queue; it will retry with every fallback, including gallery-dl.")
        anyway.clicked.connect(lambda: self.add.emit([self.url], copy_options(self.opts), self._playlist_mode, [],
                                                     True))
        self.err_actions = hbox(retry, anyway, None)
        w.setLayout(vbox(24, self.err_title, self.err_hint, self.err_raw, 8, self.err_actions, None, spacing=8,
                         margins=(12, 0, 24, 0)))
        return w

    def _scroll(self, inner: QWidget) -> QScrollArea:
        sa = QScrollArea()
        sa.setWidgetResizable(True)
        sa.setFrameShape(QScrollArea.NoFrame)
        sa.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        sa.setWidget(inner)
        return sa

    def _build_video(self) -> QWidget:
        inner = QWidget()
        self.v_thumb = Thumb(self.thumbs)
        self.v_title = Label("", 20, 600, wrap=True)
        self.v_title.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.v_meta = Label("", 13, 400, T.FG2, wrap=True)
        self.v_pick = Label("", 12, 400, T.FG3, mono=True, wrap=True)
        self.v_list_note = Label("", 12, 400, T.FG2, wrap=True)
        self.v_list_btn = Button("Open the playlist instead", "secondary")
        self.v_list_btn.clicked.connect(lambda: self.open(self.url, True))
        head_text = vbox(self.v_title, self.v_meta, 6, self.v_pick, None, spacing=6)
        head = hbox(self.v_thumb, head_text, spacing=20)

        self.vtable = Table([("Resolution", 84, Qt.AlignLeft), ("FPS", 56, Qt.AlignLeft),
                             ("Codec", 124, Qt.AlignLeft), ("Range", 64, Qt.AlignLeft),
                             ("Bitrate", 90, Qt.AlignRight), ("Size", 86, Qt.AlignRight),
                             ("", 0, Qt.AlignRight)], drop=(4, 3, 5))
        self.vtable.changed.connect(self._format_changed)
        self.atable = Table([("Codec", 96, Qt.AlignLeft), ("Bitrate", 90, Qt.AlignRight),
                             ("Rate", 86, Qt.AlignRight), ("", 28, Qt.AlignLeft), ("Language", 110, Qt.AlignLeft),
                             ("Size", 86, Qt.AlignRight), ("", 0, Qt.AlignRight)], drop=(3, 1, 2))
        self.atable.changed.connect(self._format_changed)

        self.trim_in = TextField("", "Start", mono=True)
        self.trim_out = TextField("", "End", mono=True)
        for f in (self.trim_in, self.trim_out):
            f.setFixedWidth(150)
            f.textChanged.connect(self._trim_changed)
        self.trim_note = Label("Whole video", 12, 400, T.FG3)
        self.subs = Chips()
        self.subs.changed.connect(self._subs_changed)
        self.subs_note = Label("", 12, 400, T.FG3, wrap=True)
        self.estimate = Label("", 13, 500)
        self.add_btn = Button("Add to queue", "primary", size="lg")
        self.add_btn.clicked.connect(self._add_video)

        self.video_section = vbox(Label("Video", 13, 600), self.vtable, spacing=6)
        self.audio_section = vbox(Label("Audio", 13, 600), self.atable, spacing=6)
        lay = vbox(head, hbox(self.v_list_note, self.v_list_btn, None, spacing=12), 8,
                   self.video_section, 8, self.audio_section, 8,
                   vbox(Label("Trim", 13, 600), hbox(self.trim_in, self.trim_out, self.trim_note, None, spacing=8),
                        spacing=8), 8,
                   vbox(Label("Subtitles", 13, 600), self.subs, self.subs_note, spacing=8),
                   None, spacing=10, margins=(4, 8, 16, 16))
        inner.setLayout(lay)
        self._video_inner = inner
        page = QWidget()
        footer = hbox(self.estimate, None, self.add_btn, margins=(4, 12, 16, 0))
        page.setLayout(vbox(self._scroll(inner), footer, spacing=0))
        return page

    def _build_playlist(self) -> QWidget:
        inner = QWidget()
        self.p_title = Label("", 20, 600, wrap=True)
        self.p_meta = Label("", 13, 400, T.FG2)
        self.entries = EntryList(self.thumbs)
        self.entries.changed.connect(self._entries_changed)
        all_btn = Button("All", "ghost")
        all_btn.clicked.connect(lambda: self.entries.set_all(True))
        none_btn = Button("None", "ghost")
        none_btn.clicked.connect(lambda: self.entries.set_all(False))
        self.range = TextField("", "Range, e.g. 1-10, 15", mono=True)
        self.range.setFixedWidth(200)
        self.range.returnPressed.connect(lambda: self.entries.set_range(parse_ranges(self.range.text(),
                                                                                     len(self.entries.entries))))
        self.p_count = Label("", 12, 400, T.FG2, tabular=True)
        tools = hbox(all_btn, none_btn, self.range, None, self.p_count, spacing=6)
        inner.setLayout(vbox(self.p_title, self.p_meta, 10, tools, self.entries, None, spacing=8,
                             margins=(4, 8, 16, 16)))
        page = QWidget()
        self.p_add = Button("Add to queue", "primary", size="lg")
        self.p_add.clicked.connect(self._add_playlist)
        page.setLayout(vbox(self._scroll(inner), hbox(None, self.p_add, margins=(4, 12, 16, 0)), spacing=0))
        return page

    # ------------------------------------------------------------------ flow
    def open(self, url: str, playlist: bool = False, info: dict | None = None,
             children: list[dict] | None = None) -> None:
        self.url = url
        self._playlist_mode = playlist
        self.url_label.setText(url if len(url) < 110 else url[:107] + "…")
        self.opts = copy_options(self.defaults)
        self.panel.set_options(self.opts)
        if info is not None:
            self._on_done(info, None, children)
            return
        self.stack.setCurrentWidget(self.loading)
        self.loading_text.setText("Reading the page")
        self.resolver.start(url, playlist, self.get_tools(), self.get_net(), self.opts)

    def _back(self) -> None:
        self.resolver.cancel()
        self.back.emit()

    def _on_status(self, text: str) -> None:
        self.loading_text.setText(text)

    def _on_done(self, info: dict | None, failure: Failure | None, children: list[dict] | None = None) -> None:
        if failure is not None or info is None:
            f = failure or Failure("unknown", "Nothing came back", "Try again.")
            self.err_title.setText(f.title)
            self.err_hint.setText(f.hint)
            self.err_raw.setText(f.raw)
            self.stack.setCurrentWidget(self.error)
            return
        self.info = info
        if info.get("_type") in ("playlist", "multi_video") or children:
            self._show_playlist(info, children)
        else:
            self._show_video(info)

    def _show_playlist(self, info: dict, children: list[dict] | None) -> None:
        if children is None:
            children = []
            for i, e in enumerate([e for e in info.get("entries") or [] if e], 1):
                thumbs = e.get("thumbnails") or []
                children.append({
                    "url": e.get("url") or e.get("webpage_url"), "title": e.get("title") or "",
                    "duration": e.get("duration") or 0,
                    "thumbnail": e.get("thumbnail") or (thumbs[-1].get("url") if thumbs else ""),
                    "uploader": e.get("uploader") or info.get("uploader") or "",
                    "playlist": {"title": info.get("title") or "", "index": e.get("playlist_index") or i,
                                 "count": len(info.get("entries") or []), "id": info.get("id") or ""}})
        children = [c for c in children if c.get("url")]
        self.p_title.setText(info.get("title") or "Playlist")
        meta = [x for x in (info.get("uploader") or info.get("channel"), domain_of(self.url),
                            f"{len(children)} items") if x]
        self.p_meta.setText("  ·  ".join(meta))
        self.entries.set_entries(children)
        self._entries_changed()
        self.stack.setCurrentWidget(self.playlist)
        self._relayout(self.entries.parentWidget())

    def _entries_changed(self) -> None:
        n = sum(self.entries.checked)
        total = len(self.entries.entries)
        dur = sum(float(e.get("duration") or 0) for e, c in zip(self.entries.entries, self.entries.checked) if c)
        self.p_count.setText(f"{n} of {total} selected" + (f"  ·  {human_duration(dur)}" if dur else ""))
        self.p_add.setText(f"Add {n} to queue" if n != 1 else "Add 1 to queue")
        self.p_add.setEnabled(n > 0)

    def _add_playlist(self) -> None:
        chosen = [e for e, c in zip(self.entries.entries, self.entries.checked) if c]
        if not chosen:
            return
        # Picked from a list: entries already in the library are still skipped, as for any playlist.
        self.add.emit([e["url"] for e in chosen], copy_options(self.opts), False, chosen, False)

    # ------------------------------------------------------------------ single video
    def _show_video(self, info: dict) -> None:
        self.v_thumb.set_url(info.get("thumbnail") or "")
        self.v_title.setText(info.get("title") or info.get("id") or "")
        meta = [info.get("uploader") or info.get("channel"), info.get("extractor_key"),
                _date(info.get("upload_date")), human_duration(info.get("duration")),
                f"{info['view_count']:,} views" if info.get("view_count") else None]
        if info.get("is_live"):
            meta.insert(0, "Live now")
        self.v_meta.setText("  ·  ".join(str(m) for m in meta if m))
        has_list = "list=" in self.url and not self._playlist_mode
        self.v_list_note.setVisible(has_list)
        self.v_list_btn.setVisible(has_list)
        self.v_list_note.setText("This link is also part of a playlist.")
        fmts = info.get("formats") or []
        vids = [f for f in fmts if f.get("vcodec") not in (None, "none") or
                (f.get("vcodec") is None and f.get("acodec") is None and f.get("height"))]
        auds = [f for f in fmts if f.get("vcodec") == "none" and f.get("acodec") != "none"]
        vids.sort(key=lambda f: (f.get("height") or 0, f.get("fps") or 0, f.get("tbr") or 0), reverse=True)
        auds.sort(key=lambda f: (f.get("abr") or f.get("tbr") or 0), reverse=True)
        self._vids, self._auds = vids, auds
        self._describe_pick(info)
        self.trim_in.setText("")
        self.trim_out.setText("")
        self.trim_in.setPlaceholderText("00:00:00.000")
        self.trim_out.setPlaceholderText(timecode(info.get("duration")) if info.get("duration") else "End")
        subs = info.get("subtitles") or {}
        auto = info.get("automatic_captions") or {}
        items = [(k, k) for k in sorted(subs)]
        self.subs.set_items(items, set())
        self.subs_note.setText(
            ("" if items else "No uploaded subtitles.") +
            (f" {len(auto)} auto-generated languages available; type them under Also save." if auto else ""))
        self._fill_tables()
        self.stack.setCurrentWidget(self.video)

    def _fill_tables(self) -> None:
        info = self.info or {}
        req = self._picked(info) if info.get("format_id") or info.get("requested_formats") else []
        auto_v = next((f for f in req if f.get("vcodec") not in (None, "none")), None)
        auto_a = next((f for f in req if f.get("vcodec") == "none" and f.get("acodec") != "none"), None)
        prof = self.opts.profile
        dur = float(info.get("duration") or 0)
        vrows = [("auto", ["Automatic", "", "", "", "", "", self._auto_desc(auto_v, True)], False)]
        for f in self._vids:
            fam = codec_family(f.get("vcodec"))
            size = f.get("filesize") or f.get("filesize_approx") or (
                (f.get("tbr") or 0) * 125 * dur if f.get("tbr") and dur else 0)
            act = self._action(fam, prof) if fam else "checked later"
            combined = f.get("acodec") not in ("none",) and f.get("vcodec") is None or \
                f.get("acodec") not in (None, "none")
            vrows.append((f["format_id"], [
                short_res(f.get("height"), f.get("width")) or (f.get("format_note") or "?"),
                fps_label(f.get("fps")) if f.get("fps") else "",
                (conform.codec_name(fam) if fam else "Not listed")
                + (" + audio" if combined else ""),
                f.get("dynamic_range") or "SDR",
                f"{f['tbr']:.0f} kb/s" if f.get("tbr") else "",
                human_bytes(size) if size else "",
                act], fam in ("vp9", "av1") and prof == "h264"))
        arows = [("auto", ["Automatic", "", "", "", "", "", self._auto_desc(auto_a, False)], False)]
        for f in self._auds:
            fam = codec_family(f.get("acodec"))
            size = f.get("filesize") or f.get("filesize_approx") or (
                (f.get("abr") or f.get("tbr") or 0) * 125 * dur if dur else 0)
            if not fam:
                act = "checked later"
            elif fam == "aac" and prof in ("h264", "hevc", "aac") and (
                    self.opts.audio_rate == "source" or (f.get("asr") or 0) == 48000):
                act = "copy"
            else:
                act = "convert"
            arows.append((f["format_id"], [
                conform.codec_name(fam) if fam else "Not listed",
                f"{(f.get('abr') or f.get('tbr') or 0):.0f} kb/s",
                f"{(f.get('asr') or 0) / 1000:g} kHz" if f.get("asr") else "", "",
                f.get("language") or "", human_bytes(size) if size else "", act], False))
        self.vtable.set_rows(vrows, self.vtable.value if self.vtable.value in [r[0] for r in vrows] else "auto")
        self.atable.set_rows(arows, self.atable.value if self.atable.value in [r[0] for r in arows] else "auto")
        kind = conform.PROFILES.get(prof, conform.PROFILES["h264"]).kind
        self._set_visible(self.video_section, kind != "audio" and bool(self._vids))
        self._set_visible(self.audio_section, bool(self._auds))
        self._relayout(self._video_inner)
        self._update_estimate()

    @staticmethod
    def _relayout(inner: QWidget) -> None:
        # Children change height while the page may be hidden; make the layout
        # recompute now instead of trusting a deferred request.
        lay = inner.layout()
        lay.invalidate()
        lay.activate()
        inner.adjustSize()

    @staticmethod
    def _set_visible(layout, visible: bool) -> None:
        for i in range(layout.count()):
            w = layout.itemAt(i).widget()
            if w is not None:
                w.setVisible(visible)

    def _auto_desc(self, f: dict | None, video: bool) -> str:
        if not f:
            return ""
        if video:
            fam = codec_family(f.get("vcodec"))
            return f"{short_res(f.get('height'), f.get('width'))} {fps_label(f.get('fps')) or ''} " \
                   f"{conform.codec_name(fam)}, {self._action(fam, self.opts.profile)}".replace("  ", " ")
        fam = codec_family(f.get("acodec"))
        name = conform.codec_name(fam) if fam else (f.get("ext") or "audio").upper()
        return f"{name} {(f.get('abr') or f.get('tbr') or 0):.0f} kb/s"

    @staticmethod
    def _action(fam: str, prof: str) -> str:
        if prof == "original":
            return "keep"
        if prof in ("wav", "aac", "mp3"):
            return "audio only"
        if prof == "h264" and fam == "h264":
            return "copy"
        if prof == "hevc" and fam == "hevc":
            return "copy"
        return "convert"

    def _describe_pick(self, info: dict) -> None:
        req = self._picked(info) if info.get("format_id") or info.get("requested_formats") else []
        v = next((f for f in req if f.get("vcodec") not in (None, "none") or f.get("height")), None)
        a = next((f for f in req if f.get("vcodec") == "none" and f.get("acodec") != "none"), None)
        prof = self.opts.profile
        bits = []
        if v is not None:
            fam = codec_family(v.get("vcodec"))
            bits.append(" ".join(x for x in (short_res(v.get("height"), v.get("width")),
                                             fps_label(v.get("fps")) if v.get("fps") else "",
                                             conform.codec_name(fam) if fam else (v.get("ext") or "").upper()) if x))
        if a is not None:
            fam = codec_family(a.get("acodec"))
            bits.append((conform.codec_name(fam) if fam else "audio") +
                        (f" {a['abr']:.0f} kb/s" if a.get("abr") else ""))
        line = "Best available: " + " + ".join(bits) if bits else ""
        fam = codec_family((v or {}).get("vcodec"))
        target = conform.PROFILES.get(prof, conform.PROFILES["h264"])
        if prof == "original":
            plan = "Saved exactly as served."
        elif target.kind == "audio":
            plan = f"Audio extracted to {target.label}."
        elif prof in ("h264", "hevc") and fam == prof:
            plan = "Video is copied without re-encoding, then checked for constant frame rate."
        elif fam in ("vp9", "av1", "vp8"):
            plan = f"{conform.codec_name(fam)} doesn't open in Premiere, so it's converted to {target.label}."
        elif not fam:
            plan = "The codec is measured after download; Ingest copies or converts as needed."
        else:
            plan = f"Converted to {target.label}."
        self.v_pick.setText(line + ("\n" if line else "") + plan)

    def _format_changed(self, _v: str) -> None:
        v, a = self.vtable.value, self.atable.value
        if v == "auto" and a == "auto":
            self.opts.format_ids = ""
        else:
            vid = v if v != "auto" else ""
            aud = a if a != "auto" else ""
            vf = next((f for f in self._vids if f["format_id"] == vid), None)
            if vf is not None and vf.get("acodec") not in (None, "none") and not aud:
                self.opts.format_ids = vid
            elif vid and aud:
                self.opts.format_ids = f"{vid}+{aud}"
            elif vid:
                self.opts.format_ids = f"{vid}+ba/{vid}"
            else:
                self.opts.format_ids = f"bv*+{aud}/{aud}"
        self._update_estimate()

    def _trim_changed(self) -> None:
        a = parse_timecode(self.trim_in.text()) if self.trim_in.text().strip() else None
        b = parse_timecode(self.trim_out.text()) if self.trim_out.text().strip() else None
        dur = float((self.info or {}).get("duration") or 0)
        bad = (self.trim_in.text().strip() and a is None) or (self.trim_out.text().strip() and b is None)
        if bad:
            self.trim_note.setText("Use 1:30, 01:30.5 or 1:02:03")
            self.trim_note.set_color(T.FG)
            self.add_btn.setEnabled(False)
            return
        if a is not None and b is not None and b <= a:
            self.trim_note.setText("End must be after start")
            self.trim_note.set_color(T.FG)
            self.add_btn.setEnabled(False)
            return
        if dur and ((a or 0) >= dur):
            self.trim_note.setText("Start is past the end of the video")
            self.trim_note.set_color(T.FG)
            self.add_btn.setEnabled(False)
            return
        self.add_btn.setEnabled(True)
        self.opts.trim_start = a if a else None
        self.opts.trim_end = b if (b is not None and (not dur or b < dur)) else None
        if self.opts.trim_start is None and self.opts.trim_end is None:
            self.trim_note.setText("Whole video")
        else:
            length = (self.opts.trim_end or dur) - (self.opts.trim_start or 0)
            self.trim_note.setText(f"{human_duration(length)} clip, cut frame-accurately")
        self.trim_note.set_color(T.FG3)
        self._update_estimate()

    def _subs_changed(self) -> None:
        if self.subs.selected:
            self.opts.subtitles = True
            self.opts.sub_langs = ",".join(sorted(self.subs.selected))
        else:
            self.opts.subtitles = self.defaults.subtitles
            self.opts.sub_langs = self.defaults.sub_langs
        self.panel.refresh(full=True)

    def _options_changed(self) -> None:
        if self.info and self.stack.currentWidget() is self.video:
            self._describe_pick(self.info)
            self._fill_tables()

    def _picked(self, info: dict) -> list[dict]:
        """The formats yt-dlp chose, as full entries from the format list where possible."""
        req = info.get("requested_formats")
        if req:
            return req
        by_id = {str(f.get("format_id")): f for f in info.get("formats") or []}
        return [by_id.get(str(info.get("format_id")), info)]

    def _update_estimate(self) -> None:
        info = self.info or {}
        v = next((f for f in self._vids if f["format_id"] == self.vtable.value), None) if self.vtable.value != "auto" \
            else next((f for f in self._picked(info) if f.get("vcodec") not in (None, "none")), None)
        dur = float(info.get("duration") or 0)
        if self.opts.trim_start is not None or self.opts.trim_end is not None:
            dur = (self.opts.trim_end or dur) - (self.opts.trim_start or 0)
        if not dur:
            self.estimate.setText("")
            return
        w = int((v or {}).get("width") or 1920)
        h = int((v or {}).get("height") or 1080)
        if self.opts.resolution.isdigit() and min(w, h) > int(self.opts.resolution):
            s = int(self.opts.resolution) / min(w, h)
            w, h = int(w * s), int(h * s)
        fps = float((v or {}).get("fps") or 30)
        total = float(info.get("duration") or 0)
        a = next((f for f in self._auds if f["format_id"] == self.atable.value), None) if self.atable.value != "auto" \
            else next((f for f in (info.get("requested_formats") or []) if f.get("vcodec") == "none"), None)
        src = _format_bytes(v, total) + (_format_bytes(a, total) if a is not None and a is not v else 0.0)
        if not src:
            src = float(info.get("filesize") or info.get("filesize_approx") or 0)
        if total and dur < total:
            src *= dur / total
        fam = codec_family((v or {}).get("vcodec"))
        size = conform.estimate_size(self.opts, w, h, fps, dur, src, copies_video=self._action(fam, self.opts.profile)
                                     == "copy" and src > 0)
        prof = conform.PROFILES.get(self.opts.profile, conform.PROFILES["h264"])
        self.estimate.setText(f"About {human_bytes(size)} as {prof.label}" if size else prof.label)

    def _add_video(self) -> None:
        self._trim_changed()
        if not self.add_btn.isEnabled():
            return
        info = self.info or {}
        meta = [{"title": info.get("title") or "", "duration": info.get("duration") or 0,
                 "thumbnail": info.get("thumbnail") or "", "uploader": info.get("uploader") or ""}]
        self.add.emit([info.get("webpage_url") or self.url], copy_options(self.opts), False, meta, True)

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(1100, 700)


def _date(d: str | None) -> str:
    if d and len(d) == 8 and d.isdigit():
        return f"{d[:4]}-{d[4:6]}-{d[6:]}"
    return ""

