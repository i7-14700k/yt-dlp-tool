"""The Output panel: what the next download becomes. Everything here is saved as the default."""

from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import QRectF, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QFontMetricsF, QPainter
from PySide6.QtWidgets import QFileDialog, QScrollArea, QSizePolicy, QVBoxLayout, QWidget

from ..core import conform, naming
from ..core.settings import OutputOptions
from . import icons, theme as T
from .kit import (IconButton, Label, Segmented, Select, TextField, Toggle, _Hover, focus_mark, hbox, kbd_focus, mix,
                  rounded, vbox)

PROFILE_ROWS = [
    ("h264", "H.264", "MP4 · AAC"),
    ("prores", "ProRes 422", "MOV · 10-bit · PCM"),
    ("dnxhr", "DNxHR", "MOV · PCM"),
    ("hevc", "HEVC", "MP4 · 10-bit · AAC"),
    ("audio", "Audio only", "WAV · AAC · MP3"),
    ("original", "Original", "As served"),
]

FPS_OPTIONS = [
    ("source", "Match source", "constant rate, snapped to standard"),
    ("max30", "Source, up to 30", "60 becomes 30, 50 becomes 25"),
    ("max60", "Source, up to 60", ""),
    ("23.976", "23.976", ""), ("24", "24", ""), ("25", "25", ""), ("29.97", "29.97", ""),
    ("30", "30", ""), ("50", "50", ""), ("59.94", "59.94", ""), ("60", "60", ""),
]

LOUDNESS = [("off", "Off", ""), ("-14", "−14 LUFS", "YouTube, Spotify"), ("-16", "−16 LUFS", "Podcasts"),
            ("-23", "−23 LUFS", "EBU R128 broadcast")]

SUBFOLDERS = [("none", "None"), ("site", "By site"), ("uploader", "By channel"), ("playlist", "By playlist"),
              ("date", "By download date")]


class ProfileList(QWidget):
    changed = Signal(str)
    ROW = 38

    def __init__(self, value: str, parent=None) -> None:
        super().__init__(parent)
        self.value = value
        self.hover = -1
        self._name = T.font(13, 500)
        self._detail = T.font(11.5, 400, mono=True)
        self.setMouseTracking(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.TabFocus)
        self.setFixedHeight(self.ROW * len(PROFILE_ROWS) + 8)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def _row(self, i: int) -> QRectF:
        return QRectF(4, 4 + i * self.ROW, self.width() - 8, self.ROW)

    def set_value(self, v: str) -> None:
        self.value = v
        self.update()

    def mouseMoveEvent(self, e) -> None:  # noqa: N802
        i = int((e.position().y() - 4) // self.ROW)
        i = i if 0 <= i < len(PROFILE_ROWS) else -1
        if i != self.hover:
            self.hover = i
            self.update()

    def leaveEvent(self, e) -> None:  # noqa: N802
        self.hover = -1
        self.update()

    def mousePressEvent(self, e) -> None:  # noqa: N802
        i = int((e.position().y() - 4) // self.ROW)
        if 0 <= i < len(PROFILE_ROWS):
            key = PROFILE_ROWS[i][0]
            if key != self.value:
                self.value = key
                self.update()
                self.changed.emit(key)

    def keyPressEvent(self, e) -> None:  # noqa: N802
        keys = [k for k, _, _ in PROFILE_ROWS]
        i = keys.index(self.value) if self.value in keys else 0
        if e.key() == Qt.Key_Down and i < len(keys) - 1:
            self.value = keys[i + 1]
        elif e.key() == Qt.Key_Up and i > 0:
            self.value = keys[i - 1]
        else:
            return super().keyPressEvent(e)
        self.update()
        self.changed.emit(self.value)

    def paintEvent(self, _e) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        rounded(p, QRectF(self.rect()), T.R_MD + 2, T.S2 if not kbd_focus(self) else T.S3)
        for i, (key, name, detail) in enumerate(PROFILE_ROWS):
            r = self._row(i)
            sel = key == self.value
            if sel:
                rounded(p, r, T.R_MD, T.FG)
            elif i == self.hover:
                rounded(p, r, T.R_MD, T.S3)
            p.setFont(self._name)
            p.setPen(T.BG if sel else T.FG)
            p.drawText(r.adjusted(12, 0, 0, 0), Qt.AlignLeft | Qt.AlignVCenter, name)
            p.setFont(self._detail)
            p.setPen(T.INV_FG2 if sel else T.FG3)
            p.drawText(r.adjusted(0, 0, -12, 0), Qt.AlignRight | Qt.AlignVCenter, detail)
            if sel and kbd_focus(self):
                focus_mark(p, r, T.BG, inset=12)


class FolderRow(QWidget):
    """Shows the output folder; click to change it."""
    clicked = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.path = ""
        self._hover = _Hover(self)
        self._f = T.font(13, 500)
        self._f2 = T.font(11.5, 400, mono=True)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.TabFocus)
        self.setFixedHeight(48)
        self.setToolTip("Change folder")

    def set_path(self, p: str) -> None:
        self.path = p
        self.setToolTip(p)
        self.update()

    def enterEvent(self, e) -> None:  # noqa: N802
        self._hover.to(1)

    def leaveEvent(self, e) -> None:  # noqa: N802
        self._hover.to(0)

    def mouseReleaseEvent(self, e) -> None:  # noqa: N802
        if e.button() == Qt.LeftButton and self.rect().contains(e.position().toPoint()):
            self.clicked.emit()

    def keyPressEvent(self, e) -> None:  # noqa: N802
        if e.key() in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space):
            self.clicked.emit()
        else:
            super().keyPressEvent(e)

    def focusInEvent(self, e) -> None:  # noqa: N802
        self.update()
        super().focusInEvent(e)

    def focusOutEvent(self, e) -> None:  # noqa: N802
        self.update()
        super().focusOutEvent(e)

    def paintEvent(self, _e) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        rounded(p, QRectF(self.rect()), T.R_MD, mix(T.S2, T.S3, max(self._hover.value, kbd_focus(self))))
        if kbd_focus(self):
            focus_mark(p, QRectF(self.rect()), T.FG, inset=12)
        icons.draw(p, "folder", QRectF(12, self.height() / 2 - 8, 16, 16), T.FG2)
        path = Path(self.path) if self.path else None
        name = path.name if path else "Choose a folder"
        parent = str(path.parent) if path else ""
        avail = self.width() - 48
        p.setFont(self._f)
        p.setPen(T.FG)
        p.drawText(QRectF(38, 7, avail, 18), Qt.AlignLeft | Qt.AlignVCenter,
                   QFontMetricsF(self._f).elidedText(name, Qt.ElideRight, avail))
        p.setFont(self._f2)
        p.setPen(T.FG3)
        p.drawText(QRectF(38, 25, avail, 16), Qt.AlignLeft | Qt.AlignVCenter,
                   QFontMetricsF(self._f2).elidedText(parent, Qt.ElideMiddle, avail))


