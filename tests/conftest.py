import os
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def _find(name: str) -> str | None:
    env = os.environ.get(f"INGEST_TEST_{name.upper()}")
    if env and Path(env).is_file():
        return env
    home = os.environ.get("INGEST_HOME")
    if home:
        base = Path(home) / "data" / "engines" / "ffmpeg"
        for p in sorted(base.glob(f"v*/{name}*"), reverse=True):
            if p.is_file():
                return str(p)
    return shutil.which(name)


@pytest.fixture(scope="session")
def ffmpeg() -> str:
    p = _find("ffmpeg")
    if not p:
        pytest.skip("ffmpeg not available")
    return p


@pytest.fixture(scope="session")
def ffprobe() -> str:
    p = _find("ffprobe")
    if not p:
        pytest.skip("ffprobe not available")
    return p


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("INGEST_HOME", os.environ.get("INGEST_HOME_TESTS", str(tmp_path / "home")))
    yield
