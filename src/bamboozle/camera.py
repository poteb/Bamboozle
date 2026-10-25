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

from .discovery import model_info

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


# ffmpeg input profiles for the RTSPS camera (port 322). Every profile reads
# without input buffering (`-fflags nobuffer -flags low_delay`); they differ
# in how they probe the stream.
#
# LOW_LATENCY (default): a tiny probe so the stream starts at once.
# TOLERANT (P2S): P2S RTP timestamps don't advance, so the `-r` rate
# conversion would drop every frame after the first. Stamp packets with the
# arrival time instead, and probe long enough to lock onto the stream. It is
# also the fallback when a LOW_LATENCY start fails (`probesize 32` doesn't
# lock on every firmware, and a printer whose model isn't known yet may be
# a P2S).
LOW_LATENCY = "low_latency"
TOLERANT = "tolerant"

_PROFILE_ARGS: dict[str, list[str]] = {
    LOW_LATENCY: ["-probesize", "32", "-analyzeduration", "0"],
    TOLERANT: [
        "-probesize", "1000000",
        "-analyzeduration", "500000",
        "-use_wallclock_as_timestamps", "1",
    ],
}
_TOLERANT_MODELS = frozenset({"P2S"})  # friendly names, see discovery.model_info


def rtsp_profile(model: str) -> str:
    """The ffmpeg profile for a printer's SSDP DevModel code ("" if unknown)."""
    name, _port = model_info(model)
    return TOLERANT if name in _TOLERANT_MODELS else LOW_LATENCY


