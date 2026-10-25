# Auto Detect Printer Ip

## Status
done  <!-- todo | in progress | blocked | done -->

## Severity
minor  <!-- trivial | minor | major — set at creation; may be updated later (see Severity) -->

## Task
When a printer's DHCP-assigned IP changes, Bamboozle should find it again on its own instead of failing with "Printer Values Not Available Yet".

- Listen for Bambu SSDP announcements: UDP broadcast to port 2021. Each packet holds `Location: <ip>`, `USN: <serial>`, `DevModel.bambu.com`, `DevName.bambu.com`.
- When an announcement's USN matches a configured printer's serial and the IP differs, update that printer's IP in the config (through the `PrinterManager` path that calls `save_config`) and reconnect.
- Fix the misleading error. When the TCP connection to port 8883 fails, report "printer unreachable at <ip>". This applies to `POST /api/printers/test` (`src/bamboozle/routers/api.py`) and to connect/reconnect in `src/bamboozle/printer_manager.py`. Today both just surface the `bambulabs_api` "Printer Values Not Available Yet" log message.

Context (diagnosed 2026-10-25): after a router or mesh reboot, all 4 printers moved (H2C .186→.100, P2S .125→.101, A1 .220→.102, P1S .51→.103). Access codes were still valid. A probe that bound UDP 2021 with SO_REUSEADDR received announcements from all 4 within about 12s. The printers sit behind a Wi-Fi repeater, so they all share one proxy-ARP MAC (`e2-44-89-c0-a9-7f`). Match on serial only, never on MAC.

## Grilling
resolved  <!-- n/a (trivial) | pending | resolved — resolved decisions listed below -->

1. **Apply automatically.** On a detected move, update the IP through `PrinterManager` (`save_config`, existing per-printer history backup), reconnect, log it, and show a short dashboard notice (e.g. "P1S moved to 192.168.0.103") in the existing UI style.
2. **Switch only when offline.** Act on an announcement only when the printer is currently offline or unreachable at its saved IP. Never switch a healthy connection.
   - **Certificate check.** Before trusting the new IP, check that the printer's TLS certificate CN (or SAN) equals the serial. Verify which of the 4 models (A1, P1S, H2C, P2S) present the serial; you can probe them on the LAN.
   - **Unsupported models.** If a model's cert doesn't carry the serial, skip the check for that model only, fall back to the offline-only rule, and record it in Notes.
3. **Passive listener only.** Always-on listener on UDP 2021 with SO_REUSEADDR, so it can share the port with Bambu Studio. No active M-SEARCH. If the bind fails, log a warning and disable the feature; the app keeps running. Start and stop it with the `PrinterManager` lifecycle.
4. **Out of scope:** discovering unconfigured printers. That is the separate task `discover-printers-settings`.
5. **In scope:** the "printer unreachable at <ip>" error fix (TCP pre-check on 8883 before `mqtt_start`) in `/api/printers/test` and in connect/reconnect.

