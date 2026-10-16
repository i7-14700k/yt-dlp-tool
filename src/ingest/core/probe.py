"""ffprobe wrapper and the measurements that decide whether media needs converting."""

from __future__ import annotations

import json
import re
import statistics
import struct
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path

from .procs import run_capture

STANDARD_RATES = [
    Fraction(24000, 1001), Fraction(24), Fraction(25), Fraction(30000, 1001), Fraction(30),
    Fraction(48000, 1001), Fraction(48), Fraction(50), Fraction(60000, 1001), Fraction(60),
    Fraction(100), Fraction(120000, 1001), Fraction(120),
    Fraction(15), Fraction(15000, 1001), Fraction(12), Fraction(10), Fraction(8),
]


def parse_rate(text: str | None) -> Fraction | None:
    if not text or text in ("0/0", "N/A"):
        return None
    try:
        r = Fraction(text)
    except (ValueError, ZeroDivisionError):
        return None
    return r if r > 0 else None


def snap_rate(rate: Fraction | float | None) -> Fraction | None:
    """Snap a measured rate to the nearest broadcast/NLE rate within 0.6 %."""
    if not rate:
        return None
    r = Fraction(rate).limit_denominator(100000)
    best = min(STANDARD_RATES, key=lambda s: abs(float(s) - float(r)))
    if abs(float(best) - float(r)) / float(best) < 0.006:
        return best
    # Unusual rate (e.g. 14.985): keep it, but on a millisecond-clean grid.
    return Fraction(round(float(r) * 1000), 1000)


def rate_str(rate: Fraction) -> str:
    return f"{rate.numerator}/{rate.denominator}" if rate.denominator != 1 else str(rate.numerator)


def _bit_depth(pix_fmt: str | None, raw: str | None) -> int:
    if raw and str(raw).isdigit() and int(raw) > 0:
        return int(raw)
    if not pix_fmt:
        return 8
    m = re.search(r"p(\d{2})(?:le|be)?$", pix_fmt)
    if m:
        return int(m.group(1))
    if pix_fmt in ("p010le", "p010be", "x2rgb10le", "y210le"):
        return 10
    return 8


@dataclass
class Stream:
    index: int
    kind: str
    codec: str = ""
    profile: str = ""
    pix_fmt: str = ""
    width: int = 0
    height: int = 0
    sar: str = ""
    r_rate: Fraction | None = None
    avg_rate: Fraction | None = None
    time_base: str = ""
    start_time: float = 0.0
    duration: float = 0.0
    bit_depth: int = 8
    color_space: str = ""
    color_transfer: str = ""
    color_primaries: str = ""
    color_range: str = ""
    field_order: str = ""
    rotation: int = 0
    sample_rate: int = 0
    channels: int = 0
    channel_layout: str = ""
    bitrate: int = 0
    attached_pic: bool = False
    language: str = ""
    nb_frames: int = 0

    @property
    def fps(self) -> float:
        r = self.avg_rate or self.r_rate
        return float(r) if r else 0.0

    @property
    def is_hdr(self) -> bool:
        return self.color_transfer in ("smpte2084", "arib-std-b67") or self.color_primaries == "bt2020"

    @property
    def is_interlaced(self) -> bool:
        return self.field_order in ("tt", "bb", "tb", "bt")

    @property
    def full_range(self) -> bool:
        return self.color_range == "pc" or self.pix_fmt.startswith("yuvj")

    @property
    def square_pixels(self) -> bool:
        return self.sar in ("", "0:1", "1:1", "N/A")

    @property
    def sar_value(self) -> float:
        try:
            num, den = (int(x) for x in self.sar.split(":"))
            return num / den if num > 0 and den > 0 else 1.0
        except (ValueError, ZeroDivisionError):
            return 1.0

    @property
    def near_square(self) -> bool:
        """Square for practical purposes: encoders often write 640:639 and similar for 852x480."""
        return self.square_pixels or abs(self.sar_value - 1.0) < 0.01

    @property
    def chroma(self) -> str:
        p = self.pix_fmt
        if "444" in p or p.startswith(("gbr", "rgb", "bgr")):
            return "4:4:4"
        if "422" in p or p.startswith("y210"):
            return "4:2:2"
        if "420" in p or p in ("nv12", "p010le", "nv21"):
            return "4:2:0"
        return p


