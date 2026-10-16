"""Output file names that Windows, Premiere Pro and After Effects all accept.

Adobe apps are not long-path aware and have a history of trouble with emoji
and other characters outside the Basic Multilingual Plane, so names are kept
inside MAX_PATH and those characters are removed. Non-Latin scripts are kept.
"""

from __future__ import annotations

import os
import re
import unicodedata
from datetime import datetime
from pathlib import Path

MAX_PATH_SAFE = 250  # leaves room under MAX_PATH (260) for Premiere's own suffixes
MAX_COMPONENT = 120

_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
_TOKEN_RE = re.compile(r"\{(\w+)(?::([^{}]*))?\}")

TOKENS = {
    "title": "Video title",
    "uploader": "Channel or account name",
    "id": "Site's video ID",
    "site": "Site name",
    "date": "Upload date, 2026-10-16",
    "year": "Upload year",
    "playlist": "Playlist title",
    "index": "Position in playlist",
    "res": "Output resolution, 1080p",
    "fps": "Output frame rate",
    "profile": "Output format, H.264",
    "today": "Download date",
}

PRESETS = [
    ("{title}", "Title"),
    ("{title} [{id}]", "Title [ID]"),
    ("{uploader} - {title}", "Channel - Title"),
    ("{date} {title}", "Date Title"),
    ("{index:03} {title}", "Playlist number Title"),
    ("{title} {res} {fps}", "Title Resolution Frame rate"),
]


def _replace_chars(s: str) -> str:
    s = s.replace(": ", " - ").replace(":", "-")
    s = re.sub(r"[\\/|]", "-", s)
    s = s.replace('"', "'")
    s = re.sub(r"[<>*?]", "", s)
    return s


def sanitize_component(text: str, ascii_only: bool = False, max_len: int = MAX_COMPONENT) -> str:
    s = unicodedata.normalize("NFC", str(text or ""))
    # Control characters, zero-width joiners, bidi controls and everything past U+FFFF (emoji).
    s = "".join(
        ch for ch in s
        if ord(ch) <= 0xFFFF and unicodedata.category(ch) not in ("Cc", "Cf", "Cs", "Co", "So")
        or ch in "\t "
    )
    s = _replace_chars(s)
    if ascii_only:
        s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(r"(\s*-\s*){2,}", " - ", s)
    s = s.strip(" .-_")
    if len(s) > max_len:
        cut = s[:max_len]
        space = cut.rfind(" ")
        if space > max_len * 0.6:
            cut = cut[:space]
        s = cut.rstrip(" .-_,")
    if s.split(".")[0].upper() in _RESERVED:
        s += "_"
    return s or "untitled"


def _fmt(value, spec: str | None) -> str:
    if value is None or value == "":
        return ""
    if spec:
        try:
            if isinstance(value, str) and value.isdigit():
                value = int(value)
            return format(value, spec)
        except (ValueError, TypeError):
            pass
    return str(value)


def _date(upload_date: str | None) -> str:
    if upload_date and re.fullmatch(r"\d{8}", upload_date):
        return f"{upload_date[:4]}-{upload_date[4:6]}-{upload_date[6:]}"
    return ""


def context_from_info(info: dict, extra: dict | None = None) -> dict:
    ctx = {
        "title": info.get("title") or info.get("fulltitle") or info.get("id") or "",
        "uploader": info.get("uploader") or info.get("channel") or info.get("creator") or "",
        "id": info.get("id") or "",
        "site": info.get("extractor_key") or info.get("extractor") or "",
        "date": _date(info.get("upload_date") or info.get("release_date")),
        "year": (info.get("upload_date") or "")[:4],
        "playlist": info.get("playlist_title") or info.get("playlist") or "",
        "index": info.get("playlist_index") or "",
        "today": datetime.now().strftime("%Y-%m-%d"),
    }
    if ctx["site"].lower() == "generic":
        ctx["site"] = info.get("webpage_url_domain") or "web"
    if extra:
        ctx.update({k: v for k, v in extra.items() if v not in (None, "")})
    return ctx


def render(template: str, ctx: dict, ascii_only: bool = False) -> list[str]:
    """Return sanitized path components. A template may contain '/' for subfolders."""
    template = (template or "{title}").replace("\\", "/")
    parts = []
    for raw in template.split("/"):
        text = _TOKEN_RE.sub(lambda m: _fmt(ctx.get(m.group(1)), m.group(2)), raw)
        # Drop separators left behind by empty tokens: "Title []" -> "Title".
        text = re.sub(r"\(\s*\)|\[\s*\]|\{\s*\}", "", text)
        text = re.sub(r"^\s*[-_.·,]+\s*|\s*[-_·,]+\s*$", "", text)
        text = re.sub(r"\s{2,}", " ", text).strip()
        if text:
            parts.append(sanitize_component(text, ascii_only))
    if not parts:
        parts = [sanitize_component(ctx.get("title") or ctx.get("id") or "untitled", ascii_only)]
    return parts


def subfolder_parts(rule: str, ctx: dict, ascii_only: bool) -> list[str]:
    value = {
        "site": ctx.get("site"),
        "uploader": ctx.get("uploader"),
        "playlist": ctx.get("playlist"),
        "date": ctx.get("today"),
    }.get(rule)
    return [sanitize_component(value, ascii_only, 80)] if value else []


def build_output_path(out_dir: Path, template: str, subfolder: str, ctx: dict, ext: str,
                      ascii_only: bool = False) -> Path:
    parts = subfolder_parts(subfolder, ctx, ascii_only) + render(template, ctx, ascii_only)
    directory = out_dir.joinpath(*parts[:-1])
    stem = parts[-1]
    return fit_path(directory, stem, ext)


def fit_path(directory: Path, stem: str, ext: str, reserve: int = 8) -> Path:
    """Shorten the stem so the full path stays under MAX_PATH with room for ' (12)'."""
    ext = ext if not ext or ext.startswith(".") else "." + ext
    budget = MAX_PATH_SAFE - len(str(directory)) - 1 - len(ext) - reserve
    if budget < 16:
        budget = 16
    if len(stem) > budget:
        stem = stem[:budget].rstrip(" .-_,")
    return directory / (stem + ext)


def unique_path(path: Path, taken: set[str] | None = None) -> Path:
    """'Name.mp4' -> 'Name (2).mp4' while the name exists on disk or is reserved."""
    taken = taken or set()

    def busy(p: Path) -> bool:
        return p.exists() or os.path.normcase(str(p)) in taken

    if not busy(path):
        return path
    stem, ext = path.stem, path.suffix
    for i in range(2, 10000):
        candidate = path.with_name(f"{stem} ({i}){ext}")
        if not busy(candidate):
            return candidate
    raise FileExistsError(str(path))
