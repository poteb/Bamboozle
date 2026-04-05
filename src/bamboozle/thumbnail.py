from __future__ import annotations

import ftplib
import io
import logging
import re
import socket
import ssl
import zipfile

logger = logging.getLogger(__name__)


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


def _connect_ftp(ip: str, access_code: str, port: int = 990) -> ftplib.FTP_TLS:
    """Connect to printer's implicit FTPS on port 990."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    sock = socket.create_connection((ip, port), timeout=10)
    ssl_sock = ctx.wrap_socket(sock, server_hostname=ip)

    ftp = _ReusedSessionFTP(context=ctx)
    ftp.host = ip
    ftp.sock = ssl_sock
    ftp.af = socket.AF_INET
    ftp.file = ftp.sock.makefile("r", encoding="utf-8")
    ftp.getresp()
    ftp.login("bblp", access_code)
    ftp.prot_p()
    return ftp


def fetch_thumbnail(
    ip: str, access_code: str, gcode_file: str
) -> bytes | None:
    """Fetch the plate thumbnail for the current print job from the printer's FTP.

    The gcode_file from MQTT tells us:
    - For cloud prints: the 3MF filename (e.g. "0.2mm layer, 2 walls, 15% infill.3mf")
    - For SD prints: path like "/data/Metadata/plate_2.gcode"

    The 3MF file lives in /cache/ on the printer and contains plate thumbnails
    at Metadata/plate_N.png.
    """
    if not gcode_file:
        return None

    try:
        ftp = _connect_ftp(ip, access_code)
    except Exception as e:
        logger.debug("FTP connect failed for %s: %s", ip, e)
        return None

    try:
        # Determine plate number from gcode_file
        plate_num = _extract_plate_number(gcode_file)

        # Find the 3MF file in /cache/ (A1/P1/P1S store files here)
        try:
            ftp.cwd("/cache")
        except ftplib.error_perm:
            logger.debug("No /cache directory on %s — printer may not expose files via FTP", ip)
            return None
        files: list[str] = []
        ftp.retrlines("NLST", files.append)

        # The 3MF filename from MQTT might match directly, or we need to search
        threemf_name = _find_3mf(gcode_file, files)
        if not threemf_name:
            logger.debug("No 3MF found for '%s' on %s", gcode_file, ip)
            return None

        # Download the 3MF
        buf = io.BytesIO()
        ftp.retrbinary(f"RETR {threemf_name}", buf.write)
        buf.seek(0)

        # Extract the plate thumbnail
        return _extract_plate_thumbnail(buf, plate_num)

    except Exception as e:
        logger.debug("Thumbnail fetch failed for %s: %s", ip, e)
        return None
    finally:
        try:
            ftp.quit()
        except Exception:
            pass


def _extract_plate_number(gcode_file: str) -> int:
    """Extract plate number from gcode_file path."""
    # "/data/Metadata/plate_2.gcode" -> 2
    m = re.search(r"plate_(\d+)", gcode_file)
    if m:
        return int(m.group(1))
    return 1  # Default to plate 1


def _find_3mf(gcode_file: str, cache_files: list[str]) -> str | None:
    """Find the matching 3MF file in the cache directory."""
    threemf_files = [f for f in cache_files if f.endswith(".3mf")]

    # Direct match: gcode_file IS the 3MF name
    if gcode_file in threemf_files:
        return gcode_file

    # gcode_file might be just the name without .3mf
    for f in threemf_files:
        if gcode_file in f or f.replace(".3mf", "") in gcode_file:
            return f

    # For paths like "/data/Metadata/plate_2.gcode", the 3MF is harder to find.
    # Look for the most recently modified 3MF in the cache
    # (FTP LIST doesn't give us easy date sorting, so just return the last one)
    if threemf_files:
        return threemf_files[-1]

    return None


def _extract_plate_thumbnail(
    threemf_buf: io.BytesIO, plate_num: int
) -> bytes | None:
    """Extract plate thumbnail from a 3MF (ZIP) file."""
    try:
        with zipfile.ZipFile(threemf_buf) as zf:
            # Try plate_N.png first (larger), then plate_N_small.png
            candidates = [
                f"Metadata/plate_{plate_num}.png",
                f"Metadata/top_{plate_num}.png",
                f"Metadata/plate_{plate_num}_small.png",
                f"Metadata/pick_{plate_num}.png",
            ]
            for name in candidates:
                if name in zf.namelist():
                    data = zf.read(name)
                    logger.debug("Extracted thumbnail %s (%d bytes)", name, len(data))
                    return data

            # Fallback: any plate PNG
            for name in zf.namelist():
                if name.startswith("Metadata/plate_") and name.endswith(".png") and "_small" not in name:
                    data = zf.read(name)
                    logger.debug("Extracted fallback thumbnail %s (%d bytes)", name, len(data))
                    return data

    except zipfile.BadZipFile:
        logger.debug("Invalid 3MF file (not a valid ZIP)")
    return None
