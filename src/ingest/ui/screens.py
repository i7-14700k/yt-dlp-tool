"""Headless modes: --screenshot renders every page to PNG, --selftest checks the install.

Both run on CI (windows-latest) to prove the frozen build starts, finds its
fonts and draws its pages.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from PySide6.QtGui import QFontInfo
from PySide6.QtWidgets import QApplication

from ..core.jobs import Job, Stage, Status
from ..core.settings import copy_options
from ..core.ytdlp import Network
from . import theme as T


def _pump(app: QApplication, seconds: float = 0.3) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.01)


def demo_jobs(opts, thumbs: list[str]) -> list[Job]:
    def j(title, site, uploader, dur, thumb_i, **kw) -> Job:
        job = Job(url=f"https://example.com/{abs(hash(title))}", opts=copy_options(opts), net=Network())
        job.title, job.site, job.uploader, job.duration = title, site, uploader, dur
        job.thumbnail = thumbs[thumb_i % len(thumbs)] if thumbs else ""
        for k, v in kw.items():
            setattr(job, k, v)
        return job

    report_ok = {"premiere": True, "after_effects": True, "checks": [], "notes": []}
    return [
        j("Starship Flight 6: launch from Starbase, full broadcast", "YouTube", "SpaceX", 3725, 0,
          status=Status.ACTIVE.value, stage=Stage.DOWNLOAD.value, stage_text="Downloading video",
          dl_progress=0.62, speed=14.2 * 1024 * 1024, eta=31, downloaded=1.21e9, total=1.95e9,
          selection={"vcodec": "vp9", "height": 2160, "width": 3840, "fps": 59.94}),
        j("Tokyo at night, 8K walking tour, Shinjuku to Shibuya", "Vimeo", "Rambalac", 2418, 1,
          status=Status.ACTIVE.value, stage=Stage.CONVERT.value, stage_text="Converting to H.264",
          dl_progress=1.0, cv_progress=0.34, cv_speed=2.4, eta=212,
          plan_reasons=["AV1 isn't supported by Premiere or After Effects"],
          selection={"vcodec": "av1", "height": 2160, "width": 3840, "fps": 29.97}),
        j("Interview B-roll, studio lighting setup", "X", "Studio North", 94, 2,
          status=Status.ACTIVE.value, stage=Stage.RESOLVE.value, stage_text="Reading page"),
        j("Field recording: harbour ambience, 48 kHz", "SoundCloud", "Ambient Archive", 612, 3,
          status=Status.QUEUED.value, stage_text="Waiting"),
        j("Product teaser, 9:16 vertical cut", "Instagram", "atelier.form", 31, 4,
          status=Status.DONE.value, stage_text="Done", dl_progress=1, cv_progress=1, report=report_ok,
          out_props={"vcodec": "h264", "width": 1080, "height": 1920, "rate": "30000/1001", "acodec": "aac",
                     "sample_rate": 48000, "size": 24_800_000},
          selection={"vcodec": "h264", "height": 1920, "width": 1080, "fps": 29.97}),
        j("Members-only livestream replay", "YouTube", "Creator Club", 5400, 5,
          status=Status.FAILED.value, stage=Stage.RESOLVE.value,
          error={"kind": "members", "title": "Members-only or paid content",
                 "hint": "Sign in with an account that has access, through browser cookies."}),
        j("Drone pass over the coastline, 60p", "Dailymotion", "Aerials", 187, 6,
          status=Status.PAUSED.value, stage=Stage.DOWNLOAD.value, stage_text="Paused", dl_progress=0.48,
          selection={"vcodec": "h264", "height": 1080, "width": 1920, "fps": 60}),
    ]


def run_headless(app, store, engines, library, outdir: str | None, selftest: bool,
                 report_path: str | None = None) -> int:
    from .main_window import MainWindow
    report: dict = {"ok": True, "checks": {}}
    win = MainWindow(store, engines, library)
    win.resize(1440, 900)
    win.show()
    _pump(app, 0.5)

    fam = QFontInfo(T.font(13, 500)).family()
    report["checks"]["font"] = fam
    if "Instrument" not in fam:
        report["ok"] = False
    report["checks"]["missing_engines"] = engines.missing_required()

    if outdir:
        out = Path(outdir)
        out.mkdir(parents=True, exist_ok=True)
        thumbs = [str(p) for p in sorted(Path(os.environ.get("INGEST_DEMO_THUMBS", "")).glob("*.jpg"))] \
            if os.environ.get("INGEST_DEMO_THUMBS") else []
        win.show_page("queue")
        _pump(app)
        win.grab().save(str(out / "01-queue-empty.png"))
        jobs = demo_jobs(store.settings.output, thumbs)
        win.model.add(jobs, 0)
        win.queue_page.stats.setText("2 downloading at 14 MB/s  ·  1 converting  ·  1 waiting  ·  1.4 TB free")
        win.tabs["queue"].set_count("4")
        win._timer.stop()
        _pump(app, 0.8)
        win.grab().save(str(out / "02-queue.png"))
        win.view.hover_row = 4
        win.view.hover_pos = None
        win.view.viewport().update()
        _pump(app)
        win.grab().save(str(out / "03-queue-hover.png"))
        win.view.hover_row = -1
        for key, name in (("library", "04-library"), ("settings", "05-settings"), ("setup", "06-setup")):
            win.show_page(key)
            _pump(app, 0.4)
            win.grab().save(str(out / f"{name}.png"))
        info_path = os.environ.get("INGEST_DEMO_INFO")
        if info_path and Path(info_path).is_file():
            info = json.loads(Path(info_path).read_text(encoding="utf-8"))
            win.inspect_page.open(info.get("webpage_url") or "https://example.com", info=info)
            win.show_page("inspect")
            _pump(app, 1.2)
            if os.environ.get("INGEST_DEBUG_LAYOUT"):
                pg = win.inspect_page
                print("DEBUG", pg.vtable.geometry(), pg.atable.geometry(), pg.vtable.parentWidget().geometry(),
                      file=sys.stderr)
            win.grab().save(str(out / "07-inspect.png"))
        win.inspect_page.open("https://example.com/watch?v=1")
        win.inspect_page.resolver.cancel()
        win.inspect_page.stack.setCurrentWidget(win.inspect_page.loading)
        win.show_page("inspect")
        _pump(app, 0.4)
        win.grab().save(str(out / "08-inspect-loading.png"))
        report["checks"]["screenshots"] = sorted(p.name for p in out.glob("*.png"))

    if selftest:
        st = {k: engines.state(k).__dict__ for k in ("ytdlp", "ffmpeg", "deno", "gallerydl")}
        report["checks"]["engines"] = {k: {"version": v["version"], "source": v["source"]} for k, v in st.items()}
    win._really_quit = True
    win.qm.shutdown(1)
    text = json.dumps(report, indent=2, default=str)
    if report_path:
        Path(report_path).write_text(text, encoding="utf-8")
    if sys.stdout is not None:  # a windowed build has no console
        print(text)
        sys.stdout.flush()
    return 0 if report["ok"] else 1
