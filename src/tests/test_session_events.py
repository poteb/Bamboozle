"""A PrinterConnection's MQTT session: drops, retired clients, the thread
handoff, and asking for a full report until one arrives."""
import asyncio
import threading

import pytest

from bamboozle import discovery, mqtt
from bamboozle.models import PrinterConfig
from bamboozle.printer_manager import PrinterConnection
from fakes import RecordingPaho, connack, deliver

SUCCESS = 0


@pytest.fixture
def lan(monkeypatch):
    monkeypatch.setattr(discovery, "tcp_reachable", lambda ip, port=8883, timeout=3.0: True)
    monkeypatch.setattr(mqtt.BambuClient, "start", lambda self: connack(self, SUCCESS))
    monkeypatch.setattr(mqtt.BambuClient, "is_connected", lambda self: True)
    monkeypatch.setattr(PrinterConnection, "MQTT_SETTLE_SECONDS", 0)
    retired = []
    monkeypatch.setattr(mqtt, "retire", lambda client: retired.append(client))
    return retired


def _conn():
    return PrinterConnection(
        PrinterConfig(name="A1", ip="192.168.0.101", access_code="x", serial="03919A000000000"))


def _drop(client):
    client._on_disconnect(client.paho, None, None, 7, None)


def test_a_dropped_session_is_retired_and_replaced_at_once(lan):
    retired = lan

    async def scenario():
        conn = _conn()
        assert await conn.connect()
        old = conn.client
        deliver(old, {"print": {"command": "push_status", "msg": 0, "gcode_state": "RUNNING"}})
        assert conn.report.print
        _drop(old)
        return conn, old

    conn, old = asyncio.run(scenario())
    assert retired == [old]  # with its queue of unacked requests
    assert conn.client is not old
    assert conn.report.print == {}  # the new session starts empty
    assert conn._connected is False and conn.needs_reconnect() is False  # backs off first


def test_a_drop_from_a_client_that_never_logged_in_is_left_to_connect(lan, monkeypatch):
    monkeypatch.setattr(mqtt.BambuClient, "start", lambda self: None)

    async def scenario():
        conn = _conn()
        await conn.connect()
        client = conn.client
        _drop(client)  # e.g. a refused login; connect() / the grace handle it
        return conn, client

    conn, client = asyncio.run(scenario())
    assert conn.client is client


def test_a_drop_while_settling_fails_the_connect(lan, monkeypatch):
    monkeypatch.setattr(PrinterConnection, "MQTT_SETTLE_SECONDS", 0.05)

    async def scenario():
        conn = _conn()
        task = asyncio.create_task(conn.connect())
        await asyncio.sleep(0.01)
        _drop(conn.client)
        return conn, await task

    conn, ok = asyncio.run(scenario())
    assert ok is False
    assert conn._connected is False


def test_reports_from_a_retired_client_are_ignored(lan):
    async def scenario():
        conn = _conn()
        await conn.connect()
        old = conn.client
        await conn.reset_session()
        deliver(old, {"print": {"command": "push_status", "msg": 0, "gcode_state": "PAUSE"}})
        return conn

    conn = asyncio.run(scenario())
    assert conn.report.print == {}
    assert conn._reports == 0


def test_reports_from_paho_thread_are_merged_on_the_loop_in_order(lan):
    seen = []

    async def scenario():
        conn = _conn()
        await conn.connect()
        loop_thread = threading.get_ident()
        conn.on_change = lambda: seen.append(
            (threading.get_ident() == loop_thread, conn.report.print.get("mc_percent")))

        def paho_thread():
            for pct in range(100):
                deliver(conn.client, {"print": {"command": "push_status", "msg": 1, "mc_percent": pct}})

        await asyncio.to_thread(paho_thread)
        await asyncio.sleep(0.05)
        return conn

    conn = asyncio.run(scenario())
    assert [pct for _, pct in seen] == list(range(100))
    assert all(on_loop for on_loop, _ in seen)
    assert conn._reports == 100


def test_pushall_is_repeated_until_a_full_report_arrives(lan, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr("bamboozle.printer_manager.time.monotonic", lambda: clock[0])

    async def scenario():
        conn = _conn()
        await conn.connect()  # the client's own on-connect pushall counts as the first
        conn.client._paho = RecordingPaho()
        sent_at = []
        for t in range(0, 200):
            clock[0] = 1000.0 + t
            before = len(conn.client.paho.published)
            await conn.ensure_full_report()
            if len(conn.client.paho.published) > before:
                sent_at.append(t)
            if t == 100:
                deliver(conn.client, {"print": {"command": "push_status", "msg": 0}})
        return sent_at, conn.client.paho.published

    sent_at, published = asyncio.run(scenario())
    assert sent_at == [10, 30, 70]  # 10 s, then doubling; none after the full report
    assert all(p == {"pushing": {"command": "pushall", "sequence_id": p["pushing"]["sequence_id"]}}
               for _, p, _ in published)


def test_no_pushall_retries_once_a_full_report_came(lan):
    async def scenario():
        conn = _conn()
        await conn.connect()
        conn.client._paho = RecordingPaho()
        deliver(conn.client, {"print": {"command": "push_status", "msg": 0}})
        conn._pushall_at = 0.0  # long ago
        await conn.ensure_full_report()
        return conn.client.paho.published

    assert asyncio.run(scenario()) == []
