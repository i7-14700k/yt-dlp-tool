"""Generate awkward media with ffmpeg, conform it, and verify the result for Premiere / After Effects."""

import subprocess
from fractions import Fraction

import pytest

from ingest.core import conform, verify
from ingest.core.probe import measure_timing, probe
from ingest.core.settings import OutputOptions, copy_options


def run(cmd):
    r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    assert r.returncode == 0, r.stdout.decode(errors="replace")[-3000:]


def make(ffmpeg, out, *args, dur=3):
    run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", *args, str(out)])
    return str(out)


def lavfi_video(spec):
    return ["-f", "lavfi", "-i", spec]


def lavfi_audio(rate=48000, dur=3, layout="stereo"):
    return ["-f", "lavfi", "-i", f"sine=f=440:d={dur}:r={rate},aformat=channel_layouts={layout}"]


def conform_and_verify(ffmpeg, ffprobe, tmp_path, files, opts=None, **plan_kw):
    opts = opts or OutputOptions()
    infos = [probe(ffprobe, f) for f in files]
    infos.sort(key=lambda m: 0 if m.video else 1)
    timing = None
    v = next((m for m in infos if m.video), None)
    if v and v.video.codec in ("h264", "hevc"):
        timing = measure_timing(ffprobe, v.path)
    caps = conform.Caps(hw_h264=[], hw_hevc=[], zscale=True, libx265=True)
    plan = conform.plan(infos, opts, caps, timing, title="Test clip", source_url="https://example.com/v", **plan_kw)
    out = tmp_path / f"out.{plan.ext}"
    if plan.passthrough:
        return plan, None, None
    run([ffmpeg, *plan.args, str(out)])
    mi = probe(ffprobe, out)
    t = measure_timing(ffprobe, out) if mi.video else None
    rep = verify.verify(mi, plan.profile, plan.out, t, plan.duration)
    return plan, mi, rep


def assert_ok(rep):
    bad = [(c.name, c.detail) for c in rep.checks if not c.ok and c.critical]
    assert rep.premiere and rep.after_effects, bad


def test_vp9_opus_webm_to_h264(ffmpeg, ffprobe, tmp_path):
    src = make(ffmpeg, tmp_path / "yt.webm", *lavfi_video("testsrc2=s=1280x720:r=30:d=3"), *lavfi_audio(),
               "-c:v", "libvpx-vp9", "-b:v", "1M", "-deadline", "realtime", "-cpu-used", "8", "-c:a", "libopus")
    plan, mi, rep = conform_and_verify(ffmpeg, ffprobe, tmp_path, [src])
    assert plan.video_action == "encode" and plan.audio_action == "encode"
    assert any("VP9" in r for r in plan.reasons)
    assert_ok(rep)
    assert mi.video.codec == "h264" and mi.audio.codec == "aac" and mi.audio.sample_rate == 48000
    assert mi.video.avg_rate == Fraction(30)
    assert (mi.video.color_space, mi.video.color_transfer, mi.video.color_primaries) == ("bt709",) * 3


def test_dash_split_streams_copy(ffmpeg, ffprobe, tmp_path):
    v = make(ffmpeg, tmp_path / "v.mp4", *lavfi_video("testsrc2=s=1920x1080:r=30000/1001:d=3"), "-c:v", "libx264",
             "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-an", "-color_primaries", "bt709", "-color_trc",
             "bt709", "-colorspace", "bt709")
    a = make(ffmpeg, tmp_path / "a.m4a", *lavfi_audio(48000), "-c:a", "aac", "-b:a", "128k")
    plan, mi, rep = conform_and_verify(ffmpeg, ffprobe, tmp_path, [a, v])
    assert plan.video_action == "copy" and plan.audio_action == "copy", plan.reasons
    assert_ok(rep)
    assert mi.video.avg_rate == Fraction(30000, 1001)