def rtsp_command(ffmpeg: str, url: str, profile: str = LOW_LATENCY) -> list[str]:
    """The ffmpeg command line that turns the printer's RTSPS feed into MJPEG on stdout."""
    return [
        ffmpeg,
        "-hide_banner",
        "-loglevel", "error",
        "-rtsp_transport", "tcp",
        "-tls_verify", "0",
        "-timeout", "10000000",
        "-fflags", "nobuffer",
        "-flags", "low_delay",
        "-max_delay", "500000",
        *_PROFILE_ARGS[profile],
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

    RTSP sessions use the ffmpeg profile for `model` (the SSDP DevModel code,
    which may be learned while streaming). For a LOW_LATENCY model, until
    LOW_LATENCY has shown a moving picture (2+ distinct frames) this run, a
    LOW_LATENCY session that was unhealthy or froze switches to TOLERANT, and
    a TOLERANT session without a moving picture switches back, since the
    printer may just have been unreachable or busy. Once LOW_LATENCY has
    worked, routine drops and still scenes keep it.
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
    STOP_JOIN_SECONDS = 10.0

    def __init__(self, ip: str, access_code: str, camera_port: int = 6000, model: str = ""):
        self._ip = ip
        self._access_code = access_code
        self._port = camera_port
        self.model = model
        # Per run, set in the capture thread: use TOLERANT from now on, and
        # whether LOW_LATENCY has delivered a moving picture.
        self._fallback = False
        self._low_latency_works = False
        self._profile = LOW_LATENCY  # the current session's
        self._frame: bytes | None = None
        self._frame_at = 0.0
        self._lock = threading.Lock()
        self._running = False
        # One per run: start() makes a fresh one, so a superseded thread's set
        # event can't stop the new run.
        self._stopped = threading.Event()
        self._thread: threading.Thread | None = None
        # How stop() interrupts the blocking I/O of the current session
        # (kill ffmpeg, close the socket). Guarded by _io_lock.
        self._interrupt: Callable[[], None] | None = None
        self._io_lock = threading.Lock()
        self._session_frames = 0
        self._session_first_at = 0.0
        self._session_last_at = 0.0
        self._session_froze = False  # the watchdog ended the session
        self._session_pictures = 0  # changes of picture this session
        self._session_last_frame: bytes | None = None
        self._frozen_on: bytes | None = None

    @property
    def available(self) -> bool:
        return self.get_frame() is not None

    def start(self) -> None:
        if self._running:
            return
        # A capture thread from an earlier run may still be blocked in I/O.
        # The new thread waits for it, since the camera often accepts only
        # one client. It's no longer self._thread, so it won't carry on.
        previous = self._thread if self._thread and self._thread.is_alive() else None
        self._running = True
        self._stopped = threading.Event()
        self._thread = threading.Thread(target=self._capture_loop, args=(previous,), daemon=True)
        self._thread.start()
        logger.info("Camera stream started for %s:%d", self._ip, self._port)

    def stop(self) -> None:
        """Stop capturing. Blocks up to STOP_JOIN_SECONDS, so call it off the event loop."""
        with self._io_lock:
            self._running = False
            self._stopped.set()
            interrupt = self._interrupt
        if interrupt is not None:
            interrupt()
        thread = self._thread
        if thread:
            thread.join(timeout=self.STOP_JOIN_SECONDS)
            if not thread.is_alive() and self._thread is thread:
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

    def _active(self) -> bool:
        """True in the capture thread of the current run until stop()."""
        return self._running and threading.current_thread() is self._thread

    def _publish(self, frame: bytes) -> None:
        now = time.monotonic()
        with self._lock:
            self._frame = frame
            self._frame_at = now
        if not self._session_frames:
            self._session_first_at = now
        if frame != self._session_last_frame:
            self._session_pictures += 1
            self._session_last_frame = frame
        self._session_frames += 1
        self._session_last_at = now

    def _session_was_healthy(self) -> bool:
        return (
            self._session_frames > 0
            and self._session_last_at - self._session_first_at >= self.HEALTHY_SESSION_SECONDS
        )

    def _session_profile(self) -> str:
        return TOLERANT if self._fallback else rtsp_profile(self.model)

    def _choose_fallback(self, profile: str, healthy: bool) -> None:
        """After an RTSP session, decide whether the next one falls back to TOLERANT."""
        if self._port != 322 or rtsp_profile(self.model) != LOW_LATENCY:
            return
        moving = self._session_pictures >= 2
        if profile == LOW_LATENCY:
            if moving:
                self._low_latency_works = True
            elif not self._low_latency_works and (not healthy or self._session_froze):
                logger.info("Camera %s: trying the tolerant ffmpeg profile", self._ip)
                self._fallback = True
        elif not moving:
            logger.info("Camera %s: trying the low-latency ffmpeg profile again", self._ip)
            self._fallback = False

    def _wait(self, seconds: float) -> None:
        self._stopped.wait(seconds)

    def _set_interrupt(self, interrupt: Callable[[], None]) -> bool:
        """Let stop() interrupt this session's I/O. False if the run is already over."""
        with self._io_lock:
            if not self._active():
                return False
            self._interrupt = interrupt
            return True

    def _clear_interrupt(self, interrupt: Callable[[], None]) -> None:
        with self._io_lock:
            if self._interrupt is interrupt:
                self._interrupt = None

    def _capture_loop(self, previous: threading.Thread | None = None) -> None:
        if previous is not None:
            previous.join()
        self._fallback = False
        self._low_latency_works = False
        backoff = self.BACKOFF_MIN_SECONDS
        while self._active():
            self._session_frames = 0
            self._session_froze = False
            self._session_pictures = 0
            self._session_last_frame = None
            self._profile = profile = self._session_profile()
            try:
                self._capture_session()
            except Exception as e:
                if self._active():  # not just stop() closing the socket
                    logger.error("Camera error for %s:%d: %s", self._ip, self._port, e)
            if not self._active():
                break

            healthy = self._session_was_healthy()
            self._choose_fallback(profile, healthy)
            if healthy:
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
            with ctx.wrap_socket(
                sock, server_hostname=self._ip, do_handshake_on_connect=False
            ) as ssl_sock:
                interrupt = lambda: _close(ssl_sock)  # noqa: E731
                if not self._set_interrupt(interrupt):
                    return
                try:
                    ssl_sock.do_handshake()
                    self._read_raw_socket(ssl_sock, auth_data)
                finally:
                    self._clear_interrupt(interrupt)

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
            if not self._active():
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
        profile = self._profile
        proc = subprocess.Popen(
            rtsp_command(ffmpeg, url, profile),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=10 * 1024 * 1024,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        # Drain stderr as it comes: a full pipe would block ffmpeg, which the
        # watchdog would then mistake for a frozen camera.
        stderr_tail: deque[bytes] = deque(maxlen=20)
        drain = threading.Thread(target=_drain, args=(proc.stderr, stderr_tail), daemon=True)
        drain.start()

        def stderr_text() -> str:
            drain.join(timeout=1)
            if drain.is_alive():  # still appending; don't read a moving deque
                return ""
            return b"".join(stderr_tail).decode(errors="replace").strip()[-300:]

        interrupt = lambda: _kill(proc)  # noqa: E731
        watch: _FreezeWatch | None = None
        try:
            if not self._set_interrupt(interrupt):
                return
            if self._stopped.wait(self.FFMPEG_STARTUP_SECONDS):
                return
            if proc.poll() is not None:
                raise ConnectionError(f"ffmpeg exited: {stderr_text()}")

            logger.info("Camera connected to %s via ffmpeg RTSP (%s profile)", self._ip, profile)
            watch = _FreezeWatch(self.FROZEN_SECONDS, self.STILL_SCENE_SECONDS, self._frozen_on)
            threading.Thread(
                target=self._watch_ffmpeg, args=(proc, watch, self._stopped), daemon=True
            ).start()

            for frame in jpeg_frames(proc.stdout.read):
                if not self._active():
                    break
                watch.feed(frame)
                self._publish(frame)

            if self._active() and not watch.tripped:
                try:
                    returncode = proc.wait(timeout=2)  # stdout hit EOF; let it exit
                except subprocess.TimeoutExpired:
                    returncode = None
                stderr = stderr_text() if returncode else ""
                if stderr:
                    logger.error("ffmpeg error for %s: %s", self._ip, stderr)
        finally:
            self._clear_interrupt(interrupt)
            if watch is not None and threading.current_thread() is self._thread:
                self._frozen_on = watch.frozen_on  # not from a superseded run
                self._session_froze = watch.tripped
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
            proc.stdout.close()

    def _watch_ffmpeg(
        self, proc: subprocess.Popen, watch: _FreezeWatch, stopped: threading.Event
    ) -> None:
        """Kill ffmpeg when the picture stops changing. The capture loop respawns it."""
        while not stopped.is_set() and proc.poll() is None:
            if watch.frozen():
                logger.warning(
                    "Camera %s: no new frame for %.0fs, restarting ffmpeg", self._ip, watch.timeout
                )
                watch.trip()
                _kill(proc)
                return
            stopped.wait(self.WATCH_INTERVAL_SECONDS)


def _drain(pipe, tail: deque[bytes]) -> None:
    """Read a pipe to EOF, keeping only its last lines."""
    with pipe:
        for line in iter(pipe.readline, b""):
            tail.append(line)


def _close(sock: socket.socket) -> None:
    """Wake a recv blocked in another thread. On Windows shutdown alone doesn't."""
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    sock.close()


def _kill(proc: subprocess.Popen) -> None:
    try:
        if proc.poll() is None:
            proc.kill()
    except OSError:
        pass
