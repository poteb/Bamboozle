from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

router = APIRouter()
templates = Jinja2Templates(directory="bamboozle/templates")


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    manager = request.app.state.manager
    has_printers = len(manager.config.printers) > 0
    return templates.TemplateResponse(
        request, "dashboard.html", {"has_printers": has_printers}
    )


@router.get("/camera/{printer_id}", response_class=HTMLResponse)
async def camera_view(request: Request, printer_id: str):
    manager = request.app.state.manager
    conn = manager.get_connection(printer_id)
    name = conn.cfg.name if conn else "Unknown"
    return templates.TemplateResponse(
        request, "camera.html", {"printer_id": printer_id, "printer_name": name}
    )


@router.get("/settings", response_class=HTMLResponse)
async def settings(request: Request):
    manager = request.app.state.manager
    printers = sorted(
        manager.config.printers, key=lambda p: p.name.lower()
    )
    return templates.TemplateResponse(
        request, "settings.html", {"printers": printers}
    )
