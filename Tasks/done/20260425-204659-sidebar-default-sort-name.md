# Sidebar Default Sort Name

## Status
done  <!-- todo | in progress | blocked | done -->

## Task
The settings sidebar currently sorts printers by `(sort, name)` (the printer's `sort` integer first, name as tie-breaker). Change it to sort by name only — alphabetical, case-insensitive. The `sort` integer is for the dashboard view; the settings sidebar should be a stable alphabetical list. Update the sort in `src/bamboozle/routers/pages.py` (the settings handler).

## Notes
- Updated `src/bamboozle/routers/pages.py` settings handler: `sorted(..., key=lambda p: p.name.lower())`. Dashboard handler untouched.
- Verified `from bamboozle.routers.pages import router` imports cleanly via the Python Manager 3.14 interpreter from `src/`.
- Note: the on-disk HEAD version of the file actually had no `sorted(...)` call at all (just `printers = manager.config.printers`), not the `(p.sort, p.name.lower())` key described in the task brief. End state still matches the requested behavior — alphabetical, case-insensitive, name-only.

## Questions
