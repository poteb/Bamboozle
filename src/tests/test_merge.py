"""MergedReport: how partial pushes are merged into the printer's state."""
import copy

from bamboozle.merge import MergedReport


def _status(msg=1, **fields):
    return {"print": {"command": "push_status", "msg": msg, **fields}}


def _tray(i, color="FF0000FF", ftype="PLA"):
    return {"id": str(i), "tray_type": ftype, "tray_color": color, "remain": 50, "state": 11}


def _unit(uid, trays, **extra):
    return {"id": str(uid), "humidity_raw": "30", "temp": "25.0", "tray": trays, **extra}


def _full_with_ams(units, exist="1", tray_bits="f"):
    return _status(msg=0, gcode_state="IDLE", ams={
        "ams": units, "ams_exist_bits": exist, "tray_exist_bits": tray_bits, "tray_now": "255"})


def test_a_delta_keeps_the_keys_it_does_not_carry():
    r = MergedReport()
    r.apply(_status(msg=0, gcode_state="RUNNING", mc_percent=10, nozzle_temper=220))
    r.apply(_status(mc_percent=11))
    assert r.print == {"command": "push_status", "msg": 1, "gcode_state": "RUNNING",
                       "mc_percent": 11, "nozzle_temper": 220}


def test_nested_dicts_merge_key_by_key():
    r = MergedReport()
    r.apply(_status(msg=0, device={"bed": {"info": {"temp": 60}}, "ctc": {"info": {"temp": 30}}}))
    r.apply(_status(device={"bed": {"info": {"temp": 61}}}))
    assert r.print["device"] == {"bed": {"info": {"temp": 61}}, "ctc": {"info": {"temp": 30}}}


def test_null_and_scalars_replace():
    r = MergedReport()
    r.apply(_status(msg=0, vt_tray={"id": "254"}, layer_num=3))
    r.apply(_status(vt_tray=None, layer_num=4))
    assert r.print["vt_tray"] is None
    assert r.print["layer_num"] == 4


def test_a_frame_without_msg_counts_as_a_delta():
    r = MergedReport()
    r.apply(_full_with_ams([_unit(0, [_tray(0), _tray(1)])]))
    r.apply({"print": {"command": "push_status", "ams": {"ams": [_unit(0, [_tray(1, "00FF00FF")])]}}})
    trays = r.print["ams"]["ams"][0]["tray"]
    assert [t["id"] for t in trays] == ["0", "1"]


def test_a_print_frame_without_command_is_status():
    r = MergedReport()
    frame = r.apply({"print": {"gcode_state": "IDLE"}})
    assert frame.status
    assert r.print["gcode_state"] == "IDLE"


# --- AMS ------------------------------------------------------------------------


def test_p1s_style_partial_unit_keeps_the_other_units_and_ams_fields():
    """P1S partial AMS pushes are {"ams": [whole unit]} without the bit fields."""
    r = MergedReport()
    r.apply(_full_with_ams([_unit(0, [_tray(i) for i in range(4)]),
                            _unit(1, [_tray(i, "0000FFFF") for i in range(4)])], exist="3", tray_bits="ff"))
    r.apply(_status(ams={"ams": [_unit(0, [_tray(i) for i in range(4)], temp="26.0")]}))
    ams = r.print["ams"]
    assert [u["id"] for u in ams["ams"]] == ["0", "1"]
    assert ams["ams"][0]["temp"] == "26.0"
    assert len(ams["ams"][1]["tray"]) == 4
    assert ams["tray_exist_bits"] == "ff"
    assert ams["tray_now"] == "255"


def test_a_partial_tray_merges_into_the_old_one():
    r = MergedReport()
    r.apply(_full_with_ams([_unit(0, [_tray(0)])]))
    r.apply(_status(ams={"ams": [{"id": "0", "tray": [{"id": "0", "remain": 40}]}]}))
    tray = r.print["ams"]["ams"][0]["tray"][0]
    assert tray["remain"] == 40
    assert tray["tray_type"] == "PLA"


def test_an_empty_slot_tray_in_a_delta_replaces_the_spool():
    """A1 sends {"id": "1"}, H2C {"id": "1", "state": 10} for an empty slot."""
    r = MergedReport()
    r.apply(_full_with_ams([_unit(0, [_tray(0), _tray(1)])]))
    r.apply(_status(ams={"ams": [_unit(0, [_tray(0), {"id": "1", "state": 10}])]}))
    assert r.print["ams"]["ams"][0]["tray"][1] == {"id": "1", "state": 10}


