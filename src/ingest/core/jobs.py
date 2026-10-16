"""One download, from link to verified edit-ready file.

A Runner thread walks the job through its stages:

  resolve   yt-dlp reads the page and picks formats (-J), metadata lands in the UI
  download  each chosen format downloads to a private work folder
  convert   ffprobe measures, the planner decides, ffmpeg copies or encodes
  verify    the result is measured against Premiere / After Effects requirements
  finalize  sidecars and originals move next to the file, the work folder goes

Pausing during download stops the processes and resumes from the partial
files later. Pausing during conversion suspends ffmpeg in place.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time
import traceback
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable

from . import conform, naming, paths, sponsorblock, verify, winapi
from .errors import Failure, classify, ffmpeg_failure
from .library import Item, Library
from .probe import MediaInfo, ProbeError, measure_timing, probe
from .procs import Proc
from .settings import OutputOptions, from_dict, to_dict
from .ytdlp import (Attempt, Network, Tools, download_args, gallery_args, load_info, parse_file,
                    parse_progress, resolve_args, selection_from_info)

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif", ".heic", ".heif", ".bmp", ".tif", ".tiff",
              ".jxl", ".jfif"}
ADOBE_IMAGE_OK = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tif", ".tiff", ".psd"}
MEDIA_EXTS = {".mp4", ".m4a", ".m4v", ".mov", ".webm", ".mkv", ".mp3", ".ogg", ".opus", ".flac", ".wav", ".aac",
              ".ts", ".mts", ".flv", ".avi", ".3gp", ".wmv", ".mpg", ".mpeg", ".oga", ".weba", ".f4v"}
SIDE_EXTS = {".srt", ".vtt", ".ass", ".lrc", ".json", ".description", ".jpg", ".png", ".webp"}


class Status(str, Enum):
    QUEUED = "queued"
    ACTIVE = "active"
    PAUSED = "paused"
    DONE = "done"
    FAILED = "failed"
    CANCELED = "canceled"


class Stage(str, Enum):
    NONE = ""
    RESOLVE = "resolve"
    DOWNLOAD = "download"
    CONVERT_WAIT = "convert_wait"
    CONVERT = "convert"
    VERIFY = "verify"
    FINALIZE = "finalize"


@dataclass
class Job:
    url: str
    opts: OutputOptions
    net: Network
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    created: float = field(default_factory=time.time)
    status: str = Status.QUEUED.value
    stage: str = Stage.NONE.value
    stage_text: str = ""
    # metadata
    title: str = ""
    uploader: str = ""
    site: str = ""
    duration: float = 0.0
    thumbnail: str = ""
    webpage_url: str = ""
    extractor: str = ""
    video_id: str = ""
    upload_date: str = ""
    is_live: bool = False
    playlist: dict = field(default_factory=dict)
    want_playlist: bool = False
    confirmed: bool = False
    selection: dict = field(default_factory=dict)
    # progress (0..1)
    dl_progress: float = 0.0
    cv_progress: float = 0.0
    speed: float | None = None
    eta: float | None = None
    cv_speed: float | None = None
    downloaded: float = 0.0
    total: float | None = None
    # outcome
    plan_summary: str = ""
    plan_reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    outputs: list[str] = field(default_factory=list)
    sidecars: list[str] = field(default_factory=list)
    report: dict | None = None
    error: dict | None = None
    out_props: dict = field(default_factory=dict)
    finished: float | None = None
    skipped: bool = False
    # bookkeeping
    work_dir: str = ""
    info_path: str = ""
    log_path: str = ""
    attempts: dict = field(default_factory=dict)
    gallery: bool = False
    files: list[str] = field(default_factory=list)

    @property
    def done(self) -> bool:
        return self.status == Status.DONE.value

    @property
    def active(self) -> bool:
        return self.status == Status.ACTIVE.value

    def to_dict(self) -> dict:
        d = asdict(self)
        d["opts"] = to_dict(self.opts)
        d["net"] = asdict(self.net)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Job":
        d = dict(d)
        opts = from_dict(OutputOptions, d.pop("opts", {}) or {})
        net = from_dict(Network, d.pop("net", {}) or {})
        known = {f for f in cls.__dataclass_fields__}
        job = cls(url=d.pop("url", ""), opts=opts, net=net, **{k: v for k, v in d.items() if k in known})
        return job

    def snapshot(self) -> dict:
        return {k: getattr(self, k) for k in self.__dataclass_fields__ if k not in ("opts", "net")}


class Paused(Exception):
    pass


class Canceled(Exception):
    pass


class Expanded(Exception):
    pass


class JobFailed(Exception):
    def __init__(self, failure: Failure) -> None:
        super().__init__(failure.title)
        self.failure = failure


class _ToolFailed(Exception):
    def __init__(self, failure: Failure) -> None:
        super().__init__(failure.title)
        self.failure = failure


@dataclass
class Context:
    """What a runner needs from the outside world. Supplied by the QueueManager."""
    tools: Callable[[], Tools]
    caps: Callable[[], conform.Caps]
    library: Library | None
    settings: Callable[[], object]
    update: Callable[[Job, dict], None]
    expand: Callable[[Job, dict, list[dict]], None]
    release_download: Callable[[Job], None]
    acquire_convert: Callable[[Job, "Runner"], bool]
    release_convert: Callable[[Job], None]


_REDACT = re.compile(r"(://)[^/@\s]+@")


class Runner(threading.Thread):
    def __init__(self, job: Job, ctx: Context) -> None:
        super().__init__(name=f"job-{job.id}", daemon=True)
        self.job = job
        self.ctx = ctx
        self._stop_reason: str | None = None  # pause | cancel | finish
        self._proc: Proc | None = None
        self._proc_lock = threading.Lock()
        self._wake = threading.Event()
        self._last_emit = 0.0
        self._log_fh = None
        self.attempt = Attempt(cookies=bool(job.attempts.get("cookies")),
                               impersonate=bool(job.attempts.get("impersonate")))
        self.suspended = False
        self.holds_download = True
        self.holds_convert = False

    # ------------------------------------------------------------ control (any thread)
    def pause(self) -> None:
        with self._proc_lock:
            if self.job.stage in (Stage.CONVERT.value,) and self._proc is not None and not self.suspended:
                if self._proc.suspend():
                    self.suspended = True
                    self._set(status=Status.PAUSED.value, stage_text="Paused")
                    return
            if self.job.stage == Stage.CONVERT_WAIT.value:
                self._stop_reason = "pause"
                self._wake.set()
                return
            self._stop_reason = "pause"
            if self._proc is not None:
                self._proc.kill()
            self._wake.set()

    def resume(self) -> bool:
        """Resume a suspended conversion. Returns False if the job must be re-queued instead."""
        with self._proc_lock:
            if self.suspended and self._proc is not None:
                self._proc.resume()
                self.suspended = False
                self._set(status=Status.ACTIVE.value, stage_text=self._resume_text)
                return True
        return False

    def cancel(self) -> None:
        with self._proc_lock:
            self._stop_reason = "cancel"
            if self._proc is not None:
                self._proc.kill()
            self.suspended = False
            self._wake.set()

    def finish_live(self) -> None:
        with self._proc_lock:
            if self.job.stage == Stage.DOWNLOAD.value and self._proc is not None:
                self._stop_reason = "finish"
                self._proc.kill()

    def _check_stop(self) -> None:
        if self._stop_reason == "cancel":
            raise Canceled()
        if self._stop_reason == "pause":
            raise Paused()

    def _sleep(self, seconds: float) -> None:
        self._wake.wait(seconds)
        self._check_stop()

    # ------------------------------------------------------------ emitting
    def _set(self, force: bool = True, **fields) -> None:
        for k, v in fields.items():
            setattr(self.job, k, v)
        now = time.monotonic()
        if force or now - self._last_emit >= 0.12:
            self._last_emit = now
            try:
                self.ctx.update(self.job, fields)
            except Exception:
                pass

    def log(self, text: str) -> None:
        if self._log_fh is None:
            return
        try:
            self._log_fh.write(_REDACT.sub(r"\1***@", text.rstrip("\n")) + "\n")
        except (OSError, ValueError):
            pass

    def _open_log(self) -> None:
        d = paths.logs_dir() / "jobs"
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"{self.job.id}.log"
        self.job.log_path = str(p)
        try:
            self._log_fh = open(p, "a", encoding="utf-8", buffering=1)
            self.log(f"\n==== {time.strftime('%Y-%m-%d %H:%M:%S')}  {self.job.url}")
        except OSError:
            self._log_fh = None

    def _spawn(self, args: list[str], merge: bool = True, cwd: str | None = None) -> Proc:
        self.log("$ " + " ".join(_quote(a) for a in args))
        with self._proc_lock:
            self._check_stop()
            proc = Proc(args, cwd=cwd, merge_stderr=merge, on_stderr=None if merge else self.log)
            self._proc = proc
        return proc

    def _clear_proc(self) -> None:
        with self._proc_lock:
            self._proc = None

    # ------------------------------------------------------------ main
    def run(self) -> None:
        job = self.job
        self._open_log()
        try:
            self._set(status=Status.ACTIVE.value, error=None)
            self._prepare_work_dir()
            info: dict = {}
            if not job.gallery:
                info = self._load_cached_info() or self._resolve()
            if job.files and all(Path(f).is_file() for f in job.files):
                files = list(job.files)
            elif job.gallery:
                files = self._gallery_download()
            else:
                files = self._download(info)
            job.files = files
            self._convert_and_finish(info, files)
            self._set(status=Status.DONE.value, stage=Stage.NONE.value, finished=time.time(),
                      stage_text="Done", dl_progress=1.0, cv_progress=1.0, speed=None, eta=None)
        except Expanded:
            pass
        except Paused:
            self._set(status=Status.PAUSED.value, stage_text="Paused", speed=None, eta=None)
        except Canceled:
            self._discard_work()
            self._set(status=Status.CANCELED.value, stage_text="Canceled", speed=None, eta=None)
        except JobFailed as e:
            f = e.failure
            self.log(f"!! {f.kind}: {f.title} | {f.raw}")
            self._set(status=Status.FAILED.value, stage_text=f.title, speed=None, eta=None,
                      error={"kind": f.kind, "title": f.title, "hint": f.hint, "raw": f.raw})
        except Exception as e:  # never let a thread die silently
            self.log(traceback.format_exc())
            self._set(status=Status.FAILED.value, stage_text="Unexpected error", speed=None, eta=None,
                      error={"kind": "internal", "title": "Unexpected error",
                             "hint": "Open the log and report it.", "raw": f"{type(e).__name__}: {e}"})
        finally:
            job.attempts = {"cookies": self.attempt.cookies, "impersonate": self.attempt.impersonate}
            if self.holds_download:
                self.holds_download = False
                self.ctx.release_download(job)
            if self.holds_convert:
                self.holds_convert = False
                self.ctx.release_convert(job)
            if self._log_fh:
                try:
                    self._log_fh.close()
                except OSError:
                    pass

    # ------------------------------------------------------------ work folder
    def _prepare_work_dir(self) -> None:
        job = self.job
        if job.work_dir and Path(job.work_dir).is_dir():
            return
        s = self.ctx.settings()
        out = job.opts.resolved_output_dir()
        root: Path | None = None
        if getattr(s, "work_location", "output") == "output":
            try:
                out.mkdir(parents=True, exist_ok=True)
                root = out / ".ingest-work"
                root.mkdir(exist_ok=True)
                winapi.set_hidden(root)
                probe_file = root / ".write-test"
                probe_file.write_bytes(b"")
                probe_file.unlink()
            except OSError:
                root = None
        if root is None:
            root = paths.work_root()
            root.mkdir(parents=True, exist_ok=True)
        wd = root / job.id
        wd.mkdir(parents=True, exist_ok=True)
        job.work_dir = str(wd)

    def _discard_work(self) -> None:
        wd = self.job.work_dir
        if wd and Path(wd).is_dir():
            shutil.rmtree(wd, ignore_errors=True)
            _remove_if_empty(Path(wd).parent)
        self.job.work_dir = ""
        self.job.info_path = ""
        self.job.files = []

    def _load_cached_info(self) -> dict | None:
        p = self.job.info_path
        if not p or not Path(p).is_file():
            return None
        try:
            info = json.loads(Path(p).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        # Signed media URLs expire. Older than two hours: extract again.
        if time.time() - Path(p).stat().st_mtime > 7200:
            return None
        return info

    # ------------------------------------------------------------ remediation loop
    def _with_remediation(self, step: Callable[[], object], allow_gallery: bool = False):
        net = self.job.net
        refreshed = False
        while True:
            self._check_stop()
            try:
                return step()
            except _ToolFailed as tf:
                f = tf.failure
                self._check_stop()
                if f.retry_with == "cookies" and net.has_cookies and not self.attempt.cookies:
                    self.attempt.cookies = True
                    self.log("-- retrying with browser cookies")
                    self._set(stage_text="Retrying signed in")
                    continue
                if f.retry_with in ("impersonate", "cookies") and net.impersonate != "off" \
                        and not self.attempt.impersonate and f.kind in ("forbidden", "impersonate", "rate_limit"):
                    self.attempt.impersonate = True
                    self.log("-- retrying with browser impersonation")
                    self._set(stage_text="Retrying as a browser")
                    continue
                if f.retry_with == "refresh" and not refreshed:
                    refreshed = True
                    self._set(stage_text="Network hiccup, retrying")
                    self._sleep(3)
                    continue
                if allow_gallery and f.kind in ("unsupported", "no_formats") and self._gallery_available():
                    self.log("-- yt-dlp found nothing, trying gallery-dl")
                    self.job.gallery = True
                    return None
                raise JobFailed(f)

    def _gallery_available(self) -> bool:
        s = self.ctx.settings()
        return bool(getattr(s, "gallery_fallback", True)) and bool(self.ctx.tools().gallerydl)

    # ------------------------------------------------------------ resolve
    def _resolve(self) -> dict:
        job = self.job
        tools = self.ctx.tools()
        if not tools.ytdlp:
            raise JobFailed(Failure("engine", "yt-dlp isn't installed", "Install it in Settings > Engines."))
        self._set(stage=Stage.RESOLVE.value, stage_text="Reading page", dl_progress=0.0)

        def step():
            args = resolve_args(tools, job.net, self.attempt, job.url, job.opts, job.want_playlist)
            proc = self._spawn(args, merge=False)
            raw = proc.read_all()
            code = proc.wait()
            self._clear_proc()
            self._check_stop()
            text = raw.decode("utf-8", "replace")
            if code == 0:
                try:
                    return load_info(text)
                except ValueError:
                    pass
            raise _ToolFailed(classify(proc.stderr_lines, code))

        info = self._with_remediation(step, allow_gallery=True)
        if info is None:  # switched to gallery-dl
            self._set(title=job.title or naming_title_from_url(job.url), site=_site_from_url(job.url))
            return {}
        return self._accept_info(info)

    def _accept_info(self, info: dict) -> dict:
        job = self.job
        kind = info.get("_type", "video")
        if kind in ("playlist", "multi_video") or (kind == "url" and info.get("entries")):
            entries = [e for e in (info.get("entries") or []) if e]
            if not entries:
                raise JobFailed(Failure("empty", "The playlist is empty", "Nothing to download at this link."))
            children = []
            for i, e in enumerate(entries, 1):
                url = e.get("url") or e.get("webpage_url") or e.get("original_url")
                if not url:
                    continue
                thumbs = e.get("thumbnails") or []
                children.append({
                    "url": url, "title": e.get("title") or "", "duration": float(e.get("duration") or 0),
                    "thumbnail": e.get("thumbnail") or (thumbs[-1].get("url") if thumbs else ""),
                    "uploader": e.get("uploader") or e.get("channel") or info.get("uploader") or "",
                    "playlist": {"title": info.get("title") or "", "index": e.get("playlist_index") or i,
                                 "count": len(entries), "id": info.get("id") or ""},
                })
            self.ctx.expand(job, info, children)
            raise Expanded()

        if info.get("live_status") == "is_upcoming":
            raise JobFailed(Failure("upcoming", "Not live yet", "Queue it again when the stream starts."))
        sel = selection_from_info(info)
        thumb = info.get("thumbnail") or ""
        if not thumb and info.get("thumbnails"):
            thumb = info["thumbnails"][-1].get("url", "")
        self._set(
            title=info.get("title") or job.title or info.get("id") or "",
            uploader=info.get("uploader") or info.get("channel") or job.uploader,
            site=_site_name(info),
            duration=float(info.get("duration") or job.duration or 0),
            thumbnail=thumb or job.thumbnail,
            webpage_url=info.get("webpage_url") or job.url,
            extractor=info.get("extractor_key") or info.get("extractor") or "",
            video_id=str(info.get("id") or ""),
            upload_date=info.get("upload_date") or "",
            is_live=bool(info.get("is_live")),
            selection={"format_ids": sel.format_ids, "width": sel.width, "height": sel.height, "fps": sel.fps,
                       "vcodec": sel.vcodec, "acodec": sel.acodec, "hdr": sel.dynamic_range, "size": sel.size},
            total=sel.size or None,
        )
        s = self.ctx.settings()
        if getattr(s, "skip_existing", True) and self.ctx.library is not None and not job.confirmed:
            existing = self.ctx.library.find(job.extractor, job.video_id, job.opts.profile)
            if existing is not None:
                job.skipped = True
                self._set(outputs=[existing.path], report=existing.report, plan_summary=existing.summary,
                          status=Status.DONE.value, stage=Stage.NONE.value, stage_text="Already in your library",
                          finished=time.time(), dl_progress=1.0, cv_progress=1.0)
                raise Expanded()
        p = Path(job.work_dir) / "info.json"
        try:
            p.write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
            job.info_path = str(p)
        except OSError:
            job.info_path = ""
        return info

    # ------------------------------------------------------------ download
    def _download(self, info: dict) -> list[str]:
        job = self.job
        tools = self.ctx.tools()
        if not info and job.info_path:
            info = json.loads(Path(job.info_path).read_text(encoding="utf-8"))
        ids = job.selection.get("format_ids") or job.opts.format_ids or "bv*+ba/b"
        fmts = (info.get("requested_formats") or ([info] if info.get("format_id") else [])) if info else []
        weights = _weights(fmts, float(info.get("duration") or 0) if info else 0.0)
        audio_ids = {f.get("format_id") for f in fmts if f.get("vcodec") in (None, "none")
                     and f.get("acodec") not in (None, "none")}
        use_url = job.is_live or not job.info_path
        state = {"files": {}, "pct": {}, "use_url": use_url, "audio": audio_ids}
        self._set(stage=Stage.DOWNLOAD.value, stage_text="Starting download", dl_progress=0.0)

        def step():
            args = download_args(tools, job.net, self.attempt, None if state["use_url"] else job.info_path,
                                 job.webpage_url or job.url, ids, job.work_dir, job.opts, live=job.is_live)
            proc = self._spawn(args)
            tail: list[str] = []
            for line in proc.lines():
                pr = parse_progress(line)
                if pr is not None:
                    self._on_progress(pr, weights, state)
                    continue
                pf = parse_file(line)
                if pf is not None:
                    state["files"][pf[0]] = pf[1]
                    self.log(line)
                    continue
                self.log(line)
                tail.append(line)
                if len(tail) > 300:
                    del tail[:100]
                if "[Merger]" in line or "[Fixup" in line:
                    self._set(stage_text="Fixing up container", force=False)
            code = proc.wait()
            self._clear_proc()
            if self._stop_reason == "finish":
                self._stop_reason = None
                return
            self._check_stop()
            if code != 0:
                f = classify(tail, code)
                if not state["use_url"] and f.kind in ("forbidden", "network", "unavailable", "unknown"):
                    # Media URLs from the cached page data may have expired: extract fresh.
                    state["use_url"] = True
                    self.log("-- retrying with a fresh extraction")
                    raise _ToolFailed(Failure(f.kind, f.title, f.hint, f.raw, "refresh"))
                raise _ToolFailed(f)

        self._with_remediation(step)
        files = [p for p in state["files"].values() if Path(p).is_file()]
        if not files:
            files = _scan_media(Path(job.work_dir))
        if not files:
            raise JobFailed(Failure("nothing", "Nothing was downloaded",
                                    "The site returned no media. Open the log for details."))
        self._set(dl_progress=1.0, speed=None, eta=None)
        return files

    def _on_progress(self, pr, weights: dict[str, float], state: dict) -> None:
        pct = state["pct"]
        pct[pr.format_id] = (pr.percent or 0.0) / 100.0
        total_w = sum(weights.values()) or 1.0
        if weights and all(k in weights for k in pct):
            overall = sum(weights[k] * pct.get(k, 0.0) for k in weights) / total_w
        else:
            done = sum(pct.values())
            overall = done / max(len(weights) or 1, len(pct))
        fmt_is_audio = pr.format_id in state["audio"]
        n = max(len(weights), 1)
        label = ("Downloading audio" if fmt_is_audio else "Downloading video") if n > 1 else "Downloading"
        if self.job.is_live:
            label = "Recording live"
        eta = pr.eta
        if pr.speed and weights and all(v > 1 for v in weights.values()):
            remaining = sum(w * (1 - pct.get(k, 0.0)) for k, w in weights.items())
            eta = remaining / pr.speed
        self._set(force=pr.status == "finished", stage_text=label, dl_progress=min(1.0, max(0.0, overall)),
                  speed=pr.speed, eta=eta, downloaded=pr.downloaded, total=pr.total)

    def _gallery_download(self) -> list[str]:
        job = self.job
        tools = self.ctx.tools()
        out = Path(job.work_dir) / "gallery"
        out.mkdir(parents=True, exist_ok=True)
        self._set(stage=Stage.DOWNLOAD.value, stage_text="Downloading with gallery-dl", dl_progress=0.0)
        count = {"n": 0}

        def step():
            proc = self._spawn(gallery_args(tools, job.net, self.attempt, job.url, str(out)))
            tail = []
            for line in proc.lines():
                self.log(line)
                tail.append(line)
                if line.startswith(str(out)) or line.startswith("# "):
                    count["n"] += 1
                    self._set(force=False, stage_text=f"Downloaded {count['n']} file{'s' if count['n'] != 1 else ''}")
            code = proc.wait()
            self._clear_proc()
            self._check_stop()
            files = [str(p) for p in sorted(out.rglob("*")) if p.is_file() and not p.name.endswith(".part")]
            if code != 0 and not files:
                raise _ToolFailed(classify(tail, code))
            return files

        files = self._with_remediation(step) or []
        if not files:
            raise JobFailed(Failure("unsupported", "No media found at this link",
                                    "Neither yt-dlp nor gallery-dl found anything to download here."))
        self._set(dl_progress=1.0)
        return files

    # ------------------------------------------------------------ convert
    def _wait_convert_slot(self) -> None:
        if self.holds_download:
            self.holds_download = False
            self.ctx.release_download(self.job)
        self._set(stage=Stage.CONVERT_WAIT.value, stage_text="Waiting to convert", speed=None, eta=None)
        while not self.ctx.acquire_convert(self.job, self):
            self._wake.wait(0.5)
            self._wake.clear()
            self._check_stop()
        self.holds_convert = True
        self._check_stop()

    def _convert_and_finish(self, info: dict, files: list[str]) -> None:
        job = self.job
        tools = self.ctx.tools()
        if not tools.ffmpeg or not tools.ffprobe:
            raise JobFailed(Failure("ffmpeg", "ffmpeg isn't installed", "Install it in Settings > Engines."))
        self._wait_convert_slot()
        self._set(stage=Stage.CONVERT.value, stage_text="Measuring", cv_progress=0.0)

        images = [f for f in files if Path(f).suffix.lower() in IMAGE_EXTS]
        media = [f for f in files if f not in images]
        if job.gallery or (images and not media):
            outputs = self._finish_gallery(media, images)
        else:
            outputs = self._finish_media(info, media)
        self._set(outputs=outputs)

    def _probe(self, path: str) -> MediaInfo:
        try:
            return probe(self.ctx.tools().ffprobe, path)
        except ProbeError as e:
            raise JobFailed(Failure("corrupt", "The downloaded file can't be read", "Retry the download.",
                                    str(e), "refresh"))

    def _finish_media(self, info: dict, files: list[str]) -> list[str]:
        job = self.job
        tools = self.ctx.tools()
        opts = job.opts
        inputs = [self._probe(f) for f in files]
        inputs.sort(key=lambda mi: 0 if mi.video else 1)
        video = next((mi for mi in inputs if mi.video), None)
        timing = None
        if video and video.video and video.video.codec in ("h264", "hevc") and opts.profile in ("h264", "hevc"):
            self._set(stage_text="Checking frame timing")
            timing = measure_timing(tools.ffprobe, video.path)
        segments = []
        if opts.sponsorblock and (job.extractor or "").lower().startswith("youtube") and job.video_id:
            try:
                segments = sponsorblock.segments(job.video_id, opts.sponsor_categories, job.duration, job.net.proxy)
            except Exception as e:
                job.warnings.append(f"SponsorBlock unavailable: {e}")
        trim = None
        if opts.trim_start is not None or opts.trim_end is not None:
            start = opts.trim_start or 0.0
            end = opts.trim_end if opts.trim_end is not None else (job.duration or 0.0)
            if end > start:
                trim = (start, end)
                segments = [(max(0.0, a - start), b - start) for a, b in segments if b > start and a < end]
        try:
            p = conform.plan(inputs, opts, self.ctx.caps(), timing, title=job.title,
                             source_url=job.webpage_url or job.url, trim=trim, sponsor_segments=segments or None)
        except conform.PlanError as e:
            raise JobFailed(Failure("plan", str(e), "Try the Original profile, or open the log."))
        self._set(plan_summary=p.summary, plan_reasons=p.reasons, warnings=job.warnings + p.warnings,
                  out_props=p.out)
        self.log(f"-- plan: {p.summary} | reasons: {', '.join(p.reasons) or 'none'}")

        final = self._final_path(info, p)
        if final is None:
            self._discard_work()
            return [str(self._existing_target)]
        final.parent.mkdir(parents=True, exist_ok=True)
        if p.passthrough:
            self._set(stage_text="Moving file")
            _move(files[0], final)
        else:
            self._run_ffmpeg(p, final)
        if opts.profile not in ("original",):
            self._verify(final, p)
        outputs = [str(final)]
        if opts.proxy in ("prores", "h264") and p.kind == "video":
            proxy = self._make_proxy(final)
            if proxy:
                outputs.append(proxy)
        self._finalize(final, files, p)
        return outputs

    def _final_path(self, info: dict, p: conform.Plan) -> Path | None:
        job = self.job
        opts = job.opts
        ctx = naming.context_from_info(info or {}, {
            "title": job.title, "uploader": job.uploader, "site": job.site,
            "playlist": job.playlist.get("title"), "index": job.playlist.get("index"),
            "res": f"{min(p.out.get('width', 0), p.out.get('height', 0))}p" if p.out.get("height") else "",
            "fps": _fps_text(p.out.get("rate")),
            "profile": conform.PROFILES.get(p.profile, conform.PROFILES["h264"]).label,
        })
        if not ctx.get("title"):
            ctx["title"] = job.title or job.video_id or "untitled"
        ext = p.ext or "bin"
        target = naming.build_output_path(opts.resolved_output_dir(), opts.filename, opts.subfolder, ctx, ext,
                                          opts.ascii_names)
        if target.exists():
            if opts.conflict == "skip":
                self._existing_target = target
                self._set(stage_text="A file with this name already exists", skipped=True)
                return None
            if opts.conflict == "number":
                target = naming.unique_path(target)
        return target

    def _run_ffmpeg(self, p: conform.Plan, final: Path) -> None:
        tmp = final.with_name(final.stem + ".ingest-part" + final.suffix)
        plan = p
        while True:
            label = "Copying streams" if plan.video_action != "encode" and plan.audio_action != "encode" \
                else ("Converting to " + conform.PROFILES[plan.profile].label if plan.kind == "video"
                      else "Converting audio")
            self._resume_text = label
            self._set(stage_text=label, cv_progress=0.0, cv_speed=None, eta=None)
            ok, lines = self._ffmpeg([self.ctx.tools().ffmpeg, *plan.args, str(tmp)], plan.duration)
            if ok:
                break
            _unlink(tmp)
            f = ffmpeg_failure(lines)
            if f.retry_with == "cpu" and plan.fallback is not None:
                self.log("-- GPU encoder failed, falling back to the CPU encoder")
                self.job.warnings.append("GPU encoder failed, converted on the CPU")
                plan = plan.fallback
                self._set(plan_summary=plan.summary)
                continue
            raise JobFailed(f)
        os.replace(tmp, final)

    def _ffmpeg(self, args: list[str], duration: float) -> tuple[bool, list[str]]:
        proc = self._spawn(args)
        lines: list[str] = []
        out_time = 0.0
        speed = None
        for line in proc.lines():
            if "=" in line and not line.startswith(" "):
                k, _, v = line.partition("=")
                k = k.strip()
                if k in ("out_time_us", "out_time_ms"):
                    try:
                        out_time = int(v) / 1_000_000
                    except ValueError:
                        pass
                    continue
                if k == "speed":
                    try:
                        speed = float(v.strip().rstrip("x"))
                    except ValueError:
                        speed = None
                    continue
                if k == "progress":
                    frac = min(1.0, out_time / duration) if duration > 0 else 0.0
                    eta = (duration - out_time) / speed if speed and speed > 0 and duration > 0 else None
                    self._set(force=v.strip() == "end", cv_progress=frac, cv_speed=speed, eta=eta)
                    continue
                if k in ("frame", "fps", "stream_0_0_q", "bitrate", "total_size", "out_time", "dup_frames",
                         "drop_frames") or k.startswith("stream_"):
                    continue
            self.log(line)
            lines.append(line)
            if len(lines) > 400:
                del lines[:100]
        code = proc.wait()
        self._clear_proc()
        self._check_stop()
        return code == 0, lines

    def _verify(self, final: Path, p: conform.Plan) -> None:
        s = self.ctx.settings()
        if not getattr(s, "verify", True):
            return
        self._set(stage=Stage.VERIFY.value, stage_text="Verifying for Premiere and After Effects")
        tools = self.ctx.tools()
        try:
            mi = probe(tools.ffprobe, final)
        except ProbeError as e:
            raise JobFailed(Failure("verify", "The converted file can't be read", "Retry.", str(e)))
        timing = measure_timing(tools.ffprobe, final, seconds=20) if mi.video else None
        profile = p.profile if p.profile in conform.PROFILES else "h264"
        rep = verify.verify(mi, profile, p.out, timing, p.duration)
        self.log("-- verify: " + ", ".join(f"{c.name}={'ok' if c.ok else 'FAIL'}({c.detail})" for c in rep.checks))
        v = mi.video
        props = dict(self.job.out_props)
        if v:
            props.update({"width": v.width, "height": v.height, "rate": f"{v.fps:.3f}", "vcodec": v.codec})
        if mi.audio:
            props.update({"acodec": mi.audio.codec, "sample_rate": mi.audio.sample_rate})
        props["size"] = mi.size
        self._set(report=rep.to_dict(), out_props=props)

    def _make_proxy(self, final: Path) -> str | None:
        opts = self.job.opts
        tools = self.ctx.tools()
        try:
            mi = probe(tools.ffprobe, final)
        except ProbeError:
            return None
        v = mi.video
        if v is None:
            return None
        folder = final.parent / "Proxies"
        folder.mkdir(exist_ok=True)
        h = 540 if opts.proxy == "prores" else 720
        if min(v.width, v.height) <= h:
            return None
        scale = f"scale=-2:{h}:flags=bicubic" if v.width >= v.height else f"scale={h}:-2:flags=bicubic"
        if opts.proxy == "prores":
            ext, venc, aenc = ".mov", ["-c:v", "prores_ks", "-profile:v", "0", "-vendor", "apl0",
                                       "-pix_fmt", "yuv422p10le"], ["-c:a", "pcm_s16le"]
        else:
            ext, venc, aenc = ".mp4", ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                                       "-pix_fmt", "yuv420p", "-g", "15"], ["-c:a", "aac", "-b:a", "192k"]
        target = naming.unique_path(folder / f"{final.stem}_Proxy{ext}")
        tmp = target.with_name(target.stem + ".ingest-part" + ext)
        args = [tools.ffmpeg, "-hide_banner", "-nostdin", "-y", "-loglevel", "warning", "-progress", "pipe:1",
                "-nostats", "-i", str(final), "-map", "0:v:0", "-map", "0:a?", "-vf", scale, *venc, *aenc,
                "-map_metadata", "-1", "-movflags", "+faststart", str(tmp)]
        self._resume_text = "Making Premiere proxy"
        self._set(stage=Stage.CONVERT.value, stage_text="Making Premiere proxy", cv_progress=0.0)
        ok, _ = self._ffmpeg(args, mi.duration)
        if not ok:
            _unlink(tmp)
            self.job.warnings.append("Proxy couldn't be made")
            return None
        os.replace(tmp, target)
        return str(target)

    def _finish_gallery(self, media: list[str], images: list[str]) -> list[str]:
        job = self.job
        opts = job.opts
        tools = self.ctx.tools()
        out_dir = opts.resolved_output_dir()
        ctx = naming.context_from_info({}, {"title": job.title or naming_title_from_url(job.url),
                                            "site": job.site or _site_from_url(job.url)})
        folder_parts = naming.render(opts.filename, ctx, opts.ascii_names)
        folder = out_dir.joinpath(*naming.subfolder_parts(opts.subfolder, ctx, opts.ascii_names), *folder_parts)
        if len(media) + len(images) == 1:
            folder = folder.parent
        folder.mkdir(parents=True, exist_ok=True)
        outputs: list[str] = []
        total = len(media) + len(images)
        done = 0
        for path in images:
            self._check_stop()
            src = Path(path)
            ext = src.suffix.lower()
            stem = naming.sanitize_component(src.stem, opts.ascii_names)
            if total == 1:
                stem = folder_parts[-1]
            if ext in ADOBE_IMAGE_OK:
                target = naming.unique_path(naming.fit_path(folder, stem, ext))
                _move(path, target)
            else:
                target = naming.unique_path(naming.fit_path(folder, stem, ".png"))
                self._set(stage_text=f"Converting {ext[1:].upper()} to PNG")
                ok, _ = self._ffmpeg([tools.ffmpeg, "-hide_banner", "-nostdin", "-y", "-loglevel", "error",
                                      "-progress", "pipe:1", "-nostats", "-i", path, "-frames:v", "1",
                                      "-update", "1", str(target)], 0)
                if not ok:
                    _move(path, target.with_suffix(ext))
                    target = target.with_suffix(ext)
                    job.warnings.append(f"{src.name} kept as {ext[1:].upper()}, which Adobe apps can't open")
            outputs.append(str(target))
            done += 1
            self._set(cv_progress=done / total, force=False)
        for path in media:
            self._check_stop()
            mi = self._probe(path)
            try:
                p = conform.plan([mi], opts, self.ctx.caps(), None, title=job.title, source_url=job.url)
            except conform.PlanError as e:
                job.warnings.append(f"{Path(path).name}: {e}")
                continue
            stem = folder_parts[-1] if total == 1 else naming.sanitize_component(Path(path).stem, opts.ascii_names)
            target = naming.fit_path(folder, stem, p.ext or Path(path).suffix)
            target = naming.unique_path(target)
            if p.passthrough:
                _move(path, target)
            else:
                self._run_ffmpeg(p, target)
            outputs.append(str(target))
            done += 1
            self._set(cv_progress=done / total, plan_summary=p.summary)
        if not outputs:
            raise JobFailed(Failure("nothing", "Nothing usable was downloaded", "Open the log for details."))
        self._finalize(Path(outputs[0]), [], None)
        return outputs

    # ------------------------------------------------------------ finalize
    def _finalize(self, final: Path, inputs: list[str], p: conform.Plan | None) -> None:
        job = self.job
        opts = job.opts
        self._set(stage=Stage.FINALIZE.value, stage_text="Finishing")
        wd = Path(job.work_dir) if job.work_dir else None
        sidecars: list[str] = []
        if wd and wd.is_dir():
            used = {os.path.normcase(os.path.abspath(x)) for x in inputs}
            for f in sorted(wd.iterdir()):
                if not f.is_file() or os.path.normcase(os.path.abspath(f)) in used:
                    continue
                name = f.name.lower()
                if name == "info.json" or name.endswith((".part", ".ytdl", ".tmp")):
                    continue
                target = None
                if name.endswith(".srt"):
                    parts = f.name.split(".")
                    lang = parts[-2] if len(parts) >= 3 else "sub"
                    target = final.with_name(f"{final.stem}.{lang}.srt")
                elif name.endswith(".info.json") and opts.info_json:
                    target = final.with_name(f"{final.stem}.info.json")
                elif name.endswith(".description") and opts.description:
                    target = final.with_name(f"{final.stem}.description.txt")
                elif f.suffix.lower() in (".jpg", ".jpeg", ".png") and opts.thumbnail:
                    target = final.with_name(f"{final.stem}{f.suffix.lower()}")
                if target is not None:
                    target = naming.unique_path(target)
                    try:
                        _move(str(f), target)
                        sidecars.append(str(target))
                    except OSError as e:
                        job.warnings.append(f"Couldn't save {f.name}: {e}")
            if opts.keep_source and inputs:
                keep = final.parent / "Originals"
                keep.mkdir(exist_ok=True)
                for src in inputs:
                    sp = Path(src)
                    if sp.exists():
                        tgt = naming.unique_path(keep / f"{final.stem} [{_fmt_from_name(sp.name)}]{sp.suffix}")
                        try:
                            _move(src, tgt)
                            sidecars.append(str(tgt))
                        except OSError:
                            pass
        self._set(sidecars=sidecars)
        self._discard_work()
        if self.ctx.library is not None and final.exists():
            props = job.out_props or {}
            try:
                self.ctx.library.add(Item(
                    id=job.id, url=job.url, webpage_url=job.webpage_url, extractor=job.extractor,
                    video_id=job.video_id, title=job.title, uploader=job.uploader, site=job.site,
                    duration=job.duration, profile=job.opts.profile if p is None else p.profile, path=str(final),
                    size=final.stat().st_size, width=int(props.get("width") or 0),
                    height=int(props.get("height") or 0), fps=_fps_float(props.get("rate")),
                    vcodec=str(props.get("vcodec") or ""), acodec=str(props.get("acodec") or ""),
                    thumb_url=job.thumbnail, report=job.report, summary=job.plan_summary,
                    extra={"sidecars": sidecars, "warnings": job.warnings}))
            except Exception as e:
                self.log(f"library: {e}")

    _existing_target: Path = Path()
    _resume_text: str = "Converting"


# ---------------------------------------------------------------- helpers

def _quote(a: str) -> str:
    return f'"{a}"' if (" " in a or not a) else a


def _unlink(p: Path) -> None:
    try:
        p.unlink()
    except OSError:
        pass


def _move(src: str, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.replace(src, dst)
    except OSError:
        shutil.move(src, str(dst))


def _remove_if_empty(d: Path) -> None:
    try:
        if d.name == ".ingest-work" and d.is_dir() and not any(d.iterdir()):
            d.rmdir()
    except OSError:
        pass


def _scan_media(wd: Path) -> list[str]:
    files = []
    for f in wd.iterdir() if wd.is_dir() else []:
        if not f.is_file():
            continue
        name = f.name.lower()
        if name.endswith(".part"):
            # A stopped live recording: the .part file is the recording.
            stem = Path(f.name[:-5])
            if stem.suffix.lower() in MEDIA_EXTS and f.stat().st_size > 0:
                target = f.with_name(stem.name)
                os.replace(f, target)
                files.append(str(target))
            continue
        if f.suffix.lower() in MEDIA_EXTS and f.stat().st_size > 0:
            files.append(str(f))
    return files


def _weights(fmts: list[dict], duration: float) -> dict[str, float]:
    w: dict[str, float] = {}
    for f in fmts:
        fid = f.get("format_id")
        if not fid:
            continue
        size = f.get("filesize") or f.get("filesize_approx")
        if not size and f.get("tbr") and duration:
            size = float(f["tbr"]) * 125 * duration
        if not size:
            size = 0.15 if f.get("vcodec") in (None, "none") and f.get("acodec") not in (None, "none") else 0.85
        w[fid] = float(size)
    return w


def _site_name(info: dict) -> str:
    key = info.get("extractor_key") or ""
    if key.lower() == "generic" or not key:
        return info.get("webpage_url_domain") or _site_from_url(info.get("webpage_url") or "")
    return re.sub(r"(?i)(tab|ie|playlist|user|channel|post|shorts|clip|vod|video)$", "", key) or key


def _site_from_url(url: str) -> str:
    from .util import domain_of
    return domain_of(url)


def naming_title_from_url(url: str) -> str:
    from urllib.parse import unquote, urlparse
    try:
        p = urlparse(url)
    except ValueError:
        return url
    last = [x for x in p.path.split("/") if x]
    return unquote(last[-1]) if last else p.netloc


def _fmt_from_name(name: str) -> str:
    m = re.search(r"\.f([^.]+)\.[^.]+$", name)
    return m.group(1) if m else "source"


def _fps_float(rate) -> float:
    if not rate:
        return 0.0
    try:
        if isinstance(rate, str) and "/" in rate:
            a, b = rate.split("/")
            return float(a) / float(b)
        return float(rate)
    except (ValueError, ZeroDivisionError):
        return 0.0


def _fps_text(rate) -> str:
    from .util import fps_label
    f = _fps_float(rate)
    return fps_label(f) if f else ""
