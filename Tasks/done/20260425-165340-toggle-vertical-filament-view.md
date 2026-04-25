# Toggle Vertical Filament View

## Status
done  <!-- todo | in progress | blocked | done -->

## Task
Clicking any filament square on a card toggles that card's filament row between the default compact view (horizontal squares with hover tooltips) and an expanded view where filaments are stacked vertically and the tooltip text is shown to the right of each color square. Toggle is per-card. Clicking again returns to the compact view.

## Notes
- Added `this._expandedFilaments = new Set()` to `BamboozleApp` constructor; tracks printer ids whose filaments row is currently expanded. Ephemeral, no localStorage.
- `_renderFilaments(s, id)` now takes the printer id and emits either compact squares or `.filament-row-item` (square + `.filament-label`) divs depending on expanded state. Class list is `filaments-row` or `filaments-row expanded`. Whole row has `onclick="app.toggleFilamentsExpanded('${id}')"` so clicking a square anywhere toggles.
- Reuses `_filamentTooltip(f)` for both `title` attribute (kept on the square in both views, also added to the label span) and the visible label text in expanded mode — same string in both views.
- New method `toggleFilamentsExpanded(id)` flips the Set entry and rewrites just that card's `.filaments-row` element (className + innerHTML) without re-rendering the whole card.
- Updated both callers — `renderCard` and the `.filaments-row` update branch in `updateCard` — to pass `id`. The update branch now also refreshes `className` (not just `innerHTML`) so a WS tick doesn't clobber the `expanded` modifier.
- CSS: `.filaments-row` got `cursor: pointer`. New `.filaments-row.expanded { flex-direction: column; align-items: flex-start; flex-wrap: nowrap; gap: 0.3rem; }`. Added `.filament-row-item` (flex row, square+label) and `.filament-label` (small, dim, monospace). Removed `cursor: help` from `.filament-square` so the row's pointer cursor wins.
- Bumped `style.css` and `app.js` cache busters from `?v=22` to `?v=23` in `base.html`.
- Verified by inspection of edits — no test suite to run.

## Questions
