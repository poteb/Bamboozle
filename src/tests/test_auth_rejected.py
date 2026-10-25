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


def test_late_refusal_counts_as_offline_without_waiting_for_the_grace(lan, monkeypatch):
    """So the poll loop stops paho retrying the bad code at once."""
    monkeypatch.setattr(bl.Printer, "mqtt_start", lambda self: connack(self, SUCCESS))
    monkeypatch.setattr(PrinterConnection, "MQTT_GRACE_SECONDS", 60)
    conn = _conn()

    async def scenario():
        await conn.connect()
        connack(conn.printer, BAD_PASSWORD)
        return conn.is_offline()

    assert asyncio.run(scenario()) is True


def test_other_refusal_keeps_the_access_code_error(lan, monkeypatch):
    monkeypatch.setattr(bl.Printer, "mqtt_start", lambda self: connack(self, BAD_PASSWORD))
    conn = _conn()

    async def scenario():
        await conn.connect()
        connack(conn.printer, UNAVAILABLE)
        return await conn.read_state()

    assert asyncio.run(scenario()).last_error == protocol.AUTH_REJECTED


def test_retry_does_not_act_on_the_previous_attempts_refusal(lan, monkeypatch):
    """A slow CONNACK on the retry must not be cut short by the old refusal."""
    answers = [BAD_PASSWORD, None]  # retry: no CONNACK within the settle time
    stops = []

    def start(self):
        rc = answers.pop(0)
        if rc is not None:
            connack(self, rc)

    monkeypatch.setattr(bl.Printer, "mqtt_start", start)
    monkeypatch.setattr(bl.Printer, "mqtt_stop", lambda self: stops.append(1))
    monkeypatch.setattr(PrinterConnection, "MQTT_GRACE_SECONDS", 60)
    conn = _conn()

    async def scenario():
        assert await conn.connect() is False
        assert len(stops) == 1
        ok = await conn.connect()
        return ok, conn.is_offline(), await conn.read_state()

    ok, offline, state = asyncio.run(scenario())
    assert ok is True
    assert len(stops) == 1          # the retry's session was left to finish its login
    assert offline is False         # ...and isn't torn down by the poll loop either
    assert state.last_error == protocol.AUTH_REJECTED  # still the last thing we know


def test_retry_refused_for_another_reason_is_not_called_a_bad_code(lan, monkeypatch, caplog):
    answers = [BAD_PASSWORD, UNAVAILABLE]
    monkeypatch.setattr(bl.Printer, "mqtt_start", lambda self: connack(self, answers.pop(0)))
    monkeypatch.setattr(PrinterConnection, "MQTT_GRACE_SECONDS", 60)
    conn = _conn()

    async def scenario():
        await conn.connect()
        caplog.clear()
        return await conn.connect()

    with caplog.at_level(logging.INFO):
        ok = asyncio.run(scenario())
    assert ok is True  # left to paho / the grace period, like any other failure
    assert "access code wrong" not in caplog.text


def test_unreachable_printer_drops_the_access_code_error(lan, monkeypatch):
    """Switched off after a refusal: we know nothing about the code any more."""
    monkeypatch.setattr(bl.Printer, "mqtt_start", lambda self: connack(self, BAD_PASSWORD))
    conn = _conn()

    async def scenario():
        await conn.connect()
        monkeypatch.setattr(discovery, "tcp_reachable", lambda ip, port=8883, timeout=3.0: False)
        await conn.connect()
        return await conn.read_state()

    state = asyncio.run(scenario())
    assert state.online is False
    assert state.last_error == ""


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


def _answer_only_the_version_request(self):
    """A1 / P1S: the library's combined on-connect request gets no full status."""
    connack(self, SUCCESS)
    mc = self.mqtt_client
    mc.manual_update({"info": {"command": "get_version", "module": []}})
    mc.on_message_handler(mc, None, None, None)


def test_api_test_asks_for_a_pushall_when_no_status_came(quick_test_endpoint, monkeypatch):
    monkeypatch.setattr(bl.Printer, "mqtt_start", _answer_only_the_version_request)

    def pushall(mc):
        mc.manual_update({"print": {"gcode_state": "RUNNING"}})
        mc.on_message_handler(mc, None, None, None)
        return True

    monkeypatch.setattr(bl.PrinterMQTTClient, "pushall", pushall)
    result = asyncio.run(api_test_connection(_body()))
    assert result == {"success": True, "message": "Connected successfully. State: RUNNING"}


def test_api_test_succeeds_without_a_status(quick_test_endpoint, monkeypatch):
    """Reports on the serial's topic prove the login and serial; state is a bonus."""
    monkeypatch.setattr(bl.Printer, "mqtt_start", _answer_only_the_version_request)
    monkeypatch.setattr(bl.PrinterMQTTClient, "pushall", lambda mc: True)
    result = asyncio.run(api_test_connection(_body()))
    assert result == {"success": True, "message": "Connected successfully"}


def test_api_test_survives_a_failing_pushall(quick_test_endpoint, monkeypatch):
    """Login and serial are already proven; the state is only a bonus."""
    monkeypatch.setattr(bl.Printer, "mqtt_start", _answer_only_the_version_request)

    def pushall(mc):
        raise RuntimeError("Message publish failed: The connection was lost.")

    monkeypatch.setattr(bl.PrinterMQTTClient, "pushall", pushall)
    result = asyncio.run(api_test_connection(_body()))
    assert result == {"success": True, "message": "Connected successfully"}


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
