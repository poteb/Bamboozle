"""Bambu's own descriptions of HMS and print_error codes, fetched at runtime.

Bambu publishes the catalogue Bambu Studio downloads at
`https://e.bambulab.com/query.php?lang=en`; `&d=<serial prefix>` gives one
model's catalogue, which has codes the generic one lacks. Its text is Bambu's,
so it's never bundled: it's fetched, cached under
`%LOCALAPPDATA%\\Bamboozle\\hms-cache\\`, refreshed weekly, and a stale cache is
used while a fetch fails. Only the serial's 3-character model prefix is sent.

- `HmsCatalog.refresh(serials)` / `start(serials_fn)`: fetch what's missing or
  a week old, in the background, off the poll path.
- `HmsCatalog.describe(errors, serial)`: pure lookup over what's loaded; adds
  the text to decoded `PrinterError`s. No catalogue means no change (the card
  shows the code alone), and it never raises.

The download is untrusted: size-capped, type-checked entry by entry, and the
card renders its text with `textContent` only.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
import time
import urllib.request
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import NamedTuple

from .config import _config_dir
from .models import PrinterError

logger = logging.getLogger(__name__)

CATALOG_URL = "https://e.bambulab.com/query.php?lang=en"
# Bambu's CDN refuses Python's default user agent.
USER_AGENT = "Bamboozle (LAN printer dashboard)"
MAX_BYTES = 4 * 1024 * 1024  # the H2C's catalogue is ~530 KB
MAX_INTRO_CHARS = 1000       # the longest is ~340
FETCH_TIMEOUT_SECONDS = 20
REFRESH_SECONDS = 7 * 86400
RETRY_MIN_SECONDS = 10 * 60  # after a failed fetch; doubles ...
RETRY_MAX_SECONDS = 6 * 3600  # ... up to this
CHECK_SECONDS = 60           # how often the refresh loop looks for work

NO_TEXT = "No description from Bambu (internal code)"

_PREFIX = re.compile(r"[0-9A-Z]{3}")
_HEX = re.compile(r"[0-9A-F]+")
# The catalogue's sections, and the ecode length in each (HMS: attr + code;
# print_error: module + unit + error).
_SECTIONS = {"hms": ("device_hms", 16), "error": ("device_error", 8)}
_RESULT_OK = 0
_RESULT_NO_CATALOGUE = 201  # e.g. a prefix Bambu has no catalogue for


class Tables(NamedTuple):
    hms: dict[str, str]    # 16 hex, upper case -> text ("" = listed without text)
    error: dict[str, str]  # 8 hex (print_error)


class _Entry(NamedTuple):
    fetched_at: float  # wall clock, as it's persisted
    tables: Tables


def serial_prefix(serial: str) -> str | None:
    """The model part of a serial (its first 3 characters), or None."""
    prefix = (serial or "").strip().upper()[:3]
    return prefix if _PREFIX.fullmatch(prefix) else None


def _url(prefix: str) -> str:
    return f"{CATALOG_URL}&d={prefix}" if prefix else CATALOG_URL


def _http_get(url: str, timeout: float, max_bytes: int) -> bytes:
    """GET the body; raises on an error status or a body over `max_bytes`."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = resp.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError(f"catalogue over {max_bytes} bytes")
    return data


http_get = _http_get  # the network adapter (tests replace it)


def _table(section, ecode_len: int) -> dict[str, str] | None:
    """{ecode: text} from one section's `en` list; None if it isn't a list."""
    if not isinstance(section, dict) or not isinstance(section.get("en"), list):
        return None
    out = {}
    for item in section["en"]:
        if not isinstance(item, dict):
            continue
        ecode, intro = item.get("ecode"), item.get("intro", "")
        if not isinstance(ecode, str) or not isinstance(intro, str):
            continue
        ecode = ecode.upper()
        if len(ecode) != ecode_len or not _HEX.fullmatch(ecode):
            continue
        out[ecode] = " ".join(intro.split())[:MAX_INTRO_CHARS]
    return out


