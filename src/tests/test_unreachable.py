"""'printer unreachable at <ip>' instead of 'Printer Values Not Available Yet'."""
import asyncio
import logging

from bamboozle import discovery
from bamboozle.models import PrinterAddRequest, PrinterConfig
from bamboozle.printer_manager import PrinterConnection
from bamboozle.routers.api import test_connection as api_test_connection

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

    class FakeWs:
        def __init__(self):
            self.sent = []

        async def send_json(self, msg):
            self.sent.append(msg)

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
