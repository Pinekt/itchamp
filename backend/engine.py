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
import numpy as np
from .models import ParameterState, EquipmentState, ControlCommand, ActionType
from scipy.integrate import odeint
import matplotlib.pyplot as plt


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
        # --- исполнение сценария (задачи капитана, неделя 2) ---
        self.faults: list[dict] = []      # [{at, target, type}, ...]
        self._fired: set[int] = set()     # индексы уже сработавших отказов
        self._pressure_bias = 0.0         # накопленное возмущение давления от отказов
        self.events: list[str] = []       # события текущего шага (для журнала/ИИ)

    def load_scenario(self, initial: dict | None = None, faults: list[dict] | None = None) -> None:
        """Задать начальное состояние и запланированные отказы сценария."""
        self.faults = list(faults or [])
        self._fired.clear()
        self._pressure_bias = 0.0
        for k, v in (initial or {}).items():
            if hasattr(self, k):
                setattr(self, k, v)

    def _apply_faults(self) -> None:
        """Срабатывание отказов, у которых наступило время at."""
        self.events = []
        for i, f in enumerate(self.faults):
            if i in self._fired or self.t < f.get("at", 0):
                continue
            self._fired.add(i)
            ftype = f.get("type")
            target = f.get("target", "")
            if ftype == "trip":                 # аварийный останов насоса
                self.pump_on = False
                self.events.append(f"FAULT:trip:{target}")
            elif ftype == "pressure_up":         # нештатный рост давления
                self._pressure_bias += 60.0
                self.events.append(f"FAULT:pressure_up:{target}")
            elif ftype == "heater_off":
                self.heater_on = False
                self.events.append(f"FAULT:heater_off:{target}")

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
        self._apply_faults()
        if self.running and self.pump_on:
            target_flow = self.feed_valve * 1.5
        else:
            target_flow = 0.0
        # простая релаксация к целевым значениям (заглушка вместо ОДУ)
        # self.flow += (target_flow - self.flow) * 0.2 * dt
        # heat = self.setpoint_temp if (self.running and self.heater_on) else 25.0
        # self.temperature += (heat - self.temperature) * 0.05 * dt
        # self.pressure += (self.flow * 1.1 + 30 + self._pressure_bias - self.pressure) * 0.1 * dt
        # self._pressure_bias *= 0.97  # возмущение затухает со временем
       

        # --- 1. ПАРАМЕТРЫ ПРОЦЕССА (Физические константы для фракций) ---
        # Условные параметры для смеси (бензин + мазут)
        Cp_liquid = 2.5       # Теплоемкость жидкости, кДж/(кг*К)
        Cp_vapor = 1.8        # Теплоемкость пара
        rho_liquid = 850      # Плотность жидкости, кг/м3
        H_vap = 350           # Теплота парообразования (скрытая), кДж/кг
        V_column = 150        # Объем тарелки (условно), м3

        # Коэффициент гидравлики (сопротивление тарелки)
        K_hydraulic = 0.05    # Коэффициент сопротивления для уравнения Дарси-Вейсбаха

        # --- 2. ФУНКЦИЯ МОДЕЛИ (Система ДУ) ---
        def distillation_tray_model(y, t, Q_feed, T_feed, Q_heat):
            """
            Модель одной тарелки ректификационной колонны.
            y = [L_level, T_tray, P_tray] 
            L_level - уровень жидкости на тарелке (аналог массы)
            T_tray  - температура на тарелке
            P_tray  - давление паров
            """
            L_level, T_tray, P_tray = y

            # --- А. ТЕПЛОВОЙ БАЛАНС (Уравнение изменения температуры) ---
            # Приход тепла: от притока сырья + от нагревателя (куба)
            # Расход тепла: на испарение жидкости
            # Упрощенное уравнение: dT/dt = (Q_вход - Q_выход) / (M * Cp)
            
            # Тепло, идущее на испарение при текущей температуре
            # (Пропорционально давлению пара и отклонению от точки кипения)
            # Допустим, точка кипения условно 180°C
            T_boiling = 180.0 
            vapor_flow = 0.0
            
            if T_tray > T_boiling:
                # Если температура выше кипения, жидкость испаряется
                # Количество пара пропорционально подведенному теплу
                vapor_flow = Q_heat / H_vap  # кг/сек (упрощение)
            
            # Уравнение теплового баланса:
            dT_dt = (Q_feed * Cp_liquid * (T_feed - T_tray) + Q_heat - vapor_flow * H_vap) / (L_level * Cp_liquid)

            # --- Б. МАТЕРИАЛЬНЫЙ БАЛАНС (Изменение уровня жидкости) ---
            # dM/dt = Приток - Расход
            # Расход жидкости вниз зависит от уровня (как в открытом сосуде)
            # и от перепада давления (если пар идет вверх, он тормозит жидкость)
            flow_out = 0.1 * np.sqrt(L_level) # Расход через сливное устройство
            
            dL_dt = Q_feed - flow_out - vapor_flow 

            # --- В. ГИДРАВЛИКА (Изменение давления) ---
            # Давление зависит от количества пара (чем больше пара, тем выше давление)
            # и от температуры (идеальный газ: P*V = n*R*T)
            # Примем, что объем парового пространства постоянен
            
            # Модель: dP/dt пропорционально притоку пара минус утечка (через верх колонны)
            # Константа времени для давления
            tau_p = 2.0 
            dP_dt = (vapor_flow * 8.314 * (T_tray + 273) / (V_column * 1000) - P_tray) / tau_p

            return [dL_dt, dT_dt, dP_dt]

        # --- 3. ЗАПУСК СИМУЛЯЦИИ ---
        # Время моделирования (сек)
        t = np.linspace(0, 100, 1000)

        # Начальные условия (Уровень, Температура, Давление)
        # Допустим, колонна уже заполнена наполовину
        y0 = [500.0, 150.0, 101.3] 

        # Сценарий 1: Штатный режим (Q_feed = 10 кг/с, Нагрев = 50 кВт)
        # Сценарий 2: Аварийный режим - рост давления (Нагрев вырос до 200 кВт)
        # Сценарий 3: Остановка подачи сырья (Q_feed = 0)

        scenarios = {
            'Normal Mode': {'Q_feed': 10, 'Q_heat': 50},
            'Heater Failure (High P)': {'Q_feed': 10, 'Q_heat': 200},
            'Feed Cut Off': {'Q_feed': 0, 'Q_heat': 50}
        }

        plt.figure(figsize=(15, 10))

        # Проходим по всем сценариям
        for idx, (scenario_name, params) in enumerate(scenarios.items()):
            Q_feed_val = params['Q_feed']
            Q_heat_val = params['Q_heat']
            T_feed_const = 20.0 # Температура подачи сырья (холодное)

            # Запуск решателя
            # args передает постоянные параметры в функцию (кроме переменных y и t)
            solution = odeint(distillation_tray_model, y0, t, args=(Q_feed_val, T_feed_const, Q_heat_val))

            # Извлекаем результаты
            L_result = solution[:, 0]
            T_result = solution[:, 1]
            P_result = solution[:, 2]

            # --- Графики ---
            
            # 1. Уровень жидкости (Материальный баланс)
            plt.subplot(3, 1, 1)
            plt.plot(t, L_result, label=scenario_name)
            plt.title('Динамика уровня жидкости на тарелке')
            plt.ylabel('Уровень (усл. ед.)')
            plt.grid(True)
            plt.legend()

            # 2. Температура (Тепловой баланс)
            plt.subplot(3, 1, 2)
            plt.plot(t, T_result, label=scenario_name)
            plt.title('Динамика температуры на тарелке')
            plt.ylabel('Температура (°C)')
            plt.grid(True)
            plt.legend()

            # 3. Давление (Гидравлика)
            plt.subplot(3, 1, 3)
            plt.plot(t, P_result, label=scenario_name)
            plt.title('Динамика давления на тарелке')
            plt.ylabel('Давление (кПа)')
            plt.xlabel('Время (сек)')
            plt.grid(True)
            plt.legend()

        plt.tight_layout()
        plt.show()

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
