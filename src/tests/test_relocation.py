"""PrinterManager.handle_announcement: relocating printers whose IP changed."""
import asyncio
import json

import pytest

from bamboozle import discovery
from bamboozle.discovery import Announcement
from bamboozle.models import AppConfig, PrinterConfig
from bamboozle.printer_manager import PrinterConnection, PrinterManager
from fakes import FakeWs

SERIAL = "01P00C511100150"
OLD_IP = "192.168.0.51"
NEW_IP = "192.168.0.103"


@pytest.fixture
def network(monkeypatch):
    """Fake LAN: which IPs answer on 8883, and what cert CN each presents."""
    state = {"reachable": set(), "cert_cn": {}, "cert_probes": []}

    def tcp_reachable(ip, port=8883, timeout=3.0):
        return ip in state["reachable"]

    def fetch_cert_common_name(ip, port=8883, timeout=5.0):
        state["cert_probes"].append(ip)
        return state["cert_cn"].get(ip)

    monkeypatch.setattr(discovery, "tcp_reachable", tcp_reachable)
    monkeypatch.setattr(discovery, "fetch_cert_common_name", fetch_cert_common_name)
    return state


def _manager():
    cfg = PrinterConfig(id="p1s", name="P1S", ip=OLD_IP, access_code="x", serial=SERIAL)
    return PrinterManager(AppConfig(printers=[cfg]), discovery_port=None)


def _saved_config(appdata):
    return json.loads((appdata / "Bamboozle" / "config.json").read_text(encoding="utf-8"))


def test_offline_printer_is_relocated_saved_and_announced(network, isolated_appdata):
    network["cert_cn"][NEW_IP] = SERIAL

    async def scenario():
        mgr = _manager()
        await mgr.start()
        ws = FakeWs()
        mgr.register_ws(ws)
        await asyncio.sleep(0.05)  # initial connect to OLD_IP fails (unreachable)
        await mgr.handle_announcement(Announcement(ip=NEW_IP, serial=SERIAL, name="P1S"))
        conn_ip = mgr.get_connection("p1s").cfg.ip
        await mgr.stop()
        return mgr, ws, conn_ip

    mgr, ws, conn_ip = asyncio.run(scenario())

    assert conn_ip == NEW_IP
    assert mgr.config.printers[0].ip == NEW_IP
    assert _saved_config(isolated_appdata)["printers"][0]["ip"] == NEW_IP
    assert list((isolated_appdata / "Bamboozle" / "history").glob("P1S.*.json"))
    assert {"type": "notice", "level": "info", "message": "P1S moved to 192.168.0.103"} in ws.sent


def _run_announcement(ann, healthy=False, monkeypatch=None, repeat=1):
    async def scenario():
        mgr = _manager()
        await mgr.start()
        ws = FakeWs()
        mgr.register_ws(ws)
        await asyncio.sleep(0.05)
        for _ in range(repeat):
            await mgr.handle_announcement(ann)
        ip = mgr.get_connection("p1s").cfg.ip
        await mgr.stop()
        return mgr, ws, ip

    if healthy:
        monkeypatch.setattr(PrinterConnection, "is_offline", lambda self: False)
    return asyncio.run(scenario())


def _assert_unchanged(mgr, ws, ip, appdata):
    assert ip == OLD_IP
    assert mgr.config.printers[0].ip == OLD_IP
    assert not (appdata / "Bamboozle" / "config.json").exists()
    assert not [m for m in ws.sent if m.get("type") == "notice"]


def test_healthy_connection_is_never_switched(network, isolated_appdata, monkeypatch):
    network["cert_cn"][NEW_IP] = SERIAL
    mgr, ws, ip = _run_announcement(
        Announcement(ip=NEW_IP, serial=SERIAL), healthy=True, monkeypatch=monkeypatch)
    _assert_unchanged(mgr, ws, ip, isolated_appdata)
    assert network["cert_probes"] == []


def test_cert_serial_mismatch_is_ignored(network, isolated_appdata):
    network["cert_cn"][NEW_IP] = "22E8BJ630504999"  # some other printer
    mgr, ws, ip = _run_announcement(Announcement(ip=NEW_IP, serial=SERIAL))
    _assert_unchanged(mgr, ws, ip, isolated_appdata)


def test_no_cert_at_new_ip_is_ignored(network, isolated_appdata):
    mgr, ws, ip = _run_announcement(Announcement(ip=NEW_IP, serial=SERIAL))
    _assert_unchanged(mgr, ws, ip, isolated_appdata)


def test_rejected_ip_is_not_reprobed_on_every_announcement(network, isolated_appdata):
    network["cert_cn"][NEW_IP] = "22E8BJ630504999"
    _run_announcement(Announcement(ip=NEW_IP, serial=SERIAL), repeat=5)
    assert network["cert_probes"] == [NEW_IP]


def test_same_ip_announcement_does_nothing(network, isolated_appdata):
    mgr, ws, ip = _run_announcement(Announcement(ip=OLD_IP, serial=SERIAL))
    _assert_unchanged(mgr, ws, ip, isolated_appdata)
    assert network["cert_probes"] == []


def test_unknown_serial_is_ignored(network, isolated_appdata):
    network["cert_cn"][NEW_IP] = "03919D533005771"
    mgr, ws, ip = _run_announcement(Announcement(ip=NEW_IP, serial="03919D533005771"))
    _assert_unchanged(mgr, ws, ip, isolated_appdata)
    assert network["cert_probes"] == []


def test_printer_still_connecting_is_not_switched(network, isolated_appdata, monkeypatch):
    """A dual-interface printer may announce its other IP while we connect to the saved one."""
    import threading
    import time as _time

    network["cert_cn"][NEW_IP] = SERIAL
    release = threading.Event()

    def slow_reachable(ip, port=8883, timeout=3.0):
        release.wait(2)
        return False

    monkeypatch.setattr(discovery, "tcp_reachable", slow_reachable)

    async def scenario():
        mgr = _manager()
        await mgr.start()
        await asyncio.sleep(0.05)  # initial connect to OLD_IP still in progress
        await mgr.handle_announcement(Announcement(ip=NEW_IP, serial=SERIAL))
        ip = mgr.get_connection("p1s").cfg.ip
        release.set()
        await mgr.stop()
        return ip

    assert asyncio.run(scenario()) == OLD_IP
    assert network["cert_probes"] == []
