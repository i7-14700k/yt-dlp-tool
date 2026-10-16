from fractions import Fraction
from pathlib import Path

from ingest.core import naming, util
from ingest.core.errors import classify, ffmpeg_failure
from ingest.core.probe import snap_rate, timing_from_pts
from ingest.core.settings import OutputOptions, Settings, SettingsStore, from_dict, to_dict
from ingest.core.ytdlp import format_selection, parse_file, parse_progress, selection_from_info


def test_sanitize_windows_names():
    assert naming.sanitize_component('A/B: C?*<>|"x"') == "A-B - C-'x'"
    assert naming.sanitize_component("CON") == "CON_"
    assert naming.sanitize_component("  trailing dots... ") == "trailing dots"
    assert naming.sanitize_component("emoji 🔥 title ✨") == "emoji title"
    assert naming.sanitize_component("guess it😍❤️ #fyp 1️⃣") == "guess it #fyp 1"  # no selectors left behind
    assert naming.sanitize_component("हिन्दी शीर्षक") == "हिन्दी शीर्षक"  # combining vowel signs stay
    assert naming.sanitize_component("日本語のタイトル") == "日本語のタイトル"
    assert naming.sanitize_component("Ünïcödé", ascii_only=True) == "Unicode"
    assert naming.sanitize_component("") == "untitled"
    long = naming.sanitize_component("word " * 80)
    assert len(long) <= naming.MAX_COMPONENT and not long.endswith(" ")


def test_render_template_drops_empty_tokens():
    ctx = {"title": "Clip", "id": "", "uploader": "Chan", "index": 7}
    assert naming.render("{title} [{id}]", ctx) == ["Clip"]
    assert naming.render("{uploader} - {title}", ctx) == ["Chan - Clip"]
    assert naming.render("{index:03} {title}", ctx) == ["007 Clip"]
    assert naming.render("{uploader}/{title}", ctx) == ["Chan", "Clip"]
    assert naming.render("{missing}", ctx) == ["Clip"]


def test_fit_path_respects_max_path(tmp_path):
    folder = tmp_path / ("d" * max(10, 200 - len(str(tmp_path))))
    p = naming.fit_path(folder, "t" * 200, ".mp4")
    assert len(str(p)) <= naming.MAX_PATH_SAFE
    assert p.suffix == ".mp4" and len(p.stem) >= 16
    # A folder already past the limit keeps a readable stem rather than an empty one.
    deep = naming.fit_path(tmp_path / ("d" * 260), "title", ".mp4")
    assert deep.name == "title.mp4"


def test_unique_path(tmp_path):
    a = tmp_path / "x.mp4"
    a.write_bytes(b"")
    assert naming.unique_path(a).name == "x (2).mp4"


def test_util_formatting():
    assert util.human_bytes(1536) == "2 KB"
    assert util.human_bytes(5 * 1024 * 1024) == "5.0 MB"
    assert util.human_duration(3725) == "1:02:05"
    assert util.human_duration(65) == "1:05"
    assert util.human_eta(30) == "30 s left"
    assert util.human_eta(600) == "10 min left"
    assert util.parse_timecode("1:30") == 90
    assert util.parse_timecode("01:02:03.5") == 3723.5
    assert util.parse_timecode("abc") is None
    assert util.fps_label(23.976023) == "23.976"
    assert util.fps_label(30.0) == "30"


def test_extract_urls():
    text = "see https://youtu.be/abc, and (https://en.wikipedia.org/wiki/Foo_(bar)) www.example.com/v."
    assert util.extract_urls(text) == ["https://youtu.be/abc", "https://en.wikipedia.org/wiki/Foo_(bar)",
                                       "https://www.example.com/v"]
    assert util.domain_of("https://www.youtube.com/watch?v=1") == "youtube.com"


def test_snap_rate():
    assert snap_rate(Fraction(2997, 100)) == Fraction(30000, 1001)
    assert snap_rate(23.98) == Fraction(24000, 1001)
    assert snap_rate(59.95) == Fraction(60000, 1001)
    assert snap_rate(14.985) == Fraction(15000, 1001)
    assert snap_rate(17.3) == Fraction(173, 10)


def test_timing_cfr_vfr():
    cfr = [i / 30 for i in range(300)]
    assert not timing_from_pts(cfr).vfr
    rounded = [round(i * 1001 / 30000, 3) for i in range(300)]  # MKV millisecond timestamps
    assert not timing_from_pts(rounded).vfr
    vfr = [i / 30 for i in range(100)] + [100 / 30 + i / 15 for i in range(100)]
    assert timing_from_pts(vfr).vfr


