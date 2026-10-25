import pytest

from bamboozle import camera, hms_catalog
from bamboozle.camera import CameraStream
from fakes import FakeFfmpeg


@pytest.fixture(autouse=True)
def isolated_appdata(tmp_path, monkeypatch):
    """Point LOCALAPPDATA at a temp dir so tests never touch the real config."""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    return tmp_path


@pytest.fixture(autouse=True)
def offline_hms_catalog(monkeypatch):
    """Never fetch Bambu's HMS catalogue in tests; start each with none loaded."""
    def no_network(url, timeout, max_bytes):
        raise OSError("network disabled in tests")
    monkeypatch.setattr(hms_catalog, "http_get", no_network)
    monkeypatch.setattr(hms_catalog, "CATALOG", hms_catalog.HmsCatalog())


@pytest.fixture
def ffmpeg(monkeypatch):
    """Queue FakeFfmpeg processes; each Popen call takes the next one."""
    monkeypatch.setattr(camera, "_find_ffmpeg", lambda: "ffmpeg")
    monkeypatch.setattr(CameraStream, "FFMPEG_STARTUP_SECONDS", 0)
    monkeypatch.setattr(CameraStream, "WATCH_INTERVAL_SECONDS", 0.02)
    monkeypatch.setattr(CameraStream, "FROZEN_SECONDS", 0.5)
    monkeypatch.setattr(CameraStream, "RESPAWN_SECONDS", 0.05)
    monkeypatch.setattr(CameraStream, "HEALTHY_SESSION_SECONDS", 0.1)
    queue = []
    spawned = []

    def popen(cmd, **kwargs):
        proc = queue.pop(0) if queue else FakeFfmpeg([])
        spawned.append(proc)
        return proc

    monkeypatch.setattr(camera.subprocess, "Popen", popen)
    yield queue, spawned
