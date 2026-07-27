"""
Общая подготовка автотестов.

Тесты работают на отдельной базе SQLite во временном файле — рабочая база
ktk.db не затрагивается. Переменные окружения выставляются ДО импорта
backend, потому что DATABASE_URL и настройки безопасности читаются
на этапе импорта модулей.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

_TMP_DB = Path(tempfile.gettempdir()) / "ktk_tests.db"
_TMP_DB.unlink(missing_ok=True)

os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_TMP_DB}"
os.environ["KTK_SECRET_KEY"] = "test-secret-key-длиной-не-меньше-32-байт-для-HS256"
os.environ["KTK_MAX_FAILED_ATTEMPTS"] = "3"     # чтобы тест блокировки был короче
os.environ["KTK_LOCKOUT_S"] = "60"

from fastapi.testclient import TestClient          # noqa: E402
from backend.main import app                       # noqa: E402
from backend import security                       # noqa: E402


@pytest.fixture(scope="session")
def client():
    """Клиент с поднятым приложением: на старте создаются таблицы и сид."""
    with TestClient(app) as c:
        yield c
    _TMP_DB.unlink(missing_ok=True)


@pytest.fixture(autouse=True)
def _clean_state(client):
    """Перед каждым тестом: без активного сеанса и без накопленных неудач входа."""
    client.cookies.clear()
    security.reset_failures()
    yield


@pytest.fixture()
def db_query(client):
    """
    Прямой SELECT к тестовой базе — чтобы проверять то, что реально записано
    (хеши паролей, журнал аудита), а не только ответы API.
    """
    import sqlite3

    def _q(sql: str, params: tuple = ()):
        conn = sqlite3.connect(_TMP_DB)
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()
    return _q


def login_as(client, login: str, password: str | None = None):
    """Войти в систему и вернуть ответ. По умолчанию пароль равен логину."""
    return client.post("/api/login", json={"login": login,
                                           "password": password if password is not None else login})
