"""
Автотесты разбора тренировки.

Проверяется то, ради чего экран разбора и делался:
  • после завершения тренировки есть журнал, телеметрия, ошибки и оценка;
  • оценка считается по эталонной последовательности — пропущенный шаг видно;
  • эталонные шаги в разбор попадают только инструктору и администратору;
  • чужой разбор оператору закрыт;
  • обрыв связи не оставляет тренировку в статусе «идёт» и без оценки.

Такт симуляции в тестах укорочен до 50 мс (см. conftest), поэтому несколько
точек телеметрии набираются мгновенно, а не за реальные секунды.
"""
from __future__ import annotations

import time

import pytest

from conftest import login_as

WAIT_MESSAGES = 400        # предел ожидания сообщения нужного типа, штук


def wait_until(check, timeout: float = 20.0):
    """
    Дождаться условия. Закрытие тренировки при обрыве связи выполняется на
    стороне сервера уже после того, как клиент отпустил канал, поэтому
    проверять его сразу же нельзя — нужно дать обработчику дойти до конца.

    Запас намеренно большой: обычно условие выполняется за десятки
    миллисекунд, и цикл выходит сразу. Предел здесь — страховка от зависания,
    а не норматив: на загруженной машине сборщика прежние 5 секунд изредка
    не выдерживались, и тест падал на ровном месте.
    """
    deadline = time.monotonic() + timeout
    while True:
        value = check()
        if value:
            return value
        if time.monotonic() > deadline:
            raise AssertionError("условие не выполнилось за отведённое время")
        time.sleep(0.02)


def wait_for(ws, kind: str) -> dict:
    """Читать канал до первого сообщения указанного типа."""
    for _ in range(WAIT_MESSAGES):
        msg = ws.receive_json()
        if msg.get("type") == kind:
            return msg
    raise AssertionError(f"не дождались сообщения типа {kind!r}")


def run_training(ws, scenario: str = "startup", actions: list[dict] | None = None,
                 stop: bool = True) -> dict | None:
    """
    Провести тренировку: старт, действия оператора, завершение.
    Возвращает payload итоговой оценки (или None, если завершения не просили).
    """
    ws.send_json({"session_action": "start", "scenario": scenario})
    wait_for(ws, "state")
    for a in actions or []:
        ws.send_json(a)
        wait_for(ws, "action")
    wait_for(ws, "state")                 # хотя бы ещё одна точка телеметрии
    if not stop:
        return None
    ws.send_json({"session_action": "stop"})
    return wait_for(ws, "assessment")["payload"]


#: Полная и верная последовательность пуска — совпадает с эталоном сценария
#: startup в backend/seed.py.
STARTUP_CORRECT = [
    {"action": "set_pump", "target": "PUMP_1", "value": 1},
    {"action": "set_valve", "target": "VALVE_FEED", "value": 70},
    {"action": "start", "target": None, "value": None},
]


@pytest.fixture()
def finished_session(client):
    """Завершённая тренировка оператора, выполненная правильно."""
    login_as(client, "operator")
    with client.websocket_connect("/ws") as ws:
        assessment = run_training(ws, "startup", STARTUP_CORRECT)
    return assessment["session_id"], assessment


# ------------------------------------------------------- состав разбора

def test_debrief_has_all_sections(client, finished_session):
    """Разбор отдаёт всё одним ответом: шапку, журнал, телеметрию, ошибки, оценку."""
    sid, _ = finished_session
    d = client.get(f"/api/sessions/{sid}/debrief").json()

    assert d["session"]["scenario_code"] == "startup"
    assert d["session"]["scenario_name"]                 # название, а не только код
    assert d["session"]["status"] == "finished"
    assert d["session"]["duration_s"] is not None
    assert len(d["journal"]) == len(STARTUP_CORRECT)
    assert d["telemetry"], "без телеметрии график разбора рисовать нечем"
    assert set(d["telemetry"][0]) >= {"t", "pressure", "temperature", "flow", "level", "alarms"}
    assert isinstance(d["errors"], list)
    assert d["assessment"]["verdict"] in ("passed", "not_passed")


def test_debrief_of_unknown_session_gives_404(client):
    login_as(client, "instructor")
    assert client.get("/api/sessions/нет-такой/debrief").status_code == 404


