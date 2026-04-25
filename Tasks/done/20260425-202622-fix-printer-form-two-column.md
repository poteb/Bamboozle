# Fix Printer Form Two Column

## Status
done  <!-- todo | in progress | blocked | done -->

## Task
The previous layout task (`20260425-202014-revamp-printer-form-layout`) intended a two-column form (Name/Sort/Camera Port/Enable-toggle on the left; Connection group on the right with Test Connection inside). What actually rendered (`D:\s2.png`) is single-column: Connection sits **below** the camera fields, not next to them.

Fix the layout so it renders as two columns at desktop widths. Likely cause: the form children aren't grouped into left-column and right-column wrappers — the CSS grid is laying out individual fields rather than two column blocks. Wrap the left-column fields in one element and the Connection fieldset in another, so the grid has exactly two children.

Verify the fix by reading the rendered HTML and confirming both column wrappers exist; the user is on a desktop-width browser so the `@media (max-width: 760px)` stack rule should not be triggering here.

## Notes
- On inspection, the wrappers (`.form-col-left`, `.form-col-right`, `.form-submit-row`) and the grid CSS were already in place from the prior task. User confirmed in chat that the layout now looks correct, so no code change needed.

## Questions
