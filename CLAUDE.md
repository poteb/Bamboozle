# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Bamboozle is a local FastAPI web client that monitors and controls Bambu Lab 3D printers over the LAN via MQTT, with live camera streaming and a WebSocket-driven dashboard.

## Running

The app is a Python package run as a module; there is no installer or linter configured.

```bash
cd src
py -m bamboozle.main           # or: python -m bamboozle.main
```

`src/run.bat` is the Windows launcher — it probes the Python Manager install (`%LOCALAPPDATA%\Python\pythoncore-3.14-64`), then `py`, then `python` on PATH. Dependencies in `src/requirements.txt` must be installed into whichever interpreter is used (`pip install -r src/requirements.txt`). Python 3.10+ required (3.14 used in practice).

Tests: `pip install -r src/requirements-dev.txt`, then run `python -m pytest` from `src/` (`src/pytest.ini` sets `pythonpath = .`). `tests/conftest.py` points `LOCALAPPDATA` at a temp dir so tests never touch the real config. Network adapters (`discovery.tcp_reachable`, `discovery.fetch_cert_common_name`) are monkeypatched in tests; `PrinterManager(..., discovery_port=None)` disables the UDP listener.

Captured MQTT reports from the user's A1 / P1S / H2C / P2S live in `src/tests/fixtures/reports/*.jsonl.gz` (masked; format and contents in its README), loaded with `tests/report_fixtures.py`. Re-capture with `python -m tools.capture_reports capture` then `mask` from `src/`: it sends only a standalone `pushall` and `get_version` per printer and writes the raw capture outside the repo.

JS tests: `node --test "src/tests/js/*.test.mjs"` from the repo root (Node 22+, no npm deps). They load `static/app.js` into a `node:vm` context with stubbed `document` / `fetch` / `WebSocket` / `setTimeout` and stub out `render()`, so they cover state logic, not card DOM.

Working directory matters: `main.py` mounts static files with the relative path `bamboozle/static`, so the server must be launched from `src/`.

On startup `main.py` spawns a thread that opens `http://127.0.0.1:<port>` in the default browser after a 2-second delay.

## Configuration

- Config lives at `%LOCALAPPDATA%\Bamboozle\config.json` (see `config.py`). Absent file → defaults (no printers, port 8080, 3s poll).
- Corrupt JSON is backed up to `config.json.bak` and defaults are loaded.
- Mutations go through `PrinterManager.add_printer` / `update_printer` / `remove_printer`, which call `save_config` — don't write the file directly.
- `PrinterConfig.model` is the SSDP `DevModel` code (`""` until heard). `PrinterManager._learn_model` backfills it from an announcement sent from the printer's configured IP: it sets the field and calls `save_config` directly, with no reconnect and no history backup. Relocation carries it over. Add and edit fill an empty model from the discovery cache when the cached announcement came from the config's IP within the cache TTL; an edit that changes the serial clears it.
- `PrinterConfig.camera_enabled` is force-reset to `False` on every connection construction (see `PrinterConnection.__init__`), so cameras always start off regardless of saved config.

## Architecture

Single FastAPI app (`bamboozle.main:app`) with a lifespan that builds a `PrinterManager`, stores it on `app.state.manager`, and tears it down on shutdown. Routers pull the manager off `request.app.state.manager`.

**`PrinterManager`** (`printer_manager.py`) is the core. It owns:
- A dict of `PrinterConnection` objects keyed by printer id.
- A set of WebSocket clients.
- A single `_poll_loop` task that sleeps `poll_interval` seconds, polls every printer concurrently (health checks, auto-reconnect, state read), and broadcasts `{"type": "update", "printers": {...}}` to all WS clients. Each printer's poll step gets `POLL_STEP_TIMEOUT_SECONDS` (2 s); a slower one is broadcast with its last known state, and the next round waits on the same step instead of starting another. Never put blocking network I/O in a state read: one slow printer used to freeze every card.

Connections start in the background (`asyncio.create_task(self._connect_printer(...))`) so `start()`, `add_printer`, and `update_printer` never block the HTTP response on MQTT handshakes.

