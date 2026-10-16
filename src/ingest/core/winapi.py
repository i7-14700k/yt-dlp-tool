"""Thin ctypes wrappers for the Windows APIs Ingest relies on.

Every function is safe to call on other platforms: it either does nothing or
returns a neutral value, so callers never need their own platform checks.
"""

from __future__ import annotations

import ctypes
import os
import sys
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"

if IS_WINDOWS:
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _ntdll = ctypes.WinDLL("ntdll")
    _shell32 = ctypes.WinDLL("shell32")
    _ole32 = ctypes.WinDLL("ole32")
    try:
        _dwmapi = ctypes.WinDLL("dwmapi")
    except OSError:  # pragma: no cover - dwmapi exists on every supported Windows
        _dwmapi = None

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [(n, ctypes.c_ulonglong) for n in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", _IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    _JobObjectExtendedLimitInformation = 9
    _JobObjectBasicProcessIdList = 3
    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
    _PROCESS_TERMINATE = 0x0001
    _PROCESS_SET_QUOTA = 0x0100
    _PROCESS_SUSPEND_RESUME = 0x0800
    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

    _kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    _kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    _kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    _kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _kernel32.QueryInformationJobObject.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.SetFileAttributesW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD]
    _kernel32.GetFileAttributesW.argtypes = [wintypes.LPCWSTR]
    _kernel32.GetFileAttributesW.restype = wintypes.DWORD
    _ntdll.NtSuspendProcess.argtypes = [wintypes.HANDLE]
    _ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]


class JobObject:
    """A Windows job object that kills every process in it when closed.

    Children started by yt-dlp (its own PyInstaller child, ffmpeg) inherit the
    job, so terminating it stops the entire tree. If Ingest crashes the OS
    closes the handle and the tree still dies, which prevents orphaned
    downloads from running in the background.
    """

    def __init__(self) -> None:
        self._handle = None
        if not IS_WINDOWS:
            return
        handle = _kernel32.CreateJobObjectW(None, None)
        if not handle:
            return
        info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        ok = _kernel32.SetInformationJobObject(
            handle, _JobObjectExtendedLimitInformation, ctypes.byref(info), ctypes.sizeof(info))
        if not ok:
            _kernel32.CloseHandle(handle)
            return
        self._handle = handle

    @property
    def valid(self) -> bool:
        return bool(self._handle)

    def assign(self, pid: int) -> bool:
        if not self._handle:
            return False
        proc = _kernel32.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, pid)
        if not proc:
            return False
        try:
            return bool(_kernel32.AssignProcessToJobObject(self._handle, proc))
        finally:
            _kernel32.CloseHandle(proc)

    def pids(self) -> list[int]:
        if not self._handle:
            return []
        count = 256

        class _List(ctypes.Structure):
            _fields_ = [
                ("NumberOfAssignedProcesses", wintypes.DWORD),
                ("NumberOfProcessIdsInList", wintypes.DWORD),
                ("ProcessIdList", ctypes.c_size_t * count),
            ]

        lst = _List()
        ok = _kernel32.QueryInformationJobObject(
            self._handle, _JobObjectBasicProcessIdList, ctypes.byref(lst), ctypes.sizeof(lst), None)
        if not ok:
            return []
        return [int(lst.ProcessIdList[i]) for i in range(min(lst.NumberOfProcessIdsInList, count))]

    def terminate(self, code: int = 1) -> None:
        if self._handle:
            _kernel32.TerminateJobObject(self._handle, code)

    def close(self) -> None:
        if self._handle:
            _kernel32.CloseHandle(self._handle)
            self._handle = None

    def __del__(self) -> None:  # pragma: no cover - finalizer
        try:
            self.close()
        except Exception:
            pass


def suspend_pid(pid: int) -> bool:
    if not IS_WINDOWS:
        return False
    proc = _kernel32.OpenProcess(_PROCESS_SUSPEND_RESUME, False, pid)
    if not proc:
        return False
    try:
        return _ntdll.NtSuspendProcess(proc) == 0
    finally:
        _kernel32.CloseHandle(proc)


