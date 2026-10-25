"""SsdpListener: passive UDP listener for printer announcements."""
import asyncio
import logging
import socket

import pytest

from bamboozle.discovery import SsdpListener

PACKET = (
    b"NOTIFY * HTTP/1.1\r\nLocation: 192.168.0.102\r\nUSN: 03919D533005771\r\n"
    b"DevModel.bambu.com: N2S\r\nDevName.bambu.com: A1\r\n\r\n"
)


def test_delivers_parsed_announcements():
    async def scenario():
        received = []
        listener = SsdpListener(received.append, port=0, host="127.0.0.1")
        assert await listener.start() is True
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as tx:
            tx.sendto(b"not an announcement", ("127.0.0.1", listener.port))
            tx.sendto(PACKET, ("127.0.0.1", listener.port))
        for _ in range(50):
            if received:
                break
            await asyncio.sleep(0.02)
        listener.stop()
        return received

    received = asyncio.run(scenario())
    assert [(a.ip, a.serial, a.name) for a in received] == [
        ("192.168.0.102", "03919D533005771", "A1")]


def test_shares_port_with_another_reuseaddr_listener():
    """Bambu Studio may already hold UDP 2021 with SO_REUSEADDR."""
    async def scenario():
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as other:
            other.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            other.bind(("127.0.0.1", 0))
            port = other.getsockname()[1]
            listener = SsdpListener(lambda a: None, port=port, host="127.0.0.1")
            ok = await listener.start()
            listener.stop()
            return ok

    assert asyncio.run(scenario()) is True


@pytest.mark.skipif(not hasattr(socket, "SO_EXCLUSIVEADDRUSE"), reason="Windows only")
def test_bind_failure_disables_feature_without_raising(caplog):
    async def scenario():
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as hog:
            hog.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            hog.bind(("127.0.0.1", 0))
            port = hog.getsockname()[1]
            listener = SsdpListener(lambda a: None, port=port, host="127.0.0.1")
            ok = await listener.start()
            listener.stop()
            return ok

    with caplog.at_level(logging.WARNING):
        assert asyncio.run(scenario()) is False
    assert "auto-detect disabled" in caplog.text
