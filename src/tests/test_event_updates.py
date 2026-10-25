"""Dashboard updates are pushed when reports arrive: debounced, deduped,
batched, with a heartbeat, and never held up by a slow dashboard."""
import asyncio

import pytest

from bamboozle.models import AppConfig, PrinterConfig
from bamboozle.printer_manager import PrinterConnection, PrinterManager
from fakes import FakeWs, connected, deliver

DEBOUNCE = 0.05


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    monkeypatch.setattr(PrinterManager, "PUSH_DEBOUNCE_SECONDS", DEBOUNCE)


def _manager(*pids):
    mgr = PrinterManager(AppConfig(), discovery_port=None)
    for pid in pids:
        conn = mgr._new_connection(PrinterConfig(id=pid, name=pid, ip="192.168.0.9",
                                                 access_code="x", serial=f"S{pid}"))
        connected(conn)
        conn.report.info = {"module": [{"name": "ota", "sw_ver": "01.00.00.00"}]}
        mgr._connections[pid] = conn
    ws = FakeWs()
    mgr.register_ws(ws)
    return mgr, ws


def _report(mgr, pid, **fields):
    deliver(mgr.get_connection(pid).client,
            {"print": {"command": "push_status", "msg": 1, "gcode_state": "RUNNING", **fields}})


def _updates(ws):
    return [m for m in ws.sent if m.get("type") == "update"]


def test_a_report_is_pushed_within_the_debounce():
    async def scenario():
        mgr, ws = _manager("a")
        _report(mgr, "a", mc_percent=5)
        assert _updates(ws) == []
        await asyncio.sleep(DEBOUNCE * 3)
        return _updates(ws)

    [update] = asyncio.run(scenario())
    assert update["printers"]["a"]["progress"] == 5
    assert update["printers"]["a"]["online"] is True


def test_an_unchanged_state_is_not_pushed_again():
    async def scenario():
        mgr, ws = _manager("a")
        _report(mgr, "a", mc_percent=5, nozzle_temper=200)
        await asyncio.sleep(DEBOUNCE * 3)
        _report(mgr, "a", mc_percent=5, nozzle_temper=200)  # H2C/P2S: same full state each second
        await asyncio.sleep(DEBOUNCE * 3)
        _report(mgr, "a", mc_percent=6)
        await asyncio.sleep(DEBOUNCE * 3)
        return _updates(ws)

    updates = asyncio.run(scenario())
    assert [u["printers"]["a"]["progress"] for u in updates] == [5, 6]


def test_a_burst_of_reports_is_one_update_with_the_latest_state():
    async def scenario():
        mgr, ws = _manager("a")
        for pct in range(10):
            _report(mgr, "a", mc_percent=pct)
        await asyncio.sleep(DEBOUNCE * 3)
        return _updates(ws)

    [update] = asyncio.run(scenario())
    assert update["printers"]["a"]["progress"] == 9


def test_printers_changing_together_share_one_update():
    async def scenario():
        mgr, ws = _manager("a", "b", "c")
        _report(mgr, "a", mc_percent=1)
        _report(mgr, "b", mc_percent=2)
        await asyncio.sleep(DEBOUNCE * 3)
        return _updates(ws)

    [update] = asyncio.run(scenario())
    assert set(update["printers"]) == {"a", "b"}  # c didn't change


def test_steady_reports_are_not_starved_by_the_debounce():
    """A throttle: a printer reporting faster than the debounce still updates."""
    async def scenario():
        mgr, ws = _manager("a")
        for pct in range(12):
            _report(mgr, "a", mc_percent=pct)
            await asyncio.sleep(DEBOUNCE / 2)
        await asyncio.sleep(DEBOUNCE * 3)
        return _updates(ws)

    updates = asyncio.run(scenario())
    assert len(updates) >= 4
    assert updates[-1]["printers"]["a"]["progress"] == 11


def test_the_heartbeat_sends_every_printer(monkeypatch):
    monkeypatch.setattr(PrinterManager, "HEARTBEAT_SECONDS", 0)

    async def scenario():
        mgr, ws = _manager("a", "b")
        await mgr._broadcast_changed(["a", "b"])
        ws.sent.clear()
        await mgr._broadcast_changed(["a", "b"])  # nothing changed
        quiet = list(ws.sent)
        await mgr._broadcast_changed(["a", "b"], force=True)
        return quiet, _updates(ws)

    quiet, [beat] = asyncio.run(scenario())
    assert quiet == []
    assert set(beat["printers"]) == {"a", "b"}


class StuckWs(FakeWs):
    def __init__(self):
        super().__init__()
        self.closed = False

    async def send_json(self, msg):
        await asyncio.sleep(10)

    async def close(self, code=1000):
        self.closed = True


def test_a_dashboard_that_cannot_keep_up_is_dropped(monkeypatch):
    monkeypatch.setattr(PrinterManager, "WS_SEND_TIMEOUT_SECONDS", 0.05)

    async def scenario():
        mgr, ws = _manager("a")
        stuck = StuckWs()
        mgr.register_ws(stuck)
        _report(mgr, "a", mc_percent=1)
        await asyncio.sleep(DEBOUNCE + 0.2)
        _report(mgr, "a", mc_percent=2)
        await asyncio.sleep(DEBOUNCE * 3)
        return mgr, ws, stuck

    mgr, ws, stuck = asyncio.run(scenario())
    assert [u["printers"]["a"]["progress"] for u in _updates(ws)] == [1, 2]
    assert stuck not in mgr._ws_clients and stuck.closed


def test_a_replaced_connection_does_not_push():
    async def scenario():
        mgr, ws = _manager("a")
        old = mgr.get_connection("a")
        replacement = mgr._new_connection(old.cfg.model_copy())
        mgr._connections["a"] = replacement
        old.on_change()  # a late report on the old connection
        await asyncio.sleep(DEBOUNCE * 3)
        return _updates(ws)

    assert asyncio.run(scenario()) == []


def test_shutdown_cancels_a_pending_push():
    async def scenario():
        mgr, ws = _manager("a")
        _report(mgr, "a", mc_percent=1)
        await mgr.stop()
        await asyncio.sleep(DEBOUNCE * 3)
        return _updates(ws)

    assert asyncio.run(scenario()) == []


def test_reports_after_shutdown_push_nothing():
    async def scenario():
        mgr, ws = _manager("a")
        await mgr.stop()
        mgr.get_connection("a").on_change()  # a late callback
        await asyncio.sleep(DEBOUNCE * 3)
        return _updates(ws), mgr._push_handle

    updates, handle = asyncio.run(scenario())
    assert updates == [] and handle is None
