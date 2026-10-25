"""Capture real MQTT `report` messages from the configured printers, and turn
them into the masked test fixtures under `tests/fixtures/reports/`.

Run from `src/` (it imports `bamboozle`):

    python -m tools.capture_reports capture [--minutes 10] [--printer NAME ...]
    python -m tools.capture_reports mask [--raw DIR] [--dest tests/fixtures/reports]

`capture` is read-only towards the printers: per printer it opens one MQTT
session (TLS, user `bblp`), subscribes to `device/<serial>/report`, and sends
only one standalone `pushall` and a standalone `get_version` (retried once if
unanswered). Nothing else is ever published. It refuses to send a pushall to a
printer that got one from this tool in the last 5 minutes (P1-series printers
lag on frequent pushalls; the ledger is `%TEMP%/bamboozle-capture-pushall-ledger.json`). The raw capture (real serials and IPs, never the
access code, which reports don't carry) goes to a directory outside the repo,
by default `%TEMP%/bamboozle-capture`.

`mask` rewrites the raw capture into the fixture format (see
`tests/fixtures/reports/README.md`) with serials, IPs, cloud ids, SSIDs, MACs
and file names masked consistently. Masking is a separate step so the rules
can be refined without capturing again.

The model code (SSDP `DevModel`) comes from the config, or else from an
announcement heard passively on UDP 2021 during the capture.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import ipaddress
import json
import re
import socket
import ssl
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote

FORMAT_VERSION = 1
PUSHALL_MIN_INTERVAL_SECONDS = 5 * 60
VERSION_RETRY_SECONDS = 60
VERSION_DELAY_SECONDS = 3

DEFAULT_RAW_DIR = Path(tempfile.gettempdir()) / "bamboozle-capture"
# Outside the raw dir, so deleting a capture (or passing another --raw) can't
# reset the 5-minute pushall guard.
LEDGER_PATH = Path(tempfile.gettempdir()) / "bamboozle-capture-pushall-ledger.json"
APP_PORT = 8080
DEFAULT_DEST = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "reports"


# --- Capture ----------------------------------------------------------------------

def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _decode_payload(raw: bytes) -> dict[str, Any]:
    """`{"payload": <json>}`, or `{"payload_text": ...}` when it isn't JSON
    (A1 mini firmware has sent invalid UTF-8)."""
    text = raw.decode("utf-8", errors="replace")
    try:
        return {"payload": json.loads(text)}
    except ValueError:
        return {"payload_text": text}


class _Recorder:
    """Appends one JSON line per event to the raw file, flushing each, so an
    interrupted capture keeps what it got."""

    def __init__(self, path: Path, header: dict[str, Any]):
        self._lock = threading.Lock()
        self._t0 = time.monotonic()
        self._file = path.open("w", encoding="utf-8", newline="\n")
        self._write(header)
        self.counts = {"in": 0, "out": 0}

    def _write(self, record: dict[str, Any]) -> None:
        self._file.write(json.dumps(record, separators=(",", ":"), ensure_ascii=False) + "\n")
        self._file.flush()

    def record(self, direction: str, topic: str, body: dict[str, Any]) -> None:
        with self._lock:
            self.counts[direction] = self.counts.get(direction, 0) + 1
            self._write({
                "t": round(time.monotonic() - self._t0, 3),
                "utc": _utc_now(),
                "dir": direction,
                "topic": topic,
                **body,
            })

    def note(self, event: str, **fields: Any) -> None:
        with self._lock:
            self._write({"t": round(time.monotonic() - self._t0, 3), "utc": _utc_now(),
                         "dir": "event", "event": event, **fields})

    def close(self) -> None:
        with self._lock:
            self._file.close()


class _PushallLedger:
    """When this tool last sent each printer a pushall, so re-runs can't flood
    it. Keyed by a hash of the serial, so the file holds no serials."""

    def __init__(self, path: Path):
        self._path = path
        try:
            self._data: dict[str, float] = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self._data = {}
        self._lock = threading.Lock()

    @staticmethod
    def _key(serial: str) -> str:
        return hashlib.sha256(serial.upper().encode()).hexdigest()[:16]

    def seconds_since(self, serial: str) -> float | None:
        last = self._data.get(self._key(serial))
        return None if last is None else time.time() - last

    def mark(self, serial: str) -> None:
        with self._lock:
            self._data[self._key(serial)] = time.time()
            self._path.write_text(json.dumps(self._data), encoding="utf-8")


def _allowed_requests(payload: dict) -> list[dict]:
    """`payload` itself if it is a standalone pushall or get_version, else []."""
    if len(payload) != 1:
        return []
    (section, body), = payload.items()
    if not isinstance(body, dict) or set(body) != {"command", "sequence_id"}:
        return []
    if (section, body["command"]) in (("pushing", "pushall"), ("info", "get_version")):
        return [payload]
    return []


def _later(seconds: float, fn: Any) -> None:
    timer = threading.Timer(seconds, fn)
    timer.daemon = True
    timer.start()


def _capture_one(cfg: Any, raw_dir: Path, seconds: float, send_pushall: bool,
                 ledger: _PushallLedger, models: dict[str, str],
                 stop: threading.Event) -> _Recorder:
    import paho.mqtt.client as mqtt

    from bamboozle import mqtt as bmqtt

    report_topic = f"device/{cfg.serial}/report"
    request_topic = f"device/{cfg.serial}/request"
    recorder = _Recorder(raw_dir / f"{_slug(cfg.name)}.raw.jsonl", {
        "kind": "raw-header",
        "format": FORMAT_VERSION,
        "name": cfg.name,
        "serial": cfg.serial,
        "ip": cfg.ip,
        "model": cfg.model,
        "started_utc": _utc_now(),
    })
    state = {"pushall_sent": False, "version_sent": 0, "version_answered": False}
    lock = threading.Lock()

    def publish(payload: dict) -> None:
        # The only two requests this tool may ever send (a real check, not an
        # assert, so `python -O` can't strip it).
        if payload not in _allowed_requests(payload):
            raise ValueError(f"refusing to publish {payload}")
        client.publish(request_topic, json.dumps(payload), qos=1)
        recorder.record("out", request_topic, {"payload": payload})

    def send_version() -> None:
        with lock:
            if state["version_answered"] or state["version_sent"] >= 2 or not client.is_connected():
                return
            state["version_sent"] += 1
        publish(bmqtt.version_request())
        _later(VERSION_RETRY_SECONDS, send_version)

    def on_connect(client, userdata, flags, reason_code, properties):
        recorder.note("connack", reason_code=str(reason_code))
        if not reason_code.is_failure:
            client.subscribe(report_topic, qos=0)

    def on_subscribe(client, userdata, mid, reason_codes, properties):
        recorder.note("suback", reason_codes=[str(rc) for rc in reason_codes])
        with lock:
            first = not state["pushall_sent"]
            state["pushall_sent"] = True
        if not first:
            return  # reconnected: one pushall per run is the budget
        if send_pushall:
            since = ledger.seconds_since(cfg.serial)
            if since is not None and since < PUSHALL_MIN_INTERVAL_SECONDS:
                recorder.note("pushall_skipped", seconds_since_last=round(since))
            else:
                ledger.mark(cfg.serial)
                publish(bmqtt.pushall_request())
        _later(VERSION_DELAY_SECONDS, send_version)

    def on_message(client, userdata, msg):
        body = _decode_payload(msg.payload)
        info = body.get("payload", {}).get("info") if isinstance(body.get("payload"), dict) else None
        if isinstance(info, dict) and info.get("command") == "get_version":
            state["version_answered"] = True
        recorder.record("in", msg.topic, body)

    def on_disconnect(client, userdata, flags, reason_code, properties):
        recorder.note("disconnect", reason_code=str(reason_code))

    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id=f"bamboozle-capture-{uuid.uuid4().hex[:8]}",
        protocol=mqtt.MQTTv311,
    )
    client.username_pw_set("bblp", cfg.access_code)
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # printers use self-signed certs
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    client.tls_set_context(ctx)
    client.reconnect_delay_set(1, 30)
    client.on_connect = on_connect
    client.on_subscribe = on_subscribe
    client.on_message = on_message
    client.on_disconnect = on_disconnect
    client.connect_async(cfg.ip, 8883, keepalive=30)
    client.loop_start()
    try:
        stop.wait(seconds)  # set early on Ctrl+C
    finally:
        with lock:
            state["version_answered"] = True  # stop retries
        client.disconnect()
        client.loop_stop()
        recorder.note("end", model_heard=models.get(cfg.serial.upper(), ""))
        recorder.close()
    return recorder


def _listen_ssdp(models: dict[str, str], stop: threading.Event) -> None:
    """Passively note `DevModel` per serial from SSDP announcements (UDP 2021,
    shared with Bambu Studio). Best effort: a bind failure just means the
    model comes from the config only."""
    from bamboozle.discovery import SSDP_PORT, parse_announcement

    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("", SSDP_PORT))
        sock.settimeout(1.0)
    except OSError as e:
        print(f"SSDP listen unavailable ({e}); models come from the config only")
        return
    with sock:
        while not stop.is_set():
            try:
                data, _ = sock.recvfrom(4096)
            except OSError:
                continue
            ann = parse_announcement(data)
            if ann and ann.model:
                models[ann.serial.upper()] = ann.model


def _port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "printer"


def _load_config_readonly() -> Any:
    """The app config, read without any of `load_config`'s write paths."""
    from bamboozle.config import _config_path
    from bamboozle.models import AppConfig

    path = _config_path()
    return AppConfig.model_validate(json.loads(path.read_text(encoding="utf-8")))


