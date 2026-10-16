"""Library: everything Ingest has finished, searchable, draggable into Premiere."""

from __future__ import annotations

import threading
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QAbstractListModel, QMimeData, QModelIndex, QPoint, QPointF, QRectF, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDrag, QFontMetricsF, QPainter
from PySide6.QtWidgets import QAbstractItemView, QListView, QStyle, QStyledItemDelegate, QWidget

from ..core import conform
from ..core.library import Item, Library
from ..core.util import domain_of, fps_label, human_bytes, human_duration, short_res
from . import icons, theme as T
from .kit import Label, Segmented, TextField, hbox, rounded, vbox
from .thumbs import ThumbCache, draw_thumb

ROW_H = 80


class LibraryModel(QAbstractListModel):
    ItemRole = Qt.UserRole + 2
    _checked = Signal(int, dict)  # generation, item id -> file exists

    def __init__(self, library: Library, parent=None) -> None:
        super().__init__(parent)
        self.library = library
        self.items: list[Item] = []
        self._exists: dict[str, bool] = {}
        self._gen = 0
        self._checked.connect(self._on_checked)

    def reload(self, query: str = "", kind: str = "all") -> None:
        self.beginResetModel()
        self.items = self.library.list(query, kind)
        self._exists = {}
        self._gen += 1
        self.endResetModel()
        # Whether each file is still there, asked off the GUI thread: on a network share that's
        # offline every check can block for seconds.
        items, gen = [(it.id, it.path) for it in self.items], self._gen
        threading.Thread(target=self._check, args=(gen, items), name="library-exists", daemon=True).start()

    def _check(self, gen: int, items: list[tuple[str, str]]) -> None:
        batch: dict[str, bool] = {}
        for n, (item_id, path) in enumerate(items, 1):
            if gen != self._gen:
                return
            try:
                batch[item_id] = bool(path) and Path(path).exists()
            except OSError:
                batch[item_id] = False
            if len(batch) >= 40 or n == len(items):
                self._checked.emit(gen, batch)
                batch = {}

    def _on_checked(self, gen: int, found: dict) -> None:
        if gen != self._gen:
            return
        self._exists.update(found)
        if self.items:
            self.dataChanged.emit(self.index(0), self.index(len(self.items) - 1))

    def rowCount(self, parent=QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self.items)

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid() or index.row() >= len(self.items):
            return None
        it = self.items[index.row()]
        if role == self.ItemRole:
            return it
        if role == Qt.DisplayRole:
            return it.title
        if role == Qt.ToolTipRole:
            return it.path
        return None

    def flags(self, index):
        return super().flags(index) | Qt.ItemIsDragEnabled

    def exists(self, it: Item) -> bool:
        return self._exists.get(it.id, True)  # shown as present until the check says otherwise


def _when(ts: float) -> str:
    d = datetime.fromtimestamp(ts)
    now = datetime.now()
    if d.date() == now.date():
        return "Today " + d.strftime("%H:%M")
    if (now.date() - d.date()).days == 1:
        return "Yesterday " + d.strftime("%H:%M")
    if d.year == now.year:
        return d.strftime("%b %d").replace(" 0", " ")
    return d.strftime("%Y-%m-%d")