def test_tray_exist_bits_clear_removed_spools_when_they_arrive():
    r = MergedReport()
    r.apply(_full_with_ams([_unit(0, [_tray(i) for i in range(4)])], tray_bits="f"))
    r.apply(_status(ams={"tray_exist_bits": "d", "version": 3}))  # slot 1 emptied
    trays = r.print["ams"]["ams"][0]["tray"]
    assert trays[1] == {"id": "1"}
    assert trays[0]["tray_type"] == "PLA" and trays[2]["tray_type"] == "PLA"
    assert r.print["ams"]["tray_exist_bits"] == "d"


def test_a_tray_update_after_the_bits_is_trusted():
    """The A1 sends the bits for a newly inserted spool, then the unit."""
    r = MergedReport()
    r.apply(_full_with_ams([_unit(0, [_tray(0), {"id": "1"}])], tray_bits="1"))
    r.apply(_status(ams={"tray_exist_bits": "3", "version": 4}))
    assert r.print["ams"]["ams"][0]["tray"][1] == {"id": "1"}
    r.apply(_status(ams={"ams": [_unit(0, [_tray(0), _tray(1, "00FF00FF")])]}))
    assert r.print["ams"]["ams"][0]["tray"][1]["tray_color"] == "00FF00FF"


def test_tray_exist_bits_use_four_bits_per_unit():
    r = MergedReport()
    r.apply(_full_with_ams([_unit(0, [_tray(i) for i in range(4)]),
                            _unit(1, [_tray(i) for i in range(4)])], exist="3", tray_bits="ff"))
    r.apply(_status(ams={"tray_exist_bits": "df"}))  # unit 1 slot 1 = bit 5
    assert r.print["ams"]["ams"][1]["tray"][1] == {"id": "1"}
    assert all("tray_type" in t for t in r.print["ams"]["ams"][0]["tray"])


def test_ams_ht_is_left_alone_by_the_exist_bits():
    r = MergedReport()
    r.apply(_full_with_ams([_unit(0, [_tray(0)]), _unit(128, [_tray(0)])], exist="11", tray_bits="10001"))
    r.apply(_status(ams={"tray_exist_bits": "1", "ams_exist_bits": "1"}))
    units = {u["id"]: u for u in r.print["ams"]["ams"]}
    assert units["128"]["tray"][0]["tray_type"] == "PLA"


def test_ams_exist_bits_drop_a_removed_unit():
    r = MergedReport()
    r.apply(_full_with_ams([_unit(0, [_tray(0)]), _unit(1, [_tray(0)])], exist="3", tray_bits="11"))
    r.apply(_status(ams={"ams_exist_bits": "1"}))
    assert [u["id"] for u in r.print["ams"]["ams"]] == ["0"]


def test_a_full_report_replaces_lists():
    r = MergedReport()
    r.apply(_full_with_ams([_unit(0, [_tray(0)]), _unit(1, [_tray(0)])], exist="3"))
    r.apply(_status(hms=[{"attr": 1, "code": 2}]))
    r.apply(_full_with_ams([_unit(0, [_tray(0)])]) | {})
    assert [u["id"] for u in r.print["ams"]["ams"]] == ["0"]
    r.apply(_status(msg=0, hms=[]))
    assert r.print["hms"] == []


def test_a_delta_list_without_ids_replaces():
    r = MergedReport()
    r.apply(_status(msg=0, hms=[{"attr": 1, "code": 2}, {"attr": 3, "code": 4}]))
    r.apply(_status(hms=[{"attr": 3, "code": 4}]))
    assert r.print["hms"] == [{"attr": 3, "code": 4}]


def test_lights_report_merges_on_node():
    r = MergedReport()
    r.apply(_status(msg=0, lights_report=[{"node": "chamber_light", "mode": "on"},
                                          {"node": "work_light", "mode": "flashing"}]))
    r.apply(_status(lights_report=[{"node": "chamber_light", "mode": "off"}]))
    assert r.print["lights_report"] == [{"node": "chamber_light", "mode": "off"},
                                        {"node": "work_light", "mode": "flashing"}]


def test_extruder_info_merges_on_id():
    r = MergedReport()
    r.apply(_status(msg=0, device={"extruder": {"info": [{"id": 0, "temp": 30}, {"id": 1, "temp": 31}]}}))
    r.apply(_status(device={"extruder": {"info": [{"id": 1, "temp": 200}]}}))
    assert r.print["device"]["extruder"]["info"] == [{"id": 0, "temp": 30}, {"id": 1, "temp": 200}]


