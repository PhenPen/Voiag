"""FastAPI app: serves the page and relays one websocket per call."""

from __future__ import annotations

import logging

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import tools
from .config import ROOT, SETTINGS, load_business_config
from .live import Bridge
from .store import Store

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("voiag")

BUSINESS = tools.Business(load_business_config())
STORE = Store(SETTINGS.db_path)
WEB = ROOT / "web"

# Settings are read from .env once, when this module is imported. Log what was
# loaded so a stale or missing key is obvious instead of looking like a model bug.
log.info("business=%s model=%s voice=%s tz=%s", BUSINESS.name, SETTINGS.model, SETTINGS.voice, BUSINESS.tz)
if SETTINGS.api_key:
    log.info("GEMINI_API_KEY loaded (%s...)", SETTINGS.api_key[:6])
else:
    log.warning("GEMINI_API_KEY is missing — add it to .env, then restart the server")

app = FastAPI(title="Voiag", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=WEB), name="static")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(WEB / "index.html")


@app.get("/health")
def health() -> dict:
    return {
        "ok": True,
        "model": SETTINGS.model,
        "voice": SETTINGS.voice,
        "key_configured": bool(SETTINGS.api_key),
        "business": BUSINESS.name,
    }


@app.get("/appointments")
def appointments() -> dict:
    return {"appointments": STORE.list_upcoming(BUSINESS.tz)}


@app.websocket("/ws")
async def call_socket(websocket: WebSocket) -> None:
    await websocket.accept()

    if not SETTINGS.api_key:
        await websocket.send_json(
            {
                "type": "error",
                "message": (
                    "No GEMINI_API_KEY found. Copy .env.example to .env, "
                    "paste your key, then restart the server."
                ),
            }
        )
        await websocket.close()
        return

    bridge = Bridge(
        websocket=websocket,
        business=BUSINESS,
        store=STORE,
        api_key=SETTINGS.api_key,
        model=SETTINGS.model,
        voice=SETTINGS.voice,
    )

    try:
        await bridge.run()
    except WebSocketDisconnect:
        log.info("caller disconnected")
