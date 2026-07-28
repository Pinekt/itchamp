"""
Автотесты схемы БД — то, что SQLite прощает, а PostgreSQL нет.

Автотесты работают на SQLite: он не требует поднятого сервера и создаёт базу
во временном файле. Расплата за это — SQLite почти не проверяет типы, и часть
ошибок всплывает только на боевой базе.

Так и вышло: все колонки со временем были объявлены как `DateTime` (без пояса),
а приложение писало в них время со смещением. SQLite принимал это молча, а
PostgreSQL на первом же запуске отказался наполнять таблицу пользователей:

    invalid input for query argument $6:
    can't subtract offset-naive and offset-aware datetimes

Тесты ниже проверяют схему, а не поведение, — зато ловят такие расхождения без
поднятого PostgreSQL.
"""
from __future__ import annotations

import pytest
from sqlalchemy import DateTime, String

from backend import db

#: Все таблицы приложения.
TABLES = [db.User, db.AuditLog, db.Scenario, db.ScenarioStep, db.TrainingSession,
          db.OperatorAction, db.Telemetry, db.DetectedError, db.Assessment]


def datetime_columns():
    for model in TABLES:
        for col in model.__table__.columns:
            if isinstance(col.type, DateTime):
                yield model.__tablename__, col


def test_every_timestamp_column_keeps_timezone():
    """
    Приложение пишет время в UTC со смещением (`db._now`). Колонка без пояса
    превращается в PostgreSQL в TIMESTAMP WITHOUT TIME ZONE, и asyncpg такое
    значение не примет — стенд падает на старте, ещё до первого запроса.
    """
    naive = [f"{table}.{col.name}" for table, col in datetime_columns()
             if not col.type.timezone]
    assert not naive, (
        "колонки со временем без пояса — PostgreSQL их не примет: "
        + ", ".join(naive))


def test_there_are_timestamp_columns_to_check():
    """Страховка от «зелёного» теста, если модели вдруг перестанут находиться."""
    assert len(list(datetime_columns())) >= 8


def test_now_returns_aware_time():
    """Обратная сторона проверки выше: время должно быть со смещением."""
    assert db._now().tzinfo is not None


@pytest.mark.parametrize("model,column,least", [
    # Argon2id даёт хеш около 95 символов; запас нужен на смену параметров
    (db.User, "password_hash", 128),
    # ФИО целиком переносится в training_sessions.operator
    (db.User, "full_name", 128),
    # IPv6 — до 45 символов
    (db.AuditLog, "ip", 45),
])
def test_string_columns_are_long_enough(model, column, least):
    """
    PostgreSQL обрежет или отвергнет длинное значение, SQLite молча запишет.
    Проверяем запас там, где длина задаётся не нами.
    """
    col = model.__table__.columns[column]
    assert isinstance(col.type, String) and col.type.length >= least


def test_operator_column_fits_full_name():
    """
    В training_sessions.operator кладётся users.full_name целиком — колонка
    не должна быть короче, иначе длинное ФИО не сохранится.
    """
    assert (db.TrainingSession.__table__.columns["operator"].type.length
            >= db.User.__table__.columns["full_name"].type.length)


def test_audit_event_column_fits_longest_event():
    """Названия событий заданы в коде — проверяем, что самое длинное влезает."""
    events = ["login_success", "login_failed", "login_blocked", "logout",
              "access_denied", "password_rehashed", "password_reset",
              "session_start", "session_end", "user_created", "user_updated",
              "debrief_released"]
    assert db.AuditLog.__table__.columns["event"].type.length >= max(map(len, events))
