"""
Автотесты ИИ-модуля: классификация, локализация, объяснение, прогноз риска.

Модуль работает без сети и без БД, поэтому проверяется напрямую — это
единственный набор в проекте, которому не нужен поднятый сервер.

Проверяется то, ради чего модуль и делался:
  • ошибка не просто обнаружена, а отнесена к классу и локализована;
  • объяснение содержит последствие и ссылку на регламент — иначе обучаемому
    нечего понять из строки «pressure_runaway»;
  • ошибочные действия оператора видны в контексте: одно и то же действие
    бывает верным и грубой ошибкой;
  • прогноз срабатывает ДО аварии, а не после.
"""
from __future__ import annotations

import pytest

from backend.ai_module import CATALOGUE, FORECAST_HORIZON_S, LIMITS, ErrorAnalyzer
from backend.models import (ActionType, EquipmentState, OperatorAction,
                            ParameterState, Severity)


def state(t=1.0, pressure=120.0, temperature=340.0, flow=90.0, level=50.0,
          running=True, alarms=None, valve=60.0, pump=True) -> ParameterState:
    return ParameterState(
        t=t, pressure=pressure, temperature=temperature, flow=flow, level=level,
        running=running, alarms=alarms or [],
        equipment=[
            EquipmentState(id="PUMP_1", kind="pump", on=pump),
            EquipmentState(id="VALVE_FEED", kind="valve", position=valve),
            EquipmentState(id="COLUMN_1", kind="column"),
        ])


def action(kind: ActionType, target=None, value=None, t=1.0) -> OperatorAction:
    return OperatorAction(t=t, action=kind, target=target, value=value)


def feed(ai: ErrorAnalyzer, states):
    """Прогнать несколько снимков подряд и вернуть последний ответ."""
    result = None
    for s in states:
        result = ai.analyze(s)
    return result


# --------------------------------------------------- справочник и объяснения

def test_every_error_class_explains_consequence_and_cites_regulation():
    """
    Требование критерия — интерпретируемая обратная связь. Класс без
    объяснения последствия и ссылки на регламент обучаемому бесполезен.
    """
    for code, cls in CATALOGUE.items():
        assert cls.code == code
        assert len(cls.why) > 40, f"{code}: нет объяснения последствия"
        assert "регламент" in cls.reference, f"{code}: нет ссылки на регламент"
        assert cls.recommendation, f"{code}: нет рекомендации"


def test_normal_state_is_not_an_error():
    fb = ErrorAnalyzer().analyze(state())
    assert fb.error_detected is False
    assert fb.severity == Severity.INFO
    assert fb.risk_score is not None and fb.risk_score < 0.2


# --------------------------------------------- классификация по состоянию

def test_pressure_over_limit_is_classified_and_located():
    fb = ErrorAnalyzer().analyze(state(pressure=LIMITS["pressure_max"] + 15))
    assert fb.error_detected and fb.error_class == "pressure_runaway"
    assert fb.location == "COLUMN_1"
    assert fb.severity == Severity.ERROR
    assert fb.risk_score == 1.0
    assert fb.reference and fb.recommendation


def test_low_level_cites_the_furnace_pump_consequence():
    """
    Падение уровня — не абстрактная «ошибка уровня»: регламент прямо связывает
    её со срывом печных насосов и прогаром змеевика.
    """
    fb = ErrorAnalyzer().analyze(state(level=LIMITS["level_min"] - 5))
    assert fb.error_class == "low_level_drain"
    assert "насос" in fb.message and "змеевик" in fb.message
    assert "7.7.1.14" in fb.reference


def test_low_level_is_not_an_error_on_stopped_plant():
    """На остановленной установке пустой куб — норма, а не авария."""
    fb = ErrorAnalyzer().analyze(state(level=5.0, running=False))
    assert fb.error_class != "low_level_drain"


@pytest.mark.parametrize("kwargs,expected", [
    (dict(temperature=LIMITS["temperature_max"] + 10), "temp_runaway"),
    (dict(level=LIMITS["level_max"] + 5), "high_level"),
])
def test_other_limits_are_classified(kwargs, expected):
    assert ErrorAnalyzer().analyze(state(**kwargs)).error_class == expected


# ------------------------------------------- ошибочные действия оператора

def test_opening_valve_under_pressure_alarm_is_an_error():
    """
    То, чего не видно по одним показаниям приборов: оператор увеличил подачу
    там, где нагрузку надо сбрасывать.
    """
    ai = ErrorAnalyzer()
    ai.observe_action(action(ActionType.SET_VALVE, "VALVE_FEED", 100))
    fb = ai.analyze(state(pressure=190, alarms=["HIGH_PRESSURE"], valve=60))
    assert fb.error_class == "load_increase_under_alarm"
    assert fb.location == "VALVE_FEED"


def test_load_increase_is_caught_while_pressure_is_still_rising():
    """
    Замечание должно приходить в момент ошибки, а не через семь секунд, когда
    загорится авария: к тому времени обучаемый уже не свяжет его со своим
    действием. Давление здесь ещё под уставкой, но уверенно идёт к ней.
    """
    ai = ErrorAnalyzer()
    feed(ai, [state(t=t, pressure=120.0 + 4 * t, valve=60) for t in range(1, 9)])
    ai.observe_action(action(ActionType.SET_VALVE, "VALVE_FEED", 100, t=9))
    fb = ai.analyze(state(t=9, pressure=156.0, valve=60))

    assert fb.error_class == "load_increase_under_alarm"
    assert fb.t == 9.0, "замечание должно быть на том же такте, что и действие"


