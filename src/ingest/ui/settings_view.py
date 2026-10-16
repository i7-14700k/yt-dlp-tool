"""Settings: app-wide behaviour. Per-download choices live in the Output panel."""

from __future__ import annotations

import os

from PySide6.QtCore import QRectF, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QPainter
from PySide6.QtWidgets import QAbstractButton, QFileDialog, QScrollArea, QWidget

from .. import __version__
from ..core import paths
from ..core.encoders import HW_NAMES
from ..core.engines import SETUP_KEYS
from ..core.settings import Settings
from ..core.util import normalize_country, normalize_proxy, normalize_rate
from . import theme as T
from .kit import (Button, Label, Segmented, Select, Stepper, TextField, Toggle, _Hover, focus_mark, hbox, kbd_focus,
                  mix, rounded, vbox)
from .setup_view import EngineRow

SECTIONS = [("general", "General"), ("downloads", "Downloads"), ("conversion", "Conversion"),
            ("network", "Network"), ("accounts", "Accounts"), ("engines", "Engines"), ("advanced", "Advanced"),
            ("about", "About")]

BROWSERS = [("firefox", "Firefox", "works reliably on Windows"), ("chrome", "Chrome", "may fail while Chrome runs"),
            ("edge", "Edge", "may fail while Edge runs"), ("brave", "Brave", ""), ("opera", "Opera", ""),
            ("vivaldi", "Vivaldi", ""), ("chromium", "Chromium", ""), ("whale", "Whale", ""),
            ("safari", "Safari", "macOS only")]


class _IndexButton(QAbstractButton):
    def __init__(self, text: str, parent=None) -> None:
        super().__init__(parent)
        self.setText(text)
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.TabFocus)
        self.setFixedHeight(32)
        self._hover = _Hover(self)
        self._f = T.font(13, 500)

    def enterEvent(self, e) -> None:  # noqa: N802
        self._hover.to(1)

    def leaveEvent(self, e) -> None:  # noqa: N802
        self._hover.to(0)

    def paintEvent(self, _e) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        if self.isChecked():
            rounded(p, QRectF(self.rect()), T.R_MD, T.S2)
        elif self._hover.value > 0:
            rounded(p, QRectF(self.rect()), T.R_MD, mix(T.BG, T.S1, self._hover.value))
        p.setFont(self._f)
        p.setPen(T.FG if self.isChecked() or kbd_focus(self) else mix(T.FG3, T.FG, self._hover.value))
        p.drawText(QRectF(self.rect()).adjusted(12, 0, 0, 0), Qt.AlignVCenter | Qt.AlignLeft, self.text())
        if kbd_focus(self):
            focus_mark(p, QRectF(self.rect()), T.FG, inset=12)


class _Group(QWidget):
    def __init__(self, title: str, parent=None) -> None:
        super().__init__(parent)
        self.lay = vbox(spacing=0)
        self.setLayout(self.lay)
        self.lay.addWidget(Label(title, 18, 600))
        self.lay.addSpacing(14)
        self._rows = 0

    def row(self, title: str, control: QWidget | None, hint: str = "", wide: bool = False) -> QWidget:
        w = _Row(title, control, hint, wide)
        self.lay.addWidget(w)
        self._rows += 1
        return w

    def add(self, w: QWidget, spacing: int = 8) -> None:
        self.lay.addSpacing(spacing)
        self.lay.addWidget(w)


class _Row(QWidget):
    """Label and explanation on the left, control on the right. Grouped by spacing, not lines."""

    def __init__(self, title: str, control: QWidget | None, hint: str, wide: bool) -> None:
        super().__init__()
        t = Label(title, 13, 500)
        left = vbox(t, spacing=3)
        if hint:
            h = Label(hint, 12, 400, T.FG3, wrap=True)
            h.setMaximumWidth(480)
            h.setMinimumWidth(320)
            left.addWidget(h)
        if wide and control is not None:
            lay = vbox(left, control, spacing=8, margins=(0, 10, 0, 10))
        else:
            lay = hbox(spacing=32, margins=(0, 10, 0, 10))
            lay.addLayout(left, 1)
            if control is not None:
                lay.addWidget(control, 0, Qt.AlignRight | Qt.AlignVCenter)
        self.setLayout(lay)


