"""
ИИ-модуль: классификация и локализация ошибок оператора, объяснение и прогноз риска.

Что делает:

  1. **Классифицирует ошибки** по справочнику `CATALOGUE`. Каждый класс несёт
     не только название, но и объяснение последствия со ссылкой на пункт
     технологического регламента установки — критерий требует
     интерпретируемой обратной связи, а «ошибка давления» ничего оператору
     не объясняет.
  2. **Локализует** — указывает аппарат или узел (COLUMN_1, PUMP_1, VALVE_FEED).
  3. **Смотрит на действия оператора**, а не только на состояние установки.
     Разбор «оператор увеличил подачу при уже растущем давлении» невозможен,
     если видеть одни показания приборов.
  4. **Прогнозирует риск** экстраполяцией тренда: по последним снимкам
     считается скорость изменения параметра и время до аварийной уставки.
     Это даёт предупреждение ДО срабатывания сигнализации, а не после.

Почему база знаний, а не обученная модель. Размеченных тренировок пока нет:
классификатор, обученный на синтетике, выдавал бы уверенные ответы, ни на чём
не основанные. Правила же опираются на техрегламент, проверяемы и объяснимы —
а данные для обучения копятся с каждой тренировкой (`operator_actions`,
`telemetry`, `detected_errors`). Когда наберётся статистика, `classify`
заменяется обученной моделью: справочник классов и признаки — это уже
готовая разметка. Место для замены отмечено ниже.

Наружу отдаётся AIFeedback по контракту (backend/models.py).
"""
from __future__ import annotations

from dataclasses import dataclass

from .models import ActionType, AIFeedback, OperatorAction, ParameterState, Severity


# ------------------------------------------------------- уставки регламента

#: Границы, при выходе за которые состояние считается нештатным.
#: Значения согласованы с `engine._check_alarms`; уровень взят из регламента
#: (разд. 7.7.1.14: не допускать снижение уровня в колоннах ниже 20 %).
LIMITS = dict(
    pressure_max=180.0,     # кПа
    temperature_max=400.0,  # °C
    level_max=90.0,         # %
    level_min=20.0,         # %
)

#: Насколько заранее предупреждать. Если по текущему тренду уставка будет
#: достигнута в пределах этого времени — выдаём предупреждение, не дожидаясь
#: срабатывания сигнализации.
FORECAST_HORIZON_S = 30.0

#: Сколько последних снимков держим для оценки тренда. Пятнадцать секунд —
#: достаточно, чтобы отличить устойчивый рост от шума, и достаточно мало,
#: чтобы прогноз успевал за действиями оператора.
TREND_WINDOW = 15


# ------------------------------------------------------- справочник ошибок

@dataclass(frozen=True)
class ErrorClass:
    """
    Класс ошибки: что произошло, чем это грозит и что делать.

    `why` и `reference` — то, ради чего справочник вообще существует.
    Обучаемому недостаточно услышать «pressure_runaway»: он должен понять,
    к чему это ведёт на реальной установке и каким пунктом регламента
    запрещено.
    """
    code: str
    location: str
    severity: Severity
    message: str
    why: str
    recommendation: str
    reference: str


