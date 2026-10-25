class FakeWs:
    """Stands in for a dashboard WebSocket; records what was broadcast."""

    def __init__(self):
        self.sent = []

    async def send_json(self, msg):
        self.sent.append(msg)
