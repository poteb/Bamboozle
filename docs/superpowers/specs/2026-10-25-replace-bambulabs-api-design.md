# Replace `bambulabs_api` with an in-house paho client: design

Docket task `20261025-123740-replace-bambulabs-api` (severity major). Research: `docs/research/bambuddy.md` §1.3 A5, §1.4, TL;DR #10–#14. The user delegated the design decisions to that document (Grilling: resolved). BamBuddy is AGPL: ideas only, no code.

## 1. Goal

Drop `bambulabs_api`. Talk to the printers with a thin paho-mqtt client of our own that:

- is event-driven: every inbound report updates the printer's state and pushes a debounced, deduped WS update (no 3 s poll for state);
- deep-merges partial pushes (AMS per unit and tray, `tray_exist_bits`, `vir_slot` precedence);
- sends `pushall` on connect and on demand only (never on a timer; the library sent one every 60 s);
- publishes commands with QoS 1 and verifies them by watching for the resulting state.

Every existing feature keeps working: auth refusal, staleness (30 s probe, 60 s stale), Developer Mode hint, per-printer poll timeouts, background thumbnails, `get_version` with backoff, model backfill, camera, SSDP relocation with the cert CN check, the Settings connection test, the light on both LED nodes, and Refresh.

Non-goals: new UI features; changing the decoders' output (`report.py`, `hms.py`, `protocol.py` stay pure and keep their contracts); camera and discovery code (untouched).

## 2. Architecture

```
paho network thread                     asyncio event loop
───────────────────                     ───────────────────────────────────────────
BambuClient._on_message ─ decode ──►    PrinterConnection._ingest(payload)
  (stamps last-report time)  call_soon_threadsafe   │ MergedReport.apply()   (deep merge)
                                                   │ resolve command waiters
                                                   └► on_change() ─► PrinterManager._mark_dirty(pid)
                                                                      │ 0.3 s throttle
                                                                      ▼
                                                    flush: read_state() → dedupe → WS "update" {pid: state}
PrinterManager._poll_loop (every poll_interval, 3 s): silence probe, stale/reset, reconnect,
   recompute state → broadcast only printers whose state changed; full heartbeat every 30 s.
```

### 2.1 `mqtt.py`: `BambuClient`

One instance per MQTT session (a reset builds a new one, as today). Owns a paho `Client`:

