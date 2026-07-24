"""Каталог учебных сценариев (каркас). Расширяется командой на неделе 3."""
from .models import Scenario

SCENARIOS = [
    Scenario(id="startup", name="Пуск установки",
             description="Штатный пуск: включить насос, открыть клапан сырья, вывести на режим."),
    Scenario(id="pump_trip", name="Отказ насоса сырья",
             description="Внезапный останов PUMP_1 на 30-й секунде. Оператор должен стабилизировать процесс.",
             faults=[{"at": 30, "target": "PUMP_1", "type": "trip"}]),
    Scenario(id="pressure_alarm", name="Рост давления",
             description="Нештатный рост давления в колонне. Отработать снижение нагрузки.",
             faults=[{"at": 20, "target": "COLUMN_1", "type": "pressure_up"}]),
]

def get(scenario_id: str) -> Scenario | None:
    return next((s for s in SCENARIOS if s.id == scenario_id), None)
