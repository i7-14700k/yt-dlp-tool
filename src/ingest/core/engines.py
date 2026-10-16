"""Install, find, version and update the command-line engines Ingest drives.

  yt-dlp     the downloader (the part that knows about 1,800+ sites)
  ffmpeg     merging, converting, measuring (with ffprobe)
  Deno       JavaScript runtime yt-dlp needs to solve YouTube's player challenges
  gallery-dl optional fallback for image galleries and sites yt-dlp doesn't cover

Each install lands in its own versioned folder under %LOCALAPPDATA%\\Ingest\\engines,
so an update never replaces an executable that a running download is using.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import stat
import sys
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import date
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .. import __version__
from . import paths
from .procs import run_capture
from .util import normalize_proxy
from .ytdlp import Tools

IS_WINDOWS = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"
EXE = ".exe" if IS_WINDOWS else ""
UA = f"Ingest/{__version__} (+https://github.com/i7-14700k/yt-dlp-tool)"

ProgressCb = Callable[[str, float, float | None], None]  # (phase, done, total)


@dataclass(frozen=True)
class Spec:
    key: str
    name: str
    files: tuple[str, ...]
    required: bool
    purpose: str
    size: str


SPECS: dict[str, Spec] = {s.key: s for s in [
    Spec("ytdlp", "yt-dlp", (f"yt-dlp{EXE}",), True, "Downloads from 1,800+ sites", "18 MB"),
    Spec("ffmpeg", "FFmpeg", (f"ffmpeg{EXE}", f"ffprobe{EXE}"), True, "Merges and converts for Premiere",
         "95 MB"),
    Spec("deno", "Deno", (f"deno{EXE}",), True, "Solves YouTube's player challenges", "45 MB"),
    Spec("gallerydl", "gallery-dl", (f"gallery-dl{EXE}",), False, "Image galleries and extra sites", "14 MB"),
    Spec("ytdlp_nightly", "yt-dlp nightly", (f"yt-dlp{EXE}",), False,
         "Fetched on its own when a site breaks, used only for that retry", "18 MB"),
]}
SETUP_KEYS = ("ytdlp", "ffmpeg", "deno", "gallerydl")
CUSTOM_ATTR = {"ytdlp": "path_ytdlp", "ffmpeg": "path_ffmpeg", "deno": "path_deno", "gallerydl": "path_gallerydl"}

_GH = "https://github.com"
_FFB = f"{_GH}/yt-dlp/FFmpeg-Builds/releases/download/latest"


def _arch() -> str:
    m = platform.machine().lower()
    return "arm64" if m in ("arm64", "aarch64") else "x64"


def asset_plan(key: str, channel: str = "stable") -> dict:
    """Where to download an engine for this platform and how to check it."""
    arch = _arch()
    if key in ("ytdlp", "ytdlp_nightly"):
        repo = "yt-dlp/yt-dlp-nightly-builds" if channel == "nightly" or key == "ytdlp_nightly" else "yt-dlp/yt-dlp"
        if IS_WINDOWS:
            name = "yt-dlp.exe"  # x64 build; runs emulated on Windows on ARM
        elif IS_MAC:
            name = "yt-dlp_macos"
        else:
            name = "yt-dlp_linux_aarch64" if arch == "arm64" else "yt-dlp_linux"
        base = f"{_GH}/{repo}/releases/latest/download"
        return {"url": f"{base}/{name}", "sums": f"{base}/SHA2-256SUMS", "sum_name": name,
                "kind": "file", "target": f"yt-dlp{EXE}", "api": f"https://api.github.com/repos/{repo}/releases/latest"}
    if key == "ffmpeg":
        if IS_WINDOWS:
            name = "ffmpeg-master-latest-win64-gpl.zip"
        elif IS_MAC:
            return {}
        else:
            name = f"ffmpeg-master-latest-linux{'arm' if arch == 'arm64' else ''}64-gpl.tar.xz"
        return {"url": f"{_FFB}/{name}", "sums": f"{_FFB}/checksums.sha256", "sum_name": name,
                "kind": "archive", "members": (f"ffmpeg{EXE}", f"ffprobe{EXE}")}
    if key == "deno":
        if IS_WINDOWS:
            triple = "x86_64-pc-windows-msvc"
        elif IS_MAC:
            triple = "aarch64-apple-darwin" if arch == "arm64" else "x86_64-apple-darwin"
        else:
            triple = "aarch64-unknown-linux-gnu" if arch == "arm64" else "x86_64-unknown-linux-gnu"
        url = f"{_GH}/denoland/deno/releases/latest/download/deno-{triple}.zip"
        return {"url": url, "sums": url + ".sha256sum", "sum_name": None, "kind": "archive",
                "members": (f"deno{EXE}",), "api": "https://api.github.com/repos/denoland/deno/releases/latest"}
    if key == "gallerydl":
        name = "gallery-dl_windows.exe" if IS_WINDOWS else "gallery-dl_macos" if IS_MAC else "gallery-dl_linux"
        return {"url": f"{_GH}/gdl-org/builds/releases/latest/download/{name}", "sums": None,
                "kind": "file", "target": f"gallery-dl{EXE}"}
    raise KeyError(key)


class InstallError(RuntimeError):
    pass


class Cancelled(Exception):
    pass


def _opener(proxy: str = "") -> urllib.request.OpenerDirector:
    handlers: list = []
    proxy = normalize_proxy(proxy)
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    return urllib.request.build_opener(*handlers)


def http_get(url: str, proxy: str = "", timeout: float = 30, accept: str = "*/*") -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": accept})
    with _opener(proxy).open(req, timeout=timeout) as r:
        return r.read()


def http_json(url: str, proxy: str = "", timeout: float = 20) -> dict:
    return json.loads(http_get(url, proxy, timeout, "application/vnd.github+json").decode("utf-8"))


def download_file(url: str, dest: Path, proxy: str = "", progress: ProgressCb | None = None,
                  cancel: threading.Event | None = None, phase: str = "Downloading") -> str:
    """Stream to dest, returning the SHA-256. Raises Cancelled or InstallError."""
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    h = hashlib.sha256()
    try:
        with _opener(proxy).open(req, timeout=45) as r, open(dest, "wb") as fh:
            total = r.headers.get("Content-Length")
            total_f = float(total) if total and total.isdigit() else None
            done = 0
            last = 0.0
            while True:
                if cancel is not None and cancel.is_set():
                    raise Cancelled()
                chunk = r.read(1 << 17)
                if not chunk:
                    break
                fh.write(chunk)
                h.update(chunk)
                done += len(chunk)
                now = time.monotonic()
                if progress and (now - last > 0.1):
                    last = now
                    progress(phase, done, total_f)
            if progress:
                progress(phase, done, total_f)
            if total_f and done < total_f:
                raise InstallError("The download was cut off. Check the connection and retry.")
    except urllib.error.HTTPError as e:
        raise InstallError(f"Server answered {e.code} for {url}") from e
    except urllib.error.URLError as e:
        raise InstallError(f"Couldn't reach {urllib.parse.urlparse(url).netloc}: {e.reason}") from e
    except TimeoutError as e:
        raise InstallError("The download timed out.") from e
    return h.hexdigest()


def _expected_sum(plan: dict, proxy: str) -> str | None:
    url = plan.get("sums")
    if not url:
        return None
    try:
        text = http_get(url, proxy, 20).decode("utf-8", "replace")
    except Exception:
        return None
    name = plan.get("sum_name")
    for line in text.splitlines():
        m = re.match(r"^\s*([0-9a-fA-F]{64})\s+\*?(\S+)\s*$", line)
        if m and (name is None or m.group(2) == name):
            return m.group(1).lower()
    m = re.search(r"\b([0-9a-fA-F]{64})\b", text)  # Deno publishes Get-FileHash output
    return m.group(1).lower() if m and name is None else None


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


@dataclass
class EngineState:
    key: str
    path: str = ""
    version: str = ""
    source: str = ""        # managed | custom | bundled | system
    ok: bool = False
    error: str = ""
    latest: str = ""
    update: bool = False
    extra: dict = field(default_factory=dict)


def parse_version(key: str, output: str) -> str:
    out = output.strip()
    if not out:
        return ""
    first = out.splitlines()[0].strip()
    if key == "ffmpeg":
        m = re.search(r"version\s+(\S+)", first)
        v = m.group(1) if m else first
        m2 = re.match(r"N-\d+-g[0-9a-f]+-(\d{4})(\d{2})(\d{2})", v)
        if m2:
            return f"master {m2.group(1)}-{m2.group(2)}-{m2.group(3)}"
        m3 = re.match(r"n?(\d+(?:\.\d+)+)", v)
        return m3.group(1) if m3 else v[:24]
    if key == "deno":
        m = re.search(r"deno\s+(\S+)", first)
        return m.group(1) if m else first[:24]
    return first[:32]


def too_old(key: str, version: str, today: date | None = None) -> str:
    """Why a copy found on PATH can't be relied on, or "" when it's fine.

    Ingest keeps its own copies current, but one that came with something else
    may be years behind: FFmpeg before 7 lacks filters the conversions use, and
    a yt-dlp that's months old has usually lost a few sites.
    """
    if key == "ffmpeg":
        m = re.match(r"(\d+)\.", version or "")
        if m and int(m.group(1)) < 7:
            return "Too old: Ingest needs FFmpeg 7 or newer"
    elif key == "ytdlp":
        m = re.match(r"(\d{4})\.(\d{2})\.(\d{2})", version or "")
        if m:
            try:
                age = ((today or date.today()) - date(int(m.group(1)), int(m.group(2)), int(m.group(3)))).days
            except ValueError:
                return ""
            if age > 120:
                return f"{age // 30} months old: sites change faster than that"
    return ""


def check_engine_file(key: str, path: str) -> tuple[str, str]:
    """Is the file someone picked with Locate really this engine?

    Returns (path to use, note). An empty path means it was refused and the
    note says why; otherwise the note is an optional warning. A folder, or
    ffprobe picked instead of ffmpeg, resolves to the right file next to it.
    """
    spec = SPECS[key]
    p = Path(path)
    if p.is_dir():
        p = p / spec.files[0]
    elif key == "ffmpeg" and p.name.lower().startswith("ffprobe"):
        p = p.with_name(spec.files[0])
    if not p.is_file():
        return "", f"There's no {spec.files[0]} there."
    flag = "-version" if key == "ffmpeg" else "--version"
    code, out = run_capture([str(p), flag], timeout=60)
    first = out.strip().splitlines()[0].strip() if out.strip() else ""
    if not first:
        return "", f"{p.name} didn't run. Pick {spec.files[0]} itself."
    looks_right = code == 0 and {
        "ffmpeg": first.lower().startswith("ffmpeg version"),
        "ytdlp": bool(re.match(r"\d{4}\.\d{1,2}\.\d{1,2}", first)),
        "deno": first.lower().startswith("deno "),
        "gallerydl": bool(re.match(r"\d+\.\d+", first)),
    }.get(key, True)
    if not looks_right:
        said = first.split(" Copyright")[0][:60]
        return "", f"That's not {spec.name}: {p.name} says \u201c{said}\u201d."
    if key == "ffmpeg" and not p.with_name(f"ffprobe{EXE}").is_file():
        return "", f"ffprobe{EXE} has to be next to ffmpeg{EXE}; Ingest needs both."
    version = parse_version(key, out)
    why = too_old(key, version)
    if why and key == "ffmpeg":
        return "", f"FFmpeg {version} is too old: Ingest needs 7 or newer."
    return str(p), (f"yt-dlp {version} is {why.split(':')[0]}; sites may fail until it's updated." if why else "")


class Engines:
    """Thread-safe registry of engine locations and versions."""

    def __init__(self, get_settings: Callable[[], object]) -> None:
        self._settings = get_settings
        self._lock = threading.RLock()
        self._versions: dict[str, tuple[str, str]] = {}  # key -> (path, version)
        self.root = paths.engines_dir()

    # ---------------------------------------------------------- location
    def _manifest_path(self, key: str) -> Path:
        return self.root / key / "manifest.json"

    def manifest(self, key: str) -> dict:
        try:
            return json.loads(self._manifest_path(key).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _custom(self, key: str) -> str:
        attr = CUSTOM_ATTR.get(key)
        return (getattr(self._settings(), attr, "") or "") if attr else ""

    def locate(self, key: str) -> tuple[str, str]:
        """Return (path to primary executable, source) or ("", "")."""
        spec = SPECS[key]
        primary = spec.files[0]
        custom = self._custom(key)
        if key == "ytdlp_nightly":
            m = self.manifest(key)
            p = self.root / key / m.get("current", "-") / primary
            return (str(p), "managed") if m.get("current") and p.is_file() else ("", "")
        if custom:
            p = Path(custom)
            if p.is_dir():
                p = p / primary
            if p.is_file():
                return str(p), "custom"
        m = self.manifest(key)
        if m.get("current"):
            p = self.root / key / m["current"] / primary
            if p.is_file():
                return str(p), "managed"
        b = paths.bundled_engines_dir() / primary
        if b.is_file():
            return str(b), "bundled"
        w = shutil.which(primary) or (shutil.which(primary.replace(EXE, "")) if EXE else None)
        if not w and key == "ytdlp":
            w = shutil.which("yt-dlp_linux") if not IS_WINDOWS else None
        if w:
            return w, "system"
        return "", ""

    def tools(self) -> Tools:
        y, _ = self.locate("ytdlp")
        f, _ = self.locate("ffmpeg")
        probe = ""
        if f:
            cand = Path(f).with_name(f"ffprobe{EXE}")
            probe = str(cand) if cand.is_file() else (shutil.which(f"ffprobe{EXE}") or "")
        d, _ = self.locate("deno")
        g, _ = self.locate("gallerydl")
        return Tools(ytdlp=y, ffmpeg=f, ffprobe=probe, deno=d, gallerydl=g)

    def version(self, key: str, refresh: bool = False) -> str:
        path, _ = self.locate(key)
        if not path:
            return ""
        with self._lock:
            cached = self._versions.get(key)
            if cached and cached[0] == path and not refresh:
                return cached[1]
        flag = "-version" if key == "ffmpeg" else "--version"
        code, out = run_capture([path, flag], timeout=40)
        v = parse_version(key, out) if code == 0 else ""
        with self._lock:
            self._versions[key] = (path, v)
        return v

    def state(self, key: str, refresh: bool = False) -> EngineState:
        path, source = self.locate(key)
        st = EngineState(key=key, path=path, source=source)
        if not path:
            st.error = "Not installed"
            return st
        if key == "ffmpeg" and not self.tools().ffprobe:
            st.error = "ffprobe is missing next to ffmpeg"
        st.version = self.version(key, refresh)
        if source in ("system", "custom") and not st.error:
            st.error = too_old(key, st.version)
            st.extra["too_old"] = bool(st.error)
        st.ok = bool(st.version) and not st.error
        if not st.version and not st.error:
            st.error = "Doesn't run"
        m = self.manifest(key)
        st.latest = m.get("latest", "")
        st.update = bool(m.get("update_available")) and source == "managed"
        return st

    def missing_required(self) -> list[str]:
        """Required engines with nothing usable: not found, or only an outdated copy on PATH."""
        out = []
        for k, s in SPECS.items():
            if not s.required:
                continue
            path, source = self.locate(k)
            if not path or (source == "system" and too_old(k, self.version(k))):
                out.append(k)
        return out

    # ---------------------------------------------------------- install / update
    def install(self, key: str, progress: ProgressCb | None = None, cancel: threading.Event | None = None) -> str:
        s = self._settings()
        plan = asset_plan(key, getattr(s, "channel", "stable"))
        if not plan:
            raise InstallError(f"No {SPECS[key].name} build is published for this system. Install it yourself "
                               f"and set its path in Settings > Engines.")
        proxy = getattr(s, "proxy", "")
        folder_name = time.strftime("v%Y%m%d-%H%M%S")
        base = self.root / key
        base.mkdir(parents=True, exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix=".install-", dir=str(base)))
        try:
            if progress:
                progress("Checking", 0, None)
            expected = _expected_sum(plan, proxy)
            if plan.get("sums") and not expected:
                # Unverified binaries don't get installed, whatever stopped the check.
                raise InstallError(f"Couldn't get the published checksum for {SPECS[key].name}, so nothing was "
                                   "installed. Check the connection and try again.")
            fname = plan["url"].rsplit("/", 1)[-1]
            blob = stage / fname
            digest = download_file(plan["url"], blob, proxy, progress, cancel)
            if expected and digest != expected:
                raise InstallError(f"{SPECS[key].name} failed its checksum. Nothing was installed.")
            out_dir = stage / "out"
            out_dir.mkdir()
            if progress:
                progress("Unpacking", 0, None)
            if plan["kind"] == "file":
                target = out_dir / plan["target"]
                shutil.move(str(blob), target)
            else:
                _extract_members(blob, out_dir, plan["members"], cancel)
            for f in out_dir.iterdir():
                if not IS_WINDOWS:
                    f.chmod(f.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
            primary = out_dir / SPECS[key].files[0]
            flag = "-version" if key == "ffmpeg" else "--version"
            if progress:
                progress("Testing", 0, None)
            code, out = run_capture([str(primary), flag], timeout=60)
            if code != 0:
                raise InstallError(f"The downloaded {SPECS[key].name} doesn't run: {out.strip()[:200]}")
            version = parse_version(key, out)
            final = base / folder_name
            out_dir.rename(final)
            m = self.manifest(key)
            m.update({"current": folder_name, "version": version, "sha256": digest, "url": plan["url"],
                      "installed": time.time(), "channel": getattr(s, "channel", "stable"),
                      "update_available": False, "latest": version})
            self._write_manifest(key, m)
            with self._lock:
                self._versions.pop(key, None)
            self.cleanup(key)
            return str(final / SPECS[key].files[0])
        finally:
            shutil.rmtree(stage, ignore_errors=True)

    def _write_manifest(self, key: str, data: dict) -> None:
        p = self._manifest_path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, p)

    def cleanup(self, key: str) -> None:
        """Remove old version folders. Files still in use on Windows simply stay until next time."""
        current = self.manifest(key).get("current")
        base = self.root / key
        if not base.is_dir():
            return
        for child in base.iterdir():
            if child.is_dir() and child.name != current and (child.name.startswith("v") or
                                                             child.name.startswith(".install-")):
                shutil.rmtree(child, ignore_errors=True)

    def check_update(self, key: str) -> tuple[bool, str]:
        """(update available, latest version label). Only for engines Ingest installed itself."""
        _, source = self.locate(key)
        if source != "managed":
            return False, ""
        s = self._settings()
        plan = asset_plan(key, getattr(s, "channel", "stable"))
        m = self.manifest(key)
        proxy = getattr(s, "proxy", "")
        latest, available = "", False
        if plan.get("api"):
            try:
                tag = str(http_json(plan["api"], proxy).get("tag_name", "")).lstrip("v")
                latest = tag
                local = (m.get("version") or "").lstrip("v")
                available = bool(tag) and tag != local
                if key == "ytdlp" and m.get("channel", "stable") != getattr(s, "channel", "stable"):
                    available = True
            except Exception:
                latest = ""
        if not latest and plan.get("sums"):
            expected = _expected_sum(plan, proxy)
            if expected and m.get("sha256"):
                available = expected != m["sha256"]
                latest = "newer build" if available else m.get("version", "")
        m["latest"] = latest or m.get("latest", "")
        m["update_available"] = available
        m["checked"] = time.time()
        self._write_manifest(key, m)
        return available, latest


def _extract_members(archive: Path, out_dir: Path, members: tuple[str, ...], cancel) -> None:
    wanted = {m.lower(): m for m in members}
    found: set[str] = set()
    name = archive.name.lower()
    if name.endswith(".zip"):
        with zipfile.ZipFile(archive) as z:
            for info in z.infolist():
                base = info.filename.replace("\\", "/").rsplit("/", 1)[-1]
                if base.lower() in wanted and not info.is_dir():
                    if cancel is not None and cancel.is_set():
                        raise Cancelled()
                    with z.open(info) as src, open(out_dir / wanted[base.lower()], "wb") as dst:
                        shutil.copyfileobj(src, dst, 1 << 20)
                    found.add(base.lower())
    elif ".tar" in name:
        with tarfile.open(archive) as t:
            for info in t:
                base = info.name.rsplit("/", 1)[-1]
                if base.lower() in wanted and info.isfile():
                    src = t.extractfile(info)
                    if src is None:
                        continue
                    with src, open(out_dir / wanted[base.lower()], "wb") as dst:
                        shutil.copyfileobj(src, dst, 1 << 20)
                    found.add(base.lower())
    else:
        raise InstallError(f"Unknown archive format: {archive.name}")
    missing = [m for m in members if m.lower() not in found]
    if missing:
        raise InstallError(f"The archive is missing {', '.join(missing)}")