CATALOGUE: dict[str, ErrorClass] = {
    "pressure_runaway": ErrorClass(
        code="pressure_runaway", location="COLUMN_1", severity=Severity.ERROR,
        message="Давление в колонне выше уставки.",
        why="Рост давления ведёт к срабатыванию предохранительных клапанов "
            "и сбросу парогазовой смеси в атмосферу — загазованность и риск "
            "воспламенения.",
        recommendation="Прикрыть клапан сырья до 40 % и снизить нагрузку.",
        reference="регламент, разд. 7.7.1.16"),

    "low_level_drain": ErrorClass(
        code="low_level_drain", location="COLUMN_1", severity=Severity.ERROR,
        message="Уровень в кубе колонны ниже допустимого (20 %).",
        why="Падение уровня приводит к срыву печных насосов и прекращению "
            "циркуляции по змеевику печей: трубы деформируются и прогорают, "
            "это прямой путь к пожару на печи.",
        recommendation="Увеличить подачу сырья и сократить отбор с низа.",
        reference="регламент, разд. 7.7.1.14 и 7.10.3"),

    "high_level": ErrorClass(
        code="high_level", location="COLUMN_1", severity=Severity.ERROR,
        message="Уровень в кубе колонны выше допустимого.",
        why="Переполнение куба ведёт к уносу жидкости в вышележащие тарелки "
            "и срыву режима ректификации.",
        recommendation="Увеличить отбор с низа колонны или снизить приток.",
        reference="регламент, разд. 7.7.1.2"),

    "temp_runaway": ErrorClass(
        code="temp_runaway", location="HEATER_1", severity=Severity.ERROR,
        message="Температура низа выше нормы технологического режима.",
        why="Завышенная температура и неравномерный прогрев деформируют трубы "
            "и аппараты, нарушают герметичность — загазованность, взрыв, пожар.",
        recommendation="Снизить уставку нагрева и проверить режим печи.",
        reference="регламент, разд. 7.10.2"),

    "load_increase_under_alarm": ErrorClass(
        code="load_increase_under_alarm", location="VALVE_FEED", severity=Severity.ERROR,
        message="Подача сырья увеличена при активной аварии по давлению.",
        why="Действие противоположно требуемому: нагрузка растёт там, где её "
            "нужно сбрасывать, и авария развивается быстрее.",
        recommendation="Немедленно прикрыть клапан сырья.",
        reference="регламент, разд. 7.7.1.2"),

    "pump_off_under_load": ErrorClass(
        code="pump_off_under_load", location="PUMP_1", severity=Severity.ERROR,
        message="Насос остановлен при работающей установке.",
        why="Останов насоса прекращает циркуляцию нефти по змеевику печей — "
            "трубы деформируются и прогорают.",
        recommendation="Включить насос или остановить процесс штатно.",
        reference="регламент, разд. 7.10.3"),

    "ack_without_fix": ErrorClass(
        code="ack_without_fix", location="COLUMN_1", severity=Severity.WARNING,
        message="Авария квитирована, но причина не устранена.",
        why="Квитирование убирает сигнал, а не причину: оператор теряет "
            "предупреждение и продолжает вести режим с нарушением.",
        recommendation="Устранить причину, затем квитировать.",
        reference="регламент, разд. 7.7.1.3"),

    "missed_step": ErrorClass(
        code="missed_step", location="", severity=Severity.ERROR,
        message="Пропущен шаг регламентной последовательности.",
        why="Невыполнение операций и несоблюдение очерёдности при выводе на "
            "режим ведёт к неравномерному прогреву и деформации аппаратов.",
        recommendation="Выполнить пропущенный шаг.",
        reference="регламент, разд. 7.7.1.1 и 7.10.1"),
}


# --------------------------------------------------------- прогноз риска

