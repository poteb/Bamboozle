"""Loader for the captured MQTT report fixtures in `fixtures/reports/`
(format: `fixtures/reports/README.md`; re-capture with `tools/capture_reports.py`)."""
from __future__ import annotations

import gzip
import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "reports"
PRINTERS = ("a1", "p1s", "h2c", "p2s")


@dataclass(frozen=True)
class Message:
    t: float  # seconds since the capture session started
    dir: str  # "in" (report from the printer) | "out" (our request)
    topic: str
    payload: Any  # parsed JSON; None when the message wasn't JSON
    payload_text: str | None = None

    @property
    def print(self) -> dict | None:
        part = self.payload.get("print") if isinstance(self.payload, dict) else None
        return part if isinstance(part, dict) else None

    @property
    def info(self) -> dict | None:
        part = self.payload.get("info") if isinstance(self.payload, dict) else None
        return part if isinstance(part, dict) else None


@dataclass(frozen=True)
class Capture:
    header: dict
    messages: tuple[Message, ...]

    @property
    def model(self) -> str:
        return self.header["model"]

    @property
    def serial(self) -> str:
        return self.header["serial"]

    def reports(self) -> list[Message]:
        return [m for m in self.messages if m.dir == "in"]

    def status_pushes(self) -> list[Message]:
        """`push_status` reports in arrival order (full and partial)."""
        return [m for m in self.reports() if m.print and m.print.get("command") == "push_status"]

    def full_reports(self) -> list[Message]:
        """Full status reports (`msg` 0). A1/P1S send one only when asked
        (the pushall reply); H2C/P2S sent one about every second."""
        return [m for m in self.status_pushes() if m.print.get("msg") == 0]

    def partial_reports(self) -> list[Message]:
        """Deltas (`msg` 1): only the fields that changed."""
        return [m for m in self.status_pushes() if m.print.get("msg") != 0]

    def full_report(self) -> dict:
        """The `print` dict of the first full status report."""
        full = self.full_reports()
        if not full:
            raise LookupError(f"no full report in {self.header['printer']}")
        return full[0].print

    def version(self) -> dict:
        """The `info` dict answering `get_version`."""
        for m in self.reports():
            if m.info and m.info.get("command") == "get_version":
                return m.info
        raise LookupError(f"no get_version reply in {self.header['printer']}")


def _open(path: Path):
    if path.suffix == ".gz":
        return gzip.open(path, "rt", encoding="utf-8")
    return path.open(encoding="utf-8")


@lru_cache(maxsize=None)
def _text(name: str) -> str:
    path = next((p for p in (FIXTURE_DIR / f"{name}.jsonl.gz", FIXTURE_DIR / f"{name}.jsonl")
                 if p.exists()), None)
    if path is None:
        raise FileNotFoundError(f"no fixture for {name} in {FIXTURE_DIR}")
    with _open(path) as f:
        return f.read()


def load(name: str) -> Capture:
    """A freshly parsed capture: payloads are plain dicts the caller may
    mutate (e.g. merge into) without affecting other tests."""
    header, *lines = [json.loads(line) for line in _text(name).splitlines() if line.strip()]
    assert header.get("kind") == "header", name
    messages = tuple(
        Message(t=r["t"], dir=r["dir"], topic=r["topic"], payload=r.get("payload"),
                payload_text=r.get("payload_text"))
        for r in lines
    )
    return Capture(header=header, messages=messages)
