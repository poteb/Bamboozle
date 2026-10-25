"""PrinterConnection.is_offline / reconnect pacing, driven through connect()."""
import asyncio

import bambulabs_api as bl
import pytest

from bamboozle import discovery
from bamboozle.models import AppConfig, PrinterConfig
from bamboozle.printer_manager import PrinterConnection, PrinterManager


class FakeMqtt:
    def __init__(self):
        self.up = False

    def is_connected(self):
        return self.up


@pytest.fixture
def lan(monkeypatch):
    monkeypatch.setattr(discovery, "tcp_reachable", lambda ip, port=8883, timeout=3.0: True)
    monkeypatch.setattr(bl.Printer, "mqtt_start", lambda self: None)
    monkeypatch.setattr(PrinterConnection, "MQTT_SETTLE_SECONDS", 0)
    monkeypatch.setattr(PrinterConnection, "MQTT_GRACE_SECONDS", 0.2)


def _conn():
    conn = PrinterConnection(
        PrinterConfig(name="P1S", ip="192.168.0.103", access_code="x", serial="01P00C511100150"))
    conn.printer.mqtt_client = FakeMqtt()
    return conn


def test_never_connected_printer_is_offline(lan):
    assert _conn().is_offline() is True


def test_brief_blip_on_long_lived_session_is_not_offline(lan):
    async def scenario():
        conn = _conn()
        await conn.connect()
        conn.printer.mqtt_client.up = True
        await asyncio.sleep(0.3)          # session up for longer than the grace
        assert conn.is_offline() is False  # seen connected now
        conn.printer.mqtt_client.up = False  # paho drops; it may reconnect itself
        return conn.is_offline()

    assert asyncio.run(scenario()) is False


def test_session_down_longer_than_grace_is_offline(lan):
    async def scenario():
        conn = _conn()
        await conn.connect()
        conn.printer.mqtt_client.up = True
        assert conn.is_offline() is False
        conn.printer.mqtt_client.up = False
        await asyncio.sleep(0.3)
        return conn.is_offline()

    assert asyncio.run(scenario()) is True


def test_session_that_never_comes_up_backs_off(lan, monkeypatch):
    """Wrong printer at the saved IP (TCP ok, MQTT never up): retries slow down,
    leaving growing offline windows in which an announcement can relocate it."""
    monkeypatch.setattr(PrinterConnection, "MQTT_GRACE_SECONDS", 0.05)
    monkeypatch.setattr(PrinterConnection, "INITIAL_BACKOFF_SECONDS", 0.1)
    starts = []
    monkeypatch.setattr(bl.Printer, "mqtt_start", lambda self: starts.append(1))

    async def scenario():
        cfg = PrinterConfig(name="P1S", ip="192.168.0.51", access_code="x", serial="01P00C511100150")
        mgr = PrinterManager(AppConfig(printers=[cfg], poll_interval=0.01), discovery_port=None)
        await mgr.start()
        await asyncio.sleep(1.0)
        await mgr.stop()

    asyncio.run(scenario())
    # Fixed 0.1s backoff would give ~7 attempts in 1s; doubling gives ~4.
    assert 2 <= len(starts) <= 5
