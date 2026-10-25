from __future__ import annotations

import asyncio
import logging
from urllib.parse import urlsplit

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

logger = logging.getLogger(__name__)

router = APIRouter()

# Commands one dashboard may have waiting for confirmation at once.
MAX_PENDING_COMMANDS = 4


def same_origin(ws: WebSocket) -> bool:
    """False when a page from another origin opened this socket.

    Browsers send `Origin` on every WebSocket handshake and don't apply CORS
    to WebSockets, so without this check any web page the user visits could
    open ws://127.0.0.1:<port>/ws, read the printers and send them commands.
    Non-browser clients send no Origin and are allowed.
    """
    origin = ws.headers.get("origin")
    if origin is None:
        return True
    host = ws.headers.get("host", "")
    try:
        return urlsplit(origin).netloc.lower() == host.lower() and bool(host)
    except ValueError:
        return False


@router.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    if not same_origin(ws):
        logger.warning("Refused a dashboard WebSocket from origin %r", ws.headers.get("origin"))
        await ws.close(code=1008)  # policy violation
        return
    await ws.accept()
    manager = ws.app.state.manager
    manager.register_ws(ws)

    # Send initial full state
    states = manager.get_all_states()
    init_msg = {
        "type": "init",
        "printers": {
            pid: state.model_dump(mode="json") for pid, state in states.items()
        },
    }
    await ws.send_json(init_msg)

    commands: set[asyncio.Task] = set()

    async def run_command(printer_id, action, params):
        # A command waits for the printer to confirm it (up to ~10 s); in its
        # own task, so the socket keeps reading meanwhile.
        result = await manager.execute_command(printer_id, action, params)
        try:
            await ws.send_json({"type": "command_result", "printer_id": printer_id,
                                "action": action, **result._asdict()})
        except Exception:
            pass  # the dashboard went away meanwhile

    try:
        while True:
            data = await ws.receive_json()
            if not isinstance(data, dict):
                continue
            if data.get("type") == "command":
                printer_id = str(data.get("printer_id", ""))
                action = str(data.get("action", ""))
                if len(commands) >= MAX_PENDING_COMMANDS:
                    await ws.send_json({
                        "type": "command_result", "printer_id": printer_id, "action": action,
                        "success": False, "confirmed": None,
                        "message": "Busy: wait for the earlier commands to finish",
                    })
                    continue
                task = asyncio.create_task(run_command(printer_id, action, data.get("params") or {}))
                commands.add(task)
                task.add_done_callback(commands.discard)
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        manager.unregister_ws(ws)
        for task in commands:
            task.cancel()