- MQTT 3.1.1, unique client id `bamboozle-<pid>-<n>` (fresh per instance, so the broker drops an old session and paho's local QoS 1 queue dies with the old client). No serial or access code in the id.
- User `bblp`, password = access code (never logged).
- TLS via `tls_set_context`: `ssl.SSLContext(PROTOCOL_TLS_CLIENT)`, `check_hostname=False`, `CERT_NONE` (printers use self-signed certs; identity is checked by the cert-CN rule on relocation, unchanged), `minimum_version = TLSv1_2`. This context loads no CA store, so building a client is cheap.
- keepalive 30 s, `reconnect_delay_set(1, 30)`, `max_inflight_messages_set(1000)` (Bambu PUBACK race, existing `INFLIGHT_WINDOW`).
- `start()`: `connect_async` + `loop_start` (never blocks on the network).
- `on_connect`: reports the CONNACK reason code to the owner; on success subscribes to `device/<serial>/report`.
- `on_subscribe` (success): publishes a standalone `pushall` (A1/P1S ignore combined requests). This is the "pushall on connect", once per (re)established session, including paho's own reconnects.
- `on_message`: payload over 1 MiB is dropped; bytes decoded UTF-8 with `errors="replace"`; `json.loads`; anything but a JSON object is dropped (logged at debug). The dict goes to the owner's `on_report` callback, on the paho thread.
- `on_disconnect`: tells the owner (state recompute only).
- `publish(payload) -> bool` (module function `mqtt.publish(client, payload)` kept, as today): QoS 1 to `device/<serial>/request`; refuses (False) while not connected so paho can't replay it later; never waits.
- `mqtt.retire(client)`: the existing non-blocking teardown (callbacks cleared inline, then disconnect → loop_stop → disconnect on a daemon thread, watched for 60 s). `wait_for_teardowns` unchanged.
- Request builders (pure): `pushall_request`, `version_request`, `light_requests(mode)` (unchanged) plus `pause_request`, `resume_request`, `stop_request`, `speed_request(level)`.

### 2.2 `merge.py`: `MergedReport` (pure, no I/O)

Holds the merged `print` dict and the latest `info` (get_version reply). `apply(payload) -> Frame` classifies and merges one message; `Frame` says what it was (`status`, `full`, `version`, `command echo` with `command` / `sequence_id` / `result` / `reason`, or `other`).

Classification:

- `print` with `command` == `push_status`, or with no `command`: a **status frame**. Full when `msg` == 0, otherwise (including a missing `msg`) a delta.
- `print` with `command` == `project_file`: only its `url` is merged into the state (`protocol.file_on_internal_storage` reads it). Nothing else: its `job_id` is an int where status has a string, and it isn't telemetry.
- any other `print` / `system` command (pause / ledctrl echoes, `sntp`, other clients' commands): not merged; returned as a command echo for verification.
- `info` with `command` == `get_version`: replaces `info` (other clients' replies carry the same data).

Merge rules for a status frame (`merge(dst, src, full)`):

- dicts merge recursively, key by key; a key absent from the frame keeps its value (so `url` and other keys only some frames carry survive full reports);
- a scalar or `null` replaces;
- lists: in a **full** report, a list replaces the old one (the full report is authoritative: removed AMS units, emptied HMS). In a **delta**, a list whose items are all dicts with an `id` is merged item by item on `id` (AMS units `ams.ams`, trays `tray`, `vir_slot`, `device.extruder.info`, `device.nozzle.info`), one whose items all have a `node` is merged on `node` (`lights_report`), and any other list replaces (`hms`, `net.info`, …). Items not in the delta are kept;
- a tray in a delta whose keys are only `id` / `state` is the empty-slot shape (A1 `{"id":"1"}`, H2C `{"id":"1","state":10}`) and **replaces** the old tray rather than merging into it (otherwise a removed spool would keep its filament);
- `ams.tray_exist_bits`, when present in a frame, clears the filament of every slot of units 0–3 whose bit (`unit*4 + slot`) is 0 (the tray becomes `{"id": slot}`); `ams.ams_exist_bits`, when present, drops units 0–3 whose bit is 0. AMS HT (unit 128+) is left to full reports: its bit layout (`tray_exist_bits` bit 16 on the H2C) isn't confirmed. Bits are applied when they arrive, and a later tray update is trusted: the A1 sends `{"tray_exist_bits":"f"}` seconds before the unit with the new spool;
- `vir_slot` beats `vt_tray`: both are kept as merged; the filament reader uses `vir_slot` whenever it is a non-empty list (already the case), so a single-dict `vt_tray` never overrides multi-slot data;
- recursion is bounded (depth 32); deeper structures replace.

`reset()` empties it (a new session starts from nothing, as a new library client did).

### 2.3 `report.py` additions (pure)

`job_fields(print)`: `gcode_state` (known values IDLE / PREPARE / RUNNING / PAUSE / FINISH / FAILED; anything else or missing → `UNKNOWN`, exactly as the library's enum did, so the UI sees no new values), `progress` (`mc_percent`), `remaining_minutes` (`mc_remaining_time`), `current_layer` (`layer_num`), `total_layers` (`total_layer_num`), `print_speed` (`spd_mag`), `light_on` (`lights_report` node `chamber_light`, else the first entry, mode `on`), `gcode_file`, `subtask_name`. Numbers accept ints, floats and numeric strings; anything else is 0.

`filaments(print)`: the current raw-dump filament reader moved here unchanged (AMS trays, `vir_slot` else `vt_tray`), minus the library fallback.

`status_fields` is unchanged.

### 2.4 `PrinterConnection`

- `self.client: mqtt.BambuClient` replaces `self.printer`; `self.report = MergedReport()`.
- paho thread: `_on_connack(rc)` (same flags logic as today), `_on_report(payload)` stamps `_last_report` / `_reports` / `_probed` / `_down_reason` as today and hands the payload to the loop with `call_soon_threadsafe(self._ingest, payload)` (the loop is captured in `connect()`; a closed loop drops it).
- loop: `_ingest(payload)`: `frame = report.apply(payload)`; resolve command waiters; `on_change()` (a callback the manager sets).
- `read_state()`: builds `PrinterState` from `report` (pure decoders), no thread hop; keeps the thumbnail scheduling and the `get_version` backoff (publish via `to_thread`). The thumbnail task calls `on_change()` when it finishes so the card updates without waiting for the next housekeeping round.
- `reset_session()`: retires the client, builds a new one, `report.reset()`, resets the version backoff (as today).
- Staleness, auth refusal, backoff, grace: unchanged logic, on `client.is_connected()`.
- Command waiters: `watch(command) -> Waiter`, registered before publishing. A waiter holds the command's `confirm(print_state, frame)` predicate and the request `sequence_id`s. It is checked once at registration (frame `None`) and after every ingested frame. A command echo with a matching `sequence_id` and `result` "fail"/"failed" is remembered; it only decides the outcome if the state never confirms.

### 2.5 `commands.py` (pure)

`build(action, params) -> Command`: the requests to publish, the confirmation predicate, the label and done-message, and whether it's a print control. Params are untrusted (WS JSON): `set_speed` needs an int level 1–4, `set_light` a mode of `on` / `off`; anything else is rejected before anything is sent.

| action | request(s) | confirmed when |
|---|---|---|
| pause | `{"print":{"command":"pause","param":"","sequence_id":N}}` | `gcode_state` == PAUSE |
| resume | `{"print":{"command":"resume","param":"","sequence_id":N}}` | `gcode_state` in RUNNING, PREPARE |
| stop | `{"print":{"command":"stop","param":"","sequence_id":N}}` | `gcode_state` in FINISH, FAILED, IDLE |
| set_speed L | `{"print":{"command":"print_speed","param":"L","sequence_id":N}}` | `spd_lvl` == L |
| light_on / light_off / set_light | `ledctrl` to `chamber_light` and `chamber_light2` (unchanged) | `lights_report` `chamber_light` mode == requested |
| refresh | standalone `pushall` | a full status report arrives |

The payloads are the same for all four models: OpenBambuAPI `mqtt.md` documents `pause` / `resume` / `stop` with an empty `param` and `print_speed` with the level as a decimal string, sent with QoS 1; Bambu Studio's DeviceManager builds the same objects for every model (no model branch), with an incrementing string `sequence_id`. Unit-tested per model; never sent to a real printer in testing.

### 2.6 `PrinterManager`

- Connections are created through one helper that sets `conn.on_change = lambda: self._mark_dirty(pid, conn)`.
- `_mark_dirty`: if no flush is pending for that printer, schedule one in `PUSH_DEBOUNCE_SECONDS` (0.3 s). So a burst of reports becomes one update, and a printer pushing every second gets about one update per report.
- flush: if `conn` is still the printer's connection, `await conn.read_state()`, then `_broadcast_changed({pid})`.
- `_broadcast_changed(pids)`: dumps each state; drops `timestamp`; sends `{"type":"update","printers":{…}}` with only the printers whose dump differs from the last one sent (dedupe). Removed printers are forgotten.
- `_poll_loop` keeps its shape (concurrent per-printer steps, 2 s step timeout, a stuck step isn't restarted) but its steps only do housekeeping + a state recompute. After a round it broadcasts the printers that changed (connect / offline / stale / camera / thumbnail changes that no report triggers), and every `HEARTBEAT_SECONDS` (30) all printers, so a missed message can't leave a card wrong for long.
- `execute_command(pid, action, params) -> CommandResult(success, message, confirmed)`:
  - unknown printer / not connected / invalid params → `success=False`, `confirmed=None`;
  - publish failed → `success=False`, "Not sent: printer not connected";
  - confirmed within `COMMAND_CONFIRM_SECONDS` (10) → `success=True, confirmed=True`, e.g. "Print paused";
  - not confirmed, printer echoed a failure → `success=False`, "Printer refused pause: <reason>";
  - not confirmed otherwise → `success=True, confirmed=False`, "Pause sent, but not confirmed: the printer didn't report the change within 10 s". For print controls on a printer reporting Developer Mode off, it adds "(Developer Mode is off: the printer may ignore print controls)". Never a failure because of Developer Mode.

### 2.7 Routers and frontend

- REST action endpoints return `{"success", "message", "confirmed"}`; the WS `command_result` carries `confirmed` too.
- `ws.py` runs each command in its own task, so a 10 s confirmation wait doesn't stop the socket from receiving the next message. It also rejects a handshake whose `Origin` doesn't match its `Host` (see §4).
- `POST /api/printers/test`: a throwaway `BambuClient` + `MergedReport`; waits for the CONNACK (10 s), the first report (5 s) and a `gcode_state` (5 s more; the client's on-connect pushall asks for it); same messages as today; always retired.
- `app.js`: a command result with `success` and `confirmed === false` shows an `info` toast instead of `success` (WS and REST fallback alike).

## 3. Testing

- `merge.py` unit tests per rule (dict merge, list replace in full, id / node merge in delta, empty-slot tray, `tray_exist_bits` / `ams_exist_bits`, `vir_slot` kept, project_file url only, echoes not merged, depth bound).
- Fixture replays (`tests/report_fixtures.py`), all four printers: every message in order through `MergedReport`, then the decoders:
  - P1S: once the first full report has been seen, the 4 AMS trays (and the AMS humidity) are present after every message;
  - A1: trays follow `tray_exist_bits` and the later unit push; never blank after the first full report; the print start (FINISH → PREPARE → RUNNING) shows through;
  - at every full report, the state decoded from the merge equals the state decoded from that full report alone (filaments, temps, AMS, stage, errors, job fields);
  - H2C / P2S have no deltas in the fixtures, so deltas are synthesised from consecutive full reports (only the keys that changed, `msg` 1), replayed after the first full report, and must decode to the same state as the full reports.
- Command payload tests per model, confirmation predicates, verification outcomes (confirmed, timeout, refused echo, already-in-state, publish failure, invalid params).
- Event flow tests: report → one debounced WS update; identical reports → no second update; burst → one update; removed printer not broadcast; heartbeat.
- Existing tests are ported off `bambulabs_api` (fake paho / patched `BambuClient.start`); none deleted without an equivalent.
- No test or code path imports `bambulabs_api` (checked by a test that blocks the import).

## 4. Threat model (security-and-hardening)

Assets: printer control (pause/stop can ruin a print), the access codes, the user's LAN printers' availability.

Trust boundaries:
1. Browser ↔ server: HTTP/WS on 127.0.0.1, no auth. Any web page the user visits can try cross-origin requests.
2. Server ↔ printer: MQTT over TLS on the LAN, printer cert not verified (self-signed).
3. Printer → server: report payloads (JSON) parsed and merged.

STRIDE / abuse cases, and what this change does:
- **Cross-site WebSocket hijacking (S/T/E):** a malicious page opens `ws://127.0.0.1:8080/ws`, reads the printer ids from `init` and sends `stop`. Pre-existing. Mitigation: reject the WS handshake when an `Origin` header is present and its host:port differs from `Host`. First abuse-case test. (REST: action endpoints need the random 12-hex printer id, which a cross-origin page can't read without CORS, so blocking the WS leak closes the practical path.)
- **Untrusted command params (T):** WS `params` interpolated into MQTT payloads (`speed_lvl` went into `param` unchecked). Mitigation: strict validation in `commands.build` (int 1–4, on/off). Test.
- **Malformed / hostile reports (D/T):** non-UTF-8, non-JSON, huge, deeply nested payloads from the LAN. Mitigation: size cap 1 MiB, `errors="replace"`, objects only, bounded merge depth, merge/decoders never raise into paho's thread or the loop (exceptions logged). Tests.
- **Replay of stale commands (T):** a QoS 1 command queued while down replayed later. Mitigation kept: `publish` refuses while disconnected; fresh client id per session drops paho's queue.
- **Credential disclosure (I):** access code only in `username_pw_set`; never logged or put in the client id; reports never contain it.
- **Broker session exhaustion (D):** printers allow few clients. One session per printer, clean DISCONNECT on retire (kept).
- **MITM on the LAN (S):** certs unverified (pre-existing, accepted for self-signed printer certs). Out of scope; relocation still requires CN == serial.

## 5. Decisions and why

1. Merge on the event loop, not the paho thread: all state is single-threaded (no locks); `call_soon_threadsafe` keeps arrival order.
2. Full reports deep-merge dicts but replace lists; deltas merge id/node lists. Keeps out-of-band keys (`url`) and still honours removals in full reports.
3. Event-based `tray_exist_bits` clearing (on arrival only) so the A1's bits-then-unit ordering can't blank a fresh spool.
4. AMS HT untouched by exist bits (layout unconfirmed); full reports refresh it.
5. pushall on every SUBACK (each session, including paho's reconnects), the 30 s silence probe (existing), and Refresh. No timer. No extra rate limit: our reconnect backoff (3 → 60 s) and paho's (1 → 30 s) already bound it, and none of the user's printers is a P1P.
6. 0.3 s throttle (not a reset-on-every-message debounce) so a printer pushing every second can't starve its own updates.
7. Dedupe on the whole state dump minus `timestamp`: anything visible changing is broadcast; nothing else is.
8. Heartbeat every 30 s with all printers, so a lost frame or a missed recompute heals.
9. Commands wait for confirmation before answering (one toast per click, up to 10 s). The card itself updates as soon as the state changes.
10. A refused echo only counts if the state never confirms (the light works on H2C/P2S despite Developer Mode off; an echo must not overrule the state).
11. A command already in the requested state counts as confirmed (pressing pause on a paused printer is a no-op).
12. `rtsp_url` IPs are not used for anything (they differ from the configured IP).
13. The merged state is reset with each new client (reset_session), matching what a new library client did; paho's own reconnect keeps it and the on-connect pushall refreshes it.

## 6. Risks

- H2C/P2S deltas are unseen: designed for and tested with synthesised deltas; live check with Bambu Studio closed if possible.
- Stop may end in a state we don't list (e.g. a firmware going straight to another value): then the result says "not confirmed", never a false failure.
- Thread handoff: covered by tests that deliver from another thread.
