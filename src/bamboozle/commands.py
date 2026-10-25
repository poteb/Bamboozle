"""The dashboard's printer commands: what to publish, and how to tell it worked.

No I/O (only the shared `sequence_id` counter advances). `build(action, params)` validates the (untrusted, from the browser)
action and params, and returns the requests plus a `confirmed(print, frame)`
rule over the merged report: a command counts as done when the printer's
state shows it, not when the publish went out.

Payloads (protocol facts; OpenBambuAPI `mqtt.md`, and Bambu Studio's
DeviceManager builds the same objects for every model, with no model
branch): `pause` / `resume` / `stop` are `print` commands with an empty
`param`; `print_speed` carries the level (1 silent, 2 standard, 3 sport,
4 ludicrous) as a decimal string; all with an incrementing string
`sequence_id`, published with QoS 1 (see mqtt.py). The light is `ledctrl`
to both chamber LED nodes, Refresh a standalone `pushall`.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from . import mqtt
from .merge import Frame
from .report import gcode_state, light_mode

SPEED_LEVELS = range(1, 5)
LIGHT_MODES = frozenset({"on", "off"})
_STOPPED = frozenset({"FINISH", "FAILED", "IDLE"})


class InvalidCommand(ValueError):
    """An unknown action or a bad parameter; nothing was sent."""


@dataclass(frozen=True)
class Command:
    action: str
    requests: list[dict]
    # True once the merged `print` state (and the frame just merged; None when
    # checked outside a frame) shows the command took effect.
    confirmed: Callable[[dict, Frame | None], bool]
    label: str  # "Pause", for "Pause sent, but not confirmed ..."
    done: str  # "Print paused"
    print_control: bool  # one the firmware may ignore with Developer Mode off
    # For "... the printer didn't report <waiting_for> within 10 s".
    waiting_for: str = "the change"
    # Ask for a full report halfway through an unconfirmed wait (an A1 / P1S
    # delta may not carry the changed field). Pointless for Refresh itself.
    pushall_midway: bool = True

    @property
    def echo(self) -> tuple[str, str]:
        """(command, sequence_id) of the printer's echo of the first request:
        the one a refusal is judged on (for the light, the chamber_light one;
        printers without chamber_light2 may refuse that)."""
        (body,) = self.requests[0].values()
        return str(body.get("command", "")), str(body.get("sequence_id", ""))


def _state_is(*states: str) -> Callable[[dict, Frame | None], bool]:
    return lambda print_state, frame: gcode_state(print_state) in states


def _light_is(mode: str) -> Callable[[dict, Frame | None], bool]:
    return lambda print_state, frame: light_mode(print_state) == mode


def _speed_is(level: int) -> Callable[[dict, Frame | None], bool]:
    def confirmed(print_state: dict, frame: Frame | None) -> bool:
        value = print_state.get("spd_lvl")
        return not isinstance(value, bool) and str(value) == str(level)
    return confirmed


def _full_report(print_state: dict, frame: Frame | None) -> bool:
    return frame is not None and frame.full


def print_request(command: str, param: str = "") -> dict:
    return {"print": {"sequence_id": mqtt.next_sequence_id(), "command": command, "param": param}}


def _speed_level(params: dict) -> int:
    level = params.get("level")
    if isinstance(level, bool):
        raise InvalidCommand("Invalid speed level")
    if isinstance(level, str) and level.strip().isdigit():
        level = int(level)
    if not isinstance(level, int) or level not in SPEED_LEVELS:
        raise InvalidCommand("Invalid speed level (1 silent, 2 standard, 3 sport, 4 ludicrous)")
    return level


def _light(mode: str) -> Command:
    return Command(f"light_{mode}", mqtt.light_requests(mode), _light_is(mode),
                   f"Light {mode}", f"Light turned {mode}", print_control=False)


def build(action: str, params: Any = None) -> Command:
    """The command for a dashboard action (the same payloads for every model)."""
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise InvalidCommand("Invalid parameters")
    match action:
        case "pause":
            return Command(action, [print_request("pause")], _state_is("PAUSE"),
                           "Pause", "Print paused", print_control=True)
        case "resume":
            return Command(action, [print_request("resume")], _state_is("RUNNING", "PREPARE"),
                           "Resume", "Print resumed", print_control=True)
        case "stop":
            return Command(action, [print_request("stop")], _state_is(*_STOPPED),
                           "Stop", "Print stopped", print_control=True)
        case "set_speed":
            level = _speed_level(params)
            return Command(action, [print_request("print_speed", str(level))], _speed_is(level),
                           f"Speed level {level}", f"Speed set to level {level}", print_control=True)
        case "light_on":
            return _light("on")
        case "light_off":
            return _light("off")
        case "set_light":
            mode = params.get("mode")
            if mode not in LIGHT_MODES:
                raise InvalidCommand("Invalid light mode (on or off)")
            return _light(mode)
        case "refresh":
            return Command(action, [mqtt.pushall_request()], _full_report,
                           "Refresh", "Refreshed: full status received", print_control=False,
                           waiting_for="a full status", pushall_midway=False)
        case _:
            raise InvalidCommand(f"Unknown action: {action}")
