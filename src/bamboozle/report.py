"""Readable printer status, decoded from a raw device report.

Pure functions over `print_payload` (the report's `print` object, as in
`protocol.py`) and `info_payload` (the reply to `info.get_version`), so they
don't depend on the MQTT client library. `model` is the SSDP DevModel code
(`PrinterConfig.model`, see `discovery.model_info`); "" when not yet known.

Written from protocol documentation (field names, encodings and stage numbers
are protocol facts); the stage wording is our own.
"""
from __future__ import annotations

import math
from typing import Any

from .models import AmsUnit, Fans, Filament, Nozzle
from .protocol import hex_or_int

# DevModel codes. X1 family: the flat `chamber_temper` is a real sensor and the
# door is `home_flag` bit 23. H2 / P2S report the chamber under `device.ctc`.
_X1_FAMILY = frozenset({"BL-P001", "BL-P002", "C13"})
_H2_P2S = frozenset({"O1C", "O1C2", "O1D", "N7"})
_CHAMBER_SENSOR = _X1_FAMILY | _H2_P2S
# The A1 and A1 mini have neither an aux nor a chamber fan, yet echo the part
# fan's value in `big_fan1_speed` / `big_fan2_speed`.
_NO_AUX_CHAMBER_FANS = frozenset({"N1", "N2S"})

_DOOR_BIT = 1 << 23

# Above 16 bits a temperature is `target << 16 | current` (H2 series, while
# the heater has a target); otherwise it's plain degrees C.
_PACKED_TEMP_MIN = 0x10000

# `stg_cur`. -1 and 255 mean idle.
STAGE_NAMES: dict[int, str] = {
    0: "Printing",
    1: "Auto bed leveling",
    2: "Preheating bed",
    3: "Vibration compensation",
    4: "Changing filament",
    5: "Paused (M400)",
    6: "Paused: filament ran out",
    7: "Heating nozzle",
    8: "Calibrating extrusion",
    9: "Scanning bed surface",
    10: "Inspecting first layer",
    11: "Identifying build plate",
    12: "Calibrating micro lidar",
    13: "Homing toolhead",
    14: "Cleaning nozzle tip",
    15: "Checking extruder temp",
    16: "Paused by user",
    17: "Paused: front cover fell",
    18: "Calibrating lidar",
    19: "Calibrating flow",
    20: "Paused: nozzle temp fault",
    21: "Paused: bed temp fault",
    22: "Unloading filament",
    23: "Paused: skipped step",
    24: "Loading filament",
    25: "Calibrating motor noise",
    26: "Paused: AMS lost",
    27: "Paused: heatbreak fan slow",
    28: "Paused: chamber temp fault",
    29: "Cooling chamber",
    30: "Paused by G-code",
    31: "Demonstrating motor noise",
    32: "Paused: nozzle clumping",
    33: "Paused: cutter error",
    34: "Paused: first layer error",
    35: "Paused: nozzle clog",
    36: "Checking accuracy (before)",
    37: "Calibrating accuracy",
    38: "Checking accuracy (after)",
    39: "Calibrating nozzle offset",
    40: "Bed leveling (high temp)",
    41: "Checking quick release",
    42: "Checking door and cover",
    43: "Calibrating laser",
    44: "Checking platform",
    45: "Checking camera position",
    46: "Calibrating camera",
    47: "Bed leveling (phase 1)",
    48: "Bed leveling (phase 2)",
    49: "Heating chamber",
    50: "Cooling bed",
    51: "Printing calibration lines",
    52: "Checking material",
    53: "Calibrating live view camera",
    54: "Waiting for bed temp",
    55: "Checking material position",
    56: "Calibrating cutter offset",
    57: "Measuring surface",
    58: "Thermal preconditioning",
    59: "Homing blade holder",
    60: "Calibrating camera offset",
    61: "Calibrating blade holder",
    62: "Testing hotend pick-up",
    63: "Waiting for chamber temp",
    64: "Preparing hotend",
    65: "Calibrating clump detection",
    66: "Purifying chamber air",
}
_IDLE_STAGES = frozenset({-1, 255})
_JOB_STATES = frozenset({"RUNNING", "PAUSE"})
_JOB_ENDED = frozenset({"FINISH", "FAILED"})

_SPEED_NAMES = {1: "Silent", 2: "Standard", 3: "Sport", 4: "Ludicrous"}

_NOZZLE_TYPE_NAMES = {
    "stainless_steel": "Stainless steel",
    "hardened_steel": "Hardened steel",
    "tungsten_carbide": "Tungsten carbide",
    "brass": "Brass",
}


# --- Number parsing ---------------------------------------------------------

