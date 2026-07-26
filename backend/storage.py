"""Репозиторий КТК: запись и чтение данных тренировок."""
from __future__ import annotations
import uuid
from datetime import datetime, timezone
from sqlalchemy import select, update, func

from .db import (Session, User, AuditLog, Scenario, ScenarioStep, TrainingSession,
                 OperatorAction, Telemetry, DetectedError, Assessment)
from .models import OperatorAction as ActionSchema, ParameterState, AIFeedback


# ------------------------------------------------------------------ пользователи

async def get_user(login: str) -> dict | None:
    async with Session() as s:
        u = (await s.execute(select(User).where(User.login == login))).scalar_one_or_none()
        return None if u is None else dict(id=u.id, login=u.login,
                                           full_name=u.full_name, role=u.role)


async def audit(user_id: int | None, event: str, details: dict | None = None,
                ip: str | None = None) -> None:
    async with Session() as s:
        s.add(AuditLog(user_id=user_id, event=event, details=details or {}, ip=ip))
        await s.commit()


# ---------------------------------------------------------------------- сценарии

async def list_scenarios() -> list[dict]:
    async with Session() as s:
        rows = (await s.execute(select(Scenario).order_by(Scenario.difficulty))).scalars().all()
        return [dict(id=r.code, name=r.name, description=r.description,
                     initial=r.initial, faults=r.faults, difficulty=r.difficulty)
                for r in rows]


async def get_scenario(code: str) -> dict | None:
    async with Session() as s:
        r = (await s.execute(select(Scenario).where(Scenario.code == code))).scalar_one_or_none()
        return None if r is None else dict(id=r.code, name=r.name,
                                           description=r.description,
                                           initial=r.initial, faults=r.faults)


async def get_reference_steps(code: str) -> list[dict]:
    """Эталонная последовательность шагов — основа для сравнения действий (ИИ)."""
    async with Session() as s:
        sc = (await s.execute(select(Scenario).where(Scenario.code == code))).scalar_one_or_none()
        if sc is None:
            return []
        rows = (await s.execute(select(ScenarioStep)
                .where(ScenarioStep.scenario_id == sc.id)
                .order_by(ScenarioStep.order_no))).scalars().all()
        return [dict(order_no=r.order_no, expected_action=r.expected_action,
                     expected_target=r.expected_target, window_s=r.window_s,
                     description=r.description, critical=r.critical) for r in rows]


# ----------------------------------------------------------------------- сессии

async def create_session(scenario_code: str, operator: str = "unknown",
                         user_id: int | None = None) -> str:
    sid = str(uuid.uuid4())
    async with Session() as s:
        s.add(TrainingSession(id=sid, scenario_code=scenario_code,
                              operator=operator, user_id=user_id))
        await s.commit()
    await audit(user_id, "session_start", {"session": sid, "scenario": scenario_code})
    return sid


async def end_session(session_id: str, status: str = "finished") -> None:
    async with Session() as s:
        await s.execute(update(TrainingSession).where(TrainingSession.id == session_id)
                        .values(ended_at=datetime.now(timezone.utc), status=status))
        await s.commit()
    await audit(None, "session_end", {"session": session_id, "status": status})


async def list_sessions(limit: int = 50) -> list[dict]:
    async with Session() as s:
        rows = (await s.execute(select(TrainingSession)
                .order_by(TrainingSession.started_at.desc()).limit(limit))).scalars().all()
        return [dict(id=r.id, scenario_code=r.scenario_code, operator=r.operator,
                     status=r.status, started_at=str(r.started_at)) for r in rows]


# ------------------------------------------------------- действия, телеметрия, ИИ

async def save_action(session_id: str, a: ActionSchema) -> None:
    async with Session() as s:
        s.add(OperatorAction(session_id=session_id, t=a.t, action=a.action.value,
                             target=a.target, value=a.value, reaction_ms=a.reaction_ms))
        await s.commit()


