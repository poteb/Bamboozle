"""The report capture tool (tools/capture_reports.py): masking rules and the
requests it may send. All values here are made up."""
import ipaddress

from tools.capture_reports import Masker, mask_capture

SERIAL = "01P00A000000042"
AMS_SERIAL = "03C00A000000007"


def packed(ip: str) -> int:
    """`net.info[].ip` form: the address bytes read little-endian."""
    return int.from_bytes(ipaddress.IPv4Address(ip).packed, "little")


def test_serial_keeps_model_prefix_and_length():
    m = Masker([SERIAL])
    fake = m.serial(SERIAL)
    assert fake.startswith("01P") and "MASK" in fake
    assert len(fake) == len(SERIAL)
    assert m.serial(SERIAL.lower()) == fake  # same value, same mask


def test_serials_in_sn_fields_and_text_are_masked_consistently():
    m = Masker([SERIAL])
    report = {"print": {"upgrade_state": {"sn": SERIAL}},
              "info": {"module": [{"sn": AMS_SERIAL, "name": "ams/0"}]}}
    m.learn(report)
    out = m.mask(report)
    assert out["print"]["upgrade_state"]["sn"] == m.serial(SERIAL)
    ams_sn = out["info"]["module"][0]["sn"]
    assert ams_sn.startswith("03C") and ams_sn != AMS_SERIAL
    assert m.text(f"device/{SERIAL}/report") == f"device/{m.serial(SERIAL)}/report"


def test_private_ips_masked_in_text_and_packed_ints_but_versions_kept():
    m = Masker()
    out = m.mask({"rtsp_url": "rtsps://192.168.1.50:322/streaming/live/1",
                  "net": {"info": [{"ip": packed("192.168.1.60"), "mask": 16777215}, {"ip": 0, "mask": 0}]},
                  "sw_ver": "01.10.00.00", "hw": "1.2.3.4"})
    assert out["rtsp_url"] == "rtsps://192.0.2.1:322/streaming/live/1"
    assert out["net"]["info"][0] == {"ip": packed("192.0.2.2"), "mask": 16777215}
    assert out["net"]["info"][1] == {"ip": 0, "mask": 0}
    assert out["sw_ver"] == "01.10.00.00"
    assert out["hw"] == "1.2.3.4"  # public addresses aren't the user's


def test_cloud_ids_share_one_mapping_and_zero_is_kept():
    m = Masker()
    out = m.mask({"task_id": "1234567890", "project_id": "1234567890", "job_id": "1234567890",
                  "model_id": "US00aa11bb22cc3", "lan_task_id": "0", "batch_id": 0})
    assert out["task_id"] == out["project_id"] == out["job_id"] != "1234567890"
    assert out["task_id"].isdigit() and len(out["task_id"]) == 10
    assert out["model_id"].startswith("masked-")
    assert out["lan_task_id"] == "0" and out["batch_id"] == 0


def test_file_names_keep_directory_extension_and_job_link():
    m = Masker()
    out = m.mask({"subtask_name": "My Part (Copy)",
                  "url": "file:///userdata/My_Part (Copy).gcode.3mf",
                  "gcode_file": "/data/Metadata/plate_1.gcode"})
    assert out["subtask_name"] == "job-1"
    assert out["url"].startswith("file:///userdata/") and out["url"].endswith(".gcode.3mf")
    assert out["gcode_file"] == "/data/Metadata/plate_1.gcode"


def test_ssid_and_mac_masked():
    m = Masker()
    out = m.mask({"wifi_ssid": "Home Net", "note": "mac AA:BB:CC:DD:EE:FF"})
    assert out["wifi_ssid"] == "masked-ssid"
    assert out["note"] == "mac 02:00:00:00:00:01"


def test_mask_capture_writes_header_and_drops_events():
    raw = [
        {"kind": "raw-header", "name": "P1S", "serial": SERIAL, "ip": "192.168.1.70",
         "model": "C12", "started_utc": "2026-10-25T12:00:00.000+00:00"},
        {"t": 0.9, "utc": "2026-10-25T12:00:01.000+00:00", "dir": "event", "event": "connack"},
        {"t": 1.0, "utc": "2026-10-25T12:00:01.100+00:00", "dir": "out",
         "topic": f"device/{SERIAL}/request", "payload": {"pushing": {"command": "pushall"}}},
        {"t": 1.5, "utc": "2026-10-25T12:00:01.600+00:00", "dir": "in",
         "topic": f"device/{SERIAL}/report", "payload": {"print": {"sn": SERIAL}}},
    ]
    m = Masker([SERIAL])
    header, *lines = mask_capture(raw, m, "C12")
    assert header["kind"] == "header" and header["model"] == "C12"
    assert header["serial"] == m.serial(SERIAL)
    assert header["masked_keys"] == ["sn"]
    assert [line["dir"] for line in lines] == ["out", "in"]
    assert all(SERIAL not in str(line) for line in lines)
    assert "ip" not in header and "utc" not in lines[0]


def test_presigned_cloud_url_is_replaced_whole():
    m = Masker()
    url = ("https://bucket.s3.example.com/users/1111111111/models/x/"
           "US00aa11bb22cc3_2222222222_1.3mf?X-Amz-Credential=AKIA%2F&X-Amz-Signature=abcd")
    assert m.mask({"url": url}) == {"url": "https://masked.invalid/job-1.3mf"}


def test_serials_under_other_keys_and_chip_ids():
    m = Masker([SERIAL])
    out = m.mask({"dev_id": SERIAL, "ams_id": "00600A000000099", "chip_id": "0a0b0c0d0e",
                  "ams_mapping2": [{"ams_id": 0, "slot_id": 1}]})
    assert out["dev_id"] == m.serial(SERIAL)
    assert out["ams_id"].startswith("006") and "MASK" in out["ams_id"]
    assert out["chip_id"] == "0000000001"
    assert out["ams_mapping2"] == [{"ams_id": 0, "slot_id": 1}]  # a unit number, not a serial


def test_int_and_string_forms_of_a_cloud_id_match():
    m = Masker()
    out = m.mask({"task_id": "1234567891", "job_id": 1234567891})
    assert out["job_id"] == int(out["task_id"]) != 1234567891


def test_placeholder_sn_is_kept_and_not_masked_elsewhere():
    m = Masker()
    report = {"module": [{"sn": "N/A", "hw_ver": "N/A"}]}
    m.learn(report)
    assert m.mask(report) == report


def test_only_standalone_pushall_and_get_version_may_be_sent():
    from bamboozle import mqtt
    from tools.capture_reports import _allowed_requests

    for ok in (mqtt.pushall_request(), mqtt.version_request()):
        assert _allowed_requests(ok) == [ok]
    for bad in ([*mqtt.light_requests("on")][0],
                {**mqtt.pushall_request(), **mqtt.version_request()},
                {"print": {"command": "pause", "sequence_id": "1"}},
                {"pushing": {"command": "pushall", "sequence_id": "1", "extra": 1}}):
        assert _allowed_requests(bad) == []
