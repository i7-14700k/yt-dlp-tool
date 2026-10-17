"""yt-dlp and gallery-dl command lines, plus parsing of their machine-readable output."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from . import paths
from .settings import OutputOptions, Settings
from .util import normalize_country, normalize_proxy, normalize_rate, split_args

PROGRESS_PREFIX = "@@P "
FILE_PREFIX = "@@F "
_decoder = json.JSONDecoder()


@dataclass
class Tools:
    ytdlp: str = ""
    ffmpeg: str = ""
    ffprobe: str = ""
    deno: str = ""
    gallerydl: str = ""

    @property
    def ffmpeg_dir(self) -> str:
        return str(Path(self.ffmpeg).parent) if self.ffmpeg else ""


@dataclass
class Network:
    """The network/account part of Settings, frozen per job."""
    proxy: str = ""
    impersonate: str = "auto"
    force_ipv4: bool = False
    geo_country: str = ""
    insecure: bool = False
    cookies: str = "off"
    cookies_browser: str = "firefox"
    cookies_profile: str = ""
    cookies_file: str = ""
    cookies_always: bool = False
    retries: int = 10
    fragments: int = 4
    rate_limit: str = ""
    extra_args: str = ""
    cache_dir: str = ""

    @classmethod
    def from_settings(cls, s: Settings, cache_dir: str) -> "Network":
        # Typed values are normalized here, so a stray "5 MB" or "Germany" can't fail every download.
        return cls(proxy=normalize_proxy(s.proxy), impersonate=s.impersonate, force_ipv4=s.force_ipv4,
                   geo_country=normalize_country(s.geo_country), insecure=s.insecure, cookies=s.cookies,
                   cookies_browser=s.cookies_browser, cookies_profile=s.cookies_profile.strip(),
                   cookies_file=s.cookies_file.strip(), cookies_always=s.cookies_always, retries=s.retries,
                   fragments=s.fragments, rate_limit=normalize_rate(s.rate_limit), extra_args=s.extra_args,
                   cache_dir=cache_dir)

    @property
    def has_cookies(self) -> bool:
        return (self.cookies == "browser" and bool(self.cookies_browser)) or \
               (self.cookies == "file" and bool(self.cookies_file))


@dataclass
class Attempt:
    """Per-attempt switches the job turns on when a site pushes back."""
    cookies: bool = False
    impersonate: bool = False
    nightly: bool = False
    check_formats: bool = False


def cookie_args(net: Network, tool: str = "yt-dlp") -> list[str]:
    if net.cookies == "browser" and net.cookies_browser:
        spec = net.cookies_browser
        if net.cookies_profile:
            spec += ":" + net.cookies_profile
        return ["--cookies-from-browser", spec]
    if net.cookies == "file" and net.cookies_file:
        return ["--cookies" if tool == "yt-dlp" else "-C", net.cookies_file]
    return []


def split_extra(text: str) -> list[str]:
    # Non-POSIX shlex kept the quotes on Windows: --extractor-args "youtube:..." arrived quoted.
    return split_args(text)


def base_args(tools: Tools, net: Network, attempt: Attempt) -> list[str]:
    a = [tools.ytdlp, "--ignore-config", "--color", "never", "--newline", "--no-mtime",
         "--plugin-dirs", str(paths.resource_dir() / "plugins"),
         "--retries", str(net.retries), "--fragment-retries", str(net.retries), "--extractor-retries", "3",
         "--retry-sleep", "fragment:exp=1:20", "--socket-timeout", "30",
         "--concurrent-fragments", str(max(1, min(16, net.fragments)))]
    if net.cache_dir:
        a += ["--cache-dir", net.cache_dir]
    if tools.ffmpeg:
        a += ["--ffmpeg-location", tools.ffmpeg_dir]
    if tools.deno:
        a += ["--js-runtimes", f"deno:{tools.deno}"]
    if net.proxy:
        a += ["--proxy", net.proxy]
    if net.force_ipv4:
        a += ["--force-ipv4"]
    if net.geo_country:
        a += ["--xff", net.geo_country.upper()]
    if net.insecure:
        a += ["--no-check-certificates"]
    if net.rate_limit:
        a += ["--limit-rate", net.rate_limit]
    if attempt.cookies or net.cookies_always:
        a += cookie_args(net)
    if attempt.impersonate or net.impersonate == "always":
        a += ["--impersonate", "chrome"]
    return a


def format_selection(opts: OutputOptions) -> tuple[str, str]:
    """yt-dlp -f and -S for an output profile.

    Sorting puts resolution and frame rate first, so a better picture always
    wins. Codec preference (H.264, AAC) only breaks ties, which lets Ingest
    copy streams instead of re-encoding whenever nothing is lost.
    """
    if opts.format_ids:
        return opts.format_ids, ""
    sort: list[str] = []
    profile = opts.profile
    if profile in ("wav", "aac", "mp3"):
        fmt = "ba/b"
        if profile == "aac" and opts.prefer_compatible:
            sort = ["acodec:aac"]
        if opts.audio_lang:
            fmt = f"ba[language^={opts.audio_lang}]/ba/b"
        return fmt, ",".join(sort)
    if opts.resolution.isdigit():
        sort.append(f"res:{opts.resolution}")
    else:
        sort.append("res")
    if opts.max_fps.isdigit():
        sort.append(f"fps:{opts.max_fps}")
    else:
        sort.append("fps")
    if opts.hdr == "sdr" or profile == "h264":
        sort.append("hdr:sdr")
    if profile == "original":
        fmt = "bv*+ba/b"
        return fmt, ",".join(sort) if (opts.resolution.isdigit() or opts.max_fps.isdigit()) else ""
    if opts.prefer_compatible:
        if profile == "h264":
            sort.append("vcodec:h264")
        elif profile == "hevc":
            sort.append("vcodec:h265")
        # AAC is copyable into MP4; for PCM targets the best source quality wins instead.
        if profile in ("h264", "hevc") and opts.audio_rate == "source":
            sort.append("acodec:aac")
    fmt = "bv*+ba/b"
    if opts.audio_lang:
        fmt = f"bv*+ba[language^={opts.audio_lang}]/bv*+ba/b"
    return fmt, ",".join(sort)


def subtitle_args(opts: OutputOptions) -> list[str]:
    a: list[str] = []
    if opts.subtitles:
        a += ["--write-subs", "--sub-langs", opts.sub_langs or "en.*", "--convert-subs", "srt"]
        if opts.auto_subs:
            a += ["--write-auto-subs"]
    if opts.thumbnail:
        a += ["--write-thumbnail", "--convert-thumbnails", "jpg"]
    if opts.info_json:
        a += ["--write-info-json"]
    if opts.description:
        a += ["--write-description"]
    return a


def resolve_args(tools: Tools, net: Network, attempt: Attempt, url: str, opts: OutputOptions,
                 playlist: bool) -> list[str]:
    fmt, sort = format_selection(opts)
    a = base_args(tools, net, attempt) + ["--dump-single-json", "--flat-playlist", "--no-progress"]
    a += ["--yes-playlist" if playlist else "--no-playlist"]
    if attempt.check_formats:
        a += ["--check-formats"]  # probe each format and keep the first that really downloads
    a += ["-f", fmt]
    if sort:
        a += ["-S", sort]
    a += subtitle_args(opts)
    a += split_extra(net.extra_args)
    a += ["--", url]
    return a


def download_args(tools: Tools, net: Network, attempt: Attempt, info_json: str | None, url: str,
                  format_ids: str, work: str, opts: OutputOptions, live: bool = False) -> list[str]:
    a = base_args(tools, net, attempt)
    a += ["--restrict-filenames", "--no-overwrites", "--continue", "--no-playlist",
          "-P", work,
          "-o", "%(id).40B.f%(format_id).40B.%(ext)s",
          "-o", "subtitle:%(id).40B",
          "-o", "thumbnail:%(id).40B",
          "-o", "infojson:%(id).40B",
          "-o", "description:%(id).40B",
          "-o", "pl_thumbnail:", "-o", "pl_infojson:", "-o", "pl_description:",
          "--progress", "--progress-delta", "0.25",
          "--progress-template", f"download:{PROGRESS_PREFIX}%(info.format_id)j %(progress)j",
          "--print", f"after_move:{FILE_PREFIX}%(format_id)j %(filepath)j"]
    a += ["-f", format_ids]
    if opts.trim_start is not None or opts.trim_end is not None:
        start = opts.trim_start or 0.0
        end = "inf" if opts.trim_end is None else f"{opts.trim_end:.3f}"
        a += ["--download-sections", f"*{start:.3f}-{end}"]
    if live:
        a += ["--hls-use-mpegts"]
    a += subtitle_args(opts)
    a += split_extra(net.extra_args)
    if info_json:
        a += ["--load-info-json", info_json]
    else:
        a += ["--", url]
    return a


def gallery_args(tools: Tools, net: Network, attempt: Attempt, url: str, work: str) -> list[str]:
    a = [tools.gallerydl, "--config-ignore", "-D", work, "-f", "{num:>03}_{filename}.{extension}"]
    if net.proxy:
        a += ["--proxy", net.proxy]
    if attempt.cookies or net.cookies_always:
        a += cookie_args(net, tool="gallery-dl")
    a += ["--", url]
    return a


# ---------------------------------------------------------------- output parsing

@dataclass
class Progress:
    format_id: str = ""
    status: str = ""
    downloaded: float = 0.0
    total: float | None = None
    speed: float | None = None
    eta: float | None = None
    fragment_index: int | None = None
    fragment_count: int | None = None
    percent: float | None = None


def parse_progress(line: str) -> Progress | None:
    if not line.startswith(PROGRESS_PREFIX):
        return None
    body = line[len(PROGRESS_PREFIX):].strip()
    try:
        fmt_id, end = _decoder.raw_decode(body)
        rest = body[end:].lstrip()
        data, _ = _decoder.raw_decode(rest)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    total = data.get("total_bytes") or data.get("total_bytes_estimate")
    p = Progress(
        format_id=str(fmt_id or ""),
        status=str(data.get("status") or ""),
        downloaded=float(data.get("downloaded_bytes") or 0),
        total=float(total) if total else None,
        speed=float(data["speed"]) if isinstance(data.get("speed"), (int, float)) else None,
        eta=float(data["eta"]) if isinstance(data.get("eta"), (int, float)) else None,
        fragment_index=data.get("fragment_index") if isinstance(data.get("fragment_index"), int) else None,
        fragment_count=data.get("fragment_count") if isinstance(data.get("fragment_count"), int) else None,
    )
    if p.status == "finished":
        p.percent = 100.0
    elif p.total:
        p.percent = max(0.0, min(100.0, p.downloaded * 100.0 / p.total))
    elif isinstance(data.get("_percent"), (int, float)):
        p.percent = float(data["_percent"])
    elif p.fragment_count:
        p.percent = 100.0 * (p.fragment_index or 0) / p.fragment_count
    return p


def parse_file(line: str) -> tuple[str, str] | None:
    if not line.startswith(FILE_PREFIX):
        return None
    body = line[len(FILE_PREFIX):].strip()
    try:
        fmt_id, end = _decoder.raw_decode(body)
        path, _ = _decoder.raw_decode(body[end:].lstrip())
    except ValueError:
        return None
    if not isinstance(path, str) or not path:
        return None
    return str(fmt_id or ""), path


def load_info(text: str) -> dict:
    start = text.find("{")
    if start < 0:
        raise ValueError("yt-dlp returned no metadata")
    data, _ = _decoder.raw_decode(text[start:])
    if not isinstance(data, dict):
        raise ValueError("yt-dlp metadata is not an object")
    return data


@dataclass
class Selection:
    """What yt-dlp picked, in terms the UI and the planner use."""
    format_ids: str
    formats: list[dict] = field(default_factory=list)
    width: int = 0
    height: int = 0
    fps: float = 0.0
    vcodec: str = ""
    acodec: str = ""
    dynamic_range: str = ""
    size: float = 0.0
    live: bool = False


def _codec_family(c: str | None) -> str:
    c = (c or "").lower()
    if not c or c == "none":
        return ""
    for prefix, fam in (("avc", "h264"), ("h264", "h264"), ("hev", "hevc"), ("hvc", "hevc"), ("h265", "hevc"),
                        ("vp09", "vp9"), ("vp9", "vp9"), ("vp8", "vp8"), ("av01", "av1"), ("av1", "av1"),
                        ("mp4a", "aac"), ("aac", "aac"), ("opus", "opus"), ("vorbis", "vorbis"),
                        ("mp3", "mp3"), ("flac", "flac"), ("ac-3", "ac3"), ("ac3", "ac3"), ("ec-3", "eac3"),
                        ("eac3", "eac3")):
        if c.startswith(prefix):
            return fam
    return c.split(".")[0]


def prefer_codec(info: dict, family: str) -> str | None:
    """Swap the chosen video for the same picture in `family`, when only the site's numbers differ.

    yt-dlp sorts by frame rate before codec, and some sites report one stream's
    rate inexactly per codec (Bilibili lists the same 24 fps 720p as 23.81 in
    AVC and 24.39 in HEVC), so the codec Premiere copies loses the tie. A swap
    needs the same frame size and dynamic range and a rate within 5 %.
    Returns the new format id, or None when nothing changed.
    """
    req = info.get("requested_formats")
    if not req:
        return None
    vi = next((i for i, f in enumerate(req)
               if f.get("vcodec") not in (None, "none") and f.get("acodec") in (None, "none")), None)
    if vi is None:
        return None
    cur = req[vi]
    if _codec_family(cur.get("vcodec")) == family or not cur.get("height"):
        return None
    fps = float(cur.get("fps") or 0)
    best = None
    for f in info.get("formats") or []:
        if f.get("acodec") not in (None, "none") or _codec_family(f.get("vcodec")) != family:
            continue
        if f.get("height") != cur.get("height") or (f.get("width") or 0) != (cur.get("width") or 0):
            continue
        if (f.get("dynamic_range") or "SDR") != (cur.get("dynamic_range") or "SDR"):
            continue
        ffps = float(f.get("fps") or 0)
        if fps and ffps and abs(ffps - fps) / fps > 0.05:
            continue
        if best is None or (f.get("tbr") or 0) > (best.get("tbr") or 0):
            best = f
    if best is None:
        return None
    req[vi] = best
    info["format_id"] = "+".join(str(f.get("format_id")) for f in req)
    for k in ("vcodec", "fps", "tbr", "vbr", "dynamic_range"):
        if k in best:
            info[k] = best[k]
    return str(best.get("format_id"))


def selection_from_info(info: dict) -> Selection:
    fmts = info.get("requested_formats") or ([info] if info.get("format_id") else [])
    ids = info.get("format_id") or "+".join(f.get("format_id", "") for f in fmts)
    sel = Selection(format_ids=",".join(f.get("format_id") for f in fmts if f.get("format_id")) or ids,
                    formats=fmts, live=bool(info.get("is_live")))
    for f in fmts:
        vc = f.get("vcodec")
        if vc and vc != "none" and not sel.height:
            sel.width, sel.height = int(f.get("width") or 0), int(f.get("height") or 0)
            sel.fps = float(f.get("fps") or 0)
            sel.vcodec = _codec_family(vc)
            sel.dynamic_range = f.get("dynamic_range") or ""
        ac = f.get("acodec")
        if ac and ac != "none" and not sel.acodec:
            sel.acodec = _codec_family(ac)
        sel.size += float(f.get("filesize") or f.get("filesize_approx") or 0)
    if not sel.height and info.get("height"):
        sel.width, sel.height = int(info.get("width") or 0), int(info.get("height") or 0)
        sel.fps = float(info.get("fps") or 0)
        sel.vcodec = _codec_family(info.get("vcodec"))
    if not sel.size and info.get("filesize_approx"):
        sel.size = float(info["filesize_approx"])
    if not sel.size and info.get("tbr") and info.get("duration"):
        sel.size = float(info["tbr"]) * 1000 / 8 * float(info["duration"])
    return sel


def codec_family(c: str | None) -> str:
    return _codec_family(c)
