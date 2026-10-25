from __future__ import annotations

import asyncio
import threading

import bambulabs_api as bl
from fastapi import APIRouter, HTTPException, Request

from .. import discovery, mqtt, protocol
from ..config import save_config
from ..models import PrinterAddRequest, PrinterConfig, PrinterState, PrinterUpdateRequest

router = APIRouter()

# POST /printers/test: how long to wait for the login answer, then for the
# first report and (separately) the full status.
TEST_CONNACK_TIMEOUT = 10.0
TEST_REPORT_TIMEOUT = 5.0


@router.get("/printers")
async def list_printers(request: Request) -> dict:
    manager = request.app.state.manager
    states = manager.get_all_states()
    return {
        "printers": [s.model_dump(mode="json") for s in states.values()]
    }


@router.get("/discovery")
async def list_discovered(request: Request) -> dict:
    """Unconfigured printers recently heard announcing on the LAN."""
    manager = request.app.state.manager
    return {
        "listening": manager.discovery_listening,
        "printers": manager.discovered_printers(),
    }


@router.get("/printers/{printer_id}")
async def get_printer(request: Request, printer_id: str) -> dict:
    manager = request.app.state.manager
    states = manager.get_all_states()
    state = states.get(printer_id)
    if not state:
        raise HTTPException(404, "Printer not found")
    return state.model_dump(mode="json")


@router.post("/printers")
async def add_printer(request: Request, body: PrinterAddRequest) -> dict:
    manager = request.app.state.manager
    cfg = PrinterConfig(
        name=body.name,
        ip=body.ip,
        access_code=body.access_code,
        serial=body.serial,
        camera_port=body.camera_port,
        camera_enabled=body.camera_enabled,
        sort=body.sort,
    )
    await manager.add_printer(cfg)
    return {"id": cfg.id, "name": cfg.name}


@router.put("/printers/{printer_id}")
async def update_printer(
    request: Request, printer_id: str, body: PrinterUpdateRequest
) -> dict:
    manager = request.app.state.manager
    # Find existing config
    existing = None
    for p in manager.config.printers:
        if p.id == printer_id:
            existing = p
            break
    if not existing:
        raise HTTPException(404, "Printer not found")

    updated = PrinterConfig(
        id=printer_id,
        name=body.name or existing.name,
        ip=body.ip or existing.ip,
        access_code=body.access_code or existing.access_code,
        serial=body.serial or existing.serial,
        camera_port=body.camera_port if body.camera_port is not None else existing.camera_port,
        camera_enabled=body.camera_enabled if body.camera_enabled is not None else existing.camera_enabled,
        sort=body.sort if body.sort is not None else existing.sort,
    )
    success = await manager.update_printer(printer_id, updated)
    if not success:
        raise HTTPException(500, "Failed to update printer")
    return {"id": printer_id, "name": updated.name}


@router.delete("/printers/{printer_id}")
async def delete_printer(request: Request, printer_id: str) -> dict:
    manager = request.app.state.manager
    success = await manager.remove_printer(printer_id)
    if not success:
        raise HTTPException(404, "Printer not found")
    return {"deleted": printer_id}


@router.post("/printers/{printer_id}/pause")
async def pause_print(request: Request, printer_id: str) -> dict:
    manager = request.app.state.manager
    ok, msg = await manager.execute_command(printer_id, "pause")
    return {"success": ok, "message": msg}


@router.post("/printers/{printer_id}/resume")
async def resume_print(request: Request, printer_id: str) -> dict:
    manager = request.app.state.manager
    ok, msg = await manager.execute_command(printer_id, "resume")
    return {"success": ok, "message": msg}


@router.post("/printers/{printer_id}/stop")
async def stop_print(request: Request, printer_id: str) -> dict:
    manager = request.app.state.manager
    ok, msg = await manager.execute_command(printer_id, "stop")
    return {"success": ok, "message": msg}


@router.post("/printers/{printer_id}/light")
async def toggle_light(request: Request, printer_id: str, body: dict) -> dict:
    action = "light_on" if body.get("on", True) else "light_off"
    manager = request.app.state.manager
    ok, msg = await manager.execute_command(printer_id, action)
    return {"success": ok, "message": msg}


