"""Camera reconnect policy, frozen-frame watchdog and port-6000 framing."""
import io
import struct
import threading
import time

import pytest

from bamboozle import camera
from bamboozle.camera import CameraStream, raw_frames

JPEG_A = b"\xff\xd8" + b"A" * 50 + b"\xff\xd9"
JPEG_B = b"\xff\xd8" + b"B" * 50 + b"\xff\xd9"


def wait_until(cond, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.01)
    return False


# -- reconnect policy ---------------------------------------------------------


class ScriptedSessions:
    """Replaces one capture session per call; True = delivered a frame, False = failed."""

    def __init__(self, cam, script):
        self.cam = cam
        self.script = list(script)
        self.waits = []
        self.frame_during_wait = []

    def session(self):
        if not self.script:
            self.cam._running = False
            return
        if self.script.pop(0):
            self.cam._publish(JPEG_A)
            return
        raise ConnectionError("refused")

    def wait(self, seconds):
        self.waits.append(seconds)
        self.frame_during_wait.append(self.cam.get_frame())


def run_script(script):
    cam = CameraStream("192.168.0.100", "x", 322)
    fake = ScriptedSessions(cam, script)
    cam._capture_session = fake.session
    cam._wait = fake.wait
    cam._running = True
    cam._capture_loop()
    return fake


def test_failed_sessions_back_off_exponentially_to_a_cap():
    fake = run_script([False] * 7)
    assert fake.waits == [3, 6, 12, 24, 48, 60, 60]


def test_session_with_frames_respawns_fast_and_resets_backoff():
    fake = run_script([False, False, True, False, False])
    assert fake.waits == [3, 6, CameraStream.RESPAWN_DELAY, 3, 6]


def test_last_frame_stays_visible_during_fast_respawn():
    fake = run_script([True, False])
    assert fake.frame_during_wait[0] == JPEG_A


def test_session_without_frames_clears_the_picture():
    fake = run_script([True, False])
    assert fake.frame_during_wait[1] is None


def test_frame_older_than_max_age_is_not_served(monkeypatch):
    monkeypatch.setattr(CameraStream, "FRAME_MAX_AGE", 0.1)
    cam = CameraStream("192.168.0.100", "x", 322)
    cam._publish(JPEG_A)
    assert cam.available and cam.get_frame() == JPEG_A
    time.sleep(0.15)
    assert not cam.available
    assert cam.get_frame() is None


# -- frozen-frame watchdog (RTSP / ffmpeg) -------------------------------------


class FakeFfmpeg:
    """A Popen stand-in whose stdout plays `frames` (cycled) until killed.

    `frames=[]` models an ffmpeg that has stalled and writes nothing.
    """

    def __init__(self, frames):
        self.frames = frames
        self.returncode = None
        self.killed = threading.Event()
        self.stdout = self
        self.stderr = io.BytesIO(b"")
        self._i = 0

    def read(self, n):
        if not self.frames:
            self.killed.wait()
            return b""
        if self.killed.wait(0.02):
            return b""
        frame = self.frames[self._i % len(self.frames)]
        self._i += 1
        return frame

    def poll(self):
        return self.returncode

    def kill(self):
        self.returncode = -9
        self.killed.set()

    terminate = kill

    def wait(self, timeout=None):
        return self.returncode


@pytest.fixture
def ffmpeg(monkeypatch):
    """Queue FakeFfmpeg processes; each Popen call takes the next one."""
    monkeypatch.setattr(camera, "_find_ffmpeg", lambda: "ffmpeg")
    monkeypatch.setattr(CameraStream, "FFMPEG_STARTUP_SECONDS", 0)
    monkeypatch.setattr(CameraStream, "WATCH_INTERVAL", 0.02)
    monkeypatch.setattr(CameraStream, "FROZEN_SECONDS", 0.3)
    monkeypatch.setattr(CameraStream, "RESPAWN_DELAY", 0.05)
    queue = []
    spawned = []

    def popen(cmd, **kwargs):
        proc = queue.pop(0) if queue else FakeFfmpeg([])
        spawned.append(proc)
        return proc

    monkeypatch.setattr(camera.subprocess, "Popen", popen)
    yield queue, spawned


