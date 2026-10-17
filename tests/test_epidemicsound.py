"""Resolve modern music links and download through the real yt-dlp plugin loader."""

import importlib.util
import subprocess
import sys
import threading
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from yt_dlp import YoutubeDL

from ingest.core.engines import Engines
from ingest.core.library import Library
from ingest.core.probe import probe
from ingest.core.queue import Events, QueueManager
from ingest.core.settings import SettingsStore

TRACK_ID = "cb6b688c-3b67-490a-ba00-4da8071286d7"
TRACK_URL = f"https://www.epidemicsound.com/music/tracks/{TRACK_ID}/"
API_URL = f"https://www.epidemicsound.com/json/track/kosmos-id/{TRACK_ID}"
PLUGIN = (Path(__file__).resolve().parents[1] / "plugins" / "ingest" / "yt_dlp_plugins"
          / "extractor" / "epidemicsound.py")


@pytest.fixture(scope="module")
def extractor():
    spec = importlib.util.spec_from_file_location("ingest_epidemicsound", PLUGIN)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.EpidemicSoundMusicIE


@pytest.mark.parametrize("url", [TRACK_URL, TRACK_URL.rstrip("/"), TRACK_URL + "?utm_source=share",
                                 TRACK_URL.replace("www.", "")])
def test_music_links_match(extractor, url):
    assert extractor.suitable(url)
    assert extractor()._match_id(url) == TRACK_ID


@pytest.mark.parametrize("url", [
    TRACK_URL.replace("epidemicsound.com", "epidemicsound.com.example.com"),
    TRACK_URL.replace(TRACK_ID, "not-a-track-id"),
    TRACK_URL.replace(TRACK_ID, TRACK_ID + "invalid"),
    "https://www.epidemicsound.com/music/",
    "https://www.epidemicsound.com/track/mj8GTTwsZd/",
    "https://www.epidemicsound.com/sound-effects/tracks/2f02f54b-9faa-4daf-abac-1cfe9e9cef69/",
])
def test_other_links_are_left_to_existing_extractors(extractor, url):
    assert not extractor.suitable(url)


def track_metadata(base):
    return {
        "id": 123456, "publicSlug": "exampleTrack", "title": "Example full mix", "length": 1,
        "stems": {
            "full": {"stemType": "full", "lqMp3Url": f"{base}/full.mp3", "s3TrackId": "full-id"},
            "instrumental": {"stemType": "instrumental", "lqMp3Url": f"{base}/instrumental.mp3"},
        },
    }


def test_resolves_uuid_api_and_prefers_full_mix(extractor, monkeypatch):
    calls = []

    def metadata(self, url, video_id, *args, **kwargs):
        calls.append((url, video_id))
        return track_metadata("https://audiocdn.epidemicsound.com")

    monkeypatch.setattr(extractor, "_download_json", metadata)
    with YoutubeDL({"quiet": True}, auto_init=False) as ydl:
        ydl.add_info_extractor(extractor())
        info = ydl.extract_info(TRACK_URL, download=False)
    assert calls == [(API_URL, TRACK_ID)]
    assert info["title"] == "Example full mix"
    assert info["url"] == "https://audiocdn.epidemicsound.com/full.mp3"
    assert info["format_id"] == "full"
    assert info["webpage_url"] == TRACK_URL


@pytest.mark.skipif(sys.platform == "win32", reason="test yt-dlp launcher is a POSIX script")
@pytest.mark.parametrize("profile,codec,sample_rate", [("original", "mp3", 44100), ("wav", "pcm_s24le", 48000)])
def test_music_link_downloads_and_converts_in_queue(tmp_path, ffmpeg, ffprobe, profile, codec, sample_rate):
    media = tmp_path / "media"
    media.mkdir()
    subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=d=1:r=44100",
                    "-c:a", "libmp3lame", str(media / "full.mp3")], check=True)
    requests = []

    class Handler(SimpleHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            super().do_GET()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(Handler, directory=str(media)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    metadata = track_metadata(f"http://127.0.0.1:{server.server_port}")
    # Substitute only the remote metadata response. The app's normal arguments,
    # plugin discovery, format selection, HTTP download and conversion all run.
    launcher = tmp_path / "yt-dlp"
    launcher.write_text(f"""#!{sys.executable}
from urllib.parse import urlsplit
from yt_dlp import YoutubeDL, main
from yt_dlp.extractor.epidemicsound import EpidemicSoundIE

def metadata(self, url, video_id, *args, **kwargs):
    assert url == {API_URL!r}, url
    return {metadata!r}

original_urlopen = YoutubeDL.urlopen
def local_only(self, request):
    assert urlsplit(request.url).hostname == '127.0.0.1', request.url
    return original_urlopen(self, request)

EpidemicSoundIE._download_json = metadata
YoutubeDL.urlopen = local_only
main()
""", encoding="utf-8")
    launcher.chmod(0o755)
    store = SettingsStore(tmp_path / "settings.json")
    store.settings.path_ytdlp = str(launcher)
    store.settings.path_ffmpeg = ffmpeg
    store.settings.output.output_dir = str(tmp_path / "out")
    store.settings.output.profile = profile
    store.settings.nightly_fallback = False
    store.settings.gallery_fallback = False
    library = Library(tmp_path / "library.sqlite3")
    queue = QueueManager(store, Engines(lambda: store.settings), library, Events(),
                         state_path=tmp_path / "queue.json")
    try:
        queue.start_capability_probe()
        (job,) = queue.add_urls([TRACK_URL], store.settings.output)
        deadline = time.monotonic() + 30
        while job.status not in ("done", "failed") and time.monotonic() < deadline:
            time.sleep(0.05)
        log = Path(job.log_path).read_text(encoding="utf-8") if job.log_path else ""
        assert job.status == "done", (job.error, log)
        assert job.title == "Example full mix"
        assert job.webpage_url == TRACK_URL
        assert job.selection["format_ids"] == "full"
        assert requests == ["/full.mp3"]
        result = probe(ffprobe, job.outputs[0])
        assert not result.video and result.audio.codec == codec
        assert result.audio.sample_rate == sample_rate
        if profile == "original":
            assert Path(job.outputs[0]).read_bytes() == (media / "full.mp3").read_bytes()
        assert library.count() == 1
    finally:
        queue.shutdown(2)
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
