# BamBuddy research: what Bamboozle could adopt

- **Project:** BamBuddy, <https://github.com/maziggy/bambuddy> (confirmed: "self-hosted print archive and management system for Bambu Lab 3D printers", by maziggy).
- **Commit studied:** `98ecaf5020e3a20a5de49859b8ad5a754d5ad85c` (2026-10-22). All links below are pinned to it.
- **Size:** about 182k lines of Python backend plus a React frontend. Printer protocol code is mostly in `backend/app/services/bambu_mqtt.py` (8.5k lines), `camera.py`, `bambu_ftp.py` and `discovery.py`.
- **Licence:** **AGPL-3.0** (see [§7](#7-licence-verdict)). Bamboozle is MIT. **Reimplement the ideas. Do not copy the code.**
- **Method:** shallow clone, read only (nothing executed). I also read the installed `bambulabs_api` 2.6.6 (`site-packages/bambulabs_api/mqtt_client.py`, `client.py`) to check what our library actually does on the wire.

---

## 0. TL;DR (prioritised)

| # | Item | Group | Effort | Value |
|---|------|-------|--------|-------|
| 1 | Detect Developer Mode / "command verification failed" (`fun` bit + HMS `0500050000010007`) and say so in the UI | A | S | High: otherwise pause/stop/light silently do nothing |
| 2 | Fix MQTT teardown: `mqtt_stop()` only does `loop_stop()` (no `disconnect()`) and can block | A | S | High: hangs and leaked sessions on edit/relocate |
| 3 | Staleness by last-message time (60 s) plus a "zero reports since connect → check serial" hint | A | S | High: catches the P1-series "connected but silent" broker bug |
| 4 | Fix camera backoff never resetting after a good session; fast respawn | B | S | High: X1/P2 RTSP sessions drop routinely, so our gaps grow to 60 s |
| 5 | P2S ffmpeg profile (`-use_wallclock_as_timestamps 1`, bigger probe) and low-latency flags for others | B | S | High for the user's P2S |
| 6 | Show real targets and chamber temp, incl. H2 `(target<<16)\|current` encoding; second nozzle on H2C | C | S | High: the chamber tile is always `--` today |
| 7 | HMS / `print_error` decoding with readable text | C | S–M | High |
| 8 | Stage names (`stg_cur`) with the A1/P1 idle bug handled | C | S | Medium |
| 9 | Report refused CONNACK (rc 134/135) as "access code wrong / changed" | A | S | Medium |
| 10 | **Replace `bambulabs_api` with a thin in-house paho client** (event-driven, deep-merge, QoS 1) | A | M | High: enables 11–14 and removes our library workarounds |
| 11 | Stop the library's implicit pushall every 60 s; pushall on connect and on demand only | A | (in 10) | Medium: P1 lag risk |
| 12 | Deep-merge partial pushes (AMS per unit/tray, `tray_exist_bits`, `vir_slot` precedence) | A | (in 10) | Medium: flicker and lost trays |
| 13 | Push WS updates on MQTT message (debounced, deduped), not a 3 s poll | A | (in 10) | Medium |
| 14 | QoS 1 for commands; verify the command took effect by watching the resulting state | A | (in 10) | Medium |
| 15 | Thumbnail lookup: `subtask_name` variants across `/`, `/cache`, `/model`, `/data`; detect internal-eMMC prints; P2S/H2C FTPS TLS 1.2 cap | D | M | Medium-High for H2C/P2S |
| 16 | Fans, AMS humidity/temperature, Wi-Fi, door, SD, nozzle, firmware, `spd_lvl` names | C | S | Medium |
| 17 | Frozen-frame watchdog for RTSP | B | S | Medium |
| 18 | Force-refresh button, chamber light on both LED nodes | A/D | S | Low-Medium |
| 19 | Notifications (ntfy/Discord/Telegram) on finish, fail, HMS, offline | D | M | Medium |
| 20 | Connection diagnostic panel | D | M | Medium |
| 21 | Print history | D | M | Medium |
| 22 | Local TLS proxy in front of ffmpeg (only if P2S drops persist) | B | M | Conditional |
| 23 | Upload and start print, SD browser, timelapse download | D | L | Low-Medium |

Where Bamboozle is already **ahead** of BamBuddy: SSDP-driven IP auto-relocation with cert CN == serial verification, and the TCP pre-probe before MQTT connect. BamBuddy has discovery (for adding printers) but no relocation and no cert identity check.

---

## 1. Printer communication (group A)

### 1.1 How BamBuddy talks to printers

- **Raw paho-mqtt, no wrapper library.** It uses `paho-mqtt>=2.0.0` directly ([requirements.txt](https://github.com/maziggy/bambuddy/blob/98ecaf5020e3a20a5de49859b8ad5a754d5ad85c/requirements.txt)) in `BambuMQTTClient` ([bambu_mqtt.py#L1201](https://github.com/maziggy/bambuddy/blob/98ecaf5020e3a20a5de49859b8ad5a754d5ad85c/backend/app/services/bambu_mqtt.py#L1201)). There is one client per printer, using paho's own network thread (`loop_start`). Callbacks fire `on_state_change` into the asyncio loop.
- **Connect parameters** ([#L5837-L5888](https://github.com/maziggy/bambuddy/blob/98ecaf5020e3a20a5de49859b8ad5a754d5ad85c/backend/app/services/bambu_mqtt.py#L5837)):
  - MQTT 3.1.1, user `bblp`, password = access code.
  - Unique client id per instance: `bambuddy_<serial>_<pid>_<n>`.
  - TLS with `CERT_NONE` and `minimum_version = TLSv1_2`.
  - `reconnect_delay_set(1, 30)` and `keepalive=30` (the broker drops a dead client at 45 s).
  - `max_inflight_messages_set(1000)`. Their reason: the Bambu broker's PUBACK matching races paho's QoS 1 in-flight tracking, and the default of 20 wedges a session after about 16–20 commands.
- **Always QoS 1 on publish.** The file header says the printer ignores QoS 0 while busy broadcasting. They saw K-profile requests take 20–30 s with QoS 0 and answer instantly with QoS 1 ([#L1-L8](https://github.com/maziggy/bambuddy/blob/98ecaf5020e3a20a5de49859b8ad5a754d5ad85c/backend/app/services/bambu_mqtt.py#L1)). `bambulabs_api` publishes with QoS 0 (`mqtt_client.py` `__publish_command`).
- **On connect** ([`_on_connect` #L1737](https://github.com/maziggy/bambuddy/blob/98ecaf5020e3a20a5de49859b8ad5a754d5ad85c/backend/app/services/bambu_mqtt.py#L1737)):
  - Subscribe to `device/<serial>/report`.
  - Optionally subscribe to `device/<serial>/request`. Some P1S/A1 brokers kill the TCP session for this, so they detect that and cache "unsupported" after 2 strikes.
  - Then send `pushall`, `get_version`, and a priming `extrusion_cali_get`.
- **pushall policy: event-driven, not periodic.** It is sent on connect, on the user's "Force Refresh", and once when a print shows layers but no `total_layer_num` ([#L4137-L4183](https://github.com/maziggy/bambuddy/blob/98ecaf5020e3a20a5de49859b8ad5a754d5ad85c/backend/app/services/bambu_mqtt.py#L4137)). It is never sent on a timer.
- **Incremental merge** ([`_process_message` #L2187](https://github.com/maziggy/bambuddy/blob/98ecaf5020e3a20a5de49859b8ad5a754d5ad85c/backend/app/services/bambu_mqtt.py#L2187), [`_update_state` #L4064](https://github.com/maziggy/bambuddy/blob/98ecaf5020e3a20a5de49859b8ad5a754d5ad85c/backend/app/services/bambu_mqtt.py#L4064)):
  - Each typed field is updated only `if key in data`, which merges naturally.
  - AMS is merged **per unit and per tray**, keeping fields a partial omits. Empty slots are cleared through `tray_exist_bits` ([`_handle_ams_data` #L3044](https://github.com/maziggy/bambuddy/blob/98ecaf5020e3a20a5de49859b8ad5a754d5ad85c/backend/app/services/bambu_mqtt.py#L3044); merge at about #L3480-L3534).
  - `vir_slot` (H2 external spools) takes precedence over `vt_tray`. A single-dict `vt_tray` is never allowed to overwrite multi-slot `vir_slot` data ([#L2322-L2349](https://github.com/maziggy/bambuddy/blob/98ecaf5020e3a20a5de49859b8ad5a754d5ad85c/backend/app/services/bambu_mqtt.py#L2322)).
  - Command echoes are not ingested as telemetry. Only frames with `command` absent or `push_status` count ([`is_printer_status_frame` #L89](https://github.com/maziggy/bambuddy/blob/98ecaf5020e3a20a5de49859b8ad5a754d5ad85c/backend/app/services/bambu_mqtt.py#L89)).
- **Sequence ids and acks:**
  - A per-client counter feeds `sequence_id`.
  - For commands that answer (K-profile writes, RFID re-read, the dev-mode probe), they register a pending slot keyed by `sequence_id` *before* publishing. They then poll for the echoed response for up to 6 s ([`await_cali_ack` #L6895](https://github.com/maziggy/bambuddy/blob/98ecaf5020e3a20a5de49859b8ad5a754d5ad85c/backend/app/services/bambu_mqtt.py#L6895)). A timeout is treated as "no evidence", not failure.
  - **pause / resume / stop / speed / light are fire-and-forget** with `"sequence_id": "0"` ([#L7167](https://github.com/maziggy/bambuddy/blob/98ecaf5020e3a20a5de49859b8ad5a754d5ad85c/backend/app/services/bambu_mqtt.py#L7167), [#L6267](https://github.com/maziggy/bambuddy/blob/98ecaf5020e3a20a5de49859b8ad5a754d5ad85c/backend/app/services/bambu_mqtt.py#L6267)). They do not wait for acks there either.
- **Stale and offline detection, in layers:**
  1. `STALE_TIMEOUT = 60 s` without any inbound message triggers a forced reconnect, with a 30 s cooldown ([`check_staleness` #L1599](https://github.com/maziggy/bambuddy/blob/98ecaf5020e3a20a5de49859b8ad5a754d5ad85c/backend/app/services/bambu_mqtt.py#L1599)). Their note: it covers "the P1S/P1P firmware bug where the broker stops publishing but the TCP connection stays alive".
  2. Hard reset rebuilds the paho client with a **fresh client id**, so the broker drops the old session and paho's local QoS 1 queue (an unacked `project_file` could otherwise replay) ([`_hard_reset_client` #L1697](https://github.com/maziggy/bambuddy/blob/98ecaf5020e3a20a5de49859b8ad5a754d5ad85c/backend/app/services/bambu_mqtt.py#L1697)).
  3. A **connection watchdog** runs every 60 s. If a printer has been silent for over 300 s *and* port 8883 still answers, it rebuilds the client. If the port is closed it leaves reconnection to paho ([main.py `_recover_dead_printer_sessions` #L9289](https://github.com/maziggy/bambuddy/blob/98ecaf5020e3a20a5de49859b8ad5a754d5ad85c/backend/app/main.py#L9289)). Their motivating case: a P1S stuck offline for 9 h after a keepalive drop.
  4. Spurious paho disconnect callbacks are ignored if a message arrived in the last 10 s and the rc is not a failure ([`_on_disconnect` #L1863](https://github.com/maziggy/bambuddy/blob/98ecaf5020e3a20a5de49859b8ad5a754d5ad85c/backend/app/services/bambu_mqtt.py#L1863)).
  5. A "zero reports since connect" counter. After 60 s of silence on a fresh session they log that the serial is probably wrong or mis-cased, since the topic is case-sensitive ([#L1599-L1648](https://github.com/maziggy/bambuddy/blob/98ecaf5020e3a20a5de49859b8ad5a754d5ad85c/backend/app/services/bambu_mqtt.py#L1599)).
- **Teardown that never blocks** ([utils/paho_teardown.py `retire_paho_client`](https://github.com/maziggy/bambuddy/blob/98ecaf5020e3a20a5de49859b8ad5a754d5ad85c/backend/app/utils/paho_teardown.py#L36)):
  - Clear the callbacks inline.
  - Then call `disconnect()` and `loop_stop()` on a separate thread.
  - Reason: `loop_stop()` joins the network thread, which can sit in a TLS handshake for up to the keepalive or longer. In their case that froze the whole event loop (#3068).
- **CONNACK refusal reporting** ([`_record_connect_refusal` #L1801](https://github.com/maziggy/bambuddy/blob/98ecaf5020e3a20a5de49859b8ad5a754d5ad85c/backend/app/services/bambu_mqtt.py#L1801)). rc 134/135 (MQTT 3.1.1 codes 4/5) is reported as "access code or serial wrong; the access code changes every time LAN Only or Developer Mode is toggled". Otherwise paho only logs "Unspecified error".
- **Non-UTF-8 payloads** (A1 mini firmware) are decoded with `errors="replace"` instead of crashing ([`_on_message` #L1947](https://github.com/maziggy/bambuddy/blob/98ecaf5020e3a20a5de49859b8ad5a754d5ad85c/backend/app/services/bambu_mqtt.py#L1947)).
- **Firmware authorization / Developer Mode** (the 2025 auth firmware):
  - `fun` field: dev mode is ON when `(fun & 0x20000000) == 0` ([#L2235-L2244](https://github.com/maziggy/bambuddy/blob/98ecaf5020e3a20a5de49859b8ad5a754d5ad85c/backend/app/services/bambu_mqtt.py#L2235), [#L5298](https://github.com/maziggy/bambuddy/blob/98ecaf5020e3a20a5de49859b8ad5a754d5ad85c/backend/app/services/bambu_mqtt.py#L5298)). A1/P1 never send `fun`.
  - HMS `0500050000010007` = "MQTT command verification failed" ([#L757](https://github.com/maziggy/bambuddy/blob/98ecaf5020e3a20a5de49859b8ad5a754d5ad85c/backend/app/services/bambu_mqtt.py#L757)). With it present, queries still work but every control command is silently dropped. The printer looks healthy while pause/print do nothing. They latch `developer_mode=False` from it and un-latch when it clears ([`_apply_mqtt_verify_state` #L5687](https://github.com/maziggy/bambuddy/blob/98ecaf5020e3a20a5de49859b8ad5a754d5ad85c/backend/app/services/bambu_mqtt.py#L5687)).
  - For printers without `fun`, they *probe* by re-sending the external slot's current `ams_filament_setting` and reading `result` / `reason` ([`_probe_developer_mode` #L5640](https://github.com/maziggy/bambuddy/blob/98ecaf5020e3a20a5de49859b8ad5a754d5ad85c/backend/app/services/bambu_mqtt.py#L5640)). They note the probe destabilised some brokers and is inconclusive on P1S 01.10. See §6: not recommended for us.
- **Model specifics worth knowing:**
  - H2 dual nozzle is detected when `device.extruder.info` has 2 or more entries.
  - On H2D, `nozzle_temper` mirrors one nozzle; real values come from `device.extruder.info[i].temp`.
  - `get_accessories` returns stale nozzle data on H2D, so it is not used.
  - P2S `project_file` must send `vibration_cali=false`.
  - A2L AMS-Lite reports unit id 16.

### 1.2 How Bamboozle does it today (and the concrete gaps)

What our `bambulabs_api` 2.6.6 actually does (read from site-packages):

| Aspect | `bambulabs_api` / Bamboozle | BamBuddy |
|---|---|---|
| Keepalive | 60 s (`timeout` param → `connect_async`) | 30 s |
| Reconnect delay | paho default 1–120 s | 1–30 s |
| Publish QoS | 0, then `wait_for_publish()` with no timeout | 1 |
| Merge | `self._data[k] \|= v`: **one level only**. A partial `print.ams` or `print.device` *replaces* the whole sub-dict | Typed per-field merge; per-unit/per-tray AMS merge |
| pushall | On connect, **plus every 60 s whenever any getter is called** (`_update()` in `__get_print`). Our 3 s poll calls a dozen getters, so we send pushall about every 60 s per printer | On connect and on demand only |
| Stop | `mqtt_stop()` → `loop_stop()` only. **No `disconnect()`**. Joins the network thread | Callbacks cleared, then disconnect + loop_stop on a detached thread |
| Bad payloads | `json.loads(msg.payload)` raises on the paho thread | Tolerant decode |
| Light | `{"system":{"led_mode":"on"}}` (no `led_node`), hence our hand-rolled `_set_light` | `ledctrl` to `chamber_light` **and** `chamber_light2` |
| No data yet | Logs "Printer Values Not Available Yet" on every getter | n/a |
| Stage | `PrintStatus(stg_cur)` enum: A1/P1 report `stg_cur=0` ("printing") while idle | Stage table plus an idle-bug guard |

Bamboozle-side observations:

- We already bypass the library for filaments (`_read_filaments_from_dump` reads the raw `mqtt_dump()`) and for the light (reaching into `printer.mqtt_client._client`). The library is now mostly a getter facade over a dict.
- `PrinterConnection.disconnect()` and `reset_session()` call `printer.mqtt_stop` in `to_thread`. That runs `loop_stop()` without `disconnect()`, so:
  - (a) the old TCP/TLS session is not closed cleanly, and Bambu brokers have a small client limit;
  - (b) the join can wait out a handshake, which delays `update_printer` and with it `handle_announcement` relocation and the HTTP response for a settings edit.
- `is_offline()` relies on `mqtt_client.is_connected()` plus a 20 s grace period. It cannot see the "connected but silent" P1 broker case, because the socket stays up.
- The OpenBambuAPI protocol notes (community docs) say the P1 series sends only deltas, and advise against sending `pushall` more often than about every 5 minutes on P1P because it can cause lag. That is a direct conflict with the library's 60 s implicit pushall. Verify against <https://github.com/Doridian/OpenBambuAPI/blob/main/mqtt.md>.

### 1.3 Recommendations (A)

**A1. Developer Mode / command-refused detection.** S. *Do first.*
- **What:** Read `print.fun` (dev mode ON iff `fun & 0x20000000 == 0`) and scan `print.hms[]` for full code `0500050000010007` (attr `0x05000500`, code `0x00010007`). Expose `developer_mode: bool | None` and `commands_blocked: bool` on `PrinterState`. Show a badge: "Printer is refusing control commands: enable LAN Only + Developer Mode on the printer, then restart it".
- **Value:** pause/stop/light/speed currently fail silently on non-dev-mode firmware. The API returns `True` (published, not accepted).
- **BamBuddy:** [bambu_mqtt.py#L757](https://github.com/maziggy/bambuddy/blob/98ecaf5020e3a20a5de49859b8ad5a754d5ad85c/backend/app/services/bambu_mqtt.py#L757), [#L2235](https://github.com/maziggy/bambuddy/blob/98ecaf5020e3a20a5de49859b8ad5a754d5ad85c/backend/app/services/bambu_mqtt.py#L2235), [#L5687](https://github.com/maziggy/bambuddy/blob/98ecaf5020e3a20a5de49859b8ad5a754d5ad85c/backend/app/services/bambu_mqtt.py#L5687).
- **Bamboozle:** `printer_manager.py` `_read_sync` (read from `mqtt_dump()["print"]`), `models.py` `PrinterState`, `static/app.js`.
- **Risks:** `fun` is absent on A1/P1, so show "unknown" rather than guessing. Do **not** copy the write-probe (§6).

**A2. Non-blocking, clean MQTT teardown.** S.
- **What:** In `disconnect()` / `reset_session()`:
  - clear the paho callbacks;
  - call `printer.mqtt_client._client.disconnect()`;
  - then run `loop_stop()` on a daemon thread (fire and forget) instead of awaiting it.
- **Value:** no stalled edits or relocations, and no lingering broker sessions.
- **BamBuddy:** [utils/paho_teardown.py](https://github.com/maziggy/bambuddy/blob/98ecaf5020e3a20a5de49859b8ad5a754d5ad85c/backend/app/utils/paho_teardown.py#L36), [`disconnect` #L6494](https://github.com/maziggy/bambuddy/blob/98ecaf5020e3a20a5de49859b8ad5a754d5ad85c/backend/app/services/bambu_mqtt.py#L6494).
- **Bamboozle:** `PrinterConnection.disconnect`, `reset_session`.
- **Risks:** a zombie client reconnecting behind our back if its callbacks aren't cleared. Clearing them first is the point.

**A3. Staleness by last inbound message.** S.
- **What:** Hook `printer.mqtt_client.on_message_handler` (the library exposes it) to stamp `last_message_monotonic` and count reports since connect.
  - In `is_offline()`, treat "no message for 60 s" as down, even if `is_connected()` is true.
  - Log the "zero reports, check serial case" hint once.
- **Value:** recovers the P1-series silent broker. Gives an honest "last update Ns ago" in the UI.
- **BamBuddy:** [`check_staleness` #L1599](https://github.com/maziggy/bambuddy/blob/98ecaf5020e3a20a5de49859b8ad5a754d5ad85c/backend/app/services/bambu_mqtt.py#L1599), [watchdog main.py#L9289](https://github.com/maziggy/bambuddy/blob/98ecaf5020e3a20a5de49859b8ad5a754d5ad85c/backend/app/main.py#L9289).
- **Bamboozle:** `PrinterConnection.is_offline`, `_poll_loop`.
- **Risks:** an idle X1/H2 still pushes about every 1 s and P1/A1 push deltas often, but confirm that an idle A1 never goes 60 s without a message. If it does, send one pushall after 60 s of silence before declaring it stale.

**A4. Report refused credentials.** S.
- **What:** Use `printer.mqtt_client.on_connect_handler`. On rc 134/135 set `last_error="auth_rejected"` and show "Access code or serial rejected. The code changes whenever LAN/Developer mode is toggled".
- **BamBuddy:** [#L1801](https://github.com/maziggy/bambuddy/blob/98ecaf5020e3a20a5de49859b8ad5a754d5ad85c/backend/app/services/bambu_mqtt.py#L1801).
- **Bamboozle:** `printer_manager.py`, plus the Settings test endpoint in `routers/api.py` (`POST /api/printers/test`), which today can only say "timeout".
- **Risks:** none.

**A5. Replace `bambulabs_api` with an in-house paho client.** M. *See the decision in §1.4.*
This subsumes the items below:
- **A5a. pushall policy:** on connect, on a "Refresh" button, and after 60 s of silence. Never on a timer.
- **A5b. Deep merge:**
  - recursive dict merge for `print`;
  - AMS merged by unit `id` and then tray `id`, keeping non-empty fields;
  - honour `tray_exist_bits` to clear removed spools;
  - `vir_slot` beats `vt_tray`;
  - ignore frames whose `command` is not `push_status`.
- **A5c. Event-driven UI:** `on_message`, then `loop.call_soon_threadsafe`, then a per-printer debounce of about 300 ms, then a WS broadcast of only the changed printer. Dedupe on a key of the visible fields (BamBuddy's `status_key`, [main.py ~#L1700](https://github.com/maziggy/bambuddy/blob/98ecaf5020e3a20a5de49859b8ad5a754d5ad85c/backend/app/main.py#L1556)). Keep a slow (30 s) heartbeat broadcast for staleness display.
- **A5d. Commands:** QoS 1, `sequence_id` string, `max_inflight_messages_set(1000)`, keepalive 30, `reconnect_delay_set(1,30)`, TLS ≥1.2. Verify a command by watching for the resulting state (`gcode_state` → `PAUSE` / `RUNNING` / `FINISH`, `lights_report`, `spd_lvl`) within about 5–10 s. Report "sent but printer didn't change state" (likely A1) instead of a blind success.
- **A5e.** Tolerant UTF-8 decode, and a fresh client id on hard reconnect.
- **Bamboozle:**
  - new `bamboozle/mqtt.py` of about 300–500 lines;
  - `PrinterConnection` stops calling getters;
  - `_read_sync` becomes a pure function `state_from_report(dict) -> PrinterState` (unit-testable with captured payloads);
  - `_poll_loop` shrinks to staleness and reconnect only.
- **Risks:**
  - We own protocol quirks the library papered over (few, in practice).
  - Thread-to-asyncio handoff bugs.
  - Must keep `POST /api/printers/test` working.
  - Mitigate by capturing real `report` payloads from all four printers (A1, P1S, H2C, P2S) as fixtures before switching.

**A6. Light on both nodes.** S. Send `ledctrl` for `chamber_light` and `chamber_light2` (H2D/H2C have two). Use QoS 1 and an incrementing `sequence_id`. [#L7409](https://github.com/maziggy/bambuddy/blob/98ecaf5020e3a20a5de49859b8ad5a754d5ad85c/backend/app/services/bambu_mqtt.py#L7409). Bamboozle: `_set_light`.

**A7. Force refresh.** S. Add a "Refresh" action that publishes `{"pushing":{"command":"pushall","sequence_id":"0"}}`. [#L5634](https://github.com/maziggy/bambuddy/blob/98ecaf5020e3a20a5de49859b8ad5a754d5ad85c/backend/app/services/bambu_mqtt.py#L5634). Bamboozle: `execute_command`, `app.js`.

### 1.4 Decision: drop `bambulabs_api` for raw paho?

**Recommendation: yes, but staged.** Do A1–A4 now, since they work with the library. Then replace the library (A5) as one task with captured-payload tests.

Pros:
- We already read raw `mqtt_dump()` for filaments and publish raw for the light. The library mostly adds a getter facade, two bugs (QoS 0 commands, a light payload without `led_node`), and two behaviours we don't want: the implicit 60 s pushall, and the shallow merge that drops partial AMS/device updates.
- Control over keepalive, reconnect delay, QoS, inflight window, client id, and clean disconnect. Fixes the teardown join and leaked sessions at the root.
- Event-driven push gives updates in under a second instead of polling every 3 s, with less CPU (no to_thread fan-out every 3 s).
- No "Printer Values Not Available Yet" log spam. An honest `ready` flag (first full report seen).
- Direct access to CONNACK rc, `sequence_id` echoes and `fun` / HMS. That is the prerequisite for command verification and auth errors.
- One less dependency. paho-mqtt is already installed as a transitive dependency. Its licence (EPL-2.0 / EDL-1.0 dual) is compatible with MIT distribution.

Cons:
- About 300–500 lines of new code to own: the state mapping for every model, including H2 `device.*` structures.
- Loses the library's helpers we might want later (`start_print_3mf`, gcode, FTP helpers). They are small and easy to reimplement when needed.
- Regression risk on edge payloads the library tolerated. Mitigate with fixture tests from all four printers.
- Must re-implement the Settings "test connection" path.

Rejected alternatives:
- **Fork the library:** it is MIT, but still single-level merge and polling-shaped.
- **Subclass the library:** too much private-name mangling (`__publish_command`, `__get_print`).

---

## 2. Camera (group B)

### 2.1 BamBuddy

- **Model routing:** X1/X2/H2/P2 use RTSPS on 322; A1/P1 use the chamber-image protocol on 6000 ([camera.py `supports_rtsp` #L182](https://github.com/maziggy/bambuddy/blob/98ecaf5020e3a20a5de49859b8ad5a754d5ad85c/backend/app/services/camera.py#L182)). Their chamber-image implementation (80-byte auth, 16-byte header, little-endian size, JPEG markers) is the same protocol as ours ([#L415-L600](https://github.com/maziggy/bambuddy/blob/98ecaf5020e3a20a5de49859b8ad5a754d5ad85c/backend/app/services/camera.py#L415)).
- **Local TLS proxy:** Python/OpenSSL terminates RTSPS, and ffmpeg reads plain `rtsp://127.0.0.1:<port>`, with the request-line URL rewritten so digest auth survives ([`create_tls_proxy` #L240](https://github.com/maziggy/bambuddy/blob/98ecaf5020e3a20a5de49859b8ad5a754d5ad85c/backend/app/services/camera.py#L240)). Reason: GnuTLS-built ffmpeg (Debian) rejects TLS renegotiation that P2S firmware uses, so streams drop after a few seconds.
- **Stream ffmpeg flags** ([routes/camera.py #L589](https://github.com/maziggy/bambuddy/blob/98ecaf5020e3a20a5de49859b8ad5a754d5ad85c/backend/app/api/routes/camera.py#L589)):
  ```
  -rtsp_transport tcp -rtsp_flags prefer_tcp -timeout 30000000 (or -stimeout, probed)
  -buffer_size 1024000 -max_delay 500000 -probesize <p> -analyzeduration <a>
  -fflags nobuffer -flags low_delay [profile extras] -i <url> -f mjpeg -q:v 5 -r <fps> -an -
  ```
  It probes `ffmpeg -h demuxer=rtsp` once to choose `-stimeout` vs `-timeout`, because on transitional ffmpeg builds `-timeout` means listen mode ([camera.py `rtsp_socket_timeout_flag` #L119](https://github.com/maziggy/bambuddy/blob/98ecaf5020e3a20a5de49859b8ad5a754d5ad85c/backend/app/services/camera.py#L119)).
- **Per-model profiles** ([camera_profiles.py](https://github.com/maziggy/bambuddy/blob/98ecaf5020e3a20a5de49859b8ad5a754d5ad85c/backend/app/services/camera_profiles.py#L103)):
  - Default (X1/H2) uses `probesize 32`, `analyzeduration 0` for a fast start.
  - **P2S** uses `probesize 1_000_000`, `analyzeduration 500_000` and `-use_wallclock_as_timestamps 1`. P2S RTP timestamps don't advance, so `-r` CFR conversion drops every frame after the first.
  - Respawn: 0.2 s after a session that delivered frames, doubling to a 5 s cap when sessions fail in a row, up to 30 failures in a row. Their note: "A stock X1C ends every session after about a minute".
- **Frozen-frame watchdog:** byte-identical JPEGs for 20 s restart ffmpeg. A dead upstream can leave ffmpeg repeating its last frame at about 29 fps for hours. Still scenes are re-checked every 300 s ([routes/camera.py#L83](https://github.com/maziggy/bambuddy/blob/98ecaf5020e3a20a5de49859b8ad5a754d5ad85c/backend/app/api/routes/camera.py#L83)).
- **Fan-out:** one upstream per printer feeds N subscriber queues of depth 4 (drop frames for slow viewers), and the upstream is torn down 5 s after the last viewer leaves ([camera_fanout.py](https://github.com/maziggy/bambuddy/blob/98ecaf5020e3a20a5de49859b8ad5a754d5ad85c/backend/app/services/camera_fanout.py#L53)). Most printers allow only one camera connection.
- **Snapshot:** one-shot `-frames:v 1 -f image2pipe -vcodec mjpeg -q:v 2`, coalesced per printer so it doesn't collide with a live stream ([camera.py #L750](https://github.com/maziggy/bambuddy/blob/98ecaf5020e3a20a5de49859b8ad5a754d5ad85c/backend/app/services/camera.py#L750)).
- **No-ffmpeg RTSP option:** none. They require ffmpeg too.

### 2.2 Bamboozle comparison

- We already have a single upstream per printer (`CameraStream.get_frame()` shared by all `/stream` viewers), so fan-out is effectively done. Viewers poll every 100 ms and send only new frames, which is fine.
- **Bug:** in `_capture_loop`, `backoff` starts at 3 s, doubles to 60 s, and **never resets after a successful session**. Every routine X1/P2 session drop raises it. After a few drops, viewers see 30–60 s black gaps, and `_frame` is set to `None` on every drop.
- Our RTSP args lack `-fflags nobuffer -flags low_delay` and a small probe, so ffmpeg's default 5 MB / 5 s probe slows startup. We also lack the P2S wallclock fix: with `-r 10` and P2S timestamps, the stream may freeze after the first frame, depending on firmware.
- `-timeout 10000000` is right for modern ffmpeg (gyan builds are 6/7.x), so no probe is needed on Windows.
- `-tls_verify 0` with rtsps works if the user's ffmpeg build has OpenSSL/schannel. The GnuTLS problem is Debian-specific, so the TLS proxy is low priority on Windows.
- Port-6000 parser: on a bad payload size we `continue` after dropping the header, which desyncs the stream. BamBuddy returns or errors and reconnects, which is safer.

### 2.3 Recommendations (B)

**B1. Reset backoff after a good session, respawn fast.** S.
- Track whether the session delivered at least one frame. If yes, reconnect after about 0.5 s and reset `backoff=3`. Keep the last frame visible during the respawn instead of clearing `_frame`.
- **BamBuddy:** [camera_profiles.py](https://github.com/maziggy/bambuddy/blob/98ecaf5020e3a20a5de49859b8ad5a754d5ad85c/backend/app/services/camera_profiles.py) (`rtsp_reconnect_delay`, `rtsp_reconnect_backoff_max`).
- **Bamboozle:** `camera.py` `_capture_loop`.
- **Risks:** hammering a printer that refuses sessions. Keep exponential backoff for sessions that produced no frames.

**B2. ffmpeg profiles.** S.
- Default: `-probesize 32 -analyzeduration 0 -fflags nobuffer -flags low_delay -max_delay 500000`.
- P2S (model `N7` / camera port 322 + model P2S): `-probesize 1000000 -analyzeduration 500000 -use_wallclock_as_timestamps 1`.
- Needs the model on `PrinterConfig`. Discovery already knows it via `discovery.model_info`, so persist `model` when adding or relocating.
- **Bamboozle:** `camera.py` `_capture_rtsp`, `models.PrinterConfig`, `discovery.py`.
- **Risks:** with `probesize 32`, some firmware fails to lock. Fall back to the relaxed profile after one failed start.

**B3. Frozen-frame watchdog.** S. Hash each frame. If it is identical for 20 s, kill ffmpeg (it respawns via B1). [routes/camera.py#L83](https://github.com/maziggy/bambuddy/blob/98ecaf5020e3a20a5de49859b8ad5a754d5ad85c/backend/app/api/routes/camera.py#L83). Bamboozle: `_capture_rtsp`.

**B4. Port-6000 desync.** S. On an implausible `payload_size`, raise and reconnect instead of `continue`. Bamboozle: `_capture_raw_socket`.

**B5. Local TLS proxy.** M, *conditional*. Only if P2S/H2C RTSP drops persist after B1/B2 on the user's ffmpeg build. [camera.py#L240](https://github.com/maziggy/bambuddy/blob/98ecaf5020e3a20a5de49859b8ad5a754d5ad85c/backend/app/services/camera.py#L240). Bamboozle: `camera.py`. Risk: more moving parts. It does remove the need for an ffmpeg with TLS.

**B6. On-demand streaming** (optional). S. Start the upstream when the first `/stream` viewer connects and stop it 5 s after the last leaves. Keep the manual toggle as an "always on" override. Bamboozle: `routers/stream.py`, `CameraStream`.

---

## 3. File transfer, FTPS and 3MF (feeds group D)

### 3.1 BamBuddy

- **Implicit FTPS client** ([bambu_ftp.py `ImplicitFTP_TLS` #L426](https://github.com/maziggy/bambuddy/blob/98ecaf5020e3a20a5de49859b8ad5a754d5ad85c/backend/app/services/bambu_ftp.py#L426)):
  - TLS ≥1.2.
  - X1C/P1S (vsFTPd) need **TLS session reuse** on the data channel. We already do this.
  - **A1/A1 mini:** data channel TLS times out on some firmware, so they fall back to `prot_c` (plaintext data, encrypted control) and cache the working mode per IP.
  - **P2S, X2D and H2C:** TLS capped at **1.2** ([ftp_profiles.py](https://github.com/maziggy/bambuddy/blob/98ecaf5020e3a20a5de49859b8ad5a754d5ad85c/backend/app/services/ftp_profiles.py)).
  - Dedicated thread pool, per-IP download lock (the printer has effectively one FTP socket, and parallel transfers cause 425 errors), and a 300 s handshake cool-off after a failure.
- **Where the print file lives** ([print_storage.py](https://github.com/maziggy/bambuddy/blob/98ecaf5020e3a20a5de49859b8ad5a754d5ad85c/backend/app/services/print_storage.py)):
  - Port 990 serves **external storage only** (SD/USB).
  - H2 series, P2S and X2D default to the internal eMMC. In the `project_file` echo, `url` is `ftp://<name>` for external, or `brtc://emmc/<name>` / `file:///userdata/...` for internal.
  - A touchscreen start announces itself as an unsolicited `project_file` response on the **report** topic about 2 s before PREPARE ([`_capture_report_project_file` #L2067](https://github.com/maziggy/bambuddy/blob/98ecaf5020e3a20a5de49859b8ad5a754d5ad85c/backend/app/services/bambu_mqtt.py#L2067)).
  - They probe a bounded set of paths for internal-URL prints, because some H2D builds still expose `/cache/<name>`.
- **Cover / thumbnail** ([routes/printers.py `get_printer_cover` #L1128](https://github.com/maziggy/bambuddy/blob/98ecaf5020e3a20a5de49859b8ad5a754d5ad85c/backend/app/api/routes/printers.py#L1128)):
  - The 3MF name comes from `subtask_name`. They try `<name>.gcode.3mf`, `<name>.3mf`, and the space→underscore variants, each across `/`, `/cache`, `/model` and `/data`.
  - Plate number precedence: dispatched plate, then the `plate_(\d+).gcode` regex on `gcode_file`, then a unique `Metadata/plate_*.gcode` inside the 3MF, then plate 1.
  - Positive cache, **negative cache** (no retry storm per refresh), and single-flight per (printer, subtask, view).
  - Views: angled `plate_N.png`, `top_N.png`, and the `pick_N.png` object mask.
- **3MF parsing** ([utils/threemf_tools.py](https://github.com/maziggy/bambuddy/blob/98ecaf5020e3a20a5de49859b8ad5a754d5ad85c/backend/app/utils/threemf_tools.py)): per-plate metadata (print time, filament usage per slot in grams, bed type and temperature, max Z), per-layer filament usage from G-code, and embedded presets. All of it uses `zipfile` + `defusedxml`.
- **Upload and start:** FTPS upload with progress, then `project_file` with `url`, `ams_mapping`/`ams_mapping2`, `use_ams`, calibration flags and per-model quirks ([`start_print` #L5890](https://github.com/maziggy/bambuddy/blob/98ecaf5020e3a20a5de49859b8ad5a754d5ad85c/backend/app/services/bambu_mqtt.py#L5890)). There is an SD browser with list, download, delete, plate thumbnails and storage info (routes/printers.py `/files*` around #L1604-L2031).

### 3.2 Bamboozle comparison

- `thumbnail.py` looks **only in `/cache`**. If there is no name match it returns the **last 3MF in the listing**, which risks showing the wrong job's picture.
- It runs inside `_read_sync` on the polling thread. A slow FTP transfer (a 30 MB 3MF at about 150 KB/s takes minutes) holds that `to_thread` worker. With 4 printers the default pool copes, but it delays that printer's state.
- No negative cache. A failure is retried every poll (3 s) for the same job, because `_thumb_fetched_for` is only set on success.
- No TLS 1.2 cap for P2S/H2C, and no A1 `prot_c` fallback.
- For H2C/P2S prints stored on eMMC, no thumbnail is possible over FTPS. We should say why rather than silently showing none.

### 3.3 Recommendations (D, file side)

**D1. Thumbnail robustness.** M.
- (a) Candidate names from `subtask_name` (`.gcode.3mf`, `.3mf`, `_` variants) across `/`, `/cache`, `/model`, `/data`. Remove the "last 3MF" fallback.
- (b) Negative-cache per job key. Fetch on a separate background task, not inside `_read_sync`.
- (c) Use `maximum_version = TLSv1_2` for P2S/H2C, plus an A1 `prot_c` data-channel fallback.
- (d) Read the `project_file` `url` from report-topic frames. If it is internal (`brtc://emmc/`, `file:///userdata/`), show "Thumbnail unavailable: file stored on internal storage. Enable *Store sent files on external storage* in Bambu Studio".
- (e) Extract plate time and filament grams from `Metadata/slice_info.config` while the 3MF is downloaded anyway.
- **BamBuddy:** links above.
- **Bamboozle:** `thumbnail.py`, `printer_manager.py`.
- **Risks:** more FTP connections per job. Bound them to one connection walking the candidate paths.

**D7. Upload / start print / SD browser.** L. Useful but beyond a monitor. Needs a 3MF picker, AMS mapping UI and FTPS upload. Defer.

---

## 4. Discovery

- **BamBuddy** ([discovery.py](https://github.com/maziggy/bambuddy/blob/98ecaf5020e3a20a5de49859b8ad5a754d5ad85c/backend/app/services/discovery.py)):
  - SSDP on UDP 2021 with **active** `M-SEARCH` (ST `urn:bambulab-com:device:3dprinter:1`) every 3 s during a 10 s scan window ([#L155](https://github.com/maziggy/bambuddy/blob/98ecaf5020e3a20a5de49859b8ad5a754d5ad85c/backend/app/services/discovery.py#L155)).
  - Parses `USN`, `DevName.bambu.com`, `DevModel.bambu.com`, and an `NT` fallback.
  - Docker fallback: a subnet scan (ports 990 + 8883 open, then a **unicast M-SEARCH** to that IP for serial/model, [#L438](https://github.com/maziggy/bambuddy/blob/98ecaf5020e3a20a5de49859b8ad5a754d5ad85c/backend/app/services/discovery.py#L438), [#L519](https://github.com/maziggy/bambuddy/blob/98ecaf5020e3a20a5de49859b8ad5a754d5ad85c/backend/app/services/discovery.py#L519)).
  - Model-code table in [utils/printer_models.py](https://github.com/maziggy/bambuddy/blob/98ecaf5020e3a20a5de49859b8ad5a754d5ad85c/backend/app/utils/printer_models.py).
- **Bamboozle:** passive NOTIFY listener, cert CN verification and IP relocation. This is more robust than BamBuddy for keeping existing printers connected.
- **Adopt:**
  - **Unicast M-SEARCH probe for manual add** (S): when the user types an IP in Settings, send M-SEARCH to `ip:2021` to pre-fill the serial, name and model. Combine it with our cert CN read, which already gives the serial.
  - **Model-code gaps** (S): add `O1C` (H2C), `O1S` (H2S), `O1E` / `O2D` (H2D Pro), `N6` (X2D), `N9` (A2L), `A04` / `A11` / `A12` (A1 variants). These are facts, so safe to reimplement. Bamboozle: `discovery._MODELS`.
- **Skip:** subnet scanning (we aren't containerised; SSDP works on the user's flat subnet).

---

## 5. Data model: fields BamBuddy decodes that we don't (group C)

Our `PrinterState` today has: gcode_state, `print_status` (library stg enum), progress, remaining, layers, nozzle/bed temps (targets always `None`), `chamber_temp` (always `None`, though the UI has a tile for it), file name, `spd_mag`, light, filaments (colour/type/name) and `error_code` (never set).

**C1. Temperatures and targets.** S. *High value.*
- `nozzle_target_temper`, `bed_target_temper`, `chamber_temper` (real only on models with a sensor: X1 family, P2S, H2*; meaningless on A1/P1, so hide it there, per [printer_manager.py `supports_chamber_temp` #L83](https://github.com/maziggy/bambuddy/blob/98ecaf5020e3a20a5de49859b8ad5a754d5ad85c/backend/app/services/printer_manager.py#L83)). `mc_target_cham` holds the chamber target.
- **H2 series encodes** `value = target*65536 + current` when the heater is active (values over 500), and plain °C otherwise. This applies to `device.extruder.info[i].temp` (per nozzle; index 0 = right, 1 = left), `device.bed.info.temp`, `device.ctc.info.temp` (chamber) and sometimes `chamber_temper` ([bambu_mqtt.py #L4400-L4780](https://github.com/maziggy/bambuddy/blob/98ecaf5020e3a20a5de49859b8ad5a754d5ad85c/backend/app/services/bambu_mqtt.py#L4400)).
- On H2D/H2C the flat `nozzle_temper` mirrors one nozzle, so show both nozzles from `device.extruder.info`.
- **Bamboozle:** `_read_sync` (from `mqtt_dump()`), `models.PrinterState` (`nozzle2_temp` / `target`), `app.js`.

**C2. HMS and `print_error`.** S–M. *High value.*
- **`hms[]`:** `attr` and `code` are ints or hex strings. `full = f"{attr:08X}{code:08X}"`, `severity = code >> 16` (1 fatal, 2 serious, 3 common, 4 info), `module = attr >> 24`. Skip `code & 0xFFFF < 0x4000`.
- **`print_error`:** 32-bit, shown as `MMMM_EEEE`. Skip when 0 or `low16 < 0x4000`.
- Filter the user-cancel echoes `0300_400C` and `0500_400E` ([#L739](https://github.com/maziggy/bambuddy/blob/98ecaf5020e3a20a5de49859b8ad5a754d5ad85c/backend/app/services/bambu_mqtt.py#L739), [#L4867-L5010](https://github.com/maziggy/bambuddy/blob/98ecaf5020e3a20a5de49859b8ad5a754d5ad85c/backend/app/services/bambu_mqtt.py#L4867)).
- Text: BamBuddy generates a catalogue from **Bambu Studio's `resources/hms/hms_en_<model>.json`** ([scripts/generate_hms_catalog.py](https://github.com/maziggy/bambuddy/blob/98ecaf5020e3a20a5de49859b8ad5a754d5ad85c/scripts/generate_hms_catalog.py)). For us, either:
  - (a) link to Bambu's wiki per code (zero licence risk); or
  - (b) build our own table from a permissively licensed source. ha-bambulab ships HMS tables; check its licence first. Bambu Studio is AGPL, and its message text is likely copyrightable, so don't bundle it into MIT Bamboozle without thinking.
- Optional "Clear error" action: `{"print":{"command":"clean_print_error","sequence_id":"0"}}` ([#L7189](https://github.com/maziggy/bambuddy/blob/98ecaf5020e3a20a5de49859b8ad5a754d5ad85c/backend/app/services/bambu_mqtt.py#L7189)).
- **Bamboozle:** new `hms.py`, `PrinterState.hms: list[...]`, a card badge.

**C3. Stage names.** S.
- Map `stg_cur` to a name (0–77, from Bambu Studio's DeviceManager, e.g. 1 "Auto bed leveling", 2 "Heatbed preheating", 7 "Heating nozzle", 14 "Cleaning nozzle tip"…).
- Treat -1 / 255 as idle. **Ignore `stg_cur=0` unless gcode_state is RUNNING/PAUSE** (the A1/P1/P1S idle bug).
- Unknown stage numbers display as "Preparing".
- Also: `mc_print_sub_stage`; `ams_status` main/sub (main 1 = filament change, 2 = RFID identifying…); `prepare_per` (prepare %).
- [#L1063](https://github.com/maziggy/bambuddy/blob/98ecaf5020e3a20a5de49859b8ad5a754d5ad85c/backend/app/services/bambu_mqtt.py#L1063), [printer_manager.py `get_derived_status_name` #L1140](https://github.com/maziggy/bambuddy/blob/98ecaf5020e3a20a5de49859b8ad5a754d5ad85c/backend/app/services/printer_manager.py#L1140).
- Stage numbering is protocol fact. Write our own table rather than pasting theirs.
- **Bamboozle:** replace `print_status` (library enum), `app.js`.

**C4. Fans.** S. `cooling_fan_speed` (part), `big_fan1_speed` (aux), `big_fan2_speed` (chamber), `heatbreak_fan_speed`, on a 0–15 scale (→ %). The H2/P2S left aux fan is in `device.airduct.parts`. [#L4207](https://github.com/maziggy/bambuddy/blob/98ecaf5020e3a20a5de49859b8ad5a754d5ad85c/backend/app/services/bambu_mqtt.py#L4207).

**C5. AMS details.** S.
- Per unit: `humidity_raw` (% RH). `humidity` is a 1–5 index that runs the *opposite* way (high = dry), so never show it as % ([utils/ams_humidity.py](https://github.com/maziggy/bambuddy/blob/98ecaf5020e3a20a5de49859b8ad5a754d5ad85c/backend/app/utils/ams_humidity.py)).
- Also per unit: `temp`; `dry_time` (drying minutes left).
- Per tray: `remain` (% left; -1 = unknown), `tray_weight`, `k`.
- Active slot: `tray_now` (254/255 = external / none). On H2, per extruder from `device.extruder.info[i].snow` (`ams_id = snow >> 8`, `slot = snow & 0xFF`).
- **Bamboozle:** `Filament` gains `remain`, `active`; an AMS-unit model with humidity and temp.

**C6. Misc status.** S.
- `wifi_signal` (often the string `"-52dBm"`; -90 means wired on Ethernet-capable models).
- Door open: `home_flag` bit 23 on X1; `stat` bit 23 on the others ([#L5016-L5080](https://github.com/maziggy/bambuddy/blob/98ecaf5020e3a20a5de49859b8ad5a754d5ad85c/backend/app/services/bambu_mqtt.py#L5016)).
- `sdcard`; nozzle `nozzle_type` / `nozzle_diameter` (H2: `device.nozzle.info[]`).
- Firmware: `info.command=get_version`, module `ota`, `sw_ver`.
- `spd_lvl`: 1 silent, 2 standard, 3 sport, 4 ludicrous. We show `spd_mag` %; a name is clearer.
- `lights_report[]` per node.
- `ipcam.timelapse` / `ipcam_record`.
- `s_obj` (skipped objects).
- `home_flag` bit 11 = store-to-SD.

---

## 6. Other features (group D) and what not to adopt (group E)

### Worth considering (D)

| Item | BamBuddy location | Bamboozle mapping | Effort | Notes |
|---|---|---|---|---|
| **Notifications** (ntfy / Discord / Telegram / webhook) on finish, fail, HMS and offline; offline is debounced (they wait before notifying, [main.py#L1405](https://github.com/maziggy/bambuddy/blob/98ecaf5020e3a20a5de49859b8ad5a754d5ad85c/backend/app/main.py#L1405)) | [services/notification_service.py](https://github.com/maziggy/bambuddy/blob/98ecaf5020e3a20a5de49859b8ad5a754d5ad85c/backend/app/services/notification_service.py) | new `notify.py` driven by state edges in `_poll_loop` (or the A5 event path); settings in `AppConfig` | M | ntfy alone is a single HTTP POST, so S. Attach a snapshot from `get_frame()` |
| **Connection diagnostic** (ports 8883/990/322/6000, auth, dev mode, zero-reports → serial, storage verdict) | [services/printer_diagnostic.py](https://github.com/maziggy/bambuddy/blob/98ecaf5020e3a20a5de49859b8ad5a754d5ad85c/backend/app/services/printer_diagnostic.py) | Settings "Diagnose" button; reuse `discovery.tcp_reachable` and the cert CN read | M | Their rationale: about a third of issues were setup errors |
| **Print history** (start/end/duration/outcome/thumbnail) | [services/archive.py](https://github.com/maziggy/bambuddy/blob/98ecaf5020e3a20a5de49859b8ad5a754d5ad85c/backend/app/services/archive.py), `print_log.py` | JSON-lines in `%LOCALAPPDATA%\Bamboozle\history.jsonl` from gcode_state edges | M | Keep it tiny; no DB |
| **HMS actions** (resume / ignore / retry, matching Bambu Studio's dialog buttons) | [`execute_hms_action` #L8225](https://github.com/maziggy/bambuddy/blob/98ecaf5020e3a20a5de49859b8ad5a754d5ad85c/backend/app/services/bambu_mqtt.py#L8225), `hms_actions.py` | after C2 | M | Gotcha: `err` must be the **decimal** string of the code |
| **Smart plug energy** (Tasmota, Shelly/REST, HA, MQTT) | [smart_plug_manager.py](https://github.com/maziggy/bambuddy/blob/98ecaf5020e3a20a5de49859b8ad5a754d5ad85c/backend/app/services/smart_plug_manager.py), [tasmota.py](https://github.com/maziggy/bambuddy/blob/98ecaf5020e3a20a5de49859b8ad5a754d5ad85c/backend/app/services/tasmota.py) | optional per-printer plug URL; show W/kWh | M | Only if the user owns plugs |
| **HA / MQTT relay** (publish state to an external broker) | [mqtt_relay.py](https://github.com/maziggy/bambuddy/blob/98ecaf5020e3a20a5de49859b8ad5a754d5ad85c/backend/app/services/mqtt_relay.py), [homeassistant.py](https://github.com/maziggy/bambuddy/blob/98ecaf5020e3a20a5de49859b8ad5a754d5ad85c/backend/app/services/homeassistant.py) | optional `relay.py` | M | ha-bambulab already covers HA users |
| **Timelapse download** from SD | `layer_timelapse.py`, `timelapse_processor.py`, FTPS `/timelapse` | after D1 | M | |
| **Spoolman** | [spoolman.py](https://github.com/maziggy/bambuddy/blob/98ecaf5020e3a20a5de49859b8ad5a754d5ad85c/backend/app/services/spoolman.py) | n/a | L | Only if the user runs Spoolman |
| **Prometheus metrics** | routes/metrics.py | `/metrics` text endpoint | S | Nice for Grafana users |

### Not worth adopting (E)

- **Developer-mode write probe** (`ams_filament_setting` to the external slot). It writes printer config. BamBuddy itself notes it destabilised some brokers (#887) and is inconclusive on P1S 01.10. Use the passive `fun` bit and HMS `0500050000010007` instead (A1).
- **Request-topic subscription** (to capture slicer `ams_mapping`). P1S/A1 brokers kill the connection on it, and we have no use for the mapping.
- **`get_accessories`**: stale nozzle data on H2D.
- **Subnet scanning**: only needed in Docker. Our passive SSDP plus relocation is better for a flat LAN.
- **Multi-user auth / OIDC / LDAP / 2FA, finance and billing, batch orders, projects, library, MakerWorld, slicer API, virtual printer / proxy mode, SpoolBuddy hardware, Obico AI, OpenCV plate detection, Postgres, Git backup, K-profile management, AMS drying control, FTS / nozzle-rack logic, print queue / scheduler.** All are scope creep for a local single-user LAN dashboard. Several (dispatch, AMS mapping) carry high risk of mis-commanding a printer.
- **No-ffmpeg RTSP**: BamBuddy has none. Not a reason to switch.
- **Their discovery** in place of ours: ours already verifies identity and relocates; theirs doesn't.

---

## 7. Licence verdict

- **BamBuddy: GNU AGPL-3.0** ([LICENSE](https://github.com/maziggy/bambuddy/blob/98ecaf5020e3a20a5de49859b8ad5a754d5ad85c/LICENSE); README "AGPL-3.0 License"). **Bamboozle: MIT** (`D:\git\Bamboozle\LICENSE`).
- **Copying code** (functions, classes, tables pasted or lightly edited, including the large JSON catalogues in `backend/app/data/`) makes the result a derivative work under AGPL. Distributing Bamboozle with that code requires all of the following:
  - licensing the combined work under AGPL-3.0, so Bamboozle can no longer be MIT;
  - shipping the source;
  - keeping notices;
  - and, under AGPL §13, offering source to anyone using it over a network.
- MIT → AGPL is a one-way change. **Verdict: do not copy BamBuddy code into Bamboozle** unless the user deliberately decides to relicense Bamboozle as AGPL.
- **Reimplementing ideas is fine.** Copyright covers expression, not protocol facts or techniques. These are facts about Bambu's protocol:
  - field names, bit positions (`fun & 0x20000000`, `home_flag` bit 23), and the H2 `target<<16|current` encoding;
  - QoS 1, keepalive 30, stage numbers, ffmpeg flags, and the P2S `-use_wallclock_as_timestamps` fix;
  - the "zero reports means wrong serial" heuristic.
  
  Write our own code from this document or from primary sources (Bambu Studio behaviour, OpenBambuAPI), without pasting their code.
- **Caveats:**
  - Several tables BamBuddy uses are themselves derived from **Bambu Studio (AGPL-3.0)**: STAGE_NAMES from DeviceManager.cpp, and the HMS texts from `resources/hms`. Short lists of numeric facts are low risk to re-type from protocol knowledge. Bulk HMS *message text* is better linked to Bambu's wiki, or sourced from a permissively licensed project after checking its licence.
  - Our current dependency `bambulabs_api` is MIT and paho-mqtt is EPL-2.0/EDL-1.0. Both are fine for MIT Bamboozle.
  - This is an engineering reading of the licence, not legal advice.

---

## 8. Suggested task breakdown (for Docket, if wanted)

1. A1 dev-mode / commands-blocked badge, plus A4 auth-rejected message (S).
2. A2 teardown fix, plus A3 last-message staleness (S).
3. B1 camera backoff reset, B3 frozen watchdog, B4 port-6000 desync (S).
4. B2 ffmpeg profiles, with the model stored on `PrinterConfig` (S).
5. C1 temps / targets / chamber (H2 encoding), C3 stage names, C6 misc (S).
6. C2 HMS decode (wiki links first) (S–M).
7. Capture `report` fixtures from A1 / P1S / H2C / P2S (S). This is the prerequisite for 8.
8. A5 replace `bambulabs_api` with an in-house paho client: event-driven, deep-merge, QoS 1, command verification (M).
9. D1 thumbnail robustness and the internal-storage explanation (M).
10. Notifications (ntfy first) and the connection diagnostic (M).
