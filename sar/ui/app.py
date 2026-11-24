"""FastAPI app for the operator UI (the ops screen of one ground station).

Everything shown is this station's own copy of the state: during a partition two
stations' screens can differ, which is the point of showing it per station.

The UI is stateless: every action goes through the station's ui_claim / ui_decide /
ui_release, the same path bot operators use. A station that is not the leader
forwards to the leader; with no leader reachable, verdicts are kept provisionally.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import uvicorn
from fastapi import Body, FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

STATIC = Path(__file__).parent / "static"


def build_app(station) -> FastAPI:
    app = FastAPI(title=f"SAR operator UI ({station.id})")

    def operator_name(payload: dict) -> str:
        name = str(payload.get("operator", "")).strip()[:40]
        if not name:
            raise ValueError("operator name required")
        return name

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        return (STATIC / "index.html").read_text(encoding="utf-8")

    @app.get("/api/map")
    async def map_() -> dict:
        return station.ui_map()

    @app.get("/api/state")
    async def state() -> dict:
        return station.ui_state()

    @app.post("/api/claim")
    async def claim(payload: dict = Body(...)) -> dict:
        try:
            res = await station.ui_claim(operator_name(payload))
        except ValueError as exc:
            return {"item": None, "error": str(exc)}
        if not res.get("ok"):
            return {"item": None, "error": res.get("reason", "refused")}
        return {"item": res.get("item")}

    @app.post("/api/verdict")
    async def verdict(payload: dict = Body(...)) -> dict:
        try:
            res = await station.ui_decide(operator_name(payload), str(payload["cluster_id"]),
                                          str(payload["decision"]), str(payload.get("seen", "pending")))
        except (ValueError, KeyError) as exc:
            return {"ok": False, "reason": str(exc)}
        return {"ok": bool(res.get("ok")), "reason": res.get("reason", ""), "provisional": bool(res.get("provisional"))}

    @app.get("/api/examples")
    async def examples() -> dict:
        """Reference crops for the review screen (like a training card): clear views of
        a person, a decoy and a one-off false alarm. Drawn fresh; not from the mission."""
        import random

        from ..world.sensor import thumbnail
        rng = random.Random(11)
        return {k: [thumbnail(k, 0.85, rng) for _ in range(2)] for k in ("victim", "decoy", "noise")}

    @app.get("/api/poa")
    async def poa() -> dict:
        return station.ui_poa()

    @app.post("/api/presence")
    async def presence(payload: dict = Body(...)) -> dict:
        try:
            await station.ui_presence(operator_name(payload))
        except ValueError as exc:
            return {"ok": False, "reason": str(exc)}
        return {"ok": True}

    @app.post("/api/command")
    async def command(payload: dict = Body(...)) -> dict:
        try:
            res = await station.ui_command(operator_name(payload), str(payload["action"]), dict(payload.get("args") or {}))
        except (ValueError, KeyError, TypeError) as exc:
            return {"ok": False, "reason": str(exc)}
        return {"ok": bool(res.get("ok")), "reason": res.get("reason", "")}

    @app.post("/api/inspect")
    async def inspect(payload: dict = Body(...)) -> dict:
        try:
            res = await station.ui_inspect(operator_name(payload), str(payload["cluster_id"]))
        except (ValueError, KeyError) as exc:
            return {"ok": False, "reason": str(exc)}
        return {"ok": bool(res.get("ok")), "reason": res.get("reason", "")}

    @app.post("/api/release")
    async def release(payload: dict = Body(...)) -> dict:
        try:
            res = await station.ui_release(operator_name(payload), str(payload["cluster_id"]))
        except (ValueError, KeyError) as exc:
            return {"ok": False, "reason": str(exc)}
        return {"ok": bool(res.get("ok"))}

    @app.websocket("/ws")
    async def ws(socket: WebSocket) -> None:
        await socket.accept()
        try:
            while True:
                await socket.send_json(station.ui_state())
                await asyncio.sleep(1.0)
        except (WebSocketDisconnect, RuntimeError):
            pass

    return app


async def serve(station, port: int) -> None:
    config = uvicorn.Config(build_app(station), host="127.0.0.1", port=port,
                            log_level="warning", loop="none", lifespan="off")
    server = uvicorn.Server(config)
    await server.serve()
