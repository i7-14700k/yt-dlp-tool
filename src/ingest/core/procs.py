"""Child processes that can be stopped, paused and resumed as a whole tree."""

from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import threading
from typing import Callable, Iterator

from . import winapi

IS_WINDOWS = sys.platform == "win32"
CREATE_NO_WINDOW = 0x08000000
CREATE_SUSPENDED = 0x00000004


def child_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ)
    # Frozen Python tools (yt-dlp.exe, gallery-dl.exe) print UTF-8 instead of the
    # console code page, so titles in any script survive the pipe.
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env.pop("PYTHONHOME", None)
    env.pop("PYTHONPATH", None)
    if extra:
        env.update(extra)
    return env


def popen_kwargs() -> dict:
    if IS_WINDOWS:
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0
        return {"creationflags": CREATE_NO_WINDOW, "startupinfo": si}
    return {"start_new_session": True}


def run_capture(args: list[str], timeout: float = 30, cwd: str | None = None) -> tuple[int, str]:
    """Run a short command and return (exit code, combined output)."""
    try:
        p = subprocess.run(
            args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            timeout=timeout, cwd=cwd, env=child_env(), **popen_kwargs())
    except subprocess.TimeoutExpired as e:
        out = e.output.decode("utf-8", "replace") if e.output else ""
        return -1, out + "\n[timed out]"
    except OSError as e:
        return -2, str(e)
    return p.returncode, p.stdout.decode("utf-8", "replace")


class Proc:
    """A process with its descendants grouped so they can be controlled together.

    Windows: a job object with kill-on-close. Elsewhere: a new session, signalled
    as a process group.
    """

    def __init__(self, args: list[str], cwd: str | None = None, env: dict[str, str] | None = None,
                 merge_stderr: bool = True, on_stderr: Callable[[str], None] | None = None) -> None:
        self.args = args
        self._job = winapi.JobObject() if IS_WINDOWS else None
        self._lock = threading.Lock()
        self.killed = False
        self.suspended = False
        self.stderr_lines: list[str] = []
        self._stderr_thread: threading.Thread | None = None
        kw = popen_kwargs()
        in_job = self._job is not None and self._job.valid
        if in_job:
            # Start suspended and join the job before running a single instruction: yt-dlp.exe's
            # launcher starts its real process (and that one ffmpeg) at once, and a child that
            # appeared before the assignment would escape the kill-the-whole-tree guarantee.
            kw["creationflags"] = kw.get("creationflags", 0) | CREATE_SUSPENDED
        self._p = subprocess.Popen(
            args, cwd=cwd, env=env or child_env(), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT if merge_stderr else subprocess.PIPE, bufsize=0, **kw)
        if in_job:
            try:
                self._job.assign(self._p.pid)
            finally:
                if not winapi.resume_pid(self._p.pid):
                    winapi.resume_handle(int(getattr(self._p, "_handle", 0) or 0))
        if not merge_stderr:
            self._stderr_thread = threading.Thread(target=self._drain_stderr, args=(on_stderr,), daemon=True)
            self._stderr_thread.start()

    @property
    def pid(self) -> int:
        return self._p.pid

    def _drain_stderr(self, cb: Callable[[str], None] | None) -> None:
        assert self._p.stderr is not None
        for line in _iter_lines(self._p.stderr):
            self.stderr_lines.append(line)
            if len(self.stderr_lines) > 4000:
                del self.stderr_lines[:1000]
            if cb:
                try:
                    cb(line)
                except Exception:
                    pass

    def lines(self) -> Iterator[str]:
        assert self._p.stdout is not None
        yield from _iter_lines(self._p.stdout)

    def read_all(self) -> bytes:
        assert self._p.stdout is not None
        chunks = []
        while True:
            b = self._p.stdout.read(1 << 16)
            if not b:
                break
            chunks.append(b)
        return b"".join(chunks)

    def wait(self, timeout: float | None = None) -> int:
        code = self._p.wait(timeout)
        if self._stderr_thread is not None:
            self._stderr_thread.join(timeout=5)
        if self._job is not None:
            self._job.close()
        return code

    def poll(self) -> int | None:
        return self._p.poll()

    def kill(self) -> None:
        with self._lock:
            self.killed = True
            if self._p.poll() is not None and not self.suspended:
                return
            try:
                if IS_WINDOWS:
                    if self._job is not None and self._job.valid:
                        self._job.terminate()
                    else:
                        subprocess.run(["taskkill", "/PID", str(self._p.pid), "/T", "/F"],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **popen_kwargs())
                else:
                    if self.suspended:
                        os.killpg(self._p.pid, signal.SIGCONT)
                    os.killpg(self._p.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass
            try:
                self._p.kill()
            except OSError:
                pass

    def suspend(self) -> bool:
        with self._lock:
            if self._p.poll() is not None or self.suspended:
                return False
            ok = False
            if IS_WINDOWS:
                pids = self._job.pids() if self._job is not None else []
                for pid in pids or [self._p.pid]:
                    ok = winapi.suspend_pid(pid) or ok
            else:
                try:
                    os.killpg(self._p.pid, signal.SIGSTOP)
                    ok = True
                except OSError:
                    ok = False
            self.suspended = ok
            return ok

    def resume(self) -> bool:
        with self._lock:
            if not self.suspended:
                return False
            if IS_WINDOWS:
                pids = self._job.pids() if self._job is not None else []
                for pid in pids or [self._p.pid]:
                    winapi.resume_pid(pid)
            else:
                try:
                    os.killpg(self._p.pid, signal.SIGCONT)
                except OSError:
                    pass
            self.suspended = False
            return True


def _iter_lines(stream) -> Iterator[str]:
    """Yield decoded lines split on \\n or \\r, without waiting for full buffers."""
    pending: list[bytes] = []
    pending_len = 0
    while True:
        try:
            chunk = stream.read(1 << 15)
        except (OSError, ValueError):
            break
        if not chunk:
            break
        parts = _SPLIT_RE.split(chunk)
        if len(parts) == 1:
            pending.append(chunk)
            pending_len += len(chunk)
            if pending_len > 1 << 24:  # a 16 MB line: flush rather than grow forever
                yield b"".join(pending).decode("utf-8", "replace")
                pending, pending_len = [], 0
            continue
        first = b"".join(pending) + parts[0]
        if first:
            yield first.decode("utf-8", "replace")
        for mid in parts[1:-1]:
            if mid:
                yield mid.decode("utf-8", "replace")
        pending = [parts[-1]] if parts[-1] else []
        pending_len = len(parts[-1])
    tail = b"".join(pending)
    if tail:
        yield tail.decode("utf-8", "replace")


_SPLIT_RE = re.compile(rb"[\r\n]")