class TrendForecast:
    """
    Оценка скорости изменения параметров и времени до аварийной уставки.

    Скорость считается методом наименьших квадратов по скользящему окну —
    одной разности соседних снимков мало: в показаниях есть пульсация, и по
    двум точкам «растёт» и «падает» чередовались бы каждый такт.
    """

    def __init__(self, window: int = TREND_WINDOW) -> None:
        self.window = window
        self._history: list[tuple[float, dict[str, float]]] = []

    def add(self, state: ParameterState) -> None:
        self._history.append((state.t, dict(
            pressure=state.pressure, temperature=state.temperature,
            level=state.level, flow=state.flow)))
        if len(self._history) > self.window:
            self._history.pop(0)

    def rate(self, name: str) -> float:
        """Скорость изменения параметра, единиц в секунду. 0 — данных мало."""
        if len(self._history) < 3:
            return 0.0
        ts = [t for t, _ in self._history]
        ys = [v[name] for _, v in self._history]
        n = len(ts)
        mean_t = sum(ts) / n
        mean_y = sum(ys) / n
        denom = sum((t - mean_t) ** 2 for t in ts)
        if denom == 0:
            return 0.0
        return sum((t - mean_t) * (y - mean_y) for t, y in zip(ts, ys)) / denom

    def seconds_to(self, name: str, current: float, limit: float,
                   rising: bool = True) -> float | None:
        """
        Через сколько секунд параметр достигнет уставки при текущем тренде.
        None — если движется не в сторону уставки или уже за ней.
        """
        r = self.rate(name)
        if rising:
            if r <= 0 or current >= limit:
                return None
            return (limit - current) / r
        if r >= 0 or current <= limit:
            return None
        return (current - limit) / (-r)

    def nearest_threat(self, state: ParameterState) -> tuple[str, float] | None:
        """
        Ближайшая по времени угроза: (имя параметра, секунд до уставки).
        Смотрим все контролируемые границы и берём самую близкую.
        """
        checks = [
            ("pressure", state.pressure, LIMITS["pressure_max"], True),
            ("temperature", state.temperature, LIMITS["temperature_max"], True),
            ("level", state.level, LIMITS["level_max"], True),
            ("level", state.level, LIMITS["level_min"], False),
        ]
        threats = []
        for name, cur, limit, rising in checks:
            left = self.seconds_to(name, cur, limit, rising)
            if left is not None and left <= FORECAST_HORIZON_S:
                threats.append((name, left))
        return min(threats, key=lambda x: x[1]) if threats else None


#: Как время до уставки превращается в оценку риска 0..1. Порог сигнализации
#: уже пройден — риск максимальный; за горизонтом прогноза — минимальный.
def _risk_from_seconds(seconds: float) -> float:
    return round(max(0.0, min(1.0, 1.0 - seconds / FORECAST_HORIZON_S)), 2)


# ------------------------------------------------------------ анализатор