@router.post("/printers/{printer_id}/speed")
async def set_speed(request: Request, printer_id: str, body: dict) -> dict:
    manager = request.app.state.manager
    ok, msg = await manager.execute_command(
        printer_id, "set_speed", {"level": body.get("level", 1)}
    )
    return {"success": ok, "message": msg}


@router.post("/printers/{printer_id}/refresh")
async def refresh_printer(request: Request, printer_id: str) -> dict:
    """Ask the printer for a full status report (pushall)."""
    manager = request.app.state.manager
    ok, msg = await manager.execute_command(printer_id, "refresh")
    return {"success": ok, "message": msg}


@router.post("/printers/{printer_id}/camera")
async def toggle_camera(request: Request, printer_id: str, body: dict) -> dict:
    """Toggle camera on/off for a printer."""
    manager = request.app.state.manager
    conn = manager.get_connection(printer_id)
    if not conn:
        raise HTTPException(404, "Printer not found")

    enabled = body.get("enabled", not conn.cfg.camera_enabled)
    conn.cfg.camera_enabled = enabled

    # Update config
    for p in manager.config.printers:
        if p.id == printer_id:
            p.camera_enabled = enabled
            break
    save_config(manager.config)

    # stop() joins the capture thread, so keep both off the event loop.
    await asyncio.to_thread(conn.camera.start if enabled else conn.camera.stop)

    return {"success": True, "camera_enabled": enabled}


@router.post("/printers/test")
async def test_connection(body: PrinterAddRequest) -> dict:
    """Test connecting to a printer without saving it."""
    reachable = await asyncio.to_thread(
        discovery.tcp_reachable, body.ip, discovery.MQTT_PORT
    )
    if not reachable:
        return {"success": False, "message": f"printer unreachable at {body.ip}"}

    printer = bl.Printer(body.ip, body.access_code, body.serial)
    mqtt_client = printer.mqtt_client
    connack: list = []
    got_connack = threading.Event()
    got_report = threading.Event()  # anything on the serial's report topic
    got_status = threading.Event()  # a report carrying gcode_state

    def on_connect(_mqtt, _client, _userdata, _flags, rc, _props):
        connack.append(rc)
        got_connack.set()

    def gcode_state() -> str | None:
        return (mqtt_client.dump().get("print") or {}).get("gcode_state")

    def on_message(*_):
        got_report.set()
        if gcode_state():
            got_status.set()

    mqtt_client.on_connect_handler = on_connect
    mqtt_client.on_message_handler = on_message
    try:
        await asyncio.to_thread(printer.mqtt_start)
        if not await asyncio.to_thread(got_connack.wait, TEST_CONNACK_TIMEOUT):
            return {"success": False,
                    "message": f"MQTT login to {body.ip} timed out"}
        rc = connack[0]
        if protocol.is_auth_refusal(rc):
            return {"success": False, "message": protocol.AUTH_REJECTED_MESSAGE}
        if protocol.connack_failed(rc):
            return {"success": False, "message": f"Printer refused the connection: {rc}"}
        if not await asyncio.to_thread(got_report.wait, TEST_REPORT_TIMEOUT):
            return {"success": False,
                    "message": "Connected, but the printer sent no data: check the serial number"}
        if not got_status.is_set():
            # A1 / P1S ignore the library's combined on-connect request, so
            # ask for the full status on its own.
            # Login and serial are proven already, so a failure here only
            # costs the state; the publish wait has no timeout of its own.
            try:
                await asyncio.wait_for(
                    asyncio.to_thread(mqtt_client.pushall), TEST_REPORT_TIMEOUT
                )
                await asyncio.to_thread(got_status.wait, TEST_REPORT_TIMEOUT)
            except Exception:
                pass
        state = gcode_state()
        return {
            "success": True,
            "message": f"Connected successfully. State: {state}" if state
            else "Connected successfully",
        }
    except Exception as e:
        return {"success": False, "message": str(e)}
    finally:
        mqtt.retire(printer)
