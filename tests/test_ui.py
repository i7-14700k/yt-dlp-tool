"""Drive the real main window offscreen with a fake yt-dlp and a real FFmpeg."""

import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="fake yt-dlp is a POSIX script")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

FAKE = Path(__file__).with_name("fake_ytdlp.py")


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from ingest.ui import theme
    theme.load_fonts()
    app.setPalette(theme.palette())
    app.setStyleSheet(theme.stylesheet())
    return app


@pytest.fixture(autouse=True)
def no_slot_exceptions(monkeypatch):
    """Exceptions inside Qt slots are only printed; make them fail the test."""
    caught = []
    monkeypatch.setattr(sys, "excepthook", lambda t, v, tb: caught.append((t, v)))
    yield
    assert not caught, caught


def pump(app, seconds=0.2):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.01)


def wait_for(app, pred, timeout=60):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        app.processEvents()
        if pred():
            return True
        time.sleep(0.02)
    raise AssertionError("timed out")


@pytest.fixture
def window(qapp, tmp_path, ffmpeg, monkeypatch):
    d = tmp_path / "media"
    d.mkdir()
    v, a = d / "v.mp4", d / "a.m4a"
    subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=s=640x360:r=30:d=2",
                    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-an", str(v)], check=True)
    subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=d=2:r=48000", "-c:a", "aac",
                    str(a)], check=True)
    monkeypatch.setenv("FAKE_MEDIA_V1", str(v))
    monkeypatch.setenv("FAKE_MEDIA_A1", str(a))
    FAKE.chmod(FAKE.stat().st_mode | stat.S_IXUSR)

    from ingest.core.engines import Engines
    from ingest.core.library import Library
    from ingest.core.settings import SettingsStore
    from ingest.ui.main_window import MainWindow
    store = SettingsStore(tmp_path / "settings.json")
    s = store.settings
    s.path_ytdlp, s.path_ffmpeg, s.path_deno = str(FAKE), ffmpeg, sys.executable
    s.output.output_dir = str(tmp_path / "out")
    s.skip_existing = False
    s.onboarded = True
    s.clipboard_suggest = False
    s.auto_update = False
    win = MainWindow(store, Engines(lambda: store.settings), Library(tmp_path / "lib.sqlite3"))
    win.qm.state_path = tmp_path / "queue.json"
    win.resize(1320, 860)
    win.show()
    pump(qapp, 0.3)
    yield win
    win._really_quit = True
    win.qm.shutdown(2)
    win.hide()
    win.deleteLater()
    pump(qapp, 0.1)


def test_enter_downloads_and_library_lists_it(qapp, window):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    assert window.stack.currentWidget() is window.queue_page
    field = window.queue_page.url.field
    field.setFocus()
    QTest.keyClicks(field, "https://fake.test/video/ui-one")
    QTest.keyClick(field, Qt.Key_Return)
    wait_for(qapp, lambda: window.model.rowCount() == 1)
    assert field.text() == ""
    job = window.model.job(0)
    wait_for(qapp, lambda: job.status in ("done", "failed"), timeout=90)
    assert job.status == "done", job.error
    assert Path(job.outputs[0]).exists()
    window.show_page("library")
    pump(qapp, 0.3)
    assert window.library_page.model.rowCount() == 1
    assert "1 item" in window.library_page.count.text()


def test_multi_link_paste_hint_and_enqueue(qapp, window):
    field = window.queue_page.url.field
    field.paste_links("one fake://x https://a.example/v1 and https://b.example/v2\nhttps://a.example/v1")
    assert field.text() == "https://a.example/v1 https://b.example/v2"
    assert "2 links" in window.queue_page.url.hint.text()


def test_invalid_input_shows_reason(qapp, window):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    field = window.queue_page.url.field
    field.setText("not a link at all")
    QTest.keyClick(field, Qt.Key_Return)
    assert "doesn't look like a link" in window.queue_page.url.hint.text()
    assert window.model.rowCount() == 0


