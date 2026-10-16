"""Queue behaviour against a fake yt-dlp: playlists, pause/resume, cancel, cookie retry, persistence."""

import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from ingest.core.engines import Engines
from ingest.core.library import Library
from ingest.core.queue import Events, QueueManager
from ingest.core.settings import SettingsStore

FAKE = Path(__file__).with_name("fake_ytdlp.py")
pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="fake yt-dlp is a POSIX script")


@pytest.fixture
def media(ffmpeg, tmp_path_factory, monkeypatch):
    d = tmp_path_factory.mktemp("media")
    v, a = d / "v.mp4", d / "a.m4a"
    if not v.exists():
        subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=s=640x360:r=30:d=2",
                        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-an", str(v)], check=True)
        subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=d=2:r=48000",
                        "-c:a", "aac", str(a)], check=True)
    monkeypatch.setenv("FAKE_MEDIA_V1", str(v))
    monkeypatch.setenv("FAKE_MEDIA_A1", str(a))
    return v, a


class Recorder:
    def __init__(self):
        self.lock = threading.Lock()
        self.reviews = []
        self.max_active = 0

    def updated(self, job, fields):
        pass


@pytest.fixture
def qm(tmp_path, ffmpeg, media):
    FAKE.chmod(FAKE.stat().st_mode | stat.S_IXUSR)
    store = SettingsStore(tmp_path / "settings.json")
    s = store.settings
    s.path_ytdlp = str(FAKE)
    s.path_ffmpeg = ffmpeg
    s.path_deno = "/nonexistent"
    s.output.output_dir = str(tmp_path / "out")
    s.skip_existing = False
    s.gallery_fallback = False
    rec = Recorder()
    events = Events(updated=rec.updated, review=lambda job, info, children: rec.reviews.append(len(children)))
    m = QueueManager(store, Engines(lambda: store.settings), Library(tmp_path / "lib.sqlite3"), events,
                     state_path=tmp_path / "queue.json")
    m.rec = rec
    m.start_capability_probe()
    yield m
    m.shutdown(2)


def wait(qm, pred, timeout=60):
    t0 = time.time()
    while time.time() - t0 < timeout:
        active = sum(1 for j in qm.jobs if j.status == "active" and j.stage in ("resolve", "download"))
        qm.rec.max_active = max(qm.rec.max_active, active)
        if pred():
            return True
        time.sleep(0.05)
    raise AssertionError("timed out: " + repr([(j.url, j.status, j.stage, j.stage_text, j.error) for j in qm.jobs]))


def settled(qm):
    return lambda: qm.jobs and all(j.status in ("done", "failed", "canceled", "paused") for j in qm.jobs)


def test_single_video(qm):
    (job,) = qm.add_urls(["fake://video/one"], qm.store.settings.output)
    wait(qm, settled(qm))
    assert job.status == "done", job.error
    assert job.outputs and Path(job.outputs[0]).exists()
    assert job.outputs[0].endswith("Fake one.mp4")
    assert job.report["premiere"] is True
    assert not Path(job.outputs[0]).parent.joinpath(".ingest-work").exists()
    assert qm.library.count() == 1


def test_playlist_expands(qm):
    qm.add_urls(["fake://playlist/3"], qm.store.settings.output)
    wait(qm, lambda: len(qm.jobs) == 3 and all(j.status == "done" for j in qm.jobs))
    assert sorted(j.playlist["index"] for j in qm.jobs) == [1, 2, 3]


def test_big_playlist_goes_to_review(qm):
    qm.store.settings.playlist_confirm = 2
    qm.add_urls(["fake://playlist/5"], qm.store.settings.output)
    wait(qm, lambda: qm.rec.reviews == [5])
    assert qm.jobs == []


def test_concurrency_limit(qm):
    qm.store.settings.max_concurrent = 1
    qm.add_urls([f"fake://video/c{i}?slow" for i in range(3)], qm.store.settings.output)
    wait(qm, settled(qm), timeout=90)
    assert qm.rec.max_active == 1
    assert all(j.status == "done" for j in qm.jobs)


def test_pause_and_resume_download(qm):
    (job,) = qm.add_urls(["fake://video/p?slow"], qm.store.settings.output)
    wait(qm, lambda: job.stage == "download" and job.dl_progress > 0.05)
    qm.pause(job.id)
    wait(qm, lambda: job.status == "paused")
    assert Path(job.work_dir).is_dir()
    qm.resume(job.id)
    wait(qm, lambda: job.status == "done", timeout=90)


def test_cancel_cleans_work(qm):
    (job,) = qm.add_urls(["fake://video/x?slow"], qm.store.settings.output)
    wait(qm, lambda: job.stage == "download" and job.dl_progress > 0.05)
    wd = job.work_dir
    qm.cancel(job.id)
    wait(qm, lambda: job.status == "canceled")
    assert not Path(wd).exists()


def test_failure_then_cookie_retry(qm, tmp_path):
    (job,) = qm.add_urls(["fake://needs-cookies/k"], qm.store.settings.output)
    wait(qm, settled(qm))
    assert job.status == "failed" and job.error["kind"] == "bot_check"
    cookies = tmp_path / "cookies.txt"
    cookies.write_text("# Netscape HTTP Cookie File\n")
    s = qm.store.settings
    s.cookies, s.cookies_file = "file", str(cookies)
    (job2,) = qm.add_urls(["fake://needs-cookies/k2"], s.output)
    wait(qm, lambda: job2.status in ("done", "failed"))
    assert job2.status == "done", job2.error
    assert job2.attempts.get("cookies") is True


def test_unavailable_reason(qm):
    (job,) = qm.add_urls(["fake://gone/z"], qm.store.settings.output)
    wait(qm, settled(qm))
    assert job.error["kind"] == "unavailable"


def test_persistence_roundtrip(qm, tmp_path):
    qm.store.settings.max_concurrent = 1
    jobs = qm.add_urls(["fake://video/a?slow", "fake://video/b?slow"], qm.store.settings.output)
    wait(qm, lambda: jobs[0].stage == "download")
    qm.shutdown(3)
    m2 = QueueManager(qm.store, qm.engines, qm.library, Events(), state_path=tmp_path / "queue.json")
    m2.load()
    assert [j.url for j in m2.jobs] == ["fake://video/a?slow", "fake://video/b?slow"]
    assert all(j.status == "queued" for j in m2.jobs)
    m2.start_capability_probe()
    m2.schedule()
    m2.rec = qm.rec
    wait(m2, lambda: all(j.status == "done" for j in m2.jobs), timeout=120)
    m2.shutdown(1)
