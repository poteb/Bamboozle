from __future__ import annotations

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

router = APIRouter()


@router.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
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

    try:
        while True:
            data = await ws.receive_json()
            if data.get("type") == "command":
                printer_id = data.get("printer_id", "")
                action = data.get("action", "")
                params = data.get("params", {})
                ok, msg = await manager.execute_command(printer_id, action, params)
                await ws.send_json(
                    {
                        "type": "command_result",
                        "printer_id": printer_id,
                        "action": action,
                        "success": ok,
                        "message": msg,
                    }
                )
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        manager.unregister_ws(ws)
