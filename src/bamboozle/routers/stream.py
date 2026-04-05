from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response, StreamingResponse

router = APIRouter()


@router.get("/stream/{printer_id}")
async def mjpeg_stream(request: Request, printer_id: str):
    """Stream MJPEG from a printer's camera."""
    manager = request.app.state.manager
    camera = manager.get_camera(printer_id)
    if not camera:
        raise HTTPException(404, "Printer not found")

    async def generate():
        try:
            last_frame = None
            while True:
                if await request.is_disconnected():
                    break
                frame = camera.get_frame()
                if frame and frame is not last_frame:
                    last_frame = frame
                    yield (
                        b"--frame\r\n"
                        b"Content-Type: image/jpeg\r\n"
                        b"Content-Length: " + str(len(frame)).encode() + b"\r\n\r\n"
                        + frame + b"\r\n"
                    )
                await asyncio.sleep(0.1)
        except asyncio.CancelledError:
            pass

    return StreamingResponse(
        generate(),
        media_type="multipart/x-mixed-replace; boundary=frame",
    )


@router.get("/stream/{printer_id}/snapshot")
async def snapshot(request: Request, printer_id: str):
    """Get a single JPEG frame from a printer's camera."""
    manager = request.app.state.manager
    camera = manager.get_camera(printer_id)
    if not camera:
        raise HTTPException(404, "Printer not found")

    frame = camera.get_frame()
    if not frame:
        raise HTTPException(503, "No camera frame available")

    return Response(content=frame, media_type="image/jpeg")


@router.get("/stream/{printer_id}/thumbnail")
async def thumbnail(request: Request, printer_id: str):
    """Get the print job thumbnail PNG."""
    manager = request.app.state.manager
    conn = manager.get_connection(printer_id)
    if not conn:
        raise HTTPException(404, "Printer not found")

    if not conn.thumbnail:
        raise HTTPException(404, "No thumbnail available")

    return Response(content=conn.thumbnail, media_type="image/png")
