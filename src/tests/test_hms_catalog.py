"""Bambu's HMS / print_error descriptions, fetched and cached (bamboozle.hms_catalog).

Every catalogue text here is made up: Bambu's catalogue is never copied into
the repo (not even as a fixture).
"""
import asyncio
import json
import os
import time

import pytest

from bamboozle import hms, hms_catalog
from bamboozle.models import AppConfig, PrinterConfig
from bamboozle.printer_manager import PrinterConnection, PrinterManager

H2C_SERIAL = "31BZZZZZZZZZZZZ"
P2S_SERIAL = "22EZZZZZZZZZZZZ"

AMS2_RUNOUT = {"attr": 0x07012000, "code": 0x00020001}    # 0701200000020001, serious
CAMERA_GONE = {"attr": 0x05000600, "code": 0x00020001}    # 0500060000020001
NO_TEXT_CODE = {"attr": 0x05000600, "code": 0x00020006}   # 0500060000020006
VERIFY_FAILED = {"attr": 0x05000500, "code": 0x00010007}  # ours: 0500050000010007
UNLISTED = {"attr": 0x0C000200, "code": 0x00030009}       # 0C00020000030009


def body(hms_entries=(), error_entries=(), result=0):
    """A catalogue response shaped like Bambu's, with made-up text."""
    return json.dumps({
        "result": result, "t": 1, "ver": 1,
        "data": {
            "device_hms": {"ver": 1, "en": [{"ecode": c, "intro": t} for c, t in hms_entries]},
            "device_error": {"ver": 1, "en": [{"ecode": c, "intro": t} for c, t in error_entries]},
        },
    }).encode()


GENERIC = body(
    hms_entries=[("0701200000020001", "Generic runout text."),
                 ("0500060000020001", "Generic camera text."),
                 ("0500050000010007", "Their refused-command text.")],
    error_entries=[("03008003", "Generic print error text.")],
)
H2C = body(hms_entries=[("0500060000020001", "H2C camera text."),
                        ("0500060000020006", "")])


class FakeHttp:
    """Stands in for the HTTP adapter: maps a model prefix ("" = generic) to a body."""

    def __init__(self, bodies):
        self.bodies = dict(bodies)
        self.urls = []

    def __call__(self, url, timeout, max_bytes):
        self.urls.append(url)
        prefix = url.partition("&d=")[2]
        b = self.bodies.get(prefix)
        if isinstance(b, Exception):
            raise b
        if b is None:
            raise OSError("no route")
        return b


@pytest.fixture
def http(monkeypatch):
    fake = FakeHttp({"": GENERIC, "31B": H2C})
    monkeypatch.setattr(hms_catalog, "http_get", fake)
    return fake


def refreshed(serials, catalog=None):
    catalog = catalog or hms_catalog.HmsCatalog()
    asyncio.run(catalog.refresh(serials))
    return catalog


def errors(*entries, print_error=None, gcode_state="IDLE"):
    payload = {"hms": list(entries), "gcode_state": gcode_state}
    if print_error is not None:
        payload["print_error"] = print_error
    return hms.printer_errors(payload)


def described(catalog, serial, *entries, **kw):
    return catalog.describe(errors(*entries, **kw), serial)


# --- What leaves the machine ------------------------------------------------------

def test_only_the_three_character_serial_prefix_is_sent(http):
    refreshed([H2C_SERIAL, P2S_SERIAL.lower()])
    assert sorted(http.urls) == sorted([
        "https://e.bambulab.com/query.php?lang=en",
        "https://e.bambulab.com/query.php?lang=en&d=31B",
        "https://e.bambulab.com/query.php?lang=en&d=22E",
    ])
    assert not any("ZZZ" in u for u in http.urls)


@pytest.mark.parametrize("serial, prefix", [
    ("31BZZZ", "31B"), ("  22ezzz", "22E"), ("01P", "01P"),
    ("", None), ("3B", None), ("3&B123", None), ("31B/../x", "31B"),
])
def test_serial_prefix(serial, prefix):
    assert hms_catalog.serial_prefix(serial) == prefix


