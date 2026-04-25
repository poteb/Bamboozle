# Hide Form Until New Or Edit

## Status
done  <!-- todo | in progress | blocked | done -->

## Task
On the Printer Settings page, hide the Add/Edit form by default. Show it only when the user clicks New (Add mode) or a printer in the sidebar (Edit mode). Clicking New on an already-shown form keeps it visible. The right pane can show a small placeholder ("Select a printer or click New" or similar) when nothing is selected.

## Notes
- `src/bamboozle/templates/settings.html`: split the right pane into two `<article>`s — `#form-placeholder` (visible by default, copy: "Select a printer or click New to begin.") and `#form-pane` (carries `hidden` initially, wraps the existing `<header>` + form unchanged).
- Added a `showForm()` JS helper that sets `placeholder.hidden = true` and `pane.hidden = false`. Called from both `newPrinter()` and `editPrinter()` at the end so they reveal the form. Field IDs, function names, and submit/delete handlers are unchanged.
- Reload after submit/delete already exists (`window.location.reload()`), so post-action the page returns to the placeholder-visible initial state automatically.
- `src/bamboozle/static/style.css`: added a tiny `.form-placeholder p` rule (margin 0, opacity 0.65, slightly smaller font) to keep the placeholder quiet inside Pico's default `<article>` framing.
- `src/bamboozle/templates/base.html`: bumped `style.css` and `app.js` cache busters from `v=37` to `v=38`.
- Verified by curling `http://127.0.0.1:8080/settings` from a backgrounded server: response contains `<article id="form-placeholder" class="form-placeholder">…</article>` followed by `<article id="form-pane" hidden>…</article>`, and the stylesheet/script tags reference `?v=38`.

## Questions
