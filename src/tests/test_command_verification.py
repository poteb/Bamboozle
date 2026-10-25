"""Commands are confirmed by the printer's resulting state, not by the publish.

Only fakes: nothing here reaches a printer."""
import asyncio

import pytest

from bamboozle.models import AppConfig, PrinterConfig
from bamboozle.printer_manager import CommandResult, PrinterConnection, PrinterManager
from fakes import FakeWs, connected, deliver, published

CONFIRM = 0.2
H2C_FUN = "14023FF1AFFF9CB7"  # Developer Mode off


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    monkeypatch.setattr(PrinterConnection, "COMMAND_CONFIRM_SECONDS", CONFIRM)
    monkeypatch.setattr(PrinterManager, "PUSH_DEBOUNCE_SECONDS", 0.01)


def _setup(print_payload, pid="p1"):
    conn = PrinterConnection(PrinterConfig(id=pid, name="Printer", ip="192.168.0.9",
                                           access_code="x", serial="SERIAL1"))
    connected(conn, print_payload)
    # Firmware known, so state reads don't ask for it (keeps `published` clean).
    conn.report.info = {"command": "get_version", "module": [{"name": "ota", "sw_ver": "01.00.00.00"}]}
    mgr = PrinterManager(AppConfig(), discovery_port=None)
    conn.on_change = lambda: mgr._mark_dirty(pid, conn)
    mgr._connections[pid] = conn
    return mgr, conn


def _status(**fields):
    return {"print": {"command": "push_status", "msg": 1, **fields}}


async def _later(seconds, fn, *args):
    await asyncio.sleep(seconds)
    fn(*args)


def test_pause_is_confirmed_when_the_printer_reports_pause():
    async def scenario():
        mgr, conn = _setup({"gcode_state": "RUNNING"})
        asyncio.get_running_loop().create_task(
            _later(0.05, deliver, conn.client, _status(gcode_state="PAUSE")))
        return await mgr.execute_command("p1", "pause"), published(conn)

    result, sent = asyncio.run(scenario())
    assert result == CommandResult(True, "Print paused", True)
    assert [p["print"]["command"] for p in sent] == ["pause"]


def test_no_state_change_reads_not_confirmed_never_failed():
    async def scenario():
        mgr, conn = _setup({"gcode_state": "RUNNING"})
        return await mgr.execute_command("p1", "pause"), published(conn)

    result, sent = asyncio.run(scenario())
    assert result.success is True and result.confirmed is False
    assert "not confirmed" in result.message and "Developer Mode" not in result.message
    # Halfway through, one standalone pushall asks for the full state.
    assert [list(p) for p in sent] == [["print"], ["pushing"]]
    assert sent[1]["pushing"]["command"] == "pushall"


def test_developer_mode_off_is_mentioned_for_print_controls_only():
    async def scenario():
        mgr, conn = _setup({"gcode_state": "RUNNING", "fun": H2C_FUN,
                            "lights_report": [{"node": "chamber_light", "mode": "off"}]})
        await conn.read_state()
        return (await mgr.execute_command("p1", "stop"),
                await mgr.execute_command("p1", "light_on"))

    stop, light = asyncio.run(scenario())
    assert stop.success is True and stop.confirmed is False
    assert "Developer Mode is off" in stop.message
    assert light.success is True and light.confirmed is False
    assert "Developer Mode" not in light.message


def test_light_confirmed_by_the_full_report_the_mid_wait_pushall_brings():
    """An A1 / P1S delta may not carry lights_report; the pushall reply does."""
    async def scenario():
        mgr, conn = _setup({"gcode_state": "IDLE",
                            "lights_report": [{"node": "chamber_light", "mode": "on"}]})
        loop = asyncio.get_running_loop()
        real_publish = conn.client.paho.publish
        reply = {"print": {"command": "push_status", "msg": 0, "gcode_state": "IDLE",
                           "lights_report": [{"node": "chamber_light", "mode": "off"}]}}

        def publish(topic, payload, qos=0):
            info = real_publish(topic, payload, qos)
            if "pushall" in payload:
                loop.call_soon_threadsafe(deliver, conn.client, reply)
            return info

        conn.client.paho.publish = publish
        return await mgr.execute_command("p1", "light_off")

    assert asyncio.run(scenario()) == CommandResult(True, "Light turned off", True)


def test_a_failed_echo_is_a_refusal_when_the_state_never_confirms():
    async def scenario():
        mgr, conn = _setup({"gcode_state": "RUNNING"})
        task = asyncio.create_task(mgr.execute_command("p1", "pause"))
        await asyncio.sleep(0.02)
        seq = published(conn)[0]["print"]["sequence_id"]
        deliver(conn.client, {"print": {"command": "pause", "sequence_id": seq,
                                        "result": "FAILED", "reason": "verify failed"}})
        return await task

    result = asyncio.run(scenario())
    assert result == CommandResult(False, "Printer refused pause: verify failed", False)


