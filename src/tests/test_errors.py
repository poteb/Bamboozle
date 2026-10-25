"""HMS and print_error codes decoded from the raw report (bamboozle.hms).

The H2C entries are the ones the user's H2C carried on 2026-10-25 while idle:
0500050000010007 (a refused LAN command, lingering) and 0500060000020006.
"""
import asyncio

import pytest

from bamboozle import hms
from bamboozle.models import PrinterConfig
from bamboozle.printer_manager import PrinterConnection

VERIFY_FAILED = {"attr": 0x05000500, "code": 0x00010007}
H2C_OTHER = {"attr": 83887616, "code": 131078}            # 0500060000020006
AMS2_RUNOUT = {"attr": 0x07012000, "code": 0x00020001}     # AMS unit 2, serious
TOOLHEAD_NOTE = {"attr": "08001000", "code": "00030002"}   # hex-string form, common


def codes(errors):
    return [e.code for e in errors]


# --- HMS ------------------------------------------------------------------------

def test_hms_code_is_shown_as_on_the_printer_screen_with_a_wiki_link():
    [e] = hms.hms_errors({"hms": [AMS2_RUNOUT]})
    assert e.kind == "hms"
    assert e.code == "0701_2000_0002_0001"
    # Bambu's own HMS link (what Bambu Studio opens): it redirects to the
    # code's wiki page, or to the HMS home when the wiki has no page.
    assert e.url == "https://e.bambulab.com/index.php?e=0701200000020001&s=device_hms&lang=en"


@pytest.mark.parametrize("entry, severity", [
    ({"attr": 0x03000100, "code": 0x00010005}, "fatal"),
    (AMS2_RUNOUT, "serious"),
    (TOOLHEAD_NOTE, "common"),
    ({"attr": 0x0C000200, "code": 0x00040001}, "info"),
    ({"attr": 0x0C000200, "code": 0x00090001}, ""),        # level we don't know
])
def test_hms_severity_is_the_high_half_of_code(entry, severity):
    [e] = hms.hms_errors({"hms": [entry]})
    assert e.severity == severity


def test_hms_level_zero_is_not_a_message():
    """Level 0 is invalid: status values some firmware sends while printing."""
    assert hms.hms_errors({"hms": [{"attr": 0x03000100, "code": 0x00000002},
                                   {"attr": 0x03000100, "code": 0x0000400C}]}) == []


@pytest.mark.parametrize("attr, module", [
    (0x03000100, "Motion controller"),
    (0x05000500, "Mainboard"),
    (0x07002000, "AMS 1"),
    (0x07032300, "AMS 4"),
    (0x07FF2000, "External spool"),
    (0x08001000, "Toolhead"),
    (0x0C000200, "Camera / lidar"),
    (0x18002000, "AMS HT 1"),
    (0x18812000, "AMS HT 2"),
    (0x10010000, ""),                                       # unknown module
])
def test_module_from_the_top_bytes_of_attr(attr, module):
    [e] = hms.hms_errors({"hms": [{"attr": attr, "code": 0x00020001}]})
    assert e.module == module


def test_malformed_hms_entries_are_skipped():
    assert hms.hms_errors({"hms": [{"attr": "junk", "code": None}, 5, "x"]}) == []
    assert hms.hms_errors({"hms": "nope"}) == []
    assert hms.hms_errors({}) == []


def test_user_cancel_echo_in_hms_is_ignored():
    assert hms.hms_errors({"hms": [{"attr": 0x03000000, "code": 0x0002400C},
                                   {"attr": 0x05000000, "code": 0x0001400E}]}) == []


def test_refused_command_record_has_our_own_description():
    [e] = hms.hms_errors({"hms": [VERIFY_FAILED]})
    assert e.code == "0500_0500_0001_0007"
    assert "Developer Mode" in e.description


# --- print_error ------------------------------------------------------------------

def test_print_error_is_module_and_error_with_a_link_to_the_error_code_table():
    e = hms.print_error({"print_error": 0x03008003})
    assert e.kind == "print_error"
    assert e.code == "0300_8003"
    assert e.module == "Motion controller"
    # The wiki's error-code table has no per-code anchors: a text fragment
    # scrolls to the code (its table spells it 0300-8003).
    assert e.url == "https://wiki.bambulab.com/en/hms/error-code#:~:text=0300-8003"


