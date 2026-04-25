# Printer List Sidebar

## Status
done  <!-- todo | in progress | blocked | done -->

## Task
Restructure the Printer Settings page so the "Configured Printers" list becomes a left sidebar and the Add/Edit form sits to the right.

- **Sidebar (left)**: a "New" button at the top; below it a vertical list of printer names only (no IP, no serial, no toggle indicators, no Edit/Delete buttons).
- **Click a printer name** → loads that printer into the existing form on the right (Edit mode — same as the current `editPrinter(...)` JS).
- **Click New** → resets the form to its blank Add Printer state.
- **Form (right)**: the existing two-column form, kept as-is.
- **Delete**: the existing Delete button is removed from the list. Surface a Delete button inside the form, visible only in Edit mode (next to or near the submit button), so the action is still reachable.

Apply the existing dashboard responsive breakpoint convention (e.g. stack to single column below ~760px).

## Notes
- Replaced the old `<h3>Configured Printers</h3>` + `#printer-list` block in `settings.html` with a `<div class="settings-layout">` wrapping a `<aside class="printer-sidebar">` (New button + `<ul>` of `.printer-link` buttons) and the existing `<article>` form.
- Added `newPrinter()` JS to clear all form fields, hidden `edit-id`, restore "Add Printer" titles, hide the delete button, and clear the sidebar selected state.
- `editPrinter(...)` now also calls `setSelectedSidebarItem(id)` to highlight the active row, and unhides the in-form Delete button.
- Added `deleteCurrentPrinter()` helper that pulls the current `edit-id` and reuses `deletePrinter(id, name)` (unchanged).
- Removed the `window.scrollTo` from `editPrinter` since the form is no longer below the list.
- New CSS in `style.css`: `.settings-layout` (240px / 1fr grid, stacks at 760px), `.printer-sidebar` (`.new-btn`, `ul`, `.printer-link`, hover/`.selected` states), and an updated `.form-submit-row` flex layout so the Delete button sits left of the full-width Save button (red `--pico-color-red-500` accent).
- Bumped cache buster `?v=35` -> `?v=36` for both `style.css` and `app.js` in `base.html`.
- Sorted `printers` in `pages.py` settings handler by `(sort, name.lower())` so the sidebar honours the existing Sort field.
- Verified by curling `http://127.0.0.1:8080/settings`: rendered HTML contains `settings-layout`, `printer-sidebar`, `printer-link` rows for the 3 configured printers, the New button, and the in-form Delete button (initially `display: none`); old "Configured Printers" header/list block is gone. CSS at `/static/style.css?v=36` ships the new rules. (Used the user's already-running instance because the port was busy.)

## Questions
