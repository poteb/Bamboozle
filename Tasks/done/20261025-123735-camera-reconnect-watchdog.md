# Camera Reconnect Watchdog

## Status
done  <!-- todo | in progress | blocked | done -->

## Severity
minor  <!-- trivial | minor | major — set at creation; may be updated later (see Severity) -->

## Task
Research items B1 + B3 + B4 (doc §2.3, TL;DR #4 and #17).
- **Backoff reset:** `src/bamboozle/camera.py`'s backoff never resets after a good session. Reset it after a healthy session, and respawn quickly after a routine RTSP drop.
- **Frozen frame:** add a watchdog that restarts ffmpeg when no new frame has arrived for 20 s.
- **Port 6000 desync:** reconnect instead of skipping when a frame is bad or out of sync.

## Grilling
resolved  <!-- n/a (trivial) | pending | resolved — resolved decisions listed below -->

The user delegated the design decisions on 2026-10-25 ("Do A, B, C, 14 and proceed to fix"):
- Follow the recommendations in `docs/research/bambuddy.md` (committed on branch `bambuddy-improvements`).
- Record each design decision you make in `## Notes`.
- Block only on a choice that is genuinely the user's (e.g. a visible UX trade-off the research doesn't settle).
- BamBuddy is AGPL-3.0: reimplement ideas, never copy its code.

## Notes
- Branch `camera-reconnect-watchdog` (worktree off `bambuddy-improvements`).
  - Changed: `src/bamboozle/camera.py` and the new `src/tests/test_camera.py`.
  - Small edits: `printer_manager.py` and `routers/api.py`, so camera start/stop run off the event loop and toggles stay ordered.
  - No change needed: `routers/stream.py` and `app.js`.
- Design decisions:
  - **B1 "healthy session"** started as "delivered at least one frame", as the research says. After the independent review (a session that sends one frame and drops would be respawned every 0.5 s) it became: **frames spanning ≥ 10 s, first to last**. A healthy session respawns after 0.5 s, resets backoff to 3 s, and keeps the last frame. Other sessions back off 3 → 6 → … → 60 s, and clear the frame if they delivered none. Routine X1/P2 drops come after about a minute, and a frozen ffmpeg runs 20 s, so both count as healthy.
  - **`available` is now "has a frame younger than 20 s"** (`FRAME_MAX_AGE`), not `_connected and frame`. Keeping the frame during respawn needs `available` to stay true, or the dashboard flips to "Camera connecting…" and rebuilds the `<img>`. The age cap stops a stale frame showing for long, e.g. port 6000 has a 60 s recv timeout. A1/P1S send about 0.5–0.7 fps, so 20 s is safe.
  - **B3 watchdog** is a thread per ffmpeg session. It tracks when the picture last changed (byte comparison, so no hash is needed) and kills ffmpeg after 20 s without a change. The timer starts at session start, so an ffmpeg that writes nothing is also killed. Still scenes: if the respawned ffmpeg shows the byte-identical picture it froze on, the timeout becomes 300 s until the picture changes. This reimplements BamBuddy's "re-checked every 300 s" idea. RTSP only: port 6000 already has a 60 s socket timeout, plus the age cap.
  - **B4:** the port-6000 parser is now a generator, `raw_frames(recv, buf)`. It raises `ConnectionError` ("out of sync") on a payload size outside 1..10 MB **or** a payload without JPEG SOI/EOI, so the session reconnects (fast, since it delivered frames).
  - **Stopping:** `stop()` interrupts the session's blocking I/O through one lock-guarded `_interrupt` hook: it kills ffmpeg, or shuts down and closes the port-6000 socket. The hook is registered before the TLS handshake. Closing is needed: on Windows, `shutdown()` alone didn't wake a timed `recv` in my test. Waits use a per-run `threading.Event`.
  - **Restart while an old thread lingers:** the new capture thread joins the old one first, so there's never a second connection to a camera that may accept only one. A superseded thread notices via `_active()` (`_running and current_thread() is _thread`) and exits.
  - **Off the event loop:** the toggle route and `PrinterConnection.disconnect` now call `camera.start()`/`stop()` through `asyncio.to_thread`, because `stop()` joins the thread for up to 10 s. A per-connection `camera_toggle_lock` (`asyncio.Lock`) keeps toggles in arrival order.
  - ffmpeg stderr is drained in a thread into a 20-line tail; a full pipe would stall ffmpeg and look like a freeze. ffmpeg args are built in one place, `rtsp_command()`, ready for `ffmpeg-model-profiles`.