def _tables(hms_section, error_section) -> Tables | None:
    hms_table = _table(hms_section, _SECTIONS["hms"][1])
    error_table = _table(error_section, _SECTIONS["error"][1])
    if hms_table is None and error_table is None:
        return None
    return Tables(hms_table or {}, error_table or {})


def parse(raw: bytes) -> Tables | None:
    """The tables from a catalogue response, or None if it isn't one."""
    if len(raw) > MAX_BYTES:
        return None
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(doc, dict):
        return None
    if doc.get("result") == _RESULT_NO_CATALOGUE:
        return Tables({}, {})
    data = doc.get("data")
    if doc.get("result") != _RESULT_OK or not isinstance(data, dict):
        return None
    return _tables(data.get(_SECTIONS["hms"][0]), data.get(_SECTIONS["error"][0]))


def _cache_path(prefix: str) -> Path:
    # The prefix passed serial_prefix: 3 of [0-9A-Z], safe in a file name.
    return _config_dir() / "hms-cache" / (f"en-{prefix}.json" if prefix else "en.json")


def _load(prefix: str) -> _Entry | None:
    path = _cache_path(prefix)
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
        fetched_at = doc["fetched_at"]
        if isinstance(fetched_at, bool) or not isinstance(fetched_at, (int, float)):
            raise ValueError("bad fetched_at")
        # Saved in the same shape as a section, so it's checked the same way.
        tables = _tables({"en": doc["hms"]}, {"en": doc["error"]})
        if tables is None:
            raise ValueError("bad tables")
    except FileNotFoundError:
        return None
    except Exception as e:  # corrupt: ignore it, it's refetched
        logger.warning("Ignoring the HMS catalogue cache %s: %s", path, e)
        return None
    return _Entry(float(fetched_at), tables)


def _save(prefix: str, entry: _Entry) -> None:
    path = _cache_path(prefix)
    doc = {"fetched_at": entry.fetched_at, **{
        name: [{"ecode": k, "intro": v} for k, v in getattr(entry.tables, name).items()]
        for name in _SECTIONS
    }}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(doc), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as e:
        logger.warning("Couldn't cache the HMS catalogue at %s: %s", path, e)


async def _in_daemon_thread(fn, *args):
    """asyncio.to_thread on a daemon thread, so shutdown never waits on a fetch
    (as printer_manager._in_daemon_thread; not imported: that module imports
    this one)."""
    loop = asyncio.get_running_loop()
    fut = loop.create_future()

    def settle(result, exc):
        if fut.done():  # the awaiting task was cancelled
            return
        if exc is None:
            fut.set_result(result)
        else:
            fut.set_exception(exc)

    def run():
        try:
            result, exc = fn(*args), None
        except Exception as e:
            result, exc = None, e
        try:
            loop.call_soon_threadsafe(settle, result, exc)
        except RuntimeError:
            pass  # the loop is closed

    threading.Thread(target=run, name="hms-catalog-fetch", daemon=True).start()
    return await fut