class LibraryDelegate(QStyledItemDelegate):
    def __init__(self, view: "LibraryView", thumbs: ThumbCache) -> None:
        super().__init__(view)
        self.view = view
        self.thumbs = thumbs
        self.f_title = T.font(14, 500)
        self.f_meta = T.font(12, 400)
        self.f_spec = T.font(11.5, 400, mono=True)
        self.f_small = T.font(12, 400, tabular=True)

    def sizeHint(self, option, index) -> QSize:  # noqa: N802
        return QSize(option.rect.width(), ROW_H)

    def action_rects(self, rect) -> list[tuple[str, str, QRectF]]:
        c = QRectF(rect).adjusted(0, 3, -2, -3)
        y = c.center().y() - 14
        return [("reveal", "folder", QRectF(c.right() - 12 - 60, y, 28, 28)),
                ("more", "more", QRectF(c.right() - 12 - 28, y, 28, 28))]

    def paint(self, p: QPainter, option, index) -> None:
        it: Item = index.data(LibraryModel.ItemRole)
        if it is None:
            return
        model: LibraryModel = index.model()
        exists = model.exists(it)
        p.save()
        p.setRenderHint(QPainter.Antialiasing)
        c = QRectF(option.rect).adjusted(0, 3, -2, -3)
        hovered = self.view.hover_row == index.row()
        if option.state & QStyle.State_Selected:
            rounded(p, c, T.R_LG, T.S2)
        elif hovered:
            rounded(p, c, T.R_LG, T.S1)
        tr = QRectF(c.left() + 12, c.center().y() - 31.5, 112, 63)
        url = it.path if it.profile == "image" and exists else (it.thumb_url or "")
        draw_thumb(p, tr, self.thumbs.get(url) if url else None, T.R_SM, T.S2)
        if it.duration:
            p.setFont(T.font(10.5, 400, mono=True))
            text = human_duration(it.duration)
            w = T.text_width(p.font(), text) + 8
            r = QRectF(tr.right() - w - 4, tr.bottom() - 18, w, 14)
            rounded(p, r, 3, T.BG)
            p.setPen(T.FG)
            p.drawText(r, Qt.AlignCenter, text)
        x = tr.right() + 16
        right_w = 170
        tw = c.right() - x - right_w
        p.setFont(self.f_title)
        p.setPen(T.FG if exists else T.FG3)
        p.drawText(QRectF(x, c.top() + 12, tw, 20), Qt.AlignLeft | Qt.AlignVCenter,
                   QFontMetricsF(self.f_title).elidedText(it.title or Path(it.path).stem, Qt.ElideRight, tw))
        meta = [it.site or domain_of(it.webpage_url or it.url), it.uploader, _when(it.created)]
        p.setFont(self.f_meta)
        p.setPen(T.FG3)
        p.drawText(QRectF(x, c.top() + 33, tw, 16), Qt.AlignLeft | Qt.AlignVCenter,
                   QFontMetricsF(self.f_meta).elidedText("  ·  ".join(m for m in meta if m), Qt.ElideRight, tw))
        if exists:
            spec = []
            prof = conform.PROFILES.get(it.profile)
            name = {"h264": "H.264", "hevc": "HEVC", "prores": "ProRes", "dnxhd": "DNxHR"}.get(it.vcodec, "")
            if it.profile == "image":
                dims = f"{it.width}×{it.height}" if it.width and it.height else ""
                spec.append(" ".join(x for x in (Path(it.path).suffix[1:].upper(), dims) if x))
            elif name:
                spec.append(" ".join(x for x in (name, short_res(it.height, it.width), fps_label(it.fps)) if x))
            elif prof is not None:
                spec.append(prof.label)
            if it.acodec:
                spec.append({"aac": "AAC", "mp3": "MP3", "pcm_s24le": "PCM 24", "pcm_s16le": "PCM 16"}.get(
                    it.acodec, it.acodec))
            spec.append(human_bytes(it.size))
            folder = Path(it.path).parent
            spec.append(f"  {folder.name or str(folder)}")
            text, color = "  ".join(s for s in spec if s), T.FG2
        else:
            text, color = "File moved or deleted", T.FG3
        p.setFont(self.f_spec)
        p.setPen(color)
        p.drawText(QRectF(x, c.top() + 52, tw, 16), Qt.AlignLeft | Qt.AlignVCenter,
                   QFontMetricsF(self.f_spec).elidedText(text, Qt.ElideRight, tw))
        if hovered and exists:
            pos = self.view.hover_pos
            for _n, ic, r in self.action_rects(option.rect):
                hot = pos is not None and r.contains(QPointF(pos))
                rounded(p, r, T.R_MD, T.S4 if hot else T.S3)
                icons.draw(p, ic, QRectF(r.center().x() - 8, r.center().y() - 8, 16, 16), T.FG)
        elif exists and it.report:
            p.setFont(T.font(11.5, 400, mono=True))
            ok = it.report.get("premiere")
            p.setPen(T.FG if ok else T.FG3)
            p.drawText(QRectF(c.right() - right_w, c.top(), right_w - 16, c.height()), Qt.AlignRight | Qt.AlignVCenter,
                       "Pr ✓  Ae ✓" if ok else "Pr ✕  Ae ✕")
        p.restore()


