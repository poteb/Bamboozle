# Add Filter Clear Button

## Status
done  <!-- todo | in progress | blocked | done -->

## Task
Add an inline "X" clear button inside the printer-settings sidebar filter input. Visible only when the input has content; clicking it empties the input and re-runs the filter (showing all printers). Position the button at the right edge of the input, vertically centered, with a small click target.

## Notes
- Wrapped the filter input in `<div class="printer-filter-wrap">` and added a `<button type="button" id="printer-filter-clear" class="printer-filter-clear" aria-label="Clear filter" hidden>×</button>` (literal multiplication sign) sibling.
- `filterPrinters()` extended to toggle the clear button's `hidden` attribute based on `input.value.length === 0` — filtering logic itself unchanged (still trims + lowercases for the substring match).
- New `clearPrinterFilter()` empties the input, calls `filterPrinters()` (which hides the X), then refocuses the input.
- CSS: added `.printer-sidebar .printer-filter-wrap { position: relative; margin: 0 0 0.2rem; }`, moved the existing `margin: 0 0 0.2rem;` from `.printer-filter` into the wrap, gave `.printer-filter` `padding-right: 1.8rem` to keep text from sliding under the button. Clear button is absolutely positioned right + vertically centered, transparent background, dim color brighter on hover/focus.
- Bumped both cache busters in `base.html` from `?v=38` to `?v=39`.
- Verified by starting the server with `C:/Users/paw/AppData/Local/Python/pythoncore-3.14-64/python.exe -m bamboozle.main` from `src/`, curling `/settings` (markup + cache-busters present) and `/static/style.css?v=39` (all four new rules served), then stopped the server.


## Questions
