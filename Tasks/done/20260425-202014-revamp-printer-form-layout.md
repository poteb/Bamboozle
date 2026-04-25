# Revamp Printer Form Layout

## Status
done  <!-- todo | in progress | blocked | done -->

## Task
Re-lay out the Add Printer / Edit Printer forms in `src/bamboozle/templates/settings.html` to match the layout shown in `D:\s.png`:

- Two-column form (left/right at desktop widths; stack on narrow viewports).
- **Left column**: Name, Sort (with the existing help hint), Camera Port dropdown, Enable camera stream toggle (with its existing help hint).
- **Right column**: a bordered "Connection" group containing IP Address, Access Code, Serial Number, **and the "Test Connection" button** (button moves inside the group, replacing its current position outside the form).
- The submit button ("Add Printer" / "Save Changes") sits at the bottom, full width or aligned right — keep current styling, just re-position so it's the only thing below the two columns.

Apply the same layout to both the add form and the edit form. Style the Connection group's border to match the existing card/group aesthetic on the dashboard (subtle low-contrast border, rounded corner, light padding).

## Notes
- Confirmed there is only one printer form in `settings.html`; it is reused for both add and edit (the edit flow just populates fields and changes the title/button text). No separate edit template/snippet — single layout change covers both flows.
- Reorganized form into two columns:
  - Left column (`.form-col-left`): Name, Sort (with help text below), Camera Port `<select>`, Enable camera stream toggle (with help text below).
  - Right column: `<fieldset class="connection-group">` with `<legend>Connection</legend>` containing IP Address, Access Code, Serial Number, Test Connection button, and the existing `#test-result` paragraph.
  - Submit button moved into a full-width `.form-submit-row` below both columns.
- Field IDs/names left untouched (`printer-name`, `printer-ip`, `printer-code`, `printer-serial`, `printer-camera-port`, `printer-camera-enabled`, `printer-sort`, `edit-id`, `test-btn`, `save-btn`, `test-result`) so existing JS submit/edit/test handlers continue to work.
- Added CSS for `.printer-form` (CSS Grid `1fr 1fr`, gap 1.5rem), `.form-submit-row` (spans both columns, full-width button), `.connection-group` border (`1px solid rgba(255,255,255,0.15)` with `5px` radius — matches existing `.filament-group` aesthetic), and a `<legend>` style. Stacks to single column at `max-width: 760px`.
- Bumped cache buster from `?v=34` → `?v=35` for both `style.css` and `app.js` in `base.html`.
- Sanity check: started server (port already taken by running instance, expected) and curled `/settings` against the live one — `HTTP 200`, response contains the new `connection-group`, `form-submit-row`, and `form-col` markers and references `style.css?v=35`. Template renders without errors.

## Questions
