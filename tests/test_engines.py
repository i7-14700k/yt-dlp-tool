"""Engine installs against a local web server: checksums are enforced, and a bad download installs nothing."""

import hashlib
import http.server
import sys
import threading
from functools import partial

import pytest

from ingest.core import engines as E

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="the stand-in engine is a shell script")

SCRIPT = b"#!/bin/sh\necho 2099.01.01\n"


class _Settings:
    channel = "stable"
    proxy = ""
    path_ytdlp = path_ffmpeg = path_deno = path_gallerydl = ""


@pytest.fixture
def server(tmp_path):
    root = tmp_path / "www"
    root.mkdir()
    handler = partial(http.server.SimpleHTTPRequestHandler, directory=str(root))
    handler.log_message = lambda *a, **k: None
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield root, f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def _setup(monkeypatch, tmp_path, server, sums: str | None):
    root, base = server
    (root / "yt-dlp_linux").write_bytes(SCRIPT)
    if sums is not None:
        (root / "SHA2-256SUMS").write_text(sums)
    monkeypatch.setattr(E, "asset_plan", lambda key, channel="stable": {
        "url": f"{base}/yt-dlp_linux", "sums": f"{base}/SHA2-256SUMS", "sum_name": "yt-dlp_linux",
        "kind": "file", "target": "yt-dlp"})
    eng = E.Engines(lambda: _Settings())
    eng.root = tmp_path / "engines"
    return eng


def test_install_checks_the_published_sum(monkeypatch, tmp_path, server):
    good = hashlib.sha256(SCRIPT).hexdigest()
    eng = _setup(monkeypatch, tmp_path, server, f"{good}  yt-dlp_linux\n")
    path = eng.install("ytdlp")
    assert eng.locate("ytdlp") == (path, "managed")
    assert eng.version("ytdlp") == "2099.01.01"


def test_wrong_sum_installs_nothing(monkeypatch, tmp_path, server):
    eng = _setup(monkeypatch, tmp_path, server, f"{'0' * 64}  yt-dlp_linux\n")
    with pytest.raises(E.InstallError, match="checksum"):
        eng.install("ytdlp")
    assert eng.locate("ytdlp")[1] != "managed"
    assert not any(p.name.startswith("v") for p in (eng.root / "ytdlp").iterdir())


def test_missing_sums_installs_nothing(monkeypatch, tmp_path, server):
    eng = _setup(monkeypatch, tmp_path, server, None)  # SHA2-256SUMS answers 404
    with pytest.raises(E.InstallError, match="checksum"):
        eng.install("ytdlp")
    assert eng.locate("ytdlp")[1] != "managed"
