"""
Автотесты адаптивного подбора сценария.

Подбор — чистая функция от истории обучаемого и списка сценариев, поэтому
правила проверяются напрямую, без базы и сервера. Отдельно проверяется
эндпойнт: чужая история — это результаты другого человека, и оператору она
закрыта.

Правила проверяются по одному и в столкновении друг с другом: важен не только
каждый случай сам по себе, но и их порядок — «повторить проваленное» должно
брать верх над «идти дальше по сложности».
"""
from __future__ import annotations

import pytest

from backend.ai_module import (REPEAT_THRESHOLD, Recommendation,
                               recommend_scenario)
from conftest import login_as

# Каталог из трёх сценариев — тот же, что наполняет стенд (backend/seed.py).
SCENARIOS = [
    dict(id="startup", difficulty=1, faults=[]),
    dict(id="pump_trip", difficulty=2,
         faults=[{"at": 12, "target": "PUMP_1", "type": "trip"}]),
    dict(id="pressure_alarm", difficulty=3,
         faults=[{"at": 10, "target": "COLUMN_1", "type": "pressure_up"}]),
]


def training(code: str, score: float = 85.0, verdict: str = "passed",
             errors: dict | None = None) -> dict:
    """Одна запись истории в том виде, в каком её отдаёт хранилище."""
    return dict(session_id="s", scenario_code=code, status="finished",
                started_at="2026-08-01T10:00:00+00:00", total_score=score,
                verdict=verdict, errors_by_class=errors or {})


# ------------------------------------------------------------------ правила

def test_first_training_starts_with_the_easiest():
    rec = recommend_scenario([], SCENARIOS)
    assert rec.scenario == "startup"
    assert rec.reason == "first_training"


def test_failed_training_is_repeated():
    """
    Провалил — повторяем то же самое. Иначе обучаемый уходит дальше по
    сложности, так и не отработав то, на чём споткнулся.
    """
    rec = recommend_scenario([training("pump_trip", 45.0, "failed")], SCENARIOS)
    assert rec.scenario == "pump_trip"
    assert rec.reason == "repeat_failed"
    assert "45" in rec.why


def test_repeat_outranks_moving_on():
    """Столкновение правил: последняя не сдана, но непройденные ещё есть."""
    history = [training("pump_trip", 40.0, "failed"), training("startup")]
    assert recommend_scenario(history, SCENARIOS).reason == "repeat_failed"


def test_passed_training_moves_to_the_next_difficulty():
    rec = recommend_scenario([training("startup")], SCENARIOS)
    assert rec.scenario == "pump_trip"
    assert rec.reason == "next_difficulty"


def test_repeated_error_picks_the_scenario_that_trains_it():
    """
    Сдал, но один и тот же промах повторяется — ведём не по возрастанию
    сложности, а к сценарию, который эту ошибку и отрабатывает.
    """
    history = [training("startup", errors={"pressure_runaway": 2}),
               training("startup", errors={"pressure_runaway": 1})]
    rec = recommend_scenario(history, SCENARIOS)
    assert rec.scenario == "pressure_alarm"
    assert rec.reason == "repeated_error"
    assert "Давление" in rec.why, "инструктор должен видеть, какая именно ошибка"


def test_single_error_is_not_treated_as_a_habit():
    """Одна ошибка — случайность; подбор не должен на неё реагировать."""
    history = [training("startup", errors={"pressure_runaway": 1})]
    assert recommend_scenario(history, SCENARIOS).reason == "next_difficulty"


def test_repeat_threshold_is_counted_by_trainings_not_by_episodes():
    """
    Десять эпизодов одной ошибки внутри одной тренировки — это всё ещё один
    неудачный день, а не устойчивый пробел. Считаем тренировки.
    """
    history = [training("startup", errors={"pressure_runaway": 10})]
    assert recommend_scenario(history, SCENARIOS).reason != "repeated_error"
    assert REPEAT_THRESHOLD == 2


def test_error_without_a_matching_scenario_falls_through():
    """
    Класс ошибки, которого не отрабатывает ни один сценарий, не должен
    оставлять подбор без ответа — уходим к обычному правилу.
    """
    history = [training("startup", errors={"missed_step": 1}),
               training("startup", errors={"missed_step": 1})]
    rec = recommend_scenario(history, SCENARIOS)
    assert rec is not None and rec.reason == "next_difficulty"