def test_inspect_then_add(qapp, window):
    window._submit(["https://fake.test/video/inspect-me"], True)
    page = window.inspect_page
    assert window.stack.currentWidget() is page
    wait_for(qapp, lambda: page.stack.currentWidget() is page.video, timeout=30)
    assert page.v_title.text() == "Fake inspect-me"
    assert page.vtable.rows and page.vtable.rows[0][0] == "auto"
    page.trim_in.setText("0:00.5")
    pump(qapp, 0.05)
    assert "clip" in page.trim_note.text()
    page.trim_in.setText("garbage")
    pump(qapp, 0.05)
    assert not page.add_btn.isEnabled()
    page.trim_in.setText("")
    pump(qapp, 0.05)
    page.add_btn.click()
    wait_for(qapp, lambda: window.model.rowCount() == 1)
    assert window.stack.currentWidget() is window.queue_page
    job = window.model.job(0)
    wait_for(qapp, lambda: job.status in ("done", "failed"), timeout=90)
    assert job.status == "done", job.error


def test_inspect_failure_page(qapp, window):
    window._submit(["https://fake.test/gone/nothing"], True)
    page = window.inspect_page
    wait_for(qapp, lambda: page.stack.currentWidget() is page.error, timeout=30)
    assert "isn't available" in page.err_title.text()


def test_drop_text_enqueues(qapp, window):
    from PySide6.QtCore import QMimeData, QPointF, Qt
    from PySide6.QtGui import QDropEvent
    md = QMimeData()
    md.setText("https://fake.test/video/dropped")
    ev = QDropEvent(QPointF(10, 10), Qt.CopyAction, md, Qt.LeftButton, Qt.NoModifier)
    window.dropEvent(ev)
    wait_for(qapp, lambda: window.model.rowCount() == 1)


def test_pause_resume_from_row_actions(qapp, window):
    window._enqueue(["https://fake.test/video/slowrow?slow"])
    wait_for(qapp, lambda: window.model.rowCount() == 1)
    job = window.model.job(0)
    wait_for(qapp, lambda: job.stage == "download" and job.dl_progress > 0.05, timeout=30)
    window.view.action.emit(job.id, "pause")
    wait_for(qapp, lambda: job.status == "paused", timeout=30)
    window.view.action.emit(job.id, "resume")
    wait_for(qapp, lambda: job.status == "done", timeout=90)


def test_output_panel_persists(qapp, window, tmp_path):
    panel = window.panel
    panel.profiles.changed.emit("prores")
    panel._on_profile("prores")
    assert window.s.output.profile == "prores"
    assert panel.prores.isVisible()
    panel.res.set_value("1080", emit=True)
    wait_for(qapp, lambda: (tmp_path / "settings.json").exists() and
             json.loads((tmp_path / "settings.json").read_text())["output"]["resolution"] == "1080", timeout=5)
    saved = json.loads((tmp_path / "settings.json").read_text())
    assert saved["output"]["profile"] == "prores"


def test_settings_page_toggles_save(qapp, window, tmp_path):
    window.show_page("settings")
    pump(qapp, 0.2)
    page = window.settings_page
    page.jump("accounts")
    page.cookie_mode.set_value("file", emit=True)
    pump(qapp, 0.05)
    assert page.file_row.isVisible() and not page.browser_row.isVisible()
    wait_for(qapp, lambda: (tmp_path / "settings.json").exists() and
             json.loads((tmp_path / "settings.json").read_text())["cookies"] == "file", timeout=5)


def test_every_page_renders(qapp, window, tmp_path):
    for key in ("queue", "library", "settings", "setup", "inspect"):
        window.show_page(key)
        pump(qapp, 0.1)
        img = window.grab()
        assert not img.isNull() and img.width() > 1000


def _collect_menus(monkeypatch):
    from ingest.ui import dialogs
    menus = []
    monkeypatch.setattr("ingest.ui.main_window.show_menu", lambda m, pos: menus.append(m))
    monkeypatch.setattr(dialogs.LogViewer, "exec", lambda self, *a, **k: 0)
    monkeypatch.setattr(dialogs.Confirm, "exec", lambda self, *a, **k: 0)
    return menus


