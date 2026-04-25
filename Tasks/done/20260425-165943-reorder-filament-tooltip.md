# Reorder Filament Tooltip

## Status
done  <!-- todo | in progress | blocked | done -->

## Task
Reorder the filament tooltip / inline label so the color comes first, followed by filament type, then location: `{color} · {filament type} · {location}`. Example: `Sunflower Yellow · PLA Basic · AMS 1 Slot 2`. Update `_filamentTooltip` in `src/bamboozle/static/app.js` and bump the cache buster in `base.html`.

## Notes
- Reordered `_filamentTooltip(f)` in `src/bamboozle/static/app.js` so parts are pushed in color → filament-type → location order. Color uses `colorNameFromHex(f.color) || f.color`; type uses `f.name || f.filament_type`; location is `External spool` or `AMS {n} · Slot {m}`. Each slot is skipped when its source is empty so we never emit a leading or doubled `·`.
- Bumped cache busters on both `style.css` and `app.js` from `?v=24` to `?v=25` in `src/bamboozle/templates/base.html`.
- Verification by inspection of the new function:
  - Known hex `#FFC72C` + `PLA Basic` + AMS slot 1/2 → `Sunflower Yellow · PLA Basic · AMS 1 · Slot 2`.
  - Unknown hex `#AB12CD` + `PETG` + external → `#AB12CD · PETG · External spool`.
  - Empty color + `PLA Basic` + AMS slot 1/2 → `PLA Basic · AMS 1 · Slot 2` (no leading separator).
- `_renderFilaments` and the expanded inline label both consume `_filamentTooltip`, so no other call sites needed changes.

## Questions