def _num(value: Any) -> float | None:
    """Finite ints, floats and numeric strings as float; anything else None."""
    if isinstance(value, bool):
        return None
    try:
        if isinstance(value, (int, float)):
            n = float(value)
        elif isinstance(value, str):
            n = float(value.strip())
        else:
            return None
    except (ValueError, OverflowError):
        return None
    return n if math.isfinite(n) else None


def _int(value: Any) -> int | None:
    n = _num(value)
    return int(n) if n is not None and n.is_integer() else None


def _get(d: Any, *path: str) -> Any:
    for key in path:
        if not isinstance(d, dict):
            return None
        d = d.get(key)
    return d


def _list(value: Any) -> list[dict]:
    return [x for x in value if isinstance(x, dict)] if isinstance(value, list) else []


# --- Temperatures -------------------------------------------------------------

def split_temp(value: Any) -> tuple[float | None, float | None]:
    """(current, target) from a temperature field. Target is None unless the
    value carries one (the H2 `target << 16 | current` packing)."""
    n = _num(value)
    if n is None:
        return None, None
    if n >= _PACKED_TEMP_MIN and n.is_integer():
        packed = int(n)
        return float(packed & 0xFFFF), float(packed >> 16)
    return n, None


def _flat_temp(print_payload: dict, temp_key: str, target_key: str) -> tuple[float | None, float | None]:
    current, packed_target = split_temp(print_payload.get(temp_key))
    target = _num(print_payload.get(target_key))
    return current, target if target is not None else packed_target


def nozzle_type_name(raw: Any, diameter: float | None = None) -> str:
    """Display name for `nozzle_type`: known materials spelled out, codes (H2 /
    P2S, e.g. "HS01") as sent, minus a "-<diameter>" suffix."""
    if not isinstance(raw, str) or not raw:
        return ""
    if raw in _NOZZLE_TYPE_NAMES:
        return _NOZZLE_TYPE_NAMES[raw]
    if diameter is not None and "-" in raw:
        code, suffix = raw.rsplit("-", 1)
        if _num(suffix) == diameter:
            return code
    return raw


def nozzles(print_payload: dict) -> list[Nozzle]:
    """Every nozzle; two (left, then right) on dual-nozzle printers.

    On H2C / H2D the flat `nozzle_temper` mirrors one nozzle only, so both
    come from `device.extruder.info` (id 0 = right, 1 = left), with type and
    diameter from the `device.nozzle.info` entry mounted on each.
    """
    extruders = _list(_get(print_payload, "device", "extruder", "info"))
    diameter = _num(print_payload.get("nozzle_diameter"))
    if len(extruders) >= 2:
        mounted = {_int(n.get("id")): n for n in _list(_get(print_payload, "device", "nozzle", "info"))}
        sides = {0: "right", 1: "left"}
        out = []
        for ext in extruders:
            ext_id = _int(ext.get("id"))
            if ext_id not in sides:
                continue
            nozzle = mounted.get(_int(ext.get("hnow"))) or mounted.get(ext_id) or {}
            temp, target = split_temp(ext.get("temp"))
            d = _num(nozzle.get("diameter"))
            out.append(Nozzle(
                side=sides[ext_id], temp=temp, target=target,
                type=nozzle_type_name(nozzle.get("type"), d), diameter=d,
            ))
        return sorted(out, key=lambda n: n.side != "left")

    temp, target = _flat_temp(print_payload, "nozzle_temper", "nozzle_target_temper")
    if temp is None and extruders:
        temp, target = split_temp(extruders[0].get("temp"))
    nozzle_type = nozzle_type_name(print_payload.get("nozzle_type"), diameter)
    if temp is None and not nozzle_type and diameter is None:
        return []
    return [Nozzle(temp=temp, target=target, type=nozzle_type, diameter=diameter)]


def bed(print_payload: dict) -> tuple[float | None, float | None]:
    """(current, target) of the heated bed."""
    temp, target = _flat_temp(print_payload, "bed_temper", "bed_target_temper")
    if temp is None:
        temp, target = split_temp(_get(print_payload, "device", "bed", "info", "temp"))
    return temp, target


def chamber(print_payload: dict, model: str) -> tuple[float | None, float | None]:
    """(current, target) of the chamber, or (None, None) on printers without a
    sensor: the A1 / P1 send a meaningless `chamber_temper` (5)."""
    ctc = _get(print_payload, "device", "ctc", "info", "temp")
    if ctc is not None:
        return split_temp(ctc)
    if model not in _CHAMBER_SENSOR:
        return None, None
    temp, target = split_temp(print_payload.get("chamber_temper"))
    explicit = _num(print_payload.get("mc_target_cham"))
    return temp, explicit if explicit is not None else target


# --- Stage --------------------------------------------------------------------

