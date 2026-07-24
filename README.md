# КТК ЭЛОУ-АВТ — компьютерный тренажёрный комплекс

Тренажёр рабочего места оператора установки ЭЛОУ-АВТ с цифровым двойником
и ИИ-модулем. Решение кейса IT-чемпионата нефтяной отрасли 2026.

> Статус: **неделя 2**. Сессии тренировки, сценарии с отказами по времени,
> хранение в БД. ИИ и матмодель — заглушки с чётким контрактом.

## Запуск через Docker (рекомендуется — работает у всех одинаково)

Нужен только установленный **Docker Desktop**. Одна команда:

```bash
docker compose up --build
```

Затем открыть в браузере **http://localhost:8000**.

Поднимаются два контейнера: приложение и PostgreSQL. База стартует первой,
приложение ждёт её готовности (healthcheck) и само создаёт таблицы. Данные БД
сохраняются в volume `pgdata` между перезапусками. Остановить: `Ctrl+C`,
удалить с базой начисто: `docker compose down -v`.

## Запуск без Docker (локально, SQLite)

```bash
./run.sh            # venv + зависимости + сервер, БД = SQLite
# открыть http://localhost:8000
```

Вручную:
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn backend.main:app --reload
```

БД выбирается переменной `DATABASE_URL` (по умолчанию SQLite):
`postgresql+asyncpg://ktk:ktk@localhost:5432/ktk` — Postgres,
`sqlite+aiosqlite:///./ktk.db` — локальный файл.

## Структура

```
backend/
  models.py     ← ЕДИНЫЙ КОНТРАКТ данных (менять только здесь, по согласованию)
  engine.py     ← матмодель / цифровой двойник + отказы по времени  [Светлана]
  ai_module.py  ← анализ ошибок оператора                            [Константин]
  scenarios.py  ← каталог учебных сценариев
  session.py    ← сессия тренировки: старт/стоп/сброс                [Антон]
  db.py         ← подключение к БД, модели таблиц (SQLAlchemy async)  [Антон]
  storage.py    ← запись/чтение сессий, журнала, телеметрии          [Антон]
  main.py       ← FastAPI + WebSocket real-time                      [Антон]
frontend/
  index.html    ← интерфейс оператора: мнемосхема, показания, сценарии, журнал [Михаил]
docs/
  api-contract.md   ← описание контракта и зон ответственности
docker-compose.yml  ← стек приложение + PostgreSQL
Dockerfile          ← образ приложения
requirements.txt    ← зависимости Python
run.sh              ← локальный запуск без Docker (SQLite)
```

## Как модули стыкуются

- Движок (`engine.step()`) → отдаёт `ParameterState` → уходит в UI и в ИИ.
- ИИ (`ErrorAnalyzer.analyze()`) → отдаёт `AIFeedback` → уходит в UI.
- UI → шлёт `ControlCommand` → в движок; действия пишутся в журнал (`OperatorAction`).
- Сессия (`TrainingSession`) → загружает сценарий, применяет отказы по времени,
  пишет журнал и телеметрию в БД через `storage.py`.

Все схемы — в `backend/models.py`. Пока внешний контракт не меняется,
каждый дорабатывает свой модуль независимо.

## REST и WebSocket API

- `GET /` — интерфейс оператора.
- `GET /api/scenarios` — список учебных сценариев.
- `GET /api/sessions` — список сессий тренировок.
- `GET /api/sessions/{id}/journal` — журнал действий сессии из БД.
- `WS /ws` — канал тренировки в реальном времени. От UI: `{"session_action":"start","scenario":"pump_trip"}`,
  `{"session_action":"reset"}`, `{"session_action":"stop"}` и команды оператора
  `{"action":"set_valve","value":40}`. От сервера: сообщения `state` / `feedback` / `action`.

## Кто за что

| Модуль | Ответственный | Дальнейшие задачи |
|---|---|---|
| engine.py | Светлана | реальные модели ректификации/теплообмена/гидравлики |
| ai_module.py | Константин | классификация/локализация ошибок, риск, адаптивные сценарии |
| frontend | Михаил | полноценная мнемосхема ЭЛОУ-АВТ, роли оператор/инструктор |
| backend (session/db/storage/main) | Антон | сессии, хранение (PostgreSQL), сценарии |
| требования/эк/ИБ | Элтон | БТ/ФТТ/НФТ, экономрасчёт, модель угроз |

## Стек (отечественность обосновывается в проекте)

Python (FastAPI, NumPy/SciPy) · WebSocket real-time · веб-UI (SVG-мнемосхема) ·
целевые: PostgreSQL / Postgres Pro, ОС Astra Linux / РЕД ОС, Docker.
