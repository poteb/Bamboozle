# Dev Mode Hint On Unconfirmed

## Status
done  <!-- todo | in progress | blocked | done -->

## Severity
minor  <!-- trivial | minor | major — set at creation; may be updated later (see Severity) -->

## Task
The user keeps Developer Mode OFF on the H2C and P2S on purpose (2026-10-25): they want Bambu cloud and Handy. So the permanent "Developer Mode off: print controls … may be ignored" card line is noise for them.

Show the Developer Mode hint only when it's relevant: after a print control (pause/resume/stop/speed; never the light) sent from Bamboozle to a printer reporting Developer Mode off was NOT confirmed by a state change, or was refused by the printer. `replace-bambulabs-api` added state-verified commands (`commands.py`, with `print_control` and `confirmed`). Use that outcome.

- Hidden by default, even when the `fun` bit says Developer Mode is off.
- When a print control goes unconfirmed or refused on such a printer, show the hint on that card, with its existing fix-it text, until a later print control on that printer IS confirmed, or the app restarts. Make that "remember" period explicit in code.
- The "not confirmed" toast for such a printer should mention Developer Mode briefly (it may already do this; check it).
- Printers that don't report `fun` (A1, P1S): no change.

## Grilling
resolved  <!-- n/a (trivial) | pending | resolved — resolved decisions listed below -->

Decided in the main conversation (2026-10-25). The user won't enable Developer Mode, and said "yes" to queuing this follow-up.
1. Hide the hint unless a print control actually went unconfirmed or refused on a Developer-Mode-off printer.
2. The hint stays until a print control is confirmed on that printer, or the app restarts. Kept in memory only, not persisted.
3. No live command tests against the real printers: unit and JS tests only.

## Notes
- Worktree `D:\git\worktrees\Bamboozle-dev-mode-hint-on-unconfirmed`, branch `dev-mode-hint-on-unconfirmed` off `bambuddy-improvements`. TDD: `tests/test_dev_mode_hint.py` (14 red) and updated `tests/js/card_notice.test.mjs` (2 red), then green.
- Design:
  - The server decides. `PrinterConnection.print_control_unconfirmed` (memory only) → `PrinterState.print_control_unconfirmed`. The "remember" period is documented on the attribute.
  - Set in `run_command` when a print control (`Command.print_control`: pause/resume/stop/speed; not the light or Refresh) ends unconfirmed or refused while `state.developer_mode is False`. That is exactly the case where the toast already appends "(Developer Mode is off: the printer may ignore print controls)", so the toast already mentions it; no text change.
  - Cleared only when a later print control is confirmed **by a state change**. A command "confirmed" only because the printer was already in that state (`_Waiter.already_in_state`) proves nothing, so it keeps the hint. A confirmed light/Refresh does not clear it.
  - Kept across reconnects (same connection object). `update_printer` (edits, IP auto-relocation) carries it to the new connection when the serial is unchanged (`PrinterConnection.carry_over_from`); a different serial starts clean. App restart forgets it (not in `PrinterConfig`).
  - `execute_command` now pushes the card for any sent command (`confirmed is not None`), so the hint appears with the toast, not at the next poll.
  - JS `cardNotice`: hint needs `online && developer_mode === false && print_control_unconfirmed`. So no-`fun` printers (A1/P1S) and a printer whose Developer Mode was since turned on show nothing. Summary text unchanged. The details body now starts "A print control from Bamboozle was not confirmed, and the printer reports Developer Mode off." to say why it appeared; the fix-it text is unchanged.
  - CLAUDE.md: own "Developer Mode hint" paragraph; the push wording under Commands is updated.
  - Documented edge cases, not handled: the flag isn't cleared if Developer Mode is later turned on (the card hides it anyway; matches Grilling 2). With overlapping print controls, the one that finishes last decides.
- Reviews:
  - Self-review (`code-review` skill; Standards + Spec opus agents).
    - Standards: no hard violations. Fixed: `already` → `already_in_state`, `dev_mode_off` → `print_control_while_dev_off`, feature envy → `carry_over_from`, the rule is described once (the `PrinterConnection` comment; others point to it), own CLAUDE.md paragraph, merged the overlapping JS test. Not done: a `same_serial` helper (the pattern is mostly pre-existing) and moving shared test helpers to `fakes.py` (optional).
    - Spec: nothing missing, no bugs. It flagged as worth checking: clearing only on a state-change confirmation (stricter than the wording; kept, since an already-matching state proves nothing), the same-serial carry-over, the push after unconfirmed commands, and the new opening sentence in the details body. All kept.
  - Independent opus review: no P1/P2. P3s fixed: CLAUDE.md push wording, a reconnect test (`reset_session`), tests for a refusal on a Developer-Mode-on / no-`fun` printer and a command cut short by an edit (neither sets the flag), and edge cases documented on the attribute.
  - ChatGPT/Loom: not triggered. No P1/P2, and nothing touches money, auth, security, privacy, data loss or webhooks.
  - Reviewer results were relayed through the main conversation.
- Commits: `c62e5d3`, `b670e6d`, `9106547`. Merged into `bambuddy-improvements` as `7557acd`. After the merge: pytest 684 passed, node 81 passed. Worktree removed, branch deleted. No live tests (Grilling 3).

## Questions
