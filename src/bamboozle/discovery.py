"""Passive LAN discovery of Bambu printers via their SSDP announcements.

Bambu printers broadcast SSDP-style ``NOTIFY`` packets to UDP port 2021.
Each packet carries the printer's current IP (``Location``) and serial
(``USN``). We only listen — no M-SEARCH is ever sent.
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import socket
import ssl
from dataclasses import dataclass
from typing import Callable

logger = logging.getLogger(__name__)

SSDP_PORT = 2021
MQTT_PORT = 8883

_SERIAL_RE = re.compile(r"^[A-Za-z0-9]{6,32}$")
_MAX_FIELD_LEN = 64  # DevName / DevModel are free text from the network


@dataclass(frozen=True)
class Announcement:
    ip: str
    serial: str
    model: str = ""
    name: str = ""


def parse_announcement(data: bytes) -> Announcement | None:
    """Parse a Bambu SSDP packet. Returns None if it isn't a usable announcement."""
    try:
        text = data.decode("utf-8", errors="replace")
    except Exception:
        return None
    headers: dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.partition(":")
        if sep:
            headers[key.strip().lower()] = value.strip()

    serial = headers.get("usn", "")
    location = headers.get("location", "")
    if not _SERIAL_RE.match(serial):
        return None
    try:
        addr = ipaddress.ip_address(location)
    except ValueError:
        return None
    if not addr.is_private:  # printers live on the LAN
        return None
    ip = str(addr)
    return Announcement(
        ip=ip,
        serial=serial,
        model=headers.get("devmodel.bambu.com", "")[:_MAX_FIELD_LEN],
        name=headers.get("devname.bambu.com", "")[:_MAX_FIELD_LEN],
    )


# --- Model codes -------------------------------------------------------------
#
# DevModel code -> (friendly name, camera port). C12, N2S, O1C2 and N7 are
# confirmed from live printers; the rest come from Bambu Studio profiles.
# Anything missing is treated as unknown.

_MODELS: dict[str, tuple[str, int]] = {
    "N1": ("A1 mini", 6000),
    "N2S": ("A1", 6000),
    "C11": ("P1P", 6000),
    "C12": ("P1S", 6000),
    "BL-P002": ("X1", 322),
    "BL-P001": ("X1C", 322),
    "C13": ("X1E", 322),
    "O1C2": ("H2C", 322),
    "O1D": ("H2D", 322),
    "N7": ("P2S", 322),
}


def model_info(code: str) -> tuple[str, int | None]:
    """Friendly model name and camera port for a DevModel code.

    Unknown codes return the raw code and no port.
    """
    return _MODELS.get(code, (code, None))


# --- TLS certificate check ------------------------------------------------
#
# Bambu printers present a self-signed-chain TLS cert on 8883 whose subject
# CN is the printer's serial (verified on A1, P1S, H2C and P2S). We read the
# CN with a tiny DER walker rather than pulling in a crypto dependency.

_OID_COMMON_NAME = b"\x55\x04\x03"  # 2.5.4.3
_STRING_DECODERS = {
    0x0C: "utf-8",      # UTF8String
    0x13: "ascii",      # PrintableString
    0x16: "ascii",      # IA5String
    0x14: "latin-1",    # TeletexString
    0x1E: "utf-16-be",  # BMPString
}


def _der_items(buf: bytes) -> list[tuple[int, bytes]]:
    """Split a DER byte string into a list of (tag, content) TLVs."""
    items: list[tuple[int, bytes]] = []
    i = 0
    while i < len(buf):
        if i + 2 > len(buf):
            raise ValueError("truncated TLV")
        tag = buf[i]
        length = buf[i + 1]
        i += 2
        if length & 0x80:
            n = length & 0x7F
            if n == 0 or n > 4 or i + n > len(buf):
                raise ValueError("bad length")
            length = int.from_bytes(buf[i:i + n], "big")
            i += n
        if i + length > len(buf):
            raise ValueError("truncated content")
        items.append((tag, buf[i:i + length]))
        i += length
    return items


