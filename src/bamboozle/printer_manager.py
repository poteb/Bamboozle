from __future__ import annotations

import asyncio
import logging
import math
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NamedTuple

import bambulabs_api as bl
from fastapi import WebSocket

from . import discovery, mqtt, protocol, thumbnail
from .camera import CameraStream
from .config import _config_dir, save_config
from .models import AppConfig, Filament, PrinterConfig, PrinterState

logger = logging.getLogger(__name__)


async def _in_daemon_thread(fn, *args, **kwargs):
    """Like asyncio.to_thread, but on a daemon thread of its own, so shutdown
    never waits for it (asyncio joins the default executor's threads)."""
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
            result, exc = fn(*args, **kwargs), None
        except Exception as e:
            result, exc = None, e
        try:
            loop.call_soon_threadsafe(settle, result, exc)
        except RuntimeError:
            pass  # the loop is closed: nobody is waiting any more

    threading.Thread(target=run, name=getattr(fn, "__name__", "worker"), daemon=True).start()
    return await fut


class _Job(NamedTuple):
    """What a state read saw of the current print job, for the thumbnail."""
    key: str  # subtask_name, else gcode_file; "" when no job is known
    gcode_file: str
    subtask: str
    gcode_state: str
    internal: bool  # the file is on internal storage, out of FTPS reach


def _backup_printer_config(cfg: PrinterConfig) -> Path:
    """Snapshot a PrinterConfig to %LOCALAPPDATA%\\Bamboozle\\history\\.

    Filename: ``{sanitized_name}.{YYYYMMDD-HHMMSS}.json`` (UTC, second precision).
    Names are sanitized by replacing any character not in ``[A-Za-z0-9_-]`` with
    ``_``. If the sanitized name is empty, fall back to ``cfg.id``.
    """
    history_dir = _config_dir() / "history"
    history_dir.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", cfg.name) or cfg.id
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    path = history_dir / f"{safe}.{ts}.json"
    path.write_text(cfg.model_dump_json(indent=2), encoding="utf-8")
    return path


