"""The pure decoders (protocol.py, report.py, hms.py) over real captured
reports (fixtures/reports/, see its README). Facts asserted here are what the
user's printers sent on 2026-10-25."""
import ipaddress
import json
import re

import pytest

from bamboozle import hms, protocol, report
from report_fixtures import FIXTURE_DIR, PRINTERS, load

MODELS = {"a1": "N2S", "p1s": "C12", "h2c": "O1C2", "p2s": "N7"}
VERSION_RE = re.compile(r"^\d\d\.\d\d\.\d\d\.\d\d$")
REFUSED_COMMAND = "0500050000010007"
SERIAL_KEYS = frozenset({"sn", "dev_id", "ams_id"})
CLOUD_ID_KEYS = frozenset({"task_id", "subtask_id", "project_id", "job_id", "profile_id",
                           "design_id", "model_id", "lan_task_id"})
# What the Masker writes: 0 / "" (none), 10...0N (same length), N, or masked-N.
FAKE_ID_RE = re.compile(r"|0|10+\d{1,2}|\d{1,2}|masked-\d+")
FAKE_CHIP_RE = re.compile(r"0+[0-9a-f]{1,3}")
IPV4_RE = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d{1,3}){3})(?![\d.])")


# --- The fixtures themselves --------------------------------------------------

@pytest.mark.parametrize("name", PRINTERS)
def test_header_and_masked_identity(name):
    cap = load(name)
    assert cap.header["format"] == 1
    assert cap.model == MODELS[name]
    assert "MASK" in cap.serial
    assert all(cap.serial in m.topic for m in cap.messages)


@pytest.mark.parametrize("name", PRINTERS)
def test_no_unmasked_serials_or_lan_ips(name):
    def walk(obj, key=""):
        if isinstance(obj, dict):
            for k, v in obj.items():
                yield from walk(v, k)
        elif isinstance(obj, list):
            for v in obj:
                yield from walk(v, key)
        else:
            yield key, obj

    for m in load(name).messages:
        for key, value in walk(m.payload):
            if key in SERIAL_KEYS and isinstance(value, str) and len(value) >= 9:
                assert "MASK" in value, (key, value)
            if key in CLOUD_ID_KEYS:
                assert FAKE_ID_RE.fullmatch(str(value)), (key, value)
            if key == "chip_id":
                assert FAKE_CHIP_RE.fullmatch(value), value
            if isinstance(value, str) and "://" in value:
                assert value.startswith(("https://masked.invalid/", "rtsps://192.0.2.")), value
        for ip in IPV4_RE.findall(json.dumps(m.payload)):
            try:
                addr = ipaddress.IPv4Address(ip)
            except ValueError:  # version strings like 01.10.00.00
                continue
            assert not addr.is_private or ip.startswith("192.0.2."), ip


@pytest.mark.parametrize("name", PRINTERS)
def test_capture_sent_only_standalone_pushall_and_get_version(name):
    sent = [m.payload for m in load(name).messages if m.dir == "out"]
    assert sent, "the capture sent its requests"
    for payload in sent:
        assert len(payload) == 1
        (section, body), = payload.items()
        assert (section, body["command"]) in {("pushing", "pushall"), ("info", "get_version")}


# --- get_version -------------------------------------------------------------

@pytest.mark.parametrize("name", PRINTERS)
def test_firmware_from_get_version_reply(name):
    assert VERSION_RE.match(report.firmware(load(name).version()))


# --- Decoding the full report -------------------------------------------------

@pytest.mark.parametrize("name", PRINTERS)
def test_full_report_decodes(name):
    cap = load(name)
    full = cap.full_report()
    fields = report.status_fields(full, cap.version(), cap.model)
    assert fields["bed_temp"] is not None
    assert fields["nozzles"] and all(n.temp is not None for n in fields["nozzles"])
    assert fields["wifi_dbm"] is not None and fields["wifi_dbm"] < 0
    assert fields["firmware"]
    assert isinstance(fields["sdcard"], bool)
    assert fields["ams_units"]


def test_h2c_reports_two_nozzles_left_then_right():
    cap = load("h2c")
    nozzles = report.status_fields(cap.full_report(), None, cap.model)["nozzles"]
    assert [n.side for n in nozzles] == ["left", "right"]


