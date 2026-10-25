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
