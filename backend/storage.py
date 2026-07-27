"""Репозиторий КТК: запись и чтение данных тренировок."""
from __future__ import annotations
import uuid
from datetime import datetime, timezone
from sqlalchemy import select, update, func

from .db import (Session, User, AuditLog, Scenario, ScenarioStep, TrainingSession,
                 OperatorAction, Telemetry, DetectedError, Assessment)
from .models import OperatorAction as ActionSchema, ParameterState, AIFeedback


# ------------------------------------------------------------------ пользователи

async def get_user(login: str) -> dict | None:
    async with Session() as s:
        u = (await s.execute(select(User).where(User.login == login))).scalar_one_or_none()
        return None if u is None else dict(id=u.id, login=u.login,
                                           full_name=u.full_name, role=u.role)


async def get_user_for_auth(login: str) -> dict | None:
    """
    Пользователь вместе с хешем пароля и признаком активности — только для
    процедуры входа. В остальных местах используем get_user/get_user_by_id,
    чтобы хеш не растекался по коду.
    """
    async with Session() as s:
        u = (await s.execute(select(User).where(User.login == login))).scalar_one_or_none()
        return None if u is None else dict(id=u.id, login=u.login,
                                           full_name=u.full_name, role=u.role,
                                           password_hash=u.password_hash, active=u.active)


