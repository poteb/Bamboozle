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


class CameraStream:
    """Captures JPEG frames from a Bambu Lab printer camera.

    Supports two protocols:
    - Raw TLS socket (port 6000): A1, P1, P1S series
    - RTSP over TLS (port 322): X1, X1C, H2C, H2D, P2 series
    """

    def __init__(self, ip: str, access_code: str, camera_port: int = 6000):
        self._ip = ip
        self._access_code = access_code
        self._port = camera_port
        self._frame: bytes | None = None
        self._lock = threading.Lock()
        self._running = False
        self._thread: threading.Thread | None = None
        self._connected = False

    @property
    def available(self) -> bool:
        return self._connected and self._frame is not None

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._capture_loop, daemon=True)
        self._thread.start()
        logger.info("Camera stream started for %s:%d", self._ip, self._port)

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=10)
            self._thread = None
        self._connected = False
        self._frame = None
        logger.info("Camera stream stopped for %s", self._ip)

    def get_frame(self) -> bytes | None:
        with self._lock:
            return self._frame

    def _capture_loop(self) -> None:
        backoff = 3.0
        while self._running:
            try:
                if self._port == 322:
                    self._capture_rtsp()
                else:
                    self._capture_raw_socket()
            except Exception as e:
                logger.error("Camera error for %s:%d: %s", self._ip, self._port, e)
            finally:
                self._connected = False
                with self._lock:
                    self._frame = None

            if self._running:
                logger.info("Camera reconnecting to %s in %.0fs", self._ip, backoff)
                deadline = time.monotonic() + backoff
                while self._running and time.monotonic() < deadline:
                    time.sleep(0.5)
                backoff = min(backoff * 2, 60.0)

    # -- Raw TLS socket protocol (A1, P1, P1S on port 6000) --

    def _capture_raw_socket(self) -> None:
        auth_data = self._build_auth_packet()

        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

        with socket.create_connection((self._ip, self._port), timeout=10) as sock:
            # Enable TCP keepalive to detect dead connections
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            ssl_sock = ctx.wrap_socket(sock, server_hostname=self._ip)
            ssl_sock.write(auth_data)
            ssl_sock.settimeout(60.0)

            # Use a read buffer to handle partial reads properly
            read_buf = bytearray()

            # Read auth response (first 16-byte header)
            read_buf += self._sock_read(ssl_sock, 16 - len(read_buf))
            if len(read_buf) < 16:
                raise ConnectionError(f"Bad auth response: {len(read_buf)} bytes")

            # Check for 24-byte newer protocol response
            if read_buf[4:8] == b"\x3f\x01\x03\x00":
                raise ConnectionError(
                    "Printer uses RTSP protocol. Set camera port to 322."
                )

            self._connected = True
            logger.info("Camera connected to %s via raw socket", self._ip)

            while self._running:
                # Ensure we have at least 16 bytes for the header
                while len(read_buf) < 16:
                    chunk = ssl_sock.recv(65536)
                    if not chunk:
                        raise ConnectionError("Connection closed")
                    read_buf += chunk

                # Parse header
                payload_size = int.from_bytes(read_buf[0:4], byteorder="little")
                read_buf = read_buf[16:]

                if payload_size <= 0 or payload_size > 10_000_000:
                    logger.warning("Bad payload size %d from %s, skipping", payload_size, self._ip)
                    continue

                # Read full payload (may already have some in read_buf)
                while len(read_buf) < payload_size:
                    chunk = ssl_sock.recv(65536)
                    if not chunk:
                        raise ConnectionError("Connection closed")
                    read_buf += chunk

                frame_data = bytes(read_buf[:payload_size])
                read_buf = read_buf[payload_size:]

                # Validate JPEG markers and store frame
                if frame_data[:2] == b"\xff\xd8" and frame_data[-2:] == b"\xff\xd9":
                    with self._lock:
                        self._frame = frame_data

    @staticmethod
    def _sock_read(sock: ssl.SSLSocket, n: int) -> bytes:
        """Read at least n bytes from socket."""
        buf = b""
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                return buf
            buf += chunk
        return buf

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

    # -- RTSP over TLS protocol (X1, H2C, H2D on port 322) --

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
        cmd = [
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

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=10 * 1024 * 1024,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        try:
            time.sleep(1.0)
            if proc.poll() is not None:
                stderr = proc.stderr.read().decode(errors="replace").strip()
                raise ConnectionError(f"ffmpeg exited: {stderr[:300]}")

            self._connected = True
            logger.info("Camera connected to %s via ffmpeg RTSP", self._ip)

            buf = b""
            while self._running and proc.poll() is None:
                chunk = proc.stdout.read(4096)
                if not chunk:
                    break
                buf += chunk

                while True:
                    soi = buf.find(b"\xff\xd8")
                    if soi == -1:
                        buf = b""
                        break
                    eoi = buf.find(b"\xff\xd9", soi + 2)
                    if eoi == -1:
                        buf = buf[soi:]
                        break
                    frame = buf[soi : eoi + 2]
                    buf = buf[eoi + 2 :]
                    with self._lock:
                        self._frame = frame

            if proc.poll() is not None and proc.returncode != 0:
                stderr = proc.stderr.read().decode(errors="replace").strip()
                if stderr:
                    logger.error("ffmpeg error for %s: %s", self._ip, stderr[:300])
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
