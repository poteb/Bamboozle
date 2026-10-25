"""Chamber light on both LED nodes, and the on-demand Refresh (pushall).

Verified on the published payloads only: no command is sent to a printer."""
import asyncio
import json

import bambulabs_api as bl
import pytest

from bamboozle import mqtt
from bamboozle.models import AppConfig, PrinterConfig
from bamboozle.printer_manager import PrinterConnection, PrinterManager
from bamboozle.routers import api

SERIAL = "31B00000000000"


class RecordingPaho:
    def __init__(self, connected=True):
        self.connected = connected
        self.published = []

    def is_connected(self):
        return self.connected

    def publish(self, topic, payload, qos=0):
        self.published.append((topic, json.loads(payload), qos))

        class Info:
            rc = 0
        return Info()


def _printer(connected=True):
    printer = bl.Printer("192.168.0.102", "x", SERIAL)
    printer.mqtt_client._client = RecordingPaho(connected)
    return printer


def _published(printer):
    return printer.mqtt_client._client.published


@pytest.mark.parametrize("action, mode", [("light_on", "on"), ("light_off", "off")])
def test_light_switches_both_chamber_led_nodes(action, mode):
    printer = _printer()
    ok, _ = PrinterManager._run_command(printer, action, {})
    assert ok is True
    sent = _published(printer)
    assert [p["system"]["led_node"] for _, p, _ in sent] == ["chamber_light", "chamber_light2"]
    for topic, payload, qos in sent:
        assert topic == f"device/{SERIAL}/request"
        assert qos == 1
        system = payload["system"]
        assert system["command"] == "ledctrl"
        assert system["led_mode"] == mode
        assert isinstance(system["sequence_id"], str)
    seqs = [p["system"]["sequence_id"] for _, p, _ in sent]
    assert len(set(seqs)) == 2  # each request has its own sequence id


def test_refresh_sends_a_standalone_pushall():
    """A1 / P1S ignore pushall when combined with other requests."""
    printer = _printer()
    ok, msg = PrinterManager._run_command(printer, "refresh", {})
    assert ok is True
    assert "refresh" in msg.lower()
    [(topic, payload, qos)] = _published(printer)
    assert topic == f"device/{SERIAL}/request"
    assert qos == 1
    assert list(payload) == ["pushing"]
    assert payload["pushing"]["command"] == "pushall"
    assert isinstance(payload["pushing"]["sequence_id"], str)


@pytest.mark.parametrize("action", ["light_on", "light_off", "refresh"])
def test_nothing_is_queued_while_the_session_is_down(action):
    """paho would replay a queued QoS 1 request on reconnect, e.g. a light toggle."""
    printer = _printer(connected=False)
    ok, msg = PrinterManager._run_command(printer, action, {})
    assert ok is False
    assert "not connected" in msg
    assert _published(printer) == []


def _manager_with_blocked_printer():
    conn = PrinterConnection(
        PrinterConfig(id="h2c", name="H2C", ip="192.168.0.102", access_code="x", serial=SERIAL))
    conn.printer.mqtt_client._client = RecordingPaho()
    conn._connected = True
    conn.state.commands_blocked = True
    mgr = PrinterManager(AppConfig(), discovery_port=None)
    mgr._connections[conn.cfg.id] = conn
    return mgr, conn


def test_refresh_works_on_a_printer_refusing_control_commands():
    """pushall is a query, which still works without Developer Mode."""
    mgr, conn = _manager_with_blocked_printer()
    ok, msg = asyncio.run(mgr.execute_command("h2c", "refresh"))
    assert ok is True
    assert "Developer Mode" not in msg


def test_refresh_endpoint():
    calls = []

    class Mgr:
        async def execute_command(self, printer_id, action, params=None):
            calls.append((printer_id, action))
            return True, "Refresh requested"

    class Req:
        class app:
            class state:
                manager = Mgr()

    result = asyncio.run(api.refresh_printer(Req(), "h2c"))
    assert result == {"success": True, "message": "Refresh requested"}
    assert calls == [("h2c", "refresh")]
