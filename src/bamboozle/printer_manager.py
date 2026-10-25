from __future__ import annotations

import asyncio
import logging
import math
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, NamedTuple

from fastapi import WebSocket

from . import commands, discovery, hms_catalog, mqtt, protocol, snapshot, thumbnail
from .camera import CameraStream
from .config import _config_dir, save_config
from .merge import Frame, MergedReport
from .models import AppConfig, PrinterConfig, PrinterState
from .snapshot import Job

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


class CommandResult(NamedTuple):
    """How a dashboard command went.

    `confirmed` is True when the printer's state showed the change, False
    when it was sent but the state didn't show it in time (or the printer
    refused it), None when nothing was sent.
    """
    success: bool
    message: str
    confirmed: bool | None = None


class _Waiter:
    """A command waiting for the printer's state to show it took effect."""

    def __init__(self, command: commands.Command, future: asyncio.Future):
        self.command = command
        self.future = future
        self.refused: Frame | None = None  # the printer's own "failed" echo


class PrinterConnection:
    """One printer: its MQTT session, merged report, camera and state."""

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
    # Until the firmware version is known, ask for it; the wait doubles per
    # unanswered request, up to the max.
    VERSION_RETRY_SECONDS = 60.0
    VERSION_MAX_RETRY_SECONDS = 900.0
    # How long a command waits for the printer's state to show it. Halfway, an
    # unconfirmed one asks for a full report (an A1 / P1S delta may not carry
    # the changed field).
    COMMAND_CONFIRM_SECONDS = 10.0
    # Until a session has had a full report, its pushall is repeated this long
    # after the last one, doubling up to the max.
    FULL_REPORT_RETRY_SECONDS = 10.0
    FULL_REPORT_MAX_RETRY_SECONDS = 300.0
    _JOB_ACTIVE = frozenset({"PREPARE", "RUNNING"})
    _JOB_ENDED = frozenset({"FINISH", "FAILED", "IDLE"})

    def __init__(self, cfg: PrinterConfig):
        self.cfg = cfg
        self.cfg.camera_enabled = False  # Always start with cameras off
        self.camera = CameraStream(cfg.ip, cfg.access_code, cfg.camera_port, cfg.model)
        # Camera toggles run start()/stop() in worker threads; this keeps
        # them in arrival order.
        self.camera_toggle_lock = asyncio.Lock()
        self.state = PrinterState(printer_id=cfg.id, name=cfg.name, sort=cfg.sort)
        # Everything the printer reported this session, merged (see merge.py).
        # Only touched on the event loop.
        self.report = MergedReport()
        # Called on the event loop whenever the state may have changed (the
        # manager pushes a debounced update).
        self.on_change: Callable[[], None] = lambda: None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._waiters: set[_Waiter] = set()
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
        # Whether this session has had a full report, and when (monotonic) the
        # last pushall asking for one went out.
        self._full_seen = False
        self._pushall_at = 0.0
        self._pushall_wait = self.FULL_REPORT_RETRY_SECONDS
        # Why the last session was dropped for silence (protocol.NO_REPORTS /
        # STALE); kept until a report arrives or the printer stops answering.
        self._down_reason = ""
        self._version_asked_at = -math.inf  # monotonic; get_version last sent
        self._version_wait = self.VERSION_RETRY_SECONDS
        self.client = self._new_client()

    def _new_client(self) -> mqtt.BambuClient:
        """A fresh MQTT client, wired to this connection. Its reports are
        tagged with it, so a retired client's late ones are dropped."""
        client = mqtt.BambuClient(self.cfg.ip, self.cfg.access_code, self.cfg.serial)
        client.on_connack = self._on_mqtt_connack
        client.on_report = lambda payload: self._on_mqtt_report(client, payload)
        client.on_disconnect = lambda: self._to_loop(self._on_drop, client)
        return client

    # --- paho's network thread -------------------------------------------------

    def _on_mqtt_report(self, client: mqtt.BambuClient, payload: dict) -> None:
        if client is not self.client:
            return  # a retired client's callback that was already running
        self._last_report = time.monotonic()
        self._reports += 1
        self._probed = False
        self._down_reason = ""
        self._to_loop(self._ingest, client, payload)

    def _on_mqtt_connack(self, rc: Any) -> None:
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
        self._to_loop_changed()

    def _to_loop(self, fn: Callable, *args: Any) -> None:
        """Run fn on the event loop: queued from paho's thread (in arrival
        order), right away when already on the loop. Dropped once the loop is
        gone."""
        loop = self._loop
        if loop is None:
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            fn(*args)
            return
        try:
            loop.call_soon_threadsafe(fn, *args)
        except RuntimeError:
            pass  # loop closed

    def _to_loop_changed(self) -> None:
        self._to_loop(self._changed)

    # --- The event loop --------------------------------------------------------

    def _changed(self) -> None:
        try:
            self.on_change()
        except Exception:
            logger.exception("%s: state change callback failed", self.cfg.name)

    def _ingest(self, client: mqtt.BambuClient, payload: dict) -> None:
        """Merge one report, settle the commands it confirms, and say the
        state may have changed."""
        if client is not self.client:
            return  # from a client that has been replaced meanwhile
        try:
            frame = self.report.apply(payload)
        except Exception:
            logger.exception("%s: merging a report failed", self.cfg.name)
            return
        if frame.full:
            self._full_seen = True
        self._check_waiters(frame)
        self._changed()

    def _on_drop(self, client: mqtt.BambuClient) -> None:
        """The current client lost its session. paho doesn't reconnect it (a
        reconnect would replay unacked commands, see mqtt.py): retire it and
        let the reconnect start a fresh one."""
        if client is not self.client or not client.connected_once:
            return  # replaced already, or never logged in (connect() handles that)
        logger.warning("%s: MQTT session to %s dropped; reconnecting", self.cfg.name, self.cfg.ip)
        self._reset_now()

    @property
    def connecting(self) -> bool:
        return self._connecting

    async def connect(self) -> bool:
        self._loop = asyncio.get_running_loop()
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
        self._full_seen = False
        self._pushall_at = time.monotonic()  # the client asks once subscribed
        self._pushall_wait = self.FULL_REPORT_RETRY_SECONDS
        self._session_started = time.monotonic()
        client = self.client
        try:
            await asyncio.wait_for(asyncio.to_thread(client.start), timeout=10)
            await asyncio.sleep(self.MQTT_SETTLE_SECONDS)
            if self.client is not client:
                return False  # the session dropped while settling; a retry is scheduled
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
        finally:
            self._changed()

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
        mqtt.retire(self.client)
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
            await self._pushall()

    async def ensure_full_report(self) -> None:
        """Repeat the pushall until the session has had a full report (an
        A1 / P1S otherwise only sends deltas, and the on-connect pushall may
        have been lost)."""
        if (not self._connected or self._connecting or self._full_seen
                or not self.client.is_connected()):
            return
        now = time.monotonic()
        if now - self._pushall_at < self._pushall_wait:
            return
        self._pushall_at = now
        self._pushall_wait = min(self._pushall_wait * 2, self.FULL_REPORT_MAX_RETRY_SECONDS)
        logger.info("%s: no full report yet; asking again", self.cfg.name)
        await self._pushall()

    async def _pushall(self) -> None:
        try:
            await asyncio.to_thread(mqtt.publish, self.client, mqtt.pushall_request())
        except Exception as e:
            logger.debug("pushall failed for %s: %s", self.cfg.name, e)

    def is_offline(self) -> bool:
        """True when the printer is known to be down at its saved IP.

        A connect in progress, or a session that was up (or started) within
        the grace period, does not count as offline yet - paho may still be
        handshaking (it retries a session's first connection by itself).
        """
        if self._connecting:
            return False
        if not self._connected or self._login_refused():
            return True  # a refused code won't fix itself; don't wait out the grace
        if self._silence() >= self.STALE_SECONDS:
            return True  # the socket may be up, but nothing is coming through
        if self.client.is_connected():
            self._last_seen_up = time.monotonic()
            if self._reports:
                self._backoff = self.INITIAL_BACKOFF_SECONDS  # session verified
            return False
        return time.monotonic() - self._last_seen_up > self.MQTT_GRACE_SECONDS

    def _login_refused(self) -> bool:
        """The current attempt's login was refused (not just a previous one)."""
        return self._login_answered and self._auth_rejected

    async def reset_session(self) -> None:
        """Drop a dead MQTT session so the next reconnect re-probes the IP.

        The old client is retired in the background and replaced, so the
        reconnect never waits for (or races) its network thread. The new
        session starts from an empty report, as the dashboard should not show
        the old one's data as live.
        """
        self._reset_now()

    def _reset_now(self) -> None:
        mqtt.retire(self.client)
        self._schedule_retry()
        self.client = self._new_client()
        self.report.reset()
        self._full_seen = False
        # A new session has no version reply yet: ask the new one right away.
        self._version_asked_at = -math.inf
        self._version_wait = self.VERSION_RETRY_SECONDS
        self._changed()

    async def read_state(self) -> PrinterState:
        """Rebuild `state` from the merged report (and the connection's own
        knowledge: online, camera, thumbnail, why it's down)."""
        if not self._connected or self._auth_rejected:
            # Never started MQTT (e.g. unreachable) or the login was refused.
            self.state.online = False
            self.state.last_error = (
                protocol.AUTH_REJECTED if self._auth_rejected else self._down_reason
            )
            self.state.timestamp = time.time()
            return self.state
        try:
            fields, job = snapshot.decode(
                self.report.print, self.report.info, self.cfg.model, self.cfg.name
            )
            # Bambu's own text for the codes (fetched catalogue; see hms_catalog).
            try:
                fields["errors"] = hms_catalog.describe(fields["errors"], self.cfg.serial)
            except Exception as e:  # undescribed errors beat none
                logger.warning("Describing the errors from %s failed: %s", self.cfg.name, e)
            self._update_thumbnail(job)
            state = PrinterState(
                printer_id=self.cfg.id,
                name=self.cfg.name,
                camera_enabled=self.cfg.camera_enabled,
                camera_available=self.camera.available,
                sort=self.cfg.sort,
                timestamp=time.time(),
                last_error=self._down_reason,  # until the first report
                has_thumbnail=self.thumbnail is not None,
                thumbnail_key=self._thumb_key,  # set and cleared with it
                thumbnail_unavailable=(
                    protocol.THUMBNAIL_INTERNAL_STORAGE if job.key and job.internal else ""
                ),
                **fields,
            )
            self.state = state
            if state.online and not state.firmware:
                await self._ask_version()
            return state
        except Exception as e:
            logger.error("Error reading state from %s: %s", self.cfg.name, e)
            self.state.online = False
            self.state.timestamp = time.time()
            return self.state

    async def _ask_version(self) -> None:
        """Request the module versions (for the firmware); unanswered requests
        are repeated with a doubling wait (VERSION_RETRY_SECONDS up to
        VERSION_MAX_RETRY_SECONDS)."""
        now = time.monotonic()
        if self._version_asked_at > -math.inf:
            if now - self._version_asked_at < self._version_wait:
                return
            self._version_wait = min(self._version_wait * 2, self.VERSION_MAX_RETRY_SECONDS)
        self._version_asked_at = now
        try:
            await asyncio.to_thread(mqtt.publish, self.client, mqtt.version_request())
        except Exception as e:
            logger.debug("get_version failed for %s: %s", self.cfg.name, e)

    # --- Commands ---------------------------------------------------------------

    async def run_command(self, command: commands.Command) -> CommandResult:
        """Publish a command and wait (up to COMMAND_CONFIRM_SECONDS) for the
        printer's state to show it. Developer Mode off never fails a command:
        at most the "not confirmed" message mentions it."""
        waiter = self._watch(command)
        try:
            sent = await asyncio.to_thread(self._publish_all, command.requests)
            if not sent:
                return CommandResult(False, "Not sent: printer not connected")
            if await self._confirmed_within(waiter, self.COMMAND_CONFIRM_SECONDS / 2):
                return CommandResult(True, command.done, True)
            if command.pushall_midway:
                await self._pushall()  # the full report shows what a delta may not
            if await self._confirmed_within(waiter, self.COMMAND_CONFIRM_SECONDS / 2):
                return CommandResult(True, command.done, True)
            if waiter.refused is not None:
                reason = waiter.refused.reason or waiter.refused.result
                return CommandResult(False, f"Printer refused {command.label.lower()}: {reason}", False)
            message = (
                f"{command.label} sent, but not confirmed: the printer didn't report "
                f"{command.waiting_for} within {self.COMMAND_CONFIRM_SECONDS:.0f} s"
            )
            if command.print_control and self.state.developer_mode is False:
                message += " (Developer Mode is off: the printer may ignore print controls)"
            return CommandResult(True, message, False)
        finally:
            self._waiters.discard(waiter)
            if not waiter.future.done():
                waiter.future.cancel()

    @staticmethod
    async def _confirmed_within(waiter: _Waiter, seconds: float) -> bool:
        try:
            await asyncio.wait_for(asyncio.shield(waiter.future), seconds)
            return True
        except asyncio.TimeoutError:
            return False

    def _publish_all(self, requests: list[dict]) -> bool:
        # A list, not a generator: every request is tried even if one fails.
        return all([mqtt.publish(self.client, request) for request in requests])

    def _watch(self, command: commands.Command) -> _Waiter:
        """Start watching for the command's effect (before it's published).
        A printer already in the requested state counts as confirmed."""
        waiter = _Waiter(command, asyncio.get_running_loop().create_future())
        if self._confirms(waiter, None):
            waiter.future.set_result(True)
        else:
            self._waiters.add(waiter)
        return waiter

    def _confirms(self, waiter: _Waiter, frame: Frame | None) -> bool:
        try:
            return bool(waiter.command.confirmed(self.report.print, frame))
        except Exception:
            logger.exception("%s: checking %s failed", self.cfg.name, waiter.command.action)
            return False

    def _check_waiters(self, frame: Frame) -> None:
        for waiter in list(self._waiters):
            if waiter.future.done():
                self._waiters.discard(waiter)
            elif self._confirms(waiter, frame):
                waiter.future.set_result(True)
                self._waiters.discard(waiter)
            elif ((frame.command, frame.sequence_id) == waiter.command.echo
                  and frame.result in ("fail", "failed")):
                waiter.refused = frame  # only counts if the state never confirms

    # --- Thumbnail ----------------------------------------------------------

    def _update_thumbnail(self, job: Job) -> None:
        """After each read: start a background fetch when the job needs one.

        At most one fetch runs per printer (the printer allows few FTP
        sessions). Runs on the event loop; the fetch itself never does.
        """
        # Bambu keeps the names after a print ends, so a reprint of the same
        # project shows up as the state going from ended back to active.
        restarted = (
            job.gcode_state in self._JOB_ACTIVE and self._thumb_last_state in self._JOB_ENDED
        )
        if job.gcode_state != "UNKNOWN":  # e.g. a reconnect between the prints
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

    async def _fetch_thumbnail(self, job: Job, cancel: threading.Event) -> None:
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
            self._changed()  # show it now, not at the next housekeeping round
        else:
            self._thumb_retry_at = math.inf
            logger.info(
                "%s: no 3MF for %r on the printer's storage; no thumbnail for this job",
                self.cfg.name, job.key,
            )


