"""Control center web app (served by the launcher).

Routes
  GET  /                    the control center page
  GET  /api/map             terrain plus true victim and decoy positions
  GET  /api/state           processes, active faults, observer view, live metrics
  WS   /ws                  /api/state pushed once a second
  POST /api/fault           {kind: isolate|cut|jam, nodes, zone, duration_min}
  POST /api/fault/end       {id}
  POST /api/faults/clear
  POST /api/loss            {loss: 0..0.95}
  POST /api/node/kill       {node}
  POST /api/node/restart    {node}
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import uvicorn
from fastapi import Body, FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

STATIC = Path(__file__).parent / "static"


def build_app(launcher) -> FastAPI:
    app = FastAPI(title="SAR control center")

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        return (STATIC / "control.html").read_text(encoding="utf-8")

    @app.get("/api/map")
    async def map_() -> dict:
        return launcher.map_view()

    @app.get("/api/state")
    async def state() -> dict:
        return launcher.state_view()

    @app.post("/api/fault")
    async def fault(p: dict = Body(...)) -> dict:
        return launcher.add_fault(str(p.get("kind")), p.get("nodes"), p.get("zone"), p.get("duration_min"))

    @app.post("/api/fault/end")
    async def fault_end(p: dict = Body(...)) -> dict:
        return launcher.end_fault(str(p.get("id")))

    @app.post("/api/faults/clear")
    async def faults_clear() -> dict:
        return launcher.clear_faults()

    @app.post("/api/clock")
    async def clock(p: dict = Body(...)) -> dict:
        action = p.get("action")
        if action == "pause":
            return launcher.pause()
        if action == "resume":
            return launcher.resume()
        if action == "speed":
            return launcher.set_speed(float(p.get("speed", 0)))
        return {"ok": False, "reason": f"unknown action {action!r}"}

    @app.post("/api/loss")
    async def loss(p: dict = Body(...)) -> dict:
        return launcher.set_loss(float(p.get("loss", 0)))

    @app.post("/api/node/kill")
    async def kill(p: dict = Body(...)) -> dict:
        return launcher.kill(str(p.get("node")))

    @app.post("/api/node/restart")
    async def restart(p: dict = Body(...)) -> dict:
        return launcher.restart(str(p.get("node")))

    @app.websocket("/ws")
    async def ws(socket: WebSocket) -> None:
        await socket.accept()
        try:
            while True:
                await socket.send_json(launcher.state_view())
                await asyncio.sleep(1.0)
        except (WebSocketDisconnect, RuntimeError):
            pass

    return app


async def serve(launcher, port: int) -> None:
    config = uvicorn.Config(build_app(launcher), host="127.0.0.1", port=port,
                            log_level="warning", loop="none", lifespan="off")
    await uvicorn.Server(config).serve()
