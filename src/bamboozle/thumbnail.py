"""Plate thumbnail for the current print job, from the 3MF on the printer's
external storage (SD card / USB drive), over implicit FTPS on port 990.

Blocking; callers run it off the event loop. Returns the PNG, or None when the
job's 3MF isn't on the printer (or holds no picture for the plate). Anything
else (connect / login failure, timeout, cancel) raises, so the caller can
retry later.
"""
from __future__ import annotations

import ftplib
import io
import logging
import re
import socket
import ssl
import threading
import time
import zipfile

logger = logging.getLogger(__name__)

# Where print files land, in lookup order: /cache is where Bambu Studio sends
# them on A1 / P1; the others are used by SD prints and other models.
SEARCH_DIRS = ("/cache", "/", "/model", "/data")
# Every blocking socket operation (control and data connections).
SOCKET_TIMEOUT_SECONDS = 15.0
# The whole fetch. A big 3MF can take a minute or more on a P1S.
FETCH_DEADLINE_SECONDS = 240.0


class FetchCancelled(Exception):
    """The caller asked the fetch to stop (printer removed, app shutting down)."""


class _ReusedSessionFTP(ftplib.FTP_TLS):
    """FTP_TLS subclass that reuses the control connection's TLS session
    for data connections. Required by newer Bambu Lab printers (H2C/H2D)."""

    def ntransfercmd(self, cmd, rest=None):
        conn, size = ftplib.FTP.ntransfercmd(self, cmd, rest)
        if self._prot_p:
            conn = self.context.wrap_socket(
                conn,
                server_hostname=self.host,
                session=self.sock.session,
            )
        return conn, size


def _tls_context() -> ssl.SSLContext:
    """TLS 1.2 exactly. P2S / H2C need the cap; every model speaks 1.2, and
    1.2 keeps the data channel's session reuse simple (no 1.3 tickets)."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.maximum_version = ssl.TLSVersion.TLSv1_2
    return ctx


def _new_ftp(ctx: ssl.SSLContext) -> _ReusedSessionFTP:
    # `timeout` also applies to the data connections ftplib opens.
    return _ReusedSessionFTP(context=ctx, timeout=SOCKET_TIMEOUT_SECONDS)


def _connect_ftp(ip: str, access_code: str, port: int = 990) -> ftplib.FTP_TLS:
    """Connect to printer's implicit FTPS on port 990."""
    ctx = _tls_context()
    sock = socket.create_connection((ip, port), timeout=SOCKET_TIMEOUT_SECONDS)
    ftp = None
    try:
        sock = ctx.wrap_socket(sock, server_hostname=ip)
        ftp = _new_ftp(ctx)
        ftp.host = ip
        ftp.sock = sock
        ftp.af = socket.AF_INET
        ftp.file = ftp.sock.makefile("r", encoding="utf-8")
        ftp.getresp()
        ftp.login("bblp", access_code)
        ftp.prot_p()
        return ftp
    except BaseException:
        # e.g. "421 too many connections": don't leak the socket
        if ftp is not None:
            ftp.close()  # also drops the makefile() reader holding it open
        sock.close()
        raise


def candidate_names(gcode_file: str, subtask_name: str) -> list[str]:
    """3MF file names the job may be stored under, most likely first.

    `gcode_file` is the 3MF name for prints sent from Bambu Studio, or a path
    inside the printer (`/data/Metadata/plate_2.gcode`) for others;
    `subtask_name` is the project name, without an extension.
    """
    names: list[str] = []
    base = gcode_file.rsplit("/", 1)[-1]
    if base.lower().endswith(".3mf"):
        names.append(base)
    sub = subtask_name.strip()
    if sub:
        for stem in (sub, sub.replace(" ", "_")):
            if stem.lower().endswith(".3mf"):
                names.append(stem)
            else:
                names += [f"{stem}.gcode.3mf", f"{stem}.3mf"]
    return list(dict.fromkeys(names))


