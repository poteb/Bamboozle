# Discover Printers Settings

## Status
done  <!-- todo | in progress | blocked | done -->

## Severity
minor  <!-- trivial | minor | major — set at creation; may be updated later (see Severity) -->

## Task
Add a "Discover printers" list to the Add Printer form in Settings. It should show printers found on the LAN through Bambu SSDP announcements that aren't configured yet. Picking one fills in IP, serial, name and model. The user still enters the access code.

Reuse the SSDP listener built in `auto-detect-printer-ip` (Tasks/20261025-091025-auto-detect-printer-ip.md). That task must be done first.

## Grilling
resolved  <!-- n/a (trivial) | pending | resolved — resolved decisions listed below -->

User accepted all 9 recommendations in `## Questions` → `### Grilling` (2026-10-25). Those answers are the binding decisions.

## Notes
- 2026-10-25: Severity `minor`, Grilling `pending` → ran the grilling question round (SEVERITY.md). No code changed, no worktree created.
- Code findings that shape the questions:
  - `PrinterManager._on_announcement` drops every announcement whose serial isn't configured, so nothing is cached. Need a new in-memory cache of seen announcements.
  - `PrinterConfig` has **no `model` field**. The only setting that depends on model is `camera_port` (6000 vs 322). The form has no model input.
  - The settings page has **no WebSocket**. `app.js` only connects when `#printer-grid` exists (the dashboard), so a live list needs polling or new WS wiring.
  - Route trap: `GET /api/printers/{printer_id}` is declared first, so `GET /api/printers/discovered` would hit it and return 404. The new endpoint must live outside `/printers/...` or be declared before it.
  - `SsdpListener.start()` returns False and logs a warning when the bind fails. The manager doesn't keep that result yet.

