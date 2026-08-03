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
        self._initial: dict | None = None
        self._faults: list | None = None

    def start(self, scenario_id: str, initial: dict | None = None,
              faults: list | None = None) -> bool:
        """
        Загрузить сценарий и начать тренировку.
        initial/faults можно передать из БД; иначе берётся встроенный каталог.
        """
        if initial is None and faults is None:
            sc = get_scenario(scenario_id)
            if sc is None:
                return False
            initial, faults = sc.initial, sc.faults
        self.scenario_id = scenario_id
        self.engine = SimulationEngine()
        self.engine.load_scenario(initial=initial, faults=faults)
        self._initial, self._faults = initial, faults
        self.active = True
        return True

    def reset(self) -> None:
        """Перезапустить текущий сценарий с начала (сохраняя загруженные параметры)."""
        if self.scenario_id:
            self.start(self.scenario_id, self._initial, self._faults)

    def stop(self) -> None:
        self.active = False

    # --- шаг симуляции ---
    def tick(self) -> tuple[ParameterState, AIFeedback, list[str]]:
        state = self.engine.step(dt=1.0)
        feedback = self.ai.analyze(state)
        return state, feedback, list(self.engine.events)

    # --- команда оператора ---
    def command(self, cmd: ControlCommand) -> None:
        self.engine.apply(cmd)

    def record_action(self, action: OperatorAction) -> None:
        """
        Передать действие оператора ИИ-модулю.

        Разбирается оно не здесь, а на ближайшем такте: ошибочность действия
        зависит от состояния установки, а его считает движок. «Открыть клапан»
        — нормальная операция при выводе на режим и грубая ошибка при растущем
        давлении, и различить их можно только в контексте.
        """
        self.ai.observe_action(action)

    def load_reference(self, steps: list[dict]) -> None:
        """Эталонные шаги сценария — ИИ сверяет с ними последовательность."""
        self.ai.load_reference(steps)
