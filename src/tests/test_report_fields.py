"""Readable status decoded from the raw device report (bamboozle.report).

Payload shapes are the ones the user's printers sent to a pushall on
2026-10-25 (A1 printing, P1S finished, H2C printing on its right nozzle,
P2S finished), cut down to the fields under test.
"""
import asyncio

import pytest

from bamboozle import report
from bamboozle.models import PrinterConfig
from bamboozle.printer_manager import PrinterConnection

A1 = "N2S"
P1S = "C12"
H2C = "O1C2"
P2S = "N7"
X1C = "BL-P001"


def h2(target, current):
    """The H2 series' encoding for a heater with a target: target<<16 | current."""
    return target * 65536 + current


# --- Temperatures -------------------------------------------------------

@pytest.mark.parametrize("value, expected", [
    (14418140, (220.0, 220.0)),          # H2C right nozzle, printing
    (1638450, (50.0, 25.0)),             # H2C left nozzle: 50 now, target 25
    (3604535, (55.0, 55.0)),             # H2C bed
    (30, (30.0, None)),                  # heater off: plain degrees
    (219.90625, (219.90625, None)),      # flat field, float
    ("28", (28.0, None)),
    (None, (None, None)),
    ("junk", (None, None)),
    (True, (None, None)),
])
def test_split_temp(value, expected):
    assert report.split_temp(value) == expected


def test_single_nozzle_from_flat_fields():
    p = {"nozzle_temper": 219.90625, "nozzle_target_temper": 220,
         "nozzle_type": "stainless_steel", "nozzle_diameter": "0.4"}
    [n] = report.nozzles(p)
    assert (n.side, n.temp, n.target) == ("", 219.90625, 220.0)
    assert n.type == "Stainless steel"
    assert n.diameter == 0.4


H2C_DEVICE = {
    "extruder": {"info": [
        {"id": 0, "hnow": 0, "temp": h2(220, 220)},   # right, printing
        {"id": 1, "hnow": 1, "temp": h2(25, 50)},     # left
    ], "state": 2},
    "nozzle": {"info": [
        {"id": 21, "type": "HS01", "diameter": 0.4},  # in the nozzle rack
        {"id": 1, "type": "HH01", "diameter": 0.6},
        {"id": 0, "type": "HS01", "diameter": 0.4},
    ]},
    "bed": {"info": {"temp": h2(55, 55)}},
    "ctc": {"info": {"temp": 30}},
}


def test_dual_nozzles_left_then_right_with_targets():
    # The flat fields mirror one nozzle only; the extruder list has both.
    p = {"nozzle_temper": 50.0, "nozzle_target_temper": 25.0,
         "nozzle_type": "HS01-0.4", "device": H2C_DEVICE}
    left, right = report.nozzles(p)
    assert (left.side, left.temp, left.target) == ("left", 50.0, 25.0)
    assert (right.side, right.temp, right.target) == ("right", 220.0, 220.0)
    # Type and diameter come from the nozzle mounted on each extruder.
    assert (left.type, left.diameter) == ("HH01", 0.6)
    assert (right.type, right.diameter) == ("HS01", 0.4)


def test_single_extruder_device_list_is_one_nozzle():
    """The P2S sends a one-entry extruder list next to the flat fields."""
    p = {"nozzle_temper": 28.0, "nozzle_target_temper": 0.0,
         "nozzle_type": "HS01", "nozzle_diameter": "0.4",
         "device": {"extruder": {"info": [{"id": 0, "temp": 28}]}}}
    [n] = report.nozzles(p)
    assert (n.side, n.temp, n.target, n.type, n.diameter) == ("", 28.0, 0.0, "HS01", 0.4)


def test_single_nozzle_falls_back_to_the_extruder_list():
    p = {"device": {"extruder": {"info": [{"id": 0, "temp": h2(210, 180)}]}}}
    [n] = report.nozzles(p)
    assert (n.temp, n.target) == (180.0, 210.0)


def test_no_nozzle_data_is_an_empty_list():
    assert report.nozzles({}) == []


@pytest.mark.parametrize("raw, diameter, expected", [
    ("hardened_steel", 0.4, "Hardened steel"),
    ("stainless_steel", 0.4, "Stainless steel"),
    ("tungsten_carbide", 0.4, "Tungsten carbide"),
    ("HS01-0.4", 0.4, "HS01"),         # the diameter suffix is shown separately
    ("HS01", 0.4, "HS01"),             # H2 / P2S codes are shown as sent
    ("", None, ""),
    (None, None, ""),
])
def test_nozzle_type_name(raw, diameter, expected):
    assert report.nozzle_type_name(raw, diameter) == expected


