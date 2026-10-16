"""Turn yt-dlp, gallery-dl and ffmpeg failures into a reason and a next step."""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Failure:
    kind: str
    title: str
    hint: str
    raw: str = ""
    retry_with: str = ""   # cookies | impersonate | gallery | refresh | update | ""


_RULES: list[tuple[str, str, str, str, str]] = [
    # kind, regex, title, hint, automatic retry
    ("bot_check", r"confirm you.?re not a bot|sign in to confirm",
     "The site is asking for a sign-in to prove you're human",
     "Turn on browser cookies in Settings > Accounts. Firefox works best on Windows.", "cookies"),
    ("age", r"confirm your age|age.?restricted|inappropriate for some users|age verification",
     "Age-restricted video", "Sign in with browser cookies in Settings > Accounts.", "cookies"),
    ("members", r"members.?only|join this channel|available to this channel's members|subscriber.?only|"
                r"premium members|requires a (?:paid )?subscription|purchase",
     "Members-only or paid content", "Sign in with an account that has access, through browser cookies.",
     "cookies"),
    ("private", r"private video|this video is private|is private|has been made private",
     "This video is private", "Only the owner and people they invited can see it.", "cookies"),
    ("cookie_read", r"could not copy .*cookie|failed to decrypt|dpapi|could not find .* cookies database|"
                    r"cookies? database|failed to load cookies|permission denied.*cookies|app.?bound",
     "Couldn't read the browser's cookies",
     "Chrome and Edge lock and encrypt their cookies on Windows. Use Firefox, or export a cookies.txt "
     "file and choose it in Settings > Accounts.", ""),
    ("login", r"login required|log in|sign in|account authentication is required|requires authentication|"
              r"only works when logged.?in|logged.?in|needs? (?:you to )?(?:be )?authenticat|"
              r"use --cookies|--cookies-from-browser|--username",
     "This site needs you to be signed in", "Turn on browser cookies in Settings > Accounts.", "cookies"),
    ("drm", r"\bdrm\b|widevine|playready|fairplay|protected by",
     "DRM-protected", "This stream is encrypted with DRM, which yt-dlp does not bypass.", ""),
    ("geo", r"not available (?:in|from) your (?:country|region|location)|geo.?restrict|geo.?block|"
            r"blocked it in your country|unavailable in your country|this content is not available in",
     "Blocked in your region", "Use a proxy or VPN in that region (Settings > Network > Proxy).", ""),
    ("upcoming", r"premieres in|this live event will begin|will begin in|scheduled|is_upcoming|"
                 r"waiting for (?:the )?(?:video|stream)|live stream recording is not available",
     "Not live yet", "Queue it again when the stream starts.", ""),
    ("ffmpeg", r"ffmpeg (?:not found|is not installed)|ffprobe (?:not found|and ffmpeg)",
     "ffmpeg is missing", "Install the engines in Settings > Engines.", ""),
    ("unavailable", r"video unavailable|has been removed|no longer available|does not exist|not found|"
                    r"http error 404|deleted|account.*(?:terminated|suspended)|copyright",
     "The video isn't available", "It was removed, or the link is wrong.", ""),
    ("rate_limit", r"http error 429|too many requests|rate.?limit|try again later",
     "The site is rate-limiting this PC",
     "Wait a few minutes, lower parallel downloads, or sign in with cookies.", "cookies"),
    ("forbidden", r"http error 403|forbidden|access denied|cloudflare|challenge|just a moment|captcha",
     "The site refused the request", "Retrying as a regular browser often fixes this.", "impersonate"),
    ("impersonate", r"impersonat",
     "The site needs browser impersonation", "Retrying as a regular browser.", "impersonate"),
    ("unsupported", r"unsupported url|no video could be found|no media found|not a valid url|"
                    r"unable to recognize|no suitable extractor",
     "No video found at this link", "Check that the page plays a video. Image galleries use gallery-dl.",
     "gallery"),
    ("no_formats", r"no video formats found|requested format is not available|no formats",
     "No downloadable formats", "Try updating yt-dlp in Settings > Engines, or pick another format.",
     "update"),
    ("js", r"n challenge|nsig|signature (?:extraction|solving)|javascript runtime|js runtime|\bejs\b|"
           r"player js|\bdeno\b",
     "YouTube's player changed", "Update yt-dlp and Deno in Settings > Engines.", "update"),
    ("extractor", r"unable to extract|failed to parse|please report this issue|extractor error|keyerror|"
                  r"typeerror|indexerror|attributeerror|nonetype|object has no attribute|must be str|"
                  r"list index out of range|not subscriptable|unexpected keyword|invalid literal|"
                  r"unable to download (?:json|api)",
     "yt-dlp can't read this page right now", "Update yt-dlp (nightly often has the fix already).",
     "update"),
    ("disk", r"no space left|not enough space|errno 28|winerror 112|disk full",
     "The disk is full", "Free space on the output drive or choose another folder.", ""),
    ("permission", r"permission denied|access is denied|winerror 5\b|errno 13|read-only file system",
     "Can't write to the output folder", "Choose a folder you own, or close the app holding the file.", ""),
    ("path", r"filename too long|winerror 206|errno 36|path too long",
     "The file path is too long", "Pick a shorter output folder or filename template.", ""),
    ("network", r"timed out|timeout|connection (?:reset|refused|aborted)|getaddrinfo|name or service not known|"
                r"no address associated|network is unreachable|temporary failure in name resolution|"
                r"winerror 10054|winerror 10060|ssl|certificate|eof occurred|remote end closed|"
                r"incomplete ?read|did not get any data|unable to download webpage",
     "Network error", "Check the connection or proxy, then retry.", "refresh"),
]

