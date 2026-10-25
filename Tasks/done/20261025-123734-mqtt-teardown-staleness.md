# Mqtt Teardown Staleness

## Status
done  <!-- todo | in progress | blocked | done -->

## Severity
minor  <!-- trivial | minor | major — set at creation; may be updated later (see Severity) -->

## Task
Research items A2 + A3 + A6 (doc §1.3, TL;DR #2, #3 and #18).
- **Teardown:** fix MQTT teardown. `bambulabs_api`'s `mqtt_stop()` only calls `loop_stop()`, with no `disconnect()`, and can block. Disconnect first and never block the event loop. This affects edit, relocate, remove and shutdown.
- **Staleness:** mark a printer offline/stale when no MQTT message has arrived for 60 s, not only when the socket drops. Add a hint: "connected but zero reports since connect → check serial".
- **Lights and refresh:** turn the chamber light on/off on both LED nodes (H2 models have two). Add a per-printer "Refresh" action that sends a pushall on demand.

## Grilling
resolved  <!-- n/a (trivial) | pending | resolved — resolved decisions listed below -->

The user delegated the design decisions on 2026-10-25 ("Do A, B, C, 14 and proceed to fix"):
- Follow the recommendations in `docs/research/bambuddy.md` (committed on branch `bambuddy-improvements`).
- Record each design decision you make in `## Notes`.
- Block only on a choice that is genuinely the user's (e.g. a visible UX trade-off the research doesn't settle).
- BamBuddy is AGPL-3.0: reimplement ideas, never copy its code.

## Notes
- 2026-10-25: worktree `D:\git\worktrees\Bamboozle-mqtt-teardown-staleness` (branch `mqtt-teardown-staleness` from `bambuddy-improvements`). TDD; first commit a479ad7.
- Design decisions:
  - New `src/bamboozle/mqtt.py` (seed for the later replace-bambulabs-api task): `retire`, `publish`, `configure`, `pushall_request`, `light_requests`, `wait_for_teardowns`.
  - **Teardown** (A2): `retire` clears paho `on_connect/on_message/on_disconnect` inline, then disconnect → `loop_stop` → disconnect on a daemon thread (the 2nd disconnect closes a handshake that completed while the 1st found no socket). Used by `disconnect`, `reset_session`, the auth-refused path and `POST /api/printers/test` (the deferred fix). Nothing awaits paho any more.
  - `reset_session` builds a fresh `bl.Printer` instead of restarting the old one: the old network thread may still be joining, and `loop_start` on the same client would fail; fresh dump also stops stale data looking live.
  - Shutdown waits up to 2 s total (`SHUTDOWN_TEARDOWN_SECONDS`, off the event loop) so DISCONNECTs leave before exit.
  - **Staleness** (A3): `on_message_handler` stamps reports. 60 s silent → `is_offline()` true even with socket up → poll loop resets. Per research risk note, a quiet session first gets one standalone pushall at 30 s (`PROBE_SECONDS`), once per quiet spell. `last_error` gains `"no_reports"` (zero reports since connect; log hint "check the serial … case-sensitive" once) and `"stale"`; sticky until a report arrives, cleared when the printer is unreachable. Card notices for both (offline only).
  - Backoff now resets only for a session that delivered reports, so a wrong-serial printer backs off instead of reconnecting every ~60 s.
  - **Light** (A6): `ledctrl` to `chamber_light` + `chamber_light2` on all models (BamBuddy does the same unconditionally), QoS 1, incrementing string `sequence_id`. paho inflight window raised to 1000 (research A5d: Bambu PUBACK race wedges QoS 1 at the default 20).
  - `mqtt.publish` refuses while the session is down (paho would replay a queued QoS 1 light toggle on reconnect) → "Not sent: printer not connected".
  - **Refresh** (A7): `refresh` action → standalone `{"pushing":{"command":"pushall","sequence_id":…}}` (A1/P1S ignore combined requests). `POST /api/printers/{id}/refresh`, WS command, REST fallback in app.js, header icon button (shown when online). It's a query, so not reported as refused on `commands_blocked` printers.
  - Kept the library's own 60 s implicit pushall and QoS-0 pause/resume/stop/speed; those belong to the replace-bambulabs-api task.
  - Probe at 30 s, stale at 60 s (research says "one pushall after 60 s of silence before declaring it stale"; probing earlier keeps the 60 s offline threshold from the task).
  - Not built: the "last update Ns ago" UI hint the research lists as a side benefit. The task asks only for offline/stale + the serial hint.
- Self-review (`code-review`, Standards + Spec agents):
  - Fixed: duplicated `_sent(_set_light(...))` wrapping became one `_send` helper; unused test params; a misleading fixture name; CLAUDE.md Commands line.
  - Fixed (Spec, real race): if paho was mid-reconnect, the first DISCONNECT found no socket and the handshake completed later; with unacked QoS 1 requests, `loop_stop` never returned and the session leaked. `_shutdown` now watches the join and disconnects again whenever the client shows up connected (bounded 60 s, all public paho API). Commit 41147db, with a regression test.
  - Accepted, documented: a QoS 1 request sent while up but never acked can be replayed by paho's own reconnect inside the 20 s grace period. Callback setters can wait for one running paho callback (ms).
  - Left as is: duplicated `lan` fixtures across test files (existing pattern); `QUERY_ACTIONS` kept as a named concept. The test endpoint's library `pushall` (no publish timeout) predates this task.
- Live verification (own instances only, temp LOCALAPPDATA copy, port 8093, no browser; only pushall / get_version sent; no light or other commands):
  - Direct `PrinterConnection` run, 180 s, all 4 printers: all connected. Max gap between reports: A1 8.1 s (idle, FINISH), P1S 5.3 s (printing), H2C 1.4 s, P2S 1.5 s. No probe was needed, 0 false offline/stale events.
  - `reset_session` took ~16 ms, `disconnect` 0.2 ms. Every retired client ended with socket closed, state `MQTT_CS_DISCONNECTED` and its thread joined.
  - Settings test endpoint against A1: "Connected successfully. State: FINISH" in 1.1 s; its session closed the same way.
  - App on 8093: `POST /api/printers/{id}/refresh` returned success in 1–2 ms on all 4; served app.js has the Refresh button; edit (PUT) answered in 19 ms.
  - **Existing issue found (not this task):** the synchronous thumbnail FTP fetch inside `_read_sync` took about 45 s on P1S. It stalls the whole poll loop (so other printers' states freeze, e.g. A1 stayed offline after the edit), and it holds asyncio's default executor at exit (shutdown took ~28 s even though `PrinterManager.stop` finished instantly). Belongs to the thumbnail-robustness task.
- Independent review (fresh Claude agent, P1/P2/P3): **no P1/P2**. Fixed P3s: the replacement client is built off the event loop (`to_thread`; `tls_set` loads the CA store); Refresh button has an `aria-label`. Left as noted: a two-node light publish that half-fails reports "not sent"; with `poll_interval` ≥ 30 s (config file only, default 3 s) the stale check can fire before a probe; a cancelled Settings test can retire before `loop_start` (very rare); a fresh client per reset shows partial data until the first full report.
- ChatGPT/Loom review: **not triggered**. No money, auth, security, privacy, data-loss or webhook surface, and no P1/P2 from the independent review.
- Merged into `bambuddy-improvements` as ce22e95 (branch commits a479ad7, 41147db, d31e32d). After the merge: pytest 126 passed, node 33 passed. Worktree removed, branch deleted, temp config copy deleted.

## Questions
