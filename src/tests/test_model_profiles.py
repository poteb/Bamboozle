"""Per-model ffmpeg profiles and the printer model stored on PrinterConfig."""
import asyncio
import json

from types import SimpleNamespace

import pytest

from bamboozle import camera, discovery
from bamboozle.camera import CameraStream, rtsp_command
from bamboozle.discovery import Announcement, model_info
from bamboozle.models import AppConfig, PrinterConfig, PrinterUpdateRequest
from bamboozle.printer_manager import PrinterManager
from bamboozle.routers import api
from test_camera import JPEG_A, JPEG_B, FakeFfmpeg, ffmpeg, wait_until  # noqa: F401

URL = "rtsps://bblp:x@192.168.0.103:322/streaming/live/1"
P2S_SERIAL = "22E8BJ610801473"
P2S_IP = "192.168.0.103"


def _input_options(cmd):
    """The options before `-i`, as a flag -> value dict."""
    head = cmd[1 : cmd.index("-i")]
    return {head[i]: head[i + 1] if i + 1 < len(head) and not head[i + 1].startswith("-") else None
            for i in range(len(head)) if head[i].startswith("-")}


# --- command line per profile ---------------------------------------------------

def test_p2s_gets_wallclock_timestamps_and_a_bigger_probe():
    opts = _input_options(rtsp_command("ffmpeg", URL, camera.TOLERANT))
    assert opts["-use_wallclock_as_timestamps"] == "1"
    assert opts["-probesize"] == "1000000"
    assert opts["-analyzeduration"] == "500000"


def test_low_latency_profile_starts_fast_without_buffering():
    opts = _input_options(rtsp_command("ffmpeg", URL, camera.LOW_LATENCY))
    assert opts["-probesize"] == "32"
    assert opts["-analyzeduration"] == "0"
    assert opts["-fflags"] == "nobuffer"
    assert opts["-flags"] == "low_delay"
    assert opts["-max_delay"] == "500000"
    assert "-use_wallclock_as_timestamps" not in opts


def test_every_profile_keeps_the_rtsp_basics_and_mjpeg_output():
    for profile in (camera.LOW_LATENCY, camera.TOLERANT):
        cmd = rtsp_command("ffmpeg", URL, profile)
        opts = _input_options(cmd)
        assert opts["-rtsp_transport"] == "tcp"
        assert opts["-tls_verify"] == "0"
        assert cmd[cmd.index("-i") + 1] == URL
        assert cmd[-1] == "pipe:1"
        assert cmd[cmd.index("-f") + 1] == "mjpeg"


@pytest.mark.parametrize("model, profile", [
    ("N7", camera.TOLERANT),       # P2S
    ("O1C2", camera.LOW_LATENCY),  # H2C
    ("O1C", camera.LOW_LATENCY),   # H2C variant
    ("BL-P001", camera.LOW_LATENCY),
    ("", camera.LOW_LATENCY),      # not known yet
    ("ZZ9", camera.LOW_LATENCY),
])
def test_profile_follows_the_model(model, profile):
    assert camera.rtsp_profile(model) == profile


def test_h2c_o1c_variant_is_known():
    assert model_info("O1C") == ("H2C", 322)


# --- profile choice in a running stream -------------------------------------------

def _profile_of(cmd):
    return camera.TOLERANT if "-use_wallclock_as_timestamps" in cmd else camera.LOW_LATENCY


@pytest.fixture
def commands(ffmpeg, monkeypatch):  # noqa: F811
    """Record each spawned ffmpeg's command line."""
    queue, spawned = ffmpeg
    cmds = []
    popen = camera.subprocess.Popen

    def recording_popen(cmd, **kwargs):
        cmds.append(cmd)
        return popen(cmd, **kwargs)

    monkeypatch.setattr(camera.subprocess, "Popen", recording_popen)
    return queue, cmds


def test_p2s_stream_uses_the_tolerant_profile_from_the_start(commands):
    queue, cmds = commands
    queue.append(FakeFfmpeg([JPEG_A, JPEG_B]))
    cam = CameraStream(P2S_IP, "x", 322, model="N7")
    cam.start()
    try:
        assert wait_until(lambda: cmds)
    finally:
        cam.stop()
    assert _profile_of(cmds[0]) == camera.TOLERANT


