# Hms Descriptions From Bambu

## Status
done  <!-- todo | in progress | blocked | done -->

## Severity
minor  <!-- trivial | minor | major — set at creation; may be updated later (see Severity) -->

## Task
The printer-message codes on the cards (from `hms-error-decoding`) mean little to the user: "Warning · System · 0500_0600_0002_0006". Show Bambu's own description text, fetched at runtime from Bambu's public catalogue (the one Bambu Studio downloads). It is cached locally and never bundled in the repo, so there's no licence issue.

Catalogue facts, checked 2026-10-25:
- `https://e.bambulab.com/query.php?lang=en` returns JSON, about 330 KB: `{"result":0,"t":..,"ver":..,"data":{"device_hms":{"ver":..,"en":[{"ecode":"0500050000010007","intro":"MQTT Command verification failed. ..."}, ...]}, "device_error":{...}}}`.
- Adding `&d=<first 3 chars of the serial>` (e.g. `d=31B` for the H2C, `d=22E` for the P2S) returns a per-model catalogue, about 530 KB / 375 KB. It holds entries the generic one lacks, e.g. H2C `0500060000020001` "The Toolhead Camera is not in place...".
- `d=O1C2` / `d=N7` (DevModel codes) return nothing. Use the serial prefix.
- Some codes are listed with an EMPTY `intro`: the H2C's `0500060000020006` and `0500060000020052`. Bambu has no text for these.
- `0501040000030002` (P2S) = "Threaded rods need lubrication now."
- `device_error` covers `print_error` codes. Check its ecode format.

## Grilling
resolved  <!-- n/a (trivial) | pending | resolved — resolved decisions listed below -->