class LibraryView(QListView):
    action = Signal(str, str, QPoint)  # item id, action, global pos

    def __init__(self, model: LibraryModel, thumbs: ThumbCache, parent=None) -> None:
        super().__init__(parent)
        self.setModel(model)
        self.lmodel = model
        self.delegate = LibraryDelegate(self, thumbs)
        self.setItemDelegate(self.delegate)
        self.setMouseTracking(True)
        self.setUniformItemSizes(True)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.verticalScrollBar().setSingleStep(24)
        self.setDragEnabled(True)
        self.setDragDropMode(QAbstractItemView.DragOnly)
        self.setFrameShape(QListView.NoFrame)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(
            lambda pos: self._emit_at(pos, "more"))
        self.hover_row = -1
        self.hover_pos = None
        self.empty_text = ""
        thumbs.loaded.connect(lambda _u: self.viewport().update())

    def _emit_at(self, pos: QPoint, action: str) -> None:
        idx = self.indexAt(pos)
        if not idx.isValid():
            return
        if not self.selectionModel().isSelected(idx):
            self.setCurrentIndex(idx)
        it = self.lmodel.items[idx.row()]
        self.action.emit(it.id, action, self.viewport().mapToGlobal(pos))

    def selected_items(self) -> list[Item]:
        return [self.lmodel.items[i.row()] for i in self.selectionModel().selectedRows()
                if i.row() < len(self.lmodel.items)]

    def mouseMoveEvent(self, e) -> None:  # noqa: N802
        idx = self.indexAt(e.position().toPoint())
        old = self.hover_row
        self.hover_row = idx.row() if idx.isValid() else -1
        self.hover_pos = e.position().toPoint()
        if old != self.hover_row and old >= 0:
            self.viewport().update(self.visualRect(self.lmodel.index(old)))
        if idx.isValid():
            self.viewport().update(self.visualRect(idx))
        super().mouseMoveEvent(e)

    def leaveEvent(self, e) -> None:  # noqa: N802
        old = self.hover_row
        self.hover_row = -1
        if old >= 0:
            self.viewport().update(self.visualRect(self.lmodel.index(old)))
        super().leaveEvent(e)

    def mouseReleaseEvent(self, e) -> None:  # noqa: N802
        pos = e.position().toPoint()
        idx = self.indexAt(pos)
        if idx.isValid() and e.button() == Qt.LeftButton:
            for name, _ic, r in self.delegate.action_rects(self.visualRect(idx)):
                if r.contains(QPointF(pos)) and self.lmodel.exists(self.lmodel.items[idx.row()]):
                    self.action.emit(self.lmodel.items[idx.row()].id, name, self.viewport().mapToGlobal(pos))
                    return
        super().mouseReleaseEvent(e)

    def mouseDoubleClickEvent(self, e) -> None:  # noqa: N802
        idx = self.indexAt(e.position().toPoint())
        if idx.isValid():
            self.action.emit(self.lmodel.items[idx.row()].id, "open", e.globalPosition().toPoint())

    def keyPressEvent(self, e) -> None:  # noqa: N802
        items = self.selected_items()
        if items and e.key() in (Qt.Key_Return, Qt.Key_Enter):
            self.action.emit(items[0].id, "open", QPoint())
            return
        if items and e.key() == Qt.Key_Delete:
            self.action.emit(items[0].id, "forget", QPoint())
            return
        super().keyPressEvent(e)

    def startDrag(self, actions) -> None:  # noqa: N802
        files = [it.path for it in self.selected_items() if Path(it.path).exists()]
        if not files:
            return
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(f) for f in files])
        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.exec(Qt.CopyAction, Qt.CopyAction)

    def paintEvent(self, e) -> None:  # noqa: N802
        if self.lmodel.rowCount() == 0 and self.empty_text:
            p = QPainter(self.viewport())
            p.setFont(T.font(13, 400))
            p.setPen(T.FG3)
            p.drawText(QRectF(14, 20, self.viewport().width() - 28, 60), Qt.AlignLeft | Qt.TextWordWrap,
                       self.empty_text)
            p.end()
            return
        super().paintEvent(e)


class LibraryPage(QWidget):
    action = Signal(str, str, QPoint)

    def __init__(self, library: Library, thumbs: ThumbCache, parent=None) -> None:
        super().__init__(parent)
        self.model = LibraryModel(library)
        self.view = LibraryView(self.model, thumbs)
        self.view.action.connect(self.action.emit)
        self.count = Label("", 12, 400, T.FG3, tabular=True)
        self.search = TextField("", "Search titles, channels, sites, paths")
        self.search.setFixedWidth(320)
        self.filter = Segmented([("all", "All"), ("video", "Video"), ("audio", "Audio"), ("image", "Images")], "all")
        self.filter.setFixedWidth(290)
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(180)
        self._debounce.timeout.connect(self.reload)
        self.search.textChanged.connect(lambda _t: self._debounce.start())
        self.filter.changed.connect(lambda _v: self.reload())
        hint = Label("Drag files from here straight into Premiere's Project panel or After Effects.", 12, 400,
                     T.FG3)
        head = hbox(self.search, self.filter, None, self.count, spacing=12, margins=(0, 0, 4, 0))
        self.setLayout(vbox(head, vbox(hint, margins=(2, 0, 0, 0)), self.view, spacing=10,
                            margins=(24, 8, 24, 20)))

    def reload(self) -> None:
        self.model.reload(self.search.text().strip(), self.filter.value)
        n = self.model.rowCount()
        self.count.setText(f"{n} item{'s' if n != 1 else ''}")
        if self.search.text().strip():
            self.view.empty_text = "Nothing matches that search."
        else:
            self.view.empty_text = "Finished downloads land here, with their Premiere and After Effects checks."
        self.view.viewport().update()

    def showEvent(self, e) -> None:  # noqa: N802
        self.reload()
        super().showEvent(e)

