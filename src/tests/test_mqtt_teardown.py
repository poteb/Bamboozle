"""MQTT teardown: disconnect first, never block the event loop (edit, relocate,
remove, shutdown, the Settings connection test)."""
import asyncio
import threading
import time

import bambulabs_api as bl
import pytest

from bamboozle import discovery, mqtt
from bamboozle.models import AppConfig, PrinterAddRequest, PrinterConfig
from bamboozle.printer_manager import PrinterConnection, PrinterManager
from bamboozle.routers import api
from bamboozle.routers.api import test_connection as api_test_connection

SERIAL = "01P00C511100150"


class FakePaho:
    """Records teardown calls; loop_stop blocks until released, like a join on
    a network thread that sits in a TLS handshake."""

    def __init__(self, release: threading.Event | None = None):
        self.calls = []
        self.on_connect = object()
        self.on_message = object()
        self.on_disconnect = object()
        self.callbacks_at_disconnect = None
        self._release = release
        self.stopped = threading.Event()

    def disconnect(self):
        self.calls.append("disconnect")
        self.callbacks_at_disconnect = (self.on_connect, self.on_message, self.on_disconnect)

    def loop_stop(self):
        self.calls.append("loop_stop")
        if self._release is not None:
            self._release.wait(5)
        self.stopped.set()


def _printer_with(paho):
    printer = bl.Printer("192.168.0.103", "x", SERIAL)
    printer.mqtt_client._client = paho
    return printer


def test_retire_clears_callbacks_then_disconnects_before_loop_stop():
    paho = FakePaho()
    thread = mqtt.retire(_printer_with(paho))
    # Cleared before retire returns, so a late CONNACK can't resubscribe.
    assert (paho.on_connect, paho.on_message, paho.on_disconnect) == (None, None, None)
    thread.join(2)
    assert paho.calls[:2] == ["disconnect", "loop_stop"]
    assert paho.callbacks_at_disconnect == (None, None, None)


def test_retire_does_not_wait_for_a_blocked_network_thread():
    release = threading.Event()
    paho = FakePaho(release)
    started = time.monotonic()
    thread = mqtt.retire(_printer_with(paho))
    assert time.monotonic() - started < 0.5
    assert thread.is_alive()  # still joining in the background
    release.set()
    thread.join(2)
    assert paho.stopped.is_set()


def test_retire_disconnects_again_after_the_thread_stops():
    """A handshake that finished during the first disconnect gets closed too."""
    paho = FakePaho()
    mqtt.retire(_printer_with(paho)).join(2)
    assert paho.calls == ["disconnect", "loop_stop", "disconnect"]


def test_retire_keeps_going_when_disconnect_fails():
    paho = FakePaho()

    def boom():
        paho.calls.append("disconnect")
        raise OSError("socket gone")

    paho.disconnect = boom
    mqtt.retire(_printer_with(paho)).join(2)
    assert "loop_stop" in paho.calls


def test_retire_tolerates_a_printer_without_a_paho_client():
    class Bare:
        mqtt_client = object()

    assert mqtt.retire(Bare()) is None


def test_retire_on_a_real_never_started_client_is_harmless():
    printer = bl.Printer("192.168.0.103", "x", SERIAL)
    mqtt.retire(printer).join(2)


# --- PrinterConnection / PrinterManager -----------------------------------------


@pytest.fixture
def lan(monkeypatch):
    monkeypatch.setattr(discovery, "tcp_reachable", lambda ip, port=8883, timeout=3.0: True)
    monkeypatch.setattr(bl.Printer, "mqtt_start", lambda self: None)
    monkeypatch.setattr(PrinterConnection, "MQTT_SETTLE_SECONDS", 0)


@pytest.fixture
def blocked_teardown(monkeypatch):
    """Every retired client blocks in loop_stop until the test ends."""
    release = threading.Event()
    clients = []
    orig_init = PrinterConnection._new_printer

    def new_printer(self):
        printer = orig_init(self)
        paho = FakePaho(release)
        clients.append(paho)
        printer.mqtt_client._client = paho
        return printer

    monkeypatch.setattr(PrinterConnection, "_new_printer", new_printer)
    yield clients
    release.set()