class ErrorAnalyzer:
    """
    Разбирает состояние установки и действия оператора, выдаёт AIFeedback.

    Используется по одному экземпляру на тренировку: внутри копится история
    для тренда и признаки уже выданных предупреждений.
    """

    def __init__(self) -> None:
        self.forecast = TrendForecast()
        self._pending: list[OperatorAction] = []
        self._reference: list[dict] = []
        #: Такт, на котором авария стала активной. Нужен, чтобы отличить
        #: «квитировал и устранил» от «квитировал и оставил как есть».
        self._alarm_since: float | None = None
        self._acked_at: float | None = None
        #: Положение клапана на предыдущем такте. Сравнивать команду с текущим
        #: нельзя: движок применяет её сразу, и к моменту разбора клапан уже
        #: стоит в новом положении — «увеличил» неотличимо от «не трогал».
        self._prev_valve: float | None = None

    # --- вход от сессии -------------------------------------------------

    def load_reference(self, steps: list[dict]) -> None:
        """Эталонная последовательность сценария — для контроля пропуска шагов."""
        self._reference = list(steps or [])

    def observe_action(self, action: OperatorAction) -> None:
        """
        Запомнить действие оператора. Разбирается на ближайшем такте: решение
        о том, ошибочно ли действие, зависит от состояния установки, а оно
        считается движком.
        """
        self._pending.append(action)

    # --- основной разбор -------------------------------------------------

    def analyze(self, state: ParameterState,
                last_action: OperatorAction | None = None) -> AIFeedback:
        """
        Оценить обстановку. Порядок проверок — по убыванию тяжести: сначала
        ошибочные действия оператора, затем выход за уставки, затем прогноз.
        Возвращается одно, самое важное сообщение: вываливать на оператора
        три предупреждения разом — верный способ, чтобы он не прочитал ни одно.
        """
        if last_action is not None:
            self._pending.append(last_action)
        self.forecast.add(state)
        actions, self._pending = self._pending, []

        # Положение органов управления ДО применения команд этого такта.
        # Запоминаем в локальной переменной и обновляем только в самом конце:
        # иначе разбор действия сравнивал бы команду с положением, которое
        # эта же команда и установила.
        was_valve = self._prev_valve
        if was_valve is None:                # первый такт — сравнивать не с чем
            was_valve = self._equipment_position(state, "VALVE_FEED")

        self._track_alarm(state, actions)
        threat = self.forecast.nearest_threat(state)
        self._remember(state)

        # 1. Действие оператора, противоречащее обстановке
        for action in actions:
            hit = self._classify_action(state, action, threat, was_valve)
            if hit:
                return self._feedback(state, hit, risk=0.9)

        # 2. Состояние вне уставок
        breach = self._classify_state(state)
        if breach:
            return self._feedback(state, breach, risk=1.0)

        # 3. Квитировал, но причина осталась
        if self._acked_without_fix(state):
            return self._feedback(state, CATALOGUE["ack_without_fix"], risk=0.7)

        # 4. Прогноз: авария ещё не наступила, но тренд ведёт к ней
        if threat:
            name, seconds = threat
            return self._forecast_feedback(state, name, seconds)

        return AIFeedback(t=state.t, error_detected=False, severity=Severity.INFO,
                          message="Параметры в норме.", risk_score=0.05)

    def _remember(self, state: ParameterState) -> None:
        """Запомнить положение органов управления для следующего такта."""
        pos = self._equipment_position(state, "VALVE_FEED")
        if pos is not None:
            self._prev_valve = pos

    # --- разбор по видам --------------------------------------------------

    def _classify_state(self, state: ParameterState) -> ErrorClass | None:
        """
        Выход за уставку. Здесь же место для обученной модели: на входе те же
        признаки (значения и скорости их изменения), на выходе — код класса
        из CATALOGUE.
        """
        if state.pressure > LIMITS["pressure_max"]:
            return CATALOGUE["pressure_runaway"]
        if state.temperature > LIMITS["temperature_max"]:
            return CATALOGUE["temp_runaway"]
        if state.level > LIMITS["level_max"]:
            return CATALOGUE["high_level"]
        # низкий уровень опасен только на идущем процессе: на остановленной
        # установке пустой куб — это норма, а не авария
        if state.running and state.level < LIMITS["level_min"]:
            return CATALOGUE["low_level_drain"]
        return None

    def _classify_action(self, state: ParameterState, action: OperatorAction,
                         threat: tuple[str, float] | None = None,
                         was_valve: float | None = None) -> ErrorClass | None:
        """
        Ошибочное действие — то, чего не видно по одним показаниям приборов.
        Проверяется в контексте текущей обстановки: то же самое действие может
        быть верным или грубой ошибкой в зависимости от состояния установки.

        Обстановка — это не только сработавшая сигнализация, но и активный
        прогноз. Оператор, добавляющий нагрузку, когда давление уже уверенно
        идёт к уставке, ошибается в этот момент, а не через семь секунд, когда
        загорится авария. Ловить позже — значит показывать замечание тогда,
        когда обучаемый уже не свяжет его со своим действием.
        """
        rising_pressure = threat is not None and threat[0] == "pressure"
        pressure_trouble = ("HIGH_PRESSURE" in state.alarms
                            or state.pressure > LIMITS["pressure_max"] * self.SAFE_MARGIN
                            or rising_pressure)

        if action.action == ActionType.SET_VALVE and action.value is not None:
            if pressure_trouble and was_valve is not None and action.value > was_valve:
                return CATALOGUE["load_increase_under_alarm"]

        if action.action == ActionType.SET_PUMP and not action.value and state.running:
            return CATALOGUE["pump_off_under_load"]

        return None

    #: Доля уставки, ниже которой режим считаем приведённым в норму.
    #: Параметр, замерший в паре процентов от границы, — это не «устранено»,
    #: а «пока не сработало»: одного порыва хватит, чтобы сигнализация ожила.
    SAFE_MARGIN = 0.95

    def _still_risky(self, state: ParameterState) -> bool:
        """Режим ещё не приведён в норму — параметр у самой границы уставки."""
        return (state.pressure > LIMITS["pressure_max"] * self.SAFE_MARGIN
                or state.temperature > LIMITS["temperature_max"] * self.SAFE_MARGIN
                or state.level > LIMITS["level_max"] * self.SAFE_MARGIN
                or (state.running and state.level < LIMITS["level_min"] / self.SAFE_MARGIN))

    def _acked_without_fix(self, state: ParameterState) -> bool:
        """Квитирование было, а причина никуда не делась."""
        if self._acked_at is None:
            return False
        # после квитирования дали несколько тактов на устранение
        if state.t - self._acked_at < 5:
            return False
        return self._still_risky(state)

    def _track_alarm(self, state: ParameterState,
                     actions: list[OperatorAction]) -> None:
        if state.alarms and self._alarm_since is None:
            self._alarm_since = state.t
        # Условие сброса — то же самое `_still_risky`, что и в проверке выше.
        # Пока они расходились, признак квитирования сбрасывался у параметра,
        # который формально ушёл под уставку, но остался в опасной зоне.
        if not state.alarms and not self._still_risky(state):
            self._alarm_since = None
            self._acked_at = None
        for a in actions:
            if a.action == ActionType.ACK_ALARM:
                self._acked_at = state.t

    # --- сборка ответа ---------------------------------------------------

    @staticmethod
    def _equipment_position(state: ParameterState, eq_id: str) -> float | None:
        for e in state.equipment:
            if e.id == eq_id:
                return e.position
        return None

    def _feedback(self, state: ParameterState, cls: ErrorClass,
                  risk: float) -> AIFeedback:
        return AIFeedback(
            t=state.t, error_detected=True, error_class=cls.code,
            location=cls.location or None, severity=cls.severity,
            message=f"{cls.message} {cls.why}",
            recommendation=cls.recommendation,
            risk_score=risk, reference=cls.reference)

    def _forecast_feedback(self, state: ParameterState, name: str,
                           seconds: float) -> AIFeedback:
        """
        Предупреждение до аварии. Ошибкой не считается: оператор ещё ничего не
        нарушил, и записывать это в оценку было бы нечестно — но предупредить
        обязаны, в этом и смысл прогноза.
        """
        names = {"pressure": "давление", "temperature": "температура",
                 "level": "уровень"}
        return AIFeedback(
            t=state.t, error_detected=False, error_class=None,
            location="COLUMN_1", severity=Severity.WARNING,
            # Число секунд намеренно не дублируется в тексте: оно уходит
            # в predicted_alarm_s, и интерфейс показывает его отдельной
            # строкой. Иначе одно и то же время печаталось дважды и с разным
            # округлением — «через 19 с» рядом с «≈ 20 с».
            message=f"При текущем тренде {names.get(name, name)} выйдет "
                    f"за уставку.",
            recommendation="Скорректировать режим заранее, не дожидаясь "
                           "срабатывания сигнализации.",
            risk_score=_risk_from_seconds(seconds),
            predicted_alarm_s=round(seconds, 1))


