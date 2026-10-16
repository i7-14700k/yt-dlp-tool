"""Small formatting and parsing helpers shared by the core and the UI."""

from __future__ import annotations

import math
import re
from urllib.parse import urlparse

_UNITS = ["B", "KB", "MB", "GB", "TB"]


def human_bytes(n: float | None, digits: int = 1) -> str:
    """Sizes the way Explorer shows them (1024-based, labelled KB/MB/GB)."""
    if n is None or n < 0 or math.isnan(n):
        return ""
    if n < 1024:
        return f"{int(n)} B"
    i = 0
    value = float(n)
    while value >= 1024 and i < len(_UNITS) - 1:
        value /= 1024
        i += 1
    if value >= 100 or i == 1:
        return f"{value:.0f} {_UNITS[i]}"
    return f"{value:.{digits}f} {_UNITS[i]}"


def human_speed(bps: float | None) -> str:
    if not bps or bps <= 0:
        return ""
    return human_bytes(bps) + "/s"


def human_duration(seconds: float | None) -> str:
    """12:04, 1:02:09. Empty for unknown."""
    if seconds is None or seconds < 0 or math.isnan(seconds):
        return ""
    s = int(round(seconds))
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def human_eta(seconds: float | None) -> str:
    if seconds is None or seconds < 0 or math.isnan(seconds) or seconds > 86400 * 7:
        return ""
    s = int(math.ceil(seconds))
    if s < 60:
        return f"{s} s left"
    m = s // 60
    if m < 60:
        return f"{m} min left"
    h, m = divmod(m, 60)
    return f"{h} h {m} min left" if m else f"{h} h left"


def timecode(seconds: float | None, frac: bool = True) -> str:
    """00:01:23.040 style, used for trim fields."""
    if seconds is None:
        return ""
    seconds = max(0.0, float(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if frac:
        return f"{int(h):02d}:{int(m):02d}:{s:06.3f}"
    return f"{int(h):02d}:{int(m):02d}:{int(s):02d}"


_TC_RE = re.compile(r"^\s*(?:(\d+):)?(?:(\d+):)?(\d+(?:[.,]\d+)?)\s*$")


def parse_timecode(text: str) -> float | None:
    """Accepts 90, 1:30, 01:30.5, 1:02:03.250. Returns seconds or None."""
    if text is None:
        return None
    text = text.strip()
    if not text:
        return None
    m = _TC_RE.match(text)
    if not m:
        return None
    a, b, c = m.groups()
    sec = float(c.replace(",", "."))
    if a is not None and b is not None:
        return int(a) * 3600 + int(b) * 60 + sec
    if a is not None:
        return int(a) * 60 + sec
    return sec


def fps_label(fps: float | None) -> str:
    if not fps:
        return ""
    for std, label in ((23.976, "23.976"), (29.97, "29.97"), (59.94, "59.94"), (47.952, "47.952"), (119.88, "119.88")):
        if abs(fps - std) < 0.01:
            return label
    if abs(fps - round(fps)) < 0.01:
        return str(int(round(fps)))
    return f"{fps:.3f}".rstrip("0").rstrip(".")


def resolution_label(width: int | None, height: int | None) -> str:
    if not width or not height:
        return ""
    return f"{width}×{height}"


def short_res(height: int | None, width: int | None = None) -> str:
    """Marketing-free height label: 2160p, 1080p. Uses the short side for vertical video."""
    if not height:
        return ""
    side = min(height, width) if width else height
    return f"{side}p"


_URL_RE = re.compile(r"""(?i)\b((?:https?://|www\.)[^\s<>"'`]+)""")


def extract_urls(text: str) -> list[str]:
    """Find links in pasted text, a dropped .txt file, or a browser drag.

    Trailing punctuation that is almost never part of a link is stripped, and
    duplicates are removed while keeping order.
    """
    if not text:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for m in _URL_RE.finditer(text):
        url = m.group(1).rstrip(".,;:!?)]}>'\"")
        if url.lower().startswith("www."):
            url = "https://" + url
        # Balance a trailing parenthesis that belongs to the URL (Wikipedia style).
        if url.count("(") > url.count(")") and m.group(1).endswith(")"):
            url += ")"
        if url not in seen:
            seen.add(url)
            out.append(url)
    return out


def is_url(text: str) -> bool:
    text = (text or "").strip()
    if not text or " " in text:
        return False
    try:
        p = urlparse(text if "://" in text else "https://" + text)
    except ValueError:
        return False
    return bool(p.netloc) and "." in p.netloc


def domain_of(url: str) -> str:
    try:
        host = urlparse(url).netloc.lower()
    except ValueError:
        return ""
    host = host.split("@")[-1].split(":")[0]
    for prefix in ("www.", "m.", "mobile.", "music."):
        if host.startswith(prefix) and host.count(".") > 1:
            host = host[len(prefix):]
    return host


def clamp(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else hi if v > hi else v
