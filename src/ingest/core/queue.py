"""The queue: ordering, concurrency, persistence, and the playlist hand-off.

Downloads and conversions have separate slots. A job gives up its download
slot the moment its files are on disk, so the next download starts while the
previous one converts.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import paths
from .conform import Caps
from .encoders import detect
from .engines import Engines
from .jobs import Context, Job, Runner, Stage, Status
from .library import Library
from .settings import OutputOptions, SettingsStore, atomic_write_text, copy_options
from .ytdlp import Network


@dataclass
class Events:
    added: Callable[[list[Job], int], None] = lambda jobs, index: None
    updated: Callable[[Job, dict], None] = lambda job, fields: None
    removed: Callable[[list[str]], None] = lambda ids: None
    review: Callable[[Job, dict, list[dict]], None] = lambda job, info, children: None
    finished: Callable[[Job], None] = lambda job: None


@dataclass
class _Slots:
    downloads: set[str] = field(default_factory=set)
    converts: set[str] = field(default_factory=set)


class QueueManager:
    def __init__(self, store: SettingsStore, engines: Engines, library: Library | None, events: Events,
                 state_path: Path | None = None) -> None:
        self.store = store
        self.engines = engines
        self.library = library
        self.events = events
        self.state_path = state_path or paths.data_dir() / "queue.json"
        self.jobs: list[Job] = []
        self.runners: dict[str, Runner] = {}
        self._lock = threading.RLock()
        self._slots = _Slots()
        self._caps: Caps | None = None
        self._caps_ready = threading.Event()
        self._tools_cache = None
        self._tools_time = 0.0
        self._save_timer: threading.Timer | None = None
        self._schedule_pending = False
        self._closing = False

    # ---------------------------------------------------------------- capabilities
    def start_capability_probe(self) -> None:
        def work():
            try:
                ff = self.engines.tools().ffmpeg
                self._caps = detect(ff, paths.cache_dir() / "encoders.json") if ff else Caps()
            except Exception:
                self._caps = Caps()
            finally:
                self._caps_ready.set()
        threading.Thread(target=work, name="caps", daemon=True).start()

    def caps(self) -> Caps:
        if not self._caps_ready.is_set():
            self._caps_ready.wait(60)
        return self._caps or Caps()

    def refresh_capabilities(self) -> None:
        self._caps_ready.clear()
        try:
            (paths.cache_dir() / "encoders.json").unlink()
        except OSError:
            pass
        self.start_capability_probe()

    def tools(self):
        now = time.monotonic()
        if self._tools_cache is None or now - self._tools_time > 5:
            self._tools_cache = self.engines.tools()
            self._tools_time = now
        return self._tools_cache

    def invalidate_tools(self) -> None:
        self._tools_cache = None

    # ---------------------------------------------------------------- context for runners
    def _context(self) -> Context:
        return Context(
            tools=self.tools,
            caps=self.caps,
            library=self.library,
            settings=lambda: self.store.settings,
            update=self._on_update,
            expand=self._on_expand,
            release_download=self._release_download,
            acquire_convert=self._acquire_convert,
            release_convert=self._release_convert,
        )

    def _on_update(self, job: Job, fields: dict) -> None:
        self.events.updated(job, fields)
        st = fields.get("status")
        if st in (Status.DONE.value, Status.FAILED.value, Status.CANCELED.value, Status.PAUSED.value):
            if st in (Status.DONE.value, Status.FAILED.value):
                self.events.finished(job)
            self.save_soon()
            self._schedule_later()

    def _release_download(self, job: Job) -> None:
        with self._lock:
            self._slots.downloads.discard(job.id)
        self._schedule_later()

    def _acquire_convert(self, job: Job, runner: Runner) -> bool:
        with self._lock:
            if job.id in self._slots.converts:
                return True
            free = max(1, self.store.settings.max_conform) - len(self._slots.converts)
            if free <= 0:
                return False
            # Queue order decides who converts next, not whoever polls first.
            waiting = [j.id for j in self.jobs if j.stage == Stage.CONVERT_WAIT.value and j.active
                       and j.id not in self._slots.converts]
            if job.id in waiting and waiting.index(job.id) >= free:
                return False
            self._slots.converts.add(job.id)
            return True

    def _release_convert(self, job: Job) -> None:
        with self._lock:
            self._slots.converts.discard(job.id)
            for r in self.runners.values():
                if r.job.stage == Stage.CONVERT_WAIT.value:
                    r._wake.set()
        self._schedule_later()

    def _on_expand(self, parent: Job, info: dict, children: list[dict]) -> None:
        s = self.store.settings
        if not parent.confirmed and len(children) > max(1, s.playlist_confirm):
            with self._lock:
                self._remove_ids([parent.id])
            self.events.review(parent, info, children)
            return
        self.replace_with_children(parent, children)

    def replace_with_children(self, parent: Job, children: list[dict]) -> list[Job]:
        new_jobs = []
        for c in children:
            j = Job(url=c["url"], opts=copy_options(parent.opts), net=Network(**parent.net.__dict__))
            j.title = c.get("title") or ""
            j.duration = float(c.get("duration") or 0)
            j.thumbnail = c.get("thumbnail") or ""
            j.uploader = c.get("uploader") or ""
            j.playlist = dict(c.get("playlist") or {})
            j.confirmed = parent.confirmed
            new_jobs.append(j)
        with self._lock:
            try:
                idx = next(i for i, j in enumerate(self.jobs) if j.id == parent.id)
            except StopIteration:
                idx = len(self.jobs)
            self.jobs[idx:idx + 1] = new_jobs
            self.runners.pop(parent.id, None)
            self._slots.downloads.discard(parent.id)
        self.events.removed([parent.id])
        self.events.added(new_jobs, idx)
        self.save_soon()
        self._schedule_later()
        return new_jobs

    # ---------------------------------------------------------------- public API (UI thread)
    def add_urls(self, urls: list[str], opts: OutputOptions, *, playlist: bool | None = None,
                 confirmed: bool = False, front: bool = False, meta: list[dict] | None = None) -> list[Job]:
        s = self.store.settings
        net = Network.from_settings(s, str(paths.cache_dir() / "yt-dlp"))
        jobs = []
        for i, url in enumerate(urls):
            j = Job(url=url, opts=copy_options(opts), net=Network(**net.__dict__))
            j.want_playlist = _wants_playlist(url, s.playlist_links) if playlist is None else playlist
            j.confirmed = confirmed
            if meta and i < len(meta):
                m = meta[i]
                j.title = m.get("title") or ""
                j.duration = float(m.get("duration") or 0)
                j.thumbnail = m.get("thumbnail") or ""
                j.uploader = m.get("uploader") or ""
                j.playlist = dict(m.get("playlist") or {})
            jobs.append(j)
        with self._lock:
            idx = 0 if front else len(self.jobs)
            self.jobs[idx:idx] = jobs
        self.events.added(jobs, idx)
        self.save_soon()
        self.schedule()
        return jobs

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return next((j for j in self.jobs if j.id == job_id), None)

    def pause(self, job_id: str) -> None:
        with self._lock:
            job = self.get(job_id)
            if job is None:
                return
            r = self.runners.get(job_id)
            if job.status == Status.QUEUED.value:
                job.status = Status.PAUSED.value
                job.stage_text = "Paused"
                self.events.updated(job, {"status": job.status, "stage_text": job.stage_text})
            elif r is not None and r.is_alive():
                r.pause()
        self.save_soon()

    def resume(self, job_id: str) -> None:
        with self._lock:
            job = self.get(job_id)
            if job is None:
                return
            r = self.runners.get(job_id)
            if r is not None and r.is_alive() and r.suspended:
                r.resume()
                return
            if job.status in (Status.PAUSED.value, Status.FAILED.value, Status.CANCELED.value):
                if job.status != Status.PAUSED.value:
                    job.error = None
                    job.info_path = ""  # failed or canceled: always extract fresh
                job.status = Status.QUEUED.value
                job.stage_text = "Waiting"
                self.events.updated(job, {"status": job.status, "stage_text": job.stage_text, "error": None})
        self.save_soon()
        self.schedule()

    retry = resume

    def cancel(self, job_id: str) -> None:
        with self._lock:
            job = self.get(job_id)
            if job is None:
                return
            r = self.runners.get(job_id)
            if r is not None and r.is_alive():
                r.cancel()
                return
            if job.status in (Status.QUEUED.value, Status.PAUSED.value, Status.FAILED.value):
                Runner(job, self._context())._discard_work()
                job.status = Status.CANCELED.value
                job.stage_text = "Canceled"
                self.events.updated(job, {"status": job.status, "stage_text": job.stage_text})
        self.save_soon()

    def remove(self, job_ids: list[str]) -> None:
        with self._lock:
            for jid in job_ids:
                job = self.get(jid)
                if job is None:
                    continue
                r = self.runners.get(jid)
                if r is not None and r.is_alive():
                    r.cancel()
                elif not job.done:
                    Runner(job, self._context())._discard_work()
            self._remove_ids(job_ids)
        self.save_soon()
        self.schedule()

    def _remove_ids(self, ids: list[str]) -> None:
        idset = set(ids)
        self.jobs = [j for j in self.jobs if j.id not in idset]
        for jid in ids:
            self.runners.pop(jid, None)
            self._slots.downloads.discard(jid)
        self.events.removed(list(ids))

    def finish_live(self, job_id: str) -> None:
        r = self.runners.get(job_id)
        if r is not None:
            r.finish_live()

    def move(self, job_id: str, new_index: int) -> None:
        with self._lock:
            job = self.get(job_id)
            if job is None:
                return
            self.jobs.remove(job)
            new_index = max(0, min(len(self.jobs), new_index))
            self.jobs.insert(new_index, job)
        self.save_soon()

    def pause_all(self) -> None:
        for j in list(self.jobs):
            if j.status in (Status.QUEUED.value, Status.ACTIVE.value):
                self.pause(j.id)

    def resume_all(self) -> None:
        for j in list(self.jobs):
            if j.status == Status.PAUSED.value:
                self.resume(j.id)

    def clear_finished(self) -> list[str]:
        with self._lock:
            ids = [j.id for j in self.jobs if j.status in (Status.DONE.value, Status.CANCELED.value)]
            self._remove_ids(ids)
        self.save_soon()
        return ids

    def counts(self) -> dict:
        with self._lock:
            c = {"total": len(self.jobs), "active": 0, "queued": 0, "done": 0, "failed": 0, "paused": 0}
            for j in self.jobs:
                if j.status in c:
                    c[j.status] += 1
            return c

    # ---------------------------------------------------------------- scheduling
    def _schedule_later(self) -> None:
        with self._lock:
            if self._schedule_pending:
                return
            self._schedule_pending = True

        def fire():
            with self._lock:
                self._schedule_pending = False
            self.schedule()
        t = threading.Timer(0.05, fire)
        t.daemon = True
        t.start()

    def schedule(self) -> None:
        if self._closing:
            return
        with self._lock:
            limit = max(1, self.store.settings.max_concurrent)
            for job in self.jobs:
                if len(self._slots.downloads) >= limit:
                    break
                if job.status != Status.QUEUED.value:
                    continue
                r = self.runners.get(job.id)
                if r is not None and r.is_alive():
                    continue
                self._slots.downloads.add(job.id)
                runner = Runner(job, self._context())
                self.runners[job.id] = runner
                job.status = Status.ACTIVE.value
                runner.start()

    def active_progress(self) -> tuple[float | None, bool]:
        """Aggregate progress for the taskbar: (fraction or None, any paused)."""
        with self._lock:
            act = [j for j in self.jobs if j.status == Status.ACTIVE.value]
            paused = any(j.status == Status.PAUSED.value for j in self.jobs)
            if not act:
                return None, paused
            total = 0.0
            for j in act:
                total += 0.7 * j.dl_progress + 0.3 * j.cv_progress
            return total / len(act), paused

    # ---------------------------------------------------------------- persistence
    def save_soon(self) -> None:
        with self._lock:
            if self._save_timer is not None:
                self._save_timer.cancel()
            self._save_timer = threading.Timer(0.8, self.save)
            self._save_timer.daemon = True
            self._save_timer.start()

    def save(self) -> None:
        with self._lock:
            keep = [j for j in self.jobs if j.status != Status.CANCELED.value][-500:]
            data = {"version": 1, "jobs": [j.to_dict() for j in keep]}
        try:
            atomic_write_text(self.state_path, json.dumps(data, ensure_ascii=False))
        except OSError:
            pass

    def load(self) -> None:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        resume = self.store.settings.resume_on_launch
        jobs = []
        for d in data.get("jobs", []):
            try:
                j = Job.from_dict(d)
            except Exception:
                continue
            if j.status == Status.ACTIVE.value:
                j.status = Status.QUEUED.value if resume else Status.PAUSED.value
                j.stage_text = "Waiting" if resume else "Paused"
            elif j.status == Status.QUEUED.value and not resume:
                j.status = Status.PAUSED.value
                j.stage_text = "Paused"
            j.speed = j.eta = j.cv_speed = None
            jobs.append(j)
        with self._lock:
            self.jobs = jobs
        if jobs:
            self.events.added(jobs, 0)

    def shutdown(self, timeout: float = 4.0) -> None:
        """Stop everything quickly. Partial downloads stay on disk and resume next launch."""
        self._closing = True
        with self._lock:
            runners = [r for r in self.runners.values() if r.is_alive()]
            interrupted = {r.job.id for r in runners if not r.suspended}
            for r in runners:
                if r.suspended:
                    # A suspended conversion is killed; it restarts from the downloaded files.
                    r.suspended = False
                    r._stop_reason = "pause"
                    if r._proc is not None:
                        r._proc.kill()
                else:
                    r.pause()
        deadline = time.monotonic() + timeout
        for r in runners:
            r.join(max(0.0, deadline - time.monotonic()))
        with self._lock:
            for j in self.jobs:
                if j.id in interrupted and j.status in (Status.PAUSED.value, Status.ACTIVE.value):
                    j.status = Status.QUEUED.value
                    j.stage_text = "Waiting"
        if self._save_timer is not None:
            self._save_timer.cancel()
        self.save()


def _wants_playlist(url: str, mode: str) -> bool:
    """Links like watch?v=X&list=Y carry both a video and a playlist."""
    low = url.lower()
    has_list = "list=" in low or "/playlist" in low or "/sets/" in low or "/album" in low
    has_video = "v=" in low or "/watch" in low or "/video/" in low or "youtu.be/" in low or "/shorts/" in low
    if has_list and has_video:
        return mode == "playlist"
    return True
