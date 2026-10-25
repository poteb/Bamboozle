# Thumbnail Robustness

## Status
done  <!-- todo | in progress | blocked | done -->

## Severity
minor  <!-- trivial | minor | major — set at creation; may be updated later (see Severity) -->

## Task
Research item D1 (doc §3.3, TL;DR #15).
- **Lookup:** try `subtask_name` variants across `/`, `/cache`, `/model` and `/data`, and remember misses so we don't hammer FTPS.
- **Internal storage:** detect prints stored on internal eMMC (common on H2C/P2S) and show why there's no thumbnail.
- **TLS:** cap FTPS TLS at 1.2 for P2S/H2C if the doc says that's needed.
- Code is in `src/bamboozle/thumbnail.py` and its caller in `printer_manager.py`.
- **Must fix:** found by `mqtt-teardown-staleness` (2026-10-25). The thumbnail FTPS download runs inside the state read on the poll path. On the P1S it took about 45 s, which froze updates for every printer and made shutdown take about 28 s. Move the fetch off the poll path (a background task per printer, with a timeout) so one slow download can't stall the rest.
- **Also seen:** `camera-reconnect-watchdog` saw the dashboard state stop refreshing about 4 s after startup on the integration branch, with cameras untouched. The likely cause is one slow per-printer read blocking the sequential 3 s poll loop. Confirm the root cause first, using the diagnosing-bugs approach: build a loop that shows the stall. Then make sure no single printer's read (thumbnail or otherwise) can block the loop, e.g. by reading printers concurrently with per-printer timeouts. Check whether `main` has the same stall and note it.

## Grilling
resolved  <!-- n/a (trivial) | pending | resolved — resolved decisions listed below -->

The user delegated the design decisions on 2026-10-25 ("Do A, B, C, 14 and proceed to fix"):
- Follow the recommendations in `docs/research/bambuddy.md` (committed on branch `bambuddy-improvements`).
- Record each design decision you make in `## Notes`.
- Block only on a choice that is genuinely the user's (e.g. a visible UX trade-off the research doesn't settle).
- BamBuddy is AGPL-3.0: reimplement ideas, never copy its code.

