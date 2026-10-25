"""Chamber light on both LED nodes, and the on-demand Refresh (pushall).

Verified on the published payloads only: no command is sent to a printer."""
import asyncio

import pytest

from bamboozle.models import AppConfig, PrinterConfig
from bamboozle.printer_manager import CommandResult, PrinterConnection, PrinterManager
from bamboozle.routers import api
from fakes import RecordingPaho, connected

SERIAL = "31B00000000000"


def _conn(connected_=True):
    conn = PrinterConnection(
        PrinterConfig(id="h2c", name="H2C", ip="192.168.0.102", access_code="x", serial=SERIAL))
    return conn


async def _run(action, connected_=True, dev_mode_off=False):
    conn = connected(_conn(), {"gcode_state": "IDLE"}, paho=RecordingPaho(connected_))
    conn.report.info = {"module": [{"name": "ota", "sw_ver": "01.00.00.00"}]}
    if dev_mode_off:
        conn.report.print["fun"] = "14023FF1AFFF9CB7"
        await conn.read_state()
    mgr = PrinterManager(AppConfig(), discovery_port=None)
    mgr._connections["h2c"] = conn
    result = await mgr.execute_command("h2c", action)
    return result, conn.client.paho.published


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    monkeypatch.setattr(PrinterConnection, "COMMAND_CONFIRM_SECONDS", 0.05)


@pytest.mark.parametrize("action, mode", [("light_on", "on"), ("light_off", "off")])
def test_light_switches_both_chamber_led_nodes(action, mode):
    result, sent = asyncio.run(_run(action))
    assert result.success is True
    light = [(t, p, q) for t, p, q in sent if "system" in p]
    assert [p["system"]["led_node"] for _, p, _ in light] == ["chamber_light", "chamber_light2"]
    for topic, payload, qos in light:
        assert topic == f"device/{SERIAL}/request"
        assert qos == 1
        system = payload["system"]
        assert system["command"] == "ledctrl"
        assert system["led_mode"] == mode
        assert isinstance(system["sequence_id"], str)
    seqs = [p["system"]["sequence_id"] for _, p, _ in light]
    assert len(set(seqs)) == 2  # each request has its own sequence id


def test_refresh_sends_a_standalone_pushall():
    """A1 / P1S ignore pushall when combined with other requests."""
    result, sent = asyncio.run(_run("refresh"))
    assert result.success is True
    [(topic, payload, qos)] = sent
    assert topic == f"device/{SERIAL}/request"
    assert qos == 1
    assert list(payload) == ["pushing"]
    assert payload["pushing"]["command"] == "pushall"
    assert isinstance(payload["pushing"]["sequence_id"], str)


@pytest.mark.parametrize("action", ["light_on", "light_off", "refresh"])
def test_nothing_is_queued_while_the_session_is_down(action):
    """paho would replay a queued QoS 1 request on reconnect, e.g. a light toggle."""
    result, sent = asyncio.run(_run(action, connected_=False))
    assert result.success is False
    assert "not connected" in result.message
    assert sent == []


def test_refresh_works_on_a_printer_with_developer_mode_off():
    """pushall is a query, which works without Developer Mode."""
    result, _ = asyncio.run(_run("refresh", dev_mode_off=True))
    assert result.success is True
    assert "Developer Mode" not in result.message


@pytest.mark.parametrize("action", ["light_on", "light_off"])
def test_light_reports_success_on_a_printer_with_developer_mode_off(action):
    """The user's H2C and P2S toggle the light fine with Developer Mode off."""
    result, sent = asyncio.run(_run(action, dev_mode_off=True))
    assert result.success is True
    assert "Developer Mode" not in result.message
    assert len([p for _, p, _ in sent if "system" in p]) == 2


def test_refresh_endpoint():
    calls = []

    class Mgr:
        async def execute_command(self, printer_id, action, params=None):
            calls.append((printer_id, action))
            return CommandResult(True, "Refresh requested", False)

    class Req:
        class app:
            class state:
                manager = Mgr()

    result = asyncio.run(api.refresh_printer(Req(), "h2c"))
    assert result == {"success": True, "message": "Refresh requested", "confirmed": False}
    assert calls == [("h2c", "refresh")]


def test_the_speed_endpoint_needs_a_level():
    """A missing level used to mean "silent"; now it's refused."""
    calls = []

    class Mgr:
        async def execute_command(self, printer_id, action, params=None):
            calls.append(params)
            return CommandResult(False, "Invalid speed level")

    class Req:
        class app:
            class state:
                manager = Mgr()

    asyncio.run(api.set_speed(Req(), "h2c", {}))
    assert calls == [{"level": None}]
