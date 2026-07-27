"""
КТК ЭЛОУ-АВТ — backend.
FastAPI + WebSocket: сессии тренировки, исполнение сценариев с отказами по
времени, хранение журнала и телеметрии в БД (PostgreSQL / SQLite-fallback).

Доступ к системе — по логину и паролю, с разграничением по ролям
(operator / instructor / admin), см. backend/auth.py.

Запуск:  uvicorn backend.main:app --reload
Открыть: http://localhost:8000
"""
from __future__ import annotations
import asyncio
import json
import os
import time
from pathlib import Path

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect, status
from fastapi.responses import FileResponse, RedirectResponse

from .models import ControlCommand, OperatorAction, WSMessage, WSMessageType
from .session import TrainingSession
from .scenarios import SCENARIOS
from . import auth, db, storage, seed
from .auth import current_user, optional_user, require_role


#: Задачи, дописывающие данные после обрыва соединения (см. `_run_detached`).
#: Ссылки нужно держать: цикл событий хранит на задачи только слабые ссылки,
#: и без этого сборщик мусора может убрать задачу до того, как она отработает.
_background: set[asyncio.Task] = set()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """При старте: создать таблицы и наполнить начальными данными."""
    await db.init_db()
    await seed.seed()
    yield
    # при остановке даём фоновым задачам дописать закрытие тренировок
    if _background:
        await asyncio.wait(set(_background), timeout=5.0)


app = FastAPI(title="КТК ЭЛОУ-АВТ", version="0.4.0", lifespan=lifespan)
app.include_router(auth.router)
FRONTEND = Path(__file__).resolve().parent.parent / "frontend"

#: Роли, которым доступны чужие тренировки и эталонные шаги.
SUPERVISOR_ROLES = ("instructor", "admin")


# ------------------------------------------------------------------ сценарии

@app.get("/api/scenarios")
async def list_scenarios(user: dict = Depends(current_user)):
    """Сценарии из БД (инструктор может добавлять свои)."""
    rows = await storage.list_scenarios()
    return rows if rows else [s.model_dump() for s in SCENARIOS]


@app.get("/api/scenarios/{code}/reference")
async def reference_steps(code: str, user: dict = Depends(require_role(*SUPERVISOR_ROLES))):
    """
    Эталонная последовательность шагов — основа для сравнения действий (ИИ).
    Оператору закрыто: это «ответы» к сценарию, их показывают только на разборе.
    """
    return await storage.get_reference_steps(code)


# ------------------------------------------------------------------- сессии

async def _ensure_session_access(session_id: str, user: dict, request: Request) -> dict:
    """
    Разрешить доступ к тренировке: инструктору и администратору — к любой,
    оператору — только к своей. Отказ фиксируется в журнале аудита.
    """
    owner = await storage.get_session_owner(session_id)
    if owner is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Тренировка не найдена")
    if user["role"] not in SUPERVISOR_ROLES and owner["user_id"] != user["id"]:
        await storage.audit(user["id"], "access_denied",
                            {"path": request.url.path, "session": session_id,
                             "reason": "чужая тренировка"},
                            ip=auth.client_ip(request))
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="Недостаточно прав")
    return owner


@app.get("/api/sessions")
async def sessions(user: dict = Depends(current_user)):
    """Инструктор и администратор видят все тренировки, оператор — только свои."""
    own_only = None if user["role"] in SUPERVISOR_ROLES else user["id"]
    return await storage.list_sessions(user_id=own_only)


@app.get("/api/sessions/{session_id}/journal")
async def journal(session_id: str, request: Request, user: dict = Depends(current_user)):
    await _ensure_session_access(session_id, user, request)
    return await storage.get_journal(session_id)


@app.get("/api/sessions/{session_id}/errors")
async def session_errors(session_id: str, request: Request,
                         user: dict = Depends(current_user)):
    """Ошибки, выявленные ИИ в ходе тренировки."""
    await _ensure_session_access(session_id, user, request)
    return await storage.get_errors(session_id)


@app.get("/api/sessions/{session_id}/assessment")
async def session_assessment(session_id: str, request: Request,
                             user: dict = Depends(current_user)):
    """Оценка квалификации по итогам тренировки (создаётся при завершении)."""
    await _ensure_session_access(session_id, user, request)
    return await storage.get_assessment(session_id) or {"detail": "оценка ещё не сформирована"}


