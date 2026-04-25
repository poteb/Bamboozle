# Tighten Ui Density

## Status
done  <!-- todo | in progress | blocked | done -->

## Task
Make the entire UI slimmer and more desktop-oriented. Pico CSS's defaults give buttons, inputs, and form rows generous touch-target padding meant for tablets/phones; this app is desktop-only and should feel denser.

Approach: override the relevant Pico CSS variables in a small, coordinated pass on `:root` (or `[data-theme="dark"]`) at the top of `src/bamboozle/static/style.css`. Suggested variables to revisit (sub-agent: pick reasonable values, document them in `## Notes`):

- `--pico-spacing` — base spacing unit (Pico default `1rem`).
- `--pico-form-element-spacing-vertical` and `--pico-form-element-spacing-horizontal`.
- `--pico-font-size` — slightly smaller base font is fine.
- Button-specific overrides if the variables don't reach far enough.

Spot-check after the pass: dashboard cards, the printer settings sidebar (filter input, list rows, New button), the add/edit form (inputs, selects, submit row, Test/Delete/Add buttons), the camera-feed control overlay buttons. Aim for a noticeably tighter, more compact look without feeling cramped.

Bump cache busters in `base.html`.

## Notes
- Added a Pico variable override block on `:root` at the top of `src/bamboozle/static/style.css` with a comment explaining the desktop-density intent. Overrides:
  - `--pico-spacing: 0.7rem` (default 1rem)
  - `--pico-form-element-spacing-vertical: 0.45rem` (default 0.75rem)
  - `--pico-form-element-spacing-horizontal: 0.7rem` (default 1rem)
  - `--pico-font-size: 95%` (default 100%)
  - `--pico-line-height: 1.45` (default 1.5)
- These cascade into the `[data-theme="dark"]` scope via inheritance, so no separate dark-theme block was needed.
- Trimmed several absolute-spacing rules in `style.css` so dashboard cards don't look chunky relative to the new scale (kept all values proportional to the original):
  - `.printer-card header` padding: `1rem 1.2rem` -> `0.7rem 0.9rem`
  - `.printer-card .actions` padding: `0.8rem 1.2rem` -> `0.55rem 0.9rem`; `min-height: 3.5rem` -> `3rem`
  - `.printer-card > div, .printer-card > .file-name` padding: `0 1.2rem` -> `0 0.9rem`
  - `.progress-row` padding: `0.5rem 1.2rem` -> `0.4rem 0.9rem`
  - `.detail-row` padding: `0.2rem 1.2rem 0.5rem` -> `0.15rem 0.9rem 0.4rem`
  - `.temps-row` padding: `0.8rem 1.2rem` -> `0.55rem 0.9rem`
  - `.speed-row` padding: `0 1.2rem 0.5rem` -> `0 0.9rem 0.4rem`
  - `.filaments-row` padding: `0.5rem 1.2rem` -> `0.4rem 0.9rem`
  - `.printer-form .connection-group` padding: `0.8rem 1rem 1rem` -> `0.6rem 0.8rem 0.7rem`
  - `.printer-form` grid `gap: 1.5rem` -> `1.2rem`
- Left filament squares (20x20), camera-feed aspect ratio, dark theme colors, layout grids, and toast/modal sizes alone per task scope.
- Bumped cache busters in `src/bamboozle/templates/base.html`: style.css and app.js `?v=39` -> `?v=40`.
- Verification: started server from `src/`, curled `/`, `/settings`, `/static/style.css?v=40`. All returned 200; dashboard and settings reference `style.css?v=40` and `app.js?v=40`; the served stylesheet contains all five Pico variable overrides at the top. Server stopped after verification.

## Questions