async def get_user_by_id(user_id: int) -> dict | None:
    """Пользователь по id — проверка, что владелец токена ещё существует и активен."""
    async with Session() as s:
        u = (await s.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
        return None if u is None else dict(id=u.id, login=u.login,
                                           full_name=u.full_name, role=u.role,
                                           active=u.active)


async def set_password_hash(user_id: int, password_hash: str) -> None:
    """Обновить хеш пароля (перевод старых SHA-256 на Argon2id при входе)."""
    async with Session() as s:
        await s.execute(update(User).where(User.id == user_id)
                        .values(password_hash=password_hash))
        await s.commit()


async def audit(user_id: int | None, event: str, details: dict | None = None,
                ip: str | None = None) -> None:
    async with Session() as s:
        s.add(AuditLog(user_id=user_id, event=event, details=details or {}, ip=ip))
        await s.commit()


# ---------------------------------------------------------------------- сценарии

async def list_scenarios() -> list[dict]:
    async with Session() as s:
        rows = (await s.execute(select(Scenario).order_by(Scenario.difficulty))).scalars().all()
        return [dict(id=r.code, name=r.name, description=r.description,
                     initial=r.initial, faults=r.faults, difficulty=r.difficulty)
                for r in rows]


async def get_scenario(code: str) -> dict | None:
    async with Session() as s:
        r = (await s.execute(select(Scenario).where(Scenario.code == code))).scalar_one_or_none()
        return None if r is None else dict(id=r.code, name=r.name,
                                           description=r.description,
                                           initial=r.initial, faults=r.faults)


async def get_reference_steps(code: str) -> list[dict]:
    """Эталонная последовательность шагов — основа для сравнения действий (ИИ)."""
    async with Session() as s:
        sc = (await s.execute(select(Scenario).where(Scenario.code == code))).scalar_one_or_none()
        if sc is None:
            return []
        rows = (await s.execute(select(ScenarioStep)
                .where(ScenarioStep.scenario_id == sc.id)
                .order_by(ScenarioStep.order_no))).scalars().all()
        return [dict(order_no=r.order_no, expected_action=r.expected_action,
                     expected_target=r.expected_target, window_s=r.window_s,
                     description=r.description, critical=r.critical) for r in rows]


# ----------------------------------------------------------------------- сессии

async def create_session(scenario_code: str, operator: str = "unknown",
                         user_id: int | None = None, ip: str | None = None) -> str:
    sid = str(uuid.uuid4())
    async with Session() as s:
        s.add(TrainingSession(id=sid, scenario_code=scenario_code,
                              operator=operator, user_id=user_id))
        await s.commit()
    await audit(user_id, "session_start", {"session": sid, "scenario": scenario_code}, ip=ip)
    return sid


async def finalize_session(session_id: str, status: str = "finished",
                           user_id: int | None = None, ip: str | None = None) -> bool:
    """
    Завершить тренировку — ровно один раз.

    UPDATE ограничен условием `status == 'active'`, поэтому завершить уже
    завершённую тренировку нельзя: возвращается False, и вызывающий код не
    формирует вторую оценку. Это важно, потому что завершение приходит из
    двух мест — по кнопке «Завершить» и при обрыве канала — и они могут
    сработать подряд на одной тренировке.
    """
    async with Session() as s:
        res = await s.execute(
            update(TrainingSession)
            .where(TrainingSession.id == session_id, TrainingSession.status == "active")
            .values(ended_at=datetime.now(timezone.utc), status=status))
        await s.commit()
        if res.rowcount == 0:
            return False
    await audit(user_id, "session_end", {"session": session_id, "status": status}, ip=ip)
    return True


async def list_sessions(limit: int = 50, user_id: int | None = None) -> list[dict]:
    """
    Список тренировок. Если задан user_id — только тренировки этого
    пользователя (оператор видит лишь свои; инструктор и админ — все).
    """
    async with Session() as s:
        q = select(TrainingSession).order_by(TrainingSession.started_at.desc()).limit(limit)
        if user_id is not None:
            q = q.where(TrainingSession.user_id == user_id)
        rows = (await s.execute(q)).scalars().all()
        return [dict(id=r.id, scenario_code=r.scenario_code, operator=r.operator,
                     status=r.status, started_at=str(r.started_at),
                     user_id=r.user_id) for r in rows]


async def get_session_owner(session_id: str) -> dict | None:
    """
    Владелец тренировки — для проверки прав доступа к журналу, ошибкам
    и оценке. None, если такой тренировки нет.
    """
    async with Session() as s:
        r = (await s.execute(select(TrainingSession)
             .where(TrainingSession.id == session_id))).scalar_one_or_none()
        return None if r is None else dict(id=r.id, user_id=r.user_id,
                                           operator=r.operator,
                                           scenario_code=r.scenario_code)


async def get_session_detail(session_id: str) -> dict | None:
    """Шапка разбора: кто, какой сценарий, когда начал и сколько занял."""
    async with Session() as s:
        r = (await s.execute(select(TrainingSession)
             .where(TrainingSession.id == session_id))).scalar_one_or_none()
        if r is None:
            return None
        sc = (await s.execute(select(Scenario)
              .where(Scenario.code == r.scenario_code))).scalar_one_or_none()
        duration = (r.ended_at - r.started_at).total_seconds() if r.ended_at else None
        return dict(id=r.id, scenario_code=r.scenario_code,
                    scenario_name=sc.name if sc else r.scenario_code,
                    scenario_description=sc.description if sc else "",
                    user_id=r.user_id, operator=r.operator, status=r.status,
                    started_at=str(r.started_at),
                    ended_at=str(r.ended_at) if r.ended_at else None,
                    duration_s=round(duration, 1) if duration is not None else None)


# ------------------------------------------------------- действия, телеметрия, ИИ

async def save_action(session_id: str, a: ActionSchema) -> None:
    async with Session() as s:
        s.add(OperatorAction(session_id=session_id, t=a.t, action=a.action.value,
                             target=a.target, value=a.value, reaction_ms=a.reaction_ms))
        await s.commit()


async def save_telemetry(session_id: str, st: ParameterState) -> None:
    async with Session() as s:
        s.add(Telemetry(session_id=session_id, t=st.t, pressure=st.pressure,
                        temperature=st.temperature, flow=st.flow, level=st.level,
                        running=st.running, alarms=st.alarms))
        await s.commit()


async def save_error(session_id: str, fb: AIFeedback) -> None:
    """Сохранить выявленную ИИ ошибку — данные для разбора и обучения модели."""
    if not fb.error_detected or not fb.error_class:
        return
    async with Session() as s:
        s.add(DetectedError(session_id=session_id, t=fb.t, error_class=fb.error_class,
                            location=fb.location, severity=fb.severity.value,
                            message=fb.message, recommendation=fb.recommendation,
                            risk_score=fb.risk_score))
        await s.commit()


async def get_journal(session_id: str) -> list[dict]:
    async with Session() as s:
        rows = (await s.execute(select(OperatorAction)
                .where(OperatorAction.session_id == session_id)
                .order_by(OperatorAction.t, OperatorAction.id))).scalars().all()
        return [dict(t=r.t, action=r.action, target=r.target, value=r.value,
                     reaction_ms=r.reaction_ms) for r in rows]


async def get_errors(session_id: str) -> list[dict]:
    async with Session() as s:
        rows = (await s.execute(select(DetectedError)
                .where(DetectedError.session_id == session_id)
                .order_by(DetectedError.t))).scalars().all()
        return [dict(t=r.t, error_class=r.error_class, location=r.location,
                     severity=r.severity, message=r.message,
                     recommendation=r.recommendation, risk_score=r.risk_score)
                for r in rows]


#: Предел выборки телеметрии за одну тренировку. Час записи с шагом 1 с — это
#: 3600 точек; больше на график всё равно не поместится, а тянуть из БД
#: неограниченную выборку по чужому session_id нельзя.
TELEMETRY_LIMIT = 5000


async def get_telemetry(session_id: str, limit: int = TELEMETRY_LIMIT) -> list[dict]:
    """История параметров установки за тренировку — данные для графика разбора."""
    async with Session() as s:
        rows = (await s.execute(select(Telemetry)
                .where(Telemetry.session_id == session_id)
                .order_by(Telemetry.t).limit(limit))).scalars().all()
        return [dict(t=r.t, pressure=r.pressure, temperature=r.temperature,
                     flow=r.flow, level=r.level, running=r.running, alarms=r.alarms)
                for r in rows]


# ------------------------------------------------- оценка квалификации оператора

#: Разрыв, после которого повторное срабатывание считается новой ошибкой, с
#: модельного времени. ИИ-модуль оценивает состояние на каждом такте, поэтому
#: одна непрекращающаяся авария даёт запись каждую секунду. Для журнала это
#: правильно (видно, сколько она длилась), а для оценки — нет: иначе балл
#: зависел бы от длительности аварии, а не от числа промахов оператора.
EPISODE_GAP_S = 3.0


def group_errors(rows: list[dict]) -> list[dict]:
    """
    Свести подряд идущие записи одного класса в один эпизод.

    Возвращает эпизоды с временем начала и конца и числом записей: разбор
    показывает «давление выше уставки, 40…45 с, 6 замеров» вместо шести
    одинаковых строк.
    """
    episodes: list[dict] = []
    for r in sorted(rows, key=lambda x: x["t"]):
        last = episodes[-1] if episodes else None
        if (last and last["error_class"] == r["error_class"]
                and r["t"] - last["t_to"] <= EPISODE_GAP_S):
            last["t_to"] = r["t"]
            last["count"] += 1
            # держим самую тяжёлую оценку и наибольший риск за эпизод
            if r["severity"] == "error":
                last["severity"] = "error"
            if (r.get("risk_score") or 0) > (last.get("risk_score") or 0):
                last["risk_score"] = r["risk_score"]
            continue
        episodes.append(dict(r, t_from=r["t"], t_to=r["t"], count=1))
    return episodes


#: Штрафы методики оценки, баллы из 100. Вынесены отдельно, чтобы инструктор
#: видел «цену» каждого нарушения, а не искал числа по коду.
PENALTY = dict(step_missed_critical=20.0, step_missed=8.0, step_late=5.0,
               error_critical=25.0, error=8.0)
#: Порог сдачи, баллы.
PASS_SCORE = 70.0


def _match_steps(steps: list[ScenarioStep], actions: list[OperatorAction]) -> list[dict]:
    """
    Сопоставить действия обучаемого с эталонной последовательностью.

    Шаги разбираются по порядку: под каждый берётся первое подходящее действие
    из тех, что идут после засчитанного предыдущего шага. Очерёдность важна —
    на установке значим не только набор операций, но и их последовательность
    (регламент, разд. 7.7.1.1).

    Порядок определяется положением в списке, а не полем `t`: модельное время
    идёт с шагом в секунду, и несколько команд подряд получают одинаковое `t`,
    по которому их уже не различить. Список приходит отсортированным по (t, id),
    то есть в фактическом порядке нажатий.

    Оборудование сверяется, только если оно указано с обеих сторон: интерфейс
    шлёт часть команд без target (клапан сырья на мнемосхеме один), и требовать
    его там значило бы засчитывать верное действие как пропуск.
    """
    after = -1                      # индекс последнего засчитанного действия
    result: list[dict] = []
    for st in steps:
        hit = None
        for i in range(after + 1, len(actions)):
            a = actions[i]
            if a.action != st.expected_action:
                continue
            if st.expected_target and a.target and a.target != st.expected_target:
                continue
            hit = (i, a)
            break
        if hit is None:
            result.append(dict(order_no=st.order_no, description=st.description,
                               expected_action=st.expected_action,
                               expected_target=st.expected_target,
                               window_s=st.window_s, critical=st.critical,
                               done=False, late=False, t=None))
            continue
        i, a = hit
        after = i
        result.append(dict(order_no=st.order_no, description=st.description,
                           expected_action=st.expected_action,
                           expected_target=st.expected_target,
                           window_s=st.window_s, critical=st.critical,
                           done=True, late=a.t > st.window_s, t=a.t))
    return result


async def build_assessment(session_id: str) -> dict:
    """
    Свести итог тренировки: выполнение эталонных шагов, ошибки ИИ, время реакции.

    Методика (базовая, уточняется Константином) — 100 баллов минус штрафы:
    пропуск критического шага −20, обычного −8, выполнение позже отведённого
    окна −5, критическая ошибка ИИ −25, прочая ошибка −8. Порог сдачи — 70.

    Разбор шагов кладётся в `details.steps`: экран разбора показывает по нему,
    что именно обучаемый пропустил, а не только итоговый балл.
    """
    async with Session() as s:
        ts = (await s.execute(select(TrainingSession)
              .where(TrainingSession.id == session_id))).scalar_one_or_none()
        rows = (await s.execute(select(DetectedError)
                .where(DetectedError.session_id == session_id)
                .order_by(DetectedError.t))).scalars().all()
        # в баллах считаем эпизоды, а не отдельные записи (см. group_errors)
        errs = group_errors([dict(t=r.t, error_class=r.error_class,
                                  severity=r.severity, risk_score=r.risk_score)
                             for r in rows])
        # (t, id): внутри одной секунды модельного времени порядок нажатий
        # виден только по идентификатору
        actions = (await s.execute(select(OperatorAction)
                   .where(OperatorAction.session_id == session_id)
                   .order_by(OperatorAction.t, OperatorAction.id))).scalars().all()
        avg_react = (await s.execute(select(func.avg(OperatorAction.reaction_ms))
                .where(OperatorAction.session_id == session_id))).scalar()

        steps: list[ScenarioStep] = []
        if ts is not None:
            sc = (await s.execute(select(Scenario)
                  .where(Scenario.code == ts.scenario_code))).scalar_one_or_none()
            if sc is not None:
                steps = list((await s.execute(select(ScenarioStep)
                             .where(ScenarioStep.scenario_id == sc.id)
                             .order_by(ScenarioStep.order_no))).scalars().all())

        matched = _match_steps(steps, actions)
        missed_critical = sum(1 for m in matched if not m["done"] and m["critical"])
        missed = sum(1 for m in matched if not m["done"] and not m["critical"])
        late = sum(1 for m in matched if m["done"] and m["late"])
        critical = sum(1 for e in errs if e["severity"] == "error")

        penalties = {
            "step_missed_critical": missed_critical * PENALTY["step_missed_critical"],
            "step_missed": missed * PENALTY["step_missed"],
            "step_late": late * PENALTY["step_late"],
            "error_critical": critical * PENALTY["error_critical"],
            "error": (len(errs) - critical) * PENALTY["error"],
        }
        score = max(0.0, 100.0 - sum(penalties.values()))
        verdict = "passed" if score >= PASS_SCORE else "not_passed"

        by_class: dict[str, int] = {}
        for e in errs:
            by_class[e["error_class"]] = by_class.get(e["error_class"], 0) + 1

        row = Assessment(
            session_id=session_id, total_score=round(score, 1),
            errors_count=len(errs), critical_errors=critical,
            avg_reaction_ms=int(avg_react) if avg_react else None,
            verdict=verdict,
            details={"errors_by_class": by_class, "actions": len(actions),
                     "steps": matched, "steps_total": len(matched),
                     "steps_done": sum(1 for m in matched if m["done"]),
                     "steps_late": late, "penalties": penalties,
                     "pass_score": PASS_SCORE,
                     # записей в журнале ошибок больше, чем эпизодов: авария
                     # фиксируется на каждом такте, пока держится
                     "error_records": len(rows)},
        )
        s.add(row)
        await s.commit()
        return dict(session_id=session_id, total_score=row.total_score,
                    errors_count=row.errors_count, critical_errors=critical,
                    avg_reaction_ms=row.avg_reaction_ms, verdict=verdict,
                    details=row.details)


async def get_assessment(session_id: str) -> dict | None:
    async with Session() as s:
        r = (await s.execute(select(Assessment)
             .where(Assessment.session_id == session_id)
             .order_by(Assessment.id.desc()))).scalars().first()
        return None if r is None else dict(
            session_id=r.session_id, total_score=r.total_score,
            errors_count=r.errors_count, critical_errors=r.critical_errors,
            avg_reaction_ms=r.avg_reaction_ms, verdict=r.verdict, details=r.details)


# ------------------------------------------------------------ разбор тренировки

async def get_debrief(session_id: str) -> dict | None:
    """
    Всё, что нужно экрану разбора, одним ответом: шапка, журнал действий,
    телеметрия для графика, найденные ИИ ошибки и итоговая оценка.

    Собрано в один запрос намеренно: раздельная загрузка четырьмя запросами
    давала рассинхронизацию (оценка приходила раньше журнала) и четыре
    проверки прав вместо одной.
    """
    detail = await get_session_detail(session_id)
    if detail is None:
        return None
    errors = await get_errors(session_id)
    return dict(session=detail,
                journal=await get_journal(session_id),
                telemetry=await get_telemetry(session_id),
                errors=errors,
                # эпизоды — то, что показывают на разборе; `errors` остаётся
                # полным журналом, по нему видно длительность аварии
                error_episodes=group_errors(errors),
                assessment=await get_assessment(session_id))