class _Section(QWidget):
    def __init__(self, title: str, parent=None) -> None:
        super().__init__(parent)
        self.lay = QVBoxLayout(self)
        self.lay.setContentsMargins(0, 0, 0, 0)
        self.lay.setSpacing(10)
        self.title = Label(title, 12, 500, T.FG2)
        self.lay.addWidget(self.title)

    def add(self, w) -> None:
        if isinstance(w, QWidget):
            self.lay.addWidget(w)
        else:
            self.lay.addLayout(w)


def _row(label: str, control: QWidget, hint: str = "") -> QWidget:
    w = QWidget()
    lay = vbox(spacing=6)
    w.setLayout(lay)
    top = hbox(Label(label, 13, 400, T.FG), None, control)
    lay.addLayout(top)
    if hint:
        h = Label(hint, 11.5, 400, T.FG3, wrap=True)
        lay.addWidget(h)
    return w


def _toggle_row(label: str, toggle: Toggle, hint: str = "") -> QWidget:
    w = QWidget()
    lay = vbox(spacing=2)
    w.setLayout(lay)
    lab = Label(label, 13, 400, T.FG)
    lay.addLayout(hbox(lab, None, toggle))
    if hint:
        lay.addWidget(Label(hint, 11.5, 400, T.FG3, wrap=True))
    return w


