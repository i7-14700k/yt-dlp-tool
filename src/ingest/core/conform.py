"""Decide how to turn downloaded streams into files Premiere Pro and After Effects open.

The planner looks at what ffprobe measured, compares it with what the chosen
output profile requires, and copies every stream it can. It only re-encodes
for a concrete reason, and records that reason so the UI can show it.

What both Adobe apps need from H.264 files, in practice:
  * H.264 High/Main, 8-bit 4:2:0, in MP4, index at the front
  * a constant frame rate (variable rate drifts audio out of sync)
  * even dimensions and square pixels
  * BT.709 color tags (untagged media is guessed, often wrongly)
  * AAC-LC audio; 48 kHz is the video standard
  * no cover-art stream, no chapter text track, no data tracks
VP9, AV1, Opus and WebM/MKV are not supported by either app (confirmed again in
Premiere 26.x), so those always convert.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction

from .probe import STANDARD_RATES, MediaInfo, Stream, Timing, rate_str, snap_rate
from .settings import OutputOptions
from .util import fps_label


@dataclass(frozen=True)
class Profile:
    id: str
    label: str
    detail: str
    kind: str          # video | audio | original
    ext: str
    muxer: str
    premiere: bool = True
    after_effects: bool = True
    note: str = ""


PROFILES: dict[str, Profile] = {p.id: p for p in [
    Profile("h264", "H.264", "MP4 · AAC 48 kHz", "video", "mp4", "mp4",
            note="Smallest files. Opens everywhere."),
    Profile("prores", "ProRes 422", "MOV · 10-bit · PCM", "video", "mov", "mov",
            note="Intra-frame. Smooth scrubbing in After Effects. Large."),
    Profile("dnxhr", "DNxHR", "MOV · PCM", "video", "mov", "mov",
            note="Intra-frame. Fast timeline playback in Premiere. Large."),
    Profile("hevc", "HEVC", "MP4 · 10-bit · AAC", "video", "mp4", "mp4",
            note="Keeps HDR. Needs hardware HEVC decode for smooth playback."),
    Profile("wav", "WAV", "PCM · 48 kHz", "audio", "wav", "wav",
            note="Uncompressed audio for editing."),
    Profile("aac", "AAC", "M4A · 48 kHz", "audio", "m4a", "ipod",
            note="Compressed audio, opens in every Adobe app."),
    Profile("mp3", "MP3", "320 kb/s", "audio", "mp3", "mp3",
            note="Compressed audio for sharing."),
    Profile("original", "Original", "No conversion", "original", "", "",
            premiere=False, after_effects=False,
            note="Exactly what the site serves. May not import."),
]}

PRORES_PROFILES = {"proxy": (0, "ProRes 422 Proxy"), "lt": (1, "ProRes 422 LT"),
                   "standard": (2, "ProRes 422"), "hq": (3, "ProRes 422 HQ")}
DNXHR_PROFILES = {"lb": ("dnxhr_lb", "DNxHR LB", "yuv422p"), "sq": ("dnxhr_sq", "DNxHR SQ", "yuv422p"),
                  "hq": ("dnxhr_hq", "DNxHR HQ", "yuv422p"), "hqx": ("dnxhr_hqx", "DNxHR HQX", "yuv422p10le")}

H264_QUALITY = {"max": (14, 15, 16, 14), "high": (17, 19, 20, 18),
                "balanced": (20, 23, 23, 21), "compact": (23, 26, 26, 24)}
HEVC_QUALITY = {"max": (16, 17, 18, 16), "high": (19, 21, 22, 20),
                "balanced": (22, 25, 25, 24), "compact": (25, 28, 28, 27)}
_ENC_INDEX = {"x264": 0, "x265": 0, "nvenc": 1, "qsv": 2, "amf": 3}

CODEC_NAMES = {
    "h264": "H.264", "hevc": "HEVC", "vp9": "VP9", "vp8": "VP8", "av1": "AV1", "prores": "ProRes",
    "dnxhd": "DNxHR", "mpeg4": "MPEG-4", "mpeg2video": "MPEG-2", "theora": "Theora", "flv1": "Sorenson",
    "aac": "AAC", "opus": "Opus", "vorbis": "Vorbis", "mp3": "MP3", "flac": "FLAC", "ac3": "AC-3",
    "eac3": "E-AC-3", "alac": "ALAC", "pcm_s16le": "PCM 16-bit", "pcm_s24le": "PCM 24-bit",
    "pcm_f32le": "PCM float", "mjpeg": "Motion JPEG", "gif": "GIF", "dts": "DTS",
}

# Hardware H.264 encoders stop at 4096 on the long side (NVENC, AMF and QSV alike).
HW_H264_MAX = 4096
HW_HEVC_MAX = 8192
_MP4_AUDIO_COPY = {"aac", "mp3", "ac3", "eac3", "alac"}


def codec_name(codec: str) -> str:
    return CODEC_NAMES.get(codec, codec.upper() if codec else "unknown")


@dataclass
class Caps:
    """What the installed ffmpeg can do. Filled in by encoders.detect()."""
    hw_h264: list[str] = field(default_factory=list)   # e.g. ["nvenc", "qsv"]
    hw_hevc: list[str] = field(default_factory=list)
    zscale: bool = True
    libx265: bool = True
    libmp3lame: bool = True


@dataclass
class Plan:
    profile: str
    kind: str
    ext: str
    muxer: str
    inputs: list[str]
    args: list[str]                   # everything after "ffmpeg", minus the output path
    passthrough: bool = False         # nothing to do: move the single input as-is
    video_action: str | None = None   # copy | encode | None
    audio_action: str | None = None
    encoder: str = ""
    reasons: list[str] = field(default_factory=list)
    steps: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    out: dict = field(default_factory=dict)
    duration: float = 0.0
    fallback: "Plan | None" = None

    @property
    def summary(self) -> str:
        return " · ".join(self.steps)


class PlanError(RuntimeError):
    pass


# ---------------------------------------------------------------- helpers

def _pick_streams(inputs: list[MediaInfo]) -> tuple[tuple[int, Stream] | None, tuple[int, Stream] | None]:
    video = audio = None
    for i, mi in enumerate(inputs):
        v = mi.video
        if v and video is None:
            video = (i, v)
    # Prefer audio from an audio-only file (DASH/HLS split downloads), else first audio anywhere.
    for i, mi in enumerate(inputs):
        if mi.audio and not mi.video:
            audio = (i, mi.audio)
            break
    if audio is None:
        for i, mi in enumerate(inputs):
            if mi.audio:
                audio = (i, mi.audio)
                break
    return video, audio


def _display_dims(v: Stream) -> tuple[int, int]:
    w, h = v.width, v.height
    if not v.near_square:
        try:
            num, den = (int(x) for x in v.sar.split(":"))
            if num > 0 and den > 0:
                w = round(w * num / den)
        except ValueError:
            pass
    if v.rotation in (90, 270):
        w, h = h, w
    return w, h


def _even(x: float) -> int:
    return max(2, int(round(x / 2.0)) * 2)


def _target_dims(v: Stream, opts: OutputOptions) -> tuple[int, int, bool]:
    """Output size and whether it differs from the stored frame size."""
    dw, dh = _display_dims(v)
    cap = int(opts.resolution) if opts.resolution.isdigit() else 0
    short = min(dw, dh)
    if cap and short > cap:
        scale = cap / short
        dw, dh = dw * scale, dh * scale
    ow, oh = _even(dw), _even(dh)
    stored = (v.height, v.width) if v.rotation in (90, 270) else (v.width, v.height)
    return ow, oh, (ow, oh) != stored or not v.near_square


def _target_rate(v: Stream, opts: OutputOptions, timing: Timing | None) -> Fraction:
    if opts.fps not in ("", "source"):
        fixed = {"23.976": Fraction(24000, 1001), "29.97": Fraction(30000, 1001),
                 "59.94": Fraction(60000, 1001)}
        return fixed.get(opts.fps) or Fraction(opts.fps)
    measured = None
    if timing and timing.vfr and timing.measured_rate:
        # Variable rate: the most common frame interval is the rate the material was shot at.
        # The average is skewed by drops and splices (a 23.976 intro on a 29.97 talk averages 27.97).
        measured = timing.measured_rate
    src = measured or v.avg_rate or v.r_rate or Fraction(30)
    rate = snap_rate(src) or Fraction(30)
    if opts.max_fps.isdigit() and float(rate) > int(opts.max_fps) + 0.5:
        cap = int(opts.max_fps)
        # Drop whole frames where possible: 50 -> 25, 59.94 -> 29.97, 120 -> 60 or 30.
        for d in range(2, 9):
            r = rate / d
            if float(r) <= cap + 0.01 and snap_rate(r) == r and r in STANDARD_RATES:
                rate = r
                break
        else:
            rate = Fraction(cap)
    return rate


def _gop(rate: Fraction, opts: OutputOptions) -> int | None:
    if opts.gop == "auto":
        return None
    secs = 2 if opts.gop == "2s" else 1
    return max(1, int(round(float(rate) * secs)))


def _matrix_name(v: Stream, height: int) -> str:
    cs = v.color_space
    if cs in ("bt709",):
        return "bt709"
    if cs in ("smpte170m", "bt470bg", "bt601", "fcc"):
        return "bt601"
    if cs in ("bt2020nc", "bt2020c"):
        return "bt2020"
    return "bt709" if height >= 720 else "bt601"


def _color_tags(hdr_keep: bool, src: Stream | None) -> list[str]:
    if hdr_keep and src is not None:
        trc = src.color_transfer if src.color_transfer in ("smpte2084", "arib-std-b67") else "smpte2084"
        return ["-color_primaries", "bt2020", "-color_trc", trc, "-colorspace", "bt2020nc", "-color_range", "tv"]
    return ["-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709", "-color_range", "tv"]


def _setparams(hdr_keep: bool, src: Stream | None) -> str:
    if hdr_keep and src is not None:
        trc = src.color_transfer if src.color_transfer in ("smpte2084", "arib-std-b67") else "smpte2084"
        return f"setparams=color_primaries=bt2020:color_trc={trc}:colorspace=bt2020nc:range=tv"
    return "setparams=color_primaries=bt709:color_trc=bt709:colorspace=bt709:range=tv"


# How much more bitrate H.264/HEVC needs than the source codec for the same picture.
_EFFICIENCY = {"h264": 1.4, "hevc": 2.0, "vp9": 2.2, "av1": 2.6, "vp8": 1.6, "mpeg4": 1.3, "mpeg2video": 1.0}


def _bitrate_cap(fam: str, v: Stream, vin: MediaInfo, target: str, out_dims: tuple[int, int],
                 rate: Fraction) -> list[str]:
    """A ceiling so re-encoding never balloons a file far past what the source held.

    CRF 17 on a 1 Mb/s 480p stream lands near 2 Mb/s: twice the size for no visible gain.
    Only CRF/CQ encoders take a ceiling cleanly (x264, x265, NVENC).
    """
    if fam not in ("x264", "x265", "nvenc"):
        return []
    src = v.bitrate
    if not src and vin.bitrate and vin.duration:
        audio = sum(s.bitrate or 0 for s in vin.streams if s.kind == "audio")
        src = max(0, vin.bitrate - audio)
    if not src or src < 50_000:
        return []
    factor = _EFFICIENCY.get(v.codec, 1.6) * (0.65 if target == "hevc" else 1.0)
    # Scale for resolution and frame-rate changes made during the conversion.
    src_px = max(1, v.width * v.height) * (v.fps or float(rate))
    out_px = max(1, out_dims[0] * out_dims[1]) * float(rate)
    ceiling = src * factor * min(1.0, out_px / src_px) if src_px else src * factor
    ceiling = max(ceiling, 1_500_000 if out_dims[1] >= 720 else 800_000)
    kbps = int(ceiling / 1000)
    return ["-maxrate", f"{kbps}k", "-bufsize", f"{kbps * 2}k"]


def _trimmed_length(trim: tuple[float, float], segments) -> float:
    length = max(0.0, trim[1] - trim[0])
    if segments:
        length -= sum(max(0.0, min(b, length) - max(a, 0.0)) for a, b in segments)
    return max(0.04, length)


def _sponsor_expr(segments: list[tuple[float, float]]) -> str:
    parts = "+".join(f"between(t\\,{a:.3f}\\,{b:.3f})" for a, b in segments)
    return f"not({parts})"


def _audio_channels(a: Stream, opts: OutputOptions) -> int:
    if opts.audio_channels == "stereo":
        return 2
    if opts.audio_channels == "mono":
        return 1
    return max(1, min(a.channels or 2, 8))


def _choose_hw(kind: str, opts: OutputOptions, caps: Caps, w: int, h: int) -> tuple[str, str | None]:
    """Return (encoder family, note). Family is x264/x265 or nvenc/qsv/amf."""
    soft = "x264" if kind == "h264" else "x265"
    available = caps.hw_h264 if kind == "h264" else caps.hw_hevc
    limit = HW_H264_MAX if kind == "h264" else HW_HEVC_MAX
    want = opts.encoder
    if want in ("x264", "x265", "cpu"):
        return soft, None
    if want in ("nvenc", "qsv", "amf"):
        if want not in available:
            return soft, f"{want.upper()} isn't available on this PC, using the CPU encoder"
        if max(w, h) > limit:
            return soft, f"{w}×{h} is past the hardware encoder's limit, using the CPU encoder"
        return want, None
    # auto
    for fam in ("nvenc", "qsv", "amf"):
        if fam in available and max(w, h) <= limit:
            return fam, None
    if kind == "hevc" and not caps.libx265 and available:
        return available[0], None
    return soft, None


def _h264_args(fam: str, opts: OutputOptions, gop: int | None) -> tuple[str, list[str], str]:
    q = H264_QUALITY.get(opts.quality, H264_QUALITY["high"])[_ENC_INDEX[fam]]
    g = ["-g", str(gop)] if gop else []
    if fam == "nvenc":
        return "h264_nvenc", ["-c:v", "h264_nvenc", "-preset", "p5", "-tune", "hq", "-rc", "vbr", "-cq", str(q),
                              "-b:v", "0", "-profile:v", "high", "-spatial_aq", "1", *g], "yuv420p"
    if fam == "qsv":
        return "h264_qsv", ["-c:v", "h264_qsv", "-preset", "slow", "-global_quality", str(q),
                            "-profile:v", "high", *g], "nv12"
    if fam == "amf":
        return "h264_amf", ["-c:v", "h264_amf", "-quality", "quality", "-rc", "cqp", "-qp_i", str(q),
                            "-qp_p", str(q + 2), "-qp_b", str(q + 4), "-profile:v", "high", *g], "nv12"
    preset = opts.speed if opts.speed in ("ultrafast", "superfast", "veryfast", "faster", "fast",
                                          "medium", "slow", "slower", "veryslow") else "medium"
    return "libx264", ["-c:v", "libx264", "-preset", preset, "-crf", str(q), "-profile:v", "high", *g], "yuv420p"


def _hevc_args(fam: str, opts: OutputOptions, gop: int | None, hdr_keep: bool, src: Stream) -> tuple[str, list[str], str]:
    q = HEVC_QUALITY.get(opts.quality, HEVC_QUALITY["high"])[_ENC_INDEX[fam]]
    g = ["-g", str(gop)] if gop else []
    tag = ["-tag:v", "hvc1"]
    if fam == "nvenc":
        return "hevc_nvenc", ["-c:v", "hevc_nvenc", "-preset", "p5", "-tune", "hq", "-rc", "vbr", "-cq", str(q),
                              "-b:v", "0", "-profile:v", "main10", *g, *tag], "p010le"
    if fam == "qsv":
        return "hevc_qsv", ["-c:v", "hevc_qsv", "-preset", "slow", "-global_quality", str(q),
                            "-profile:v", "main10", *g, *tag], "p010le"
    if fam == "amf":
        return "hevc_amf", ["-c:v", "hevc_amf", "-quality", "quality", "-rc", "cqp", "-qp_i", str(q),
                            "-qp_p", str(q + 2), "-profile:v", "main10", *g, *tag], "p010le"
    preset = opts.speed if opts.speed in ("ultrafast", "superfast", "veryfast", "faster", "fast",
                                          "medium", "slow", "slower", "veryslow") else "medium"
    params = ["log-level=error"]
    if gop:
        params.append(f"keyint={gop}")
    if hdr_keep:
        trc = src.color_transfer if src.color_transfer in ("smpte2084", "arib-std-b67") else "smpte2084"
        params += ["colorprim=bt2020", f"transfer={trc}", "colormatrix=bt2020nc", "hdr10-opt=1", "repeat-headers=1"]
    return "libx265", ["-c:v", "libx265", "-preset", preset, "-crf", str(q), "-profile:v", "main10",
                       "-x265-params", ":".join(params), *tag], "yuv420p10le"


# ---------------------------------------------------------------- planning

def plan(inputs: list[MediaInfo], opts: OutputOptions, caps: Caps | None = None,
         timing: Timing | None = None, title: str = "", source_url: str = "",
         trim: tuple[float, float] | None = None,
         sponsor_segments: list[tuple[float, float]] | None = None,
         force_encoder: str | None = None) -> Plan:
    caps = caps or Caps()
    if not inputs:
        raise PlanError("Nothing was downloaded.")
    profile = PROFILES.get(opts.profile, PROFILES["h264"])
    video, audio = _pick_streams(inputs)
    if video is None and audio is None:
        raise PlanError("The download has no audio or video streams.")

    if profile.kind == "original":
        return _plan_original(inputs, video, audio)

    if profile.kind == "video" and video is None:
        # Audio-only source (a podcast, SoundCloud): deliver audio the profile's apps take.
        fallback = "aac" if profile.id in ("h264", "hevc") else "wav"
        p = plan(inputs, _with(opts, profile=fallback), caps, timing, title, source_url, trim, sponsor_segments)
        p.warnings.insert(0, "No video in this source, saved as audio")
        return p

    if profile.kind == "audio":
        return _plan_audio(inputs, audio, profile, opts, title, source_url, trim, sponsor_segments)

    return _plan_video(inputs, video, audio, profile, opts, caps, timing, title, source_url, trim,
                       sponsor_segments, force_encoder)


def _with(opts: OutputOptions, **kw) -> OutputOptions:
    from .settings import copy_options
    return copy_options(opts, **kw)


def _common_head(inputs: list[MediaInfo]) -> list[str]:
    args = ["-hide_banner", "-nostdin", "-y", "-loglevel", "warning", "-progress", "pipe:1", "-nostats"]
    for mi in inputs:
        args += ["-i", mi.path]
    return args


def _metadata_args(title: str, url: str) -> list[str]:
    out = ["-map_metadata", "-1", "-map_chapters", "-1"]
    if title:
        out += ["-metadata", f"title={title}"]
    if url:
        out += ["-metadata", f"comment={url}"]
    return out


def _expected_duration(inputs: list[MediaInfo], video, audio, trim, segments) -> float:
    durs = []
    if video:
        mi = inputs[video[0]]
        durs.append(video[1].duration or mi.duration)
    if audio:
        mi = inputs[audio[0]]
        durs.append(audio[1].duration or mi.duration)
    d = max([x for x in durs if x] or [0.0])
    if trim:
        d = min(d, max(0.0, trim[1] - trim[0])) if d else max(0.0, trim[1] - trim[0])
    if segments:
        d -= sum(max(0.0, b - a) for a, b in segments)
    return max(0.0, d)


def _plan_original(inputs, video, audio) -> Plan:
    if len(inputs) == 1:
        mi = inputs[0]
        ext = mi.path.rsplit(".", 1)[-1].lower() if "." in mi.path else "bin"
        return Plan("original", "original", ext, "", [mi.path], [], passthrough=True,
                    steps=["Keep original file"], duration=mi.duration,
                    out={"container": mi.container})
    vcodec = video[1].codec if video else ""
    acodec = audio[1].codec if audio else ""
    if vcodec in ("h264", "hevc", "av1", "") and acodec in ("aac", "mp3", "ac3", "eac3", "alac", ""):
        ext, muxer = "mp4", "mp4"
    elif vcodec in ("vp8", "vp9", "av1", "") and acodec in ("opus", "vorbis", ""):
        ext, muxer = "webm", "webm"
    else:
        ext, muxer = "mkv", "matroska"
    args = _common_head(inputs)
    if video:
        args += ["-map", f"{video[0]}:{video[1].index}"]
    if audio:
        args += ["-map", f"{audio[0]}:{audio[1].index}"]
    args += ["-c", "copy"]
    if muxer == "mp4":
        args += ["-movflags", "+faststart"]
    args += ["-f", muxer]
    return Plan("original", "original", ext, muxer, [m.path for m in inputs], args,
                video_action="copy" if video else None, audio_action="copy" if audio else None,
                steps=[f"Combine {codec_name(vcodec)} + {codec_name(acodec)} into {ext.upper()}"],
                duration=_expected_duration(inputs, video, audio, None, None),
                out={"container": muxer})


def _audio_encode_args(profile_id: str, opts: OutputOptions, channels: int) -> tuple[str, list[str]]:
    if profile_id in ("wav", "prores", "dnxhr"):
        depth = 24 if opts.pcm_depth == 24 else 16
        return f"pcm_s{depth}le", ["-c:a", f"pcm_s{depth}le"]
    if profile_id == "mp3":
        return "mp3", ["-c:a", "libmp3lame", "-b:a", f"{min(320, max(128, opts.audio_bitrate))}k"]
    # AAC: scale the stereo bitrate for surround, capped where the encoder stops gaining.
    br = max(128, min(512, opts.audio_bitrate))
    if channels > 2:
        br = min(768, int(br * channels / 2))
    return "aac", ["-c:a", "aac", "-b:a", f"{br}k"]


def _audio_plan(a: Stream, inp: MediaInfo, profile_id: str, opts: OutputOptions, container: str,
                segments, force_encode: bool) -> tuple[str, list[str], list[str], dict]:
    """Return (action, args, reasons, out-props) for the audio stream."""
    channels = _audio_channels(a, opts)
    target_rate = 48000 if opts.audio_rate == "48000" else (a.sample_rate or 48000)
    if profile_id == "mp3" and target_rate not in (32000, 44100, 48000):
        target_rate = 48000
    target_codec, enc = _audio_encode_args(profile_id, opts, channels)
    reasons = []
    if force_encode:
        reasons.append("cut")
    if a.codec != target_codec and not (target_codec == "aac" and a.codec == "aac"):
        reasons.append(f"{codec_name(a.codec)} audio isn't supported" if a.codec in ("opus", "vorbis", "flac")
                       else f"{codec_name(a.codec)} to {codec_name(target_codec)}")
    if target_codec == "aac" and a.codec == "aac" and a.profile and "LC" not in a.profile:
        reasons.append(f"AAC {a.profile} to AAC-LC")
    if a.sample_rate and a.sample_rate != target_rate:
        reasons.append(f"{a.sample_rate / 1000:g} kHz to {target_rate / 1000:g} kHz")
    if channels != (a.channels or channels):
        reasons.append(f"{a.channels} to {channels} channels")
    if opts.loudness != "off":
        reasons.append(f"loudness {opts.loudness} LUFS")
    if segments:
        reasons.append("sponsor segments removed")
    if container in ("mp4", "mov", "ipod") and a.codec not in _MP4_AUDIO_COPY and target_codec == a.codec:
        reasons.append("container")
    props = {"acodec": target_codec, "sample_rate": target_rate, "channels": channels}
    if not reasons:
        return "copy", ["-c:a", "copy"], [], {**props, "sample_rate": a.sample_rate, "channels": a.channels}
    af = []
    if segments:
        af.append(f"aselect='{_sponsor_expr(segments)}',asetpts=N/SR/TB")
    if opts.loudness != "off":
        af.append(f"loudnorm=I={opts.loudness}:TP=-1.0:LRA=11")
    af.append(f"aresample={target_rate}:async=1:first_pts=0")
    args = ["-af", ",".join(af), *enc, "-ar", str(target_rate)]
    if channels != a.channels:
        args += ["-ac", str(channels)]
    return "encode", args, reasons, props


def _plan_audio(inputs, audio, profile: Profile, opts, title, url, trim, segments) -> Plan:
    if audio is None:
        raise PlanError("This source has no audio track.")
    ai, a = audio
    args = _common_head(inputs) + ["-map", f"{ai}:{a.index}", "-vn", "-sn", "-dn"]
    action, aargs, reasons, props = _audio_plan(a, inputs[ai], profile.id, opts, profile.muxer, segments,
                                                force_encode=bool(trim))
    args += aargs
    if trim:
        args += ["-t", f"{_trimmed_length(trim, segments):.3f}"]
    args += _metadata_args(title, url)
    if profile.muxer == "ipod":
        args += ["-movflags", "+faststart"]
    if profile.muxer == "wav":
        args += ["-rf64", "auto"]
    if profile.muxer == "mp3":
        args += ["-id3v2_version", "3"]
    args += ["-f", profile.muxer]
    step = (f"Copy {codec_name(a.codec)} audio" if action == "copy"
            else f"Encode {codec_name(props['acodec'])} {props['sample_rate'] / 1000:g} kHz")
    return Plan(profile.id, "audio", profile.ext, profile.muxer, [m.path for m in inputs], args,
                audio_action=action, reasons=reasons, steps=[step],
                duration=_expected_duration(inputs, None, audio, trim, segments),
                out={"container": profile.muxer, **props})


def _plan_video(inputs, video, audio, profile: Profile, opts: OutputOptions, caps: Caps, timing: Timing | None,
                title, url, trim, segments, force_encoder) -> Plan:
    vi, v = video
    vin = inputs[vi]
    reasons: list[str] = []
    warnings: list[str] = []
    out_w, out_h, resized = _target_dims(v, opts)
    rate = _target_rate(v, opts, timing)
    src_rate = v.avg_rate or v.r_rate
    hdr = v.is_hdr
    hdr_keep = hdr and opts.hdr == "keep" and profile.id in ("hevc", "prores", "dnxhr")
    if profile.id == "dnxhr" and hdr_keep and opts.dnxhr != "hqx":
        hdr_keep = False
        warnings.append("HDR needs DNxHR HQX to stay HDR, tone-mapped to SDR instead")
    tonemap = hdr and not hdr_keep
    if tonemap and not caps.zscale:
        warnings.append("This ffmpeg can't tone-map HDR, colors will look flat")
        tonemap = False

    # ---- can the video stream be copied?
    if profile.id == "h264":
        if v.codec != "h264":
            reasons.append(f"{codec_name(v.codec)} isn't supported by Premiere or After Effects"
                           if v.codec in ("vp9", "av1", "vp8") else f"{codec_name(v.codec)} to H.264")
        else:
            if v.bit_depth > 8 or v.chroma != "4:2:0":
                reasons.append(f"{v.bit_depth}-bit {v.chroma} H.264 decodes poorly in Adobe apps")
            if v.profile and not any(k in v.profile for k in ("High", "Main", "Baseline")):
                reasons.append(f"H.264 {v.profile} profile")
            if v.profile and ("High 10" in v.profile or "4:2:2" in v.profile or "4:4:4" in v.profile):
                if not any("H.264" in r for r in reasons):
                    reasons.append(f"H.264 {v.profile} profile")
            if vin.container in ("matroska", "webm", "flv"):
                reasons.append(f"{vin.container.upper()} timestamps are rounded, rebuilt as constant rate")
    elif profile.id == "hevc":
        if v.codec != "hevc":
            reasons.append(f"{codec_name(v.codec)} to HEVC")
        elif vin.container in ("matroska", "webm", "flv"):
            reasons.append("container timestamps are rounded")
    elif profile.id == "prores":
        if not (v.codec == "prores" and v.profile and PRORES_PROFILES[opts.prores][1].split()[-1] in v.profile):
            reasons.append(f"{codec_name(v.codec)} to {PRORES_PROFILES.get(opts.prores, PRORES_PROFILES['hq'])[1]}")
    elif profile.id == "dnxhr":
        reasons.append(f"{codec_name(v.codec)} to {DNXHR_PROFILES.get(opts.dnxhr, DNXHR_PROFILES['hq'])[1]}")

    if timing and timing.vfr:
        reasons.append("variable frame rate, conformed to constant")
    if src_rate and abs(float(src_rate) - float(rate)) > 0.01 and not (timing and timing.vfr):
        reasons.append(f"{fps_label(float(src_rate))} to {fps_label(float(rate))} fps")
    if resized:
        if not v.near_square:
            reasons.append("non-square pixels")
        elif v.width % 2 or v.height % 2:
            reasons.append("odd frame size")
        else:
            reasons.append(f"scaled to {min(out_w, out_h)}p")
    if v.rotation:
        reasons.append("rotation baked in")
    if tonemap:
        reasons.append("HDR tone-mapped to SDR")
    if trim:
        reasons.append("frame-accurate trim")
    if segments:
        reasons.append("sponsor segments removed")

    copy_video = not reasons
    gop = _gop(rate, opts)
    args = _common_head(inputs)
    args += ["-map", f"{vi}:{v.index}"]
    if audio:
        args += ["-map", f"{audio[0]}:{audio[1].index}"]
    args += ["-sn", "-dn"]

    encoder = ""
    pix_fmt = ""
    fallback_family = None
    if copy_video:
        args += ["-c:v", "copy"]
        # Fix header-only problems in the bitstream itself, without re-encoding.
        meta = []
        if not v.color_primaries and not v.color_transfer and not v.color_space:
            meta.append("colour_primaries=1:transfer_characteristics=1:matrix_coefficients=1")
        if not v.square_pixels:
            meta.append("sample_aspect_ratio=1/1")
        if meta and v.codec in ("h264", "hevc"):
            args += ["-bsf:v", f"{v.codec}_metadata=" + ":".join(meta)]
        if not v.square_pixels:
            args += ["-aspect", f"{v.width}:{v.height}"]
        if v.codec == "hevc":
            args += ["-tag:v", "hvc1"]
        step_v = f"Copy {codec_name(v.codec)} {min(out_w, out_h)}p {fps_label(float(src_rate or rate))}"
        out_rate = src_rate or rate
    else:
        filters = []
        if v.is_interlaced:
            filters.append("bwdif=mode=send_frame:parity=auto:deint=interlaced")
        if tonemap:
            tin = v.color_transfer if v.color_transfer in ("smpte2084", "arib-std-b67") else "smpte2084"
            filters.append(
                f"zscale=tin={tin}:min=bt2020nc:pin=bt2020:rin=tv:t=linear:npl=100,format=gbrpf32le,"
                f"zscale=p=bt709,tonemap=tonemap=hable:desat=0,"
                f"zscale=w={out_w}:h={out_h}:f=lanczos:t=bt709:m=bt709:r=tv")
        elif hdr_keep:
            if resized:
                filters.append(f"scale={out_w}:{out_h}:flags=lanczos")
        else:
            matrix_in = _matrix_name(v, v.height)
            color = f"in_color_matrix={matrix_in}:out_color_matrix=bt709"
            rng = "in_range=pc:out_range=tv" if v.full_range else "in_range=tv:out_range=tv"
            if resized or matrix_in != "bt709" or v.full_range:
                filters.append(f"scale={out_w}:{out_h}:flags=lanczos:{color}:{rng}")
        filters.append("setsar=1")
        # Plain fps conversion. start_time=0 looked tidier but duplicates frames on mixed-rate
        # sources (TED's 23.976 intro inside a 29.97 talk), so the offset is left to the muxer.
        filters.append(f"fps=fps={rate_str(rate)}")
        if segments:
            filters.append(f"select='{_sponsor_expr(segments)}',setpts=N/FRAME_RATE/TB")

        if profile.id == "h264":
            fam, note = _choose_hw("h264", opts if not force_encoder else _with(opts, encoder=force_encoder),
                                   caps, out_w, out_h)
            if note:
                warnings.append(note)
            encoder, venc, pix_fmt = _h264_args(fam, opts, gop)
            venc += _bitrate_cap(fam, v, vin, "h264", (out_w, out_h), rate)
            if fam != "x264":
                fallback_family = "x264"
            step_v = f"Encode H.264 {min(out_w, out_h)}p {fps_label(float(rate))}"
        elif profile.id == "hevc":
            fam, note = _choose_hw("hevc", opts if not force_encoder else _with(opts, encoder=force_encoder),
                                   caps, out_w, out_h)
            if note:
                warnings.append(note)
            if fam == "x265" and not caps.libx265:
                raise PlanError("This ffmpeg build has no HEVC encoder.")
            encoder, venc, pix_fmt = _hevc_args(fam, opts, gop, hdr_keep, v)
            venc += _bitrate_cap(fam, v, vin, "hevc", (out_w, out_h), rate)
            if fam != "x265":
                fallback_family = "x265"
            step_v = f"Encode HEVC 10-bit {min(out_w, out_h)}p {fps_label(float(rate))}"
        elif profile.id == "prores":
            num, label = PRORES_PROFILES.get(opts.prores, PRORES_PROFILES["hq"])
            encoder = "prores_ks"
            pix_fmt = "yuv422p10le"
            venc = ["-c:v", "prores_ks", "-profile:v", str(num), "-vendor", "apl0"]
            step_v = f"Encode {label} {min(out_w, out_h)}p {fps_label(float(rate))}"
        else:
            prof, label, pix_fmt = DNXHR_PROFILES.get(opts.dnxhr, DNXHR_PROFILES["hq"])
            encoder = "dnxhd"
            venc = ["-c:v", "dnxhd", "-profile:v", prof]
            step_v = f"Encode {label} {min(out_w, out_h)}p {fps_label(float(rate))}"
        # Tag the frames themselves: ffmpeg 8 takes VUI color from frame properties, and the
        # -color_* options alone are dropped when the filter graph reports "unknown".
        filters.append(_setparams(hdr_keep, v))
        filters.append(f"format={pix_fmt}")
        args += ["-vf", ",".join(filters), *venc, "-fps_mode", "passthrough", *_color_tags(hdr_keep, v)]
        out_rate = rate

    # ---- audio
    step_a = None
    audio_action = None
    aprops: dict = {}
    if audio:
        ai, a = audio
        audio_action, aargs, areasons, aprops = _audio_plan(
            a, inputs[ai], profile.id, opts, profile.muxer, segments, force_encode=bool(trim) and False)
        args += aargs
        if audio_action == "copy":
            step_a = f"Copy {codec_name(a.codec)}"
        else:
            step_a = f"Encode {codec_name(aprops['acodec'])} {aprops['sample_rate'] / 1000:g} kHz"
            if not copy_video:
                areasons = []  # the video reasons already explain the conversion
        reasons += [r for r in areasons if r not in reasons and r != "container"]

    if trim:
        args += ["-t", f"{_trimmed_length(trim, segments):.3f}"]
    args += _metadata_args(title, url)
    args += ["-max_muxing_queue_size", "4096"]
    if profile.muxer in ("mp4", "mov"):
        args += ["-movflags", "+faststart"]
    args += ["-f", profile.muxer]

    steps = [step_v] + ([step_a] if step_a else [])
    p = Plan(profile.id, "video", profile.ext, profile.muxer, [m.path for m in inputs], args,
             video_action="copy" if copy_video else "encode", audio_action=audio_action,
             encoder=encoder or "copy", reasons=reasons, steps=steps, warnings=warnings,
             duration=_expected_duration(inputs, video, audio, trim, segments),
             out={"container": profile.muxer, "width": out_w, "height": out_h,
                  "rate": rate_str(out_rate) if isinstance(out_rate, Fraction) else str(out_rate),
                  "vcodec": {"h264": "h264", "hevc": "hevc", "prores": "prores", "dnxhr": "dnxhd"}[profile.id],
                  "pix_fmt": pix_fmt, "hdr": hdr_keep, **aprops})
    if fallback_family and not force_encoder:
        p.fallback = _plan_video(inputs, video, audio, profile, opts, caps, timing, title, url, trim, segments,
                                 force_encoder=fallback_family)
    return p


# ---------------------------------------------------------------- size estimates

_REF_PIXEL_RATE = 1920 * 1080 * 29.97
_INTRA_MBPS = {
    "prores": {"proxy": 45, "lt": 102, "standard": 147, "hq": 220},
    "dnxhr": {"lb": 45, "sq": 145, "hq": 220, "hqx": 220},
}
_BPP = {"max": 0.25, "high": 0.15, "balanced": 0.09, "compact": 0.055}


def estimate_size(opts: OutputOptions, width: int, height: int, fps: float, duration: float,
                  source_bytes: float = 0.0, copies_video: bool = False) -> float:
    """Rough output size in bytes, for showing before a download starts."""
    if not duration:
        return 0.0
    prof = opts.profile
    if prof == "wav":
        return duration * 48000 * (opts.pcm_depth // 8) * 2
    if prof in ("aac", "mp3"):
        return duration * min(320, opts.audio_bitrate) * 125
    if prof == "original" or copies_video:
        return source_bytes
    fps = fps or 30.0
    pixel_rate = max(1, width * height) * fps
    if prof in _INTRA_MBPS:
        key = opts.prores if prof == "prores" else opts.dnxhr
        mbps = _INTRA_MBPS[prof].get(key, 220) * pixel_rate / _REF_PIXEL_RATE
        audio = 48000 * (opts.pcm_depth // 8) * 2 * 8
        return duration * (mbps * 1e6 + audio) / 8
    bpp = _BPP.get(opts.quality, 0.15) * (0.6 if prof == "hevc" else 1.0)
    return duration * (pixel_rate * bpp + opts.audio_bitrate * 1000) / 8