def test_aac_441_resampled_video_copied(ffmpeg, ffprobe, tmp_path):
    src = make(ffmpeg, tmp_path / "s.mp4", *lavfi_video("testsrc2=s=640x360:r=25:d=3"), *lavfi_audio(44100),
               "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac")
    plan, mi, rep = conform_and_verify(ffmpeg, ffprobe, tmp_path, [src])
    assert plan.video_action == "copy" and plan.audio_action == "encode"
    assert_ok(rep)
    assert mi.audio.sample_rate == 48000
    assert mi.video.color_space == "bt709"  # tagged through h264_metadata without re-encoding


def test_audio_rate_source_keeps_copy(ffmpeg, ffprobe, tmp_path):
    src = make(ffmpeg, tmp_path / "s.mp4", *lavfi_video("testsrc2=s=640x360:r=25:d=3"), *lavfi_audio(44100),
               "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac")
    plan, mi, rep = conform_and_verify(ffmpeg, ffprobe, tmp_path, [src],
                                       copy_options(OutputOptions(), audio_rate="source"))
    assert plan.audio_action == "copy"
    assert_ok(rep)


def test_vfr_h264_becomes_cfr(ffmpeg, ffprobe, tmp_path):
    # 30 fps for one second, then 15 fps: the classic phone-recording pattern.
    tmp = make(ffmpeg, tmp_path / "base.mp4", *lavfi_video("testsrc2=s=640x360:r=30:d=4"), *lavfi_audio(48000, 4),
               "-vf", "setpts='if(lt(N,30),N/30/TB,(1+(N-30)/15)/TB)'", "-fps_mode", "vfr",
               "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac")
    t = measure_timing(ffprobe, tmp)
    assert t.vfr
    plan, mi, rep = conform_and_verify(ffmpeg, ffprobe, tmp_path, [tmp])
    assert plan.video_action == "encode"
    assert any("variable" in r for r in plan.reasons)
    assert_ok(rep)
    assert not measure_timing(ffprobe, mi.path).vfr


def test_mixed_rate_splice_stays_constant(ffmpeg, ffprobe, tmp_path):
    """TED-style: a 23.976 intro spliced onto a 29.97 talk. Must come out strictly constant, no extra frames."""
    a = make(ffmpeg, tmp_path / "a.mp4", *lavfi_video("testsrc2=s=640x360:r=24000/1001:d=3"), "-c:v", "libx264",
             "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-video_track_timescale", "30000")
    b = make(ffmpeg, tmp_path / "b.mp4", *lavfi_video("testsrc2=s=640x360:r=30000/1001:d=6"), "-c:v", "libx264",
             "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-video_track_timescale", "30000")
    lst = tmp_path / "list.txt"
    lst.write_text(f"file '{a}'\nfile '{b}'\n")
    src = tmp_path / "spliced.mp4"
    run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
         "-c", "copy", str(src)])
    assert measure_timing(ffprobe, src).vfr
    plan, mi, rep = conform_and_verify(ffmpeg, ffprobe, tmp_path, [str(src)])
    assert_ok(rep)
    t = measure_timing(ffprobe, mi.path)
    assert not t.vfr
    expected = mi.duration * 30000 / 1001
    assert abs(mi.video.nb_frames - expected) <= 3, (mi.video.nb_frames, expected)


def test_reencode_bitrate_is_capped(ffmpeg, ffprobe, tmp_path):
    from ingest.core.probe import probe as _probe
    src = make(ffmpeg, tmp_path / "s.mkv", *lavfi_video("testsrc2=s=854x480:r=30:d=3"), "-c:v", "libx264",
               "-preset", "ultrafast", "-b:v", "600k", "-pix_fmt", "yuv420p")
    mi = _probe(ffprobe, src)
    plan = conform.plan([mi], OutputOptions(), conform.Caps(), None)
    assert "-maxrate" in plan.args


def test_odd_dimensions(ffmpeg, ffprobe, tmp_path):
    src = make(ffmpeg, tmp_path / "odd.webm", *lavfi_video("testsrc2=s=853x481:r=24:d=2"), "-c:v", "libvpx-vp9",
               "-deadline", "realtime", "-cpu-used", "8", "-an")
    plan, mi, rep = conform_and_verify(ffmpeg, ffprobe, tmp_path, [src])
    assert_ok(rep)
    assert mi.video.width % 2 == 0 and mi.video.height % 2 == 0
    assert mi.audio is None


