# Temps Stage Status Fields

## Status
done  <!-- todo | in progress | blocked | done -->

## Severity
minor  <!-- trivial | minor | major — set at creation; may be updated later (see Severity) -->

## Task
Research items C1 + C3 + C4 (doc §5, TL;DR #6, #8 and #16).
- **Temperatures:** show real nozzle/bed targets and chamber temp (today `chamber_temp` is hard-coded `None` in `printer_manager.py`). Handle the H2 `(target<<16)|current` encoding, and show the second nozzle on H2C.
- **Stage names:** decode `stg_cur` into readable names, handling the A1/P1 idle bug.
- **Extra fields:** fans, AMS humidity (the 0–100 reading vs the inverted 1–5 index) and AMS temperature, Wi-Fi signal, door, SD card, nozzle type/diameter, firmware, and `spd_lvl` names. Show them compactly in the existing card style; put the low-value ones in the detail view.
- **Decoding:** write all decoding as pure functions over the raw MQTT report dict, kept separate from `bambulabs_api` (use its raw dump), so they survive the later client replacement task.

## Grilling
resolved  <!-- n/a (trivial) | pending | resolved — resolved decisions listed below -->

The user delegated the design decisions on 2026-10-25 ("Do A, B, C, 14 and proceed to fix"):
- Follow the recommendations in `docs/research/bambuddy.md` (committed on branch `bambuddy-improvements`).
- Record each design decision you make in `## Notes`.
- Block only on a choice that is genuinely the user's (e.g. a visible UX trade-off the research doesn't settle).
- BamBuddy is AGPL-3.0: reimplement ideas, never copy its code.

## Notes
- 2026-10-25: started in worktree `D:/git/worktrees/Bamboozle-temps-stage-status-fields` (branch `temps-stage-status-fields` from `bambuddy-improvements`). Took one read-only pushall + get_version capture from all 4 printers (paho, no other requests) to confirm field shapes; nothing saved in the repo.
- Implementation: new `src/bamboozle/report.py`. It holds pure decoders over the raw `print` / `info` dicts, and `status_fields()` returns the `PrinterState` fields. New models: `Nozzle`, `Fans`, `AmsUnit`. `_read_sync` now reads temperatures from the raw dump instead of the library getters. `print_status` (the library's enum) is replaced by `stage` / `stage_name`. The dashboard was updated in `app.js` + `style.css`. `CLAUDE.md` documents `report.py`.
- **Design decisions**
  - **Temperatures:** H2 packing is decoded when the value is above 0xFFFF (`target<<16|current`); a plain value has target None. The card shows "current / target" only while target > 0.
  - **Two nozzles:** H2C/H2D get one tile per nozzle ("Nozzle L", "Nozzle R"), from `device.extruder.info` (id 0 = right, 1 = left). Type and diameter come from the `device.nozzle.info` entry whose id = the extruder's `hnow` (falling back to the extruder id). `nozzle_temp` / `nozzle_target` keep the flat fields.
  - **Chamber:** read from `device.ctc` (H2/P2S). The flat `chamber_temper` is used only on X1-family models. Otherwise it is None and the tile is hidden (the A1/P1 send a junk 5).
  - **Stage table:** numbers 0–66 with our own short wording (not copied; e.g. "Demonstrating motor noise"). -1 and 255 mean idle. 0 only counts while RUNNING or PAUSE (the A1/P1 idle bug). No stage after FINISH or FAILED. A number past the table reads "Preparing", except while paused, where it is blank and the header's "Paused" says enough (a small departure from the research). Stages 67–77 are left out because I couldn't confirm their meanings from protocol facts, so they fall back to "Preparing".
  - **Stage display:** a small line in the job section, also during PREPARE. Hidden for stage 0, which would just repeat "Printing".
  - **Fans:** the 0–15 scale maps to % in 10 % steps (`round(v/1.5)*10`; this matches the H2C's airduct %). Aux and chamber fans are hidden on A1 / A1 mini (they echo the part fan). The H2/P2S `device.airduct.parts` "left aux fan" is not decoded: the `func` codes are unconfirmed.
  - **AMS:** humidity uses only `humidity_raw` %. The 1–5 `humidity` index is never shown, not even as a fallback. Raw 0 and temp 0.0 mean no sensor (AMS Lite). Readings appear in the expanded filament group header ("AMS 1 · 35% RH · 28°C"). Unit ids 128 and up are named "AMS HT n", which also fixes the old "AMS 129" header and tooltip.
  - **Misc fields:** Wi-Fi dBm (the "-90 = wired" case is not special-cased, since no Ethernet printer is here to confirm it). Door: `stat` bit 23 (a hex string) on H2/P2S, `home_flag` bit 23 on X1; None for A1/P1 and unknown models. SD card. Firmware from the `info` module "ota". `spd_lvl` names are shown next to the % ("Standard · 100%").
  - **Firmware request:** the library's connect-time `get_version` shares a message with pushall and is never answered. So `read_state` sends its own `{"info":{"command":"get_version"}}` while online and the firmware is unknown, waiting 60 s and doubling up to 15 min between unanswered requests. It resets on a new MQTT client. This is the only new automatic MQTT traffic.
  - **Details view:** low-value fields (fans, nozzle type/diameter, Wi-Fi, door, SD, firmware) sit in a quiet "Details" `<details>` disclosure on each card, styled like the Developer Mode hint. Only its body is rewritten on update, so it stays open (verified live across WS updates). Hidden while offline.
- **Reviews**
  - **Self-review** (code-review skill, Standards + Spec agents). Fixed: decoding is now wrapped in try/except (a failure keeps the rest of the read); `hex_or_int` made public; a contradictory docstring; a named fan constant; the "Motor noise showoff" wording; CLAUDE.md. Accepted: `report.py` returns pydantic sub-models; small duplications (RUNNING/PAUSE checks, the JS `amsName` fallback).
  - **Independent review** (fresh Opus agent, given the diff + task). No P1. Two P2s:
    - Door decoded on every model: fixed, it is now per model.
    - H2 nozzle/chamber flicker from the library's shallow merge: left open, see below.
    - The P3s are fixed: no stage after a job ends, version backoff and reset, one dump snapshot per poll, extra tests. A1 fans with an unknown model are accepted, since SSDP learns the model within seconds.
  - **ChatGPT/Loom review:** risk-triggered because of the P2s, but this project names no ChatGPT/Loom channel and none is available to a sub-agent, so it was skipped. The independent reviewer re-checked the fixes instead.
- **Merged** into `bambuddy-improvements` as c206f25 (branch commits 6625bf3, ec0c921, 36b4cd7). Both suites green there: pytest 335, node 57. Worktree removed and branch deleted.
- **Open**
  - The library merges reports one level deep (`_data[k] |= v`), so a partial push that carries `print.device` or `print.ams` replaces the whole object. A live capture shows the H2C/P2S send the full `device` every push, so the dual nozzles and chamber don't flicker today. The P1S does send partial `ams` pushes, which can briefly drop trays and AMS readings; that was already the case before this task. The fix is deep-merge (research #12), in `replace-bambulabs-api`.
  - JS DOM update paths (`updateCard`) have no automated test because the harness stubs the DOM; they were checked by headless screenshots.

## Questions
