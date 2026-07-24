"""
КТК ЭЛОУ-АВТ — backend (каркас).
FastAPI + WebSocket: real-time поток состояния установки и обратной связи ИИ.
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
from fastapi.staticfiles import StaticFiles

from .models import ControlCommand, OperatorAction, WSMessage, WSMessageType
from .engine import SimulationEngine
from .ai_module import ErrorAnalyzer
from .scenarios import SCENARIOS

app = FastAPI(title="КТК ЭЛОУ-АВТ", version="0.1.0")

FRONTEND = Path(__file__).resolve().parent.parent / "frontend"

# журнал действий в памяти (на неделе 4 -> PostgreSQL)
JOURNAL: list[OperatorAction] = []


@app.get("/api/scenarios")
def list_scenarios():
    return [s.model_dump() for s in SCENARIOS]


@app.get("/api/journal")
def get_journal():
    return [a.model_dump() for a in JOURNAL]


@app.get("/")
def index():
    return FileResponse(FRONTEND / "index.html")


@app.websocket("/ws")
async def ws(websocket: WebSocket):
    """Двусторонний канал: <- команды оператора, -> состояние + фидбэк ИИ."""
    await websocket.accept()
    engine = SimulationEngine()
    ai = ErrorAnalyzer()
    last_action: OperatorAction | None = None
    last_event_t = time.time()

    async def receiver():
        nonlocal last_action, last_event_t
        try:
            while True:
                raw = await websocket.receive_text()
                cmd = ControlCommand(**json.loads(raw))
                engine.apply(cmd)
                reaction = int((time.time() - last_event_t) * 1000)
                last_action = OperatorAction(
                    t=engine.t, action=cmd.action, target=cmd.target,
                    value=cmd.value, reaction_ms=reaction,
                )
                JOURNAL.append(last_action)
                await websocket.send_text(WSMessage(
                    type=WSMessageType.ACTION, payload=last_action.model_dump()
                ).model_dump_json())
        except WebSocketDisconnect:
            pass

    async def ticker():
        nonlocal last_action
        try:
            while True:
                state = engine.step(dt=1.0)
                await websocket.send_text(WSMessage(
                    type=WSMessageType.STATE, payload=state.model_dump()
                ).model_dump_json())
                feedback = ai.analyze(state, last_action)
                await websocket.send_text(WSMessage(
                    type=WSMessageType.FEEDBACK, payload=feedback.model_dump()
                ).model_dump_json())
                last_action = None
                await asyncio.sleep(1.0)
        except (WebSocketDisconnect, RuntimeError):
            pass

    await asyncio.gather(receiver(), ticker())
