"""Check a finished file against what Premiere Pro and After Effects need.

The result is shown on every finished item, so a pass means the file was
actually measured, not assumed.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from .probe import MediaInfo, Timing, moov_before_mdat


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""
    critical: bool = True


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)
    premiere: bool = False
    after_effects: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def failed(self) -> list[Check]:
        return [c for c in self.checks if not c.ok and c.critical]

    def to_dict(self) -> dict:
        return {"checks": [asdict(c) for c in self.checks], "premiere": self.premiere,
                "after_effects": self.after_effects, "notes": self.notes}

    @classmethod
    def from_dict(cls, d: dict | None) -> "Report | None":
        if not d:
            return None
        return cls(checks=[Check(**c) for c in d.get("checks", [])], premiere=bool(d.get("premiere")),
                   after_effects=bool(d.get("after_effects")), notes=list(d.get("notes", [])))


def verify(mi: MediaInfo, profile: str, expected: dict, timing: Timing | None,
           expected_duration: float = 0.0) -> Report:
    r = Report()
    add = r.checks.append
    v, a = mi.video, mi.audio
    container = mi.container

    if profile in ("h264", "hevc"):
        add(Check("Container", container == "mp4", container.upper()))
        add(Check("Index at start", moov_before_mdat(mi.path) is True, "fast start", critical=False))
    elif profile in ("prores", "dnxhr"):
        add(Check("Container", container in ("mov", "mp4"), container.upper()))
    elif profile == "wav":
        add(Check("Container", "wav" in mi.format_name, mi.format_name))
    elif profile == "aac":
        add(Check("Container", container == "mp4", "M4A"))
    elif profile == "mp3":
        add(Check("Container", "mp3" in mi.format_name, "MP3"))

    if profile in ("h264", "hevc", "prores", "dnxhr"):
        if v is None:
            add(Check("Video stream", False, "missing"))
        else:
            want = {"h264": "h264", "hevc": "hevc", "prores": "prores", "dnxhr": "dnxhd"}[profile]
            add(Check("Video codec", v.codec == want, v.codec))
            if profile == "h264":
                add(Check("8-bit 4:2:0", v.pix_fmt in ("yuv420p", "yuvj420p"), v.pix_fmt))
            elif profile == "hevc":
                add(Check("Pixel format", v.pix_fmt in ("yuv420p10le", "yuv420p", "p010le"), v.pix_fmt))
            elif profile == "prores":
                add(Check("4:2:2 10-bit", v.pix_fmt.startswith("yuv422p10"), v.pix_fmt))
            add(Check("Even frame size", v.width % 2 == 0 and v.height % 2 == 0, f"{v.width}×{v.height}"))
            add(Check("Square pixels", v.square_pixels, v.sar or "1:1"))
            if timing is not None and timing.samples >= 10:
                add(Check("Constant frame rate", not timing.vfr,
                          "constant" if not timing.vfr else f"varies {timing.spread * 100:.1f}%"))
            else:
                same = (v.avg_rate and v.r_rate and abs(float(v.avg_rate) - float(v.r_rate)) < 0.01)
                add(Check("Constant frame rate", bool(same) or not v.avg_rate, "header", critical=False))
            known = [x for x in (v.color_primaries, v.color_transfer, v.color_space) if x and x != "unknown"]
            add(Check("Color tagged", len(known) == 3, v.color_space if len(known) == 3 else "partly untagged",
                      critical=False))
            if expected.get("width") and expected.get("height"):
                add(Check("Frame size", (v.width, v.height) == (expected["width"], expected["height"]),
                          f"{v.width}×{v.height}", critical=False))
            extra = [s for s in mi.streams if s.kind not in ("video", "audio") or s.attached_pic]
            add(Check("No extra tracks", not extra, ", ".join(s.kind for s in extra) or "clean"))

    if a is not None:
        want_a = expected.get("acodec")
        if want_a:
            add(Check("Audio codec", a.codec == want_a, a.codec))
        if profile in ("h264", "hevc", "aac") and a.codec == "aac" and a.profile:
            add(Check("AAC-LC", "LC" in a.profile, a.profile))
        if expected.get("sample_rate"):
            add(Check("Sample rate", a.sample_rate == expected["sample_rate"], f"{a.sample_rate} Hz",
                      critical=False))
    elif profile in ("wav", "aac", "mp3"):
        add(Check("Audio stream", False, "missing"))

    if expected_duration > 1 and mi.duration > 0:
        drift = abs(mi.duration - expected_duration)
        add(Check("Full length", drift < max(1.5, expected_duration * 0.02),
                  f"{mi.duration:.1f} s of {expected_duration:.1f} s", critical=False))

    ok = not r.failed and bool(r.checks)
    r.premiere = ok
    r.after_effects = ok
    if ok and profile == "h264":
        r.notes.append("Long-GOP H.264 scrubs slower in After Effects than ProRes or DNxHR")
    if ok and profile == "hevc":
        r.notes.append("HEVC plays smoothly with hardware decoding (Intel, NVIDIA, AMD)")
    return r