def cert_common_name(der: bytes) -> str | None:
    """Return the subject CN of a DER-encoded X.509 certificate, or None."""
    try:
        (cert_tag, cert), = _der_items(der)
        if cert_tag != 0x30:
            return None
        tbs_tag, tbs = _der_items(cert)[0]
        if tbs_tag != 0x30:
            return None
        fields = _der_items(tbs)
        if fields and fields[0][0] == 0xA0:  # explicit [0] version
            fields = fields[1:]
        # serialNumber, signature, issuer, validity, subject
        subject_tag, subject = fields[4]
        if subject_tag != 0x30:
            return None
        for _set_tag, rdn in _der_items(subject):
            for _seq_tag, atv in _der_items(rdn):
                (oid_tag, oid), (val_tag, val) = _der_items(atv)[:2]
                if oid_tag == 0x06 and oid == _OID_COMMON_NAME:
                    encoding = _STRING_DECODERS.get(val_tag)
                    return val.decode(encoding) if encoding else None
    except (ValueError, IndexError, UnicodeDecodeError):
        return None
    return None


# --- Network probes (blocking; call via asyncio.to_thread) ----------------


def tcp_reachable(ip: str, port: int = MQTT_PORT, timeout: float = 3.0) -> bool:
    """True if a TCP connection to ip:port can be opened."""
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except OSError:
        return False


def fetch_cert_common_name(
    ip: str, port: int = MQTT_PORT, timeout: float = 5.0
) -> str | None:
    """Return the subject CN of the TLS cert presented at ip:port, or None."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # Bambu certs chain to a private CA
    try:
        with socket.create_connection((ip, port), timeout=timeout) as raw:
            with ctx.wrap_socket(raw) as tls:
                der = tls.getpeercert(binary_form=True)
    except (OSError, ssl.SSLError):
        return None
    return cert_common_name(der) if der else None


# --- Listener --------------------------------------------------------------


class _SsdpProtocol(asyncio.DatagramProtocol):
    def __init__(self, on_announcement: Callable[[Announcement], None]):
        self._on_announcement = on_announcement

    def datagram_received(self, data: bytes, addr) -> None:
        ann = parse_announcement(data)
        if ann is None:
            return
        if addr[0] != ann.ip:  # Location must be the sender itself
            logger.debug("Ignoring announcement for %s sent from %s", ann.ip, addr[0])
            return
        try:
            self._on_announcement(ann)
        except Exception as e:  # never let a handler kill the listener
            logger.debug("Announcement handler failed: %s", e)

    def error_received(self, exc: Exception) -> None:
        logger.debug("SSDP listener socket error: %s", exc)


class SsdpListener:
    """Passive listener for Bambu SSDP broadcasts on UDP 2021.

    Binds with SO_REUSEADDR so it can share the port with Bambu Studio.
    If the bind fails, logs a warning and stays disabled.
    """

    def __init__(
        self,
        on_announcement: Callable[[Announcement], None],
        port: int = SSDP_PORT,
        host: str = "",
    ):
        self._on_announcement = on_announcement
        self._port = port
        self._host = host
        self._transport: asyncio.DatagramTransport | None = None

    @property
    def port(self) -> int | None:
        """The bound port, or None when not listening."""
        if self._transport is None:
            return None
        return self._transport.get_extra_info("sockname")[1]

    async def start(self) -> bool:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((self._host, self._port))
            sock.setblocking(False)
            loop = asyncio.get_running_loop()
            self._transport, _ = await loop.create_datagram_endpoint(
                lambda: _SsdpProtocol(self._on_announcement), sock=sock
            )
        except OSError as e:
            sock.close()
            logger.warning(
                "Printer IP auto-detect disabled: cannot listen on UDP %d (%s)",
                self._port, e,
            )
            return False
        logger.info("Listening for printer announcements on UDP %d", self.port)
        return True

    def stop(self) -> None:
        if self._transport is not None:
            self._transport.close()
            self._transport = None