def test_a_printer_without_a_usable_serial_gets_the_generic_catalogue_only(http):
    refreshed(["", "?"])
    assert http.urls == ["https://e.bambulab.com/query.php?lang=en"]


# --- Descriptions on the errors -----------------------------------------------------

def test_the_models_own_text_comes_first_then_the_generic_one(http):
    catalog = refreshed([H2C_SERIAL])
    by_code = {e.code: e.description for e in described(catalog, H2C_SERIAL, CAMERA_GONE, AMS2_RUNOUT)}
    assert by_code == {"0500_0600_0002_0001": "H2C camera text.",
                       "0701_2000_0002_0001": "Generic runout text."}


def test_another_model_uses_the_generic_text(http):
    catalog = refreshed([H2C_SERIAL, P2S_SERIAL])  # 22E: no catalogue served
    [e] = described(catalog, P2S_SERIAL, CAMERA_GONE)
    assert e.description == "Generic camera text."


def test_a_print_error_is_described_from_the_print_error_table(http):
    catalog = refreshed([H2C_SERIAL])
    [e] = described(catalog, H2C_SERIAL, print_error=0x03008003)
    assert (e.code, e.description) == ("0300_8003", "Generic print error text.")


def test_our_own_text_stays_where_it_is_better(http):
    """Bambu's refused-command text tells the user to update Bambu Studio."""
    catalog = refreshed([H2C_SERIAL])
    [e] = described(catalog, H2C_SERIAL, VERIFY_FAILED)
    assert e.description.startswith("A LAN command was refused earlier")


def test_a_code_bambu_lists_without_text_says_so_and_sorts_last(http):
    catalog = refreshed([H2C_SERIAL])
    out = described(catalog, H2C_SERIAL, NO_TEXT_CODE, CAMERA_GONE, VERIFY_FAILED)
    assert [e.code for e in out][-1] == "0500_0600_0002_0006"
    last = out[-1]
    assert last.description == "No description from Bambu (internal code)"
    assert last.undocumented
    assert not any(e.undocumented for e in out[:-1])


def test_undocumented_codes_sort_last_among_lingering_but_active_stays_first(http):
    catalog = refreshed([H2C_SERIAL])
    fatal = {"attr": 0x03000100, "code": 0x00010005}  # active, listed nowhere
    out = described(catalog, H2C_SERIAL, NO_TEXT_CODE, fatal, VERIFY_FAILED)
    assert [(e.code, e.active) for e in out] == [
        ("0300_0100_0001_0005", True),
        ("0500_0500_0001_0007", False),
        ("0500_0600_0002_0006", False),  # serious, but Bambu has no text for it
    ]


def test_a_code_no_catalogue_lists_keeps_the_code_only_display(http):
    catalog = refreshed([H2C_SERIAL])
    [e] = described(catalog, H2C_SERIAL, UNLISTED)
    assert (e.description, e.undocumented) == ("", False)


def test_without_any_catalogue_the_errors_are_unchanged():
    catalog = hms_catalog.HmsCatalog()
    before = errors(CAMERA_GONE, NO_TEXT_CODE)
    after = catalog.describe(errors(CAMERA_GONE, NO_TEXT_CODE), H2C_SERIAL)
    assert [e.model_dump() for e in after] == [e.model_dump() for e in before]


def test_describe_never_raises():
    class Broken(hms_catalog.HmsCatalog):
        def _lookup(self, *a):
            raise RuntimeError("boom")
    out = Broken().describe(errors(CAMERA_GONE), H2C_SERIAL)
    assert [e.description for e in out] == [""]


# --- Untrusted input -----------------------------------------------------------------

