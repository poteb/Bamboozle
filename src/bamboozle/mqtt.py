"""Requests and teardown on the paho client inside a `bambulabs_api.Printer`.

The library publishes with QoS 0 and its `mqtt_stop()` only joins paho's
network thread (no DISCONNECT), which can block for as long as a TLS
handshake takes. These helpers go to the paho client directly instead.
"""
from __future__ import annotations

import itertools
import json
import logging
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

# The Bambu broker's PUBACK matching races paho's QoS 1 in-flight tracking;
# with paho's default window of 20 a session wedges after a few dozen requests.
INFLIGHT_WINDOW = 1000
# Printers busy broadcasting may ignore QoS 0 requests.
REQUEST_QOS = 1

_sequence = itertools.count(1)
_teardowns: set[threading.Thread] = set()
_teardowns_lock = threading.Lock()


def _next_sequence_id() -> str:
    return str(next(_sequence))


def pushall_request() -> dict:
    """Ask for a full status report. Sent on its own: A1 / P1S ignore it when
    it shares a message with other requests."""
    return {"pushing": {"command": "pushall", "sequence_id": _next_sequence_id()}}


def light_requests(mode: str) -> list[dict]:
    """`ledctrl` for both chamber LED nodes (H2 models have two; others ignore
    the second)."""
    return [
        {
            "system": {
                "sequence_id": _next_sequence_id(),
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


def configure(printer: Any) -> None:
    """Session settings the library doesn't expose; call before connecting."""
    printer.mqtt_client._client.max_inflight_messages_set(INFLIGHT_WINDOW)


def publish(printer: Any, payload: dict) -> bool:
    """Queue one request for the printer; never waits for delivery.

    Nothing is queued while the session is down: paho would replay a QoS 1
    request on reconnect, long after the user gave up on it.
    """
    mc = printer.mqtt_client
    if not mc.is_connected():
        return False
    info = mc._client.publish(mc.command_topic, json.dumps(payload), qos=REQUEST_QOS)
    return info.rc == 0


def retire(printer: Any) -> threading.Thread | None:
    """End the printer's MQTT session without blocking the caller.

    Callbacks are cleared first, so a CONNACK still in flight can't
    resubscribe or report into a connection that has been replaced. The
    DISCONNECT (which also stops paho's auto-reconnect) and the join of the
    network thread run on a daemon thread. Returns that thread, or None when
    the printer has no paho client.
    """
    client = getattr(printer.mqtt_client, "_client", None)
    if client is None:
        return None
    for name in ("on_connect", "on_message", "on_disconnect"):
        try:
            setattr(client, name, None)
        except Exception as e:
            logger.debug("Clearing %s failed: %s", name, e)
    thread = threading.Thread(
        target=_shutdown, args=(client,), name="mqtt-teardown", daemon=True
    )
    with _teardowns_lock:
        _teardowns.difference_update([t for t in _teardowns if not t.is_alive()])
        _teardowns.add(thread)
    thread.start()
    return thread


def _shutdown(client: Any) -> None:
    for step in (client.disconnect, client.loop_stop, client.disconnect):
        # The second disconnect closes a handshake that completed while the
        # first one ran (it then found no socket and couldn't send anything).
        try:
            step()
        except Exception as e:
            logger.debug("MQTT teardown step %s failed: %s", step.__name__, e)


def wait_for_teardowns(timeout: float) -> None:
    """Give pending teardowns up to `timeout` seconds in total (at shutdown,
    so the DISCONNECTs leave before the process exits)."""
    deadline = time.monotonic() + timeout
    with _teardowns_lock:
        pending = list(_teardowns)
    for thread in pending:
        thread.join(max(0.0, deadline - time.monotonic()))