def test_telemetry_endpoint_returns_series(client, finished_session):
    sid, _ = finished_session
    rows = client.get(f"/api/sessions/{sid}/telemetry").json()
    assert rows and [r["t"] for r in rows] == sorted(r["t"] for r in rows)


# --------------------------------------------------------- итоговая оценка

def test_correct_run_scores_full_marks(client, finished_session):
    """Все эталонные шаги вовремя и без ошибок ИИ — 100 баллов и зачёт."""
    _, a = finished_session
    assert a["details"]["steps_done"] == a["details"]["steps_total"] == len(STARTUP_CORRECT)
    assert a["details"]["steps_late"] == 0
    assert a["total_score"] == 100.0
    assert a["verdict"] == "passed"


def test_missed_critical_step_is_visible_and_penalised(client):
    """
    Пропущен критический шаг (насос не включён) — балл ниже, а в разборе
    видно, какой именно шаг не сделан.

    Тренировку проводит инструктор: разбор шагов проверяется от роли, которой
    он открыт сразу, — здесь под проверкой методика оценки, а не доступ к ней.
    """
    login_as(client, "instructor")
    with client.websocket_connect("/ws") as ws:
        a = run_training(ws, "startup", [
            {"action": "set_valve", "target": "VALVE_FEED", "value": 70},
            {"action": "start", "target": None, "value": None},
        ])
    steps = a["details"]["steps"]
    missed = [s for s in steps if not s["done"]]
    assert [s["expected_action"] for s in missed] == ["set_pump"]
    assert missed[0]["critical"] is True
    assert a["total_score"] == 80.0                      # −20 за критический шаг
    assert a["details"]["penalties"]["step_missed_critical"] == 20.0


def test_steps_are_matched_in_order(client):
    """
    Порядок операций важен: пуск процесса до включения насоса не засчитывает
    шаг «включить насос» задним числом.
    """
    login_as(client, "instructor")
    with client.websocket_connect("/ws") as ws:
        a = run_training(ws, "startup", [
            {"action": "start", "target": None, "value": None},
            {"action": "set_pump", "target": "PUMP_1", "value": 1},
            {"action": "set_valve", "target": "VALVE_FEED", "value": 70},
        ])
    done = {s["expected_action"]: s["done"] for s in a["details"]["steps"]}
    # насос и клапан совершены после пуска, поэтому в эталонный порядок не легли
    assert done["set_pump"] is True          # первый шаг нашёл своё действие
    assert done["start"] is False            # а пуск был раньше и уже израсходован
    assert a["total_score"] < 100.0


def test_continuous_alarm_counts_as_one_error(client):
    """
    ИИ оценивает состояние каждый такт, поэтому непрекращающаяся авария даёт
    запись в секунду. В баллах она должна считаться одной ошибкой, иначе оценка
    зависела бы от длительности аварии, а не от промахов оператора.
    """
    login_as(client, "operator")
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"session_action": "start", "scenario": "pressure_alarm"})
        wait_for(ws, "state")
        # оператор делает ровно обратное нужному — открывает клапан вместо того,
        # чтобы прикрыть. Давление уходит за уставку и держится там до конца
        ws.send_json({"action": "set_valve", "target": "VALVE_FEED", "value": 100})
        wait_for(ws, "action")
        for _ in range(60):
            wait_for(ws, "state")
        sid = run_stop(ws)
    d = client.get(f"/api/sessions/{sid}/debrief").json()
    assessment = d["assessment"]

    assert len(d["errors"]) > 1, "авария должна была продержаться несколько тактов"
    assert assessment["details"]["error_records"] == len(d["errors"])
    assert assessment["errors_count"] == len(d["error_episodes"])
    assert assessment["errors_count"] < len(d["errors"]), \
        "подряд идущие записи одного класса должны схлопнуться в один эпизод"

    episode = d["error_episodes"][0]
    assert episode["count"] > 1 and episode["t_to"] > episode["t_from"]


