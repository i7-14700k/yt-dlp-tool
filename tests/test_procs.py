"""Process control: the whole tree dies on kill, and suspend really stops output. Runs on Windows CI too."""

import sys
import threading
import time

from ingest.core.procs import Proc


def alive(pid: int) -> bool:
    if sys.platform == "win32":
        import ctypes
        k = ctypes.WinDLL("kernel32")
        h = k.OpenProcess(0x1000 | 0x00100000, False, pid)  # QUERY_LIMITED_INFORMATION | SYNCHRONIZE
        if not h:
            return False
        try:
            return k.WaitForSingleObject(h, 0) == 0x102  # WAIT_TIMEOUT: still running
        finally:
            k.CloseHandle(h)
    import os
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:  # a zombie still answers kill(0); check its state
        with open(f"/proc/{pid}/stat") as fh:
            return fh.read().split(")")[-1].split()[0] != "Z"
    except OSError:
        return True


PARENT = (
    "import subprocess, sys, time\n"
    "c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
    "print(c.pid, flush=True)\n"
    "time.sleep(120)\n"
)


def test_kill_takes_the_grandchild_too():
    p = Proc([sys.executable, "-c", PARENT])
    it = p.lines()
    grandchild = int(next(it))
    assert alive(grandchild)
    p.kill()
    p.wait(10)
    deadline = time.time() + 10
    while alive(grandchild) and time.time() < deadline:
        time.sleep(0.1)
    assert not alive(grandchild)


def test_suspend_and_resume():
    p = Proc([sys.executable, "-u", "-c", "import time\nfor i in range(100000):\n print(i, flush=True)\n time.sleep(0.05)"])
    lines: list[str] = []

    def read():
        for ln in p.lines():
            lines.append(ln)

    t = threading.Thread(target=read, daemon=True)
    t.start()
    time.sleep(0.6)
    assert lines
    assert p.suspend()
    time.sleep(0.3)  # let anything already in the pipe arrive
    frozen = len(lines)
    time.sleep(0.8)
    assert len(lines) == frozen
    assert p.resume()
    time.sleep(0.6)
    assert len(lines) > frozen
    p.kill()
    p.wait(10)


def test_unicode_output_survives():
    p = Proc([sys.executable, "-c", "import sys; sys.stdout.buffer.write('日本語 ✓ é\\n'.encode('utf-8'))"])
    out = list(p.lines())
    p.wait(10)
    assert out == ["日本語 ✓ é"]
