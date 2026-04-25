# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Bamboozle is a local FastAPI web client that monitors and controls Bambu Lab 3D printers over the LAN via MQTT, with live camera streaming and a WebSocket-driven dashboard.

## Running

The app is a Python package run as a module; there is no installer, test suite, or linter configured.

```bash
cd src
py -m bamboozle.main           # or: python -m bamboozle.main
```

`src/run.bat` is the Windows launcher — it probes the Python Manager install (`%LOCALAPPDATA%\Python\pythoncore-3.14-64`), then `py`, then `python` on PATH. Dependencies in `src/requirements.txt` must be installed into whichever interpreter is used (`pip install -r src/requirements.txt`). Python 3.10+ required (3.14 used in practice).

Working directory matters: `main.py` mounts static files with the relative path `bamboozle/static`, so the server must be launched from `src/`.

On startup `main.py` spawns a thread that opens `http://127.0.0.1:<port>` in the default browser after a 2-second delay.

## Configuration

- Config lives at `%LOCALAPPDATA%\Bamboozle\config.json` (see `config.py`). Absent file → defaults (no printers, port 8080, 3s poll).
- Corrupt JSON is backed up to `config.json.bak` and defaults are loaded.
- Mutations go through `PrinterManager.add_printer` / `update_printer` / `remove_printer`, which call `save_config` — don't write the file directly.
- `PrinterConfig.camera_enabled` is force-reset to `False` on every connection construction (see `PrinterConnection.__init__`), so cameras always start off regardless of saved config.

## Architecture

Single FastAPI app (`bamboozle.main:app`) with a lifespan that builds a `PrinterManager`, stores it on `app.state.manager`, and tears it down on shutdown. Routers pull the manager off `request.app.state.manager`.

**`PrinterManager`** (`printer_manager.py`) is the core. It owns:
- A dict of `PrinterConnection` objects keyed by printer id.
- A set of WebSocket clients.
- A single `_poll_loop` task that sleeps `poll_interval` seconds, reads state from every printer, auto-reconnects any that dropped, and broadcasts `{"type": "update", "printers": {...}}` to all WS clients.

Connections start in the background (`asyncio.create_task(self._connect_printer(...))`) so `start()`, `add_printer`, and `update_printer` never block the HTTP response on MQTT handshakes.

**`PrinterConnection`** wraps three things per printer:
1. A `bambulabs_api.Printer` MQTT client — all `bambulabs_api` calls are synchronous, so reads go through `asyncio.to_thread` (see `_read_sync`, which defensively wraps every getter in try/except and falls back to defaults).
2. A `CameraStream` (see below).
3. The latest `PrinterState` pydantic model (sent to the frontend via `model_dump(mode="json")`).

Thumbnails are fetched once per new print job (keyed on `subtask_name` or `file_name`) via `thumbnail.fetch_thumbnail` and cached on the connection.

**`CameraStream`** (`camera.py`) supports two wire protocols selected by port:
- **Port 6000** (A1 / P1 / P1S): raw TLS socket with a custom 80-byte auth packet (`_build_auth_packet`) and a 16-byte-header framing protocol. Implemented inline.
- **Port 322** (X1 / X1C / H2C / H2D / P2): RTSPS via `ffmpeg` subprocess piping MJPEG to stdout, parsed for JPEG SOI/EOI markers. `ffmpeg` is discovered via PATH or `Program Files\*ffmpeg*\bin\ffmpeg.exe`; if missing, the RTSP stream disables itself.

Both protocols run in a dedicated thread with exponential backoff reconnect (3s → 60s) and expose the latest frame via `get_frame()` behind a lock.

**Commands** (pause/resume/stop/light/speed) go through `PrinterManager.execute_command` → `asyncio.to_thread(_run_command)` → `bambulabs_api` methods. The chamber light uses a hand-rolled `ledctrl` MQTT publish (`_set_light`) because it works across all models where the library helper doesn't.

**Routers** (`bamboozle/routers/`):
- `pages.py` — Jinja2-rendered HTML (`dashboard.html`, `settings.html`, `camera.html`, base layout).
- `api.py` (mounted at `/api`) — REST CRUD for printers plus action endpoints; `POST /api/printers/test` does a throwaway MQTT connection without persisting.
- `ws.py` — WebSocket endpoint the dashboard subscribes to for push updates.
- `stream.py` — MJPEG camera passthrough.

**Frontend** is plain HTML/JS/CSS in `bamboozle/templates/` and `bamboozle/static/` (no build step). The dashboard opens a WebSocket and re-renders on each `update` message.

## Task workflow (Docket)

Tasks live as one file per task under `Tasks/`, named `YYYYMMDD-HHMMSS-slug.md` (UTC timestamp + kebab-case slug, e.g. `20260419-145321-scaffold.md`). The timestamp keeps filenames unique across contributors without coordination. `Tasks/done/` (the archive) IS the log — listed chronologically by filename.

- **The user creates** task files in the main conversation. Initial Status is `todo` with a description of the work.
- **Do NOT execute tasks in the main conversation.** Execution is always delegated to a sub-agent, so the main conversation stays free for the user to queue more tasks. When the user says "Proceed", do not open the task file and start working — dispatch a sub-agent instead. It takes the **earliest** `todo` task (smallest filename = oldest-created). "Proceed with `<slug>`" dispatches for the task whose slug matches.
- **Claude Code dispatch**: use the `Agent` tool with `subagent_type: general-purpose` and `run_in_background: true`. Other agents: use your platform's equivalent sub-agent / background-task mechanism.
- **The sub-agent does the bookkeeping**: Status → `in progress`, append progress/decisions/findings to `## Notes` as it works, Status → `done` when finished, and move the file from `Tasks/` to `Tasks/done/`.
- **If the sub-agent needs input**, it sets Status to `blocked`, writes the question in `## Questions`, and returns. The main conversation flags it in chat so the user can answer. Auto-proceed pauses until the user responds.
- **Auto-proceed**: when a sub-agent finishes a task cleanly (status `done`), immediately dispatch the next-earliest `todo` task in a fresh sub-agent, with no prompt from the user. Surface a one-line "`<slug>` done; starting `<next-slug>`" update in chat. Stop auto-proceeding when: (a) no `todo` tasks remain, (b) a task ends `blocked`, (c) the sub-agent errors or fails verification, or (d) the user says "stop", "pause", or "halt".
- **Halt** (graceful stop): when the user writes "Halt", let any in-flight sub-agent(s) finish their current task normally, but do **not** dispatch the next `todo` after they finish. Surface "`<slug>` done; halted (queue paused)" instead of the usual auto-proceed line. Pending `todo` files stay in `Tasks/` untouched. Resume by saying "Proceed". Distinguished from "stop"/"pause", which serve the same role; "halt" is the explicit form when the user wants to be sure in-flight work isn't killed.
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