@pytest.fixture
def cam():
    c = CameraStream("192.168.0.101", "x", 322)
    yield c
    c.stop()


def test_identical_frames_for_too_long_restart_ffmpeg(ffmpeg, cam):
    queue, spawned = ffmpeg
    frozen = FakeFfmpeg([JPEG_A])
    queue.append(frozen)
    cam.start()
    assert frozen.killed.wait(2)
    assert wait_until(lambda: len(spawned) >= 2), "ffmpeg was not respawned"


def test_changing_frames_keep_ffmpeg_running(ffmpeg, cam):
    queue, _ = ffmpeg
    live = FakeFfmpeg([JPEG_A, JPEG_B])
    queue.append(live)
    cam.start()
    assert not live.killed.wait(0.8)
    assert cam.get_frame() in (JPEG_A, JPEG_B)


def test_ffmpeg_that_writes_nothing_is_restarted(ffmpeg, cam):
    queue, _ = ffmpeg
    stalled = FakeFfmpeg([])
    queue.append(stalled)
    cam.start()
    assert stalled.killed.wait(2)


def test_still_scene_after_a_restart_gets_the_long_timeout(ffmpeg, cam, monkeypatch):
    """If the fresh ffmpeg shows the very same picture, the scene is still, not frozen."""
    monkeypatch.setattr(CameraStream, "STILL_SCENE_SECONDS", 30)
    queue, _ = ffmpeg
    first, second = FakeFfmpeg([JPEG_A]), FakeFfmpeg([JPEG_A])
    queue.extend([first, second])
    cam.start()
    assert first.killed.wait(2)
    assert not second.killed.wait(0.8)


def test_still_scene_timeout_ends_when_the_picture_changes(ffmpeg, cam, monkeypatch):
    monkeypatch.setattr(CameraStream, "STILL_SCENE_SECONDS", 30)
    queue, _ = ffmpeg
    first = FakeFfmpeg([JPEG_A])
    second = FakeFfmpeg([JPEG_A, JPEG_B] + [JPEG_B] * 1000)
    queue.extend([first, second])
    cam.start()
    assert first.killed.wait(2)
    assert second.killed.wait(2)


def test_stop_kills_a_stalled_ffmpeg_promptly(ffmpeg, monkeypatch):
    monkeypatch.setattr(CameraStream, "FROZEN_SECONDS", 60)
    queue, _ = ffmpeg
    stalled = FakeFfmpeg([])
    queue.append(stalled)
    cam = CameraStream("192.168.0.101", "x", 322)
    cam.start()
    assert wait_until(lambda: cam._proc is stalled)
    started = time.monotonic()
    cam.stop()
    assert stalled.killed.is_set()
    assert time.monotonic() - started < 2


# -- port 6000 framing ---------------------------------------------------------


def packet(payload, size=None):
    header = struct.pack("<I", len(payload) if size is None else size) + b"\x00" * 12
    return header + payload


def chunked(data, n):
    """recv() stand-in that hands out `data` n bytes at a time, then EOF."""
    stream = io.BytesIO(data)
    return lambda _bufsize: stream.read(n)


def test_raw_frames_reassembles_frames_across_reads():
    recv = chunked(packet(JPEG_A) + packet(JPEG_B), 7)
    frames = raw_frames(recv, bytearray())
    assert [next(frames), next(frames)] == [JPEG_A, JPEG_B]
    with pytest.raises(ConnectionError):
        next(frames)


def test_raw_frames_uses_bytes_already_read():
    data = packet(JPEG_A)
    frames = raw_frames(chunked(data[16:], 4096), bytearray(data[:16]))
    assert next(frames) == JPEG_A


@pytest.mark.parametrize("size", [0, 20_000_000])
def test_implausible_payload_size_raises_instead_of_skipping(size):
    recv = chunked(packet(JPEG_A, size=size) + packet(JPEG_B), 4096)
    with pytest.raises(ConnectionError, match="sync"):
        next(raw_frames(recv, bytearray()))


def test_payload_that_is_not_a_jpeg_raises():
    recv = chunked(packet(b"\x00" * 40) + packet(JPEG_B), 4096)
    with pytest.raises(ConnectionError, match="sync"):
        next(raw_frames(recv, bytearray()))