def cmd_capture(args: argparse.Namespace) -> int:
    config = _load_config_readonly()
    printers = [p for p in config.printers
                if not args.printer or p.name.lower() in {n.lower() for n in args.printer}]
    if not printers:
        print("No matching printers in the config")
        return 1
    # One session per printer, one raw file per printer.
    if len({p.serial.upper() for p in printers}) != len(printers):
        print("Two config entries share a serial; pick one with --printer")
        return 1
    if len({_slug(p.name) for p in printers}) != len(printers):
        print("Two printer names map to the same file name; pick with --printer")
        return 1
    if _port_open("127.0.0.1", APP_PORT):
        print(f"Note: something listens on port {APP_PORT} (Bamboozle?); it holds its own "
              "MQTT session to each printer, so the P1-series client limit is closer.")
    raw_dir = Path(args.raw)
    raw_dir.mkdir(parents=True, exist_ok=True)
    ledger = _PushallLedger(LEDGER_PATH)
    models: dict[str, str] = {}
    stop = threading.Event()
    threading.Thread(target=_listen_ssdp, args=(models, stop), daemon=True).start()
    seconds = args.minutes * 60
    print(f"Capturing {', '.join(p.name for p in printers)} for {args.minutes} min into {raw_dir}",
          flush=True)
    results: dict[str, _Recorder] = {}

    def run(cfg):
        results[cfg.name] = _capture_one(cfg, raw_dir, seconds, not args.no_pushall, ledger,
                                         models, stop)

    threads = [threading.Thread(target=run, args=(p,)) for p in printers]
    for t in threads:
        t.start()
    try:
        for t in threads:
            while t.is_alive():
                t.join(0.5)  # a bare join() can't be interrupted on Windows
    except KeyboardInterrupt:
        print("Stopping early", flush=True)
    finally:
        stop.set()  # ends the sessions (cleanly) and the SSDP listener
        for t in threads:
            t.join()
    for name, rec in results.items():
        print(f"{name}: {rec.counts.get('in', 0)} received, {rec.counts.get('out', 0)} sent")
    return 0


