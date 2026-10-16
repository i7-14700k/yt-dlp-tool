"""Main window: header navigation, the Queue page, and the wiring between UI and core."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from PySide6.QtCore import QEvent, QPoint, QRectF, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import (QColor, QDesktopServices, QGuiApplication, QIcon, QKeySequence, QPainter, QPainterPath,
                           QPen, QShortcut)
from PySide6.QtWidgets import (QAbstractButton, QAbstractScrollArea, QApplication, QMainWindow, QStackedWidget,
                               QSystemTrayIcon, QWidget)

from .. import APP_NAME
from ..core import paths, winapi
from ..core.engines import Engines
from ..core.jobs import Job, Stage, Status
from ..core.library import Library
from ..core.queue import QueueManager
from ..core.settings import SettingsStore
from ..core.util import extract_urls, human_bytes, human_speed, is_url
from ..core.ytdlp import Network
from . import icons, theme as T
from .bridge import Bridge
from .dialogs import Confirm, LogViewer
from .engine_ops import EngineTasks, due_for_check
from .inspect_view import InspectPage
from .kit import (Button, Label, Toast, _Hover, autohide_scrollbars, hbox, kbd_focus, make_menu, mix, rounded,
                  track_keyboard_focus, vbox)
from .library_view import LibraryPage
from .output_panel import OutputPanel
from .queue_view import QueueModel, QueueView
from .settings_view import SettingsPage
from .setup_view import SetupPage, pick_engine_file
from .thumbs import ThumbCache
from .url_bar import UrlBar


class NavTab(QAbstractButton):
    def __init__(self, text: str, parent=None) -> None:
        super().__init__(parent)
        self.setText(text)
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.TabFocus)
        self.count = ""
        self._hover = _Hover(self)
        self._f = T.font(13.5, 500)
        self._fc = T.font(11, 400, mono=True)
        self.setFixedHeight(56)

    def set_count(self, c: str) -> None:
        self.count = c
        self.updateGeometry()
        self.update()

    def sizeHint(self) -> QSize:  # noqa: N802
        w = T.text_width(self._f, self.text())
        if self.count:
            w += T.text_width(self._fc, self.count) + 6
        return QSize(int(w + 20), 56)

    def enterEvent(self, e) -> None:  # noqa: N802
        self._hover.to(1)

    def leaveEvent(self, e) -> None:  # noqa: N802
        self._hover.to(0)

    def paintEvent(self, _e) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        on = self.isChecked()
        color = T.FG if on or kbd_focus(self) else mix(T.FG3, T.FG2, self._hover.value)
        p.setFont(self._f)
        p.setPen(color)
        tw = T.text_width(self._f, self.text())
        x = 10.0
        if kbd_focus(self):
            # The underline already means "this page"; focus gets a pill behind the label.
            extra = (T.text_width(self._fc, self.count) + 6) if self.count else 0
            rounded(p, QRectF(x - 8, self.height() / 2 - 14, tw + extra + 16, 26), T.R_MD, T.S3)
            p.setPen(color)
        p.drawText(QRectF(x, 0, tw + 2, self.height()), Qt.AlignVCenter | Qt.AlignLeft, self.text())
        if self.count:
            p.setFont(self._fc)
            p.setPen(T.FG2 if on else T.FG3)
            p.drawText(QRectF(x + tw + 6, 0, 60, self.height() - 1), Qt.AlignVCenter | Qt.AlignLeft, self.count)
        if on:
            rounded(p, QRectF(x, self.height() - 2, self.width() - 20, 2), 1, T.FG)


class EngineBadge(QAbstractButton):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.text_value = ""
        self.attention = False
        self._f = T.font(11.5, 400, mono=True)
        self._hover = _Hover(self)
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedHeight(28)

    def set(self, text: str, attention: bool) -> None:
        self.text_value = text
        self.attention = attention
        self.updateGeometry()
        self.update()

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(int(T.text_width(self._f, self.text_value) + 22 + (12 if self.attention else 0)), 28)

    def enterEvent(self, e) -> None:  # noqa: N802
        self._hover.to(1)

    def leaveEvent(self, e) -> None:  # noqa: N802
        self._hover.to(0)

    def paintEvent(self, _e) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        if self.attention:
            rounded(p, QRectF(self.rect()), T.R_MD, mix(T.FG, QColor("#E3E3E3"), self._hover.value))
            p.setPen(T.BG)
        else:
            rounded(p, QRectF(self.rect()), T.R_MD, mix(T.BG, T.S2, self._hover.value))
            p.setPen(mix(T.FG3, T.FG2, self._hover.value))
        p.setFont(self._f)
        p.drawText(QRectF(self.rect()), Qt.AlignCenter, self.text_value)


class Wordmark(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._f = T.font(16, 600)
        self.setFixedSize(int(T.text_width(self._f, APP_NAME) + 30), 56)

    def paintEvent(self, _e) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        # The mark: a frame with corner crop marks and a downward stroke into it.
        y = self.height() / 2 - 8
        draw_mark(p, QRectF(0, y, 16, 16), T.FG)
        p.setFont(self._f)
        p.setPen(T.FG)
        p.drawText(QRectF(24, 0, self.width(), self.height()), Qt.AlignVCenter | Qt.AlignLeft, APP_NAME)


def draw_mark(p: QPainter, r: QRectF, color, bg=None) -> None:
    p.save()
    p.setRenderHint(QPainter.Antialiasing)
    s = r.width() / 16
    p.translate(r.left(), r.top())
    p.scale(s, s)
    if bg is not None:
        rounded(p, QRectF(0, 0, 16, 16), 3.5, bg)
    pen = QPen(color, 1.6)
    pen.setCapStyle(Qt.FlatCap)
    p.setPen(pen)
    # corner marks
    for (x, y, dx, dy) in ((1.5, 1.5, 1, 1), (14.5, 1.5, -1, 1), (1.5, 14.5, 1, -1), (14.5, 14.5, -1, -1)):
        p.drawLine(QRectF(x, y, 0, 0).topLeft(), QRectF(x + 3.5 * dx, y, 0, 0).topLeft())
        p.drawLine(QRectF(x, y, 0, 0).topLeft(), QRectF(x, y + 3.5 * dy, 0, 0).topLeft())
    # arrow
    p.drawLine(QRectF(8, 3.6, 0, 0).topLeft(), QRectF(8, 11.2, 0, 0).topLeft())
    pen.setJoinStyle(Qt.MiterJoin)
    p.setPen(pen)
    path = QPainterPath()
    path.moveTo(5.2, 8.6)
    path.lineTo(8, 11.4)
    path.lineTo(10.8, 8.6)
    p.setBrush(Qt.NoBrush)
    p.drawPath(path)
    p.restore()


class QueuePage(QWidget):
    def __init__(self, view: QueueView, panel: OutputPanel, parent=None) -> None:
        super().__init__(parent)
        self.url = UrlBar()
        self.view = view
        self.panel = panel
        self.stats = Label("", 12, 400, T.FG3, tabular=True)
        self.pause_all = Button("Pause all", "ghost", size="sm")
        self.resume_all = Button("Resume all", "ghost", size="sm")
        self.clear = Button("Clear finished", "ghost", size="sm")
        head = hbox(self.stats, None, self.pause_all, self.resume_all, self.clear, spacing=4,
                    margins=(14, 0, 4, 0))
        left = vbox(head, self.view, spacing=6)
        panel.setFixedWidth(T.PANEL_W)
        body = hbox(left, panel, spacing=16)
        self.setLayout(vbox(self.url, 6, body, spacing=10, margins=(24, 8, 24, 20)))


class MainWindow(QMainWindow):
    quit_requested = Signal()

    def __init__(self, store: SettingsStore, engines: Engines, library: Library) -> None:
        super().__init__()
        track_keyboard_focus(QApplication.instance())
        self.store = store
        self.s = store.settings
        self.engines = engines
        self.library = library
        self._really_quit = False
        self._seen_clipboard: set[str] = set()
        self.setWindowTitle(APP_NAME)
        # Fits a 1366x768 laptop at 125 % scaling (1093x614 logical).
        self.setMinimumSize(980, 600)
        screen = QGuiApplication.primaryScreen()
        avail = screen.availableGeometry() if screen is not None else None
        if avail is not None:
            self.resize(min(1320, int(avail.width() * 0.92)), min(860, int(avail.height() * 0.92)))
        else:
            self.resize(1320, 860)

        self.thumbs = ThumbCache(self)
        self.thumbs.set_proxy(self.s.proxy)
        self.bridge = Bridge(self)
        self.qm = QueueManager(store, engines, library, self.bridge.events())
        self.model = QueueModel(self)
        self.bridge.added.connect(self.model.add)
        self.bridge.removed.connect(self.model.remove)
        self.bridge.updated.connect(self._on_job_updated)
        self.bridge.finished.connect(self._on_job_finished)
        self.bridge.review.connect(self._on_review)

        self.tasks = EngineTasks(engines, self)
        self.tasks.states.connect(self._on_engine_states)
        self.tasks.progress.connect(self._on_engine_progress)
        self.tasks.finished.connect(self._on_engine_finished)
        self.tasks.batch_done.connect(self._on_engine_batch)
        self.tasks.updates.connect(self._on_engine_updates)

        # ---- pages
        self.view = QueueView(self.model, self.thumbs)
        self.view.empty_lines = [
            ("title", "Nothing in the queue"),
            ("body", "Paste a link above. yt-dlp reads 1,800+ sites plus most pages with an embedded video, "
                     "and every file comes out ready for Premiere Pro and After Effects."),
            ("key", "Enter\tDownload with the Output settings"),
            ("key", "Shift+Enter\tInspect formats, trim, pick playlist entries"),
            ("key", "Ctrl+V\tPaste links from anywhere in the window"),
            ("key", "Drop\tLinks, browser tabs or a .txt list onto the window"),
            ("key", "Drag out\tFinished files straight into Premiere"),
        ]
        self.view.action.connect(self._job_action)
        self.view.opened.connect(self._open_job)
        self.view.context.connect(self._job_menu)
        self.panel = OutputPanel(self.s.output, self.s.recent_dirs)
        self.panel.changed.connect(self._save_settings)
        self.queue_page = QueuePage(self.view, self.panel)
        self.queue_page.url.submit.connect(self._submit)
        self.queue_page.pause_all.clicked.connect(self.qm.pause_all)
        self.queue_page.resume_all.clicked.connect(self.qm.resume_all)
        self.queue_page.clear.clicked.connect(self.qm.clear_finished)

        self.inspect_page = InspectPage(self.thumbs, self.qm.tools, self._network, self.s.output,
                                        get_nightly=self.qm.nightly_ytdlp,
                                        allow_nightly=lambda: self.s.nightly_fallback and self.s.channel != "nightly")
        self.inspect_page.back.connect(lambda: self.show_page("queue"))
        self.inspect_page.add.connect(self._add_from_inspect)
        self.library_page = LibraryPage(library, self.thumbs)
        self.library_page.action.connect(self._library_action)
        self.settings_page = SettingsPage(self.s)
        self.settings_page.changed.connect(self._settings_changed)
        self.settings_page.engine_install.connect(self._install_engines)
        self.settings_page.engine_locate.connect(self._locate_engine)
        self.settings_page.engine_check.connect(lambda: self.tasks.check_updates(["ytdlp", "deno"]))
        self.settings_page.redetect.connect(self._redetect)
        self.settings_page.reset.connect(self._reset_settings)
        self.setup_page = SetupPage()
        self.setup_page.install_requested.connect(self._install_engines)
        self.setup_page.locate_requested.connect(self._locate_engine)
        self.setup_page.skip.connect(lambda: self.show_page("queue"))

        self.stack = QStackedWidget()
        self.pages = {"queue": self.queue_page, "inspect": self.inspect_page, "library": self.library_page,
                      "settings": self.settings_page, "setup": self.setup_page}
        for w in self.pages.values():
            self.stack.addWidget(w)

        # ---- header
        self.tabs = {"queue": NavTab("Queue"), "library": NavTab("Library"), "settings": NavTab("Settings")}
        for key, tab in self.tabs.items():
            tab.clicked.connect(lambda _c=False, k=key: self.show_page(k))
        self.badge = EngineBadge()
        self.badge.clicked.connect(lambda: (self.show_page("settings"), self.settings_page.jump("engines")))
        header = hbox(24, Wordmark(), 20, *self.tabs.values(), None, self.badge, 24, spacing=4)
        header_w = QWidget()
        header_w.setLayout(header)
        header_w.setFixedHeight(56)

        root = QWidget()
        root.setObjectName("Root")
        root.setAttribute(Qt.WA_StyledBackground, True)
        root.setLayout(vbox(header_w, self.stack, spacing=0))
        self.setCentralWidget(root)
        self.toast = Toast(root)
        self.setAcceptDrops(True)

        self._shortcuts()
        self.taskbar = winapi.TaskbarProgress()
        self._timer = QTimer(self)
        self._timer.setInterval(500)
        self._timer.timeout.connect(self._tick)
        self._timer.start()
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(400)
        self._save_timer.timeout.connect(self.store.save)
        self.tray: QSystemTrayIcon | None = None

        self.qm.load()
        self.qm.start_capability_probe()
        QTimer.singleShot(1500, self._caps_to_settings)
        self.tasks.refresh_states()
        missing = engines.missing_required()
        self.show_page("setup" if missing and not self.s.onboarded else "queue")
        self.qm.schedule()
        if self.s.auto_update and not missing and due_for_check(self.s.last_update_check):
            QTimer.singleShot(4000, self._auto_update_check)
        QGuiApplication.clipboard().dataChanged.connect(self._clipboard_changed)
        for area in self.findChildren(QAbstractScrollArea):
            autohide_scrollbars(area)
        self._tick()

    # ------------------------------------------------------------------ chrome
    def showEvent(self, e) -> None:  # noqa: N802
        super().showEvent(e)
        winapi.style_window_chrome(int(self.winId()))

    def show_page(self, key: str) -> None:
        page = self.pages[key]
        self.stack.setCurrentWidget(page)
        for k, tab in self.tabs.items():
            tab.setChecked(k == key or (key == "inspect" and k == "queue") or (key == "setup" and k == "settings"))
        if key == "queue":
            self.queue_page.url.field.setFocus()
        elif key == "library":
            self.library_page.search.setFocus()

    def _shortcuts(self) -> None:
        def sc(seq, fn):
            s = QShortcut(QKeySequence(seq), self)
            s.setContext(Qt.WindowShortcut)
            s.activated.connect(fn)
        sc("Ctrl+L", lambda: (self.show_page("queue"), self.queue_page.url.field.selectAll()))
        sc("Ctrl+1", lambda: self.show_page("queue"))
        sc("Ctrl+2", lambda: self.show_page("library"))
        sc("Ctrl+3", lambda: self.show_page("settings"))
        sc("Ctrl+,", lambda: self.show_page("settings"))
        sc("Ctrl+F", lambda: self.show_page("library"))
        sc("Ctrl+O", self.panel.choose_folder)
        sc("Ctrl+Shift+P", self.qm.pause_all)
        sc("Ctrl+Shift+R", self.qm.resume_all)
        sc("Ctrl+A", lambda: self.view.selectAll() if self.stack.currentWidget() is self.queue_page
           and not self.queue_page.url.field.hasFocus() else None)
        sc("Escape", lambda: self.show_page("queue") if self.stack.currentWidget() is self.inspect_page else None)

    def keyPressEvent(self, e) -> None:  # noqa: N802
        # Ctrl+V anywhere that isn't a text field pastes links straight into the queue.
        if e.matches(QKeySequence.Paste):
            urls = extract_urls(QGuiApplication.clipboard().text())
            if urls:
                self.show_page("queue")
                self._enqueue(urls)
                return
        super().keyPressEvent(e)

    def _network(self) -> Network:
        return Network.from_settings(self.s, str(paths.cache_dir() / "yt-dlp"))

    # ------------------------------------------------------------------ adding
    def _submit(self, urls: list[str], inspect: bool) -> None:
        if self.engines.missing_required():
            self.toast.show_message("Install the engines first", "Install", lambda: self.show_page("setup"))
            self.show_page("setup")
            return
        if inspect:
            self.inspect_page.defaults = self.s.output
            self.inspect_page.open(urls[0], playlist=False)
            self.show_page("inspect")
            if len(urls) > 1:
                self.toast.show_message(f"Inspecting the first of {len(urls)} links")
            return
        self._enqueue(urls)
        self.queue_page.url.clear()

    def _enqueue(self, urls: list[str]) -> None:
        if self.engines.missing_required():
            self.show_page("setup")
            return
        self.store.remember_dir(str(self.s.output.resolved_output_dir()))
        self.qm.add_urls(urls, self.s.output)
        self._seen_clipboard.update(urls)
        if len(urls) > 1:
            self.toast.show_message(f"Added {len(urls)} links")

    def _add_from_inspect(self, urls: list, opts, playlist: bool, meta: list) -> None:
        self.qm.add_urls(urls, opts, playlist=playlist, confirmed=True, meta=meta or None)
        self.show_page("queue")
        self.queue_page.url.clear()
        self.toast.show_message(f"Added {len(urls)} to the queue" if len(urls) > 1 else "Added to the queue")

    def _on_review(self, job: Job, info: dict, children: list) -> None:
        self.inspect_page.defaults = job.opts
        self.inspect_page.open(job.url, playlist=True, info=info, children=children)
        self.show_page("inspect")
        self.toast.show_message(f"{len(children)} items. Pick the ones you want.")

    def dragEnterEvent(self, e) -> None:  # noqa: N802
        md = e.mimeData()
        if md.hasUrls() or md.hasText():
            e.acceptProposedAction()

    def dropEvent(self, e) -> None:  # noqa: N802
        md = e.mimeData()
        urls: list[str] = []
        for u in md.urls() if md.hasUrls() else []:
            if u.isLocalFile():
                p = Path(u.toLocalFile())
                if p.suffix.lower() in (".txt", ".url", ".csv", ".m3u", ".m3u8") and p.stat().st_size < 5_000_000:
                    try:
                        urls += extract_urls(p.read_text(encoding="utf-8", errors="replace"))
                    except OSError:
                        pass
            elif u.scheme() in ("http", "https"):
                urls.append(u.toString())
        if not urls and md.hasText():
            urls = extract_urls(md.text())
        urls = list(dict.fromkeys(urls))
        if urls:
            e.acceptProposedAction()
            self.show_page("queue")
            self._enqueue(urls)
            if len(urls) == 1:
                self.toast.show_message("Added to the queue")
        else:
            self.toast.show_message("No links in what you dropped")

    # ------------------------------------------------------------------ job updates
    def _on_job_updated(self, job: Job, fields: dict) -> None:
        self.model.touch(job.id)

    def _on_job_finished(self, job: Job) -> None:
        self.model.touch(job.id)
        if job.status == Status.DONE.value and not job.skipped:
            self.library_page.reload() if self.stack.currentWidget() is self.library_page else None
        if job.status == Status.FAILED.value:
            self._suggest_fix(job)
        if not self.s.notifications or self.isActiveWindow():
            return
        if job.status == Status.DONE.value:
            self._notify("Ready for editing", job.title or "Download finished")
        elif job.status == Status.FAILED.value:
            self._notify("Download failed", f"{job.title or job.url}: {(job.error or {}).get('title', '')}")

    def _suggest_fix(self, job: Job) -> None:
        """Turn the most common failures into one click, at most once a minute."""
        kind = (job.error or {}).get("kind", "")
        now = time.monotonic()
        if now - getattr(self, "_last_fix_hint", 0.0) < 60:
            return
        if kind in ("bot_check", "login", "age", "members", "private", "rate_limit") and self.s.cookies == "off":
            self._last_fix_hint = now
            self.toast.show_message("This site wants you signed in", "Set up cookies",
                                    lambda: (self.show_page("settings"), self.settings_page.jump("accounts")),
                                    ms=6000)
        elif kind == "cookie_read":
            self._last_fix_hint = now
            self.toast.show_message("The browser's cookies couldn't be read", "Choose another way",
                                    lambda: (self.show_page("settings"), self.settings_page.jump("accounts")),
                                    ms=6000)
        elif kind in ("engine", "ffmpeg"):
            self._last_fix_hint = now
            self.toast.show_message("An engine is missing", "Install", lambda: self.show_page("setup"), ms=6000)

    def _notify(self, title: str, body: str) -> None:
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        if self.tray is None:
            self.tray = QSystemTrayIcon(self.windowIcon(), self)
            self.tray.setToolTip(APP_NAME)
            self.tray.activated.connect(lambda _r: (self.showNormal(), self.raise_(), self.activateWindow()))
            self.tray.messageClicked.connect(lambda: (self.showNormal(), self.raise_(), self.activateWindow()))
            self.tray.show()
        self.tray.showMessage(title, body, QSystemTrayIcon.Information, 5000)

    def _job_action(self, job_id: str, action: str) -> None:
        job = self.qm.get(job_id)
        if job is None:
            return
        if action == "pause":
            self.qm.pause(job_id)
        elif action in ("resume", "retry"):
            self.qm.resume(job_id)
        elif action == "cancel":
            self.qm.cancel(job_id)
        elif action == "remove":
            self.qm.remove([job_id])
        elif action == "finish":
            self.qm.finish_live(job_id)
        elif action == "log":
            LogViewer(self, job.title or job.url, job.log_path).exec()
        elif action == "reveal":
            if job.outputs:
                reveal(job.outputs[0])
        self.model.touch(job_id)

    def _open_job(self, job_id: str) -> None:
        job = self.qm.get(job_id)
        if job is None:
            return
        if job.done and job.outputs and Path(job.outputs[0]).exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(job.outputs[0]))
        elif job.status == Status.FAILED.value:
            LogViewer(self, job.title or job.url, job.log_path).exec()

    def _job_menu(self, ids: list[str], pos: QPoint) -> None:
        jobs = [j for j in (self.qm.get(i) for i in ids) if j is not None]
        if not jobs:
            return
        m = make_menu(self)
        one = jobs[0] if len(jobs) == 1 else None
        done = [j for j in jobs if j.done and j.outputs]
        if one and done:
            m.addAction("Open", lambda: self._open_job(one.id))
            m.addAction("Show in Explorer", lambda: reveal(one.outputs[0]))
            m.addAction("Copy file path", lambda: QGuiApplication.clipboard().setText(one.outputs[0]))
            m.addSeparator()
        if one:
            m.addAction("Copy link", lambda: QGuiApplication.clipboard().setText(one.webpage_url or one.url))
            m.addAction("Open link in browser", lambda: QDesktopServices.openUrl(QUrl(one.webpage_url or one.url)))
            m.addAction("Inspect again", lambda: (self.inspect_page.open(one.webpage_url or one.url),
                                                  self.show_page("inspect")))
            m.addAction("Show log", lambda: LogViewer(self, one.title or one.url, one.log_path).exec())
            if (one.error or {}).get("kind") in ("bot_check", "login", "age", "members", "private", "rate_limit",
                                                 "cookie_read"):
                m.addAction("Sign in with cookies…", lambda: (self.show_page("settings"),
                                                             self.settings_page.jump("accounts")))
            if one.plan_reasons or one.report:
                m.addAction("Why it was converted", lambda: self._explain(one))
            m.addSeparator()
        if any(j.status in (Status.ACTIVE.value, Status.QUEUED.value) for j in jobs):
            m.addAction("Pause", lambda: [self.qm.pause(j.id) for j in jobs])
        if any(j.status in (Status.PAUSED.value, Status.FAILED.value, Status.CANCELED.value) for j in jobs):
            m.addAction("Resume" if all(j.status == Status.PAUSED.value for j in jobs) else "Retry",
                        lambda: [self.qm.resume(j.id) for j in jobs])
        if done:
            m.addAction("Download again", lambda: self.qm.add_urls([j.webpage_url or j.url for j in done],
                                                                   self.s.output, confirmed=True))
        m.addAction("Move to top", lambda: [self._move(j.id, 0) for j in reversed(jobs)])
        m.addSeparator()
        m.addAction("Remove from queue", lambda: self.qm.remove([j.id for j in jobs]))
        if done:
            m.addAction("Delete file" + ("s" if len(done) > 1 or len(done[0].outputs) > 1 else ""),
                        lambda: self._delete_files(done))
        show_menu(m, pos)

    def _move(self, job_id: str, to: int) -> None:
        self.qm.move(job_id, to)
        self.model.move(job_id, to)

    def _explain(self, job: Job) -> None:
        lines = []
        if job.plan_summary:
            lines.append(job.plan_summary)
        if job.plan_reasons:
            lines.append("Converted because: " + "; ".join(job.plan_reasons) + ".")
        elif job.plan_summary:
            lines.append("Nothing needed re-encoding, so the original streams were copied.")
        rep = job.report or {}
        for c in rep.get("checks", []):
            lines.append(f"{'✓' if c['ok'] else '✕'} {c['name']}  {c['detail']}")
        for n in rep.get("notes", []):
            lines.append(n)
        for w in job.warnings:
            lines.append("Note: " + w)
        Confirm(self, "What Ingest did", "\n".join(lines), ok="Close", cancel="").exec()

    def _delete_files(self, jobs: list[Job]) -> None:
        files = [f for j in jobs for f in j.outputs + j.sidecars if Path(f).exists()]
        if not files:
            return
        if not Confirm.ask(self, f"Delete {len(files)} file{'s' if len(files) != 1 else ''}?",
                           "They go to the Recycle Bin when possible." if sys.platform == "win32" else
                           "This can't be undone.", ok="Delete", cancel="Keep"):
            return
        failed = 0
        for f in files:
            if not trash(f):
                failed += 1
        self.qm.remove([j.id for j in jobs])
        self.toast.show_message("Deleted" if not failed else f"{failed} file(s) couldn't be deleted")

    # ------------------------------------------------------------------ library
    def _library_action(self, item_id: str, action: str, pos: QPoint) -> None:
        it = self.library.get(item_id)
        if it is None:
            return
        if action == "open" and it.exists:
            QDesktopServices.openUrl(QUrl.fromLocalFile(it.path))
        elif action == "reveal":
            reveal(it.path)
        elif action == "forget":
            self.library.remove(item_id)
            self.library_page.reload()
        elif action == "more":
            m = make_menu(self)
            if it.exists:
                m.addAction("Open", lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(it.path)))
                m.addAction("Show in Explorer", lambda: reveal(it.path))
                m.addAction("Copy file path", lambda: QGuiApplication.clipboard().setText(it.path))
            m.addAction("Copy link", lambda: QGuiApplication.clipboard().setText(it.webpage_url or it.url))
            m.addAction("Open link in browser", lambda: QDesktopServices.openUrl(QUrl(it.webpage_url or it.url)))
            m.addAction("Download again", lambda: (self.qm.add_urls([it.webpage_url or it.url], self.s.output,
                                                                    confirmed=True), self.show_page("queue")))
            m.addSeparator()
            m.addAction("Remove from library", lambda: (self.library.remove(item_id), self.library_page.reload()))
            if it.exists:
                m.addAction("Delete file", lambda: self._delete_library_file(it))
            show_menu(m, pos)

    def _delete_library_file(self, it) -> None:
        if Confirm.ask(self, "Delete this file?", it.path, ok="Delete", cancel="Keep"):
            trash(it.path)
            self.library.remove(it.id)
            self.library_page.reload()

    # ------------------------------------------------------------------ settings and engines
    def _save_settings(self) -> None:
        self._save_timer.start()

    def _settings_changed(self) -> None:
        self.thumbs.set_proxy(self.s.proxy)
        self.qm.invalidate_tools()
        self.panel.refresh(full=True)
        self._save_settings()
        self.qm.schedule()

    def _reset_settings(self) -> None:
        if not Confirm.ask(self, "Reset all settings?", "Output, network, account and engine settings go back to "
                           "their defaults. Your queue, library and engines stay.", ok="Reset", cancel="Keep"):
            return
        from ..core.settings import Settings
        fresh = Settings()
        fresh.onboarded = True
        for f in fresh.__dataclass_fields__:
            setattr(self.s, f, getattr(fresh, f))
        self.store.save()
        self.toast.show_message("Settings reset. Restart Ingest to refresh every page.")
        self.panel.set_options(self.s.output)

    def _install_engines(self, keys: list[str]) -> None:
        if not self.tasks.install(keys):
            self.toast.show_message("An engine install is already running")
            return
        self.setup_page.set_busy(True)

    def _on_engine_progress(self, key: str, phase: str, done: float, total: float) -> None:
        for rows in (self.setup_page.rows, self.settings_page.engine_rows):
            if key in rows:
                rows[key].set_progress(phase, done, total)

    def _on_engine_finished(self, key: str, ok: bool, message: str) -> None:
        for rows in (self.setup_page.rows, self.settings_page.engine_rows):
            if key in rows:
                rows[key].set_done(ok, message)
        if not ok:
            self.toast.show_message(f"{message[:120]}")

    def _on_engine_batch(self, ok: bool) -> None:
        self.setup_page.set_busy(False)
        self.qm.invalidate_tools()
        self.qm.refresh_capabilities()
        QTimer.singleShot(3000, self._caps_to_settings)
        if not self.engines.missing_required():
            self.s.onboarded = True
            self._save_settings()
            if self.stack.currentWidget() is self.setup_page:
                self.show_page("queue")
                self.toast.show_message("Engines ready. Paste a link to start.")
            self.qm.schedule()

    def _on_engine_states(self, states: dict) -> None:
        self.setup_page.set_states(states)
        self.settings_page.set_engine_states(states)
        y = states.get("ytdlp")
        missing = [k for k in ("ytdlp", "ffmpeg", "deno") if not states.get(k) or not states[k].path]
        if missing:
            from ..core.engines import SPECS
            names = [SPECS[k].name for k in missing]
            self.badge.set(f"{names[0]} missing" if len(names) == 1 else "Engines missing", True)
        elif any(st.update for st in states.values()):
            self.badge.set("Update available", True)
        else:
            self.badge.set(f"yt-dlp {y.version}" if y and y.version else "yt-dlp", False)

    def _on_engine_updates(self, res: dict) -> None:
        avail = [k for k, (a, _l) in res.items() if a]
        self.s.last_update_check = time.time()
        self._save_settings()
        if avail and self.s.auto_update and not any(j.active for j in self.qm.jobs):
            self._install_engines(avail)
        elif avail:
            self.toast.show_message("Engine updates available", "Update", lambda: self._install_engines(avail))

    def _auto_update_check(self) -> None:
        self.tasks.check_updates(["ytdlp", "deno"])

    def _locate_engine(self, key: str) -> None:
        path = pick_engine_file(self, key)
        if not path:
            return
        attr = {"ytdlp": "path_ytdlp", "ffmpeg": "path_ffmpeg", "deno": "path_deno", "gallerydl": "path_gallerydl"}[key]
        setattr(self.s, attr, os.path.normpath(path))
        self.store.save()
        self.qm.invalidate_tools()
        self.tasks.refresh_states()
        if key == "ffmpeg":
            self.qm.refresh_capabilities()
        if not self.engines.missing_required() and self.stack.currentWidget() is self.setup_page:
            self.s.onboarded = True
            self.show_page("queue")

    def _redetect(self) -> None:
        self.settings_page.detected.setText("Detecting encoders…")
        self.qm.refresh_capabilities()
        QTimer.singleShot(2500, self._caps_to_settings)

    def _caps_to_settings(self) -> None:
        if not self.qm._caps_ready.is_set():
            QTimer.singleShot(1000, self._caps_to_settings)
            return
        caps = self.qm.caps()
        self.settings_page.set_detected(caps.hw_h264, caps.zscale)

    # ------------------------------------------------------------------ clipboard
    def _clipboard_changed(self) -> None:
        if self.isActiveWindow():
            return  # copying inside Ingest isn't a suggestion
        self._pending_clip = True

    def changeEvent(self, e) -> None:  # noqa: N802
        if e.type() == QEvent.ActivationChange and self.isActiveWindow():
            QTimer.singleShot(60, self._check_clipboard)
        super().changeEvent(e)

    def _check_clipboard(self) -> None:
        if not self.s.clipboard_suggest:
            return
        text = QGuiApplication.clipboard().text().strip()
        if not text or len(text) > 4000:
            return
        urls = extract_urls(text)
        if len(urls) != 1 or not is_url(urls[0]) or urls[0] in self._seen_clipboard:
            return
        if any((j.webpage_url or j.url) == urls[0] or j.url == urls[0] for j in self.qm.jobs):
            return
        self._seen_clipboard.add(urls[0])
        self.queue_page.url.suggest_clipboard(urls[0])

    # ------------------------------------------------------------------ periodic
    def _tick(self) -> None:
        c = self.qm.counts()
        dl = [j for j in self.qm.jobs if j.active and j.stage in (Stage.DOWNLOAD.value, Stage.RESOLVE.value)]
        cv = [j for j in self.qm.jobs if j.active and j.stage in (Stage.CONVERT.value, Stage.VERIFY.value)]
        speed = sum(j.speed or 0 for j in dl)
        parts = []
        if dl:
            parts.append(f"{len(dl)} downloading" + (f" at {human_speed(speed)}" if speed else ""))
        if cv:
            parts.append(f"{len(cv)} converting")
        if c["queued"]:
            parts.append(f"{c['queued']} waiting")
        if c["paused"]:
            parts.append(f"{c['paused']} paused")
        if c["failed"]:
            parts.append(f"{c['failed']} failed")
        free = disk_free(self.s.output.resolved_output_dir())
        if free is not None:
            parts.append(f"{human_bytes(free)} free")
        self.queue_page.stats.setText("  ·  ".join(parts))
        busy = c["active"] + c["queued"]
        self.tabs["queue"].set_count(str(busy) if busy else "")
        self.queue_page.pause_all.setVisible(bool(c["active"] or c["queued"]))
        self.queue_page.resume_all.setVisible(bool(c["paused"]))
        self.queue_page.clear.setVisible(bool(c["done"]))
        frac, paused = self.qm.active_progress()
        hwnd = int(self.winId())
        if frac is None:
            self.taskbar.set(hwnd, None)
        else:
            self.taskbar.set(hwnd, frac, winapi.TaskbarProgress.NORMAL)
        self.setWindowTitle(f"{APP_NAME}  {frac * 100:.0f}%" if frac is not None else APP_NAME)

    # ------------------------------------------------------------------ closing
    def closeEvent(self, e) -> None:  # noqa: N802
        busy = [j for j in self.qm.jobs if j.active]
        if busy and not self._really_quit:
            if not Confirm.ask(self, "Quit while downloading?",
                               f"{len(busy)} item{'s are' if len(busy) != 1 else ' is'} still working. "
                               "Partial downloads are kept and resume next time.", ok="Quit", cancel="Keep working"):
                e.ignore()
                return
        self.store.save()
        self.qm.shutdown()
        if self.tray is not None:
            self.tray.hide()
        try:
            self.library.close()
        except Exception:
            pass
        e.accept()
        QApplication.instance().quit()


def show_menu(menu, pos: QPoint) -> None:
    """One place that blocks on a menu, so tests can replace it."""
    menu.exec(pos)


def disk_free(path: Path) -> int | None:
    p = Path(path)
    while not p.exists() and p.parent != p:
        p = p.parent
    try:
        return shutil.disk_usage(p).free
    except OSError:
        return None


def reveal(path: str) -> None:
    p = Path(path)
    if sys.platform == "win32":
        if p.exists():
            subprocess.Popen(["explorer", "/select,", str(p)], creationflags=0x08000000)
        else:
            os.startfile(str(p.parent)) if p.parent.exists() else None  # noqa: S606
    elif sys.platform == "darwin":
        subprocess.Popen(["open", "-R", str(p)])
    else:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(p.parent)))


def trash(path: str) -> bool:
    """Recycle Bin on Windows (SHFileOperation), plain delete elsewhere."""
    p = Path(path)
    if not p.exists():
        return True
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            class SHFILEOPSTRUCTW(ctypes.Structure):
                _fields_ = [("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT), ("pFrom", wintypes.LPCWSTR),
                            ("pTo", wintypes.LPCWSTR), ("fFlags", ctypes.c_uint16), ("fAnyOperationsAborted",
                                                                                    wintypes.BOOL),
                            ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", wintypes.LPCWSTR)]
            op = SHFILEOPSTRUCTW(None, 3, str(p) + "\0", None, 0x0040 | 0x0010 | 0x0004 | 0x0400, False, None, None)
            return ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op)) == 0
        except Exception:
            pass
    try:
        p.unlink()
        return True
    except OSError:
        return False


def app_icon() -> QIcon:
    ico = paths.asset("icon.ico")
    if ico.exists():
        return QIcon(str(ico))
    return icons.icon("arrow-down", 32)