@pytest.mark.parametrize("name, has_chamber", [("a1", False), ("p1s", False), ("h2c", True), ("p2s", True)])
def test_chamber_only_on_printers_with_a_sensor(name, has_chamber):
    cap = load(name)
    temp, _ = report.chamber(cap.full_report(), cap.model)
    assert (temp is not None) == has_chamber


@pytest.mark.parametrize("name, expected", [("a1", None), ("p1s", None), ("h2c", False), ("p2s", False)])
def test_developer_mode_from_fun(name, expected):
    assert protocol.developer_mode(load(name).full_report()) is expected


@pytest.mark.parametrize("name, has_record", [("a1", False), ("p1s", False), ("h2c", True), ("p2s", True)])
def test_refused_command_record_is_decoded_but_never_active(name, has_record):
    """The H2C / P2S carry HMS 0500050000010007; the A1 / P1S had no HMS at all."""
    errors = hms.printer_errors(load(name).full_report())
    refused = [e for e in errors if e.code.replace("_", "") == REFUSED_COMMAND]
    assert len(refused) == has_record
    assert all(not e.active and e.severity == "" for e in refused)
    assert not any(e.active for e in errors)  # every printer was idle, nothing wrong


def test_h2c_p2s_lingering_hms_are_decoded():
    codes = {n: {e.code for e in hms.printer_errors(load(n).full_report())} for n in ("h2c", "p2s")}
    assert {"0500_0600_0002_0006", "0500_0600_0002_0052"} <= codes["h2c"]
    assert "0501_0400_0003_0002" in codes["p2s"]


# --- Every push, full and partial ---------------------------------------------

@pytest.mark.parametrize("name", PRINTERS)
def test_every_push_decodes_on_its_own(name):
    """Partial pushes are what a merge sees; the decoders must take any of
    them alone without raising."""
    cap = load(name)
    for m in cap.status_pushes():
        report.status_fields(m.print, None, cap.model)
        hms.printer_errors(m.print)
        protocol.developer_mode(m.print)
        protocol.file_on_internal_storage(m.print)


# --- Shapes the client swap must handle (replace-bambulabs-api) ----------------

@pytest.mark.parametrize("name", ["a1", "p1s"])
def test_a1_p1s_send_small_deltas_and_full_reports_only_on_request(name):
    cap = load(name)
    pushall_at = next(m.t for m in cap.messages if m.dir == "out" and "pushing" in m.payload)
    full = cap.full_reports()
    # Ours, plus any pushall another client (Bambu Studio, on the A1) sent.
    assert full and full[0].t > pushall_at
    partial = cap.partial_reports()
    assert len(partial) > 100
    assert all(m.print.get("msg") == 1 and len(m.print) < 30 for m in partial)


@pytest.mark.parametrize("name", ["h2c", "p2s"])
def test_h2c_p2s_push_full_state_every_second(name):
    """Observed with Bambu Studio connected to both; without a client that
    asked for pushes they may send deltas like the others."""
    cap = load(name)
    assert not cap.partial_reports()
    assert len(cap.full_reports()) > 400


def test_a1_partial_ams_push_without_the_unit_list():
    """A shallow merge (`print.ams = partial`) would drop every tray here."""
    pushes = [m.print["ams"] for m in load("a1").partial_reports() if "ams" in m.print]
    assert any("ams" not in ams and "tray_exist_bits" in ams for ams in pushes)


def test_p1s_partial_ams_push_without_the_tray_bits():
    """The units come alone: `tray_exist_bits`, `tray_now`, ... must survive."""
    full_ams = load("p1s").full_report()["ams"]
    assert {"tray_exist_bits", "tray_now"} <= set(full_ams)
    pushes = [m.print["ams"] for m in load("p1s").partial_reports() if "ams" in m.print]
    assert pushes and all(set(ams) == {"ams"} for ams in pushes)


def test_a1_print_start_sequence():
    """A print started from Bambu Studio mid-capture: a `project_file` echo
    (not a status frame) and gcode_state FINISH -> PREPARE -> RUNNING in deltas."""
    cap = load("a1")
    echoes = [m for m in cap.reports() if m.print and m.print.get("command") == "project_file"]
    assert len(echoes) == 1 and echoes[0].print["url"].startswith("https://masked.invalid/")
    states = [m.print["gcode_state"] for m in cap.status_pushes() if "gcode_state" in m.print]
    assert states[0] == "FINISH" and states[-2:] == ["PREPARE", "RUNNING"]
