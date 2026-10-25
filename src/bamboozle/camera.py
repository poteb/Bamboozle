from __future__ import annotations

import logging
import os
import shutil
import socket
import ssl
import struct
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator

logger = logging.getLogger(__name__)

_ffmpeg_path: str | None = None


def _find_ffmpeg() -> str | None:
    """Find ffmpeg executable, checking PATH and common install locations."""
    global _ffmpeg_path
    if _ffmpeg_path:
        return _ffmpeg_path

    found = shutil.which("ffmpeg")
    if found:
        _ffmpeg_path = found
        return found

    prog_files = os.environ.get("ProgramFiles", r"C:\Program Files")
    if os.path.isdir(prog_files):
        for entry in os.listdir(prog_files):
            if "ffmpeg" in entry.lower():
                candidate = os.path.join(prog_files, entry, "bin", "ffmpeg.exe")
                if os.path.isfile(candidate):
                    _ffmpeg_path = candidate
                    return candidate

    return None


JPEG_SOI = b"\xff\xd8"
JPEG_EOI = b"\xff\xd9"
RAW_HEADER_SIZE = 16
MAX_RAW_PAYLOAD = 10_000_000


def raw_frames(recv: Callable[[int], bytes], buf: bytearray) -> Iterator[bytes]:
    """Yield JPEG frames from the port-6000 stream until the connection closes.

    Each frame is a 16-byte header (payload size, little-endian, first) and
    then the JPEG. `buf` holds bytes already read. An implausible size or a
    payload that isn't a JPEG means we've lost the frame boundary. Skipping
    would only parse the next "header" from the middle of a frame, so raise
    ConnectionError and let the caller reconnect.
    """
    while True:
        _fill(recv, buf, RAW_HEADER_SIZE)
        size = int.from_bytes(buf[0:4], byteorder="little")
        if not 0 < size <= MAX_RAW_PAYLOAD:
            raise ConnectionError(f"Camera stream out of sync (payload size {size})")
        _fill(recv, buf, RAW_HEADER_SIZE + size)
        frame = bytes(buf[RAW_HEADER_SIZE : RAW_HEADER_SIZE + size])
        del buf[: RAW_HEADER_SIZE + size]
        if not (frame.startswith(JPEG_SOI) and frame.endswith(JPEG_EOI)):
            raise ConnectionError("Camera stream out of sync (payload is not a JPEG)")
        yield frame


def _fill(recv: Callable[[int], bytes], buf: bytearray, n: int) -> None:
    while len(buf) < n:
        chunk = recv(65536)
        if not chunk:
            raise ConnectionError("Connection closed")
        buf += chunk


def jpeg_frames(read: Callable[[int], bytes]) -> Iterator[bytes]:
    """Yield the JPEGs in an MJPEG byte stream (ffmpeg stdout) until EOF."""
    buf = b""
    while True:
        chunk = read(4096)
        if not chunk:
            return
        buf += chunk
        while True:
            soi = buf.find(JPEG_SOI)
            if soi == -1:
                buf = b""
                break
            eoi = buf.find(JPEG_EOI, soi + 2)
            if eoi == -1:
                buf = buf[soi:]
                break
            yield buf[soi : eoi + 2]
            buf = buf[eoi + 2 :]


def rtsp_command(ffmpeg: str, url: str) -> list[str]:
    """The ffmpeg command line that turns the printer's RTSPS feed into MJPEG on stdout."""
    return [
        ffmpeg,
        "-hide_banner",
        "-loglevel", "error",
        "-rtsp_transport", "tcp",
        "-tls_verify", "0",
        "-timeout", "10000000",
        "-i", url,
        "-f", "mjpeg",
        "-q:v", "5",
        "-r", "10",
        "-an",
        "pipe:1",
    ]


class _FreezeWatch:
    """Tracks when an ffmpeg session last delivered a picture different from the one before.

    `frozen_on` is the picture the previous ffmpeg froze on. If a fresh ffmpeg
    shows exactly that picture, the scene is still rather than the stream
    dead, so the long timeout applies until the picture changes.
    """

    def __init__(self, frozen_after: float, still_after: float, frozen_on: bytes | None):
        self._frozen_after = frozen_after
        self._still_after = still_after
        self.frozen_on = frozen_on
        self.timeout = frozen_after
        self.last: bytes | None = None
        self.tripped = False
        self._changed_at = time.monotonic()

    def feed(self, frame: bytes) -> None:
        if frame == self.last:
            return
        self.last = frame
        self._changed_at = time.monotonic()
        if self.frozen_on is not None and frame == self.frozen_on:
            self.timeout = self._still_after
        else:
            self.frozen_on = None
            self.timeout = self._frozen_after

    def frozen(self) -> bool:
        return time.monotonic() - self._changed_at > self.timeout

    def trip(self) -> None:
        self.tripped = True
        self.frozen_on = self.last