def test_assessment_counts_everything_that_was_written(client):
    """
    Оценка обязана учесть всё, что записано за тренировку.

    Такт симуляции и завершение работают в одном цикле событий: такт,
    начавшийся до нажатия «Завершить», успевал дописать ошибку уже после того,
    как оценка сформирована, и она эту ошибку не учитывала. Расхождение было
    ровно на одну запись и всплывало примерно раз на десять прогонов.

    Окно узкое, поэтому повторяем: с одного раза в него можно не попасть.
    """
    login_as(client, "operator")
    for _ in range(3):
        with client.websocket_connect("/ws") as ws:
            ws.send_json({"session_action": "start", "scenario": "pressure_alarm"})
            wait_for(ws, "state")
            ws.send_json({"action": "set_valve", "target": "VALVE_FEED", "value": 100})
            wait_for(ws, "action")
            for _ in range(20):
                wait_for(ws, "state")
            sid = run_stop(ws)

        d = client.get(f"/api/sessions/{sid}/debrief").json()
        details = d["assessment"]["details"]
        assert details["error_records"] == len(d["errors"]), \
            "оценка посчитана раньше, чем такт дописал свою ошибку"
        assert d["assessment"]["errors_count"] == len(d["error_episodes"])


def test_assessment_counts_reaction_time(client, finished_session):
    """Время реакции попадает в оценку — это показатель квалификации."""
    _, a = finished_session
    assert a["avg_reaction_ms"] is not None and a["avg_reaction_ms"] >= 0


# ------------------------------------------------- разграничение доступа

def test_operator_does_not_get_reference_in_debrief(client, finished_session):
    """Эталон — «ответы» к сценарию: в разборе оператора его быть не должно."""
    sid, _ = finished_session
    d = client.get(f"/api/sessions/{sid}/debrief").json()
    assert "reference" not in d


def test_instructor_gets_reference_in_debrief(client, finished_session):
    sid, _ = finished_session
    client.cookies.clear()
    login_as(client, "instructor")
    d = client.get(f"/api/sessions/{sid}/debrief").json()
    assert d["reference"] and d["reference"][0]["expected_action"] == "set_pump"


def test_trainee_does_not_see_reference_steps_until_released(client, finished_session):
    """
    Лазейка, которую закрывает открытие разбора: начать тренировку, сразу
    завершить её и прочитать в собственной оценке правильную последовательность.
    До открытия обучаемый видит балл и сводку, но не сами шаги.
    """
    sid, _ = finished_session
    d = client.get(f"/api/sessions/{sid}/debrief").json()
    details = d["assessment"]["details"]
    assert "steps" not in details
    assert details["steps_total"] and details["steps_done"] is not None   # сводка осталась
    # тот же разрыв закрыт и в отдельной оценке
    assert "steps" not in client.get(f"/api/sessions/{sid}/assessment").json()["details"]


def test_instructor_sees_steps_without_release(client, finished_session):
    """Инструктору разбор доступен сразу — открывать самому себе нечего."""
    sid, _ = finished_session
    client.cookies.clear()
    login_as(client, "instructor")
    steps = client.get(f"/api/sessions/{sid}/debrief").json()["assessment"]["details"]["steps"]
    assert steps and steps[0]["description"]


def test_released_debrief_becomes_visible_to_trainee(client, finished_session):
    """После открытия инструктором обучаемый видит свои шаги — это и есть разбор."""
    sid, _ = finished_session
    client.cookies.clear()
    login_as(client, "instructor")
    assert client.post(f"/api/sessions/{sid}/release").status_code == 200

    client.cookies.clear()
    login_as(client, "operator")
    details = client.get(f"/api/sessions/{sid}/debrief").json()["assessment"]["details"]
    assert details["released"] is True
    assert details["steps"] and details["steps"][0]["description"]


def test_operator_cannot_release_debrief(client, finished_session):
    """Открыть разбор самому себе обучаемый не может — иначе смысла в защите нет."""
    sid, _ = finished_session
    assert client.post(f"/api/sessions/{sid}/release").status_code == 403
    assert "steps" not in client.get(f"/api/sessions/{sid}/debrief").json()["assessment"]["details"]


def test_release_of_unknown_session_gives_404(client):
    login_as(client, "instructor")
    assert client.post("/api/sessions/нет-такой/release").status_code == 404


def test_release_without_assessment_gives_409(client):
    """Нечего открывать, пока тренировка не завершена и оценки нет."""
    login_as(client, "instructor")
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"session_action": "start", "scenario": "startup"})
        wait_for(ws, "state")
        sid = client.get("/api/sessions").json()[0]["id"]
        assert client.post(f"/api/sessions/{sid}/release").status_code == 409