def test_low_latency_start_that_fails_falls_back_to_the_tolerant_profile(commands):
    """probesize 32 doesn't lock on every firmware, and an unknown model may be a P2S."""
    queue, cmds = commands
    queue.append(FakeFfmpeg([JPEG_A]))  # one frame, then frozen: unhealthy session
    cam = CameraStream(P2S_IP, "x", 322)
    cam.RESPAWN_SECONDS = cam.BACKOFF_MIN_SECONDS = 0.05
    cam.start()
    try:
        assert wait_until(lambda: len(cmds) >= 2, timeout=4)
    finally:
        cam.stop()
    assert [_profile_of(c) for c in cmds[:2]] == [camera.LOW_LATENCY, camera.TOLERANT]


def test_low_latency_start_without_frames_falls_back_too(commands):
    queue, cmds = commands
    queue.append(FakeFfmpeg([], returncode=1, stderr=b"Invalid data found"))
    cam = CameraStream("192.168.0.101", "x", 322, model="O1C2")
    cam.BACKOFF_MIN_SECONDS = 0.05
    cam.start()
    try:
        assert wait_until(lambda: len(cmds) >= 2, timeout=4)
    finally:
        cam.stop()
    assert [_profile_of(c) for c in cmds[:2]] == [camera.LOW_LATENCY, camera.TOLERANT]


def test_healthy_low_latency_session_keeps_the_profile(commands):
    queue, cmds = commands
    first = FakeFfmpeg([JPEG_A, JPEG_B])
    queue.extend([first, FakeFfmpeg([JPEG_A, JPEG_B])])
    cam = CameraStream("192.168.0.101", "x", 322, model="O1C2")
    cam.start()
    try:
        assert wait_until(lambda: cam._session_was_healthy())
        first.kill()  # routine session drop
        assert wait_until(lambda: len(cmds) >= 2)
    finally:
        cam.stop()
    assert [_profile_of(c) for c in cmds[:2]] == [camera.LOW_LATENCY, camera.LOW_LATENCY]


def test_restart_tries_the_low_latency_profile_again(commands):
    queue, cmds = commands
    queue.extend([FakeFfmpeg([JPEG_A, JPEG_B]), FakeFfmpeg([JPEG_A, JPEG_B])])
    cam = CameraStream("192.168.0.101", "x", 322)
    cam._fallback = True
    cam.start()
    try:
        assert wait_until(lambda: cmds)
    finally:
        cam.stop()
    assert _profile_of(cmds[0]) == camera.LOW_LATENCY


def test_model_learned_while_streaming_applies_to_the_next_session():
    cam = CameraStream(P2S_IP, "x", 322)
    assert cam._session_profile() == camera.LOW_LATENCY
    cam.model = "N7"
    assert cam._session_profile() == camera.TOLERANT


# --- model on PrinterConfig: backfill, relocation, add, edit -----------------------

@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(discovery, "tcp_reachable", lambda ip, port=8883, timeout=3.0: False)


def _manager(model=""):
    cfg = PrinterConfig(id="p2s", name="P2S", ip=P2S_IP, access_code="x",
                        serial=P2S_SERIAL, camera_port=322, model=model)
    return PrinterManager(AppConfig(printers=[cfg]), discovery_port=None)


def _saved(appdata):
    path = appdata / "Bamboozle" / "config.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def _history(appdata):
    return list((appdata / "Bamboozle" / "history").glob("*.json"))


def test_config_without_model_loads_with_an_empty_model():
    cfg = PrinterConfig.model_validate(
        {"name": "P2S", "ip": P2S_IP, "access_code": "x", "serial": P2S_SERIAL})
    assert cfg.model == ""