def test_bed_from_flat_fields_then_device():
    assert report.bed({"bed_temper": 65.03125, "bed_target_temper": 65}) == (65.03125, 65.0)
    assert report.bed({"device": {"bed": {"info": {"temp": h2(55, 54)}}}}) == (54.0, 55.0)
    assert report.bed({}) == (None, None)


@pytest.mark.parametrize("payload, model, expected", [
    # A1 / P1 have no chamber sensor: their `chamber_temper` (5) is junk.
    ({"chamber_temper": 5}, A1, (None, None)),
    ({"chamber_temper": 5}, P1S, (None, None)),
    ({"chamber_temper": 5}, "", (None, None)),
    # H2 / P2S report it under device.ctc, encoded while heating.
    ({"device": {"ctc": {"info": {"temp": 30}}}}, H2C, (30.0, None)),
    ({"device": {"ctc": {"info": {"temp": h2(45, 38)}}}}, H2C, (38.0, 45.0)),
    ({"device": {"ctc": {"info": {"temp": 26}}}}, "", (26.0, None)),
    ({"chamber_temper": h2(40, 35)}, H2C, (35.0, 40.0)),
    # X1 family: the flat field is real.
    ({"chamber_temper": 31, "mc_target_cham": 0}, X1C, (31.0, 0.0)),
])
def test_chamber(payload, model, expected):
    assert report.chamber(payload, model) == expected


# --- Stage ----------------------------------------------------------------

@pytest.mark.parametrize("stg_cur, gcode_state, expected", [
    (2, "RUNNING", 2),
    (0, "RUNNING", 0),
    (0, "PAUSE", 0),
    # A1 / P1 report 0 while idle: only trust it during a job.
    (0, "IDLE", None),
    (0, "FINISH", None),
    (0, "PREPARE", None),
    (-1, "FINISH", None),
    (255, "FINISH", None),
    (None, "RUNNING", None),
    ("7", "RUNNING", 7),
])
def test_stage(stg_cur, gcode_state, expected):
    p = {"gcode_state": gcode_state}
    if stg_cur is not None:
        p["stg_cur"] = stg_cur
    assert report.stage(p) == expected


@pytest.mark.parametrize("stage, gcode_state, expected", [
    (0, "RUNNING", "Printing"),
    (1, "RUNNING", "Auto bed leveling"),
    (2, "PREPARE", "Preheating bed"),
    (7, "RUNNING", "Heating nozzle"),
    (14, "RUNNING", "Cleaning nozzle tip"),
    (16, "PAUSE", "Paused by user"),
    (None, "RUNNING", ""),
    (999, "RUNNING", "Preparing"),     # a stage newer than our table
    (999, "PAUSE", ""),                # ... but not while paused
])
def test_stage_name(stage, gcode_state, expected):
    assert report.stage_name(stage, gcode_state) == expected


def test_stage_names_cover_the_documented_range():
    assert set(report.STAGE_NAMES) == set(range(0, 67))
    assert all(name and len(name) <= 32 for name in report.STAGE_NAMES.values())


# --- Fans ---------------------------------------------------------------

@pytest.mark.parametrize("value, expected", [
    ("0", 0), ("15", 100), ("11", 70), ("4", 30), (14, 90), (None, None), ("x", None), (99, 100),
])
def test_fan_percent(value, expected):
    assert report.fan_percent(value) == expected


def test_fans():
    p = {"cooling_fan_speed": "14", "big_fan1_speed": "11",
         "big_fan2_speed": "4", "heatbreak_fan_speed": "14"}
    f = report.fans(p, H2C)
    assert (f.part, f.aux, f.chamber, f.heatbreak) == (90, 70, 30, 90)


def test_a1_has_no_aux_or_chamber_fan():
    """The A1 echoes the part fan value in big_fan1/2; it has neither fan."""
    p = {"cooling_fan_speed": "11", "big_fan1_speed": "11",
         "big_fan2_speed": "11", "heatbreak_fan_speed": "10"}
    f = report.fans(p, A1)
    assert (f.part, f.aux, f.chamber, f.heatbreak) == (70, None, None, 70)


