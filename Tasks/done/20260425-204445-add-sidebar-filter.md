# Add Sidebar Filter

## Status
done  <!-- todo | in progress | blocked | done -->

## Task
Add a text filter input to the top of the printer settings sidebar (`.printer-sidebar` in `src/bamboozle/templates/settings.html`). As the user types, hide list items whose printer name doesn't contain the filter string (case-insensitive substring match). Empty filter shows all. The "New" button stays at the very top; place the filter just below it, above the printer name list.

## Notes
- Added `<input type="text" id="printer-filter" class="printer-filter" placeholder="Filter…" oninput="filterPrinters()" autocomplete="off">` between the New button and the `<ul>` in `src/bamboozle/templates/settings.html`.
- Added `data-name="{{ p.name }}"` on each `<li>` so filtering reads the name without inspecting innerText.
- Added `filterPrinters()` JS: lowercases trimmed input, toggles each `<li>`'s `style.display` based on case-insensitive substring match. Empty/whitespace-only query shows all. Does not touch selection state, so a hidden selected item simply disappears in place.
- Added `.printer-sidebar .printer-filter` rule in `style.css` (`width:100%`, `box-sizing:border-box`, `margin:0 0 0.2rem`). The sidebar's flex `gap: 0.8rem` already gives breathing room above; the small bottom margin keeps it flush against the list.
- Bumped `style.css` and `app.js` query strings from `?v=36` to `?v=37` in `base.html`.
- Sanity check: launched FastAPI from `src/`, curled `/settings`, confirmed the markup contains the new input, the `filterPrinters` function, and `v=37` references; CSS rule is served. Stopped the process.

## Questions