@pytest.mark.parametrize("value, severity", [
    (0x03004000, "fatal"),      # 4xxx: the task was stopped
    (0x03008003, "serious"),    # 8xxx: the task was paused
    (0x0500C010, "common"),     # Cxxx: a prompt
])
def test_print_error_severity_from_the_error_half(value, severity):
    assert hms.print_error({"print_error": value}).severity == severity


@pytest.mark.parametrize("value", [
    0, None, "junk", True,
    0x03000002,                 # below 0x4000: a status value, not an error
    0x0300400C, 0x0500400E,     # a user cancel echoes these
])
def test_print_error_none(value):
    assert hms.print_error({"print_error": value}) is None


def test_print_error_accepts_a_decimal_string():
    assert hms.print_error({"print_error": str(0x03008003)}).code == "0300_8003"


# --- Both, and which ones are active ---------------------------------------------

def test_idle_printer_messages_are_lingering_not_active():
    """An idle printer keeps HMS messages until they're acknowledged on it."""
    errors = hms.printer_errors({"gcode_state": "IDLE", "hms": [H2C_OTHER, VERIFY_FAILED]})
    assert sorted(codes(errors)) == ["0500_0500_0001_0007", "0500_0600_0002_0006"]
    assert not any(e.active for e in errors)


@pytest.mark.parametrize("state", ["PAUSE", "FAILED"])
def test_messages_explain_a_paused_or_failed_job(state):
    errors = hms.printer_errors({"gcode_state": state, "hms": [AMS2_RUNOUT],
                                 "print_error": 0x07018011})
    assert codes(errors) == ["0701_2000_0002_0001", "0701_8011"]
    assert all(e.active for e in errors)


def test_running_job_messages_are_not_active():
    [e] = hms.printer_errors({"gcode_state": "RUNNING", "hms": [TOOLHEAD_NOTE]})
    assert not e.active


def test_refused_command_record_is_never_active():
    """0500050000010007 records a refused LAN command; the H2C and P2S carry it
    while the light works, so it never explains a paused job."""
    errors = hms.printer_errors({"gcode_state": "PAUSE", "hms": [VERIFY_FAILED, AMS2_RUNOUT]})
    assert [(e.code, e.active) for e in errors] == [
        ("0701_2000_0002_0001", True), ("0500_0500_0001_0007", False)]


def test_print_error_already_raised_as_hms_is_listed_once():
    errors = hms.printer_errors({"gcode_state": "PAUSE",
                                 "hms": [{"attr": 0x03000300, "code": 0x00028003}],
                                 "print_error": 0x03008003})
    assert codes(errors) == ["0300_0300_0002_8003"]


def test_order_active_first_then_by_severity():
    errors = hms.printer_errors({"gcode_state": "IDLE", "hms": [
        TOOLHEAD_NOTE, AMS2_RUNOUT, {"attr": 0x03000100, "code": 0x00010005}]})
    assert [e.severity for e in errors] == ["fatal", "serious", "common"]


def test_no_errors():
    assert hms.printer_errors({"gcode_state": "IDLE", "hms": [], "print_error": 0}) == []


# --- On the printer state ------------------------------------------------------

class FakeMqttClient:
    def is_connected(self):
        return True


class DumpOnlyPrinter:
    """Only `mqtt_dump()`; every library getter raises, so _read_sync defaults."""

    def __init__(self, print_payload):
        self._payload = print_payload
        self._client = FakeMqttClient()

    def mqtt_dump(self):
        return {"print": self._payload}

    def __getattr__(self, name):
        def fail(*a, **k):
            raise RuntimeError("not available")
        return fail


def test_printer_state_carries_the_errors():
    cfg = PrinterConfig(name="H2C", ip="10.0.0.3", access_code="x", serial="S", model="O1C2")
    conn = PrinterConnection(cfg)
    conn.printer = DumpOnlyPrinter({"gcode_state": "PAUSE", "nozzle_temper": 25,
                                    "hms": [AMS2_RUNOUT]})
    state, _ = conn._read_sync()
    assert [(e.code, e.active) for e in state.errors] == [("0701_2000_0002_0001", True)]
    dumped = state.model_dump(mode="json")["errors"][0]
    assert dumped["module"] == "AMS 2"
    assert dumped["url"].startswith("https://e.bambulab.com/")