# ------------------------------------------------- адаптивный подбор сценария

#: Какие классы ошибок отрабатывает сценарий — выводится из его отказов,
#: а не задаётся отдельной таблицей. Тип отказа и определяет, чему сценарий
#: учит: `pressure_up` ставит обучаемого перед ростом давления, `trip` — перед
#: потерей насоса. Отдельное поле в БД пришлось бы поддерживать вручную и
#: рассинхронизировать при первой же правке сценария.
FAULT_TRAINS: dict[str, set[str]] = {
    "pressure_up": {"pressure_runaway", "load_increase_under_alarm", "ack_without_fix"},
    "trip": {"pump_off_under_load", "low_level_drain"},
    "heater_off": {"temp_runaway"},
}

#: Сколько последних тренировок смотрим, разыскивая повторяющуюся ошибку.
RECENT_WINDOW = 5

#: Со скольких раз ошибка считается устойчивой, а не случайной.
REPEAT_THRESHOLD = 2


@dataclass(frozen=True)
class Recommendation:
    """
    Что тренировать дальше и почему.

    `why` обязательно: инструктор должен видеть основание, иначе подбор
    выглядит как случайный выбор из списка и доверия к нему нет.
    """
    scenario: str
    reason: str
    why: str


def _trains(scenario: dict) -> set[str]:
    """Классы ошибок, которые отрабатывает сценарий."""
    out: set[str] = set()
    for fault in scenario.get("faults") or []:
        out |= FAULT_TRAINS.get(fault.get("type", ""), set())
    return out


