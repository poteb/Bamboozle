# Fix H2C Filaments Missing

## Status
done  <!-- todo | in progress | blocked | done -->

## Task
The dashboard shows no filament squares for the H2C printer, even though other printer models render their filaments correctly. Investigate why `ams_hub()` / `vt_tray()` returns no data for H2C and fix it. The H2C is a multi-extruder model and likely emits a different MQTT payload shape that the current code (`PrinterConnection._read_filaments` in `src/bamboozle/printer_manager.py`) doesn't handle.

## Notes
- Investigated `bambulabs_api` 1.x source (`mqtt_client.py`, `ams.py`, `filament_info.py`). Two H2C-blocking issues confirmed:
  - `vt_tray()` calls `__get_print("vt_tray")` and pipes it through `FilamentTray.from_dict`. On dual-extruder models the payload key is `vir_slot` (a list, ids 254 / 253) instead, so `vt_tray()` either returns empty or raises.
  - `process_ams()` only keeps trays where `tray.get("n", None)` is truthy. H2C/H2D AMS trays don't include the legacy `n` field, so every loaded tray is silently dropped.
- Cross-referenced the ha-bambulab integration source — same conclusion. They fall back to `vir_slot` for H2C/H2D and use a different empty detection (no `n` filter on AMS trays).
- Fix: added `_read_filaments_from_dump` that parses `printer.mqtt_dump()["print"]` directly. It walks `ams.ams[].tray[]` (no `n` filter), then `vir_slot` (multi-extruder external spools, indexed by ams_index 0/1) or `vt_tray` (single-extruder fallback).
- Kept the original `ams_hub()` / `vt_tray()` path as `_read_filaments_from_library`, used as a fallback if the dump has no `print` block yet (e.g. before pushall arrives) — protects all the previously-working models.
- Tightened `_normalize_tray_color` to treat all-zero hex (`00000000`, `000000`) as no-color so empty/unidentified slots are skipped (the firmware's reset value).
- Verified by running synthetic payloads through `_read_filaments`:
  - H2C (vir_slot + AMS without `n`): 2 AMS + 2 external returned, indices preserved.
  - H2C with no AMS attached: 2 external returned.
  - Legacy P1S shape via dump (vt_tray + AMS with `n`): 1 AMS + 1 external.
  - Empty dump: falls back to library helpers, returns library data.
  - Empty `00000000` color skipped.
- Couldn't run against an actual H2C — the assumed payload shape comes from ha-bambulab's documented `vir_slot` example. If a real H2C dump shows a different field name or extra wrapping (`vir_slots`, etc.), the parser needs another pass; current code degrades to empty list rather than raising.
- Files touched: `src/bamboozle/printer_manager.py` (only).
- Backend-only change; no template/static cache buster bump needed.

## Questions

## Questions
