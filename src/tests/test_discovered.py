"""Discovered-printers cache feeding the Settings "Discover printers" list."""
import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from bamboozle import discovery, printer_manager
from bamboozle.discovery import Announcement, model_info
from bamboozle.models import AppConfig, PrinterConfig
from bamboozle.printer_manager import PrinterManager
from bamboozle.routers import api

CONFIGURED = "01P00C511100150"
A1 = Announcement(ip="192.168.0.102", serial="03919D533005771", model="N2S", name="Workshop A1")
H2C = Announcement(ip="192.168.0.110", serial="31B8BP611200123", model="O1C2", name="Bench H2C")


@pytest.fixture
def clock(monkeypatch):
    """Controllable time.monotonic for the manager module."""
    now = {"t": 1000.0}
    monkeypatch.setattr(printer_manager.time, "monotonic", lambda: now["t"])
    return now


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(discovery, "tcp_reachable", lambda ip, port=8883, timeout=3.0: False)


def _manager(*serials):
    printers = [
        PrinterConfig(id=f"p{i}", name=f"P{i}", ip=f"192.168.0.{50 + i}",
                      access_code="x", serial=s)
        for i, s in enumerate(serials)
    ]
    return PrinterManager(AppConfig(printers=printers), discovery_port=None)


# --- model table -------------------------------------------------------------

@pytest.mark.parametrize("code, name, port", [
    ("C12", "P1S", 6000),
    ("N2S", "A1", 6000),
    ("N1", "A1 mini", 6000),
    ("C11", "P1P", 6000),
    ("O1C2", "H2C", 322),
    ("N7", "P2S", 322),
    ("BL-P001", "X1C", 322),
])
def test_model_info_maps_known_codes(code, name, port):
    assert model_info(code) == (name, port)


def test_model_info_unknown_code_keeps_raw_code_and_no_port():
    assert model_info("ZZ9") == ("ZZ9", None)
    assert model_info("") == ("", None)


# --- manager cache -------------------------------------------------------------

def test_unconfigured_announcement_is_listed(clock):
    mgr = _manager(CONFIGURED)
    mgr._on_announcement(A1)
    clock["t"] += 2.5
    assert mgr.discovered_printers() == [{
        "ip": "192.168.0.102",
        "serial": "03919D533005771",
        "name": "Workshop A1",
        "model": "N2S",
        "model_name": "A1",
        "camera_port": 6000,
        "last_seen_s": 2.5,
    }]


def test_unknown_model_lists_raw_code_without_port(clock):
    mgr = _manager()
    mgr._on_announcement(Announcement(ip="192.168.0.9", serial="ABCDEF123456", model="ZZ9"))
    (entry,) = mgr.discovered_printers()
    assert entry["model_name"] == "ZZ9"
    assert entry["camera_port"] is None


def test_configured_serial_is_hidden_case_insensitively(clock):
    mgr = _manager(CONFIGURED.lower())
    mgr._on_announcement(Announcement(ip="192.168.0.51", serial=CONFIGURED, model="C12"))
    mgr._on_announcement(A1)
    assert [p["serial"] for p in mgr.discovered_printers()] == [A1.serial]


def test_filter_follows_config_changes_immediately(clock):
    async def scenario():
        mgr = _manager()
        await mgr.start()
        mgr._on_announcement(A1)
        before = mgr.discovered_printers()
        await mgr.add_printer(PrinterConfig(
            id="a1", name="A1", ip=A1.ip, access_code="x", serial=A1.serial))
        after_add = mgr.discovered_printers()
        await mgr.remove_printer("a1")
        after_remove = mgr.discovered_printers()
        await mgr.stop()
        return before, after_add, after_remove

    before, after_add, after_remove = asyncio.run(scenario())
    assert [p["serial"] for p in before] == [A1.serial]
    assert after_add == []
    assert [p["serial"] for p in after_remove] == [A1.serial]


def test_latest_announcement_wins_and_serial_key_is_case_insensitive(clock):
    mgr = _manager()
    mgr._on_announcement(A1)
    clock["t"] += 10
    mgr._on_announcement(Announcement(
        ip="192.168.0.200", serial=A1.serial.lower(), model="N2S", name="Renamed"))
    (entry,) = mgr.discovered_printers()
    assert (entry["ip"], entry["name"], entry["last_seen_s"]) == ("192.168.0.200", "Renamed", 0.0)


def test_entries_not_seen_for_five_minutes_are_dropped(clock):
    mgr = _manager()
    mgr._on_announcement(A1)
    clock["t"] += 200
    mgr._on_announcement(H2C)
    clock["t"] += 100.5  # A1 last seen 300.5 s ago, H2C 100.5 s ago
    assert [p["serial"] for p in mgr.discovered_printers()] == [H2C.serial]


