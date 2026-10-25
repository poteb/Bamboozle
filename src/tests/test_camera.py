"""Camera reconnect policy, frozen-frame watchdog and port-6000 framing."""
import datetime
import io
import socket
import ssl
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


HEALTHY = 0.05

GOOD = "good"  # delivered frames for at least HEALTHY_SESSION_SECONDS
BLIP = "blip"  # delivered a frame, then nothing more
FAIL = "fail"  # no frames


class ScriptedSessions:
    """Replaces one capture session per call, following the script."""

    def __init__(self, cam, script):
        self.cam = cam
        self.script = list(script)
        self.waits = []
        self.frame_during_wait = []

    def session(self):
        if not self.script:
            self.cam._running = False
            return
        kind = self.script.pop(0)
        if kind == FAIL:
            raise ConnectionError("refused")
        self.cam._publish(JPEG_A)
        if kind == GOOD:
            time.sleep(HEALTHY + 0.02)
            self.cam._publish(JPEG_B)
        raise ConnectionError("dropped")

    def wait(self, seconds):
        self.waits.append(seconds)
        self.frame_during_wait.append(self.cam.get_frame())


@pytest.fixture
def run_script(monkeypatch):
    monkeypatch.setattr(CameraStream, "HEALTHY_SESSION_SECONDS", HEALTHY)

    def run(script):
        cam = CameraStream("192.168.0.100", "x", 322)
        fake = ScriptedSessions(cam, script)
        cam._capture_session = fake.session
        cam._wait = fake.wait
        cam._running = True
        cam._thread = threading.current_thread()
        cam._capture_loop()
        return fake

    return run


def test_failed_sessions_back_off_exponentially_to_a_cap(run_script):
    fake = run_script([FAIL] * 7)
    assert fake.waits == [3, 6, 12, 24, 48, 60, 60]


def test_healthy_session_respawns_fast_and_resets_backoff(run_script):
    fake = run_script([FAIL, FAIL, GOOD, FAIL, FAIL])
    assert fake.waits == [3, 6, CameraStream.RESPAWN_SECONDS, 3, 6]


def test_sessions_that_die_right_after_a_frame_still_back_off(run_script):
    """One frame then a drop, over and over, must not hammer the printer."""
    fake = run_script([BLIP, BLIP, BLIP])
    assert fake.waits == [3, 6, 12]


def test_last_frame_stays_visible_during_fast_respawn(run_script):
    fake = run_script([GOOD, FAIL])
    assert fake.frame_during_wait[0] == JPEG_B


def test_session_without_frames_clears_the_picture(run_script):
    fake = run_script([GOOD, FAIL])
    assert fake.frame_during_wait[1] is None


def test_frame_older_than_max_age_is_not_served(monkeypatch):
    monkeypatch.setattr(CameraStream, "FRAME_MAX_AGE_SECONDS", 0.1)
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

    def __init__(self, frames, returncode=None, stderr=b""):
        self.frames = frames
        self.returncode = returncode
        self.killed = threading.Event()
        self.stdout = self
        self.stderr = io.BytesIO(stderr)
        self._i = 0

    def close(self):
        pass

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
    assert not live.killed.wait(1.2)
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
    assert not second.killed.wait(1.2)


def test_still_scene_timeout_ends_when_the_picture_changes(ffmpeg, cam, monkeypatch):
    monkeypatch.setattr(CameraStream, "STILL_SCENE_SECONDS", 30)
    queue, _ = ffmpeg
    first = FakeFfmpeg([JPEG_A])
    second = FakeFfmpeg([JPEG_A, JPEG_B] + [JPEG_B] * 1000)
    queue.extend([first, second])
    cam.start()
    assert first.killed.wait(2)
    assert second.killed.wait(3)


def test_stop_kills_a_stalled_ffmpeg_promptly(ffmpeg, monkeypatch):
    monkeypatch.setattr(CameraStream, "FROZEN_SECONDS", 60)
    queue, spawned = ffmpeg
    stalled = FakeFfmpeg([])
    queue.append(stalled)
    cam = CameraStream("192.168.0.101", "x", 322)
    cam.start()
    assert wait_until(lambda: stalled in spawned and cam._interrupt is not None)
    started = time.monotonic()
    cam.stop()
    assert stalled.killed.is_set()
    assert time.monotonic() - started < 2


def test_ffmpeg_that_exits_at_startup_reports_its_stderr(ffmpeg, cam, caplog):
    queue, _ = ffmpeg
    queue.append(FakeFfmpeg([], returncode=1, stderr=b"401 Unauthorized"))
    cam.start()
    assert wait_until(lambda: "401 Unauthorized" in caplog.text)


# -- port 6000 connection --------------------------------------------------------


@pytest.fixture
def silent_printer(tmp_path):
    """A local TLS server that accepts the camera connection and then sends nothing.

    With `handshake=False` it doesn't even answer the TLS handshake. Either
    way the capture thread ends up blocked like on a stalled printer.
    """
    x509 = pytest.importorskip("cryptography.x509")
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "printer")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder().subject_name(name).issuer_name(name)
        .public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now).not_valid_after(now + datetime.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    cert_file, key_file = tmp_path / "cert.pem", tmp_path / "key.pem"
    cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()))
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert_file, key_file)

    server = socket.create_server(("127.0.0.1", 0))
    accepted = []

    def serve(handshake):
        conn, _ = server.accept()
        if handshake:
            conn = ctx.wrap_socket(conn, server_side=True)
        accepted.append(conn)

    def run(handshake=True):
        threading.Thread(target=serve, args=(handshake,), daemon=True).start()
        return server.getsockname()[1], accepted

    yield run
    for conn in accepted:
        conn.close()
    server.close()


@pytest.mark.parametrize("handshake", [True, False], ids=["in-recv", "in-handshake"])
def test_stop_interrupts_a_stalled_raw_socket_promptly(silent_printer, handshake):
    port, accepted = silent_printer(handshake)
    cam = CameraStream("127.0.0.1", "x", port)
    cam.start()
    assert wait_until(lambda: accepted and cam._interrupt is not None)
    time.sleep(0.1)  # let the capture thread block
    thread = cam._thread
    started = time.monotonic()
    cam.stop()
    assert time.monotonic() - started < 2
    assert not thread.is_alive()


def test_restart_waits_for_a_capture_thread_that_did_not_stop(monkeypatch):
    """Never two capture threads at once: the camera often allows one client."""
    monkeypatch.setattr(CameraStream, "STOP_JOIN_SECONDS", 0.05)
    cam = CameraStream("192.168.0.100", "x", 322)
    release = threading.Event()
    entered = []
    running = []
    overlap = []

    def session():
        running.append(1)
        overlap.append(len(running))
        entered.append(threading.current_thread())
        try:
            if len(entered) == 1:
                release.wait(5)  # stuck in I/O that stop() can't interrupt
            else:
                cam._wait(5)
        finally:
            running.pop()

    cam._capture_session = session
    cam.start()
    assert wait_until(lambda: len(entered) == 1)
    cam.stop()
    cam.start()
    time.sleep(0.2)
    assert len(entered) == 1, "second thread started while the first was still connected"
    release.set()
    assert wait_until(lambda: len(entered) == 2)
    assert entered[0] is not entered[1]
    assert max(overlap) == 1
    assert not entered[0].is_alive() or wait_until(lambda: not entered[0].is_alive())
    cam.stop()


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
