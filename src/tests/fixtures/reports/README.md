# Captured MQTT report fixtures

Real `device/<serial>/report` traffic from the user's four printers, one
gzipped JSONL file per printer: `a1.jsonl.gz`, `p1s.jsonl.gz`,
`h2c.jsonl.gz`, `p2s.jsonl.gz`. They are the regression base for decoding
and for merging partial pushes. Load them with `tests/report_fixtures.py`
(`load("p1s").full_report()`, `.status_pushes()`, `.version()`, ...).

They are gzipped because the H2C and P2S push their full state every second
(6 MB of near-duplicate JSON → 50 KB). Read one by hand with
`python -c "import gzip,sys;sys.stdout.write(gzip.open(sys.argv[1],'rt',encoding='utf-8').read())" p1s.jsonl.gz`.

## Format (version 1)

Line 1 is the header:

```json
{"kind": "header", "format": 1, "printer": "P1S", "model": "C12",
 "serial": "01PMASK00000003", "captured_utc": "2026-10-25T16:32Z",
 "duration_s": 597.522, "masked_keys": ["ams_id", "chip_id", ...]}
```

- `model`: the SSDP `DevModel` code (`PrinterConfig.model`): A1 `N2S`,
  P1S `C12`, H2C `O1C2`, P2S `N7`.
- `serial`: the masked serial the topics use.
- `captured_utc`: when the first message was sent or received (minute).
- `duration_s`: `t` of the last message (the session itself ran 600 s).
- `masked_keys`: JSON keys whose values were masked in this file.

Every other line is one MQTT message, in arrival order:

```json
{"t": 1.614, "dir": "in", "topic": "device/<serial>/report", "payload": {"print": {...}}}
```

- `t`: seconds since the session started (monotonic; keeps the real gaps).
- `dir`: `in` = report from the printer, `out` = a request this capture sent
  (on `device/<serial>/request`; only a standalone `pushall` and
  `get_version`, so you can tell which report answers what).
- `payload`: the parsed JSON, unchanged apart from masking. A message that
  isn't valid JSON would carry `payload_text` instead (none so far).

Connection events (CONNACK, SUBACK, disconnect) aren't kept.

## What was captured (2026-10-25)

One 10-minute session per printer, all four at once, from 16:32 UTC. Bambu
Studio was connected to the A1, H2C and P2S at the time (not the P1S).

| File | Model | Messages | Full (`msg` 0) | Deltas (`msg` 1) | Notes |
|---|---|---|---|---|---|
| `a1` | N2S | 261 | 4 | 251 | Idle (FINISH), then a print started from Bambu Studio at ~540 s: `system` `sntp`, a second `get_version` reply (Studio's), the `project_file` echo, FINISH → PREPARE → RUNNING. 4 partial `ams` pushes, including `{"tray_exist_bits", "version"}` with no unit list. 3 of the full reports answer Studio's pushalls |
| `p1s` | C12 | 183 | 1 | 179 | Idle. 17 partial `ams` pushes that carry only `{"ams": [unit]}` (no `tray_exist_bits` / `tray_now` / ...) |
| `h2c` | O1C2 | 538 | 535 | 0 | Idle (FINISH). A full 13 KB report about every second (`sequence_id` "2021"); three AMS units (2 x AMS 2 Pro, AMS HT); heaters off, so no packed `target<<16\|current` temps |
| `p2s` | N7 | 530 | 527 | 0 | Idle (FINISH). A full 9 KB report about every second |

Every file starts with our `pushall` and `get_version` requests and has the
`get_version` (`info`) reply.

## Masking

The tool's `mask` step (`Masker` in `tools/capture_reports.py`) replaces,
consistently across all four files (the same real value always gets the same
fake one):

- serials: printer, AMS, nozzle and module `sn` fields, `dev_id` (in the
  `project_file` echo), the P1S AMS `ams_id`, and anywhere in text (topics).
  The first 3 characters (the model part) are kept, then `MASK` + a counter,
  same length. Placeholders like `sn: "N/A"` are kept;
- AMS `chip_id`s: a hex counter, same length;
- the cloud print's `project_file` `url`, a presigned S3 link holding the
  account id and an AWS signature: `https://masked.invalid/job-N.3mf`;
- private IPv4 addresses (`ipcam.rtsp_url`, the packed little-endian
  `net.info[].ip`): `192.0.2.N`;
- cloud / MakerWorld ids (`task_id`, `subtask_id`, `project_id`, `job_id`,
  `profile_id`, `design_id`, `model_id`, ...): one mapping, so a cloud job's
  shared number stays shared; `0` / `""` kept;
- job file names (`subtask_name`, `gcode_file`, `file`, `url`): the stem
  becomes `job-N`, keeping the directory, the extension, generic
  `plate_N.gcode` names and, for LAN `file://` / `ftp://` URLs, the
  `subtask_name` ↔ `url` match (none in these files: the A1 print came
  from the cloud, see above);
- any `*ssid*` field and MAC addresses (none seen).

Kept: RFID spool ids (`tag_uid`, `tray_uuid`), firmware and hardware
versions, `fun` / `cfg` bitfields, timestamps and the `sntp` time zone, the
file `md5`. Reports never contain the access code.

## Re-capturing

From `src/`:

```
python -m tools.capture_reports capture --minutes 10   # raw, to %TEMP%\bamboozle-capture
python -m tools.capture_reports mask                   # raw -> these files
```

`capture` sends only one standalone `pushall` and `get_version` per printer
(no pushall within 5 minutes of the last one, tracked in
`%TEMP%/bamboozle-capture-pushall-ledger.json`), one MQTT session per printer.
Delete the raw directory afterwards: it holds the real serials and IPs.