def stage(print_payload: dict) -> int | None:
    """`stg_cur`, or None when idle or the job has ended (a leftover stage
    isn't current). The A1 / P1 report 0 ("printing") while idle, so 0 only
    counts during a job."""
    n = _int(print_payload.get("stg_cur"))
    if n is None or n in _IDLE_STAGES or print_payload.get("gcode_state") in _JOB_ENDED:
        return None
    if n == 0 and print_payload.get("gcode_state") not in _JOB_STATES:
        return None
    return n


def stage_name(stage_number: int | None, gcode_state: str) -> str:
    """Readable stage. One newer than our table reads "Preparing" (most stages
    are start-of-print steps), except while paused."""
    if stage_number is None:
        return ""
    name = STAGE_NAMES.get(stage_number)
    if name:
        return name
    return "" if gcode_state == "PAUSE" else "Preparing"


# --- Fans -----------------------------------------------------------------------

# Fan speeds are reported on a 0-15 scale; 1.5 units per 10 % step.
_FAN_UNITS_PER_STEP = 1.5


def fan_percent(value: Any) -> int | None:
    """Fan speed from the report's 0-15 scale, in 10 % steps."""
    n = _int(value)
    if n is None:
        return None
    return min(100, max(0, round(n / _FAN_UNITS_PER_STEP) * 10))


def fans(print_payload: dict, model: str) -> Fans:
    no_extra = model in _NO_AUX_CHAMBER_FANS
    return Fans(
        part=fan_percent(print_payload.get("cooling_fan_speed")),
        aux=None if no_extra else fan_percent(print_payload.get("big_fan1_speed")),
        chamber=None if no_extra else fan_percent(print_payload.get("big_fan2_speed")),
        heatbreak=fan_percent(print_payload.get("heatbreak_fan_speed")),
    )


# --- AMS ------------------------------------------------------------------------

def ams_name(index: int) -> str:
    """Units 0-3 are AMS / AMS Lite / AMS 2 Pro; 128 and up are AMS HT."""
    return f"AMS HT {index - 127}" if index >= 128 else f"AMS {index + 1}"


def ams_units(print_payload: dict) -> list[AmsUnit]:
    """Humidity is `humidity_raw` (% RH). `humidity` is a 1-5 index that runs
    the other way (5 = dry), so it's never shown. 0 % and 0.0 degrees mean no
    sensor (AMS Lite)."""
    out = []
    for pos, unit in enumerate(_list(_get(print_payload, "ams", "ams"))):
        index = _int(unit.get("id"))
        index = pos if index is None else index
        humidity = _int(unit.get("humidity_raw"))
        temp = _num(unit.get("temp"))
        out.append(AmsUnit(
            index=index, name=ams_name(index),
            humidity=humidity if humidity and humidity > 0 else None,
            temp=temp if temp else None,
        ))
    return out


# --- Misc -------------------------------------------------------------------------

def wifi_dbm(print_payload: dict) -> int | None:
    """Signal strength from `wifi_signal` (e.g. "-53dBm")."""
    value = print_payload.get("wifi_signal")
    if isinstance(value, str):
        value = value.strip().lower().removesuffix("dbm")
    return _int(value)


def door_open(print_payload: dict, model: str) -> bool | None:
    """Bit 23 of `home_flag` on the X1 family, of `stat` (a hex string) on
    H2 / P2S. None for other or unknown models: the A1 / P1 have no door
    sensor, and the bit means something else there."""
    if model in _X1_FAMILY:
        flags = _int(print_payload.get("home_flag"))
    elif model in _H2_P2S:
        flags = hex_or_int(print_payload.get("stat"))
    else:
        return None
    if flags is None:
        return None
    return bool(flags & _DOOR_BIT)


def sdcard(print_payload: dict) -> bool | None:
    value = print_payload.get("sdcard")
    return value if isinstance(value, bool) else None


def firmware(info_payload: dict | None) -> str:
    """Printer firmware: `sw_ver` of the "ota" module in a get_version reply."""
    for module in _list(_get(info_payload, "module")):
        if module.get("name") == "ota" and isinstance(module.get("sw_ver"), str):
            return module["sw_ver"]
    return ""


def speed_name(level: Any) -> str:
    return _SPEED_NAMES.get(_int(level), "")


# --- The job ----------------------------------------------------------------------

# `gcode_state` values shown as they are; anything else reads UNKNOWN.
GCODE_STATES = frozenset({"IDLE", "PREPARE", "RUNNING", "PAUSE", "FINISH", "FAILED"})


def gcode_state(print_payload: dict) -> str:
    value = print_payload.get("gcode_state")
    return value if value in GCODE_STATES else "UNKNOWN"


def _count(value: Any) -> int:
    """A whole number from an int, float or numeric string; else 0."""
    n = _num(value)
    return int(n) if n is not None else 0


