# Ams Headline In Detail View

## Status
done  <!-- todo | in progress | blocked | done -->

## Task
In the expanded (detailed, vertical) filament view only:
- Show a small headline at the top of each filament group naming the AMS unit (e.g. `AMS 1`, `AMS 2`, `External`).
- Remove the location segment (`AMS N · Slot M` / `External spool`) from each per-filament label, since the headline already conveys it. Per-filament text keeps just `{color name} · {filament type}`.
- Step the per-filament label font size down again (currently `0.75rem` — go a notch smaller).

The compact (horizontal) view and its hover `title` tooltip stay as they are: full text including location.

## Notes
- `app.js`: added `_filamentLabelExpanded(f)` (mirrors `_filamentTooltip` but skips the location segment). `_filamentTooltip` is unchanged and still feeds the compact swatch's `title`.
- `app.js`: `_renderFilaments` now buckets groups as `{ kind, amsIndex?, items }`. In the expanded branch it prepends a `.filament-group-header` showing `AMS N` (1-based) or `External`, and uses `_filamentLabelExpanded` for the per-row label. Compact branch still emits bare swatches.
- `style.css`: added `.filament-group-header` (font-size 0.7rem, opacity 0.6, uppercase, slight letter-spacing, small bottom margin) plus a `.filaments-row:not(.expanded) .filament-group-header { display:none; }` safety rule. The header isn't emitted in compact mode anyway, but the rule guards future tweaks.
- `style.css`: `.filament-label` font-size 0.75rem -> 0.7rem (matches the header so the visual hierarchy comes from opacity, not size). Bumped line-height 1.2 -> 1.4 to keep the text vertically centered against the 20px swatch.
- `base.html`: cache buster v30 -> v31 on both `style.css` and `app.js`.
- Compact view diff: only change is groups now containing a header `<div>` -- but that header is hidden by CSS in non-expanded mode AND not emitted in non-expanded mode (double safety). No compact-only rule was touched.

## Questions
