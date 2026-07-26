# Схема данных КТК ЭЛОУ-АВТ

База: **ktk_eloyavt** (PostgreSQL). 9 таблиц. Каждая закрывает конкретный
критерий оценки кейса — это аргумент для презентации, а не просто хранилище.

| Таблица | Назначение | Критерий |
|---|---|---|
| `users` | пользователи и роли: operator / instructor / admin | ИБ (разграничение доступа), тех. реализация (разделение ролей) |
| `audit_log` | аудит действий пользователей (вход, старт сессии, …) | ИБ (мониторинг и аудит) |
| `scenarios` | учебные сценарии: начальное состояние + отказы | тех. реализация, демонстрация |
| `scenario_steps` | **эталонная** последовательность действий | ИИ (сравнение с эталоном) |
| `training_sessions` | сессии тренировок: кто, когда, какой сценарий | демонстрация, оценка квалификации |
| `operator_actions` | журнал действий + **время реакции** | тех. реализация (журнал, время) |
| `telemetry` | история параметров установки по секундам | демонстрация, разбор с инструктором |
| `detected_errors` | выход ИИ: класс, локализация, объяснение, риск | ИИ (классификация и локализация) |
| `assessments` | итог: баллы, вердикт, среднее время реакции | оценка квалификации персонала |

## Связи

```
users ──< training_sessions ──< operator_actions
                │              ──< telemetry
                │              ──< detected_errors
                └────────────────< assessments
scenarios ──< scenario_steps
users ──< audit_log
```

## Методика оценки (черновая, уточняет Константин)

100 баллов минус штрафы: критическая ошибка −25, некритическая −8.
Порог сдачи — 70 баллов. Дополнительно считаются: количество ошибок по классам,
среднее время реакции, число действий. Всё сохраняется в `assessments.details`.

## Начальные данные (создаются автоматически)

- Пользователи: `operator`, `instructor`, `admin` (пароль = логин, учебная заглушка —
  на неделе 4 заменить на bcrypt/argon2, задача по ИБ).
- Сценарии: `startup` (пуск), `pump_trip` (отказ насоса), `pressure_alarm` (рост давления)
  — с эталонными шагами для каждого.

## Полезные запросы

```sql
-- последние тренировки с оценками
SELECT s.scenario_code, s.operator, a.total_score, a.verdict, a.errors_count
FROM training_sessions s LEFT JOIN assessments a ON a.session_id = s.id
ORDER BY s.started_at DESC LIMIT 10;

-- какие ошибки оператор допускает чаще всего
SELECT error_class, COUNT(*) FROM detected_errors GROUP BY error_class ORDER BY 2 DESC;

-- среднее время реакции по сценариям
SELECT s.scenario_code, ROUND(AVG(a.reaction_ms)) AS avg_ms
FROM operator_actions a JOIN training_sessions s ON s.id = a.session_id
GROUP BY s.scenario_code;

-- эталон против факта: что оператор должен был сделать
SELECT st.order_no, st.expected_action, st.description, st.critical
FROM scenario_steps st JOIN scenarios sc ON sc.id = st.scenario_id
WHERE sc.code = 'pump_trip' ORDER BY st.order_no;
```