def light_mode(print_payload: dict) -> str:
    """The chamber light's mode ("on", "off", "flashing") from `lights_report`:
    the `chamber_light` node, else the first node listed; "" when unknown."""
    nodes = _list(print_payload.get("lights_report"))
    node = next((n for n in nodes if n.get("node") == "chamber_light"), nodes[0] if nodes else None)
    mode = node.get("mode") if node is not None else None
    return mode if isinstance(mode, str) else ""


def light_on(print_payload: dict) -> bool:
    return light_mode(print_payload) == "on"


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def job_fields(print_payload: dict) -> dict[str, Any]:
    """Job progress and the files it's printing (`gcode_file`, `subtask_name`)."""
    return {
        "gcode_state": gcode_state(print_payload),
        "progress": _count(print_payload.get("mc_percent")),
        "remaining_minutes": _count(print_payload.get("mc_remaining_time")),
        "current_layer": _count(print_payload.get("layer_num")),
        "total_layers": _count(print_payload.get("total_layer_num")),
        "print_speed": _count(print_payload.get("spd_mag")),
        "light_on": light_on(print_payload),
        "gcode_file": _text(print_payload.get("gcode_file")),
        "subtask_name": _text(print_payload.get("subtask_name")),
    }


# --- Filaments ----------------------------------------------------------------------

def tray_color(raw: Any) -> str:
    """A Bambu `tray_color` ("RRGGBB" or "RRGGBBAA", alpha last) as a CSS
    colour ("#RRGGBBAA"); "" when unusable. All zeros is the firmware's value
    for an empty / unidentified slot, so it's "" too."""
    if not isinstance(raw, str):
        return ""
    s = raw.strip().lstrip("#")
    if len(s) not in (6, 8):
        return ""
    try:
        value = int(s, 16)
    except ValueError:
        return ""
    return f"#{s.upper()}" if value else ""


def _filament(tray: Any, source: str, ams_index: int, tray_index: int) -> Filament | None:
    """None for an empty slot (no colour, type or name)."""
    if not isinstance(tray, dict):
        return None
    color = tray_color(tray.get("tray_color"))
    ftype = tray.get("tray_type") or ""
    fname = tray.get("tray_sub_brands") or tray.get("tray_id_name") or ""
    if not color and not ftype and not fname:
        return None
    return Filament(source=source, ams_index=ams_index, tray_index=tray_index,
                    color=color, filament_type=str(ftype), name=str(fname))


def _index(value: Any, default: int) -> int:
    n = _int(value)
    return default if n is None else n


def filaments(print_payload: dict) -> list[Filament]:
    """Loaded spools: AMS trays, then the external spool(s): `vir_slot` (H2,
    one per extruder) when it's a non-empty list, else `vt_tray`."""
    out: list[Filament] = []
    ams = print_payload.get("ams")
    if isinstance(ams, dict) and hex_or_int(ams.get("ams_exist_bits")):
        for unit_pos, unit in enumerate(_list(ams.get("ams"))):
            unit_index = _index(unit.get("id"), unit_pos)
            for slot_pos, tray in enumerate(_list(unit.get("tray"))):
                f = _filament(tray, "ams", unit_index, _index(tray.get("id"), slot_pos))
                if f is not None:
                    out.append(f)
    vir_slot = print_payload.get("vir_slot")
    if isinstance(vir_slot, list) and vir_slot:
        externals = list(enumerate(vir_slot))
    else:
        vt = print_payload.get("vt_tray")
        externals = [(0, vt)] if isinstance(vt, dict) and vt else []
    for pos, slot in externals:
        f = _filament(slot, "external", pos, 0)
        if f is not None:
            out.append(f)
    return out


# --- Everything ---------------------------------------------------------------------

def status_fields(print_payload: dict, info_payload: dict | None, model: str) -> dict[str, Any]:
    """The decoded `PrinterState` fields."""
    gcode_state = print_payload.get("gcode_state")
    stage_number = stage(print_payload)
    nozzle_temp, nozzle_target = _flat_temp(print_payload, "nozzle_temper", "nozzle_target_temper")
    bed_temp, bed_target = bed(print_payload)
    chamber_temp, chamber_target = chamber(print_payload, model)
    level = _int(print_payload.get("spd_lvl"))
    return {
        "nozzle_temp": nozzle_temp,
        "nozzle_target": nozzle_target,
        "nozzles": nozzles(print_payload),
        "bed_temp": bed_temp,
        "bed_target": bed_target,
        "chamber_temp": chamber_temp,
        "chamber_target": chamber_target,
        "stage": stage_number,
        "stage_name": stage_name(stage_number, gcode_state if isinstance(gcode_state, str) else ""),
        "speed_level": level,
        "speed_name": speed_name(level),
        "fans": fans(print_payload, model),
        "ams_units": ams_units(print_payload),
        "wifi_dbm": wifi_dbm(print_payload),
        "door_open": door_open(print_payload, model),
        "sdcard": sdcard(print_payload),
        "firmware": firmware(info_payload),
    }
