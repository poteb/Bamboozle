"""Facts about the Bambu LAN MQTT protocol, as pure functions over report payloads.

`print_payload` is the `print` object of a device report (what
`Printer.mqtt_dump()["print"]` holds once a pushall has arrived).
"""
from __future__ import annotations

from typing import Any

# `fun` is a hex bitfield sent by newer firmware (H2*, P2S, X1 on the 2025
# authorization firmware); A1 / P1 don't send it. When this bit is SET,
# Developer Mode is off and the printer drops control commands from LAN
# clients such as us.
DEV_MODE_OFF_BIT = 0x20000000

# HMS raised when the printer rejects a command it couldn't verify
# ("MQTT command verification failed"). Status queries keep working.
COMMAND_VERIFY_FAILED_HMS = "0500050000010007"

COMMANDS_BLOCKED_MESSAGE = (
    "Printer is refusing control commands: enable LAN Only + Developer Mode "
    "on the printer, then restart it"
)

# CONNACK reason codes for a refused login (MQTT 3.1.1 return codes 4 and 5,
# as paho reports them through the v5 ReasonCode API).
_AUTH_REFUSAL_CODES = (134, 135)  # bad user name or password, not authorized
AUTH_REJECTED = "auth_rejected"
AUTH_REJECTED_MESSAGE = (
    "Access code wrong or changed (it changes whenever LAN Only or "
    "Developer Mode is toggled on the printer)"
)

# Why a session that logged in was dropped. The report topic is
# `device/<serial>/report` and is case-sensitive, so a session that never gets
# a single report usually means a wrong (or wrongly cased) serial.
NO_REPORTS = "no_reports"
STALE = "stale"  # reports stopped (P1-series brokers can go silent, socket up)


def _hex_or_int(value: Any) -> int | None:
    """Ints pass through; strings are read as hex. Anything else is None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return int(value.strip(), 16)
        except ValueError:
            return None
    return None


def developer_mode(print_payload: dict) -> bool | None:
    """True / False from `fun`; None when the printer doesn't report it."""
    fun = _hex_or_int(print_payload.get("fun"))
    if fun is None:
        return None
    return not fun & DEV_MODE_OFF_BIT


def hms_codes(print_payload: dict) -> set[str]:
    """Full 16-hex-digit HMS codes (`AAAAAAAACCCCCCCC`) currently raised."""
    entries = print_payload.get("hms")
    if not isinstance(entries, list):
        return set()
    codes = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        attr = _hex_or_int(entry.get("attr"))
        code = _hex_or_int(entry.get("code"))
        if attr is None or code is None:
            continue
        codes.add(f"{attr:08X}{code:08X}")
    return codes


def commands_blocked(print_payload: dict) -> bool:
    """True when the printer is known to refuse control commands.

    `fun` is the live state, so it wins where reported. The HMS only records
    that a command was refused and can outlive turning Developer Mode on;
    it's the only evidence on printers without `fun`.
    """
    dev_mode = developer_mode(print_payload)
    if dev_mode is not None:
        return not dev_mode
    return COMMAND_VERIFY_FAILED_HMS in hms_codes(print_payload)


def connack_failed(reason_code: Any) -> bool:
    """True when a CONNACK (paho ReasonCode, or its int value) refused us."""
    return getattr(reason_code, "is_failure", reason_code != 0)


def is_auth_refusal(reason_code: Any) -> bool:
    """True for a CONNACK refusing our credentials (ReasonCode, or 134 / 135)."""
    return getattr(reason_code, "value", reason_code) in _AUTH_REFUSAL_CODES


# Where a print file is stored, from the `url` echoed in a `project_file`
# report (sent when a print starts): `ftp://<name>` is external storage
# (SD card / USB drive), these are the internal eMMC (H2 series, P2S default).
_INTERNAL_URL_PREFIXES = ("brtc://emmc/", "file:///userdata/")
THUMBNAIL_INTERNAL_STORAGE = "internal_storage"


def file_on_internal_storage(print_payload: dict) -> bool:
    """True when the print file can't be on the FTPS share (port 990 serves
    external storage only): no SD card / USB drive is inserted, or the job
    was sent to internal storage.

    Reports are merged, so `url` is from the latest `project_file` seen.
    """
    if print_payload.get("sdcard") is False:
        return True
    url = print_payload.get("url")
    return isinstance(url, str) and url.lower().startswith(_INTERNAL_URL_PREFIXES)
