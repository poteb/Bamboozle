"""One printer's slow read (or slow thumbnail download) must not freeze the
dashboard for the others, nor hold up shutdown.

Found live: the P1S's 3MF download took ~60 s inside the sequential poll loop,
and no printer got an update until it finished."""
import asyncio
import threading
import time

import bambulabs_api as bl
import pytest

from bamboozle import discovery, mqtt, thumbnail
from bamboozle.models import AppConfig, PrinterConfig
from bamboozle.printer_manager import PrinterConnection, PrinterManager
from fakes import FakeWs

STEP_TIMEOUT = 0.1
JOB = {"print": {"gcode_state": "RUNNING", "nozzle_temper": 210, "sdcard": True,
                 "gcode_file": "Benchy.3mf", "subtask_name": "Benchy"}}


@pytest.fixture
def lan(monkeypatch):
    monkeypatch.setattr(discovery, "tcp_reachable", lambda ip, port=8883, timeout=3.0: True)
    monkeypatch.setattr(bl.Printer, "mqtt_start", lambda self: None)
    monkeypatch.setattr(bl.PrinterMQTTClient, "is_connected", lambda self: True)
    monkeypatch.setattr(PrinterConnection, "MQTT_SETTLE_SECONDS", 0)
    monkeypatch.setattr(PrinterManager, "POLL_STEP_TIMEOUT_SECONDS", STEP_TIMEOUT)
    monkeypatch.setattr(mqtt, "publish", lambda printer, payload: True)


def _cfg(pid, ip):
    return PrinterConfig(id=pid, name=pid, ip=ip, access_code="x", serial=f"SERIAL{pid}")


def _report(conn, payload):
    mc = conn.printer.mqtt_client
    mc.manual_update(payload)
    mc.on_message_handler(mc, None, None, None)


def _fresh_updates(ws, pid, since):
    """Broadcasts after `since` whose state for `pid` was read after `since`."""
    return [
        m for m in ws.sent
        if m.get("type") == "update" and m["printers"][pid]["timestamp"] > since
    ]


def test_a_slow_read_does_not_stall_the_other_printers(lan, monkeypatch):
    release = threading.Event()
    real_read = PrinterConnection._read_sync

    def read(self):
        if self.cfg.id == "slow":
            release.wait(5)
        return real_read(self)

    monkeypatch.setattr(PrinterConnection, "_read_sync", read)

    async def scenario():
        mgr = PrinterManager(
            AppConfig(printers=[_cfg("slow", "192.168.0.50"), _cfg("fast", "192.168.0.51")],
                      poll_interval=0.02),
            discovery_port=None,
        )
        ws = FakeWs()
        mgr.register_ws(ws)
        await mgr.start()
        await asyncio.sleep(0.05)  # connected
        since = time.time()
        await asyncio.sleep(0.8)
        release.set()
        await mgr.stop()
        return ws, since

    try:
        ws, since = asyncio.run(scenario())
    finally:
        release.set()
    fresh = _fresh_updates(ws, "fast", since)
    assert len(fresh) >= 3
    # The stuck printer is still listed, with its last known state.
    assert all("slow" in m["printers"] for m in fresh)


def test_a_slow_thumbnail_download_does_not_stall_polling(lan, monkeypatch):
    release = threading.Event()
    calls = []

    def fetch(ip, access_code, gcode_file, subtask_name="", **kw):
        calls.append((gcode_file, subtask_name))
        release.wait(5)
        return b"PNG"

    monkeypatch.setattr(thumbnail, "fetch_thumbnail", fetch)

    async def scenario():
        mgr = PrinterManager(
            AppConfig(printers=[_cfg("p1s", "192.168.0.50")], poll_interval=0.02),
            discovery_port=None,
        )
        ws = FakeWs()
        mgr.register_ws(ws)
        await mgr.start()
        await asyncio.sleep(0.05)
        _report(mgr.get_connection("p1s"), JOB)
        since = time.time()
        await asyncio.sleep(0.5)
        during = _fresh_updates(ws, "p1s", since)
        release.set()
        await asyncio.sleep(0.2)
        after = mgr.get_all_states()["p1s"]
        await mgr.stop()
        return during, after

    try:
        during, after = asyncio.run(scenario())
    finally:
        release.set()
    assert len(during) >= 5
    assert not any(m["printers"]["p1s"]["has_thumbnail"] for m in during)
    assert calls == [("Benchy.3mf", "Benchy")]  # one download, not one per poll
    assert after.has_thumbnail is True
    assert after.thumbnail_key == "Benchy"


def test_shutdown_does_not_wait_for_a_thumbnail_download(lan, monkeypatch):
    stuck = threading.Event()

    def fetch(ip, access_code, gcode_file, subtask_name="", **kw):
        stuck.wait(3)  # a download that ignores the cancel request
        return None

    monkeypatch.setattr(thumbnail, "fetch_thumbnail", fetch)

    async def scenario():
        mgr = PrinterManager(
            AppConfig(printers=[_cfg("p1s", "192.168.0.50")], poll_interval=0.02),
            discovery_port=None,
        )
        await mgr.start()
        await asyncio.sleep(0.05)
        _report(mgr.get_connection("p1s"), JOB)
        await asyncio.sleep(0.1)
        await mgr.stop()

    started = time.monotonic()
    try:
        asyncio.run(scenario())
        elapsed = time.monotonic() - started
    finally:
        stuck.set()
    assert elapsed < 1.5
