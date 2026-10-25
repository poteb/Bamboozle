"""'printer unreachable at <ip>' instead of 'Printer Values Not Available Yet'."""
import asyncio
import logging

from bamboozle import discovery
from bamboozle.models import PrinterAddRequest, PrinterConfig
from bamboozle.printer_manager import PrinterConnection
from bamboozle.routers.api import test_connection as api_test_connection
from fakes import FakeWs

IP = "192.168.0.51"


def _unreachable(monkeypatch):
    monkeypatch.setattr(discovery, "tcp_reachable", lambda ip, port=8883, timeout=3.0: False)


def test_connect_reports_unreachable_ip(monkeypatch, caplog):
    _unreachable(monkeypatch)
    conn = PrinterConnection(
        PrinterConfig(name="P1S", ip=IP, access_code="x", serial="01P00C511100150"))

    async def scenario():
        ok = await conn.connect()
        state = await conn.read_state()
        return ok, state

    with caplog.at_level(logging.INFO):
        ok, state = asyncio.run(scenario())

    assert ok is False
    assert state.online is False
    assert f"printer unreachable at {IP}" in caplog.text
    assert "Printer Values Not Available Yet" not in caplog.text


def test_api_test_endpoint_reports_unreachable_ip(monkeypatch, caplog):
    _unreachable(monkeypatch)
    body = PrinterAddRequest(name="P1S", ip=IP, access_code="x", serial="01P00C511100150")

    with caplog.at_level(logging.INFO):
        result = asyncio.run(api_test_connection(body))

    assert result == {"success": False, "message": f"printer unreachable at {IP}"}
    assert "Printer Values Not Available Yet" not in caplog.text


def test_unreachable_printer_does_not_stall_dashboard_updates(monkeypatch):
    """Reconnect probes run in the background; the poll loop keeps broadcasting."""
    import time

    from bamboozle.models import AppConfig
    from bamboozle.printer_manager import PrinterManager

    def slow_unreachable(ip, port=8883, timeout=3.0):
        time.sleep(0.5)
        return False

    monkeypatch.setattr(discovery, "tcp_reachable", slow_unreachable)

    async def scenario():
        cfg = PrinterConfig(name="P1S", ip=IP, access_code="x", serial="01P00C511100150")
        mgr = PrinterManager(AppConfig(printers=[cfg], poll_interval=0.05), discovery_port=None)
        ws = FakeWs()
        mgr.register_ws(ws)
        await mgr.start()
        await asyncio.sleep(0.4)
        updates = sum(1 for m in ws.sent if m["type"] == "update")
        await mgr.stop()
        return updates

    assert asyncio.run(scenario()) >= 3


def test_unreachable_printer_is_retried_with_backoff(monkeypatch):
    from bamboozle.models import AppConfig
    from bamboozle.printer_manager import PrinterManager

    probes = []
    monkeypatch.setattr(
        discovery, "tcp_reachable", lambda ip, port=8883, timeout=3.0: probes.append(ip) or False)

    async def scenario():
        cfg = PrinterConfig(name="P1S", ip=IP, access_code="x", serial="01P00C511100150")
        mgr = PrinterManager(AppConfig(printers=[cfg], poll_interval=0.02), discovery_port=None)
        await mgr.start()
        await asyncio.sleep(0.5)
        await mgr.stop()

    asyncio.run(scenario())
    assert len(probes) == 1  # first retry is due after 3s, not every poll


def test_session_lost_mid_run_is_reported_unreachable_on_reconnect(monkeypatch, caplog):
    """IP changed while running: MQTT drops, the reconnect hits the TCP pre-check."""
    import bambulabs_api as bl

    from bamboozle.models import AppConfig
    from bamboozle.printer_manager import PrinterManager

    reachable = {"value": True}
    monkeypatch.setattr(
        discovery, "tcp_reachable", lambda ip, port=8883, timeout=3.0: reachable["value"])
    # MQTT "starts" but the session never comes up (the printer has moved away).
    monkeypatch.setattr(bl.Printer, "mqtt_start", lambda self: None)
    monkeypatch.setattr(PrinterConnection, "MQTT_GRACE_SECONDS", 0.1)
    monkeypatch.setattr(PrinterConnection, "MQTT_SETTLE_SECONDS", 0)
    monkeypatch.setattr(PrinterConnection, "INITIAL_BACKOFF_SECONDS", 0.01)

    async def scenario():
        cfg = PrinterConfig(name="P1S", ip=IP, access_code="x", serial="01P00C511100150")
        mgr = PrinterManager(AppConfig(printers=[cfg], poll_interval=0.02), discovery_port=None)
        await mgr.start()
        await asyncio.sleep(0.05)
        reachable["value"] = False
        await asyncio.sleep(0.4)
        await mgr.stop()

    with caplog.at_level(logging.INFO):
        asyncio.run(scenario())
    assert f"printer unreachable at {IP}" in caplog.text
