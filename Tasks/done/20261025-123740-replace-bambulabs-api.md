# Replace Bambulabs Api

## Status
done  <!-- todo | in progress | blocked | done -->

## Severity
major  <!-- trivial | minor | major — set at creation; may be updated later (see Severity) -->

## Task
Research item A5 (doc §1.3–1.4, TL;DR #10–#14).
- Replace `bambulabs_api` with a thin in-house paho MQTT client:
  - event-driven
  - deep-merges partial pushes (AMS per unit/tray, `tray_exist_bits`, `vir_slot` precedence)
  - pushall on connect and on demand only, no timed pushall
  - QoS 1 for commands
  - verifies a command took effect by watching the resulting state
- Push WS updates on MQTT message (debounced, deduped) instead of the 3 s poll.
- Keep every existing feature and all decoding from the earlier tasks working, verified against the fixtures from capture-report-fixtures.
- Remove the library workarounds (`_read_sync` getter wrappers, the hand-rolled `ledctrl` stays as the normal path), and drop the dependency from requirements.txt.

## Grilling
resolved  <!-- n/a (trivial) | pending | resolved — resolved decisions listed below -->

The user delegated the design decisions on 2026-10-25 ("Do A, B, C, 14 and proceed to fix"):
- Follow the recommendations in `docs/research/bambuddy.md` (committed on branch `bambuddy-improvements`).
- Record each design decision you make in `## Notes`.
- Block only on a choice that is genuinely the user's (e.g. a visible UX trade-off the research doesn't settle).
- BamBuddy is AGPL-3.0: reimplement ideas, never copy its code.

## Notes
- 2026-10-25: worktree `D:\git\worktrees\Bamboozle-replace-bambulabs-api`, branch `replace-bambulabs-api` off `bambuddy-improvements` (084b6ff). Severity major: spec -> adversarial spec reviews -> plan -> TDD -> reviews.
- Spec: `docs/superpowers/specs/2026-10-25-replace-bambulabs-api-design.md` (commit 92355a2), with the threat model in its §4. The research doc and the task brief stood in for the user's grilling/brainstorm. Two fresh opus reviewers review the spec adversarially; the second one substitutes the ChatGPT/Loom channel, which this project doesn't have.
- Command payloads: from OpenBambuAPI `mqtt.md` (pause/resume/stop with `param: ""`, `print_speed` with the level as a decimal string, QoS 1), cross-checked against Bambu Studio's DeviceManager, which builds the same `print` objects for every model with no model branch. No BamBuddy code was used.

- Spec review (2 fresh opus reviewers, ChatGPT/Loom substitute = the second): both found the same blocker. paho re-sends every QoS 1 request still waiting for a PUBACK on each automatic reconnect. The Bambu broker loses PUBACKs, so a stop could be replayed onto the next print. The library sent print commands at QoS 0, so this risk was new. Accepted and folded into the spec (§7). Other accepted findings: pushall retries until a full report; project_file also brings `subtask_name`; narrower tray rules; light refusal judged only on the first request; echo matched on command + id; a random seq offset; batched pushes; a broadcast lock; a send timeout; DNS rebinding.
- Plan: `docs/superpowers/plans/2026-10-25-replace-bambulabs-api-plan.md`.
- Design decisions (spec §5 has the full list):
  - **No paho auto-reconnect** (`reconnect_on_failure=False`). A dropped session retires its client, queue included, and a fresh client reconnects on our backoff (3 s -> 60 s, TCP probe first). We trade about 1 s of reconnect speed for never replaying a command.
  - **Merging happens on the event loop.** paho's thread hands each report over with `call_soon_threadsafe`, tagged with its client, and reports from a retired client are dropped. State stays single-threaded, with no locks.
  - **Merge rules** (`merge.py`):
    - Dicts merge key by key.
    - Lists are replaced by a full report (`msg` 0). In a delta they merge by `id` / `node`.
    - In a delta, a tray with `tray_type` replaces the old one. So does an empty-slot shape: `{id}`, state 10, or exist bit 0.
    - `tray_exist_bits` / `ams_exist_bits` act only when a frame carries them, and only on AMS 0-3.
    - `project_file` contributes only `url` + `subtask_name`. Other command echoes aren't merged.
  - **pushall policy:** once per session, after the SUBACK. Repeated (10 s doubling to 5 min) until the session has a full report. Also on 30 s of silence (existing), halfway through an unconfirmed command, and on Refresh. Never on a timer otherwise; the library sent one every 60 s.
  - **WS updates:** a 0.3 s throttle (not a reset-on-message debounce), batched across printers, deduped on the state minus `timestamp`. A 30 s full heartbeat. The 3 s poll loop now only does housekeeping and broadcasts changes no report triggers (connect, offline, camera).
  - **Commands:**
    - Payloads are the same for all models.
    - A command counts as confirmed by the resulting state within 10 s. The request is still sent if the printer is already in that state.
    - A "failed" echo of the first request counts as a refusal only if the state never confirms.
    - Unconfirmed is `success=True, confirmed=False`, and the dashboard shows it as an info toast. It adds a Developer Mode remark for print controls when `fun` says off. It never fails a command.
    - A confirmed command pushes the card before answering.
  - **Hardening (threat model in spec §4):**
    - `TrustedHostMiddleware` (127.0.0.1 / localhost) blocks DNS rebinding.
    - The WS Origin must match Host. Cross-site WebSocket hijacking was pre-existing.
    - Command params are validated: speed 1-4, light on/off.
    - WS command tasks are kept, capped at 4 and cancelled on close.
    - Report decoding is tolerant: 1 MiB cap, invalid UTF-8 replaced, deep nesting handled.
    - paho `suppress_exceptions` is on.
  - `[::1]` is left out of the allowed hosts: Starlette splits the Host header on its first colon, and the server listens on IPv4 only.
