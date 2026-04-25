# Shrink Filament Detail Text

## Status
done  <!-- todo | in progress | blocked | done -->

## Task
Reduce the font size of the inline label in the expanded (vertical) filament view a bit. Target the `.filament-label` rule in `src/bamboozle/static/style.css` and step the size down one notch (e.g. ~0.8rem or whatever fits the rest of the card's small-text scale).

## Notes
- `.filament-label` was at `0.8rem`; dropped to `0.75rem` to match the `.temp-label` small-text scale already used on the card (uppercase temp labels are also `0.75rem`).
- Added `line-height: 1.2` to `.filament-label`. The row uses `align-items: center` with a 20px swatch, so the swatch dictates row height and the text centers regardless. The explicit line-height just prevents any inherited body line-height from making the label visually offset when wrapped narrowly.
- No other rules touched. Squares, borders, group layout, compact-view spacing untouched.
- Bumped `?v=29` -> `?v=30` for both `style.css` and `app.js` in `src/bamboozle/templates/base.html`.

## Questions
