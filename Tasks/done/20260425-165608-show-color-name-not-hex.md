# Show Color Name Not Hex

## Task
In the filament tooltip / inline label, replace the raw hex color (e.g. `#FFE600`) with a human-readable color name (e.g. `Sunflower Yellow`). The user has Bambu-branded filaments in mind; the names should match Bambu's official catalog where possible. If the exact hex isn't in the catalog, the sub-agent should pick a reasonable approach (nearest-match, or fall back to hex) and document the choice in `## Notes`.

## Status
done  <!-- todo | in progress | blocked | done -->

## Notes
- Implementation: frontend-only. Kept backend `Filament` model untouched.
- Added a `BAMBU_COLOR_NAMES` lookup table at the top of `src/bamboozle/static/app.js` covering Bambu Lab's PLA Basic, PLA Matte, PETG HF, ABS, and Silk/Galaxy/Sparkle palettes. Names sourced from BambuStudio's open-source preset profiles (BBL filament JSONs in `BambuStudio/resources/profiles/BBL/filament/`); the slicer code is AGPL but the name/hex pairs themselves are factual data.
- Added two helpers: `normalizeHex(color)` strips `#`, drops the alpha pair from `#RRGGBBAA` to `#RRGGBB`, returns uppercased 6-digit hex or null; `colorNameFromHex(color)` looks the normalized hex up in the catalog. Both case-insensitive.
- Decision: exact-match only with hex fallback, as the task brief specified. No nearest-color match to avoid mislabeling slightly-off custom filaments.
- Updated `_filamentTooltip(f)` to push `colorNameFromHex(f.color) || f.color` instead of the raw hex; empty `f.color` still skips the segment.
- Bumped `style.css` and `app.js` cache buster from `?v=23` to `?v=24` in `src/bamboozle/templates/base.html`.
- Sanity-checked JS with `node --check`, no syntax errors.
- Touched files: `src/bamboozle/static/app.js`, `src/bamboozle/templates/base.html`.

## Questions
