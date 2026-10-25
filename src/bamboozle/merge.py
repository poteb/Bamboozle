"""The printer's state, merged from its report stream.

Pure (no I/O, no threads): `MergedReport.apply(payload)` takes one decoded
message from `device/<serial>/report` and folds it into `print` (the status
the decoders in report.py / hms.py / protocol.py read) and `info` (the
get_version reply).

Printers don't always send their whole state. The A1 / P1 series send deltas
(`msg` 1: only what changed) and a full report (`msg` 0) only when asked; a
P1S AMS delta is just the changed unit, an A1 one can be just the
`tray_exist_bits`. So status frames are deep-merged:

- dicts merge key by key; a key a frame doesn't carry keeps its value;
- scalars and null replace;
- lists replace in a full report (it's authoritative: units removed, HMS
  cleared). In a delta, a list of dicts that all have an `id` (AMS units,
  trays, `vir_slot`, `device.extruder.info`) or all have a `node`
  (`lights_report`) merges item by item; other lists replace;
- a delta tray holding only `id` / `state` is an empty slot and replaces the
  old tray (a merge would keep the removed spool);
- `tray_exist_bits` / `ams_exist_bits`, when a frame carries them, clear the
  spools / drop the units of AMS 0-3 whose bit is 0 (4 bits per unit). A later
  tray update is trusted (the A1 sends the bits before the unit). AMS HT
  (unit 128+) is left to full reports: its bit layout is unconfirmed;
- `vir_slot` and `vt_tray` are both kept; the filament reader prefers
  `vir_slot` (H2 external spools), so a `vt_tray` never overrides it.

Only status frames (`push_status`, or a `print` without a command) are
telemetry. A `project_file` echo contributes its `url` only (its `job_id` is
an int where status has a string). Other commands' echoes (ours and other
clients') aren't merged; `apply` returns them for command verification.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any

from .protocol import hex_or_int

# Deeper structures replace instead of merging (bounds the recursion).
MAX_MERGE_DEPTH = 32
# AMS units 0-3 have 4 slots each in tray_exist_bits / one bit in ams_exist_bits.
_BITS_UNITS = range(4)
_SLOTS_PER_UNIT = 4
_EMPTY_TRAY_KEYS = frozenset({"id", "state"})


@dataclass(frozen=True)
class Frame:
    """What one report message was."""
    status: bool = False  # telemetry, merged into `print`
    full: bool = False  # a full status report (`msg` 0)
    version: bool = False  # a get_version reply
    # A command echo (`print` / `system` frame that isn't status): the
    # command, its sequence_id and the printer's verdict, when given.
    command: str = ""
    sequence_id: str = ""
    result: str = ""
    reason: str = ""


class MergedReport:
    def __init__(self) -> None:
        self.print: dict = {}
        self.info: dict | None = None

    def reset(self) -> None:
        self.print = {}
        self.info = None

    def apply(self, payload: Any) -> Frame:
        if not isinstance(payload, dict):
            return Frame()
        info = payload.get("info")
        if isinstance(info, dict) and info.get("command") == "get_version":
            self.info = copy.deepcopy(info)
            return Frame(version=True)
        body = payload.get("print")
        if isinstance(body, dict):
            command = body.get("command")
            if command is None or command == "push_status":
                full = body.get("msg") == 0
                self._merge_status(body, full)
                return Frame(status=True, full=full)
            if command == "project_file" and isinstance(body.get("url"), str):
                self.print["url"] = body["url"]
            return _echo(body)
        system = payload.get("system")
        if isinstance(system, dict):
            return _echo(system)
        return Frame()

    def _merge_status(self, body: dict, full: bool) -> None:
        _merge_dict(self.print, body, full, 0)
        ams = body.get("ams")
        if isinstance(ams, dict) and isinstance(self.print.get("ams"), dict):
            _apply_exist_bits(self.print["ams"], ams)


def _echo(body: dict) -> Frame:
    def text(key: str) -> str:
        value = body.get(key)
        return str(value) if isinstance(value, (str, int)) and not isinstance(value, bool) else ""
    return Frame(command=text("command"), sequence_id=text("sequence_id"),
                 result=text("result").lower(), reason=text("reason"))


def _merge_dict(dst: dict, src: dict, full: bool, depth: int) -> None:
    for key, value in src.items():
        old = dst.get(key)
        if depth < MAX_MERGE_DEPTH and isinstance(value, dict) and isinstance(old, dict):
            _merge_dict(old, value, full, depth + 1)
        elif depth < MAX_MERGE_DEPTH and not full and isinstance(value, list) and isinstance(old, list):
            dst[key] = _merge_list(old, value, depth + 1, tray=key == "tray")
        else:
            dst[key] = copy.deepcopy(value)


def _list_key(items: list) -> str | None:
    """The field a delta list's items are matched on, if it has one."""
    if not items or not all(isinstance(i, dict) for i in items):
        return None
    for key in ("id", "node"):
        if all(isinstance(i.get(key), (str, int)) and not isinstance(i.get(key), bool)
               for i in items):
            return key
    return None


def _merge_list(old: list, new: list, depth: int, tray: bool) -> list:
    key = _list_key(new)
    if key is None or _list_key(old) != key:
        return copy.deepcopy(new)
    merged = list(old)
    index = {str(item[key]): pos for pos, item in enumerate(merged)}
    for item in new:
        pos = index.get(str(item[key]))
        if pos is None:
            index[str(item[key])] = len(merged)
            merged.append(copy.deepcopy(item))
        elif tray and set(item) <= _EMPTY_TRAY_KEYS:
            merged[pos] = copy.deepcopy(item)  # the slot is empty now
        else:
            _merge_dict(merged[pos], item, False, depth)
    return merged


def _unit_id(unit: Any) -> int | None:
    if not isinstance(unit, dict):
        return None
    uid = unit.get("id")
    if isinstance(uid, bool):
        return None
    try:
        return int(uid)
    except (TypeError, ValueError):
        return None


def _apply_exist_bits(state_ams: dict, frame_ams: dict) -> None:
    """Honour the presence bits a frame carries (see the module docstring)."""
    units = state_ams.get("ams")
    if not isinstance(units, list):
        return
    unit_bits = hex_or_int(frame_ams.get("ams_exist_bits")) if "ams_exist_bits" in frame_ams else None
    if unit_bits is not None:
        units[:] = [u for u in units
                    if _unit_id(u) not in _BITS_UNITS or unit_bits >> _unit_id(u) & 1]
    tray_bits = hex_or_int(frame_ams.get("tray_exist_bits")) if "tray_exist_bits" in frame_ams else None
    if tray_bits is None:
        return
    for unit in units:
        uid = _unit_id(unit)
        trays = unit.get("tray") if isinstance(unit, dict) else None
        if uid not in _BITS_UNITS or not isinstance(trays, list):
            continue
        for pos, tray in enumerate(trays):
            slot = _unit_id(tray)
            if slot is None or not 0 <= slot < _SLOTS_PER_UNIT:
                continue
            if not tray_bits >> (uid * _SLOTS_PER_UNIT + slot) & 1:
                trays[pos] = {"id": tray["id"]}