def test_all_passed_returns_to_the_weakest_result():
    history = [training("pressure_alarm", 95.0),
               training("pump_trip", 72.0),
               training("startup", 88.0)]
    rec = recommend_scenario(history, SCENARIOS)
    assert rec.scenario == "pump_trip"
    assert rec.reason == "weakest_result"


def test_weakest_result_uses_the_best_attempt():
    """
    Сценарий, проваленный когда-то и сданный потом, слабым местом уже не
    считается: берём лучший результат по каждому.
    """
    history = [training("pump_trip", 91.0),
               training("startup", 80.0),
               training("pressure_alarm", 85.0),
               training("pump_trip", 30.0, "failed")]
    assert recommend_scenario(history, SCENARIOS).scenario == "startup"


def test_no_scenarios_no_recommendation():
    assert recommend_scenario([training("startup")], []) is None


def test_recommendation_always_explains_itself():
    """
    Без основания подбор выглядит случайным выбором из списка, и инструктор
    ему не поверит. Пустое `why` недопустимо ни в одной ветке.
    """
    cases = [
        [],
        [training("startup")],
        [training("startup", 40.0, "failed")],
        [training("startup", errors={"pressure_runaway": 1})] * 2,
        [training(s["id"]) for s in SCENARIOS],
    ]
    for history in cases:
        rec = recommend_scenario(history, SCENARIOS)
        assert isinstance(rec, Recommendation)
        assert len(rec.why) > 20, f"нет объяснения: {rec}"
        assert rec.scenario in {s["id"] for s in SCENARIOS}


def test_history_beyond_the_window_is_ignored():
    """
    Ошибка, которую человек делал давно и с тех пор не повторял, тянуть его
    назад не должна — иначе подбор застревает на прошлогоднем промахе.
    """
    old = training("startup", errors={"pressure_runaway": 1})
    recent = [training("pump_trip"), training("pressure_alarm"),
              training("startup"), training("pump_trip"),
              training("pressure_alarm")]
    rec = recommend_scenario(recent + [old, old], SCENARIOS)
    assert rec.reason != "repeated_error"


# ---------------------------------------------------------------- эндпойнт

def test_endpoint_recommends_for_the_caller(client):
    login_as(client, "operator")
    body = client.get("/api/recommendation").json()
    assert body["user_id"] > 0
    assert body["recommendation"]["scenario"] in {"startup", "pump_trip",
                                                  "pressure_alarm"}
    assert body["recommendation"]["why"]


def test_endpoint_requires_a_session(client):
    client.post("/api/logout")
    assert client.get("/api/recommendation").status_code == 401


def test_operator_cannot_ask_about_someone_else(client):
    """Рекомендация выдаёт слабые места человека — это его результаты."""
    instructor_id = login_as(client, "instructor").json()["id"]
    login_as(client, "operator")
    assert client.get(f"/api/recommendation?user_id={instructor_id}").status_code == 403


def test_operator_may_pass_their_own_id(client):
    own = login_as(client, "operator").json()["id"]
    assert client.get(f"/api/recommendation?user_id={own}").status_code == 200


def test_instructor_sees_the_same_recommendation_as_the_trainee(client):
    """
    Инструктор смотрит подбор по обучаемому — и должен видеть ровно то же
    самое, что видит сам обучаемый, иначе разговор о плане тренировок идёт
    о разных вещах.
    """
    own = login_as(client, "operator").json()["id"]
    mine = client.get("/api/recommendation").json()
    login_as(client, "instructor")
    theirs = client.get(f"/api/recommendation?user_id={own}").json()
    assert theirs == mine
    assert theirs["user_id"] == own


def test_refusal_is_written_to_the_audit_log(client, db_query):
    instructor_id = login_as(client, "instructor").json()["id"]
    login_as(client, "operator")
    client.get(f"/api/recommendation?user_id={instructor_id}")
    rows = db_query("SELECT event FROM audit_log ORDER BY id DESC LIMIT 5")
    assert "access_denied" in [r[0] for r in rows]