async def save_telemetry(session_id: str, st: ParameterState) -> None:
    async with Session() as s:
        s.add(Telemetry(session_id=session_id, t=st.t, pressure=st.pressure,
                        temperature=st.temperature, flow=st.flow, level=st.level,
                        running=st.running, alarms=st.alarms))
        await s.commit()


async def save_error(session_id: str, fb: AIFeedback) -> None:
    """Сохранить выявленную ИИ ошибку — данные для разбора и обучения модели."""
    if not fb.error_detected or not fb.error_class:
        return
    async with Session() as s:
        s.add(DetectedError(session_id=session_id, t=fb.t, error_class=fb.error_class,
                            location=fb.location, severity=fb.severity.value,
                            message=fb.message, recommendation=fb.recommendation,
                            risk_score=fb.risk_score))
        await s.commit()


async def get_journal(session_id: str) -> list[dict]:
    async with Session() as s:
        rows = (await s.execute(select(OperatorAction)
                .where(OperatorAction.session_id == session_id)
                .order_by(OperatorAction.t))).scalars().all()
        return [dict(t=r.t, action=r.action, target=r.target, value=r.value,
                     reaction_ms=r.reaction_ms) for r in rows]


async def get_errors(session_id: str) -> list[dict]:
    async with Session() as s:
        rows = (await s.execute(select(DetectedError)
                .where(DetectedError.session_id == session_id)
                .order_by(DetectedError.t))).scalars().all()
        return [dict(t=r.t, error_class=r.error_class, location=r.location,
                     severity=r.severity, message=r.message,
                     recommendation=r.recommendation, risk_score=r.risk_score)
                for r in rows]


# ------------------------------------------------- оценка квалификации оператора

async def build_assessment(session_id: str) -> dict:
    """
    Свести итог тренировки: баллы, вердикт, среднее время реакции.
    Базовая методика (уточняется Константином): 100 баллов минус штрафы
    за ошибки; критические ошибки весят больше; порог сдачи — 70.
    """
    async with Session() as s:
        errs = (await s.execute(select(DetectedError)
                .where(DetectedError.session_id == session_id))).scalars().all()
        avg_react = (await s.execute(select(func.avg(OperatorAction.reaction_ms))
                .where(OperatorAction.session_id == session_id))).scalar()
        actions_n = (await s.execute(select(func.count(OperatorAction.id))
                .where(OperatorAction.session_id == session_id))).scalar() or 0

        critical = sum(1 for e in errs if e.severity == "error")
        score = max(0.0, 100.0 - critical * 25.0 - (len(errs) - critical) * 8.0)
        verdict = "passed" if score >= 70 else "not_passed"
        by_class: dict[str, int] = {}
        for e in errs:
            by_class[e.error_class] = by_class.get(e.error_class, 0) + 1

        row = Assessment(
            session_id=session_id, total_score=round(score, 1),
            errors_count=len(errs), critical_errors=critical,
            avg_reaction_ms=int(avg_react) if avg_react else None,
            verdict=verdict,
            details={"errors_by_class": by_class, "actions": actions_n},
        )
        s.add(row)
        await s.commit()
        return dict(session_id=session_id, total_score=row.total_score,
                    errors_count=row.errors_count, critical_errors=critical,
                    avg_reaction_ms=row.avg_reaction_ms, verdict=verdict,
                    details=row.details)


async def get_assessment(session_id: str) -> dict | None:
    async with Session() as s:
        r = (await s.execute(select(Assessment)
             .where(Assessment.session_id == session_id)
             .order_by(Assessment.created_at.desc()))).scalars().first()
        return None if r is None else dict(
            session_id=r.session_id, total_score=r.total_score,
            errors_count=r.errors_count, critical_errors=r.critical_errors,
            avg_reaction_ms=r.avg_reaction_ms, verdict=r.verdict, details=r.details)