## Notes
- 2026-10-25: started in worktree `D:/git/worktrees/Bamboozle-thumbnail-robustness` (branch `thumbnail-robustness` off `bambuddy-improvements`).
- **Stall feedback loop** (live, read-only): harness runs the real `PrinterManager` against the 4 printers with a fake WS client timestamping broadcasts and timing `read_state` / `fetch_thumbnail`. RED on the integration branch: one update at 3.2 s, then `fetch_thumbnail` for P1S (`Family one child, ... .3mf`, mid-print) ran 6.3 s -> 65.5 s (59 s) and no update was broadcast in between (worst gap 56.8 s). The process could not exit until the FTPS thread finished (asyncio's default executor joins its threads), which is the slow shutdown.
- **Root cause**: `_poll_loop` awaits each printer's `read_state` in turn, and `_read_sync` calls the blocking FTPS `fetch_thumbnail` inline whenever the job name differs from the last *successful* fetch. A slow 3MF download freezes every printer's updates; a failing fetch (H2C/P2S) is retried on every poll. Also: the FTP data socket had no timeout (only the control socket did), so a stalled transfer could block forever.
- **main has the same stall**: identical sequential loop + inline fetch (`main:src/bamboozle/printer_manager.py` ~L238 and ~L794). Not fixed on main. The user's 8080 app also opens an FTPS session to the H2C every 3 s (39 TIME_WAIT sockets to `192.168.0.100:990` seen) because failures are never cached.
- **Live findings**: H2C and P2S report `sdcard: false` and `gcode_file: /data/Metadata/plate_N.gcode`; H2C's FTPS root is empty and `/cache`, `/model`, `/data` give 550 (no external storage, so no thumbnail is possible over FTPS). H2C negotiates TLS 1.2 by default. P2S port 990 answers `421 There are too many connections from your internet address` even with none open from this PC, so its FTPS couldn't be tested. A1 and P1S report `sdcard: true`, `gcode_file: <name>.3mf`, `subtask_name: <name>`.
- **Fix** (`printer_manager.py`, `thumbnail.py`, `protocol.py`, `models.py`, `app.js`, `style.css`, CLAUDE.md):
  - Poll loop polls all printers concurrently (`_poll_all`); each printer's step (health checks + read) gets `POLL_STEP_TIMEOUT_SECONDS` = 2 s, then its last known state is broadcast. A step still running from an earlier round isn't started again or re-waited. The broadcast lists the printers as they are after the round (a removed printer would otherwise reappear on the merged dashboard).
  - Thumbnail fetch moved out of `_read_sync` into a background task per printer on its own daemon thread (shutdown never waits), single-flight, cancelled on disconnect / job change, 240 s overall deadline, 15 s socket timeout now also on data connections.
- **Design decisions** (research doc §3.3 D1, user-delegated):
  - Lookup: candidates `<gcode_file basename if .3mf>`, `<subtask>.gcode.3mf`, `<subtask>.3mf`, plus space->underscore variants; one connection walks `/cache`, `/`, `/model`, `/data` (`/cache` first: where Studio sends A1/P1 prints; the doc lists `/` first, order only matters for duplicate names). "Last 3MF listed" fallback removed (could show another job's picture).
  - Misses: no 3MF / no picture = never looked up again for that job; errors (connect, 421, timeout) retry after 60 s, doubling to 15 min. A new job name, or the state going ended -> PREPARE/RUNNING (reprint of the same project), resets this. A job change clears the old picture instead of showing it on the new job.
  - Internal storage: `sdcard: false` (observed on H2C/P2S, whose FTPS is empty) or a `project_file` `url` of `brtc://emmc/` / `file:///userdata/` whose file name is the current job's -> no FTPS lookup, `PrinterState.thumbnail_unavailable = "internal_storage"`, and the job section of a running/paused card says "No preview: the print file is on the printer's internal storage. To get previews, insert an SD card or USB drive and enable "Store sent files on external storage" in Bambu Studio". The url must name the job because merged reports keep an old job's url.
  - TLS: pinned to 1.2 (min and max) for every model, not just P2S/H2C: all models speak 1.2 (H2C negotiated it by default; A1/P1S fetched fine live), and it avoids model detection.
  - Plate: from a `.gcode` path (`plate_N`), else the single `Metadata/plate_N.gcode` inside the 3MF, else 1. Pictures: `plate_N.png`, `top_N.png`, `plate_N_small.png`. Dropped `pick_N.png` (an object-ID mask) and the "any plate" fallback (wrong plate).
- **Skipped / open**: A1 `prot_c` fallback (D1c) not needed: the A1 fetched over `prot_p` in 9 s live. `slice_info.config` extraction (D1e) not in this task's bullets. P2S FTPS untested (421, no SD card). A ranged (REST) download of just the 3MF's thumbnail would cut the ~60 s P1S download to seconds: left for later. Shutdown can still wait on a hung `to_thread` MQTT call (outside this task).
- **Tests**: `test_poll_stall.py` (red before the fix: 0 updates for the fast printer while the other's read blocked; now green), `test_thumbnail.py`, `test_thumbnail_policy.py`, `tests/js/thumbnail_note.test.mjs`. pytest 185 passed, node 37 passed (worktree and integration branch after merge). Timing tests looped 5x with no flakes.
- **Live verification** (temp LOCALAPPDATA copy, deleted after): stall loop GREEN: 29 updates in 90 s, worst gap 3.1 s, while the P1S download took 61.7 s and the A1 one 9.3 s in the background; H2C/P2S didn't open FTPS at all. Process exit mid-download: 8.4 s for an 8 s run (it used to wait for the download). App on port 8093: `/api/printers` showed A1 `has_thumbnail` with key `Magnetic_Heart` (and the PNG was that job's plate), H2C/P2S `thumbnail_unavailable: internal_storage`, no errors in the log. Only `pushall` was sent over MQTT; FTPS was read-only.
- **Reviews**:
  - Self-review (`code-review`, Standards + Spec agents): fixed stale CLAUDE.md text (poll loop, thumbnails), a redundant ternary, the comment wrap in models.py, and a stale-`url` risk (url must name the job; test added). Not changed: test helper duplication (matches the per-file fixtures used elsewhere); `poll_step` stored on the connection (keying by pid would mix up a replaced connection's step); TLS for all models and `/cache` first (decisions above).
  - Independent opus review: no P1/P2. Fixed P3s: a stuck step re-waited 2 s every round; a removed printer broadcast once more; reprint of the same name not re-looked up; `plate_3 holder.3mf` taken as plate 3; substring url match; socket held by the makefile on a failed connect (it also exposed a NameError on a failed TLS wrap, fixed and checked live against the P2S's 421). Test retry delay raised to 0.2 s. Accepted: retries every 15 min on an idle printer with a failing fetch; a new fetch can briefly overlap the cancelled one's closing session.
  - Fresh opus re-review of the fix commit: clean (P3s only). Fixed: url/subtask names with extensions, reprint detection across an UNKNOWN state, a looser broadcast-count bound in the test. Left: a printer removed during `send_json` can be in that one message.
  - ChatGPT/Loom: **not triggered**: no P1/P2. The TLS change doesn't weaken anything, since certificates were never verified on this LAN link. There's also no ChatGPT/Loom channel for this project.
- Severity stays `minor`: the stall fix is contained in the poll loop and the thumbnail path, with regression tests, and adds no new external surface.
- Merged into `bambuddy-improvements` as 340f39d (branch commits 7e02d79, 9f2c3f6, 12880d7, 649240f). Worktree removed and branch deleted.

## Questions