class SettingsPage(QWidget):
    changed = Signal()
    engine_install = Signal(list)
    engine_locate = Signal(str)
    engine_check = Signal()
    redetect = Signal()
    reset = Signal()

    def __init__(self, settings: Settings, parent=None) -> None:
        super().__init__(parent)
        self.s = settings
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(300)
        self._debounce.timeout.connect(self.changed.emit)

        self.index_buttons: dict[str, _IndexButton] = {}
        index = vbox(spacing=2)
        for key, label in SECTIONS:
            b = _IndexButton(label)
            b.clicked.connect(lambda _c=False, k=key: self.jump(k))
            self.index_buttons[key] = b
            index.addWidget(b)
        index.addStretch(1)
        index_w = QWidget()
        index_w.setLayout(index)
        index_w.setFixedWidth(180)

        self.body = QWidget()
        self.body_lay = vbox(spacing=44, margins=(8, 4, 32, 200))
        self.body.setLayout(self.body_lay)
        self.groups: dict[str, _Group] = {}
        self._build()
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QScrollArea.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setWidget(self.body)
        self.scroll.verticalScrollBar().valueChanged.connect(self._sync_index)
        self.body.setMaximumWidth(860)
        self.setLayout(hbox(index_w, self.scroll, spacing=24, margins=(24, 8, 24, 0)))
        self.index_buttons["general"].setChecked(True)

    def _save(self) -> None:
        self._debounce.start()

    def _bind_toggle(self, attr: str, obj=None) -> Toggle:
        obj = obj or self.s
        t = Toggle(bool(getattr(obj, attr)))
        t.toggled.connect(lambda v: (setattr(obj, attr, v), self._save()))
        return t

    def _bind_seg(self, attr: str, options, obj=None, width: int | None = None) -> Segmented:
        obj = obj or self.s
        seg = Segmented(options, str(getattr(obj, attr)))
        if width:
            seg.setFixedWidth(width)
        else:
            seg.setFixedWidth(max(200, seg.sizeHint().width()))

        def apply(v):
            cur = getattr(obj, attr)
            setattr(obj, attr, int(v) if isinstance(cur, int) and not isinstance(cur, bool) else v)
            self._save()
        seg.changed.connect(apply)
        return seg

    def _bind_select(self, attr: str, options, obj=None, width: int = 200) -> Select:
        obj = obj or self.s
        sel = Select(options, str(getattr(obj, attr)), min_width=width)
        sel.setFixedWidth(width)

        def apply(v):
            cur = getattr(obj, attr)
            setattr(obj, attr, int(v) if isinstance(cur, int) and not isinstance(cur, bool) else v)
            self._save()
        sel.changed.connect(apply)
        return sel

    def _bind_stepper(self, attr: str, lo: int, hi: int) -> Stepper:
        st = Stepper(int(getattr(self.s, attr)), lo, hi)
        st.setFixedWidth(130)
        st.valueChanged.connect(lambda v: (setattr(self.s, attr, v), self._save()))
        return st

    def _bind_text(self, attr: str, placeholder: str, width: int = 280, mono: bool = True, obj=None,
                   normalize=None) -> TextField:
        obj = obj or self.s
        f = TextField(str(getattr(obj, attr)), placeholder, mono=mono)
        f.setFixedWidth(width)

        def apply():
            value = f.text().strip()
            if normalize is not None:
                value = normalize(value)
                if value != f.text():
                    f.setText(value)  # show what will actually be used ("5 mb" -> "5M", junk -> empty)
            setattr(obj, attr, value)
            self._save()
        f.editingFinished.connect(apply)
        return f

    def _group(self, key: str, title: str) -> _Group:
        g = _Group(title)
        self.groups[key] = g
        self.body_lay.addWidget(g)
        return g

    def _build(self) -> None:
        o = self.s.output

        g = self._group("general", "General")
        g.row("Notify when downloads finish", self._bind_toggle("notifications"),
              "A Windows notification when Ingest is in the background.")
        g.row("Offer copied links", self._bind_toggle("clipboard_suggest"),
              "When you come back to Ingest with a link on the clipboard, it offers to download it.")
        g.row("Verify every file", self._bind_toggle("verify"),
              "Measure finished files against Premiere and After Effects requirements. Adds a second or two.")
        g.row("Resume unfinished downloads on launch", self._bind_toggle("resume_on_launch"))
        copy_bm = Button("Copy bookmarklet", "secondary")
        copy_bm.clicked.connect(self._copy_bookmarklet)
        g.row("Send pages from the browser", copy_bm,
              "Make a new bookmark and paste this as its address. Clicking it on any page sends that page to "
              "Ingest. The first time, the browser asks to open Ingest.")

        g = self._group("downloads", "Downloads")
        g.row("Parallel downloads", self._bind_stepper("max_concurrent", 1, 8),
              "Conversions run separately, so downloads keep going while files convert.")
        g.row("Parallel conversions", self._bind_stepper("max_conform", 1, 4),
              "Each conversion uses most of a CPU or a GPU encoder session. 1 is right for most PCs.")
        g.row("Fragments at once", self._bind_stepper("fragments", 1, 16),
              "For HLS and DASH streams. Higher is faster on fast connections.")
        g.row("Speed limit", self._bind_text("rate_limit", "No limit", 180, normalize=normalize_rate),
              "Bytes per second, per download, like 5M or 800K.")
        g.row("Links with a video and a playlist", self._bind_seg(
            "playlist_links", [("video", "Just the video"), ("playlist", "Whole playlist")], width=260))
        g.row("Review playlists longer than", self._bind_stepper("playlist_confirm", 5, 2000),
              "Bigger playlists and channels open in Inspect so you can pick entries.")
        g.row("Skip videos already in the library", self._bind_toggle("skip_existing"),
              "Matched by site and video ID, as long as the file still exists.")
        g.row("Working files", self._bind_seg("work_location", [("output", "Next to output"),
                                                                 ("appdata", "App data")], width=260),
              "Next to output avoids copying big files between drives. The folder is hidden and removed after.")
        g.row("If a file with the same name exists", self._bind_seg(
            "conflict", [("number", "Add a number"), ("overwrite", "Replace"), ("skip", "Skip")], obj=o, width=300))
        g.row("ASCII-only file names", self._bind_toggle("ascii_names", o),
              "Strips accents and non-Latin scripts. Emoji are always removed, since Adobe apps mishandle them.")

        g = self._group("conversion", "Conversion")
        self.encoder = self._bind_select("encoder", [("auto", "Automatic", "GPU when available"),
                                                     ("x264", "CPU (x264)", "best quality per MB"),
                                                     ("nvenc", "NVIDIA NVENC", ""), ("qsv", "Intel Quick Sync", ""),
                                                     ("amf", "AMD AMF", "")], obj=o, width=220)
        self.encoder_row = g.row("Video encoder", self.encoder, "")
        self.detected = Label("Detecting encoders…", 12, 400, T.FG3)
        redetect = Button("Detect again", "ghost")
        redetect.clicked.connect(self.redetect.emit)
        det = QWidget()
        det.setLayout(hbox(self.detected, None, redetect))
        g.add(det, 0)
        g.row("CPU encoder speed", self._bind_select("speed", [
            ("veryfast", "Very fast"), ("faster", "Faster"), ("fast", "Fast"), ("medium", "Medium"),
            ("slow", "Slow"), ("slower", "Slower")], obj=o, width=160),
              "Slower makes smaller files at the same quality. Doesn't affect GPU encoders.")
        g.row("Keyframe interval", self._bind_seg("gop", [("1s", "1 second"), ("2s", "2 seconds"),
                                                           ("auto", "Encoder default")], obj=o, width=330),
              "Shorter intervals scrub faster in Premiere's timeline.")
        g.row("AAC bitrate", self._bind_select("audio_bitrate", [("192", "192 kb/s"), ("256", "256 kb/s"),
                                                                 ("320", "320 kb/s")], obj=o, width=140),
              "Per stereo pair. Surround scales up.")
        g.row("Audio channels", self._bind_seg("audio_channels", [("source", "Source"), ("stereo", "Stereo"),
                                                                   ("mono", "Mono")], obj=o, width=260))
        g.row("PCM bit depth", self._bind_seg("pcm_depth", [("16", "16-bit"), ("24", "24-bit")], obj=o, width=180),
              "For WAV, ProRes and DNxHR audio.")

        g = self._group("network", "Network")
        g.row("Proxy", self._bind_text("proxy", "http://host:port or socks5://host:port", 320,
                                       normalize=normalize_proxy),
              "Used for downloads, engine updates and thumbnails. Empty uses the system setting.")
        g.row("Browser impersonation", self._bind_seg("impersonate", [("auto", "When needed"), ("always", "Always"),
                                                                      ("off", "Off")], width=300),
              "Retries as Chrome when a site blocks yt-dlp (Cloudflare, HTTP 403).")
        g.row("Force IPv4", self._bind_toggle("force_ipv4"), "Helps on networks with broken IPv6.")
        g.row("Pretend to be in country", self._bind_text("geo_country", "e.g. US, DE", 120,
                                                          normalize=normalize_country),
              "Two-letter code sent as X-Forwarded-For. Works on some sites, not all.")
        g.row("Skip TLS certificate checks", self._bind_toggle("insecure"),
              "Only for networks that intercept HTTPS. Turn it back off afterwards.")

        g = self._group("accounts", "Accounts")
        self.cookie_mode = self._bind_seg("cookies", [("off", "Off"), ("browser", "From a browser"),
                                                      ("file", "cookies.txt")], width=330)
        self.cookie_mode.changed.connect(lambda _v: self._sync_cookie_rows())
        g.row("Sign in with cookies", self.cookie_mode,
              "Needed for private, members-only, age-restricted videos and YouTube's bot checks.")
        self.browser_row = g.row("Browser", self._bind_select("cookies_browser", BROWSERS, width=200),
                                 "Chrome and Edge encrypt cookies with app-bound keys on Windows; yt-dlp often can't "
                                 "read them. Firefox works reliably.")
        self.profile_row = g.row("Browser profile", self._bind_text("cookies_profile", "Default", 200),
                                 "Leave empty for the default profile.")
        self.cookie_file = self._bind_text("cookies_file", "cookies.txt path", 280)
        pick = Button("Choose", "secondary")
        pick.clicked.connect(self._pick_cookies)
        fw = QWidget()
        fw.setLayout(hbox(self.cookie_file, pick))
        self.file_row = g.row("Cookie file", fw, "Netscape format, exported with a browser extension.")
        g.row("Use cookies on every download", self._bind_toggle("cookies_always"),
              "Off: cookies are only sent when a site asks you to sign in. Safer for your account.")
        self._sync_cookie_rows()

        g = self._group("engines", "Engines")
        self.engine_rows = {k: EngineRow(k) for k in SETUP_KEYS}  # the nightly slot manages itself
        for r in self.engine_rows.values():
            r.install.connect(lambda k: self.engine_install.emit([k]))
            r.locate.connect(self.engine_locate.emit)
            g.add(r, 8)
        g.lay.addSpacing(10)
        g.row("yt-dlp channel", self._bind_seg("channel", [("stable", "Stable"), ("nightly", "Nightly")], width=200),
              "Nightly gets site fixes days or weeks before a stable release.")
        g.row("Update engines automatically", self._bind_toggle("auto_update"), "Checks once a day at launch.")
        g.row("Retry broken sites with nightly", self._bind_toggle("nightly_fallback"),
              "When yt-dlp can't read a page, fetch the latest nightly build and retry that one download "
              "with it. Your stable yt-dlp stays as it is.")
        g.row("Fall back to gallery-dl", self._bind_toggle("gallery_fallback"),
              "When yt-dlp finds no video, try gallery-dl for images and galleries.")
        check = Button("Check for updates", "secondary")
        check.clicked.connect(self.engine_check.emit)
        cw = QWidget()
        cw.setLayout(hbox(check, None))
        g.add(cw, 6)

        g = self._group("advanced", "Advanced")
        g.row("Extra yt-dlp options", self._bind_text("extra_args", "--extractor-args \"youtube:player_client=tv\"",
                                                      620), "Added to every yt-dlp call. Use with care.", wide=True)
        logs = Button("Open logs", "secondary")
        logs.clicked.connect(lambda: _open(paths.logs_dir()))
        eng = Button("Open engines folder", "secondary")
        eng.clicked.connect(lambda: _open(paths.engines_dir()))
        cfg = Button("Open settings folder", "secondary")
        cfg.clicked.connect(lambda: _open(paths.config_dir()))
        bw = QWidget()
        bw.setLayout(hbox(logs, eng, cfg, None))
        g.add(bw, 8)
        reset = Button("Reset all settings", "ghost")
        reset.clicked.connect(self.reset.emit)
        rw = QWidget()
        rw.setLayout(hbox(reset, None))
        g.add(rw, 16)

        g = self._group("about", "About")
        about = Label(
            f"Ingest {__version__}. Downloads with yt-dlp and converts with FFmpeg into files that open in "
            "Premiere Pro and After Effects without transcoding, proxies or interpretation fixes.\n\n"
            "yt-dlp is public domain (Unlicense). FFmpeg builds come from yt-dlp/FFmpeg-Builds under the GPL. "
            "Deno is MIT licensed, gallery-dl GPL-2.0. Instrument Sans and Fragment Mono are under the SIL Open "
            "Font License. Ingest downloads these engines from their publishers; it does not bundle them.\n\n"
            "Only download what you have the right to use.", 13, 400, T.FG2, wrap=True)
        about.setMaximumWidth(620)
        g.add(about, 0)

    def _copy_bookmarklet(self) -> None:
        from PySide6.QtGui import QGuiApplication
        from ..core.util import BOOKMARKLET
        QGuiApplication.clipboard().setText(BOOKMARKLET)
        w = self.window()
        if hasattr(w, "toast"):
            w.toast.show_message("Bookmarklet copied. Paste it as a bookmark's address.")

    def _sync_cookie_rows(self) -> None:
        mode = self.s.cookies
        self.browser_row.setVisible(mode == "browser")
        self.profile_row.setVisible(mode == "browser")
        self.file_row.setVisible(mode == "file")

    def _pick_cookies(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Choose cookies.txt", "", "Cookie files (*.txt);;All files (*)")
        if path:
            self.s.cookies_file = os.path.normpath(path)
            self.cookie_file.setText(self.s.cookies_file)
            self._save()

    def set_detected(self, hw_h264: list[str], zscale: bool) -> None:
        if hw_h264:
            names = ", ".join(HW_NAMES.get(h, h) for h in hw_h264)
            self.detected.setText(f"GPU encoders on this PC: {names}.")
        else:
            self.detected.setText("No GPU encoder found. Converting on the CPU.")
        if not zscale:
            self.detected.setText(self.detected.text() + " This FFmpeg can't tone-map HDR.")

    def set_engine_states(self, states: dict) -> None:
        for k, st in states.items():
            if k in self.engine_rows:
                self.engine_rows[k].set_state(st)

    def jump(self, key: str) -> None:
        g = self.groups.get(key)
        if g is None:
            return
        for k, b in self.index_buttons.items():
            b.setChecked(k == key)
        self._jumping = True
        self.scroll.verticalScrollBar().setValue(max(0, g.y() - 4))
        QTimer.singleShot(50, lambda: setattr(self, "_jumping", False))

    def _sync_index(self, value: int) -> None:
        if getattr(self, "_jumping", False):
            return
        current = None
        for key, g in self.groups.items():
            if g.y() - 60 <= value:
                current = key
        if self.scroll.verticalScrollBar().maximum() - value < 8:
            current = list(self.groups)[-1]
        for k, b in self.index_buttons.items():
            b.setChecked(k == (current or "general"))


def _open(path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

