"""
КТК ЭЛОУ-АВТ — backend (неделя 2).
FastAPI + WebSocket: сессии тренировки, исполнение сценариев с отказами по
времени, хранение журнала и телеметрии в БД (PostgreSQL / SQLite-fallback).

Запуск:  uvicorn backend.main:app --reload
Открыть: http://localhost:8000
"""
from __future__ import annotations
import asyncio
import json
import time
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse

from .models import ControlCommand, OperatorAction, WSMessage, WSMessageType
from .session import TrainingSession
from .scenarios import SCENARIOS
from . import db, storage

app = FastAPI(title="КТК ЭЛОУ-АВТ", version="0.2.0")
FRONTEND = Path(__file__).resolve().parent.parent / "frontend"


@app.on_event("startup")
async def _startup():
    await db.init_db()


@app.get("/api/scenarios")
def list_scenarios():
    return [s.model_dump() for s in SCENARIOS]


@app.get("/api/sessions")
async def sessions():
    return await storage.list_sessions()


@app.get("/api/sessions/{session_id}/journal")
async def journal(session_id: str):
    return await storage.get_journal(session_id)


@app.get("/")
def index():
    return FileResponse(FRONTEND / "index.html")


@app.websocket("/ws")
async def ws(websocket: WebSocket):
    """
    Канал тренировки.
    UI -> сервер:
      {"session_action":"start","scenario":"pump_trip"}  — начать сценарий
      {"session_action":"reset"}                          — перезапустить
      {"session_action":"stop"}                           — завершить
      {"action":"set_valve","value":40, ...}              — команда оператора
    сервер -> UI: WSMessage(state|feedback|action)
    """
    await websocket.accept()
    sess = TrainingSession()
    last_event_t = time.time()
    ticker_task: asyncio.Task | None = None

    async def ticker():
        try:
            while True:
                if sess.active:
                    state, feedback, events = sess.tick()
                    await websocket.send_text(WSMessage(
                        type=WSMessageType.STATE, payload=state.model_dump()).model_dump_json())
                    await websocket.send_text(WSMessage(
                        type=WSMessageType.FEEDBACK, payload=feedback.model_dump()).model_dump_json())
                    if sess.session_id:
                        await storage.save_telemetry(sess.session_id, state)
                await asyncio.sleep(1.0)
        except (WebSocketDisconnect, RuntimeError):
            pass

    ticker_task = asyncio.create_task(ticker())
    try:
        while True:
            raw = await websocket.receive_text()
            msg = json.loads(raw)

            # --- управление сессией ---
            if "session_action" in msg:
                sa = msg["session_action"]
                if sa == "start":
                    if sess.start(msg.get("scenario", "startup")):
                        sess.session_id = await storage.create_session(
                            sess.scenario_id, sess.operator)
                        last_event_t = time.time()
                elif sa == "reset":
                    sess.reset(); last_event_t = time.time()
                elif sa == "stop":
                    sess.stop()
                    if sess.session_id:
                        await storage.end_session(sess.session_id)
                continue

            # --- команда оператора ---
            cmd = ControlCommand(**msg)
            sess.command(cmd)
            reaction = int((time.time() - last_event_t) * 1000)
            action = OperatorAction(t=sess.engine.t, action=cmd.action,
                                    target=cmd.target, value=cmd.value, reaction_ms=reaction)
            if sess.session_id:
                await storage.save_action(sess.session_id, action)
            await websocket.send_text(WSMessage(
                type=WSMessageType.ACTION, payload=action.model_dump()).model_dump_json())
    except WebSocketDisconnect:
        pass
    finally:
        if ticker_task:
            ticker_task.cancel()
