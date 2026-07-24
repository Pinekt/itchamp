"""
Единый контракт данных КТК ЭЛОУ-АВТ.

Это «стык» между тремя модулями команды:
  - Матмодель (цифровой двойник)  -> отдаёт ParameterState  (Светлана)
  - ИИ-модуль                     -> отдаёт AIFeedback       (Константин)
  - Интерфейс оператора (UI)      -> шлёт ControlCommand     (Михаил)

Меняем схемы ТОЛЬКО здесь и по согласованию — тогда модули не разъедутся.
"""
from __future__ import annotations
from enum import Enum
from typing import Optional
from pydantic import BaseModel, Field


# ---------- Команды от оператора (UI -> движок) ----------

class ActionType(str, Enum):
    START = "start"              # пуск установки
    STOP = "stop"               # останов
    SET_PUMP = "set_pump"       # включить/выключить насос
    SET_VALVE = "set_valve"     # положение клапана 0..100 %
    SET_SETPOINT = "set_setpoint"  # уставка (температура/расход и т.п.)
    ACK_ALARM = "ack_alarm"     # квитирование аварии


class ControlCommand(BaseModel):
    """Команда, которую UI отправляет в движок симуляции."""
    action: ActionType
    target: Optional[str] = Field(None, description="ID оборудования: PUMP_1, VALVE_FEED, ...")
    value: Optional[float] = Field(None, description="Числовое значение команды (%, °C, м3/ч)")


# ---------- Состояние процесса (движок -> UI и ИИ) ----------

class EquipmentState(BaseModel):
    id: str
    kind: str                    # pump | valve | column | heater | sensor
    on: Optional[bool] = None    # для насосов/нагревателей
    position: Optional[float] = None  # для клапанов, 0..100 %


class ParameterState(BaseModel):
    """Мгновенный снимок технологических параметров установки."""
    t: float = Field(..., description="Время симуляции, с")
    pressure: float = Field(..., description="Давление в колонне, кПа")
    temperature: float = Field(..., description="Температура низа колонны, °C")
    flow: float = Field(..., description="Расход сырья, м3/ч")
    level: float = Field(..., description="Уровень в кубе, %")
    running: bool = False
    equipment: list[EquipmentState] = []
    alarms: list[str] = []       # активные аварии/предупреждения


# ---------- Журнал действий (движок -> хранилище/ИИ) ----------

class OperatorAction(BaseModel):
    """Запись действия оператора для журнала и анализа ИИ."""
    t: float
    action: ActionType
    target: Optional[str] = None
    value: Optional[float] = None
    reaction_ms: Optional[int] = Field(None, description="Время реакции с момента события, мс")


# ---------- Обратная связь ИИ (ИИ -> UI) ----------

class Severity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class AIFeedback(BaseModel):
    """Результат анализа действий оператора ИИ-модулем."""
    t: float
    error_detected: bool
    error_class: Optional[str] = Field(None, description="Класс ошибки (справочник)")
    location: Optional[str] = Field(None, description="Где: оборудование/этап")
    severity: Severity = Severity.INFO
    message: str = ""            # интерпретируемое объяснение «почему неверно»
    recommendation: Optional[str] = None
    risk_score: Optional[float] = Field(None, description="Прогноз риска ошибки 0..1")


# ---------- Сценарии обучения ----------

class Scenario(BaseModel):
    id: str
    name: str
    description: str
    # начальное состояние и запланированные отказы задаёт матмодель/сценарист
    initial: dict = {}
    faults: list[dict] = []      # [{at: 30, target: PUMP_1, type: trip}, ...]


# ---------- Обёртка сообщений WebSocket ----------

class WSMessageType(str, Enum):
    STATE = "state"        # ParameterState
    FEEDBACK = "feedback"  # AIFeedback
    ACTION = "action"      # OperatorAction (эхо в журнал)


class WSMessage(BaseModel):
    type: WSMessageType
    payload: dict
