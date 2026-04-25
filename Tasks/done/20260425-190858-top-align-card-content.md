# Top Align Card Content

## Status
done  <!-- todo | in progress | blocked | done -->

## Task
When one printer card on a row is taller than another (e.g. one has the expanded filament view open and another doesn't, or one has a much longer job description), the shorter card's content collapses to the bottom and leaves a large empty gap below the camera/thumbnail. Make all card content stay top-aligned instead — extra height should appear as empty space at the bottom of the card, below everything.

The likely fix is the `margin-top: auto` on `.printer-card .card-bottom` in `src/bamboozle/static/style.css`, which currently pushes the bottom block down to fill space.

## Notes
- Removed the `.printer-card .card-bottom { margin-top: auto; }` rule in `src/bamboozle/static/style.css`. Card content now stacks naturally top-to-bottom; on shorter cards, spare height becomes empty space below the action row instead of an interior gap.
- Left `align-items: stretch` on `.printer-grid` untouched per the task constraint (rows still match height).
- Bumped cache busters in `src/bamboozle/templates/base.html` for `style.css` and `app.js` from `?v=32` → `?v=33`.

## Questions