def test_hdr_pq_tonemapped(ffmpeg, ffprobe, tmp_path):
    src = make(ffmpeg, tmp_path / "hdr.mp4", *lavfi_video("testsrc2=s=1280x720:r=30:d=2"), "-c:v", "libx265",
               "-preset", "ultrafast", "-pix_fmt", "yuv420p10le", "-x265-params",
               "log-level=error:colorprim=bt2020:transfer=smpte2084:colormatrix=bt2020nc",
               "-color_primaries", "bt2020", "-color_trc", "smpte2084", "-colorspace", "bt2020nc", "-an")
    plan, mi, rep = conform_and_verify(ffmpeg, ffprobe, tmp_path, [src])
    assert any("HDR" in r for r in plan.reasons)
    assert_ok(rep)
    assert mi.video.pix_fmt == "yuv420p" and mi.video.color_transfer == "bt709"


def test_hdr_kept_in_hevc(ffmpeg, ffprobe, tmp_path):
    src = make(ffmpeg, tmp_path / "hdr.webm", *lavfi_video("testsrc2=s=1280x720:r=30:d=2"),
               "-vf", "setparams=color_primaries=bt2020:color_trc=smpte2084:colorspace=bt2020nc",
               "-c:v", "libvpx-vp9", "-deadline", "realtime", "-cpu-used", "8", "-pix_fmt", "yuv420p10le", "-an")
    plan, mi, rep = conform_and_verify(ffmpeg, ffprobe, tmp_path, [src],
                                       copy_options(OutputOptions(), profile="hevc", hdr="keep"))
    assert_ok(rep)
    assert mi.video.codec == "hevc" and mi.video.color_transfer == "smpte2084"


def test_interlaced_deinterlaced(ffmpeg, ffprobe, tmp_path):
    src = make(ffmpeg, tmp_path / "i.mkv", *lavfi_video("testsrc2=s=720x576:r=25:d=2"), "-vf", "setfield=tff",
               "-c:v", "libx264", "-preset", "ultrafast", "-flags", "+ilme+ildct",
               "-x264-params", "tff=1", "-pix_fmt", "yuv420p", "-an")
    info = probe(ffprobe, src)
    plan, mi, rep = conform_and_verify(ffmpeg, ffprobe, tmp_path, [src])
    assert_ok(rep)
    if info.video.is_interlaced:
        assert "bwdif" in " ".join(plan.args)


def test_rotation_baked(ffmpeg, ffprobe, tmp_path):
    base = make(ffmpeg, tmp_path / "b.mp4", *lavfi_video("testsrc2=s=640x360:r=30:d=2"), "-c:v", "libx264",
                "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-an")
    rot = tmp_path / "rot.mp4"
    run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-display_rotation", "90", "-i", base, "-c", "copy",
         str(rot)])
    info = probe(ffprobe, rot)
    plan, mi, rep = conform_and_verify(ffmpeg, ffprobe, tmp_path, [str(rot)])
    assert_ok(rep)
    if info.video.rotation:
        assert (mi.video.width, mi.video.height) == (360, 640)
        assert mi.video.rotation == 0


def test_full_range_and_anamorphic(ffmpeg, ffprobe, tmp_path):
    src = make(ffmpeg, tmp_path / "fr.mp4", *lavfi_video("testsrc2=s=720x576:r=25:d=2"),
               "-vf", "setsar=16/15,format=yuvj420p", "-c:v", "libx264", "-preset", "ultrafast", "-an")
    plan, mi, rep = conform_and_verify(ffmpeg, ffprobe, tmp_path, [src])
    assert_ok(rep)
    assert mi.video.square_pixels
    assert mi.video.width == 768 and mi.video.height == 576
    assert mi.video.color_range in ("tv", "")