def resume_pid(pid: int) -> bool:
    if not IS_WINDOWS:
        return False
    proc = _kernel32.OpenProcess(_PROCESS_SUSPEND_RESUME, False, pid)
    if not proc:
        return False
    try:
        return _ntdll.NtResumeProcess(proc) == 0
    finally:
        _kernel32.CloseHandle(proc)


def resume_handle(handle: int) -> bool:
    """Resume through a process handle we already hold (subprocess.Popen's has full access)."""
    if not IS_WINDOWS or not handle:
        return False
    return _ntdll.NtResumeProcess(wintypes.HANDLE(handle)) == 0


def set_hidden(path: os.PathLike | str) -> None:
    """Mark a file or folder hidden in Explorer (and in Premiere's Media Browser)."""
    if not IS_WINDOWS:
        return
    p = str(path)
    attrs = _kernel32.GetFileAttributesW(p)
    if attrs == 0xFFFFFFFF:
        return
    _kernel32.SetFileAttributesW(p, attrs | 0x2)


def set_app_user_model_id(app_id: str) -> None:
    """Group taskbar buttons under Ingest's own identity instead of python.exe."""
    if not IS_WINDOWS:
        return
    try:
        _shell32.SetCurrentProcessExplicitAppUserModelID(ctypes.c_wchar_p(app_id))
    except Exception:
        pass


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_uint32),
        ("Data2", ctypes.c_uint16),
        ("Data3", ctypes.c_uint16),
        ("Data4", ctypes.c_ubyte * 8),
    ]

    @classmethod
    def parse(cls, text: str) -> "_GUID":
        import uuid

        u = uuid.UUID(text)
        g = cls()
        g.Data1, g.Data2, g.Data3 = u.fields[0], u.fields[1], u.fields[2]
        rest = u.bytes[8:]
        for i in range(8):
            g.Data4[i] = rest[i]
        return g


def known_folder(guid: str) -> Path | None:
    """Resolve a Windows known folder such as Videos, honouring user relocation."""
    if not IS_WINDOWS:
        return None
    fid = _GUID.parse(guid)
    out = ctypes.c_wchar_p()
    try:
        hr = _shell32.SHGetKnownFolderPath(ctypes.byref(fid), 0, None, ctypes.byref(out))
    except Exception:
        return None
    if hr != 0 or not out.value:
        return None
    path = Path(out.value)
    _ole32.CoTaskMemFree(out)
    return path


FOLDERID_VIDEOS = "18989B1D-99B5-455B-841C-AB7C74E4DDFC"
FOLDERID_DOWNLOADS = "374DE290-123F-4565-9164-39C4925E467B"


# ---------------------------------------------------------------- window chrome

_DWMWA_USE_IMMERSIVE_DARK_MODE_OLD = 19
_DWMWA_USE_IMMERSIVE_DARK_MODE = 20
_DWMWA_WINDOW_CORNER_PREFERENCE = 33
_DWMWA_BORDER_COLOR = 34
_DWMWA_CAPTION_COLOR = 35
_DWMWA_TEXT_COLOR = 36
_DWMWA_COLOR_NONE = 0xFFFFFFFE
_DWMWCP_ROUND = 2


def windows_build() -> int:
    if not IS_WINDOWS:
        return 0
    try:
        return sys.getwindowsversion().build
    except Exception:
        return 0


def style_window_chrome(hwnd: int, caption_rgb: int = 0x000000, text_rgb: int = 0xFFFFFF) -> None:
    """Paint the native title bar black with white text and drop the 1 px frame.

    Windows 11 honours caption, text and border colors. Windows 10 only gets the
    dark-mode caption, which is the closest it supports.
    """
    if not IS_WINDOWS or not _dwmapi or not hwnd:
        return

    def _set(attr: int, value: int) -> None:
        v = ctypes.c_int(value) if value < 0x80000000 else ctypes.c_uint(value)
        try:
            _dwmapi.DwmSetWindowAttribute(wintypes.HWND(hwnd), attr, ctypes.byref(v), ctypes.sizeof(v))
        except Exception:
            pass

    build = windows_build()
    _set(_DWMWA_USE_IMMERSIVE_DARK_MODE if build >= 18985 else _DWMWA_USE_IMMERSIVE_DARK_MODE_OLD, 1)
    if build >= 22000:
        bgr = lambda rgb: ((rgb & 0xFF) << 16) | (rgb & 0xFF00) | ((rgb >> 16) & 0xFF)  # noqa: E731
        _set(_DWMWA_CAPTION_COLOR, bgr(caption_rgb))
        _set(_DWMWA_TEXT_COLOR, bgr(text_rgb))
        _set(_DWMWA_BORDER_COLOR, _DWMWA_COLOR_NONE)
        _set(_DWMWA_WINDOW_CORNER_PREFERENCE, _DWMWCP_ROUND)


