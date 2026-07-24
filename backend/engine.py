"""
Движок симуляции (ЗАГЛУШКА-КАРКАС).

>>> Зона ответственности: Светлана (цифровой двойник) <<<
Здесь сейчас упрощённая динамика 1 узла, чтобы UI показывал живые параметры.
На неделе 2 внутренности заменяются на реальные матмодели ректификации,
теплообмена и гидравлики. ВАЖНО: наружу метод step() должен и дальше
возвращать ParameterState по контракту (backend/models.py) — тогда UI и ИИ
менять не придётся.
"""
from __future__ import annotations
import math
from .models import ParameterState, EquipmentState, ControlCommand, ActionType


class SimulationEngine:
    def __init__(self) -> None:
        self.t = 0.0
        self.running = False
        self.feed_valve = 60.0      # % открытия клапана сырья
        self.pump_on = True
        self.heater_on = True
        self.setpoint_temp = 350.0  # уставка температуры низа, °C
        # текущие параметры
        self.pressure = 120.0
        self.temperature = 340.0
        self.flow = 90.0
        self.level = 50.0
        self.alarms: list[str] = []

    # --- приём команды от UI ---
    def apply(self, cmd: ControlCommand) -> None:
        if cmd.action == ActionType.START:
            self.running = True
        elif cmd.action == ActionType.STOP:
            self.running = False
        elif cmd.action == ActionType.SET_PUMP and cmd.target:
            self.pump_on = bool(cmd.value)
        elif cmd.action == ActionType.SET_VALVE and cmd.value is not None:
            self.feed_valve = max(0.0, min(100.0, cmd.value))
        elif cmd.action == ActionType.SET_SETPOINT and cmd.value is not None:
            self.setpoint_temp = cmd.value
        elif cmd.action == ActionType.ACK_ALARM:
            self.alarms.clear()

    # --- один шаг интегрирования (dt секунд) ---
    def step(self, dt: float = 1.0) -> ParameterState:
        self.t += dt
        if self.running and self.pump_on:
            target_flow = self.feed_valve * 1.5
        else:
            target_flow = 0.0
        # простая релаксация к целевым значениям (заглушка вместо ОДУ)
        self.flow += (target_flow - self.flow) * 0.2 * dt
        heat = self.setpoint_temp if (self.running and self.heater_on) else 25.0
        self.temperature += (heat - self.temperature) * 0.05 * dt
        self.pressure += (self.flow * 1.1 + 30 - self.pressure) * 0.1 * dt
        # уровень: приток минус кипение
        self.level += (self.flow * 0.05 - self.temperature * 0.012) * dt
        self.level = max(0.0, min(100.0, self.level))
        # лёгкий шум-пульсация для реалистичности
        self.pressure += math.sin(self.t / 5) * 0.4

        self._check_alarms()
        return self._snapshot()

    def _check_alarms(self) -> None:
        self.alarms = []
        if self.pressure > 180:
            self.alarms.append("HIGH_PRESSURE")
        if self.level > 90:
            self.alarms.append("HIGH_LEVEL")
        if self.level < 10 and self.running:
            self.alarms.append("LOW_LEVEL")
        if self.temperature > 400:
            self.alarms.append("HIGH_TEMP")

    def _snapshot(self) -> ParameterState:
        return ParameterState(
            t=round(self.t, 1),
            pressure=round(self.pressure, 1),
            temperature=round(self.temperature, 1),
            flow=round(self.flow, 1),
            level=round(self.level, 1),
            running=self.running,
            equipment=[
                EquipmentState(id="PUMP_1", kind="pump", on=self.pump_on),
                EquipmentState(id="VALVE_FEED", kind="valve", position=round(self.feed_valve, 1)),
                EquipmentState(id="HEATER_1", kind="heater", on=self.heater_on),
                EquipmentState(id="COLUMN_1", kind="column"),
            ],
            alarms=self.alarms,
        )
