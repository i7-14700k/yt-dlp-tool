"""Thumbnail loading with a disk cache. Network requests honour the proxy setting."""

from __future__ import annotations

import hashlib
import queue
import threading
from pathlib import Path

from PySide6.QtCore import QObject, QRectF, QSize, Qt, QUrl, Signal
from PySide6.QtGui import QImage, QImageReader, QPainter, QPainterPath, QPixmap
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkProxy, QNetworkReply, QNetworkRequest

from ..core import paths
from ..core.util import normalize_proxy

THUMB_W, THUMB_H = 320, 180


def _key(url: str) -> str:
    return hashlib.sha1(url.encode("utf-8", "ignore")).hexdigest()


def cover(img: QImage, w: int, h: int) -> QImage:
    """Scale to fill w×h and crop the overflow, like CSS object-fit: cover."""
    if img.isNull():
        return img
    scaled = img.scaled(w, h, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
    x = max(0, (scaled.width() - w) // 2)
    y = max(0, (scaled.height() - h) // 2)
    return scaled.copy(x, y, w, h)


def _read_local(url: str) -> QImage:
    """Decode a picture on disk at thumbnail size. Runs on a worker thread."""
    try:
        if not Path(url).is_file():
            return QImage()
    except OSError:
        return QImage()
    reader = QImageReader(url)
    reader.setAutoTransform(True)
    full = reader.size()
    if full.isValid() and full.width() > THUMB_W * 2 and full.height() > THUMB_H * 2:
        # Decode small: a 6000 px photo needn't be decoded at full size for a 112 px tile.
        k = max(THUMB_W * 2 / full.width(), THUMB_H * 2 / full.height())
        reader.setScaledSize(QSize(max(1, round(full.width() * k)), max(1, round(full.height() * k))))
    img = reader.read()
    return img if img.isNull() else cover(img, THUMB_W, THUMB_H)


class ThumbCache(QObject):
    loaded = Signal(str)
    _local_done = Signal(str, QImage)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.dir = paths.thumbs_dir()
        self.dir.mkdir(parents=True, exist_ok=True)
        self._mem: dict[str, QPixmap] = {}
        self._pending: set[str] = set()
        self._failed: set[str] = set()
        self._nam = QNetworkAccessManager(self)
        self._nam.setTransferTimeout(20000)
        # Pictures on the output drive load on two daemon threads, not in paint: a network share
        # can stall for seconds, and a daemon thread stuck on one never holds up quitting.
        self._local_q: queue.Queue[str] = queue.Queue()
        self._workers: list[threading.Thread] = []
        self._local_done.connect(self._on_local)

    def set_proxy(self, proxy_url: str) -> None:
        proxy_url = normalize_proxy(proxy_url)
        if not proxy_url:
            self._nam.setProxy(QNetworkProxy(QNetworkProxy.DefaultProxy))
            return
        u = QUrl(proxy_url)
        kind = QNetworkProxy.Socks5Proxy if u.scheme().startswith("socks") else QNetworkProxy.HttpProxy
        self._nam.setProxy(QNetworkProxy(kind, u.host(), u.port(8080), u.userName(), u.password()))

    def path_for(self, url: str) -> Path:
        return self.dir / f"{_key(url)}.jpg"

    def get(self, url: str) -> QPixmap | None:
        if not url:
            return None
        pm = self._mem.get(url)
        if pm is not None:
            return pm
        if url in self._failed:
            return None
        p = self.path_for(url)
        if p.is_file():
            img = QImage(str(p))
            if not img.isNull():
                pm = QPixmap.fromImage(img)
                self._remember(url, pm)
                return pm
        if url in self._pending:
            return None
        if url.startswith(("http://", "https://")):
            self._fetch(url)
        else:
            self._pending.add(url)
            if len(self._workers) < 2:
                t = threading.Thread(target=self._local_worker, name="thumbs", daemon=True)
                t.start()
                self._workers.append(t)
            self._local_q.put(url)
        return None

    def _local_worker(self) -> None:
        while True:
            url = self._local_q.get()
            try:
                img = _read_local(url)
            except Exception:
                img = QImage()
            self._local_done.emit(url, img)

    def _on_local(self, url: str, img: QImage) -> None:
        self._pending.discard(url)
        if img.isNull():
            self._failed.add(url)
            return
        self._remember(url, QPixmap.fromImage(img))
        self.loaded.emit(url)

    def _remember(self, url: str, pm: QPixmap) -> None:
        if len(self._mem) > 600:
            for k in list(self._mem)[:200]:
                self._mem.pop(k, None)
        self._mem[url] = pm

    def _fetch(self, url: str) -> None:
        self._pending.add(url)
        req = QNetworkRequest(QUrl(url))
        req.setAttribute(QNetworkRequest.RedirectPolicyAttribute, QNetworkRequest.NoLessSafeRedirectPolicy)
        req.setRawHeader(b"User-Agent", b"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                                        b"(KHTML, like Gecko) Chrome/141.0 Safari/537.36")
        reply = self._nam.get(req)
        reply.finished.connect(lambda r=reply, u=url: self._done(r, u))

    def _done(self, reply: QNetworkReply, url: str) -> None:
        self._pending.discard(url)
        data = bytes(reply.readAll()) if reply.error() == QNetworkReply.NoError else b""
        reply.deleteLater()
        img = QImage()
        if data:
            img.loadFromData(data)
        if img.isNull():
            self._failed.add(url)
            return
        img = cover(img, THUMB_W, THUMB_H)
        try:
            img.save(str(self.path_for(url)), "JPG", 88)
        except Exception:
            pass
        self._remember(url, QPixmap.fromImage(img))
        self.loaded.emit(url)


def draw_thumb(p: QPainter, rect: QRectF, pm: QPixmap | None, radius: float, placeholder) -> None:
    path = QPainterPath()
    path.addRoundedRect(rect, radius, radius)
    p.save()
    p.setClipPath(path)
    if pm is not None and not pm.isNull():
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)
        src = QRectF(0, 0, pm.width(), pm.height())
        # Fit by cover in case the cached image has another aspect.
        target_ratio = rect.width() / rect.height()
        src_ratio = src.width() / max(1.0, src.height())
        if src_ratio > target_ratio:
            w = src.height() * target_ratio
            src = QRectF((src.width() - w) / 2, 0, w, src.height())
        else:
            h = src.width() / target_ratio
            src = QRectF(0, (src.height() - h) / 2, src.width(), h)
        p.drawPixmap(rect, pm, src)
    else:
        p.fillRect(rect, placeholder)
    p.restore()


def size() -> QSize:
    return QSize(THUMB_W, THUMB_H)
