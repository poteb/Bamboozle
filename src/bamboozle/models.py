from __future__ import annotations

import time
import uuid

from pydantic import BaseModel, Field


class PrinterConfig(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    name: str
    ip: str
    access_code: str
    serial: str
    camera_port: int = 6000  # 6000 for A1/P1/P1S, 322 for X1/H2C/H2D/P2
    camera_enabled: bool = False


class AppConfig(BaseModel):
    printers: list[PrinterConfig] = []
    port: int = 8080
    poll_interval: float = 3.0


class AMSTray(BaseModel):
    tray_id: int = 0
    filament_type: str = ""
    color: str = ""
    remaining: float = 0.0


class PrinterState(BaseModel):
    printer_id: str
    name: str
    online: bool = False
    gcode_state: str = "UNKNOWN"
    print_status: str = "UNKNOWN"
    progress: int = 0
    remaining_minutes: int = 0
    current_layer: int = 0
    total_layers: int = 0
    nozzle_temp: float | None = None
    nozzle_target: float | None = None
    bed_temp: float | None = None
    bed_target: float | None = None
    chamber_temp: float | None = None
    file_name: str = ""
    print_speed: int = 0
    light_on: bool = False
    camera_enabled: bool = False
    camera_available: bool = False
    has_thumbnail: bool = False
    ams_trays: list[AMSTray] = []
    error_code: int = 0
    timestamp: float = Field(default_factory=time.time)


class PrinterAddRequest(BaseModel):
    name: str
    ip: str
    access_code: str
    serial: str
    camera_port: int = 322
    camera_enabled: bool = False


class PrinterUpdateRequest(BaseModel):
    name: str | None = None
    ip: str | None = None
    access_code: str | None = None
    serial: str | None = None
    camera_port: int | None = None
    camera_enabled: bool | None = None
