"""Find out which encoders and filters the installed ffmpeg can really use.

Listing `-encoders` is not enough: an NVENC build reports h264_nvenc on a PC
without an NVIDIA GPU. Each hardware encoder is test-encoded for a few frames.
"""

from __future__ import annotations

import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .conform import Caps
from .procs import run_capture

_HW = {
    "h264": {"nvenc": "h264_nvenc", "qsv": "h264_qsv", "amf": "h264_amf"},
    "hevc": {"nvenc": "hevc_nvenc", "qsv": "hevc_qsv", "amf": "hevc_amf"},
}
_cache_lock = threading.Lock()


def _test_encode(ffmpeg: str, encoder: str) -> bool:
    pix = "nv12" if encoder.endswith(("_qsv", "_amf")) else "yuv420p"
    if encoder.startswith("hevc"):
        pix = "p010le" if encoder.endswith(("_qsv", "_amf", "_nvenc")) else pix
    code, out = run_capture(
        [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-f", "lavfi",
         "-i", "color=c=black:s=640x360:r=30:d=0.2", "-vf", f"format={pix}", "-frames:v", "4",
         "-c:v", encoder, "-f", "null", "-"], timeout=20)
    return code == 0


def _fingerprint(ffmpeg: str) -> str:
    try:
        st = os.stat(ffmpeg)
        return f"{os.path.realpath(ffmpeg)}|{st.st_size}|{int(st.st_mtime)}"
    except OSError:
        return ffmpeg


def detect(ffmpeg: str, cache_file: Path | None = None) -> Caps:
    fp = _fingerprint(ffmpeg)
    if cache_file is not None:
        with _cache_lock:
            try:
                data = json.loads(cache_file.read_text(encoding="utf-8"))
                if data.get("fingerprint") == fp:
                    return Caps(**data["caps"])
            except (OSError, ValueError, KeyError, TypeError):
                pass

    code, enc_list = run_capture([ffmpeg, "-hide_banner", "-encoders"], timeout=20)
    _, filt_list = run_capture([ffmpeg, "-hide_banner", "-filters"], timeout=20)
    listed = enc_list if code == 0 else ""
    caps = Caps(hw_h264=[], hw_hevc=[], zscale=" zscale " in filt_list,
                libx265=" libx265 " in listed, libmp3lame=" libmp3lame " in listed)

    candidates = [(kind, fam, enc) for kind, fams in _HW.items() for fam, enc in fams.items()
                  if f" {enc} " in listed]
    if candidates:
        with ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(lambda c: (c, _test_encode(ffmpeg, c[2])), candidates))
        for (kind, fam, _enc), ok in results:
            if ok:
                (caps.hw_h264 if kind == "h264" else caps.hw_hevc).append(fam)
        order = {"nvenc": 0, "qsv": 1, "amf": 2}
        caps.hw_h264.sort(key=order.get)
        caps.hw_hevc.sort(key=order.get)

    if cache_file is not None:
        with _cache_lock:
            try:
                cache_file.parent.mkdir(parents=True, exist_ok=True)
                cache_file.write_text(json.dumps({"fingerprint": fp, "caps": caps.__dict__}), encoding="utf-8")
            except OSError:
                pass
    return caps


HW_NAMES = {"nvenc": "NVIDIA NVENC", "qsv": "Intel Quick Sync", "amf": "AMD AMF"}
