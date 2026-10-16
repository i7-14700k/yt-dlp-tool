"""Engine install/update work on a background thread, reported through Qt signals."""

from __future__ import annotations

import threading
import time

from PySide6.QtCore import QObject, Signal

from ..core.engines import SPECS, Cancelled, Engines, EngineState, InstallError


class EngineTasks(QObject):
    progress = Signal(str, str, float, float)     # key, phase, done, total (-1 unknown)
    finished = Signal(str, bool, str)             # key, ok, message
    batch_done = Signal(bool)                     # all ok
    states = Signal(dict)                         # key -> EngineState
    updates = Signal(dict)                        # key -> (available, latest)

    def __init__(self, engines: Engines, parent=None) -> None:
        super().__init__(parent)
        self.engines = engines
        self._cancel = threading.Event()
        self._busy = False
        self._lock = threading.Lock()

    @property
    def busy(self) -> bool:
        return self._busy

    def cancel(self) -> None:
        self._cancel.set()

    def install(self, keys: list[str]) -> bool:
        with self._lock:
            if self._busy:
                return False
            self._busy = True
        self._cancel.clear()
        threading.Thread(target=self._install, args=(list(keys),), name="engines", daemon=True).start()
        return True

    def _install(self, keys: list[str]) -> None:
        ok_all = True
        try:
            for key in keys:
                def cb(phase, done, total, k=key):
                    self.progress.emit(k, phase, float(done), float(total) if total else -1.0)
                try:
                    self.engines.install(key, cb, self._cancel)
                    self.finished.emit(key, True, f"{SPECS[key].name} {self.engines.version(key, refresh=True)}")
                except Cancelled:
                    ok_all = False
                    self.finished.emit(key, False, "Canceled")
                    break
                except InstallError as e:
                    ok_all = False
                    self.finished.emit(key, False, str(e))
                except Exception as e:  # network stacks raise a zoo of types
                    ok_all = False
                    self.finished.emit(key, False, f"{type(e).__name__}: {e}")
        finally:
            self._busy = False
            self.batch_done.emit(ok_all)
            self.refresh_states()

    def refresh_states(self) -> None:
        def work():
            out: dict[str, EngineState] = {}
            for key in SPECS:
                try:
                    out[key] = self.engines.state(key, refresh=True)
                except Exception as e:
                    out[key] = EngineState(key=key, error=str(e))
            self.states.emit(out)
        threading.Thread(target=work, name="engine-states", daemon=True).start()

    def check_updates(self, keys: list[str] | None = None) -> None:
        def work():
            res = {}
            for key in keys or ["ytdlp", "deno"]:
                try:
                    res[key] = self.engines.check_update(key)
                except Exception:
                    res[key] = (False, "")
            self.updates.emit(res)
            self.refresh_states()
        threading.Thread(target=work, name="engine-updates", daemon=True).start()


def due_for_check(last: float, hours: float = 20) -> bool:
    return time.time() - (last or 0) > hours * 3600