def test_parse_keeps_valid_entries_and_drops_malformed_ones():
    raw = json.dumps({"result": 0, "data": {
        "device_hms": {"en": [
            {"ecode": "0701200000020001", "intro": "  Made-up\n  text.  "},
            {"ecode": "0701200000020002"},                     # no intro
            {"ecode": "07012000000200XX", "intro": "x"},       # not hex
            {"ecode": "07012000", "intro": "x"},               # wrong length
            {"ecode": 7, "intro": "x"},
            {"ecode": "0701200000020003", "intro": ["x"]},
            "junk",
            {"ecode": "0701200000020004", "intro": "y" * 5000},
            {"ecode": "0c00020000030009", "intro": "lower-case code"},
        ]},
        "device_error": {"en": [{"ecode": "03008003", "intro": "pe"},
                                {"ecode": "0701200000020001", "intro": "wrong table length"}]},
    }}).encode()
    tables = hms_catalog.parse(raw)
    assert tables.hms["0701200000020001"] == "Made-up text."
    assert tables.hms["0701200000020002"] == ""
    assert "07012000000200XX" not in tables.hms and "07012000" not in tables.hms
    assert "0701200000020003" not in tables.hms
    assert len(tables.hms["0701200000020004"]) <= hms_catalog.MAX_INTRO_CHARS
    assert tables.hms["0C00020000030009"] == "lower-case code"
    assert tables.error == {"03008003": "pe"}


@pytest.mark.parametrize("raw", [
    b"", b"not json", b"[]", b'{"result": 0}', b'{"result": 0, "data": []}',
    b'{"result": 500, "data": {}}', b'{"result": 0, "data": {"device_hms": {"en": {}}}}',
    b'\xff\xfe',
])
def test_parse_rejects_what_is_not_a_catalogue(raw):
    assert hms_catalog.parse(raw) is None


def test_parse_treats_no_catalogue_for_this_model_as_empty():
    """`d=<unknown prefix>` answers {"result":201,...} with no data."""
    tables = hms_catalog.parse(b'{"result":201,"t":1,"ver":0,"msg":""}')
    assert (tables.hms, tables.error) == ({}, {})


def test_http_get_refuses_an_oversized_body(monkeypatch):
    class Resp:
        status = 200
        def __init__(self): self.data = b"x" * 101
        def read(self, n):
            chunk, self.data = self.data[:n], self.data[n:]
            return chunk
        def __enter__(self): return self
        def __exit__(self, *a): return False
    seen = {}
    def urlopen(req, timeout):
        seen["ua"] = req.get_header("User-agent")
        seen["timeout"] = timeout
        return Resp()
    monkeypatch.setattr(hms_catalog.urllib.request, "urlopen", urlopen)
    with pytest.raises(ValueError):
        hms_catalog._http_get("https://e.bambulab.com/query.php?lang=en", timeout=5, max_bytes=100)
    # Bambu's CDN refuses Python's default user agent.
    assert seen["ua"] and "urllib" not in seen["ua"].lower()
    assert seen["timeout"] == 5


# --- Cache -------------------------------------------------------------------------

def cache_dir():
    return os.path.join(os.environ["LOCALAPPDATA"], "Bamboozle", "hms-cache")


def test_a_fetched_catalogue_is_cached_and_used_without_fetching_again(http):
    refreshed([H2C_SERIAL])
    assert sorted(os.listdir(cache_dir())) == ["en-31B.json", "en.json"]
    http.urls.clear()
    catalog = refreshed([H2C_SERIAL])  # a fresh start: reads the cache
    assert http.urls == []
    [e] = described(catalog, H2C_SERIAL, CAMERA_GONE)
    assert e.description == "H2C camera text."


def age_cache(seconds):
    for name in os.listdir(cache_dir()):
        path = os.path.join(cache_dir(), name)
        data = json.loads(open(path, encoding="utf-8").read())
        data["fetched_at"] -= seconds
        open(path, "w", encoding="utf-8").write(json.dumps(data))


def test_a_week_old_cache_is_refreshed(http):
    refreshed([H2C_SERIAL])
    age_cache(hms_catalog.REFRESH_SECONDS + 1)
    http.urls.clear()
    http.bodies["31B"] = body(hms_entries=[("0500060000020001", "Newer H2C text.")])
    catalog = refreshed([H2C_SERIAL])
    assert len(http.urls) == 2
    [e] = described(catalog, H2C_SERIAL, CAMERA_GONE)
    assert e.description == "Newer H2C text."