def test_vir_slot_and_vt_tray_are_both_kept():
    r = MergedReport()
    r.apply(_status(msg=0, vir_slot=[{"id": "254", "tray_type": "PLA"}, {"id": "255", "tray_type": "PETG"}]))
    r.apply(_status(vt_tray={"id": "254", "tray_type": "TPU"}))
    assert [s["tray_type"] for s in r.print["vir_slot"]] == ["PLA", "PETG"]


# --- Frames that aren't status --------------------------------------------------


def test_project_file_contributes_its_url_and_job_name_only():
    r = MergedReport()
    r.apply(_status(msg=0, subtask_name="Benchy", job_id="123"))
    frame = r.apply({"print": {"command": "project_file", "url": "ftp://Benchy.3mf",
                               "job_id": 123, "subtask_name": "other", "sequence_id": "9"}})
    assert not frame.status
    assert r.print["url"] == "ftp://Benchy.3mf"
    assert r.print["job_id"] == "123"  # an int there, a string in status
    assert r.print["subtask_name"] == "other"


def test_command_echoes_are_not_merged():
    r = MergedReport()
    r.apply(_status(msg=0, gcode_state="RUNNING"))
    before = copy.deepcopy(r.print)
    frame = r.apply({"print": {"command": "pause", "sequence_id": "7", "result": "failed",
                               "reason": "verify failed", "gcode_state": "PAUSE"}})
    assert r.print == before
    assert frame.command == "pause"
    assert frame.sequence_id == "7"
    assert frame.result == "failed"
    assert frame.reason == "verify failed"


def test_system_frames_are_echoes():
    r = MergedReport()
    frame = r.apply({"system": {"command": "ledctrl", "sequence_id": "8", "result": "success"}})
    assert frame.command == "ledctrl" and frame.sequence_id == "8"
    assert r.print == {}
    assert not r.apply({"system": {"command": "sntp", "time": 1}}).status


def test_get_version_replaces_info():
    r = MergedReport()
    frame = r.apply({"info": {"command": "get_version", "module": [{"name": "ota", "sw_ver": "1"}]}})
    assert frame.version
    r.apply({"info": {"command": "get_version", "module": [{"name": "ota", "sw_ver": "2"}]}})
    assert r.info["module"] == [{"name": "ota", "sw_ver": "2"}]


def test_full_and_status_flags():
    r = MergedReport()
    assert r.apply(_status(msg=0)).full
    assert not r.apply(_status(msg=1)).full
    assert r.apply(_status(msg=1)).status


def test_reset_forgets_everything():
    r = MergedReport()
    r.apply(_status(msg=0, gcode_state="IDLE"))
    r.apply({"info": {"command": "get_version", "module": []}})
    r.reset()
    assert r.print == {} and r.info is None


# --- Hostile input --------------------------------------------------------------


def test_junk_never_raises():
    r = MergedReport()
    for junk in (None, [], "x", 5, {"print": None}, {"print": []}, {"print": {"ams": 5}},
                 {"print": {"command": "push_status", "ams": {"ams": "x", "tray_exist_bits": "zz"}}},
                 {"print": {"command": "push_status", "ams": {"ams": [5, None, {"id": None}]}}},
                 {"info": "x"}, {"system": 3}):
        r.apply(junk)
    r.apply(_full_with_ams([_unit(0, [_tray(0)])]))
    r.apply(_status(ams={"tray_exist_bits": "not hex", "ams_exist_bits": None}))
    assert r.print["ams"]["ams"][0]["tray"][0]["tray_type"] == "PLA"


def test_merging_is_bounded_in_depth():
    deep = leaf = {}
    for _ in range(200):
        leaf["x"] = {}
        leaf = leaf["x"]
    r = MergedReport()
    r.apply(_status(msg=0, deep=deep))
    r.apply(_status(deep=deep))  # merging into itself, 200 levels: no RecursionError
    assert "deep" in r.print


def test_the_merged_state_does_not_alias_the_frame():
    r = MergedReport()
    frame = _full_with_ams([_unit(0, [_tray(0)])])
    r.apply(frame)
    frame["print"]["ams"]["ams"][0]["tray"][0]["tray_type"] = "CHANGED"
    assert r.print["ams"]["ams"][0]["tray"][0]["tray_type"] == "PLA"


# --- Review follow-ups -----------------------------------------------------------


