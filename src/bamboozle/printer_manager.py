from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import bambulabs_api as bl
from fastapi import WebSocket

from .camera import CameraStream
from .config import save_config
from .models import AMSTray, AppConfig, PrinterConfig, PrinterState
from .thumbnail import fetch_thumbnail

logger = logging.getLogger(__name__)


class PrinterConnection:
    """Wraps a single printer's MQTT connection, camera stream, and state."""

    def __init__(self, cfg: PrinterConfig):
        self.cfg = cfg
        self.cfg.camera_enabled = False  # Always start with cameras off
        self.printer = bl.Printer(cfg.ip, cfg.access_code, cfg.serial)
        self.camera = CameraStream(cfg.ip, cfg.access_code, cfg.camera_port)
        self.state = PrinterState(printer_id=cfg.id, name=cfg.name)
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
            timestamp=time.time(),
        )


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
