from __future__ import annotations

import asyncio
import threading

from fastapi import APIRouter, HTTPException, Request

from .. import discovery, mqtt, protocol
from ..config import save_config
from ..merge import MergedReport
from ..models import PrinterAddRequest, PrinterConfig, PrinterState, PrinterUpdateRequest

router = APIRouter()

# POST /printers/test: how long to wait for the login answer, then for the
# first report and (separately) the full status.
TEST_CONNACK_TIMEOUT = 10.0
TEST_REPORT_TIMEOUT = 5.0


def _result(result) -> dict:
    """A command's outcome (PrinterManager.execute_command) as JSON."""
    return result._asdict()


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

    serial = body.serial or existing.serial
    updated = PrinterConfig(
        id=printer_id,
        name=body.name or existing.name,
        ip=body.ip or existing.ip,
        access_code=body.access_code or existing.access_code,
        serial=serial,
        # Another serial is another printer; the manager re-learns its model.
        model=existing.model if serial.upper() == existing.serial.upper() else "",
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
    return _result(await manager.execute_command(printer_id, "pause"))


@router.post("/printers/{printer_id}/resume")
async def resume_print(request: Request, printer_id: str) -> dict:
    manager = request.app.state.manager
    return _result(await manager.execute_command(printer_id, "resume"))


@router.post("/printers/{printer_id}/stop")
async def stop_print(request: Request, printer_id: str) -> dict:
    manager = request.app.state.manager
    return _result(await manager.execute_command(printer_id, "stop"))


@router.post("/printers/{printer_id}/light")
async def toggle_light(request: Request, printer_id: str, body: dict) -> dict:
    action = "light_on" if body.get("on", True) else "light_off"
    manager = request.app.state.manager
    return _result(await manager.execute_command(printer_id, action))


@router.post("/printers/{printer_id}/speed")
async def set_speed(request: Request, printer_id: str, body: dict) -> dict:
    manager = request.app.state.manager
    return _result(await manager.execute_command(
        printer_id, "set_speed", {"level": body.get("level", 1)}
    ))


@router.post("/printers/{printer_id}/refresh")
async def refresh_printer(request: Request, printer_id: str) -> dict:
    """Ask the printer for a full status report (pushall)."""
    manager = request.app.state.manager
    return _result(await manager.execute_command(printer_id, "refresh"))


@router.post("/printers/{printer_id}/camera")
async def toggle_camera(request: Request, printer_id: str, body: dict) -> dict:
    """Toggle camera on/off for a printer."""
    manager = request.app.state.manager
    conn = manager.get_connection(printer_id)
    if not conn:
        raise HTTPException(404, "Printer not found")

    async with conn.camera_toggle_lock:
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

    loop = asyncio.get_running_loop()
    connack: list = []
    got_connack = threading.Event()
    got_report = threading.Event()  # anything on the serial's report topic
    got_status = asyncio.Event()  # the merged report has a gcode_state
    report = MergedReport()

    def on_connect(rc):
        connack.append(rc)
        got_connack.set()

    def ingest(payload):  # on the event loop
        report.apply(payload)
        if report.print.get("gcode_state"):
            got_status.set()

    def on_report(payload):  # on paho's thread
        got_report.set()
        try:
            loop.call_soon_threadsafe(ingest, payload)
        except RuntimeError:
            pass  # the request is over

    client = mqtt.BambuClient(body.ip, body.access_code, body.serial,
                              on_connack=on_connect, on_report=on_report)
    try:
        await asyncio.to_thread(client.start)
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
        # Login and serial are proven; the client's on-connect pushall brings
        # the full status (A1 / P1S otherwise only send deltas).
        try:
            await asyncio.wait_for(got_status.wait(), TEST_REPORT_TIMEOUT)
        except asyncio.TimeoutError:
            pass
        state = report.print.get("gcode_state")
        return {
            "success": True,
            "message": f"Connected successfully. State: {state}" if state
            else "Connected successfully",
        }
    except Exception as e:
        return {"success": False, "message": str(e)}
    finally:
        mqtt.retire(client)
