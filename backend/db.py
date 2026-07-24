"""
Слой хранения КТК (задачи капитана, неделя 2).

SQLAlchemy async. По умолчанию — PostgreSQL (asyncpg), для локальной разработки
без БД — автоматический fallback на SQLite. Управляется переменной окружения:

    DATABASE_URL=postgresql+asyncpg://ktk:ktk@localhost:5432/ktk   # прод/командная
    DATABASE_URL=sqlite+aiosqlite:///./ktk.db                      # локально (по умолчанию)
"""
from __future__ import annotations
import os
from datetime import datetime, timezone

from sqlalchemy import String, Float, Integer, DateTime, JSON, Boolean
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./ktk.db")

engine = create_async_engine(DATABASE_URL, echo=False, future=True)
Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


class TrainingSessionRow(Base):
    __tablename__ = "training_sessions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    scenario_id: Mapped[str] = mapped_column(String(64))
    operator: Mapped[str] = mapped_column(String(128), default="unknown")
    started_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished: Mapped[bool] = mapped_column(Boolean, default=False)


class ActionRow(Base):
    __tablename__ = "operator_actions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(String(36), index=True)
    t: Mapped[float] = mapped_column(Float)
    action: Mapped[str] = mapped_column(String(32))
    target: Mapped[str | None] = mapped_column(String(64), nullable=True)
    value: Mapped[float | None] = mapped_column(Float, nullable=True)
    reaction_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class TelemetryRow(Base):
    __tablename__ = "telemetry"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(String(36), index=True)
    t: Mapped[float] = mapped_column(Float)
    pressure: Mapped[float] = mapped_column(Float)
    temperature: Mapped[float] = mapped_column(Float)
    flow: Mapped[float] = mapped_column(Float)
    level: Mapped[float] = mapped_column(Float)
    running: Mapped[bool] = mapped_column(Boolean)
    alarms: Mapped[list] = mapped_column(JSON, default=list)


async def init_db(retries: int = 15, delay: float = 2.0) -> None:
    """Создать таблицы. Ждём готовности БД (важно при старте в docker-compose)."""
    import asyncio
    last_err: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            return
        except Exception as e:  # БД ещё поднимается — ждём и пробуем снова
            last_err = e
            print(f"[db] БД не готова (попытка {attempt}/{retries}): {e}")
            await asyncio.sleep(delay)
    raise RuntimeError(f"Не удалось подключиться к БД после {retries} попыток: {last_err}")