def test_ws_assessment_hides_steps_from_trainee(client):
    """
    Оценка приходит и по WebSocket сразу после «Завершить» — эталонные шаги
    должны быть закрыты и там, иначе защита обходится не открывая разбор.
    """
    login_as(client, "operator")
    with client.websocket_connect("/ws") as ws:
        payload = run_training(ws, "startup", STARTUP_CORRECT)
    assert "steps" not in payload["details"]
    assert payload["total_score"] == 100.0        # сама оценка при этом полная


def test_operator_cannot_open_foreign_debrief(client):
    """Чужой разбор и чужая телеметрия оператору закрыты."""
    login_as(client, "instructor")
    with client.websocket_connect("/ws") as ws:
        run_training(ws, "startup", STARTUP_CORRECT)
    foreign = client.get("/api/sessions").json()[0]["id"]

    client.cookies.clear()
    login_as(client, "operator")
    assert client.get(f"/api/sessions/{foreign}/debrief").status_code == 403
    assert client.get(f"/api/sessions/{foreign}/telemetry").status_code == 403


@pytest.mark.parametrize("path", ["debrief", "telemetry"])
def test_debrief_closed_without_login(client, path):
    assert client.get(f"/api/sessions/любая/{path}").status_code == 401


def test_debrief_page_requires_login(client):
    r = client.get("/debrief", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"

    login_as(client, "operator")
    assert client.get("/debrief").status_code == 200


# ------------------------------------------------------------ устойчивость

def test_broken_connection_does_not_leave_session_active(client):
    """
    Обрыв канала без нажатия «Завершить» раньше оставлял тренировку в статусе
    active навсегда и без оценки — разбирать было нечего.
    """
    login_as(client, "operator")
    with client.websocket_connect("/ws") as ws:
        run_training(ws, "startup", STARTUP_CORRECT, stop=False)
    sid = client.get("/api/sessions").json()[0]["id"]

    def closed():
        d = client.get(f"/api/sessions/{sid}/debrief").json()
        return d if d["session"]["status"] != "active" else None

    d = wait_until(closed)
    assert d["session"]["status"] == "aborted"
    assert d["session"]["ended_at"] is not None
    assert d["assessment"] is not None, "оценка должна сформироваться и по прерванной тренировке"


def test_stop_creates_exactly_one_assessment(client, finished_session):
    """
    Завершение приходит и по кнопке, и при обрыве канала. Оценка должна
    остаться одна, иначе в разборе показывался бы дубль.
    """
    sid, a = finished_session
    same = client.get(f"/api/sessions/{sid}/assessment").json()
    assert same["total_score"] == a["total_score"]
    assert same["details"]["steps_done"] == a["details"]["steps_done"]


def test_repeated_stop_is_safe(client):
    """Повторное «Завершить» не создаёт вторую тренировку и не роняет канал."""
    login_as(client, "operator")
    with client.websocket_connect("/ws") as ws:
        run_training(ws, "startup", STARTUP_CORRECT)
        ws.send_json({"session_action": "stop"})
        ws.send_json({"action": "ack_alarm", "target": None, "value": None})
        assert wait_for(ws, "action")["payload"]["action"] == "ack_alarm"


def test_reset_starts_a_separate_training(client):
    """
    Сброс — новая попытка. Раньше действия после сброса дописывались в ту же
    запись, и журнал разбора смешивал два прогона.
    """
    login_as(client, "operator")
    before = len(client.get("/api/sessions").json())
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"session_action": "start", "scenario": "startup"})
        wait_for(ws, "state")
        ws.send_json({"action": "set_pump", "target": "PUMP_1", "value": 1})
        wait_for(ws, "action")

        ws.send_json({"session_action": "reset"})
        wait_for(ws, "state")
        ws.send_json({"action": "ack_alarm", "target": None, "value": None})
        wait_for(ws, "action")
        second = run_stop(ws)

    sessions = client.get("/api/sessions").json()
    assert len(sessions) == before + 2, "сброс должен заводить отдельную тренировку"
    journal = client.get(f"/api/sessions/{second}/journal").json()
    assert [a["action"] for a in journal] == ["ack_alarm"]


def run_stop(ws) -> str:
    """Завершить тренировку и вернуть её идентификатор."""
    ws.send_json({"session_action": "stop"})
    return wait_for(ws, "assessment")["payload"]["session_id"]