def test_progress_parsing():
    line = '@@P "137" {"status": "downloading", "downloaded_bytes": 500, "total_bytes": 1000, "speed": 10.5, "eta": 3}'
    p = parse_progress(line)
    assert p.format_id == "137" and p.percent == 50 and p.speed == 10.5
    frag = '@@P "hls-1" {"status": "downloading", "downloaded_bytes": 5, "fragment_index": 3, "fragment_count": 10}'
    assert parse_progress(frag).percent == 30
    assert parse_file('@@F "137" "C:\\\\Videos\\\\a \\u00e9.mp4"') == ("137", "C:\\Videos\\a é.mp4")
    assert parse_progress("[download] something") is None


def test_format_selection_prefers_resolution_then_codec():
    fmt, sort = format_selection(OutputOptions())
    assert fmt == "bv*+ba/b"
    assert sort.split(",") == ["res", "fps", "hdr:sdr", "vcodec:h264"]
    fmt, sort = format_selection(from_dict(OutputOptions, {"resolution": "1080", "max_fps": "30"}))
    assert sort.startswith("res:1080,fps:30")
    fmt, sort = format_selection(from_dict(OutputOptions, {"profile": "wav"}))
    assert fmt == "ba/b"


def test_selection_from_info():
    info = {"format_id": "137+251", "duration": 10, "requested_formats": [
        {"format_id": "137", "vcodec": "avc1.640028", "acodec": "none", "width": 1920, "height": 1080, "fps": 30,
         "filesize": 1000},
        {"format_id": "251", "vcodec": "none", "acodec": "opus", "filesize": 100}]}
    sel = selection_from_info(info)
    assert sel.format_ids == "137,251" and sel.vcodec == "h264" and sel.acodec == "opus" and sel.size == 1100


def test_ffmpeg_failures():
    from ingest.core.errors import ffmpeg_failure
    # A header rewrite that can't parse the stream is not a damaged download.
    bsf = ffmpeg_failure(["[h264_metadata @ 0x1] Failed to read unit 3 (type 6): Invalid data found when processing "
                          "input.", "[vost#0:0/copy] Error applying bitstream filters to a packet: Invalid data found "
                          "when processing input"])
    assert bsf.kind == "bsf" and bsf.retry_with == "plain"
    assert ffmpeg_failure(["moov atom not found"]).retry_with == "refresh"
    assert ffmpeg_failure(["[h264_nvenc] OpenEncodeSessionEx failed: out of memory"]).retry_with == "cpu"


def test_error_classification():
    assert classify(["ERROR: [youtube] x: Sign in to confirm you’re not a bot"]).retry_with == "cookies"
    assert classify(["ERROR: unable to download video data: HTTP Error 403: Forbidden"]).kind == "forbidden"
    assert classify(["ERROR: [generic] Unsupported URL: https://a.b"]).kind == "unsupported"
    assert classify(["ERROR: Failed to decrypt with DPAPI"]).kind == "cookie_read"
    assert classify(["WARNING: x", "nothing"], 1).kind == "unknown"
    assert ffmpeg_failure(["[h264_nvenc @ 0x1] OpenEncodeSessionEx failed: unsupported device (2)"]).retry_with == "cpu"


def test_settings_roundtrip_and_bad_values(tmp_path):
    store = SettingsStore(tmp_path / "s.json")
    store.settings.output.profile = "prores"
    store.settings.max_concurrent = 5
    store.save()
    again = SettingsStore(tmp_path / "s.json")
    assert again.settings.output.profile == "prores" and again.settings.max_concurrent == 5
    (tmp_path / "s.json").write_text('{"max_concurrent": "lots", "output": {"profile": 3, "trim_start": 5}}')
    third = SettingsStore(tmp_path / "s.json")
    assert third.settings.max_concurrent == Settings().max_concurrent
    assert third.settings.output.profile == "h264"
    assert third.settings.output.trim_start is None  # per-job overrides never persist
    (tmp_path / "s.json").write_text("{not json")
    assert SettingsStore(tmp_path / "s.json").settings.schema == 1
    assert Path(tmp_path / "s.broken.json").exists()
    d = to_dict(Settings())
    assert from_dict(Settings, d) == Settings()


def test_protocol_links():
    from ingest.core.util import expand_protocol_links
    assert expand_protocol_links(["ingest://add?url=https%3A%2F%2Fyoutu.be%2Fabc%3Ft%3D5"]) == ["https://youtu.be/abc?t=5"]
    assert expand_protocol_links(["ingest:https%3A%2F%2Fx.com%2Fa"]) == ["https://x.com/a"]
    assert expand_protocol_links(["https://plain"]) == ["https://plain"]