_COMPILED = [(k, re.compile(rx, re.I), t, h, r) for k, rx, t, h, r in _RULES]


def _error_lines(lines: list[str]) -> list[str]:
    errs = [ln for ln in lines if ln.lstrip().startswith(("ERROR:", "error:", "[error]"))]
    return errs or [ln for ln in lines if "error" in ln.lower()][-5:] or lines[-5:]


def classify(lines: list[str] | str, exit_code: int | None = None) -> Failure:
    if isinstance(lines, str):
        lines = lines.splitlines()
    errs = _error_lines([ln for ln in lines if ln.strip()])
    text = "\n".join(errs)
    raw = _clean(errs[-1]) if errs else (f"Exited with code {exit_code}" if exit_code else "Unknown error")
    for kind, rx, title, hint, retry in _COMPILED:
        if rx.search(text):
            return Failure(kind, title, hint, raw, retry)
    return Failure("unknown", "Download failed", "Open the log for details, or retry.", raw, "")


def _clean(line: str) -> str:
    line = re.sub(r"^\s*(ERROR|error|\[error\]):\s*", "", line.strip())
    line = re.sub(r"^\[[\w:.-]+\]\s*", "", line)  # "[youtube] "
    line = re.sub(r"^[^\s:]{3,80}:\s+(?=[A-Z])", "", line)  # "abc123: " before the message
    line = re.sub(r"\s*;?\s*please report this issue on .*$", "", line, flags=re.I)
    line = re.sub(r"\s*Confirm you are on the latest version.*$", "", line, flags=re.I)
    line = re.sub(r"\.?\s*See\s+https?://\S+\s+for (?:how to|more).*$", ".", line, flags=re.I)
    line = re.sub(r"\s*\(caused by .*\)$", "", line)
    line = re.sub(r"\s{2,}", " ", line)
    return line[:300]


def ffmpeg_failure(lines: list[str]) -> Failure:
    text = "\n".join(lines[-40:])
    low = text.lower()
    if re.search(r"no space left|not enough space", low):
        return Failure("disk", "The disk is full", "Free space on the output drive.", _last(lines))
    if re.search(r"permission denied|access is denied", low):
        return Failure("permission", "Can't write the output file", "Close apps that use it, or pick another "
                       "folder.", _last(lines))
    if re.search(r"(nvenc|amf|qsv|mfx|cuda|d3d11|vaapi).*(error|fail|cannot|not supported|no capable)", low) or \
            re.search(r"(openencodesessionex failed|no nvenc capable devices|driver does not support)", low):
        return Failure("hw_encoder", "The GPU encoder failed", "Retrying on the CPU encoder.", _last(lines),
                       "cpu")
    if "error applying bitstream filters" in low:
        # A stream copy whose in-place header fix (h264_metadata and friends) can't parse the
        # stream, e.g. Twitch's custom SEI. ffmpeg drops those packets and still exits 0.
        return Failure("bsf", "The video stream couldn't be rewritten in place",
                       "Retrying with the fix in the container instead.", _last(lines), "plain")
    if "invalid data found" in low or "moov atom not found" in low:
        return Failure("corrupt", "The download is damaged", "Retry the download.", _last(lines), "refresh")
    return Failure("convert", "Conversion failed", "Open the log for the ffmpeg output.", _last(lines))


def os_failure(e: OSError) -> Failure:
    """A file operation Ingest itself did (create the folder, move the result) failed."""
    import errno
    where = getattr(e, "filename", None) or ""
    raw = f"{type(e).__name__}: {e}"
    win = getattr(e, "winerror", None)
    if e.errno == errno.ENOSPC or win in (39, 112):
        return Failure("disk", "The disk is full", "Free space on the output drive, then retry.", raw)
    if e.errno in (errno.EACCES, errno.EPERM) or win in (5, 32, 33):
        return Failure("permission", "Can't write to the output folder",
                       "Pick another folder under Save to, or close the app that has the file open.", raw)
    if e.errno == errno.ENAMETOOLONG or win == 206:
        return Failure("path", "The file path is too long for Windows",
                       "Pick a shorter output folder or file name template.", raw)
    if e.errno in (errno.EEXIST, errno.ENOTDIR) or win in (183, 267):
        return Failure("path", "A file is in the way of the output folder",
                       "Pick another folder under Save to, or move that file.", raw)
    if e.errno == errno.ENOENT or win in (2, 3, 21, 53, 67):
        return Failure("path", "The output folder isn't reachable",
                       "Check that the drive or network share is connected, then retry.", raw)
    return Failure("io", "Couldn't save the file", f"Check {where or 'the output folder'} and retry.", raw)


def _last(lines: list[str]) -> str:
    for ln in reversed(lines):
        if ln.strip() and not ln.startswith(("frame=", "fps=", "progress=", "out_time", "speed=", "bitrate=",
                                              "total_size=", "dup_frames", "drop_frames", "stream_")):
            return ln.strip()[:300]
    return ""
