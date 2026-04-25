# Add Printer Sort Field

## Status
done  <!-- todo | in progress | blocked | done -->

## Task
Add an integer `sort` field to each printer:

- **Model**: extend `PrinterConfig` (and the `PrinterAddRequest` / `PrinterUpdateRequest` schemas) with `sort: int = 0`. Persist via the existing config flow.
- **Settings UI**: surface it as a number input in the printer add/edit form on `settings.html`.
- **Dashboard sort**: add a `Sort` option to the sort-by dropdown. When selected, sort printers ascending by `sort` (use `name` as the tie-breaker for equal values). Honor the existing asc/desc toggle.

## Notes
- Added `sort: int = 0` to `PrinterConfig`, `PrinterAddRequest`, and `PrinterState`; added `sort: int | None = None` to `PrinterUpdateRequest` (`src/bamboozle/models.py`).
- Wired `sort` through both add and update endpoints in `src/bamboozle/routers/api.py` (update preserves existing value when body.sort is `None`).
- Pulled `cfg.sort` into the initial `PrinterState` in `PrinterConnection.__init__` and the per-poll state built in `_read_sync` (`src/bamboozle/printer_manager.py`).
- Settings UI: added a number input ("Sort", help text, step 1, default 0) below the camera-stream toggle, populated on edit, included in the form submit as `parseInt(...)`. The inline `editPrinter(...)` button signature gained a trailing `sort` arg (`src/bamboozle/templates/settings.html`).
- Dashboard sort: added `<option value="sort">Sort</option>` at the top of `#sort-by` (`src/bamboozle/templates/dashboard.html`) and a `case 'sort'` to `_sortedPrinters()` that compares `(a.sort ?? 0) - (b.sort ?? 0)` with `name` localeCompare as the equal-value tie-breaker, still respecting the asc/desc multiplier (`src/bamboozle/static/app.js`).
- Bumped cache busters `style.css` / `app.js` from `?v=33` to `?v=34` in `src/bamboozle/templates/base.html`.
- Verification: `PrinterConfig(name='x', ip='1.2.3.4', access_code='a', serial='s')` reports `sort=0` and dumps the field; legacy dict without `sort` loads with default 0; `from bamboozle.printer_manager import PrinterConnection` imports cleanly.

## Questions
