"""Command payloads and their confirmation rules. These payloads are never
sent to a real printer by the tests.

The payloads are the same for the A1, P1S, H2C and P2S (OpenBambuAPI
`mqtt.md`; Bambu Studio's DeviceManager has no model branch for them), so
`build` takes no model."""
import pytest

from bamboozle import commands
from bamboozle.merge import Frame

def _seq_free(payload):
    """The payload without its sequence_id (it increments)."""
    (section, body), = payload.items()
    assert isinstance(body["sequence_id"], str) and body["sequence_id"].isdigit()
    return {section: {k: v for k, v in body.items() if k != "sequence_id"}}


@pytest.mark.parametrize("action,params,expected", [
    ("pause", {}, {"print": {"command": "pause", "param": ""}}),
    ("resume", {}, {"print": {"command": "resume", "param": ""}}),
    ("stop", {}, {"print": {"command": "stop", "param": ""}}),
    ("set_speed", {"level": 1}, {"print": {"command": "print_speed", "param": "1"}}),
    ("set_speed", {"level": 4}, {"print": {"command": "print_speed", "param": "4"}}),
    ("set_speed", {"level": "3"}, {"print": {"command": "print_speed", "param": "3"}}),
    ("refresh", {}, {"pushing": {"command": "pushall"}}),
])
def test_print_command_payloads(action, params, expected):
    cmd = commands.build(action, params)
    assert [_seq_free(p) for p in cmd.requests] == [expected]


@pytest.mark.parametrize("action,params,mode", [
    ("light_on", {}, "on"), ("light_off", {}, "off"),
    ("set_light", {"mode": "on"}, "on"), ("set_light", {"mode": "off"}, "off"),
])
def test_light_goes_to_both_chamber_nodes(action, params, mode):
    cmd = commands.build(action, params)
    bodies = [_seq_free(p)["system"] for p in cmd.requests]
    assert [b["led_node"] for b in bodies] == ["chamber_light", "chamber_light2"]
    assert all(b["command"] == "ledctrl" and b["led_mode"] == mode for b in bodies)


def test_sequence_ids_are_unique_strings():
    ids = [p["print"]["sequence_id"] for _ in range(3) for p in commands.build("pause", {}).requests]
    assert len(set(ids)) == 3


@pytest.mark.parametrize("action,params", [
    ("set_speed", {"level": 0}), ("set_speed", {"level": 5}), ("set_speed", {"level": "2; x"}),
    ("set_speed", {"level": None}), ("set_speed", {"level": True}), ("set_speed", {"level": 2.5}),
    ("set_speed", {}), ("set_light", {"mode": "flashing"}), ("set_light", {"mode": '"on"'}),
    ("set_light", {}), ("gcode_line", {"param": "M112"}), ("nope", {}),
])
def test_bad_actions_and_params_are_refused_before_anything_is_sent(action, params):
    with pytest.raises(commands.InvalidCommand):
        commands.build(action, params)


def test_params_must_be_a_dict():
    with pytest.raises(commands.InvalidCommand):
        commands.build("set_speed", ["x"])


# --- Confirmation ----------------------------------------------------------------

STATUS = Frame(status=True)
FULL = Frame(status=True, full=True)


@pytest.mark.parametrize("action,params,before,after", [
    ("pause", {}, {"gcode_state": "RUNNING"}, {"gcode_state": "PAUSE"}),
    ("resume", {}, {"gcode_state": "PAUSE"}, {"gcode_state": "RUNNING"}),
    ("stop", {}, {"gcode_state": "RUNNING"}, {"gcode_state": "FAILED"}),
    ("stop", {}, {"gcode_state": "PAUSE"}, {"gcode_state": "FINISH"}),
    ("stop", {}, {"gcode_state": "RUNNING"}, {"gcode_state": "IDLE"}),
    ("set_speed", {"level": 3}, {"spd_lvl": 2}, {"spd_lvl": 3}),
    ("light_on", {}, {"lights_report": [{"node": "chamber_light", "mode": "off"}]},
     {"lights_report": [{"node": "chamber_light", "mode": "on"}]}),
    ("light_off", {}, {"lights_report": [{"node": "chamber_light", "mode": "on"}]},
     {"lights_report": [{"node": "chamber_light", "mode": "off"}]}),
])
def test_confirmed_by_the_resulting_state(action, params, before, after):
    cmd = commands.build(action, params)
    assert not cmd.confirmed(before, STATUS)
    assert cmd.confirmed(after, STATUS)


def test_resume_is_confirmed_by_prepare_too():
    assert commands.build("resume", {}).confirmed({"gcode_state": "PREPARE"}, STATUS)


def test_stop_is_not_confirmed_by_an_unknown_state():
    cmd = commands.build("stop", {})
    assert not cmd.confirmed({}, STATUS)
    assert not cmd.confirmed({"gcode_state": "SLICING"}, STATUS)


def test_refresh_is_confirmed_by_a_full_report_only():
    cmd = commands.build("refresh", {})
    assert not cmd.confirmed({"gcode_state": "IDLE"}, None)
    assert not cmd.confirmed({"gcode_state": "IDLE"}, STATUS)
    assert cmd.confirmed({"gcode_state": "IDLE"}, FULL)


def test_print_controls_are_flagged():
    assert all(commands.build(a, p).print_control for a, p in
               [("pause", {}), ("resume", {}), ("stop", {}), ("set_speed", {"level": 2})])
    assert not any(commands.build(a, {}).print_control for a in ("light_on", "light_off", "refresh"))


def test_the_echo_a_refusal_is_judged_on_is_the_first_requests():
    cmd = commands.build("light_on", {})
    body = cmd.requests[0]["system"]
    assert cmd.echo == ("ledctrl", body["sequence_id"]) and body["led_node"] == "chamber_light"


def test_only_refresh_skips_the_midway_pushall():
    assert not commands.build("refresh", {}).pushall_midway
    assert all(commands.build(a, {"level": 2}).pushall_midway
               for a in ("pause", "resume", "stop", "set_speed", "light_on", "light_off"))
