"""BambuClient: our paho client. No network: paho is never started here."""
import json
import ssl

import pytest
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.reasoncodes import ReasonCode

from bamboozle import mqtt
from fakes import FakePahoMessage, RecordingPaho, connack, deliver

SERIAL = "01P00C511100150"
CODE = "12345678"
SUCCESS = ReasonCode(PacketTypes.CONNACK, "Success")
REFUSED = ReasonCode(PacketTypes.CONNACK, "Not authorized")


def _client(**callbacks):
    return mqtt.BambuClient("192.168.0.103", CODE, SERIAL, **callbacks)


def test_session_settings():
    client = _client()
    paho = client.paho
    assert paho._reconnect_on_failure is False  # see test_no_auto_reconnect...
    assert paho._max_inflight_messages == mqtt.INFLIGHT_WINDOW
    assert paho.suppress_exceptions is True
    assert paho._username == b"bblp" and paho._password == CODE.encode()
    ctx = paho._ssl_context
    assert ctx.verify_mode == ssl.CERT_NONE and ctx.check_hostname is False
    assert ctx.minimum_version == ssl.TLSVersion.TLSv1_2
    assert client.report_topic == f"device/{SERIAL}/report"
    assert client.request_topic == f"device/{SERIAL}/request"


def test_client_ids_are_unique_and_carry_no_secrets():
    ids = {_client().paho._client_id.decode() for _ in range(3)}
    assert len(ids) == 3
    for cid in ids:
        assert cid.startswith("bamboozle-")
        assert SERIAL not in cid and CODE not in cid


def test_start_never_waits_on_the_network(monkeypatch):
    calls = []
    client = _client()
    monkeypatch.setattr(client.paho, "connect_async", lambda *a: calls.append(("connect_async", a)))
    monkeypatch.setattr(client.paho, "loop_start", lambda: calls.append(("loop_start",)))
    client.start()
    assert calls == [("connect_async", ("192.168.0.103", 8883, mqtt.KEEPALIVE_SECONDS)),
                     ("loop_start",)]


def test_connack_is_reported_and_an_accepted_login_subscribes():
    seen = []
    client = _client(on_connack=seen.append)
    subscribed = []
    client._paho = RecordingPaho()
    client._paho.subscribe = lambda topic, qos=0: subscribed.append(topic)
    connack(client, REFUSED)
    assert seen == [REFUSED] and subscribed == [] and not client.connected_once
    connack(client, SUCCESS)
    assert seen == [REFUSED, SUCCESS]
    assert subscribed == [f"device/{SERIAL}/report"]
    assert client.connected_once


def test_the_subscription_brings_one_standalone_pushall():
    client = _client()
    client._paho = RecordingPaho()
    client._on_subscribe(client.paho, None, 1, [ReasonCode(PacketTypes.SUBACK, identifier=0)], None)
    [(topic, payload, qos)] = client.paho.published
    assert topic == client.request_topic and qos == 1
    assert list(payload) == ["pushing"] and payload["pushing"]["command"] == "pushall"


def test_a_refused_subscription_sends_nothing():
    client = _client()
    client._paho = RecordingPaho()
    client._on_subscribe(client.paho, None, 1, [ReasonCode(PacketTypes.SUBACK, identifier=0x80)], None)
    assert client.paho.published == []


def test_reports_are_decoded_and_handed_over():
    got = []
    client = _client(on_report=got.append)
    deliver(client, {"print": {"gcode_state": "IDLE"}})
    assert got == [{"print": {"gcode_state": "IDLE"}}]


@pytest.mark.parametrize("raw", [
    b"not json", b"[1, 2]", b'"text"', b"", b"\xff\xfe",
    b"[" * 100_000 + b"]" * 100_000,  # deeper than the parser's recursion limit
    b'{"x": "' + b"a" * (mqtt.MAX_PAYLOAD_BYTES + 1) + b'"}',
], ids=["not-json", "list", "string", "empty", "bad-utf8", "too-deep", "too-big"])
def test_junk_payloads_are_dropped(raw):
    got = []
    client = _client(on_report=got.append)
    client._on_message(client.paho, None, FakePahoMessage(raw))
    assert got == []


def test_invalid_utf8_inside_json_is_replaced_not_fatal():
    """Seen from A1 mini firmware."""
    got = []
    client = _client(on_report=got.append)
    client._on_message(client.paho, None, FakePahoMessage(b'{"print": {"name": "a\xffb"}}'))
    assert got == [{"print": {"name": "a�b"}}]


def test_an_owner_callback_error_never_reaches_paho():
    def boom(*_):
        raise RuntimeError("bug")

    client = _client(on_report=boom, on_connack=boom, on_disconnect=boom)
    client._paho = RecordingPaho()
    deliver(client, {"print": {}})
    connack(client, SUCCESS)
    client._on_disconnect(client.paho, None, None, 0, None)


def test_publish_queues_qos1_and_refuses_while_down():
    client = _client()
    client._paho = RecordingPaho(connected=False)
    assert mqtt.publish(client, {"x": 1}) is False
    assert client.paho.published == []
    client.paho.connected = True
    assert mqtt.publish(client, {"x": 1}) is True
    assert client.paho.published == [(client.request_topic, {"x": 1}, 1)]


def test_no_auto_reconnect_so_unacked_commands_are_never_replayed(monkeypatch):
    """paho re-sends every QoS 1 message without a PUBACK on reconnect; the
    Bambu broker loses PUBACKs. So a dropped session must not reconnect."""
    client = _client()
    paho = client.paho
    # What paho's network loop decides after a dropped connection.
    reconnects = []
    states = paho._state.__class__

    def reconnect():
        reconnects.append(1)
        paho._state = states.MQTT_CS_DISCONNECTED  # ends the loop either way

    monkeypatch.setattr(paho, "reconnect", reconnect)
    monkeypatch.setattr(paho, "_reconnect_wait", lambda: None)
    monkeypatch.setattr(paho, "_loop", lambda timeout: 7)  # MQTT_ERR_CONN_LOST
    paho._state = states.MQTT_CS_CONNECTED
    paho.loop_forever()
    assert reconnects == []


def test_sequence_ids_are_strings_and_start_high():
    first = int(mqtt.next_sequence_id())
    assert 100_000 <= first < 900_000 + 1_000_000
    assert int(mqtt.pushall_request()["pushing"]["sequence_id"]) > first


def test_requests_are_standalone_json():
    for payload in (mqtt.pushall_request(), mqtt.version_request(), *mqtt.light_requests("on")):
        assert len(payload) == 1
        json.dumps(payload)
