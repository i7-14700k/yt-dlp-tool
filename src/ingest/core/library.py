"""SQLite record of everything Ingest has finished, used for the Library page and to skip repeats."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import paths

_SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id TEXT PRIMARY KEY,
    url TEXT, webpage_url TEXT, extractor TEXT, video_id TEXT,
    title TEXT, uploader TEXT, site TEXT, duration REAL,
    profile TEXT, path TEXT, size INTEGER,
    width INTEGER, height INTEGER, fps REAL, vcodec TEXT, acodec TEXT,
    created REAL, thumb TEXT, thumb_url TEXT, report TEXT, summary TEXT, extra TEXT
);
CREATE INDEX IF NOT EXISTS items_vid ON items(extractor, video_id);
CREATE INDEX IF NOT EXISTS items_created ON items(created);
"""


@dataclass
class Item:
    id: str
    url: str = ""
    webpage_url: str = ""
    extractor: str = ""
    video_id: str = ""
    title: str = ""
    uploader: str = ""
    site: str = ""
    duration: float = 0.0
    profile: str = ""
    path: str = ""
    size: int = 0
    width: int = 0
    height: int = 0
    fps: float = 0.0
    vcodec: str = ""
    acodec: str = ""
    created: float = field(default_factory=time.time)
    thumb: str = ""
    thumb_url: str = ""
    report: dict | None = None
    summary: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def exists(self) -> bool:
        return bool(self.path) and Path(self.path).exists()


_COLS = [f for f in Item.__dataclass_fields__]


class Library:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or paths.data_dir() / "library.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(self.path), check_same_thread=False, timeout=10)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(_SCHEMA)
        self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def add(self, item: Item) -> None:
        row = []
        for c in _COLS:
            v = getattr(item, c)
            if c in ("report", "extra"):
                v = json.dumps(v) if v is not None else None
            row.append(v)
        with self._lock:
            self._db.execute(
                f"INSERT OR REPLACE INTO items ({','.join(_COLS)}) VALUES ({','.join('?' * len(_COLS))})", row)
            self._db.commit()

    def _row(self, r: tuple) -> Item:
        d = dict(zip(_COLS, r))
        for c in ("report", "extra"):
            try:
                d[c] = json.loads(d[c]) if d[c] else ({} if c == "extra" else None)
            except ValueError:
                d[c] = {} if c == "extra" else None
        for c in ("duration", "fps"):
            d[c] = d[c] or 0.0
        for c in ("size", "width", "height"):
            d[c] = d[c] or 0
        for c in _COLS:
            if d[c] is None and c not in ("report",):
                d[c] = "" if Item.__dataclass_fields__[c].type in ("str", str) else d[c]
        return Item(**d)

    def list(self, query: str = "", kind: str = "all", limit: int = 2000) -> list[Item]:
        sql = f"SELECT {','.join(_COLS)} FROM items"
        where, args = [], []
        if query:
            # Typed % and _ are searched for, not wildcards ("100%", "my_clip").
            q = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            cols = ("title", "uploader", "site", "path", "url")
            where.append("(" + " OR ".join(f"{c} LIKE ? ESCAPE '\\'" for c in cols) + ")")
            args += [f"%{q}%"] * len(cols)
        if kind == "video":
            where.append("profile IN ('h264','prores','dnxhr','hevc','original') AND IFNULL(vcodec,'') != ''")
        elif kind == "audio":
            where.append("(profile IN ('wav','aac','mp3') OR (IFNULL(vcodec,'') = '' AND profile != 'image'))")
        elif kind == "image":
            where.append("profile = 'image'")
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY created DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            rows = self._db.execute(sql, args).fetchall()
        return [self._row(r) for r in rows]

    def get(self, item_id: str) -> Item | None:
        with self._lock:
            r = self._db.execute(f"SELECT {','.join(_COLS)} FROM items WHERE id=?", (item_id,)).fetchone()
        return self._row(r) if r else None

    def find(self, extractor: str, video_id: str, profile: str | None = None) -> Item | None:
        if not extractor or not video_id:
            return None
        sql = f"SELECT {','.join(_COLS)} FROM items WHERE lower(extractor)=lower(?) AND video_id=?"
        args: list = [extractor, video_id]
        if profile:
            sql += " AND profile=?"
            args.append(profile)
        sql += " ORDER BY created DESC"
        with self._lock:
            rows = self._db.execute(sql, args).fetchall()
        for r in rows:
            it = self._row(r)
            if it.exists:
                return it
        return None

    def remove(self, item_id: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM items WHERE id=?", (item_id,))
            self._db.commit()

    def count(self) -> int:
        with self._lock:
            return int(self._db.execute("SELECT COUNT(*) FROM items").fetchone()[0])