class OutputPanel(QScrollArea):
    """Edits an OutputOptions instance in place and emits `changed` after each edit."""
    changed = Signal()

    def __init__(self, opts: OutputOptions, recent_dirs: list[str] | None = None, parent=None) -> None:
        super().__init__(parent)
        self.opts = opts
        self.recent_dirs = recent_dirs or []
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setFrameShape(QScrollArea.NoFrame)
        body = QWidget()
        body.setObjectName("OutputBody")
        self.setWidget(body)
        lay = QVBoxLayout(body)
        lay.setContentsMargins(20, 18, 16, 24)
        lay.setSpacing(26)

        head = hbox(Label("Output", 15, 600), None, spacing=8)
        self.compat = Label("", 11.5, 400, T.FG3, mono=True)
        head.addWidget(self.compat)
        lay.addLayout(head)

        # ---- format
        fmt = _Section("Format")
        self.profiles = ProfileList(self._profile_row_key())
        self.profiles.changed.connect(self._on_profile)
        fmt.add(self.profiles)
        self.prores = Segmented([("proxy", "Proxy"), ("lt", "LT"), ("standard", "422"), ("hq", "HQ")], opts.prores)
        self.prores.changed.connect(lambda v: self._set("prores", v))
        self.dnxhr = Segmented([("lb", "LB"), ("sq", "SQ"), ("hq", "HQ"), ("hqx", "HQX 10-bit")], opts.dnxhr)
        self.dnxhr.changed.connect(lambda v: self._set("dnxhr", v))
        self.audio_kind = Segmented([("wav", "WAV"), ("aac", "AAC"), ("mp3", "MP3")],
                                    opts.profile if opts.profile in ("wav", "aac", "mp3") else "wav")
        self.audio_kind.changed.connect(self._on_audio_kind)
        fmt.add(self.prores)
        fmt.add(self.dnxhr)
        fmt.add(self.audio_kind)
        self.note = Label("", 12, 400, T.FG2, wrap=True)
        fmt.add(self.note)
        lay.addWidget(fmt)

        # ---- video
        self.video = _Section("Video")
        self.res = Segmented([("best", "Best"), ("2160", "4K"), ("1440", "1440"), ("1080", "1080"),
                              ("720", "720"), ("480", "480")], opts.resolution)
        self.res.changed.connect(lambda v: self._set("resolution", v))
        self.video.add(self.res)
        self.fps = Select(FPS_OPTIONS, self._fps_key(), min_width=170)
        self.fps.changed.connect(self._on_fps)
        self.video.add(_row("Frame rate", self.fps))
        self.hdr = Segmented([("sdr", "SDR"), ("keep", "Keep HDR")], opts.hdr, equal=True)
        self.hdr.setFixedWidth(170)
        self.hdr.changed.connect(lambda v: self._set("hdr", v))
        self.hdr_row = _row("HDR", self.hdr)
        self.video.add(self.hdr_row)
        self.hdr_note = Label("HDR sources are tone-mapped to SDR BT.709, so colors look right without a LUT.",
                              11.5, 400, T.FG3, wrap=True)
        self.video.add(self.hdr_note)
        self.quality = Segmented([("max", "Max"), ("high", "High"), ("balanced", "Balanced"), ("compact", "Small")],
                                 opts.quality)
        self.quality.changed.connect(lambda v: self._set("quality", v))
        self.quality_label = Label("Quality when converting", 13, 400, T.FG)
        quality_block = vbox(spacing=8)
        quality_block.setContentsMargins(0, 6, 0, 0)  # the HDR note above is grey too; keep them apart
        quality_block.addWidget(self.quality_label)
        quality_block.addWidget(self.quality)
        self.quality_box = QWidget()
        self.quality_box.setLayout(quality_block)
        self.video.add(self.quality_box)
        self.compatible = Toggle(opts.prefer_compatible)
        self.compatible.toggled.connect(lambda v: self._set("prefer_compatible", v))
        self.compatible_row = _toggle_row("Avoid re-encoding", self.compatible,
                                          "Prefer H.264 and AAC sources when they match the best quality.")
        self.video.add(self.compatible_row)
        lay.addWidget(self.video)

        # ---- audio
        audio = _Section("Audio")
        self.rate = Segmented([("48000", "48 kHz"), ("source", "Source rate")], opts.audio_rate)
        self.rate.changed.connect(lambda v: self._set("audio_rate", v))
        audio.add(self.rate)
        self.loud = Select(LOUDNESS, opts.loudness, min_width=150)
        self.loud.changed.connect(lambda v: self._set("loudness", v))
        audio.add(_row("Loudness", self.loud))
        lay.addWidget(audio)

        # ---- extras
        extras = _Section("Also save")
        self.subs = Toggle(opts.subtitles)
        self.subs.toggled.connect(self._on_subs)
        extras.add(_toggle_row("Subtitles as .srt", self.subs))
        self.sub_langs = TextField(opts.sub_langs, "en.*, de, ja", mono=True)
        self.sub_langs.editingFinished.connect(lambda: self._set("sub_langs", self.sub_langs.text().strip() or "en.*"))
        self.sub_langs.setToolTip("Languages, comma separated. en.* matches every English variant. all for all.")
        extras.add(self.sub_langs)
        self.thumb = Toggle(opts.thumbnail)
        self.thumb.toggled.connect(lambda v: self._set("thumbnail", v))
        extras.add(_toggle_row("Thumbnail as .jpg", self.thumb))
        self.proxy = Toggle(opts.proxy != "off")
        self.proxy.toggled.connect(self._on_proxy)
        extras.add(_toggle_row("Premiere proxies", self.proxy, "Low-res copies in a Proxies folder."))
        self.proxy_kind = Segmented([("prores", "ProRes Proxy 540p"), ("h264", "H.264 720p")],
                                    opts.proxy if opts.proxy != "off" else "prores")
        self.proxy_kind.changed.connect(lambda v: self._set("proxy", v))
        extras.add(self.proxy_kind)
        self.sponsor = Toggle(opts.sponsorblock)
        self.sponsor.toggled.connect(lambda v: self._set("sponsorblock", v))
        extras.add(_toggle_row("Cut sponsor segments", self.sponsor, "YouTube only, from SponsorBlock."))
        self.keep = Toggle(opts.keep_source)
        self.keep.toggled.connect(lambda v: self._set("keep_source", v))
        extras.add(_toggle_row("Keep the original download", self.keep, "Saved to an Originals folder."))
        lay.addWidget(extras)

        # ---- destination
        dest = _Section("Save to")
        self.folder = FolderRow()
        self.folder.clicked.connect(self.choose_folder)
        self.folder_open = IconButton("external", "Open folder")
        self.folder_open.clicked.connect(self._open_folder)
        dest.add(self.folder)
        self.subfolder = Select(SUBFOLDERS, opts.subfolder, min_width=150)
        self.subfolder.changed.connect(lambda v: self._set("subfolder", v))
        dest.add(_row("Subfolder", self.subfolder))
        self.name_tpl = Select([(t, label) for t, label in naming.PRESETS] + [("custom", "Custom")],
                               self._template_key(), min_width=150)
        self.name_tpl.changed.connect(self._on_template)
        dest.add(_row("File name", self.name_tpl))
        self.custom_tpl = TextField(opts.filename, "{title} [{id}]", mono=True)
        self.custom_tpl.textChanged.connect(self._on_custom_text)
        tokens = ", ".join("{" + k + "}" for k in naming.TOKENS)
        self.custom_tpl.setToolTip("Tokens: " + tokens + ". Use / for subfolders. {index:03} pads numbers.")
        dest.add(self.custom_tpl)
        self.preview = Label("", 11.5, 400, T.FG3, mono=True, wrap=True)
        dest.add(self.preview)
        lay.addWidget(dest)
        lay.addStretch(1)

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(250)
        self._debounce.timeout.connect(self.changed.emit)
        self.refresh()

    # ---------------------------------------------------------------- state
    def _profile_row_key(self) -> str:
        return "audio" if self.opts.profile in ("wav", "aac", "mp3") else self.opts.profile

    def _fps_key(self) -> str:
        if self.opts.fps not in ("", "source"):
            return self.opts.fps
        if self.opts.max_fps in ("30", "60"):
            return "max" + self.opts.max_fps
        return "source"

    def _template_key(self) -> str:
        presets = [t for t, _ in naming.PRESETS]
        return self.opts.filename if self.opts.filename in presets else "custom"

    def _set(self, key: str, value) -> None:
        setattr(self.opts, key, value)
        self.refresh()
        self._debounce.start()

    def _on_profile(self, key: str) -> None:
        if key == "audio":
            key = self.audio_kind.value
        self._set("profile", key)

    def _on_audio_kind(self, key: str) -> None:
        self._set("profile", key)

    def _on_fps(self, key: str) -> None:
        if key.startswith("max"):
            self.opts.fps, self.opts.max_fps = "source", key[3:]
        elif key == "source":
            self.opts.fps, self.opts.max_fps = "source", "any"
        else:
            self.opts.fps, self.opts.max_fps = key, "any"
        self.refresh()
        self._debounce.start()

    def _on_subs(self, on: bool) -> None:
        self._set("subtitles", on)

    def _on_proxy(self, on: bool) -> None:
        self._set("proxy", self.proxy_kind.value if on else "off")

    def _on_template(self, key: str) -> None:
        if key == "custom":
            self.custom_tpl.setVisible(True)
            self.custom_tpl.setFocus()
            return
        self.custom_tpl.setText(key)
        self._set("filename", key)

    def _on_custom_text(self, text: str) -> None:
        if self.name_tpl.value == "custom":
            self.opts.filename = text or "{title}"
            self._update_preview()
            self._debounce.start()

    def choose_folder(self) -> None:
        start = str(self.opts.resolved_output_dir())
        d = QFileDialog.getExistingDirectory(self, "Save downloads to", start)
        if d:
            self.opts.output_dir = os.path.normpath(d)
            self.refresh()
            self._debounce.start()

    def _open_folder(self) -> None:
        d = self.opts.resolved_output_dir()
        d.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(d)))

    def set_options(self, opts: OutputOptions) -> None:
        self.opts = opts
        self.refresh(full=True)

    def refresh(self, full: bool = False) -> None:
        o = self.opts
        prof = conform.PROFILES.get(o.profile, conform.PROFILES["h264"])
        if full:
            self.profiles.set_value(self._profile_row_key())
            self.prores.set_value(o.prores, animate=False)
            self.dnxhr.set_value(o.dnxhr, animate=False)
            if o.profile in ("wav", "aac", "mp3"):
                self.audio_kind.set_value(o.profile, animate=False)
            self.res.set_value(o.resolution, animate=False)
            self.fps.set_value(self._fps_key())
            self.hdr.set_value(o.hdr, animate=False)
            self.quality.set_value(o.quality, animate=False)
            self.compatible.set_checked_silent(o.prefer_compatible)
            self.rate.set_value(o.audio_rate, animate=False)
            self.loud.set_value(o.loudness)
            self.subs.set_checked_silent(o.subtitles)
            self.sub_langs.setText(o.sub_langs)
            self.thumb.set_checked_silent(o.thumbnail)
            self.proxy.set_checked_silent(o.proxy != "off")
            self.sponsor.set_checked_silent(o.sponsorblock)
            self.keep.set_checked_silent(o.keep_source)
            self.subfolder.set_value(o.subfolder)
            self.name_tpl.set_value(self._template_key())
            self.custom_tpl.setText(o.filename)
        self.prores.setVisible(o.profile == "prores")
        self.dnxhr.setVisible(o.profile == "dnxhr")
        self.audio_kind.setVisible(prof.kind == "audio")
        self.video.setVisible(prof.kind != "audio")
        encodes = o.profile in ("h264", "hevc")
        self.quality_box.setVisible(encodes)
        self.compatible_row.setVisible(o.profile in ("h264", "hevc"))
        hdr_ok = o.profile in ("hevc", "prores") or (o.profile == "dnxhr" and o.dnxhr == "hqx")
        self.hdr_row.setVisible(prof.kind == "video" and hdr_ok)
        self.hdr_note.setVisible(prof.kind == "video" and (not hdr_ok or o.hdr == "sdr"))
        if not hdr_ok and o.hdr == "keep":
            o.hdr = "sdr"
            self.hdr.set_value("sdr", animate=False)
        self.sub_langs.setVisible(o.subtitles)
        self.proxy_kind.setVisible(o.proxy != "off" and prof.kind == "video")
        self.proxy.parentWidget().setVisible(prof.kind == "video")
        self.custom_tpl.setVisible(self.name_tpl.value == "custom")
        note = prof.note
        if o.profile == "prores":
            note += {"proxy": " Proxy is for offline editing only.", "lt": " LT is a lighter master.",
                     "standard": "", "hq": " HQ keeps the most detail."}.get(o.prores, "")
        self.note.setText(note)
        self.compat.setText("Pr ✓  Ae ✓" if prof.premiere else "may not import")
        self.compat.set_color(T.FG2 if prof.premiere else T.FG3)
        self.folder.set_path(str(o.resolved_output_dir()))
        self._update_preview()

    def _update_preview(self) -> None:
        o = self.opts
        prof = conform.PROFILES.get(o.profile, conform.PROFILES["h264"])
        ctx = {"title": "Rocket launch at dawn", "uploader": "SpaceX", "id": "dQw4w9WgXcQ", "site": "YouTube",
               "date": "2026-10-14", "year": "2026", "playlist": "Launches", "index": 3, "res": "1080p",
               "fps": "29.97", "profile": prof.label, "today": "2026-10-16"}
        parts = naming.subfolder_parts(o.subfolder, ctx, o.ascii_names) + naming.render(o.filename, ctx, o.ascii_names)
        ext = prof.ext or "mp4"
        self.preview.setText("/".join(parts) + "." + ext)

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(T.PANEL_W, 600)

    def paintEvent(self, e) -> None:  # noqa: N802
        p = QPainter(self.viewport())
        p.setRenderHint(QPainter.Antialiasing)
        rounded(p, QRectF(self.viewport().rect()), T.R_LG, T.S1)
        p.end()
        super().paintEvent(e)