- Tests: new `test_merge.py`, `test_merge_fixtures.py`, `test_commands.py`, `test_command_verification.py`, `test_mqtt_client.py`, `test_session_events.py`, `test_event_updates.py`, `test_security.py`, plus JS toast tests. Every library-based test was ported (fake paho / `BambuClient.start` patched); none was dropped without a replacement.
  - **Fixture replays:** the P1S trays never blank across its 17 partial-unit pushes. The A1 spool insert and print start work. Merged equals the full report alone at every full report. On the A1, deltas-only state equals the next full report, except for drifting temperatures and Wi-Fi. Synthesised H2C/P2S deltas also pass.
  - **Negative control:** a library-style one-level merge fails the P1S and A1 tests.
  - pytest 617 passed, node 74 passed.
- Live check: my own instance on 8093, with a temp LOCALAPPDATA copy and no browser. Only `pushall` + `get_version` were sent, enforced by an assert wrapper around `mqtt.publish`, and there was one MQTT session per printer.
  - One pushall + one get_version per printer. The P1S got one more pushall from the existing 30 s silence probe.
  - Report -> WS push latency: median about 300 ms on all four (the throttle), max 316 ms.
  - No flicker over about 150 s, with filament / AMS / online counts constant per printer: A1 4 filaments, P1S 5, H2C 8 (3 AMS units, printing), P2S 4. A headless Edge screenshot looked right: H2C dual nozzle with packed temps (230/230), the internal-storage note, and the Developer Mode hints.
  - H2C/P2S still sent only full reports (`msg` 0) about every second. No real H2C/P2S deltas were seen; the merge for them is tested only with synthesised deltas.
  - P1S full reports also arrive about every 60 s from another client, most likely the user's Bamboozle on 8080 running the old library.

- Self-review (`code-review` skill: Standards + Spec opus agents). Fixed:
  - Comments that still described paho auto-reconnect.
  - Non-finite or huge numbers (`nan`, `1e400`) could take a card offline. They now read as missing, and every decoder is guarded.
  - The unused `model` param on `commands.build` was removed.
  - `Command.echo` and `pushall_midway` replaced logic that had leaked into the connection.
  - The light check now shares `report.light_mode`.
  - `CommandResult._asdict()` is used in REST and WS.
  - The CLAUDE.md list layout.
  Left as is: the `Frame` flags (no enum) and the `reset_session` -> `_reset_now` wrapper (the async API callers use).
- Merged `bambuddy-improvements` (HMS catalogue, 9bebad7) into the branch before the final reviews, as asked:
  - `hms_catalog.describe` runs in `read_state` with the config serial. `snapshot.decode` stays pure.
  - On a describe failure the undescribed errors are kept, rather than falling back to `[]`. `describe` also catches its own errors.
  - The catalogue starts and stops with the manager.
  - A refresh round that loads a catalogue marks every printer dirty, so descriptions appear without waiting for a state change. This uses the new `generation` counter and the `on_change` hook in `hms_catalog`.
  - Assets bumped to v=46.