class TaskbarProgress:
    """ITaskbarList3 progress on the taskbar button, through raw COM vtable calls."""

    NOPROGRESS, INDETERMINATE, NORMAL, ERROR, PAUSED = 0, 1, 2, 4, 8

    def __init__(self) -> None:
        self._ptr = None
        self._vtbl = None
        if not IS_WINDOWS:
            return
        try:
            _ole32.CoInitializeEx(None, 0x2)  # apartment threaded; S_FALSE if Qt already did it
            clsid = _GUID.parse("56FDF344-FD6D-11d0-958A-006097C9A090")
            iid = _GUID.parse("EA1AFB91-9E28-4B86-90E9-9E9F8A5EEFAF")
            ptr = ctypes.c_void_p()
            hr = _ole32.CoCreateInstance(ctypes.byref(clsid), None, 1, ctypes.byref(iid), ctypes.byref(ptr))
            if hr != 0 or not ptr.value:
                return
            vtbl = ctypes.cast(ctypes.cast(ptr, ctypes.POINTER(ctypes.c_void_p))[0],
                               ctypes.POINTER(ctypes.c_void_p))
            hr_init = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p)(vtbl[3])
            if hr_init(ptr) != 0:
                return
            self._ptr, self._vtbl = ptr, vtbl
            self._set_value = ctypes.WINFUNCTYPE(
                ctypes.c_long, ctypes.c_void_p, wintypes.HWND, ctypes.c_ulonglong, ctypes.c_ulonglong)(vtbl[9])
            self._set_state = ctypes.WINFUNCTYPE(
                ctypes.c_long, ctypes.c_void_p, wintypes.HWND, ctypes.c_int)(vtbl[10])
        except Exception:
            self._ptr = None

    @property
    def available(self) -> bool:
        return self._ptr is not None

    def set(self, hwnd: int, fraction: float | None, state: int | None = None) -> None:
        if not self._ptr or not hwnd:
            return
        try:
            if fraction is None:
                self._set_state(self._ptr, wintypes.HWND(hwnd), self.NOPROGRESS if state is None else state)
                return
            self._set_state(self._ptr, wintypes.HWND(hwnd), self.NORMAL if state is None else state)
            value = max(0, min(1000, int(fraction * 1000)))
            self._set_value(self._ptr, wintypes.HWND(hwnd), value, 1000)
        except Exception:
            pass


# ---------------------------------------------------------------- ingest:// links

PROTOCOL = "ingest"


def register_protocol(exe: str) -> bool:
    """Register ingest:// for the current user so a browser bookmarklet can hand links over.

    Writes only under HKCU\\Software\\Classes; no administrator rights needed.
    Returns True if the registration is (now) in place.
    """
    if not IS_WINDOWS:
        return False
    import winreg
    command = f'"{exe}" "%1"'
    base = rf"Software\Classes\{PROTOCOL}"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, base + r"\shell\open\command") as k:
            if winreg.QueryValueEx(k, "")[0] == command:
                return True
    except OSError:
        pass
    try:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, base) as k:
            winreg.SetValueEx(k, "", 0, winreg.REG_SZ, "URL:Ingest")
            winreg.SetValueEx(k, "URL Protocol", 0, winreg.REG_SZ, "")
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, base + r"\DefaultIcon") as k:
            winreg.SetValueEx(k, "", 0, winreg.REG_SZ, f'"{exe}",0')
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, base + r"\shell\open\command") as k:
            winreg.SetValueEx(k, "", 0, winreg.REG_SZ, command)
        return True
    except OSError:
        return False
