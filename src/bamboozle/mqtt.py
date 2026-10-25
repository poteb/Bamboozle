"""Our MQTT client for the printers' LAN broker, on paho-mqtt.

One `BambuClient` per session (a reset builds a new one). It connects with
TLS to port 8883 as `bblp` / access code, subscribes to
`device/<serial>/report`, asks for a full report (`pushall`) once the
subscription is in place, and hands every decoded report to its owner on
paho's network thread. Requests go to `device/<serial>/request` with QoS 1.

What paho is told and why:
- no automatic reconnect: on every reconnect paho re-sends each QoS 1 request
  still waiting for its PUBACK, and the Bambu broker loses PUBACKs, so a stop
  from minutes ago could hit the next print. A dropped session ends the
  client; its owner retires it (with its queue) and connects a fresh one;
- a fresh client id per client (process id + a random token + a counter), so
  the broker drops an older session, and two running instances don't kick
  each other off;
- keepalive 30 s (the broker drops a dead client at 45 s); the first
  connection is still retried, 1-30 s apart;
- an inflight window of 1000: the Bambu broker's PUBACK matching races paho's
  QoS 1 in-flight tracking, and with the default 20 a session wedges after a
  few dozen requests;
- TLS 1.2+ without certificate verification: printers use self-signed
  certificates. (A move to another IP is checked against the certificate CN
  in PrinterManager.handle_announcement.)

Requests are sent on their own: the A1 / P1S ignore a message that combines
several (e.g. pushall + get_version).
"""
from __future__ import annotations

import itertools
import json
import logging
import os
import secrets
import ssl
import threading
import time
from typing import Any, Callable

import paho.mqtt.client as paho

logger = logging.getLogger(__name__)

MQTT_PORT = 8883
USERNAME = "bblp"
KEEPALIVE_SECONDS = 30
RECONNECT_MIN_SECONDS = 1
RECONNECT_MAX_SECONDS = 30
INFLIGHT_WINDOW = 1000
# Printers busy broadcasting may ignore QoS 0 requests.
REQUEST_QOS = 1
# Reports are a few KB (13 KB on the H2C); anything this big isn't one.
MAX_PAYLOAD_BYTES = 1 << 20
# How long a teardown keeps closing late handshakes before leaving the
# (daemon) network thread to itself.
TEARDOWN_WATCH_SECONDS = 60.0

# Started at a random offset: echoes of other clients' requests (Bambu
# Studio's ids are in the 2000s) share the report topic with ours.
_sequence = itertools.count(100_000 + secrets.randbelow(800_000))
_client_ids = itertools.count(1)
_client_token = secrets.token_hex(3)
_teardowns: set[threading.Thread] = set()
_teardowns_lock = threading.Lock()


def next_sequence_id() -> str:
    return str(next(_sequence))


def pushall_request() -> dict:
    """Ask for a full status report. Sent on its own: A1 / P1S ignore it when
    it shares a message with other requests."""
    return {"pushing": {"command": "pushall", "sequence_id": next_sequence_id()}}


def version_request() -> dict:
    """Ask for module versions (the firmware is module "ota"); answered with
    an `info` report."""
    return {"info": {"command": "get_version", "sequence_id": next_sequence_id()}}


def light_requests(mode: str) -> list[dict]:
    """`ledctrl` for both chamber LED nodes (H2 models have two; others ignore
    the second)."""
    return [
        {
            "system": {
                "sequence_id": next_sequence_id(),
                "command": "ledctrl",
                "led_node": node,
                "led_mode": mode,
                "led_on_time": 500,
                "led_off_time": 500,
                "loop_times": 0,
                "interval_time": 0,
            }
        }
        for node in ("chamber_light", "chamber_light2")
    ]


def decode_payload(raw: bytes) -> dict | None:
    """A report as a dict; None for anything else (too big, not JSON, not an
    object). Invalid UTF-8 (seen from A1 mini firmware) is replaced, not fatal."""
    if len(raw) > MAX_PAYLOAD_BYTES:
        return None
    try:
        payload = json.loads(raw.decode("utf-8", errors="replace"))
    except Exception:  # not JSON, or nested past the parser's recursion limit
        return None
    return payload if isinstance(payload, dict) else None


def _tls_context() -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # self-signed printer certificates
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx


class BambuClient:
    """One MQTT session with one printer.

    Callbacks run on paho's network thread:
    - `on_connack(reason_code)` for every CONNACK (a paho ReasonCode);
    - `on_report(payload)` for every report that decodes to a JSON object;
    - `on_disconnect()` when the connection drops. The client doesn't
      reconnect after that (see the module docstring): retire it and start a
      new one. `connected_once` says whether it ever logged in.
    """

    def __init__(
        self,
        ip: str,
        access_code: str,
        serial: str,
        on_connack: Callable[[Any], None] | None = None,
        on_report: Callable[[dict], None] | None = None,
        on_disconnect: Callable[[], None] | None = None,
    ):
        self.ip = ip
        self.serial = serial
        self.report_topic = f"device/{serial}/report"
        self.request_topic = f"device/{serial}/request"
        self.on_connack = on_connack or (lambda rc: None)
        self.on_report = on_report or (lambda payload: None)
        self.on_disconnect = on_disconnect or (lambda: None)
        self.connected_once = False
        self._paho = paho.Client(
            paho.CallbackAPIVersion.VERSION2,
            client_id=f"bamboozle-{os.getpid()}-{_client_token}-{next(_client_ids)}",
            protocol=paho.MQTTv311,
            reconnect_on_failure=False,
        )
        self._paho.suppress_exceptions = True  # a callback bug must not kill the loop
        self._paho.username_pw_set(USERNAME, access_code)
        self._paho.tls_set_context(_tls_context())
        self._paho.max_inflight_messages_set(INFLIGHT_WINDOW)
        self._paho.reconnect_delay_set(RECONNECT_MIN_SECONDS, RECONNECT_MAX_SECONDS)
        self._paho.on_connect = self._on_connect
        self._paho.on_subscribe = self._on_subscribe
        self._paho.on_message = self._on_message
        self._paho.on_disconnect = self._on_disconnect

    @property
    def paho(self) -> Any:
        return self._paho

    def start(self) -> None:
        """Connect in the background (never waits on the network)."""
        self._paho.connect_async(self.ip, MQTT_PORT, KEEPALIVE_SECONDS)
        self._paho.loop_start()

    def is_connected(self) -> bool:
        try:
            return bool(self._paho.is_connected())
        except Exception:
            return False

    def publish(self, payload: dict) -> bool:
        """Queue one request (QoS 1); False while the session is down."""
        if not self.is_connected():
            return False
        info = self._paho.publish(self.request_topic, json.dumps(payload), qos=REQUEST_QOS)
        return info.rc == 0

    # --- paho callbacks (network thread) ----------------------------------------

    def _on_connect(self, client, userdata, flags, reason_code, properties=None) -> None:
        accepted = not getattr(reason_code, "is_failure", reason_code != 0)
        if accepted:
            self.connected_once = True
        self._safe(self.on_connack, reason_code)
        if accepted:
            client.subscribe(self.report_topic)

    def _on_subscribe(self, client, userdata, mid, reason_codes, properties=None) -> None:
        if any(getattr(rc, "is_failure", False) for rc in reason_codes or ()):
            logger.warning("%s: subscribing to the report topic was refused", self.ip)
            return
        # The full state, once per session (A1 / P1S only push deltas).
        publish(self, pushall_request())

    def _on_message(self, client, userdata, msg) -> None:
        try:
            payload = decode_payload(msg.payload)
        except Exception:
            payload = None
        if payload is None:
            logger.debug("%s: ignored a report that isn't a JSON object", self.ip)
            return
        self._safe(self.on_report, payload)

    def _on_disconnect(self, client, userdata, flags, reason_code, properties=None) -> None:
        self._safe(self.on_disconnect)

    def _safe(self, fn: Callable, *args: Any) -> None:
        """An owner callback must never break paho's network thread."""
        try:
            fn(*args)
        except Exception:
            logger.exception("%s: MQTT callback failed", self.ip)


def publish(client: BambuClient, payload: dict) -> bool:
    """Queue one request for the printer (QoS 1); never waits for delivery.

    Nothing is queued while the session is down: the request would sit in
    paho's queue for a session that never comes back (clients don't
    reconnect; see the module docstring).
    """
    return client.publish(payload)


def retire(client: Any) -> threading.Thread | None:
    """End the client's MQTT session without waiting on paho.

    Callbacks are cleared first (each setter waits at most for a callback
    already running on paho's thread), so a CONNACK still in flight can't
    resubscribe or report into a connection that has been replaced. (A
    callback already running isn't waited for; the connection drops reports
    from a client it has replaced.) The DISCONNECT and the join of the network
    thread run on a daemon thread. Returns that thread, or None when
    there's no paho client.
    """
    paho_client = getattr(client, "paho", None)
    if paho_client is None:
        return None
    for name in ("on_connect", "on_subscribe", "on_message", "on_disconnect"):
        try:
            setattr(paho_client, name, None)
        except Exception as e:
            logger.debug("Clearing %s failed: %s", name, e)
    thread = threading.Thread(
        target=_shutdown, args=(paho_client,), name="mqtt-teardown", daemon=True
    )
    with _teardowns_lock:
        _teardowns.difference_update([t for t in _teardowns if not t.is_alive()])
        _teardowns.add(thread)
    thread.start()
    return thread


def _shutdown(client: Any) -> None:
    _attempt(client.disconnect)
    # If paho was still retrying the first connection, that disconnect found
    # no socket and the handshake may complete afterwards. paho's loop then ignores loop_stop's
    # terminate flag while QoS 1 requests are unacked, so watch the join and
    # disconnect again whenever the session shows up connected.
    stopper = threading.Thread(
        target=_attempt, args=(client.loop_stop,), name="mqtt-loop-stop", daemon=True
    )
    stopper.start()
    deadline = time.monotonic() + TEARDOWN_WATCH_SECONDS
    while stopper.is_alive() and time.monotonic() < deadline:
        stopper.join(0.2)
        if _attempt(client.is_connected):
            _attempt(client.disconnect)
    # The loop may exit on the terminate flag with the session still open.
    _attempt(client.disconnect)


def _attempt(step: Any) -> Any:
    try:
        return step()
    except Exception as e:
        logger.debug("MQTT teardown step %s failed: %s", getattr(step, "__name__", step), e)
        return None


def wait_for_teardowns(timeout: float) -> None:
    """Give pending teardowns up to `timeout` seconds in total (at shutdown,
    so the DISCONNECTs leave before the process exits)."""
    deadline = time.monotonic() + timeout
    with _teardowns_lock:
        pending = list(_teardowns)
    for thread in pending:
        thread.join(max(0.0, deadline - time.monotonic()))