def test_a_failed_refresh_keeps_using_the_stale_cache(http):
    refreshed([H2C_SERIAL])
    age_cache(hms_catalog.REFRESH_SECONDS + 1)
    http.bodies = {"": OSError("offline"), "31B": b"<html>bot check</html>"}
    catalog = refreshed([H2C_SERIAL])
    [e] = described(catalog, H2C_SERIAL, CAMERA_GONE)
    assert e.description == "H2C camera text."


def test_a_failed_fetch_is_retried_later_not_on_every_round(http, monkeypatch):
    http.bodies = {"": OSError("offline")}
    catalog = refreshed([])
    asyncio.run(catalog.refresh([]))
    assert len(http.urls) == 1
    now = time.monotonic()
    monkeypatch.setattr(hms_catalog.time, "monotonic", lambda: now + hms_catalog.RETRY_MIN_SECONDS + 1)
    asyncio.run(catalog.refresh([]))
    assert len(http.urls) == 2


def test_a_corrupt_cache_file_is_ignored_and_refetched(http):
    os.makedirs(cache_dir())
    open(os.path.join(cache_dir(), "en.json"), "w").write("{broken")
    catalog = refreshed([])
    assert http.urls == ["https://e.bambulab.com/query.php?lang=en"]
    [e] = described(catalog, "", AMS2_RUNOUT)
    assert e.description == "Generic runout text."


def test_a_cache_file_from_the_future_counts_as_stale(http):
    refreshed([])
    age_cache(-(2 * 86400))
    http.urls.clear()
    refreshed([])
    assert len(http.urls) == 1


# --- Wiring ------------------------------------------------------------------------

def test_the_card_state_carries_bambus_text(monkeypatch, http):
    catalog = refreshed([H2C_SERIAL])
    monkeypatch.setattr(hms_catalog, "CATALOG", catalog)

    conn = PrinterConnection(PrinterConfig(name="H2C", ip="10.0.0.3", access_code="x",
                                           serial=H2C_SERIAL, model="O1C2"))
    conn.report.print = {"gcode_state": "IDLE", "nozzle_temper": 25, "hms": [CAMERA_GONE]}
    conn._connected = True
    state = asyncio.run(conn.read_state())
    assert [e.description for e in state.errors] == ["H2C camera text."]


def test_a_newly_loaded_catalogue_pushes_the_cards_again(monkeypatch, http):
    """Event-driven pushes only re-read a printer when something changes, so
    a catalogue that loads later says so."""
    catalog = hms_catalog.HmsCatalog()
    calls = []

    async def scenario():
        catalog.start(lambda: [H2C_SERIAL], on_change=lambda: calls.append(catalog.generation))
        for _ in range(200):
            if calls:
                break
            await asyncio.sleep(0.01)
        await catalog.stop()

    asyncio.run(scenario())
    assert calls and calls[0] >= 1


def test_a_refresh_round_that_loads_nothing_new_is_quiet(monkeypatch):
    catalog = hms_catalog.HmsCatalog()
    calls = []

    async def scenario():  # network disabled (conftest): nothing loads
        catalog.start(lambda: [], on_change=lambda: calls.append(1))
        await asyncio.sleep(0.1)
        await catalog.stop()

    asyncio.run(scenario())
    assert calls == []


def test_the_manager_refreshes_the_catalogue_for_its_printers_in_the_background(monkeypatch, http):
    monkeypatch.setattr(hms_catalog, "CATALOG", hms_catalog.HmsCatalog())
    cfg = PrinterConfig(name="H2C", ip="10.0.0.3", access_code="x", serial=H2C_SERIAL)
    mgr = PrinterManager(AppConfig(printers=[cfg]), discovery_port=None)

    async def no_connect(self):  # no real connection attempts
        return False
    monkeypatch.setattr(PrinterConnection, "connect", no_connect)

    async def scenario():
        await mgr.start()
        for _ in range(200):
            if len(http.urls) >= 2:
                break
            await asyncio.sleep(0.01)
        await mgr.stop()
        return hms_catalog.CATALOG.running

    assert asyncio.run(scenario()) is False
    assert sorted(http.urls) == ["https://e.bambulab.com/query.php?lang=en",
                                 "https://e.bambulab.com/query.php?lang=en&d=31B"]