def _cfg(**kw):
    fields = dict(id="p1s", name="P1S", ip="192.168.0.103", access_code="x", serial=SERIAL)
    return PrinterConfig(**{**fields, **kw})


def test_disconnect_returns_at_once_and_disconnects(blocked_teardown):
    conn = PrinterConnection(_cfg())
    paho = blocked_teardown[0]

    async def scenario():
        await asyncio.wait_for(conn.disconnect(), 0.5)

    asyncio.run(scenario())
    assert paho.calls[0] == "disconnect"


def test_reset_session_retires_the_client_and_starts_a_fresh_one(lan, blocked_teardown):
    conn = PrinterConnection(_cfg())
    old_printer = conn.printer

    async def scenario():
        await conn.connect()
        await asyncio.wait_for(conn.reset_session(), 0.5)

    asyncio.run(scenario())
    assert blocked_teardown[0].calls[0] == "disconnect"
    assert conn.printer is not old_printer
    # The fresh client reports logins and messages to the connection too.
    mc = conn.printer.mqtt_client
    assert mc.on_connect_handler == conn._on_mqtt_connack
    assert mc.on_message_handler == conn._on_mqtt_message


def test_connection_widens_the_inflight_window():
    """The Bambu broker's PUBACKs race paho's QoS 1 tracking; the default
    window of 20 wedges a session after a few dozen commands."""
    conn = PrinterConnection(_cfg())
    assert conn.printer.mqtt_client._client._max_inflight_messages == mqtt.INFLIGHT_WINDOW


def test_edit_and_remove_never_wait_for_teardown(lan, blocked_teardown, monkeypatch):
    monkeypatch.setattr(PrinterManager, "SHUTDOWN_TEARDOWN_SECONDS", 0.1)

    async def scenario():
        mgr = PrinterManager(AppConfig(printers=[_cfg()]), discovery_port=None)
        await mgr.start()
        await asyncio.wait_for(mgr.update_printer("p1s", _cfg(name="P1S edited")), 0.5)
        await asyncio.wait_for(mgr.remove_printer("p1s"), 0.5)
        await mgr.stop()

    asyncio.run(scenario())
    assert len(blocked_teardown) == 2
    assert all(c.calls and c.calls[0] == "disconnect" for c in blocked_teardown)


def test_shutdown_waits_briefly_for_the_disconnects_to_go_out(lan, monkeypatch):
    """So the DISCONNECT packets leave before the process exits, but bounded."""
    monkeypatch.setattr(PrinterManager, "SHUTDOWN_TEARDOWN_SECONDS", 0.3)
    release = threading.Event()
    paho = FakePaho(release)

    async def scenario():
        mgr = PrinterManager(AppConfig(printers=[_cfg()]), discovery_port=None)
        await mgr.start()
        mgr.get_connection("p1s").printer.mqtt_client._client = paho
        started = time.monotonic()
        await mgr.stop()
        return time.monotonic() - started

    elapsed = asyncio.run(scenario())
    release.set()
    assert 0.2 < elapsed < 1.0
    assert paho.calls[0] == "disconnect"


# --- POST /api/printers/test ---------------------------------------------------


def _body():
    return PrinterAddRequest(name="P1S", ip="192.168.0.103", access_code="x", serial=SERIAL)


@pytest.mark.parametrize("start", [
    lambda self: None,  # no CONNACK: times out
    lambda self: self.mqtt_client.on_connect_handler(self.mqtt_client, None, None, None, 0, None),
])
def test_api_test_retires_its_throwaway_session(lan, monkeypatch, start):
    monkeypatch.setattr(api, "TEST_CONNACK_TIMEOUT", 0.1)
    monkeypatch.setattr(api, "TEST_REPORT_TIMEOUT", 0.1)
    monkeypatch.setattr(bl.Printer, "mqtt_start", start)
    retired = []
    monkeypatch.setattr(mqtt, "retire", lambda printer: retired.append(printer))

    asyncio.run(api_test_connection(_body()))
    assert len(retired) == 1
    assert isinstance(retired[0], bl.Printer)
