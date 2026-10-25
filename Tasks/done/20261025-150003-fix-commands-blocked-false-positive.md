# Fix Commands Blocked False Positive

## Status
done  <!-- todo | in progress | blocked | done -->

## Severity
minor  <!-- trivial | minor | major — set at creation; may be updated later (see Severity) -->

## Task
`commands-blocked-auth-errors` (merged into `bambuddy-improvements`) marks the H2C and P2S as "refusing control commands". It goes by the `fun` Developer-Mode-off bit, plus HMS `0500050000010007` on printers without `fun`. But the user confirmed on 2026-10-25 that light on/off from the current version (main) **works** on both. So that signal does not mean "all commands refused", and on the integration branch `execute_command` now wrongly reports light commands on those printers as failed.

Fix it so Bamboozle never reports a working command as failed:
- `execute_command` must stop turning a successful publish into a failure based on `commands_blocked`, for every action.
- Keep the information: Developer Mode off is still worth showing. Rename/reword it as a fact, not a claim about all commands. For example, a quiet card hint "Developer Mode off — some print controls (pause/resume/stop) may be refused", with the fix-it text in a tooltip or detail view. Reconsider whether the HMS-only fallback should drive the hint at all; it's stale evidence.
- Re-check the research doc (§1 on Developer Mode) for which commands the firmware actually gates. Record what's known vs unknown in Notes. Real proof that a command took effect is planned for `replace-bambulabs-api` (watch for the state change), so don't build that here.
- Update tests, CLAUDE.md, and the notice text in `protocol.py` / `app.js`.

## Grilling
resolved  <!-- n/a (trivial) | pending | resolved — resolved decisions listed below -->

Decided in the main conversation (2026-10-25), from the user's report that the light works on H2C/P2S:
1. Never fail a command because of `commands_blocked` / Developer Mode detection; the publish result alone decides.
2. Show Developer Mode off only as a soft, informational hint naming the print controls, not as "refusing commands".
3. No live command tests against the real printers (no light toggles, no pause): unit tests only.

## Notes
- Worktree `D:\git\worktrees\Bamboozle-fix-commands-blocked-false-positive`, branch `fix-commands-blocked-false-positive` off `bambuddy-improvements`. TDD: tests red first (8 Python failures on the old override), then green.
- Design decisions:
  - `execute_command` returns the `_run_command` result unchanged for every action (Grilling 1). Removed `PrinterState.commands_blocked`, `protocol.commands_blocked`, `COMMANDS_BLOCKED_MESSAGE`, `COMMAND_VERIFY_FAILED_HMS` and `PrinterManager.QUERY_ACTIONS` (only existed to exempt refresh from the override).
  - The hint is driven by `developer_mode is False` (the live `fun` bit) only. The HMS-only fallback is dropped: HMS `0500050000010007` records a past refusal, and the H2C/P2S carry it while the light works. Unknown (A1/P1, no `fun`) shows nothing. `protocol.hms_codes` is kept (tested; no caller) for `hms-error-decoding`.
  - Card hint (Grilling 2): `cardNotice` returns `{text, title, quiet: true}`. It renders as a quiet (small, dimmed, no warning colours) `<details>` inside `.card-notice`. The summary is "Developer Mode off: print controls (pause, resume, stop, speed) may be ignored". The body holds the fix: "The printer reports Developer Mode off. Its firmware may silently ignore print controls from LAN apps (the light works on some models). To allow them, enable LAN Only + Developer Mode on the printer, then restart it. This changes the access code: update it in Settings."
    - Used a `<details>` instead of a tooltip, so keyboard and touch users can reach the fix.
    - "Ignored" instead of "refused", because the printer drops commands silently.
    - Speed is listed as a print control, but whether the firmware gates it is unconfirmed (see below).
  - `applyNotice(el, notice)` replaces the content only when the notice changes (`data-key`), so an open details survives the 3 s poll (checked live).
  - `esc()` now also escapes `"` (it's used in attributes).
  - CLAUDE.md is updated. The research doc §1 gets a dated correction, and the TL;DR / A1 lines are annotated.
- **Known vs unknown: which commands the firmware gates with Developer Mode off.**
  - Known: queries (pushall, get_version) work. Per the user (2026-10-25), the `ledctrl` chamber-light toggles work on the H2C and P2S, both of which report `fun` bit 0x20000000 set and HMS 0500050000010007.
  - Claimed but unverified: BamBuddy says "every control command is silently dropped" and that pause/print do nothing. That's disproved for the light, and unconfirmed for pause/resume/stop/speed on our printers.
  - Unknown:
    - which print commands (pause/resume/stop, `print_speed`, project_file/print start, AMS, gcode_line) are dropped;
    - whether it varies by model or firmware;
    - what the HMS was raised by;
    - A1/P1 behaviour (no `fun`).
  - No live tests (Grilling 3). Real proof that a command took effect is left to `replace-bambulabs-api`, which will watch for the state change.
- Visual check: my own instance on port 8093, using a temp LOCALAPPDATA with a copy of the config, `webbrowser` patched, and headless Edge via CDP.
  - The H2C and P2S cards show the collapsed hint; A1/P1S show none.
  - Opened, the hint shows the fix, and it stayed open across poll updates.
  - The temp files were deleted and the 8080 app was untouched. Only the app's normal connect/pushall traffic was sent; no commands.
- Reviews:
  - Self-review (`code-review` skill, Standards + Spec opus agents): no hard violations. Fixed:
    - Notes were empty (filled here).
    - The `hms_codes` caller is now documented.
    - The wording now says "some models".
    - Dropped the redundant `commands_blocked`-alone JS test.
  - Rejected:
    - Deduplicating the H2C/P2S rationale comments (each is short, and the code comment now points to `protocol.DEV_MODE_OFF_BIT`).
    - A `level` string instead of `quiet` (only two levels).
    - Listing speed: kept, as a print control.
  - Independent opus review: no P1. Two P2s:
    - The fix text was hover-only → changed to `<details>`.
    - Empty Notes → filled.
    - P3s fixed: "ignored" wording, research doc annotations, a test of the class toggle (`applyNotice` tests).
  - ChatGPT/Loom step: triggered (P2s found), but the project has no ChatGPT/Loom channel. A second fresh opus re-review of `f22d507` ran instead: no P1/P2.
    - P3s fixed: `esc` escapes quotes, `aria-live="off"` on the detail body, a focus outline and full opacity when open/focused, wording aligned.
    - P3s rejected:
      - A Safari < 18.4 marker fallback (Windows user).
      - Renaming `title` → `detail`.
      - A render/applyNotice key round-trip test (the reviewer verified it by hand).
- Commits: `badf3bd`, `f22d507`, `547d6a2`. Merged into `bambuddy-improvements` as `4e65e24`. After the merge: pytest 221 passed, node 42 passed. Worktree removed, branch deleted.

## Questions
