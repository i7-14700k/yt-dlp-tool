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