class PrinterConnection:
    """Wraps a single printer's MQTT connection, camera stream, and state."""

    INITIAL_BACKOFF_SECONDS = 3.0
    MAX_BACKOFF_SECONDS = 60.0
    MQTT_SETTLE_SECONDS = 2.0  # give MQTT a moment to establish after start
    # How long a started session may stay not-connected before it counts as down.
    MQTT_GRACE_SECONDS = 20.0
    # A connected session without a report for this long counts as down.
    STALE_SECONDS = 60.0
    # ...and one this quiet is first asked for a full report (an idle printer
    # may push rarely).
    PROBE_SECONDS = 30.0
    # After a failed thumbnail fetch, wait this long before trying again
    # (doubling per failure, up to the max). A job whose 3MF isn't on the
    # printer is not looked up again.
    THUMB_RETRY_SECONDS = 60.0
    THUMB_MAX_RETRY_SECONDS = 900.0
    _JOB_ACTIVE = frozenset({"PREPARE", "RUNNING"})
    _JOB_ENDED = frozenset({"FINISH", "FAILED", "IDLE"})

    def __init__(self, cfg: PrinterConfig):
        self.cfg = cfg
        self.cfg.camera_enabled = False  # Always start with cameras off
        self.camera = CameraStream(cfg.ip, cfg.access_code, cfg.camera_port)
        # Camera toggles run start()/stop() in worker threads; this keeps
        # them in arrival order.
        self.camera_toggle_lock = asyncio.Lock()
        self.state = PrinterState(printer_id=cfg.id, name=cfg.name, sort=cfg.sort)
        self.thumbnail: bytes | None = None
        self._thumb_key = ""  # job the thumbnail belongs to
        self._thumb_job = ""  # job the lookup state below is for
        self._thumb_retry_at = 0.0  # monotonic; math.inf = not on the printer
        self._thumb_backoff = self.THUMB_RETRY_SECONDS
        self._thumb_task: asyncio.Task | None = None
        self._thumb_cancel = threading.Event()
        self._thumb_last_state = ""  # gcode_state at the previous read
        # The manager's in-flight poll step for this printer (see _poll_all).
        self.poll_step: asyncio.Task | None = None
        self._backoff = self.INITIAL_BACKOFF_SECONDS
        self._retry_at = 0.0  # monotonic time the next reconnect is due
        self._connected = False
        self._last_seen_up = 0.0  # monotonic time MQTT was last seen connected
        self._connecting = False
        # Set from paho's thread on every CONNACK; kept across reconnects
        # until a login is accepted (or the printer stops answering at all).
        self._auth_rejected = False
        # Whether the current connect attempt's login was accepted or refused
        # yet, so a retry doesn't act on the previous attempt's refusal.
        self._login_answered = False
        # Reports on the current session; set from paho's thread.
        self._session_started = 0.0  # monotonic
        self._last_report = 0.0  # monotonic; 0 = none yet this session
        self._reports = 0
        self._probed = False  # pushall sent for the current quiet spell
        # Why the last session was dropped for silence (protocol.NO_REPORTS /
        # STALE); kept until a report arrives or the printer stops answering.
        self._down_reason = ""
        self.printer = self._new_printer()

    def _new_printer(self) -> bl.Printer:
        """A fresh library client, wired to this connection."""
        printer = bl.Printer(self.cfg.ip, self.cfg.access_code, self.cfg.serial)
        printer.mqtt_client.on_connect_handler = self._on_mqtt_connack
        printer.mqtt_client.on_message_handler = self._on_mqtt_message
        mqtt.configure(printer)
        return printer

    def _on_mqtt_message(self, _mqtt, _client, _userdata, _msg) -> None:
        self._last_report = time.monotonic()
        self._reports += 1
        self._probed = False
        self._down_reason = ""

    def _on_mqtt_connack(self, _mqtt, _client, _userdata, _flags, rc, _props) -> None:
        # _login_answered is set last, so _login_refused() never pairs a new
        # answer with the previous attempt's flag.
        if protocol.is_auth_refusal(rc):
            if not self._auth_rejected:
                logger.warning(
                    "%s: MQTT login refused at %s (%s): access code wrong or changed",
                    self.cfg.name, self.cfg.ip, rc,
                )
            self._auth_rejected = True
            self._login_answered = True
        elif not protocol.connack_failed(rc):
            if self._auth_rejected:
                logger.info("%s: MQTT login accepted again", self.cfg.name)
            self._auth_rejected = False
            self._login_answered = True
        # Other refusals (e.g. server unavailable) say nothing about the code.

    @property
    def connecting(self) -> bool:
        return self._connecting

    async def connect(self) -> bool:
        self._connecting = True
        try:
            return await self._connect()
        finally:
            self._connecting = False

    async def _connect(self) -> bool:
        reachable = await asyncio.to_thread(
            discovery.tcp_reachable, self.cfg.ip, discovery.MQTT_PORT
        )
        if not reachable:
            logger.warning("%s: printer unreachable at %s", self.cfg.name, self.cfg.ip)
            self._auth_rejected = False  # says nothing about the code any more
            self._down_reason = ""  # ...or about the serial
            self._schedule_retry()
            return False
        self._login_answered = False
        self._reports = 0
        self._last_report = 0.0
        self._probed = False
        self._session_started = time.monotonic()
        try:
            await asyncio.wait_for(
                asyncio.to_thread(self.printer.mqtt_start), timeout=10
            )
            await asyncio.sleep(self.MQTT_SETTLE_SECONDS)
            self._connected = True
            if self._login_refused():
                # paho would keep retrying the bad code every few seconds;
                # stop it and retry on our own (backed-off) schedule instead.
                logger.error(
                    "%s: connection refused at %s: access code wrong or changed",
                    self.cfg.name, self.cfg.ip,
                )
                await self.reset_session()
                return False
            self._last_seen_up = time.monotonic()  # grace starts now
            logger.info("Connected to printer %s (%s)", self.cfg.name, self.cfg.ip)
            return True
        except asyncio.TimeoutError:
            logger.error("Timeout connecting to %s (%s)", self.cfg.name, self.cfg.ip)
            self._schedule_retry()
            return False
        except Exception as e:
            logger.error("Failed to connect to %s: %s", self.cfg.name, e)
            self._schedule_retry()
            return False

    def _schedule_retry(self) -> None:
        self._connected = False
        self._retry_at = time.monotonic() + self._backoff
        self._backoff = min(self._backoff * 2, self.MAX_BACKOFF_SECONDS)

    def needs_reconnect(self) -> bool:
        """True when not connected, not mid-connect, and the backoff has elapsed."""
        return (
            not self._connected
            and not self._connecting
            and time.monotonic() >= self._retry_at
        )

    async def disconnect(self) -> None:
        """Stop the camera and end the MQTT session. Never waits on paho."""
        self._cancel_thumbnail_fetch()
        try:
            await asyncio.to_thread(self.camera.stop)
        except Exception as e:
            logger.debug("Error stopping camera for %s: %s", self.cfg.name, e)
        mqtt.retire(self.printer)
        self._connected = False

    def _silence(self) -> float:
        """Seconds since the last report (or the session start, if none)."""
        return time.monotonic() - (self._last_report or self._session_started)

    async def check_silence(self) -> None:
        """Ask a quiet session for a full report; past STALE_SECONDS, record
        why it is about to be dropped."""
        if not self._connected or self._connecting:
            return
        silence = self._silence()
        if silence >= self.STALE_SECONDS:
            reason = protocol.STALE if self._reports else protocol.NO_REPORTS
            if reason != self._down_reason:
                if reason == protocol.NO_REPORTS:
                    logger.warning(
                        "%s: connected to %s but no report in %.0fs since connect: "
                        "check the serial (%s; it is case-sensitive)",
                        self.cfg.name, self.cfg.ip, silence, self.cfg.serial,
                    )
                else:
                    logger.warning(
                        "%s: no report from %s for %.0fs", self.cfg.name, self.cfg.ip, silence
                    )
            self._down_reason = reason
        elif silence >= self.PROBE_SECONDS and not self._probed:
            self._probed = True
            try:
                await asyncio.to_thread(mqtt.publish, self.printer, mqtt.pushall_request())
            except Exception as e:
                logger.debug("pushall probe failed for %s: %s", self.cfg.name, e)

    def is_offline(self) -> bool:
        """True when the printer is known to be down at its saved IP.

        A connect in progress, or a session that was up (or started) within
        the grace period, does not count as offline yet - paho may still be
        handshaking or reconnecting by itself.
        """
        if self._connecting:
            return False
        if not self._connected or self._login_refused():
            return True  # a refused code won't fix itself; don't wait out the grace
        if self._silence() >= self.STALE_SECONDS:
            return True  # the socket may be up, but nothing is coming through
        try:
            if self.printer.mqtt_client.is_connected():
                self._last_seen_up = time.monotonic()
                if self._reports:
                    self._backoff = self.INITIAL_BACKOFF_SECONDS  # session verified
                return False
        except Exception:
            pass
        return time.monotonic() - self._last_seen_up > self.MQTT_GRACE_SECONDS

    def _login_refused(self) -> bool:
        """The current attempt's login was refused (not just a previous one)."""
        return self._login_answered and self._auth_rejected

    async def reset_session(self) -> None:
        """Drop a dead MQTT session so the next reconnect re-probes the IP.

        The old client is retired in the background and replaced, so the
        reconnect never waits for (or races) its network thread.
        """
        mqtt.retire(self.printer)
        self._schedule_retry()
        # Building a client loads the system CA store; keep it off the loop.
        self.printer = await asyncio.to_thread(self._new_printer)

    async def read_state(self) -> PrinterState:
        if not self._connected or self._auth_rejected:
            # Never started MQTT (e.g. unreachable) or the login was refused -
            # don't poke the library getters, they'd only log "Printer Values
            # Not Available Yet".
            self.state.online = False
            self.state.last_error = (
                protocol.AUTH_REJECTED if self._auth_rejected else self._down_reason
            )
            self.state.timestamp = time.time()
            return self.state
        try:
            state_data, job = await asyncio.to_thread(self._read_sync)
            state_data.last_error = self._down_reason  # until the first report
            self._update_thumbnail(job)
            state_data.has_thumbnail = self.thumbnail is not None
            state_data.thumbnail_key = self._thumb_key  # set and cleared with it
            state_data.thumbnail_unavailable = (
                protocol.THUMBNAIL_INTERNAL_STORAGE if job.key and job.internal else ""
            )
            self.state = state_data
            return state_data
        except Exception as e:
            logger.error("Error reading state from %s: %s", self.cfg.name, e)
            self.state.online = False
            self.state.timestamp = time.time()
            return self.state

    def _read_sync(self) -> tuple[PrinterState, _Job]:
        p = self.printer
        try:
            gcode_state = p.get_state()
            gcode_str = gcode_state.value if gcode_state else "UNKNOWN"
        except Exception:
            gcode_str = "UNKNOWN"

        try:
            print_status = p.get_current_state()
            status_str = print_status.name if print_status else "UNKNOWN"
        except Exception:
            status_str = "UNKNOWN"

        try:
            progress = p.get_percentage() or 0
        except Exception:
            progress = 0

        try:
            remaining = p.get_time()
            logger.debug("get_time() for %s returned: %r (type: %s)", self.cfg.name, remaining, type(remaining).__name__)
            if isinstance(remaining, (int, float)):
                remaining_min = int(remaining)
            else:
                remaining_min = 0
        except Exception:
            remaining_min = 0

        try:
            nozzle_temp = p.get_nozzle_temperature()
        except Exception:
            nozzle_temp = None

        try:
            bed_temp = p.get_bed_temperature()
        except Exception:
            bed_temp = None

        try:
            current_layer = p.current_layer_num() or 0
        except Exception:
            current_layer = 0

        try:
            total_layers = p.total_layer_num() or 0
        except Exception:
            total_layers = 0

        try:
            print_speed = p.get_print_speed() or 0
        except Exception:
            print_speed = 0

        try:
            light_state = p.get_light_state()
            light_on = light_state == "on"
        except Exception:
            light_on = False

        try:
            file_name = p.get_file_name() or ""
        except Exception:
            file_name = ""

        try:
            subtask = p.subtask_name() or ""
        except Exception:
            subtask = ""

        filaments = self._read_filaments()

        report = self._print_payload() or {}
        developer_mode = protocol.developer_mode(report)
        commands_blocked = protocol.commands_blocked(report)

        online = gcode_str != "UNKNOWN" or nozzle_temp is not None

        display_name = subtask or file_name
        job = _Job(
            key=display_name,
            gcode_file=file_name,
            subtask=subtask,
            gcode_state=gcode_str,
            internal=protocol.file_on_internal_storage(report),
        )

        # The thumbnail fields are filled in by read_state.
        state = PrinterState(
            printer_id=self.cfg.id,
            name=self.cfg.name,
            online=online,
            gcode_state=gcode_str,
            print_status=status_str,
            progress=progress,
            remaining_minutes=remaining_min,
            current_layer=current_layer,
            total_layers=total_layers,
            nozzle_temp=nozzle_temp,
            nozzle_target=None,
            bed_temp=bed_temp,
            bed_target=None,
            chamber_temp=None,
            file_name=display_name,
            print_speed=print_speed,
            light_on=light_on,
            camera_enabled=self.cfg.camera_enabled,
            camera_available=self.camera.available,
            filaments=filaments,
            developer_mode=developer_mode,
            commands_blocked=commands_blocked,
            sort=self.cfg.sort,
            timestamp=time.time(),
        )
        return state, job

    # --- Thumbnail ----------------------------------------------------------

    def _update_thumbnail(self, job: _Job) -> None:
        """After each read: start a background fetch when the job needs one.

        At most one fetch runs per printer (the printer allows few FTP
        sessions). Runs on the event loop; the fetch itself never does.
        """
        # Bambu keeps the names after a print ends, so a reprint of the same
        # project shows up as the state going from ended back to active.
        restarted = (
            job.gcode_state in self._JOB_ACTIVE and self._thumb_last_state in self._JOB_ENDED
        )
        self._thumb_last_state = job.gcode_state
        if not job.key:
            if job.gcode_state == "IDLE":
                self._forget_thumbnail()
            return
        if job.key != self._thumb_job or restarted:
            self._forget_thumbnail()  # not the previous job's picture
            self._thumb_job = job.key
        if (
            job.internal
            or self.thumbnail is not None
            or (self._thumb_task is not None and not self._thumb_task.done())
            or time.monotonic() < self._thumb_retry_at
        ):
            return
        self._thumb_cancel = threading.Event()
        self._thumb_task = asyncio.create_task(self._fetch_thumbnail(job, self._thumb_cancel))

    def _forget_thumbnail(self) -> None:
        self._cancel_thumbnail_fetch()
        self.thumbnail = None
        self._thumb_key = ""
        self._thumb_job = ""
        self._thumb_retry_at = 0.0
        self._thumb_backoff = self.THUMB_RETRY_SECONDS

    def _cancel_thumbnail_fetch(self) -> None:
        self._thumb_cancel.set()
        if self._thumb_task is not None and not self._thumb_task.done():
            self._thumb_task.cancel()
        self._thumb_task = None

    async def _fetch_thumbnail(self, job: _Job, cancel: threading.Event) -> None:
        try:
            data = await _in_daemon_thread(
                thumbnail.fetch_thumbnail,
                self.cfg.ip, self.cfg.access_code, job.gcode_file, job.subtask,
                cancel=cancel,
            )
        except asyncio.CancelledError:
            cancel.set()
            raise
        except thumbnail.FetchCancelled:
            return
        except Exception as e:
            if self._thumb_job == job.key:
                self._thumb_retry_at = time.monotonic() + self._thumb_backoff
                logger.info(
                    "%s: thumbnail fetch failed (%s: %s); retrying in %.0fs",
                    self.cfg.name, type(e).__name__, e, self._thumb_backoff,
                )
                self._thumb_backoff = min(self._thumb_backoff * 2, self.THUMB_MAX_RETRY_SECONDS)
            return
        if cancel.is_set() or self._thumb_job != job.key:
            return
        if data:
            self.thumbnail = data
            self._thumb_key = job.key
            logger.info("Fetched thumbnail for %s on %s", job.key, self.cfg.name)
        else:
            self._thumb_retry_at = math.inf
            logger.info(
                "%s: no 3MF for %r on the printer's storage; no thumbnail for this job",
                self.cfg.name, job.key,
            )

    def _print_payload(self) -> dict | None:
        """The raw `print` object of the latest reports, or None before any."""
        try:
            dump = self.printer.mqtt_dump() or {}
        except Exception as e:
            logger.debug("mqtt_dump() failed for %s: %s", self.cfg.name, e)
            return None
        payload = dump.get("print") if isinstance(dump, dict) else None
        return payload if isinstance(payload, dict) else None

    # --- Filament helpers -------------------------------------------------

    @staticmethod
    def _normalize_tray_color(raw: Any) -> str:
        """Convert a Bambu tray_color (e.g. 'RRGGBB' or 'RRGGBBAA') to a CSS color.

        Bambu firmware emits 8-char RRGGBBAA hex (alpha last). Browsers also
        accept 8-digit hex. Returns empty string when input is unusable.

        The all-zero value ('00000000' / '000000') is the firmware default
        for empty / unidentified slots — treat it as no color so callers can
        skip those entries.
        """
        if not raw:
            return ""
        if not isinstance(raw, str):
            return ""
        s = raw.strip().lstrip("#")
        if len(s) not in (6, 8):
            return ""
        try:
            value = int(s, 16)
        except ValueError:
            return ""
        if value == 0:
            return ""
        return f"#{s.upper()}"

    @staticmethod
    def _filament_label(tray: Any) -> tuple[str, str]:
        """Return (filament_type, friendly_name) from a FilamentTray.

        Falls back gracefully if any field is missing.
        """
        ftype = ""
        name = ""
        try:
            ftype = getattr(tray, "tray_type", "") or ""
        except Exception:
            ftype = ""
        try:
            name = (
                getattr(tray, "tray_sub_brands", "")
                or getattr(tray, "tray_id_name", "")
                or ""
            )
        except Exception:
            name = ""
        return ftype, name

    @staticmethod
    def _filament_from_dict(
        tray: dict, source: str, ams_index: int, tray_index: int
    ) -> Filament | None:
        """Build a Filament from a raw MQTT tray dict.

        Returns None if the tray has no useful info (empty slot).
        Mirrors the same defensive shape used for the FilamentTray dataclass.
        """
        if not isinstance(tray, dict):
            return None
        color = PrinterConnection._normalize_tray_color(tray.get("tray_color", ""))
        ftype = tray.get("tray_type", "") or ""
        fname = tray.get("tray_sub_brands", "") or tray.get("tray_id_name", "") or ""
        if not color and not ftype and not fname:
            return None
        return Filament(
            source=source,
            ams_index=ams_index,
            tray_index=tray_index,
            color=color,
            filament_type=str(ftype),
            name=str(fname),
        )

    def _read_filaments_from_dump(self) -> list[Filament] | None:
        """Read filaments straight from the raw MQTT print payload.

        Used for dual-extruder models (H2C / H2D) where the bambulabs_api
        helpers don't surface filament data:
          * `vt_tray()` is replaced by a `vir_slot` list (one entry per
            extruder, ids 254 / 253).
          * AMS trays omit the legacy `n` field, so the library's
            `process_ams()` silently drops them.

        Reading the raw payload also covers single-extruder models, but we
        only return a non-None list when we actually find something useful;
        otherwise we let the caller fall back to the library helpers.
        """
        print_payload = self._print_payload()
        if print_payload is None:
            return None

        out: list[Filament] = []
        found_ams_block = False
        found_external_block = False

        # --- AMS trays --------------------------------------------------
        try:
            ams_block = print_payload.get("ams")
            if isinstance(ams_block, dict):
                exist_bits = ams_block.get("ams_exist_bits", "0")
                ams_units = ams_block.get("ams", [])
                if (str(exist_bits) not in ("0", "")) and isinstance(ams_units, list):
                    found_ams_block = True
                    for unit_idx, unit in enumerate(ams_units):
                        if not isinstance(unit, dict):
                            continue
                        try:
                            ams_index = int(unit.get("id", unit_idx))
                        except (TypeError, ValueError):
                            ams_index = unit_idx
                        trays = unit.get("tray", [])
                        if not isinstance(trays, list):
                            continue
                        for slot_idx, tray in enumerate(trays):
                            if not isinstance(tray, dict):
                                continue
                            try:
                                tray_index = int(tray.get("id", slot_idx))
                            except (TypeError, ValueError):
                                tray_index = slot_idx
                            f = self._filament_from_dict(
                                tray, "ams", ams_index, tray_index
                            )
                            if f is not None:
                                out.append(f)
        except Exception as e:
            logger.debug("Raw AMS parse failed for %s: %s", self.cfg.name, e)

        # --- External spool(s) -----------------------------------------
        # H2C / H2D: list of per-extruder virtual slots.
        try:
            vir_slot = print_payload.get("vir_slot")
            if isinstance(vir_slot, list) and vir_slot:
                found_external_block = True
                for ext_idx, slot in enumerate(vir_slot):
                    if not isinstance(slot, dict):
                        continue
                    f = self._filament_from_dict(
                        slot, "external", ext_idx, 0
                    )
                    if f is not None:
                        out.append(f)
        except Exception as e:
            logger.debug("vir_slot parse failed for %s: %s", self.cfg.name, e)

        # Single-extruder fallback inside the dump.
        if not found_external_block:
            try:
                vt = print_payload.get("vt_tray")
                if isinstance(vt, dict) and vt:
                    found_external_block = True
                    f = self._filament_from_dict(vt, "external", 0, 0)
                    if f is not None:
                        out.append(f)
            except Exception as e:
                logger.debug("vt_tray dump parse failed for %s: %s", self.cfg.name, e)

        # If neither AMS nor external blocks were present in the dump,
        # the payload hasn't arrived yet — let the caller fall back to the
        # library helpers (which are also no-ops in that case but keep the
        # original control flow intact).
        if not found_ams_block and not found_external_block:
            return None
        return out

    def _read_filaments_from_library(self) -> list[Filament]:
        """Original code path: use bambulabs_api helpers.

        Works for P1P/P1S/X1/A1 single-extruder models. Skips dual-extruder
        models (H2C/H2D) because their MQTT payload differs (see
        `_read_filaments_from_dump`).
        """
        out: list[Filament] = []
        p = self.printer

        # AMS units (zero or more)
        try:
            hub = p.ams_hub()
            ams_map = getattr(hub, "ams_hub", {}) or {}
            for ams_index, ams_unit in ams_map.items():
                trays = getattr(ams_unit, "filament_trays", {}) or {}
                for tray_index, tray in trays.items():
                    if tray is None:
                        continue
                    color = self._normalize_tray_color(
                        getattr(tray, "tray_color", "")
                    )
                    ftype, fname = self._filament_label(tray)
                    if not color and not ftype and not fname:
                        # Slot is empty / unidentified, skip it.
                        continue
                    out.append(
                        Filament(
                            source="ams",
                            ams_index=int(ams_index),
                            tray_index=int(tray_index),
                            color=color,
                            filament_type=ftype,
                            name=fname,
                        )
                    )
        except Exception as e:
            logger.debug("AMS read failed for %s: %s", self.cfg.name, e)

        # External spool (vt_tray). May raise or return an empty/blank tray.
        try:
            vt = p.vt_tray()
            if vt is not None:
                color = self._normalize_tray_color(getattr(vt, "tray_color", ""))
                ftype, fname = self._filament_label(vt)
                if color or ftype or fname:
                    out.append(
                        Filament(
                            source="external",
                            ams_index=0,
                            tray_index=0,
                            color=color,
                            filament_type=ftype,
                            name=fname,
                        )
                    )
        except Exception as e:
            logger.debug("vt_tray read failed for %s: %s", self.cfg.name, e)

        return out

    def _read_filaments(self) -> list[Filament]:
        """Read AMS trays + external spool defensively. Always returns a list.

        Strategy: parse the raw MQTT print payload first — that's the only
        path that handles dual-extruder models (H2C/H2D), where the
        bambulabs_api helpers return nothing because the payload uses
        `vir_slot` instead of `vt_tray` and AMS trays omit the legacy `n`
        field that `process_ams()` filters on.

        If the dump path doesn't surface any filament blocks (e.g. the
        first few polls before a full `pushall` arrives), fall back to the
        library helpers so we don't regress on already-working models.
        """
        try:
            from_dump = self._read_filaments_from_dump()
        except Exception as e:
            logger.debug("Dump-based filament read failed for %s: %s", self.cfg.name, e)
            from_dump = None
        if from_dump is not None:
            return from_dump
        return self._read_filaments_from_library()


