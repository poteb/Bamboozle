"""Printer refusing control commands (Developer Mode off / HMS 0500050000010007)."""
import asyncio

import pytest

from bamboozle import protocol
from bamboozle.models import AppConfig, PrinterConfig
from bamboozle.printer_manager import PrinterConnection, PrinterManager

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


@pytest.mark.parametrize("hms", [
    [VERIFY_FAILED],
    [OTHER_HMS, VERIFY_FAILED],
    [{"attr": "05000500", "code": "00010007"}],   # hex-string form
])
def test_verify_failed_hms_blocks_commands(hms):
    assert protocol.commands_blocked({"hms": hms}) is True


def test_developer_mode_off_blocks_commands():
    assert protocol.commands_blocked({"fun": H2C_FUN, "hms": []}) is True


@pytest.mark.parametrize("payload", [
    {},                                             # nothing received yet
    {"hms": []},                                    # A1 / P1S: no fun, no HMS
    {"hms": [OTHER_HMS]},
    {"fun": "14023FF18FFF9CB7", "hms": []},         # dev mode on
    {"hms": [{"attr": "junk", "code": None}, 5]},   # malformed entries
])
def test_commands_not_blocked(payload):
    assert protocol.commands_blocked(payload) is False


class FakeMqttClient:
    def is_connected(self):
        return True


class DumpOnlyPrinter:
    """Only `mqtt_dump()`; every library getter raises, so _read_sync defaults."""

    def __init__(self, print_payload):
        self._dump = {"print": print_payload}
        self.mqtt_client = FakeMqttClient()

    def mqtt_dump(self):
        return self._dump


def _conn(print_payload):
    conn = PrinterConnection(
        PrinterConfig(name="H2C", ip="192.168.0.102", access_code="x", serial="31B00000000000"))
    conn.printer = DumpOnlyPrinter(print_payload)
    conn._connected = True
    return conn


def test_state_reports_blocked_printer():
    conn = _conn({"fun": H2C_FUN, "hms": [OTHER_HMS, VERIFY_FAILED]})
    state = asyncio.run(conn.read_state())
    assert state.developer_mode is False
    assert state.commands_blocked is True


def test_state_reports_unknown_developer_mode_without_fun():
    conn = _conn({"hms": []})
    state = asyncio.run(conn.read_state())
    assert state.developer_mode is None
    assert state.commands_blocked is False


def _manager_with(conn, monkeypatch):
    sent = []

    def fake_run(printer, action, params):
        sent.append(action)
        return True, "Print paused"

    monkeypatch.setattr(PrinterManager, "_run_command", staticmethod(fake_run))
    mgr = PrinterManager(AppConfig(), discovery_port=None)
    mgr._connections[conn.cfg.id] = conn
    return mgr, sent


@pytest.mark.parametrize("action", ["pause", "resume", "stop", "light_on", "set_speed"])
def test_command_to_blocked_printer_says_why(monkeypatch, action):
    conn = _conn({"fun": H2C_FUN, "hms": [VERIFY_FAILED]})
    asyncio.run(conn.read_state())
    mgr, sent = _manager_with(conn, monkeypatch)

    ok, msg = asyncio.run(mgr.execute_command(conn.cfg.id, action, {"level": 2}))

    assert ok is False
    assert "Developer Mode" in msg
    # Still sent: if detection were ever wrong, the command must keep working.
    assert sent == [action]


def test_command_to_unblocked_printer_is_unchanged(monkeypatch):
    conn = _conn({"hms": []})
    asyncio.run(conn.read_state())
    mgr, sent = _manager_with(conn, monkeypatch)

    assert asyncio.run(mgr.execute_command(conn.cfg.id, "pause")) == (True, "Print paused")
    assert sent == ["pause"]
