# All Cameras Toggle Button

## Status
done  <!-- todo | in progress | blocked | done -->

## Task
Replace the dashboard toolbar's pair of "All Cameras On" and "All Cameras Off" buttons with a single toggle button styled like the per-card camera button (`.cam-toggle-btn` — icon-only, green when on / gray when off), but with the text `All` rendered before the camera icon. Clicking it should turn all cameras on when any are off, and turn all off when all are on (the existing `toggleAllCameras(enabled)` already takes a boolean).

## Notes
- Replaced the two `.camera-controls` buttons in `dashboard.html` with a single `<button id="all-cams-toggle" class="outline small all-cams-btn">`. Did not use `icon-btn` because that class forces zero horizontal padding meant for icon-only buttons; instead added a dedicated `.all-cams-btn` rule.
- Added `_updateAllCamsToggle()` in `app.js`. It computes `allOn = onlineIds.length > 0 && every(camera_enabled === true)`, then sets the button's icon (green `#4caf50` when all on, gray `#888` otherwise via `ICO.camera`), title, and onclick. Mirrors the per-card pattern in `updateCard()`.
- Wired into `render()` so it refreshes on every WebSocket tick. Also called once from the constructor so the button's initial appearance (gray "off") is correct before the first WS message arrives.
- Edge case: zero online printers → `allOn` is `false`, so the button shows gray "Enable all cameras". Clicking calls `toggleAllCameras(true)`, which is a no-op when no online printers exist (the method filters by `online`). Matches the spec.
- Added `.all-cams-btn` and `.all-cams-btn .all-cams-label` CSS rules: inline-flex layout, 0.4rem gap between text and SVG, slightly wider padding than icon-only buttons.
- Bumped cache busters in `base.html` from `?v=25` to `?v=26` for both `style.css` and `app.js`.
- Verification: read-only — visually confirmed the diff is consistent. No live test environment used; cannot confirm the rendered SVG/text alignment in a real browser session.

## Questions