# --- Masking ----------------------------------------------------------------------

# Cloud / account / MakerWorld ids. Zero or empty means "none" (LAN prints)
# and is kept. One namespace: a cloud job reports the same number as task,
# subtask, project and job id, and that stays visible.
_ID_KEYS = frozenset({
    "task_id", "subtask_id", "project_id", "profile_id", "design_id", "model_id",
    "job_id", "lan_task_id", "user_id", "uid", "creator_id",
})
# Job / file names, which can say something personal. The stem is replaced
# (consistently, so `subtask_name` still matches the `url` naming it); the
# directory prefix and print extension are kept, and so are the generic
# `plate_N` names of the G-code inside a 3MF.
_FILE_KEYS = frozenset({"subtask_name", "gcode_file", "file", "url", "project_file_name"})
_PRINT_EXTS = (".gcode.3mf", ".3mf", ".gcode")
_GENERIC_STEM_RE = re.compile(r"plate_\d+", re.IGNORECASE)
_IPV4_RE = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d{1,3}){3})(?![\d.])")
_MAC_RE = re.compile(r"\b[0-9A-Fa-f]{2}(?:[:-][0-9A-Fa-f]{2}){5}\b")
_SERIAL_PREFIX = 3  # the model part of a Bambu serial (e.g. "01P" = P1S)
# Serials under other names: the printer's own (`dev_id` in a project_file
# echo) and the AMS unit's (`ams_id` on P1S; a small int elsewhere).
_SERIAL_KEYS = frozenset({"sn", "dev_id", "ams_id"})
_SERIAL_VALUE_RE = re.compile(r"[A-Za-z0-9]{9,}")
# Per-unit hardware ids (AMS `chip_id`): hex, length kept.
_HARDWARE_ID_KEYS = frozenset({"chip_id"})
# A cloud print's project_file `url` is a presigned download link carrying the
# account id and an AWS signature: replaced whole.
_WEB_URL_RE = re.compile(r"^https?://", re.IGNORECASE)


