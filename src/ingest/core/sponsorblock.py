"""SponsorBlock segment lookup (YouTube only). Segments are cut during conversion."""

from __future__ import annotations

import hashlib
import json
import urllib.parse

from .engines import http_get

API = "https://sponsor.ajay.app/api/skipSegmentsHash/"

CATEGORIES = {
    "sponsor": "Sponsor",
    "selfpromo": "Self-promotion",
    "interaction": "Subscribe reminder",
    "intro": "Intro",
    "outro": "Endcards",
    "preview": "Preview",
    "music_offtopic": "Non-music in music videos",
    "filler": "Filler",
}


def segments(video_id: str, categories: list[str], duration: float, proxy: str = "") -> list[tuple[float, float]]:
    """Fetch, merge and clamp segments. Uses the hash-prefix endpoint for privacy."""
    if not video_id or not categories:
        return []
    prefix = hashlib.sha256(video_id.encode()).hexdigest()[:4]
    q = urllib.parse.urlencode({"categories": json.dumps(categories), "actionTypes": json.dumps(["skip"])})
    try:
        data = json.loads(http_get(f"{API}{prefix}?{q}", proxy, timeout=15).decode("utf-8"))
    except Exception as e:
        if "404" in str(e):
            return []  # no segments for any video with this prefix
        raise
    out: list[tuple[float, float]] = []
    for entry in data if isinstance(data, list) else []:
        if entry.get("videoID") != video_id:
            continue
        for seg in entry.get("segments") or []:
            try:
                a, b = (float(x) for x in seg["segment"])
            except (KeyError, TypeError, ValueError):
                continue
            if b - a >= 1.0:
                out.append((max(0.0, a), min(duration, b) if duration else b))
    out.sort()
    merged: list[tuple[float, float]] = []
    for a, b in out:
        if merged and a <= merged[-1][1] + 0.5:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        elif b > a:
            merged.append((a, b))
    return merged