def test_announcement_backfills_the_model_without_reconnecting(isolated_appdata):
    async def scenario():
        mgr = _manager()
        await mgr.start()
        conn = mgr.get_connection("p2s")
        mgr._on_announcement(Announcement(ip=P2S_IP, serial=P2S_SERIAL.lower(), model="N7"))
        await asyncio.sleep(0.05)
        same_conn = mgr.get_connection("p2s") is conn
        await mgr.stop()
        return mgr, conn, same_conn

    mgr, conn, same_conn = asyncio.run(scenario())
    assert same_conn, "a model-only change must not rebuild the connection"
    assert conn.cfg.model == "N7"
    assert conn.camera.model == "N7"
    assert mgr.config.printers[0].model == "N7"
    assert _saved(isolated_appdata)["printers"][0]["model"] == "N7"
    assert _history(isolated_appdata) == []


def test_unchanged_model_is_not_saved_again(isolated_appdata, monkeypatch):
    saves = []
    monkeypatch.setattr("bamboozle.printer_manager.save_config", lambda cfg: saves.append(cfg))

    async def scenario():
        mgr = _manager(model="N7")
        await mgr.start()
        mgr._on_announcement(Announcement(ip=P2S_IP, serial=P2S_SERIAL, model="N7"))
        mgr._on_announcement(Announcement(ip=P2S_IP, serial=P2S_SERIAL, model=""))
        await asyncio.sleep(0.05)
        await mgr.stop()

    asyncio.run(scenario())
    assert saves == []


def test_announcement_from_another_ip_does_not_set_the_model(isolated_appdata):
    """Only the printer at its configured IP (or a cert-verified relocation) is trusted."""
    async def scenario():
        mgr = _manager()
        await mgr.start()
        mgr._on_announcement(Announcement(ip="192.168.0.250", serial=P2S_SERIAL, model="N7"))
        await asyncio.sleep(0.05)
        model = mgr.get_connection("p2s").cfg.model
        await mgr.stop()
        return model

    assert asyncio.run(scenario()) == ""


def test_relocation_records_the_announced_model(isolated_appdata, monkeypatch):
    new_ip = "192.168.0.120"
    monkeypatch.setattr(discovery, "fetch_cert_common_name",
                        lambda ip, port=8883, timeout=5.0: P2S_SERIAL)

    async def scenario():
        mgr = _manager()
        await mgr.start()
        await asyncio.sleep(0.05)
        await mgr.handle_announcement(Announcement(ip=new_ip, serial=P2S_SERIAL, model="N7"))
        conn = mgr.get_connection("p2s")
        await mgr.stop()
        return conn

    conn = asyncio.run(scenario())
    assert (conn.cfg.ip, conn.cfg.model, conn.camera.model) == (new_ip, "N7", "N7")
    assert _saved(isolated_appdata)["printers"][0]["model"] == "N7"


def test_added_printer_takes_its_model_from_discovery(isolated_appdata):
    async def scenario():
        mgr = PrinterManager(AppConfig(), discovery_port=None)
        await mgr.start()
        mgr._on_announcement(Announcement(ip=P2S_IP, serial=P2S_SERIAL, model="N7"))
        await mgr.add_printer(PrinterConfig(
            id="p2s", name="P2S", ip=P2S_IP, access_code="x",
            serial=P2S_SERIAL.lower(), camera_port=322))
        conn = mgr.get_connection("p2s")
        await mgr.stop()
        return conn

    conn = asyncio.run(scenario())
    assert (conn.cfg.model, conn.camera.model) == ("N7", "N7")
    assert _saved(isolated_appdata)["printers"][0]["model"] == "N7"


def _put(mgr, **fields):
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(manager=mgr)))
    return api.update_printer(request, "p2s", PrinterUpdateRequest(**fields))


def test_editing_a_printer_keeps_its_model(isolated_appdata):
    async def scenario():
        mgr = _manager(model="N7")
        await mgr.start()
        await _put(mgr, name="Renamed")
        cfg = mgr.get_connection("p2s").cfg
        await mgr.stop()
        return cfg

    cfg = asyncio.run(scenario())
    assert (cfg.name, cfg.model) == ("Renamed", "N7")


def test_changing_the_serial_drops_the_old_model(isolated_appdata):
    async def scenario():
        mgr = _manager(model="N7")
        await mgr.start()
        await _put(mgr, serial="03919D533005771")
        cfg = mgr.get_connection("p2s").cfg
        await mgr.stop()
        return cfg

    assert asyncio.run(scenario()).model == ""
