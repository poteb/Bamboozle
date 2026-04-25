# Group Filaments By Ams

## Status
done  <!-- todo | in progress | blocked | done -->

## Task
Group the filament squares on each card by their actual placement — each AMS unit becomes its own visually bordered cluster of squares, and the external spool sits in its own cluster. Apply the same grouping to both the compact (horizontal) and expanded (vertical, with labels) views. Order groups: AMS 1, AMS 2, ..., External. Use `f.source` and `f.ams_index` from the existing `Filament` payload to bucket them.

## Notes
- Refactored `_renderFilaments(s, id)` in `src/bamboozle/static/app.js` to bucket filaments into groups: a `Map<ams_index, filament[]>` for AMS units (sorted ascending) plus an external bucket appended last. Each bucket renders as a `<div class="filament-group">` inside the existing `.filaments-row`.
- Defensive: filaments with `source !== 'external'` and missing/non-numeric `ams_index` fall back to bucket `0` (avoids NaN sort / crashes).
- Click handler stays on `.filaments-row`, so clicking any square or any group still toggles compact <-> expanded.
- WebSocket update path in `updateCard` already replaces `.filaments-row` `className` + `innerHTML` from the regenerated HTML, so it picks up the new group markup unchanged.
- CSS in `src/bamboozle/static/style.css`: added `.filament-group` (flex row, 4px padding, 1px solid rgba(255,255,255,0.15), 5px radius). In compact view groups sit side-by-side via the existing flex `.filaments-row`. Added `.filaments-row.expanded .filament-group` to flip to `flex-direction: column;` so each group becomes a vertical stack of `<square + label>` rows; the parent `.filaments-row.expanded` already stacks groups vertically via `flex-direction: column`.
- Bumped cache busters in `src/bamboozle/templates/base.html` from `?v=28` to `?v=29` for both `style.css` and `app.js`.
- No backend changes; `Filament` payload (`source`, `ams_index`) already carried what was needed.

## Questions