class PrinterManager:
    """Manages all printer connections, polling, and WebSocket broadcasting."""

    # How long to ignore an announced IP whose cert didn't match the serial.
    RELOCATE_RETRY_SECONDS = 60.0
    # Discovered-printer cache: forget printers not heard from for this long.
    DISCOVERY_TTL_SECONDS = 300.0
    DISCOVERY_MAX_ENTRIES = 64
    # At shutdown, how long to let MQTT teardowns send their DISCONNECTs.
    SHUTDOWN_TEARDOWN_SECONDS = 2.0
    # Requests that only ask for data; they work without Developer Mode.
    QUERY_ACTIONS = frozenset({"refresh"})
    # How long the poll loop waits for one printer's poll step (health checks
    # + state read) before broadcasting its last known state instead. The step
    # keeps running; the next round waits on it again rather than starting
    # another.
    POLL_STEP_TIMEOUT_SECONDS = 2.0

    def __init__(
        self, config: AppConfig, discovery_port: int | None = discovery.SSDP_PORT
    ):
        self._config = config
        self._connections: dict[str, PrinterConnection] = {}
        self._ws_clients: set[WebSocket] = set()
        self._poll_task: asyncio.Task | None = None
        self._prev_states: dict[str, dict] = {}
        self._discovery_port = discovery_port
        self._listener: discovery.SsdpListener | None = None
        self._listening = False
        # upper-cased serial -> (latest announcement, monotonic time seen)
        self._discovered: dict[str, tuple[discovery.Announcement, float]] = {}
        self._relocating: set[str] = set()
        self._rejected: dict[tuple[str, str], float] = {}  # (serial, ip) -> time
        self._tasks: set[asyncio.Task] = set()

    @property
    def config(self) -> AppConfig:
        return self._config

    async def start(self) -> None:
        for cfg in self._config.printers:
            conn = PrinterConnection(cfg)
            self._connections[cfg.id] = conn
        # Connect all printers in background (don't block startup)
        for pid in list(self._connections):
            self._spawn(self._connect_printer(pid))
        self._poll_task = asyncio.create_task(self._poll_loop())
        if self._discovery_port is not None:
            self._listener = discovery.SsdpListener(
                self._on_announcement, port=self._discovery_port
            )
            self._listening = await self._listener.start()
        logger.info("PrinterManager started with %d printers", len(self._connections))

    async def stop(self) -> None:
        if self._listener:
            self._listener.stop()
            self._listener = None
        self._listening = False
        if self._poll_task:  # first, so it can't spawn more reconnects
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for conn in self._connections.values():
            await conn.disconnect()
        await asyncio.to_thread(mqtt.wait_for_teardowns, self.SHUTDOWN_TEARDOWN_SECONDS)
        logger.info("PrinterManager stopped")

    async def add_printer(self, cfg: PrinterConfig) -> None:
        conn = PrinterConnection(cfg)
        self._connections[cfg.id] = conn
        self._config.printers.append(cfg)
        save_config(self._config)
        # Connect in background so the API response returns immediately
        self._spawn(self._connect_printer(cfg.id))

    async def remove_printer(self, printer_id: str) -> bool:
        conn = self._connections.pop(printer_id, None)
        if not conn:
            return False
        try:
            path = _backup_printer_config(conn.cfg)
            logger.info("Backed up old printer config to %s", path)
        except Exception as e:
            logger.warning(
                "Failed to back up printer config for %s: %s", conn.cfg.name, e
            )
        await conn.disconnect()
        self._config.printers = [
            p for p in self._config.printers if p.id != printer_id
        ]
        self._prev_states.pop(printer_id, None)
        save_config(self._config)
        return True

    async def update_printer(self, printer_id: str, cfg: PrinterConfig) -> bool:
        conn = self._connections.get(printer_id)
        if not conn:
            return False
        try:
            path = _backup_printer_config(conn.cfg)
            logger.info("Backed up old printer config to %s", path)
        except Exception as e:
            logger.warning(
                "Failed to back up printer config for %s: %s", conn.cfg.name, e
            )
        await conn.disconnect()
        new_conn = PrinterConnection(cfg)
        self._connections[printer_id] = new_conn
        self._config.printers = [
            cfg if p.id == printer_id else p for p in self._config.printers
        ]
        save_config(self._config)
        # Reconnect in background
        self._spawn(self._connect_printer(printer_id))
        return True

    async def _connect_printer(self, printer_id: str) -> None:
        conn = self._connections.get(printer_id)
        if not conn or conn.connecting:
            return
        ok = await conn.connect()
        if self._connections.get(printer_id) is not conn:
            # Replaced (edited / relocated) while we were connecting.
            await conn.disconnect()
            return
        if ok:
            logger.info("Printer %s connected successfully", conn.cfg.name)
        else:
            logger.warning("Printer %s failed to connect, will retry", conn.cfg.name)

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    # --- IP auto-detect -----------------------------------------------------

    def _on_announcement(self, ann: discovery.Announcement) -> None:
        serial = ann.serial.upper()
        self._remember(serial, ann)
        if any(c.cfg.serial.upper() == serial for c in self._connections.values()):
            self._spawn(self.handle_announcement(ann))

    async def handle_announcement(self, ann: discovery.Announcement) -> None:
        """Follow a configured printer to the IP it just announced.

        Only acts when the printer is offline at its saved IP, and only after
        the TLS cert at the new IP proves it is the same printer (CN == serial).
        """
        serial = ann.serial.upper()
        conn = next(
            (c for c in self._connections.values() if c.cfg.serial.upper() == serial),
            None,
        )
        if conn is None or conn.cfg.ip == ann.ip or not conn.is_offline():
            return
        printer_id = conn.cfg.id
        key = (serial, ann.ip)
        rejected_at = self._rejected.get(key)
        if rejected_at and time.monotonic() - rejected_at < self.RELOCATE_RETRY_SECONDS:
            return
        if printer_id in self._relocating:
            return
        self._relocating.add(printer_id)
        try:
            cn = await asyncio.to_thread(
                discovery.fetch_cert_common_name, ann.ip, discovery.MQTT_PORT
            )
            if (cn or "").upper() != serial:
                now = time.monotonic()
                self._rejected = {
                    k: t for k, t in self._rejected.items()
                    if now - t < self.RELOCATE_RETRY_SECONDS
                }
                self._rejected[key] = now
                logger.warning(
                    "%s announced at %s but its certificate CN is %r, not %s; ignoring",
                    conn.cfg.name, ann.ip, cn, conn.cfg.serial,
                )
                return
            # Re-check: the connection may have recovered or been edited meanwhile.
            current = self._connections.get(printer_id)
            if current is not conn or not conn.is_offline():
                return
            old_ip = conn.cfg.ip
            new_cfg = conn.cfg.model_copy(update={"ip": ann.ip})
            await self.update_printer(printer_id, new_cfg)
            logger.info("%s moved from %s to %s", new_cfg.name, old_ip, ann.ip)
            await self._broadcast({
                "type": "notice",
                "level": "info",
                "message": f"{new_cfg.name} moved to {ann.ip}",
            })
        finally:
            self._relocating.discard(printer_id)

    # --- Discovered printers (Settings "Discover printers" list) -------------

    @property
    def discovery_listening(self) -> bool:
        """True while the SSDP listener is bound and receiving."""
        return self._listening

    def _remember(self, serial: str, ann: discovery.Announcement) -> None:
        self._discovered.pop(serial, None)  # re-insert so dict order = recency
        self._discovered[serial] = (ann, time.monotonic())
        if len(self._discovered) > self.DISCOVERY_MAX_ENTRIES:
            self._prune_discovered()
            while len(self._discovered) > self.DISCOVERY_MAX_ENTRIES:
                del self._discovered[next(iter(self._discovered))]  # oldest

    def _prune_discovered(self) -> None:
        now = time.monotonic()
        self._discovered = {
            s: (a, t) for s, (a, t) in self._discovered.items()
            if now - t < self.DISCOVERY_TTL_SECONDS
        }

    def discovered_printers(self) -> list[dict]:
        """Recently announced printers that aren't configured, sorted by name."""
        self._prune_discovered()
        now = time.monotonic()
        configured = {c.serial.upper() for c in self._config.printers}
        result = []
        for serial, (ann, seen) in self._discovered.items():
            if serial in configured:
                continue
            model_name, camera_port = discovery.model_info(ann.model)
            result.append({
                "ip": ann.ip,
                "serial": ann.serial,
                "name": ann.name,
                "model": ann.model,
                "model_name": model_name,
                "camera_port": camera_port,
                "last_seen_s": round(now - seen, 1),
            })
        # The UI shows the serial when there's no name; sort on what's shown.
        result.sort(key=lambda p: ((p["name"] or p["serial"]).casefold(), p["serial"]))
        return result

    def get_connection(self, printer_id: str) -> PrinterConnection | None:
        return self._connections.get(printer_id)

    def get_all_states(self) -> dict[str, PrinterState]:
        return {pid: conn.state for pid, conn in self._connections.items()}

    def get_camera(self, printer_id: str) -> CameraStream | None:
        conn = self._connections.get(printer_id)
        return conn.camera if conn else None

    def register_ws(self, ws: WebSocket) -> None:
        self._ws_clients.add(ws)

    def unregister_ws(self, ws: WebSocket) -> None:
        self._ws_clients.discard(ws)

    async def _poll_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self._config.poll_interval)
                states = await self._poll_all()

                if self._ws_clients:
                    msg = {"type": "update", "printers": states}
                    await self._broadcast(msg)

                self._prev_states = states
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("Poll loop error: %s", e)

    async def _poll_all(self) -> dict[str, Any]:
        """Poll every printer at once; a slow one can't hold up the rest."""
        conns = list(self._connections.items())
        await asyncio.gather(*(self._await_poll_step(pid, conn) for pid, conn in conns))
        # Printers as they are now: one removed (or replaced) meanwhile must
        # not be broadcast; the dashboard merges updates and would keep it.
        return {pid: conn.state.model_dump(mode="json") for pid, conn in self._connections.items()}

    async def _await_poll_step(self, pid: str, conn: PrinterConnection) -> None:
        step = conn.poll_step
        timeout = 0.0  # a step still running from an earlier round: don't wait again
        if step is None or step.done():
            step = conn.poll_step = self._spawn(self._poll_step(pid, conn))
            timeout = self.POLL_STEP_TIMEOUT_SECONDS
        try:
            await asyncio.wait_for(asyncio.shield(step), timeout)
        except asyncio.TimeoutError:
            pass  # still running; this round shows the last known state

    async def _poll_step(self, pid: str, conn: PrinterConnection) -> None:
        started = time.monotonic()
        try:
            await conn.check_silence()
            # A session that went down (e.g. the printer's IP changed)
            # is reset so the reconnect re-probes the saved IP.
            if conn._connected and conn.is_offline():
                logger.warning(
                    "%s: MQTT session to %s is down, reconnecting",
                    conn.cfg.name, conn.cfg.ip,
                )
                await conn.reset_session()
            # Reconnect in the background so an unreachable printer
            # can't stall its own updates either.
            if conn.needs_reconnect():
                logger.info("Attempting reconnect for %s", conn.cfg.name)
                self._spawn(self._connect_printer(pid))
            await conn.read_state()
        except Exception as e:
            logger.error("Poll error for %s: %s", conn.cfg.name, e)
        took = time.monotonic() - started
        if took > self.POLL_STEP_TIMEOUT_SECONDS:
            logger.warning(
                "%s: polling took %.1fs; its last known state was shown meanwhile",
                conn.cfg.name, took,
            )

    async def _broadcast(self, message: dict) -> None:
        dead: list[WebSocket] = []
        for ws in list(self._ws_clients):
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._ws_clients.discard(ws)

    async def execute_command(
        self, printer_id: str, action: str, params: dict | None = None
    ) -> tuple[bool, str]:
        conn = self._connections.get(printer_id)
        if not conn:
            return False, "Printer not found"
        if not conn._connected:
            return False, "Printer not connected"

        try:
            ok, msg = await asyncio.to_thread(
                self._run_command, conn.printer, action, params or {}
            )
        except Exception as e:
            return False, str(e)
        if ok and conn.state.commands_blocked and action not in self.QUERY_ACTIONS:
            # Published, but the printer will drop it. Still sent, so a wrong
            # detection can never stop commands from working.
            return False, protocol.COMMANDS_BLOCKED_MESSAGE
        return ok, msg

    @staticmethod
    def _run_command(
        printer: bl.Printer, action: str, params: dict
    ) -> tuple[bool, str]:
        match action:
            case "pause":
                return printer.pause_print(), "Print paused"
            case "resume":
                return printer.resume_print(), "Print resumed"
            case "stop":
                return printer.stop_print(), "Print stopped"
            case "light_on":
                return PrinterManager._send(printer, mqtt.light_requests("on"), "Light turned on")
            case "light_off":
                return PrinterManager._send(printer, mqtt.light_requests("off"), "Light turned off")
            case "set_light":
                mode = params.get("mode", "on")
                return PrinterManager._send(printer, mqtt.light_requests(mode), f"Light {mode}")
            case "set_speed":
                lvl = params.get("level", 1)
                return printer.set_print_speed_lvl(speed_lvl=lvl), f"Speed set to level {lvl}"
            case "refresh":
                return PrinterManager._send(printer, [mqtt.pushall_request()], "Refresh requested")
            case _:
                return False, f"Unknown action: {action}"

    @staticmethod
    def _send(printer: bl.Printer, requests: list[dict], msg: str) -> tuple[bool, str]:
        """Publish requests ourselves, bypassing the library (QoS 1, never waits).

        The light goes to both chamber LED nodes this way; the library's helper
        sends no `led_node`, which not every model accepts.
        """
        results = [mqtt.publish(printer, req) for req in requests]
        if all(results):
            return True, msg
        return False, "Not sent: printer not connected"