# --- AMS ----------------------------------------------------------------

def test_ams_units_use_the_raw_humidity_not_the_index():
    p = {"ams": {"ams": [
        {"id": "0", "humidity": "2", "humidity_raw": "35", "temp": "28.0", "tray": []},
        {"id": "1", "humidity": "2", "humidity_raw": "36", "temp": "25.4"},
        {"id": "128", "humidity": "0", "humidity_raw": "0", "temp": "27.0"},
    ]}}
    units = report.ams_units(p)
    assert [(u.index, u.name, u.humidity, u.temp) for u in units] == [
        (0, "AMS 1", 35, 28.0),
        (1, "AMS 2", 36, 25.4),
        (128, "AMS HT 1", None, 27.0),   # 0 % means no reading
    ]


def test_ams_lite_without_sensors():
    """The A1's AMS Lite sends index 5 (would read as "dry"), raw 0, temp 0.0."""
    p = {"ams": {"ams": [{"id": "0", "humidity": "5", "humidity_raw": "0", "temp": "0.0"}]}}
    [u] = report.ams_units(p)
    assert (u.humidity, u.temp) == (None, None)


def test_old_firmware_without_humidity_raw_shows_no_humidity():
    p = {"ams": {"ams": [{"id": "0", "humidity": "4", "temp": "24.0"}]}}
    [u] = report.ams_units(p)
    assert (u.humidity, u.temp) == (None, 24.0)


@pytest.mark.parametrize("payload", [{}, {"ams": None}, {"ams": {"ams": "x"}}, {"ams": {"ams": [5]}}])
def test_ams_units_tolerate_missing_or_malformed(payload):
    assert report.ams_units(payload) == []


@pytest.mark.parametrize("index, expected", [(0, "AMS 1"), (3, "AMS 4"), (128, "AMS HT 1"), (129, "AMS HT 2")])
def test_ams_name(index, expected):
    assert report.ams_name(index) == expected


# --- Misc -----------------------------------------------------------------

@pytest.mark.parametrize("value, expected", [
    ("-53dBm", -53), ("-74dBm", -74), (-60, -60), ("", None), (None, None), ("junk", None),
])
def test_wifi_dbm(value, expected):
    assert report.wifi_dbm({"wifi_signal": value}) == expected


@pytest.mark.parametrize("payload, model, expected", [
    ({"stat": "1638218070"}, H2C, False),          # hex; bit 23 clear
    ({"stat": "258000"}, P2S, False),
    ({"stat": "800000"}, P2S, True),
    ({"stat": "1638A18070"}, H2C, True),
    # X1 reports the door in home_flag bit 23.
    ({"home_flag": 0x00800000}, X1C, True),
    ({"home_flag": 0}, X1C, False),
    # A1 / P1S: no door sensor reported (home_flag bit 23 means something else).
    ({"home_flag": 0x00800000}, A1, None),
    ({"home_flag": 847201727}, P1S, None),
    ({}, "", None),
])
def test_door_open(payload, model, expected):
    assert report.door_open(payload, model) is expected


@pytest.mark.parametrize("value, expected", [(True, True), (False, False), (None, None), ("x", None)])
def test_sdcard(value, expected):
    p = {} if value is None else {"sdcard": value}
    assert report.sdcard(p) is expected


def test_firmware_from_the_ota_module():
    info = {"command": "get_version", "module": [
        {"name": "esp32", "sw_ver": "01.16.33.15"},
        {"name": "ota", "sw_ver": "01.08.01.00", "product_name": "Bambu Lab A1"},
    ]}
    assert report.firmware(info) == "01.08.01.00"
    assert report.firmware({}) == ""
    assert report.firmware(None) == ""
    assert report.firmware({"module": "x"}) == ""


@pytest.mark.parametrize("lvl, expected", [
    (1, "Silent"), (2, "Standard"), (3, "Sport"), (4, "Ludicrous"), (9, ""), (None, ""),
])
def test_speed_name(lvl, expected):
    assert report.speed_name(lvl) == expected


def test_status_fields_tolerates_an_empty_report():
    fields = report.status_fields({}, None, "")
    assert fields["nozzles"] == []
    assert fields["stage"] is None
    assert fields["stage_name"] == ""
    assert fields["chamber_temp"] is None
    assert fields["firmware"] == ""


# --- Wiring into the poll ---------------------------------------------------