def test_every_queue_menu_action_runs(qapp, window, monkeypatch):
    from PySide6.QtCore import QPoint
    from PySide6.QtGui import QGuiApplication
    menus = _collect_menus(monkeypatch)
    monkeypatch.setattr("ingest.ui.main_window.reveal", lambda p: None)
    monkeypatch.setattr("ingest.ui.main_window.QDesktopServices.openUrl", lambda u: True)
    window._enqueue(["https://fake.test/video/menu-done", "https://fake.test/gone/menu-failed"])
    wait_for(qapp, lambda: window.model.rowCount() == 2)
    jobs = [window.model.job(0), window.model.job(1)]
    wait_for(qapp, lambda: all(j.status in ("done", "failed") for j in jobs), timeout=90)
    for job in jobs:
        window._job_menu([job.id], QPoint(0, 0))
        menu = menus.pop()
        names = [a.text() for a in menu.actions() if a.text()]
        assert names
        for action in menu.actions():
            if action.text() and not action.text().startswith(("Delete", "Remove", "Download again", "Inspect")):
                action.trigger()
                pump(qapp, 0.05)
    window._job_menu([j.id for j in jobs], QPoint(0, 0))
    assert menus
    assert QGuiApplication.clipboard().text()


def test_library_menu_actions_run(qapp, window, monkeypatch):
    from PySide6.QtCore import QPoint
    menus = _collect_menus(monkeypatch)
    monkeypatch.setattr("ingest.ui.main_window.reveal", lambda p: None)
    monkeypatch.setattr("ingest.ui.main_window.QDesktopServices.openUrl", lambda u: True)
    window._enqueue(["https://fake.test/video/lib-menu"])
    wait_for(qapp, lambda: window.model.rowCount() == 1)
    job = window.model.job(0)
    wait_for(qapp, lambda: job.status == "done", timeout=90)
    window.show_page("library")
    pump(qapp, 0.2)
    item = window.library_page.model.items[0]
    window._library_action(item.id, "more", QPoint(0, 0))
    menu = menus.pop()
    for action in menu.actions():
        if action.text() and not action.text().startswith(("Delete", "Remove", "Download again")):
            action.trigger()
            pump(qapp, 0.05)
    window._library_action(item.id, "reveal", QPoint(0, 0))
    window._library_action(item.id, "open", QPoint(0, 0))
    window._library_action(item.id, "forget", QPoint(0, 0))
    pump(qapp, 0.1)
    assert window.library_page.model.rowCount() == 0


def test_big_playlist_opens_review(qapp, window):
    window.s.playlist_confirm = 2
    window._enqueue(["https://fake.test/playlist/5"])
    page = window.inspect_page
    wait_for(qapp, lambda: window.stack.currentWidget() is page and page.stack.currentWidget() is page.playlist,
             timeout=30)
    assert len(page.entries.entries) == 5
    page.range.setText("2-3")
    page.range.returnPressed.emit()
    assert sum(page.entries.checked) == 2
    page.p_add.click()
    wait_for(qapp, lambda: window.model.rowCount() == 2)


def test_engine_rows_and_badge(qapp, window):
    from ingest.core.engines import EngineState
    states = {k: EngineState(key=k, path="/x", version="1.0", source="managed", ok=True)
              for k in ("ytdlp", "ffmpeg", "deno", "gallerydl", "ytdlp_nightly")}
    window._on_engine_states(states)
    assert window.badge.text_value == "yt-dlp 1.0"
    states["deno"] = EngineState(key="deno")
    window._on_engine_states(states)
    assert window.badge.text_value == "Deno missing" and window.badge.attention
    window._on_engine_progress("ytdlp", "Downloading", 5e6, 18e6)
    window._on_engine_finished("ytdlp", True, "yt-dlp 2.0")
    window._on_engine_batch(True)


def test_suggest_cookie_fix_toast(qapp, window):
    from ingest.core.jobs import Job
    from ingest.core.settings import OutputOptions
    from ingest.core.ytdlp import Network
    job = Job(url="https://x", opts=OutputOptions(), net=Network())
    job.status = "failed"
    job.error = {"kind": "bot_check", "title": "t", "hint": "h", "raw": ""}
    window._suggest_fix(job)
    assert window.toast.isVisible() and "signed in" in window.toast.text
