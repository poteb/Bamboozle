from __future__ import annotations

import logging
import threading
import webbrowser
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .config import load_config
from .printer_manager import PrinterManager
from .routers import api, pages, stream, ws

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    config = load_config()
    manager = PrinterManager(config)
    app.state.config = config
    app.state.manager = manager
    await manager.start()
    logger.info("Bamboozle started on http://127.0.0.1:%d", config.port)
    yield
    await manager.stop()
    logger.info("Bamboozle stopped")


app = FastAPI(title="Bamboozle", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="bamboozle/static"), name="static")
app.include_router(pages.router)
app.include_router(api.router, prefix="/api")
app.include_router(ws.router)
app.include_router(stream.router)


def _open_browser(port: int) -> None:
    import time
    time.sleep(2)
    webbrowser.open(f"http://127.0.0.1:{port}")


if __name__ == "__main__":
    import uvicorn

    config = load_config()
    threading.Thread(target=_open_browser, args=(config.port,), daemon=True).start()
    uvicorn.run(
        "bamboozle.main:app",
        host="127.0.0.1",
        port=config.port,
        log_level="info",
    )