@dataclass
class MediaInfo:
    path: str
    format_name: str = ""
    duration: float = 0.0
    size: int = 0
    bitrate: int = 0
    start_time: float = 0.0
    streams: list[Stream] = field(default_factory=list)
    tags: dict = field(default_factory=dict)

    @property
    def video(self) -> Stream | None:
        return next((s for s in self.streams if s.kind == "video" and not s.attached_pic), None)

    @property
    def audio(self) -> Stream | None:
        return next((s for s in self.streams if s.kind == "audio"), None)

    @property
    def attached_pics(self) -> list[Stream]:
        return [s for s in self.streams if s.kind == "video" and s.attached_pic]

    @property
    def container(self) -> str:
        f = self.format_name
        if "mp4" in f or "mov" in f:
            ext = Path(self.path).suffix.lower()
            return "mov" if ext == ".mov" else "mp4"
        if "matroska" in f or "webm" in f:
            return "webm" if Path(self.path).suffix.lower() == ".webm" else "matroska"
        return f.split(",")[0]


class ProbeError(RuntimeError):
    pass


def probe(ffprobe: str, path: str | Path, timeout: float = 60) -> MediaInfo:
    code, out = run_capture(
        [ffprobe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
        timeout=timeout)
    if code != 0:
        raise ProbeError(out.strip().splitlines()[-1] if out.strip() else f"ffprobe exited with {code}")
    try:
        data = json.loads(out[out.find("{"):])
    except ValueError as e:
        raise ProbeError(f"Unreadable ffprobe output: {e}") from e
    return parse_probe(str(path), data)


def _float(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _int(v, default=0) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def parse_probe(path: str, data: dict) -> MediaInfo:
    fmt = data.get("format") or {}
    info = MediaInfo(
        path=path,
        format_name=fmt.get("format_name", ""),
        duration=_float(fmt.get("duration")),
        size=_int(fmt.get("size")),
        bitrate=_int(fmt.get("bit_rate")),
        start_time=_float(fmt.get("start_time")),
        tags=fmt.get("tags") or {},
    )
    for s in data.get("streams") or []:
        kind = s.get("codec_type", "")
        rotation = 0
        for sd in s.get("side_data_list") or []:
            if "rotation" in sd:
                rotation = _int(sd.get("rotation"))
        if not rotation and (s.get("tags") or {}).get("rotate"):
            rotation = _int(s["tags"]["rotate"])
        disposition = s.get("disposition") or {}
        st = Stream(
            index=_int(s.get("index")),
            kind=kind,
            codec=s.get("codec_name", "") or "",
            profile=s.get("profile", "") or "",
            pix_fmt=s.get("pix_fmt", "") or "",
            width=_int(s.get("width")),
            height=_int(s.get("height")),
            sar=s.get("sample_aspect_ratio", "") or "",
            r_rate=parse_rate(s.get("r_frame_rate")),
            avg_rate=parse_rate(s.get("avg_frame_rate")),
            time_base=s.get("time_base", "") or "",
            start_time=_float(s.get("start_time")),
            duration=_float(s.get("duration")),
            bit_depth=_bit_depth(s.get("pix_fmt"), s.get("bits_per_raw_sample")),
            color_space=s.get("color_space", "") or "",
            color_transfer=s.get("color_transfer", "") or "",
            color_primaries=s.get("color_primaries", "") or "",
            color_range=s.get("color_range", "") or "",
            field_order=s.get("field_order", "") or "",
            rotation=((rotation % 360) + 360) % 360,
            sample_rate=_int(s.get("sample_rate")),
            channels=_int(s.get("channels")),
            channel_layout=s.get("channel_layout", "") or "",
            bitrate=_int(s.get("bit_rate")),
            attached_pic=bool(disposition.get("attached_pic")),
            language=(s.get("tags") or {}).get("language", "") or "",
            nb_frames=_int(s.get("nb_frames")),
        )
        if kind == "video" and st.codec in ("mjpeg", "png", "bmp", "gif", "webp") and st.attached_pic is False:
            # Cover art muxed as a regular single-frame stream.
            if st.nb_frames == 1 or (info.duration and st.duration == 0):
                st.attached_pic = True
        info.streams.append(st)
    return info


@dataclass
class Timing:
    """Frame-interval measurement over the first stretch of a video stream."""
    samples: int = 0
    median: float = 0.0
    spread: float = 0.0  # (max - min) / median, outliers trimmed
    vfr: bool = False
    measured_rate: Fraction | None = None


def measure_timing(ffprobe: str, path: str | Path, stream_index: int | None = None,
                   seconds: float = 40, timeout: float = 90) -> Timing:
    sel = f"{stream_index}" if stream_index is not None else "v:0"
    code, out = run_capture(
        [ffprobe, "-v", "error", "-select_streams", sel, "-read_intervals", f"%+{seconds}",
         "-show_entries", "packet=pts_time,flags", "-of", "csv=p=0", str(path)],
        timeout=timeout)
    if code != 0:
        return Timing()
    pts: list[float] = []
    for line in out.splitlines():
        parts = line.strip().split(",")
        if not parts or not parts[0] or parts[0] == "N/A":
            continue
        # Packets flagged 'D' are decoder pre-roll hidden by an edit list.
        if len(parts) > 1 and "D" in parts[1]:
            continue
        try:
            pts.append(float(parts[0]))
        except ValueError:
            continue
    return timing_from_pts(pts)


def timing_from_pts(pts: list[float]) -> Timing:
    pts = sorted(set(pts))
    if len(pts) < 12:
        return Timing(samples=len(pts))
    deltas = [b - a for a, b in zip(pts, pts[1:]) if b - a > 0]
    if len(deltas) < 10:
        return Timing(samples=len(deltas))
    med = statistics.median(deltas)
    trimmed = sorted(deltas)[1:-1]  # one stray packet at a segment boundary is not VFR
    spread = (trimmed[-1] - trimmed[0]) / med if med else 0.0
    # 1.5 ms absorbs millisecond-rounded container timestamps; anything wider is real.
    tolerance = max(0.0015, med * 0.004)
    irregular = sum(1 for d in deltas if abs(d - med) > tolerance)
    vfr = irregular > max(2, len(deltas) * 0.01)
    return Timing(samples=len(deltas), median=med, spread=spread, vfr=vfr,
                  measured_rate=Fraction(1 / med).limit_denominator(1001) if med else None)


@dataclass
class GridFit:
    """Every frame of a stream mapped onto an exact constant-rate grid."""
    rate: Fraction
    k0: int        # grid slot of the first packet in decode order (the earliest frame is slot 0)
    frames: int
    worst: float   # largest distance from its slot, in frames


def grid_fit(ffprobe: str, path: str | Path, rate: Fraction, stream_index: int | None = None,
             timeout: float = 900) -> GridFit | None:
    """Can each frame move onto an exact `rate` grid without collisions, gaps or reordering?

    True for constant-rate video whose container rounded the timestamps (RTMP and
    FLV origins such as Twitch, Matroska's milliseconds): the frames were evenly
    spaced all along and only their stamps jitter. Reads every packet of the
    stream, no decoding.
    """
    code, out = run_capture(grid_scan_args(ffprobe, path, stream_index), timeout=timeout)
    return grid_fit_from_scan(out, rate) if code == 0 else None


def grid_scan_args(ffprobe: str, path: str | Path, stream_index: int | None = None) -> list[str]:
    sel = f"{stream_index}" if stream_index is not None else "v:0"
    return [ffprobe, "-v", "error", "-select_streams", sel, "-show_entries", "packet=pts_time,flags",
            "-of", "csv=p=0", str(path)]


def grid_fit_from_scan(out: str, rate: Fraction) -> GridFit | None:
    if not rate:
        return None
    pts: list[float] = []
    first_key = False
    for line in out.splitlines():
        parts = line.strip().split(",")
        if not parts or not parts[0]:
            continue
        flags = parts[1] if len(parts) > 1 else ""
        if parts[0] == "N/A" or "D" in flags:
            return None  # a frame with no stamp, or edit-list preroll: leave it to the encoder
        try:
            t = float(parts[0])
        except ValueError:
            return None
        if not pts:
            first_key = "K" in flags  # stream copy drops whatever precedes the first keyframe
        pts.append(t)
    if len(pts) < 12 or not first_key:
        return None
    step = 1 / float(rate)
    p0 = min(pts)
    slots, worst = [], 0.0
    for t in pts:
        x = (t - p0) / step
        k = round(x)
        worst = max(worst, abs(x - k))
        slots.append(k)
    if worst > 0.25 or sorted(slots) != list(range(len(slots))):
        return None
    return GridFit(rate=Fraction(rate), k0=slots[0], frames=len(pts), worst=worst)


def moov_before_mdat(path: str | Path) -> bool | None:
    """True if the MP4/MOV index is at the front (fast start). None if not an ISO file."""
    try:
        with open(path, "rb") as fh:
            pos = 0
            size_total = Path(path).stat().st_size
            while pos < size_total:
                fh.seek(pos)
                header = fh.read(16)
                if len(header) < 8:
                    return None
                size, kind = struct.unpack(">I4s", header[:8])
                if size == 1:
                    size = struct.unpack(">Q", header[8:16])[0]
                elif size == 0:
                    size = size_total - pos
                if kind == b"moov":
                    return True
                if kind == b"mdat":
                    return False
                if size < 8:
                    return None
                pos += size
    except OSError:
        return None
    return None
