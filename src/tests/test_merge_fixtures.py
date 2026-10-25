"""The user's real report streams (fixtures/reports/), replayed message by
message through MergedReport and decoded the way the dashboard is."""
import copy

import pytest

from bamboozle import snapshot
from bamboozle.merge import MergedReport
from bamboozle.models import PrinterState
from report_fixtures import PRINTERS, load

MODELS = {"a1": "N2S", "p1s": "C12", "h2c": "O1C2", "p2s": "N7"}


def _state(print_payload, info, name):
    fields, job = snapshot.decode(print_payload, info, MODELS[name])
    state = PrinterState(printer_id="x", name=name, **fields).model_dump(mode="json")
    del state["timestamp"]
    return state, job


def replay(name):
    """(message, frame, decoded state) after each report, in arrival order."""
    merged = MergedReport()
    for m in load(name).reports():
        frame = merged.apply(m.payload)
        yield m, frame, merged


def _ams_trays(state):
    return [f for f in state["filaments"] if f["source"] == "ams"]


@pytest.mark.parametrize("name", PRINTERS)
def test_every_full_report_decodes_the_same_merged_or_alone(name):
    """Merging never leaves anything a full report would show differently."""
    checked = 0
    for m, frame, merged in replay(name):
        if frame.full:
            alone = MergedReport()
            alone.apply(copy.deepcopy(m.payload))
            alone.info = merged.info
            assert _state(merged.print, merged.info, name) == _state(alone.print, alone.info, name)
            checked += 1
    assert checked >= 1


def test_p1s_ams_trays_never_blank_on_its_partial_unit_pushes():
    """P1S AMS deltas carry only {"ams": [unit]}: the library's one-level
    merge dropped ams_exist_bits and with it every tray."""
    baseline = None
    partial_ams = 0
    for m, frame, merged in replay("p1s"):
        if baseline is None:
            if frame.full:
                baseline, _ = _state(merged.print, merged.info, "p1s")
                assert len(_ams_trays(baseline)) == 4
            continue
        state, _ = _state(merged.print, merged.info, "p1s")
        assert state["filaments"] == baseline["filaments"], m.t
        assert state["ams_units"][0]["humidity"] is not None, m.t
        if frame.status and "ams" in m.print:
            partial_ams += 1
    assert partial_ams == 17


def test_a1_spool_insert_follows_the_bits_then_the_unit_push():
    seen_full = False
    slots_over_time = []
    for m, frame, merged in replay("a1"):
        seen_full = seen_full or frame.full
        if not seen_full:
            continue
        state, _ = _state(merged.print, merged.info, "a1")
        slots = sorted(f["tray_index"] for f in _ams_trays(state))
        assert len(slots) >= 3, m.t  # never blank
        slots_over_time.append(slots)
    assert slots_over_time[0] == [0, 2, 3]  # slot 1 empty (tray_exist_bits "d")
    assert slots_over_time[-1] == [0, 1, 2, 3]  # the spool loaded at ~511 s


def test_a1_print_start_shows_through_the_deltas():
    states = []
    for m, frame, merged in replay("a1"):
        if frame.status:
            state, _ = _state(merged.print, merged.info, "a1")
            if not states or states[-1] != state["gcode_state"]:
                states.append(state["gcode_state"])
    assert states[-3:] == ["FINISH", "PREPARE", "RUNNING"]


@pytest.mark.parametrize("name", PRINTERS)
def test_non_status_frames_are_not_telemetry(name):
    kinds = set()
    for m, frame, merged in replay(name):
        if not frame.status and not frame.version and frame.command:
            kinds.add(frame.command)
    assert "push_status" not in kinds


def test_project_file_echo_contributes_only_its_url():
    for m, frame, merged in replay("a1"):
        if frame.command == "project_file":
            assert merged.print["url"] == m.print["url"]
            assert isinstance(merged.print.get("job_id", ""), str)
            return
    pytest.fail("the A1 capture has a project_file echo")


# --- H2C / P2S deltas (none captured: synthesised) -------------------------------


def _id_key(items):
    for key in ("id", "node"):
        if items and all(isinstance(i, dict) and key in i for i in items):
            return key
    return None


def _diff(old, new):
    """Only what changed from `old` to `new`, the way the A1 / P1S push
    deltas: changed keys, changed list items (by id / node)."""
    if isinstance(old, dict) and isinstance(new, dict):
        out = {}
        for k, v in new.items():
            if k not in old:
                out[k] = v
            elif old[k] != v:
                out[k] = _diff(old[k], v)
        return out
    if isinstance(old, list) and isinstance(new, list):
        key = _id_key(new)
        if key and _id_key(old) == key and {i[key] for i in old} <= {i[key] for i in new}:
            by_id = {i[key]: i for i in old}
            changed = []
            for item in new:
                prev = by_id.get(item[key])
                if prev is None:
                    changed.append(item)
                elif prev != item:
                    changed.append({key: item[key], **_diff(prev, item)})
            return changed
    return new


@pytest.mark.parametrize("name", ["h2c", "p2s"])
def test_synthesised_deltas_decode_like_the_full_reports(name):
    fulls = [m.print for m in load(name).full_reports()]
    version = load(name).version()
    merged = MergedReport()
    merged.apply({"print": fulls[0]})
    merged.info = version
    for prev, cur in zip(fulls, fulls[1:]):
        delta = _diff(prev, cur)
        delta.update(command="push_status", msg=1)
        merged.apply({"print": delta})
        assert _state(merged.print, version, name) == _state(cur, version, name)
