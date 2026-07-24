# API-контракт КТК ЭЛОУ-АВТ

Единый стык трёх модулей. Источник истины — `backend/models.py`.
Меняем схемы только здесь и по согласованию капитана.

## Потоки данных

```
[UI оператора] --ControlCommand-->  [Движок/цифровой двойник]
[Движок]       --ParameterState-->  [UI] и [ИИ-модуль]
[ИИ-модуль]    --AIFeedback------>  [UI]
[Движок]       --OperatorAction-->  [Журнал/хранилище] --> [ИИ]
```

## Схемы

### ControlCommand (UI → движок)
| поле | тип | описание |
|---|---|---|
| action | enum | start / stop / set_pump / set_valve / set_setpoint / ack_alarm |
| target | str? | ID оборудования (PUMP_1, VALVE_FEED, …) |
| value | float? | числовое значение (%, °C, м³/ч) |

### ParameterState (движок → UI, ИИ)
| поле | тип | ед. |
|---|---|---|
| t | float | с (время симуляции) |
| pressure | float | кПа |
| temperature | float | °C |
| flow | float | м³/ч |
| level | float | % |
| running | bool | — |
| equipment | EquipmentState[] | состояние насосов/клапанов |
| alarms | str[] | активные аварии |

### AIFeedback (ИИ → UI)
| поле | тип | описание |
|---|---|---|
| error_detected | bool | обнаружена ли ошибка |
| error_class | str? | класс ошибки (справочник) |
| location | str? | где произошло |
| severity | enum | info / warning / error |
| message | str | интерпретируемое объяснение |
| recommendation | str? | что сделать |
| risk_score | float? | прогноз риска 0..1 |

### OperatorAction (журнал)
t, action, target, value, reaction_ms — фиксация действия и времени реакции.

## Транспорт
WebSocket `/ws`: сообщения `{type: state|feedback|action, payload: {...}}`.
REST: `GET /api/scenarios`, `GET /api/journal`.

## Правила совместной работы
1. Внешний вид схем в `models.py` — заморожен на неделю; расширения через PR + согласование.
2. Каждый модуль заменяет свою «начинку» (engine/ai/frontend), не трогая контракт.
3. Новые поля добавляем как опциональные, чтобы не ломать остальных.
