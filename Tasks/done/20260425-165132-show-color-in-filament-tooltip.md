# Show Color In Filament Tooltip

## Status
done  <!-- todo | in progress | blocked | done -->

## Task
Include the filament color (the hex string, e.g. `#FF8800`) in the mouse-over tooltip on each filament square, alongside the existing AMS slot / external label and filament name.

## Notes
- Updated `_filamentTooltip(f)` in `src/bamboozle/static/app.js`: changed the `else if (f.color)` fallback to an unconditional `if (f.color) parts.push(f.color)` so the hex appears alongside (not instead of) the name/type. Empty colors are still skipped.
- Bumped cache buster from `?v=21` to `?v=22` for both `style.css` and `app.js` in `src/bamboozle/templates/base.html` so browsers reload the JS.
- Tooltips now read e.g. `AMS 1 · Slot 2 · PLA Basic · #FF8800` or `External spool · PETG HF · #00AA88`. Color hex passed through verbatim (preserves any alpha channel from firmware).

## Questions