def test_audio_only_source_with_video_profile(ffmpeg, ffprobe, tmp_path):
    src = make(ffmpeg, tmp_path / "pod.webm", *lavfi_audio(48000), "-c:a", "libopus")
    plan, mi, rep = conform_and_verify(ffmpeg, ffprobe, tmp_path, [src])
    assert plan.profile == "aac" and plan.ext == "m4a"
    assert_ok(rep)


@pytest.mark.parametrize("profile,codec,pix", [("prores", "prores", "yuv422p10le"), ("dnxhr", "dnxhd", "yuv422p")])
def test_intermediate_profiles(ffmpeg, ffprobe, tmp_path, profile, codec, pix):
    src = make(ffmpeg, tmp_path / "s.webm", *lavfi_video("testsrc2=s=1281x721:r=30:d=2"), *lavfi_audio(48000, 2),
               "-c:v", "libvpx-vp9", "-deadline", "realtime", "-cpu-used", "8", "-c:a", "libopus")
    plan, mi, rep = conform_and_verify(ffmpeg, ffprobe, tmp_path, [src], copy_options(OutputOptions(), profile=profile))
    assert_ok(rep)
    assert mi.video.codec == codec and mi.video.pix_fmt == pix
    assert mi.audio.codec == "pcm_s24le"


@pytest.mark.parametrize("profile,codec", [("wav", "pcm_s24le"), ("aac", "aac"), ("mp3", "mp3")])
def test_audio_profiles(ffmpeg, ffprobe, tmp_path, profile, codec):
    src = make(ffmpeg, tmp_path / "s.webm", *lavfi_video("testsrc2=s=320x240:r=30:d=2"), *lavfi_audio(44100, 2),
               "-c:v", "libvpx-vp9", "-deadline", "realtime", "-cpu-used", "8", "-c:a", "libopus")
    plan, mi, rep = conform_and_verify(ffmpeg, ffprobe, tmp_path, [src], copy_options(OutputOptions(), profile=profile))
    assert_ok(rep)
    assert mi.audio.codec == codec and mi.video is None


def test_original_mux_two_inputs(ffmpeg, ffprobe, tmp_path):
    v = make(ffmpeg, tmp_path / "v.webm", *lavfi_video("testsrc2=s=320x240:r=30:d=2"), "-c:v", "libvpx-vp9",
             "-deadline", "realtime", "-cpu-used", "8", "-an")
    a = make(ffmpeg, tmp_path / "a.webm", *lavfi_audio(48000, 2), "-c:a", "libopus")
    infos = [probe(ffprobe, v), probe(ffprobe, a)]
    plan = conform.plan(infos, copy_options(OutputOptions(), profile="original"))
    assert plan.ext == "webm" and not plan.passthrough
    out = tmp_path / "o.webm"
    run([ffmpeg, *plan.args, str(out)])
    mi = probe(ffprobe, out)
    assert mi.video.codec == "vp9" and mi.audio.codec == "opus"


def test_trim_and_sponsor_segments(ffmpeg, ffprobe, tmp_path):
    # yt-dlp already cut the section, so the file is 5 s long; one second of sponsor goes too.
    src = make(ffmpeg, tmp_path / "s.mp4", *lavfi_video("testsrc2=s=640x360:r=30:d=5.2"),
               *lavfi_audio(48000, 5.2), "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
               "-c:a", "aac")
    plan, mi, rep = conform_and_verify(ffmpeg, ffprobe, tmp_path, [src], trim=(10.0, 15.0),
                                       sponsor_segments=[(1.0, 2.0)])
    assert_ok(rep)
    assert abs(mi.duration - 4.0) < 0.15, mi.duration


def test_resolution_cap_and_fps_cap(ffmpeg, ffprobe, tmp_path):
    src = make(ffmpeg, tmp_path / "s.webm", *lavfi_video("testsrc2=s=2560x1440:r=60:d=1"), "-c:v", "libvpx-vp9",
               "-deadline", "realtime", "-cpu-used", "8", "-an")
    opts = copy_options(OutputOptions(), resolution="1080", max_fps="30")
    plan, mi, rep = conform_and_verify(ffmpeg, ffprobe, tmp_path, [src], opts)
    assert_ok(rep)
    assert (mi.video.width, mi.video.height) == (1920, 1080)
    assert mi.video.avg_rate == Fraction(30)


