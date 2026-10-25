# Capture Report Fixtures

## Status
done  <!-- todo | in progress | blocked | done -->

## Severity
minor  <!-- trivial | minor | major — set at creation; may be updated later (see Severity) -->

## Task
Research step 7 (doc §8), the prerequisite for replace-bambulabs-api.
- Capture real MQTT `report` payloads (full pushall, plus a sample of incremental `push_status` messages, `get_version` info, and an AMS update if one happens) from the A1, P1S, H2C and P2S.
- Store them as test fixtures under `src/tests/fixtures/`, with serials partly masked and no access codes, plus a small capture script for re-capturing.
- **Read-only:** subscribe and send only `pushall`/`get_version`. Never send any other command.

## Grilling
resolved  <!-- n/a (trivial) | pending | resolved — resolved decisions listed below -->

The user delegated the design decisions on 2026-10-25 ("Do A, B, C, 14 and proceed to fix"):
- Follow the recommendations in `docs/research/bambuddy.md` (committed on branch `bambuddy-improvements`).
- Record each design decision you make in `## Notes`.
- Block only on a choice that is genuinely the user's (e.g. a visible UX trade-off the research doesn't settle).
- BamBuddy is AGPL-3.0: reimplement ideas, never copy its code.

## Notes
- 2026-10-25: worktree `D:/git/worktrees/Bamboozle-capture-report-fixtures`, branch `capture-report-fixtures` off `bambuddy-improvements`.
- Decision: tool at `src/tools/capture_reports.py` (package `tools`, run `python -m tools.capture_reports` from `src/`). Next to `bamboozle` so it imports the config path, the `pushall`/`get_version` builders and the SSDP parser with the app's `src/` cwd convention; outside the app package because it's a dev tool, and outside `tests/` because it isn't a test.
- Decision: two steps. `capture` writes raw JSONL (real serials/IPs; reports never carry the access code) to `%TEMP%/bamboozle-capture`, outside the repo. `mask` turns that into fixtures. So masking rules can be refined without re-capturing (pushall budget). The raw dir is deleted at the end.
- Safety in the tool: a single `publish()` asserts the payload is a standalone `pushall` or `get_version`; one pushall per printer per run, plus a ledger file refusing a pushall within 5 min of the last one; get_version retried once after 60 s if unanswered; one paho session per printer; config read with `json` + pydantic (no `load_config` write paths). Model code from config, else heard passively on SSDP UDP 2021.
- Smoke run (`--no-pushall`, 24 s): all four connect. Bambu Studio was connected to A1, H2C (x2) and P2S, not P1S; Bamboozle (8080) wasn't running.
- Finding: H2C and P2S send a FULL `push_status` (13 KB / 9 KB, `msg: 0`, `sequence_id "2021"`) about every 1 s, even without our pushall, at least while Bambu Studio is connected. A1/P1S send small deltas every ~2 s.
- Decision: the payload shapes say what to mask: `sn` everywhere (printer, AMS, nozzle, module serials, `upgrade_state.sn`), IPs (`ipcam.rtsp_url`, packed `net.info[].ip`), cloud/MakerWorld ids (`task_id`, `subtask_id`, `project_id`, `job_id`, `profile_id`, `design_id`, `model_id`; one shared mapping since a cloud job reports one number for all of them), `subtask_name` and other job file names (stem → `job-N`, keeping `plate_N` G-code paths, directory and extension, and the `subtask_name`↔`url` link). Kept: RFID `tag_uid` / `tray_uuid` (spool tags, not personal), firmware versions, `cfg`/`fun`.
- Capture: 10 min, all four at once from 16:32 UTC, one pushall + one get_version each (no retries needed). A1 261 msgs (4 full: ours + 3 answering Bambu Studio's pushalls; 251 deltas; a print started from Studio at ~540 s: `system` `sntp`, Studio's get_version reply, the `project_file` echo, FINISH → PREPARE → RUNNING). P1S 183 (1 full, 179 deltas, 17 partial AMS pushes). H2C 538 (535 full, 0 deltas). P2S 530 (527 full, 0 deltas).
- Finding (merge-relevant): P1S partial AMS = `{"ams": {"ams": [whole unit]}}` with no `tray_exist_bits` / `tray_now` / `tray_tar` / `version`; A1 partial AMS = `{"tray_exist_bits": "f", "version": 209}` with no unit list (a shallow merge drops every tray), plus `{"ams": [...], "tray_read_done_bits", ...}` variants.
- Finding (privacy): the cloud print's `project_file` `url` is a presigned S3 link containing the Bambu account id (`users/<id>/`) and an AWS signature; `dev_id` and the P1S AMS `ams_id` are serials; AMS `chip_id` is a hardware id; an H2C module reports `sn: "N/A"` (must not be learned as a serial or every "N/A" gets masked). Masker extended for all of these; ints and strings of the same cloud id share a fake.
- Decision: fixtures gzipped (`*.jsonl.gz`, gzip mtime 0 for byte-stable re-masks): H2C/P2S full-state-every-second is 10 MB raw → 92 KB total. Format documented in `src/tests/fixtures/reports/README.md`; loader `src/tests/report_fixtures.py` (`load(name)` → `full_report()`, `full_reports()`, `partial_reports()`, `status_pushes()`, `version()`; fresh objects per call so merges can mutate).
- Not captured: H2-style packed `target<<16|current` temps (heaters were off on H2C/P2S, H2C left nozzle cooling at 120 with no target); H2C/P2S deltas (Studio connected → full pushes only); a P1S/H2C/P2S job running. The A1 print covers a live job on deltas.
- Finding: A1 and P1S reported no HMS at all; H2C carries `0500050000010007` plus two serious lingering `0500_0600_...`, P2S `0500050000010007` plus `0501_0400_0003_0002`. CLAUDE.md's "all four carry 0500050000010007" is not true of these reports (left as is: it's hms.py's concern, unchanged).
- Finding: H2C/P2S `ipcam.rtsp_url` names IPs other than their configured MQTT IPs (H2C at .100 says .186 / .125 across reports). Not acted on; may matter to the camera code.
- Tests: `test_capture_tool.py` (masking rules, only-standalone-pushall/get_version gate, made-up values only), `test_report_fixtures.py` (privacy residue check, requests sent, firmware from get_version, full-report decode per model: H2C two nozzles left/right, chamber only on H2C/P2S, developer_mode None/False, refused-command record never active, every push decodes alone, the AMS partial shapes, the A1 print-start sequence). pytest 441 passed, JS 68 passed.
- Self-review fixes: the publish gate was an `assert` (stripped under `-O`) → explicit check `_allowed_requests`; stdout flush.
- Independent review (fresh Claude, opus): P1 — `test_capture_tool.py` hard-coded real serials, ids, IPs, the account id and a job name copied from the capture → replaced by made-up values and the branch commit amended (never pushed/merged) so they're not in branch history. P2 — pushall ledger lived in the raw dir that the README says to delete → moved to `%TEMP%/bamboozle-capture-pushall-ledger.json`, keyed by a serial hash; P2 — `load()` was lru-cached shared dicts → fresh parse per call; P2 — raw capture still on disk → deleted at the end. P3s fixed: Ctrl+C stops sessions via a shared Event; duplicate serials/slugs refused; warning when 8080 is open; stronger privacy test (dev_id/ams_id/chip_id/cloud ids/URLs); refused-command test now asserts presence on H2C/P2S; README example/`duration_s`/job-link wording. P3 not fixed: no H2C/P2S deltas (noted for replace-bambulabs-api).
- ChatGPT/Loom review: risk-triggered (privacy/personal data, and step 3 found a P1), but no ChatGPT/Loom channel is configured for this project or available in this environment; ran a second fresh-Claude round on the fixes instead.
- Second fresh-Claude round on the fixes: all earlier findings fixed; new P2 (a literal backspace byte in the README from a `` escape) fixed, P3s fixed (docstring wrap, tighter fake-id regex). Verified no real value in the committed text, commit messages or fixtures.
- For replace-bambulabs-api: the fixtures have no H2C/P2S deltas and no packed H2 temps; if the merge needs those, re-capture with Bambu Studio closed (or while H2C heats). The A1 and P1S files cover the partial-AMS hazards and a live job start on deltas.
- Commits on `capture-report-fixtures`: 104af7c (fixtures, tool, tests; amended once to drop the real values from the first test file before any merge), a04a9a1 (review fixes). Merged into `bambuddy-improvements` as 084b6ff; there pytest 441 passed, JS 68 passed. Worktree removed and pruned, branch deleted. Raw captures (`%TEMP%/bamboozle-capture`, `%TEMP%/bamboozle-capture-smoke`) deleted. The amended-away commit 24dae6e stays only as an unreachable local object until git gc; never pushed.

## Questions
