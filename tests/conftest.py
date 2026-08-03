"""
Общая подготовка автотестов.

По умолчанию тесты работают на отдельной базе SQLite во временном файле —
поднимать сервер БД не нужно, рабочая база `ktk.db` не затрагивается.

Но SQLite почти не проверяет типы, и часть ошибок на нём не воспроизводится:
время со смещением в колонке без пояса он принимал молча, а PostgreSQL
отказался наполнять таблицу пользователей — стенд не поднялся. Поэтому тот же
набор тестов должен уметь гоняться и на настоящей базе:

    DATABASE_URL=postgresql+asyncpg://postgres@127.0.0.1:5432/ktk_test pytest

Именно так работает вторая половина CI (см. `.github/workflows/tests.yml`).
Переменные окружения выставляются ДО импорта backend: `DATABASE_URL` и
настройки безопасности читаются на этапе импорта модулей.
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
from pathlib import Path

import pytest

#: Файл временной базы SQLite. None, если тесты запущены на внешней базе.
_TMP_DB: Path | None = None

if os.environ.get("DATABASE_URL"):
    # база задана снаружи (CI на PostgreSQL) — ничего не подменяем
    pass
else:
    _TMP_DB = Path(tempfile.gettempdir()) / "ktk_tests.db"
    _TMP_DB.unlink(missing_ok=True)
    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_TMP_DB}"

os.environ.setdefault("KTK_SECRET_KEY",
                      "test-secret-key-длиной-не-меньше-32-байт-для-HS256")
os.environ["KTK_MAX_FAILED_ATTEMPTS"] = "3"     # чтобы тест блокировки был короче
os.environ["KTK_LOCKOUT_S"] = "60"
# такт симуляции — 50 мс вместо секунды: тесты, которым нужно несколько тактов
# телеметрии, иначе ждали бы реальные секунды на каждый шаг
os.environ["KTK_TICK_PERIOD_S"] = "0.05"

from sqlalchemy import text                        # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402
from fastapi.testclient import TestClient          # noqa: E402
from backend.main import app                       # noqa: E402
from backend import db as db_module                # noqa: E402
from backend import security                       # noqa: E402


def _wipe_database() -> None:
    """
    Очистить базу перед прогоном.

    Для SQLite достаточно удалить файл, а внешняя база живёт между запусками:
    её нужно чистить явно, иначе `seed` не сработает (он наполняет только
    пустую таблицу пользователей), а тесты увидят чужие записи аудита.
    """
    if _TMP_DB is not None:
        _TMP_DB.unlink(missing_ok=True)
        return

    async def run():
        engine = create_async_engine(os.environ["DATABASE_URL"])
        try:
            async with engine.begin() as conn:
                await conn.run_sync(db_module.Base.metadata.drop_all)
        finally:
            await engine.dispose()

    asyncio.run(run())


@pytest.fixture(scope="session")
def client():
    """Клиент с поднятым приложением: на старте создаются таблицы и сид."""
    _wipe_database()
    with TestClient(app) as c:
        yield c
    if _TMP_DB is not None:
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

    Идёт через SQLAlchemy, а не через `sqlite3`, чтобы тот же тест работал и на
    PostgreSQL. Подстановки — именованные (`:имя`): позиционные `?` понимает
    только SQLite.
    """
    def _q(sql: str, params: dict | None = None) -> list[tuple]:
        async def run():
            engine = create_async_engine(os.environ["DATABASE_URL"])
            try:
                async with engine.connect() as conn:
                    res = await conn.execute(text(sql), params or {})
                    return [tuple(row) for row in res.fetchall()]
            finally:
                await engine.dispose()

        return asyncio.run(run())
    return _q


def as_json(value):
    """
    Привести значение JSON-колонки к словарю.

    Драйверы отдают её по-разному: PostgreSQL через asyncpg — уже разобранным
    словарём, SQLite — сырым текстом. Тест, написанный под один из них, на
    другом либо падает, либо проходит по случайному совпадению подстроки.
    """
    return json.loads(value) if isinstance(value, (str, bytes)) else value


def login_as(client, login: str, password: str | None = None):
    """Войти в систему и вернуть ответ. По умолчанию пароль равен логину."""
    return client.post("/api/login", json={"login": login,
                                           "password": password if password is not None else login})
