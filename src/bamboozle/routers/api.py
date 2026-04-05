from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from ..config import save_config
from ..models import PrinterAddRequest, PrinterConfig, PrinterState, PrinterUpdateRequest

router = APIRouter()


@router.get("/printers")
async def list_printers(request: Request) -> dict:
    manager = request.app.state.manager
    states = manager.get_all_states()
    return {
        "printers": [s.model_dump(mode="json") for s in states.values()]
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

    if enabled:
        conn.camera.start()
    else:
        conn.camera.stop()

    return {"success": True, "camera_enabled": enabled}


@router.post("/printers/test")
async def test_connection(body: PrinterAddRequest) -> dict:
    """Test connecting to a printer without saving it."""
    import bambulabs_api as bl
    import asyncio

    printer = bl.Printer(body.ip, body.access_code, body.serial)
    try:
        await asyncio.to_thread(printer.mqtt_start)
        await asyncio.sleep(3)
        state = await asyncio.to_thread(printer.get_state)
        await asyncio.to_thread(printer.mqtt_stop)
        return {
            "success": True,
            "message": f"Connected successfully. State: {state}",
        }
    except Exception as e:
        try:
            await asyncio.to_thread(printer.mqtt_stop)
        except Exception:
            pass
        return {"success": False, "message": str(e)}
