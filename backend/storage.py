"""Репозиторий: запись/чтение сессий, журнала действий и телеметрии."""
from __future__ import annotations
import uuid
from datetime import datetime, timezone
from sqlalchemy import select, update

from .db import Session, TrainingSessionRow, ActionRow, TelemetryRow
from .models import OperatorAction, ParameterState


async def create_session(scenario_id: str, operator: str = "unknown") -> str:
    sid = str(uuid.uuid4())
    async with Session() as s:
        s.add(TrainingSessionRow(id=sid, scenario_id=scenario_id, operator=operator))
        await s.commit()
    return sid


async def end_session(session_id: str) -> None:
    async with Session() as s:
        await s.execute(
            update(TrainingSessionRow).where(TrainingSessionRow.id == session_id)
            .values(ended_at=datetime.now(timezone.utc), finished=True)
        )
        await s.commit()


async def save_action(session_id: str, a: OperatorAction) -> None:
    async with Session() as s:
        s.add(ActionRow(session_id=session_id, t=a.t, action=a.action.value,
                        target=a.target, value=a.value, reaction_ms=a.reaction_ms))
        await s.commit()


async def save_telemetry(session_id: str, st: ParameterState) -> None:
    async with Session() as s:
        s.add(TelemetryRow(session_id=session_id, t=st.t, pressure=st.pressure,
                           temperature=st.temperature, flow=st.flow, level=st.level,
                           running=st.running, alarms=st.alarms))
        await s.commit()


async def get_journal(session_id: str) -> list[dict]:
    async with Session() as s:
        rows = (await s.execute(
            select(ActionRow).where(ActionRow.session_id == session_id).order_by(ActionRow.t)
        )).scalars().all()
        return [dict(t=r.t, action=r.action, target=r.target, value=r.value,
                     reaction_ms=r.reaction_ms) for r in rows]


async def list_sessions() -> list[dict]:
    async with Session() as s:
        rows = (await s.execute(
            select(TrainingSessionRow).order_by(TrainingSessionRow.started_at.desc())
        )).scalars().all()
        return [dict(id=r.id, scenario_id=r.scenario_id, operator=r.operator,
                     started_at=str(r.started_at), finished=r.finished) for r in rows]
