# Ffmpeg Model Profiles

## Status
done  <!-- todo | in progress | blocked | done -->

## Severity
minor  <!-- trivial | minor | major — set at creation; may be updated later (see Severity) -->

## Task
Research item B2 (doc §2.3, TL;DR #5).
- Add per-model ffmpeg argument profiles: P2S gets `-use_wallclock_as_timestamps 1` and a bigger probe; the other RTSP models get low-latency flags.
- This needs the printer model to be known. Store it on `PrinterConfig`, filled from the SSDP `DevModel` via `discovery.model_info` (an existing config without it gets it backfilled from announcements), and keep the `model_info` table in one place.
- Add H2C's `O1C` code variant if the doc confirms it.
- The TLS proxy (TL;DR #22) is out of scope unless the P2S still drops after this.

## Grilling
resolved  <!-- n/a (trivial) | pending | resolved — resolved decisions listed below -->

The user delegated the design decisions on 2026-10-25 ("Do A, B, C, 14 and proceed to fix"):
- Follow the recommendations in `docs/research/bambuddy.md` (committed on branch `bambuddy-improvements`).
- Record each design decision you make in `## Notes`.
- Block only on a choice that is genuinely the user's (e.g. a visible UX trade-off the research doesn't settle).
- BamBuddy is AGPL-3.0: reimplement ideas, never copy its code.

## Notes
Branch `ffmpeg-model-profiles` (commits 912bbb7, 7ab38eb, 3517716), merged into `bambuddy-improvements` as 25de264. Worktree removed, branch deleted.

**What changed**
- `camera.py`: `rtsp_command(ffmpeg, url, profile)`. Every profile reads unbuffered: `-fflags nobuffer -flags low_delay -max_delay 500000`, as BamBuddy's real command line does for all models.
  - `LOW_LATENCY` (default): adds `-probesize 32 -analyzeduration 0`.
  - `TOLERANT` (P2S): adds `-probesize 1000000 -analyzeduration 500000 -use_wallclock_as_timestamps 1`.
  - `rtsp_profile(model)` picks the profile by the friendly name from `discovery.model_info`, so the code table stays only in `discovery._MODELS`.
- `PrinterConfig.model`: the SSDP DevModel code, `""` until heard.
- `discovery._MODELS`: added `O1C` → H2C (322). Research doc §4 lists it.
- CLAUDE.md is updated.

**Design decisions**
1. Store the raw DevModel code, not the friendly name. `model_info` resolves it, and an unknown code is still kept.
2. The backfill uses a lighter save, not `update_printer`. `PrinterManager._learn_model` sets `cfg.model` and `camera.model` in place and calls `save_config`: no reconnect and no history backup.
   - Why: `update_printer` would drop MQTT (possibly mid-print), rebuild the connection (which force-resets `camera_enabled`, so an open camera view would turn off), and write a history file. That is all for a field that only picks the ffmpeg flags of the next camera session.
   - It saves only when the model changes.
3. The backfill only trusts an announcement whose sender IP is the configured IP. A move to a new IP goes through the existing cert-CN-verified relocation, which now also carries `model`.
4. Add/edit fill an empty model from the discovery cache, but only for an announcement from the config's IP within the 5-minute TTL. An API edit keeps the model, and clears it if the serial changes.
5. Fallback follows research B2's "probesize 32 may not lock" risk, and also covers a P2S whose model isn't known yet.
   - For a LOW_LATENCY model, until LOW_LATENCY has shown a moving picture (2+ distinct frames) this run, an unhealthy or frozen LOW_LATENCY session switches to TOLERANT.
   - A TOLERANT session with no moving picture switches back, so an unreachable or busy printer at enable time can't pin a profile.
   - Once LOW_LATENCY has worked, drops and still scenes keep it.
   - `start()` resets this (done in the capture thread after the previous thread has finished).
6. The profile is shown in the "Camera connected ... (x profile)" log line.

**Tests**: TDD. `src/tests/test_model_profiles.py` (new, 28 tests). The shared ffmpeg fakes moved to `fakes.py` / `conftest.py`. On the integration branch after the merge: pytest 214 passed, JS 37 passed.

**Live verification** (own instance on :8093, temp LOCALAPPDATA copy, browser patched; temp copy deleted; :8080 untouched)
- A raw ffmpeg probe found all profiles working on this ffmpeg 8.1 build:
  - P2S TOLERANT: 230 frames / 25 s, first frame 1.83 s.
  - P2S LOW_LATENCY: 232 / 25 s, first frame 1.92 s.
  - H2C LOW_LATENCY: first frame 1.17 s.
  - H2C TOLERANT: first frame 0.95 s.
- In the app, all 4 printers backfilled from SSDP within about 40 s (A1 N2S, P1S C12, H2C O1C2, P2S N7). No history dir was written and no reconnects happened.
- Through `/stream`:
  - P2S: TOLERANT, 314 frames / 40 s, first frame 1.86 s, no drops.
  - H2C: LOW_LATENCY, 175 frames / 25 s, first frame 1.1 s.
- No MQTT commands were sent. One stray probe hit port 322 on the P1S (.103): a TCP connect, refused.

**Reviews**
- Self-review: clean.
- Independent review 1 (fresh Opus agent):
  - P2, fixed: the fallback fired on any freeze or unhealthy session, so it lost low latency after routine drops. Low latency now stays once it has shown a moving picture.
  - P2, fixed: a misleading test comment, and a missing test.
  - P3, fixed: a reset race (the reset moved into the capture thread).
  - P3, addressed: a redundant config loop (now commented).
  - P3, rejected: `-buffer_size` is optional and not in B2.
- The ChatGPT/Loom step was triggered (review 1 found P2s), but this environment has no ChatGPT/Loom channel. A second fresh-context Opus review stood in for it:
  - P2, fixed: the fallback never switched back. Profiles now alternate.
  - P3, fixed: the profile was computed twice; the cache model needed an IP/TTL check; the shared flags are now documented and tested; the fakes moved to conftest/fakes; test names and coverage.
  - P3, not done: a hostname in `cfg.ip` would never backfill. Configs hold IPs and relocation compares IPs too. The `asyncio.sleep(0.05)` waits follow the existing pattern in `test_relocation.py`.

**Open**
- The TLS proxy (B5) is not needed: the P2S streamed steadily in both profiles on this Windows ffmpeg.
- The other BamBuddy model codes (O1S, O1E/O2D, N6, N9, A04/A11/A12) were not added. They are out of this task's scope.

## Questions