def test_closing_valve_under_the_same_alarm_is_not_an_error():
    """Обратное действие в той же обстановке — верное. Контекст решает."""
    ai = ErrorAnalyzer()
    ai.observe_action(action(ActionType.SET_VALVE, "VALVE_FEED", 30))
    fb = ai.analyze(state(pressure=190, alarms=["HIGH_PRESSURE"], valve=60))
    assert fb.error_class != "load_increase_under_alarm"


def test_opening_valve_in_normal_regime_is_not_an_error():
    """При выводе на режим открыть клапан — штатная операция."""
    ai = ErrorAnalyzer()
    ai.observe_action(action(ActionType.SET_VALVE, "VALVE_FEED", 80))
    fb = ai.analyze(state(pressure=120, valve=60))
    assert fb.error_detected is False


def test_stopping_pump_on_running_plant_is_an_error():
    ai = ErrorAnalyzer()
    ai.observe_action(action(ActionType.SET_PUMP, "PUMP_1", 0))
    fb = ai.analyze(state(running=True))
    assert fb.error_class == "pump_off_under_load"
    assert fb.location == "PUMP_1"


def test_stopping_pump_on_stopped_plant_is_not_an_error():
    ai = ErrorAnalyzer()
    ai.observe_action(action(ActionType.SET_PUMP, "PUMP_1", 0))
    assert ai.analyze(state(running=False)).error_detected is False


def test_acknowledging_without_fixing_is_reported():
    """
    Квитирование убирает сигнал, а не причину. Если через несколько тактов
    режим всё ещё нарушен — это отдельное замечание.
    """
    ai = ErrorAnalyzer()
    ai.analyze(state(t=1, pressure=190, alarms=["HIGH_PRESSURE"]))
    ai.observe_action(action(ActionType.ACK_ALARM, t=2))
    ai.analyze(state(t=2, pressure=190, alarms=["HIGH_PRESSURE"]))
    # давление держится у уставки, авария квитирована
    fb = feed(ai, [state(t=t, pressure=178, alarms=[]) for t in range(3, 12)])
    assert fb.error_class == "ack_without_fix"
    assert fb.severity == Severity.WARNING


# ------------------------------------------------------------ прогноз риска

def test_forecast_warns_before_the_alarm_fires():
    """
    Главное отличие от простой сигнализации: предупреждение приходит, пока
    параметр ещё в норме.
    """
    ai = ErrorAnalyzer()
    # давление растёт на 4 кПа в секунду, уставка 180
    fb = feed(ai, [state(t=t, pressure=120.0 + 4 * t) for t in range(1, 10)])
    assert fb.error_detected is False, "авария ещё не наступила — это не ошибка"
    assert fb.severity == Severity.WARNING
    assert fb.predicted_alarm_s is not None
    assert 0 < fb.predicted_alarm_s <= FORECAST_HORIZON_S
    assert "уставку" in fb.message


def test_forecast_estimates_time_reasonably():
    """Скорость 4 кПа/с, до уставки ~24 кПа — ожидаем около шести секунд."""
    ai = ErrorAnalyzer()
    fb = feed(ai, [state(t=t, pressure=120.0 + 4 * t) for t in range(1, 10)])
    assert 4.0 <= fb.predicted_alarm_s <= 8.0


def test_risk_grows_as_the_alarm_approaches():
    slow = feed(ErrorAnalyzer(),
                [state(t=t, pressure=100.0 + 1.0 * t) for t in range(1, 10)])
    fast = feed(ErrorAnalyzer(),
                [state(t=t, pressure=100.0 + 6.0 * t) for t in range(1, 10)])
    assert fast.risk_score > slow.risk_score


def test_stable_parameters_produce_no_forecast():
    ai = ErrorAnalyzer()
    fb = feed(ai, [state(t=t, pressure=120.0) for t in range(1, 12)])
    assert fb.predicted_alarm_s is None
    assert fb.error_detected is False


def test_falling_level_is_forecast_too():
    """Прогноз работает и на убывание — уровень падает к нижней границе."""
    ai = ErrorAnalyzer()
    fb = feed(ai, [state(t=t, level=60.0 - 2.0 * t) for t in range(1, 12)])
    assert fb.predicted_alarm_s is not None or fb.error_class == "low_level_drain"


def test_trend_needs_more_than_two_points():
    """
    Скорость считается по окну, а не по двум соседним снимкам: в показаниях
    есть пульсация, и по двум точкам знак тренда скакал бы каждый такт.
    """
    ai = ErrorAnalyzer()
    ai.analyze(state(t=1, pressure=120))
    fb = ai.analyze(state(t=2, pressure=170))
    assert fb.predicted_alarm_s is None


# ------------------------------------------------------------- приоритеты

def test_actual_breach_outranks_forecast():
    """Случившаяся авария важнее прогноза — показываем её."""
    ai = ErrorAnalyzer()
    fb = feed(ai, [state(t=t, pressure=150.0 + 8 * t) for t in range(1, 8)])
    assert fb.error_class == "pressure_runaway"


def test_only_one_message_at_a_time():
    """
    Три предупреждения разом — верный способ, чтобы оператор не прочитал ни
    одного. Ответ всегда один, самый важный.
    """
    ai = ErrorAnalyzer()
    ai.observe_action(action(ActionType.SET_PUMP, "PUMP_1", 0))
    fb = ai.analyze(state(pressure=200, temperature=450, level=95,
                          alarms=["HIGH_PRESSURE", "HIGH_TEMP", "HIGH_LEVEL"]))
    assert isinstance(fb.message, str) and fb.error_class
