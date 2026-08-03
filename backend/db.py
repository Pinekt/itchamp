"""
Схема данных КТК ЭЛОУ-АВТ (SQLAlchemy async).

Схема спроектирована так, чтобы напрямую закрывать критерии оценки кейса:
  • users, audit_log            → ИБ: разграничение доступа по ролям + аудит действий
  • scenarios, scenario_steps   → эталонные сценарии (база для сравнения действий)
  • training_sessions, actions  → журнал действий и времени реакции (тех. реализация)
  • telemetry                   → история параметров установки (демонстрация, разбор)
  • detected_errors             → выход ИИ-модуля: классификация и локализация ошибок
  • assessments                 → оценка квалификации оператора (сдал/не сдал + баллы)

БД задаётся переменной окружения:
    DATABASE_URL=postgresql+asyncpg://ktk:ktk@localhost:5432/ktk_eloyavt
    DATABASE_URL=sqlite+aiosqlite:///./ktk.db     (локально, по умолчанию)
"""
from __future__ import annotations
import os
import asyncio
from datetime import datetime, timezone

from sqlalchemy import (String, Float, Integer, DateTime, JSON, Boolean, Text,
                        ForeignKey)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./ktk.db")
engine = create_async_engine(DATABASE_URL, echo=False, future=True)
Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def _now() -> datetime:
    """
    Текущее время в UTC — со смещением, а не «голое».

    Все колонки со временем объявлены как DateTime(timezone=True), то есть
    TIMESTAMPTZ в PostgreSQL. Пара «наивное время в колонке без пояса» тоже
    работала бы, но повод выбрать пояс есть: журнал аудита — это про «когда
    именно», а сервер стенда и рабочие места могут жить в разных зонах.

    Важно: смешивать нельзя. Время со смещением в колонку без пояса asyncpg
    не примет вовсе (SQLite примет молча — на нём эта ошибка не всплывает).
    """
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------- ИБ: роли и аудит

class User(Base):
    """Пользователь КТК. Роли: operator | instructor | admin."""
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    login: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    full_name: Mapped[str] = mapped_column(String(128))
    role: Mapped[str] = mapped_column(String(16), default="operator")
    password_hash: Mapped[str] = mapped_column(String(256), default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class AuditLog(Base):
    """Аудит действий пользователей — требование критерия ИБ."""
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    event: Mapped[str] = mapped_column(String(64))        # login, session_start, ...
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)


# ------------------------------------------------------- сценарии и эталонные шаги

class Scenario(Base):
    """Учебный сценарий. Инструктор может добавлять свои — потому в БД, а не в коде."""
    __tablename__ = "scenarios"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(160))
    description: Mapped[str] = mapped_column(Text, default="")
    initial: Mapped[dict] = mapped_column(JSON, default=dict)   # начальное состояние
    faults: Mapped[list] = mapped_column(JSON, default=list)    # [{at,target,type}]
    difficulty: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class ScenarioStep(Base):
    """Шаг эталонной последовательности — с чем ИИ сравнивает действия обучаемого."""
    __tablename__ = "scenario_steps"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    scenario_id: Mapped[int] = mapped_column(ForeignKey("scenarios.id"), index=True)
    order_no: Mapped[int] = mapped_column(Integer)
    expected_action: Mapped[str] = mapped_column(String(32))
    expected_target: Mapped[str | None] = mapped_column(String(64), nullable=True)
    window_s: Mapped[float] = mapped_column(Float, default=30.0)  # допустимое окно, с
    description: Mapped[str] = mapped_column(Text, default="")
    critical: Mapped[bool] = mapped_column(Boolean, default=False)


# --------------------------------------------------------- тренировка и её данные

class TrainingSession(Base):
    __tablename__ = "training_sessions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    scenario_code: Mapped[str] = mapped_column(String(64), index=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    operator: Mapped[str] = mapped_column(String(128), default="unknown")
    status: Mapped[str] = mapped_column(String(16), default="active")  # active|finished|aborted
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class OperatorAction(Base):
    __tablename__ = "operator_actions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("training_sessions.id"), index=True)
    t: Mapped[float] = mapped_column(Float)
    action: Mapped[str] = mapped_column(String(32))
    target: Mapped[str | None] = mapped_column(String(64), nullable=True)
    value: Mapped[float | None] = mapped_column(Float, nullable=True)
    reaction_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Telemetry(Base):
    __tablename__ = "telemetry"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("training_sessions.id"), index=True)
    t: Mapped[float] = mapped_column(Float)
    pressure: Mapped[float] = mapped_column(Float)
    temperature: Mapped[float] = mapped_column(Float)
    flow: Mapped[float] = mapped_column(Float)
    level: Mapped[float] = mapped_column(Float)
    running: Mapped[bool] = mapped_column(Boolean)
    alarms: Mapped[list] = mapped_column(JSON, default=list)


class DetectedError(Base):
    """Ошибка, выявленная ИИ-модулем: класс, локализация, объяснение, риск."""
    __tablename__ = "detected_errors"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("training_sessions.id"), index=True)
    t: Mapped[float] = mapped_column(Float)
    error_class: Mapped[str] = mapped_column(String(64), index=True)
    location: Mapped[str | None] = mapped_column(String(64), nullable=True)
    severity: Mapped[str] = mapped_column(String(16), default="info")
    message: Mapped[str] = mapped_column(Text, default="")
    recommendation: Mapped[str | None] = mapped_column(Text, nullable=True)
    risk_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Assessment(Base):
    """Итог тренировки: баллы, вердикт, разбор — оценка квалификации персонала."""
    __tablename__ = "assessments"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("training_sessions.id"), index=True)
    total_score: Mapped[float] = mapped_column(Float, default=0.0)   # 0..100
    errors_count: Mapped[int] = mapped_column(Integer, default=0)
    critical_errors: Mapped[int] = mapped_column(Integer, default=0)
    avg_reaction_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    verdict: Mapped[str] = mapped_column(String(32), default="not_passed")  # passed|not_passed
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


async def init_db(retries: int = 15, delay: float = 2.0) -> None:
    """Создать таблицы. Ждём готовности БД (важно при старте в docker-compose)."""
    last_err: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            return
        except Exception as e:
            last_err = e
            print(f"[db] БД не готова (попытка {attempt}/{retries}): {e}")
            await asyncio.sleep(delay)
    raise RuntimeError(f"Не удалось подключиться к БД после {retries} попыток: {last_err}")