class CameraStream:
    """Captures JPEG frames from a Bambu Lab printer camera.

    Supports two protocols:
    - Raw TLS socket (port 6000): A1, P1, P1S series
    - RTSP over TLS (port 322): X1, X1C, H2C, H2D, P2 series

    Sessions drop routinely (an X1/P2 RTSP session often lasts about a
    minute). A healthy session (frames for at least HEALTHY_SESSION_SECONDS)
    is respawned after RESPAWN_SECONDS, with the last frame kept on screen.
    Other sessions back off exponentially from BACKOFF_MIN_SECONDS to
    BACKOFF_MAX_SECONDS, so a printer that drops us right after a frame isn't
    hammered. A session that delivered no frames also clears the picture.
    """

    HEALTHY_SESSION_SECONDS = 10.0
    RESPAWN_SECONDS = 0.5
    BACKOFF_MIN_SECONDS = 3.0
    BACKOFF_MAX_SECONDS = 60.0
    # A frame this old is not served, so a dead stream shows "connecting".
    FRAME_MAX_AGE_SECONDS = 20.0
    # ffmpeg is restarted when the picture hasn't changed for this long.
    # A dead upstream can leave it repeating its last frame indefinitely.
    FROZEN_SECONDS = 20.0
    STILL_SCENE_SECONDS = 300.0
    WATCH_INTERVAL_SECONDS = 1.0
    FFMPEG_STARTUP_SECONDS = 1.0

    def __init__(self, ip: str, access_code: str, camera_port: int = 6000):
        self._ip = ip
        self._access_code = access_code
        self._port = camera_port
        self._frame: bytes | None = None
        self._frame_at = 0.0
        self._lock = threading.Lock()
        self._running = False
        self._stopped = threading.Event()
        self._thread: threading.Thread | None = None
        self._proc: subprocess.Popen | None = None
        self._sock: socket.socket | None = None
        self._session_frames = 0
        self._frozen_on: bytes | None = None

    @property
    def available(self) -> bool:
        return self.get_frame() is not None

    def start(self) -> None:
        if self._running:
            return
        if self._thread is not None and self._thread.is_alive():
            # A capture thread that didn't stop in time; a second one would
            # fight it for a camera that often accepts only one client.
            logger.warning("Camera for %s is still stopping, not starting", self._ip)
            return
        self._running = True
        self._stopped.clear()
        self._thread = threading.Thread(target=self._capture_loop, daemon=True)
        self._thread.start()
        logger.info("Camera stream started for %s:%d", self._ip, self._port)

    def stop(self) -> None:
        self._running = False
        self._stopped.set()
        proc = self._proc
        if proc is not None:
            _kill(proc)
        sock = self._sock
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()
        if self._thread:
            self._thread.join(timeout=10)
            if not self._thread.is_alive():
                self._thread = None
        with self._lock:
            self._frame = None
        self._frozen_on = None
        logger.info("Camera stream stopped for %s", self._ip)

    def get_frame(self) -> bytes | None:
        with self._lock:
            if self._frame is None or time.monotonic() - self._frame_at > self.FRAME_MAX_AGE_SECONDS:
                return None
            return self._frame

    def _publish(self, frame: bytes) -> None:
        with self._lock:
            self._frame = frame
            self._frame_at = time.monotonic()
        self._session_frames += 1

    def _wait(self, seconds: float) -> None:
        self._stopped.wait(seconds)

    def _capture_loop(self) -> None:
        backoff = self.BACKOFF_MIN_SECONDS
        while self._running:
            self._session_frames = 0
            started = time.monotonic()
            try:
                self._capture_session()
            except Exception as e:
                if self._running:  # not just stop() closing the socket
                    logger.error("Camera error for %s:%d: %s", self._ip, self._port, e)
            if not self._running:
                break

            lasted = time.monotonic() - started
            if self._session_frames and lasted >= self.HEALTHY_SESSION_SECONDS:
                backoff = self.BACKOFF_MIN_SECONDS
                delay = self.RESPAWN_SECONDS
            else:
                if not self._session_frames:
                    with self._lock:
                        self._frame = None
                delay = backoff
                backoff = min(backoff * 2, self.BACKOFF_MAX_SECONDS)
            logger.info("Camera reconnecting to %s in %.1fs", self._ip, delay)
            self._wait(delay)

    def _capture_session(self) -> None:
        if self._port == 322:
            self._capture_rtsp()
        else:
            self._capture_raw_socket()

    # -- Raw TLS socket protocol (A1, P1, P1S on port 6000) --

    def _capture_raw_socket(self) -> None:
        auth_data = self._build_auth_packet()

        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

        with socket.create_connection((self._ip, self._port), timeout=10) as sock:
            # Enable TCP keepalive to detect dead connections
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            with ctx.wrap_socket(sock, server_hostname=self._ip) as ssl_sock:
                self._sock = ssl_sock
                try:
                    self._read_raw_socket(ssl_sock, auth_data)
                finally:
                    self._sock = None

    def _read_raw_socket(self, ssl_sock: ssl.SSLSocket, auth_data: bytes) -> None:
        ssl_sock.sendall(auth_data)
        ssl_sock.settimeout(60.0)
        # There is no separate auth reply: the first 16 bytes are the
        # first frame's header.
        read_buf = bytearray()
        try:
            _fill(ssl_sock.recv, read_buf, RAW_HEADER_SIZE)
        except ConnectionError:
            raise ConnectionError(f"Bad auth response: {len(read_buf)} bytes") from None

        # Check for 24-byte newer protocol response
        if read_buf[4:8] == b"\x3f\x01\x03\x00":
            raise ConnectionError(
                "Printer uses RTSP protocol. Set camera port to 322."
            )

        logger.info("Camera connected to %s via raw socket", self._ip)
        for frame in raw_frames(ssl_sock.recv, read_buf):
            if not self._running:
                break
            self._publish(frame)

    def _build_auth_packet(self) -> bytearray:
        auth = bytearray()
        auth += struct.pack("<I", 0x40)
        auth += struct.pack("<I", 0x3000)
        auth += struct.pack("<I", 0)
        auth += struct.pack("<I", 0)
        username = "bblp"
        for c in username:
            auth += struct.pack("<c", c.encode("ascii"))
        auth += b"\x00" * (32 - len(username))
        for c in self._access_code:
            auth += struct.pack("<c", c.encode("ascii"))
        auth += b"\x00" * (32 - len(self._access_code))
        return auth

    # -- RTSP over TLS protocol (X1, H2C, H2D, P2 on port 322) --

    def _capture_rtsp(self) -> None:
        ffmpeg = _find_ffmpeg()
        if not ffmpeg:
            logger.error(
                "ffmpeg not found — required for RTSP camera (port 322). "
                "Download from https://www.gyan.dev/ffmpeg/builds/"
            )
            self._running = False
            return

        url = f"rtsps://bblp:{self._access_code}@{self._ip}:{self._port}/streaming/live/1"
        proc = subprocess.Popen(
            rtsp_command(ffmpeg, url),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=10 * 1024 * 1024,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        self._proc = proc
        # Drain stderr as it comes: a full pipe would block ffmpeg, which the
        # watchdog would then mistake for a frozen camera.
        stderr_tail: deque[bytes] = deque(maxlen=20)
        drain = threading.Thread(target=_drain, args=(proc.stderr, stderr_tail), daemon=True)
        drain.start()

        def stderr_text() -> str:
            drain.join(timeout=1)
            return b"".join(stderr_tail).decode(errors="replace").strip()[-300:]

        watch: _FreezeWatch | None = None
        try:
            if self._stopped.wait(self.FFMPEG_STARTUP_SECONDS):
                return
            if proc.poll() is not None:
                raise ConnectionError(f"ffmpeg exited: {stderr_text()}")

            logger.info("Camera connected to %s via ffmpeg RTSP", self._ip)
            watch = _FreezeWatch(self.FROZEN_SECONDS, self.STILL_SCENE_SECONDS, self._frozen_on)
            threading.Thread(target=self._watch_ffmpeg, args=(proc, watch), daemon=True).start()

            for frame in jpeg_frames(proc.stdout.read):
                if not self._running:
                    break
                watch.feed(frame)
                self._publish(frame)

            if proc.poll() is not None and proc.returncode != 0 and not watch.tripped:
                stderr = stderr_text()
                if stderr:
                    logger.error("ffmpeg error for %s: %s", self._ip, stderr)
        finally:
            self._proc = None
            if watch is not None:
                self._frozen_on = watch.frozen_on
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
            proc.stdout.close()

    def _watch_ffmpeg(self, proc: subprocess.Popen, watch: _FreezeWatch) -> None:
        """Kill ffmpeg when the picture stops changing. The capture loop respawns it."""
        while self._running and proc.poll() is None:
            if watch.frozen():
                logger.warning(
                    "Camera %s: no new frame for %.0fs, restarting ffmpeg", self._ip, watch.timeout
                )
                watch.trip()
                _kill(proc)
                return
            self._stopped.wait(self.WATCH_INTERVAL_SECONDS)


def _drain(pipe, tail: deque[bytes]) -> None:
    """Read a pipe to EOF, keeping only its last lines."""
    with pipe:
        for line in iter(pipe.readline, b""):
            tail.append(line)


def _kill(proc: subprocess.Popen) -> None:
    try:
        if proc.poll() is None:
            proc.kill()
    except OSError:
        pass