def test_a_failed_echo_does_not_overrule_the_state():
    async def scenario():
        mgr, conn = _setup({"gcode_state": "RUNNING"})
        task = asyncio.create_task(mgr.execute_command("p1", "pause"))
        await asyncio.sleep(0.02)
        seq = published(conn)[0]["print"]["sequence_id"]
        deliver(conn.client, {"print": {"command": "pause", "sequence_id": seq, "result": "failed"}})
        deliver(conn.client, _status(gcode_state="PAUSE"))
        return await task

    assert asyncio.run(scenario()).confirmed is True


@pytest.mark.parametrize("echo", [
    {"command": "pause", "sequence_id": "1", "result": "failed"},  # another client's id
    {"command": "stop", "result": "failed"},  # another command, our id
])
def test_someone_elses_failed_echo_is_ignored(echo):
    async def scenario():
        mgr, conn = _setup({"gcode_state": "RUNNING"})
        task = asyncio.create_task(mgr.execute_command("p1", "pause"))
        await asyncio.sleep(0.02)
        echo.setdefault("sequence_id", published(conn)[0]["print"]["sequence_id"])
        deliver(conn.client, {"print": echo})
        return await task

    result = asyncio.run(scenario())
    assert result.success is True and result.confirmed is False


def test_the_light_refusal_counts_only_for_the_chamber_light_request():
    """Printers without a second LED node may refuse the chamber_light2 one."""
    async def scenario():
        mgr, conn = _setup({"lights_report": [{"node": "chamber_light", "mode": "off"}]})
        task = asyncio.create_task(mgr.execute_command("p1", "light_on"))
        await asyncio.sleep(0.02)
        second = published(conn)[1]["system"]["sequence_id"]
        deliver(conn.client, {"system": {"command": "ledctrl", "sequence_id": second,
                                         "result": "failed"}})
        return await task

    result = asyncio.run(scenario())
    assert result.success is True and result.confirmed is False


def test_already_in_the_requested_state_is_confirmed_but_still_sent():
    async def scenario():
        mgr, conn = _setup({"gcode_state": "PAUSE"})
        return await mgr.execute_command("p1", "pause"), published(conn)

    result, sent = asyncio.run(scenario())
    assert result == CommandResult(True, "Print paused", True)
    assert [p["print"]["command"] for p in sent] == ["pause"]


def test_refresh_is_confirmed_by_a_full_report():
    async def scenario():
        mgr, conn = _setup({"gcode_state": "IDLE"})
        full = {"print": {"command": "push_status", "msg": 0, "gcode_state": "IDLE"}}
        asyncio.get_running_loop().create_task(_later(0.05, deliver, conn.client, full))
        return await mgr.execute_command("p1", "refresh"), published(conn)

    result, sent = asyncio.run(scenario())
    assert result.confirmed is True
    assert len(sent) == 1  # no extra pushall for a refresh


def test_nothing_is_sent_while_the_session_is_down():
    async def scenario():
        mgr, conn = _setup({"gcode_state": "RUNNING"})
        conn.client.paho.connected = False
        return await mgr.execute_command("p1", "pause"), published(conn)

    result, sent = asyncio.run(scenario())
    assert result == CommandResult(False, "Not sent: printer not connected")
    assert sent == []


@pytest.mark.parametrize("action,params", [("set_speed", {"level": 9}), ("set_light", {"mode": "x"}),
                                           ("gcode_line", {}), ("set_speed", "2")])
def test_invalid_commands_send_nothing(action, params):
    async def scenario():
        mgr, conn = _setup({"gcode_state": "RUNNING"})
        return await mgr.execute_command("p1", action, params), published(conn)

    result, sent = asyncio.run(scenario())
    assert result.success is False and result.confirmed is None
    assert sent == []


def test_unknown_or_disconnected_printer():
    async def scenario():
        mgr, conn = _setup({"gcode_state": "RUNNING"})
        missing = await mgr.execute_command("nope", "pause")
        conn._connected = False
        down = await mgr.execute_command("p1", "pause")
        return missing, down

    missing, down = asyncio.run(scenario())
    assert missing == CommandResult(False, "Printer not found")
    assert down == CommandResult(False, "Printer not connected")


def test_a_confirmed_command_updates_the_card_before_answering():
    async def scenario():
        mgr, conn = _setup({"gcode_state": "RUNNING"})
        ws = FakeWs()
        mgr.register_ws(ws)
        await conn.read_state()
        await mgr._broadcast_changed(["p1"])
        ws.sent.clear()
        asyncio.get_running_loop().create_task(
            _later(0.02, deliver, conn.client, _status(gcode_state="PAUSE")))
        result = await mgr.execute_command("p1", "pause")
        return result, list(ws.sent)

    result, sent = asyncio.run(scenario())
    assert result.confirmed is True
    assert sent and sent[-1]["printers"]["p1"]["gcode_state"] == "PAUSE"
