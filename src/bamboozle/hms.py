"""HMS messages and print errors, decoded from a raw device report.

Pure functions over `print_payload` (the report's `print` object, as in
`protocol.py` and `report.py`).

Only the code structure is decoded here (severity, module, unit): those are
protocol facts. The message text is not bundled. Bambu's wiki is "all rights
reserved", Bambu Studio's HMS tables are AGPL, and ha-bambulab's (MIT) are
downloaded from Bambu's own catalogue, so each code links to Bambu's wiki.

- `hms[]` entries: `attr` is module, unit, part, part number (a byte each);
  `code` is the level (high 16 bits) then the error (low 16). Shown as
  `AAAA_AAAA_CCCC_CCCC`, like the printer screen.
- `print_error`: 32 bits, module + unit (high 16) then the error (low 16),
  shown as `MMMM_EEEE`. The error's top two bits are the level: 4xxx stops the
  task, 8xxx pauses it, Cxxx is a prompt; below 0x4000 is a status value.
"""
from __future__ import annotations

from typing import Any

from .models import PrinterError
from .protocol import hms_codes
from .report import ams_name

# HMS level (high half of `code`); 0 is invalid.
_HMS_LEVELS = {1: "fatal", 2: "serious", 3: "common", 4: "info"}
# print_error level (top two bits of the error half).
_PRINT_ERROR_LEVELS = {1: "fatal", 2: "serious", 3: "common"}
_SEVERITY_ORDER = ("fatal", "serious", "common", "info", "")

_PRINT_ERROR_MIN = 0x4000

# Module byte. AMS modules carry the unit in the next byte.
_MODULE_NAMES = {
    0x03: "Motion controller",
    0x05: "System",  # mainboard: also maintenance reminders, LAN commands
    0x08: "Toolhead",
    0x0C: "Camera / lidar",
}
_MODULE_AMS = 0x07
_MODULE_AMS_HT = 0x18
_UNIT_EXTERNAL = 0xFF
_AMS_HT_FIRST_INDEX = 128  # AMS HT units are numbered from 128 (report.ams_name)

# A user cancel echoes these as errors (in print_error and in hms[]).
_USER_CANCEL = frozenset({"0300_400C", "0500_400E"})

# Codes we understand well enough to describe in our own words.
_REFUSED_COMMAND = "0500050000010007"
_DESCRIPTIONS = {
    _REFUSED_COMMAND: "A LAN command was refused earlier: it couldn't be verified "
                      "(Developer Mode allows them). It stays listed; not a fault.",
}
# Records of something that happened, never why a job is paused: the user's
# H2C and P2S carry the refused-command code while the light works.
_RECORD_ONLY = frozenset({_REFUSED_COMMAND})

# A job in these states stopped for a reason, which its errors explain.
_STOPPED_JOB = frozenset({"PAUSE", "FAILED"})

HMS_URL = "https://e.bambulab.com/index.php?e={code}&s=device_hms&lang=en"
PRINT_ERROR_URL = "https://wiki.bambulab.com/en/hms/error-code#:~:text={module:04X}-{error:04X}"


def module_name(module: int, unit: int) -> str:
    """Readable module from its byte and the unit byte after it; "" if unknown."""
    if module == _MODULE_AMS:
        if unit == _UNIT_EXTERNAL:
            return "External spool"
        return ams_name(unit) if unit < 4 else "AMS"
    if module == _MODULE_AMS_HT:
        return ams_name(_AMS_HT_FIRST_INDEX + (unit & 0x7F))
    return _MODULE_NAMES.get(module, "")


def _short_code(module16: int, error: int) -> str:
    return f"{module16:04X}_{error:04X}"


def hms_errors(print_payload: dict) -> list[PrinterError]:
    """Every HMS message raised (not yet ordered or marked active)."""
    out = []
    for full in sorted(hms_codes(print_payload)):
        attr, code = int(full[:8], 16), int(full[8:], 16)
        level = code >> 16
        if level == 0 or _short_code(attr >> 16, code & 0xFFFF) in _USER_CANCEL:
            continue
        out.append(PrinterError(
            kind="hms",
            code="_".join(full[i:i + 4] for i in range(0, 16, 4)),
            severity=_HMS_LEVELS.get(level, ""),
            module=module_name(attr >> 24, (attr >> 16) & 0xFF),
            description=_DESCRIPTIONS.get(full, ""),
            url=HMS_URL.format(code=full),
        ))
    return out


def _print_error_value(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def print_error(print_payload: dict) -> PrinterError | None:
    """The print error, or None when there's none (or it's a cancel echo)."""
    value = _print_error_value(print_payload.get("print_error"))
    if not value:
        return None
    module16, error = (value >> 16) & 0xFFFF, value & 0xFFFF
    if error < _PRINT_ERROR_MIN or _short_code(module16, error) in _USER_CANCEL:
        return None
    return PrinterError(
        kind="print_error",
        code=_short_code(module16, error),
        severity=_PRINT_ERROR_LEVELS.get(error >> 14, ""),
        module=module_name(module16 >> 8, module16 & 0xFF),
        url=PRINT_ERROR_URL.format(module=module16, error=error),
    )


def printer_errors(print_payload: dict) -> list[PrinterError]:
    """HMS messages and the print error: active ones first, then by severity.

    They're active while the job is paused or failed (they say why), except
    records like the refused-command code. Otherwise they're lingering: the
    printer keeps HMS messages until they're acknowledged on it.
    """
    errors = hms_errors(print_payload)
    pe = print_error(print_payload)
    # The same fault is often raised as both; the HMS form says more.
    if pe and not any(_hms_short(e.code) == pe.code for e in errors):
        errors.append(pe)
    stopped = print_payload.get("gcode_state") in _STOPPED_JOB
    for e in errors:
        e.active = stopped and e.code.replace("_", "") not in _RECORD_ONLY
    return sorted(errors, key=lambda e: (not e.active, _SEVERITY_ORDER.index(e.severity)))


def _hms_short(code: str) -> str:
    """"0300_0300_0002_8003" -> "0300_8003" (module + unit, error)."""
    return f"{code[:4]}_{code[-4:]}"