def test_prefer_codec_when_only_the_reported_rate_differs():
    from ingest.core.ytdlp import prefer_codec
    def fmt(fid, vc, h, fps, tbr, w=None):
        return {"format_id": fid, "vcodec": vc, "acodec": "none", "height": h, "width": w or h * 16 // 9,
                "fps": fps, "tbr": tbr}
    audio = {"format_id": "a", "vcodec": "none", "acodec": "mp4a.40.2"}
    hevc, avc = fmt("30066", "hev1.1.6.L120", 720, 24.39, 1016), fmt("30064", "avc1.64001F", 720, 23.81, 1682)
    info = {"formats": [avc, hevc, fmt("30080", "avc1.640032", 1080, 23.81, 3000), audio],
            "requested_formats": [hevc, audio], "format_id": "30066+a"}
    assert prefer_codec(info, "h264") == "30064"  # Bilibili: one 24 fps stream, two reported rates
    assert info["format_id"] == "30064+a" and info["requested_formats"][0] is avc
    assert prefer_codec(info, "h264") is None  # already H.264
    # A real difference stays: 60 fps VP9 isn't traded for 30 fps H.264, nor 4K for 1080p.
    vp9 = fmt("303", "vp9", 1080, 60, 4000)
    info = {"formats": [fmt("137", "avc1.640028", 1080, 30, 4000), vp9, audio], "requested_formats": [vp9, audio]}
    assert prefer_codec(info, "h264") is None


def test_outdated_engines_on_path():
    from datetime import date
    from ingest.core.engines import too_old
    assert too_old("ffmpeg", "6.1.1")  # no setts prescale, older setparams
    assert not too_old("ffmpeg", "7.1") and not too_old("ffmpeg", "master 2026-10-15")
    today = date(2026, 10, 16)
    assert not too_old("ytdlp", "2026.08.19", today)
    assert too_old("ytdlp", "2025.06.30", today)
    assert not too_old("ytdlp", "garbage", today) and not too_old("deno", "1.0.0")


def test_check_engine_file(tmp_path, ffmpeg, monkeypatch):
    import shutil
    import sys
    from pathlib import Path
    from ingest.core import engines
    from ingest.core.engines import EXE, check_engine_file
    monkeypatch.setattr(engines, "too_old", lambda key, version, today=None: "")  # whichever FFmpeg runs the tests
    # A real FFmpeg folder, picked as the folder, as ffmpeg or as ffprobe.
    d = tmp_path / "ff"
    d.mkdir()
    for name in ("ffmpeg", "ffprobe"):
        shutil.copy(Path(ffmpeg).with_name(name + EXE), d / (name + EXE))
    want = str(d / ("ffmpeg" + EXE))
    for picked in (d, d / ("ffmpeg" + EXE), d / ("ffprobe" + EXE)):
        assert check_engine_file("ffmpeg", str(picked)) == (want, "")
    # ffmpeg without ffprobe next to it.
    (d / ("ffprobe" + EXE)).unlink()
    use, why = check_engine_file("ffmpeg", want)
    assert not use and "ffprobe" in why
    # Something that runs but isn't the engine.
    use, why = check_engine_file("ytdlp", sys.executable)
    assert not use and "not yt-dlp" in why
    use, why = check_engine_file("deno", str(tmp_path / "nothing.exe"))
    assert not use and "no deno" in why


def test_free_space_is_measured_off_the_gui_thread(tmp_path, monkeypatch):
    import threading
    import time
    from ingest.ui import main_window as mw
    gate = threading.Event()
    monkeypatch.setattr(mw, "disk_free", lambda p: (gate.wait(5), 123)[1])  # a share that hangs
    fs = mw.FreeSpace(every=0)
    t0 = time.monotonic()
    assert fs.get(tmp_path) is None  # returns at once, doesn't wait for the drive
    assert time.monotonic() - t0 < 0.5
    gate.set()
    deadline = time.monotonic() + 5
    while fs.get(tmp_path) is None and time.monotonic() < deadline:
        time.sleep(0.02)
    assert fs.get(tmp_path) == 123


def test_atomic_write_waits_out_a_locked_file(tmp_path, monkeypatch):
    import os
    from ingest.core import settings as S
    real, calls = os.replace, []

    def flaky(src, dst):
        calls.append(1)
        if len(calls) < 3:
            raise PermissionError(13, "The process cannot access the file")  # a scanner has it open
        return real(src, dst)
    monkeypatch.setattr(S.os, "replace", flaky)
    target = tmp_path / "settings.json"
    S.atomic_write_text(target, '{"ok": true}')
    assert target.read_text() == '{"ok": true}' and len(calls) == 3
    assert not [p for p in tmp_path.iterdir() if p.suffix == ".tmp"]


def test_check_engine_file_refuses_an_old_ffmpeg(tmp_path, ffmpeg, monkeypatch):
    import shutil
    from pathlib import Path
    from ingest.core import engines
    d = tmp_path / "old"
    d.mkdir()
    for name in ("ffmpeg", "ffprobe"):
        shutil.copy(Path(ffmpeg).with_name(name + engines.EXE), d / (name + engines.EXE))
    monkeypatch.setattr(engines, "parse_version", lambda key, out: "6.1.1")
    use, why = engines.check_engine_file("ffmpeg", str(d))
    assert not use and "too old" in why