- 2026-10-25 (exec): Status `in progress`. Worktree `D:/git/worktrees/Bamboozle-discover-printers-settings`, branch `discover-printers-settings`.
  - TDD: `src/tests/test_discovered.py` (19 tests) written first (red: ImportError), then implemented → 47/47 pass.
  - `discovery.model_info(code)` → (friendly name, camera port); unknown → (raw code, None). Table: live-confirmed C12/N2S/O1C2/N7 plus Bambu Studio codes N1, C11, BL-P001, BL-P002, C13, O1D.
  - `PrinterManager`: `_on_announcement` caches every announcement first (`_remember`), keyed by upper-cased serial, latest wins, 5 min TTL, capped at 64 entries (oldest evicted; guards against a LAN flood of fake serials). `discovery_listening` stores `SsdpListener.start()` result. `discovered_printers()` prunes, filters configured serials (case-insensitive, from `config.printers` at call time), sorts by name (casefold, then serial).
  - `GET /api/discovery` → `{listening, printers:[{ip,serial,name,model,model_name,camera_port,last_seen_s}]}`, outside `/printers/...`.
  - Settings Add form: "Discovered on network" list at top of Connection fieldset, polled every 5 s while Add form shown (skips ticks when tab hidden; generation counter drops stale responses); hidden + polling stopped in edit mode. Rows built with textContent only (announcement fields are network-supplied). Pick overwrites IP/serial/camera port (if known), Name only if blank, focuses Access Code, highlights row (survives re-render).
  - CLAUDE.md: documented the cache + endpoint.
  - Commit `125a9f5`.
  - Live check 1 (real config, all 4 configured): `/api/discovery` → `{"listening":true,"printers":[]}`; `/api/printers/discovery` still 404 (no clash concern; route lives at `/api/discovery`).
  - Live check 2 (temp LOCALAPPDATA with config copy minus A1; real config never touched, hash verified unchanged): A1 listed `{ip 192.168.0.102, model N2S, model_name A1, camera_port 6000}`. Headless Edge via CDP: list visible on Add, pick filled name/IP/serial, camera port 322→6000, focus on Access Code, highlight kept after a poll, list hidden in edit mode. Screenshot checked.

  - Self-review (`code-review` skill, Standards + Spec opus agents): no hard violations, spec fully met. Fixed: pick highlight now case-insensitive; added `httpx` to requirements-dev (TestClient). Rejected: sharing one configured-serial source between relocation gate and discovery filter (tried it; relocation must gate on live connections, discovery on config; 2 tests broke, reverted); pydantic model / cache class / named tuple refactors (judgement-call smells, not worth it at this size). Commit `23ac764`.
  - Independent review (fresh opus agent, diff + task file): no P1, one P2: sidebar Edit buttons put name/access code into an inline `onclick` JS string (pre-existing), and a picked DevName could now carry a quote → script injection. Fixed with data-* attributes + `editPrinterFromLink`, with a template-render test. P3s fixed: DevName/DevModel capped at 64 chars; unnamed printers sort by the serial shown; stale rows cleared on poll failure; `overflow-wrap` for long names; added a test that configured serials still reach `handle_announcement`. P3s skipped: no-op rebuild skip, in-flight poll flag, replacing an auto-filled Name on a second pick (the spec says fill only when blank), runtime socket errors not clearing `listening`. Commit `e247a4b`.
  - ChatGPT/Loom review: **triggered** (step 3 found a P2, which is security-related), but no ChatGPT/Loom channel is configured for this project or available to the agent, so it **did not run**. Substitute: a second fresh opus re-review of the fix and the whole branch found no P1/P2. Remaining P3s are pre-existing and out of scope: `app.js` `esc()` doesn't escape `"`/`'` but is used in `title="..."` for filament tooltips; `data-name` duplicated on the `<li>` and the button.
  - Re-verified live after the fix (temp LOCALAPPDATA copy, deleted afterwards; real config hash unchanged): sidebar Edit still fills every field correctly (camera port 322, sort 3, boolean camera flag), and the discovery pick flow is unchanged.
  - Merge gate: 51/51 pytest on main after the merge. Merged `--no-ff` to local main as `e95fea5`, not pushed. Worktree removed and branch deleted.

## Questions
### Grilling
1. **Model.** There's no model field in config. What does "fills in model" mean?
   - **Recommend:** don't add a config field. Map the `DevModel` code to a friendly name, show it in the list, and use it to set the Camera Port dropdown: A1 / A1 mini / P1P / P1S → 6000; X1 / X1C / X1E / H2C / H2D / P2S → 322. Unknown code: leave the port alone and show the raw code. (Confirmed codes so far: `C12` = P1S, `N2S` = A1. The other codes come from Bambu Studio profiles; I'd keep the table small and treat anything missing as unknown.)
2. **Where the list goes.** Inside the Add Printer form only (hidden when editing), as a compact "Discovered on network" list at the top of the Connection fieldset. Each row shows name, model, IP and serial; clicking a row picks it.
   - **Recommend:** yes, Add form only.
3. **How it refreshes.** The settings page has no WS.
   - **Recommend:** poll a new GET endpoint every 5 s while the Add form is visible, and stop when it's hidden or in edit mode. No WS changes.
4. **Cache lifetime.** Keep it in memory only (not persisted), keyed by upper-cased serial, storing the latest IP / name / model and a last-seen time. Drop entries not seen for **5 min**. Latest announcement wins if the IP changes.
   - **Recommend:** as stated.
5. **Already-configured printers.** **Recommend:** hide them, matching on serial (case-insensitive) against the current config, filtered when the endpoint is called so it reflects add/delete right away. No IP-based matching.
6. **API shape.** **Recommend:** `GET /api/discovery` → `{"listening": bool, "printers": [{"ip","serial","name","model","model_name","camera_port": int|null,"last_seen_s": float}]}`, sorted by name. Off `/printers/...` to avoid the `{printer_id}` route clash.
7. **Listener disabled / nothing seen.** **Recommend:** `listening:false` → muted note: "Discovery unavailable: couldn't listen on UDP 2021 (another app may hold it). Enter details manually." `listening:true` with an empty list → "Listening for printers… none found yet (printers announce every few seconds; make sure LAN mode is on)." The form stays fully usable either way.
8. **What a pick fills.** **Recommend:** overwrite IP, serial and camera port (if the model is known). Fill Name with the announced name only if Name is blank. Then focus Access Code. Leave Sort and camera-enabled alone. Highlight the picked row.
9. **Cert check on pick?** **Recommend:** no. The listener already requires the sender address to equal `Location`, and the user's next step is Test Connection, which (with the access code) is the real check. A cert probe would add a ~5 s wait for little gain.
