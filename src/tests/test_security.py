"""Abuse cases from the threat model (docs/superpowers/specs/
2026-10-25-replace-bambulabs-api-design.md §4)."""
import asyncio
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from bamboozle import main
from bamboozle.models import AppConfig, PrinterConfig
from bamboozle.printer_manager import CommandResult, PrinterManager
from bamboozle.routers import ws as ws_router

WS = "ws://127.0.0.1:8080/ws"
SRC = Path(__file__).resolve().parents[1]


@pytest.fixture
def client():
    """The real app, without its lifespan (no printers, no SSDP listener)."""
    main.app.state.manager = PrinterManager(AppConfig(), discovery_port=None)
    return TestClient(main.app, base_url="http://127.0.0.1:8080")


def test_the_dashboard_answers_on_localhost(client):
    assert client.get("/api/printers").status_code == 200
    for host in ("localhost:8080", "127.0.0.1"):
        assert client.get("/api/printers", headers={"host": host}).status_code == 200


@pytest.mark.parametrize("host", ["evil.example:8080", "evil.example", "192.168.0.5:8080"])
def test_a_dns_rebinding_page_is_refused(client, host):
    """A site whose name resolves to 127.0.0.1 is same-origin with us; its
    requests name its own host."""
    assert client.get("/api/printers", headers={"host": host}).status_code == 400
    assert client.post("/api/printers/x/stop", headers={"host": host}).status_code == 400
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(WS, headers={"host": host}):
            pass


@pytest.mark.parametrize("origin", ["http://evil.example", "null", "http://127.0.0.1:9999",
                                    "http://localhost:8080"])
def test_a_cross_origin_websocket_is_refused(client, origin):
    """Any page could otherwise open ws://127.0.0.1:8080/ws and send `stop`."""
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(WS, headers={"origin": origin}):
            pass


def test_the_dashboards_own_websocket_works(client):
    with client.websocket_connect(WS, headers={"origin": "http://127.0.0.1:8080"}) as ws:
        assert ws.receive_json()["type"] == "init"
    with client.websocket_connect(WS) as ws:  # no Origin: not a browser
        assert ws.receive_json()["type"] == "init"


@pytest.mark.parametrize("params", [{"level": "1; M112"}, {"level": "²"}, {"level": 7},
                                    {"level": True}, ["x"]])
def test_websocket_command_params_are_validated(client, params):
    """Untrusted params never reach the printer: nothing is published."""
    from fakes import RecordingPaho
    mgr = main.app.state.manager
    conn = mgr._new_connection(PrinterConfig(id="p1", name="P", ip="192.168.0.9",
                                             access_code="x", serial="S1"))
    conn.client._paho = RecordingPaho()
    conn._connected = True
    mgr._connections["p1"] = conn
    with client.websocket_connect(WS) as ws:
        ws.receive_json()
        ws.send_json({"type": "command", "printer_id": "p1", "action": "set_speed",
                      "params": params})
        result = ws.receive_json()
    assert result["type"] == "command_result" and result["success"] is False
    assert result["message"].startswith("Invalid")
    assert conn.client.paho.published == []


def test_a_dashboard_can_have_only_a_few_commands_waiting(client, monkeypatch):
    started = []

    async def slow_command(self, printer_id, action, params=None):
        started.append(action)
        await asyncio.sleep(0.5)
        return CommandResult(True, "done", True)

    monkeypatch.setattr(PrinterManager, "execute_command", slow_command)
    with client.websocket_connect(WS) as ws:
        ws.receive_json()
        for _ in range(ws_router.MAX_PENDING_COMMANDS + 1):
            ws.send_json({"type": "command", "printer_id": "p1", "action": "pause"})
        first = ws.receive_json()
    assert first["success"] is False and first["message"].startswith("Busy")
    assert len(started) <= ws_router.MAX_PENDING_COMMANDS


def test_a_failing_command_still_answers(client, monkeypatch):
    async def broken(self, printer_id, action, params=None):
        raise RuntimeError("bug")

    monkeypatch.setattr(PrinterManager, "execute_command", broken)
    with client.websocket_connect(WS) as ws:
        ws.receive_json()
        ws.send_json({"type": "command", "printer_id": "p1", "action": "pause"})
        result = ws.receive_json()
    assert result["success"] is False and "bug" in result["message"]


def test_nothing_imports_bambulabs_api():
    """The dependency is gone: every module imports with it blocked."""
    code = (
        "import sys, pkgutil, importlib\n"
        "sys.modules['bambulabs_api'] = None\n"
        "import bamboozle\n"
        "for m in pkgutil.walk_packages(bamboozle.__path__, 'bamboozle.'):\n"
        "    importlib.import_module(m.name)\n"
        "import tools.capture_reports\n"
    )
    subprocess.run([sys.executable, "-c", code], cwd=SRC, check=True, timeout=60)
    requirements = (SRC / "requirements.txt").read_text(encoding="utf-8").lower()
    assert "bambulabs" not in requirements
    assert "paho-mqtt" in requirements
