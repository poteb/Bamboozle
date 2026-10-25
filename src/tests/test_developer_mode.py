"""Developer Mode off (the `fun` bit): shown as a hint, never fails a command.

The user's H2C and P2S report Developer Mode off and carry HMS
0500050000010007, yet the chamber light toggles from Bamboozle work on both
(confirmed 2026-10-25). So neither signal means "every command is refused".
"""
import asyncio

import pytest

from bamboozle import protocol
from bamboozle.models import AppConfig, PrinterConfig
from bamboozle.printer_manager import PrinterConnection, PrinterManager
from fakes import connected

# Captured from the user's H2C (Developer Mode off): bit 0x20000000 of `fun`
# is set, and HMS carries 0x05000500 / 0x00010007 next to unrelated entries.
H2C_FUN = "14023FF1AFFF9CB7"
VERIFY_FAILED = {"attr": 0x05000500, "code": 0x00010007}
OTHER_HMS = {"attr": 83887616, "code": 131078}


@pytest.mark.parametrize("fun, expected", [
    (H2C_FUN, False),               # bit set -> dev mode off
    ("14023FF18FFF9CB7", True),     # same value with bit 29 cleared
    (0x8FFF9CB7, True),             # ints are accepted too
    (0xAFFF9CB7, False),
    (None, None),                   # A1 / P1 never send `fun`
    ("not-hex", None),
    ("", None),
])
def test_developer_mode_from_fun(fun, expected):
    payload = {} if fun is None else {"fun": fun}
    assert protocol.developer_mode(payload) is expected


def test_verify_failed_hms_alone_says_nothing_about_developer_mode():
    """The HMS records a past refusal; without `fun` the state stays unknown."""
    assert protocol.developer_mode({"hms": [VERIFY_FAILED]}) is None


@pytest.mark.parametrize("hms, expected", [
    ([OTHER_HMS, VERIFY_FAILED], {"0500060000020006", "0500050000010007"}),
    ([{"attr": "05000500", "code": "00010007"}], {"0500050000010007"}),   # hex-string form
    ([{"attr": "junk", "code": None}, 5], set()),                          # malformed entries
    (None, set()),
])
def test_hms_codes(hms, expected):
    payload = {} if hms is None else {"hms": hms}
    assert protocol.hms_codes(payload) == expected


def _conn(print_payload):
    conn = PrinterConnection(
        PrinterConfig(name="H2C", ip="192.168.0.102", access_code="x", serial="31B00000000000"))
    conn.report.print = dict(print_payload)
    conn._connected = True
    return conn


def test_state_reports_developer_mode_off():
    conn = _conn({"fun": H2C_FUN, "hms": [OTHER_HMS, VERIFY_FAILED]})
    state = asyncio.run(conn.read_state())
    assert state.developer_mode is False
    # No claim that commands are refused: the light works on these printers.
    assert "commands_blocked" not in state.model_dump()


@pytest.mark.parametrize("payload", [{"hms": []}, {"hms": [VERIFY_FAILED]}])
def test_state_reports_unknown_developer_mode_without_fun(payload):
    conn = _conn(payload)
    state = asyncio.run(conn.read_state())
    assert state.developer_mode is None


ACTIONS = ["pause", "resume", "stop", "light_on", "light_off", "set_speed", "refresh"]


@pytest.mark.parametrize("action", ACTIONS)
def test_developer_mode_off_never_fails_a_command(monkeypatch, action):
    """Sent and not confirmed reads as success (not confirmed), never failure."""
    monkeypatch.setattr(PrinterConnection, "COMMAND_CONFIRM_SECONDS", 0.05)

    async def scenario():
        state = "PAUSE" if action == "resume" else "RUNNING"  # not already done
        conn = connected(_conn({"fun": H2C_FUN, "hms": [VERIFY_FAILED], "gcode_state": state}))
        await conn.read_state()
        assert conn.state.developer_mode is False
        mgr = PrinterManager(AppConfig(), discovery_port=None)
        mgr._connections[conn.cfg.id] = conn
        return await mgr.execute_command(conn.cfg.id, action, {"level": 2})

    result = asyncio.run(scenario())
    assert result.success is True
    assert result.confirmed is False
