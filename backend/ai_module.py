"""
ИИ-модуль (ЗАГЛУШКА-КАРКАС).

>>> Зона ответственности: Константин <<<
Сейчас — простые правила поверх состояния и действий (демонстрация контракта).
На неделях 3–4 заменяется на: классификацию/локализацию ошибок (scikit-learn),
интерпретируемую обратную связь, адаптивные сценарии и прогноз риска.
Наружу отдаём AIFeedback по контракту (backend/models.py).
"""
from __future__ import annotations
from .models import ParameterState, OperatorAction, AIFeedback, Severity


class ErrorAnalyzer:
    def analyze(self, state: ParameterState, last_action: OperatorAction | None) -> AIFeedback:
        # Правило 1: авария по давлению без реакции
        if "HIGH_PRESSURE" in state.alarms:
            return AIFeedback(
                t=state.t, error_detected=True, error_class="pressure_runaway",
                location="COLUMN_1", severity=Severity.ERROR,
                message="Давление выше уставки. Нужно прикрыть клапан сырья или сбросить нагрузку.",
                recommendation="Уменьшить VALVE_FEED до 40% и проверить насос.",
                risk_score=0.85,
            )
        # Правило 2: риск перелива
        if state.level > 80:
            return AIFeedback(
                t=state.t, error_detected=False, error_class=None,
                location="COLUMN_1", severity=Severity.WARNING,
                message="Уровень в кубе растёт — риск перелива.",
                recommendation="Увеличить отбор или снизить приток.",
                risk_score=0.6,
            )
        # Норма
        return AIFeedback(t=state.t, error_detected=False, severity=Severity.INFO,
                          message="Параметры в норме.", risk_score=0.1)