@app.get("/api/sessions/{session_id}/telemetry")
async def session_telemetry(session_id: str, request: Request,
                            user: dict = Depends(current_user)):
    """История параметров установки — данные для графика на разборе."""
    await _ensure_session_access(session_id, user, request)
    return await storage.get_telemetry(session_id)


@app.get("/api/sessions/{session_id}/debrief")
async def session_debrief(session_id: str, request: Request,
                          user: dict = Depends(current_user)):
    """
    Разбор тренировки одним ответом: шапка, журнал, телеметрия, ошибки, оценка.

    Эталонные шаги добавляются только инструктору и администратору — оператору
    они закрыты и здесь, иначе разбор стал бы обходом `/reference`.
    """
    await _ensure_session_access(session_id, user, request)
    data = await storage.get_debrief(session_id)
    if data is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Тренировка не найдена")
    if user["role"] in SUPERVISOR_ROLES:
        data["reference"] = await storage.get_reference_steps(data["session"]["scenario_code"])
    return data


# ------------------------------------------------------------------ интерфейс

@app.get("/login")
def login_page():
    return FileResponse(FRONTEND / "login.html")


@app.get("/")
async def index(user: dict | None = Depends(optional_user)):
    """Рабочее место оператора. Без входа — переадресация на страницу входа."""
    if user is None:
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    return FileResponse(FRONTEND / "index.html")


@app.get("/debrief")
async def debrief_page(user: dict | None = Depends(optional_user)):
    """Экран разбора тренировки. Данные тянет из `/api/sessions/{id}/debrief`."""
    if user is None:
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    return FileResponse(FRONTEND / "debrief.html")


# ----------------------------------------------------------------- WebSocket

#: Код закрытия WS при отсутствии действующего сеанса (свободный диапазон 4000+).
WS_UNAUTHORIZED = 4401

#: Период выдачи состояния установки, с. Один такт — это всегда одна секунда
#: модельного времени (`dt=1.0` в `session.tick()`), поэтому значение меньше
#: единицы ускоряет ход тренировки относительно реального времени. В штатной
#: работе — 1.0; уменьшается в автотестах и в замерах задержек, чтобы не ждать
#: реальные минуты. Переопределяется переменной `KTK_TICK_PERIOD_S`.
TICK_PERIOD_S = float(os.getenv("KTK_TICK_PERIOD_S", "1.0"))


def _run_detached(coro, what: str) -> None:
    """
    Выполнить работу вне области отмены обработчика соединения.

    Когда клиент отключается, сервер отменяет задачу соединения, и любой
    `await` в блоке `finally` немедленно получает `CancelledError` — дописать
    что-либо в БД оттуда нельзя. Поэтому закрытие тренировки выносится в
    отдельную задачу цикла событий: она не входит в отменённую область и
    доводит запись до конца.
    """
    async def runner():
        try:
            await coro
        except Exception as e:                                  # noqa: BLE001
            print(f"[ws] {what}: {e!r}")

    task = asyncio.create_task(runner())
    _background.add(task)
    task.add_done_callback(_background.discard)