class Masker:
    """Consistent replacements across everything masked by one instance:
    the same real value always maps to the same fake one.

    - serials (`sn` / `dev_id` / `ams_id` fields, the configured serials,
      anywhere in a string): the first 3 characters are kept, the rest
      becomes `MASK` + a counter, same length;
    - private IPv4 addresses, in strings and as the packed little-endian ints
      of `net.info[].ip`: 192.0.2.N (TEST-NET-1);
    - MAC addresses; any `*ssid*` field;
    - non-zero cloud ids (`task_id`, `project_id`, ...), the same fake for
      the int and the string form of a number;
    - AMS `chip_id`s;
    - job file names: the stem becomes `job-N` (`plate_N` is kept); web
      URLs (presigned cloud downloads) become `https://masked.invalid/job-N.ext`.
    """

    def __init__(self, serials: list[str] = ()):  # type: ignore[assignment]
        self._serials: dict[str, str] = {}
        self._ips: dict[str, str] = {}
        self._ids: dict[str, Any] = {}
        self._stems: dict[str, str] = {}
        self._macs: dict[str, str] = {}
        self._hardware: dict[str, str] = {}
        self.masked_keys: set[str] = set()
        for s in serials:
            self.serial(s)

    # Individual replacements

    def serial(self, real: str) -> str:
        key = real.upper()
        if key not in self._serials:
            n = len(self._serials) + 1
            if len(real) >= _SERIAL_PREFIX + 6:
                prefix = real[:_SERIAL_PREFIX]
                width = len(real) - _SERIAL_PREFIX - 4
            else:
                prefix, width = "", max(2, len(real) - 4)
            self._serials[key] = f"{prefix}MASK{n:0{width}d}"
        return self._serials[key]

    def ip(self, real: str) -> str:
        if real not in self._ips:
            self._ips[real] = f"192.0.2.{len(self._ips) + 1}"
        return self._ips[real]

    def _packed_ip(self, value: int) -> int:
        if not 0 < value < 2**32:
            return value
        real = str(ipaddress.IPv4Address(value.to_bytes(4, "little")))
        if not ipaddress.IPv4Address(real).is_private:
            return value
        return int.from_bytes(ipaddress.IPv4Address(self.ip(real)).packed, "little")

    def _cloud_id(self, value: Any) -> Any:
        if value in (0, "0", "", None) or isinstance(value, (bool, float, dict, list)):
            return value
        token = str(value)
        if token not in self._ids:
            n = len(self._ids) + 1
            if token.isdigit():
                self._ids[token] = str(10 ** (len(token) - 1) + n) if len(token) > 4 else str(n)
            else:
                self._ids[token] = f"masked-{n}"
        fake = self._ids[token]
        return int(fake) if isinstance(value, int) and fake.isdigit() else fake

    def _hardware_id(self, real: str) -> str:
        if real not in self._hardware:
            n = len(self._hardware) + 1
            self._hardware[real] = f"{n:0{max(len(real), 2)}x}"
        return self._hardware[real]

    def file_name(self, real: str) -> str:
        if not real:
            return real
        if _WEB_URL_RE.match(real):
            path = real.split("?", 1)[0]
            ext = next((e for e in _PRINT_EXTS if path.lower().endswith(e)), "")
            self._stems.setdefault(real, f"job-{len(self._stems) + 1}")
            return f"https://masked.invalid/{self._stems[real]}{ext}"
        head, sep, base = real.rpartition("/")
        ext = next((e for e in _PRINT_EXTS if base.lower().endswith(e)), "")
        stem = base[: len(base) - len(ext)] if ext else base
        if not stem or _GENERIC_STEM_RE.fullmatch(stem):
            return real
        norm = unquote(stem).replace(" ", "_")  # as protocol.file_on_internal_storage compares
        if norm not in self._stems:
            self._stems[norm] = f"job-{len(self._stems) + 1}"
        return f"{head}{sep}{self._stems[norm]}{base[len(stem):]}"

    def text(self, value: str) -> str:
        """Serials, private IPs and MACs inside free text (topics, URLs)."""
        for real, fake in self._serials.items():
            value = re.sub(re.escape(real), fake, value, flags=re.IGNORECASE)

        def ip_sub(m: re.Match) -> str:
            try:
                addr = ipaddress.IPv4Address(m.group(1))
            except ValueError:  # e.g. a version string "01.09.00.00"
                return m.group(0)
            return self.ip(m.group(1)) if addr.is_private and not addr.is_unspecified else m.group(0)

        value = _IPV4_RE.sub(ip_sub, value)

        def mac_sub(m: re.Match) -> str:
            real = m.group(0).upper()
            if real not in self._macs:
                self._macs[real] = f"02:00:00:00:00:{len(self._macs) + 1:02X}"
            return self._macs[real]

        return _MAC_RE.sub(mac_sub, value)

    # Whole documents

    def learn(self, obj: Any) -> None:
        """Register every `sn` value first, so serials embedded in text that
        appears before their `sn` field are still caught."""
        if isinstance(obj, dict):
            for k, v in obj.items():
                if _is_serial_field(k, v):
                    self.serial(v)
                else:
                    self.learn(v)
        elif isinstance(obj, list):
            for v in obj:
                self.learn(v)

    def mask(self, obj: Any, key: str = "") -> Any:
        if isinstance(obj, dict):
            return {k: self.mask(v, k) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self.mask(v, key) for v in obj]
        lowered = key.lower()
        if _is_serial_field(lowered, obj):
            self.masked_keys.add(key)
            return self.serial(obj)
        if lowered in _HARDWARE_ID_KEYS and isinstance(obj, str) and obj:
            self.masked_keys.add(key)
            return self._hardware_id(obj)
        if "ssid" in lowered and obj not in ("", None):
            self.masked_keys.add(key)
            return "masked-ssid"
        if lowered in _ID_KEYS:
            masked = self._cloud_id(obj)
            if masked != obj:
                self.masked_keys.add(key)
            return masked
        if lowered == "ip" and isinstance(obj, int) and not isinstance(obj, bool):
            masked = self._packed_ip(obj)
            if masked != obj:
                self.masked_keys.add(key)
            return masked
        if isinstance(obj, str):
            out = self.text(obj)
            if lowered in _FILE_KEYS or _WEB_URL_RE.match(out):
                out = self.file_name(out)
            if out != obj:
                self.masked_keys.add(key)
            return out
        return obj