def test_a_state_only_update_of_a_loaded_tray_merges():
    """{"id", "state": 11} on a loaded slot is a state change, not an empty slot."""
    r = MergedReport()
    r.apply(_full_with_ams([_unit(0, [_tray(0), _tray(1)])], tray_bits="3"))
    r.apply(_status(ams={"ams": [{"id": "0", "tray": [{"id": "1", "state": 11}]}]}))
    tray = r.print["ams"]["ams"][0]["tray"][1]
    assert tray["tray_type"] == "PLA" and tray["state"] == 11


def test_a_state_only_tray_on_a_slot_whose_bit_is_off_is_empty():
    r = MergedReport()
    r.apply(_full_with_ams([_unit(0, [_tray(0), _tray(1)])], tray_bits="1"))
    r.apply(_status(ams={"ams": [{"id": "0", "tray": [{"id": "1", "state": 3}]}]}))
    assert r.print["ams"]["ams"][0]["tray"][1] == {"id": "1", "state": 3}


def test_an_id_only_tray_is_empty():
    r = MergedReport()
    r.apply(_full_with_ams([_unit(0, [_tray(0), _tray(1)])], tray_bits="3"))
    r.apply(_status(ams={"ams": [{"id": "0", "tray": [{"id": "1"}]}]}))
    assert r.print["ams"]["ams"][0]["tray"][1] == {"id": "1"}


def test_a_complete_tray_replaces_the_old_one():
    """A non-RFID spool replacing an RFID one must not keep its brand."""
    r = MergedReport()
    rfid = {**_tray(0), "tray_id_name": "A00-B5", "tray_sub_brands": "PLA Basic"}
    r.apply(_full_with_ams([_unit(0, [rfid])], tray_bits="1"))
    generic = {"id": "0", "tray_type": "PETG", "tray_color": "00FF00FF", "state": 11}
    r.apply(_status(ams={"ams": [{"id": "0", "tray": [generic]}]}))
    assert r.print["ams"]["ams"][0]["tray"][0] == generic


def test_msg_as_a_string_zero_is_a_full_report():
    assert MergedReport().apply(_status(msg="0")).full


def test_project_file_also_brings_the_new_job_name():
    r = MergedReport()
    r.apply(_status(msg=0, subtask_name="Old"))
    r.apply({"print": {"command": "project_file", "subtask_name": "New", "url": "ftp://New.3mf",
                       "param": "Metadata/plate_1.gcode", "sequence_id": "5"}})
    assert r.print["subtask_name"] == "New"
    assert "param" not in r.print and r.print.get("sequence_id") is None


def test_merged_lists_are_capped():
    from bamboozle.merge import MAX_MERGED_ITEMS
    r = MergedReport()
    r.apply(_status(msg=0, things=[{"id": 0}]))
    r.apply(_status(things=[{"id": i} for i in range(1, MAX_MERGED_ITEMS + 50)]))
    assert len(r.print["things"]) == MAX_MERGED_ITEMS


def test_an_empty_get_version_reply_does_not_blank_the_firmware():
    """The A1 sent one with `module: []` just before the real one."""
    r = MergedReport()
    r.apply({"info": {"command": "get_version", "module": [{"name": "ota", "sw_ver": "1"}]}})
    assert r.apply({"info": {"command": "get_version", "module": []}}).version
    assert r.info["module"] == [{"name": "ota", "sw_ver": "1"}]


def test_a_complete_external_spool_replaces_the_old_one():
    r = MergedReport()
    r.apply(_status(msg=0, vt_tray={"id": "254", "tray_type": "PLA", "tray_id_name": "A00-B5"},
                    vir_slot=[{"id": "254", "tray_type": "PLA", "tray_sub_brands": "PLA Basic"},
                              {"id": "255", "tray_type": "PETG"}]))
    r.apply(_status(vt_tray={"id": "254", "tray_type": "TPU"},
                    vir_slot=[{"id": "254", "tray_type": "ABS"}]))
    assert r.print["vt_tray"] == {"id": "254", "tray_type": "TPU"}
    assert r.print["vir_slot"] == [{"id": "254", "tray_type": "ABS"}, {"id": "255", "tray_type": "PETG"}]


def test_a_partial_external_spool_update_merges():
    r = MergedReport()
    r.apply(_status(msg=0, vt_tray={"id": "254", "tray_type": "PLA", "remain": 50}))
    r.apply(_status(vt_tray={"id": "254", "remain": 40}))
    assert r.print["vt_tray"] == {"id": "254", "tray_type": "PLA", "remain": 40}