def _easiest(scenarios: list[dict]) -> dict:
    return min(scenarios, key=lambda s: s.get("difficulty", 1))


def recommend_scenario(history: list[dict], scenarios: list[dict]) -> Recommendation | None:
    """
    Подобрать следующий сценарий по истории обучаемого.

    `history` — завершённые тренировки, свежие первыми (`storage.get_trainee_history`).
    `scenarios` — доступные сценарии с полями `id`, `difficulty`, `faults`.

    Правила по убыванию приоритета. Порядок не произвольный: сначала
    закрываем то, что человек только что провалил, и лишь потом двигаемся
    вперёд по сложности.

      1. истории нет — начинаем с самого простого;
      2. последняя тренировка не сдана — повторяем её же;
      3. один и тот же класс ошибок повторяется — берём сценарий, который
         его отрабатывает;
      4. есть непройденные сценарии — следующий по возрастанию сложности;
      5. пройдено всё — возвращаемся к сценарию с худшим баллом.
    """
    if not scenarios:
        return None

    by_code = {s["id"]: s for s in scenarios}

    if not history:
        s = _easiest(scenarios)
        return Recommendation(
            scenario=s["id"], reason="first_training",
            why="Первая тренировка — начинаем с самого простого сценария.")

    last = history[0]
    if last.get("verdict") != "passed" and last["scenario_code"] in by_code:
        return Recommendation(
            scenario=last["scenario_code"], reason="repeat_failed",
            why=f"Прошлая попытка не сдана ({last['total_score']:.0f} баллов). "
                f"Повторяем тот же сценарий, пока он не отработан.")

    # 3. Устойчивая ошибка — важнее движения по сложности: гонять человека
    #    дальше, когда он раз за разом повторяет один промах, бессмысленно.
    counts: dict[str, int] = {}
    for row in history[:RECENT_WINDOW]:
        for cls in (row.get("errors_by_class") or {}):
            counts[cls] = counts.get(cls, 0) + 1
    repeated = [c for c, n in counts.items() if n >= REPEAT_THRESHOLD]
    if repeated:
        # самый частый класс, при равенстве — по имени, чтобы подбор был
        # воспроизводимым, а не зависел от порядка обхода словаря
        worst = sorted(repeated, key=lambda c: (-counts[c], c))[0]
        for s in sorted(scenarios, key=lambda x: x.get("difficulty", 1)):
            if worst in _trains(s):
                cls = CATALOGUE.get(worst)
                title = cls.message.rstrip(".") if cls else worst
                return Recommendation(
                    scenario=s["id"], reason="repeated_error",
                    why=f"Ошибка «{title}» повторяется "
                        f"{counts[worst]} раза из последних "
                        f"{min(len(history), RECENT_WINDOW)}. "
                        f"Этот сценарий её отрабатывает.")

    done = {row["scenario_code"] for row in history}
    fresh = [s for s in scenarios if s["id"] not in done]
    if fresh:
        s = _easiest(fresh)
        return Recommendation(
            scenario=s["id"], reason="next_difficulty",
            why="Предыдущий сценарий сдан. Следующий по сложности "
                "из ещё не пройденных.")

    # 5. Всё пройдено — возвращаемся туда, где результат был худшим.
    best_by_code: dict[str, float] = {}
    for row in history:
        code = row["scenario_code"]
        best_by_code[code] = max(best_by_code.get(code, 0.0), row["total_score"])
    weakest = min((c for c in best_by_code if c in by_code),
                  key=lambda c: (best_by_code[c], c), default=None)
    if weakest is None:
        return Recommendation(
            scenario=_easiest(scenarios)["id"], reason="fallback",
            why="Все сценарии пройдены. Повторяем с начала.")
    return Recommendation(
        scenario=weakest, reason="weakest_result",
        why=f"Все сценарии пройдены. Лучший результат по этому — "
            f"{best_by_code[weakest]:.0f} баллов, он и остаётся самым слабым.")
