"""One printer's slow read (or slow thumbnail download) must not freeze the
dashboard for the others, nor hold up shutdown.

Found live: the P1S's 3MF download took ~60 s inside the sequential poll loop,
and no printer got an update until it finished."""
import asyncio
import threading
import time

import pytest

from bamboozle import discovery, mqtt, thumbnail
from bamboozle.models import AppConfig, PrinterConfig
from bamboozle.printer_manager import PrinterConnection, PrinterManager
from fakes import FakeWs, deliver

STEP_TIMEOUT = 0.1
JOB = {"print": {"gcode_state": "RUNNING", "nozzle_temper": 210, "sdcard": True,
                 "gcode_file": "Benchy.3mf", "subtask_name": "Benchy"}}


@pytest.fixture
def lan(monkeypatch):
    monkeypatch.setattr(discovery, "tcp_reachable", lambda ip, port=8883, timeout=3.0: True)
    monkeypatch.setattr(mqtt.BambuClient, "start", lambda self: None)
    monkeypatch.setattr(mqtt.BambuClient, "is_connected", lambda self: True)
    monkeypatch.setattr(PrinterConnection, "MQTT_SETTLE_SECONDS", 0)
    monkeypatch.setattr(PrinterManager, "POLL_STEP_TIMEOUT_SECONDS", STEP_TIMEOUT)
    monkeypatch.setattr(PrinterManager, "PUSH_DEBOUNCE_SECONDS", 0.02)
    monkeypatch.setattr(mqtt, "publish", lambda printer, payload: True)


def _cfg(pid, ip):
    return PrinterConfig(id=pid, name=pid, ip=ip, access_code="x", serial=f"SERIAL{pid}")


def _report(conn, payload):
    deliver(conn.client, payload)


def _progress(updates, pid):
    return [m["printers"][pid]["progress"] for m in updates if pid in m["printers"]]


def _stuck_housekeeping(monkeypatch, release, stuck_id):
    """One printer's housekeeping step hangs (as a blocked publish could)."""
    real = PrinterConnection.check_silence
    calls = []

    async def check_silence(self):
        calls.append(self.cfg.id)
        if self.cfg.id == stuck_id:
            await asyncio.to_thread(release.wait, 5)
        return await real(self)

    monkeypatch.setattr(PrinterConnection, "check_silence", check_silence)
    return calls


def test_a_stuck_printer_does_not_stall_the_others(lan, monkeypatch):
    release = threading.Event()
    calls = _stuck_housekeeping(monkeypatch, release, "slow")

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
        fast = mgr.get_connection("fast")
        for pct in range(1, 9):
            _report(fast, {"print": {**JOB["print"], "mc_percent": pct}})
            await asyncio.sleep(0.1)
        await asyncio.sleep(0.4)
        release.set()
        await mgr.stop()
        return ws

    try:
        ws = asyncio.run(scenario())
    finally:
        release.set()
    updates = [m for m in ws.sent if m.get("type") == "update"]
    # Every report reached the dashboard, in order, while "slow" was stuck.
    assert _progress(updates, "fast")[-8:] == list(range(1, 9))
    # ...and the fast printer's housekeeping kept running (~0.12 s a round).
    assert calls.count("fast") >= 8


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
        conn = mgr.get_connection("p1s")
        _report(conn, JOB)
        await asyncio.sleep(0.1)
        for pct in range(1, 6):  # reports keep coming during the download
            _report(conn, {"print": {**JOB["print"], "mc_percent": pct}})
            await asyncio.sleep(0.1)
        during = [m for m in ws.sent if m.get("type") == "update"]
        release.set()
        await asyncio.sleep(0.5)
        after = mgr.get_all_states()["p1s"]
        last = [m for m in ws.sent if m.get("type") == "update"][-1]["printers"]["p1s"]
        await mgr.stop()
        return during, after, last

    try:
        during, after, last = asyncio.run(scenario())
    finally:
        release.set()
    assert _progress(during, "p1s")[-5:] == [1, 2, 3, 4, 5]
    assert not any(m["printers"]["p1s"]["has_thumbnail"] for m in during)
    assert last["has_thumbnail"] is True  # pushed when the download finished
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


def test_a_printer_removed_mid_poll_is_not_broadcast_again(lan, monkeypatch):
    release = threading.Event()
    _stuck_housekeeping(monkeypatch, release, "gone")

    async def scenario():
        mgr = PrinterManager(
            AppConfig(printers=[_cfg("gone", "192.168.0.50"), _cfg("kept", "192.168.0.51")],
                      poll_interval=0.02),
            discovery_port=None,
        )
        ws = FakeWs()
        mgr.register_ws(ws)
        await mgr.start()
        await asyncio.sleep(0.05)  # a poll round is waiting on "gone"
        _report(mgr.get_connection("gone"), JOB)  # and a push is pending for it
        await mgr.remove_printer("gone")
        removed_at = len(ws.sent)
        release.set()
        _report(mgr.get_connection("kept"), JOB)
        await asyncio.sleep(0.5)
        await mgr.stop()
        return ws.sent[removed_at:]

    try:
        after = asyncio.run(scenario())
    finally:
        release.set()
    # The dashboard merges updates, so one stale broadcast would bring the
    # removed printer back on screen until a reload.
    updates = [m for m in after if m.get("type") == "update"]
    assert updates
    assert not any("gone" in m["printers"] for m in updates)
