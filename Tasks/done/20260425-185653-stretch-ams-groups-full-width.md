# Stretch Ams Groups Full Width

## Status
done  <!-- todo | in progress | blocked | done -->

## Task
In the expanded (detailed) filament view, each `.filament-group` box currently sizes to its content's natural width, leaving empty space to its right. Make each group stretch to fill the full width of `.filaments-row.expanded` so the groups all line up at the same right edge of the card. Compact view stays unchanged.

## Notes
- `.filaments-row.expanded` had `align-items: flex-start;` which made flex children (the `.filament-group` boxes) shrink to content width. Changed to `align-items: stretch;` so each group fills the row width. That was the entire fix.
- `.filament-group` itself had no `width` rule, so no compact-vs-expanded scoping was needed.
- Compact view (`.filaments-row:not(.expanded)`) untouched — still uses `flex-wrap: wrap` and `justify-content: center` so groups sit side-by-side at natural width.
- Bumped cache busters in `src/bamboozle/templates/base.html` from `?v=31` → `?v=32` for both `style.css` and `app.js`.

## Questions