@app.websocket("/ws")
async def ws(websocket: WebSocket):
    """
    Канал тренировки. Требует действующего сеанса: cookie приходит вместе
    с рукопожатием WebSocket.

    UI -> сервер:
      {"session_action":"start","scenario":"pump_trip"}  — начать сценарий
      {"session_action":"reset"}                          — перезапустить
      {"session_action":"stop"}                           — завершить
      {"action":"set_valve","value":40, ...}              — команда оператора
    сервер -> UI: WSMessage(state|feedback|action), по завершении — assessment
    """
    user = await auth.ws_user(websocket)
    await websocket.accept()
    if user is None:
        await websocket.send_text(json.dumps(
            {"type": "error", "payload": {"detail": "Требуется вход в систему"}},
            ensure_ascii=False))
        await websocket.close(code=WS_UNAUTHORIZED)
        return

    sess = TrainingSession(operator=user["full_name"])
    ip = auth.client_ip(websocket)          # для журнала аудита
    last_event_t = time.time()
    ticker_task: asyncio.Task | None = None

    async def finish(status_: str) -> dict | None:
        """
        Закрыть текущую тренировку и сформировать оценку — ровно один раз.

        Вызывается из четырёх мест: кнопка «Завершить», сброс, старт следующего
        сценария и обрыв канала. `finalize_session` завершает запись, только
        если та ещё активна, поэтому повторный вызов ничего не портит и второй
        оценки не создаёт.
        """
        sid, sess.session_id = sess.session_id, None
        if not sid:
            return None
        if not await storage.finalize_session(sid, status_, user_id=user["id"], ip=ip):
            return None
        return await storage.build_assessment(sid)

    async def ticker():
        """
        Отдаёт состояние установки раз в TICK_PERIOD_S.

        Момент следующего такта отсчитывается от расписания, а не от конца
        обработки: иначе к периоду каждый раз прибавлялись бы шаг симуляции и
        запись в БД, и за длинную тренировку время на мнемосхеме заметно
        разошлось бы с реальным.
        """
        next_at = time.monotonic()
        while True:
            next_at += TICK_PERIOD_S
            if sess.active:
                state, feedback, events = sess.tick()
                # идентификатор запоминаем один раз на такт: пока идут отправка
                # и запись, оператор может нажать «Завершить», и sess.session_id
                # обнулится прямо посреди такта
                sid = sess.session_id
                await websocket.send_text(WSMessage(
                    type=WSMessageType.STATE, payload=state.model_dump()).model_dump_json())
                await websocket.send_text(WSMessage(
                    type=WSMessageType.FEEDBACK, payload=feedback.model_dump()).model_dump_json())
                if sid:
                    await storage.save_telemetry(sid, state)
                    await storage.save_error(sid, feedback)
            delay = next_at - time.monotonic()
            if delay < 0:
                # такт не уложился в период — не копим долг, начинаем отсчёт заново
                next_at = time.monotonic()
                delay = 0.0
            await asyncio.sleep(delay)

    async def ticker_guarded():
        """
        Обёртка над ticker: отмена и обрыв канала — штатные, остальное печатаем.
        Без неё упавшая задача умирала молча: телеметрия прекращалась, а в
        интерфейсе это выглядело как «связь есть, но параметры застыли».
        """
        try:
            await ticker()
        except (asyncio.CancelledError, WebSocketDisconnect, RuntimeError):
            pass
        except Exception as e:                                  # noqa: BLE001
            print(f"[ws] такт симуляции прерван: {e!r}")

    ticker_task = asyncio.create_task(ticker_guarded())
    try:
        while True:
            raw = await websocket.receive_text()
            msg = json.loads(raw)

            # --- управление сессией ---
            if "session_action" in msg:
                sa = msg["session_action"]
                if sa == "start":
                    # предыдущая тренировка закрывается, иначе она навсегда
                    # осталась бы в статусе active и без оценки
                    await finish("aborted")
                    code = msg.get("scenario", "startup")
                    sc = await storage.get_scenario(code)
                    ok = sess.start(code, sc["initial"], sc["faults"]) if sc else sess.start(code)
                    if ok:
                        sess.session_id = await storage.create_session(
                            sess.scenario_id, sess.operator, user_id=user["id"], ip=ip)
                        last_event_t = time.time()
                elif sa == "reset":
                    # сброс — это новая попытка: старую закрываем и заводим
                    # отдельную запись, иначе в журнале смешались бы два прогона
                    await finish("aborted")
                    sess.reset()
                    if sess.scenario_id:
                        sess.session_id = await storage.create_session(
                            sess.scenario_id, sess.operator, user_id=user["id"], ip=ip)
                    last_event_t = time.time()
                elif sa == "stop":
                    sess.stop()
                    assessment = await finish("finished")
                    if assessment:
                        await websocket.send_text(json.dumps(
                            {"type": "assessment", "payload": assessment}, ensure_ascii=False))
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
        # обрыв связи не должен оставлять тренировку висеть в статусе active:
        # закрываем её и формируем оценку по тому, что успели записать.
        # Отдельной задачей — здесь мы уже в отменённой области (см. _run_detached).
        sess.stop()
        _run_detached(finish("aborted"), "не удалось закрыть прерванную тренировку")