# --- Review fixes ------------------------------------------------------------------

def test_every_cache_file_is_loaded_before_any_fetch(http, monkeypatch):
    """A slow fetch mustn't delay the cached text of the next model."""
    refreshed([H2C_SERIAL])  # caches generic + 31B
    age_cache(hms_catalog.REFRESH_SECONDS + 1)  # both due again
    catalog = hms_catalog.HmsCatalog()
    seen = []

    def fetch_and_look(url, timeout, max_bytes):
        # When the generic refetch starts, the H2C cache is already loaded.
        seen.append(catalog._lookup("hms", "0500060000020001", "31B"))
        raise OSError("slow and then offline")
    monkeypatch.setattr(hms_catalog, "http_get", fetch_and_look)
    asyncio.run(catalog.refresh([H2C_SERIAL]))
    assert seen[0] == "H2C camera text."


@pytest.mark.parametrize("empty", [
    b'{"result":201,"t":1,"ver":0,"msg":""}',
    body(),  # result 0, empty lists
])
def test_an_empty_answer_never_replaces_a_cached_catalogue(http, empty, monkeypatch):
    refreshed([H2C_SERIAL])
    age_cache(hms_catalog.REFRESH_SECONDS + 1)
    http.bodies["31B"] = empty
    catalog = refreshed([H2C_SERIAL])
    [e] = described(catalog, H2C_SERIAL, CAMERA_GONE)
    assert e.description == "H2C camera text."
    saved = json.loads(open(os.path.join(cache_dir(), "en-31B.json"), encoding="utf-8").read())
    assert saved["hms"]  # the file wasn't overwritten either
    # ... and it's asked again sooner than a week, not on every round.
    http.urls.clear()
    asyncio.run(catalog.refresh([H2C_SERIAL]))
    assert not any("d=31B" in u for u in http.urls)
    now = time.monotonic()
    monkeypatch.setattr(hms_catalog.time, "monotonic", lambda: now + hms_catalog.EMPTY_RETRY_SECONDS + 1)
    asyncio.run(catalog.refresh([H2C_SERIAL]))
    assert any("d=31B" in u for u in http.urls)


def test_an_empty_answer_with_nothing_cached_is_kept_but_asked_again_within_a_day(http, monkeypatch):
    http.bodies["22E"] = b'{"result":201,"t":1,"ver":0,"msg":""}'
    catalog = refreshed([P2S_SERIAL])
    http.urls.clear()
    asyncio.run(catalog.refresh([P2S_SERIAL]))
    assert http.urls == []
    real = time.time()
    monkeypatch.setattr(hms_catalog.time, "time", lambda: real + hms_catalog.EMPTY_RETRY_SECONDS + 1)
    asyncio.run(catalog.refresh([P2S_SERIAL]))
    assert http.urls == ["https://e.bambulab.com/query.php?lang=en&d=22E"]


def test_http_get_gives_up_after_an_overall_deadline(monkeypatch):
    clock = [0.0]

    class SlowResp:
        def read(self, n=-1):
            clock[0] += 3  # each chunk arrives just inside the socket timeout
            return b"x" * 10
        def __enter__(self): return self
        def __exit__(self, *a): return False
    monkeypatch.setattr(hms_catalog.urllib.request, "urlopen", lambda req, timeout: SlowResp())
    monkeypatch.setattr(hms_catalog.time, "monotonic", lambda: clock[0])
    with pytest.raises(TimeoutError):
        hms_catalog._http_get("https://e.bambulab.com/query.php?lang=en", timeout=5, max_bytes=10_000)
