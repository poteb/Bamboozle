"""A refused MQTT login (CONNACK 134/135) reads as "access code wrong or changed"."""
import asyncio
import logging

import bambulabs_api as bl
import pytest
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.reasoncodes import ReasonCode

from bamboozle import discovery, protocol
from bamboozle.models import PrinterAddRequest, PrinterConfig
from bamboozle.printer_manager import PrinterConnection
from bamboozle.routers import api
from bamboozle.routers.api import test_connection as api_test_connection

BAD_PASSWORD = ReasonCode(PacketTypes.CONNACK, "Bad user name or password")  # 134
NOT_AUTHORIZED = ReasonCode(PacketTypes.CONNACK, "Not authorized")            # 135
SUCCESS = ReasonCode(PacketTypes.CONNACK, "Success")
UNAVAILABLE = ReasonCode(PacketTypes.CONNACK, "Server unavailable")


def connack(printer, rc):
    """Deliver a CONNACK to the library client the way paho's thread would."""
    mc = printer.mqtt_client
    mc.on_connect_handler(mc, None, None, None, rc, None)


@pytest.fixture
def lan(monkeypatch):
    monkeypatch.setattr(discovery, "tcp_reachable", lambda ip, port=8883, timeout=3.0: True)
    monkeypatch.setattr(bl.Printer, "mqtt_stop", lambda self: None)
    monkeypatch.setattr(PrinterConnection, "MQTT_SETTLE_SECONDS", 0)


def _conn():
    return PrinterConnection(
        PrinterConfig(name="P1S", ip="192.168.0.103", access_code="wrong", serial="01P00C511100150"))


def test_reason_codes():
    assert BAD_PASSWORD.value == 134 and NOT_AUTHORIZED.value == 135
    assert protocol.is_auth_refusal(BAD_PASSWORD)
    assert protocol.is_auth_refusal(NOT_AUTHORIZED)
    assert not protocol.is_auth_refusal(SUCCESS)
    assert not protocol.is_auth_refusal(UNAVAILABLE)


@pytest.mark.parametrize("rc", [BAD_PASSWORD, NOT_AUTHORIZED])
def test_connect_reports_refused_access_code(lan, monkeypatch, caplog, rc):
    monkeypatch.setattr(bl.Printer, "mqtt_start", lambda self: connack(self, rc))
    conn = _conn()

    async def scenario():
        ok = await conn.connect()
        return ok, await conn.read_state()

    with caplog.at_level(logging.INFO):
        ok, state = asyncio.run(scenario())

    assert ok is False
    assert state.online is False
    assert state.last_error == protocol.AUTH_REJECTED
    assert "access code wrong or changed" in caplog.text
    assert "Connected to printer" not in caplog.text
    assert "Printer Values Not Available Yet" not in caplog.text
    assert conn.needs_reconnect() is False  # backs off before retrying


def test_late_refusal_on_reconnect_is_reported(lan, monkeypatch, caplog):
    """paho reconnects by itself; a refusal after the session started still shows."""
    monkeypatch.setattr(bl.Printer, "mqtt_start", lambda self: connack(self, SUCCESS))
    conn = _conn()

    async def scenario():
        assert await conn.connect() is True
        connack(conn.printer, BAD_PASSWORD)  # code changed on the printer
        return await conn.read_state()

    with caplog.at_level(logging.INFO):
        state = asyncio.run(scenario())

    assert state.online is False
    assert state.last_error == protocol.AUTH_REJECTED
    assert "access code wrong or changed" in caplog.text
    assert "Printer Values Not Available Yet" not in caplog.text


def test_accepted_login_clears_the_error(lan, monkeypatch):
    monkeypatch.setattr(bl.Printer, "mqtt_start", lambda self: connack(self, BAD_PASSWORD))
    conn = _conn()

    async def scenario():
        await conn.connect()
        connack(conn.printer, SUCCESS)
        conn.printer.mqtt_client.manual_update({"print": {"gcode_state": "IDLE", "nozzle_temper": 25}})
        conn._connected = True
        return await conn.read_state()

    state = asyncio.run(scenario())
    assert state.last_error == ""
    assert state.online is True


# --- POST /api/printers/test ---------------------------------------------------


def _body():
    return PrinterAddRequest(name="P1S", ip="192.168.0.103", access_code="wrong", serial="01P00C511100150")


@pytest.fixture
def quick_test_endpoint(lan, monkeypatch):
    monkeypatch.setattr(api, "TEST_CONNACK_TIMEOUT", 0.2)
    monkeypatch.setattr(api, "TEST_REPORT_TIMEOUT", 0.2)


@pytest.mark.parametrize("rc", [BAD_PASSWORD, NOT_AUTHORIZED])
def test_api_test_reports_refused_access_code(quick_test_endpoint, monkeypatch, rc):
    monkeypatch.setattr(bl.Printer, "mqtt_start", lambda self: connack(self, rc))
    result = asyncio.run(api_test_connection(_body()))
    assert result["success"] is False
    assert "access code wrong or changed" in result["message"].lower()


def test_api_test_reports_other_refusal(quick_test_endpoint, monkeypatch):
    monkeypatch.setattr(bl.Printer, "mqtt_start", lambda self: connack(self, UNAVAILABLE))
    result = asyncio.run(api_test_connection(_body()))
    assert result["success"] is False
    assert "Server unavailable" in result["message"]


def test_api_test_reports_no_connack(quick_test_endpoint, monkeypatch):
    monkeypatch.setattr(bl.Printer, "mqtt_start", lambda self: None)
    result = asyncio.run(api_test_connection(_body()))
    assert result["success"] is False
    assert "timed out" in result["message"]


def test_api_test_reports_no_data(quick_test_endpoint, monkeypatch):
    monkeypatch.setattr(bl.Printer, "mqtt_start", lambda self: connack(self, SUCCESS))
    result = asyncio.run(api_test_connection(_body()))
    assert result["success"] is False
    assert "serial" in result["message"]


def test_api_test_success(quick_test_endpoint, monkeypatch, caplog):
    def start(self):
        connack(self, SUCCESS)
        mc = self.mqtt_client
        mc.manual_update({"print": {"gcode_state": "IDLE"}})
        mc.on_message_handler(mc, None, None, None)

    monkeypatch.setattr(bl.Printer, "mqtt_start", start)
    with caplog.at_level(logging.INFO):
        result = asyncio.run(api_test_connection(_body()))
    assert result["success"] is True
    assert "IDLE" in result["message"]
    assert "Printer Values Not Available Yet" not in caplog.text