def test_sorted_by_name(clock):
    mgr = _manager()
    mgr._on_announcement(A1)   # "Workshop A1"
    mgr._on_announcement(H2C)  # "Bench H2C"
    mgr._on_announcement(Announcement(ip="192.168.0.5", serial="ZZZZZZ111111", name="attic"))
    assert [p["name"] for p in mgr.discovered_printers()] == ["attic", "Bench H2C", "Workshop A1"]


def test_cache_is_bounded(clock):
    mgr = _manager()
    for i in range(PrinterManager.DISCOVERY_MAX_ENTRIES + 20):
        clock["t"] += 1
        mgr._on_announcement(Announcement(ip="192.168.1.1", serial=f"SERIAL{i:06d}"))
    listed = mgr.discovered_printers()
    assert len(listed) == PrinterManager.DISCOVERY_MAX_ENTRIES
    newest = f"SERIAL{PrinterManager.DISCOVERY_MAX_ENTRIES + 19:06d}"
    assert newest in {p["serial"] for p in listed}
    assert "SERIAL000000" not in {p["serial"] for p in listed}


# --- listening flag --------------------------------------------------------------

def test_not_listening_when_discovery_disabled():
    async def scenario():
        mgr = _manager()
        await mgr.start()
        listening = mgr.discovery_listening
        await mgr.stop()
        return listening

    assert asyncio.run(scenario()) is False


def test_listening_reflects_listener_start_result(monkeypatch):
    results = []

    async def scenario(ok):
        async def fake_start(self):
            return ok
        monkeypatch.setattr(discovery.SsdpListener, "start", fake_start)
        mgr = PrinterManager(AppConfig(), discovery_port=0)
        await mgr.start()
        results.append(mgr.discovery_listening)
        await mgr.stop()
        results.append(mgr.discovery_listening)

    asyncio.run(scenario(True))
    asyncio.run(scenario(False))
    assert results == [True, False, False, False]


# --- API -------------------------------------------------------------------------

def test_discovery_endpoint_shape(clock):
    mgr = _manager(CONFIGURED)
    mgr._listening = True
    mgr._on_announcement(A1)
    mgr._on_announcement(Announcement(ip="192.168.0.51", serial=CONFIGURED, model="C12"))

    app = FastAPI()
    app.include_router(api.router, prefix="/api")
    app.state.manager = mgr
    res = TestClient(app).get("/api/discovery")

    assert res.status_code == 200
    body = res.json()
    assert body["listening"] is True
    assert [p["serial"] for p in body["printers"]] == [A1.serial]
    assert set(body["printers"][0]) == {
        "ip", "serial", "name", "model", "model_name", "camera_port", "last_seen_s"}


def test_unnamed_printers_sort_by_their_serial(clock):
    """The UI shows the serial when there's no name, so sort on what's shown."""
    mgr = _manager()
    mgr._on_announcement(Announcement(ip="192.168.0.5", serial="ZZZZZZ111111"))
    mgr._on_announcement(A1)   # "Workshop A1"
    mgr._on_announcement(H2C)  # "Bench H2C"
    assert [p["serial"] for p in mgr.discovered_printers()] == [
        H2C.serial, A1.serial, "ZZZZZZ111111"]


def test_configured_serial_still_triggers_relocation_check(clock):
    async def scenario():
        mgr = _manager(CONFIGURED)
        await mgr.start()
        seen = []

        async def record(ann):
            seen.append(ann.serial)
        mgr.handle_announcement = record
        mgr._on_announcement(Announcement(ip="192.168.0.99", serial=CONFIGURED.lower()))
        mgr._on_announcement(A1)  # unconfigured: cached only
        await asyncio.sleep(0)
        await mgr.stop()
        return seen

    assert asyncio.run(scenario()) == [CONFIGURED.lower()]


def test_settings_sidebar_keeps_printer_fields_out_of_inline_js():
    """A network-supplied name like x');alert(1)// must not reach an onclick."""
    import html
    import re

    from bamboozle.routers import pages

    evil = "x');alert(1)//"
    mgr = PrinterManager(AppConfig(printers=[PrinterConfig(
        id="p1", name=evil, ip="192.168.0.5", access_code="c0de'1", serial="ABCDEF123456")]),
        discovery_port=None)
    app = FastAPI()
    app.include_router(pages.router)
    app.state.manager = mgr
    page = TestClient(app).get("/settings").text

    handlers = [html.unescape(h) for h in re.findall(r'onclick="([^"]*)"', page)]
    assert not any("alert" in h or "c0de" in h for h in handlers)
    names = [html.unescape(n) for n in re.findall(r'data-name="([^"]*)"', page)]
    assert names and set(names) == {evil}
