"""A refused MQTT login (CONNACK 134/135) reads as "access code wrong or changed"."""
import asyncio
import logging
import threading

import pytest
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.reasoncodes import ReasonCode

from bamboozle import discovery, mqtt, protocol
from bamboozle.models import PrinterAddRequest, PrinterConfig
from bamboozle.printer_manager import PrinterConnection
from bamboozle.routers import api
from bamboozle.routers.api import test_connection as api_test_connection
from fakes import connack, deliver

BAD_PASSWORD = ReasonCode(PacketTypes.CONNACK, "Bad user name or password")  # 134
NOT_AUTHORIZED = ReasonCode(PacketTypes.CONNACK, "Not authorized")            # 135
SUCCESS = ReasonCode(PacketTypes.CONNACK, "Success")
UNAVAILABLE = ReasonCode(PacketTypes.CONNACK, "Server unavailable")


@pytest.fixture
def lan(monkeypatch):
    monkeypatch.setattr(discovery, "tcp_reachable", lambda ip, port=8883, timeout=3.0: True)
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
    monkeypatch.setattr(mqtt.BambuClient, "start", lambda self: connack(self, rc))
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
    monkeypatch.setattr(mqtt.BambuClient, "start", lambda self: connack(self, SUCCESS))
    conn = _conn()

    async def scenario():
        assert await conn.connect() is True
        connack(conn.client, BAD_PASSWORD)  # code changed on the printer
        return await conn.read_state()

    with caplog.at_level(logging.INFO):
        state = asyncio.run(scenario())

    assert state.online is False
    assert state.last_error == protocol.AUTH_REJECTED
    assert "access code wrong or changed" in caplog.text
    assert "Printer Values Not Available Yet" not in caplog.text


def test_late_refusal_counts_as_offline_without_waiting_for_the_grace(lan, monkeypatch):
    """So the poll loop stops paho retrying the bad code at once."""
    monkeypatch.setattr(mqtt.BambuClient, "start", lambda self: connack(self, SUCCESS))
    monkeypatch.setattr(PrinterConnection, "MQTT_GRACE_SECONDS", 60)
    conn = _conn()

    async def scenario():
        await conn.connect()
        connack(conn.client, BAD_PASSWORD)
        return conn.is_offline()

    assert asyncio.run(scenario()) is True


def test_other_refusal_keeps_the_access_code_error(lan, monkeypatch):
    monkeypatch.setattr(mqtt.BambuClient, "start", lambda self: connack(self, BAD_PASSWORD))
    conn = _conn()

    async def scenario():
        await conn.connect()
        connack(conn.client, UNAVAILABLE)
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

    monkeypatch.setattr(mqtt.BambuClient, "start", start)
    monkeypatch.setattr(mqtt, "retire", lambda printer: stops.append(1))
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
    monkeypatch.setattr(mqtt.BambuClient, "start", lambda self: connack(self, answers.pop(0)))
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
    monkeypatch.setattr(mqtt.BambuClient, "start", lambda self: connack(self, BAD_PASSWORD))
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
    monkeypatch.setattr(mqtt.BambuClient, "start", lambda self: connack(self, BAD_PASSWORD))
    conn = _conn()

    async def scenario():
        await conn.connect()
        connack(conn.client, SUCCESS)
        conn._connected = True
        deliver(conn.client, {"print": {"gcode_state": "IDLE", "nozzle_temper": 25}})
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
    monkeypatch.setattr(mqtt.BambuClient, "start", lambda self: connack(self, rc))
    result = asyncio.run(api_test_connection(_body()))
    assert result["success"] is False
    assert "access code wrong or changed" in result["message"].lower()


def test_api_test_reports_other_refusal(quick_test_endpoint, monkeypatch):
    monkeypatch.setattr(mqtt.BambuClient, "start", lambda self: connack(self, UNAVAILABLE))
    result = asyncio.run(api_test_connection(_body()))
    assert result["success"] is False
    assert "Server unavailable" in result["message"]


def test_api_test_reports_no_connack(quick_test_endpoint, monkeypatch):
    monkeypatch.setattr(mqtt.BambuClient, "start", lambda self: None)
    result = asyncio.run(api_test_connection(_body()))
    assert result["success"] is False
    assert "timed out" in result["message"]


def test_api_test_reports_no_data(quick_test_endpoint, monkeypatch):
    monkeypatch.setattr(mqtt.BambuClient, "start", lambda self: connack(self, SUCCESS))
    result = asyncio.run(api_test_connection(_body()))
    assert result["success"] is False
    assert "serial" in result["message"]


def _answer_only_the_version_request(self):
    """A1 / P1S before the pushall reply: a report, but no status yet."""
    connack(self, SUCCESS)
    deliver(self, {"info": {"command": "get_version", "module": []}})


def test_api_test_waits_for_the_status_the_on_connect_pushall_brings(quick_test_endpoint, monkeypatch):
    def start(self):
        _answer_only_the_version_request(self)
        # The pushall reply, a moment later on paho's thread.
        threading.Timer(0.05, deliver, (self, {"print": {"command": "push_status", "msg": 0,
                                                         "gcode_state": "RUNNING"}})).start()

    monkeypatch.setattr(mqtt.BambuClient, "start", start)
    result = asyncio.run(api_test_connection(_body()))
    assert result == {"success": True, "message": "Connected successfully. State: RUNNING"}


def test_api_test_succeeds_without_a_status(quick_test_endpoint, monkeypatch):
    """Reports on the serial's topic prove the login and serial; state is a bonus."""
    monkeypatch.setattr(mqtt.BambuClient, "start", _answer_only_the_version_request)
    result = asyncio.run(api_test_connection(_body()))
    assert result == {"success": True, "message": "Connected successfully"}


def test_api_test_merges_partial_reports(quick_test_endpoint, monkeypatch):
    def start(self):
        connack(self, SUCCESS)
        deliver(self, {"print": {"command": "push_status", "msg": 1, "gcode_state": "IDLE"}})
        deliver(self, {"print": {"command": "push_status", "msg": 1, "nozzle_temper": 25}})

    monkeypatch.setattr(mqtt.BambuClient, "start", start)
    result = asyncio.run(api_test_connection(_body()))
    assert result == {"success": True, "message": "Connected successfully. State: IDLE"}


def test_api_test_success(quick_test_endpoint, monkeypatch, caplog):
    def start(self):
        connack(self, SUCCESS)
        deliver(self, {"print": {"gcode_state": "IDLE"}})

    monkeypatch.setattr(mqtt.BambuClient, "start", start)
    with caplog.at_level(logging.INFO):
        result = asyncio.run(api_test_connection(_body()))
    assert result["success"] is True
    assert "IDLE" in result["message"]
    assert "Printer Values Not Available Yet" not in caplog.text
