"""Persistent settings.

`OutputOptions` holds everything that shapes one download. A copy is frozen
into each job when it is queued, so changing the panel never alters work that
is already waiting. `Settings` holds app-wide preferences plus the default
`OutputOptions`.
"""

from __future__ import annotations

import dataclasses
import json
import os
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, get_args, get_origin, get_type_hints

from . import paths

SCHEMA = 1


@dataclass
class OutputOptions:
    # Format
    profile: str = "h264"          # h264 | prores | dnxhr | hevc | wav | aac | mp3 | original
    prores: str = "hq"             # proxy | lt | standard | hq
    dnxhr: str = "hq"              # lb | sq | hq | hqx
    # Video
    resolution: str = "best"       # best | 2160 | 1440 | 1080 | 720 | 480 | 360
    fps: str = "source"            # source | 23.976 | 24 | 25 | 29.97 | 30 | 50 | 59.94 | 60
    max_fps: str = "any"           # any | 30 | 60
    hdr: str = "sdr"               # sdr (prefer SDR, tone-map if needed) | keep
    prefer_compatible: bool = True  # choose H.264/AAC sources when nothing is lost
    quality: str = "high"          # max | high | balanced | compact
    encoder: str = "auto"          # auto | x264 | nvenc | qsv | amf
    speed: str = "fast"            # x264 / x265 preset (fast: good quality, 4K stays practical)
    gop: str = "1s"                # 1s | 2s | auto
    # Audio
    audio_rate: str = "48000"      # 48000 | source
    audio_bitrate: int = 320
    audio_channels: str = "source"  # source | stereo | mono
    pcm_depth: int = 24            # 16 | 24
    loudness: str = "off"          # off | -14 | -16 | -23
    # Extras
    subtitles: bool = False
    sub_langs: str = "en.*"
    auto_subs: bool = False
    thumbnail: bool = False
    info_json: bool = False
    description: bool = False
    sponsorblock: bool = False
    sponsor_categories: list[str] = field(default_factory=lambda: ["sponsor", "selfpromo", "interaction"])
    keep_source: bool = False
    proxy: str = "off"             # off | prores | h264  (Premiere proxy files)
    # Destination
    output_dir: str = ""
    subfolder: str = "none"        # none | site | uploader | playlist | date
    filename: str = "{title}"
    conflict: str = "number"       # number | overwrite | skip
    ascii_names: bool = False
    # Per-job overrides (set from the Inspect page, never saved as defaults)
    trim_start: float | None = None
    trim_end: float | None = None
    format_ids: str = ""
    audio_lang: str = ""

    def resolved_output_dir(self) -> Path:
        return Path(self.output_dir) if self.output_dir else paths.default_download_dir()


@dataclass
class Settings:
    schema: int = SCHEMA
    output: OutputOptions = field(default_factory=OutputOptions)
    # Downloads
    max_concurrent: int = 3
    max_conform: int = 1
    fragments: int = 4
    retries: int = 10
    rate_limit: str = ""
    playlist_links: str = "video"  # video | playlist  (for links that carry both)
    playlist_confirm: int = 50     # open Inspect instead of queueing playlists bigger than this
    skip_existing: bool = True
    work_location: str = "output"  # output | appdata
    resume_on_launch: bool = True
    # Network
    proxy: str = ""
    impersonate: str = "auto"      # auto | always | off
    force_ipv4: bool = False
    geo_country: str = ""
    insecure: bool = False
    # Accounts
    cookies: str = "off"           # off | browser | file
    cookies_browser: str = "firefox"
    cookies_profile: str = ""
    cookies_file: str = ""
    cookies_always: bool = False   # False: only retry with cookies when a site asks
    # Engines
    channel: str = "stable"        # stable | nightly
    auto_update: bool = True
    path_ytdlp: str = ""
    path_ffmpeg: str = ""
    path_deno: str = ""
    path_gallerydl: str = ""
    gallery_fallback: bool = True
    nightly_fallback: bool = True  # retry broken extractors with yt-dlp nightly
    last_update_check: float = 0.0
    # App
    clipboard_suggest: bool = True
    browser_links: bool = True     # register ingest:// for the bookmarklet
    notifications: bool = True
    verify: bool = True
    extra_args: str = ""
    recent_dirs: list[str] = field(default_factory=list)
    onboarded: bool = False


def _coerce(tp: Any, value: Any, default: Any) -> Any:
    """Best-effort conversion of JSON values back into the declared field types."""
    origin = get_origin(tp)
    if dataclasses.is_dataclass(tp):
        return from_dict(tp, value) if isinstance(value, dict) else default
    if origin is list:
        if not isinstance(value, list):
            return default
        (inner,) = get_args(tp) or (Any,)
        return [v for v in value if inner is Any or isinstance(v, inner)]
    args = get_args(tp)
    if args and type(None) in args:  # Optional[...]
        if value is None:
            return None
        non_none = [a for a in args if a is not type(None)]
        return _coerce(non_none[0], value, default)
    if tp is bool:
        return value if isinstance(value, bool) else default
    if tp is int:
        return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else default
    if tp is float:
        return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else default
    if tp is str:
        return value if isinstance(value, str) else default
    return value


def from_dict(cls, data: dict) -> Any:
    obj = cls()
    hints = get_type_hints(cls)
    for f in dataclasses.fields(cls):
        if f.name in data:
            setattr(obj, f.name, _coerce(hints[f.name], data[f.name], getattr(obj, f.name)))
    return obj


def to_dict(obj) -> dict:
    return dataclasses.asdict(obj)


def copy_options(opts: OutputOptions, **changes) -> OutputOptions:
    return dataclasses.replace(from_dict(OutputOptions, to_dict(opts)), **changes)


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        for attempt in range(6):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                # Windows refuses to replace a file that a virus scanner or the indexer has
                # open for a moment. Wait it out rather than lose the write.
                if attempt == 5:
                    raise
                time.sleep(0.05 * (attempt + 1))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class SettingsStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or paths.config_dir() / "settings.json"
        self._lock = threading.Lock()
        self.settings = self.load()

    def load(self) -> Settings:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("settings root is not an object")
            s = from_dict(Settings, data)
        except FileNotFoundError:
            s = Settings()
        except Exception:
            # Keep the unreadable file for inspection, start clean.
            try:
                self.path.replace(self.path.with_suffix(".broken.json"))
            except OSError:
                pass
            s = Settings()
        s.schema = SCHEMA
        # Per-job overrides never persist as defaults.
        s.output.trim_start = s.output.trim_end = None
        s.output.format_ids = ""
        s.output.audio_lang = ""
        return s

    def save(self) -> bool:
        with self._lock:
            try:
                atomic_write_text(self.path, json.dumps(to_dict(self.settings), indent=2, ensure_ascii=False))
                return True
            except (OSError, TypeError, ValueError):
                return False  # the next change saves again; never take the window down over it

    def remember_dir(self, d: str) -> None:
        dirs = [x for x in self.settings.recent_dirs if os.path.normcase(x) != os.path.normcase(d)]
        self.settings.recent_dirs = [d] + dirs[:7]