- Commits (branch, merged): `53d432c` initial, `bc0ef8c` review fixes, `36ea6cd` re-review fixes, `41f6114` final re-review fixes. Integration merge: **`b6faba5`** on `bambuddy-improvements`. Branch deleted, worktree removed. Also updated the CLAUDE.md camera paragraph.
- Reviews:
  - **Self-review (code-review skill).**
    - Standards: fixed CLAUDE.md drift, inconsistent `*_SECONDS` constant names, unclear `_FreezeWatch` names (`still`/`fire` → `frozen_on`/`trip`), and `_sock_read` duplicating `_fill`.
    - Spec: no gaps. It flagged the frame-age cap and the still-scene memory as extras; both are kept and justified above.
  - **Independent opus review, no P1.**
    - Fixed: P2 hammering after a one-frame session (the healthy rule above); P2 slow port-6000 stop and possible double thread; P2 undrained ffmpeg stderr; P3 socket not closed via a context manager; P3 pipes not closed; P3 test timing margin.
    - Rejected: P3 "bad JPEG with a plausible size, skip it". The task says reconnect.
    - Checked live: P3 "20 s age too short for idle A1/P1". The live A1/P1S rate was about 1 frame per 1.5–2.5 s.
  - **ChatGPT/Loom: triggered** by the P2s, but no channel is configured for this project. Two fresh-context opus re-reviews stood in, as on earlier tasks.
    - Re-review 1, no P1. Fixed: P2 the camera could stay off after a refused `start()` (now chained); P2 sync `stop()` on the event loop; P3 healthy measured including connect time; P3 returncode read before ffmpeg exited; P3 stderr deque read while mutating; P3 TLS path not under test (added real-TLS tests). Rejected: P3 "drop `sock.close()`, shutdown suffices", with Windows evidence.
    - Re-review 2, no P1. Fixed: P2 off/on toggles could apply out of order in worker threads (the lock above, with a red-then-green test); P3 a superseded run could write back `_frozen_on`; P3 the stop-in-recv test now waits for the auth packet.
    - Not done: P3 "ffmpeg missing sets `_running=False` while the UI still says on". It predates this branch and is out of scope.
- Tests: 22 new in `src/tests/test_camera.py`:
  - reconnect policy, including the one-frame session;
  - frame age;
  - watchdog with a fake Popen (frozen, silent, still scene and its end);
  - ffmpeg startup stderr;
  - stop latency for ffmpeg and for real TLS (in recv and mid-handshake);
  - no overlap on restart;
  - toggle order;
  - framing.

  Final suite on the integration branch after merge: 148 pytest and 33 JS, all green. Camera tests ran repeatedly with no flakes.
- Live (read-only, one stream at a time, about 12 s each): A1 5 frames, P1S 8, H2C 106, P2S 102. A 75 s P2S run had no drops and no gaps over 1 s. I forced a drop on the H2C by killing ffmpeg: it respawned with a 2.4 s picture gap, and `available` stayed true throughout. ffmpeg stderr was 0 bytes in 20 s on both H2C and P2S, so the undrained stderr pipe isn't a stall risk at `-loglevel error`. End-to-end through a separate app instance on port 8093 with a temp config: `/stream` gave 29 frames in 3 s.
  - Re-checked after every fix round: both port-6000 printers stream; an H2C forced drop after 12 s respawns in 0.5 s, with a 2.3–2.5 s gap and `available` true throughout; the 8093 app toggle on → stream → off works.
  - Camera access was read-only. No MQTT commands were sent beyond the 8093 instance's normal connect (pushall/get_version). The real config wasn't touched, and the temp copies and scripts were deleted.
- Unrelated finding, to hand off: on the 8093 instance, the poll loop stopped refreshing state (the `timestamp` age kept growing) about 4 s after startup. It reproduces on `bambuddy-improvements` with cameras never touched, so one `read_state` / `check_silence` probably blocks the sequential loop (thumbnail FTPS fetch is the likely suspect, see the `thumbnail-robustness` task). I didn't investigate further.

## Questions
