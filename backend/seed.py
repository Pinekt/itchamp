"""
Наполнение БД начальными данными: пользователи (роли), сценарии и эталонные шаги.
Выполняется автоматически при старте, если таблицы пустые (идемпотентно).
"""
from __future__ import annotations
import hashlib
from sqlalchemy import select, func

from .db import Session, User, Scenario, ScenarioStep


def _hash(p: str) -> str:
    # Учебная заглушка. В проде — bcrypt/argon2 (задача по ИБ, Элтон/Михаил).
    return hashlib.sha256(p.encode()).hexdigest()


USERS = [
    dict(login="operator", full_name="Оператор-стажёр", role="operator", pwd="operator"),
    dict(login="instructor", full_name="Инструктор", role="instructor", pwd="instructor"),
    dict(login="admin", full_name="Администратор КТК", role="admin", pwd="admin"),
]

SCENARIOS = [
    dict(code="startup", name="Пуск установки", difficulty=1,
         description="Штатный пуск: включить насос, открыть клапан сырья, вывести на режим.",
         initial={"level": 40.0, "temperature": 120.0, "running": False},
         faults=[],
         steps=[
             dict(order_no=1, expected_action="set_pump", expected_target="PUMP_1",
                  window_s=20, description="Включить насос сырья", critical=True),
             dict(order_no=2, expected_action="set_valve", expected_target="VALVE_FEED",
                  window_s=30, description="Открыть клапан сырья до рабочего значения"),
             dict(order_no=3, expected_action="start", expected_target=None,
                  window_s=40, description="Пустить процесс", critical=True),
         ]),
    dict(code="pump_trip", name="Отказ насоса сырья", difficulty=2,
         description="Внезапный останов PUMP_1. Оператор должен стабилизировать процесс.",
         initial={"level": 55.0, "temperature": 340.0, "running": True},
         faults=[{"at": 12, "target": "PUMP_1", "type": "trip"}],
         steps=[
             dict(order_no=1, expected_action="set_valve", expected_target="VALVE_FEED",
                  window_s=20, description="Снизить подачу сырья после отказа", critical=True),
             dict(order_no=2, expected_action="ack_alarm", expected_target=None,
                  window_s=30, description="Квитировать аварийный сигнал"),
             dict(order_no=3, expected_action="set_pump", expected_target="PUMP_1",
                  window_s=60, description="Запустить резервный насос"),
         ]),
    dict(code="pressure_alarm", name="Рост давления в колонне", difficulty=3,
         description="Нештатный рост давления. Отработать снижение нагрузки.",
         initial={"level": 60.0, "temperature": 350.0, "running": True},
         faults=[{"at": 10, "target": "COLUMN_1", "type": "pressure_up"}],
         steps=[
             dict(order_no=1, expected_action="set_valve", expected_target="VALVE_FEED",
                  window_s=15, description="Прикрыть клапан сырья", critical=True),
             dict(order_no=2, expected_action="ack_alarm", expected_target=None,
                  window_s=25, description="Квитировать аварию по давлению"),
         ]),
]


async def seed() -> None:
    async with Session() as s:
        if (await s.execute(select(func.count(User.id)))).scalar() == 0:
            for u in USERS:
                s.add(User(login=u["login"], full_name=u["full_name"],
                           role=u["role"], password_hash=_hash(u["pwd"])))
            await s.commit()
            print("[seed] пользователи созданы:", ", ".join(u["login"] for u in USERS))

        if (await s.execute(select(func.count(Scenario.id)))).scalar() == 0:
            for sc in SCENARIOS:
                row = Scenario(code=sc["code"], name=sc["name"],
                               description=sc["description"], initial=sc["initial"],
                               faults=sc["faults"], difficulty=sc["difficulty"])
                s.add(row)
                await s.flush()
                for st in sc["steps"]:
                    s.add(ScenarioStep(scenario_id=row.id, **st))
            await s.commit()
            print("[seed] сценарии и эталонные шаги созданы:",
                  ", ".join(sc["code"] for sc in SCENARIOS))
