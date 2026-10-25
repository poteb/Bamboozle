import json
import io
import threading
import time


class FakeWs:
    """Stands in for a dashboard WebSocket; records what was broadcast."""

    def __init__(self):
        self.sent = []

    async def send_json(self, msg):
        self.sent.append(msg)


JPEG_A = b"\xff\xd8" + b"A" * 50 + b"\xff\xd9"
JPEG_B = b"\xff\xd8" + b"B" * 50 + b"\xff\xd9"


def wait_until(cond, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.01)
    return False


class FakeFfmpeg:
    """A Popen stand-in whose stdout plays `frames` (cycled) until killed.

    `frames=[]` models an ffmpeg that has stalled and writes nothing.
    """

    def __init__(self, frames, returncode=None, stderr=b""):
        self.frames = frames
        self.returncode = returncode
        self.killed = threading.Event()
        self.stdout = self
        self.stderr = io.BytesIO(stderr)
        self._i = 0

    def close(self):
        pass

    def read(self, n):
        if not self.frames:
            self.killed.wait()
            return b""
        if self.killed.wait(0.02):
            return b""
        frame = self.frames[self._i % len(self.frames)]
        self._i += 1
        return frame

    def poll(self):
        return self.returncode

    def kill(self):
        self.returncode = -9
        self.killed.set()

    terminate = kill

    def wait(self, timeout=None):
        return self.returncode


# --- MQTT (bamboozle.mqtt.BambuClient) -------------------------------------------


class FakePahoMessage:
    def __init__(self, payload):
        self.payload = payload if isinstance(payload, bytes) else json.dumps(payload).encode()


def deliver(client, payload):
    """Hand a report to a BambuClient the way paho's network thread does."""
    client._on_message(client.paho, None, FakePahoMessage(payload))


def connack(client, rc):
    """Hand a CONNACK (a paho ReasonCode, or 0) to a BambuClient like paho does."""
    client._on_connect(client.paho, None, None, rc, None)


class RecordingPaho:
    """Stands in for the paho client inside a BambuClient; records publishes."""

    def __init__(self, connected=True):
        self.connected = connected
        self.published = []  # (topic, payload dict, qos)

    def is_connected(self):
        return self.connected

    def publish(self, topic, payload, qos=0):
        self.published.append((topic, json.loads(payload), qos))

        class Info:
            rc = 0
        return Info()

    def subscribe(self, topic, qos=0):
        return (0, 1)

    def disconnect(self):
        self.connected = False

    def loop_stop(self):
        pass


def connected(conn, print_payload=None, paho=None):
    """A PrinterConnection that looks logged in, with a recording paho client
    and (optionally) a full report merged. Call inside the test's event loop."""
    import asyncio
    conn.client._paho = paho or RecordingPaho()
    conn._connected = True
    conn._loop = asyncio.get_running_loop()
    if print_payload is not None:
        conn.report.apply({"print": {"command": "push_status", "msg": 0, **print_payload}})
    return conn


def published(conn):
    """The payloads a `connected` connection published, in order."""
    return [payload for _, payload, _ in conn.client.paho.published]
