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


FULL = {"print": {"command": "push_status", "msg": 0, "gcode_state": "RUNNING", "nozzle_temper": 200}}


def test_a_dropped_session_reconnects_at_once_and_keeps_its_card(lan, monkeypatch):
    retired = lan
    reconnects = []
    monkeypatch.setattr(PrinterConnection, "STALE_SECONDS", 0.05)  # "long-lived" here

    async def scenario():
        conn = _conn()
        conn.on_reconnect_due = lambda: reconnects.append(conn.needs_reconnect())
        assert await conn.connect()
        old = conn.client
        deliver(old, FULL)
        await asyncio.sleep(0.06)
        _drop(old)
        during = await conn.read_state()
        return conn, old, during

    conn, old, during = asyncio.run(scenario())
    assert retired == [old]  # with its queue of unacked requests
    assert conn.client is not old
    # The card stays as it was (online, same data) while the new session connects.
    assert during.online is True and during.gcode_state == "RUNNING"
    assert conn._full_seen is False  # the new session asks for a full report again
    assert reconnects == [True]  # due now, not after a backoff


def test_a_session_that_keeps_dropping_backs_off_but_keeps_its_card(lan):
    reconnects = []

    async def scenario():
        conn = _conn()
        conn.on_reconnect_due = lambda: reconnects.append(1)
        await conn.connect()
        deliver(conn.client, FULL)
        _drop(conn.client)  # seconds after connecting
        return conn, await conn.read_state()

    conn, state = asyncio.run(scenario())
    assert reconnects == [] and conn.needs_reconnect() is False
    assert state.online is True


def test_the_card_goes_offline_when_the_reconnect_grace_runs_out(lan, monkeypatch):
    monkeypatch.setattr(PrinterConnection, "MQTT_GRACE_SECONDS", 0.05)

    async def scenario():
        conn = _conn()
        await conn.connect()
        deliver(conn.client, FULL)
        _drop(conn.client)
        await asyncio.sleep(0.06)
        return conn, await conn.read_state()

    conn, state = asyncio.run(scenario())
    assert state.online is False
    assert conn.report.print == {}  # not shown as live when the printer is back


def test_an_unreachable_printer_after_a_drop_forgets_the_kept_report(lan, monkeypatch):
    async def scenario():
        conn = _conn()
        await conn.connect()
        deliver(conn.client, FULL)
        _drop(conn.client)
        monkeypatch.setattr(discovery, "tcp_reachable", lambda ip, port=8883, timeout=3.0: False)
        conn._retry_at = 0
        await conn.connect()
        return conn, await conn.read_state()

    conn, state = asyncio.run(scenario())
    assert state.online is False and conn.report.print == {}


def test_silence_after_a_reconnect_reads_stale_not_check_the_serial(lan, monkeypatch):
    monkeypatch.setattr(PrinterConnection, "STALE_SECONDS", 0.05)

    async def scenario():
        conn = _conn()
        await conn.connect()
        deliver(conn.client, FULL)
        await conn.reset_session()
        conn._retry_at = 0
        await conn.connect()  # a new session that stays silent
        await asyncio.sleep(0.06)
        await conn.check_silence()
        return conn._down_reason

    from bamboozle import protocol
    assert asyncio.run(scenario()) == protocol.STALE


def test_a_drop_from_a_session_that_never_reported_backs_off(lan, monkeypatch):
    """E.g. the broker refused or dropped us before a single report: paho's
    thread is gone, so the client is replaced now, on the usual backoff."""
    reconnects = []

    async def scenario():
        conn = _conn()
        conn.on_reconnect_due = lambda: reconnects.append(1)
        await conn.connect()
        client = conn.client
        _drop(client)
        return conn, client

    conn, client = asyncio.run(scenario())
    assert conn.client is not client
    assert reconnects == [] and conn.needs_reconnect() is False


def test_a_drop_from_paho_s_thread_is_handled_on_the_loop(lan, monkeypatch):
    threads = []
    real = PrinterConnection._on_drop
    monkeypatch.setattr(PrinterConnection, "_on_drop",
                        lambda self, client: threads.append(threading.get_ident()) or real(self, client))

    async def scenario():
        conn = _conn()
        assert await conn.connect()
        old = conn.client
        deliver(old, {"print": {"command": "push_status", "msg": 0, "gcode_state": "IDLE"}})
        await asyncio.to_thread(_drop, old)
        await asyncio.sleep(0.02)
        return conn, old, threading.get_ident()

    conn, old, loop_thread = asyncio.run(scenario())
    assert conn.client is not old
    assert threads == [loop_thread]


def test_reports_after_disconnect_are_ignored(lan):
    async def scenario():
        conn = _conn()
        await conn.connect()
        await conn.disconnect()
        deliver(conn.client, {"print": {"command": "push_status", "msg": 0, "gcode_state": "PAUSE"}})
        return conn

    assert asyncio.run(scenario()).report.print == {}


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


def test_the_new_sessions_full_report_replaces_the_kept_one(lan):
    """Keys from before the drop must not linger in the new session's state."""
    async def scenario():
        conn = _conn()
        await conn.connect()
        deliver(conn.client, {"print": {**FULL["print"], "url": "ftp://old.3mf"}})
        _drop(conn.client)
        conn._retry_at = 0
        await conn.connect()
        deliver(conn.client, {"print": {"command": "push_status", "msg": 0, "gcode_state": "IDLE"}})
        return conn

    conn = asyncio.run(scenario())
    assert conn.report.print == {"command": "push_status", "msg": 0, "gcode_state": "IDLE"}
    assert conn._kept is False


def test_a_kept_report_expires_even_once_reconnected(lan, monkeypatch):
    """Reconnected but no full report within the grace: the old data isn't
    shown as live any longer, and a full report is asked for again."""
    monkeypatch.setattr(PrinterConnection, "MQTT_GRACE_SECONDS", 0.05)

    async def scenario():
        conn = _conn()
        await conn.connect()
        deliver(conn.client, FULL)
        _drop(conn.client)
        conn._retry_at = 0
        await conn.connect()
        deliver(conn.client, {"print": {"command": "push_status", "msg": 1, "mc_percent": 6}})
        await asyncio.sleep(0.06)
        state = await conn.read_state()
        conn.client._paho = RecordingPaho()
        await conn.ensure_full_report()
        return state, conn.client.paho.published

    state, published = asyncio.run(scenario())
    assert state.gcode_state == "UNKNOWN"  # not the RUNNING from before the drop
    assert [list(p) for _, p, _ in published] == [["pushing"]]


def test_a_command_while_reconnecting_says_so():
    from bamboozle.models import AppConfig
    from bamboozle.printer_manager import PrinterManager

    async def scenario():
        mgr = PrinterManager(AppConfig(), discovery_port=None)
        conn = mgr._new_connection(PrinterConfig(id="a", name="A1", ip="192.168.0.101",
                                                 access_code="x", serial="S"))
        mgr._connections["a"] = conn
        conn._kept = True
        return await mgr.execute_command("a", "pause")

    result = asyncio.run(scenario())
    assert result.success is False and "Reconnecting" in result.message