def test_vertical_video_cap_uses_short_side(ffmpeg, ffprobe, tmp_path):
    src = make(ffmpeg, tmp_path / "s.webm", *lavfi_video("testsrc2=s=1080x1920:r=30:d=1"), "-c:v", "libvpx-vp9",
               "-deadline", "realtime", "-cpu-used", "8", "-an")
    opts = copy_options(OutputOptions(), resolution="720")
    plan, mi, rep = conform_and_verify(ffmpeg, ffprobe, tmp_path, [src], opts)
    assert_ok(rep)
    assert (mi.video.width, mi.video.height) == (720, 1280)


def test_near_square_sar_fixed_without_reencode(ffmpeg, ffprobe, tmp_path):
    src = make(ffmpeg, tmp_path / "s.mp4", *lavfi_video("testsrc2=s=852x480:r=30:d=2"), *lavfi_audio(48000, 2),
               "-vf", "setsar=640/639", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
               "-c:a", "aac")
    plan, mi, rep = conform_and_verify(ffmpeg, ffprobe, tmp_path, [src])
    assert plan.video_action == "copy", plan.reasons
    assert_ok(rep)
    assert mi.video.sar in ("1:1", "")


def test_cover_art_dropped(ffmpeg, ffprobe, tmp_path):
    v = make(ffmpeg, tmp_path / "v.mp4", *lavfi_video("testsrc2=s=640x360:r=30:d=2"), *lavfi_audio(48000, 2),
             "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac")
    img = make(ffmpeg, tmp_path / "c.jpg", *lavfi_video("color=red:s=320x180:d=0.04"), "-frames:v", "1")
    withart = tmp_path / "art.mp4"
    run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", v, "-i", img, "-map", "0", "-map", "1",
         "-c", "copy", "-disposition:v:1", "attached_pic", str(withart)])
    plan, mi, rep = conform_and_verify(ffmpeg, ffprobe, tmp_path, [str(withart)])
    assert_ok(rep)
    assert len([s for s in mi.streams if s.kind == "video"]) == 1


# ---------------------------------------------------------------- retiming instead of encoding

def _frame_hashes(ffmpeg, path):
    r = subprocess.run([ffmpeg, "-hide_banner", "-v", "error", "-i", str(path), "-map", "0:v", "-f", "framemd5", "-"],
                       stdout=subprocess.PIPE, check=True)
    return [ln.rsplit(",", 1)[-1].strip() for ln in r.stdout.decode().splitlines() if ln and not ln.startswith("#")]


def _plan_like_the_runner(ffprobe, src, opts=None):
    """Plan; when only timestamps stand in the way of a copy, check the grid and plan again."""
    from ingest.core.probe import grid_fit
    opts = opts or OutputOptions()
    infos = [probe(ffprobe, src)]
    timing = measure_timing(ffprobe, src)
    caps = conform.Caps(hw_h264=[], hw_hevc=[], zscale=True, libx265=True)
    plan = conform.plan(infos, opts, caps, timing)
    if plan.retime_rate:
        fit = grid_fit(ffprobe, src, plan.retime_rate, infos[0].video.index)
        if fit is not None:
            plan = conform.plan(infos, opts, caps, timing, retime=fit)
    return plan


def _run_and_check(ffmpeg, ffprobe, tmp_path, plan):
    out = tmp_path / "out.mp4"
    run([ffmpeg, *plan.args, str(out)])
    mi = probe(ffprobe, out)
    t = measure_timing(ffprobe, out)
    rep = verify.verify(mi, plan.profile, plan.out, t, plan.duration)
    assert_ok(rep)
    r = subprocess.run([ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries", "packet=pts,dts",
                        "-of", "csv=p=0", str(out)], stdout=subprocess.PIPE, check=True)
    rows = [tuple(int(x) for x in ln.split(",")) for ln in r.stdout.decode().split() if ln]
    pts = sorted(p for p, _ in rows)
    return out, mi, {b - a for a, b in zip(pts, pts[1:])}, rows