def fetch_thumbnail(
    ip: str,
    access_code: str,
    gcode_file: str,
    subtask_name: str = "",
    *,
    cancel: threading.Event | None = None,
    deadline_s: float = FETCH_DEADLINE_SECONDS,
) -> bytes | None:
    """Fetch the plate thumbnail for the current print job (see module doc).

    One FTPS connection walks SEARCH_DIRS for the first candidate name, then
    downloads that 3MF and takes the plate's picture out of it. Setting
    `cancel`, or passing `deadline_s`, abandons the download.
    """
    names = candidate_names(gcode_file, subtask_name)
    if not names:
        return None
    stop_at = time.monotonic() + deadline_s

    def check() -> None:
        if cancel is not None and cancel.is_set():
            raise FetchCancelled()
        if time.monotonic() > stop_at:
            raise TimeoutError(f"thumbnail fetch from {ip} took over {deadline_s:.0f}s")

    ftp = _connect_ftp(ip, access_code)
    try:
        path = _locate(ftp, names, check)
        if path is not None:
            buf = io.BytesIO()

            def sink(chunk: bytes) -> None:
                check()
                buf.write(chunk)

            ftp.retrbinary(f"RETR {path}", sink)
    except BaseException:
        ftp.close()  # the control channel may be mid-reply; don't wait on QUIT
        raise
    _quit(ftp)
    if path is None:
        logger.debug("No 3MF for %r / %r on %s", gcode_file, subtask_name, ip)
        return None
    buf.seek(0)
    # Only a G-code path names the plate; a 3MF name is the project's.
    plate = _plate_number(gcode_file) if gcode_file.lower().endswith(".gcode") else None
    return _extract_plate_thumbnail(buf, plate)


def _quit(ftp: ftplib.FTP) -> None:
    try:
        ftp.quit()
    except Exception:
        ftp.close()


def _locate(ftp: ftplib.FTP, names: list[str], check) -> str | None:
    """Path of the first candidate found, walking SEARCH_DIRS in order."""
    for folder in SEARCH_DIRS:
        check()
        try:
            ftp.cwd(folder)
        except ftplib.error_perm:
            continue  # not on this model / no storage inserted
        try:
            listing = {entry.rsplit("/", 1)[-1] for entry in ftp.nlst()}
        except ftplib.error_perm:
            continue  # vsFTPd answers an empty folder with 550
        for name in names:
            if name in listing:
                return f"{folder.rstrip('/')}/{name}"
    return None


def _plate_number(gcode_file: str) -> int | None:
    """"/data/Metadata/plate_2.gcode" -> 2; None when the name doesn't say."""
    m = re.search(r"plate_(\d+)", gcode_file)
    return int(m.group(1)) if m else None


def _extract_plate_thumbnail(
    threemf_buf: io.BytesIO, plate_num: int | None
) -> bytes | None:
    """The plate's picture from a 3MF (ZIP) file, or None."""
    try:
        with zipfile.ZipFile(threemf_buf) as zf:
            members = set(zf.namelist())
            if plate_num is None:
                # A 3MF sent to print holds G-code only for the sliced plate.
                sliced = [m for m in members if re.fullmatch(r"Metadata/plate_\d+\.gcode", m)]
                plate_num = _plate_number(sliced[0]) if len(sliced) == 1 else 1
            # pick_N.png is an object-ID mask, not a preview.
            for name in (
                f"Metadata/plate_{plate_num}.png",
                f"Metadata/top_{plate_num}.png",
                f"Metadata/plate_{plate_num}_small.png",
            ):
                if name in members:
                    data = zf.read(name)
                    logger.debug("Extracted thumbnail %s (%d bytes)", name, len(data))
                    return data
    except zipfile.BadZipFile:
        logger.debug("Invalid 3MF file (not a valid ZIP)")
    return None
