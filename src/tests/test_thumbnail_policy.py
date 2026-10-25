"""When a connection fetches a job's thumbnail: once per job, in the
background, with misses remembered, and never for files on internal storage."""
import asyncio

import bambulabs_api as bl
import pytest

from bamboozle import discovery, thumbnail
from bamboozle.models import PrinterConfig
from bamboozle.printer_manager import PrinterConnection

RETRY = 0.05


@pytest.fixture
def lan(monkeypatch):
    monkeypatch.setattr(discovery, "tcp_reachable", lambda ip, port=8883, timeout=3.0: True)
    monkeypatch.setattr(bl.Printer, "mqtt_start", lambda self: None)
    monkeypatch.setattr(bl.PrinterMQTTClient, "is_connected", lambda self: True)
    monkeypatch.setattr(PrinterConnection, "MQTT_SETTLE_SECONDS", 0)
    monkeypatch.setattr(PrinterConnection, "THUMB_RETRY_SECONDS", RETRY)


@pytest.fixture
def fetches(monkeypatch):
    """Records fetch_thumbnail calls; answer with `fetches.result` (bytes, None or
    an exception instance)."""
    class Recorder(list):
        result = b"PNG"

    rec = Recorder()

    def fetch(ip, access_code, gcode_file, subtask_name="", **kw):
        rec.append((gcode_file, subtask_name))
        if isinstance(rec.result, Exception):
            raise rec.result
        return rec.result

    monkeypatch.setattr(thumbnail, "fetch_thumbnail", fetch)
    return rec


def job(name="Benchy", state="RUNNING", **extra):
    payload = {"gcode_state": state, "nozzle_temper": 210, "sdcard": True,
               "gcode_file": f"{name}.3mf" if name else "", "subtask_name": name}
    payload.update(extra)
    return {"print": payload}


def _conn():
    return PrinterConnection(
        PrinterConfig(name="P1S", ip="192.168.0.103", access_code="x", serial="01P00C511100150"))


async def _connected():
    conn = _conn()
    await conn.connect()
    return conn


def report(conn, payload):
    mc = conn.printer.mqtt_client
    mc.manual_update(payload)
    mc.on_message_handler(mc, None, None, None)


async def poll(conn, times=1):
    """Read state `times` times, letting background fetches run in between."""
    state = None
    for _ in range(times):
        state = await conn.read_state()
        await asyncio.sleep(0.01)
    return state


def test_a_job_thumbnail_is_fetched_once(lan, fetches):
    async def scenario():
        conn = await _connected()
        report(conn, job())
        await poll(conn, 5)
        return await poll(conn)

    state = asyncio.run(scenario())
    assert fetches == [("Benchy.3mf", "Benchy")]
    assert state.has_thumbnail is True
    assert state.thumbnail_key == "Benchy"


def test_a_job_with_no_3mf_on_the_printer_is_not_looked_up_again(lan, fetches):
    fetches.result = None

    async def scenario():
        conn = await _connected()
        report(conn, job())
        await poll(conn, 3)
        await asyncio.sleep(RETRY * 3)
        return await poll(conn, 3)

    state = asyncio.run(scenario())
    assert len(fetches) == 1
    assert state.has_thumbnail is False


def test_a_failed_fetch_is_retried_later_not_on_every_poll(lan, fetches):
    fetches.result = ConnectionRefusedError("421 too many connections")

    async def scenario():
        conn = await _connected()
        report(conn, job())
        await poll(conn, 3)  # well inside the retry delay
        first = len(fetches)
        await asyncio.sleep(RETRY * 1.5)
        fetches.result = b"PNG"
        state = await poll(conn, 3)
        return first, state

    first, state = asyncio.run(scenario())
    assert first == 1
    assert len(fetches) == 2
    assert state.has_thumbnail is True


def test_a_new_job_gets_its_own_thumbnail_and_drops_the_old_one(lan, fetches):
    async def scenario():
        conn = await _connected()
        report(conn, job("Benchy"))
        await poll(conn, 2)
        fetches.result = None  # the new job's 3MF isn't there
        report(conn, job("Cube"))
        return await poll(conn, 2)

    state = asyncio.run(scenario())
    assert fetches == [("Benchy.3mf", "Benchy"), ("Cube.3mf", "Cube")]
    assert state.has_thumbnail is False  # not Benchy's picture on Cube's card


def test_an_idle_printer_with_no_job_drops_the_thumbnail(lan, fetches):
    async def scenario():
        conn = await _connected()
        report(conn, job())
        await poll(conn, 2)
        report(conn, job(name="", state="IDLE"))
        return conn, await poll(conn)

    conn, state = asyncio.run(scenario())
    assert state.has_thumbnail is False
    assert conn.thumbnail is None


def test_no_external_storage_means_no_lookup_and_says_why(lan, fetches):
    """H2C / P2S without an SD card: FTPS shows an empty root."""
    async def scenario():
        conn = await _connected()
        report(conn, job(sdcard=False, gcode_file="/data/Metadata/plate_2.gcode"))
        return await poll(conn, 3)

    state = asyncio.run(scenario())
    assert fetches == []
    assert state.thumbnail_unavailable == "internal_storage"


@pytest.mark.parametrize("url", ["brtc://emmc/Benchy.3mf", "file:///userdata/Benchy.3mf"])
def test_a_job_sent_to_internal_storage_is_not_looked_up(lan, fetches, url):
    async def scenario():
        conn = await _connected()
        report(conn, job(url=url))
        return await poll(conn, 3)

    state = asyncio.run(scenario())
    assert fetches == []
    assert state.thumbnail_unavailable == "internal_storage"


def test_an_earlier_jobs_internal_url_does_not_stick_to_the_next_job(lan, fetches):
    """Reports are merged, so `url` stays until the next project_file."""
    async def scenario():
        conn = await _connected()
        report(conn, job("Cube", url="brtc://emmc/Cube.gcode.3mf"))
        await poll(conn)
        report(conn, job("Benchy"))  # e.g. started from the SD card
        return await poll(conn, 2)

    state = asyncio.run(scenario())
    assert fetches == [("Benchy.3mf", "Benchy")]
    assert state.thumbnail_unavailable == ""


def test_a_job_on_external_storage_is_looked_up(lan, fetches):
    async def scenario():
        conn = await _connected()
        report(conn, job(url="ftp://Benchy.3mf"))
        return await poll(conn, 2)

    state = asyncio.run(scenario())
    assert len(fetches) == 1
    assert state.thumbnail_unavailable == ""


def test_disconnect_cancels_a_running_fetch(lan, monkeypatch):
    seen = {}

    def fetch(ip, access_code, gcode_file, subtask_name="", cancel=None, **kw):
        seen["cancel"] = cancel
        cancel.wait(2)
        raise thumbnail.FetchCancelled()

    monkeypatch.setattr(thumbnail, "fetch_thumbnail", fetch)

    async def scenario():
        conn = await _connected()
        report(conn, job())
        await poll(conn)
        await conn.disconnect()

    asyncio.run(scenario())
    assert seen["cancel"].is_set()