**`PrinterConnection`** wraps three things per printer:
1. A `bambulabs_api.Printer` MQTT client — all `bambulabs_api` calls are synchronous, so reads go through `asyncio.to_thread` (see `_read_sync`, which defensively wraps every getter in try/except and falls back to defaults).
2. A `CameraStream` (see below).
3. The latest `PrinterState` pydantic model (sent to the frontend via `model_dump(mode="json")`).

Thumbnails come from the job's 3MF over FTPS (`thumbnail.fetch_thumbnail`, TLS 1.2, one connection walking `/cache`, `/`, `/model`, `/data` for `gcode_file` / `subtask_name` name variants). `PrinterConnection` starts the fetch after a state read, as a background task on a daemon thread (shutdown never waits for it), at most one per printer, with a 240 s deadline, cancelled on disconnect. It's keyed on `subtask_name` or `file_name`: a 3MF that isn't on the printer is not looked up again for that job; other failures retry after 60 s, doubling to 15 min. FTPS (port 990) only serves external storage, so a job on internal storage (`sdcard: false`, or a `brtc://emmc/` / `file:///userdata/` `url` naming the job) is never looked up; `PrinterState.thumbnail_unavailable = "internal_storage"` and the card says why.

**`CameraStream`** (`camera.py`) supports two wire protocols selected by port:
- **Port 6000** (A1 / P1 / P1S): raw TLS socket with a custom 80-byte auth packet (`_build_auth_packet`) and a 16-byte-header framing protocol. Implemented inline.
- **Port 322** (X1 / X1C / H2C / H2D / P2): RTSPS via `ffmpeg` subprocess piping MJPEG to stdout, parsed for JPEG SOI/EOI markers. `ffmpeg` is discovered via PATH or `Program Files\*ffmpeg*\bin\ffmpeg.exe`; if missing, the RTSP stream disables itself. Arguments come from `rtsp_command(ffmpeg, url, profile)`; every profile reads unbuffered (`-fflags nobuffer -flags low_delay`). `LOW_LATENCY` (tiny probe) is the default; `TOLERANT` (bigger probe + `-use_wallclock_as_timestamps 1`, since P2S RTP timestamps don't advance) is used for P2S per `rtsp_profile(model)`. For other models, until `LOW_LATENCY` has shown a moving picture (2+ distinct frames) in the current run, an unhealthy or frozen `LOW_LATENCY` session switches to `TOLERANT`, and a `TOLERANT` session with no moving picture switches back, so an unreachable or busy printer can't pin a profile (`_choose_fallback`).

Both protocols run in a dedicated thread and expose the latest frame via `get_frame()` behind a lock. A healthy session (its frames span at least 10 s) is respawned after 0.5 s and keeps its last frame on screen, because X1/P2 RTSP sessions drop routinely. Other sessions back off 3 s → 60 s; if they delivered nothing, they also clear the frame. `stop()` interrupts the session's blocking I/O: it kills ffmpeg, or closes the port-6000 socket, including mid-handshake. Callers run `start()`/`stop()` via `asyncio.to_thread`, because `stop()` joins the thread. If an old capture thread is still alive, the new one waits for it, so only one connection ever exists per camera. `get_frame()` (and so `camera_available`) returns `None` for a frame older than 20 s. On port 322 a watchdog thread kills ffmpeg when the picture hasn't changed for 20 s, since a dead upstream can leave ffmpeg repeating its last frame. If the respawned ffmpeg shows the byte-identical picture, the scene is still, and the timeout becomes 300 s until the picture changes. On port 6000, `raw_frames` raises (and the session reconnects) on an implausible payload size or a non-JPEG payload, rather than parsing mid-frame. ffmpeg args live in `rtsp_command`.

Before `mqtt_start`, `PrinterConnection.connect` (and `POST /api/printers/test`) TCP-probes port 8883 and reports "printer unreachable at <ip>" on failure — `mqtt_start` itself is paho `connect_async` and never fails, which used to surface only as the library's "Printer Values Not Available Yet". The poll loop schedules reconnects as background tasks so an unreachable printer can't stall updates.

A refused MQTT login (CONNACK 134/135) is caught through the library's `mqtt_client.on_connect_handler` and reported as "access code wrong or changed": `PrinterState.last_error = "auth_rejected"` (shown on the card), a log line, and the `POST /api/printers/test` message. That endpoint waits for the CONNACK and then a first report, so it can also say "timed out" or "check the serial number".

**Teardown** (`mqtt.retire`): the library's `mqtt_stop()` only joins paho's network thread (no DISCONNECT) and can block for a whole TLS handshake. `retire` clears the paho callbacks inline, then runs disconnect → `loop_stop` → disconnect on a daemon thread, so edit / relocate / remove / reset / the Settings test never wait on paho. `reset_session` retires the client and builds a fresh `bl.Printer` (`_new_printer` wires the handlers) rather than restarting the old one. `PrinterManager.stop` gives pending teardowns up to `SHUTDOWN_TEARDOWN_SECONDS` so the DISCONNECTs go out. Don't call `mqtt_stop` directly.

**Staleness**: `on_message_handler` stamps every report. A connected session silent for `PROBE_SECONDS` (30) gets one standalone pushall; at `STALE_SECONDS` (60) `is_offline()` is true even with the socket up (P1-series brokers can go silent), so the poll loop resets it. `last_error` becomes `"no_reports"` (zero reports since connect: wrong or wrongly cased serial; logged once) or `"stale"`, shown on the card until a report arrives. The reconnect backoff only resets on a session that delivered reports.

**`protocol.py`** holds Bambu protocol facts as pure functions over the report's `print` dict (plus CONNACK helpers), so they're unit-testable without MQTT. `PrinterState.developer_mode` comes from the `fun` bitfield (bit `0x20000000` set = off; `None` on A1/P1, which don't send it). It is informational only: the user's H2C and P2S report it off (and carry HMS `0500050000010007`, "command verification failed", a record of a past refusal), yet light toggles work. So the card shows a quiet hint that print controls may be ignored (`cardNotice` → `quiet`; a `<details>` whose body holds the fix, so it works without hover) and `execute_command` never fails a command because of it; the publish result alone decides. The HMS is not used as a fallback on printers without `fun` (unknown shows nothing).

**`report.py`** decodes the readable status (`report.status_fields(print, info, model)` → `PrinterState` fields), also pure over the raw dump so it survives a client swap: temps and targets (H2 packs `target<<16|current`; `nozzles[]` lists left then right on H2C/H2D from `device.extruder.info`, id 0 = right), chamber (`device.ctc`, or the flat field on X1 only; A1/P1 send junk, so `None`), `stg_cur` stage names (0 only counts while RUNNING/PAUSE: the A1/P1 report 0 when idle; none after FINISH/FAILED), fans (0–15 → %), AMS `humidity_raw`/`temp` (never the inverted 1–5 `humidity` index), Wi-Fi, door (`stat` bit 23 on H2/P2S, `home_flag` on X1, `None` otherwise), SD, firmware and `spd_lvl` names. Model-dependent rules key on `PrinterConfig.model` ("" until SSDP is heard). The firmware comes from an `info` reply: the library's connect-time `get_version` shares a message with pushall and goes unanswered, so `read_state` sends its own (`mqtt.version_request`) until it's known, waiting 60 s doubling to 15 min between unanswered requests. On the card, low-value fields sit behind a quiet "Details" disclosure (`detailItems`); only its body is rewritten on update so it stays open.

**`hms.py`** decodes HMS messages and `print_error` (`hms.printer_errors(print)` → `PrinterState.errors`), pure like `report.py`. Only the code structure is decoded (HMS `attr` = module/unit/part bytes, `code` high half = level 1 fatal … 4 info, level 0 skipped; `print_error` = module+unit then error, top two bits = level, below `0x4000` skipped). The message text is NOT bundled: Bambu's wiki is all rights reserved, Bambu Studio's tables are AGPL and ha-bambulab's (MIT repo) are Bambu's catalogue text. HMS codes link to Bambu's redirector `e.bambulab.com/index.php?e=<16 hex>&s=device_hms&lang=en` (what Bambu Studio opens; falls back to the HMS home), `print_error` to the wiki's error-code table with a `#:~:text=MMMM-EEEE` fragment (it has no per-code anchors). User-cancel echoes `0300_400C` / `0500_400E` are dropped; a `print_error` also raised as HMS is listed once. Errors are `active` when they're a fatal HMS (job or not), or while the job is PAUSE on its own (not a user/G-code pause, `stg_cur` 5/16/30) or FAILED with a real `print_error` (not a cancel echo or 0) for the print error and serious HMS. Record-only codes (`0500050000010007`, which all four of the user's printers carry) get no severity and our own description, so they're never active. Everything else is lingering (the printer keeps HMS until acknowledged on it). The card shows active ones in a red block and lingering ones in a quiet collapsed "N printer messages" line (`errorsHtml`, rewritten only when the errors change so an open list stays open).

**`hms_catalog.py`** adds Bambu's own text to the decoded errors (`hms_catalog.describe(errors, serial)`, called right after `hms.printer_errors`). It's Bambu's public catalogue (what Bambu Studio downloads), fetched at runtime and never bundled or used as a test fixture: `e.bambulab.com/query.php?lang=en` (generic) plus `&d=<serial[:3]>` per model (more codes; only the 3-character prefix leaves the machine; DevModel codes return nothing, and an unknown prefix answers `result: 201`). `device_hms` ecodes are 16 hex, `device_error` (print_error) 8 hex. `HmsCatalog.start` (from `PrinterManager.start`, serials re-read from the config each minute) loads `%LOCALAPPDATA%\Bamboozle\hms-cache\en[-PFX].json`, refetches entries a week old on a daemon thread (20 s timeout, 4 MB cap, a non-urllib User-Agent since Bambu's CDN 403s Python's), and keeps the stale cache when a fetch fails (retry 10 min doubling to 6 h). All cache files load before any fetch. An empty answer (`result: 201` or no entries) never replaces a non-empty cache; with nothing cached it's kept, but asked again after a day. The 20 s is an overall deadline (urllib's timeout is per socket operation). Lookup: model text, else generic text; a code listed only with empty text gets "No description from Bambu (internal code)" (`undocumented`, sorted last after the active ones); unlisted codes and a missing catalogue keep the code-only line. Our own descriptions (the refused-command record) win. The card leads with the text (severity first), the module and linked code below in small type; the text goes in with `textContent` (`applyErrors` fills empty `.error-text` spans), never as HTML. Tests: `conftest.py` replaces `hms_catalog.http_get` with one that fails and gives each test an empty `CATALOG`.

**IP auto-detect** (`discovery.py`): `SsdpListener` passively listens on UDP 2021 (SO_REUSEADDR, shared with Bambu Studio; bind failure just disables the feature) for Bambu SSDP announcements (`Location` = IP, `USN` = serial). `PrinterManager.handle_announcement` matches on serial only (printers behind a repeater share one MAC), acts only when the printer isn't healthy at its saved IP, requires the TLS cert CN at the new IP to equal the serial (true for A1, P1S, H2C, P2S), then goes through `update_printer` and broadcasts `{"type": "notice"}` (shown as a toast).

**Discover printers**: every announcement (configured or not) is also cached in memory on `PrinterManager` (keyed by upper-cased serial, latest wins, dropped after 5 min, capped at 64). `GET /api/discovery` returns `{"listening", "printers"}` with configured serials filtered out at request time; `discovery.model_info` maps `DevModel` codes to a friendly name + camera port (unknown → raw code, no port). The Settings Add form polls it every 5 s and a pick fills IP / serial / camera port (and Name if blank). The route sits outside `/printers/...` because `GET /printers/{printer_id}` would swallow it.

**Commands** (pause/resume/stop/light/speed/refresh) go through `PrinterManager.execute_command` → `asyncio.to_thread(_run_command)`. Pause/resume/stop/speed use `bambulabs_api` methods; light and refresh skip the library and go through `mqtt.publish` (QoS 1, string `sequence_id`, never waits, refuses while the session is down so paho can't replay it later; the paho inflight window is raised to 1000 in `mqtt.configure`). The light sends `ledctrl` to both `chamber_light` and `chamber_light2` (H2 models have two). Refresh (`POST /api/printers/{id}/refresh`, card button) sends a standalone pushall (a query).

**Routers** (`bamboozle/routers/`):
- `pages.py` — Jinja2-rendered HTML (`dashboard.html`, `settings.html`, `camera.html`, base layout).
- `api.py` (mounted at `/api`) — REST CRUD for printers plus action endpoints; `POST /api/printers/test` does a throwaway MQTT connection without persisting and reports why it failed (unreachable, access code refused, timeout, no data). `GET /api/discovery` lists unconfigured printers heard on the LAN.
- `ws.py` — WebSocket endpoint the dashboard subscribes to for push updates.
- `stream.py` — MJPEG camera passthrough.

**Frontend** is plain HTML/JS/CSS in `bamboozle/templates/` and `bamboozle/static/` (no build step). On load the dashboard fetches `GET /api/printers` over HTTP and renders right away (browsers can throttle WS reconnects, Firefox by up to ~60s), then opens a WebSocket and re-renders on each `init`/`update`. Once any WS state has been applied, a late HTTP response is ignored. A WS `init` replaces the printer set; `update` merges. `#ws-status` ("Reconnecting…") shows while the WS isn't open, after a 1s grace period from page load (`WS_MARKER_GRACE_MS`); it is invisible while empty and stays rendered, so its `role="status"` live region announces the text when set. While the WS isn't open, `sendCommand` falls back to the REST action endpoints (`/api/printers/{id}/pause|resume|stop|light|speed|refresh`) and toasts the result.

## Task workflow (Docket)

Tasks live as one file per task under `Tasks/`, named `YYYYMMDD-HHMMSS-slug.md` (UTC timestamp + kebab-case slug, e.g. `20260419-145321-scaffold.md`). The timestamp keeps filenames unique across contributors without coordination. `Tasks/done/` (the archive) IS the log — listed chronologically by filename.

- **The user creates** task files in the main conversation. Initial Status is `todo` with a description of the work.
- **Do NOT execute tasks in the main conversation.** Execution is always delegated to a sub-agent, so the main conversation stays free for the user to queue more tasks. When the user says "Proceed", do not open the task file and start working — dispatch a sub-agent instead. It takes the **earliest** `todo` task (smallest filename = oldest-created). "Proceed with `<slug>`" dispatches for the task whose slug matches.
- **Claude Code dispatch**: use the `Agent` tool with `subagent_type: general-purpose` and `run_in_background: true`. Other agents: use your platform's equivalent sub-agent / background-task mechanism.
- **The sub-agent does the bookkeeping**: Status → `in progress`, append progress/decisions/findings to `## Notes` as it works, Status → `done` when finished, and move the file from `Tasks/` to `Tasks/done/`.
- **If the sub-agent needs input**, it sets Status to `blocked`, writes the question in `## Questions`, and returns. The main conversation flags it in chat so the user can answer. Auto-proceed pauses until the user responds.
- **Auto-proceed**: when a sub-agent finishes a task cleanly (status `done`), immediately dispatch the next-earliest `todo` task in a fresh sub-agent, with no prompt from the user. Surface a one-line "`<slug>` done; starting `<next-slug>`" update in chat. Stop auto-proceeding when: (a) no `todo` tasks remain, (b) a task ends `blocked`, (c) the sub-agent errors or fails verification, or (d) the user says `Halt`, `stop`, or `pause`.
- **Halt vs stop**: `Halt` is graceful — do not dispatch any further `todo` tasks, but let in-flight sub-agent(s) run to completion. `stop` and `pause` are immediate — cancel in-flight sub-agent(s) using the platform's cancel mechanism (e.g. Claude Code's `TaskStop`). A cancelled task stays in `Tasks/` with Status `in progress` and whatever partial `## Notes` were written; the user decides whether to revert it to `todo`, edit it, or delete it. Sub-agents do not auto-resume `in progress` tasks. With no sub-agent in flight, `Halt` and `stop` are equivalent — both just prevent the next dispatch.
- **Brief the sub-agent well**: sub-agents start with no conversation context. The dispatch prompt must tell the sub-agent to read its task file by path, follow these workflow rules, and include any decisions from prior tasks it needs. Point it at relevant files by path.

### Status vocabulary
`todo` | `in progress` | `blocked` | `done`

### Task file template

````markdown
# <title>

## Status
todo  <!-- todo | in progress | blocked | done -->

## Task
<what needs to be done>

## Notes
<agent appends progress, decisions, findings as work proceeds>

## Questions
<optional; only when status is blocked>
````