class PrinterManager:
    """Manages all printer connections and the dashboard's WebSocket updates.

    Updates are event-driven: a printer's report marks it dirty, and within
    PUSH_DEBOUNCE_SECONDS its new state is broadcast if anything visible
    changed. The poll loop only does housekeeping (silence probes, stale
    sessions, reconnects) and broadcasts the changes no report triggers
    (connect / offline / camera), plus a full heartbeat now and then.
    """

    # How long to ignore an announced IP whose cert didn't match the serial.
    RELOCATE_RETRY_SECONDS = 60.0
    # Discovered-printer cache: forget printers not heard from for this long.
    DISCOVERY_TTL_SECONDS = 300.0
    DISCOVERY_MAX_ENTRIES = 64
    # At shutdown, how long to let MQTT teardowns send their DISCONNECTs.
    SHUTDOWN_TEARDOWN_SECONDS = 2.0
    # How long the poll loop waits for one printer's poll step (health checks
    # + state read) before broadcasting its last known state instead. The step
    # keeps running; the next round waits on it again rather than starting
    # another.
    POLL_STEP_TIMEOUT_SECONDS = 2.0
    # A report is broadcast at most this long after it arrives; a burst of
    # reports becomes one update.
    PUSH_DEBOUNCE_SECONDS = 0.3
    # Every printer's state is broadcast this often, changed or not, so a
    # missed update can't leave a card wrong for long.
    HEARTBEAT_SECONDS = 30.0
    # A dashboard that can't take a message this fast is dropped (it
    # reconnects and gets a fresh `init`); a frozen tab must not stall
    # everyone's updates.
    WS_SEND_TIMEOUT_SECONDS = 2.0

    def __init__(
        self, config: AppConfig, discovery_port: int | None = discovery.SSDP_PORT
    ):
        self._config = config
        self._connections: dict[str, PrinterConnection] = {}
        self._ws_clients: set[WebSocket] = set()
        self._poll_task: asyncio.Task | None = None
        # printer id -> the state last broadcast, without its timestamp (dedupe)
        self._last_sent: dict[str, dict] = {}
        # Printers whose state may have changed since the last push, and the
        # pending push for them all.
        self._dirty: set[str] = set()
        self._push_handle: asyncio.TimerHandle | None = None
        # One broadcast at a time, so a client never gets an older state
        # after a newer one.
        self._broadcast_lock = asyncio.Lock()
        self._last_heartbeat = time.monotonic()
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
            self._connections[cfg.id] = self._new_connection(cfg)
        # Connect all printers in background (don't block startup)
        for pid in list(self._connections):
            self._spawn(self._connect_printer(pid))
        self._poll_task = asyncio.create_task(self._poll_loop())
        # A newly loaded catalogue adds text to errors already on the cards.
        hms_catalog.CATALOG.start(
            lambda: [c.serial for c in self._config.printers], on_change=self._mark_all_dirty
        )
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
        await hms_catalog.CATALOG.stop()
        if self._push_handle is not None:
            self._push_handle.cancel()
            self._push_handle = None
        self._dirty.clear()
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for conn in self._connections.values():
            await conn.disconnect()
        await asyncio.to_thread(mqtt.wait_for_teardowns, self.SHUTDOWN_TEARDOWN_SECONDS)
        logger.info("PrinterManager stopped")

    async def add_printer(self, cfg: PrinterConfig) -> None:
        cfg.model = cfg.model or self._announced_model(cfg)
        self._connections[cfg.id] = self._new_connection(cfg)
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
        self._forget_pushes(printer_id)
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
        cfg.model = cfg.model or self._announced_model(cfg)
        self._forget_pushes(printer_id)
        self._connections[printer_id] = self._new_connection(cfg)
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

    def _new_connection(self, cfg: PrinterConfig) -> PrinterConnection:
        conn = PrinterConnection(cfg)
        conn.on_change = lambda: self._mark_dirty(cfg.id, conn)
        return conn

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    # --- IP auto-detect -----------------------------------------------------

    def _on_announcement(self, ann: discovery.Announcement) -> None:
        serial = ann.serial.upper()
        self._remember(serial, ann)
        conn = next(
            (c for c in self._connections.values() if c.cfg.serial.upper() == serial),
            None,
        )
        if conn is not None:
            self._learn_model(conn, ann)
            self._spawn(self.handle_announcement(ann))

    def _learn_model(self, conn: PrinterConnection, ann: discovery.Announcement) -> None:
        """Record the model a configured printer announces (backfills old configs).

        Trusted only from the printer's configured IP; a move to a new IP goes
        through handle_announcement's cert check. Only the field changes and
        the config is saved: no reconnect and no history backup, since the
        model just picks the camera's ffmpeg profile for its next session.
        """
        if not ann.model or ann.ip != conn.cfg.ip or ann.model == conn.cfg.model:
            return
        logger.info(
            "%s announced model %s (%s)", conn.cfg.name, discovery.model_info(ann.model)[0], ann.model
        )
        conn.cfg.model = ann.model
        conn.camera.model = ann.model
        for p in self._config.printers:  # conn.cfg is normally this entry already
            if p.id == conn.cfg.id:
                p.model = ann.model
        save_config(self._config)

    def _announced_model(self, cfg: PrinterConfig) -> str:
        """The model the printer recently announced from cfg's IP, or ""."""
        entry = self._discovered.get(cfg.serial.upper())
        if entry is None:
            return ""
        ann, seen = entry
        if ann.ip != cfg.ip or time.monotonic() - seen >= self.DISCOVERY_TTL_SECONDS:
            return ""
        return ann.model

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
            new_cfg = conn.cfg.model_copy(
                update={"ip": ann.ip, "model": ann.model or conn.cfg.model}
            )
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

    # --- Dashboard updates ------------------------------------------------------

    def _mark_dirty(self, pid: str, conn: PrinterConnection) -> None:
        """A printer's state may have changed: push it within
        PUSH_DEBOUNCE_SECONDS. A throttle, not a debounce: a printer
        reporting every second still gets an update per report. One push
        serves every printer that changed meanwhile (one message, one
        dashboard re-render)."""
        if self._connections.get(pid) is not conn:
            return
        self._dirty.add(pid)
        if self._push_handle is None:
            loop = asyncio.get_running_loop()
            self._push_handle = loop.call_later(
                self.PUSH_DEBOUNCE_SECONDS, lambda: self._spawn(self._push())
            )

    def _mark_all_dirty(self) -> None:
        for pid, conn in list(self._connections.items()):
            self._mark_dirty(pid, conn)

    async def _push(self) -> None:
        # Changes from here on schedule the next push.
        self._push_handle = None
        pids, self._dirty = self._dirty, set()
        await self._push_now(pids)

    async def _push_now(self, pids) -> None:
        """Re-read these printers' states and broadcast those that changed."""
        try:
            for pid in pids:
                conn = self._connections.get(pid)
                if conn is not None:
                    await conn.read_state()
            await self._broadcast_changed(pids)
        except Exception as e:
            logger.error("Pushing printer states failed: %s", e)

    def _forget_pushes(self, pid: str) -> None:
        self._dirty.discard(pid)
        self._last_sent.pop(pid, None)

    async def _broadcast_changed(self, pids, force: bool = False) -> None:
        """Broadcast the printers whose state differs from what was last sent
        (all of `pids` when forced). Printers no longer configured are skipped:
        the dashboard merges updates and would keep them."""
        async with self._broadcast_lock:
            changed = {}
            for pid in pids:
                conn = self._connections.get(pid)
                if conn is None:
                    self._last_sent.pop(pid, None)
                    continue
                state = conn.state.model_dump(mode="json")
                key = {k: v for k, v in state.items() if k != "timestamp"}
                if force or self._last_sent.get(pid) != key:
                    self._last_sent[pid] = key
                    changed[pid] = state
            if changed and self._ws_clients:
                await self._send_all({"type": "update", "printers": changed})

    async def _poll_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self._config.poll_interval)
                await self._poll_all()
                heartbeat = time.monotonic() - self._last_heartbeat >= self.HEARTBEAT_SECONDS
                if heartbeat:
                    self._last_heartbeat = time.monotonic()
                await self._broadcast_changed(list(self._connections), force=heartbeat)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("Poll loop error: %s", e)

    async def _poll_all(self) -> None:
        """Housekeeping for every printer at once; a slow one can't hold up
        the rest."""
        conns = list(self._connections.items())
        await asyncio.gather(*(self._await_poll_step(pid, conn) for pid, conn in conns))

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
            await conn.ensure_full_report()
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
        async with self._broadcast_lock:
            await self._send_all(message)

    async def _send_all(self, message: dict) -> None:
        dead: list[WebSocket] = []
        for ws in list(self._ws_clients):
            try:
                await asyncio.wait_for(ws.send_json(message), self.WS_SEND_TIMEOUT_SECONDS)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._ws_clients.discard(ws)
            try:
                await asyncio.wait_for(ws.close(), self.WS_SEND_TIMEOUT_SECONDS)
            except Exception:
                pass

    async def execute_command(
        self, printer_id: str, action: str, params: dict | None = None
    ) -> CommandResult:
        """Send a dashboard command and report whether the printer's state
        showed it (see PrinterConnection.run_command)."""
        conn = self._connections.get(printer_id)
        if not conn:
            return CommandResult(False, "Printer not found")
        try:
            command = commands.build(action, params)
        except commands.InvalidCommand as e:
            return CommandResult(False, str(e))
        if not conn._connected:
            return CommandResult(False, "Printer not connected")
        try:
            result = await conn.run_command(command)
        except Exception as e:
            logger.error("%s on %s failed: %s", action, conn.cfg.name, e)
            return CommandResult(False, str(e))
        if result.confirmed and self._connections.get(printer_id) is conn:
            # Show it on the card now, so the toast doesn't arrive first.
            self._dirty.discard(printer_id)
            await self._push_now([printer_id])
        return result
