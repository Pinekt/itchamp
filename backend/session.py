"""
Сессия тренировки (задачи капитана, неделя 2).

Оборачивает движок + сценарий и управляет жизненным циклом:
старт / стоп / сброс. Один экземпляр = одна тренировка одного обучаемого.
"""
from __future__ import annotations
from .engine import SimulationEngine
from .ai_module import ErrorAnalyzer
from .scenarios import get as get_scenario
from .models import ControlCommand, ParameterState, AIFeedback, OperatorAction


class TrainingSession:
    def __init__(self, operator: str = "unknown") -> None:
        self.operator = operator
        self.session_id: str | None = None   # проставляется хранилищем при старте
        self.scenario_id: str | None = None
        self.active = False
        self.engine = SimulationEngine()
        self.ai = ErrorAnalyzer()

    def start(self, scenario_id: str) -> bool:
        """Загрузить сценарий и начать тренировку. False — сценарий не найден."""
        sc = get_scenario(scenario_id)
        if sc is None:
            return False
        self.scenario_id = scenario_id
        self.engine = SimulationEngine()
        self.engine.load_scenario(initial=sc.initial, faults=sc.faults)
        self.active = True
        return True

    def reset(self) -> None:
        """Перезапустить текущий сценарий с начала."""
        if self.scenario_id:
            self.start(self.scenario_id)

    def stop(self) -> None:
        self.active = False

    # --- шаг симуляции ---
    def tick(self) -> tuple[ParameterState, AIFeedback, list[str]]:
        state = self.engine.step(dt=1.0)
        feedback = self.ai.analyze(state, None)
        return state, feedback, list(self.engine.events)

    # --- команда оператора ---
    def command(self, cmd: ControlCommand) -> None:
        self.engine.apply(cmd)
