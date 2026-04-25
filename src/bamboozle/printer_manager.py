from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import bambulabs_api as bl
from fastapi import WebSocket

from .camera import CameraStream
from .config import _config_dir, save_config
from .models import AppConfig, Filament, PrinterConfig, PrinterState
from .thumbnail import fetch_thumbnail

logger = logging.getLogger(__name__)


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

    def __init__(self, cfg: PrinterConfig):
        self.cfg = cfg
        self.cfg.camera_enabled = False  # Always start with cameras off
        self.printer = bl.Printer(cfg.ip, cfg.access_code, cfg.serial)
        self.camera = CameraStream(cfg.ip, cfg.access_code, cfg.camera_port)
        self.state = PrinterState(printer_id=cfg.id, name=cfg.name, sort=cfg.sort)
        self.thumbnail: bytes | None = None
        self._thumb_fetched_for: str = ""  # gcode_file we fetched thumbnail for
        self._backoff = 3.0
        self._connected = False

    async def connect(self) -> bool:
        try:
            await asyncio.wait_for(
                asyncio.to_thread(self.printer.mqtt_start), timeout=10
            )
            # Give MQTT a moment to establish
            await asyncio.sleep(2)
            self._connected = True
            self._backoff = 3.0
            logger.info("Connected to printer %s (%s)", self.cfg.name, self.cfg.ip)
            return True
        except asyncio.TimeoutError:
            logger.error("Timeout connecting to %s (%s)", self.cfg.name, self.cfg.ip)
            self._connected = False
            return False
        except Exception as e:
            logger.error("Failed to connect to %s: %s", self.cfg.name, e)
            self._connected = False
            return False

    async def disconnect(self) -> None:
        try:
            self.camera.stop()
            await asyncio.to_thread(self.printer.mqtt_stop)
        except Exception as e:
            logger.debug("Error disconnecting %s: %s", self.cfg.name, e)
        self._connected = False

    async def read_state(self) -> PrinterState:
        try:
            state_data = await asyncio.to_thread(self._read_sync)
            self.state = state_data
            return state_data
        except Exception as e:
            logger.error("Error reading state from %s: %s", self.cfg.name, e)
            self.state.online = False
            self.state.timestamp = time.time()
            return self.state

    def _read_sync(self) -> PrinterState:
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

        online = gcode_str != "UNKNOWN" or nozzle_temp is not None

        # Fetch thumbnail when a new print job is detected
        display_name = subtask or file_name
        if display_name and display_name != self._thumb_fetched_for:
            try:
                thumb = fetch_thumbnail(
                    self.cfg.ip, self.cfg.access_code, file_name
                )
                if thumb:
                    self.thumbnail = thumb
                    self._thumb_fetched_for = display_name
                    logger.info("Fetched thumbnail for %s on %s", display_name, self.cfg.name)
            except Exception as e:
                logger.debug("Thumbnail fetch failed for %s: %s", self.cfg.name, e)
        elif not display_name and gcode_str == "IDLE":
            self.thumbnail = None
            self._thumb_fetched_for = ""

        return PrinterState(
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
            has_thumbnail=self.thumbnail is not None,
            thumbnail_key=self._thumb_fetched_for if self.thumbnail is not None else "",
            filaments=filaments,
            sort=self.cfg.sort,
            timestamp=time.time(),
        )

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
        try:
            dump = self.printer.mqtt_dump() or {}
        except Exception as e:
            logger.debug("mqtt_dump() failed for %s: %s", self.cfg.name, e)
            return None

        print_payload = dump.get("print") if isinstance(dump, dict) else None
        if not isinstance(print_payload, dict):
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

    def __init__(self, config: AppConfig):
        self._config = config
        self._connections: dict[str, PrinterConnection] = {}
        self._ws_clients: set[WebSocket] = set()
        self._poll_task: asyncio.Task | None = None
        self._prev_states: dict[str, dict] = {}

    @property
    def config(self) -> AppConfig:
        return self._config

    async def start(self) -> None:
        for cfg in self._config.printers:
            conn = PrinterConnection(cfg)
            self._connections[cfg.id] = conn
        # Connect all printers in background (don't block startup)
        for pid in list(self._connections):
            asyncio.create_task(self._connect_printer(pid))
        self._poll_task = asyncio.create_task(self._poll_loop())
        logger.info("PrinterManager started with %d printers", len(self._connections))

    async def stop(self) -> None:
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
        for conn in self._connections.values():
            await conn.disconnect()
        logger.info("PrinterManager stopped")

    async def add_printer(self, cfg: PrinterConfig) -> None:
        conn = PrinterConnection(cfg)
        self._connections[cfg.id] = conn
        self._config.printers.append(cfg)
        save_config(self._config)
        # Connect in background so the API response returns immediately
        asyncio.create_task(self._connect_printer(cfg.id))

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
        asyncio.create_task(self._connect_printer(printer_id))
        return True

    async def _connect_printer(self, printer_id: str) -> None:
        conn = self._connections.get(printer_id)
        if conn:
            ok = await conn.connect()
            if ok:
                logger.info("Printer %s connected successfully", conn.cfg.name)
            else:
                logger.warning("Printer %s failed to connect, will retry", conn.cfg.name)

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
                states: dict[str, Any] = {}
                for pid, conn in list(self._connections.items()):
                    # Auto-reconnect disconnected printers
                    if not conn._connected:
                        logger.info("Attempting reconnect for %s", conn.cfg.name)
                        await conn.connect()

                    state = await conn.read_state()
                    state_dict = state.model_dump(mode="json")
                    states[pid] = state_dict

                if self._ws_clients:
                    msg = {"type": "update", "printers": states}
                    await self._broadcast(msg)

                self._prev_states = states
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("Poll loop error: %s", e)

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
            result = await asyncio.to_thread(
                self._run_command, conn.printer, action, params or {}
            )
            return result
        except Exception as e:
            return False, str(e)

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
                return PrinterManager._set_light(printer, "on"), "Light turned on"
            case "light_off":
                return PrinterManager._set_light(printer, "off"), "Light turned off"
            case "set_light":
                mode = params.get("mode", "on")
                return PrinterManager._set_light(printer, mode), f"Light {mode}"
            case "set_speed":
                lvl = params.get("level", 1)
                return printer.set_print_speed_lvl(speed_lvl=lvl), f"Speed set to level {lvl}"
            case _:
                return False, f"Unknown action: {action}"

    @staticmethod
    def _set_light(printer: bl.Printer, mode: str) -> bool:
        """Set light using the full ledctrl command format (works on all models)."""
        import json
        cmd = {
            "system": {
                "sequence_id": "0",
                "command": "ledctrl",
                "led_node": "chamber_light",
                "led_mode": mode,
                "led_on_time": 500,
                "led_off_time": 500,
                "loop_times": 0,
                "interval_time": 0,
            }
        }
        printer.mqtt_client._client.publish(
            printer.mqtt_client.command_topic, json.dumps(cmd)
        )
        return True
