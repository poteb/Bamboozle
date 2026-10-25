# Hms Error Decoding

## Status
done  <!-- todo | in progress | blocked | done -->

## Severity
minor  <!-- trivial | minor | major — set at creation; may be updated later (see Severity) -->

## Task
Research item C2 (doc §5, TL;DR #7).
- Decode HMS and `print_error` codes into readable text on the printer card, ignoring the errors a normal user cancel produces.
- Error text: link each code to Bambu's wiki first. Don't copy BamBuddy's or Bambu Studio's AGPL tables. Bundled text is OK only from a licence-compatible source.
- Pure decoding functions over the raw report dict, as in temps-stage-status-fields.

## Grilling
resolved  <!-- n/a (trivial) | pending | resolved — resolved decisions listed below -->

The user delegated the design decisions on 2026-10-25 ("Do A, B, C, 14 and proceed to fix"):
- Follow the recommendations in `docs/research/bambuddy.md` (committed on branch `bambuddy-improvements`).
- Record each design decision you make in `## Notes`.
- Block only on a choice that is genuinely the user's (e.g. a visible UX trade-off the research doesn't settle).
- BamBuddy is AGPL-3.0: reimplement ideas, never copy its code.

## Notes
- 2026-10-25: worktree `D:/git/worktrees/Bamboozle-hms-error-decoding`, branch `hms-error-decoding` from `bambuddy-improvements`. Severity minor; TDD (pytest + node tests first).
- **Licence check (decision: link only, bundle no Bambu text).**
  - Bambu wiki pages (`wiki.bambulab.com/en/hms/error-code`, `/en/x1/troubleshooting/hmscode/...`) carry page licence `"alr"` (all rights reserved) in their page data. Not bundleable.
  - Bambu Studio `resources/hms` and BamBuddy's catalogue: AGPL-3.0. Not bundleable in MIT Bamboozle.
  - ha-bambulab (greghesp/ha-bambulab): repo is MIT, but its `hms_error_text/*.json.gz` is downloaded by `scripts/update_error_text.py` from Bambu's `e.bambulab.com/query.php` catalogue. So it's Bambu's text, not covered by ha-bambulab's MIT grant. Not bundleable either.
  - So: no third-party message text. We show code + severity + module derived from the code structure (protocol facts), and our own one-line wording for the single code we understand (0500050000010007).
- **Link scheme (verified with curl 2026-10-25):**
  - HMS: `https://e.bambulab.com/index.php?e=<16 hex>&s=device_hms&lang=en`. This is Bambu's own redirector, the one Bambu Studio opens. It 302s to the model-appropriate wiki page (e.g. `/en/x1/troubleshooting/hmscode/0500_0500_0001_0007`, `/en/h2/...` for the P2S lubrication code; `1800...` AMS HT codes map to the `0700...` page) and falls back to `/en/hms/home` when the wiki has no page. The direct wiki URL 404s for unknown codes, so the redirector is better.
  - `print_error`: the wiki has a single table at `https://wiki.bambulab.com/en/hms/error-code` with no per-code anchors (codes spelled `0300-8003 (03008003)`). The redirector sends every print_error code to the HMS home. So the link is the table plus a text fragment `#:~:text=0300-8003`, which scrolls to the code in Chromium, Firefox 131+ and Safari, and is harmless elsewhere.