@pytest.mark.parametrize("container", ["mkv", "flv"])
def test_millisecond_container_is_retimed_not_encoded(ffmpeg, ffprobe, tmp_path, container):
    # Matroska and FLV keep milliseconds, so 29.97 arrives as 33/34 ms steps. B-frames on purpose.
    src = make(ffmpeg, tmp_path / f"src.{container}", *lavfi_video("testsrc2=s=640x360:r=30000/1001:d=6"),
               *lavfi_audio(48000, 6), "-c:v", "libx264", "-preset", "ultrafast", "-bf", "3", "-g", "60",
               "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest")
    plan = _plan_like_the_runner(ffprobe, src)
    assert plan.retimed and plan.video_action == "copy", plan.reasons
    assert plan.summary.startswith("Retime H.264")
    out, mi, steps, rows = _run_and_check(ffmpeg, ffprobe, tmp_path, plan)
    assert mi.video.avg_rate == Fraction(30000, 1001)
    assert len(steps) == 1  # one exact frame duration throughout
    dts = [d for _, d in rows]
    assert all(b > a for a, b in zip(dts, dts[1:])) and all(d <= p for p, d in rows)
    assert _frame_hashes(ffmpeg, out) == _frame_hashes(ffmpeg, src)  # nothing was re-encoded


def test_jittery_mp4_is_restamped(ffmpeg, ffprobe, tmp_path):
    """Twitch-style: MP4 with a 90 kHz clock but millisecond-rounded stamps (RTMP origin)."""
    mkv = make(ffmpeg, tmp_path / "rtmp.mkv", *lavfi_video("testsrc2=s=640x360:r=30000/1001:d=6"),
               "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p")
    src = make(ffmpeg, tmp_path / "clip.mp4", "-i", mkv, "-c", "copy", "-video_track_timescale", "90000")
    t = measure_timing(ffprobe, src)
    assert not t.vfr and t.spread > 0.02
    plan = _plan_like_the_runner(ffprobe, src)
    assert plan.retimed, plan.reasons
    _out, mi, steps, _rows = _run_and_check(ffmpeg, ffprobe, tmp_path, plan)
    assert len(steps) == 1 and mi.video.avg_rate == Fraction(30000, 1001)


def test_gap_in_the_stream_is_encoded(ffmpeg, ffprobe, tmp_path):
    """A dropped second can't be restamped away; the encoder fills it with held frames."""
    src = make(ffmpeg, tmp_path / "gap.mkv", *lavfi_video("testsrc2=s=640x360:r=30000/1001:d=6"),
               "-vf", "select='not(between(t,2,3))'", "-fps_mode", "passthrough",
               "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p")
    plan = _plan_like_the_runner(ffprobe, src)
    assert not plan.retimed and plan.video_action == "encode"
    _out, _mi, steps, _rows = _run_and_check(ffmpeg, ffprobe, tmp_path, plan)
    assert len(steps) == 1


def test_untagged_copy_has_a_plain_fallback(ffmpeg, ffprobe, tmp_path):
    """If h264_metadata can't parse a stream (Twitch's SEI), the copy is retried with container tags only."""
    src = make(ffmpeg, tmp_path / "untagged.mp4", *lavfi_video("testsrc2=s=640x360:r=30:d=2"), "-c:v", "libx264",
               "-preset", "ultrafast", "-pix_fmt", "yuv420p")
    plan, mi, rep = conform_and_verify(ffmpeg, ffprobe, tmp_path, [src])
    assert plan.video_action == "copy" and any("h264_metadata" in a for a in plan.args)
    assert plan.plain is not None and not any("h264_metadata" in a for a in plan.plain.args)
    assert "-color_primaries" in plan.plain.args
    assert_ok(rep)
    out = tmp_path / "plain.mp4"
    run([ffmpeg, *plan.plain.args, str(out)])
    v = probe(ffprobe, out).video
    assert (v.color_primaries, v.color_transfer, v.color_space) == ("bt709",) * 3