Decided with the user (2026-10-25):
1. **Fetch and cache, always on.** The user approved Bamboozle contacting e.bambulab.com: fetch per printer model (by serial prefix), plus the generic catalogue as a fallback. Cache under `%LOCALAPPDATA%\Bamboozle\` (e.g. `hms-cache/`), refresh weekly, and use the stale cache if the fetch fails. No setting is needed.
2. **Only the model prefix leaves the machine:** the 3-character serial prefix, never the full serial. Fetch in the background, off the poll path, with a timeout. A missing catalogue must never break the cards; fall back to today's code-only display.
3. **Card shows the text first:** each message leads with Bambu's description. The code becomes small secondary text that keeps the wiki link. Severity stays.
4. **Codes with empty text:** say "No description from Bambu (internal code)" and sort them last within the collapsed group.
5. **Keep our own text where it's better:** the 0500050000010007 description stays (Bambu's text is misleading for us: it says update Studio).
6. **Never bundle catalogue text in the repo:** use short made-up strings in tests, and no fixtures copied from Bambu's catalogue.
7. **Language:** English only (`lang=en`).

## Notes
- 2026-10-25: worktree `D:/git/worktrees/Bamboozle-hms-descriptions-from-bambu`, branch `hms-descriptions-from-bambu` from `bambuddy-improvements`. Severity minor: TDD (pytest + node tests first, red then green).
- **Catalogue facts checked (downloads kept outside the repo, deleted after):**
  - `device_hms` ecodes are 16 hex, upper case. `device_error` ecodes are 8 hex: the `print_error` code without the underscore (`03008003`). Entries are only `{ecode, intro}`, no HTML in any intro, max ~340 chars, a few non-ASCII and one newline.
  - Bambu's CDN (Cloudflare) answers 403 to Python's default `Python-urllib` User-Agent, so the client sends `Bamboozle (LAN printer dashboard)`.
  - An unknown prefix (or a DevModel code) answers `{"result":201,...}` with no data. This is cached as an empty catalogue, so it isn't refetched every round.
  - The user's prefixes: A1 `039`, P1S `01P`, H2C `31B`, P2S `22E`. All four have per-model catalogues.
- **Design (decisions mine within the Grilling):**
  - New module `hms_catalog.py`, kept out of `printer_manager.py` because of the parallel `replace-bambulabs-api` work. It holds an `HmsCatalog` class and a default `CATALOG` instance.
    - Pure lookup `describe(errors, serial)`, called right after `hms.printer_errors` (one changed line in `_read_sync`). `hms.py` keeps its signature.
    - Background `start(serials_fn)` / `stop()`, called from `PrinterManager.start` / `stop` (2 lines). Serials are re-read from the config every 60 s, so added printers are picked up.
  - Fetch: urllib on a daemon thread (shutdown never waits on it), 20 s timeout, 4 MB cap. Each catalogue is fetched when it's missing or a week old (wall clock; a cache "from the future" counts as stale). A failure keeps the stale cache and retries after 10 min, doubling to 6 h.
  - Cache: `%LOCALAPPDATA%\Bamboozle\hms-cache\en.json` (generic) and `en-<PFX>.json`. It stores our validated `{fetched_at, hms, error}`, not the raw download. Written atomically and re-validated on load; a corrupt file is ignored and refetched.
  - Parsing is defensive: JSON object, `result` 0 (or 201 = empty), sections are lists, each ecode a hex string of the right length (upper-cased), intro a string. Whitespace is collapsed and capped at 1000 chars; malformed entries are dropped.
  - Lookup: the model's text first, then the generic text. A code listed only with an empty intro gets "No description from Bambu (internal code)" plus a new `PrinterError.undocumented` flag. It's sorted last after the active ones (stable re-sort, so severity order holds), and an active one stays in the red block. A code no loaded catalogue lists keeps the code-only line, as does a missing catalogue. Our own description (0500050000010007) always wins. `describe` never raises.
  - Card (decision 3): a described line reads "Warning · <text>" (severity kept, first), then "<module> · <linked code>" small and muted below. A line without text keeps today's "Warning · AMS 2 · <code>". Undocumented text is italic.
  - Untrusted text never goes through HTML. `errorLineHtml` emits an empty `<span class="error-text">`, and `applyErrors` fills them with `textContent`. The card's initial render now goes through `applyErrors` too, instead of inlining `errorsHtml`. This applies to our own descriptions as well.
  - Tests: `conftest.py` replaces `hms_catalog.http_get` with one that fails, and gives each test an empty `CATALOG`, so no test touches the network. Every catalogue string in the tests is made up.
  - Asset cache-buster v=44 → v=45. CLAUDE.md has a `hms_catalog.py` paragraph.
- **Live check (own instance on :8094, temp LOCALAPPDATA with a config copy, browser open patched out, no commands sent).**
  - All five catalogues were fetched on start (generic, 01P, 039, 22E, 31B) and cached.
  - Expanded-list screenshot checked. What the cards read now:
    - A1 (finished), P1S (finished): "1 printer message": our refused-command text (System · 0500_0500_0001_0007).
    - H2C (printing): 3 messages: our refused-command text, then "Warning · No description from Bambu (internal code)" for both 0500_0600_0002_0006 and 0500_0600_0002_0052 (listed with empty intro in the 31B catalogue), sorted last.
    - P2S (finished): 2 messages: "Notice · Threaded rods need lubrication now." (0501_0400_0003_0002), then our refused-command text.
  - Nothing is active, so nothing is red.
- Branch commit: 37f4c1a.
- **Reviews:**
  - Self-review of the full diff: fine.
  - Independent fresh-context Claude (opus) review: no P1, 2 P2, 4 P3. Its result went to the coordinator, which relayed it. All of them were fixed in 3435289, with tests:
    - P2: each model's cache was only read after the earlier models' fetches. Now every cache file loads before the first fetch.
    - P2: an empty answer (`result: 201`, or no entries) replaced a good cache for a week. Now it never replaces a non-empty cache, which is asked again in a day. With nothing cached, the empty answer is kept but due again in a day.
    - P3: `_http_get` has an overall 20 s deadline (chunked read); urllib's timeout is per socket operation.
    - P3: stale / misplaced comments in app.js.
    - P3: the catalogue's stop moved after the poll-task cancel, so "first, so it can't spawn more reconnects" is true again.
  - ChatGPT/Loom: the two P2s trigger it, but this project has no ChatGPT/Loom channel available to this agent (as in `hms-error-decoding`). The fixes are test-covered. The change touches no money, auth, personal data (only the 3-character model prefix leaves the machine) or migrations.
- **Final:**
  - Merged into `bambuddy-improvements` as 9bebad7 (`--no-ff`, no conflicts: `replace-bambulabs-api` hadn't merged yet). Tests there: pytest 483 passed, node 74 passed.
  - Worktree removed and branch deleted. My :8094 instance was stopped; the temp config copy, catalogue downloads and Chrome profile were deleted.
- **Open:**
  - When `replace-bambulabs-api` merges, its `snapshot.decode` takes over the `hms.printer_errors` call (no serial there). The `hms_catalog.describe(..., self.cfg.serial)` line must move to its `read_state`, right after `decode` (`fields["errors"] = hms_catalog.describe(fields["errors"], self.cfg.serial)`). The `CATALOG.start/stop` lines in `PrinterManager.start/stop` sit next to its changes.
  - With event-driven updates, a description appears on the next re-decode after the catalogue loads (not instantly).

## Questions
