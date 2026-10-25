"""A connected session that stops delivering reports counts as down
(the P1-series broker that goes silent with the socket still up)."""
import asyncio
import logging

import bambulabs_api as bl
import pytest

from bamboozle import discovery, mqtt, protocol
from bamboozle.models import AppConfig, PrinterConfig
from bamboozle.printer_manager import PrinterConnection, PrinterManager

STALE = 0.2
PROBE = 0.1


@pytest.fixture
def lan(monkeypatch):
    """Reachable printer whose MQTT socket is up, but that sends nothing unless told."""
    monkeypatch.setattr(discovery, "tcp_reachable", lambda ip, port=8883, timeout=3.0: True)
    monkeypatch.setattr(bl.Printer, "mqtt_start", lambda self: None)
    monkeypatch.setattr(bl.PrinterMQTTClient, "is_connected", lambda self: True)
    monkeypatch.setattr(PrinterConnection, "MQTT_SETTLE_SECONDS", 0)
    monkeypatch.setattr(PrinterConnection, "STALE_SECONDS", STALE)
    monkeypatch.setattr(PrinterConnection, "PROBE_SECONDS", PROBE)
    published = []
    monkeypatch.setattr(mqtt, "publish", lambda printer, payload: published.append(payload) or True)
    return published


def report(conn, payload=None):
    """Deliver a report the way the library's paho callback does."""
    mc = conn.printer.mqtt_client
    mc.manual_update(payload or {"print": {"gcode_state": "IDLE", "nozzle_temper": 25}})
    mc.on_message_handler(mc, None, None, None)


def _conn():
    return PrinterConnection(
        PrinterConfig(name="P1S", ip="192.168.0.103", access_code="x", serial="01P00C511100150"))


def test_reports_keep_a_session_up(lan):
    async def scenario():
        conn = _conn()
        await conn.connect()
        for _ in range(4):
            await asyncio.sleep(STALE / 2)
            report(conn)
        return conn.is_offline()

    assert asyncio.run(scenario()) is False


def test_silent_session_counts_as_offline_though_the_socket_is_up(lan):
    async def scenario():
        conn = _conn()
        await conn.connect()
        report(conn)
        assert conn.is_offline() is False
        await asyncio.sleep(STALE + 0.05)
        return conn.is_offline()

    assert asyncio.run(scenario()) is True


def test_silence_reads_as_stale(lan, caplog):
    async def scenario():
        conn = _conn()
        await conn.connect()
        report(conn)
        await asyncio.sleep(STALE + 0.05)
        await conn.check_silence()
        await conn.reset_session()
        return await conn.read_state()

    with caplog.at_level(logging.WARNING):
        state = asyncio.run(scenario())
    assert state.online is False
    assert state.last_error == protocol.STALE
    assert "no report" in caplog.text.lower()


def test_zero_reports_since_connect_hints_at_the_serial(lan, caplog):
    async def scenario():
        conn = _conn()
        await conn.connect()
        await asyncio.sleep(STALE + 0.05)
        await conn.check_silence()
        await conn.check_silence()  # hint is logged once, not every poll
        offline = conn.is_offline()
        await conn.reset_session()
        return offline, await conn.read_state()

    with caplog.at_level(logging.WARNING):
        offline, state = asyncio.run(scenario())
    assert offline is True
    assert state.last_error == protocol.NO_REPORTS
    assert caplog.text.lower().count("check the serial") == 1


def test_the_hint_stays_through_the_next_attempt_until_a_report_arrives(lan):
    async def scenario():
        conn = _conn()
        await conn.connect()
        await asyncio.sleep(STALE + 0.05)
        await conn.check_silence()
        await conn.reset_session()
        await conn.connect()  # still nothing on the topic
        during = (await conn.read_state()).last_error
        report(conn)
        after = (await conn.read_state()).last_error
        return during, after

    during, after = asyncio.run(scenario())
    assert during == protocol.NO_REPORTS
    assert after == ""


def test_unreachable_printer_drops_the_hint(lan, monkeypatch):
    async def scenario():
        conn = _conn()
        await conn.connect()
        await asyncio.sleep(STALE + 0.05)
        await conn.check_silence()
        await conn.reset_session()
        monkeypatch.setattr(discovery, "tcp_reachable", lambda ip, port=8883, timeout=3.0: False)
        await conn.connect()
        return await conn.read_state()

    assert asyncio.run(scenario()).last_error == ""


def test_a_quiet_session_is_asked_for_a_full_report_once(lan):
    """An idle printer may push rarely; one pushall before calling it stale."""
    published = lan

    async def scenario():
        conn = _conn()
        await conn.connect()
        report(conn)
        await conn.check_silence()
        assert published == []  # not quiet yet
        await asyncio.sleep(PROBE + 0.03)
        await conn.check_silence()
        await conn.check_silence()
        assert len(published) == 1
        report(conn)  # it answered; the next quiet spell may probe again
        await asyncio.sleep(PROBE + 0.03)
        await conn.check_silence()
        return conn.is_offline()

    assert asyncio.run(scenario()) is False
    assert len(published) == 2
    assert all(p == {"pushing": {"command": "pushall", "sequence_id": p["pushing"]["sequence_id"]}}
               for p in published)


def test_silent_session_does_not_reset_the_reconnect_backoff(lan):
    async def scenario():
        conn = _conn()
        conn._backoff = 24.0
        await conn.connect()
        conn.is_offline()
        silent = conn._backoff
        report(conn)
        conn.is_offline()
        return silent, conn._backoff

    silent, verified = asyncio.run(scenario())
    assert silent == 24.0
    assert verified == PrinterConnection.INITIAL_BACKOFF_SECONDS


def test_poll_loop_reconnects_a_silent_printer(lan, monkeypatch):
    monkeypatch.setattr(PrinterConnection, "INITIAL_BACKOFF_SECONDS", 0.01)
    starts = []
    monkeypatch.setattr(bl.Printer, "mqtt_start", lambda self: starts.append(self))

    async def scenario():
        cfg = PrinterConfig(id="p1s", name="P1S", ip="192.168.0.103", access_code="x",
                            serial="01P00C511100150")
        mgr = PrinterManager(AppConfig(printers=[cfg], poll_interval=0.02), discovery_port=None)
        await mgr.start()
        await asyncio.sleep(STALE * 2 + 0.2)
        state = mgr.get_all_states()["p1s"]
        await mgr.stop()
        return state

    state = asyncio.run(scenario())
    assert len(starts) >= 2
    assert starts[0] is not starts[1]  # a fresh client each time
    assert state.last_error == protocol.NO_REPORTS
