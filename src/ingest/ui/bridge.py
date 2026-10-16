"""Connects the thread-based core to Qt signals so every UI update happens on the UI thread."""

from __future__ import annotations

from PySide6.QtCore import QObject, Signal

from ..core.queue import Events


class Bridge(QObject):
    added = Signal(list, int)        # [Job], index
    updated = Signal(object, dict)   # Job, changed fields
    removed = Signal(list)           # [job id]
    review = Signal(object, dict, list)
    finished = Signal(object)

    def events(self) -> Events:
        return Events(
            added=lambda jobs, idx: self.added.emit(list(jobs), int(idx)),
            updated=lambda job, fields: self.updated.emit(job, dict(fields)),
            removed=lambda ids: self.removed.emit(list(ids)),
            review=lambda job, info, children: self.review.emit(job, dict(info), list(children)),
            finished=lambda job: self.finished.emit(job),
        )
