"""Facts about the Bambu LAN MQTT protocol, as pure functions over report payloads.

`print_payload` is the `print` object of a device report (what
`Printer.mqtt_dump()["print"]` holds once a pushall has arrived).
"""
from __future__ import annotations

from typing import Any
from urllib.parse import unquote

# `fun` is a hex bitfield sent by newer firmware (H2*, P2S, X1 on the 2025
# authorization firmware); A1 / P1 don't send it. When this bit is SET,
# Developer Mode is off. That does NOT mean every command is dropped: the
# user's H2C and P2S report it, yet the chamber light toggles from us work.
# Which commands the firmware gates is unconfirmed, so it's only a hint
# (print controls may be ignored) and never fails a command.
DEV_MODE_OFF_BIT = 0x20000000

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
    """True / False from `fun`; None when the printer doesn't report it.

    HMS 0500050000010007 ("MQTT command verification failed") isn't used as
    a fallback: it records a past refusal, not the current state, and the
    user's H2C / P2S carry it while the light works.
    """
    fun = _hex_or_int(print_payload.get("fun"))
    if fun is None:
        return None
    return not fun & DEV_MODE_OFF_BIT


def hms_codes(print_payload: dict) -> set[str]:
    """Full 16-hex-digit HMS codes (`AAAAAAAACCCCCCCC`) currently raised.

    No caller yet: kept for the queued HMS decoding task.
    """
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

    Reports are merged, so `url` is from the latest `project_file` seen; it
    only counts when it names the current job (`subtask_name`).
    """
    if print_payload.get("sdcard") is False:
        return True
    url = print_payload.get("url")
    subtask = print_payload.get("subtask_name")
    if not isinstance(url, str) or not isinstance(subtask, str) or not subtask:
        return False
    url = unquote(url)
    if not url.lower().startswith(_INTERNAL_URL_PREFIXES):
        return False
    name = _strip_print_ext(url.rsplit("/", 1)[-1])
    subtask = _strip_print_ext(subtask)
    return name in (subtask, subtask.replace(" ", "_"))


def _strip_print_ext(name: str) -> str:
    for ext in (".gcode.3mf", ".3mf", ".gcode"):
        if name.lower().endswith(ext):
            return name[: -len(ext)]
    return name
