"""Where Ingest keeps its files."""

from __future__ import annotations

import functools
import os
import sys
from pathlib import Path

from . import winapi

APP_DIR = "Ingest"


def is_windows() -> bool:
    return sys.platform == "win32"


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def _override() -> Path | None:
    value = os.environ.get("INGEST_HOME")
    return Path(value) if value else None


def config_dir() -> Path:
    """Roaming settings (small JSON files)."""
    o = _override()
    if o:
        return o / "config"
    if is_windows():
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / APP_DIR


def data_dir() -> Path:
    """Machine-local data: engines, caches, logs, the library database."""
    o = _override()
    if o:
        return o / "data"
    if is_windows():
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / APP_DIR


def engines_dir() -> Path:
    return data_dir() / "engines"


def cache_dir() -> Path:
    return data_dir() / "cache"


def thumbs_dir() -> Path:
    return cache_dir() / "thumbs"


def logs_dir() -> Path:
    return data_dir() / "logs"


def work_root() -> Path:
    return data_dir() / "work"


def resource_dir() -> Path:
    """Read-only resources: bundled assets inside the frozen app, or the repo root."""
    if is_frozen():
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parents[3]


def asset(*parts: str) -> Path:
    return resource_dir().joinpath("assets", *parts)


def bundled_engines_dir() -> Path:
    """Engines shipped next to Ingest.exe (portable builds)."""
    if is_frozen():
        return Path(sys.executable).parent / "engines"
    return resource_dir() / "engines"


@functools.lru_cache(maxsize=1)
def default_download_dir() -> Path:
    videos = winapi.known_folder(winapi.FOLDERID_VIDEOS) if is_windows() else None
    if videos is None:
        candidate = Path.home() / "Videos"
        videos = candidate if candidate.exists() else Path.home() / "Downloads"
    return videos / APP_DIR


def ensure(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path
