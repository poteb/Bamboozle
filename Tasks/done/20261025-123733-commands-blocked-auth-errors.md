# Commands Blocked Auth Errors

## Status
done  <!-- todo | in progress | blocked | done -->

## Severity
minor  <!-- trivial | minor | major — set at creation; may be updated later (see Severity) -->

## Task
Research items A1 + A4 (doc §1.3, TL;DR #1 and #9).
- **Commands refused:** detect when the printer refuses commands (Developer Mode / "command verification failed": `fun` bit plus HMS `0500050000010007`). Show it on the printer card as a badge, and make pause/stop/light/speed say why they failed instead of failing silently.
- **Wrong access code:** report a refused CONNACK (rc 134/135) as "access code wrong or changed", in both connect/reconnect and `POST /api/printers/test`, instead of a generic timeout or no data.

## Grilling
resolved  <!-- n/a (trivial) | pending | resolved — resolved decisions listed below -->

The user delegated the design decisions on 2026-10-25 ("Do A, B, C, 14 and proceed to fix"):
- Follow the recommendations in `docs/research/bambuddy.md` (committed on branch `bambuddy-improvements`).
- Record each design decision you make in `## Notes`.
- Block only on a choice that is genuinely the user's (e.g. a visible UX trade-off the research doesn't settle).
- BamBuddy is AGPL-3.0: reimplement ideas, never copy its code.

## Notes
- Worktree `D:\git\worktrees\Bamboozle-commands-blocked-auth-errors`, branch `commands-blocked-auth-errors` off `bambuddy-improvements`.
- Live probe (pushall only, no commands): A1 and P1S send no `fun` and no HMS. H2C `fun=14023FF1AFFF9CB7` and P2S `fun=60039FD1B3FF9CB7` both have bit 0x20000000 set (Developer Mode off), and both carry HMS `0500050000010007`. So both signals agree: the user's H2C and P2S currently drop control commands.
- Design decisions:
  - New `bamboozle/protocol.py`: pure functions over the report `print` dict (`developer_mode`, `hms_codes`, `commands_blocked`, `is_auth_refusal`) plus the message constants. Later tasks (HMS decoding, state_from_report) can grow it.
  - `commands_blocked` = `developer_mode is False` OR HMS `0500050000010007` present. No latching is needed: the library's dump keeps the last `fun` / `hms`.
  - `PrinterState` gains `developer_mode: bool | None` (None = not reported, A1/P1), `commands_blocked: bool`, and `last_error: str` ("" or `auth_rejected`).
  - Unknown Developer Mode (A1/P1) shows nothing. A "Developer Mode: unknown" line on every A1/P1 card would just be noise. No write-probe (per research §6).
  - Commands to a blocked printer are **still sent**, then reported as failed with the reason ("Printer is refusing control commands: enable LAN Only + Developer Mode on the printer, then restart it"). If the detection is ever wrong, the command still works. A refused command is a no-op on the printer anyway.
  - Card notice: one `.card-notice` line (role="status", stays rendered while empty, like `#ws-status`) in the card body. It shows either "Printer is refusing commands: enable Developer Mode" (full text in the tooltip) while online, or "Access code wrong or changed: update it in Settings" while offline with `auth_rejected`. The control buttons stay visible.
  - Auth: `PrinterConnection` sets `mqtt_client.on_connect_handler`. A CONNACK of 134/135 sets `_auth_rejected` and logs "access code wrong or changed" once; an accepted CONNACK clears it. Other refusals leave it unchanged. Through `read_state` it becomes `last_error="auth_rejected"`, `online=False`, without calling the getters (so no "Values Not Available" spam). If the refusal is seen when `connect()` settles, the session is stopped and retried on our own backoff (3 s → 60 s) rather than paho's tight loop with the bad code. A later refusal (on a paho reconnect) still shows through `read_state`, and the existing grace/reset path handles it.
  - `POST /api/printers/test` now waits for the CONNACK (10 s) and then for a first report (5 s), instead of `sleep(3)` + `get_state`, which used to report "success, state UNKNOWN" for a wrong code. Results: 134/135 → access-code message; other refusal → "Printer refused the connection: <reason>"; no CONNACK → "timed out"; logged in but no report → "check the serial number". The last one goes slightly beyond A4, but without it a wrong serial still reads as success.
- TDD: `tests/test_commands_blocked.py`, `tests/test_auth_rejected.py`, `tests/js/card_notice.test.mjs`. All were red first (import error / missing `cardNotice`), then green.
- Live check on my own instance (port 8093, temp LOCALAPPDATA holding H2C with a deliberately wrong code plus P2S, webbrowser patched; temp files deleted afterwards; no commands sent):
  - H2C: refused "Not authorized" (135). The card showed "Access code wrong or changed". Reconnects backed off 3 s → 6 s → 12 s, with 0 "Values Not Available" lines.
  - P2S (printing): `developer_mode=false`, `commands_blocked=true`, and the card notice was shown (headless Edge screenshot checked).
  - Test endpoint against H2C: wrong code → access-code message; right code → "Connected successfully. State: FINISH"; wrong serial → "check the serial number".
