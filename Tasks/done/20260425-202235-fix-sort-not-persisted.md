# Fix Sort Not Persisted

## Status
done  <!-- todo | in progress | blocked | done -->

## Task
The new printer `sort` field added in the previous task is not being saved when editing an existing printer. Verify and fix the persistence path on both edit and add. Likely culprits in order: the `editPrinter(...)` JS submit handler in `src/bamboozle/templates/settings.html` (may not be sending `sort` in the request body), the `/api/printers/{id}` update endpoint in `src/bamboozle/routers/api.py` (may not be applying it to the stored config), and `PrinterManager.update_printer` in `src/bamboozle/printer_manager.py` (may not be threading it through).

Reproduce: edit a printer, change the Sort value, save, reload — value reverts.

## Notes
- Traced `sort` end-to-end: model schema (`PrinterConfig`, `PrinterAddRequest`, `PrinterUpdateRequest`) all carry it; settings.html form input id `printer-sort` is read into the JSON body in `handlePrinterSubmit` (`sort: parseInt(...) || 0`); `editPrinter(...)` prefills the field from `{{ p.sort }}`; `POST /api/printers` passes `body.sort` straight into `PrinterConfig`; `PUT /api/printers/{id}` uses `body.sort if body.sort is not None else existing.sort` (correct preserve-unset pattern); `PrinterManager.add_printer` / `update_printer` both call `save_config(self._config)`, and `save_config` writes `model_dump(mode="json")` which includes `sort`.
- Verified schema with pydantic: `PrinterUpdateRequest(sort=5).model_dump(exclude_unset=True) == {"sort": 5}` and `sort=0` likewise serializes to `{"sort": 0}`. Default `PrinterConfig` carries `sort=0`.
- Verified live server with FastAPI test client (in-process round-trip): POST with `sort=7` and PUT with `sort=12` and PUT with `sort=0` all persisted correctly via `manager.config.printers`.
- Verified live server end-to-end against the user's own config: started a fresh `bamboozle.main` instance, called `PUT /api/printers/4b99ae4da105` with `sort=7`, observed (a) `GET /api/printers` returns the new value, (b) `%LOCALAPPDATA%\Bamboozle\config.json` on disk now includes `"sort": 7`. Round-tripped back to 0 successfully.
- Inspected the rendered `/settings` HTML: each row's Edit button correctly receives the printer's current `sort` as the trailing arg to `editPrinter(...)`, and the input is prefilled accordingly.
- Conclusion: the persistence path is correct in the current source. The likely cause of the reported regression is that the user's running server was started before the previous "Add Printer Sort Field" task wired `sort` through the API/save path, so PUTs from the new UI hit a `PrinterUpdateRequest` schema that silently dropped the unknown `sort` field. Pre-kill, the running server returned no `sort` key in `/api/printers`, confirming it was stale.
- Resolution: no code change needed. After restarting the server (which happened during verification), the add and edit flows both persist `sort` correctly. Cache busters left at v=35 since nothing in `app.js` / `style.css` was touched.
- Side effect of verification: `config.json` on disk now has explicit `"sort": 0` keys for printers that previously omitted them — benign, identical pydantic round-trip.

## Questions