- **Decoding (hms.py; pure over the `print` dict, reusing `protocol.hms_codes`):**
  - HMS level = `code >> 16`: 1 fatal, 2 serious, 3 common, 4 info. Level 0 is invalid (Bambu Studio's enum) and is skipped. The research doc's "skip `code & 0xFFFF < 0x4000`" is a misreading: BamBuddy skips `code < 0x4000` (whole value). A low-16 filter would drop real codes like 0500050000010007 and 0700200000020001.
  - Module from the top byte of `attr`:
    - 03 Motion controller, 05 System, 08 Toolhead, 0C Camera / lidar.
    - 07 AMS: the unit is in the next byte (0-3 → "AMS 1-4"); FF → External spool.
    - 18 AMS HT: unit `& 0x7F` → "AMS HT n", as in `report.ams_name`.
    - Unknown modules show nothing.
    - I called 0x05 "System", not "Mainboard": the P2S's 0501_0400_0003_0002 is a lubrication reminder.
  - `print_error`: high 16 bits = module + unit, low 16 = error. Its top 2 bits give the level (4xxx fatal / task stopped, 8xxx serious / paused, Cxxx common / prompt); below 0x4000 it's skipped. Accepts an int or a decimal string.
  - User-cancel echoes `0300_400C` / `0500_400E` are dropped from both print_error and hms[] (HMS short code = `attr>>16` + `code & 0xFFFF`).
  - A print_error that is also raised as an HMS with the same short code is listed once, in its HMS form.
- **Lingering vs active (design decision).** An error is `active` only when all of these hold:
  - the job is PAUSE/FAILED;
  - the job wasn't stopped by the user or the file (`stg_cur` 5/16/30, or a cancel echo in print_error);
  - the error is the print error, or a fatal/serious HMS that isn't record-only (0500050000010007).
  
  Everything else is "lingering": printers keep HMS until they're acknowledged on them. On the card:
  - Active errors are a red-bordered block above the job ("Warning · AMS 2 · <code link>").
  - Lingering ones are a quiet collapsed "N printer messages" disclosure, styled like the Developer Mode hint, and listed in full when opened.
  - The block is rewritten only when the errors change, so an open list stays open.
  - Offline printers show none.
  
  `PrinterState.error_code` (never set, unused) was replaced by `errors: list[PrinterError]`.
- Severity labels in the UI: fatal "Error", serious "Warning", common "Notice", info "Info". No "↗" after the code: Windows renders it as an emoji.
- Asset cache-buster bumped v=43 → v=44 (`base.html`).
- **Live check (own instance on :8093, temp LOCALAPPDATA, no commands sent).** All four printers carry 0500050000010007 (A1 and P1S too, though they send no `fun`), so its description no longer claims Developer Mode is off. What each card shows now:
  - A1 (printing): "1 printer message".
  - P1S (finished): "1 printer message".
  - H2C (printing): 3 messages: 0500050000010007, 0500_0600_0002_0006, 0500_0600_0002_0052. The last two have no page in Bambu's catalogue or wiki.
  - P2S (finished): 2 messages: 0500050000010007 and 0501_0400_0003_0002 (lubrication reminder, Notice).
  
  All of them are lingering, so nothing is red. Screenshot of the live dashboard checked; a mock with an active AMS runout (paused) and a failed print_error also checked.
- Later tweak from that check: record-only codes get no severity. Otherwise 0500050000010007 read "Error · System" next to "not a fault".
- **Reviews:**
  - Self-review of the full diff: fine.
  - Independent fresh-context Claude (opus) review, first round: 2 P1, 3 P2, 4 P3.
    - Fixed P1: lingering HMS turned active after a user cancel or pause. Now user pauses (`stg_cur` 5/16/30) and cancels don't count, and notices never do.
    - Fixed P1: the refused-command record showed "Error". Now it has no severity.
    - Fixed P2: a fatal HMS is now active even without a job.
    - Fixed P2: the lingering list has aria-live off.
    - Rejected P2: "print_error wiki link unverified". It was bot-blocked (402) for the reviewer; curl shows 200 and the code spelling. The reviewer withdrew it.
    - Fixed P3: shared CSS with the quiet notice, reworded the dedupe comment.
    - Not done P3: 0x12 module (unconfirmed, harmless) and the duplicated JS test harness (matches the other test files).
  - Re-review: all fixes correct, 1 new P2: a FAILED job with print_error 0 counted as a fault. Fixed: FAILED needs a real print_error.
  - ChatGPT/Loom review: triggered by the P1/P2s, but this project has no ChatGPT/Loom channel configured and none is available to this agent. The Claude re-review round stood in for it.
- **Final work:**
  - Branch commits: 0397302, 23520d5, 4350ec6, c6af54e, d3e94c3.
  - Merged into `bambuddy-improvements` as 7b69e79 (`--no-ff`). Tests on the integration branch: pytest 384 passed, node 68 passed.
  - Worktree removed and branch deleted. My :8093 instance was stopped and the temp config copy deleted.
- **Open:**
  - A "Clear error" action (`clean_print_error`) is not done. It's a printer command, out of scope here.
  - The module byte 0x12 and others stay unnamed.

## Questions