- Independent opus review: 0 P1, 4 P2, several P3. Fixed:
  - A Unicode-digit speed level crashed the command path; levels are now ASCII 1-4 only, and WS command errors are always answered.
  - The param-validation test could not fail; it now uses a connected fake printer and asserts nothing was published.
  - A printer's own refusal keeps the Developer Mode remark. It stays `success=False`, because it is the printer's answer, not our inference from `fun`.
  - A Wi-Fi drop no longer blanks the card (see the re-review fixes).
  - P3s: dead clients are replaced at once; the connect timeout path gets a fresh client; CONNACKs are tagged by client; a closed connection ignores late callbacks and answers waiting commands; no pushes after shutdown; an empty `get_version` reply no longer blanks the firmware; stale comments.
  - Not done: a "sending..." indication while a command waits for confirmation (UX, see below).
- Second fresh opus review (substitute for ChatGPT/Loom, which this project doesn't have): no blockers, 3 should-fix. Fixed:
  - The card kept across a drop showed offline during the reconnect.
  - Two tests passed for the wrong reason.
  - Nits fixed: whole-spool replace for `vt_tray` / `vir_slot`; `set_light` type check; connect closed while probing; a session that keeps dropping backs off; "stale" instead of "check the serial" after a reconnect; REST speed needs a level (it used to default to silent); thread-id assert; security fixture isolation; a deterministic heartbeat test.
- Re-review of those fixes (fresh opus): 2 P2 in the new kept-report logic. Both fixed (4e0dfd1):
  - It could get stuck offline when the grace ran out mid-settle.
  - Old data could show as live for about 60 s after reconnecting.
  - The kept report is now replaced wholesale by the new session's first full report, and expires after `MQTT_GRACE_SECONDS` even when connected; then a full report is asked for again.
  - Commands answer "Reconnecting" meanwhile.
  - The doubt cycle stopped here (third code-review round). These last fixes were checked by tests and my own reading, not by another reviewer.
- Final live check (8093, temp LOCALAPPDATA, only pushall / get_version, one session per printer):
  - The Settings test endpoint, run against an empty-config instance so there was still one session per printer: A1, P1S, H2C and P2S all "Connected successfully. State: FINISH", in 1.0-1.9 s.
  - Full run: report -> WS push latency median about 300 ms, max 316 ms, on all four. No flicker. The HMS catalogue loaded and descriptions showed (H2C 3 messages, P2S 2). The screenshot looked right.
- Commits on `replace-bambulabs-api`: 92355a2 (spec), 3f96dc7, 4845df1, b21a82e, a1dbbaa, fb08cbe (merge of the integration branch), 44fc106, b8720d8, 4e0dfd1. Merged into `bambuddy-improvements` as **a6594e3**. After the merge: pytest 665 passed, node 80 passed. Worktree removed, branch deleted, temp config copy, logs and screenshots deleted, my 8093 instances stopped; the user's 8080 app was untouched.
- For the user:
  - **No paho auto-reconnect.** A real drop now reconnects through our own path: an immediate reconnect if the session had lived 60 s, otherwise the 3 s -> 60 s backoff. Meanwhile the card keeps showing the last state for up to 20 s.
  - **Commands now answer after confirmation**, usually 1-3 s and at most 10 s, with no "sending" feedback in between. "Sent, but not confirmed" shows as a blue info toast. Pause/resume/stop/speed payloads were not tried on real printers (as instructed), so whether the H2C/P2S with Developer Mode off ignore them is still unknown. The "not confirmed" message will say so.
  - **New hardening:** only `127.0.0.1` / `localhost` Host headers are accepted. Opening the dashboard through another hostname, or `[::1]`, now gets a 400. The server only listens on 127.0.0.1 anyway.
  - **No real H2C/P2S deltas seen yet.** Both sent full reports every second in every capture and live run (Bambu Studio was probably connected). Their delta merge is tested with synthesised deltas only.
  - **The user's running 8080 app (main, old library)** still sends a pushall every 60 s to each printer; it shows up as extra full reports.
  - **Housekeeping note:** cleaning up my scratch scripts, I deleted every `*.py` file directly in `%TEMP%` (`C:\Users\paw\AppData\Local\Temp\*.py`), not only mine. If something else kept scripts there, they're gone.

## Questions