class HmsCatalog:
    """The loaded catalogues (generic + one per model prefix) and their refresh."""

    def __init__(self) -> None:
        self._lock = threading.Lock()  # describe() runs on the state-read thread
        self._entries: dict[str, _Entry] = {}  # "" = generic, else serial prefix
        self._loaded: set[str] = set()  # cache files read
        self._failures: dict[str, int] = {}
        self._retry_at: dict[str, float] = {}  # monotonic
        self._task: asyncio.Task | None = None

    # --- lookup ---------------------------------------------------------------

    def _lookup(self, table: str, ecode: str, prefix: str | None) -> str | None:
        """Text for the code: the model's first, then the generic one's.

        "" when it's only listed without text, None when nobody lists it.
        """
        with self._lock:
            entries = [self._entries.get(k) for k in (prefix, "") if k is not None]
        found = None
        for entry in entries:
            if entry is None:
                continue
            text = getattr(entry.tables, table).get(ecode)
            if text:
                return text
            if text is not None:
                found = ""
        return found

    def describe(self, errors: list[PrinterError], serial: str) -> list[PrinterError]:
        """The errors with Bambu's text added, codes listed without text last
        (after the active ones). Our own descriptions are kept."""
        try:
            prefix = serial_prefix(serial)
            out = []
            for e in errors:
                text = None
                if not e.description:
                    table = "hms" if e.kind == "hms" else "error"
                    text = self._lookup(table, e.code.replace("_", ""), prefix)
                if text:
                    e = e.model_copy(update={"description": text})
                elif text == "":
                    e = e.model_copy(update={"description": NO_TEXT, "undocumented": True})
                out.append(e)
            # Stable: keeps hms.printer_errors' order (active, then severity).
            return sorted(out, key=lambda e: (not e.active, e.undocumented))
        except Exception as e:  # a card without text beats no card
            logger.warning("Describing printer errors failed: %s", e)
            return errors

    # --- refresh --------------------------------------------------------------

    def _due(self, prefix: str) -> bool:
        if self._retry_at.get(prefix, 0.0) > time.monotonic():
            return False
        with self._lock:
            entry = self._entries.get(prefix)
        if entry is None:
            return True
        age = time.time() - entry.fetched_at
        return age >= REFRESH_SECONDS or age < 0  # < 0: the clock went back

    def _store(self, prefix: str, entry: _Entry) -> None:
        with self._lock:
            self._entries[prefix] = entry

    async def refresh(self, serials: Iterable[str]) -> None:
        """Load the cache, then fetch each catalogue that's missing or stale:
        the generic one and one per printer model."""
        prefixes = [""] + sorted({p for p in map(serial_prefix, serials) if p})
        for prefix in prefixes:
            if prefix not in self._loaded:
                self._loaded.add(prefix)
                entry = await asyncio.to_thread(_load, prefix)
                if entry:
                    self._store(prefix, entry)
            if self._due(prefix):
                await self._fetch(prefix)

    async def _fetch(self, prefix: str) -> None:
        name = prefix or "generic"
        try:
            raw = await _in_daemon_thread(http_get, _url(prefix), FETCH_TIMEOUT_SECONDS, MAX_BYTES)
            tables = parse(raw)
            if tables is None:
                raise ValueError("not a catalogue")
        except Exception as e:
            n = self._failures[prefix] = self._failures.get(prefix, 0) + 1
            delay = min(RETRY_MIN_SECONDS * 2 ** (n - 1), RETRY_MAX_SECONDS)
            self._retry_at[prefix] = time.monotonic() + delay
            logger.warning("Fetching the %s HMS catalogue failed (retry in %d min): %s",
                           name, delay // 60, e)
            return
        self._failures.pop(prefix, None)
        self._retry_at.pop(prefix, None)
        entry = _Entry(time.time(), tables)
        self._store(prefix, entry)
        await asyncio.to_thread(_save, prefix, entry)
        logger.info("HMS catalogue %s: %d messages, %d print errors",
                    name, len(tables.hms), len(tables.error))

    async def _run(self, serials: Callable[[], Iterable[str]]) -> None:
        while True:
            try:
                await self.refresh(list(serials()))
            except Exception:
                logger.exception("HMS catalogue refresh failed")
            await asyncio.sleep(CHECK_SECONDS)

    def start(self, serials: Callable[[], Iterable[str]]) -> None:
        """Keep the catalogues for `serials()` (re-read each round) fresh."""
        if not self.running:
            self._task = asyncio.create_task(self._run(serials))

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()


CATALOG = HmsCatalog()


def describe(errors: list[PrinterError], serial: str) -> list[PrinterError]:
    """`CATALOG.describe`: the errors with Bambu's text, where it has some."""
    return CATALOG.describe(errors, serial)
