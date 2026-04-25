# Show Filaments On Cards

## Status
done  <!-- todo | in progress | blocked | done -->

## Task
Fetch filament information from each printer (AMS units and external spool) and display the filaments at the bottom of each printer card on the dashboard. Render each filament as a 20x20 px square (use the filament color) with the filament name shown as a tooltip on mouseover.

## Notes
- API discovery: `bambulabs_api.Printer` exposes `ams_hub()` (returns `AMSHub` with `.ams_hub: dict[int, AMS]`, each AMS has `.filament_trays: dict[int, FilamentTray]`) and `vt_tray()` (returns a single `FilamentTray` for the external/virtual spool). Both are sync, so they go through `_read_sync` like other reads.
- `FilamentTray` fields used: `tray_color` (Bambu firmware emits 8-char `RRGGBBAA` hex per `mqtt_client.set_printer_filament` which appends `FF`), `tray_type` ("PLA", "PETG", ...), `tray_sub_brands` (e.g. "PLA Basic"), `tray_id_name` (fallback name).
- Color rendering: emit `#RRGGBBAA` (browsers accept 8-digit hex). 6-digit also accepted. Invalid/missing → empty string → CSS `transparent` square (still bordered so it shows up).
- Tooltip format: `"AMS 1 · Slot 2 · PLA Basic"` for AMS slots, `"External spool · PLA Basic"` for vt_tray. Indices are shown 1-based to humans, 0-based in the data model.
- Empty-slot handling: a slot with no color, no type, and no name is skipped. `vt_tray` always returns a tray object even when no external spool is loaded; same skip logic applies. If no AMS and no external spool, the row is omitted entirely.
- Visual distinction for external spool: same 20x20 square but a dashed white border, vs solid hairline border for AMS slots.
- Pre-existing `AMSTray` model + `ams_trays` field were imported but never populated. Replaced with `Filament` model + `filaments` field. JS rendering for the old `ams-row` was also unused (never triggered) and is removed.
- `updateCard` previously had no path to update AMS rows on live state changes — added one alongside the new initial render path.
- Verified by running `from bamboozle.main import app` from `src/` with the project's Python — imports cleanly, no startup errors. No real printer available to validate live data; defensive try/except wrappers around every getter follow the existing `_read_sync` pattern so failures fall back to an empty list.

Files touched:
- `src/bamboozle/models.py` — replaced `AMSTray` with `Filament` (`source`, `ams_index`, `tray_index`, `color`, `filament_type`, `name`); replaced `ams_trays` with `filaments` on `PrinterState`.
- `src/bamboozle/printer_manager.py` — import update; added `_read_filaments`, `_normalize_tray_color`, `_filament_label`; populate `filaments` in the `PrinterState` returned by `_read_sync`.
- `src/bamboozle/static/app.js` — new `_renderFilaments` and `_filamentTooltip`; replaced `ams_trays` rendering in `renderCard`; added live update path in `updateCard`.
- `src/bamboozle/static/style.css` — replaced `.ams-row`/`.ams-tray` with `.filaments-row`/`.filament-square`; 20x20 squares with hairline border, dashed border for external spool.

## Questions