def _is_serial_field(key: str, value: Any) -> bool:
    """A serial-looking value (alphanumeric, 9+ characters) under a serial key;
    placeholders like `sn: "N/A"` are kept (and must not be learned, or every
    "N/A" in the capture would be masked)."""
    return (key.lower() in _SERIAL_KEYS and isinstance(value, str)
            and _SERIAL_VALUE_RE.fullmatch(value) is not None)


def mask_capture(lines: list[dict], masker: Masker, model: str) -> list[dict]:
    """Raw capture lines → fixture lines (header first). Events (connack,
    suback, ...) are dropped; messages keep `t`, `dir`, `topic`, `payload`."""
    header, *records = lines
    for record in records:
        masker.learn(record.get("payload"))
    out_records = []
    first_utc = last_t = None
    for record in records:
        if record.get("dir") not in ("in", "out"):
            continue
        first_utc = first_utc or record["utc"]
        last_t = record["t"]
        item = {"t": record["t"], "dir": record["dir"], "topic": masker.text(record["topic"])}
        if "payload" in record:
            item["payload"] = masker.mask(record["payload"])
        else:
            item["payload_text"] = masker.text(record.get("payload_text", ""))
        out_records.append(item)
    fixture_header = {
        "kind": "header",
        "format": FORMAT_VERSION,
        "printer": header["name"],
        "model": model,
        "serial": masker.serial(header["serial"]),
        "captured_utc": (first_utc or header["started_utc"])[:16] + "Z",
        "duration_s": last_t or 0,
        "masked_keys": sorted(masker.masked_keys),
    }
    return [fixture_header, *out_records]


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def cmd_mask(args: argparse.Namespace) -> int:
    raw_dir, dest = Path(args.raw), Path(args.dest)
    dest.mkdir(parents=True, exist_ok=True)
    raws = sorted(raw_dir.glob("*.raw.jsonl"))
    if not raws:
        print(f"No *.raw.jsonl in {raw_dir}")
        return 1
    captures = [(p, _read_jsonl(p)) for p in raws]
    # One masker for all printers, so a value means the same thing everywhere.
    masker = Masker([lines[0]["serial"] for _, lines in captures])
    for path, lines in captures:
        header = lines[0]
        end = next((r for r in reversed(lines) if r.get("event") == "end"), {})
        model = header.get("model") or end.get("model_heard", "")
        masker.masked_keys = set()
        fixture = mask_capture(lines, masker, model)
        out = dest / f"{_slug(header['name'])}.jsonl.gz"
        text = "".join(json.dumps(line, separators=(",", ":"), ensure_ascii=False) + "\n"
                       for line in fixture)
        # gzip: H2C/P2S push their full state every second (MBs of near-
        # duplicate JSON). mtime=0 keeps re-masking byte-identical.
        with out.open("wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", mtime=0, filename="") as f:
            f.write(text.encode("utf-8"))
        print(f"{out.name}: {len(fixture) - 1} messages, model {model or '?'}, "
              f"{len(text) // 1024} KB -> {out.stat().st_size // 1024} KB")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tools.capture_reports", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    cap = sub.add_parser("capture", help="record report messages (raw, outside the repo)")
    cap.add_argument("--minutes", type=float, default=10)
    cap.add_argument("--printer", action="append", help="printer name from the config (repeatable)")
    cap.add_argument("--raw", default=str(DEFAULT_RAW_DIR))
    cap.add_argument("--no-pushall", action="store_true", help="only listen (and get_version)")
    cap.set_defaults(func=cmd_capture)
    msk = sub.add_parser("mask", help="raw capture -> masked fixtures")
    msk.add_argument("--raw", default=str(DEFAULT_RAW_DIR))
    msk.add_argument("--dest", default=str(DEFAULT_DEST))
    msk.set_defaults(func=cmd_mask)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
