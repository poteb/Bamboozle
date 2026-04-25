# Printer Config History Backups

## Status
done  <!-- todo | in progress | blocked | done -->

## Task
Whenever a printer is updated or deleted, snapshot the **previous** `PrinterConfig` to a per-printer JSON backup file before applying the change.

- **Location**: `%LOCALAPPDATA%\Bamboozle\history\` (alongside `config.json`). Create the folder lazily on first write.
- **Filename**: `{printername}.{YYYYMMDD-HHMMSS}.json` using a UTC timestamp including seconds. Sanitize the printer name for filesystem safety (replace any character that isn't alphanumeric / dash / underscore with `_`).
- **Hook points**: `PrinterManager.update_printer` and `PrinterManager.remove_printer` in `src/bamboozle/printer_manager.py`. Snapshot the existing config from `self._connections[printer_id].cfg` (or the corresponding entry in `self._config.printers`) before mutating it.
- **Format**: dump the old `PrinterConfig` to JSON via `model_dump_json(indent=2)` so the file is human-readable.

No UI changes required — just persistence on the backend.

## Notes
- Added `_backup_printer_config(cfg)` helper in `src/bamboozle/printer_manager.py` (top-level, just under the imports). Reuses `_config_dir()` from `config.py` so the history folder always sits next to `config.json`.
- Folder created lazily with `Path.mkdir(parents=True, exist_ok=True)`; filename is `{sanitized_name}.{YYYYMMDD-HHMMSS}.json` (UTC, second precision). Sanitization: `re.sub(r"[^A-Za-z0-9_-]", "_", cfg.name) or cfg.id`.
- Hooked into `PrinterManager.update_printer` and `PrinterManager.remove_printer` after the `if not conn: return False` guard and before `await conn.disconnect()` — so backups never run for a non-existent printer and always run when the operation will actually proceed. `add_printer` left untouched.
- Wrapped the helper call in try/except in both methods; backup failure logs a warning and continues so the update/delete operation isn't broken by I/O issues. Successful backup logs the path at INFO.
- Pydantic dump uses `cfg.model_dump_json(indent=2)`, written UTF-8.
- Verification: ran the verify snippet from `src/` with Python 3.14. It produced `C:\Users\paw\AppData\Local\Bamboozle\history\Test_Printer.20260425-210246.json` (slash in `Test/Printer` correctly sanitized to `_`); content matched the dumped PrinterConfig. Test file deleted afterwards.

## Questions