class FakeMqttClient:
    def is_connected(self):
        return True


class DumpOnlyPrinter:
    """Only `mqtt_dump()`; every library getter raises."""

    def __init__(self, dump):
        self._dump = dump
        self.mqtt_client = FakeMqttClient()

    def mqtt_dump(self):
        return self._dump


def _read(dump, model=""):
    conn = PrinterConnection(PrinterConfig(
        name="P", ip="192.168.0.1", access_code="x", serial="S", model=model))
    conn.printer = DumpOnlyPrinter(dump)
    conn._connected = True
    return asyncio.run(conn.read_state())


def test_state_carries_the_decoded_h2c_status():
    state = _read({
        "print": {
            "gcode_state": "RUNNING", "stg_cur": 7, "spd_lvl": 3, "spd_mag": 124,
            "nozzle_temper": 50.0, "nozzle_target_temper": 25.0,
            "bed_temper": 55.0, "bed_target_temper": 55.0,
            "cooling_fan_speed": "15", "wifi_signal": "-49dBm", "sdcard": False,
            "stat": "800000", "device": H2C_DEVICE,
            "ams": {"ams": [{"id": "0", "humidity_raw": "35", "temp": "28.0"}]},
        },
        "info": {"module": [{"name": "ota", "sw_ver": "01.01.02.00"}]},
    }, model=H2C)
    assert state.online
    assert [n.side for n in state.nozzles] == ["left", "right"]
    assert (state.bed_temp, state.bed_target) == (55.0, 55.0)
    assert (state.chamber_temp, state.chamber_target) == (30.0, None)
    assert (state.stage, state.stage_name) == (7, "Heating nozzle")
    assert (state.speed_level, state.speed_name) == (3, "Sport")
    assert state.fans.part == 100
    assert state.ams_units[0].humidity == 35
    assert state.wifi_dbm == -49
    assert state.door_open is True
    assert state.sdcard is False
    assert state.firmware == "01.01.02.00"
    assert "print_status" not in state.model_dump()


def test_state_hides_the_a1_chamber_and_idle_stage():
    state = _read({"print": {
        "gcode_state": "FINISH", "stg_cur": 0, "chamber_temper": 5,
        "nozzle_temper": 28.875, "nozzle_target_temper": 0,
        "bed_temper": 26, "bed_target_temper": 0,
    }}, model=A1)
    assert state.online
    assert (state.nozzle_temp, state.nozzle_target) == (28.875, 0.0)
    assert state.chamber_temp is None
    assert (state.stage, state.stage_name) == (None, "")


# --- Firmware version request -------------------------------------------------

def _recording_conn():
    import bambulabs_api as bl
    from test_light_refresh import RecordingPaho

    printer = bl.Printer("192.168.0.102", "x", "S")
    printer.mqtt_client._client = RecordingPaho()
    printer.mqtt_client._data = {"print": {"gcode_state": "IDLE", "nozzle_temper": 25.0}}
    conn = PrinterConnection(PrinterConfig(name="P", ip="192.168.0.102", access_code="x", serial="S"))
    conn.printer = printer
    conn._connected = True
    return conn


def _version_requests(conn):
    return [p for _, p, _ in conn.printer.mqtt_client._client.published if "info" in p]


def test_firmware_is_asked_for_until_the_printer_answers(monkeypatch):
    """The library's connect-time get_version shares a message with pushall,
    which the printers ignore, so the poll asks on its own."""
    conn = _recording_conn()
    asyncio.run(conn.read_state())
    asyncio.run(conn.read_state())
    [req] = _version_requests(conn)
    assert req["info"]["command"] == "get_version"
    assert isinstance(req["info"]["sequence_id"], str)

    monkeypatch.setattr(PrinterConnection, "VERSION_RETRY_SECONDS", 0.0)
    asyncio.run(conn.read_state())
    assert len(_version_requests(conn)) == 2

    conn.printer.mqtt_client._data["info"] = {"module": [{"name": "ota", "sw_ver": "01.08.01.00"}]}
    state = asyncio.run(conn.read_state())
    assert state.firmware == "01.08.01.00"
    assert len(_version_requests(conn)) == 2


def test_no_version_request_while_offline():
    conn = _recording_conn()
    conn.printer.mqtt_client._data = {}
    state = asyncio.run(conn.read_state())
    assert not state.online
    assert _version_requests(conn) == []
