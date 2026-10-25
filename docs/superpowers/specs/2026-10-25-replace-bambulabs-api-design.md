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

- MQTT 3.1.1, paho `CallbackAPIVersion.VERSION2`, unique client id `bamboozle-<process id>-<random token>-<n>` (fresh per instance, and distinct between two running instances, so they don't kick each other off the broker). No serial or access code in the id.
- **No paho auto-reconnect** (`reconnect_on_failure=False`). paho re-sends every QoS 1 message still waiting for a PUBACK on each reconnect (`_messages_reconnect_reset_out`), and the Bambu broker loses PUBACKs, so a stop from minutes ago could be replayed onto the next print. Instead a dropped session ends the client: `on_disconnect` makes the connection retire it and build a fresh one (empty queue), and our own reconnect (TCP probe, backoff 3 -> 60 s) takes over. paho still retries the *first* connection (nothing is queued before the first CONNACK, as `publish` refuses while down).
- User `bblp`, password = access code (never logged).
- TLS via `tls_set_context`: `ssl.SSLContext(PROTOCOL_TLS_CLIENT)`, `check_hostname=False`, `CERT_NONE` (printers use self-signed certs; identity is checked by the cert-CN rule on relocation, unchanged), `minimum_version = TLSv1_2`. This context loads no CA store, so building a client is cheap.
- keepalive 30 s, `reconnect_delay_set(1, 30)` (first connection only), `max_inflight_messages_set(1000)` (Bambu PUBACK race, existing `INFLIGHT_WINDOW`), `suppress_exceptions = True` (a callback bug must not kill the network thread).
- `start()`: `connect_async` + `loop_start` (never blocks on the network).
- `on_connect`: reports the CONNACK reason code to the owner; on success subscribes to `device/<serial>/report`.
- `on_subscribe` (success = no reason code with `is_failure`): publishes a standalone `pushall` (A1/P1S ignore combined requests). A refused subscription is logged; the session then gets no reports and the staleness rules (`no_reports`) apply. `is_connected()` is True from the CONNACK, slightly before the SUBACK, so a command can go out before the subscription exists; the waits below cover it.
- **pushall until a full report** (`PrinterConnection`): if no full report (`msg` 0) has arrived on the session, the poll loop repeats the standalone pushall 10 s after the last one, doubling up to 5 min. H2C/P2S send full reports anyway; this covers a lost or ignored on-connect pushall on the A1/P1S, which otherwise would show a state built from deltas only (the library's 60 s pushall used to paper over that).
- `on_message`: payload over 1 MiB is dropped; bytes decoded UTF-8 with `errors="replace"`; `json.loads` (any exception, including `RecursionError`, drops the message); anything but a JSON object is dropped (logged at debug). The dict goes to the owner's `on_report` callback, on the paho thread, wrapped so no exception escapes.
- `on_disconnect`: tells the owner (state recompute only).
- `publish(payload) -> bool` (module function `mqtt.publish(client, payload)` kept, as today): QoS 1 to `device/<serial>/request`; refuses (False) while not connected so paho can't replay it later; never waits.
- `mqtt.retire(client)`: the existing non-blocking teardown (callbacks cleared inline, now including `on_subscribe`, then disconnect -> loop_stop -> disconnect on a daemon thread, watched for 60 s). Clearing a callback does not wait for one already running, so the connection also tags every report with the client it came from and drops reports from a client that is no longer current. `wait_for_teardowns` unchanged.
- `sequence_id`: one process-wide counter, started at a random offset (100000-899999) so our ids don't collide with Bambu Studio's (~2000s) on the shared report topic.
- Request builders (pure): `pushall_request`, `version_request`, `light_requests(mode)` (unchanged) plus `pause_request`, `resume_request`, `stop_request`, `speed_request(level)`.

### 2.2 `merge.py`: `MergedReport` (pure, no I/O)

Holds the merged `print` dict and the latest `info` (get_version reply). `apply(payload) -> Frame` classifies and merges one message; `Frame` says what it was (`status`, `full`, `version`, `command echo` with `command` / `sequence_id` / `result` / `reason`, or `other`).

Classification:

- `print` with `command` == `push_status`, or with no `command`: a **status frame**. Full when `msg` is 0 or "0", otherwise (including a missing `msg`) a delta.
- `print` with `command` == `project_file`: its `url` and `subtask_name` (strings) are merged into the state: `protocol.file_on_internal_storage` compares them, and an A1/P1S delta at print start need not repeat `subtask_name` (the library merged the whole echo). Nothing else: its `job_id` is an int where status has a string, `param` / `sequence_id` aren't status, and it isn't telemetry.
- any other `print` / `system` command (pause / ledctrl echoes, `sntp`, other clients' commands): not merged; returned as a command echo for verification.
- `info` with `command` == `get_version`: replaces `info` (other clients' replies carry the same data).

Merge rules for a status frame (`merge(dst, src, full)`):

- dicts merge recursively, key by key; a key absent from the frame keeps its value (so `url` and other keys only some frames carry survive full reports);
- a scalar or `null` replaces;
- lists: in a **full** report, a list replaces the old one (the full report is authoritative: removed AMS units, emptied HMS). In a **delta**, a list whose items are all dicts with an `id` is merged item by item on `id` (AMS units `ams.ams`, trays `tray`, `vir_slot`, `device.extruder.info`, `device.nozzle.info`), one whose items all have a `node` is merged on `node` (`lights_report`), and any other list replaces (`hms`, `net.info`, …). Items not in the delta are kept;
- trays in a delta: one carrying `tray_type` is a complete tray and **replaces** the old one (so a non-RFID spool replacing an RFID one doesn't keep the old brand); `{"id"}` alone, or `{"id","state"}` with `state` 10 (the H2C's empty value) or with the slot's merged `tray_exist_bits` bit 0, is an empty slot and replaces it too; anything else (e.g. `{"id","remain"}`, or `{"id","state":11}` on a loaded slot) merges key by key;
- `ams.tray_exist_bits` (hex, like all the bit fields; read with `protocol.hex_or_int`), when present in a frame, clears the filament of every slot of units 0–3 whose bit (`unit*4 + slot`) is 0 (the tray becomes `{"id": slot}`); `ams.ams_exist_bits`, when present, drops units 0–3 whose bit is 0. AMS HT (unit 128+) is left to full reports: its bit layout (`tray_exist_bits` bit 16 on the H2C) isn't confirmed. Bits are applied when they arrive, and a later tray update is trusted: the A1 sends `{"tray_exist_bits":"f"}` seconds before the unit with the new spool;
- `vir_slot` beats `vt_tray`: both are kept as merged; the filament reader uses `vir_slot` whenever it is a non-empty list (already the case), so a single-dict `vt_tray` never overrides multi-slot data;
- recursion is bounded (depth 32; deeper structures replace), and a list merged by id/node keeps at most 256 items.

`reset()` empties it (a new session starts from nothing, as a new library client did).

### 2.3 `report.py` additions (pure)

`job_fields(print)`: `gcode_state` (known values IDLE / PREPARE / RUNNING / PAUSE / FINISH / FAILED; anything else or missing → `UNKNOWN`, exactly as the library's enum did, so the UI sees no new values), `progress` (`mc_percent`), `remaining_minutes` (`mc_remaining_time`), `current_layer` (`layer_num`), `total_layers` (`total_layer_num`), `print_speed` (`spd_mag`), `light_on` (`lights_report` node `chamber_light`, else the first entry, mode `on`), `gcode_file`, `subtask_name`. Numbers accept ints, floats and numeric strings; anything else is 0.

`filaments(print)`: the current raw-dump filament reader moved here unchanged (AMS trays, `vir_slot` else `vt_tray`), minus the library fallback.

`status_fields` is unchanged.

### 2.4 `PrinterConnection`

- `self.client: mqtt.BambuClient` replaces `self.printer`; `self.report = MergedReport()`.
- paho thread: `_on_connack(rc)` (same flags logic as today), `_on_report(payload)` stamps `_last_report` / `_reports` / `_probed` / `_down_reason` as today and hands the payload to the loop with `call_soon_threadsafe(self._ingest, payload)` (the loop is captured in `connect()`; a closed loop drops it).
- loop: `_ingest(payload)`: `frame = report.apply(payload)`; resolve command waiters; `on_change()` (a callback the manager sets).
- `read_state()`: builds `PrinterState` from `report` (pure decoders) and assigns it with no `await` in between, so two concurrent reads (a push and a poll step) can't land out of order; then the `get_version` backoff (publish via `to_thread`). Keeps the thumbnail scheduling. The thumbnail task calls `on_change()` when it finishes so the card updates without waiting for the next housekeeping round.
- `reset_session()`: retires the client, builds a new one, `report.reset()`, resets the version backoff (as today).
- Staleness, auth refusal, backoff, grace: unchanged logic, on `client.is_connected()`. A drop (`on_disconnect` of the current client, after it connected) resets the session at once (see §2.1).
- Command waiters: `watch(command) -> Waiter`, registered before publishing. A waiter holds the command's `confirm(print_state, frame)` predicate and the first request's command and `sequence_id` (for the light that's the `chamber_light` request: printers without `chamber_light2` may refuse the second one). It is checked once at registration (frame `None`) and after every ingested frame. An echo with the same command and `sequence_id` (as strings) and `result` "fail"/"failed" is remembered; it only decides the outcome if the state never confirms. The request is always published, even if the state already shows it (the merged state may be stale). If a command isn't confirmed halfway through the wait, one standalone pushall is sent (a query): an A1/P1S may not push the changed field (e.g. `lights_report`) in a delta, and the full report then shows it.

### 2.5 `commands.py` (pure)

`build(action, params) -> Command`: the requests to publish, the confirmation predicate, the label and done-message, and whether it's a print control. Params are untrusted (WS JSON): `set_speed` needs a level 1-4 as an int or a digit string (not a bool or float), `set_light` a mode of `on` / `off`; anything else is rejected before anything is sent.

| action | request(s) | confirmed when |
|---|---|---|
| pause | `{"print":{"command":"pause","param":"","sequence_id":"N"}}` | `gcode_state` == PAUSE |
| resume | `{"print":{"command":"resume","param":"","sequence_id":"N"}}` | `gcode_state` in RUNNING, PREPARE |
| stop | `{"print":{"command":"stop","param":"","sequence_id":"N"}}` | `gcode_state` in FINISH, FAILED, IDLE |
| set_speed L | `{"print":{"command":"print_speed","param":"L","sequence_id":"N"}}` | `spd_lvl` == L |
| light_on / light_off / set_light | `ledctrl` to `chamber_light` and `chamber_light2` (unchanged) | `lights_report` `chamber_light` mode == requested |
| refresh | standalone `pushall` | a full status report arrives |

The payloads are the same for all four models: OpenBambuAPI `mqtt.md` documents `pause` / `resume` / `stop` with an empty `param` and `print_speed` with the level as a decimal string, sent with QoS 1; Bambu Studio's DeviceManager builds the same objects for every model (no model branch), with an incrementing string `sequence_id`. Unit-tested per model; never sent to a real printer in testing.

### 2.6 `PrinterManager`

- Connections are created through one helper that sets `conn.on_change = lambda: self._mark_dirty(pid, conn)`.
- `_mark_dirty`: adds the printer to a dirty set; if no flush is pending, schedules one in `PUSH_DEBOUNCE_SECONDS` (0.3 s). One flush serves every dirty printer (one `update` message, since each makes the dashboard re-render), a burst of reports becomes one update, and a printer pushing every second gets about one update per report.
- flush: takes the dirty set (so changes from then on schedule the next flush), `read_state()` for each printer still current, then `_broadcast_changed(pids)`. Pending flushes are cancelled at shutdown.
- `_broadcast_changed(pids)`: under one lock (so two broadcasts can't reach a client out of order), dumps each state, drops `timestamp`, and sends `{"type":"update","printers":{...}}` with only the printers whose dump differs from the last one sent (dedupe). Removed printers are forgotten.
- Each WS send gets 2 s; a client that can't take it is dropped (a frozen tab must not stall every printer's updates; it reconnects and gets `init`).
- `_poll_loop` keeps its shape (concurrent per-printer steps, 2 s step timeout, a stuck step isn't restarted) but its steps only do housekeeping (silence probe, pushall until a full report, stale/reset, reconnect) + a state recompute. After a round it broadcasts the printers that changed (connect / offline / stale / camera / thumbnail changes that no report triggers), and every `HEARTBEAT_SECONDS` (30) all printers. The heartbeat only re-sends our state (a dashboard that missed an update catches up); it can't heal a missed MQTT delta: that's what the pushall retries and the silence probe are for.
- `execute_command(pid, action, params) -> CommandResult(success, message, confirmed)`:
  - unknown printer / not connected / invalid params → `success=False`, `confirmed=None`;
  - publish failed → `success=False`, "Not sent: printer not connected";
  - confirmed within `COMMAND_CONFIRM_SECONDS` (10) -> `success=True, confirmed=True`, e.g. "Print paused"; the printer's card is pushed right away (not after the 0.3 s throttle), so the toast doesn't arrive before the card changes;
  - not confirmed, printer echoed a failure → `success=False`, "Printer refused pause: <reason>";
  - not confirmed otherwise → `success=True, confirmed=False`, "Pause sent, but not confirmed: the printer didn't report the change within 10 s". For print controls on a printer reporting Developer Mode off, it adds "(Developer Mode is off: the printer may ignore print controls)". Never a failure because of Developer Mode.

### 2.7 Routers and frontend

- REST action endpoints return `{"success", "message", "confirmed"}`; the WS `command_result` carries `confirmed` too.
- `ws.py` runs each command in its own task (kept in a set, cancelled when the socket closes, at most 4 at once per socket; more are answered "busy"), so a 10 s confirmation wait doesn't stop the socket from receiving the next message. It also rejects a handshake whose `Origin` is present and doesn't match its `Host` (`Origin: null` included; see §4).
- `main.py` adds Starlette's `TrustedHostMiddleware` with `127.0.0.1`, `localhost` (not `[::1]`: Starlette splits the Host on the first colon, and we only listen on IPv4): the server only listens on 127.0.0.1, so any other `Host` is a DNS-rebinding page (§4).
- `POST /api/printers/test`: TCP pre-probe as today, then a throwaway `BambuClient` + `MergedReport` (merged on the event loop via `call_soon_threadsafe`); waits for the CONNACK (10 s), the first report (5 s) and a `gcode_state` (5 s more; the client's on-connect pushall asks for it); same messages as today; always retired.
- `app.js`: a command result with `success` and `confirmed === false` shows an `info` toast instead of `success` (WS and REST fallback alike).

## 3. Testing

- `merge.py` unit tests per rule (dict merge, list replace in full, id / node merge in delta, empty-slot tray, `tray_exist_bits` / `ams_exist_bits`, `vir_slot` kept, project_file url only, echoes not merged, depth bound).
- Fixture replays (`tests/report_fixtures.py`), all four printers: every message in order through `MergedReport`, then the decoders:
  - P1S: once the first full report has been seen, the 4 AMS trays (and the AMS humidity) are present after every message;
  - A1: trays follow `tray_exist_bits` and the later unit push; never blank after the first full report; the print start (FINISH → PREPARE → RUNNING) shows through;
  - at every full report, the state decoded from the merge equals the state decoded from that full report alone (weak: holds largely by construction);
  - stronger: on the A1/P1S, the state merged from deltas *just before* each full report equals that full report's, except for fields that drift between the two (nozzle / bed temperature, Wi-Fi);
  - H2C / P2S have no deltas in the fixtures, so deltas are synthesised (they encode the merge's own idea of a delta, so they test consistency, not the real shape) from consecutive full reports (only the keys that changed, `msg` 1), replayed after the first full report, and must decode to the same state as the full reports.
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
- **Cross-site WebSocket hijacking (S/T/E):** a malicious page opens `ws://127.0.0.1:8080/ws`, reads the printer ids from `init` and sends `stop`. Pre-existing. Mitigation: reject the WS handshake when an `Origin` header is present and its host:port differs from `Host`. First abuse-case test.
- **DNS rebinding (S/T/E/I):** a page on a domain that resolves to 127.0.0.1 is same-origin with us: Origin == Host, so the check above passes; it could read `/api/printers`, POST `/printers/{id}/stop` (no body, no preflight), or PUT a printer's `ip` to an attacker's host and harvest the access code from our MQTT login. Pre-existing. Mitigation: `TrustedHostMiddleware` (Host must be 127.0.0.1 or localhost) on HTTP and WS. Test.
- **Cross-site POST (T):** a page can POST `/api/printers/{id}/stop` without a preflight, but it needs the random 12-hex printer id, which it can't read cross-origin (no CORS) once the WS leak and rebinding are closed. Accepted.
- **Untrusted command params (T):** WS `params` interpolated into MQTT payloads (`speed_lvl` went into `param` unchecked). Mitigation: strict validation in `commands.build` (int 1–4, on/off). Test.
- **Malformed / hostile reports (D/T):** non-UTF-8, non-JSON, huge, deeply nested payloads from the LAN. Mitigation: size cap 1 MiB, `errors="replace"`, objects only, bounded merge depth, merge/decoders never raise into paho's thread or the loop (exceptions logged). Tests.
- **Replay of stale commands (T):** paho replays unacked QoS 1 messages on every reconnect, and keeps a message queued when the connection drops mid-publish. Mitigation: no paho auto-reconnect; a dropped session's client is retired with its queue (§2.1); `publish` refuses while disconnected. Test.
- **Credential disclosure (I):** access code only in `username_pw_set`; never logged or put in the client id; reports never contain it.
- **Broker session exhaustion (D):** printers allow few clients. One session per printer, clean DISCONNECT on retire (kept).
- **MITM on the LAN (S):** certs unverified (pre-existing, accepted for self-signed printer certs). Out of scope. The relocation rule (cert CN == serial) is a sanity check against announcing the wrong printer, not an identity proof: anyone can mint a self-signed cert with that CN, and the serial is broadcast in SSDP.

## 5. Decisions and why

1. Merge on the event loop, not the paho thread: all state is single-threaded (no locks); `call_soon_threadsafe` keeps arrival order.
2. Full reports deep-merge dicts but replace lists; deltas merge id/node lists. Keeps out-of-band keys (`url`) and still honours removals in full reports.
3. Event-based `tray_exist_bits` clearing (on arrival only) so the A1's bits-then-unit ordering can't blank a fresh spool.
4. AMS HT untouched by exist bits (layout unconfirmed); full reports refresh it.
5. pushall on every SUBACK (each session), repeated (10 s doubling to 5 min) until the session has had a full report, the 30 s silence probe (existing), halfway through an unconfirmed command, and Refresh. No timer once a full report has arrived. No extra rate limit: our reconnect backoff (3 -> 60 s) bounds sessions, and none of the user's printers is a P1P.
6. 0.3 s throttle (not a reset-on-every-message debounce) so a printer pushing every second can't starve its own updates; one flush for all dirty printers.
7. Dedupe on the whole state dump minus `timestamp`: anything visible changing is broadcast; nothing else is.
8. Heartbeat every 30 s with all printers, so a dashboard that missed an update (or a missed recompute) catches up.
9. Commands wait for confirmation before answering (one toast per click, up to 10 s). The card itself updates as soon as the state changes.
10. A refused echo only counts if the state never confirms (the light works on H2C/P2S despite Developer Mode off; an echo must not overrule the state).
11. A command already in the requested state counts as confirmed (pressing pause on a paused printer is a no-op).
12. `rtsp_url` IPs are not used for anything (they differ from the configured IP).
13. The merged state is reset with each new client (each session), matching what a new library client did.
14. No "ready" flag (research §1.4): before the first full report the card is decoded from whatever has arrived, as it was with the library. The window is about a second (the on-connect pushall reply), and the pushall retries bound it.
15. No paho auto-reconnect (§2.1): replay safety beats the ~1 s faster reconnect.

## 6. Risks

- H2C/P2S deltas are unseen: designed for and tested with synthesised deltas; live check with Bambu Studio closed if possible.
- Stop may end in a state we don't list (e.g. a firmware going straight to another value): then the result says "not confirmed", never a false failure.
- Thread handoff: covered by tests that deliver from another thread.

## 7. Spec review (2026-10-25)

Two fresh-context opus reviewers (the second standing in for the ChatGPT/Loom channel this project lacks). Accepted and folded in above: paho-mqtt in requirements; QoS 1 replay on paho's auto-reconnect (both: blocker) -> no auto-reconnect, fresh client per session; pushall until a full report; project_file `subtask_name`; light refusal only from the primary request, echo matched on command + id, random counter offset; always publish; tray replace/empty rules narrowed (full-shape replaces, empty only `{id}` / state 10 / bit 0); read_state assigns without an await; batched flush, broadcast lock, send timeout; DNS rebinding -> TrustedHostMiddleware; ws task set/cancel/cap; `suppress_exceptions`, catch-all decode; retire clears `on_subscribe` and reports are tagged by client; stronger pre-full-report fixture check; heartbeat wording; CN check wording; push the card at once on a confirmed command; CLAUDE.md to be updated.

Rejected: a cap on merged key count (printer payloads are bounded and list growth is capped); per-printer flushes (batched instead); accepting a success echo as confirmation of the light (the mid-wait pushall shows `lights_report` instead, and the echo says nothing about the state); restoring the library's default `print_speed` of 100 before any report (only visible on a card that has no data yet).