- Self-review (`code-review` skill: Standards and Spec sub-agents, opus). Fixed in commit `ff229d8`:
  - Duplicated `mqtt_dump()["print"]` walk → `_print_payload()`.
  - The manager and the test endpoint disagreed on the `is_failure` fallback → `protocol.connack_failed()`.
  - `last_error` is now `Literal["", "auth_rejected"]`; `_as_int` renamed `_hex_or_int`.
  - CLAUDE.md now documents `protocol.py`, the new fields and the test endpoint.
  - The visible notice showed only half the fix (LAN Only / restart were tooltip-only) → it now shows the full text.
  - A refusal arriving after `connect()` settled waited out the 20 s grace while paho kept retrying the bad code → `is_offline()` is true at once when the login is refused (new test).
- Self-review findings rejected:
  - JS duplicating the message wording: the frontend owns the card wording.
  - A shared JS test helper: two small harnesses; not worth the churn.
  - `developer_mode` unused by the UI: kept, because the research asks to expose it.
  - "Badge" vs notice strip: the strip is the badge (one line per card, same place for both notices).
  - Not showing "unknown": decided above.
  - The test endpoint's "no data → check serial" (A3-flavoured): kept, as decided above.
- Independent review (fresh opus agent, read-only): no P1, 2 P2, 5 P3. Fixed in commit `decc758`:
  - P2 (both): a leftover HMS overrode a live `fun` that says Developer Mode is on, so working commands were reported as failed. **Decision changed:** `fun` wins where it is reported; the HMS counts only on printers without `fun`.
  - P3: a retry with a CONNACK slower than the settle time was torn down by the previous attempt's refusal → per-attempt `_login_answered`.
  - P3: an unreachable printer kept "access code wrong" → the flag is now cleared on a failed TCP probe.
  - P3: why the code changes is now in the visible card text, not only the tooltip.
  - P3: `is_auth_refusal` docstring.
  - Test gaps added: signals disagreeing, a non-auth refusal after an auth refusal, a retry with a stale flag, unreachable after a refusal.
- Rejected / deferred:
  - Test endpoint leaves the socket open (`mqtt_stop` = `loop_stop`, no DISCONNECT): that predates this change and is task `mqtt-teardown-staleness` (A2).
  - Non-UTF-8 payloads: A5 (tolerant decode). The user's A1 decodes fine.
  - JS `updateCard` notice path: no DOM in the node harness. Covered by the live screenshot.
- **Live finding while checking the test endpoint on all 4 printers:**
  - A1 and P1S ignore the library's combined on-connect request (`pushing` + `info` + `upgrade`). They answer only the `upgrade` part, then send deltas without `gcode_state`.
  - The endpoint therefore now treats any report as proof of login + serial. If no status has arrived, it sends a plain `pushall` and reads `gcode_state` from the dump, rather than calling the library getter, which only worked by triggering its own implicit pushall.
  - Live result: A1 FINISH, P1S RUNNING, H2C FINISH, P2S RUNNING, all success.
  - Relevant for A5 (replace library): send `pushall` on its own on connect.
- ChatGPT/Loom step: triggered (step 3 found P2s, and the change touches credentials/auth reporting), but this project has no ChatGPT/Loom channel configured. A second fresh-context opus re-review of the fix commit was run instead.
- Re-review of the fixes (fresh opus agent): no P1 or P2. Fixed in `46c54ff`:
  - The test endpoint's `pushall` is capped by `wait_for` and its failure is caught, because login and serial are already proven by then.
  - `_login_answered` is now set only on an accepted or auth-refused CONNACK, after the flag. So a retry refused for another reason (e.g. 136) isn't torn down as a bad code.
  - Accepted as is: a stale `auth_rejected` notice persists if the printer accepts TCP but never answers MQTT. That is rare, and it clears on an accepted login or a failed probe.
- Live re-check after the fixes:
  - Wrong-code H2C → `auth_rejected`, backoff 4 s → 7 s → 9 s, 0 "Values Not Available" lines.
  - P2S → `commands_blocked`.
  - Test endpoint on all 4 printers → success with state.
  - Temp config/logs deleted; port 8080 app untouched.
- Merged into `bambuddy-improvements` as `5a800dd` (branch commits `81be019`, `ff229d8`, `decc758`, `46c54ff`). After the merge: pytest 95 passed, node 29 passed. Worktree removed and branch deleted.

## Questions