## Notes
- Worktree `D:\git\worktrees\Bamboozle-auto-detect-printer-ip`, branch `auto-detect-printer-ip`.
- **Cert check result (live probe 2026-10-25, 192.168.0.100-103:8883):** all 4 models present the serial as the subject CN; no SAN on any. H2C CN=31B8BP5A1001391 (issuer "BBL Device CA O1C2-V2"), P2S CN=22E8BJ630504999 (issuer "BBL Device CA N7-V2"), A1 CN=03919D533005771 (issuer "BBL CA"), P1S CN=01P00C511100150 (issuer "BBL CA"). So the check applies to every model, and no per-model skip was needed. Models not in this fleet (X1C, H2D...) weren't verified. If their cert lacks the serial, relocation is refused and a warning is logged.
- Root cause of the misleading error: `bambulabs_api.Printer.mqtt_start()` is paho `connect_async` + `loop_start`, so it never fails on an unreachable host. `_connected` went True, and every getter logged "Printer Values Not Available Yet".
- Design:
  - New `src/bamboozle/discovery.py`: `parse_announcement`, `cert_common_name` (a tiny DER walker, so no new dependency; `cryptography` isn't in requirements), `tcp_reachable`, `fetch_cert_common_name`, and `SsdpListener` (an asyncio datagram endpoint on a manually bound socket with SO_REUSEADDR; on bind failure it logs a warning and returns False).
  - `PrinterManager.handle_announcement` matches on serial only. It acts only when `conn.is_healthy()` is False (`_connected` and paho `is_connected()`). It verifies the cert CN equals the serial, then calls `update_printer` (saves config and makes the history backup), logs, and broadcasts `{"type":"notice"}`. `app.js` shows it as an info toast.
  - A rejected (serial, ip) pair isn't re-probed for 60s.
- Error fix: `PrinterConnection.connect` and `POST /api/printers/test` TCP-probe 8883 first (3s timeout) and report "printer unreachable at <ip>". `read_state` skips the library getters while not connected.
- Extra: poll-loop reconnects now run as background tasks (`_spawn`), guarded by `_connecting`. Without this, the new 3s TCP probe would block dashboard updates for every unreachable printer. A connect that finishes after its connection was replaced disconnects the orphan.
- TDD: added a minimal pytest suite under `src/tests/` (run with `python -m pytest tests` from `src/`; pytest was installed into the 3.14 interpreter). `conftest.py` points LOCALAPPDATA at tmp, so tests never touch the real config. Seams under test: `parse_announcement`, `cert_common_name`, `SsdpListener` (real UDP on localhost), `PrinterManager.handle_announcement` (network adapters `discovery.tcp_reachable` / `fetch_cert_common_name` faked), `PrinterConnection.connect` / `read_state`, and the `test_connection` endpoint. Non-interactive run, so I chose the seams myself rather than confirming them with the user. Mutation-checked the key guards (health, cert, cooldown, SO_REUSEADDR, unreachable): each mutation fails a test.
- Live check (no config change): the parser and DER walker read all 4 real certs correctly, and the listener received all 4 announcements within a few seconds. A TCP probe to the stale .51 takes 3.0s (the timeout).
- **Self-review (`code-review` skill: Standards + Spec sub-agents, opus).** Commit c0760ad reviewed; fixes went in as baa6c36.
  - Fixed: CLAUDE.md said there was no test suite. It now documents the tests and the IP auto-detect flow. Added `src/requirements-dev.txt` and `src/pytest.ini` (`pythonpath = .`, replacing the sys.path hack). The duplicated `FakeWs` moved to `tests/fakes.py`.
  - Fixed: `is_healthy()` was False while a connect was in progress, so a dual-interface printer's other IP could be adopted. Replaced it with `is_offline()`: False while connecting or within a 20s grace after a session starts; True otherwise when paho isn't connected.
  - Fixed: a mid-session IP change never produced "printer unreachable". The poll loop now resets a session that `is_offline()` reports down, so the reconnect hits the TCP pre-check.
  - Fixed: the reconnect log spam. Reconnects now back off exponentially from 3s to 60s, reusing the previously unused `_backoff`. The `_connecting` guard moved into `PrinterConnection`.
  - Fixed: `stop()` now awaits its cancelled tasks.
  - Rejected / kept:
    - The duplicated pre-check in `api.py` and `connect`: two call sites, different return shapes.
    - Explicit `MQTT_PORT` arguments.
    - Splitting the DER walker into its own module: small, and only used here.
    - The unbounded `_rejected` dict: keyed by configured serial × announced IP, so it stays tiny.
- **Independent review** (fresh opus agent, read-only; given the diff and this task file, not my reasoning). Reviewed baa6c36: no P1s, 3 P2s, 6 P3s. Fixes went in as f6723ab.
  - P2-1 fixed: the grace period started at connect time, so a brief paho blip on a long-lived session tore it down and allowed relocation. Grace now counts from `_last_seen_up`.
  - P2-2 fixed: if another printer takes the old IP, TCP passes but MQTT never comes up, and the reconnect loop left hardly any offline window. Backoff now resets only once a session is verified, and `reset_session` schedules a retry, so the offline windows grow (3 → 6 → 12...s). I chose this over a connect-time cert check so models that weren't verified don't get locked out.
  - P2-3, partly fixed: a spoofed announcement plus a fake cert (CN = serial) could redirect the app. Announcements are now dropped unless the UDP source equals `Location`, and `Location` must be a private IP. A live probe confirmed that source == Location for all 4 printers, even behind the proxy-ARP repeater. Not done: cert fingerprint pinning. It's beyond the grilled design (CN check), and the library's MQTT already uses CERT_NONE, so an attacker on the LAN can get the access code anyway.
  - P3 fixed:
    - `stop()` order.
    - No task is spawned for unknown serials.
    - `_rejected` is pruned.
    - Added `is_offline` tests through `connect()` with a fake mqtt_client.
  - P3 not done:
    - Coexistence with Bambu Studio on UDP 2021 is untested (Studio wasn't running).
    - `mqtt_stop` doesn't close the socket. That was already true before this task.
    - No test covers the printer recovering during the cert probe.
- **ChatGPT/Loom review: risk-triggered** (step 3 found P2s, and P2-3 is security-related). No ChatGPT/Loom channel is set up for this project or available to this agent, so it was skipped. As a substitute, the same independent reviewer re-reviewed the fix commit.
- Re-review of f6723ab: clean, no new P1/P2. Its new P3s were left as is:
  - Grace vs. a `poll_interval` above 20s: the default is 3s.
  - Source-address check if the repeater rewrote addresses: verified live that it doesn't.
  - The timing-based backoff test: stable over 5 runs.
- **End-to-end (real run, 2026-10-25 10:39):** started `python -m bamboozle.main` from the worktree's `src/` with the stale config. Within 11s:
  - All 4 printers logged "printer unreachable at <old ip>".
  - Then "X moved from <old> to <new>": H2C .186→.100, P2S .125→.101, A1 .220→.102, P1S .51→.103.
  - Each reconnected, and `/api/printers` showed all 4 online.
  - config.json now holds the new IPs, and history backups were written for each printer.
  - No "Printer Values Not Available Yet" in the log.
  - The dashboard toasts were not observed live: the browser's WebSocket connected after the moves. They are covered by a unit test.
  - Server stopped afterwards. A pre-run copy of config.json is at `/tmp/config.json.pre-e2e.bak` (not restored, since this was the real run).
- Merge gate: the project defines none. Ran the full pytest suite (28 passed), then merged `auto-detect-printer-ip` into `main` locally (532157a, --no-ff). Not pushed.
- Housekeeping: `.gitignore` now has `__pycache__/` and `.pytest_cache/`. Note: 5 `.pyc` files under `src/bamboozle/routers/__pycache__/` are still tracked in git (they predate this task). Not touched.

## Questions
