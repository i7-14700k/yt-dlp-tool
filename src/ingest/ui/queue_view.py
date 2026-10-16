"""The queue list: one painted row per job, actions on hover, finished files drag into Premiere."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import (QAbstractListModel, QMimeData, QModelIndex, QPoint, QPointF, QRect, QRectF, QSize, Qt,
                            QTimer, QUrl, Signal)
from PySide6.QtGui import QColor, QDrag, QFontMetricsF, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import QAbstractItemView, QListView, QStyle, QStyledItemDelegate, QStyleOptionViewItem

from ..core import conform
from ..core.jobs import Job, Stage, Status
from ..core.util import domain_of, fps_label, human_bytes, human_duration, human_eta, short_res
from . import icons, theme as T
from .kit import rounded
from .thumbs import ThumbCache, draw_thumb

ROW_H = 98
THUMB = QSize(128, 72)
DL_SHARE = 0.72


class QueueModel(QAbstractListModel):
    JobRole = Qt.UserRole + 1

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.ids: list[str] = []
        self.jobs: dict[str, Job] = {}
        self._dirty: set[str] = set()
        self._timer = QTimer(self)
        self._timer.setInterval(50)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._flush)

    def rowCount(self, parent=QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self.ids)

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole):
        if not index.isValid() or index.row() >= len(self.ids):
            return None
        job = self.jobs.get(self.ids[index.row()])
        if role == self.JobRole:
            return job
        if role == Qt.DisplayRole:
            return job.title or job.url if job else ""
        if role == Qt.ToolTipRole and job is not None:
            return job.webpage_url or job.url
        return None

    def flags(self, index: QModelIndex):
        base = super().flags(index)
        if not index.isValid():
            return base
        return base | Qt.ItemIsDragEnabled | Qt.ItemIsSelectable | Qt.ItemIsEnabled

    def job(self, row: int) -> Job | None:
        if 0 <= row < len(self.ids):
            return self.jobs.get(self.ids[row])
        return None

    def row_of(self, job_id: str) -> int:
        try:
            return self.ids.index(job_id)
        except ValueError:
            return -1

    def add(self, jobs: list[Job], index: int) -> None:
        jobs = [j for j in jobs if j.id not in self.jobs]
        if not jobs:
            return
        index = max(0, min(index, len(self.ids)))
        self.beginInsertRows(QModelIndex(), index, index + len(jobs) - 1)
        for k, j in enumerate(jobs):
            self.ids.insert(index + k, j.id)
            self.jobs[j.id] = j
        self.endInsertRows()

    def remove(self, ids: list[str]) -> None:
        for jid in ids:
            row = self.row_of(jid)
            if row < 0:
                continue
            self.beginRemoveRows(QModelIndex(), row, row)
            self.ids.pop(row)
            self.jobs.pop(jid, None)
            self.endRemoveRows()

    def move(self, job_id: str, to: int) -> None:
        row = self.row_of(job_id)
        if row < 0:
            return
        to = max(0, min(to, len(self.ids) - 1))
        if to == row:
            return
        dest = to + 1 if to > row else to
        self.beginMoveRows(QModelIndex(), row, row, QModelIndex(), dest)
        self.ids.insert(to, self.ids.pop(row))
        self.endMoveRows()

    def touch(self, job_id: str) -> None:
        self._dirty.add(job_id)
        if not self._timer.isActive():
            self._timer.start()

    def _flush(self) -> None:
        rows = sorted(self.row_of(j) for j in self._dirty)
        self._dirty.clear()
        rows = [r for r in rows if r >= 0]
        if not rows:
            return
        # Emit contiguous ranges to keep repaints minimal.
        start = prev = rows[0]
        for r in rows[1:] + [None]:
            if r is None or r != prev + 1:
                self.dataChanged.emit(self.index(start), self.index(prev), [self.JobRole])
                if r is not None:
                    start = r
            if r is not None:
                prev = r


def _actions_for(job: Job) -> list[tuple[str, str, str]]:
    """(action, icon, tooltip)"""
    st = job.status
    if st == Status.ACTIVE.value:
        if job.is_live and job.stage == Stage.DOWNLOAD.value:
            return [("finish", "stop", "Stop recording and keep it"), ("cancel", "close", "Cancel")]
        return [("pause", "pause", "Pause  (Space)"), ("cancel", "close", "Cancel")]
    if st == Status.QUEUED.value:
        return [("pause", "pause", "Hold"), ("remove", "close", "Remove")]
    if st == Status.PAUSED.value:
        return [("resume", "play", "Resume  (Space)"), ("cancel", "close", "Cancel")]
    if st == Status.FAILED.value:
        return [("retry", "retry", "Retry"), ("log", "log", "Show log"), ("remove", "close", "Remove")]
    if st == Status.CANCELED.value:
        return [("retry", "retry", "Download again"), ("remove", "close", "Remove")]
    return [("reveal", "folder", "Show in Explorer"), ("more", "more", "More")]


def source_spec(job: Job) -> str:
    sel = job.selection or {}
    parts = []
    codec = sel.get("vcodec")
    if codec:
        parts.append(conform.codec_name(codec))
    h, w = sel.get("height"), sel.get("width")
    if h:
        res = short_res(h, w)
        fps = fps_label(sel.get("fps")) if sel.get("fps") else ""
        parts.append(f"{res} {fps}" if fps else res)
    hdr = sel.get("hdr")
    if hdr and hdr not in ("SDR", ""):
        parts.append(hdr)
    if not codec and sel.get("acodec"):
        parts.append(conform.codec_name(sel["acodec"]) + " audio")
    return " ".join(parts)


def output_spec(job: Job) -> str:
    o = job.out_props or {}
    parts = []
    if o.get("vcodec"):
        name = {"h264": "H.264", "hevc": "HEVC", "prores": "ProRes", "dnxhd": "DNxHR"}.get(o["vcodec"], o["vcodec"])
        res = short_res(o.get("height"), o.get("width")) if o.get("height") else ""
        fps = ""
        rate = o.get("rate")
        if rate:
            try:
                if isinstance(rate, str) and "/" in rate:
                    a, b = rate.split("/")
                    fps = fps_label(float(a) / float(b))
                else:
                    fps = fps_label(float(rate))
            except (ValueError, ZeroDivisionError):
                fps = ""
        parts.append(" ".join(x for x in (name, res, fps) if x))
    if o.get("acodec"):
        a = o["acodec"]
        name = {"aac": "AAC", "mp3": "MP3", "pcm_s16le": "PCM 16-bit", "pcm_s24le": "PCM 24-bit"}.get(a, a.upper())
        sr = o.get("sample_rate")
        parts.append(f"{name} {sr / 1000:g} kHz" if sr else name)
    if o.get("size"):
        parts.append(human_bytes(o["size"]))
    return " · ".join(parts)


class QueueDelegate(QStyledItemDelegate):
    def __init__(self, view: "QueueView", thumbs: ThumbCache) -> None:
        super().__init__(view)
        self.view = view
        self.thumbs = thumbs
        self.f_title = T.font(14, 500)
        self.f_meta = T.font(12, 400)
        self.f_status = T.font(12, 500)
        self.f_pct = T.font(16, 500, tabular=True)
        self.f_small = T.font(12, 400, tabular=True)
        self.f_mono = T.font(11.5, 400, mono=True)
        self.f_badge = T.font(10.5, 400, mono=True)
        self.phase = 0.0

    def sizeHint(self, option, index) -> QSize:  # noqa: N802
        return QSize(option.rect.width(), ROW_H)

    # geometry ---------------------------------------------------------------
    @staticmethod
    def content_rect(rect: QRect) -> QRectF:
        return QRectF(rect).adjusted(0, 3, -2, -3)

    def action_rects(self, rect: QRect, job: Job) -> list[tuple[str, str, str, QRectF]]:
        c = self.content_rect(rect)
        acts = _actions_for(job)
        out = []
        x = c.right() - 12 - 28
        y = c.center().y() - 14
        for name, ic, tip in reversed(acts):
            out.append((name, ic, tip, QRectF(x, y, 28, 28)))
            x -= 32
        return list(reversed(out))

    # painting ---------------------------------------------------------------
    def paint(self, p: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        job: Job = index.data(QueueModel.JobRole)
        if job is None:
            return
        p.save()
        p.setRenderHint(QPainter.Antialiasing)
        rect = option.rect
        c = self.content_rect(rect)
        selected = bool(option.state & QStyle.State_Selected)
        hovered = self.view.hover_row == index.row()
        if selected:
            rounded(p, c, T.R_LG, T.S2)
        elif hovered:
            rounded(p, c, T.R_LG, T.S1)

        # thumbnail
        tr = QRectF(c.left() + 12, c.center().y() - THUMB.height() / 2, THUMB.width(), THUMB.height())
        pm = self.thumbs.get(job.thumbnail) if job.thumbnail else None
        draw_thumb(p, tr, pm, T.R_SM, T.S2 if not selected else T.S3)
        if job.duration:
            self._badge(p, tr, human_duration(job.duration))
        if job.is_live:
            self._badge(p, tr, "LIVE", left=True)

        right_w = 150
        x = tr.right() + 16
        text_w = c.right() - x - right_w - 8
        fm = QFontMetricsF(self.f_title)

        # line 1: title
        title = job.title or job.url
        p.setFont(self.f_title)
        p.setPen(T.FG if job.status != Status.CANCELED.value else T.FG3)
        p.drawText(QRectF(x, c.top() + 13, text_w, 20), Qt.AlignLeft | Qt.AlignVCenter,
                   fm.elidedText(title, Qt.ElideRight, text_w))

        # line 2: where it's from and what it is
        meta = [job.site or domain_of(job.webpage_url or job.url)]
        if job.uploader:
            meta.append(job.uploader)
        if job.playlist.get("index"):
            meta.append(f"{job.playlist['index']} of {job.playlist.get('count', '?')}")
        spec = source_spec(job)
        if spec:
            meta.append(spec)
        p.setFont(self.f_meta)
        p.setPen(T.FG3)
        fm2 = QFontMetricsF(self.f_meta)
        p.drawText(QRectF(x, c.top() + 35, text_w, 16), Qt.AlignLeft | Qt.AlignVCenter,
                   fm2.elidedText("  ·  ".join(m for m in meta if m), Qt.ElideRight, text_w))

        # line 3: status
        self._status_line(p, job, QRectF(x, c.top() + 55, text_w, 16))

        # progress bar or verification line
        bar = QRectF(x, c.top() + 79, c.right() - x - 14, 3)
        self._bar(p, job, bar)

        # right column
        rc = QRectF(c.right() - right_w - 12, c.top() + 10, right_w, c.height() - 20)
        if hovered or job.status == Status.FAILED.value:
            self._actions(p, rect, job, hot=hovered)
        else:
            self._right(p, job, rc)
        p.restore()

    def _badge(self, p: QPainter, tr: QRectF, text: str, left: bool = False) -> None:
        p.setFont(self.f_badge)
        w = T.text_width(self.f_badge, text) + 8
        r = QRectF(tr.left() + 4 if left else tr.right() - w - 4, tr.bottom() - 18, w, 14)
        rounded(p, r, 3, QColor(0, 0, 0, 200))
        p.setPen(T.FG)
        p.drawText(r, Qt.AlignCenter, text)

    def _status_line(self, p: QPainter, job: Job, r: QRectF) -> None:
        st = job.status
        main, rest, main_color = "", "", T.FG
        if st == Status.QUEUED.value:
            main, main_color = "Waiting", T.FG3
            rest = conform.PROFILES.get(job.opts.profile, conform.PROFILES["h264"]).label
        elif st == Status.ACTIVE.value:
            main = job.stage_text or "Working"
            if job.stage == Stage.DOWNLOAD.value:
                parts = []
                if job.total:
                    parts.append(f"{human_bytes(job.downloaded)} of {human_bytes(job.total)}")
                if job.speed:
                    parts.append(human_bytes(job.speed) + "/s")
                rest = "  ·  ".join(parts)
            elif job.stage == Stage.CONVERT.value:
                bits = []
                if job.cv_speed:
                    bits.append(f"{job.cv_speed:.1f}× realtime")
                if job.plan_reasons:
                    bits.append(job.plan_reasons[0])
                rest = "  ·  ".join(bits)
            elif job.stage == Stage.CONVERT_WAIT.value:
                main, main_color = "Downloaded", T.FG
                rest = "waiting for a conversion slot"
        elif st == Status.PAUSED.value:
            main, main_color = "Paused", T.FG2
            if job.total and job.stage in ("", Stage.DOWNLOAD.value, Stage.RESOLVE.value):
                rest = f"{human_bytes(job.downloaded)} of {human_bytes(job.total)}"
            elif job.stage == Stage.CONVERT.value:
                rest = "converting, picks up where it stopped"
        elif st == Status.FAILED.value:
            err = job.error or {}
            main = err.get("title") or "Failed"
            rest = err.get("hint") or ""
        elif st == Status.DONE.value:
            if job.skipped:
                main, rest = "Already downloaded", job.stage_text if job.stage_text != "Done" else ""
            else:
                main = "Ready"
                rest = output_spec(job) or job.plan_summary
        elif st == Status.CANCELED.value:
            main, main_color = "Canceled", T.FG3
        p.setFont(self.f_status)
        fm = QFontMetricsF(self.f_status)
        main = fm.elidedText(main, Qt.ElideRight, r.width())
        mw = fm.horizontalAdvance(main)
        p.setPen(main_color)
        p.drawText(QRectF(r.left(), r.top(), mw + 1, r.height()), Qt.AlignLeft | Qt.AlignVCenter, main)
        if rest and mw + 24 < r.width():
            p.setFont(self.f_meta)
            p.setPen(T.FG2)
            fm2 = QFontMetricsF(self.f_meta)
            avail = r.width() - mw - 12
            p.drawText(QRectF(r.left() + mw + 12, r.top(), avail, r.height()), Qt.AlignLeft | Qt.AlignVCenter,
                       fm2.elidedText(rest, Qt.ElideRight, avail))

    @staticmethod
    def _stage_pct(job: Job) -> float:
        if job.stage in (Stage.CONVERT.value, Stage.VERIFY.value, Stage.FINALIZE.value):
            return job.cv_progress * 100
        return job.dl_progress * 100

    def _bar(self, p: QPainter, job: Job, r: QRectF) -> None:
        st = job.status
        if st == Status.DONE.value:
            return
        gap = 3
        a = QRectF(r.left(), r.top(), r.width() * DL_SHARE - gap / 2, r.height())
        b = QRectF(a.right() + gap, r.top(), r.right() - a.right() - gap, r.height())
        track = T.S3
        rounded(p, a, 1.5, track)
        rounded(p, b, 1.5, track)
        if st == Status.QUEUED.value or st == Status.CANCELED.value:
            return
        fill = T.FG if st == Status.ACTIVE.value else T.FG3
        if st == Status.FAILED.value:
            self._hatch(p, a if job.stage in ("", Stage.RESOLVE.value, Stage.DOWNLOAD.value) else b)
            if job.stage not in ("", Stage.RESOLVE.value, Stage.DOWNLOAD.value):
                rounded(p, a, 1.5, T.FG3)
            return
        indeterminate = st == Status.ACTIVE.value and (
            job.stage == Stage.RESOLVE.value or job.stage == Stage.CONVERT_WAIT.value or
            (job.stage == Stage.DOWNLOAD.value and job.dl_progress <= 0 and not job.total) or job.is_live)
        if job.stage in (Stage.RESOLVE.value, Stage.DOWNLOAD.value, ""):
            seg, frac = a, job.dl_progress
        else:
            rounded(p, a, 1.5, fill if st == Status.ACTIVE.value else T.FG3)
            seg, frac = b, job.cv_progress if job.stage == Stage.CONVERT.value else (
                1.0 if job.stage in (Stage.VERIFY.value, Stage.FINALIZE.value) else 0.0)
        if indeterminate and job.stage != Stage.CONVERT_WAIT.value:
            self._sweep(p, seg)
        elif job.stage == Stage.CONVERT_WAIT.value:
            pass
        elif frac > 0:
            rounded(p, QRectF(seg.left(), seg.top(), max(3.0, seg.width() * min(1.0, frac)), seg.height()), 1.5,
                    fill)

    def _sweep(self, p: QPainter, seg: QRectF) -> None:
        w = seg.width() * 0.28
        x = seg.left() + (seg.width() + w) * (self.phase % 1.0) - w
        p.save()
        path = QPainterPath()
        path.addRoundedRect(seg, 1.5, 1.5)
        p.setClipPath(path)
        rounded(p, QRectF(x, seg.top(), w, seg.height()), 1.5, T.FG2)
        p.restore()

    def _hatch(self, p: QPainter, seg: QRectF) -> None:
        p.save()
        path = QPainterPath()
        path.addRoundedRect(seg, 1.5, 1.5)
        p.setClipPath(path)
        pen = QPen(T.FG2, 1.4)
        p.setPen(pen)
        x = seg.left() - 8
        while x < seg.right() + 8:
            p.drawLine(QPointF(x, seg.bottom() + 1), QPointF(x + 4, seg.top() - 1))
            x += 5
        p.restore()

    def _right(self, p: QPainter, job: Job, rc: QRectF) -> None:
        st = job.status
        top = QRectF(rc.left(), rc.top() + 2, rc.width(), 22)
        second = QRectF(rc.left(), rc.top() + 26, rc.width(), 16)
        if st == Status.ACTIVE.value and job.stage not in (Stage.RESOLVE.value, Stage.CONVERT_WAIT.value) \
                and not job.is_live:
            self._percent(p, top, self._stage_pct(job), T.FG)
            eta = human_eta(job.eta)
            if eta:
                p.setFont(self.f_small)
                p.setPen(T.FG3)
                p.drawText(second, Qt.AlignRight | Qt.AlignVCenter, eta)
        elif st == Status.PAUSED.value:
            self._percent(p, top, self._stage_pct(job), T.FG3)
        elif st == Status.DONE.value:
            rep = job.report or {}
            if rep:
                self._compat(p, top, rep)
            if job.outputs and len(job.outputs) > 1:
                p.setFont(self.f_small)
                p.setPen(T.FG3)
                p.drawText(second, Qt.AlignRight | Qt.AlignVCenter, f"{len(job.outputs)} files")
        elif st == Status.ACTIVE.value and job.is_live and job.downloaded:
            p.setFont(self.f_pct)
            p.setPen(T.FG)
            p.drawText(top, Qt.AlignRight | Qt.AlignVCenter, human_bytes(job.downloaded))

    def _percent(self, p: QPainter, r: QRectF, pct: float, color: QColor) -> None:
        num = f"{pct:.0f}"
        fm = QFontMetricsF(self.f_pct)
        fs = QFontMetricsF(self.f_small)
        sign_w = fs.horizontalAdvance("%") + 1
        base = r.top() + (r.height() + fm.ascent() - fm.descent()) / 2
        p.setFont(self.f_small)
        p.setPen(T.FG3 if color == T.FG else T.FG4)
        p.drawText(QPointF(r.right() - sign_w + 1, base), "%")
        p.setFont(self.f_pct)
        p.setPen(color)
        p.drawText(QPointF(r.right() - sign_w - fm.horizontalAdvance(num) - 1, base), num)

    def _compat(self, p: QPainter, r: QRectF, rep: dict) -> None:
        p.setFont(self.f_mono)
        items = [("Pr", rep.get("premiere")), ("Ae", rep.get("after_effects"))]
        x = r.right()
        for name, ok in reversed(items):
            mark = "✓" if ok else "✕"
            text = f"{name} {mark}"
            w = T.text_width(self.f_mono, text)
            p.setPen(T.FG if ok else T.FG3)
            p.drawText(QRectF(x - w, r.top(), w + 1, r.height()), Qt.AlignRight | Qt.AlignVCenter, text)
            x -= w + 14

    def _actions(self, p: QPainter, rect: QRect, job: Job, hot: bool = True) -> None:
        pos = self.view.hover_pos if hot else None
        for name, ic, _tip, r in self.action_rects(rect, job):
            over = pos is not None and r.contains(QPointF(pos))
            if hot:
                rounded(p, r, T.R_MD, T.S4 if over else T.S3)
            icons.draw(p, ic, QRectF(r.center().x() - 8, r.center().y() - 8, 16, 16), T.FG if hot else T.FG2)


class QueueView(QListView):
    action = Signal(str, str)          # job id, action
    opened = Signal(str)
    context = Signal(list, QPoint)     # job ids, global pos
    remove_many = Signal(list)         # job ids (Delete key): confirmed once for the batch

    def __init__(self, model: QueueModel, thumbs: ThumbCache, parent=None) -> None:
        super().__init__(parent)
        self.setModel(model)
        self.qmodel = model
        self.delegate = QueueDelegate(self, thumbs)
        self.setItemDelegate(self.delegate)
        self.setMouseTracking(True)
        self.setUniformItemSizes(True)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.verticalScrollBar().setSingleStep(24)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setDragEnabled(True)
        self.setDragDropMode(QAbstractItemView.DragOnly)
        self.setFrameShape(QListView.NoFrame)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._context)
        self.hover_row = -1
        self.hover_pos: QPoint | None = None
        self.empty_lines: list[tuple[str, str]] = []
        thumbs.loaded.connect(lambda _u: self.viewport().update())
        self._anim = QTimer(self)
        self._anim.setInterval(33)
        self._anim.timeout.connect(self._tick)
        self._anim.start()

    def selected_ids(self) -> list[str]:
        rows = sorted(i.row() for i in self.selectionModel().selectedRows())
        return [self.qmodel.ids[r] for r in rows if 0 <= r < len(self.qmodel.ids)]

    def _tick(self) -> None:
        # Only rows with an indeterminate sweep need frames.
        animate = False
        top = self.indexAt(QPoint(0, 0)).row()
        if top < 0:
            top = 0
        bottom = self.indexAt(QPoint(0, self.viewport().height() - 1)).row()
        if bottom < 0:
            bottom = self.qmodel.rowCount() - 1
        for row in range(top, bottom + 1):
            job = self.qmodel.job(row)
            if job is not None and job.status == Status.ACTIVE.value and (
                    job.stage == Stage.RESOLVE.value or job.is_live or
                    (job.stage == Stage.DOWNLOAD.value and job.dl_progress <= 0 and not job.total)):
                animate = True
                self.viewport().update(self.visualRect(self.qmodel.index(row)))
        if animate:
            self.delegate.phase = (self.delegate.phase + 0.012) % 1.0

    def mouseMoveEvent(self, e) -> None:  # noqa: N802
        idx = self.indexAt(e.position().toPoint())
        row = idx.row() if idx.isValid() else -1
        old = self.hover_row
        self.hover_row = row
        self.hover_pos = e.position().toPoint()
        if old != row and old >= 0:
            self.viewport().update(self.visualRect(self.qmodel.index(old)))
        if row >= 0:
            self.viewport().update(self.visualRect(idx))
            job = self.qmodel.job(row)
            tip = ""
            if job is not None:
                for _n, _i, t, r in self.delegate.action_rects(self.visualRect(idx), job):
                    if r.contains(QPointF(self.hover_pos)):
                        tip = t
            self.setCursor(Qt.PointingHandCursor if tip else Qt.ArrowCursor)
            if tip != self.toolTip():
                self.setToolTip(tip)
        super().mouseMoveEvent(e)

    def leaveEvent(self, e) -> None:  # noqa: N802
        old = self.hover_row
        self.hover_row = -1
        self.hover_pos = None
        if old >= 0:
            self.viewport().update(self.visualRect(self.qmodel.index(old)))
        super().leaveEvent(e)

    def _hit_action(self, pos: QPoint) -> tuple[str, str] | None:
        idx = self.indexAt(pos)
        if not idx.isValid():
            return None
        job = self.qmodel.job(idx.row())
        if job is None:
            return None
        for name, _i, _t, r in self.delegate.action_rects(self.visualRect(idx), job):
            if r.contains(QPointF(pos)):
                return job.id, name
        return None

    def mousePressEvent(self, e) -> None:  # noqa: N802
        if e.button() == Qt.LeftButton and self._hit_action(e.position().toPoint()):
            self._pressed_action = self._hit_action(e.position().toPoint())
            return
        self._pressed_action = None
        super().mousePressEvent(e)

    def mouseReleaseEvent(self, e) -> None:  # noqa: N802
        hit = self._hit_action(e.position().toPoint()) if e.button() == Qt.LeftButton else None
        pressed = getattr(self, "_pressed_action", None)
        if pressed is not None:
            self._pressed_action = None
            if hit == pressed:
                if hit[1] == "more":
                    self.context.emit([hit[0]], self.viewport().mapToGlobal(e.position().toPoint()))
                else:
                    self.action.emit(*hit)
            return
        super().mouseReleaseEvent(e)

    def mouseDoubleClickEvent(self, e) -> None:  # noqa: N802
        if self._hit_action(e.position().toPoint()):
            return
        idx = self.indexAt(e.position().toPoint())
        if idx.isValid():
            job = self.qmodel.job(idx.row())
            if job is not None:
                self.opened.emit(job.id)
                return
        super().mouseDoubleClickEvent(e)

    def keyPressEvent(self, e) -> None:  # noqa: N802
        ids = self.selected_ids()
        if ids and e.key() in (Qt.Key_Delete, Qt.Key_Backspace):
            self.remove_many.emit(ids)
            return
        if ids and e.key() == Qt.Key_Space:
            for jid in ids:
                job = self.qmodel.jobs.get(jid)
                if job is None:
                    continue
                if job.status in (Status.ACTIVE.value, Status.QUEUED.value):
                    self.action.emit(jid, "pause")
                elif job.status in (Status.PAUSED.value, Status.FAILED.value):
                    self.action.emit(jid, "resume")
            return
        if ids and e.key() in (Qt.Key_Return, Qt.Key_Enter):
            self.opened.emit(ids[0])
            return
        super().keyPressEvent(e)

    def _context(self, pos: QPoint) -> None:
        idx = self.indexAt(pos)
        if not idx.isValid():
            return
        if not self.selectionModel().isSelected(idx):
            self.setCurrentIndex(idx)
        self.context.emit(self.selected_ids(), self.viewport().mapToGlobal(pos))

    def startDrag(self, actions) -> None:  # noqa: N802
        files = []
        for jid in self.selected_ids():
            job = self.qmodel.jobs.get(jid)
            if job is not None and job.done:
                files += [f for f in job.outputs if Path(f).exists()]
        if not files:
            return
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(f) for f in files])
        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.setPixmap(self._drag_pixmap(len(files)))
        drag.setHotSpot(QPoint(16, 16))
        drag.exec(Qt.CopyAction, Qt.CopyAction)

    def _drag_pixmap(self, n: int) -> QPixmap:
        f = T.font(13, 600)
        text = f"{n} file{'s' if n != 1 else ''}"
        w = int(T.text_width(f, text) + 28)
        dpr = self.devicePixelRatioF()
        pm = QPixmap(int(w * dpr), int(32 * dpr))
        pm.setDevicePixelRatio(dpr)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        rounded(p, QRectF(0, 0, w, 32), T.R_MD, T.FG)
        p.setFont(f)
        p.setPen(T.BG)
        p.drawText(QRectF(0, 0, w, 32), Qt.AlignCenter, text)
        p.end()
        return pm

    def paintEvent(self, e) -> None:  # noqa: N802
        if self.qmodel.rowCount() == 0 and self.empty_lines:
            p = QPainter(self.viewport())
            p.setRenderHint(QPainter.Antialiasing)
            y = 28.0
            x = 14.0
            for kind, text in self.empty_lines:
                if kind == "title":
                    p.setFont(T.font(16, 600))
                    p.setPen(T.FG)
                    p.drawText(QRectF(x, y, 600, 24), Qt.AlignLeft | Qt.AlignVCenter, text)
                    y += 30
                elif kind == "body":
                    p.setFont(T.font(13, 400))
                    p.setPen(T.FG2)
                    p.drawText(QRectF(x, y, min(560.0, self.viewport().width() - 28), 40),
                               Qt.AlignLeft | Qt.TextWordWrap, text)
                    y += 46
                elif kind == "key":
                    key, _, desc = text.partition("\t")
                    p.setFont(T.font(12, 400, mono=True))
                    kw = T.text_width(p.font(), key) + 14
                    rounded(p, QRectF(x, y + 1, kw, 22), T.R_SM, T.S2)
                    p.setPen(T.FG)
                    p.drawText(QRectF(x, y + 1, kw, 22), Qt.AlignCenter, key)
                    p.setFont(T.font(13, 400))
                    p.setPen(T.FG2)
                    p.drawText(QRectF(x + 132, y, 520, 24), Qt.AlignLeft | Qt.AlignVCenter, desc)
                    y += 32
                elif kind == "gap":
                    y += 12
            p.end()
            return
        super().paintEvent(e)
