# Dashboard Http Initial Load

## Status
done  <!-- todo | in progress | blocked | done -->

## Severity
minor  <!-- trivial | minor | major — set at creation; may be updated later (see Severity) -->

## Task
The dashboard shows "Connecting to printers..." until the WebSocket delivers its `init` message. In Firefox that can take up to about 60s: after repeated failed WebSocket connects (for example, tabs left open while the server was down), Firefox delays new connections to that server. Plain HTTP isn't delayed.

- On page load, fetch `GET /api/printers` over HTTP and render the printers immediately.
- Keep the WebSocket for live updates.
- Show a small "reconnecting…" marker on the dashboard while the WebSocket isn't open.

## Grilling
resolved  <!-- n/a (trivial) | pending | resolved — resolved decisions listed below -->

The user approved this approach (2026-10-25):
1. Initial state comes from `GET /api/printers`, which returns `{"printers": [PrinterState, ...]}` with `printer_id` on each. Map it into `this.printers` keyed by `printer_id`, then call `render()`. A WS `init`/`update` that arrives later overwrites it as usual. An HTTP response that arrives after WS `init` must not overwrite newer WS data: skip it once any WS message has been applied.
2. If the HTTP fetch fails, stay silent and fall back to waiting for WS (no error toast).
3. Reconnecting marker: subtle, consistent with the existing dashboard header styling. Show it when the WS is not OPEN, hide it on `onopen`. Don't show it during the first ~1s after page load, to avoid a flash on normal loads.
4. Keep the existing 2s client reconnect timer; Firefox throttles it anyway, and that is fine.
5. Out of scope: server-side changes.

## Notes
- 2026-10-25 (exec): Status `in progress`. Worktree `D:/git/worktrees/Bamboozle-dashboard-http-initial-load`, branch `dashboard-http-initial-load`.
  - TDD seam: there was no JS harness, but `app.js` loads cleanly into a `node:vm` context with stubbed `document` / `fetch` / `WebSocket` / `setTimeout` (Node 24 is installed, no npm deps). Added `src/tests/js/dashboard_load.test.mjs` (10 tests, `render()` stubbed so they cover state logic only). Red first (8 fail), then green. Run: `node --test "src/tests/js/*.test.mjs"` from the repo root (documented in CLAUDE.md).
  - `app.js`: the constructor (dashboard only) calls `_loadInitial()` (fetch `/api/printers`, maps the list by `printer_id`, `render()`; non-OK, network error or bad JSON → silent), then `connect()`. `onMessage` sets `_wsStateReceived` on `init`/`update`, after which a late HTTP response is ignored. Marker `#ws-status` ("Reconnecting…", muted 0.85rem text + pulsing `--status-paused` dot, right end of the toolbar, `role="status"`): `_updateWsMarker()` runs on `onopen` / `onclose`; it is armed by a `WS_MARKER_GRACE_MS` = 1000 timer after load. The 2s reconnect timer is unchanged. No server changes. Asset cache-bust `?v=40` → `?v=41`.
  - Commit `0ac121e`. 51/51 pytest still green.
  - Self-review (`code-review` skill, Standards + Spec opus agents): Spec found nothing missing or wrong. It flagged the new JS test harness as minor scope creep (kept; it's the TDD seam the severity recipe asks for) and noted that the WS merges into `printers` rather than replacing it (pre-existing behaviour, left alone). Standards: no hard violations. Fixed these judgement calls: reuse the existing `pulse` keyframes; `_wsApplied` → `_wsStateReceived`; underscore the internal `_loadInitial` / `_applyInitialPrinters`; named the 1s grace constant. Skipped: sharing one merge loop between the HTTP list and the WS dict (the shapes differ; a helper would be bigger than the duplication). Commit `b46be7f`.
  - Live check (own instance on port 8093, temp LOCALAPPDATA with a config copy; real config untouched; webbrowser.open patched so no tab opened; headless Edge via CDP, 4 real printers):
    1. WS stubbed to never open: 4 cards rendered from HTTP at ~0.7s, loading msg gone, marker hidden; at ~2s marker visible (screenshot checked, marker shows at the right of the toolbar).
    2. WS delayed 5s: cards from HTTP at 1.5s; a sentinel written into a temp cell was overwritten once the WS delivered init + update (~7s), and the marker hid.
    3. Normal load: marker never visible (MutationObserver + 20ms poll over ~4.7s); WS open at ~0.1s.
    4. Server killed: marker shown immediately (WS closed), stayed shown through the 2s retries; server restarted: WS reopened and marker hid.
  - Independent review (fresh opus agent, diff + task file): no P1. One P2: cards now render before the WS opens, but `sendCommand` silently dropped clicks while the WS wasn't open. Fixed: falls back to the REST action endpoints (pause/resume/stop, light `{on}`, speed `{level}`) and toasts `{success, message}` like `command_result`; an action with no REST route toasts "Not connected — reconnecting". P3s fixed: WS `init` now replaces `this.printers` (render() already drops removed cards); the marker is toggled by its text, so the `role="status"` live region announces it; no pulse under `prefers-reduced-motion`; tests pin the grace behaviour (closed socket inside the grace period), cover HTTP after an init-less socket, and read `WS_MARKER_GRACE_MS` from app.js; fixed the test `runTimers` helper, which dropped unrelated timers. P3s skipped: toolbar layout shift on narrow screens (accepted). Commit `bd0131c`.
  - Re-review of the fix (fresh opus agent): no P1/P2. P3s fixed: the marker span stays rendered while empty (`:not(:empty)::before` dot) so screen readers keep the live region; REST fallback checks `res.ok` and toasts the status; `toggleCamera` guards against a printer removed by `init` mid-request. Kept `set_speed` mapping (no current UI caller; cheap, matches the API). Cache-bust now `?v=43`. Commit `15c5830`. 24 node tests, 51 pytest.
  - Re-verified live after the fixes: WS blocked → cards at 0.7s, "Reconnecting…" at 2s; normal load → marker never shown; REST fallback against a nonexistent printer id → "Printer not found" error toast; unknown action → "Not connected — reconnecting" toast. Didn't click real pause/stop/light (real printers, one was printing).
  - ChatGPT/Loom review: **triggered** by the P2 (not security-related; a lost-command UX bug). No ChatGPT/Loom channel is configured for this project or available to the agent, so it **did not run**. The fresh opus re-review above stood in for it.
  - Merge gate: 51/51 pytest + 24/24 node tests on main after merge. Merged `--no-ff` to local main as `722adbd`, not pushed. Worktree removed, branch deleted. Temp config copy deleted; real config hash unchanged. Side note: the verification instance also bound UDP 2021 (discovery) alongside the user's instance for the run.
  - Note for the user: the running 8080 instance serves static files from `src/`, so the new app.js/CSS (`?v=43`) apply on the next page reload. The template change (marker span) may need a server restart if Jinja caches it.

## Questions
